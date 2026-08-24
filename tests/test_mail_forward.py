"""Managed Application Mailbox — PLAIN full-content forwarding to the user inbox.

The "user gets the rejection at their ORIGINAL email" half of the Greenhouse
acceptance scenario (managed-mailbox-v1 §8.1 step 7), redesigned as a Hide-My-
Email-style relay. Fully offline: Mongo is mongomock (conftest/_vg_support), S3
is a FakeStorage, and ``email_service.send_email`` is monkeypatched to CAPTURE
every send (no ZeptoMail/SES/network). We assert:

  * a forward is sent ONLY when enabled + verified + not-paused + not-suppressed,
    and each gate INDIVIDUALLY blocks it;
  * From = MAIL_FORWARD_FROM with a "(via My Private Mail)" display name (never
    the recruiter — no spoof); Reply-To = the ORIGINAL sender;
  * the body is the REAL sanitized message (full content) + a compact
    ``/app/apply/<thread_id>`` manage link — NOT the retired generic notice;
  * the open-relay / loop guards (own domain / self / X-JP-Forwarded) block;
  * the per-user daily rate cap skips the overflow;
  * the verification mint/confirm flow, its OLD-destination + IDOR rejections,
    and the two JWT-required endpoints (owner-scoped, business failures never 401).
"""
from __future__ import annotations

import secrets
from datetime import timedelta
from pathlib import Path

import pytest

import services.managed_mailbox_service as mailbox
import services.mail_store_service as store
import services.mail_forward_service as fwd
from _vg_support import load_service_app

DOMAIN = "pilotmail.co"
OWNER = "owner@x.com"
OTHER = "other@x.com"
FWD_TO = "harshith@aithrex.tech"
FORWARD_FROM = "notifications@aspirely.me"
FRONTEND = "https://app.test"
ALIAS_ADDR = "harshith.manne@pilotmail.co"
FIX = Path(__file__).resolve().parent / "fixtures" / "mail"

