"""Coverage snapshot scheduling: lease, keep previous on failure."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from _vg_support import current_db
from services import coverage_snapshot as snap
from services import coverage_snapshot_job as job


@pytest.fixture(autouse=True)
def clean():
    db = current_db()
    db[snap.COVERAGE_SNAPSHOTS].delete_many({})
    db[job.LEASES].delete_many({})
    db[job.RUNS].delete_many({})
    yield


def test_overlapping_runs_are_skipped(monkeypatch):
    db = current_db()
    assert job.acquire_lease(db=db) is True
    assert job.acquire_lease(db=db) is False
    job.release_lease(db=db)
    assert job.acquire_lease(db=db) is True


def test_failed_generation_keeps_previous_snapshot(monkeypatch):
    db = current_db()
    snap.save({
        "technology_employer_denominator": 10,
        "covered_technology_employers": 4,
        "employer_coverage_pct": 0.4,
        "covered_career_sources": 4,
        "ownership_verified_unvalidated_sources": 1,
        "candidate_technology_employers": 6,
        "measured_at": datetime.now(timezone.utc) - timedelta(hours=2),
    }, db=db)

    def boom(*a, **k):
        raise RuntimeError("nope")

    monkeypatch.setattr(job, "acquire_lease", lambda **k: True)
    monkeypatch.setattr(job, "release_lease", lambda **k: None)
    import employer_coverage_report as report
    monkeypatch.setattr(report, "build", boom)
    out = job.run(persist=True, db=db)
    assert out["ok"] is False
    latest = snap.latest(db=db)
    assert latest["covered_technology_employers"] == 4
