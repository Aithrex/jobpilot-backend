"""Guest freshness contract v5 + cost_class paid-source exclusion.

The guest scan is the product's front door, so it is held to a TIGHTER bar
than the authed feeds:

  * a job may only be served if its posting date is KNOWN and within
    GUEST_MAX_AGE_DAYS (7). An unknown date is never rewritten to now();
    it is dropped.
  * an explicitly closed/inactive row is dropped.
  * the filter runs BEFORE scoring, and again on every snapshot read, so a
    cached result can never serve what a fresh scan would reject.
  * fewer than guest_jobs_max survivors is a SUCCESS. Nothing is padded.
  * an anonymous guest never spends a metered ("paid" cost_class) source,
    even when the JSearch/RapidAPI key IS configured.

Fully offline: mongomock via tests/_vg_support, every fetcher monkeypatched,
no LLM anywhere (deterministic scoring only).
"""
import sys
import uuid as _uuid
from datetime import datetime, timedelta, timezone

import pytest

from services import guest_service
from services import platform_config_service as pconfig
import services.mnc_jobs_service as mnc
from utils import db_connect

COLLECTIONS = (
    "guest_sessions",
    "guest_quota_subjects",
    "guest_global_counters",
    "job_match_snapshots",
    "platform_config",
    "resume_assets",
    "resume_jobs",
    "sponsorship_signals",
)


@pytest.fixture(autouse=True)
def _secrets(monkeypatch):
    monkeypatch.setenv("GUEST_SIGNAL_HMAC_KEY", "unit-test-signal-key")
    monkeypatch.setenv("GUEST_DOWNLOAD_TOKEN_SECRET", "unit-test-download-secret")
    monkeypatch.delenv("GUEST_IP_HASH_SALT", raising=False)


@pytest.fixture()
def guest_db():
    from utils.security import get_rate_limiter
    db = db_connect.DBConnect().get_db()
    for c in COLLECTIONS:
        db[c].delete_many({})
    pconfig.invalidate_cache()
    get_rate_limiter()._requests.clear()
    yield db
    pconfig.invalidate_cache()
    get_rate_limiter()._requests.clear()


def _structured(skills=("Python", "Go", "SQL", "AWS")):
    return {
        "contact": {"name": "Jordan Alvarez", "email": "jordan@example.com",
                    "phone": "(555) 010-7788", "linkedin": "", "github": ""},
        "summary": "Backend engineer.",
        "skills": {"languages": list(skills)},
        "experience": [{"title": "Backend Engineer", "company": "Acme",
                        "dates": "2020 - 2024", "bullets": ["Built APIs"]}],
        "education": [], "projects": [], "certifications": [],
    }


def _session_with_resume():
    s = guest_service.create_or_get_session(None, None, "iphash")
    guest_service.attach_parsed_resume(
        s["guest_id"], _structured(), "freshness test resume " * 5, "resume.pdf")
    return s["guest_id"]


def _row(title, *, days_ago=1, source="Greenhouse", company=None, **extra):
    row = {
        "title": title,
        "company": company or f"Co {_uuid.uuid4().hex[:6]}",
        "location": "US",
        "source": source,
        "description": "Python Go SQL AWS backend services",
        "url": f"https://jobs.example/{_uuid.uuid4().hex[:8]}",
        "apply_link": "",
    }
    if days_ago is not None:
        row["days_ago"] = days_ago
    row.update(extra)
    return row


def _scoring_spy(monkeypatch):
    """Record every title the scorer actually sees, so 'rejected BEFORE
    scoring' is asserted rather than inferred from the output."""
    # ADR-002: guest scoring moved off jd_match_scorer (resume-derived
    # denominator) onto the shared fit engine (JD-derived denominator).
    import services.fit_score as fit_score
    seen = []
    real = fit_score.score_one

    def spy(**kwargs):
        seen.append(kwargs.get("title"))
        return real(**kwargs)

    monkeypatch.setattr(fit_score, "score_one", spy)
    return seen


# ===========================================================================
# The filter itself
# ===========================================================================

