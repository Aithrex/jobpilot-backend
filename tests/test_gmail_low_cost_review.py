"""Application evidence is retained without model spend or guessed identities."""
from datetime import datetime, timezone

import pytest

from _vg_support import current_db
from services import gmail_service as gs

USER = "low-cost@example.test"


def record(rid="r1", title="Software Engineer", company="Acme"):
    return {"record_id": rid, "user_email": USER,
            "jd_analysis": {"company": company, "job_title": title},
            "application": {"status": "applied"}}


def message(body, mid="m1", date=2000, subject="Acme application update"):
    return {"message_id": mid, "subject": subject, "body": body, "snippet": body,
            "from_address": "careers@acme.test", "from_domain": "acme.test",
            "internal_date": date}


@pytest.fixture
def db(monkeypatch):
    db = current_db()
    for name in ("gmail_connections", "gmail_suggestions", "tailoring_records", "applications"):
        db[name].delete_many({})
    db.gmail_connections.insert_one({"user_email": USER})
    monkeypatch.setattr(gs, "classify_record_thread", lambda *_: pytest.fail("Default scan spent on a model"))
    return db


@pytest.mark.parametrize("role", ["Senior Software Engineer", "Software Engineer II", "Software Engineer, Data Platform"])
def test_generic_title_cannot_capture_explicit_different_role(db, monkeypatch, role):
    db.tailoring_records.insert_one(record())
    monkeypatch.setattr(gs, "fetch_recent_messages", lambda *_: [message(
        f"Your application for {role} at Acme was not selected.")])
    result = gs.sync_user(USER)
    assert result["auto_applied"] == 0
    assert db.tailoring_records.find_one()["application"]["status"] == "applied"
    row = gs.list_suggestions(USER)[0]
    assert row["candidate_records"] == []
    assert row["requires_application_match"]
    with pytest.raises(ValueError, match="matching application"):
        gs.apply_suggestion(USER, row["suggestion_id"], record_id="r1")


def test_specific_role_selects_only_identical_candidate(db, monkeypatch):
    db.tailoring_records.insert_many([record(), record("senior", "Senior Software Engineer")])
    monkeypatch.setattr(gs, "fetch_recent_messages", lambda *_: [message(
        "Your application for Senior Software Engineer at Acme was not selected.")])
    result = gs.sync_user(USER)
    assert result["auto_applied"] == 1
    assert db.tailoring_records.find_one({"record_id": "r1"})["application"]["status"] == "applied"
    assert db.tailoring_records.find_one({"record_id": "senior"})["application"]["status"] == "rejected"


def test_unknown_outcome_is_durable_and_requires_explicit_status(db, monkeypatch):
    db.tailoring_records.insert_one(record())
    monkeypatch.setattr(gs, "fetch_recent_messages", lambda *_: [message(
        "Regarding Software Engineer at Acme: your candidacy has a new update. Please read our portal.")])
    first = gs.sync_user(USER)
    second = gs.sync_user(USER)
    assert first["deferred"] == 1
    assert first["model_calls"] == second["model_calls"] == 0
    assert db.gmail_connections.find_one().get("last_synced_at")
    rows = gs.list_suggestions(USER)
    assert len(rows) == 1
    assert rows[0]["suggested_status"] is None
    assert rows[0]["requires_status_selection"]
    with pytest.raises(ValueError, match="status"):
        gs.apply_suggestion(USER, rows[0]["suggestion_id"])
    for invalid in ({"$ne": ""}, "invented"):
        with pytest.raises(ValueError):
            gs.apply_suggestion(USER, rows[0]["suggestion_id"], status=invalid)
    applied = gs.apply_suggestion(USER, rows[0]["suggestion_id"], record_id="r1", status="rejected")
    assert applied["suggested_status"] == "rejected"
    assert applied["requires_status_selection"] is False
    assert db.tailoring_records.find_one()["application"]["status"] == "rejected"


def test_known_outcome_cannot_be_overridden_in_apply_request(db, monkeypatch):
    db.tailoring_records.insert_one(record())
    monkeypatch.setattr(gs, "fetch_recent_messages", lambda *_: [message("We have decided to move forward with other candidates.")])
    gs.sync_user(USER)
    row = gs.list_suggestions(USER)[0]
    with pytest.raises(ValueError, match="status"):
        gs.apply_suggestion(USER, row["suggestion_id"], status="offer")
    assert db.tailoring_records.find_one()["application"]["status"] == "applied"


