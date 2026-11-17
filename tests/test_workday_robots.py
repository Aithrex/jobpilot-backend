"""robots.txt before every Workday request (2026-11-17).

The crawler's public page and Cloudflare's Verified Bots programme both say it
obeys robots.txt, so that has to hold for each request, not only when a source
is first verified. Workday's real file (read 2026-11-17, nordstrom):

    User-agent: *
    Allow: /nordstrom_careers/
    Disallow: /nordstrom_internship/
    Disallow: /refreshFacet/
"""
from types import SimpleNamespace

import pytest

from services import job_ingest_worker as w

HOST = "acme.wd5.myworkdayjobs.com"
API = f"https://{HOST}/wday/cxs/acme/External/jobs"
WORKDAY_ROBOTS = ("User-agent: *\nAllow: /External/\nDisallow: /Internal/\n"
                  "Disallow: /refreshFacet/\n")


class FakeClock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


@pytest.fixture()
def robots_on(monkeypatch):
    monkeypatch.setenv("JOB_INGEST_WORKDAY_ROBOTS", "1")
    clock = FakeClock()
    cache = w.RobotsCache(clock=clock)
    monkeypatch.setattr(w, "WORKDAY_ROBOTS", cache)
    return cache, clock


def _site(monkeypatch, robots_status=200, robots_text=WORKDAY_ROBOTS, robots_headers=None):
    import requests
    log = []

    def fake_get(url, **kw):
        log.append(("GET", url))
        if url.endswith("/robots.txt"):
            return SimpleNamespace(status_code=robots_status, text=robots_text,
                                   headers=robots_headers or {}, cookies=None)
        return SimpleNamespace(status_code=200, headers={}, cookies=None, json=lambda: {"ok": 1})

    def fake_post(url, **kw):
        log.append(("POST", url))
        return SimpleNamespace(status_code=200, headers={}, cookies=None, json=lambda: {"ok": 1})
    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(requests, "post", fake_post)
    return log


def _call(url, method="POST"):
    return w.http_get_json(w.SOURCE_WORKDAY, url, w.HostPolicy(), deadline=1e12, method=method)


class TestRobotsBeforeWorkday:
    def test_an_allowed_path_is_fetched_after_reading_robots_once(self, robots_on, monkeypatch):
        log = _site(monkeypatch)
        assert _call(API) == {"ok": 1}
        assert _call(API) == {"ok": 1}
        assert log == [("GET", f"https://{HOST}/robots.txt"), ("POST", API), ("POST", API)]

    def test_a_disallowed_path_is_never_requested(self, robots_on, monkeypatch):
        log = _site(monkeypatch)
        before = w.WORKDAY_GATE.refusals()
        assert _call(f"https://{HOST}/refreshFacet/x", method="GET") is None
        assert ("GET", f"https://{HOST}/refreshFacet/x") not in log
        assert w.WORKDAY_GATE.refusals() == before + 1, "counted as a refusal, never a miss"

    def test_no_robots_file_means_no_rules(self, robots_on, monkeypatch):
        _site(monkeypatch, robots_status=404, robots_text="")
        assert _call(API) == {"ok": 1}

    def test_a_challenged_robots_file_means_wait_not_guess(self, robots_on, monkeypatch):
        cache, clock = robots_on
        log = _site(monkeypatch, robots_status=429, robots_text="<html>Security Check</html>",
                    robots_headers={"cf-mitigated": "challenge"})
        assert _call(API) is None
        assert ("POST", API) not in log
        clock.t += cache.RETRY_SECONDS + 1
        w.reset_workday_gate()                      # the cooldown would refuse anyway
        monkeypatch.setattr(w, "WORKDAY_ROBOTS", cache)
        _site(monkeypatch)
        assert _call(API) == {"ok": 1}, "asked again after the retry window"

    def test_a_5xx_robots_file_is_not_permission(self, robots_on, monkeypatch):
        log = _site(monkeypatch, robots_status=503, robots_text="")
        assert _call(API) is None and ("POST", API) not in log

    def test_the_file_is_read_again_after_a_day(self, robots_on, monkeypatch):
        cache, clock = robots_on
        log = _site(monkeypatch)
        _call(API)
        clock.t += cache.TTL_SECONDS + 1
        _call(API)
        assert [e for e in log if e[1].endswith("robots.txt")] == [
            ("GET", f"https://{HOST}/robots.txt")] * 2

    def test_a_rule_naming_our_agent_is_obeyed(self, robots_on, monkeypatch):
        log = _site(monkeypatch, robots_text="User-agent: JobPilotIngest\nDisallow: /\n")
        assert _call(API) is None and ("POST", API) not in log

    def test_other_vendors_never_read_robots_here(self, robots_on, monkeypatch):
        log = _site(monkeypatch)
        w.http_get_json("career-pages", "https://boards-api.greenhouse.io/v1/boards/acme/jobs",
                        w.HostPolicy(), deadline=1e12)
        assert not [e for e in log if e[1].endswith("robots.txt")]

    def test_off_switch(self, monkeypatch):
        monkeypatch.setenv("JOB_INGEST_WORKDAY_ROBOTS", "0")
        log = _site(monkeypatch, robots_text="User-agent: *\nDisallow: /\n")
        assert _call(API) == {"ok": 1} and log == [("POST", API)]
