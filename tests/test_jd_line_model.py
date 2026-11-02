"""The line reader: what each line of a JD is for, and what tailoring gets from it."""
import pytest

from schemas.resume_schemas import JD_ANALYSIS_SCHEMA
from services import jd_line_model, jd_profile, jd_skill_model

POSTING = """About Northwind
Northwind is a global leader in logistics software, and our mission is to move goods simply.

Job Summary
We are seeking a Senior Data Engineer to join our platform team.

Key Responsibilities
Design and build data pipelines in Python and Spark.
Partner with analysts to define metrics.
Own the reliability of our nightly warehouse loads.

Required Qualifications
Bachelor's degree in Computer Science or a related field.
5+ years of experience with Python and SQL.
Must be authorized to work in the United States without visa sponsorship.

Preferred Qualifications
Experience with Kafka.
AWS Certified Data Engineer is a plus.

Benefits
Medical, dental and vision coverage from day one.
401(k) with a company match.

Northwind is an equal opportunity employer and considers all applicants without regard to race.
"""


@pytest.mark.parametrize("line,role", [
    ("Key Responsibilities", "duty"),
    ("What You'll Do:", "duty"),
    ("Required Qualifications", "required"),
    ("Preferred Qualifications", "preferred"),
    ("Nice to Have", "preferred"),
    ("Travel Requirements", "logistics"),
    ("Physical Requirements", "required"),
    ("Pay Range", "benefit"),
    ("Equal Opportunity Employer", "legal"),
    ("About Us", "company"),
    ("About the Role", "summary"),
    ("Job Description", "neutral"),
])
def test_headings_name_their_section(line, role):
    assert jd_line_model.looks_like_heading(line)
    assert jd_line_model.heading_role(line) == role


@pytest.mark.parametrize("line", [
    "Work Schedule: Monday-Friday 8am-4:30pm or 9am-5:30pm",
    "Location: Austin, TX",
    "Design and build data pipelines in Python and Spark.",
    "Experience with Python, SQL, and Spark",
])
def test_a_fact_under_a_label_is_not_a_heading(line):
    assert not jd_line_model.looks_like_heading(line)


def test_a_label_line_does_not_relabel_the_lines_after_it():
    """"Work Schedule: ..." read as a heading made the next seven lines logistics."""
    lines = ["Key Responsibilities", "Work Schedule: Monday-Friday 8am-5pm", "Analyze patient data."]
    assert jd_line_model.silver_labels(lines) == ["heading", "logistics", "duty"]


def test_a_preference_under_a_required_heading_stays_a_preference():
    lines = ["Requirements", "5+ years of Python.", "Kafka experience is a plus."]
    assert jd_line_model.silver_labels(lines) == ["heading", "required", "preferred"]


def test_lines_with_no_heading_above_them_are_not_vouched_for():
    assert jd_line_model.silver_labels(["We build things."]) == [None]


def test_every_line_gets_a_role_and_keeps_its_order():
    found = jd_line_model.read_lines(POSTING)
    assert [item["text"] for item in found] == jd_line_model.lines_of(POSTING)
    assert all(item["role"] in jd_line_model.ROLES for item in found)
    assert all(0.0 <= item["confidence"] <= 1.0 for item in found)


def test_it_reads_a_plain_posting():
    roles = {item["text"]: item["role"] for item in jd_line_model.read_lines(POSTING)}
    assert roles["Key Responsibilities"] == "heading"
    assert roles["Design and build data pipelines in Python and Spark."] == "duty"
    assert roles["5+ years of experience with Python and SQL."] == "required"
    assert roles["Experience with Kafka."] == "preferred"
    assert roles["401(k) with a company match."] == "benefit"


def test_it_reads_a_posting_with_no_headings_at_all():
    """Headings are how it learned. It still has to read lines without them."""
    posting = ("Design and build data pipelines in Python and Spark.\n"
               "5+ years of experience with Python and SQL required.\n"
               "We offer medical, dental and vision coverage from day one.\n"
               "We are an equal opportunity employer.\n")
    roles = [item["role"] for item in jd_line_model.read_lines(posting)]
    assert roles == ["duty", "required", "benefit", "legal"]


def test_empty_postings_are_empty():
    assert jd_line_model.read_lines("") == []
    assert jd_line_model.read_lines(None) == []
    content = jd_profile.read_content("")
    assert content["requirements"] == [] and content["responsibilities"] == []


def test_a_missing_weights_file_degrades_to_headings(monkeypatch):
    monkeypatch.setattr(jd_line_model, "_model", None)
    monkeypatch.setattr(jd_line_model, "WEIGHTS_FILE", "/nonexistent/weights.json.gz")
    found = {item["text"]: item for item in jd_line_model.read_lines(POSTING)}
    assert found["Experience with Kafka."]["role"] == "preferred"
    assert found["Experience with Kafka."]["confidence"] == 0.0


def test_a_corrupt_weights_file_degrades_to_headings(monkeypatch, tmp_path):
    bad = tmp_path / "weights.json.gz"
    bad.write_bytes(b"not gzip")
    monkeypatch.setattr(jd_line_model, "_model", None)
    monkeypatch.setattr(jd_line_model, "WEIGHTS_FILE", str(bad))
    assert jd_line_model.read_lines(POSTING)[0]["confidence"] == 0.0


