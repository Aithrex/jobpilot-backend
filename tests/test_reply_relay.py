"""Managed Application Mailbox — privacy-preserving REPLY-RELAY.

The user replies to a forwarded recruiter email from their OWN inbox; the reply
lands on a per-thread reply-bound alias (``reply.<hex>@<domain>``) and is relayed
BACK to the single bound recruiter WITHOUT ever exposing the user's real address
— and it is NEVER an open relay (every defense is fail-closed).

Fully offline: Mongo is mongomock, S3 is a FakeStorage, and
``email_service.send_email`` is captured (no ZeptoMail/SES/network).

Proves:
  * mint_reply_alias is idempotent per (thread, correspondent) and resolves as
    ``reply_bound`` (only while active);
  * an owner reply is relayed to the bound correspondent ONLY, From = fallback
    sender (flag OFF) or the primary alias (flag ON) — NEVER the user's real email;
    Reply-To = the primary alias; X-JP-Relayed set; stored as ``outbound`` in the
    thread; not re-forwarded;
  * a NON-owner / spoofed sender, a failing DMARC, a loop-marked message, a
    suppressed correspondent, and an over-cap reply are all NOT relayed;
  * a recruiter emailing the reply-bound alias (not the owner) is rejected;
  * the relay never sends anywhere but the single bound correspondent;
  * the full slot->commit route branches to the relay for a reply-bound alias.
"""
from __future__ import annotations

import hashlib
import secrets

import pytest

import services.managed_mailbox_service as mailbox
import services.mail_store_service as store
import services.mail_forward_service as fwd
from utils.webhook_hmac import sign
from _vg_support import load_service_app

DOMAIN = "pilotmail.co"
OWNER = "owner@x.com"
FWD_TO = "harshith@aithrex.tech"          # the owner's VERIFIED real inbox
FORWARD_FROM = "notifications@aspirely.me"
PRIMARY = "harshith.manne@pilotmail.co"    # the owner's primary managed alias
RECRUITER = "hr@corp.example"
SECRET = "test-inbound-secret"

_COLLECTIONS = (
    "managed_mailboxes", "mail_aliases", "mail_threads", "mail_messages",
    "mail_attachments", "mail_commit_tokens", "mail_forward_verifications",
    "mail_forward_counters", "email_suppressions", "tailoring_records",
)


def _db():
    from utils.db_connect import DBConnect
    return DBConnect().get_db()


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
        return {"url": f"https://s3.local/{key}", "headers": {}}

    def generate_presigned_get(self, key, *, bucket=None, expiry=300):
        return f"https://s3.local/get/{key}?ttl={expiry}"

    def delete_object(self, key, *, bucket=None):
        self.objects.pop(key, None)
        return True


@pytest.fixture()
def env(monkeypatch):
    monkeypatch.setenv("USE_SSM_SECRETS", "false")
    monkeypatch.setenv("APPLY_MAIL_DOMAIN", DOMAIN)
    monkeypatch.setenv("APPLY_MAIL_BUCKET", "jobpilot-mail-test")
    monkeypatch.setenv("APPLY_MAIL_KMS_KEY_ID", "test-cmk")
    monkeypatch.setenv("MAIL_FORWARD_FROM", FORWARD_FROM)
    monkeypatch.setenv("MAIL_INBOUND_HMAC_SECRET", SECRET)
    # Reply-relay sending-as-alias is OFF by default (fallback sender).
    monkeypatch.delenv("APPLY_MAIL_SEND_AS_ALIAS", raising=False)
    db = _db()
    for c in _COLLECTIONS:
        db[c].delete_many({})
    mailbox.ensure_indexes()
    store.ensure_indexes()
    fake = FakeStorage()
    import services.s3_service as s3
    monkeypatch.setattr(s3, "get_storage_service", lambda: fake)
    return monkeypatch


@pytest.fixture()
def sent(monkeypatch):
    calls = []
    import services.email_service as es

    def _capture(to, subject, html, text=None, *, from_addr=None, reply_to=None, headers=None):
        calls.append({"to": to, "subject": subject, "html": html, "text": text,
                      "from_addr": from_addr, "reply_to": reply_to, "headers": headers})
        return True

    monkeypatch.setattr(es, "send_email", _capture)
    return calls


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------

def _provision_owner():
    mailbox.provision(OWNER, "Harshith Manne")
    mailbox.set_forwarding(OWNER, True, FWD_TO)
    _db().managed_mailboxes.update_one({"user_email": OWNER}, {"$set": {"forward_to_verified": True}})


