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

from eval_tech_feed_retrieval import CORPUS, run, run_ordering, seed_corpus  # noqa: E402

from services import employer_display            # noqa: E402
from services import job_content_store as store   # noqa: E402
from services import job_inventory_service as inv  # noqa: E402
from services import tech_feed_service as tfs      # noqa: E402
from services import work_authorization as wa      # noqa: E402

COLLECTIONS = ("job_postings", "tech_feed_searches", "job_source_state",
               "job_ingestion_runs", "ingest_runner_heartbeat", "companies",
               "company_ats_endpoints", "employers", "visa_profiles",
               "user_preferences", "sponsorship_signals")


@pytest.fixture()
def clean_db(monkeypatch):
    monkeypatch.delenv("RESUME_S3_BUCKET", raising=False)
    store.reset_caches()
    # The canonical-name cache is PROCESS-global; a warm entry from another
    # test would pin an answer for a database that looks nothing like this one.
    employer_display.reset_name_cache()
    d = inv._db()
    for c in COLLECTIONS:
        d[c].delete_many({})
    yield d
    for c in COLLECTIONS:
        d[c].delete_many({})
    employer_display.reset_name_cache()


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


@pytest.fixture()
def ordering(clean_db, capsys):
    """The ordering measurements, on a freshly seeded corpus."""
    with capsys.disabled():
        seed_corpus()
        return run_ordering()


class TestWorkAuthorizationOrdering:
    """A job the candidate cannot legally hold must not lead their feed.

    Measured live on 2026-09-15 for one F-1 account: four of the first
    twenty-five cards stated a clearance requirement or "no sponsorship",
    INCLUDING THE FIRST ONE, while `requirements.clearance_required` was true on
    3,004 active core postings and `work_authorization_restricted` on 2,076.
    Both flags were populated at ingest and nothing in retrieval or ranking read
    either of them.
    """

    def test_a_no_sponsorship_posting_sinks_below_every_reachable_one(self, ordering):
        assert ordering["no_sponsor_pos_needs"] is not None
        assert ordering["no_sponsor_pos_needs"] > ordering["last_reachable_pos_needs"], (
            f"a posting that states it does not sponsor sat at "
            f"#{ordering['no_sponsor_pos_needs']} while the last reachable row "
            f"was #{ordering['last_reachable_pos_needs']}")

    def test_a_clearance_posting_sinks_for_a_candidate_who_needs_sponsorship(
            self, ordering):
        assert ordering["clearance_pos_needs"] is not None
        assert ordering["clearance_pos_needs"] > ordering["last_reachable_pos_needs"]

    def test_demotion_never_removes_the_posting(self, ordering):
        """DEMOTE, do not filter. A false-ineligible removes a reachable job,
        which this codebase treats as the worst error available — `fit_score`
        holds its false-INELIGIBLE rate at zero for the same reason."""
        assert ordering["no_sponsor_present_for_needs"] is True
        assert ordering["clearance_present_for_needs"] is True

    def test_the_opt_in_filter_is_the_only_thing_that_removes_them(self, ordering):
        assert ordering["no_sponsor_present_when_hidden"] is False
        assert ordering["clearance_present_when_hidden"] is False

    def test_a_citizen_is_not_demoted_for_either_flag(self, ordering):
        """The demotion is about THIS reader. A US citizen loses nothing for a
        posting that does not sponsor, and a cleared role is reachable."""
        assert ordering["reach_no_sponsor_citizen"] == "likely"
        assert ordering["clearance_pos_citizen"] < ordering["clearance_pos_needs"]

    def test_unknown_is_conservative_but_not_punitive(self, ordering):
        """Never relegated for an answer we never asked for — but the badge is
        still shown, because "no sponsorship" is a fact about the POSTING."""
        assert ordering["no_sponsor_pos_unknown"] < ordering["no_sponsor_pos_needs"]
        assert "no_sponsorship" in ordering["badges_no_sponsor_unknown"]

    def test_the_badge_is_present_for_a_sponsorship_needing_candidate(self, ordering):
        assert "no_sponsorship" in ordering["badges_no_sponsor_needs"]
        assert ordering["reach_no_sponsor_needs"] == "unlikely"

    def test_an_employer_that_states_it_sponsors_is_still_reachable(self, ordering):
        assert ordering["sponsors_pos_needs"] is not None
        assert ordering["sponsors_pos_needs"] <= ordering["last_reachable_pos_needs"]

    def test_the_response_says_which_status_it_ordered_against(self, ordering):
        assert ordering["envelope_declares_status"] == "needs_sponsorship_future"

    def test_an_undeclared_user_is_given_the_question_to_answer(self, ordering):
        """The live account this work started from had no `visa_profiles` row
        at all, because onboarding never asked. A feature that depends on the
        answer has to be able to say the answer is missing."""
        assert ordering["envelope_asks_when_unknown"] is True


