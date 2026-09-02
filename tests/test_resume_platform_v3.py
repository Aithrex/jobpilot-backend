"""Resume Platform v3 — Phase A test coverage (spec 1.1 / 1.2 / 1.3 / 2 / 4 / 6).

Covers:
  * resume_assets creation on auth upload (Mongo-first write order, S3-key-only
    async payloads, dev-only inline fallback, Lambda 503 storage_unavailable);
  * active-pointer promotion + the stale-parse guard + PUT /active semantics;
  * GET /versions and DELETE /file/<resume_id> (delete-repoint);
  * bounded upload reads + the global 8MB / 413 JSON handler;
  * utils/zip_guard.py (DOCX bomb heuristics);
  * async job slimming: create_job idempotency_key + expire_at TTL stamps;
  * guest_parse result contract: resume_id + honestly-derived profile block.

Offline: mongomock via _vg_support; S3 is a fake; the LLM parser is
monkeypatched (no Bedrock).
"""
import base64
import io
import sys
import uuid as _uuid
import zipfile
from datetime import datetime, timedelta, timezone

import pytest

from _vg_support import REPO_ROOT, load_service_app  # noqa: F401
from utils import db_connect

from services import guest_service, resume_access

PDF_BYTES = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n"

COLLECTIONS = (
    "resume_assets",
    "resume_jobs",
    "user_resumes",
    "users",
    "guest_sessions",
    "guest_quota_subjects",
    "guest_global_counters",
    "platform_config",
)


def _structured(name="Jordan Alvarez"):
    return {
        "contact": {"name": name, "email": "jordan.alvarez@example.com",
                    "phone": "(555) 010-7788", "linkedin": "", "github": "",
                    "location": "Tempe, AZ"},
        "summary": "Backend engineer building APIs.",
        "skills": {"Languages": ["Python", "Go", "SQL"]},
        "experience": [{
            "title": "Backend Engineer", "company": "Acme",
            "dates": "Jan 2020 - Present", "bullets": ["Built APIs in Python"],
        }],
        "education": [],
        "projects": [],
        "certifications": [],
    }


class _FakeStorage:
    def __init__(self, fail_upload=False):
        self.fail_upload = fail_upload
        self.assets = []     # (resume_id, mime_type)
        self.fetched = []    # keys
        self.deleted = []    # keys

    def upload_resume_asset(self, resume_id, file_bytes, mime_type):
        if self.fail_upload:
            raise ValueError("RESUME_S3_BUCKET is not configured")
        self.assets.append((resume_id, mime_type))
        return f"resume-assets/{resume_id}/original"

    def get_resume(self, s3_key):
        self.fetched.append(s3_key)
        return PDF_BYTES

    def delete_resume(self, s3_key):
        self.deleted.append(s3_key)
        return True


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
    fake = _FakeStorage()
    import services.s3_service as s3s
    monkeypatch.setattr(s3s, "get_storage_service", lambda: fake)
    return fake


@pytest.fixture()
def resume_app(db, monkeypatch):
    import services.resume_service as rs
    monkeypatch.setattr(rs.ResumeService, "invoke_async",
                        staticmethod(lambda *a, **k: None))
    monkeypatch.delenv("AWS_LAMBDA_FUNCTION_NAME", raising=False)
    return load_service_app("resume")


def _headers(app, email="upload.user@example.com"):
    from flask_jwt_extended import create_access_token
    with app.app_context():
        token = create_access_token(identity=email)
    return {"Authorization": f"Bearer {token}"}


def _post_upload(app, headers, filename="resume.pdf", body=PDF_BYTES):
    return app.test_client().post(
        "/api/resume/upload",
        data={"file": (io.BytesIO(body), filename)},
        content_type="multipart/form-data",
        headers=headers,
    )


# ===========================================================================
# zip_guard unit tests (spec section 4)
# ===========================================================================

def _make_zip(entries):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries:
            zf.writestr(name, data)
    return buf.getvalue()


