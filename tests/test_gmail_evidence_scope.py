"""Synthetic application mail: footer brands and job alerts are not outcomes."""
from copy import deepcopy
from datetime import datetime, timezone

import pytest

from _vg_support import current_db
from services import gmail_service as gs

USER = "evidence@example.test"


def record(record_id, company, title="Software Engineer", day=None):
    return {"record_id": record_id, "user_email": USER,
            "created_at": datetime(2026, 10, 1, tzinfo=timezone.utc),
            "jd_analysis": {"company": company, "job_title": title},
            "application": {"status": "draft", **({"applied_date": day} if day else {})}}


def mail(subject, body="", sender="no-reply@us.greenhouse-mail.io"):
    return {"message_id": "synthetic-mail", "subject": subject, "body": body,
            "snippet": body[:180], "from_name": "Recruiting platform",
            "from_address": sender, "from_domain": sender.split("@")[-1], "internal_date": 1000}


def candidates(msg, records):
    match = gs._match_message_to_record(msg, gs._build_record_index(records))
    return match, set() if not match else {r["record_id"] for r in match[0]["candidate_records"]}


def test_subject_employer_excludes_social_footer_company():
    msg = mail("Thank you for applying to GitLab", "We received your application for Backend Engineer. LinkedIn | Social profile")
    match, ids = candidates(msg, [record("gitlab", "GitLab", "Backend Engineer"), record("linkedin", "LinkedIn")])
    assert ids == {"gitlab"}
    assert match[1] >= gs.AUTO_APPLY_CONFIDENCE


@pytest.mark.parametrize("subject,body,sender", [
    ("Thank you for applying to Checkbook", "Your application has been received. LinkedIn | Social profile", "no-reply@us.greenhouse-mail.io"),
    ("Application to Stochastic successfully submitted", "Your application has been submitted! AI Engineer Stochastic. Recommended jobs NVIDIA Software Engineering Manager Cloud Streaming", "team@hi.wellfound.com"),
    ("Application received", "Thank you for applying to Checkbook. Your application has been received. LinkedIn | Social profile", "no-reply@us.greenhouse-mail.io"),
])
def test_untracked_primary_employer_cannot_fall_back_to_footer(subject, body, sender):
    match, _ = candidates(mail(subject, body, sender), [record("linkedin", "LinkedIn"), record("nvidia", "NVIDIA", "Software Engineering Manager Cloud Streaming")])
    assert match is None


def test_recommendation_role_cannot_raise_identity_confidence():
    msg = mail("Thank you for applying to GitLab", "Your application for Backend Engineer was received. Recommended jobs: Software Engineer at GitLab.")
    match, ids = candidates(msg, [record("backend", "GitLab", "Backend Engineer"), record("other", "GitLab")])
    assert ids == {"backend"}
    assert match[1] >= gs.AUTO_APPLY_CONFIDENCE


def test_subject_company_beats_other_employer_in_body_without_standard_template():
    msg = mail("Acme application update", "We have decided to move forward with other candidates. LinkedIn | Social profile")
    _, ids = candidates(msg, [record("acme", "Acme"), record("linkedin", "LinkedIn")])
    assert ids == {"acme"}


def test_same_company_same_role_stays_ambiguous_with_candidate_dates():
    match, ids = candidates(mail("SmartRent DevOps Engineer application", "We received your application."), [
        record("older", "SmartRent", "DevOps Engineer", "2026-10-01"),
        record("newer", "SmartRent", "DevOps Engineer", "2026-10-20"),
    ])
    assert ids == {"older", "newer"}
    assert match[0]["requires_record_selection"]
    assert {r["applied_date"] for r in match[0]["candidate_records"]} == {"2026-10-01", "2026-10-20"}
    assert all(isinstance(r["created_at"], str) for r in match[0]["candidate_records"])


@pytest.mark.parametrize("subject", ["Thank you for applying to Acme for the Software Engineer position", "Your application to Acme - Software Engineer"])
def test_primary_employer_ends_before_role_suffix(subject):
    _, ids = candidates(mail(subject, "We received your application. LinkedIn | Social profile"), [record("acme", "Acme"), record("linkedin", "LinkedIn")])
    assert ids == {"acme"}


def test_indeed_recommendation_is_neither_application_identity_nor_receipt():
    msg = mail("Site Reliability Engineer II @ RELX", "Your experience is a strong match. If you're interested, view this job and apply now.", "donotreply@match.indeed.com")
    assert candidates(msg, [record("relx", "RELX", "Site Reliability Engineer II")])[0] is None
    assert gs.extract_deterministic_signals([msg]) == {}


def test_conditional_interest_alone_is_not_a_submission():
    msg = mail("A role at Acme", "If you're interested, our recruiting team will review profiles.")
    assert "receipt" not in gs.extract_deterministic_signals([msg])


def test_real_receipt_with_conditional_boilerplate_remains_applied():
    msg = mail("Acme application", "We received your application. If you're selected, our team will reach out.")
    result = gs.reconcile_classification(gs.extract_deterministic_signals([msg]), None)
    assert result["status"] == "applied"