class TestAggregatorsDoNotOutrankDirectBoards:
    def test_a_marketplace_repost_ranks_below_a_direct_employer_board(self, ordering):
        """`career_pages_service._rank` has pinned marketplace boards below
        first-party postings "even when match_score is higher" since the legacy
        tab shipped. The tech feed carried only an arithmetic penalty — -0.6 on
        a 0.15-weighted component, 0.09 of `rank_score` — and Jobgether was the
        largest single employer of active core-tech postings (4,157) and led the
        unfiltered feed."""
        assert ordering["marketplace_pos"] is not None, "the repost was DROPPED"
        assert ordering["marketplace_pos"] > ordering["direct_backend_pos"]

    def test_the_repost_is_still_reachable(self, ordering):
        assert ordering["marketplace_pos"] is not None


class TestEmployerDisplayName:
    def test_a_registry_shell_is_not_shown_as_the_employer(self, ordering):
        """"NUTMEG CONTAINER CORPORATION", "CHASE FAMILY LIMITED PARTNERSHIP
        NO. 5" and "M.J. WILLIAMS COMPANY" held 681 active core postings
        between them. They are JPMorgan's and Boeing's shared-tenant slug bug,
        and `employer_registry.IDENTITY_OVERRIDES` has named all three as
        `wrong_names` all along — nothing in the display path read it."""
        assert ordering["shell_display_name"] == "JPMorgan Chase"


class TestDefaultSectorIsSoftware:
    def test_a_default_feed_leads_with_software_family_roles(self, ordering):
        assert ordering["software_families_in_top10"] >= 8, (
            f"only {ordering['software_families_in_top10']}/10 of the first "
            f"page of a DEFAULT feed were software-sector roles")


class TestWorkAuthorizationPolicy:
    """The pure policy, independent of retrieval."""

    def test_unknown_is_the_default_and_costs_nothing_on_a_silent_posting(self):
        v = wa.assess(status=wa.UNKNOWN, clearance_required=None, sponsorship=None)
        assert v["penalty"] == 1.0 and v["tier"] == wa.TIER_OK
        assert v["reachability"] == "likely"

    def test_an_unrecognised_status_is_unknown_never_a_guess(self):
        assert wa.normalize_status("sure why not") == wa.UNKNOWN
        assert wa.normalize_status(None) == wa.UNKNOWN
        assert wa.normalize_status({"$ne": None}) == wa.UNKNOWN

    def test_a_green_card_holder_needs_no_sponsorship_but_is_not_clearance_eligible(self):
        assert wa.needs_sponsorship(wa.US_PERMANENT_RESIDENT) is False
        assert wa.clearance_eligible(wa.US_PERMANENT_RESIDENT) is False
        assert wa.clearance_eligible(wa.US_CITIZEN) is True

    def test_not_knowing_is_a_third_answer(self):
        assert wa.clearance_eligible(wa.UNKNOWN) is None

    def test_silence_on_the_posting_is_never_a_mismatch(self):
        v = wa.assess(status=wa.NEEDS_SPONSORSHIP_NOW,
                      clearance_required=None, sponsorship=None)
        assert v["tier"] == wa.TIER_OK and v["penalty"] == 1.0

    def test_hide_is_opt_in_and_reads_the_posting_not_the_reader(self):
        v = wa.assess(status=wa.UNKNOWN, clearance_required=None,
                      sponsorship="not_offered")
        assert wa.should_hide(assessment=v, hide_sponsorship_restricted=False,
                              hide_clearance_required=False) is False
        assert wa.should_hide(assessment=v, hide_sponsorship_restricted=True,
                              hide_clearance_required=False) is True


#: `sponsorship-ingest-2.0.0` gave `sponsorship_service` row-level provenance
#: (`window_of` / `build_disclaimer`, and a `data_window` on the scored object).
#: Two gates below assert what a PROVENANCED row renders as, and they can only
#: run against a module that has it.
#:
#: The production path needs no such guard and deliberately has none: the feed
#: keys on `signal["data_window"]`, which the older module never sets, so
#: against it every signal is withheld — the conservative answer, and exactly
#: what `test_a_signal_with_no_row_level_provenance_is_withheld` pins.
_HAS_ROW_PROVENANCE = all(
    hasattr(__import__("services.sponsorship_service", fromlist=["x"]), attr)
    for attr in ("window_of", "build_disclaimer"))
_needs_provenance = pytest.mark.skipif(
    not _HAS_ROW_PROVENANCE,
    reason="sponsorship_service predates sponsorship-ingest-2.0.0 row provenance")


