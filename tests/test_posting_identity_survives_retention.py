"""A posting the retention TTL deleted comes back as the SAME posting.

Reported 2026-10-21: Rippling and JazzHR jobs "repeat" - with Today selected the
board shows postings from earlier days, and jobs the user already acted on come
back. Rippling, JazzHR, iCIMS and Atlassian publish no date on the listing, so
an undated row's expiry is anchored on `first_seen_at`. The TTL deleted it
after the window while the board still listed it, and the next crawl inserted
it again with a NEW random `job_id` and `first_seen_at = now`: served as
"First seen today", and unknown to every hide/dismiss/save keyed on the old id.

`job_id` is now a function of `(source, source_job_id)`, and
`posting_sightings` remembers the first sighting past the row's deletion.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from services import job_inventory_service as inv

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _clean(mock_db):
    mock_db.job_postings.delete_many({})
    mock_db.posting_sightings.delete_many({})
    yield
    mock_db.job_postings.delete_many({})
    mock_db.posting_sightings.delete_many({})


def _raw(sjid="rippling-acme:5f1c", **over):
    base = {
        "source": "career-pages",
        "source_job_id": sjid,
        "url": f"https://ats.rippling.com/acme/jobs/{sjid}",
        "company": "Acme",
        "title": "Backend Engineer",
        "location": ["Austin, TX, USA"],
        "description": "Build services in Python and Go on AWS.",
        "posted_at": None,
        "cost_class": "free",
    }
    base.update(over)
    return base


def _ttl_delete(db):
    """What the `ttl_expire_at` index does once `expire_at` passes."""
    db.job_postings.delete_many({})


def test_job_id_is_a_function_of_identity():
    a = inv.posting_job_id("career-pages", "rippling-acme:1")
    assert a == inv.posting_job_id("career-pages", "rippling-acme:1")
    assert a != inv.posting_job_id("career-pages", "rippling-acme:2")
    assert a != inv.posting_job_id("workday", "rippling-acme:1")
    assert len(a) == 32 and int(a, 16) >= 0


def test_reborn_undated_posting_keeps_its_id_and_first_sighting(mock_db):
    first = inv.upsert_posting(_raw(), now=T0)
    _ttl_delete(mock_db)

    later = T0 + timedelta(days=8)
    again = inv.upsert_posting(_raw(), now=later)

    assert again["created"] is True
    assert again["job_id"] == first["job_id"]
    doc = mock_db.job_postings.find_one({"source_job_id": "rippling-acme:5f1c"})
    assert inv._safe_dt(doc["first_seen_at"]) == T0, "a re-insert is not a new sighting"
    # Its window is already spent, so it lives only the relisted floor.
    assert inv._safe_dt(doc["expire_at"]) == later + timedelta(
        hours=inv.RELISTED_MIN_LIFETIME_HOURS)


def test_a_legacy_random_id_is_restored_on_rebirth(mock_db):
    """Rows inserted before ids were derived carry a uuid4. The sighting
    backfill records it, and the reborn row gets it back."""
    sid = "career-pages\x1frippling-acme:5f1c"
    mock_db.posting_sightings.insert_one(
        {"_id": sid, "first_seen_at": T0, "job_id": "ab" * 16,
         "expire_at": T0 + timedelta(days=120)})
    res = inv.upsert_posting(_raw(), now=T0 + timedelta(days=9))
    assert res["job_id"] == "ab" * 16
    assert mock_db.job_postings.find_one({"job_id": "ab" * 16}) is not None


def test_a_dated_rebirth_keeps_its_own_date_anchor(mock_db):
    posted = T0 - timedelta(days=1)
    inv.upsert_posting(_raw(posted_at=posted), now=T0)
    _ttl_delete(mock_db)
    later = T0 + timedelta(days=3)
    inv.upsert_posting(_raw(posted_at=posted), now=later)
    doc = mock_db.job_postings.find_one({})
    assert inv._safe_dt(doc["first_seen_at"]) == T0
    assert inv._safe_dt(doc["expire_at"]) == posted + timedelta(
        days=inv.max_age_days_for(doc.get("seniority")))


def test_a_reborn_undated_posting_is_not_served_as_new(mock_db):
    """The serving gate for undated rows is `first_seen_at` inside the window.
    A rebirth must not satisfy it with the time of the re-insert."""
    now = datetime.now(timezone.utc)
    inv.upsert_posting(_raw(), now=now - timedelta(days=10))
    _ttl_delete(mock_db)
    inv.upsert_posting(_raw(), now=now)
    fresh = inv.find_active_fresh(max_age_days=1, include_undated=True)
    assert [d.get("source_job_id") for d in fresh] == []


def test_a_genuinely_new_posting_is_unaffected(mock_db):
    now = datetime.now(timezone.utc)
    res = inv.upsert_posting(_raw(sjid="rippling-acme:new"), now=now)
    doc = mock_db.job_postings.find_one({"job_id": res["job_id"]})
    assert inv._safe_dt(doc["first_seen_at"]) == now.replace(microsecond=(now.microsecond // 1000) * 1000) \
        or abs((inv._safe_dt(doc["first_seen_at"]) - now).total_seconds()) < 1
    fresh = inv.find_active_fresh(max_age_days=1, include_undated=True)
    assert [d.get("source_job_id") for d in fresh] == ["rippling-acme:new"]
