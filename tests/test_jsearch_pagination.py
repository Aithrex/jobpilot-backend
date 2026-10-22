"""Gap 7: the legacy JSearch path fixed page 1 and discarded the cursor.

`_search_jsearch_raw` sent `page=1&num_pages=1` and threw `data.cursor` away,
so a search could never see past its first page, `search_jobs(page=2)`
returned page 1 relabelled as page 2, and nothing reported that the result
was partial. Pagination is now honoured within a bounded budget
(`JSEARCH_MAX_PAGES`, default 1 page = unchanged cost) and every call reports
requested vs effective pages and whether the source was exhausted.
"""
from __future__ import annotations

import pytest

import services.job_service as js


class _Resp:
    def __init__(self, payload, ok=True, status=200):
        self._payload, self.ok, self.status_code, self.reason = payload, ok, status, "x"

    def json(self):
        return self._payload


def _job(i, **kw):
    base = {"job_id": f"jid-{i}", "job_title": f"Engineer {i}", "employer_name": "Acme",
            "job_apply_link": f"https://boards.greenhouse.io/acme/jobs/{i}",
            "job_publisher": "LinkedIn", "job_city": "Austin", "job_state": "TX",
            "job_country": "US", "job_description": "Python"}
    base.update(kw)
    return base


@pytest.fixture
def svc(monkeypatch):
    monkeypatch.setattr(js.JobService, "_get_jsearch_key", staticmethod(lambda: "k"))
    return js.get_job_service()


def _install(monkeypatch, pages):
    """pages: list of (jobs, cursor) served in order; records the params."""
    calls = []

    def fake_get(url, headers=None, params=None, timeout=None):
        calls.append(dict(params))
        idx = len(calls) - 1
        if idx >= len(pages):
            return _Resp({"data": {"jobs": []}})
        jobs, cursor = pages[idx]
        data = {"jobs": jobs}
        if cursor:
            data["cursor"] = cursor
        return _Resp({"data": data})

    monkeypatch.setattr(js.requests, "get", fake_get)
    return calls


def _call(svc, **kw):
    return svc._search_jsearch_raw(query="backend engineer", location="", date_posted="week",
                                   remote_only=False, employment_type="", **kw)


def test_cursor_is_followed_within_the_budget(svc, monkeypatch):
    calls = _install(monkeypatch, [([_job(i) for i in range(10)], "c2"),
                                   ([_job(i) for i in range(10, 20)], "c3")])
    meta = {}
    out = _call(svc, max_pages=2, meta_out=meta)
    assert len(out) == 20
    assert calls[1].get("cursor") == "c2"
    assert meta["requested_pages"] == 2 and meta["effective_pages"] == 2
    assert meta["complete"] is False and meta["stop_reason"] == "budget_limited"


def test_exhausted_source_is_reported_complete(svc, monkeypatch):
    _install(monkeypatch, [([_job(1), _job(2)], None)])
    meta = {}
    out = _call(svc, max_pages=3, meta_out=meta)
    assert len(out) == 2
    assert meta["complete"] is True and meta["stop_reason"] == "exhausted"
    assert meta["effective_pages"] == 1


def test_default_budget_keeps_cost_at_one_call_and_says_it_is_partial(svc, monkeypatch):
    monkeypatch.delenv("JSEARCH_MAX_PAGES", raising=False)
    calls = _install(monkeypatch, [([_job(i) for i in range(10)], "c2")])
    meta = {}
    _call(svc, meta_out=meta)
    assert len(calls) == 1
    assert meta["complete"] is False and meta["stop_reason"] == "budget_limited"


def test_requested_start_page_is_sent(svc, monkeypatch):
    calls = _install(monkeypatch, [([_job(1)], None)])
    _call(svc, start_page=3)
    assert calls[0]["page"] == "3"


def test_a_repeated_page_stops_paying(svc, monkeypatch):
    same = [_job(i) for i in range(10)]
    _install(monkeypatch, [(same, None), (same, None)])
    meta = {}
    out = _call(svc, max_pages=3, meta_out=meta)
    assert len(out) == 10
    assert meta["stop_reason"] == "pagination_not_honored" and meta["complete"] is False


def test_a_later_page_failure_keeps_earlier_pages_and_is_partial(svc, monkeypatch):
    state = {"n": 0}

    def fake_get(url, headers=None, params=None, timeout=None):
        state["n"] += 1
        if state["n"] == 1:
            return _Resp({"data": {"jobs": [_job(i) for i in range(10)], "cursor": "c2"}})
        return _Resp({}, ok=False, status=503)

    monkeypatch.setattr(js.requests, "get", fake_get)
    meta = {}
    out = _call(svc, max_pages=2, meta_out=meta)
    assert len(out) == 10
    assert meta["complete"] is False and meta["stop_reason"] == "error"


def test_normalizer_keeps_provenance(svc):
    row = svc._normalize_job(_job(7, job_apply_is_direct=True,
                                  job_google_link="https://www.google.com/search?q=x",
                                  apply_options=[{"publisher": "Acme", "apply_link":
                                                  "https://acme.com/jobs/7", "is_direct": True}],
                                  job_min_salary=0, job_max_salary=0))
    assert row["source_job_id"] == "jid-7"
    assert row["original_job_url"] == "https://boards.greenhouse.io/acme/jobs/7"
    assert row["source_publisher"] == "LinkedIn"
    assert row["apply_options"][0]["apply_link"] == "https://acme.com/jobs/7"
    assert row["apply_is_direct"] is True
    # A stated 0 is kept as 0, never turned into "missing".
    assert row["salary_min"] == 0 and row["salary_max"] == 0


def test_search_jobs_page_two_is_not_page_one_relabelled(svc, monkeypatch):
    monkeypatch.setattr(js.JobService, "_get_apify_token", staticmethod(lambda: ""))
    calls = _install(monkeypatch, [([_job(1)], None), ([_job(2)], None)])
    svc.jobs_cache.delete_many({})
    svc.search_jobs(query="backend engineer", page=1, source="jsearch",
                    use_resume_recommendations=False)
    r2 = svc.search_jobs(query="backend engineer", page=2, source="jsearch",
                         use_resume_recommendations=False)
    assert [c["page"] for c in calls] == ["1", "2"]
    assert r2["pagination"]["requested_page"] == 2
    assert r2["pagination"]["sources"]["jsearch"][0]["requested_start_page"] == 2
    svc.jobs_cache.delete_many({})


def test_a_silent_provider_fallback_is_reported(svc, monkeypatch):
    monkeypatch.setattr(js.JobService, "_get_apify_token", staticmethod(lambda: ""))
    _install(monkeypatch, [([_job(1)], None)])
    svc.jobs_cache.delete_many({})
    r = svc.search_jobs(query="backend engineer", source="linkedin",
                        use_resume_recommendations=False)
    assert r["requested_source"] == "linkedin"
    assert r["effective_sources"] == ["jsearch"]
    assert r["source_fallback"]["to"] == ["jsearch"]
    svc.jobs_cache.delete_many({})
