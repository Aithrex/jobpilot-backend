"""
Workday Jobs service — powers the dedicated "Workday Jobs" tab.

Fans out across the curated, live-validated tenant catalog
(services/workday_tenant_catalog.py) hitting each tenant's public CXS JSON
endpoint directly:

    POST https://{tenant}.{wd_host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs

No scraping, no Apify credits — the CXS API is the same JSON the public
career site renders from. Behavior notes confirmed by live probing:

  - With a `searchText` the response is relevance-sorted; without one it is
    newest-first. We search per derived term so a huge retail tenant's
    store-associate firehose doesn't bury the user's engineering titles.
  - `postedOn` is a relative phrase ("Posted Today", "Posted Yesterday",
    "Posted 2 Days Ago", "Posted 30+ Days Ago") — we convert to an absolute
    date and compute days_ago so the UI can filter Today/24h/3d/7d/30d
    client-side without a refetch.
  - Wrong site → 404, wrong host/tenant → 422. The catalog is pre-validated
    (scripts/validate_workday_tenants.py) so no runtime probing is needed.

Caching mirrors job_service's two-tier shape: we always fetch the widest
window (30 days) and cache that raw set in Mongo; recency filters are applied
downstream (client-side in the tab), so switching Today ↔ 30d is instant.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from threading import Lock
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

from services.domain_classifier import ROLE_NOUNS
from services.workday_cxs_fetcher import _HEADERS, _approx_iso_from_relative
from services.workday_tenant_catalog import WORKDAY_TENANT_CATALOG, INDUSTRIES

logger = logging.getLogger(__name__)


def _build_match_regex(skill_lower: str) -> Optional[re.Pattern]:
    """Compile a word-boundary regex for a single skill token.

    Identical semantics to services/jobs/services/jd_match_scorer.py::
    _build_match_regex — duplicated here (not imported) because that module
    is jobs-Lambda-local and this service runs in the RESUME Lambda's async
    job processor; a shared-layer service must never import a domain-local
    one. Defined at module load, NOT lazily inside the search: a lazy import
    that runs after the fan-out can fail if anything (e.g. FD pressure)
    breaks file access mid-invocation.
    """
    if not skill_lower:
        return None
    if len(skill_lower) <= 2:
        # "Go" / "R" / "C" — too short for word-boundary; require surrounding
        # whitespace or punctuation to avoid matching "Google", "RuntimeError".
        return re.compile(r"(?:^|[\s.,;:/()\[\]{}])" + re.escape(skill_lower) + r"(?:$|[\s.,;:/()\[\]{}])",
                          re.IGNORECASE)
    return re.compile(r"(?<![A-Za-z0-9])" + re.escape(skill_lower) + r"(?![A-Za-z0-9])", re.IGNORECASE)


def _env_int(name: str, default: int) -> int:
    """Optional env override for fan-out knobs — never a hard requirement."""
    try:
        return max(1, int(os.environ.get(name, "") or default))
    except (TypeError, ValueError):
        return default


_IN_LAMBDA = bool(os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))

# Bump when the record schema or fetch behavior changes so stale cached
# results don't leak the old shape into the UI. v7: seniority + tailor
# feasibility fields, 4 title terms + 2 skill terms, 300-row enrichment.
RESULT_SCHEMA_VERSION = 7

# Fan-out envelope — tuned against a live full-catalog run (532 tenants x
# 3 terms, 2026-07): 1 page/term is enough because the US facet + relevance
# ranking put the best matches on page 1 and PER_TENANT_KEEP caps the take
# anyway; worker load spreads across ~532 distinct hosts (per-host
# concurrency stays ~1). The CXS API rejects limit > 20.
#
# Lambda sizing (this repo): the async runner is the RESUME Lambda with a
# 300s timeout (infra/terraform/variables.tf service_config.resume), NOT the
# legacy 900s monolith — so the fan-out cap must leave room for detail
# enrichment + scoring + cache writes inside 300s. Defaults: 32 workers /
# 210s cap under Lambda (env-overridable via WORKDAY_MAX_WORKERS /
# WORKDAY_OVERALL_TIMEOUT); the legacy 96/600 stays for local dev where
# there is no invoke timeout. A fan-out timeout degrades gracefully to a
# partial result — never a hard error.
PAGE_LIMIT = 20
MAX_PAGES_PER_TERM = 1
MAX_QUERY_TERMS = 4       # title-family terms (multi-domain resumes need 4)
TOTAL_TERMS_CAP = 6       # title terms + resume-skill terms combined
PER_TENANT_KEEP = 30
GLOBAL_KEEP = 1500
REQUEST_TIMEOUT = 6
OVERALL_TIMEOUT = _env_int("WORKDAY_OVERALL_TIMEOUT", 210 if _IN_LAMBDA else 600)
MAX_WORKERS = _env_int("WORKDAY_MAX_WORKERS", 32 if _IN_LAMBDA else 96)
WINDOW_DAYS = 30
# Top-ranked rows get their full JD fetched from the CXS detail endpoint so
# skill matching + tailor-feasibility run against the real description.
DETAIL_ENRICH_TOP = 300      # global top slice, whatever their age
DETAIL_ENRICH_RECENT = 400   # extra slice from the <=3d cohort (default view)
DETAIL_TIMEOUT = 5
# Stream a provisional top slice to the poller as companies complete, so the
# tab renders matches while the scan is still running.
STREAM_EVERY_TASKS = 80
STREAM_TOP = 120

# Shared session with HARD-CAPPED pooling. The fan-out touches ~532 distinct
# hosts; generous pools (256 hosts x 96 sockets) accumulated enough
# keep-alive sockets to exhaust Lambda's 1024-FD limit mid-run ("[Errno 24]
# Too many open files" — it even broke later imports). Keep-alive across
# hosts is worth little here (~6 requests/host), so cap to 32 host pools x
# 8 sockets: worst case ~256 pooled + ~96 in-flight FDs, far under the limit.
_SESSION = requests.Session()
_SESSION.mount("https://", requests.adapters.HTTPAdapter(
    pool_connections=32, pool_maxsize=8, max_retries=0,
))


def _drain_session_pools() -> None:
    """Close every pooled socket. Called after each search so a warm Lambda
    container never carries hundreds of idle sockets into the next run."""
    try:
        for adapter in _SESSION.adapters.values():
            adapter.poolmanager.clear()
    except Exception:
        pass

CACHE_COLLECTION = "workday_jobs_cache"
CACHE_TTL_SECONDS = 6 * 3600     # Mongo TTL reaper
CACHE_FRESH_SECONDS = 3 * 3600   # served-as-fresh horizon

# Seniority/stack noise words stripped when deriving compact CXS search terms
# from the user's (often long) title list. "Senior Backend Engineer Python"
# and "Backend Engineer II" both collapse to "backend engineer".
_TERM_NOISE = frozenset({
    "senior", "sr", "sr.", "junior", "jr", "jr.", "associate", "staff",
    "lead", "principal", "mid-level", "midlevel", "entry", "level",
    "new", "grad", "graduate", "intern", "internship", "i", "ii", "iii",
    "iv", "1", "2", "3",
})


def _derive_query_terms(titles: List[str], cap: int = MAX_QUERY_TERMS) -> List[str]:
    """Collapse a long title list into a few core CXS search terms.

    The fan-out cost is tenants x terms, so we can't send all 40 synthesized
    titles — but Workday's search is a keyword match, so the de-seniorized
    core families ("backend engineer", "data engineer") cover their variants.
    All original titles still participate in local title-match ranking.
    """
    seen: set = set()
    out: List[str] = []
    loose_seen: set = set()
    loose: List[str] = []          # every cleaned term, incl. lone modifiers
    for raw in titles or []:
        # Compound resume titles like "Cloud/DevOps Engineer (Associate
        # Data Scientist)" must yield SEPARATE terms ("cloud engineer",
        # "devops engineer", "data scientist"), not one mangled phrase.
        # First collapse an ampersand sitting BETWEEN single letters ("R&D",
        # "M&A") into one token so the & splitter below (and the [^a-z0-9…]
        # token split) don't shatter "R&D Engineer" into "r" + "d engineer".
        lowered = re.sub(r"\b([a-z])\s*&\s*([a-z])\b", r"\1\2", str(raw).lower())
        for seg in re.split(r"[/(),;&+]|\band\b", lowered):
            tokens = [t for t in re.split(r"[^a-z0-9+#.]+", seg) if t]
            core = [t for t in tokens if t not in _TERM_NOISE]
            if not core:
                continue
            term = " ".join(core[:4])
            if term not in loose_seen:
                loose_seen.add(term)
                loose.append(term)
            # A lone token joins the PRIMARY search set only if it's a real
            # role noun. ROLE_NOUNS spans tech AND non-tech professions (nurse,
            # electrician, accountant, teacher, …), so a single-word non-tech
            # title is kept. A lone MODIFIER ("cloud", left over from
            # "Cloud/DevOps Engineer") is too broad to search alongside real
            # role terms, so it's held back here — but still captured in
            # `loose` so it isn't lost if it's all the user gave us.
            if len(core) == 1 and core[0] not in ROLE_NOUNS:
                continue
            if term in seen:
                continue
            seen.add(term)
            out.append(term)
            if len(out) >= cap:
                return out
    # Prefer role-qualified terms. If none survived, fall back to the user's
    # OWN cleaned title tokens (so a bare "cloud" searches cloud roles) rather
    # than a tech-centric default; only use "software engineer" when the user
    # gave no usable title text at all.
    return out or loose[:cap] or ["software engineer"]


# Soft skills / ubiquitous tokens that would make terrible search terms —
# CXS relevance search returns SOMETHING for any query, so a generic term
# like "bash" floods the result with junk from every tenant.
_SKILL_TERM_STOPLIST = frozenset({
    "communication", "leadership", "teamwork", "problem solving", "agile",
    "scrum", "git", "github", "microsoft office", "excel", "english", "jira",
    "collaboration", "documentation", "testing", "debugging", "linux",
    "bash", "shell", "html", "css", "sql", "rest api", "apis", "windows",
})


def _derive_skill_terms(skills: List[str], title_terms: List[str],
                        cap: int) -> List[str]:
    """Broaden retrieval beyond title vocabulary.

    Workday's searchText matches the FULL posting text, so querying by a
    distinctive resume skill ("python", "kubernetes") surfaces roles whose
    titles never say "engineer" — e.g. "Member of Technical Staff". Those
    rows then earn a skill-signal boost in scoring instead of being missed
    entirely. Skills arrive in resume-prominence order from the parser.
    """
    if cap <= 0:
        return []
    title_blob = " ".join(title_terms).lower()
    out: List[str] = []
    for s in skills or []:
        tok = str(s).strip().lower()
        if len(tok) < 3 or tok in _SKILL_TERM_STOPLIST or tok in title_blob:
            continue
        if not re.fullmatch(r"[a-z0-9+#. /-]{3,30}", tok):
            continue
        out.append(tok)
        if len(out) >= cap:
            break
    return out


def _title_matchers(titles: List[str]) -> List[List[str]]:
    """Token lists for local title matching — a job title 'matches' a user
    title when every significant token of that user title appears in it."""
    matchers: List[List[str]] = []
    for raw in titles or []:
        tokens = [t for t in re.split(r"[^a-z0-9+#.]+", str(raw).lower()) if t]
        core = [t for t in tokens if t not in _TERM_NOISE]
        if core:
            matchers.append(core[:5])
    return matchers


def _role_matchers(titles: List[str]) -> List[List[str]]:
    """Matchers built from the DERIVED role phrases, not the raw titles.

    A raw compound resume title ("Cloud/DevOps Engineer (Associate Data
    Scientist)") tokenizes to [cloud, devops, engineer, associate, data] —
    no real job title contains all five, so the +45 title bonus never fired
    and every row flatlined at the +20 skill-signal score. The derived
    phrases ("devops engineer", "data scientist", "software developer")
    are what a job title can actually cover.
    """
    return _title_matchers(_derive_query_terms(titles, cap=12))


def _resume_family_weights(skills: List[str]) -> Tuple[set, set]:
    """(primary, all) stack families of the resume, weighted by skill count.

    A resume with 20 cloud/devops skills, 4 frontend and 3 python skills is
    a cloud-primary profile — cloud-centered JDs must outrank equally
    title-matched frontend JDs. Primary = families within 60% of the top
    family's skill count (min 3 skills).
    """
    counts: Dict[str, int] = {}
    for s in skills or []:
        for key, (_label, rx) in _STACK_FAMILIES.items():
            if rx.search(s):
                counts[key] = counts.get(key, 0) + 1
    if not counts:
        return set(), set()
    top = max(counts.values())
    primary = {k for k, n in counts.items() if n >= max(3, int(top * 0.6))}
    return primary, set(counts)


def _clamp_tz_offset(minutes: Any) -> int:
    """Sanitize a client-supplied UTC offset (minutes EAST of UTC, e.g. MST
    = -420). Clamped to the real-world range; anything unparseable -> 0."""
    try:
        return max(-14 * 60, min(14 * 60, int(minutes)))
    except (TypeError, ValueError):
        return 0


def _days_ago(iso_date: str, tz_offset_minutes: int = 0) -> Optional[int]:
    """Calendar-day age of a posting in the VIEWER's timezone.

    The old UTC-date diff made "Today" roll over at 5pm for a Mountain-time
    user: a job posted that same local morning counted as 1 day old the
    moment UTC crossed midnight, while the source site still said "posted
    today". Both `now` and timestamped postings are shifted into the
    viewer's frame before the date diff. Date-ONLY strings (len 10) are the
    source's calendar date already — they diff directly against the
    viewer-local today, never shifted.
    """
    if not iso_date:
        return None
    offset = timedelta(minutes=_clamp_tz_offset(tz_offset_minutes))
    try:
        raw = iso_date.replace("Z", "+00:00")
        d = datetime.fromisoformat(raw)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        local_today = (datetime.now(timezone.utc) + offset).date()
        posted_date = d.date() if len(raw) == 10 else (d.astimezone(timezone.utc) + offset).date()
        return max(0, (local_today - posted_date).days)
    except ValueError:
        return None


# Workday's global country reference WID for the United States. Confirmed
# identical across independent tenants (CVS wd1, Adobe wd5, Salesforce wd12)
# — the CXS locationCountry facet uses global reference IDs, not
# tenant-local ones. The 532-tenant live run showed two failure modes we
# must handle: ~half the tenants 400 the facet (not exposed on their site),
# and a further set (notably some wd12 tenants) silently IGNORE it and
# return worldwide results with HTTP 200. So the facet is used only as a
# server-side narrowing hint — the client-side geo filter below always runs.
US_COUNTRY_FACET_ID = "bc33aa3152ec42d4995f4791a106ed09"

# Tenants observed to 400 the locationCountry facet — remembered for the
# process lifetime so only the FIRST term-query per tenant pays the
# 400+retry round trip (the live run had 264 such tenants x 3 terms).
_FACET_UNSUPPORTED: set = set()
_FACET_LOCK = Lock()

# "in" is deliberately absent: Indian postings like "Hyderabad IN" would
# match it. Indiana rows don't need the rescue — anything not explicitly
# foreign is kept by the default tier anyway.
_US_STATE_RE = re.compile(
    r"\b(al|ak|az|ar|ca|co|ct|de|fl|ga|hi|id|il|ia|ks|ky|la|me|md|ma|mi"
    r"|mn|ms|mo|mt|ne|nv|nh|nj|nm|ny|nc|nd|oh|ok|or|pa|ri|sc|sd|tn|tx|ut"
    r"|vt|va|wa|wv|wi|wy|dc)\b", re.IGNORECASE,
)

# Explicit non-US markers seen in enterprise Workday locationsText values.
# Checked ONLY after the US-positive tier, so "Paris, TX" / "Vienna, VA" /
# "Ontario, CA" are already kept before these substrings are consulted.
_NON_US_MARKERS = (
    "india", "canada", "united kingdom", "ireland", "poland", "philippines",
    "mexico", "brazil", "china", "japan", "singapore", "australia",
    "germany", "france", "spain", "italy", "netherlands", "romania",
    "hungary", "czech", "israel", "egypt", "colombia", "argentina",
    "costa rica", "malaysia", "vietnam", "thailand", "indonesia",
    "switzerland", "sweden", "denmark", "norway", "finland", "belgium",
    "austria", "portugal", "greece", "turkey", "united arab emirates",
    "saudi", "qatar", "south africa", "kenya", "nigeria", "hong kong",
    "taiwan", "korea", "new zealand", "chile", "peru", "sri lanka",
    "pakistan", "bangladesh",
    # City / hub tells that appear without a country suffix:
    "pune", "bengaluru", "bangalore", "hyderabad", "chennai", "mumbai",
    "new delhi", "noida", "gurgaon", "gurugram", "kolkata", "ahmedabad",
    "toronto", "vancouver", "montreal", "mississauga", "ottawa", "calgary",
    "london", "edinburgh", "glasgow", "belfast", "leeds", "dublin, ireland",
    "warsaw", "krakow", "gdansk", "bucharest", "budapest", "prague",
    "lisbon", "madrid", "barcelona", "paris, fr", "munich", "berlin, de",
    "frankfurt", "amsterdam", "zurich", "geneva", "stockholm", "copenhagen",
    "oslo", "helsinki", "brussels", "milan, it", "tel aviv", "cairo",
    "nairobi", "lagos", "dubai", "abu dhabi", "riyadh", "doha", "seoul",
    "taipei", "jakarta", "kuala lumpur", "ho chi minh", "hanoi", "tokyo",
    "osaka", "sydney", "melbourne", "brisbane", "shanghai", "beijing",
    "shenzhen", "makati", "manila", "taguig", "cebu", "guadalajara",
    "monterrey", "sao paulo", "colombo", "karachi", "lahore", "islamabad",
)
# Word-boundary matching — "india" must not fire inside "Indianapolis".
_NON_US_RE = re.compile(
    r"\b(" + "|".join(re.escape(m) for m in _NON_US_MARKERS) + r")\b",
    re.IGNORECASE,
)
# Comma-anchored so "On-site" / "Ab initio" prose never matches — Canadian
# postings write provinces as "Toronto, ON" / "Vancouver, BC".
_CA_PROVINCE_RE = re.compile(r",\s*(on|bc|ab|qc|mb|sk|ns|nb|nl|pe)\b", re.IGNORECASE)


def _geo_keep_us(loc_text: str) -> bool:
    """Tiered US filter, applied to EVERY row when us_only is set.

    1. Country-level US signal ("United States", "USA", "US") -> keep.
    2. US state code -> keep. Runs BEFORE the marker tier so collisions like
       "Paris, TX" / "Vienna, VA" / "Ontario, CA" are rescued.
    3. Explicit non-US marker (country, foreign hub city, ", ON"-style
       Canadian province) -> drop. Catches "Remote India" and "Hyderabad IN"
       — which is why "remote" alone is NOT a keep signal earlier.
    4. Ambiguous ("6 Locations", "Remote", bare city, empty) -> keep.
       Dropping ambiguous rows would silently lose valid US roles; the
       server-side facet already biased page 1 toward US where honored.
    """
    loc = (loc_text or "").strip().lower()
    if not loc or "location" in loc:
        return True
    # Pre-tier: "Toronto" outranks the state-code rescue — Canadian rows like
    # "CA-Toronto-York St" would otherwise be kept by the "ca" token.
    if "toronto" in loc:
        return False
    if "united states" in loc or "usa" in loc:
        return True
    if re.search(r"\b(us|u\.s\.)\b", loc):
        return True
    if _US_STATE_RE.search(loc):
        return True
    if _NON_US_RE.search(loc):
        return False
    if _CA_PROVINCE_RE.search(loc):
        return False
    return True


def _fetch_tenant_term(
    row: Dict[str, str],
    term: str,
    location: str,
    us_only: bool = True,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """One (tenant, search-term) unit of the fan-out.

    Strategy (proven against the full 532-tenant catalog, 2026-07):
      1. Query with searchText=<term> AND the US locationCountry facet so the
         server both relevance-ranks and geo-narrows page 1 where supported.
      2. If the tenant 400s the facet, remember that in _FACET_UNSUPPORTED
         (so later terms skip the wasted round trip) and retry unfaceted.
      3. The client-side _geo_keep_us filter runs on EVERY row regardless —
         some tenants silently ignore unknown facets and return worldwide
         results with HTTP 200, so facet acceptance proves nothing.
      4. Keep rows inside the 30-day window, normalize into the live-search
         Job record shape.

    Returns (jobs, meta). Failures return ([], meta) — a partial result
    across N tenants always beats a hard error."""
    tenant, host, site = row["tenant"], row["wd_host"], row["site"]
    base = f"https://{tenant}.{host}.myworkdayjobs.com"
    url = f"{base}/wday/cxs/{tenant}/{site}/jobs"
    location_lc = (location or "").strip().lower()
    if location_lc in {"united states", "us", "usa"}:
        location_lc = ""  # us_only already handles country scoping
    out: List[Dict[str, Any]] = []
    meta: Dict[str, Any] = {"facet_fallback": False, "error": None, "pages": 0}
    with _FACET_LOCK:
        facet_known_bad = tenant in _FACET_UNSUPPORTED
    facets: Dict[str, Any] = (
        {"locationCountry": [US_COUNTRY_FACET_ID]}
        if us_only and not facet_known_bad else {}
    )

    for page in range(MAX_PAGES_PER_TERM):
        body = {
            "searchText": term,
            "locations": [],
            "appliedFacets": facets,
            "limit": PAGE_LIMIT,
            "offset": page * PAGE_LIMIT,
        }
        try:
            resp = _SESSION.post(url, json=body, headers=_HEADERS, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 400 and facets:
                # Tenant doesn't expose the locationCountry facet — drop it,
                # remember for this tenant's remaining terms, retry unfaceted.
                facets = {}
                meta["facet_fallback"] = True
                with _FACET_LOCK:
                    _FACET_UNSUPPORTED.add(tenant)
                body["appliedFacets"] = {}
                resp = _SESSION.post(url, json=body, headers=_HEADERS, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as e:
            logger.debug("workday-jobs %s/%s: network error %s", tenant, term, e)
            meta["error"] = f"network: {e.__class__.__name__}"
            break
        if resp.status_code != 200:
            logger.debug("workday-jobs %s/%s: HTTP %s", tenant, term, resp.status_code)
            meta["error"] = f"http_{resp.status_code}"
            break
        try:
            data = resp.json()
        except ValueError:
            meta["error"] = "bad_json"
            break
        meta["pages"] = page + 1
        postings = data.get("jobPostings") or []
        for jp in postings:
            if not isinstance(jp, dict):
                continue
            title = (jp.get("title") or "").strip()
            if not title:
                continue
            posted_text = (jp.get("postedOn") or "").strip()
            posted_iso = _approx_iso_from_relative(posted_text)
            days = _days_ago(posted_iso)
            # "Posted 30+ Days Ago" parses to exactly 30 — treat >30 or
            # unparseable as outside the tab's widest window.
            if days is None or days > WINDOW_DAYS or "30+" in posted_text:
                continue
            loc_text = (jp.get("locationsText") or "").strip()
            # Geo filtering ALWAYS runs — the facet is only a narrowing hint
            # (some tenants silently ignore it and return worldwide rows).
            if us_only and not _geo_keep_us(loc_text):
                continue
            if location_lc:
                haystack = loc_text.lower()
                if location_lc not in haystack and "remote" not in haystack:
                    continue
            path = (jp.get("externalPath") or "").lstrip("/")
            req_id = (jp.get("bulletFields") or [None])[0] or path
            seed = f"workday_jobs|{tenant}|{req_id}|{title}".lower()
            out.append({
                "job_id": "workday:" + hashlib.md5(seed.encode()).hexdigest()[:24],
                "title": title,
                "company": row["display_name"],
                "logo": "",
                "location": loc_text or "Not specified",
                # Canonical form Workday itself emits in the detail endpoint's
                # externalUrl — no locale segment.
                "apply_link": f"{base}/{site}/{path}" if path else base,
                "description": "",
                "salary": "",
                "employment_type": (jp.get("timeType") or "").strip(),
                "date_posted": posted_iso,
                "posted_text": posted_text,
                "is_remote": "remote" in loc_text.lower(),
                "h1b_sponsor": False,
                "contract_friendly": False,
                "source": "Workday",
                "tenant": tenant,
                "industry": row.get("industry") or "",
                "days_ago": days,
                "search_term": term,
            })
        if len(postings) < PAGE_LIMIT:
            break  # exhausted this term on this tenant
    return out, meta


# ----------------------------------------------------------------------
# Seniority + tailor-feasibility (deterministic, no LLM)
# ----------------------------------------------------------------------

_SENIOR_RE = re.compile(
    r"\b(senior|sr\.?|staff|principal|lead|architect|director|manager|head|"
    r"vp|vice president|distinguished|fellow|iii|iv|v)\b", re.IGNORECASE)
_ENTRY_RE = re.compile(
    r"\b(junior|jr\.?|associate|entry|early career|new grad|graduate|"
    r"university|campus|\bi\b|\b1\b)\b", re.IGNORECASE)
_INTERN_RE = re.compile(r"\b(intern|internship|co-?op)\b", re.IGNORECASE)


def _seniority(title: str) -> str:
    t = title or ""
    if _INTERN_RE.search(t):
        return "intern"
    if _SENIOR_RE.search(t):
        return "senior"
    if _ENTRY_RE.search(t):
        return "entry"
    return "mid"


_DESC_ENTRY_RE = re.compile(
    r"entry[- ]level|new grad|recent graduate|early[- ]career|"
    r"university grad|no prior experience|0-2 years", re.IGNORECASE)


def _seniority_full(title: str, description: str, exp_level: str = "") -> str:
    """Title verdict, refined by the JD or the board's experience-level label.

    Enterprise postings often title entry roles plainly ("Software
    Engineer") and put the level in the description ("entry level",
    "0-2 years experience") — title-only classification files those under
    "mid" and starves the Entry filter. Only unmarked titles are refined;
    an explicit "Senior X" title always wins.

    ``exp_level`` is the board's structured experience label (SmartRecruiters /
    Workable) — a clean signal used when the title is ambiguous AND no
    description was fetched, so un-enriched rows still get an accurate level.
    """
    level = _seniority(title)
    if level != "mid":
        return level
    # Ambiguous title — refine from the JD description when we fetched one.
    if description:
        req = _required_years(description)
        if _DESC_ENTRY_RE.search(description) or (req is not None and req <= 2):
            return "entry"
        if req is not None and req >= 7:
            return "senior"
    # Otherwise fall back to the board's experience-level label (no per-job
    # fetch needed). "Mid-Senior Level" is left as mid (genuinely ambiguous).
    e = (exp_level or "").lower()
    if "intern" in e:
        return "intern"
    if "entry" in e or e == "associate":
        return "entry"
    if "director" in e or "executive" in e:
        return "senior"
    return "mid"


# Tech-stack families for the frank tailor-feasibility check. A family is a
# JD "core requirement" when it appears in the TITLE or >= 3 times in the
# description; the resume "has" a family when any parsed skill matches it.
_STACK_FAMILIES: Dict[str, Tuple[str, re.Pattern]] = {
    k: (label, re.compile(rx, re.IGNORECASE)) for k, (label, rx) in {
        "dotnet":     (".NET/C#", r"\.net\b|\bc#|asp\.net|dotnet"),
        "java":       ("Java/Spring", r"\bjava\b(?!script)|spring boot|spring framework|\bj2ee\b"),
        "python":     ("Python", r"\bpython\b|django|fastapi|\bflask\b"),
        "node":       ("Node.js", r"node\.?js|\bexpress\.js\b|\bnestjs\b"),
        "frontend":   ("Frontend JS", r"\breact\b|\bangular\b|\bvue\b|next\.?js|\btypescript\b"),
        "golang":     ("Go", r"\bgolang\b|\bgo\b(?=\s+(developer|engineer|programming|lang))"),
        "ruby":       ("Ruby/Rails", r"\bruby\b|\brails\b"),
        "php":        ("PHP", r"\bphp\b|\blaravel\b"),
        "ios":        ("iOS/Swift", r"\bios\b|\bswift\b|objective-c"),
        "android":    ("Android/Kotlin", r"\bandroid\b|\bkotlin\b"),
        "embedded":   ("Embedded/C++", r"\bembedded\b|\bc\+\+|\brtos\b|\bfirmware\b"),
        "mainframe":  ("Mainframe/COBOL", r"\bmainframe\b|\bcobol\b|as/400|\brpg\b"),
        "sap":        ("SAP/ABAP", r"\babap\b|\bsap\b"),
        "salesforce": ("Salesforce", r"\bsalesforce\b|\bapex\b"),
        "data_eng":   ("Data Eng", r"\bspark\b|\bhadoop\b|\bairflow\b|\bdbt\b|\bsnowflake\b|\bdatabricks\b|\betl\b"),
        "cloud":      ("Cloud/DevOps", r"\baws\b|\bazure\b|\bgcp\b|kubernetes|terraform|\bdocker\b|devops|ci/cd|\bsre\b|\bjenkins\b"),
        "ml_ai":      ("ML/AI", r"machine learning|pytorch|tensorflow|\bnlp\b|\bllm\b|deep learning"),
    }.items()
}

_CLEARANCE_RE = re.compile(
    r"(active|current)\s+(security\s+)?clearance|ts/?sci|top secret|secret clearance",
    re.IGNORECASE)
# CXS relevance search returns a company's least-bad matches for ANY query,
# so retail/banking rows ("Universal Banker I", "Retail Sales Associate")
# leak in at low scores. A row with no tech-role word in the title, no
# stack family anywhere, and zero matched skills is not a target for a
# technical resume — feasibility marks it skip.
_TECH_ROLE_RE = re.compile(
    r"engineer|developer|architect|scientist|devops|sre\b|programmer|"
    r"software|cloud|data|security|platform|infrastructure|\bqa\b|\bit\b|"
    r"test|technolog|technical|systems|network|database|analytics|machine learning",
    re.IGNORECASE)
# Business-side titles that sneak past _TECH_ROLE_RE via one generic word
# ("Data Business Analyst", "Senior Marketing Technologist", "Finance
# Manager, Data & Automation"). Unless the title ALSO carries a real IC
# engineering word, these are not targets for an engineering resume.
_OFF_ROLE_RE = re.compile(
    r"marketing|\bsales\b|account (manager|executive)|talent|recruit|"
    r"human resources|\bhr\b|finance|accounting|\baudit\b|business analyst|"
    r"procurement|merchandis|underwrit|claims|compliance|paralegal|"
    r"customer success|call center|collections|payroll|\btax\b",
    re.IGNORECASE)
_IC_ENG_RE = re.compile(
    r"engineer|developer|architect|scientist|devops|\bsre\b|programmer|"
    r"administrator", re.IGNORECASE)
_MGMT_RE = re.compile(
    r"\b(manager|director|vp|vice president|head of)\b", re.IGNORECASE)
_YEARS_RE = re.compile(
    r"(?:(\d{1,2})\s*(?:[-–—]|to)\s*)?(\d{1,2})\s*\+?\s*years?", re.IGNORECASE)
# A "N years" number is a years-of-EXPERIENCE ask only when an experience cue
# sits nearby — otherwise "5 years ago" / "renewed every 2 years" would
# register. Broader than a bare "experience" check: real JDs phrase the bar as
# "5+ years in a similar role", "minimum 3 years leading…", "at least 4 years
# hands-on" — none of which contain the word "experience".
_EXP_CUE_RE = re.compile(
    r"experience|exp\.|years?\s+(?:in|of|as|with|building|leading|working|"
    r"developing|managing|supporting)|background|track record|minimum|"
    r"at least|require|proven|hands-on|expertise|history of|seniority",
    re.IGNORECASE)


def _family_counts(text: str) -> Dict[str, int]:
    """Occurrence count per stack family in `text` (nonzero families only).

    Split out so the count map can be computed ONCE per job during scoring and
    reused by _attach_feasibility, sparing a second full-description regex
    sweep (17 families over up to ~5k chars) on every enriched row.
    """
    counts: Dict[str, int] = {}
    for key, (_label, rx) in _STACK_FAMILIES.items():
        n = len(rx.findall(text))
        if n:
            counts[key] = n
    return counts


def _families_in(text: str, core_threshold: int = 1) -> set:
    return {k for k, n in _family_counts(text).items() if n >= core_threshold}


def _required_years(description: str, prefer_min: bool = False) -> Optional[int]:
    """Plausible years-of-experience ask near "experience".

    Default: the MAX such number ("8+ years"). With ``prefer_min=True`` a range
    uses its LOWER bound ("3-6 years" -> 3) — used by the experience-gap check so
    a range isn't over-penalized. Seniority keeps the default (max).
    """
    best = None
    for m in _YEARS_RE.finditer(description or ""):
        lo, hi = m.group(1), m.group(2)           # lo set only for a range
        n = int(lo) if (prefer_min and lo) else int(hi)
        if n > 20:
            continue
        window = description[max(0, m.start() - 60):m.end() + 60]
        if _EXP_CUE_RE.search(window):
            best = n if best is None else max(best, n)
    return best


def _attach_feasibility(jobs: List[Dict[str, Any]], resume_skills: List[str],
                        resume_years: Optional[int]) -> None:
    """Frank, strict "can the base resume be tailored into this?" verdict.

    strong   - core stacks overlap and the skill score is already solid
    possible - no hard conflict; tailoring is plausible
    stretch  - real gap (2-4 missing years, partial stack mismatch)
    skip     - not honestly tailorable: the JD centers on a stack the resume
               has zero footprint in (.NET JD vs Python resume), demands
               5+ more years than the resume shows, or needs an active
               security clearance. The tab hides these by default.

    Left unset when there's no parsed resume — we refuse to guess.
    """
    if not resume_skills:
        return
    resume_fams = _families_in(" \n".join(resume_skills))
    # A resume with no recognizable tech stack is non-tech. The stack-mismatch
    # / "not a technical role" / IC-vs-management verdicts below are all
    # tech-specific and would wrongly "skip" every nurse/accountant/teacher
    # role. For non-tech resumes we therefore run ONLY the domain-agnostic
    # gates that are still valid — the experience-year gap and the clearance
    # requirement — and otherwise leave feasibility neutral.
    is_tech = bool(resume_fams)
    for job in jobs:
        title = job.get("title") or ""
        desc = job.get("description") or ""
        if not is_tech:
            verdict, reason = None, ""
            if desc and _CLEARANCE_RE.search(desc):
                verdict, reason = "skip", "Requires an active security clearance"
            else:
                req = _required_years(desc, prefer_min=True) if desc else None
                gap = (req - resume_years) if (req and resume_years is not None) else 0
                if gap >= 5:
                    verdict, reason = "skip", f"Asks ~{req}+ yrs experience vs ≈{resume_years} on resume"
                elif gap >= 2:
                    verdict, reason = "stretch", f"Asks ~{req}+ yrs vs ≈{resume_years} — a stretch"
                    # Graded experience penalty so an under-qualified role can't
                    # headline as a perfect score (gap 2→-11, 3→-22, 4→-33).
                    job["match_score"] = max(0, (job.get("match_score") or 0) - (gap - 1) * 11)
            if verdict:
                job["tailor_feasibility"] = verdict
                job["feasibility_reason"] = reason
                if verdict == "skip":
                    job["match_score"] = min(job.get("match_score") or 0, 12)
            continue
        title_fams = _families_in(title)
        core = set(title_fams)
        if desc:
            # Reuse the family counts cached by _score_jobs (blob = title +
            # desc). For a family ABSENT from the title, its blob-count equals
            # its desc-count, so {f: count>=3} unioned with title_fams is
            # identical to the old _families_in(desc, 3) — without re-scanning
            # the JD. Fall back to a direct count only if scoring didn't run.
            counts = job.get("_fam_counts")
            if counts is None:
                counts = _family_counts(title + "\n" + desc)
            core |= {k for k, n in counts.items() if n >= 3}
        verdict, reason = "possible", "No hard conflicts found"
        overlap = core & resume_fams
        conflicts = core - resume_fams
        # A title with no tech-role word and no stack family is not a target
        # for a technical resume — an incidental skill hit ("sql" in a
        # Universal Banker JD) must NOT rescue it. Only a description whose
        # CORE stacks overlap the resume can (differently-titled tech roles).
        if not _TECH_ROLE_RE.search(title) and not title_fams and not overlap:
            verdict, reason = "skip", "Not a technical role"
        elif _OFF_ROLE_RE.search(title) and not _IC_ENG_RE.search(title) and not title_fams:
            verdict, reason = "skip", "Business-side role, not an engineering fit"
        elif core and not overlap:
            labels = ", ".join(_STACK_FAMILIES[k][0] for k in sorted(conflicts))
            verdict, reason = "skip", f"Core stack mismatch — centers on {labels}, no resume footprint"
        elif desc and _CLEARANCE_RE.search(desc):
            verdict, reason = "skip", "Requires an active security clearance"
        else:
            req = _required_years(desc, prefer_min=True) if desc else None
            gap = (req - resume_years) if (req and resume_years is not None) else 0
            if gap >= 5:
                verdict, reason = "skip", f"Asks ~{req}+ yrs experience vs ≈{resume_years} on resume"
            elif gap >= 2:
                verdict, reason = "stretch", f"Asks ~{req}+ yrs vs ≈{resume_years} — a stretch"
                # Graded experience penalty (years-gap stretch only, not the
                # partial-off-stack / management stretches below).
                job["match_score"] = max(0, (job.get("match_score") or 0) - (gap - 1) * 11)
            elif conflicts and overlap:
                labels = ", ".join(_STACK_FAMILIES[k][0] for k in sorted(conflicts))
                verdict, reason = "stretch", f"Partially off-stack ({labels}) — tailorable via {', '.join(_STACK_FAMILIES[k][0] for k in sorted(overlap))}"
            elif overlap and (job.get("match_score") or 0) >= 55:
                labels = ", ".join(_STACK_FAMILIES[k][0] for k in sorted(overlap))
                verdict, reason = "strong", f"Direct overlap on {labels}"
        if verdict in ("strong", "possible") and _MGMT_RE.search(title):
            # An IC resume can't be tailored into a people-management req.
            verdict, reason = "stretch", "Management-track title — not an IC engineering role"
        job["tailor_feasibility"] = verdict
        job["feasibility_reason"] = reason
        if verdict == "skip":
            # An off-target row must never outrank a real one, whatever
            # incidental skills its JD mentions.
            job["match_score"] = min(job.get("match_score") or 0, 12)


# ----------------------------------------------------------------------
# Visa sponsorship signals (for international candidates)
# ----------------------------------------------------------------------

# JD-level refusal language — the strongest signal there is; overrides any
# company-level sponsorship history.
_NO_SPONSOR_RE = re.compile(
    r"no (visa |work )?sponsorship"
    r"|sponsorship (is |will )?not (be )?(available|provided|offered|considered)"
    r"|(will|can|do(es)?) ?n[o']t (be able to )?(provide |offer )?sponsor"
    r"|unable to sponsor|not eligible for (visa )?sponsorship"
    r"|without (the need for |requiring |need of )?(visa |employer |work )?sponsorship"
    r"|must be (a )?(u\.?s\.? citizen|us citizen)"
    r"|u\.?s\.? citizenship (is )?required|citizens? only"
    r"|no (h-?1b|opt|cpt)\b",
    re.IGNORECASE)


def _attach_sponsorship(jobs: List[Dict[str, Any]]) -> None:
    """Per-job visa signal for international candidates.

    sponsorship:
      refused - the JD itself says no sponsorship / citizens only (checked
                on title + description; description exists for top rows)
      likely  - company filed meaningful H-1Bs last year (high/medium tier)
      rare    - company files occasionally (low tier)
      unknown - no data; NOT excluded by default (absence of evidence)

    Also sets the existing h1b_sponsor flag so the JobCard's H1B badge
    lights up for proven sponsors.
    """
    from services.h1b_sponsor_data import H1B_SPONSORS
    for job in jobs:
        text = (job.get("title") or "") + "\n" + (job.get("description") or "")
        m = _NO_SPONSOR_RE.search(text)
        tier_info = H1B_SPONSORS.get(job.get("tenant") or "")
        tier = tier_info[0] if tier_info else None
        if m:
            # Direct evidence from the JD itself — the highest-fidelity signal.
            job["sponsorship"] = "refused"
            job["h1b_sponsor"] = False
            job["sponsor_source"] = "jd_text"
            job["sponsor_note"] = f'JD states: "…{m.group(0)[:70]}…"'
        elif tier in ("high", "medium"):
            job["sponsorship"] = "likely"
            job["h1b_sponsor"] = True
            # ESTIMATE from a curated employer tier list — NOT live per-employer
            # USCIS data (that honest path needs the human sponsorship ingest).
            # Label it as an estimate so we never overstate the source.
            job["sponsor_source"] = "estimate"
            job["sponsor_note"] = (
                f"Est.: {job['company']} historically files ~{tier_info[1]:,} H-1Bs/yr")
        elif tier == "low":
            job["sponsorship"] = "rare"
            job["sponsor_source"] = "estimate"
            job["sponsor_note"] = f"Est.: {job['company']} files few H-1Bs (~{tier_info[1]}/yr)"
        else:
            job["sponsorship"] = "unknown"
            job["sponsor_source"] = "none"


def _load_resume_profile(user_email: str) -> Tuple[List[str], Optional[int]]:
    """Skills (resume-prominence order) + experience years for feasibility."""
    if not user_email:
        return [], None
    try:
        from schemas.resume_schemas import flatten_skills
        from services import resume_access
        # v3: the active resume_assets doc via the canonical accessor.
        doc = resume_access.get_active_structured(user_email)
        if not doc:
            return [], None
        skills_raw = flatten_skills(doc.get("structured") or {}) or []
        seen: set = set()
        skills: List[str] = []
        for s in skills_raw:
            n = str(s).strip().lower()
            if n and n not in seen:
                seen.add(n)
                skills.append(n)
        years = doc.get("experience_years")
        try:
            years = int(years) if years is not None else None
        except (TypeError, ValueError):
            years = None
        return skills, years
    except Exception as e:
        logger.warning("workday-jobs: resume profile load failed for %s: %s", user_email, e)
        return [], None


# ----------------------------------------------------------------------
# Scoring
# ----------------------------------------------------------------------

def _score_jobs(
    jobs: List[Dict[str, Any]],
    titles: List[str],
    skills: List[str],
    skill_terms: List[str],
) -> List[Dict[str, Any]]:
    """Attach match_score / matched_skills / missing_skills to every job.

    Deterministic (no LLM). Three signals:
      - skill overlap of the resume skills against title + description (the
        description exists once _enrich_details has run for that row — same
        regex semantics as jd_match_scorer);
      - a title-family bonus (+45) when the job title covers all core tokens
        of one of the user's titles;
      - a skill-signal bonus (+20) when the title DOESN'T match but the row
        either overlaps resume skills or was retrieved by a skill-derived
        search term — this is what keeps differently-titled roles ("Member
        of Technical Staff") ranked instead of buried at zero.

    Nothing is ever dropped here — scoring only orders; recall is preserved.
    """
    skill_rx = [(s, _build_match_regex(s)) for s in skills]
    matchers = _role_matchers(titles)
    skill_terms_set = {t.lower() for t in skill_terms}
    primary_fams, resume_fams = _resume_family_weights(skills)

    for job in jobs:
        title_lc = job["title"].lower()
        blob = (job["title"] + "\n" + (job.get("description") or "")
                + "\n" + (job.get("_meta") or "")).lower()
        matched = [s for s, rx in skill_rx if rx is not None and rx.search(blob)]
        matched_set = set(matched)
        missing = [s for s, _ in skill_rx if s not in matched_set][:8]
        # Base measures JD COVERAGE, not "fraction of resume used" — a
        # 50-skill resume matching 10 of a JD's asks is a great fit, not a
        # 20%. ~6 points per matched skill, saturating at 55 so the
        # title/skill boosts still differentiate the top of the scale.
        base = min(55, len(matched) * 6) if skills else 0
        title_matched = any(all(tok in title_lc for tok in m) for m in matchers)
        skill_signal = bool(matched) or bool(
            skill_terms_set & {str(t).lower() for t in (job.get("matched_terms") or [])}
        )
        # Stack alignment: a cloud-primary resume must rank cloud-centered
        # JDs above equally title-matched off-stack ones. Cache the family
        # counts on the job so _attach_feasibility reuses this scan instead of
        # re-sweeping the full description.
        fam_counts = _family_counts(blob)
        job["_fam_counts"] = fam_counts
        job_fams = set(fam_counts)
        align = 12 if job_fams & primary_fams else (6 if job_fams & resume_fams else 0)
        score = min(100, base + (45 if title_matched else (20 if skill_signal else 0)) + align)
        if not skills and not title_matched:
            score = 15  # neutral floor so the ring isn't a sea of zeros
        job["match_score"] = score
        job["matched_skills"] = matched[:12]
        job["missing_skills"] = missing
        job["title_matched"] = title_matched
        job["seniority"] = _seniority_full(
            job["title"], job.get("description") or "", job.get("_exp") or "")
    return jobs


# ----------------------------------------------------------------------
# Detail enrichment — full JD for the top-ranked rows
# ----------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")


def _html_to_text(raw: str) -> str:
    import html as _html
    return re.sub(r"\s+", " ", _TAG_RE.sub(" ", _html.unescape(raw or ""))).strip()


def _enrich_details(jobs: List[Dict[str, Any]], tz_offset_minutes: int = 0) -> int:
    """Fetch the CXS job-detail JSON for rows lacking a description.

    GET {base}/wday/cxs/{tenant}/{site}{externalPath} returns
    jobPostingInfo with the full HTML jobDescription, the EXACT posting
    startDate (better than the lossy "Posted N Days Ago"), and remoteType.
    Enables real skill-overlap scoring + accurate recency for top matches.
    Returns how many rows were enriched; individual failures are skipped.
    """
    need = [j for j in jobs if not j.get("description") and j.get("tenant")]
    if not need:
        return 0

    def _one(job: Dict[str, Any]) -> bool:
        m = re.match(r"(https://[^/]+)/(.+)$", job.get("apply_link") or "")
        if not m:
            return False
        url = f"{m.group(1)}/wday/cxs/{job['tenant']}/{m.group(2)}"
        try:
            resp = _SESSION.get(url, headers=_HEADERS, timeout=DETAIL_TIMEOUT)
            if resp.status_code != 200:
                return False
            info = (resp.json() or {}).get("jobPostingInfo") or {}
        except (requests.RequestException, ValueError):
            return False
        desc = info.get("jobDescription") or ""
        if desc:
            job["description"] = _html_to_text(desc)[:5000]
        start = str(info.get("startDate") or "")[:10]
        if re.match(r"^\d{4}-\d{2}-\d{2}$", start):
            days = _days_ago(start, tz_offset_minutes)
            if days is not None:
                job["date_posted"] = start
                job["days_ago"] = days
        if "remote" in str(info.get("remoteType") or "").lower():
            job["is_remote"] = True
        # Definitive geo: the detail payload names the posting's country,
        # which resolves rows the list view hid behind "N Locations".
        cinfo = info.get("country") or {}
        rloc = ((info.get("jobRequisitionLocation") or {}).get("country") or {})
        if cinfo or rloc:
            is_us_row = (cinfo.get("id") == US_COUNTRY_FACET_ID
                         or rloc.get("alpha2Code") == "US")
            job["country_code"] = "US" if is_us_row else str(
                rloc.get("alpha2Code") or cinfo.get("descriptor") or "non-US")[:40]
        primary_loc = str(info.get("location") or "").strip()
        if primary_loc and "location" in str(job.get("location") or "").lower():
            job["location"] = primary_loc
        return bool(desc)

    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, 64)) as pool:
        results = list(pool.map(_one, need))
    return sum(1 for ok in results if ok)


# ----------------------------------------------------------------------
# Cache
# ----------------------------------------------------------------------

def _catalog_signature(rows: List[Dict[str, str]]) -> str:
    blob = "|".join(sorted(f"{r['tenant']}.{r['wd_host']}/{r['site']}" for r in rows))
    return hashlib.md5(blob.encode()).hexdigest()[:12]


def _cache_key(rows: List[Dict[str, str]], terms: List[str], location: str,
               us_only: bool) -> str:
    payload = json.dumps({
        "v": RESULT_SCHEMA_VERSION,
        "catalog": _catalog_signature(rows),
        "terms": sorted(terms),
        "location": (location or "").strip().lower(),
        "us_only": bool(us_only),
    }, sort_keys=True)
    return hashlib.md5(payload.encode()).hexdigest()


def _get_cache_collection():
    try:
        from utils.db_connect import DBConnect
        db = DBConnect().get_db()
        coll = db[CACHE_COLLECTION]
        try:
            coll.create_index("cache_key", unique=True)
            coll.create_index("cached_at", expireAfterSeconds=CACHE_TTL_SECONDS,
                              name="workday_jobs_cache_ttl")
        except Exception:
            pass  # index may already exist / racing another cold start
        return coll
    except Exception as e:
        logger.warning("workday-jobs: cache unavailable: %s", e)
        return None


def _read_cache(coll, key: str) -> Optional[List[Dict[str, Any]]]:
    if coll is None:
        return None
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=CACHE_FRESH_SECONDS)
        doc = coll.find_one({"cache_key": key, "cached_at": {"$gte": cutoff}})
        if doc and isinstance(doc.get("jobs"), list):
            return doc["jobs"]
    except Exception as e:
        logger.warning("workday-jobs: cache read failed: %s", e)
    return None


def _write_cache(coll, key: str, jobs: List[Dict[str, Any]]) -> None:
    if coll is None:
        return
    try:
        coll.update_one(
            {"cache_key": key},
            {"$set": {"cache_key": key, "jobs": jobs,
                      "cached_at": datetime.now(timezone.utc)}},
            upsert=True,
        )
    except Exception as e:
        logger.warning("workday-jobs: cache write failed: %s", e)


# ----------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------

def run_workday_jobs_search(
    payload: Dict[str, Any],
    partial_cb: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> Dict[str, Any]:
    """Execute one Workday Jobs search. Called from the async job runner.

    payload (sanitized by the blueprint):
        titles: list[str]        - role titles (prefilled from resume)
        industries: list[str]    - subset of catalog industries ([] = all)
        companies: list[str]     - subset of catalog tenants ([] = all)
        location: str            - free-text location filter within the US
                                   ("" / "United States" = whole country)
        remote_only: bool
        us_only: bool            - default True; server-side US country facet
        force_refresh: bool
        user_email: str

    Returns the full 30-day scored result; the tab filters recency locally.
    """
    titles = [str(t).strip() for t in (payload.get("titles") or []) if str(t or "").strip()]
    industries = {str(i).strip() for i in (payload.get("industries") or []) if str(i or "").strip()}
    companies = {str(c).strip().lower() for c in (payload.get("companies") or []) if str(c or "").strip()}
    location = str(payload.get("location") or "").strip()
    remote_only = bool(payload.get("remote_only", False))
    us_only = bool(payload.get("us_only", True))
    force_refresh = bool(payload.get("force_refresh", False))
    user_email = str(payload.get("user_email") or "")
    # Viewer timezone: recency ("today") is computed in the USER's calendar
    # day, not UTC's. 0 (absent) preserves the old UTC behavior.
    tz_offset = _clamp_tz_offset(payload.get("tz_offset_minutes"))
    # Callers with a tighter wall-clock (the guest scan runs three sources in
    # parallel inside one Lambda) may shrink the fan-out budget — never grow.
    try:
        overall_timeout = min(OVERALL_TIMEOUT, max(30, int(payload.get("overall_timeout") or OVERALL_TIMEOUT)))
    except (TypeError, ValueError):
        overall_timeout = OVERALL_TIMEOUT
    # Detail-enrichment cap for embedded callers (guest scan): the tab
    # defaults fetch hundreds of JD pages — a bounded caller shrinks both
    # slices. Never grows beyond the defaults.
    try:
        _enrich_cap = int(payload.get("enrich_top")) if payload.get("enrich_top") else None
    except (TypeError, ValueError):
        _enrich_cap = None
    enrich_top_n = min(DETAIL_ENRICH_TOP, _enrich_cap) if _enrich_cap else DETAIL_ENRICH_TOP
    enrich_recent_n = min(DETAIL_ENRICH_RECENT, _enrich_cap) if _enrich_cap else DETAIL_ENRICH_RECENT

    rows = [
        r for r in WORKDAY_TENANT_CATALOG
        if (not industries or r.get("industry") in industries)
        and (not companies or r["tenant"] in companies)
    ]
    # Resume skills drive retrieval (extra search terms), scoring, and the
    # tailor-feasibility verdicts; years feed the experience-gap check.
    skills, resume_years = _load_resume_profile(user_email)
    # Anonymous callers (the guest scan) have no profile doc — they pass the
    # guest's own parsed skills/years directly so scoring quality matches an
    # authed run. Never overrides a real profile.
    if not user_email:
        payload_skills = [str(s).strip() for s in (payload.get("resume_skills") or []) if str(s or "").strip()]
        if payload_skills:
            skills = payload_skills[:60]
        try:
            if payload.get("resume_years") is not None:
                resume_years = max(0, int(payload["resume_years"]))
        except (TypeError, ValueError):
            pass
    title_terms = _derive_query_terms(titles)
    skill_terms = _derive_skill_terms(
        skills, title_terms, cap=TOTAL_TERMS_CAP - len(title_terms),
    )
    terms = title_terms + skill_terms
    errors: List[str] = []

    coll = _get_cache_collection()
    key = _cache_key(rows, terms, location, us_only)
    cache_hit = False
    raw_jobs: Optional[List[Dict[str, Any]]] = None
    if not force_refresh:
        raw_jobs = _read_cache(coll, key)
        cache_hit = raw_jobs is not None

    tenants_total = len(rows)
    tenants_done = 0
    tenants_with_results = 0
    diagnostics = {"facet_fallbacks": 0, "task_errors": 0}

    if raw_jobs is None:
        collected: List[Dict[str, Any]] = []
        tasks: List[Tuple[Dict[str, str], str]] = [(r, t) for r in rows for t in terms]
        seen_tenants_with_hits: set = set()
        fallback_tenants: set = set()
        done_tasks = 0
        tasks_per_tenant = max(1, len(terms))
        matchers = _role_matchers(titles)
        # Provisional per-job scores for the streamed preview: real skill
        # matching against the TITLE (descriptions come later), computed once
        # per job (memoized) so snapshots stay cheap. Without this the
        # preview showed a flat wall of 45/15 until the final payload.
        stream_rx = [(s, _build_match_regex(s)) for s in skills[:30]]
        provisional: Dict[str, Tuple[int, List[str]]] = {}

        def _provisional(j: Dict[str, Any]) -> Tuple[int, List[str]]:
            cached = provisional.get(j["job_id"])
            if cached is None:
                tl = j["title"].lower()
                tm = any(all(tok in tl for tok in m) for m in matchers)
                matched = [s for s, rx in stream_rx if rx is not None and rx.search(tl)]
                base = min(55, len(matched) * 6)
                score = min(100, base + (45 if tm else (20 if matched else 0)))
                if not skills and not tm:
                    score = 15
                cached = (score, matched[:8])
                provisional[j["job_id"]] = cached
            return cached

        def _stream_snapshot() -> None:
            """Push a provisional, skill-ranked top slice to the poller so
            the tab renders matches company-by-company during the scan."""
            if not partial_cb:
                return
            snap: Dict[str, Dict[str, Any]] = {}
            for j in collected:
                snap.setdefault(j["job_id"], j)
            rows_snap = list(snap.values())
            for j in rows_snap:
                score, matched = _provisional(j)
                j["match_score"] = score
                j["matched_skills"] = matched
                j.setdefault("missing_skills", [])
            rows_snap.sort(key=lambda x: (
                -(x["match_score"]),
                x.get("days_ago") if x.get("days_ago") is not None else 99,
            ))
            try:
                partial_cb({
                    "progress": {
                        "tenants_done": min(tenants_total, done_tasks // tasks_per_tenant),
                        "tenants_total": tenants_total,
                        "jobs_found": len(rows_snap),
                    },
                    "jobs": rows_snap[:STREAM_TOP],
                    "total": len(rows_snap),
                    "streaming": True,
                })
            except Exception:
                pass
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {pool.submit(_fetch_tenant_term, r, t, location, us_only): (r, t)
                       for r, t in tasks}
            try:
                for fut in as_completed(futures, timeout=overall_timeout):
                    r, _t = futures[fut]
                    done_tasks += 1
                    try:
                        items, meta = fut.result()
                    except Exception as e:
                        logger.debug("workday-jobs tenant task failed: %s", e)
                        items, meta = [], {"error": str(e)}
                    if meta.get("facet_fallback"):
                        fallback_tenants.add(r["tenant"])
                    if meta.get("error"):
                        diagnostics["task_errors"] += 1
                    if items:
                        seen_tenants_with_hits.add(r["tenant"])
                        collected.extend(items)
                    # Stream a provisional result slice every ~80 tasks so
                    # matches appear in the tab while the scan continues.
                    if partial_cb and done_tasks % STREAM_EVERY_TASKS == 0:
                        _stream_snapshot()
            except TimeoutError:
                pending = sum(1 for f in futures if not f.done())
                errors.append(f"timeout: {pending} tenant queries did not finish")
                logger.info("[workday-jobs] overall timeout; %d tasks pending", pending)
                for f in futures:
                    if not f.done():
                        f.cancel()
        tenants_done = min(tenants_total, done_tasks // tasks_per_tenant)
        tenants_with_results = len(seen_tenants_with_hits)
        diagnostics["facet_fallbacks"] = len(fallback_tenants)

        # Dedupe (same posting can match multiple terms — remember WHICH
        # terms found it, the scorer uses that) + per-tenant cap so a single
        # high-volume tenant can't flood the feed.
        by_id: Dict[str, Dict[str, Any]] = {}
        for j in collected:
            term = j.pop("search_term", None)
            existing = by_id.get(j["job_id"])
            if existing is None:
                j["matched_terms"] = [term] if term else []
                by_id[j["job_id"]] = j
            elif term and term not in existing["matched_terms"]:
                existing["matched_terms"].append(term)
        per_tenant: Dict[str, int] = {}
        raw_jobs = []
        for j in sorted(by_id.values(), key=lambda x: x.get("days_ago") if x.get("days_ago") is not None else 99):
            n = per_tenant.get(j["tenant"], 0)
            if n >= PER_TENANT_KEEP:
                continue
            per_tenant[j["tenant"]] = n + 1
            raw_jobs.append(j)
        _write_cache(coll, key, raw_jobs)
    else:
        tenants_done = tenants_total
        tenants_with_results = len({j.get("tenant") for j in raw_jobs if j.get("tenant")})

    # Recompute recency at SERVE time — cached rows age (a "Posted Today"
    # row cached before midnight is 1 day old after it), so days_ago must
    # never be trusted from the cache. Rows aging past the window drop out.
    jobs = []
    for j0 in raw_jobs:
        j = dict(j0)
        days = _days_ago(j.get("date_posted") or "", tz_offset)
        if days is None or days > WINDOW_DAYS:
            continue
        # country_code is set by detail enrichment (possibly via a cached
        # earlier run) — it outranks the locationsText heuristic.
        if us_only and j.get("country_code") not in (None, "US"):
            continue
        j["days_ago"] = days
        # The card label must agree with the viewer-local bucket (the old
        # cached "Posted Yesterday" text disagreed with the source site).
        j["posted_text"] = (
            "Posted Today" if days == 0
            else "Posted Yesterday" if days == 1
            else f"Posted {days} Days Ago"
        )
        jobs.append(j)

    if remote_only:
        jobs = [j for j in jobs if j.get("is_remote")]

    def _rank(items: List[Dict[str, Any]]) -> None:
        items.sort(key=lambda j: (
            -(j.get("match_score") or 0),
            j.get("days_ago") if j.get("days_ago") is not None else 99,
        ))

    jobs = _score_jobs(jobs, titles, skills, skill_terms)
    _rank(jobs)

    # Fetch full descriptions for the top slice, then rescore those rows on
    # real skill overlap + exact posting dates. Rows outside the slice keep
    # their title-based score — nothing is dropped either way.
    if partial_cb:
        try:
            partial_cb({
                "progress": {"tenants_done": tenants_total,
                             "tenants_total": tenants_total,
                             "jobs_found": len(jobs), "phase": "details"},
                # Strip the internal _fam_counts on the streamed copies while
                # leaving the originals intact for _attach_feasibility to reuse.
                "jobs": [{k: v for k, v in j.items() if k != "_fam_counts"}
                         for j in jobs[:STREAM_TOP]],
                "total": len(jobs),
                "streaming": True,
            })
        except Exception:
            pass
    # Enrich the global top slice PLUS the recent cohort — the tab defaults
    # to "last 3 days", and a 3d-old row stuck at the +20 skill-signal score
    # because its description was never fetched is exactly the "wall of 20s"
    # the user sees. Recent rows must carry real skill scores.
    top = jobs[:enrich_top_n] + [
        j for j in jobs[enrich_top_n:]
        if (j.get("days_ago") if j.get("days_ago") is not None else 99) <= 3
    ][:enrich_recent_n]
    enriched = _enrich_details(top, tz_offset)
    if enriched:
        _score_jobs(top, titles, skills, skill_terms)
        # startDate refinement can push a row past the window, and the
        # detail country resolves "N Locations" rows definitively.
        jobs = [j for j in jobs
                if j.get("days_ago") is not None and j["days_ago"] <= WINDOW_DAYS
                and not (us_only and j.get("country_code") not in (None, "US"))]
        _rank(jobs)
        # Persist descriptions/exact dates/geo into the cache so repeat
        # searches serve them without re-fetching 200 detail pages.
        if coll is not None:
            enriched_by_id = {j["job_id"]: j for j in top
                              if j.get("description") or j.get("country_code")}
            for rj in raw_jobs:
                e = enriched_by_id.get(rj["job_id"])
                if e:
                    if e.get("description"):
                        rj["description"] = e["description"]
                    rj["date_posted"] = e["date_posted"]
                    rj["is_remote"] = e["is_remote"]
                    rj["location"] = e["location"]
                    if e.get("country_code"):
                        rj["country_code"] = e["country_code"]
            _write_cache(coll, key, raw_jobs)

    jobs = jobs[:GLOBAL_KEEP]
    _attach_feasibility(jobs, skills, resume_years)
    _rank(jobs)  # feasibility caps skip-row scores — order must reflect it
    _attach_sponsorship(jobs)
    for j in jobs:
        j.pop("_fam_counts", None)  # internal scan cache — never leak to client
    # Release every pooled socket — a warm container must not carry idle
    # connections to hundreds of hosts into its next invocation.
    _drain_session_pools()

    window_counts = {"today": 0, "d1": 0, "d3": 0, "d7": 0, "d30": 0}
    for j in jobs:
        d = j.get("days_ago")
        if d is None:
            continue
        if d == 0:
            window_counts["today"] += 1
        if d <= 1:
            window_counts["d1"] += 1
        if d <= 3:
            window_counts["d3"] += 1
        if d <= 7:
            window_counts["d7"] += 1
        if d <= 30:
            window_counts["d30"] += 1

    return {
        "ok": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "jobs": jobs,
        "total": len(jobs),
        "window_counts": window_counts,
        "query_terms": terms,
        "tenants_total": tenants_total,
        "tenants_done": tenants_done,
        "tenants_with_results": tenants_with_results,
        "industries_available": INDUSTRIES,
        "cache_hit": cache_hit,
        "us_only": us_only,
        # 0 => this account has no parsed resume: the tab shows an upload
        # nudge because skill scores/feasibility are unavailable without it.
        "resume_skill_count": len(skills),
        "diagnostics": diagnostics,
        "errors": errors[:20],
    }


def catalog_summary() -> Dict[str, Any]:
    """Lightweight catalog metadata for the tab's filter UI."""
    by_industry: Dict[str, List[Dict[str, str]]] = {}
    for r in WORKDAY_TENANT_CATALOG:
        by_industry.setdefault(r.get("industry") or "tech", []).append(
            {"display_name": r["display_name"], "tenant": r["tenant"]}
        )
    return {
        "total": len(WORKDAY_TENANT_CATALOG),
        "industries": [
            {
                "key": ind,
                "count": len(companies),
                "companies": sorted(companies, key=lambda c: c["display_name"].lower()),
            }
            for ind, companies in sorted(by_industry.items())
        ],
    }
