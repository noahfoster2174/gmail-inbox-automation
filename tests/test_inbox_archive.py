import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).parent))

import pytest

import gmail_cleanup.inbox_ops as inbox_ops
from fake_gmail import FakeGmailService
from gmail_cleanup.db import Database
from gmail_cleanup.inbox_ops import fetch_inbox_unread, plan_inbox_archive, run_inbox_archive


def _raw(mid, sender, subject="hi"):
    return {"id": mid, "internalDate": "1700000000000", "payload": {"headers": [
        {"name": "From", "value": sender}, {"name": "Subject", "value": subject}]}}


def test_fetch_retries_per_item_batch_failures(monkeypatch):
    # Regression: items failing inside a batch (rate limits) were silently
    # dropped — 45% of the inbox vanished from the decision set.
    monkeypatch.setattr(inbox_ops.time, "sleep", lambda s: None)
    meta = {f"m{i}": _raw(f"m{i}", f"s{i}@example.com") for i in range(6)}
    service = FakeGmailService(message_meta=meta, fail_once={"m1", "m3", "m4"})

    result = fetch_inbox_unread(service)

    assert set(result) == set(meta)  # every listed message recovered
    assert result["m3"]["sender"] == "s3@example.com"

# Routing table is injected per-test so the suite never depends on the
# user's private senders.json (CI runs with only the example config).
KNOWN_SENDER = "receipts@example-bank.com"
KNOWN_LABEL = "Archive/Financial"


@pytest.fixture(autouse=True)
def _hermetic_routing(monkeypatch):
    monkeypatch.setattr(inbox_ops, "NOTIFICATION_SENDERS", {KNOWN_SENDER: KNOWN_LABEL})


def msg(sender, subject="hello", unsub=None, precedence=None):
    return {
        "sender": sender,
        "subject": subject,
        "list_unsubscribe": unsub,
        "precedence": precedence,
    }


# ---------------------------------------------------------------------------
# Pure planning tests — no Gmail service, no DB
# ---------------------------------------------------------------------------

def test_known_sender_routes_to_configured_label():
    plan = plan_inbox_archive({"m1": msg(KNOWN_SENDER)}, frozenset(), {})
    assert plan["to_archive"] == {KNOWN_LABEL: ["m1"]}


def test_flag_keyword_skips_even_known_sender():
    plan = plan_inbox_archive(
        {"m1": msg(KNOWN_SENDER, subject="Your invoice is ready")},
        frozenset(), {},
    )
    assert plan["skipped_flagged"] == ["m1"]
    assert plan["to_archive"] == {}


def test_unknown_sender_with_unsubscribe_header_goes_to_marketing():
    plan = plan_inbox_archive(
        {"m1": msg("stranger@example.com", unsub="<mailto:u@x.com>")},
        frozenset(), {},
    )
    assert plan["to_archive"] == {"Archive/Marketing": ["m1"]}


def test_unknown_sender_without_headers_is_skipped():
    plan = plan_inbox_archive({"m1": msg("stranger@example.com")}, frozenset(), {})
    assert plan["skipped_unknown"] == ["m1"]
    assert plan["to_archive"] == {}


def test_keep_list_beats_configured_routing():
    plan = plan_inbox_archive(
        {"m1": msg(KNOWN_SENDER)},
        frozenset({KNOWN_SENDER}), {},
    )
    assert plan["kept_list"] == ["m1"]
    assert plan["to_archive"] == {}


def test_keep_list_beats_newsletter_branch():
    # The techbrew@ regression: keep-listed sender NOT in config but with
    # bulk headers must not be swept to Archive/Marketing.
    plan = plan_inbox_archive(
        {"m1": msg("techbrew@morningbrew.com", unsub="<https://unsub>", precedence="bulk")},
        frozenset({"techbrew@morningbrew.com"}), {},
    )
    assert plan["kept_list"] == ["m1"]
    assert plan["to_archive"] == {}


def test_keep_list_beats_flag_keywords():
    plan = plan_inbox_archive(
        {"m1": msg(KNOWN_SENDER, subject="invoice enclosed")},
        frozenset({KNOWN_SENDER}), {},
    )
    assert plan["kept_list"] == ["m1"]
    assert plan["skipped_flagged"] == []


