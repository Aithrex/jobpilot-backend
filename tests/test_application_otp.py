"""Tests for managed-mailbox OTP capture (application_otp_service)."""
from datetime import datetime, timedelta, timezone

import pytest

from services import application_otp_service as otp


def test_extract_workday_style_code():
    payload = otp.extract_otp_payload(
        subject="Your Workday verification code",
        text="Your one-time passcode is 847291. It expires in 10 minutes.",
        from_addr="noreply@myworkday.com",
    )
    assert payload["code"] == "847291"


def test_extract_greenhouse_alphanumeric_security_code():
    text = (
        "Hi Harshith, Copy and paste this code into the security code field "
        "on your application: ORZTYWie After you enter the code, resubmit. "
        "© 2026 Greenhouse 18 West 18th Street, New York, NY 10011, USA"
    )
    html = "<h1>ORZTYWie</h1><p>© 2026 Greenhouse</p>"
    payload = otp.extract_otp_payload(
        subject="Security code for your application to Torq",
        text=text,
        html=html,
        from_addr="no-reply@us.greenhouse-mail.io",
    )
    assert payload["code"] == "ORZTYWie"
    assert payload["code"] != "2026"


def test_extract_verify_link_from_workday_html():
    html = (
        '<a href="https://tenant.wd5.myworkdayjobs.com/verify?token=abc">'
        "Verify your email</a>"
    )
    payload = otp.extract_otp_payload(
        subject="Verify your candidate account",
        text="",
        html=html,
        from_addr="noreply@wd5.myworkday.com",
    )
    assert payload["links"]
    assert "myworkdayjobs.com" in payload["links"][0]


def test_capture_stores_grant_without_active_freeze(monkeypatch):
  class FakeCol:
      def __init__(self):
          self.docs = {}

      def update_one(self, filt, upd, upsert=False):
          key = (filt["user_email"], filt["message_id"])
          self.docs[key] = {**filt, **upd.get("$set", {})}

  fake_col = FakeCol()
  monkeypatch.setattr(otp, "_coll", lambda: fake_col)
  # `_active_freeze` now also takes the sender + subject, so it can tell WHICH
  # application a code belongs to rather than binding by recency.
  monkeypatch.setattr(otp, "_active_freeze", lambda *a, **k: None)
  monkeypatch.setattr(otp, "_db", lambda: type("DB", (), {"auto_apply_freezes": None})())

  out = otp.capture_from_noise_message(
      user_email="user@example.com",
      message_id="msg_test",
      subject="Your verification code",
      text="Your code is 123456",
  )
  assert out["captured"] is True
  assert fake_col.docs[("user@example.com", "msg_test")]["code"] == "123456"


def test_claim_prefers_bound_then_unbound(monkeypatch):
  now = datetime.now(timezone.utc)

  class FakeCol:
      def find_one_and_update(self, q, upd, sort=None):
          if q.get("status") != "available":
              return None
          return {
              "code": "999888",
              "links": ["https://jobs.lever.co/acme/verify"],
              "message_id": "msg_1",
              "from_addr": "noreply@lever.co",
          }

  monkeypatch.setattr(otp, "_coll", lambda: FakeCol())
  grant = otp.claim_for_verify("user@example.com", "app_123")
  assert grant["code"] == "999888"
  assert grant["links"]
