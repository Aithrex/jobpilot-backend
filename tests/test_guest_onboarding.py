"""Guest Onboarding — offline test suite (docs/specs/guest-onboarding.md §QA).

Covers, per the spec:
  * identity resolution matrix — 2-signal merge, 1-signal (namesake) no-merge,
    cluster quota = SUM;
  * guest quota enforcement — cluster lifetime limit + the global daily
    circuit breaker (atomic $inc semantics);
  * claim idempotency — a double claim never double-seeds tailor_usage;
  * admin config validation bounds (service + real /api/admin route);
  * route-level guest flow on the real Flask apps (flag gating → 503, onboard
    → {guest_id, job_id}, capability-scoped polling, tailor 429
    {quota_exhausted:true}, jobs-Lambda matches cache).

Fully offline: mongomock via tests/_vg_support (patched before any service
import), no Bedrock (async dispatch is stubbed where a route would spawn the
worker thread), no network (the free-source fetch is patched with canned rows).
"""
import io
import sys

import pytest

from _vg_support import load_service_app

from services import guest_identity_service as identity
from services import guest_service
from services import platform_config_service as pconfig
from utils import db_connect

GUEST_COLLECTIONS = (
    "guest_sessions",
    "guest_global_counters",
    "platform_config",
    "tailoring_records",
    "tailor_usage",
    "user_resumes",
    "resume_jobs",
    "users",
    "feature_usage",
)

PDF_BYTES = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n"


def _structured(name="Jordan Alvarez", email="jordan.alvarez@example.com",
                phone="(555) 010-7788", skills=None):
    return {
        "contact": {"name": name, "email": email, "phone": phone,
                    "linkedin": "", "github": ""},
        "summary": "Backend engineer building APIs.",
        "skills": {"languages": skills or ["Python", "Go", "SQL"]},
        "experience": [{
            "title": "Backend Engineer", "company": "Acme",
            "dates": "2020 – 2024", "bullets": ["Built APIs in Python"],
        }],
        "education": [],
        "projects": [],
        "certifications": [],
    }


@pytest.fixture()
def guest_db():
    """Clean guest-related collections + drop the config TTL cache."""
    db = db_connect.DBConnect().get_db()
    for c in GUEST_COLLECTIONS:
        db[c].delete_many({})
    pconfig.invalidate_cache()
    yield db
    pconfig.invalidate_cache()


def _new_session(fp=None):
    return guest_service.create_or_get_session(None, fp, "iphash")


def _attach(guest_id, structured, raw_text, fp=None):
    return guest_service.attach_parsed_resume(
        guest_id, structured, raw_text, "resume.pdf", fingerprint_hash=fp
    )


# ===========================================================================
# Identity resolution matrix
# ===========================================================================

class TestIdentityResolution:
    def test_extract_signals_normalization(self, guest_db):
        sig = identity.extract_signals(
            _structured(name="  JOrdan   Álvarez ", phone="+1 (555) 010-7788"),
            fingerprint_hash="fp_ABC-123",
            raw_text="Jordan Alvarez resume text " * 5,
        )
        assert sig["name_normalized"] == "jordan alvarez"
        assert sig["emails"] == ["jordan.alvarez@example.com"]
        assert sig["phones"] == ["15550107788"]
        assert len(sig["resume_hash"]) == 64
        assert sig["fingerprint"] == "fp_ABC-123"

    def test_two_signal_match_merges_cluster(self, guest_db):
        raw_a = "Jordan Alvarez jordan.alvarez@example.com backend python " * 4
        a = _new_session()
        _attach(a["guest_id"], _structured(), raw_a)

        # Second device, DIFFERENT resume text/fingerprint but same email+phone
        # (2 distinct signal types) -> same person, soft merge.
        b = _new_session(fp="fp_other_device")
        rec = _attach(
            b["guest_id"],
            _structured(),
            "totally different canonical text for hashing " * 4,
            fp="fp_other_device",
        )
        assert rec["recognized"] is True
        assert rec["recognized_name"] == "jordan alvarez"

        b_doc = guest_db.guest_sessions.find_one({"guest_id": b["guest_id"]})
        assert b_doc["merged_into"] == a["guest_id"]
        assert set(identity.cluster_ids(b["guest_id"])) == {a["guest_id"], b["guest_id"]}

    def test_single_signal_namesake_does_not_merge(self, guest_db):
        a = _new_session()
        _attach(a["guest_id"], _structured(), "resume text one " * 5)

        # Same NAME only — different email, phone, resume text, no fingerprint.
        namesake = _structured(
            email="other.jordan@elsewhere.org", phone="(444) 222-9999"
        )
        b = _new_session()
        rec = _attach(b["guest_id"], namesake, "a wholly different resume " * 5)
        assert rec["recognized"] is False

        b_doc = guest_db.guest_sessions.find_one({"guest_id": b["guest_id"]})
        assert b_doc["merged_into"] is None
        assert identity.cluster_ids(b["guest_id"]) == [b["guest_id"]]

    def test_cluster_quota_is_sum(self, guest_db):
        raw = "shared resume content for both sessions " * 4
        a = _new_session()
        _attach(a["guest_id"], _structured(), raw)
        b = _new_session()
        _attach(b["guest_id"], _structured(), raw)  # email+phone+hash match
        guest_db.guest_sessions.update_one(
            {"guest_id": a["guest_id"]}, {"$set": {"tailors_used": 3}})
        guest_db.guest_sessions.update_one(
            {"guest_id": b["guest_id"]}, {"$set": {"tailors_used": 2}})
        # SUM (not max): prevents quota splitting across merged sessions.
        assert identity.cluster_tailors_used(a["guest_id"]) == 5
        assert identity.cluster_tailors_used(b["guest_id"]) == 5


