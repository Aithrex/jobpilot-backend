"""100+ anonymised résumé/job scenarios across the fourteen technical domains.

Why a second fixture file
-------------------------
`fit_score_cases.py` has 10 résumés and 33 jobs, and its job is to prove the
engine does not rank a teacher above a backend engineer. That is a coarse
question and the fixtures are sized for it. It cannot answer the finer one
this file exists for: **does the engine separate a robotics engineer from an
embedded engineer, or a quantum software engineer from a distributed-systems
engineer?** Those pairs are all `core` technical, all plausible on a keyword
reading, and they are what a technical seeker's feed is actually made of.

Anonymisation
-------------
Every résumé here is SYNTHETIC. Names are `Candidate <domain>-<n>`, employers
are `Employer A`..`Employer D`, and no line was taken from a real person's
résumé. The job descriptions are written from the requirement patterns the
live inventory shows, not copied from any employer's posting. Nothing in this
file is anyone's data, which is what makes it safe to check in and to keep
running in CI forever.

Structure
---------
`DOMAINS` declares, per domain, the content that actually differs: titles,
skills, the bullets a person in that domain writes, and the requirements a
posting in that domain states. `JOBS` and `RESUMES` are composed from it, so
adding a domain is a dict entry rather than sixty lines of prose — and the
composition is deliberately thin, because the thing under test is the
CONTENT (does `quantum_software` separate from `distributed_systems`?), not
the prose around it.

`SCENARIOS` is the labelled part and it is hand-written. Each row is

    (resume_id, job_id, grade, expected_eligibility)

with the same grade scale the original fixtures use:

    3  clear fit - a plausible interview
    2  reasonable fit - adjacent discipline, or a surmountable gap
    1  marginal - defensible to show, but a stretch
    0  not relevant - wrong field, or a hard requirement they cannot meet

`expected_eligibility` is `eligible`, `ineligible` or `None` (meaning "do not
assert"). It is asserted SPARINGLY and only where the answer is a fact rather
than a judgement: a missing named licence, a stated clearance the résumé
lacks. Two technical specialties the adjacency matrix does not connect are
`unrelated` and still ELIGIBLE — that is the 2.1 rule and this file is one of
the things that must keep it true.
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

# ---------------------------------------------------------------------------
# Per-domain content. Everything that differs between domains lives here.
# ---------------------------------------------------------------------------
#: `key -> {label, senior_title, mid_title, skills, bullets, required,
#:          preferred, years_senior, years_mid}`
DOMAINS: Dict[str, Dict[str, Any]] = {
    "backend": {
        "label": "Backend",
        "senior_title": "Senior Backend Engineer",
        "mid_title": "Backend Engineer",
        "skills": ["Python", "Go", "PostgreSQL", "REST API", "Docker",
                   "Kafka", "Redis", "microservices"],
        "bullets": [
            "Built and operated Python and Go microservices behind REST APIs.",
            "Owned PostgreSQL schema design and query performance for a payments ledger.",
            "Ran Kafka consumers processing 40M events a day.",
        ],
        "required": ["Python", "SQL", "REST APIs", "Docker", "microservices"],
        "preferred": ["Go", "Kafka", "Redis"],
        "years_senior": 7, "years_mid": 3,
    },
    "frontend": {
        "label": "Frontend",
        "senior_title": "Senior Frontend Engineer",
        "mid_title": "Frontend Engineer",
        "skills": ["TypeScript", "React", "JavaScript", "CSS", "Webpack",
                   "Jest", "accessibility", "GraphQL"],
        "bullets": [
            "Built a React and TypeScript design system used by nine product teams.",
            "Cut largest-contentful-paint from 4.1s to 1.3s with code splitting.",
            "Drove WCAG 2.1 AA accessibility remediation across the checkout flow.",
        ],
        "required": ["TypeScript", "React", "CSS", "accessibility"],
        "preferred": ["GraphQL", "Jest", "Webpack"],
        "years_senior": 8, "years_mid": 3,
    },
    "full_stack": {
        "label": "Full stack",
        "senior_title": "Senior Full Stack Engineer",
        "mid_title": "Full Stack Engineer",
        "skills": ["TypeScript", "React", "Node.js", "PostgreSQL", "REST API",
                   "Docker", "AWS"],
        "bullets": [
            "Shipped features end to end in React, Node.js and PostgreSQL.",
            "Built REST APIs and the React screens that consume them.",
            "Deployed to AWS with Docker and GitHub Actions.",
        ],
        "required": ["TypeScript", "React", "Node.js", "SQL", "REST APIs"],
        "preferred": ["AWS", "Docker"],
        "years_senior": 6, "years_mid": 3,
    },
    "mobile": {
        "label": "Mobile",
        "senior_title": "Senior iOS Engineer",
        "mid_title": "Android Engineer",
        "skills": ["Swift", "Kotlin", "iOS", "Android", "SwiftUI",
                   "Jetpack Compose", "REST API"],
        "bullets": [
            "Owned a Swift and SwiftUI iOS app with 3M monthly active users.",
            "Reduced cold start from 2.4s to 0.9s by deferring non-critical work.",
            "Shipped Kotlin and Jetpack Compose screens on the Android client.",
        ],
        "required": ["Swift", "iOS", "REST APIs"],
        "preferred": ["SwiftUI", "Kotlin"],
        "years_senior": 9, "years_mid": 4,
    },
    "devops_sre": {
        "label": "DevOps / SRE",
        "senior_title": "Senior Site Reliability Engineer",
        "mid_title": "DevOps Engineer",
        "skills": ["Kubernetes", "Terraform", "Prometheus", "Go", "Python",
                   "CI/CD", "Linux", "Grafana"],
        "bullets": [
            "Ran Kubernetes across three regions and owned the on-call rotation.",
            "Wrote Terraform modules covering the whole production estate.",
            "Built SLO dashboards in Prometheus and Grafana and cut alert noise by 70%.",
        ],
        "required": ["Kubernetes", "Terraform", "Linux", "CI/CD"],
        "preferred": ["Prometheus", "Go"],
        "years_senior": 8, "years_mid": 4,
    },
    "cloud": {
        "label": "Cloud",
        "senior_title": "Senior Cloud Engineer",
        "mid_title": "Cloud Infrastructure Engineer",
        "skills": ["AWS", "Terraform", "networking", "IAM", "Python",
                   "CloudFormation", "Linux"],
        "bullets": [
            "Designed AWS landing zones, VPC networking and IAM boundaries.",
            "Migrated 140 workloads from data centres to AWS with Terraform.",
            "Cut cloud spend 31% through rightsizing and savings plans.",
        ],
        "required": ["AWS", "Terraform", "networking", "IAM"],
        "preferred": ["Python", "CloudFormation"],
        "years_senior": 9, "years_mid": 4,
    },
    "security": {
        "label": "Security",
        "senior_title": "Senior Application Security Engineer",
        "mid_title": "Security Operations Engineer",
        "skills": ["threat modeling", "OWASP", "penetration testing", "Python",
                   "SAST", "SIEM", "incident response", "cryptography"],
        "bullets": [
            "Ran threat modelling and secure design review for 40 services.",
            "Built SAST and dependency scanning into CI and triaged the findings.",
            "Led incident response for two Sev-1 security incidents.",
        ],
        "required": ["threat modeling", "OWASP", "SAST", "secure code review"],
        "preferred": ["Python", "penetration testing"],
        "years_senior": 8, "years_mid": 4,
    },
    "data": {
        "label": "Data",
        "senior_title": "Senior Data Engineer",
        "mid_title": "Analytics Engineer",
        "skills": ["SQL", "Python", "Spark", "Airflow", "dbt", "Snowflake",
                   "data modeling"],
        "bullets": [
            "Built Airflow and Spark pipelines loading 2TB a day into Snowflake.",
            "Owned dbt models and the semantic layer the analytics team reads.",
            "Cut warehouse cost 44% by rewriting the heaviest twelve models.",
        ],
        "required": ["SQL", "Python", "Spark", "Airflow", "data modeling"],
        "preferred": ["dbt", "Snowflake"],
        "years_senior": 7, "years_mid": 3,
    },
    "ai_ml": {
        "label": "AI / ML",
        "senior_title": "Senior Machine Learning Engineer",
        "mid_title": "Applied AI Engineer",
        "skills": ["PyTorch", "Python", "machine learning", "LLM", "MLOps",
                   "transformers", "SQL", "feature engineering"],
        "bullets": [
            "Trained and served PyTorch ranking models behind a 12ms p99 budget.",
            "Built the retrieval and evaluation harness for an LLM assistant.",
            "Owned the feature store and the offline/online consistency checks.",
        ],
        "required": ["Python", "PyTorch", "machine learning", "MLOps"],
        "preferred": ["LLM", "transformers"],
        "years_senior": 7, "years_mid": 3,
    },
    "embedded": {
        "label": "Embedded",
        "senior_title": "Senior Embedded Software Engineer",
        "mid_title": "Firmware Engineer",
        "skills": ["C", "C++", "RTOS", "firmware", "I2C", "SPI",
                   "device drivers", "oscilloscope"],
        "bullets": [
            "Wrote C and C++ firmware on an RTOS for a battery-powered sensor.",
            "Brought up I2C and SPI device drivers and debugged with a logic analyser.",
            "Cut idle current from 480uA to 22uA to hit a two-year battery target.",
        ],
        "required": ["C", "RTOS", "firmware", "device drivers"],
        "preferred": ["C++", "I2C"],
        "years_senior": 10, "years_mid": 4,
    },
    "semiconductor": {
        "label": "Semiconductor",
        "senior_title": "Senior ASIC Design Engineer",
        "mid_title": "Design Verification Engineer",
        "skills": ["Verilog", "SystemVerilog", "RTL", "UVM", "timing closure",
                   "synthesis", "ASIC", "FPGA"],
        "bullets": [
            "Owned RTL for a memory controller block through tapeout.",
            "Closed timing at 1.4GHz across three process corners.",
            "Built a UVM testbench and drove functional coverage to 98%.",
        ],
        "required": ["SystemVerilog", "RTL design", "synthesis", "timing closure"],
        "preferred": ["UVM", "FPGA"],
        "years_senior": 11, "years_mid": 4,
    },
    "robotics": {
        "label": "Robotics",
        "senior_title": "Senior Robotics Software Engineer",
        "mid_title": "Perception Engineer",
        "skills": ["ROS", "C++", "Python", "SLAM", "computer vision",
                   "motion planning", "sensor fusion", "Kalman filter"],
        "bullets": [
            "Wrote C++ ROS nodes for motion planning on a warehouse AMR fleet.",
            "Implemented SLAM and sensor fusion across lidar, IMU and wheel odometry.",
            "Cut path-planning latency from 90ms to 24ms.",
        ],
        "required": ["ROS", "C++", "motion planning", "sensor fusion"],
        "preferred": ["SLAM", "computer vision"],
        "years_senior": 8, "years_mid": 3,
    },
    "gaming": {
        "label": "Gaming",
        "senior_title": "Senior Game Engine Programmer",
        "mid_title": "Gameplay Engineer",
        "skills": ["C++", "Unreal Engine", "Unity", "graphics", "shaders",
                   "game engine", "profiling", "physics"],
        "bullets": [
            "Owned rendering and shader work in an Unreal Engine title.",
            "Held a 60fps frame budget on console through CPU and GPU profiling.",
            "Built gameplay systems in C++ and exposed them to designers.",
        ],
        "required": ["C++", "game engine", "graphics", "profiling"],
        "preferred": ["Unreal Engine", "shaders"],
        "years_senior": 9, "years_mid": 3,
    },
    "quantum": {
        "label": "Quantum",
        "senior_title": "Senior Quantum Software Engineer",
        "mid_title": "Quantum Control Engineer",
        "skills": ["Qiskit", "Python", "quantum computing", "compilers",
                   "quantum error correction", "C++", "FPGA", "linear algebra"],
        "bullets": [
            "Built the transpiler pass pipeline for a superconducting backend.",
            "Implemented surface-code decoders and measured logical error rates.",
            "Wrote Python and C++ tooling for pulse-level calibration.",
        ],
        "required": ["Python", "quantum computing", "compilers",
                     "quantum error correction"],
        "preferred": ["Qiskit", "C++"],
        "years_senior": 6, "years_mid": 3,
    },
}

DOMAIN_KEYS: Tuple[str, ...] = tuple(DOMAINS)


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------
def _job(domain: str, level: str, *, location: str = "Remote, US",
         extra_requirements: Tuple[str, ...] = (),
         body_suffix: str = "") -> Dict[str, Any]:
    d = DOMAINS[domain]
    years = d["years_senior"] if level == "senior" else d["years_mid"]
    title = d["senior_title"] if level == "senior" else d["mid_title"]
    req = "\n".join(f"- {r}" for r in list(d["required"]) + list(extra_requirements))
    pref = "\n".join(f"- {r}" for r in d["preferred"])
    return {
        "title": title,
        "location": location,
        "description": f"""