class TestSponsorshipIntelDegradesSilently:
    def test_an_empty_signals_collection_yields_no_block_and_no_error(
            self, clean_db):
        seed_corpus()
        got = tfs.search({"tech_scope": ["core"], "page_size": 10,
                          "personalize": False, "posted_within_days": 90})
        assert got["jobs"]
        assert all(j.get("sponsorship_signal") is None for j in got["jobs"])

    @_needs_provenance
    def test_a_stored_signal_reaches_the_card_with_its_disclaimer(self, clean_db):
        from services import sponsorship_service as ss
        seed_corpus()
        clean_db.sponsorship_signals.insert_one({
            "employer_norm": ss.normalize_employer("RTX"),
            "employer_display": "RTX", "lca_filings_5y": 900,
            "approvals_5y": 640, "denials_5y": 40,
            "top_titles": ["Software Engineer"],
            "source_label": "USCIS H-1B Employer Data Hub",
            # A realistic row: provenance is what admits it (see
            # `test_a_signal_with_no_row_level_provenance_is_withheld`).
            "data_window": {"fiscal_years": [2026], "years": 1,
                            "label": "USCIS fiscal year 2026 (single fiscal year)",
                            "dol_lca_included": False, "uscis_included": True},
        })
        got = tfs.search({"tech_scope": ["core"], "page_size": 100,
                          "personalize": False, "posted_within_days": 90,
                          "query": "mission systems"})
        cards = [j for j in got["jobs"] if j.get("sponsorship_signal")]
        assert cards, "the stored signal never reached a card"
        sig = cards[0]["sponsorship_signal"]
        # The framing is `sponsorship_service`'s, verbatim - this module
        # reproduces none of the copy, so there is one place it can be wrong.
        assert sig["disclaimer"] == ss.build_disclaimer(
            sig["data_vintage"], sig["source"])
        assert sig["source"] == "USCIS H-1B Employer Data Hub"
        assert "640" in sig["headline"] or "640" in sig["summary"]

    def test_a_signal_with_no_row_level_provenance_is_withheld(self, clean_db):
        """`sponsorship-ingest-2.0.0` made the service read each row's own
        `data_window` / `source_label`, so a FY2026 row correctly renders as
        "in FY2026" from "USCIS H-1B Employer Data Hub". A row written BEFORE
        provenance existed falls back to `DATA_WINDOW_YEARS = 5` and a label
        naming DOL — five years of two datasets we do not have — and this feed
        will not put that in front of a candidate."""
        from services import sponsorship_service as ss
        seed_corpus()
        clean_db.sponsorship_signals.insert_one({
            "employer_norm": ss.normalize_employer("RTX"),
            "employer_display": "RTX", "approvals_5y": 640, "denials_5y": 40,
        })
        got = tfs.search({"tech_scope": ["core"], "page_size": 100,
                          "personalize": False, "posted_within_days": 90,
                          "query": "mission systems"})
        assert all(j.get("sponsorship_signal") is None for j in got["jobs"])
        assert got["diagnostics"].get("sponsorship_suppressed_window", 0) >= 1, (
            "the refusal must be REPORTED, not silent")

    @_needs_provenance
    def test_a_row_carrying_its_own_window_is_served_with_that_window(
            self, clean_db):
        """Self-healing in both directions: nothing to flip, the row's own
        provenance is what admits it."""
        from services import sponsorship_service as ss
        seed_corpus()
        clean_db.sponsorship_signals.insert_one({
            "employer_norm": ss.normalize_employer("RTX"),
            "employer_display": "RTX", "approvals_5y": 640, "denials_5y": 40,
            "source_label": "USCIS H-1B Employer Data Hub",
            "data_window": {"fiscal_years": [2026], "years": 1,
                            "label": "USCIS fiscal year 2026 (single fiscal year)",
                            "dol_lca_included": False, "uscis_included": True},
        })
        got = tfs.search({"tech_scope": ["core"], "page_size": 100,
                          "personalize": False, "posted_within_days": 90,
                          "query": "mission systems"})
        served = [j for j in got["jobs"] if j.get("sponsorship_signal")]
        assert served, "a row with its own window must be served"
        sig = served[0]["sponsorship_signal"]
        assert sig["data_window_years"] == 1
        # The copy must not claim five years of two datasets from one year of one.
        for field in ("headline", "summary", "disclaimer"):
            assert "5 years" not in sig[field] and "5 fiscal years" not in sig[field]
            assert "DOL" not in sig[field]
        assert "FY2026" in sig["summary"] and "FY2026" in sig["disclaimer"]


