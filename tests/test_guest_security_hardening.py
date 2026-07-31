"""Security hardening for the guest surface - offline suite.

Four confirmed weaknesses, one test class each:

1. DISTRIBUTED RATE LIMITING. The old limiter kept state in a per-container
   dict, so with N warm Lambda containers the real ceiling was N x limit and
   "10 uploads per hour per client" was never enforced. These tests run TWO
   limiter instances against ONE Mongo - the stand-in for two containers - and
   pin that the shared count is authoritative, plus the deliberate
   fail-closed (expensive) / fail-open (cheap read) split.

2. CHALLENGE VERIFICATION. There used to be no verify function at all: a 429
   challenge_required was unclearable theatre. These tests pin the redemption
   path - replay, expiry, subject binding, counter clearing - and that an
   unsatisfiable provider is never presented.

3. GUEST CAPABILITY OUT OF QUERY STRINGS. guest_id is a bearer capability; in
   a URL it lands in edge logs, history and Referer. These tests pin the
   cookie/header transports, the preserved generic-404 (no existence oracle)
   on cross-session access, and Origin/CSRF verification on cookie-authorized
   writes.

4. JSEARCH COST CHOKEPOINT. The metered aggregator must refuse guest and
   unauthenticated context at _search_jsearch_raw itself, not only at the
   mnc_jobs_service caller.

Fully offline: mongomock via tests/_vg_support, no network, no Bedrock.
"""
import io
import uuid as _uuid
from datetime import datetime, timedelta, timezone

import pytest

from _vg_support import load_service_app

from services import guest_challenge_service as challenge
from services import guest_service
from utils import db_connect
from utils.security import DistributedRateLimiter, check_rate_limit, get_rate_limiter

COLLECTIONS = (
    "guest_sessions", "guest_quota_subjects", "guest_global_counters",
    "guest_challenge_velocity", "guest_challenges", "rate_limit_buckets",
    "job_match_snapshots", "platform_config", "tailoring_records",
    "resume_jobs", "resume_assets", "user_resumes", "users",
)

PDF_BYTES = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n"
ALLOWED_ORIGIN = "http://localhost:5173"


@pytest.fixture(autouse=True)
def _secrets(monkeypatch):
    monkeypatch.setenv("GUEST_SIGNAL_HMAC_KEY", "unit-test-signal-key")
    monkeypatch.setenv("GUEST_DOWNLOAD_TOKEN_SECRET", "unit-test-download-secret")
    monkeypatch.setenv("GUEST_IP_HASH_SALT", "unit-test-ip-salt")
    monkeypatch.delenv("GUEST_CHALLENGE_PROVIDER", raising=False)
    monkeypatch.delenv("GUEST_CHALLENGE_SECRET", raising=False)


@pytest.fixture()
def db():
    database = db_connect.DBConnect().get_db()
    for c in COLLECTIONS:
        database[c].delete_many({})
    from services import platform_config_service as pconfig

    pconfig.invalidate_cache()
    get_rate_limiter()._requests.clear()
    yield database
    pconfig.invalidate_cache()
    get_rate_limiter()._requests.clear()


@pytest.fixture()
def resume_app():
    return load_service_app("resume")


def _structured():
    return {
        "contact": {"name": "Jordan Alvarez", "email": "j@example.com",
                    "phone": "", "linkedin": "", "github": ""},
        "summary": "Backend engineer building APIs.",
        "skills": {"languages": ["Python", "Go", "SQL"]},
        "experience": [{"title": "Backend Engineer", "company": "Acme",
                        "dates": "2020 - 2024",
                        "bullets": ["Built APIs in Python"]}],
        "education": [], "projects": [], "certifications": [],
    }


def _session_with_resume():
    session = guest_service.create_or_get_session(None, None, "iphash")
    guest_service.attach_parsed_resume(
        session["guest_id"], _structured(), "python go sql backend " * 10,
        "resume.pdf",
    )
    return session["guest_id"]


