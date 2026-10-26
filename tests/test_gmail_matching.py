"""Gmail identity and outcome regressions; no network or real database."""
from datetime import datetime, timezone

import pytest

from _vg_support import current_db
from services import gmail_service as gs

USER = "gmail-tracker@example.test"
OTHER = "other@example.test"


def record(record_id="platform", title="Platform Engineer", company="Acme", user=USER):
    return {"record_id": record_id, "user_email": user,
            "jd_analysis": {"company": company, "job_title": title},
            "application": {"status": "applied"}}


def message(subject="Acme application update", body="", sender="careers@acme.test", mid="mail-1", date=2000):
    return {"message_id": mid, "thread_id": "thread-1", "subject": subject,
            "snippet": "", "body": body, "from_name": "Acme Careers",
            "from_address": sender, "from_domain": sender.split("@")[-1],
            "internal_date": date}


@pytest.fixture
def db(monkeypatch):
    db = current_db()
    for name in ("gmail_connections", "gmail_suggestions", "tailoring_records", "applications"):
        db[name].delete_many({})
    db.gmail_connections.insert_one({"user_email": USER})
    monkeypatch.setattr(gs, "classify_record_thread", lambda *_: None)
    return db


def test_role_in_body_selects_the_right_same_company_application():
    index = gs._build_record_index([record("data", "Data Engineer"), record()])
    match = gs._match_message_to_record(message(body="Your application for Platform Engineer at Acme was not selected."), index)
    assert match[0]["record_id"] == "platform"
    assert match[1] >= gs.AUTO_APPLY_CONFIDENCE
    assert not match[0]["requires_record_selection"]


def test_company_only_rejection_requires_a_choice_between_roles():
    index = gs._build_record_index([record("data", "Data Engineer"), record()])
    match = gs._match_message_to_record(message(body="We have decided to move forward with other candidates."), index)
    assert match[0]["requires_record_selection"]
    assert {r["record_id"] for r in match[0]["candidate_records"]} == {"data", "platform"}
    assert match[1] < gs.AUTO_APPLY_CONFIDENCE


def test_common_company_prefix_and_substrings_do_not_match():
    index = gs._build_record_index([record(company="United Airlines"), record("box", company="Box")])
    msg = message("United Healthcare application update via Dropbox", sender="jobs@unitedhealthcare.test")
    msg["from_name"] = "United Healthcare"
    assert gs._match_message_to_record(msg, index) is None


def test_generic_recruiter_domain_is_not_identity():
    rec = record()
    rec["application"]["recruiter_email"] = "alice@gmail.com"
    msg = message("Application update", sender="unrelated@gmail.com")
    msg["from_name"] = "Recruiter"
    assert gs._match_message_to_record(msg, gs._build_record_index([rec])) is None


def test_ambiguous_rejection_never_updates_a_record_and_repeats_are_idempotent(db, monkeypatch):
    db.tailoring_records.insert_many([record("data", "Data Engineer"), record()])
    msg = message(body="We have decided to move forward with other candidates.")
    monkeypatch.setattr(gs, "fetch_recent_messages", lambda *_: [msg])
    first = gs.sync_user(USER)
    second = gs.sync_user(USER)
    assert first["auto_applied"] == 0
    assert first["ambiguous_matches"] == 1
    assert second["auto_applied"] == 0
    assert {r["application"]["status"] for r in db.tailoring_records.find()} == {"applied"}
    suggestions = gs.list_suggestions(USER)
    assert len(suggestions) == 1
    assert db.gmail_suggestions.count_documents({}) == 1
    with pytest.raises(ValueError):
        gs.apply_suggestion(USER, suggestions[0]["suggestion_id"])
    result = gs.apply_suggestion(USER, suggestions[0]["suggestion_id"], record_id="platform")
    assert result["record_id"] == "platform"
    assert db.tailoring_records.find_one({"record_id": "platform"})["application"]["status"] == "rejected"
    assert db.tailoring_records.find_one({"record_id": "data"})["application"]["status"] == "applied"