class TestZipGuard:
    def test_normal_docx_shape_passes(self):
        from utils.zip_guard import guard_zip_archive
        data = _make_zip([
            ("[Content_Types].xml", b"<Types/>"),
            ("word/document.xml", b"<w:document>hello resume</w:document>" * 20),
            ("word/_rels/document.xml.rels", b"<Relationships/>"),
        ])
        guard_zip_archive(data)  # must not raise

    def test_too_many_entries_rejected(self):
        from utils.zip_guard import guard_zip_archive, ZipGuardError, MAX_ENTRIES
        data = _make_zip([(f"e/{i}.xml", b"x") for i in range(MAX_ENTRIES + 1)])
        with pytest.raises(ZipGuardError):
            guard_zip_archive(data)

    def test_total_uncompressed_bound_rejected(self):
        from utils.zip_guard import guard_zip_archive, ZipGuardError
        # 2 x 40MB of ones — total declared 80MB > 60MB cap. Compresses tiny,
        # but ALSO trips the per-entry ratio; either bound is a correct reject.
        data = _make_zip([
            ("word/a.xml", b"1" * (40 * 1024 * 1024)),
            ("word/b.xml", b"1" * (40 * 1024 * 1024)),
        ])
        with pytest.raises(ZipGuardError):
            guard_zip_archive(data)

    def test_compression_ratio_bomb_rejected(self):
        from utils.zip_guard import guard_zip_archive, ZipGuardError
        # 20MB of zeros deflates >1000x — classic bomb signature.
        data = _make_zip([("word/document.xml", b"\x00" * (20 * 1024 * 1024))])
        with pytest.raises(ZipGuardError):
            guard_zip_archive(data)

    def test_nested_container_rejected(self):
        from utils.zip_guard import guard_zip_archive, ZipGuardError
        inner = _make_zip([("x.txt", b"payload")])
        data = _make_zip([("word/embedded.zip", inner)])
        with pytest.raises(ZipGuardError):
            guard_zip_archive(data)

    def test_garbage_bytes_rejected(self):
        from utils.zip_guard import guard_zip_archive, ZipGuardError
        with pytest.raises(ZipGuardError):
            guard_zip_archive(b"PK\x03\x04 but not really a zip")

    def test_upload_route_rejects_docx_bomb(self, db, storage, resume_app):
        bomb = _make_zip([("word/document.xml", b"\x00" * (20 * 1024 * 1024))])
        resp = _post_upload(resume_app, _headers(resume_app),
                            filename="resume.docx", body=bomb)
        assert resp.status_code == 400
        assert db.resume_assets.count_documents({}) == 0  # rejected pre-asset


# ===========================================================================
# Auth upload — asset creation + transport-only payloads (spec 1.1 / 1.3 / 2)
# ===========================================================================

