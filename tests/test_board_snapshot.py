"""The For-you board is precomputed every 30 minutes and served as a read.

What has to hold:

* a snapshot is only ever served for the EXACT request that built it;
* "fresh" (no scan needed) needs all three tabs, inside the window, and on the
  viewer's current calendar day - a board built at 23:50 must not call
  yesterday's postings "today";
* a live For-you scan stores its result, and other surfaces' scans do not;
* the refresh rebuilds only due boards of recently active users, oldest
  request first, and reports what its budget did not reach;
* production uses the bounded fan-out envelope without Lambda.
"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from _vg_support import current_db, load_service_app
from services import board_snapshot as bs
from services import resume_service as rs

USER = "seeker@example.com"
OTHER = "other@example.com"
NOW = datetime(2026, 11, 2, 18, 0, tzinfo=timezone.utc)   # 13:00 US Eastern
EASTERN = -300


def _payload(**over):
    base = {"titles": ["Backend Engineer", "Platform Engineer"], "location": "",
            "remote_only": False, "us_only": True, "tz_offset_minutes": EASTERN,
            "user_email": USER, "board": bs.BOARD_FOR_YOU}
    base.update(over)
    return base


def _result(n=3):
    return {"ok": True, "jobs": [{"job_id": f"j{i}", "title": "Backend Engineer",
                                  "match_score": 80 - i} for i in range(n)],
            "total": n}


@pytest.fixture(autouse=True)
def clean():
    db = current_db()
    for name in (bs.SNAPSHOTS, bs.REQUESTS, "resume_jobs"):
        db[name].delete_many({})
    yield
    for name in (bs.SNAPSHOTS, bs.REQUESTS, "resume_jobs"):
        db[name].delete_many({})


def _store(payload, keys=("workday", "careers", "mncs"), built_at=NOW):
    sig = bs.signature(bs.params_of(payload))
    for key in keys:
        bs.save(payload["user_email"], key, sig, _result(), built_by="test")
    current_db()[bs.SNAPSHOTS].update_many({"user_email": payload["user_email"]},
                                           {"$set": {"built_at": built_at}})


class TestSignature:

    def test_case_and_whitespace_do_not_make_a_new_request(self):
        a = bs.signature(bs.params_of(_payload(titles=["Backend Engineer"])))
        b = bs.signature(bs.params_of(_payload(titles=["  backend engineer "])))
        assert a == b

    def test_title_order_scope_and_zone_do(self):
        base = bs.signature(bs.params_of(_payload()))
        assert base != bs.signature(bs.params_of(
            _payload(titles=["Platform Engineer", "Backend Engineer"])))
        assert base != bs.signature(bs.params_of(_payload(remote_only=True)))
        assert base != bs.signature(bs.params_of(_payload(location="Texas")))
        assert base != bs.signature(bs.params_of(_payload(tz_offset_minutes=-480)))

    def test_scan_only_fields_are_not_part_of_the_answer(self):
        assert bs.signature(bs.params_of(_payload())) == bs.signature(
            bs.params_of(_payload(force_refresh=True, user_email=OTHER)))


class TestLoad:

    def test_nothing_stored_is_unavailable(self):
        out = bs.load(_payload(), now=NOW)
        assert out["available"] is False and out["fresh"] is False
        assert out["sources"] == {"workday": None, "careers": None, "mncs": None}

    def test_all_three_recent_tabs_are_fresh(self):
        _store(_payload(), built_at=NOW - timedelta(minutes=20))
        out = bs.load(_payload(), now=NOW)
        assert out["available"] and out["fresh"]
        assert out["age_seconds"] == 20 * 60
        assert len(out["sources"]["workday"]["jobs"]) == 3

    def test_a_missing_tab_is_shown_but_not_fresh(self):
        _store(_payload(), keys=("workday", "careers"), built_at=NOW)
        out = bs.load(_payload(), now=NOW)
        assert out["available"] and not out["fresh"]
        assert out["sources"]["mncs"] is None

    def test_an_old_board_is_shown_but_not_fresh(self):
        _store(_payload(), built_at=NOW - timedelta(seconds=bs.FRESH_SECONDS + 60))
        out = bs.load(_payload(), now=NOW)
        assert out["available"] and not out["fresh"]

    def test_a_day_old_board_is_not_shown(self):
        _store(_payload(), built_at=NOW - timedelta(seconds=bs.MAX_SERVE_SECONDS + 60))
        assert bs.load(_payload(), now=NOW)["available"] is False

    def test_a_board_from_before_the_viewers_midnight_is_not_fresh(self):
        # Built 23:50 Eastern, read 00:05 Eastern: 15 minutes old, other day.
        built = datetime(2026, 11, 3, 4, 50, tzinfo=timezone.utc)
        _store(_payload(), built_at=built)
        out = bs.load(_payload(), now=built + timedelta(minutes=15))
        assert out["available"] and not out["fresh"]

    def test_another_request_never_sees_this_board(self):
        _store(_payload())
        assert bs.load(_payload(titles=["Data Engineer"]), now=NOW)["available"] is False
        assert bs.load(_payload(user_email=OTHER), now=NOW)["available"] is False


class TestScanSavesTheBoard:

    def test_a_for_you_scan_is_stored(self):
        assert bs.save_from_scan(_payload(), "workday_jobs", _result()) is True
        doc = current_db()[bs.SNAPSHOTS].find_one({"_id": f"{USER}|workday"})
        assert doc["built_by"] == "scan" and doc["sig"] == bs.signature(bs.params_of(_payload()))

    def test_other_surfaces_and_failures_are_not(self):
        other = _payload()
        other.pop("board")
        assert bs.save_from_scan(other, "workday_jobs", _result()) is False
        assert bs.save_from_scan(_payload(), "workday_jobs", {"ok": False}) is False
        assert bs.save_from_scan(_payload(), "daily_pipeline", _result()) is False
        assert current_db()[bs.SNAPSHOTS].count_documents({}) == 0

    def test_process_job_completes_then_stores(self, monkeypatch):
        monkeypatch.setattr(rs, "run_board_source", lambda jt, p, partial_cb=None: _result(5))
        svc = rs.get_resume_service()
        job_id = svc.create_job("career_pages", _payload(), user_email=USER)
        rs._process_job(job_id, "career_pages", _payload())
        assert svc.get_job(job_id, USER)["status"] == "completed"
        doc = current_db()[bs.SNAPSHOTS].find_one({"_id": f"{USER}|careers"})
        assert len(doc["result"]["jobs"]) == 5


class TestRefresh:

    def _request(self, email, requested_at, **over):
        payload = _payload(user_email=email, **over)
        params = bs.params_of(payload)
        current_db()[bs.REQUESTS].insert_one({
            "_id": email, "params": params, "sig": bs.signature(params),
            "requested_at": requested_at})
        return payload

    def test_rebuilds_due_boards_only(self, monkeypatch):
        calls = []

        def fake(job_type, payload, partial_cb=None):
            calls.append((payload["user_email"], job_type, payload["board"],
                          payload["force_refresh"]))
            return _result()
        monkeypatch.setattr(rs, "run_board_source", fake)
        self._request(USER, NOW - timedelta(hours=1))
        fresh = self._request(OTHER, NOW - timedelta(hours=2))
        _store(fresh, built_at=NOW - timedelta(minutes=5))

        out = bs.refresh_due(now=NOW)

        assert out == {"due": 1, "built": 1, "behind": 0, "seconds": out["seconds"]}
        assert sorted(c[1] for c in calls) == ["career_pages", "mnc_jobs", "workday_jobs"]
        assert all(c[0] == USER and c[2] == bs.BOARD_FOR_YOU and c[3] is False for c in calls)
        assert bs.load(_payload(), now=datetime.now(timezone.utc))["fresh"] is True

    def test_inactive_users_are_left_alone(self, monkeypatch):
        monkeypatch.setattr(rs, "run_board_source", lambda *a, **k: pytest.fail("built"))
        self._request(USER, NOW - timedelta(days=bs.ACTIVE_DAYS + 1))
        assert bs.refresh_due(now=NOW)["due"] == 0

    def test_a_failing_tab_does_not_stop_the_others(self, monkeypatch):
        def fake(job_type, payload, partial_cb=None):
            if job_type == "mnc_jobs":
                raise TimeoutError("crawl")
            return _result()
        monkeypatch.setattr(rs, "run_board_source", fake)
        self._request(USER, NOW)
        bs.refresh_due(now=NOW)
        assert {d["source"] for d in current_db()[bs.SNAPSHOTS].find()} == {"workday", "careers"}

    def test_the_budget_reports_what_it_did_not_reach(self, monkeypatch):
        monkeypatch.setattr(rs, "run_board_source", lambda *a, **k: _result())
        self._request(USER, NOW)
        self._request(OTHER, NOW - timedelta(minutes=1))
        out = bs.refresh_due(budget_seconds=0, now=NOW)
        assert out["due"] == 2 and out["built"] == 1 and out["behind"] == 1

    def test_the_scheduler_runs_it_every_ten_minutes(self):
        import importlib.util
        path = Path(__file__).resolve().parents[1] / "services" / "scheduler" / "runner.py"
        spec = importlib.util.spec_from_file_location("scheduler_runner_board", path)
        runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner)
        assert runner.EVERY["board_refresh"] == 10
        at = datetime(2026, 11, 2, 18, 27, tzinfo=timezone.utc)
        assert runner.latest_slot("board_refresh", at) == at.replace(minute=20)


class TestRoutes:

    @pytest.fixture()
    def call(self):
        app = load_service_app("jobs")
        from flask_jwt_extended import create_access_token
        with app.app_context():
            tok = create_access_token(identity=USER)
        client = app.test_client()

        def _call(path, body):
            return client.post(path, json=body, headers={"Authorization": f"Bearer {tok}"})
        return _call

    def test_snapshot_needs_a_token(self):
        app = load_service_app("jobs")
        assert app.test_client().post("/api/jobs/board/snapshot", json={}).status_code == 401

    def test_snapshot_read_records_the_open_and_serves_the_board(self, call):
        payload = _payload()
        _store(payload, built_at=datetime.now(timezone.utc))
        body = {k: v for k, v in payload.items() if k not in ("user_email", "board")}
        resp = call("/api/jobs/board/snapshot", body)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["available"] is True and data["fresh"] is True
        assert current_db()[bs.REQUESTS].find_one({"_id": USER})["params"]["titles"] == payload["titles"]

    def test_identity_comes_from_the_token_not_the_body(self, call):
        _store(_payload(user_email=OTHER), built_at=datetime.now(timezone.utc))
        body = {k: v for k, v in _payload().items() if k != "board"}
        body["user_email"] = OTHER
        assert call("/api/jobs/board/snapshot", body).get_json()["available"] is False

    def test_a_for_you_scan_records_its_request(self, call, monkeypatch):
        monkeypatch.setattr(rs.ResumeService, "invoke_async", staticmethod(lambda *a, **k: None))
        body = {k: v for k, v in _payload().items() if k != "user_email"}
        assert call("/api/jobs/workday/search", body).status_code == 202
        job = current_db()["resume_jobs"].find_one({"job_type": "workday_jobs"})
        assert job["payload"]["board"] == bs.BOARD_FOR_YOU
        assert current_db()[bs.REQUESTS].count_documents({"_id": USER}) == 1

    def test_a_catalog_tab_scan_does_not(self, call, monkeypatch):
        monkeypatch.setattr(rs.ResumeService, "invoke_async", staticmethod(lambda *a, **k: None))
        body = {k: v for k, v in _payload().items() if k not in ("user_email", "board")}
        assert call("/api/jobs/career-pages/search", body).status_code == 202
        assert "board" not in current_db()["resume_jobs"].find_one()["payload"]
        assert current_db()[bs.REQUESTS].count_documents({}) == 0


@pytest.mark.parametrize("environment,expected", [
    ("prod", "210 32 210 32 180 16"),
    ("", "600 96 480 64 420 32"),
])
def test_production_uses_the_bounded_fan_out_without_lambda(environment, expected):
    """The box has no AWS_LAMBDA_FUNCTION_NAME. Production must still get the
    server envelope, not the laptop one. Checked in a fresh interpreter because
    the bounds are read at import."""
    root = Path(__file__).resolve().parents[1]
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("WORKDAY_", "CAREER_PAGES_", "MNC_", "AWS_LAMBDA"))}
    env.update({"ENVIRONMENT": environment, "PYTHONPATH": str(root / "shared" / "python")})
    code = ("from services import workday_jobs_service as w, career_pages_service as c, "
            "mnc_jobs_service as m; print(w.OVERALL_TIMEOUT, w.MAX_WORKERS, c.OVERALL_TIMEOUT, "
            "c.MAX_WORKERS, m.OVERALL_TIMEOUT, m.MAX_WORKERS)")
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True,
                         text=True, timeout=120, cwd=str(root))
    assert out.returncode == 0, out.stderr[-500:]
    assert out.stdout.strip().splitlines()[-1] == expected


class TestNewSinceLastVisit:
    """`seen_through` = the end of the user's PREVIOUS board visit, so the
    client can mark what the crawler found since then as new (2026-11-05)."""

    def _open(self, monkeypatch, at):
        monkeypatch.setattr(bs, "_now", lambda: at)
        bs.record_request(_payload())

    def test_a_first_visit_has_nothing_to_compare_against(self, monkeypatch):
        self._open(monkeypatch, NOW)
        assert bs.load(_payload(), now=NOW)["seen_through"] is None

    def test_activity_inside_one_visit_does_not_move_it(self, monkeypatch):
        self._open(monkeypatch, NOW)
        self._open(monkeypatch, NOW + timedelta(minutes=30))   # the page's auto-sync
        self._open(monkeypatch, NOW + timedelta(minutes=60))
        assert bs.seen_through(USER) is None

    def test_a_return_after_the_gap_marks_the_previous_visit_seen(self, monkeypatch):
        self._open(monkeypatch, NOW)
        self._open(monkeypatch, NOW + timedelta(minutes=40))
        back = NOW + timedelta(minutes=40 + bs.VISIT_GAP_MINUTES + 1)
        self._open(monkeypatch, back)
        seen = bs.load(_payload(), now=back)["seen_through"]
        assert seen == (NOW + timedelta(minutes=40)).isoformat()
        # ...and stays put for the rest of this visit.
        self._open(monkeypatch, back + timedelta(minutes=30))
        assert bs.seen_through(USER) == seen

    def test_unknown_user_is_none(self):
        assert bs.seen_through("") is None
        assert bs.seen_through("nobody@example.com") is None
