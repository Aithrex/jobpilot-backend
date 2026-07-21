"""Resume Platform v3 Phase C — guest matching, snapshots, claim state
machine, erasure and retention (docs/specs/resume-platform-v3.md 1.4/1.7/2/3).

Covers:
  * job_match_snapshots storage — one doc per (quota_subject_id, date),
    session snapshot_id ref, same-day replace preserves snapshot_id, no
    embedded matched_jobs anywhere, jd_sources stay server-only;
  * CXS detail enrichment re-scoring (3.1) with a fake detail fetcher;
  * non-tech resume passes the fetcher-boundary prefilter (3.2) and the
    healthcare-noise bypass + target-role bonus in _filter_and_score;
  * 75s wall-clock budget persists partial genuine results (3.3, clock
    monkeypatched);
  * empty_reason classification matrix (3.5);
  * matches retry never double-spends the global counter (3.7);
  * claim state machine (1.7): interrupted-claim resume, second-account
    rejection mid-claim, pre-claim download tokens die at claim;
  * account erasure additions (spec 2) with a fake VERSIONED S3;
  * retention sweep covers guest assets + snapshots + the orphan rule.

Fully offline: mongomock via tests/_vg_support; requests / S3 / dispatch all
monkeypatched — no live HTTP anywhere.
"""
import sys
import uuid as _uuid
from datetime import datetime, timedelta, timezone

import pytest

from _vg_support import load_service_app

from services import guest_service
from services import platform_config_service as pconfig
from utils import db_connect

COLLECTIONS = (
    "guest_sessions",
    "guest_quota_subjects",
    "guest_claim_carryins",
    "guest_global_counters",
    "guest_challenge_velocity",
    "job_match_snapshots",
    "platform_config",
    "tailoring_records",
    "tailor_usage",
    "user_resumes",
    "resume_assets",
    "resume_jobs",
    "users",
    "feature_usage",
    "sponsorship_signals",
    "quota_counters",
    "quota_ledger",
)


@pytest.fixture(autouse=True)
def _secrets(monkeypatch):
    monkeypatch.setenv("GUEST_SIGNAL_HMAC_KEY", "unit-test-signal-key")
    monkeypatch.setenv("GUEST_DOWNLOAD_TOKEN_SECRET", "unit-test-download-secret")
    monkeypatch.delenv("GUEST_CHALLENGE_PROVIDER", raising=False)
    monkeypatch.delenv("GUEST_IP_HASH_SALT", raising=False)


@pytest.fixture()
def guest_db():
    from utils.security import get_rate_limiter
    db = db_connect.DBConnect().get_db()
    for c in COLLECTIONS:
        db[c].delete_many({})
    pconfig.invalidate_cache()
    get_rate_limiter()._requests.clear()
    yield db
    pconfig.invalidate_cache()
    get_rate_limiter()._requests.clear()


def _structured(skills=None, exp_title="Backend Engineer",
                name="Jordan Alvarez", email="jordan.alvarez@example.com"):
    return {
        "contact": {"name": name, "email": email, "phone": "(555) 010-7788",
                    "linkedin": "", "github": ""},
        "summary": "Professional summary.",
        "skills": {"languages": skills if skills is not None else ["Python", "Go", "SQL"]},
        "experience": [{
            "title": exp_title, "company": "Acme",
            "dates": "2020 - 2024", "bullets": ["Did the work"],
        }],
        "education": [],
        "projects": [],
        "certifications": [],
    }


def _session_with_resume(structured=None, raw="resume text " * 10):
    s = guest_service.create_or_get_session(None, None, "iphash")
    guest_service.attach_parsed_resume(
        s["guest_id"], structured or _structured(), raw, "resume.pdf"
    )
    return s["guest_id"]


def _row(title, source="Greenhouse", description="", company="Acme",
         url=None, apply_link=None):
    return {
        "title": title, "company": company, "location": "US",
        "source": source, "description": description,
        "url": url or f"https://jobs.example/{_uuid.uuid4().hex[:8]}",
        "apply_link": apply_link or "",
    }


