"""Company autocomplete: names in, canonical ids out — and nothing operational.

The security claim is as load-bearing as the product one. `career_sources`
holds the endpoints we fetch, the redirect chains we followed and the domains
we probed. None of it describes the employer, all of it describes our crawl,
and a public autocomplete is exactly the endpoint through which it would leak
one company at a time.
"""
from __future__ import annotations

import pytest

from services import career_source_service as css
from services import employer_directory as ed
from services import employer_registry as er
from services.career_source_service import evidence


def _emp(db, employer_id, name, **extra):
    doc = {
        "employer_id": employer_id, "display_name": name,
        "name_key": er.name_key(name), "aliases": [name],
        "operating_status": er.STATUS_ACTIVE, "identity_confidence": 0.95,
        "id_basis": "domain", "employer_categories": ["software_saas"],
        "cohorts": ["public_tech"], "canonical_domain": None,
        "evidence": [{"kind": "curated_seed", "source": "tech_companies.py"}],
    }
    doc.update(extra)
    db["employers"].insert_one(doc)
    return doc


def _posting(db, employer_id, *, scope="core", n=1):
    for i in range(n):
        db["job_postings"].insert_one({
            "job_id": f"{employer_id}-{scope}-{i}", "status": "active",
            "canonical_employer_id": employer_id,
            "tech": {"tech_scope": scope}})


@pytest.fixture()
def registry(mock_db):
    mock_db["employers"].delete_many({})
    mock_db["job_postings"].delete_many({})
    mock_db[css.CAREER_SOURCES].delete_many({})
    _emp(mock_db, "emp_000000000001", "Microsoft", canonical_domain="microsoft.com")
    _emp(mock_db, "emp_000000000002", "Micro Bubble Inc",
         identity_confidence=0.45, id_basis="name", canonical_domain=None,
         employer_categories=["other"], evidence=[])
    _emp(mock_db, "emp_000000000003", "Micron Technology",
         canonical_domain="micron.com", employer_categories=["semiconductor"])
    _posting(mock_db, "emp_000000000001", n=40)
    _posting(mock_db, "emp_000000000001", scope="non_tech", n=10)
    _posting(mock_db, "emp_000000000003", n=5)
    return mock_db


class TestRanking:

    def test_an_exact_name_wins_over_a_bigger_employer(self, registry):
        out = ed.search("Micron Technology", db=registry)
        assert out["companies"][0]["display_name"] == "Micron Technology"

    def test_a_prefix_ranks_the_hiring_employer_first(self, registry):
        """"Micro" must not answer with MICRO BUBBLE INC."""
        names = [c["display_name"] for c in ed.search("Micro", db=registry)["companies"]]
        assert names[0] == "Microsoft"
        assert "Micro Bubble Inc" in names

    def test_posting_counts_are_split_by_scope(self, registry):
        hit = ed.search("Microsoft", db=registry)["companies"][0]
        assert hit["active_postings"] == 50
        assert hit["core_tech_postings"] == 40

    def test_a_one_character_query_answers_nothing(self, registry):
        out = ed.search("M", db=registry)
        assert out["companies"] == []
        assert "2 characters" in out["detail"]

    def test_a_regex_metacharacter_is_escaped_not_executed(self, registry):
        """User text reaches a Mongo `$regex`. It must be inert.

        `.*` is the test that matters: executed it matches every employer in
        the registry, escaped it matches none. `Micro(` still finds Microsoft
        because `name_key` strips the punctuation BEFORE the escape — that is
        normalisation doing its job, not the metacharacter doing its.
        """
        assert ed.search(".*", db=registry)["companies"] == []
        assert ed.search("^.", db=registry)["companies"] == []
        names = [c["display_name"]
                 for c in ed.search("Micro(", db=registry)["companies"]]
        assert names and "Microsoft" in names


