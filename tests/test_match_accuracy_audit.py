"""Harness smoke + set-based recall tests (Part E tests 6-7).

Test 6 runs `scripts/match_accuracy_audit` end-to-end against a seeded
mongomock DB with two fake users and asserts: both scorecard files are written,
`silent_drops == 0` on a clean seed, and the harness performs ZERO writes to
`users` / `resume_assets` / `visa_profiles` / `job_postings` (the only new doc
permitted is the ephemeral `tech_feed_searches` session doc the real
`search()` creates).

Test 7 proves paging reaches survivors PAST the ranked window: with
`CANDIDATE_LIMIT=10` and ~30 postings that all survive the full pipeline, the
set-based silent-drop check (`should_be_returnable - returned == ∅`) holds, and
a row `build_query` returns but `hard_filter` legitimately drops (excluded
keyword) is in neither set and is not counted as a silent drop.

Style mirrors `tests/test_tech_feed_search.py` (a `db` fixture clearing the
collections, a wall-tracking NOW, seeding through `inv`).
"""
from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from services import fit_score
from services import job_content_store as store
from services import job_inventory_service as inv
from services import tech_feed_service as tfs

# Load the harness module by path (scripts/ is not a package on PYTHONPATH).
_HARNESS_PATH = Path(__file__).resolve().parent.parent / "scripts" / "match_accuracy_audit.py"
_spec = importlib.util.spec_from_file_location("match_accuracy_audit", _HARNESS_PATH)
audit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(audit)

NOW = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0,
                                         microsecond=0)

COLLECTIONS = ("job_postings", "job_source_state", "job_ingestion_runs",
               "tech_feed_searches", "ingest_runner_heartbeat", "companies",
               "company_ats_endpoints", "users", "resume_assets",
               "visa_profiles", "user_preferences")

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


def seed_posting(*, sjid, seniority=None, title=SWE_TITLE, days=2,
                 company="Acme Inc", tenant="acme", description=SWE_JD,
                 parse=False):
    raw = {
        "source": "career-pages", "source_job_id": sjid,
        "url": f"https://boards.greenhouse.io/{tenant}/jobs/{sjid}",
        "company": company, "title": title, "location": ["Austin, TX, USA"],
        "description": description, "employment_type": "Full time",
        "cost_class": "free", "source_quality": "direct", "industry": "tech",
        "ats": "greenhouse", "tenant": tenant,
        "posted_at": NOW - timedelta(days=days),
    }
    if seniority is not None:
        raw["seniority"] = seniority
    inv.upsert_posting(raw, now=NOW)
    if parse:
        doc = inv.get_posting("career-pages", sjid)
        inv.mark_requirements_parsed(
            "career-pages", sjid, doc["content_hash"],
            fit_score.extract_requirements(description, title), now=NOW)
    return sjid


def seed_user(d, *, email, years=3, level="mid"):
    """A user with an active, parsed resume so get_active_structured is non-None."""
    rid = f"rid-{email}"
    d.users.insert_one({"email": email, "active_resume_id": rid})
    d.resume_assets.insert_one({
        "resume_id": rid,
        "user_email": email,
        "parse_status": "parsed",
        "experience_years": years,
        "parsed_at": NOW,
        "structured": {
            "contact": {"name": "Test User"},
            "skills": ["python", "aws", "postgresql"],
            "experience": [{"title": "Engineer", "years": years}],
        },
    })
    if level:
        d.user_preferences.insert_one({"user_email": email,
                                       "experience_level": level})


def _collection_counts(d):
    return {c: d[c].count_documents({}) for c in COLLECTIONS}


# ---------------------------------------------------------------------------
# Test 6 — harness smoke + ZERO-write assertion
# ---------------------------------------------------------------------------

