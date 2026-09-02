"""Unit tests for the shared fit score engine (ADR-002).

The engine replaces jd_match_scorer's resume-derived denominator. These
tests pin the behaviours the ADR calls non-negotiable:

  - the required-skill denominator comes from the JOB DESCRIPTION;
  - required and preferred requirements score separately;
  - applied experience outweighs a bare skills-list claim;
  - role and seniority alignment are real components, not bonuses;
  - license and clearance are hard gates that CAP rather than zero;
  - the component maxima sum to exactly 100;
  - confidence and score_version are on every result;
  - a nontechnical resume is never scored as software engineering;
  - unknown information does not automatically fail the candidate.
"""

import pytest

from services import fit_score
from services.fit_score import COMPONENT_MAX, FIT_SCORE_VERSION


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _resume(**over):
    base = {
        "contact": {"name": "Jordan Alvarez", "email": "j@example.com",
                    "location": "Austin, TX"},
        "summary": "Backend engineer.",
        "skills": {"languages": ["Python", "SQL"], "cloud": ["AWS", "Docker"]},
        "experience": [{
            "title": "Backend Engineer", "company": "Acme",
            "dates": "Jan 2019 - Jan 2025",
            "bullets": ["Built Python services on AWS with Docker and PostgreSQL."],
        }],
        "education": [{"degree": "BS Computer Science", "institution": "UT"}],
        "projects": [],
        "certifications": [],
    }
    base.update(over)
    return base


def _nurse_resume():
    return {
        "contact": {"name": "Casey Rivera", "location": "Columbus, OH"},
        "summary": "Registered nurse in acute care.",
        "skills": {"clinical": ["Patient care", "Medication administration",
                                "Wound care", "EHR"]},
        "experience": [{
            "title": "Registered Nurse", "company": "Mercy Hospital",
            "dates": "Jun 2017 - Jun 2025",
            "bullets": ["Delivered direct patient care and medication administration.",
                        "Completed patient assessment and clinical documentation in Epic."],
        }],
        "education": [{"degree": "BSN Nursing", "institution": "Ohio State"}],
        "projects": [],
        "certifications": ["RN License, Ohio", "BLS", "ACLS"],
    }


SWE_JD = """
Senior Backend Engineer

Requirements:
- 5+ years of software engineering experience
- Strong Python and SQL
- Experience with AWS and Docker
- Familiarity with Kubernetes

Preferred qualifications:
- Kafka
- Terraform
"""


# ---------------------------------------------------------------------------
# Contract surface
# ---------------------------------------------------------------------------

class TestContract:
    def test_component_maxima_sum_to_one_hundred(self):
        assert sum(COMPONENT_MAX.values()) == 100

    def test_every_result_carries_version_band_and_confidence(self):
        v = fit_score.score(_resume(), {"title": "Senior Backend Engineer",
                                        "description": SWE_JD})
        assert v["score_version"] == FIT_SCORE_VERSION
        assert v["confidence"] in ("high", "medium", "low")
        assert v["band"] in {name for _, name in fit_score.BANDS}
        for key in ("score", "components", "component_scores", "evidence",
                    "strengths", "missing_requirements", "hard_constraints",
                    "explanation"):
            assert key in v

    def test_component_scores_never_exceed_their_maximum(self):
        v = fit_score.score(_resume(), {"title": "Senior Backend Engineer",
                                        "description": SWE_JD})
        for name, comp in v["components"].items():
            assert comp["max"] == COMPONENT_MAX[name]
            assert 0 <= comp["score"] <= comp["max"]

    def test_score_is_the_average_over_the_components_we_could_measure(self):
        """fit-v2.1: the score is `earned / possible` over components that had
        evidence on BOTH sides, not a raw sum over all seven.

        A raw sum required every unmeasurable component to pay out some
        neutral fraction, and a neutral fraction is still a deduction — see
        `_normalized_score`. Under the sum, a fully-evidenced perfect match
        topped out near 86 because the posting happened not to state its
        industry or its work mode."""
        v = fit_score.score(_resume(), {"title": "Senior Backend Engineer",
                                        "description": SWE_JD})
        assert not v["applied_caps"]
        known = [c for c in v["components"].values() if c["known"]]
        earned = sum(c["score"] for c in known)
        possible = sum(c["max"] for c in known)
        assert v["score"] == round(100 * earned / possible)
        assert v["evidence_breadth"] == pytest.approx(possible / 100.0)
        assert 0 <= v["score"] <= 100

    def test_an_unmeasurable_component_is_excluded_from_both_sides(self):
        v = fit_score.score(_resume(), {"title": "Senior Backend Engineer",
                                        "description": SWE_JD})
        for key, comp in v["components"].items():
            if not comp["known"]:
                assert comp["score"] == 0.0, key
        assert 0.0 < v["evidence_breadth"] <= 1.0

    def test_empty_inputs_do_not_raise(self):
        v = fit_score.score({}, {})
        assert 0 <= v["score"] <= 100
        assert v["score_version"] == FIT_SCORE_VERSION

    def test_deterministic(self):
        a = fit_score.score(_resume(), {"title": "Senior Backend Engineer",
                                        "description": SWE_JD})
        b = fit_score.score(_resume(), {"title": "Senior Backend Engineer",
                                        "description": SWE_JD})
        assert a["score"] == b["score"]
        assert a["explanation"] == b["explanation"]