_COLLECTIONS = (
    "managed_mailboxes", "mail_aliases", "mail_threads", "mail_messages",
    "mail_attachments", "mail_commit_tokens", "mail_forward_verifications",
    "mail_forward_counters", "email_suppressions", "tailoring_records",
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
    monkeypatch.setenv("FRONTEND_URL", FRONTEND)
    db = _db()
    for c in _COLLECTIONS:
        db[c].delete_many({})
    mailbox.ensure_indexes()
    store.ensure_indexes()
    # Patch S3 so seeded messages can persist (and the relay can re-read) bodies.
    fake = FakeStorage()
    import services.s3_service as s3
    monkeypatch.setattr(s3, "get_storage_service", lambda: fake)
    return monkeypatch


# Deterministic stand-in for the Haiku classifier that backs the §6 allow-list
# (no Bedrock in tests). First match wins, so the mixed "thank you for applying
# … not moving forward" rejection lands on `rejected`.
_FAKE_STATUS = (
    ("moving forward", "rejected"),
    ("careful review", "rejected"),
    ("interview", "interview_invite"),
    ("assessment", "oa_sent"),
    ("pleased to extend an offer", "offer"),
    ("received your application", "applied"),
    ("thank you for applying", "applied"),
)


@pytest.fixture(autouse=True)
def classifier(monkeypatch):
    """Patch ``mail_ingress_service.classify`` — the allow-list's status oracle.
    ``mail_forward_service`` imports it INSIDE the call, so patching the module
    attribute is enough. Keeps the suite fully offline."""
    import services.mail_ingress_service as ingress

    def _fake(from_domain, subject, body_text):
        blob = f"{subject or ''}\n{body_text or ''}".lower()
        for needle, status in _FAKE_STATUS:
            if needle in blob:
                return {"status": status, "confidence": 0.95, "company": "",
                        "role": "", "event_at": None, "evidence_sentence": ""}
        return {"status": None, "confidence": 0.0, "company": "", "role": "",
                "event_at": None, "evidence_sentence": ""}

    monkeypatch.setattr(ingress, "classify", _fake)
    return _fake


@pytest.fixture()
def sent(monkeypatch):
    """Capture every email_service.send_email call (no real provider)."""
    calls = []
    import services.email_service as es

    def _capture(to, subject, html, text=None, *, from_addr=None, reply_to=None, headers=None):
        calls.append({
            "to": to, "subject": subject, "html": html, "text": text,
            "from_addr": from_addr, "reply_to": reply_to, "headers": headers,
        })
        return True

    monkeypatch.setattr(es, "send_email", _capture)
    return calls


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------

def _enable_forwarding(*, verified=True, paused=False, forward_to=FWD_TO, enabled=True):
    mailbox.provision(OWNER, "Harshith Manne")
    mailbox.set_forwarding(OWNER, enabled, forward_to)
    updates = {}
    if verified:
        updates["forward_to_verified"] = True
    if paused:
        updates["paused"] = True
    if updates:
        _db().managed_mailboxes.update_one({"user_email": OWNER}, {"$set": updates})


def _seed_message(*, from_addr="no-reply@greenhouse.io", from_domain="greenhouse.io",
                  x_jp_forwarded=False, status="rejected", subject="Update on your application",
                  user_email=OWNER, trust="verified",
                  text="Thank you for applying. After careful review we will not be moving forward.",
                  safe_html="<p>Thank you for applying to <b>Acme</b>.</p>"):
    # Ingested messages ALWAYS carry a trust verdict (correlate sets it); default
    # to "verified" here. The plain full-content relay re-reads the sanitized
    # bodies from S3, so we persist them to the patched FakeStorage and record the
    # keys on the message (exactly as ingest_committed would).
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
        "thread_id": tid, "user_email": user_email,
        "alias_id": "alias_x", "direction": "inbound",
        "from_addr": from_addr, "from_name": "Greenhouse", "from_domain": from_domain,
        "to_addr": ALIAS_ADDR, "subject": subject, "trust": trust,
        "text_s3_key": text_key, "safe_html_s3_key": html_key,
        "classification": {"status": status} if status else None,
        "x_jp_forwarded": x_jp_forwarded,
    })
    _db().mail_threads.insert_one({
        "thread_id": tid, "user_email": user_email, "subject": subject,
        "status_suggestion": {"suggested": status} if status else None,
    })
    return {"thread_id": tid, "message_id": mid, "deduplicated": False}


# ---------------------------------------------------------------------------
# Happy path — plain full-content relay (real subject/body, our From, reply-to)
# ---------------------------------------------------------------------------

def test_forward_sent_when_all_gates_pass(env, sent):
    _enable_forwarding()
    res = _seed_message()
    out = fwd.forward_notification(res)

    assert out == {"sent": True, "reason": "sent"}
    assert len(sent) == 1
    call = sent[0]
    assert call["to"] == FWD_TO
    # From = MAIL_FORWARD_FROM (JobPilot-owned) + a "(via My Private Mail)"
    # display name; the recruiter address is NEVER the From (no spoof).
    assert FORWARD_FROM in call["from_addr"]
    assert "(via My Private Mail)" in call["from_addr"]
    assert "greenhouse.io" not in (call["from_addr"] or "")
    # Reply-To = a privacy-preserving per-thread reply-bound alias (NOT the raw
    # recruiter), so a reply from the user's inbox routes through the relay and
    # never exposes their real address.
    assert call["reply_to"].startswith("reply.")
    assert call["reply_to"].endswith(f"@{DOMAIN}")
    assert call["reply_to"] != "no-reply@greenhouse.io"
    # Outbound loop marker present.
    assert call["headers"] == {"X-JP-Forwarded": "1"}
    # ORIGINAL subject preserved on the header.
    assert call["subject"] == "Update on your application"
    # LINK-ONLY (spec §6): a status headline + the deep link in BOTH parts; the
    # recruiter body is NOT relayed — it stays in the in-app inbox.
    deep = f"{FRONTEND}/app/apply/{res['thread_id']}"
    assert deep in call["html"]
    assert deep in call["text"]
    assert "passed on your application" in call["html"]
    # OWNER DECISION (2026-08-24): the sanitized body is relayed now, so the
    # employer's words DO appear — in the HTML part only. The plain-text part
    # stays a pointer, so a text-only client still receives no untrusted markup.
    assert "Thank you for applying" in call["html"]
    assert "Thank you for applying" not in (call["text"] or "")
    assert "moving forward" not in call["text"].lower()


