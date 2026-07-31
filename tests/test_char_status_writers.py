"""CHARACTERIZATION — asserts CURRENT behavior to guard the E/F/G refactor;
update deliberately when that behavior is intentionally changed.

Scope: the SERVICE-LAYER status WRITERS that stamp
`tailoring_records.application.status` — the fields the canonical-applications
refactor must keep writing identically. Two writers are cheaply reachable at the
service layer and are pinned here as golden:

  * mail_ingress_service._apply_tracker_status — the Track-A (email ingress)
    writer. Stamps application.status/updated_at + a `source="mail_ingress"`
    provenance marker + top-level updated_at, and applied_at on "applied".
  * gmail_service._apply_status — the Track-B (Gmail) writer. Validates against
    VALID_STATUSES, stamps the same core fields but NO source marker.

SKIPPED (documented, not silently dropped): the resume blueprint
`PATCH /api/resume/tailoring-records/<id>/application` is the canonical
user-driven status write, but it is a Flask route gated by @jwt_required with
streak + saved_job bidirectional-sync side effects — exercising it needs full
app/JWT/route wiring (load_service_app + a minted token). Its core persistence
is the SAME application.status/applied_at/updated_at $set characterized above;
the E-refactor team should add a route-level test if it changes that contract.
The write we do NOT pin here is the answer-gather coverage path (defect) — out
of scope by instruction.

Runs fully offline (mongomock; no network). No LLM is invoked — these writers
are pure Mongo $set.
"""
from datetime import datetime

import pytest

from _vg_support import current_db

import services.mail_ingress_service as mi
import services.gmail_service as gs

USER = "alice@aspirely.me"


@pytest.fixture()
def rec_db():
    d = current_db()
    d.tailoring_records.delete_many({})
    _seed_record(d)
    return d


def _seed_record(db, record_id="rec-1", *, company="Acme", title="Engineer",
                 status="draft"):
    db.tailoring_records.insert_one({
        "record_id": record_id,
        "user_email": USER,
        "jd_analysis": {"company": company, "job_title": title},
        "application": {"status": status},
        "created_at": datetime(2026, 1, 1),
    })


def _app(db, record_id="rec-1"):
    doc = db.tailoring_records.find_one(
        {"record_id": record_id, "user_email": USER})
    return doc.get("application") or {}, doc


# ---------------------------------------------------------------------------
# mail_ingress_service._apply_tracker_status (Track A — email ingress)
# ---------------------------------------------------------------------------
class TestMailIngressWriter:
    def test_applied_stamps_status_source_and_applied_at(self, rec_db):
        assert mi._apply_tracker_status(USER, "rec-1", "applied") is True
        app, doc = _app(rec_db)
        assert app["status"] == "applied"
        # Provenance marker distinguishes an ingress-driven write. GOLDEN.
        assert app["source"] == "mail_ingress"
        assert isinstance(app["updated_at"], datetime)
        assert isinstance(app["applied_at"], datetime)     # only on "applied"
        # top-level updated_at is also bumped.
        assert isinstance(doc["updated_at"], datetime)

    def test_non_applied_status_sets_no_applied_at(self, rec_db):
        assert mi._apply_tracker_status(USER, "rec-1", "interviewing") is True
        app, _ = _app(rec_db)
        assert app["status"] == "interviewing"
        assert app["source"] == "mail_ingress"
        assert "applied_at" not in app          # NOT stamped for non-applied

    def test_unknown_record_returns_false_and_writes_nothing(self, rec_db):
        assert mi._apply_tracker_status(USER, "does-not-exist", "applied") is False

    def test_writer_is_user_scoped(self, rec_db):
        # Another user cannot move this row (filter folds in user_email).
        assert mi._apply_tracker_status("bob@aspirely.me", "rec-1", "applied") is False
        app, _ = _app(rec_db)
        assert app["status"] == "draft"          # untouched

    def test_status_to_tracker_vocab_map_is_this_golden(self):
        # The granular ingress vocab -> coarse tracker vocab mapping that feeds
        # _apply_tracker_status. Refactor E must preserve these coarse targets.
        assert mi._STATUS_TO_TRACKER == {
            "applied": "applied",
            "viewed": "applied",
            "oa_sent": "interviewing",
            "interview_invite": "interviewing",
            "offer": "offer",
            "rejected": "rejected",
            "ghosted": "ghosted",
        }


# ---------------------------------------------------------------------------
# gmail_service._apply_status (Track B — Gmail)
# ---------------------------------------------------------------------------
class TestGmailWriter:
    def test_applied_stamps_status_and_applied_at_but_no_source(self, rec_db):
        assert gs._apply_status(USER, "rec-1", "applied") is True
        app, doc = _app(rec_db)
        assert app["status"] == "applied"
        assert isinstance(app["updated_at"], datetime)
        assert isinstance(app["applied_at"], datetime)
        assert isinstance(doc["updated_at"], datetime)
        # Track B does NOT write an application.source marker (unlike Track A).
        assert "source" not in app

    def test_non_applied_status_sets_no_applied_at(self, rec_db):
        assert gs._apply_status(USER, "rec-1", "interviewing") is True
        app, _ = _app(rec_db)
        assert app["status"] == "interviewing"
        assert "applied_at" not in app

    def test_invalid_status_is_rejected_and_no_write(self, rec_db):
        # gmail writer validates against VALID_STATUSES and refuses others.
        assert gs._apply_status(USER, "rec-1", "teleported") is False
        app, _ = _app(rec_db)
        assert app["status"] == "draft"          # untouched

    def test_valid_statuses_set_is_this_golden(self):
        assert gs.VALID_STATUSES == {
            "applied", "interviewing", "offer", "rejected", "ghosted", "withdrawn"}

    def test_unknown_record_returns_false(self, rec_db):
        assert gs._apply_status(USER, "nope", "applied") is False
