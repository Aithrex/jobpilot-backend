"""Route-level guard for the account-deletion endpoint (AUDIT-P1-004).

Proves DELETE /api/auth/account is registered and enforces the explicit
confirmation before doing anything — without a valid {"confirm":"DELETE"} it
must be a 400 (never 401, never 500), returning BEFORE any purge is enqueued.
Offline: mongomock via conftest; the 400 path never reaches AWS.
"""
import sys

import pytest

from _vg_support import load_service_app


@pytest.fixture()
def client():
    # Load under a unique module name so it can't collide with the resume
    # service's app.py when the whole suite runs in one pytest process.
    return load_service_app("auth")


def _token(flask_app, identity="deleteme@aspirely.me"):
    from flask_jwt_extended import create_access_token
    with flask_app.app_context():
        return create_access_token(identity=identity)


def test_delete_requires_confirmation(client):
    token = _token(client)
    resp = client.test_client().delete(
        "/api/auth/account",
        headers={"Authorization": f"Bearer {token}"},
        json={},  # no confirm
    )
    assert resp.status_code == 400, resp.get_data(as_text=True)
    assert resp.get_json().get("error") == "confirmation_required"


def test_delete_requires_auth(client):
    # No token → @jwt_required() → 401 (the ONE legitimate 401 path).
    resp = client.test_client().delete("/api/auth/account", json={"confirm": "DELETE"})
    assert resp.status_code == 401


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
