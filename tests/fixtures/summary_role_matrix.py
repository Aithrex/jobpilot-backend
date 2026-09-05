"""Multi-user fixtures for JD-adaptive summary tests and the live eval.

Profiles are synthetic. None of them is a template for production copy.
"""

BACKEND_ENGINEER = {
    "id": "backend_engineer",
    "structured": {
        "summary": (
            "Backend engineer with nearly 4 years building production APIs and data "
            "models in Python and Java. Ships FastAPI and Spring Boot services with "
            "Kafka pipelines and PostgreSQL on AWS and Kubernetes."
        ),
        "skills": {"Languages": ["Python", "Java"], "Cloud": ["AWS"]},
        "experience": [
            {
                "company": "HealthCo",
                "title": "Software Engineer",
                "dates": "2022 - Present",
                "bullets": [
                    "Built production APIs in Python, Django, and FastAPI against PostgreSQL.",
                    "Shipped Spring Boot services in Java with Kafka pipelines.",
                    "Operated Kubernetes workloads on AWS with production observability.",
                    "Designed PostgreSQL schemas for regulated healthcare data models.",
                    "Tuned Kafka consumers for clinical event throughput.",
                    "Hardened AWS IAM and deployment pipelines for the API platform.",
                    "Reduced p95 API latency from 420ms to 95ms on the billing service.",
                    "Wrote pytest coverage for FastAPI handlers in the claims service.",
                    "Owned on-call for backend platform incidents across two regions.",
                ],
            }
        ],
        "projects": [
            {
                "name": "Intake Console",
                "bullets": [
                    "Added a React admin view for internal clinical ops dashboards.",
                    "Shipped end-to-end schema-to-UI work on one patient-intake feature.",
                ],
            }
        ],
    },
}

FRONTEND_ENGINEER = {
    "id": "frontend_engineer",
    "structured": {
        "summary": (
            "Frontend engineer building accessible product interfaces in TypeScript "
            "and React. Ships design-system work with Jest coverage."
        ),
        "skills": {"Languages": ["TypeScript"], "Frontend": ["React"]},
        "experience": [
            {
                "company": "Shoply",
                "title": "Frontend Engineer",
                "dates": "2021 - Present",
                "bullets": [
                    "Built checkout and catalog interfaces in TypeScript and React.",
                    "Cut checkout abandonment 12% by tightening form validation.",
                    "Wrote Jest coverage for the shared design-system components.",
                ],
            }
        ],
        "projects": [],
    },
}

MARKETING_COORDINATOR = {
    "id": "marketing_coordinator",
    "structured": {
        "summary": (
            "Marketing coordinator who plans campaign calendars and reports weekly "
            "performance to brand leads."
        ),
        "skills": {"Tools": ["Excel", "HubSpot"]},
        "experience": [
            {
                "company": "BrightLeaf",
                "title": "Marketing Coordinator",
                "dates": "2023 - Present",
                "bullets": [
                    "Scheduled 18 campaign launches across three product lines.",
                    "Built weekly HubSpot performance reports for brand leads.",
                    "Coordinated vendor invoices and creative reviews in Excel.",
                ],
            }
        ],
        "projects": [],
    },
}

FINANCE_ANALYST = {
    "id": "finance_analyst",
    "structured": {
        "summary": (
            "Financial analyst supporting monthly close and variance commentary "
            "for a manufacturing controller."
        ),
        "skills": {"Tools": ["Excel", "SAP"]},
        "experience": [
            {
                "company": "Northmill",
                "title": "Financial Analyst",
                "dates": "2020 - Present",
                "bullets": [
                    "Prepared monthly close packages and variance commentary in Excel.",
                    "Reconciled SAP cost-center actuals against the annual plan.",
                    "Cut close-cycle time from 8 days to 5 days for two plants.",
                ],
            }
        ],
        "projects": [],
    },
}

NURSE = {
    "id": "nurse",
    "structured": {
        "summary": (
            "Registered nurse providing bedside care in a medical-surgical unit "
            "and precepting new graduates."
        ),
        "skills": {"Clinical": ["Med-Surg", "Precepting"]},
        "experience": [
            {
                "company": "Riverside Hospital",
                "title": "Registered Nurse",
                "dates": "2019 - Present",
                "bullets": [
                    "Provided bedside care for a 6-patient med-surg assignment.",
                    "Precepted 4 new-graduate nurses through their first 12 weeks.",
                    "Documented assessments and interventions in the hospital EHR.",
                ],
            }
        ],
        "projects": [],
    },
}

