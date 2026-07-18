"""MNCs / Big Tech tab — catalog integrity + service pipeline + routes.

Fully offline: every fetcher (board / enterprise / workday / aggregator) and
the detail-enrichment pass are monkeypatched, Mongo is mongomock (via
conftest/_vg_support), and no LLM is involved anywhere (deterministic scoring
only, same as career-pages).
"""
import sys
from datetime import datetime, timedelta, timezone

import pytest

from _vg_support import load_service_app

from services.career_pages_service import ENTERPRISE_FETCHERS, FETCHERS
from services.mnc_catalog import MNC_CATALOG, MNC_ENGINES, MNC_SECTORS
import services.mnc_jobs_service as mnc


# ----------------------------------------------------------------------
# Catalog integrity
# ----------------------------------------------------------------------

def test_catalog_size_and_slug_uniqueness():
    assert 45 <= len(MNC_CATALOG) <= 70
    slugs = [r["slug"] for r in MNC_CATALOG]
    assert len(slugs) == len(set(slugs)), "duplicate slugs in MNC_CATALOG"


def test_catalog_rows_have_valid_engine_and_sector():
    for r in MNC_CATALOG:
        assert r.get("engine") in MNC_ENGINES, r
        assert r.get("sector") in MNC_SECTORS, r
        assert str(r.get("display_name") or "").strip(), r
        assert str(r.get("slug") or "").strip(), r


def test_catalog_engine_specific_keys():
    for r in MNC_CATALOG:
        engine = r["engine"]
        if engine == "career_pages":
            assert r.get("ats") in FETCHERS, r
        elif engine == "enterprise":
            assert r.get("fetcher") in ENTERPRISE_FETCHERS, r
            assert str(r.get("host") or "").strip(), r
            assert isinstance(r.get("extra"), dict), r
        elif engine == "workday":
            assert str(r.get("tenant") or "").strip(), r
            assert str(r.get("wd_host") or "").startswith("wd"), r
            assert str(r.get("site") or "").strip(), r
        else:  # aggregator
            assert isinstance(r.get("aliases", []), list), r


def test_catalog_includes_required_companies():
    names = {r["display_name"].lower() for r in MNC_CATALOG}
    for required in ("amazon", "meta", "apple", "google", "netflix", "microsoft",
                     "nvidia", "tesla", "cisco", "qualcomm", "intel", "amd",
                     "oracle", "ibm", "salesforce", "adobe", "uber", "airbnb",
                     "stripe"):
        assert required in names, f"missing required company: {required}"


def test_catalog_covers_all_ten_sectors():
    assert {r["sector"] for r in MNC_CATALOG} == set(MNC_SECTORS)


# ----------------------------------------------------------------------
# catalog_summary — same shape as career_pages_service.catalog_summary
# ----------------------------------------------------------------------

def test_catalog_summary_shape():
    summary = mnc.catalog_summary()
    assert summary["total"] == len(MNC_CATALOG)
    assert isinstance(summary["industries"], list)
    seen_keys = []
    count_sum = 0
    for ind in summary["industries"]:
        assert set(ind.keys()) == {"key", "count", "companies"}
        assert ind["key"] in MNC_SECTORS
        assert ind["count"] == len(ind["companies"])
        count_sum += ind["count"]
        seen_keys.append(ind["key"])
        for c in ind["companies"]:
            assert set(c.keys()) == {"name", "tenant", "ats"}
            assert c["ats"] in ("greenhouse", "lever", "ashby", "smartrecruiters",
                                "workable", "recruitee", "amazon", "eightfold",
                                "phenom", "workday", "aggregator")
    assert count_sum == summary["total"]
    assert len(seen_keys) == len(set(seen_keys))


# ----------------------------------------------------------------------
# Aggregator employer-name strict matching
# ----------------------------------------------------------------------

def _row_for(slug):
    return next(r for r in MNC_CATALOG if r["slug"] == slug)


def test_employer_allowed_exact_and_suffix_normalized():
    google = _row_for("google")
    assert mnc._employer_allowed(google, "Google")
    assert mnc._employer_allowed(google, "Google LLC")
    assert mnc._employer_allowed(google, "Alphabet")           # alias
    assert not mnc._employer_allowed(google, "Google Operations Center")
    assert not mnc._employer_allowed(google, "")


def test_employer_allowed_rejects_lookalikes():
    apple = _row_for("apple")
    assert mnc._employer_allowed(apple, "Apple Inc.")
    assert not mnc._employer_allowed(apple, "Apple Bank")
    meta = _row_for("meta")
    assert mnc._employer_allowed(meta, "Meta Platforms, Inc.")
    assert not mnc._employer_allowed(meta, "Metagenics")
    uber = _row_for("uber")
    assert mnc._employer_allowed(uber, "Uber Technologies, Inc.")
    assert not mnc._employer_allowed(uber, "Uber Freight")


