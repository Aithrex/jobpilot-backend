"""The shared JD dictionary. Sentences below are real wording from stored JDs,
with employer names removed; every one was read wrongly before this module."""
import json
from types import SimpleNamespace

import pytest

from services import fit_score, jd_profile
from services import job_inventory_service as inv
from services import tech_feed_service
from services.resume_service import ResumeService


def _norm(text):
    return fit_score._norm(text)


@pytest.mark.parametrize("sentence", [
    "Work visa sponsorship (such as TN, O-1, H-1B, etc.) is not available for this role now or in the future.",
    "Applicants must be legally authorized to work and must not require employer sponsorship.",
    "Work authorization that does not now or in the future require sponsorship of a visa.",
    "This is not a visa sponsored position.",
    "Authorized to work without employment-based immigration sponsorship, now or in the future.",
    "This position is not eligible for employment-based sponsorship.",
    "Do not apply for this role if you will need immigration sponsorship now or in the future.",
    "Required and no current or future work authorization sponsorship available.",
    "Must be authorized to work in the U.S. without needing sponsorship for a work visa.",
    "Remote roles are not eligible for U.S. visa sponsorship.",
    "Candidates must possess unrestricted work authorization and not require future sponsorship.",
    "The company is prohibited from offering sponsorship for any immigrant visas.",
    "We are not currently sponsoring employment-based visas (e.g., an H-1B).",
    "Immigration sponsorship (H1-B, TN, etc.) is not currently available for this position.",
])
def test_stated_refusals_read_as_not_offered(sentence):
    result = jd_profile.read_sponsorship(_norm(sentence))
    assert result["status"] == jd_profile.SPONSORSHIP_NOT_OFFERED
    assert result["evidence"]


@pytest.mark.parametrize("sentence", [
    "We sponsor visas to help exceptional talent join our team.",
    "We're happy to sponsor international candidates (e.g., STEM OPT, H1B).",
    "This position is eligible for employment visa sponsorship.",
    "Visa sponsorship available",
])
def test_stated_offers_read_as_offered(sentence):
    assert jd_profile.read_sponsorship(_norm(sentence))["status"] == jd_profile.SPONSORSHIP_OFFERED


@pytest.mark.parametrize("sentence", [
    "Work authorization sponsorship (H-1B, TN, J, etc.) is offered exclusively for principal-level roles.",
    "This position may not be eligible for visa sponsorship now or in the future.",
    "We will consider employment visa sponsorship on a case-by-case basis.",
    "Based on immigration requirements, not all roles are suitable for sponsorship.",
])
def test_qualified_statements_are_conditional(sentence):
    assert jd_profile.read_sponsorship(_norm(sentence))["status"] == jd_profile.SPONSORSHIP_CONDITIONAL


@pytest.mark.parametrize("text,status", [
    ("Visa Sponsorship?\nNo\nLocation: Austin", jd_profile.SPONSORSHIP_NOT_OFFERED),
    ("Sponsorship available: No", jd_profile.SPONSORSHIP_NOT_OFFERED),
    ("Is role eligible for immigration sponsorship?: Yes", jd_profile.SPONSORSHIP_OFFERED),
    # An unanswered label says nothing.
    ("Visa sponsorship:\nTravel requirements:\nNone", jd_profile.NOT_STATED),
    ("Visa sponsorship?\nGovernment clearance required?", jd_profile.NOT_STATED),
    # The form is asking the candidate, not stating a policy.
    ("Do you require visa sponsorship? No", jd_profile.NOT_STATED),
])
def test_printed_forms(text, status):
    assert jd_profile.read_sponsorship(_norm(text))["status"] == status


@pytest.mark.parametrize("sentence", [
    # Silence and a plain authorization line are not refusals: a wrong
    # "no sponsorship" hides a reachable job from a visa-holding candidate.
    "Must be authorized to work in the United States.",
    "Partner with the executive sponsor on the project roadmap.",
    "No project sponsorship is needed for this business sponsor.",
    "Without executive sponsorship the program will stall.",
    "Candidates must be prepared to indicate whether they require employment visa sponsorship.",
    "Python and AWS required.",
])
def test_unrelated_or_silent_text_is_not_stated(sentence):
    assert jd_profile.read_sponsorship(_norm(sentence))["status"] == jd_profile.NOT_STATED