# ===========================================================================
# 1.4 — job_match_snapshots storage / cache / find_cached_match
# ===========================================================================

class TestSnapshots:
    def test_store_creates_snapshot_and_session_ref(self, guest_db):
        gid = _session_with_resume()
        jobs = [{"title": "T", "match_score": 90, "match_id": _uuid.uuid4().hex}]
        sid_ret = guest_service._store_matches(
            gid, jobs, 40,
            jd_sources={jobs[0]["match_id"]: {
                "title": "T", "company": "Acme", "jd_text": "JD", "url": "https://x"}},
            sources_report={"ok": ["workday_direct"], "failed": [],
                            "timed_out": [], "considered": 1},
            timings={"total_ms": 5},
        )
        snap = guest_db.job_match_snapshots.find_one({})
        assert snap and snap["snapshot_id"] == sid_ret
        assert snap["owner_type"] == "guest" and snap["guest_id"] == gid
        assert snap["quota_subject_id"]
        assert snap["date"] == guest_service._today_key()
        assert snap["purge_after"] is not None        # unclaimed: now + 7d
        assert snap["timings"]["total_ms"] == 5

        session = guest_db.guest_sessions.find_one({"guest_id": gid})
        assert session["snapshot_id"] == sid_ret
        # Embedded matched_jobs are GONE from sessions AND subjects.
        assert "matched_jobs" not in session
        subject = guest_db.guest_quota_subjects.find_one(
            {"subject_id": session["quota_subject_id"]})
        assert "matched_jobs" not in subject

    def test_same_day_replace_preserves_snapshot_id(self, guest_db):
        gid = _session_with_resume()
        first = guest_service._store_matches(gid, [{"title": "A", "match_score": 50}], 40)
        second = guest_service._store_matches(gid, [{"title": "B", "match_score": 60}], 40)
        assert first == second
        assert guest_db.job_match_snapshots.count_documents({}) == 1
        snap = guest_db.job_match_snapshots.find_one({})
        assert snap["jobs"][0]["title"] == "B"

    def test_cache_and_find_cached_match_resolve_via_snapshot(self, guest_db):
        gid = _session_with_resume()
        mid = _uuid.uuid4().hex
        guest_service._store_matches(
            gid, [{"title": "T", "match_score": 88, "match_id": mid}], 40,
            jd_sources={mid: {"title": "T", "company": "Acme",
                              "jd_text": "Python APIs", "url": "https://x"}},
        )
        cached = guest_service.get_cached_matches(gid)
        assert cached["cached"] is True
        assert cached["jobs"][0]["match_id"] == mid
        assert "jd_sources" not in cached              # server-only, never leaves
        src = guest_service.find_cached_match(gid, mid)
        assert src["jd_text"] == "Python APIs"

    def test_claim_extends_snapshot_purge(self, guest_db):
        gid = _session_with_resume()
        guest_service._store_matches(gid, [{"title": "T", "match_score": 80}], 40)
        assert guest_db.job_match_snapshots.find_one({})["purge_after"] is not None
        guest_service.claim_session(gid, "keeper@example.com")
        assert guest_db.job_match_snapshots.find_one({})["purge_after"] is None


# ===========================================================================
# 3.1 — CXS detail enrichment re-scoring
# ===========================================================================

