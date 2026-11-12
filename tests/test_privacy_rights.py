"""US launch privacy surface: /api/auth/privacy, clickwrap, GPC, sensitive
data consent, and the data export.

Runs against the REAL auth service app on mongomock. No network: every real
mail transport is replaced, and `email_service` refuses a real send under
pytest anyway.
"""
import json
import sys

import pytest
from bson import ObjectId

from _vg_support import current_db, load_service_app

PASSWORD = "Str0ng!Pass1"
USER = "privacy.user@example.com"
OTHER = "someone.else@example.com"


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    monkeypatch.delenv("AWS_LAMBDA_FUNCTION_NAME", raising=False)
    from services import email_service as es
    sent = []
    monkeypatch.setattr(es, "_send_via_zeptomail", lambda *a, **k: sent.append(("zepto", a, k)) or True)
    monkeypatch.setattr(es, "_send_via_ses", lambda *a, **k: sent.append(("ses", a, k)) or True)
    return sent


@pytest.fixture()
def db():
    database = current_db()
    from services import account_service
    names = {"users", "rate_limit_buckets", "email_suppressions", "mail_aliases",
             "guest_sessions", "application_answer_library", "application_profiles",
             "auto_apply_answers", "visa_profiles", "user_preferences", "notification_prefs"}
    names.update(c for c, _ in account_service.CASCADE_COLLECTIONS)
    for c in names:
        database[c].delete_many({})
    # tests/_vg_support._reset_rate_limits is NOT active (a leading
    # underscore is skipped by `from _vg_support import *`), so clear the
    # per-process limiter here as test_auth_set_password does.
    from utils.security import get_rate_limiter
    get_rate_limiter()._requests.clear()
    yield database
    get_rate_limiter()._requests.clear()


@pytest.fixture()
def app(db):
    return load_service_app("auth")


def _token(flask_app, identity=USER):
    from flask_jwt_extended import create_access_token
    with flask_app.app_context():
        return create_access_token(identity=identity)


def _auth(token, **extra):
    return {"Authorization": f"Bearer {token}", **extra}


def _seed_user(db, email=USER, **fields):
    doc = {"_id": ObjectId(), "email": email, "status": "active", **fields}
    db.users.insert_one(doc)
    return doc


# ---------------------------------------------------------------------------
# GET / PUT /api/auth/privacy
# ---------------------------------------------------------------------------
def test_get_privacy_matches_the_contract_shape(app, db):
    _seed_user(db)
    resp = app.test_client().get("/api/auth/privacy", headers=_auth(_token(app)))
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["sale_share_opt_out"] is True
    assert set(body["gpc"]) == {"detected", "last_seen_at"}
    assert body["gpc"] == {"detected": False, "last_seen_at": None}
    assert body["sensitive"] == {"immigration_status": {"consented": False, "consented_at": None}}
    assert set(body["email"]) == {"job_alerts", "digests", "product_updates", "marketing"}
    assert all(isinstance(v, bool) for v in body["email"].values())
    assert body["email"]["marketing"] is False  # opt-in only
    assert body["terms"] == {"version": None, "accepted_at": None}
    assert body["age_confirmed"] is False


def test_privacy_requires_a_token_and_that_is_the_only_401(app, db):
    assert app.test_client().get("/api/auth/privacy").status_code == 401


def test_put_privacy_partial_updates_and_returns_the_same_shape(app, db):
    _seed_user(db)
    client, tok = app.test_client(), _token(app)
    resp = client.put("/api/auth/privacy", headers=_auth(tok), json={
        "accept_terms_version": "2026-11-11",
        "age_confirmed": True,
        "email": {"digests": False, "marketing": True},
        "immigration_status_consent": True,
    })
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["terms"]["version"] == "2026-11-11"
    assert body["terms"]["accepted_at"].endswith("Z")
    assert body["age_confirmed"] is True
    assert body["email"]["digests"] is False
    assert body["email"]["marketing"] is True
    assert body["email"]["job_alerts"] is True
    assert body["sensitive"]["immigration_status"]["consented"] is True
    assert body["sensitive"]["immigration_status"]["consented_at"]
    # The email lists live in the EXISTING notification store.
    prefs = db.notification_prefs.find_one({"user_email": USER})
    assert prefs["email_lists"]["digests"] is False
    assert prefs["events"]["daily_digest"] is False