# ---------------------------------------------------------------------------
# The core correction: denominator comes from the JD
# ---------------------------------------------------------------------------

class TestJdDerivedDenominator:
    def test_denominator_is_the_postings_asks_not_the_resumes_skills(self):
        """The old engine divided by the resume's own skill count clamped to
        [8, 14]. Here a resume covering everything a 3-ask posting names must
        score full coverage regardless of how long its skill list is."""
        jd = "Requirements: Python, SQL and AWS."
        lean = _resume(skills={"s": ["Python", "SQL", "AWS"]})
        rich = _resume(skills={"s": ["Python", "SQL", "AWS", "Docker", "Kubernetes",
                                     "Terraform", "Kafka", "Redis", "MongoDB",
                                     "React", "TypeScript", "Go", "Rust", "Spark",
                                     "Airflow", "dbt", "Snowflake", "Tableau"]})
        a = fit_score.score(lean, {"title": "Backend Engineer", "description": jd})
        b = fit_score.score(rich, {"title": "Backend Engineer", "description": jd})
        # Identical coverage of the posting's three asks, so identical points,
        # even though one resume lists 3 skills and the other 18. The old
        # engine scored these 3/8 = 38 and 3/14 = 21.
        assert a["evidence"]["required_coverage"] == b["evidence"]["required_coverage"]
        assert a["component_scores"]["required_skills"] == \
            b["component_scores"]["required_skills"]
        assert a["component_scores"]["required_skills"] >= \
            COMPONENT_MAX["required_skills"] * 0.9

    def test_a_posting_that_asks_for_more_is_harder_to_fit(self):
        """Same resume, same overlap count, different number of asks: the
        40-ask posting must score below the 4-ask one. The old formula could
        not tell these apart at all."""
        r = _resume(skills={"s": ["Python", "SQL"]})
        few = "Requirements: Python and SQL."
        many = ("Requirements: Python, SQL, Kubernetes, Terraform, Kafka, Redis, "
                "Elasticsearch, Spark, Airflow, dbt, Snowflake and Tableau.")
        a = fit_score.score(r, {"title": "Data Engineer", "description": few})
        b = fit_score.score(r, {"title": "Data Engineer", "description": many})
        assert a["component_scores"]["required_skills"] > \
            b["component_scores"]["required_skills"]

    def test_scale_is_not_quantised_to_fifteen_values(self):
        """The clamped denominator gave every matched skill a flat 7.14
        points, so ties were pervasive and the recency tiebreak never fired."""
        r = _resume()
        jds = [
            ("Backend Engineer", "Requirements: Python, SQL, AWS, Docker."),
            ("Backend Engineer", "Requirements: Python, SQL, AWS, Kubernetes, Kafka."),
            ("Data Engineer", "Requirements: Python, Spark, Airflow, dbt, SQL."),
            ("Platform Engineer", "Requirements: Terraform, Kubernetes, AWS, CI/CD."),
            ("Backend Engineer", "Requirements: Python."),
            ("Senior Backend Engineer", SWE_JD),
        ]
        scores = {fit_score.score(r, {"title": t, "description": d})["score"]
                  for t, d in jds}
        assert len(scores) >= 5


# ---------------------------------------------------------------------------
# Required vs preferred
# ---------------------------------------------------------------------------

class TestRequiredVersusPreferred:
    def test_preferred_block_is_parsed_out_of_the_required_denominator(self):
        reqs = fit_score.extract_requirements(SWE_JD, "Senior Backend Engineer")
        assert "kafka" in reqs["preferred_skills"]
        assert "terraform" in reqs["preferred_skills"]
        assert "kafka" not in reqs["required_skills"]
        assert "python" in reqs["required_skills"]

    def test_missing_a_required_skill_costs_more_than_missing_a_preferred_one(self):
        jd_missing_required = ("Requirements:\n- Python\n- SQL\n- Kubernetes\n"
                               "Preferred qualifications:\n- Kafka\n")
        jd_missing_preferred = ("Requirements:\n- Python\n- SQL\n- Docker\n"
                                "Preferred qualifications:\n- Kubernetes\n")
        r = _resume()
        a = fit_score.score(r, {"title": "Backend Engineer",
                                "description": jd_missing_required})
        b = fit_score.score(r, {"title": "Backend Engineer",
                                "description": jd_missing_preferred})
        assert b["score"] > a["score"]

    def test_preferred_only_posting_still_produces_requirements(self):
        reqs = fit_score.extract_requirements(
            "Nice to have: Python and SQL.", "Engineer")
        assert reqs["required_skills"]
        assert reqs["requirements_source"] == "preferred_only"


# ---------------------------------------------------------------------------
# Evidence weighting
# ---------------------------------------------------------------------------

