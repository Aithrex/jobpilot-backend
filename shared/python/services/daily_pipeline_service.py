"""
Daily Job Pipeline Service.

Ports the standalone job_pipeline.py flow into the resume-parser microservice.

Flow:
  1. Run two Apify actors in parallel:
       - curious_coder/linkedin-jobs-scraper   (LinkedIn keyword searches, past-N-days)
       - fantastic-jobs/workday-jobs-api       (Workday entry-level FTE titles)
  2. Filter & score against the user's profile (role families, body-shop /
     clearance / healthcare blocklists, H-1B sponsor & AI-native boosts).
  3. Return tier-grouped JSON (Tier 1/2/3) for the UI to render.

The user can override the LinkedIn keyword phrases and the Workday role titles
on every request; sensible defaults match job_pipeline.py.
"""
from __future__ import annotations

import ast
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import requests

from utils.config import _get_config_value
from utils.datadog_metrics import dd_metric

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Defaults (mirror job_pipeline.py)
# --------------------------------------------------------------------------
LINKEDIN_ACTOR = "curious_coder~linkedin-jobs-scraper"
WORKDAY_ACTOR = "fantastic-jobs~workday-jobs-api"
INDEED_ACTOR = "borderline~indeed-scraper"

# Source 3 — Indeed queries. Three focused queries beat one OR'd query:
# Indeed's default matcher is AND-of-tokens, so a single "software engineer
# new grad cloud full stack" only finds titles containing every word (rare).
# Three narrow queries each rank precise matches first, accepting the ~$0.75
# per pipeline cost (3 × $0.25). Override via indeed_queries payload key to
# trim runs.
INDEED_QUERIES = [
    "software engineer new grad",
    "cloud engineer DevOps",
    "full stack AI engineer",
]

# Source 5 — Direct ATS APIs (free, no Apify, no auth). Each tuple is (slug,
# display name) for the public job-board endpoints. Operators can extend these
# lists; failures on a single slug are logged and skipped.
# (slug, display name, domain) — domain is used by the Source-4 Apify
# fallback when our direct slug guess for Source 5 misses the board.
DEFAULT_GREENHOUSE_SLUGS = [
    ("anthropic", "Anthropic", "anthropic.com"),
    ("openai", "OpenAI", "openai.com"),
    ("notion", "Notion", "notion.so"),
    ("scaleai", "Scale AI", "scale.com"),
    ("snorkelai", "Snorkel AI", "snorkel.ai"),
    ("glean", "Glean", "glean.com"),
    ("stripe", "Stripe", "stripe.com"),
    ("procoretechnologies", "Procore", "procore.com"),
    ("cohere", "Cohere", "cohere.com"),
]
DEFAULT_LEVER_SLUGS = [
    ("ramp", "Ramp", "ramp.com"),
    ("brex", "Brex", "brex.com"),
    ("rippling", "Rippling", "rippling.com"),
    ("happyrobot", "HappyRobot", "happyrobot.ai"),
    ("taktile", "Taktile", "taktile.com"),
]
DEFAULT_ASHBY_SLUGS = [
    ("cognitionai", "Cognition", "cognition.ai"),
    ("decagon", "Decagon", "decagon.ai"),
    ("sierra", "Sierra", "sierra.ai"),
    ("harvey", "Harvey", "harvey.ai"),
]

# Source 4 fallback — Apify actor used when Source 5 (direct ATS APIs)
# returned no postings for a configured company. Hard-coded here so the
# fallback is always available whenever the Apify token is present.
_ATS_APIFY_ACTOR = "enosgb~ats-job-scraper"


def _ats_apify_actor_id() -> str:
    return _ATS_APIFY_ACTOR


# ---------------------------------------------------------------------------
# Curated-company-set matching
# ---------------------------------------------------------------------------
# Naive `sp in company` substring matching is fragile when curated tokens are
# short or share fragments with unrelated names. A 2-3 char token like "ea "
# is intentionally kept as a "word + trailing space" hack, but longer multi-
# word entries should still be matched as whole words. This helper compiles
# each set once into a single alternation regex with word boundaries so that
# "ge" matches "ge healthcare" but not "agency" / "genesys", and "sap" doesn't
# fire for "sapphire".
_COMPANY_SET_REGEX_CACHE: Dict[int, Any] = {}


def _company_set_regex(token_set: set):
    cached = _COMPANY_SET_REGEX_CACHE.get(id(token_set))
    if cached is not None:
        return cached
    parts: List[str] = []
    for t in sorted(token_set, key=len, reverse=True):
        t = (t or "").strip()
        if not t:
            continue
        parts.append(re.escape(t))
    pattern = r"(?<![a-z0-9])(?:" + "|".join(parts) + r")(?![a-z0-9])"
    compiled = re.compile(pattern, re.IGNORECASE)
    _COMPANY_SET_REGEX_CACHE[id(token_set)] = compiled
    return compiled


def _company_in_set(company: str, token_set: set) -> bool:
    """Word-boundary aware company-name match against a curated token set."""
    if not company:
        return False
    return bool(_company_set_regex(token_set).search(company))


def _load_applied_index(user_email: str) -> Dict[Tuple[str, str], Dict[str, Any]]:
    """Build a (normalized_company, normalized_title) → {status, ts} map from saved_jobs.

    Used to tag pipeline rows the user has already engaged with, so the UI
    can show "Already applied 3d ago" instead of presenting the same row as
    fresh. We only consider statuses that mean "I've moved on this" — e.g.
    interested means they clicked Open but never confirmed; applied / interview
    / offer mean they actually submitted. "rejected" and "withdrawn" still
    count as "engaged" so the user doesn't waste another click.
    """
    out: Dict[Tuple[str, str], Dict[str, Any]] = {}
    if not user_email:
        return out
    try:
        from utils.db_connect import DBConnect
        db = DBConnect().get_db()
        rows = db.saved_jobs.find(
            {"user_email": user_email},
            {"job_data": 1, "status": 1, "saved_at": 1, "applied_at": 1, "updated_at": 1},
        )
    except Exception as e:
        logger.warning(f"_load_applied_index: {e}")
        return out
    for row in rows:
        jd = (row.get("job_data") or {})
        company = _norm_company(jd.get("company") or "").strip()
        title = (jd.get("title") or "").strip().lower()
        if not company or not title:
            continue
        ts = row.get("applied_at") or row.get("updated_at") or row.get("saved_at")
        if hasattr(ts, "isoformat"):
            ts_iso = ts.isoformat()
        else:
            ts_iso = ""
        key = (company, title)
        existing = out.get(key)
        # Prefer the most-engaged status. Order: applied/interview/offer >
        # rejected > interested > withdrawn.
        rank = {"offer": 4, "interview": 3, "applied": 2, "rejected": 1, "interested": 0, "withdrawn": -1}
        new_status = str(row.get("status") or "interested")
        if not existing or rank.get(new_status, 0) > rank.get(existing.get("status", ""), 0):
            out[key] = {"status": new_status, "ts": ts_iso}
    return out


def _load_user_profile(user_email: str) -> Optional[Dict[str, Any]]:
    """Load (and cache for one pipeline run) the user's resume profile.

    Returns None when no parsed resume exists for the user — the scorer then
    falls back to legacy flat title bonuses, preserving existing behavior for
    users who haven't uploaded a resume yet.
    """
    try:
        from services.resume_profiler import build_profile, feedback_signal_for_user
        from utils.db_connect import DBConnect
        db = DBConnect().get_db()
        resume = db.user_resumes.find_one(
            {"user_email": user_email, "structured": {"$exists": True}},
            sort=[("parsed_at", -1)],
        )
        if not resume:
            return None
        structured = resume.get("structured") or {}
        if "experience_years" not in structured and "experience_years" in resume:
            structured = dict(structured)
            structured["experience_years"] = resume.get("experience_years")
        try:
            feedback = feedback_signal_for_user(db.job_activity_log, user_email)
        except Exception:
            feedback = None
        return build_profile(structured, feedback_signal=feedback)
    except Exception as e:
        logger.warning(f"Failed to load resume profile for {user_email}: {e}")
        return None

DEFAULT_LINKEDIN_KEYWORD_SETS = [
    "Cloud Engineer DevOps",
    "Site Reliability Platform Engineer",
    "full stack engineer AI",
    "backend engineer Python AWS",
    "agentic AI engineer new grad",
    "software engineer new grad entry level",
    "associate software engineer h1b sponsor",
]

DEFAULT_WORKDAY_TITLES = [
    # The actor's titleSearch is token-AND (every query token must appear in
    # the title). Junior/Associate/New-Grad/Senior variants are absorbed by
    # the parent because the parent's tokens are still present. We only keep
    # variants whose *tokens differ* from the parent — they'd otherwise be
    # genuine misses. aiExperienceLevelFilter=["0-2"] handles seniority.
    #
    # Cloud / DevOps / SRE / Platform
    "Cloud Engineer", "DevOps Engineer", "Site Reliability Engineer",
    "SRE",  # bare token — distinct from "Site Reliability Engineer"
    "Platform Engineer", "Infrastructure Engineer",
    "AWS Engineer", "Kubernetes Engineer",  # distinct specialty tokens
    # Backend / Full-Stack / Frontend
    "Backend Engineer", "Backend Developer",
    "Full Stack Engineer", "Full Stack Developer",
    "Frontend Engineer", "Frontend Developer",
    "API Engineer", "Software Developer",
    # AI / ML — keep both spellings; "ML" and "Machine Learning" are
    # different tokens, similarly LLM / GenAI.
    "AI Engineer", "Machine Learning Engineer", "ML Engineer",
    "LLM Engineer", "GenAI Engineer", "Applied Scientist",
    # General SWE early-career — distinct prefixes Workday's matcher won't
    # collapse on its own.
    "Software Engineer", "Software Engineer I",
    "Associate Software Engineer", "Junior Software Engineer",
    "New Grad Software Engineer", "Entry Level Software Engineer",
    "Software Development Engineer", "SDE I",
    "Member of Technical Staff", "Graduate Software Engineer",
]