class TestFreshnessFilter:
    def test_default_window_is_seven_days(self):
        assert guest_service.GUEST_MAX_AGE_DAYS == 7

    def test_boundary_seven_accepted_eight_rejected(self):
        rows = [_row("Exactly Seven", days_ago=7), _row("Eight", days_ago=8)]
        kept = [r["title"] for r in guest_service.filter_fresh_for_guest(rows)]
        assert kept == ["Exactly Seven"]

    def test_unknown_date_is_rejected_and_never_becomes_today(self):
        row = _row("No Date", days_ago=None)
        assert "days_ago" not in row
        assert guest_service._row_days_ago(row) is None
        assert guest_service.filter_fresh_for_guest([row]) == []
        # The row was NOT mutated into a fresh posting on its way out.
        assert "days_ago" not in row
        assert guest_service._row_days_ago({"days_ago": "not-a-number"}) is None
        assert guest_service._row_days_ago({"days_ago": None}) is None

    def test_closed_and_inactive_markers_are_rejected(self):
        rows = [
            _row("Open"),
            _row("Closed Status", status="closed"),
            _row("Inactive Status", job_status="Inactive"),
            _row("Not Active", state="not active"),
            _row("Inactive Flag", active=False),
            _row("Closed Flag", closed=True),
            _row("Expired Flag", is_expired=True),
        ]
        kept = [r["title"] for r in guest_service.filter_fresh_for_guest(rows)]
        assert kept == ["Open"]

    def test_open_markers_and_absent_status_are_kept(self):
        rows = [_row("Explicitly Open", status="open", active=True), _row("No Status")]
        assert len(guest_service.filter_fresh_for_guest(rows)) == 2

    def test_extra_age_ages_stored_rows(self):
        rows = [_row("Six Days", days_ago=6)]
        assert len(guest_service.filter_fresh_for_guest(rows, extra_age_days=1)) == 1
        assert guest_service.filter_fresh_for_guest(rows, extra_age_days=2) == []


# ===========================================================================
# compute_guest_matches — rejection happens BEFORE scoring
# ===========================================================================

class TestComputeRejectsBeforeScoring:
    def test_twenty_six_day_old_workday_job_never_reaches_the_scorer(
            self, guest_db, monkeypatch):
        pconfig.update_guest_config({"guest_jobs_min_score": 20}, "admin@test")
        gid = _session_with_resume()
        seen = _scoring_spy(monkeypatch)

        rows = [
            _row("Stale Workday Engineer", days_ago=26, source="Workday Direct"),
            _row("Fresh Backend Engineer", days_ago=2),
        ]
        monkeypatch.setattr(guest_service, "_fetch_free_jobs", lambda s: rows)

        result = guest_service.compute_guest_matches(gid)

        assert seen == ["Fresh Backend Engineer"]      # scorer never saw the stale row
        titles = [c["title"] for c in result["jobs"]]
        assert titles == ["Fresh Backend Engineer"]
        assert result["sources_report"]["dropped_stale"] == 1
        assert result["sources_report"]["max_age_days"] == 7
        # It is not hiding in the pool either.
        snap = guest_db.job_match_snapshots.find_one({"guest_id": gid})
        assert [c["title"] for c in snap["pool"]] == ["Fresh Backend Engineer"]

    def test_unknown_date_row_never_reaches_the_scorer(self, guest_db, monkeypatch):
        pconfig.update_guest_config({"guest_jobs_min_score": 20}, "admin@test")
        gid = _session_with_resume()
        seen = _scoring_spy(monkeypatch)

        rows = [_row("Undated Engineer", days_ago=None), _row("Dated Engineer")]
        monkeypatch.setattr(guest_service, "_fetch_free_jobs", lambda s: rows)

        result = guest_service.compute_guest_matches(gid)
        assert seen == ["Dated Engineer"]
        assert [c["title"] for c in result["jobs"]] == ["Dated Engineer"]

    def test_boundary_days_through_the_full_compute_path(self, guest_db, monkeypatch):
        pconfig.update_guest_config({"guest_jobs_min_score": 20}, "admin@test")
        gid = _session_with_resume()
        rows = [_row("Seven Day Engineer", days_ago=7),
                _row("Eight Day Engineer", days_ago=8)]
        monkeypatch.setattr(guest_service, "_fetch_free_jobs", lambda s: rows)

        result = guest_service.compute_guest_matches(gid)
        assert [c["title"] for c in result["jobs"]] == ["Seven Day Engineer"]

    def test_closed_job_is_rejected_through_the_full_compute_path(
            self, guest_db, monkeypatch):
        pconfig.update_guest_config({"guest_jobs_min_score": 20}, "admin@test")
        gid = _session_with_resume()
        rows = [_row("Closed Engineer", status="closed"), _row("Live Engineer")]
        monkeypatch.setattr(guest_service, "_fetch_free_jobs", lambda s: rows)

        result = guest_service.compute_guest_matches(gid)
        assert [c["title"] for c in result["jobs"]] == ["Live Engineer"]

    def test_fewer_than_five_is_a_success_not_padded(self, guest_db, monkeypatch):
        pconfig.update_guest_config(
            {"guest_jobs_min_score": 20, "guest_jobs_max": 5}, "admin@test")
        gid = _session_with_resume()
        rows = [
            _row("Fresh One", days_ago=1),
            _row("Fresh Two", days_ago=3),
            _row("Stale One", days_ago=12),
            _row("Stale Two", days_ago=30),
            _row("Undated", days_ago=None),
            _row("Closed", status="closed"),
        ]
        monkeypatch.setattr(guest_service, "_fetch_free_jobs", lambda s: rows)

        result = guest_service.compute_guest_matches(gid)
        assert len(result["jobs"]) == 2            # exactly the survivors
        assert result["pool_total"] == 2
        assert result["locked_count"] == 0
        assert "empty_reason" not in result        # 2 < 5 is NOT an error
        assert result["total_considered"] == 2
        # And the cached read agrees — still 2, still unpadded.
        assert len(guest_service.get_cached_matches(gid)["jobs"]) == 2


