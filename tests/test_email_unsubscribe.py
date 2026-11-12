"""CAN-SPAM + RFC 8058: tokenized one-click unsubscribe, list-mail headers and
footer, the list layer on the notification store, and suppression that
survives account erasure.

Offline: mongomock, and both mail transports are replaced by a capture.
"""
import logging
import re
import sys

import pytest
from bson import ObjectId

from _vg_support import current_db, load_service_app

USER = "list.user@example.com"
OTHER = "other.user@example.com"
PASSWORD = "Str0ng!Pass1"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("AWS_LAMBDA_FUNCTION_NAME", raising=False)
    monkeypatch.setenv("FRONTEND_URL", "https://aspirely.me")
    monkeypatch.setenv("COMPANY_LEGAL_NAME", "Aithrex Technologies Inc.")
    monkeypatch.setenv("COMPANY_POSTAL_ADDRESS", "100 Example Street, Suite 1, Austin, TX 78701")
    monkeypatch.setenv("EMAIL_UNSUBSCRIBE_MAILTO", "unsubscribe@aspirely.me")


@pytest.fixture()
def sent(monkeypatch):
    """Drive email_service from an in-test config and capture every send."""
    from services import email_service as es
    values = {"EMAIL_PROVIDER": "zeptomail", "EMAIL_FROM_ADDRESS": "Aspirely <no-reply@aspirely.me>",
              "ZEPTOMAIL_TOKEN": "test-token", "EMAIL_REPLY_TO": "", "EMAIL_FALLBACK_PROVIDER": "none"}
    monkeypatch.setattr(es, "_get_config_value", lambda k, d=None: values.get(k, d))
    calls = []

    def _capture(to, subject, html, text, from_addr, *, reply_to=None, headers=None):
        calls.append({"to": to, "subject": subject, "html": html, "text": text,
                      "headers": dict(headers or {})})
        return True

    monkeypatch.setattr(es, "_send_via_zeptomail", _capture)
    monkeypatch.setattr(es, "_send_via_ses", _capture)
    return calls


@pytest.fixture()
def db():
    database = current_db()
    from services import account_service
    for c in {"users", "notification_prefs", "email_suppressions", "rate_limit_buckets",
              "mail_aliases", "tech_feed_alert_failures", "user_preferences",
              *[c for c, _ in account_service.CASCADE_COLLECTIONS]}:
        database[c].delete_many({})
    # tests/_vg_support._reset_rate_limits is NOT active (a leading
    # underscore is skipped by `from _vg_support import *`), so clear the
    # per-process limiter here as test_auth_set_password does.
    from utils.security import get_rate_limiter
    get_rate_limiter()._requests.clear()
    yield database
    get_rate_limiter()._requests.clear()


def _user(db, email=USER):
    doc = {"_id": ObjectId(), "email": email, "status": "active"}
    db.users.insert_one(doc)
    return doc


@pytest.fixture()
def app(db):
    return load_service_app("auth")


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------
def test_token_round_trips_and_rejects_tampering(db):
    from services import privacy_service as ps
    u = _user(db)
    _user(db, OTHER)
    tok = ps.make_unsubscribe_token(USER, "job_alerts")
    assert tok and USER not in tok and "@" not in tok          # no address in URLs
    assert ps.verify_unsubscribe_token(tok) == (USER, "job_alerts")
    assert ps.make_unsubscribe_token(USER, "job_alerts") == tok  # deterministic, no expiry

    uid, lst, mac = tok.split(".")
    assert ps.verify_unsubscribe_token(f"{uid}.digests.{mac}") is None      # list swapped
    other_uid = str(db.users.find_one({"email": OTHER})["_id"])
    assert ps.verify_unsubscribe_token(f"{other_uid}.{lst}.{mac}") is None  # account swapped
    flipped = mac[:-1] + ("A" if mac[-1] != "A" else "B")
    assert ps.verify_unsubscribe_token(f"{uid}.{lst}.{flipped}") is None
    for junk in ("", "x", "a.b.c", None, {"$ne": 1}, "." * 300, f"{uid}.{lst}"):
        assert ps.verify_unsubscribe_token(junk) is None
    assert ps.make_unsubscribe_token("nobody@example.com", "job_alerts") is None
    assert ps.make_unsubscribe_token(USER, "not_a_list") is None
    assert str(u["_id"]) == uid