def test_deterministic_rules_include_decisive_eleventh_message(db, monkeypatch):
    db.tailoring_records.insert_one(record())
    messages = [message("Regarding Software Engineer at Acme: your candidacy is being evaluated.",
                        mid=f"m{i}", date=3000 + i) for i in range(10)]
    messages.append(message("Regarding Software Engineer at Acme: we have decided to move forward with other candidates.", mid="decisive", date=2000))
    monkeypatch.setattr(gs, "fetch_recent_messages", lambda *_: messages)
    result = gs.sync_user(USER)
    assert result["auto_applied"] == 1
    assert result["model_calls"] == 0
    assert db.tailoring_records.find_one()["application"]["status"] == "rejected"


def test_budget_exhaustion_and_model_failure_preserve_review(db, monkeypatch):
    db.tailoring_records.insert_one(record())
    messages = [message("Your candidacy has an update; check the portal.", mid=f"m{i}") for i in range(5)]
    monkeypatch.setattr(gs, "fetch_recent_messages", lambda *_: messages)
    monkeypatch.setattr(gs, "_model_call_limit", lambda: 2)
    calls = []
    monkeypatch.setattr(gs, "classify_record_thread", lambda *args: calls.append(args))
    result = gs.sync_user(USER)
    assert len(calls) == result["model_calls"] == 2
    assert result["deferred"] == 5
    assert len(gs.list_suggestions(USER)) == 5
    assert all(s["suggested_status"] is None for s in gs.list_suggestions(USER))


def test_model_ignore_does_not_drop_matched_application_mail(db, monkeypatch):
    db.tailoring_records.insert_one(record())
    monkeypatch.setattr(gs, "fetch_recent_messages", lambda *_: [message("Please check your application portal.")])
    monkeypatch.setattr(gs, "_model_call_limit", lambda: 1)
    monkeypatch.setattr(gs, "classify_record_thread", lambda *_: {"status": "ignore", "confidence": .9})
    result = gs.sync_user(USER)
    assert result["deferred"] == 1
    assert len(gs.list_suggestions(USER)) == 1


def test_model_classifier_uses_one_flash_attempt(monkeypatch):
    from services import claude_client
    calls = []
    def classify(**kwargs):
        calls.append(kwargs)
        raise RuntimeError("model unavailable")
    monkeypatch.setattr(claude_client, "claude_json", classify)
    assert gs.classify_record_thread([message("Please check the application portal.")], {"company": "Acme"}) is None
    assert len(calls) == 1
    assert calls[0]["model"] == claude_client.CLAUDE_FLASH
    assert calls[0]["max_retries"] == 0


def test_requisition_role_cannot_target_a_different_specialty(db, monkeypatch):
    db.tailoring_records.insert_one(record(title="Software Engineering Manager - Cloud Streaming"))
    body = ("We have reviewed your application and have decided not to move forward for the "
            "JR123456 Software Engineer, Workflow Systems role at this time.")
    monkeypatch.setattr(gs, "fetch_recent_messages", lambda *_: [message(body)])
    assert gs.sync_user(USER)["auto_applied"] == 0
    row = gs.list_suggestions(USER)[0]
    assert row["candidate_records"] == []
    assert row["email_role"] == "software engineer workflow systems"
    assert row["suggested_status"] == "rejected"


@pytest.mark.parametrize("subject", ["Software Engineer, Workflow Systems — application decision", "Acme application update"])
def test_nonstandard_specialty_does_not_auto_match_generic_title(db, monkeypatch, subject):
    db.tailoring_records.insert_one(record())
    monkeypatch.setattr(gs, "fetch_recent_messages", lambda *_: [message(
        "Software Engineer, Workflow Systems at Acme. We have decided to move forward with other candidates.", subject=subject)])
    assert gs.sync_user(USER)["auto_applied"] == 0


def test_legacy_explicit_wrong_role_is_retained_without_invalid_candidate(db):
    db.tailoring_records.insert_one(record(title="Cloud Platform Engineer"))
    db.gmail_suggestions.insert_one({"suggestion_id": "legacy-role", "user_email": USER,
        "record_id": "r1", "company": "Acme", "title": "Cloud Platform Engineer",
        "subject": "Thank you for applying to Acme", "snippet": "We received your application for Backend Engineer, Knowledge Graph.",
        "suggested_status": "applied", "applied": False, "dismissed": False,
        "candidate_records": [{"record_id": "r1"}]})
    row = gs.list_suggestions(USER)[0]
    assert row["candidate_records"] == []
    assert row["requires_application_match"]
    with pytest.raises(ValueError, match="matching application"):
        gs.apply_suggestion(USER, "legacy-role", record_id="r1")


