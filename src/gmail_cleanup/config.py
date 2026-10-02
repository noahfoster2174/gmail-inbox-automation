"""
config.py — Paths, thresholds, and user configuration loading.

Personal data (accounts, sender routing table) lives in gitignored JSON files
next to the code — `accounts.json` and `senders.json` — so the source tree
carries no PII. Fresh clones fall back to the committed examples in config/
so tests and CI run without private data.
"""
import json
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]
ARCHIVE_DIR = Path.home() / "Gmail_Archive"
DB_PATH = BASE_DIR / "gmail_cleanup.db"
CREDENTIALS_PATH = BASE_DIR / "credentials.json"
_EXAMPLES_DIR = BASE_DIR / "config"


def _load_json(name: str) -> dict:
    """Load a user config file, falling back to the committed example."""
    for path in (BASE_DIR / name, _EXAMPLES_DIR / name.replace(".json", ".example.json")):
        if path.exists():
            return json.loads(path.read_text())
    return {}


_accounts = _load_json("accounts.json")
GMAIL_ACCOUNTS = _accounts.get("accounts", [])

# Token files per account: token_<local-part>.json alongside credentials.json
TOKEN_PATHS = {
    acct: BASE_DIR / f"token_{acct.split('@')[0]}.json" for acct in GMAIL_ACCOUNTS
}

GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.settings.basic",
]

ARCHIVE_CATEGORIES = [
    "Financial", "Work", "Travel", "Personal",
    "Medical", "Legal", "Running_Sports", "Misc",
]

NEWSLETTER_CATEGORIES = [
    "Finance_Markets", "Fitness_Health", "Tech",
    "News_Media", "Shopping", "Other",
]

# Sender read-rate thresholds
HIGH_TRUST_THRESHOLD = 0.70
LOW_TRUST_THRESHOLD = 0.10

# Engagement override for the weekly archive pass: a sender the user actually
# reads is never silently archived, even if config routes it out of the inbox.
READ_RATE_KEEP_THRESHOLD = 0.30
READ_RATE_MIN_EMAILS = 25  # ignore read_rate on thin samples

# Unread inbox mail older than this is archived to Archive/Cleanup by the
# weekly pass (keep-list, flag-keyword, and blocked mail excluded).
AGE_OUT_DAYS = 30

# Subjects containing these words get FLAG regardless of tier
FLAG_KEYWORDS = [
    "invoice", "agreement", "contract", "prescription",
    "claim", "settlement", "refund", "legal", "medical",
    "diagnosis", "policy", "deed", "lease", "warrant",
]

# ---------------------------------------------------------------------------
# Sender routing tiers, loaded from senders.json (see config/senders.example.json)
#   Tier 1  skip_inbox      — never hits inbox, goes straight to Archive/*
#   Tier 2  notify          — stays in inbox with the Notify label; the weekly
#                             archive pass files it (app already notified you)
#   Tier 3  time_sensitive  — hits inbox normally, archived by the weekly pass
# ---------------------------------------------------------------------------
_senders = _load_json("senders.json")
SKIP_INBOX_SENDERS = _senders.get("skip_inbox", {})
NOTIFY_SENDERS = _senders.get("notify", {})
TIME_SENSITIVE_SENDERS = _senders.get("time_sensitive", {})

NOTIFICATION_SENDERS = {**SKIP_INBOX_SENDERS, **NOTIFY_SENDERS, **TIME_SENSITIVE_SENDERS}
