import argparse
import sys

from gmail_cleanup.config import CREDENTIALS_PATH, DB_PATH, GMAIL_ACCOUNTS
from gmail_cleanup.db import Database


def main():
    parser = argparse.ArgumentParser(description="Gmail Cleanup Tool")
    parser.add_argument(
        "phase",
        choices=["fetch", "classify", "review", "archive", "cleanup", "all"],
        help="Phase to run"
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Classify without writing decisions to DB")
    parser.add_argument("--auto", action="store_true",
                        help="Auto-delete obvious spam; only prompt for ambiguous senders")
    parser.add_argument(
        "--accounts", nargs="+", default=GMAIL_ACCOUNTS,
        help="Gmail accounts to process (default: all configured accounts)"
    )
    args = parser.parse_args()

    if not CREDENTIALS_PATH.exists():
        print(f"ERROR: credentials.json not found at {CREDENTIALS_PATH}")
        print("Download OAuth credentials from Google Cloud Console and place at that path.")
        sys.exit(1)

    db = Database(DB_PATH)

    if args.phase in ("fetch", "all"):
        from gmail_cleanup.auth import get_service
        from gmail_cleanup.fetch import fetch_all_metadata
        print("=== Phase 2: Fetch & Index ===")
        for account in args.accounts:
            print(f"\n--- {account} ---")
            service = get_service(account)
            fetch_all_metadata(service, db, account)

    if args.phase in ("classify", "all"):
        from gmail_cleanup.phase3_classify import run_classification
        print("\n=== Phase 3: Classify ===")
        run_classification(db, dry_run=args.dry_run)

    if args.phase in ("review", "all"):
        from gmail_cleanup.phase4_review import run_review
        print("\n=== Phase 4: Review Flagged Emails ===")
        run_review(db, auto=args.auto)

    if args.phase in ("archive", "all"):
        from gmail_cleanup.archive import run_archive
        from gmail_cleanup.auth import get_service
        print("\n=== Phase 5: Archive ===")
        for account in args.accounts:
            print(f"\n--- {account} ---")
            service = get_service(account)
            run_archive(service, db, account)

    if args.phase in ("cleanup", "all"):
        from gmail_cleanup.auth import get_service
        from gmail_cleanup.gmail_ops import run_apply_labels, run_create_filters, run_delete_junk
        from gmail_cleanup.unsubscribe import run_unsubscribe

        print("\n=== Phase 6a: Apply Gmail Labels ===")
        for account in args.accounts:
            print(f"\n--- {account} ---")
            service = get_service(account)
            run_apply_labels(service, db, account)

        print("\n=== Phase 6b: Delete Junk ===")
        total_delete = (
            len([e for e in db.get_by_classification("DELETE")]) +
            len([e for e in db.get_by_classification("UNSUBSCRIBE")])
        )
        confirm = input(f"About to move {total_delete} emails to Trash across all accounts. Type 'yes' to confirm: ")
        if confirm.lower() == "yes":
            for account in args.accounts:
                print(f"\n--- {account} ---")
                service = get_service(account)
                run_delete_junk(service, db, account)
        else:
            print("Deletion skipped.")

        print("\n=== Phase 6c: Unsubscribe from Newsletters ===")
        for account in args.accounts:
            print(f"\n--- {account} ---")
            run_unsubscribe(db, account)

        print("\n=== Phase 6d: Create Filters ===")
        for account in args.accounts:
            print(f"\n--- {account} ---")
            service = get_service(account)
            run_create_filters(service, db, account)

    print("\nDone.")


if __name__ == "__main__":
    main()
