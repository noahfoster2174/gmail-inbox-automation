import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import gmail_cleanup.keep as keep_mod
import gmail_cleanup.unsubscribe_pass as up
from gmail_cleanup.inbox_reduce import phase_unsubscribe, phase_unsubscribe_send

ACCOUNT = "test@example.com"


def _candidates():
    return {
        "junk@example.com": {"mailto": "mailto:u@junk.com", "https": None, "count": 5},
        "crew@morningbrew.com": {"mailto": "mailto:u@brew.com", "https": None, "count": 3},
    }


def _fail_if_called(name):
    def fn(*args, **kwargs):
        raise AssertionError(f"{name} must not be called")
    return fn


def test_pending_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(up, "BASE_DIR", tmp_path)
    up.write_pending(_candidates(), ACCOUNT)
    pending = up.load_pending(ACCOUNT)
    assert pending["account"] == ACCOUNT
    assert pending["candidates"] == _candidates()
    up.clear_pending(ACCOUNT)
    assert up.load_pending(ACCOUNT) == {}


def test_build_phase_writes_pending_and_never_sends(tmp_path, monkeypatch):
    monkeypatch.setattr(up, "BASE_DIR", tmp_path)
    monkeypatch.setattr(up, "fetch_marketing_unsubscribes", lambda svc: _candidates())
    monkeypatch.setattr(up, "run_unsubscribe_mailto", _fail_if_called("run_unsubscribe_mailto"))
    monkeypatch.setattr(up, "run_unsubscribe_http", _fail_if_called("run_unsubscribe_http"))

    phase_unsubscribe(service=None, account=ACCOUNT)

    pending = up.load_pending(ACCOUNT)
    assert set(pending["candidates"]) == set(_candidates())


def test_send_phase_refilters_keep_list_and_sends_rest(tmp_path, monkeypatch):
    monkeypatch.setattr(up, "BASE_DIR", tmp_path)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    (tmp_path / "gmail_cleanup").mkdir()
    # crew@ was added to the keep-list AFTER the pending file was generated
    monkeypatch.setattr(keep_mod, "load_keep_senders",
                        lambda path=None: frozenset({"crew@morningbrew.com"}))
    sent_maps = []

    def fake_mailto(service, unsub_map, dry_run):
        sent_maps.append(unsub_map)
        return {"sent": list(unsub_map), "failed": []}

    monkeypatch.setattr(up, "run_unsubscribe_mailto", fake_mailto)
    monkeypatch.setattr(up, "run_unsubscribe_http",
                        lambda unsub_map, dry_run: {"success": [], "needs_manual": [], "failed": []})

    up.write_pending(_candidates(), ACCOUNT)
    phase_unsubscribe_send(service=None, account=ACCOUNT, assume_yes=True)

    assert len(sent_maps) == 1
    assert set(sent_maps[0]) == {"junk@example.com"}  # keep-listed sender dropped
    assert up.load_pending(ACCOUNT) == {}  # cleared — no double-send
    results = json.loads((tmp_path / "gmail_cleanup" / "unsubscribe_results.json").read_text())
    assert results["mailto"]["sent"] == ["junk@example.com"]


def test_send_phase_aborts_without_confirmation(tmp_path, monkeypatch):
    monkeypatch.setattr(up, "BASE_DIR", tmp_path)
    monkeypatch.setattr(keep_mod, "load_keep_senders", lambda path=None: frozenset())
    monkeypatch.setattr(up, "run_unsubscribe_mailto", _fail_if_called("run_unsubscribe_mailto"))
    monkeypatch.setattr(up, "run_unsubscribe_http", _fail_if_called("run_unsubscribe_http"))
    monkeypatch.setattr("builtins.input", lambda prompt: "n")

    up.write_pending(_candidates(), ACCOUNT)
    phase_unsubscribe_send(service=None, account=ACCOUNT, assume_yes=False)

    # Nothing sent, pending file untouched
    assert set(up.load_pending(ACCOUNT)["candidates"]) == set(_candidates())


def test_send_phase_with_no_pending_is_a_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(up, "BASE_DIR", tmp_path)
    monkeypatch.setattr(up, "run_unsubscribe_mailto", _fail_if_called("run_unsubscribe_mailto"))
    phase_unsubscribe_send(service=None, account=ACCOUNT, assume_yes=True)