@pytest.mark.parametrize("payload,error", [
    ({"immigration_status_consent": "yes"}, "invalid_request"),
    ({"email": {"newsletter": True}}, "invalid_request"),
    ({"email": {"digests": "off"}}, "invalid_request"),
    ({"email": ["digests"]}, "invalid_request"),
    ({"accept_terms_version": "<script>"}, "invalid_request"),
    ({"age_confirmed": False}, "age_requirement"),
])
def test_put_privacy_rejects_bad_input_with_400_never_401(app, db, payload, error):
    _seed_user(db)
    resp = app.test_client().put("/api/auth/privacy", headers=_auth(_token(app)), json=payload)
    assert resp.status_code == 400
    assert resp.get_json()["error"] == error
    assert resp.get_json()["message"]


def test_put_privacy_on_a_closed_account_is_403(app, db):
    _seed_user(db, is_deleted=True)
    resp = app.test_client().put("/api/auth/privacy", headers=_auth(_token(app)),
                                 json={"email": {"digests": False}})
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Global Privacy Control
# ---------------------------------------------------------------------------
def test_gpc_is_recorded_on_privacy_login_and_verify(app, db):
    client = app.test_client()
    assert client.post("/api/auth/register",
                       json={"email": USER, "password": PASSWORD}).status_code == 201
    assert db.users.find_one({"email": USER}).get("privacy") is None

    # login without the header records nothing
    client.post("/api/auth/login", json={"email": USER, "password": PASSWORD})
    assert (db.users.find_one({"email": USER}).get("privacy") or {}).get("gpc") is None

    resp = client.post("/api/auth/login", json={"email": USER, "password": PASSWORD},
                       headers={"Sec-GPC": "1"})
    assert resp.status_code == 200
    gpc = db.users.find_one({"email": USER})["privacy"]["gpc"]
    assert gpc["detected"] is True and gpc["last_seen_at"]

    db.users.update_one({"email": USER}, {"$unset": {"privacy": ""}})
    tok = resp.get_json()["access_token"]
    assert client.get("/api/auth/verify", headers=_auth(tok, **{"Sec-GPC": "1"})).status_code == 200
    assert db.users.find_one({"email": USER})["privacy"]["gpc"]["detected"] is True

    db.users.update_one({"email": USER}, {"$unset": {"privacy": ""}})
    body = client.get("/api/auth/privacy", headers=_auth(tok, **{"Sec-GPC": "1"})).get_json()
    assert body["gpc"]["detected"] is True
    assert body["gpc"]["last_seen_at"].endswith("Z")
    assert body["sale_share_opt_out"] is True


# ---------------------------------------------------------------------------
# Register clickwrap
# ---------------------------------------------------------------------------
def test_register_refuses_age_confirmed_false(app, db):
    resp = app.test_client().post("/api/auth/register", json={
        "email": USER, "password": PASSWORD, "age_confirmed": False, "terms_version": "2026-11-11"})
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "age_requirement"
    assert "18" in resp.get_json()["message"]
    assert db.users.find_one({"email": USER}) is None


def test_register_stores_terms_and_age(app, db):
    resp = app.test_client().post("/api/auth/register", json={
        "email": USER, "password": PASSWORD, "age_confirmed": True, "terms_version": "2026-11-11"})
    assert resp.status_code == 201
    assert set(resp.get_json()) == {"access_token", "email"}
    user = db.users.find_one({"email": USER})
    assert user["terms"]["version"] == "2026-11-11"
    assert user["terms"]["accepted_at"]
    assert user["age_confirmed"] is True


def test_register_without_clickwrap_fields_still_works(app, db):
    """Old clients and OAuth signups send neither field."""
    resp = app.test_client().post("/api/auth/register", json={"email": USER, "password": PASSWORD})
    assert resp.status_code == 201
    user = db.users.find_one({"email": USER})
    assert "terms" not in user and "age_confirmed" not in user


def test_register_rejects_a_malformed_terms_version(app, db):
    resp = app.test_client().post("/api/auth/register", json={
        "email": USER, "password": PASSWORD, "terms_version": {"$gt": ""}})
    assert resp.status_code == 400
    assert db.users.find_one({"email": USER}) is None


def test_minimum_age_is_configurable(monkeypatch):
    from utils.config import ComplianceConfig
    monkeypatch.setenv("MINIMUM_AGE", "21")
    assert ComplianceConfig.MINIMUM_AGE == 21
    monkeypatch.setenv("MINIMUM_AGE", "nonsense")
    assert ComplianceConfig.MINIMUM_AGE == 18


