"""The authenticated feed tabs served from the canonical inventory.

`inventory_feed.build_feed` replaces a live crawl of 1013 Workday tenants /
1309 career-page companies with one indexed query. These tests pin the two
things a cutover can silently break: the PAYLOAD CONTRACT the frontend renders,
and the "defer rather than lie" rule that stops a warming shard from showing an
empty job market.
"""
from datetime import datetime, timedelta, timezone

import pytest

from services import fit_score
from services import inventory_feed as feed
from services import job_content_store as store
from services import job_inventory_service as inv

NOW = datetime.now(timezone.utc)

COLLECTIONS = ("job_postings", "job_source_state", "job_ingestion_runs")

JD = ("Senior DevOps Engineer. Requirements: 5+ years with Python, AWS, "
      "Kubernetes and Terraform. Preferred: Datadog. BS degree required.")

SKILLS = ["Python", "AWS", "Kubernetes", "Terraform", "Docker"]


@pytest.fixture()
def db(monkeypatch):
    monkeypatch.delenv("RESUME_S3_BUCKET", raising=False)
    store.reset_caches()
    d = inv._db()
    for c in COLLECTIONS:
        d[c].delete_many({})
    yield d
    for c in COLLECTIONS:
        d[c].delete_many({})


def _seed(n, *, source="career-pages", industry="tech", tenant="acme",
          title="Senior DevOps Engineer", location="Austin, TX, USA",
          description=JD, days=2):
    for i in range(n):
        sjid = f"{tenant}:{title[:6]}-{i}"
        inv.upsert_posting({
            "source": source, "source_job_id": sjid,
            "url": f"https://boards.greenhouse.io/{tenant}/jobs/{i}",
            "company": f"{tenant.title()} {i}", "title": title,
            "location": [location], "description": description,
            "employment_type": "Full time", "cost_class": "free",
            "source_quality": "direct", "industry": industry,
            "ats": "greenhouse" if source == "career-pages" else "workday",
            "tenant": tenant,
            "posted_at": NOW - timedelta(days=days),
        }, now=NOW)
        profile = fit_score.extract_requirements(description, title)
        inv.mark_requirements_parsed(
            source, sjid, inv.get_posting(source, sjid)["content_hash"],
            profile, now=NOW)


def _payload(**over):
    base = {"titles": ["DevOps Engineer"], "us_only": True, "user_email": "",
            "resume_skills": SKILLS, "resume_years": 5, "tz_offset_minutes": -420}
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# Payload contract
# ---------------------------------------------------------------------------

REQUIRED_KEYS = {
    "ok", "generated_at", "jobs", "total", "window_counts", "query_terms",
    "tenants_total", "tenants_done", "tenants_with_results",
    "industries_available", "cache_hit", "us_only", "resume_skill_count",
    "diagnostics", "errors",
}

REQUIRED_JOB_KEYS = {
    "job_id", "title", "company", "tenant", "industry", "location",
    "date_posted", "days_ago", "posted_text", "apply_link", "source",
    "is_remote", "match_score", "matched_skills", "missing_skills",
}


def test_payload_matches_the_legacy_tab_contract(db):
    """The frontend renders this shape; a missing key is a broken tab."""
    _seed(40)
    result = feed.build_feed("career-pages", _payload())

    assert result is not None
    assert REQUIRED_KEYS <= set(result), REQUIRED_KEYS - set(result)
    assert result["ok"] is True
    assert result["total"] == len(result["jobs"])
    assert REQUIRED_JOB_KEYS <= set(result["jobs"][0]), \
        REQUIRED_JOB_KEYS - set(result["jobs"][0])
    # The career tab additionally drives an ATS filter.
    assert result["ats_available"]
    # Internal scoring input must never reach the client.
    assert "_requirements" not in result["jobs"][0]


def test_workday_payload_omits_the_ats_filter(db):
    _seed(40, source="workday", tenant="acmewd")
    result = feed.build_feed("workday", _payload())
    assert result is not None
    assert REQUIRED_KEYS <= set(result)
    assert "ats_available" not in result


