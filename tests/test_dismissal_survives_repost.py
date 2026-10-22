"""A dismissal or an application must survive the job coming back under a new id.

Production, 2026-10-21 (owner-run read-only probe): 778 of 1,145 saved_jobs
point at job_ids that no longer exist, 579 of them applied; ZERO live postings
share their normalized apply link. So "applied jobs come back" is mostly the
employer reposting under a NEW requisition id/URL, or an application recorded
from a non-inventory source. The matcher is layered and explainable:

  HIDE (dismissals): job_id -> dedupe group -> apply-URL key -> (board, req id)
  ANNOTATE only:     prior application (same layers) -> `previously_applied`
                     board + title + location -> `possible_repost_of`

Company + title alone never hides or flags anything.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from services import fit_score
from services import job_content_store as store
from services import job_inventory_service as inv
from services import posting_identity as pid
from services import tech_feed_feedback as tff
from services import tech_feed_service as tfs

NOW = datetime.now(timezone.utc).replace(microsecond=0)
USER = "repost-test@aspirely.me"
JD = ("Backend Engineer. Requirements: 3+ years of experience with Python, AWS "
      "and PostgreSQL.")
COLLECTIONS = ("job_postings", "posting_sightings", "tech_feed_searches",
               "tech_feed_feedback", "saved_jobs", "companies",
               "company_ats_endpoints", "job_source_state", "job_ingestion_runs",
               "ingest_runner_heartbeat")


@pytest.fixture()
def db(monkeypatch):
    monkeypatch.delenv("RESUME_S3_BUCKET", raising=False)
    store.reset_caches()
    d = inv._db()
    for c in COLLECTIONS:
        d[c].delete_many({})
    yield d
    for c in COLLECTIONS:
        d[c].delete_many({})


def seed(*, source="career-pages", sjid, url, title="Backend Engineer",
         tenant="acme", ats="greenhouse", company="Acme Inc", location="Austin, TX, USA"):
    raw = {"source": source, "source_job_id": sjid, "url": url, "company": company,
           "title": title, "location": [location], "description": JD,
           "employment_type": "Full time", "cost_class": "free",
           "source_quality": "direct", "industry": "tech", "ats": ats,
           "tenant": tenant, "posted_at": NOW - timedelta(days=1)}
    inv.upsert_posting(raw, now=NOW)
    doc = inv.get_posting(source, sjid)
    inv.mark_requirements_parsed(source, sjid, doc["content_hash"],
                                 fit_score.extract_requirements(JD, title), now=NOW)
    return inv.get_posting(source, sjid)["job_id"]


def search():
    return tfs.search({"tech_scope": ["core"], "page_size": 25, "personalize": False,
                       "posted_within_days": 30, "user_email": USER})


def card(result, job_id):
    return next((j for j in result["jobs"] if j["job_id"] == job_id), None)


def _rehome(db, job_id):
    """Give a re-ingested copy a dedupe group of its own, as a row stored
    before identity-1.0.0 would have."""
    db.job_postings.update_one({"job_id": job_id},
                               {"$set": {"dedupe_group_id": "legacy-" + job_id}})


class TestDismissalsSurvive:
    def test_same_apply_url_under_a_new_source_id_stays_hidden(self, db):
        url = "https://boards.greenhouse.io/acme/jobs/555"
        first = seed(sjid="gh-acme:555", url=url)
        tff.record(USER, job_id=first, reason="not_interested")
        db.job_postings.delete_many({"job_id": first})         # TTL / purge
        again = seed(source="aggregator", sjid="agg-998877",
                     url=url + "?utm_source=jobright&jr_id=abc", tenant="", ats="")
        _rehome(db, again)
        assert card(search(), again) is None

    def test_same_board_requisition_through_another_url_stays_hidden(self, db):
        first = seed(sjid="gh-acme:777", url="https://boards.greenhouse.io/acme/jobs/777")
        tff.record(USER, job_id=first, reason="irrelevant_role")
        other = seed(source="career-pages-own", sjid="acme-site:777",
                     url="https://acme.com/careers/job?gh_jid=777")
        _rehome(db, other)
        assert card(search(), other) is None

    def test_a_different_requisition_with_the_same_title_is_shown_and_flagged(self, db):
        first = seed(sjid="gh-acme:1", url="https://boards.greenhouse.io/acme/jobs/1")
        tff.record(USER, job_id=first, reason="not_interested")
        repost = seed(sjid="gh-acme:2", url="https://boards.greenhouse.io/acme/jobs/2")
        c = card(search(), repost)
        assert c is not None, "a look-alike must never be hidden"
        assert c["possible_repost_of"]["kind"] == "dismissed"
        assert c["possible_repost_of"]["job_id"] == first

    def test_company_and_title_alone_flag_nothing(self, db):
        first = seed(sjid="gh-acme:1", url="https://boards.greenhouse.io/acme/jobs/1")
        tff.record(USER, job_id=first, reason="not_interested")
        other = seed(sjid="lv-acme2:9", url="https://jobs.lever.co/acme2/9f1c2d3e-0000",
                     tenant="acme2", ats="lever")
        c = card(search(), other)
        assert c is not None and c["possible_repost_of"] is None


class TestApplicationsSurvive:
    def _applied(self, db, job_id, job_data, days_ago=3):
        db.saved_jobs.insert_one({"user_email": USER, "job_id": job_id,
                                  "status": "applied", "job_data": job_data,
                                  "saved_at": NOW - timedelta(days=days_ago)})

    def test_application_from_a_gone_posting_matches_on_the_apply_url(self, db):
        self._applied(db, "mnc:google:123", {
            "company": "Acme", "title": "Backend Engineer",
            "apply_link": "https://boards.greenhouse.io/acme/jobs/42?gh_src=x"})
        live = seed(sjid="gh-acme:42", url="https://boards.greenhouse.io/acme/jobs/42")
        c = card(search(), live)
        assert c is not None, "an application annotates; it never hides"
        assert c["previously_applied"]["matched_on"] == "apply_url"
        assert c["previously_applied"]["status"] == "applied"

    def test_repost_with_a_new_requisition_is_a_hint_not_a_match(self, db):
        self._applied(db, "gone-1", {
            "company": "Acme Inc", "title": "Backend Engineer", "ats": "greenhouse",
            "tenant": "acme", "location": "Austin, TX, USA",
            "apply_link": "https://boards.greenhouse.io/acme/jobs/1"})
        repost = seed(sjid="gh-acme:2", url="https://boards.greenhouse.io/acme/jobs/2")
        c = card(search(), repost)
        assert c["previously_applied"] is None
        assert c["possible_repost_of"]["kind"] == "applied"

    def test_an_old_application_does_not_flag_a_repost(self, db):
        self._applied(db, "gone-1", {
            "company": "Acme Inc", "title": "Backend Engineer", "ats": "greenhouse",
            "tenant": "acme", "location": "Austin, TX, USA",
            "apply_link": "https://boards.greenhouse.io/acme/jobs/1"},
            days_ago=tff.REPOST_HINT_DAYS + 5)
        repost = seed(sjid="gh-acme:2", url="https://boards.greenhouse.io/acme/jobs/2")
        assert card(search(), repost)["possible_repost_of"] is None

    def test_interest_is_not_an_application(self, db):
        db.saved_jobs.insert_one({"user_email": USER, "job_id": "x", "status": "interested",
                                  "job_data": {"apply_link":
                                               "https://boards.greenhouse.io/acme/jobs/42"}})
        live = seed(sjid="gh-acme:42", url="https://boards.greenhouse.io/acme/jobs/42")
        assert card(search(), live)["previously_applied"] is None


def test_the_index_explains_which_layer_hid_a_row():
    idx = pid.EngagementIndex({"job_ids": ["j1"], "dedupe_group_ids": ["g1"],
                               "apply_url_keys": ["acme.com/jobs/9"],
                               "requisition_keys": ["ats:greenhouse:acme|5"]})
    assert idx.hides({"job_id": "j1"}) == "job_id"
    assert idx.hides({"job_id": "x", "dedupe_group_id": "g1"}) == "dedupe_group"
    assert idx.hides({"job_id": "x", "url": "https://www.acme.com/jobs/9?utm_source=a"}) \
        == "apply_url"
    assert idx.hides({"job_id": "x", "ats": "greenhouse", "tenant": "acme",
                      "source_job_id": "gh-acme:5"}) == "requisition"
    assert idx.hides({"job_id": "x", "company": "Acme", "title": "Engineer"}) == ""
