"""De-identified resume fixtures for preservation / multi-profession tests.

No personal names, phones, emails, or employer identities from live PDFs.
"""
from __future__ import annotations

from typing import Any, Dict, List


def contact(**kwargs) -> Dict[str, str]:
    base = {
        "name": "Alex Rivera",
        "email": "alex@example.com",
        "phone": "480-555-0100",
        "linkedin": "",
        "github": "",
        "location": "Tempe, AZ",
        "portfolio": "",
    }
    base.update(kwargs)
    return base


def role(title, company, dates, bullets, **extra) -> Dict[str, Any]:
    row = {
        "title": title,
        "company": company,
        "location": "Remote",
        "dates": dates,
        "type": extra.pop("type", "Full-time"),
        "bullets": list(bullets),
    }
    row.update(extra)
    return row


def project(name, bullets, dates="2024", tech="Python") -> Dict[str, Any]:
    return {"name": name, "dates": dates, "tech": tech, "bullets": list(bullets)}


def extra_section(heading, semantic, entries, **kwargs) -> Dict[str, Any]:
    sec = {
        "heading": heading,
        "original_heading": heading.upper(),
        "semantic_type": semantic,
        "layout_supported": "generic",
        "parse_status": "ok",
        "raw_text": "",
        "entries": entries,
    }
    sec.update(kwargs)
    return sec


LONG = (
    "Delivered platform work covering architecture, ownership, reliability, "
    "customer impact, and cross-team delivery in production systems with "
    "measurable operational outcomes across several quarters."
)


def many_jobs_one_project() -> Dict[str, Any]:
    """Analog of the supplied project-loss case: several roles + one project."""
    jobs = []
    companies = [
        ("Platform Engineer, Contract", "Northwind Analytics", "Dec 2025 - Present"),
        ("Cloud Engineer", "Helix Systems", "May 2023 - May 2024"),
        ("Associate Developer Intern", "Cedar Labs", "Jan 2023 - Apr 2023"),
        ("Backend Intern", "Helix Systems", "Jan 2021 - Mar 2022"),
        ("Software Engineering Intern", "Northwind Analytics", "May 2022 - Aug 2022"),
    ]
    for i, (title, company, dates) in enumerate(companies):
        jobs.append(role(title, company, dates, [
            f"{company} role {i}: {LONG} Stream {i}.a with on-call ownership.",
            f"{company} role {i}: {LONG} Stream {i}.b with API reliability.",
            f"{company} role {i}: {LONG} Stream {i}.c with customer impact.",
        ]))
    return {
        "contact": contact(),
        "summary": "Software engineer who ships production backend and cloud systems.",
        "skills": {
            "Languages": ["Java", "Python", "TypeScript", "SQL"],
            "Cloud": ["AWS", "Terraform", "Docker", "Kubernetes"],
        },
        "experience": jobs,
        "projects": [project(
            "InfraGraph | Foundry",
            [
                "Reads a customer's cloud account read-only through least-privilege "
                "cross-account access across 22 service APIs and materializes it as a graph.",
                "Answers architecture and cost questions with a deterministic solver "
                "over that graph rather than a language model, so every finding carries "
                "a traversable evidence path.",
            ],
            dates="Apr 2026 - Present",
        )],
        "education": [{
            "institution": "State University", "degree": "M.S. Information Technology",
            "dates": "May 2026", "location": "AZ", "gpa": "", "coursework": "",
        }],
        "certifications": [],
        "additional_sections": [],
    }


def student_projects_primary() -> Dict[str, Any]:
    return {
        "contact": contact(name="Jordan Lee"),
        "summary": "",
        "skills": {"Languages": ["Python", "R"], "Tools": ["Git"]},
        "experience": [],
        "projects": [
            project("Capstone Sensor Network", [
                "Built a campus air-quality mesh with 40 nodes and MQTT ingest.",
                "Published weekly PM2.5 dashboards used by facilities staff.",
            ]),
            project("Course Scheduler", [
                "Implemented constraint solving for 1,200 student registrations.",
            ]),
        ],
        "education": [{
            "institution": "State University", "degree": "B.S. Computer Science",
            "dates": "2026", "location": "AZ", "gpa": "3.7", "coursework": "Algorithms",
        }],
        "certifications": [],
        "additional_sections": [
            extra_section("Research", "research", [{
                "title": "Undergraduate researcher",
                "organization": "HCI Lab",
                "dates": "2025",
                "location": "",
                "url": "",
                "bullets": ["Coded interview transcripts for a study of clinic kiosks."],
                "body": "",
            }]),
        ],
    }