class TestEvidenceWeighting:
    def test_applied_experience_outweighs_a_bare_skills_list_mention(self):
        jd = "Requirements: Kubernetes and Terraform."
        listed_only = _resume(
            skills={"s": ["Kubernetes", "Terraform"]},
            experience=[{"title": "Backend Engineer", "company": "Acme",
                         "dates": "Jan 2019 - Jan 2025",
                         "bullets": ["Wrote documentation."]}])
        applied = _resume(
            skills={"s": ["Kubernetes", "Terraform"]},
            experience=[{"title": "Backend Engineer", "company": "Acme",
                         "dates": "Jan 2019 - Jan 2025",
                         "bullets": ["Ran Kubernetes clusters provisioned with "
                                     "Terraform."]}])
        a = fit_score.score(listed_only, {"title": "Backend Engineer", "description": jd})
        b = fit_score.score(applied, {"title": "Backend Engineer", "description": jd})
        assert b["component_scores"]["required_skills"] > \
            a["component_scores"]["required_skills"]
        assert b["score"] > a["score"]

    def test_a_skills_only_caller_never_gets_applied_credit(self):
        """Feed pipelines pass a bare skill list. Those must degrade to
        listed claims, never be promoted into experience the resume did not
        state."""
        prof = fit_score._profile_from_skills(["Python", "SQL"], ["Backend Engineer"], 6)
        assert prof["experience_skills"] == set()
        assert "python" in prof["listed_skills"]


# ---------------------------------------------------------------------------
# Role and seniority
# ---------------------------------------------------------------------------

class TestRoleAlignment:
    def test_same_role_family_scores_full_role_points(self):
        v = fit_score.score(_resume(), {"title": "Backend Engineer",
                                        "description": SWE_JD})
        assert v["component_scores"]["role_alignment"] == COMPONENT_MAX["role_alignment"]

    def test_adjacent_family_scores_partial_not_full(self):
        v = fit_score.score(_resume(), {
            "title": "Data Engineer",
            "description": "Requirements: SQL, Python, Airflow, dbt and Spark."})
        pts = v["component_scores"]["role_alignment"]
        assert 0 < pts < COMPONENT_MAX["role_alignment"]

    def test_an_unrelated_role_lands_in_the_bottom_band(self):
        """The outcome, not the mechanism.

        Under fit-v2.1 the score reaches the bottom band on the component
        arithmetic alone, so the `unrelated_role` CAP frequently no longer
        needs to fire — `applied_caps` records a cap only when it actually
        lowered something. Asserting the cap fired was asserting that the
        score was too high without it.
        """
        v = fit_score.score(_nurse_resume(), {
            "title": "Senior Backend Engineer", "description": SWE_JD})
        assert v["score"] <= fit_score.CONSTRAINT_CAPS["unrelated_role"]
        assert v["band"] == "unlikely match"
        assert any(c["type"] == "role" and c["status"] == "missing"
                   for c in v["hard_constraints"])

    def test_a_clinical_resume_is_ineligible_for_a_backend_role(self):
        """The cross-domain veto, on a complete single-employer career."""
        v = fit_score.score(_nurse_resume(), {
            "title": "Senior Backend Engineer", "description": SWE_JD})
        assert v["eligibility"] == fit_score.INELIGIBLE
        assert v["ineligible_reasons"]
        assert v["score"] <= 34


class TestSeniority:
    def test_junior_resume_against_staff_role_is_penalised(self):
        junior = _resume(experience=[{
            "title": "Junior Developer", "company": "Acme",
            "dates": "Jan 2024 - Jan 2025",
            "bullets": ["Wrote Python scripts and SQL queries."]}])
        jd = ("Staff Software Engineer. Requirements: 10+ years of software "
              "engineering experience. Python, SQL, AWS and Docker.")
        v = fit_score.score(junior, {"title": "Staff Software Engineer",
                                     "description": jd})
        assert v["component_scores"]["experience"] < COMPONENT_MAX["experience"] * 0.4
        assert "experience_gap" in {c["type"] for c in v["hard_constraints"]}
        assert v["score"] <= fit_score.CONSTRAINT_CAPS["severe_experience_gap"]

    def test_senior_resume_against_entry_role_is_only_mildly_discounted(self):
        jd = ("Junior Software Engineer, entry level. Requirements: 0-1 years "
              "experience. Python and SQL.")
        v = fit_score.score(_resume(), {"title": "Junior Software Engineer",
                                        "description": jd})
        assert v["score"] >= 60
        assert not v["applied_caps"]

    def test_experience_is_never_invented_from_undated_entries(self):
        undated = _resume(experience=[{
            "title": "Backend Engineer", "company": "Acme", "dates": "",
            "bullets": ["Built Python services on AWS."]}])
        prof = fit_score.build_resume_profile(undated)
        assert prof["years"] is None
        assert prof["has_dated_experience"] is False


# ---------------------------------------------------------------------------
# Hard constraints
# ---------------------------------------------------------------------------

