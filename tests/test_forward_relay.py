"""Managed Application Mailbox — LINK-ONLY notification + ALLOW-LIST filter.

AUTOAPPLY_REPAIR_SPEC §6 inverted this path. The in-app inbox keeps EVERYTHING;
the user's personal inbox now gets only an ALLOW-LIST of application-status
events (``mail_ingress_service.classify``'s closed STATUS_ENUM), delivered as a
LINK-ONLY notification — the recruiter body stays in-app.

Still asserted here: From = MAIL_FORWARD_FROM with a "(via My Private Mail)"
display name (never spoofing), Reply-To = a per-thread reply-bound alias,
X-JP-Forwarded loop marker, the deterministic system-noise deny, trust decoupled
from forwarding, and the loop / suppression / rate / kill-switch guards.

Fully offline: Mongo is mongomock, S3 is a FakeStorage, the CLASSIFIER is a
deterministic stub (no Bedrock), and ``email_service.send_email`` is captured
(no ZeptoMail/SES/network).
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


# Deterministic stand-in for the Haiku classifier (no Bedrock in tests). Maps
# the seeded bodies onto the closed STATUS_ENUM; first match wins, so the mixed
# "thank you for applying … not moving forward" rejection lands on `rejected`.
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
    """Patch ``mail_ingress_service.classify`` (the allow-list's status oracle).
    ``mail_forward_service`` imports it INSIDE the call, so patching the module
    attribute is enough."""
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
    ("Next steps", "We would like to schedule an assessment. Over 5000 candidates applied."),
    ("Your offer from Acme", "We are pleased to extend an offer. Start date 2026-08-01."),
])
def test_should_forward_relays_meaningful_mail(subject, text):
    # A real inbound message always carries a from_domain (see _seed_message /
    # mail_store_service.ingest_committed) — the allow-list needs it to classify.
    ok, category = fwd.should_forward({"subject": subject, "text": text,
                                       "from_domain": "greenhouse.io"})
    assert ok is True
    assert category != "system_noise"


def test_an_ats_confirmation_is_not_allowlisted_by_default():
    """`applied` is deliberately excluded (apply_daily_summary owns "we applied
    for you"). This used to read as RELAYABLE only because a context-less message
    took the fail-open oracle branch before the classifier ever ran."""
    assert fwd.should_forward({
        "subject": "Application received",
        "text": "Thank you for applying. We received your application.",
        "from_domain": "greenhouse.io"}) == (False, "not_allowlisted")


@pytest.mark.parametrize("from_domain", ["", None])
def test_a_message_with_no_context_is_never_relayed(from_domain, monkeypatch):
    """AN ALLOW-LIST MUST NOT HAVE A `return True` DEFAULT.

    The noise-oracle carve-out used to be INFERRED from the absence of
    from_domain, so a message with an empty one — a null return-path `<>`, a
    bounce, a malformed From — took that branch and was RELAYED to the user's real
    personal inbox with no classification at all. The oracle contract is now
    explicit (`classify=False`) and the forwarding path fails CLOSED.
    """
    import services.mail_ingress_service as ingress
    monkeypatch.setattr(ingress, "classify",
                        lambda *a, **k: pytest.fail("must not classify with no context"))

    assert fwd.should_forward({"subject": "Update on your application",
                               "text": "We would like to schedule an interview.",
                               "from_domain": from_domain}) == (False, "no_context")


def test_an_ungrounded_classification_never_earns_a_relay(env, monkeypatch):
    """`mail_ingress_service` caps a classification whose evidence quote is
    missing or not in the body at _UNGROUNDED_CONFIDENCE_CAP (0.60) exactly so a
    hallucinated verdict cannot drive an automatic action. Forwarding IS one, so
    the floor must sit above that cap — the gate ignored `confidence` entirely."""
    import services.mail_ingress_service as ingress

    def _ungrounded(from_domain, subject, body_text):
        return {"status": "interview_invite",
                "confidence": ingress._UNGROUNDED_CONFIDENCE_CAP,
                "company": "", "role": "", "event_at": None,
                "evidence_sentence": ""}

    monkeypatch.setattr(ingress, "classify", _ungrounded)
    msg = {"subject": "Next steps", "text": "Let's talk.",
           "from_domain": "greenhouse.io"}
    assert fwd.should_forward(msg) == (False, "not_allowlisted")

    # A grounded, confident verdict on the same message still relays.
    monkeypatch.setattr(ingress, "classify",
                        lambda *a, **k: {"status": "interview_invite",
                                         "confidence": 0.95, "company": "",
                                         "role": "", "event_at": None,
                                         "evidence_sentence": "Let's talk."})
    assert fwd.should_forward(msg) == (True, "interview_invite")


def test_the_confidence_floor_can_never_be_configured_below_the_ungrounded_cap(
        monkeypatch):
    import services.mail_ingress_service as ingress
    monkeypatch.setenv("MAIL_FORWARD_CONFIDENCE_FLOOR", "0.0")
    assert fwd._confidence_floor() > ingress._UNGROUNDED_CONFIDENCE_CAP


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
# Link-only notification — a POINTER into the app, never the recruiter body
# ---------------------------------------------------------------------------

def test_forward_sends_link_only_notification(env, sent):
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
    # ORIGINAL subject preserved on the header (so the ping is scannable).
    assert call["subject"] == "Update on your application"
    # A status headline + the deep link, in BOTH parts.
    manage = f"{FRONTEND}/app/apply/{res['thread_id']}"
    assert manage in call["html"] and manage in call["text"]
    assert "passed on your application" in call["html"]


def test_forward_body_is_never_relayed(env, sent):
    """The whole point of link-only: recruiter CONTENT stays in-app."""
    _enable_forwarding()
    fwd.forward_notification(_seed_message(
        env,
        text="Thank you for applying. After careful review we will not be moving forward. "
             "SECRET-BODY-MARKER",
        safe_html="<p>SECRET-HTML-MARKER</p>",
    ))
    blob = (sent[0]["html"] + sent[0]["text"])
    assert "SECRET-BODY-MARKER" not in blob
    assert "SECRET-HTML-MARKER" not in blob


def test_full_pipeline_sends_link_only_for_greenhouse_rejection(env, sent):
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
    # The in-app inbox still stores the FULL message (no content drop at ingest).
    stored = _db().mail_messages.find_one({"message_id": res["message_id"]})
    assert stored["text_s3_key"]
    # ...but the personal inbox only gets the pointer.
    assert "careful review" not in blob
    assert f"{FRONTEND}/app/apply/{res['thread_id']}" in call["html"]
    # ORIGINAL subject preserved.
    assert call["subject"] == "Update on your application to Acme"
    # Sender is ours; reply routes through a privacy-preserving reply-bound alias
    # (never the raw recruiter).
    assert FORWARD_FROM in call["from_addr"]
    assert call["reply_to"].startswith("reply.")
    assert call["reply_to"].endswith(f"@{DOMAIN}")
    assert call["reply_to"] != "no-reply@greenhouse.io"


# ---------------------------------------------------------------------------
# Trust decoupled — unverified still notifies (no downgrade)
# ---------------------------------------------------------------------------

def test_unverified_still_notifies(env, sent):
    _enable_forwarding()
    res = _seed_message(env, trust="unverified", subject="Interview invitation",
                        text="We would like to invite you to interview.")
    out = fwd.forward_notification(res)

    assert out == {"sent": True, "reason": "sent"}
    call = sent[0]
    # Same notification as a verified message — trust does NOT downgrade it.
    assert call["subject"] == "Interview invitation"
    assert "wants to interview you" in call["html"]
    assert FORWARD_FROM in call["from_addr"]


def test_missing_trust_still_notifies(env, sent):
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
    assert "2 attachment(s) included" in call["html"]
    assert "2 attachment(s) included" in call["text"]


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
    # Message points at S3 keys that were never written -> fetch fails. The
    # notification is link-only anyway, so a body it never sends must not crash
    # it; the STORED classification supplies the allow-list status without an
    # LLM call.
    mid = "msg_" + secrets.token_hex(8)
    tid = "thr_" + secrets.token_hex(8)
    _db().mail_messages.insert_one({
        "message_id": mid, "thread_id": tid, "user_email": OWNER, "alias_id": "a",
        "direction": "inbound", "from_addr": "no-reply@greenhouse.io",
        "from_name": "Greenhouse", "from_domain": "greenhouse.io", "to_addr": ALIAS_ADDR,
        "subject": "Update on your application", "trust": "verified",
        "classification": {"status": "rejected"},
        "text_s3_key": "mail/text/missing.txt", "safe_html_s3_key": "mail/html/missing.html",
        "x_jp_forwarded": False,
    })
    _db().mail_threads.insert_one({"thread_id": tid, "user_email": OWNER,
                                   "subject": "Update on your application", "trust": "verified"})
    out = fwd.forward_notification({"thread_id": tid, "message_id": mid, "deduplicated": False})
    assert out["sent"] is True
    assert sent[0]["subject"] == "Update on your application"


# ---------------------------------------------------------------------------
# The ALLOW-LIST itself (spec §6) — silence is the default
# ---------------------------------------------------------------------------

def test_unclassifiable_mail_is_not_forwarded_but_stays_in_app(env, sent):
    """An allow-list FAILS CLOSED: no status -> no personal-inbox ping. The
    message is untouched in the in-app inbox."""
    _enable_forwarding()
    res = _seed_message(env, subject="Our weekly engineering newsletter",
                        text="Here are this week's articles from our blog.",
                        safe_html="<p>Newsletter</p>")
    out = fwd.forward_notification(res)
    assert out == {"sent": False, "reason": "not_allowlisted"}
    assert sent == []
    # In-app inbox keeps EVERYTHING — the message is still stored.
    assert _db().mail_messages.find_one({"message_id": res["message_id"]}) is not None


def test_classifier_outage_fails_closed(env, sent, monkeypatch):
    import services.mail_ingress_service as ingress

    def _boom(*a, **k):
        raise RuntimeError("bedrock down")

    monkeypatch.setattr(ingress, "classify", _boom)
    _enable_forwarding()
    out = fwd.forward_notification(_seed_message(env))
    assert out == {"sent": False, "reason": "not_allowlisted"}
    assert sent == []


def test_application_confirmation_defers_to_the_daily_digest(env, sent):
    """`applied` is NOT allow-listed by default: apply_scheduler's end-of-day
    apply_daily_summary already reports every application submitted that day, so
    per-event forwarding of the ATS acknowledgement would double-notify."""
    _enable_forwarding()
    out = fwd.forward_notification(_seed_message(
        env, subject="Application received",
        text="We received your application and will be in touch.",
        safe_html="<p>Received</p>",
    ))
    assert out == {"sent": False, "reason": "not_allowlisted"}
    assert sent == []


def test_include_applied_flag_re_arms_confirmations(env, sent, monkeypatch):
    monkeypatch.setenv("MAIL_FORWARD_INCLUDE_APPLIED", "true")
    _enable_forwarding()
    out = fwd.forward_notification(_seed_message(
        env, subject="Application received",
        text="We received your application and will be in touch.",
        safe_html="<p>Received</p>",
    ))
    assert out == {"sent": True, "reason": "sent"}
    assert "received your application" in sent[0]["html"]


@pytest.mark.parametrize("subject,text,headline", [
    ("Interview invitation", "We would like to invite you to interview.",
     "wants to interview you"),
    ("Next steps", "Please complete the online assessment.", "sent you an assessment"),
    ("Your offer", "We are pleased to extend an offer.", "sent you an offer"),
    ("Update on your application", "After careful review we will not be moving forward.",
     "passed on your application"),
])
def test_allowlisted_statuses_each_notify(env, sent, subject, text, headline):
    _enable_forwarding()
    out = fwd.forward_notification(
        _seed_message(env, subject=subject, text=text, safe_html=f"<p>{text}</p>")
    )
    assert out == {"sent": True, "reason": "sent"}
    assert headline in sent[0]["html"]


def test_allowlist_config_override_is_closed_to_status_enum(env, sent, monkeypatch):
    # Only `offer` forwards; the bogus name is dropped (STATUS_ENUM is authority).
    monkeypatch.setenv("MAIL_FORWARD_ALLOWLIST", "offer, not_a_status")
    _enable_forwarding()
    assert fwd.forward_notification(_seed_message(
        env, subject="Update", text="After careful review we will not be moving forward.",
    )) == {"sent": False, "reason": "not_allowlisted"}
    assert fwd.forward_notification(_seed_message(
        env, subject="Your offer", text="We are pleased to extend an offer.",
    )) == {"sent": True, "reason": "sent"}


def test_noise_oracle_callers_get_the_deterministic_verdict_only(env, monkeypatch):
    """`mail_store_service._maybe_capture_application_otp` calls should_forward as
    a NOISE oracle, now via the EXPLICIT `classify=False` contract (it used to be
    inferred from the absence of from_domain, which made the same fail-OPEN
    carve-out reachable from message data on the forwarding path). That path must
    never gain an LLM call nor change its capture trigger."""
    import services.mail_ingress_service as ingress

    def _boom(*a, **k):  # any classifier call here is a bug
        raise AssertionError("classifier must not run without context")

    monkeypatch.setattr(ingress, "classify", _boom)
    assert fwd.should_forward({"subject": "Update on your application",
                               "text": "We will not be moving forward."},
                              classify=False) == (True, "application")
    assert fwd.should_forward({"subject": "Your verification code",
                               "text": "Your code is 483920."},
                              classify=False) == (False, "system_noise")


def test_the_otp_capture_path_uses_the_explicit_oracle_contract(env, monkeypatch):
    """The webhook commit path itself (not just should_forward) must stay
    LLM-free and keep the same capture trigger."""
    import services.mail_ingress_service as ingress
    import services.application_otp_service as otp

    monkeypatch.setattr(ingress, "classify",
                        lambda *a, **k: pytest.fail("no LLM on the commit path"))
    captured = []
    monkeypatch.setattr(otp, "capture_from_noise_message",
                        lambda **kw: captured.append(kw.get("message_id")))

    # Noise -> captured for the executor, never surfaced to the user.
    store._maybe_capture_application_otp(
        user_email=OWNER, message_id="m1", subject="Your verification code",
        text="Your code is 483920.", html="", from_addr="no-reply@wd.com",
        application_id=None)
    # A real employer reply -> not an OTP, nothing captured.
    store._maybe_capture_application_otp(
        user_email=OWNER, message_id="m2", subject="Update on your application",
        text="We will not be moving forward.", html="",
        from_addr="no-reply@greenhouse.io", application_id=None)

    assert captured == ["m1"]


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