# ===========================================================================
# Snapshot / cache reads re-apply the contract
# ===========================================================================

class TestSnapshotReadReappliesFilter:
    def _store(self, gid, cards):
        guest_service._store_matches(gid, cards, 20, pool=list(cards))

    def test_stored_card_that_has_aged_out_is_not_served(self, guest_db, monkeypatch):
        gid = _session_with_resume()
        mid = _uuid.uuid4().hex
        self._store(gid, [{"title": "Fresh When Stored", "match_score": 90,
                           "match_id": mid, "days_ago": 1}])
        # Sanity: served today.
        assert len(guest_service.get_cached_matches(gid)["jobs"]) == 1

        # Advance the clock 8 days. The snapshot doc is untouched on disk —
        # only the READ re-applies the age contract.
        real_now = guest_service._now()
        monkeypatch.setattr(guest_service, "_now",
                            lambda: real_now + timedelta(days=8))
        doc = guest_service._cache_doc_for_guest(gid)
        assert doc is not None                      # the doc still exists...
        assert doc["jobs"] == []                    # ...but serves nothing
        assert doc["pool"] == []
        # Stored bytes are untouched: this is a read-time contract.
        raw = guest_db.job_match_snapshots.find_one({"guest_id": gid})
        assert raw["jobs"][0]["days_ago"] == 1

    def test_same_day_snapshot_with_a_stale_card_is_filtered_on_read(self, guest_db):
        gid = _session_with_resume()
        self._store(gid, [
            {"title": "Stale Legacy Card", "match_score": 95,
             "match_id": _uuid.uuid4().hex, "days_ago": 26},
            {"title": "Fresh Card", "match_score": 80,
             "match_id": _uuid.uuid4().hex, "days_ago": 2},
        ])
        cached = guest_service.get_cached_matches(gid)
        assert [c["title"] for c in cached["jobs"]] == ["Fresh Card"]
        assert cached["pool_total"] == 1

    def test_undated_legacy_card_is_not_served(self, guest_db):
        gid = _session_with_resume()
        self._store(gid, [{"title": "Undated Legacy Card", "match_score": 95,
                           "match_id": _uuid.uuid4().hex}])
        cached = guest_service.get_cached_matches(gid)
        assert cached["jobs"] == []

    def test_closed_stored_card_is_not_served(self, guest_db):
        gid = _session_with_resume()
        self._store(gid, [{"title": "Closed Card", "match_score": 95,
                           "match_id": _uuid.uuid4().hex, "days_ago": 1,
                           "status": "closed"}])
        assert guest_service.get_cached_matches(gid)["jobs"] == []


# ===========================================================================
# Freshness inflation: edit timestamps are no longer posting dates
# ===========================================================================

class TestNoFreshnessInflation:
    def test_greenhouse_ignores_updated_at(self, monkeypatch):
        import services.career_pages_service as cps

        class _Resp:
            def raise_for_status(self):
                return None

            def json(self):
                return {"jobs": [
                    {"id": 1, "title": "Backend Engineer",
                     "absolute_url": "https://boards.example/1",
                     "location": {"name": "Remote - United States"},
                     # No first_published: the posting date is UNKNOWN. The
                     # recent updated_at must NOT stand in for it.
                     "updated_at": datetime.now(timezone.utc).isoformat()},
                    {"id": 2, "title": "Platform Engineer",
                     "absolute_url": "https://boards.example/2",
                     "location": {"name": "Remote - United States"},
                     "first_published": (
                         datetime.now(timezone.utc) - timedelta(days=1)
                     ).isoformat(),
                     "updated_at": datetime.now(timezone.utc).isoformat()},
                ]}

        monkeypatch.setattr(cps._SESSION, "get", lambda *a, **k: _Resp())
        rows = cps._fetch_greenhouse(
            {"display_name": "Acme", "ats": "greenhouse", "slug": "acme",
             "industry": "big_tech", "host": "", "extra": {}})
        by_title = {r["title"]: r for r in rows}
        assert by_title["Backend Engineer"]["date_posted"] == ""
        assert by_title["Backend Engineer"]["days_ago"] is None
        assert by_title["Platform Engineer"]["days_ago"] == 1
        # Unknown-date rows are dropped by the guest contract, never aged to 0.
        kept = [r["title"] for r in guest_service.filter_fresh_for_guest(rows)]
        assert kept == ["Platform Engineer"]


