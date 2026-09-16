"""`ineligible` means a DOMAIN change, not an unfamiliar technology family.

`fit_score._eligibility` turns `role_alignment(...)["cross_domain"]` into a
hard INELIGIBLE verdict, and ineligible rows are pulled out of the ranked
feed entirely. So the cost of over-firing is a job the candidate never sees.

Measured on live data for the account the brief names: 522 of 2,500 core-tech
postings were ineligible, and 478 of those were security or technical-
leadership roles — a software-and-cloud resume being told it may not apply for
"Information Security Engineer" or "Staff Engineer". Neither is a domain
change; both are ordinary next jobs.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "shared" / "python"))

from services import fit_score as fs           # noqa: E402
from services import tech_taxonomy as tx       # noqa: E402


def _align(**kw):
    return tx.role_alignment(**kw)


class TestTheVetoStillFiresOnARealDomainChange:
    def test_a_non_tech_resume_against_a_technical_role(self):
        got = _align(job_families=["software_engineering"], job_specialties=["backend"],
                     resume_families=[], resume_specialties=[],
                     job_scope=tx.SCOPE_CORE, resume_scope=tx.SCOPE_NON_TECH,
                     resume_non_tech_domain="education_public")
        assert got["cross_domain"] is True
        assert got["tier"] == tx.ALIGN_UNRELATED

    def test_a_technical_resume_against_a_non_tech_role(self):
        """The mirror case, which previously fell through to `unknown` (0.60,
        neutral) because a non-tech posting carries no tech families to
        compare against."""
        got = _align(job_families=[], job_specialties=[],
                     resume_families=["software_engineering"],
                     resume_specialties=["backend"],
                     job_scope=tx.SCOPE_NON_TECH, resume_scope=tx.SCOPE_CORE)
        assert got["cross_domain"] is True
        assert got["tier"] == tx.ALIGN_UNRELATED


class TestTwoTechnologyFamiliesAreNeverADomainChange:
    def test_an_unrelated_pair_still_scores_unrelated_but_stays_eligible(self):
        got = _align(job_families=["semiconductor_hardware"],
                     job_specialties=["analog_mixed_signal"],
                     resume_families=["technical_program"],
                     resume_specialties=["technical_writing"],
                     job_scope=tx.SCOPE_CORE, resume_scope=tx.SCOPE_CORE)
        assert got["tier"] == tx.ALIGN_UNRELATED
        assert got["cross_domain"] is False, (
            "0.08 is already a severe penalty; removing the row from the feed "
            "on top of it is a second, harsher judgement")

    def test_the_penalty_is_unchanged(self):
        assert tx.ALIGNMENT_WEIGHT[tx.ALIGN_UNRELATED] == 0.08


class TestTheCareersLeadershipIsStaffedFrom:
    @pytest.mark.parametrize("ic", ["backend", "full_stack", "general_software",
                                    "distributed_systems", "platform_engineering"])
    def test_an_ic_specialty_reaches_a_staff_or_principal_posting(self, ic):
        got = _align(job_families=["technical_leadership"],
                     job_specialties=["principal_staff_ic"],
                     resume_families=["software_engineering"],
                     resume_specialties=[ic],
                     job_scope=tx.SCOPE_CORE, resume_scope=tx.SCOPE_CORE)
        assert got["tier"] != tx.ALIGN_UNRELATED

    @pytest.mark.parametrize("ic", ["backend", "cloud_engineering",
                                    "infrastructure", "data_platform"])
    def test_an_ic_specialty_reaches_an_architect_posting(self, ic):
        got = _align(job_families=["technical_leadership"],
                     job_specialties=["architecture"],
                     resume_families=["software_engineering"],
                     resume_specialties=[ic],
                     job_scope=tx.SCOPE_CORE, resume_scope=tx.SCOPE_CORE)
        assert got["tier"] != tx.ALIGN_UNRELATED

    def test_an_engineering_manager_posting_is_reachable_through_the_bridge(self):
        """No direct pair is declared for it; the family bridge is what makes
        it reachable, which is exactly what the bridge is for."""
        got = _align(job_families=["technical_leadership"],
                     job_specialties=["engineering_management"],
                     resume_families=["software_engineering"],
                     resume_specialties=["backend"],
                     job_scope=tx.SCOPE_CORE, resume_scope=tx.SCOPE_CORE)
        assert got["tier"] == tx.ALIGN_ADJACENT_FAMILY


class TestSecurityIsReachableFromSoftwareAndCloud:
    @pytest.mark.parametrize("spec", ["application_security", "product_security",
                                      "secops", "penetration_testing"])
    def test_a_backend_resume_is_not_unrelated_to_security(self, spec):
        got = _align(job_families=["security"], job_specialties=[spec],
                     resume_families=["software_engineering"],
                     resume_specialties=["backend"],
                     job_scope=tx.SCOPE_CORE, resume_scope=tx.SCOPE_CORE)
        assert got["tier"] != tx.ALIGN_UNRELATED

    @pytest.mark.parametrize("spec", ["cloud_security", "secops", "iam_pam"])
    def test_a_cloud_resume_is_not_unrelated_to_security(self, spec):
        got = _align(job_families=["security"], job_specialties=[spec],
                     resume_families=["cloud_platform"],
                     resume_specialties=["cloud_engineering"],
                     job_scope=tx.SCOPE_CORE, resume_scope=tx.SCOPE_CORE)
        assert got["tier"] != tx.ALIGN_UNRELATED


class TestTheDeclaredMatrixIsVersioned:
    def test_the_version_moved_with_the_pairs(self):
        """The matrix is a declared, versioned artifact; adding edges without
        moving the version makes two different matrices answer to one name."""
        # 1.2.0 added the quantum cluster: quantum_software / quantum_hardware
        # / quantum_control / cryogenic_systems and the 17 edges that connect
        # them to each other and to the specialties people actually move
        # between (systems programming, FPGA/RTL, analog, HPC, photonics).
        #
        # 1.3.0 added two: (data_science, ml_engineering) and (data_science,
        # applied_ai). A data scientist and an ML engineer were reading
        # `adjacent_family` (0.46) — a claim that the two disciplines barely
        # reach each other, when they are the same hiring pool and the same
        # résumés. `nlp`, `computer_vision` and `genai_llm` are deliberately
        # NOT included: hiring for those does ask for the specialism.
        assert tx.ADJACENCY_VERSION == "tech-adjacency-1.3.0"
        for pair in (("data_science", "ml_engineering"),
                     ("data_science", "applied_ai")):
            assert tx.specialties_are_adjacent(*pair), pair
            assert tx.specialties_are_adjacent(*reversed(pair)), pair
        # And the ones 1.3.0 refused, so the boundary is asserted and not
        # merely intended.
        for pair in (("data_science", "nlp"),
                     ("data_science", "computer_vision"),
                     ("data_science", "genai_llm")):
            assert not tx.specialties_are_adjacent(*pair), pair

    def test_every_declared_pair_names_real_specialties(self):
        known = set(tx.SPECIALTY_LABELS)
        unknown = {s for pair in tx._ADJACENT_PAIRS for s in pair if s not in known}
        assert not unknown, f"adjacency names specialties that do not exist: {sorted(unknown)}"

    def test_adjacency_is_symmetric(self):
        for a, adj in tx.SPECIALTY_ADJACENCY.items():
            for b in adj:
                assert a in tx.SPECIALTY_ADJACENCY.get(b, frozenset()), \
                    f"{a} -> {b} but not back"
