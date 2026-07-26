"""Managed Application Mailbox — PLAIN FULL-CONTENT relay (Hide-My-Email style).

The redesigned forwarder: instead of a sanitized SUMMARY + deep link, we relay
the REAL email to the user's verified inbox — original subject, sanitized body,
From = MAIL_FORWARD_FROM with a "(via My Private Mail)" display name (never
spoofing), Reply-To = original sender, X-JP-Forwarded loop marker. A message
FILTER (``should_forward``) skips system noise (OTP / verification / magic-login
/ password-reset). Trust is decoupled from forwarding (unverified still relays
full content); the loop / suppression / rate / kill-switch guards all remain.

Fully offline: Mongo is mongomock, S3 is a FakeStorage, and
``email_service.send_email`` is captured (no ZeptoMail/SES/network).
"""
from __future__ import annotations

import secrets
from pathlib import Path

import pytest

import services.managed_mailbox_service as mailbox
import services.mail_store_service as store
import services.mail_forward_service as fwd

DOMAIN = "pilotmail.co"
OWNER = "owner@x.com"
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
    fake = FakeStorage()
    import services.s3_service as s3
    monkeypatch.setattr(s3, "get_storage_service", lambda: fake)
    return fake


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


def _seed_message(env, *, from_addr="no-reply@greenhouse.io", from_name="Greenhouse",
                  from_domain="greenhouse.io", subject="Update on your application",
                  text="Thank you for applying. After careful review we will not be moving forward.",
                  safe_html="<p>Thank you for applying to <b>Acme</b>.</p>",
                  trust="verified", x_jp_forwarded=False, clean_attachments=0):
    mid = "msg_" + secrets.token_hex(8)
    tid = "thr_" + secrets.token_hex(8)
    bucket = store._apply_mail_bucket()
    text_key = f"mail/text/seed/{mid}.txt"
    html_key = f"mail/html/seed/{mid}.html" if safe_html else None
    env.put_object_bytes(text_key, (text or "").encode("utf-8"), bucket=bucket)
    if html_key:
        env.put_object_bytes(html_key, safe_html.encode("utf-8"), bucket=bucket)
    _db().mail_messages.insert_one({
        "message_id": mid, "provider_message_id": "pm_" + secrets.token_hex(6),
        "thread_id": tid, "user_email": OWNER, "alias_id": "alias_x",
        "direction": "inbound", "from_addr": from_addr, "from_name": from_name,
        "from_domain": from_domain, "to_addr": ALIAS_ADDR, "subject": subject,
        "trust": trust, "text_s3_key": text_key, "safe_html_s3_key": html_key,
        "x_jp_forwarded": x_jp_forwarded,
    })
    _db().mail_threads.insert_one({
        "thread_id": tid, "user_email": OWNER, "subject": subject, "trust": trust,
    })
    for _ in range(clean_attachments):
        _db().mail_attachments.insert_one({
            "attachment_id": "att_" + secrets.token_hex(6), "message_id": mid,
            "user_email": OWNER, "scan_verdict": "clean", "s3_key": f"mail/att/{mid}",
        })
    return {"thread_id": tid, "message_id": mid, "deduplicated": False}


# ---------------------------------------------------------------------------
# should_forward — meaningful mail vs. system noise
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("subject,text", [
    ("Your verification code", "Your verification code is 483920. It expires in 10 minutes."),
    ("Verify your email address", "Please verify your email address to continue."),
    ("Confirm your email", "Confirm your email to activate notifications."),
    ("Your one-time passcode", "Your one-time passcode is 5566."),
    ("Reset your password", "Click here to reset your password."),
    ("Sign in to Acme", "Here is your login code: 2231"),
    ("Finish signing in", "Use this magic link to sign in."),
    ("Welcome", "Activate your account to get started."),
    ("Security alert", "Your security code is 9981 to authenticate."),
])
def test_should_forward_skips_system_noise(subject, text):
    ok, category = fwd.should_forward({"subject": subject, "text": text})
    assert ok is False
    assert category == "system_noise"


@pytest.mark.parametrize("subject,text", [
    ("Update on your application to Acme", "After careful review we will not be moving forward."),
    ("You have an interview", "Congratulations, you have an interview at Corp."),
    ("Application received", "Thank you for applying. We received your application."),
    ("Next steps", "We would like to schedule an assessment. Over 5000 candidates applied."),
    ("Your offer from Acme", "We are pleased to extend an offer. Start date 2026-08-01."),
])
def test_should_forward_relays_meaningful_mail(subject, text):
    ok, category = fwd.should_forward({"subject": subject, "text": text})
    assert ok is True
    assert category != "system_noise"


