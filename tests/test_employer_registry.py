"""One employer, one id — and never a merge on a name.

Every wrong pairing asserted here is a live one. `stripe` matched "STRIPES
SOLUTIONS LLC", `notion` matched "NOTIONSBYNANCY LLC", `ramp` matched
"RAMPART RANGE MOTORCYCLE MANAGEMENT COMMITTEE" and Capital One was given the
domain `capital.com`, a CFD trading site, in `aspirely_db` on 2026-09-02.
"""
from __future__ import annotations

import pytest

from services import employer_registry as er


class TestDomainNormalisation:
    @pytest.mark.parametrize("raw, want", [
        ("https://www.stripe.com/careers", "stripe.com"),
        ("WWW.Stripe.COM", "stripe.com"),
        ("jobs.lever.co", "lever.co"),
        ("careers.sub.example.co.uk", "example.co.uk"),
        ("http://example.com:8443/x?y=1", "example.com"),
        ("mailto:jobs@acme.com", "acme.com"),
    ])
    def test_registrable_domain(self, raw, want):
        assert er.normalize_domain(raw) == want

    @pytest.mark.parametrize("raw", ["", None, "localhost", "not a domain",
                                     "192.168.1.1.", "10.0.0.1", "  "])
    def test_unusable_input_is_no_evidence_not_a_key(self, raw):
        assert er.normalize_domain(raw) == ""


class TestEmployerId:
    def test_domain_beats_name(self):
        a = er.employer_id_for(domain="stripe.com", name="Stripe")
        b = er.employer_id_for(domain="https://www.stripe.com/jobs", name="Stripe, Inc.")
        assert a == b

    def test_two_domains_are_two_employers(self):
        assert er.employer_id_for(domain="stripe.com") != er.employer_id_for(domain="stripes.com")

    def test_deterministic_across_calls(self):
        assert er.employer_id_for(name="Acme Widgets") == er.employer_id_for(name="Acme Widgets, Inc.")

    def test_nothing_identifying_raises_rather_than_minting_a_junk_row(self):
        with pytest.raises(ValueError):
            er.employer_id_for()


class TestIdentityOverrides:
    def test_capital_one_never_gets_capital_dot_com(self):
        row = er.build_employer(display_name="Capital One",
                                canonical_domain="capital.com")
        assert row["canonical_domain"] is None
        assert row["id_basis"] == "name"
        assert any(e["kind"] == "domain_rejected" for e in row["evidence"])

    def test_capital_dot_com_is_still_a_fine_domain_for_someone_else(self):
        row = er.build_employer(display_name="Capital.com", canonical_domain="capital.com")
        assert row["canonical_domain"] == "capital.com"

    @pytest.mark.parametrize("bad, real", [
        ("STRIPES SOLUTIONS LLC", "Stripe"),
        ("NOTIONSBYNANCY LLC", "Notion Labs"),
        ("RAMPART RANGE MOTORCYCLE MANAGEMENT COMMITTEE", "Ramp"),
        ("NUTMEG CONTAINER CORPORATION", "JPMorgan Chase"),
        ("M.J. WILLIAMS COMPANY", "Boeing"),
        ("COBAS SERVICES LLC", "Roche"),
        ("AIO GROUND BREAKING LLC", "AT&T"),
        ("Abbott BBQ", "Abbott"),
        ("INTUITIVE MACHINES", "Intuit"),
    ])
    def test_documented_wrong_names_are_blacklisted(self, bad, real):
        assert er.wrong_name_keys().get(er.name_key(bad)) == real

    def test_no_override_blacklists_itself(self):
        """`name_key` strips legal suffixes, so "RAMP HOLDINGS INC" reduces to
        `ramp` — the employer's own key. Listing it made Ramp blacklist Ramp,
        and `resolve_employer(name="Ramp")` returned None."""
        for row in er.IDENTITY_OVERRIDES:
            own = {er.name_key(x) for x in
                   [row["display_name"], *(row.get("aliases") or ()),
                    *(row.get("legal_names") or ()), *(row.get("brands") or ())]}
            for bad in row.get("wrong_names") or ():
                assert er.name_key(bad) not in own, (row["display_name"], bad)

    def test_a_blacklisted_employer_still_resolves_as_itself(self):
        assert er.wrong_name_keys().get("ramp") is None
        assert er.override_for_name("Ramp") is not None


