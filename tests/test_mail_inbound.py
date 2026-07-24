"""Managed Application Mailbox — inbound Cloudflare webhook ROUTES (managed-mailbox-v1 §4).

Drives the real Flask routes on the resume Lambda app, fully offline (mongomock
+ a FakeStorage S3; no AWS, no Bedrock). Proves the two-phase HMAC protocol:
JWT-exempt but HMAC-gated (bad/missing signature -> 403, NEVER 401), envelope
recipient authoritative over a forged MIME To, one-use commit tokens, sha256
integrity, idempotency, and IDOR (owner resolved only from the alias).
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

import services.managed_mailbox_service as mailbox
import services.mail_store_service as store
from utils.webhook_hmac import sign
from _vg_support import load_service_app

DOMAIN = "pilotmail.co"
SECRET = "test-inbound-secret"
OWNER = "owner@x.com"
ALIAS_ADDR = "harshith.manne@pilotmail.co"
FIX = Path(__file__).resolve().parent / "fixtures" / "mail"

_COLLECTIONS = (
    "managed_mailboxes", "mail_aliases", "mail_threads", "mail_messages",
    "mail_attachments", "mail_commit_tokens", "tailoring_records",
)

SLOT = "/api/resume/mail-inbound/cloudflare/slot"
COMMIT = "/api/resume/mail-inbound/cloudflare/commit"


def _db():
    from utils.db_connect import DBConnect
    return DBConnect().get_db()


def _eml(name: str) -> bytes:
    return (FIX / name).read_bytes()


class FakeStorage:
    def __init__(self):
        self.objects = {}

    def put_object_bytes(self, key, body, *, bucket=None, content_type="application/octet-stream", kms_key_id=None):
        self.objects[key] = body
        return key

    def get_object_bytes(self, key, *, bucket=None):
        if key not in self.objects:
            raise KeyError(key)
        return self.objects[key]

    def generate_presigned_put(self, key, *, bucket=None, content_type=None, kms_key_id=None, expiry=900):
        headers = {"Content-Type": content_type} if content_type else {}
        if kms_key_id:
            headers["x-amz-server-side-encryption"] = "aws:kms"
        return {"url": f"https://s3.local/{key}", "headers": headers}


@pytest.fixture()
def ctx(monkeypatch):
    monkeypatch.setenv("USE_SSM_SECRETS", "false")
    monkeypatch.setenv("APPLY_MAIL_DOMAIN", DOMAIN)
    monkeypatch.setenv("APPLY_MAIL_BUCKET", "jobpilot-mail-test")
    monkeypatch.setenv("APPLY_MAIL_KMS_KEY_ID", "test-cmk")
    monkeypatch.setenv("MAIL_INBOUND_HMAC_SECRET", SECRET)

    db = _db()
    for c in _COLLECTIONS:
        db[c].delete_many({})
    mailbox.ensure_indexes()
    store.ensure_indexes()

    fake = FakeStorage()
    import services.s3_service as s3
    monkeypatch.setattr(s3, "get_storage_service", lambda: fake)

    mailbox.provision(OWNER, "Harshith Manne")

    app = load_service_app("resume")

    class Ctx:
        client = app.test_client()
        s3 = fake

    return Ctx()


def _slot_sig(to, frm, mid, size):
    return sign(SECRET, f"{to}\n{frm}\n{mid}\n{size}")


def _commit_sig(token, sha):
    return sign(SECRET, f"{token}\n{sha}")


def _do_slot(ctx, to=ALIAS_ADDR, frm="hr@corp.example", mid="<m-1@corp>", raw=b"x", sig=None):
    size = len(raw)
    headers = {}
    if sig is None:
        sig = _slot_sig(to, frm, mid, size)
    if sig != "":
        headers["X-JP-Signature"] = sig
    return ctx.client.post(
        SLOT,
        json={"envelope_to": to, "envelope_from": frm, "message_id": mid, "raw_size": size},
        headers=headers,
    )


def _roundtrip(ctx, raw, mid="<m-1@corp>", frm="hr@corp.example", to=ALIAS_ADDR,
               tamper_sha=None):
    """slot -> simulate the Worker's direct-to-S3 PUT -> commit."""
    slot_resp = _do_slot(ctx, to=to, frm=frm, mid=mid, raw=raw)
    assert slot_resp.status_code == 200, slot_resp.get_data(as_text=True)
    token = slot_resp.get_json()["commit_token"]

    # The Worker PUTs the raw straight to S3 via the presigned URL.
    raw_key = store.raw_key_for(OWNER, mid)
    ctx.s3.objects[raw_key] = raw

    sha = hashlib.sha256(raw).hexdigest()
    body_sha = tamper_sha if tamper_sha is not None else sha
    commit_resp = ctx.client.post(
        COMMIT,
        json={"commit_token": token, "raw_sha256": body_sha},
        headers={"X-JP-Signature": _commit_sig(token, body_sha)},
    )
    return slot_resp, commit_resp, token


# ---------------------------------------------------------------------------
# slot — HMAC gate + alias resolution
# ---------------------------------------------------------------------------

def test_slot_valid_hmac_returns_presigned_and_token(ctx):
    resp = _do_slot(ctx, raw=_eml("pdf_attachment.eml"), mid="<pdf-0001@corp.example>")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["commit_token"].startswith("ct_")
    assert body["upload_url"].startswith("https://s3.local/mail/raw/")
    assert body["put_headers"]["x-amz-server-side-encryption"] == "aws:kms"