def test_notify_tier_bypasses_engagement_override():
    # Receipts/records (NOTIFICATION_SENDERS) are archived even when engaged:
    # the app already notified you, the email is just the record.
    stats = {KNOWN_SENDER: {"total_count": 100, "read_rate": 0.40}}
    plan = plan_inbox_archive({"m1": msg(KNOWN_SENDER)}, frozenset(), stats)
    assert plan["to_archive"] == {KNOWN_LABEL: ["m1"]}
    assert plan["kept_engaged"] == []


def test_engaged_sender_is_kept_despite_newsletter_headers():
    stats = {"nl@example.com": {"total_count": 100, "read_rate": 0.55}}
    plan = plan_inbox_archive(
        {"m1": msg("nl@example.com", unsub="<mailto:u@x.com>")},
        frozenset(), stats,
    )
    assert plan["kept_engaged"] == ["m1"]
    assert plan["to_archive"] == {}


def test_low_read_rate_sender_is_archived():
    stats = {KNOWN_SENDER: {"total_count": 100, "read_rate": 0.05}}
    plan = plan_inbox_archive({"m1": msg(KNOWN_SENDER)}, frozenset(), stats)
    assert plan["to_archive"] == {KNOWN_LABEL: ["m1"]}
    assert plan["kept_engaged"] == []


def test_thin_sample_does_not_trigger_engagement_override():
    stats = {KNOWN_SENDER: {"total_count": 5, "read_rate": 0.90}}
    plan = plan_inbox_archive({"m1": msg(KNOWN_SENDER)}, frozenset(), stats)
    assert plan["to_archive"] == {KNOWN_LABEL: ["m1"]}


def test_blocked_sender_routes_to_trash():
    plan = plan_inbox_archive(
        {"m1": msg("updates@email.dailygopnews.com", subject="invoice for freedom",
                   unsub="<mailto:u@x.com>")},
        frozenset(), {}, frozenset({"dailygopnews.com"}),
    )
    # blocked beats flag keywords AND the newsletter branch
    assert plan["to_trash"] == ["m1"]
    assert plan["skipped_flagged"] == []
    assert plan["to_archive"] == {}


NOW_MS = 1_800_000_000_000
DAY_MS = 86_400_000


def test_old_unread_is_aged_out_to_cleanup():
    m = msg("stranger@example.com")
    m["ts"] = NOW_MS - 45 * DAY_MS
    plan = plan_inbox_archive({"m1": m}, frozenset(), {}, frozenset(), now_ms=NOW_MS)
    assert plan["aged_out"] == ["m1"]
    assert plan["to_archive"] == {"Archive/Cleanup": ["m1"]}


def test_fresh_unread_is_not_aged_out():
    m = msg("stranger@example.com")
    m["ts"] = NOW_MS - 5 * DAY_MS
    plan = plan_inbox_archive({"m1": m}, frozenset(), {}, frozenset(), now_ms=NOW_MS)
    assert plan["aged_out"] == []
    assert plan["skipped_unknown"] == ["m1"]


def test_age_out_beats_engagement_override():
    m = msg("nl@example.com", unsub="<mailto:u@x.com>")
    m["ts"] = NOW_MS - 45 * DAY_MS
    stats = {"nl@example.com": {"total_count": 100, "read_rate": 0.55}}
    plan = plan_inbox_archive({"m1": m}, frozenset(), stats, frozenset(), now_ms=NOW_MS)
    assert plan["aged_out"] == ["m1"]


def test_keep_and_flag_beat_age_out():
    old = NOW_MS - 45 * DAY_MS
    kept = msg("crew@morningbrew.com")
    kept["ts"] = old
    flagged = msg("x@example.com", subject="your invoice")
    flagged["ts"] = old
    plan = plan_inbox_archive(
        {"m1": kept, "m2": flagged},
        frozenset({"crew@morningbrew.com"}), {}, frozenset(), now_ms=NOW_MS,
    )
    assert plan["kept_list"] == ["m1"]
    assert plan["skipped_flagged"] == ["m2"]
    assert plan["aged_out"] == []


def test_message_without_timestamp_is_never_aged_out():
    plan = plan_inbox_archive({"m1": msg("stranger@example.com")},
                              frozenset(), {}, frozenset(), now_ms=NOW_MS)
    assert plan["aged_out"] == []
    assert plan["skipped_unknown"] == ["m1"]


def test_keep_list_beats_block_list():
    plan = plan_inbox_archive(
        {"m1": msg("crew@morningbrew.com")},
        frozenset({"crew@morningbrew.com"}), {}, frozenset({"morningbrew.com"}),
    )
    assert plan["kept_list"] == ["m1"]
    assert plan["to_trash"] == []


