"""E2E-1 — the full GUEST journey through the REAL application routes.

Journey (docs/specs/resume-platform-v3.md):
  multipart onboard upload -> guest_parse worker (real parser, real pypdf text
  extraction of a real fpdf2-generated PDF; Bedrock faked at the claude_json
  boundary) -> poll to parsed (profile block + resume_id) ->
  POST /api/jobs/guest/matches (real run_pipeline; outbound job-provider HTTP
  faked at the requests.get/post boundary; >=5 scoreable Greenhouse jobs) ->
  guest_matches worker -> <=5 cards with match_id/title + sources_report ->
  POST /api/resume/guest/tailor with a SERVED match_id -> 202 + reservation ->
  guest_tailor worker (FakeLLM supplies JD analysis + tailored output; the
  real IntegrityGuard/finalize pipeline runs) -> record persisted guest-owned
  -> POST /guest/download + GET the token URL (real shared renderer streams
  real PDF bytes) -> register + login through the REAL auth routes ->
  POST /guest/claim (state machine completed, record/asset transferred,
  day-1 carry-in seeded, second account 403) -> the pre-claim download token
  dies at redemption.

Plus the refund contract: a FAILED guest tailor job refunds the single-slot
reservation and a retry succeeds without burning the trial.

Mocked ONLY at the platform boundaries: Bedrock (FakeLLM via claude_json),
S3 (in-memory storage fake), outbound job-provider HTTP (requests.get/post).
Everything else — routes, workers, parser, scorer, renderer, guards — is real.
"""
import copy
import io
import json
import uuid as _uuid
from datetime import datetime, timedelta, timezone

import pytest
import requests as _requests_mod

from _vg_support import load_service_app

from services import guest_service
from services import platform_config_service as pconfig
from services import usage_service
from utils import db_connect

COLLECTIONS = (
    "guest_sessions",
    "guest_quota_subjects",
    "guest_claim_carryins",
    "guest_global_counters",
    "guest_challenge_velocity",
    "guest_challenges",
    "rate_limit_buckets",
    "job_match_snapshots",
    "platform_config",
    "tailoring_records",
    "tailor_usage",
    "user_resumes",
    "resume_assets",
    "resume_jobs",
    "users",
    "user_preferences",
    "feature_usage",
    "sponsorship_signals",
    "quota_counters",
    "quota_ledger",
    "company_logos",
    "parse_streaks",
)

PASSWORD = "Str0ng!Pass1"


# ---------------------------------------------------------------------------
# Canned LLM payloads (schema-valid; the ONLY faked boundary is claude_json)
# ---------------------------------------------------------------------------

def _parsed_payload():
    return {
        "contact": {
            "name": "Jordan Alvarez", "email": "jordan.alvarez@example.com",
            "phone": "(555) 010-7788", "linkedin": "", "github": "",
            "location": "Tempe, AZ", "portfolio": "",
        },
        "summary": "Backend engineer with four years building Python APIs on AWS.",
        "skills": {"Languages": ["Python", "Go", "SQL"], "Cloud": ["AWS"]},
        "experience": [{
            "title": "Backend Engineer", "company": "Acme",
            "location": "Tempe, AZ", "dates": "Jan 2020 - Present",
            "type": "Full-time",
            "bullets": [
                "Built REST APIs in Python serving two million requests per day.",
                "Designed SQL schemas and tuned queries on AWS RDS.",
            ],
        }],
        "education": [{
            "degree": "BS Computer Science",
            "institution": "Arizona State University",
            "location": "Tempe, AZ", "dates": "2016 - 2020",
            "gpa": "", "coursework": "",
        }],
        "projects": [],
        "certifications": [],
    }


def _tailored_payload():
    t = _parsed_payload()
    t["summary"] = "Backend engineer focused on Python APIs, SQL and AWS services."
    t["skills_section_title"] = "Technical Skills"
    return t


