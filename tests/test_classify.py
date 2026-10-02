import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from gmail_cleanup.classify import (
    categorize_newsletter,
    classify_tier,
    contains_flag_keywords,
    is_newsletter,
)


def test_is_newsletter_by_list_unsubscribe():
    assert is_newsletter({"list_unsubscribe": "<mailto:u@x.com>", "precedence": None})

def test_is_newsletter_by_precedence_bulk():
    assert is_newsletter({"list_unsubscribe": None, "precedence": "bulk"})

def test_not_newsletter():
    assert not is_newsletter({"list_unsubscribe": None, "precedence": None})

def test_flag_keyword_invoice():
    assert contains_flag_keywords("Invoice #1234 for services")

def test_no_flag_keyword():
    assert not contains_flag_keywords("Happy birthday!")

def make_email(is_read=False, list_unsubscribe=None, precedence=None,
               has_attachments=False, subject="Test"):
    return {
        "gmail_id": "test", "is_read": is_read,
        "list_unsubscribe": list_unsubscribe, "precedence": precedence,
        "has_attachments": has_attachments, "subject": subject,
        "sender": "test@example.com",
    }

def make_sender(read_rate=0.5, is_newsletter=False, reply_count=0):
    return {"read_rate": read_rate, "is_newsletter": is_newsletter, "reply_count": reply_count}

def test_tier1_read_is_keep():
    action, _, decided_by = classify_tier(make_email(is_read=True), make_sender(0.3))
    assert action == "KEEP"
    assert "tier1" in decided_by

def test_tier2_high_trust_sender():
    action, _, decided_by = classify_tier(make_email(is_read=False), make_sender(0.85))
    assert action == "KEEP"
    assert "tier2" in decided_by

def test_tier4_newsletter_unread():
    email = make_email(is_read=False, list_unsubscribe="<mailto:u@x.com>")
    action, _, decided_by = classify_tier(email, make_sender(0.05, is_newsletter=True))
    assert action == "UNSUBSCRIBE"

def test_tier5_low_trust_no_attachment():
    action, _, _ = classify_tier(make_email(is_read=False), make_sender(0.05))
    assert action == "DELETE"

def test_tier5_with_attachment_goes_to_llm():
    action, _, _ = classify_tier(make_email(is_read=False, has_attachments=True), make_sender(0.05))
    assert action == "LLM"

def test_flag_keyword_overrides_delete():
    email = make_email(is_read=False, subject="Your invoice is ready")
    action, _, decided_by = classify_tier(email, make_sender(0.03))
    assert action == "FLAG"

def test_ambiguous_sender_goes_to_llm():
    action, _, decided_by = classify_tier(make_email(is_read=False), make_sender(0.35))
    assert action == "LLM"

def test_read_newsletter_gets_categorized():
    email = make_email(is_read=True, list_unsubscribe="<mailto:u@x.com>", subject="Runner's World Weekly")
    email["sender"] = "news@runnersworld.com"
    action, category, _ = classify_tier(email, make_sender(0.8))
    assert action == "KEEP"
    assert category is not None
    assert "Newsletters" in category

def test_categorize_newsletter_fitness():
    assert categorize_newsletter("Runner's World Weekly", "news@runnersworld.com") == "Fitness_Health"

def test_categorize_newsletter_finance():
    assert categorize_newsletter("Morning Brew Markets", "brew@morningbrew.com") == "Finance_Markets"

def test_categorize_newsletter_tech():
    assert categorize_newsletter("Tech Brew daily digest", "techbrew@morningbrew.com") == "Tech"

def test_categorize_newsletter_sports():
    assert categorize_newsletter("How KC just saved cap space", "newsletter@theathletic.com") == "News_Media"