JDS = {
    "sde": {
        "job_title": "Software Development Engineer",
        "required_skills": ["Python", "APIs"],
        "keywords": ["AWS"],
        "responsibilities": [
            "Design and ship production services",
            "Write tests and operate what you ship",
        ],
        "qualifications": ["2+ years software engineering"],
        "experience_years": "2+ years",
        "industry": "Software",
    },
    "backend": {
        "job_title": "Backend Engineer",
        "required_skills": ["Python", "PostgreSQL", "Kafka"],
        "keywords": ["APIs"],
        "responsibilities": [
            "Build high-throughput backend services",
            "Tune data stores and streaming pipelines",
        ],
        "qualifications": ["3+ years backend"],
        "experience_years": "3+ years",
        "industry": "Software",
    },
    "fullstack": {
        "job_title": "Full-Stack Product Engineer",
        "required_skills": ["Django", "PostgreSQL", "React"],
        "keywords": ["APIs", "Python"],
        "responsibilities": [
            "Own features from schema through API to UI",
            "Ship product increments with designers",
        ],
        "qualifications": ["Experience across the stack"],
        "experience_years": "5+ years",
        "industry": "Software",
    },
    "frontend": {
        "job_title": "Frontend Engineer",
        "required_skills": ["React", "TypeScript"],
        "keywords": ["Jest"],
        "responsibilities": [
            "Build accessible product interfaces",
            "Maintain the design system",
        ],
        "qualifications": ["Frontend experience"],
        "industry": "Ecommerce",
    },
    "ml": {
        "job_title": "Machine Learning Engineer",
        "required_skills": ["Python", "PyTorch"],
        "keywords": ["LLM"],
        "responsibilities": [
            "Train and evaluate models",
            "Ship inference services",
        ],
        "qualifications": ["ML research or applied ML"],
        "industry": "Software",
    },
    "manager": {
        "job_title": "Engineering Manager",
        "required_skills": ["Python"],
        "keywords": ["leadership"],
        "responsibilities": [
            "Coach engineers and run the delivery process",
            "Set team priorities with product",
        ],
        "qualifications": ["Prior people management"],
        "industry": "Software",
    },
    "platform": {
        "job_title": "Platform / Infrastructure Engineer",
        "required_skills": ["Kubernetes", "AWS", "Terraform"],
        "keywords": ["observability"],
        "responsibilities": [
            "Own deployment platforms and reliability",
            "Standardize infrastructure as code",
        ],
        "industry": "Software",
    },
    "same_title_reliability": {
        "job_title": "Backend Engineer",
        "required_skills": ["Python", "AWS"],
        "keywords": ["on-call"],
        "responsibilities": [
            "Own production reliability and incident response",
            "Improve latency and uptime for existing services",
        ],
        "industry": "Software",
    },
    "same_title_delivery": {
        "job_title": "Backend Engineer",
        "required_skills": ["Python", "Django"],
        "keywords": ["features"],
        "responsibilities": [
            "Ship product features end to end with a squad",
            "Deliver API changes on a weekly cadence",
        ],
        "industry": "Software",
    },
    "untitled": {
        "job_title": "",
        "required_skills": ["Python", "APIs"],
        "keywords": ["services"],
        "responsibilities": [
            "Write software services and automated tests",
            "Deploy and operate production APIs",
        ],
        "industry": "Software",
    },
    "office_manager": {
        "job_title": "Office Manager",
        "required_skills": [],
        "keywords": ["scheduling"],
        "responsibilities": [
            "Coordinate staff calendars and vendor invoices",
            "Keep weekly operations reporting current",
        ],
        "industry": "Healthcare",
    },
    "staff_accountant": {
        "job_title": "Staff Accountant",
        "required_skills": ["Excel"],
        "keywords": ["close"],
        "responsibilities": [
            "Support monthly close and variance commentary",
            "Reconcile cost-center actuals",
        ],
        "industry": "Manufacturing",
    },
}


def tailored_from(structured):
    """Empty-summary tailored copy used as writer input."""
    experience = []
    for exp in structured.get("experience") or []:
        experience.append({
            "company": exp.get("company", ""),
            "title": exp.get("title", ""),
            "dates": exp.get("dates", ""),
            "bullets": list(exp.get("bullets") or []),
        })
    projects = []
    for proj in structured.get("projects") or []:
        projects.append({
            "name": proj.get("name", ""),
            "bullets": list(proj.get("bullets") or []),
        })
    return {
        "summary": "",
        "skills": {k: list(v) for k, v in (structured.get("skills") or {}).items()},
        "experience": experience,
        "projects": projects,
    }


def gap_for(required=None, matched=None):
    return {
        "required_matched": list(required or []),
        "matched_keywords": list(matched or []),
    }