def _jd_payload():
    # Required skills all genuinely on the resume -> no gaps, no fabrication
    # pressure: the real IntegrityGuard must come back clean.
    return {
        "job_title": "Backend Engineer",
        "company": "InitechSoft",
        "location": "Remote, United States",
        "employment_type": "Full-time",
        "required_skills": ["Python", "SQL"],
        "preferred_skills": ["AWS"],
        "responsibilities": ["Build and operate backend APIs"],
        "qualifications": ["Experience building Python services"],
        "experience_years": "2-4 years",
        "industry": "Software",
        "keywords": ["Python", "SQL", "AWS", "APIs"],
    }


def _install_llm_handler(fake):
    """Dispatch canned responses by call shape.

    * multimodal parts        -> resume parse (PARSED_RESUME shape)
    * JD_ANALYSIS schema      -> JD extraction
    * schema-less prose calls -> the tailored resume (tailor primary /
                                 correction / regenerate share this shape)
    * anything else (quality judge, verifier, augmenter, gap tiering, ...)
      raises — those callers are fail-open BY DESIGN and must degrade.
    """
    def handler(call):
        if call.get("parts") is not None:
            return copy.deepcopy(_parsed_payload())
        schema = call.get("schema")
        if isinstance(schema, dict) and "required_skills" in schema:
            return copy.deepcopy(_jd_payload())
        if schema is None:
            return copy.deepcopy(_tailored_payload())
        raise AssertionError(
            "auxiliary LLM call without a canned response (caller must fail open)"
        )
    fake.set_handler(handler)
    return fake


# ---------------------------------------------------------------------------
# A REAL text PDF (fpdf2) so pypdf extraction + the real parser path runs
# ---------------------------------------------------------------------------

_RESUME_LINES = (
    "Jordan Alvarez",
    "jordan.alvarez@example.com | (555) 010-7788 | Tempe, AZ",
    "Summary",
    "Backend engineer with four years of experience building Python APIs",
    "and data services on AWS. Comfortable with Go, SQL and cloud tooling.",
    "Skills: Python, Go, SQL, AWS",
    "Experience",
    "Backend Engineer, Acme (Jan 2020 - Present)",
    "- Built REST APIs in Python serving two million requests per day.",
    "- Designed SQL schemas and tuned queries on AWS RDS.",
    "Education",
    "BS Computer Science, Arizona State University, 2016 - 2020",
)


def _resume_pdf_bytes() -> bytes:
    from fpdf import FPDF

    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    pdf.set_font("Helvetica", size=11)
    for line in _RESUME_LINES:
        pdf.cell(0, 7, line, new_x="LMARGIN", new_y="NEXT")
    return bytes(pdf.output())


# ---------------------------------------------------------------------------
# Boundary fakes: S3 (in-memory) + outbound job-provider HTTP (requests)
# ---------------------------------------------------------------------------

class _MemoryStorage:
    """S3 stand-in that really round-trips bytes so the worker parses the
    genuinely uploaded document."""

    def __init__(self):
        self.objects = {}

    def upload_resume_asset(self, resume_id, file_bytes, mime_type):
        key = f"resume-assets/{resume_id}/original"
        self.objects[key] = bytes(file_bytes)
        return key

    def get_resume(self, s3_key):
        return self.objects[s3_key]

    def delete_resume(self, s3_key):
        self.objects.pop(s3_key, None)
        return True


class _FakeHTTPResponse:
    def __init__(self, payload=None, status=200):
        self.status_code = status
        self.reason = "OK" if status == 200 else "Not Found"
        self._payload = payload
        self.headers = {"Content-Type": "application/json"}
        self.text = "" if payload is None else json.dumps(payload)
        self.content = self.text.encode()

    @property
    def ok(self):
        return 200 <= self.status_code < 300

    def raise_for_status(self):
        if self.status_code >= 400:
            raise _requests_mod.HTTPError(f"{self.status_code} for fake url")

    def json(self):
        if self._payload is None:
            raise ValueError("no json body")
        return self._payload


