"""Managed Application Mailbox — inbound STORAGE pipeline (managed-mailbox-v1 §2/§6/§8).

Offline: Mongo is mongomock (conftest/_vg_support); S3 is a FakeStorage that
captures every put/get in a dict (no AWS, no network, no Bedrock). We drive
``mail_store_service.ingest_committed`` with the real .eml fixtures and assert
the fail-closed §6 limits, magic-byte validation, HTML sanitization, and the §8
correlation-without-mutation guarantee.
"""
from __future__ import annotations

import hashlib
from datetime import timedelta
from pathlib import Path

import pytest

import services.managed_mailbox_service as mailbox
import services.mail_store_service as store

DOMAIN = "pilotmail.co"
OWNER = "owner@x.com"
ALIAS_ADDR = "harshith.manne@pilotmail.co"
FIX = Path(__file__).resolve().parent / "fixtures" / "mail"

_STORE_COLLECTIONS = (
    "managed_mailboxes", "mail_aliases", "mail_threads", "mail_messages",
    "mail_attachments", "mail_commit_tokens", "tailoring_records",
)


def _db():
    from utils.db_connect import DBConnect
    return DBConnect().get_db()


def _eml(name: str) -> bytes:
    return (FIX / name).read_bytes()


class FakeStorage:
    """In-memory stand-in for ResumeStorageService (managed-mailbox S3 helpers)."""

    def __init__(self):
        self.objects = {}

    def put_object_bytes(self, key, body, *, bucket=None, content_type="application/octet-stream", kms_key_id=None):
        # KMS must be threaded through in prod; assert the pipeline supplies it.
        self.objects[key] = body
        return key

    def get_object_bytes(self, key, *, bucket=None):
        return self.objects[key]

    def generate_presigned_put(self, key, *, bucket=None, content_type=None, kms_key_id=None, expiry=900):
        headers = {}
        if content_type:
            headers["Content-Type"] = content_type
        if kms_key_id:
            headers["x-amz-server-side-encryption"] = "aws:kms"
        return {"url": f"https://s3.local/{key}", "headers": headers}


@pytest.fixture()
def env(monkeypatch):
    monkeypatch.setenv("USE_SSM_SECRETS", "false")
    monkeypatch.setenv("APPLY_MAIL_DOMAIN", DOMAIN)
    monkeypatch.setenv("APPLY_MAIL_BUCKET", "jobpilot-mail-test")
    monkeypatch.setenv("APPLY_MAIL_KMS_KEY_ID", "test-cmk")
    db = _db()
    for c in _STORE_COLLECTIONS:
        db[c].delete_many({})
    mailbox.ensure_indexes()
    store.ensure_indexes()
    return monkeypatch


@pytest.fixture()
def fake_s3(monkeypatch):
    fake = FakeStorage()
    import services.s3_service as s3
    monkeypatch.setattr(s3, "get_storage_service", lambda: fake)
    return fake


@pytest.fixture()
def alias_info(env):
    """A provisioned, active alias resolved the way the route resolves it."""
    mailbox.provision(OWNER, "Harshith Manne")
    return mailbox.resolve_alias(ALIAS_ADDR)


# ---------------------------------------------------------------------------
# Happy path + storage discipline
# ---------------------------------------------------------------------------

def test_ingest_stores_thread_message_and_attachment(alias_info, fake_s3):
    raw = _eml("pdf_attachment.eml")
    res = store.ingest_committed(alias_info, ALIAS_ADDR, "hr@corp.example", raw, "<pdf-0001@corp.example>")

    assert res["deduplicated"] is False
    assert res["thread_id"] and res["message_id"]

    msg = _db().mail_messages.find_one({"message_id": res["message_id"]})
    assert msg["user_email"] == OWNER            # owner from alias, never the body
    assert msg["alias_id"] == alias_info["alias_id"]
    assert msg["direction"] == "inbound"
    # Bodies live in S3, not Mongo (D6): only keys are stored.
    assert msg["text_s3_key"] in fake_s3.objects
    assert "raw" not in msg and "html" not in msg

    thread = _db().mail_threads.find_one({"thread_id": res["thread_id"]})
    assert thread["message_count"] == 1 and thread["unread_count"] == 1

    atts = list(_db().mail_attachments.find({"message_id": res["message_id"]}))
    assert len(atts) == 1
    att = atts[0]
    assert att["scan_verdict"] == "clean"
    assert att["sniffed_content_type"] == "application/pdf"
    assert att["quarantined"] is False
    assert att["s3_key"] in fake_s3.objects   # clean attachment bytes stored