def test_slot_tampered_hmac_is_403_not_401(ctx):
    resp = _do_slot(ctx, raw=b"hello", sig="deadbeef" * 8)
    assert resp.status_code == 403
    assert resp.status_code != 401


def test_slot_missing_hmac_is_403(ctx):
    resp = _do_slot(ctx, raw=b"hello", sig="")
    assert resp.status_code == 403


def test_slot_unknown_alias_is_404(ctx):
    resp = _do_slot(ctx, to="nobody@pilotmail.co", raw=b"hi")
    assert resp.status_code == 404
    assert "no_such_user" in resp.get_data(as_text=True)


def test_slot_retired_alias_is_404(ctx):
    # Rotate to retire the current local part; the old address must not resolve.
    mailbox.rotate(OWNER, "Harshith Manne")
    resp = _do_slot(ctx, to=ALIAS_ADDR, raw=b"hi")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# commit — full flow + integrity + idempotency
# ---------------------------------------------------------------------------

def test_full_roundtrip_stores_message(ctx):
    _slot, commit, _tok = _roundtrip(ctx, _eml("pdf_attachment.eml"), mid="<pdf-0001@corp.example>")
    assert commit.status_code == 200, commit.get_data(as_text=True)
    body = commit.get_json()
    assert body["ok"] is True
    assert body["deduplicated"] is False
    assert body["thread_id"] and body["message_id"]

    msg = _db().mail_messages.find_one({"message_id": body["message_id"]})
    assert msg["user_email"] == OWNER


def test_commit_tampered_hmac_is_403(ctx):
    slot_resp = _do_slot(ctx, raw=_eml("pdf_attachment.eml"), mid="<pdf-0001@corp.example>")
    token = slot_resp.get_json()["commit_token"]
    resp = ctx.client.post(
        COMMIT,
        json={"commit_token": token, "raw_sha256": "abc"},
        headers={"X-JP-Signature": "not-a-valid-sig"},
    )
    assert resp.status_code == 403


def test_commit_sha256_mismatch_is_422(ctx):
    _slot, commit, _tok = _roundtrip(
        ctx, _eml("pdf_attachment.eml"), mid="<pdf-0001@corp.example>",
        tamper_sha="0" * 64,
    )
    assert commit.status_code == 422
    assert "sha256_mismatch" in commit.get_data(as_text=True)
    # Nothing stored on an integrity failure.
    assert _db().mail_messages.count_documents({}) == 0


def test_commit_reused_token_is_409(ctx):
    _slot, commit, token = _roundtrip(ctx, _eml("pdf_attachment.eml"), mid="<pdf-0001@corp.example>")
    assert commit.status_code == 200
    # Replaying the same (now-used) token is a 409, treated as done by the Worker.
    sha = hashlib.sha256(_eml("pdf_attachment.eml")).hexdigest()
    replay = ctx.client.post(
        COMMIT,
        json={"commit_token": token, "raw_sha256": sha},
        headers={"X-JP-Signature": _commit_sig(token, sha)},
    )
    assert replay.status_code == 409


def test_forged_mime_to_owner_is_resolved_alias(ctx):
    # forged_to.eml MIME To = someone.else@pilotmail.co; envelope is harshith's.
    _slot, commit, _tok = _roundtrip(ctx, _eml("forged_to.eml"), mid="<forged-0001@corp.example>")
    assert commit.status_code == 200
    msg = _db().mail_messages.find_one({"message_id": commit.get_json()["message_id"]})
    assert msg["user_email"] == OWNER               # IDOR: owner from resolve only
    assert msg["to_addr"] == ALIAS_ADDR
    assert "someone.else" not in msg["to_addr"]


def test_idempotent_second_delivery_dedups(ctx):
    mid = "<pdf-0001@corp.example>"
    raw = _eml("pdf_attachment.eml")
    _s1, c1, _t1 = _roundtrip(ctx, raw, mid=mid)
    assert c1.status_code == 200 and c1.get_json()["deduplicated"] is False

    # Second delivery of the SAME provider message id: slot mints a done-flagged
    # token, commit is a no-op dedup — still exactly one stored message.
    _s2, c2, _t2 = _roundtrip(ctx, raw, mid=mid)
    assert c2.status_code == 200
    assert c2.get_json()["deduplicated"] is True
    assert _db().mail_messages.count_documents({}) == 1


def test_commit_structural_reject_is_422(ctx, monkeypatch):
    monkeypatch.setenv("MAIL_MAX_PARTS", "3")   # many_parts.eml has 8 parts
    _slot, commit, _tok = _roundtrip(ctx, _eml("many_parts.eml"), mid="<mp-1@corp>")
    assert commit.status_code == 422
    assert "message_rejected" in commit.get_data(as_text=True)
    assert _db().mail_messages.count_documents({}) == 0


def test_routes_reachable_without_jwt(ctx):
    # JWT-exempt: no Authorization header, and the failure mode is 403 (HMAC),
    # never 401 (which would trip the FE force-logout).
    resp = ctx.client.post(SLOT, json={"envelope_to": ALIAS_ADDR, "envelope_from": "a@b.c",
                                       "message_id": "<x@y>", "raw_size": 1})
    assert resp.status_code in (403,)   # bad/missing HMAC, not 401
    assert resp.status_code != 401


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
