"""Route-level regression guard for the Authored-Résumé endpoint (AUDIT-P1-001).

The offline eval suite calls ``build_authored_resume()`` directly and therefore
never exercised the HTTP route — which used to raise ``TypeError`` (a missing
``user_email`` arg to ``_rate_limited``) and return **HTTP 500 on every call**
before consent/entitlement/LLM. This test drives the real Flask route so that
regression can never return silently.

Runs fully offline (mongomock via conftest/_vg_support; no Bedrock, no live
Mongo). It asserts the route does NOT 500 and reaches the consent gate.
"""
import sys

import pytest

from _vg_support import load_service_app


@pytest.fixture()
def client():
    # Load under a unique module name so it can't collide with another service's
    # app.py when the whole suite runs in one pytest process.
    return load_service_app("resume")


def _token(flask_app, identity="regression@aspirely.me"):
    from flask_jwt_extended import create_access_token
    with flask_app.app_context():
        return create_access_token(identity=identity)


def test_authored_route_does_not_500(client):
    """The flagship route must reach its business logic, not TypeError-500."""
    token = _token(client)
    resp = client.test_client().post(
        "/api/resume/voice-graph/resume/authored",
        headers={"Authorization": f"Bearer {token}"},
        json={"style_dial": "balanced"},
    )
    # Without prior consent the correct answer is 403 consent_required — the
    # point is only that we get PAST the rate-limit call without a 500.
    assert resp.status_code != 500, resp.get_data(as_text=True)
    assert resp.status_code == 403
    assert resp.get_json().get("error") == "consent_required"


if __name__ == "__main__":  # allow `python tests/test_voicegraph_route.py`
    sys.exit(pytest.main([__file__, "-q"]))
