"""Workday's bot wall (2026-11-14 onward).

Workday boards moved behind Cloudflare bot management. The crawler hit them at
tens of requests a second from one IP (every tenant is its own hostname, so the
per-host spacing never slowed the vendor), and Cloudflare answered with 429
"Security Check" / `cf-mitigated: challenge`. Three things then went wrong at
once, and each has a test class here:

* the crawler kept hammering (retrying into the wall) - `VendorGate`;
* a challenged FIRST page counted as an empty, complete listing, so the shard
  closed every open posting of that employer as "not seen";
* the daily sweep closed every posting not re-seen in 72 h - ~13,800 open
  Workday postings in two days - although the crawl had stopped seeing ANY
  Workday board.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from services import job_ingest_worker as w
from services import job_inventory_service as inv

NOW = datetime(2026, 11, 17, 12, 0, 0, tzinfo=timezone.utc)
WD = "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/External/jobs"


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def _gate(clock, **kw):
    args = dict(interval=1.0, min_interval=0.4, max_interval=8.0, cooldown=120.0,
                max_cooldown=1800.0, recover_after=3)
    args.update(kw)
    return w.VendorGate("workday", clock=clock, sleep=clock.sleep, **args)


class TestVendorGate:
    def test_requests_are_spaced_across_callers(self):
        c = FakeClock(); g = _gate(c)
        assert g.acquire(deadline=c.t + 60)
        start = c.t
        assert g.acquire(deadline=c.t + 60)
        assert c.t - start == pytest.approx(1.0)

    def test_a_slot_past_the_deadline_is_refused_and_books_nothing(self):
        c = FakeClock(); g = _gate(c, interval=5.0)
        assert g.acquire(deadline=c.t + 60)
        assert not g.acquire(deadline=c.t + 2)          # next slot is 5 s away
        start = c.t
        assert g.acquire(deadline=c.t + 60)
        assert c.t - start == pytest.approx(5.0), "the refusal did not push the queue back"

    def test_a_challenge_closes_the_gate_and_slows_it(self):
        c = FakeClock(); g = _gate(c)
        g.note_challenge()
        assert g.interval == 2.0
        assert not g.acquire(deadline=c.t + 60), "closed while cooling down"
        c.t += 121
        assert g.acquire(deadline=c.t + 60)

    def test_one_wave_of_challenges_is_one_strike(self):
        c = FakeClock(); g = _gate(c)
        for _ in range(5):
            g.note_challenge()                          # 5 threads hit the wall together
        assert g.strikes == 1 and g.interval == 2.0
        assert g.closed_until == pytest.approx(c.t + 120)

    def test_consecutive_strikes_lengthen_the_pause_up_to_the_cap(self):
        c = FakeClock(); g = _gate(c)
        pauses = []
        for _ in range(6):
            g.note_challenge(); pauses.append(g.closed_until - c.t); c.t = g.closed_until + 1
        assert pauses == [120, 240, 480, 960, 1800, 1800]
        assert g.interval == 8.0

    def test_clean_answers_speed_it_back_up_but_not_below_the_floor(self):
        c = FakeClock(); g = _gate(c)
        g.note_challenge(); c.t = g.closed_until + 1
        for _ in range(3 * 20):
            g.note_ok()
        assert g.interval == pytest.approx(0.4)
        assert g.strikes == 0

    def test_refusals_are_counted_per_thread(self):
        import threading
        g = _gate(FakeClock())
        g.note_refused()
        seen = []
        t = threading.Thread(target=lambda: seen.append(g.refusals())); t.start(); t.join()
        assert g.refusals() == 1 and seen == [0]


def _resp(status, headers=None, body=None):
    return SimpleNamespace(status_code=status, headers=headers or {},
                           json=lambda: body if body is not None else {})


class TestHttpGetJsonBehindTheWall:
    @pytest.fixture()
    def gate(self, monkeypatch):
        c = FakeClock()
        g = _gate(c, interval=0.0, min_interval=0.0)
        monkeypatch.setattr(w, "WORKDAY_GATE", g)
        return g, c

    def _post(self, monkeypatch, answers):
        calls = []

        def fake_post(url, **kw):
            calls.append(url)
            return answers[min(len(calls), len(answers)) - 1]
        import requests
        monkeypatch.setattr(requests, "post", fake_post)
        monkeypatch.setattr(requests, "get", fake_post)
        monkeypatch.setattr(w.time, "sleep", lambda s: None)
        return calls

    def test_a_challenge_is_not_retried_and_closes_the_gate(self, gate, monkeypatch):
        g, c = gate
        calls = self._post(monkeypatch, [_resp(429, {"cf-mitigated": "challenge"})])
        assert w.http_get_json(w.SOURCE_WORKDAY, WD, w.HostPolicy(), deadline=1e12,
                               method="POST", json_body={}) is None
        assert len(calls) == 1, "never retry into a bot wall"
        assert g.closed_until > c.t and g.refusals() == 1

    def test_a_cloudflare_403_challenge_counts_too(self, gate, monkeypatch):
        g, c = gate
        self._post(monkeypatch, [_resp(403, {"cf-mitigated": "challenge"})])
        w.http_get_json(w.SOURCE_WORKDAY, WD, w.HostPolicy(), deadline=1e12, method="POST")
        assert g.strikes == 1

    def test_a_closed_gate_sends_nothing(self, gate, monkeypatch):
        g, c = gate
        g.note_challenge()
        calls = self._post(monkeypatch, [_resp(200, body={"jobPostings": []})])
        assert w.http_get_json(w.SOURCE_WORKDAY, WD, w.HostPolicy(), deadline=1e12,
                               method="POST") is None
        assert calls == [] and g.refusals() == 1

    def test_a_clean_answer_is_returned_and_counted(self, gate, monkeypatch):
        g, c = gate
        self._post(monkeypatch, [_resp(200, body={"total": 1})])
        assert w.http_get_json(w.SOURCE_WORKDAY, WD, w.HostPolicy(), deadline=1e12,
                               method="POST") == {"total": 1}
        assert g._clean == 1

    def test_other_vendors_keep_their_retry_and_never_touch_the_gate(self, gate, monkeypatch):
        g, c = gate
        calls = self._post(monkeypatch, [_resp(429), _resp(200, body={"ok": 1})])
        out = w.http_get_json("career-pages", "https://boards-api.greenhouse.io/v1/boards/acme/jobs",
                              w.HostPolicy(), deadline=1e12)
        assert out == {"ok": 1} and len(calls) == 2
        assert g.strikes == 0


class TestAFailedFirstPageIsNotACensus:
    def test_a_challenged_first_page_leaves_the_board_truncated(self, monkeypatch):
        monkeypatch.setattr(w, "http_get_json", lambda *a, **k: None)
        member = {"member_key": "acme", "tenant": "acme", "wd_host": "wd5", "site": "External"}
        rows = w._list_workday(member, cutoff=NOW - timedelta(days=7), policy=w.HostPolicy(),
                               deadline=1e12, now=NOW)
        assert rows == []
        assert member["_listing_truncated"] is True, "nothing proven: close nothing"

    def test_an_empty_board_is_still_a_complete_census(self, monkeypatch):
        monkeypatch.setattr(w, "http_get_json", lambda *a, **k: {"total": 0, "jobPostings": []})
        member = {"member_key": "acme", "tenant": "acme", "wd_host": "wd5", "site": "External"}
        w._list_workday(member, cutoff=NOW - timedelta(days=7), policy=w.HostPolicy(),
                        deadline=1e12, now=NOW)
        assert member["_listing_truncated"] is False


class TestVerifySkipsASourceTheCrawlCannotSee:
    def test_an_unhealthy_source_closes_nothing(self, monkeypatch):
        closed = []
        monkeypatch.setattr(inv, "find_stale_for_verification",
                            lambda src, **k: [{"source_job_id": f"{src}:x"}])
        monkeypatch.setattr(inv, "close_posting", lambda src, sjid, **k: closed.append(src) or True)
        health = {w.SOURCE_WORKDAY: 0.012}
        out = w.verify_liveness(limit=400, now=NOW, health=lambda s, now: health.get(s, 1.0))
        assert w.SOURCE_WORKDAY not in closed
        assert out["skipped_sources"] == {w.SOURCE_WORKDAY: 0.012}
        assert len(closed) == len(w.SOURCES) - 1

    def test_no_history_counts_as_healthy(self, monkeypatch):
        closed = []
        monkeypatch.setattr(inv, "find_stale_for_verification",
                            lambda src, **k: [{"source_job_id": f"{src}:x"}])
        monkeypatch.setattr(inv, "close_posting", lambda src, sjid, **k: closed.append(src) or True)
        out = w.verify_liveness(limit=400, now=NOW, health=lambda s, now: None)
        assert len(closed) == len(w.SOURCES) and out["skipped_sources"] == {}

    def test_health_is_the_last_day_over_the_usual_day(self, mock_db):
        runs = mock_db.job_ingestion_runs
        runs.delete_many({})
        for day in range(1, 7):                         # six ordinary days
            runs.insert_one({"source": "workday", "started_at": NOW - timedelta(days=day, hours=1),
                             "counts": {"fetched": 3_000_000}})
        runs.insert_one({"source": "workday", "started_at": NOW - timedelta(hours=3),
                         "counts": {"fetched": 37_000}})
        assert w._source_listing_health("workday", NOW) == pytest.approx(37_000 / 3_000_000)
        assert w._source_listing_health("oracle", NOW) is None