class TestHarnessSmokeIsReadOnly:
    def test_end_to_end_writes_both_files_zero_drops_zero_writes(self, db, tmp_path, monkeypatch):
        # Two fake users with active parsed resumes.
        seed_user(db, email="alice@example.com", years=2, level="entry")
        seed_user(db, email="bob@example.com", years=6, level="senior")

        # A clean seed: a handful of postings every pipeline stage keeps.
        for i in range(5):
            seed_posting(sjid=f"e{i}", seniority="entry", parse=True)
        for i in range(4):
            seed_posting(sjid=f"m{i}", seniority="mid", parse=True)
        for i in range(3):
            seed_posting(sjid=f"s{i}", seniority="senior", parse=True)

        # Redirect scorecard output into a temp dir (never touch the real one).
        monkeypatch.setattr(audit, "SCORECARD_DIR", tmp_path)

        before = _collection_counts(db)

        payload = audit.run_full(db)
        md_path, json_path = audit.write_scorecards(payload["run_id"], payload)

        after = _collection_counts(db)

        # Both files written.
        assert md_path.exists() and json_path.exists()
        assert md_path.read_text(encoding="utf-8").startswith("# Match-accuracy")

        # Discovery sanity: both users seen, both with a resume.
        agg = payload["aggregate"]
        assert agg["users_total"] == 2
        assert agg["users_with_active_resume"] == 2
        assert agg["runs_audited"] == 4

        # Clean seed ⇒ no silent drops, no window/level mismatch.
        assert agg["silent_drops_total"] == 0
        assert agg["recall_fail"] == 0
        assert agg["max_abs_level_diff"] == 0
        assert agg["max_abs_window_diff"] == 0
        assert agg["acceptance_bar_pass"] is True

        # ZERO writes to every collection EXCEPT the ephemeral session doc.
        for coll, n in after.items():
            if coll == "tech_feed_searches":
                assert n >= before[coll]  # new session docs are expected
                continue
            assert n == before[coll], f"{coll} changed: {before[coll]} -> {n}"

        # The audit obviously ran searches, so session docs were created.
        assert after["tech_feed_searches"] > before["tech_feed_searches"]


# ---------------------------------------------------------------------------
# Test 7 — recall past the ranked window (set-based silent-drop guard)
# ---------------------------------------------------------------------------

class TestRecallPastTheWindow:
    def test_survivors_past_window_are_all_paged_and_legit_drops_are_not_silent(
            self, db, monkeypatch):
        # Ranked window far below the survivor set — survivors must still be
        # reached by paging in index order (the exact silent-drop class the
        # task names).
        monkeypatch.setattr(tfs, "CANDIDATE_LIMIT", 10)

        seed_user(db, email="carol@example.com", years=3, level="mid")

        # 30 postings that survive hard_filter/scope/dedupe for this user.
        # Distinct tenants/companies so dedupe/collapse keep them all.
        for i in range(30):
            seed_posting(sjid=f"keep{i}", seniority="mid",
                         company=f"Company {i}", tenant=f"tenant{i}", parse=True)

        # A row build_query returns but hard_filter legitimately drops: an
        # excluded keyword in the title. It must be in NEITHER set and must NOT
        # count as a silent drop.
        seed_posting(sjid="dropme", seniority="mid", title="Backend Engineer Intern",
                     company="DropCo", tenant="dropco", parse=True)

        now = tfs._now()
        payload = {"user_email": "carol@example.com", "personalize": True,
                   "seniority": ["mid"], "page_size": tfs.MAX_PAGE_SIZE,
                   "exclude_title_terms": ["Intern"]}
        req = tfs.normalize_request(payload)
        true_total = audit._postings().count_documents(tfs.build_query(req, now=now))

        should, bounded, limit = audit.survivor_set(req, now, true_total)
        returned, first, pages = audit.page_to_exhaustion(payload)

        # The excluded-keyword row is in neither set.
        assert "dropme" not in should
        assert "dropme" not in returned

        # Every survivor is reached by paging past the 10-row ranked window.
        assert should - returned == set()
        assert returned - should == set()
        assert len(should) == 30
        assert pages >= 2  # proves we paged past the first (10-row) window

    def test_legit_drop_does_not_inflate_silent_drops_in_full_audit(self, db, monkeypatch):
        monkeypatch.setattr(tfs, "CANDIDATE_LIMIT", 10)
        seed_user(db, email="dave@example.com", years=3, level="mid")
        for i in range(15):
            seed_posting(sjid=f"ok{i}", seniority="mid",
                         company=f"Co {i}", tenant=f"t{i}", parse=True)
        seed_posting(sjid="legit-drop", seniority="mid",
                     title="Backend Engineer Internship",
                     company="DropCo", tenant="dropco", parse=True)

        user = {"email": "dave@example.com", "active_resume_id": "rid-dave@example.com",
                "has_active_resume": True, "structured": {}}
        result = audit.audit_user(user, tfs._now())
        # The all-levels run has no exclusions, so the intern row is a real
        # survivor there and silent_drops is still 0; the point is only that
        # the recall math never false-fails on a legitimate hard_filter drop.
        for key in ("personalized", "all_levels"):
            assert result[key]["recall"]["silent_drops"] == 0
            assert result[key]["recall"]["extra_returned"] == 0
