"""One measurement, read everywhere, and it always says how old it is.

`scripts/employer_coverage_report.py` reads five collections and classifies
23,782 employers. That is the right cost for an accountability artefact and
the wrong cost for a banner on a search page — so the banner used to show a
DIFFERENT, cheaper set of numbers, and the two disagreed. One of them said 43%.

The snapshot is how there comes to be one set of numbers. Freshness is part of
the payload rather than an assumption, because the numbers move on a crawl
cadence and a figure measured before a verification sweep is history.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from _vg_support import current_db
from services import coverage_snapshot as snap


def _payload(**over):
    base = {
        "technology_employer_denominator": 2624,
        "covered_technology_employers": 875,
        "employer_coverage_pct": 0.3335,
        "covered_career_sources": 2393,
        "ownership_verified_unvalidated_sources": 182,
        "candidate_technology_employers": 1749,
        "measured_at": datetime.now(timezone.utc),
        "source_revision": "abc1234",
    }
    base.update(over)
    return base


@pytest.fixture(autouse=True)
def clean():
    db = current_db()
    db[snap.COVERAGE_SNAPSHOTS].delete_many({})
    yield
    db[snap.COVERAGE_SNAPSHOTS].delete_many({})


class TestCompleteness:

    def test_a_complete_snapshot_stores_and_reads_back(self):
        assert snap.save(_payload())
        got = snap.latest()
        assert got["covered_technology_employers"] == 875
        assert got["source_revision"] == "abc1234"

    def test_an_INCOMPLETE_snapshot_is_refused(self):
        """A banner that renders `covered_technology_employers` as 0 because a
        field was missing is worse than a banner that says the measurement is
        unavailable."""
        assert snap.save(_payload(covered_technology_employers=None)) is None
        assert snap.latest() is None

    def test_no_snapshot_is_a_real_answer(self):
        assert snap.latest() is None


class TestFreshness:

    def test_a_fresh_snapshot_is_not_stale(self):
        snap.save(_payload())
        got = snap.latest()
        assert got["stale"] is False
        assert got["age_hours"] < 1

    def test_an_old_snapshot_is_stale_and_says_how_old(self):
        old = datetime.now(timezone.utc) - timedelta(hours=snap.STALE_AFTER_HOURS + 5)
        snap.save(_payload(measured_at=old))
        got = snap.latest()
        assert got["stale"] is True
        assert got["age_hours"] > snap.STALE_AFTER_HOURS

    def test_a_snapshot_with_no_timestamp_is_stale_not_fresh(self):
        """A measurement that cannot say when it was taken has to be presented
        as historical. Failing the other way is how a figure from last week
        renders as status."""
        got = snap.annotate({"measured_at": None})
        assert got["stale"] is True
        assert got["age_hours"] is None

    def test_an_iso_string_timestamp_is_understood(self):
        got = snap.annotate({"measured_at": "2026-09-02T00:00:00Z"})
        assert isinstance(got["age_hours"], float)


class TestHistory:

    def test_the_newest_snapshot_wins(self):
        older = datetime.now(timezone.utc) - timedelta(hours=10)
        snap.save(_payload(measured_at=older, covered_technology_employers=800))
        snap.save(_payload(covered_technology_employers=875))
        assert snap.latest()["covered_technology_employers"] == 875

    def test_history_is_retained_so_a_number_can_be_attributed(self):
        """"It was 33.4% at 3e0b4c9" is a claim somebody must be able to
        check."""
        for i in range(5):
            snap.save(_payload(
                measured_at=datetime.now(timezone.utc) - timedelta(hours=i),
                source_revision=f"rev{i}"))
        assert current_db()[snap.COVERAGE_SNAPSHOTS].count_documents({}) == 5

    def test_prune_keeps_the_most_recent(self):
        for i in range(8):
            snap.save(_payload(
                measured_at=datetime.now(timezone.utc) - timedelta(hours=i),
                source_revision=f"rev{i}"))
        snap.prune(keep=3)
        rows = list(current_db()[snap.COVERAGE_SNAPSHOTS].find({}, {"_id": 0}))
        assert len(rows) == 3
        assert {r["source_revision"] for r in rows} == {"rev0", "rev1", "rev2"}


class TestTheFeedReadsTheSnapshot:

    def test_coverage_block_serves_the_measurement_with_its_age(self):
        from services import tech_feed_service as tfs
        snap.save(_payload())
        block = tfs.coverage_block(force=True)
        measured = block["measured"]
        assert measured["available"] is True
        assert measured["covered_technology_employers"] == 875
        assert "age_hours" in measured and "stale" in measured

    def test_an_unmeasured_index_says_so_rather_than_reporting_zero(self):
        from services import tech_feed_service as tfs
        block = tfs.coverage_block(force=True)
        assert block["measured"]["available"] is False
        assert "coverage measurement" in block["measured"]["detail"]

    def test_internal_snapshot_fields_do_not_reach_the_public_block(self):
        from services import tech_feed_service as tfs
        snap.save(_payload(secret_internal_note="do not publish"))
        measured = tfs.coverage_block(force=True)["measured"]
        assert "secret_internal_note" not in measured
        assert "snapshot_version" not in measured