def test_forward_carries_the_body_and_inbox_keeps_everything(env, sent):
    # Full pipeline: ingest a real Greenhouse rejection to S3, then notify.
    _enable_forwarding()
    _db().tailoring_records.insert_one({
        "record_id": "rec-1", "user_email": OWNER,
        "application": {"status": "applied", "employer_domain": "greenhouse.io",
                        "applied_at": store._now() - timedelta(hours=1)},
        "jd_analysis": {"company": "Acme"},
    })
    alias_info = mailbox.resolve_alias(ALIAS_ADDR)
    res = store.ingest_committed(
        alias_info, ALIAS_ADDR, "no-reply@greenhouse.io",
        _eml("rejection_greenhouse.eml"), "<rej-0001@greenhouse.io>",
    )
    out = store.maybe_forward(res)

    assert out["sent"] is True
    assert len(sent) == 1
    html = sent[0]["html"]
    blob = (html + sent[0]["text"]).lower()
    # The IN-APP inbox kept the full message (no content drop at ingest)...
    stored = _db().mail_messages.find_one({"message_id": res["message_id"]})
    assert stored["text_s3_key"] and stored["safe_html_s3_key"]
    # ...and the personal inbox now receives the sanitized body too, per the
    # owner's decision to reverse link-only. The tradeoff is explicit: employer
    # HTML reaches the real inbox; only the RAW MIME is still never relayed.
    assert "careful review" in blob
    assert "moving forward" in blob
    # ORIGINAL subject + the "read it in app" link.
    assert sent[0]["subject"] == "Update on your application to Acme"
    assert f"{FRONTEND}/app/apply/{res['thread_id']}" in html


# ---------------------------------------------------------------------------
# Each gate individually blocks the send
# ---------------------------------------------------------------------------

def test_paused_blocks_forward(env, sent):
    _enable_forwarding(paused=True)
    out = fwd.forward_notification(_seed_message())
    assert out == {"sent": False, "reason": "paused"}
    assert sent == []


def test_forwarding_disabled_blocks_forward(env, sent):
    _enable_forwarding(enabled=False)
    out = fwd.forward_notification(_seed_message())
    assert out == {"sent": False, "reason": "forwarding_disabled"}
    assert sent == []


def test_unverified_destination_blocks_forward(env, sent):
    _enable_forwarding(verified=False)
    out = fwd.forward_notification(_seed_message())
    assert out == {"sent": False, "reason": "forward_to_unverified"}
    assert sent == []


def test_no_destination_blocks_forward(env, sent):
    mailbox.provision(OWNER, "Harshith Manne")
    # Provisioning now DEFAULTS forward_to to the owner's registered address
    # (spec §6), so a destination-less mailbox is a legacy/hand-cleared one.
    _db().managed_mailboxes.update_one(
        {"user_email": OWNER}, {"$set": {"forwarding_enabled": True, "forward_to": None}}
    )
    out = fwd.forward_notification(_seed_message())
    assert out == {"sent": False, "reason": "no_forward_to"}
    assert sent == []


def test_suppressed_destination_blocks_forward(env, sent):
    _enable_forwarding()
    from services import email_suppression
    email_suppression.add_suppression(FWD_TO, "bounce")
    out = fwd.forward_notification(_seed_message())
    assert out == {"sent": False, "reason": "suppressed"}
    assert sent == []


