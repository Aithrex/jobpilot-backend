"""Regression cases for the match-score precision fixes.

Each class documents a row that a live US-only devops feed ranked ABOVE
genuine SRE roles for a cloud engineer, and the specific defect that let it.
"""
import pytest

from services import fit_score


def _cloud_engineer_profile():
    return fit_score.build_resume_profile({
        "summary": "Cloud and DevOps engineer.",
        "experience": [{
            "title": "Cloud Engineer", "company": "Acme", "dates": "2024-2026",
            "bullets": [
                "Ran Kubernetes on AWS with Terraform modules.",
                "Built CI/CD pipelines and Prometheus monitoring.",
            ]}],
        "skills": {"s": ["AWS", "Kubernetes", "Terraform", "Python",
                         "Docker", "Linux", "CI/CD", "Monitoring"]},
    })


class TestPhysicalQualifierVeto:
    """"Reliability Engineer" and "Systems Engineer" are software role nouns,
    but a physical-domain qualifier in front of them changes the job entirely.
    30 of the 565 postings stored in the `devops` family were roles of this
    shape."""

    @pytest.mark.parametrize("title", [
        "Corrosion Reliability Engineer II/III",
        "Staff Hardware Reliability Engineer",
        "Build Reliability Engineer II, Propulsion",
        "Senior Reliability Engineer - Power Electronics",
        "Instrument and Electrical Reliability Engineer",
        "Power Systems Engineer III - Generator/Plant Testing",
    ])
    def test_physical_roles_leave_the_software_families(self, title):
        assert fit_score.classify_role(title) == "hardware_engineering"

    @pytest.mark.parametrize("title,family", [
        ("Site Reliability Engineer", "devops"),
        ("SRE", "devops"),
        ("Cloud Engineer", "devops"),
        ("Senior Platform Engineer", "devops"),
        ("Release Engineer", "devops"),
        ("Infrastructure Engineer", "devops"),
        ("Build Engineer - Bazel", "devops"),
        ("DevOps Engineer", "devops"),
        ("Software Engineer", "software_engineering"),
        ("Backend Engineer", "software_engineering"),
        ("Staff Systems Engineer, Messaging Technologies", "software_engineering"),
        ("Security Engineer", "security"),
        ("Data Engineer", "data_engineering"),
    ])
    def test_real_software_titles_are_untouched(self, title, family):
        assert fit_score.classify_role(title) == family

    def test_veto_is_reapplied_to_a_stale_stored_family(self):
        """Postings ingested before the veto still carry `devops` on disk, and
        retrieval filters on that field. The scorer re-applies the veto against
        the live title so the fix does not wait for a re-crawl."""
        verdict = fit_score.score_one(
            title="Corrosion Reliability Engineer II/III",
            profile=_cloud_engineer_profile(),
            job={"requirements": {
                "score_version": fit_score.FIT_SCORE_VERSION,
                "requirements_source": "sectioned", "role_family": "devops",
                "required_skills": ["teamwork", "monitoring", "welding",
                                    "inspection"],
                "preferred_skills": [], "required_years": 2,
                "required_degree_level": 3, "job_level": "senior",
                "industries": [], "work_mode": "onsite", "licenses": [],
                "clearance_required": False,
                "work_authorization_restricted": False}})
        assert verdict["evidence"]["job_role_family"] == "hardware_engineering"
        assert verdict["match_score"] < 60


class TestTitleOutranksBodyDerivedFamily:
    """`extract_requirements` falls back to classifying off the JD BODY when
    the title yields nothing, and a body is full of incidental engineering
    vocabulary. "Temporary Research Assistant" is stored as
    `software_engineering` on that basis."""

    def test_body_derived_software_family_is_not_trusted(self):
        verdict = fit_score.score_one(
            title="Temporary Research Assistant",
            profile=_cloud_engineer_profile(),
            job={"requirements": {
                "score_version": fit_score.FIT_SCORE_VERSION,
                "requirements_source": "sectioned",
                "role_family": "software_engineering",
                "required_skills": ["monitoring"],
                "preferred_skills": ["recruiting", "statistics"],
                "required_years": None, "required_degree_level": 3,
                "job_level": "entry", "industries": [], "work_mode": "onsite",
                "licenses": [], "clearance_required": False,
                "work_authorization_restricted": False}})
        assert verdict["evidence"]["job_role_family"] is None
        # It scored 86 and led the feed before these fixes.
        assert verdict["match_score"] < 70


class TestCoverageDenominatorFloor:
    """One requirement, matched, is not a complete match."""

    def test_one_of_one_is_not_full_coverage(self):
        prof = _cloud_engineer_profile()
        cov, covered, missing = fit_score._coverage(["monitoring"], prof)
        assert covered == ["monitoring"] and missing == []
        assert cov == pytest.approx(1 / 3)

    def test_two_of_two_is_two_thirds(self):
        cov, _, _ = fit_score._coverage(["aws", "kubernetes"],
                                        _cloud_engineer_profile())
        assert cov == pytest.approx(2 / 3)

    def test_lists_at_or_above_the_floor_are_unchanged(self):
        prof = _cloud_engineer_profile()
        cov, _, _ = fit_score._coverage(["aws", "kubernetes", "terraform"], prof)
        assert cov == pytest.approx(1.0)


class TestJobEvidenceGate:
    """A posting must name discriminating requirements before its coverage
    ratio is treated as measurable."""

    def _reqs(self, required, preferred=()):
        return {"requirements_source": "sectioned",
                "required_skills": list(required),
                "preferred_skills": list(preferred)}

    def test_two_hard_skills_is_enough(self):
        assert fit_score.has_job_requirements(
            self._reqs(["kubernetes", "terraform"])) is True

    def test_a_soft_skill_plus_one_incidental_is_not(self):
        # The live "Corrosion Reliability Engineer" profile.
        assert fit_score.has_job_requirements(
            self._reqs(["teamwork", "monitoring"])) is False

    def test_an_all_generic_list_is_not(self):
        # "Power Systems Engineer III" published exactly this.
        assert fit_score.has_job_requirements(
            self._reqs(["compliance", "documentation", "communication"])) is False

    def test_three_items_with_one_hard_skill_is_enough(self):
        assert fit_score.has_job_requirements(
            self._reqs(["python", "communication", "teamwork"])) is True

    def test_no_source_is_never_measurable(self):
        assert fit_score.has_job_requirements(
            {"requirements_source": "none",
             "required_skills": ["python", "aws", "kubernetes"]}) is False

    def test_failing_the_gate_caps_rather_than_hides(self):
        verdict = fit_score.score_one(
            title="Reliability Engineer", profile=_cloud_engineer_profile(),
            job={"requirements": {
                "score_version": fit_score.FIT_SCORE_VERSION,
                "requirements_source": "sectioned",
                "required_skills": ["teamwork", "monitoring"],
                "preferred_skills": [], "required_years": None,
                "required_degree_level": 0, "role_family": "devops",
                "job_level": None, "industries": [], "work_mode": "onsite",
                "licenses": [], "clearance_required": False,
                "work_authorization_restricted": False}})
        assert "job_evidence" in verdict["applied_caps"]
        assert verdict["match_score"] <= 45
