import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).parent))

import gmail_cleanup.inbox_ops as inbox_ops
from fake_gmail import FakeGmailService
from gmail_cleanup.inbox_ops import run_inbox_filters


def test_keep_listed_sender_gets_no_skip_inbox_filter(monkeypatch):
    monkeypatch.setattr(inbox_ops.time, "sleep", lambda s: None)
    monkeypatch.setattr(inbox_ops, "SKIP_INBOX_SENDERS", {
        "keepme@example.com": "Archive/X",
        "bye@example.com": "Archive/Y",
    })
    monkeypatch.setattr(inbox_ops, "NOTIFY_SENDERS", {"notify@example.com": "Archive/Z"})

    service = FakeGmailService()
    run_inbox_filters(service, keep_senders=frozenset({"keepme@example.com"}),
                      blocked_senders=frozenset())

    skip_filters = [
        f for f in service.created_filters
        if "INBOX" in f["action"].get("removeLabelIds", [])
    ]
    skip_froms = {f["criteria"]["from"] for f in skip_filters}
    assert skip_froms == {"bye@example.com"}

    # Notify senders still get their label filter (keeps mail in inbox)
    notify_filters = [
        f for f in service.created_filters
        if f["action"].get("addLabelIds") and "removeLabelIds" not in f["action"]
    ]
    assert {f["criteria"]["from"] for f in notify_filters} == {"notify@example.com"}


def test_existing_filters_are_purged_first(monkeypatch):
    monkeypatch.setattr(inbox_ops.time, "sleep", lambda s: None)
    monkeypatch.setattr(inbox_ops, "SKIP_INBOX_SENDERS", {})
    monkeypatch.setattr(inbox_ops, "NOTIFY_SENDERS", {})

    service = FakeGmailService(existing_filters=[{"id": "OLD1"}, {"id": "OLD2"}])
    run_inbox_filters(service, keep_senders=frozenset(), blocked_senders=frozenset())
    assert service.deleted_filters == ["OLD1", "OLD2"]


def test_block_list_creates_combined_trash_filter(monkeypatch):
    monkeypatch.setattr(inbox_ops.time, "sleep", lambda s: None)
    monkeypatch.setattr(inbox_ops, "SKIP_INBOX_SENDERS", {})
    monkeypatch.setattr(inbox_ops, "NOTIFY_SENDERS", {})

    service = FakeGmailService()
    run_inbox_filters(service, keep_senders=frozenset({"protected@x.com"}),
                      blocked_senders=frozenset({"dailygopnews.com", "protected@x.com"}))

    block_filters = [
        f for f in service.created_filters
        if "TRASH" in f["action"].get("addLabelIds", [])
    ]
    assert len(block_filters) == 1
    assert block_filters[0]["criteria"]["from"] == "dailygopnews.com"  # keep-listed excluded