@pytest.mark.parametrize("company,title", [("Upwind", "DevOps Engineer"), ("Cohere Health", "Software Engineer II")])
def test_explicit_rejection_survives_recommendation_footer(company, title):
    msg = mail(f"{company} {title} application update", "We have decided to move forward with other candidates. Recommended jobs: OtherCo Engineer. If you're interested, apply now.")
    match, ids = candidates(msg, [record("correct", company, title), record("footer", "OtherCo", "Engineer")])
    assert ids == {"correct"}
    assert match[1] >= gs.AUTO_APPLY_CONFIDENCE
    assert gs.reconcile_classification(gs.extract_deterministic_signals([msg]), None)["status"] == "rejected"


def test_other_opportunities_in_rejection_prose_is_not_a_footer():
    msg = mail("Thank you for applying to Acme", "We considered you for other opportunities but unfortunately we will not be moving forward with your application.")
    assert gs.reconcile_classification(gs.extract_deterministic_signals([msg]), None)["status"] == "rejected"


def test_company_word_in_a_role_title_is_not_subject_employer_evidence():
    msg = mail("Guidewire Developer application update", "Regarding the Guidewire Developer role at Acme, we will not be moving forward.")
    _, ids = candidates(msg, [record("product-name", "Guidewire", "Guidewire Developer"), record("employer", "Acme", "Guidewire Developer")])
    assert ids == {"employer"}


def test_longer_explicit_employer_is_not_a_shorter_tracked_company():
    msg = mail("Thank you for applying to United Healthcare", "We received your application. Follow us on LinkedIn.")
    assert candidates(msg, [record("united", "United"), record("airline", "United Airlines"), record("social", "LinkedIn")])[0] is None


@pytest.fixture
def db(monkeypatch):
    db = current_db()
    for collection in ("gmail_connections", "gmail_suggestions", "tailoring_records"):
        db[collection].delete_many({})
    db.gmail_connections.insert_one({"user_email": USER})
    monkeypatch.setattr(gs, "classify_record_thread", lambda *_: pytest.fail("Recommendation reached the model"))
    return db


def suggestion(sid, rec, subject, snippet="", sender="no-reply@us.greenhouse-mail.io", candidate_records=None):
    return {"suggestion_id": sid, "user_email": USER, "record_id": rec["record_id"],
            "company": rec["jd_analysis"]["company"], "title": rec["jd_analysis"]["job_title"],
            "subject": subject, "snippet": snippet, "from_address": sender,
            "suggested_status": "applied", "applied": False, "dismissed": False,
            "candidate_records": candidate_records or [{"record_id": rec["record_id"], "company": rec["jd_analysis"]["company"], "title": rec["jd_analysis"]["job_title"]}]}


def test_recommendation_sync_has_no_status_suggestion_or_model_call(db, monkeypatch):
    db.tailoring_records.insert_one(record("relx", "RELX", "Site Reliability Engineer II"))
    monkeypatch.setattr(gs, "fetch_recent_messages", lambda *_: [mail("Site Reliability Engineer II @ RELX", "Strong match. If you're interested, apply now.", "donotreply@match.indeed.com")])
    result = gs.sync_user(USER)
    assert result["auto_applied"] == result["suggested"] == 0
    assert db.tailoring_records.find_one()["application"]["status"] == "draft"
    assert db.gmail_suggestions.count_documents({}) == 0


def test_stored_bad_suggestions_are_filtered_without_writes_and_good_ones_survive(db):
    records = [record("linkedin", "LinkedIn"), record("nvidia", "NVIDIA"), record("relx", "RELX"),
               record("stripe", "Stripe"), record("smart", "SmartRent", "DevOps Engineer", "2026-10-20")]
    db.tailoring_records.insert_many(records)
    rows = [suggestion("wrong-company", records[0], "Thank you for applying to Checkbook"),
            suggestion("wrong-footer", records[1], "Application to Stochastic successfully submitted"),
            suggestion("job-ad", records[2], "Site Reliability Engineer II @ RELX", "Strong match. If you're interested.", "donotreply@match.indeed.com"),
            suggestion("stripe", records[3], "Your application to Stripe", "We received your application."),
            suggestion("smart", records[4], "SmartRent application update", "We received your application.")]
    db.gmail_suggestions.insert_many(deepcopy(rows))
    before = list(db.gmail_suggestions.find())
    shown = gs.list_suggestions(USER)
    assert {s["suggestion_id"] for s in shown} == {"stripe", "smart"}
    smart = next(s for s in shown if s["suggestion_id"] == "smart")
    assert smart["candidate_records"][0]["applied_date"] == "2026-10-20"
    assert list(db.gmail_suggestions.find()) == before


