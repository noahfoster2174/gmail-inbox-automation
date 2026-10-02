"""
inbox_ops.py — Core inbox reduction module (Iteration 2).

Four public functions:
    fetch_inbox_unread  — query Gmail for current unread inbox messages
    run_inbox_archive   — batch-archive matched senders, skip flagged subjects
    run_inbox_filters   — create Gmail filters so future mail skips inbox
    run_inbox_review    — report on what remains after archive pass
"""
import time
from collections import defaultdict

from googleapiclient.errors import HttpError

from gmail_cleanup.classify import contains_flag_keywords, is_newsletter
from gmail_cleanup.config import (
    AGE_OUT_DAYS,
    NOTIFICATION_SENDERS,
    NOTIFY_SENDERS,
    READ_RATE_KEEP_THRESHOLD,
    READ_RATE_MIN_EMAILS,
    SKIP_INBOX_SENDERS,
)
from gmail_cleanup.db import Database
from gmail_cleanup.fetch import BATCH_SIZE, extract_header, extract_sender
from gmail_cleanup.gmail_ops import _api_call_with_retry, _get_or_create_label
from gmail_cleanup.keep import is_blocked, load_blocked_senders, load_keep_senders

_INBOX_BATCH_SIZE = BATCH_SIZE  # 50 — reuse fetch.py constant
_FETCH_RETRY_ROUNDS = 6  # re-request per-item batch failures until complete


# ---------------------------------------------------------------------------
# 1. Fetch unread inbox messages directly from Gmail (source of truth)
# ---------------------------------------------------------------------------

def fetch_inbox_unread(service, user_id: str = "me") -> dict:
    """Return {gmail_id: {"sender", "subject", "list_unsubscribe", "precedence"}} for all unread inbox messages.

    Does NOT touch the local DB — Gmail is authoritative for live inbox state.
    """
    print("Fetching unread inbox message list from Gmail...")
    page_token = None
    all_ids = []

    while True:
        kwargs = {
            "userId": user_id,
            "q": "in:inbox is:unread",
            "maxResults": 500,
        }
        if page_token:
            kwargs["pageToken"] = page_token
        resp = service.users().messages().list(**kwargs).execute()
        all_ids.extend(m["id"] for m in resp.get("messages", []))
        page_token = resp.get("nextPageToken")
        print(f"  Listed {len(all_ids)} unread inbox messages...", end="\r")
        if not page_token:
            break

    print(f"\n  Total unread inbox: {len(all_ids)}")

    # Batch-fetch From + Subject metadata. Individual requests inside a Gmail
    # batch can fail (rate limits) without the batch itself raising, so retry
    # the stragglers until none remain — otherwise messages silently vanish
    # from the decision set (observed 45% loss under per-item throttling).
    result: dict = {}
    missing = list(all_ids)

    for round_no in range(_FETCH_RETRY_ROUNDS):
        if not missing:
            break
        chunk_size = _INBOX_BATCH_SIZE if round_no == 0 else _INBOX_BATCH_SIZE // 2
        pause = 0.2 * (round_no + 1)

        for i in range(0, len(missing), chunk_size):
            batch_ids = missing[i: i + chunk_size]
            batch = service.new_batch_http_request()
            responses = []

            def make_callback(r):
                def callback(request_id, response, exception):
                    if exception is None and response:
                        r.append(response)
                return callback

            for msg_id in batch_ids:
                batch.add(
                    service.users().messages().get(
                        userId=user_id,
                        id=msg_id,
                        format="metadata",
                        metadataHeaders=["From", "Subject", "List-Unsubscribe", "Precedence"],
                    ),
                    callback=make_callback(responses),
                )

            try:
                batch.execute()
            except Exception:
                time.sleep(2)
                continue

            for raw in responses:
                headers = raw.get("payload", {}).get("headers", [])
                from_value = extract_header(headers, "From") or ""
                sender = extract_sender(from_value)
                subject = extract_header(headers, "Subject") or "(no subject)"
                result[raw["id"]] = {
                    "sender": sender,
                    "subject": subject,
                    "list_unsubscribe": extract_header(headers, "List-Unsubscribe"),
                    "precedence": extract_header(headers, "Precedence"),
                    "ts": int(raw.get("internalDate", 0)),
                }

            time.sleep(pause)

        missing = [m for m in all_ids if m not in result]
        print(f"  Metadata round {round_no + 1}: {len(result)}/{len(all_ids)} fetched, {len(missing)} missing")
        if missing:
            time.sleep(2 * (round_no + 1))

    if missing:
        print(f"  WARNING: {len(missing)} messages still missing metadata after "
              f"{_FETCH_RETRY_ROUNDS} rounds — they will be left untouched this run")

    print(f"\n  Metadata ready: {len(result)}/{len(all_ids)}")
    return result


