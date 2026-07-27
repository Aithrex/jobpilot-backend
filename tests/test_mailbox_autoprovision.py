"""Managed Application Mailbox — AUTO-PROVISION at signup + forwarding defaults.

AUTOAPPLY_REPAIR_SPEC §6: the user must NEVER be asked to create a private email
address, nor to choose where mail forwards. Both happen automatically from the
registered signup email. This module pins that end of the contract:

  * ``POST /api/auth/register`` provisions the mailbox (best-effort — a mailbox
    failure can NEVER fail signup, and never changes the 201 shape);
  * ``oauth_service.upsert_user`` does the same for social sign-in, with the
    provider-supplied name so the local part is readable immediately;
  * a fresh mailbox forwards to the account's OWN registered address, ENABLED and
    already VERIFIED (no round-trip) — while ``set_forwarding`` still RE-ARMS
    verification when the user overrides the destination to some other address;
  * the opaque ``career.<hex>`` alias minted when no name was resolvable
    self-repairs to ``firstname.lastname`` once a name exists — but never after
    anyone has mailed the address;
  * the idempotent backfill for pre-existing users.

Fully offline: Mongo is mongomock (conftest/_vg_support), and every outbound
email (welcome / verification) is captured, never sent.
"""
from __future__ import annotations

import pytest

import services.managed_mailbox_service as mailbox
from _vg_support import load_service_app

DOMAIN = "pilotmail.co"
EMAIL = "new.user@example.com"
PASSWORD = "Str0ng!Pass1"

_COLLECTIONS = (
    "users", "user_preferences", "managed_mailboxes", "mail_aliases",
    "mail_messages", "mail_forward_verifications", "user_resumes",
)


def _db():
    from utils.db_connect import DBConnect
    return DBConnect().get_db()


@pytest.fixture()
def env(monkeypatch):
    monkeypatch.setenv("USE_SSM_SECRETS", "false")
    monkeypatch.setenv("APPLY_MAIL_DOMAIN", DOMAIN)
    monkeypatch.setenv("FRONTEND_URL", "https://app.test")
    db = _db()
    for c in _COLLECTIONS:
        db[c].delete_many({})
    mailbox.ensure_indexes()
    # No email ever leaves the process (welcome mail is best-effort in register).
    import services.email_service as es
    monkeypatch.setattr(es, "send_email", lambda *a, **k: True)
    return monkeypatch


@pytest.fixture()
def app(env):
    return load_service_app("auth")


# ---------------------------------------------------------------------------
# 1. Auto-provision at signup (§6) — email/password
# ---------------------------------------------------------------------------

def test_register_auto_provisions_the_mailbox(app):
    resp = app.test_client().post("/api/auth/register",
                                  json={"email": EMAIL, "password": PASSWORD})
    assert resp.status_code == 201
    # The auth contract is untouched: flat, {access_token, email}.
    body = resp.get_json()
    assert set(body) == {"access_token", "email"}
    assert body["email"] == EMAIL

    mb = mailbox.get_mailbox(EMAIL)
    assert mb["provisioned"] is True
    assert mb["address"].endswith(f"@{DOMAIN}")
    # Forwarding is armed at the registered address with no user interaction.
    assert mb["forwarding_enabled"] is True
    assert mb["forward_to"] == EMAIL
    assert mb["forward_to_verified"] is True


def test_register_stores_the_name_and_uses_it_for_the_local_part(app):
    resp = app.test_client().post("/api/auth/register", json={
        "email": EMAIL, "password": PASSWORD, "name": "Harshith Manne",
    })
    assert resp.status_code == 201
    assert _db().users.find_one({"email": EMAIL})["name"] == "Harshith Manne"
    assert mailbox.get_mailbox(EMAIL)["address"] == f"harshith.manne@{DOMAIN}"


def test_register_without_a_name_mints_a_repairable_stopgap(app):
    # The current sign-up form collects only email + password, so there is no
    # name to slug -> opaque alias, FLAGGED for self-repair.
    app.test_client().post("/api/auth/register", json={"email": EMAIL, "password": PASSWORD})
    mb = mailbox.get_mailbox(EMAIL)
    assert mb["address"].startswith("career.")
    assert mb["name_pending"] is True