# ---------------------------------------------------------------------------
# Sensitive data: immigration status consent
# ---------------------------------------------------------------------------
def _seed_immigration(db, email=USER):
    db.visa_profiles.insert_one({"user_email": email, "visa_status": "F1-OPT",
                                 "opt_end_date": "2027-05-01", "country_of_birth": "India",
                                 "work_authorization": "needs_sponsorship_future"})
    db.user_preferences.insert_one({
        "user_email": email, "target_roles": ["Backend Engineer"],
        "requires_sponsorship": True, "work_authorized_us": True, "visa_status": "F-1 OPT",
        "citizenship_countries": ["India"], "work_auth": {"requires_sponsorship": True},
        "legal": {"visa_status": "F-1", "over_18": True},
    })
    db.application_answer_library.insert_many([
        {"user_email": email, "norm_key": "sponsor", "category": "sponsorship", "answer": "Yes"},
        {"user_email": email, "norm_key": "visa", "field_id": "visa_status", "answer": "F-1"},
        {"user_email": email, "norm_key": "why", "category": "essay", "answer": "I like it"},
    ])
    db.application_profiles.insert_one({"user_email": email, "vault": {
        "work_authorization": "F-1 OPT", "phone": "555-0100"}})
    db.auto_apply_answers.insert_one({"user_email": email, "application_id": "a1", "answers": {
        "requires_sponsorship": {"value": True}, "phone": {"value": "555-0100"}}})


def test_withdrawing_immigration_consent_erases_and_matching_stops_using_it(app, db):
    _seed_user(db)
    _seed_immigration(db)
    _seed_immigration(db, OTHER)
    from services import inventory_feed
    from services import work_authorization as wa
    assert wa.get_profile(USER)["needs_sponsorship"] is True

    client, tok = app.test_client(), _token(app)
    assert client.put("/api/auth/privacy", headers=_auth(tok),
                      json={"immigration_status_consent": True}).status_code == 200
    resp = client.put("/api/auth/privacy", headers=_auth(tok),
                      json={"immigration_status_consent": False})
    assert resp.status_code == 200
    assert resp.get_json()["sensitive"]["immigration_status"] == {"consented": False,
                                                                  "consented_at": None}

    assert db.visa_profiles.count_documents({"user_email": USER}) == 0
    prefs = db.user_preferences.find_one({"user_email": USER})
    for key in ("requires_sponsorship", "work_authorized_us", "visa_status",
                "citizenship_countries", "work_auth"):
        assert key not in prefs, key
    assert "visa_status" not in prefs["legal"]
    assert prefs["legal"]["over_18"] is True            # not immigration data
    assert prefs["target_roles"] == ["Backend Engineer"]
    assert [r["norm_key"] for r in db.application_answer_library.find({"user_email": USER})] == ["why"]
    assert db.application_profiles.find_one({"user_email": USER})["vault"] == {"phone": "555-0100"}
    assert db.auto_apply_answers.find_one({"user_email": USER})["answers"] == {
        "phone": {"value": "555-0100"}}

    # Matching no longer sees a status, and does not crash.
    profile = wa.get_profile(USER)
    assert profile["work_authorization"] == wa.UNKNOWN
    assert profile["needs_sponsorship"] is False
    assert inventory_feed.requires_sponsorship(USER) is False

    # The other user is untouched.
    assert db.visa_profiles.count_documents({"user_email": OTHER}) == 1
    assert wa.get_profile(OTHER)["needs_sponsorship"] is True
    withdrawn = db.users.find_one({"email": USER})["privacy"]["sensitive"]["immigration_status"]
    assert withdrawn["withdrawn_at"]


# ---------------------------------------------------------------------------
# Data export
# ---------------------------------------------------------------------------
SECRET_VALUES = ("$2b$12$hashhashhash", "refresh-secret-value", "access-secret-value",
                 "apify_api_secret_value", "pw-blob-secret", "fp-hash-value", "otp-123456")


def _seed_everything(db, email):
    from services import account_service
    user = _seed_user(db, email, password_hash=SECRET_VALUES[0],
                      fingerprints=[SECRET_VALUES[5]], name="Pat Example",
                      created_at=__import__("datetime").datetime(2026, 1, 2, 3, 4, 5))
    for coll, key in account_service.CASCADE_COLLECTIONS:
        if key == "_id":
            db[coll].insert_one({"_id": email, "marker": f"{coll}:{email}"})
        else:
            db[coll].insert_one({key: email, "marker": f"{coll}:{email}"})
    db.mail_aliases.insert_one({"user_email": email, "local_part": f"x.{email[:3]}", "state": "active"})
    db.guest_sessions.insert_one({"guest_id": f"g-{email}", "claimed_by": email})
    db.email_suppressions.insert_one({"email": email, "reason": "bounce"})
    # secrets inside otherwise exportable rows
    db.gmail_connections.update_one({"user_email": email}, {"$set": {
        "refresh_token_enc": SECRET_VALUES[1], "access_token": SECRET_VALUES[2],
        "connected_email": email}})
    db.user_apify_keys.update_one({"user_email": email}, {"$set": {"api_key": SECRET_VALUES[3]}})
    db.ats_credentials.update_one({"user_email": email}, {"$set": {
        "site": "workday", "username": email, "password_blob": SECRET_VALUES[4],
        "nested": {"password": "inner", "ok": 1}}})
    db.application_otp_grants.update_one({"user_email": email}, {"$set": {"code": SECRET_VALUES[6]}})
    return user


