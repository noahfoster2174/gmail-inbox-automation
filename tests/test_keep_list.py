import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from gmail_cleanup.keep import is_blocked, load_blocked_senders, load_keep_senders


def test_parses_addresses_comments_and_blanks(tmp_path):
    f = tmp_path / "keep_senders.txt"
    f.write_text(
        "# protected senders\n"
        "\n"
        "crew@morningbrew.com\n"
        "  TechBrew@MorningBrew.com  \n"
        "cfobrew@morningbrew.com  # inline comment\n"
        "#commented@out.com\n"
    )
    assert load_keep_senders(f) == frozenset({
        "crew@morningbrew.com",
        "techbrew@morningbrew.com",
        "cfobrew@morningbrew.com",
    })


def test_missing_file_returns_empty_set(tmp_path):
    assert load_keep_senders(tmp_path / "nope.txt") == frozenset()


def test_empty_file_returns_empty_set(tmp_path):
    f = tmp_path / "keep_senders.txt"
    f.write_text("# only comments\n\n")
    assert load_keep_senders(f) == frozenset()


def test_blocked_loader_parses_domains_and_addresses(tmp_path):
    f = tmp_path / "blocked_senders.txt"
    f.write_text("# spam\nDailyGopNews.com\nspecific@person.com  # exact\n")
    assert load_blocked_senders(f) == frozenset({"dailygopnews.com", "specific@person.com"})


def test_is_blocked_matches_address_domain_and_subdomain():
    blocked = frozenset({"dailygopnews.com", "specific@person.com"})
    assert is_blocked("updates@email.dailygopnews.com", blocked)
    assert is_blocked("anything@dailygopnews.com", blocked)
    assert is_blocked("specific@person.com", blocked)
    assert not is_blocked("other@person.com", blocked)  # address entry is exact
    assert not is_blocked("news@notdailygopnews.com", blocked)  # no substring match
    assert not is_blocked("crew@morningbrew.com", blocked)