class TestMergeDecision:
    def _emp(self, name, domain=None):
        return er.build_employer(display_name=name, canonical_domain=domain)

    def test_same_verified_domain_merges(self):
        a = self._emp("Stripe", "stripe.com")
        b = self._emp("Stripe, Inc.", "https://stripe.com")
        assert er.merge_decision(a, b)[0] == er.MERGE

    def test_two_domains_are_distinct(self):
        verdict, why = er.merge_decision(self._emp("Forward", "goforward.com"),
                                         self._emp("Forward", "forwardnetworks.com"))
        assert verdict == er.DISTINCT
        assert "different verified domains" in why

    @pytest.mark.parametrize("name", ["Forward", "Beyond", "Public", "CTS",
                                      "Super", "Archer"])
    def test_a_bare_name_collision_is_quarantined_never_merged(self, name):
        verdict, why = er.merge_decision(self._emp(name), self._emp(name))
        assert verdict == er.QUARANTINE
        assert "name-only" in why

    def test_a_curated_blacklist_beats_every_similarity(self):
        verdict, _ = er.merge_decision(self._emp("Stripe"),
                                       self._emp("STRIPES SOLUTIONS LLC"))
        assert verdict == er.DISTINCT

    def test_a_brand_merges_into_its_parent(self):
        verdict, why = er.merge_decision(self._emp("Salesforce"), self._emp("Slack"))
        assert verdict == er.MERGE
        assert "curated alias" in why

    def test_bmw_alphabet_is_not_google_alphabet(self):
        verdict, _ = er.merge_decision(self._emp("Alphabet"),
                                       self._emp("ALPHABET (BMW GROUP)"))
        assert verdict == er.DISTINCT

    def test_owner_verified_tenant_is_enough(self):
        verdict, why = er.merge_decision(self._emp("Acme"), self._emp("Acme Holdings"),
                                         shared_owner_verified_tenant=True)
        assert verdict == er.MERGE
        assert "tenant" in why


class TestBuildEmployer:
    def test_categories_are_translated_into_one_vocabulary(self):
        row = er.build_employer(display_name="Databricks", canonical_domain="databricks.com",
                                employer_categories=["big-tech", "data", "ai-ml"])
        assert set(row["employer_categories"]) == {"software_saas", "cloud_data", "ai_ml"}

    def test_an_unknown_category_becomes_other_rather_than_vanishing(self):
        row = er.build_employer(display_name="X", canonical_domain="x.example",
                                employer_categories=["quantum-widgets"])
        assert row["employer_categories"] == ["other"]

    def test_cohort_defaults_to_unknown_not_to_a_guess(self):
        assert er.build_employer(display_name="X", canonical_domain="x.example")["cohorts"] \
            == [er.COHORT_UNKNOWN]

    def test_operating_status_vocabulary_is_closed(self):
        row = er.build_employer(display_name="X", canonical_domain="x.example",
                                operating_status="probably_fine")
        assert row["operating_status"] == er.STATUS_ACTIVE

    def test_confidence_reflects_the_id_basis(self):
        dom = er.build_employer(display_name="X", canonical_domain="x.example")
        nm = er.build_employer(display_name="Y")
        assert dom["identity_confidence"] > nm["identity_confidence"]
        assert nm["id_basis"] == "name"


@pytest.fixture()
def registry_db(mock_db):
    mock_db[er.EMPLOYERS].delete_many({})
    mock_db[er.MERGE_REVIEWS].delete_many({})
    return mock_db