def test_strong_unique_identity_and_decline_updates_only_correct_account(db, monkeypatch):
    db.tailoring_records.insert_many([record(), record("other-platform", user=OTHER)])
    msg = message(body="Regarding Platform Engineer at Acme: we have decided to move forward with other candidates.")
    monkeypatch.setattr(gs, "fetch_recent_messages", lambda *_: [msg])
    result = gs.sync_user(USER)
    assert result["auto_applied"] == 1
    assert db.tailoring_records.find_one({"record_id": "other-platform"})["application"]["status"] == "applied"
    history = gs.list_suggestions(USER, include_resolved=True)
    assert history[0]["match_confidence"] >= gs.AUTO_APPLY_CONFIDENCE
    assert history[0]["match_reason"]
    assert history[0]["evidence_message_id"] == "mail-1"


def test_suggestion_cannot_target_another_users_record(db):
    db.tailoring_records.insert_many([record(), record("other", user=OTHER)])
    db.gmail_suggestions.insert_one({"suggestion_id": "s1", "user_email": USER,
        "record_id": "platform", "suggested_status": "rejected", "applied": False,
        "dismissed": False, "requires_record_selection": True,
        "candidate_records": [{"record_id": "platform"}, {"record_id": "other"}]})
    with pytest.raises(LookupError):
        gs.apply_suggestion(USER, "s1", record_id="other")
    with pytest.raises(LookupError):
        gs.apply_suggestion(OTHER, "s1", record_id="platform")
    assert db.tailoring_records.find_one({"record_id": "other"})["application"]["status"] == "applied"


def test_newer_offer_is_not_overridden_by_old_rejection():
    rejection = message(body="We have decided to move forward with other candidates.", mid="old", date=1000)
    offer = message(body="We are pleased to offer you employment.", mid="new", date=2000)
    signals = gs.extract_deterministic_signals([offer, rejection])
    result = gs.reconcile_classification(signals, None)
    assert result["status"] == "offer"
    assert result["evidence_message_id"] == "new"
    assert result["confidence"] < gs.AUTO_APPLY_CONFIDENCE


def test_same_company_different_role_cannot_auto_update(db, monkeypatch):
    db.tailoring_records.insert_one(record())
    msg = message(body="Regarding Data Engineer at Acme: we have decided to move forward with other candidates.")
    monkeypatch.setattr(gs, "fetch_recent_messages", lambda *_: [msg])
    result = gs.sync_user(USER)
    assert result["auto_applied"] == 0
    assert db.tailoring_records.find_one()["application"]["status"] == "applied"


def test_rejection_after_an_offer_requires_review():
    assert gs._is_funnel_backwards("offer", "rejected")


def test_applying_received_receipt_preserves_known_application_date(db):
    original = datetime(2026, 9, 1, tzinfo=timezone.utc)
    rec = record()
    rec["application"]["applied_at"] = original
    rec["application"]["applied_date"] = "2026-09-01"
    db.tailoring_records.insert_one(rec)
    gs._apply_status(USER, "platform", "applied")
    app = db.tailoring_records.find_one()["application"]
    assert app["applied_at"].replace(tzinfo=timezone.utc) == original
    assert app["applied_date"] == "2026-09-01"


def test_older_batch_cannot_replace_newer_email_outcome(db, monkeypatch):
    db.tailoring_records.insert_one(record())
    newest = message(body="Platform Engineer at Acme: we have decided to move forward with other candidates.", date=2000)
    monkeypatch.setattr(gs, "fetch_recent_messages", lambda *_: [newest])
    assert gs.sync_user(USER)["auto_applied"] == 1
    older = message(body="Platform Engineer at Acme: we received your application.", mid="older", date=1000)
    monkeypatch.setattr(gs, "fetch_recent_messages", lambda *_: [older])
    result = gs.sync_user(USER)
    assert result["suggested"] == 0
    assert gs.list_suggestions(USER) == []
    assert db.tailoring_records.find_one()["application"]["status"] == "rejected"


def test_email_older_than_manual_status_change_requires_review(db, monkeypatch):
    rec = record()
    rec["application"].update(status="interviewing", updated_at=datetime(2026, 10, 25, tzinfo=timezone.utc))
    db.tailoring_records.insert_one(rec)
    old = message(body="Platform Engineer at Acme: we have decided to move forward with other candidates.", date=2000)
    monkeypatch.setattr(gs, "fetch_recent_messages", lambda *_: [old])
    result = gs.sync_user(USER)
    assert result["auto_applied"] == 0
    assert result["suggested"] == 1
    assert db.tailoring_records.find_one()["application"]["status"] == "interviewing"


