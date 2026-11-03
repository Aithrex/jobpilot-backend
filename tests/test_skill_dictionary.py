"""One skill dictionary, read the same way from a resume and from a JD.

Read on all 28 live resumes (2026-11-03), the scorer recognised 647 of 1,343
listed skills (48%). Spring Boot, FastAPI, Flask, Django, LangChain, Hugging
Face, JWT and S3 did not exist, so a JD asking for Spring Boot and a resume
holding it matched on nothing, and both looked like any other Java posting.
"""
import pytest

from services import fit_score as fs


@pytest.mark.parametrize("text,skills", [
    ("5+ years building services with Spring Boot and Hibernate", {"spring boot", "hibernate"}),
    ("APIs in FastAPI, Flask and Django", {"fastapi", "flask", "django"}),
    ("RAG pipelines with LangChain and a vector database (Pinecone)", {"langchain", "vector databases"}),
    ("Deploy on EKS with Helm charts; data in S3 via AWS Glue", {"kubernetes", "aws"}),
    ("Secure endpoints with JWT and role-based access control", {"authentication"}),
    ("Unit tests with JUnit and Mockito; API tests in Postman", {"unit testing", "api testing"}),
    ("Strong grasp of data structures and algorithms and design patterns",
     {"data structures & algorithms", "object-oriented design"}),
    ("Embedded C and C/C++ on microcontrollers", {"c", "c++", "embedded c", "firmware"}),
    ("IAM policies across accounts", {"iam"}),
])
def test_jd_text_reads_the_skills_it_names(text, skills):
    assert skills <= set(fs.find_skills(text)), text


@pytest.mark.parametrize("text", [
    "Spring 2027 internship in our Austin office",
    "American Express is hiring a branch manager",
    "This is your chance to take the helm of our deli operations",
    "Rest assured, your information will be treated confidentially",
    "Use of TDD equipment, pizza cutter and pizza peel",
    "Email ocr.newyork@ed.gov with questions",
    "Thorough understanding of control system design for HVAC",
    "Protected individual per 8 U.S.C. 1324b(a)(3)",
    "C-suite stakeholders and Grade C students",
    "Erlenmeyer flasks and lab glassware",
])
def test_ordinary_english_is_not_a_skill(text):
    new = {"spring boot", "express.js", "kubernetes", "api testing", "unit testing",
           "computer vision", "system design", "c", "flask"}
    assert not (new & set(fs.find_skills(text))), text


def test_resume_items_read_the_short_words_jd_text_cannot():
    got = fs.resume_item_skills(["C", "Go", "R", "Spring", "Express", "S3", "Lambda",
                                 "Word", "LLMs", "Matplotlib"])
    assert {"c", "go", "r", "spring boot", "express.js", "aws", "serverless",
            "microsoft office", "llm", "data visualization"} <= got
    # ...which the JD side still refuses in running text.
    assert "spring boot" not in fs.find_skills("Spring semester")


def test_both_resume_paths_use_the_item_reader():
    listed = fs._profile_from_skills(["Spring", "S3", "FastAPI"], ["Backend Engineer"], 3)["listed_skills"]
    assert {"spring boot", "aws", "fastapi"} <= set(listed)
    resume = {"skills": {"Languages": ["C", "Go"], "Cloud": "S3, EC2"},
              "experience": [{"title": "Engineer", "bullets": ["Built FastAPI services on EKS"]}]}
    prof = fs.build_resume_profile(resume)
    assert {"c", "go", "aws"} <= set(prof["listed_skills"])
    assert {"fastapi", "kubernetes"} <= set(prof["experience_skills"]) | set(prof["listed_skills"])


def test_the_iam_alias_is_a_word_not_a_backspace():
    assert "iam" in fs.SKILL_LEXICON["iam"]
    assert not any("\x08" in a for aliases in fs.SKILL_LEXICON.values() for a in aliases)
