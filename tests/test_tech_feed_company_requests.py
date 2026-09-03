"""Company requests: validation, auth, rate limits, no fetch on submit."""
from __future__ import annotations

import pytest

from _vg_support import current_db, load_service_app
from services import tech_feed_company_requests as reqs

USER = "seeker@example.com"
OTHER = "other@example.com"


@pytest.fixture()
def app():
    return load_service_app("jobs")


def _client(flask_app, identity):
    from flask_jwt_extended import create_access_token
    with flask_app.app_context():
        tok = create_access_token(identity=identity)
    c = flask_app.test_client()

    def call(method, path, json=None):
        return getattr(c, method)(
            path, headers={"Authorization": f"Bearer {tok}"}, json=json)
    return call


@pytest.fixture(autouse=True)
def clean():
    db = current_db()
    db[reqs.REQUESTS].delete_many({})
    db[reqs.AUDIT].delete_many({})
    yield
    db[reqs.REQUESTS].delete_many({})
    db[reqs.AUDIT].delete_many({})


class TestSanitizeCareerUrl:
    def test_empty_is_ok(self):
        url, err = reqs.sanitize_career_url("")
        assert url is None and err is None

    def test_https_ok(self):
        url, err = reqs.sanitize_career_url("https://example.com/careers")
        assert err is None
        assert url.startswith("https://example.com")

    def test_rejects_javascript(self):
        _, err = reqs.sanitize_career_url("javascript:alert(1)")
        assert err == "scheme_not_allowed:javascript"

    def test_rejects_credentials(self):
        _, err = reqs.sanitize_career_url("https://user:pass@example.com/c")
        assert err == "credentials_in_url"

    def test_rejects_private_ip(self):
        _, err = reqs.sanitize_career_url("http://127.0.0.1/careers")
        assert err == "private_ip_literal"

    def test_rejects_localhost(self):
        _, err = reqs.sanitize_career_url("https://localhost/careers")
        assert err == "localhost"

    def test_does_not_resolve_dns(self, monkeypatch):
        import socket
        def boom(*a, **k):
            raise AssertionError("DNS must not run on the request path")
        monkeypatch.setattr(socket, "getaddrinfo", boom)
        url, err = reqs.sanitize_career_url("https://jobs.ashbyhq.com/ssi")
        assert err is None
        assert url is not None


class TestSubmit:
    def test_creates_a_safe_id(self):
        out = reqs.submit(USER, company_name="Example Co",
                          career_url="https://example.com/careers")
        assert out["request_id"].startswith("creq_")
        assert out["status"] == "submitted"
        assert "user_email" not in out

    def test_same_user_resubmit_is_already_requested(self):
        reqs.submit(USER, company_name="Example Co")
        out = reqs.submit(USER, company_name="Example Co")
        assert out["status"] == "already_requested"

    def test_other_user_cannot_read(self):
        first = reqs.submit(USER, company_name="Example Co")
        assert reqs.get_for_user(OTHER, first["request_id"]) is None

    def test_name_only_collision_does_not_merge_domains(self):
        a = reqs.submit(USER, company_name="OpenEvidence",
                        career_url="https://openevidence.com/careers")
        b = reqs.submit(USER, company_name="OpenEvidence",
                        career_url="https://open-evidence.com/careers")
        assert a["request_id"] != b["request_id"]


class TestRoutes:
    def test_requires_auth(self, app):
        c = app.test_client()
        assert c.post("/api/jobs/tech-feed/company-requests",
                      json={"company_name": "X"}).status_code == 401

    def test_happy_path(self, app):
        mine = _client(app, USER)
        r = mine("post", "/api/jobs/tech-feed/company-requests",
                 {"company_name": "Acme", "career_url": "https://acme.com/jobs"})
        assert r.status_code == 200
        body = r.get_json()
        assert body["request_id"].startswith("creq_")
        assert body["status"] in ("submitted", "already_requested")

    def test_malicious_url_400(self, app):
        mine = _client(app, USER)
        r = mine("post", "/api/jobs/tech-feed/company-requests",
                 {"company_name": "Acme", "career_url": "http://169.254.169.254/"})
        assert r.status_code == 400

    def test_handler_source_does_not_fetch(self):
        from pathlib import Path
        src = (Path(__file__).resolve().parents[1]
               / "services" / "jobs" / "blueprints" / "jobs.py").read_text(
                   encoding="utf-8")
        start = src.index("def tech_feed_company_request")
        end = src.index("def tech_feed_company_request_get")
        block = src[start:end]
        assert "requests." not in block
        assert "url_is_safe" not in block
        assert "Fetcher" not in block
