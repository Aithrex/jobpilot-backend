"""Account erasure covers every user-owned store (US launch purge audit,
2026-11-11).

`test_account_deletion.py` proves the generic cascade deletes what is listed.
This file proves the LIST is complete for the stores the audit found missing,
and covers the stores that are not a plain delete-by-key: managed-mailbox
objects, alias tombstones, hash-keyed alert failures, and the suppression list.
"""
import hashlib
import sys

import pytest
from bson import ObjectId

from _vg_support import current_db
from services import account_service

USER = "erase.me@example.com"
KEEP = "keep.me@example.com"

#: Every user-keyed collection found by grepping writers across
#: shared/python/services and services/*/ on 2026-11-11. Adding a user-owned
#: store means adding it here AND to CASCADE_COLLECTIONS.
AUDITED_USER_COLLECTIONS = {
    "user_preferences", "user_resumes", "resume_assets", "password_reset_tokens",
    "tailoring_records", "feedback", "tailor_usage", "feature_usage",
    "entitlement_overrides", "quota_ledger", "guest_claim_carryins",
    "graph_nodes", "graph_edges", "graph_evidence", "writing_samples", "style_profiles",
    "authored_resumes", "consents", "merge_candidates", "saved_jobs",
    "application_answers", "application_profiles", "applications", "approval_tokens",
    "ats_credentials", "ats_credential_audit", "credential_reveal_challenges",
    "auto_apply_answers", "auto_apply_freezes", "application_answer_library",
    "auto_submit_settings", "auto_submit_approvals", "auto_submit_audit",
    "job_activity_log", "job_filter_preferences", "pipeline_filter_presets",
    "user_apify_keys", "user_events", "networking_contacts", "offer_workspace",
    "differentiation_analyses", "github_profiles", "visa_profiles",
    "career_copilot_state", "career_copilot_outreach", "interview_attempts",
    "learning_paths", "learning_progress", "post_drafts", "post_metrics",
    "scheduled_posts", "linkedin_archives", "gmail_connections", "gmail_suggestions",
    "gmail_oauth_states", "github_connections", "linkedin_connections",
    "connect_oauth_states", "mail_ingress_addresses", "email_ingress",
    "mail_ingress_suggestions", "notification_prefs", "notification_log",
    "notification_consents", "resume_jobs",
    # the gaps closed on 2026-11-11
    "application_otp_grants", "enrichment_confirmations", "tailoring_evidence",
    "resume_assessments", "six_seconds_reads", "parse_streaks", "sprint_progress",
    "sprint_achievements", "sprint_submissions", "board_requests", "board_snapshots",
    "job_match_snapshots", "tech_feed_saved_searches", "tech_feed_alert_deliveries",
    "tech_feed_feedback", "tech_feed_searches", "tech_feed_company_requests",
    "tech_feed_company_request_audit", "managed_mailboxes", "mail_threads",
    "mail_messages", "mail_attachments", "mail_commit_tokens",
    "mail_forward_verifications", "mail_forward_counters",
}


@pytest.fixture()
def db(monkeypatch):
    database = current_db()
    for c in {"users", "mail_aliases", "tech_feed_alert_failures", "email_suppressions",
              *[c for c, _ in account_service.CASCADE_COLLECTIONS]}:
        database[c].delete_many({})
    monkeypatch.setattr(account_service, "_purge_s3_objects", lambda uid: 0)
    monkeypatch.setattr(account_service, "_revoke_providers", lambda e: {})
    # Another test may leave a fake storage singleton behind; keep these offline.
    monkeypatch.setattr(account_service, "_purge_asset_objects", lambda ids, keys=None: (0, []))
    yield database


def test_every_audited_user_store_is_in_the_cascade():
    listed = {c for c, _ in account_service.CASCADE_COLLECTIONS}
    assert AUDITED_USER_COLLECTIONS - listed == set()