class TestEnrichment:
    def test_description_less_workday_rows_are_enriched_and_rescored(
            self, guest_db, monkeypatch):
        pconfig.update_guest_config(
            {"guest_jobs_min_score": 40, "guest_jobs_max": 5}, "admin@test")
        gid = _session_with_resume(_structured(skills=["Python", "Go", "SQL", "AWS"]))

        wd_url = "https://acme.wd5.myworkdayjobs.com/en-US/External/job/US/SWE_JR1"
        rows = [
            _row("Software Engineer", source="Workday Direct",
                 description="", url=wd_url),
            _row("Python Backend Engineer", source="Greenhouse",
                 description="Python Go SQL AWS"),
        ]
        monkeypatch.setattr(guest_service, "_fetch_free_jobs",
                            lambda structured: rows)
        seen = {}

        def fake_details(urls, overall_budget=12):
            seen["urls"] = list(urls)
            return {wd_url: "We use Python Go SQL AWS daily."}

        monkeypatch.setattr(guest_service, "_fetch_workday_details", fake_details)

        result = guest_service.compute_guest_matches(gid)
        assert seen["urls"] == [wd_url]
        by_title = {c["title"]: c for c in result["jobs"]}
        # Title-only the Workday row scores 0 (< 40) — the enriched real
        # description rescues it (4/8 skills => 50).
        assert "Software Engineer" in by_title
        assert by_title["Software Engineer"]["match_score"] == 50
        assert "python" in by_title["Software Engineer"]["matched_skills"]

    def test_enrichment_failure_never_breaks_matching(self, guest_db, monkeypatch):
        pconfig.update_guest_config({"guest_jobs_min_score": 20}, "admin@test")
        gid = _session_with_resume(_structured(skills=["Python", "Go", "SQL", "AWS"]))
        rows = [
            _row("Python Engineer", source="Workday Direct", description="",
                 url="https://acme.wd5.myworkdayjobs.com/en-US/External/job/US/PE_1"),
            _row("Python Backend Engineer", description="Python Go SQL AWS"),
        ]
        monkeypatch.setattr(guest_service, "_fetch_free_jobs", lambda s: rows)

        def boom(urls, overall_budget=12):
            raise RuntimeError("cxs down")

        monkeypatch.setattr(guest_service, "_fetch_workday_details", boom)
        result = guest_service.compute_guest_matches(gid)
        assert any(c["title"] == "Python Backend Engineer" for c in result["jobs"])


# ===========================================================================
# 3.2 — non-tech resumes pass the prefilter
# ===========================================================================

class TestNonTechPrefilter:
    _MARKETING = None

    def _marketing_structured(self):
        return _structured(skills=["SEO", "Content Strategy", "Analytics"],
                           exp_title="Marketing Manager")

    def test_role_title_filter_accepts_resume_roles_and_keeps_tech(self, guest_db):
        tf = guest_service._role_title_filter(self._marketing_structured())
        assert tf("Marketing Manager")
        assert tf("Growth Marketing Lead")        # token overlap: "marketing"
        assert tf("Software Engineer")            # tech default kept (union)
        assert not tf("Executive Chef")

    def test_greenhouse_fetcher_respects_resume_filter(self, guest_db, monkeypatch):
        import services.daily_pipeline_service as dp

        class FakeResp:
            ok = True

            def json(self):
                return {"jobs": [
                    {"title": "Marketing Manager", "location": "US",
                     "absolute_url": "https://x/1", "content": "SEO campaigns"},
                    {"title": "Software Engineer", "location": "US",
                     "absolute_url": "https://x/2", "content": "Python"},
                ]}

        monkeypatch.setattr(dp.requests, "get",
                            lambda url, timeout=None: FakeResp())
        # Default (tech-only) filter drops the marketing role...
        default_titles = {r["title"] for r in dp._fetch_greenhouse("acme", "Acme")}
        assert default_titles == {"Software Engineer"}
        # ...the resume-derived filter keeps it (and keeps tech).
        tf = guest_service._role_title_filter(self._marketing_structured())
        titles = {r["title"] for r in dp._fetch_greenhouse("acme", "Acme", tf)}
        assert titles == {"Marketing Manager", "Software Engineer"}

    def test_filter_and_score_keeps_own_role_terms_over_noise_blocklist(self, guest_db):
        import services.daily_pipeline_service as dp
        records = [{
            "source": "Workday Direct", "company": "General Hospital",
            "title": "Registered Nurse", "location": "Boston, MA",
            "posted": "", "salary": "—", "applicants": "",
            "url": "https://hosp.example/rn", "description": "",
        }]
        apply_now, verify, excluded = dp._filter_and_score(
            records, cutoff="2020-01-01",
            custom_role_terms=["Registered Nurse"],
            experience_level="any", past_days=30,
        )
        assert len(apply_now) == 1                 # not zeroed
        assert "Target-role" in apply_now[0]["flags"]
        # Token-overlap variant also matches the role family.
        assert dp._custom_term_hit("Staff Nurse ICU", ["Registered Nurse"])