# ---------------------------------------------------------------------------
# 2. Archive matched senders, skipping any flagged subjects
# ---------------------------------------------------------------------------

def plan_inbox_archive(
    inbox_unread: dict,
    keep_senders: frozenset,
    sender_stats: dict,
    blocked_senders: frozenset = frozenset(),
    now_ms: float = None,
) -> dict:
    """Pure routing decision — no Gmail service, no side effects.

    Decision order per message:
        1. keep-list             → kept_list (beats everything, incl. block-list)
        2. block-list            → to_trash (straight to Gmail trash)
        3. FLAG_KEYWORDS subject → skipped_flagged
        4. age-out               → to_archive["Archive/Cleanup"] for unread older
                                   than AGE_OUT_DAYS (needs meta["ts"]; messages
                                   without a timestamp are never aged out)
        5. engagement override   → kept_engaged (read_rate >= READ_RATE_KEEP_THRESHOLD
                                   on >= READ_RATE_MIN_EMAILS; beats the
                                   marketing/newsletter branch — but NOT
                                   NOTIFICATION_SENDERS routing: notification-tier
                                   mail is a record, the app already notified you)
        6. NOTIFICATION_SENDERS  → to_archive[configured label]
        7. is_newsletter headers → to_archive["Archive/Marketing"]
        8. else                  → skipped_unknown

    sender_stats: {sender: {"total_count": int, "read_rate": float}}

    Returns:
        {
            "to_archive":      {label_name: [gmail_id]},
            "to_trash":        [gmail_id],
            "aged_out":        [gmail_id],   # subset of to_archive["Archive/Cleanup"]
            "skipped_unknown": [gmail_id],
            "skipped_flagged": [gmail_id],
            "kept_list":       [gmail_id],
            "kept_engaged":    [gmail_id],
        }
    """
    if now_ms is None:
        now_ms = time.time() * 1000
    age_cutoff_ms = AGE_OUT_DAYS * 86400 * 1000

    to_archive: dict = defaultdict(list)
    to_trash: list = []
    aged_out: list = []
    skipped_unknown: list = []
    skipped_flagged: list = []
    kept_list: list = []
    kept_engaged: list = []

    for gmail_id, meta in inbox_unread.items():
        sender = meta["sender"]
        subject = meta["subject"]

        if sender in keep_senders:
            kept_list.append(gmail_id)
            continue

        if is_blocked(sender, blocked_senders):
            to_trash.append(gmail_id)
            continue

        if contains_flag_keywords(subject):
            skipped_flagged.append(gmail_id)
            continue

        ts = meta.get("ts") or 0
        if ts and (now_ms - ts) > age_cutoff_ms:
            aged_out.append(gmail_id)
            to_archive["Archive/Cleanup"].append(gmail_id)
            continue

        stats = sender_stats.get(sender)
        if (
            sender not in NOTIFICATION_SENDERS
            and stats
            and stats["total_count"] >= READ_RATE_MIN_EMAILS
            and stats["read_rate"] >= READ_RATE_KEEP_THRESHOLD
        ):
            kept_engaged.append(gmail_id)
            continue

        label_name = NOTIFICATION_SENDERS.get(sender)
        if label_name is None:
            # Second pass: detect marketing/newsletter by headers even if sender unknown
            if is_newsletter(meta):
                to_archive["Archive/Marketing"].append(gmail_id)
            else:
                skipped_unknown.append(gmail_id)
            continue

        to_archive[label_name].append(gmail_id)

    return {
        "to_archive": dict(to_archive),
        "to_trash": to_trash,
        "aged_out": aged_out,
        "skipped_unknown": skipped_unknown,
        "skipped_flagged": skipped_flagged,
        "kept_list": kept_list,
        "kept_engaged": kept_engaged,
    }