BODY_SHOPS = {
    # Original list
    "beaconfire", "jobs via dice", "turing", "aditi", "apetan", "sira",
    "teksystems", "jobright", "chatgpt jobs", "dataannotation",
    "tata consultancy services", "tcs", "infosys", "wipro", "cognizant",
    "hcl", "akraya", "rk infotech", "crystal equation", "piper companies",
    "techtriad", "cgi", "candid", "compunnel", "kforce", "robert half",
    "insight global", "hays", "randstad", "ust global", "mphasis",
    "ltimindtree", "ltts", "l&t technology", "synechron", "virtusa",
    # Extended staffing / body shops that frequently pollute LinkedIn entry-level
    "softhq", "zycus", "saxon global", "diverse lynx", "pyramid consulting",
    "judge group", "the judge group", "modis", "akkodis", "iconma",
    "eteam", "etalent", "infojini", "bayone", "bayone solutions",
    "spar information systems", "spar group", "softpath", "zillion technologies",
    "talent burst", "talentburst", "softworld", "matlen silver", "vdart",
    "v-dart", "smartIMS", "smartims", "infinite computer solutions",
    "intellectt", "intellectt inc", "harvey nash", "experis", "manpower",
    "kelly services", "system soft", "systemsoft", "pinnacle group",
    "pinnacle technical", "tek experts", "tekberry", "tekgroup",
    "computer task group", "ctg ", "donatech", "donato technologies",
    "rangam consultants", "rangam", "wisestaffing", "wise staffing",
    "alphapoint technology", "advanced systems group", "asg",
    "kavaliro", "stefanini", "amerit consulting", "anveta",
    "talent serv", "talentserv", "rishabh software",
    "encora", "softworld inc", "millennium consulting",
    "collabera", "collabera digital", "sysmind", "wsource group",
    "us tech solutions", "ustech solutions", "us tech",
    "eclaro", "talent groups", "iqvia consulting",
    "marlabs", "softonic", "infomatics", "tek-hub",
    "the icon", "icon consultants",
    "mastech", "mastech digital", "trianz",
    "iquasar", "i-quasar",
    "amtex", "amtex enterprises",
    "n harris computer", "harris computer corporation",
    "magnit", "magnit global",
    "k force", "kforce inc",
    "dice", "diceus", "techorbit", "tech orbit",
    "sage it", "sage-it", "talent acquisition group",
    "raja sw", "rajasoftwares", "raja software",
    "smartsoft technologies", "smarttech", "infinity quest",
    "trigyn", "trigyn technologies",
    "ascendion", "ascendion engineering",
    "saicon", "saicon consultants",
    "datrose", "datrose llc",
    "primus knowledge", "primus software",
    "innova solutions", "innova", "katalyst", "katalyst healthcares",
    "metasys technologies", "metasys", "infoways",
}
CLEARANCE_COMPANIES = {
    "rtx", "raytheon", "lockheed", "northrop", "leidos", "caci",
    "gdit", "general dynamics", "nightwing", "boeing", "spacex",
    "amentum", "radiance technologies", "booz allen", "ball aerospace",
    "saic", "mantech", "peraton", "parsons", "huntington ingalls", "kbr",
    "bae systems",
}
H1B_SPONSORS = {
    "nvidia", "paypal", "notion", "adobe", "cisco", "intel", "target",
    "deutsche bank", "capital one", "goldman sachs", "morgan stanley",
    "procore", "allstate", "zillow", "servicenow", "cox", "manulife",
    "athenahealth", "mufg", "fidelity", "toyota", "general motors",
    "symbotic", "ge healthcare", "aerovironment", "calix", "cambium learning",
    "openai", "anthropic", "meta", "google", "amazon", "microsoft", "apple",
    "stripe", "databricks", "snowflake", "datadog", "atlassian", "salesforce",
    "linkedin", "uber", "doordash", "airbnb", "pinterest", "reddit", "snap",
    "twilio", "okta", "block", "shopify", "intuit", "vmware", "oracle",
    "sap", "ibm", "dell", "hpe", "vanguard", "jpmorgan", "wells fargo",
    "bank of america", "citi", "blackrock", "amex", "american express",
    "verizon", "t-mobile", "comcast", "disney", "warner bros", "ea ",
    "electronic arts", "activision", "roblox",
}
AI_NATIVE = {
    "notion", "openai", "anthropic", "glean", "abridge",
    "applied intuition", "rilla", "arcade", "snorkel", "cognition",
    "happyrobot", "taktile", "intelepeer", "scout ai", "perplexity",
    "harvey", "decagon", "sierra", "writer", "runway", "character.ai",
    "mistral", "cohere", "huggingface", "hugging face", "weaviate",
    "pinecone", "langchain", "llamaindex", "replicate", "modal",
    "fixie", "you.com", "elevenlabs", "suno",
}
HEALTHCARE_NOISE_TITLE = re.compile(
    r"\b(rn|registered nurse|nursing|physical therapist|occupational therapist|"
    r"speech therapist|sales representative|sales account|account manager|"
    r"dialysis|maternity|behavioral health|cardiac|surgical|icu|stepdown|"
    r"resident|paramedic|technician|phlebotomist|radiologist|sonographer|"
    r"pharmacy|pharmacist|dental|dentist|veterinar|chiropractor|"
    r"medical assistant|cna|lpn|caregiver|hospice|hospital|"
    r"truck driver|delivery driver|cdl|warehouse|forklift|"
    r"cashier|barista|line cook|server|housekeep|janitor|"
    r"teacher|tutor|instructor|professor|aide|counselor|"
    r"financial advisor|wealth manager|loan officer|"
    r"property manager|leasing|real estate|insurance agent|"
    r"hairstylist|cosmetolog|massage|esthetician|"
    r"electrician|plumber|hvac|welder|mechanic)\b",
    re.IGNORECASE,
)
SENIORITY_BAD = re.compile(
    r"\b(senior|sr\.?|principal|staff|lead|manager|director|"
    r"head of|vp\b|vice president|chief|distinguished|fellow|"
    r"architect|architect\s+(?:i|ii|iii))\b",
    re.IGNORECASE,
)
GOOD_TITLE = re.compile(
    r"\b(associate|junior|jr\.?|new grad|early career|entry level|entry-level|"
    r"software engineer i\b|swe i\b|engineer i\b|graduate|"
    r"co-?op|ncg|2026|recent grad)\b",
    re.IGNORECASE,
)
AGENTIC = re.compile(r"\b(agentic|ai agent|llm agent|multi-agent)\b", re.IGNORECASE)
AI_TITLE = re.compile(
    r"\b(applied ai|ai engineer|ml engineer|machine learning engineer|"
    r"gen[- ]?ai|llm|nlp engineer)\b", re.IGNORECASE,
)
CLOUD_TITLE = re.compile(
    r"\b(cloud|devops|sre|site reliability|platform engineer|infrastructure|"
    r"kubernetes|terraform|aws engineer|gcp engineer|azure engineer)\b",
    re.IGNORECASE,
)
BACKEND_TITLE = re.compile(
    r"\b(backend|back-?end|api engineer|server-side|distributed systems)\b",
    re.IGNORECASE,
)
FULLSTACK_TITLE = re.compile(r"\b(full[- ]?stack|fullstack)\b", re.IGNORECASE)
FRONTEND_TITLE = re.compile(
    r"\b(front[- ]?end|frontend|react engineer|ui engineer|web engineer)\b",
    re.IGNORECASE,
)
CLEARANCE_TEXT = re.compile(
    r"\b(ts/sci|top secret|secret clearance|active clearance|us citizen(ship)? required|"
    r"us citizens only|itar|polygraph|public trust|sci clearance)\b",
    re.IGNORECASE,
)

# Explicit "we don't sponsor" language — read from job descriptions for the
# F1-student opt-in filter. Each pattern below is a phrase companies use to
# slam the door on F-1 / H-1B candidates. Matching ANY of these means the
# job is wasted on someone who needs sponsorship. The patterns deliberately
# require restrictive context (must / required / only) so inclusive language
# like "we welcome citizens, permanent residents, and visa holders" doesn't
# trigger a false positive.
NO_SPONSORSHIP_TEXT = re.compile(
    r"("
    r"no\s+(?:visa\s+)?sponsorship\s+(?:is\s+)?(?:available|offered|provided)|"
    r"sponsorship\s+(?:is\s+)?not\s+(?:available|offered|provided|provided\s+for\s+this\s+(?:role|position))|"
    r"unable\s+to\s+(?:offer|provide|sponsor)\s+(?:visa\s+)?sponsorship|"
    r"do(?:es)?\s+not\s+(?:offer|provide|sponsor)\s+(?:visa\s+)?sponsorship|"
    r"will\s+not\s+(?:offer|provide|sponsor)\s+(?:visa\s+)?(?:sponsorship)?|"
    r"cannot\s+(?:offer|provide|sponsor)\s+(?:visa\s+)?(?:sponsorship)?|"
    # "must be authorized to work in the US without [current or future] sponsorship"
    r"must\s+be\s+(?:legally\s+)?(?:authorized|eligible)\s+to\s+work\s+in\s+the\s+(?:u\.?s\.?|united\s+states)\s+without\s+(?:current\s+or\s+future\s+)?(?:the\s+need\s+for\s+)?(?:visa\s+)?sponsorship|"
    r"without\s+(?:the\s+need\s+for\s+)?(?:visa\s+|current\s+or\s+future\s+)?sponsorship\s+(?:now\s+or\s+in\s+the\s+future)?|"
    # Citizen/PR-only language — REQUIRES restrictive phrasing nearby.
    r"(?:must\s+be|required\s+to\s+be|applicants?\s+must\s+be)\s+(?:a\s+)?u\.?s\.?\s+citizen(?:ship)?(?:\s+or\s+(?:permanent\s+resident|green\s*card\s+holder))?|"
    r"u\.?s\.?\s+citizens?(?:hip)?\s+(?:is\s+|are\s+)?required|"
    r"u\.?s\.?\s+citizens?(?:hip)?\s+or\s+permanent\s+residents?\s+(?:only|required|status\s+required)|"
    r"(?:green\s*card|gc)\s+(?:holder\s+only|required|or\s+u\.?s\.?\s+citizen\s+only)|"
    r"u\.?s\.?\s+persons?\s+only"
    r")",
    re.IGNORECASE,
)

# Affirmative sponsorship signals — only fire when "sponsor"/"sponsorship"
# appears in a visa context. The earlier loose pattern matched "we sponsor
# team events" / "open to sponsoring our community", which would falsely
# light up the green badge. We now require a visa keyword (visa, H-1B,
# work authorization, work permit, OPT, EAD) within ~30 chars.
SPONSORSHIP_AFFIRM_TEXT = re.compile(
    r"("
    r"(?:visa\s+|work\s+(?:authorization|permit)\s+|h[-\s]?1b\s+)sponsorship\s+(?:is\s+)?(?:available|provided|offered)|"
    r"sponsorship\s+(?:is\s+)?(?:available|provided|offered)\s+for\s+(?:visa|h[-\s]?1b|work\s+authorization|qualified\s+candidates)|"
    r"(?:we|company)\s+(?:will\s+|do\s+|are\s+(?:able|willing|happy)\s+to\s+)?(?:offer|provide|sponsor)\s+(?:[\w\s,]{0,30}?)?(?:visa|h[-\s]?1b|work\s+(?:authorization|permit)|opt|ead)(?:\s+sponsorship)?|"
    r"open\s+to\s+(?:visa\s+|h[-\s]?1b\s+)?sponsorship|"
    # "Open to sponsoring [up to 30 chars of stuff] visa/H-1B/qualified candidates/international"
    r"open\s+to\s+sponsoring\s+(?:[\w\s,]{0,30}?)?(?:visa|h[-\s]?1b|qualified\s+candidates|international)|"
    r"h[-\s]?1b\s+sponsorship\s+(?:is\s+)?(?:available|offered|provided)"
    r")",
    re.IGNORECASE,
)

INTERN_TITLE = re.compile(r"\b(intern|internship|summer 2026|co-?op)\b", re.IGNORECASE)


# --------------------------------------------------------------------------
# Apify client
# --------------------------------------------------------------------------
def _get_apify_token() -> str:
    return (
        _get_config_value("APIFY_API_KEY", "")
        or _get_config_value("APIFY_TOKEN", "")
        or ""
    )


_CREDIT_EXHAUSTED_HINTS = (
    "monthly-usage-limit-exceeded",
    "monthly-usage-hard-limit-exceeded",
    "actor-monthly-usage-limit-exceeded",
    "credit",
    "payment-required",
    "subscription",
    "insufficient",
)


def _classify_apify_error(status_code: int, body_text: str) -> str:
    """Return a structured error code the UI can pattern-match on."""
    body = (body_text or "").lower()
    if status_code == 402 or any(h in body for h in _CREDIT_EXHAUSTED_HINTS):
        return "APIFY_CREDITS_EXHAUSTED"
    if status_code in (401, 403):
        return "APIFY_AUTH_FAILED"
    if status_code == 429:
        return "APIFY_RATE_LIMITED"
    return "APIFY_ERROR"


def _run_actor(actor: str, run_input: dict, token: str, timeout_s: int = 480) -> List[Dict[str, Any]]:
    """Run an Apify actor synchronously and return its dataset items.

    Raises RuntimeError on failure with a structured error-code prefix so the
    pipeline result can surface a clear message to the user (e.g. exhausted
    credits → prompt to update their BYO Apify key).
    """
    url = f"https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items?token={quote(token)}"
    logger.info("[apify] starting %s ...", actor)
    t0 = time.time()
    # Short, low-cardinality actor tag (drop the dataset slug after `/` or `~`).
    actor_tag = actor.split("/", 1)[-1].split("~", 1)[-1][:48]
    try:
        r = requests.post(url, json=run_input, timeout=timeout_s)
    except requests.RequestException as e:
        dd_metric("portfolio.job_scrape.run", 1, tags=[f"actor:{actor_tag}", "result:network_error"])
        raise RuntimeError(f"APIFY_ERROR: {actor} request failed: {e}") from e
    if r.status_code >= 400:
        code = _classify_apify_error(r.status_code, r.text)
        dd_metric("portfolio.job_scrape.run", 1, tags=[f"actor:{actor_tag}", "result:error", f"code:{code.lower()}"])
        # Include a short slice of the actor's response body for 4xx errors
        # so the diagnostic surfaces "field X is required" / "invalid value"
        # to the logs and the UI errors panel. Strip newlines + cap to keep
        # error messages compact in JSON responses.
        body_snippet = ""
        if 400 <= r.status_code < 500:
            raw_body = (r.text or "").strip().replace("\n", " ")[:240]
            if raw_body:
                body_snippet = f": {raw_body}"
        raise RuntimeError(
            f"{code}: {actor} returned {r.status_code} {r.reason}{body_snippet}"
        )
    items = r.json() if r.headers.get("content-type", "").startswith("application/json") else []
    if isinstance(items, dict) and items.get("error"):
        # Some actors return a JSON error body with HTTP 200 — sniff it.
        err = items["error"]
        body = err.get("type", "") + " " + err.get("message", "")
        code = _classify_apify_error(0, body)
        raise RuntimeError(f"{code}: {actor} reported {err.get('type', 'error')}")
    if not isinstance(items, list):
        items = []
    elapsed_ms = (time.time() - t0) * 1000
    logger.info("[apify] %s -> %d items in %.1fs", actor, len(items), elapsed_ms / 1000)
    dd_metric("portfolio.job_scrape.run", 1, tags=[f"actor:{actor_tag}", "result:success"])
    dd_metric("portfolio.job_scrape.items", len(items), tags=[f"actor:{actor_tag}"])
    dd_metric("portfolio.job_scrape.duration_ms", elapsed_ms, tags=[f"actor:{actor_tag}"])
    return items