def test_a_negated_positive_is_never_offered():
    assert jd_profile.read_sponsorship(
        _norm("Please note this role cannot support future H-1B sponsorship."))["status"] \
        == jd_profile.SPONSORSHIP_NOT_OFFERED


@pytest.mark.parametrize("sentence,requirement", [
    ("Must be a U.S. citizen.", jd_profile.AUTH_CITIZEN),
    ("U.S. citizenship is required.", jd_profile.AUTH_CITIZEN),
    ("Must be a U.S. citizen or permanent resident.", jd_profile.AUTH_CITIZEN_OR_PR),
    ("Must be authorized to work in the United States.", jd_profile.AUTH_AUTHORIZED),
    ("We participate in E-Verify to confirm work authorization.", jd_profile.NOT_STATED),
])
def test_authorization(sentence, requirement):
    assert jd_profile.read_authorization(_norm(sentence))["requirement"] == requirement


@pytest.mark.parametrize("text", [
    "You will work with cross-functional teams to build distributed systems.",
    "Run nightly batch jobs for our data platforms and programs.",
    "Must know MS Office and MS Teams.",
    "Office in Bethesda, MD and Jackson, MS.",
    "Partner with the scrum master on master data management.",
    "Our dedication has paid off: a six-time J.D. Power winner.",
    "Please read the JD carefully. Work with a high degree of autonomy.",
    "We offer tuition reimbursement toward a master's degree.",
    "Be a master of your craft.",
    "Connaissance du S.I.M.D.U.T;",
    "Masters job-related, technical and professional knowledge.",
    "Master's students: $28.00 CAD / hour.",
    "Completed or working towards a high school diploma.",
    "BSN or higher premium $1.00/hr.",
    "Currently enrolled in a university program pursuing a degree in engineering.",
    "No bachelor degree required.",
    "Must have completed at least two years toward a four-year undergraduate degree program.",
    "Our team includes engineers from Google and leading PhD programs.",
])
def test_no_degree_is_invented(text):
    assert jd_profile.read_education(_norm(text))["min_level"] == 0


@pytest.mark.parametrize("text,minimum,preferred", [
    ("Bachelor's degree in Computer Science required.", 2, 0),
    ("Required: Bachelor's degree.\nPreferred: Master's degree.", 2, 3),
    ("Bachelor's required, Master's preferred.", 2, 3),
    ("BS with 5 years of experience or MS with 3 years of experience.", 2, 0),
    ("BS, MS, or PhD in Computer Science.", 2, 0),
    ("Master's degree preferred.", 0, 3),
    ("MBA considered an asset.", 0, 3),
    ("Master's degree required.", 3, 0),
    ("High school diploma or GED required.", 1, 0),
    ("PhD in Physics required.", 4, 0),
    ("Juris Doctor (J.D.) degree required.", 4, 0),
    ("University degree (or equivalent), preferably in biological sciences.", 2, 0),
    ("Degree in life sciences, engineering, or a related discipline.", 2, 0),
    ("Bachelors degree + 7 years. In lieu of required degree, associates degree + 9 years.", 2, 0),
    ("Education required: less than high school education.", 0, 0),
    ("Bachelor's/university degree, Master's degree preferred", 2, 3),
    ("University degree in accounting (CPA or similar certification highly preferred).", 2, 0),
    ("High school diploma, GED or equivalent work experience, preferred", 0, 1),
    ("Degree or equivalent experience required.", 2, 0),
    ("Master's degree in physics required.", 3, 0),
    ("Bachelor's degree in physics (or high school diploma / GED plus 6 additional years).", 2, 0),
    ("A bachelor's degree or equivalent practical experience is valued for this role.", 0, 2),
    ("Degree in life sciences required.", 2, 0),
])
def test_degree_reading(text, minimum, preferred):
    result = jd_profile.read_education(_norm(text))
    assert result["min_level"] == minimum
    assert result["preferred_level"] == preferred


