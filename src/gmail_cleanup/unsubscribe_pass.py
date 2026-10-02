"""
unsubscribe_pass.py — Unsubscribe pass for Archive/Marketing emails.

Public functions:
    fetch_marketing_unsubscribes  — fetch List-Unsubscribe headers from Archive/Marketing
    run_unsubscribe_mailto        — send unsubscribe emails via Gmail API
    run_unsubscribe_http          — make HTTP GET requests to https: unsubscribe URLs
    write_pending / load_pending / clear_pending
                                  — approval gate: candidates are written to a
                                    pending file and only sent by an explicit
                                    `unsubscribe-send` command
"""
import base64
import json
import re
import time
from collections import defaultdict
from datetime import UTC, datetime
from email.mime.text import MIMEText

from gmail_cleanup.config import BASE_DIR
from gmail_cleanup.fetch import BATCH_SIZE, extract_header, extract_sender
from gmail_cleanup.gmail_ops import _api_call_with_retry, _get_or_create_label
from gmail_cleanup.keep import load_keep_senders

try:
    import requests as _requests
    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False

_MARKETING_LABEL = "Archive/Marketing"
_RATE_DELAY = 0.5  # seconds between API calls
# Only recent marketing mail nominates unsubscribe candidates — senders that
# stopped mailing (already unsubscribed or blocked) age out of the list.
_CANDIDATE_WINDOW = "newer_than:60d"


def _parse_list_unsubscribe(header_value: str) -> dict:
    """Extract mailto: and https: URLs from a List-Unsubscribe header value.

    The header may contain comma-separated angle-bracket entries like:
        <mailto:unsub@example.com?subject=unsub>, <https://example.com/unsub>

    Returns {"mailto": str|None, "https": str|None}
    """
    result = {"mailto": None, "https": None}
    if not header_value:
        return result
    # Extract all <...> tokens
    tokens = re.findall(r"<([^>]+)>", header_value)
    for token in tokens:
        token = token.strip()
        if token.lower().startswith("mailto:") and result["mailto"] is None:
            result["mailto"] = token
        elif token.lower().startswith("https://") and result["https"] is None:
            result["https"] = token
        elif token.lower().startswith("http://") and result["https"] is None:
            result["https"] = token
    return result


