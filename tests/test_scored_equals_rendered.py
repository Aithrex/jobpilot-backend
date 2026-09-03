"""P1-3: scored text and extracted PDF text must share the same sentinel terms."""
import re

from schemas.resume_schemas import build_resume_text
from services.pdf_ats_check import extract_pdf_text
from services.resume_renderer import ResumeRenderer

SENTINELS = (
    "ZXQSMMTOKEN",
    "ZXQSKLTOKEN",
    "ZXQBLTTOKEN",
    "ZXQCRSTOKEN",
    "ZXQPRJTOKEN",
    "ZXQPBULTOKEN",
    "ZXQCERTTOKEN",
)
GHOST = "ZXQINVTOKEN"


def _resume():
    return {
        "contact": {
            "name": "Alex Rivera", "email": "a@example.com", "phone": "480-555-0100",
            "linkedin": "", "github": "", "location": "Tempe, AZ", "portfolio": "",
        },
        "summary": "Engineer who shipped ZXQSMMTOKEN systems.",
        "skills": {"Languages": ["ZXQSKLTOKEN"]},
        "experience": [{
            "title": "Engineer", "company": "Acme", "location": "AZ",
            "dates": "January 2022 - Present", "type": "Full-time",
            "bullets": ["Delivered ZXQBLTTOKEN in production"],
        }],
        "education": [{
            "degree": "BS Computer Science", "institution": "State University",
            "location": "AZ", "dates": "May 2020", "gpa": "",
            "coursework": "ZXQCRSTOKEN",
        }],
        "projects": [{
            "name": "ZXQPRJTOKEN",
            "tech": GHOST,
            "bullets": ["Used ZXQPBULTOKEN"],
        }],
        "certifications": ["ZXQCERTTOKEN"],
    }


def _tokens(blob: str) -> set:
    return set(re.findall(r"zxq[a-z]+token", blob.lower()))


def test_build_resume_text_and_pdf_share_the_same_term_set():
    resume = _resume()
    scored = build_resume_text(resume)
    pdf, _ = ResumeRenderer().generate_pdf(resume)
    extracted, _ = extract_pdf_text(pdf)

    scored_tok = _tokens(scored)
    extracted_tok = _tokens(extracted)
    expected = {s.lower() for s in SENTINELS}

    assert scored_tok == expected
    assert extracted_tok == expected
    assert GHOST.lower() not in scored_tok
    assert GHOST.lower() not in extracted_tok
