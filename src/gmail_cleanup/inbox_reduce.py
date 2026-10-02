"""
inbox_reduce.py — Inbox Zero Helper CLI (Iteration 3).

Usage:
    PYTHONPATH=src .venv/bin/python -m gmail_cleanup.inbox_reduce <phase> [options]

Phases:
    sync         Query Gmail for current unread inbox; cache to ~/gmail_cleanup/inbox_cache_<account>.json
    archive      Archive matched NOTIFICATION_SENDERS (default: dry-run)
    filters      Create Gmail filters so future mail from those senders skips inbox
    review       Show ranked report of remaining unknown senders
    unsubscribe  Build unsubscribe candidates for Archive/Marketing senders
                 (writes a pending file — NEVER sends)
    unsubscribe-send
                 Send the pending unsubscribe candidates (asks for confirmation
                 unless --yes)
    all          Run sync → archive → review → unsubscribe (build-only)

Options:
    --account EMAIL     Gmail account to process (default: first configured account)
    --all-accounts      Run for all configured accounts in sequence
    --dry-run           Preview changes without modifying Gmail (default for archive)
    --no-dry-run        Actually apply archive changes
    --yes               Skip the confirmation prompt in unsubscribe-send

Senders listed in ~/gmail_cleanup/keep_senders.txt always stay in the inbox and
are never unsubscribed.

Example sequence:
    python -m gmail_cleanup.inbox_reduce sync
    python -m gmail_cleanup.inbox_reduce archive --dry-run
    python -m gmail_cleanup.inbox_reduce archive --no-dry-run
    python -m gmail_cleanup.inbox_reduce review
    python -m gmail_cleanup.inbox_reduce filters
    python -m gmail_cleanup.inbox_reduce unsubscribe
    python -m gmail_cleanup.inbox_reduce unsubscribe-send
    python -m gmail_cleanup.inbox_reduce sync --all-accounts
    python -m gmail_cleanup.inbox_reduce all --all-accounts --no-dry-run
"""
import argparse
import json
import sys
from pathlib import Path

from gmail_cleanup.auth import get_service
from gmail_cleanup.config import CREDENTIALS_PATH, DB_PATH, GMAIL_ACCOUNTS
from gmail_cleanup.db import Database
from gmail_cleanup.inbox_ops import (
    fetch_inbox_unread,
    run_inbox_archive,
    run_inbox_filters,
    run_inbox_review,
)

# ---------------------------------------------------------------------------
# Cache helpers (per-account)
# ---------------------------------------------------------------------------

def _cache_path(account: str) -> Path:
    safe = account.replace("@", "_").replace(".", "_")
    return Path.home() / "gmail_cleanup" / f"inbox_cache_{safe}.json"


def _save_cache(inbox_unread: dict, account: str):
    path = _cache_path(account)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(inbox_unread))
    print(f"  Cached {len(inbox_unread)} messages to {path}")


def _load_cache(account: str) -> dict:
    path = _cache_path(account)
    if not path.exists():
        print(f"ERROR: cache not found at {path}")
        print("Run 'sync' phase first to populate the cache.")
        sys.exit(1)
    data = json.loads(path.read_text())
    print(f"  Loaded {len(data)} messages from cache ({path})")
    return data


# ---------------------------------------------------------------------------
# Phase runners
# ---------------------------------------------------------------------------

def phase_sync(service, account: str) -> dict:
    print("\n=== Phase: sync ===")
    inbox_unread = fetch_inbox_unread(service)
    _save_cache(inbox_unread, account)
    return inbox_unread


def phase_archive(service, db: Database, account: str, dry_run: bool) -> dict:
    print("\n=== Phase: archive ===")
    inbox_unread = _load_cache(account)
    result = run_inbox_archive(
        service=service,
        inbox_unread=inbox_unread,
        account=account,
        db=db,
        dry_run=dry_run,
    )
    return result


def phase_filters(service):
    print("\n=== Phase: filters ===")
    run_inbox_filters(service)


