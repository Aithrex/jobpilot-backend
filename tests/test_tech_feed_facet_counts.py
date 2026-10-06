"""Exact facet counts over the SAME `build_query()` filter retrieval uses.

The per-level (stored `seniority`) facet, `work_mode`, `country`, and the new
cumulative `date_window` counts must be TRUE DB values — what clicking the
filter returns — not the `CANDIDATE_LIMIT`-windowed `build_facets` estimate.
Everything else (employer/role_family/...) stays window-based but is labelled
honestly via `facets["_exactness"]`.

These are Part E tests 1-5 of the match-accuracy design. Test 1 is the
regression guard: it FAILS on the old windowed `build_facets` and PASSES after
the counting fix. Style mirrors `tests/test_tech_feed_search.py` (a `db`
fixture clearing COLLECTIONS, a wall-tracking NOW, seeding through `inv`).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from services import fit_score
from services import job_content_store as store
from services import job_inventory_service as inv
from services import tech_feed_service as tfs

#: Wall-tracking seeding clock — same rationale as test_tech_feed_search.NOW:
#: `build_query`'s freshness cutoff is computed from the real clock, so the
#: fixture clock must track it or seeded rows drift out of the window.
NOW = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0,
                                         microsecond=0)

COLLECTIONS = ("job_postings", "job_source_state", "job_ingestion_runs",
               "tech_feed_searches", "ingest_runner_heartbeat", "companies",
               "company_ats_endpoints")

#: A core-tech title so every seeded row clears the default `tech_scope=core`
#: filter via the legacy `role_family` branch (no `tech` subdoc on these rows).
SWE_TITLE = "Backend Engineer"
SWE_JD = ("Backend Engineer. Build services with Python, AWS and PostgreSQL. "
          "3+ years of experience.")


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


def seed(*, sjid, seniority=None, work_mode=None, location="Austin, TX, USA",
         days=2, confidence=None, first_seen_days=None, title=SWE_TITLE,
         company="Acme Inc", tenant="acme"):
    """Seed one active posting whose STORED fields we control directly.

    Goes through `upsert_posting`/`normalize_posting` (so the row is a valid
    `job_postings` doc the whole pipeline accepts) but with `seniority`,
    `work_mode`, `posted_at_confidence` passed through verbatim and NO parse
    step — a re-parse would re-derive `seniority` from the title and defeat the
    control. `now` backdates `first_seen_at` so undated rows can age out of a
    window.
    """
    posted_at = None if confidence == inv.CONFIDENCE_UNKNOWN else NOW - timedelta(days=days)
    raw = {
        "source": "career-pages", "source_job_id": sjid,
        "url": f"https://boards.greenhouse.io/{tenant}/jobs/{sjid}",
        "company": company, "title": title, "location": [location],
        "description": SWE_JD, "employment_type": "Full time",
        "cost_class": "free", "source_quality": "direct", "industry": "tech",
        "ats": "greenhouse", "tenant": tenant, "posted_at": posted_at,
    }
    if seniority is not None:
        raw["seniority"] = seniority
    if work_mode is not None:
        raw["work_mode"] = work_mode
    if confidence is not None:
        raw["posted_at_confidence"] = confidence
    stamp = NOW - timedelta(days=first_seen_days) if first_seen_days is not None else NOW
    inv.upsert_posting(raw, now=stamp)
    return sjid


def search(**over):
    body = {"tech_scope": ["core"], "page_size": 25, "personalize": False,
            "posted_within_days": 90}
    body.update(over)
    return tfs.search(body)


def _count(req_over, extra=None):
    """Ground truth: count_documents over build_query(normalize_request(body))."""
    body = {"tech_scope": ["core"], "page_size": 25, "personalize": False,
            "posted_within_days": 90}
    body.update(req_over)
    q = tfs.build_query(tfs.normalize_request(body))
    if extra:
        q = {"$and": [q, extra]}
    return inv._postings().count_documents(q)


def _facet_map(facets, name):
    return {e["value"]: e["count"] for e in facets.get(name, [])}


# ---------------------------------------------------------------------------
# Test 1 — true match set exceeds CANDIDATE_LIMIT (the regression guard)
# ---------------------------------------------------------------------------

class TestExactSeniorityBeatsTheWindow:
    def test_seniority_facet_is_the_true_db_count_not_the_window(self, db, monkeypatch):
        # Window set far below the match set: on the OLD windowed build_facets
        # the seniority counts would be bounded by 10, which this asserts
        # against — the regression guard.
        monkeypatch.setattr(tfs, "CANDIDATE_LIMIT", 10)
        for i in range(25):
            seed(sjid=f"e{i}", seniority="entry")
        for i in range(15):
            seed(sjid=f"s{i}", seniority="senior")
        for i in range(8):
            seed(sjid=f"u{i}", seniority="unknown")

        res = search()
        sen = _facet_map(res["facets"], "seniority")

        # Exact true-DB counts, not the <=10 windowed counts.
        assert sen.get("entry") == _count({}, {"seniority": "entry"}) == 25
        assert sen.get("senior") == _count({}, {"seniority": "senior"}) == 15
        assert sen.get("unknown") == _count({}, {"seniority": "unknown"}) == 8
        assert max(sen.values()) > 10  # would be <=10 on the windowed estimate

        assert res["facets"]["_exactness"]["seniority"]["exact"] is True
        assert res["facets"]["_exactness"]["seniority"]["basis"] == \
            "full_filter_aggregation"

        # Total is unchanged and still honest.
        assert res["total"] == 48
        assert res["diagnostics"]["total_is_exact"] is True


# ---------------------------------------------------------------------------
# Test 2 — date-window exactness + undated ride-along
# ---------------------------------------------------------------------------

class TestDateWindowExactness:
    def test_cumulative_windows_and_undated_ride_along(self, db):
        # Dated rows at known offsets.
        seed(sjid="d0", seniority="entry", days=0)      # today
        seed(sjid="d2", seniority="entry", days=2)      # <=3d
        seed(sjid="d5", seniority="entry", days=5)      # <=7d
        seed(sjid="d10", seniority="entry", days=10)    # <=14d
        seed(sjid="d20", seniority="entry", days=20)    # <=30d
        seed(sjid="d40", seniority="entry", days=40)    # <=45d
        seed(sjid="d80", seniority="entry", days=80)    # <=90d
        # Undated rows: ride along on `first_seen_at`. One fresh, one aged out
        # of the 7-day window (first seen 10 days ago).
        seed(sjid="u_fresh", seniority="entry",
             confidence=inv.CONFIDENCE_UNKNOWN, first_seen_days=1)
        seed(sjid="u_old", seniority="entry",
             confidence=inv.CONFIDENCE_UNKNOWN, first_seen_days=10)

        res = search()
        windows = {e["days"]: e["count"] for e in res["facets"]["date_window"]}

        # Each cumulative window equals its own build_query ground truth —
        # undated ride-along included by construction.
        for days in tfs.FACET_DATE_WINDOWS:
            assert windows[days] == _count({"posted_within_days": days}), \
                f"window {days}d disagreed with its own build_query count"

        # Hand-checked: dated rows are cumulative, both undated rows ride along
        # once their first_seen_at is inside the window.
        assert windows[1] == 1 + 1       # d0 + u_fresh
        assert windows[3] == 2 + 1       # d0,d2 + u_fresh
        assert windows[7] == 3 + 1       # d0,d2,d5 + u_fresh (u_old aged out)
        assert windows[14] == 4 + 2      # +d10, +u_old now in window
        assert windows[90] == 7 + 2      # every dated row + both undated

        assert res["facets"]["date_window_is_exact"] is True
        assert res["facets"]["_exactness"]["date_window"]["exact"] is True
        assert res["facets"]["_exactness"]["date_window"]["basis"] == \
            "full_filter_counts"


# ---------------------------------------------------------------------------
# Test 3 — unknown rides along in the count (consistent with retrieval)
# ---------------------------------------------------------------------------

class TestUnknownRidesAlong:
    def test_entry_filter_counts_entry_plus_unknown(self, db):
        for i in range(6):
            seed(sjid=f"e{i}", seniority="entry")
        for i in range(4):
            seed(sjid=f"u{i}", seniority="unknown")
        seed(sjid="snr", seniority="senior")   # must NOT be counted

        res = search(seniority=["entry"])
        sen = _facet_map(res["facets"], "seniority")

        # The filter admits entry + unknown; the facet count mirrors the SAME
        # $in that retrieval ran.
        ground = _count({"seniority": ["entry"]})      # count over build_query $in
        assert ground == 10
        assert sen.get("entry", 0) + sen.get("unknown", 0) == ground == 10
        assert "senior" not in sen
        assert res["facets"]["_exactness"]["seniority"]["exact"] is True


# ---------------------------------------------------------------------------
# Test 3b — missing-seniority-field edge (the B.5a $ifNull proof lock)
# ---------------------------------------------------------------------------

class TestMissingSeniorityField:
    def _seed_fieldless(self, sjid):
        """A valid active row with the `seniority` field OMITTED (not the
        string "unknown")."""
        seed(sjid=sjid, seniority="unknown")
        inv._postings().update_one({"source_job_id": sjid},
                                   {"$unset": {"seniority": "", "tech": ""}})

    def test_fieldless_row_is_excluded_by_an_entry_filter(self, db):
        seed(sjid="e0", seniority="entry")
        self._seed_fieldless("miss")

        res = search(seniority=["entry"])
        sen = _facet_map(res["facets"], "seniority")

        # $in is a non-match on an ABSENT field, so the row is in neither total
        # nor any bucket.
        assert res["total"] == 1
        assert sum(sen.values()) == 1
        assert "unknown" not in sen

    def test_fieldless_row_buckets_as_unknown_with_no_filter(self, db):
        seed(sjid="e0", seniority="entry")
        seed(sjid="s0", seniority="senior")
        self._seed_fieldless("miss")

        res = search()   # no seniority filter
        sen = _facet_map(res["facets"], "seniority")

        # $ifNull buckets the field-less row as `unknown` — its real retrieval
        # status — and sum(buckets) == total holds below COUNT_LIMIT.
        assert sen.get("unknown") == 1
        assert res["diagnostics"]["total_is_exact"] is True
        assert sum(sen.values()) == res["total"] == 3


# ---------------------------------------------------------------------------
# Test 4 — windowed facets stay labelled non-exact (and still correct)
# ---------------------------------------------------------------------------

class TestWindowedFacetsLabelled:
    def test_employer_and_role_family_stay_windowed(self, db):
        seed(sjid="a1", seniority="entry", company="Acme Inc", tenant="acme")
        seed(sjid="b1", seniority="entry", company="Beta LLC", tenant="beta")

        res = search()
        ex = res["facets"]["_exactness"]

        for facet in ("employer", "role_family", "role_specialty", "tech_scope",
                      "ats", "source", "employment_type", "salary_published",
                      "employer_kind"):
            assert ex[facet]["exact"] is False
            assert ex[facet]["basis"] == "ranked_window"
            assert ex[facet]["window"] == tfs.CANDIDATE_LIMIT

        # Still correct window-based counts — no regression to the employer facet.
        employers = {e["value"] for e in res["facets"]["employer"]}
        assert "Acme Inc" in employers and "Beta LLC" in employers

        # The exact facets are labelled exact.
        assert ex["seniority"]["exact"] is True
        assert ex["work_mode"]["exact"] is True
        assert ex["country"]["exact"] is True


# ---------------------------------------------------------------------------
# Test 5 — maxTimeMS fallback (never 0, never raise)
# ---------------------------------------------------------------------------

class TestTimeoutFallback:
    def test_aggregation_timeout_falls_back_to_the_windowed_count(self, db, monkeypatch):
        for i in range(6):
            seed(sjid=f"e{i}", seniority="entry")
        for i in range(3):
            seed(sjid=f"s{i}", seniority="senior")

        # Make the exact $facet aggregation raise a timeout. The windowed
        # build_facets value must remain, flagged exact:false — not 0, not a 500.
        real_aggregate = inv._postings().aggregate

        def boom(pipeline, *args, **kwargs):
            if any("$facet" in stage for stage in pipeline):
                raise RuntimeError("ExceededTimeLimit")
            return real_aggregate(pipeline, *args, **kwargs)

        monkeypatch.setattr(inv._postings(), "aggregate", boom)

        res = search()
        sen = _facet_map(res["facets"], "seniority")

        assert res["facets"]["_exactness"]["seniority"]["exact"] is False
        assert res["facets"]["_exactness"]["seniority"]["basis"] == \
            "ranked_window_timeout_fallback"
        # Fell back to the windowed count — a real number, never 0, never raised.
        assert sum(sen.values()) > 0