def test_forward_notification_skips_noise_message(env, sent):
    _enable_forwarding()
    res = _seed_message(
        env, subject="Your verification code",
        text="Your verification code is 224466. Do not share it.",
    )
    out = fwd.forward_notification(res)
    assert out == {"sent": False, "reason": "system_noise"}
    assert sent == []


# ---------------------------------------------------------------------------
# Full-content relay — the REAL email, not a summary/deep-link notice
# ---------------------------------------------------------------------------

def test_forward_relays_full_real_content(env, sent):
    _enable_forwarding()
    res = _seed_message(env)
    out = fwd.forward_notification(res)

    assert out == {"sent": True, "reason": "sent"}
    assert len(sent) == 1
    call = sent[0]

    assert call["to"] == FWD_TO
    # From = MAIL_FORWARD_FROM (JobPilot-owned) with a "(via My Private Mail)"
    # display name — NEVER the recruiter address (no spoof).
    assert FORWARD_FROM in call["from_addr"]
    assert "(via My Private Mail)" in call["from_addr"]
    assert "greenhouse.io" not in call["from_addr"]
    # Reply-To = a privacy-preserving per-thread reply-bound alias (NOT the raw
    # recruiter) so the user's reply routes through the relay, hiding their real
    # address. The recruiter is never exposed as the Reply-To.
    assert call["reply_to"].startswith("reply.")
    assert call["reply_to"].endswith(f"@{DOMAIN}")
    assert call["reply_to"] != "no-reply@greenhouse.io"
    # Outbound loop marker.
    assert call["headers"] == {"X-JP-Forwarded": "1"}
    # ORIGINAL subject preserved (NOT a generic subject-less notice).
    assert call["subject"] == "Update on your application"
    # The REAL sanitized body is delivered (full content), plus a compact
    # manage-in-app link (the body IS the message, not a redirect).
    assert "Thank you for applying" in call["html"]
    assert "moving forward" in call["text"].lower()
    manage = f"{FRONTEND}/next/applications/{res['thread_id']}"
    assert manage in call["html"] and manage in call["text"]


def test_forward_is_not_the_old_generic_notice(env, sent):
    _enable_forwarding()
    res = _seed_message(env)
    fwd.forward_notification(res)
    call = sent[0]
    # The retired generic/summary notice used these strings — assert they're gone.
    assert "New message about your application" not in call["subject"]
    assert "new message in your" not in call["subject"].lower()
    assert "we do not include the full message" not in call["html"].lower()
    assert "open it in the app to read and reply" not in call["text"].lower()


def test_full_pipeline_relays_real_greenhouse_body(env, sent):
    from datetime import timedelta
    _enable_forwarding()
    _db().tailoring_records.insert_one({
        "record_id": "rec-1", "user_email": OWNER,
        "application": {"status": "applied", "employer_domain": "greenhouse.io",
                        "applied_at": store._now() - timedelta(hours=1)},
        "jd_analysis": {"company": "Acme"},
    })
    info = mailbox.resolve_alias(ALIAS_ADDR)
    res = store.ingest_committed(
        info, ALIAS_ADDR, "no-reply@greenhouse.io",
        _eml("rejection_greenhouse.eml"), "<rej-0001@greenhouse.io>",
    )
    out = store.maybe_forward(res)

    assert out["sent"] is True
    call = sent[0]
    blob = (call["html"] + call["text"]).lower()
    # The REAL recruiter message is delivered end-to-end (full-content relay).
    assert "careful review" in blob
    assert "moving forward" in blob
    # ORIGINAL subject preserved.
    assert call["subject"] == "Update on your application to Acme"
    # Sender is ours; reply routes through a privacy-preserving reply-bound alias
    # (never the raw recruiter).
    assert FORWARD_FROM in call["from_addr"]
    assert call["reply_to"].startswith("reply.")
    assert call["reply_to"].endswith(f"@{DOMAIN}")
    assert call["reply_to"] != "no-reply@greenhouse.io"


# ---------------------------------------------------------------------------
# Trust decoupled — unverified still relays full content (no downgrade)
# ---------------------------------------------------------------------------

