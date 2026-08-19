"""CHARACTERIZATION — guards the E1 PATCH-route migration.

Scope: the resume blueprint's canonical USER-DRIVEN status writer
``PATCH /api/resume/tailoring-records/<record_id>/application`` — the one status
writer the service-layer golden net (tests/test_char_status_writers.py) had to
SKIP because it is a Flask route gated by ``@jwt_required`` with side effects the
mail/gmail writers don't have:

  * the ``tailoring_records.application.status/applied_at/updated_at`` $set;
  * the STREAK ``record_application`` fire — which the route gates on a
    batch-confirmation transition (``was_confirmation_pending and
    was_status != 'applied' and status == 'applied'``), NOT on every
    draft->applied change (characterized both ways below);
  * the ``saved_jobs`` bidirectional sync via ``job_service.update_saved_job``
    (fires whenever a truthy ``status`` is written and the record carries a
    ``source_job_id``);
  * owner scoping (another user's record -> 404) and invalid-status validation
    (-> 400, NEVER 401).

This file pins CURRENT behavior so the E1 migration (which ADDS a
``application_ledger_service.set_outcome`` write-through) can be proven additive:
every assertion here must stay green BEFORE and AFTER the route change.

Runs fully offline (mongomock via conftest/_vg_support; no Bedrock, no network).
The streak/saved_job side effects are spied (monkeypatched) so no real streak or
saved_jobs document is touched.
"""
import sys
from datetime import datetime

import pytest

from _vg_support import current_db, load_service_app

USER = "patch-char@aspirely.me"
OTHER = "someone-else@aspirely.me"


@pytest.fixture()
def client():
    # Unique module name (importlib) so the resume app's routes don't 404 when
    # the whole suite runs in one pytest process — same idiom as
    # tests/test_voicegraph_route.py.
    return load_service_app("resume")


@pytest.fixture()
def db():
    d = current_db()
    for c in ("tailoring_records", "applications", "auto_submit_audit"):
        d[c].delete_many({})
    return d


def _token(flask_app, identity=USER):
    from flask_jwt_extended import create_access_token
    with flask_app.app_context():
        return create_access_token(identity=identity)


def _seed_record(db, record_id="rec-1", *, user=USER, status="draft",
                 source_job_id=None, batch_confirmation_pending=False):
    app_doc = {"status": status}
    if batch_confirmation_pending:
        app_doc["batch_confirmation_pending"] = True
    doc = {
        "record_id": record_id,
        "user_email": user,
        "jd_analysis": {"company": "Acme", "job_title": "Engineer"},
        "application": app_doc,
        "created_at": datetime(2026, 1, 1),
    }
    if source_job_id:
        doc["source_job_id"] = source_job_id
    db.tailoring_records.insert_one(doc)


def _patch(client, token, record_id, body):
    return client.test_client().patch(
        f"/api/resume/tailoring-records/{record_id}/application",
        headers={"Authorization": f"Bearer {token}"},
        json=body,
    )


def _app(db, record_id="rec-1", user=USER):
    doc = db.tailoring_records.find_one({"record_id": record_id, "user_email": user})
    return (doc or {}).get("application") or {}


# ---------------------------------------------------------------------------
# 1. The core tailoring_records.application persistence
# ---------------------------------------------------------------------------
class TestStatusPersistence:
    def test_valid_status_writes_status_and_updated_at(self, client, db):
        _seed_record(db, status="draft")
        token = _token(client)
        resp = _patch(client, token, "rec-1", {"status": "interviewing"})
        assert resp.status_code == 200, resp.get_data(as_text=True)
        app = _app(db)
        assert app["status"] == "interviewing"
        assert isinstance(app["updated_at"], datetime)
        # applied_at is stamped ONLY on the "applied" status.
        assert "applied_at" not in app
        # The response echoes the (serialized) application block.
        body = resp.get_json()
        assert body["application"]["status"] == "interviewing"

    def test_applied_status_stamps_applied_at(self, client, db):
        _seed_record(db, status="draft")
        token = _token(client)
        resp = _patch(client, token, "rec-1", {"status": "applied"})
        assert resp.status_code == 200, resp.get_data(as_text=True)
        app = _app(db)
        assert app["status"] == "applied"
        assert isinstance(app["applied_at"], datetime)
        assert isinstance(app["updated_at"], datetime)

    def test_invalid_status_returns_400_never_401(self, client, db):
        _seed_record(db, status="draft")
        token = _token(client)
        resp = _patch(client, token, "rec-1", {"status": "teleported"})
        assert resp.status_code == 400
        assert "Invalid status" in (resp.get_json() or {}).get("error", "")
        # Untouched on rejection.
        assert _app(db)["status"] == "draft"

    def test_valid_status_set_is_this_golden(self, client):
        # The route's accepted status vocabulary — the E1 outcome map must cover
        # every one of these (saved/draft -> "none", the rest map to themselves).
        # `client` puts services/resume on sys.path so blueprints.resume imports
        # the SAME module the loaded app registered.
        import importlib
        mod = importlib.import_module("blueprints.resume")
        assert mod._APPLICATION_STATUSES == {
            "saved", "draft", "applied", "interviewing", "offer", "rejected",
            "withdrawn", "ghosted",
        }