# ===========================================================================
# cost_class — guests never spend a metered source
# ===========================================================================

class TestCostClass:
    def test_engine_cost_classes(self):
        assert mnc.cost_class_for_engine("aggregator") == mnc.COST_CLASS_PAID
        for free_engine in ("career_pages", "enterprise", "workday", "orc", "uber"):
            assert mnc.cost_class_for_engine(free_engine) == mnc.COST_CLASS_FREE
        # Unknown/new engines are treated as PAID until opted in.
        assert mnc.cost_class_for_engine("brand_new_metered_api") == mnc.COST_CLASS_PAID
        assert mnc.cost_class_for_engine(None) == mnc.COST_CLASS_PAID

    @pytest.fixture()
    def paid_spy(self, monkeypatch, mock_db):
        """JSearch key CONFIGURED (the real deployment state) with every other
        fetcher stubbed out, so any aggregator call is unambiguous."""
        calls = []

        def _aggregator(row, terms):
            calls.append(row["slug"])
            return []

        monkeypatch.setattr(mnc, "_jsearch_configured", lambda: True)
        monkeypatch.setattr(mnc, "_fetch_aggregator", _aggregator)
        monkeypatch.setattr(mnc, "_fetch_board", lambda r: [])
        monkeypatch.setattr(mnc, "_fetch_enterprise", lambda r, terms: [])
        monkeypatch.setattr(mnc, "_fetch_workday", lambda r, terms: [])
        monkeypatch.setattr(mnc, "_fetch_orc", lambda r, terms: [])
        monkeypatch.setattr(mnc, "_fetch_uber", lambda r, terms: [])
        monkeypatch.setattr(mnc, "_enrich_details", lambda jobs: 0)
        monkeypatch.setattr(mnc, "_load_resume_profile", lambda email: (["python"], 3))
        mock_db[mnc.CACHE_COLLECTION].delete_many({})
        return calls

    def _payload(self, **extra):
        return {"titles": ["software engineer"], "industries": [], "companies": [],
                "location": "", "remote_only": False, "us_only": True,
                "force_refresh": True, "user_email": "", **extra}

    def test_authed_run_still_uses_paid_sources(self, paid_spy):
        result = mnc.run_mnc_search(self._payload())
        assert paid_spy, "authed runs keep aggregator coverage"
        assert "paid_sources_excluded" not in result["diagnostics"]

    def test_guest_safe_run_excludes_paid_sources_despite_configured_key(
            self, paid_spy):
        result = mnc.run_mnc_search(self._payload(exclude_paid=True))
        assert paid_spy == [], "a guest run must never spend a metered source"
        excluded = result["diagnostics"]["paid_sources_excluded"]
        assert excluded and all(isinstance(s, str) for s in excluded)

    def test_guest_safe_alias(self, paid_spy):
        mnc.run_mnc_search(self._payload(guest_safe=True))
        assert paid_spy == []

    def test_guest_scan_passes_exclude_paid(self, guest_db, monkeypatch):
        """The guest fan-out itself must set the flag — not just support it."""
        captured = {}

        def fake_run_mnc(payload, partial_cb=None):
            captured["payload"] = payload
            return {"jobs": []}

        monkeypatch.setattr(mnc, "run_mnc_search", fake_run_mnc)
        monkeypatch.setattr(
            "services.workday_jobs_service.run_workday_jobs_search",
            lambda payload: {"jobs": []})
        monkeypatch.setattr(
            "services.career_pages_service.run_career_pages_search",
            lambda payload: {"jobs": []})

        guest_service._fetch_free_jobs(_structured())
        assert captured["payload"]["exclude_paid"] is True


if __name__ == "__main__":  # allow `python tests/test_guest_freshness_v5.py`
    sys.exit(pytest.main([__file__, "-q"]))