def test_a_bullet_saying_preferred_is_not_a_heading():
    text = "Qualifications\n- Prior closing experience strongly preferred\n- Bachelor's degree"
    assert jd_profile.read_education(_norm(text))["min_level"] == 2


@pytest.mark.parametrize("text,requirement", [
    ("This job requires a U.S. person.", jd_profile.AUTH_CITIZEN_OR_PR),
    ("Requires the ability to work lawfully in the U.S.", jd_profile.AUTH_AUTHORIZED),
    ("Work authorization:", jd_profile.NOT_STATED),
    ("Right to work poster (English/Spanish)", jd_profile.NOT_STATED),
    ("U.S. citizenship is required for all positions with a government clearance.",
     jd_profile.NOT_STATED),
])
def test_authorization_needs_a_statement_about_this_role(text, requirement):
    assert jd_profile.read_authorization(_norm(text))["requirement"] == requirement


def test_clearance_level_label_and_customs_work():
    assert jd_profile.clearance_level(_norm("Clearance type: Secret"), True) == "secret"
    reqs = fit_score.extract_requirements(
        "Ensure import/export control compliance with customs rules.", "Trade Analyst")
    assert reqs["clearance_required"] is False


def test_citizenship_boilerplate_only_binds_a_cleared_role():
    line = _norm("U.S. citizenship is required for all positions with a government clearance.")
    assert jd_profile.read_authorization(line)["requirement"] == jd_profile.NOT_STATED
    assert jd_profile.read_authorization(line, True)["requirement"] == jd_profile.AUTH_CITIZEN


@pytest.mark.parametrize("text,level", [
    ("Obtain and maintain a Secret U.S. security clearance.", "secret"),
    ("Active Top Secret clearance with SCI eligibility.", "ts_sci"),
])
def test_clearance_levels(text, level):
    assert jd_profile.clearance_level(_norm(text), True) == level


def test_itar_compliance_work_is_not_a_clearance_rule():
    reqs = fit_score.extract_requirements(
        "Maintains records for exports of ITAR hardware and technical data.", "Trade Analyst")
    assert reqs["clearance_required"] is False
    assert fit_score.extract_requirements(
        "Must be a U.S. person under ITAR.", "Engineer")["clearance_required"] is True


def test_advanced_degree_reads_as_a_masters():
    result = jd_profile.read_education(
        _norm("Bachelor's degree required. Advanced degree in business preferred."))
    assert (result["min_level"], result["preferred_level"]) == (2, 3)


def test_education_detail():
    result = jd_profile.read_education(
        _norm("Bachelor's degree in Computer Science or equivalent experience required."))
    assert result["min_degree"] == "bachelor"
    assert "computer science" in result["fields"]
    assert result["equivalent_experience_accepted"] is True


@pytest.mark.parametrize("text,expected", [
    ("High school diploma or equivalent required.", False),
    ("Bachelor's degree or equivalent work experience.", True),
    ("Equivalent combination of education and experience accepted with a bachelor's degree.", True),
])
def test_equivalent_experience_is_not_a_ged(text, expected):
    assert jd_profile.read_education(_norm(text))["equivalent_experience_accepted"] is expected


@pytest.mark.parametrize("text,years", [
    ("Must have at least two (2) years of recent experience as a surveyor.", 2),
    ("Minimum ten (10) years progressive experience in quality assurance.", 10),
    ("Minimum of five years coaching experience.", 5),
    ("One to four years of client management experience.", 1),
    ("Bachelors degree + 7 years work experience; associates degree + 9 years work experience", 7),
    ("Bachelor's degree and 5+ years of relevant experience (master's with 3 years).", 5),
    ("We are one of the top ten teams. Python required.", None),
])
def test_years_read_spelled_numbers_and_the_bachelors_path(text, years):
    assert fit_score._required_years(_norm(text)) == years