def run_inbox_archive(
    service,
    inbox_unread: dict,
    account: str,
    db: Database,
    dry_run: bool = True,
    user_id: str = "me",
    keep_senders: frozenset = None,
    blocked_senders: frozenset = None,
) -> dict:
    """Archive unread inbox messages whose sender is in NOTIFICATION_SENDERS,
    plus any message that looks like marketing (List-Unsubscribe / Precedence: bulk).

    Marketing emails go to Archive/Marketing (separate label for future unsubscribe pass).
    Safety gates, in order: keep_senders.txt, FLAG_KEYWORDS subjects, and the
    engagement override (senders the user actually reads are never archived).

    Returns:
        {
            "archived":         int,
            "skipped_unknown":  list[str],   # ids that are truly unknown (not marketing)
            "skipped_flagged":  list[str],   # ids skipped due to FLAG_KEYWORDS
            "kept_list":        list[str],   # ids protected by keep_senders.txt
            "kept_engaged":     list[str],   # ids protected by read-rate override
            "plan":             dict,        # full plan_inbox_archive() output
        }
    """
    label_cache: dict = {}

    if keep_senders is None:
        keep_senders = load_keep_senders()
    if blocked_senders is None:
        blocked_senders = load_blocked_senders()

    # Engagement stats for each distinct sender in this batch (from the local DB)
    sender_stats: dict = {}
    for meta in inbox_unread.values():
        sender = meta["sender"]
        if sender not in sender_stats:
            row = db.get_sender(sender)
            if row:
                sender_stats[sender] = {
                    "total_count": row["total_count"],
                    "read_rate": row["read_rate"],
                }

    plan = plan_inbox_archive(inbox_unread, keep_senders, sender_stats, blocked_senders)
    to_archive = plan["to_archive"]
    skipped_unknown = plan["skipped_unknown"]
    skipped_flagged = plan["skipped_flagged"]

    def _senders_of(ids):
        return sorted({inbox_unread[gid]["sender"] for gid in ids})

    total_matched = sum(len(ids) for ids in to_archive.values())
    print(f"\n[{'DRY RUN — ' if dry_run else ''}archive for {account}]")
    print(f"  Matched (to archive):  {total_matched}")
    print(f"  Aged out (>{AGE_OUT_DAYS}d unread): {len(plan['aged_out'])}")
    print(f"  Blocked (to trash):    {len(plan['to_trash'])}")
    for s in _senders_of(plan["to_trash"]):
        print(f"    - {s}")
    print(f"  Skipped (unknown):     {len(skipped_unknown)}")
    print(f"  Skipped (flag kw):     {len(skipped_flagged)}")
    print(f"  Kept (keep-list):      {len(plan['kept_list'])}")
    for s in _senders_of(plan["kept_list"]):
        print(f"    - {s}")
    print(f"  Kept (engaged):        {len(plan['kept_engaged'])}")
    for s in _senders_of(plan["kept_engaged"]):
        stats = sender_stats.get(s, {})
        print(f"    - {s} (read_rate {stats.get('read_rate', 0):.2f} over {stats.get('total_count', 0)} emails)")

    if dry_run:
        print("\nPer-label breakdown:")
        for label_name, ids in sorted(to_archive.items()):
            print(f"  {label_name}: {len(ids)}")
        return {
            "archived": 0,
            "skipped_unknown": skipped_unknown,
            "skipped_flagged": skipped_flagged,
            "kept_list": plan["kept_list"],
            "kept_engaged": plan["kept_engaged"],
            "plan": plan,
        }

    archived_ids: list = []

    for label_name, ids in to_archive.items():
        try:
            label_id = _get_or_create_label(service, user_id, label_name, label_cache)
        except Exception as e:
            print(f"  Warning: could not get/create label '{label_name}': {e}")
            continue

        for i in range(0, len(ids), 1000):
            batch = ids[i: i + 1000]
            try:
                _api_call_with_retry(
                    lambda b=batch, lid=label_id: service.users().messages().batchModify(
                        userId=user_id,
                        body={
                            "ids": b,
                            "removeLabelIds": ["INBOX", "UNREAD"],
                            "addLabelIds": [lid],
                        },
                    ).execute()
                )
                archived_ids.extend(batch)
            except HttpError as e:
                if e.resp.status == 400:
                    print(f"  Warning: batch had bad IDs for label '{label_name}', skipping chunk")
                else:
                    raise
            print(f"  Archived {len(archived_ids)} so far (label: {label_name})...")
            time.sleep(1)

    # Block-list: straight to trash
    trashed_ids: list = []
    for i in range(0, len(plan["to_trash"]), 1000):
        batch = plan["to_trash"][i: i + 1000]
        _api_call_with_retry(
            lambda b=batch: service.users().messages().batchModify(
                userId=user_id,
                body={
                    "ids": b,
                    "addLabelIds": ["TRASH"],
                    "removeLabelIds": ["INBOX", "UNREAD"],
                },
            ).execute()
        )
        trashed_ids.extend(batch)
        time.sleep(1)

    # Audit trail — recorded in decisions, NOT emails.is_read, so tool-archived
    # mail never counts as user engagement in sender read_rate stats.
    if archived_ids:
        db.record_auto_archive(archived_ids)
    if trashed_ids:
        db.record_auto_archive(trashed_ids, action="AUTO_TRASH")

    print(f"\nArchive complete. Total archived: {len(archived_ids)}, trashed (blocked): {len(trashed_ids)}")
    return {
        "archived": len(archived_ids),
        "skipped_unknown": skipped_unknown,
        "skipped_flagged": skipped_flagged,
        "kept_list": plan["kept_list"],
        "kept_engaged": plan["kept_engaged"],
        "plan": plan,
    }


