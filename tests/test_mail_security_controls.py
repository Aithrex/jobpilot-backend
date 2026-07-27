"""Managed Application Mailbox — P1 security controls (auto-apply audit).

Two controls from ``docs/security/auto-apply-audit.md``:

  * P1-RATE-IN — a per-alias (daily + hourly) AND per-sender-domain (hourly)
    atomic rate limit on the INBOUND ingest pipeline. Driven through the real
    Cloudflare slot route (mongomock + FakeStorage, no AWS): the N+1th new
    message in a window -> 429 (never 401), nothing minted/stored; an idempotent
    replay (same provider_message_id, already stored) does NOT consume budget;
    the per-sender-domain cap trips independently of the per-alias caps.

  * P1-FWD-TRUST — trust is DECOUPLED from forwarding. The mailbox is a plain
    full-content relay: BOTH verified (correlated) AND unverified (uncorrelated)
    mail forward the REAL message (original subject + sanitized body), from
    MAIL_FORWARD_FROM (never spoofing) with Reply-To = the original sender. Trust
    is retained only as the correlation signal for application STATUS — it never
    downgrades or blocks a forward. The optional MAIL_FORWARD_UNVERIFIED_MODE=
    suppress kill-switch still honors "send nothing" for unverified mail; the
    loop / suppression / rate guards still hold.

Both are fully offline (no AWS, no Bedrock, no network); ``email_service.send_email``
is captured.
"""
from __future__ import annotations

import hashlib
import secrets
from pathlib import Path

import pytest

import services.managed_mailbox_service as mailbox
import services.mail_store_service as store
import services.mail_forward_service as fwd
from utils.webhook_hmac import sign
from _vg_support import load_service_app

DOMAIN = "pilotmail.co"
SECRET = "test-inbound-secret"
OWNER = "owner@x.com"
OTHER = "other@x.com"
OWNER_ADDR = "harshith.manne@pilotmail.co"
OTHER_ADDR = "other.person@pilotmail.co"
FWD_TO = "harshith@aithrex.tech"
FORWARD_FROM = "notifications@aspirely.me"
FRONTEND = "https://app.test"
FIX = Path(__file__).resolve().parent / "fixtures" / "mail"

SLOT = "/api/resume/mail-inbound/cloudflare/slot"
COMMIT = "/api/resume/mail-inbound/cloudflare/commit"

_COLLECTIONS = (
    "managed_mailboxes", "mail_aliases", "mail_threads", "mail_messages",
    "mail_attachments", "mail_commit_tokens", "mail_forward_verifications",
    "mail_forward_counters", "mail_inbound_rate_counters", "email_suppressions",
    "tailoring_records",
)


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
        headers = {}
        if content_type:
            headers["Content-Type"] = content_type
        if kms_key_id:
            headers["x-amz-server-side-encryption"] = "aws:kms"
        return {"url": f"https://s3.local/{key}", "headers": headers}

    def generate_presigned_get(self, key, *, bucket=None, expiry=300):
        return f"https://s3.local/get/{key}?ttl={expiry}"


# ===========================================================================
# P1-RATE-IN — inbound ingest rate limit (route-driven)
# ===========================================================================

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
        mp = monkeypatch

    return Ctx()


def _slot_sig(to, frm, mid, size):
    return sign(SECRET, f"{to}\n{frm}\n{mid}\n{size}")


def _do_slot(ctx, to=OWNER_ADDR, frm="hr@corp.example", mid="<m-1@corp>", raw=b"x"):
    size = len(raw)
    return ctx.client.post(
        SLOT,
        json={"envelope_to": to, "envelope_from": frm, "message_id": mid, "raw_size": size},
        headers={"X-JP-Signature": _slot_sig(to, frm, mid, size)},
    )


def _roundtrip(ctx, mid, to=OWNER_ADDR, frm="hr@corp.example", raw=None):
    """slot -> Worker's direct-to-S3 PUT -> commit (stores the message)."""
    raw = raw if raw is not None else _eml("pdf_attachment.eml")
    slot = _do_slot(ctx, to=to, frm=frm, mid=mid, raw=raw)
    assert slot.status_code == 200, slot.get_data(as_text=True)
    token = slot.get_json()["commit_token"]
    # Resolve the owner for the raw key exactly as the pipeline does.
    owner = mailbox.resolve_alias(to)["user_email"]
    ctx.s3.objects[store.raw_key_for(owner, mid)] = raw
    sha = hashlib.sha256(raw).hexdigest()
    commit = ctx.client.post(
        COMMIT,
        json={"commit_token": token, "raw_sha256": sha},
        headers={"X-JP-Signature": sign(SECRET, f"{token}\n{sha}")},
    )
    return slot, commit