# ===========================================================================
# 3.3 — 75s wall-clock budget persists partial genuine results
# ===========================================================================

class TestWallClockBudget:
    def test_budget_expiry_persists_partial_results(self, guest_db, monkeypatch):
        import services.jd_match_scorer as jms
        pconfig.update_guest_config({"guest_jobs_min_score": 20}, "admin@test")
        gid = _session_with_resume(_structured(skills=["Python", "Go", "SQL", "AWS"]))

        state = {"t": 0.0}
        monkeypatch.setattr(guest_service, "_clock", lambda: state["t"])

        rows = [
            _row("Python Engineer A", description="Python Go SQL AWS"),
            _row("Python Engineer B", description="Python Go SQL"),
            _row("Python Engineer C", description="Python Go SQL AWS backend"),
        ]

        def fake_fetch(structured):
            state["t"] = 10.0                     # fetch consumed 10s
            return rows

        monkeypatch.setattr(guest_service, "_fetch_free_jobs", fake_fetch)

        real_score = jms.score_one

        def slow_score(**kwargs):
            state["t"] += 100.0                   # each row costs 100s
            return real_score(**kwargs)

        monkeypatch.setattr(jms, "score_one", slow_score)

        result = guest_service.compute_guest_matches(gid)
        # Rows 1+2 scored (t=110, 210); row 3 hit the 210s deadline (the v4
        # authed-parity scan budget).
        assert len(result["jobs"]) == 2
        assert "budget" in result["sources_report"]["timed_out"]
        # The PARTIAL result is persisted as today's snapshot.
        snap = guest_db.job_match_snapshots.find_one({"guest_id": gid})
        assert snap and len(snap["jobs"]) == 2
        assert guest_service.get_cached_matches(gid)["cached"] is True


# ===========================================================================
# 3.5 — empty_reason classification matrix
# ===========================================================================

class TestEmptyReason:
    def _no_skills_structured(self):
        s = _structured()
        s["skills"] = {}
        return s

    def test_fetch_raise_is_sources_unavailable(self, guest_db, monkeypatch):
        gid = _session_with_resume()

        def boom(structured):
            raise RuntimeError("network down")

        monkeypatch.setattr(guest_service, "_fetch_free_jobs", boom)
        result = guest_service.compute_guest_matches(gid)
        assert result["jobs"] == []
        assert result["empty_reason"] == "sources_unavailable"

    def test_all_sources_failed_is_sources_unavailable(self, guest_db, monkeypatch):
        gid = _session_with_resume()
        report = {"ok": [], "failed": ["workday_direct", "ats_direct"],
                  "timed_out": [], "considered": 0}
        monkeypatch.setattr(guest_service, "_fetch_free_jobs",
                            lambda s: ([], report))
        result = guest_service.compute_guest_matches(gid)
        assert result["empty_reason"] == "sources_unavailable"
        # Cached empty result carries the reason too (FE honest states).
        cached = guest_service.get_cached_matches(gid)
        assert cached["empty_reason"] == "sources_unavailable"

    def test_no_resume_skills_is_low_signal_resume(self, guest_db, monkeypatch):
        gid = _session_with_resume(self._no_skills_structured())
        monkeypatch.setattr(
            guest_service, "_fetch_free_jobs",
            lambda s: [_row("Python Engineer", description="Python Go")])
        result = guest_service.compute_guest_matches(gid)
        assert result["jobs"] == []
        assert result["empty_reason"] == "low_signal_resume"

    def test_healthy_sources_no_bar_clear_is_no_openings_matched(
            self, guest_db, monkeypatch):
        pconfig.update_guest_config({"guest_jobs_min_score": 90}, "admin@test")
        gid = _session_with_resume(_structured(skills=["Python"]))
        monkeypatch.setattr(
            guest_service, "_fetch_free_jobs",
            lambda s: [_row("Python Engineer", description="Python")])
        result = guest_service.compute_guest_matches(gid)
        assert result["jobs"] == []
        assert result["empty_reason"] == "no_openings_matched"
        assert result["total_considered"] == 1

    def test_non_empty_result_has_no_empty_reason(self, guest_db, monkeypatch):
        pconfig.update_guest_config({"guest_jobs_min_score": 10}, "admin@test")
        gid = _session_with_resume(_structured(skills=["Python"]))
        monkeypatch.setattr(
            guest_service, "_fetch_free_jobs",
            lambda s: [_row("Python Engineer", description="Python")])
        result = guest_service.compute_guest_matches(gid)
        assert result["jobs"] and "empty_reason" not in result