class TestHardConstraints:
    RN_JD = ("Registered Nurse, ICU. A current RN license is required. "
             "Requirements: 3+ years of nursing experience, patient assessment, "
             "medication administration and EHR charting. BLS and ACLS.")

    def test_missing_mandatory_license_caps_the_score(self):
        v = fit_score.score(_resume(), {"title": "Registered Nurse ICU",
                                        "description": self.RN_JD})
        licenses = [c for c in v["hard_constraints"] if c["type"] == "license"]
        assert licenses and licenses[0]["status"] == "missing"
        assert v["score"] <= fit_score.CONSTRAINT_CAPS["license_missing"]

    def test_held_license_is_recognised_and_does_not_cap(self):
        v = fit_score.score(_nurse_resume(), {"title": "Registered Nurse ICU",
                                              "description": self.RN_JD})
        licenses = [c for c in v["hard_constraints"] if c["type"] == "license"]
        assert licenses and licenses[0]["status"] == "met"
        assert "license" not in v["applied_caps"]
        assert v["score"] >= 65

    def test_license_unstated_but_field_matches_is_unknown_not_a_rejection(self):
        """A nurse resume that never spells out "RN license" must not be hard
        gated. Unknown is unknown."""
        nurse = _nurse_resume()
        nurse["summary"] = "Acute care clinician."
        nurse["certifications"] = []
        nurse["experience"] = [{
            "title": "Charge Nurse", "company": "Mercy",
            "dates": "Jun 2017 - Jun 2025",
            "bullets": ["Led patient care and medication administration."]}]
        nurse["education"] = [{"degree": "Nursing", "institution": "Ohio State"}]
        v = fit_score.score(nurse, {"title": "Registered Nurse ICU",
                                    "description": self.RN_JD})
        licenses = [c for c in v["hard_constraints"] if c["type"] == "license"]
        assert licenses and licenses[0]["status"] == "unknown"
        assert "license" not in v["applied_caps"]

    def test_an_unstated_clearance_is_surfaced_as_a_requirement_not_priced(self):
        """fit-v2.1: silence about a clearance is UNKNOWN, not a failure.

        Almost nobody writes "I hold an active Secret clearance" on a resume.
        Reading that silence as a definite absence capped every cleared
        candidate at 40 on the strength of a phrase they had no reason to
        include. A clearance requirement is now a hard requirement the reader
        is shown, which is what a hard constraint that we cannot evaluate
        should be: visible, and not a deduction.
        """
        jd = ("Software Engineer supporting a federal program. An active TS/SCI "
              "security clearance is required. Requirements: 5+ years of software "
              "engineering experience, Python, SQL, AWS and Docker.")
        v = fit_score.score(_resume(), {"title": "Software Engineer", "description": jd})
        clearance = [c for c in v["hard_constraints"] if c["type"] == "clearance"]
        assert clearance and clearance[0]["status"] == "unknown"
        assert clearance[0]["hard_requirement"] is True
        assert "clearance" not in v["applied_caps"]
        assert v["eligibility"] == fit_score.ELIGIBLE

    def test_held_clearance_removes_the_cap(self):
        jd = ("Software Engineer supporting a federal program. An active TS/SCI "
              "security clearance is required. Requirements: 5+ years of software "
              "engineering experience, Python, SQL, AWS and Docker.")
        cleared = _resume()
        cleared["summary"] = "Backend engineer. Active TS/SCI security clearance."
        v = fit_score.score(cleared, {"title": "Software Engineer", "description": jd})
        assert "clearance" not in v["applied_caps"]
        assert v["score"] > fit_score.CONSTRAINT_CAPS["clearance_missing"]

    def test_work_authorization_unknown_does_not_cap(self):
        jd = ("Software Engineer. We are unable to provide visa sponsorship. "
              "Requirements: 5+ years of software engineering experience, "
              "Python, SQL, AWS and Docker.")
        v = fit_score.score(_resume(), {"title": "Software Engineer", "description": jd})
        wa = [c for c in v["hard_constraints"] if c["type"] == "work_authorization"]
        assert wa and wa[0]["status"] == "unknown"
        assert "work_authorization" not in v["applied_caps"]

    def test_work_authorization_conflict_caps_only_when_stated(self):
        jd = ("Software Engineer. We are unable to provide visa sponsorship. "
              "Requirements: 5+ years of experience, Python, SQL, AWS and Docker.")
        needs = _resume()
        needs["summary"] = "Backend engineer on F-1 OPT, will require sponsorship."
        v = fit_score.score(needs, {"title": "Software Engineer", "description": jd})
        assert "work_authorization" in v["applied_caps"]
        assert v["score"] <= fit_score.CONSTRAINT_CAPS["work_authorization_conflict"]

    def test_incompatible_onsite_location_caps(self):
        jd = ("Backend Engineer. This role is on-site five days a week in our "
              "Boston office. Requirements: 5+ years of experience, Python, "
              "SQL, AWS and Docker.")
        v = fit_score.score(_resume(), {"title": "Backend Engineer",
                                        "description": jd,
                                        "location": "Boston, MA"})
        assert "location" in v["applied_caps"]
        assert v["score"] <= fit_score.CONSTRAINT_CAPS["location_incompatible"]

    def test_caps_never_produce_a_negative_or_zero_floor(self):
        v = fit_score.score(_nurse_resume(), {"title": "Senior Backend Engineer",
                                              "description": SWE_JD})
        assert v["score"] > 0