def test_partial_legacy_title_does_not_prune_valid_candidate(db):
    db.tailoring_records.insert_one(record(title="Software Engineer II"))
    db.gmail_suggestions.insert_one({"suggestion_id": "partial", "user_email": USER,
        "record_id": "r1", "company": "Acme", "title": "Software Engineer II",
        "subject": "Acme application update", "snippet": "We received your application for Software Engineer",
        "suggested_status": "applied", "applied": False, "dismissed": False,
        "candidate_records": [{"record_id": "r1"}]})
    assert gs.list_suggestions(USER)[0]["candidate_records"][0]["record_id"] == "r1"


@pytest.mark.parametrize("url,expected_auto,candidate_ids", [
    ("https://acme.myworkdayjobs.com/job/Software-Engineer_JR1111111", 0, []),
    ("https://acme.myworkdayjobs.com/job/Software-Engineer_JR2222222", 1, ["r1"]),
    ("", 0, ["r1"]),
])
def test_requisition_identity_is_preserved(db, monkeypatch, url, expected_auto, candidate_ids):
    rec = record()
    rec["application"]["job_url"] = url
    db.tailoring_records.insert_one(rec)
    monkeypatch.setattr(gs, "fetch_recent_messages", lambda *_: [message(
        "We have reviewed your application and have decided not to move forward for the JR2222222 Software Engineer position.")])
    assert gs.sync_user(USER)["auto_applied"] == expected_auto
    row = gs.list_suggestions(USER, include_resolved=True)[0]
    assert [r["record_id"] for r in row["candidate_records"]] == candidate_ids
    assert row["email_requisition_ids"] == ["JR2222222"]


@pytest.mark.parametrize("body", [
    "We have decided to offer you the position.",
    "We have made the decision to move forward with your application.",
    "We've reached a decision to proceed to the next interview.",
    "A decision has been made to schedule your next interview.",
    "Thank you for your time. We have decided to schedule a call.",
])
def test_positive_decision_is_not_a_rejection(body):
    assert "rejected" not in gs.extract_deterministic_signals([message(body)])


@pytest.mark.parametrize("title,expected", [
    ("Software Engineering Manager - Cloud Streaming", []),
    ("Software Engineer, Workflow Systems", ["r1"]),
])
def test_legacy_truncated_role_excludes_only_conflicting_titles(db, title, expected):
    db.tailoring_records.insert_one(record(title=title))
    db.gmail_suggestions.insert_one({"suggestion_id": "partial-role", "user_email": USER,
        "record_id": "r1", "company": "Acme", "title": title,
        "subject": "Thank you from Acme", "snippet": "Thank you for your interest in working at Acme. We have reviewed your application and have decided not to move forward for the JR2222222 Software Engineer, Workflow",
        "suggested_status": "rejected", "applied": False, "dismissed": False,
        "candidate_records": [{"record_id": "r1"}]})
    row = gs.list_suggestions(USER)[0]
    assert [r["record_id"] for r in row["candidate_records"]] == expected
    if not expected:
        assert row["requires_application_match"]


def test_unmatched_legacy_row_replaces_stale_company_and_match_metadata(db):
    db.tailoring_records.insert_many([
        record("wrong-company", "Software Engineer", "LinkedIn"),
        record("same-company-wrong-role", "Cloud Platform Engineer", "GitLab"),
    ])
    original = {"suggestion_id": "old-primary", "user_email": USER,
        "record_id": "wrong-company", "company": "LinkedIn", "title": "Software Engineer",
        "subject": "Thank you for applying to GitLab",
        "snippet": "We received your application for Backend Engineer, Knowledge Graph.",
        "suggested_status": "applied", "current_status": "applied", "confidence": .95,
        "match_confidence": .6, "match_reason": "Company LinkedIn matched",
        "applied": False, "dismissed": False,
        "candidate_records": [{"record_id": "wrong-company"}, {"record_id": "same-company-wrong-role"}]}
    db.gmail_suggestions.insert_one(dict(original))
    row = gs.list_suggestions(USER)[0]
    assert row["company"] == "GitLab"
    assert row["current_status"] is None
    assert row["match_confidence"] == 0
    assert row["match_reason"] == "No tracked application matches this email's role or requisition"
    assert row["candidate_records"] == []
    assert row["record_id"] is None
    assert row["requires_application_match"]
    assert row["suggested_status"] == "applied"
    assert row["confidence"] == .95
    stored = db.gmail_suggestions.find_one({"suggestion_id": "old-primary"})
    assert stored["company"] == "LinkedIn"
    assert stored["match_confidence"] == .6