class TestAuthUploadAssets:
    EMAIL = "upload.user@example.com"

    def _seed_user(self, db):
        db.users.insert_one({"email": self.EMAIL, "plan": "free"})

    def test_upload_creates_pending_asset_then_object_key(
            self, db, storage, resume_app):
        self._seed_user(db)
        resp = _post_upload(resume_app, _headers(resume_app))
        assert resp.status_code == 202, resp.get_data(as_text=True)
        body = resp.get_json()
        rid = body["resume_id"]
        assert body["job_id"]

        asset = db.resume_assets.find_one({"resume_id": rid})
        assert asset["owner_type"] == "user"
        assert asset["user_email"] == self.EMAIL
        assert asset["parse_status"] == "pending"
        assert asset["scan_status"] == "unscanned"
        assert asset["object_key"] == f"resume-assets/{rid}/original"
        assert asset["filename"] == "resume.pdf"  # Mongo only, not in the key
        assert asset["content_hash"] and asset["size_bytes"] == len(PDF_BYTES)
        assert storage.assets == [(rid, "application/pdf")]

        # Transport-only payload: S3 key, never bytes (spec 1.3).
        row = db.resume_jobs.find_one({"job_id": body["job_id"]})
        payload = row["payload"]
        assert payload["object_key"] == asset["object_key"]
        for forbidden in ("file_base64", "pdf_base64", "file_bytes_b64_dev"):
            assert forbidden not in payload

        # Active-candidate stamped for the worker's guarded promotion.
        user = db.users.find_one({"email": self.EMAIL})
        assert user["pending_active_resume_id"] == rid

    def test_upload_s3_down_on_lambda_returns_503(
            self, db, resume_app, monkeypatch):
        self._seed_user(db)
        import services.s3_service as s3s
        monkeypatch.setattr(s3s, "get_storage_service",
                            lambda: _FakeStorage(fail_upload=True))
        monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "jobpilot-resume-prod")
        resp = _post_upload(resume_app, _headers(resume_app))
        assert resp.status_code == 503
        assert resp.get_json()["error"] == "storage_unavailable"
        asset = db.resume_assets.find_one({})
        assert asset["parse_status"] == "failed"  # marked, never orphaned

    def test_upload_s3_down_dev_falls_back_to_inline_dev_key(
            self, db, resume_app, monkeypatch):
        self._seed_user(db)
        import services.s3_service as s3s
        monkeypatch.setattr(s3s, "get_storage_service",
                            lambda: _FakeStorage(fail_upload=True))
        resp = _post_upload(resume_app, _headers(resume_app))
        assert resp.status_code == 202
        row = db.resume_jobs.find_one({"job_id": resp.get_json()["job_id"]})
        assert "object_key" not in row["payload"]
        assert row["payload"]["file_bytes_b64_dev"]  # dev-only fallback key

    def test_upload_bounded_read_rejects_oversize_body(
            self, db, storage, resume_app):
        self._seed_user(db)
        big = b"%PDF-1.4\n" + b"a" * (6 * 1024 * 1024)  # >5MB cap, <8MB global
        resp = _post_upload(resume_app, _headers(resume_app), body=big)
        assert resp.status_code == 400
        assert "too large" in resp.get_json()["error"].lower()
        assert db.resume_assets.count_documents({}) == 0

    def test_global_8mb_cap_returns_json_413(self, db, resume_app):
        # A route that parses the body without its own pre-check: the global
        # MAX_CONTENT_LENGTH must reject with the JSON 413 shape.
        headers = _headers(resume_app)
        headers["Content-Type"] = "application/json"
        resp = resume_app.test_client().post(
            "/api/resume/extract-jd",
            data=b'{"job_description": "' + b"x" * (9 * 1024 * 1024) + b'"}',
            headers=headers,
        )
        assert resp.status_code == 413
        assert resp.get_json() == {"error": "file_too_large"}

    def test_duplicate_upload_reuses_parsed_asset(self, db, storage, resume_app):
        self._seed_user(db)
        headers = _headers(resume_app)
        first = _post_upload(resume_app, headers)
        rid = first.get_json()["resume_id"]
        # Simulate the worker having parsed the asset.
        resume_access.save_parsed_fields(
            rid, resume_access.build_parsed_fields(_structured(), "raw text " * 20)
        )
        again = _post_upload(resume_app, headers)
        assert again.status_code == 202
        body = again.get_json()
        assert body["cached"] is True and body["resume_id"] == rid
        # Pointer re-points at the existing asset; no second asset created.
        assert db.users.find_one({"email": self.EMAIL})["active_resume_id"] == rid
        assert db.resume_assets.count_documents({}) == 1
        job = db.resume_jobs.find_one({"job_id": body["job_id"]})
        assert job["status"] == "completed"
        assert job["result"]["resume_id"] == rid


# ===========================================================================
# Worker parse + active-pointer promotion (spec 1.2)
# ===========================================================================