def test_forward_from_unconfigured_is_inert(env, sent):
    env.setenv("MAIL_FORWARD_FROM", "")   # no verified relay sender
    _enable_forwarding()
    out = fwd.forward_notification(_seed_message())
    assert out == {"sent": False, "reason": "forward_from_unconfigured"}
    assert sent == []


def test_deduplicated_redelivery_not_reforwarded(env, sent):
    _enable_forwarding()
    res = _seed_message()
    res["deduplicated"] = True
    out = fwd.forward_notification(res)
    assert out == {"sent": False, "reason": "deduplicated"}
    assert sent == []


# ---------------------------------------------------------------------------
# Open-relay / loop guards
# ---------------------------------------------------------------------------

def test_loop_guard_own_apply_domain(env, sent):
    _enable_forwarding()
    out = fwd.forward_notification(
        _seed_message(from_addr=f"x@{DOMAIN}", from_domain=DOMAIN)
    )
    assert out == {"sent": False, "reason": "loop_own_domain"}
    assert sent == []


def test_loop_guard_self_forward_from(env, sent):
    _enable_forwarding()
    out = fwd.forward_notification(
        _seed_message(from_addr=FORWARD_FROM, from_domain="aspirely.me")
    )
    assert out == {"sent": False, "reason": "loop_self"}
    assert sent == []


def test_loop_guard_x_jp_forwarded_header(env, sent):
    _enable_forwarding()
    out = fwd.forward_notification(_seed_message(x_jp_forwarded=True))
    assert out == {"sent": False, "reason": "loop_forwarded_header"}
    assert sent == []


# ---------------------------------------------------------------------------
# Per-user daily rate cap
# ---------------------------------------------------------------------------

def test_rate_limit_skips_overflow(env, sent):
    env.setenv("MAIL_FORWARD_DAILY_CAP", "2")
    _enable_forwarding()
    r1 = fwd.forward_notification(_seed_message())
    r2 = fwd.forward_notification(_seed_message())
    r3 = fwd.forward_notification(_seed_message())
    assert r1["sent"] is True and r2["sent"] is True
    assert r3 == {"sent": False, "reason": "rate_limited"}
    assert len(sent) == 2


# ---------------------------------------------------------------------------
# Verification flow (service level)
# ---------------------------------------------------------------------------

def test_mint_and_confirm_sets_verified(env):
    mailbox.provision(OWNER, "Harshith Manne")
    mailbox.set_forwarding(OWNER, True, FWD_TO)
    token = mailbox.mint_forward_verification(OWNER, FWD_TO)
    result = mailbox.confirm_forward_verification(OWNER, token)
    assert result["forward_to"] == FWD_TO
    assert result["forward_to_verified"] is True


def test_confirm_is_one_use(env):
    mailbox.provision(OWNER, "Harshith Manne")
    mailbox.set_forwarding(OWNER, True, FWD_TO)
    token = mailbox.mint_forward_verification(OWNER, FWD_TO)
    mailbox.confirm_forward_verification(OWNER, token)
    with pytest.raises(ValueError):
        mailbox.confirm_forward_verification(OWNER, token)


def test_token_for_old_destination_rejected(env):
    mailbox.provision(OWNER, "Harshith Manne")
    mailbox.set_forwarding(OWNER, True, "old@dest.com")
    stale = mailbox.mint_forward_verification(OWNER, "old@dest.com")
    # User changes the destination — the stale token must not verify the new one.
    mailbox.set_forwarding(OWNER, True, FWD_TO)
    with pytest.raises(ValueError):
        mailbox.confirm_forward_verification(OWNER, stale)
    mb = mailbox.get_mailbox(OWNER)
    assert mb["forward_to"] == FWD_TO
    assert mb["forward_to_verified"] is False


def test_changing_destination_resets_verified(env):
    mailbox.provision(OWNER, "Harshith Manne")
    mailbox.set_forwarding(OWNER, True, FWD_TO)
    token = mailbox.mint_forward_verification(OWNER, FWD_TO)
    mailbox.confirm_forward_verification(OWNER, token)
    assert mailbox.get_mailbox(OWNER)["forward_to_verified"] is True
    # Any change re-arms verification.
    m = mailbox.set_forwarding(OWNER, True, "new@dest.com")
    assert m["forward_to_verified"] is False