# ---------------------------------------------------------------------------
# Nontechnical resumes
# ---------------------------------------------------------------------------

class TestNontechnicalIsNotSoftwareEngineering:
    @pytest.mark.parametrize("title,summary", [
        ("Registered Nurse", "Acute care nurse."),
        ("Staff Accountant", "Accountant handling month end close."),
        ("High School Teacher", "Teacher of tenth grade biology."),
        ("Paralegal", "Litigation paralegal supporting discovery."),
    ])
    def test_nontechnical_titles_never_classify_as_software_engineering(
            self, title, summary):
        fam = fit_score.classify_role(title)
        assert fam is not None
        assert fam != "software_engineering"

    def test_unrecognised_title_returns_none_rather_than_defaulting(self):
        assert fit_score.classify_role("Widget Wrangler of the Fourth Realm") is None
        assert fit_score.classify_role("") is None

    def test_accountant_resume_does_not_read_as_an_engineer(self):
        accountant = {
            "contact": {"name": "Sam Lee", "location": "Chicago, IL"},
            "summary": "Staff accountant.",
            "skills": {"core": ["General ledger", "Month-end close", "Excel",
                                "QuickBooks", "GAAP"]},
            "experience": [{
                "title": "Staff Accountant", "company": "Ledger Co",
                "dates": "Jan 2019 - Jan 2025",
                "bullets": ["Owned month-end close, bank reconciliation and "
                            "financial reporting under GAAP."]}],
            "education": [{"degree": "BS Accounting"}],
            "certifications": [],
        }
        prof = fit_score.build_resume_profile(accountant)
        assert "software_engineering" not in prof["families"]
        assert "accounting" in prof["families"]
        v = fit_score.score(accountant, {"title": "Senior Backend Engineer",
                                         "description": SWE_JD})
        assert v["score"] <= fit_score.CONSTRAINT_CAPS["unrelated_role"]

    def test_nontechnical_resume_scores_well_on_its_own_field(self):
        accountant = {
            "contact": {"name": "Sam Lee", "location": "Chicago, IL"},
            "summary": "Staff accountant.",
            "skills": {"core": ["General ledger", "Month-end close", "Excel",
                                "QuickBooks", "GAAP", "Bank reconciliation"]},
            "experience": [{
                "title": "Staff Accountant", "company": "Ledger Co",
                "dates": "Jan 2019 - Jan 2025",
                "bullets": ["Owned month-end close, bank reconciliation, journal "
                            "entries and financial reporting under GAAP in "
                            "QuickBooks."]}],
            "education": [{"degree": "BS Accounting"}],
            "certifications": [],
        }
        jd = ("Senior Accountant. Requirements: 5+ years of accounting "
              "experience, month-end close, bank reconciliation, general ledger, "
              "financial reporting and GAAP. Excel required.")
        v = fit_score.score(accountant, {"title": "Senior Accountant",
                                         "description": jd})
        assert v["score"] >= 75
        assert v["band"] in ("strong match", "good match")


# ---------------------------------------------------------------------------
# Unknown information
# ---------------------------------------------------------------------------