def fetch_marketing_unsubscribes(
    service,
    user_id: str = "me",
    keep_senders: frozenset = None,
) -> dict:
    """Fetch List-Unsubscribe + From headers from all Archive/Marketing messages.

    Keep-listed senders are excluded — they must never be unsubscribed, even if
    old mail from them is still sitting under Archive/Marketing.

    Returns:
        {sender_email: {"mailto": str|None, "https": str|None, "count": int}}
        Deduped by sender; first-seen URL is kept.
    """
    if keep_senders is None:
        keep_senders = load_keep_senders()
    already_unsubscribed = set(load_ledger())
    # Resolve label ID for Archive/Marketing
    label_cache: dict = {}
    try:
        label_id = _get_or_create_label(service, user_id, _MARKETING_LABEL, label_cache)
    except Exception as e:
        print(f"ERROR: could not resolve label '{_MARKETING_LABEL}': {e}")
        return {}

    # List all message IDs in Archive/Marketing
    print(f"Listing messages in {_MARKETING_LABEL}...")
    page_token = None
    all_ids = []

    while True:
        kwargs = {
            "userId": user_id,
            "labelIds": [label_id],
            "q": _CANDIDATE_WINDOW,
            "maxResults": 500,
        }
        if page_token:
            kwargs["pageToken"] = page_token
        resp = _api_call_with_retry(
            lambda kw=kwargs: service.users().messages().list(**kw).execute()
        )
        all_ids.extend(m["id"] for m in resp.get("messages", []))
        page_token = resp.get("nextPageToken")
        print(f"  Listed {len(all_ids)} messages...", end="\r")
        if not page_token:
            break

    print(f"\n  Total Archive/Marketing messages: {len(all_ids)}")
    if not all_ids:
        return {}

    # Batch-fetch From + List-Unsubscribe headers
    unsub_map: dict = defaultdict(lambda: {"mailto": None, "https": None, "count": 0})
    errors = 0

    for i in range(0, len(all_ids), BATCH_SIZE):
        batch_ids = all_ids[i: i + BATCH_SIZE]
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
                    metadataHeaders=["From", "List-Unsubscribe"],
                ),
                callback=make_callback(responses),
            )

        try:
            batch.execute()
        except Exception:
            errors += 1
            time.sleep(2)
            continue

        for raw in responses:
            headers = raw.get("payload", {}).get("headers", [])
            from_value = extract_header(headers, "From") or ""
            sender = extract_sender(from_value)
            unsub_header = extract_header(headers, "List-Unsubscribe")

            unsub_map[sender]["count"] += 1

            if unsub_header and (
                unsub_map[sender]["mailto"] is None
                and unsub_map[sender]["https"] is None
            ):
                parsed = _parse_list_unsubscribe(unsub_header)
                if parsed["mailto"]:
                    unsub_map[sender]["mailto"] = parsed["mailto"]
                if parsed["https"]:
                    unsub_map[sender]["https"] = parsed["https"]

        fetched = min(i + BATCH_SIZE, len(all_ids))
        print(f"  Fetched {fetched}/{len(all_ids)} headers...", end="\r")
        time.sleep(0.2)

    print(f"\n  Headers fetched. Errors: {errors}")
    # Filter to only senders that have at least one unsubscribe mechanism,
    # excluding keep-listed senders.
    result = {
        sender: dict(info)
        for sender, info in unsub_map.items()
        if (info["mailto"] or info["https"])
        and sender not in keep_senders
        and sender not in already_unsubscribed
    }
    protected = sum(
        1 for sender, info in unsub_map.items()
        if (info["mailto"] or info["https"]) and sender in keep_senders
    )
    if protected:
        print(f"  Keep-list: excluded {protected} protected sender(s)")
    print(f"  Distinct senders with unsubscribe headers: {len(result)}")
    return result


# ---------------------------------------------------------------------------
# Approval gate: pending-candidates file
# ---------------------------------------------------------------------------

_LEDGER_PATH = BASE_DIR / "unsubscribed_senders.json"


def load_ledger(path=_LEDGER_PATH) -> dict:
    """Cumulative {sender: date_unsubscribed} across all sends (never overwritten)."""
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def record_unsubscribed(senders, path=_LEDGER_PATH):
    """Append senders to the permanent unsubscribe ledger."""
    ledger = load_ledger(path)
    today = datetime.now(UTC).date().isoformat()
    for s in senders:
        ledger.setdefault(s, today)
    path.write_text(json.dumps(ledger, indent=2, sort_keys=True))
    print(f"  Ledger: {len(ledger)} senders unsubscribed to date ({path.name})")


def _pending_path(account: str):
    safe = account.replace("@", "_").replace(".", "_")
    return BASE_DIR / f"unsubscribe_pending_{safe}.json"


def write_pending(unsub_map: dict, account: str):
    """Write unsubscribe candidates for later approval. Overwrites any previous file."""
    path = _pending_path(account)
    path.write_text(json.dumps({
        "generated_at": datetime.now(UTC).isoformat(),
        "account": account,
        "candidates": unsub_map,
    }, indent=2))
    print(f"  Wrote {len(unsub_map)} unsubscribe candidate(s) to {path}")
    return path


def load_pending(account: str) -> dict:
    """Load pending unsubscribe candidates. Returns {} if none."""
    path = _pending_path(account)
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def clear_pending(account: str):
    """Delete the pending file after a successful send (prevents double-send)."""
    path = _pending_path(account)
    if path.exists():
        path.unlink()
        print(f"  Cleared pending file {path}")


