"""Observability: the metric definitions must not drift from the dashboard.

Each metric is defined by the query that produces it. These tests seed a known
inventory and assert the derived numbers, so a definition change that alters a
dashboard number is a test failure rather than a quiet shift.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from services import job_content_store as store
from services import job_inventory_service as inv
from services import tech_feed_metrics as m

NOW = datetime.now(timezone.utc)


def _fresh() -> datetime:
    """Wall-clock now. Module-level NOW goes stale if the suite runs >5 min."""
    return datetime.now(timezone.utc)
COLLECTIONS = ("job_postings", "job_source_state", "job_ingestion_runs",
               "companies", "company_ats_endpoints", "ingest_runner_heartbeat",
               "tech_feed_searches", "tech_feed_metrics")


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


def _post(sjid, *, title="Backend Engineer", company="Acme", tenant="acme",
          ats="greenhouse", desc="Python and AWS.", days=1):
    inv.upsert_posting({
        "source": "career-pages", "source_job_id": sjid,
        "url": f"https://boards.greenhouse.io/{tenant}/jobs/{sjid}",
        "company": company, "title": title, "location": ["Austin, TX"],
        "description": desc, "cost_class": "free", "ats": ats,
        "tenant": tenant, "posted_at": NOW - timedelta(days=days),
    }, now=NOW)


class TestInventoryMetrics:
    def test_active_and_totals(self, db):
        _post("a"); _post("b")
        got = m.inventory_metrics(db)
        assert got["active_total"] == 2
        assert got["postings_total"] == 2

    def test_duplicate_rate_counts_EXCESS_rows_not_group_members(self, db):
        """Two copies of one job is ONE duplicate, not two."""
        _post("d1", title="Backend Engineer")
        _post("d2", title="Backend Engineer")
        _post("d3", title="Backend Engineer")
        got = m.inventory_metrics(db)
        assert got["duplicate_groups"] == 1
        assert got["duplicate_excess_rows"] == 2      # 3 rows, 1 real job
        assert got["duplicate_rate"] == pytest.approx(2 / 3, abs=0.01)

    def test_missing_field_rate_is_a_MISSING_rate(self, db):
        _post("s1")           # no compensation
        got = m.inventory_metrics(db)
        assert got["missing_field_rate"]["salary"] == 1.0
        assert got["missing_field_rate"]["locations"] == 0.0

    def test_employer_name_conflict_rate(self, db):
        """One ATS tenant published under two employer names."""
        _post("c1", company="Boeing", tenant="boeing", ats="workday")
        _post("c2", company="M.J. Williams Company", tenant="boeing",
              ats="workday")
        _post("c3", company="Acme", tenant="acme")
        got = m.inventory_metrics(db)
        assert got["employer_name_conflict_tenants"] == 1
        assert got["employer_name_conflict_rate"] == pytest.approx(2 / 3, abs=0.01)

    def test_unknown_posting_date_rate(self, db):
        _post("k1")
        inv.upsert_posting({
            "source": "icims", "source_job_id": "u1",
            "url": "https://careers-x.icims.com/jobs/1", "company": "X",
            "title": "Backend Engineer", "location": ["Austin, TX"],
            "description": "Python.", "cost_class": "free", "ats": "icims",
            "tenant": "x", "posted_at": None}, now=NOW)
        got = m.inventory_metrics(db)
        assert got["unknown_posting_date_rate"] == pytest.approx(0.5, abs=0.01)

    def test_no_postings_does_not_divide_by_zero(self, db):
        got = m.inventory_metrics(db)
        assert got["active_total"] == 0
        assert got["duplicate_rate"] == 0.0
        assert got["missing_field_rate"]["salary"] == 0.0


class TestIngestionMetrics:
    def test_a_stale_source_raises_an_alarm(self, db):
        db.job_source_state.insert_one({
            "source": "workday", "shard": "s000",
            "last_success_at": NOW - timedelta(days=2), "retry_count": 0})
        got = m.ingestion_metrics(db)
        assert got["sources"][0]["stale"] is True
        assert any("stale" in a for a in got["alarms"])

    def test_a_fresh_source_does_not(self, db):
        db.job_source_state.insert_one({
            "source": "workday", "shard": "s000",
            "last_success_at": NOW - timedelta(minutes=3), "retry_count": 0})
        db.ingest_runner_heartbeat.insert_one({"updated_at": _fresh()})
        got = m.ingestion_metrics(db)
        assert got["sources"][0]["stale"] is False
        assert got["alarms"] == []

    def test_a_missing_heartbeat_is_an_alarm_not_a_silence(self, db):
        got = m.ingestion_metrics(db)
        assert got["ingest_runner_healthy"] is False
        assert any("heartbeat" in a for a in got["alarms"])

    def test_shards_in_backoff_are_counted(self, db):
        db.job_source_state.insert_many([
            {"source": "icims", "shard": "s000", "retry_count": 3,
             "last_success_at": NOW},
            {"source": "icims", "shard": "s001", "retry_count": 0,
             "last_success_at": NOW}])
        got = m.ingestion_metrics(db)
        assert got["sources"][0]["shards_in_backoff"] == 1


class TestCoverageMetrics:
    def test_endpoint_validation_rate(self, db):
        db.company_ats_endpoints.insert_many([
            {"vendor": "greenhouse", "status": "validated"},
            {"vendor": "greenhouse", "status": "validated"},
            {"vendor": "lever", "status": "dead"},
            {"vendor": "lever", "status": "empty"}])
        got = m.coverage_metrics(db)
        assert got["endpoint_validation_rate"] == 0.5
        assert got["validated_endpoints_by_vendor"] == {"greenhouse": 2}

    def test_source_yield_joins_boards_to_postings(self, db):
        db.company_ats_endpoints.insert_one(
            {"vendor": "greenhouse", "status": "validated"})
        _post("y1"); _post("y2")
        got = m.coverage_metrics(db)
        y = got["source_yield_by_vendor"]["greenhouse"]
        assert y["boards"] == 1 and y["active_postings"] == 2
        assert y["jobs_per_board"] == 2.0

    def test_discovery_throughput_windows(self, db):
        db.companies.insert_many([
            {"company_id": "a", "last_discovered_at": NOW - timedelta(hours=2)},
            {"company_id": "b", "last_discovered_at": NOW - timedelta(days=4)},
            {"company_id": "c"}])
        got = m.coverage_metrics(db)
        assert got["discovery_throughput_24h"] == 1
        assert got["discovery_throughput_7d"] == 2
        assert got["employer_universe_total"] == 3


class TestSearchMetrics:
    def test_no_sessions_reports_zero_not_a_crash(self, db):
        assert m.search_metrics(db) == {"searches": 0}

    def test_latency_percentiles_and_zero_result_rate(self, db):
        db.tech_feed_searches.insert_many([
            {"created_at": NOW, "total": 10, "diagnostics": {"total_ms": 100},
             "warnings": []},
            {"created_at": NOW, "total": 0, "diagnostics": {"total_ms": 900},
             "warnings": []},
        ])
        got = m.search_metrics(db)
        assert got["searches"] == 2
        assert got["search_max_ms"] == 900
        assert got["search_zero_result_rate"] == 0.5


class TestAlarms:
    def test_a_healthy_system_raises_nothing(self, db):
        db.job_source_state.insert_one({
            "source": "workday", "shard": "s000",
            "last_success_at": NOW, "retry_count": 0})
        db.ingest_runner_heartbeat.insert_one({"updated_at": _fresh()})
        db.company_ats_endpoints.insert_many(
            [{"vendor": "greenhouse", "status": "validated", "tenant": f"t{i}"}
             for i in range(8)])
        db.companies.insert_many([
            {"company_id": f"c{i}", "last_discovered_at": NOW} for i in range(150)])
        _post("h1")
        assert m.alarms(db) == []

    def test_an_employer_name_conflict_epidemic_alarms(self, db):
        db.ingest_runner_heartbeat.insert_one({"updated_at": _fresh()})
        _post("c1", company="Boeing", tenant="boeing", ats="workday")
        _post("c2", company="M.J. Williams", tenant="boeing", ats="workday")
        assert any("employer-name conflicts" in a for a in m.alarms(db))

    def test_a_dead_heartbeat_alarms(self, db):
        assert any("heartbeat" in a for a in m.alarms(db))


class TestOwnershipMetrics:
    """The two coverage numbers, published side by side.

    `coverage_metrics` counts endpoints that ANSWERED. `ownership` counts
    sources whose owner is proven. Reporting only the first is how 43% came to
    mean something it did not.
    """

    def test_the_snapshot_carries_both(self, mock_db):
        from services import tech_feed_metrics as tfm

        got = tfm.snapshot(mock_db)
        assert "coverage" in got
        assert "ownership" in got

    def test_ownership_reports_the_registry_and_the_sources(self, mock_db):
        from services import career_source_service as css
        from services import employer_registry as er
        from services import tech_feed_metrics as tfm

        mock_db[er.EMPLOYERS].delete_many({})
        mock_db[css.CAREER_SOURCES].delete_many({})
        er.upsert_employer(er.build_employer(
            display_name="Stripe", canonical_domain="stripe.com",
            cohorts=["private_growth"], employer_categories=["fintech"]))
        css.upsert_source(css.build_source(
            employer_id="e1", ats_vendor="greenhouse", tenant="stripe",
            ownership_evidence=[css.evidence(css.SIGNAL_DOMAIN_LINK)],
            content_validation_evidence=[css.evidence("content_fetch")],
            job_count_last=10))

        got = tfm.ownership_metrics(mock_db)
        assert got["employers"]["total"] == 1
        assert got["employers"]["by_cohort"]["private_growth"] == 1
        assert got["career_sources"]["by_state"][css.STATE_VALIDATED] == 1
        assert got["career_sources"]["ownership_verified"] == 1

    def test_a_missing_collection_degrades_rather_than_raising(self, mock_db):
        """A metrics snapshot must never be the thing that takes a dashboard
        down, and a registry that has not been backfilled yet is a normal
        state, not an error."""
        from services import tech_feed_metrics as tfm

        got = tfm.ownership_metrics(mock_db)
        assert isinstance(got, dict)