# ---------------------------------------------------------------------------
# The public endpoint
# ---------------------------------------------------------------------------
def test_one_click_form_post_unsubscribes_without_login(app, db):
    from services import privacy_service as ps
    from services.notifications import service as notify
    _user(db)
    tok = ps.make_unsubscribe_token(USER, "job_alerts")
    client = app.test_client()

    # RFC 8058 section 3.2: POST, form-encoded body, token in the URI.
    resp = client.post(f"/api/auth/email/unsubscribe?t={tok}",
                       data="List-Unsubscribe=One-Click",
                       content_type="application/x-www-form-urlencoded")
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert resp.get_json() == {"unsubscribed": True, "list": "job_alerts"}
    state = notify.email_list_state(USER)
    assert state["job_alerts"] is False and state["digests"] is True
    prefs = notify.get_prefs(USER)
    assert prefs["events"]["match_alert"] is False and prefs["events"]["application_nudge"] is False

    # idempotent
    again = client.post(f"/api/auth/email/unsubscribe?t={tok}", data="List-Unsubscribe=One-Click",
                        content_type="application/x-www-form-urlencoded")
    assert again.status_code == 200 and again.get_json()["unsubscribed"] is True


def test_json_token_post_and_get_confirmation(app, db):
    from services import privacy_service as ps
    _user(db)
    tok = ps.make_unsubscribe_token(USER, "digests")
    client = app.test_client()
    info = client.get(f"/api/auth/email/unsubscribe?t={tok}")
    assert info.status_code == 200
    assert info.get_json() == {"list": "digests", "email_masked": "l***@example.com"}
    from services.notifications import service as notify
    assert notify.email_list_state(USER)["digests"] is True     # GET changes nothing

    resp = client.post("/api/auth/email/unsubscribe", json={"token": tok})
    assert resp.status_code == 200
    assert resp.get_json() == {"unsubscribed": True, "list": "digests"}
    assert notify.email_list_state(USER)["digests"] is False


@pytest.mark.parametrize("method", ["get", "post"])
def test_invalid_token_is_400_not_401(app, db, method):
    _user(db)
    client = app.test_client()
    resp = getattr(client, method)("/api/auth/email/unsubscribe?t=" + "0" * 24 + ".job_alerts." + "A" * 43)
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "invalid_token"
    assert getattr(client, method)("/api/auth/email/unsubscribe").status_code == 400


def test_unsubscribe_is_rate_limited_by_ip(app, db, monkeypatch):
    mod = sys.modules["blueprints.auth"]
    monkeypatch.setattr(mod, "UNSUBSCRIBE_IP_LIMIT", 3)
    client = app.test_client()
    codes = [client.get("/api/auth/email/unsubscribe?t=bad").status_code for _ in range(4)]
    assert codes[:3] == [400, 400, 400]
    assert codes[3] == 429
    resp = client.get("/api/auth/email/unsubscribe?t=bad")
    assert resp.get_json()["error"] == "rate_limited"
    assert resp.get_json()["retry_after"] > 0