@pytest.mark.parametrize("text,minimum,preferred", [
    ("3-6 years of experience, preferred", None, 3),
    ("One year of general maintenance experience preferred.", None, 1),
    ("A degree is a plus and a minimum of 5 years successful experience in sales.", 5, None),
    ("Must be 18 years or older.", None, None),
    ("Degree and typically requires 10+ years of relevant experience.\n12+ years of experience in architecture.", 10, None),
    ("Minimum 2 years of controls experience.\nNo degree - must have 5+ years verifiable experience.", 2, None),
    ("5 years of management experience.\nAssociate degree plus seven (7) years of experience will meet the requirement.", 5, None),
    ("Bachelor's degree and four (4) years of progressively responsible work.", 4, None),
    ("1-3 years of inside sales experience, preferably in B2B distribution.", 1, None),
    ("+3 years of experience in dealer administration (ideal), with strong results.", 3, None),
    ("Typically 2-3 years of relevant experience and a degree in a related field desirable.", 2, None),
    ("Current nursing experience within the past 5 years.", None, None),
    ("Bachelor's degree, along with 5+ years as a clinical research associate.", 5, None),
])
def test_experience_keeps_required_and_preferred_apart(text, minimum, preferred):
    block = fit_score.extract_requirements(text, "Analyst")["jd"]["experience"]
    assert (block["min_years"], block["preferred_years"]) == (minimum, preferred)


def test_months_read_as_years():
    assert jd_profile.read_experience(
        _norm("Six (6) months prior sales experience."), None)["min_years"] == 0.5


def test_the_scorer_keeps_its_preferred_years_fallback():
    assert fit_score.extract_requirements(
        "3-6 years of experience, preferred", "Analyst")["required_years"] == 3


def test_the_scorer_no_longer_taxes_a_bachelors_for_the_word_teams():
    jd = "Work with teams on distributed systems. Requirements: 2+ years of Python, AWS, Docker."
    assert fit_score.extract_requirements(jd, "Software Engineer")["required_degree_level"] == 0


def test_resume_degree_reader_keeps_bare_abbreviations_and_gains_the_boundary():
    assert fit_score._degree_level("ms computer science") == 3
    assert fit_score._degree_level("bs computer science") == 2
    assert fit_score._degree_level("distributed systems") == 0


def test_alternatives_are_tight_lists_only():
    read = lambda text: jd_profile.read_skill_alternatives(_norm(text), fit_score.find_skills)
    assert read("Experience with AWS, GCP or Azure.") == [["aws", "gcp", "azure"]]
    assert read("Excel or recruiting experience.") == []


def test_experience_range():
    block = jd_profile.read_experience(_norm("We ask for 6-8 years of experience."), 6)
    assert (block["min_years"], block["max_years"]) == (6, 8)


def test_clearance_level_certifications_and_employment_type():
    assert jd_profile.clearance_level(_norm("Active TS/SCI required"), True) == "ts_sci"
    assert jd_profile.clearance_level("", False) == jd_profile.NOT_STATED
    assert jd_profile.read_certifications(_norm("CISSP or Security+ required")) \
        == ["CISSP", "CompTIA Security+"]
    assert jd_profile.read_employment_type(
        _norm("Full-time role. Part-time employees get benefits.")) == "full_time"


@pytest.mark.parametrize("text,mode", [
    ("Work environment: On-site, Arlington, VA", "onsite"),
    ("This role is remote, but distance is no barrier. Our hybrid teams collaborate.", "remote"),
    ("Remote\nRemote: employees in remote roles work offsite.\nFor onsite and hybrid roles...", "remote"),
    ("This role requires working in person in our Mexico City office 4 days per week.", "hybrid"),
    ("Software Engineer - Remote\nBuild things.", "remote"),
    ("We prioritize in-office presence.\n#LI-Hybrid", "hybrid"),
    ("Job type: Permanent, Full-time, Hybrid", "hybrid"),
    ("Work mode: Hybrid/Split", "hybrid"),
    ("Please note: this position is located on-site in Albuquerque, NM.", "onsite"),
    ("Fidelity is transitioning to a full-time onsite working model.", "unknown"),
    ("Assist customers in person and over the phone.", "unknown"),
    ("A one-of-a-kind shop/play hybrid experience.", "unknown"),
    ("Deploy across on-premise, hybrid, or cloud environments.", "unknown"),
    ("If this position is listed as hybrid, you work from a facility.", "unknown"),
    ("Support remote monitoring of devices.", "unknown"),
    ("Proficiency with Microsoft Office.", "unknown"),
])
def test_work_mode_needs_a_statement(text, mode):
    assert jd_profile.read_work_mode(_norm(text)) == mode


