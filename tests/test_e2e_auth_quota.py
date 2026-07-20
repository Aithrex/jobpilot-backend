"""E2E-2 — the AUTHENTICATED tailor quota journey through the REAL routes.

Journey (docs/specs/resume-platform-v3.md 1.2/1.5/1.6):
  register + login through the REAL auth routes (free plan) ->
  POST /api/resume/upload (real multipart route, S3 faked in-memory) ->
  upload_parse worker parses the REAL PDF (pypdf extraction; Bedrock faked at
  claude_json) and promotes users.active_resume_id ->
  five tailor sessions via POST /api/resume/tailor (atomic reserve BEFORE
  dispatch; worker persists the record and settles the ledger entry
  reserved -> completed) -> the SIXTH POST is 429 daily_quota_exceeded with
  {used: 5, limit: 5, remaining: 0} ->
  regenerate WITHIN a record consumes nothing; the 6th version of one record
  is 409 version_limit_reached ->
  on a FRESH day (time controlled) a terminal worker failure REFUNDS the
  reservation (entry refunded, counter decremented) and the user can
  immediately reserve again.

Mocked ONLY at the platform boundaries: Bedrock (FakeLLM via claude_json)
and S3 (in-memory fake). Routes, workers, quota ledger, versioning, guards
and the renderer all run for real.
"""
import copy
import io
from datetime import datetime, timedelta, timezone

import pytest

from _vg_support import load_service_app

from services import quota_ledger_service as ql
from services import usage_service
from utils import db_connect

EMAIL = "quota.e2e@example.com"
PASSWORD = "Str0ng!Pass1"

COLLECTIONS = (
    "users",
    "user_preferences",
    "resume_jobs",
    "resume_assets",
    "user_resumes",
    "tailoring_records",
    "quota_counters",
    "quota_ledger",
    "tailor_usage",
    "feature_usage",
    "guest_quota_subjects",
    "entitlement_overrides",
    "platform_config",
    "parse_streaks",
)


# ---------------------------------------------------------------------------
# Canned LLM payloads (the only faked boundary besides S3)
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


def _tailored_payload(marker="tailored"):
    t = _parsed_payload()
    t["summary"] = f"Backend engineer focused on Python, SQL and AWS ({marker})."
    t["skills_section_title"] = "Technical Skills"
    return t


