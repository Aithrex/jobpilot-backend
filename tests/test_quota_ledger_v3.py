"""Resume Platform v3 — Phase B test coverage (spec 1.5 / 1.6 / 2).

Covers:
  * atomic tailor quota: guarded reserve (exactly one winner at the boundary),
    refund-on-terminal-failure idempotency, no feature_usage double count;
  * batch-tailor: free-plan 403 upgrade_required + all-or-nothing reserve;
  * worker-persisted tailoring records + idempotent POST /save-record;
  * bounded refinement: 409 version_limit_reached on the 6th version;
  * regenerate never consumes quota (and respects the version bound);
  * tailoring-record delete: per-object S3 cleanup, deletion_pending on
    failure (Mongo doc survives), clean delete otherwise.

Offline: mongomock via _vg_support; S3 faked; no Bedrock (workers are
monkeypatched at the tailor boundary).
"""
import uuid as _uuid
from datetime import datetime, timezone

import pytest

from _vg_support import load_service_app  # noqa: F401
from utils import db_connect

from services import quota_ledger_service as ql
from services import usage_service

EMAIL = "quota.user@example.com"

COLLECTIONS = (
    "users",
    "resume_jobs",
    "resume_assets",
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


def _structured():
    return {
        "contact": {"name": "Jordan Alvarez", "email": "j@example.com",
                    "phone": "555", "linkedin": "x", "github": "x",
                    "location": "Tempe, AZ"},
        "summary": "Backend engineer.",
        "skills": {"Languages": ["Python"]},
        "experience": [{"title": "Engineer", "company": "Acme",
                        "dates": "Jan 2020 - Present", "bullets": ["Built APIs"]}],
        "education": [], "projects": [], "certifications": [],
    }


def _tailored(marker="v1"):
    t = _structured()
    t["summary"] = f"Tailored summary {marker}"
    return t


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
def resume_app(db, monkeypatch):
    import services.resume_service as rs
    monkeypatch.setattr(rs.ResumeService, "invoke_async",
                        staticmethod(lambda *a, **k: None))
    monkeypatch.delenv("AWS_LAMBDA_FUNCTION_NAME", raising=False)
    return load_service_app("resume")


def _headers(app, email=EMAIL):
    from flask_jwt_extended import create_access_token
    with app.app_context():
        token = create_access_token(identity=email)
    return {"Authorization": f"Bearer {token}"}


def _counter_count(db, email=EMAIL):
    doc = db.quota_counters.find_one({"_id": ql.counter_id_for(email)})
    return int((doc or {}).get("count", 0))


# ===========================================================================
# Ledger unit semantics (spec 1.6)
# ===========================================================================

class TestReserveAtomicity:
    def test_reserve_boundary_exactly_one_wins(self, db):
        db.users.insert_one({"email": EMAIL, "plan": "free"})  # limit 5
        # Counter already at limit-1: two competing reserves — the guarded
        # find_one_and_update lets exactly ONE through.
        db.quota_counters.insert_one({
            "_id": ql.counter_id_for(EMAIL), "count": 4,
            "limit_snapshot": 5, "updated_at": datetime.now(timezone.utc),
        })
        first_ok, first_entries, first_usage = ql.reserve_tailor(EMAIL)
        second_ok, second_entries, second_usage = ql.reserve_tailor(EMAIL)
        assert [first_ok, second_ok].count(True) == 1
        assert _counter_count(db) == 5          # never above the limit
        assert len(first_entries) == 1 and second_entries == []
        assert first_usage["used"] == 5 and first_usage["remaining"] == 0
        assert second_usage["remaining"] == 0   # pre-attempt state for the 429

    def test_missing_counter_doc_is_created_atomically(self, db):
        db.users.insert_one({"email": EMAIL, "plan": "free"})
        ok, entries, usage = ql.reserve_tailor(EMAIL)
        assert ok and len(entries) == 1
        assert _counter_count(db) == 1
        assert usage["used"] == 1 and usage["limit"] == 5
        entry = db.quota_ledger.find_one({"entry_id": entries[0]})
        assert entry["state"] == "reserved" and entry["feature"] == "tailor"

    def test_idempotency_key_never_double_reserves(self, db):
        db.users.insert_one({"email": EMAIL, "plan": "free"})
        ok1, e1, _ = ql.reserve_tailor(EMAIL, idempotency_key="retry-abc")
        ok2, e2, _ = ql.reserve_tailor(EMAIL, idempotency_key="retry-abc")
        assert ok1 and ok2 and e1 == e2
        assert _counter_count(db) == 1
        assert db.quota_ledger.count_documents({}) == 1

    def test_complete_then_refund_never_decrements(self, db):
        db.users.insert_one({"email": EMAIL, "plan": "free"})
        _, entries, _ = ql.reserve_tailor(EMAIL)
        assert ql.complete_entry(entries[0], record_id="rec-1") is True
        # A completed slot is CONSUMED — refund must be a no-op.
        assert ql.refund_entry(entries[0]) is False
        assert _counter_count(db) == 1
        entry = db.quota_ledger.find_one({"entry_id": entries[0]})
        assert entry["state"] == "completed" and entry["record_id"] == "rec-1"


class TestRefundIdempotency:
    def test_refund_on_terminal_failure_is_idempotent(self, db):
        db.users.insert_one({"email": EMAIL, "plan": "free"})
        _, entries, _ = ql.reserve_tailor(EMAIL)
        eid = entries[0]

        import services.resume_service as rs
        svc = rs.get_resume_service()
        job_id = svc.create_job(
            "tailor",
            {"user_email": EMAIL, "jd_analysis": {}, "quota_entry_id": eid},
            user_email=EMAIL,
        )
        svc.fail_job(job_id, "terminal internal failure")

        assert _counter_count(db) == 0          # slot given back
        entry = db.quota_ledger.find_one({"entry_id": eid})
        assert entry["state"] == "refunded"

        # Repeated failure signals (Lambda at-least-once) never double-refund.
        svc.fail_job(job_id, "terminal internal failure (retry)")
        assert ql.refund_entry(eid) is False
        assert _counter_count(db) == 0

    def test_refund_floor_never_goes_negative(self, db):
        db.users.insert_one({"email": EMAIL, "plan": "free"})
        _, entries, _ = ql.reserve_tailor(EMAIL)
        # Simulate an out-of-band reset, then refund: floored at 0.
        db.quota_counters.update_one(
            {"_id": ql.counter_id_for(EMAIL)}, {"$set": {"count": 0}})
        assert ql.refund_entry(entries[0]) is True
        assert _counter_count(db) == 0


# ===========================================================================
# Routes — reserve before dispatch, 429 shape, no feature_usage double count
# ===========================================================================

class TestTailorRoutes:
    def test_tailor_reserves_and_sixth_returns_daily_quota_shape(
            self, db, resume_app):
        db.users.insert_one({"email": EMAIL, "plan": "free"})
        client = resume_app.test_client()
        headers = _headers(resume_app)
        for i in range(5):
            resp = client.post("/api/resume/tailor",
                               json={"jd_analysis": {"job_title": f"Role {i}"}},
                               headers=headers)
            assert resp.status_code == 202, resp.get_data(as_text=True)
            assert resp.get_json()["usage"]["used"] == i + 1
        sixth = client.post("/api/resume/tailor",
                            json={"jd_analysis": {"job_title": "Role 6"}},
                            headers=headers)
        assert sixth.status_code == 429
        body = sixth.get_json()
        assert body["error"] == "daily_quota_exceeded"     # shape unchanged
        assert body["usage"]["used"] == 5 and body["usage"]["remaining"] == 0
        assert "message" in body

        # The reservation entry_id rides the async payload.
        job = db.resume_jobs.find_one({"job_type": "tailor"})
        eid = job["payload"]["quota_entry_id"]
        assert db.quota_ledger.find_one({"entry_id": eid})["job_id"] == job["job_id"]

        # feature_usage still does NOT meter tailor (no double count).
        assert db.feature_usage.count_documents({"feature": "tailor"}) == 0
        # The QuotaMeter reader agrees with the atomic counter.
        assert usage_service.get_usage(EMAIL)["used"] == 5

    def test_tailor_with_jd_reserves(self, db, resume_app):
        db.users.insert_one({"email": EMAIL, "plan": "free"})
        resp = resume_app.test_client().post(
            "/api/resume/tailor-with-jd",
            json={"job_description": "We need a backend engineer with Python."},
            headers=_headers(resume_app))
        assert resp.status_code == 202
        assert _counter_count(db) == 1
        job = db.resume_jobs.find_one({"job_type": "extract_and_tailor"})
        assert job["payload"]["quota_entry_id"]

    def test_regenerate_consumes_no_quota(self, db, resume_app):
        db.users.insert_one({"email": EMAIL, "plan": "free"})
        resp = resume_app.test_client().post(
            "/api/resume/regenerate",
            json={
                "jd_analysis": {"job_title": "Role"},
                "tailored_resume": _tailored(),
                "user_feedback": "tighten the summary",
            },
            headers=_headers(resume_app))
        assert resp.status_code == 202
        assert _counter_count(db) == 0                    # refinement is free
        assert db.quota_ledger.count_documents({}) == 0
        assert db.tailor_usage.count_documents({}) == 0


class TestBatchTailor:
    def _jds(self, n):
        return [{"text": f"JD number {i} for a python role", "title": f"T{i}"}
                for i in range(n)]

    def test_free_plan_gets_403_upgrade_required(self, db, resume_app):
        db.users.insert_one({"email": EMAIL, "plan": "free"})
        resp = resume_app.test_client().post(
            "/api/resume/batch-tailor", json={"jd_list": self._jds(2)},
            headers=_headers(resume_app))
        assert resp.status_code == 403
        body = resp.get_json()
        assert body["error"] == "upgrade_required"
        assert body["feature"] == "batch_tailor"
        assert _counter_count(db) == 0                    # gate BEFORE reserve

    def test_batch_reserve_is_all_or_nothing(self, db, resume_app):
        db.users.insert_one(
            {"email": EMAIL, "plan": "premium", "daily_tailor_limit": 3})
        client = resume_app.test_client()
        headers = _headers(resume_app)

        too_big = client.post("/api/resume/batch-tailor",
                              json={"jd_list": self._jds(5)}, headers=headers)
        assert too_big.status_code == 429
        body = too_big.get_json()
        assert body["error"] == "daily_quota_exceeded"
        assert body["usage"]["requested"] == 5
        assert _counter_count(db) == 0                    # nothing partial
        assert db.quota_ledger.count_documents({}) == 0

        fits = client.post("/api/resume/batch-tailor",
                           json={"jd_list": self._jds(2)}, headers=headers)
        assert fits.status_code == 202
        assert len(fits.get_json()["jobs"]) == 2
        assert _counter_count(db) == 2
        entries = list(db.quota_ledger.find({}))
        assert len(entries) == 2
        assert all(e["state"] == "reserved" for e in entries)
        # Every dispatched item carries its own entry_id.
        payload_eids = {
            j["payload"]["quota_entry_id"]
            for j in db.resume_jobs.find({"job_type": "batch_tailor_item"})
        }
        assert payload_eids == {e["entry_id"] for e in entries}


# ===========================================================================
# Worker-persisted records + idempotent /save-record (spec 1.5)
# ===========================================================================

class TestWorkerPersistedRecord:
    def _run_tailor_worker(self, db, monkeypatch, entry_id):
        import services.resume_service as rs
        import services.profile_facts as pf
        import services.edit_signals as es

        svc = rs.get_resume_service()
        resume_doc = {
            "structured": _structured(), "raw_text": "",
            "filename": "base.pdf", "s3_key": "resume-assets/base/original",
        }
        monkeypatch.setattr(svc.parser, "ensure_structured_resume",
                            lambda user_email="": resume_doc)
        monkeypatch.setattr(svc.tailor, "tailor",
                            lambda *a, **k: _tailored("worker"))
        monkeypatch.setattr(pf, "build_fact_store", lambda **k: {"facts": []})
        monkeypatch.setattr(es, "past_edits_context", lambda e: "")
        monkeypatch.setattr(rs, "_project_policy", lambda *a, **k: {
            "allow": False, "evidence_only": False, "reason": "test",
            "github_evidence": [],
        })
        monkeypatch.setattr(rs, "_finalize_tailored_result",
                            lambda svc, job_id, result, *a, **k: result)

        payload = {
            "user_email": EMAIL,
            "jd_analysis": {"job_title": "Backend Engineer"},
            "quota_entry_id": entry_id,
        }
        job_id = svc.create_job("tailor", payload, user_email=EMAIL)
        rs._process_job(job_id, "tailor", payload)
        return job_id

    def test_worker_persists_record_and_settles_quota(self, db, monkeypatch):
        db.users.insert_one({"email": EMAIL, "plan": "free"})
        _, entries, _ = ql.reserve_tailor(EMAIL)
        job_id = self._run_tailor_worker(db, monkeypatch, entries[0])

        job = db.resume_jobs.find_one({"job_id": job_id})
        assert job["status"] == "completed"
        record_id = job["result"]["record_id"]
        assert record_id

        record = db.tailoring_records.find_one({"record_id": record_id})
        assert record["user_email"] == EMAIL
        assert record["source"] == "tailor"
        assert len(record["versions"]) == 1
        assert record["versions"][0]["source"] == "initial"
        assert record["current_version_id"] == record["versions"][0]["version_id"]

        # Quota settled: reserved -> completed, linked to the record.
        entry = db.quota_ledger.find_one({"entry_id": entries[0]})
        assert entry["state"] == "completed"
        assert entry["record_id"] == record_id
        assert _counter_count(db) == 1                    # consumed, not refunded

    def test_save_record_is_idempotent_by_record_id(
            self, db, resume_app, monkeypatch):
        db.users.insert_one({"email": EMAIL, "plan": "free"})
        _, entries, _ = ql.reserve_tailor(EMAIL)
        job_id = self._run_tailor_worker(db, monkeypatch, entries[0])
        record_id = db.resume_jobs.find_one({"job_id": job_id})["result"]["record_id"]

        streaks = []
        import services.streak_service as ss
        monkeypatch.setattr(ss, "record_application",
                            lambda email, tz=None: streaks.append(email))

        client = resume_app.test_client()
        headers = _headers(resume_app)
        ats = {"overall": 88}

        first = client.post("/api/resume/save-record",
                            json={"record_id": record_id, "ats_scores": ats},
                            headers=headers)
        assert first.status_code == 200
        assert first.get_json() == {"record_id": record_id, "updated": True}
        again = client.post("/api/resume/save-record",
                            json={"record_id": record_id, "ats_scores": ats},
                            headers=headers)
        assert again.status_code == 200

        # No duplicate record; ATS attached to the current version + top level.
        assert db.tailoring_records.count_documents({"user_email": EMAIL}) == 1
        record = db.tailoring_records.find_one({"record_id": record_id})
        assert record["ats_scores"] == ats
        assert record["versions"][0]["ats_scores"] == ats
        # Streak fired exactly once per record.
        assert streaks == [EMAIL]

    def test_save_record_without_id_dedups_by_content_hash(
            self, db, resume_app, monkeypatch):
        db.users.insert_one({"email": EMAIL, "plan": "free"})
        _, entries, _ = ql.reserve_tailor(EMAIL)
        job_id = self._run_tailor_worker(db, monkeypatch, entries[0])
        record_id = db.resume_jobs.find_one({"job_id": job_id})["result"]["record_id"]

        import services.streak_service as ss
        monkeypatch.setattr(ss, "record_application", lambda email, tz=None: None)

        # A legacy client that never learned record_id re-posts the SAME
        # tailored content — must settle onto the worker's record.
        resp = resume_app.test_client().post(
            "/api/resume/save-record",
            json={
                "jd_analysis": {"job_title": "Backend Engineer"},
                "tailored_resume": _tailored("worker"),
            },
            headers=_headers(resume_app))
        assert resp.status_code == 200
        assert resp.get_json()["record_id"] == record_id
        assert db.tailoring_records.count_documents({"user_email": EMAIL}) == 1


# ===========================================================================
# Bounded refinement — 409 on the 6th version (spec 1.5)
# ===========================================================================

def _seed_record(db, n_versions, email=EMAIL):
    from services.resume_versioning import build_initial_version, append_version
    versions = [build_initial_version(_tailored("v0"))]
    for i in range(1, n_versions):
        append_version(versions, _tailored(f"v{i}"), source="edited")
    rid = str(_uuid.uuid4())
    db.tailoring_records.insert_one({
        "record_id": rid, "user_email": email,
        "jd_analysis": {"job_title": "Role"},
        "tailored_resume": versions[-1]["tailored_resume"],
        "versions": versions,
        "current_version_id": versions[-1]["version_id"],
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    })
    return rid


class TestVersionBound:
    def test_sixth_version_returns_409(self, db, resume_app):
        db.users.insert_one({"email": EMAIL, "plan": "free"})
        rid = _seed_record(db, 5)
        resp = resume_app.test_client().post(
            f"/api/resume/tailoring-records/{rid}/versions",
            json={"tailored_resume": _tailored("v6"), "source": "edited"},
            headers=_headers(resume_app))
        assert resp.status_code == 409
        assert resp.get_json() == {"error": "version_limit_reached", "limit": 5}
        record = db.tailoring_records.find_one({"record_id": rid})
        assert len(record["versions"]) == 5               # unchanged

    def test_identical_content_resave_stays_idempotent_at_cap(
            self, db, resume_app):
        db.users.insert_one({"email": EMAIL, "plan": "free"})
        rid = _seed_record(db, 5)
        resp = resume_app.test_client().post(
            f"/api/resume/tailoring-records/{rid}/versions",
            json={"tailored_resume": _tailored("v4"), "source": "edited"},
            headers=_headers(resume_app))
        assert resp.status_code == 200                    # no-op, not a 409
        assert resp.get_json()["created"] is False

    def test_regenerate_within_capped_record_returns_409(self, db, resume_app):
        db.users.insert_one({"email": EMAIL, "plan": "free"})
        rid = _seed_record(db, 5)
        resp = resume_app.test_client().post(
            "/api/resume/regenerate",
            json={
                "record_id": rid,
                "jd_analysis": {"job_title": "Role"},
                "tailored_resume": _tailored("v4"),
                "user_feedback": "one more pass",
            },
            headers=_headers(resume_app))
        assert resp.status_code == 409
        assert resp.get_json()["error"] == "version_limit_reached"
        assert _counter_count(db) == 0                    # still no quota spend


# ===========================================================================
# Tailoring-record delete — S3 cleanup first (spec 2)
# ===========================================================================

class _RecordingStorage:
    def __init__(self, fail_keys=()):
        self.fail_keys = set(fail_keys)
        self.deleted = []

    def delete_resume(self, s3_key):
        if s3_key in self.fail_keys:
            raise RuntimeError("s3 down")
        self.deleted.append(s3_key)
        return True


class TestRecordDelete:
    def _seed_with_files(self, db):
        rid = _seed_record(db, 2)
        record = db.tailoring_records.find_one({"record_id": rid})
        versions = record["versions"]
        versions[0]["files"] = {
            "pdf": {"s3_key": f"tailoring/{rid}/v1/r.pdf"},
            "docx": {"s3_key": f"tailoring/{rid}/v1/r.docx"},
        }
        versions[1]["files"] = {"pdf": {"s3_key": f"tailoring/{rid}/v2/r.pdf"}}
        db.tailoring_records.update_one(
            {"record_id": rid}, {"$set": {"versions": versions}})
        return rid

    def test_clean_delete_removes_every_object_then_doc(
            self, db, resume_app, monkeypatch):
        db.users.insert_one({"email": EMAIL, "plan": "free"})
        rid = self._seed_with_files(db)
        fake = _RecordingStorage()
        import services.s3_service as s3s
        monkeypatch.setattr(s3s, "get_storage_service", lambda: fake)

        resp = resume_app.test_client().delete(
            f"/api/resume/tailoring-records/{rid}",
            headers=_headers(resume_app))
        assert resp.status_code == 200
        assert resp.get_json()["files_deleted"] == 3
        assert sorted(fake.deleted) == sorted([
            f"tailoring/{rid}/v1/r.pdf",
            f"tailoring/{rid}/v1/r.docx",
            f"tailoring/{rid}/v2/r.pdf",
        ])
        assert db.tailoring_records.find_one({"record_id": rid}) is None

    def test_s3_failure_marks_deletion_pending_and_keeps_doc(
            self, db, resume_app, monkeypatch):
        db.users.insert_one({"email": EMAIL, "plan": "free"})
        rid = self._seed_with_files(db)
        fake = _RecordingStorage(fail_keys={f"tailoring/{rid}/v1/r.docx"})
        import services.s3_service as s3s
        monkeypatch.setattr(s3s, "get_storage_service", lambda: fake)

        resp = resume_app.test_client().delete(
            f"/api/resume/tailoring-records/{rid}",
            headers=_headers(resume_app))
        assert resp.status_code == 502
        body = resp.get_json()
        assert body["error"] == "deletion_pending"
        assert body["files_pending"] == 1 and body["files_deleted"] == 2
        record = db.tailoring_records.find_one({"record_id": rid})
        assert record is not None                          # doc survives
        assert record["deletion_pending"] is True          # sweep will finish

    def test_foreign_record_stays_404(self, db, resume_app):
        db.users.insert_one({"email": EMAIL, "plan": "free"})
        rid = _seed_record(db, 1, email="someone.else@example.com")
        resp = resume_app.test_client().delete(
            f"/api/resume/tailoring-records/{rid}",
            headers=_headers(resume_app))
        assert resp.status_code == 404