_GH_DESC_HTML = (
    "<p>We use Python Go SQL AWS daily. You will build backend APIs "
    "and data services with Python and SQL on AWS.</p>"
)


def _greenhouse_jobs_payload():
    """Six scoreable postings (>=5 clear the score bar; the guest cap must
    trim the visible response to 5). Shapes match the REAL Greenhouse board
    API the career-pages fetcher consumes (?content=false list + per-job
    detail): dict location, numeric ids, first_published timestamps.
    Seniority-neutral titles by design — they appear in every view.

    first_published is RELATIVE to now (1 day back): the guest freshness
    contract drops anything older than GUEST_MAX_AGE_DAYS, so a hardcoded
    date would silently rot this suite once the calendar passed it."""
    published = (
        datetime.now(timezone.utc) - timedelta(days=1)
    ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    titles = (
        "Backend Engineer",
        "Python Backend Engineer",
        "Software Engineer, Backend",
        "Platform Engineer",
        "API Engineer",
        "Go Backend Engineer",
    )
    return {"jobs": [
        {
            "id": 1000 + i,
            "title": t,
            "location": {"name": "Remote - United States"},
            "absolute_url": f"https://boards.example/jobs/{1000 + i}",
            "content": _GH_DESC_HTML,
            "first_published": published,
            # Present but IGNORED: updated_at is an edit timestamp and is no
            # longer a posting-date fallback (freshness inflation).
            "updated_at": published,
        }
        for i, t in enumerate(titles)
    ]}


@pytest.fixture()
def fake_job_http(monkeypatch):
    """Route ALL outbound provider HTTP through a deterministic fake — both
    the module-level requests.get/post AND Session.get/post (the v4 scan's
    three feed services each use a module Session). ONE Greenhouse board
    (the first greenhouse company in the career-pages catalog) returns six
    scoreable jobs; every other provider/host answers an empty 404 — with
    three full catalogs in the scan, enumerating hosts is impractical, so
    the guard is "no real network" rather than "no unknown URL"."""
    from services.career_pages_catalog import CAREER_PAGES_CATALOG

    gh = next(r for r in CAREER_PAGES_CATALOG if r["ats"] == "greenhouse")
    slug, company = gh["slug"], gh["display_name"]
    payload = _greenhouse_jobs_payload()

    def _get(url, *args, **kwargs):
        u = str(url)
        if "boards-api.greenhouse.io" in u and f"/boards/{slug}/jobs" in u:
            tail = u.split(f"/boards/{slug}/jobs", 1)[1]
            if tail.startswith("/"):  # per-job detail (description enrich)
                return _FakeHTTPResponse({"content": _GH_DESC_HTML})
            return _FakeHTTPResponse(payload)
        if "boards-api.greenhouse.io" in u:
            return _FakeHTTPResponse({"jobs": []})
        if "api.lever.co" in u:
            return _FakeHTTPResponse([])
        if "api.ashbyhq.com" in u:
            return _FakeHTTPResponse({"jobs": []})
        return _FakeHTTPResponse(None, status=404)

    def _post(url, *args, **kwargs):
        return _FakeHTTPResponse(None, status=404)

    monkeypatch.setattr(_requests_mod, "get", _get)
    monkeypatch.setattr(_requests_mod, "post", _post)
    # The feed services call their own requests.Session instances — patch the
    # CLASS so every session routes through the same fake (no real network).
    monkeypatch.setattr(_requests_mod.Session, "get",
                        lambda self, url, *a, **k: _get(url, *a, **k))
    monkeypatch.setattr(_requests_mod.Session, "post",
                        lambda self, url, *a, **k: _post(url, *a, **k))
    return company


# ---------------------------------------------------------------------------
# Suite fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _guest_secrets(monkeypatch):
    monkeypatch.setenv("GUEST_SIGNAL_HMAC_KEY", "unit-test-signal-key")
    monkeypatch.setenv("GUEST_DOWNLOAD_TOKEN_SECRET", "unit-test-download-secret")
    monkeypatch.delenv("GUEST_CHALLENGE_PROVIDER", raising=False)
    monkeypatch.delenv("GUEST_IP_HASH_SALT", raising=False)
    monkeypatch.delenv("AWS_LAMBDA_FUNCTION_NAME", raising=False)


@pytest.fixture()
def db():
    from utils.security import get_rate_limiter
    database = db_connect.DBConnect().get_db()
    for c in COLLECTIONS:
        database[c].delete_many({})
    pconfig.invalidate_cache()
    get_rate_limiter()._requests.clear()
    yield database
    pconfig.invalidate_cache()
    get_rate_limiter()._requests.clear()


@pytest.fixture()
def storage(monkeypatch):
    fake = _MemoryStorage()
    import services.s3_service as s3s
    monkeypatch.setattr(s3s, "get_storage_service", lambda: fake)
    return fake


@pytest.fixture()
def dispatched(monkeypatch):
    """Capture async dispatches so each worker can be run INLINE through the
    real resume_service._process_job (the exact local-thread body)."""
    calls = []
    import services.resume_service as rs
    monkeypatch.setattr(
        rs.ResumeService, "invoke_async",
        staticmethod(lambda job_id, job_type, payload:
                     calls.append((job_id, job_type, payload))),
    )
    return calls


def _run_next_worker(calls):
    import services.resume_service as rs
    job_id, job_type, payload = calls.pop(0)
    rs._process_job(job_id, job_type, payload)
    return job_id


@pytest.fixture()
def resume_app(db, dispatched):
    return load_service_app("resume")


@pytest.fixture()
def jobs_app(db, dispatched):
    return load_service_app("jobs")


@pytest.fixture()
def auth_app(db):
    return load_service_app("auth")


def _register_and_login(auth_app, email):
    client = auth_app.test_client()
    reg = client.post("/api/auth/register",
                      json={"email": email, "password": PASSWORD})
    assert reg.status_code == 201, reg.get_data(as_text=True)
    login = client.post("/api/auth/login",
                        json={"email": email, "password": PASSWORD})
    assert login.status_code == 200, login.get_data(as_text=True)
    body = login.get_json()
    assert body["email"] == email and body["access_token"]  # FLAT payload
    return {"Authorization": f"Bearer {body['access_token']}"}


# ===========================================================================
# The journey
# ===========================================================================

class TestGuestJourneyE2E:
    def test_full_guest_journey(self, db, storage, dispatched, fake_llm,
                                fake_job_http, resume_app, jobs_app, auth_app):
        _install_llm_handler(fake_llm)
        resume_client = resume_app.test_client()
        jobs_client = jobs_app.test_client()

        # ------------------------------------------------------------------
        # 1. Guest onboard — real multipart upload route
        # ------------------------------------------------------------------
        onboard = resume_client.post(
            "/api/resume/guest/onboard",
            data={"file": (io.BytesIO(_resume_pdf_bytes()), "resume.pdf"),
                  "fingerprint_hash": "fp_e2e_guest"},
            content_type="multipart/form-data",
        )
        assert onboard.status_code == 202, onboard.get_data(as_text=True)
        ob = onboard.get_json()
        guest_id, resume_id = ob["guest_id"], ob["resume_id"]
        assert ob["job_id"] and ob["device_token"]
        # The upload really landed in (fake) S3 under the v3 key scheme.
        assert f"resume-assets/{resume_id}/original" in storage.objects

        # ------------------------------------------------------------------
        # 2. Run the guest_parse worker inline; poll the REAL guest job route
        # ------------------------------------------------------------------
        assert dispatched and dispatched[0][1] == "guest_parse"
        parse_job = _run_next_worker(dispatched)

        poll = resume_client.get(
            f"/api/resume/guest/job/{parse_job}?guest_id={guest_id}")
        assert poll.status_code == 200
        job = poll.get_json()
        assert job["status"] == "completed", job.get("error")
        result = job["result"]
        assert result["resume_id"] == resume_id
        profile = result["profile"]
        assert profile["first_name"] == "Jordan"
        assert profile["headline_title"] == "Backend Engineer"
        assert 0 < len(profile["skills"]) <= 10

        asset = db.resume_assets.find_one({"resume_id": resume_id})
        assert asset["parse_status"] == "parsed"
        assert asset["guest_id"] == guest_id
        # The worker parsed the REAL uploaded bytes (fetched from storage).
        assert asset["raw_text"] and "Jordan Alvarez" in asset["raw_text"]

        # ------------------------------------------------------------------
        # 3. Guest matches — real route + real run_pipeline over fake HTTP
        # ------------------------------------------------------------------
        m = jobs_client.post("/api/jobs/guest/matches",
                             json={"guest_id": guest_id})
        assert m.status_code == 202, m.get_data(as_text=True)
        matches_job = m.get_json()["job_id"]
        assert dispatched and dispatched[0][1] == "guest_matches"
        _run_next_worker(dispatched)

        poll = resume_client.get(
            f"/api/resume/guest/job/{matches_job}?guest_id={guest_id}")
        assert poll.status_code == 200
        mjob = poll.get_json()
        assert mjob["status"] == "completed", mjob.get("error")
        mres = mjob["result"]
        cards = mres["jobs"]
        # Six qualifying postings, hard-capped at 5 (spec 3.6 — no padding,
        # no hidden rows).
        assert 1 <= len(cards) <= 5
        assert len(cards) == 5
        for card in cards:
            assert guest_service.validate_match_id(card["match_id"])
            assert card["title"]
            assert card["match_score"] >= 25  # default guest_jobs_min_score
            assert card["seniority"] in ("entry", "mid", "senior", "neutral")
        # v4: the scan runs the SAME three sources as the authed feed.
        for src in ("workday", "career_pages", "mnc"):
            assert src in mres["sources_report"]["ok"]
        # Seniority pool metadata: honest counts, instant re-rank support.
        assert mres["seniority"] in ("any", "entry", "mid", "senior")
        assert mres["pool_total"] >= len(cards)
        assert mres["locked_count"] == mres["pool_total"] - len(cards)
        assert mres["detected_experience"]["level"] in ("entry", "mid", "senior")
        # Snapshot persisted server-side; jd_sources never leave the server.
        snap = db.job_match_snapshots.find_one({"guest_id": guest_id})
        assert snap and len(snap["jobs"]) == 5
        assert len(snap["pool"]) >= 5
        assert "jd_sources" not in mres
        assert "pool" not in mres

        # Seniority toggle: a cached re-request with a different view is an
        # INSTANT 200 re-slice of the stored pool (no new job, no budget).
        m2 = jobs_client.post("/api/jobs/guest/matches",
                              json={"guest_id": guest_id, "seniority": "any"})
        assert m2.status_code == 200, m2.get_data(as_text=True)
        v2 = m2.get_json()
        assert v2["cached"] is True and v2["seniority"] == "any"
        assert 1 <= len(v2["jobs"]) <= 5

        match_id = cards[0]["match_id"]

        # ------------------------------------------------------------------
        # 4. Guest tailor — real route: reservation + async dispatch
        # ------------------------------------------------------------------
        t = resume_client.post("/api/resume/guest/tailor",
                               json={"guest_id": guest_id,
                                     "match_id": match_id})
        assert t.status_code == 202, t.get_data(as_text=True)
        tbody = t.get_json()
        tailor_job = tbody["job_id"]
        assert tbody["tailors_used"] == 1 and tbody["tailors_limit"] == 1

        # ------------------------------------------------------------------
        # 5. Guest tailor worker — real pipeline (guards run), record persisted
        # ------------------------------------------------------------------
        assert dispatched and dispatched[0][1] == "guest_tailor"
        _run_next_worker(dispatched)

        poll = resume_client.get(
            f"/api/resume/guest/job/{tailor_job}?guest_id={guest_id}")
        tjob = poll.get_json()
        assert tjob["status"] == "completed", tjob.get("error")
        record_id = tjob["result"]["record_id"]
        assert record_id

        record = db.tailoring_records.find_one({"record_id": record_id})
        assert record["user_email"] is None          # guest-owned until claim
        assert record["guest_id"] == guest_id
        assert record["source"] == "guest_tailor"
        assert record["versions"] and record["current_version_id"]
        # No fabricated employers: experience mirrors the uploaded resume.
        companies = {e["company"] for e in record["tailored_resume"]["experience"]}
        assert companies == {"Acme"}

        subject = db.guest_quota_subjects.find_one(
            {"subject_id": guest_service.subject_for_guest(guest_id)})
        assert subject["tailors_used"] == 1          # quota subject charged once
        assert subject["reservation"]["status"] == "completed"
        assert subject["reservation"]["record_id"] == record_id

        # ------------------------------------------------------------------
        # 6. Polished download — mint token, stream REAL rendered PDF bytes
        # ------------------------------------------------------------------
        mint = resume_client.post(
            "/api/resume/guest/download",
            json={"guest_id": guest_id, "record_id": record_id,
                  "format": "pdf"})
        assert mint.status_code == 200, mint.get_data(as_text=True)
        token = mint.get_json()["token"]
        assert token.startswith("v1.")

        got = resume_client.get(f"/api/resume/guest/download/{token}")
        assert got.status_code == 200
        assert got.mimetype == "application/pdf"
        assert got.data.startswith(b"%PDF")
        assert len(got.data) > 500                   # a real rendered document
        # Downloads never consume quota.
        assert guest_service.tailor_quota_state(guest_id)["tailors_used"] == 1

        # ------------------------------------------------------------------
        # 7. Register + login through the REAL auth routes (flat payloads,
        #    failures never 401), then claim.
        # ------------------------------------------------------------------
        email = "keeper@example.com"
        headers = _register_and_login(auth_app, email)

        claim = resume_client.post("/api/resume/guest/claim",
                                   headers=headers,
                                   json={"guest_id": guest_id})
        assert claim.status_code == 200, claim.get_data(as_text=True)
        cbody = claim.get_json()
        assert cbody["claimed"] is True and cbody["already_claimed"] is False
        assert cbody["records_transferred"] == 1
        assert cbody["tailors_carried"] == 1
        assert cbody["daily_tailors_used"] == 1

        session = db.guest_sessions.find_one({"guest_id": guest_id})
        assert session["claim"]["state"] == "completed"
        assert session["claim"]["email"] == email
        assert session["claimed_by"] == email

        record = db.tailoring_records.find_one({"record_id": record_id})
        assert record["user_email"] == email          # transferred

        asset = db.resume_assets.find_one({"resume_id": resume_id})
        assert asset["claim_status"] == "claimed"
        assert asset["user_email"] == email
        assert asset["purge_after"] is None           # retention lifted

        # Day-1 carry-in seeded exactly once; the account sees 1 of 5 used.
        assert db.guest_claim_carryins.count_documents({}) == 1
        assert usage_service.get_usage(email)["used"] == 1

        # ------------------------------------------------------------------
        # 8. A SECOND account cannot claim the same session -> 403 (never 401)
        # ------------------------------------------------------------------
        thief_headers = _register_and_login(auth_app, "second@example.com")
        second = resume_client.post("/api/resume/guest/claim",
                                    headers=thief_headers,
                                    json={"guest_id": guest_id})
        assert second.status_code == 403
        assert second.get_json()["error"] == "already_claimed"
        # Nothing leaked to the loser.
        assert db.tailoring_records.find_one(
            {"user_email": "second@example.com"}) is None

        # ------------------------------------------------------------------
        # 9. Pre-claim download token is DEAD after the claim (mint + redeem)
        # ------------------------------------------------------------------
        dead = resume_client.get(f"/api/resume/guest/download/{token}")
        assert dead.status_code == 404
        remint = resume_client.post(
            "/api/resume/guest/download",
            json={"guest_id": guest_id, "record_id": record_id,
                  "format": "pdf"})
        assert remint.status_code == 404


# ===========================================================================
# Fail path — a FAILED tailor job refunds the reservation; retry succeeds
# ===========================================================================

class TestGuestTailorFailureRefund:
    def _serve_match(self, guest_id):
        mid = _uuid.uuid4().hex
        guest_service._store_matches(
            guest_id,
            [{"match_id": mid, "title": "Backend Engineer", "company": "Acme",
              "url": "https://jobs.example/1", "source": "Greenhouse",
              "match_score": 80, "matched_skills": ["python"],
              "why": "Estimated match because this posting mentions python from your resume.",
              "sponsorship": None}],
            20,
            jd_sources={mid: {
                "title": "Backend Engineer", "company": "Acme",
                "jd_text": "Python SQL AWS backend APIs " * 20,
                "url": "https://jobs.example/1",
            }},
        )
        return mid

    def test_failed_tailor_refunds_and_retry_does_not_burn_trial(
            self, db, storage, dispatched, fake_llm, resume_app):
        _install_llm_handler(fake_llm)
        session = guest_service.create_or_get_session(None, "fp_fail", "iphash")
        gid = session["guest_id"]
        guest_service.attach_parsed_resume(
            gid, _parsed_payload(), "Jordan Alvarez resume raw text " * 10,
            "resume.pdf", fingerprint_hash="fp_fail")
        mid = self._serve_match(gid)
        client = resume_app.test_client()

        first = client.post("/api/resume/guest/tailor",
                            json={"guest_id": gid, "match_id": mid})
        assert first.status_code == 202, first.get_data(as_text=True)
        job1 = first.get_json()["job_id"]
        assert first.get_json()["tailors_used"] == 1

        # Worker hits a terminal LLM outage -> the job fails and the guest's
        # single-slot reservation is REFUNDED (an infra error never consumes
        # the trial).
        fake_llm.fail_with(RuntimeError("bedrock unavailable"))
        assert dispatched and dispatched[0][1] == "guest_tailor"
        _run_next_worker(dispatched)

        failed = db.resume_jobs.find_one({"job_id": job1})
        assert failed["status"] == "failed"
        subject = db.guest_quota_subjects.find_one(
            {"subject_id": guest_service.subject_for_guest(gid)})
        assert subject["tailors_used"] == 0           # refunded, never negative
        assert subject["reservation"]["status"] == "failed"

        # Retry with the provider healthy again: a FRESH reservation (not the
        # dead job), 202, and the worker completes — the guest was not cheated.
        fake_llm.fail_with(None)
        retry = client.post("/api/resume/guest/tailor",
                            json={"guest_id": gid, "match_id": mid})
        assert retry.status_code == 202, retry.get_data(as_text=True)
        job2 = retry.get_json()["job_id"]
        assert job2 != job1
        assert "existing" not in (retry.get_json() or {})

        _run_next_worker(dispatched)
        done = db.resume_jobs.find_one({"job_id": job2})
        assert done["status"] == "completed", done.get("error")
        record_id = done["result"]["record_id"]
        assert db.tailoring_records.find_one(
            {"record_id": record_id, "guest_id": gid, "user_email": None})

        subject = db.guest_quota_subjects.find_one(
            {"subject_id": guest_service.subject_for_guest(gid)})
        assert subject["tailors_used"] == 1           # exactly one trial spent
        assert subject["reservation"]["status"] == "completed"
