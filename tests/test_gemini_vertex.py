"""Gemini on Vertex AI: backend selection, service-account credentials,
thinking settings and output ceilings. Offline — no request leaves the box."""
import base64
import json

import pytest

from services.llm_providers.gemini import GeminiProvider


_ENV = ("GEMINI_BACKEND", "GOOGLE_GENAI_USE_VERTEXAI", "VERTEX_PROJECT",
        "VERTEX_LOCATION", "VERTEX_CREDENTIALS_JSON", "GEMINI_THINKING_LEVEL",
        "GEMINI_THINKING_BUDGET", "GOOGLE_APPLICATION_CREDENTIALS")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in _ENV:
        monkeypatch.delenv(key, raising=False)


def _service_account(project="aspirely-ai"):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    return {
        "type": "service_account",
        "project_id": project,
        "private_key_id": "0" * 40,
        "private_key": pem,
        "client_email": f"jobpilot-ai@{project}.iam.gserviceaccount.com",
        "client_id": "1",
        "token_uri": "https://oauth2.googleapis.com/token",
    }


def test_backend_defaults_to_ai_studio():
    assert GeminiProvider.backend() == "studio"


@pytest.mark.parametrize("env,value", [
    ("GEMINI_BACKEND", "vertex"), ("GEMINI_BACKEND", " Vertex "),
    ("GOOGLE_GENAI_USE_VERTEXAI", "true"),
])
def test_backend_selects_vertex(monkeypatch, env, value):
    monkeypatch.setenv(env, value)
    assert GeminiProvider.backend() == "vertex"


def test_a_misspelled_backend_fails_loudly(monkeypatch):
    monkeypatch.setenv("GEMINI_BACKEND", "vertx")
    with pytest.raises(RuntimeError, match="GEMINI_BACKEND"):
        GeminiProvider.backend()


def test_service_account_key_is_read_raw_or_base64():
    info = {"type": "service_account", "project_id": "p"}
    raw = json.dumps(info)
    assert GeminiProvider._service_account_info(raw) == info
    assert GeminiProvider._service_account_info(base64.b64encode(raw.encode()).decode()) == info


@pytest.mark.parametrize("raw", [
    "not-a-key", json.dumps({"type": "authorized_user"}), "{broken",
])
def test_anything_but_a_service_account_key_is_refused(raw):
    with pytest.raises(RuntimeError, match="VERTEX_CREDENTIALS_JSON"):
        GeminiProvider._service_account_info(raw)


def test_vertex_client_uses_the_keys_project_and_the_us_location(monkeypatch):
    sa = json.dumps(_service_account("aspirely-ai"))
    monkeypatch.setenv("GEMINI_BACKEND", "vertex")
    monkeypatch.setenv("VERTEX_CREDENTIALS_JSON", base64.b64encode(sa.encode()).decode())
    # An AI Studio key left in stack.env must not pull the client off Vertex.
    monkeypatch.setenv("GEMINI_API_KEY", "studio-key")

    api = GeminiProvider().lazy_client()._api_client
    assert api.vertexai is True
    assert api.project == "aspirely-ai"
    assert api.location == "us"
    assert api._credentials.service_account_email == "jobpilot-ai@aspirely-ai.iam.gserviceaccount.com"


def test_vertex_project_and_location_can_be_overridden(monkeypatch):
    monkeypatch.setenv("GEMINI_BACKEND", "vertex")
    monkeypatch.setenv("VERTEX_CREDENTIALS_JSON", json.dumps(_service_account("key-proj")))
    monkeypatch.setenv("VERTEX_PROJECT", "billing-proj")
    monkeypatch.setenv("VERTEX_LOCATION", "us")
    api = GeminiProvider().lazy_client()._api_client
    assert (api.project, api.location) == ("billing-proj", "us")


def test_vertex_without_a_project_says_what_to_set(monkeypatch):
    monkeypatch.setenv("GEMINI_BACKEND", "vertex")
    with pytest.raises(RuntimeError, match="VERTEX_PROJECT"):
        GeminiProvider().lazy_client()


def test_missing_google_credentials_are_not_retried():
    from google.auth.exceptions import DefaultCredentialsError, RefreshError
    assert GeminiProvider._is_retryable(DefaultCredentialsError("none")) is False
    assert GeminiProvider._is_retryable(RefreshError("invalid_grant")) is False


@pytest.mark.parametrize("model,level,budget", [
    ("gemini-4-argon", "LOW", None),
    ("gemini-3.8-flash", "LOW", None),
    ("gemini-2.5-flash", None, 0),
])
def test_thinking_setting_matches_what_each_generation_accepts(model, level, budget):
    thinking = GeminiProvider._thinking_config(model)
    assert thinking.thinking_budget == budget
    assert (thinking.thinking_level.value if thinking.thinking_level else None) == level


def test_gemini_2_pro_keeps_its_default_thinking():
    assert GeminiProvider._thinking_config("gemini-2.5-pro") is None


def test_thinking_level_is_configurable(monkeypatch):
    monkeypatch.setenv("GEMINI_THINKING_LEVEL", "high")
    assert GeminiProvider._thinking_config("gemini-4-argon").thinking_level.value == "HIGH"


def test_output_is_clamped_to_what_each_model_answers_in_one_segment():
    p = GeminiProvider()
    assert p._clamp_max_tokens(40000, "gemini-4-argon") == 32768
    assert p._clamp_max_tokens(24000, "gemini-4-argon") == 24000
    assert p._clamp_max_tokens(60000, "gemini-3.8-flash") == 60000


def test_tool_calling_rounds_bound_thinking_too():
    from google.genai import types

    class _Models:
        def __init__(self):
            self.configs = []

        def generate_content(self, model, contents, config):
            self.configs.append(config)
            content = types.Content(role="model", parts=[types.Part.from_text(text="You saved 7.")])
            return type("R", (), {"text": "You saved 7.",
                                  "candidates": [types.Candidate(content=content)]})()

    provider = GeminiProvider()
    models = _Models()
    provider._client = type("C", (), {"models": models})()
    reply, _ = provider.tool_call(
        system_text="sys", user_message="how many?",
        tools=[{"name": "n", "description": "d", "parameters": {"type": "OBJECT", "properties": {}}}],
        execute_tool=lambda name, args: ({}, "x"),
    )
    assert reply == "You saved 7."
    assert models.configs[0].thinking_config.thinking_level.value == "LOW"