# ---------------------------------------------------------------------------
# Mutation tests — fake Gmail service + real sqlite DB on tmp_path
# ---------------------------------------------------------------------------

def _make_db(tmp_path):
    db = Database(tmp_path / "test.db")
    with sqlite3.connect(db.path) as conn:
        conn.execute(
            "INSERT INTO emails (gmail_id, sender, subject, is_read) VALUES ('m1', ?, 'x', 0)",
            (KNOWN_SENDER,),
        )
    return db


def test_archive_mutation_batches_and_audit_trail(tmp_path, monkeypatch):
    monkeypatch.setattr(inbox_ops.time, "sleep", lambda s: None)
    db = _make_db(tmp_path)
    service = FakeGmailService(labels=[{"id": "L1", "name": KNOWN_LABEL}])
    inbox = {
        "m1": msg(KNOWN_SENDER),
        "m2": msg("crew@morningbrew.com", unsub="<mailto:u@x.com>"),
    }

    inbox["m3"] = msg("updates@email.dailygopnews.com")
    with sqlite3.connect(db.path) as conn:
        conn.execute(
            "INSERT INTO emails (gmail_id, sender, subject, is_read) VALUES ('m3', 'updates@email.dailygopnews.com', 'y', 0)")

    result = run_inbox_archive(
        service=service, inbox_unread=inbox, account="test@x.com",
        db=db, dry_run=False, keep_senders=frozenset({"crew@morningbrew.com"}),
        blocked_senders=frozenset({"dailygopnews.com"}),
    )

    assert result["archived"] == 1
    assert result["kept_list"] == ["m2"]
    archive_calls = [b for b in service.batch_modify_calls if "TRASH" not in b["addLabelIds"]]
    trash_calls = [b for b in service.batch_modify_calls if "TRASH" in b["addLabelIds"]]
    assert len(archive_calls) == 1
    body = archive_calls[0]
    assert body["ids"] == ["m1"]
    assert body["removeLabelIds"] == ["INBOX", "UNREAD"]
    assert body["addLabelIds"] == ["L1"]
    # Blocked sender goes to trash, keep-listed id appears in no batch
    assert len(trash_calls) == 1 and trash_calls[0]["ids"] == ["m3"]
    assert all("m2" not in b["ids"] for b in service.batch_modify_calls)

    with sqlite3.connect(db.path) as conn:
        row = conn.execute(
            "SELECT action, decided_by FROM decisions WHERE gmail_id = 'm1'"
        ).fetchone()
        assert row == ("AUTO_ARCHIVE", "weekly_archive")
        row3 = conn.execute(
            "SELECT action FROM decisions WHERE gmail_id = 'm3'"
        ).fetchone()
        assert row3 == ("AUTO_TRASH",)
        # Regression: archiving must NOT mark the email read (engagement stat)
        is_read = conn.execute(
            "SELECT is_read FROM emails WHERE gmail_id = 'm1'"
        ).fetchone()[0]
        assert is_read == 0


def test_dry_run_makes_no_api_calls(tmp_path):
    db = _make_db(tmp_path)
    # service=None proves the dry-run path never touches Gmail
    result = run_inbox_archive(
        service=None, inbox_unread={"m1": msg(KNOWN_SENDER)},
        account="test@x.com", db=db, dry_run=True, keep_senders=frozenset(),
    )
    assert result["archived"] == 0
    assert result["plan"]["to_archive"] == {KNOWN_LABEL: ["m1"]}


def test_compute_sender_stats_excludes_auto_archived(tmp_path):
    db = Database(tmp_path / "test.db")
    with sqlite3.connect(db.path) as conn:
        # 4 emails from one sender: 2 read, but one of the "reads" was the robot
        for gid, is_read in [("a", 1), ("b", 1), ("c", 0), ("d", 0)]:
            conn.execute(
                "INSERT INTO emails (gmail_id, sender, is_read) VALUES (?, 's@x.com', ?)",
                (gid, is_read),
            )
    db.record_auto_archive(["b"])
    db.compute_sender_stats()
    row = db.get_sender("s@x.com")
    assert row["total_count"] == 4
    assert row["read_count"] == 1  # only the genuine human read
    assert abs(row["read_rate"] - 0.25) < 1e-9
