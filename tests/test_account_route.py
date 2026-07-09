"""Route-level guard for the account-deletion endpoint (AUDIT-P1-004).

Proves DELETE /api/auth/account is registered and enforces the explicit
confirmation before doing anything — without a valid {"confirm":"DELETE"} it
must be a 400 (never 401, never 500), returning BEFORE any purge is enqueued.
Offline: mongomock via conftest; the 400 path never reaches AWS.
"""
import sys
from pathlib import Path

import pytest

_AUTH_SVC = Path(__file__).resolve().parent.parent / "services" / "auth"
if str(_AUTH_SVC) not in sys.path:
    sys.path.insert(0, str(_AUTH_SVC))


@pytest.fixture()
def client():
    import app as auth_app  # services/auth/app.py
    flask_app = auth_app.create_app()
    flask_app.config["TESTING"] = True
    return flask_app


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