@pytest.mark.parametrize("text,kind", [
    ("Employment type: Full-time", "full_time"),
    ("This is a part-time position.", "part_time"),
    ("Hours per week: 45+ hours per week", "full_time"),
    ("Schedule: Sunday-Saturday, varied, PRN position", "part_time"),
    ("Medical Information Specialist (Contractor)", "contract"),
    ("Temporary contract through end of year.", "temporary"),
    ("Full-time and part-time employees are eligible for our benefit programs.", "not_stated"),
    ("Full-time hires are eligible to participate.", "not_stated"),
    ("Must be authorized to work on a full-time basis.", "not_stated"),
    ("API contract definition and versioning.", "not_stated"),
    ("This job description is not a contract of employment.", "not_stated"),
    ("Employees working at least 20 hours a week are eligible.", "not_stated"),
    ("Schedule: Full - Time", "full_time"),
    ("This role is a 1099 independent contractor position.", "contract"),
    ("The medical director will work a full-time schedule Monday to Friday.", "full_time"),
    ("This full-time position is eligible for a benefits package.", "full_time"),
    ("Seasonal colleagues work 10-20 hours per week.", "temporary"),
    ("Keep the seasonal areas of the store tidy.", "not_stated"),
    ("Intern (Fixed Term)", "internship"),
])
def test_employment_type_needs_a_statement(text, kind):
    assert jd_profile.read_employment_type(_norm(text)) == kind


def test_requirements_carry_the_block_and_the_flags_come_from_it():
    reqs = fit_score.extract_requirements(
        "Requirements: Python, AWS. Visa sponsorship is not available.", "Engineer")
    assert reqs["jd"]["v"] == jd_profile.JD_PROFILE_VERSION
    assert reqs["jd"]["sponsorship"]["status"] == jd_profile.SPONSORSHIP_NOT_OFFERED
    assert reqs["work_authorization_restricted"] is True
    assert fit_score._precomputed_requirements(reqs) is not None


def test_common_tools_are_technical_not_soft():
    profile = jd_profile.build("Requirements: Excel, CRM, teamwork and communication.", "Analyst")
    assert {"excel", "crm"} <= set(profile["skills"]["technical"])
    assert "teamwork" in profile["skills"]["soft"]


def test_additional_skills_enrich_the_dictionary_and_leave_the_score_alone():
    reqs = fit_score.extract_requirements(
        "Requirements: Databricks, ISO 13485 and change management.", "Engineer")
    assert {"databricks", "iso 13485", "change management"} <= set(reqs["jd"]["additional_skills"])
    assert "iso 13485" not in reqs["required_skills"]
    profile = jd_profile.build("Requirements: Databricks and Python.", "Engineer")
    assert "databricks" in profile["skills"]["technical"]


def test_noise_terms_are_not_technical():
    profile = jd_profile.build("Requirements: documentation, recruiting and Python.", "Engineer")
    assert "python" in profile["skills"]["technical"]
    assert "recruiting" not in profile["skills"]["technical"]


def test_build_returns_the_whole_dictionary():
    profile = jd_profile.build(
        "Requirements: 5+ years of experience with Python and AWS. "
        "Bachelor's degree required. Visa sponsorship is not available.",
        "Senior Software Engineer")
    assert set(profile) == {"version", "content_hash", "role", "experience", "education",
                            "skills", "work_eligibility", "compensation", "content",
                            "provenance"}
    assert profile["experience"]["min_years"] == 5
    assert profile["education"]["min_degree"] == "bachelor"
    assert {"python", "aws"} <= set(profile["skills"]["required"])
    assert profile["work_eligibility"]["sponsorship"]["status"] == "not_offered"
    assert profile["provenance"] == {**profile["provenance"], "deterministic": True, "llm": False}


