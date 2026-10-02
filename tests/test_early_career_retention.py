"""Early-career postings stay 30 days; everything else stays 7.

Product decision 2026-10-02. Cross-matching one early-career candidate against
100 entry-level roles pulled straight from employers' own boards, 93 were
missing from the inventory - every one of them still open, but published more
than 7 days earlier, and the crawler stores only the last 7 days. New-grad and
entry-level roles stay open for weeks; senior roles fill and churn. So intern
and entry postings now live 30 days; the 7-day window is unchanged for the
rest, and the guest flow keeps its strict 7-day promise.
"""
from datetime import datetime, timedelta, timezone

import pytest

from services import job_inventory_service as inv
from services import job_ingest_worker as worker

NOW = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)


def naive(dt):
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


@pytest.fixture()
def db():
    d = inv._db()
    d.job_postings.delete_many({})
    return d


def _raw(sjid, title, age_days, body=""):
    return {"source": "workday", "source_job_id": sjid, "company": "Acme",
            "url": f"https://acme.wd1.myworkdayjobs.com/x/job/{sjid}",
            "title": title, "location": ["Austin, TX, USA"], "description": body,
            "employment_type": "Full time", "posted_at": NOW - timedelta(days=age_days),
            "cost_class": "free", "source_quality": "direct"}


class TestTheWindowFollowsTheLevel:
    def test_max_age_by_level(self):
        assert inv.max_age_days_for("entry") == 30
        assert inv.max_age_days_for("intern") == 30
        for level in ("mid", "senior", "unknown", None):
            assert inv.max_age_days_for(level) == 7

    def test_expiry_on_insert(self, db):
        inv.upsert_posting(_raw("E1", "Software Engineer, New Grad", 2), now=NOW)
        inv.upsert_posting(_raw("S1", "Senior Software Engineer", 2), now=NOW)
        e = db.job_postings.find_one({"source_job_id": "E1"})
        s = db.job_postings.find_one({"source_job_id": "S1"})
        assert e["seniority"] == "entry"
        assert e["expire_at"] == naive(NOW - timedelta(days=2) + timedelta(days=30))
        assert s["expire_at"] == naive(NOW - timedelta(days=2) + timedelta(days=7))

    def test_the_aged_sweep_keeps_entry_to_30_days(self, db):
        inv.upsert_posting(_raw("E10", "Software Engineer I", 10), now=NOW - timedelta(days=9))
        inv.upsert_posting(_raw("S10", "Senior Software Engineer", 10), now=NOW - timedelta(days=9))
        inv.upsert_posting(_raw("E31", "Software Engineer I", 31), now=NOW - timedelta(days=30))
        inv.expire_aged_postings(now=NOW)
        status = {d["source_job_id"]: d["status"] for d in db.job_postings.find({})}
        assert status == {"E10": "active", "S10": "closed", "E31": "closed"}

    def test_an_explicit_window_still_overrides(self, db):
        inv.upsert_posting(_raw("E10", "Software Engineer I", 10), now=NOW - timedelta(days=9))
        inv.expire_aged_postings(max_age_days=7, now=NOW)
        assert db.job_postings.find_one({"source_job_id": "E10"})["status"] == "closed"

    def test_a_level_change_moves_the_expiry(self, db):
        from services import fit_score
        inv.upsert_posting(_raw("M1", "Software Engineer", 3,
                                body="We are hiring."), now=NOW)
        doc = db.job_postings.find_one({"source_job_id": "M1"})
        assert doc["expire_at"] == naive(NOW - timedelta(days=3) + timedelta(days=7))
        req = fit_score.extract_requirements("This role is for new grads.", "Software Engineer")
        assert inv.mark_requirements_parsed("workday", "M1", doc["content_hash"], req, now=NOW)
        after = db.job_postings.find_one({"source_job_id": "M1"})
        assert after["seniority"] == "entry"
        assert after["expire_at"] == naive(NOW - timedelta(days=3) + timedelta(days=30))

    def test_a_pinned_posting_is_never_given_an_expiry(self, db):
        from services import fit_score
        inv.upsert_posting(_raw("P1", "Software Engineer", 3, body="Hiring."), now=NOW)
        db.job_postings.update_one({"source_job_id": "P1"},
                                   {"$set": {"pinned": True}, "$unset": {"expire_at": ""}})
        doc = db.job_postings.find_one({"source_job_id": "P1"})
        req = fit_score.extract_requirements("For new grads.", "Software Engineer")
        inv.mark_requirements_parsed("workday", "P1", doc["content_hash"], req, now=NOW)
        assert "expire_at" not in db.job_postings.find_one({"source_job_id": "P1"})


class TestTheCrawlAdmitsAgedEarlyCareerOnly:
    FRESH = NOW - timedelta(days=7)

    def _row(self, sjid, title, age):
        return ({"member_key": "m"}, {"source_job_id": sjid, "title": title,
                                      "posted_at": NOW - timedelta(days=age)})

    def test_aged_rows_need_an_early_career_title_or_stored_level(self):
        rows = [self._row("fresh", "Senior Engineer", 2),
                self._row("aged-senior", "Senior Engineer", 12),
                self._row("aged-newgrad", "Software Engineer, New Grad", 12),
                self._row("aged-unmarked-known-entry", "Software Engineer", 12),
                self._row("aged-unmarked", "Software Engineer", 12),
                ({"member_key": "m"}, {"source_job_id": "undated", "title": "X", "posted_at": None})]
        existing = {"aged-unmarked-known-entry": {"seniority": "entry"},
                    "aged-unmarked": {"seniority": "mid"}}
        kept, dropped = worker._admit_by_age(rows, existing, fresh_cutoff=self.FRESH)
        assert {r["source_job_id"] for _m, r in kept} == {
            "fresh", "aged-newgrad", "aged-unmarked-known-entry", "undated"}
        assert dropped == 2

    def test_the_stale_close_guard_follows_the_level(self):
        early_cut = NOW - timedelta(days=30)
        aged = NOW - timedelta(days=12)
        assert worker._inside_listing_window(aged, "entry", self.FRESH, early_cut) is True
        assert worker._inside_listing_window(aged, "senior", self.FRESH, early_cut) is False
        assert worker._inside_listing_window(NOW, "senior", self.FRESH, early_cut) is True