def phase_review(service, db: Database, account: str, skipped_ids: list = None):
    print("\n=== Phase: review ===")
    if skipped_ids is None:
        from gmail_cleanup.config import NOTIFICATION_SENDERS
        from gmail_cleanup.keep import load_keep_senders
        keep_senders = load_keep_senders()
        inbox_unread = _load_cache(account)
        skipped_ids = [
            gid for gid, meta in inbox_unread.items()
            if meta["sender"] not in NOTIFICATION_SENDERS
            and meta["sender"] not in keep_senders
        ]
        print(f"  (Using cache — {len(skipped_ids)} messages with unknown senders)")
    run_inbox_review(service=service, skipped_ids=skipped_ids, db=db)


def _print_unsub_table(unsub_map: dict):
    mailto_count = sum(1 for v in unsub_map.values() if v["mailto"])
    http_count = sum(1 for v in unsub_map.values() if v["https"])
    print(f"\nFound {len(unsub_map)} distinct senders:")
    print(f"  mailto: mechanism available: {mailto_count}")
    print(f"  https:  mechanism available: {http_count}")
    print(f"\n{'Sender':<45} {'Count':>6}  {'Mechanism'}")
    print("-" * 70)
    for sender, info in sorted(unsub_map.items(), key=lambda x: -x[1]["count"]):
        mechanism = []
        if info["mailto"]:
            mechanism.append("mailto")
        if info["https"]:
            mechanism.append("https")
        print(f"{sender:<45} {info['count']:>6}  {', '.join(mechanism) or '—'}")


def phase_unsubscribe(service, account: str):
    """Build unsubscribe candidates and write them to the pending file.

    NEVER sends anything — approval gate. Use the `unsubscribe-send` phase
    to actually send after reviewing the pending file.
    """
    print("\n=== Phase: unsubscribe (build candidates — nothing is sent) ===")
    from gmail_cleanup.unsubscribe_pass import (
        fetch_marketing_unsubscribes,
        write_pending,
    )

    print("Fetching Archive/Marketing messages for unsubscribe headers...")
    unsub_map = fetch_marketing_unsubscribes(service)

    if not unsub_map:
        print("No senders with unsubscribe headers found.")
        return

    _print_unsub_table(unsub_map)
    write_pending(unsub_map, account)
    print("\nTo send these after review:")
    print(f"  PYTHONPATH=src .venv/bin/python -m gmail_cleanup.inbox_reduce unsubscribe-send --account {account}")


