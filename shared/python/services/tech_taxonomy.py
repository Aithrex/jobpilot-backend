"""Versioned, multi-label technology-occupation taxonomy.

Why this module exists
----------------------
`fit_score.ROLE_FAMILIES` answers "which of 31 broad disciplines is this?" for
resume-to-job scoring. It is deliberately coarse and it is deliberately shared
with nursing, retail and hospitality, because that engine scores every seeker.

This module answers a different question, which the search product needs and
which nothing in the tree previously answered:

    Is this posting a SOFTWARE / TECHNICAL job, at ANY employer,
    and if so which specialty is it, and what is the EVIDENCE?

Those are different questions and conflating them is what produces the two
failures the product must not have:

* a Retail Store Manager at Google surfacing as a "tech job" because the
  employer is a technology company; and
* a Software Engineer at a hospital, a bank or a defence prime being excluded
  because the employer is not.

**Employer identity is not an input here.** `employer_tech_classifier` answers
the employer question separately, and the search layer composes the two. The
signature of `classify()` has no employer parameter on purpose: it cannot leak.

Design rules
------------
1. **Deterministic.** No LLM. Search must not run a model per posting, and a
   classification a user can see must be reproducible and explainable.
2. **Multi-label.** A "Senior ML Platform Engineer" is genuinely
   ai_ml/ml_platform AND cloud_platform/platform_engineering. Forcing one
   label loses recall in exactly the specialties that matter most.
3. **Evidence or nothing.** Every non-unknown verdict carries the strings that
   produced it (`classification_evidence`). A verdict with no evidence is a
   bug, not a default.
4. **Unknown beats a guess.** ADR-001's rule for dates applies to
   classification: an ambiguous title with no corroborating body returns
   `tech_scope="unknown"`, never `core`. Unknown rows are retrievable and
   rankable; they are simply not counted as core-tech precision.
5. **Versioned.** `TAXONOMY_VERSION` is stamped on every result and stored on
   every posting, so a rule change is a re-classification with an audit trail
   rather than a silent shift in what "tech job" means.

Occupation codes
----------------
`occupation_codes` carry O*NET-SOC 2019 codes. They are the public-domain
crosswalk between our specialties and the federal occupation system, which is
what makes DOL/BLS signals (wages, projections, LCA disclosure) joinable to a
posting later. Only codes that genuinely correspond to a specialty are mapped;
a specialty with no clean SOC equivalent carries none rather than the nearest
plausible one. A full O*NET title/software-skills bulk import is a follow-up
(see ADR-005 sec. 7) - it widens the alias table, it does not change the shape here.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

TAXONOMY_VERSION = "tech-tax-1.0.0"

# ---------------------------------------------------------------------------
# Scopes
# ---------------------------------------------------------------------------

SCOPE_CORE = "core"
SCOPE_ADJACENT = "adjacent"
SCOPE_NON_TECH = "non_tech"
SCOPE_UNKNOWN = "unknown"
SCOPES = (SCOPE_CORE, SCOPE_ADJACENT, SCOPE_NON_TECH, SCOPE_UNKNOWN)

# ---------------------------------------------------------------------------
# Families
# ---------------------------------------------------------------------------

F_SOFTWARE = "software_engineering"
F_AI_ML = "ai_ml"
F_DATA = "data"
F_CLOUD = "cloud_platform"
F_SECURITY = "security"
F_QUALITY = "quality"
F_TECH_LEADERSHIP = "technical_leadership"

F_TECHNICAL_PRODUCT = "technical_product"
F_SOLUTIONS_ENG = "solutions_engineering"
F_DEVREL = "developer_relations"
F_TECH_WRITING = "technical_writing"
F_UX_DESIGN_SYSTEMS = "ux_design_systems"
F_IT_SUPPORT = "it_support"
F_SEMICONDUCTOR = "semiconductor_hardware"

CORE_FAMILIES = (F_SOFTWARE, F_AI_ML, F_DATA, F_CLOUD, F_SECURITY, F_QUALITY,
                 F_TECH_LEADERSHIP)
ADJACENT_FAMILIES = (F_TECHNICAL_PRODUCT, F_SOLUTIONS_ENG, F_DEVREL,
                     F_TECH_WRITING, F_UX_DESIGN_SYSTEMS, F_IT_SUPPORT,
                     F_SEMICONDUCTOR)
ALL_FAMILIES = CORE_FAMILIES + ADJACENT_FAMILIES

FAMILY_LABELS: Dict[str, str] = {
    F_SOFTWARE: "Software Engineering",
    F_AI_ML: "AI / Machine Learning",
    F_DATA: "Data",
    F_CLOUD: "Cloud & Platform",
    F_SECURITY: "Security",
    F_QUALITY: "Quality Engineering",
    F_TECH_LEADERSHIP: "Technical Leadership",
    F_TECHNICAL_PRODUCT: "Technical Product",
    F_SOLUTIONS_ENG: "Solutions & Customer Engineering",
    F_DEVREL: "Developer Relations",
    F_TECH_WRITING: "Technical Writing",
    F_UX_DESIGN_SYSTEMS: "UX & Design Systems",
    F_IT_SUPPORT: "IT, Network & Support",
    F_SEMICONDUCTOR: "Semiconductor & Hardware Design",
}

FAMILY_SCOPE: Dict[str, str] = {f: SCOPE_CORE for f in CORE_FAMILIES}
FAMILY_SCOPE.update({f: SCOPE_ADJACENT for f in ADJACENT_FAMILIES})

# ---------------------------------------------------------------------------
# Specialty rules
#
# (family, specialty, [phrases]).  Phrases are matched word-boundary-wise
# against a normalized blob.  Order inside a family does not matter; the
# resolver scores every match.
#
# A phrase belongs here only if, ON ITS OWN IN A JOB TITLE, it is unambiguous.
# Anything that is not - "analyst", "consultant", "technical staff",
# "architect" alone - lives in _AMBIGUOUS_TITLE_RE and requires body evidence.
# ---------------------------------------------------------------------------

_SPEC_RULES: Tuple[Tuple[str, str, Tuple[str, ...]], ...] = (
    # ---- Software engineering ---------------------------------------------
    (F_SOFTWARE, "backend", (
        "backend engineer", "back end engineer", "back-end engineer",
        "backend developer", "back end developer", "backend software engineer",
        "server side engineer", "server-side engineer", "backend swe",
    )),
    (F_SOFTWARE, "frontend", (
        "frontend engineer", "front end engineer", "front-end engineer",
        "frontend developer", "front end developer", "front-end developer",
        "ui engineer", "javascript engineer", "react engineer",
        "react developer", "angular developer", "vue developer",
        "frontend software engineer", "client side engineer",
    )),
    (F_SOFTWARE, "full_stack", (
        "full stack engineer", "fullstack engineer", "full-stack engineer",
        "full stack developer", "fullstack developer", "full-stack developer",
        "full stack software engineer", "full stack swe",
    )),
    (F_SOFTWARE, "web_platform", (
        "web platform engineer", "web engineer", "web developer",
        "webmaster engineer", "web application developer",
        "web application engineer", "web infrastructure engineer",
    )),
    (F_SOFTWARE, "mobile", (
        "mobile engineer", "mobile developer", "ios engineer", "ios developer",
        "android engineer", "android developer", "swift developer",
        "kotlin developer", "react native developer", "react native engineer",
        "flutter developer", "mobile software engineer", "mobile application developer",
    )),
    (F_SOFTWARE, "desktop", (
        "desktop engineer", "desktop application developer",
        "desktop software engineer", "windows application developer",
        "macos engineer", "electron engineer",
    )),
    (F_SOFTWARE, "distributed_systems", (
        "distributed systems engineer", "distributed systems software engineer",
        "scalability engineer", "large scale systems engineer",
        "software systems engineer", "core systems engineer",
    )),
    (F_SOFTWARE, "systems_programming", (
        "systems programmer", "kernel engineer", "kernel developer",
        "operating systems engineer", "low level software engineer",
        "c\\+\\+ engineer", "c\\+\\+ developer", "rust engineer", "rust developer",
        "runtime engineer", "compiler engineer", "compiler developer",
        "toolchain engineer", "llvm engineer",
    )),
    (F_SOFTWARE, "apis", (
        "api engineer", "api developer", "api platform engineer",
        "integrations engineer", "integration software engineer",
        "middleware engineer",
    )),
    (F_SOFTWARE, "developer_tools", (
        "developer tools engineer", "developer productivity engineer",
        "developer experience engineer", "build systems engineer",
        "build engineer", "tooling engineer", "internal tools engineer",
        "developer infrastructure engineer",
    )),
    (F_SOFTWARE, "browsers", (
        "browser engineer", "rendering engineer", "webkit engineer",
        "chromium engineer", "layout engine engineer",
    )),
    (F_SOFTWARE, "games_xr", (
        "game engineer", "game developer", "gameplay engineer",
        "gameplay programmer", "game programmer", "engine programmer",
        "engine engineer", "graphics engineer", "graphics programmer",
        "rendering programmer", "unreal engineer", "unity engineer",
        "xr engineer", "ar engineer", "vr engineer", "shader programmer",
        "technical artist",
    )),
    (F_SOFTWARE, "embedded", (
        "embedded software engineer", "embedded systems engineer",
        "embedded engineer", "embedded developer", "embedded linux engineer",
        "rtos engineer", "device software engineer",
    )),
    (F_SOFTWARE, "firmware", (
        "firmware engineer", "firmware developer", "bsp engineer",
        "bios engineer", "device driver engineer", "driver developer",
        "firmware validation engineer",
    )),
    (F_SOFTWARE, "general_software", (
        "software engineer", "software developer", "software development engineer",
        "sde", "swe", "application developer", "applications developer",
        "programmer analyst", "computer programmer", "software programmer",
        "mts software",
        "software craftsperson", "polyglot engineer", "product engineer",
        "software engineering intern", "software development intern",
        "software engineering", "software development",
        "software engr", "software engineering lead", "development engineer software",
        "software development manager", "software engineering manager",
    )),

    # ---- AI / ML -----------------------------------------------------------
    (F_AI_ML, "ml_engineering", (
        "machine learning engineer", "ml engineer", "mle",
        "machine learning software engineer", "deep learning engineer",
        "machine learning developer",
    )),
    (F_AI_ML, "applied_ai", (
        "applied ai engineer", "ai engineer", "applied machine learning engineer",
        "applied scientist", "applied ai scientist", "ai developer",
        "artificial intelligence engineer",
    )),
    (F_AI_ML, "research_engineering", (
        "research engineer", "ai research engineer", "ml research engineer",
        "research software engineer",
    )),
    (F_AI_ML, "research_science", (
        "research scientist", "machine learning scientist", "ai scientist",
        "deep learning scientist", "principal research scientist",
    )),
    (F_AI_ML, "genai_llm", (
        "genai engineer", "generative ai engineer", "llm engineer",
        "large language model engineer", "foundation model engineer",
        "prompt engineer", "llm scientist", "generative ai scientist",
        "agent engineer", "ai agent engineer",
    )),
    (F_AI_ML, "nlp", (
        "nlp engineer", "natural language processing engineer", "nlp scientist",
        "computational linguist", "conversational ai engineer",
    )),
    (F_AI_ML, "speech", (
        "speech engineer", "speech scientist", "speech recognition engineer",
        "asr engineer", "text to speech engineer", "audio ml engineer",
    )),
    (F_AI_ML, "computer_vision", (
        "computer vision engineer", "cv engineer", "computer vision scientist",
        "image processing engineer", "perception engineer",
        "vision software engineer",
    )),
    (F_AI_ML, "recsys_search_ranking", (
        "recommendation engineer", "recommender systems engineer",
        "search engineer", "search relevance engineer", "ranking engineer",
        "personalization engineer", "search quality engineer",
        "relevance engineer", "search infrastructure engineer",
    )),
    (F_AI_ML, "robotics_autonomy", (
        "robotics engineer", "robotics software engineer", "autonomy engineer",
        "motion planning engineer", "slam engineer", "controls software engineer",
        "autonomous vehicle engineer", "self driving engineer",
    )),
    (F_AI_ML, "mlops_llmops", (
        "mlops engineer", "ml operations engineer", "llmops engineer",
        "ml infrastructure engineer", "machine learning infrastructure engineer",
        "model deployment engineer",
    )),
    (F_AI_ML, "ai_platform", (
        "ai platform engineer", "ml platform engineer",
        "machine learning platform engineer", "ai infrastructure engineer",
        "training infrastructure engineer",
    )),
    (F_AI_ML, "inference_performance", (
        "inference engineer", "model optimization engineer",
        "gpu engineer", "cuda engineer", "kernel optimization engineer",
        "performance ml engineer", "quantization engineer",
        "accelerator software engineer", "triton engineer",
    )),
    (F_AI_ML, "ai_safety", (
        "ai safety engineer", "ai safety researcher", "alignment researcher",
        "ai alignment engineer", "responsible ai engineer",
        "ai policy engineer", "trust and safety engineer",
    )),
    (F_AI_ML, "red_teaming", (
        "ai red team", "ai red teamer", "model red teaming",
        "adversarial testing engineer", "ai security researcher",
    )),
    (F_AI_ML, "evaluations", (
        "evaluation engineer", "model evaluation engineer", "evals engineer",
        "benchmarking engineer", "ai evaluation scientist",
    )),

    # ---- Data --------------------------------------------------------------
    (F_DATA, "data_engineering", (
        "data engineer", "big data engineer", "etl developer", "etl engineer",
        "elt engineer", "data pipeline engineer", "data integration engineer",
        "hadoop engineer", "spark engineer", "data engineering",
        "data engineering manager", "data warehouse engineer",
    )),
    (F_DATA, "data_platform", (
        "data platform engineer", "data infrastructure engineer",
        "lakehouse engineer", "data lake engineer", "data platform architect",
        "data infrastructure", "data platform",
    )),
    (F_DATA, "analytics_engineering", (
        "analytics engineer", "dbt engineer", "analytics developer",
    )),
    (F_DATA, "data_science", (
        "data scientist", "decision scientist", "quantitative researcher",
        "quantitative analyst", "statistician", "biostatistician",
        "product data scientist",
    )),
    (F_DATA, "business_intelligence", (
        "business intelligence engineer", "bi engineer", "bi developer",
        "business intelligence developer", "tableau developer",
        "power bi developer", "looker developer", "bi analyst",
        "business intelligence analyst", "reporting analyst",
        "analytics analyst", "data analyst",
    )),
    (F_DATA, "streaming", (
        "streaming engineer", "kafka engineer", "real time data engineer",
        "stream processing engineer", "flink engineer",
    )),
    (F_DATA, "database_engineering", (
        "database engineer", "database administrator", "dba",
        "database reliability engineer", "postgres engineer",
        "mysql engineer", "database developer", "sql developer",
        "database architect",
    )),
    (F_DATA, "data_governance", (
        "data governance engineer", "data governance analyst",
        "data quality engineer", "master data management",
        "data steward", "metadata engineer",
    )),
    (F_DATA, "ml_data", (
        "data annotation engineer", "training data engineer",
        "data curation engineer", "labeling operations engineer",
        "ml data engineer",
    )),

    # ---- Cloud / platform --------------------------------------------------
    (F_CLOUD, "cloud_engineering", (
        "cloud engineer", "cloud developer", "aws engineer", "azure engineer",
        "gcp engineer", "cloud solutions engineer", "cloud automation engineer",
        "cloud migration engineer", "cloud operations engineer",
        "cloud operations", "cloud infrastructure engineer",
        "cloud engineering", "cloud platform engineer", "cloud consultant",
        "cloud delivery architect",
    )),
    (F_CLOUD, "devops", (
        "devops engineer", "devops", "devsecops engineer", "devsecops",
        "ci cd engineer", "automation devops engineer", "gitops engineer",
    )),
    (F_CLOUD, "sre", (
        "site reliability engineer", "sre", "site reliability",
        "site reliability engineering",
        "reliability engineering manager", "systems reliability engineer",
        "service reliability engineer",
    )),
    (F_CLOUD, "platform_engineering", (
        "platform engineer", "platform software engineer",
        "internal developer platform engineer", "platform infrastructure engineer",
        "platform engineering",
    )),
    (F_CLOUD, "infrastructure", (
        "infrastructure engineer", "infrastructure software engineer",
        "systems engineer", "linux engineer", "unix engineer",
        "virtualization engineer", "storage engineer", "compute engineer",
        "datacenter engineer", "data center engineer",
        "infrastructure engineering", "systems operations engineer",
        "infrastructure architecture", "cloud architecture",
    )),
    (F_CLOUD, "kubernetes_containers", (
        "kubernetes engineer", "container platform engineer",
        "openshift engineer", "k8s engineer", "container engineer",
    )),
    (F_CLOUD, "production_engineering", (
        "production engineer", "production support engineer",
        "application reliability engineer",
    )),
    (F_CLOUD, "observability", (
        "observability engineer", "monitoring engineer",
        "telemetry engineer", "logging engineer",
    )),
    (F_CLOUD, "networking", (
        "network engineer", "network architect", "network software engineer",
        "network automation engineer", "sdn engineer", "network reliability engineer",
    )),
    (F_CLOUD, "cloud_security", (
        "cloud security engineer", "cloud security architect",
        "container security engineer", "kubernetes security engineer",
    )),
    (F_CLOUD, "finops", (
        "finops engineer", "cloud cost engineer", "cloud economics engineer",
        "finops analyst",
    )),

    # ---- Security ----------------------------------------------------------
    (F_SECURITY, "application_security", (
        "application security engineer", "appsec engineer", "appsec",
        "application security", "software security engineer",
        "secure code reviewer",
    )),
    (F_SECURITY, "product_security", (
        "product security engineer", "product security", "offensive security engineer",
        "security software engineer",
    )),
    (F_SECURITY, "iam_pam", (
        "iam engineer", "identity and access management engineer",
        "identity engineer", "pam engineer", "okta engineer",
        "privileged access management", "idp engineer", "icam engineer",
        "sailpoint engineer", "identity governance engineer",
        "identity and access management",
    )),
    (F_SECURITY, "secops", (
        "security operations engineer", "soc analyst", "security operations center",
        "secops engineer", "security engineer", "security analyst",
        "information security engineer", "information security analyst",
        "information security specialist", "information security manager",
        "cybersecurity engineer", "cybersecurity analyst", "infosec engineer",
        "cybersecurity specialist", "cybersecurity manager",
        "cyber security engineer", "cyber security analyst",
        "security operations analyst", "security engineering",
        "information security", "cybersecurity", "cyber security",
    )),
    (F_SECURITY, "detection_response", (
        "detection engineer", "detection and response engineer",
        "incident response analyst", "incident responder",
        "incident response specialist", "incident response engineer",
        "threat detection engineer", "threat hunter", "threat intelligence analyst",
        "dfir analyst", "security incident response",
    )),
    (F_SECURITY, "penetration_testing", (
        "penetration tester", "pen tester", "red team operator",
        "offensive security researcher", "ethical hacker",
    )),
    (F_SECURITY, "malware_reverse_engineering", (
        "malware analyst", "reverse engineer", "malware reverse engineer",
        "vulnerability researcher", "exploit developer",
    )),
    (F_SECURITY, "grc", (
        "security compliance analyst", "grc analyst", "grc engineer",
        "security governance analyst", "security risk analyst",
        "compliance engineer", "soc 2 analyst", "iso 27001 analyst",
    )),

    # ---- Quality -----------------------------------------------------------
    (F_QUALITY, "qa_automation", (
        "qa automation engineer", "automation qa engineer",
        "test automation engineer", "qa engineer", "quality assurance engineer",
        "qa automation", "automation test engineer", "qa developer",
        "qa analyst", "quality assurance analyst", "qa specialist",
        "quality assurance specialist", "qa manager", "quality assurance manager",
        "qa lead", "qa tester", "software tester", "test analyst",
        "quality assurance", "qa engineering",
    )),
    (F_QUALITY, "sdet", (
        "sdet", "software development engineer in test",
        "software engineer in test", "test development engineer",
    )),
    (F_QUALITY, "performance_testing", (
        "performance test engineer", "load test engineer",
        "performance qa engineer",
    )),
    (F_QUALITY, "test_infrastructure", (
        "test infrastructure engineer", "test platform engineer",
        "test tooling engineer", "quality infrastructure engineer",
    )),
    (F_QUALITY, "reliability_qa", (
        # Bare "Quality Engineer" is deliberately absent: on live inventory it
        # is overwhelmingly manufacturing/supplier QA, and the software ones
        # say so ("Software Quality Engineer").
        "quality engineer software", "release quality engineer",
        "software quality engineer", "software test engineer",
        "system test engineer", "systems test engineer",
        "test engineer software", "integration test engineer",
    )),

    # ---- Technical leadership ----------------------------------------------
    (F_TECH_LEADERSHIP, "engineering_management", (
        "engineering manager", "software engineering manager",
        "director of engineering", "engineering director",
        "vp of engineering", "vice president of engineering",
        "head of engineering", "cto", "chief technology officer",
        "chief technical officer", "senior engineering manager",
        "head of platform", "head of infrastructure", "head of data",
        "director of software engineering", "head of security engineering",
        "director of data engineering", "vp engineering",
    )),
    (F_TECH_LEADERSHIP, "architecture", (
        "software architect", "solution architect", "solutions architect",
        "enterprise architect", "application architect", "systems architect",
        "technical architect", "cloud architect", "data architect",
        "security architect", "principal architect",
    )),
    (F_TECH_LEADERSHIP, "principal_staff_ic", (
        # Bare level nouns ONLY. "Staff Software Engineer" must resolve to
        # software_engineering with seniority=staff, not to leadership: the
        # level is carried by `seniority`, and duplicating it as a family
        # empties the software family of its most senior postings.
        "staff engineer", "principal engineer", "distinguished engineer",
        "fellow engineer", "technical lead", "tech lead", "technical leader",
        "engineering lead", "development lead", "head of engineer",
    )),

    # ---- Adjacent ----------------------------------------------------------
    (F_TECHNICAL_PRODUCT, "technical_product_management", (
        "technical product manager", "product manager technical",
        "platform product manager", "api product manager",
        "developer product manager", "ai product manager",
        "data product manager", "infrastructure product manager",
    )),
    (F_TECHNICAL_PRODUCT, "tpm", (
        "technical program manager", "tpm", "technical project manager",
        "engineering program manager", "technical delivery manager",
    )),
    (F_SOLUTIONS_ENG, "solutions_engineering", (
        "solutions engineer", "solution engineer", "sales engineer",
        "pre sales engineer", "presales engineer", "field engineer software",
        "implementation engineer", "professional services engineer",
    )),
    (F_SOLUTIONS_ENG, "customer_engineering", (
        "customer engineer", "customer success engineer",
        "technical account manager", "support engineer software",
        "technical support engineer", "forward deployed engineer",
        "partner engineer", "deployment engineer", "integration consultant",
    )),
    (F_DEVREL, "developer_relations", (
        "developer advocate", "developer relations engineer",
        "developer relations", "devrel", "developer evangelist",
        "community engineer", "developer community manager",
    )),
    (F_TECH_WRITING, "technical_writing", (
        "technical writer", "documentation engineer", "docs engineer",
        "api documentation writer", "technical content engineer",
        "information developer",
    )),
    (F_UX_DESIGN_SYSTEMS, "design_systems", (
        # NOT bare "design engineer": measured on live inventory that reads
        # "Design Engineer-Electrical" and "Memory Design Engineer, HBM" as a
        # UX role. In industry a Design Engineer is a hardware/mechanical
        # title; the UX one always qualifies itself.
        "design systems engineer", "design technologist", "ux engineer",
        "frontend design engineer", "creative technologist",
        "ui platform engineer",
    )),
    (F_UX_DESIGN_SYSTEMS, "product_design", (
        "product designer", "ux designer", "ui designer",
        "user experience designer", "interaction designer", "ux researcher",
    )),
    (F_IT_SUPPORT, "it_support", (
        "it support", "help desk", "helpdesk", "service desk",
        "desktop support", "it technician", "it specialist",
        "technical support specialist", "it analyst", "end user support",
    )),
    (F_IT_SUPPORT, "it_systems_administration", (
        "systems administrator", "sysadmin", "system administrator",
        "windows administrator", "linux administrator",
        "active directory administrator", "m365 administrator",
        "salesforce administrator", "workday administrator",
    )),
    (F_IT_SUPPORT, "network_operations", (
        "network administrator", "noc technician", "noc engineer",
        "network operations", "network technician",
        "telecommunications technician",
    )),
    (F_SEMICONDUCTOR, "asic_soc_design", (
        "asic design engineer", "soc design engineer", "asic engineer",
        "chip design engineer", "silicon design engineer",
        "physical design engineer", "dft engineer", "design for test engineer",
    )),
    (F_SEMICONDUCTOR, "fpga_rtl", (
        "fpga engineer", "rtl design engineer", "rtl engineer",
        "verilog engineer", "vhdl engineer", "digital design engineer",
        "logic design engineer",
    )),
    (F_SEMICONDUCTOR, "verification", (
        "design verification engineer", "dv engineer",
        "verification engineer", "uvm engineer", "silicon validation engineer",
        "post silicon validation engineer",
    )),
    (F_SEMICONDUCTOR, "hardware_engineering", (
        "hardware engineer", "hardware design engineer",
        "board design engineer", "pcb design engineer",
        "signal integrity engineer", "analog design engineer",
        "mixed signal design engineer", "rf design engineer",
        "computer hardware engineer",
    )),
)

SPECIALTY_LABELS: Dict[str, str] = {
    "backend": "Backend", "frontend": "Frontend", "full_stack": "Full-Stack",
    "web_platform": "Web Platform", "mobile": "Mobile", "desktop": "Desktop",
    "distributed_systems": "Distributed Systems",
    "systems_programming": "Systems Programming", "apis": "APIs",
    "developer_tools": "Developer Tools", "browsers": "Browsers",
    "games_xr": "Games / XR", "embedded": "Embedded", "firmware": "Firmware",
    "general_software": "General Software",
    "ml_engineering": "ML Engineering", "applied_ai": "Applied AI",
    "research_engineering": "Research Engineering",
    "research_science": "Research Science", "genai_llm": "GenAI / LLMs",
    "nlp": "NLP", "speech": "Speech", "computer_vision": "Computer Vision",
    "recsys_search_ranking": "Recommender / Search / Ranking",
    "robotics_autonomy": "Robotics / Autonomy", "mlops_llmops": "MLOps / LLMOps",
    "ai_platform": "AI Platform",
    "inference_performance": "Inference / Performance",
    "ai_safety": "AI Safety", "red_teaming": "Red Teaming",
    "evaluations": "Evaluations",
    "data_engineering": "Data Engineering", "data_platform": "Data Platform",
    "analytics_engineering": "Analytics Engineering",
    "data_science": "Data Science",
    "business_intelligence": "Business Intelligence", "streaming": "Streaming",
    "database_engineering": "Database Engineering / Reliability",
    "data_governance": "Data Governance", "ml_data": "ML Data",
    "cloud_engineering": "Cloud Engineering", "devops": "DevOps", "sre": "SRE",
    "platform_engineering": "Platform Engineering",
    "infrastructure": "Infrastructure",
    "kubernetes_containers": "Kubernetes / Containers",
    "production_engineering": "Production Engineering",
    "observability": "Observability", "networking": "Networking",
    "cloud_security": "Cloud Security", "finops": "FinOps",
    "application_security": "Application Security",
    "product_security": "Product Security", "iam_pam": "IAM / PAM",
    "secops": "SecOps", "detection_response": "Detection & Response",
    "penetration_testing": "Penetration Testing",
    "malware_reverse_engineering": "Malware / Reverse Engineering", "grc": "GRC",
    "qa_automation": "QA Automation", "sdet": "SDET",
    "performance_testing": "Performance Testing",
    "test_infrastructure": "Test Infrastructure",
    "reliability_qa": "Software Quality",
    "engineering_management": "Engineering Management",
    "architecture": "Architecture", "principal_staff_ic": "Principal / Staff IC",
    "technical_product_management": "Technical Product Management", "tpm": "TPM",
    "solutions_engineering": "Solutions Engineering",
    "customer_engineering": "Customer Engineering",
    "developer_relations": "Developer Relations",
    "technical_writing": "Technical Writing",
    "design_systems": "Design Systems", "product_design": "Product Design",
    "it_support": "IT Support",
    "it_systems_administration": "IT Systems Administration",
    "network_operations": "Network Operations",
    "asic_soc_design": "ASIC / SoC Design", "fpga_rtl": "FPGA / RTL",
    "verification": "Design Verification",
    "hardware_engineering": "Hardware Engineering",
}

#: Phrases that genuinely belong to TWO families at once.
#:
#: Overlap suppression (`_dedupe_overlapping`) exists because "Design Systems
#: Engineer" contains "systems engineer" and reading the substring filed a UX
#: role under Cloud & Platform. But the same mechanism drops the SECOND, real
#: label from "Machine Learning Platform Engineer", which is honestly both
#: ai_ml/ai_platform and cloud_platform/platform_engineering.
#:
#: There is no syntactic rule that separates "a second true label" from "a
#: misreading of the same words", so the dual-family cases are DECLARED here
#: rather than derived. Applied AFTER suppression, so a declaration adds a
#: label and can never resurrect a suppressed misreading.
_CROSS_LISTED: Tuple[Tuple[str, str, str], ...] = (
    ("machine learning platform engineer", F_CLOUD, "platform_engineering"),
    ("ml platform engineer", F_CLOUD, "platform_engineering"),
    ("ai platform engineer", F_CLOUD, "platform_engineering"),
    ("ml infrastructure engineer", F_CLOUD, "infrastructure"),
    ("machine learning infrastructure engineer", F_CLOUD, "infrastructure"),
    ("ai infrastructure engineer", F_CLOUD, "infrastructure"),
    ("mlops engineer", F_CLOUD, "devops"),
    ("llmops engineer", F_CLOUD, "devops"),
    ("data platform engineer", F_CLOUD, "platform_engineering"),
    ("data infrastructure engineer", F_CLOUD, "infrastructure"),
    ("cloud security engineer", F_SECURITY, "cloud_security"),
    ("cloud security architect", F_SECURITY, "cloud_security"),
    ("container security engineer", F_SECURITY, "cloud_security"),
    ("kubernetes security engineer", F_SECURITY, "cloud_security"),
    ("devsecops engineer", F_SECURITY, "application_security"),
    ("security software engineer", F_SOFTWARE, "general_software"),
    ("database reliability engineer", F_CLOUD, "sre"),
    ("analytics engineer", F_DATA, "data_engineering"),
    ("test infrastructure engineer", F_CLOUD, "platform_engineering"),
    ("developer platform engineer", F_SOFTWARE, "developer_tools"),
    ("internal developer platform engineer", F_SOFTWARE, "developer_tools"),
)

#: {family: (specialty, ...)} in declaration order - the UI's filter tree.
FAMILY_SPECIALTIES: Dict[str, Tuple[str, ...]] = {}
for _fam, _spec, _ in _SPEC_RULES:
    FAMILY_SPECIALTIES.setdefault(_fam, tuple())
    if _spec not in FAMILY_SPECIALTIES[_fam]:
        FAMILY_SPECIALTIES[_fam] = FAMILY_SPECIALTIES[_fam] + (_spec,)

ALL_SPECIALTIES: Tuple[str, ...] = tuple(
    s for f in ALL_FAMILIES for s in FAMILY_SPECIALTIES.get(f, ())
)

# ---------------------------------------------------------------------------
# O*NET-SOC 2019 crosswalk.  Only clean correspondences; a specialty with no
# genuine SOC equivalent carries none rather than the nearest plausible one.
# ---------------------------------------------------------------------------

SPECIALTY_OCCUPATION_CODES: Dict[str, Tuple[str, ...]] = {
    "backend": ("15-1252.00",), "frontend": ("15-1254.00", "15-1252.00"),
    "full_stack": ("15-1252.00", "15-1254.00"),
    "web_platform": ("15-1254.00",), "mobile": ("15-1252.00",),
    "desktop": ("15-1252.00",), "distributed_systems": ("15-1252.00",),
    "systems_programming": ("15-1252.00", "15-1251.00"),
    "apis": ("15-1252.00",), "developer_tools": ("15-1252.00",),
    "browsers": ("15-1252.00",), "games_xr": ("15-1252.00",),
    "embedded": ("15-1252.00", "17-2061.00"),
    "firmware": ("15-1252.00", "17-2061.00"),
    "general_software": ("15-1252.00", "15-1251.00"),
    "ml_engineering": ("15-2051.00", "15-1252.00"),
    "applied_ai": ("15-2051.00",), "research_engineering": ("15-1221.00",),
    "research_science": ("15-1221.00", "19-1029.00"),
    "genai_llm": ("15-2051.00",), "nlp": ("15-2051.00",),
    "speech": ("15-2051.00",), "computer_vision": ("15-2051.00",),
    "recsys_search_ranking": ("15-2051.00", "15-1252.00"),
    "robotics_autonomy": ("17-2199.08", "15-1252.00"),
    "mlops_llmops": ("15-1244.00", "15-1252.00"),
    "ai_platform": ("15-1252.00",),
    "inference_performance": ("15-1252.00",),
    "ai_safety": ("15-1212.00",), "red_teaming": ("15-1212.00",),
    "evaluations": ("15-2051.00",),
    "data_engineering": ("15-2051.00", "15-1243.00"),
    "data_platform": ("15-1243.00",),
    "analytics_engineering": ("15-2051.01",),
    "data_science": ("15-2051.00",),
    "business_intelligence": ("15-2051.01",),
    "streaming": ("15-1243.00",),
    "database_engineering": ("15-1242.00", "15-1243.00"),
    "data_governance": ("15-1299.00",), "ml_data": ("15-2051.00",),
    "cloud_engineering": ("15-1241.00", "15-1244.00"),
    "devops": ("15-1252.00", "15-1244.00"),
    "sre": ("15-1244.00", "15-1252.00"),
    "platform_engineering": ("15-1252.00", "15-1244.00"),
    "infrastructure": ("15-1244.00",),
    "kubernetes_containers": ("15-1244.00",),
    "production_engineering": ("15-1244.00",),
    "observability": ("15-1244.00",),
    "networking": ("15-1241.00", "15-1244.00"),
    "cloud_security": ("15-1212.00",), "finops": ("15-1299.00",),
    "application_security": ("15-1212.00",),
    "product_security": ("15-1212.00",), "iam_pam": ("15-1212.00",),
    "secops": ("15-1212.00",), "detection_response": ("15-1212.00",),
    "penetration_testing": ("15-1212.00",),
    "malware_reverse_engineering": ("15-1212.00",), "grc": ("13-1041.00",),
    "qa_automation": ("15-1253.00",), "sdet": ("15-1253.00",),
    "performance_testing": ("15-1253.00",),
    "test_infrastructure": ("15-1253.00",),
    "reliability_qa": ("15-1253.00",),
    "engineering_management": ("11-3021.00",),
    "architecture": ("15-1299.08", "15-1241.00"),
    "principal_staff_ic": ("15-1252.00",),
    "technical_product_management": ("11-3021.00", "15-1299.00"),
    "tpm": ("11-3021.00", "13-1082.00"),
    "solutions_engineering": ("41-9031.00", "15-1299.00"),
    "customer_engineering": ("15-1232.00",),
    "developer_relations": ("15-1299.00",),
    "technical_writing": ("27-3042.00",),
    "design_systems": ("15-1255.00",), "product_design": ("27-1024.00",),
    "it_support": ("15-1232.00", "15-1231.00"),
    "it_systems_administration": ("15-1244.00",),
    "network_operations": ("15-1231.00", "15-1244.00"),
    "asic_soc_design": ("17-2061.00", "17-2072.00"),
    "fpga_rtl": ("17-2061.00",), "verification": ("17-2061.00",),
    "hardware_engineering": ("17-2061.00", "17-2072.00"),
}


# ---------------------------------------------------------------------------
# Non-tech vetoes
#
# A posting whose TITLE names one of these is a nontechnical occupation. It
# stays non_tech even when the employer is a technology company - that is the
# whole point of separating occupation from employer classification.
#
# Every phrase here had to be unambiguous IN A TITLE. "Analyst" is not here
# (Security Analyst, Data Analyst); "Financial Analyst" is. "Manager" is not
# here; "Store Manager" is.
# ---------------------------------------------------------------------------

_NON_TECH_RULES: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("clinical", (
        "registered nurse", "nurse practitioner", "licensed practical nurse",
        "\\brn\\b", "\\blpn\\b", "\\bcna\\b", "physician", "surgeon",
        "nursing assistant", "medical assistant", "phlebotomist",
        "radiologic technologist", "respiratory therapist", "pharmacist",
        "pharmacy technician", "physical therapist", "occupational therapist",
        "speech language pathologist", "dental hygienist", "dentist",
        "veterinarian", "paramedic", "emt", "sonographer", "anesthesiologist",
        "patient care technician", "clinical coordinator", "care manager",
        "behavioral health technician", "social worker", "case manager",
        "medical technologist", "surgical technologist", "midwife",
        "chiropractor", "optometrist", "psychologist", "psychiatrist",
        "clinical research coordinator", "home health aide", "caregiver",
    )),
    ("retail_food_hospitality", (
        "store manager", "assistant store manager", "shift supervisor",
        "sales associate", "retail associate", "cashier", "stocker",
        "merchandiser", "barista", "cook", "line cook", "chef", "server",
        "bartender", "dishwasher", "restaurant manager", "general manager restaurant",
        "housekeeper", "housekeeping", "front desk agent", "hotel manager",
        "delivery driver", "crew member", "team member", "shift leader",
        "food service", "grocery clerk", "produce clerk", "deli clerk",
        "meat cutter", "baker", "pharmacy clerk", "loss prevention",
        "assistant manager retail",
    )),
    ("trades_logistics", (
        "truck driver", "cdl driver", "forklift operator", "warehouse associate",
        "warehouse worker", "material handler", "picker packer", "loader",
        "electrician", "plumber", "hvac technician", "welder", "machinist",
        "millwright", "maintenance technician", "facilities technician",
        "janitor", "custodian", "landscaper", "carpenter", "painter",
        "assembler", "production associate", "production operator",
        "machine operator", "quality inspector", "sanitation",
        "diesel mechanic", "automotive technician", "aircraft mechanic",
        "construction worker", "heavy equipment operator", "roofer",
        "pipefitter", "boilermaker", "ironworker", "rigger", "surveyor",
    )),
    ("business_admin", (
        "accountant", "accounts payable", "accounts receivable", "bookkeeper",
        "payroll specialist", "payroll administrator", "tax analyst",
        "tax manager", "auditor", "financial analyst", "finance manager",
        "controller", "treasury analyst", "credit analyst", "underwriter",
        "loan officer", "mortgage", "claims adjuster", "claims examiner",
        "insurance agent", "actuary",
        "human resources", "\\bhr\\b", "recruiter", "talent acquisition",
        "benefits specialist", "compensation analyst", "hris analyst",
        "executive assistant", "administrative assistant", "office manager",
        "receptionist", "office coordinator", "data entry clerk",
        "procurement specialist", "buyer", "sourcing manager",
        "supply chain analyst", "logistics coordinator",
        "paralegal", "attorney", "legal counsel", "compliance officer",
        "contracts manager", "legal assistant",
    )),
    ("sales_marketing_support", (
        "account executive", "sales representative", "sales manager",
        "business development representative", "\\bbdr\\b", "\\bsdr\\b",
        "sales development representative", "inside sales", "outside sales",
        "territory manager", "regional sales", "account manager sales",
        "marketing manager", "marketing specialist", "brand manager",
        "content marketing", "social media manager", "seo specialist",
        "digital marketing", "demand generation", "growth marketing",
        "public relations", "communications manager", "copywriter",
        "event coordinator", "customer service representative",
        "call center", "customer care", "collections specialist",
        "client services associate", "customer success manager",
        "member services", "patient access representative",
    )),
    ("education_public", (
        "teacher", "teaching assistant", "professor", "lecturer",
        "instructor", "school counselor", "principal school",
        "librarian", "coach", "athletic", "tutor", "childcare",
        "police officer", "firefighter", "correctional officer",
        "security guard", "security officer", "dispatcher",
        "park ranger", "social services specialist",
    )),
    ("physical_engineering", (
        "mechanical engineer", "civil engineer", "structural engineer",
        "chemical engineer", "industrial engineer", "manufacturing engineer",
        "process engineer", "environmental engineer", "petroleum engineer",
        "mining engineer", "nuclear engineer", "materials engineer",
        "metallurgical engineer", "geotechnical engineer",
        "electrical engineer power", "power systems engineer",
        "hvac engineer", "plant engineer", "corrosion engineer",
        "welding engineer", "packaging engineer", "agricultural engineer",
        "marine engineer", "mechanical design engineer",
        "facilities engineer", "reliability engineer maintenance",
    )),
)

_NON_TECH_CATEGORY: Dict[str, str] = {}
for _cat, _phrases in _NON_TECH_RULES:
    for _p in _phrases:
        _NON_TECH_CATEGORY[_p] = _cat


def _compile_alternation(phrases: Iterable[str]) -> re.Pattern:
    """One word-boundary alternation, longest-first so the most specific wins.

    Phrases may already contain `\\b` (for bare acronyms like `rn`); those are
    passed through unescaped. Everything else is escaped, and whitespace in a
    phrase matches any run of whitespace/hyphen so "front end" also matches
    "front-end" and "front  end".
    """
    parts: List[str] = []
    for p in sorted(set(phrases), key=len, reverse=True):
        if "\\b" in p or "\\+" in p:
            body = p
        else:
            body = re.escape(p).replace(r"\ ", r"[\s\-/]+")
        parts.append(body)
    # Trailing `s`/`es` is absorbed so "large language models",
    # "microservices" and "data pipelines" match their singular rule. It is a
    # SUFFIX on the whole phrase, so it cannot make a shorter word match a
    # longer one.
    return re.compile(r"(?<![A-Za-z0-9_])(?:" + "|".join(parts) + r")(?:es|s)?(?![A-Za-z0-9_])",
                      re.IGNORECASE)


#: `<technology> Developer|Engineer|Programmer|Architect`.  Measured on live
#: inventory this is the single biggest recall gap in the enumerated rules:
#: "Python Developer", "Senior SAP ABAP Engineer", "Appian Developer",
#: "ServiceNow Developer", "Java Developer - FI Risk" are all real software
#: postings that name no role noun the phrase table knows. Enumerating every
#: `<stack> <noun>` product would be thousands of rows; one composed pattern
#: per destination family is the same rule, written once.
#: `specialist` is deliberately NOT a stack role noun. It is the weakest of
#: the nouns and it is what nontechnical roles use: "Data Annotation
#: Specialist", "Security Specialist" (a guard), "IT Specialist". Including it
#: put a labelling role into the data family.
_STACK_ROLE_NOUNS = r"(?:developer|engineer|programmer|architect)"

def _stack_rule(tokens: Sequence[str], family: str, specialty: str
                ) -> Tuple[str, str, re.Pattern]:
    body = "|".join(tokens)
    return (family, specialty, re.compile(
        r"(?<![A-Za-z0-9_])(?:" + body + r")\s+(?:\w+\s+){0,1}" + _STACK_ROLE_NOUNS
        + r"(?![A-Za-z0-9_])", re.IGNORECASE))


_STACK_PATTERNS: List[Tuple[str, str, re.Pattern]] = [
    _stack_rule((
        r"python", r"java", r"javascript", r"typescript", r"golang", r"go(?:lang)?",
        r"c\+\+", r"c#", r"\.net", r"dotnet", r"ruby", r"php", r"scala", r"kotlin",
        r"swift", r"rust", r"perl", r"delphi", r"cobol", r"mainframe",
        r"node(?:\.?js)?", r"react(?:\.?js)?", r"angular", r"vue(?:\.?js)?",
        r"django", r"rails", r"spring", r"laravel", r"symfony",
        r"abap", r"sap abap", r"apex", r"salesforce", r"servicenow", r"appian",
        r"pega", r"sharepoint", r"dynamics 365", r"netsuite", r"workday integration",
        r"oracle apex", r"peoplesoft", r"drupal", r"wordpress", r"magento",
        r"shopify", r"api", r"backend", r"frontend", r"full[\s-]?stack",
        r"web", r"mobile", r"ios", r"android", r"embedded", r"firmware",
        r"blockchain", r"solidity", r"web3", r"unity", r"unreal",
    ), F_SOFTWARE, "general_software"),
    _stack_rule((
        r"aws", r"azure", r"gcp", r"google cloud", r"kubernetes", r"k8s",
        r"terraform", r"ansible", r"openshift", r"vmware", r"citrix",
        r"linux", r"unix", r"windows server", r"cisco", r"network",
        r"devops", r"cloud", r"infrastructure", r"platform", r"observability",
    ), F_CLOUD, "cloud_engineering"),
    _stack_rule((
        r"sql", r"pl[\s/]?sql", r"t[\s-]?sql", r"snowflake", r"databricks",
        r"informatica", r"talend", r"etl", r"elt", r"hadoop", r"spark",
        r"teradata", r"bigquery", r"redshift", r"ssis", r"ssrs",
        r"data warehouse", r"data lake", r"data",
    ), F_DATA, "data_engineering"),
    _stack_rule((
        r"tableau", r"power ?bi", r"looker", r"qlik", r"cognos",
        r"business intelligence",
    ), F_DATA, "business_intelligence"),
    _stack_rule((
        r"pytorch", r"tensorflow", r"llm", r"genai", r"generative ai",
        r"machine learning", r"deep learning", r"nlp", r"computer vision",
        r"artificial intelligence", r"ai/ml", r"mlops",
    ), F_AI_ML, "ml_engineering"),
    _stack_rule((
        r"siem", r"splunk security", r"crowdstrike", r"sentinel", r"zscaler",
        r"iam", r"pki", r"soc", r"vulnerability", r"penetration",
    ), F_SECURITY, "secops"),
]

_SPEC_PATTERNS: List[Tuple[str, str, re.Pattern]] = [
    (fam, spec, _compile_alternation(phrases)) for fam, spec, phrases in _SPEC_RULES
] + _STACK_PATTERNS
_NON_TECH_RE = _compile_alternation(_NON_TECH_CATEGORY.keys())

# Titles that name no discipline. Historically these were either dropped
# (recall loss on graduate programmes) or defaulted into software (precision
# loss). Both are wrong: they require body/department/skills evidence, and
# they return `unknown` when there is none.
_AMBIGUOUS_TITLE_RE = _compile_alternation((
    "technology analyst", "technical analyst", "technology associate",
    "technical associate", "technology consultant", "technical consultant",
    "solutions consultant", "solution consultant", "business technology analyst",
    "member of technical staff", "technical staff",
    "associate consultant", "analyst", "consultant", "associate",
    "specialist", "coordinator", "engineer", "engineer i", "engineer ii",
    "engineer iii", "developer", "architect", "technologist", "technician",
    "graduate program", "graduate programme", "graduate scheme",
    "rotational program", "leadership development program",
    "early career program", "apprentice", "trainee", "intern",
    "operations analyst", "systems analyst", "product analyst",
    # Each of these is software in one industry and physical plant in another.
    # Measured live: "Automation Engineer" is both test automation and
    # industrial PLC work; "Quality Engineer" is overwhelmingly supplier QA;
    # "Security Specialist" is overwhelmingly a guard ("Target Security
    # Specialist", 35 rows in one 600-row sample).
    "automation engineer", "reliability engineer", "quality engineer",
    "test engineer", "application engineer", "applications engineer",
    "development engineer", "validation engineer", "security specialist",
    "security manager", "field engineer", "process engineer",
    "controls engineer", "integration engineer", "release engineer",
    "project manager", "program manager", "product manager",
    "digital analyst", "innovation analyst", "research associate",
    "scientist", "researcher", "manager", "lead", "director",
))

# Body/skills tokens that positively evidence software/technical work. Used to
# resolve ambiguous titles and to raise confidence on title-only matches.
_TECH_EVIDENCE_TERMS: Tuple[str, ...] = (
    "python", "java", "javascript", "typescript", "golang", "\\bgo\\b", "rust",
    "c\\+\\+", "\\bc#\\b", "\\.net", "ruby", "\\bphp\\b", "scala", "kotlin",
    "swift", "objective-c", "perl", "\\br\\b programming", "matlab",
    "react", "angular", "vue", "svelte", "next\\.js", "node\\.js", "nodejs",
    "django", "flask", "fastapi", "spring boot", "rails", "laravel",
    "graphql", "rest api", "restful", "grpc", "microservices", "api design",
    "sql", "nosql", "postgresql", "postgres", "mysql", "mongodb", "redis",
    "cassandra", "dynamodb", "elasticsearch", "snowflake", "databricks",
    "bigquery", "redshift", "clickhouse",
    "aws", "azure", "\\bgcp\\b", "google cloud", "kubernetes", "\\bk8s\\b",
    "docker", "terraform", "ansible", "helm", "jenkins", "gitlab ci",
    "github actions", "circleci", "argocd", "prometheus", "grafana",
    "datadog", "splunk", "opentelemetry", "serverless", "lambda function",
    "ci/cd", "\\bci cd\\b", "infrastructure as code",
    "pytorch", "tensorflow", "scikit-learn", "hugging face", "transformers",
    "\\bllm\\b", "large language model", "machine learning", "deep learning",
    "neural network", "\\bnlp\\b", "computer vision", "mlflow", "kubeflow",
    "sagemaker", "vertex ai", "\\brag\\b", "embeddings", "vector database",
    "spark", "hadoop", "kafka", "airflow", "dbt", "flink", "etl pipeline",
    "data pipeline", "data warehouse", "data modeling",
    "git", "github", "version control", "unit test", "integration test",
    "test automation", "selenium", "cypress", "playwright", "pytest", "junit",
    "jira", "agile", "scrum", "code review", "pull request",
    "linux", "unix", "bash", "shell script", "powershell",
    "distributed system", "scalab", "latency", "throughput", "concurrency",
    "software development life cycle", "\\bsdlc\\b", "object oriented",
    "algorithm", "data structure", "design pattern", "system design",
    "penetration test", "vulnerability", "\\bsiem\\b", "\\bsoar\\b",
    "owasp", "threat model", "encryption", "\\boauth\\b", "\\bsaml\\b",
    "verilog", "vhdl", "\\brtl\\b", "\\bfpga\\b", "\\basic\\b", "systemverilog",
    "embedded c", "\\brtos\\b", "firmware", "device driver", "\\bi2c\\b", "\\bspi\\b",
    "apex", "salesforce", "lightning component", "visualforce",
    "abap", "sap hana", "servicenow", "sharepoint", "power apps", "powerapps",
    "power automate", "mulesoft", "boomi", "informatica", "talend",
    "oracle apex", "pl/sql", "\\bssis\\b", "\\bssrs\\b", "\\bcobol\\b",
    "mainframe", "\\bjcl\\b", "\\bcics\\b", "\\bsas\\b programming",
    "computer science", "software engineering degree",
)
_TECH_EVIDENCE_RE = _compile_alternation(_TECH_EVIDENCE_TERMS)

# Departments that make a technical reading of an ambiguous title credible.
_TECH_DEPARTMENT_RE = _compile_alternation((
    "engineering", "software", "technology", "information technology",
    "\\bit\\b", "data", "analytics", "research and development", "\\br&d\\b",
    "platform", "infrastructure", "cloud", "security", "cyber",
    "product engineering", "digital", "machine learning", "artificial intelligence",
    "quality engineering", "devops", "developer", "computing", "systems",
))

# A handful of ambiguous titles DO name a family once technicality has been
# established by the body - they are unspecific about the discipline, not about
# the function. Used only on the body-evidence path, never to bypass it.
_AMBIGUOUS_FAMILY_HINTS: Tuple[Tuple[re.Pattern, str, str], ...] = (
    (_compile_alternation(("solutions consultant", "solution consultant",
                           "technical consultant", "technology consultant",
                           "implementation consultant", "functional consultant")),
     F_SOLUTIONS_ENG, "solutions_engineering"),
    (_compile_alternation(("technical program manager", "technical project manager",
                           "engineering program manager")),
     F_TECHNICAL_PRODUCT, "tpm"),
    (_compile_alternation(("technologist", "technician")),
     F_IT_SUPPORT, "it_support"),
)

# A physical/plant qualifier vetoes a software reading of a role NOUN. Mirrors
# `fit_score._PHYSICAL_QUALIFIER_RE`, deliberately duplicated rather than
# imported so a tuning change to one engine cannot silently move the other.
_PHYSICAL_QUALIFIER_RE = re.compile(
    r"(?<![a-z])("
    r"corrosion|propulsion|geotechnical|metallurg\w*|welding|hvac|"
    r"mechanical|structural|civil|chemical|petroleum|pipeline|drilling|"
    r"refinery|plant|generator|turbine|boiler|instrumentation|"
    r"power electronics|power systems|power generation|power distribution|"
    r"electrical grid|substation|electromechanical|"
    r"wastewater|water treatment|hydraulic|pneumatic|"
    r"machining|tooling|airframe|automotive body|"
    r"textile|agricultural|marine|mining|nuclear|"
    r"facilities|janitorial|calibration"
    r")(?![a-z])", re.IGNORECASE)

# Semiconductor/hardware words are NOT a physical veto - they are the
# `semiconductor_hardware` adjacent family, which is selectable.
_HARDWARE_CONTEXT_RE = _compile_alternation((
    "semiconductor", "wafer", "silicon", "chip", "\\basic\\b", "\\bfpga\\b",
    "\\brtl\\b", "verilog", "vhdl", "tape out", "tapeout", "\\bpcb\\b",
    "signal integrity", "analog", "mixed signal", "\\brf\\b",
))

# Seniority. Title-first, then explicit year requirements from the body.
_SENIORITY_ORDER = ("intern", "entry", "mid", "senior", "staff", "principal",
                    "director", "executive")
_SENIORITY_RULES: Tuple[Tuple[str, re.Pattern], ...] = (
    ("intern", _compile_alternation(("intern", "internship", "co-op", "coop student",
                                     "apprentice", "student worker", "summer analyst"))),
    ("executive", _compile_alternation(("chief technology officer", "cto", "ciso",
                                        "chief information officer", "cio",
                                        "chief data officer", "chief architect",
                                        "svp", "senior vice president",
                                        "vice president", "\\bvp\\b", "head of"))),
    ("director", _compile_alternation(("director", "senior director",
                                       "sr director", "group manager"))),
    ("principal", _compile_alternation(("principal", "distinguished", "fellow",
                                        "senior principal", "architect"))),
    ("staff", _compile_alternation(("staff", "senior staff", "lead engineer",
                                    "tech lead", "technical lead", "team lead"))),
    ("senior", _compile_alternation(("senior", "\\bsr\\b", "\\bsr\\.", "\\biii\\b",
                                     "\\biv\\b", "level 5", "\\bl5\\b", "\\bl6\\b"))),
    ("entry", _compile_alternation(("junior", "\\bjr\\b", "entry level",
                                    "associate engineer", "graduate engineer",
                                    "new grad", "early career", "\\bi\\b",
                                    "level 1", "\\bl1\\b", "trainee"))),
)
_YEARS_RE = re.compile(
    r"(\d{1,2})\s*(?:\+|plus)?\s*(?:-|to|–)?\s*(\d{1,2})?\s*\+?\s*years?"
    r"(?:\s+of)?(?:\s+(?:relevant|professional|related|industry|hands[\s-]?on|"
    r"work|practical))?\s+experience", re.IGNORECASE)


@dataclass
class TechClassification:
    """One posting's occupation verdict. Every field is reproducible."""
    tech_scope: str = SCOPE_UNKNOWN
    role_family: Optional[str] = None
    role_families: List[str] = field(default_factory=list)
    role_specialties: List[str] = field(default_factory=list)
    normalized_title: str = ""
    occupation_codes: List[str] = field(default_factory=list)
    seniority: str = "unknown"
    required_years_min: Optional[int] = None
    required_years_max: Optional[int] = None
    skills_required: List[str] = field(default_factory=list)
    skills_preferred: List[str] = field(default_factory=list)
    classification_confidence: float = 0.0
    classification_evidence: List[Dict[str, str]] = field(default_factory=list)
    taxonomy_version: str = TAXONOMY_VERSION

    def to_doc(self) -> Dict[str, Any]:
        """The subdocument stored on `job_postings.tech` (Mongo-safe)."""
        return {
            "tech_scope": self.tech_scope,
            "role_family": self.role_family,
            "role_families": list(self.role_families),
            "role_specialties": list(self.role_specialties),
            "normalized_title": self.normalized_title,
            "occupation_codes": list(self.occupation_codes),
            "seniority": self.seniority,
            "required_years_min": self.required_years_min,
            "required_years_max": self.required_years_max,
            "skills_required": list(self.skills_required),
            "skills_preferred": list(self.skills_preferred),
            "classification_confidence": round(float(self.classification_confidence), 3),
            "classification_evidence": list(self.classification_evidence),
            "taxonomy_version": self.taxonomy_version,
        }


