"""E1 (defect E) — the canonical OUTCOME axis + the migrated MAIL writer.

Covers `application_ledger_service.set_outcome` (find-or-create the canonical
row, stamp the employer-response funnel axis, reject invalid outcomes, stay
owner-scoped) and the mail writer's write-through
(`mail_ingress_service._apply_tracker_status` now ALSO lands the canonical
outcome while keeping its exact `tailoring_records.application.*` mirror + its
`matched_count>0` contract — the golden characterization files stay untouched).

Runs fully offline (mongomock; no Bedrock/network) — every path here is pure
Mongo. See tests/test_char_status_writers.py + tests/test_char_kanban_read.py
for the golden mirrors these additions must NOT disturb.
"""
from datetime import datetime

import pytest

from _vg_support import current_db

import services.application_ledger_service as als
import services.mail_ingress_service as mi

USER = "alice@aspirely.me"
OTHER = "bob@aspirely.me"


@pytest.fixture()
def db():
    d = current_db()
    for c in ("applications", "tailoring_records", "auto_submit_audit"):
        d[c].delete_many({})
    return d


def _seed_record(db, record_id="rec-1", *, company="Acme", title="Engineer",
                 status="draft", user=USER):
    db.tailoring_records.insert_one({
        "record_id": record_id,
        "user_email": user,
        "jd_analysis": {"company": company, "job_title": title},
        "application": {"status": status},
        "created_at": datetime(2026, 1, 1),
    })


def _apps(db, user=USER):
    return list(db.applications.find({"user_email": user}, {"_id": 0}))


# ---------------------------------------------------------------------------
# 1. set_outcome on an EXISTING canonical row
# ---------------------------------------------------------------------------
class TestSetOutcomeExistingRow:
    def test_updates_outcome_source_and_updated_at(self, db):
        # A real auto-apply row already exists (execution axis in `ready`).
        view, _ = als.create_application(
            USER, company="Acme", title="Engineer",
            jd_text="Backend engineer building distributed systems.",
            source="greenhouse", tailoring_record_id="rec-1")
        assert view["status"] == als.STATUS_QUEUED

        res = als.set_outcome(USER, record_id="rec-1", outcome="interviewing",
                              source="mail_ingress", evidence="See you Tuesday.")
        assert res["created"] is False
        assert res["application_id"] == view["application_id"]

        row = db.applications.find_one(
            {"application_id": view["application_id"]}, {"_id": 0})
        assert row["outcome"] == "interviewing"
        assert row["outcome_source"] == "mail_ingress"
        assert row["outcome_evidence"] == "See you Tuesday."
        assert isinstance(row["outcome_updated_at"], datetime)
        assert isinstance(row["updated_at"], datetime)
        # The EXECUTION axis is untouched — outcome is orthogonal to status.
        assert row["status"] == als.STATUS_QUEUED
        # Exactly one row: set_outcome found the existing one, did NOT create.
        assert len(_apps(db)) == 1

    def test_rejects_an_invalid_outcome_value(self, db):
        _seed_record(db)
        with pytest.raises(als.InvalidApplicationInput):
            als.set_outcome(USER, record_id="rec-1", outcome="teleported",
                            source="mail_ingress")
        # Nothing written on rejection.
        assert _apps(db) == []


# ---------------------------------------------------------------------------
# 2. set_outcome with NO existing row → lightweight external row
# ---------------------------------------------------------------------------
class TestSetOutcomeCreatesLightweightRow:
    def test_creates_external_row_linked_to_record(self, db):
        res = als.set_outcome(USER, record_id="rec-99", outcome="rejected",
                              source="mail_ingress")
        assert res["created"] is True

        rows = _apps(db)
        assert len(rows) == 1
        row = rows[0]
        assert row["origin"] == "mail_ingress"
        assert row["status"] == als.STATUS_EXTERNAL
        assert row["tailoring_record_id"] == "rec-99"
        assert row["outcome"] == "rejected"
        assert row["outcome_source"] == "mail_ingress"
        assert row["application_id"] == res["application_id"]

    def test_external_is_not_active_or_retryable(self):
        # dedup_gate keys on ACTIVE_STATUSES; retry keys on RETRYABLE_STATUSES —
        # `external` is in NEITHER, so an external row never blocks the queue nor
        # gets re-queued.
        assert als.STATUS_EXTERNAL not in als.ACTIVE_STATUSES
        assert als.STATUS_EXTERNAL not in als.RETRYABLE_STATUSES
        assert als.STATUS_EXTERNAL in als.ALL_STATUSES

    def test_dedup_gate_ignores_an_external_row(self, db):
        # A lightweight row created against a job identity must NOT make
        # dedup_gate report the posting as "already handled" (skip=False).
        identity = "a" * 64
        als.set_outcome(USER, job_ref=identity, outcome="applied",
                        source="manual")
        skip, view = als.dedup_gate(USER, identity)
        assert skip is False
        assert view is not None and view["status"] == als.STATUS_EXTERNAL


