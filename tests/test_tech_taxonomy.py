"""The tech-occupation taxonomy: rules, evidence, and the golden-set gates.

The golden set lives in `evals/data/tech_taxonomy_golden.py` and the harness
in `evals/eval_tech_taxonomy.py`; this module runs it as a TEST so a rule
change that drops below the acceptance thresholds fails CI rather than being
noticed later.

The individual cases below are the ones where a wrong answer is not merely a
miss but a specific documented failure mode.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from services import tech_taxonomy as tx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "evals"))
sys.path.insert(0, str(ROOT / "evals" / "data"))


# ---------------------------------------------------------------------------
# Acceptance gates
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def golden_results():
    from eval_tech_taxonomy import run
    return run(verbose=False)


class TestAcceptance:
    def test_core_precision(self, golden_results):
        assert golden_results["precision"] >= 0.95, golden_results["misses"]

    def test_core_recall(self, golden_results):
        assert golden_results["recall"] >= 0.92, golden_results["misses"]

    def test_non_tech_leakage(self, golden_results):
        assert golden_results["leakage"] <= 0.02, golden_results["misses"]

    def test_every_classification_carries_a_version(self):
        for title in ("Backend Engineer", "Registered Nurse", "Analyst", ""):
            assert tx.classify(title).taxonomy_version == tx.TAXONOMY_VERSION

    def test_every_non_unknown_verdict_carries_evidence(self):
        for title in ("Backend Engineer", "Registered Nurse",
                      "Technical Program Manager"):
            got = tx.classify(title)
            assert got.tech_scope != tx.SCOPE_UNKNOWN
            assert got.classification_evidence, title


# ---------------------------------------------------------------------------
# The documented failure modes
# ---------------------------------------------------------------------------

class TestFailureModes:
    def test_the_employer_is_not_an_input(self):
        """`classify` must not accept an employer. If it ever does, a
        nontechnical role at a technology company can become core-tech, which
        is the product's single most visible failure."""
        import inspect
        params = set(inspect.signature(tx.classify).parameters)
        assert not (params & {"employer", "employer_name", "company",
                              "employer_id", "employer_tech"})

    def test_an_ambiguous_title_with_no_evidence_is_unknown_not_software(self):
        for title in ("Technology Analyst", "Member of Technical Staff",
                      "Solutions Consultant", "Associate Consultant",
                      "Business Analyst"):
            got = tx.classify(title)
            assert got.tech_scope == tx.SCOPE_UNKNOWN, (title, got.tech_scope)

    def test_the_same_ambiguous_title_resolves_with_body_evidence(self):
        got = tx.classify("Member of Technical Staff",
                          description="Train and serve large language models "
                                      "with PyTorch and CUDA.",
                          department="Research")
        assert got.tech_scope == tx.SCOPE_CORE
        assert got.classification_evidence

    def test_a_department_alone_cannot_promote_an_arbitrary_title(self):
        """'Executive Assistant, Information Technology' is not a tech job."""
        got = tx.classify("Executive Assistant",
                          description="Manage calendars and travel.",
                          department="Information Technology")
        assert got.tech_scope != tx.SCOPE_CORE

    def test_a_seniority_prefix_does_not_change_the_family(self):
        """'Staff Software Engineer' is software engineering AT staff level -
        filing it under leadership empties the software family of its most
        senior postings."""
        got = tx.classify("Staff Software Engineer")
        assert got.role_family == tx.F_SOFTWARE
        assert got.seniority == "staff"
        got = tx.classify("Principal Software Engineer")
        assert got.role_family == tx.F_SOFTWARE
        assert got.seniority == "principal"

    def test_a_bare_level_noun_is_leadership(self):
        got = tx.classify("Staff Engineer")
        assert got.role_family == tx.F_TECH_LEADERSHIP

    def test_a_longer_phrase_wins_the_span_it_covers(self):
        """'Design Systems Engineer' contains 'systems engineer'. Reading the
        substring made a UX role render as Cloud & Platform."""
        got = tx.classify("Design Systems Engineer")
        assert got.role_family == tx.F_UX_DESIGN_SYSTEMS
        assert got.tech_scope == tx.SCOPE_ADJACENT

    def test_a_physical_qualifier_vetoes_a_software_role_noun(self):
        for title in ("Corrosion Reliability Engineer II",
                      "Staff Hardware Reliability Engineer",
                      "Global Tooling Engineering Manager"):
            assert tx.classify(title).tech_scope != tx.SCOPE_CORE, title

    def test_embedded_and_firmware_are_software(self):
        for title in ("Embedded Software Engineer", "Firmware Engineer",
                      "Embedded Systems Engineer"):
            got = tx.classify(title)
            assert got.tech_scope == tx.SCOPE_CORE, title
            assert got.role_family == tx.F_SOFTWARE

    def test_silicon_design_is_CORE_and_in_the_default_scope(self):
        """Chip design is technology employment, so it is in the default feed.

        In tech-tax-1.0.0 `semiconductor_hardware` was adjacent, which meant
        the default scope (`["core"]`) returned nothing for a silicon
        engineer unless they found and ticked an extra box. That is not a
        taxonomy nuance, it is an empty product for a whole discipline.
        """
        for title in ("FPGA Engineer", "Design Verification Engineer",
                      "ASIC Physical Design Engineer", "RF Design Engineer",
                      "Analog IC Design Engineer", "Silicon Photonics Engineer",
                      "Yield Engineer", "ATE Engineer"):
            got = tx.classify(title)
            assert got.tech_scope == tx.SCOPE_CORE, title
            assert got.role_family == tx.F_SEMICONDUCTOR, title
        assert tx.F_SEMICONDUCTOR in tx.CORE_FAMILIES

    def test_a_bare_manufacturing_title_is_still_not_semiconductor(self):
        """The other half of promoting the family: it must not swallow the
        plant. A bare "Process Engineer" optimising a polymerization line is
        not a fab role, and "Packaging Engineer" in a warehouse is not
        advanced packaging."""
        assert tx.classify("Process Engineer",
                           description="Optimize the polymerization line.",
                           department="Manufacturing").tech_scope != tx.SCOPE_CORE
        assert tx.classify("Warehouse Packaging Engineer").tech_scope \
            != tx.SCOPE_CORE
        assert tx.classify("Semiconductor Process Engineer").role_family \
            == tx.F_SEMICONDUCTOR

    def test_physical_security_is_not_infosec(self):
        assert tx.classify("Target Security Specialist").tech_scope != tx.SCOPE_CORE
        assert tx.classify("Security Officer").tech_scope == tx.SCOPE_NON_TECH

    def test_manufacturing_qa_is_not_software_qa(self):
        assert tx.classify("Supplier Quality Engineer").tech_scope != tx.SCOPE_CORE
        assert tx.classify("Quality Control Inspector").tech_scope != tx.SCOPE_CORE
        assert tx.classify("SDET").role_family == tx.F_QUALITY

    def test_a_stack_named_role_is_software(self):
        """'Python Developer', 'SAP ABAP Engineer', 'Appian Developer' name no
        role noun the phrase table knows; they were the biggest measured
        recall gap."""
        for title in ("Python Developer", "Senior SAP ABAP Engineer",
                      "Appian Developer", "Java Developer - FI Risk",
                      "ServiceNow Developer"):
            got = tx.classify(title)
            assert got.tech_scope == tx.SCOPE_CORE, title

    def test_a_stack_named_specialist_is_not_promoted(self):
        """'specialist' is the weakest role noun and is what nontechnical
        roles use ('Data Annotation Specialist')."""
        assert tx.classify("Data Annotation Specialist").tech_scope != tx.SCOPE_CORE