We are hiring a {title} for our {d['label'].lower()} team.

Requirements:
- {years}+ years of professional engineering experience
{req}

Preferred qualifications:
{pref}
{body_suffix}""".strip(),
    }


def _resume(domain: str, level: str, *, location: str = "Remote, US",
            note: str = "") -> Dict[str, Any]:
    d = DOMAINS[domain]
    years = d["years_senior"] if level == "senior" else d["years_mid"]
    title = d["senior_title"] if level == "senior" else d["mid_title"]
    start = 2026 - years
    return {
        "contact": {"name": f"Candidate {domain}-{level[0]}",
                    "email": "candidate@example.invalid", "location": location},
        "summary": f"{d['label']} engineer. {note}".strip(),
        "skills": {"core": list(d["skills"])},
        "experience": [
            {"title": title, "company": "Employer A",
             "dates": f"Jan {start + max(1, years // 2)} - Present",
             "bullets": list(d["bullets"])},
            {"title": d["mid_title"], "company": "Employer B",
             "dates": f"Jan {start} - Dec {start + max(1, years // 2) - 1}",
             "bullets": [d["bullets"][-1]]},
        ],
        "education": [{"degree": "BS Computer Engineering",
                       "institution": "State University",
                       "dates": f"{start - 4} - {start}"}],
        "projects": [],
        "certifications": [],
    }


JOBS: Dict[str, Dict[str, Any]] = {}
RESUMES: Dict[str, Dict[str, Any]] = {}
for _d in DOMAIN_KEYS:
    JOBS[f"j_{_d}_senior"] = _job(_d, "senior")
    JOBS[f"j_{_d}_mid"] = _job(_d, "mid")
    RESUMES[f"r_{_d}_senior"] = _resume(_d, "senior")
    RESUMES[f"r_{_d}_mid"] = _resume(_d, "mid")

#: Two hard-gate jobs. These are the ONLY rows where `expected_eligibility`
#: asserts `ineligible`, because a named clearance and a named licence are
#: facts, and everything else in this file is a judgement about fit.
JOBS["j_security_clearance"] = _job(
    "security", "senior", location="Fort Meade, MD",
    extra_requirements=("Active TS/SCI security clearance with polygraph required",),
    body_suffix="\nThis role requires an active TS/SCI clearance. "
                "Candidates without one cannot be considered.")
JOBS["j_embedded_pe_license"] = _job(
    "embedded", "senior", location="Detroit, MI",
    extra_requirements=("Active Professional Engineer (PE) license required",),
    body_suffix="\nAn active PE license is required for this role.")

#: A résumé that states a clearance, so the gate can be shown to open as well
#: as to close. A gate that only ever closes is indistinguishable from a bug.
RESUMES["r_security_cleared"] = _resume(
    "security", "senior", location="Annapolis Junction, MD",
    note="Holds an active TS/SCI security clearance with polygraph.")
RESUMES["r_security_cleared"]["certifications"] = [
    "Active TS/SCI security clearance with polygraph", "CISSP"]


# ---------------------------------------------------------------------------
# REAL-SHAPED RÉSUMÉS: the four ways a résumé fails to look like a résumé
#
# Every `_resume()` above is a synthetic ideal - a canonical title, a matching
# skill list, dated senior experience. The live bug this section exists for
# was in the gap between that and an actual résumé, and NONE of the 115
# scenarios above could see it: a candidate whose job titles are non-canonical
# scored `role_alignment: unknown` against every posting, which
# `_normalized_score` excludes, which collapsed the whole score onto skill
# overlap. Measured live on 4,000 active core-tech postings for one account:
# role_alignment was `unknown` 4,000 times out of 4,000, and not one Data
# Scientist / ML Engineer / AI Engineer role reached the top 25.
# ---------------------------------------------------------------------------

#: 1. The live bug, reproduced. Two internship titles that name a discipline
#: the phrase table could not see through the suffix, an unmistakable AI/ML +
#: data skill set, and four PORTFOLIO PROJECTS in clinical, therapy and
#: university domains - which is also the industry-inference bug: this person
#: has never worked in healthcare, education, law or the nonprofit sector.
RESUMES["r_newgrad_intern_titles"] = {
    "contact": {"name": "Candidate new-grad", "email": "ng@example.invalid",
                "location": "Tempe, Arizona"},
    "summary": "",
    "skills": {
        "Programming & Data": ["Python", "SQL", "R", "Pandas", "NumPy",
                               "SciPy", "PostgreSQL", "MongoDB"],
        "ML & Deep Learning": ["Scikit-learn", "XGBoost", "PyTorch",
                               "TensorFlow", "Keras", "Feature Engineering",
                               "Time-Series Forecasting"],
        "GenAI, NLP & Vision": ["LLMs", "RAG", "Prompt Engineering",
                                "Hugging Face", "LangChain", "NLP",
                                "Computer Vision"],
        "Data Engineering": ["Apache Spark", "Databricks", "ETL", "Docker"],
        "Analytics": ["Statistical Analysis", "A/B Testing",
                      "Hypothesis Testing", "Tableau", "Power BI"],
    },
    "experience": [
        {"title": "Data Quality Specialist Intern", "company": "Scalpel Inc.",
         "dates": "March 2026 - May 2026",
         "bullets": [
             "Led dataset validation and governance for computer vision "
             "training pipelines, auditing annotation integrity and metadata "
             "consistency.",
             "Investigated model predictions and dataset drift alongside ML "
             "engineers.",
         ]},
        {"title": "Data Science Intern", "company": "BrainOvision Solutions",
         "dates": "May 2023 - June 2023",
         "bullets": [
             "Built a Python ETL pipeline over 500+ unstructured documents "
             "into PostgreSQL.",
             "Engineered NLP document classification across 10+ categories.",
         ]},
    ],
    "education": [{"degree": "M.S. in Data Science, Engineering and Analytics",
                   "institution": "Arizona State University",
                   "dates": "Aug 2024 - May 2026"},
                  {"degree": "B.Tech in Data Science",
                   "institution": "GITAM University",
                   "dates": "Aug 2020 - May 2024"}],
    # The industry trap. Every one of these is a PROJECT, not an employer.
    "projects": [
        {"name": "Hybrid Multimodal Clinical Decision Support System",
         "tech": "PyTorch, LLM, RAG",
         "bullets": ["Fused clinical notes and imaging for a hospital triage "
                     "prototype used in a university course."]},
        {"name": "Talking Personal AI Model for Counseling Therapy",
         "tech": "Speech, LLM",
         "bullets": ["Built a therapy-style conversational agent; evaluated "
                     "with a nonprofit counseling service's public dataset."]},
    ],
    "certifications": [],
    "target_roles": ["Data Scientist", "Data Analyst",
                     "Machine Learning Engineer", "AI Engineer",
                     "Data Engineer", "Software Engineer"],
}

#: 2. Level codes and vendor ladders. Every title is software and not one of
#: them was reachable before `strip_title_modifiers`.
RESUMES["r_backend_level_codes"] = {
    "contact": {"name": "Candidate SDE", "email": "sde@example.invalid",
                "location": "Seattle, WA"},
    "summary": "",
    "skills": {"core": ["Java", "Python", "Spring Boot", "REST API",
                        "PostgreSQL", "Kafka", "Docker", "Kubernetes",
                        "microservices", "distributed systems", "AWS"]},
    "experience": [
        {"title": "SDE II", "company": "Employer A",
         "dates": "Jan 2023 - Present",
         "bullets": ["Owned a payments service in Java and Spring Boot.",
                     "Designed idempotent REST APIs over PostgreSQL and Kafka."]},
        {"title": "Software Development Engineer I", "company": "Employer B",
         "dates": "Jan 2021 - Dec 2022",
         "bullets": ["Built internal microservices and their deployment "
                     "pipelines on AWS."]},
    ],
    "education": [{"degree": "BS Computer Science",
                   "institution": "State University", "dates": "2017 - 2021"}],
    "projects": [], "certifications": [],
}

#: 3. Titles that say nothing at all, and a skill list that says everything.
#: This is the "no evidence must mean NO evidence" case: before 2.2 the
#: taxonomy read the titles, found nothing and reported `unknown`, while the
#: résumé named Terraform, Kubernetes, Prometheus and six AWS services.
RESUMES["r_skills_only_unhelpful_titles"] = {
    "contact": {"name": "Candidate consultant", "email": "c@example.invalid",
                "location": "Austin, TX"},
    "summary": "",
    "skills": {"core": ["Terraform", "Kubernetes", "Docker", "AWS",
                        "CI/CD", "Jenkins", "Ansible", "Helm",
                        "GitHub Actions", "infrastructure as code"]},
    "experience": [
        {"title": "Technical Associate", "company": "Employer A",
         "dates": "Jan 2021 - Present",
         "bullets": ["Ran the Terraform estate and the Kubernetes clusters "
                     "behind three product teams.",
                     "Owned CI/CD on GitHub Actions and Jenkins."]},
        {"title": "Analyst", "company": "Employer B",
         "dates": "Jan 2019 - Dec 2020",
         "bullets": ["Automated cloud provisioning with Ansible and Helm."]},
    ],
    "education": [{"degree": "BS Information Systems",
                   "institution": "State University", "dates": "2015 - 2019"}],
    "projects": [], "certifications": [],
}

#: 4. NO EMPLOYMENT AT ALL - a new graduate with coursework, projects and the
#: target roles they typed into onboarding. `classify_resume` sees no titles,
#: so this row is scored on tools plus declared targets alone, which is the
#: whole of goal 5.
#:
#: The stricter case - declared targets and NOTHING else, no skills either -
#: is deliberately NOT here. It must return `not_scored`, because a declared
#: target is not evidence that anything can be COMPARED against a posting's
#: requirements, and a ranking fixture is the wrong place to assert an absence.
#: `evals/test_resume_evidence.py::TestDeclaredTargetRoles` asserts it
#: directly.
RESUMES["r_newgrad_targets_and_tools"] = {
    "contact": {"name": "Candidate declared", "email": "d@example.invalid",
                "location": "Remote, US"},
    "summary": "",
    "skills": {"core": ["Python", "PyTorch", "scikit-learn", "NumPy",
                        "Pandas", "SQL", "machine learning",
                        "feature engineering", "statistical analysis"]},
    "experience": [],
    "education": [{"degree": "BS Computer Science",
                   "institution": "State University", "dates": "2022 - 2026"}],
    "projects": [
        {"name": "Course capstone: churn prediction",
         "tech": "PyTorch, scikit-learn",
         "bullets": ["Trained and evaluated gradient-boosted and neural "
                     "models on a public churn dataset."]},
    ],
    "certifications": [],
    "target_roles": ["Machine Learning Engineer", "AI Engineer",
                     "Data Scientist"],
}


# ---------------------------------------------------------------------------
# The labelled scenarios
# ---------------------------------------------------------------------------
E = "eligible"
I = "ineligible"

def _self(domain: str) -> List[Tuple[str, str, int, Any]]:
    """A domain's own senior and mid rows. Written once per domain."""
    return [
        (f"r_{domain}_senior", f"j_{domain}_senior", 3, E),
        (f"r_{domain}_senior", f"j_{domain}_mid", 2, E),
        (f"r_{domain}_mid", f"j_{domain}_mid", 3, E),
        # A mid-level candidate against a senior posting: a real gap, worth
        # showing, not a match. `1` rather than `0` on purpose — the market
        # interviews these people and a feed that hides them is wrong in the
        # direction users complain about.
        (f"r_{domain}_mid", f"j_{domain}_senior", 1, E),
    ]