# ---------------------------------------------------------------------------
# 3. The migrated MAIL writer — golden mirror preserved + canonical outcome set
# ---------------------------------------------------------------------------
class TestMailWriterWriteThrough:
    def test_known_record_mirrors_and_sets_canonical_outcome(self, db):
        _seed_record(db, "rec-1", status="draft")
        assert mi._apply_tracker_status(USER, "rec-1", "applied") is True

        # (a) The tailoring_records mirror is byte-identical to the golden.
        doc = db.tailoring_records.find_one(
            {"record_id": "rec-1", "user_email": USER})
        app = doc["application"]
        assert app["status"] == "applied"
        assert app["source"] == "mail_ingress"
        assert isinstance(app["updated_at"], datetime)
        assert isinstance(app["applied_at"], datetime)
        assert isinstance(doc["updated_at"], datetime)

        # (b) The canonical outcome axis is now also populated.
        rows = _apps(db)
        assert len(rows) == 1
        assert rows[0]["status"] == als.STATUS_EXTERNAL
        assert rows[0]["tailoring_record_id"] == "rec-1"
        assert rows[0]["outcome"] == "applied"
        assert rows[0]["outcome_source"] == "mail_ingress"

    def test_interviewing_maps_to_interviewing_outcome(self, db):
        _seed_record(db, "rec-1")
        assert mi._apply_tracker_status(USER, "rec-1", "interviewing") is True
        rows = _apps(db)
        assert len(rows) == 1
        assert rows[0]["outcome"] == "interviewing"

    def test_unknown_record_returns_false_and_creates_no_canonical_row(self, db):
        _seed_record(db, "rec-1")
        assert mi._apply_tracker_status(USER, "does-not-exist", "applied") is False
        # No tailoring_records write (rec-1 untouched, stays draft).
        doc = db.tailoring_records.find_one(
            {"record_id": "rec-1", "user_email": USER})
        assert doc["application"]["status"] == "draft"
        # And NO canonical row created.
        assert _apps(db) == []

    def test_tracker_to_outcome_map_is_this(self):
        assert mi._TRACKER_TO_OUTCOME == {
            "applied": "applied",
            "viewed": "viewed",
            "interviewing": "interviewing",
            "offer": "offer",
            "rejected": "rejected",
            "ghosted": "ghosted",
        }


# ---------------------------------------------------------------------------
# 4. Owner scoping — an outcome for user A never touches user B's row
# ---------------------------------------------------------------------------
class TestOutcomeOwnerScoping:
    def test_set_outcome_for_a_never_touches_b(self, db):
        # B already owns a canonical row linked to the SAME record id.
        b_res = als.set_outcome(OTHER, record_id="rec-shared", outcome="applied",
                                source="mail_ingress")
        b_id = b_res["application_id"]

        # A stamps an outcome for the same record id.
        a_res = als.set_outcome(USER, record_id="rec-shared", outcome="rejected",
                                source="manual")

        assert a_res["application_id"] != b_id
        assert a_res["created"] is True

        # B's row is completely untouched.
        b_row = db.applications.find_one({"application_id": b_id}, {"_id": 0})
        assert b_row["user_email"] == OTHER
        assert b_row["outcome"] == "applied"

        # A has exactly one, separate row.
        a_rows = _apps(db, USER)
        assert len(a_rows) == 1
        assert a_rows[0]["outcome"] == "rejected"
        assert a_rows[0]["user_email"] == USER


# ---------------------------------------------------------------------------
# 5. The migrated PATCH /application ROUTE — end-to-end write-through.
#
# The route's OLD behavior (tailoring_records mirror + streak + saved_jobs sync +
# 400/404 contract) is pinned byte-for-byte in tests/test_char_patch_application.py.
# THIS class only asserts the E1 ADDITION: a user status change ALSO lands the
# canonical OUTCOME row, source="user", mapping draft->none and every other
# status to itself. Best-effort in the route, so it never alters the response.
# ---------------------------------------------------------------------------
class TestPatchRouteWriteThrough:
    def _client(self):
        from _vg_support import load_service_app
        return load_service_app("resume")

    def _token(self, flask_app, identity=USER):
        from flask_jwt_extended import create_access_token
        with flask_app.app_context():
            return create_access_token(identity=identity)

    def _seed(self, db, record_id="rec-1", status="draft"):
        db.tailoring_records.insert_one({
            "record_id": record_id,
            "user_email": USER,
            "jd_analysis": {"company": "Acme", "job_title": "Engineer"},
            "application": {"status": status},
            "created_at": datetime(2026, 1, 1),
        })

    def _patch(self, client, token, record_id, body):
        return client.test_client().patch(
            f"/api/resume/tailoring-records/{record_id}/application",
            headers={"Authorization": f"Bearer {token}"},
            json=body,
        )

    def test_status_change_lands_canonical_outcome_source_user(self, db):
        self._seed(db, "rec-1", status="draft")
        client = self._client()
        token = self._token(client)
        resp = self._patch(client, token, "rec-1", {"status": "rejected"})
        assert resp.status_code == 200, resp.get_data(as_text=True)

        rows = _apps(db)
        assert len(rows) == 1
        assert rows[0]["tailoring_record_id"] == "rec-1"
        assert rows[0]["outcome"] == "rejected"
        assert rows[0]["outcome_source"] == "user"
        assert rows[0]["status"] == als.STATUS_EXTERNAL

    def test_draft_status_maps_to_none_outcome(self, db):
        self._seed(db, "rec-1", status="applied")
        client = self._client()
        token = self._token(client)
        resp = self._patch(client, token, "rec-1", {"status": "draft"})
        assert resp.status_code == 200, resp.get_data(as_text=True)
        rows = _apps(db)
        assert len(rows) == 1
        assert rows[0]["outcome"] == "none"

    def test_non_status_update_writes_no_canonical_row(self, db):
        self._seed(db, "rec-1", status="draft")
        client = self._client()
        token = self._token(client)
        # A notes-only update carries no status -> no outcome write-through.
        resp = self._patch(client, token, "rec-1", {"notes": "left a voicemail"})
        assert resp.status_code == 200, resp.get_data(as_text=True)
        assert _apps(db) == []
