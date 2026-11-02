"""The crawl scheduler fires late rather than never, and a deploy stops it cleanly.

Measured on the box, 2026-11-02: the discovery dispatch at :02/:17/:32/:47 took
2-3.5 minutes on the single poll thread, so the iCIMS tick due at
:04/:19/:34/:49 was skipped whenever it ran over - 21 of 72 ticks ran. And
every deploy SIGKILLed shards mid-run (349 failed runs over two days).
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

import pytest

import services.ingest_runner.runner as R
from services import ingest_shutdown
from services import job_ingest_worker as worker


@pytest.fixture(autouse=True)
def _clean_shutdown():
    ingest_shutdown.reset()
    yield
    ingest_shutdown.reset()


@pytest.fixture()
def runner(monkeypatch):
    r = R.Runner(["workday", "icims", "career-pages", "oracle"], maintenance=True)
    fired = []
    monkeypatch.setattr(r, "dispatch_source", lambda src: fired.append(src))
    monkeypatch.setattr(r, "run_maintenance", lambda: fired.append("maintenance"))
    monkeypatch.setattr(r, "run_coverage_snapshot", lambda: fired.append("coverage"))
    r.fired = fired
    yield r
    r.pool.shutdown(wait=False)
    r.discovery_pool.shutdown(wait=False)


def at(h, m, s=0):
    return datetime(2026, 11, 2, h, m, s, tzinfo=timezone.utc)


class TestLateNotNever:

    def test_a_tick_missed_on_its_minute_fires_later_in_the_quarter(self, runner, monkeypatch):
        monkeypatch.setattr(runner, "dispatch_discovery", lambda: None)
        runner.poll(at(17, 45, 5))          # workday's minute
        runner.poll(at(17, 52, 30))         # the poll was held past :49
        assert runner.fired == ["workday", "icims", "career-pages"]

    def test_it_fires_once_per_quarter(self, runner, monkeypatch):
        monkeypatch.setattr(runner, "dispatch_discovery", lambda: None)
        for minute in (4, 5, 6, 14):
            runner.poll(at(18, minute))
        assert runner.fired.count("icims") == 1
        runner.poll(at(18, 19))
        assert runner.fired.count("icims") == 2

    def test_a_slow_discovery_dispatch_does_not_hold_the_crawl(self, runner, monkeypatch):
        release = threading.Event()
        monkeypatch.setattr(runner, "dispatch_discovery", lambda: release.wait(5))
        started = time.monotonic()
        runner.poll(at(17, 47, 1))
        runner.poll(at(17, 49, 1))
        assert time.monotonic() - started < 2
        assert "icims" in runner.fired
        release.set()

    def test_discovery_never_runs_twice_at_once(self, runner, monkeypatch):
        release = threading.Event()
        calls = []

        def slow():
            calls.append(1)
            release.wait(5)
        monkeypatch.setattr(runner, "dispatch_discovery", slow)
        runner.poll(at(17, 2))
        runner.poll(at(17, 17))             # next quarter, first still running
        time.sleep(0.1)
        assert len(calls) == 1
        release.set()

    def test_daily_jobs_catch_up_inside_their_window_only(self, runner, monkeypatch):
        monkeypatch.setattr(runner, "dispatch_discovery", lambda: None)
        runner.poll(at(5, 27))               # held past 05:20
        assert runner.fired.count("maintenance") == 1
        runner.poll(at(5, 28))
        assert runner.fired.count("maintenance") == 1   # once a day
        fresh = R.Runner(["icims"], maintenance=True)
        hits = []
        monkeypatch.setattr(fresh, "run_maintenance", lambda: hits.append(1))
        monkeypatch.setattr(fresh, "dispatch_source", lambda s: None)
        monkeypatch.setattr(fresh, "dispatch_discovery", lambda: None)
        fresh.poll(at(12, 0))                # a noon restart does not re-run it
        assert hits == []
        fresh.pool.shutdown(wait=False)
        fresh.discovery_pool.shutdown(wait=False)


class TestCleanStop:

    def test_request_stops_every_running_shard(self):
        a, b = threading.Event(), threading.Event()
        ingest_shutdown.register(a)
        ingest_shutdown.register(b)
        ingest_shutdown.unregister(b)
        assert ingest_shutdown.request() == 1
        assert a.is_set() and not b.is_set()

    def test_a_shard_registered_after_the_request_is_stopped_at_once(self):
        ingest_shutdown.request()
        late = threading.Event()
        ingest_shutdown.register(late)
        assert late.is_set()

    def test_a_shard_does_not_start_once_stopping(self, monkeypatch):
        monkeypatch.setattr(worker.inv, "acquire_lease",
                            lambda *a, **k: pytest.fail("took a lease while stopping"))
        ingest_shutdown.request()
        out = worker.run_shard("icims", "s001")
        assert out.get("skipped") == "shutting_down"

    def test_a_shard_stopped_before_any_board_answered_is_not_a_failure(self, monkeypatch):
        calls = {"failure": 0, "status": None}
        monkeypatch.setattr(worker.inv, "acquire_lease", lambda *a, **k: {"_id": "x"})
        monkeypatch.setattr(worker.inv, "deep_listing_due", lambda *a, **k: False)
        monkeypatch.setattr(worker.inv, "start_run", lambda *a, **k: "run1")
        monkeypatch.setattr(worker.inv, "release_lease", lambda *a, **k: None)
        monkeypatch.setattr(worker.inv, "bump_run_counts", lambda *a, **k: None)
        monkeypatch.setattr(worker.inv, "record_run_failure", lambda *a, **k: None)
        monkeypatch.setattr(worker.inv, "advance_watermark", lambda *a, **k: None)
        monkeypatch.setattr(worker.inv, "finish_run",
                            lambda run_id, status="succeeded", now=None: calls.update(status=status))

        def failure(*a, **k):
            calls["failure"] += 1
        monkeypatch.setattr(worker.inv, "record_failure", failure)
        monkeypatch.setattr(worker, "shard_members",
                            lambda src, shard: [{"member_key": "icims-acme"}])
        monkeypatch.setattr(worker, "_close_stale_in_shard", lambda *a, **k: 0)

        def stopped_listing(fn, members, **k):
            ingest_shutdown.request()        # SIGTERM lands mid-listing
            return [(m, None, "stopped") for m in members]
        monkeypatch.setattr(worker, "run_bounded", stopped_listing)

        out = worker.run_shard("icims", "s001")
        assert out.get("skipped") == "shutting_down"
        assert calls["status"] == "interrupted" and calls["failure"] == 0

    def test_the_runner_tells_shards_on_sigterm(self, monkeypatch):
        r = R.Runner(["icims"], discovery=False, maintenance=False)
        monkeypatch.setattr(r, "poll", lambda now=None: None)
        monkeypatch.setattr(r, "heartbeat", lambda: None)
        stop = threading.Event()
        ingest_shutdown.register(stop)
        r.stop.set()
        r.serve()
        assert stop.is_set() and ingest_shutdown.requested()


def test_pacing_never_sleeps_a_negative_length(monkeypatch):
    """`end` can pass between the loop check and the sleep; a negative sleep
    raised ValueError and failed the member (17 times on 11-01..02)."""
    seen = []
    monkeypatch.setattr(worker.time, "sleep", lambda s: seen.append(s))
    policy = worker.HostPolicy()
    host = "example.com"
    policy.acquire(host, deadline=time.monotonic() + 5)
    policy.acquire(host, deadline=time.monotonic() + 5)
    assert all(s >= 0 for s in seen)