# ===========================================================================
# Quota enforcement — cluster lifetime limit + global circuit breaker
# ===========================================================================

class TestGuestQuotas:
    def test_cluster_quota_enforced(self, guest_db):
        s = _new_session()
        allowed, state = guest_service.check_guest_tailor_quota(s["guest_id"])
        assert allowed and state["tailors_limit"] == 5

        guest_db.guest_sessions.update_one(
            {"guest_id": s["guest_id"]}, {"$set": {"tailors_used": 5}})
        allowed, state = guest_service.check_guest_tailor_quota(s["guest_id"])
        assert not allowed and state["remaining"] == 0

    def test_cluster_quota_spans_merged_sessions(self, guest_db):
        raw = "one resume to rule them all " * 4
        a = _new_session()
        _attach(a["guest_id"], _structured(), raw)
        b = _new_session()
        _attach(b["guest_id"], _structured(), raw)
        guest_db.guest_sessions.update_one(
            {"guest_id": a["guest_id"]}, {"$set": {"tailors_used": 4}})
        guest_db.guest_sessions.update_one(
            {"guest_id": b["guest_id"]}, {"$set": {"tailors_used": 1}})
        allowed, _ = guest_service.check_guest_tailor_quota(b["guest_id"])
        assert not allowed  # 4 + 1 == limit 5 — no fresh session loophole

    def test_global_daily_circuit_breaker(self, guest_db):
        pconfig.update_guest_config({"global_daily_guest_tailors": 2}, "admin@test")
        assert guest_service.try_reserve_global_tailor() is True
        assert guest_service.try_reserve_global_tailor() is True
        assert guest_service.try_reserve_global_tailor() is False
        # Over-limit attempt rolled its $inc back — counter stays at the cap.
        doc = guest_db.guest_global_counters.find_one(
            {"_id": {"$regex": "^guest_tailors:"}})
        assert doc["count"] == 2
        # release (failed dispatch) frees a slot again
        guest_service.release_global_tailor()
        assert guest_service.try_reserve_global_tailor() is True

    def test_free_daily_tailors_flows_into_entitlements(self, guest_db):
        from services import entitlements_service
        assert entitlements_service.quota("free", "tailor") == 5  # default
        pconfig.update_guest_config({"free_daily_tailors": 7}, "admin@test")
        assert entitlements_service.quota("free", "tailor") == 7
        # premium plans unaffected by the guest knob
        assert entitlements_service.quota("premium", "tailor") == 30

    def test_quota_identical_when_platform_config_doc_absent(self, guest_db):
        # Prod state until an admin first writes the doc: existing users must
        # get byte-identical behavior to before Guest Onboarding existed.
        from services import entitlements_service
        assert guest_db.platform_config.find_one({"_id": "guest_onboarding"}) is None
        assert entitlements_service.quota("free", "tailor") == 5
        assert entitlements_service.quota("premium", "tailor") == 30
        assert entitlements_service.quota("free", "tailor") == \
            entitlements_service.PLAN_LIMITS["free"]["tailor"]


# ===========================================================================
# Claim — atomic ownership flip, idempotent usage seeding
# ===========================================================================

class TestClaim:
    def _seed_guest(self, guest_db, tailors_used=3):
        s = _new_session()
        _attach(s["guest_id"], _structured(), "resume content for claim " * 4)
        guest_db.guest_sessions.update_one(
            {"guest_id": s["guest_id"]}, {"$set": {"tailors_used": tailors_used}})
        guest_db.tailoring_records.insert_one({
            "record_id": "rec-1", "user_email": None, "guest_id": s["guest_id"],
            "tailored_resume": {"summary": "x"}, "source": "guest_tailor",
        })
        return s["guest_id"]

    def test_claim_transfers_and_seeds_usage_once(self, guest_db):
        gid = self._seed_guest(guest_db, tailors_used=3)
        email = "newuser@example.com"
        guest_db.users.insert_one({"email": email, "plan": "free"})

        first = guest_service.claim_session(gid, email)
        assert first["claimed"] and first["first_claim"]
        assert first["records_transferred"] == 1
        assert first["resume_copied"] is True
        assert first["carried_tailors"] == 3

        # DOUBLE CLAIM: no double count, no re-transfer.
        second = guest_service.claim_session(gid, email)
        assert second["claimed"] and second["first_claim"] is False
        assert second["records_transferred"] == 0
        assert second["carried_tailors"] == 0

        from services import usage_service
        usage = usage_service.get_usage(email)
        assert usage["used"] == 3  # seeded exactly ONCE

        rec = guest_db.tailoring_records.find_one({"record_id": "rec-1"})
        assert rec["user_email"] == email
        session = guest_db.guest_sessions.find_one({"guest_id": gid})
        assert session["claimed_by"] == email and session["claimed_at"]

    def test_claim_by_second_account_is_rejected(self, guest_db):
        gid = self._seed_guest(guest_db)
        assert guest_service.claim_session(gid, "first@example.com")["claimed"]
        res = guest_service.claim_session(gid, "second@example.com")
        assert res.get("error") == "already_claimed"

    def test_claim_does_not_overwrite_existing_resume(self, guest_db):
        gid = self._seed_guest(guest_db)
        email = "hasresume@example.com"
        from datetime import datetime, timezone
        guest_db.user_resumes.insert_one({
            "user_email": email, "structured": {"summary": "existing"},
            "parsed_at": datetime.now(timezone.utc),
        })
        res = guest_service.claim_session(gid, email)
        assert res["resume_copied"] is False
        doc = guest_db.user_resumes.find_one({"user_email": email})
        assert doc["structured"]["summary"] == "existing"


