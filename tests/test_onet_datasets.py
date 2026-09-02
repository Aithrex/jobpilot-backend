"""The imported O*NET vocabulary, and the overlay that covers what it lacks.

The point of importing a dataset rather than writing a list is that the
vocabulary is SOURCED. The point of these tests is that it stays sourced, that
the import cannot silently become empty, and that it never overrides the
market evidence the taxonomy already holds.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from services import onet_overlay as ov      # noqa: E402
from services import tech_taxonomy as tx     # noqa: E402


class TestImportedDataset:
    def test_the_dataset_is_present_and_versioned(self):
        from services import onet_data as od
        assert od.ONET_VERSION.startswith("onet-")
        assert od.ONET_RELEASE
        assert od.ONET_IMPORTED_AT

    def test_it_is_not_empty(self):
        """A silently empty import is worse than no import: the vocabulary
        looks sourced and does nothing. `onet_titles()` was exactly that for
        one commit, because it ran before `_norm` was defined and the
        `NameError` went into a bare `except`."""
        from services import onet_data as od
        assert len(od.ALL_TITLES) > 500
        assert len(od.ALL_SKILLS) > 500
        assert len(od.HOT_TECHNOLOGIES) > 50

    def test_office_products_are_excluded(self):
        """O*NET lists "Microsoft Word" as software. It names no engineering
        skill, and matching it would make every job with an office suite look
        technical."""
        from services import onet_data as od
        for noise in ("Microsoft Word", "Microsoft PowerPoint",
                      "Adobe Acrobat", "Google Docs"):
            assert noise not in od.ALL_SKILLS, noise

    def test_the_taxonomy_actually_reaches_the_titles(self):
        assert len(tx.onet_titles()) > 300


class TestOnetNeverOverridesMarketEvidence:
    @pytest.mark.parametrize("title", [
        "Technology Analyst", "Business Analyst", "Business Systems Analyst",
        "Member of Technical Staff", "Solutions Consultant",
        "Associate Consultant", "Systems Analyst",
    ])
    def test_a_bare_ambiguous_title_stays_unknown(self, title):
        """O*NET lists these under computer SOC codes and is not wrong to —
        they ARE computer occupations in the survey's frame. In a job board
        they are used across finance, operations and IT, which is why
        `_AMBIGUOUS_TITLE_RE` exists. The import must not undo it."""
        assert tx.classify(title).tech_scope == tx.SCOPE_UNKNOWN, title

    @pytest.mark.parametrize("title", ["Analyst Programmer", "Application Analyst"])
    def test_a_qualified_title_the_phrase_rules_miss_is_rescued(self, title):
        """The gain: titles nothing in the taxonomy names, that O*NET does."""
        assert tx.classify(title).tech_scope in (tx.SCOPE_CORE, tx.SCOPE_ADJACENT)

    def test_a_non_computer_soc_contributes_no_titles(self):
        """The crosswalk legitimately maps `grc` to 13-1041 (Compliance
        Officers) and `tpm` to 13-1082 (Project Management Specialists). A
        specialty mapping is a statement about where a role sits; it is not a
        licence to read every title in that occupation as technical."""
        assert tx.classify("Air Pollution Compliance Inspector").tech_scope \
            != tx.SCOPE_CORE
        assert tx.classify("Environmental Compliance Inspector").tech_scope \
            != tx.SCOPE_CORE

    def test_a_clinical_title_is_still_vetoed(self):
        assert tx.classify("Informatics Nurse Specialist").tech_scope \
            == tx.SCOPE_NON_TECH


class TestOverlay:
    def test_the_overlay_is_versioned_and_dated(self):
        assert ov.OVERLAY_VERSION.startswith("onet-overlay-")
        assert ov.OVERLAY_DATED

    def test_every_overlay_family_is_a_real_taxonomy_family(self):
        for family in ov.MODERN_TOOLS:
            assert family in tx.ALL_FAMILIES, family

    def test_every_role_alias_maps_to_a_real_specialty(self):
        for alias, (family, specialty) in ov.ROLE_ALIASES.items():
            assert family in tx.ALL_FAMILIES, alias
            assert specialty in tx.ALL_SPECIALTIES, (alias, specialty)
            assert specialty in tx.FAMILY_SPECIALTIES.get(family, ()), alias

    def test_the_overlay_earns_its_place(self):
        """An overlay whose terms O*NET already has is dead weight. This is
        the audit that finds entries to DELETE."""
        missing = ov.missing_from_onet()
        assert len(missing) >= 0.8 * len(ov.ALL_TOOLS), (
            f"only {len(missing)} of {len(ov.ALL_TOOLS)} overlay terms are "
            f"absent from O*NET; the rest can be deleted")

    @pytest.mark.parametrize("term", [
        "LLM", "RAG", "vector database", "MLOps", "platform engineering",
        "eBPF", "WebAssembly", "dbt", "Apache Iceberg", "RISC-V", "chiplet",
        "post-quantum cryptography", "SBOM", "Rust", "Bun", "Deno",
    ])
    def test_the_modern_terms_the_survey_lacks_are_covered(self, term):
        assert term in ov.ALL_TOOLS, term

    def test_the_overlay_reaches_the_classifier(self):
        got = tx.classify("Engineer", description=(
            "Own our RAG pipeline: vector database, embeddings and LLM "
            "evaluation with a Kubernetes-based serving stack."))
        assert got.tech_scope == tx.SCOPE_CORE


class TestImporter:
    def test_the_importer_filters_to_our_occupations(self):
        import import_onet

        socs = import_onet.target_socs()
        assert "15-1252.00" in socs          # Software Developers
        assert "17-2061.00" in socs          # Computer Hardware Engineers
        assert len(socs) >= 25

    def test_the_importer_drops_office_products(self):
        import import_onet

        assert import_onet._is_noise("Microsoft Word")
        assert import_onet._is_noise("Adobe Acrobat Reader")
        assert not import_onet._is_noise("Kubernetes")
        assert not import_onet._is_noise("Apache Kafka")

    def test_generated_output_declares_itself_generated(self):
        import import_onet

        alt = ("O*NET-SOC Code\tAlternate Title\tShort Title\tSource(s)\n"
               "15-1252.00\tBackend Developer\tn/a\t08\n")
        tech = ("O*NET-SOC Code\tExample\tCommodity Code\tCommodity Title\t"
                "Hot Technology\tIn Demand\n"
                "15-1252.00\tKubernetes\t43233004\tPlatform software\tY\tY\n"
                "15-1252.00\tMicrosoft Word\t43232110\tWord processing\tN\tN\n")
        body = import_onet.build(alt, tech, release="30_0")
        assert "GENERATED FILE" in body
        assert "import_onet.py" in body
        assert "Backend Developer" in body
        assert "Kubernetes" in body
        assert "Microsoft Word" not in body
        assert 'ONET_VERSION = "onet-30.0"' in body
