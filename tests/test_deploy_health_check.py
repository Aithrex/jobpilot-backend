"""The deployment check must distinguish the resume API from the SPA fallback."""
import importlib.util
import json
from email.message import Message
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SPEC = {
    "page": "letter",
    "page_width_mm": 215.9,
    "page_height_mm": 279.4,
    "section_order": ["summary", "experience"],
    "headings": {"summary": "SUMMARY", "experience": "EXPERIENCE"},
}


def test_workflow_checks_resume_json_and_does_not_accept_failed_health():
    workflow = yaml.safe_load((ROOT / ".github/workflows/deploy.yml").read_text())
    job = workflow["jobs"]["verify"]
    step = next(s for s in job["steps"] if "run" in s)
    assert not step.get("continue-on-error", False)
    assert "exit 0" not in step["run"]
    assert "resume" in job["name"].lower()
    assert "detect-changes" in job["needs"]
    assert "contains(fromJSON(needs.detect-changes.outputs.services), 'resume')" in job["if"]
    assert "python scripts/check_deployed_api.py" in step["run"]
    assert any(s.get("uses", "").startswith("actions/checkout@") for s in job["steps"])


@pytest.fixture
def checker():
    path = ROOT / "scripts/check_deployed_api.py"
    assert path.is_file(), "A strict JSON checker is required instead of curl status alone"
    spec = importlib.util.spec_from_file_location("deployed_api_check", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Response(BytesIO):
    def __init__(self, body, content_type="application/json", status=200):
        super().__init__(body)
        self.status = status
        self.headers = Message()
        self.headers["Content-Type"] = content_type


def run_responses(checker, monkeypatch, responses):
    calls, pauses = [], []
    responses = iter(responses)

    def open_response(request, timeout):
        calls.append((request, timeout))
        result = next(responses)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(checker, "urlopen", open_response)
    monkeypatch.setattr(checker.time, "sleep", pauses.append)
    return checker.main(), calls, pauses


def test_public_layout_json_passes_without_authentication(checker, monkeypatch):
    result, calls, pauses = run_responses(
        checker, monkeypatch,
        [Response(json.dumps(SPEC).encode(), "application/json; charset=utf-8")],
    )
    assert result == 0
    assert len(calls) == 1
    request, timeout = calls[0]
    assert request.full_url == "https://aspirely.me/api/resume/layout-spec"
    assert request.get_method() == "GET"
    assert not request.has_header("Authorization")
    assert 0 < timeout <= 15
    assert pauses == []


@pytest.mark.parametrize("body,content_type", [
    (b"<html><title>Aspirely</title></html>", "text/html"),
    (b"<html><title>Aspirely</title></html>", "application/json"),
    (b'{"error":"unavailable"}', "application/json"),
    (b"[]", "application/json"),
    (json.dumps({**SPEC, "page_width_mm": 0}).encode(), "application/json"),
    (json.dumps({**SPEC, "page_height_mm": True}).encode(), "application/json"),
    (json.dumps({**SPEC, "section_order": []}).encode(), "application/json"),
    (json.dumps({**SPEC, "headings": {}}).encode(), "application/json"),
])
def test_html_or_wrong_json_never_reports_success(checker, monkeypatch, capsys, body, content_type):
    result, calls, pauses = run_responses(
        checker, monkeypatch, [Response(body, content_type) for _ in range(3)],
    )
    assert result == 1
    assert len(calls) == 3
    assert pauses == [5, 5]
    assert "resume API" in capsys.readouterr().err


def test_transient_failure_is_retried_before_valid_json(checker, monkeypatch):
    failure = HTTPError("https://aspirely.me/api/resume/layout-spec", 503, "Unavailable", {}, None)
    result, calls, pauses = run_responses(
        checker, monkeypatch, [failure, Response(json.dumps(SPEC).encode())],
    )
    assert result == 0
    assert len(calls) == 2
    assert pauses == [5]


def test_timeout_exhaustion_fails_with_bounded_retries(checker, monkeypatch):
    result, calls, pauses = run_responses(
        checker, monkeypatch, [TimeoutError("timed out") for _ in range(3)],
    )
    assert result == 1
    assert len(calls) == 3
    assert pauses == [5, 5]
