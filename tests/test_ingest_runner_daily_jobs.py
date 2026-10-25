"""The EC2 runner's daily jobs must leave evidence in MongoDB.

From 2026-09-02 to 2026-09-24 the runner ran with `--no-maintenance` (a systemd
unit no deploy ever replaced) and its coverage job failed ModuleNotFoundError
every morning. Both facts lived only in journald on a box nobody reads; every
deploy was green. These tests pin the evidence the runner now writes, and the
watchdog checks that turn that evidence into a failed run.
"""
from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import services.ingest_runner.runner as R

NOW = datetime.now(timezone.utc).replace(microsecond=0)
_OWN = ("ingest_runner_daily_runs", "ingest_runner_heartbeat",
        "coverage_snapshots", "coverage_snapshot_runs")


@pytest.fixture(autouse=True)
def _isolated(mock_db):
    for name in _OWN:
        mock_db[name].delete_many({})
    yield


@pytest.fixture()
def runner():
    r = R.Runner(["icims"], maintenance=True)
    yield r
    r.pool.shutdown(wait=False)
    r.discovery_pool.shutdown(wait=False)


class TestTheRunnerRecordsItsDailyJobs:

    def test_a_successful_job_records_start_finish_and_a_summary(self, runner, mock_db):
        out = runner._daily("maintenance", lambda: {
            "expired": 3, "purged": 2, "registry": {"big": "dict"}})()
        row = mock_db.ingest_runner_daily_runs.find_one({"job": "maintenance"})
        assert out["expired"] == 3
        assert row["ok"] is True and row["finished_at"] and row["started_at"]
        assert row["result"] == {"expired": 3, "purged": 2}   # scalars only
        assert row["runner_id"] == runner.runner_id

    def test_a_raising_job_records_the_error_and_still_raises(self, runner, mock_db):
        def boom():
            raise ModuleNotFoundError("No module named 'employer_coverage_report'")
        with pytest.raises(ModuleNotFoundError):
            runner._daily("maintenance", boom)()
        row = mock_db.ingest_runner_daily_runs.find_one({"job": "maintenance"})
        assert row["ok"] is False
        assert row["error"] == "ModuleNotFoundError"
        assert "employer_coverage_report" in row["message"]

    def test_a_job_that_reports_its_own_failure_is_recorded_as_failed(self, runner, mock_db):
        """The coverage job catches its own exceptions and RETURNS ok=False.
        Treating a returned dict as success is how three weeks of failures read
        as a working job."""
        runner._daily("coverage_snapshot", lambda: {
            "status": "failed", "ok": False, "error": "ModuleNotFoundError"})()
        row = mock_db.ingest_runner_daily_runs.find_one({"job": "coverage_snapshot"})
        assert row["ok"] is False and row["error"] == "ModuleNotFoundError"

    def test_a_recording_failure_never_stops_the_job(self, runner, monkeypatch):
        import utils.db_connect as dbc

        def broken(self):
            raise RuntimeError("mongo down")
        monkeypatch.setattr(dbc.DBConnect, "get_db", broken)
        assert runner._daily("maintenance", lambda: {"expired": 1})() == {"expired": 1}

    def test_the_heartbeat_says_whether_maintenance_is_enabled(self, mock_db):
        r = R.Runner(["icims"], maintenance=False)
        try:
            r.heartbeat()
        finally:
            r.pool.shutdown(wait=False)
            r.discovery_pool.shutdown(wait=False)
        hb = mock_db.ingest_runner_heartbeat.find_one({"runner_id": r.runner_id})
        assert hb["maintenance"] is False
        assert hb["started_at"]

    def test_the_heartbeat_says_which_build_is_running(self, mock_db, tmp_path, monkeypatch):
        """A deploy is confirmed by the box reporting the SHA it was handed,
        not by the installer exiting 0 - the box ran a 20-hour-old build once
        while every deploy was green."""
        (tmp_path / "REVISION").write_text("f5d672e0abc\n")
        monkeypatch.setattr(R, "REVISION_FILE", tmp_path / "REVISION")
        r = R.Runner(["icims"], maintenance=True)
        try:
            r.heartbeat()
        finally:
            r.pool.shutdown(wait=False)
            r.discovery_pool.shutdown(wait=False)
        hb = mock_db.ingest_runner_heartbeat.find_one({"runner_id": r.runner_id})
        assert hb["revision"] == "f5d672e0abc"

    def test_a_missing_revision_file_reads_unknown(self, tmp_path, monkeypatch):
        monkeypatch.setattr(R, "REVISION_FILE", tmp_path / "nope")
        assert R.read_revision() == "unknown"

    def test_maintenance_is_scheduled_through_the_recorder(self, runner, mock_db, monkeypatch):
        from services import job_ingest_worker as worker
        monkeypatch.setattr(worker, "run_maintenance",
                            lambda **kw: {"expired": 0, "purged": 0})
        runner.run_maintenance()
        runner.pool.shutdown(wait=True)
        assert mock_db.ingest_runner_daily_runs.find_one(
            {"job": "maintenance", "ok": True})


    def test_maintenance_runs_the_unseen_posting_backstop(self, runner, mock_db, monkeypatch):
        """The 72h backstop only runs when a verify limit is passed, and the
        runner never passed one: 65,290 of 335,137 active postings had not
        been seen in over 72 hours on 2026-10-25."""
        from services import job_ingest_worker as worker
        seen = {}
        monkeypatch.setattr(worker, "run_maintenance",
                            lambda **kw: seen.update(kw) or {"expired": 0, "purged": 0})
        runner.run_maintenance()
        runner.pool.shutdown(wait=True)
        assert seen.get("verify_limit", 0) > 0


