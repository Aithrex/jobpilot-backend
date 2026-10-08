"""Fit score engine (ADR-002) - one shared, versioned, explainable
candidate-to-job fit score.

Replaces the resume-denominator overlap heuristic in
`services/jd_match_scorer.py`, whose denominator came from the RESUME's own
skill list clamped to [8, 14]. That measured "how much of this resume appears
in this posting" and had only ~15 reachable values.

Here the required-skill denominator comes from the JOB DESCRIPTION. Required
and preferred requirements are separated, skill evidence found inside work or
project history outweighs a bare skills-list mention, experience comes from
dated evidence only, and unknown information stays unknown instead of
failing the candidate.

Displayed alignment components (100 total):

    role / title alignment                25
    required skill coverage               30
    relevant experience and seniority     20
    preferred skills                      10
    domain / industry alignment            5
    location / work-mode compatibility     5
    education / certification / license    5

Hard gates cap (never zero) the final score for a mandatory license, a
security clearance, a work-authorization conflict, a severe experience gap,
an incompatible onsite location, and an unrelated role.

Deterministic and rule based. There is NO Bedrock call anywhere in this
module, so it is safe on the matching request path. `extract_requirements`
is cached in-process by content hash and is also the unit an asynchronous
per-JD extraction job would cache; the request path works correctly without
that job existing.

Freshness, source quality, and employer diversity are deliberately NOT here.
They are ranking concerns and live in the caller (guest_service sorts by
(-score, days_ago)); letting them touch the displayed fit score would make
the number dishonest.

Public API:
    score_one(jd_text=..., title=..., resume_skills=..., resume=..., job=...)
    score_batch(resume, items)
    extract_requirements(jd_text, title)
    FIT_SCORE_VERSION
"""

from __future__ import annotations

import hashlib
import logging
import os
import html
import re
import threading
from collections import OrderedDict
from functools import lru_cache
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

logger = logging.getLogger(__name__)

FIT_SCORE_VERSION = "fit-v2.3"

# The cliff described below is REAL, and this is how 2.1 crosses it.
#
# `_precomputed_requirements` used to reject any stored profile whose
# `score_version` was not the current string, and `score()` then fell back to
# extracting from the JD body. Inventory rows carry NO body (it lives in S3),
# so the fallback extracted from an empty string, `has_job_requirements` went
# False for every posting at once, and the guest path filtered the entire
# inventory out as evidence-less. A bump was a hard cliff.
#
# What changed in 2.1 is HOW COMPONENTS ARE WEIGHED, not WHAT IS EXTRACTED:
# `extract_requirements` produces the identical dict, field for field. So a
# 2.0 requirements profile is still valid input to a 2.1 scorer, and
# `_COMPATIBLE_REQUIREMENT_VERSIONS` says so explicitly. The 216,687 postings
# carrying a stored `requirements` keep scoring through the deploy with no
# window during which nothing is servable, and
# `scripts/backfill_fit_score_v21.py` re-stamps them at leisure.
#
# The rule this encodes, for the next bump: add to this set when the
# extraction CONTRACT is unchanged; change
# `job_inventory_service.EXTRACTOR_VERSION` (which drives a re-parse through
# the normal crawl) when it is not.
# 2.2 keeps the same contract again. `extract_requirements` still returns the
# identical dict, field for field, so every stored profile remains VALID input
# and nothing becomes unscorable at deploy time.
#
# It is not, however, identical OUTPUT. 2.2 widens `_CLEARANCE_JD` and
# `_WORKAUTH_JD` (see those patterns), so a posting re-extracted under 2.2 can
# set `clearance_required` / `work_authorization_restricted` where the stored
# 2.0/2.1 profile left them False. That is a VALUE change, not a shape change:
# the two flags get MORE accurate on re-extraction and are merely
# under-reported until then, which is the safe direction — a missing flag
# surfaces as "not stated", never as a penalty. A re-extraction backfill is
# therefore worth running and is not required for correctness.
#
# 2.3 keeps the contract again and changes two VALUES, in the direction that
# matters: `required_years` is the posting's HEADLINE requirement (it was the
# smallest number in the body - "5+ years, 2+ with AWS" read 2) and `job_level`
# lets stated years raise a title's band ("Software Engineer II" no longer
# reads entry; "6+ years" is never entry). A stored 2.2 profile is still valid
# input but under-reports both until re-extracted, which
# `job_inventory_service.EXTRACTOR_VERSION` 5 and
# `scripts/rederive_posting_levels.py` do.
_COMPATIBLE_REQUIREMENT_VERSIONS: frozenset = frozenset(
    {"fit-v2.0", "fit-v2.1", "fit-v2.2", "fit-v2.3"})

# Component maxima. The sum is exactly 100; `test_fit_score_v2` asserts it.
COMPONENT_MAX: Dict[str, int] = {
    "role_alignment": 25,
    "required_skills": 30,
    "experience": 20,
    "preferred_skills": 10,
    "domain": 5,
    "location": 5,
    "education": 5,
}

# Score caps applied when a hard constraint is violated. Caps, not zeros:
# the engine is confident enough to say "this is very unlikely to clear
# screening" and not confident enough to claim certainty about a person.
CONSTRAINT_CAPS: Dict[str, int] = {
    "unrelated_role": 30,
    "license_missing": 40,
    "clearance_missing": 40,
    "work_authorization_conflict": 45,
    "severe_experience_gap": 50,
    "location_incompatible": 60,
    # Not a judgement about the candidate: the resume yielded no readable
    # skill evidence at all, so required-skill coverage cannot be assessed
    # and any number we printed would be invented. The product surfaces this
    # as the honest "low signal resume" state rather than a fake match.
    "insufficient_resume_evidence": 20,
    # The MIRROR of the above, on the job side. A posting whose body could not
    # be read yields no requirements, and absent requirements earn NEUTRAL
    # credit in every component (60% of skills, domain and education), so a
    # body-less row scored 72 "good match" against a resume it had literally
    # never been compared to. That is the single most damaging failure this
    # engine can produce: a confident number attached to no evidence, sitting
    # in a guest's top five next to real matches. Capped into "weak match" so
    # it can still be browsed but can never outrank a posting we actually read,
    # and never reads as a "possible match" (>=50) it has no evidence to claim.
    "insufficient_job_requirements": 40,
}

# ---------------------------------------------------------------------------
# Eligibility is SEPARATE from the score, and is not a second score.
#
# A hard requirement is not a preference expressed strongly. A role that
# demands an active RN licence is not 40% reachable by someone without one; it
# is not reachable. Before 2.1 those were score CAPS, which put them on the
# same axis as skill overlap and let a strong-on-paper candidate out-rank a
# qualified one. Now they answer a different question, in a different field.
#
# The gate only ever fires on a KNOWN mismatch. "The résumé does not mention a
# clearance" is not a mismatch, it is silence, and silence is `unknown` — it
# surfaces as a requirement the reader can check for themselves and costs
# nothing.
ELIGIBLE = "eligible"
INELIGIBLE = "ineligible"
NOT_SCORED = "not_scored"

BANDS: Sequence[Tuple[int, str]] = (
    (80, "strong match"),
    (65, "good match"),
    (50, "possible match"),
    (35, "weak match"),
    (0, "unlikely match"),
)


# ---------------------------------------------------------------------------
# Text normalisation
# ---------------------------------------------------------------------------

_DASHES = dict.fromkeys(map(ord, "‐‑‒–—―−"), "-")
_QUOTES = {ord("‘"): "'", ord("’"): "'", ord("“"): '"', ord("”"): '"'}
_TAG_RE = re.compile(r"<[^>]+>")


def _norm(text: Any) -> str:
    """Lowercase, de-tag, and fold unicode dashes/quotes. Newlines survive:
    section detection needs them."""
    s = str(text or "")
    # Decode entities BEFORE stripping tags (an encoded "&lt;p&gt;" is a tag).
    # Workday serves descriptions entity-encoded - "8&#43; years", "C&#43;&#43;"
    # - and decoding was missing: sampled 2026-10-03, 62% of Workday bodies
    # were encoded and the years reading moved for 22% of Workday postings.
    if "&" in s:
        s = html.unescape(s)
    if "<" in s and ">" in s:
        s = _TAG_RE.sub("\n", s)
    s = s.translate(_DASHES).translate(_QUOTES).lower()
    s = re.sub(r"[ \t\r\f\v]+", " ", s)
    return re.sub(r"\n{2,}", "\n", s)


def content_hash(jd_text: Any, title: Any = "") -> str:
    """Stable id for a job description. The cache key an asynchronous
    requirement-extraction job would use, exposed so callers can store it."""
    blob = (str(title or "") + "\x00" + str(jd_text or "")).encode("utf-8", "replace")
    return hashlib.sha1(blob).hexdigest()


# ---------------------------------------------------------------------------
# Skill lexicon
#
# Canonical name -> alias regex fragments. Aliases are matched as one big
# alternation in a single pass, so cost is O(text) and not O(text * lexicon).
# Coverage spans technical AND nontechnical work on purpose: a nurse or a
# paralegal must not fall through to an empty requirement set (which would
# then be scored as "unknown" and flatten the whole scale).
# ---------------------------------------------------------------------------

SKILL_LEXICON: Dict[str, List[str]] = {
    # --- programming languages -------------------------------------------
    "python": ["python"],
    "java": ["java"],
    "javascript": ["javascript", "java script"],
    "typescript": ["typescript"],
    "go": ["golang"],  # bare "go" handled by _SHORT_SKILLS
    "rust": ["rust"],
    "c++": [r"c\+\+", "cpp"],
    "c#": [r"c#", r"\.net", "dotnet"],
    "ruby": ["ruby"],
    "php": ["php"],
    "swift": ["swift"],
    "kotlin": ["kotlin"],
    "scala": ["scala"],
    "perl": ["perl"],
    "matlab": ["matlab"],
    "sql": ["sql", "t-sql", "pl/sql"],
    "bash": ["bash", "shell scripting", "shell script"],
    "vba": ["vba", "visual basic"],
    # --- web / frontend ---------------------------------------------------
    "react": ["react", "react.js", "reactjs"],
    "angular": ["angular", "angularjs"],
    "vue": ["vue", "vue.js", "vuejs"],
    "node.js": ["node.js", "nodejs", "node js"],
    "html": ["html", "html5"],
    "css": ["css", "css3", "sass", "scss", "tailwind"],
    "next.js": ["next.js", "nextjs"],
    "graphql": ["graphql"],
    "rest api": ["rest api", "restful", "rest apis", "rest services"],
    # --- data / backend ---------------------------------------------------
    "postgresql": ["postgresql", "postgres"],
    "mysql": ["mysql", "mariadb"],
    "mongodb": ["mongodb", "mongo"],
    "redis": ["redis"],
    "elasticsearch": ["elasticsearch", "opensearch"],
    "spark": ["spark", "pyspark"],
    "hadoop": ["hadoop", "hive"],
    "kafka": ["kafka"],
    "airflow": ["airflow"],
    "dbt": ["dbt"],
    "snowflake": ["snowflake"],
    "etl": ["etl", "elt", "data pipelines", "data pipeline"],
    "data modeling": ["data modeling", "data modelling", "dimensional modeling"],
    "data warehouse": ["data warehouse", "data warehousing", "redshift", "bigquery"],
    # --- cloud / infra ----------------------------------------------------
    "aws": ["aws", "amazon web services"],
    "azure": ["azure"],
    "gcp": ["gcp", "google cloud"],
    "docker": ["docker", "containerization", "containerisation"],
    "kubernetes": ["kubernetes", "k8s"],
    "terraform": ["terraform"],
    "ci/cd": ["ci/cd", "cicd", "continuous integration", "continuous delivery",
              "jenkins", "github actions", "gitlab ci"],
    "linux": ["linux", "unix"],
    "git": ["git", "github", "gitlab", "version control"],
    "microservices": ["microservices", "microservice"],
    "serverless": ["serverless", "lambda functions"],
    "monitoring": ["monitoring", "observability", "datadog", "prometheus", "grafana"],
    # --- data science / ml ------------------------------------------------
    "machine learning": ["machine learning", "ml models"],
    "deep learning": ["deep learning", "neural networks"],
    "nlp": ["nlp", "natural language processing"],
    "pytorch": ["pytorch"],
    "tensorflow": ["tensorflow", "keras"],
    "scikit-learn": ["scikit-learn", "sklearn"],
    "pandas": ["pandas", "numpy"],
    "statistics": ["statistics", "statistical analysis", "statistical modeling"],
    "a/b testing": ["a/b testing", "ab testing", "experimentation"],
    "data visualization": ["data visualization", "data visualisation", "tableau",
                           "power bi", "powerbi", "looker"],
    # --- semiconductor / hardware ----------------------------------------
    #
    # `semiconductor_hardware` is a CORE family in `tech_taxonomy` — the
    # default search returns every ASIC, RTL, verification, firmware, analog,
    # RF, EDA and photonics role — and this lexicon contained NOT ONE of its
    # words. Measured on the domain eval: a senior ASIC designer's résumé
    # yielded zero readable skills, tripped `insufficient_resume_evidence`,
    # and scored 20 against EVERY job including their own discipline. The
    # engine was not ranking those candidates badly; it could not read them at
    # all, and the cap it applied was the honest answer to a question it
    # should never have had to ask.
    "verilog": ["verilog", "systemverilog", "system verilog"],
    "vhdl": ["vhdl"],
    # Multi-word forms only. A bare "rtl" collides with the FRONTEND meaning
    # (right-to-left), and a React posting that mentions "RTL support" would
    # otherwise acquire a silicon-design requirement its candidates cannot
    # cover.
    "rtl design": ["rtl design", "rtl coding", "rtl development",
                   "rtl verification", "rtl engineer", "rtl to gdsii",
                   "register transfer level", "synthesizable rtl"],
    "uvm": ["uvm", "universal verification methodology", "constrained random"],
    "design verification": ["design verification", "functional coverage",
                            "formal verification", "assertion based verification"],
    "synthesis": ["logic synthesis", "design compiler", "genus", "synthesis"],
    "timing closure": ["timing closure", "static timing analysis",
                       "physical design", "place and route"],
    "asic": ["asic", "soc design", "tapeout", "tape out"],
    "fpga": ["fpga", "xilinx", "altera", "vivado", "quartus"],
    "analog design": ["analog design", "mixed signal", "mixed-signal",
                      "analog layout", "spice simulation"],
    "rf engineering": ["rf design", "rf engineering", "radio frequency",
                       "antenna design", "microwave"],
    "semiconductor process": ["photolithography", "wafer fab", "etch process",
                              "thin film deposition", "yield analysis"],
    "signal integrity": ["signal integrity", "power integrity"],
    "pcb design": ["pcb design", "schematic capture", "altium", "cadence allegro"],
    # --- embedded ---------------------------------------------------------
    "embedded c": ["embedded c", "bare metal", "baremetal"],
    "rtos": ["rtos", "freertos", "zephyr", "real time operating system"],
    "firmware": ["firmware", "bootloader", "board bring-up", "board bringup"],
    "device drivers": ["device driver", "device drivers", "kernel driver"],
    "embedded protocols": ["i2c", "spi", "uart", "can bus", "modbus", "mqtt"],
    "hardware debug": ["oscilloscope", "logic analyzer", "logic analyser",
                       "jtag", "in-circuit"],
    # --- robotics ---------------------------------------------------------
    "ros": ["ros", "ros2", "robot operating system"],
    "slam": ["slam", "simultaneous localization", "localization and mapping"],
    "motion planning": ["motion planning", "path planning", "trajectory optimization",
                        "trajectory planning"],
    "sensor fusion": ["sensor fusion", "kalman filter", "extended kalman",
                      "state estimation"],
    "computer vision": ["computer vision", "opencv", "image processing",
                        "object detection", "point cloud"],
    "control systems": ["control systems", "pid control", "model predictive control",
                        "closed loop control"],
    "lidar": ["lidar", "radar", "imu", "depth camera"],
    # --- gaming -----------------------------------------------------------
    "game engine": ["game engine", "unreal engine", "unreal", "unity3d", "unity engine",
                    "godot", "cryengine"],
    "graphics programming": ["graphics programming", "rendering pipeline",
                             "shader", "shaders", "hlsl", "glsl", "directx",
                             "vulkan", "opengl"],
    "gameplay programming": ["gameplay programming", "gameplay systems",
                             "animation system", "game physics"],
    "performance profiling": ["performance profiling", "frame budget",
                              "cpu profiling", "gpu profiling", "renderdoc",
                              "profiling", "flame graph"],
    # --- quantum ----------------------------------------------------------
    #
    # Added with `tech-tax-1.2.0`, which introduced `quantum_software`,
    # `quantum_hardware`, `quantum_control` and `cryogenic_systems` on live
    # evidence. A taxonomy that can classify a quantum posting and a scorer
    # that cannot read a quantum résumé is half a feature.
    "quantum computing": ["quantum computing", "quantum information",
                          "quantum algorithms", "qubit", "qubits"],
    "quantum sdk": ["qiskit", "cirq", "pennylane", "braket", "quantum sdk"],
    "quantum error correction": ["quantum error correction", "surface code",
                                 "logical qubit", "\bqec\b"],
    "quantum control": ["pulse level", "pulse-level", "qubit control",
                        "quantum control", "cryo cmos"],
    "cryogenics": ["cryogenic", "cryogenics", "dilution refrigerator",
                   "cryostat", "superconducting circuit"],
    # --- compilers / systems ---------------------------------------------
    "compilers": ["compiler", "compilers", "llvm", "code generation",
                  "transpiler", "intermediate representation"],
    "distributed systems": ["distributed systems", "consensus", "raft protocol",
                            "sharding", "replication"],
    # --- ml platform ------------------------------------------------------
    "mlops": ["mlops", "llmops", "model serving", "feature store",
              "model registry", "model monitoring"],
    "llm": ["llm", "large language model", "large language models",
            "transformers", "fine tuning", "fine-tuning", "rag",
            "retrieval augmented"],
    # --- security engineering --------------------------------------------
    "threat modeling": ["threat modeling", "threat modelling", "attack surface",
                        "security design review", "secure design review"],
    "owasp": ["owasp", "sast", "dast", "static analysis security",
              "dependency scanning", "secure code review"],
    "cryptography": ["cryptography", "cryptographic", "key management",
                     "pki", "tls", "encryption at rest"],
    "iam": ["identity and access management", "\biam\b", "oauth", "saml",
            "single sign on", "sso"],
    # --- mobile -----------------------------------------------------------
    "ios": ["ios development", "swiftui", "uikit", "xcode"],
    "android": ["android development", "jetpack compose", "android sdk"],
    # --- accessibility / frontend quality ---------------------------------
    "accessibility": ["accessibility", "wcag", "aria", "screen reader",
                      "a11y"],
    # --- qa / security ----------------------------------------------------
    "automated testing": ["automated testing", "test automation", "selenium",
                          "cypress", "playwright", "unit testing", "pytest"],
    "manual testing": ["manual testing", "exploratory testing", "test cases"],
    "penetration testing": ["penetration testing", "pen testing", "ethical hacking"],
    "siem": ["siem", "splunk", "security monitoring"],
    "incident response": ["incident response", "threat detection"],
    "compliance": ["compliance", "soc 2", "iso 27001", "hipaa", "gdpr", "pci dss"],
    # --- product / process ------------------------------------------------
    "agile": ["agile", "scrum", "kanban", "sprint planning"],
    "jira": ["jira", "confluence"],
    "roadmapping": ["roadmap", "roadmapping", "product strategy"],
    "stakeholder management": ["stakeholder management", "stakeholder communication",
                               "cross functional", "cross-functional"],
    "requirements gathering": ["requirements gathering", "requirement analysis",
                               "business requirements", "user stories"],
    "project management": ["project management", "program management",
                           "project planning", "pmp"],
    "process improvement": ["process improvement", "continuous improvement",
                            "lean", "six sigma"],
    # --- design -----------------------------------------------------------
    "figma": ["figma", "sketch app"],
    "user research": ["user research", "usability testing", "user interviews"],
    "wireframing": ["wireframing", "wireframes", "prototyping"],
    "adobe creative suite": ["photoshop", "illustrator", "indesign", "adobe creative"],
    # --- healthcare / nursing --------------------------------------------
    "patient care": ["patient care", "direct patient care", "bedside care"],
    "patient assessment": ["patient assessment", "clinical assessment", "triage"],
    "medication administration": ["medication administration", "medication management",
                                  "iv therapy", "administering medication"],
    "electronic health records": ["electronic health record", "electronic health records",
                                  "ehr", "emr", "epic systems", "cerner"],
    "care planning": ["care plan", "care planning", "discharge planning"],
    "wound care": ["wound care", "wound management"],
    "acls": ["acls", "advanced cardiac life support"],
    "bls": ["bls", "basic life support", "cpr certification"],
    "infection control": ["infection control", "sterile technique", "aseptic technique"],
    "vital signs": ["vital signs", "vitals monitoring"],
    "phlebotomy": ["phlebotomy", "venipuncture", "blood draw"],
    "hipaa compliance": ["hipaa compliance", "patient privacy", "patient confidentiality"],
    "clinical documentation": ["clinical documentation", "charting", "nursing notes"],
    # --- accounting / finance --------------------------------------------
    "general ledger": ["general ledger", "journal entries", "gl accounting"],
    "accounts payable": ["accounts payable", "ap processing"],
    "accounts receivable": ["accounts receivable", "ar processing", "collections"],
    "month-end close": ["month end close", "month-end close", "period close",
                        "closing process"],
    "bank reconciliation": ["bank reconciliation", "account reconciliation",
                            "reconciliations"],
    "financial reporting": ["financial reporting", "financial statements",
                            "balance sheet", "income statement"],
    "gaap": ["gaap", "us gaap", "ifrs"],
    "tax preparation": ["tax preparation", "tax returns", "tax filings", "tax compliance"],
    "auditing": ["auditing", "internal audit", "external audit", "audit support"],
    "budgeting": ["budgeting", "budget preparation", "forecasting", "variance analysis"],
    "payroll": ["payroll", "payroll processing"],
    "quickbooks": ["quickbooks"],
    "sap": ["sap", "netsuite", "oracle financials", "erp system"],
    "excel": ["excel", "microsoft excel", "spreadsheets", "pivot tables", "vlookup"],
    "accounts analysis": ["financial analysis", "cost analysis", "profitability analysis"],
    # --- education / teaching --------------------------------------------
    "lesson planning": ["lesson plan", "lesson planning", "instructional planning"],
    "classroom management": ["classroom management", "behavior management",
                             "behaviour management"],
    "curriculum development": ["curriculum development", "curriculum design",
                               "curriculum planning"],
    "differentiated instruction": ["differentiated instruction", "differentiation",
                                   "individualized instruction"],
    "student assessment": ["student assessment", "formative assessment",
                           "summative assessment", "grading"],
    "iep": ["iep", "individualized education program", "special education"],
    "parent communication": ["parent communication", "parent teacher",
                             "family engagement"],
    "classroom technology": ["google classroom", "smartboard", "learning management system",
                             "blackboard", "canvas lms"],
    # --- legal ------------------------------------------------------------
    "legal research": ["legal research", "case research", "westlaw", "lexisnexis"],
    "document review": ["document review", "discovery review", "e-discovery",
                        "ediscovery"],
    "legal drafting": ["legal drafting", "drafting pleadings", "drafting contracts",
                       "drafting motions", "legal writing"],
    "case management": ["case management", "docketing", "case files"],
    "litigation support": ["litigation support", "trial preparation", "trial prep"],
    "contract review": ["contract review", "contract administration",
                        "contract negotiation"],
    "regulatory filings": ["regulatory filings", "court filings", "filing motions"],
    # --- hr / recruiting --------------------------------------------------
    "recruiting": ["recruiting", "recruitment", "talent acquisition", "sourcing candidates"],
    "onboarding": ["onboarding", "new hire orientation"],
    "employee relations": ["employee relations", "employee engagement",
                           "performance management"],
    "hris": ["hris", "workday hcm", "adp", "bamboohr"],
    "benefits administration": ["benefits administration", "open enrollment"],
    # --- sales / marketing / support -------------------------------------
    "crm": ["crm", "salesforce", "hubspot"],
    "lead generation": ["lead generation", "prospecting", "cold calling", "outbound"],
    "account management": ["account management", "client relationship",
                           "customer success"],
    "seo": ["seo", "search engine optimization", "sem"],
    "content marketing": ["content marketing", "content strategy", "copywriting"],
    "social media": ["social media", "social channels"],
    "email marketing": ["email marketing", "marketing automation", "marketo", "mailchimp"],
    "google analytics": ["google analytics", "web analytics"],
    "customer service": ["customer service", "customer support", "client service",
                         "help desk"],
    "conflict resolution": ["conflict resolution", "de-escalation", "de escalation"],
    # --- operations / trades ---------------------------------------------
    "inventory management": ["inventory management", "inventory control", "stock control"],
    "supply chain": ["supply chain", "procurement", "vendor management", "logistics"],
    "scheduling": ["scheduling", "shift scheduling", "staff scheduling"],
    "quality control": ["quality control", "quality assurance", "qa inspection"],
    "safety compliance": ["safety compliance", "osha", "workplace safety",
                          "safety protocols"],
    "equipment maintenance": ["equipment maintenance", "preventive maintenance",
                              "troubleshooting equipment"],
    "forklift": ["forklift", "pallet jack"],
    "blueprint reading": ["blueprint reading", "read blueprints", "schematics"],
    # --- universal --------------------------------------------------------
    "communication": ["communication skills", "written communication",
                      "verbal communication", "strong communicator"],
    "teamwork": ["teamwork", "team player", "collaborative"],
    "problem solving": ["problem solving", "problem-solving", "analytical thinking",
                        "critical thinking"],
    "time management": ["time management", "prioritization", "meeting deadlines"],
    "leadership": ["leadership", "team leadership", "mentoring", "coaching team"],
    "training": ["training staff", "staff training", "employee training"],
    "microsoft office": ["microsoft office", "ms office", "word and excel", "outlook"],
    "data entry": ["data entry", "record keeping", "recordkeeping"],
    "reporting": ["reporting", "report preparation", "dashboards"],
    "documentation": ["documentation", "technical writing", "sop"],
    "bilingual": ["bilingual", "spanish speaking", "fluent in spanish"],
}