# ---------------------------------------------------------------------------
# 2. Streak side effect — fires ONLY on a confirmed batch draft->applied
# ---------------------------------------------------------------------------
class TestStreakSideEffect:
    def test_streak_fires_on_batch_confirmed_draft_to_applied(
            self, client, db, monkeypatch):
        _seed_record(db, status="draft", batch_confirmation_pending=True)
        calls = []
        import services.streak_service as ss
        monkeypatch.setattr(
            ss, "record_application", lambda email, tz=None: calls.append(email))
        token = _token(client)
        resp = _patch(client, token, "rec-1",
                      {"status": "applied", "batch_confirmation_pending": False})
        assert resp.status_code == 200, resp.get_data(as_text=True)
        # Fires EXACTLY once, with the JWT-identity user_email.
        assert calls == [USER]

    def test_streak_does_not_fire_without_pending_confirmation(
            self, client, db, monkeypatch):
        _seed_record(db, status="draft", batch_confirmation_pending=False)
        calls = []
        import services.streak_service as ss
        monkeypatch.setattr(
            ss, "record_application", lambda email, tz=None: calls.append(email))
        token = _token(client)
        resp = _patch(client, token, "rec-1", {"status": "applied"})
        assert resp.status_code == 200, resp.get_data(as_text=True)
        # A normal draft->applied change must NOT double-fire the streak.
        assert calls == []


# ---------------------------------------------------------------------------
# 3. saved_jobs bidirectional sync — job_service.update_saved_job
# ---------------------------------------------------------------------------
class TestSavedJobSync:
    def _spy_job_service(self, monkeypatch):
        calls = []

        class _FakeJS:
            def update_saved_job(self, job_id, status=None, notes=None,
                                 user_email=""):
                calls.append((job_id, status, user_email))
                return {"job_id": job_id, "status": status}

        import services.job_service as js
        monkeypatch.setattr(js, "get_job_service", lambda: _FakeJS())
        return calls

    def test_status_change_propagates_to_saved_job(self, client, db, monkeypatch):
        _seed_record(db, status="draft", source_job_id="linkedin:123")
        calls = self._spy_job_service(monkeypatch)
        token = _token(client)
        resp = _patch(client, token, "rec-1", {"status": "applied"})
        assert resp.status_code == 200, resp.get_data(as_text=True)
        assert calls == [("linkedin:123", "applied", USER)]

    def test_no_status_field_skips_saved_job_sync(self, client, db, monkeypatch):
        _seed_record(db, status="draft", source_job_id="linkedin:123")
        calls = self._spy_job_service(monkeypatch)
        token = _token(client)
        # A non-status update (notes only) must NOT touch saved_jobs.
        resp = _patch(client, token, "rec-1", {"notes": "called recruiter"})
        assert resp.status_code == 200, resp.get_data(as_text=True)
        assert calls == []

    def test_no_source_job_id_skips_saved_job_sync(self, client, db, monkeypatch):
        _seed_record(db, status="draft")  # no source_job_id
        calls = self._spy_job_service(monkeypatch)
        token = _token(client)
        resp = _patch(client, token, "rec-1", {"status": "applied"})
        assert resp.status_code == 200, resp.get_data(as_text=True)
        assert calls == []


# ---------------------------------------------------------------------------
# 4. Owner scoping — another user's record is invisible (404, never 401)
# ---------------------------------------------------------------------------
class TestOwnerScoping:
    def test_other_users_record_returns_404(self, client, db):
        _seed_record(db, record_id="rec-b", user=OTHER, status="draft")
        token = _token(client, identity=USER)
        resp = _patch(client, token, "rec-b", {"status": "applied"})
        assert resp.status_code == 404
        # OTHER's row is untouched.
        assert _app(db, "rec-b", user=OTHER)["status"] == "draft"


if __name__ == "__main__":  # allow `python tests/test_char_patch_application.py`
    sys.exit(pytest.main([__file__, "-q"]))
