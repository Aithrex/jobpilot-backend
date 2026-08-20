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
import re
from functools import lru_cache
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

logger = logging.getLogger(__name__)

FIT_SCORE_VERSION = "fit-v2.0"

# HAZARD - do not bump this without a re-parse plan.
#
# `_precomputed_requirements` rejects any stored profile whose score_version is
# not this exact string, and `score()` then falls back to extracting from the JD
# body. Inventory rows carry NO body (it lives in S3), so the fallback extracts
# from an empty string, `has_job_requirements` goes False for every posting at
# once, and the guest path filters the entire inventory out as evidence-less.
#
# A bump is therefore a hard cliff, not a gradual migration. Change the
# EXTRACTOR contract via `job_inventory_service.EXTRACTOR_VERSION` instead
# whenever the change is to what gets extracted rather than to how the
# components are weighed: that drives a re-parse through the normal crawl with
# no window during which nothing is servable.

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
    literal string the scanner will actually produce."""
    out: Dict[str, str] = {}
    for alias, canon in alias_to_canon.items():
        literal = alias.replace("\\", "") if _looks_like_regex(alias) else alias
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
    "software_engineering": {"data_engineering", "devops", "qa", "security", "data_science"},
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


def classify_role(text: Any) -> Optional[str]:
    """Role family for a title (or a short blob). Returns None when nothing
    matches. It NEVER falls back to software_engineering: defaulting an
    unrecognised nontechnical title into an engineering family is exactly the
    failure this engine exists to prevent."""
    blob = _norm(text)
    if not blob.strip():
        return None
    m = _FAMILY_RE.search(blob)
    if m:
        fam = _PHRASE_TO_FAMILY.get(m.group(0))
        # A physical-domain qualifier vetoes a software reading. "Hardware
        # Reliability Engineer" is a hardware role that happens to contain the
        # phrase "reliability engineer"; reading it as SRE puts a corrosion
        # engineer at the top of a cloud engineer's feed.
        if fam in _SOFTWARE_FAMILIES and _PHYSICAL_QUALIFIER_RE.search(blob):
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

_LEVEL_PATTERNS: Sequence[Tuple[str, re.Pattern]] = (
    ("executive", re.compile(r"(?<![a-z])(chief|vp of|vice president|head of|cto|ceo|cfo)(?![a-z])")),
    ("director", re.compile(r"(?<![a-z])(director|principal|distinguished|fellow)(?![a-z])")),
    ("staff", re.compile(r"(?<![a-z])(staff|architect|lead|manager|supervisor)(?![a-z])")),
    ("senior", re.compile(r"(?<![a-z])(senior|sr\.?|iii|iv)(?![a-z])")),
    ("entry", re.compile(r"(?<![a-z])(intern|internship|junior|jr\.?|entry level|entry-level|"
                         r"associate|graduate|trainee|apprentice|assistant|i{1,2})(?![a-z])")),
)

# Years a level normally implies, used only when the posting names no number.
_LEVEL_YEARS: Dict[str, int] = {
    "entry": 0, "mid": 3, "senior": 5, "staff": 8, "director": 10, "executive": 12,
}

# Ordinal seniority bands, used to compare a resume's title level against the
# posting's. `title_level` never yields "mid", but the map carries it so the
# `_LEVEL_YEARS` fallback level is orderable too.
_LEVEL_ORDER: Dict[str, int] = {
    "entry": 0, "mid": 1, "senior": 2, "staff": 3, "director": 4, "executive": 5,
}
# Bands where a two-plus-band under-level is a genuine screen-out signal, not
# just a stretch.
_SENIOR_PLUS: frozenset = frozenset({"senior", "staff", "director", "executive"})


def title_level(title: Any) -> Optional[str]:
    t = _norm(title)
    if not t.strip():
        return None
    for level, rx in _LEVEL_PATTERNS:
        if rx.search(t):
            return level
    return None


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

_CLEARANCE_JD = re.compile(
    r"(security clearance|ts/sci|top secret|secret clearance|active clearance|"
    r"public trust|polygraph|dod clearance|q clearance|l clearance)")
_CLEARANCE_RESUME = re.compile(
    r"(security clearance|ts/sci|top secret|secret clearance|active clearance|"
    r"public trust|polygraph|cleared to)")

_WORKAUTH_JD = re.compile(
    r"(no visa sponsorship|not (?:able|willing) to sponsor|"
    r"un(?:able|willing) to (?:provide |offer )?(?:visa )?sponsor|"
    r"without sponsorship|does not (?:provide|offer) sponsorship|"
    r"cannot sponsor|no sponsorship|must be a u\.?s\.? citizen|"
    r"u\.?s\.? citizenship (?:is )?required|citizens? only)")
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


def _required_years(blob: str) -> Optional[int]:
    best: Optional[int] = None
    for m in _YEARS_RE.finditer(blob):
        try:
            n = int(m.group(1))
        except (TypeError, ValueError):
            continue
        if 0 <= n <= 30:
            best = n if best is None else min(best, n)
    return best


def _degree_level(blob: str) -> int:
    """0 = none stated, 1 associate/diploma, 2 bachelor, 3 master, 4 doctorate."""
    for level, rx in _DEGREE_LEVELS:
        if rx.search(blob):
            return level
    return 0


def _industries(blob: str) -> Set[str]:
    out: Set[str] = set()
    for name, cues in INDUSTRIES.items():
        for cue in cues:
            if cue in blob:
                out.add(name)
                break
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
        "job_level": title_level(title_blob) or title_level(blob[:400]),
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

    Returns the profile only when it is complete AND was produced by this exact
    scorer version. Returns None otherwise, which makes the caller fall back to
    extracting from the JD body - correct, just slower.
    """
    if not isinstance(stored, dict) or not stored:
        return None
    if stored.get("score_version") != FIT_SCORE_VERSION:
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