# ===========================================================================
# 3.7 — failed-run retry never double-spends the global counter
# ===========================================================================

class TestMatchesRetryNoDoubleSpend:
    def test_retry_after_failed_run_does_not_respend(self, guest_db, monkeypatch):
        import services.resume_service as rs
        monkeypatch.setattr(rs.ResumeService, "invoke_async",
                            staticmethod(lambda *a, **k: None))
        pconfig.update_guest_config({"global_daily_guest_matches": 5}, "admin@test")
        app = load_service_app("jobs")
        client = app.test_client()
        gid = _session_with_resume()

        first = client.post("/api/jobs/guest/matches", json={"guest_id": gid})
        assert first.status_code == 202
        job1 = first.get_json()["job_id"]

        # In-flight repeat: idempotent — the SAME job is handed back.
        repeat = client.post("/api/jobs/guest/matches", json={"guest_id": gid})
        assert repeat.status_code == 202
        assert repeat.get_json()["job_id"] == job1
        assert repeat.get_json()["existing"] is True

        # The run fails; a retry re-dispatches WITHOUT re-spending.
        rs.get_resume_service().fail_job(job1, "boom")
        retry = client.post("/api/jobs/guest/matches", json={"guest_id": gid})
        assert retry.status_code == 202
        job2 = retry.get_json()["job_id"]
        assert job2 != job1

        counter = guest_db.guest_global_counters.find_one(
            {"_id": {"$regex": "^guest_matches:"}})
        assert counter["count"] == 1               # spent exactly ONCE

    def test_capacity_rollback_releases_subject_reservation(
            self, guest_db, monkeypatch):
        import services.resume_service as rs
        monkeypatch.setattr(rs.ResumeService, "invoke_async",
                            staticmethod(lambda *a, **k: None))
        pconfig.update_guest_config({"global_daily_guest_matches": 0}, "admin@test")
        app = load_service_app("jobs")
        gid = _session_with_resume()
        resp = app.test_client().post(
            "/api/jobs/guest/matches", json={"guest_id": gid})
        assert resp.status_code == 429
        # The (subject, date) flag was rolled back — a later slot (admin
        # raises the cap) still allows today's FIRST spend.
        sid = guest_service.subject_for_guest(gid)
        subject = guest_db.guest_quota_subjects.find_one({"subject_id": sid})
        assert subject.get("matches_reserved") is None


# ===========================================================================
# 1.7 — claim state machine
# ===========================================================================

