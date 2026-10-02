"""
keep.py — User-editable keep-list and block-list of senders.

Keep-list (keep_senders.txt): senders that must always land in the inbox.
Wins over every other mechanism: config routing, marketing/header detection,
engagement thresholds, filter creation, and the unsubscribe pass.

Block-list (blocked_senders.txt): senders/domains whose mail goes straight to
trash. Entries are full addresses or bare domains (a domain also matches its
subdomains). The keep-list wins over the block-list if both match.
"""
from pathlib import Path

from gmail_cleanup.config import BASE_DIR

KEEP_SENDERS_PATH = BASE_DIR / "keep_senders.txt"
BLOCKED_SENDERS_PATH = BASE_DIR / "blocked_senders.txt"


def _load_list(path: Path, label: str) -> frozenset:
    if not path.exists():
        print(f"  ({label} not found at {path} — empty)")
        return frozenset()

    entries = set()
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip().lower()
        if line:
            entries.add(line)
    return frozenset(entries)


def load_keep_senders(path: Path = KEEP_SENDERS_PATH) -> frozenset:
    """Load the keep-list: one email address per line, '#' starts a comment.

    Addresses are lowercased to match extract_sender() normalization.
    A missing file is not an error — returns an empty set.
    """
    return _load_list(path, "keep-list")


def load_blocked_senders(path: Path = BLOCKED_SENDERS_PATH) -> frozenset:
    """Load the block-list: full addresses or bare domains, '#' comments."""
    return _load_list(path, "block-list")


def is_blocked(sender: str, blocked: frozenset) -> bool:
    """True if sender matches the block-list by address, domain, or subdomain."""
    sender = (sender or "").lower()
    if sender in blocked:
        return True
    domain = sender.rsplit("@", 1)[-1]
    return any(
        domain == entry or domain.endswith("." + entry)
        for entry in blocked
        if "@" not in entry
    )