def _resume_years(resume: Dict[str, Any]) -> Optional[int]:
    """Total years from DATED evidence only. No dates means None (unknown),
    never a guess: inventing a duration is a resume fact the candidate did
    not state."""
    explicit = resume.get("experience_years")
    if explicit is not None:
        try:
            return max(0, int(explicit))
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
    return months // 12 if months > 0 else 0


def build_resume_profile(resume: Dict[str, Any]) -> Dict[str, Any]:
    """Everything the scorer needs from one parsed resume, computed once and
    reused across every posting in a batch."""
    resume = resume or {}
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
    return {
        "experience_skills": set(find_skills(exp_text)),
        "listed_skills": set(find_skills(skills_text)),
        "all_text": full_norm,
        "titles": [t for t in titles if t.strip()],
        "families": families,
        "years": _resume_years(resume),
        "level": next((lv for lv in (title_level(t) for t in titles) if lv), None),
        "degree_level": _degree_level(_norm(" ".join(
            str((e or {}).get("degree") or "") for e in (resume.get("education") or [])
            if isinstance(e, dict)))),
        "industries": _industries(full_norm),
        "location": _norm((resume.get("contact") or {}).get("location") or ""),
        "has_dated_experience": _resume_years(resume) is not None,
    }


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
        "years": years,
        "level": next((lv for lv in (title_level(t) for t in title_list) if lv), None),
        "degree_level": 0,
        "industries": set(),
        "location": "",
        "has_dated_experience": years is not None,
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


def _score_role(reqs: Dict[str, Any], prof: Dict[str, Any], job_title: str
                ) -> Tuple[float, str, bool]:
    """(points, detail, unrelated_flag)"""
    mx = COMPONENT_MAX["role_alignment"]
    job_fam = effective_role_family(reqs, job_title)
    fams = prof.get("families") or set()
    job_tokens = _title_tokens(job_title)
    res_tokens: Set[str] = set()
    for t in prof.get("titles") or []:
        res_tokens |= _title_tokens(t)
    overlap = job_tokens & res_tokens

    if not job_fam and not fams:
        return mx * 0.6, "Role family could not be determined from either side.", False
    if job_fam and job_fam in fams:
        if overlap:
            return float(mx), "Your job titles are in the same role family as this posting.", False
        return mx * 0.92, "Your background is in the same role family as this posting.", False
    if not fams:
        if overlap:
            return mx * 0.6, "Partial title overlap, but your role history is unclear.", False
        return mx * 0.48, "Your role history could not be classified.", False
    if not job_fam:
        if overlap:
            return mx * 0.68, "Partial title overlap with this posting.", False
        return mx * 0.48, "This posting's role family could not be determined.", False
    related = RELATED_FAMILIES.get(job_fam, set()) & fams
    if related:
        return mx * 0.6, "Your background is in an adjacent role family.", False
    if len(overlap) >= 2:
        return mx * 0.4, "Some title overlap across different role families.", False
    if overlap:
        return mx * 0.24, "Limited title overlap across different role families.", True
    return mx * 0.08, "This role is in a different field from your background.", True


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