_LINKEDIN_EXPERIENCE_CODES = {
    # LinkedIn's f_E parameter — multiple values are comma-separated.
    # We send a RANGE rather than a single bucket so filtering doesn't drop
    # postings that LinkedIn tagged with an adjacent label. Without ranges,
    # picking "Associate (0-5y)" would only match LinkedIn-tagged Associate
    # roles and miss every entry-level grad posting that LinkedIn tagged
    # f_E=2 (Entry). Mirrors the Workday _WORKDAY_EXPERIENCE_MAP behavior.
    #   1=Internship  2=Entry  3=Associate  4=Mid-Senior  5=Director  6=Executive
    "internship": "1",
    "entry": "2",
    "associate": "2,3",   # entry + associate (0-5y)
    "mid": "3,4",         # associate + mid-senior (3-5y → covers some senior)
    "senior": "4,5",      # mid-senior + director (5-10y)
    "director": "5,6",
    "executive": "6",
}
_LINKEDIN_JT_CODES = {
    "FULLTIME": "F",
    "PARTTIME": "P",
    "CONTRACTOR": "C",
    "TEMPORARY": "T",
    "INTERN": "I",
}
_LINKEDIN_WT_CODES = {
    "onsite": "1",
    "remote": "2",
    "hybrid": "3",
}
_WORKDAY_EMPLOYMENT_MAP = {
    "FULLTIME": "FULL_TIME",
    "PARTTIME": "PART_TIME",
    "INTERN": "INTERN",
    "CONTRACTOR": "CONTRACTOR",
}
_WORKDAY_EXPERIENCE_MAP = {
    "internship": ["0-2"],
    "entry": ["0-2"],
    "associate": ["0-2", "3-5"],
    "mid": ["3-5"],
    "senior": ["5-10"],
    "director": ["10+"],
    "executive": ["10+"],
}
_WORKDAY_ARRANGEMENT_MAP = {
    "remote": ["Remote OK", "Remote Solely"],
    "hybrid": ["Hybrid"],
    "onsite": ["In-Office"],
}


def _build_linkedin_url(
    keywords: str,
    past_days: int,
    *,
    location: str = "United States",
    experience_level: str = "entry",
    employment_type: str = "FULLTIME",
    work_arrangement: str = "any",
) -> str:
    """Build a LinkedIn /jobs/search URL with the actor-supported filter params.

    All filters are optional — passing the defaults reproduces the previous
    24h/Entry-Level/US-only URL, so existing callers are unaffected.
    """
    f_tpr = "r86400" if past_days <= 1 else f"r{86400 * max(1, past_days)}"
    base = "https://www.linkedin.com/jobs/search/?"
    parts = [
        f"keywords={quote(keywords)}",
        f"location={quote(location or 'United States')}",
        f"f_TPR={f_tpr}",
    ]
    f_e = _LINKEDIN_EXPERIENCE_CODES.get((experience_level or "").lower())
    if f_e:
        parts.append(f"f_E={f_e}")
    # f_JT is only emitted for non-default employment types. The previous
    # LinkedIn URL had no f_JT, so passing employment_type="FULLTIME" (the
    # implicit baseline) reproduces that exactly. Explicit INTERN/CONTRACTOR/
    # PARTTIME tighten the URL; "ANY" leaves it unrestricted.
    emp_upper = (employment_type or "").upper()
    if emp_upper in ("PARTTIME", "INTERN", "CONTRACTOR"):
        f_jt = _LINKEDIN_JT_CODES.get(emp_upper)
        if f_jt:
            parts.append(f"f_JT={f_jt}")
    f_wt = _LINKEDIN_WT_CODES.get((work_arrangement or "").lower())
    if f_wt:
        parts.append(f"f_WT={f_wt}")
    return base + "&".join(parts)


# Pre-filter for ATS direct fetchers: drop sales / PM / marketing / recruiter /
# legal / finance / ops roles before we even normalize them. Saves compute on
# large company boards (Stripe, OpenAI, Anthropic publish 200+ jobs each).
_ATS_TECH_TITLE = re.compile(
    r"\b(engineer|engineering|developer|swe|sde|sre|mts|devops|"
    r"programmer|scientist|architect|technical staff|software|"
    r"data engineer|ml engineer|ai engineer|infrastructure|platform|"
    r"backend|frontend|full[- ]?stack|web)\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# HTML → plain text helper
# ---------------------------------------------------------------------------
# Greenhouse content fields and Ashby descriptionHtml return raw HTML. We
# strip tags and decode common entities so the Tailor JD textarea pre-fills
# with readable text. Deliberately small + dependency-free — we don't need a
# full HTML parser, just enough to remove tags and collapse whitespace.
_HTML_BLOCK_RE = re.compile(r"</?(?:p|br|div|li|ul|ol|h[1-6]|tr|td|hr)\b[^>]*>", re.IGNORECASE)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t]+")
_NL_RE = re.compile(r"\n{3,}")
_HTML_ENTITIES = {
    "&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"', "&#39;": "'",
    "&apos;": "'", "&nbsp;": " ", "&rsquo;": "'", "&lsquo;": "'",
    "&rdquo;": '"', "&ldquo;": '"', "&ndash;": "–", "&mdash;": "—",
    "&bull;": "•", "&hellip;": "…",
}


def _html_to_text(html: str) -> str:
    if not html:
        return ""
    s = str(html)
    # Block elements → newlines so paragraphs / list items don't smash together.
    s = _HTML_BLOCK_RE.sub("\n", s)
    # Strip remaining inline tags.
    s = _HTML_TAG_RE.sub("", s)
    # Decode the common entities (no full entity table — these cover ~99% of JDs).
    for entity, char in _HTML_ENTITIES.items():
        s = s.replace(entity, char)
    # Numeric entities like &#8217;
    s = re.sub(r"&#(\d+);", lambda m: chr(int(m.group(1))) if int(m.group(1)) < 0x10000 else "", s)
    # Whitespace cleanup.
    s = _WS_RE.sub(" ", s)
    s = _NL_RE.sub("\n\n", s)
    return s.strip()


def _fetch_greenhouse(slug: str, display: str) -> List[Dict[str, Any]]:
    # ?content=true returns each job with a `content` field holding the
    # full HTML description. Costs nothing extra (single API call) and
    # Tailor pre-fills correctly without a per-job follow-up request.
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
    try:
        r = requests.get(url, timeout=12)
        if not r.ok:
            return []
        items = (r.json() or {}).get("jobs") or []
    except Exception as e:
        logger.warning("Greenhouse %s failed: %s", slug, e)
        return []
    out: List[Dict[str, Any]] = []
    for j in items:
        title = _clean_str(j.get("title"))
        if not _ATS_TECH_TITLE.search(title):
            continue
        description = _html_to_text(_clean_str(j.get("content")))
        out.append({
            "source": "Greenhouse",
            "company": display,
            "title": title,
            "location": _clean_str(j.get("location")),
            "posted": _normalize_posted(j.get("updated_at")),
            "salary": "—",
            "applicants": "",
            "url": _clean_str(j.get("absolute_url")),
            "description": description[:6000],
        })
    return out


def _fetch_lever(slug: str, display: str) -> List[Dict[str, Any]]:
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    try:
        r = requests.get(url, timeout=10)
        if not r.ok:
            return []
        items = r.json() or []
    except Exception as e:
        logger.warning("Lever %s failed: %s", slug, e)
        return []
    out: List[Dict[str, Any]] = []
    for j in items:
        title = _clean_str(j.get("text"))
        if not _ATS_TECH_TITLE.search(title):
            continue
        cats = j.get("categories") or {}
        posted_iso = ""
        ts = j.get("createdAt")
        if isinstance(ts, (int, float)) and ts > 0:
            try:
                posted_iso = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
            except Exception:
                pass
        # Lever returns descriptionPlain (text) + descriptionBody (HTML) +
        # `lists` array of {text, content} (e.g. responsibilities). Compose
        # the most readable bundle we can: prefer descriptionPlain, fall back
        # to stripped descriptionBody, then append list bullets so the user
        # sees the role's responsibility / qualification sections.
        plain = _clean_str(j.get("descriptionPlain"))
        if not plain:
            plain = _html_to_text(_clean_str(j.get("descriptionBody")))
        list_chunks: List[str] = []
        for sec in (j.get("lists") or []):
            if not isinstance(sec, dict):
                continue
            heading = _clean_str(sec.get("text"))
            content = _html_to_text(_clean_str(sec.get("content")))
            if heading or content:
                list_chunks.append(f"\n\n{heading}\n{content}".strip())
        description = (plain + "".join(list_chunks)).strip()
        out.append({
            "source": "Lever",
            "company": display,
            "title": title,
            "location": _clean_str(cats.get("location") or cats.get("allLocations")),
            "posted": posted_iso,
            "salary": "—",
            "applicants": "",
            "url": _clean_str(j.get("hostedUrl") or j.get("applyUrl")),
            "description": description[:6000],
        })
    return out


def _fetch_ashby(slug: str, display: str) -> List[Dict[str, Any]]:
    url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
    try:
        r = requests.get(url, timeout=10)
        if not r.ok:
            return []
        items = (r.json() or {}).get("jobs") or []
    except Exception as e:
        logger.warning("Ashby %s failed: %s", slug, e)
        return []
    out: List[Dict[str, Any]] = []
    for j in items:
        title = _clean_str(j.get("title"))
        if not _ATS_TECH_TITLE.search(title):
            continue
        # Ashby returns descriptionPlain (preferred) or descriptionHtml.
        plain = _clean_str(j.get("descriptionPlain"))
        if not plain:
            plain = _html_to_text(_clean_str(j.get("descriptionHtml") or j.get("description")))
        out.append({
            "source": "Ashby",
            "company": display,
            "title": title,
            "location": _clean_str(j.get("location") or j.get("locationName")),
            "posted": _normalize_posted(j.get("publishedAt") or j.get("updatedAt")),
            "salary": "—",
            "applicants": "",
            "url": _clean_str(j.get("jobUrl") or j.get("applyUrl")),
            "description": plain[:6000],
        })
    return out


