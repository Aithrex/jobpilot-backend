"""The tech-feed feedback contract: what a "no" means, and what it does NOT.

The load-bearing test in this file is the one asserting that `wrong_company`,
`stale_job` and `incorrect_metadata` are RECORDED but never suppress anything.
Those three are reports that our RECORD is wrong (attribution, liveness, field
accuracy) - not statements about what the user wants to see. Treating them as
preferences would let one unverified click delete real postings from a user's
feed, with no way for them to tell "gone" from "wrong". Only `not_interested`,
`irrelevant_role` and `duplicate` hide a job.

Also pinned here:
  * the endpoint is idempotent per (user, job) - one row, however many taps;
  * undo is unconditional 204, so a retry is never an error;
  * a guest search does not touch the feedback collection at all.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

import pytest

from _vg_support import load_service_app
from services import fit_score
from services import job_content_store as store
from services import job_inventory_service as inv
from services import tech_feed_feedback as tff
from services import tech_feed_service as tfs

# Relative, not pinned: `search()` reads the real clock, so a fixed date aged
# the seeded postings out of the default freshness window on 2026-10-01 and
# every suppression test went red with no code change.
NOW = datetime.now(timezone.utc).replace(microsecond=0)
USER = "feedback-test@aspirely.me"
OTHER = "someone-else@aspirely.me"
COLLECTIONS = ("job_postings", "job_source_state", "job_ingestion_runs",
               "tech_feed_searches", "tech_feed_feedback",
               "ingest_runner_heartbeat", "companies", "company_ats_endpoints")

SWE_JD = ("Senior Backend Engineer. Requirements: 5+ years of experience with "
          "Python, AWS, Kubernetes and PostgreSQL. Preferred: Terraform, Go.")


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


@pytest.fixture()
def jobs_app():
    return load_service_app("jobs")


def _token(flask_app, identity=USER):
    from flask_jwt_extended import create_access_token
    with flask_app.app_context():
        return create_access_token(identity=identity)


def _auth(flask_app, identity=USER):
    return {"Authorization": f"Bearer {_token(flask_app, identity)}"}


def seed(*, sjid, title="Senior Backend Engineer", company="Acme Inc",
         description=SWE_JD, tenant="acme", url=None):
    """One active posting in the inventory. Returns its `job_id`."""
    raw = {
        "source": "career-pages", "source_job_id": sjid,
        "url": url or f"https://boards.greenhouse.io/{tenant}/jobs/{sjid}",
        "company": company, "title": title, "location": ["Austin, TX, USA"],
        "description": description, "employment_type": "Full time",
        "cost_class": "free", "source_quality": "direct", "industry": "tech",
        "ats": "greenhouse", "tenant": tenant,
        "posted_at": NOW - timedelta(days=2),
    }
    inv.upsert_posting(raw, now=NOW)
    doc = inv.get_posting("career-pages", sjid)
    inv.mark_requirements_parsed(
        "career-pages", sjid, doc["content_hash"],
        fit_score.extract_requirements(description, title), now=NOW)
    return inv.get_posting("career-pages", sjid)["job_id"]


def search(**over):
    body = {"tech_scope": ["core"], "page_size": 25, "personalize": False,
            "posted_within_days": 30}
    body.update(over)
    return tfs.search(body)


def titles(result):
    return [j["title_raw"] for j in result["jobs"]]


# ---------------------------------------------------------------------------
# The closed reason set
# ---------------------------------------------------------------------------

class TestReasons:
    def test_exactly_six_reasons_exist(self):
        assert tff.REASONS == ("not_interested", "irrelevant_role",
                               "wrong_company", "duplicate", "stale_job",
                               "incorrect_metadata")

    def test_only_three_of_them_suppress(self):
        assert tff.SUPPRESSING_REASONS == {"not_interested", "irrelevant_role",
                                           "duplicate"}
        # The complement is the data-quality half, and the two halves partition
        # REASONS with nothing left over and nothing counted twice.
        assert tff.DATA_QUALITY_REASONS == {"wrong_company", "stale_job",
                                            "incorrect_metadata"}
        assert (tff.SUPPRESSING_REASONS | tff.DATA_QUALITY_REASONS) == set(tff.REASONS)
        assert not (tff.SUPPRESSING_REASONS & tff.DATA_QUALITY_REASONS)

    @pytest.mark.parametrize("reason", list(tff.REASONS))
    def test_every_reason_is_accepted(self, db, jobs_app, reason):
        resp = jobs_app.test_client().post(
            "/api/jobs/tech-feed/feedback", headers=_auth(jobs_app),
            json={"job_id": f"job-{reason}", "reason": reason})
        assert resp.status_code == 204, resp.get_data(as_text=True)

    @pytest.mark.parametrize("reason", [
        "", "  ", "nope", "NOT_INTERESTED_MAYBE", "spam", None, 7,
        {"$ne": None}, ["not_interested"],
    ])
    def test_anything_else_is_400(self, db, jobs_app, reason):
        resp = jobs_app.test_client().post(
            "/api/jobs/tech-feed/feedback", headers=_auth(jobs_app),
            json={"job_id": "job-1", "reason": reason})
        assert resp.status_code == 400, resp.get_data(as_text=True)
        assert "reason" in resp.get_json()["error"]

    def test_a_reason_is_case_insensitive_but_stored_canonically(self, db, jobs_app):
        resp = jobs_app.test_client().post(
            "/api/jobs/tech-feed/feedback", headers=_auth(jobs_app),
            json={"job_id": "job-case", "reason": " Not_Interested "})
        assert resp.status_code == 204
        row = db.tech_feed_feedback.find_one({"job_id": "job-case"})
        assert row["reason"] == "not_interested"


# ---------------------------------------------------------------------------
# The endpoint contract
# ---------------------------------------------------------------------------

class TestEndpointContract:
    def test_missing_job_id_is_400(self, db, jobs_app):
        for body in ({"reason": "not_interested"},
                     {"job_id": "", "reason": "not_interested"},
                     {"job_id": {"$ne": None}, "reason": "not_interested"}):
            resp = jobs_app.test_client().post(
                "/api/jobs/tech-feed/feedback", headers=_auth(jobs_app), json=body)
            assert resp.status_code == 400, body
            assert resp.get_json()["error"] == "job_id is required"

    def test_success_is_204_with_no_body(self, db, jobs_app):
        resp = jobs_app.test_client().post(
            "/api/jobs/tech-feed/feedback", headers=_auth(jobs_app),
            json={"job_id": "job-204", "reason": "duplicate"})
        assert resp.status_code == 204
        assert resp.get_data() == b""

    def test_requires_a_jwt(self, jobs_app):
        # 401 here is the @jwt_required() missing-token contract, NOT an auth
        # failure code - the only place 401 is allowed.
        c = jobs_app.test_client()
        assert c.post("/api/jobs/tech-feed/feedback", json={}).status_code == 401
        assert c.get("/api/jobs/tech-feed/feedback").status_code == 401
        assert c.delete("/api/jobs/tech-feed/feedback/x").status_code == 401

    def test_reposting_the_same_pair_is_idempotent(self, db, jobs_app):
        c = jobs_app.test_client()
        h = _auth(jobs_app)
        for reason in ("not_interested", "not_interested", "irrelevant_role"):
            assert c.post("/api/jobs/tech-feed/feedback", headers=h,
                          json={"job_id": "job-dup", "reason": reason,
                                "note": "n"}).status_code == 204
        rows = list(db.tech_feed_feedback.find({"user_email": USER,
                                                "job_id": "job-dup"}))
        assert len(rows) == 1
        # The latest thing the user said wins; created_at is preserved, so an
        # amendment is not a new event.
        assert rows[0]["reason"] == "irrelevant_role"
        assert rows[0]["created_at"] <= rows[0]["updated_at"]

    def test_delete_removes_it_and_is_unconditionally_204(self, db, jobs_app):
        c = jobs_app.test_client()
        h = _auth(jobs_app)
        c.post("/api/jobs/tech-feed/feedback", headers=h,
               json={"job_id": "job-del", "reason": "not_interested"})
        assert db.tech_feed_feedback.count_documents({"job_id": "job-del"}) == 1

        first = c.delete("/api/jobs/tech-feed/feedback/job-del", headers=h)
        assert first.status_code == 204 and first.get_data() == b""
        assert db.tech_feed_feedback.count_documents({"job_id": "job-del"}) == 0
        # Deleting nothing is not an error: a retried undo must look identical.
        assert c.delete("/api/jobs/tech-feed/feedback/job-del",
                        headers=h).status_code == 204
        assert c.delete("/api/jobs/tech-feed/feedback/never-existed",
                        headers=h).status_code == 204

    def test_get_returns_the_suppressed_ids(self, db, jobs_app):
        c = jobs_app.test_client()
        h = _auth(jobs_app)
        c.post("/api/jobs/tech-feed/feedback", headers=h,
               json={"job_id": "job-a", "reason": "not_interested",
                     "dedupe_group_id": "grp-a"})
        c.post("/api/jobs/tech-feed/feedback", headers=h,
               json={"job_id": "job-b", "reason": "duplicate"})

        resp = c.get("/api/jobs/tech-feed/feedback", headers=h)
        assert resp.status_code == 200
        body = resp.get_json()
        assert set(body["job_ids"]) == {"job-a", "job-b"}
        assert body["dedupe_group_ids"] == ["grp-a"]
        assert body["employer_ids"] == []
        assert body["updated_at"]

    def test_get_is_marked_private_and_short_lived(self, db, jobs_app):
        """The view sets `private, max-age=30` - this is per-user state, so it
        must never land in a shared cache.

        Asserted BEFORE the service-wide `set_security_headers` hook
        (`services/jobs/app.py:87`), which today rewrites Cache-Control to
        `no-store` for every `/api/` path except `/api/jobs/logo/*` - so the
        response the client sees is `no-store`, exactly as for the existing
        `/tech-feed/taxonomy` route and its `private, max-age=3600`. Flask runs
        `after_request` handlers in reverse registration order, so this probe
        registered last sees the view's own header first.
        """
        seen = {}

        @jobs_app.after_request
        def _probe(resp):  # pragma: no cover - test scaffolding
            seen.setdefault("cc", resp.headers.get("Cache-Control"))
            return resp

        resp = jobs_app.test_client().get("/api/jobs/tech-feed/feedback",
                                          headers=_auth(jobs_app))
        assert resp.status_code == 200
        assert seen["cc"] == "private, max-age=30"
        assert resp.headers["Cache-Control"] == "no-store"

    def test_suppression_is_per_user(self, db, jobs_app):
        c = jobs_app.test_client()
        c.post("/api/jobs/tech-feed/feedback", headers=_auth(jobs_app, USER),
               json={"job_id": "job-mine", "reason": "not_interested"})
        body = c.get("/api/jobs/tech-feed/feedback",
                     headers=_auth(jobs_app, OTHER)).get_json()
        assert body["job_ids"] == []

    def test_a_long_note_is_capped_at_500_chars(self, db, jobs_app):
        jobs_app.test_client().post(
            "/api/jobs/tech-feed/feedback", headers=_auth(jobs_app),
            json={"job_id": "job-note", "reason": "incorrect_metadata",
                  "note": "x" * 5000})
        row = db.tech_feed_feedback.find_one({"job_id": "job-note"})
        assert len(row["note"]) == tff.MAX_NOTE_CHARS == 500

    def test_rate_limited_after_240_in_five_minutes(self, db, jobs_app, monkeypatch):
        from utils import security
        limiter = security.get_rate_limiter()
        calls = {"n": 0}
        real = limiter.is_rate_limited

        def fake(key, max_requests=100, window_seconds=60):
            if key.startswith("jobs_tech_feed_feedback:"):
                calls["n"] += 1
                assert (max_requests, window_seconds) == (240, 300)
                return calls["n"] > 240
            return real(key, max_requests, window_seconds)

        monkeypatch.setattr(limiter, "is_rate_limited", fake)
        c, h = jobs_app.test_client(), _auth(jobs_app)
        calls["n"] = 240
        resp = c.post("/api/jobs/tech-feed/feedback", headers=h,
                      json={"job_id": "job-rl", "reason": "not_interested"})
        assert resp.status_code == 429
        assert "error" in resp.get_json()


# ---------------------------------------------------------------------------
# Recorded != suppressed. The distinction this feature exists to hold.
# ---------------------------------------------------------------------------

class TestDataQualityReasonsDoNotSuppress:
    @pytest.mark.parametrize("reason", ["wrong_company", "stale_job",
                                        "incorrect_metadata"])
    def test_recorded_but_never_suppressing(self, db, jobs_app, reason):
        jid = f"job-{reason}"
        resp = jobs_app.test_client().post(
            "/api/jobs/tech-feed/feedback", headers=_auth(jobs_app),
            json={"job_id": jid, "reason": reason,
                  "dedupe_group_id": f"grp-{reason}",
                  "employer_id": f"emp-{reason}"})
        assert resp.status_code == 204

        # RECORDED: the report exists and is attributable, so ingestion can be
        # fixed and `counts()` can show the trend.
        row = db.tech_feed_feedback.find_one({"user_email": USER, "job_id": jid})
        assert row is not None and row["reason"] == reason
        assert row["suppresses"] is False

        # NOT SUPPRESSING: none of its ids reach the filter set. A report that
        # our record is wrong is not a request to hide a posting - and
        # `wrong_company` in particular is about ATTRIBUTION, so it must never
        # hide the employer either.
        sup = tff.suppressed_for(USER)
        assert sup["job_ids"] == []
        assert sup["dedupe_group_ids"] == []
        assert sup["employer_ids"] == []

    def test_wrong_company_does_not_hide_the_posting_from_the_feed(self, db):
        seed(sjid="wc1", title="Senior Backend Engineer", company="Acme Inc")
        tff.record(USER, job_id=seed(sjid="wc2", title="Staff Platform Engineer",
                                     company="Globex"),
                   reason="wrong_company", employer_id="globex")
        got = titles(search(user_email=USER))
        assert "Staff Platform Engineer" in got
        assert "Senior Backend Engineer" in got

    def test_employer_ids_is_always_empty_even_for_many_wrong_company_reports(self, db):
        for i in range(5):
            tff.record(USER, job_id=f"j{i}", reason="wrong_company",
                       employer_id="globex")
        assert tff.suppressed_for(USER)["employer_ids"] == []

    def test_a_mixed_history_suppresses_only_the_preference_half(self, db):
        tff.record(USER, job_id="p1", reason="not_interested")
        tff.record(USER, job_id="p2", reason="irrelevant_role")
        tff.record(USER, job_id="p3", reason="duplicate")
        tff.record(USER, job_id="q1", reason="wrong_company")
        tff.record(USER, job_id="q2", reason="stale_job")
        tff.record(USER, job_id="q3", reason="incorrect_metadata")
        assert set(tff.suppressed_for(USER)["job_ids"]) == {"p1", "p2", "p3"}
        assert tff.counts()["total"] == 6


# ---------------------------------------------------------------------------
# The search path
# ---------------------------------------------------------------------------

class TestSearchSuppression:
    def test_a_suppressed_job_is_dropped_and_counted(self, db):
        keep = seed(sjid="k1", title="Senior Backend Engineer")
        hide = seed(sjid="h1", title="Staff Platform Engineer", tenant="globex")
        tff.record(USER, job_id=hide, reason="not_interested")

        result = search(user_email=USER)
        got = titles(result)
        assert "Senior Backend Engineer" in got
        assert "Staff Platform Engineer" not in got
        assert result["diagnostics"]["filtered_out"]["user_suppressed"] == 1
        assert keep in [j["job_id"] for j in result["jobs"]]

    def test_undo_restores_it(self, db):
        hide = seed(sjid="u1", title="Staff Platform Engineer", tenant="globex")
        tff.record(USER, job_id=hide, reason="duplicate")
        assert "Staff Platform Engineer" not in titles(search(user_email=USER))
        assert tff.undo(USER, hide) is True
        assert "Staff Platform Engineer" in titles(search(user_email=USER))

    def test_a_dedupe_group_suppresses_the_other_copy(self, db):
        """The same requisition through two sources is two job_ids, one group.

        Dismissing one copy has to take the other with it, or "I already saw
        this" reappears one row later.
        """
        a = seed(sjid="d1", title="Staff Platform Engineer", tenant="globex")
        doc = db.job_postings.find_one({"job_id": a})
        group = doc.get("dedupe_group_id")
        assert group
        b = seed(sjid="d2", title="Staff Platform Engineer", tenant="globex2")
        db.job_postings.update_one({"job_id": b},
                                   {"$set": {"dedupe_group_id": group}})

        tff.record(USER, job_id=a, reason="duplicate", dedupe_group_id=group)
        result = search(user_email=USER)
        ids = [j["job_id"] for j in result["jobs"]]
        assert a not in ids and b not in ids
        assert result["diagnostics"]["filtered_out"]["user_suppressed"] == 2

    def test_another_users_feedback_does_not_affect_this_feed(self, db):
        hide = seed(sjid="o1", title="Staff Platform Engineer", tenant="globex")
        tff.record(OTHER, job_id=hide, reason="not_interested")
        assert "Staff Platform Engineer" in titles(search(user_email=USER))

    def test_a_guest_search_never_queries_the_collection(self, db, monkeypatch):
        """No identity, no lookup - not an empty lookup."""
        seed(sjid="g1", title="Senior Backend Engineer")
        opened = []
        monkeypatch.setattr(tff, "_coll",
                            lambda: opened.append("coll") or db.tech_feed_feedback)
        result = search()  # no user_email at all
        assert opened == []
        assert "user_suppressed" not in (result["diagnostics"]["filtered_out"] or {})
        assert "Senior Backend Engineer" in titles(result)

    def test_hard_filter_without_a_suppression_set_drops_nothing(self, db):
        seed(sjid="n1", title="Senior Backend Engineer")
        docs, _ = tfs.retrieve(tfs.normalize_request({"tech_scope": ["core"]}),
                               now=NOW)
        kept, drops = tfs.hard_filter(
            docs, tfs.normalize_request({"tech_scope": ["core"]}))
        assert "user_suppressed" not in drops
        assert len(kept) == len(docs)


# ---------------------------------------------------------------------------
# Service-level details
# ---------------------------------------------------------------------------

class TestServiceDetails:
    def test_record_rejects_a_container_in_an_id_position(self, db):
        """A dict must never survive into a Mongo filter as an operator."""
        with pytest.raises(ValueError):
            tff.record(USER, job_id={"$ne": None}, reason="not_interested")
        with pytest.raises(ValueError):
            tff.record({"$ne": None}, job_id="j", reason="not_interested")
        with pytest.raises(ValueError):
            tff.record(USER, job_id="j", reason={"$ne": None})
        assert db.tech_feed_feedback.count_documents({}) == 0

    def test_optional_container_fields_degrade_to_empty_strings(self, db):
        tff.record(USER, job_id="j-c", reason="duplicate",
                   dedupe_group_id={"$ne": None}, employer_id=["x"],
                   search_id={"$gt": ""}, note={"$where": "1"})
        row = db.tech_feed_feedback.find_one({"job_id": "j-c"})
        assert row["dedupe_group_id"] == "" and row["employer_id"] == ""
        assert row["search_id"] == "" and row["note"] == ""
        for value in row.values():
            assert not isinstance(value, dict)

    def test_expire_at_uses_the_configured_ttl(self, db, monkeypatch):
        monkeypatch.setenv("TECH_FEED_FEEDBACK_TTL_DAYS", "7")
        row = tff.record(USER, job_id="j-ttl", reason="stale_job")
        delta = row["expire_at"] - row["updated_at"]
        assert delta == timedelta(days=7)

        monkeypatch.setenv("TECH_FEED_FEEDBACK_TTL_DAYS", "not-a-number")
        row = tff.record(USER, job_id="j-ttl2", reason="stale_job")
        assert row["expire_at"] - row["updated_at"] == timedelta(days=180)

    def test_suppressed_for_is_capped(self, db, monkeypatch):
        monkeypatch.setattr(tff, "MAX_SUPPRESSED", 3)
        for i in range(6):
            tff.record(USER, job_id=f"cap{i}", reason="not_interested")
        assert len(tff.suppressed_for(USER)["job_ids"]) == 3

    def test_suppressed_for_is_empty_for_a_missing_or_bad_identity(self, db):
        for identity in ("", None, "not-an-email", {"$ne": None}):
            assert tff.suppressed_for(identity) == {
                "job_ids": [], "dedupe_group_ids": [], "employer_ids": [],
                "updated_at": None}

    def test_a_feedback_outage_degrades_to_no_suppression(self, db, monkeypatch):
        """Better to show a job twice than to fail the whole search."""
        def boom():
            raise RuntimeError("mongo down")
        monkeypatch.setattr(tff, "_coll", boom)
        assert tff.suppressed_for(USER)["job_ids"] == []

        seed(sjid="x1", title="Senior Backend Engineer")
        assert "Senior Backend Engineer" in titles(search(user_email=USER))

    def test_counts_splits_by_reason_and_window(self, db):
        tff.record(USER, job_id="c1", reason="not_interested")
        tff.record(USER, job_id="c2", reason="wrong_company")
        tff.record(OTHER, job_id="c3", reason="wrong_company")
        db.tech_feed_feedback.update_one(
            {"job_id": "c3"},
            {"$set": {"updated_at": tff._now() - timedelta(days=5)}})

        got = tff.counts()
        assert got["total"] == 3
        assert got["total_by_reason"] == {"not_interested": 1, "wrong_company": 2}
        assert got["last_24h"] == 2
        assert got["last_24h_by_reason"] == {"not_interested": 1,
                                             "wrong_company": 1}
        assert got["suppressing_total"] == 1
        assert got["data_quality_total"] == 2

    def test_ensure_indexes_is_idempotent_and_enforces_uniqueness(self, db):
        first = tff.ensure_indexes()
        assert "user_job_unique" in first
        assert tff.ensure_indexes() == first
        # The unique index is the idempotency guarantee in the schema, not only
        # in the code: a second row for the same pair is impossible.
        tff.record(USER, job_id="uniq", reason="duplicate")
        tff.record(USER, job_id="uniq", reason="not_interested")
        assert db.tech_feed_feedback.count_documents({"job_id": "uniq"}) == 1


if __name__ == "__main__":  # allow `python tests/test_tech_feed_feedback.py`
    sys.exit(pytest.main([__file__, "-q"]))