# Ultra-short skills cannot use plain word boundaries ("go" is in "go to
# market", "r" is everywhere). They are matched only next to a domain cue.
_SHORT_SKILLS: Dict[str, Tuple[re.Pattern, re.Pattern]] = {
    "go": (
        re.compile(r"(?<![A-Za-z0-9_])go(?![A-Za-z0-9_])"),
        re.compile(r"(program|language|develop|engineer|backend|back end|"
                   r"python|java|rust|scala|kotlin|c\+\+|microservice|/|,)"),
    ),
    "r": (
        re.compile(r"(?<![A-Za-z0-9_])r(?![A-Za-z0-9_])"),
        re.compile(r"(python|sas|spss|stata|statistic|matlab|analytics|/|,)"),
    ),
}
_SHORT_WINDOW = 40


def _compile_lexicon() -> Tuple[re.Pattern, Dict[str, str]]:
    alias_to_canon: Dict[str, str] = {}
    for canon, aliases in SKILL_LEXICON.items():
        for alias in aliases:
            alias_to_canon.setdefault(alias, canon)
    # Longest first so "data warehouse" wins over "data" style prefixes.
    ordered = sorted(alias_to_canon, key=len, reverse=True)
    body = "|".join(a if _looks_like_regex(a) else re.escape(a) for a in ordered)
    pattern = re.compile(r"(?<![A-Za-z0-9_])(?:" + body + r")(?![A-Za-z0-9_])")
    return pattern, alias_to_canon


def _looks_like_regex(alias: str) -> bool:
    """Aliases carrying their own escaping (c\\+\\+, \\.net) are passed through."""
    return "\\" in alias


def _alias_lookup(alias_to_canon: Dict[str, str]) -> Dict[str, str]:
    """Match text -> canonical, with the regex-escaped aliases resolved to the
    literal string the scanner will actually produce.

    `\\b` is stripped BEFORE the backslashes are, and that ordering is the
    whole correctness of this function. Dropping backslashes alone turns
    `\\brtl\\b` into the literal `brtlb`, which no scan will ever produce — so
    the alias compiled into the pattern, matched "rtl" in a résumé, and then
    failed the reverse lookup and was silently dropped. An alias that matches
    and is then discarded is worse than one that never matched: it looks
    present in the lexicon and is absent from every result.

    Zero-width assertions are unnecessary in this lexicon anyway — the
    compiled pattern already brackets the whole alternation with
    `(?<![A-Za-z0-9_])` / `(?![A-Za-z0-9_])` — so this is belt and braces for
    the next person who writes one.
    """
    out: Dict[str, str] = {}
    for alias, canon in alias_to_canon.items():
        literal = alias
        if _looks_like_regex(alias):
            literal = re.sub(r"\\[bBAZ]", "", literal).replace("\\", "")
        out.setdefault(literal, canon)
    return out


_SKILL_RE, _ALIAS_TO_CANON = _compile_lexicon()
_LITERAL_TO_CANON = _alias_lookup(_ALIAS_TO_CANON)


def find_skills(text: str) -> List[str]:
    """Canonical skills mentioned in `text`, first-appearance order."""
    if not text:
        return []
    blob = _norm(text)
    seen: Set[str] = set()
    out: List[str] = []
    for m in _SKILL_RE.finditer(blob):
        canon = _LITERAL_TO_CANON.get(m.group(0))
        if canon and canon not in seen:
            seen.add(canon)
            out.append(canon)
    for canon, (rx, cue) in _SHORT_SKILLS.items():
        if canon in seen:
            continue
        for m in rx.finditer(blob):
            window = blob[max(0, m.start() - _SHORT_WINDOW): m.end() + _SHORT_WINDOW]
            if cue.search(window):
                seen.add(canon)
                out.append(canon)
                break
    return out


# ---------------------------------------------------------------------------
# Role families
# ---------------------------------------------------------------------------

#: LEGACY. `tech_taxonomy` (14 families / 101 specialties) is the canonical
#: occupation ontology as of `tech-tax-1.1.0`, and it is what `_score_role`
#: now scores against.
#:
#: This 32-key map is kept for exactly one reason: `job_postings.role_family`
#: is written from it on 405,133 stored rows and three production indexes key
#: on that field (`active_role_cost_posted_at` among them). Deleting it would
#: invalidate the stored value and the indexes in the same commit.
#:
#: So it stays as a BRIDGE, not as a taxonomy: `classify_role` still answers
#: "which legacy family is this" for retrieval and storage, and nothing in the
#: scoring path reads it for role alignment any more. `tech_taxonomy.
#: LEGACY_FAMILY_MAP` translates between the two. When the stored field is
#: re-backfilled onto `tech.role_families`, this can go.
ROLE_FAMILIES: Dict[str, List[str]] = {
    "software_engineering": [
        "software engineer", "software developer", "backend engineer", "back end engineer",
        "frontend engineer", "front end engineer", "full stack", "fullstack",
        "web developer", "application developer", "programmer", "sde",
        "member of technical staff", "mobile engineer", "ios engineer", "ios developer",
        "android engineer", "android developer", "api engineer",
        "software architect", "development engineer",
        # Software-flavored systems roles only. Bare "systems engineer" lives
        # under devops (infra / IT systems) so a Software Engineer chip's
        # family feed is not flooded with generic Systems Engineer postings.
        "software systems engineer",
        # Engineering leadership reads as the same family: the requirements are
        # the discipline's, and a senior IC resume legitimately matches them.
        "engineering manager", "engineering lead", "engineering leader",
        "head of engineering", "director of engineering", "vp of engineering",
        "software development",
        # Customer-facing engineering titles. Deliberately NOT filed under sales:
        # every one of these postings lists an engineering requirement set, which
        # is what the scorer actually measures.
        "solutions architect", "solution architect", "solutions engineer",
        "solution engineer", "forward deployed engineer", "partner engineer",
        # Seniority-prefixed bare engineer. Safe because matching is on literal
        # phrases, so "Staff Civil Engineer" does not contain "staff engineer".
        "staff engineer", "principal engineer", "distinguished engineer",
        "embedded engineer", "firmware engineer", "compiler engineer",
        "graphics engineer", "computer scientist",
        "performance engineer", "integration engineer", "applications engineer",
        "technical lead", "developer advocate", "developer experience",
    ],
    "data_engineering": [
        "data engineer", "analytics engineer", "etl developer", "big data engineer",
        "data platform engineer", "data infrastructure",
    ],
    "data_science": [
        "data scientist", "machine learning engineer", "ml engineer", "ai engineer",
        "research scientist", "applied scientist", "machine learning scientist",
    ],
    "data_analysis": [
        "data analyst", "business analyst", "bi analyst", "business intelligence",
        "reporting analyst", "insights analyst", "operations analyst",
        "strategy analyst", "analytics associate", "insights manager",
        "analytics manager", "decision scientist",
    ],
    "devops": [
        "devops", "site reliability", "sre", "platform engineer",
        "infrastructure engineer", "cloud engineer", "build engineer",
        "release engineer", "systems administrator", "sysadmin",
        # Bare "systems engineer" is infra/IT systems, not software product eng.
        # "software systems engineer" stays in software_engineering.
        "systems engineer",
        "devsecops", "cloud architect", "infrastructure architect",
        "reliability engineer", "tech ops", "cloud operations",
    ],
    "security": [
        "security engineer", "security analyst", "information security", "infosec",
        "application security", "appsec", "soc analyst", "penetration tester",
        "cybersecurity", "security architect",
        "detection engineer", "incident response", "security operations",
        "security manager", "security specialist", "threat intelligence",
        "security consultant", "grc analyst", "security engineering",
        # Bare "grc" (governance, risk, compliance) is unambiguous in a title.
        "grc", "security architecture", "vulnerability management",
        "identity and access",
    ],
    "qa": [
        "qa engineer", "quality assurance engineer", "test engineer", "sdet",
        "automation engineer", "qa analyst", "quality engineer",
        # A bare "Senior Tester" is unambiguous in a job title.
        "tester", "quality assurance", "test analyst", "qa specialist", "qa lead",
        "quality control", "test automation",
        # Bare "qa" as a standalone token is only ever quality assurance.
        "qa",
    ],
    "product": [
        "product manager", "product owner", "technical product manager",
        "group product manager",
        "product management", "director of product", "head of product",
        "principal product manager", "product lead", "product strategy",
    ],
    "design": [
        "ux designer", "ui designer", "product designer", "graphic designer",
        "visual designer", "ux researcher", "interaction designer",
        "user researcher", "user research", "design lead", "design manager",
        "motion designer", "content designer", "design system",
    ],
    "it_support": [
        "help desk", "helpdesk", "desktop support", "it support", "technical support",
        "network engineer", "network administrator", "it technician",
        "service desk", "systems analyst", "it analyst", "it operations",
        "field service technician", "it specialist", "systems support",
        "technical specialist", "application support", "sccm", "endpoint",
    ],
    "nursing": [
        "nurse", "registered nurse", " rn ", "lpn", "lvn", "cna",
        "nurse practitioner", "charge nurse", "clinical nurse", "staff nurse",
        "nursing supervisor", "nurse manager",
    ],
    "allied_health": [
        "medical assistant", "radiologic technologist", "pharmacist",
        "pharmacy technician", "physical therapist", "occupational therapist",
        "respiratory therapist", "phlebotomist", "medical technologist",
        "surgical technologist", "dental hygienist", "paramedic", "emt",
        "patient care", "caregiver", "care coordinator", "home health aide",
        "medical scribe", "clinical coordinator", "sterile processing",
        "clinical specialist", "sonographer", "veterinary technician", "optician",
    ],
    "physician": [
        "physician", "surgeon", "medical doctor", "hospitalist", "anesthesiologist",
        "psychiatrist", "radiologist",
    ],
    "accounting": [
        "accountant", "accounting", "bookkeeper", "controller", "auditor",
        "accounts payable", "accounts receivable", "tax preparer", "tax associate",
        "staff accountant", "senior accountant", "cpa",
        "payroll", "billing specialist", "financial reporting",
        "revenue accountant", "accounts receivable specialist",
    ],
    "finance": [
        "financial analyst", "finance manager", "investment analyst", "treasury analyst",
        "fp&a", "underwriter", "credit analyst", "loan officer", "actuary",
        # "equity research" sits here so it wins the leftmost match over
        # science_lab's "research associate" in "Equity Research Associate".
        "equity research", "finops", "order to cash", "revenue analyst",
        "pricing analyst", "banker", "treasury", "risk analyst",
        "collections specialist", "billing analyst", "portfolio manager",
        "quantitative analyst",
    ],
    "teaching": [
        "teacher", "instructor", "educator", "professor", "lecturer", "tutor",
        "curriculum specialist", "instructional designer", "teaching assistant",
        "principal of school", "school counselor",
        "curriculum developer", "academic advisor", "instructional coach",
        "training manager", "learning experience", "education specialist",
    ],
    "legal": [
        "paralegal", "legal assistant", "attorney", "lawyer", "counsel",
        "legal secretary", "law clerk", "contract manager", "compliance officer",
        "compliance specialist", "compliance analyst", "immigration specialist",
        "regulatory affairs", "contracts manager", "privacy counsel",
        "compliance manager",
        "vendor compliance", "compliance director", "risk and compliance",
        "ethics and compliance",
        # Bare "compliance" catches the "<domain> Senior Specialist" pattern that
        # qualified phrases miss ("Compliance Senior Specialist").
        "compliance", "communications compliance",
    ],
    "hr": [
        "recruiter", "human resources", "talent acquisition", "hr generalist",
        "hr manager", "people operations", "hr business partner", "hr coordinator",
        "talent management", "people partner", "hris", "compensation analyst",
        "benefits specialist", "employee relations", "learning and development",
        "training specialist", "talent partner",
        "talent development", "workforce", "staffing coordinator",
    ],
    "marketing": [
        "marketing manager", "marketing specialist", "marketing coordinator",
        "seo specialist", "content marketer", "brand manager", "social media manager",
        "growth marketer", "digital marketing", "content writer", "copywriter",
        "paid media", "field marketing", "product marketing",
        "communications manager", "social media", "demand generation",
        "public relations", "marketing analyst", "events manager",
        "lifecycle marketing", "marketing operations",
        # Qualified forms only: bare "communications" would swallow
        # "Communications Compliance Senior Specialist", which is legal.
        "corporate communications", "internal communications",
        "communications director", "brand marketing", "content strategist",
        "campaign manager",
        # Safe because legal claims "communications compliance" explicitly, and
        # the alternation is ordered longest-first, so the compound wins.
        "communications",
    ],
    "sales": [
        "sales representative", "account executive", "business development",
        "sales manager", "sales development", "inside sales", "sales associate",
        # "account manager" loses to "technical account manager" when both
        # match, because the alternation is ordered longest-first.
        "sales engineer", "sales enablement", "sales operations",
        "solutions consultant", "account manager", "partner development",
        "partnerships manager", "deal desk", "sales specialist",
        "sales consultant", "sales director", "sales lead", "sales support",
        "territory manager", "channel manager", "presales", "pre-sales",
        "revenue operations", "go-to-market", "sales analyst",
        "gtm", "client partnerships", "strategic alliances", "partner manager",
        "renewals", "account director", "commercial lead",
    ],
    "customer_support": [
        "customer support", "customer service", "client success", "customer success",
        "call center", "support specialist", "service advisor",
        "technical account manager", "implementation manager",
        "implementation specialist", "client services", "customer experience",
        "support engineer", "product support", "onboarding specialist",
        "client relationship",
    ],
    "operations": [
        "operations manager", "operations coordinator", "supply chain",
        "logistics coordinator", "warehouse", "inventory specialist",
        "production supervisor", "fulfillment",
        "procurement", "business operations", "operations specialist",
        "operations associate", "operations lead", "operations director",
        "operations analyst", "continuous improvement", "vendor manager",
        "process improvement", "facilities manager", "distribution center",
        "shift supervisor", "plant manager",
        "distribution operations", "property manager", "inventory control",
        "logistics manager", "warehouse associate", "supply planner",
        "production planner", "materials manager",
    ],
    "project_management": [
        "project manager", "program manager", "scrum master", "delivery manager",
        "project coordinator", "pmo",
    ],
    "admin": [
        "administrative assistant", "office manager", "executive assistant",
        "receptionist", "office coordinator", "administrative coordinator",
        "data entry clerk",
    ],
    "skilled_trades": [
        "electrician", "plumber", "hvac technician", "welder", "machinist",
        "maintenance technician", "millwright", "auto technician", "mechanic",
        "maintenance specialist", "maintenance mechanic", "facilities technician",
        "diesel technician", "hvac", "boilermaker", "pipefitter", "sheet metal",
    ],
    "driving": [
        "truck driver", "delivery driver", "cdl driver", "courier", "chauffeur",
        "driver helper", "route driver", "shuttle driver", "forklift",
    ],
    "construction": [
        "construction manager", "site supervisor", "foreman", "estimator",
        "superintendent", "civil engineer",
        "project engineer", "construction superintendent", "field superintendent",
        "surveyor", "architectural designer",
    ],
    "hospitality": [
        "chef", "line cook", "sous chef", "server", "bartender", "housekeeper",
        "hotel manager", "front desk agent", "banquet",
        "restaurant manager", "food service", "barista", "dishwasher",
        "kitchen manager", "catering",
    ],
    "retail": [
        "retail associate", "cashier", "store manager", "merchandiser",
        "sales floor", "shift lead",
        "keyholder", "key holder", "retail sales", "store associate",
        "sales floor associate", "visual merchandiser", "stocker",
    ],
    "social_work": [
        "social worker", "case manager", "counselor", "therapist", "clinician",
        "behavioral health", "community outreach",
        "care manager", "youth counselor", "substance abuse", "peer support",
    ],
    # Physical / industrial engineering. Previously homeless: every one of these
    # fell to "other" and was therefore unreachable by role-family retrieval,
    # while the nearest existing families would have been actively wrong
    # (a Mechanical Engineer is not construction, and not software).
    "hardware_engineering": [
        "mechanical engineer", "electrical engineer", "manufacturing engineer",
        "process engineer", "industrial engineer", "design engineer",
        "hardware engineer", "controls engineer", "structural engineer",
        "chemical engineer", "aerospace engineer", "rf engineer",
        "validation engineer", "packaging engineer", "fire safety engineer",
        "facade engineer", "electronics engineer", "product engineer",
        "tooling engineer", "materials engineer", "reliability engineering",
    ],
    "science_lab": [
        "lab technician", "laboratory technician", "research associate", "chemist",
        "biologist", "microbiologist", "scientist i", "lab assistant",
        "research technician", "formulation scientist", "process scientist",
        "principal scientist", "senior scientist", "staff scientist",
        "preclinical", "clinical development", "bioinformatics",
        "assay development",
    ],
}

# Adjacent families get partial role credit; a career change inside a
# neighbourhood is a real, defensible fit signal, a jump across it is not.
RELATED_FAMILIES: Dict[str, Set[str]] = {
    "software_engineering": {"data_engineering", "devops", "qa", "security",
                             "data_science", "hardware_engineering"},
    "data_engineering": {"software_engineering", "data_science", "data_analysis", "devops"},
    "data_science": {"data_engineering", "data_analysis", "software_engineering"},
    "data_analysis": {"data_science", "data_engineering", "finance", "operations"},
    "devops": {"software_engineering", "it_support", "security", "data_engineering"},
    "security": {"software_engineering", "devops", "it_support"},
    "qa": {"software_engineering", "data_analysis"},
    "product": {"project_management", "data_analysis", "design", "marketing"},
    "design": {"product", "marketing"},
    "it_support": {"devops", "security", "customer_support"},
    "nursing": {"allied_health", "physician", "social_work"},
    "allied_health": {"nursing", "physician", "science_lab"},
    "physician": {"nursing", "allied_health"},
    "accounting": {"finance", "data_analysis", "admin"},
    "finance": {"accounting", "data_analysis"},
    "teaching": {"social_work", "admin"},
    "legal": {"admin", "hr"},
    "hr": {"admin", "legal", "operations"},
    "marketing": {"sales", "design", "product"},
    "sales": {"marketing", "customer_support", "retail"},
    "customer_support": {"sales", "retail", "admin", "it_support"},
    "operations": {"project_management", "supply_chain", "admin", "retail"},
    "project_management": {"product", "operations", "construction"},
    "admin": {"customer_support", "hr", "operations"},
    "skilled_trades": {"construction", "operations"},
    "driving": {"operations"},
    "construction": {"skilled_trades", "project_management"},
    "hospitality": {"retail", "customer_support"},
    "retail": {"customer_support", "sales", "hospitality"},
    "social_work": {"nursing", "teaching"},
    "science_lab": {"allied_health", "data_science"},
    # `hardware_engineering` is a family `classify_role` and
    # `effective_role_family` can both RETURN, but it had no entry here - so
    # `RELATED_FAMILIES.get("hardware_engineering")` was always empty, the
    # legacy tier was always `unrelated`, and `_eligibility` turned that into
    # a hard INELIGIBLE with the score capped at 34. It is the only producible
    # family in `ROLE_FAMILIES` that was missing, and 150 of 7,054 live
    # software-titled postings (2.1%) route to it - "AWS Cloud Engineering
    # Intern", "Site Reliability Engineer (Manufacturing Infrastructure)".
    # Embedded and firmware work is adjacent to software, not a different
    # field; the edge is declared in both directions, as every other pair here
    # is.
    "hardware_engineering": {"software_engineering", "devops", "qa"},
}


def _compile_families() -> Tuple[re.Pattern, Dict[str, str]]:
    phrase_to_family: Dict[str, str] = {}
    for fam, phrases in ROLE_FAMILIES.items():
        for p in phrases:
            phrase_to_family.setdefault(p.strip(), fam)
    ordered = sorted(phrase_to_family, key=len, reverse=True)
    body = "|".join(re.escape(p) for p in ordered)
    return re.compile(r"(?<![A-Za-z0-9_])(?:" + body + r")(?![A-Za-z0-9_])"), phrase_to_family


_FAMILY_RE, _PHRASE_TO_FAMILY = _compile_families()

# The software families. A title that resolves into one of these but carries a
# PHYSICAL-engineering qualifier has been misread.
_SOFTWARE_FAMILIES = frozenset({
    "software_engineering", "devops", "data_engineering", "data_science",
    "data_analysis", "security", "qa", "it_support",
})