@pytest.mark.parametrize("subject,expected_company", [
    ("Thank you for applying to GitLab", "GitLab"),
    ("GitLab and LinkedIn application update", ""),
    ("Application update", ""),
])
def test_already_unmatched_rows_clear_stale_metadata_without_guessing_company(db, subject, expected_company):
    db.tailoring_records.insert_many([
        record("gitlab", "Cloud Engineer", "GitLab"),
        record("linkedin", "Cloud Engineer", "LinkedIn"),
    ])
    db.gmail_suggestions.insert_one({"suggestion_id": "unmatched", "user_email": USER,
        "record_id": None, "company": "LinkedIn", "title": "Backend Engineer",
        "subject": subject, "suggested_status": "rejected", "current_status": "applied",
        "match_confidence": .6, "match_reason": "Previously matched",
        "requires_application_match": True, "candidate_records": [],
        "applied": False, "dismissed": False})
    row = gs.list_suggestions(USER)[0]
    assert row["company"] == expected_company
    assert row["current_status"] is None
    assert row["match_confidence"] == 0
    assert row["candidate_records"] == []


@pytest.mark.parametrize("resolved", ["applied", "dismissed"])
def test_resolved_unmatched_history_keeps_original_metadata(db, resolved):
    db.tailoring_records.insert_one(record(company="GitLab"))
    original = {"suggestion_id": "historical", "user_email": USER,
        "record_id": None, "company": "LinkedIn", "title": "Backend Engineer",
        "subject": "Thank you for applying to GitLab", "suggested_status": "applied",
        "current_status": "applied", "match_confidence": .6, "match_reason": "Historical match",
        "requires_application_match": True, "candidate_records": [],
        "applied": False, "dismissed": False, resolved: True}
    db.gmail_suggestions.insert_one(dict(original))
    row = gs.list_suggestions(USER, include_resolved=True)[0]
    assert row["company"] == original["company"]
    assert row["current_status"] == original["current_status"]
    assert row["match_confidence"] == original["match_confidence"]
    assert row["match_reason"] == original["match_reason"]


@pytest.mark.parametrize("title,body,subject", [
    ("Site Reliability Engineer I", "Unfortunately, we have decided not to proceed with your candidacy for the current Site Reliability Engineer I opening at Acme.", "Acme application update"),
    ("DevOps Platform Engineer I", "Thank you for taking the time to apply for our DevOps Platform Engineer I role.", "Thank you for applying to Acme"),
    ("Dev Ops Engineer", "We received your application.", "Your Application for Dev Ops Engineer – Thank You for Your Interest at Acme"),
    ("Full Stack Engineer", "Thank you for applying to Acme! We have received your application and will review it promptly. Position: Full Stack Engineer Thank you, Acme Please do not reply to this email. It is sent from an unmonitored inbox.", "Thank you for applying to Acme"),
    ("Software Quality Assurance Engineer I (Full-Time) - United States", "Thank you for applying to the Software Quality Assurance Engineer I (Full-Time) - United States (Req. 2029616) position at Acme.", "Acme application update"),
])
def test_ats_prose_wrappers_do_not_exclude_the_actual_role(db, monkeypatch, title, body, subject):
    db.tailoring_records.insert_one(record(title=title))
    msg = message(body, subject=subject)
    index = gs._build_record_index([record(title=title)])
    match = gs._match_message_to_record(msg, index)
    assert match is not None
    assert [r["record_id"] for r in match[0]["candidate_records"]] == ["r1"]
    assert not match[0].get("requires_application_match")
    monkeypatch.setattr(gs, "fetch_recent_messages", lambda *_: [msg])
    gs.sync_user(USER)
    assert all(not row.get("requires_application_match") for row in gs.list_suggestions(USER, include_resolved=True))