def test_never_stores_raw_mime_in_mongo(alias_info, fake_s3):
    raw = _eml("pdf_attachment.eml")
    store.ingest_committed(alias_info, ALIAS_ADDR, "hr@corp.example", raw, "<pdf-0001@corp.example>")
    msg = _db().mail_messages.find_one({"provider_message_id": "<pdf-0001@corp.example>"})
    # No field should carry the raw MIME bytes / base64.
    for v in msg.values():
        assert not (isinstance(v, (bytes, bytearray)))
    assert b"%PDF" not in repr(msg).encode()


# ---------------------------------------------------------------------------
# Envelope authority (forged MIME To ignored)
# ---------------------------------------------------------------------------

def test_forged_mime_to_ignored_owner_is_alias(alias_info, fake_s3):
    # forged_to.eml's MIME To is someone.else@pilotmail.co, but the envelope
    # recipient (authoritative) is harshith.manne — owner must be the alias owner.
    raw = _eml("forged_to.eml")
    res = store.ingest_committed(alias_info, ALIAS_ADDR, "hr@corp.example", raw, "<forged-0001@corp.example>")
    msg = _db().mail_messages.find_one({"message_id": res["message_id"]})
    assert msg["user_email"] == OWNER
    assert msg["to_addr"] == ALIAS_ADDR                 # envelope, not MIME To
    assert "someone.else" not in msg["to_addr"]


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

def test_idempotent_dedup_on_provider_message_id(alias_info, fake_s3):
    raw = _eml("pdf_attachment.eml")
    a = store.ingest_committed(alias_info, ALIAS_ADDR, "hr@corp.example", raw, "<dup-1@corp>")
    b = store.ingest_committed(alias_info, ALIAS_ADDR, "hr@corp.example", raw, "<dup-1@corp>")
    assert a["deduplicated"] is False
    assert b["deduplicated"] is True
    assert b["message_id"] == a["message_id"]
    assert _db().mail_messages.count_documents({"alias_id": alias_info["alias_id"]}) == 1


# ---------------------------------------------------------------------------
# §6 structural limits (fail-closed reject)
# ---------------------------------------------------------------------------

def test_part_count_over_limit_rejected(alias_info, fake_s3, env):
    env.setenv("MAIL_MAX_PARTS", "3")   # many_parts.eml has 8 MIME parts
    with pytest.raises(store.MailRejected):
        store.ingest_committed(alias_info, ALIAS_ADDR, "hr@corp.example", _eml("many_parts.eml"), "<mp-1@corp>")
    # A rejected message is never stored.
    assert _db().mail_messages.count_documents({}) == 0


def test_oversize_raw_rejected(alias_info, fake_s3, env):
    env.setenv("MAIL_MAX_RAW_BYTES", "10")
    with pytest.raises(store.MailRejected):
        store.ingest_committed(alias_info, ALIAS_ADDR, "hr@corp.example", _eml("pdf_attachment.eml"), "<big-1@corp>")
    assert _db().mail_messages.count_documents({}) == 0


# ---------------------------------------------------------------------------
# Attachment validation (magic bytes + zip bomb) — message stored, file isolated
# ---------------------------------------------------------------------------

def test_zipbomb_attachment_blocked_download(alias_info, fake_s3):
    res = store.ingest_committed(alias_info, ALIAS_ADDR, "hr@corp.example", _eml("zipbomb.eml"), "<zb-1@corp>")
    # The message IS visible in the inbox; the archive is isolated.
    assert res["message_id"]
    att = _db().mail_attachments.find_one({"message_id": res["message_id"]})
    assert att["scan_verdict"] == "blocked"
    assert att["quarantined"] is True
    assert att["s3_key"] is None                # download blocked (no key)
    assert att["s3_key"] not in fake_s3.objects


def test_magic_byte_mismatch_suspicious(alias_info, fake_s3):
    # mismatch_attachment.eml: filename report.pdf, declared application/pdf,
    # but the bytes are a PNG signature -> suspicious, download blocked.
    res = store.ingest_committed(alias_info, ALIAS_ADDR, "hr@corp.example", _eml("mismatch_attachment.eml"), "<mm-1@corp>")
    att = _db().mail_attachments.find_one({"message_id": res["message_id"]})
    assert att["scan_verdict"] == "suspicious"
    assert att["sniffed_content_type"] == "image/png"
    assert att["declared_content_type"].startswith("application/pdf")
    assert att["quarantined"] is True
    assert att["s3_key"] is None


# ---------------------------------------------------------------------------
# HTML sanitization (server-side)
# ---------------------------------------------------------------------------