# ===========================================================================
# Admin config validation
# ===========================================================================

class TestAdminConfig:
    def test_validation_bounds(self, guest_db):
        clean, errors = pconfig.validate_guest_config_patch({
            "guest_tailor_limit": 10,
            "guest_jobs_min_score": 101,      # > max 100
            "guest_jobs_max": 0,              # < min 1
            "free_daily_tailors": "5",        # wrong type
            "enabled": "yes",                 # wrong type
            "nonsense_key": 1,                # unknown
            "retention_days": 30,
        })
        assert clean == {"guest_tailor_limit": 10, "retention_days": 30}
        assert set(errors) == {
            "guest_jobs_min_score", "guest_jobs_max", "free_daily_tailors",
            "enabled", "nonsense_key",
        }

    def test_bool_rejected_for_numeric_knob(self, guest_db):
        clean, errors = pconfig.validate_guest_config_patch({"guest_jobs_max": True})
        assert not clean and "guest_jobs_max" in errors

    def test_min_score_default_matches_scorer_range(self, guest_db):
        # jd_match_scorer.score_one lands ~20-55 on real resumes; the spec's
        # original 70 would blank the guest funnel. Locked at 40 — don't
        # "fix" it back up without re-tuning the scorer.
        assert pconfig.DEFAULTS["guest_jobs_min_score"] == 40

    def test_admin_route_put_and_get(self, guest_db):
        app = load_service_app("auth")
        from flask_jwt_extended import create_access_token
        with app.app_context():
            token = create_access_token(identity="harshith@aithrex.tech")
        client = app.test_client()
        headers = {"Authorization": f"Bearer {token}"}

        bad = client.put("/api/admin/guest-config", headers=headers,
                         json={"guest_jobs_min_score": -1})
        assert bad.status_code == 400
        assert bad.get_json()["error"] == "invalid_config"

        ok = client.put("/api/admin/guest-config", headers=headers,
                        json={"guest_jobs_min_score": 40, "guest_jobs_max": 10})
        assert ok.status_code == 200
        body = ok.get_json()
        assert body["guest_jobs_min_score"] == 40
        assert body["guest_jobs_max"] == 10
        assert body["updated_by"] == "harshith@aithrex.tech"
        assert body["updated_at"]

        got = client.get("/api/admin/guest-config", headers=headers)
        assert got.status_code == 200
        assert got.get_json()["guest_jobs_min_score"] == 40

    def test_admin_route_requires_admin(self, guest_db):
        app = load_service_app("auth")
        from flask_jwt_extended import create_access_token
        with app.app_context():
            token = create_access_token(identity="pleb@example.com")
        resp = app.test_client().get(
            "/api/admin/guest-config",
            headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 403  # never 401 for authorization failures


# ===========================================================================
# Route-level guest flow (resume Lambda app)
# ===========================================================================

@pytest.fixture()
def resume_app(guest_db, monkeypatch):
    # Never spawn the async worker thread in route tests (it would hit the
    # LLM boundary). Routes still return {job_id} and the job row exists.
    import services.resume_service as rs
    monkeypatch.setattr(rs.ResumeService, "invoke_async",
                        staticmethod(lambda *a, **k: None))
    return load_service_app("resume")


class TestGuestRoutes:
    def test_disabled_flag_returns_503(self, guest_db, monkeypatch, resume_app):
        monkeypatch.setenv("GUEST_ONBOARDING_ENABLED", "false")
        resp = resume_app.test_client().get("/api/resume/guest/session")
        assert resp.status_code == 503
        assert resp.get_json()["error"] == "guest_onboarding_disabled"

    def test_admin_kill_switch_returns_503(self, guest_db, resume_app):
        pconfig.update_guest_config({"enabled": False}, "admin@test")
        resp = resume_app.test_client().get("/api/resume/guest/session")
        assert resp.status_code == 503

    def test_onboard_returns_guest_and_job_ids(self, guest_db, resume_app):
        client = resume_app.test_client()
        resp = client.post(
            "/api/resume/guest/onboard",
            data={"file": (io.BytesIO(PDF_BYTES), "resume.pdf"),
                  "fingerprint_hash": "fp_route_test"},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 202, resp.get_data(as_text=True)
        body = resp.get_json()
        gid, jid = body["guest_id"], body["job_id"]
        assert guest_service.validate_guest_id(gid)

        # Capability scoping: right guest sees the job, wrong guest gets 404.
        ok = client.get(f"/api/resume/guest/job/{jid}?guest_id={gid}")
        assert ok.status_code == 200
        job = ok.get_json()
        # Same shape as the JWT /api/resume/job/<id> endpoint.
        assert {"job_id", "job_type", "status", "result", "error",
                "created_at", "completed_at"} <= set(job)
        assert "payload" not in job
        assert job["status"] == "processing"

        import uuid as _uuid
        wrong = client.get(f"/api/resume/guest/job/{jid}?guest_id={_uuid.uuid4()}")
        assert wrong.status_code == 404

    def test_onboard_rejects_bad_files(self, guest_db, resume_app):
        client = resume_app.test_client()
        resp = client.post(
            "/api/resume/guest/onboard",
            data={"file": (io.BytesIO(b"MZ not a pdf"), "resume.exe")},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 400

    def test_tailor_gates_quota_with_429(self, guest_db, resume_app):
        s = _new_session()
        _attach(s["guest_id"], _structured(), "resume text for tailor " * 4)
        guest_db.guest_sessions.update_one(
            {"guest_id": s["guest_id"]}, {"$set": {"tailors_used": 5}})
        resp = resume_app.test_client().post(
            "/api/resume/guest/tailor",
            json={"guest_id": s["guest_id"], "job": {"title": "SWE", "jd_text": "Python APIs"}},
        )
        assert resp.status_code == 429
        body = resp.get_json()
        assert body["quota_exhausted"] is True  # FE renders signup CTA, not error

    def test_tailor_happy_path_charges_quota(self, guest_db, resume_app):
        s = _new_session()
        _attach(s["guest_id"], _structured(), "resume text for tailor " * 4)
        resp = resume_app.test_client().post(
            "/api/resume/guest/tailor",
            json={"guest_id": s["guest_id"],
                  "job": {"title": "SWE", "company": "Acme", "jd_text": "Python APIs " * 10}},
        )
        assert resp.status_code == 202, resp.get_data(as_text=True)
        body = resp.get_json()
        assert body["job_id"] and body["tailors_used"] == 1 and body["tailors_limit"] == 5
        # Global circuit breaker reserved exactly one slot.
        counter = guest_db.guest_global_counters.find_one(
            {"_id": {"$regex": "^guest_tailors:"}})
        assert counter["count"] == 1

    def test_tailor_global_breaker_returns_429(self, guest_db, resume_app):
        pconfig.update_guest_config({"global_daily_guest_tailors": 0}, "admin@test")
        s = _new_session()
        _attach(s["guest_id"], _structured(), "resume text " * 5)
        resp = resume_app.test_client().post(
            "/api/resume/guest/tailor",
            json={"guest_id": s["guest_id"], "job": {"jd_text": "Python"}},
        )
        assert resp.status_code == 429
        body = resp.get_json()
        assert body["quota_exhausted"] is True and body.get("global") is True

    def test_session_recognizes_fingerprint(self, guest_db, resume_app):
        s = _new_session(fp="fp_return_visitor")
        _attach(s["guest_id"], _structured(), "text " * 20, fp="fp_return_visitor")
        client = resume_app.test_client()

        known = client.get(f"/api/resume/guest/session?guest_id={s['guest_id']}")
        assert known.status_code == 200
        body = known.get_json()
        assert body["exists"] and body["has_resume"] and body["tailors_limit"] == 5

        # guest_id lost (new browser storage) but fingerprint matches: the
        # single-signal (fingerprint-only) hit recognizes WITHOUT the name —
        # a guessable fingerprint must never be a name-disclosure oracle.
        recog = client.get(
            "/api/resume/guest/session?fingerprint_hash=fp_return_visitor")
        assert recog.status_code == 200
        rbody = recog.get_json()
        assert rbody["exists"] is False and rbody["recognized"] is True
        assert rbody["recognized_name"] == ""

        # The guest_id capability holder (2+ signals per the spec rule) still
        # gets the name.
        assert known.get_json()["recognized_name"] == "jordan alvarez"

    def test_claim_route_requires_jwt(self, guest_db, resume_app):
        resp = resume_app.test_client().post(
            "/api/resume/guest/claim", json={"guest_id": "x"})
        assert resp.status_code == 401  # the ONLY legitimate 401 (missing JWT)

    def test_claim_route_happy_path(self, guest_db, resume_app):
        s = _new_session()
        _attach(s["guest_id"], _structured(), "claimable resume " * 5)
        from flask_jwt_extended import create_access_token
        with resume_app.app_context():
            token = create_access_token(identity="claimer@example.com")
        resp = resume_app.test_client().post(
            "/api/resume/guest/claim",
            headers={"Authorization": f"Bearer {token}"},
            json={"guest_id": s["guest_id"]},
        )
        assert resp.status_code == 200
        assert resp.get_json()["claimed"] is True


# ===========================================================================
# Guest parse job (async worker branch) + matches computation
# ===========================================================================

class TestGuestJobs:
    def test_guest_parse_job_updates_session(self, guest_db, monkeypatch):
        import services.resume_service as rs
        s = _new_session(fp="fp_parse")
        payload = {
            "file_base64": "ignored", "mime_type": "application/pdf",
            "filename": "resume.pdf", "guest_id": s["guest_id"],
            "fingerprint_hash": "fp_parse",
        }
        job_id = guest_service.create_guest_job("guest_parse", payload, s["guest_id"])
        svc = rs.get_resume_service()
        monkeypatch.setattr(
            svc.parser, "parse_to_structured",
            lambda *a, **k: (_structured(), "Jordan Alvarez raw resume text " * 5),
        )
        rs._process_job(job_id, "guest_parse", payload)

        job = guest_service.get_guest_job(job_id, s["guest_id"])
        assert job["status"] == "completed"
        assert job["result"]["parsed_resume"]["structured"]["contact"]["name"] == "Jordan Alvarez"
        assert job["result"]["guest"]["tailors_limit"] == 5

        session = guest_db.guest_sessions.find_one({"guest_id": s["guest_id"]})
        assert session["resume"]["structured"]
        assert session["identity"]["emails"] == ["jordan.alvarez@example.com"]

    def test_compute_matches_filters_caps_and_caches(self, guest_db, monkeypatch):
        pconfig.update_guest_config(
            {"guest_jobs_min_score": 20, "guest_jobs_max": 2}, "admin@test")
        s = _new_session()
        _attach(s["guest_id"],
                _structured(skills=["Python", "Go", "SQL", "AWS"]),
                "match me " * 10)

        rows = [
            {"title": "Python Backend Engineer", "company": "A", "location": "US",
             "apply_link": "https://a.example/1", "source": "Greenhouse",
             "description": "Python Go SQL AWS backend"},
            {"title": "Go Platform Engineer", "company": "B", "location": "US",
             "apply_link": "https://b.example/2", "source": "Workday Direct",
             "description": "Go SQL AWS"},
            {"title": "Python Data Engineer", "company": "C", "location": "US",
             "apply_link": "https://c.example/3", "source": "Lever",
             "description": "Python SQL"},
            {"title": "Marketing Manager", "company": "D", "location": "US",
             "apply_link": "https://d.example/4", "source": "Ashby",
             "description": "brand campaigns"},
        ]
        monkeypatch.setattr(guest_service, "_fetch_free_jobs", lambda structured: rows)

        result = guest_service.compute_guest_matches(s["guest_id"])
        assert result["cached"] is False
        assert len(result["jobs"]) == 2  # capped at guest_jobs_max
        scores = [j["match_score"] for j in result["jobs"]]
        assert scores == sorted(scores, reverse=True)
        assert all(j["match_score"] >= 20 for j in result["jobs"])
        assert all("description" not in j for j in result["jobs"])  # card whitelist

        cached = guest_service.get_cached_matches(s["guest_id"])
        assert cached and cached["cached"] is True
        assert len(cached["jobs"]) == 2

    def test_jobs_route_returns_cache_or_job_id(self, guest_db, monkeypatch):
        import services.resume_service as rs
        monkeypatch.setattr(rs.ResumeService, "invoke_async",
                            staticmethod(lambda *a, **k: None))
        app = load_service_app("jobs")
        client = app.test_client()

        s = _new_session()
        _attach(s["guest_id"], _structured(), "resume for matches " * 5)

        first = client.post("/api/jobs/guest/matches", json={"guest_id": s["guest_id"]})
        assert first.status_code == 202
        assert first.get_json()["job_id"]

        # Same-day cache present -> synchronous 200 with cached rows.
        guest_service._store_matches(s["guest_id"], [{"title": "T", "match_score": 90}], 20)
        second = client.post("/api/jobs/guest/matches", json={"guest_id": s["guest_id"]})
        assert second.status_code == 200
        body = second.get_json()
        assert body["cached"] is True and body["jobs"][0]["match_score"] == 90

    def test_jobs_route_requires_resume(self, guest_db):
        app = load_service_app("jobs")
        s = _new_session()
        resp = app.test_client().post(
            "/api/jobs/guest/matches", json={"guest_id": s["guest_id"]})
        assert resp.status_code == 400

    def test_jobs_route_validates_guest_id(self, guest_db):
        app = load_service_app("jobs")
        resp = app.test_client().post(
            "/api/jobs/guest/matches",
            json={"guest_id": {"$ne": None}})  # NoSQL-injection shape
        assert resp.status_code == 400


# ===========================================================================
# get_client_ip — rightmost-XFF / spoof resistance (security review §1)
# ===========================================================================

class _FakeHeaders:
    def __init__(self, xff):
        self._xff = list(xff or [])

    def getlist(self, name):
        return self._xff if name == "X-Forwarded-For" else []


class _FakeRequest:
    """Minimal request stub: headers.getlist + remote_addr + WSGI environ."""

    def __init__(self, xff=None, remote_addr="", source_ip=None, v1_source_ip=None):
        self.headers = _FakeHeaders(xff)
        self.remote_addr = remote_addr
        self.environ = {}
        if source_ip:
            self.environ["apig_wsgi.request_context"] = {"http": {"sourceIp": source_ip}}
        elif v1_source_ip:
            self.environ["apig_wsgi.request_context"] = {"identity": {"sourceIp": v1_source_ip}}


class TestClientIp:
    @pytest.fixture(autouse=True)
    def _default_hops(self, monkeypatch):
        monkeypatch.delenv("TRUSTED_PROXY_HOPS", raising=False)

    def test_normal_single_hop_request_unchanged(self):
        # No X-Forwarded-For (local dev / tests / direct calls): identical to
        # the old behavior — remote_addr. Existing logged-in throttling keys
        # do not move.
        from utils.security import get_client_ip
        assert get_client_ip(_FakeRequest(remote_addr="203.0.113.7")) == "203.0.113.7"

    def test_prod_cloudfront_chain_normal_user(self):
        # client -> CloudFront (appends client) -> API GW (appends CF edge).
        # Same key the old leftmost parse produced for a non-spoofing user.
        from utils.security import get_client_ip
        req = _FakeRequest(
            xff=["203.0.113.7", "130.176.5.10"], source_ip="130.176.5.10"
        )
        assert get_client_ip(req) == "203.0.113.7"

    def test_spoofed_xff_prefix_is_ignored(self):
        # Attacker sends X-Forwarded-For: 9.9.9.9 — old code keyed the rate
        # limiter on the attacker-chosen value; now the real client wins.
        from utils.security import get_client_ip
        req = _FakeRequest(
            xff=["9.9.9.9", "203.0.113.7", "130.176.5.10"],
            source_ip="130.176.5.10",
        )
        assert get_client_ip(req) == "203.0.113.7"

    def test_direct_api_gateway_normal_user(self):
        # No CloudFront: API GW appends its peer (the client) to XFF and the
        # request-context sourceIp IS the client.
        from utils.security import get_client_ip
        req = _FakeRequest(xff=["203.0.113.7"], source_ip="203.0.113.7")
        assert get_client_ip(req) == "203.0.113.7"

    def test_v1_identity_source_ip_supported(self):
        from utils.security import get_client_ip
        req = _FakeRequest(xff=["203.0.113.7"], v1_source_ip="203.0.113.7")
        assert get_client_ip(req) == "203.0.113.7"

    def test_hops_zero_trusts_only_connection_peer(self, monkeypatch):
        monkeypatch.setenv("TRUSTED_PROXY_HOPS", "0")
        from utils.security import get_client_ip
        req = _FakeRequest(xff=["9.9.9.9"], source_ip="203.0.113.7")
        assert get_client_ip(req) == "203.0.113.7"

    def test_rightmost_xff_without_apig_context(self):
        # Local/test-server topology: rightmost XFF entry wins, junk on the
        # left is never honored.
        from utils.security import get_client_ip
        req = _FakeRequest(xff=["9.9.9.9", "198.51.100.4"], remote_addr="127.0.0.1")
        assert get_client_ip(req) == "198.51.100.4"

    def test_port_decorated_token_normalized(self):
        from utils.security import get_client_ip
        req = _FakeRequest(xff=["9.9.9.9", "203.0.113.7:4711"], remote_addr="127.0.0.1")
        assert get_client_ip(req) == "203.0.113.7"

    def test_invalid_trusted_token_falls_back_to_peer_not_left(self):
        # The entry at the trusted position isn't an IP: NEVER walk left into
        # attacker-controlled territory — fall back to the connection peer.
        from utils.security import get_client_ip
        req = _FakeRequest(xff=["9.9.9.9", "not-an-ip"], remote_addr="127.0.0.1")
        assert get_client_ip(req) == "127.0.0.1"

    def test_logged_in_throttling_not_tightened(self):
        # A normal user hammering under the limit stays un-throttled and two
        # distinct clients keep independent buckets (keying shape unchanged).
        from utils.security import RateLimiter, get_client_ip
        limiter = RateLimiter()
        ip_a = get_client_ip(_FakeRequest(remote_addr="203.0.113.7"))
        ip_b = get_client_ip(
            _FakeRequest(xff=["198.51.100.4", "130.176.5.10"], source_ip="130.176.5.10")
        )
        assert ip_a == "203.0.113.7" and ip_b == "198.51.100.4"
        for _ in range(9):
            assert limiter.is_rate_limited(f"auth:login:{ip_a}", max_requests=10) is False
        assert limiter.is_rate_limited(f"auth:login:{ip_b}", max_requests=10) is False


# ===========================================================================
# ip_hash salt (security review §5) — fail closed, never a reversible hash
# ===========================================================================

class TestIpHashSalt:
    def test_hash_ip_is_none_when_salt_unset(self, guest_db, monkeypatch):
        monkeypatch.delenv("GUEST_IP_HASH_SALT", raising=False)
        assert guest_service.hash_ip("203.0.113.7") is None
        s = guest_service.create_or_get_session(
            None, None, guest_service.hash_ip("203.0.113.7"))
        doc = guest_db.guest_sessions.find_one({"guest_id": s["guest_id"]})
        assert doc["ip_hash"] is None  # stored null — nothing brute-forceable

    def test_hash_ip_hashes_with_salt(self, guest_db, monkeypatch):
        monkeypatch.setenv("GUEST_IP_HASH_SALT", "unit-test-salt")
        h = guest_service.hash_ip("203.0.113.7")
        assert h and len(h) == 64 and "203.0.113.7" not in h
        assert h != guest_service.hash_ip("198.51.100.4")


# ===========================================================================
# Atomic tailor charge (security review §3) — no check-then-inc TOCTOU
# ===========================================================================

class TestAtomicTailorCharge:
    def test_sixth_charge_fails_and_counter_stays_at_cap(self, guest_db):
        s = _new_session()
        for i in range(5):
            charged, state = guest_service.try_charge_guest_tailor(s["guest_id"])
            assert charged is True and state["tailors_used"] == i + 1
        charged, state = guest_service.try_charge_guest_tailor(s["guest_id"])
        assert charged is False
        assert state["tailors_used"] == 5 and state["remaining"] == 0
        doc = guest_db.guest_sessions.find_one({"guest_id": s["guest_id"]})
        assert doc["tailors_used"] == 5  # over-limit attempt rolled back

    def test_charge_verifies_cluster_sum(self, guest_db):
        raw = "one resume shared across sessions " * 4
        a = _new_session()
        _attach(a["guest_id"], _structured(), raw)
        b = _new_session()
        _attach(b["guest_id"], _structured(), raw)  # merged (email+phone+hash)
        guest_db.guest_sessions.update_one(
            {"guest_id": a["guest_id"]}, {"$set": {"tailors_used": 4}})

        charged, _ = guest_service.try_charge_guest_tailor(b["guest_id"])
        assert charged is True  # 4 + 1 == limit 5
        charged, _ = guest_service.try_charge_guest_tailor(b["guest_id"])
        assert charged is False  # would be 6 — refused
        b_doc = guest_db.guest_sessions.find_one({"guest_id": b["guest_id"]})
        assert b_doc["tailors_used"] == 1  # the failed charge rolled back

    def test_unknown_session_charge_fails_cleanly(self, guest_db):
        import uuid as _uuid
        charged, state = guest_service.try_charge_guest_tailor(str(_uuid.uuid4()))
        assert charged is False and state["remaining"] == 0
        assert guest_db.guest_sessions.count_documents({}) == 0


# ===========================================================================
# Global daily caps: parse (onboard) + matches (security review §1/§6)
# ===========================================================================

class TestGlobalCapacityCaps:
    def _post_onboard(self, client):
        return client.post(
            "/api/resume/guest/onboard",
            data={"file": (io.BytesIO(PDF_BYTES), "resume.pdf"),
                  "fingerprint_hash": "fp_cap_test"},
            content_type="multipart/form-data",
        )

    def test_onboard_global_parse_cap(self, guest_db, resume_app):
        pconfig.update_guest_config({"global_daily_guest_parses": 1}, "admin@test")
        client = resume_app.test_client()
        assert self._post_onboard(client).status_code == 202
        over = self._post_onboard(client)
        assert over.status_code == 429
        body = over.get_json()
        assert body["error"] == "capacity" and body["global"] is True
        assert body["message"]
        counter = guest_db.guest_global_counters.find_one(
            {"_id": {"$regex": "^guest_parses:"}})
        assert counter["count"] == 1  # over-limit attempt rolled back

    def test_onboard_dispatch_failure_releases_reservation(
            self, guest_db, resume_app, monkeypatch):
        import services.resume_service as rs

        def boom(*a, **k):
            raise RuntimeError("lambda invoke failed")

        monkeypatch.setattr(rs.ResumeService, "invoke_async", staticmethod(boom))
        resp = self._post_onboard(resume_app.test_client())
        assert resp.status_code == 500
        counter = guest_db.guest_global_counters.find_one(
            {"_id": {"$regex": "^guest_parses:"}})
        assert (counter or {}).get("count", 0) == 0  # slot freed

    def test_matches_global_cap_and_cache_exemption(self, guest_db, monkeypatch):
        import services.resume_service as rs
        monkeypatch.setattr(rs.ResumeService, "invoke_async",
                            staticmethod(lambda *a, **k: None))
        from _vg_support import load_service_app
        app = load_service_app("jobs")
        client = app.test_client()

        pconfig.update_guest_config({"global_daily_guest_matches": 1}, "admin@test")
        a = _new_session()
        _attach(a["guest_id"], _structured(), "matches cap resume A " * 5)
        b = _new_session(fp="fp_other")
        _attach(b["guest_id"],
                _structured(name="Sam Lee", email="sam.lee@example.com",
                            phone="(777) 555-0000"),
                "matches cap resume B totally different " * 5, fp="fp_other")

        first = client.post("/api/jobs/guest/matches", json={"guest_id": a["guest_id"]})
        assert first.status_code == 202  # cache miss consumes the 1 slot

        over = client.post("/api/jobs/guest/matches", json={"guest_id": b["guest_id"]})
        assert over.status_code == 429
        body = over.get_json()
        assert body["error"] == "capacity" and body["global"] is True

        # Same-day cache HIT must serve even with the budget exhausted —
        # a cache hit never consumes the global scrape budget.
        guest_service._store_matches(
            a["guest_id"], [{"title": "T", "match_score": 90}], 20)
        hit = client.post("/api/jobs/guest/matches", json={"guest_id": a["guest_id"]})
        assert hit.status_code == 200 and hit.get_json()["cached"] is True

        counter = guest_db.guest_global_counters.find_one(
            {"_id": {"$regex": "^guest_matches:"}})
        assert counter["count"] == 1


# ===========================================================================
# Claim race (security review §7) — the loser transfers NOTHING
# ===========================================================================

class TestClaimRace:
    def test_losing_concurrent_claim_transfers_nothing(self, guest_db, monkeypatch):
        s = _new_session()
        _attach(s["guest_id"], _structured(), "raced claim resume " * 5)
        gid = s["guest_id"]
        guest_db.guest_sessions.update_one(
            {"guest_id": gid}, {"$set": {"tailors_used": 2}})
        guest_db.tailoring_records.insert_one({
            "record_id": "rec-race", "user_email": None, "guest_id": gid,
            "tailored_resume": {"summary": "x"}, "source": "guest_tailor",
        })
        winner, loser = "winner@example.com", "loser@example.com"
        # The winner's flip has already landed...
        from datetime import datetime, timezone
        guest_db.guest_sessions.update_one(
            {"guest_id": gid},
            {"$set": {"claimed_by": winner,
                      "claimed_at": datetime.now(timezone.utc)}})

        # ...but the loser's pre-check read a STALE (unclaimed) doc — the
        # classic interleaving. Its atomic flip must lose and it must not
        # touch records/resume/usage.
        real_coll = guest_db.guest_sessions

        class StaleFirstRead:
            def __init__(self, real):
                self._real = real
                self._first = True

            def find_one(self, *a, **k):
                doc = self._real.find_one(*a, **k)
                if self._first and doc is not None:
                    self._first = False
                    doc = dict(doc)
                    doc["claimed_by"] = None  # stale pre-check view
                return doc

            def __getattr__(self, name):
                return getattr(self._real, name)

        proxy = StaleFirstRead(real_coll)
        monkeypatch.setattr(guest_service, "_sessions", lambda: proxy)

        res = guest_service.claim_session(gid, loser)
        assert res.get("error") == "already_claimed"

        rec = guest_db.tailoring_records.find_one({"record_id": "rec-race"})
        assert rec["user_email"] is None                      # no PII transfer
        assert guest_db.user_resumes.find_one({"user_email": loser}) is None
        from services import usage_service
        assert usage_service.get_usage(loser)["used"] == 0
        session = guest_db.guest_sessions.find_one({"guest_id": gid})
        assert session["claimed_by"] == winner                # winner keeps it

    def test_repeat_claim_same_account_no_recopy(self, guest_db):
        # Idempotency: the same account claiming twice re-copies nothing.
        s = _new_session()
        _attach(s["guest_id"], _structured(), "repeat claim resume " * 5)
        email = "repeat@example.com"
        first = guest_service.claim_session(s["guest_id"], email)
        assert first["first_claim"] is True and first["resume_copied"] is True
        before = guest_db.user_resumes.count_documents({"user_email": email})

        second = guest_service.claim_session(s["guest_id"], email)
        assert second["claimed"] is True and second["first_claim"] is False
        assert second["resume_copied"] is False and second["records_transferred"] == 0
        assert guest_db.user_resumes.count_documents({"user_email": email}) == before


# ===========================================================================
# Job-card link scheme guard (security review §8)
# ===========================================================================

class TestJobCardUrlScheme:
    def test_javascript_and_data_urls_never_reach_cards(self, guest_db, monkeypatch):
        pconfig.update_guest_config(
            {"guest_jobs_min_score": 0, "guest_jobs_max": 10}, "admin@test")
        s = _new_session()
        _attach(s["guest_id"],
                _structured(skills=["Python", "Go", "SQL", "AWS"]),
                "scheme guard resume " * 5)

        rows = [
            {"title": "Python Engineer", "company": "A", "location": "US",
             "apply_link": "javascript:alert(document.cookie)",
             "url": "https://ok.example/apply/1",
             "source": "Greenhouse", "description": "Python Go SQL AWS"},
            {"title": "Go Engineer", "company": "B", "location": "US",
             "apply_link": "https://good.example/2",
             "source": "Lever", "description": "Go SQL AWS"},
            {"title": "SQL Engineer", "company": "C", "location": "US",
             "apply_link": "data:text/html,<script>alert(1)</script>",
             "url": "vbscript:msgbox(1)",
             "source": "Ashby", "description": "SQL Python"},
        ]
        monkeypatch.setattr(guest_service, "_fetch_free_jobs", lambda structured: rows)
        result = guest_service.compute_guest_matches(s["guest_id"])
        cards = {c["title"]: c for c in result["jobs"]}

        assert "apply_link" not in cards["Python Engineer"]        # javascript: dropped
        assert cards["Python Engineer"]["url"] == "https://ok.example/apply/1"
        assert cards["Go Engineer"]["apply_link"] == "https://good.example/2"
        assert "apply_link" not in cards["SQL Engineer"]           # data: dropped
        assert "url" not in cards["SQL Engineer"]                  # vbscript: dropped


if __name__ == "__main__":  # allow `python tests/test_guest_onboarding.py`
    sys.exit(pytest.main([__file__, "-q"]))