def _jsearch_job(job_id, company, title="Software Engineer"):
    return {
        "job_id": job_id,
        "title": title,
        "company": company,
        "location": "Austin, TX, US",
        "apply_link": f"https://example.com/{job_id}",
        "description": "Build services in Python on AWS.",
        "date_posted": (datetime.now(timezone.utc) - timedelta(days=2)).isoformat(),
        "is_remote": False,
        "posted_text": "2 days ago",
        "source": "JSearch",
    }


def test_fetch_aggregator_filters_foreign_employers(monkeypatch):
    calls = []

    class FakeJobService:
        def _search_jsearch_raw(self, query, location, date_posted,
                                remote_only, employment_type):
            calls.append(query)
            return [
                _jsearch_job("1", "Google LLC"),
                _jsearch_job("2", "Google Operations Center"),
                _jsearch_job("3", "Alphabet"),
                _jsearch_job("4", "Googler Staffing Agency"),
            ]

    import services.job_service as job_service_mod
    monkeypatch.setattr(job_service_mod, "get_job_service", lambda: FakeJobService())

    out = mnc._fetch_aggregator(_row_for("google"), ["software engineer"])
    ids = sorted(j["job_id"] for j in out)
    assert ids == ["aggregator:google:1", "aggregator:google:3"]
    assert calls == ["software engineer Google"]
    for j in out:
        assert j["ats"] == "aggregator"
        assert j["tenant"] == "google"
        assert j["company"] == "Google"
        assert j["source"] == "JSearch"


# ----------------------------------------------------------------------
# run_mnc_search — mocked fetchers, real scoring/filter/rank pipeline
# ----------------------------------------------------------------------

RESULT_KEYS = {
    "ok", "generated_at", "jobs", "total", "window_counts", "query_terms",
    "tenants_total", "tenants_done", "tenants_with_results",
    "industries_available", "ats_available", "cache_hit", "us_only",
    "resume_skill_count", "diagnostics", "errors",
}


def _job(row_slug, job_id, title, *, days=1, location="San Francisco, CA",
         description="", ats="greenhouse", country=""):
    date_posted = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    return {
        "job_id": f"{ats}:{row_slug}:{job_id}",
        "title": title,
        "company": row_slug.title(),
        "tenant": row_slug,
        "ats": ats,
        "industry": "big_tech",
        "location": location,
        "country_hint": country,
        "date_posted": date_posted,
        "days_ago": days,
        "posted_text": f"Posted {days} Days Ago",
        "apply_link": f"https://example.com/{job_id}",
        "description": description,
        "is_remote": False,
        "source": ats,
    }


@pytest.fixture()
def patched_pipeline(monkeypatch, mock_db):
    """Mock every network boundary; leave scoring/filtering/caching real."""
    board_jobs = {
        "airbnb": [
            _job("airbnb", "a1", "Software Engineer",
                 description="We use Python and AWS daily."),
            _job("airbnb", "a2", "Backend Engineer", days=3),
            _job("airbnb", "a3", "Software Engineer", days=45),          # stale
            _job("airbnb", "a4", "Software Engineer",
                 location="Toronto, Canada"),                            # non-US
            _job("airbnb", "a5", "Head of Communications"),              # irrelevant
        ],
    }
    monkeypatch.setattr(mnc, "_fetch_board", lambda r: list(board_jobs.get(r["slug"], [])))
    monkeypatch.setattr(mnc, "_fetch_enterprise", lambda r, terms: [])
    monkeypatch.setattr(
        mnc, "_fetch_workday",
        lambda r, terms: [_job(r["slug"], "w1", "Software Engineer",
                               ats="workday",
                               description="Python microservices on AWS.")]
        if r["slug"] == "cisco" else [])
    monkeypatch.setattr(mnc, "_fetch_aggregator", lambda r, terms: [])
    monkeypatch.setattr(mnc, "_jsearch_configured", lambda: True)
    monkeypatch.setattr(mnc, "_enrich_details", lambda jobs: 0)
    monkeypatch.setattr(mnc, "_load_resume_profile",
                        lambda email: (["python", "aws"], 3))
    mock_db[mnc.CACHE_COLLECTION].delete_many({})
    return mock_db


def test_run_mnc_search_scores_filters_and_ranks(patched_pipeline):
    partials = []
    result = mnc.run_mnc_search(
        {"titles": ["software engineer"], "companies": ["airbnb", "cisco"],
         "us_only": True, "user_email": "t@x.com"},
        partial_cb=partials.append,
    )

    assert set(result.keys()) == RESULT_KEYS
    assert result["ok"] is True
    assert result["tenants_total"] == 2
    assert result["tenants_done"] == 2
    assert result["tenants_with_results"] == 2
    assert result["us_only"] is True
    assert result["resume_skill_count"] == 2
    assert result["cache_hit"] is False
    assert result["industries_available"] == list(MNC_SECTORS)
    assert "aggregator" in result["ats_available"]

    jobs = result["jobs"]
    ids = {j["job_id"] for j in jobs}
    assert "greenhouse:airbnb:a3" not in ids       # outside 30d window
    assert "greenhouse:airbnb:a4" not in ids       # non-US
    assert "greenhouse:airbnb:a5" not in ids       # fails relevance gate
    assert {"greenhouse:airbnb:a1", "greenhouse:airbnb:a2",
            "workday:cisco:w1"} <= ids

    for j in jobs:
        assert isinstance(j.get("match_score"), int)
        assert isinstance(j.get("matched_skills"), list)
        assert "missing_skills" in j
        assert j.get("sector") in MNC_SECTORS
        assert j.get("source")

    scores = [j["match_score"] for j in jobs]
    assert scores == sorted(scores, reverse=True)
    # Rows whose description carries the resume skills outrank the bare one.
    by_id = {j["job_id"]: j for j in jobs}
    assert by_id["greenhouse:airbnb:a1"]["match_score"] > \
        by_id["greenhouse:airbnb:a2"]["match_score"]
    assert "python" in [s.lower() for s in by_id["greenhouse:airbnb:a1"]["matched_skills"]]

    # Streaming: at least the pre-details snapshot fired, career-pages shape.
    assert partials, "partial_cb never called"
    assert partials[-1]["streaming"] is True
    assert "progress" in partials[-1] and "jobs" in partials[-1]