# ---------------------------------------------------------------------------
# 3. Create Gmail filters so future mail from these senders skips inbox
# ---------------------------------------------------------------------------

def run_inbox_filters(service, user_id: str = "me", keep_senders: frozenset = None,
                      blocked_senders: frozenset = None):
    """Create tiered Gmail filters for all known senders.

    Tier 1 (SKIP_INBOX_SENDERS): removeLabelIds=["INBOX"] — never hits inbox.
    Tier 2 (NOTIFY_SENDERS): addLabelIds=["Notify"] — stays in inbox, labeled
        for easy bulk-archive (select all Notify → archive in one tap).
    Tier 3 (TIME_SENSITIVE_SENDERS): no filter — hits inbox normally.
    Block-list (blocked_senders.txt): one combined filter sends their mail
        straight to trash at delivery.

    Keep-listed senders never get a skip-inbox filter, even if present in
    SKIP_INBOX_SENDERS.

    Purges all existing filters before creating new ones to stay under Gmail's
    1,000-filter limit.
    """
    if keep_senders is None:
        keep_senders = load_keep_senders()
    if blocked_senders is None:
        blocked_senders = load_blocked_senders()
    # --- Purge existing filters ---
    existing = service.users().settings().filters().list(userId=user_id).execute()
    old_filters = existing.get("filter", [])
    if old_filters:
        print(f"  Purging {len(old_filters)} existing filters...")
        deleted = 0
        for f in old_filters:
            try:
                service.users().settings().filters().delete(
                    userId=user_id, id=f["id"]
                ).execute()
                deleted += 1
            except HttpError:
                pass
            time.sleep(0.05)
        print(f"  Deleted {deleted} old filters.")

    # --- Get or create the "Notify" label ---
    notify_label_cache: dict = {}
    try:
        notify_label_id = _get_or_create_label(service, user_id, "Notify", notify_label_cache)
        print(f"  'Notify' label ready (id: {notify_label_id})")
    except Exception as e:
        print(f"  Warning: could not create 'Notify' label: {e}")
        notify_label_id = None

    skip_count = len(SKIP_INBOX_SENDERS)
    notify_count = len(NOTIFY_SENDERS)
    print(f"\nCreating filters: {skip_count} skip-inbox + {notify_count} notify-label...")

    created_skip = 0
    created_notify = 0
    skipped = 0

    # Tier 1: skip inbox
    for sender in SKIP_INBOX_SENDERS:
        if sender in keep_senders:
            print(f"  Keep-list: no skip-inbox filter for {sender}")
            continue
        try:
            service.users().settings().filters().create(
                userId=user_id,
                body={
                    "criteria": {"from": sender},
                    "action": {"removeLabelIds": ["INBOX"]},
                },
            ).execute()
            created_skip += 1
        except HttpError as e:
            skipped += 1
            print(f"  Skipped (skip) {sender}: {e.resp.status} {e.reason}")
        time.sleep(0.1)

    # Tier 2: notify label (keep in inbox, tag for easy dismissal)
    if notify_label_id:
        for sender in NOTIFY_SENDERS:
            try:
                service.users().settings().filters().create(
                    userId=user_id,
                    body={
                        "criteria": {"from": sender},
                        "action": {"addLabelIds": [notify_label_id]},
                    },
                ).execute()
                created_notify += 1
            except HttpError as e:
                skipped += 1
                print(f"  Skipped (notify) {sender}: {e.resp.status} {e.reason}")
            time.sleep(0.1)

    # Block-list: one combined delivery-time filter → trash
    created_block = 0
    to_block = sorted(s for s in blocked_senders if s not in keep_senders)
    if to_block:
        try:
            service.users().settings().filters().create(
                userId=user_id,
                body={
                    "criteria": {"from": " OR ".join(to_block)},
                    "action": {"addLabelIds": ["TRASH"], "removeLabelIds": ["INBOX"]},
                },
            ).execute()
            created_block = 1
            print(f"  Block filter created for {len(to_block)} sender(s)/domain(s)")
        except HttpError as e:
            skipped += 1
            print(f"  Skipped (block filter): {e.resp.status} {e.reason}")

    print(
        f"Filters done. "
        f"Skip-inbox: {created_skip}/{skip_count}, "
        f"Notify-label: {created_notify}/{notify_count}, "
        f"Block: {created_block}, "
        f"Errors: {skipped}"
    )