class TestWorkerPromotion:
    EMAIL = "promote.user@example.com"

    def _upload(self, db, resume_app, headers):
        resp = _post_upload(resume_app, headers)
        assert resp.status_code == 202
        body = resp.get_json()
        row = db.resume_jobs.find_one({"job_id": body["job_id"]})
        return body["resume_id"], body["job_id"], row["payload"]

    def _patch_parser(self, monkeypatch, structured=None):
        import services.resume_service as rs
        svc = rs.get_resume_service()
        monkeypatch.setattr(
            svc.parser, "parse_to_structured",
            lambda *a, **k: (structured or _structured(), "Jordan Alvarez raw resume text " * 5),
        )
        return rs, svc

    def test_worker_fetches_s3_parses_into_asset_and_promotes(
            self, db, storage, resume_app, monkeypatch):
        db.users.insert_one({"email": self.EMAIL, "plan": "free"})
        headers = _headers(resume_app, self.EMAIL)
        rid, job_id, payload = self._upload(db, resume_app, headers)
        rs, svc = self._patch_parser(monkeypatch)

        phases = []
        monkeypatch.setattr(
            svc, "update_job_progress",
            lambda jid, phase, pct=None: phases.append((phase, pct)),
        )
        rs._process_job(job_id, "upload_parse", payload)

        # Worker fetched the bytes from S3 by key (no bytes in the payload).
        assert storage.fetched == [f"resume-assets/{rid}/original"]
        # Honest progress phases (spec 1.3 / 3.4).
        assert phases == [("parsing", 20), ("extracting_skills", 60), ("finalizing", 90)]

        asset = db.resume_assets.find_one({"resume_id": rid})
        assert asset["parse_status"] == "parsed"
        assert asset["structured"]["contact"]["name"] == "Jordan Alvarez"
        assert asset["skills"] and asset["experience_years"] >= 1

        user = db.users.find_one({"email": self.EMAIL})
        assert user["active_resume_id"] == rid
        assert "pending_active_resume_id" not in user

        job = db.resume_jobs.find_one({"job_id": job_id})
        assert job["status"] == "completed"
        assert job["result"]["resume_id"] == rid
        assert job["expire_at"] is not None

        # The canonical accessor resolves the pointer.
        doc = resume_access.get_active_structured(self.EMAIL)
        assert doc["resume_id"] == rid and doc["structured"]

        # And the legacy delegate keeps its shape for the 35+ call sites.
        from services.resume_parser import ResumeParser
        legacy = ResumeParser(db).get_structured_resume(user_email=self.EMAIL)
        assert legacy["structured"] == doc["structured"]
        assert "s3_key" in legacy and "_id" not in legacy

    def test_stale_parse_never_overwrites_user_choice(
            self, db, storage, resume_app, monkeypatch):
        # User already has an explicitly-chosen active resume C.
        db.users.insert_one({"email": self.EMAIL, "active_resume_id": "asset-c"})
        db.resume_assets.insert_one({
            "resume_id": "asset-c", "owner_type": "user",
            "user_email": self.EMAIL, "parse_status": "parsed",
            "structured": {"summary": "chosen"},
            "created_at": datetime.now(timezone.utc) - timedelta(days=1),
        })
        headers = _headers(resume_app, self.EMAIL)
        rid_a, job_a, payload_a = self._upload(db, resume_app, headers)
        rid_b, job_b, payload_b = self._upload(db, resume_app, headers)
        rs, _ = self._patch_parser(monkeypatch)

        # The pending stamp now points at B; completing A must NOT steal the
        # pointer (stale-parse guard) — the user still has C active.
        rs._process_job(job_a, "upload_parse", payload_a)
        assert db.users.find_one({"email": self.EMAIL})["active_resume_id"] == "asset-c"

        # Completing B (the current candidate) promotes it.
        rs._process_job(job_b, "upload_parse", payload_b)
        user = db.users.find_one({"email": self.EMAIL})
        assert user["active_resume_id"] == rid_b
        assert "pending_active_resume_id" not in user

    def test_put_active_clears_pending_so_late_parse_cannot_override(
            self, db, storage, resume_app, monkeypatch):
        db.users.insert_one({"email": self.EMAIL})
        db.resume_assets.insert_one({
            "resume_id": "asset-c", "owner_type": "user",
            "user_email": self.EMAIL, "parse_status": "parsed",
            "structured": {"summary": "chosen"},
            "created_at": datetime.now(timezone.utc),
        })
        headers = _headers(resume_app, self.EMAIL)
        rid, job_id, payload = self._upload(db, resume_app, headers)

        # While the parse is in flight the user explicitly activates C.
        resp = resume_app.test_client().put(
            "/api/resume/active", json={"resume_id": "asset-c"}, headers=headers)
        assert resp.status_code == 200
        assert "pending_active_resume_id" not in db.users.find_one({"email": self.EMAIL})

        rs, _ = self._patch_parser(monkeypatch)
        rs._process_job(job_id, "upload_parse", payload)
        # Parse completed fine, but the pointer stays on the user's choice.
        assert db.resume_assets.find_one({"resume_id": rid})["parse_status"] == "parsed"
        assert db.users.find_one({"email": self.EMAIL})["active_resume_id"] == "asset-c"

    def test_failed_parse_marks_asset_and_never_promotes(
            self, db, storage, resume_app, monkeypatch):
        db.users.insert_one({"email": self.EMAIL})
        headers = _headers(resume_app, self.EMAIL)
        rid, job_id, payload = self._upload(db, resume_app, headers)
        import services.resume_service as rs
        svc = rs.get_resume_service()
        monkeypatch.setattr(svc.parser, "parse_to_structured",
                            lambda *a, **k: (_structured(), "tiny"))  # <50 chars
        rs._process_job(job_id, "upload_parse", payload)

        asset = db.resume_assets.find_one({"resume_id": rid})
        assert asset["parse_status"] == "failed"
        user = db.users.find_one({"email": self.EMAIL})
        assert user.get("active_resume_id") is None
        assert "pending_active_resume_id" not in user
        job = db.resume_jobs.find_one({"job_id": job_id})
        assert job["status"] == "failed" and job["expire_at"] is not None