def test_run_mnc_search_second_call_hits_cache(patched_pipeline):
    payload = {"titles": ["software engineer"], "companies": ["airbnb"],
               "us_only": True, "user_email": "t@x.com"}
    first = mnc.run_mnc_search(payload)
    assert first["cache_hit"] is False
    second = mnc.run_mnc_search(payload)
    assert second["cache_hit"] is True
    assert {j["job_id"] for j in second["jobs"]} == {j["job_id"] for j in first["jobs"]}


def test_run_mnc_search_skips_aggregator_without_jsearch_key(patched_pipeline, monkeypatch):
    monkeypatch.setattr(mnc, "_jsearch_configured", lambda: False)

    def _boom(row, terms):  # must never be called without a key
        raise AssertionError("aggregator fetch attempted without JSearch key")

    monkeypatch.setattr(mnc, "_fetch_aggregator", _boom)
    result = mnc.run_mnc_search(
        {"titles": ["software engineer"], "companies": ["google", "airbnb"],
         "us_only": True, "user_email": "t@x.com"})
    assert result["diagnostics"].get("aggregator_skipped") == ["google"]
    assert result["tenants_total"] == 1        # google excluded from fan-out
    assert result["diagnostics"]["task_errors"] == 0


def test_run_mnc_search_sector_filter(patched_pipeline):
    result = mnc.run_mnc_search(
        {"titles": ["software engineer"], "industries": ["media_entertainment"],
         "us_only": True, "user_email": "t@x.com"})
    expected = sum(1 for r in MNC_CATALOG
                   if r["sector"] == "media_entertainment" and r["engine"] != "aggregator")
    # tenants_total counts only fetchable rows (aggregator rows are included
    # because _jsearch_configured is patched True in this fixture).
    agg = sum(1 for r in MNC_CATALOG
              if r["sector"] == "media_entertainment" and r["engine"] == "aggregator")
    assert result["tenants_total"] == expected + agg


# ----------------------------------------------------------------------
# Routes — /api/jobs/mnc/catalog + /api/jobs/mnc/search (202 {job_id})
# ----------------------------------------------------------------------

@pytest.fixture()
def jobs_app():
    return load_service_app("jobs")


def _token(flask_app, identity="mnc-test@aspirely.me"):
    from flask_jwt_extended import create_access_token
    with flask_app.app_context():
        return create_access_token(identity=identity)


def test_mnc_catalog_route_returns_summary(jobs_app):
    token = _token(jobs_app)
    resp = jobs_app.test_client().get(
        "/api/jobs/mnc/catalog", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["total"] == len(MNC_CATALOG)
    assert {i["key"] for i in body["industries"]} == set(MNC_SECTORS)


def test_mnc_search_route_returns_202_job_id(jobs_app, monkeypatch):
    import services.resume_service as resume_service_mod
    dispatched = []
    monkeypatch.setattr(
        resume_service_mod.ResumeService, "invoke_async",
        staticmethod(lambda job_id, job_type, payload: dispatched.append(job_type)))

    token = _token(jobs_app)
    resp = jobs_app.test_client().post(
        "/api/jobs/mnc/search",
        headers={"Authorization": f"Bearer {token}"},
        json={"titles": ["software engineer"], "industries": ["big_tech"]})
    assert resp.status_code == 202, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body.get("job_id")
    assert dispatched == ["mnc_jobs"]

    # The pending job landed in resume_jobs with the right type + owner.
    from _vg_support import current_db
    doc = current_db().resume_jobs.find_one({"job_id": body["job_id"]})
    assert doc is not None
    assert doc["job_type"] == "mnc_jobs"
    assert doc["user_email"] == "mnc-test@aspirely.me"


def test_mnc_search_route_requires_jwt(jobs_app):
    resp = jobs_app.test_client().post("/api/jobs/mnc/search", json={})
    assert resp.status_code == 401  # @jwt_required missing-token contract


if __name__ == "__main__":  # allow `python tests/test_mnc_jobs.py`
    sys.exit(pytest.main([__file__, "-q"]))