def _jd_payload(i=0):
    return {
        "job_title": f"Backend Engineer {i}",
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


class _MemoryStorage:
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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    monkeypatch.delenv("AWS_LAMBDA_FUNCTION_NAME", raising=False)


@pytest.fixture()
def db():
    from utils.security import get_rate_limiter
    database = db_connect.DBConnect().get_db()
    for c in COLLECTIONS:
        database[c].delete_many({})
    get_rate_limiter()._requests.clear()
    yield database
    get_rate_limiter()._requests.clear()


@pytest.fixture()
def storage(monkeypatch):
    fake = _MemoryStorage()
    import services.s3_service as s3s
    monkeypatch.setattr(s3s, "get_storage_service", lambda: fake)
    return fake


@pytest.fixture()
def dispatched(monkeypatch):
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
def auth_app(db):
    return load_service_app("auth")


def _register_and_login(auth_app, email=EMAIL):
    client = auth_app.test_client()
    reg = client.post("/api/auth/register",
                      json={"email": email, "password": PASSWORD})
    assert reg.status_code == 201, reg.get_data(as_text=True)
    login = client.post("/api/auth/login",
                        json={"email": email, "password": PASSWORD})
    assert login.status_code == 200, login.get_data(as_text=True)
    return {"Authorization": f"Bearer {login.get_json()['access_token']}"}


def _counter_count(db, email=EMAIL, date=None):
    doc = db.quota_counters.find_one({"_id": ql.counter_id_for(email, date)})
    return int((doc or {}).get("count", 0))


# ===========================================================================
# The journey
# ===========================================================================

class TestAuthQuotaJourneyE2E:
    def _upload_and_parse(self, db, resume_app, dispatched, headers):
        resp = resume_app.test_client().post(
            "/api/resume/upload",
            data={"file": (io.BytesIO(_resume_pdf_bytes()), "resume.pdf")},
            content_type="multipart/form-data",
            headers=headers,
        )
        assert resp.status_code == 202, resp.get_data(as_text=True)
        rid = resp.get_json()["resume_id"]
        assert dispatched and dispatched[0][1] == "upload_parse"
        _run_next_worker(dispatched)
        asset = db.resume_assets.find_one({"resume_id": rid})
        assert asset["parse_status"] == "parsed"
        user = db.users.find_one({"email": EMAIL})
        assert user["active_resume_id"] == rid       # pointer promoted
        assert user["plan"] == "free"                # register default: limit 5
        return rid

    def test_full_auth_quota_journey(self, db, storage, dispatched, fake_llm,
                                     resume_app, auth_app, monkeypatch):
        _install_llm_handler(fake_llm)
        headers = _register_and_login(auth_app)
        client = resume_app.test_client()

        self._upload_and_parse(db, resume_app, dispatched, headers)

        # ------------------------------------------------------------------
        # Five tailor sessions: reserve -> dispatch -> worker persists record
        # and the ledger entry settles reserved -> completed.
        # ------------------------------------------------------------------
        record_ids = []
        for i in range(5):
            resp = client.post("/api/resume/tailor",
                               json={"jd_analysis": _jd_payload(i)},
                               headers=headers)
            assert resp.status_code == 202, resp.get_data(as_text=True)
            body = resp.get_json()
            job_id = body["job_id"]
            assert body["usage"]["used"] == i + 1
            assert body["usage"]["limit"] == 5

            # The reservation entry rides the payload and starts 'reserved'.
            row = db.resume_jobs.find_one({"job_id": job_id})
            entry_id = row["payload"]["quota_entry_id"]
            entry = db.quota_ledger.find_one({"entry_id": entry_id})
            assert entry["state"] == "reserved"
            assert entry["job_id"] == job_id

            assert dispatched and dispatched[0][1] == "tailor"
            _run_next_worker(dispatched)

            job = db.resume_jobs.find_one({"job_id": job_id})
            assert job["status"] == "completed", job.get("error")
            record_id = job["result"]["record_id"]
            assert record_id
            record_ids.append(record_id)

            record = db.tailoring_records.find_one({"record_id": record_id})
            assert record["user_email"] == EMAIL
            assert record["source"] == "tailor"
            assert len(record["versions"]) == 1

            entry = db.quota_ledger.find_one({"entry_id": entry_id})
            assert entry["state"] == "completed"      # reserved -> completed
            assert entry["record_id"] == record_id

        assert _counter_count(db) == 5
        assert usage_service.get_usage(EMAIL)["used"] == 5
        # No double count anywhere else.
        assert db.feature_usage.count_documents({"feature": "tailor"}) == 0

        # ------------------------------------------------------------------
        # The SIXTH new session: 429 daily_quota_exceeded, exact usage block.
        # ------------------------------------------------------------------
        sixth = client.post("/api/resume/tailor",
                            json={"jd_analysis": _jd_payload(6)},
                            headers=headers)
        assert sixth.status_code == 429
        body = sixth.get_json()
        assert body["error"] == "daily_quota_exceeded"
        assert body["usage"]["used"] == 5
        assert body["usage"]["limit"] == 5
        assert body["usage"]["remaining"] == 0
        assert _counter_count(db) == 5                # denial never increments
        assert db.quota_ledger.count_documents({}) == 5

        # ------------------------------------------------------------------
        # Regenerate WITHIN an existing record consumes NOTHING.
        # ------------------------------------------------------------------
        rid1 = record_ids[0]
        regen = client.post(
            "/api/resume/regenerate",
            json={
                "record_id": rid1,
                "jd_analysis": _jd_payload(0),
                "tailored_resume": _tailored_payload("v1"),
                "user_feedback": "tighten the summary",
            },
            headers=headers)
        assert regen.status_code == 202, regen.get_data(as_text=True)
        assert dispatched and dispatched[0][1] == "regenerate"
        _run_next_worker(dispatched)
        rjob = db.resume_jobs.find_one({"job_id": regen.get_json()["job_id"]})
        assert rjob["status"] == "completed", rjob.get("error")
        # Refinement is free: counter and ledger untouched.
        assert _counter_count(db) == 5
        assert db.quota_ledger.count_documents({}) == 5

        # ------------------------------------------------------------------
        # Bounded versions: reach 5 on one record, the 6th attempt is 409.
        # ------------------------------------------------------------------
        for v in range(2, 6):                          # versions 2..5
            resp = client.post(
                f"/api/resume/tailoring-records/{rid1}/versions",
                json={"tailored_resume": _tailored_payload(f"v{v}"),
                      "source": "edited"},
                headers=headers)
            assert resp.status_code == 201, resp.get_data(as_text=True)
            assert resp.get_json()["created"] is True
        capped = client.post(
            f"/api/resume/tailoring-records/{rid1}/versions",
            json={"tailored_resume": _tailored_payload("v6"),
                  "source": "edited"},
            headers=headers)
        assert capped.status_code == 409
        assert capped.get_json() == {"error": "version_limit_reached",
                                     "limit": 5}
        assert len(db.tailoring_records.find_one(
            {"record_id": rid1})["versions"]) == 5

        # Regenerate against the CAPPED record is refused up front (no LLM
        # spend for a result that could never be saved) and still free.
        regen_capped = client.post(
            "/api/resume/regenerate",
            json={
                "record_id": rid1,
                "jd_analysis": _jd_payload(0),
                "tailored_resume": _tailored_payload("v5"),
                "user_feedback": "one more pass",
            },
            headers=headers)
        assert regen_capped.status_code == 409
        assert regen_capped.get_json()["error"] == "version_limit_reached"
        assert _counter_count(db) == 5

        # ------------------------------------------------------------------
        # FRESH DAY (time controlled): a terminal worker failure refunds —
        # entry refunded, counter decremented, immediate re-reserve works.
        # ------------------------------------------------------------------
        fresh = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")
        monkeypatch.setattr(ql, "_today_key", lambda: fresh)
        monkeypatch.setattr(usage_service, "_today_key", lambda: fresh)

        first = client.post("/api/resume/tailor",
                            json={"jd_analysis": _jd_payload(7)},
                            headers=headers)
        assert first.status_code == 202, first.get_data(as_text=True)
        assert first.get_json()["usage"]["used"] == 1
        job_id = first.get_json()["job_id"]
        entry_id = db.resume_jobs.find_one(
            {"job_id": job_id})["payload"]["quota_entry_id"]

        fake_llm.fail_with(RuntimeError("bedrock unavailable"))
        _run_next_worker(dispatched)

        job = db.resume_jobs.find_one({"job_id": job_id})
        assert job["status"] == "failed"
        entry = db.quota_ledger.find_one({"entry_id": entry_id})
        assert entry["state"] == "refunded"            # reserved -> refunded
        assert _counter_count(db, date=fresh) == 0     # slot given back
        assert usage_service.get_usage(EMAIL)["used"] == 0

        # The user can IMMEDIATELY reserve again — and this one completes.
        fake_llm.fail_with(None)
        again = client.post("/api/resume/tailor",
                            json={"jd_analysis": _jd_payload(8)},
                            headers=headers)
        assert again.status_code == 202, again.get_data(as_text=True)
        assert again.get_json()["usage"]["used"] == 1
        _run_next_worker(dispatched)
        done = db.resume_jobs.find_one({"job_id": again.get_json()["job_id"]})
        assert done["status"] == "completed", done.get("error")
        assert _counter_count(db, date=fresh) == 1     # consumed, not refunded
        # complete_job drops the payload; the ledger row is linked by job_id.
        retry_entry = db.quota_ledger.find_one(
            {"job_id": again.get_json()["job_id"]})
        assert retry_entry["state"] == "completed"