# ===========================================================================
# /versions + PUT /active + DELETE /file/<resume_id> (spec 1.2 / 5)
# ===========================================================================

class TestVersionsActiveDeleteRoutes:
    EMAIL = "routes.user@example.com"

    def _seed_assets(self, db):
        now = datetime.now(timezone.utc)
        db.users.insert_one({"email": self.EMAIL, "active_resume_id": "a2"})
        for rid, status, age_days in (("a1", "parsed", 2), ("a2", "parsed", 1),
                                      ("a3", "pending", 0)):
            db.resume_assets.insert_one({
                "resume_id": rid, "owner_type": "user",
                "user_email": self.EMAIL, "filename": f"{rid}.pdf",
                "size_bytes": 1234, "parse_status": status,
                "structured": {"summary": rid} if status == "parsed" else None,
                "object_key": f"resume-assets/{rid}/original",
                "created_at": now - timedelta(days=age_days),
            })

    def test_versions_lists_assets_with_active_flag(self, db, resume_app):
        self._seed_assets(db)
        resp = resume_app.test_client().get(
            "/api/resume/versions", headers=_headers(resume_app, self.EMAIL))
        assert resp.status_code == 200
        rows = resp.get_json()["versions"]
        assert [r["resume_id"] for r in rows] == ["a3", "a2", "a1"]  # newest first
        by_id = {r["resume_id"]: r for r in rows}
        assert by_id["a2"]["is_active"] is True
        assert by_id["a1"]["is_active"] is False
        assert by_id["a3"]["parse_status"] == "pending"
        assert set(rows[0]) == {"resume_id", "filename", "uploaded_at",
                                "size_bytes", "parse_status", "is_active",
                                "from_guest_session"}
        assert by_id["a2"]["from_guest_session"] is False

    def test_versions_lists_claimed_guest_assets(self, db, resume_app):
        """A resume uploaded as a guest and CLAIMED at signup must appear.

        Regression test. This route used to filter `owner_type == "user"`
        while `resume_access.list_parsed_assets` — the resolver behind
        tailoring, scoring and `base_resume_selector` — filters only on
        `user_email`. A claimed guest resume was therefore invisible in the
        library yet fully eligible for tailoring, so the product tailored from
        a file the user could neither see nor delete. Measured on live data
        (2026-09-02): 10 such assets across 6 accounts.

        An UNCLAIMED guest asset carries no `user_email` and must still be
        excluded — that is what keeps this from surfacing someone else's file.
        """
        self._seed_assets(db)
        now = datetime.now(timezone.utc)
        db.resume_assets.insert_one({
            "resume_id": "g1", "owner_type": "guest", "claim_status": "claimed",
            "user_email": self.EMAIL, "filename": "from_guest.pdf",
            "size_bytes": 999, "parse_status": "parsed",
            "structured": {"summary": "g1"},
            "object_key": "resume-assets/g1/original",
            "created_at": now - timedelta(days=3),
        })
        db.resume_assets.insert_one({
            "resume_id": "g2", "owner_type": "guest", "claim_status": "unclaimed",
            "user_email": None, "filename": "stranger.pdf",
            "size_bytes": 999, "parse_status": "parsed",
            "structured": {"summary": "g2"},
            "created_at": now - timedelta(days=4),
        })

        resp = resume_app.test_client().get(
            "/api/resume/versions", headers=_headers(resume_app, self.EMAIL))
        assert resp.status_code == 200
        ids = [r["resume_id"] for r in resp.get_json()["versions"]]
        assert "g1" in ids, "claimed guest resume must be listed"
        assert "g2" not in ids, "unclaimed guest resume must never be listed"

        by_id = {r["resume_id"]: r for r in resp.get_json()["versions"]}
        assert by_id["g1"]["from_guest_session"] is True

    def test_versions_matches_what_tailoring_can_use(self, db, resume_app):
        """The library and the tailor resolver must agree on ownership.

        The bug was not either query in isolation; it was that they disagreed.
        Pinning them together means a future change to one that forgets the
        other fails here rather than in production.
        """
        from services import resume_access

        self._seed_assets(db)
        now = datetime.now(timezone.utc)
        db.resume_assets.insert_one({
            "resume_id": "g1", "owner_type": "guest", "claim_status": "claimed",
            "user_email": self.EMAIL, "filename": "from_guest.pdf",
            "size_bytes": 999, "parse_status": "parsed",
            "structured": {"summary": "g1"},
            "object_key": "resume-assets/g1/original",
            "created_at": now - timedelta(days=3),
        })

        resp = resume_app.test_client().get(
            "/api/resume/versions", headers=_headers(resume_app, self.EMAIL))
        listed = {r["resume_id"] for r in resp.get_json()["versions"]
                  if r["parse_status"] == "parsed"}
        usable = {a["resume_id"] for a in resume_access.list_parsed_assets(self.EMAIL)}
        assert usable - listed == set(), (
            f"tailoring can use resumes the library hides: {usable - listed}")

    def test_put_active_validates_ownership_and_parse_state(self, db, resume_app):
        self._seed_assets(db)
        client = resume_app.test_client()
        headers = _headers(resume_app, self.EMAIL)

        assert client.put("/api/resume/active", json={},
                          headers=headers).status_code == 400
        assert client.put("/api/resume/active", json={"resume_id": "nope"},
                          headers=headers).status_code == 404
        # Someone else's asset → same 404 (no existence oracle).
        db.resume_assets.insert_one({
            "resume_id": "foreign", "owner_type": "user",
            "user_email": "other@example.com", "parse_status": "parsed",
        })
        assert client.put("/api/resume/active", json={"resume_id": "foreign"},
                          headers=headers).status_code == 404
        # Unparsed asset can't be activated.
        resp = client.put("/api/resume/active", json={"resume_id": "a3"},
                          headers=headers)
        assert resp.status_code == 409
        assert resp.get_json()["error"] == "resume_not_parsed"
        # Parsed + owned → 200 and the pointer moves.
        ok = client.put("/api/resume/active", json={"resume_id": "a1"},
                        headers=headers)
        assert ok.status_code == 200
        assert db.users.find_one({"email": self.EMAIL})["active_resume_id"] == "a1"

    def test_delete_active_asset_repoints_to_newest_parsed(
            self, db, storage, resume_app):
        self._seed_assets(db)
        client = resume_app.test_client()
        headers = _headers(resume_app, self.EMAIL)

        resp = client.delete("/api/resume/file/a2", headers=headers)
        assert resp.status_code == 200
        assert storage.deleted == ["resume-assets/a2/original"]
        assert db.resume_assets.find_one({"resume_id": "a2"}) is None
        # Re-pointed to the newest REMAINING parsed asset (a1; a3 is pending).
        assert db.users.find_one({"email": self.EMAIL})["active_resume_id"] == "a1"

        # Deleting the last parsed asset unsets the pointer.
        client.delete("/api/resume/file/a1", headers=headers)
        assert "active_resume_id" not in db.users.find_one({"email": self.EMAIL})

        # Foreign / unknown asset → 404, nothing deleted.
        assert client.delete("/api/resume/file/ghost",
                             headers=headers).status_code == 404


