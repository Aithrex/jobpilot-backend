"""The ATS score is strict, explainable and hard to game.

Each persona is a resume a recruiter would grade the same way every time. The
assertions pin BANDS, not exact numbers, so small weight changes stay possible
while the ordering a person cares about cannot move: proven beats listed,
listed beats missing, and nothing reaches 90 without results in real work.
Pure Python: no database, no model, no network."""
import copy
from datetime import date

import pytest

from services import ats_score

TODAY = date(2026, 11, 16)

JD = {
    "job_title": "Cloud Engineer",
    "company": "Contoso",
    "required_skills": ["AWS", "Terraform", "Kubernetes", "Python", "CI/CD", "Docker"],
    "preferred_skills": ["Go", "Prometheus"],
    "responsibilities": [
        "Design and operate cloud infrastructure on AWS with Terraform",
        "Run Kubernetes clusters and container workloads in production",
        "Build CI/CD pipelines that deploy services safely",
        "Automate operations with Python tooling",
    ],
    "qualifications": ["Bachelor's degree in Computer Science or related field",
                       "3+ years of experience in cloud or infrastructure engineering"],
    "experience_years": "3+ years",
    "keywords": ["AWS", "Terraform", "Kubernetes"],
}

CONTACT = {"name": "Priya Sharma", "email": "priya@example.test", "phone": "+1 555 0100",
           "location": "Austin, TX"}

STRONG = {
    "contact": CONTACT,
    "summary": ("Cloud Engineer with 4 years running AWS infrastructure with Terraform and Kubernetes, "
                "building CI/CD pipelines and Python automation for production services."),
    "skills": {"Cloud": ["AWS", "Terraform", "Kubernetes", "Docker"], "Languages": ["Python", "Go"],
               "Tooling": ["GitHub Actions", "Prometheus"]},
    "experience": [
        {"title": "Cloud Engineer", "company": "Fabrikam", "dates": "Jan 2024 to Present", "bullets": [
            "Provisioned AWS infrastructure for 40 services with Terraform, cutting setup time by 60%",
            "Ran 6 Kubernetes clusters serving 2M requests a day at 99.95% uptime",
            "Built CI/CD pipelines in GitHub Actions that deploy 30 times a day with automatic rollback",
            "Containerized 25 services with Docker, shrinking images by 70%",
            "Wrote Python tooling that automated 15 on-call runbooks and saved 10 hours a week",
            "Added Prometheus alerts for 120 metrics and halved paging noise",
        ]},
        {"title": "DevOps Engineer", "company": "Northwind", "dates": "Jun 2022 to Dec 2023", "bullets": [
            "Migrated 12 services from VMs to Docker and Kubernetes with zero downtime",
            "Wrote Terraform modules reused by 8 teams for AWS networking",
            "Built Python scripts that cut monthly AWS spend by $18,000",
            "Introduced CI/CD checks that dropped failed deploys by 45%",
        ]},
    ],
    "education": [{"degree": "B.S. in Computer Science", "institution": "UT Austin", "dates": "2018 to 2022"}],
    "projects": [],
    "certifications": ["AWS Certified Solutions Architect"],
}


def _score(resume, jd=JD, **kw):
    return ats_score.score(resume, jd, today=TODAY, **kw)


def test_a_resume_that_proves_every_must_have_with_results_reaches_the_top():
    result = _score(STRONG)
    assert result["score"] >= 90, result
    assert result["label"] == "Excellent"
    assert result["skills"]["missing"] == [] and result["skills"]["listed_only"] == []
    assert sum(c["max"] for c in result["components"]) == 100


def test_a_typical_good_fit_lands_in_the_seventies_or_low_eighties():
    typical = copy.deepcopy(STRONG)
    # Results on fewer bullets, Kubernetes and CI/CD only in the skills list.
    typical["experience"][0]["bullets"] = [
        "Managed AWS infrastructure with Terraform for several product teams",
        "Containerized services with Docker and supported production releases",
        "Wrote Python tooling for on-call runbooks",
    ]
    typical["experience"][1]["bullets"] = [
        "Wrote Terraform modules for AWS networking",
        "Built Python scripts that cut monthly AWS spend by $18,000",
    ]
    typical["skills"]["Cloud"] += ["CI/CD"]
    result = _score(typical)
    assert 65 <= result["score"] <= 84, result
    assert set(result["skills"]["listed_only"]) == {"Kubernetes", "CI/CD"}
    assert any("Kubernetes" in f for f in result["fixes"])


def test_listing_every_skill_without_using_it_cannot_score_well():
    stuffed = copy.deepcopy(STRONG)
    for job in stuffed["experience"]:
        job["bullets"] = ["Worked on backend services for internal customers",
                          "Helped the team ship features on time"]
    stuffed["summary"] = "Engineer."
    result = _score(stuffed)
    assert result["score"] <= 65, result
    assert any(c["key"] == "must_have" for c in result["caps"])
    assert any(d["key"] == "listed_only" for d in result["deductions"])