def required_license_vs_summary() -> Dict[str, Any]:
    return {
        "contact": contact(name="Sam Patel"),
        "summary": (
            "Registered nurse with a lengthy narrative describing every rotation, "
            "every committee, and every continuing-education workshop attended over "
            "several years, plus optional career goals and a mission statement that "
            "repeats the same patient-advocacy theme in three different sentences."
        ),
        "skills": {"Clinical": ["Med-Surg", "Telemetry"], "Tools": ["Epic"]},
        "experience": [role(
            "Staff Nurse", "Valley Hospital", "2022 - Present",
            ["Cared for a 5-patient telemetry assignment with documented handoff quality."],
            type="Full-time",
        )],
        "projects": [],
        "education": [{
            "institution": "State College", "degree": "BSN",
            "dates": "2022", "location": "AZ", "gpa": "", "coursework": "",
        }],
        "certifications": ["BLS", "ACLS"],
        "additional_sections": [
            extra_section("Licenses", "licenses", [{
                "title": "Registered Nurse",
                "organization": "Arizona Board of Nursing",
                "dates": "2022 - Present",
                "location": "",
                "url": "",
                "bullets": [],
                "body": "RN license #demo-0000, compact privilege.",
            }]),
        ],
    }


def custom_paragraph_section() -> Dict[str, Any]:
    return {
        "contact": contact(name="Riley Chen"),
        "summary": "Operations coordinator supporting multi-site logistics.",
        "skills": {"Tools": ["Excel", "SAP"]},
        "experience": [role(
            "Coordinator", "Harbor Freight Co", "2021 - Present",
            ["Scheduled inbound freight across three warehouses."],
        )],
        "projects": [],
        "education": [{
            "institution": "Community College", "degree": "A.A. Business",
            "dates": "2020", "location": "AZ", "gpa": "", "coursework": "",
        }],
        "certifications": [],
        "additional_sections": [
            extra_section("Community Leadership", "custom", [{
                "title": "",
                "organization": "",
                "dates": "",
                "location": "",
                "url": "",
                "bullets": [],
                "body": "Chaired a neighborhood food-drive that delivered 4,000 meals.",
            }]),
        ],
    }


def grouped_employer() -> Dict[str, Any]:
    return {
        "contact": contact(),
        "summary": "Engineer with two roles at the same company.",
        "skills": {"Languages": ["Python"]},
        "experience": [
            role("Software Engineer, Contract", "Northwind Analytics",
                 "Dec 2025 - Present", ["Owned the production API."], type="Contract"),
            role("Software Engineering Intern", "Northwind Analytics",
                 "May 2022 - Aug 2022", ["Built onboarding and audit trails."], type="Internship"),
            role("Cloud Engineer", "Helix Systems", "2023 - 2024",
                 ["Provisioned AWS accounts with Terraform."]),
        ],
        "projects": [],
        "education": [{
            "institution": "State University", "degree": "B.S. CS",
            "dates": "2023", "location": "AZ", "gpa": "", "coursework": "",
        }],
        "certifications": [],
        "additional_sections": [],
    }


JD_SDE = {
    "job_title": "Software Development Engineering Advisor I",
    "required_skills": ["Java", "SQL", "CI/CD", "AWS"],
    "keywords": ["Python", "REST"],
    "preferred_skills": [],
}

JD_NURSE = {
    "job_title": "Registered Nurse",
    "required_skills": ["RN license", "telemetry", "Epic"],
    "keywords": ["BLS"],
    "qualifications": ["Current RN license in state of practice"],
}

JD_STUDENT = {
    "job_title": "Research Intern",
    "required_skills": ["Python", "research"],
    "keywords": ["MQTT"],
}


CASES: List[Dict[str, Any]] = [
    {
        "id": "many_jobs_one_project",
        "resume": many_jobs_one_project,
        "jd": JD_SDE,
        "must_keep_section": "projects",
        "must_keep_text": "InfraGraph",
    },
    {
        "id": "student_projects_primary",
        "resume": student_projects_primary,
        "jd": JD_STUDENT,
        "must_keep_section": "projects",
        "must_keep_text": "MQTT",
    },
    {
        "id": "required_license",
        "resume": required_license_vs_summary,
        "jd": JD_NURSE,
        "must_keep_section": "licenses",
        "must_keep_text": "Registered Nurse",
    },
    {
        "id": "custom_paragraph",
        "resume": custom_paragraph_section,
        "jd": {"job_title": "Operations Coordinator", "required_skills": ["Excel"]},
        "must_keep_section": "custom",
        "must_keep_text": "food-drive",
    },
]