def _score_experience(reqs: Dict[str, Any], prof: Dict[str, Any]
                      ) -> Tuple[float, str, bool]:
    """(points, detail, severe_gap_flag).

    Scored primarily off the years gap, then corrected by the resume's title
    SENIORITY band. Dated-years alone miss the case of someone with several
    noisy years but only ever junior/associate titles applying to a Staff role
    - the band comparison catches that, and the symmetric over-qualification
    case for entry roles.
    """
    mx = COMPONENT_MAX["experience"]
    have = prof.get("years")
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

    if have is None or want is None:
        # Years unknown on one side. A clear two-plus-band under-level for a
        # senior+ posting is still a real signal, so don't hand out the neutral
        # "unknown" credit in that case.
        if level_gap is not None and level_gap >= 2 and job_level in _SENIOR_PLUS:
            return mx * 0.30, ("Your job titles read below the seniority this role "
                               "asks for."), True
        return mx * 0.65, "Years of experience are not stated on both sides.", False

    gap = want - have
    if gap <= 0:
        if job_level in ("entry",) and have >= want + 6:
            pts, detail, severe = mx * 0.7, (
                f"You have about {have} years, well above what this entry level "
                f"role asks for."), False
        else:
            pts, detail, severe = float(mx), (
                f"You have about {have} years against roughly {want} asked for."), False
    elif gap <= 1:
        pts, detail, severe = (
            mx * 0.85, f"You have about {have} years against roughly {want} asked for.", False)
    elif gap <= 2:
        pts, detail, severe = (
            mx * 0.65, f"About {gap} years short of the {want} years asked for.", False)
    elif gap <= 4:
        pts, detail, severe = (
            mx * 0.35, f"About {gap} years short of the {want} years asked for.", False)
    else:
        severe = stated or job_level in _SENIOR_PLUS
        pts, detail, severe = (
            mx * 0.12, f"About {gap} years short of the {want} years asked for.", severe)

    # Seniority-band correction. Only ever dampens - it cannot lift a weak
    # years score, so a padded resume can't buy seniority points.
    if level_gap is not None and level_gap >= 2:
        cap = mx * 0.4
        if pts > cap:
            pts, detail = cap, ("Your job titles read well below the seniority this "
                                "role asks for.")
        if job_level in _SENIOR_PLUS:
            severe = True
    elif level_gap is not None and level_gap == 1 and pts > mx * 0.75:
        pts = mx * 0.75
    elif (level_gap is not None and level_gap <= -2 and job_level == "entry"
          and pts > mx * 0.7):
        # Markedly over-levelled for an entry role - a common auto-reject.
        pts, detail = mx * 0.7, ("Your seniority is well above what this entry level "
                                 "role asks for.")

    return pts, detail, severe


def _score_domain(reqs: Dict[str, Any], prof: Dict[str, Any]) -> Tuple[float, str]:
    mx = COMPONENT_MAX["domain"]
    job_inds = set(reqs.get("industries") or [])
    res_inds = prof.get("industries") or set()
    if not job_inds or not res_inds:
        return mx * 0.6, "Industry could not be determined on both sides."
    shared = job_inds & res_inds
    if shared:
        return float(mx), f"Shared industry context: {', '.join(sorted(shared))}."
    return mx * 0.2, "Different industry from your background."


def _score_location(reqs: Dict[str, Any], prof: Dict[str, Any], job_location: str
                    ) -> Tuple[float, str, bool]:
    mx = COMPONENT_MAX["location"]
    mode = reqs.get("work_mode")
    if mode == "remote":
        return float(mx), "This role is remote.", False
    res_loc = prof.get("location") or ""
    job_loc = _norm(job_location)
    if not res_loc or not job_loc:
        return mx * 0.6, "Location compatibility is unknown.", False
    res_states = set(_STATE_RE.findall(res_loc))
    job_states = set(_STATE_RE.findall(job_loc))
    res_city = res_loc.split(",")[0].strip()
    job_city = job_loc.split(",")[0].strip()
    if res_city and res_city == job_city:
        return float(mx), "Same city as your listed location.", False
    if res_states & job_states:
        return mx * 0.8, "Same state as your listed location.", False
    if mode == "hybrid":
        return mx * 0.4, "Hybrid role in a different area from your listed location.", False
    if mode == "onsite":
        return mx * 0.2, "Onsite role in a different area from your listed location.", True
    return mx * 0.5, "Different area from your listed location.", False


def _score_education(reqs: Dict[str, Any], prof: Dict[str, Any],
                     license_ok: Optional[bool]) -> Tuple[float, str]:
    mx = COMPONENT_MAX["education"]
    want = int(reqs.get("required_degree_level") or 0)
    have = int(prof.get("degree_level") or 0)
    if license_ok is False:
        return 0.0, "A required credential is not shown on your resume."
    if want == 0:
        return float(mx) if license_ok else mx * 0.8, "No specific degree is required."
    if have == 0:
        return mx * 0.6, "Your education level could not be read from the resume."
    if have >= want:
        return float(mx), "Your education meets the stated requirement."
    if want - have == 1:
        return mx * 0.5, "Your education is one level below the stated requirement."
    return mx * 0.2, "Your education is below the stated requirement."


