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

    def test_score_equals_capped_sum_of_components(self):
        v = fit_score.score(_resume(), {"title": "Senior Backend Engineer",
                                        "description": SWE_JD})
        assert not v["applied_caps"]
        assert v["score"] == round(sum(v["component_scores"].values()))
        assert 0 <= v["score"] <= 100

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

    def test_unrelated_role_is_capped(self):
        v = fit_score.score(_nurse_resume(), {
            "title": "Senior Backend Engineer", "description": SWE_JD})
        assert "role" in v["applied_caps"]
        assert v["score"] <= fit_score.CONSTRAINT_CAPS["unrelated_role"]
        assert v["band"] == "unlikely match"


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

    def test_missing_clearance_caps_the_score(self):
        jd = ("Software Engineer supporting a federal program. An active TS/SCI "
              "security clearance is required. Requirements: 5+ years of software "
              "engineering experience, Python, SQL, AWS and Docker.")
        v = fit_score.score(_resume(), {"title": "Software Engineer", "description": jd})
        assert "clearance" in v["applied_caps"]
        assert v["score"] <= fit_score.CONSTRAINT_CAPS["clearance_missing"]

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
    def test_posting_with_no_readable_requirements_is_neutral_not_zero(self):
        v = fit_score.score(_resume(), {"title": "Backend Engineer",
                                        "description": "Join our growing team!"})
        assert v["component_scores"]["required_skills"] == \
            COMPONENT_MAX["required_skills"] * 0.6
        assert v["evidence"]["requirements_source"] == "none"
        assert v["confidence"] in ("low", "medium")

    def test_unknown_years_on_either_side_is_neutral(self):
        jd = "Backend Engineer. Requirements: Python, SQL, AWS and Docker."
        v = fit_score.score(_resume(), {"title": "Backend Engineer", "description": jd})
        assert v["evidence"]["required_years"] is None
        assert v["component_scores"]["experience"] == \
            pytest.approx(COMPONENT_MAX["experience"] * 0.65)

    def test_unknown_location_is_neutral_not_a_penalty(self):
        jd = "Backend Engineer. Requirements: Python, SQL, AWS and Docker."
        no_loc = _resume(contact={"name": "Jordan Alvarez"})
        v = fit_score.score(no_loc, {"title": "Backend Engineer", "description": jd})
        assert v["component_scores"]["location"] == \
            pytest.approx(COMPONENT_MAX["location"] * 0.6)
        assert "location" not in v["applied_caps"]

    def test_unknown_industry_is_neutral(self):
        jd = "Backend Engineer. Requirements: Python and SQL."
        v = fit_score.score(_resume(), {"title": "Backend Engineer", "description": jd})
        assert v["component_scores"]["domain"] == \
            pytest.approx(COMPONENT_MAX["domain"] * 0.6)

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