def test_rate_limit_alias_over_cap_is_429_not_stored(ctx):
    # Per-alias hourly cap = 2: the 3rd genuinely-new inbound trips 429.
    ctx.mp.setenv("MAIL_INBOUND_ALIAS_HOURLY_CAP", "2")

    r1 = _do_slot(ctx, mid="<a-1@corp>", raw=b"one")
    r2 = _do_slot(ctx, mid="<a-2@corp>", raw=b"two")
    r3 = _do_slot(ctx, mid="<a-3@corp>", raw=b"three")

    assert r1.status_code == 200 and r2.status_code == 200
    assert r3.status_code == 429                      # over cap
    assert r3.status_code != 401                      # never 401
    assert "rate_limited" in r3.get_data(as_text=True)
    # The rejected slot minted NO commit token (nothing enqueued/stored) — only
    # the two allowed slots did.
    assert "commit_token" not in (r3.get_json() or {})
    assert _db().mail_commit_tokens.count_documents({}) == 2


def test_rate_limit_duplicate_message_id_does_not_consume_budget(ctx):
    # Per-alias hourly cap = 1. One genuinely-new message consumes the whole
    # budget; a REPLAY of the same provider_message_id must still succeed (it is
    # dedup'd BEFORE the rate check) while a brand-new message is blocked.
    ctx.mp.setenv("MAIL_INBOUND_ALIAS_HOURLY_CAP", "1")

    _slot, commit = _roundtrip(ctx, mid="<dup-1@corp>")   # new -> consumes 1
    assert commit.status_code == 200 and commit.get_json()["deduplicated"] is False

    # Replay the SAME message id: already stored -> done-flagged token, NOT 429,
    # even though the alias budget is now exhausted.
    replay = _do_slot(ctx, mid="<dup-1@corp>", raw=b"whatever")
    assert replay.status_code == 200
    assert replay.get_json()["commit_token"].startswith("ct_")

    # A genuinely NEW message is over cap -> 429 (proves the replay never spent
    # budget; only the first new message did).
    fresh = _do_slot(ctx, mid="<new-1@corp>", raw=b"fresh")
    assert fresh.status_code == 429


def test_rate_limit_sender_domain_trips_independently(ctx):
    # Alias caps generous; per-sender-domain hourly cap = 2. Three messages from
    # the SAME sender domain across DIFFERENT aliases trip the domain backstop
    # even though no single alias is near its own cap.
    ctx.mp.setenv("MAIL_INBOUND_ALIAS_HOURLY_CAP", "100")
    ctx.mp.setenv("MAIL_INBOUND_ALIAS_DAILY_CAP", "100")
    ctx.mp.setenv("MAIL_INBOUND_DOMAIN_HOURLY_CAP", "2")
    mailbox.provision(OTHER, "Other Person")

    r1 = _do_slot(ctx, to=OWNER_ADDR, frm="hr@corp.example", mid="<d-1@corp>", raw=b"1")
    r2 = _do_slot(ctx, to=OTHER_ADDR, frm="hr@corp.example", mid="<d-2@corp>", raw=b"2")
    r3 = _do_slot(ctx, to=OWNER_ADDR, frm="hr@corp.example", mid="<d-3@corp>", raw=b"3")

    assert r1.status_code == 200 and r2.status_code == 200
    assert r3.status_code == 429                      # domain cap, not alias cap
    assert "rate_limited" in r3.get_data(as_text=True)


def test_rate_limit_helper_returns_reason_label_only(ctx):
    # The helper returns a PII-free counter label (no alias/sender/body).
    ctx.mp.setenv("MAIL_INBOUND_ALIAS_HOURLY_CAP", "1")
    ok, reason = store.check_inbound_rate("alias_abc", "hr@corp.example")
    assert ok is True and reason is None
    ok2, reason2 = store.check_inbound_rate("alias_abc", "hr@corp.example")
    assert ok2 is False and reason2 == "alias_hourly"
    assert "alias_abc" not in (reason2 or "")
    assert "corp.example" not in (reason2 or "")


# ===========================================================================
# P1-FWD-TRUST — forwarding trust gate (service-driven)
# ===========================================================================

@pytest.fixture()
def env(monkeypatch):
    monkeypatch.setenv("USE_SSM_SECRETS", "false")
    monkeypatch.setenv("APPLY_MAIL_DOMAIN", DOMAIN)
    monkeypatch.setenv("APPLY_MAIL_BUCKET", "jobpilot-mail-test")
    monkeypatch.setenv("APPLY_MAIL_KMS_KEY_ID", "test-cmk")
    monkeypatch.setenv("MAIL_FORWARD_FROM", FORWARD_FROM)
    monkeypatch.setenv("FRONTEND_URL", FRONTEND)
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


