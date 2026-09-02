"""Saved searches and telemetry, at the HTTP boundary.

Authorization is the load-bearing test here: a saved search is a query
somebody wrote about their own job hunt, and the only thing standing between
two accounts is that every route reads `get_jwt_identity()` and never a body
field.
"""
from __future__ import annotations

import pytest

from _vg_support import current_db, load_service_app
from services import tech_feed_saved_searches as tfss
from services import tech_feed_telemetry as tft

USER = "seeker@example.com"
OTHER = "someone.else@example.com"


@pytest.fixture()
def app():
    return load_service_app("jobs")


def _client(flask_app, identity):
    from flask_jwt_extended import create_access_token
    with flask_app.app_context():
        tok = create_access_token(identity=identity)
    c = flask_app.test_client()

    def call(method, path, json=None):
        return getattr(c, method)(path, headers={"Authorization": f"Bearer {tok}"},
                                  json=json)
    return call


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setenv("TELEMETRY_SALT", "route-test-salt")
    db = current_db()
    db[tfss.SAVED_SEARCHES].delete_many({})
    db[tft.EVENTS].delete_many({})
    yield
    db[tfss.SAVED_SEARCHES].delete_many({})
    db[tft.EVENTS].delete_many({})


FILTERS = {"query": "kubernetes", "role_families": ["cloud_platform"],
           "employer_scope": "tech_only"}


class TestSavedSearchAuthorization:

    def test_every_route_requires_a_token(self, app):
        c = app.test_client()
        assert c.get("/api/jobs/tech-feed/saved-searches").status_code == 401
        assert c.post("/api/jobs/tech-feed/saved-searches", json={}).status_code == 401
        assert c.delete("/api/jobs/tech-feed/saved-searches/x").status_code == 401
        assert c.post("/api/jobs/tech-feed/saved-searches/reconcile",
                      json={}).status_code == 401

    def test_one_account_cannot_read_anothers(self, app):
        mine = _client(app, USER)
        theirs = _client(app, OTHER)
        mine("post", "/api/jobs/tech-feed/saved-searches",
             {"id": "a", "name": "My hunt", "filters": FILTERS})
        body = theirs("get", "/api/jobs/tech-feed/saved-searches").get_json()
        assert body["searches"] == []

    def test_one_account_cannot_delete_anothers(self, app):
        mine = _client(app, USER)
        theirs = _client(app, OTHER)
        mine("post", "/api/jobs/tech-feed/saved-searches",
             {"id": "a", "name": "My hunt", "filters": FILTERS})
        theirs("delete", "/api/jobs/tech-feed/saved-searches/a")
        assert len(mine("get", "/api/jobs/tech-feed/saved-searches")
                   .get_json()["searches"]) == 1

    def test_a_body_supplied_identity_is_ignored(self, app):
        """The route reads the TOKEN. A `user_email` in the body is data."""
        mine = _client(app, USER)
        mine("post", "/api/jobs/tech-feed/saved-searches",
             {"id": "a", "name": "X", "filters": FILTERS,
              "user_email": OTHER})
        theirs = _client(app, OTHER)
        assert theirs("get", "/api/jobs/tech-feed/saved-searches"
                      ).get_json()["searches"] == []


class TestSavedSearchContract:

    def test_save_then_list(self, app):
        c = _client(app, USER)
        r = c("post", "/api/jobs/tech-feed/saved-searches",
              {"id": "tfs_1", "name": "Remote SRE", "filters": FILTERS})
        assert r.status_code == 200
        rows = c("get", "/api/jobs/tech-feed/saved-searches").get_json()["searches"]
        assert rows[0]["name"] == "Remote SRE"
        assert rows[0]["filters"]["query"] == "kubernetes"

    def test_a_full_account_returns_409_not_400(self, app):
        """A well-formed request against a full account is not bad input."""
        c = _client(app, USER)
        for i in range(tfss.MAX_PER_USER):
            c("post", "/api/jobs/tech-feed/saved-searches",
              {"id": f"c{i}", "name": f"S{i}", "filters": {"query": f"q{i}"}})
        r = c("post", "/api/jobs/tech-feed/saved-searches",
              {"id": "extra", "name": "X", "filters": FILTERS})
        assert r.status_code == 409

    def test_reconcile_pushes_local_up_and_returns_the_server_list(self, app):
        c = _client(app, USER)
        r = c("post", "/api/jobs/tech-feed/saved-searches/reconcile",
              {"searches": [{"id": "local1", "name": "Offline",
                             "filters": FILTERS}]})
        body = r.get_json()
        assert body["pushed_from_client"] == 1
        assert body["searches"][0]["id"] == "local1"


class TestTelemetryRoute:

    def test_it_requires_a_token(self, app):
        assert app.test_client().post("/api/jobs/tech-feed/telemetry",
                                      json={"events": []}).status_code == 401

    def test_a_batch_is_accepted_and_carries_no_identity(self, app):
        c = _client(app, USER)
        r = c("post", "/api/jobs/tech-feed/telemetry",
              {"events": [{"event": "search",
                           "dimensions": {"filter_keys": ["countries"]}}]})
        assert r.status_code == 200
        assert r.get_json()["accepted"] == 1
        raw = repr(list(current_db()[tft.EVENTS].find({})))
        assert USER not in raw and "@" not in raw

    def test_a_malformed_batch_never_fails_the_request(self, app):
        c = _client(app, USER)
        r = c("post", "/api/jobs/tech-feed/telemetry",
              {"events": [{"event": "not-a-real-event"}]})
        assert r.status_code == 200
        assert r.get_json() == {"accepted": 0, "rejected": 1}

    def test_a_body_that_is_not_an_object_is_survived(self, app):
        c = _client(app, USER)
        assert c("post", "/api/jobs/tech-feed/telemetry", None).status_code in (200, 204)