# Physical / plant / hardware domain qualifiers. Measured problem: the taxonomy
# matches the ROLE NOUN ("reliability engineer" -> devops, "systems engineer" ->
# devops) and ignores the qualifier in front of it, so a live
# US-only devops feed ranked these ABOVE real SRE roles for a cloud engineer:
#
#   "Corrosion Reliability Engineer II/III"            -> devops   (score 75)
#   "Staff Hardware Reliability Engineer"              -> devops
#   "Build Reliability Engineer II, Propulsion"        -> devops
#   "Senior Reliability Engineer - Power Electronics"  -> devops
#   "Instrument and Electrical Reliability Engineer"   -> devops
#   "Power Systems Engineer III - Generator/Plant ..." -> software_engineering
#
# 30 of the 565 postings in the devops family were mechanical/hardware roles
# of this shape. They are real engineering jobs — they belong in
# `hardware_engineering`, which the taxonomy already has (Mechanical, Chemical
# and Process Engineer all classify there correctly) — just not in a software
# candidate's feed. Word-boundary matched, and deliberately NOT including
# "systems"/"platform"/"build" on their own, which are ordinary software words.
_PHYSICAL_QUALIFIER_RE = re.compile(
    r"(?<![a-z])("
    r"corrosion|propulsion|geotechnical|metallurg\w*|welding|hvac|"
    r"mechanical|structural|civil|chemical|petroleum|pipeline|drilling|"
    r"refinery|plant|generator|turbine|boiler|instrumentation|instrument and|"
    r"power electronics|power systems|power generation|power distribution|"
    r"electrical grid|substation|electrical|electromechanical|hardware|firmware|"
    r"rf |antenna|semiconductor|wafer|fab |foundry|"
    r"manufacturing|industrial|process safety|machining|tooling|"
    r"aerospace structures|airframe|avionics hardware|automotive body|"
    r"packaging|textile|agricultural|marine|mining|nuclear|"
    r"wastewater|water treatment|environmental|hydraulic|pneumatic|"
    r"facilities|maintenance|hvacr|calibration"
    r")(?![a-z])", re.IGNORECASE)


#: A role an employer could only mean as software, whatever team it sits on.
_UNAMBIGUOUS_SOFTWARE_HEAD = re.compile(
    r"(?<![a-z])(software|developer|data engineer|data scientist|data analyst|"
    r"application development|"
    r"front ?end|back ?end|full ?stack|web|mobile|ios|android|devops|cloud)(?![a-z])")
_TITLE_SEGMENT_SPLIT = re.compile(r"\s*(?:,|\s-\s|\s–\s|\s—\s|\(|\||/|:)\s*")


_SOFTWARE_MAINTENANCE = re.compile(
    r"(?<![a-z])(application|software|systems?)\s+(development\s+and\s+)?"
    r"(maintenance|support)(?![a-z])")


def _names_a_physical_domain(blob: str) -> bool:
    """`_PHYSICAL_QUALIFIER_RE`, except that "Application Development and
    Maintenance" is software being maintained, not a plant."""
    return bool(_PHYSICAL_QUALIFIER_RE.search(_SOFTWARE_MAINTENANCE.sub(" ", blob)))


def _team_name_only_qualifier(blob: str) -> bool:
    """True when the physical word is the TEAM, not the job.

    "Software Engineer, Test Frameworks & Tooling" and "Data Engineer,
    Hardware & Consumer Devices" are software roles named for the group they
    serve; both read `hardware_engineering` and left every software feed
    (live, 2026-10-06). The veto still stands when the qualifier modifies the
    role itself ("Hardware Reliability Engineer") or when the role noun is one
    a plant also uses ("Reliability Engineer - Power Electronics").
    """
    head = _TITLE_SEGMENT_SPLIT.split(blob, maxsplit=1)[0]
    return bool(_UNAMBIGUOUS_SOFTWARE_HEAD.search(head)) \
        and not _PHYSICAL_QUALIFIER_RE.search(head)


_EARLY_ROLE_NOUN = (r"(intern|interns|internship|co ?op|apprentice\w*|trainee|"
                    r"new ?grad\w*|graduate|resident|residency|fellow|fellowship|"
                    r"engineer|scientist|researcher|analyst|developer|specialist)")
#: Discipline-noun titles, tried only when no role phrase matched.
_DISCIPLINE_FAMILIES: Sequence[Tuple[str, re.Pattern]] = (
    ("software_engineering", re.compile(
        r"(?<![a-z])(software|web|back ?end|front ?end|full ?stack|mobile|ios|"
        r"android|platform|cloud|api|application|systems software)\s+"
        r"(engineering|development)(?![a-z])")),
    ("data_engineering", re.compile(r"(?<![a-z])data\s+engineering(?![a-z])")),
    # NOT bare "infrastructure" or "production" engineering: a water utility
    # and an assembly line use both ("Water Infrastructure Engineering
    # Intern", "Production Engineering (Assembly)").
    ("devops", re.compile(
        r"(?<![a-z])(devops|site reliability|cloud infrastructure|"
        r"platform infrastructure)\s+engineering(?![a-z])")),
    ("security", re.compile(
        r"(?<![a-z])(security|cybersecurity|cyber security)\s+engineering(?![a-z])")),
    # NOT bare "quality" or "test" engineering: on a factory floor those are
    # manufacturing roles ("Material Quality Engineering Co-Op", "Quality
    # Engineering Technician, Dimensional"), and `qa` here means software QA.
    ("qa", re.compile(
        r"(?<![a-z])(software quality|quality assurance|software test|"
        r"test automation)\s+engineering(?![a-z])")),
    ("data_science", re.compile(
        r"(?<![a-z])(machine learning|deep learning|data science|applied science|"
        r"artificial intelligence|computer vision|natural language processing)"
        r"(?![a-z]).{0,40}?(?<![a-z])" + _EARLY_ROLE_NOUN + r"(?![a-z])|"
        r"(?<![a-z])" + _EARLY_ROLE_NOUN + r"(?![a-z]).{0,24}?(?<![a-z])"
        r"(machine learning|data science|applied science)(?![a-z])")),
    ("hardware_engineering", re.compile(
        r"(?<![a-z])(hardware|electrical|mechanical|firmware|embedded)\s+"
        r"engineering(?![a-z])")),
)


_IT_SERVER = re.compile(
    r"(?<![a-z])(sql|windows|linux|unix|web|application|app|database|db|exchange|"
    r"sharepoint|mail|file|proxy|virtual|vmware|cloud|game|media|build|"
    r"mcp|api)\s+servers?(?![a-z])"
    r"|(?<![a-z])servers?[\s\-]+(side|admin|administrator|administration|"
    r"engineer|engineering|developer|architect|infrastructure|support|"
    r"technician|operations|hardware|platform|systems?)(?![a-z])")
_IT_SERVER_WORD = re.compile(r"(?<![a-z])servers?(?![a-z])")
#: Database administration and development: the data family, by title.
_DATABASE_ROLE = re.compile(
    r"(?<![a-z])(dba|database\s+(administrator|admin|engineer|developer|"
    r"architect|specialist)|sql\s+server\s+(developer|dba|administrator|"
    r"engineer)|sql\s+developer)(?![a-z])")


def classify_role(text: Any) -> Optional[str]:
    """Role family for a title (or a short blob). Returns None when nothing
    matches. It NEVER falls back to software_engineering: defaulting an
    unrecognised nontechnical title into an engineering family is exactly the
    failure this engine exists to prevent."""
    blob = _norm(text)
    if not blob.strip():
        return None
    # A machine, not a waiter. "server" is a hospitality title, and every
    # "SQL Server Developer", "Windows Server Administrator" and "Linux Server
    # Engineer" was being filed with the bartenders - hidden from the people
    # it is for and shown to the people it is not.
    if _IT_SERVER.search(blob):
        if _DATABASE_ROLE.search(blob):
            return "data_engineering"
        blob = _IT_SERVER_WORD.sub(" ", blob)
    elif _DATABASE_ROLE.search(blob) and not _FAMILY_RE.search(blob):
        return "data_engineering"
    m = _FAMILY_RE.search(blob)
    if m:
        fam = _PHRASE_TO_FAMILY.get(m.group(0))
        # A physical-domain qualifier vetoes a software reading. "Hardware
        # Reliability Engineer" is a hardware role that happens to contain the
        # phrase "reliability engineer"; reading it as SRE puts a corrosion
        # engineer at the top of a cloud engineer's feed.
        if fam in _SOFTWARE_FAMILIES and _names_a_physical_domain(blob) \
                and not _team_name_only_qualifier(blob):
            return "hardware_engineering"
        return fam
    # The DISCIPLINE form: "Software Engineering Intern", "Data Science
    # Intern", "Machine Learning Intern". The phrase table knows the role noun
    # ("software engineer") and not the discipline, so every internship titled
    # this way had no family. Reached only when no phrase matched at all.
    for fam, rx in _DISCIPLINE_FAMILIES:
        if rx.search(blob):
            if fam in _SOFTWARE_FAMILIES and _names_a_physical_domain(blob) \
                    and not _team_name_only_qualifier(blob):
                return "hardware_engineering"
            return fam
    # "nurse"/"rn" appear as bare tokens far more than as phrases.
    if re.search(r"(?<![A-Za-z0-9_])(nurse|nursing|rn)(?![A-Za-z0-9_])", blob):
        return "nursing"
    if re.search(r"(?<![A-Za-z0-9_])(teacher|teaching)(?![A-Za-z0-9_])", blob):
        return "teaching"
    if re.search(r"(?<![A-Za-z0-9_])(developer|engineer)(?![A-Za-z0-9_])", blob) and \
            re.search(r"(software|web|backend|back end|frontend|front end|application|"
                      r"platform|api|mobile|cloud)", blob):
        return "software_engineering"
    return None


# ---------------------------------------------------------------------------
# Seniority
# ---------------------------------------------------------------------------

# The bands, lowest to highest. `intern` is a band of its own and NOT a synonym
# for `entry`: a candidate six weeks from a Master's is looking for a new-grad
# role, and folding internships into `entry` fills that feed with unpaid summer
# positions while burying the graduate programmes they actually want. The
# separation also runs the other way - an intern posting is a real result for
# someone still enrolled.
#: `principal` is its own band, between `staff` and `director`.
#:
#: It used to fold into `director`, and that was wrong in both directions.
#: A Principal Engineer is a senior INDIVIDUAL CONTRIBUTOR, not a people
#: manager, so folding them together made a "Principal Engineer" search
#: return engineering directors and vice versa. It was also unreachable:
#: `tech_feed_service.normalize_request` accepts `principal` as a seniority
#: filter and `tech_taxonomy._SENIORITY_ORDER` has always carried it, so a
#: user could select a band that `job_postings.seniority` could never hold —
#: the same shape of silent-zero-results bug `_LEVEL_ALIASES` documents.
#: These three vocabularies now agree.
LEVELS: Tuple[str, ...] = ("intern", "entry", "mid", "senior", "staff",
                           "principal", "director", "executive")

# Titles that contain a band word but do NOT carry that band. Checked BEFORE
# `_LEVEL_PATTERNS`, because each one is a measured misclassification off the
# live inventory (2026-08-27, 284,416 postings):
#
#   "staff"     -> 59,218 rows, 21% of the corpus, which is not a shape any
#                  real labour market has. The bulk were "RN Staff Nurse",
#                  "Staff Accountant" and "Staff Pharmacist", where "staff"
#                  means rank-and-file - the OPPOSITE of the IC staff band.
#   "manager"   -> "Restaurant Shift Manager", "Retail Store Manager",
#                  "PT Merchandising Manager" all read as staff-level ICs.
#   "assistant" -> pulled "Assistant Director" and "Assistant Vice President"
#                  down into entry.
#   "associate" -> pulled "Associate Director" and "Associate Professor" down
#                  into entry.
_STAFF_IS_RANK_AND_FILE = re.compile(
    r"(?<![a-z])staff\s+(nurse|rn|lpn|cna|pharmacist|physician|therapist|"
    r"technician|tech|technologist|assistant|associate|accountant|auditor|"
    r"attorney|counsel|writer|editor|chaplain|counselor|aide|clerk|"
    r"interpreter|dietitian|psychologist|surveyor|appraiser|sergeant|officer)"
    r"(?![a-z])")
# "Member of Technical Staff" / "MTS" is a job-family label, not the IC staff
# band: on the live corpus it reads `staff` off the bare word and hides the
# role from new-grad/entry users and the guest entry view, even when the title
# literally says "New Grad". With the staff rung skipped, the scan falls
# through to the entry rung when an early-career word is present, else returns
# None (unknown) — "unknown is not a no".
_STAFF_IS_MTS = re.compile(
    r"(?<![a-z])member\s+of\s+(?:[a-z]+\s+)?technical\s+staff(?![a-z])|"
    r"(?<![a-z])MTS(?![a-z])|"
    r"(?<=member of )technical\s+staff(?![a-z])", re.IGNORECASE)
# "Assistant/Associate <senior thing>" is senior, not junior.
_JUNIOR_WORD_IS_SENIOR = re.compile(
    r"(?<![a-z])(assistant|associate|deputy)\s+"
    r"(director|vice president|vp|president|dean|professor|principal|chief|"
    r"head|superintendent|controller|treasurer|commissioner|administrator|"
    r"general counsel|partner)(?![a-z])")

# The reverse demotion: band words that mean TRAINEE in medicine and academia.
# "Clinical Fellow", "Research Fellow" and "Chief Resident" are early-career
# posts, and reading them as director/executive puts a first-year doctor in the
# same band as a VP.
_SENIOR_WORD_IS_TRAINEE = re.compile(
    r"(?<![a-z])((clinical|research|post ?doctoral|postdoc|teaching|medical|"
    r"surgical|academic|graduate)\s+fellow|chief\s+resident|"
    r"fellowship)(?![a-z])")

#: An engineering manager, in every spelling live titles use: "Software
#: Engineering Manager", "Engineering Manager II", "Site Reliability Engineering
#: (SRE) Manager", "Manager, Software Engineering", "Manager, Technology
#: Delivery". "Business Development Manager" is a sales role and is excluded.
_ENGINEERING_MANAGER = (
    r"(?<![a-z])(?<!business )(?:software|engineering|development|devops|sre|"
    r"platform|infrastructure|reliability)\s+(?:\([a-z/&]+\)\s+)?manager(?![a-z])|"
    r"(?<![a-z])manager,?\s+(?:of\s+)?(?:software|engineering|platform|"
    r"infrastructure|technology)(?![a-z])")

_LEVEL_PATTERNS: Sequence[Tuple[str, re.Pattern]] = (
    # 1. Internships and co-ops FIRST, so "Software Engineer Intern" does not
    #    fall through to the entry rule and become indistinguishable from a
    #    new-grad role.
    ("intern", re.compile(r"(?<![a-z])(intern|interns|interne|internship|"
                          r"co[ -]?op|summer analyst|summer associate|"
                          r"industrial placement|work placement|"
                          r"placement student|student worker|"
                          r"student assistant|praktikum)(?![a-z])")),
    # `avp` is deliberately absent: at a bank "AVP" is Assistant Vice
    # President, a mid-band individual contributor, not an officer.
    ("executive", re.compile(r"(?<![a-z])(chief|c[eft]o|coo|cio|ciso|cmo|chro|"
                             r"vp|svp|evp|vice president|head of|"
                             r"president|managing director|"
                             r"executive director)(?![a-z])")),
    # `partner` is deliberately absent: "Partner Solutions Engineer" and
    # "Channel Partner Manager" are not partners.
    # `principal`, `distinguished` and `fellow` are the IC ladder above
    # staff; `director` and friends are the management ladder. Reading them
    # as one band meant a Principal Engineer and an Engineering Director were
    # the same result.
    ("director", re.compile(r"(?<![a-z])(director|dean|superintendent)(?![a-z])")),
    ("principal", re.compile(r"(?<![a-z])(principal|distinguished|"
                             r"fellow|senior\s+principal)(?![a-z])")),
    # 4. The IC staff band. `manager`, `supervisor` and `lead` are NOT here any
    #    more - see `_MANAGER_LEVEL` below, which reads them at the band they
    #    actually sit at.
    ("staff", re.compile(r"(?<![a-z])(staff|architect)(?![a-z])")),
    # The numeric alternatives carry their own, TIGHTER boundaries: a bare
    # `(?<![a-z])` lets the trailing digit of "Engineer R-1234" match `[34]`
    # and file the posting as senior, and the trailing `.` of "Autonomy 2.0"
    # gets past a plain `(?![a-z0-9])`.
    # "Lead <...> Engineer/Developer/Scientist" is a senior IC title (live:
    # JPMorgan's "Lead Software Engineer" asks 5+ years and read `mid`); a bare
    # "Lead" or "Team Lead" stays with the manager rules below ("Lead Cashier").
    # Rungs V/VI and 5/6 are senior too (Netflix "Software Engineer 5").
    ("senior", re.compile(r"(?<![a-z])(senior|snr|sr\.?)(?![a-z])|"
                          # People who manage or lead ENGINEERS, read here so a
                          # rung cannot outrank them: "Engineering Manager II",
                          # "Test Engineering Manager 2" and "Site Reliability
                          # Engineering (SRE) Manager" all read `mid` and showed
                          # on Entry+Mid boards (live, 2026-10-03).
                          + _ENGINEERING_MANAGER + r"|"
                          r"(?<![a-z])(?:engineering|software|development|devops|"
                          r"sre|platform|infrastructure)\s+(?:team|pod|squad)\s+lead(?![a-z])|"
                          r"(?<![a-z])(?:engineer|developer)\s*,\s*lead(?![a-z])|"
                          r"(?<![a-z])lead\s+(?:[a-z/+#.-]+\s+){0,2}"
                          r"(?:engineer|developer|scientist|programmer|administrator)(?![a-z])|"
                          # ...and the lead AFTER the discipline: "Site Reliability
                          # Engineering Lead", "Tech Lead". "Team Lead" and
                          # "Shift Lead" stay with the manager rules.
                          r"(?<![a-z])(?:engineering|engineer|software|reliability|"
                          r"platform|infrastructure|devops|sre|ml|qa|security|"
                          r"frontend|backend|data\s+engineering)\s+lead(?![a-z])|"
                          r"(?<![a-z])tech(?:nical)?\s+lead(?![a-z])|"
                          r"(?<![a-z0-9])(i{3,4}|iv|vi?|[3-6])(?![a-z0-9.])")),
    # 6. Early career. This list is the answer to "I never see early-career
    #    roles": the old one carried only intern/junior/graduate/trainee/
    #    apprentice/associate/assistant, and so missed the phrasing most large
    #    employers actually use for their graduate pipeline - "New Grad",
    #    "Early Career", "Campus Hire", "Rotational Program", "University
    #    Graduate", "Engineer I". Every alternative below appears in live
    #    inventory titles.
    ("entry", re.compile(
        r"(?<![a-z])("
        r"new\s?grad(uate)?s?|recent\s+grad(uate)?|university\s+grad(uate)?|"
        # A class year next to "grad" is a graduate intake: "Software Engineer
        # Grad 2027" (Pure Storage) named no level and was cut from every
        # entry-level board.
        r"grad(uate)?s?\s+20\d\d|20\d\d\s+grad(uate)?s?|"
        r"college\s+grad(uate)?|grad(uate)?\s+(engineer|developer|analyst|"
        r"scientist|programme|program|scheme|role|hire|trainee)|"
        r"campus\s+(hire|recruit|program|programme)|on[ -]campus|"
        # A bare "(Campus)" is a graduate-hire marker, but only on an
        # ENGINEERING title: scope it to a tech role noun within a short
        # bounded distance, in either order, so "Software Engineer (Campus)"
        # reads entry while "Campus Police"/"Campus Security" do not.
        r"campus(?:[^a-z0-9]+(?:[a-z]+\s+){0,2}(engineer|developer|programmer|"
        r"software|data\s+scientist|analyst|sde|swe))|"
        r"(engineer|developer|programmer|software|data\s+scientist|analyst|"
        r"sde|swe)(?:[^a-z0-9]+(?:[a-z]+\s+){0,2}campus)|"
        r"early\s+(career|professional|talent)|early[ -]in[ -]career|"
        r"entry[ -]?level|entry\s+career|"
        r"rotational|rotation\s+program|"
        r"leadership\s+development\s+program|"
        r"junior|jnr|jr\.?|trainee|apprentice(ship)?|fresher|"
        r"graduate|associate|assistant|"
        r"level\s*1|l1|tier\s*1|grade\s*1"
        r")(?![a-z0-9])|"
        r"(?<![a-z0-9])(i|1)(?![a-z0-9.])")),
    # 7. Rung two of a numbered ladder. It used to share the entry rule, so
    #    "Software Engineer II", "Program Manager 2" and "Private Client
    #    Advisor II" all read entry - on live inventory they asked for 3, 6-8
    #    and 12+ years. On every ladder that numbers its rungs, I is the
    #    entry rung and II is the next one up. Checked AFTER the entry words,
    #    so "Junior Engineer II" still reads junior.
    ("mid", re.compile(
        r"(?<![a-z])(level\s*2|l2|tier\s*2|grade\s*2)(?![a-z0-9])|"
        r"(?<![a-z0-9])(ii|2)(?![a-z0-9.])")),
)

# Management titles, read by what they MANAGE rather than by the bare word.
# Checked after the executive/director rules (so "Director of Engineering" and
# "VP, Sales" are already claimed) and before the IC bands.
_MANAGER_LEVEL: Sequence[Tuple[str, re.Pattern]] = (
    # A senior/group/general manager is a real second-line band.
    # An ENGINEERING manager leads engineers - live, NVIDIA's "Software
    # Engineering Manager" asks 8+ years with 3+ leading teams - and fell to
    # the generic `mid` rule meant for "Restaurant Manager".
    ("senior", re.compile(r"(?<![a-z])(senior|sr\.?|group|general|regional|"
                          r"district|division|area|plant|site|national|global)"
                          r"\s+manager(?![a-z])|" + _ENGINEERING_MANAGER)),
    # An assistant/associate/shift/team manager is not.
    ("entry", re.compile(r"(?<![a-z])(assistant|associate|shift|trainee)\s+"
                         r"(manager|supervisor|lead)(?![a-z])")),
    # Everything else that manages people or a shift: mid. "Restaurant
    # Manager", "Engineering Manager", "Team Lead", "Shift Supervisor".
    # "Lead Generation Specialist" is a sales job, not a lead.
    ("mid", re.compile(r"(?<![a-z])(manager|mgr|supervisor|lead(?!\s+gen)|foreman|"
                       r"crew chief)(?![a-z])")),
)

# Years a level normally implies, used only when the posting names no number.
_LEVEL_YEARS: Dict[str, int] = {
    "intern": 0, "entry": 0, "mid": 3, "senior": 5, "staff": 8,
    "principal": 11, "director": 10, "executive": 12,
}

# Ordinal seniority bands, used to compare a resume's title level against the
# posting's. Inserting `intern` at the bottom shifts every band by one and so
# leaves every EXISTING gap unchanged; only comparisons against `intern` are
# new.
_LEVEL_ORDER: Dict[str, int] = {name: i for i, name in enumerate(LEVELS)}
# Bands where a two-plus-band under-level is a genuine screen-out signal, not
# just a stretch.
_SENIOR_PLUS: frozenset = frozenset({"senior", "staff", "principal",
                                     "director", "executive"})