def _seed_thread(correspondent=RECRUITER):
    """A recruiter thread + a reply-bound alias for (thread, correspondent).
    Returns (thread_id, reply_address, alias_info)."""
    tid = "thr_" + secrets.token_hex(8)
    _db().mail_threads.insert_one({
        "thread_id": tid, "user_email": OWNER, "subject": "Update on your application",
        "message_count": 1, "unread_count": 1,
    })
    mb = _db().managed_mailboxes.find_one({"user_email": OWNER})
    alias = mailbox.mint_reply_alias(OWNER, mb.get("mailbox_id"), tid, correspondent)
    info = mailbox.resolve_alias(alias["address"])
    return tid, alias["address"], info


def _reply_eml(*, from_addr=FWD_TO, to_addr=None, subject="Re: Update on your application",
               body="Thanks, I am very interested. When can we talk?",
               dmarc="pass", spf="pass", dkim="pass",
               mid=None, relayed=False, extra_body_email=None):
    mid = mid or f"<reply-{secrets.token_hex(4)}@aithrex.tech>"
    to_addr = to_addr or "reply.deadbeef@pilotmail.co"
    if extra_body_email:
        body = f"{body}\nAlso cc my friend {extra_body_email} please."
    lines = [
        f"Message-ID: {mid}",
        f"From: {from_addr}",
        f"To: {to_addr}",
        f"Subject: {subject}",
        "Date: Wed, 22 Jul 2026 10:00:00 +0000",
        (f"Authentication-Results: mx.pilotmail.co; spf={spf} smtp.mailfrom=aithrex.tech; "
         f"dkim={dkim} header.d=aithrex.tech; dmarc={dmarc} header.from=aithrex.tech"),
        "MIME-Version: 1.0",
        'Content-Type: text/plain; charset="utf-8"',
    ]
    if relayed:
        lines.append("X-JP-Relayed: 1")
    raw = "\r\n".join(lines) + "\r\n\r\n" + body + "\r\n"
    return raw.encode("utf-8")


# ---------------------------------------------------------------------------
# mint_reply_alias — format, idempotency, resolution
# ---------------------------------------------------------------------------

def test_mint_reply_alias_format_and_binding(env):
    _provision_owner()
    tid, addr, _info = _seed_thread()
    assert addr.startswith("reply.")
    assert addr.endswith(f"@{DOMAIN}")
    doc = _db().mail_aliases.find_one({"address": addr})
    assert doc["kind"] == "reply_bound"
    assert doc["state"] == "active"
    assert doc["thread_id"] == tid
    assert doc["correspondent_email"] == RECRUITER
    assert doc["primary_alias"] == PRIMARY
    assert doc["user_email"] == OWNER


def test_mint_reply_alias_idempotent_per_thread_correspondent(env):
    _provision_owner()
    mb = _db().managed_mailboxes.find_one({"user_email": OWNER})
    tid = "thr_" + secrets.token_hex(8)
    a = mailbox.mint_reply_alias(OWNER, mb["mailbox_id"], tid, RECRUITER)
    b = mailbox.mint_reply_alias(OWNER, mb["mailbox_id"], tid, RECRUITER)
    assert a["alias_id"] == b["alias_id"]
    assert a["address"] == b["address"]
    # Only ONE reply alias for this (thread, correspondent).
    assert _db().mail_aliases.count_documents(
        {"kind": "reply_bound", "thread_id": tid, "correspondent_email": RECRUITER}
    ) == 1
    # A DIFFERENT correspondent on the same thread gets a DIFFERENT alias.
    c = mailbox.mint_reply_alias(OWNER, mb["mailbox_id"], tid, "other@corp.example")
    assert c["address"] != a["address"]


def test_reply_alias_resolves_as_reply_bound(env):
    _provision_owner()
    tid, addr, info = _seed_thread()
    assert info is not None
    assert info["kind"] == "reply_bound"
    assert info["thread_id"] == tid
    assert info["correspondent_email"] == RECRUITER
    assert info["primary_alias"] == PRIMARY
    assert info["user_email"] == OWNER


def test_reply_alias_resolves_only_while_active(env):
    _provision_owner()
    _tid, addr, _info = _seed_thread()
    _db().mail_aliases.update_one({"address": addr}, {"$set": {"state": "paused"}})
    assert mailbox.resolve_alias(addr) is None


# ---------------------------------------------------------------------------
# Owner reply -> relayed to the bound correspondent ONLY
# ---------------------------------------------------------------------------