def _serve_one_match(guest_id):
    mid = _uuid.uuid4().hex
    jobs = [{
        "match_id": mid, "title": "Backend Role", "company": "Acme",
        "url": "https://jobs.example/1", "source": "Greenhouse", "days_ago": 1,
        "match_score": 80, "matched_skills": ["python"],
        "why": "Estimated match because this posting mentions python.",
        "sponsorship": None,
    }]
    sources = {mid: {"title": "Backend Role", "company": "Acme",
                     "jd_text": "Python APIs backend services " * 20,
                     "url": "https://jobs.example/1"}}
    guest_service._store_matches(guest_id, jobs, 20, jd_sources=sources)
    return mid


# ===========================================================================
# 1. Distributed rate limiting
# ===========================================================================
class TestDistributedRateLimiting:
    def test_limit_holds_across_simulated_containers(self, db):
        """Two limiter instances = two Lambda containers sharing one Mongo.
        The 4th request must be rejected NO MATTER which container serves it."""
        container_a = DistributedRateLimiter()
        container_b = DistributedRateLimiter()
        key = "guest:onboard:198.51.100.9"

        # 3 allowed, alternating containers - neither has local state.
        assert container_a.is_rate_limited(key, 3, 3600) is False
        assert container_b.is_rate_limited(key, 3, 3600) is False
        assert container_a.is_rate_limited(key, 3, 3600) is False
        # The 4th trips on a container that has only ever seen ONE request.
        assert container_b.is_rate_limited(key, 3, 3600) is True

    def test_in_process_limiter_alone_would_have_leaked(self, db):
        """The regression this closes: per-container dicts do not share, so
        N containers multiply the effective limit by N."""
        from utils.security import RateLimiter

        key = "guest:onboard:198.51.100.10"
        containers = [RateLimiter() for _ in range(4)]
        # Every container independently grants the full allowance of 1.
        assert [c.is_rate_limited(key, 1, 3600) for c in containers] == [False] * 4
        # The distributed limiter grants it exactly once, in total.
        shared = [DistributedRateLimiter().is_rate_limited(key, 1, 3600)
                  for _ in range(4)]
        assert shared == [False, True, True, True]

    def test_no_raw_client_key_is_stored(self, db):
        DistributedRateLimiter().is_rate_limited("guest:onboard:203.0.113.44", 5, 60)
        docs = list(db.rate_limit_buckets.find({}))
        assert len(docs) == 1
        assert "203.0.113.44" not in str(docs[0])
        assert docs[0]["bucket"] == "guest:onboard"   # non-PII ops label only
        assert isinstance(docs[0]["expires_at"], datetime)  # TTL reaping

    def test_windows_are_independent(self, db, monkeypatch):
        """A new fixed window starts on a fresh document, so the count resets
        without any sweep. Patch the module-local ``time`` reference only."""
        import time as real_time
        import types

        import utils.security as security

        clock = {"now": real_time.time()}
        monkeypatch.setattr(
            security, "time", types.SimpleNamespace(time=lambda: clock["now"]))

        key = "guest:onboard:198.51.100.11"
        limiter = DistributedRateLimiter()
        assert limiter.is_rate_limited(key, 1, 60) is False
        assert limiter.is_rate_limited(key, 1, 60) is True
        clock["now"] += 60
        assert limiter.is_rate_limited(key, 1, 60) is False  # fresh window
        assert db.rate_limit_buckets.count_documents({}) == 2

    def test_storage_error_fails_closed_for_expensive_endpoints(self, monkeypatch):
        limiter = DistributedRateLimiter()
        monkeypatch.setattr(
            limiter, "_collection",
            lambda: (_ for _ in ()).throw(RuntimeError("mongo down")))
        assert limiter.is_rate_limited("guest:onboard:ip", 10, 3600) is True

    def test_storage_error_fails_open_for_cheap_reads(self, monkeypatch):
        """A Mongo blip must not break every in-flight guest session's poll."""
        limiter = DistributedRateLimiter()
        monkeypatch.setattr(
            limiter, "_collection",
            lambda: (_ for _ in ()).throw(RuntimeError("mongo down")))
        assert limiter.is_rate_limited(
            "guest:poll:ip", 600, 300, fail_open=True) is False

    def test_check_rate_limit_short_circuit_can_only_reject(self, db):
        """The cheap in-process first pass may reject early but must never
        grant a request the distributed counter has not seen."""
        key = "guest:tailor:198.51.100.12"
        assert check_rate_limit(key, 2, 3600) is False
        assert check_rate_limit(key, 2, 3600) is False
        assert check_rate_limit(key, 2, 3600) is True
        # Every admitted request was counted in the SHARED store.
        assert db.rate_limit_buckets.count_documents({}) == 1

    def test_guest_onboard_route_is_distributed(self, db, resume_app):
        """End to end: the route limit now lives in Mongo, so a second
        container (a cleared in-process limiter) still sees the count."""
        client = resume_app.test_client()

        def onboard():
            return client.post(
                "/api/resume/guest/onboard",
                data={"file": (io.BytesIO(PDF_BYTES), "resume.pdf")},
                content_type="multipart/form-data",
            )

        for _ in range(10):
            assert onboard().status_code == 202
        # Simulate the request landing on a COLD container: local state gone,
        # shared state intact. The old code let this through.
        get_rate_limiter()._requests.clear()
        assert onboard().status_code == 429