# Bands an early-career candidate is actually looking at.
EARLY_CAREER_LEVELS: frozenset = frozenset({"intern", "entry"})

# Every spelling of a band we accept from a USER preference, a resume, or a
# stored document, mapped onto `LEVELS`.
#
# This map exists because of a silent zero-results bug: onboarding stores
# `user_preferences.experience_level` as "Junior", "Entry", "Mid", "Senior" -
# and "Junior" is not a value `job_postings.seniority` has ever held, so a
# filter built from it matched nothing at all. A vocabulary the writer and the
# reader do not share is not a filter, it is an outage.
_LEVEL_ALIASES: Dict[str, str] = {
    "intern": "intern", "internship": "intern", "co-op": "intern",
    "coop": "intern", "student": "intern", "trainee": "intern",

    "entry": "entry", "entry level": "entry", "entry-level": "entry",
    "entrylevel": "entry", "junior": "entry", "jr": "entry", "jnr": "entry",
    "new grad": "entry", "new graduate": "entry", "newgrad": "entry",
    "graduate": "entry", "grad": "entry", "recent graduate": "entry",
    "early career": "entry", "early-career": "entry", "associate": "entry",
    "apprentice": "entry", "fresher": "entry", "0-2": "entry",
    "beginner": "entry",

    "mid": "mid", "mid level": "mid", "mid-level": "mid", "midlevel": "mid",
    "intermediate": "mid", "experienced": "mid", "3-5": "mid",

    "senior": "senior", "sr": "senior", "senior level": "senior",
    "senior-level": "senior", "6-10": "senior",

    "staff": "staff", "architect": "staff", "expert": "staff",

    "director": "director", "principal": "director", "manager": "mid",
    "lead": "mid", "head": "executive", "executive": "executive",
    "vp": "executive", "c-level": "executive", "chief": "executive",
}


def normalize_level(value: Any) -> Optional[str]:
    """Any spelling of a seniority band -> one of `LEVELS`, or None.

    The single place a level string is interpreted. Callers that read a level
    off a user preference, a resume or a stored document go through this rather
    than lowercasing and hoping, because the writers of those three do not
    agree on spelling and never have.
    """
    key = re.sub(r"\s+", " ", str(value or "").strip().lower())
    if not key:
        return None
    if key in LEVELS:
        return key
    if key in _LEVEL_ALIASES:
        return _LEVEL_ALIASES[key]
    # "Junior Engineer", "Entry-Level (0-2 years)": fall back to reading it as
    # a title, which is the same vocabulary by another route.
    return title_level(key)


_YEARS_IN_TITLE = re.compile(
    r"(?<![\d.])\d{1,2}\s*\+?\s*(?:(?:-|to)\s*\d{1,2}\s*\+?\s*)?(?:years?|yrs?)(?![a-z])")


# Employer level codes in titles - Netflix "(L5)", Meta "E4", Apple "ICT3".
# Their meaning is the EMPLOYER's: an L5 is senior at Netflix and Google but
# mid at Amazon, so they are read only for employers whose ladder is known.
_LADDER_CODE = re.compile(r"(?<![a-z0-9])(ict|ic|l|e)\s?-?(\d{1,2})(?![a-z0-9])")
_LADDERS: Dict[str, Dict[str, Dict[int, str]]] = {
    "google": {"l": {3: "entry", 4: "mid", 5: "senior", 6: "staff", 7: "principal", 8: "principal"}},
    "netflix": {"l": {3: "entry", 4: "mid", 5: "senior", 6: "staff", 7: "principal"}},
    "meta": {"e": {3: "entry", 4: "mid", 5: "senior", 6: "staff", 7: "principal"},
             "ic": {3: "entry", 4: "mid", 5: "senior", 6: "staff", 7: "principal"}},
    "amazon": {"l": {4: "entry", 5: "mid", 6: "senior", 7: "principal", 8: "principal"}},
    "apple": {"ict": {2: "entry", 3: "mid", 4: "senior", 5: "staff", 6: "principal"}},
    "uber": {"l": {3: "entry", 4: "mid", 5: "senior", 6: "staff", 7: "principal"}},
    "lyft": {"l": {3: "entry", 4: "mid", 5: "senior", 6: "staff", 7: "principal"}},
}
_LADDER_ALIASES = {"alphabet": "google", "facebook": "meta", "aws": "amazon",
                   "amazon web services": "amazon"}


def _ladder_for(employer: Any) -> Optional[Dict[str, Dict[int, str]]]:
    name = _norm(employer)
    for alias, key in _LADDER_ALIASES.items():
        if re.search(rf"(?<![a-z]){re.escape(alias)}(?![a-z])", name):
            return _LADDERS[key]
    for key, ladder in _LADDERS.items():
        if re.search(rf"(?<![a-z]){key}(?![a-z])", name):
            return ladder
    return None


def employer_ladder_level(title: Any, employer: Any) -> Optional[str]:
    """The band an employer's own level code in the title states, or None."""
    ladder = _ladder_for(employer)
    if not ladder:
        return None
    for m in _LADDER_CODE.finditer(_norm(title)):
        band = ladder.get(m.group(1), {}).get(int(m.group(2)))
        if band:
            return band
    return None


# Banks print a CORPORATE title, and theirs do not mean what the words mean
# anywhere else: a Vice President at Goldman Sachs or JPMorgan is a senior
# individual contributor with six to ten years, not an officer of the firm.
# Read by the general rules every such posting was `executive`, so it was
# hidden from exactly the senior engineers it is for. Named employers only.
_BANK_EMPLOYER = re.compile(
    r"(?<![a-z])(goldman sachs|jpmorgan|jp morgan|j p morgan|morgan stanley|"
    r"citi|citigroup|citibank|bank of america|merrill lynch|barclays|"
    r"deutsche bank|ubs|credit suisse|wells fargo|bny|bny mellon|"
    r"bank of new york|state street|blackrock|nomura|hsbc|bnp paribas|"
    r"societe generale|northern trust|rbc|royal bank of canada)(?![a-z])")
_BANK_ABOVE_VP = re.compile(
    r"(?<![a-z])(senior vice president|executive vice president|svp|evp|"
    r"chief|head of|managing director|president and|global head)(?![a-z])")
_BANK_AVP = re.compile(r"(?<![a-z])(assistant vice president|avp)(?![a-z])")
_BANK_VP = re.compile(r"(?<![a-z])(vice president|vp)(?![a-z])")
_BANK_ED = re.compile(r"(?<![a-z])executive director(?![a-z])")


def bank_title_level(title: Any, employer: Any) -> Optional[str]:
    """The band a bank's corporate title states, or None."""
    if not _BANK_EMPLOYER.search(_norm(employer)):
        return None
    text = _norm(title)
    if _BANK_ABOVE_VP.search(text):
        return None
    if _BANK_AVP.search(text):
        return "mid"
    if _BANK_VP.search(text):
        return "senior"
    if _BANK_ED.search(text):
        return "staff"
    return None


def level_with_employer(level: Any, title: Any, employer: Any,
                        required_years: Any) -> Any:
    """`level`, unless the title carries the employer's own level code - that
    is the stated level, raised (never lowered) by the years asked for."""
    stated = employer_ladder_level(title, employer)
    if not stated:
        # A bank's corporate title corrects the MISREADING ("Vice President"
        # as an executive) and fills a blank. It does not overrule a level the
        # title states in ordinary words: "Senior Analyst, AVP" stays senior.
        if level in (None, "", "unknown", "executive", "director"):
            stated = bank_title_level(title, employer)
    if not stated:
        return level
    by_years = _level_from_years(required_years)
    if by_years and stated in _YEARS_CAN_RAISE \
            and _LEVEL_ORDER[by_years] > _LEVEL_ORDER[stated]:
        return by_years
    return stated


def title_level(title: Any) -> Optional[str]:
    """The band a TITLE states, or None when it states none.

    None is a real answer and is never coerced to `mid`: "Software Engineer",
    "Data Analyst" and "Registered Nurse" carry no marker, and that is most of
    the market. `level_from_signals` resolves those from the description.
    """
    t = _norm(title)
    if not t.strip():
        return None
    # A count of years is not a rung: "Mechanical Engineer (M.S. + 3 years of
    # consulting experience)" read `senior` off the bare 3 (live, 2026-10-02).
    # `level_from_signals` still reads those years - as years.
    t = _YEARS_IN_TITLE.sub(" ", t)
    # Demotions and promotions that the ordered scan below would get wrong.
    if _JUNIOR_WORD_IS_SENIOR.search(t):
        return "director" if re.search(
            r"(?<![a-z])(assistant|associate|deputy)\s+"
            r"(vice president|vp|president|chief|dean|general counsel)"
            r"(?![a-z])", t) else "senior"
    if _SENIOR_WORD_IS_TRAINEE.search(t):
        return "entry"
    staff_is_rank_and_file = bool(_STAFF_IS_RANK_AND_FILE.search(t))
    staff_is_mts = bool(_STAFF_IS_MTS.search(t))

    for level, rx in _LEVEL_PATTERNS:
        if level == "staff" and (staff_is_rank_and_file or staff_is_mts):
            # "Staff Nurse" is not the staff band. Keep scanning: a genuine
            # marker later in the title still wins, and otherwise the
            # management scan below or `None` answers.
            continue
        if rx.search(t):
            return level
    for level, rx in _MANAGER_LEVEL:
        if rx.search(t):
            return level
    return None


# Phrases in a BODY that state an early-career target even when the title does
# not. Deliberately narrow: each one is an employer saying the quiet part out
# loud, not an inference from tone.
_EARLY_CAREER_JD = re.compile(
    r"(new\s?grad(uate)?s?|recent\s+grad(uate)?|graduating\s+(senior|student)|"
    r"campus\s+(hire|recruit)|early[ -]in[ -]career|early\s+career\s+program|"
    r"entry[ -]?level|no\s+prior\s+experience|no\s+experience\s+required|"
    r"0\s*[-to]{1,3}\s*2\s+years|1\s*[-to]{1,3}\s*2\s+years|"
    r"less\s+than\s+(one|two|1|2)\s+years?)")
_INTERN_JD = re.compile(r"(internship|intern\s+program|co[ -]?op\s+(program|term)|"
                        r"summer\s+internship|currently\s+enrolled)")

# Years -> band, for a posting whose TITLE names no level. The bands match
# `_LEVEL_YEARS` read backwards, so `level_from_signals` and `_score_experience`
# cannot disagree about what "5 years" means.
#: A years-only reading stops at `staff`. Years cannot distinguish a
#: Principal IC from a Director — that distinction is about the LADDER, which
#: only the title states — so a posting that names no level and asks for 12
#: years is `staff`, not a guess between two different jobs.
_YEARS_TO_LEVEL: Sequence[Tuple[int, str]] = (
    (2, "entry"), (4, "mid"), (7, "senior"), (9, "staff"),
)


def level_from_signals(title: Any, *, description: Any = "",
                       required_years: Any = None, employer: Any = None) -> Optional[str]:
    """The band, resolved from every signal available, or None.

    Title first, because a stated level is the employer's own answer. Then the
    body, then the years the posting asks for.

    Why this exists: on the live corpus 134,467 of 284,416 postings (47%) had
    `seniority: unknown` because the title was the ONLY input, while
    `requirements.required_years` was already extracted and sitting on the same
    document. Reading a band off "minimum 2 years of experience" is not a guess
    - it is the posting's own requirement, restated in the vocabulary the feed
    filters on.
    """
    stated = employer_ladder_level(title, employer) or title_level(title)
    if stated == "intern":
        # An internship that mentions "3 years of coursework" is still an
        # internship; years never move a posting out of this band.
        return stated

    body = _norm(description)
    # The WHOLE body for years: requirements sections sit at the end of long
    # descriptions, and the old 4,000-character window cut them off.
    years = _max_known(required_years, _required_years(body) if body else None,
                       _required_years(_norm(title)))
    by_years = _level_from_years(years)

    if stated:
        # The employer's word for the level, RAISED - never lowered - by the
        # years the same posting asks for. "Associate Engineer, 6+ years" and
        # "Program Manager 2, 8 years" are not entry roles whatever the title
        # says; "Senior Engineer, 2+ years" is still senior.
        if by_years and _LEVEL_ORDER[by_years] > _LEVEL_ORDER[stated] \
                and stated in _YEARS_CAN_RAISE:
            return by_years
        return stated

    # Body phrases only where the posting states no years that contradict
    # them. "We hire new grads across many teams" and "we also run a summer
    # internship program" are boilerplate on senior postings; a stated
    # "5+ years" is the requirement. The phrase window stays at 4,000
    # characters, where a posting describes its OWN audience; benefits and
    # EEO sections further down mention internships in general.
    hint_window = body[:4000]
    if hint_window:
        if _INTERN_JD.search(hint_window) and (years is None or years <= 1):
            return "intern"
        if _EARLY_CAREER_JD.search(hint_window) and (years is None or years <= 2):
            return "entry"
    # `_level_from_years` is the same ladder the RESUME side reads, so a
    # posting's "3+ years" and a candidate's three years resolve to the same
    # word. Years alone top out at `staff` there: "12+ years" is not evidence
    # of an executive posting - it is evidence of a senior one - and reading
    # it as `executive` put ordinary senior engineering roles three bands
    # above where they belong, which is a three-band `level_gap` against
    # every candidate who applied.
    return by_years


#: Title bands that stated years may raise. The IC ladder up to staff, where
#: years themselves top out. Management and executive bands are about the
#: ladder, which only a title states, so years never touch them.
_YEARS_CAN_RAISE = frozenset({"entry", "mid", "senior"})


def _max_known(*values: Any) -> Optional[int]:
    known = []
    for v in values:
        try:
            if v is not None:
                known.append(int(v))
        except (TypeError, ValueError):
            continue
    return max(known) if known else None


# ---------------------------------------------------------------------------
# Requirement section detection
# ---------------------------------------------------------------------------

_PREFERRED_HEAD = re.compile(
    r"(preferred|nice to have|nice-to-have|bonus|a plus|pluses|desired|"
    r"good to have|great to have|additional qualifications|extra credit|"
    r"we'd love|would be a plus|preferred qualifications)")
_REQUIRED_HEAD = re.compile(
    r"(minimum qualifications|basic qualifications|required qualifications|"
    r"requirements|required skills|must have|must-have|essential|"
    r"what you'll need|what you will need|who you are|qualifications|"
    r"what we're looking for|you have|you should have|responsibilities)")

_YEARS_RE = re.compile(
    r"(\d{1,2})\s*(?:\+|plus)?\s*(?:to|-|or more|or more years)?\s*(?:\d{1,2})?\s*"
    r"(?:\+)?\s*years?(?:'|\s+of)?\s*(?:[a-z ]{0,30}?)?experience")

_REMOTE_RE = re.compile(r"(fully remote|100% remote|remote first|remote-first|"
                        r"work from home|remote position|remote role|remote opportunity|"
                        r"\bremote\b)")
_ONSITE_RE = re.compile(r"(on site|on-site|onsite|in office|in-office|"
                        r"in person|in-person|must relocate|relocation required)")
_HYBRID_RE = re.compile(r"hybrid")

_DEGREE_LEVELS: Sequence[Tuple[int, re.Pattern]] = (
    (4, re.compile(r"(ph\.?d|doctorate|doctoral|m\.?d\.?(?![a-z])|j\.?d\.?(?![a-z]))")),
    (3, re.compile(r"(master'?s?(?![a-z])|m\.?s\.?(?![a-z])|m\.?b\.?a|msn|m\.?eng)")),
    (2, re.compile(r"(bachelor'?s?(?![a-z])|b\.?s\.?(?![a-z])|b\.?a\.?(?![a-z])|"
                   r"undergraduate degree|bsn|b\.?tech|four year degree|4 year degree)")),
    (1, re.compile(r"(associate'?s? degree|a\.?a\.?s\.?(?![a-z])|diploma|"
                   r"high school|ged(?![a-z])|vocational)")),
)

# Mandatory credentials: the JD trigger, the resume evidence, and
# `role_evidence` - a pattern over the resume's JOB TITLES that identifies
# someone who almost certainly holds the credential even though the resume
# never spells it out. `role_evidence` is matched on titles, not on the whole
# resume, precisely so that adjacent-but-different roles do not inherit it: a
# certified nursing assistant works in nursing and definitively does NOT hold
# an RN license, so that resume must be gated, while a charge nurse who simply
# omitted the licence line must not be.
LICENSE_RULES: Sequence[Dict[str, Any]] = (
    {
        "key": "rn_license",
        "label": "Registered nurse license",
        "jd": re.compile(r"(rn license|registered nurse license|current rn|active rn|"
                         r"licensed registered nurse|nclex|unencumbered rn|"
                         r"valid rn|rn licensure|compact license)"),
        "resume": re.compile(r"((?<![a-z])rn(?![a-z])|registered nurse|nclex|nursing license|"
                             r"licensure|bsn|adn(?![a-z]))"),
        "role_evidence": re.compile(
            r"(registered nurse|staff nurse|charge nurse|nurse practitioner|"
            r"nurse manager|clinical nurse|(?<![a-z])rn(?![a-z]))"),
    },
    {
        "key": "cpa_license",
        "label": "CPA license",
        "jd": re.compile(r"(cpa license|cpa certification|certified public accountant|"
                         r"active cpa|cpa required|cpa credential)"),
        "resume": re.compile(r"(cpa(?![a-z])|certified public accountant)"),
        # No unstated case: an accountant either holds a CPA or does not, and
        # the resume of someone who holds one says so.
        "role_evidence": None,
    },
    {
        "key": "bar_admission",
        "label": "Bar admission",
        "jd": re.compile(r"(bar admission|admitted to the bar|licensed attorney|"
                         r"member of the state bar|active bar license|jd required)"),
        "resume": re.compile(r"(bar admission|admitted to the bar|state bar|"
                             r"licensed attorney|j\.?d\.?(?![a-z]))"),
        "role_evidence": re.compile(r"(attorney|lawyer|counsel)"),
    },
    {
        "key": "teaching_license",
        "label": "State teaching license",
        "jd": re.compile(r"(teaching license|teaching certification|teaching credential|"
                         r"state certification to teach|certified teacher|"
                         r"valid teaching certificate)"),
        "resume": re.compile(r"(teaching license|teaching certification|"
                             r"teaching credential|certified teacher|"
                             r"state certified|licensed teacher)"),
        "role_evidence": re.compile(
            r"(teacher|instructor|educator|professor|lecturer)"),
    },
    {
        "key": "cdl",
        "label": "Commercial driver license",
        "jd": re.compile(r"(cdl(?![a-z])|commercial driver'?s? license)"),
        "resume": re.compile(r"(cdl(?![a-z])|commercial driver'?s? license)"),
        "role_evidence": re.compile(r"(driver|chauffeur|operator)"),
    },
    {
        "key": "pe_license",
        "label": "Professional engineer license",
        "jd": re.compile(r"(pe license|professional engineer license|licensed pe|"
                         r"p\.e\. license)"),
        "resume": re.compile(r"(pe license|professional engineer|p\.e\.(?![a-z]))"),
        "role_evidence": None,
    },
    {
        "key": "pharmacist_license",
        "label": "Pharmacist license",
        "jd": re.compile(r"(pharmacist license|licensed pharmacist|pharmd required|"
                         r"registered pharmacist)"),
        "resume": re.compile(r"(pharmacist|pharm\.?d)"),
        "role_evidence": re.compile(r"(pharmacist|pharm\.?d)"),
    },
)

# Widened in 2.2. The 2.1 pattern read only the phrasings that name a
# clearance the candidate ALREADY holds, and the defence-and-federal half of
# the inventory does not write it that way: "must be able to obtain and
# maintain a security clearance", "clearance eligible", "US Person as defined
# by ITAR", "subject to export control regulations (EAR/ITAR)", "position
# requires the ability to obtain a Public Trust". Every one of those is a
# stated requirement a reader needs to see, and every one of them was silent.
#
# This flag never penalises anyone: `_hard_constraints` records an unstated
# clearance as `status: "unknown"` with NO cap (a résumé cannot contradict a
# clearance requirement). Widening it therefore only ever SURFACES a
# requirement earlier, which is the whole purpose of the field.
_CLEARANCE_JD = re.compile(
    r"(security clearance|ts/sci|top[ -]secret|secret clearance|"
    r"active clearance|public trust|polygraph|dod clearance|"
    r"q clearance|l clearance|"
    r"(?:obtain|acquire|qualify for|be eligible for)\s+(?:and maintain\s+)?"
    r"(?:a|an|the)?\s*(?:u\.?s\.?\s+)?(?:government|dod|federal)?\s*"
    r"(?:security\s+)?clearance|"
    r"clearance[ -]eligible|eligible for (?:a )?clearance|"
    # A BARE "clearance required" IS DELIBERATELY NOT HERE. Measured on 4,000
    # live bodies, it is a FORM LABEL far more often than a requirement:
    # "government clearance required? no", "government clearance required?"
    # with no answer at all, and "minimum clearance required to start: none".
    # Every genuine statement carries a qualifier that one of the other
    # alternatives already reads ("security clearance", "TS/SCI", "active
    # clearance", "public trust", "obtain a clearance", "clearance eligible"),
    # so dropping it costs recall nothing measurable and removes 100% of the
    # false positives it produced.
    r"must (?:be able to )?(?:hold|obtain) a clearance|"
    # Left boundary is load-bearing: without it "indigenous persons" matches
    # "us persons".
    r"(?<![a-z])u\.?s\.? person(?:s)?(?: as defined)?|(?<![a-z])us person(?:s)?|"
    # `\bear\b` on its own would match the English word. Only the paired or
    # qualified forms of the Export Administration Regulations are read.
    r"export[ -]control(?:led|s)?|\bitar\b|itar\s*/\s*ear|ear\s*/\s*itar|"
    r"\bear\b\s+(?:regulation|requirement|restriction)|"
    r"government contract requires|federal contract requires|"
    r"national agency check|suitability determination)")
_CLEARANCE_RESUME = re.compile(
    r"(security clearance|ts/sci|top[ -]secret|secret clearance|"
    r"active clearance|public trust|polygraph|cleared to|"
    r"clearance[ -]eligible|dod clearance|q clearance|l clearance)")

