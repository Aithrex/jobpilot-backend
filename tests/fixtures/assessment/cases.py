"""Offline fixtures for assessment-v2 Package 0 inventory and Package 1 reducer tests.

These are synthetic cases from the master plan adversarial list. They are not
employer validation and not a single-user template.
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# Resume / JD stubs for inventory and future artifact tests
# ---------------------------------------------------------------------------

SQL_NOSQL_RESUME = {
    "contact": {"name": "Alex Rivera", "email": "a@example.com", "phone": "555-0100"},
    "summary": "Data engineer working with NoSQL document stores.",
    "skills": {"Data": ["NoSQL", "MongoDB"]},
    "experience": [{
        "company": "DataCo",
        "title": "Data Engineer",
        "dates": "2022 - Present",
        "bullets": ["Operated MongoDB NoSQL clusters for product analytics."],
    }],
    "education": [],
    "projects": [],
    "certifications": [],
}

SQL_NOSQL_JD = {
    "job_title": "Analytics Engineer",
    "required_skills": ["SQL"],
    "preferred_skills": [],
    "keywords": ["SQL"],
    "responsibilities": ["Write SQL against the warehouse"],
    "qualifications": [],
}

AND_OR_JD = {
    "job_title": "Backend Engineer",
    "required_skills": ["Python", "Java", "SQL"],
    "responsibilities": ["Build services in Python or Java, plus SQL"],
    "qualifications": [],
    "keywords": ["Python", "Java", "SQL"],
    # Intended canonical expression once Package 4 lands:
    # AND(OR(Python, Java), SQL)
    "requirements": [
        {
            "text": "Python or Java, plus SQL",
            "priority": "required",
            "kind": "capability",
            "alternatives": ["Python", "Java"],
            "source_excerpt": "Python or Java, plus SQL",
        }
    ],
}

WRONG_EMPLOYER_RESUME = {
    "contact": {"name": "Sam Lee", "email": "s@example.com", "phone": ""},
    "summary": "",
    "skills": {"Languages": ["Python"]},
    "experience": [
        {
            "company": "Acme",
            "title": "Software Engineer",
            "dates": "2020 - 2022",
            "bullets": ["Built billing APIs in Python."],
        },
        {
            "company": "Globex",
            "title": "Software Engineer",
            "dates": "2022 - Present",
            "bullets": ["Built billing APIs in Python."],  # same text, wrong source job
        },
    ],
    "education": [],
    "projects": [],
    "certifications": [],
}

KEYWORD_REPEAT_RESUME = {
    "contact": {"name": "Jordan Kim", "email": "j@example.com", "phone": ""},
    "summary": "Python Python Python engineer.",
    "skills": {"Languages": ["Python", "Python"]},
    "experience": [{
        "company": "Shoply",
        "title": "Engineer",
        "dates": "2023 - Present",
        "bullets": [
            "Used Python daily.",
            "Used Python daily.",
            "Used Python daily.",
        ],
    }],
    "education": [],
    "projects": [],
    "certifications": [],
}

ABSENCE_FINDING_JD = {
    "job_title": "Staff Accountant",
    "required_skills": ["Excel"],
    "responsibilities": ["Support monthly close and variance commentary"],
    "qualifications": ["CPA preferred"],
    "keywords": ["close"],
}

QUALITATIVE_RESUME = {
    "contact": {"name": "Riley Chen", "email": "r@example.com", "phone": ""},
    "summary": "Registered nurse providing bedside care in a medical-surgical unit.",
    "skills": {"Clinical": ["Med-Surg", "Precepting"]},
    "experience": [{
        "company": "Riverside Hospital",
        "title": "Registered Nurse",
        "dates": "2019 - Present",
        "bullets": [
            "Provided bedside care for a six-patient med-surg assignment.",
            "Precepted four new-graduate nurses through their first twelve weeks.",
        ],
    }],
    "education": [],
    "projects": [],
    "certifications": [],
}

# Cases the inventory tracks for later packages (Package 3+).
FIXTURE_CASES = [
    {
        "id": "sql_nosql_substring",
        "resume": SQL_NOSQL_RESUME,
        "jd": SQL_NOSQL_JD,
        "adversarial": "SQL must not match NoSQL via whitespace-stripped normalization",
        "package": 3,
    },
    {
        "id": "and_or_python_java_sql",
        "resume": KEYWORD_REPEAT_RESUME,
        "jd": AND_OR_JD,
        "adversarial": "AND(OR(Python, Java), SQL) must not flatten to three independent headlines",
        "package": 1,
    },
    {
        "id": "wrong_employer_claim",
        "resume": WRONG_EMPLOYER_RESUME,
        "jd": {"job_title": "Backend Engineer", "required_skills": ["Python"], "responsibilities": []},
        "adversarial": "Identical bullet text on the wrong employer fails source reconciliation",
        "package": 4,
    },
    {
        "id": "keyword_repetition",
        "resume": KEYWORD_REPEAT_RESUME,
        "jd": {"job_title": "Python Engineer", "required_skills": ["Python"], "responsibilities": []},
        "adversarial": "Repeating Python does not increase coverage counts",
        "package": 4,
    },
    {
        "id": "absence_without_fake_quote",
        "resume": QUALITATIVE_RESUME,
        "jd": ABSENCE_FINDING_JD,
        "adversarial": "Missing requirement findings cite JD + search scope, not fabricated resume quotes",
        "package": 5,
    },
    {
        "id": "qualitative_full_support",
        "resume": QUALITATIVE_RESUME,
        "jd": {
            "job_title": "Registered Nurse",
            "required_skills": [],
            "responsibilities": ["Provide bedside care", "Precept new graduates"],
        },
        "adversarial": "Qualitative accomplishments can fully support appropriate requirements",
        "package": 4,
    },
    {
        "id": "docx_table_order",
        "resume": SQL_NOSQL_RESUME,
        "jd": SQL_NOSQL_JD,
        "adversarial": "DOCX must traverse document order; not all paragraphs then all tables",
        "package": 3,
    },
]


# Representative tailor pair for the keyword-mismatch audit. Synthetic — the
# screenshot's exact saved record was not available in this workspace.
SCREENSHOT_LIKE_RESUME = {
    "contact": {"name": "Alex Rivera", "email": "a@example.com", "phone": "555-0100"},
    "summary": "Backend engineer who developed REST endpoints and documented them with OpenAPI.",
    "skills": {
        "Languages": ["Python", "Java"],
        "Data": ["PostgreSQL"],
        "APIs": ["OpenAPI"],
    },
    "experience": [{
        "company": "DataCo",
        "title": "Software Engineer",
        "dates": "2022 - Present",
        "bullets": [
            "Developed REST endpoints for billing and account services.",
            "Published OpenAPI specifications for those services.",
            "Tuned PostgreSQL queries for the reporting warehouse.",
        ],
    }],
    "projects": [{
        "name": "Payments sandbox",
        "bullets": ["Certified AWS practitioner coursework applied to a sandbox API."],
    }],
    "education": [{
        "degree": "BS Computer Science", "institution": "State University",
        "location": "", "dates": "2020", "gpa": "", "coursework": "",
    }],
    "certifications": ["AWS Certified Cloud Practitioner"],
}

SCREENSHOT_LIKE_JD = {
    "job_title": "Backend Engineer",
    "company": "Example",
    "required_skills": [
        "REST API Development",
        "OpenAPI/Swagger",
        "PostgreSQL",
        "Java",
    ],
    "preferred_skills": ["Kafka"],
    "keywords": ["microservices"],
    "jd_context": {"domain": ["fintech", "distributed systems"]},
    "requirements": [
        {
            "text": "REST API Development",
            "priority": "required",
            "kind": "capability",
            "source_excerpt": "REST API Development",
        },
        {
            "text": "OpenAPI/Swagger",
            "priority": "required",
            "kind": "capability",
            "source_excerpt": "OpenAPI/Swagger",
        },
        {
            "text": "PostgreSQL",
            "priority": "required",
            "kind": "capability",
            "source_excerpt": "PostgreSQL",
        },
    ],
}