class TestPayloadSafety:

    def test_no_career_source_internals_are_returned(self, registry):
        css_row = css.build_source(
            employer_id="emp_000000000001", ats_vendor="workday",
            tenant="microsoft", job_count_last=50,
            ownership_evidence=[evidence(css.SIGNAL_DOMAIN_LINK,
                                         source="https://microsoft.com/careers",
                                         value="secret-internal-endpoint",
                                         confirmed=True)],
            content_validation_evidence=[
                evidence("content_fetch", source="workday", value="50 postings",
                         confirmed=True)])
        assert css_row["state"] == css.STATE_VALIDATED
        registry[css.CAREER_SOURCES].insert_one(dict(css_row))
        hit = ed.search("Microsoft", db=registry)["companies"][0]
        blob = repr(hit)
        for leak in ("ownership_evidence", "canonical_endpoint", "redirect_chain",
                     "secret-internal-endpoint", "member_key", "verified_domain",
                     "failure_detail", "ats_vendor"):
            assert leak not in blob, f"{leak!r} leaked into the public payload"
        assert hit["career_source_verified"] is True

    def test_an_employer_with_no_verified_source_says_so_without_detail(self, registry):
        hit = ed.search("Micron", db=registry)["companies"][0]
        assert hit["career_source_verified"] is False

    def test_provenance_is_about_the_employer_not_about_the_crawl(self, registry):
        hit = ed.search("Microsoft", db=registry)["companies"][0]
        assert set(hit["identity"]) == {"basis", "confidence",
                                        "operating_status", "redirects_to"}
        assert hit["identity"]["basis"] == "domain"


class TestTechnologyOnly:

    def test_technology_only_uses_the_coverage_denominator_rule(self, registry):
        """The filter and the report must admit the same employers."""
        from services import employer_tech_classifier as etc
        out = ed.search("Micro", db=registry, technology_only=True)
        names = [c["display_name"] for c in out["companies"]]
        assert "Microsoft" in names
        assert "Micro Bubble Inc" not in names
        emp = registry["employers"].find_one({"display_name": "Micro Bubble Inc"})
        assert etc.is_evidence_backed_tech(emp, {}, None)[0] is False

    def test_a_filed_SIC_CODE_admits_an_employer_with_no_curated_evidence(self, registry):
        """The strongest technology signal lives on the `companies` row and
        nowhere else. Judging the autocomplete without it would put an
        employer in the technology-only FEED and leave them out of the
        technology-only LIST — the exact inconsistency the shared rule exists
        to prevent."""
        registry["employers"].insert_one({
            "employer_id": "emp_00000000000c", "display_name": "Microchip Filer",
            "name_key": "microchip filer", "aliases": ["Microchip Filer"],
            "operating_status": er.STATUS_ACTIVE, "identity_confidence": 0.95,
            "id_basis": "domain", "canonical_domain": "microchipfiler.example",
            "employer_categories": ["other"], "cohorts": ["public_tech"],
            # No curated seed, no override: the ONLY evidence is the filing.
            "evidence": [],
            "registry_refs": {"company_ids": ["c-filer"]},
        })
        registry["companies"].insert_one({
            "company_id": "c-filer", "name": "MICROCHIP FILER INC",
            "cik": "0001234567", "ticker": "MCF", "sic": "3674",
            "sic_description": "Semiconductors & Related Devices",
        })
        names = [c["display_name"] for c in
                 ed.search("Microchip", db=registry, technology_only=True)["companies"]]
        assert "Microchip Filer" in names


class TestLookup:

    def test_ids_resolve_back_to_names(self, registry):
        out = ed.lookup(["emp_000000000001"], db=registry)
        assert out["companies"][0]["display_name"] == "Microsoft"
        assert out["unresolved"] == []

    def test_an_unknown_id_is_reported_not_invented(self, registry):
        out = ed.lookup(["emp_deadbeefdead"], db=registry)
        assert out["companies"] == []
        assert out["unresolved"] == ["emp_deadbeefdead"]

    def test_a_saved_filter_follows_an_acquisition(self, registry):
        _emp(registry, "emp_00000000000a", "Acquired Co",
             operating_status=er.STATUS_ACQUIRED,
             parent_employer_id="emp_000000000001")
        out = ed.lookup(["emp_00000000000a"], db=registry)
        assert out["companies"][0]["display_name"] == "Microsoft"
        assert out["companies"][0]["requested_employer_id"] == "emp_00000000000a"

    def test_a_redirecting_row_is_not_offered_as_a_pickable_company(self, registry):
        _emp(registry, "emp_00000000000b", "Micro Legacy",
             operating_status=er.STATUS_MERGED,
             parent_employer_id="emp_000000000001")
        names = [c["display_name"] for c in ed.search("Micro", db=registry)["companies"]]
        assert "Micro Legacy" not in names


