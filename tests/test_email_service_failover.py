"""email_service provider failover + diagnostics.

Motivated by a real incident: the ZeptoMail API token in SSM went invalid
(TM_4001 "Invalid API Token"), and because `send_email` swallows provider
errors so mail can never break signup, EVERY password-reset email stopped
arriving with nothing visibly failing. These tests pin the two behaviours that
make that recoverable: a failed primary falls over to the other transport, and
`diagnose()` reports why a send would be a no-op.
"""
import pytest

from services import email_service as es


@pytest.fixture()
def cfg(monkeypatch):
    """Drive email_service purely from an in-test config map."""
    values = {
        "EMAIL_PROVIDER": "zeptomail",
        "EMAIL_FROM_ADDRESS": "Aspirely <no-reply@aspirely.me>",
        "ZEPTOMAIL_TOKEN": "Zoho-enczapikey test-token",
        "EMAIL_REPLY_TO": "",
        "EMAIL_FALLBACK_PROVIDER": "",
    }
    monkeypatch.setattr(es, "_get_config_value", lambda k, d=None: values.get(k, d))
    # Suppression checks hit Mongo; keep this unit test off the DB.
    monkeypatch.setattr(es, "is_suppressed", lambda _e: False)
    return values


def _spy(monkeypatch, *, zepto, ses):
    """Patch both transports. Each arg is True (succeed), or an Exception."""
    calls = []

    def make(name, behaviour):
        def fn(*a, **k):
            calls.append(name)
            if isinstance(behaviour, Exception):
                raise behaviour
            return True
        return fn

    monkeypatch.setattr(es, "_send_via_zeptomail", make("zeptomail", zepto))
    monkeypatch.setattr(es, "_send_via_ses", make("ses", ses))
    return calls


# ---------------------------------------------------------------------------
# Failover
# ---------------------------------------------------------------------------

def test_healthy_primary_never_touches_the_fallback(cfg, monkeypatch):
    calls = _spy(monkeypatch, zepto=True, ses=True)
    assert es.send_email("u@example.com", "s", "<p>b</p>") is True
    assert calls == ["zeptomail"]


def test_dead_primary_token_falls_over_to_ses(cfg, monkeypatch):
    """The exact incident: ZeptoMail rejects the token, SES delivers."""
    calls = _spy(
        monkeypatch,
        zepto=RuntimeError("ZeptoMail HTTP 401: TM_4001 Invalid API Token found"),
        ses=True,
    )
    assert es.send_email("u@example.com", "s", "<p>b</p>") is True
    assert calls == ["zeptomail", "ses"]


def test_both_providers_down_reports_failure(cfg, monkeypatch):
    calls = _spy(
        monkeypatch,
        zepto=RuntimeError("ZeptoMail HTTP 401"),
        ses=RuntimeError("SES throttled"),
    )
    assert es.send_email("u@example.com", "s", "<p>b</p>") is False
    assert calls == ["zeptomail", "ses"]


def test_failover_can_be_disabled(cfg, monkeypatch):
    cfg["EMAIL_FALLBACK_PROVIDER"] = "none"
    calls = _spy(monkeypatch, zepto=RuntimeError("boom"), ses=True)
    assert es.send_email("u@example.com", "s", "<p>b</p>") is False
    assert calls == ["zeptomail"]


def test_missing_zepto_token_still_delivers_via_the_fallback(cfg, monkeypatch):
    """A missing token must not short-circuit to the dev console while SES is
    perfectly able to send."""
    cfg["ZEPTOMAIL_TOKEN"] = ""
    calls = _spy(monkeypatch, zepto=RuntimeError("no token"), ses=True)
    assert es.send_email("u@example.com", "s", "<p>b</p>") is True
    assert calls == ["zeptomail", "ses"]


def test_no_from_address_is_a_hard_stop(cfg, monkeypatch):
    cfg["EMAIL_FROM_ADDRESS"] = ""
    calls = _spy(monkeypatch, zepto=True, ses=True)
    assert es.send_email("u@example.com", "s", "<p>b</p>") is False
    assert calls == []


def test_suppressed_recipient_is_skipped_entirely(cfg, monkeypatch):
    # send_email re-imports the suppression module on each call, so patch the
    # module itself rather than email_service's re-exported helper.
    from services import email_suppression
    monkeypatch.setattr(email_suppression, "is_suppressed", lambda _e: True)
    calls = _spy(monkeypatch, zepto=True, ses=True)
    assert es.send_email("u@example.com", "s", "<p>b</p>") is False
    assert calls == []


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------

def test_diagnose_reports_a_healthy_config(cfg):
    d = es.diagnose("u@example.com")
    assert d["provider"] == "zeptomail"
    assert d["fallback_provider"] == "ses"
    assert d["fallback_usable"] is True
    assert d["token_present"] is True
    assert d["blocked_by"] is None
    # Never leak the credential itself.
    assert "test-token" not in repr(d)


def test_diagnose_flags_a_missing_from_address(cfg):
    cfg["EMAIL_FROM_ADDRESS"] = ""
    assert es.diagnose("u@example.com")["blocked_by"] == "no_from_address"


def test_diagnose_flags_a_suppressed_recipient(cfg, monkeypatch):
    monkeypatch.setattr(es, "is_suppressed", lambda _e: True)
    assert es.diagnose("u@example.com")["blocked_by"] == "recipient_suppressed"


def test_diagnose_does_not_call_a_missing_token_blocked_when_ses_can_cover(cfg):
    cfg["ZEPTOMAIL_TOKEN"] = ""
    d = es.diagnose("u@example.com")
    assert d["token_present"] is False
    assert d["fallback_usable"] is True
    assert d["blocked_by"] is None


def test_send_test_surfaces_the_provider_error(cfg, monkeypatch):
    """Unlike send_email, the admin probe must REPORT the provider's message."""
    _spy(monkeypatch, zepto=RuntimeError("ZeptoMail HTTP 401: TM_4001 Invalid API Token found"),
         ses=True)
    res = es.send_test("u@example.com")
    assert res["sent"] is False
    assert "Invalid API Token" in res["error"]