def test_rows_are_ranked_by_score_then_recency(db):
    _seed(40)
    result = feed.build_feed("career-pages", _payload())
    ranked = [(j["match_score"], j["days_ago"]) for j in result["jobs"]]
    assert ranked == sorted(ranked, key=lambda p: (-p[0], p[1]))
    assert result["jobs"][0]["match_score"] > 0


def test_scores_come_from_the_shared_engine(db):
    """ADR-002: one versioned engine across guest and authenticated paths, so
    selection and display cannot disagree."""
    _seed(40)
    result = feed.build_feed("career-pages", _payload())
    job = result["jobs"][0]
    assert job["fit"]["score_version"] == fit_score.FIT_SCORE_VERSION
    assert job["fit"]["band"]
    assert job["matched_skills"]


# ---------------------------------------------------------------------------
# Defer rather than lie
# ---------------------------------------------------------------------------

def test_a_cold_slice_defers_to_the_live_crawl(db):
    """A thin payload renders as "there are no jobs", which is false while
    shards are still warming. None makes the caller fall back to the crawl."""
    _seed(max(1, feed.MIN_ROWS - 5))
    assert feed.build_feed("career-pages", _payload()) is None


def test_an_empty_inventory_defers(db):
    assert feed.build_feed("career-pages", _payload()) is None


def test_scan_mode_is_the_rollback(monkeypatch):
    monkeypatch.setenv("FEED_MATCH_MODE", "scan")
    assert feed.enabled() is False
    monkeypatch.delenv("FEED_MATCH_MODE")
    assert feed.enabled() is True


def test_retrieval_failure_defers_instead_of_raising(db, monkeypatch):
    _seed(40)

    def _boom(**kw):
        raise RuntimeError("atlas down")

    monkeypatch.setattr(inv, "find_active_fresh", _boom)
    assert feed.build_feed("career-pages", _payload()) is None


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------

def test_industry_and_company_filters_apply(db):
    _seed(40, industry="tech", tenant="acme")
    _seed(40, industry="finance", tenant="bank")

    finance = feed.build_feed("career-pages", _payload(industries=["finance"]))
    assert finance is not None
    assert {j["industry"] for j in finance["jobs"]} == {"finance"}

    by_company = feed.build_feed("career-pages", _payload(companies=["bank"]))
    assert by_company is not None
    assert {j["tenant"] for j in by_company["jobs"]} == {"bank"}


def test_a_workday_query_never_returns_career_page_rows(db):
    _seed(40, source="career-pages", tenant="acme")
    assert feed.build_feed("workday", _payload()) is None


def test_non_us_rows_are_dropped_when_us_only(db):
    """Uses the tab's OWN geo filter. A country-code check passes almost
    everything, because boards ship free-text locations and the inventory
    rarely parses a country out of them."""
    _seed(40, location="Austin, TX, USA")
    _seed(40, tenant="offshore", location="Hyderabad, India")

    result = feed.build_feed("career-pages", _payload(us_only=True))
    assert result is not None
    assert not any("india" in (j["location"] or "").lower() for j in result["jobs"])


def test_duplicate_postings_are_collapsed(db):
    """Employers publish one requisition per location and re-post across
    sub-boards; undeduped, the same job fills the top of the tab."""
    for i in range(40):
        inv.upsert_posting({
            "source": "career-pages", "source_job_id": f"dupe:{i}",
            "url": f"https://boards.greenhouse.io/dupe/jobs/{i}",
            "company": "Acme", "title": "Senior DevOps Engineer",
            "location": ["Austin, TX, USA"], "description": JD,
            "cost_class": "free", "industry": "tech", "ats": "greenhouse",
            "tenant": "dupe", "posted_at": NOW - timedelta(days=2),
        }, now=NOW)
    _seed(40, tenant="real")

    result = feed.build_feed("career-pages", _payload())
    acme = [j for j in result["jobs"] if j["company"] == "Acme"]
    assert len(acme) == 1, "40 identical postings must collapse to one card"


def test_no_resume_and_no_skills_scores_nothing_rather_than_guessing(db):
    _seed(40)
    result = feed.build_feed("career-pages",
                             _payload(resume_skills=[], resume_years=None))
    assert result is not None
    assert result["resume_skill_count"] == 0
    assert all(j["match_score"] == 0 for j in result["jobs"])