class TestPersistence:
    def test_upsert_is_idempotent(self, registry_db):
        row = er.build_employer(display_name="Stripe", canonical_domain="stripe.com")
        eid = er.upsert_employer(row)
        er.upsert_employer(row)
        assert registry_db[er.EMPLOYERS].count_documents({"employer_id": eid}) == 1

    def test_a_weaker_source_cannot_downgrade_a_verified_one(self, registry_db):
        strong = er.build_employer(display_name="Stripe", canonical_domain="stripe.com")
        eid = er.upsert_employer(strong)
        weak = dict(strong)
        weak["display_name"] = "STRIPE PAYMENTS LLC"
        weak["identity_confidence"] = 0.1
        er.upsert_employer(weak)
        assert er.get_employer(eid)["display_name"] == "Stripe"

    def test_aliases_accumulate_and_are_never_replaced(self, registry_db):
        base = er.build_employer(display_name="AT&T", canonical_domain="att.com",
                                 aliases=["ATT"])
        eid = er.upsert_employer(base)
        more = er.build_employer(display_name="AT&T", canonical_domain="att.com",
                                 aliases=["AT and T"])
        er.upsert_employer(more)
        assert set(er.get_employer(eid)["aliases"]) >= {"ATT", "AT and T"}

    def test_resolve_by_domain(self, registry_db):
        eid = er.upsert_employer(er.build_employer(display_name="Ramp",
                                                   canonical_domain="ramp.com"))
        assert er.resolve_employer(domain="https://ramp.com/careers")["employer_id"] == eid

    def test_resolve_refuses_a_blacklisted_name(self, registry_db):
        er.upsert_employer(er.build_employer(display_name="Ramp", canonical_domain="ramp.com"))
        assert er.resolve_employer(
            name="RAMPART RANGE MOTORCYCLE MANAGEMENT COMMITTEE") is None

    def test_resolve_refuses_an_ambiguous_name(self, registry_db):
        er.upsert_employer(er.build_employer(display_name="Forward",
                                             curated_key="forward-health"))
        er.upsert_employer(er.build_employer(display_name="Forward",
                                             curated_key="forward-air"))
        assert er.resolve_employer(name="Forward") is None

    def test_an_acquired_employer_follows_its_parent(self, registry_db):
        parent = er.upsert_employer(er.build_employer(display_name="Broadcom",
                                                      canonical_domain="broadcom.com"))
        child = er.build_employer(display_name="VMware", canonical_domain="vmware.com",
                                  operating_status=er.STATUS_ACQUIRED,
                                  parent_employer_id=parent)
        er.upsert_employer(child)
        got = er.resolve_employer(domain="vmware.com")
        assert got["employer_id"] == parent

    def test_rekey_keeps_the_old_id_resolvable(self, registry_db):
        old = er.upsert_employer(er.build_employer(display_name="Turso"))
        new = er.rekey_to_domain(old, "turso.tech",
                                 evidence={"kind": "domain_verified", "source": "test",
                                           "value": "turso.tech"})
        assert new != old
        assert er.get_employer(old)["operating_status"] == er.STATUS_MERGED
        assert er._follow(er.get_employer(old))["employer_id"] == new

    def test_quarantine_is_idempotent_and_reviewable(self, registry_db):
        a = er.build_employer(display_name="Public", curated_key="public-investing")
        b = er.build_employer(display_name="Public", curated_key="public-storage")
        er.quarantine_merge(a, b, "name-only match")
        er.quarantine_merge(a, b, "name-only match")
        assert registry_db[er.MERGE_REVIEWS].count_documents({"resolved": False}) == 1

    def test_no_update_document_uses_slice_with_addToSet(self):
        """`$slice` is a `$push` modifier. MongoDB rejects the ENTIRE update
        with "Found unexpected fields after $each in $addToSet" when it
        appears there — and in an unordered `bulk_write` that rejection is a
        per-op write error, not an exception, so it dropped 16,339 of 24,175
        employers in silence. mongomock does not enforce this, so the shape
        itself is what gets asserted."""
        row = er.build_employer(display_name="Stripe", canonical_domain="stripe.com",
                                aliases=["Stripe Inc"],
                                evidence=[{"kind": "k", "source": "s", "value": "v"}])
        update = er._merge_update(row)
        for op, spec in (update.get("$addToSet") or {}).items():
            assert "$slice" not in spec, op
            assert set(spec) == {"$each"}, op

    def test_bulk_and_single_upsert_agree(self, registry_db):
        rows = [er.build_employer(display_name=f"Bulk {i}",
                                  canonical_domain=f"bulk{i}.example",
                                  evidence=[{"kind": "seed", "source": "t", "value": str(i)}])
                for i in range(5)]
        stats = er.bulk_upsert_employers(rows)
        assert stats["seen"] == 5
        assert registry_db[er.EMPLOYERS].count_documents({}) == 5
        # Re-running must not duplicate evidence — `$addToSet`, not `$push`.
        er.bulk_upsert_employers(rows)
        assert registry_db[er.EMPLOYERS].count_documents({}) == 5
        got = er.get_employer(rows[0]["employer_id"])
        assert len(got["evidence"]) == 1

    def test_counts_shape(self, registry_db):
        er.upsert_employer(er.build_employer(display_name="Stripe",
                                             canonical_domain="stripe.com",
                                             cohorts=["private_growth"],
                                             employer_categories=["fintech"]))
        got = er.counts()
        assert got["total"] == 1
        assert got["by_cohort"]["private_growth"] == 1
        assert got["by_category"]["fintech"] == 1
        assert got["domain_verified"] == 1