class TestCategories:

    def test_other_is_reported_last_and_labelled(self, registry):
        out = ed.categories(db=registry)
        values = [c["value"] for c in out["categories"]]
        assert values[-1] == "other"
        assert out["categories"][-1]["label"] == "Not yet categorised"
        assert set(values) >= {"software_saas", "semiconductor", "other"}


class TestTheAutocompleteAndTheFilterAgree:
    """The technology-only LIST and the technology-only FEED admit the same
    employers.

    This is the whole reason `is_evidence_backed_tech` was moved out of
    `scripts/employer_coverage_report.py` and into
    `employer_tech_classifier`. Three surfaces ask "is this a technology
    employer" — the coverage report's denominator, the feed's
    `employer_scope=tech_only`, and this autocomplete — and before the move
    they had two implementations between them. A company you can pick from
    the technology-only list and then cannot find in the technology-only feed
    is the failure that guarantees.
    """

    def test_a_company_offered_by_the_autocomplete_survives_the_feed_filter(
            self, registry):
        from services import employer_tech_classifier as etc
        offered = ed.search("Micro", db=registry, technology_only=True)["companies"]
        assert offered, "fixture must offer at least one technology employer"
        for company in offered:
            emp = registry["employers"].find_one(
                {"employer_id": company["employer_id"]})
            # The same call `_apply_employer_scope` makes for a canonical row.
            ok, why = etc.is_evidence_backed_tech(emp, {}, None)
            assert ok or company["core_tech_postings"] > 0, (
                company["display_name"], why)

    def test_one_function_answers_the_question_for_every_surface(self):
        """Not two implementations that happen to agree today."""
        import importlib
        import inspect
        import sys
        from pathlib import Path

        from services import employer_tech_classifier as etc

        # `scripts/` is not on the suite's path (the suite runs with
        # PYTHONPATH=shared/python), so the report module is loaded here
        # rather than imported at the top of the file.
        scripts_dir = str(Path(__file__).resolve().parent.parent / "scripts")
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        report = importlib.import_module("employer_coverage_report")
        assert report.is_evidence_backed_tech is etc.is_evidence_backed_tech
        assert report.TECH_EVIDENCE_KINDS is etc.TECH_EVIDENCE_KINDS
        # And the feed calls the same one.
        from services import tech_feed_service as tfs
        source = inspect.getsource(tfs._apply_employer_scope)
        assert "etc.is_evidence_backed_tech" in source