def phase_unsubscribe_send(service, account: str, assume_yes: bool = False):
    """Send previously approved unsubscribe candidates from the pending file."""
    print("\n=== Phase: unsubscribe-send ===")
    from gmail_cleanup.keep import load_keep_senders
    from gmail_cleanup.unsubscribe_pass import (
        clear_pending,
        load_pending,
        record_unsubscribed,
        run_unsubscribe_http,
        run_unsubscribe_mailto,
    )

    pending = load_pending(account)
    unsub_map = pending.get("candidates", {})
    if not unsub_map:
        print(f"No pending unsubscribe candidates for {account}.")
        print("Run the 'unsubscribe' phase first to build the candidate list.")
        return

    # Re-filter against the CURRENT keep-list — protects senders added to the
    # keep file after the pending list was generated.
    keep_senders = load_keep_senders()
    protected = sorted(s for s in unsub_map if s in keep_senders)
    if protected:
        print(f"Keep-list: dropping {len(protected)} protected sender(s):")
        for s in protected:
            print(f"  - {s}")
        unsub_map = {s: v for s, v in unsub_map.items() if s not in keep_senders}
    if not unsub_map:
        print("Nothing left to send after keep-list filtering.")
        return

    print(f"Pending list generated at: {pending.get('generated_at', 'unknown')}")
    _print_unsub_table(unsub_map)

    if not assume_yes:
        try:
            answer = input(f"\nSend unsubscribe requests to {len(unsub_map)} senders? [y/N] ")
        except EOFError:
            # Non-interactive context (cron, piped stdin) — never send without a human
            answer = "n"
        if answer.strip().lower() != "y":
            print("Aborted — nothing sent. Pending file left in place.")
            return

    mailto_results = run_unsubscribe_mailto(service, unsub_map, dry_run=False)
    http_results = run_unsubscribe_http(unsub_map, dry_run=False)

    results_path = Path.home() / "gmail_cleanup" / "unsubscribe_results.json"
    results_path.write_text(json.dumps({
        "mailto": mailto_results,
        "http": http_results,
    }, indent=2))
    print(f"\nResults written to {results_path}")
    record_unsubscribed(
        set(mailto_results.get("sent", [])) | set(http_results.get("success", []))
    )
    clear_pending(account)

    print("\n=== Unsubscribe Report ===")
    print(f"  mailto sent:       {len(mailto_results.get('sent', []))}")
    print(f"  mailto failed:     {len(mailto_results.get('failed', []))}")
    print(f"  http success:      {len(http_results.get('success', []))}")
    print(f"  http needs manual: {len(http_results.get('needs_manual', []))}")
    print(f"  http failed:       {len(http_results.get('failed', []))}")
    if http_results.get("needs_manual"):
        print("\nManual unsubscribe needed:")
        for sender in http_results["needs_manual"]:
            url = unsub_map[sender]["https"]
            print(f"  {sender}: {url}")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _run_phase_for_account(args, service, db: Database, account: str):
    """Run the selected phase for a single account."""
    if args.phase == "sync":
        phase_sync(service, account)

    elif args.phase == "archive":
        result = phase_archive(service, db, account, dry_run=args.dry_run)
        if not args.dry_run and result["archived"] > 0:
            inbox_unread = _load_cache(account)
            remaining_ids = (
                set(result["skipped_unknown"])
                | set(result["skipped_flagged"])
                | set(result["kept_list"])
                | set(result["kept_engaged"])
            )
            remaining = {gid: meta for gid, meta in inbox_unread.items() if gid in remaining_ids}
            _save_cache(remaining, account)
            print(f"  Cache updated: {len(remaining)} messages remaining")

    elif args.phase == "filters":
        phase_filters(service)

    elif args.phase == "review":
        phase_review(service, db, account)

    elif args.phase == "unsubscribe":
        phase_unsubscribe(service, account)

    elif args.phase == "unsubscribe-send":
        phase_unsubscribe_send(service, account, assume_yes=args.yes)

    elif args.phase == "all":
        inbox_unread = phase_sync(service, account)
        result = run_inbox_archive(
            service=service,
            inbox_unread=inbox_unread,
            account=account,
            db=db,
            dry_run=args.dry_run,
        )
        phase_review(service, db, account, skipped_ids=result["skipped_unknown"])
        # Build (but never send) unsubscribe candidates for later review
        phase_unsubscribe(service, account)


def main():
    parser = argparse.ArgumentParser(
        description="Inbox Zero Helper — reduce unread inbox count",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "phase",
        choices=["sync", "archive", "filters", "review", "unsubscribe", "unsubscribe-send", "all"],
        help="Phase to run",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the confirmation prompt in unsubscribe-send",
    )
    parser.add_argument(
        "--account",
        default=GMAIL_ACCOUNTS[0],
        help=f"Gmail account (default: {GMAIL_ACCOUNTS[0]})",
    )
    parser.add_argument(
        "--all-accounts",
        action="store_true",
        help="Run for all configured accounts in sequence",
    )
    # Dry-run control
    dry_run_group = parser.add_mutually_exclusive_group()
    dry_run_group.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        default=True,
        help="Preview only, no Gmail changes (default for archive)",
    )
    dry_run_group.add_argument(
        "--no-dry-run",
        dest="dry_run",
        action="store_false",
        help="Apply changes to Gmail",
    )
    args = parser.parse_args()

    if not CREDENTIALS_PATH.exists():
        print(f"ERROR: credentials.json not found at {CREDENTIALS_PATH}")
        sys.exit(1)

    if not args.all_accounts and args.account not in GMAIL_ACCOUNTS:
        print(f"ERROR: unknown account '{args.account}'")
        print(f"Configured accounts: {GMAIL_ACCOUNTS}")
        sys.exit(1)

    accounts = GMAIL_ACCOUNTS if args.all_accounts else [args.account]
    db = Database(DB_PATH)

    for account in accounts:
        if args.all_accounts:
            print(f"\n{'='*60}\n  Account: {account}\n{'='*60}")
        service = get_service(account)
        _run_phase_for_account(args, service, db, account)

    print("\nDone.")


if __name__ == "__main__":
    main()