# ===========================================================================
# 2. Challenge issue + verify
# ===========================================================================
@pytest.fixture()
def armed(monkeypatch):
    """A configured, satisfiable provider whose verifier accepts only 'solved'."""
    calls = []

    def _verifier(solution, remote_ip, secret):
        calls.append({"solution": solution, "remote_ip": remote_ip, "secret": secret})
        return solution == "solved"

    challenge.register_verifier("testcaptcha", _verifier)
    monkeypatch.setenv("GUEST_CHALLENGE_PROVIDER", "testcaptcha")
    monkeypatch.setenv("GUEST_CHALLENGE_SECRET", "unit-test-challenge-secret")
    monkeypatch.setenv("GUEST_CHALLENGE_THRESHOLD", "1")
    yield calls
    challenge._VERIFIERS.pop("testcaptcha", None)


class TestChallengeVerification:
    def _trip(self, ip_hash="iphash-a", subject_id=None, kind="onboard"):
        assert challenge.maybe_challenge(kind, ip_hash, subject_id) is None
        issued = challenge.maybe_challenge(kind, ip_hash, subject_id)
        assert issued and issued["challenge_id"]
        return issued["challenge_id"]

    def test_solved_challenge_verifies_once_and_clears_the_counter(self, db, armed):
        cid = self._trip()
        key = challenge._velocity_key("onboard", "iphash-a")
        assert db.guest_challenge_velocity.find_one({"_id": key})["count"] == 2

        assert challenge.verify_challenge(cid, "solved", "iphash-a")["ok"] is True
        # The counter that produced the trip is reset, so the retry proceeds.
        assert db.guest_challenge_velocity.find_one({"_id": key})["count"] == 0
        assert challenge.maybe_challenge("onboard", "iphash-a") is None
        # The provider actually saw the solution and the secret.
        assert armed[-1]["solution"] == "solved"
        assert armed[-1]["secret"] == "unit-test-challenge-secret"

    def test_replay_is_rejected(self, db, armed):
        cid = self._trip()
        assert challenge.verify_challenge(cid, "solved", "iphash-a")["ok"] is True
        again = challenge.verify_challenge(cid, "solved", "iphash-a")
        assert again["ok"] is False
        assert again["error"] == challenge.ERR_USED

    def test_expired_challenge_is_rejected(self, db, armed):
        cid = self._trip()
        db.guest_challenges.update_one(
            {"_id": cid},
            {"$set": {"expires_at": datetime.now(timezone.utc) - timedelta(seconds=1)}},
        )
        result = challenge.verify_challenge(cid, "solved", "iphash-a")
        assert result["ok"] is False
        assert result["error"] == challenge.ERR_EXPIRED
        # An expired challenge is NOT consumed and clears nothing.
        assert db.guest_challenges.find_one({"_id": cid})["used_at"] is None

    def test_challenge_bound_to_another_subject_is_rejected(self, db, armed):
        cid = self._trip(ip_hash="iphash-victim")
        stolen = challenge.verify_challenge(cid, "solved", "iphash-attacker")
        assert stolen["ok"] is False
        assert stolen["error"] == challenge.ERR_SUBJECT_MISMATCH
        # Crucially the victim's challenge was NOT burned by the attempt.
        assert db.guest_challenges.find_one({"_id": cid})["used_at"] is None
        assert challenge.verify_challenge(cid, "solved", "iphash-victim")["ok"] is True

    def test_quota_subject_binding_is_enforced(self, db, armed):
        cid = self._trip(ip_hash="iphash-s", subject_id="subject-a", kind="tailor")
        wrong = challenge.verify_challenge(cid, "solved", "iphash-s", "subject-b")
        assert wrong["error"] == challenge.ERR_SUBJECT_MISMATCH
        assert challenge.verify_challenge(
            cid, "solved", "iphash-s", "subject-a")["ok"] is True

    def test_wrong_solution_is_rejected_and_not_consumed(self, db, armed):
        cid = self._trip()
        bad = challenge.verify_challenge(cid, "not-the-answer", "iphash-a")
        assert bad["error"] == challenge.ERR_SOLUTION_REJECTED
        assert db.guest_challenges.find_one({"_id": cid})["used_at"] is None

    def test_unknown_and_malformed_ids_are_rejected(self, db, armed):
        for bogus in ("", "nope", {"$ne": None}, None, _uuid.uuid4().hex):
            assert challenge.verify_challenge(bogus, "solved", "iphash-a")["ok"] is False

    def test_unsatisfiable_provider_is_never_presented(self, db, monkeypatch):
        """Never issue a challenge nobody can solve: an unknown provider, or a
        known one with no secret, degrades to the no-op."""
        monkeypatch.setenv("GUEST_CHALLENGE_THRESHOLD", "1")
        monkeypatch.setenv("GUEST_CHALLENGE_PROVIDER", "no-such-provider")
        monkeypatch.setenv("GUEST_CHALLENGE_SECRET", "s")
        assert challenge.active_provider() == challenge.PROVIDER_NONE
        for _ in range(5):
            assert challenge.maybe_challenge("onboard", "iphash-x") is None

        monkeypatch.setenv("GUEST_CHALLENGE_PROVIDER", "turnstile")
        monkeypatch.delenv("GUEST_CHALLENGE_SECRET", raising=False)
        assert challenge.active_provider() == challenge.PROVIDER_NONE
        for _ in range(5):
            assert challenge.maybe_challenge("onboard", "iphash-y") is None
        assert db.guest_challenges.count_documents({}) == 0

    def test_verify_off_provider_does_not_report_success(self, db):
        result = challenge.verify_challenge(_uuid.uuid4().hex, "solved", "iphash")
        assert result["ok"] is False
        assert result["error"] == challenge.ERR_NOT_REQUIRED

    def test_verify_route_closes_the_loop_and_never_401s(self, db, resume_app, armed):
        client = resume_app.test_client()
        cid = self._trip(ip_hash=guest_service.hash_ip("127.0.0.1"))

        bad = client.post("/api/resume/guest/challenge/verify",
                          json={"challenge_id": cid, "solution": "wrong"})
        assert bad.status_code == 400       # NEVER 401
        assert bad.get_json()["error"] == challenge.ERR_SOLUTION_REJECTED

        ok = client.post("/api/resume/guest/challenge/verify",
                         json={"challenge_id": cid, "solution": "solved"})
        assert ok.status_code == 200
        assert ok.get_json()["verified"] is True

        replay = client.post("/api/resume/guest/challenge/verify",
                             json={"challenge_id": cid, "solution": "solved"})
        assert replay.status_code == 400
        assert replay.get_json()["error"] == challenge.ERR_USED

    def test_challenged_route_hands_back_a_redeemable_id(self, db, resume_app, armed):
        """The 429 must carry something the client can actually solve."""
        client = resume_app.test_client()

        def onboard():
            return client.post(
                "/api/resume/guest/onboard",
                data={"file": (io.BytesIO(PDF_BYTES), "resume.pdf")},
                content_type="multipart/form-data",
            )

        assert onboard().status_code == 202
        tripped = onboard()
        assert tripped.status_code == 429
        body = tripped.get_json()
        assert body["error"] == "challenge_required"
        assert body["provider"] == "testcaptcha"
        assert db.guest_challenges.find_one({"_id": body["challenge_id"]})

        # Onboard set the capability cookie, so this write is cookie-authorized
        # and needs the Origin every real browser sends on a POST.
        solved = client.post("/api/resume/guest/challenge/verify",
                             json={"challenge_id": body["challenge_id"],
                                   "solution": "solved"},
                             headers={"Origin": ALLOWED_ORIGIN})
        assert solved.status_code == 200
        assert onboard().status_code == 202   # the retry now goes through