def _scrape_ats_direct(
    greenhouse: Optional[List[Tuple[str, str, str]]] = None,
    lever: Optional[List[Tuple[str, str, str]]] = None,
    ashby: Optional[List[Tuple[str, str, str]]] = None,
) -> List[Dict[str, Any]]:
    """Fan out to Greenhouse / Lever / Ashby in parallel and merge results."""
    gh = greenhouse if greenhouse is not None else DEFAULT_GREENHOUSE_SLUGS
    lv = lever if lever is not None else DEFAULT_LEVER_SLUGS
    ab = ashby if ashby is not None else DEFAULT_ASHBY_SLUGS
    tasks = []
    out: List[Dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        for slug, name, _domain in gh:
            tasks.append(pool.submit(_fetch_greenhouse, slug, name))
        for slug, name, _domain in lv:
            tasks.append(pool.submit(_fetch_lever, slug, name))
        for slug, name, _domain in ab:
            tasks.append(pool.submit(_fetch_ashby, slug, name))
        for fut in tasks:
            try:
                out.extend(fut.result(timeout=15))
            except Exception as e:
                logger.warning("ATS fetch worker failed: %s", e)
    logger.info("[ats-direct] fetched %d total postings", len(out))
    return out


def _ats_apify_fallback(
    missing_companies: List[Tuple[str, str, str]],
    ats_type: str,
    token: str,
    max_per_company: int = 50,
) -> List[Dict[str, Any]]:
    """Run the Source-4 Apify ATS actor for companies that Source 5 missed.

    No-ops cleanly when APIFY_ATS_ACTOR isn't configured, the token is empty,
    or there's nothing to fetch. Per the spec caveats: posted_at is always
    null on this actor, so we route everything through _normalize_posted (which
    returns "" for missing dates and the cutoff filter then keeps it). We do
    not pass `filters.keywords` — Python-side filtering does the work.
    """
    actor = _ats_apify_actor_id()
    if not actor or not missing_companies or not token:
        return []
    payload = {
        "ats_override": ats_type,
        "include_description": False,
        "include_salary": True,
        "max_jobs_per_company": max_per_company,
        "companies": [{"name": name, "domain": domain} for _slug, name, domain in missing_companies],
    }
    try:
        items = _run_actor(actor, payload, token, timeout_s=300)
    except Exception as e:
        logger.warning("ATS Apify fallback (%s) failed: %s", ats_type, e)
        return []
    out: List[Dict[str, Any]] = []
    label = ats_type.capitalize()
    for j in items or []:
        title = _clean_str(j.get("title"))
        if not _ATS_TECH_TITLE.search(title):
            continue
        out.append({
            "source": f"{label} (Apify)",
            "company": _clean_str(j.get("company") or j.get("companyName") or j.get("organization")),
            "title": title,
            "location": _clean_str(j.get("location") or j.get("locationName")),
            # spec note: posted_at is always null here — _normalize_posted("") -> ""
            "posted": _normalize_posted(j.get("posted_at") or j.get("posted") or j.get("createdAt")),
            "salary": _clean_str(j.get("salary") or j.get("salary_text")) or "—",
            "applicants": "",
            "url": _clean_str(j.get("url") or j.get("job_url") or j.get("apply_url")),
            "description": "",
        })
    logger.info("[ats-apify-fallback] %s -> %d postings (covered %d companies)",
                ats_type, len(out), len(missing_companies))
    return out


def _scrape_indeed(
    token: str,
    queries: Optional[List[str]] = None,
    max_rows: int = 75,
    *,
    past_days: int = 1,
    experience_level: str = "entry",
    employment_type: str = "FULLTIME",
    location: str = "United States",
) -> List[Dict[str, Any]]:
    """Source 3 — Indeed scrape. Returns normalized records or [] on any failure.

    User filters (past_days / experience_level / employment_type / location)
    are now threaded through to the actor input — previously they were
    hardcoded so the Indeed source ignored the rest of the form. The actor's
    canonical values are mapped from our internal vocab.
    """
    if not token:
        return []
    qs = queries if queries is not None else INDEED_QUERIES

    # Indeed actor's `level` accepts: entry_level | mid_level | senior_level | internship.
    indeed_level_map = {
        "internship": "internship",
        "entry": "entry_level",
        "associate": "entry_level",   # Indeed has no associate tier
        "mid": "mid_level",
        "senior": "senior_level",
        "any": "",                     # leave out to widen
    }
    # Indeed actor's `jobType`: fulltime | parttime | contract | temporary | internship.
    indeed_emp_map = {
        "FULLTIME": "fulltime",
        "PARTTIME": "parttime",
        "CONTRACTOR": "contract",
        "INTERN": "internship",
        "ANY": "",
    }
    level_v = indeed_level_map.get((experience_level or "").lower(), "entry_level")
    emp_v = indeed_emp_map.get((employment_type or "").upper(), "fulltime")
    # Indeed's `fromDays` is a string of days back. Clamp to actor's range.
    from_days = str(max(1, min(int(past_days or 1), 14)))
    country = "us"  # Indeed actor requires this — fixed regardless of location string.

    out: List[Dict[str, Any]] = []
    for q in qs:
        payload: Dict[str, Any] = {
            "country": country,
            "query": q,
            "fromDays": from_days,
            "sort": "date",
            "maxRows": max_rows,
        }
        if level_v:
            payload["level"] = level_v
        if emp_v:
            payload["jobType"] = emp_v
        # location string — only emit when user picked something more specific
        # than the US-baseline so the actor's default coverage stays wide.
        loc_clean = (location or "").strip()
        if loc_clean and loc_clean.lower() not in ("united states", "usa", "us", ""):
            payload["location"] = loc_clean
        try:
            items = _run_actor(INDEED_ACTOR, payload, token, timeout_s=180)
        except Exception as e:
            logger.warning("Indeed actor query=%r failed: %s", q, e)
            continue
        for j in items or []:
            out.append({
                "source": "Indeed",
                "company": _clean_str(j.get("companyName") or j.get("company")),
                "title": _clean_str(j.get("jobTitle") or j.get("title")),
                "location": _clean_str(j.get("location")),
                "posted": _normalize_posted(j.get("date") or j.get("postedAt") or j.get("posted_at")),
                "salary": _clean_str(j.get("salary") or j.get("salary_text")) or "—",
                "applicants": "",
                "url": _clean_str(j.get("url") or j.get("jobUrl") or j.get("job_url")),
                "description": _clean_str(j.get("description") or j.get("jobDescription"))[:500],
            })
    logger.info("[indeed] %d total postings across %d queries", len(out), len(qs))
    return out


def _scrape_in_parallel(
    token: str,
    linkedin_keywords: List[str],
    workday_titles: List[str],
    past_days: int,
    linkedin_count: int = 80,
    workday_limit: int = 200,
    *,
    location: str = "United States",
    # NOTE: experience_level / employment_type / work_arrangement are accepted
    # for API compatibility but are NO LONGER passed to the actors. Every
    # narrowing happens in _filter_and_score on the merged result so a single
    # cached fetch can serve every filter combination.
    experience_level: str = "entry",
    employment_type: str = "FULLTIME",
    work_arrangement: str = "any",
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[str]]:
    errors: List[str] = []

    linkedin_input = {
        "urls": [
            _build_linkedin_url(
                k, past_days,
                location=location,
                # Force wide on the actor side — post-filter handles narrowing.
                experience_level="any",
                employment_type="ANY",
                work_arrangement="any",
            )
            for k in linkedin_keywords
        ],
        "count": linkedin_count,
        "scrapeCompany": False,
    }

    # Workday actor — fetch with the widest filters the actor accepts. We used
    # to send aiExperienceLevelFilter / aiEmploymentTypeFilter /
    # aiWorkArrangementFilter so the actor pre-narrowed, but that's exactly
    # what was hiding Yahoo postings: the actor's AI labeller never tagged
    # them as 0-2, so they were dropped before we saw them. titleSearch is
    # still capped at 50 because the actor 400s on oversized payloads.
    safe_workday_titles = (workday_titles or [])[:50]
    workday_input: Dict[str, Any] = {
        "descriptionType": "text",
        "includeAi": True,
        "limit": workday_limit,
        "locationSearch": ["United States"],
        "removeAgency": True,
        "titleSearch": safe_workday_titles,
    }

    def _safe_run(actor: str, payload: dict) -> List[Dict[str, Any]]:
        try:
            return _run_actor(actor, payload, token)
        except Exception as e:
            msg = str(e)
            # 400 Bad Request → retry once with a stripped-down payload that
            # only carries the canonical fields the original pipeline used.
            # Most 400s come from one of the AI filter fields rejecting an
            # otherwise valid combination after Apify schema bumps.
            if actor == WORKDAY_ACTOR and "400" in msg and isinstance(payload, dict):
                logger.warning(
                    "Workday 400 — retrying with minimal payload. Original error: %s", msg,
                )
                fallback = {
                    "descriptionType": "text",
                    "includeAi": True,
                    "limit": payload.get("limit", workday_limit),
                    "locationSearch": ["United States"],
                    "removeAgency": True,
                    "titleSearch": payload.get("titleSearch", []),
                }
                try:
                    return _run_actor(actor, fallback, token)
                except Exception as e2:
                    msg2 = str(e2)
                    errors.append(f"{actor}: {msg2}")
                    logger.warning("Workday retry also failed: %s", msg2)
                    return []
            errors.append(f"{actor}: {msg}")
            logger.warning("Apify actor %s failed: %s", actor, msg)
            return []

    with ThreadPoolExecutor(max_workers=2) as ex:
        f_li = ex.submit(_safe_run, LINKEDIN_ACTOR, linkedin_input)
        f_wd = ex.submit(_safe_run, WORKDAY_ACTOR, workday_input)
        linkedin = f_li.result()
        workday = f_wd.result()

    # Silent-zero detection — when an actor returns [] with HTTP 200 and no
    # entry was added to the errors list, we can't tell from the result
    # whether the search was genuinely empty (narrow filters / stale time
    # window) or the actor was rate-limited / returned empty silently. Tag
    # both cases so the UI can hint at the cause.
    li_errored = any(LINKEDIN_ACTOR in e for e in errors)
    wd_errored = any(WORKDAY_ACTOR in e for e in errors)
    diagnostics = {
        "linkedin_urls": list(linkedin_input.get("urls", [])),
        "linkedin_silent_zero": (len(linkedin) == 0) and not li_errored,
        "workday_silent_zero": (len(workday) == 0) and not wd_errored,
        "linkedin_input_summary": {
            "urls": len(linkedin_input.get("urls", [])),
            "count_per_url": linkedin_input.get("count"),
        },
        "workday_input_summary": {
            "titles": len(workday_input.get("titleSearch") or []),
            "limit": workday_input.get("limit"),
            "experience": workday_input.get("aiExperienceLevelFilter"),
            "employment": workday_input.get("aiEmploymentTypeFilter"),
            "arrangement": workday_input.get("aiWorkArrangementFilter"),
        },
    }

    return linkedin, workday, errors, diagnostics


# --------------------------------------------------------------------------
# Normalization
# --------------------------------------------------------------------------
def _clean_str(value: Any) -> str:
    """Coerce arbitrary actor-returned values into a clean string.

    Apify/ATS responses occasionally return dicts ({"name": "..."} or
    {"display": "..."}) or lists of strings where we expect a plain string,
    which then crashes downstream `.strip()` / regex calls.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ("name", "display", "displayName", "text", "value", "label"):
            v = value.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
        return ""
    if isinstance(value, (list, tuple)):
        parts: List[str] = []
        for item in value:
            s = _clean_str(item)
            if s:
                parts.append(s)
        return ", ".join(parts)
    return str(value).strip()


def _norm_company(s: Any) -> str:
    return _clean_str(s).lower()


# Broader than the prior version so we catch every shape Apify / LinkedIn /
# Workday surface:
#   "2 days ago", "2d ago", "2h ago"
#   "Posted 2 days ago", "Reposted 5 days ago"
#   "Posted just now", "Active 3 hours ago"
# Anything that doesn't match still falls back to "unknown date" which the
# new strict-mode below routes to verify_dates when past_days <= 3.
_RELATIVE_TIME_RE = re.compile(
    r"^\s*(?:posted|reposted|listed|active|updated)?\s*"
    r"(\d+)\s*"
    r"(minute|min|m|hour|hr|h|day|d|week|w|month|mo)s?\s*"
    r"ago\s*$",
    re.IGNORECASE,
)
_JUST_NOW_RE = re.compile(
    r"^\s*(?:posted\s*)?(today|just\s*now|just\s*posted|now)\s*$",
    re.IGNORECASE,
)
_YESTERDAY_RE = re.compile(
    r"^\s*(?:posted\s*)?yesterday\s*$",
    re.IGNORECASE,
)


def _normalize_posted(raw: Any, now: Optional[datetime] = None) -> str:
    """Normalize a posted timestamp into YYYY-MM-DD.

    Handles ISO strings ("2026-04-29T...") and LinkedIn's relative phrases
    ("2 days ago", "5 hours ago"). Returns "" when unparseable so the
    downstream cutoff filter treats the row as unknown-date (kept).
    """
    if not raw:
        return ""
    s = str(raw).strip()
    # ISO date / datetime — first 10 chars are YYYY-MM-DD when present.
    if re.match(r"^\d{4}-\d{2}-\d{2}", s):
        return s[:10]
    m = _RELATIVE_TIME_RE.match(s)
    if m:
        n = int(m.group(1))
        unit = m.group(2).lower()
        ref = now or datetime.now(timezone.utc)
        delta = {
            "minute": timedelta(minutes=n), "min": timedelta(minutes=n), "m": timedelta(minutes=n),
            "hour":   timedelta(hours=n),   "hr":  timedelta(hours=n),   "h": timedelta(hours=n),
            "day":    timedelta(days=n),    "d":   timedelta(days=n),
            "week":   timedelta(weeks=n),   "w":   timedelta(weeks=n),
            "month":  timedelta(days=30 * n),
            "mo":     timedelta(days=30 * n),
        }.get(unit, timedelta())
        return (ref - delta).strftime("%Y-%m-%d")
    if _JUST_NOW_RE.match(s):
        return (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d")
    if _YESTERDAY_RE.match(s):
        return ((now or datetime.now(timezone.utc)) - timedelta(days=1)).strftime("%Y-%m-%d")
    return ""


def _linkedin_to_record(j: dict) -> dict:
    return {
        "source": "LinkedIn",
        "company": _clean_str(j.get("companyName")),
        "title": _clean_str(j.get("title")),
        "location": _clean_str(j.get("location")),
        "posted": _normalize_posted(j.get("postedAt") or j.get("postedDate") or j.get("listedAt")),
        "salary": _clean_str(j.get("salary")) or "—",
        "applicants": j.get("applicantsCount") or "",
        "url": _clean_str(j.get("link")),
        "description": _clean_str(j.get("descriptionText")),
    }


def _scrape_workday_direct(search_text: str, location: str) -> List[Dict[str, Any]]:
    """Direct Workday CXS scrape — free, no Apify, ~10s overall.

    Returns the raw shape produced by services.workday_cxs_fetcher; the caller
    runs each item through _workday_direct_to_record to get the daily-pipeline
    record shape (source/company/title/location/posted/url/description) that
    _filter_and_score expects.
    """
    try:
        from services.workday_cxs_fetcher import fetch_workday_direct
        items = fetch_workday_direct(
            search_text=search_text,
            location=location,
            overall_timeout=15,
            max_workers=32,
        )
        logger.info("[workday-direct] pipeline scrape returned %d postings", len(items))
        return items
    except Exception as e:
        logger.warning("Workday-direct pipeline scrape failed: %s", e)
        return []


def _workday_direct_to_record(j: dict) -> dict:
    """Adapt a workday_cxs_fetcher output dict to the pipeline record shape.

    The CXS list view omits descriptions; we leave the field empty and rely
    on the existing on-demand JD fetch (the Tailor flow already handles this
    for LinkedIn / Workday rows that lack a description).
    """
    return {
        "source": "Workday Direct",
        "company": _clean_str(j.get("company")),
        "title": _clean_str(j.get("title")),
        "location": _clean_str(j.get("location")),
        # workday_cxs_fetcher already gave us an ISO datetime in `date_posted`
        # (derived from "Posted N Days Ago"). _normalize_posted produces the
        # YYYY-MM-DD string _filter_and_score's cutoff comparison expects.
        "posted": _normalize_posted(j.get("date_posted")),
        "salary": _clean_str(j.get("salary")) or "—",
        "applicants": "",
        "url": _clean_str(j.get("apply_link")),
        "description": _clean_str(j.get("description")),
    }


def _workday_to_record(j: dict) -> dict:
    loc_raw = j.get("locations_alt_raw") or ""
    locs: List[str] = []
    if loc_raw:
        try:
            parsed = ast.literal_eval(loc_raw) if isinstance(loc_raw, str) else loc_raw
            if isinstance(parsed, list):
                locs = [str(x) for x in parsed]
        except Exception:
            locs = [str(loc_raw)]
    cities = j.get("cities_derived") or []
    regions = j.get("regions_derived") or []
    if isinstance(cities, str):
        try: cities = ast.literal_eval(cities)
        except Exception: cities = []
    if isinstance(regions, str):
        try: regions = ast.literal_eval(regions)
        except Exception: regions = []
    if not locs and (cities or regions):
        locs = [f"{c}, {r}" for c, r in zip(cities, regions)] or list(cities) or list(regions)
    if not locs and j.get("remote_derived"):
        locs = ["Remote (US)"]
    location = "; ".join(locs[:3]) if locs else "United States"

    salary_raw = j.get("salary_raw")
    salary = "—"
    if salary_raw:
        try:
            s = ast.literal_eval(salary_raw) if isinstance(salary_raw, str) else salary_raw
            v = s.get("value") if isinstance(s, dict) else None
            if isinstance(v, dict):
                lo, hi, cur = v.get("minValue"), v.get("maxValue"), v.get("unitText", "")
                if lo and hi: salary = f"${int(lo):,}-${int(hi):,} {cur}"
                elif lo:      salary = f"${int(lo):,}+ {cur}"
        except Exception:
            pass

    # The Workday actor returns the date under a few possible keys depending
    # on the actor version. Walk them all so we don't drop entries that just
    # have a different field name.
    posted_raw = (
        j.get("date_posted")
        or j.get("posted_date")
        or j.get("datePosted")
        or j.get("date_published")
        or j.get("aiDatePosted")
        or ""
    )
    return {
        "source": "Workday",
        "company": _clean_str(j.get("organization") or j.get("aiOrganization") or j.get("company")),
        "title": _clean_str(j.get("title") or j.get("aiTitle")),
        "location": _clean_str(location),
        "posted": _normalize_posted(posted_raw),
        "salary": salary,
        "applicants": "",
        "url": _clean_str(j.get("url") or j.get("link")),
        "description": _clean_str(j.get("description") or j.get("aiDescription"))[:500],
    }


# --------------------------------------------------------------------------
# Filters / scoring
# --------------------------------------------------------------------------
_US_STATE_CODES = frozenset({
    "al","ak","az","ar","ca","co","ct","de","fl","ga","hi","id","il","in",
    "ia","ks","ky","la","me","md","ma","mi","mn","ms","mo","mt","ne","nv",
    "nh","nj","nm","ny","nc","nd","oh","ok","or","pa","ri","sc","sd","tn",
    "tx","ut","vt","va","wa","wv","wi","wy","dc",
})


def _is_us(rec: dict) -> bool:
    loc = _clean_str(rec.get("location")).lower()
    if not loc:
        return True
    if "united states" in loc or "usa" in loc or "remote" in loc:
        return True
    # Standalone "US" / "U.S." / "U.S.A." tokens
    if re.search(r"\b(us|u\.s\.|u\.s\.a\.)\b", loc):
        return True
    # Any token that is a US state abbreviation (handles bare "NY", "TX",
    # "Plano, TX", "Remote, CA", etc.)
    tokens = re.findall(r"[a-z]{2,}", loc)
    if any(t in _US_STATE_CODES for t in tokens):
        return True
    return bool(
        re.search(
            r"\b(new york|san francisco|seattle|austin|chicago|boston|atlanta|"
            r"denver|phoenix|chandler|scottsdale|tempe|dallas|houston|raleigh|"
            r"durham|charlotte|miami|los angeles|san diego|portland|nashville)\b",
            loc,
        )
    )


_STAFFING_HINT = re.compile(
    r"\b(staffing|recruit(ing|ers?)|consultanc(y|ies)|placement(s)?|"
    r"workforce solutions|human capital|talent solutions|talent partners|"
    r"contract(?:s|ing)?|c2c|w2|corp[- ]to[- ]corp|body shop|"
    r"sourcing solutions|talent network|talent group|talent acquisition)\b",
    re.IGNORECASE,
)


def _blocked_company(rec: dict) -> Optional[str]:
    c = _norm_company(rec.get("company"))
    for bs in BODY_SHOPS:
        if bs in c:
            return f"body-shop:{bs}"
    # Generic staffing/recruiting hint anywhere in the company name —
    # catches the long tail of LLCs we haven't curated by name.
    if _STAFFING_HINT.search(_clean_str(rec.get("company"))) or _STAFFING_HINT.search(_clean_str(rec.get("description"))):
        return "staffing/recruiting agency"
    return None


_SUMMER_2026_RE = re.compile(
    r"\b(summer\s*2026|may\s*2026|june\s*2026|jun\s*2026|2026\s+summer)\b",
    re.IGNORECASE,
)


def _opt_body_shop_reason(rec: dict) -> Optional[str]:
    """Drop low-pay 'OPT-friendly' postings — common with shady consultancies."""
    desc = _clean_str(rec.get("description")).lower()
    salary = _clean_str(rec.get("salary"))
    if "opt" not in desc:
        return None
    # Pull the largest dollar figure we can find from the salary string.
    nums = re.findall(r"\$?\s*([\d]{2,3}(?:[, ]\d{3})*|[\d]{4,6})", salary)
    cleaned: List[int] = []
    for n in nums:
        try:
            cleaned.append(int(n.replace(",", "").replace(" ", "")))
        except ValueError:
            continue
    if cleaned and max(cleaned) < 70_000:
        return f"OPT body-shop pattern (salary < $70K + 'OPT' in description)"
    return None


def _summer_intern_block(rec: dict) -> Optional[str]:
    """Hard-exclude Summer/May/June 2026 internship starts — collide with OPT EAD start."""
    title = _clean_str(rec.get("title"))
    desc = _clean_str(rec.get("description"))
    if not INTERN_TITLE.search(title):
        return None
    if _SUMMER_2026_RE.search(title) or _SUMMER_2026_RE.search(desc):
        return "summer-2026 intern (OPT EAD timing conflict)"
    return None


def _ghost_job_reason(rec: dict) -> Optional[str]:
    """Heuristics for low-quality / ghost postings."""
    # Very high applicant count = saturated, often reposted
    try:
        apc = int(rec.get("applicants") or 0)
    except (ValueError, TypeError):
        apc = 0
    if apc >= 500:
        return f"saturated posting ({apc}+ applicants)"
    # Reposted-from-old indicator on LinkedIn descriptions
    desc = _clean_str(rec.get("description")).lower()
    if "reposted" in desc and ("month" in desc or "30 days" in desc):
        return "reposted (likely stale / ghost)"
    return None


def _clearance_block(rec: dict) -> Optional[str]:
    c = _norm_company(rec.get("company"))
    if _company_in_set(c, CLEARANCE_COMPANIES):
        m = _company_set_regex(CLEARANCE_COMPANIES).search(c)
        return f"clearance-co:{m.group(0) if m else 'match'}"
    title = _clean_str(rec.get("title"))
    desc = _clean_str(rec.get("description"))
    if CLEARANCE_TEXT.search(title) or CLEARANCE_TEXT.search(desc):
        return "clearance-required"
    return None


def _classify_visa_status(rec: dict) -> str:
    """Tag each row with its sponsorship signal: sponsor_verified / no_sponsorship / unknown.

    Used by both the F1-student exclusion filter and the per-row visa badge.
    Thin wrapper over _classify_visa_with_confidence so callers that only need
    the label stay unchanged.
    """
    status, _conf = _classify_visa_with_confidence(rec)
    return status


def _classify_visa_with_confidence(rec: dict) -> Tuple[str, float]:
    """Return (status, confidence_0_1) with a calibrated sponsorship score.

    Calibration anchors:
      0.97  — JD has explicit "no sponsorship" language (very high recall).
      0.92  — JD has explicit "we sponsor" / "H-1B available" language.
      0.88  — Company is on the curated H1B_SPONSORS list (~78 names).
      0.62  — Company name contains a tech-co stem (likely sponsor) but
              not on the strict list. F-1 friendly but worth verifying.
      0.40  — Unknown company with no JD signals. Default "needs research".
      0.20  — Tiny / unknown company that's unlikely to file H-1B paperwork.

    Status semantics (back-compat for existing F-1 filter):
      "sponsor_verified" — confidence >= 0.85 (strong positive signal).
      "no_sponsorship"   — explicit negative signal.
      "likely_sponsor"   — confidence 0.55..0.85 (positive but soft).
      "unknown"          — confidence < 0.55.
    The downstream `exclude_no_sponsorship` filter only drops the explicit
    negative bucket — soft signals never get dropped, just demoted in score.
    """
    company = _norm_company(rec.get("company"))
    desc = _clean_str(rec.get("description"))
    title = _clean_str(rec.get("title"))

    if desc and NO_SPONSORSHIP_TEXT.search(desc):
        return ("no_sponsorship", 0.97)

    on_list = bool(_company_in_set(company, H1B_SPONSORS))
    affirm = bool(desc and SPONSORSHIP_AFFIRM_TEXT.search(desc))

    if affirm and on_list:
        return ("sponsor_verified", 0.96)
    if affirm:
        return ("sponsor_verified", 0.92)
    if on_list:
        return ("sponsor_verified", 0.88)

    # Soft "likely sponsor" heuristic — large tech employers tend to file H-1B
    # paperwork even when the specific posting doesn't say so. The curated
    # list is conservative; this catches the long tail without false-positive
    # spamming small companies.
    likely_tech_stems = (
        "engineering", "technologies", "software", "labs", "cloud", "ai",
        "platform", "systems", "compute",
    )
    is_techy_company = bool(company) and any(s in company.lower() for s in likely_tech_stems)
    looks_like_tech_role = any(
        kw in (title or "").lower()
        for kw in (
            "engineer", "developer", "scientist", "sre", "devops",
            "platform", "infrastructure", "data", "ml ", "machine learning",
        )
    )
    if is_techy_company and looks_like_tech_role:
        return ("likely_sponsor", 0.62)

    if looks_like_tech_role:
        return ("unknown", 0.40)

    return ("unknown", 0.25)


def _healthcare_noise(rec: dict) -> Optional[str]:
    return "off-domain (healthcare/non-tech)" if HEALTHCARE_NOISE_TITLE.search(_clean_str(rec.get("title"))) else None


def _role_match(rec: dict, custom_role_terms: Optional[List[str]] = None) -> List[str]:
    t = _clean_str(rec.get("title"))
    out: List[str] = []
    if CLOUD_TITLE.search(t): out.append("Cloud/DevOps/SRE")
    if BACKEND_TITLE.search(t): out.append("Backend")
    if FULLSTACK_TITLE.search(t): out.append("Full-Stack")
    if AI_TITLE.search(t) or AGENTIC.search(t): out.append("AI/ML")
    if FRONTEND_TITLE.search(t): out.append("Frontend")
    if not out and re.search(r"\bsoftware engineer\b|\bswe\b", t, re.IGNORECASE):
        out.append("Software Engineer")
    if not out and custom_role_terms:
        tl = t.lower()
        for term in custom_role_terms:
            if term and term.lower() in tl:
                out.append(f"Custom: {term}")
                break
    return out


def _score(rec: dict, today_iso: Optional[str] = None, profile: Optional[Dict[str, Any]] = None) -> Tuple[int, List[str]]:
    s = 50
    flags: List[str] = []
    title = _clean_str(rec.get("title"))
    description = _clean_str(rec.get("description"))
    company = _norm_company(rec.get("company"))
    location = _clean_str(rec.get("location"))

    is_known_sponsor = _company_in_set(company, H1B_SPONSORS)
    if is_known_sponsor: s += 30; flags.append("H1B-sponsor")
    if _company_in_set(company, AI_NATIVE): s += 20; flags.append("AI-native")
    # Visa signal — when JD explicitly says "we sponsor" but the company isn't
    # in the curated H1B_SPONSORS list, give a smaller boost. Conversely, when
    # the JD says "no sponsorship" and the user hasn't opted into the strict
    # exclude_no_sponsorship filter, drop the score so it can't crowd Tier 1.
    visa = rec.get("visa_status")
    if visa == "sponsor_verified" and not is_known_sponsor:
        s += 12; flags.append("Sponsor-verified (JD)")
    elif visa == "no_sponsorship":
        s -= 30; flags.append("No-sponsorship JD")
    if AGENTIC.search(title):       s += 25; flags.append("Agentic")

    # Title-family bonuses are now WEIGHTED by the candidate's primary intent.
    # An ML-focused candidate's Cloud-titled job stays neutral; their ML-titled
    # job gets the larger boost. A generalist (no profile) still gets the
    # legacy flat bonuses so first-time users see the same behavior as before.
    primary = (profile or {}).get("primary_intent")
    secondary = (profile or {}).get("secondary_intent")

    def _title_bonus(family_match: bool, family_key: str, base: int) -> int:
        if not profile:
            return base
        if family_key == primary:
            return base + 8           # primary domain → premium
        if family_key == secondary:
            return base                # secondary domain → unchanged
        return max(2, base // 3)       # off-domain → heavily damped

    if CLOUD_TITLE.search(title):
        bump = _title_bonus(True, "cloud_devops", 15); s += bump; flags.append(f"Cloud/DevOps (+{bump})")
    if GOOD_TITLE.search(title):
        s += 15; flags.append("Early-career")
    if BACKEND_TITLE.search(title) or FULLSTACK_TITLE.search(title):
        family = "fullstack" if FULLSTACK_TITLE.search(title) else "backend"
        bump = _title_bonus(True, family, 12); s += bump; flags.append(f"Backend/FS (+{bump})")
    if FRONTEND_TITLE.search(title):
        bump = _title_bonus(True, "frontend", 8); s += bump; flags.append(f"Frontend (+{bump})")

    # AI/ML title family — when the candidate's primary intent is ai_ml this
    # becomes the dominant signal; for a Cloud-focused candidate it stays
    # modest so AWS-only ML platform roles don't crowd out infra roles.
    from services.resume_profiler import INTENTS as _INTENTS
    ml_match = any(re.compile(p, re.IGNORECASE).search(title)
                   for p in _INTENTS["ai_ml"]["title_terms"])
    if ml_match:
        bump = _title_bonus(True, "ai_ml", 18); s += bump; flags.append(f"AI/ML title (+{bump})")

    # Domain mention in description (catch jobs with vague titles like
    # "Software Engineer" but ML-heavy descriptions for an ML candidate).
    if profile:
        try:
            from services.resume_profiler import domain_relevance as _domain_relevance
            dr = _domain_relevance(profile, f"{title} {description}")
            if dr >= 0.5:
                s += 10; flags.append(f"On-domain JD ({dr:.2f})")
            elif dr <= 0.1 and not (CLOUD_TITLE.search(title) or BACKEND_TITLE.search(title)
                                    or FULLSTACK_TITLE.search(title) or FRONTEND_TITLE.search(title)
                                    or ml_match or GOOD_TITLE.search(title)):
                # Title doesn't match any family AND description has no domain
                # signal — likely off-domain noise (e.g. a generic "Engineer"
                # at a hospital). Drop hard to push it below the Tier-3 floor.
                s -= 18; flags.append("off-domain")
        except Exception:
            pass

    try:
        apc = int(rec["applicants"]) if rec["applicants"] else None
        if apc is not None and apc < 30:
            s += 8; flags.append(f"<30 apps ({apc})")
    except (ValueError, TypeError):
        pass
    # Fresh-post bonus when the post is dated today.
    if today_iso and rec.get("posted") == today_iso:
        s += 5; flags.append("Fresh today")
    if SENIORITY_BAD.search(title):       s -= 25; flags.append("senior-ish")
    # Catch roman numerals II/III/IV AND arabic 2/3/4 in titles like
    # "SDE 2", "SWE 3", "Software Engineer 2". Allow "I" and "1" through
    # because those are early-career.
    if re.search(r"\b(II|III|IV)\b|\b(SDE|SWE|Software Engineer)\s*[234]\b", title, re.IGNORECASE):
        s -= 25; flags.append("level II+")
    if CLEARANCE_TEXT.search(title):      s -= 40; flags.append("clearance")
    return s, flags


def _opt_status(rec: dict) -> str:
    return ("VERIFY-DATES (intern - confirm dates)"
            if INTERN_TITLE.search(rec["title"]) else "FTE OK")


def _tier(s: int) -> str:
    if s >= 100: return "Tier 1"
    if s >= 80:  return "Tier 2"
    if s >= 60:  return "Tier 3"
    return "Skip"


def _filter_and_score(
    records: List[dict],
    cutoff: str,
    custom_role_terms: Optional[List[str]] = None,
    profile: Optional[Dict[str, Any]] = None,
    *,
    location_filter: str = "United States",
    experience_level: str = "entry",
    employment_type: str = "FULLTIME",
    work_arrangement: str = "any",
    domain_strict: bool = False,
    h1b_only: bool = False,
    exclude_no_sponsorship: bool = False,
    applied_index: Optional[Dict[Tuple[str, str], Dict[str, Any]]] = None,
    past_days: int = 1,
):
    apply_now: List[dict] = []
    verify: List[dict] = []
    excluded: List[dict] = []
    today_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Cross-source dedupe — when LinkedIn and Workday both surface the same role
    # at the same company, keep one canonical copy. Prefer the entry that has a
    # parseable date or a non-empty description (more useful for tailoring).
    seen: Dict[Tuple[str, str], dict] = {}
    for r in records:
        key = (
            _norm_company(r.get("company") or ""),
            (r.get("title") or "").strip().lower(),
        )
        if not key[0] or not key[1]:
            # Anything missing both fields gets a synthetic key so it isn't
            # collapsed with other empty-field rows.
            seen[(r.get("source", ""), r.get("url") or id(r))] = r
            continue
        existing = seen.get(key)
        if existing is None:
            seen[key] = r
        else:
            # Choose whichever copy is "richer".
            def _quality(x: dict) -> int:
                q = 0
                if x.get("posted"): q += 2
                if (x.get("description") or "").strip(): q += 1
                if x.get("url"): q += 1
                return q
            if _quality(r) > _quality(existing):
                seen[key] = r
    deduped = list(seen.values())

    # Pre-compute domain-strict matchers ONCE if domain_strict is enabled,
    # so per-record filtering is cheap regex .search() calls. We track the
    # intent each pattern came from so when a row matches strict mode, we
    # can synthesize a role-family label downstream — otherwise broad-but-
    # in-domain titles like "Applied Scientist" or "MLOps Engineer" would
    # pass strict mode but get dropped by the legacy _role_match regexes.
    strict_patterns: List[Tuple[Any, str]] = []
    strict_intent_labels: Dict[str, str] = {}
    if domain_strict and profile:
        try:
            from services.resume_profiler import INTENTS as _INTENTS_STRICT
            primary_intent = profile.get("primary_intent")
            secondary_intent = profile.get("secondary_intent")
            for intent_key in (primary_intent, secondary_intent):
                if not intent_key or intent_key not in _INTENTS_STRICT:
                    continue
                spec = _INTENTS_STRICT[intent_key]
                strict_intent_labels[intent_key] = spec.get("label", intent_key)
                for pat in spec.get("title_terms", []):
                    try:
                        strict_patterns.append((re.compile(pat, re.IGNORECASE), intent_key))
                    except re.error:
                        continue
        except Exception:
            strict_patterns = []

    # Location post-filter — when caller specified something narrower than
    # "United States" (e.g. "New York", "Phoenix"), drop rows whose location
    # doesn't mention any token from the filter. We split on commas/spaces
    # and require at least one substring to match — Workday/LinkedIn return
    # location as "City, State, Country" strings.
    location_tokens: List[str] = []
    if location_filter:
        loc_lower = location_filter.strip().lower()
        if loc_lower not in ("", "united states", "usa", "us", "remote"):
            for token in re.split(r"[,/\s]+", loc_lower):
                token = token.strip()
                if len(token) >= 3:
                    location_tokens.append(token)

    # Work-arrangement post-filter — actors filter best-effort; we double-check
    # so a "remote-only" pipeline run doesn't quietly include hybrid roles.
    arrangement = (work_arrangement or "any").lower()

    # Employment-type post-filter — same reason.
    emp_type = (employment_type or "").upper()

    # Experience-level hard-drop. Previously the scorer subtracted 25 points
    # for SENIORITY_BAD matches but kept the row, so "Distinguished Product
    # Software Engineer" / "Senior AI Engineer" surfaced in Tier 3 with a
    # "senior-ish" flag even when the user picked entry-level. Workday Direct
    # rows arrive description-less from CXS list-view, so the title is the
    # only signal we have for them — but in practice senior-level postings
    # almost always announce themselves in the title (Senior / Staff / Lead /
    # Principal / Distinguished / Manager / Architect), which is exactly what
    # SENIORITY_BAD catches. We also drop when GOOD_TITLE is missing AND the
    # title contains an explicit non-entry numeric (e.g. "II/III" suffix on a
    # role series).
    requires_entry = (experience_level or "").lower() in {"entry", "internship", "associate", "new_grad"}
    _NON_ENTRY_SUFFIX = re.compile(r"\b(engineer|developer|swe|sde)\s+(?:ii|iii|iv|2|3|4)\b", re.IGNORECASE)
    # Minimum-years-of-experience matchers. We accept the FIRST integer they
    # quote (so "4-6 years" → 4, "5+ years" → 5) and drop the row when that
    # floor is >= ENTRY_YEARS_MAX. ENTRY_YEARS_MAX = 4 because most
    # legitimate entry roles cap their phrasing at "0-3 years" or "2+ years";
    # once a JD says "4+ years" it's targeting mid-level and above.
    _MIN_YEARS_PATTERNS = (
        re.compile(r"\b(\d+)\s*\+\s*years?\s+(?:of\s+)?(?:professional\s+|relevant\s+|industry\s+)?experience", re.IGNORECASE),
        re.compile(r"\bminimum\s+(?:of\s+)?(\d+)\s+years?", re.IGNORECASE),
        re.compile(r"\bat\s+least\s+(\d+)\s+years?", re.IGNORECASE),
        re.compile(r"\b(\d+)\s*-\s*\d+\s+years?\s+(?:of\s+)?experience", re.IGNORECASE),
        re.compile(r"\b(\d+)\+\s+years?\s+(?:of\s+)?(?:hands[-\s]?on|industry|software|engineering)", re.IGNORECASE),
    )
    ENTRY_YEARS_MAX = 4

    def _min_years_required(text: str) -> int:
        """Return the lowest 'N+ years' the JD demands, or 0 if none stated."""
        best = 0
        for rx in _MIN_YEARS_PATTERNS:
            for m in rx.finditer(text or ""):
                try:
                    n = int(m.group(1))
                except (TypeError, ValueError):
                    continue
                if n > best:
                    best = n
        return best

    for r in deduped:
        # Only drop on date when we *know* it's older than the cutoff.
        # Workday entries often have no parseable date_posted; the actor
        # already filtered by recency on its side, so trusting them is safer
        # than dropping them all to "older than cutoff".
        posted = (r.get("posted") or "").strip()
        if posted and re.match(r"^\d{4}-\d{2}-\d{2}$", posted) and posted < cutoff:
            excluded.append({**r, "reason": f"older than {cutoff} (posted {posted})",
                             "tier": "Skip", "score": 0, "flags": ""}); continue
        if not _is_us(r):
            excluded.append({**r, "reason": f"non-US ({r['location']})",
                             "tier": "Skip", "score": 0, "flags": ""}); continue

        # User-specified location filter (post-filter — actors may return wider sets).
        if location_tokens:
            loc_text = (r.get("location") or "").lower()
            if not any(tok in loc_text for tok in location_tokens) and "remote" not in loc_text:
                excluded.append({**r, "reason": f"outside location filter ({location_filter})",
                                 "tier": "Skip", "score": 0, "flags": ""}); continue

        # Work-arrangement post-filter (only when user picked a specific mode).
        if arrangement in ("remote", "hybrid", "onsite"):
            loc_text = (r.get("location") or "").lower()
            desc_text = (r.get("description") or "").lower()
            blob = f"{loc_text} {desc_text}"
            if arrangement == "remote":
                if "remote" not in blob:
                    excluded.append({**r, "reason": "not remote",
                                     "tier": "Skip", "score": 0, "flags": ""}); continue
            elif arrangement == "hybrid":
                if "hybrid" not in blob:
                    excluded.append({**r, "reason": "not hybrid",
                                     "tier": "Skip", "score": 0, "flags": ""}); continue
            elif arrangement == "onsite":
                if "remote" in blob or "hybrid" in blob:
                    excluded.append({**r, "reason": "not on-site",
                                     "tier": "Skip", "score": 0, "flags": ""}); continue

        # Experience-level hard-drop (only when user opted into a tight
        # bracket). Senior title → drop. "Engineer II/III" without a
        # new-grad phrase → drop. JD that demands 4+ years experience → drop.
        # Otherwise keep — the scorer still de-ranks junior-ambiguous rows
        # below confirmed-entry titles.
        if requires_entry:
            title_only = (r.get("title") or "")
            if SENIORITY_BAD.search(title_only):
                excluded.append({**r, "reason": f"senior title ({title_only.strip()})",
                                 "tier": "Skip", "score": 0, "flags": ""}); continue
            if _NON_ENTRY_SUFFIX.search(title_only) and not GOOD_TITLE.search(title_only):
                excluded.append({**r, "reason": f"mid/senior numeric tier ({title_only.strip()})",
                                 "tier": "Skip", "score": 0, "flags": ""}); continue
            # JD-based years floor — only meaningful when we actually have a
            # description (Workday CXS list-view rows have none, so they
            # pass this check on title alone). Catches things like
            # "Requires 6+ years of experience" / "Minimum 8 years".
            desc_text = r.get("description") or ""
            if desc_text:
                min_yrs = _min_years_required(desc_text)
                if min_yrs >= ENTRY_YEARS_MAX:
                    excluded.append({**r, "reason": f"JD requires {min_yrs}+ years",
                                     "tier": "Skip", "score": 0, "flags": ""}); continue

        # Employment-type post-filter — primarily catches Contract postings
        # leaking through when user wants Full-Time only. "any" / empty skips.
        if emp_type and emp_type != "ANY":
            t_blob = f"{(r.get('title') or '').lower()} {(r.get('description') or '').lower()}"
            if emp_type == "FULLTIME":
                if any(k in t_blob for k in ["contract", "contractor", "1099", "c2c", "corp-to-corp"]):
                    excluded.append({**r, "reason": "contract role (full-time filter)",
                                     "tier": "Skip", "score": 0, "flags": ""}); continue
            elif emp_type == "CONTRACTOR":
                if not any(k in t_blob for k in ["contract", "contractor", "consultant", "1099"]):
                    excluded.append({**r, "reason": "not a contract role",
                                     "tier": "Skip", "score": 0, "flags": ""}); continue
            elif emp_type == "INTERN":
                # Word-boundary check on the TITLE only — substring 'intern' in
                # 'internal' / 'international' was promoting full-time roles
                # into the intern bucket. Description-level mentions are too
                # noisy to count toward this filter.
                if not INTERN_TITLE.search(r.get("title") or ""):
                    excluded.append({**r, "reason": "not an internship",
                                     "tier": "Skip", "score": 0, "flags": ""}); continue

        # Domain-strict post-filter — drop anything whose title doesn't match
        # the candidate's primary or secondary intent regex. The candidate
        # explicitly opted into "specialist mode" via the UI toggle; off-domain
        # roles (even high-scoring ones) get cut before scoring. When we DO
        # match, remember which intent matched so we can synthesize a role
        # family below — otherwise titles like "Applied Scientist" pass strict
        # mode but get killed by the narrower legacy _role_match regex.
        strict_matched_intent: Optional[str] = None
        if strict_patterns:
            t_lower = (r.get("title") or "").lower()
            for rx, intent_key in strict_patterns:
                if rx.search(t_lower):
                    strict_matched_intent = intent_key
                    break
            if not strict_matched_intent:
                excluded.append({**r, "reason": "off-domain (strict mode)",
                                 "tier": "Skip", "score": 0, "flags": ""}); continue

        # F-1 / H-1B sponsorship filters — opt-in. Off by default so first-time
        # users (and anyone not on a visa) keep the existing flow unchanged.
        visa_status, visa_conf = _classify_visa_with_confidence(r)
        r["visa_status"] = visa_status
        # Surface the calibrated probability so the UI badge can render
        # "Likely sponsor 62%" instead of a binary verdict.
        r["visa_confidence"] = visa_conf

        # Tag rows the user already engaged with (saved/applied/interviewing).
        # We don't drop these — the user might want to escalate ("apply again
        # to the SRE role at the same company") — but we do mark them so the
        # UI can show an "Already applied" badge.
        if applied_index:
            ai_key = (
                _norm_company(r.get("company") or "").strip(),
                (r.get("title") or "").strip().lower(),
            )
            ai_hit = applied_index.get(ai_key)
            if ai_hit:
                r["previously_applied_status"] = ai_hit.get("status")
                r["previously_applied_at"] = ai_hit.get("ts") or ""
        if exclude_no_sponsorship and visa_status == "no_sponsorship":
            excluded.append({**r, "reason": "JD says no visa sponsorship",
                             "tier": "Skip", "score": 0, "flags": ""}); continue
        if h1b_only and visa_status != "sponsor_verified":
            excluded.append({**r, "reason": "not a verified H-1B sponsor",
                             "tier": "Skip", "score": 0, "flags": ""}); continue
        if (why := _healthcare_noise(r)):
            excluded.append({**r, "reason": why, "tier": "Skip", "score": 0, "flags": ""}); continue
        if (why := _blocked_company(r)):
            excluded.append({**r, "reason": why, "tier": "Skip", "score": 0, "flags": ""}); continue
        if (why := _ghost_job_reason(r)):
            excluded.append({**r, "reason": why, "tier": "Skip", "score": 0, "flags": ""}); continue
        if (why := _clearance_block(r)):
            excluded.append({**r, "reason": why, "tier": "Skip", "score": 0, "flags": ""}); continue
        if (why := _summer_intern_block(r)):
            excluded.append({**r, "reason": why, "tier": "Skip", "score": 0, "flags": ""}); continue
        if (why := _opt_body_shop_reason(r)):
            excluded.append({**r, "reason": why, "tier": "Skip", "score": 0, "flags": ""}); continue
        roles = _role_match(r, custom_role_terms=custom_role_terms)
        # If domain-strict already matched, trust that match and synthesize
        # a role family from the intent label so "Applied Scientist" etc.
        # don't fall off the end — the strict regex IS the user's preferred
        # role definition for this run.
        if not roles and strict_matched_intent:
            roles = [strict_intent_labels.get(strict_matched_intent, strict_matched_intent)]
        if not roles:
            excluded.append({**r, "reason": "no role-family match",
                             "tier": "Skip", "score": 0, "flags": ""}); continue

        sc, flags = _score(r, today_iso=today_iso, profile=profile)
        rec = {**r, "score": sc, "tier": _tier(sc),
               "flags": ", ".join(flags) or "—",
               "roles": ", ".join(roles), "opt": _opt_status(r)}
        if sc < 60:
            excluded.append({**rec, "reason": f"score {sc} < 60"})
        elif INTERN_TITLE.search(r.get("title") or ""):
            # Word-boundary regex (not substring) — previously "Senior Internal
            # Tools Engineer" and "International Backend Engineer" got demoted
            # to verify_dates because "intern" is a substring of internal /
            # international. Real intern/co-op postings still route here.
            verify.append(rec)
        else:
            # Past-N-days strict mode: when the user asked for a narrow window
            # (≤3 days), rows whose date we couldn't parse get demoted to
            # verify_dates instead of trusted as apply_now. Previously they
            # silently passed through, which is why "past 3 days" sometimes
            # surfaced 2-week-old postings from sources without dates.
            posted = (r.get("posted") or "").strip()
            if past_days <= 3 and not re.match(r"^\d{4}-\d{2}-\d{2}$", posted):
                rec_no_date = {**rec, "flags": ((rec.get("flags") or "—") + ", no-date").strip(", ")}
                verify.append(rec_no_date)
            else:
                apply_now.append(rec)

    apply_now.sort(key=lambda x: -x["score"])
    verify.sort(key=lambda x: -x["score"])
    return apply_now, verify, excluded


def _cap_per_company(
    apply_now: List[dict],
    verify: List[dict],
    excluded: List[dict],
    max_per_company: int,
) -> Tuple[List[dict], List[dict], List[dict]]:
    """Cap the number of apply_now rows per company.

    A single sponsor like Stripe was flooding the daily feed (10-15 of 40
    rows). After scoring, we keep the top-N highest-scoring rows per company
    in apply_now; the rest are demoted to verify_dates with a "company-cap"
    reason so the user can still see them on demand but they don't crowd
    the primary triage list.

    Implemented after _filter_and_score so the cap operates on the FINAL
    scored list — we always keep a company's best matches, never random ones.
    """
    if max_per_company <= 0 or not apply_now:
        return apply_now, verify, excluded
    seen: Dict[str, int] = {}
    kept: List[dict] = []
    demoted: List[dict] = []
    # apply_now is already sorted by descending score
    for rec in apply_now:
        company_key = _norm_company(rec.get("company") or "").strip()
        if not company_key:
            kept.append(rec)
            continue
        count = seen.get(company_key, 0)
        if count < max_per_company:
            seen[company_key] = count + 1
            kept.append(rec)
        else:
            demoted.append({
                **rec,
                "tier": rec.get("tier", "Tier 2"),
                "flags": (rec.get("flags") or "").strip(),
                "reason": f"company-cap (>{max_per_company} {rec.get('company') or 'this company'})",
            })
    if demoted:
        # Insert before existing verify rows but after the highest-score
        # apply_now demotions so the user sees them first when expanding.
        verify = demoted + verify
        logger.info("[company-cap] demoted %d rows beyond cap=%d", len(demoted), max_per_company)
    return kept, verify, excluded


def _drop_hidden_companies(
    rows: List[dict],
    hide_companies: List[str],
) -> Tuple[List[dict], List[dict]]:
    """Split rows into (kept, hidden) by exact-or-substring company match."""
    if not hide_companies:
        return rows, []
    norms = [(_norm_company(h) or "").strip().lower() for h in hide_companies if h and h.strip()]
    norms = [n for n in norms if n]
    if not norms:
        return rows, []
    kept: List[dict] = []
    hidden: List[dict] = []
    for r in rows:
        c = (_norm_company(r.get("company") or "") or "").strip().lower()
        if any(n == c or (n and n in c) for n in norms):
            hidden.append({**r, "reason": f"hidden by user ({r.get('company') or 'company'})",
                           "tier": "Skip", "score": 0, "flags": ""})
        else:
            kept.append(r)
    return kept, hidden


def _drop_hidden_titles(
    rows: List[dict],
    hide_title_patterns: List[str],
) -> Tuple[List[dict], List[dict]]:
    """Split rows by case-insensitive substring match against title patterns.

    Mirrors _drop_hidden_companies but for title text. Used when the user
    clicks "Hide titles matching 'Senior'" to suppress mid/senior listings
    that don't fit their level — without dropping the whole company.
    """
    if not hide_title_patterns:
        return rows, []
    norms = [str(p or "").strip().lower() for p in hide_title_patterns]
    norms = [n for n in norms if n]
    if not norms:
        return rows, []
    kept: List[dict] = []
    hidden: List[dict] = []
    for r in rows:
        t = (str(r.get("title") or "")).strip().lower()
        matched = next((n for n in norms if n and n in t), None)
        if matched:
            hidden.append({**r, "reason": f"hidden by user (title contains '{matched}')",
                           "tier": "Skip", "score": 0, "flags": ""})
        else:
            kept.append(r)
    return kept, hidden


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------
def run_pipeline(
    linkedin_keywords: Optional[List[str]] = None,
    workday_titles: Optional[List[str]] = None,
    past_days: int = 1,
    custom_role_terms: Optional[List[str]] = None,
    linkedin_count: int = 80,
    workday_limit: int = 200,
    apify_token: Optional[str] = None,
    include_indeed: bool = False,
    user_email: Optional[str] = None,
    location: str = "United States",
    experience_level: str = "entry",
    employment_type: str = "FULLTIME",
    work_arrangement: str = "any",
    domain_strict: bool = False,
    h1b_only: bool = False,
    exclude_no_sponsorship: bool = False,
    hide_companies: Optional[List[str]] = None,
    hide_title_patterns: Optional[List[str]] = None,
    max_per_company: int = 4,
) -> Dict[str, Any]:
    """Run the full daily pipeline and return tier-grouped JSON.

    apify_token: when provided (e.g. a per-user BYO key), it overrides the
    shared SSM/env token. None falls back to _get_apify_token().
    """
    token = (apify_token or "").strip() or _get_apify_token()
    if not token:
        raise RuntimeError("APIFY_API_KEY/APIFY_TOKEN is not configured")

    li_kw = [k.strip() for k in (linkedin_keywords or DEFAULT_LINKEDIN_KEYWORD_SETS) if k and k.strip()]
    wd_titles = [t.strip() for t in (workday_titles or DEFAULT_WORKDAY_TITLES) if t and t.strip()]
    # past_days=0 means "today only" — cutoff is today's date, so anything
    # posted before today is excluded.
    past_days = max(0, min(int(past_days if past_days is not None else 1), 30))

    today = datetime.now(timezone.utc)
    cutoff = (today - timedelta(days=past_days)).strftime("%Y-%m-%d")

    # Run Apify scrapes (LinkedIn+Workday, optional Indeed) and direct-ATS in
    # parallel. ATS APIs have no Apify dependency so they keep delivering even
    # when the Apify token is missing or an actor is rate-limited. Indeed is
    # opt-in (include_indeed=True) — its date reliability is poor and its
    # listings are dominated by body-shop reposts. Every source fetches at the
    # widest sensible window; the user's filters (experience_level,
    # employment_type, work_arrangement, location, h1b_only, ...) apply once
    # downstream in _filter_and_score on the merged set.
    ats_enabled = True
    indeed_enabled = include_indeed

    with ThreadPoolExecutor(max_workers=4) as pool:
        f_apify = pool.submit(
            _scrape_in_parallel,
            token=token,
            linkedin_keywords=li_kw or DEFAULT_LINKEDIN_KEYWORD_SETS,
            workday_titles=wd_titles or DEFAULT_WORKDAY_TITLES,
            past_days=past_days,
            linkedin_count=linkedin_count,
            workday_limit=workday_limit,
            location=location,
        )
        f_indeed = pool.submit(
            _scrape_indeed, token,
            past_days=past_days,
            # Indeed actor accepts a coarse experience filter but we honor
            # the "wide fetch / post-filter" pattern: fetch every level.
            experience_level="any",
            employment_type="",
            location=location,
        ) if indeed_enabled else None
        f_ats = pool.submit(_scrape_ats_direct) if ats_enabled else None
        # Direct Workday CXS — free, no Apify, covers tenants the Apify
        # actor's index misses (Yahoo / Adobe / NVIDIA / Mastercard / ...).
        # This is the whole reason the direct fetcher exists; without it
        # the pipeline would silently lose Yahoo's postings whenever the
        # Apify Workday actor's daily index didn't include them.
        f_wd_direct = pool.submit(
            _scrape_workday_direct,
            search_text="software engineer",
            location=location,
        )
        linkedin_items, workday_items, errors, actor_diagnostics = f_apify.result()
        indeed_items = f_indeed.result() if f_indeed is not None else []
        ats_items = f_ats.result() if f_ats is not None else []
        wd_direct_items = f_wd_direct.result()

    # Source-4 fallback: only fire for companies that returned 0 postings from
    # Source 5 (direct API). When APIFY_ATS_ACTOR is unset, _ats_apify_fallback
    # returns []. Still gated by the existing Apify token. Skipped entirely
    # when the user muted the ATS source for this run.
    if ats_enabled and token and _ats_apify_actor_id():
        def _missing(slugs: List[Tuple[str, str, str]], label: str) -> List[Tuple[str, str, str]]:
            seen = {(r.get("company") or "").strip().lower() for r in ats_items if r.get("source") == label}
            return [t for t in slugs if t[1].lower() not in seen]

        fallback_jobs: List[Dict[str, Any]] = []
        for slugs, ats_type, label in (
            (DEFAULT_GREENHOUSE_SLUGS, "greenhouse", "Greenhouse"),
            (DEFAULT_LEVER_SLUGS, "lever", "Lever"),
            (DEFAULT_ASHBY_SLUGS, "ashby", "Ashby"),
        ):
            missing = _missing(slugs, label)
            if missing:
                fallback_jobs.extend(_ats_apify_fallback(missing, ats_type, token))
        if fallback_jobs:
            logger.info("[ats-direct] Apify fallback added %d postings", len(fallback_jobs))
            ats_items.extend(fallback_jobs)

    raw_li = [_linkedin_to_record(x) for x in linkedin_items]
    raw_wd = [_workday_to_record(x) for x in workday_items]
    raw_in = indeed_items   # already normalized by _scrape_indeed
    raw_ats = ats_items     # already normalized by the ATS fetchers
    raw_wd_direct = [_workday_direct_to_record(x) for x in wd_direct_items]
    all_raw = raw_li + raw_wd + raw_in + raw_ats + raw_wd_direct

    # Resume profile (intent + weighted skills) — pulled from the user's
    # stored resume + recent feedback signals. None for anonymous runs;
    # _score falls back to its legacy flat bonuses when profile is None.
    profile = _load_user_profile(user_email) if user_email else None

    # Already-applied index — lets the row tagger mark rows the user has
    # already engaged with so we don't surface the same Microsoft SWE
    # posting fresh every day after the user opened it.
    applied_index = _load_applied_index(user_email) if user_email else {}

    # User-controlled hide list — drop entire companies the user dismissed
    # (e.g. repeated Stripe rows that don't sponsor). Applied BEFORE scoring
    # so they don't waste compute on the heavy filter pipeline.
    all_raw, user_hidden = _drop_hidden_companies(all_raw, hide_companies or [])
    # Same idea for title patterns ("Senior", "Lead", "Staff") so a new-grad
    # candidate can suppress level-mismatched roles without blocking the
    # whole company.
    all_raw, title_hidden = _drop_hidden_titles(all_raw, hide_title_patterns or [])
    user_hidden = user_hidden + title_hidden

    apply_now, verify, excluded = _filter_and_score(
        all_raw,
        cutoff,
        custom_role_terms=custom_role_terms,
        profile=profile,
        location_filter=location,
        experience_level=experience_level,
        employment_type=employment_type,
        work_arrangement=work_arrangement,
        domain_strict=domain_strict,
        h1b_only=h1b_only,
        exclude_no_sponsorship=exclude_no_sponsorship,
        applied_index=applied_index,
        past_days=past_days,
    )
    # Per-company cap — prevents single sponsors (Stripe was the worst
    # offender) from flooding the daily triage list with 10+ rows. Extras
    # get demoted to verify_dates so the user can still see them on demand.
    apply_now, verify, excluded = _cap_per_company(
        apply_now, verify, excluded, max_per_company=max_per_company,
    )
    excluded.extend(user_hidden)
    duplicates_dropped = max(0, len(all_raw) + len(user_hidden) - (len(apply_now) + len(verify) + len(excluded)))

    tier_counts = {
        "tier_1": sum(1 for r in apply_now + verify if r["tier"] == "Tier 1"),
        "tier_2": sum(1 for r in apply_now + verify if r["tier"] == "Tier 2"),
        "tier_3": sum(1 for r in apply_now + verify if r["tier"] == "Tier 3"),
    }

    def _by_src(rows: List[dict], src: str) -> int:
        return sum(1 for r in rows if (r.get("source") or "").lower() == src.lower())

    _ATS_SOURCES = {
        "Greenhouse", "Lever", "Ashby",
        "Greenhouse (Apify)", "Lever (Apify)", "Ashby (Apify)",
    }

    def _ats_count(rows: List[dict]) -> int:
        return sum(1 for r in rows if (r.get("source") or "") in _ATS_SOURCES)

    source_breakdown = {
        "linkedin": {
            "raw": len(raw_li),
            "apply_now": _by_src(apply_now, "LinkedIn"),
            "verify": _by_src(verify, "LinkedIn"),
            "excluded": _by_src(excluded, "LinkedIn"),
        },
        "workday": {
            "raw": len(raw_wd),
            "apply_now": _by_src(apply_now, "Workday"),
            "verify": _by_src(verify, "Workday"),
            "excluded": _by_src(excluded, "Workday"),
        },
        "workday_direct": {
            "raw": len(raw_wd_direct),
            "apply_now": _by_src(apply_now, "Workday Direct"),
            "verify": _by_src(verify, "Workday Direct"),
            "excluded": _by_src(excluded, "Workday Direct"),
        },
        "indeed": {
            "raw": len(raw_in),
            "apply_now": _by_src(apply_now, "Indeed"),
            "verify": _by_src(verify, "Indeed"),
            "excluded": _by_src(excluded, "Indeed"),
        },
        "ats_direct": {
            "raw": len(raw_ats),
            "apply_now": _ats_count(apply_now),
            "verify": _ats_count(verify),
            "excluded": _ats_count(excluded),
        },
    }

    credits_exhausted = any("APIFY_CREDITS_EXHAUSTED" in (e or "") for e in errors)

    return {
        "ok": True,
        "generated_at": today.isoformat(),
        "credits_exhausted": credits_exhausted,
        "past_days": past_days,
        "cutoff": cutoff,
        "raw_counts": {
            "linkedin": len(raw_li),
            "workday": len(raw_wd),
            "workday_direct": len(raw_wd_direct),
            "indeed": len(raw_in),
            "ats_direct": len(raw_ats),
            "total": len(all_raw),
            "duplicates_dropped": duplicates_dropped,
        },
        "tier_counts": tier_counts,
        "source_breakdown": source_breakdown,
        "totals": {
            "apply_now": len(apply_now),
            "verify_dates": len(verify),
            "excluded": len(excluded),
        },
        "apply_now": apply_now,
        "verify_dates": verify,
        # Trim excluded to avoid huge payloads.
        "excluded_sample": excluded[:50],
        "excluded_total": len(excluded),
        "errors": errors,
        "inputs": {
            "linkedin_keywords": li_kw,
            "workday_titles": wd_titles[:20] + (["..."] if len(wd_titles) > 20 else []),
            "custom_role_terms": custom_role_terms or [],
        },
        # Actor-level diagnostics — surface the URLs we sent to LinkedIn and
        # whether each actor came back silently empty (no items + no error)
        # so the UI can render hints like "0 results — try widening past
        # days or unchecking strict filters".
        "actor_diagnostics": actor_diagnostics,
    }