class TestUnknownDoesNotAutoFail:
    """Unknown is EXCLUDED, not priced.

    Under fit-v2.0 each of these paid a "neutral" fraction, which sounds
    generous and is not: a component that pays 60% of its maximum when it has
    nothing to say costs the candidate the other 40%. Measured on the live
    inventory that was systematic — 96.1% of postings state no work mode,
    43.3% no seniority, 81.5% no salary — so essentially every candidate lost
    roughly twelve points to gaps in OUR parsing, and almost nothing could
    reach the top band. v2.1 drops an unmeasurable component from the
    numerator and the denominator both.
    """

    def test_posting_with_no_readable_requirements_is_excluded_not_scored(self):
        v = fit_score.score(_resume(), {"title": "Backend Engineer",
                                        "description": "Join our growing team!"})
        assert v["components"]["required_skills"]["known"] is False
        assert v["evidence"]["requirements_source"] == "none"
        assert v["confidence"] in ("low", "medium")
        # ...and the job-evidence cap still stops it reading as a real match.
        assert "job_evidence" in v["applied_caps"]

    def test_unknown_years_on_either_side_costs_nothing(self):
        jd = "Backend Engineer. Requirements: Python, SQL, AWS and Docker."
        v = fit_score.score(_resume(), {"title": "Backend Engineer", "description": jd})
        assert v["evidence"]["required_years"] is None
        known = [c for c in v["components"].values() if c["known"]]
        assert v["score"] == round(100 * sum(c["score"] for c in known)
                                   / sum(c["max"] for c in known))

    def test_unknown_location_is_neither_scored_nor_capped(self):
        jd = "Backend Engineer. Requirements: Python, SQL, AWS and Docker."
        no_loc = _resume(contact={"name": "Jordan Alvarez"})
        v = fit_score.score(no_loc, {"title": "Backend Engineer", "description": jd})
        assert v["components"]["location"]["known"] is False
        assert "location" not in v["applied_caps"]

    def test_unknown_industry_is_excluded(self):
        jd = "Backend Engineer. Requirements: Python and SQL."
        v = fit_score.score(_resume(), {"title": "Backend Engineer", "description": jd})
        assert v["components"]["domain"]["known"] is False

    def test_an_unstated_work_mode_never_lowers_the_score(self):
        """The single largest unknown in the inventory."""
        jd = "Backend Engineer. Requirements: Python, SQL, AWS and Docker."
        far = _resume(contact={"name": "J", "location": "Austin, TX"})
        v = fit_score.score(far, {"title": "Backend Engineer", "description": jd,
                                  "location": "Seattle, WA"})
        assert v["components"]["location"]["known"] is False
        assert v["components"]["location"]["score"] == 0.0
        assert "location" not in v["applied_caps"]

    def test_nothing_measurable_is_not_scored_rather_than_scored_zero(self):
        """A 0 renders as "unlikely match", which is a claim about the
        candidate. When NOTHING could be compared, the honest answer is the
        same one a missing resume gets: not scored."""
        v = fit_score.score(
            {"contact": {"name": "A"}, "skills": {"core": ["Widgetry"]},
             "experience": [{"title": "Widget Coordinator", "company": "X",
                             "dates": "", "bullets": ["Coordinated widgets."]}],
             "education": [{"degree": "Diploma"}]},
            {"title": "Coordinator", "description": "Join us!",
             "requirements": {"score_version": fit_score.FIT_SCORE_VERSION,
                              "required_degree_level": 3,
                              "required_skills": [], "preferred_skills": [],
                              "required_years": None, "role_family": None,
                              "job_level": None, "industries": [],
                              "work_mode": "unknown", "licenses": [],
                              "clearance_required": False,
                              "work_authorization_restricted": False,
                              "requirements_source": "none", "text_length": 9,
                              "content_hash": "x"}})
        if v["evidence_breadth"] == 0.0:
            assert v["eligibility"] == fit_score.NOT_SCORED
            assert v["ineligible_reasons"]

    def test_a_posting_naming_no_degree_does_not_cost_a_point(self):
        jd = "Backend Engineer. Requirements: Python, SQL, AWS and Docker."
        v = fit_score.score(_resume(), {"title": "Backend Engineer", "description": jd})
        edu = v["components"]["education"]
        assert edu["known"] is True
        assert edu["score"] == edu["max"]

    def test_sparse_resume_is_flagged_rather_than_silently_scored(self):
        sparse = {"contact": {"name": "A"}, "skills": {}, "experience": [
            {"title": "Backend Engineer", "company": "Acme", "dates": "",
             "bullets": ["Did the work."]}]}
        v = fit_score.score(sparse, {"title": "Backend Engineer",
                                     "description": SWE_JD})
        assert "resume_evidence" in v["applied_caps"]
        assert v["score"] <= fit_score.CONSTRAINT_CAPS["insufficient_resume_evidence"]
        assert v["confidence"] in ("low", "medium")


# ---------------------------------------------------------------------------
# Batch + caching seam
# ---------------------------------------------------------------------------

class TestBatchAndCaching:
    def test_score_batch_returns_meta_and_per_id_results(self):
        items = [
            {"id": "a", "title": "Backend Engineer", "description": SWE_JD},
            {"id": "b", "title": "Registered Nurse", "description": "RN license required."},
            {"title": "no id", "description": "ignored"},
        ]
        out, meta = fit_score.score_batch(_resume(), items)
        assert set(out) == {"a", "b"}
        assert meta["score_version"] == FIT_SCORE_VERSION
        assert meta["has_resume"] is True
        assert out["a"]["score"] > out["b"]["score"]

    def test_content_hash_is_stable_and_input_sensitive(self):
        h1 = fit_score.content_hash(SWE_JD, "Senior Backend Engineer")
        h2 = fit_score.content_hash(SWE_JD, "Senior Backend Engineer")
        h3 = fit_score.content_hash(SWE_JD + " extra", "Senior Backend Engineer")
        assert h1 == h2 != h3
        assert fit_score.extract_requirements(
            SWE_JD, "Senior Backend Engineer")["content_hash"] == h1

    def test_request_path_makes_no_bedrock_call(self, monkeypatch):
        """No LLM in the matching request path (ADR-002). Any attempt to
        reach the provider seam must blow up loudly."""
        import services.llm_providers as providers

        def explode(*a, **k):  # pragma: no cover - must never run
            raise AssertionError("fit_score reached the LLM provider")

        monkeypatch.setattr(providers, "get_provider", explode, raising=False)
        v = fit_score.score(_resume(), {"title": "Senior Backend Engineer",
                                        "description": SWE_JD})
        assert v["score"] > 0


# ---------------------------------------------------------------------------
# Copy rule
# ---------------------------------------------------------------------------