@pytest.mark.parametrize("title,retained_role", [
    ("Site Reliability Engineer I", "current site reliability engineer i opening"),
    ("DevOps Platform Engineer I", "taking the time to apply for our devops platform engineer i"),
    ("Dev Ops Engineer", "dev ops engineer thank you for your interest"),
    ("Full Stack Engineer", "full stack engineer thank you acme please do not reply to this email"),
    ("Software Quality Assurance Engineer I (Full-Time) - United States", "applying to the software quality assurance engineer i full time united states req 2029616 position"),
])
def test_retained_noisy_roles_recover_only_manual_candidates(db, title, retained_role):
    db.tailoring_records.insert_many([record(title=title), record("wrong", title="Senior Software Engineer")])
    stored = {"suggestion_id": "retained", "user_email": USER, "subject": "Acme application update",
              "title": retained_role, "email_roles": [retained_role], "company": "Acme",
              "record_id": None, "candidate_records": [], "requires_application_match": True,
              "requires_record_selection": True, "suggested_status": "rejected", "current_status": None,
              "match_confidence": 0, "source": "deterministic", "applied": False, "dismissed": False}
    db.gmail_suggestions.insert_one(dict(stored))
    row = gs.list_suggestions(USER)[0]
    assert [r["record_id"] for r in row["candidate_records"]] == ["r1"]
    assert not row["requires_application_match"]
    assert row["requires_record_selection"]
    assert row["match_confidence"] < gs.AUTO_APPLY_CONFIDENCE
    assert row["source"] == "deterministic"
    assert db.tailoring_records.find_one({"record_id": "r1"})["application"]["status"] == "applied"
    assert db.gmail_suggestions.find_one({"suggestion_id": "retained"})["candidate_records"] == []
    with pytest.raises(ValueError, match="Choose the application"):
        gs.apply_suggestion(USER, "retained")
    assert gs.apply_suggestion(USER, "retained", record_id="r1")["applied"]
    history = gs.list_suggestions(USER, include_resolved=True)[0]
    assert not history["requires_application_match"]
    assert not history["requires_record_selection"]


def test_role_cleanup_keeps_seniority_and_specialty_conflicts(db):
    db.tailoring_records.insert_one(record(title="Software Engineer"))
    db.gmail_suggestions.insert_one({"suggestion_id": "retained-conflict", "user_email": USER,
        "subject": "Acme application update", "email_roles": ["current senior software engineer workflow systems opening"],
        "record_id": None, "candidate_records": [], "requires_application_match": True,
        "suggested_status": "rejected", "applied": False, "dismissed": False})
    row = gs.list_suggestions(USER)[0]
    assert row["candidate_records"] == []
    assert row["requires_application_match"]


def test_recovered_role_still_rejects_conflicting_retained_requisition(db):
    rec = record()
    rec["application"]["job_url"] = "https://acme.example/jobs/Software-Engineer_JR1111111"
    db.tailoring_records.insert_one(rec)
    db.gmail_suggestions.insert_one({"suggestion_id": "retained-req", "user_email": USER,
        "subject": "Acme application update", "email_roles": ["current JR2222222 software engineer opening"],
        "record_id": None, "candidate_records": [], "requires_application_match": True,
        "suggested_status": "rejected", "applied": False, "dismissed": False})
    row = gs.list_suggestions(USER)[0]
    assert row["candidate_records"] == []
    assert row["requires_application_match"]


def test_recovered_role_needs_unique_employer_evidence(db):
    db.tailoring_records.insert_many([record(), record("other", company="OtherCo")])
    db.gmail_suggestions.insert_one({"suggestion_id": "unknown-employer", "user_email": USER,
        "subject": "Application update", "email_roles": ["current software engineer opening"],
        "record_id": None, "company": "Acme", "candidate_records": [], "requires_application_match": True,
        "suggested_status": "rejected", "applied": False, "dismissed": False})
    row = gs.list_suggestions(USER)[0]
    assert row["candidate_records"] == []
    assert row["requires_application_match"]
    assert row["company"] == ""


@pytest.mark.parametrize("subject", [
    "You shared some Google Account data with Acme",
    "Your Acme A to Z verification code",
    "Please confirm your identity",
    "Security code for your application to Acme",
])
def test_auth_notifications_never_become_application_review(db, monkeypatch, subject):
    db.tailoring_records.insert_one(record())
    msg = message("A security notification, not an application outcome.", subject=subject)
    monkeypatch.setattr(gs, "fetch_recent_messages", lambda *_: [msg])
    result = gs.sync_user(USER)
    assert result["auto_applied"] == result["suggested"] == result["deferred"] == result["model_calls"] == 0
    assert gs.extract_deterministic_signals([msg]) == {}
    assert gs.list_suggestions(USER) == []
    db.gmail_suggestions.insert_one({"suggestion_id": "legacy-auth", "user_email": USER,
        "subject": subject, "snippet": msg["snippet"], "record_id": "r1", "candidate_records": [{"record_id": "r1"}],
        "requires_status_selection": True, "suggested_status": None, "applied": False, "dismissed": False})
    assert gs.list_suggestions(USER) == []
    with pytest.raises(ValueError, match="evidence"):
        gs.apply_suggestion(USER, "legacy-auth", record_id="r1", status="rejected")
    assert db.tailoring_records.find_one()["application"]["status"] == "applied"


