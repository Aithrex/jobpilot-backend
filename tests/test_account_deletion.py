"""Offline tests for the account-erasure cascade (AUDIT-P1-004 / PRIV-1).

Runs fully offline on mongomock (see tests/_vg_support.py). S3 and provider
revocation are monkeypatched to no-ops so no network / AWS is touched — the
focus here is the Mongo cascade and, critically, its ISOLATION: purging user A
must not touch user B's identical rows.
"""
import pytest
from bson import ObjectId

from services import account_service


USER_A = "alice@example.com"
USER_B = "bob@example.com"


@pytest.fixture(autouse=True)
def _clean_all():
    """Wipe users + every cascade collection before each test.

    The shared ``mock_db`` fixture only clears the Voice-Graph subset, so
    ``users`` and the non-VG collections would otherwise leak across tests.
    """
    from utils.db_connect import DBConnect
    db = DBConnect().get_db()
    db.users.delete_many({})
    for coll, _ in account_service.CASCADE_COLLECTIONS:
        db[coll].delete_many({})
    yield


def _seed_user(db, email):
    """Insert one row for `email` in every cascade collection + the users doc."""
    db.users.insert_one({"_id": ObjectId(), "email": email, "role": "user"})
    for coll, key in account_service.CASCADE_COLLECTIONS:
        db[coll].insert_one({key: email, "marker": f"{coll}:{email}"})


def _total_rows(db, email):
    """Count every row keyed to `email` across users + all cascade collections."""
    n = db.users.count_documents({"email": email})
    for coll, key in account_service.CASCADE_COLLECTIONS:
        n += db[coll].count_documents({key: email})
    return n


def test_hard_purge_erases_owner_and_spares_others(mock_db, monkeypatch):
    # No network: stub S3 + provider revocation.
    monkeypatch.setattr(account_service, "_purge_s3_objects", lambda user_id: 7)
    revoked = {}
    def _fake_revoke(email):
        revoked["email"] = email
        return {"gmail": True, "github": True, "linkedin": True}
    monkeypatch.setattr(account_service, "_revoke_providers", _fake_revoke)

    db = mock_db
    _seed_user(db, USER_A)
    _seed_user(db, USER_B)

    # Sanity: both users fully populated before the purge.
    expected_rows = len(account_service.CASCADE_COLLECTIONS) + 1  # +users
    assert _total_rows(db, USER_A) == expected_rows
    assert _total_rows(db, USER_B) == expected_rows

    report = account_service.hard_purge(USER_A)

    # Every collection keyed to A is empty, including the users doc.
    assert db.users.count_documents({"email": USER_A}) == 0
    for coll, key in account_service.CASCADE_COLLECTIONS:
        assert db[coll].count_documents({key: USER_A}) == 0, f"{coll} not purged"
    assert _total_rows(db, USER_A) == 0

    # B is completely untouched.
    assert _total_rows(db, USER_B) == expected_rows

    # Report + side effects are correct.
    assert report["errors"] == []
    assert report["s3_objects_deleted"] == 7
    assert revoked["email"] == USER_A
    assert report["deleted"]["users"] == 1
    assert report["deleted"]["user_resumes"] == 1


def test_hard_purge_is_idempotent(mock_db, monkeypatch):
    monkeypatch.setattr(account_service, "_purge_s3_objects", lambda user_id: 0)
    monkeypatch.setattr(account_service, "_revoke_providers", lambda email: {})

    db = mock_db
    _seed_user(db, USER_A)

    first = account_service.hard_purge(USER_A)
    assert first["errors"] == []
    assert _total_rows(db, USER_A) == 0

    # Second run over an already-empty account: no rows, no errors, no raise.
    second = account_service.hard_purge(USER_A)
    assert second["errors"] == []
    assert second["deleted"] == {}
    assert _total_rows(db, USER_A) == 0


def test_hard_purge_preserves_running_job(mock_db, monkeypatch):
    monkeypatch.setattr(account_service, "_purge_s3_objects", lambda user_id: 0)
    monkeypatch.setattr(account_service, "_revoke_providers", lambda email: {})

    db = mock_db
    db.users.insert_one({"_id": ObjectId(), "email": USER_A})
    # Two of the user's jobs: the running purge job, plus an unrelated old one.
    db.resume_jobs.insert_one({"user_email": USER_A, "job_id": "purge-123"})
    db.resume_jobs.insert_one({"user_email": USER_A, "job_id": "old-tailor"})

    account_service.hard_purge(USER_A, exclude_job_id="purge-123")

    # The running job survives (so the poller can read completion); the rest go.
    remaining = list(db.resume_jobs.find({"user_email": USER_A}))
    assert len(remaining) == 1
    assert remaining[0]["job_id"] == "purge-123"


def test_soft_delete_flags_user_and_clears_reset_tokens(mock_db):
    db = mock_db
    db.users.insert_one({"_id": ObjectId(), "email": USER_A, "status": "active"})
    db.password_reset_tokens.insert_one({"user_email": USER_A, "token_hash": "x"})

    out = account_service.soft_delete(USER_A)

    assert out["soft_deleted"] is True
    assert out["existed"] is True
    doc = db.users.find_one({"email": USER_A})
    assert doc["is_deleted"] is True
    assert doc["deletion_status"] == "pending_purge"
    assert db.password_reset_tokens.count_documents({"user_email": USER_A}) == 0


def test_one_failing_store_does_not_abort_cascade(mock_db, monkeypatch):
    """A single store raising must not stop the others (fail-soft per store)."""
    monkeypatch.setattr(account_service, "_purge_s3_objects", lambda user_id: 0)
    monkeypatch.setattr(account_service, "_revoke_providers", lambda email: {})

    db = mock_db
    _seed_user(db, USER_A)

    # Make exactly one store raise, leaving all others healthy.
    def _boom(*_a, **_k):
        raise RuntimeError("simulated mongo outage")
    monkeypatch.setattr(db["saved_jobs"], "delete_many", _boom)

    report = account_service.hard_purge(USER_A)

    # The failure is reported, but every OTHER collection still got purged.
    assert any("saved_jobs" in e for e in report["errors"])
    assert db["saved_jobs"].count_documents({"user_email": USER_A}) == 1  # still there
    assert db.user_resumes.count_documents({"user_email": USER_A}) == 0
    assert db.users.count_documents({"email": USER_A}) == 0