SCENARIOS: List[Tuple[str, str, int, Any]] = []
for _d in DOMAIN_KEYS:
    SCENARIOS.extend(_self(_d))

#: CROSS-DOMAIN pairs, hand-graded. This is the part the original fixtures
#: could not express and the part that matters: every pair below is `core`
#: technical on both sides, so none of them is separable by "is this a
#: technical job". They are separated, or not, by the adjacency matrix.
SCENARIOS.extend([
    # --- genuinely adjacent: the market moves people along these edges -----
    ("r_backend_senior", "j_full_stack_senior", 2, E),
    ("r_backend_senior", "j_data_senior", 2, E),
    ("r_backend_senior", "j_devops_sre_senior", 2, E),
    ("r_backend_senior", "j_cloud_senior", 2, E),
    ("r_full_stack_senior", "j_backend_senior", 2, E),
    ("r_full_stack_senior", "j_frontend_senior", 2, E),
    ("r_frontend_senior", "j_full_stack_senior", 2, E),
    ("r_devops_sre_senior", "j_cloud_senior", 3, E),
    ("r_cloud_senior", "j_devops_sre_senior", 3, E),
    ("r_devops_sre_senior", "j_security_senior", 1, E),
    ("r_data_senior", "j_ai_ml_senior", 2, E),
    ("r_ai_ml_senior", "j_data_senior", 2, E),
    ("r_ai_ml_senior", "j_robotics_senior", 1, E),
    ("r_robotics_senior", "j_ai_ml_senior", 2, E),
    ("r_robotics_senior", "j_embedded_senior", 2, E),
    ("r_embedded_senior", "j_robotics_senior", 2, E),
    ("r_embedded_senior", "j_semiconductor_senior", 2, E),
    ("r_semiconductor_senior", "j_embedded_senior", 2, E),
    ("r_quantum_senior", "j_backend_senior", 1, E),
    ("r_quantum_senior", "j_semiconductor_senior", 1, E),
    ("r_gaming_senior", "j_embedded_senior", 1, E),
    ("r_gaming_senior", "j_frontend_senior", 1, E),
    ("r_security_senior", "j_cloud_senior", 1, E),
    ("r_security_senior", "j_backend_senior", 1, E),
    ("r_mobile_senior", "j_frontend_senior", 2, E),
    ("r_frontend_senior", "j_mobile_senior", 1, E),

    # --- distant, and STILL ELIGIBLE -------------------------------------
    # This is the 2.1 rule under test. Two technical specialties the matrix
    # does not connect score `unrelated` (0.08, already severe) and stay
    # eligible. Setting `cross_domain` on any unrelated technology pair made
    # a software-and-cloud résumé ineligible for 522 of 2,500 live core-tech
    # postings; these rows are what stops that returning.
    ("r_frontend_senior", "j_semiconductor_senior", 0, E),
    ("r_semiconductor_senior", "j_frontend_senior", 0, E),
    ("r_gaming_senior", "j_data_senior", 0, E),
    ("r_data_senior", "j_gaming_senior", 0, E),
    ("r_mobile_senior", "j_quantum_senior", 0, E),
    ("r_quantum_senior", "j_mobile_senior", 0, E),
    ("r_frontend_senior", "j_devops_sre_senior", 0, E),
    ("r_semiconductor_senior", "j_ai_ml_senior", 0, E),
    ("r_robotics_senior", "j_frontend_senior", 0, E),
    ("r_security_senior", "j_gaming_senior", 0, E),
    ("r_embedded_senior", "j_data_senior", 0, E),
    ("r_cloud_senior", "j_mobile_senior", 0, E),
    ("r_ai_ml_senior", "j_frontend_senior", 0, E),
    ("r_backend_senior", "j_semiconductor_senior", 0, E),
    ("r_quantum_senior", "j_gaming_senior", 0, E),
    ("r_devops_sre_senior", "j_mobile_senior", 0, E),

    # --- quantum, the newest specialties, in both directions ---------------
    ("r_quantum_senior", "j_quantum_mid", 2, E),
    ("r_quantum_mid", "j_quantum_senior", 1, E),
    ("r_backend_senior", "j_quantum_senior", 0, E),
    ("r_semiconductor_senior", "j_quantum_mid", 1, E),
    ("r_embedded_senior", "j_quantum_mid", 1, E),

    # --- the two hard gates ------------------------------------------------
    ("r_security_cleared", "j_security_clearance", 3, E),
    ("r_security_cleared", "j_security_senior", 3, E),
    # NOT asserted `ineligible`, and that is the 2.1 rule rather than a
    # weakness: a résumé that does not MENTION a clearance is silent, and
    # silence is `unknown`. The gate fires on a KNOWN mismatch only. What the
    # verdict must do instead is SURFACE the requirement, and
    # `test_hard_requirements_are_surfaced_not_priced` asserts exactly that.
    ("r_security_senior", "j_security_clearance", 0, None),
    ("r_backend_senior", "j_security_clearance", 0, None),
    ("r_embedded_senior", "j_embedded_pe_license", 0, None),
    ("r_devops_sre_senior", "j_embedded_pe_license", 0, None),

    # --- seniority, in both directions, within one discipline --------------
    ("r_semiconductor_mid", "j_semiconductor_senior", 1, E),
    ("r_mobile_mid", "j_mobile_senior", 1, E),
    ("r_embedded_mid", "j_embedded_senior", 1, E),
    ("r_backend_senior", "j_backend_mid", 2, E),
    ("r_cloud_senior", "j_cloud_mid", 2, E),
    ("r_ai_ml_senior", "j_ai_ml_mid", 2, E),

    # --- the four real-shaped résumés --------------------------------------
    # Graded as a recruiter would: the discipline is what makes a row
    # relevant, and the seniority gap is what keeps a new grad's match to a
    # mid-level requisition at 2 rather than 3.
    ("r_newgrad_intern_titles", "j_data_mid", 2, E),
    ("r_newgrad_intern_titles", "j_ai_ml_mid", 2, E),
    ("r_newgrad_intern_titles", "j_data_senior", 1, E),
    ("r_newgrad_intern_titles", "j_ai_ml_senior", 1, E),
    ("r_newgrad_intern_titles", "j_frontend_senior", 0, E),
    ("r_newgrad_intern_titles", "j_semiconductor_senior", 0, E),
    ("r_newgrad_intern_titles", "j_gaming_senior", 0, E),

    ("r_backend_level_codes", "j_backend_mid", 3, E),
    ("r_backend_level_codes", "j_full_stack_senior", 2, E),
    ("r_backend_level_codes", "j_backend_senior", 2, E),
    ("r_backend_level_codes", "j_cloud_mid", 1, E),
    ("r_backend_level_codes", "j_gaming_senior", 0, E),
    ("r_backend_level_codes", "j_semiconductor_senior", 0, E),

    ("r_skills_only_unhelpful_titles", "j_devops_sre_senior", 2, E),
    ("r_skills_only_unhelpful_titles", "j_cloud_mid", 2, E),
    ("r_skills_only_unhelpful_titles", "j_cloud_senior", 2, E),
    ("r_skills_only_unhelpful_titles", "j_frontend_senior", 0, E),
    ("r_skills_only_unhelpful_titles", "j_quantum_senior", 0, E),
    ("r_skills_only_unhelpful_titles", "j_gaming_senior", 0, E),

    # Declared targets are the WEAKEST evidence and they are still evidence.
    # Grade 2 rather than 3 everywhere: this person has demonstrated nothing,
    # and a feed that ranked them equal to someone who has would be lying in
    # the other direction.
    ("r_newgrad_targets_and_tools", "j_ai_ml_mid", 2, E),
    ("r_newgrad_targets_and_tools", "j_data_mid", 2, E),
    ("r_newgrad_targets_and_tools", "j_ai_ml_senior", 1, E),
    ("r_newgrad_targets_and_tools", "j_semiconductor_senior", 0, E),
    ("r_newgrad_targets_and_tools", "j_frontend_senior", 0, E),
    ("r_newgrad_targets_and_tools", "j_gaming_senior", 0, E),
])

#: `{resume_id: {job_id: grade}}`, for the ranking metrics.
LABELS: Dict[str, Dict[str, int]] = {}
for _r, _j, _g, _e in SCENARIOS:
    LABELS.setdefault(_r, {})[_j] = _g

#: `{resume_id: {job_id: expected_eligibility}}`, only where asserted.
EXPECTED_ELIGIBILITY: Dict[str, Dict[str, str]] = {}
for _r, _j, _g, _e in SCENARIOS:
    if _e:
        EXPECTED_ELIGIBILITY.setdefault(_r, {})[_j] = _e

#: A grade at or above this is "relevant" for precision@k.
RELEVANT_AT = 2