# ---------------------------------------------------------------------------
# Bands, confidence, copy
# ---------------------------------------------------------------------------

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
                out.append({"type": "license", "key": entry["key"], "status": "met",
                            "detail": f"{entry['label']} is evidenced on your resume."})
            elif (rule.get("role_evidence") is not None
                  and rule["role_evidence"].search(titles_text)):
                results.append(None)
                out.append({"type": "license", "key": entry["key"], "status": "unknown",
                            "detail": (f"This posting requires a {entry['label'].lower()}. "
                                       f"Your job titles suggest you hold one, but the "
                                       f"resume does not state it.")})
            else:
                results.append(False)
                out.append({"type": "license", "key": entry["key"], "status": "missing",
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
                        "detail": "A security clearance is evidenced on your resume."})
        else:
            out.append({"type": "clearance", "status": "missing",
                        "cap": CONSTRAINT_CAPS["clearance_missing"],
                        "detail": ("This posting requires a security clearance, which "
                                   "is not shown on your resume.")})

    if reqs.get("work_authorization_restricted"):
        if _WORKAUTH_RESUME_NEEDS.search(text):
            out.append({"type": "work_authorization", "status": "missing",
                        "cap": CONSTRAINT_CAPS["work_authorization_conflict"],
                        "detail": ("This employer states it does not sponsor, and your "
                                   "resume indicates sponsorship would be needed.")})
        else:
            out.append({"type": "work_authorization", "status": "unknown",
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

    role_pts, role_detail, unrelated = _score_role(reqs, prof, job_title)
    req_cov, req_covered, req_missing = _coverage(required, prof)
    req_pts = (req_cov * COMPONENT_MAX["required_skills"]) if required \
        else COMPONENT_MAX["required_skills"] * 0.6
    pref_cov, pref_covered, pref_missing = _coverage(preferred, prof)
    pref_pts = (pref_cov * COMPONENT_MAX["preferred_skills"]) if preferred \
        else COMPONENT_MAX["preferred_skills"] * 0.6
    exp_pts, exp_detail, severe_gap = _score_experience(reqs, prof)
    dom_pts, dom_detail = _score_domain(reqs, prof)
    loc_pts, loc_detail, location_bad = _score_location(reqs, prof, job_location)

    unrelated = unrelated and req_cov < 0.35
    constraints, license_ok = _hard_constraints(
        reqs, prof, severe_gap, location_bad, unrelated)
    edu_pts, edu_detail = _score_education(reqs, prof, license_ok)

    components = {
        "role_alignment": {"score": round(role_pts, 2),
                           "max": COMPONENT_MAX["role_alignment"], "detail": role_detail},
        "required_skills": {"score": round(req_pts, 2),
                            "max": COMPONENT_MAX["required_skills"],
                            "detail": (f"{len(req_covered)} of {len(required)} required "
                                       f"items evidenced.") if required
                            else "This posting names no clear required skills."},
        "experience": {"score": round(exp_pts, 2),
                       "max": COMPONENT_MAX["experience"], "detail": exp_detail},
        "preferred_skills": {"score": round(pref_pts, 2),
                             "max": COMPONENT_MAX["preferred_skills"],
                             "detail": (f"{len(pref_covered)} of {len(preferred)} preferred "
                                        f"items evidenced.") if preferred
                             else "This posting names no separate preferred skills."},
        "domain": {"score": round(dom_pts, 2),
                   "max": COMPONENT_MAX["domain"], "detail": dom_detail},
        "location": {"score": round(loc_pts, 2),
                     "max": COMPONENT_MAX["location"], "detail": loc_detail},
        "education": {"score": round(edu_pts, 2),
                      "max": COMPONENT_MAX["education"], "detail": edu_detail},
    }

    raw = sum(c["score"] for c in components.values())
    final = int(round(max(0.0, min(100.0, raw))))

    applied_caps: List[str] = []
    for c in constraints:
        cap = c.get("cap")
        if cap is not None and final > cap:
            final = int(cap)
            applied_caps.append(str(c.get("type")))

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


def score_batch(resume: Dict[str, Any], items: Iterable[Dict[str, Any]]
                ) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Any]]:
    """Score many postings against one resume. The resume profile is built
    once; requirement extraction is memoised per JD."""
    prof = build_resume_profile(resume or {})
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
    return build_resume_profile(structured)


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
    return score_batch(structured, items)


def _batch_meta(prof: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "score_version": FIT_SCORE_VERSION,
        "resume_skill_count": len(prof["experience_skills"] | prof["listed_skills"]),
        "has_resume": bool(prof["experience_skills"] or prof["listed_skills"]
                           or prof["titles"]),
        "resume_years": prof["years"],
        "resume_role_families": sorted(prof["families"]),
    }