def test_idor_user_a_token_cannot_confirm_user_b(env):
    mailbox.provision(OWNER, "Harshith Manne")
    mailbox.set_forwarding(OWNER, True, FWD_TO)
    mailbox.provision(OTHER, "Someone Else")
    mailbox.set_forwarding(OTHER, True, FWD_TO)
    token_a = mailbox.mint_forward_verification(OWNER, FWD_TO)
    # OTHER tries to consume A's token -> not found (owner-scoped).
    with pytest.raises(LookupError):
        mailbox.confirm_forward_verification(OTHER, token_a)
    assert mailbox.get_mailbox(OTHER)["forward_to_verified"] is False


# ---------------------------------------------------------------------------
# Endpoints (JWT-required, owner-scoped, business failures never 401)
# ---------------------------------------------------------------------------

@pytest.fixture()
def app(env, sent):
    return load_service_app("resume")


def _hdr(app, email):
    from flask_jwt_extended import create_access_token
    with app.app_context():
        token = create_access_token(identity=email)
    return {"Authorization": f"Bearer {token}"}


def test_endpoint_set_forwarding_sends_verification(app, sent):
    client = app.test_client()
    mailbox.provision(OWNER, "Harshith Manne")
    resp = client.post("/api/resume/mailbox/forwarding",
                       json={"enabled": True, "forward_to": FWD_TO}, headers=_hdr(app, OWNER))
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["forward_to"] == FWD_TO
    assert body["forward_to_verified"] is False
    # A verification email went out from MAIL_FORWARD_FROM to the destination.
    assert len(sent) == 1
    assert sent[0]["to"] == FWD_TO
    assert sent[0]["from_addr"] == FORWARD_FROM


def test_endpoint_verify_confirms_destination(app, sent):
    client = app.test_client()
    mailbox.provision(OWNER, "Harshith Manne")
    client.post("/api/resume/mailbox/forwarding",
                json={"enabled": True, "forward_to": FWD_TO}, headers=_hdr(app, OWNER))
    token = mailbox.mint_forward_verification(OWNER, FWD_TO)
    resp = client.post("/api/resume/mailbox/forwarding/verify",
                       json={"token": token}, headers=_hdr(app, OWNER))
    assert resp.status_code == 200
    assert resp.get_json()["forward_to_verified"] is True


def test_endpoint_verify_idor_is_404_not_401(app):
    client = app.test_client()
    mailbox.provision(OWNER, "Harshith Manne")
    mailbox.set_forwarding(OWNER, True, FWD_TO)
    mailbox.provision(OTHER, "Someone Else")
    mailbox.set_forwarding(OTHER, True, FWD_TO)
    token_a = mailbox.mint_forward_verification(OWNER, FWD_TO)
    # OTHER submits A's token — 404 (never 401, never verifies).
    resp = client.post("/api/resume/mailbox/forwarding/verify",
                       json={"token": token_a}, headers=_hdr(app, OTHER))
    assert resp.status_code == 404
    assert resp.status_code != 401
    assert mailbox.get_mailbox(OTHER)["forward_to_verified"] is False


def test_endpoint_verify_bad_token_is_400_not_401(app):
    client = app.test_client()
    mailbox.provision(OWNER, "Harshith Manne")
    resp = client.post("/api/resume/mailbox/forwarding/verify",
                       json={}, headers=_hdr(app, OWNER))
    assert resp.status_code == 400
    assert resp.status_code != 401


def test_endpoint_forwarding_no_mailbox_is_404_not_401(app):
    client = app.test_client()
    resp = client.post("/api/resume/mailbox/forwarding",
                       json={"enabled": True}, headers=_hdr(app, "nomailbox@x.com"))
    assert resp.status_code == 404
    assert resp.status_code != 401


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