def _watchdog():
    path = Path(__file__).resolve().parents[1] / "scripts" / "retention_watchdog.py"
    spec = importlib.util.spec_from_file_location("retention_watchdog", path)
    mod = importlib.util.module_from_spec(spec)
    # The scripts call load_dotenv() at import. Under test that would pull the
    # developer's real .env - JWT_SECRET_KEY, MONGO_URI - into the process for
    # every test that runs afterwards. CI has no .env; a laptop does.
    import dotenv
    real = dotenv.load_dotenv
    dotenv.load_dotenv = lambda *a, **k: False
    try:
        spec.loader.exec_module(mod)
    finally:
        dotenv.load_dotenv = real
    return mod


def _heartbeat(mock_db, *, maintenance=True, started=NOW - timedelta(days=3),
               beat=NOW - timedelta(minutes=1)):
    mock_db.ingest_runner_heartbeat.insert_one({
        "runner_id": "box:1:abc", "maintenance": maintenance,
        "started_at": started, "updated_at": beat})


def _healthy(mock_db):
    _heartbeat(mock_db)
    mock_db.ingest_runner_daily_runs.insert_one({
        "job": "maintenance", "started_at": NOW - timedelta(hours=5),
        "finished_at": NOW - timedelta(hours=5), "ok": True})
    mock_db.coverage_snapshots.insert_one({"measured_at": NOW - timedelta(hours=4)})


class TestTheWatchdogCatchesASilentCrawler:

    def test_a_healthy_crawler_raises_nothing(self, mock_db):
        _healthy(mock_db)
        _, problems = _watchdog().crawler_checks(mock_db, NOW)
        assert problems == []

    def test_no_maintenance_on_the_box_is_a_problem(self, mock_db):
        _healthy(mock_db)
        mock_db.ingest_runner_heartbeat.update_many({}, {"$set": {"maintenance": False}})
        _, problems = _watchdog().crawler_checks(mock_db, NOW)
        assert any("--no-maintenance" in p for p in problems)

    def test_a_failed_maintenance_run_is_reported_with_its_error(self, mock_db):
        _healthy(mock_db)
        mock_db.ingest_runner_daily_runs.insert_one({
            "job": "maintenance", "started_at": NOW - timedelta(hours=1),
            "finished_at": NOW - timedelta(hours=1), "ok": False,
            "error": "OperationFailure", "message": "quota exceeded"})
        _, problems = _watchdog().crawler_checks(mock_db, NOW)
        assert any("OperationFailure" in p and "quota exceeded" in p for p in problems)

    def test_a_run_that_never_finished_is_reported(self, mock_db):
        _healthy(mock_db)
        mock_db.ingest_runner_daily_runs.insert_one({
            "job": "maintenance", "started_at": NOW - timedelta(hours=3)})
        _, problems = _watchdog().crawler_checks(mock_db, NOW)
        assert any("never finished" in p for p in problems)

    def test_no_successful_run_for_a_day_is_reported(self, mock_db):
        _heartbeat(mock_db)
        mock_db.coverage_snapshots.insert_one({"measured_at": NOW - timedelta(hours=4)})
        _, problems = _watchdog().crawler_checks(mock_db, NOW)
        assert any("no successful runner maintenance" in p for p in problems)

    def test_a_freshly_restarted_runner_gets_a_day_before_it_is_blamed(self, mock_db):
        _heartbeat(mock_db, started=NOW - timedelta(hours=2))
        mock_db.coverage_snapshots.insert_one({"measured_at": NOW - timedelta(hours=4)})
        _, problems = _watchdog().crawler_checks(mock_db, NOW)
        assert problems == []

    def test_a_stale_coverage_snapshot_names_the_last_error(self, mock_db):
        _healthy(mock_db)
        mock_db.coverage_snapshots.delete_many({})
        mock_db.coverage_snapshots.insert_one({"measured_at": NOW - timedelta(days=21)})
        mock_db.coverage_snapshot_runs.insert_one({
            "at": NOW - timedelta(hours=18), "status": "failed",
            "error": "ModuleNotFoundError"})
        _, problems = _watchdog().crawler_checks(mock_db, NOW)
        assert any("coverage snapshot" in p and "ModuleNotFoundError" in p for p in problems)

    def test_a_dead_crawl_is_reported(self, mock_db):
        _healthy(mock_db)
        mock_db.ingest_runner_heartbeat.update_many(
            {}, {"$set": {"updated_at": NOW - timedelta(hours=2)}})
        _, problems = _watchdog().crawler_checks(mock_db, NOW)
        assert any("heartbeat" in p for p in problems)
