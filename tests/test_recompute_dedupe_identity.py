"""The identity repair is dry-run by default, fixes the gh_jid collapse, and
rolls back exactly."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _mod():
    spec = importlib.util.spec_from_file_location(
        "recompute_dedupe_identity", ROOT / "scripts" / "recompute_dedupe_identity.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def db(mock_db):
    mock_db.job_postings.delete_many({})
    # Two Stripe jobs stored under the OLD rule: one shared group.
    for n in ("111", "222"):
        mock_db.job_postings.insert_one({
            "status": "active", "source": "career-pages", "source_job_id": f"gh-stripe:{n}",
            "employer_id": "stripe", "ats": "greenhouse", "title_raw": "SWE",
            "canonical_url": f"https://stripe.com/jobs/search?gh_jid={n}",
            "dedupe_group_id": "old-shared", "canonical_url_key":
            "https://stripe.com/jobs/search"})
    yield mock_db
    mock_db.job_postings.delete_many({})


def test_dry_run_writes_nothing_and_reports_the_plan(db):
    out = _mod().run(db)
    assert out["stats"]["changed"] == 2
    assert {d["dedupe_group_id"] for d in db.job_postings.find()} == {"old-shared"}


def test_apply_splits_the_collapsed_group_and_rollback_restores_it(db):
    m = _mod()
    m.run(db, apply=True)
    groups = {d["dedupe_group_id"] for d in db.job_postings.find()}
    assert len(groups) == 2 and "old-shared" not in groups
    assert m.run(db)["stats"].get("changed", 0) == 0          # idempotent
    m.run(db, apply=True, rollback=True)
    docs = list(db.job_postings.find())
    assert {d["dedupe_group_id"] for d in docs} == {"old-shared"}
    assert not any("identity_prev" in d for d in docs)