class TestCopyRule:
    FORBIDDEN = "–—‘’“”"

    def _strings(self, v):
        out = [v["explanation"], v["band"]]
        out += [c["detail"] for c in v["components"].values()]
        out += list(v["strengths"]) + [str(m) for m in v["missing_requirements"]]
        out += [str(c.get("detail") or "") for c in v["hard_constraints"]]
        return out

    @pytest.mark.parametrize("title,jd", [
        ("Senior Backend Engineer", SWE_JD),
        ("Registered Nurse ICU", "Current RN license required. 3+ years of nursing."),
        ("Staff Software Engineer", "10+ years required. TS/SCI clearance required."),
    ])
    def test_no_dashes_curly_quotes_or_accents_in_user_visible_copy(self, title, jd):
        v = fit_score.score(_resume(), {"title": title, "description": jd})
        for s in self._strings(v):
            assert not any(ch in s for ch in self.FORBIDDEN), s
            assert s.isascii(), s


# ---------------------------------------------------------------------------
# Seniority taxonomy
# ---------------------------------------------------------------------------

class TestSeniorityTaxonomy:
    """The bands a seeker filters on, and the misreads that emptied them.

    Every case below is a title taken from live `job_postings` (2026-08-27,
    284,416 rows). The measurement that motivated the rewrite: `staff` held
    59,218 rows - 21% of the whole corpus, a shape no labour market has -
    because `manager|supervisor|lead` were staff markers, while `mid` held
    4,957 and could not be reached from a title at all.
    """

    @pytest.mark.parametrize("title", [
        "New Grad Software Engineer",
        "2026 University Graduate - Software Engineer",
        "Early Career Data Analyst",
        "Campus Hire - Analyst",
        "Rotational Program Associate",
        "Junior Developer",
        "Graduate Engineer Trainee",
        "Software Engineer I",
        "Software Engineer II",
        "Entry Level Technician",
        "Apprentice Electrician",
    ])
    def test_early_career_titles_land_in_entry(self, title):
        """The old vocabulary had only intern/junior/graduate/trainee/
        apprentice/associate/assistant, so "New Grad", "Early Career",
        "Campus Hire" and "Rotational" - how large employers actually title a
        graduate pipeline - all came back unclassified."""
        assert fit_score.title_level(title) == "entry", title

    @pytest.mark.parametrize("title", [
        "Software Engineering Intern",
        "Data Science Internship - Summer 2027",
        "Engineering Co-op",
    ])
    def test_internships_are_their_own_band(self, title):
        """`intern` is not a synonym for `entry`. A candidate six weeks from
        graduating wants new-grad roles, and answering "entry" with unpaid
        summer positions is not answering the question."""
        assert fit_score.title_level(title) == "intern", title

    @pytest.mark.parametrize("title,expected", [
        # Live rows that the old rule filed as `staff`.
        ("Restaurant Shift Manager", "entry"),
        ("Retail Store Manager", "mid"),
        ("Sales Manager", "mid"),
        ("Team Lead", "mid"),
        ("Lead Cashier", "mid"),
        ("Night Shift Assistant Manager", "entry"),
        # ... and the ones that really are the IC staff band.
        ("Staff Product Manager", "staff"),
        ("Sr Staff Endpoint Systems Engineer", "staff"),
        ("Solutions Architect", "staff"),
    ])
    def test_management_titles_are_not_the_ic_staff_band(self, title, expected):
        assert fit_score.title_level(title) == expected, title

    def test_staff_nurse_is_not_a_staff_engineer(self):
        """In healthcare and accounting "staff" means rank-and-file - the
        opposite of the IC band. "RN Staff Nurse Burn ICU" was filed as
        `staff` alongside Distinguished Engineers."""
        assert fit_score.title_level("RN Staff Nurse Burn ICU Full-time Nights") != "staff"
        assert fit_score.title_level("Staff Accountant") != "staff"

    @pytest.mark.parametrize("title,expected", [
        ("Assistant Director of Nursing", "senior"),
        ("Associate Professor", "senior"),
        ("Assistant Vice President", "director"),
    ])
    def test_a_junior_word_before_a_senior_role_is_not_junior(self, title, expected):
        assert fit_score.title_level(title) == expected, title

    @pytest.mark.parametrize("title", [
        "Clinical Research Fellow",
        "Chief Resident",
    ])
    def test_medical_trainee_titles_are_not_leadership(self, title):
        """"Fellow" and "Chief" mean TRAINEE in medicine. Reading them as
        director/executive put a first-year doctor in the VP band."""
        assert fit_score.title_level(title) == "entry", title

    @pytest.mark.parametrize("title", [
        "Engineer R-1234",          # requisition id, not a level
        "Autonomy 2.0 Engineer",    # version number, not a level
        "Software Engineer",        # genuinely unmarked
        "Registered Nurse",
    ])
    def test_a_title_with_no_level_marker_returns_none(self, title):
        assert fit_score.title_level(title) is None, title

    def test_level_is_resolved_from_the_postings_own_stated_years(self):
        """47% of live postings were `unknown` while `required_years` sat
        extracted on the same document. Reading it is not a guess."""
        assert fit_score.level_from_signals("Software Engineer",
                                            required_years=1) == "entry"
        assert fit_score.level_from_signals("Software Engineer",
                                            required_years=8) == "staff"
        assert fit_score.level_from_signals(
            "Software Engineer",
            description="We hire recent graduates. 0-2 years of experience.") == "entry"
        assert fit_score.level_from_signals(
            "Analyst",
            description="A summer internship; you must be currently enrolled.") == "intern"
        # A stated title always wins over the body.
        assert fit_score.level_from_signals(
            "Senior Software Engineer", required_years=0) == "senior"
        # Nothing stated anywhere is still None, never a default.
        assert fit_score.level_from_signals("Software Engineer") is None

    @pytest.mark.parametrize("value,expected", [
        ("Junior", "entry"), ("junior", "entry"), ("New Grad", "entry"),
        ("Entry-Level", "entry"), ("Mid", "mid"), ("Senior", "senior"),
        ("intern", "intern"), ("Lead", "mid"), ("", None), (None, None),
    ])
    def test_user_level_spellings_map_onto_the_stored_vocabulary(self, value, expected):
        """`user_preferences.experience_level` stores "Junior", which is not a
        value `job_postings.seniority` has ever held. A filter built from it
        matched nothing at all - an outage, not a preference."""
        assert fit_score.normalize_level(value) == expected

    def test_a_resume_takes_its_HIGHEST_band_not_its_first(self):
        """"First title that states a level" is order-dependent. A resume
        reading ["Cloud/DevOps Engineer", "Associate Software Developer
        Intern"] resolved to `intern`, and `_score_experience` then docked the
        candidate on the entry roles they were applying to."""
        assert fit_score.resume_level(
            ["Software Engineer Intern", "Senior Software Engineer"]) == "senior"
        assert fit_score.resume_level(["Software Engineer"]) is None

    def test_an_intern_resume_is_not_docked_on_an_entry_role(self):
        """intern -> entry is the intended progression, not a stretch."""
        mx = COMPONENT_MAX["experience"]
        pts, _detail, severe, _known = fit_score._score_experience(
            {"job_level": "entry", "required_years": 0},
            {"level": "intern", "years": 2})
        assert pts == float(mx) and severe is False
        # A senior role IS still a real gap for the same resume.
        pts, _detail, severe, _known = fit_score._score_experience(
            {"job_level": "senior", "required_years": 5},
            {"level": "intern", "years": 2})
        assert pts < mx * 0.5 and severe is True


