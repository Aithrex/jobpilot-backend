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


# ---------------------------------------------------------------------------
# Dedupe key. These run against `_dedupe` directly: the collapse rule is pure
# and the DB round-trip in the test above only obscures which shapes it catches.
# ---------------------------------------------------------------------------

def _row(company, title, location, days_ago=3):
    return {"company": company, "title": title, "location": location,
            "days_ago": days_ago, "job_id": f"{company}|{title}|{days_ago}"}


@pytest.mark.parametrize("a,b,why", [
    (("Google", "Software Engineer III", "New York, NY"),
     ("Google LLC", "Software Engineer III", "New York, NY"),
     "legal suffix"),
    (("Stripe", "Backend Engineer", "Seattle, WA"),
     ("Stripe", "Backend Engineer (Remote)", "Seattle, WA"),
     "work-mode tag in the title"),
    (("Acme", "Data Analyst (Req ID: 4471)", "Austin, TX"),
     ("Acme", "Data Analyst", "Austin, TX"),
     "per-requisition id glued into the title"),
    (("Acme", "Data Analyst", "Austin, TX"),
     ("Acme", "Data Analyst - Full Time", "Austin, TX"),
     "employment type"),
])
def test_one_opening_published_two_ways_collapses(a, b, why):
    """Every one of these is a single opening that reached us twice. Matching on
    the raw display strings let all of them through - they are byte-unequal and
    none of them is a different job."""
    out = feed._dedupe([_row(*a), _row(*b)])
    assert len(out) == 1, f"duplicate survived: {why}"


@pytest.mark.parametrize("a,b", [
    (("Acme", "Software Engineer (Backend)", "Austin, TX"),
     ("Acme", "Software Engineer (Frontend)", "Austin, TX")),
    (("Acme", "Engineer I", "Austin, TX"),
     ("Acme", "Engineer II", "Austin, TX")),
    (("Acme", "Data Analyst", "Austin, TX"),
     ("Acme", "Data Analyst", "Seattle, WA")),
    (("Acme", "Data Analyst", "Austin, TX"),
     ("Acme Staffing", "Data Analyst", "Austin, TX")),
])
def test_genuinely_different_openings_are_never_merged(a, b):
    """The failure that matters. `job_inventory_service.normalize_title` strips
    EVERY parenthetical, so keying on it would map (Backend) and (Frontend) to
    the same string and drop a real job off the feed. Showing a job twice is a
    blemish; hiding one is a lost application."""
    out = feed._dedupe([_row(*a), _row(*b)])
    assert len(out) == 2


def test_the_freshest_copy_of_a_duplicate_wins():
    """The displayed age has to stay honest once copies collapse."""
    out = feed._dedupe([
        _row("Google LLC", "Software Engineer", "New York, NY", days_ago=9),
        _row("Google", "Software Engineer (Remote)", "New York, NY", days_ago=1),
    ])
    assert len(out) == 1
    assert out[0]["days_ago"] == 1


def test_dedupe_preserves_input_order():
    """Ranking happens before this; collapsing must not reshuffle it."""
    rows = [_row("A", "Engineer", "Austin, TX"), _row("B", "Engineer", "Austin, TX"),
            _row("C", "Engineer", "Austin, TX")]
    assert [r["company"] for r in feed._dedupe(rows)] == ["A", "B", "C"]


# ---------------------------------------------------------------------------
# Salary. The inventory stored none at all and the row hard-coded "", so the
# filter had nothing to compare - these pin capture, surfacing, and the one
# semantic that decides whether the filter is useful or destructive.
# ---------------------------------------------------------------------------

def _seed_paid(sjid, *, comp, tenant="paid"):
    inv.upsert_posting({
        "source": "career-pages", "source_job_id": sjid,
        "url": f"https://boards.greenhouse.io/{tenant}/jobs/{sjid}",
        "company": f"Payer {sjid}", "title": "Senior DevOps Engineer",
        "location": ["Austin, TX, USA"], "description": JD,
        "cost_class": "free", "industry": "tech", "ats": "greenhouse",
        "tenant": tenant, "compensation": comp,
        "posted_at": NOW - timedelta(days=2),
    }, now=NOW)
    profile = fit_score.extract_requirements(JD, "Senior DevOps Engineer")
    inv.mark_requirements_parsed("career-pages", sjid,
                                 inv.get_posting("career-pages", sjid)["content_hash"],
                                 profile, now=NOW)


def test_a_stated_band_reaches_the_card(db):
    _seed_paid("has-pay", comp={"min": 120000, "max": 150000, "currency": "USD"})
    doc = inv.get_posting("career-pages", "has-pay")
    row = feed._row_from_posting(doc, tz_offset=-420)
    assert row["salary"] == "$120,000 - $150,000 a year"
    assert row["salary_min_annual"] == 120000
    assert row["salary_confidence"] == "structured"


def test_an_undisclosed_salary_is_none_not_zero(db):
    """Zero would read as "this job pays nothing" to every comparison
    downstream, and would fail any salary floor. Unknown is not a number."""
    _seed(1, tenant="nopay")
    doc = inv.get_posting("career-pages", "nopay:Senior-0")
    row = feed._row_from_posting(doc, tz_offset=-420)
    assert row["salary"] == ""
    assert row["salary_min_annual"] is None


def test_a_salary_floor_drops_only_bands_it_can_actually_compare(db):
    """The decision that makes this filter safe. Most postings do not publish a
    salary, so treating unknown as "fails the filter" would not narrow the feed
    - it would empty it, and hide most of the market without saying so."""
    _seed(30, tenant="nopay")                       # no band at all
    _seed_paid("low", comp={"min": 60000, "max": 70000, "currency": "USD"})
    _seed_paid("high", comp={"min": 180000, "max": 220000, "currency": "USD"})

    result = feed.build_feed("career-pages", _payload(min_salary=150000))
    companies = {j["company"] for j in result["jobs"]}

    assert "Payer high" in companies, "a band above the floor must survive"
    assert "Payer low" not in companies, "a band below the floor must be dropped"
    assert any(c.startswith("Nopay") for c in companies), \
        "postings with no stated band must NOT be filtered out"


def test_no_floor_means_no_salary_filtering(db):
    _seed(30, tenant="nopay")
    _seed_paid("low", comp={"min": 60000, "max": 70000, "currency": "USD"})
    result = feed.build_feed("career-pages", _payload())
    assert "Payer low" in {j["company"] for j in result["jobs"]}


def test_an_hourly_band_is_compared_annualized(db):
    """$60/hr is above a $100k floor; comparing the raw 60 would drop it."""
    _seed(30, tenant="nopay")
    _seed_paid("hourly", comp={"min": 60, "max": 75, "currency": "USD",
                               "interval": "hourly"})
    result = feed.build_feed("career-pages", _payload(min_salary=100000))
    assert "Payer hourly" in {j["company"] for j in result["jobs"]}