def test_content_lines_are_copied_never_written():
    """The whole reason it is safe: a résumé prompt only sees the posting's own words."""
    cleaned = jd_skill_model.clean(POSTING)
    content = jd_profile.read_content(POSTING)
    assert content["responsibilities"] and content["qualifications"]
    for line in content["responsibilities"] + content["qualifications"]:
        assert line in cleaned
    for record in content["requirements"]:
        assert record["source_excerpt"] in cleaned


def test_content_splits_required_from_preferred():
    content = jd_profile.read_content(POSTING)
    assert "5+ years of experience with Python and SQL." in content["qualifications"]
    by_text = {r["text"]: r for r in content["requirements"]}
    assert by_text["5+ years of experience with Python and SQL."]["priority"] == "required"
    assert by_text["Experience with Kafka."]["priority"] == "preferred"
    assert by_text["Partner with analysts to define metrics."]["kind"] == "responsibility"
    assert "kafka" in content["preferred_skills"]
    assert "kafka" not in content["required_skills"]
    assert {"python", "sql"} <= set(content["required_skills"])


@pytest.mark.parametrize("line", [
    "Must be authorized to work in the United States without visa sponsorship.",
    "US citizenship is required.",
    "Active Secret security clearance required.",
    "We do not sponsor H-1B visas.",
    "Northwind is an equal opportunity employer.",
])
def test_eligibility_wording_never_reaches_a_resume_prompt(line):
    """A résumé must never echo it, even when the line reads as a requirement."""
    posting = f"Required Qualifications\n5+ years of Python.\n{line}\n"
    content = jd_profile.read_content(posting)
    everything = content["responsibilities"] + content["qualifications"]
    everything += [r["text"] for r in content["requirements"]]
    assert line not in everything
    assert "5+ years of Python." in everything


def test_benefits_and_company_text_stay_out_of_what_tailoring_sees():
    content = jd_profile.read_content(POSTING)
    joined = " ".join(content["responsibilities"] + content["qualifications"]).lower()
    assert "401(k)" not in joined and "global leader" not in joined


def test_local_analysis_has_the_llm_shape():
    analysis = jd_profile.local_analysis(POSTING, "Senior Data Engineer")
    assert set(JD_ANALYSIS_SCHEMA) <= set(analysis)
    assert analysis["job_title"] == "Senior Data Engineer"
    assert analysis["experience_years"] == "5+ years"
    assert analysis["analysis_source"] == "local"
    assert analysis["analysis_completeness"] == "complete"
    assert analysis["requirements"]
    for record in analysis["requirements"]:
        assert record["source_excerpt"] in analysis["source_text"]


def test_local_analysis_invents_nothing_it_was_not_told():
    analysis = jd_profile.local_analysis(POSTING)
    assert analysis["job_title"] == ""
    assert analysis["company"] == "Not specified"
    assert analysis["location"] == "Not specified"


def test_a_silent_posting_stays_silent():
    analysis = jd_profile.local_analysis("We are hiring.\nJoin us.\n")
    assert analysis["experience_years"] == ""
    assert analysis["required_skills"] == []


def test_build_fills_content_without_an_llm():
    profile = jd_profile.build(POSTING, "Senior Data Engineer")
    assert profile["provenance"]["llm"] is False
    assert profile["content"]["responsibilities"]
    assert profile["content"]["qualifications"]


def test_build_keeps_the_llms_lists_when_it_ran():
    analysis = {"responsibilities": ["from the llm"], "qualifications": ["from the llm too"]}
    profile = jd_profile.build(POSTING, "Senior Data Engineer", analysis=analysis)
    assert profile["content"]["responsibilities"] == ["from the llm"]
    assert profile["content"]["qualifications"] == ["from the llm too"]


def test_analysis_falls_back_to_the_reader_when_the_llm_fails(monkeypatch):
    from services.resume_service import ResumeService

    def down(text):
        raise RuntimeError("no working key")

    service = ResumeService.__new__(ResumeService)
    monkeypatch.setattr(ResumeService, "_analyze_jd_with_llm", staticmethod(down))
    monkeypatch.delenv("JD_ANALYSIS_SOURCE", raising=False)
    analysis = service.extract_jd(POSTING)
    assert analysis["analysis_source"] == "local"
    assert analysis["jd_profile"]["content"]["responsibilities"]


def test_analysis_can_skip_the_llm_altogether(monkeypatch):
    from services.resume_service import ResumeService

    def boom(text):  # pragma: no cover - the point is that it is never called
        raise AssertionError("the LLM must not be called")

    service = ResumeService.__new__(ResumeService)
    monkeypatch.setattr(ResumeService, "_analyze_jd_with_llm", staticmethod(boom))
    monkeypatch.setenv("JD_ANALYSIS_SOURCE", "local")
    assert service.extract_jd(POSTING)["analysis_source"] == "local"


def test_a_working_llm_still_answers(monkeypatch):
    from services.resume_service import ResumeService

    service = ResumeService.__new__(ResumeService)
    monkeypatch.setattr(ResumeService, "_analyze_jd_with_llm", staticmethod(
        lambda text: {"job_title": "From the LLM", "responsibilities": ["x"]}))
    monkeypatch.delenv("JD_ANALYSIS_SOURCE", raising=False)
    assert service.extract_jd(POSTING)["job_title"] == "From the LLM"
