
from gmail_cleanup.config import FLAG_KEYWORDS, HIGH_TRUST_THRESHOLD, LOW_TRUST_THRESHOLD

# Tech is checked before Finance_Markets so "techbrew" matches Tech, not Finance via "brew"
_NEWSLETTER_KEYWORDS = {
    "Fitness_Health": ["run", "running", "fitness", "health", "workout", "training",
                       "marathon", "yoga", "nutrition", "strava", "triathlon", "athlete"],
    "Tech": ["tech", "developer", "code", "software", "startup", "ai", "product",
             "verge", "wired", "hacker", "github", "engineering", "techbrew"],
    "Finance_Markets": ["market", "stock", "invest", "finance", "fund", "crypto",
                        "economy", "bloomberg", "financial", "equity", "wsj", "brew"],
    "News_Media": ["news", "daily", "weekly", "morning", "briefing", "digest",
                   "times", "post", "journal", "update", "report", "athletic", "nfl",
                   "nba", "mlb", "sports", "cap space", "roster"],
    "Shopping": ["deal", "sale", "offer", "discount", "promo", "shop", "store",
                 "order", "cart", "amazon", "walmart", "target", "coupon"],
}


def is_newsletter(email: dict) -> bool:
    if email.get("list_unsubscribe"):
        return True
    precedence = (email.get("precedence") or "").lower()
    return precedence in ("bulk", "list", "junk")


def contains_flag_keywords(subject: str) -> bool:
    subject_lower = subject.lower()
    return any(kw in subject_lower for kw in FLAG_KEYWORDS)


def categorize_newsletter(subject: str, sender: str) -> str:
    text = (subject + " " + sender).lower()
    for category, keywords in _NEWSLETTER_KEYWORDS.items():
        if any(kw in text for kw in keywords):
            return category
    return "Other"


def classify_tier(
    email: dict,
    sender: dict,
    keep_senders: frozenset = frozenset(),
) -> tuple[str, str | None, str]:
    """Returns (action, category, decided_by).
    action: KEEP | DELETE | FLAG | UNSUBSCRIBE | LLM
    """
    subject = email.get("subject", "")
    is_read = email.get("is_read", False)
    has_attachments = email.get("has_attachments", False)
    read_rate = (sender or {}).get("read_rate", 0.0)
    reply_count = (sender or {}).get("reply_count", 0)
    is_nl = is_newsletter(email)

    if (email.get("sender") or "").lower() in keep_senders:
        return ("KEEP", None, "rule:keep_list")

    if contains_flag_keywords(subject) and not is_read:
        return ("FLAG", None, "rule:flag_keyword")

    if is_read:
        if is_nl:
            category = categorize_newsletter(subject, email.get("sender", ""))
            return ("KEEP", f"Newsletters/{category}", "rule:tier1_read")
        return ("KEEP", None, "rule:tier1_read")

    if read_rate >= HIGH_TRUST_THRESHOLD:
        return ("KEEP", None, "rule:tier2_high_trust")

    if is_nl and read_rate < LOW_TRUST_THRESHOLD:
        return ("UNSUBSCRIBE", None, "rule:tier4_newsletter_unread")

    if read_rate < LOW_TRUST_THRESHOLD and not has_attachments and reply_count == 0:
        return ("DELETE", None, "rule:tier5_low_trust")

    return ("LLM", None, "rule:tier6_ambiguous")
