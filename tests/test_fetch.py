import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from gmail_cleanup.fetch import (
    extract_domain,
    extract_header,
    extract_sender,
    parse_message_metadata,
)


def test_extract_header_found():
    headers = [{"name": "From", "value": "John <john@example.com>"}]
    assert extract_header(headers, "From") == "John <john@example.com>"

def test_extract_header_missing():
    assert extract_header([{"name": "Subject", "value": "Hi"}], "From") is None

def test_extract_sender_with_display_name():
    assert extract_sender("John Doe <john@example.com>") == "john@example.com"

def test_extract_sender_bare_address():
    assert extract_sender("john@example.com") == "john@example.com"

def test_extract_domain():
    assert extract_domain("john@example.com") == "example.com"

def test_parse_message_metadata_read():
    raw = {
        "id": "msg1", "threadId": "thread1",
        "labelIds": ["INBOX"],
        "payload": {
            "headers": [
                {"name": "From", "value": "alice@test.com"},
                {"name": "Subject", "value": "Hello"},
                {"name": "Date", "value": "Mon, 1 Jan 2024 10:00:00 +0000"},
                {"name": "List-Unsubscribe", "value": "<mailto:unsub@test.com>"},
                {"name": "Precedence", "value": "bulk"},
            ],
            "parts": []
        },
        "snippet": "Hi there",
    }
    result = parse_message_metadata(raw, account="user@example.com")
    assert result["gmail_id"] == "msg1"
    assert result["is_read"]
    assert result["sender"] == "alice@test.com"
    assert result["list_unsubscribe"] == "<mailto:unsub@test.com>"
    assert result["precedence"] == "bulk"
    assert result["account"] == "user@example.com"

def test_parse_message_metadata_unread():
    raw = {
        "id": "msg2", "threadId": "thread2",
        "labelIds": ["INBOX", "UNREAD"],
        "payload": {
            "headers": [
                {"name": "From", "value": "bob@test.com"},
                {"name": "Subject", "value": "Buy now!"},
                {"name": "Date", "value": "Tue, 2 Jan 2024 10:00:00 +0000"},
            ],
            "parts": []
        },
        "snippet": "Limited offer",
    }
    result = parse_message_metadata(raw, account="user2@example.com")
    assert not result["is_read"]
    assert result["list_unsubscribe"] is None
    assert result["account"] == "user2@example.com"

def test_has_attachments_detected():
    raw = {
        "id": "msg3", "threadId": "t3", "labelIds": [],
        "payload": {
            "headers": [
                {"name": "From", "value": "a@b.com"},
                {"name": "Subject", "value": "See attached"},
                {"name": "Date", "value": "2024-01-01"},
            ],
            "parts": [{"filename": "doc.pdf", "mimeType": "application/pdf"}]
        },
        "snippet": ""
    }
    result = parse_message_metadata(raw, account="user@example.com")
    assert result["has_attachments"]