def test_unverified_still_relays_full_content(env, sent):
    _enable_forwarding()
    res = _seed_message(env, trust="unverified", subject="Interview invitation",
                        text="We would like to invite you to interview.")
    out = fwd.forward_notification(res)

    assert out == {"sent": True, "reason": "sent"}
    call = sent[0]
    # Same full content as a verified message — trust does NOT downgrade it.
    assert call["subject"] == "Interview invitation"
    assert "invite you to interview" in call["text"]
    assert FORWARD_FROM in call["from_addr"]


def test_missing_trust_still_relays_full_content(env, sent):
    _enable_forwarding()
    res = _seed_message(env, trust="", subject="Interview invitation",
                        text="We would like to invite you to interview.")
    out = fwd.forward_notification(res)
    assert out["sent"] is True
    assert sent[0]["subject"] == "Interview invitation"


def test_unverified_suppress_kill_switch_sends_nothing(env, sent, monkeypatch):
    monkeypatch.setenv("MAIL_FORWARD_UNVERIFIED_MODE", "suppress")
    _enable_forwarding()
    out = fwd.forward_notification(_seed_message(env, trust="unverified"))
    assert out == {"sent": False, "reason": "unverified_suppressed"}
    assert sent == []


def test_verified_still_relays_under_suppress_mode(env, sent, monkeypatch):
    monkeypatch.setenv("MAIL_FORWARD_UNVERIFIED_MODE", "suppress")
    _enable_forwarding()
    out = fwd.forward_notification(_seed_message(env, trust="verified"))
    assert out["sent"] is True


# ---------------------------------------------------------------------------
# Attachments — a note only (clean count), never raw bytes / suspicious refs
# ---------------------------------------------------------------------------

def test_clean_attachment_note_present(env, sent):
    _enable_forwarding()
    res = _seed_message(env, clean_attachments=2)
    fwd.forward_notification(res)
    call = sent[0]
    assert "2 attachment(s) available in your" in call["html"]
    assert "2 attachment(s) available in your" in call["text"]


# ---------------------------------------------------------------------------
# Guards still hold under the new relay path
# ---------------------------------------------------------------------------

def test_loop_guard_x_jp_forwarded_still_skips(env, sent):
    _enable_forwarding()
    out = fwd.forward_notification(_seed_message(env, x_jp_forwarded=True))
    assert out == {"sent": False, "reason": "loop_forwarded_header"}
    assert sent == []


def test_loop_guard_own_domain_still_skips(env, sent):
    _enable_forwarding()
    out = fwd.forward_notification(
        _seed_message(env, from_addr=f"x@{DOMAIN}", from_domain=DOMAIN)
    )
    assert out == {"sent": False, "reason": "loop_own_domain"}
    assert sent == []


def test_suppression_still_blocks(env, sent):
    _enable_forwarding()
    from services import email_suppression
    email_suppression.add_suppression(FWD_TO, "bounce")
    out = fwd.forward_notification(_seed_message(env))
    assert out == {"sent": False, "reason": "suppressed"}
    assert sent == []


def test_daily_cap_still_enforced(env, sent, monkeypatch):
    monkeypatch.setenv("MAIL_FORWARD_DAILY_CAP", "2")
    _enable_forwarding()
    r1 = fwd.forward_notification(_seed_message(env))
    r2 = fwd.forward_notification(_seed_message(env))
    r3 = fwd.forward_notification(_seed_message(env))
    assert r1["sent"] is True and r2["sent"] is True
    assert r3 == {"sent": False, "reason": "rate_limited"}
    assert len(sent) == 2


def test_body_fetch_failure_degrades_gracefully(env, sent):
    _enable_forwarding()
    # Message points at S3 keys that were never written -> fetch fails; the
    # forward must degrade to a short notice, not crash.
    mid = "msg_" + secrets.token_hex(8)
    tid = "thr_" + secrets.token_hex(8)
    _db().mail_messages.insert_one({
        "message_id": mid, "thread_id": tid, "user_email": OWNER, "alias_id": "a",
        "direction": "inbound", "from_addr": "no-reply@greenhouse.io",
        "from_name": "Greenhouse", "from_domain": "greenhouse.io", "to_addr": ALIAS_ADDR,
        "subject": "Update on your application", "trust": "verified",
        "text_s3_key": "mail/text/missing.txt", "safe_html_s3_key": "mail/html/missing.html",
        "x_jp_forwarded": False,
    })
    _db().mail_threads.insert_one({"thread_id": tid, "user_email": OWNER,
                                   "subject": "Update on your application", "trust": "verified"})
    out = fwd.forward_notification({"thread_id": tid, "message_id": mid, "deduplicated": False})
    assert out["sent"] is True
    assert sent[0]["subject"] == "Update on your application"


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