# Widened in 2.2 for the same reason: the 2.1 pattern missed the spellings
# employers and staffing firms actually use. "must be authorized to work in the
# US without sponsorship now or in the future", "no C2C", "no third party
# candidates", "OPT/CPT candidates are not eligible", "must be a Green Card
# holder or US Citizen", "permanent work authorization required". All of these
# are the employer STATING a restriction, which is exactly what this flag
# means.
#
# Note the asymmetry this preserves: the flag alone never costs a candidate
# anything. `_hard_constraints` caps the score ONLY when the flag meets a
# résumé that states it needs sponsorship (`_WORKAUTH_RESUME_NEEDS`);
# otherwise it records `status: "unknown"` with no cap. Silence is never a
# mismatch.
_WORKAUTH_JD = re.compile(
    r"(no visa sponsorship|not (?:able|willing) to sponsor|"
    r"un(?:able|willing) to (?:provide |offer )?(?:visa )?sponsor|"
    # The sponsorship noun is MANDATORY here. An earlier draft allowed
    # "does not (provide|offer)" with an optional object, which matched "does
    # not provide relocation assistance" and would have flagged thousands of
    # postings that say nothing about work authorization at all.
    r"without sponsorship|does not (?:provide|offer|sponsor)\s+"
    r"(?:visa\s+|employment\s+|immigration\s+)?sponsorship|"
    r"does not sponsor\b|"
    # The negation and the noun in the same clause, with the object allowed to
    # sit between them ("we do not offer relocation or sponsorship"). Bounded
    # to 40 characters and stopped at a sentence break so the negation cannot
    # reach across into an unrelated sentence.
    r"(?:do(?:es)? not|will not|cannot|can ?not|are unable to|is unable to)\s+"
    r"(?:provide|offer|consider|support)[^.\n]{0,40}?sponsorship|"
    r"will not (?:provide|offer|consider)\s+(?:visa\s+)?sponsorship|"
    r"cannot sponsor|no sponsorship|not sponsor(?:ing)?|"
    r"sponsorship (?:is )?not (?:available|offered|provided)|"
    r"must be a u\.?s\.? citizen|"
    r"u\.?s\.? citizenship (?:is )?required|citizens? only|"
    r"(?:us|u\.?s\.?) citizen(?:ship)? or (?:permanent resident|green card)|"
    r"green[ -]card holder|permanent resident(?:s)? only|"
    r"authorized to work in the (?:u\.?s\.?|united states)"
    r"(?:[^.\n]{0,60}?)(?:without (?:the need for )?(?:visa )?sponsorship|"
    r"now or in the future)|"
    r"permanent (?:us |u\.?s\.? )?work authorization|"
    r"no (?:c2c|corp[ -]to[ -]corp|third[ -]part(?:y|ies))|"
    r"third[ -]party (?:candidates|resumes|agencies) (?:are )?not|"
    r"no (?:opt|cpt|h1b|h-1b)|"
    r"(?:opt|cpt)(?:/(?:opt|cpt))? (?:candidates )?(?:are )?not eligible|"
    r"not eligible for (?:opt|cpt|visa sponsorship)|"
    r"(?:w2|w-2) only|"
    r"government contract requires|"
    r"must be a (?:green[ -]card holder|lawful permanent resident))")
_WORKAUTH_RESUME_NEEDS = re.compile(
    r"(require[sd]? sponsorship|need sponsorship|visa sponsorship required|"
    r"f-1 opt|f1 opt|(?<![a-z])opt(?![a-z])|(?<![a-z])cpt(?![a-z])|"
    r"h-?1b (?:visa|transfer|required)|student visa)")

INDUSTRIES: Dict[str, List[str]] = {
    "healthcare": ["hospital", "clinic", "patient", "healthcare", "health care",
                   "medical center", "nursing", "clinical"],
    "finance": ["bank", "banking", "insurance", "investment", "fintech",
                "financial services", "lending", "wealth management"],
    "education": ["school", "district", "university", "college", "classroom",
                  "students", "k-12", "k12", "edtech"],
    "legal": ["law firm", "litigation", "attorney", "counsel", "legal department"],
    "government": ["federal agency", "government", "public sector", "municipal",
                   "department of"],
    "retail": ["retail", "store", "merchandise", "point of sale", "ecommerce",
               "e-commerce"],
    "manufacturing": ["manufacturing", "factory", "production line", "assembly",
                      "plant floor", "industrial"],
    "technology": ["saas", "software company", "tech company", "platform",
                   "startup", "developer tools", "cloud provider"],
    "logistics": ["logistics", "freight", "warehouse", "distribution center",
                  "supply chain", "shipping"],
    "hospitality": ["restaurant", "hotel", "hospitality", "guests", "food service"],
    "nonprofit": ["nonprofit", "non-profit", "ngo", "charitable", "donor"],
    "construction": ["construction", "job site", "contractor", "building trades"],
}

_STATE_RE = re.compile(r"(?<![A-Za-z])(a[klrz]|c[aot]|de|fl|ga|hi|i[adln]|k[sy]|la|"
                       r"m[adeinost]|n[cdehjmvy]|o[hkr]|pa|ri|s[cd]|t[nx]|ut|v[at]|"
                       r"w[aivy])(?![A-Za-z])")


# ---------------------------------------------------------------------------
# Requirement extraction
# ---------------------------------------------------------------------------

def _split_sections(blob: str) -> Tuple[str, str]:
    """(required_region, preferred_region).

    Everything that is not under an explicit preferred heading counts as
    required. That is the conservative reading of a posting and it keeps the
    denominator honest: an employer who lists 20 asks IS harder to fit than
    one who lists 4."""
    required: List[str] = []
    preferred: List[str] = []
    mode = "required"
    for line in blob.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        head = stripped[:90]
        # A short line that names a preferred heading switches the mode; a
        # bullet that merely says "a plus" only reclassifies itself.
        is_head = len(stripped) <= 90
        if _PREFERRED_HEAD.search(head):
            # Checked before the required heads on purpose: "preferred
            # qualifications" contains "qualifications", and reading it as a
            # required head would fold the whole nice-to-have block back into
            # the denominator.
            if is_head:
                mode = "preferred"
            preferred.append(stripped)
            continue
        if _REQUIRED_HEAD.search(head) and is_head:
            mode = "required"
        (preferred if mode == "preferred" else required).append(stripped)
    return "\n".join(required), "\n".join(preferred)


# One "N years" mention: the number (a range reads as its LOWER bound - "3-6
# years" asks for 3), then up to 80 characters of what the years are OF.
_HEADLINE_YEARS_RE = re.compile(
    r"(?<![\d.$])(\d{1,2})\s*(?:\+|plus)?\s*(?:(?:-|to)\s*\d{1,2}\s*\+?\s*)?"
    r"(?:or\s+more\s+)?"
    # A qualifier may sit between the number and "years": "8+ overall years",
    # "5+ total years", "6 combined years" (live: NVIDIA's engineering manager).
    r"(?:(?:overall|total|combined|cumulative|full[ -]time|professional|relevant|"
    r"related|industry|progressive|post[ -]graduate)\s+)?"
    r"(?:years?|yrs?)(?![a-z])'?\s*([^\n]{0,80})")
# What makes a count of years an EXPERIENCE requirement rather than an age, a
# company's history or a degree's length.
_YEARS_CONTEXT = re.compile(
    r"experience|(?<![a-z])exp(?![a-z])|(?<![a-z])work(ing)?(?![a-z])|industry|"
    r"professional|relevant|"
    r"related|hands on|engineering|development|programming|coding|(?<![a-z])role|"
    r"position|building|leading|managing|designing|supporting|administering|"
    r"running|operating|deploying|developing|writing|delivering|owning|"
    r"implementing|architecting|maintaining|testing|analy[sz]ing|"
    r"(?<![a-z])in (a|an)(?![a-z])")
# Award streaks too: "voted 13 years in a row a top workplace" filed a nursing
# assistant and a maintenance assistant as `staff` in the 2026-10-02 spot check.
_YEARS_NOT_EXPERIENCE = re.compile(
    r"of age|(?<![a-z])old(?![a-z])|in business|business for|history|founded|"
    r"anniversary|warranty|tenure|lease|coursework|of (college|school|study)|"
    r"(?<![a-z])ago(?![a-z])|in a row|consecutive|top workplace|best place")
_PREFERRED_CUE = re.compile(
    r"prefer|nice to have|nice-to-have|(?<![a-z])bonus(?![a-z])|a plus(?![a-z])|"
    r"ideal(ly)?(?![a-z])|desired|desirable|advantageous")
_REQUIRED_CUE = re.compile(r"required|(?<![a-z])must(?![a-z])|minimum|at least")
_SENTENCE_SPLIT = re.compile(r"(?<=[.;])\s+|\s*•\s*")


def _required_years(blob: str) -> Optional[int]:
    """The posting's HEADLINE experience requirement, in years, or None.

    The headline, not the smallest number. "5+ years of software engineering,
    2+ years with AWS" asks for five; the old reader returned the minimum over
    every match and called it two, which filed senior postings as entry and
    let the scorer believe a two-year candidate met them. Measured on live
    inventory 2026-10-02: Program Manager 2 (6-8 years), Business Intelligence
    Developer (5 years) and Software Engineer II (3+ years) all read entry.

    Required statements outrank preferred ones - "3+ required, 5+ preferred"
    asks for three - and preferred ones still count when they are the only
    statement. Degree alternatives ("8 years with a bachelor's, or 6 with a
    master's") take the larger, bachelor's path, the posting's general case.
    """
    if not blob:
        return None
    blob = _norm(blob)  # callers normally pass normalised text; cheap if so
    required: List[int] = []
    preferred: List[int] = []
    mode = "required"
    for line in blob.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        # Same heading rules as `_split_sections`, but decided per SENTENCE:
        # a one-line "Required: 3+ years. Preferred: 5+ years." has to split.
        if len(stripped) <= 90:
            if _PREFERRED_HEAD.search(stripped):
                mode = "preferred"
            elif _REQUIRED_HEAD.search(stripped):
                mode = "required"
        for sentence in _SENTENCE_SPLIT.split(stripped):
            is_pref = bool(_PREFERRED_CUE.search(sentence)) or (
                mode == "preferred" and not _REQUIRED_CUE.search(sentence))
            for m in _HEADLINE_YEARS_RE.finditer(sentence):
                n = int(m.group(1))
                tail = m.group(2) or ""
                head = sentence[max(0, m.start() - 40):m.start()]
                if not 0 <= n <= 20 or _YEARS_NOT_EXPERIENCE.search(tail[:40]):
                    continue
                if not (_YEARS_CONTEXT.search(tail[:60]) or "experience" in head):
                    continue
                (preferred if is_pref else required).append(n)
    pool = required or preferred
    return max(pool) if pool else None


def _degree_level(blob: str) -> int:
    """0 = none stated, 1 associate/diploma, 2 bachelor, 3 master, 4 doctorate."""
    for level, rx in _DEGREE_LEVELS:
        if rx.search(blob):
            return level
    return 0


def _industries(blob: str) -> Set[str]:
    """Industries named anywhere in a blob. Used for the JOB side only.

    A posting IS about one employer's line of business, so every word in it is
    evidence about that business. A résumé is not - see `_resume_industries`.
    """
    out: Set[str] = set()
    for name, cues in INDUSTRIES.items():
        for cue in cues:
            if cue in blob:
                out.add(name)
                break
    return out


#: An EMPLOYER or a JOB TITLE naming an industry is direct evidence; a bullet
#: mentioning an industry word is weak evidence and needs corroboration.
#: One word-boundary alternation per industry, longest cue first, so
#: "clinical" and "clinic" are ONE match at ONE position rather than two
#: independent pieces of evidence for the same four letters.
_INDUSTRY_PATTERNS: Dict[str, re.Pattern] = {
    name: re.compile(
        r"(?<![a-z0-9])(?:"
        + "|".join(re.escape(c).replace(r"\ ", r"[\s\-]+")
                   for c in sorted(set(cues), key=len, reverse=True))
        + r")(?:es|s)?(?![a-z0-9])")
    for name, cues in INDUSTRIES.items()
}

_INDUSTRY_EMPLOYER_WEIGHT = 2
_INDUSTRY_BULLET_WEIGHT = 1
_INDUSTRY_MIN_WEIGHT = 2


def _resume_industries(resume: Dict[str, Any]) -> Set[str]:
    """The industries a résumé's EMPLOYMENT evidences. Employers first.

    The 2.1 reading ran `_industries` over the whole résumé - summary, skills,
    education AND project descriptions - and any noun anywhere set an
    industry. Measured on a live account: an MS Data Science new grad whose
    four PORTFOLIO PROJECTS included a clinical decision-support system, a
    counselling-therapy chatbot and a university course project was recorded
    as working in {education, healthcare, nonprofit, legal}. She has never
    worked in any of them, and the `domain` component then scored her against
    every posting on four industries she does not have.

    Industry is a fact about WHERE SOMEONE HAS WORKED, so it is read from the
    experience section and nowhere else:

    * a company name or job title naming an industry counts 2 (an employer
      called "Mercy Hospital" IS healthcare, and that is the case the
      component exists for);
    * an industry word inside a work bullet counts 1, so two independent
      mentions are needed - one incidental "clinical" in a bullet is not a
      career in healthcare;
    * projects, education, coursework, the summary and the skills block are
      NOT read at all. A project is something a person BUILT, often
      deliberately in a domain they have never been paid to work in.

    The failure mode this trades into is `unknown`, which is free:
    `_score_domain` returns `known=False` when either side is empty, so the
    component leaves the average entirely. Being wrong costs 4 of 5 points on
    every posting; knowing nothing costs zero. That is the right trade.
    """
    strong: List[str] = []
    bullets: List[str] = []
    for entry in (resume or {}).get("experience") or []:
        if not isinstance(entry, dict):
            continue
        strong.append(str(entry.get("company") or ""))
        strong.append(str(entry.get("title") or ""))
        for b in (entry.get("bullets") or []):
            bullets.append(str(b))
    strong_blob = _norm("\n".join(strong))
    bullet_blob = _norm("\n".join(bullets))
    if not strong_blob and not bullet_blob:
        return set()
    out: Set[str] = set()
    for name, rx in _INDUSTRY_PATTERNS.items():
        weight = _INDUSTRY_EMPLOYER_WEIGHT if rx.search(strong_blob) else 0
        # DISTINCT MENTIONS, by position. A plain substring scan counts
        # "clinical" twice, because `clinic` is inside it, and two cues that
        # are the same word corroborate nothing - which is exactly how one
        # incidental "clinical data feed" bullet became a healthcare career.
        weight += _INDUSTRY_BULLET_WEIGHT * len(
            {m.start() for m in rx.finditer(bullet_blob)})
        if weight >= _INDUSTRY_MIN_WEIGHT:
            out.add(name)
    return out


def _work_mode(blob: str) -> str:
    if _HYBRID_RE.search(blob):
        return "hybrid"
    if _REMOTE_RE.search(blob):
        return "remote"
    if _ONSITE_RE.search(blob):
        return "onsite"
    return "unknown"


@lru_cache(maxsize=512)
def _extract_requirements_cached(jd_text: str, title: str) -> Dict[str, Any]:
    blob = _norm(jd_text)
    title_blob = _norm(title)
    combined = (title_blob + "\n" + blob).strip()

    required_region, preferred_region = _split_sections(blob)
    pref_skills = find_skills(preferred_region)
    req_skills = [s for s in find_skills(title_blob + "\n" + required_region)
                  if s not in set(pref_skills)]
    source = "sectioned"
    if not req_skills and pref_skills:
        # A posting that only has a "nice to have" block still asks for those.
        req_skills, pref_skills, source = pref_skills, [], "preferred_only"
    if not req_skills:
        req_skills = find_skills(combined)
        source = "whole_text" if req_skills else "none"

    licenses: List[Dict[str, Any]] = []
    for rule in LICENSE_RULES:
        if rule["jd"].search(combined):
            licenses.append({"key": rule["key"], "label": rule["label"]})

    return {
        "content_hash": content_hash(jd_text, title),
        "score_version": FIT_SCORE_VERSION,
        "requirements_source": source,
        "required_skills": req_skills[:30],
        "preferred_skills": pref_skills[:20],
        "required_years": _required_years(combined),
        "required_degree_level": _degree_level(combined),
        # Title first, body as fallback. 3000 chars, not 800: the first 800 are
        # almost always company boilerplate ("About us..."), so the old window
        # recovered a family for only 9% of title-unclassifiable postings
        # against 50% at 3000, measured on live inventory. Deliberately NOT the
        # whole body - benefits and EEO sections mention other professions
        # ("you will partner with our nursing staff") and would misfile the row.
        "role_family": classify_role(title_blob) or classify_role(blob[:3000]),
        # Title first, then the body, then the years the posting itself asks
        # for. Reading the level off "minimum 2 years of experience" is not an
        # inference - it is the posting's own requirement restated in the
        # vocabulary the feed filters on, and it is why `job_level` is now
        # answerable for postings whose title carries no marker (most of them).
        "job_level": level_from_signals(title_blob, description=blob,
                                        required_years=_required_years(combined)),
        "industries": sorted(_industries(combined)),
        "work_mode": _work_mode(combined),
        "licenses": licenses,
        "clearance_required": bool(_CLEARANCE_JD.search(combined)),
        "work_authorization_restricted": bool(_WORKAUTH_JD.search(combined)),
        "text_length": len(blob),
    }


def extract_requirements(jd_text: Any, title: Any = "") -> Dict[str, Any]:
    """Deterministic requirement profile for one posting.

    Cached in-process by (jd_text, title). This is also the exact unit an
    asynchronous, once-per-changed-JD extraction job would persist under
    `content_hash`; the request path never depends on that job existing.
    """
    return dict(_extract_requirements_cached(str(jd_text or ""), str(title or "")))


# Keys `score()` reads off a requirement profile. A stored profile missing any
# of these is treated as unusable rather than silently scored with defaults,
# because a partial profile scores HIGH (absent requirements award the neutral
# 60% credit) and would quietly promote junk to the top of a guest's results.
_REQUIRED_PROFILE_KEYS = (
    "required_skills", "preferred_skills", "required_years",
    "required_degree_level", "role_family", "job_level", "industries",
    "work_mode", "licenses", "clearance_required",
    "work_authorization_restricted",
)


# Skills that almost every posting names and that therefore discriminate
# between candidates barely at all. They are still SCORED (a posting that wants
# stakeholder management and the resume shows it is a real, if weak, signal) —
# they just cannot be the ONLY thing a posting published, because then there is
# no measurable technical requirement to assess a fit against.
#
# Measured: 5,588 of 30,000 active postings (18%) name nothing but these.
_GENERIC_SKILLS = frozenset({
    "communication", "teamwork", "collaboration", "interpersonal",
    "leadership", "problem solving", "critical thinking", "time management",
    "organization", "attention to detail", "multitasking", "adaptability",
    "work ethic", "self motivated", "documentation", "writing",
    "presentation", "public speaking", "training", "mentoring", "onboarding",
    "recruiting", "scheduling", "planning", "budgeting", "negotiation",
    "customer service", "compliance", "reporting", "research", "analysis",
    "data entry", "excel", "microsoft office", "word", "powerpoint",
    "outlook", "google workspace", "project management",
    "stakeholder management", "account management", "lead generation", "crm",
})

# How much measurable evidence a posting must publish. Set from the live
# inventory rather than by feel: the real technical families publish a MEDIAN
# of 8-13 skills (devops 11, software_engineering 9, data_engineering 13,
# security 8, qa 8), while 4,703 postings (15.6%) name two or fewer.
#
# The bar is on DISCRIMINATING skills, not raw count, because both failure
# modes are real:
#   * "Corrosion Reliability Engineer" published ["teamwork", "monitoring"];
#     the resume matched "monitoring" and one-of-two read as 50% requirement
#     coverage -> 15 of 30 points -> a final 75, "good match", ranked above
#     genuine SRE roles. One incidental word in common is not half a job.
#   * "Requirements: Kubernetes and Terraform." is only two skills but both
#     are hard and specific, and that posting is perfectly measurable.
# So: two hard skills is enough, and a longer list still needs at least one.
_MIN_HARD_SKILLS = 2
_MIN_TOTAL_SKILLS = 3


def _hard_skills(names: Sequence[str]) -> List[str]:
    """The named skills that actually discriminate (see `_GENERIC_SKILLS`)."""
    return [s for s in names if str(s).strip().lower() not in _GENERIC_SKILLS]


def has_job_requirements(reqs: Any) -> bool:
    """Did this posting publish anything the scorer can actually measure?

    Public because the retrieval path uses the SAME predicate to decide what a
    guest is allowed to see (ADR-001's rule: fewer true matches beat more
    manufactured ones). Keeping one definition means the scorer's cap and the
    feed's exclusion can never disagree about what "no evidence" means.

    Failing this does NOT hide the posting from the scorer — it routes to the
    `insufficient_job_requirements` hard constraint, which caps the score at 45
    and says so in the explanation ("this posting does not publish readable
    requirements"). The row is still shown, honestly labelled.
    """
    if not isinstance(reqs, dict):
        return False
    if reqs.get("requirements_source") in (None, "", "none"):
        return False
    named = list(reqs.get("required_skills") or []) + \
        list(reqs.get("preferred_skills") or [])
    hard = _hard_skills(named)
    if len(hard) >= _MIN_HARD_SKILLS:
        return True
    return len(named) >= _MIN_TOTAL_SKILLS and len(hard) >= 1


def _precomputed_requirements(stored: Any) -> Optional[Dict[str, Any]]:
    """Validate a requirement profile persisted by the ingestion path.

    Returns the profile only when it is complete AND was produced by a scorer
    version whose EXTRACTION contract matches this one
    (`_COMPATIBLE_REQUIREMENT_VERSIONS`). Returns None otherwise, which makes
    the caller fall back to extracting from the JD body - correct, just slower,
    and on an inventory row (whose body lives in S3) not possible at all.

    This is the backward-compatibility half of the 2.0 -> 2.1 migration: the
    weights changed, the extracted fields did not, so 216,687 stored profiles
    stay valid and no posting becomes unscorable at deploy time.
    """
    if not isinstance(stored, dict) or not stored:
        return None
    if stored.get("score_version") not in _COMPATIBLE_REQUIREMENT_VERSIONS:
        return None
    if any(k not in stored for k in _REQUIRED_PROFILE_KEYS):
        return None
    return stored


# ---------------------------------------------------------------------------
# Resume profile
# ---------------------------------------------------------------------------

def _resume_text_parts(resume: Dict[str, Any]) -> Tuple[str, str]:
    """(experience_text, listed_skills_text).

    Experience text is work bullets, titles, companies, project bullets and
    project tech. Evidence found there is real applied evidence; evidence
    found only in the skills block is a claim. They are weighted differently.
    """
    exp_bits: List[str] = []
    for entry in (resume.get("experience") or []):
        if not isinstance(entry, dict):
            continue
        exp_bits.append(str(entry.get("title") or ""))
        exp_bits.append(str(entry.get("company") or ""))
        for b in (entry.get("bullets") or []):
            exp_bits.append(str(b))
    for proj in (resume.get("projects") or []):
        if not isinstance(proj, dict):
            continue
        exp_bits.append(str(proj.get("name") or ""))
        exp_bits.append(str(proj.get("tech") or ""))
        for b in (proj.get("bullets") or []):
            exp_bits.append(str(b))
    skills_bits: List[str] = []
    skills = resume.get("skills")
    if isinstance(skills, dict):
        for group in skills.values():
            if isinstance(group, (list, tuple)):
                skills_bits.extend(str(s) for s in group)
            elif group:
                skills_bits.append(str(group))
    elif isinstance(skills, (list, tuple)):
        skills_bits.extend(str(s) for s in skills)
    skills_bits.extend(str(c) for c in (resume.get("certifications") or []))
    return "\n".join(exp_bits), "\n".join(skills_bits)