def run_unsubscribe_mailto(
    service,
    unsub_map: dict,
    dry_run: bool = True,
    user_id: str = "me",
) -> dict:
    """Send unsubscribe emails via Gmail API for senders with a mailto: address.

    Returns:
        {"sent": [senders], "failed": [senders]}
    """
    sent = []
    failed = []

    senders_with_mailto = [s for s, v in unsub_map.items() if v["mailto"]]
    if not senders_with_mailto:
        print("No mailto: unsubscribe addresses found.")
        return {"sent": sent, "failed": failed}

    print(f"\n{'[DRY RUN] ' if dry_run else ''}Sending {len(senders_with_mailto)} mailto unsubscribes...")

    for sender in senders_with_mailto:
        mailto_url = unsub_map[sender]["mailto"]
        # Parse address and optional subject from mailto:
        # e.g. "mailto:unsub@example.com?subject=unsubscribe"
        address = mailto_url[len("mailto:"):]
        subject = "unsubscribe"
        if "?" in address:
            address, qs = address.split("?", 1)
            for param in qs.split("&"):
                if param.lower().startswith("subject="):
                    subject = param[len("subject="):]
                    break

        if dry_run:
            print(f"  [DRY RUN] Would send to: {address} (subject: {subject})")
            sent.append(sender)
            continue

        try:
            msg = MIMEText("")
            msg["To"] = address
            msg["Subject"] = subject
            msg["From"] = "me"
            raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
            _api_call_with_retry(
                lambda r=raw: service.users().messages().send(
                    userId=user_id,
                    body={"raw": r},
                ).execute()
            )
            sent.append(sender)
            print(f"  Sent: {address}")
        except Exception as e:
            failed.append(sender)
            print(f"  Failed ({sender}): {e}")

        time.sleep(_RATE_DELAY)

    print(f"  mailto done: {len(sent)} sent, {len(failed)} failed")
    return {"sent": sent, "failed": failed}


def run_unsubscribe_http(unsub_map: dict, dry_run: bool = True) -> dict:
    """Make HTTP GET requests to https: unsubscribe URLs.

    Returns:
        {"success": [senders], "needs_manual": [senders], "failed": [senders]}
    """
    success = []
    needs_manual = []
    failed = []

    senders_with_https = [s for s, v in unsub_map.items() if v["https"]]
    if not senders_with_https:
        print("No https: unsubscribe URLs found.")
        return {"success": success, "needs_manual": needs_manual, "failed": failed}

    if not _HAS_REQUESTS:
        print("WARNING: 'requests' library not installed. Skipping HTTP unsubscribes.")
        print("Install with: pip install requests")
        needs_manual.extend(senders_with_https)
        return {"success": success, "needs_manual": needs_manual, "failed": failed}

    print(f"\n{'[DRY RUN] ' if dry_run else ''}Sending {len(senders_with_https)} http unsubscribes...")

    for sender in senders_with_https:
        url = unsub_map[sender]["https"]

        if dry_run:
            print(f"  [DRY RUN] Would GET: {url}")
            success.append(sender)
            continue

        try:
            resp = _requests.get(url, timeout=10, allow_redirects=True)
            body_lower = resp.text.lower() if resp.text else ""
            if resp.status_code == 200 and "error" not in body_lower[:500]:
                success.append(sender)
                print(f"  OK ({resp.status_code}): {url[:60]}")
            else:
                needs_manual.append(sender)
                print(f"  Needs manual ({resp.status_code}): {url[:60]}")
        except Exception as e:
            failed.append(sender)
            print(f"  Failed ({sender}): {e}")

        time.sleep(_RATE_DELAY)

    print(
        f"  http done: {len(success)} success, "
        f"{len(needs_manual)} needs manual, {len(failed)} failed"
    )
    return {"success": success, "needs_manual": needs_manual, "failed": failed}