@pytest.mark.parametrize("subject,body", [
    ("Acme application update", "Please check your application portal for an update."),
    ("Acme interview logistics", "We will share your interview logistics for the Software Engineer role."),
    ("Acme consent document", "Please review the consent document for your application."),
    ("Verify your availability for an interview at Acme", "Please confirm a convenient time for an interview."),
    ("Verify your assessment submission for Acme", "Your application assessment has a new update in the portal."),
    ("Verify your candidate account documents before the Acme interview", "Please review the required interview documents."),
])
def test_unclear_application_and_consent_mail_remain_reviewable(db, monkeypatch, subject, body):
    db.tailoring_records.insert_one(record())
    monkeypatch.setattr(gs, "fetch_recent_messages", lambda *_: [message(body, subject=subject)])
    assert gs.sync_user(USER)["suggested"] == 1
    assert len(gs.list_suggestions(USER)) == 1


@pytest.mark.parametrize("retained", [
    "taking the time to apply for the backend engineer",
    "your interview logistics for the software development engineer",
    "software development engineer open",
])
def test_additional_observed_prose_wrappers_are_cleaned(retained):
    expected = "backend engineer" if "backend" in retained else "software development engineer"
    assert gs._clean_role_title(retained) == expected


def test_role_cleanup_preserves_open_source_specialty():
    assert gs._clean_role_title("Open Source Software Engineer") == "open source software engineer"


def test_retained_roles_and_partial_snippet_use_same_wrapper_rules(db):
    db.tailoring_records.insert_one(record(title="DevOps Engineer"))
    db.gmail_suggestions.insert_one({"suggestion_id": "partial-wrapper", "user_email": USER,
        "subject": "Acme application update", "email_roles": ["our devops engineer"],
        "snippet": "We received your application for our DevOps Engineer",
        "record_id": None, "candidate_records": [], "requires_application_match": True,
        "suggested_status": "applied", "applied": False, "dismissed": False})
    row = gs.list_suggestions(USER)[0]
    assert [r["record_id"] for r in row["candidate_records"]] == ["r1"]
    assert not row["requires_application_match"]
    assert row["requires_record_selection"]


@pytest.mark.parametrize("subject,body,sender", [
    ("Verify your candidate account", "Click this link to confirm your email address and complete setup for your candidate account: https://careers.example.test/activate/example", "notifications@otp.workday.com"),
    ("Reset your password", "We received a request to change your password.", "no-reply@icims.com"),
])
def test_account_setup_and_password_notices_are_excluded_from_saved_review(db, monkeypatch, subject, body, sender):
    db.tailoring_records.insert_one(record())
    msg = message(body, subject=subject)
    msg.update(from_address=sender, from_domain=sender.split("@")[-1])
    # A legacy company-only association must not turn an account event into a
    # status verdict, even when the provider is also used for applications.
    db.gmail_suggestions.insert_one({"suggestion_id": "old-account-notice", "user_email": USER,
        "subject": subject, "snippet": body, "from_address": sender,
        "record_id": "r1", "candidate_records": [{"record_id": "r1"}],
        "requires_status_selection": True, "suggested_status": None,
        "applied": False, "dismissed": False})
    assert gs.list_suggestions(USER) == []
    with pytest.raises(ValueError, match="evidence"):
        gs.apply_suggestion(USER, "old-account-notice", record_id="r1", status="rejected")
    assert db.tailoring_records.find_one()["application"]["status"] == "applied"
    assert gs._is_irrelevant_mail(msg)
    assert gs.extract_deterministic_signals([msg]) == {}
    monkeypatch.setattr(gs, "fetch_recent_messages", lambda *_: [msg])
    result = gs.sync_user(USER)
    assert result["auto_applied"] == result["suggested"] == result["deferred"] == result["model_calls"] == 0