def test_owner_reply_relayed_fallback_sender(env, sent):
    _provision_owner()
    tid, addr, info = _seed_thread()
    raw = _reply_eml(to_addr=addr)
    out = fwd.relay_reply(info, FWD_TO, raw, "<reply-1@aithrex.tech>")

    assert out == {"relayed": True, "reason": "relayed"}
    assert len(sent) == 1
    call = sent[0]
    # Relayed to the ONE bound correspondent — nowhere else.
    assert call["to"] == RECRUITER
    # From = fallback MAIL_FORWARD_FROM (flag OFF) — NEVER the user's real email.
    assert FORWARD_FROM in call["from_addr"]
    assert FWD_TO not in call["from_addr"]
    assert "(via My Private Mail)" in call["from_addr"]
    # Reply-To = the PRIMARY alias so the recruiter's next reply returns to it.
    assert call["reply_to"] == PRIMARY
    # Loop marker set.
    assert call["headers"] == {"X-JP-Relayed": "1"}
    # Subject preserved (Re: kept). Body relayed.
    assert call["subject"] == "Re: Update on your application"
    assert "very interested" in (call["text"] or "")
    # Stored as OUTBOUND in the SAME thread.
    msg = _db().mail_messages.find_one({"thread_id": tid, "direction": "outbound"})
    assert msg is not None
    assert msg["relayed"] is True
    assert msg["to_addr"] == RECRUITER
    assert msg["x_jp_relayed"] is True
    # NEVER stored with the user's real email as From.
    assert msg["from_addr"] != FWD_TO


def test_owner_reply_relayed_from_primary_alias_when_flag_on(env, sent):
    env.setenv("APPLY_MAIL_SEND_AS_ALIAS", "true")
    _provision_owner()
    _tid, addr, info = _seed_thread()
    out = fwd.relay_reply(info, FWD_TO, _reply_eml(to_addr=addr), "<reply-2@aithrex.tech>")

    assert out["relayed"] is True
    call = sent[0]
    assert call["to"] == RECRUITER
    # From = the PRIMARY managed alias (verified sending domain) — never real email.
    assert PRIMARY in call["from_addr"]
    assert FWD_TO not in call["from_addr"]
    assert call["reply_to"] == PRIMARY


def test_relay_only_to_bound_correspondent_never_body(env, sent):
    _provision_owner()
    _tid, addr, info = _seed_thread()
    # The body names ANOTHER address; the relay must ignore it entirely.
    raw = _reply_eml(to_addr=addr, extra_body_email="attacker@evil.com")
    out = fwd.relay_reply(info, FWD_TO, raw, "<reply-3@aithrex.tech>")
    assert out["relayed"] is True
    assert len(sent) == 1
    assert sent[0]["to"] == RECRUITER
    assert sent[0]["to"] != "attacker@evil.com"


def test_owner_reply_not_reforwarded_to_user(env, sent):
    # Relaying the user's own reply must not also forward it back to the user's
    # inbox — the only send is the outbound relay to the recruiter.
    _provision_owner()
    _tid, addr, info = _seed_thread()
    fwd.relay_reply(info, FWD_TO, _reply_eml(to_addr=addr), "<reply-4@aithrex.tech>")
    assert all(c["to"] != FWD_TO for c in sent)
    assert len(sent) == 1


def test_relay_is_idempotent_no_double_send(env, sent):
    _provision_owner()
    _tid, addr, info = _seed_thread()
    raw = _reply_eml(to_addr=addr)
    a = fwd.relay_reply(info, FWD_TO, raw, "<reply-dup@aithrex.tech>")
    b = fwd.relay_reply(info, FWD_TO, raw, "<reply-dup@aithrex.tech>")
    assert a["relayed"] is True
    assert b == {"relayed": False, "reason": "duplicate"}
    assert len(sent) == 1  # relayed exactly once


# ---------------------------------------------------------------------------
# Open-relay defenses — all fail-closed
# ---------------------------------------------------------------------------

def test_non_owner_sender_not_relayed(env, sent):
    _provision_owner()
    _tid, addr, info = _seed_thread()
    raw = _reply_eml(from_addr="someone.else@evil.com", to_addr=addr)
    out = fwd.relay_reply(info, "someone.else@evil.com", raw, "<spoof-1@evil.com>")
    assert out == {"relayed": False, "reason": "sender_not_owner"}
    assert sent == []


def test_envelope_from_mismatch_not_relayed(env, sent):
    # MIME From is the owner but the ENVELOPE from is a spoofer -> fail closed.
    _provision_owner()
    _tid, addr, info = _seed_thread()
    raw = _reply_eml(from_addr=FWD_TO, to_addr=addr)
    out = fwd.relay_reply(info, "spoofer@evil.com", raw, "<spoof-2@evil.com>")
    assert out == {"relayed": False, "reason": "sender_not_owner"}
    assert sent == []


def test_failing_dmarc_not_relayed(env, sent):
    _provision_owner()
    _tid, addr, info = _seed_thread()
    raw = _reply_eml(to_addr=addr, dmarc="fail", spf="fail", dkim="fail")
    out = fwd.relay_reply(info, FWD_TO, raw, "<reply-df@aithrex.tech>")
    assert out == {"relayed": False, "reason": "auth_unaligned"}
    assert sent == []


