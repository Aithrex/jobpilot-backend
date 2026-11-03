"""A posting titled like the user's own titles is retrieved, however busy the week.

Replaying every real user's board against everything crawled in the week
(2026-11-03): today's title-matched postings were 97-100% listed and the
week's 76-89%, and every miss was never RETRIEVED - the window is the newest
CANDIDATE_LIMIT rows, two or three days for a broad profile.
"""
from datetime import datetime, timedelta, timezone

import pytest

from services import experience_view
from services import inventory_feed
from services import job_inventory_service as inv

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)
FAMS = ["software_engineering", "data_engineering"]


@pytest.fixture()
def db(monkeypatch):
    d = inv._db()
    d.job_postings.delete_many({})
    monkeypatch.setattr(inv, "_now", lambda: NOW)
    monkeypatch.setattr(inventory_feed, "CANDIDATE_LIMIT", 8)
    yield d
    d.job_postings.delete_many({})


def _post(sjid, title, age_days):
    inv.upsert_posting({
        "source": "workday", "source_job_id": sjid, "company": "Acme",
        "url": f"https://acme.wd1.myworkdayjobs.com/x/job/{sjid}", "title": title,
        "location": ["Austin, TX, USA"], "description": "", "employment_type": "Full time",
        "posted_at": NOW - timedelta(days=age_days), "cost_class": "free",
        "source_quality": "direct"}, now=NOW - timedelta(days=age_days))


def _seed(db):
    for i in range(8):                                  # today's software fills the window
        _post(f"SW{i}", "Software Engineer", 0)
    _post("DE1", "Senior Data Engineer", 4)             # titled like the user, older
    _post("DE2", "Data Engineer II", 6)
    _post("STAFF", "Staff Data Engineer", 5)
    _post("ETL", "ETL Developer", 5)                    # same family, not titled
    _post("CTR", "Data Center Engineer", 5)             # the words, not the title
    _post("OLD", "Data Engineer", 40)                   # outside the window days
    db.job_postings.update_many({}, {"$set": {"status": "active"}})


def _window():
    return inv.find_active_fresh(source="workday", max_age_days=30, limit=8, now=NOW,
                                 role_families=FAMS)


def _ids(rows):
    return {d["source_job_id"] for d in rows}


def test_titled_postings_beyond_a_full_window_are_retrieved(db):
    _seed(db)
    window = _window()
    assert _ids(window) == {f"SW{i}" for i in range(8)}
    extra = inventory_feed._title_slice(window, ["Data Engineer"], FAMS, "", source="workday")
    assert _ids(extra) == {"DE1", "DE2", "STAFF"}


def test_the_users_levels_bound_the_slice_and_an_unstated_level_rides_along(db, monkeypatch):
    _seed(db)
    monkeypatch.setattr(experience_view, "for_user", lambda email: {"levels": ["mid"]})
    extra = inventory_feed._title_slice(_window(), ["Data Engineer"], FAMS, "u@x.io",
                                        source="workday")
    assert "STAFF" not in _ids(extra)
    assert "DE2" in _ids(extra)


def test_an_unfilled_window_or_no_titles_adds_nothing(db, monkeypatch):
    _seed(db)
    window = _window()
    assert inventory_feed._title_slice(window, [], FAMS, "", source="workday") == []
    assert inventory_feed._title_slice(window, ["Engineer"], FAMS, "", source="workday") == []
    monkeypatch.setattr(inventory_feed, "CANDIDATE_LIMIT", 50)
    assert inventory_feed._title_slice(window, ["Data Engineer"], FAMS, "", source="workday") == []


def test_title_phrases_drop_seniority_and_lone_generic_nouns():
    assert inventory_feed.title_phrases(
        ["Senior Data Engineer II", "Engineer", "DevOps Engineer", "devops engineer"]
    ) == ["data engineer", "devops engineer"]