class TestClaimStateMachine:
    def test_full_claim_records_state_and_steps(self, guest_db):
        gid = _session_with_resume()
        guest_db.tailoring_records.insert_one({
            "record_id": str(_uuid.uuid4()), "guest_id": gid, "user_email": None,
            "tailored_resume": {"summary": "x"}, "source": "guest_tailor",
        })
        res = guest_service.claim_session(gid, "claimer@example.com")
        assert res["claimed"] is True and res["records_transferred"] == 1
        session = guest_db.guest_sessions.find_one({"guest_id": gid})
        claim = session["claim"]
        assert claim["state"] == "completed"
        assert claim["email"] == "claimer@example.com"
        assert claim["started_at"] and claim["completed_at"]
        assert claim["steps"] == {"records": True, "resume": True,
                                  "subject_link": True, "carryin": True}
        # Legacy fields set at completion.
        assert session["claimed_by"] == "claimer@example.com"
        # The guest asset was attached, retention lifted.
        asset = guest_db.resume_assets.find_one({"guest_id": gid})
        assert asset["claim_status"] == "claimed"
        assert asset["user_email"] == "claimer@example.com"
        assert asset["purge_after"] is None

    def test_interrupted_claim_resumes_for_same_account(self, guest_db):
        gid = _session_with_resume()
        email = "resumer@example.com"
        # Simulate an interruption mid-transfer: state stuck at transferring
        # with the records step already done.
        started = datetime.now(timezone.utc) - timedelta(minutes=5)
        guest_db.guest_sessions.update_one(
            {"guest_id": gid},
            {"$set": {"claim.state": "transferring", "claim.email": email,
                      "claim.started_at": started,
                      "claim.steps.records": True}})
        res = guest_service.claim_session(gid, email)
        assert res["claimed"] is True and res["already_claimed"] is False
        session = guest_db.guest_sessions.find_one({"guest_id": gid})
        assert session["claim"]["state"] == "completed"
        assert session["claimed_by"] == email
        # started_at from the ORIGINAL attempt survives the resume
        # (mongomock stores naive ms-precision datetimes — compare epochs).
        stored = session["claim"]["started_at"].replace(tzinfo=timezone.utc)
        assert abs((stored - started).total_seconds()) < 1

    def test_second_account_rejected_mid_claim(self, guest_db):
        gid = _session_with_resume()
        guest_db.guest_sessions.update_one(
            {"guest_id": gid},
            {"$set": {"claim.state": "claim_pending",
                      "claim.email": "first@example.com"}})
        res = guest_service.claim_session(gid, "second@example.com")
        assert res.get("error") == "already_claimed"
        # Nothing transferred for the loser.
        assert guest_db.resume_assets.find_one(
            {"user_email": "second@example.com"}) is None

    def test_pre_claim_download_token_dies_at_claim(self, guest_db, monkeypatch):
        import services.resume_service as rs
        monkeypatch.setattr(rs.ResumeService, "invoke_async",
                            staticmethod(lambda *a, **k: None))
        app = load_service_app("resume")
        app.config["TESTING"] = True
        client = app.test_client()

        gid = _session_with_resume()
        record_id = str(_uuid.uuid4())
        guest_db.tailoring_records.insert_one({
            "record_id": record_id, "guest_id": gid, "user_email": None,
            "tailored_resume": {"summary": "x"}, "source": "guest_tailor",
        })
        token, _exp = guest_service.mint_download_token(record_id, gid, "pdf")

        guest_service.claim_session(gid, "owner@example.com")

        # REDEMPTION re-checks guest ownership — the pre-claim token is dead.
        resp = client.get(f"/api/resume/guest/download/{token}")
        assert resp.status_code == 404
        # And MINTING for the claimed record is over too.
        mint = client.post("/api/resume/guest/download",
                           json={"guest_id": gid, "record_id": record_id,
                                 "format": "pdf"})
        assert mint.status_code == 404


# ===========================================================================
# Spec 2 — account erasure additions (versioned S3 fake)
# ===========================================================================

class _FakeVersionedStorage:
    def __init__(self, fail_prefixes=()):
        self.bucket = "test-bucket"
        self.purged = []          # prefixes version-purged
        self.fail_prefixes = set(fail_prefixes)

    def delete_prefix_all_versions(self, prefix):
        self.purged.append(prefix)
        if prefix in self.fail_prefixes:
            return {"deleted": 0, "errors": [f"{prefix}@v1"]}
        return {"deleted": 2, "errors": []}       # 2 versions per prefix

    def delete_resume(self, s3_key):
        return True