def test_in_progress_sync_cannot_run_concurrently(db, monkeypatch):
    from datetime import timedelta
    db.gmail_connections.update_one({"user_email": USER}, {"$set": {
        "sync_lock_until": datetime.now(timezone.utc) + timedelta(minutes=5)}})
    def no_fetch(*_):
        pytest.fail("A duplicate sync fetched mail")
    monkeypatch.setattr(gs, "fetch_recent_messages", no_fetch)
    with pytest.raises(RuntimeError, match="already running"):
        gs.sync_user(USER)


def test_old_receipt_does_not_dismiss_review_of_a_different_email(db, monkeypatch):
    db.tailoring_records.insert_one(record())
    db.gmail_suggestions.insert_one({"suggestion_id": "new-decline", "user_email": USER,
        "record_id": "platform", "gmail_message_id": "new-mail", "suggested_status": "rejected",
        "applied": False, "dismissed": False})
    receipt = message(body="Platform Engineer at Acme: we received your application.", mid="receipt")
    monkeypatch.setattr(gs, "fetch_recent_messages", lambda *_: [receipt])
    gs.sync_user(USER)
    assert [s["suggestion_id"] for s in gs.list_suggestions(USER)] == ["new-decline"]


def test_confirmation_of_existing_status_prevents_older_batch_from_rejecting_it(db, monkeypatch):
    rec = record()
    rec["application"]["status"] = "offer"
    db.tailoring_records.insert_one(rec)
    offer = message(body="Platform Engineer at Acme: we are pleased to offer you employment.", date=2000)
    monkeypatch.setattr(gs, "fetch_recent_messages", lambda *_: [offer])
    gs.sync_user(USER)
    assert db.tailoring_records.find_one()["application"].get("gmail_evidence_ms") == 2000
    rejection = message(body="Platform Engineer at Acme: we have decided to move forward with other candidates.", mid="old", date=1000)
    monkeypatch.setattr(gs, "fetch_recent_messages", lambda *_: [rejection])
    result = gs.sync_user(USER)
    assert result["suggested"] == 0
    assert result["auto_applied"] == 0


def test_competing_ambiguous_choices_cannot_update_two_applications(db, monkeypatch):
    db.tailoring_records.insert_many([record(), record("data", "Data Engineer")])
    db.gmail_suggestions.insert_one({"suggestion_id": "race", "user_email": USER,
        "record_id": "platform", "suggested_status": "rejected", "applied": False,
        "dismissed": False, "requires_record_selection": True,
        "candidate_records": [{"record_id": "platform"}, {"record_id": "data"}]})
    original = gs._apply_status
    entered = False

    def apply(user, record_id, status, **kwargs):
        nonlocal entered
        if not entered:
            entered = True
            with pytest.raises(ValueError):
                gs.apply_suggestion(USER, "race", record_id="data")
        return original(user, record_id, status, **kwargs)

    monkeypatch.setattr(gs, "_apply_status", apply)
    gs.apply_suggestion(USER, "race", record_id="platform")
    assert db.tailoring_records.find_one({"record_id": "platform"})["application"]["status"] == "rejected"
    assert db.tailoring_records.find_one({"record_id": "data"})["application"]["status"] == "applied"


def test_failed_resolution_can_retry_the_same_choice(db, monkeypatch):
    db.tailoring_records.insert_one(record())
    db.gmail_suggestions.insert_one({"suggestion_id": "retry", "user_email": USER,
        "record_id": "platform", "suggested_status": "rejected", "applied": False,
        "dismissed": False, "candidate_records": [{"record_id": "platform"}]})
    original = gs._apply_status
    monkeypatch.setattr(gs, "_apply_status", lambda *_, **__: False)
    with pytest.raises(RuntimeError):
        gs.apply_suggestion(USER, "retry")
    monkeypatch.setattr(gs, "_apply_status", original)
    assert gs.apply_suggestion(USER, "retry")["applied"] is True


def test_backfilled_receipt_uses_evidence_date_not_sync_date(db):
    rec = record()
    rec["application"]["status"] = "draft"
    db.tailoring_records.insert_one(rec)
    received = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
    gs._apply_status(USER, "platform", "applied", evidence_ms=int(received.timestamp() * 1000))
    app = db.tailoring_records.find_one()["application"]
    assert app["applied_at"].replace(tzinfo=timezone.utc) == received
    assert app["applied_at_source"] == "gmail_receipt"