def test_missing_most_years_caps_the_score():
    junior = copy.deepcopy(STRONG)
    junior["experience"] = [dict(junior["experience"][0], dates="Jun 2026 to Present")]
    result = _score(junior, dict(JD, experience_years="5+ years"))
    assert result["score"] <= 55, result
    assert result["held_back_by"] and "5+ years" in result["held_back_by"]


def test_a_required_degree_that_is_missing_caps_the_score():
    no_degree = copy.deepcopy(STRONG)
    no_degree["education"] = [{"degree": "High School Diploma", "institution": "Austin High"}]
    jd = dict(JD, qualifications=["Bachelor's degree in Computer Science", "3+ years of experience"])
    assert _score(no_degree, jd)["score"] <= 60


def test_or_equivalent_experience_lifts_the_degree_cap_for_enough_years():
    no_degree = copy.deepcopy(STRONG)
    no_degree["education"] = []
    jd = dict(JD, experience_years="1+ years",
              qualifications=["Bachelor's degree or equivalent practical experience"])
    result = _score(no_degree, jd)
    assert not any(c["key"] == "degree" for c in result["caps"]), result


def test_ms_sql_server_is_not_a_masters_degree():
    jd = dict(JD, qualifications=["Experience with MS SQL Server", "3+ years of experience"])
    assert ats_score.required_degree(jd)[0] is None


def test_copying_a_sentence_from_the_posting_costs_points():
    copied = copy.deepcopy(STRONG)
    copied["experience"][0]["bullets"].append(
        "Run Kubernetes clusters and container workloads in production for the platform")
    result = _score(copied)
    assert any(d["key"] == "copied" for d in result["deductions"])
    assert result["score"] < _score(STRONG)["score"]


def test_repeating_a_skill_over_and_over_costs_points():
    spammy = copy.deepcopy(STRONG)
    spammy["summary"] += " " + " ".join(["Terraform expert."] * 8)
    assert any(d["key"] == "repeated" for d in _score(spammy)["deductions"])


def test_no_way_to_contact_you_is_a_hard_cap():
    nameless = copy.deepcopy(STRONG)
    nameless["contact"] = {"name": "Priya Sharma"}
    assert _score(nameless)["score"] <= 50


def test_a_pdf_that_does_not_read_back_is_capped():
    assert _score(STRONG, render_error="boom")["score"] <= 50
    assert _score(STRONG, pdf_check={"pdf_parseable_score": 62})["score"] <= 62


def test_evidence_tiers_order_the_credit():
    jd = dict(JD, required_skills=["Kubernetes"], preferred_skills=[])
    proven = _score(STRONG, jd)["components"][0]["points"]
    listed = copy.deepcopy(STRONG)
    for job in listed["experience"]:
        job["bullets"] = [b for b in job["bullets"] if "Kubernetes" not in b]
    listed_points = _score(listed, jd)["components"][0]["points"]
    missing = copy.deepcopy(listed)
    missing["skills"]["Cloud"].remove("Kubernetes")
    missing["summary"] = missing["summary"].replace("Kubernetes", "containers")
    missing_points = _score(missing, jd)["components"][0]["points"]
    assert proven > listed_points > missing_points == 0


def test_the_score_is_the_same_every_time():
    assert _score(STRONG) == _score(STRONG)


@pytest.mark.parametrize("empty", [{}, None, {"contact": CONTACT}])
def test_an_empty_resume_scores_low_and_does_not_crash(empty):
    result = _score(empty)
    assert result["score"] <= 50 and result["label"] in ("Weak", "Needs work")


def test_fixes_never_ask_to_claim_something_new():
    thin = copy.deepcopy(STRONG)
    thin["skills"] = {"Cloud": ["AWS"]}
    for job in thin["experience"]:
        job["bullets"] = [b for b in job["bullets"] if "Kubernetes" not in b and "Docker" not in b]
    fixes = _score(thin)["fixes"]
    missing_fix = next(f for f in fixes if f.startswith("Not on your resume"))
    assert "only what you have really used" in missing_fix


# --- found by the adversarial review (2026-11-16) ------------------------------

@pytest.mark.parametrize("dates,months", [
    ("Oct 2023 - Present", 38), ("October 2023 - Present", 38), ("Nov 2023 – Present", 37),
    ("Sept 2021 – Oct 2023", 26), ("Mar 2015 – Feb 2017", 24), ("Jun 2024 - Aug 2024", 3),
    ("06/2022 - 08/2023", 15), ("2022-06 to 2023-08", 15), ("2019-2021", 36), ("2019 - 2021", 36),
    ("Summer 2024", 3), ("Fall 2023 - Spring 2024", 9), ("Aug 2019 - Jun 2025", 71),
    ("Jan 2024 to Present", 35), ("May 2020", 1),
])
def test_every_common_date_format_is_read(dates, months):
    start, end = ats_score.role_span(dates, TODAY)
    assert start and end and ats_score._months(start, end) == months


