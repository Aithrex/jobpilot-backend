"""Categories, provenance and geography — the rules `repair_employer_metadata`
applies, tested where they live rather than through the script.

Each one is a defect measured on the live registry:

  * `other` beside a real category, on 1,177 employers, because
    `employer_categories` was merged with `$addToSet` and `other` is what an
    UNMAPPED vocabulary term becomes;
  * categories with no recorded origin, which cannot be argued with;
  * one `countries` field answering four different questions.
"""
from __future__ import annotations

import pytest

from services import employer_registry as er


class TestCategoryResolution:

    def test_other_is_dropped_when_anything_specific_survives(self):
        assert er.resolve_categories(["fintech", "other"]) == ["fintech"]
        assert er.resolve_categories(["other", "semiconductor", "ai_ml"]) \
            == ["semiconductor", "ai_ml"]

    def test_other_ALONE_is_kept(self):
        """An employer we cannot place is a real and countable state. Hiding
        it makes the facet counts stop adding up."""
        assert er.resolve_categories(["other"]) == ["other"]
        assert er.resolve_categories(["a-vocabulary-nobody-has"]) == ["other"]

    def test_nothing_in_yields_nothing_out(self):
        assert er.resolve_categories([]) == []

    def test_an_unmapped_term_beside_a_mapped_one_does_not_add_other(self):
        assert er.resolve_categories(["payments", "wibble"]) == ["fintech"]

    def test_the_order_of_the_specific_categories_is_preserved(self):
        assert er.resolve_categories(["semiconductor", "other", "ai_ml"]) \
            == ["semiconductor", "ai_ml"]


class TestCategoryProvenance:

    def test_every_declared_term_is_recorded_including_the_ones_that_became_other(self):
        emp = er.build_employer(
            display_name="Acme", canonical_domain="acme.example",
            employer_categories=["fintech", "wibble"],
            category_source="tech_companies.py")
        declared = {(e["declared_as"], e["category"]) for e in emp["category_evidence"]}
        assert ("fintech", "fintech") in declared
        # The term that resolved to `other` is RECORDED even though it was
        # dropped from the resolved list — that is how the next vocabulary gap
        # becomes visible instead of silently widening the `other` bucket.
        assert ("wibble", "other") in declared
        assert emp["employer_categories"] == ["fintech"]

    def test_the_source_travels_with_the_category(self):
        emp = er.build_employer(
            display_name="Acme", canonical_domain="acme.example",
            employer_categories=["fintech"], category_source="sic:6199")
        assert emp["category_evidence"][0]["source"] == "sic:6199"

    def test_an_unattributed_category_says_so_rather_than_claiming_a_source(self):
        emp = er.build_employer(display_name="Acme",
                                canonical_domain="acme.example",
                                employer_categories=["fintech"])
        assert emp["category_evidence"][0]["source"] == "unattributed"

    def test_categories_are_recomputed_from_evidence_not_accumulated(self):
        row = {"employer_categories": ["other"],
               "category_evidence": [
                   {"category": "other", "declared_as": "wibble", "source": "a"},
                   {"category": "fintech", "declared_as": "payments", "source": "b"}]}
        assert er.categories_from_evidence(row) == ["fintech"]


class TestGeographyIsFourQuestions:

    def test_headquarters_hiring_and_remote_are_separate_fields(self):
        emp = er.build_employer(
            display_name="Acme", canonical_domain="acme.example",
            headquarters_country="US", headquarters_evidence="sec_registrant",
            hiring_countries=["United States", "ie", "Remote"],
            remote_eligible=True)
        assert emp["headquarters_country"] == "US"
        assert emp["headquarters_evidence"] == "sec_registrant"
        assert emp["hiring_countries"] == ["US", "IE"]
        assert emp["remote_eligible"] is True

    def test_an_unplaceable_string_stores_NOTHING_rather_than_itself(self):
        """"Remote", "EMEA" and "2 Locations" are not countries, and a field
        whose whole value is that it holds ISO codes must not hold them."""
        emp = er.build_employer(
            display_name="Acme", canonical_domain="acme.example",
            hiring_countries=["Remote", "EMEA", "2 Locations"])
        assert emp["hiring_countries"] == []

    def test_an_absent_headquarters_stays_absent(self):
        emp = er.build_employer(display_name="Acme",
                                canonical_domain="acme.example")
        assert emp["headquarters_country"] is None
        assert emp["headquarters_evidence"] is None
        assert emp["remote_eligible"] is None

    def test_a_hiring_country_is_never_promoted_to_a_headquarters(self):
        """An employer hiring in Poland is not a Polish company."""
        emp = er.build_employer(
            display_name="Acme", canonical_domain="acme.example",
            hiring_countries=["PL"])
        assert emp["hiring_countries"] == ["PL"]
        assert emp["headquarters_country"] is None


class TestTheMergeDoesNotUndoTheRules:

    def test_employer_categories_are_not_merged_additively(self):
        """`$addToSet` is how one unmapped term permanently re-added `other`
        beside a real category on 1,177 employers."""
        record = er.build_employer(
            display_name="Acme", canonical_domain="acme.example",
            employer_categories=["fintech"])
        update = er._merge_update(record)
        assert "employer_categories" not in (update.get("$addToSet") or {})
        assert update["$set"]["employer_categories"] == ["fintech"]

    def test_a_source_that_only_knows_other_does_not_erase_a_real_category(self):
        record = er.build_employer(display_name="Acme",
                                   canonical_domain="acme.example",
                                   employer_categories=["not-a-category"])
        update = er._merge_update(record)
        assert "employer_categories" not in update["$set"]

    def test_a_source_that_cannot_say_does_not_erase_a_geography(self):
        record = er.build_employer(display_name="Acme",
                                   canonical_domain="acme.example")
        update = er._merge_update(record)
        for field in ("headquarters_country", "headquarters_evidence",
                      "remote_eligible"):
            assert field not in update["$set"]