# ===========================================================================
# 3. Guest capability transport: cookie / header, no query-string leak
# ===========================================================================
class TestGuestCapabilityTransport:
    def test_onboard_sets_httponly_secure_cookies(self, db, resume_app, monkeypatch):
        # GUEST_COOKIE_SECURE forces the prod cookie flags without flipping
        # ENVIRONMENT (which would disable guest onboarding entirely).
        monkeypatch.setenv("GUEST_COOKIE_SECURE", "true")
        client = resume_app.test_client()
        resp = client.post(
            "/api/resume/guest/onboard",
            data={"file": (io.BytesIO(PDF_BYTES), "resume.pdf")},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 202
        jar = [h for k, h in resp.headers if k == "Set-Cookie"]
        guest_cookie = next(h for h in jar if h.startswith("jp_guest_id="))
        assert "HttpOnly" in guest_cookie      # unreachable from any XSS
        assert "Secure" in guest_cookie        # never sent in clear
        assert "SameSite=Lax" in guest_cookie  # blocks the cross-site POST
        assert "Path=/api" in guest_cookie
        assert any(h.startswith("jp_guest_device=") for h in jar)

    def test_cookie_authorizes_the_session_read_without_a_url_parameter(
            self, db, resume_app):
        client = resume_app.test_client()
        onboard = client.post(
            "/api/resume/guest/onboard",
            data={"file": (io.BytesIO(PDF_BYTES), "resume.pdf")},
            content_type="multipart/form-data",
        )
        assert onboard.get_json()["guest_id"]
        # No ?guest_id= anywhere in the URL - the cookie carries it.
        resp = client.get("/api/resume/guest/session")
        assert resp.status_code == 200
        assert resp.get_json()["exists"] is True
        # A client WITHOUT the cookie and without a URL parameter resolves to
        # no session at all - proving the cookie is what authorized the read.
        anonymous = resume_app.test_client().get("/api/resume/guest/session")
        assert anonymous.get_json()["exists"] is False

    def test_header_transport_works_for_non_cookie_clients(self, db, resume_app):
        gid = _session_with_resume()
        job_id = guest_service.create_guest_job("guest_parse", {"guest_id": gid}, gid)
        client = resume_app.test_client()
        resp = client.get(f"/api/resume/guest/job/{job_id}",
                          headers={"X-Guest-Id": gid})
        assert resp.status_code == 200
        assert resp.get_json()["job_id"] == job_id

    def test_deprecated_query_string_still_works(self, db, resume_app):
        """The shipped frontend still sends ?guest_id= - it must not break."""
        gid = _session_with_resume()
        job_id = guest_service.create_guest_job("guest_parse", {"guest_id": gid}, gid)
        client = resume_app.test_client()
        assert client.get(
            f"/api/resume/guest/job/{job_id}?guest_id={gid}").status_code == 200

    def test_explicit_value_is_never_overridden_by_the_cookie(self, db, resume_app):
        """An explicitly-passed guest_id must win, or the cookie would mask the
        cross-session check and turn every IDOR probe into a false 200."""
        client = resume_app.test_client()
        mine = client.post(
            "/api/resume/guest/onboard",
            data={"file": (io.BytesIO(PDF_BYTES), "resume.pdf")},
            content_type="multipart/form-data",
        ).get_json()
        # Cookie for MY session is set; ask for MY job as SOMEONE ELSE.
        resp = client.get(
            f"/api/resume/guest/job/{mine['job_id']}?guest_id={_uuid.uuid4()}")
        assert resp.status_code == 404

    # ---- IDOR: cross-session access is a generic 404 (no existence oracle) --
    @pytest.mark.parametrize("transport", ["query", "header", "cookie"])
    def test_cross_session_job_read_returns_generic_404(
            self, db, resume_app, transport):
        victim = _session_with_resume()
        attacker = _session_with_resume()
        job_id = guest_service.create_guest_job(
            "guest_parse", {"guest_id": victim}, victim)
        client = resume_app.test_client()

        if transport == "query":
            resp = client.get(f"/api/resume/guest/job/{job_id}?guest_id={attacker}")
        elif transport == "header":
            resp = client.get(f"/api/resume/guest/job/{job_id}",
                              headers={"X-Guest-Id": attacker})
        else:
            client.set_cookie("jp_guest_id", attacker, path="/api")
            resp = client.get(f"/api/resume/guest/job/{job_id}")

        assert resp.status_code == 404
        # IDENTICAL response to a job that does not exist at all: an attacker
        # learns nothing about whether the id is real.
        unknown = client.get(f"/api/resume/guest/job/{_uuid.uuid4()}"
                             f"?guest_id={attacker}")
        assert unknown.status_code == 404
        assert resp.get_json() == unknown.get_json()

    def test_cross_session_download_mint_returns_generic_404(self, db, resume_app):
        victim = _session_with_resume()
        attacker = _session_with_resume()
        record_id = str(_uuid.uuid4())
        db.tailoring_records.insert_one({
            "record_id": record_id, "guest_id": victim, "user_email": None,
        })
        client = resume_app.test_client()
        stolen = client.post("/api/resume/guest/download",
                             json={"guest_id": attacker, "record_id": record_id,
                                   "format": "pdf"})
        assert stolen.status_code == 404
        unknown = client.post("/api/resume/guest/download",
                              json={"guest_id": attacker,
                                    "record_id": str(_uuid.uuid4()),
                                    "format": "pdf"})
        assert stolen.get_json() == unknown.get_json()

    # ---- CSRF on cookie-authorized writes ---------------------------------
    def test_cookie_authorized_write_requires_an_allowed_origin(
            self, db, resume_app, monkeypatch):
        import services.resume_service as rs

        monkeypatch.setattr(rs.ResumeService, "invoke_async",
                            staticmethod(lambda *a, **k: None))
        gid = _session_with_resume()
        match_id = _serve_one_match(gid)
        client = resume_app.test_client()
        client.set_cookie("jp_guest_id", gid, path="/api")

        # No Origin at all -> rejected (a missing Origin is not a free pass).
        none = client.post("/api/resume/guest/tailor", json={"match_id": match_id})
        assert none.status_code == 403
        assert none.get_json()["error"] == "origin_not_allowed"

        # Attacker origin -> rejected.
        evil = client.post("/api/resume/guest/tailor", json={"match_id": match_id},
                           headers={"Origin": "https://evil.example"})
        assert evil.status_code == 403

        # Our own origin (the SAME explicit allowlist CORS uses) -> allowed.
        good = client.post("/api/resume/guest/tailor", json={"match_id": match_id},
                           headers={"Origin": ALLOWED_ORIGIN})
        assert good.status_code == 202

    def test_csrf_check_never_emits_401(self, db, resume_app):
        gid = _session_with_resume()
        client = resume_app.test_client()
        client.set_cookie("jp_guest_id", gid, path="/api")
        for path, payload in (
            ("/api/resume/guest/tailor", {"match_id": _uuid.uuid4().hex}),
            ("/api/resume/guest/download", {"record_id": str(_uuid.uuid4()),
                                            "format": "pdf"}),
            ("/api/resume/guest/challenge/verify", {"challenge_id": "x",
                                                    "solution": "y"}),
        ):
            resp = client.post(path, json=payload,
                               headers={"Origin": "https://evil.example"})
            assert resp.status_code == 403, path

    def test_explicit_body_value_does_not_need_an_origin(self, db, resume_app,
                                                         monkeypatch):
        """The shipped frontend posts guest_id in the JSON body; a body value
        is actively supplied, not ambient, so it is not CSRF-able the same way
        and must keep working with no Origin header."""
        import services.resume_service as rs

        monkeypatch.setattr(rs.ResumeService, "invoke_async",
                            staticmethod(lambda *a, **k: None))
        gid = _session_with_resume()
        match_id = _serve_one_match(gid)
        client = resume_app.test_client()
        resp = client.post("/api/resume/guest/tailor",
                           json={"guest_id": gid, "match_id": match_id})
        assert resp.status_code == 202

    def test_unverifiable_cookie_on_onboard_mints_a_new_session(
            self, db, resume_app):
        """Onboard treats the cookie as a hint, not a grant: a cross-origin
        upload cannot be steered INTO the victim's session."""
        client = resume_app.test_client()
        victim = _session_with_resume()
        client.set_cookie("jp_guest_id", victim, path="/api")
        resp = client.post(
            "/api/resume/guest/onboard",
            data={"file": (io.BytesIO(PDF_BYTES), "resume.pdf")},
            content_type="multipart/form-data",
            headers={"Origin": "https://evil.example"},
        )
        assert resp.status_code == 202
        assert resp.get_json()["guest_id"] != victim


# ===========================================================================
# 4. JSearch cost chokepoint
# ===========================================================================
class TestJSearchCostChokepoint:
    def _svc(self):
        from services.job_service import get_job_service

        return get_job_service()

    def _call(self):
        return self._svc()._search_jsearch_raw(
            query="backend engineer", location="United States",
            date_posted="week", remote_only=False, employment_type="",
        )

    def test_explicit_guest_context_refuses(self, monkeypatch):
        from services.job_service import PaidSourceBlocked, guest_cost_context

        monkeypatch.setenv("JSEARCH_API_KEY", "should-never-be-used")
        with guest_cost_context():
            with pytest.raises(PaidSourceBlocked):
                self._call()

    def test_refusal_happens_before_the_api_key_or_network(self, monkeypatch):
        """The gate is the FIRST statement: no key read, no requests.get."""
        import services.job_service as js

        monkeypatch.setattr(
            js.requests, "get",
            lambda *a, **k: pytest.fail("JSearch network call from guest context"))
        monkeypatch.setattr(
            js.JobService, "_get_jsearch_key",
            staticmethod(lambda: pytest.fail("API key read from guest context")))
        with js.guest_cost_context():
            with pytest.raises(js.PaidSourceBlocked):
                self._call()

    def test_unauthenticated_request_context_refuses_without_any_flag(self):
        """Defense in depth: a NEW caller that reaches JSearch from an
        unauthenticated HTTP route is refused even though nobody remembered to
        set the guest flag."""
        from services.job_service import PaidSourceBlocked

        app = load_service_app("jobs")
        with app.test_request_context("/api/jobs/guest/matches", method="POST"):
            with pytest.raises(PaidSourceBlocked):
                self._call()

    def test_authenticated_caller_is_unaffected(self, monkeypatch):
        from flask_jwt_extended import create_access_token

        app = load_service_app("jobs")
        with app.app_context():
            token = create_access_token(identity="user@example.com")
        # No API key configured -> the normal empty result, NOT a refusal:
        # proving the gate did not fire for an authenticated caller.
        monkeypatch.setattr(
            "services.job_service.JobService._get_jsearch_key",
            staticmethod(lambda: ""))
        with app.test_request_context(
            "/api/jobs/search", headers={"Authorization": f"Bearer {token}"}
        ):
            assert self._call() == []

    def test_background_worker_without_a_flag_is_unaffected(self, monkeypatch):
        """No request context and no flag = the authed daily pipeline; it must
        keep working (the guest path sets the flag / is gated upstream)."""
        monkeypatch.setattr(
            "services.job_service.JobService._get_jsearch_key",
            staticmethod(lambda: ""))
        assert self._call() == []

    def test_context_is_visible_from_pool_threads(self):
        """The guest match pipeline fans out over a ThreadPoolExecutor, which
        does NOT propagate contextvars - the flag must survive the hop."""
        from concurrent.futures import ThreadPoolExecutor

        from services.job_service import guest_cost_context, in_guest_cost_context

        with guest_cost_context():
            with ThreadPoolExecutor(max_workers=2) as pool:
                assert all(f.result() for f in [
                    pool.submit(in_guest_cost_context) for _ in range(4)
                ])

    def test_context_is_released_on_exit(self):
        from services.job_service import guest_cost_context, in_guest_cost_context

        with guest_cost_context():
            assert in_guest_cost_context() is True
        assert in_guest_cost_context() is False