def _enable_forwarding():
    mailbox.provision(OWNER, "Harshith Manne")
    mailbox.set_forwarding(OWNER, True, FWD_TO)
    _db().managed_mailboxes.update_one({"user_email": OWNER}, {"$set": {"forward_to_verified": True}})


RECRUITER_SUBJECT = "Update on your application to Acme"
RECRUITER_BODY = "We would like to invite you to interview. Please pick a slot."


def _seed(*, trust, subject=RECRUITER_SUBJECT, from_name="Acme Recruiting",
          text=RECRUITER_BODY, safe_html="<p>Interview invitation.</p>", status="rejected"):
    mid = "msg_" + secrets.token_hex(8)
    tid = "thr_" + secrets.token_hex(8)
    bucket = store._apply_mail_bucket()
    storage = store.get_storage_service()
    text_key = f"mail/text/seed/{mid}.txt"
    html_key = f"mail/html/seed/{mid}.html" if safe_html else None
    storage.put_object_bytes(text_key, (text or "").encode("utf-8"), bucket=bucket)
    if html_key:
        storage.put_object_bytes(html_key, safe_html.encode("utf-8"), bucket=bucket)
    _db().mail_messages.insert_one({
        "message_id": mid, "provider_message_id": "pm_" + secrets.token_hex(6),
        "thread_id": tid, "user_email": OWNER, "alias_id": "alias_x",
        "direction": "inbound", "from_addr": "no-reply@corp.example",
        "from_name": from_name, "from_domain": "corp.example",
        "to_addr": OWNER_ADDR, "subject": subject, "trust": trust,
        "text_s3_key": text_key, "safe_html_s3_key": html_key,
        "classification": {"status": status} if status else None,
        "x_jp_forwarded": False,
    })
    _db().mail_threads.insert_one({
        "thread_id": tid, "user_email": OWNER, "subject": subject,
        "status_suggestion": {"suggested": status} if status else None,
        "trust": trust,
    })
    return {"thread_id": tid, "message_id": mid, "deduplicated": False}


def test_verified_message_relays_full_content(env, sent):
    _enable_forwarding()
    res = _seed(trust="verified")
    out = fwd.forward_notification(res)

    assert out == {"sent": True, "reason": "sent"}
    call = sent[0]
    # Plain full-content relay: ORIGINAL subject + real body, our From, reply-to.
    assert call["subject"] == RECRUITER_SUBJECT
    assert "invite you to interview" in call["text"]
    assert FORWARD_FROM in call["from_addr"]
    # Privacy-preserving reply-bound alias (reply.<hex>@APPLY_MAIL_DOMAIN), not
    # the raw recruiter address — inbound reply-relay keeps the user's email private.
    assert str(call["reply_to"]).startswith("reply.")
    assert str(call["reply_to"]).endswith("@pilotmail.co")
    # Compact manage link still present (the body IS the message, not a redirect).
    deep = f"{FRONTEND}/next/applications/{res['thread_id']}"
    assert deep in call["html"] and deep in call["text"]


def test_unverified_message_still_relays_full_content(env, sent):
    # Trust is DECOUPLED: unverified mail forwards the SAME full content — no
    # downgrade to a generic notice (the product wants the real message).
    _enable_forwarding()
    res = _seed(trust="unverified")
    out = fwd.forward_notification(res)

    assert out == {"sent": True, "reason": "sent"}
    call = sent[0]
    assert call["subject"] == RECRUITER_SUBJECT
    assert "invite you to interview" in call["text"]
    assert FORWARD_FROM in call["from_addr"]
    assert str(call["reply_to"]).startswith("reply.")
    assert str(call["reply_to"]).endswith("@pilotmail.co")


def test_unverified_suppress_toggle_sends_nothing(env, sent):
    env.setenv("MAIL_FORWARD_UNVERIFIED_MODE", "suppress")
    _enable_forwarding()
    out = fwd.forward_notification(_seed(trust="unverified"))
    assert out == {"sent": False, "reason": "unverified_suppressed"}
    assert sent == []


def test_verified_still_full_under_suppress_mode(env, sent):
    # The suppress kill-switch only gates UNVERIFIED mail; verified still forwards.
    env.setenv("MAIL_FORWARD_UNVERIFIED_MODE", "suppress")
    _enable_forwarding()
    res = _seed(trust="verified")
    out = fwd.forward_notification(res)
    assert out == {"sent": True, "reason": "sent"}
    assert sent[0]["subject"] == RECRUITER_SUBJECT


def test_missing_trust_still_relays_full_content(env, sent):
    # Fail-safe: a blank trust verdict is treated as unverified — which, with the
    # default (forward) mode, STILL relays full content (trust never blocks it).
    _enable_forwarding()
    res = _seed(trust="")
    out = fwd.forward_notification(res)
    assert out["sent"] is True
    assert sent[0]["subject"] == RECRUITER_SUBJECT


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