class TestErasureAdditions:
    def _seed(self, db, email, storage, monkeypatch):
        from bson import ObjectId
        from services import account_service
        import services.s3_service as s3s

        monkeypatch.setattr(s3s, "get_storage_service", lambda: storage)
        monkeypatch.setattr(account_service, "_revoke_providers", lambda e: {})

        user_id = ObjectId()
        db.users.insert_one({"_id": user_id, "email": email})
        db.resume_assets.insert_one({
            "resume_id": "userasset1", "owner_type": "user",
            "user_email": email, "user_id": str(user_id),
            "structured": {"summary": "s"}, "parse_status": "parsed",
        })
        # A guest world claimed by this account.
        gid = str(_uuid.uuid4())
        sid = str(_uuid.uuid4())
        db.guest_sessions.insert_one({
            "guest_id": gid, "claimed_by": email, "quota_subject_id": sid,
            "resume_file_s3_keys": ["guest/legacy/key.pdf"],
        })
        db.resume_assets.insert_one({
            "resume_id": "guestasset1", "owner_type": "guest",
            "guest_id": gid, "user_email": email, "claim_status": "claimed",
        })
        db.job_match_snapshots.insert_one({
            "snapshot_id": "snap1", "guest_id": gid, "quota_subject_id": sid,
            "date": "2026-07-20", "jobs": [],
        })
        db.guest_quota_subjects.insert_one({
            "subject_id": sid, "tailors_used": 1,
            "linked_accounts": [email],
        })
        db.quota_counters.insert_one(
            {"_id": f"tailor:{email}:2026-07-20", "count": 3})
        db.quota_ledger.insert_one(
            {"entry_id": "e1", "user_email": email, "feature": "tailor"})
        db.guest_claim_carryins.insert_one(
            {"subject_id": sid, "date": "2026-07-20", "email": email})
        return str(user_id), gid, sid

    def test_hard_purge_erases_guest_world_and_versioned_s3(
            self, guest_db, monkeypatch):
        from services import account_service
        email = "erase.me@example.com"
        storage = _FakeVersionedStorage()
        user_id, gid, sid = self._seed(guest_db, email, storage, monkeypatch)

        report = account_service.hard_purge(email)

        assert guest_db.guest_sessions.find_one({"guest_id": gid}) is None
        assert guest_db.resume_assets.count_documents({}) == 0
        assert guest_db.job_match_snapshots.count_documents({}) == 0
        assert guest_db.quota_ledger.count_documents({}) == 0
        assert guest_db.guest_claim_carryins.count_documents({}) == 0
        # Sole-member pool: counters deleted.
        assert guest_db.quota_counters.count_documents({}) == 0
        # Subject RETAINED as abuse memory, but without the email.
        subject = guest_db.guest_quota_subjects.find_one({"subject_id": sid})
        assert subject is not None
        assert email not in (subject.get("linked_accounts") or [])
        # Versioned purge covered: user prefix, both asset prefixes, legacy key.
        assert f"{user_id}/" in storage.purged
        assert "resume-assets/userasset1/" in storage.purged
        assert "resume-assets/guestasset1/" in storage.purged
        assert "guest/legacy/key.pdf" in storage.purged
        # Identity removed LAST, after verified storage cleanup.
        assert guest_db.users.count_documents({"email": email}) == 0
        assert report["errors"] == []

    def test_shared_pool_counters_survive(self, guest_db, monkeypatch):
        from services import account_service
        email = "shared.pool@example.com"
        storage = _FakeVersionedStorage()
        _uid, _gid, sid = self._seed(guest_db, email, storage, monkeypatch)
        guest_db.guest_quota_subjects.update_one(
            {"subject_id": sid},
            {"$addToSet": {"linked_accounts": "other@example.com"}})
        account_service.hard_purge(email)
        # NOT the sole member -> the pool counter survives (still meters the
        # other linked account).
        assert guest_db.quota_counters.count_documents({}) == 1

    def test_s3_failure_retains_users_doc_for_retry(self, guest_db, monkeypatch):
        from services import account_service
        email = "retry.me@example.com"
        storage = _FakeVersionedStorage(
            fail_prefixes={"resume-assets/userasset1/"})
        self._seed(guest_db, email, storage, monkeypatch)

        report = account_service.hard_purge(email)

        # Storage cleanup NOT verified -> identity record retained, flagged.
        user = guest_db.users.find_one({"email": email})
        assert user is not None
        assert user["deletion_status"] == "purge_incomplete"
        assert any("retained pending storage cleanup" in e for e in report["errors"])