class TestPrefixFirstIsAnOptimisationNotABehaviourChange:
    """The indexed prefix pass must not change WHICH companies come back.

    Measured on the live registry, the contains query examines all 23,782
    employers on every keystroke; the anchored prefix examines 22 keys. The
    fast path is only legitimate if the slow one cannot outrank it, which is
    why `_rank_key` reads a prefix on aliases as well as on `name_key`.
    """

    def test_a_match_only_reachable_by_CONTAINS_is_still_found(self, mock_db):
        mock_db["employers"].delete_many({})
        _emp(mock_db, "emp_0000000000c1", "Advanced Micro Devices",
             canonical_domain="amd.com")
        out = ed.search("micro", limit=5, db=mock_db)
        assert [c["display_name"] for c in out["companies"]] \
            == ["Advanced Micro Devices"]

    def test_a_full_page_of_prefix_hits_does_not_run_the_scan(self, mock_db,
                                                              monkeypatch):
        mock_db["employers"].delete_many({})
        for i in range(4):
            _emp(mock_db, f"emp_0000000000d{i}", f"Microthing {i}")
        _emp(mock_db, "emp_0000000000e0", "Advanced Micro Devices")
        seen = []
        real = ed._match_regex
        monkeypatch.setattr(ed, "_match_regex",
                            lambda q: (seen.append(q), real(q))[1])
        out = ed.search("micro", limit=3, db=mock_db)
        assert len(out["companies"]) == 3
        assert seen == [], "the prefix pass already filled the page"

    def test_a_SHORT_page_falls_back_and_merges_without_duplicating(self, mock_db):
        mock_db["employers"].delete_many({})
        _emp(mock_db, "emp_0000000000f1", "Microsoft")          # prefix
        _emp(mock_db, "emp_0000000000f2", "Advanced Micro Devices")  # contains
        out = ed.search("micro", limit=10, db=mock_db)
        ids = [c["employer_id"] for c in out["companies"]]
        assert ids == ["emp_0000000000f1", "emp_0000000000f2"]
        assert len(ids) == len(set(ids))

    def test_an_alias_only_match_is_FOUND_and_ranked_below_a_name_prefix(self, mock_db):
        """The fast query cannot see an alias prefix — `aliases` keep their
        original capitalisation and the anchored regex has to stay
        case-sensitive to use an index — so the RANKING must not claim to.
        `_rank_key`'s prefix rung reads `name_key` and nothing else, which is
        exactly the field the fast query matches; the two agreeing is what
        makes skipping the scan exact instead of merely fast."""
        mock_db["employers"].delete_many({})
        mock_db["job_postings"].delete_many({})
        _emp(mock_db, "emp_00000000a001", "Alphabet Holdings",
             aliases=["Alphabet Holdings", "Googol Labs"])
        _emp(mock_db, "emp_00000000a002", "Googol Systems")
        _posting(mock_db, "emp_00000000a002", n=50)
        out = ed.search("googol", limit=5, db=mock_db)
        ids = [c["employer_id"] for c in out["companies"]]
        assert ids == ["emp_00000000a002", "emp_00000000a001"]

    def test_a_page_FILLED_by_name_key_prefixes_can_skip_the_scan_safely(self, mock_db):
        """Every row the fast query returns scores `prefix = 0`; every row the
        scan could add scores 1. A full page of them is already the answer."""
        mock_db["employers"].delete_many({})
        mock_db["job_postings"].delete_many({})
        for i in range(3):
            _emp(mock_db, f"emp_00000000b00{i}", f"Googol {i}")
        _emp(mock_db, "emp_00000000b009", "Barre Googol Systems")
        _posting(mock_db, "emp_00000000b009", n=500)
        out = ed.search("googol", limit=3, db=mock_db)
        ids = [c["employer_id"] for c in out["companies"]]
        assert "emp_00000000b009" not in ids, (
            "500 postings must not lift a CONTAINS match over a prefix one")

    def test_technology_only_still_reaches_a_contains_match(self, mock_db):
        """The prefix pass can fill the page and the technology rule can then
        empty it. A page short for THAT reason has nothing to do with the
        query, so the contains pass still has to run."""
        mock_db["employers"].delete_many({})
        mock_db["job_postings"].delete_many({})
        _emp(mock_db, "emp_00000000b001", "Micro Bubble Inc",
             identity_confidence=0.45, id_basis="name", canonical_domain=None,
             employer_categories=["other"], cohorts=[], evidence=[])
        _emp(mock_db, "emp_00000000b002", "Advanced Micro Devices",
             canonical_domain="amd.com", employer_categories=["semiconductor"])
        out = ed.search("micro", limit=5, db=mock_db, technology_only=True)
        assert [c["display_name"] for c in out["companies"]] \
            == ["Advanced Micro Devices"]


class TestAQueryThatNormalisesToNothing:
    """`name_key` strips punctuation, so a query of pure punctuation
    normalises to the empty string — and `^` matches every row on earth. The
    fallback to the raw lowercase query is what keeps `_prefix_regex` from
    turning "&&" into "return the registry"."""

    @pytest.mark.parametrize("query", ["&&", "..", "--", "??", "()"])
    def test_it_matches_nothing_rather_than_everything(self, mock_db, query):
        mock_db["employers"].delete_many({})
        for i in range(5):
            _emp(mock_db, f"emp_00000000c00{i}", f"Company {i}")
        assert ed.search(query, limit=10, db=mock_db)["companies"] == []

    def test_a_query_below_the_minimum_never_reaches_the_database(self, mock_db):
        out = ed.search("a", limit=10, db=mock_db)
        assert out["companies"] == []
        assert "type at least" in out["detail"]
