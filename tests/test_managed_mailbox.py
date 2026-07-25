"""Managed Application Mailbox — provisioning + alias model (managed-mailbox-v1 §1/§2/§10).

Offline: Mongo is mongomock (conftest/_vg_support). No AWS, no Bedrock, no
network. The unique index on ``mail_aliases.local_part`` is created via
``ensure_indexes`` so the collision/tombstone/idempotency guards are exercised
against real ``DuplicateKeyError`` behavior, not just the in-code scan.
"""
import re

import pytest

import services.managed_mailbox_service as mmb

DOMAIN = "pilotmail.co"


@pytest.fixture()
def svc(monkeypatch):
    """A clean managed-mailbox surface: domain configured, both collections
    emptied, indexes (incl. the local_part unique guard) ensured."""
    monkeypatch.setenv("APPLY_MAIL_DOMAIN", DOMAIN)
    monkeypatch.setenv("USE_SSM_SECRETS", "false")
    from utils.db_connect import DBConnect

    db = DBConnect().get_db()
    db["managed_mailboxes"].delete_many({})
    db["mail_aliases"].delete_many({})
    mmb.ensure_indexes()
    return mmb


def _db():
    from utils.db_connect import DBConnect

    return DBConnect().get_db()


# ---------------------------------------------------------------------------
# _slug_name (pure)
# ---------------------------------------------------------------------------

def test_slug_basic():
    assert mmb._slug_name("Harshith Manne") == "harshith.manne"


def test_slug_unicode_ascii_fold():
    assert mmb._slug_name("José Núñez") == "jose.nunez"


def test_slug_symbols_stripped():
    assert mmb._slug_name("John @#$ Smith!!!") == "john.smith"
    assert mmb._slug_name("  O'Brien-McFly  ") == "o.brien.mcfly"


def test_slug_degenerate_returns_empty():
    # Empty or pure-symbol names slug to "" — provision falls back to a privacy
    # local part rather than an empty/dotted-only address.
    assert mmb._slug_name("") == ""
    assert mmb._slug_name("!!!") == ""
    assert mmb._slug_name("...") == ""


# ---------------------------------------------------------------------------
# provision — shape + naming
# ---------------------------------------------------------------------------

def test_provision_returns_named_address(svc):
    m = svc.provision("harshith@aithrex.tech", "Harshith Manne")
    assert m == {
        "provisioned": True,
        "address": "harshith.manne@pilotmail.co",
        "mode": "approval",
        "paused": False,
        "forwarding_enabled": False,
        "forward_to": None,
        "forward_to_verified": False,
        "unread_count": 0,
    }


def test_provision_privacy_alias(svc):
    m = svc.provision("p@x.com", "Harshith Manne", privacy_alias=True)
    assert re.match(r"^career\.[0-9a-f]{6}@pilotmail\.co$", m["address"])
    alias = _db()["mail_aliases"].find_one({"user_email": "p@x.com"})
    assert alias["kind"] == "privacy"


def test_provision_empty_name_falls_back_to_privacy(svc):
    m = svc.provision("n@x.com", "!!!")
    assert re.match(r"^career\.[0-9a-f]{6}@pilotmail\.co$", m["address"])


# ---------------------------------------------------------------------------
# D2 collision handling
# ---------------------------------------------------------------------------

def test_collision_appends_suffix(svc):
    a = svc.provision("a@x.com", "John Smith")
    b = svc.provision("b@x.com", "John Smith")
    assert a["address"] == "john.smith@pilotmail.co"
    assert b["address"] == "john.smith2@pilotmail.co"


def test_collision_token_fallback_after_cap(svc):
    # base + john.smith2 from real provisions; seed john.smith3..john.smith20 so
    # every numeric candidate (base, 2..20) is taken -> the 21st must fall back
    # to the opaque base.<4-hex> form (D2).
    svc.provision("a@x.com", "John Smith")   # john.smith
    svc.provision("b@x.com", "John Smith")   # john.smith2
    db = _db()
    for i in range(3, mmb._COLLISION_CAP + 1):  # 3..20
        db["mail_aliases"].insert_one({
            "alias_id": f"seed_{i}",
            "mailbox_id": "seed_mbx",
            "user_email": "seed@x.com",
            "local_part": f"john.smith{i}",
            "address": f"john.smith{i}@{DOMAIN}",
            "kind": "primary",
            "state": "active",
            "created_at": mmb._now(),
        })
    c = svc.provision("c@x.com", "John Smith")
    assert re.match(r"^john\.smith\.[0-9a-f]{4}@pilotmail\.co$", c["address"]), c["address"]


# ---------------------------------------------------------------------------
# §1.3 tombstone anti-recycle
# ---------------------------------------------------------------------------