def _norm(text: Any) -> str:
    """Lowercase, punctuation-flattened, single-spaced."""
    s = str(text or "").lower()
    s = s.replace("&", " and ")
    s = re.sub(r"[‐-―]", "-", s)
    s = re.sub(r"[^a-z0-9+#./\-\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def normalize_title(title: Any, *, max_length: int = 160) -> str:
    """Display title stripped of req ids, location suffixes and shift noise.

    Kept separate from `job_inventory_service.normalize_title` (a slug) - this
    one stays human-readable because it is what the card shows and what the
    dedupe grouper compares.
    """
    s = str(title or "").strip()
    if not s:
        return ""
    s = re.sub(r"\s*[\(\[\{][^)\]\}]*(?:req|job|requisition|id|#)\s*[:#]?\s*[\w-]+[\)\]\}]",
               " ", s, flags=re.IGNORECASE)
    s = re.sub(r"\b(?:req|job|requisition)\s*(?:id|#|no\.?)?\s*[:#]?\s*[A-Za-z]{0,4}[-_]?\d{3,}\b",
               " ", s, flags=re.IGNORECASE)
    # A bracketed token that is ALL requisition id and no words - "[R-99881]",
    # "(JR10022494)", "[2024-1187]". Boards glue these on and they make an
    # otherwise identical role look like a different one to every grouper.
    s = re.sub(r"[\(\[\{]\s*[A-Za-z]{0,4}[-_ ]?\d{3,}[-_ ]?\d{0,6}\s*[\)\]\}]",
               " ", s)
    s = re.sub(r"[\(\[]\s*(?:remote|hybrid|onsite|on-site|us|usa|united states|"
               r"full[\s-]?time|part[\s-]?time|contract|w2|c2c)\s*[\)\]]",
               " ", s, flags=re.IGNORECASE)
    s = re.sub(r"\s*[-–|/]\s*(?:remote|hybrid|onsite|on-site)\s*$", "", s,
               flags=re.IGNORECASE)
    s = re.sub(r"\s{2,}", " ", s).strip(" -–|,/")
    return s[:max_length]


def _seniority_of(title_blob: str, body_blob: str,
                  years_min: Optional[int]) -> str:
    """Highest band the title claims; years only break a tie of silence.

    Ordered most-specific-first so "Senior Director" reads as director and
    "Senior Staff Engineer" reads as staff, not senior.
    """
    for band, pattern in _SENIORITY_RULES:
        if pattern.search(title_blob):
            return band
    if years_min is not None:
        if years_min >= 12:
            return "principal"
        if years_min >= 8:
            return "staff"
        if years_min >= 5:
            return "senior"
        if years_min >= 2:
            return "mid"
        return "entry"
    if _SENIORITY_RULES[0][1].search(body_blob):
        return "intern"
    return "unknown"


def _years_of(body_blob: str) -> Tuple[Optional[int], Optional[int]]:
    """Lowest stated requirement wins; a posting asking 3+ and 8+ years for
    different sub-skills requires 3 to apply. Nothing is invented: no match
    returns (None, None), never (0, 0)."""
    lo: Optional[int] = None
    hi: Optional[int] = None
    for m in _YEARS_RE.finditer(body_blob):
        try:
            a = int(m.group(1))
        except (TypeError, ValueError):
            continue
        if a > 40:
            continue
        b = None
        if m.group(2):
            try:
                b = int(m.group(2))
            except (TypeError, ValueError):
                b = None
            if b is not None and (b > 45 or b < a):
                b = None
        if lo is None or a < lo:
            lo = a
            hi = b if b is not None else hi
    return lo, hi


_SKILL_CANON: Dict[str, str] = {}
for _t in _TECH_EVIDENCE_TERMS:
    _clean = _t.replace("\\b", "").replace("\\.", ".").replace("\\+", "+").strip()
    if _clean:
        _SKILL_CANON[_clean.lower()] = _clean

_REQUIRED_SECTION_RE = re.compile(
    r"(?:^|\n)\s*(?:minimum|basic|required|must[\s-]have|requirements?|"
    r"qualifications?|what you.{0,12}ll need|you have)\b", re.IGNORECASE)
_PREFERRED_SECTION_RE = re.compile(
    r"(?:^|\n)\s*(?:preferred|nice[\s-]to[\s-]have|bonus|plus(?:es)?|desired|"
    r"good to have|additional qualifications)\b", re.IGNORECASE)


def _split_requirement_sections(description: str) -> Tuple[str, str]:
    """(required-ish text, preferred-ish text).

    Deliberately crude: postings do not agree on structure, so this only claims
    "the text after a `Preferred` heading is preferred". When no heading is
    found the whole body counts as required-ish, which is the conservative
    reading - a skill listed once with no section is treated as needed.
    """
    if not description:
        return "", ""
    m = _PREFERRED_SECTION_RE.search(description)
    if not m:
        return description, ""
    return description[:m.start()], description[m.start():]


def _skills_in(text: str, limit: int = 40) -> List[str]:
    if not text:
        return []
    out: List[str] = []
    seen: Set[str] = set()
    for m in _TECH_EVIDENCE_RE.finditer(text):
        tok = m.group(0).strip().lower()
        canon = _SKILL_CANON.get(tok, tok)
        if canon in seen:
            continue
        seen.add(canon)
        out.append(canon)
        if len(out) >= limit:
            break
    return out


def _ev(kind: str, field_name: str, value: str) -> Dict[str, str]:
    return {"kind": kind, "field": field_name, "value": str(value)[:120]}


def classify(
    title: Any,
    *,
    description: Any = "",
    department: Any = "",
    skills: Optional[Sequence[Any]] = None,
    requirements: Optional[Dict[str, Any]] = None,
) -> TechClassification:
    """Classify one posting's OCCUPATION. Employer identity is not an input.

    Evidence ladder, highest first:

    1. TITLE names a specialty unambiguously        -> core/adjacent, conf .88-.95
    2. TITLE is a non-tech occupation               -> non_tech, conf .93
    3. TITLE is ambiguous AND body/department/skills
       carry technical evidence                     -> core/adjacent, conf .62-.80
    4. TITLE is ambiguous with no evidence          -> unknown, conf <= .3
    5. Nothing matches at all                       -> unknown, conf 0

    A physical-engineering qualifier vetoes a software reading (rule 1) and
    demotes to non_tech/physical_engineering, EXCEPT where semiconductor or
    embedded context is present, which is the `semiconductor_hardware`
    adjacent family and is a legitimate selectable area.
    """
    raw_title = str(title or "")
    title_blob = _norm(raw_title)
    desc_text = str(description or "")
    body_blob = _norm(desc_text)
    dept_blob = _norm(department)
    skills_blob = _norm(" ".join(str(s) for s in (skills or [])))

    result = TechClassification(normalized_title=normalize_title(raw_title))
    if not title_blob:
        return result

    evidence: List[Dict[str, str]] = []

    # ---- years + seniority + skills are independent of the scope verdict ----
    req = requirements if isinstance(requirements, dict) else {}
    years_min, years_max = _years_of(body_blob)
    if years_min is None:
        rq = req.get("required_years")
        if isinstance(rq, (int, float)) and 0 <= rq <= 40:
            years_min = int(rq)
    result.required_years_min = years_min
    result.required_years_max = years_max
    result.seniority = _seniority_of(title_blob, body_blob, years_min)

    req_text, pref_text = _split_requirement_sections(desc_text)
    required_skills = _skills_in(_norm(req_text) + " " + skills_blob)
    preferred_skills = [s for s in _skills_in(_norm(pref_text))
                        if s not in set(required_skills)]
    result.skills_required = required_skills[:30]
    result.skills_preferred = preferred_skills[:20]

    # ---- 1. positive specialty matches on the TITLE ------------------------
    spanned: List[Tuple[str, str, str, int, int]] = []
    for fam, spec, pattern in _SPEC_PATTERNS:
        m = pattern.search(title_blob)
        if m:
            spanned.append((fam, spec, m.group(0), m.start(), m.end()))
    title_hits = _dedupe_overlapping(spanned)

    physical = bool(_PHYSICAL_QUALIFIER_RE.search(title_blob))
    hardware_ctx = bool(_HARDWARE_CONTEXT_RE.search(title_blob)
                        or _HARDWARE_CONTEXT_RE.search(body_blob[:4000]))

    # ---- 2. non-tech veto on the TITLE -------------------------------------
    nt = _NON_TECH_RE.search(title_blob)
    if nt and not title_hits:
        phrase = nt.group(0)
        result.tech_scope = SCOPE_NON_TECH
        result.classification_confidence = 0.93
        result.classification_evidence = [
            _ev("non_tech_title", "title", phrase)]
        return result

    if title_hits:
        # A physical qualifier beats a software role noun. "Staff Hardware
        # Reliability Engineer" is not SRE; "Corrosion Reliability Engineer"
        # is not devops. Semiconductor/embedded context is the exception -
        # those ARE the semiconductor_hardware adjacent family.
        software_ish = {f for f, _, _ in title_hits} & {
            F_SOFTWARE, F_CLOUD, F_DATA, F_AI_ML, F_SECURITY, F_QUALITY,
            # "Global Tooling Engineering Manager" at a manufacturer matched
            # `engineering manager` and read as technical leadership. A
            # physical qualifier vetoes that reading too.
            F_TECH_LEADERSHIP, F_UX_DESIGN_SYSTEMS}
        if physical and software_ish and not hardware_ctx:
            result.tech_scope = SCOPE_NON_TECH
            result.classification_confidence = 0.8
            result.classification_evidence = [
                _ev("physical_qualifier_veto", "title",
                    _PHYSICAL_QUALIFIER_RE.search(title_blob).group(0)),
                _ev("role_noun", "title", title_hits[0][2]),
            ]
            return result

        # Ambiguity guard: a title whose ONLY hit came from a broad phrase and
        # which also reads as a non-tech occupation needs body evidence.
        if nt:
            has_body = bool(_TECH_EVIDENCE_RE.search(body_blob)
                            or _TECH_DEPARTMENT_RE.search(dept_blob)
                            or skills_blob)
            if not has_body:
                result.tech_scope = SCOPE_UNKNOWN
                result.classification_confidence = 0.3
                result.classification_evidence = [
                    _ev("conflicting_title", "title",
                        f"{title_hits[0][2]} vs {nt.group(0)}")]
                return result
            evidence.append(_ev("conflict_resolved_by_body", "description",
                                _TECH_EVIDENCE_RE.search(body_blob).group(0)
                                if _TECH_EVIDENCE_RE.search(body_blob)
                                else "department/skills"))

        return _finalize(result, title_hits, evidence, body_blob, dept_blob,
                         skills_blob, title_source="title")

    # ---- 3. ambiguous title: look for evidence -----------------------------
    ambiguous = bool(_AMBIGUOUS_TITLE_RE.search(title_blob))
    corpus = " ".join(x for x in (body_blob, dept_blob, skills_blob) if x)
    body_spanned: List[Tuple[str, str, str, int, int]] = []
    if corpus:
        for fam, spec, pattern in _SPEC_PATTERNS:
            m = pattern.search(corpus)
            if m:
                body_spanned.append((fam, spec, m.group(0), m.start(), m.end()))
    body_hits = _dedupe_overlapping(body_spanned)

    tech_terms = _skills_in(corpus, limit=12)
    dept_hit = _TECH_DEPARTMENT_RE.search(dept_blob)

    if not ambiguous and not body_hits and not tech_terms:
        # Not a title we recognise at all, and no evidence anywhere. Unknown -
        # NEVER software. This is the branch that keeps a Store Manager we
        # forgot to enumerate out of the core-tech result set.
        result.classification_confidence = 0.0
        return result

    # A department alone must NOT promote an arbitrary title: "Executive
    # Assistant, Information Technology" is not a technical job. Department is
    # corroboration for a title that is ALREADY technically ambiguous, never
    # evidence on its own. Two skill terms are enough on an ambiguous title
    # (the non-tech titles are vetoed above), three otherwise.
    strong_enough = (
        bool(body_hits)
        or len(tech_terms) >= 3
        or (ambiguous and (bool(dept_hit) or len(tech_terms) >= 2))
    )
    if not strong_enough:
        result.tech_scope = SCOPE_UNKNOWN
        result.classification_confidence = 0.25 if ambiguous else 0.1
        if tech_terms:
            result.classification_evidence = [
                _ev("weak_body_signal", "description", ", ".join(tech_terms))]
        return result

    if dept_hit:
        evidence.append(_ev("department", "department", dept_hit.group(0)))
    if tech_terms:
        evidence.append(_ev("skills", "description", ", ".join(tech_terms[:8])))

    if not body_hits:
        for pattern, fam, spec in _AMBIGUOUS_FAMILY_HINTS:
            m = pattern.search(title_blob)
            if m:
                evidence.append(_ev("ambiguous_title_hint", "title", m.group(0)))
                result.tech_scope = FAMILY_SCOPE.get(fam, SCOPE_ADJACENT)
                result.role_family = fam
                result.role_families = [fam]
                result.role_specialties = [spec]
                result.occupation_codes = list(
                    SPECIALTY_OCCUPATION_CODES.get(spec, ()))
                result.classification_confidence = 0.66
                result.classification_evidence = evidence[:8]
                return result
        # Technical work is evidenced but no specialty is named. That is a real
        # answer: scope core, family unknown. It is retrievable and it is
        # honest, which is better than assigning `general_software` and being
        # wrong about the discipline.
        result.tech_scope = SCOPE_CORE
        result.role_family = None
        result.classification_confidence = 0.62
        result.classification_evidence = evidence
        return result

    return _finalize(result, body_hits, evidence, body_blob, dept_blob,
                     skills_blob, title_source="body")


def _dedupe_overlapping(
        hits: List[Tuple[str, str, str, int, int]]) -> List[Tuple[str, str, str]]:
    """Drop a match whose span is CONTAINED in a longer match's span.

    Without this, "Design Systems Engineer" matches both
    `design systems engineer` (ux_design_systems, adjacent) and the substring
    `systems engineer` (cloud_platform.infrastructure, core) - and since the
    result is multi-label, the spurious core label wins the scope and the card
    reads "Cloud & Platform". The longer phrase is the more specific reading of
    the same words; the shorter one is not an additional label, it is the same
    label read badly.

    Remaining hits are ordered longest-first so `role_family` is the most
    specific family, not whichever rule happens to be declared earliest.
    """
    ordered = sorted(hits, key=lambda h: (h[4] - h[3]), reverse=True)
    kept: List[Tuple[str, str, str, int, int]] = []
    for h in ordered:
        if any(k[3] <= h[3] and h[4] <= k[4] for k in kept):
            continue
        kept.append(h)
    return [(f, sp, ph) for f, sp, ph, _, _ in kept]


def _finalize(result: TechClassification,
              hits: List[Tuple[str, str, str]],
              evidence: List[Dict[str, str]],
              body_blob: str, dept_blob: str, skills_blob: str,
              *, title_source: str) -> TechClassification:
    """Turn raw hits into the multi-label verdict + confidence."""
    families: List[str] = []
    specialties: List[str] = []
    codes: List[str] = []
    for fam, spec, phrase in hits:
        if fam not in families:
            families.append(fam)
        if spec not in specialties:
            specialties.append(spec)
            for c in SPECIALTY_OCCUPATION_CODES.get(spec, ()):
                if c not in codes:
                    codes.append(c)
        evidence.append(_ev(f"{title_source}_phrase", title_source, phrase))

    # `general_software` is the catch-all inside its family; if a narrower
    # specialty in the SAME family also matched, the narrow one is the answer
    # and the catch-all is noise on the card.
    # Declared dual-family labels. See `_CROSS_LISTED`.
    matched_phrases = [p.lower() for _, _, p in hits]
    for phrase, xfam, xspec in _CROSS_LISTED:
        if not any(phrase == mp or phrase in mp for mp in matched_phrases):
            continue
        if xfam not in families:
            families.append(xfam)
        if xspec not in specialties:
            specialties.append(xspec)
            for c in SPECIALTY_OCCUPATION_CODES.get(xspec, ()):
                if c not in codes:
                    codes.append(c)
        evidence.append(_ev("cross_listed", title_source, f"{phrase} -> {xspec}"))

    if "general_software" in specialties and len(
            [s for s in specialties if s in FAMILY_SPECIALTIES[F_SOFTWARE]]) > 1:
        specialties.remove("general_software")

    # Family ordering: the family of the FIRST (longest, most specific) hit
    # leads, then the rest in match order. `role_family` is the single value
    # legacy readers and Mongo equality filters use.
    primary_family = families[0] if families else None
    scopes = {FAMILY_SCOPE.get(f, SCOPE_UNKNOWN) for f in families}
    scope = SCOPE_CORE if SCOPE_CORE in scopes else (
        SCOPE_ADJACENT if SCOPE_ADJACENT in scopes else SCOPE_UNKNOWN)

    # Confidence. A title-sourced verdict is stronger than a body-sourced one;
    # corroboration in the body raises either.
    if title_source == "title":
        conf = 0.88
        if _TECH_EVIDENCE_RE.search(body_blob) or skills_blob:
            conf = 0.95
        elif _TECH_DEPARTMENT_RE.search(dept_blob):
            conf = 0.91
    else:
        conf = 0.72
        if len(hits) > 1:
            conf = 0.8

    result.tech_scope = scope
    result.role_family = primary_family
    result.role_families = families[:4]
    result.role_specialties = specialties[:6]
    result.occupation_codes = codes[:6]
    result.classification_confidence = conf
    result.classification_evidence = evidence[:8]
    return result


# ---------------------------------------------------------------------------
# Catalog surface for the UI filter tree and for request validation.
# ---------------------------------------------------------------------------

def taxonomy_catalog() -> Dict[str, Any]:
    """The whole taxonomy in one payload the frontend can render as a tree."""
    return {
        "taxonomy_version": TAXONOMY_VERSION,
        "scopes": list(SCOPES),
        "families": [
            {
                "key": fam,
                "label": FAMILY_LABELS[fam],
                "scope": FAMILY_SCOPE[fam],
                "specialties": [
                    {
                        "key": spec,
                        "label": SPECIALTY_LABELS.get(spec, spec),
                        "occupation_codes": list(
                            SPECIALTY_OCCUPATION_CODES.get(spec, ())),
                    }
                    for spec in FAMILY_SPECIALTIES.get(fam, ())
                ],
            }
            for fam in ALL_FAMILIES
        ],
    }


def is_valid_family(value: Any) -> bool:
    return str(value or "") in set(ALL_FAMILIES)


def is_valid_specialty(value: Any) -> bool:
    return str(value or "") in set(ALL_SPECIALTIES)


def families_for_specialties(specialties: Iterable[Any]) -> List[str]:
    """Every family that owns at least one of the given specialties."""
    wanted = {str(s or "") for s in specialties}
    return [f for f in ALL_FAMILIES
            if wanted & set(FAMILY_SPECIALTIES.get(f, ()))]


#: Legacy bridge. `job_postings.role_family` currently stores
#: `fit_score.ROLE_FAMILIES` keys, and every existing index and read path uses
#: them. Rather than rewrite 227k documents' primary retrieval key on day one,
#: the new taxonomy is stored ALONGSIDE under `tech.*` and this map lets a
#: request expressed in new families still hit the old index.
LEGACY_FAMILY_MAP: Dict[str, Tuple[str, ...]] = {
    F_SOFTWARE: ("software_engineering",),
    F_AI_ML: ("data_science", "software_engineering"),
    F_DATA: ("data_engineering", "data_science", "data_analysis"),
    F_CLOUD: ("devops", "software_engineering"),
    F_SECURITY: ("security",),
    F_QUALITY: ("qa",),
    F_TECH_LEADERSHIP: ("software_engineering", "devops", "product"),
    F_TECHNICAL_PRODUCT: ("product", "project_management"),
    F_SOLUTIONS_ENG: ("software_engineering", "customer_support"),
    F_DEVREL: ("software_engineering", "marketing"),
    F_TECH_WRITING: ("design", "marketing"),
    F_UX_DESIGN_SYSTEMS: ("design",),
    F_IT_SUPPORT: ("it_support", "devops"),
    F_SEMICONDUCTOR: ("hardware_engineering",),
}


def legacy_families_for(families: Iterable[Any]) -> List[str]:
    """New-taxonomy families -> the `role_family` values already indexed."""
    out: List[str] = []
    for f in families or ():
        for legacy in LEGACY_FAMILY_MAP.get(str(f or ""), ()):
            if legacy not in out:
                out.append(legacy)
    return out


#: Legacy `role_family` values that can plausibly contain core-tech postings.
#: Used ONLY to widen retrieval before the `tech.*` backfill completes; it
#: never decides a scope on its own.
LEGACY_TECH_CANDIDATE_FAMILIES: Tuple[str, ...] = (
    "software_engineering", "devops", "security", "qa", "data_engineering",
    "data_science", "data_analysis", "it_support", "hardware_engineering",
    "product", "project_management", "design", "other",
)