def _export(client, tok):
    resp = client.get("/api/auth/account/export", headers=_auth(tok))
    return resp, (json.loads(resp.get_data(as_text=True)) if resp.status_code == 200 else None)


def test_export_covers_every_user_collection_and_excludes_secrets(app, db):
    from services import account_service, privacy_service
    _seed_everything(db, USER)
    _seed_everything(db, OTHER)

    resp, data = _export(app.test_client(), _token(app))
    assert resp.status_code == 200, resp.get_data(as_text=True)
    cd = resp.headers["Content-Disposition"]
    assert cd.startswith('attachment; filename="aspirely-data-export-') and cd.endswith('.json"')
    raw = resp.get_data(as_text=True)

    assert data["format"] == "aspirely-data-export"
    assert data["account"] == {"email": USER}
    expected = {"users", "mail_aliases", "guest_sessions", "email_suppressions"}
    expected.update(c for c, _ in account_service.CASCADE_COLLECTIONS
                    if c not in privacy_service.WITHHELD_COLLECTIONS)
    assert expected <= set(data["collections"]), expected - set(data["collections"])
    for coll in expected:
        assert len(data["collections"][coll]) == 1, coll
    # withheld stores are named, never dumped
    for coll in privacy_service.WITHHELD_COLLECTIONS:
        assert coll not in data["collections"]
    assert {w["collection"] for w in data["withheld"]} == set(privacy_service.WITHHELD_COLLECTIONS)

    # no other user's data, no secrets, JSON-safe types
    assert OTHER not in raw
    for secret in SECRET_VALUES:
        assert secret not in raw, secret
    user = data["collections"]["users"][0]
    assert "password_hash" not in user and "fingerprints" not in user
    assert user["created_at"] == "2026-01-02T03:04:05Z"
    assert isinstance(user["_id"], str)
    ats = data["collections"]["ats_credentials"][0]
    assert ats["site"] == "workday" and ats["nested"] == {"ok": 1}
    assert data["summary"]["truncated"] == []
    assert set(data["summary"]["searched"]) >= expected


def test_export_is_limited_to_five_a_day(app, db):
    _seed_user(db)
    client, tok = app.test_client(), _token(app)
    for _ in range(5):
        assert client.get("/api/auth/account/export", headers=_auth(tok)).status_code == 200
    resp = client.get("/api/auth/account/export", headers=_auth(tok))
    assert resp.status_code == 429
    body = resp.get_json()
    assert body["error"] == "rate_limited"
    assert isinstance(body["retry_after"], int) and 0 < body["retry_after"] <= 86400
    assert resp.headers["Retry-After"] == str(body["retry_after"])


def test_export_for_missing_or_closed_account_is_never_401(app, db):
    client = app.test_client()
    resp = client.get("/api/auth/account/export", headers=_auth(_token(app, "ghost@example.com")))
    assert resp.status_code == 400
    _seed_user(db, is_deleted=True)
    assert client.get("/api/auth/account/export", headers=_auth(_token(app))).status_code == 403


def test_export_caps_a_huge_collection_and_says_so(app, db, monkeypatch):
    from services import privacy_service
    monkeypatch.setattr(privacy_service, "EXPORT_MAX_DOCS_PER_COLLECTION", 3)
    _seed_user(db)
    db.user_events.insert_many([{"user_email": USER, "n": i} for i in range(5)])
    _resp, data = _export(app.test_client(), _token(app))
    assert len(data["collections"]["user_events"]) == 3
    assert data["summary"]["truncated"] == ["user_events"]


# ---------------------------------------------------------------------------
# The gateway still builds with every service blueprint
# ---------------------------------------------------------------------------
def test_gateway_builds_and_serves_the_new_routes():
    import importlib.util
    from _vg_support import REPO_ROOT
    spec = importlib.util.spec_from_file_location(
        "_gateway_wsgi_privacy", REPO_ROOT / "services" / "gateway" / "wsgi.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    rules = {r.rule for r in mod.app.url_map.iter_rules()}
    for route in ("/api/auth/privacy", "/api/auth/account/export",
                  "/api/auth/email/unsubscribe", "/api/auth/register", "/api/health"):
        assert route in rules, route


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
