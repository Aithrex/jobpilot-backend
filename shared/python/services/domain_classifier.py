"""Profession/domain classifier (Craft-overhaul Phase 2).

The tailoring pipeline had no notion of profession — every deterministic layer
(keyword taxonomy, skill implications, scorer, project generator) assumed
software engineering. This classifier produces the coarse domain signal those
layers branch on, so a nurse's resume uses nursing assists (and skips GitHub
project generation) while an engineer's uses SWE assists.

Deterministic-first: it reads the `jd_analysis["industry"]` string (already
populated by the JD analyzer but never used as a control signal), the job title,
and the resume's skill tokens. It only needs an LLM if the heuristic is
genuinely ambiguous — and even then it fails safe to "general", which uses the
existing cross-industry prompt with no domain-specific deterministic assist
(i.e. today's graceful-degradation behavior). Never raises.

Domains (coarse, intentionally few):
  software · data · finance · healthcare · marketing · sales · legal ·
  operations · design · general
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ~20 research-backed domains (BLS/O*NET/LinkedIn job-functions, trimmed to the
# resume-using / ATS-keyword-tailoring market). Split so roles with genuinely
# distinct tool vocabularies each get a bucket; soft/low-tool roles share one.
DOMAINS = (
    # tech (code/portfolio)
    "software", "data_science", "data_engineering", "devops",
    # tech (no GitHub portfolio)
    "data_analytics", "cybersecurity", "it_support",
    # business / knowledge work
    "finance", "product", "project_program_mgmt", "business_ops",
    "marketing", "sales", "hr", "consulting",
    # licensed / specialized
    "healthcare", "legal", "design",
    # engineering disciplines (NOT software)
    "mechanical_eng", "civil_eng", "electrical_eng", "construction",
    # fallback
    "general",
)

# Domains for which GitHub-style project generation makes sense (code repos).
# Design uses a portfolio but NOT GitHub, so it is deliberately excluded here.
PROJECT_GEN_DOMAINS = frozenset({
    "software", "data_science", "data_engineering", "devops",
})

# Ordered rules: (domain, needles). FIRST MATCH WINS — order is load-bearing:
#   1. Non-software engineering disciplines FIRST, so "Civil/Mechanical/
#      Electrical Engineer" never falls into the bare-"engineer" software rule.
#   2. Specific tech sub-fields (data-*, devops, security) before generic software.
#   3. Generic "software"/"engineer" LAST as the tech catch-all.
# Needles are lowercased substrings matched against a blob of industry + title +
# skills. Space-padded needles (" ml ") match whole tokens only.
_DOMAIN_RULES: List = [
    # --- Engineering disciplines (MUST precede the generic "engineer" rule) ---
    ("civil_eng", ("civil engineer", "structural engineer", "geotechnical",
                   "site engineer", "civil 3d", "autocad civil", "surveying",
                   "transportation engineer")),
    ("mechanical_eng", ("mechanical engineer", "manufacturing engineer",
                        "design engineer", "solidworks", "catia", "gd&t",
                        "hvac engineer", "thermal engineer", "mechatronics",
                        "cad engineer")),
    ("electrical_eng", ("electrical engineer", "controls engineer", "power engineer",
                        "hardware engineer", "rf engineer", "pcb", "eplan",
                        "plc programming", "embedded hardware", "circuit design")),
    ("construction", ("construction", "general contractor", "estimator",
                      "superintendent", "procore", "primavera", "bluebeam",
                      "osha", "site supervisor", "field engineer", "cost estimator")),
    # --- Tech sub-fields (before generic software) ---
    ("data_science", ("data scientist", "machine learning", " ml ", "ml engineer",
                      "applied scientist", "mlops", "deep learning", "nlp",
                      "computer vision", "research scientist")),
    ("data_engineering", ("data engineer", "analytics engineer", "etl developer",
                          "data platform", "spark", "airflow", "data pipeline",
                          "data warehouse", "dbt")),
    ("data_analytics", ("data analyst", "business analyst", "bi analyst",
                        "business intelligence", "analytics", "tableau",
                        "power bi", "reporting analyst", "insights analyst")),
    ("cybersecurity", ("cybersecurity", "security analyst", "soc analyst",
                       "information security", "infosec", "penetration test",
                       "pen test", "siem", "threat", "vulnerability",
                       "security engineer", "appsec", "security operations")),
    ("devops", ("devops", " sre ", "site reliability", "platform engineer",
                "cloud engineer", "infrastructure engineer", "kubernetes",
                "terraform", "ci/cd")),
    ("it_support", ("help desk", "helpdesk", "it support", "desktop support",
                    "system administrator", "sysadmin", "network administrator",
                    "network engineer", "it technician", "service desk",
                    "servicedesk")),
    # --- Business / knowledge work ---
    ("finance", ("finance", "financial", "fp&a", "accounting", "accountant",
                 "gaap", "ifrs", "audit", "cpa", "cfa", "treasury", "tax",
                 "controller", "investment", "actuar", "underwrit", "banking",
                 "variance analysis", "budgeting")),
    ("product", ("product manager", "product owner", "product management",
                 "product lead", "group product manager", "product roadmap",
                 "product strategy")),
    ("hr", ("human resources", "recruiter", "recruiting", "talent acquisition",
            "hrbp", "hr business partner", "business partner", "people operations",
            "people ops", "hr generalist", "hr manager",
            "compensation and benefits", "onboarding specialist")),
    ("consulting", ("consultant", "consulting", "management consultant",
                    "strategy consultant", "advisory", "mba")),
    ("project_program_mgmt", ("project manager", "program manager", "scrum master",
                              "pmo", "delivery manager", "pmp", "agile coach",
                              "technical program manager", "tpm")),
    ("marketing", ("marketing", "seo", "sem", "growth", "brand", "content",
                   "social media", "paid social", "google ads", "ga4",
                   "hubspot", "campaign", "demand generation", "ppc",
                   "conversion optimization")),
    ("sales", ("sales", "account executive", "account manager", "business development",
               "quota", "sales pipeline", "salesforce", "sdr", "bdr", "revenue",
               "account exec", "sales development")),
    ("business_ops", ("operations", "supply chain", "logistics", "procurement",
                      "warehouse", "fulfillment", "vendor management",
                      "process improvement", "six sigma", "operations manager",
                      "business operations", "chief of staff")),
    # --- Licensed / specialized ---
    ("healthcare", ("nurse", "nursing", "rn ", "registered nurse", "clinical",
                    "patient", "healthcare", "medical", "physician", "epic",
                    "cerner", "telemetry", "acls", "bls", "phlebotom",
                    "pharmac", "therapist", "caregiver", "ehr")),
    ("legal", ("legal", "attorney", "lawyer", "paralegal", "counsel",
               "litigation", "compliance officer", "contract law", "bar admission")),
    ("design", ("designer", " ux ", " ui ", "product design", "graphic design",
                "figma", "sketch", "user research", "visual design",
                "interaction design")),
    # --- Generic software / engineering catch-all (LAST among tech) ---
    ("software", ("software", "engineer", "developer", "swe", "backend", "frontend",
                  "full stack", "fullstack", "programmer", "microservice",
                  "web developer", "application developer")),
]


def _blob(jd_analysis: Dict[str, Any], structured_resume: Optional[Dict[str, Any]]) -> str:
    parts: List[str] = []
    jd = jd_analysis or {}
    parts.append(str(jd.get("industry") or ""))
    parts.append(str(jd.get("job_title") or ""))
    # A few JD skills sharpen the signal (but title/industry dominate).
    for key in ("required_skills", "preferred_skills"):
        for s in (jd.get(key) or [])[:12]:
            if isinstance(s, str):
                parts.append(s)
    if structured_resume:
        # Resume job titles + a sample of skills.
        for e in (structured_resume.get("experience") or [])[:3]:
            parts.append(str(e.get("title") or ""))
        skills = structured_resume.get("skills")
        if isinstance(skills, dict):
            for vals in list(skills.values())[:4]:
                for s in (vals or [])[:6]:
                    if isinstance(s, str):
                        parts.append(s)
    # Pad with spaces so token-boundary needles like " ml " can match at edges.
    return " " + " ".join(parts).lower() + " "


def classify_domain(
    jd_analysis: Dict[str, Any],
    structured_resume: Optional[Dict[str, Any]] = None,
) -> str:
    """Return the coarse profession domain. Deterministic; never raises.

    Fail-safe default is "general" (no domain-specific assist — today's
    graceful-degradation behavior).
    """
    try:
        blob = _blob(jd_analysis, structured_resume)
        for domain, needles in _DOMAIN_RULES:
            if any(n in blob for n in needles):
                return domain
        return "general"
    except Exception as e:
        logger.warning("domain_classifier: failed (%s) — defaulting to general", e)
        return "general"


def allows_project_generation(domain: str) -> bool:
    """GitHub-style project generation only makes sense for tech domains."""
    return domain in PROJECT_GEN_DOMAINS


# ---------------------------------------------------------------------------
# Career-Portals retrieval vocabulary (used by workday/career-pages/mnc tabs)
# ---------------------------------------------------------------------------
# Single-word profession head-nouns. The Career-Portals engines use this to
#   (a) accept a lone-token job title as a real search term, and
#   (b) widen cross-title recall to the user's OWN profession.
# It is a SUPERSET of the old tech-only allowlist (engineer/developer/...), so
# tech users are unaffected — it only rescues non-tech titles that were being
# dropped (a lone "Nurse"/"Electrician"/"Accountant" used to fall through to
# the "software engineer" fallback). Generic corporate filler (manager,
# coordinator, specialist, associate, director, ...) is deliberately EXCLUDED:
# those match nearly every posting and would flood results.
ROLE_NOUNS = frozenset({
    # tech (superset of the previous _derive_query_terms allowlist)
    "engineer", "developer", "scientist", "analyst", "architect", "devops",
    "sre", "administrator", "consultant", "designer", "programmer",
    # healthcare
    "nurse", "physician", "surgeon", "clinician", "therapist", "pharmacist",
    "technologist", "technician", "phlebotomist", "hygienist", "paramedic",
    "caregiver", "practitioner", "dietitian", "sonographer", "radiographer",
    # skilled trades / industrial
    "electrician", "machinist", "welder", "mechanic", "plumber", "carpenter",
    "operator", "millwright", "fabricator", "installer", "lineman",
    "pipefitter",
    # finance / legal / admin
    "accountant", "auditor", "bookkeeper", "controller", "actuary",
    "underwriter", "attorney", "paralegal", "counsel", "adjuster", "teller",
    "cashier",
    # education / services / hospitality / ops
    "teacher", "professor", "instructor", "librarian", "counselor", "chef",
    "cook", "bartender", "server", "barista", "housekeeper", "custodian",
    "estimator", "surveyor", "inspector", "appraiser", "recruiter", "buyer",
    "planner", "dispatcher", "driver",
})


def user_role_noun_re(titles: Optional[List[str]]) -> Optional["re.Pattern"]:
    """Regex matching the profession head-nouns present in the user's OWN
    titles (e.g. {"nurse"} for a nurse, {"engineer","devops"} for a SWE).

    Career-Portals tier-1 recall unions this with the built-in engineering net,
    so a nurse also recalls differently-titled nurse rows ("Charge Nurse")
    without a global list that would flood every user's results. Returns None
    when the titles carry no recognizable role noun (callers then keep today's
    exact behavior). Never raises.
    """
    try:
        found = {
            re.escape(tok)
            for t in (titles or [])
            for tok in re.split(r"[^a-z0-9]+", str(t).lower())
            if tok in ROLE_NOUNS
        }
        if not found:
            return None
        return re.compile(r"\b(?:" + "|".join(sorted(found)) + r")\b", re.IGNORECASE)
    except Exception:  # pragma: no cover - retrieval must never break on this
        return None