# ---------------------------------------------------------------------------
# 4. Review what remains after archive — ranked report of unknown senders
# ---------------------------------------------------------------------------

def run_inbox_review(
    service,
    skipped_ids: list,
    db: Database,
    user_id: str = "me",
):
    """Print a ranked table of remaining unknown senders so you can expand NOTIFICATION_SENDERS.

    Read-only — does NOT modify Gmail or the DB.
    """
    if not skipped_ids:
        print("\nNothing to review — no skipped messages.")
        return

    print(f"\nFetching metadata for {len(skipped_ids)} skipped messages...")

    sender_data: dict = defaultdict(lambda: {"count": 0, "category": None})

    for i in range(0, len(skipped_ids), _INBOX_BATCH_SIZE):
        batch_ids = skipped_ids[i: i + _INBOX_BATCH_SIZE]
        batch = service.new_batch_http_request()
        responses = []

        def make_callback(r):
            def callback(request_id, response, exception):
                if exception is None and response:
                    r.append(response)
            return callback

        for msg_id in batch_ids:
            batch.add(
                service.users().messages().get(
                    userId=user_id,
                    id=msg_id,
                    format="metadata",
                    metadataHeaders=["From"],
                ),
                callback=make_callback(responses),
            )

        try:
            batch.execute()
        except Exception:
            time.sleep(2)
            continue

        for raw in responses:
            headers = raw.get("payload", {}).get("headers", [])
            from_value = extract_header(headers, "From") or ""
            sender = extract_sender(from_value)
            sender_data[sender]["count"] += 1

            # Try to look up category from local DB
            if sender_data[sender]["category"] is None:
                row = db.get_sender(sender)
                if row:
                    sender_data[sender]["category"] = row["sender"]  # best we have

        time.sleep(0.2)

    # Sort by volume descending
    ranked = sorted(sender_data.items(), key=lambda x: x[1]["count"], reverse=True)

    print(f"\n{'Sender':<55} {'Count':>6}  {'DB Category'}")
    print("-" * 80)
    for sender, info in ranked:
        cat = info["category"] or "—"
        print(f"{sender:<55} {info['count']:>6}  {cat}")

    print(f"\nTotal distinct senders: {len(ranked)}")
    print("Add high-volume senders to NOTIFICATION_SENDERS in config.py, then re-run archive.")
