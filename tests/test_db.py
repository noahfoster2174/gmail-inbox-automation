import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from gmail_cleanup.db import Database


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "test.db")

def test_creates_schema(db):
    conn = sqlite3.connect(db.path)
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    assert {"emails", "senders", "decisions"} == tables

def test_upsert_email(db):
    db.upsert_email({
        "gmail_id": "abc123", "thread_id": "thread1",
        "sender": "test@example.com", "sender_domain": "example.com",
        "subject": "Hello", "date": "2024-01-01", "is_read": True,
        "has_attachments": False, "body_preview": "Hi there",
        "list_unsubscribe": None, "precedence": None,
        "account": "user@example.com",
    })
    row = db.get_email("abc123")
    assert row["subject"] == "Hello"
    assert row["is_read"] == 1

def test_upsert_email_is_idempotent(db):
    email = {
        "gmail_id": "abc", "thread_id": "t1", "sender": "a@b.com",
        "sender_domain": "b.com", "subject": "S", "date": "2024-01-01",
        "is_read": True, "has_attachments": False, "body_preview": "",
        "list_unsubscribe": None, "precedence": None,
        "account": "user@example.com",
    }
    db.upsert_email(email)
    db.upsert_email(email)
    assert db.count_emails() == 1

def test_compute_sender_stats(db):
    db.upsert_email({
        "gmail_id": "1", "thread_id": "t1", "sender": "a@b.com",
        "sender_domain": "b.com", "subject": "S", "date": "2024-01-01",
        "is_read": True, "has_attachments": False, "body_preview": "",
        "list_unsubscribe": None, "precedence": None,
        "account": "user@example.com",
    })
    db.compute_sender_stats()
    sender = db.get_sender("a@b.com")
    assert sender["total_count"] == 1
    assert sender["read_count"] == 1
    assert abs(sender["read_rate"] - 1.0) < 0.001

def test_set_decision(db):
    db.upsert_email({
        "gmail_id": "1", "thread_id": "t1", "sender": "a@b.com",
        "sender_domain": "b.com", "subject": "S", "date": "2024-01-01",
        "is_read": True, "has_attachments": False, "body_preview": "",
        "list_unsubscribe": None, "precedence": None,
        "account": "user@example.com",
    })
    db.set_decision("1", action="KEEP", category="Personal", decided_by="rule")
    row = db.get_email("1")
    assert row["classification"] == "KEEP"
    assert row["category"] == "Personal"

def test_get_by_classification(db):
    db.upsert_email({
        "gmail_id": "2", "thread_id": "t2", "sender": "b@c.com",
        "sender_domain": "c.com", "subject": "S2", "date": "2024-01-02",
        "is_read": False, "has_attachments": False, "body_preview": "",
        "list_unsubscribe": None, "precedence": None,
        "account": "user2@example.com",
    })
    db.set_decision("2", action="DELETE", category=None, decided_by="rule")
    results = db.get_by_classification("DELETE")
    assert len(results) == 1
    assert results[0]["gmail_id"] == "2"