def test_tailoring_and_filtering_read_the_same_facts():
    jd = "Requirements: Python. Master's degree required. Visa sponsorship is not available."
    analysis = {"job_title": "Engineer", "required_skills": ["LangGraph"],
                "responsibilities": ["Ship features"]}
    stub = SimpleNamespace(_analyze_jd=lambda text: analysis)
    result = ResumeService.extract_jd(stub, jd)
    profile = result["jd_profile"]
    reqs = fit_score.extract_requirements(jd, "Engineer")
    assert profile["work_eligibility"]["sponsorship"] == reqs["jd"]["sponsorship"]
    assert profile["education"]["min_level"] == reqs["required_degree_level"]
    assert "langgraph" in profile["skills"]["required"]
    assert profile["content"]["responsibilities"] == ["Ship features"]
    assert profile["provenance"]["llm"] is True
    assert result["required_skills"] == ["LangGraph"]


def test_prompts_get_job_facts_and_never_eligibility_or_quotes():
    jd = "Requirements: 5+ years of experience in Python. Bachelor's degree required. Experience with AWS, " \
         "GCP or Azure. Visa sponsorship is not available for this role."
    analysis = jd_profile.attach_to_analysis({"job_title": "Engineer"}, jd)
    view = jd_profile.prompt_view(analysis)
    assert "jd_profile" not in view
    assert view["job_facts"]["min_years"] == 5
    assert view["job_facts"]["min_degree"] == "bachelor"
    assert view["job_facts"]["skill_alternatives"] == [["aws", "gcp", "azure"]]
    assert "sponsor" not in json.dumps(view).lower()
    assert "jd_profile" in analysis
    assert jd_profile.prompt_view({"job_title": "x"}) == {"job_title": "x"}
    assert jd_profile.prompt_view(None) is None


def test_the_analysis_prompt_reads_past_the_old_limit(monkeypatch):
    from services import claude_client

    seen = {}

    def fake(prompt, **_):
        seen["prompt"] = prompt
        return {"job_title": "Engineer"}

    monkeypatch.setattr(claude_client, "claude_json", fake)
    monkeypatch.setattr("services.tailoring_policy.enabled", lambda: False)
    ResumeService._analyze_jd(SimpleNamespace(), "x " * 4000 + "LATEREQUIREMENT")
    assert "LATEREQUIREMENT" in seen["prompt"]


def test_analysis_passes_through_when_it_is_not_a_dict():
    assert jd_profile.attach_to_analysis(None, "text") is None


def test_stored_block_is_bounded_and_keeps_evidence_punctuation():
    reqs = fit_score.extract_requirements(
        "U.S. work visa sponsorship is not available for this role.", "Engineer")
    stored = inv._safe_jd_block(reqs["jd"])
    assert stored["sponsorship"]["status"] == "not_offered"
    assert "u.s." in stored["sponsorship"]["evidence"]
    assert set(stored["education"]) == {"min_level", "preferred_level", "fields",
                                        "equivalent_experience_accepted"}
    assert "evidence" not in stored["experience"]
    assert inv._safe_jd_block(None) == {}
    assert inv._safe_jd_block({"$where": "x"})["sponsorship"]["status"] == ""


@pytest.mark.parametrize("requirements,expected", [
    ({"work_authorization_restricted": True,
      "jd": {"sponsorship": {"status": "not_stated"}}}, "not_offered"),
    ({"jd": {"sponsorship": {"status": "offered"}}}, "offered"),
    ({"jd": {"sponsorship": {"status": "conditional"}}}, None),
    ({"jd": {"sponsorship": {"status": "not_stated"}}}, None),
])
def test_feed_reads_the_stored_block(requirements, expected):
    assert tech_feed_service._sponsorship_of({"requirements": requirements}) == expected


def test_feed_still_reads_a_profile_stored_before_the_block():
    row = {"requirements": {"required_skills": []},
           "description_text": "Visa sponsorship is available for this role."}
    assert tech_feed_service._sponsorship_of(row) == "offered"
