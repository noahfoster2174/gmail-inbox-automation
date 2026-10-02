import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

pytest.importorskip("anthropic", reason="LLM classifier is an optional extra: pip install '.[llm]'")

from gmail_cleanup.llm import build_classification_prompt, parse_llm_response  # noqa: E402


def test_prompt_includes_sender():
    prompt = build_classification_prompt(
        sender="deals@store.com",
        subjects=["50% off today", "Flash sale!", "Last chance"],
        read_rate=0.12, has_replied=False,
    )
    assert "deals@store.com" in prompt
    assert "50% off today" in prompt

def test_parse_keep():
    action, category, _ = parse_llm_response('{"action": "KEEP", "category": "Financial", "reason": "bank"}')
    assert action == "KEEP"
    assert category == "Financial"

def test_parse_delete():
    action, category, _ = parse_llm_response('{"action": "DELETE", "category": null, "reason": "spam"}')
    assert action == "DELETE"
    assert category is None

def test_parse_invalid_json_returns_flag():
    action, _, _ = parse_llm_response("sorry I don't know")
    assert action == "FLAG"

def test_parse_invalid_action_returns_flag():
    action, _, _ = parse_llm_response('{"action": "MAYBE", "category": null, "reason": "unsure"}')
    assert action == "FLAG"