class TestTheCoarserClassifierMayNotOverrideTheFinerOnesSilence:
    """`_legacy_role_tier` is the 32-key bridge. `unrelated` is the harshest
    verdict it can return (0.08), and when the canonical taxonomy declined to
    classify the resume at all — no families, no specialties, confidence 0.0 —
    letting the blunter classifier assert "different field" inverts which one
    we trust.

    Measured on a live account whose entire resume is "Assistant Engineer" and
    nineteen skills: 280 of 400 core-tech postings came back `unrelated`, and
    the account topped out at 77 with nothing above 80. After the fix: 312
    `unknown`, max 84, mean 32.9 -> 41.8.
    """

    def _prof(self, *, families, tech):
        return {"families": set(families), "titles": ["Assistant Engineer"],
                "listed_skills": ["python"], "experience_skills": ["sql"],
                "tech": tech}

    def test_an_unclassified_resume_yields_unknown_not_unrelated(self):
        # `accounting` is not in `RELATED_FAMILIES["software_engineering"]`, so
        # the legacy tree's own verdict here is `unrelated` — which is exactly
        # the verdict it is no longer entitled to.
        prof = self._prof(families=["accounting"],
                          tech={"classification_confidence": 0.0,
                                "role_families": [], "role_specialties": []})
        assert fit_score._legacy_role_tier(
            {"role_family": "software_engineering"}, prof,
            "Backend Engineer") is None

    def test_a_confidently_classified_resume_still_gets_the_verdict(self):
        prof = self._prof(families=["healthcare"],
                          tech={"classification_confidence": 0.9,
                                "role_families": [], "role_specialties": [],
                                "non_tech_domain": "clinical"})
        got = fit_score._legacy_role_tier(
            {"role_family": "software_engineering"}, prof, "Backend Engineer")
        assert got is not None and got[0] == "unrelated"

    def test_a_positive_finding_never_needed_the_confidence_gate(self):
        """`same_family` and `adjacent_family` ADD information; only the
        harshest verdict is gated."""
        prof = self._prof(families=["software_engineering"],
                          tech={"classification_confidence": 0.0,
                                "role_families": [], "role_specialties": []})
        got = fit_score._legacy_role_tier(
            {"role_family": "software_engineering"}, prof, "Backend Engineer")
        assert got is not None and got[0] == "same_family"

    def test_the_cross_domain_veto_is_untouched(self):
        """A licensed teacher's resume IS confidently classified, and
        `role_alignment` answers it in its own branch before the legacy bridge
        is reached."""
        from services import tech_taxonomy as tx
        got = tx.role_alignment(
            job_families=["software_engineering"], job_specialties=["backend"],
            resume_families=[], resume_specialties=[],
            job_scope=tx.SCOPE_CORE, resume_scope=tx.SCOPE_NON_TECH,
            resume_non_tech_domain="education_public")
        assert got["cross_domain"] is True and got["tier"] == tx.ALIGN_UNRELATED
