import re
from collections import defaultdict

from gmail_cleanup.db import Database

# Domain substrings that reliably indicate spam
_SPAM_DOMAIN_KEYWORDS = {
    "kinky", "maid", "casino", "adult", "xxx", "porn", "sex",
    "drug", "pill", "pharma", "lottery", "prize", "winner",
}

# Subject-line regex patterns that reliably indicate spam
_SPAM_SUBJECT_PATTERNS = [re.compile(p, re.I) for p in [
    r"please.*respond",
    r"trying.to.reach",
    r"we.have.been.trying",
    r"lottery|prize|winner|claim.your",
    r"bitcoin|crypto.*profit|investment.*return",
    r"urgent.*reply|reply.*urgent",
    r"\.GSO\b",
    # add personalized-bait patterns for your own name here, e.g. r"yourname.*please"
]]


def _is_obvious_spam(sender: str, emails: list) -> bool:
    """Return True when a 0%-read-rate sender looks unambiguously like spam."""
    if "@" in sender:
        local, domain = sender.rsplit("@", 1)
        domain = domain.lower()
        local = local.lower()
    else:
        local, domain = sender.lower(), ""

    # Domain contains a known spam keyword
    if any(kw in domain for kw in _SPAM_DOMAIN_KEYWORDS):
        return True

    # Local part looks randomly generated (>10 chars, <20 % vowels)
    if len(local) > 10:
        vowel_ratio = sum(1 for c in local if c in "aeiou") / len(local)
        if vowel_ratio < 0.20:
            return True

    # Subject line matches a spam pattern
    for email in emails:
        subject = email.get("subject") or ""
        if any(p.search(subject) for p in _SPAM_SUBJECT_PATTERNS):
            return True

    return False


def run_review(db: Database, auto: bool = False):
    """Interactive CLI to review flagged emails before any destructive actions."""
    flagged = db.get_flagged()

    if not flagged:
        print("No flagged emails to review.")
        return

    print(f"\n{'='*60}")
    print(f"REVIEW REQUIRED: {len(flagged)} flagged emails")
    print(f"{'='*60}")

    # Auto-keep any flagged emails from senders with read history
    auto_kept = 0
    remaining = []
    for email in flagged:
        sender_row = db.get_sender(email["sender"])
        read_count = (sender_row["read_count"] or 0) if sender_row else 0
        if read_count > 0:
            db.set_decision(email["gmail_id"], "KEEP",
                            email["category"] or "Misc", "user:auto_keep_read_history")
            auto_kept += 1
        else:
            remaining.append(dict(email))

    if auto_kept:
        print(f"Auto-kept {auto_kept} emails from senders you've read before.")

    # Auto-delete obvious spam when --auto is set
    auto_deleted = 0
    if auto:
        still_remaining = []
        by_sender_tmp = defaultdict(list)
        for email in remaining:
            by_sender_tmp[email["sender"]].append(email)

        for sender, sender_emails in by_sender_tmp.items():
            if _is_obvious_spam(sender, sender_emails):
                for email in sender_emails:
                    db.set_decision(email["gmail_id"], "DELETE", None, "auto:spam_heuristic")
                auto_deleted += len(sender_emails)
            else:
                still_remaining.extend(sender_emails)
        remaining = still_remaining

    if auto_deleted:
        print(f"Auto-deleted {auto_deleted} emails flagged as obvious spam.")

    # Group remaining by sender
    by_sender = defaultdict(list)
    for email in remaining:
        by_sender[email["sender"]].append(email)

    sender_list = sorted(by_sender.keys())
    print(f"{len(remaining)} emails from {len(sender_list)} senders need manual review.")
    print("Enter: k (keep) / d (delete) / s (skip)\n")

    for i, sender in enumerate(sender_list):
        emails = by_sender[sender]
        sender_row = db.get_sender(sender)
        read_rate = (sender_row["read_rate"] or 0.0) if sender_row else 0.0
        total = (sender_row["total_count"] or 0) if sender_row else 0
        read_count = (sender_row["read_count"] or 0) if sender_row else 0

        print(f"\n[{i+1}/{len(sender_list)}] {sender}")
        print(f"  Read rate: {read_rate:.0%}  ({read_count}/{total} emails read)")
        print(f"  {len(emails)} flagged email(s):")
        for email in emails[:5]:
            print(f"    • [{str(email['date'])[:10]}] {email['subject']}")
            if email.get("body_preview"):
                print(f"      {str(email['body_preview'])[:100]}")
        if len(emails) > 5:
            print(f"    ... and {len(emails) - 5} more")

        while True:
            choice = input("  → [k]eep / [d]elete / [s]kip: ").strip().lower()
            if choice == "k":
                category = input("  Category (default: Misc): ").strip() or "Misc"
                for email in emails:
                    db.set_decision(email["gmail_id"], "KEEP", category, "user:review")
                break
            elif choice == "d":
                for email in emails:
                    db.set_decision(email["gmail_id"], "DELETE", None, "user:review")
                break
            elif choice == "s":
                print("  Skipped.")
                break
            else:
                print("  Enter k, d, or s.")

    still_flagged = db.get_flagged()
    if still_flagged:
        print(f"\n{len(still_flagged)} emails still flagged (skipped senders).")
        auto_keep = input("Auto-keep remaining as 'Misc'? [y/N]: ").strip().lower()
        if auto_keep == "y":
            for email in still_flagged:
                db.set_decision(email["gmail_id"], "KEEP", "Misc", "user:auto_keep")
            print("All remaining flagged emails set to KEEP/Misc.")
    else:
        print("\nAll flagged emails reviewed.")
