"""Gmail tracker additive API contract (offline)."""
import pytest

from _vg_support import load_service_app
from services import gmail_service as gs

USER = "gmail-route@example.test"


@pytest.fixture
def client():
    app = load_service_app("resume")
    from flask_jwt_extended import create_access_token
    with app.app_context():
        token = create_access_token(identity=USER)
    return app.test_client(), {"Authorization": f"Bearer {token}"}


def test_apply_passes_selected_record_to_owner_scoped_service(client, monkeypatch):
    seen = []
    def apply(user, suggestion_id, record_id=None):
        seen.append((user, suggestion_id, record_id))
        return {"suggestion_id": suggestion_id, "record_id": record_id, "applied": True}
    monkeypatch.setattr(gs, "apply_suggestion", apply)
    response = client[0].post("/api/resume/gmail/suggestions/s1/apply",
                              headers=client[1], json={"record_id": "rec-2"})
    assert response.status_code == 200
    assert seen == [(USER, "s1", "rec-2")]


def test_selection_error_is_400_not_500(client, monkeypatch):
    def apply(*_, **__):
        raise ValueError("Choose the application")
    monkeypatch.setattr(gs, "apply_suggestion", apply)
    response = client[0].post("/api/resume/gmail/suggestions/s1/apply", headers=client[1])
    assert response.status_code == 400


@pytest.mark.parametrize("body", [{"record_id": {"$ne": ""}}, ["rec-2"], {"record_id": 4}])
def test_invalid_selection_never_reaches_service(client, monkeypatch, body):
    def apply(*_, **__):
        pytest.fail("Invalid selection reached service")
    monkeypatch.setattr(gs, "apply_suggestion", apply)
    response = client[0].post("/api/resume/gmail/suggestions/s1/apply", headers=client[1], json=body)
    assert response.status_code == 400


def test_status_exposes_continuation_without_cursor_token(client, monkeypatch):
    monkeypatch.setattr(gs, "gmail_configured", lambda: True)
    monkeypatch.setattr(gs, "gmail_oauth_enabled", lambda: True)
    monkeypatch.setattr(gs, "get_connection", lambda _: {
        "gmail_address": USER, "scan_next_page_token": "private-cursor",
        "last_sync_summary": {"messages_scanned": 250, "has_more": True}})
    response = client[0].get("/api/resume/gmail/status", headers=client[1])
    data = response.get_json()
    assert data["has_more"] is True
    assert data["last_sync_summary"]["messages_scanned"] == 250
    assert "private-cursor" not in response.get_data(as_text=True)


def test_status_count_matches_validated_review_list(client, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(gs, "gmail_configured", lambda: True)
    monkeypatch.setattr(gs, "gmail_oauth_enabled", lambda: True)
    monkeypatch.setattr(gs, "get_connection", lambda _: {"gmail_address": USER})
    monkeypatch.setattr(gs, "_suggestions", lambda: SimpleNamespace(count_documents=lambda _: 7))
    monkeypatch.setattr(gs, "list_suggestions", lambda _: [{"suggestion_id": "good-1"}, {"suggestion_id": "good-2"}])
    response = client[0].get("/api/resume/gmail/status", headers=client[1])
    assert response.get_json()["pending_suggestions"] == 2
