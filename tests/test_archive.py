import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from gmail_cleanup.archive import make_archive_path, sanitize_filename


def test_sanitize_removes_slashes():
    assert "/" not in sanitize_filename("Hello/World")

def test_sanitize_removes_colons():
    assert ":" not in sanitize_filename("Re: Hello")

def test_sanitize_truncates():
    assert len(sanitize_filename("a" * 200)) <= 80

def test_archive_path_financial(tmp_path):
    path = make_archive_path(tmp_path, "Financial", "2024-01-15T10:00:00",
                             "Bank statement", "statements@chase.com")
    assert path.parent == tmp_path / "Financial"
    assert "2024-01-15" in path.name
    assert path.suffix == ".eml"

def test_archive_path_newsletter(tmp_path):
    path = make_archive_path(tmp_path, "Newsletters/Fitness_Health",
                             "2024-03-01T08:00:00", "Weekly recap", "digest@strava.com")
    assert path.parent == tmp_path / "Newsletters" / "Fitness_Health"

def test_archive_path_none_category_uses_misc(tmp_path):
    path = make_archive_path(tmp_path, None, "2024-01-01T00:00:00",
                             "Hello", "friend@gmail.com")
    assert path.parent == tmp_path / "Misc"

def test_archive_path_creates_unique_names(tmp_path):
    p1 = make_archive_path(tmp_path, "Personal", "2024-01-01T10:00:00",
                           "Hey", "alice@test.com")
    p2 = make_archive_path(tmp_path, "Personal", "2024-01-02T10:00:00",
                           "Hey", "alice@test.com")
    assert p1 != p2