def test_mailbox_failure_never_fails_signup(app, monkeypatch):
    """The provision call is best-effort — exactly like the welcome email."""
    def _boom(*a, **k):
        raise RuntimeError("mongo is down")

    monkeypatch.setattr(mailbox, "provision", _boom)
    resp = app.test_client().post("/api/auth/register",
                                  json={"email": EMAIL, "password": PASSWORD})
    assert resp.status_code == 201
    assert _db().users.find_one({"email": EMAIL}) is not None
    assert _db().managed_mailboxes.find_one({"user_email": EMAIL}) is None


def test_register_is_still_the_only_source_of_identity(app):
    """D7: the mailbox owner is the registered email, never a body-supplied one."""
    app.test_client().post("/api/auth/register", json={
        "email": EMAIL, "password": PASSWORD, "user_email": "attacker@evil.com",
    })
    assert _db().managed_mailboxes.find_one({"user_email": "attacker@evil.com"}) is None
    assert _db().managed_mailboxes.find_one({"user_email": EMAIL}) is not None


def test_second_register_is_409_and_mints_no_second_mailbox(app):
    client = app.test_client()
    client.post("/api/auth/register", json={"email": EMAIL, "password": PASSWORD})
    resp = client.post("/api/auth/register", json={"email": EMAIL, "password": PASSWORD})
    assert resp.status_code == 409
    assert resp.status_code != 401          # auth failures are NEVER 401
    assert _db().managed_mailboxes.count_documents({"user_email": EMAIL}) == 1


# ---------------------------------------------------------------------------
# 2. Auto-provision at signup (§6) — OAuth
# ---------------------------------------------------------------------------

def test_oauth_new_user_auto_provisions_with_a_readable_name(env):
    import importlib.util
    from pathlib import Path
    import sys

    svc_dir = Path(__file__).resolve().parent.parent / "services" / "auth"
    if str(svc_dir) not in sys.path:
        sys.path.insert(0, str(svc_dir))
    spec = importlib.util.spec_from_file_location(
        "_oauth_svc", svc_dir / "services" / "oauth_service.py")
    oauth = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(oauth)

    out = oauth.upsert_user("google", {
        "subject": "sub-123", "email": EMAIL,
        "email_verified": True, "name": "Harshith Manne",
    })
    assert out == EMAIL
    mb = mailbox.get_mailbox(EMAIL)
    # OAuth already carries a name -> readable local part straight away.
    assert mb["address"] == f"harshith.manne@{DOMAIN}"
    assert mb["name_pending"] is False
    assert mb["forward_to"] == EMAIL
    assert mb["forward_to_verified"] is True


# ---------------------------------------------------------------------------
# 3. Forwarding defaults + the RETAINED re-verify-on-override (§6)
# ---------------------------------------------------------------------------

def test_provision_defaults_to_the_registered_email_verified(env):
    mb = mailbox.provision(EMAIL, "Harshith Manne")
    assert (mb["forwarding_enabled"], mb["forward_to"], mb["forward_to_verified"]) == (
        True, EMAIL, True)


def test_provision_is_idempotent(env):
    a = mailbox.provision(EMAIL, "Harshith Manne")
    b = mailbox.provision(EMAIL, "Harshith Manne")
    assert a["address"] == b["address"]
    assert _db().managed_mailboxes.count_documents({"user_email": EMAIL}) == 1
    assert _db().mail_aliases.count_documents({"user_email": EMAIL}) == 1


def test_overriding_the_destination_still_requires_verification(env):
    mailbox.provision(EMAIL, "Harshith Manne")
    mb = mailbox.set_forwarding(EMAIL, True, "somewhere.else@example.com")
    assert mb["forward_to"] == "somewhere.else@example.com"
    assert mb["forward_to_verified"] is False   # re-verify kept for overrides


def test_reverting_to_the_registered_email_needs_no_round_trip(env):
    mailbox.provision(EMAIL, "Harshith Manne")
    mailbox.set_forwarding(EMAIL, True, "somewhere.else@example.com")
    mb = mailbox.set_forwarding(EMAIL, True, EMAIL)
    assert mb["forward_to"] == EMAIL
    assert mb["forward_to_verified"] is True    # ownership already proven at signup


def test_a_privacy_alias_is_never_flagged_for_repair(env):
    mb = mailbox.provision(EMAIL, "Harshith Manne", privacy_alias=True)
    assert mb["address"].startswith("career.")
    assert mb["name_pending"] is False