def test_age_and_graduation_lines_are_not_years_of_experience():
    for line in ["Must be at least 18 years of age", "Graduated within the last 2 years",
                 "Valid license held for 2 years"]:
        assert ats_score.required_years({"qualifications": [line]}) is None, line
    assert ats_score.required_years(
        {"qualifications": ["Bachelor's and 2 years of experience, or 6 years of experience in lieu"]}) == 2


@pytest.mark.parametrize("degree,level", [
    ("B.Com in Accounting", 2), ("BSN", 2), ("BBA, Finance", 2), ("B.Eng. Mechanical", 2), ("LLB", 2),
    ("MSN, Nursing", 3), ("MBBS", 4), ("Pharm.D.", 4), ("Bachelor of Science in Biology", 2),
    ("High School Diploma", 0),
])
def test_degrees_are_placed(degree, level):
    assert ats_score._degree_level(ats_score._normalize_resume({"education": [{"degree": degree}]})) == level


def test_a_degree_we_cannot_place_is_never_capped():
    odd = copy.deepcopy(STRONG)
    odd["education"] = [{"degree": "Diplom-Ingenieur (Univ.)", "institution": "TU Munich"}]
    assert not any(c["key"] == "degree" for c in _score(odd)["caps"])


@pytest.mark.parametrize("line,level", [
    ("Proficiency in MS or Google office suites", None), ("Experience with MS/Azure", None),
    ("Bachelor of Science in Computer Science", 2), ("PhD in CS, or MS in CS with 3+ years", 3),
])
def test_posting_degree_words_need_degree_context(line, level):
    assert ats_score.required_degree({"qualifications": [line]})[0] == level


def test_a_bullet_listing_skills_is_not_proof():
    weak = copy.deepcopy(STRONG)
    for job in weak["experience"]:
        job["bullets"] = ["Worked on backend services for internal customers"]
    before = _score(weak)["score"]
    weak["experience"][0]["bullets"].append(
        "Used AWS, Terraform, Kubernetes, Python, CI/CD and Docker across 12 internal tools")
    after = _score(weak)
    assert after["score"] <= before + 3
    assert any(d["key"] == "skill_dump" for d in after["deductions"])


def test_version_numbers_are_not_results():
    assert not ats_score._METRIC.search(ats_score._strip_versions(
        "Ran Kubernetes 1.28 and Python 3.11 with GitLab 16 for releases"))
    assert ats_score._METRIC.search(ats_score._strip_versions("Served 16 teams across 3 regions"))


def test_text_the_pdf_never_prints_is_not_evidence():
    student = copy.deepcopy(STRONG)
    student["experience"] = []
    student["skills"] = {}
    student["summary"] = "Student."
    student["projects"] = [{"name": "Homelab", "tech": "AWS, Terraform, Kubernetes, Python, CI/CD, Docker",
                            "bullets": ["Set up a home server for my family"]}]
    tiers = {t: e["tier"] for t, e in ats_score._evidence(
        ats_score._normalize_resume(student), JD["required_skills"], set(), TODAY)["best"].items()}
    assert set(tiers.values()) == {"listed"}


def test_a_listed_certification_counts_as_shown():
    jd = dict(JD, required_skills=["BLS certification", "Patient care"], preferred_skills=[])
    nurse = copy.deepcopy(STRONG)
    nurse["certifications"] = ["BLS (American Heart Association)"]
    assert ats_score._evidence(ats_score._normalize_resume(nurse), jd["required_skills"], set(),
                               TODAY)["best"]["BLS certification"]["tier"] == "result"


def test_soft_words_and_the_company_are_not_must_haves():
    jd = dict(JD, required_skills=[], keywords=["AWS", "fast-paced", "Contoso", "collaboration", "Terraform"])
    result = _score(STRONG, jd)
    assert set(result["skills"]["missing"]) | set(result["skills"]["proven"]) <= {"AWS", "Terraform"}
    assert any(c["key"] == "thin_posting" for c in result["caps"])


def test_odd_shapes_do_not_crash():
    for resume in ([], "text", {"contact": "x", "experience": ["a", None], "skills": "AWS, Terraform"}):
        assert "score" in _score(resume)
    assert _score(STRONG, dict(JD, required_skills="AWS, Terraform, Kubernetes"))["skills"]["proven"] == [
        "AWS", "Terraform", "Kubernetes"]


def test_honest_repetition_across_jobs_is_fine():
    more = copy.deepcopy(STRONG)
    more["experience"][1]["bullets"].append("Moved 9 AWS accounts to AWS Organizations with 0 downtime")
    assert not any(d["key"] == "repeated" for d in _score(more)["deductions"])