# ---------------------------------------------------------------------------
# Headers + footer: present on list mail, absent from transactional mail
# ---------------------------------------------------------------------------
def test_list_mail_carries_rfc8058_headers_and_canspam_footer(db, sent):
    from services import email_service as es
    from services import privacy_service as ps
    _user(db)
    assert es.send_email(USER, "3 new roles", "<p>roles</p>", "roles", list_id="job_alerts") is True
    msg = sent[-1]
    tok = ps.make_unsubscribe_token(USER, "job_alerts")
    lu = msg["headers"]["List-Unsubscribe"]
    assert lu.startswith(f"<https://aspirely.me/api/auth/email/unsubscribe?t={tok}>")
    assert "<mailto:unsubscribe@aspirely.me?subject=unsubscribe%20job_alerts%20" in lu
    assert msg["headers"]["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    for body in (msg["html"], msg["text"]):
        assert f"https://aspirely.me/unsubscribe?t={tok}" in body
        assert "Aithrex Technologies Inc." in body
        assert "100 Example Street, Suite 1, Austin, TX 78701" in body
        assert "Settings" not in body                     # opt-out never needs a login
        prose = re.sub(r"https?://[^\s\"<>]+", "", body)  # tokens may hold "-"
        for bad in ("\u2014", "\u2013", " - ", "--"):
            assert bad not in prose, bad
    # ZeptoMail receives them as mime_headers
    payload = es._zepto_payload(USER, "s", "h", "t", "Aspirely <no-reply@aspirely.me>",
                                headers=msg["headers"])
    assert payload["mime_headers"]["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"


def test_transactional_mail_has_no_unsubscribe(db, sent):
    from services import email_service as es
    _user(db)
    assert es.send_password_reset(USER, "https://aspirely.me/reset-password?token=abc") is True
    msg = sent[-1]
    assert "List-Unsubscribe" not in msg["headers"]
    assert "List-Unsubscribe-Post" not in msg["headers"]
    assert "/unsubscribe?t=" not in msg["html"] and "/unsubscribe?t=" not in msg["text"]


def test_welcome_is_list_mail(db, sent):
    from services import email_service as es
    _user(db)
    assert es.send_welcome(USER, "https://aspirely.me") is True
    msg = sent[-1]
    assert "List-Unsubscribe" in msg["headers"]
    assert "\u00e9" not in msg["html"] and "\u2014" not in msg["html"]


def test_notification_events_route_to_lists(db, sent):
    from services.notifications import channels
    _user(db)
    ch = channels.EmailChannel()
    assert ch.send(USER, "daily_digest", {"email": {"subject": "d", "html": "<p>d</p>"}})
    assert "/unsubscribe?t=" in sent[-1]["html"]
    assert ".digests." in sent[-1]["headers"]["List-Unsubscribe"]
    assert ch.send(USER, "application_wall", {"email": {"subject": "w", "html": "<p>w</p>"}})
    assert "List-Unsubscribe" not in sent[-1]["headers"]     # transactional


def test_apply_templates_are_list_mail(db, sent):
    from services import apply_email_templates as tmpl
    _user(db)
    t = tmpl.apply_approval_request([{"title": "SRE", "company": "Acme"}],
                                    approve_all_url="https://x/a", review_url="https://x/r",
                                    skip_url="https://x/s")
    assert t["list_id"] == "job_alerts"
    assert "Settings" not in t["html"] and "\u2014" not in t["html"]
    assert tmpl.send(USER, t) is True
    assert ".job_alerts." in sent[-1]["headers"]["List-Unsubscribe"]
    assert tmpl.apply_daily_summary([], [], review_url="https://x/r")["list_id"] == "digests"


def test_list_mail_is_refused_after_opt_out_and_without_an_account(db, sent):
    from services import email_service as es
    from services.notifications import service as notify
    _user(db)
    notify.unsubscribe_list(USER, "digests")
    assert es.send_email(USER, "s", "<p>b</p>", list_id="digests") is False
    assert es.send_email(USER, "s", "<p>b</p>", list_id="job_alerts") is True
    n = len(sent)
    # no account -> no working opt-out link -> no commercial mail
    assert es.send_email("stranger@example.com", "s", "<p>b</p>", list_id="job_alerts") is False
    # marketing is opt-in
    assert es.send_email(USER, "s", "<p>b</p>", list_id="marketing") is False
    assert len(sent) == n


def test_missing_postal_address_warns_once_and_still_sends(db, sent, monkeypatch, caplog):
    from services import email_service as es
    monkeypatch.setenv("COMPANY_POSTAL_ADDRESS", "")
    monkeypatch.setattr(es, "_postal_warning_logged", False)
    _user(db)
    with caplog.at_level(logging.WARNING, logger="services.email_service"):
        assert es.send_email(USER, "s", "<p>b</p>", list_id="job_alerts") is True
        assert es.send_email(USER, "s", "<p>b</p>", list_id="job_alerts") is True
    warnings = [r for r in caplog.records if "COMPANY_POSTAL_ADDRESS" in r.getMessage()]
    assert len(warnings) == 1


def test_real_transport_is_refused_under_pytest():
    from services import email_service as es
    with pytest.raises(RuntimeError):
        es._refuse_real_send_under_test()


# ---------------------------------------------------------------------------
# The list layer on the existing notification store
# ---------------------------------------------------------------------------
def test_resubscribe_restores_the_events_it_turned_off(db):
    from services.notifications import service as notify
    _user(db)
    notify.set_prefs(USER, {"events": {"daily_digest": True}})
    notify.set_email_lists(USER, {"digests": False})
    prefs = notify.get_prefs(USER)
    assert prefs["events"]["daily_digest"] is False
    notify.set_email_lists(USER, {"digests": False})           # repeat keeps the snapshot
    notify.set_email_lists(USER, {"digests": True})
    prefs = notify.get_prefs(USER)
    assert prefs["events"]["daily_digest"] is True
    assert prefs["events"]["evening_summary"] is False         # never blanket "all on"


def test_turning_an_event_on_resubscribes_its_list(db):
    from services.notifications import service as notify
    _user(db)
    notify.unsubscribe_list(USER, "job_alerts")
    notify.set_prefs(USER, {"events": {"match_alert": True}})
    assert notify.email_list_state(USER)["job_alerts"] is True


def test_master_unsubscribe_reads_all_off_and_one_list_back_on_keeps_others_off(db):
    from services.notifications import service as notify
    _user(db)
    notify.set_unsubscribed(USER, True)
    assert set(notify.email_list_state(USER).values()) == {False}
    state = notify.set_email_lists(USER, {"job_alerts": True})
    assert state == {"job_alerts": True, "digests": False, "product_updates": False,
                     "marketing": False}
    assert notify.get_prefs(USER)["unsubscribed"] is False


def test_echoing_the_current_state_changes_nothing(db):
    from services.notifications import service as notify
    _user(db)
    before = notify.get_prefs(USER)["events"]
    notify.set_email_lists(USER, notify.email_list_state(USER))
    assert notify.get_prefs(USER)["events"] == before


def test_digest_sweep_skips_users_who_left_the_digest_list(db):
    from services import scheduled_jobs
    from services.notifications import service as notify
    _user(db)
    _user(db, OTHER)
    for e in (USER, OTHER):
        notify.set_prefs(e, {"events": {"daily_digest": True}, "channels": {"email": {"enabled": True}}})
    notify.unsubscribe_list(OTHER, "digests")
    assert scheduled_jobs._target_users_for_digest("daily_digest") == [USER]


def test_saved_search_alerts_stop_after_job_alerts_unsubscribe(db, monkeypatch):
    from services import tech_feed_alerts as alerts
    from services import tech_feed_saved_searches as tfss
    from services.notifications import service as notify
    _user(db)
    db[tfss.SAVED_SEARCHES].delete_many({})
    tfss.save(USER, client_id="s1", name="SRE", filters={"query": "sre"}, alerts={"enabled": True})
    notify.set_prefs(USER, {"events": {"saved_search_digest": True}})
    notify.unsubscribe_list(USER, "job_alerts")
    monkeypatch.setattr(notify, "send", lambda *a, **k: (_ for _ in ()).throw(AssertionError("sent")))
    out = alerts.evaluate_search({"user_email": USER, "client_id": "s1", "name": "SRE",
                                  "filters": {"query": "sre"}, "alerts": {"enabled": True}})
    assert out["status"] == "unsubscribed"


# ---------------------------------------------------------------------------
# Suppression survives erasure
# ---------------------------------------------------------------------------
def test_deleted_user_is_never_emailed_again_until_they_sign_up_again(app, db, sent, monkeypatch):
    from services import account_service, email_service as es
    monkeypatch.setattr(account_service, "_purge_s3_objects", lambda uid: 0)
    monkeypatch.setattr(account_service, "_revoke_providers", lambda e: {})
    # Another test may leave a fake storage singleton behind; keep this one offline.
    monkeypatch.setattr(account_service, "_purge_asset_objects", lambda ids, keys=None: (0, []))
    _user(db)
    db.email_suppressions.insert_one({"email": OTHER, "reason": "bounce"})
    _user(db, OTHER)

    report = account_service.hard_purge(USER)
    assert report["errors"] == []
    rows = list(db.email_suppressions.find({}, {"_id": 0}))
    assert all(USER not in str(r) for r in rows)              # no raw address kept
    n = len(sent)
    assert es.send_email(USER, "s", "<p>b</p>") is False       # transactional too
    assert es.send_email(USER, "s", "<p>b</p>", list_id="job_alerts") is False

    # the OTHER user's bounce is converted to a hash on THEIR erasure
    account_service.hard_purge(OTHER)
    assert db.email_suppressions.count_documents({"email": OTHER}) == 0
    assert es.send_email(OTHER, "s", "<p>b</p>") is False
    assert len(sent) == n

    # signing up again is a new relationship: only the erasure marker goes
    resp = app.test_client().post("/api/auth/register", json={"email": USER, "password": PASSWORD})
    assert resp.status_code == 201
    assert es.send_email(USER, "s", "<p>b</p>") is True
    resp = app.test_client().post("/api/auth/register", json={"email": OTHER, "password": PASSWORD})
    assert resp.status_code == 201
    assert es.send_email(OTHER, "s", "<p>b</p>") is False      # still bounced


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