# ===========================================================================
# Async job slimming (spec 1.3)
# ===========================================================================

class TestJobLifecycleV3:
    def test_idempotency_key_dedups_create_job(self, db):
        import services.resume_service as rs
        svc = rs.get_resume_service()
        key = f"tailor:{_uuid.uuid4().hex}"
        first = svc.create_job("tailor", {"x": 1}, "a@b.c", idempotency_key=key)
        second = svc.create_job("tailor", {"x": 1}, "a@b.c", idempotency_key=key)
        assert first == second
        assert db.resume_jobs.count_documents({"idempotency_key": key}) == 1
        # No key → always a fresh job.
        assert svc.create_job("tailor", {}, "a@b.c") != svc.create_job("tailor", {}, "a@b.c")

    def test_complete_and_fail_stamp_expire_at(self, db):
        import services.resume_service as rs
        svc = rs.get_resume_service()
        done = svc.create_job("extract_jd", {}, "a@b.c")
        svc.complete_job(done, {"ok": True})
        failed = svc.create_job("extract_jd", {}, "a@b.c")
        svc.fail_job(failed, "boom")
        for jid in (done, failed):
            row = db.resume_jobs.find_one({"job_id": jid})
            delta = row["expire_at"] - row["completed_at"]
            assert timedelta(days=6, hours=23) < delta <= timedelta(days=7)
            assert "payload" not in row


