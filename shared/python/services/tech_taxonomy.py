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

import functools
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

logger = logging.getLogger(__name__)

TAXONOMY_VERSION = "tech-tax-1.3.0"

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

#: `semiconductor_hardware` is CORE, not adjacent.
#:
#: It was adjacent in 1.0.0, which meant the default scope (`["core"]`) hid
#: every ASIC, RTL, verification, firmware, analog, RF, EDA and photonics role
#: unless the searcher went and ticked an extra box. Measured on live
#: `aspirely_db`, that is 7,161 active postings under the legacy
#: `hardware_engineering` family alone — a silicon engineer searching this
#: product saw an empty feed by default. Chip design is technology employment;
#: the taxonomy now says so.
CORE_FAMILIES = (F_SOFTWARE, F_AI_ML, F_DATA, F_CLOUD, F_SECURITY, F_QUALITY,
                 F_TECH_LEADERSHIP, F_SEMICONDUCTOR)
ADJACENT_FAMILIES = (F_TECHNICAL_PRODUCT, F_SOLUTIONS_ENG, F_DEVREL,
                     F_TECH_WRITING, F_UX_DESIGN_SYSTEMS, F_IT_SUPPORT)
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
        # The bare discipline, for the same reason as "data science": the
        # phrase table could read "Machine Learning Engineer" and not
        # "Machine Learning Intern", "Machine Learning Co-op" or "Machine
        # Learning, Ads". Longest-first alternation means every enumerated
        # phrase above still wins where it applies.
        "machine learning", "deep learning",
    )),
    (F_AI_ML, "applied_ai", (
        # "Applied AI" alone: real live titles interleave a level word
        # ("Applied AI SENIOR Consultant"), and the phrase compiler joins
        # words with a single separator, so the full phrases below cannot
        # reach them. The bare term is unambiguous in a job title.
        "applied ai", "applied machine learning",
        "applied ai consultant", "applied ai specialist", "applied ai lead",
        "applied ai architect", "applied machine learning engineer",
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
        "inference performance engineer", "inference engineer",
        "model performance engineer", "model optimization engineer",
        "inference optimization engineer", "gpu performance engineer",
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
        # The BARE discipline noun. "Data Science Intern", "Data Science
        # Co-op", "Data Science Associate" and "Data Science, Fraud" all name
        # the discipline and none of them contains "data scientist", so the
        # whole set read `unknown` — which is what produced a live résumé
        # whose two titles were "Data Science Intern" and "Data Quality
        # Specialist Intern" and whose taxonomy came back EMPTY.
        "data science",
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
        "data stewardship", "data steward",
        "data governance engineer", "data governance analyst",
        "data quality engineer", "master data management",
        "data steward", "metadata engineer",
        # `specialist` is not a STACK role noun (see `_STACK_ROLE_NOUNS`) and
        # it never will be, but "Data Quality Specialist" and "Data Quality
        # Analyst" are the two commonest spellings of this job on live
        # inventory and both were unreadable. Enumerated, not generalised.
        "data quality specialist", "data quality analyst", "data quality lead",
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
        "compute engineer",
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
        "it service management", "itsm", "service management specialist",
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
    (F_IT_SUPPORT, "data_center_operations", (
        "data center technician", "data centre technician",
        "data center operations technician", "data center engineer",
        "data centre engineer", "data center operations engineer",
        "critical facilities technician", "critical environment technician",
        "cabling infrastructure technician", "cabling infrastructure tech",
        "data center operations", "data centre operations",
        "colocation technician",
        "engineering operations technician",
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
        "signal integrity engineer", "power electronics engineer",
        "computer hardware engineer", "hardware systems engineer",
    )),

    # ---- 1.1.0 additions --------------------------------------------------
    #
    # Every specialty below was added from UNMATCHED live titles, not from
    # imagination. Before this, each of these titles classified as `unknown`
    # or landed in `general_software`, which is why the taxonomy report showed
    # a large unclassified tail inside employers we crawl well.

    (F_SOFTWARE, "enterprise_mainframe", (
        "mainframe developer", "mainframe engineer", "mainframe programmer",
        "cobol developer", "cobol programmer", "cobol engineer",
        "as400 developer", "as/400 developer", "iseries developer",
        "rpg developer", "jcl developer", "cics developer",
        "z/os systems programmer", "mainframe systems programmer",
        "sap abap developer", "abap developer", "abap consultant",
        "peoplesoft developer", "siebel developer", "mumps developer",
        "legacy modernization engineer",
    )),
    (F_SOFTWARE, "quantitative_low_latency", (
        # Live titles interleave the language: "Low Latency C++ Engineer".
        # The phrase compiler joins words with ONE separator, so the bare
        # phrase cannot reach them and `c++ engineer` won the span instead.
        "low latency engineer", "low-latency engineer",
        "low latency c++ engineer", "low latency java engineer",
        "low latency rust engineer", "low latency systems engineer",
        "low latency software engineer", "low latency platform engineer",
        "ultra low latency engineer", "quantitative developer",
        "quantitative software engineer", "quant developer",
        "quantitative engineer", "trading systems engineer",
        "electronic trading engineer", "market data engineer",
        "exchange connectivity engineer", "hft engineer",
        "high frequency trading engineer", "algorithmic trading engineer",
    )),
    (F_CLOUD, "hpc", (
        "hpc engineer", "high performance computing engineer",
        "high-performance computing engineer", "supercomputing engineer",
        "hpc systems engineer", "hpc software engineer",
        "parallel computing engineer", "cuda engineer",
        "gpu computing engineer", "scientific computing engineer",
        "cluster engineer", "slurm administrator",
    )),
    (F_CLOUD, "edge_cdn", (
        "edge engineer", "edge computing engineer", "cdn engineer",
        "content delivery engineer", "edge infrastructure engineer",
        "edge platform engineer", "edge network engineer",
    )),
    (F_CLOUD, "storage_engineering", (
        "storage engineer", "storage systems engineer",
        "distributed storage engineer", "filesystem engineer",
        "file system engineer", "object storage engineer",
        "san engineer", "nas engineer", "storage software engineer",
        "block storage engineer",
    )),
    (F_CLOUD, "virtualization", (
        "virtualization engineer", "hypervisor engineer",
        "vmware engineer", "kvm engineer", "xen engineer",
        "virtual infrastructure engineer", "vdi engineer",
        "virtualization software engineer",
    )),
    (F_QUALITY, "build_release", (
        "build engineer", "release engineer", "build and release engineer",
        "build & release engineer", "release management engineer",
        "software configuration management engineer", "scm engineer",
        "build systems engineer", "toolchain engineer",
        "release train engineer", "continuous integration engineer",
        "ci engineer", "ci/cd engineer",
    )),
    (F_AI_ML, "ai_agents", (
        "ai agent engineer", "agent engineer", "agentic ai engineer",
        "ai agent developer", "agentic ai developer", "agent developer",
        "agentic systems engineer", "autonomous agent engineer",
        "ai automation engineer", "agent infrastructure engineer",
        "multi agent systems engineer", "ai orchestration engineer",
    )),
    (F_AI_ML, "ai_tooling", (
        "ai tooling engineer", "ai developer tools engineer",
        "llm tooling engineer",
        "ai developer experience engineer", "ai product engineer",
        "forward deployed engineer",
    )),
    (F_SECURITY, "ai_security", (
        "ai security engineer", "ml security engineer",
        "llm security engineer", "ai red team engineer",
        "adversarial machine learning engineer",
        "model security engineer", "ai trust and safety engineer",
    )),
    (F_SECURITY, "privacy_engineering", (
        "privacy engineer", "data privacy engineer",
        "privacy software engineer", "privacy infrastructure engineer",
        "privacy program engineer", "gdpr engineer",
    )),
    (F_SECURITY, "cryptography", (
        "cryptography engineer", "cryptographic engineer",
        "applied cryptography engineer", "cryptography researcher",
        "pki engineer", "key management engineer",
        "post quantum cryptography engineer", "hsm engineer",
        "confidential computing engineer",
    )),
    (F_SEMICONDUCTOR, "analog_mixed_signal", (
        "analog design engineer", "analog ic design engineer",
        "mixed signal design engineer", "mixed-signal design engineer",
        "analog layout engineer", "analog circuit design engineer",
        "ams design engineer", "serdes design engineer",
        "pll design engineer", "adc design engineer", "dac design engineer",
        "power management ic engineer", "pmic design engineer",
    )),
    (F_SEMICONDUCTOR, "rf_engineering", (
        "rf engineer", "rf design engineer", "rfic design engineer",
        "rf systems engineer", "microwave engineer", "antenna engineer",
        "millimeter wave engineer", "mmwave engineer",
        "rf test engineer", "wireless hardware engineer",
    )),
    (F_SEMICONDUCTOR, "eda_cad", (
        "cad engineer", "eda engineer", "design automation engineer",
        "physical design cad engineer", "eda software engineer",
        "cad methodology engineer", "pdk engineer",
        "design enablement engineer", "circuit simulation engineer",
    )),
    (F_SEMICONDUCTOR, "process_yield", (
        # A BARE "process engineer" is not a semiconductor role - the golden
        # set's own example is "Optimize the polymerization line" in a
        # Manufacturing department. Every phrase here names the fab.
        "semiconductor process engineer", "wafer process engineer",
        "fab process engineer", "process integration engineer",
        "yield engineer", "yield enhancement engineer", "yield analysis engineer",
        "semiconductor device engineer", "device physics engineer",
        "lithography engineer", "photolithography engineer", "etch engineer",
        "thin film engineer", "cmp engineer", "diffusion engineer",
        "epitaxy engineer", "defect engineer", "metrology engineer",
    )),
    (F_SEMICONDUCTOR, "product_test", (
        "semiconductor product engineer", "semiconductor test engineer",
        "ate engineer", "automated test equipment engineer",
        "silicon test engineer", "silicon characterization engineer",
        "product test engineer semiconductor", "burn in engineer",
        "wafer test engineer", "probe engineer",
    )),
    (F_SEMICONDUCTOR, "packaging", (
        "semiconductor packaging engineer", "advanced packaging engineer",
        "ic packaging engineer", "assembly and test engineer",
        "substrate engineer", "chiplet engineer", "3d integration engineer",
        "flip chip engineer", "wafer level packaging engineer",
    )),
    (F_SEMICONDUCTOR, "photonics", (
        "photonics engineer", "silicon photonics engineer",
        "photonic integrated circuit engineer", "optical design engineer",
        "optical systems engineer", "electro optic engineer",
        "fiber optic engineer", "laser engineer", "optomechanical engineer",
        # The specialty existed and the phrases did not reach the market's own
        # spellings. Measured on live `job_postings`: "Photonics Technology
        # Engineer" (6 postings), "Photonics Scientist", "Photonic Design
        # Engineer", "Integrated Photonic Test Engineer", "Silicon Photonics
        # Technology Development & Integration Engineer" — every one read
        # `unknown` while `photonics` sat in the taxonomy unmatched.
        "photonics technology engineer", "photonics scientist",
        "photonic design engineer", "photonic design manager",
        "photonics technologist", "integrated photonics engineer",
        "integrated photonic test engineer", "photonic test engineer",
        "photonics integration engineer", "silicon photonics",
        "integrated photonics", "photonics engineering",
    )),

    # ---- quantum ---------------------------------------------------------
    # Added in tech-tax-1.2.0 on live evidence, not on a hunch: 71 distinct
    # active titles / 77 postings match quantum, qubit, cryogenic or
    # superconducting, and the classifier read them badly. "Quantum Software
    # Engineer (Compiler)" landed in `general_software`, "Senior Cryogenic
    # Systems Engineer" in cloud `infrastructure`, "Sr. Quantum 3D Integration
    # and Packaging Engineer" and "Lithography Process Engineer - Quantum
    # Program" in **non_tech**, and the rest in `unknown`.
    #
    # Four specialties, because the market hires for four different jobs and a
    # single `quantum` bucket would put a compiler engineer and a dilution-
    # refrigerator engineer in the same ranked list:
    #
    #   quantum_software      SDKs, compilers, simulators, error-correction
    #                         software. A software job; lives in the software
    #                         family, which is why a backend engineer reads as
    #                         same-family rather than unrelated.
    #   quantum_hardware      the device itself: qubit design, superconducting
    #                         and trapped-ion processors, quantum packaging.
    #   quantum_control       the electronics that drive it — pulse and AWG
    #                         stacks, cryo-CMOS, FPGA control planes. The
    #                         nearest existing neighbours are FPGA/RTL and
    #                         analog, and the adjacency matrix says so.
    #   cryogenic_systems     cryostats, dilution refrigerators, vacuum and
    #                         cryogenic engineering. Not `infrastructure`:
    #                         cloud infrastructure and a dilution refrigerator
    #                         share a word and nothing else.
    (F_SOFTWARE, "quantum_software", (
        "quantum software engineer", "quantum software developer",
        "quantum software manager", "quantum compiler engineer",
        "quantum algorithm engineer", "quantum algorithm scientist",
        "quantum algorithms researcher", "quantum error correction software",
        "quantum error correction engineer", "quantum error correction",
        "quantum computing software", "quantum sdk", "quantum simulator",
        "quantum applications engineer", "quantum integrations engineer",
        "quantum integration engineer", "quantum systems software",
        "qiskit", "cirq", "pennylane",
    )),
    (F_SEMICONDUCTOR, "quantum_hardware", (
        "quantum hardware engineer", "qubit engineer", "qubit design",
        "quantum processor", "quantum device engineer",
        "quantum device physicist", "superconducting qubit",
        "superconducting circuit designer", "superconducting ic designer",
        "trapped ion engineer", "ion trap engineer",
        "quantum packaging engineer", "quantum integration and packaging",
        "quantum 3d integration", "quantum integration", "quantum packaging",
        "quantum chip", "quantum fabrication", "quantum photonics",
        "quantum engineer", "quantum physicist", "quantum scientist",
        "quantum research scientist", "quantum researcher",
    )),
    (F_SEMICONDUCTOR, "quantum_control", (
        "quantum control engineer", "qubit control", "quantum control systems",
        "quantum control electronics", "cryo cmos", "cryogenic cmos",
        "quantum controller", "pulse sequencing engineer",
        "quantum measurement engineer", "quantum test and measurement",
        "quantum characterization engineer",
    )),
    (F_SEMICONDUCTOR, "cryogenic_systems", (
        "cryogenic engineer", "cryogenics engineer", "cryogenic systems engineer",
        "cryogenic system engineer", "cryogenic rf engineer",
        "dilution refrigerator", "cryostat engineer", "cryogenic technician",
        "vacuum and cryogenic", "cryogenic and vacuum", "cryogenic hardware",
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
    "data_center_operations": "Data Center Operations",
    "asic_soc_design": "ASIC / SoC Design", "fpga_rtl": "FPGA / RTL",
    "verification": "Design Verification",
    "hardware_engineering": "Hardware Engineering",
    # 1.1.0
    "enterprise_mainframe": "Enterprise / Mainframe",
    "quantitative_low_latency": "Quant / Low Latency",
    "hpc": "High-Performance Computing",
    "edge_cdn": "Edge / CDN",
    "storage_engineering": "Storage Engineering",
    "virtualization": "Virtualization",
    "build_release": "Build & Release",
    "ai_agents": "AI Agents",
    "ai_tooling": "AI Tooling",
    "ai_security": "AI Security",
    "privacy_engineering": "Privacy Engineering",
    "cryptography": "Cryptography",
    "analog_mixed_signal": "Analog / Mixed-Signal",
    "rf_engineering": "RF & Antenna",
    "eda_cad": "EDA / CAD",
    "process_yield": "Process & Yield",
    "product_test": "Product & Test Engineering",
    "packaging": "Advanced Packaging",
    "photonics": "Photonics & Optics",
    "quantum_software": "Quantum Software",
    "quantum_hardware": "Quantum Hardware",
    "quantum_control": "Quantum Control Systems",
    "cryogenic_systems": "Cryogenic Systems",
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
        # Live-sample additions; see the note in `sales_marketing_support`.
        "medication technician", "medication assistant",
        "patient service representative", "patient care administration",
        "house supervisor", "clinical specialist", "clinical account",
        "hospice", "counseling services", "counselor", "irb coordinator",
        "research study coordinator", "environmental services associate",
        "\\bpodiatrist\\b", "biometrics", "ip coding", "transporter",
        "sample and receiving technician", "\\bpharmacy\\b",
        "\\ber tech\\b", "revenue cycle",
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
        "medical laboratory scientist", "clinical laboratory scientist",
        "clinical laboratory technologist", "medical laboratory technician",
        "radiology technologist", "mri technologist", "ct technologist",
        "mammography technologist", "nuclear medicine technologist",
        "histotechnologist", "cytotechnologist", "polysomnographic technologist",
        "echocardiographer", "cardiovascular technologist",
        "\\bnurse\\b", "\\brn\\b", "charge nurse", "staff nurse",
        "travel nurse", "clinical nurse", "nurse manager", "\\bcrna\\b",
        "physician assistant", "\\bmd\\b", "hospitalist", "radiologist",
        "pathologist", "dietitian", "nutritionist", "medical scribe",
        "patient care coordinator", "ultrasound technologist",
        "surgical assistant", "perfusionist", "audiologist",
        "pharmacy intern", "pharmacy graduate", "dietary aide",
        "patient access representative", "unit secretary", "sterile processing",
    )),
    ("retail_food_hospitality", (
        # Live-sample additions; see the note in `sales_marketing_support`.
        "teammate", "retail sales and store support", "store support",
        "merchandising assistant manager", "retail key holder",
        "temporary associate", "seasonal full time", "seasonal part time",
        "\\bchecker\\b", "grille clerk", "deli assistant manager",
        "stewarding", "room attendant", "hotel valet",
        "restaurant and operations manager", "restaurant & operations manager",
        # Non-English titles in the live inventory. Enumerated rather than
        # translated: the classifier reads the string the board printed.
        "gerente de turno", "miembro del equipo", "cajero",
        "ausbildung", "verkaufer", "kaufmann", "einzelhandel",
        "prepose", "entretien menager", "gerente de saude",
        "auditor trainee", "praktikum",
        "store manager", "assistant store manager", "shift supervisor",
        "sales associate", "retail associate", "cashier", "stocker",
        "merchandiser", "barista", "cook", "line cook", "chef", "server",
        "bartender", "dishwasher", "restaurant manager", "general manager restaurant",
        "housekeeper", "housekeeping", "front desk agent", "hotel manager",
        "delivery driver", "crew member", "team member", "shift leader",
        "food service", "grocery clerk", "produce clerk", "deli clerk",
        "meat cutter", "baker", "pharmacy clerk", "loss prevention",
        "assistant manager retail",
        # Added 2026-09-02 from the highest-VOLUME unmatched live titles. Each
        # was landing in `unknown`, and `unknown` is ORed into retrieval by
        # design - so tens of thousands of shop-floor postings were diluting a
        # technology feed instead of being confidently excluded from it.
        "store associate", "in store shopper", "front end entry level",
        "restaurant shift manager", "meat clerk", "seafood clerk",
        "bakery clerk", "cake decorator", "merchandise associate",
        "retail merchandise associate", "customer experience associate",
        "customer experience manager", "retail sales consultant",
        "retail parts pro", "sales lead", "support lead", "manager trainee",
        "room attendant", "order fulfillment associate", "mobile associate",
        "personal banker", "bank teller", "branch manager",
        "retail display installer", "asset protection specialist",
        "retail security specialist", "guest advocate", "fulfillment associate",
        "overnight stocker", "cart attendant", "food service worker",
    )),
    ("trades_logistics", (
        # Live-sample additions; see the note in `sales_marketing_support`.
        "\\bdriver\\b", "store driver", "transfer driver", "medical courier",
        "warehouse order selector", "order selector", "warehouse labor",
        "warehouse laborer", "general warehouse", "piece rate",
        "fuel station attendant", "valet attendant", "cart attendant",
        "auto body technician", "fleet maintenance", "fueler",
        "hvac maintenance", "maintenance mechanic", "maintenance supervisor",
        "maintenance director", "equipment maintenance", "equipment associate",
        "logistics team leader", "area dispatcher", "dispatcher",
        "highway maintenance", "floor person", "comber",
        "associate operator", "machine operator", "\\bcleaner\\b",
        "chiller technician", "equipment maint", "engineer, manufacturing",
        "manufacturing engineering", "dispersion chemist", "\\bchemist\\b",
        "building automation", "\\bbas/bms\\b", "process control systems",
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
        "janitorial cleaner", "janitorial maintenance", "diesel technician",
        "production supervisor", "warehouse supervisor", "shipping clerk",
        "receiving clerk", "inventory associate",
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
        # See the note in `sales_marketing_support` above: these are the
        # business-administration titles the live sample showed reading
        # `unknown`.
        "actuarial analyst", "actuarial", "tax operations", "tax analyst",
        "internal auditor", "internal audit", "internal control manager",
        "compliance product", "counterparty credit", "credit underwriting",
        "fee analyst", "reference data services", "revenue cycle",
        "procurement", "team assistant", "workplace specialist",
        "intelligence analyst", "all-source",
        "conference services", "event services", "admissions counselor",
        "director pricing", "director of pricing",
        # `district manager` is retail-specific enough to enumerate.
        # `general manager`, `account manager`, `operations supervisor`,
        # `regional manager` and `business manager` are NOT here on purpose:
        # they are genuinely ambiguous from the title alone, the live sample
        # labels them `unknown`, and enumerating them would trade 114 honest
        # `unknown`s for four confident wrong answers. Not knowing must stay
        # cheaper than being wrong.
        "district manager",
        "paralegal", "attorney", "legal counsel", "compliance officer",
        "contracts manager", "legal assistant",
    )),
    ("sales_marketing_support", (
        # Added from `evals/data/tech_taxonomy_live_titles.py`, where 114 of
        # 334 reviewed non-technical titles read `unknown` rather than
        # `non_tech`. `unknown` is not a wrong ANSWER — neither state reaches
        # the default core search — but it is a wrong FACT, and two things
        # read it: `fit_score._eligibility` fires a confident cross-domain
        # verdict only on `non_tech`, and `employer_tech_classifier`'s posting
        # mix needs a non-technical denominator to place a private employer.
        # 132,543 active postings sit at `unknown` scope, and every one of
        # them is a row neither can use.
        "customer service associate", "customer service specialist",
        "customer service - self storage", "customer experience specialist",
        "client service associate", "client services project manager",
        "sales consultant", "sales supervisor", "sales lead",
        "sales and operations management trainee", "loan sales",
        "business development manager", "field sales", "direct sales",
        "channel sales", "territory sales", "regional sales manager",
        "director of sales", "senior director, pricing", "strategic partnerships",
        "product marketing", "marketing assistant", "marketing & communications",
        "marketing and communications", "internal communications",
        "campus ambassador", "account coordinator", "sales specialist",
        "sales professional", "management and sales", "technical sales intern",
        "relationship banker", "private client banker",
        "personal banker", "wealth management", "financial advisor",
        "\\bteller\\b",
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
    # Gig / content work that carries an AI or technology word in the title
    # without being a technical occupation. This is a fast-growing shape in
    # the live inventory ("Portuguese Voice Actor - Freelance AI Trainer
    # Project", "AI Visual Creator (Static Ads)") and it is exactly the kind
    # of row a keyword reading of "AI" puts in front of an ML engineer.
    ("gig_content", (
        "voice actor", "ai trainer", "ai tutor", "data annotator",
        "data annotation specialist", "content moderator", "transcriptionist",
        "freelance writer", "translator", "subtitler", "captioner",
        "search quality rater", "ads quality rater", "crowd worker",
        "ai visual creator", "prompt writer",
    )),
    ("education_public", (
        "teacher", "teaching assistant", "professor", "lecturer",
        "instructor", "school counselor", "principal school",
        "librarian", "coach", "athletic", "tutor", "childcare",
        "police officer", "firefighter", "correctional officer",
        "security guard", "security officer", "dispatcher",
        "park ranger", "social services specialist",
        "substitute teacher", "special education teacher", "paraprofessional",
        "school administrator", "curriculum specialist", "school psychologist",
        "academic advisor", "admissions counselor", "registrar",
        "\\bdean\\b", "preschool", "daycare",
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
        "controls engineer", "maintenance engineer", "operating engineer",
        "chief engineer", "stationary engineer", "building engineer",
        "traffic engineer", "landscape architect", "structural designer",
        "civil engineering intern", "civil engineering analyst",
        "hvac design engineer", "fire protection engineer",
        "geological engineer", "drilling engineer", "completions engineer",
        "supplier quality engineer", "landscape architecture",
        "quality engineer manufacturing", "tooling engineer",
        "plant maintenance engineer", "utilities engineer",
    )),
)

#: The physical-engineering role nouns that ARE a semiconductor specialty once
#: fab, photonic or quantum context is present, and which one.
#:
#: Declared, not inferred. "Process Engineer" optimising a polymerization line
#: and "Process Engineer" running wafer lithography are the same two words and
#: different occupations, and the only thing that separates them is the
#: context — so the mapping from one to the other has to be written down where
#: a human can disagree with it.
#:
#: Deliberately short. A phrase is here only when the fab reading is the ONLY
#: reading once hardware context is established; "facilities engineer" is
#: absent because a fab's facilities engineer is still a facilities engineer.
_PHYSICAL_TO_SEMICONDUCTOR: Dict[str, str] = {
    "process engineer": "process_yield",
    "manufacturing engineer": "process_yield",
    "materials engineer": "process_yield",
    "metallurgical engineer": "process_yield",
    "chemical engineer": "process_yield",
    "packaging engineer": "packaging",
    "mechanical engineer": "hardware_engineering",
    "mechanical design engineer": "hardware_engineering",
    "tooling engineer": "product_test",
    "quality engineer manufacturing": "product_test",
}

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
        # Added 2026-09-02 from live unmatched titles: "Senior Sitecore
        # Developer", "Mulesoft Developer", "Guidewire Developer" and the
        # rest were classifying as `unknown` purely because their platform
        # was not in this list.
        r"sitecore", r"mulesoft", r"guidewire", r"duck ?creek", r"tibco",
        r"boomi", r"camunda", r"outsystems", r"mendix", r"powerapps",
        r"power ?platform", r"adobe experience manager", r"aem",
        r"sitefinity", r"episerver", r"optimizely", r"contentful",
        r"hybris", r"commercetools", r"epic", r"cerner", r"mumps",
        r"rpg", r"as/?400", r"iseries", r"progress", r"clarity",
        r"anaplan", r"oracle ?ebs", r"jde", r"sap ?hana", r"s/?4hana",
        r"veeva", r"coupa", r"ariba", r"blackline", r"workfront",
        r"flutter", r"react ?native", r"xamarin", r"maui", r"electron",
        r"graphql", r"grpc", r"elixir", r"erlang", r"clojure", r"haskell",
        r"julia", r"matlab", r"labview", r"verilog", r"vhdl", r"systemverilog",
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
def _onet_skill_terms() -> Tuple[str, ...]:
    """Technology skills from the imported O*NET release plus the overlay.

    Both are OPTIONAL: a missing or malformed data module degrades to the
    hand-written `_TECH_EVIDENCE_TERMS` rather than breaking classification.
    A taxonomy that cannot classify because a dataset failed to import is a
    worse failure than one with a smaller vocabulary.

    Terms are filtered before they join the alternation:

    * two characters or fewer would match inside ordinary words;
    * anything with a regex metacharacter is escaped by the compiler already,
      but a term containing a slash or parenthesis produces a phrase no title
      contains, so it is dropped rather than carried;
    * O*NET's generic category names ("... software", "... system") are
      dropped because they match prose rather than a skill.
    """
    terms: List[str] = []
    seen: Set[str] = set()

    def _add(raw: Any) -> None:
        text = str(raw or "").strip()
        low = text.lower()
        if (len(text) <= 2 or low in seen
                or any(ch in text for ch in "()/\\[]{}|*+?^$")
                or low.endswith((" software", " system", " systems",
                                 " application", " applications", " tool",
                                 " tools", " program", " programs"))):
            return
        seen.add(low)
        terms.append(re.escape(text))

    try:
        from services.onet_data import ALL_SKILLS
        for skill in ALL_SKILLS:
            _add(skill)
    except Exception as exc:      # pragma: no cover - optional dataset
        logger.debug("tech_taxonomy: onet_data unavailable (%s)", type(exc).__name__)
    try:
        from services.onet_overlay import ALL_TOOLS
        for tool in ALL_TOOLS:
            _add(tool)
    except Exception as exc:      # pragma: no cover
        logger.debug("tech_taxonomy: onet_overlay unavailable (%s)",
                     type(exc).__name__)
    return tuple(terms)


#: The hand-written terms FIRST, so a curated phrase always wins a tie against
#: an imported one, then the sourced vocabulary.
_TECH_EVIDENCE_RE = _compile_alternation(
    tuple(_TECH_EVIDENCE_TERMS) + _onet_skill_terms())


#: SOC prefixes whose O*NET alternate titles are evidence that a title is
#: TECHNICAL. Deliberately narrower than the set the taxonomy maps to: the
#: crosswalk legitimately points `grc` at 13-1041 (Compliance Officers) and
#: `tpm` at 13-1082 (Project Management Specialists), and those SOCs carry
#: alternate titles like "Air Pollution Compliance Inspector". A specialty
#: mapping is a statement about where a role sits; it is not a licence to read
#: every title in that occupation as technical.
_ONET_TECH_SOC_PREFIXES: Tuple[str, ...] = (
    "15-12",     # computer occupations
    "15-2051",   # data scientists
    "17-2061",   # computer hardware engineers
    "17-2071",   # electrical engineers
    "17-2072",   # electronics engineers
    "11-3021",   # computer and information systems managers
)


@functools.lru_cache(maxsize=1)
def onet_titles() -> frozenset:
    """Folded O*NET alternate titles for the COMPUTER occupations.

    Used as EVIDENCE that an otherwise-ambiguous title is technical - not as a
    specialty assignment. O*NET maps a title to a SOC code, and a SOC code is
    coarser than a specialty ("Software Developers" covers backend, frontend,
    mobile and embedded), so treating it as a specialty would assert a
    precision the source does not have.

    LAZY, and that is load-bearing: `_norm` is defined further down this
    module, so computing this at import time raised `NameError` into a bare
    `except` and produced an empty set - a vocabulary that silently did
    nothing. Deferring it to first use is what makes the data actually
    reachable.
    """
    try:
        from services.onet_data import ALTERNATE_TITLES
    except Exception:      # pragma: no cover - optional dataset
        return frozenset()
    out = set()
    for soc, titles in ALTERNATE_TITLES.items():
        if not str(soc).startswith(_ONET_TECH_SOC_PREFIXES):
            continue
        for title in titles:
            # "AI Engineer (Artificial Intelligence Engineer)" - the
            # parenthetical is a gloss, not part of the title anybody posts.
            head = str(title).split(" (")[0].strip()
            for variant in {title, head}:
                folded = _norm(variant)
                if folded and not _is_bare_ambiguous(folded):
                    out.add(folded)
    return frozenset(out)


def _is_bare_ambiguous(folded_title: str) -> bool:
    """Is this title one our OWN market evidence says is ambiguous?

    O*NET lists "Business Analyst" and "Technology Analyst" under computer
    SOC codes, and it is not wrong to: those ARE computer occupations in the
    survey's frame. But in a job board they are used across finance,
    operations and IT, and `_AMBIGUOUS_TITLE_RE` exists because reading them
    as software produced exactly the false positives this taxonomy was built
    to stop.

    So O*NET is trusted EXCEPT where the ambiguous pattern covers most of the
    title. "business analyst" is entirely the ambiguous phrase and is
    excluded; "applications systems analyst" contains it only as a suffix and
    is kept, because the qualifier is what makes it technical.
    """
    m = _AMBIGUOUS_TITLE_RE.search(folded_title)
    if not m:
        return False
    # 0.4, measured against the titles in the golden set rather than picked:
    # "technology analyst" is 100% the ambiguous phrase, "business analyst" is
    # 44%, "analyst programmer" 39% and "applications systems analyst" 25%.
    # The first two must be excluded and the last two kept, so the boundary
    # sits between 0.39 and 0.44.
    return (m.end() - m.start()) >= 0.4 * len(folded_title)

#: A token in the TITLE ITSELF that makes a technical reading positive rather
#: than merely permitted.
#:
#: The defect this fixes
#: ---------------------
#: An exact O*NET alternate title mapped to one of our computer SOC codes was
#: sufficient evidence on its own — no body, no skills, no department. For most
#: titles that is right and it is the only signal that reaches a title our
#: phrase rules do not name. For a handful it is badly wrong, because O*NET is
#: a survey of OCCUPATIONS and a job board is a market of INDUSTRIES:
#:
#:     Field Service Engineer      81 live postings, classified `core` with no
#:                                 family, on the title alone. The same title
#:                                 is used for medical imaging, HVAC,
#:                                 semiconductor capital equipment, elevators
#:                                 and photocopiers.
#:     Distribution Engineer       power distribution
#:     Transmission Engineer       power transmission
#:     Utilization Review Coord.   clinical
#:     Intelligence Analyst        not a data analyst
#:
#: The fix is POSITIVE EVIDENCE, not a blocklist. A blocklist of titles would
#: have to be maintained against a market that invents new ones, and would say
#: nothing about why. This asks the opposite question: does the title itself
#: name a technical domain? "IT Business Analyst", "Data Analytics Manager",
#: "Computer Support Specialist" and "Clinical Informatics Specialist" all do,
#: and all keep their `core` reading with no body at all. "Field Service
#: Engineer" does not, so it needs the posting to say something technical —
#: which "Field Service Engineer, Semiconductor Capital Equipment" does, and
#: which "Field Service Engineer, Elevators" never will.
#:
#: Measured on the live inventory: 35 distinct titles / 207 active postings
#: reached `core` on an O*NET match with no other evidence.
_TITLE_TECH_TOKEN_RE = _compile_alternation((
    "software", "developer", "programmer", "engineer(ing)? manager",
    "\\bit\\b", "information technology", "computer", "comput(ing|ational)",
    "data", "database", "analytics", "\\bbi\\b", "business intelligence",
    "network(ing)?", "systems? admin", "sysadmin", "infrastructure",
    "cloud", "devops", "sre", "site reliability", "platform",
    "security", "cyber", "infosec", "appsec", "vulnerability", "penetration",
    "informatics", "machine learning", "\\bml\\b", "\\bai\\b",
    "artificial intelligence", "\\bnlp\\b", "data science", "scientist",
    "web", "frontend", "front-end", "backend", "back-end", "full[- ]?stack",
    "mobile", "ios", "android", "embedded", "firmware", "silicon", "asic",
    "\\brtl\\b", "fpga", "semiconductor", "verification", "\\bqa\\b",
    "quality engineering", "test automation", "\\bsdet\\b",
    "technical support", "help ?desk", "service desk", "desktop support",
    "salesforce", "sap", "oracle", "workday", "erp", "crm",
    # `applications? <role noun>` for a NARROW set of nouns, never bare
    # "application". "Application Analyst" is an O*NET computer-SOC title and
    # must be rescued; "Application Manager" is as often a loan-application
    # manager; "Application Engineer" is, per the golden set, "often pre-sales
    # at an instrument vendor" and is deliberately left `unknown`. The `\b`
    # is load-bearing — `_compile_alternation` escapes any phrase that does
    # not contain one, so a phrase written as a regex without it becomes a
    # literal that matches nothing.
    r"\bapplications?[\s\-]+(?:systems?[\s\-]+)?(?:analyst|developer|"
    r"programmer|administrator|architect|dba)\b",
    "digital", "technology", "technical", "architect", "api", "\\bui\\b",
    "\\bux\\b", "\\bsql\\b", "python", "java", "\\bqubit\\b",
    "quantum",
))

#: A qualifier that vetoes a TECHNICAL reading of a title whose role noun the
#: phrase rules matched. Each entry declares WHICH families it vetoes, because
#: a qualifier is domain-specific: "spacecraft" vetoes a cloud-infrastructure
#: reading of "Systems Engineer" and says nothing about a security role.
#:
#: The physical-engineering veto already worked this way and had exactly one
#: qualifier set covering exactly one family group. Generalising it is what
#: these eight measured live titles needed — every one of them was classified
#: `core` with high confidence:
#:
#:     Space Systems Engineer                       -> cloud infrastructure
#:     Principal Systems Engineer (GNC Analyst)     -> cloud infrastructure
#:     Product Engineer - Air/Water Cooled Chiller  -> general software
#:     Principal/Engineer Energetics Manufacturing  -> principal/staff IC
#:     Assistant Director of Engineering, <hotel>   -> engineering management
#:     Forecasting, Budgeting & Reporting Analyst   -> business intelligence
#:     Principal Corporate Counsel: Product Security-> product security
#:     M365 Endpoint Admin, Dept of Cybersecurity,
#:       Adjunct Faculty                            -> secops
#:
#: `non_tech` is the one verdict the feed acts on rather than reports, so a
#: false `core` here is the expensive direction: it puts a hotel's engineering
#: director in front of a backend engineer.
_TECHNICAL_FAMILIES = frozenset({
    F_SOFTWARE, F_CLOUD, F_DATA, F_AI_ML, F_SECURITY, F_QUALITY,
    F_TECH_LEADERSHIP, F_UX_DESIGN_SYSTEMS, F_SEMICONDUCTOR,
    F_TECHNICAL_PRODUCT, F_SOLUTIONS_ENG, F_IT_SUPPORT,
})

_DOMAIN_QUALIFIERS: Tuple[Tuple[re.Pattern, frozenset, str], ...] = (
    # Aerospace / spacecraft. "Systems Engineer" is one of the most
    # overloaded titles in the market and cloud `infrastructure` is the
    # reading it falls into.
    (_compile_alternation((
        "spacecraft", "space systems", "satellite", "launch vehicle",
        "avionics", "propulsion", "\\bgnc\\b", "guidance navigation",
        "flight systems", "flight test", "aerodynamics", "airframe",
        "energetics", "munitions", "ordnance",
    )), _TECHNICAL_FAMILIES - {F_SEMICONDUCTOR}, "aerospace/defence hardware"),
    # Building services, HVAC and hospitality plant.
    (_compile_alternation((
        "chiller", "air cooled", "water cooled", "\\bhvac\\b", "boiler",
        "refrigeration", "plumbing", "elevator", "escalator",
        "hotel", "resort", "\\binn\\b", "suites", "lodge", "hospitality",
        "housekeeping", "banquet", "guest services",
    )), _TECHNICAL_FAMILIES, "building services or hospitality"),
    # Finance and accounting. Vetoes a DATA reading only — a security
    # engineer at a bank is still a security engineer.
    (_compile_alternation((
        "budgeting", "forecasting, budgeting", "\\bfp&a\\b",
        "financial planning and analysis", "general ledger", "accounts payable",
        "accounts receivable", "treasury", "underwriting", "actuarial",
    )), frozenset({F_DATA}), "finance and accounting"),
    # Legal.
    (_compile_alternation((
        "corporate counsel", "general counsel", "\\bcounsel\\b", "attorney",
        "paralegal", "solicitor", "barrister",
    )), _TECHNICAL_FAMILIES, "legal"),
    # Teaching. The SUBJECT of a course is not the occupation.
    (_compile_alternation((
        "adjunct faculty", "adjunct professor", "professor", "lecturer",
        "instructor", "teaching assistant", "curriculum", "\\bfaculty\\b",
    )), _TECHNICAL_FAMILIES, "teaching"),
    # Heavy industry sales engineering. `solutions_engineering` is a real
    # adjacent occupation; "Drilling Solutions Engineer" is not it.
    (_compile_alternation((
        "drilling", "oilfield", "downhole", "wellbore", "marine and offshore",
        "offshore", "mining", "\\bdredging\\b",
    )), frozenset({F_SOLUTIONS_ENG, F_CLOUD, F_SOFTWARE}), "heavy industry"),
)


#: Role nouns whose MEANING COMES FROM THE QUALIFIER. These are the only
#: phrases a domain qualifier is allowed to veto.
#:
#: "Systems Engineer" is a spacecraft job or a Linux job depending entirely on
#: the words beside it; "Backend Engineer" is a backend job beside any words
#: at all. Without this distinction the veto over-fires on exactly the titles
#: it must never touch — measured on this file's own sanity sweep, "Backend
#: Engineer, Satellite Ground Software" and "Data Scientist, Actuarial
#: Modeling" both came back `non_tech`, which is a worse error than the one
#: the veto was added to fix: `non_tech` is the one verdict the feed ACTS on.
#:
#: A generic noun plus a domain word is a job in that domain. A named
#: discipline plus a domain word is that discipline, working in that domain,
#: and that is most of the technical market.
_GENERIC_ROLE_PHRASES: Tuple[str, ...] = (
    "systems engineer", "system engineer", "product engineer",
    "principal engineer", "principal/engineer", "staff engineer",
    "senior engineer", "lead engineer", "engineer",
    "engineering manager", "director of engineering", "head of engineering",
    "vp of engineering", "engineering director",
    "reporting analyst", "business intelligence analyst", "analyst",
    "architect", "solutions engineer", "solution engineer", "sales engineer",
    "product security", "cybersecurity", "security", "technical lead",
    "project engineer", "test engineer", "application engineer",
)
_GENERIC_ROLE_SET = frozenset(_GENERIC_ROLE_PHRASES)

#: A role noun that NAMES ITS DISCIPLINE: a discipline word immediately
#: followed by a role noun. "backend engineer", "data scientist", "firmware
#: engineer".
#:
#: Deliberately narrower than "anything not in `_GENERIC_ROLE_SET`". That
#: looser test cleared the non-tech veto for "Executive Assistant to the CTO"
#: — `cto` is not a generic role phrase, but it is a SENIORITY marker and not
#: a discipline, and the golden set's whole B stratum exists to keep that row
#: non-technical. A rule about disciplines has to be written in terms of
#: disciplines.
_DISCIPLINE_NOUN_RE = re.compile(
    r"(?<![a-z0-9])(?:backend|back[\s-]?end|frontend|front[\s-]?end|"
    r"full[\s-]?stack|mobile|ios|android|embedded|firmware|data|"
    r"machine[\s-]learning|deep[\s-]learning|devops|sre|site[\s-]reliability|"
    r"cloud|platform|infrastructure|network|application|qa|quality|game|"
    r"graphics|robotics|perception|quantum|asic|soc|rtl|fpga|silicon|"
    r"verification|compiler|database|web|systems?[\s-]programming|"
    r"distributed[\s-]systems)"
    r"[\s\-/]+"
    r"(?:software[\s\-]+)?"
    r"(?:engineer|developer|programmer|scientist|architect|analyst|"
    r"administrator|dba)(?![a-z0-9])",
    re.IGNORECASE)


def names_its_discipline(phrase: Any) -> bool:
    """Does this matched role noun name the discipline itself?"""
    return bool(_DISCIPLINE_NOUN_RE.search(str(phrase or "")))


def domain_veto(title_blob: str, families: Iterable[str],
                matched_phrases: Iterable[str] = ()) -> Optional[Tuple[str, str]]:
    """`(qualifier, label)` when a declared qualifier vetoes these families.

    `matched_phrases` is the role noun (or nouns) the title actually matched.
    When ANY of them names a discipline outright — `backend engineer`,
    `data scientist`, `machine learning engineer` — no qualifier applies: a
    backend engineer at a satellite company is a backend engineer. The veto
    is for GENERIC nouns only; see `_GENERIC_ROLE_PHRASES`.

    Passing no phrases keeps the old behaviour, which is what the
    `token_role` guard in `classify` wants: there is no role noun there to
    judge, only a token.
    """
    phrases = [str(p or "").strip().lower() for p in (matched_phrases or ())]
    if any(names_its_discipline(p) for p in phrases):
        return None
    fams = {str(f) for f in families}
    for pattern, vetoed, label in _DOMAIN_QUALIFIERS:
        if not (fams & vetoed):
            continue
        m = pattern.search(title_blob)
        if m:
            return m.group(0), label
    return None


#: A generic role noun. Paired with `_TITLE_TECH_TOKEN_RE` it is positive
#: evidence FROM THE TITLE that a posting is technical, without the title
#: naming a specialty the phrase rules know.
#:
#: The two together, and never either alone. A tech token alone reads
#: "Retail Display Installer - Technology" as technical; a role noun alone
#: reads every manager in the market as technical. Together they reach the
#: titles the live sample showed the classifier declining: "IT CSV Engineer",
#: "Lead Data & Cloud Solutions Senior Engineer", "Senior Quantitative Model
#: Solutions Specialist - Python/R or SAS", "University Laboratory School
#: Database and Student Information System Manager".
#:
#: It is reached only AFTER the non-tech title veto and the domain-qualifier
#: veto have both declined to fire, which is what keeps "Digital Marketing
#: Specialist" and "Technical Sales Intern" out.
_GENERIC_ROLE_NOUN_RE = _compile_alternation((
    "engineer", "developer", "programmer", "analyst", "architect",
    "administrator", "scientist", "specialist", "manager", "consultant",
    "\\blead\\b", "\\bdba\\b",
))

#: An explicitly IT/computing role phrase. Suppresses a DOMAIN qualifier veto,
#: because an IT job at an aerospace company is an IT job: "IT Systems
#: Engineer - Millennium Space Systems" was vetoed by `space systems` in the
#: EMPLOYER's name and read as non-technical.
_EXPLICIT_IT_ROLE_RE = _compile_alternation((
    "it systems engineer", "it engineer", "it manager", "it analyst",
    "it specialist", "it support", "it administrator", "it architect",
    "software engineer", "software developer", "network engineer",
    "systems administrator", "cloud engineer", "data engineer",
    "security engineer", "devops engineer", "site reliability engineer",
))

#: Pre-sales is an adjacent occupation, not core architecture. The title says
#: so and the classifier read past it: "Pre-Sales Solutions Architect" and
#: "Senior Solution Architect (pre-sales)" both landed in
#: `technical_leadership/architecture`, which is the IC role they are not.
_PRESALES_RE = _compile_alternation((
    "pre sales", "presales", "pre-sales", "sales engineer", "sales engineering",
    "solution sales", "solutions sales",
))

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
    # "Member of Technical Staff" is the canonical IC engineering title at
    # OpenAI, Anthropic, VMware, AMD, Oracle and Bell Labs' descendants, and
    # it named NO specialty at all — `unknown`, confidence 0.25, so a
    # résumé carrying it contributed nothing to role alignment.
    #
    # It is a HINT and not a phrase-table row on purpose. The reviewed golden
    # set labels a bare "Member of Technical Staff" with no corroboration
    # `unknown`, and it is right to: the title says the person is staff, not
    # what they do, and "Member of Technical Staff (TPM, Inference)" is a
    # program-management role. As a hint it resolves to software exactly when
    # something else on the posting or résumé is technical, and stays unknown
    # when nothing is. Listed LAST so the TPM and consultant hints above win
    # a title that names both.
    (_compile_alternation(("member of technical staff",
                           "member of the technical staff",
                           "members of technical staff")),
     F_SOFTWARE, "general_software"),
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
    # Quantum, photonic and fab context. Measured live: "Lithography Process
    # Engineer - Quantum Program" and "Sr. Quantum 3D Integration and
    # Packaging Engineer" were classified **non_tech** — vetoed by
    # `process engineer` and `packaging engineer`, which are correctly
    # non-technical in a plant and are exactly the semiconductor_hardware
    # family in a fab. Hardware context is what separates the two, and it
    # already existed; it simply did not know these words.
    "quantum", "qubit", "cryogen", "photonic", "photonics", "lithograph",
    "cleanroom", "clean room", "\\bfab\\b", "superconduct", "\\bmems\\b",
    "epitax", "\\bcmos\\b", "die attach", "wire bond",
))

# Seniority is `fit_score.level_from_signals` - one classifier for the filter,
# the card and the scorer (see `classify`).
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
    """Lowercase, diacritics FOLDED, punctuation-flattened, single-spaced.

    Folding rather than deleting matters on a job board that is not only in
    English. The class `[^a-z0-9...]` turned every accented letter into a
    space, so "Préposé à l'entretien ménager" arrived as "pr pos e l entretien
    m nager" — a string no rule can match and no human would write. Live
    inventory carries French, German, Spanish and Portuguese titles, and every
    one of them was being shredded before any rule saw it.

    NFKD + dropping the combining marks is the standard fold and it is what
    `normalize_country` already does for the same reason.
    """
    s = unicodedata.normalize("NFKD", str(text or "").lower())
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.replace("&", " and ").replace("\u00df", "ss")
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


# ---------------------------------------------------------------------------
# Role-title normalisation (for PHRASE MATCHING, not for display)
#
# The phrase table names DISCIPLINES: "data scientist", "backend engineer",
# "site reliability engineer". A real title carries a discipline wrapped in
# employment modifiers - "Data Science Intern", "SDE II", "Sr. Software
# Engineer (Payments), Seattle" - and the modifier is what made the phrase
# unreachable. Measured on the live account this was written for, BOTH résumé
# titles ("Data Science Intern", "Data Quality Specialist Intern") classified
# to no family at all, so `classify_resume` returned tech_scope=unknown, so
# `fit_score`'s largest component (role_alignment, 25/100) was `unknown` for
# EVERY posting and the score collapsed to skill-overlap noise.
#
# The modifier is NOT thrown away. Seniority (`fit_score.level_from_signals`)
# still reads the ORIGINAL title, so "Data Science Intern" keeps `seniority="intern"`; this only stops
# the intern suffix from destroying family detection. Two separate questions,
# two separate readings of the same string.
# ---------------------------------------------------------------------------

#: Employment-shape and seniority modifiers, stripped from either end of a
#: title. Every one of these describes the EMPLOYMENT, not the discipline.
#: Ordered longest-first inside the pattern by `_compile_alternation`.
_TITLE_MODIFIER_PHRASES: Tuple[str, ...] = (
    # employment shape
    "intern", "internship", "interns", "co-op", "coop", "co op",
    "co-op student", "coop student", "student worker", "working student",
    "student", "trainee", "traineeship", "apprentice", "apprenticeship",
    "graduate trainee", "industrial placement", "placement",
    "summer analyst", "summer associate", "summer", "winter", "fall", "spring",
    "part time", "part-time", "full time", "full-time", "contract",
    "contractor", "temporary", "temp", "seasonal", "casual", "volunteer",
    "freelance", "consultant intern", "returnship",
    # seniority / rank words
    "senior", "sr", "sr.", "junior", "jr", "jr.", "lead", "principal",
    "staff", "senior staff", "distinguished", "chief", "head of", "head",
    "associate", "assistant", "entry level", "entry-level", "entry",
    "new grad", "new graduate", "graduate", "grad", "early career",
    "experienced", "mid level", "mid-level", "advanced", "expert",
    "apprentice engineer",
)
_TITLE_MODIFIER_RE = _compile_alternation(_TITLE_MODIFIER_PHRASES)

#: Level codes: roman numerals, bare digits, and the vendor ladders
#: ("L3", "SDE-1" handled by the digit rule after the stack token, "T4",
#: "E5", "IC3", "P4", "G7", "Grade 12", "Level II").
_TITLE_LEVEL_CODE_RE = re.compile(
    r"(?<![a-z0-9])(?:"
    r"level\s*[-#]?\s*(?:[ivx]{1,4}|\d{1,2})"
    r"|grade\s*[-#]?\s*\d{1,2}"
    r"|[ivx]{1,4}"                       # I, II, III, IV, V, VI ...
    r"|[lteipg]\s*-?\s*\d{1,2}"          # L3, T4, E5, IC3 (below), P4, G7
    r"|ic\s*-?\s*\d{1,2}"
    r"|\d{1,2}"                          # bare "2" in "Software Engineer 2"
    r")(?![a-z0-9])", re.IGNORECASE)

#: A roman-numeral-looking token that is a real word. `[ivx]{1,4}` matches
#: "i", "v", "x", "ii", "vi", "xi", "ivi"... and also "civil"? No - the
#: boundary prevents that - but it DOES match the standalone word "I" and the
#: Roman numeral "V", which are the only English words in the class. Listed so
#: the intent is visible rather than implied by a regex.
_ROMAN_OK = frozenset({"i", "ii", "iii", "iv", "v", "vi", "vii", "viii",
                       "ix", "x", "xi", "xii"})

#: Segment separators. Everything after the FIRST one is a qualifier, a
#: department, a location or a programme name - "Data Scientist - Fraud,
#: Chicago", "Software Engineer | Payments", "Analyst, Risk Technology".
_TITLE_SEGMENT_RE = re.compile(r"\s*[|;/]\s*|\s+[-–—]\s+|\s*,\s*")

_PARENTHETICAL_RE = re.compile(r"\s*[\(\[\{][^)\]\}]*[\)\]\}]")

#: A stripped title has to still SAY something. "Intern", "Senior", "II" strip
#: to nothing; a one-character residue is noise, not a discipline.
_MIN_STRIPPED_LEN = 3


def strip_title_modifiers(title: Any) -> str:
    """A title with employment/seniority modifiers and level codes removed.

    "Data Science Intern" -> "data science"; "SDE II" -> "sde";
    "Sr. Software Engineer" -> "software engineer";
    "Associate Data Analyst (Contract)" -> "data analyst".

    Returns "" when nothing survives, which the caller must read as "this
    title was ONLY a modifier" and not as a classification.
    """
    s = _norm(title)
    if not s:
        return ""
    s = _TITLE_MODIFIER_RE.sub(" ", s)
    s = _TITLE_LEVEL_CODE_RE.sub(" ", s)
    s = re.sub(r"[\s\-./]+", " ", s).strip()
    if len(s) < _MIN_STRIPPED_LEN:
        return ""
    return s


def title_variants(title: Any) -> List[str]:
    """Progressively simpler readings of one title, best first.

    The RAW title is always index 0, so a caller that tries them in order can
    never be moved off an answer the unmodified title already supports. Each
    later entry removes one more layer of packaging:

        0. the title as written
        1. parentheticals removed          "SWE (Payments)"      -> "swe"
        2. modifiers + level codes removed "Data Science Intern" -> "data science"
        3. 1 and 2 together
        4. the FIRST segment only          "Analyst, Risk Tech"  -> "analyst"
        5. the first segment, stripped

    Duplicates and empties are dropped, so a title with no packaging yields a
    single-entry list and costs one extra `_norm`.
    """
    # Punctuation is what marks the layers, and `_norm` deletes it, so the
    # splitting happens on the ORIGINAL string and `_norm` runs afterwards.
    original = str(title or "")
    raw = _norm(original)
    if not raw:
        return []
    no_paren = _norm(_PARENTHETICAL_RE.sub(" ", original))
    head = _norm((_TITLE_SEGMENT_RE.split(
        _PARENTHETICAL_RE.sub(" ", original)) or [""])[0])
    out: List[str] = []
    for cand in (raw, no_paren, strip_title_modifiers(raw),
                 strip_title_modifiers(no_paren), head,
                 strip_title_modifiers(head)):
        c = (cand or "").strip()
        if c and c not in out:
            out.append(c)
    return out


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


#: Tokens that `_TECH_EVIDENCE_RE` matches and that are NOT evidence of
#: technical WORK. They are still extracted (several of them are real
#: technologies and belong in `skills_required`); they simply may not PROMOTE
#: an otherwise-unrecognised posting to `core`. Same discipline as
#: `fit_score._GENERIC_SKILLS`: the bar is on DISCRIMINATING evidence, not on
#: raw count.
#:
#: Every entry was measured. Over 6,000 live active `core` postings carrying a
#: readable body, token frequency was compared between the 4,458 that resolve
#: to a FAMILY (genuinely technical) and the 1,542 that do not (the suspect
#: class). The lift of each token in the second group against the first:
#:
#:     eiffel 4.0   routes 3.9   smartsheet 3.5   facebook 2.9   ada 2.9
#:     linkedin 2.7 sharepoint 2.7 route 2.6  zoom 2.6  slack 2.5
#:     sas 2.3      youtube 2.2  salesforce 1.9  scheme 1.8
#:     power automate 1.7  dig 1.6  power apps 1.5  go 1.5  chef 1.2
#:
#: Three kinds, and the reason is different for each:
#:
#: * BOILERPLATE BRANDS - `linkedin` appears in 11.8% of the no-family bodies
#:   and 4.1% of the technical ones, because every JD ends with "follow us on
#:   LinkedIn". Finding it says nothing about the work.
#: * FALSE FRIENDS - ordinary words that collide with a technology's name.
#:   `go` is the biggest by volume (20.4% vs 13.2%) and is usually the English
#:   verb; `ada` is the Americans with Disabilities Act in the EEO paragraph;
#:   `scheme`/`schemes` is a benefits scheme; `routes` is a delivery route;
#:   `sas` is the French corporate form in a company name; `vite` and `rapide`
#:   are French adverbs; `forth`, `dig`, `chef`, `tempo`, `magic`, `enclave`,
#:   `bedrock` and `eiffel` are all just words.
#: * BUSINESS-APPLICATION PLATFORMS used AS a business tool. `salesforce`,
#:   `power apps`, `power automate` and `smartsheet` in a sales or operations
#:   body describe the system the team USES. This costs a real Salesforce or
#:   Power Platform developer nothing, because `_TITLE_TECH_TOKEN_RE` already
#:   admits `salesforce`, `sap`, `oracle`, `workday`, `erp` and `crm` from the
#:   TITLE, which is where that job announces itself.
#:
#: NOT here, deliberately, and each was checked: `sql`, `python`, `tableau`,
#: `matlab`, `agile`, `jira`, `servicenow`, `abap`, `spark`, `verilog`,
#: `asic`, `perl`, `algorithms`, `machine learning`. Several of them lift
#: slightly toward the no-family class, and every one of them is a thing a
#: genuinely technical Analyst or Specialist posting would name about its own
#: work. Removing evidence is how a reachable job disappears.
_WEAK_TECH_TERMS: frozenset = frozenset({
    # boilerplate brands
    "linkedin", "facebook", "youtube", "twitter", "instagram", "tiktok",
    "zoom", "slack", "sharepoint", "teams",
    # false friends
    "go", "ada", "scheme", "route", "dig", "chef", "forth", "tempo",
    "magic", "vite", "rapide", "enclave", "bedrock", "eiffel", "sas",
    # business-application platforms, as a business tool
    "salesforce", "power apps", "powerapps", "power automate", "smartsheet",
})


def discriminating_terms(terms: Iterable[Any]) -> List[str]:
    """The subset of `_skills_in` output that may PROMOTE a posting to core.

    Plural surfaces are folded, because `_compile_alternation` absorbs a
    trailing `s`/`es` into the match and "schemes"/"routes" would otherwise
    walk straight past the set that names "scheme"/"route".
    """
    out: List[str] = []
    for raw in terms or ():
        t = re.sub(r"[\s\-/]+", " ", str(raw or "").lower()).strip()
        if not t or t in _WEAK_TECH_TERMS:
            continue
        if t.endswith("es") and t[:-2] in _WEAK_TECH_TERMS:
            continue
        if t.endswith("s") and t[:-1] in _WEAK_TECH_TERMS:
            continue
        out.append(str(raw))
    return out


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


#: Phrases that name a DISCIPLINE rather than a role. They earn a family on
#: their own ("Data Science Intern") and they yield to a role noun from an
#: adjacent family ("TPM, Machine Learning Infrastructure"). See rule 1c.
_BARE_DISCIPLINE_PHRASES = frozenset({
    "data science", "machine learning", "deep learning",
})

#: A stripped title is one inference step further from what the employer
#: wrote, and the confidence says so. Small on purpose: "Data Science Intern"
#: really is a data-science role, it was just wearing a suffix.
_VARIANT_CONFIDENCE_PENALTY = 0.04

#: How many stripped readings to try. `title_variants` returns at most six and
#: the raw one is index 0; three retries covers parenthetical, modifier and
#: first-segment stripping without turning one classification into six.
_MAX_TITLE_VARIANTS = 3


def _classify_stripped_title(raw_title: Any, *, description: Any = "",
                             department: Any = "",
                             skills: Optional[Sequence[Any]] = None,
                             requirements: Optional[Dict[str, Any]] = None
                             ) -> Optional[TechClassification]:
    """The first stripped reading of `raw_title` that names a family, or None.

    Returns None — never a guess — when no stripped form classifies. See
    `title_variants` for the ladder and `strip_title_modifiers` for why the
    modifier is removed for MATCHING and kept for SENIORITY.
    """
    variants = title_variants(raw_title)[1:_MAX_TITLE_VARIANTS + 1]
    for cand in variants:
        got = classify(cand, description=description, department=department,
                       skills=skills, requirements=requirements, _variant=True)
        if got.tech_scope in (SCOPE_CORE, SCOPE_ADJACENT) and got.role_families:
            got.classification_confidence = round(
                max(0.0, got.classification_confidence
                    - _VARIANT_CONFIDENCE_PENALTY), 3)
            got.classification_evidence = (
                [_ev("title_modifiers_stripped", "title",
                     f"{_norm(raw_title)} -> {cand}")]
                + list(got.classification_evidence))[:8]
            return got
    return None


def classify(
    title: Any,
    *,
    description: Any = "",
    department: Any = "",
    skills: Optional[Sequence[Any]] = None,
    requirements: Optional[Dict[str, Any]] = None,
    _variant: bool = False,
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
    # ONE reader for years and level, shared with `fit_score` and with
    # `job_postings.seniority`. This module used to keep its own: its own
    # title rules ("architect" = principal here, staff there) and "lowest
    # stated requirement wins", so "5+ years overall, 2+ with AWS" read as 2.
    # The card showed this value, the filter queried the other one, and on
    # 2026-10-02 they disagreed about 29,852 "entry" postings. Imported here,
    # not at module level: fit_score imports this module.
    from services import fit_score as _fs
    _lo, years_max = _years_of(body_blob)
    years_min = _fs._required_years(body_blob) if body_blob else None
    if years_min is None:
        rq = req.get("required_years")
        if isinstance(rq, (int, float)) and 0 <= rq <= 40:
            years_min = int(rq)
    if years_max is not None and years_min is not None and years_max < years_min:
        years_max = None
    result.required_years_min = years_min
    result.required_years_max = years_max
    result.seniority = _fs.level_from_signals(
        raw_title, description=desc_text, required_years=years_min) or "unknown"

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

    # ---- 1b. the same title with its packaging removed ---------------------
    #
    # Only when the title AS WRITTEN named nothing. The raw form always wins,
    # so this can never move a title off an answer it already had; it can only
    # reach a title the phrase table could not see through an intern suffix, a
    # level code or a department tail. The re-read is a full `classify` on the
    # stripped string (never a span transplant, which would make the
    # overlap/veto arithmetic below compare offsets from two different
    # strings), and only a CORE/ADJACENT verdict that names a family is
    # adopted — a `non_tech` reading of a stripped title is a weaker claim
    # than the raw title's silence and is discarded.
    if not title_hits and not _variant:
        alt = _classify_stripped_title(
            raw_title, description=desc_text, department=department,
            skills=skills, requirements=requirements)
        if alt is not None:
            # Seniority, years and skills are read from the ORIGINAL title and
            # body. Stripping "Intern" must cost the intern signal nothing.
            alt.normalized_title = result.normalized_title
            alt.seniority = result.seniority
            alt.required_years_min = result.required_years_min
            alt.required_years_max = result.required_years_max
            alt.skills_required = result.skills_required
            alt.skills_preferred = result.skills_preferred
            return alt

    # ---- 1c. a bare DISCIPLINE noun qualifies a role, it does not replace it -
    #
    # "data science", "machine learning" and "deep learning" were added to the
    # phrase table so "Data Science Intern" and "Machine Learning Co-op" stop
    # reading as nothing. They are domain words, not role nouns, and when the
    # title ALSO names an adjacent-family role the role is the job and the
    # discipline is the subject matter: "Senior Technical Program Manager,
    # Machine Learning Infrastructure" is a TPM, and "Principal Forward
    # Deployed Engineer - Data Science" is a forward-deployed engineer. Both
    # are labelled `adjacent` by the reviewer in `eval_live_titles`, and
    # without this the core discipline label captured the scope.
    if len(title_hits) > 1:
        bare = [h for h in title_hits
                if re.sub(r"[\s\-/]+", " ", h[2].lower()).strip()
                in _BARE_DISCIPLINE_PHRASES]
        if bare and len(bare) < len(title_hits) and any(
                FAMILY_SCOPE.get(f) == SCOPE_ADJACENT for f, _s, _p in title_hits):
            evidence.append(_ev("discipline_qualifies_adjacent_role", "title",
                                f"{bare[0][2]} qualifies "
                                f"{[h[2] for h in title_hits if h not in bare][0]}"))
            title_hits = [h for h in title_hits if h not in bare]

    physical = bool(_PHYSICAL_QUALIFIER_RE.search(title_blob))
    hardware_ctx = bool(_HARDWARE_CONTEXT_RE.search(title_blob)
                        or _HARDWARE_CONTEXT_RE.search(body_blob[:4000]))

    # ---- 2. non-tech veto on the TITLE -------------------------------------
    #
    # `hardware_ctx` is consulted HERE as well as at step 1. It was only ever
    # read at step 1, so a title carrying both a non-technical role noun AND
    # unmistakable fab/quantum context was vetoed before the context could be
    # heard: "Lithography Process Engineer - Quantum Program" matched
    # `process engineer` and returned `non_tech` at confidence 0.93, and
    # "Sr. Quantum 3D Integration and Packaging Engineer" matched
    # `packaging engineer` and did the same. Both are semiconductor_hardware
    # roles, and both were reported with high confidence as not technical at
    # all — the most expensive kind of wrong, because `non_tech` is the one
    # verdict the feed acts on rather than reports.
    #
    # The veto still fires for every title WITHOUT that context: a bare
    # "Process Engineer" optimising a polymerization line stays non-technical,
    # which is the case the veto was written for.
    nt = _NON_TECH_RE.search(title_blob)
    if nt and not title_hits:
        if not (hardware_ctx and _NON_TECH_CATEGORY.get(nt.group(0).lower())
                == "physical_engineering"):
            phrase = nt.group(0)
            result.tech_scope = SCOPE_NON_TECH
            result.classification_confidence = 0.93
            result.classification_evidence = [
                _ev("non_tech_title", "title", phrase)]
            return result
        ctx_hit = _HARDWARE_CONTEXT_RE.search(title_blob + " " + body_blob[:4000])
        evidence.append(_ev("hardware_context_overrides_physical_veto", "title",
                            ctx_hit.group(0) if ctx_hit else "hardware context"))
        semi_spec = _PHYSICAL_TO_SEMICONDUCTOR.get(nt.group(0).lower())
        if semi_spec:
            evidence.append(_ev("physical_role_in_hardware_context", "title",
                                f"{nt.group(0)} -> {semi_spec}"))
            result.tech_scope = SCOPE_CORE
            result.role_family = F_SEMICONDUCTOR
            result.role_families = [F_SEMICONDUCTOR]
            result.role_specialties = [semi_spec]
            result.occupation_codes = list(
                SPECIALTY_OCCUPATION_CODES.get(semi_spec, ()))
            # Lower than a direct title match (0.88-0.95): this is a role noun
            # plus a context word, which is weaker than a phrase that names
            # the specialty outright, and the confidence has to say so.
            result.classification_confidence = 0.72
            result.classification_evidence = evidence[:8]
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

        # The same veto, generalised: a DECLARED domain qualifier beats a
        # technical role noun for the families that qualifier covers. See
        # `_DOMAIN_QUALIFIERS` for the eight live titles this exists for.
        vetoed = domain_veto(title_blob, (f for f, _s, _p in title_hits),
                             (p for _f, _s, p in title_hits))
        if vetoed and not hardware_ctx and not _EXPLICIT_IT_ROLE_RE.search(title_blob):
            qualifier, label = vetoed
            result.tech_scope = SCOPE_NON_TECH
            result.classification_confidence = 0.8
            result.classification_evidence = [
                _ev("domain_qualifier_veto", "title", f"{qualifier} ({label})"),
                _ev("role_noun", "title", title_hits[0][2]),
            ]
            return result

        # Pre-sales architecture is `solutions_engineering`, which is adjacent,
        # not `architecture`, which is core. The title says which; re-file it
        # rather than dropping it, because the role is real and selectable.
        if _PRESALES_RE.search(title_blob) and any(
                sp == "architecture" for _f, sp, _p in title_hits):
            evidence.append(_ev("presales_qualifier", "title",
                                _PRESALES_RE.search(title_blob).group(0)))
            title_hits = [(F_SOLUTIONS_ENG, "solutions_engineering",
                           _PRESALES_RE.search(title_blob).group(0))] + [
                h for h in title_hits if h[1] != "architecture"]

        # Ambiguity guard: a title whose ONLY hit came from a broad phrase and
        # which also reads as a non-tech occupation needs body evidence.
        #
        # Unless the tech phrase CONTAINS the non-tech one. "Advanced Packaging
        # Engineer" matches the semiconductor rule across the whole title and
        # the non-tech rule across its last two words; that is the same
        # `_dedupe_overlapping` situation as "Design Systems Engineer", one
        # boundary further out. The longer phrase is the more specific reading
        # of the same words, not a second, conflicting reading of them.
        if nt and any(start <= nt.start() and nt.end() <= end
                      for _f, _s, _p, start, end in spanned):
            nt = None
        # Nor is it a conflict when the "non-tech" noun IS the hardware job.
        # "Sr. Quantum 3D Integration and Packaging Engineer" matched
        # `quantum_hardware` on its first half and `packaging engineer` on its
        # second, and the two do not overlap — so the guard above could not
        # see that they are the same role described twice. A semiconductor
        # title plus a physical-engineering noun that `_PHYSICAL_TO_
        # SEMICONDUCTOR` already maps into the SAME family is not two readings
        # in conflict; it is one reading with two names, and the second name
        # adds a specialty rather than removing the first.
        if nt and any(f == F_SEMICONDUCTOR for f, _s, _p in title_hits):
            mapped = _PHYSICAL_TO_SEMICONDUCTOR.get(nt.group(0).lower())
            if mapped:
                evidence.append(_ev("physical_role_in_hardware_context", "title",
                                    f"{nt.group(0)} -> {mapped}"))
                if not any(sp == mapped for _f, sp, _p in title_hits):
                    title_hits = list(title_hits) + [
                        (F_SEMICONDUCTOR, mapped, nt.group(0))]
                nt = None
        # A NAMED DISCIPLINE is not in conflict with a domain word. "Data
        # Scientist, Actuarial Modeling" matched `data scientist` and
        # `actuarial`, and read `unknown` for want of a body — but a data
        # scientist doing actuarial modelling is a data scientist. Same rule
        # as `domain_veto`: only a GENERIC role noun takes its meaning from
        # the words beside it. "Physical Security Officer" is unaffected,
        # because `security` IS generic.
        if nt and title_hits and any(
                names_its_discipline(p) for _f, _s, p in title_hits):
            evidence.append(_ev("named_discipline_over_domain_word", "title",
                                f"{title_hits[0][2]} vs {nt.group(0)}"))
            nt = None
        if nt:
            has_body = bool(_TECH_EVIDENCE_RE.search(body_blob)
                            or _TECH_DEPARTMENT_RE.search(dept_blob)
                            or skills_blob
                            or title_blob in onet_titles())
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
    # What may PROMOTE is narrower than what is EXTRACTED. See
    # `_WEAK_TECH_TERMS` for the measurement behind the difference.
    strong_terms = discriminating_terms(tech_terms)
    dept_hit = _TECH_DEPARTMENT_RE.search(dept_blob)

    if not ambiguous and not body_hits and not strong_terms:
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
    # An exact O*NET alternate title for one of the occupations we classify is
    # documentary evidence that the title is technical, and it is the only
    # signal that reaches a title the phrase rules do not name at all. It is
    # filtered to OUR SOC codes at import, which is what keeps "Informatics
    # Nurse Specialist" (a clinical SOC) out while letting "Applications
    # Systems Analyst" (a computer SOC) in.
    # An O*NET alternate title counts ON ITS OWN only when the title itself
    # names a technical domain. Without that token it is a title that CAN be a
    # computer occupation, which is not the same as a posting that IS one, and
    # the posting still has to say something technical. See
    # `_TITLE_TECH_TOKEN_RE`.
    onet_title = title_blob in onet_titles()
    title_token = _TITLE_TECH_TOKEN_RE.search(title_blob)
    onet_token = bool(title_token)
    onet_sufficient = onet_title and onet_token
    # A technical DOMAIN token plus a role noun, both in the title, and no
    # veto has fired. See `_GENERIC_ROLE_NOUN_RE`.
    token_role = bool(title_token and _GENERIC_ROLE_NOUN_RE.search(title_blob)
                      and not domain_veto(title_blob, _TECHNICAL_FAMILIES)
                      # "Technology Analyst" is a technical token and a role
                      # noun and is still ambiguous — `_is_bare_ambiguous`
                      # measured that the ambiguous phrase IS the whole title.
                      # This rule must not undo the one that exists to catch it.
                      and not _is_bare_ambiguous(title_blob))
    strong_enough = (
        bool(body_hits)
        # LOOSE BODY VOCABULARY IS CORROBORATION, NOT EVIDENCE (1.3.0).
        #
        # This clause used to read `len(tech_terms) >= 3` with no condition on
        # the title at all, which is the same mistake `onet_title_without_
        # technical_token` and the department rule below were both written to
        # stop, made one layer further in. `_TECH_EVIDENCE_RE` is mostly the
        # IMPORTED O*NET/overlay vocabulary, and that vocabulary contains
        # consumer brands and ordinary words: `linkedin`, `youtube`,
        # `facebook`, `salesforce`, `chef`, `spark`, `go`, `magic`, `scheme`,
        # `eiffel`, `vite`, `tempo`, `bedrock`, `enclave`, `transformer`.
        # Three of those appear in almost any corporate job description.
        #
        # Measured on 4,000 live active postings already stored as
        # `tech_scope: core`: 925 carried NO family, and 798 of the 925 were
        # promoted by exactly this clause. The evidence strings are what they
        # look like - "Growth Marketer" -> `linkedin, youtube, salesforce,
        # sql`; "Manager Sales - Key Accounts" -> `enclave, eiffel, bedrock`;
        # "Tecnico Elettrico ed Elettronico di Manutenzione" -> `vite, tempo,
        # agile`. Those rows then score on a THIN frame (no family means
        # `role_alignment` is excluded from `_normalized_score`) against a
        # two-or-three-skill requirement list, which is easy to cover, so they
        # land at the TOP of several different users' feeds. "Sanofi VMI
        # Intern" and "Santander GERENCIA JR. DATA ANALYST ALM" were in the
        # top five for three unrelated accounts.
        #
        # A specialty phrase found in the BODY (`body_hits`) is unaffected and
        # is still sufficient alone: that is a named discipline, not loose
        # vocabulary. What is required now is that the TITLE be at least
        # recognisable as possibly-technical before body words may promote it.
        or (len(strong_terms) >= 3 and (ambiguous or onet_title or onet_token))
        or onet_sufficient
        or token_role
        # An O*NET title WITHOUT a technical token still corroborates: paired
        # with a technical department or two technical skill terms it clears
        # the same bar an ambiguous title does. What it may no longer do is
        # clear it alone.
        or (onet_title and (bool(dept_hit) or len(strong_terms) >= 2))
        or (ambiguous and (bool(dept_hit) or len(strong_terms) >= 2))
    )
    if not strong_enough:
        result.tech_scope = SCOPE_UNKNOWN
        result.classification_confidence = 0.25 if (ambiguous or onet_title) else 0.1
        if onet_title and not onet_token:
            result.classification_evidence = [
                _ev("onet_title_without_technical_token", "title",
                    "an O*NET computer-occupation title that names no technical "
                    "domain; the posting itself must say something technical")]
        elif tech_terms:
            result.classification_evidence = [
                _ev("weak_body_signal", "description",
                    ", ".join(strong_terms or tech_terms))]
        return result

    if dept_hit:
        evidence.append(_ev("department", "department", dept_hit.group(0)))
    if strong_terms:
        # The evidence names the DISCRIMINATING terms. Printing `linkedin,
        # youtube, salesforce` as the reason a posting is technical is how
        # this defect stayed invisible: the verdict carried its own refutation
        # and nobody read it.
        evidence.append(_ev("skills", "description", ", ".join(strong_terms[:8])))

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
        if not evidence and (onet_sufficient or token_role):
            evidence.append(_ev(
                "onet_title_with_technical_token" if onet_sufficient
                else "technical_token_and_role_noun", "title",
                title_token.group(0) if title_token else "technical token"))
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


def title_phrases(families: Optional[Iterable[Any]] = None,
                  specialties: Optional[Iterable[Any]] = None) -> List[str]:
    """The canonical title phrases for these families / specialties.

    This is `_SPEC_RULES` read as a VOCABULARY rather than as a classifier.
    Same 967 phrases, same authoring rule (see `_SPEC_RULES`): a phrase is in
    here only if, on its own in a job title, it is unambiguous. That is
    exactly the property a phrase needs to be usable as a SEARCH term, which
    is why the search side should generate against this list instead of
    inventing title strings.

    It exists because the LLM title synthesizer was emitting keyword-style
    strings - "Software Engineer Backend Python", "Cloud Engineer AWS
    Terraform" - which are matched as literal contiguous substrings against
    `job_postings.title_raw` and therefore matched NOTHING. Measured live,
    34 of one account's 44 generated titles hit zero postings.

    Both arguments None returns every phrase. Unknown names are ignored
    rather than raising: a caller passing a stale family must get a smaller
    vocabulary, never an exception in the middle of a search.
    """
    fam_set = {str(f or "") for f in families} if families is not None else None
    spec_set = ({str(s or "") for s in specialties}
                if specialties is not None else None)
    out: List[str] = []
    seen: set = set()
    for family, specialty, phrases in _SPEC_RULES:
        if fam_set is not None and family not in fam_set:
            continue
        if spec_set is not None and specialty not in spec_set:
            continue
        for phrase in phrases:
            key = phrase.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(phrase)
    return out


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


# ===========================================================================
# Specialty adjacency, and the role-alignment ladder
# ===========================================================================
#
# The problem this replaces
# -------------------------
# `fit_score._score_role` scored role fit against its own 32-key
# `ROLE_FAMILIES` list, whose finest software distinction is
# "software_engineering". Under it, a backend engineer and an ASIC verification
# engineer scored IDENTICALLY against a backend posting, because both resolve
# to the same family. That is why the same handful of jobs came back at 80+
# for everyone: the role term was nearly constant, so ranking fell to skill
# overlap alone.
#
# Alignment is now a five-rung ladder over the 14/101 tree:
#
#   exact_specialty     backend resume  -> backend posting
#   adjacent_specialty  backend         -> distributed systems      (declared)
#   same_family         backend         -> mobile                   (same family)
#   adjacent_family     backend         -> data engineering         (declared)
#   unrelated           backend         -> nursing / ASIC design
#
# `unknown` is a SIXTH answer and is deliberately NOT on the ladder: when one
# side carries no taxonomy signal we do not know the distance, and scoring it
# as "unrelated" would punish a résumé for being hard to parse.
#
# Why the matrix is declared, not derived
# ---------------------------------------
# Adjacency is a claim about how hiring managers actually substitute skills,
# and no string metric encodes that. "Backend" and "distributed systems" are
# adjacent; "backend" and "browsers" are the same family and not adjacent.
# Declaring it means it is reviewable and versioned; deriving it would mean
# it silently changes whenever a phrase list does.

ADJACENCY_VERSION = "tech-adjacency-1.3.0"

#: Declared once, applied symmetrically. Each pair is "a team hiring for A
#: would seriously consider a strong B", which is a narrower claim than
#: "related" and a much narrower one than "same family".
_ADJACENT_PAIRS: Tuple[Tuple[str, str], ...] = (
    # ---- software ---------------------------------------------------------
    ("backend", "full_stack"), ("backend", "apis"),
    ("backend", "distributed_systems"), ("backend", "general_software"),
    ("backend", "production_engineering"), ("backend", "database_engineering"),
    # Reach across families for transitions people actually make. Each of
    # these was `unrelated` before, which is a 0.08 weight - i.e. a claim that
    # a backend engineer has no business applying to an AI-platform role.
    ("backend", "ai_platform"), ("backend", "data_engineering"),
    ("frontend", "full_stack"), ("frontend", "web_platform"),
    ("frontend", "design_systems"), ("frontend", "general_software"),
    ("full_stack", "apis"), ("full_stack", "web_platform"),
    ("full_stack", "general_software"),
    ("web_platform", "browsers"),
    ("mobile", "full_stack"), ("mobile", "desktop"), ("mobile", "embedded"),
    ("desktop", "systems_programming"),
    ("distributed_systems", "systems_programming"),
    ("distributed_systems", "platform_engineering"),
    ("distributed_systems", "streaming"),
    ("distributed_systems", "storage_engineering"),
    ("systems_programming", "embedded"), ("systems_programming", "firmware"),
    ("systems_programming", "browsers"), ("systems_programming", "virtualization"),
    ("systems_programming", "malware_reverse_engineering"),
    ("apis", "developer_tools"),
    ("developer_tools", "platform_engineering"),
    ("developer_tools", "build_release"), ("developer_tools", "test_infrastructure"),
    ("developer_tools", "eda_cad"), ("developer_tools", "ai_tooling"),
    ("games_xr", "computer_vision"), ("games_xr", "general_software"),
    ("embedded", "firmware"), ("embedded", "hardware_engineering"),
    ("embedded", "robotics_autonomy"), ("embedded", "fpga_rtl"),
    ("firmware", "hardware_engineering"), ("firmware", "asic_soc_design"),
    ("general_software", "apis"),
    ("enterprise_mainframe", "backend"),
    ("enterprise_mainframe", "general_software"),
    ("enterprise_mainframe", "database_engineering"),
    ("quantitative_low_latency", "systems_programming"),
    ("quantitative_low_latency", "distributed_systems"),
    ("quantitative_low_latency", "hpc"), ("quantitative_low_latency", "backend"),

    # ---- ai / ml ----------------------------------------------------------
    ("ml_engineering", "applied_ai"), ("ml_engineering", "mlops_llmops"),
    ("ml_engineering", "ai_platform"), ("ml_engineering", "data_engineering"),
    ("ml_engineering", "research_engineering"),
    ("ml_engineering", "recsys_search_ranking"), ("ml_engineering", "ml_data"),
    ("applied_ai", "genai_llm"), ("applied_ai", "ai_agents"),
    ("applied_ai", "ai_tooling"),
    ("research_engineering", "research_science"),
    ("research_engineering", "inference_performance"),
    ("research_science", "data_science"), ("research_science", "evaluations"),
    ("genai_llm", "ai_agents"), ("genai_llm", "ai_tooling"),
    ("genai_llm", "nlp"), ("genai_llm", "evaluations"),
    ("nlp", "speech"), ("speech", "computer_vision"),
    ("computer_vision", "robotics_autonomy"),
    ("recsys_search_ranking", "data_science"),
    ("mlops_llmops", "devops"), ("mlops_llmops", "ai_platform"),
    ("ai_platform", "platform_engineering"), ("ai_platform", "infrastructure"),
    ("inference_performance", "hpc"),
    ("inference_performance", "systems_programming"),
    ("ai_safety", "red_teaming"), ("ai_safety", "evaluations"),
    ("ai_safety", "ai_security"),
    ("red_teaming", "penetration_testing"), ("red_teaming", "ai_security"),
    ("ai_agents", "ai_tooling"),

    # ---- data -------------------------------------------------------------
    ("data_engineering", "data_platform"),
    ("data_engineering", "analytics_engineering"),
    ("data_engineering", "streaming"), ("data_engineering", "ml_data"),
    ("data_platform", "platform_engineering"),
    ("data_platform", "infrastructure"), ("data_platform", "database_engineering"),
    ("analytics_engineering", "business_intelligence"),
    ("analytics_engineering", "data_science"),
    ("data_science", "business_intelligence"),
    # 1.3.0. A data scientist and an ML engineer were `adjacent_family`
    # (0.46) — a claim that the two disciplines barely reach each other, when
    # in practice they are the same hiring pool and the same résumés. Measured
    # on the live account that produced this bug (MS Data Science, PyTorch /
    # scikit-learn / NLP / CV), every ML Engineer and AI Engineer posting in
    # the feed sat two rungs below where it belongs. `applied_ai` is included
    # for the same reason and no further: `nlp`, `computer_vision` and
    # `genai_llm` remain a family hop away, because hiring for those does ask
    # for the specialism.
    ("data_science", "ml_engineering"), ("data_science", "applied_ai"),
    ("database_engineering", "sre"),
    ("data_governance", "privacy_engineering"), ("data_governance", "grc"),

    # ---- cloud / platform -------------------------------------------------
    ("cloud_engineering", "devops"), ("cloud_engineering", "infrastructure"),
    ("cloud_engineering", "platform_engineering"), ("cloud_engineering", "finops"),
    ("devops", "sre"), ("devops", "platform_engineering"),
    ("devops", "build_release"), ("devops", "kubernetes_containers"),
    ("sre", "production_engineering"), ("sre", "observability"),
    ("sre", "infrastructure"), ("sre", "performance_testing"),
    ("platform_engineering", "infrastructure"),
    ("platform_engineering", "kubernetes_containers"),
    ("platform_engineering", "test_infrastructure"),
    ("platform_engineering", "architecture"),
    ("infrastructure", "networking"), ("infrastructure", "storage_engineering"),
    ("infrastructure", "virtualization"), ("infrastructure", "hpc"),
    ("infrastructure", "it_systems_administration"),
    ("observability", "developer_tools"),
    ("networking", "edge_cdn"), ("networking", "network_operations"),
    ("networking", "cloud_security"),
    ("cloud_security", "application_security"), ("cloud_security", "secops"),
    ("cloud_security", "iam_pam"),
    ("hpc", "storage_engineering"),
    ("edge_cdn", "web_platform"),
    ("storage_engineering", "database_engineering"),
    ("virtualization", "kubernetes_containers"),

    # ---- security ---------------------------------------------------------
    ("application_security", "product_security"),
    ("application_security", "penetration_testing"),
    ("application_security", "secops"), ("application_security", "ai_security"),
    ("product_security", "cryptography"),
    ("product_security", "penetration_testing"),
    ("iam_pam", "secops"), ("iam_pam", "grc"),
    ("secops", "detection_response"),
    ("detection_response", "malware_reverse_engineering"),
    ("detection_response", "penetration_testing"),
    ("grc", "privacy_engineering"),
    ("privacy_engineering", "cryptography"),

    # ---- quality ----------------------------------------------------------
    ("qa_automation", "general_software"),
    ("qa_automation", "sdet"), ("qa_automation", "test_infrastructure"),
    ("qa_automation", "reliability_qa"),
    ("sdet", "test_infrastructure"), ("sdet", "backend"),
    ("performance_testing", "reliability_qa"),
    ("test_infrastructure", "build_release"),

    # ---- leadership -------------------------------------------------------
    ("engineering_management", "architecture"),
    ("engineering_management", "principal_staff_ic"),
    ("engineering_management", "tpm"),
    ("architecture", "principal_staff_ic"),
    ("architecture", "distributed_systems"),
    # The leadership family had no edge to the ICs it is STAFFED FROM. A
    # "Staff Engineer" or "Cloud Architect" posting is the ordinary next step
    # for a backend or platform engineer - it is the most common promotion in
    # the industry - and with no pair here the specialty bridge could not
    # reach `technical_leadership`, so alignment fell all the way to
    # `unrelated` (0.08). Measured on one live account, 317 of 522 rejected
    # postings were leadership titles the candidate is squarely qualified for.
    ("principal_staff_ic", "backend"), ("principal_staff_ic", "full_stack"),
    ("principal_staff_ic", "general_software"),
    ("principal_staff_ic", "distributed_systems"),
    ("principal_staff_ic", "platform_engineering"),
    ("principal_staff_ic", "ml_engineering"),
    ("architecture", "backend"), ("architecture", "general_software"),
    ("architecture", "platform_engineering"),
    ("architecture", "cloud_engineering"), ("architecture", "infrastructure"),
    ("architecture", "data_platform"), ("architecture", "enterprise_mainframe"),
    ("architecture", "ai_platform"),

    # ---- software / cloud <-> security -------------------------------------
    # Same gap, same consequence: 161 of those 522 were security postings.
    # Application and product security are done BY people who read and write
    # application code, and cloud security by people who run cloud
    # infrastructure. One declared pair per family opens the 0.46
    # adjacent-family rung for the rest through the bridge.
    ("application_security", "backend"),
    ("application_security", "full_stack"),
    ("application_security", "general_software"),
    ("application_security", "apis"),
    ("application_security", "web_platform"),
    ("application_security", "product_security"),
    ("application_security", "penetration_testing"),
    ("product_security", "backend"),
    ("cloud_security", "cloud_engineering"), ("cloud_security", "infrastructure"),
    ("cloud_security", "kubernetes_containers"), ("cloud_security", "devops"),
    ("secops", "sre"), ("secops", "observability"),
    ("secops", "detection_response"), ("secops", "network_operations"),
    ("detection_response", "observability"),
    # The one edge that lets a cloud engineer reach the security family at
    # all: `cloud_security` lives in `cloud_platform`, so without this the
    # bridge found nothing and "Cloud Security Engineer" read as unrelated to
    # cloud engineering. Identity and access work in cloud is cloud work.
    ("iam_pam", "cloud_engineering"),
    ("iam_pam", "infrastructure"), ("iam_pam", "it_systems_administration"),
    ("ai_security", "ai_safety"), ("ai_security", "red_teaming"),
    ("ai_security", "application_security"),

    # ---- adjacent families ------------------------------------------------
    ("technical_product_management", "tpm"),
    ("technical_product_management", "product_design"),
    ("technical_product_management", "solutions_engineering"),
    ("solutions_engineering", "customer_engineering"),
    ("solutions_engineering", "developer_relations"),
    ("customer_engineering", "developer_relations"),
    ("developer_relations", "technical_writing"),
    ("developer_relations", "ai_tooling"),
    ("design_systems", "product_design"),
    ("it_support", "it_systems_administration"),
    ("it_systems_administration", "network_operations"),

    # ---- semiconductor / hardware -----------------------------------------
    ("asic_soc_design", "fpga_rtl"), ("asic_soc_design", "verification"),
    ("asic_soc_design", "eda_cad"), ("asic_soc_design", "analog_mixed_signal"),
    ("fpga_rtl", "verification"),
    ("verification", "product_test"), ("verification", "eda_cad"),
    ("hardware_engineering", "analog_mixed_signal"),
    ("hardware_engineering", "rf_engineering"),
    ("hardware_engineering", "packaging"),
    ("hardware_engineering", "product_test"),
    ("analog_mixed_signal", "rf_engineering"),
    ("analog_mixed_signal", "photonics"),
    ("rf_engineering", "photonics"),
    ("process_yield", "product_test"), ("process_yield", "packaging"),
    ("packaging", "photonics"),

    # --- quantum ---------------------------------------------------------
    # Declared edges, like every other pair here. The quantum cluster is
    # internally connected (a control engineer and a hardware engineer are one
    # step apart) and reaches the rest of the taxonomy through the specialties
    # people actually move between: quantum software <-> systems programming
    # and research engineering; quantum control <-> FPGA/RTL and analog;
    # cryogenic systems <-> hardware engineering and RF.
    ("quantum_software", "quantum_hardware"),
    ("quantum_software", "quantum_control"),
    ("quantum_hardware", "quantum_control"),
    ("quantum_hardware", "cryogenic_systems"),
    ("quantum_control", "cryogenic_systems"),
    ("quantum_software", "systems_programming"),
    ("quantum_software", "research_engineering"),
    ("quantum_software", "hpc"),
    ("quantum_hardware", "asic_soc_design"),
    ("quantum_hardware", "photonics"),
    ("quantum_hardware", "process_yield"),
    ("quantum_hardware", "packaging"),
    ("quantum_control", "fpga_rtl"),
    ("quantum_control", "analog_mixed_signal"),
    ("quantum_control", "embedded"),
    ("cryogenic_systems", "hardware_engineering"),
    ("cryogenic_systems", "rf_engineering"),
)


def _build_adjacency() -> Dict[str, frozenset]:
    out: Dict[str, Set[str]] = {s: set() for s in ALL_SPECIALTIES}
    for a, b in _ADJACENT_PAIRS:
        if a not in out or b not in out:      # pragma: no cover - guarded by a test
            continue
        out[a].add(b)
        out[b].add(a)
    return {k: frozenset(v) for k, v in out.items()}


#: `{specialty: frozenset(adjacent specialties)}`. Symmetric by construction.
SPECIALTY_ADJACENCY: Dict[str, frozenset] = _build_adjacency()


#: `{family: frozenset(adjacent families)}`. Coarser and more forgiving than
#: the specialty matrix: it is the rung BELOW "same family", so it is where a
#: genuine discipline change lands (backend -> data engineering), not where a
#: neighbouring speciality does.
_ADJACENT_FAMILY_PAIRS: Tuple[Tuple[str, str], ...] = (
    (F_SOFTWARE, F_CLOUD), (F_SOFTWARE, F_QUALITY), (F_SOFTWARE, F_DATA),
    (F_SOFTWARE, F_AI_ML), (F_SOFTWARE, F_SEMICONDUCTOR),
    (F_SOFTWARE, F_TECH_LEADERSHIP), (F_SOFTWARE, F_SECURITY),
    (F_SOFTWARE, F_UX_DESIGN_SYSTEMS), (F_SOFTWARE, F_SOLUTIONS_ENG),
    (F_AI_ML, F_DATA), (F_AI_ML, F_CLOUD),
    (F_DATA, F_CLOUD),
    (F_CLOUD, F_SECURITY), (F_CLOUD, F_QUALITY), (F_CLOUD, F_IT_SUPPORT),
    (F_CLOUD, F_SEMICONDUCTOR),
    (F_SECURITY, F_IT_SUPPORT),
    (F_QUALITY, F_TECH_LEADERSHIP),
    (F_TECH_LEADERSHIP, F_TECHNICAL_PRODUCT), (F_TECH_LEADERSHIP, F_AI_ML),
    (F_TECH_LEADERSHIP, F_DATA), (F_TECH_LEADERSHIP, F_CLOUD),
    (F_TECH_LEADERSHIP, F_SECURITY), (F_TECH_LEADERSHIP, F_SEMICONDUCTOR),
    (F_TECHNICAL_PRODUCT, F_SOLUTIONS_ENG), (F_TECHNICAL_PRODUCT, F_UX_DESIGN_SYSTEMS),
    (F_SOLUTIONS_ENG, F_DEVREL),
    (F_DEVREL, F_TECH_WRITING),
    (F_SEMICONDUCTOR, F_QUALITY),
)


def _build_family_adjacency() -> Dict[str, frozenset]:
    out: Dict[str, Set[str]] = {f: set() for f in ALL_FAMILIES}
    for a, b in _ADJACENT_FAMILY_PAIRS:
        out[a].add(b)
        out[b].add(a)
    return {k: frozenset(v) for k, v in out.items()}


FAMILY_ADJACENCY: Dict[str, frozenset] = _build_family_adjacency()


# ---------------------------------------------------------------------------
# The ladder
# ---------------------------------------------------------------------------
ALIGN_EXACT_SPECIALTY = "exact_specialty"
ALIGN_ADJACENT_SPECIALTY = "adjacent_specialty"
ALIGN_SAME_FAMILY = "same_family"
ALIGN_ADJACENT_FAMILY = "adjacent_family"
ALIGN_UNRELATED = "unrelated"
ALIGN_UNKNOWN = "unknown"

#: Human-readable names for the non-tech domains, used in the "why not"
#: explanation a candidate actually reads.
NON_TECH_DOMAIN_LABELS: Dict[str, str] = {
    "clinical": "clinical / healthcare",
    "retail_food_hospitality": "retail, food or hospitality",
    "trades_logistics": "skilled trades or logistics",
    "business_admin": "business, finance or administration",
    "sales_marketing_support": "sales, marketing or customer support",
    "education_public": "education or public service",
    "physical_engineering": "physical / non-software engineering",
}

#: Best to worst. `unknown` is absent on purpose — it is off the ladder.
ALIGNMENT_TIERS: Tuple[str, ...] = (
    ALIGN_EXACT_SPECIALTY, ALIGN_ADJACENT_SPECIALTY, ALIGN_SAME_FAMILY,
    ALIGN_ADJACENT_FAMILY, ALIGN_UNRELATED,
)

#: Fraction of the role-alignment component each rung earns.
#:
#: `unknown` sits at 0.60 — the same neutral credit the old scorer gave when
#: it had no family on either side. Not knowing must cost about what it always
#: cost; it must never cost what being wrong costs.
#:
#: `unrelated` at 0.08 is deliberately near-zero rather than zero: a component
#: that can reach exactly 0 makes the whole score bimodal, and a teacher
#: applying to a backend role should rank last, not be assigned a number that
#: implies a measurement we did not make.
ALIGNMENT_WEIGHT: Dict[str, float] = {
    ALIGN_EXACT_SPECIALTY: 1.00,
    ALIGN_ADJACENT_SPECIALTY: 0.84,
    ALIGN_SAME_FAMILY: 0.70,
    ALIGN_ADJACENT_FAMILY: 0.46,
    ALIGN_UNRELATED: 0.08,
    ALIGN_UNKNOWN: 0.60,
}


def non_tech_domain_of(title: Any) -> Optional[str]:
    """Which non-tech occupation a title names (`clinical`, `education_public`,
    `retail_food_hospitality`, ...), or None if it names none.

    Naming the domain is what turns "not technical" into evidence. A veto that
    can only say "not tech" is indistinguishable from a parse failure.
    """
    m = _NON_TECH_RE.search(_norm(title))
    if not m:
        return None
    return _NON_TECH_CATEGORY.get(m.group(0).lower())


def specialties_are_adjacent(a: Any, b: Any) -> bool:
    return str(b or "") in SPECIALTY_ADJACENCY.get(str(a or ""), frozenset())


def families_are_adjacent(a: Any, b: Any) -> bool:
    return str(b or "") in FAMILY_ADJACENCY.get(str(a or ""), frozenset())


def role_alignment(
    *,
    job_families: Sequence[Any] = (),
    job_specialties: Sequence[Any] = (),
    resume_families: Sequence[Any] = (),
    resume_specialties: Sequence[Any] = (),
    job_scope: Any = None,
    resume_scope: Any = None,
    resume_non_tech_domain: Any = None,
) -> Dict[str, Any]:
    """How close is this candidate's discipline to this job's?

    Returns `{tier, weight, matched, detail, adjacency_version,
    taxonomy_version}`. Best rung across every (job, résumé) pair wins — a
    candidate is credited for their strongest overlap, not their average one,
    because a hiring manager reads the strongest one too.

    Either side being empty returns `ALIGN_UNKNOWN`, never `ALIGN_UNRELATED`.
    Those are different claims and the second one is a penalty.
    """
    jf = [str(f) for f in (job_families or ()) if f]
    js = [str(s) for s in (job_specialties or ()) if s]
    rf = [str(f) for f in (resume_families or ()) if f]
    rs = [str(s) for s in (resume_specialties or ()) if s]

    base = {"adjacency_version": ADJACENCY_VERSION,
            "taxonomy_version": TAXONOMY_VERSION, "cross_domain": False}

    # The confident cross-domain case. A licensed teacher's résumé and a
    # backend posting are not "unknown distance apart" — we know exactly how
    # far apart they are, and saying "unknown" (0.60, neutral) is what let a
    # teacher tie with a software engineer on generic evidence.
    if (str(resume_scope or "") == SCOPE_NON_TECH
            and str(job_scope or "") in (SCOPE_CORE, SCOPE_ADJACENT)):
        raw = str(resume_non_tech_domain or "")
        dom = NON_TECH_DOMAIN_LABELS.get(raw, raw.replace("_", " ") or "another")
        return {**base, "tier": ALIGN_UNRELATED,
                "weight": ALIGNMENT_WEIGHT[ALIGN_UNRELATED], "matched": None,
                "cross_domain": True,
                "detail": (f"this resume is a {dom} career; "
                           f"this is a technical role")}

    # The mirror case, for the same reason. A backend engineer's resume against
    # a registered-nurse posting is not "unknown distance apart" either, and
    # without this it fell through to ALIGN_UNKNOWN (0.60, neutral) because a
    # non-tech posting carries no tech families to compare.
    if (str(job_scope or "") == SCOPE_NON_TECH
            and str(resume_scope or "") in (SCOPE_CORE, SCOPE_ADJACENT)):
        return {**base, "tier": ALIGN_UNRELATED,
                "weight": ALIGNMENT_WEIGHT[ALIGN_UNRELATED], "matched": None,
                "cross_domain": True,
                "detail": "this is a technical resume; this role is not a "
                          "technology occupation"}

    if not (jf or js) or not (rf or rs):
        return {**base, "tier": ALIGN_UNKNOWN,
                "weight": ALIGNMENT_WEIGHT[ALIGN_UNKNOWN], "matched": None,
                "detail": "no taxonomy signal on "
                          + ("the job" if not (jf or js) else "the resume")}

    for j in js:
        if j in rs:
            return {**base, "tier": ALIGN_EXACT_SPECIALTY,
                    "weight": ALIGNMENT_WEIGHT[ALIGN_EXACT_SPECIALTY],
                    "matched": j,
                    "detail": f"same specialty: {SPECIALTY_LABELS.get(j, j)}"}
    for j in js:
        for r in rs:
            if specialties_are_adjacent(j, r):
                return {**base, "tier": ALIGN_ADJACENT_SPECIALTY,
                        "weight": ALIGNMENT_WEIGHT[ALIGN_ADJACENT_SPECIALTY],
                        "matched": r,
                        "detail": (f"{SPECIALTY_LABELS.get(r, r)} is adjacent to "
                                   f"{SPECIALTY_LABELS.get(j, j)}")}
    # Families implied by specialties, so a caller that passes only
    # specialties still gets the family rungs.
    jf_all = list(dict.fromkeys(jf + families_for_specialties(js)))
    rf_all = list(dict.fromkeys(rf + families_for_specialties(rs)))
    for j in jf_all:
        if j in rf_all:
            return {**base, "tier": ALIGN_SAME_FAMILY,
                    "weight": ALIGNMENT_WEIGHT[ALIGN_SAME_FAMILY], "matched": j,
                    "detail": f"same family: {FAMILY_LABELS.get(j, j)}"}
    for j in jf_all:
        for r in rf_all:
            if not families_are_adjacent(j, r):
                continue
            # Family adjacency alone is too blunt to be the answer when both
            # sides know their specialty. Software and Semiconductor ARE
            # adjacent — through embedded, firmware and systems programming —
            # but a backend engineer is not a chip designer, and letting the
            # family edge speak for them credits a discipline change nobody
            # would make. So when the resume names specialties, require one of
            # them to actually reach INTO the job's family through the
            # declared matrix. No new data: it is the same matrix, read one
            # step further out.
            if rs and js:
                target = set(FAMILY_SPECIALTIES.get(j, ()))
                bridge = next((r_spec for r_spec in rs
                               if SPECIALTY_ADJACENCY.get(r_spec, frozenset()) & target),
                              None)
                if bridge is None:
                    continue
                return {**base, "tier": ALIGN_ADJACENT_FAMILY,
                        "weight": ALIGNMENT_WEIGHT[ALIGN_ADJACENT_FAMILY],
                        "matched": bridge,
                        "detail": (f"{SPECIALTY_LABELS.get(bridge, bridge)} reaches "
                                   f"into {FAMILY_LABELS.get(j, j)}")}
            return {**base, "tier": ALIGN_ADJACENT_FAMILY,
                    "weight": ALIGNMENT_WEIGHT[ALIGN_ADJACENT_FAMILY],
                    "matched": r,
                    "detail": (f"{FAMILY_LABELS.get(r, r)} is adjacent to "
                               f"{FAMILY_LABELS.get(j, j)}")}
    # NOT cross-domain. `cross_domain` is what `fit_score._eligibility` turns
    # into a hard INELIGIBLE verdict, and it means exactly one thing: the
    # resume and the posting are in different OCCUPATIONAL DOMAINS — the
    # teacher-versus-backend case the two blocks above handle. Two technology
    # families that our adjacency matrix does not connect are still two
    # technology families, and the 0.08 weight is already a severe penalty.
    #
    # Setting it here meant a software-and-cloud resume was declared INELIGIBLE
    # for "Information Security Engineer", "Security Architect" and "Staff
    # Engineer" — 478 of 522 ineligible verdicts in a 2,500-posting sample for
    # one live account, none of them a domain change. Ineligible removes the
    # row from the ranked feed entirely, so the cost of over-firing here is a
    # job the candidate never sees.
    return {**base, "tier": ALIGN_UNRELATED,
            "weight": ALIGNMENT_WEIGHT[ALIGN_UNRELATED], "matched": None,
            "cross_domain": False,
            "detail": (f"{', '.join(FAMILY_LABELS.get(f, f) for f in rf_all[:2]) or 'the resume'}"
                       f" is unrelated to "
                       f"{', '.join(FAMILY_LABELS.get(f, f) for f in jf_all[:2]) or 'the job'}")}


# ---------------------------------------------------------------------------
# Résumé-side classification
# ---------------------------------------------------------------------------
#: How much of a résumé's own history has to point at a family before we will
#: call that family the candidate's. One contract title out of twelve is not a
#: discipline, and treating it as one is how a teacher's single "IT
#: coordinator" stint made every backend role look reachable.
RESUME_FAMILY_MIN_SHARE = 0.20

# ---------------------------------------------------------------------------
# Technology evidence: what a person's TOOLS say about their discipline
#
# Why this exists
# ---------------
# `classify_resume` read TITLES and nothing else. Its "if the titles said
# nothing, read the summary and the bodies" fallback called
# `classify("", description=blob)`, and `classify` returns immediately on an
# empty title — so that branch could never return anything but `unknown`. The
# fallback was dead code, and the consequence was measured on a live account:
# an MS Data Science candidate listing PyTorch, TensorFlow, scikit-learn, NLP,
# computer vision, LLM/RAG, Spark and SQL classified to NO family at all,
# because neither of her two job titles ("Data Science Intern", "Data Quality
# Specialist Intern") was in the phrase table. `fit_score.role_alignment` —
# 25 of 100 points, the largest component — was therefore `unknown` for every
# one of 4,000 live postings.
#
# What this is NOT
# ----------------
# It is not a keyword bag. Every entry names a tool, framework or technique
# that belongs to ONE discipline; nothing generic (Excel, PowerPoint, Word,
# email, "communication", Jira, Slack) appears, because a teacher who lists
# Google Classroom must keep reading as a teacher. And it never overrides the
# non-tech veto: a confidently non-technical TITLE history still wins, so the
# people the veto exists for are unaffected. See `classify_resume`.
# ---------------------------------------------------------------------------

#: (family, specialty, tools). Matched case-insensitively with the same
#: word-boundary/alternation rules as every other phrase table here.
_SKILL_SPECIALTY_RULES: Tuple[Tuple[str, str, Tuple[str, ...]], ...] = (
    (F_AI_ML, "ml_engineering", (
        "pytorch", "tensorflow", "keras", "scikit learn", "scikit-learn",
        "sklearn", "xgboost", "lightgbm", "catboost", "\\bjax\\b",
        "random forest", "gradient boosting", "neural network",
        "convolutional neural network", "\\bcnn\\b", "\\brnn\\b", "\\blstm\\b",
        "deep learning", "machine learning", "model training",
        "feature engineering", "hyperparameter tuning", "transfer learning",
        "supervised learning", "unsupervised learning", "reinforcement learning",
    )),
    (F_AI_ML, "genai_llm", (
        "large language model", "\\bllms?\\b", "\\brag\\b",
        "retrieval augmented generation", "retrieval-augmented generation",
        "prompt engineering", "fine tuning", "fine-tuning", "langchain",
        "llamaindex", "hugging face", "huggingface", "transformers library",
        "generative ai", "\\bgenai\\b", "vector search", "vector database",
        "embeddings", "openai api", "anthropic api",
    )),
    (F_AI_ML, "nlp", (
        "natural language processing", "\\bnlp\\b", "spacy", "\\bnltk\\b",
        "\\bbert\\b", "named entity recognition", "text classification",
        "topic modeling", "sentiment analysis", "tokenization",
    )),
    (F_AI_ML, "computer_vision", (
        "computer vision", "opencv", "\\byolo\\b", "image segmentation",
        "object detection", "image classification", "image processing",
        "\\bocr\\b", "optical character recognition",
    )),
    (F_AI_ML, "speech", (
        "speech recognition", "\\basr\\b", "text to speech", "\\btts\\b",
        "whisper", "speech synthesis", "audio processing",
    )),
    (F_AI_ML, "mlops_llmops", (
        "mlops", "mlflow", "kubeflow", "sagemaker", "vertex ai",
        "model deployment", "model monitoring", "model registry",
        "feature store", "weights and biases", "\\bwandb\\b",
    )),
    (F_AI_ML, "robotics_autonomy", (
        "\\bros2?\\b", "robot operating system", "\\bslam\\b",
        "motion planning", "path planning", "sensor fusion",
    )),
    (F_DATA, "data_engineering", (
        "apache spark", "pyspark", "databricks", "airflow", "apache kafka",
        "\\bkafka\\b", "\\bdbt\\b", "snowflake", "redshift", "bigquery",
        "hadoop", "\\bhive\\b", "data pipeline", "\\betl\\b", "\\belt\\b",
        "data warehouse", "data lake", "delta lake", "informatica",
        "data ingestion", "data modeling",
    )),
    (F_DATA, "data_science", (
        "pandas", "numpy", "scipy", "statsmodels", "matplotlib", "seaborn",
        "jupyter", "statistical analysis", "statistical modeling",
        "hypothesis testing", "a/b testing", "ab testing",
        "time series forecasting", "time-series", "regression analysis",
        "experimental design", "\\banova\\b", "bayesian",
        "exploratory data analysis", "\\beda\\b", "predictive modeling",
    )),
    (F_DATA, "business_intelligence", (
        "tableau", "power bi", "powerbi", "looker", "qlik", "\\bdax\\b",
        "data visualization", "google data studio", "microstrategy",
        "dashboarding",
    )),
    (F_DATA, "database_engineering", (
        "postgresql", "postgres", "mysql", "\\bmongodb\\b", "cassandra",
        "dynamodb", "\\bt-sql\\b", "pl/sql", "query optimization",
        "database design", "\\bsql\\b", "\\bnosql\\b", "redis",
    )),
    (F_DATA, "data_governance", (
        "data governance", "data quality", "data stewardship",
        "master data management", "data lineage", "data catalog",
        "annotation", "data validation",
    )),
    (F_CLOUD, "cloud_engineering", (
        "\\baws\\b", "amazon web services", "\\bazure\\b",
        "google cloud platform", "\\bgcp\\b", "cloudformation",
        "\\bec2\\b", "\\bs3\\b", "cloud architecture",
    )),
    (F_CLOUD, "kubernetes_containers", (
        "kubernetes", "\\bk8s\\b", "docker", "\\bhelm\\b", "openshift",
        "containerization",
    )),
    (F_CLOUD, "devops", (
        "\\bci/cd\\b", "\\bcicd\\b", "ci cd", "jenkins", "github actions",
        "gitlab ci", "terraform", "ansible", "circleci", "argocd",
        "infrastructure as code",
    )),
    (F_CLOUD, "networking", (
        "\\btcp/ip\\b", "\\bbgp\\b", "load balancing", "\\bvpc\\b",
        "network security", "\\bdns\\b",
    )),
    (F_SOFTWARE, "backend", (
        "django", "flask", "fastapi", "spring boot", "node.js", "nodejs",
        "express.js", "rest api", "restful api", "graphql", "grpc",
        "microservice", "\\brabbitmq\\b", "celery",
    )),
    (F_SOFTWARE, "frontend", (
        "react.js", "reactjs", "\\breact\\b", "angular", "vue.js", "vuejs",
        "next.js", "tailwind", "redux", "\\bhtml5?\\b", "\\bcss3?\\b",
        "webpack", "svelte",
    )),
    (F_SOFTWARE, "mobile", (
        "android sdk", "\\bios\\b", "swiftui", "react native", "flutter",
        "\\bxcode\\b", "jetpack compose", "kotlin",
    )),
    (F_SOFTWARE, "general_software", (
        "object oriented", "data structures", "design patterns",
        "\\bgit\\b", "unit testing", "code review", "\\bagile\\b",
        "\\bscrum\\b", "\\bjava\\b", "\\bc\\+\\+", "\\bc#", "golang",
        "\\btypescript\\b",
    )),
    (F_SECURITY, "penetration_testing", (
        "penetration testing", "burp suite", "metasploit", "nessus",
        "\\bowasp\\b", "vulnerability assessment", "ethical hacking",
    )),
    (F_SECURITY, "secops", (
        "\\bsiem\\b", "incident response", "threat detection",
        "security monitoring", "threat hunting", "\\bsoar\\b",
    )),
    (F_SEMICONDUCTOR, "fpga_rtl", (
        "verilog", "systemverilog", "\\bvhdl\\b", "\\buvm\\b",
        "\\brtl\\b", "\\bfpga\\b", "timing closure", "synthesis",
    )),
    (F_SOFTWARE, "embedded", (
        "\\brtos\\b", "freertos", "bare metal", "\\bi2c\\b", "\\bspi\\b",
        "\\bcan bus\\b", "arm cortex", "microcontroller", "firmware",
        "embedded c",
    )),
    (F_QUALITY, "qa_automation", (
        "selenium", "cypress", "playwright", "appium", "test automation",
        "\\bjunit\\b", "\\btestng\\b",
    )),
)

_SKILL_SPECIALTY_PATTERNS: Tuple[Tuple[str, str, re.Pattern], ...] = tuple(
    (fam, spec, _compile_alternation(tools))
    for fam, spec, tools in _SKILL_SPECIALTY_RULES)

#: How many DISTINCT tools a specialty needs before it counts. One shared tool
#: ("SQL", "Git", "AWS") is not a discipline; two that belong to the same one
#: is the smallest honest claim.
SKILL_SPECIALTY_MIN = 2
#: And how many across a family, so a family never rests on one specialty's
#: bare minimum.
SKILL_FAMILY_MIN = 3


def classify_skills(skills: Sequence[Any] = (), text: Any = "") -> Dict[str, Any]:
    """What a person's TOOLS say about their discipline.

    `{families: {family: n_tools}, specialties: {specialty: n_tools},
      matched: [tool, ...]}` where `n_tools` counts DISTINCT tools. Families
    and specialties below `SKILL_FAMILY_MIN` / `SKILL_SPECIALTY_MIN` are not
    reported at all — the thresholds are applied here so no caller can
    accidentally read one tool as a career.

    Deterministic, no DB, no model. Safe to call on untrusted résumé text: it
    only ever runs fixed regexes over it.
    """
    blob = _norm(" ".join([str(s) for s in (skills or ())] + [str(text or "")]))
    if not blob:
        return {"families": {}, "specialties": {}, "matched": []}
    spec_hits: Dict[str, Set[str]] = {}
    spec_family: Dict[str, str] = {}
    for fam, spec, pattern in _SKILL_SPECIALTY_PATTERNS:
        found = {re.sub(r"[\s\-/]+", " ", m.group(0).lower()).strip()
                 for m in pattern.finditer(blob)}
        if not found:
            continue
        # `_compile_alternation` absorbs a trailing `s`/`es` into the match, so
        # "annotation" and "annotations" arrive as two surface forms of one
        # tool and would count as two. Fold a plural back onto its singular
        # only when the singular was ALSO seen - "kubernetes" has no singular
        # and must not become "kubernete".
        found = {t for t in found
                 if not ((t.endswith("s") and t[:-1] in found)
                         or (t.endswith("es") and t[:-2] in found))}
        spec_hits.setdefault(spec, set()).update(found)
        spec_family[spec] = fam
    specialties = {s: len(v) for s, v in spec_hits.items()
                   if len(v) >= SKILL_SPECIALTY_MIN}
    families: Dict[str, int] = {}
    for spec, n in specialties.items():
        fam = spec_family[spec]
        families[fam] = families.get(fam, 0) + n
    families = {f: n for f, n in families.items() if n >= SKILL_FAMILY_MIN}
    # A specialty whose family did not clear its own bar is dropped with it:
    # reporting `nlp` while refusing `ai_ml` would be an incoherent pair.
    specialties = {s: n for s, n in specialties.items()
                   if spec_family[s] in families}
    matched = sorted({t for s, v in spec_hits.items() if s in specialties
                      for t in v})
    return {"families": families, "specialties": specialties,
            "matched": matched[:40]}

#: A cross-domain veto is a REFUSAL TO RANK, so the evidence bar is high: most
#: of the résumé's titles must name the same non-tech occupation, and there
#: must be more than one of them. One "Barista" on an otherwise-software
#: résumé vetoes nothing.
NON_TECH_VETO_SHARE = 0.75
NON_TECH_VETO_MIN_TITLES = 2
RESUME_MAX_FAMILIES = 4
RESUME_MAX_SPECIALTIES = 8


#: Weight of one résumé JOB TITLE that names a discipline. The unit everything
#: else is expressed in.
TITLE_EVIDENCE_WEIGHT = 1.0

#: Weight of one specialty established by the person's TOOLS
#: (`classify_skills`), plus a little for each tool beyond the minimum, capped
#: so a long list cannot outvote a career.
SKILL_EVIDENCE_WEIGHT = 0.6
SKILL_EVIDENCE_PER_EXTRA_TOOL = 0.1
SKILL_EVIDENCE_SPECIALTY_MAX = 1.0
SKILL_EVIDENCE_FAMILY_MAX = 2.0

#: Weight of one DECLARED target role from onboarding. Aspiration is real
#: evidence about what a person is applying for and it is not evidence that
#: they have done it, so it is worth about a third of a job title.
TARGET_ROLE_WEIGHT = 0.35
TARGET_ROLE_FAMILY_MAX = 1.0

#: Confidence ceilings per evidence source. `classification_confidence` gates
#: `fit_score._eligibility`'s cross-domain veto at 0.70, so neither tools nor
#: declared targets may ever reach it: nothing a person LISTS or WANTS may
#: make them ineligible for anything.
SKILLS_ONLY_MAX_CONFIDENCE = 0.68
TARGET_ONLY_MAX_CONFIDENCE = 0.55


def _target_role_families(target_roles: Sequence[Any]
                          ) -> Tuple[Dict[str, float], Dict[str, float], List[str]]:
    """`(family_weight, specialty_weight, roles_read)` for declared targets."""
    fams: Dict[str, float] = {}
    specs: Dict[str, float] = {}
    read: List[str] = []
    for role in target_roles or ():
        text = str(role or "").strip()
        if not text:
            continue
        got = classify(text)
        if got.tech_scope not in (SCOPE_CORE, SCOPE_ADJACENT) or not got.role_families:
            continue
        read.append(text)
        for f in got.role_families:
            fams[f] = min(TARGET_ROLE_FAMILY_MAX,
                          fams.get(f, 0.0) + TARGET_ROLE_WEIGHT)
        for s in got.role_specialties:
            specs[s] = specs.get(s, 0.0) + TARGET_ROLE_WEIGHT
    return fams, specs, read


def classify_resume(
    *,
    titles: Sequence[Any] = (),
    skills: Sequence[Any] = (),
    summary: Any = "",
    descriptions: Sequence[Any] = (),
    target_roles: Sequence[Any] = (),
) -> Dict[str, Any]:
    """A résumé's own families and specialties, on the same 14/101 tree.

    Job classification and résumé classification MUST use one vocabulary or
    the alignment ladder has nothing to compare. Before this, jobs used
    `tech_taxonomy` (14/101) and résumés used `fit_score.ROLE_FAMILIES` (32
    coarse keys), so "backend" on one side and "software_engineering" on the
    other were the finest distinction available.

    Titles are weighted by RECENCY-neutral count, not by position: a résumé's
    ordering is a formatting choice, and inferring seniority of interest from
    it would be reading the layout, not the career.

    Three sources, in a DECLARED order of authority
    ----------------------------------------------
    1. **Job titles** (`TITLE_EVIDENCE_WEIGHT`, 1.0) — what the person has
       been paid to be. Strongest, and the only source that may license a
       cross-domain veto.
    2. **Tools and techniques** (`SKILL_EVIDENCE_WEIGHT`, 0.6 per specialty,
       capped per family) — what they have demonstrably worked with, read from
       the skills block AND the experience/project bodies by
       `classify_skills`. This exists because a real résumé's titles are
       routinely non-canonical: a live MS Data Science candidate whose two
       titles were "Data Science Intern" and "Data Quality Specialist Intern"
       classified to NOTHING, while listing PyTorch, TensorFlow,
       scikit-learn, NLP, computer vision, LLM/RAG and Spark. "No evidence"
       has to mean no evidence, not "the title string was not in our table".
    3. **Declared target roles** (`TARGET_ROLE_WEIGHT`, 0.35) — what
       onboarding says they are applying for (`user_preferences.target_roles`).
       Weakest, and FENCED: when the résumé already demonstrates something, a
       declared target only counts where it is corroborated by, or adjacent
       to, what was demonstrated. A target in an unrelated family is reported
       in `target_families_unsupported` and contributes nothing — wanting a
       job must never look like having done it. When the résumé demonstrates
       NOTHING, declared targets are the only evidence there is and are used
       in full, at `TARGET_ONLY_MAX_CONFIDENCE`.

    A confidently NON-TECH title history outranks all three. The veto is
    evaluated on titles alone, before tools or targets are admitted, so a
    nurse who lists SQL and a teacher who declares "Data Analyst" are read
    exactly as they were before this change.

    `unknown` still means unknown: with no title, no recognised tool and no
    declared target, the result is `tech_scope="unknown"` at confidence 0.0.
    """
    counts: Dict[str, float] = {}
    spec_counts: Dict[str, float] = {}
    evidence_titles: List[str] = []
    non_tech_domains: Dict[str, int] = {}
    total = 0
    seen_titles = 0

    for title in titles or ():
        text = str(title or "").strip()
        if not text:
            continue
        seen_titles += 1
        got = classify(text, skills=skills)
        if got.tech_scope in (SCOPE_CORE, SCOPE_ADJACENT) and got.role_families:
            total += 1
            evidence_titles.append(text)
            for f in got.role_families:
                counts[f] = counts.get(f, 0.0) + TITLE_EVIDENCE_WEIGHT
            for s in got.role_specialties:
                spec_counts[s] = spec_counts.get(s, 0.0) + TITLE_EVIDENCE_WEIGHT
        elif got.tech_scope == SCOPE_NON_TECH:
            # WHICH non-tech domain matters. "we found no tech evidence" is an
            # absence; "every title on this resume is a classroom teaching
            # title" is a positive finding about a different occupation, and
            # only the second one may veto anything.
            dom = non_tech_domain_of(text) or "other"
            non_tech_domains[dom] = non_tech_domains.get(dom, 0) + 1

    # ---- the non-tech veto, decided on TITLES ALONE ------------------------
    #
    # Checked BEFORE tools and targets are admitted, and it returns. That
    # ordering is the whole safety property of this function: every résumé
    # that was vetoed before this change is vetoed identically after it, no
    # matter what its skills block says or what onboarding it completed.
    if not counts:
        nt_total = sum(non_tech_domains.values())
        classified = nt_total + total
        share = (nt_total / classified) if classified else 0.0
        # Two non-tech titles, or a résumé whose classified history is
        # ENTIRELY non-tech. The second clause matters: a single-employer
        # nurse or teacher résumé is a complete career, not a thin one, and
        # requiring two entries would have exempted exactly the people the
        # veto exists for.
        if nt_total and share >= NON_TECH_VETO_SHARE \
                and (nt_total >= NON_TECH_VETO_MIN_TITLES or share >= 1.0):
            domain = max(non_tech_domains, key=lambda k: non_tech_domains[k])
            return {"role_families": [], "role_specialties": [],
                    "taxonomy_version": TAXONOMY_VERSION,
                    # Confidence rises with corroboration AND with coverage.
                    # A résumé whose classified history is 100% one non-tech
                    # domain is a confident finding even with a single entry;
                    # three-of-four is confident because of the count.
                    "classification_confidence": round(
                        min(0.95, 0.50 + 0.15 * min(nt_total, 3)
                            + (0.10 if share >= 1.0 else 0.0)), 3),
                    "evidence_titles": [], "tech_scope": SCOPE_NON_TECH,
                    "non_tech_domain": domain,
                    "non_tech_titles": nt_total, "titles_seen": seen_titles,
                    "evidence_sources": ["titles"], "skill_evidence": [],
                    "target_roles_read": [], "target_families_unsupported": []}

    # ---- 2. tools and techniques ------------------------------------------
    title_families = set(counts)
    body_blob = " ".join([str(summary or "")]
                         + [str(d or "") for d in (descriptions or ())])
    tools = classify_skills(skills, text=body_blob)
    skill_families = dict(tools.get("families") or {})
    skill_specialties = dict(tools.get("specialties") or {})
    for spec, n_tools in skill_specialties.items():
        w = min(SKILL_EVIDENCE_SPECIALTY_MAX,
                SKILL_EVIDENCE_WEIGHT
                + SKILL_EVIDENCE_PER_EXTRA_TOOL * max(0, n_tools - SKILL_SPECIALTY_MIN))
        spec_counts[spec] = spec_counts.get(spec, 0.0) + w
    for fam in skill_families:
        fam_specs = [s for s in skill_specialties
                     if s in FAMILY_SPECIALTIES.get(fam, ())]
        w = min(SKILL_EVIDENCE_FAMILY_MAX, sum(
            min(SKILL_EVIDENCE_SPECIALTY_MAX,
                SKILL_EVIDENCE_WEIGHT + SKILL_EVIDENCE_PER_EXTRA_TOOL
                * max(0, skill_specialties[s] - SKILL_SPECIALTY_MIN))
            for s in fam_specs))
        if w > 0:
            counts[fam] = counts.get(fam, 0.0) + w

    demonstrated = set(counts)

    # ---- 3. declared target roles -----------------------------------------
    tgt_fams, tgt_specs, roles_read = _target_role_families(target_roles)
    unsupported: List[str] = []
    for fam, w in tgt_fams.items():
        if demonstrated and not (
                fam in demonstrated
                or any(families_are_adjacent(fam, d) for d in demonstrated)):
            unsupported.append(fam)
            continue
        counts[fam] = counts.get(fam, 0.0) + w
    admitted_targets = {f for f in tgt_fams if f not in unsupported}
    for spec, w in tgt_specs.items():
        fam_of = families_for_specialties([spec])
        if not any(f in admitted_targets or f in demonstrated for f in fam_of):
            continue
        spec_counts[spec] = spec_counts.get(spec, 0.0) + w

    if not counts:
        return {"role_families": [], "role_specialties": [],
                "taxonomy_version": TAXONOMY_VERSION,
                "classification_confidence": 0.0,
                "evidence_titles": [], "tech_scope": SCOPE_UNKNOWN,
                "non_tech_domain": None,
                "non_tech_titles": sum(non_tech_domains.values()),
                "titles_seen": seen_titles, "evidence_sources": [],
                "skill_evidence": [], "target_roles_read": roles_read,
                "target_families_unsupported": sorted(unsupported)}

    total_weight = sum(counts.values())
    # The floor is one declared target role, not one job title: a résumé whose
    # ONLY evidence is onboarding must still yield the families it named.
    threshold = max(TARGET_ROLE_WEIGHT, total_weight * RESUME_FAMILY_MIN_SHARE)
    families = [f for f, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
                if n >= threshold][:RESUME_MAX_FAMILIES]
    if not families:                       # pragma: no cover - threshold floor
        families = [max(counts, key=lambda k: counts[k])]
    # A specialty whose family did not survive is dropped with it. Reporting
    # `kubernetes_containers` while refusing `cloud_platform` would put a
    # specialty on the alignment ladder that the résumé's own family list
    # contradicts.
    fam_set = set(families)
    specialties = [s for s, _ in sorted(spec_counts.items(),
                                        key=lambda kv: (-kv[1], kv[0]))
                   if any(f in fam_set for f in families_for_specialties([s]))
                   ][:RESUME_MAX_SPECIALTIES]
    scope = (SCOPE_CORE if any(FAMILY_SCOPE.get(f) == SCOPE_CORE for f in families)
             else SCOPE_ADJACENT)

    # ---- confidence: the STRONGEST source present, capped by that source ---
    sources: List[str] = []
    conf = 0.0
    if total:
        sources.append("titles")
        # Confidence rises with corroboration: one matching title is a hint,
        # four are a discipline.
        conf = max(conf, min(0.95, 0.45 + 0.12 * min(total, 4)))
    if skill_families:
        sources.append("skills")
        conf = max(conf, min(SKILLS_ONLY_MAX_CONFIDENCE,
                             0.30 + 0.05 * len(tools.get("matched") or ())))
    if roles_read and admitted_targets:
        sources.append("target_roles")
        conf = max(conf, min(TARGET_ONLY_MAX_CONFIDENCE,
                             0.30 + 0.05 * len(roles_read)))

    return {"role_families": families, "role_specialties": specialties,
            "taxonomy_version": TAXONOMY_VERSION,
            "classification_confidence": round(conf, 3),
            "evidence_titles": evidence_titles[:6], "tech_scope": scope,
            "non_tech_domain": None,
            "non_tech_titles": sum(non_tech_domains.values()),
            "titles_seen": seen_titles,
            # Explainability: which of the three sources actually spoke, the
            # tools that were read, and any declared target the demonstrated
            # evidence did not support.
            "evidence_sources": sources,
            "skill_evidence": list(tools.get("matched") or ())[:20],
            "target_roles_read": roles_read,
            "target_families_unsupported": sorted(unsupported)}