class TestMatchScoreIsUntouched:
    def test_the_demotion_lands_in_rank_score_and_never_in_match_score(
            self, clean_db):
        """ADR-002: `fit_score` is the ONE candidate-to-job score. Preference
        signals live in `rank_score`. A demotion that moved `match_score` would
        be a second match formula wearing the first one's name."""
        seed_corpus()
        body = {"tech_scope": ["core"], "page_size": 100, "personalize": False,
                "posted_within_days": 90}
        citizen = tfs.search({**body, "work_authorization_status": "us_citizen"})
        needs = tfs.search({**body,
                            "work_authorization_status": "needs_sponsorship_now"})
        by_id = {j["job_id"]: j for j in citizen["jobs"]}
        moved = 0
        for job in needs["jobs"]:
            other = by_id.get(job["job_id"])
            if not other:
                continue
            assert job["match_score"] == other["match_score"], job["title_raw"]
            if job["rank_score"] != other["rank_score"]:
                moved += 1
        assert moved, "no row's rank_score responded to the declared status"


class TestWorkLocationDimension:
    """Foreign work locations demote in `rank_score`, never in `match_score`.

    Distinct from sponsorship: "will not sponsor" and "job is in Bangkok" are
    different facts, different badges, different copy. Unknown country is
    kept — hard-filtering the 71% unreadable corpus would empty the feed.
    """

    def _seed_geo(self, clean_db):
        from datetime import timedelta
        from eval_tech_feed_retrieval import NOW
        from services import fit_score
        seed_corpus()
        rows = [
            ("geo-cdmx", "GERENCIA JR. DATA ANALYST ALM", "CDMX",
             "Data Analyst. Requirements: SQL, Python, Tableau, Power BI. "
             "Build dashboards and data pipelines."),
            ("geo-bkk", "Data Analyst Intern",
             "Bangkok MSG Headquarters",
             "Data Analyst Intern. Requirements: Python, SQL, pandas, "
             "Tableau, statistics. Build dashboards."),
            ("geo-us", "Backend Engineer", "Austin, TX, USA",
             "Backend Engineer. Requirements: Python, SQL, AWS, Docker."),
        ]
        for key, title, loc, desc in rows:
            inv.upsert_posting({
                "source": "career-pages", "source_job_id": key,
                "url": f"https://example.test/geo/{key}",
                "company": f"Geo Co {key}", "title": title,
                "location": [loc], "description": desc,
                "employment_type": "Full time", "cost_class": "free",
                "source_quality": "direct", "industry": "tech",
                "ats": "greenhouse", "tenant": f"geo-{key}",
                "posted_at": NOW - timedelta(days=2),
            }, now=NOW)
            doc = inv.get_posting("career-pages", key)
            inv.mark_requirements_parsed(
                "career-pages", key, doc["content_hash"],
                fit_score.extract_requirements(desc, title), now=NOW)

    def test_foreign_rows_rank_below_home_and_stay_reachable(self, clean_db):
        self._seed_geo(clean_db)
        got = tfs.search({
            "tech_scope": ["core"], "page_size": 100, "personalize": False,
            "posted_within_days": 90,
            "work_authorization_status": "needs_sponsorship_now",
            "home_country": "US",
        })
        by_url = {j.get("canonical_url"): j for j in got["jobs"]}
        us = by_url.get("https://example.test/geo/geo-us")
        mx = by_url.get("https://example.test/geo/geo-cdmx")
        th = by_url.get("https://example.test/geo/geo-bkk")
        assert us and mx and th, "demotion must not DROP foreign rows"
        assert us["rank_score"] > mx["rank_score"]
        assert us["rank_score"] > th["rank_score"]
        assert mx["match_score"] is not None and th["match_score"] is not None
        assert mx["work_location"]["verdict"] == "foreign"
        assert "foreign_location" in (mx["work_location"].get("badges") or [])
        assert th["work_location"]["primary_country"] == "TH"

    def test_opt_in_filter_hides_only_resolved_foreign(self, clean_db):
        self._seed_geo(clean_db)
        got = tfs.search({
            "tech_scope": ["core"], "page_size": 100, "personalize": False,
            "posted_within_days": 90,
            "work_authorization_status": "needs_sponsorship_now",
            "home_country": "US",
            "hide_foreign_locations": True,
        })
        urls = {j.get("canonical_url") for j in got["jobs"]}
        assert "https://example.test/geo/geo-us" in urls
        assert "https://example.test/geo/geo-cdmx" not in urls
        assert "https://example.test/geo/geo-bkk" not in urls

    def test_location_demotion_never_moves_match_score(self, clean_db):
        self._seed_geo(clean_db)
        body = {"tech_scope": ["core"], "page_size": 100, "personalize": False,
                "posted_within_days": 90}
        plain = tfs.search({**body, "work_authorization_status": "unknown"})
        us_home = tfs.search({
            **body, "work_authorization_status": "needs_sponsorship_now",
            "home_country": "US"})
        by_id = {j["job_id"]: j for j in plain["jobs"]}
        for job in us_home["jobs"]:
            other = by_id.get(job["job_id"])
            if other:
                assert job["match_score"] == other["match_score"], job["title_raw"]


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