def test_rotate_tombstones_and_blocks_recycle(svc):
    a = svc.provision("a@x.com", "Harshith Manne")
    assert a["address"] == "harshith.manne@pilotmail.co"

    rotated = svc.rotate("a@x.com", "Harshith Manne")
    assert rotated["address"] != a["address"]
    assert rotated["address"].startswith("harshith.manne")

    old = _db()["mail_aliases"].find_one({"local_part": "harshith.manne"})
    assert old["state"] == "retired"
    assert old.get("retired_at") is not None

    # A DIFFERENT user with the same name can never be handed the retired local.
    b = svc.provision("b@x.com", "Harshith Manne")
    assert b["address"] != "harshith.manne@pilotmail.co"
    assert b["address"].startswith("harshith.manne")


# ---------------------------------------------------------------------------
# concurrent / idempotent provision
# ---------------------------------------------------------------------------

def test_provision_is_idempotent(svc):
    a = svc.provision("a@x.com", "Harshith Manne")
    b = svc.provision("a@x.com", "Harshith Manne")
    assert a["address"] == b["address"]
    db = _db()
    assert db["managed_mailboxes"].count_documents({"user_email": "a@x.com"}) == 1
    assert db["mail_aliases"].count_documents(
        {"user_email": "a@x.com", "state": "active"}
    ) == 1


def test_provision_race_recovers_via_duplicate_key(svc, monkeypatch):
    # Exercise the concurrent path: a second container's pre-check misses the
    # existing mailbox, tries to insert its own, hits the unique-index
    # DuplicateKeyError, and recovers by returning the winner's mailbox.
    a = svc.provision("a@x.com", "Harshith Manne")

    coll = mmb._mailboxes()
    real_find_one = coll.find_one
    state = {"n": 0}

    def racing_find_one(*args, **kwargs):
        state["n"] += 1
        if state["n"] == 1:  # the pre-insert lookup "misses"
            return None
        return real_find_one(*args, **kwargs)

    monkeypatch.setattr(coll, "find_one", racing_find_one)
    b = svc.provision("a@x.com", "Harshith Manne")
    monkeypatch.undo()

    assert b["address"] == a["address"]
    assert _db()["managed_mailboxes"].count_documents({"user_email": "a@x.com"}) == 1


# ---------------------------------------------------------------------------
# resolve_alias — owner resolution + hostile input
# ---------------------------------------------------------------------------

def test_resolve_active_alias(svc):
    svc.provision("a@x.com", "Harshith Manne")
    r = svc.resolve_alias("harshith.manne@pilotmail.co")
    assert r is not None
    assert r["user_email"] == "a@x.com"
    assert r["state"] == "active"
    assert r["mailbox_id"] and r["alias_id"]


def test_resolve_accepts_bare_local(svc):
    svc.provision("a@x.com", "Harshith Manne")
    r = svc.resolve_alias("harshith.manne")
    assert r and r["user_email"] == "a@x.com"


def test_resolve_retired_returns_none(svc):
    svc.provision("a@x.com", "Harshith Manne")
    svc.rotate("a@x.com", "Harshith Manne")
    # The retired local is a tombstone — it must never resolve to an owner.
    assert svc.resolve_alias("harshith.manne@pilotmail.co") is None


def test_resolve_unknown_returns_none(svc):
    assert svc.resolve_alias("nobody@pilotmail.co") is None


def test_resolve_injection_input_safe(svc):
    svc.provision("a@x.com", "Harshith Manne")
    for hostile in ["$ne", "$where", "$gt.$lt", "{'$ne': null}", "$ne@pilotmail.co", ""]:
        assert svc.resolve_alias(hostile) is None


# ---------------------------------------------------------------------------
# forwarding
# ---------------------------------------------------------------------------

def test_set_forwarding_new_target_unverifies(svc):
    svc.provision("a@x.com", "Harshith Manne")
    m = svc.set_forwarding("a@x.com", True, "harshith@aithrex.tech")
    assert m["forwarding_enabled"] is True
    assert m["forward_to"] == "harshith@aithrex.tech"
    assert m["forward_to_verified"] is False


def test_set_forwarding_requires_mailbox(svc):
    with pytest.raises(LookupError):
        svc.set_forwarding("nobody@x.com", True, "x@y.com")


# ---------------------------------------------------------------------------
# owner scoping
# ---------------------------------------------------------------------------

def test_get_mailbox_owner_scoped(svc):
    svc.provision("a@x.com", "Harshith Manne")
    other = svc.get_mailbox("b@x.com")
    assert other["provisioned"] is False
    assert other["address"] is None

    mine = svc.get_mailbox("a@x.com")
    assert mine["provisioned"] is True
    assert mine["address"] == "harshith.manne@pilotmail.co"


def test_set_paused_owner_scoped(svc):
    svc.provision("a@x.com", "Harshith Manne")
    m = svc.set_paused("a@x.com", True)
    assert m["paused"] is True
    with pytest.raises(LookupError):
        svc.set_paused("b@x.com", True)


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-q"]))