def test_stored_mixed_candidates_are_pruned_on_read_and_apply(db):
    good, bad = record("gitlab", "GitLab", "Backend Engineer"), record("linkedin", "LinkedIn")
    db.tailoring_records.insert_many([good, bad])
    row = suggestion("mixed", bad, "Thank you for applying to GitLab", "Your application has been received.", candidate_records=[
        {"record_id": "linkedin", "company": "LinkedIn", "title": "Software Engineer"},
        {"record_id": "gitlab", "company": "GitLab", "title": "Backend Engineer"},
    ])
    row["requires_record_selection"] = True
    db.gmail_suggestions.insert_one(row)
    shown = gs.list_suggestions(USER)[0]
    assert shown["record_id"] == "gitlab"
    assert [r["record_id"] for r in shown["candidate_records"]] == ["gitlab"]
    assert not shown["requires_record_selection"]
    with pytest.raises(ValueError):
        gs.apply_suggestion(USER, "mixed", record_id="linkedin")
    assert db.tailoring_records.find_one({"record_id": "linkedin"})["application"]["status"] == "draft"
    assert gs.apply_suggestion(USER, "mixed")["record_id"] == "gitlab"


def test_known_wrong_stored_suggestion_cannot_be_applied_directly(db):
    rec = record("linkedin", "LinkedIn")
    db.tailoring_records.insert_one(rec)
    db.gmail_suggestions.insert_one(suggestion("bad", rec, "Thank you for applying to Checkbook"))
    with pytest.raises(ValueError, match="evidence"):
        gs.apply_suggestion(USER, "bad")
    assert db.tailoring_records.find_one()["application"]["status"] == "draft"
    assert "resolution_lock_id" not in db.gmail_suggestions.find_one()


@pytest.mark.parametrize("resolved_field", ["applied", "dismissed"])
def test_resolved_history_keeps_original_identity_even_when_evidence_was_bad(db, resolved_field):
    bad, good = record("linkedin", "LinkedIn"), record("gitlab", "GitLab")
    db.tailoring_records.insert_many([bad, good])
    row = suggestion("historical", bad, "Thank you for applying to GitLab", candidate_records=[
        {"record_id": "linkedin", "company": "LinkedIn", "title": "Software Engineer"},
        {"record_id": "gitlab", "company": "GitLab", "title": "Software Engineer"},
    ])
    row[resolved_field] = True
    db.gmail_suggestions.insert_one(row)
    assert gs.list_suggestions(USER) == []
    history = gs.list_suggestions(USER, include_resolved=True)
    assert len(history) == 1
    assert history[0]["record_id"] == "linkedin"
    assert history[0]["company"] == "LinkedIn"
    assert gs.apply_suggestion(USER, "historical")["record_id"] == "linkedin"


def test_filtered_rows_do_not_hide_older_valid_review_items(db):
    bad, good = record("linkedin", "LinkedIn"), record("stripe", "Stripe")
    db.tailoring_records.insert_many([bad, good])
    rows = []
    for i in range(100):
        row = suggestion(f"bad-{i}", bad, "Thank you for applying to Checkbook")
        row["created_at"] = datetime(2026, 10, 25, tzinfo=timezone.utc)
        rows.append(row)
    valid = suggestion("older-valid", good, "Your application to Stripe")
    valid["created_at"] = datetime(2026, 10, 1, tzinfo=timezone.utc)
    db.gmail_suggestions.insert_many(rows + [valid])
    assert [s["suggestion_id"] for s in gs.list_suggestions(USER)] == ["older-valid"]


def test_pending_choices_exclude_deleted_and_other_users_records(db):
    owned, foreign = record("owned", "Acme"), record("foreign", "Acme")
    foreign["user_email"] = "someone-else@example.test"
    db.tailoring_records.insert_many([owned, foreign])
    row = suggestion("choices", owned, "Your application to Acme", candidate_records=[
        {"record_id": key, "company": "Acme", "title": "Software Engineer"}
        for key in ("owned", "foreign", "deleted")])
    db.gmail_suggestions.insert_one(row)
    assert [r["record_id"] for r in gs.list_suggestions(USER)[0]["candidate_records"]] == ["owned"]
    for invalid in ("foreign", "deleted"):
        with pytest.raises(LookupError):
            gs.apply_suggestion(USER, "choices", record_id=invalid)


def test_partial_legacy_snippet_does_not_reassign_primary_candidate(db):
    first, second = record("first", "FirstCo"), record("second", "SecondCo")
    db.tailoring_records.insert_many([first, second])
    row = suggestion("legacy", first, "Application update", "Thank you for applying to SecondCo", candidate_records=[
        {"record_id": "first", "company": "FirstCo", "title": "Software Engineer"},
        {"record_id": "second", "company": "SecondCo", "title": "Software Engineer"},
    ])
    row.update(confidence=0.6, match_confidence=0.6, requires_record_selection=True)
    db.gmail_suggestions.insert_one(row)
    shown = gs.list_suggestions(USER)[0]
    assert shown["record_id"] == "first"
    assert shown["requires_record_selection"]
    assert shown["confidence"] == shown["match_confidence"] == 0.6


def test_preview_entities_are_plain_text_not_html():
    result = gs._serialize_suggestion({"subject": "We&#39;ve received your application", "snippet": "Thank you &amp; good luck"})
    assert result["subject"] == "We've received your application"
    assert result["snippet"] == "Thank you & good luck"