def _resume_months(resume: Dict[str, Any]) -> Optional[int]:
    """Total MONTHS of dated experience, or None when nothing is dated.

    Months, not years, is the honest unit for the early-career résumés this
    engine gets most of. `_resume_years` floors to whole years, so five months
    of internships and zero months of anything both read `0` - the two
    candidates then scored identically against every posting, and a résumé
    WITH dated evidence scored WORSE than one with none (a `None` falls
    through to the seniority-band branch of `_score_experience`, which is
    kinder than a zero-year gap). `has_dated_experience: True` beside
    `years: 0` was the visible symptom of that.

    Internship and co-op months COUNT, at full calendar weight. A dated,
    supervised, paid engagement is professional experience; the thing that
    makes it junior is its LENGTH and the seniority band the title carries,
    and both of those are already modelled. Discounting it a second time here
    would be pricing the same fact twice.
    """
    explicit = resume.get("experience_months")
    if explicit is not None:
        try:
            return max(0, int(explicit))
        except (TypeError, ValueError):
            pass
    explicit_years = resume.get("experience_years")
    if explicit_years is not None:
        try:
            return max(0, int(explicit_years)) * 12
        except (TypeError, ValueError):
            pass
    entries = resume.get("experience") or []
    if not any(str((e or {}).get("dates") or "").strip()
               for e in entries if isinstance(e, dict)):
        return None
    try:
        from services.resume_parser import ResumeParser
        months = ResumeParser._experience_intervals_months(resume)
    except Exception:  # pragma: no cover - parser import is optional here
        return None
    return max(0, int(months or 0))


def _resume_years(resume: Dict[str, Any]) -> Optional[int]:
    """Total WHOLE years from DATED evidence only. No dates means None
    (unknown), never a guess: inventing a duration is a resume fact the
    candidate did not state.

    Still the floor, deliberately: five months is not one year and this number
    is displayed. The sub-year detail lives in `_resume_months`, which is what
    `_score_experience` actually compares.
    """
    months = _resume_months(resume)
    if months is None:
        return None
    return months // 12


def build_resume_profile(resume: Dict[str, Any],
                         target_roles: Sequence[Any] = ()) -> Dict[str, Any]:
    """Everything the scorer needs from one parsed resume, computed once and
    reused across every posting in a batch.

    `target_roles` is the caller's DECLARED targets from onboarding
    (`user_preferences.target_roles`). Nothing in the scoring path read them
    before 2.2 - six canonical titles a user typed into onboarding were
    visible to career_paths, differentiation, trend_signal and career_readiness
    and invisible to the one number the feed ranks on. They are passed to
    `tech_taxonomy.classify_resume`, which fences them: see that function's
    docstring for the precedence (titles > tools > declared targets) and for
    why a declared target can never make somebody INELIGIBLE for anything.

    They also fall back to `resume["target_roles"]`, so a caller holding the
    preferences document can supply them without threading a second argument.
    """
    resume = resume or {}
    if not target_roles:
        target_roles = resume.get("target_roles") or ()
    exp_text, skills_text = _resume_text_parts(resume)
    full = "\n".join([
        str(resume.get("summary") or ""), exp_text, skills_text,
        str(((resume.get("contact") or {}).get("location")) or ""),
        "\n".join(str((e or {}).get("degree") or "") + " " + str((e or {}).get("institution") or "")
                  for e in (resume.get("education") or []) if isinstance(e, dict)),
    ])
    full_norm = _norm(full)
    titles = [str((e or {}).get("title") or "") for e in (resume.get("experience") or [])
              if isinstance(e, dict)]
    families = {f for f in (classify_role(t) for t in titles) if f}
    if not families:
        # Only from the summary, and only if a title yielded nothing. Never a
        # default family.
        fam = classify_role(resume.get("summary") or "")
        if fam:
            families.add(fam)
    listed = set(find_skills(skills_text))
    applied = set(find_skills(exp_text))
    # Computed once: `level` reads it too, so a second `_resume_years` call
    # here could silently disagree with the `years` the profile reports.
    _months = _resume_months(resume)
    _years = None if _months is None else _months // 12
    return {
        "experience_skills": applied,
        "listed_skills": listed,
        "all_text": full_norm,
        "titles": [t for t in titles if t.strip()],
        "families": families,
        # The CANONICAL occupation classification, on the same 14/101 tree the
        # postings are classified with. `families` above is the legacy 32-key
        # reading, kept because `job_postings.role_family` is written from it.
        # Alignment scores against THIS.
        "tech": _resume_taxonomy(titles, sorted(listed | applied),
                                 resume.get("summary") or "", exp_text,
                                 target_roles),
        "years": _years,
        # Months as well as years. `years` is floored and is what the product
        # SHOWS; `experience_months` is what `_score_experience` compares, so
        # eleven months of dated work is no longer indistinguishable from
        # never having worked. See `_resume_months`.
        "experience_months": _months,
        # Years as well as titles: an early-career resume whose only
        # band-bearing titles are internships used to resolve to `intern` no
        # matter how much dated experience sat beside it. See `resume_level`.
        "level": resume_level(titles, _years),
        "degree_level": _degree_level(_norm(" ".join(
            str((e or {}).get("degree") or "") for e in (resume.get("education") or [])
            if isinstance(e, dict)))),
        # Employment evidence only - NOT projects, education or the summary.
        # See `_resume_industries` for the live résumé this was measured on.
        "industries": _resume_industries(resume),
        "location": _norm((resume.get("contact") or {}).get("location") or ""),
        "has_dated_experience": _months is not None,
        "declared_target_roles": [str(t) for t in (target_roles or ()) if str(t or "").strip()],
    }


#: Persisted onto résumé profiles so a stored profile can be read back
#: without re-classifying, and so a taxonomy upgrade is detectable.
RESUME_TAXONOMY_KEYS = ("role_families", "role_specialties", "taxonomy_version",
                        "tech_scope", "non_tech_domain",
                        "classification_confidence")


def _resume_taxonomy(titles: Sequence[Any], skills: Sequence[Any],
                     summary: Any = "", exp_text: Any = "",
                     target_roles: Sequence[Any] = ()) -> Dict[str, Any]:
    """The résumé's own families/specialties on the canonical tree.

    Degrades to an empty, `unknown`-scoped result on any failure: a taxonomy
    problem must cost a candidate their role points, never their whole score.
    """
    try:
        from services import tech_taxonomy as tx
        return tx.classify_resume(titles=titles, skills=skills,
                                  summary=summary, descriptions=[exp_text],
                                  target_roles=target_roles)
    except Exception as exc:  # pragma: no cover - never break scoring
        logger.debug("fit_score._resume_taxonomy: %s", exc)
        return {"role_families": [], "role_specialties": [],
                "taxonomy_version": "error", "tech_scope": "unknown",
                "non_tech_domain": None, "classification_confidence": 0.0}


def job_taxonomy(job: Optional[Dict[str, Any]], reqs: Dict[str, Any],
                 job_title: str = "") -> Dict[str, Any]:
    """The job's families/specialties, preferring the value stored at ingest.

    `job_postings.tech` is written by `job_inventory_service.normalize_posting`
    from the FULL body, which the request path no longer has (it lives in S3).
    Re-deriving from the title alone would throw away the better answer, so the
    stored subdocument wins whenever it is present and current.
    """
    stored = (job or {}).get("tech")
    if isinstance(stored, dict) and stored.get("role_families"):
        return {"role_families": list(stored.get("role_families") or []),
                "role_specialties": list(stored.get("role_specialties") or []),
                "tech_scope": stored.get("tech_scope") or "unknown",
                "taxonomy_version": stored.get("taxonomy_version") or "",
                "source": "stored"}
    try:
        from services import tech_taxonomy as tx
        got = tx.classify(
            job_title or (job or {}).get("title") or "",
            description=str((job or {}).get("description")
                            or (job or {}).get("jd_text") or "")[:8000],
            skills=(reqs.get("required_skills") or []) + (reqs.get("preferred_skills") or []),
            requirements=reqs)
        return {"role_families": list(got.role_families),
                "role_specialties": list(got.role_specialties),
                "tech_scope": got.tech_scope,
                "taxonomy_version": got.taxonomy_version,
                "source": "derived"}
    except Exception as exc:  # pragma: no cover
        logger.debug("fit_score.job_taxonomy: %s", exc)
        return {"role_families": [], "role_specialties": [],
                "tech_scope": "unknown", "taxonomy_version": "error",
                "source": "error"}


def resume_level(titles: Sequence[Any],
                 years: Any = None) -> Optional[str]:
    """The HIGHEST band the resume supports, from titles AND years, or None.

    Not "the first title that states one". That was order-dependent in a way
    that quietly mis-levels people: a resume listing
    ["Cloud/DevOps Engineer", "Associate Software Developer Intern", ...]
    resolves its first title to None and its second to `intern`, so a
    candidate with two years and a Master's was scored as an intern - and
    `_score_experience` then capped their points on the ENTRY roles they were
    actually applying to, because entry is one band above intern.

    Taking the maximum is the honest read: an internship on a resume is
    something a person has ALSO done, never a ceiling on what they are. It is
    also the conservative direction for the over-qualification branch, which is
    the only place a higher band costs a candidate points.

    YEARS ARE THE SECOND SIGNAL, for the same reason. Taking the max over
    titles alone still lands on `intern` whenever the only band-BEARING titles
    are internships - which is the normal shape of an early-career resume,
    because "Software Engineer, Contract" and "Cloud and DevOps Engineer"
    state no band at all and return None. A live account with three years of
    production experience across five such titles resolved to `intern`, and
    `_score_experience` then ranked SDE-Intern reqs (77) above SDE-III (40).

    The years reading is the same ladder `level_from_signals` already applies
    to a POSTING's stated requirement (`_YEARS_TO_LEVEL`), so the two sides of
    the comparison are finally derived the same way. It can only ever RAISE
    the verdict: a title that states `senior` outranks two years of dated
    experience, because the title is the person's own claim and the years may
    simply be undated on the page.
    """
    best: Optional[str] = None
    for title in titles or []:
        level = title_level(title)
        if level is None:
            continue
        if best is None or _LEVEL_ORDER.get(level, -1) > _LEVEL_ORDER.get(best, -1):
            best = level

    from_years = _level_from_years(years)
    if from_years is not None and (
            best is None
            or _LEVEL_ORDER.get(from_years, -1) > _LEVEL_ORDER.get(best, -1)):
        best = from_years
    return best


def _level_from_years(years: Any) -> Optional[str]:
    """The band a count of years supports, on `_YEARS_TO_LEVEL`, or None.

    Shared with `level_from_signals` so a candidate's three years and a
    posting's "3+ years" resolve to the same word. `None`/unparseable/negative
    is unknown, never zero - not stating a number is not the same as stating
    none, the rule this module applies to every other absent signal.
    """
    if years is None:
        return None
    try:
        n = int(years)
    except (TypeError, ValueError):
        return None
    if n < 0:
        return None
    for ceiling, level in _YEARS_TO_LEVEL:
        if n <= ceiling:
            return level
    return "staff"


def _profile_from_skills(resume_skills: Sequence[str],
                         titles: Optional[Sequence[str]] = None,
                         years: Optional[int] = None) -> Dict[str, Any]:
    """Degraded profile for callers that only have a skill list (the feed
    pipelines). Every skill counts as a LISTED claim, never as applied
    experience, so a skills-only caller can never out-score a real resume."""
    canon: Set[str] = set()
    for s in resume_skills or []:
        canon.update(find_skills(str(s)))
        n = str(s or "").strip().lower()
        if n in SKILL_LEXICON:
            canon.add(n)
    title_list = [str(t) for t in (titles or []) if str(t or "").strip()]
    return {
        "experience_skills": set(),
        "listed_skills": canon,
        "all_text": _norm("\n".join(list(resume_skills or []) + title_list)),
        "titles": title_list,
        "families": {f for f in (classify_role(t) for t in title_list) if f},
        # 2.2: this path had NO `tech` key at all, so `_score_role` read an
        # empty taxonomy and every skills-only caller got `role_alignment:
        # unknown` on every posting - the same 25-point blind spot the résumé
        # path had, in the pipeline that feeds the guest and inventory flows.
        "tech": _resume_taxonomy(title_list, sorted(canon)),
        "years": years,
        "experience_months": None if years is None else int(years) * 12,
        "level": resume_level(title_list, years),
        "degree_level": 0,
        "industries": set(),
        "location": "",
        "has_dated_experience": years is not None,
        "declared_target_roles": [],
    }


# ---------------------------------------------------------------------------
# Components
# ---------------------------------------------------------------------------

_LEVEL_WORDS = re.compile(
    r"(?<![a-z])(senior|sr\.?|junior|jr\.?|staff|principal|lead|entry level|entry-level|"
    r"associate|intern|i{1,3}|iv|v)(?![a-z])")
_TITLE_STOP = {"of", "the", "and", "for", "a", "an", "to", "in", "at", "with"}


def _title_tokens(title: str) -> Set[str]:
    t = _LEVEL_WORDS.sub(" ", _norm(title))
    return {w for w in re.findall(r"[a-z][a-z+#.]{2,}", t) if w not in _TITLE_STOP}


def effective_role_family(reqs: Dict[str, Any], job_title: Any) -> Optional[str]:
    """The role family actually used to score, given the posting and its title.

    A STORED `role_family` is not taken at face value, for two reasons:

    1. The TITLE outranks it. `extract_requirements` falls back to classifying
       off the JD BODY when the title yields nothing, and a body is full of
       incidental engineering vocabulary — "Temporary Research Assistant" at
       the University of Chicago is stored as `software_engineering` on that
       basis and was collecting 23 of 25 role-alignment points in a cloud
       engineer's feed. A title that classifies is direct evidence about the
       role; a body that merely mentions software is not. When the title says
       nothing and the only claim is body-derived, the family is treated as
       UNDETERMINED, which is the honest reading.

    2. A profile written before the physical-qualifier veto (see
       EXTRACTOR_VERSION v3) still carries the old family, so the veto is
       re-applied live rather than waiting for every shard to be re-crawled.

    Public because `score` reports it in the `evidence` block: the family named
    in the explanation must be the one the score was actually computed from.
    """
    job_fam = (reqs or {}).get("role_family")
    title_fam = classify_role(job_title)
    if title_fam:
        job_fam = title_fam
    elif job_fam in _SOFTWARE_FAMILIES:
        job_fam = None
    if job_fam in _SOFTWARE_FAMILIES and _PHYSICAL_QUALIFIER_RE.search(_norm(job_title)):
        return "hardware_engineering"
    return job_fam


def _score_role(reqs: Dict[str, Any], prof: Dict[str, Any], job_title: str,
                job_tax: Optional[Dict[str, Any]] = None
                ) -> Tuple[float, str, bool, Dict[str, Any]]:
    """(points, detail, unrelated_flag, alignment)

    Scored on the canonical 14/101 tree via
    `tech_taxonomy.role_alignment`, which distinguishes

        exact_specialty > adjacent_specialty > same_family >
        adjacent_family > unrelated

    where the previous implementation's finest software distinction was the
    single family `software_engineering`. That flatness is the mechanical
    reason a user saw only a handful of jobs above 80: for anyone in software,
    this 25-point component was very nearly a constant, so ranking collapsed
    onto skill overlap and the whole distribution compressed.

    A title-token overlap bonus is retained as a TIE-BREAK inside a tier, not
    as a tier of its own - "Engineer" appearing on both sides is weak evidence
    and it used to be able to outrank a real specialty match.
    """
    mx = COMPONENT_MAX["role_alignment"]
    tax = job_tax or {}
    res_tax = prof.get("tech") or {}

    try:
        from services import tech_taxonomy as tx
        align = tx.role_alignment(
            job_families=tax.get("role_families") or [],
            job_specialties=tax.get("role_specialties") or [],
            resume_families=res_tax.get("role_families") or [],
            resume_specialties=res_tax.get("role_specialties") or [],
            job_scope=tax.get("tech_scope"),
            resume_scope=res_tax.get("tech_scope"),
            resume_non_tech_domain=res_tax.get("non_tech_domain"),
        )
    except Exception as exc:  # pragma: no cover
        logger.debug("fit_score._score_role: %s", exc)
        align = {"tier": "unknown", "weight": 0.6, "detail": "",
                 "cross_domain": False, "matched": None}

    tier = str(align.get("tier") or "unknown")
    weight = float(align.get("weight") or 0.6)

    # Legacy fallback. The canonical tree returns `unknown` when either side
    # carries no taxonomy signal, and "unknown" must never be scored as
    # "unrelated" — but the 32-key legacy classification sometimes DOES know
    # both sides (it reads "Registered Nurse" and "Staff Accountant", which
    # `tech_taxonomy` deliberately declines to give a tech family). Where the
    # legacy reading is confident on both sides and says the two are
    # unrelated, that is real evidence and it is used. Taxonomy leads; legacy
    # only speaks when taxonomy is silent.
    if tier == "unknown":
        legacy = _legacy_role_tier(reqs, prof, job_title)
        if legacy is not None:
            tier, weight, legacy_detail = legacy
            align = dict(align)
            align["tier"] = tier
            align["weight"] = weight
            align["detail"] = legacy_detail
            align["source"] = "legacy_families"
            align["cross_domain"] = tier == "unrelated"

    # Tie-break, capped so it can never promote a tier.
    job_tokens = _title_tokens(job_title)
    res_tokens: Set[str] = set()
    for t in prof.get("titles") or []:
        res_tokens |= _title_tokens(t)
    overlap = job_tokens & res_tokens
    if overlap and tier not in ("unrelated", "exact_specialty"):
        weight = min(weight + 0.04 * min(len(overlap), 2),
                     _TIER_CEILING.get(tier, weight))

    detail = str(align.get("detail") or "")
    if tier == "unknown":
        detail = detail or "Role family could not be determined from either side."
    elif tier == "exact_specialty":
        detail = f"Same specialty as your background - {align.get('matched')}."
    align = dict(align)
    align["overlap_tokens"] = sorted(overlap)[:6]
    align["effective_weight"] = round(weight, 3)
    return mx * weight, detail, tier == "unrelated", align


def _legacy_role_tier(reqs: Dict[str, Any], prof: Dict[str, Any],
                      job_title: str) -> Optional[Tuple[str, float, str]]:
    """`(tier, weight, detail)` from the 32-key legacy families, or None.

    Returns None unless BOTH sides classify — an absent legacy family is the
    same non-answer as an absent taxonomy family, and turning it into
    "unrelated" is exactly the mistake this whole module is correcting.
    """
    job_fam = effective_role_family(reqs, job_title)
    fams = prof.get("families") or set()
    if not job_fam or not fams:
        return None
    if job_fam in fams:
        return ("same_family", 0.70,
                "Your background is in the same role family as this posting.")
    if RELATED_FAMILIES.get(job_fam, set()) & fams:
        return ("adjacent_family", 0.46,
                "Your background is in an adjacent role family.")
    # `unrelated` is the harshest verdict this function can return (0.08), and
    # the 32-key legacy tree is the COARSER of the two classifiers. When the
    # canonical taxonomy declined to classify the resume at all — no families,
    # no specialties, confidence 0.0 — letting the blunter one assert "different
    # field" inverts which classifier we trust.
    #
    # Measured on a live account whose whole resume is "Assistant Engineer" and
    # nineteen skills: 280 of 400 core-tech postings came back `unrelated`, and
    # the account topped out at 77 with nothing above 80. Not knowing must cost
    # what not knowing costs, which is `unknown` (0.60).
    #
    # This does not touch the cross-domain veto. A licensed teacher's resume
    # IS confidently classified — `classify_resume` returns `non_tech` with a
    # named domain — and `role_alignment` answers that in its own branch before
    # this function is ever reached.
    res_tax = prof.get("tech") or {}
    confident = (float(res_tax.get("classification_confidence") or 0.0) >= 0.7
                 and bool(res_tax.get("role_families")
                          or res_tax.get("role_specialties")
                          or res_tax.get("non_tech_domain")))
    if not confident:
        return None
    return ("unrelated", 0.08,
            "This role is in a different field from your background.")


#: A tie-break may lift a tier's weight toward, but never past, the next
#: tier's floor. Without this a two-word title overlap could carry a
#: `same_family` match above an `adjacent_specialty` one.
_TIER_CEILING: Dict[str, float] = {
    "adjacent_specialty": 0.92,
    "same_family": 0.78,
    "adjacent_family": 0.54,
    "unknown": 0.66,
}


# Smallest requirement list a coverage RATIO is computed over. See `_coverage`.
_MIN_COVERAGE_DENOM = 3


def _coverage(reqs_list: Sequence[str], prof: Dict[str, Any]
              ) -> Tuple[float, List[str], List[str]]:
    """(0..1 coverage, covered names, missing names).

    Applied evidence counts 1.0, a bare skills-list claim 0.75. That gap is
    the difference between "I have shipped this" and "I have listed this".

    The DENOMINATOR has a floor of three. Without it a one-item list reads as
    certainty: "Temporary Research Assistant" at the University of Chicago
    published exactly one required skill, "monitoring", the resume happened to
    have it, and one-of-one scored a perfect 30/30 -> a final 86, the single
    highest-ranked row in a cloud engineer's US feed. One word in common is
    not a complete match.

    The floor only touches lists shorter than it, which is the only place the
    ratio is unreliable:

        1 of 1   -> 0.33 instead of 1.00   (the case above)
        2 of 2   -> 0.67 instead of 1.00
        3 of 3   -> 1.00 unchanged
        6 of 12  -> 0.50 unchanged
        12 of 12 -> 1.00 unchanged

    This is the mirror of the floor `jd_match_scorer` documents on the resume
    side of its ratio, applied to the side that actually belongs there: the
    posting's asks.
    """
    if not reqs_list:
        return 0.0, [], []
    exp = prof.get("experience_skills") or set()
    listed = prof.get("listed_skills") or set()
    total = 0.0
    covered: List[str] = []
    missing: List[str] = []
    for skill in reqs_list:
        if skill in exp:
            total += 1.0
            covered.append(skill)
        elif skill in listed:
            total += 0.75
            covered.append(skill)
        else:
            missing.append(skill)
    return total / max(len(reqs_list), _MIN_COVERAGE_DENOM), covered, missing


#: How much of a candidate's elapsed experience counts AS EXPERIENCE FOR THIS
#: ROLE, by role-alignment tier.
#:
#: The defect this fixes
#: ---------------------
#: The experience component measured calendar time and nothing else, so seven
#: years of backend engineering scored FULL MARKS against a senior
#: semiconductor requisition asking for eight. Measured on
#: `evals/eval_fit_score_domains.py` across 115 labelled scenarios: grade-3
#: pairs averaged 19.91 of 20 on this component and grade-0 pairs averaged
#: 16.29 — a separation of 3.6 points out of 20. A component that gives an
#: irrelevant match 81% of its maximum is not measuring fit; it is measuring
#: how long the person has been working, which is the same for every posting
#: they will ever see.
#:
#: Not a second role-alignment penalty. Role alignment answers "is this your
#: discipline"; this answers "does your experience meet what they ask", and
#: the honest answer to the second question depends on the first — seven
#: years of backend is not seven years of ASIC design. The factor is applied
#: ONLY to this component, only downward, and it is deliberately gentle at
#: the near rungs: transferable experience IS experience, and an adjacent
#: specialty keeps 95% of it.
#:
#: `unknown` is 1.00, and that is the load-bearing entry. Not knowing which
#: discipline a posting is in must not cost a candidate their career — the
#: whole `_normalized_score` design is that unknown is EXCLUDED, never priced,
#: and a relevance factor below 1.0 for `unknown` would price it here through
#: the back door on the 43.3% of live postings that state no discipline.
_EXPERIENCE_RELEVANCE: Dict[str, float] = {
    "exact_specialty": 1.00,
    "adjacent_specialty": 0.95,
    "same_family": 0.90,
    "adjacent_family": 0.80,
    "unrelated": 0.55,
    "unknown": 1.00,
}