def test_html_sanitized_strips_script_and_remote(alias_info, fake_s3):
    res = store.ingest_committed(alias_info, ALIAS_ADDR, "hr@corp.example", _eml("xss.eml"), "<xss-1@corp>")
    msg = _db().mail_messages.find_one({"message_id": res["message_id"]})
    safe = fake_s3.objects[msg["safe_html_s3_key"]].decode("utf-8").lower()
    assert "<script" not in safe
    assert "onerror" not in safe
    assert "onclick" not in safe
    assert "javascript:" not in safe
    assert "<iframe" not in safe
    assert "evil.example" not in safe            # remote img/iframe/link neutralized
    # The legitimate content survives.
    assert "interview" in safe


# ---------------------------------------------------------------------------
# §8 correlation — verified vs unverified, NEVER mutates application status
# ---------------------------------------------------------------------------

def _seed_application(status="applied", domain="greenhouse.io", hours_before=1):
    submitted = store._now() - timedelta(hours=hours_before)
    _db().tailoring_records.insert_one({
        "record_id": "rec-corr-1",
        "user_email": OWNER,
        "application": {"status": status, "employer_domain": domain, "applied_at": submitted},
        "jd_analysis": {"company": "Acme"},
    })


def test_correlation_verified_sets_trust_without_status_mutation(alias_info, fake_s3):
    _seed_application(status="applied", domain="greenhouse.io")
    res = store.ingest_committed(alias_info, ALIAS_ADDR, "no-reply@greenhouse.io", _eml("rejection_greenhouse.eml"), "<rej-1@greenhouse.io>")

    thread = _db().mail_threads.find_one({"thread_id": res["thread_id"]})
    assert thread["trust"] == "verified"
    assert thread["application_id"] == "rec-corr-1"

    # The pipeline NEVER transitions application status (D8) — still "applied".
    rec = _db().tailoring_records.find_one({"record_id": "rec-corr-1"})
    assert rec["application"]["status"] == "applied"

    # Evidence, not proof: SPF/DKIM/DMARC captured from the headers.
    msg = _db().mail_messages.find_one({"message_id": res["message_id"]})
    assert msg["spf"] == "pass" and msg["dkim"] == "pass" and msg["dmarc"] == "pass"


def test_correlation_unverified_when_no_matching_application(alias_info, fake_s3):
    # No application seeded -> the message lands in the low-trust bucket.
    res = store.ingest_committed(alias_info, ALIAS_ADDR, "no-reply@greenhouse.io", _eml("rejection_greenhouse.eml"), "<rej-2@greenhouse.io>")
    thread = _db().mail_threads.find_one({"thread_id": res["thread_id"]})
    assert thread["trust"] == "unverified"
    assert thread["application_id"] is None


def test_correlate_is_read_only(alias_info, fake_s3):
    _seed_application(status="applied", domain="greenhouse.io")
    out = store.correlate(OWNER, "greenhouse.io", store._now())
    assert out["trust"] == "verified"
    assert out["application_id"] == "rec-corr-1"
    # correlate() must not mutate the application.
    rec = _db().tailoring_records.find_one({"record_id": "rec-corr-1"})
    assert rec["application"]["status"] == "applied"


def test_correlate_ignores_application_after_received(alias_info, fake_s3):
    # An application submitted AFTER the mail arrived cannot correlate (timing).
    _seed_application(status="applied", domain="greenhouse.io", hours_before=-2)
    out = store.correlate(OWNER, "greenhouse.io", store._now())
    assert out["trust"] == "unverified"


# ---------------------------------------------------------------------------
# Commit-token store (two-phase handshake)
# ---------------------------------------------------------------------------

def test_commit_token_one_use(alias_info, fake_s3):
    tok = store.mint_commit_token(
        alias_info=alias_info, envelope_to=ALIAS_ADDR, envelope_from="hr@corp.example",
        message_id="<ct-1@corp>", raw_s3_key="mail/raw/x/ct.eml",
    )
    first = store.claim_commit_token(tok)
    assert first and first["user_email"] == OWNER
    # A second claim of the same token fails (one-use).
    assert store.claim_commit_token(tok) is None


def test_commit_token_expired_not_claimable(alias_info, fake_s3):
    tok = store.mint_commit_token(
        alias_info=alias_info, envelope_to=ALIAS_ADDR, envelope_from="hr@corp.example",
        message_id="<ct-2@corp>", raw_s3_key="mail/raw/x/ct2.eml",
    )
    # Force expiry into the past.
    _db().mail_commit_tokens.update_one(
        {"commit_token": tok}, {"$set": {"expires_at": store._now() - timedelta(minutes=1)}}
    )
    assert store.claim_commit_token(tok) is None


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
