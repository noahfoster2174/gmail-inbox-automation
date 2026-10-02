import json
import re

import anthropic

from gmail_cleanup.config import ARCHIVE_CATEGORIES, NEWSLETTER_CATEGORIES

VALID_ACTIONS = {"KEEP", "DELETE", "FLAG", "UNSUBSCRIBE"}
ALL_CATEGORIES = ARCHIVE_CATEGORIES + [f"Newsletters/{c}" for c in NEWSLETTER_CATEGORIES]

_client = None


def _get_client():
    global _client
    if _client is None:
        _client = anthropic.Anthropic()
    return _client


def build_classification_prompt(sender: str, subjects: list,
                                 read_rate: float, has_replied: bool) -> str:
    subjects_text = "\n".join(f"  - {s}" for s in subjects[:5])
    categories_text = "\n".join(f"  - {c}" for c in ALL_CATEGORIES)
    return f"""You are classifying emails for a Gmail inbox cleanup.

Sender: {sender}
Recent subjects:
{subjects_text}
User read rate for this sender: {read_rate:.2f} (0=never, 1=always)
User has replied to this sender: {has_replied}

Available categories if KEEP:
{categories_text}

Respond with JSON only:
{{"action": "KEEP|DELETE|FLAG|UNSUBSCRIBE", "category": "category or null", "reason": "one sentence"}}

Rules:
- KEEP: personal, financial, professional, or genuinely interesting content
- DELETE: promotional noise, transactional spam
- UNSUBSCRIBE: mailing list the user should unsubscribe from
- FLAG: uncertain, possibly legal/medical/financial importance
- Err toward KEEP or FLAG when uncertain
- category required if KEEP, null otherwise"""


def parse_llm_response(response_text: str) -> tuple[str, str | None, str]:
    try:
        match = re.search(r'\{.*\}', response_text, re.DOTALL)
        if not match:
            return ("FLAG", None, "LLM response not parseable")
        data = json.loads(match.group())
        action = data.get("action", "FLAG")
        if action not in VALID_ACTIONS:
            return ("FLAG", None, f"Invalid action: {action}")
        return (action, data.get("category"), data.get("reason", ""))
    except Exception:
        return ("FLAG", None, "LLM parse error")


def classify_with_llm(sender: str, subjects: list,
                      read_rate: float, has_replied: bool) -> tuple[str, str | None, str]:
    prompt = build_classification_prompt(sender, subjects, read_rate, has_replied)
    message = _get_client().messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}]
    )
    return parse_llm_response(message.content[0].text)
