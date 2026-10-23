"""A small job family is not crowded out of a multi-family user's candidates.

Measured 2026-10-23: the candidate window is the newest CANDIDATE_LIMIT rows
across all of a user's families. Software posts ~1,500 rows a day and data
science ~500, so a data-science user whose search also carries software
titles lost every three-day-old "Data Scientist" posting (own-family recall
0.59 against 0.81-0.93 for everyone else).
"""
from datetime import datetime, timedelta, timezone

import pytest

from services import inventory_feed
from services import job_inventory_service as inv

NOW = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)


@pytest.fixture()
def db(monkeypatch):
    d = inv._db()
    d.job_postings.delete_many({})
    monkeypatch.setattr(inv, "_now", lambda: NOW)
    return d


def _raw(sjid, title, age_days):
    return {"source": "workday", "source_job_id": sjid, "company": "Acme",
            "url": f"https://acme.wd1.myworkdayjobs.com/x/job/{sjid}",
            "title": title, "location": ["Austin, TX, USA"], "description": "",
            "employment_type": "Full time", "posted_at": NOW - timedelta(days=age_days),
            "cost_class": "free", "source_quality": "direct"}


def _seed(db):
    for i in range(8):                       # the big family fills the window
        inv.upsert_posting(_raw(f"SW{i}", "Software Engineer", 0), now=NOW)
    for i, age in enumerate((2, 3, 5)):      # the small family is a little older
        inv.upsert_posting(_raw(f"DS{i}", "Data Scientist", age), now=NOW - timedelta(days=age))
    db.job_postings.update_many({}, {"$set": {"status": "active"}})
    fams = {d["role_family"] for d in db.job_postings.find({}, {"role_family": 1})}
    assert fams == {"software_engineering", "data_science"}


def _window(monkeypatch, limit=8):
    monkeypatch.setattr(inventory_feed, "CANDIDATE_LIMIT", limit)
    return inv.find_active_fresh(source="workday", max_age_days=30, limit=limit, now=NOW,
                                 role_families=["software_engineering", "data_science"])


def test_the_crowded_out_family_gets_its_own_slice(db, monkeypatch):
    _seed(db)
    docs = _window(monkeypatch)
    assert {d["role_family"] for d in docs} == {"software_engineering"}
    extra = inventory_feed._family_slices(
        docs, ["software_engineering", "data_science"], source="workday")
    assert {d["source_job_id"] for d in extra} == {"DS0", "DS1", "DS2"}


def test_a_single_family_or_an_unfilled_window_adds_nothing(db, monkeypatch):
    _seed(db)
    docs = _window(monkeypatch)
    assert inventory_feed._family_slices(docs, ["software_engineering"], source="workday") == []
    monkeypatch.setattr(inventory_feed, "CANDIDATE_LIMIT", 50)
    assert inventory_feed._family_slices(
        docs, ["software_engineering", "data_science"], source="workday") == []


def test_a_family_with_its_fair_share_is_left_alone(db, monkeypatch):
    _seed(db)
    docs = _window(monkeypatch, limit=8)
    # Pretend half the window is already data science: no slice for it.
    for d in docs[:4]:
        d["role_family"] = "data_science"
    extra = inventory_feed._family_slices(
        docs, ["software_engineering", "data_science"], source="workday")
    assert extra == []