def _band_only_experience(mx: int, level_gap: Optional[int],
                          job_level: Any, res_level: Any
                          ) -> Optional[Tuple[float, str, bool, bool]]:
    """The seniority-BAND reading of experience, or None when the bands are
    not known on both sides.

    Used twice, which is the point. It is the answer when years are unknown on
    either side, and it is a FLOOR under the years answer when both are known
    - because a résumé that DELETES its dates must never score higher than the
    same résumé with them. Measured: five months of dated internships against
    a "2+ years" posting scored 12.35/20, and the identical résumé with the
    date strings blanked scored 14.40/20, because a `None` falls through to
    this branch and this branch is gentler. Dated evidence was a liability.
    """
    if level_gap is None:
        return None
    # A clear two-plus-band under-level for a senior+ posting is a real signal
    # - the TITLES said it, which is evidence, not absence.
    if level_gap >= 2 and job_level in _SENIOR_PLUS:
        return (mx * 0.30, "Your job titles read below the seniority this role "
                "asks for.", True, True)
    if level_gap <= 0:
        return (mx, f"Your seniority ({res_level}) meets this "
                f"{job_level} role.", False, True)
    if level_gap == 1:
        return (mx * 0.72, f"One seniority band below this {job_level} "
                f"role.", False, True)
    return (mx * 0.40, f"Below the seniority this {job_level} role asks "
            f"for.", False, True)


def _gap_text(gap: float) -> str:
    """A years gap, rounded for a human. `gap` is fractional now (months), and
    "About 2.58 years short" is a precision we have not earned."""
    n = int(round(float(gap)))
    return "a year" if n == 1 else f"{max(1, n)} years"


def _score_experience(reqs: Dict[str, Any], prof: Dict[str, Any],
                      alignment: Optional[Dict[str, Any]] = None
                      ) -> Tuple[float, str, bool, bool]:
    """(points, detail, severe_gap_flag, known).

    Scored primarily off the years gap, then corrected by the resume's title
    SENIORITY band, then scaled by how much of that experience is RELEVANT to
    this role (`_EXPERIENCE_RELEVANCE`). Dated-years alone miss the case of
    someone with several noisy years but only ever junior/associate titles
    applying to a Staff role - the band comparison catches that, and the
    symmetric over-qualification case for entry roles.
    """
    mx = COMPONENT_MAX["experience"]
    # FRACTIONAL years, from months, when we have them. `years` is floored for
    # display, and comparing the floor made eleven months of dated work
    # identical to none at all - and strictly worse than an UNDATED résumé,
    # which falls through to the kinder seniority-band branch below. Anything
    # printed still uses the whole-year `years`; only the arithmetic is finer.
    have = prof.get("years")
    _months = prof.get("experience_months")
    if isinstance(_months, (int, float)) and _months >= 0:
        have = round(float(_months) / 12.0, 2)
    want = reqs.get("required_years")
    job_level = reqs.get("job_level")
    res_level = prof.get("level")

    if want is None and job_level in _LEVEL_YEARS:
        want = _LEVEL_YEARS[job_level]
        stated = False
    else:
        stated = want is not None

    # Ordinal band gap (job minus resume). Positive = under-levelled.
    level_gap: Optional[int] = None
    if job_level in _LEVEL_ORDER and res_level in _LEVEL_ORDER:
        level_gap = _LEVEL_ORDER[job_level] - _LEVEL_ORDER[res_level]

    band = _band_only_experience(mx, level_gap, job_level, res_level)
    if have is None or want is None:
        if band is not None:
            return band
        # Genuinely unknown on both axes. 43.3% of the live inventory states no
        # seniority and 92% state no years, so charging this component's
        # neutral 65% to almost every posting was a systematic 7-point tax on
        # every candidate for a gap that is ours, not theirs.
        return 0.0, "Years of experience are not stated on both sides.", False, False

    gap = want - have
    # What the sentence SAYS stays in whole years (and says "under a year"
    # rather than "0 years", which reads as a denial of the internships that
    # produced it). What the arithmetic above uses is the fractional value.
    _shown = prof.get("years")
    have_text = ("under a year" if (_shown is not None and int(_shown) == 0)
                 else f"about {int(_shown) if _shown is not None else have} years")
    if gap <= 0:
        if job_level in EARLY_CAREER_LEVELS and have >= want + 6:
            pts, detail, severe = mx * 0.7, (
                f"You have {have_text}, well above what this entry level "
                f"role asks for."), False
        else:
            pts, detail, severe = float(mx), (
                f"You have {have_text} against roughly {want} asked for."), False
    elif gap <= 1:
        pts, detail, severe = (
            mx * 0.85, f"You have {have_text} against roughly {want} asked for.", False)
    elif gap <= 2:
        pts, detail, severe = (
            mx * 0.65, f"About {_gap_text(gap)} short of the {want} years asked for.",
            False)
    elif gap <= 4:
        pts, detail, severe = (
            mx * 0.35, f"About {_gap_text(gap)} short of the {want} years asked for.",
            False)
    else:
        severe = stated or job_level in _SENIOR_PLUS
        pts, detail, severe = (
            mx * 0.12, f"About {_gap_text(gap)} short of the {want} years asked for.",
            severe)

    # THE NO-PENALTY-FOR-EVIDENCE FLOOR. Removing information from a résumé
    # must never improve its score. The years reading and the band reading are
    # two different scales, and for an early-career candidate the years one is
    # harsher - so a person who DATED their internships scored below the same
    # person who did not. Floored at what the bands alone would have said,
    # never lifted above it, and the band corrections below still apply on top.
    if band is not None and band[0] > pts:
        pts, detail = band[0], band[1]

    # Seniority-band correction. Only ever dampens - it cannot lift a weak
    # years score, so a padded resume can't buy seniority points.
    if level_gap is not None and level_gap >= 2:
        cap = mx * 0.4
        if pts > cap:
            pts, detail = cap, ("Your job titles read well below the seniority this "
                                "role asks for.")
        if job_level in _SENIOR_PLUS:
            severe = True
    elif (level_gap is not None and level_gap == 1 and pts > mx * 0.75
          and job_level not in EARLY_CAREER_LEVELS):
        # One band under is a stretch worth pricing - EXCEPT into an
        # early-career role, where it is the intended progression rather than a
        # gap. Someone whose only titles are internships is precisely who a
        # new-grad requisition is written for, and `resume_level` reads them as
        # `intern`, one band below `entry`. Docking them here punished the
        # candidate for matching the posting.
        pts = mx * 0.75
    elif (level_gap is not None and level_gap <= -2
          and job_level in EARLY_CAREER_LEVELS and pts > mx * 0.7):
        # Markedly over-levelled for an early-career role - a common
        # auto-reject. `EARLY_CAREER_LEVELS`, not `== "entry"`: `intern` is now
        # its own band, and a director applying to an internship is the same
        # mismatch this branch exists to price.
        pts, detail = mx * 0.7, ("Your seniority is well above what this early "
                                 "career role asks for.")

    # Relevance scaling. Applied last, so every branch above is scaled the
    # same way and none of them has to know about it. Never applied to an
    # unscored component (`known=False` returns above), never upward.
    tier = str((alignment or {}).get("tier") or "unknown")
    factor = _EXPERIENCE_RELEVANCE.get(tier, 1.0)
    if factor < 1.0 and pts > 0:
        pts *= factor
        detail = (f"{detail} Counted against a {tier.replace('_', ' ')} match to "
                  f"your background.")
    return pts, detail, severe, True


def _score_domain(reqs: Dict[str, Any], prof: Dict[str, Any]
                  ) -> Tuple[float, str, bool]:
    """(points, detail, known). `known=False` drops the component from BOTH
    sides of the average - see `_normalized_score`."""
    mx = COMPONENT_MAX["domain"]
    job_inds = set(reqs.get("industries") or [])
    res_inds = prof.get("industries") or set()
    if not job_inds or not res_inds:
        return 0.0, "Industry could not be determined on both sides.", False
    shared = job_inds & res_inds
    if shared:
        return float(mx), f"Shared industry context: {', '.join(sorted(shared))}.", True
    return mx * 0.2, "Different industry from your background.", True


def _score_location(reqs: Dict[str, Any], prof: Dict[str, Any], job_location: str
                    ) -> Tuple[float, str, bool, bool]:
    """(points, detail, location_incompatible, known).

    `work_mode` is UNKNOWN on 96.1% of the live inventory. Charging that
    inventory half the location component was not measuring location fit, it
    was measuring our own parsing gap - and it did so on almost every posting
    at once, which is a large part of why nothing reached the top band.
    An unknown work mode is now `known=False`: excluded, not penalised.
    """
    mx = COMPONENT_MAX["location"]
    mode = reqs.get("work_mode")
    if mode == "remote":
        return float(mx), "This role is remote.", False, True
    res_loc = prof.get("location") or ""
    job_loc = _norm(job_location)
    if not res_loc or not job_loc:
        return 0.0, "Location compatibility is unknown.", False, False
    res_states = set(_STATE_RE.findall(res_loc))
    job_states = set(_STATE_RE.findall(job_loc))
    res_city = res_loc.split(",")[0].strip()
    job_city = job_loc.split(",")[0].strip()
    if res_city and res_city == job_city:
        return float(mx), "Same city as your listed location.", False, True
    if res_states & job_states:
        return mx * 0.8, "Same state as your listed location.", False, True
    if mode == "hybrid":
        return (mx * 0.4, "Hybrid role in a different area from your listed location.",
                False, True)
    if mode == "onsite":
        return (mx * 0.2, "Onsite role in a different area from your listed location.",
                True, True)
    # Work mode unknown and the areas differ. We do not know whether that
    # matters, so we do not score it either way.
    return 0.0, "This posting does not state whether the role is remote.", False, False


def _score_education(reqs: Dict[str, Any], prof: Dict[str, Any],
                     license_ok: Optional[bool]) -> Tuple[float, str, bool]:
    """(points, detail, known).

    A posting that names no degree used to score 4/5, never 5/5, because
    `license_ok` is `None` whenever the posting names no licence either -
    which is almost every software posting. Every candidate silently lost a
    point for a requirement nobody stated. Naming no requirement is not a
    requirement the candidate failed.
    """
    mx = COMPONENT_MAX["education"]
    want = int(reqs.get("required_degree_level") or 0)
    have = int(prof.get("degree_level") or 0)
    if license_ok is False:
        return 0.0, "A required credential is not shown on your resume.", True
    if want == 0:
        return float(mx), "No specific degree is required.", True
    if have == 0:
        return 0.0, "Your education level could not be read from the resume.", False
    if have >= want:
        return float(mx), "Your education meets the stated requirement.", True
    if want - have == 1:
        return mx * 0.5, "Your education is one level below the stated requirement.", True
    return mx * 0.2, "Your education is below the stated requirement.", True


# ---------------------------------------------------------------------------
# Bands, confidence, copy
# ---------------------------------------------------------------------------

#: An ineligible candidate can still be shown - people apply to roles they do
#: not formally qualify for, and hiding the posting would be a worse product -
#: but they must never sit in a band that implies they clear screening.
_INELIGIBLE_CEILING = 34   # top of "unlikely match"


def _normalized_score(components: Dict[str, Dict[str, Any]]) -> Tuple[int, float]:
    """Average the components we could actually measure. `(score, breadth)`.

    This is the change that fixes the compressed distribution. Before 2.1
    every component paid out a NEUTRAL fraction when it had no evidence -
    experience 65%, domain 60%, location 60%, education 80%, absent skills
    60% - so a perfect, fully-evidenced match summed to about 86 and a
    typical good one to the low 70s. Roughly twelve points were being deducted
    from every candidate for facts the POSTING never stated: 96.1% of live
    postings do not state work mode, 43.3% state no seniority, 81.5% no
    salary.

    A neutral fraction is still a deduction. The statistically correct
    treatment of a missing feature is to drop it from both the numerator and
    the denominator, which is what this does, and which is what "unknown
    metadata is never converted into a negative signal" actually requires.

    `breadth` is the share of the 100-point frame that had evidence, returned
    so the caller can say how complete the picture was instead of quietly
    baking that into the number.
    """
    earned = sum(c["score"] for c in components.values() if c.get("known"))
    possible = sum(c["max"] for c in components.values() if c.get("known"))
    breadth = round(possible / 100.0, 3)
    if possible <= 0:
        return 0, 0.0
    return int(round(max(0.0, min(100.0, 100.0 * earned / possible)))), breadth


def _eligibility(constraints: Sequence[Dict[str, Any]],
                 alignment: Dict[str, Any],
                 prof: Dict[str, Any],
                 reqs: Dict[str, Any]) -> Tuple[str, List[str]]:
    """`(eligibility, reasons)` — a KNOWN-mismatch gate, never a guess.

    Three things make somebody ineligible, and all three are positive
    findings rather than absences:

    * a confident cross-domain verdict — the résumé is a classroom-teaching
      career and the posting is a backend role. Not "we could not classify
      this résumé", which stays eligible;
    * a named licence the résumé does not evidence anywhere;
    * the résumé stating it needs sponsorship against a posting stating it
      offers none.

    Silence is never a mismatch. A posting requiring a clearance against a
    résumé that simply does not mention one yields `unknown`, which surfaces
    as a requirement to read and costs nothing.
    """
    reasons: List[str] = []
    if not (prof.get("experience_skills") or prof.get("listed_skills")
            or prof.get("titles")):
        return NOT_SCORED, ["No readable evidence on the resume."]

    if alignment.get("cross_domain") and alignment.get("tier") == "unrelated":
        res_tax = prof.get("tech") or {}
        if float(res_tax.get("classification_confidence") or 0) >= 0.7:
            reasons.append(str(alignment.get("detail")
                               or "This role is in a different field."))

    for c in constraints:
        if c.get("status") != "missing":
            continue
        if c.get("type") in ("license", "work_authorization"):
            reasons.append(str(c.get("detail") or c.get("type")))
    return (INELIGIBLE if reasons else ELIGIBLE), reasons[:4]


def band_for(score: int) -> str:
    for floor, name in BANDS:
        if score >= floor:
            return name
    return BANDS[-1][1]


def _confidence(reqs: Dict[str, Any], prof: Dict[str, Any]) -> str:
    signals = 0
    if reqs.get("text_length", 0) >= 400:
        signals += 1
    if len(reqs.get("required_skills") or []) >= 3:
        signals += 1
    if reqs.get("requirements_source") == "sectioned":
        signals += 1
    if prof.get("has_dated_experience"):
        signals += 1
    if reqs.get("role_family") and (prof.get("families") or set()):
        signals += 1
    if signals >= 4:
        return "high"
    if signals >= 2:
        return "medium"
    return "low"


def _humanize(names: Sequence[str], limit: int = 3) -> str:
    items = [str(n) for n in names[:limit] if n]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return ", ".join(items[:-1]) + f" and {items[-1]}"


def _build_explanation(score: int, band: str, reqs: Dict[str, Any],
                       covered: Sequence[str], missing: Sequence[str],
                       role_detail: str, exp_detail: str,
                       constraints: Sequence[Dict[str, Any]]) -> str:
    """One deterministic sentence group. Sentence case, no dashes, no curly
    quotes, no accented characters (platform copy rule)."""
    parts: List[str] = [band.capitalize() + f" at {score} out of 100."]
    required = reqs.get("required_skills") or []
    if required:
        parts.append(
            f"You show {len(covered)} of the {len(required)} requirements this "
            f"posting names" + (f", including {_humanize(covered)}." if covered else "."))
    else:
        parts.append("This posting does not list specific requirements clearly, "
                     "so skill coverage is treated as unknown.")
    parts.append(role_detail)
    parts.append(exp_detail)
    if missing:
        parts.append(f"Not shown on your resume: {_humanize(missing, 4)}.")
    for c in constraints:
        if c.get("status") == "missing":
            parts.append(c.get("detail") or "")
    return " ".join(p for p in parts if p).strip()


# ---------------------------------------------------------------------------
# Hard constraints
# ---------------------------------------------------------------------------

def _hard_constraints(reqs: Dict[str, Any], prof: Dict[str, Any],
                      severe_gap: bool, location_bad: bool, unrelated: bool
                      ) -> Tuple[List[Dict[str, Any]], Optional[bool]]:
    """(constraints, license_ok).

    license_ok is True when every named credential is evidenced, False when
    one is definitely absent, None when the resume plausibly holds it but does
    not say so. None never caps: unknown is unknown, not a rejection.
    """
    out: List[Dict[str, Any]] = []
    text = prof.get("all_text") or ""
    titles_text = _norm(" | ".join(prof.get("titles") or []))

    license_ok: Optional[bool] = None
    named = reqs.get("licenses") or []
    if named:
        results: List[Optional[bool]] = []
        for entry in named:
            rule = next((r for r in LICENSE_RULES if r["key"] == entry["key"]), None)
            if rule is None:
                continue
            if rule["resume"].search(text):
                results.append(True)
                out.append({"type": "license", "key": entry["key"], "status": "met", "hard_requirement": True,
                            "detail": f"{entry['label']} is evidenced on your resume."})
            elif (rule.get("role_evidence") is not None
                  and rule["role_evidence"].search(titles_text)):
                results.append(None)
                out.append({"type": "license", "key": entry["key"], "status": "unknown", "hard_requirement": True,
                            "detail": (f"This posting requires a {entry['label'].lower()}. "
                                       f"Your job titles suggest you hold one, but the "
                                       f"resume does not state it.")})
            else:
                results.append(False)
                out.append({"type": "license", "key": entry["key"], "status": "missing", "hard_requirement": True,
                            "cap": CONSTRAINT_CAPS["license_missing"],
                            "detail": (f"This posting requires a {entry['label'].lower()}, "
                                       f"which is not shown on your resume.")})
        if False in results:
            license_ok = False
        elif results and all(r is True for r in results):
            license_ok = True

    if reqs.get("clearance_required"):
        if _CLEARANCE_RESUME.search(text):
            out.append({"type": "clearance", "status": "met",
                        "hard_requirement": True,
                        "detail": "A security clearance is evidenced on your resume."})
        else:
            # UNKNOWN, not missing. Almost nobody writes "I hold a Secret
            # clearance" on a résumé, and the previous branch read that silence
            # as a definite absence and capped the score at 40 - so a cleared
            # candidate was scored as if they were not, on the strength of a
            # phrase they had no reason to include. There is nothing on a
            # résumé that can CONTRADICT a clearance requirement, so this is
            # surfaced as a requirement to check, not priced as a gap.
            out.append({"type": "clearance", "status": "unknown",
                        "hard_requirement": True,
                        "detail": ("This posting requires an active security "
                                   "clearance. Your resume does not state whether "
                                   "you hold one.")})

    if reqs.get("work_authorization_restricted"):
        if _WORKAUTH_RESUME_NEEDS.search(text):
            out.append({"type": "work_authorization", "status": "missing",
                        "hard_requirement": True,
                        "cap": CONSTRAINT_CAPS["work_authorization_conflict"],
                        "detail": ("This employer states it does not sponsor, and your "
                                   "resume indicates sponsorship would be needed.")})
        else:
            out.append({"type": "work_authorization", "status": "unknown",
                        "hard_requirement": True,
                        "detail": ("This employer states it does not sponsor. Your work "
                                   "authorization is not stated on the resume.")})

    if not ((prof.get("experience_skills") or set()) | (prof.get("listed_skills") or set())):
        out.append({"type": "resume_evidence", "status": "missing",
                    "cap": CONSTRAINT_CAPS["insufficient_resume_evidence"],
                    "detail": ("No skills could be read from your resume, so coverage "
                               "of this posting's requirements cannot be assessed.")})

    # Job-side evidence gate. `requirements_source == "none"` means the
    # extractor found no skill, no year count and no credential anywhere in the
    # body, which in practice means there was no usable body: a listing row
    # whose detail fetch was skipped, a board that ships an empty description,
    # or a posting that is pure marketing copy. Everything below the title is
    # then guesswork, and guesswork must not be able to present as a match.
    if not has_job_requirements(reqs):
        out.append({"type": "job_evidence", "status": "missing",
                    "cap": CONSTRAINT_CAPS["insufficient_job_requirements"],
                    "detail": ("This posting does not publish readable requirements, "
                               "so the fit shown is based on the job title alone.")})
    if severe_gap:
        out.append({"type": "experience_gap", "status": "missing",
                    "cap": CONSTRAINT_CAPS["severe_experience_gap"],
                    "detail": "The experience this posting asks for is well above yours."})
    if location_bad:
        out.append({"type": "location", "status": "missing",
                    "cap": CONSTRAINT_CAPS["location_incompatible"],
                    "detail": "This is an onsite role in a different area from your location."})
    if unrelated:
        out.append({"type": "role", "status": "missing",
                    "cap": CONSTRAINT_CAPS["unrelated_role"],
                    "detail": "This role is in a different field from your background."})
    return out, license_ok


# ---------------------------------------------------------------------------
# Public scoring API
# ---------------------------------------------------------------------------

#: Fields copied out of a `sponsorship_service` signal. An allowlist, so a
#: future field on that document cannot silently start appearing on a score,
#: and so the three that carry the MANDATED FRAMING (`source`, `disclaimer`,
#: `data_vintage`) can never be dropped by accident.
_SPONSORSHIP_PASSTHROUGH = (
    "employer_display", "band", "headline", "summary",
    "approvals_5y", "denials_5y", "approval_rate", "lca_filings_5y",
    "top_titles", "median_wage", "data_vintage", "data_window_years",
    # PROVENANCE. `sponsorship_service.window_of` reads the window off the
    # ROW rather than from a module constant, so these describe the evidence
    # that actually exists. `data_window` is the raw subdocument and is
    # cross-checked in `_employer_sponsorship`.
    "data_window", "data_window_label", "fiscal_years", "data_imported_at",
    "evidence", "naics", "location",
    "source", "disclaimer",
)


