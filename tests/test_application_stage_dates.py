"""An application remembers the first time it reached each stage.

The status says where an application is now. The tracker's funnel needs to
know where it has been: one rejected after an interview still reached
interview. Offline: mongomock, no Google."""
from datetime import datetime, timedelta, timezone

import pytest

from _vg_support import current_db, load_service_app
from services import application_stages as stages
from services import gmail_service as gs

USER = "stages@example.test"
NOW = datetime.now(timezone.utc)


@pytest.fixture
def db(monkeypatch):
    db = current_db()
    for name in ("tailoring_records", "gmail_connections", "gmail_suggestions", "applications"):
        db[name].delete_many({})
    db.tailoring_records.insert_one({
        "user_email": USER, "record_id": "r1", "created_at": NOW - timedelta(days=30),
        "jd_analysis": {"company": "Acme", "job_title": "Platform Engineer"},
        "application": {"status": "saved"}})
    monkeypatch.setattr(gs, "classify_record_thread", lambda *_: None)
    return db


@pytest.fixture
def client(db):
    app = load_service_app("resume")
    from flask_jwt_extended import create_access_token
    with app.app_context():
        headers = {"Authorization": f"Bearer {create_access_token(identity=USER)}"}

    def patch(**body):
        response = app.test_client().patch(
            "/api/resume/tailoring-records/r1/application", headers=headers, json=body)
        assert response.status_code == 200, response.get_json()
        return response.get_json()["application"]

    patch.list = lambda: app.test_client().get(
        "/api/resume/tailoring-records?view=list", headers=headers).get_json()["records"][0]
    return patch


def stored(db):
    return (db.tailoring_records.find_one({"record_id": "r1"})["application"].get("stage_dates") or {})


def test_only_real_stages_are_dated():
    assert stages.first_reached("saved") == {} and stages.first_reached("draft") == {}
    assert stages.first_reached({"$ne": ""}) == {}
    assert list(stages.first_reached("offer")) == ["application.stage_dates.offer"]


def test_each_stage_is_dated_once_and_a_rejection_keeps_the_interview(db, client):
    assert stored(db) == {}
    client(status="applied")
    client(status="interviewing")
    first_interview = stored(db)["interviewing"]
    client(status="applied")
    client(status="interviewing")
    client(status="rejected")
    dates = stored(db)
    assert set(dates) == {"applied", "interviewing", "rejected"}
    assert dates["interviewing"] == first_interview, "going back and forth never moves the first date"
    assert dates["applied"] <= dates["interviewing"] <= dates["rejected"]


def test_a_back_dated_application_is_dated_when_it_was_sent(db, client):
    sent = (NOW - timedelta(days=9)).replace(microsecond=0)
    client(status="applied", applied_at=sent.isoformat())
    assert stored(db)["applied"].replace(tzinfo=timezone.utc) == sent


def test_the_tracker_receives_the_dates_as_iso_with_an_offset(db, client):
    answered = client(status="applied")
    assert answered["stage_dates"]["applied"].endswith("+00:00")
    row = client.list()
    assert datetime.fromisoformat(row["application"]["stage_dates"]["applied"]).tzinfo is not None


def test_gmail_dates_a_stage_by_the_email_not_by_the_sync(db, monkeypatch):
    db.tailoring_records.update_one({}, {"$set": {"application.status": "applied"}})
    db.gmail_connections.insert_one({"user_email": USER})
    emailed = NOW - timedelta(days=4)
    message = {"message_id": "m1", "thread_id": "t1", "from_name": "Acme Careers",
               "from_address": "careers@acme.test", "from_domain": "acme.test",
               "subject": "Acme application update", "snippet": "",
               "internal_date": int(emailed.timestamp() * 1000),
               "body": "Regarding Platform Engineer at Acme: we have decided to move forward with other candidates."}
    monkeypatch.setattr(gs, "fetch_recent_messages", lambda *_: [message])
    assert gs.sync_user(USER)["auto_applied"] == 1
    reached = stored(db)["rejected"].replace(tzinfo=timezone.utc)
    assert abs((reached - emailed).total_seconds()) < 1

    # Undo takes back the date this move wrote.
    change = gs.list_activity(USER)[0]
    gs.undo_auto_apply(USER, change["suggestion_id"])
    assert "rejected" not in stored(db)


def test_undo_keeps_a_stage_date_that_was_already_there(db, monkeypatch):
    earlier = (NOW - timedelta(days=20)).replace(tzinfo=None, microsecond=0)
    db.tailoring_records.update_one({}, {"$set": {
        "application.status": "applied", "application.stage_dates": {"rejected": earlier}}})
    db.gmail_connections.insert_one({"user_email": USER})
    message = {"message_id": "m2", "thread_id": "t2", "from_name": "Acme Careers",
               "from_address": "careers@acme.test", "from_domain": "acme.test",
               "subject": "Acme application update", "snippet": "",
               "internal_date": int((NOW - timedelta(days=2)).timestamp() * 1000),
               "body": "Regarding Platform Engineer at Acme: we have decided to move forward with other candidates."}
    monkeypatch.setattr(gs, "fetch_recent_messages", lambda *_: [message])
    assert gs.sync_user(USER)["auto_applied"] == 1
    gs.undo_auto_apply(USER, gs.list_activity(USER)[0]["suggestion_id"])
    assert stored(db)["rejected"] == earlier