def _seed_mail(db, email, n=2):
    for i in range(n):
        db.mail_threads.insert_one({"user_email": email, "thread_id": f"t{i}-{email}"})
        db.mail_messages.insert_one({
            "user_email": email, "thread_id": f"t{i}-{email}", "message_id": f"m{i}-{email}",
            "raw_s3_key": f"mail/raw/{email}/{i}.eml", "text_s3_key": f"mail/text/{email}/{i}.txt",
            "safe_html_s3_key": None})
        db.mail_attachments.insert_one({"user_email": email, "message_id": f"m{i}-{email}",
                                        "s3_key": f"mail/att/{email}/{i}.pdf"})
    db.mail_aliases.insert_one({"user_email": email, "alias_id": f"a-{email}",
                                "local_part": f"local.{email.split('@')[0]}",
                                "address": f"local.{email.split('@')[0]}@pilotmail.example",
                                "state": "active", "mailbox_id": "mbx", "kind": "primary"})
    db.managed_mailboxes.insert_one({"user_email": email, "mailbox_id": f"mbx-{email}"})


def test_purge_deletes_mail_objects_and_tombstones_aliases(db, monkeypatch):
    deleted_keys = []
    monkeypatch.setattr(account_service, "_delete_mail_objects",
                        lambda keys: (deleted_keys.extend(keys) or len(keys), []))
    for e in (USER, KEEP):
        db.users.insert_one({"_id": ObjectId(), "email": e})
        _seed_mail(db, e)
        db.board_requests.insert_one({"_id": e, "params": {}})
        db.tech_feed_alert_failures.insert_one({
            "user_hash": hashlib.sha256(e.encode()).hexdigest()[:16], "reason": "x"})

    report = account_service.hard_purge(USER)
    assert report["errors"] == []
    assert sorted(deleted_keys) == sorted(
        [f"mail/raw/{USER}/{i}.eml" for i in range(2)] + [f"mail/text/{USER}/{i}.txt" for i in range(2)]
        + [f"mail/att/{USER}/{i}.pdf" for i in range(2)])
    for coll in ("mail_threads", "mail_messages", "mail_attachments", "managed_mailboxes"):
        assert db[coll].count_documents({"user_email": USER}) == 0, coll
        assert db[coll].count_documents({"user_email": KEEP}) >= 1, coll
    assert db.board_requests.find_one({"_id": USER}) is None
    assert db.board_requests.find_one({"_id": KEEP}) is not None
    assert db.tech_feed_alert_failures.count_documents({}) == 1

    # The alias row survives as an anti-recycle tombstone with nothing personal
    # on it, so the address can never be handed to a stranger.
    tomb = db.mail_aliases.find_one({"alias_id": f"a-{USER}"})
    assert tomb["state"] == "retired" and tomb["local_part"] == "local.erase.me"
    assert "user_email" not in tomb and "mailbox_id" not in tomb
    assert db.mail_aliases.find_one({"alias_id": f"a-{KEEP}"})["user_email"] == KEEP
    assert db.users.find_one({"email": USER}) is None


def test_a_failed_mail_object_delete_keeps_rows_for_the_rerun(db, monkeypatch):
    db.users.insert_one({"_id": ObjectId(), "email": USER})
    _seed_mail(db, USER, n=1)
    monkeypatch.setattr(account_service, "_delete_mail_objects",
                        lambda keys: (0, ["mail/...: not deleted"]))
    report = account_service.hard_purge(USER)
    assert report["errors"]
    assert db.mail_messages.count_documents({"user_email": USER}) == 1
    assert db.mail_attachments.count_documents({"user_email": USER}) == 1
    assert db.users.find_one({"email": USER})["deletion_status"] == "purge_incomplete"

    monkeypatch.setattr(account_service, "_delete_mail_objects", lambda keys: (len(keys), []))
    rerun = account_service.hard_purge(USER)
    assert rerun["errors"] == []
    assert db.mail_messages.count_documents({"user_email": USER}) == 0
    assert db.users.find_one({"email": USER}) is None


def test_purge_without_mail_never_touches_storage(db, monkeypatch):
    def _boom(keys):
        raise AssertionError("storage must not be called")
    monkeypatch.setattr(account_service, "_delete_mail_objects", _boom)
    db.users.insert_one({"_id": ObjectId(), "email": USER})
    assert account_service.hard_purge(USER)["errors"] == []


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