def _employer_sponsorship(job: Dict[str, Any], reqs: Dict[str, Any]
                          ) -> Optional[Dict[str, Any]]:
    """The employer's PUBLIC H-1B disclosure history, or None.

    Read from `job["sponsorship_signal"]` - an ALREADY-FETCHED
    `sponsorship_service.get_signal` / `bulk_signals` result. This module does
    no I/O on the scoring path and will not start: a list view fetches the
    whole page's signals in one query and hands them in, exactly as
    `sponsorship_service.attach_signal` is designed for.

    Four rules, all load-bearing:

    1. **It is NOT part of `score`.** `match_score` is one formula (ADR-002)
       and this is not an input to it. It is returned beside `eligibility`,
       which is where a separate question belongs.
    2. **It is NOT a gate.** It can never make anyone ineligible, cap
       anything, or add a constraint. An employer's filing history is a fact
       about the employer's past, not a prediction about this requisition or
       about this person - which is exactly what `sponsorship_service`'s
       docstring mandates, and the `source` + `disclaimer` it ships with are
       passed through so the framing cannot be lost downstream.
    3. **It is NOT a rebuttal.** When the POSTING says it offers no
       sponsorship, that statement stands. `posting_states_no_sponsorship` is
       reported alongside so a reader sees both facts and neither is presented
       as answering the other.
    4. **No signal means None**, never a zero and never an inferred "this
       employer probably does not sponsor". `sponsorship_signals` is keyed on
       a normalized employer name and simply does not cover most employers;
       absence of a row is absence of data.
    """
    raw = (job or {}).get("sponsorship_signal")
    if not isinstance(raw, dict) or not raw:
        return None
    out = {k: raw[k] for k in _SPONSORSHIP_PASSTHROUGH if k in raw}
    if not out:
        return None
    # Renamed on the way out. `sponsorship_service` calls its 0-100 strength
    # `score`, and this dict sits inside a payload whose `score` is the match
    # score; two different numbers under one name in one response is how a UI
    # comes to print the wrong one.
    if "score" in raw:
        out["signal_score"] = raw["score"]
    out["posting_states_no_sponsorship"] = bool(
        (reqs or {}).get("work_authorization_restricted"))
    out["affects_match_score"] = False

    # THE WINDOW MUST NOT BE OVERSTATED. The ingested USCIS rows cover ONE
    # fiscal year (`data_window: {fiscal_years: [2026], years: 1,
    # service_claim_years: 5}`) while the service's name for the counts is
    # still `approvals_5y`. `sponsorship_service.window_of` now derives
    # `data_window_years` from the row, so the two normally agree — this is
    # the check that they do. If a row ever arrives whose measured window is
    # SHORTER than the figure published beside it, the measured one is stated
    # and the discrepancy is named, rather than being resolved silently in the
    # larger direction.
    window = raw.get("data_window")
    if isinstance(window, dict):
        measured = window.get("years")
        if isinstance(measured, (int, float)) and measured > 0:
            out["data_window_years_measured"] = int(measured)
            if int(measured) != int(out.get("data_window_years") or 0):
                out["data_window_claim_unverified"] = True
                out["window_note"] = (
                    f"The accompanying copy describes a "
                    f"{out.get('data_window_years')}-fiscal-year window; the "
                    f"loaded disclosure data covers "
                    f"{int(measured)}. Read the counts as the shorter window.")
    return out


def score(resume: Dict[str, Any], job: Dict[str, Any],
          profile: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Score one parsed resume against one job row.

    `job` reads `description` / `jd_text`, `title`, `location`. `profile` is
    an optional prebuilt `build_resume_profile` result so a batch pays the
    resume analysis once.

    `job["requirements"]` short-circuits extraction. The canonical inventory
    (ADR-003) runs `extract_requirements` ONCE per changed JD at ingest time and
    stores the result on the posting, so the request path can score without ever
    loading the JD body. The stored profile is only trusted when it declares the
    CURRENT `FIT_SCORE_VERSION`; anything older falls through to a live extract,
    which keeps a scorer upgrade from silently serving stale semantics.
    """
    job = job or {}
    jd_text = str(job.get("description") or job.get("jd_text") or "")
    job_title = str(job.get("title") or "")
    job_location = str(job.get("location") or "")

    prof = profile if profile is not None else build_resume_profile(resume or {})
    reqs = _precomputed_requirements(job.get("requirements"))
    if reqs is None:
        reqs = extract_requirements(jd_text, job_title)

    required = reqs.get("required_skills") or []
    preferred = reqs.get("preferred_skills") or []

    job_tax = job_taxonomy(job, reqs, job_title)
    role_pts, role_detail, unrelated, alignment = _score_role(
        reqs, prof, job_title, job_tax)
    req_cov, req_covered, req_missing = _coverage(required, prof)
    req_pts = (req_cov * COMPONENT_MAX["required_skills"]) if required else 0.0
    pref_cov, pref_covered, pref_missing = _coverage(preferred, prof)
    pref_pts = (pref_cov * COMPONENT_MAX["preferred_skills"]) if preferred else 0.0
    exp_pts, exp_detail, severe_gap, exp_known = _score_experience(
        reqs, prof, alignment)
    dom_pts, dom_detail, dom_known = _score_domain(reqs, prof)
    loc_pts, loc_detail, location_bad, loc_known = _score_location(
        reqs, prof, job_location)

    unrelated = unrelated and req_cov < 0.35
    constraints, license_ok = _hard_constraints(
        reqs, prof, severe_gap, location_bad, unrelated)
    edu_pts, edu_detail, edu_known = _score_education(reqs, prof, license_ok)

    components = {
        "role_alignment": {"score": round(role_pts, 2),
                           "max": COMPONENT_MAX["role_alignment"],
                           "known": alignment.get("tier") != "unknown",
                           "detail": role_detail},
        "required_skills": {"score": round(req_pts, 2),
                            "max": COMPONENT_MAX["required_skills"],
                            "known": bool(required),
                            "detail": (f"{len(req_covered)} of {len(required)} required "
                                       f"items evidenced.") if required
                            else "This posting names no clear required skills."},
        "experience": {"score": round(exp_pts, 2),
                       "max": COMPONENT_MAX["experience"], "known": exp_known,
                       "detail": exp_detail},
        "preferred_skills": {"score": round(pref_pts, 2),
                             "max": COMPONENT_MAX["preferred_skills"],
                             "known": bool(preferred),
                             "detail": (f"{len(pref_covered)} of {len(preferred)} preferred "
                                        f"items evidenced.") if preferred
                             else "This posting names no separate preferred skills."},
        "domain": {"score": round(dom_pts, 2), "known": dom_known,
                   "max": COMPONENT_MAX["domain"], "detail": dom_detail},
        "location": {"score": round(loc_pts, 2), "known": loc_known,
                     "max": COMPONENT_MAX["location"], "detail": loc_detail},
        "education": {"score": round(edu_pts, 2), "known": edu_known,
                      "max": COMPONENT_MAX["education"], "detail": edu_detail},
    }

    final, evidence_breadth = _normalized_score(components)

    applied_caps: List[str] = []
    for c in constraints:
        cap = c.get("cap")
        if cap is not None and final > cap:
            final = int(cap)
            applied_caps.append(str(c.get("type")))

    eligibility, ineligible_reasons = _eligibility(
        constraints, alignment, prof, reqs)
    if evidence_breadth <= 0.0 and eligibility == ELIGIBLE:
        # Nothing on either side could be measured, so `final` is 0 by
        # arithmetic rather than by judgement. A 0 renders as "unlikely
        # match", which is a claim; "not scored" is the true one. Same rule
        # as no-parsed-resume: never print a number we did not measure.
        eligibility = NOT_SCORED
        ineligible_reasons = ["Nothing on this posting could be compared "
                              "against your resume."]
    if eligibility == INELIGIBLE and final > _INELIGIBLE_CEILING:
        final = _INELIGIBLE_CEILING

    band = band_for(final)
    # A posting we could not read requirements from cannot yield a confident
    # number no matter how much the resume side lit up - the comparison had
    # nothing real on the job side. Floor the confidence so the UI never
    # presents a title-only guess as anything better than low confidence.
    confidence = _confidence(reqs, prof) if has_job_requirements(reqs) else "low"
    covered_all = req_covered + [p for p in pref_covered if p not in req_covered]
    missing_all = req_missing + [p for p in pref_missing if p not in req_missing]

    strengths: List[str] = []
    if req_covered:
        strengths.append(f"Covers {_humanize(req_covered, 3)} from the required list.")
    if components["role_alignment"]["score"] >= COMPONENT_MAX["role_alignment"] * 0.85:
        strengths.append("Role family matches your background.")
    if components["experience"]["score"] >= COMPONENT_MAX["experience"] * 0.85:
        strengths.append("Experience level lines up with the posting.")
    if pref_covered:
        strengths.append(f"Also covers preferred items: {_humanize(pref_covered, 3)}.")

    missing_requirements = list(req_missing[:8])
    for c in constraints:
        if c.get("status") == "missing" and c.get("type") != "role":
            missing_requirements.append(str(c.get("detail") or ""))

    explanation = _build_explanation(
        final, band, reqs, req_covered, req_missing, role_detail, exp_detail, constraints)

    return {
        # --- v2 contract --------------------------------------------------
        "score": final,
        "band": band,
        "confidence": confidence,
        "score_version": FIT_SCORE_VERSION,
        # NOT a second score. `score` answers "how well does this fit"; this
        # answers "may this person be considered at all", which a number
        # cannot express - see the ELIGIBLE/INELIGIBLE/NOT_SCORED note above.
        "eligibility": eligibility,
        "ineligible_reasons": ineligible_reasons,
        # Employer H-1B disclosure history, passed through UNCHANGED from
        # `sponsorship_service`. Beside `eligibility`, never inside `score`.
        # See `_employer_sponsorship`.
        "employer_sponsorship": _employer_sponsorship(job, reqs),
        # How much of the 100-point frame had evidence on both sides. The
        # score is an average over exactly these components, so a reader can
        # tell "80 out of a complete picture" from "80 out of half of one"
        # without either being penalised for the other's gaps.
        "evidence_breadth": evidence_breadth,
        "role_alignment": alignment,
        "job_taxonomy": job_tax,
        "resume_taxonomy": {k: (prof.get("tech") or {}).get(k)
                            for k in RESUME_TAXONOMY_KEYS},
        "components": components,
        "component_scores": {k: v["score"] for k, v in components.items()},
        "evidence": {
            "required_skills": required,
            "preferred_skills": preferred,
            "covered_required": req_covered,
            "covered_preferred": pref_covered,
            "required_coverage": round(req_cov, 3),
            "preferred_coverage": round(pref_cov, 3),
            "requirements_source": reqs.get("requirements_source"),
            # The family the score was computed from, which is not always the
            # one stored on the posting — see `effective_role_family`.
            "job_role_family": effective_role_family(reqs, job_title),
            "resume_role_families": sorted(prof.get("families") or set()),
            "resume_years": prof.get("years"),
            "required_years": reqs.get("required_years"),
            "job_level": reqs.get("job_level"),
            "work_mode": reqs.get("work_mode"),
            "content_hash": reqs.get("content_hash"),
        },
        "strengths": strengths,
        "missing_requirements": missing_requirements,
        "hard_constraints": constraints,
        "applied_caps": applied_caps,
        "explanation": explanation,
        # --- legacy field names kept so existing card builders keep working -
        "match_score": final,
        "matched_skills": covered_all[:12],
        "missing_top": missing_all[:8],
        "explain": f"{len(req_covered)}/{len(required)} required" if required
        else "no-jd-requirements",
    }


def score_one(
    jd_text: str = "",
    title: str = "",
    resume_skills: Optional[Sequence[str]] = None,
    *,
    resume: Optional[Dict[str, Any]] = None,
    job: Optional[Dict[str, Any]] = None,
    profile: Optional[Dict[str, Any]] = None,
    resume_titles: Optional[Sequence[str]] = None,
    resume_years: Optional[int] = None,
    location: str = "",
) -> Dict[str, Any]:
    """Drop-in replacement for `jd_match_scorer.score_one` that also accepts
    the full parsed resume.

    Callers that pass only `resume_skills` degrade gracefully: those skills
    are treated as listed claims, never as applied experience.
    """
    job_row = dict(job or {})
    job_row.setdefault("description", jd_text)
    job_row.setdefault("title", title)
    if location:
        job_row.setdefault("location", location)

    if resume is not None:
        return score(resume, job_row, profile=profile)
    if profile is not None:
        return score({}, job_row, profile=profile)
    prof = _profile_from_skills(resume_skills or [], resume_titles, resume_years)
    return score({}, job_row, profile=prof)


def score_batch(resume: Dict[str, Any], items: Iterable[Dict[str, Any]],
                target_roles: Sequence[Any] = ()
                ) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Any]]:
    """Score many postings against one resume. The resume profile is built
    once; requirement extraction is memoised per JD."""
    prof = build_resume_profile(resume or {}, target_roles)
    out: Dict[str, Dict[str, Any]] = {}
    for it in items or []:
        jid = str(it.get("id") or it.get("job_id") or it.get("url") or "")
        if not jid:
            continue
        out[jid] = score(resume or {}, it, profile=prof)
    meta = _batch_meta(prof)
    return out, meta


def load_structured_for_user(user_email: str) -> Dict[str, Any]:
    """The caller's ACTIVE parsed resume, or `{}`.

    Resolves users.active_resume_id -> resume_assets via resume_access (v3).
    `{}` means "there is no parsed resume", which every caller MUST treat as
    "do not produce a score" - not as "score against an empty resume", which
    awards the neutral 60% credit in four components and lands around 60-72.
    """
    try:
        from services import resume_access
        record = resume_access.get_active_structured(user_email)
        return (record or {}).get("structured") or {}
    except Exception as e:
        logger.warning("fit_score: could not load active resume for %s: %s",
                       user_email, type(e).__name__)
        return {}


def load_target_roles(user_email: str) -> List[str]:
    """The caller's DECLARED target roles from onboarding, or `[]`.

    `user_preferences.target_roles` is written by `POST /api/auth/preferences`
    and, until 2.2, NOTHING in the scoring path read it - only career_paths,
    differentiation, trend_signal and career_readiness did. A user who told us
    in onboarding that they are applying for "Data Scientist / Machine
    Learning Engineer / AI Engineer" was ranked as though they had said
    nothing.

    Never raises and never blocks a score: a missing collection, a missing
    document or a malformed value all return `[]`, which
    `classify_resume` reads as "no declared targets".

    The filter is an equality on a string the caller was authenticated as, so
    it cannot reach an operator position; the value is str()-cast anyway.
    """
    email = str(user_email or "").strip()
    if not email:
        return []
    try:
        from utils.db_connect import DBConnect
        doc = DBConnect().get_db()["user_preferences"].find_one(
            {"user_email": email}, {"target_roles": 1, "_id": 0}) or {}
    except Exception as exc:
        logger.debug("fit_score.load_target_roles(%s): %s", email,
                     type(exc).__name__)
        return []
    roles = doc.get("target_roles")
    if not isinstance(roles, (list, tuple)):
        return []
    return [str(r).strip() for r in roles if str(r or "").strip()][:12]


def load_profile_for_user(user_email: str) -> Optional[Dict[str, Any]]:
    """`build_resume_profile` for the caller's active resume, or None.

    ONE definition of "this account cannot be scored yet", shared by every
    display path (`/api/jobs/search`, the inventory feed, the pipeline
    match-score endpoint). None is the honest answer and must surface as "not
    scored" in the UI; it must NEVER be replaced by a number.
    """
    if not user_email:
        return None
    structured = load_structured_for_user(user_email)
    if not structured:
        return None
    return build_resume_profile(structured, load_target_roles(user_email))


def unscored_meta() -> Dict[str, Any]:
    """The batch meta for an account with no parsed resume."""
    return {"score_version": FIT_SCORE_VERSION, "resume_skill_count": 0,
            "has_resume": False, "resume_years": None,
            "resume_role_families": []}


def score_batch_for_user(user_email: str, items: Iterable[Dict[str, Any]]
                         ) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Any]]:
    """`score_batch` against the caller's ACTIVE parsed resume.

    Resolves users.active_resume_id -> resume_assets via resume_access (v3),
    exactly as the legacy jd_match_scorer did. A caller with no parsed resume
    gets empty scores and `has_resume: False` rather than a fabricated one.
    """
    structured = load_structured_for_user(user_email)
    if not structured:
        return {}, unscored_meta()
    return score_batch(structured, items,
                       target_roles=load_target_roles(user_email))


def _batch_meta(prof: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "score_version": FIT_SCORE_VERSION,
        "resume_skill_count": len(prof["experience_skills"] | prof["listed_skills"]),
        "has_resume": bool(prof["experience_skills"] or prof["listed_skills"]
                           or prof["titles"]),
        "resume_years": prof["years"],
        "resume_role_families": sorted(prof["families"]),
    }


# ---------------------------------------------------------------------------
# Multi-resume scoring (resume-set aware)
# ---------------------------------------------------------------------------
# `load_profile_for_user` scores against users.active_resume_id ALONE. That is
# right for a one-resume account and wrong for every other one: a user who
# keeps a Backend, an AI-Platform and a Cloud-Platform resume is scored on
# whichever file happens to be active, so a backend posting is matched against
# the cloud resume and loses required-skill coverage it actually has. Measured
# on live data (2026-09-01, a 6-resume account): role_alignment sat at 24.7/25
# and experience at 17.0/20 while required_skills returned 14.1/30 and
# preferred_skills 5.1/10 — i.e. the entire deficit was skill coverage the
# other resumes carried.
#
# `base_resume_selector` already ranks every asset with this same engine, but
# only at TAILOR time. This is that idea moved to the scoring path: score the
# posting against each parsed resume and keep the best verdict, which is also
# the honest one — "can this candidate do this job" is a question about the
# person, not about which file is currently pinned.
#
# Cost is bounded two ways: `MAX_SCORING_RESUMES` caps the fan-out, and the
# built profiles are memoized on the resume-set FINGERPRINT (the same one
# `job_title_seed` uses), so the profiles are rebuilt only when a resume is
# added, removed or re-parsed — never per request.

MAX_SCORING_RESUMES = int(os.environ.get("FIT_MAX_SCORING_RESUMES", "") or 6)

# (user_email, fingerprint) -> [{resume_id, filename, profile}]. Module scope so
# a warm Lambda container and the long-lived ingest process both reuse it. Only
# ever grows by one entry per user per resume-set change; `_PROFILES_MAX` bounds
# it so a long-lived process cannot accumulate every user who ever searched.
_PROFILES_CACHE: "OrderedDict[Tuple[str, str], List[Dict[str, Any]]]" = OrderedDict()
_PROFILES_MAX = int(os.environ.get("FIT_PROFILE_CACHE_MAX", "") or 256)
_PROFILES_LOCK = threading.Lock()


def _profiles_cache_get(key: Tuple[str, str]) -> Optional[List[Dict[str, Any]]]:
    with _PROFILES_LOCK:
        hit = _PROFILES_CACHE.get(key)
        if hit is not None:
            _PROFILES_CACHE.move_to_end(key)
        return hit


def _profiles_cache_put(key: Tuple[str, str], value: List[Dict[str, Any]]) -> None:
    with _PROFILES_LOCK:
        _PROFILES_CACHE[key] = value
        _PROFILES_CACHE.move_to_end(key)
        while len(_PROFILES_CACHE) > _PROFILES_MAX:
            _PROFILES_CACHE.popitem(last=False)


def reset_profiles_cache() -> None:
    """Test hook / explicit invalidation (the cache is otherwise process-wide)."""
    with _PROFILES_LOCK:
        _PROFILES_CACHE.clear()


def load_profiles_for_user(user_email: str) -> List[Dict[str, Any]]:
    """One built profile per parsed resume, active resume FIRST.

    Returns `[]` for an account with no parsed resume — the same "cannot be
    scored" signal `load_profile_for_user` returns None for. Ordering matters:
    ties are broken toward the active resume, so a user who pinned a file still
    sees that file credited when two resumes score identically.
    """
    if not user_email:
        return []
    try:
        from services import resume_access
        from services.job_title_seed import fingerprint_assets

        assets = resume_access.list_parsed_assets(user_email)
    except Exception as exc:
        logger.warning("fit_score: could not list resumes for %s: %s",
                       user_email, type(exc).__name__)
        return []
    if not assets:
        return []

    # Declared targets are an INPUT to every profile below, so they belong in
    # the cache key. Without them, editing onboarding would leave every cached
    # profile stale until a résumé changed.
    target_roles = load_target_roles(user_email)
    fp = ""
    try:
        fp = fingerprint_assets(assets)
    except Exception:
        fp = ""
    if fp and target_roles:
        fp = fp + ":" + hashlib.sha1(
            "\x00".join(target_roles).encode("utf-8", "replace")).hexdigest()[:12]
    key = (user_email, fp)
    if fp:
        hit = _profiles_cache_get(key)
        if hit is not None:
            return hit

    active_id = ""
    try:
        from services import resume_access as _ra
        active_id = _ra.get_active_resume_id(user_email) or ""
    except Exception:
        active_id = ""

    # Active first, then newest-parsed (list_parsed_assets already sorts by
    # parsed_at desc), so the cap drops the STALEST resumes, never the pinned one.
    ordered = sorted(assets, key=lambda a: str(a.get("resume_id") or "") != active_id)

    built: List[Dict[str, Any]] = []
    for asset in ordered[:max(1, MAX_SCORING_RESUMES)]:
        structured = asset.get("structured")
        if not isinstance(structured, dict) or not structured:
            continue
        try:
            profile = build_resume_profile(structured, target_roles)
        except Exception as exc:
            logger.warning("fit_score: profile build failed for resume %s: %s",
                           asset.get("resume_id"), type(exc).__name__)
            continue
        if not profile:
            continue
        built.append({
            "resume_id": str(asset.get("resume_id") or ""),
            "filename": str(asset.get("filename") or ""),
            "profile": profile,
        })

    if fp:
        _profiles_cache_put(key, built)
    return built


def best_profile_meta(profiles: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Batch meta for a resume SET: the union of what we know about the person.

    `_batch_meta` describes one resume. A set-aware caller reports the union so
    "has_resume" and the role families reflect every file, not the pinned one.
    """
    if not profiles:
        return unscored_meta()
    skills: set = set()
    families: set = set()
    years: Optional[int] = None
    for entry in profiles:
        prof = entry.get("profile") or {}
        skills |= (prof.get("experience_skills") or set())
        skills |= (prof.get("listed_skills") or set())
        families |= (prof.get("families") or set())
        y = prof.get("years")
        if isinstance(y, int) and (years is None or y > years):
            years = y
    return {
        "score_version": FIT_SCORE_VERSION,
        "resume_skill_count": len(skills),
        "has_resume": bool(skills or families),
        "resume_years": years,
        "resume_role_families": sorted(families),
        "resume_count": len(profiles),
    }


def score_one_best(
    profiles: List[Dict[str, Any]],
    *,
    jd_text: str = "",
    title: str = "",
    job: Optional[Dict[str, Any]] = None,
    location: str = "",
) -> Optional[Dict[str, Any]]:
    """`score_one` against every resume in the set; the BEST verdict wins.

    The winning verdict carries a `matched_resume` sidecar naming the file that
    produced it, so the UI can say WHICH resume this score belongs to (and the
    tailor step can pre-select it). Returns None for an empty set — the caller
    must render "not scored" rather than invent a number.

    Ties keep the earlier entry, and `load_profiles_for_user` puts the active
    resume first, so a tie resolves to the resume the user pinned.
    """
    if not profiles:
        return None
    best: Optional[Dict[str, Any]] = None
    best_score = -1
    for entry in profiles:
        try:
            verdict = score_one(
                jd_text=jd_text, title=title, job=job,
                profile=entry.get("profile"), location=location,
            )
        except Exception as exc:
            logger.warning("fit_score: scoring failed for resume %s: %s",
                           entry.get("resume_id"), type(exc).__name__)
            continue
        score_value = int(verdict.get("match_score") or 0)
        if score_value > best_score:
            best_score = score_value
            verdict["matched_resume"] = {
                "resume_id": entry.get("resume_id") or "",
                "filename": entry.get("filename") or "",
            }
            best = verdict
    if best is not None and len(profiles) > 1:
        best["scored_across_resumes"] = len(profiles)
    return best
