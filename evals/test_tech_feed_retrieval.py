"""Retrieval eval gates, plus the traversal-ceiling regression.

`eval_tech_feed_retrieval.run()` is the harness; this module runs it as a TEST
so a filter that stops matching a whole specialty fails CI instead of being
noticed by a user.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "evals"))

from eval_tech_feed_retrieval import CORPUS, run, seed_corpus  # noqa: E402

from services import job_content_store as store   # noqa: E402
from services import job_inventory_service as inv  # noqa: E402
from services import tech_feed_service as tfs      # noqa: E402

COLLECTIONS = ("job_postings", "tech_feed_searches", "job_source_state",
               "job_ingestion_runs", "ingest_runner_heartbeat", "companies",
               "company_ats_endpoints")


@pytest.fixture()
def clean_db(monkeypatch):
    monkeypatch.delenv("RESUME_S3_BUCKET", raising=False)
    store.reset_caches()
    d = inv._db()
    for c in COLLECTIONS:
        d[c].delete_many({})
    yield d
    for c in COLLECTIONS:
        d[c].delete_many({})


@pytest.fixture()
def results(clean_db, capsys):
    with capsys.disabled():
        return run(verbose=True)


class TestRetrievalQuality:
    def test_no_dimension_silently_returns_nothing(self, results):
        """The failure this eval exists for. An aggregate of 0.95 across seven
        dimensions can hide one that returns zero rows, and a discipline that
        cannot search is not a degraded experience - it is no product."""
        empty = [d for d, m in results["by_dimension"].items() if m["recall"] == 0.0]
        assert not empty, empty

    @pytest.mark.parametrize("dimension", [
        "source", "family", "specialty", "cohort", "ambiguous", "geography",
        "seniority",
    ])
    def test_every_named_dimension_is_measured(self, results, dimension):
        assert dimension in results["by_dimension"], dimension
        assert results["by_dimension"][dimension]["cases"] >= 1

    def test_recall_floor(self, results):
        assert results["overall"]["recall"] >= 0.90, results["overall"]

    def test_no_negative_row_leaks_into_a_technical_result(self, results):
        """A nurse, a store associate, an AI-training gig and a plant process
        engineer are in the corpus precisely so a core-scope search can be
        caught returning them."""
        leaked = [(r["dimension"], r["label"], r["leaked"])
                  for r in results["rows"] if r["leaked"]]
        assert not leaked, leaked

    def test_per_stratum_recall_is_reported_not_only_the_aggregate(self, results):
        for row in results["rows"]:
            assert "recall" in row and "precision" in row
            assert row["dimension"] and row["label"]


class TestUnknownIsNeverExcluded:
    def test_an_ambiguous_title_is_retrievable(self, results):
        amb = [r for r in results["rows"] if r["dimension"] == "ambiguous"][0]
        assert amb["recall"] == 1.0, amb["missing"]

    def test_unknown_seniority_is_included_when_asked_for(self, results):
        row = [r for r in results["rows"]
               if r["label"] == "unknown_is_included"][0]
        assert row["recall"] == 1.0, row["missing"]


class TestTraversalCeiling:
    """The 2,000-result ceiling, gone.

    `CANDIDATE_LIMIT` bounds how many postings one search SCORES. Until
    fit-v2.1's paging work it also bounded how many could be REACHED: the
    session stored `order[:MAX_ORDERED]`, `decode_cursor` refused any offset
    past it, and the response told the user to "narrow the filters to reach
    the rest" — of results that no filter could reach.
    """

    def _seed_many(self, n: int) -> None:
        from datetime import timedelta

        from eval_tech_feed_retrieval import NOW
        from services import fit_score
        for i in range(n):
            sjid = f"bulk-{i:05d}"
            desc = ("Backend Engineer. Requirements: Python, SQL, AWS, Docker "
                    "and Kubernetes. 3+ years of experience.")
            inv.upsert_posting({
                "source": "career-pages", "source_job_id": sjid,
                "url": f"https://example.test/bulk/{sjid}",
                "company": f"Bulk Co {i % 40}", "title": "Backend Engineer",
                "location": ["Austin, TX, USA"], "description": desc,
                "employment_type": "Full time", "cost_class": "free",
                "source_quality": "direct", "industry": "tech",
                "ats": "greenhouse", "tenant": f"bulk{i % 40}",
                # Distinct timestamps so the keyset order is TOTAL and the
                # traversal is deterministic.
                "posted_at": NOW - timedelta(minutes=i + 1),
            }, now=NOW)
            doc = inv.get_posting("career-pages", sjid)
            inv.mark_requirements_parsed(
                "career-pages", sjid, doc["content_hash"],
                fit_score.extract_requirements(desc, "Backend Engineer"), now=NOW)

    def test_paging_reaches_past_the_ranked_window(self, clean_db, monkeypatch):
        # A small window, so the test exercises the SAME handoff the 2,000-row
        # one does without seeding 2,000 postings.
        monkeypatch.setattr(tfs, "CANDIDATE_LIMIT", 20)
        monkeypatch.setattr(tfs, "MAX_ORDERED", 20)
        self._seed_many(60)

        seen: list = []
        body = {"tech_scope": ["core"], "page_size": 10, "personalize": False,
                "posted_within_days": 90}
        page = tfs.search(body)
        pages = 0
        while True:
            ids = [j["job_id"] for j in page["jobs"]]
            seen.extend(ids)
            pages += 1
            if not page.get("next_cursor") or pages > 20:
                break
            page = tfs.search({**body, "cursor": page["next_cursor"]})

        assert len(seen) > 20, (
            f"traversal stopped at {len(seen)}; the ranked window is 20 and "
            f"60 postings match")
        assert len(seen) == len(set(seen)), "a posting was served twice"
        assert len(seen) >= 55, f"only reached {len(seen)} of 60"

    def test_the_reported_total_is_the_match_count_not_the_window(
            self, clean_db, monkeypatch):
        monkeypatch.setattr(tfs, "CANDIDATE_LIMIT", 20)
        monkeypatch.setattr(tfs, "MAX_ORDERED", 20)
        self._seed_many(60)
        got = tfs.search({"tech_scope": ["core"], "page_size": 10,
                          "personalize": False, "posted_within_days": 90})
        assert got["total"] >= 55, got["total"]

    def test_the_warning_no_longer_claims_the_rest_is_unreachable(
            self, clean_db, monkeypatch):
        monkeypatch.setattr(tfs, "CANDIDATE_LIMIT", 20)
        monkeypatch.setattr(tfs, "MAX_ORDERED", 20)
        self._seed_many(60)
        got = tfs.search({"tech_scope": ["core"], "page_size": 10,
                          "personalize": False, "posted_within_days": 90})
        joined = " ".join(got.get("warnings") or [])
        assert "Narrow the filters to reach the rest" not in joined
        if joined:
            assert "date order" in joined or "ranked" in joined

    def test_a_keyset_page_declares_that_it_is_no_longer_relevance_ranked(
            self, clean_db, monkeypatch):
        """Honesty about the ordering. A page served in index order must not
        present itself as a ranked one."""
        monkeypatch.setattr(tfs, "CANDIDATE_LIMIT", 20)
        monkeypatch.setattr(tfs, "MAX_ORDERED", 20)
        self._seed_many(60)
        body = {"tech_scope": ["core"], "page_size": 10, "personalize": False,
                "posted_within_days": 90}
        page = tfs.search(body)
        for _ in range(6):
            if not page.get("next_cursor"):
                break
            page = tfs.search({**body, "cursor": page["next_cursor"]})
            if (page.get("diagnostics") or {}).get("served_from") == "keyset":
                assert page["diagnostics"]["sort"] == "index_order"
                return
        pytest.fail("never reached the keyset phase")

    def test_dedupe_still_runs_on_a_keyset_page(self, clean_db, monkeypatch):
        monkeypatch.setattr(tfs, "CANDIDATE_LIMIT", 20)
        monkeypatch.setattr(tfs, "MAX_ORDERED", 20)
        self._seed_many(60)
        body = {"tech_scope": ["core"], "page_size": 10, "personalize": False,
                "posted_within_days": 90}
        page = tfs.search(body)
        all_ids = []
        for _ in range(12):
            all_ids.extend(j["job_id"] for j in page["jobs"])
            if not page.get("next_cursor"):
                break
            page = tfs.search({**body, "cursor": page["next_cursor"]})
        assert len(all_ids) == len(set(all_ids))

    def test_an_expired_keyset_cursor_still_returns_cursor_expired(
            self, clean_db, monkeypatch):
        from datetime import datetime, timezone
        raw = tfs.encode_cursor(
            "no-such-session",
            keyset={"p": datetime(2026, 8, 1, tzinfo=timezone.utc), "j": "x"})
        got = tfs.search({"cursor": raw, "personalize": False})
        assert got["error"] == "cursor_expired"
        assert got["jobs"] == []