# ===========================================================================
# Guest parse — asset ref + profile block (spec 1.1 / 5)
# ===========================================================================

class TestGuestParseV3:
    def test_worker_writes_asset_and_returns_profile(self, db, storage, monkeypatch):
        import services.resume_service as rs
        monkeypatch.setenv("GUEST_SIGNAL_HMAC_KEY", "unit-test-signal-key")
        session = guest_service.create_or_get_session(None, "fp_v3", "iphash")
        gid = session["guest_id"]
        rid = resume_access.create_pending_asset(
            owner_type="guest", guest_id=gid,
            quota_subject_id=session.get("quota_subject_id"),
            filename="resume.pdf", mime_type="application/pdf",
            purge_after=datetime.now(timezone.utc) + timedelta(days=30),
        )
        resume_access.attach_object_key(rid, f"resume-assets/{rid}/original")
        payload = {
            "resume_id": rid, "object_key": f"resume-assets/{rid}/original",
            "mime_type": "application/pdf", "guest_id": gid,
            "fingerprint_hash": "fp_v3",
        }
        job_id = guest_service.create_guest_job("guest_parse", payload, gid)
        svc = rs.get_resume_service()
        monkeypatch.setattr(
            svc.parser, "parse_to_structured",
            lambda *a, **k: (_structured(), "Jordan Alvarez raw resume text " * 5),
        )
        rs._process_job(job_id, "guest_parse", payload)

        assert storage.fetched == [f"resume-assets/{rid}/original"]
        job = guest_service.get_guest_job(job_id, gid)
        assert job["status"] == "completed"
        result = job["result"]
        assert result["resume_id"] == rid
        # Honest profile: only genuinely extracted fields (never invented).
        profile = result["profile"]
        assert profile["first_name"] == "Jordan"
        assert profile["headline_title"] == "Backend Engineer"
        assert profile["location"] == "Tempe, AZ"
        assert profile["years"] >= 1 and profile["experience_level"]
        assert 0 < len(profile["skills"]) <= 10
        assert "work_authorization" not in profile  # not extractable → absent

        # Parsed content landed IN the asset; the session keeps only the ref.
        asset = db.resume_assets.find_one({"resume_id": rid})
        assert asset["parse_status"] == "parsed" and asset["structured"]
        raw_session = db.guest_sessions.find_one({"guest_id": gid})
        assert raw_session["resume_id"] == rid
        assert raw_session.get("resume") in (None, {})
        # Hydrated view for legacy readers (tailor/matches until Phase C).
        assert guest_service.get_session(gid)["resume"]["structured"]

    def test_guest_onboard_s3_down_on_lambda_returns_503(
            self, db, resume_app, monkeypatch):
        monkeypatch.setenv("GUEST_SIGNAL_HMAC_KEY", "unit-test-signal-key")
        import services.s3_service as s3s
        monkeypatch.setattr(s3s, "get_storage_service",
                            lambda: _FakeStorage(fail_upload=True))
        monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "jobpilot-resume-prod")
        resp = resume_app.test_client().post(
            "/api/resume/guest/onboard",
            data={"file": (io.BytesIO(PDF_BYTES), "resume.pdf")},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 503
        assert resp.get_json()["error"] == "storage_unavailable"
        asset = db.resume_assets.find_one({})
        assert asset["parse_status"] == "failed"
        # The reserved global parse slot was refunded.
        counter = db.guest_global_counters.find_one(
            {"_id": {"$regex": "^guest_parses:"}})
        assert (counter or {}).get("count", 0) == 0

    def test_profile_omits_fields_the_resume_does_not_state(
            self, db, storage, monkeypatch):
        import services.resume_service as rs
        bare = {
            "contact": {"name": "", "email": "", "phone": "", "linkedin": "",
                        "github": "", "location": ""},
            "summary": "", "skills": {}, "experience": [], "education": [],
            "projects": [], "certifications": [],
        }
        profile = rs._derive_guest_profile(bare)
        assert profile == {}  # nothing stated → nothing derived