# ---------------------------------------------------------------------------
# 4. Local-part self-repair
# ---------------------------------------------------------------------------

def test_repair_upgrades_an_unused_stopgap_alias(env):
    before = mailbox.provision(EMAIL, None)
    assert before["address"].startswith("career.")

    after = mailbox.repair_local_part(EMAIL, "Harshith Manne")
    assert after["address"] == f"harshith.manne@{DOMAIN}"
    assert after["name_pending"] is False
    # The old local part is TOMBSTONED, never recycled (§1.3).
    old_local = before["address"].split("@", 1)[0]
    old = _db().mail_aliases.find_one({"local_part": old_local})
    assert old["state"] == "retired"
    assert mailbox.resolve_alias(before["address"]) is None


def test_repair_never_changes_an_address_someone_already_mailed(env):
    before = mailbox.provision(EMAIL, None)
    alias = _db().mail_aliases.find_one({"user_email": EMAIL})
    _db().mail_messages.insert_one({
        "message_id": "m1", "alias_id": alias["alias_id"], "user_email": EMAIL,
    })
    after = mailbox.repair_local_part(EMAIL, "Harshith Manne")
    assert after["address"] == before["address"]   # untouched — delivery must not break
    assert after["name_pending"] is False          # and we stop re-checking


def test_repair_is_a_noop_without_a_name(env):
    before = mailbox.provision(EMAIL, None)
    after = mailbox.repair_local_part(EMAIL, None)
    assert after["address"] == before["address"]
    assert after["name_pending"] is True


def test_repair_leaves_a_deliberate_privacy_alias_alone(env):
    before = mailbox.provision(EMAIL, "Harshith Manne", privacy_alias=True)
    after = mailbox.repair_local_part(EMAIL, "Harshith Manne")
    assert after["address"] == before["address"]


def test_get_mailbox_route_self_repairs_from_the_users_name(env):
    mailbox.provision(EMAIL, None)
    _db().users.insert_one({"email": EMAIL, "name": "Harshith Manne"})

    resume_app = load_service_app("resume")
    from flask_jwt_extended import create_access_token
    with resume_app.app_context():
        token = create_access_token(identity=EMAIL)
    resp = resume_app.test_client().get(
        "/api/resume/mailbox", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.get_json()["address"] == f"harshith.manne@{DOMAIN}"


# ---------------------------------------------------------------------------
# 5. Backfill for pre-existing users (idempotent)
# ---------------------------------------------------------------------------

def test_backfill_sets_defaults_only_on_unconfigured_mailboxes(env):
    mailbox.provision("legacy@example.com", "Legacy User")
    mailbox.provision("chose@example.com", "Chose User")
    # Simulate the pre-§6 shape for one, and a DELIBERATE choice for the other.
    _db().managed_mailboxes.update_one(
        {"user_email": "legacy@example.com"},
        {"$set": {"forwarding_enabled": False, "forward_to": None,
                  "forward_to_verified": False}},
    )
    _db().managed_mailboxes.update_one(
        {"user_email": "chose@example.com"},
        {"$set": {"forward_to": "personal@example.com", "forward_to_verified": False}},
    )

    assert mailbox.backfill_forwarding_defaults() == {"scanned": 1, "updated": 1}

    fixed = mailbox.get_mailbox("legacy@example.com")
    assert (fixed["forwarding_enabled"], fixed["forward_to"], fixed["forward_to_verified"]) == (
        True, "legacy@example.com", True)
    # The user's own choice is untouched — address AND verified state.
    kept = mailbox.get_mailbox("chose@example.com")
    assert kept["forward_to"] == "personal@example.com"
    assert kept["forward_to_verified"] is False

    # Idempotent: a second run matches nothing.
    assert mailbox.backfill_forwarding_defaults() == {"scanned": 0, "updated": 0}


def test_backfill_dry_run_writes_nothing(env):
    mailbox.provision("legacy@example.com", "Legacy User")
    _db().managed_mailboxes.update_one(
        {"user_email": "legacy@example.com"}, {"$set": {"forward_to": None}})
    assert mailbox.backfill_forwarding_defaults(dry_run=True) == {"scanned": 1, "updated": 1}
    assert _db().managed_mailboxes.find_one(
        {"user_email": "legacy@example.com"})["forward_to"] is None


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