# ---------------------------------------------------------------------------
# Multi-label + field extraction
# ---------------------------------------------------------------------------

class TestMultiLabel:
    def test_a_cross_cutting_role_carries_both_families(self):
        got = tx.classify("Senior Machine Learning Platform Engineer")
        assert len(got.role_families) >= 2
        assert tx.F_AI_ML in got.role_families

    def test_specialties_are_deduped_and_bounded(self):
        got = tx.classify("Senior Backend / Full Stack / API Engineer")
        assert len(got.role_specialties) == len(set(got.role_specialties))
        assert len(got.role_specialties) <= 6

    def test_occupation_codes_come_from_the_specialties(self):
        got = tx.classify("Data Engineer")
        assert got.occupation_codes
        assert all("-" in c for c in got.occupation_codes)


class TestExtraction:
    def test_required_years_is_the_lowest_stated(self):
        got = tx.classify("Backend Engineer",
                          description="3+ years of experience required. "
                                      "8+ years of experience with Kafka preferred.")
        assert got.required_years_min == 3

    def test_absent_years_stays_none_never_zero(self):
        assert tx.classify("Backend Engineer").required_years_min is None

    def test_absurd_year_counts_are_ignored(self):
        got = tx.classify("Backend Engineer",
                          description="99 years of experience required")
        assert got.required_years_min is None

    def test_required_and_preferred_skills_are_separated(self):
        got = tx.classify(
            "Backend Engineer",
            description="Requirements: Python and PostgreSQL.\n"
                        "Preferred: Kubernetes and Terraform.")
        assert "python" in got.skills_required
        assert "kubernetes" in got.skills_preferred
        assert "kubernetes" not in got.skills_required

    def test_plural_terms_match_their_singular_rule(self):
        got = tx.classify("Backend Engineer",
                          description="Build microservices and data pipelines.")
        assert got.skills_required

    def test_the_normalized_title_strips_requisition_noise(self):
        assert tx.normalize_title("Backend Engineer (Remote) - Req 12345") \
            == "Backend Engineer"
        assert tx.normalize_title("Software Engineer [R-99881]") == "Software Engineer"


