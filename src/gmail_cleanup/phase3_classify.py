import time
from collections import defaultdict

from gmail_cleanup.classify import classify_tier
from gmail_cleanup.db import Database
from gmail_cleanup.keep import load_keep_senders
from gmail_cleanup.llm import classify_with_llm


def run_classification(db: Database, dry_run: bool = False):
    """Run full classification pass on all unclassified emails."""
    print("Computing sender statistics...")
    db.compute_sender_stats()
    keep_senders = load_keep_senders()

    emails = db.get_all_unclassified()
    print(f"Classifying {len(emails)} emails...")

    # Pre-group subjects by sender for LLM batch efficiency
    sender_subjects = defaultdict(list)
    for email in emails:
        sender_subjects[email["sender"]].append(email["subject"])

    # Cache LLM decisions per sender (one API call per unique sender, not per email)
    llm_decisions = {}
    stats = defaultdict(int)

    for i, email in enumerate(emails):
        sender_row = db.get_sender(email["sender"])
        sender_dict = dict(sender_row) if sender_row else {}

        action, category, decided_by = classify_tier(dict(email), sender_dict, keep_senders)

        if action == "LLM":
            sender = email["sender"]
            if sender not in llm_decisions:
                read_rate = sender_dict.get("read_rate", 0.0)
                has_replied = (sender_dict.get("reply_count") or 0) > 0
                subjects = sender_subjects[sender]
                llm_action, llm_cat, reason = classify_with_llm(
                    sender, subjects, read_rate, has_replied
                )
                llm_decisions[sender] = (llm_action, llm_cat, f"llm:{reason[:50]}")
                time.sleep(0.2)

            action, category, decided_by = llm_decisions[sender]

        stats[action] += 1
        if not dry_run:
            db.set_decision(email["gmail_id"], action=action,
                            category=category, decided_by=decided_by)

        if (i + 1) % 10000 == 0:
            print(f"  {i+1}/{len(emails)} | " +
                  " | ".join(f"{k}:{v}" for k, v in sorted(stats.items())))

    print("\nClassification complete:")
    for action, count in sorted(stats.items()):
        print(f"  {action}: {count}")
    return dict(stats)