def test_loop_marked_reply_not_relayed(env, sent):
    _provision_owner()
    _tid, addr, info = _seed_thread()
    raw = _reply_eml(to_addr=addr, relayed=True)
    out = fwd.relay_reply(info, FWD_TO, raw, "<reply-loop@aithrex.tech>")
    assert out == {"relayed": False, "reason": "loop_relayed_header"}
    assert sent == []


def test_suppressed_correspondent_not_relayed(env, sent):
    _provision_owner()
    _tid, addr, info = _seed_thread()
    from services import email_suppression
    email_suppression.add_suppression(RECRUITER, "bounce")
    out = fwd.relay_reply(info, FWD_TO, _reply_eml(to_addr=addr), "<reply-sup@aithrex.tech>")
    assert out == {"relayed": False, "reason": "suppressed"}
    assert sent == []


def test_owner_unverified_forward_to_not_relayed(env, sent):
    # forward_to is set but NOT verified -> we cannot prove the sender is the owner.
    mailbox.provision(OWNER, "Harshith Manne")
    mailbox.set_forwarding(OWNER, True, FWD_TO)  # verified stays False
    tid, addr, info = _seed_thread()
    out = fwd.relay_reply(info, FWD_TO, _reply_eml(to_addr=addr), "<reply-uv@aithrex.tech>")
    assert out == {"relayed": False, "reason": "owner_unverified"}
    assert sent == []


def test_rate_limit_blocks_second_reply(env, sent):
    env.setenv("MAIL_FORWARD_DAILY_CAP", "1")
    _provision_owner()
    _tid, addr, info = _seed_thread()
    r1 = fwd.relay_reply(info, FWD_TO, _reply_eml(to_addr=addr), "<reply-r1@aithrex.tech>")
    r2 = fwd.relay_reply(info, FWD_TO, _reply_eml(to_addr=addr), "<reply-r2@aithrex.tech>")
    assert r1["relayed"] is True
    assert r2 == {"relayed": False, "reason": "rate_limited"}
    assert len(sent) == 1


def test_recruiter_emailing_reply_alias_is_rejected(env, sent):
    # A recruiter (NOT the owner) emails the reply-bound alias directly. The reply
    # alias resolves, but the owner-proof check rejects it — it is never relayed.
    _provision_owner()
    _tid, addr, info = _seed_thread()
    raw = _reply_eml(from_addr=RECRUITER, to_addr=addr, dmarc="pass")
    out = fwd.relay_reply(info, RECRUITER, raw, "<recruiter-1@corp.example>")
    assert out["relayed"] is False
    assert out["reason"] == "sender_not_owner"
    assert sent == []


# ---------------------------------------------------------------------------
# Full slot -> commit route branches to the relay
# ---------------------------------------------------------------------------

@pytest.fixture()
def app(env, sent):
    return load_service_app("resume")


def _slot_sig(to, frm, mid, size):
    return sign(SECRET, f"{to}\n{frm}\n{mid}\n{size}")


def _commit_sig(token, sha):
    return sign(SECRET, f"{token}\n{sha}")


def test_full_route_reply_relay_roundtrip(app, sent):
    client = app.test_client()
    _provision_owner()
    tid, addr, _info = _seed_thread()
    raw = _reply_eml(to_addr=addr, mid="<route-reply-1@aithrex.tech>")

    slot = client.post(
        "/api/resume/mail-inbound/cloudflare/slot",
        json={"envelope_to": addr, "envelope_from": FWD_TO,
              "message_id": "<route-reply-1@aithrex.tech>", "raw_size": len(raw)},
        headers={"X-JP-Signature": _slot_sig(addr, FWD_TO, "<route-reply-1@aithrex.tech>", len(raw))},
    )
    assert slot.status_code == 200, slot.get_data(as_text=True)
    token = slot.get_json()["commit_token"]

    # Worker PUTs raw straight to S3.
    raw_key = store.raw_key_for(OWNER, "<route-reply-1@aithrex.tech>")
    import services.s3_service as s3
    s3.get_storage_service().objects[raw_key] = raw

    sha = hashlib.sha256(raw).hexdigest()
    commit = client.post(
        "/api/resume/mail-inbound/cloudflare/commit",
        json={"commit_token": token, "raw_sha256": sha},
        headers={"X-JP-Signature": _commit_sig(token, sha)},
    )
    assert commit.status_code == 200, commit.get_data(as_text=True)
    body = commit.get_json()
    assert body["ok"] is True
    assert body["relayed"] is True
    # Relayed to the recruiter; stored outbound in the thread.
    assert sent and sent[0]["to"] == RECRUITER
    assert _db().mail_messages.count_documents(
        {"thread_id": tid, "direction": "outbound"}
    ) == 1


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