class TestCatalog:
    def test_the_catalog_covers_every_family_and_specialty(self):
        cat = tx.taxonomy_catalog()
        assert cat["taxonomy_version"] == tx.TAXONOMY_VERSION
        keys = {f["key"] for f in cat["families"]}
        assert keys == set(tx.ALL_FAMILIES)
        specs = {s["key"] for f in cat["families"] for s in f["specialties"]}
        assert specs == set(tx.ALL_SPECIALTIES)

    def test_every_family_declares_its_scope(self):
        for f in tx.taxonomy_catalog()["families"]:
            assert f["scope"] in (tx.SCOPE_CORE, tx.SCOPE_ADJACENT)

    def test_the_mission_specialties_are_all_present(self):
        required = {
            "backend", "frontend", "full_stack", "web_platform", "mobile",
            "desktop", "distributed_systems", "systems_programming", "apis",
            "developer_tools", "browsers", "games_xr", "embedded", "firmware",
            "ml_engineering", "applied_ai", "research_engineering",
            "research_science", "genai_llm", "nlp", "speech",
            "computer_vision", "recsys_search_ranking", "robotics_autonomy",
            "mlops_llmops", "ai_platform", "inference_performance",
            "ai_safety", "red_teaming", "evaluations",
            "data_engineering", "data_platform", "analytics_engineering",
            "data_science", "business_intelligence", "streaming",
            "database_engineering", "data_governance", "ml_data",
            "cloud_engineering", "devops", "sre", "platform_engineering",
            "infrastructure", "kubernetes_containers",
            "production_engineering", "observability", "networking",
            "cloud_security", "finops",
            "application_security", "product_security", "iam_pam", "secops",
            "detection_response", "penetration_testing",
            "malware_reverse_engineering", "grc",
            "qa_automation", "sdet", "performance_testing",
            "test_infrastructure",
            "engineering_management", "architecture", "principal_staff_ic",
            "technical_product_management", "tpm", "solutions_engineering",
            "customer_engineering", "developer_relations", "technical_writing",
            "design_systems", "it_support", "network_operations",
            "asic_soc_design", "fpga_rtl", "verification",
        }
        missing = required - set(tx.ALL_SPECIALTIES)
        assert not missing, f"taxonomy is missing: {sorted(missing)}"