# ===========================================================================
# Retention sweep — guest assets + snapshots + the spec-2 orphan rule
# ===========================================================================

class _RecordingStorage:
    def __init__(self):
        self.deleted = []

    def delete_resume(self, s3_key):
        self.deleted.append(s3_key)
        return True


class TestRetentionAssetSweep:
    def test_expired_session_assets_and_snapshots_swept(
            self, guest_db, monkeypatch):
        import services.s3_service as s3s
        storage = _RecordingStorage()
        monkeypatch.setattr(s3s, "get_storage_service", lambda: storage)

        gid = _session_with_resume()
        guest_service._store_matches(gid, [{"title": "T", "match_score": 80}], 40)
        session = guest_db.guest_sessions.find_one({"guest_id": gid})
        asset_key = f"resume-assets/{session['resume_id']}/original"
        guest_db.resume_assets.update_one(
            {"resume_id": session["resume_id"]},
            {"$set": {"object_key": asset_key}})
        guest_db.guest_sessions.update_one(
            {"guest_id": gid},
            {"$set": {"last_seen_at":
                      datetime.now(timezone.utc) - timedelta(days=45)}})

        # A CLAIMED asset of another guest is never touched by retention.
        guest_db.resume_assets.insert_one({
            "resume_id": "claimedone", "owner_type": "guest",
            "guest_id": gid, "claim_status": "claimed",
            "object_key": "resume-assets/claimedone/original",
        })

        out = guest_service.run_retention_cleanup()
        assert out["sessions_deleted"] == 1
        assert out["assets_deleted"] == 1
        assert out["snapshots_deleted"] == 1
        assert asset_key in storage.deleted
        assert guest_db.resume_assets.find_one({"resume_id": "claimedone"})
        assert guest_db.job_match_snapshots.count_documents({}) == 0

    def test_orphan_pending_assets_swept_after_24h(self, guest_db, monkeypatch):
        import services.s3_service as s3s
        from services import resume_access
        storage = _RecordingStorage()
        monkeypatch.setattr(s3s, "get_storage_service", lambda: storage)

        stale = resume_access.create_pending_asset(
            owner_type="guest", guest_id=str(_uuid.uuid4()), filename="a.pdf")
        fresh = resume_access.create_pending_asset(
            owner_type="guest", guest_id=str(_uuid.uuid4()), filename="b.pdf")
        guest_db.resume_assets.update_one(
            {"resume_id": stale},
            {"$set": {"created_at":
                      datetime.now(timezone.utc) - timedelta(hours=30)}})

        out = guest_service.run_retention_cleanup()
        assert out["orphan_assets_deleted"] == 1
        assert guest_db.resume_assets.find_one({"resume_id": stale}) is None
        assert guest_db.resume_assets.find_one({"resume_id": fresh}) is not None
        # Any stray object under the derived key was cleaned up too.
        assert resume_access.asset_object_key(stale) in storage.deleted


if __name__ == "__main__":  # allow `python tests/test_guest_matching_v3.py`
    sys.exit(pytest.main([__file__, "-q"]))
