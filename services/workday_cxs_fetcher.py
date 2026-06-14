"""
Direct Workday CXS fetcher — bypasses Apify and hits each tenant's public
public job-board JSON endpoint:

    POST https://{tenant}.{wd_host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs
    Body: {"searchText": "...", "locations": [...], "limit": 20, "offset": 0}

This catches roles that the third-party Apify Workday actor misses entirely
because its index doesn't cover every tenant (e.g. Yahoo's
`ouryahoo.wd5.myworkdayjobs.com`). It also returns within seconds with no
Apify credits consumed.

The tenant list supports MULTIPLE candidate `site` paths per tenant — the
fetcher tries them in order and caches the first one that returns HTTP 200.
This means an entry like ("mastercard", "wd1", ("Mastercard_Career_Site",
"CorporateCareers", "External"), "Mastercard") will resolve even if only one
of the three paths is correct, and the cache makes subsequent calls hit the
right URL on the first try.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from threading import Lock
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

import requests

from utils.config import _get_config_value

logger = logging.getLogger(__name__)

# Either:
#   (tenant, host, site, display)                         — single path
#   (tenant, host, (cand1, cand2, ...), display)          — try each in order
TenantSpec = Tuple[str, str, Union[str, Sequence[str]], str]

# Common Workday site-path conventions — used both as default candidates and
# as fallback retry list when a tenant's first guess returns 404. Ordering
# reflects empirical hit-rate from probing well-known tenants.
COMMON_SITE_FALLBACKS: Tuple[str, ...] = (
    "External", "Careers", "ExternalCareerSite", "External_Career_Site",
    "External_Careers", "ExternalCareers", "1", "2",
    "CareerSite", "Careers_External", "external_experienced",
    "External_Career", "CorporateCareers", "Corporate_Careers",
)

# Verified tenants. Each row tells the fetcher which paths to try for that
# tenant. Single string = one path. Tuple = ordered candidates; the fetcher
# probes them in order and caches the first 200. Add a tenant by listing
# its careers URL host (wd1 / wd5 / wd12 / wd3) and one or more site path
# candidates from the URL — when in doubt, list two or three guesses; the
# resolver figures it out at runtime.
DEFAULT_WORKDAY_TENANTS: List[TenantSpec] = [
    # ─── Confirmed live (200 OK on canonical site path) ──────────────────
    ("ouryahoo", "wd5", "Careers", "Yahoo"),
    ("adobe", "wd5", "external_experienced", "Adobe"),
    ("nvidia", "wd5", "NVIDIAExternalCareerSite", "NVIDIA"),
    ("salesforce", "wd12", "External_Career_Site", "Salesforce"),
    ("capitalone", "wd12", "Capital_One", "Capital One"),
    ("citi", "wd5", "2", "Citi"),
    ("disney", "wd5", "disneycareer", "Disney"),
    ("3m", "wd1", "Search", "3M"),
    ("hpe", "wd5", "Jobsathpe", "HPE"),
    ("walmart", "wd5", "WalmartExternal", "Walmart"),
    ("hp", "wd5", "ExternalCareerSite", "HP"),
    ("dell", "wd1", "External", "Dell"),
    ("intel", "wd1", "External", "Intel"),
    ("paypal", "wd1", "jobs", "PayPal"),
    ("tmobile", "wd1", "External", "T-Mobile"),
    ("micron", "wd1", "External", "Micron"),
    ("baxter", "wd1", "baxter", "Baxter"),
    ("target", "wd5", "targetcareers", "Target"),
    ("autodesk", "wd1", "Ext", "Autodesk"),
    ("philips", "wd3", "jobs-and-careers", "Philips"),
    ("humana", "wd5", "Humana_External_Career_Site", "Humana"),
    ("boeing", "wd1", "EXTERNAL_CAREERS", "Boeing"),
    ("mastercard", "wd1", "CorporateCareers", "Mastercard"),
    ("truist", "wd1", "careers", "Truist"),
    ("pfizer", "wd1", "PfizerCareers", "Pfizer"),
    ("allstate", "wd5", "Allstate_Careers", "Allstate"),

    # ─── Speculative — candidate paths the resolver will probe at runtime.
    # When one of these returns 200, it gets cached for the process lifetime
    # so subsequent searches go straight to the right URL. When all candidates
    # 4xx, the tenant is skipped silently. Add tenants here freely — the
    # blast radius of a wrong guess is one extra HTTP error.
    ("servicenow", "wd1", ("ServiceNow", "External", "Careers"), "ServiceNow"),
    ("splunk", "wd1", ("splunk_jobs", "External", "Splunk_Careers"), "Splunk"),
    ("vmware", "wd1", ("VMware", "VMware_External", "External"), "VMware"),
    ("netapp", "wd1", ("NetApp_Careers", "External", "NetApp"), "NetApp"),
    ("cisco", "wd5", ("External_Career_Site", "Cisco", "1"), "Cisco"),
    ("usaa", "wd1", ("USAAJOBSWEBSITE", "USAA_Careers", "External"), "USAA"),
    ("mongodb", "wd1", ("mongodb", "External", "MongoDB_Careers"), "MongoDB"),
    ("mastercard", "wd5", ("mastercardcareers", "External"), "Mastercard"),
    ("vanguard", "wd5", ("Vanguard_External_Careers", "External"), "Vanguard"),
    ("fidelity", "wd1", ("fidelitycareers", "External"), "Fidelity"),
    ("vanguardcareers", "wd5", ("Vanguard_Careers", "External"), "Vanguard"),
    ("pnc", "wd5", ("PNC", "External", "PNCCareers"), "PNC"),
    ("usbank", "wd1", ("USBank_Careers", "USBank", "External"), "U.S. Bank"),
    ("usbank", "wd5", ("USBank", "External"), "U.S. Bank"),
    ("regions", "wd5", ("RegionsCareers", "External"), "Regions"),
    ("schwab", "wd1", ("schwab", "External", "schwabext"), "Charles Schwab"),
    ("synopsys", "wd5", ("Synopsys_Careers", "External"), "Synopsys"),
    ("cadence", "wd5", ("External_Career_Site", "External"), "Cadence"),
    ("cdw", "wd5", ("CDW", "External"), "CDW"),
    ("redhat", "wd5", ("redhat", "External"), "Red Hat"),
    ("activision", "wd1", ("External", "Activision_Careers"), "Activision Blizzard"),
    ("lowes", "wd1", ("lowes", "External", "Lowes_Careers"), "Lowe's"),
    ("lowes", "wd5", ("lowes", "External"), "Lowe's"),
    ("bestbuy", "wd1", ("BestBuy", "External", "bestbuy"), "Best Buy"),
    ("bestbuy", "wd5", ("bestbuy", "BestBuy", "External"), "Best Buy"),
    ("nordstrom", "wd5", ("nordstrom_careers", "External"), "Nordstrom"),
    ("pepsico", "wd5", ("PepsiCoJobs", "External", "PepsiCoCareers"), "PepsiCo"),
    ("pepsico", "wd1", ("PepsiCoCareers", "External"), "PepsiCo"),
    ("mondelezinternational", "wd3", ("mondelezinternational_careers", "External"), "Mondelēz"),
    ("kraftheinz", "wd1", ("KraftHeinz", "External"), "Kraft Heinz"),
    ("loreal", "wd3", ("lorealcareers", "External"), "L'Oréal"),
    ("aramark", "wd5", ("Aramark_Career", "External"), "Aramark"),
    ("nielsen", "wd1", ("Nielsen_External_Careers", "External"), "Nielsen"),
    ("expediagroup", "wd5", ("ExpediaGroup", "External"), "Expedia Group"),
    ("marriott", "wd1", ("Marriott_External", "External"), "Marriott"),
    ("marriott", "wd5", ("marriott", "External"), "Marriott"),
    ("teradata", "wd5", ("Teradata", "External"), "Teradata"),
    ("zebra", "wd5", ("Zebra_Careers", "External"), "Zebra"),
    ("stryker", "wd1", ("Stryker_Careers", "External"), "Stryker"),
    ("boozallen", "wd1", ("BAH_External_Career_Site", "External"), "Booz Allen"),
    ("eaton", "wd1", ("Eaton_External", "External"), "Eaton"),
    ("gm", "wd1", ("GM", "External"), "GM"),
    ("ford", "wd1", ("Ford", "External"), "Ford"),
    ("deere", "wd1", ("JohnDeere", "External"), "John Deere"),
    ("cat", "wd1", ("caterpillar", "External"), "Caterpillar"),
    ("ge", "wd1", ("ge_career", "GE_External_Career", "External"), "GE"),
    ("cvshealth", "wd5", ("cvshealth", "External"), "CVS Health"),
    ("walgreens", "wd5", ("wba", "External"), "Walgreens"),
    ("unitedhealthgroup", "wd5", ("unh", "External"), "UnitedHealth Group"),
    ("centene", "wd5", ("ExternalCareer", "External"), "Centene"),
    ("aetna", "wd5", ("aetna", "External"), "Aetna"),
    ("cigna", "wd5", ("cigna_careers", "Cigna_Jobs", "External"), "Cigna"),
    ("elevancehealth", "wd1", ("ElevanceHealth", "External"), "Elevance Health"),
    ("abbvie", "wd5", ("abbvie", "External"), "AbbVie"),
    ("mckesson", "wd5", ("External_Career", "External"), "McKesson"),
    ("bostonscientific", "wd5", ("BSCCareers", "External"), "Boston Scientific"),
    ("bd", "wd5", ("BD", "External"), "BD"),
    ("medtronic", "wd5", ("MedtronicCareers", "External"), "Medtronic"),
    ("regeneron", "wd1", ("Regeneron_External_Career_Site", "External"), "Regeneron"),
    ("ihg", "wd1", ("IHGCareers", "External"), "IHG"),
    ("fedex", "wd1", ("External", "FXE"), "FedEx"),
    ("ups", "wd5", ("UPSCareers", "ups", "External"), "UPS"),
    ("att", "wd5", ("ATTEXTERNAL", "External"), "AT&T"),
    ("comcast", "wd5", ("Comcast_Careers", "External"), "Comcast"),
    ("verizon", "wd12", ("careers", "Careers", "External"), "Verizon"),
    ("nbcuni", "wd1", ("nbcunicareers", "External"), "NBCUniversal"),
    ("wbd", "wd5", ("wbd_career", "External"), "Warner Bros. Discovery"),
    ("aig", "wd5", ("AIG_PROD", "External"), "AIG"),
    ("principal", "wd5", ("Principal_Careers", "External"), "Principal"),
    ("nationwide", "wd1", ("Nationwide_External", "External"), "Nationwide"),
    ("prudential", "wd5", ("Prudential_Career", "External"), "Prudential"),
    ("fiserv", "wd5", ("Fiserv", "External"), "Fiserv"),
    ("aexp", "wd1", ("AmericanExpressCareers", "AmericanExpress", "External"), "American Express"),
    ("wellsfargo", "wd1", ("Wells_Fargo_Jobs", "External", "wellsfargojobs"), "Wells Fargo"),
    ("bankofamerica", "wd1", ("1", "External", "BofA_Careers"), "Bank of America"),
    ("visa", "wd1", ("visa", "External"), "Visa"),
    ("honeywell", "wd1", ("1", "External", "HoneywellCareers"), "Honeywell"),
    ("westerndigital", "wd5", ("Western_Digital_External_Careers", "External"), "Western Digital"),
    ("macys", "wd5", ("Macys_Jobs", "External"), "Macy's"),
    ("kohls", "wd5", ("kohls", "External"), "Kohl's"),
    ("emerson", "wd5", ("Emerson", "External"), "Emerson"),
    ("equinix", "wd1", ("equinix_careers", "External"), "Equinix"),
]


def _load_tenants() -> List[TenantSpec]:
    """Allow operators to extend / override the tenant list via env."""
    raw = _get_config_value("WORKDAY_TENANTS", "")
    if not raw:
        return DEFAULT_WORKDAY_TENANTS
    try:
        parsed = json.loads(raw)
        out: List[TenantSpec] = []
        for row in parsed:
            if isinstance(row, list) and len(row) == 4:
                tenant, host, site, display = row
                # Accept either a string or a list of candidates.
                if isinstance(site, list):
                    site = tuple(str(s) for s in site)
                else:
                    site = str(site)
                out.append((str(tenant), str(host), site, str(display)))
        return out + DEFAULT_WORKDAY_TENANTS if out else DEFAULT_WORKDAY_TENANTS
    except Exception as e:
        logger.warning("WORKDAY_TENANTS env override failed to parse (%s); falling back to defaults", e)
        return DEFAULT_WORKDAY_TENANTS


_HEADERS = {
    # Workday CXS rejects requests with no Accept/Content-Type or with a
    # python-requests UA on some clusters. Pretend to be a modern browser.
    "Accept": "application/json,application/xml",
    "Content-Type": "application/json",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 12_0) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
}

# Process-lifetime cache: (tenant, host) → resolved working site path. The
# first request that succeeds for a multi-candidate tenant fills this in; all
# subsequent requests skip the failed-path attempts.
_RESOLVED_SITES: Dict[Tuple[str, str], str] = {}
_RESOLVED_LOCK = Lock()


def _candidates_for(spec_site: Union[str, Sequence[str]]) -> List[str]:
    """Normalize the `site` field to an ordered list of candidates."""
    if isinstance(spec_site, str):
        return [spec_site]
    return [s for s in spec_site if s]


def _resolve_site(
    tenant: str,
    host: str,
    candidates: List[str],
    timeout: int,
) -> Optional[Tuple[str, Dict[str, Any]]]:
    """Probe candidate site paths and return (winning_site, response_json).

    On a successful 200 we cache the (tenant, host) → site mapping so the
    next call goes straight to the right URL. On exhaustion of candidates we
    return None and the tenant is silently skipped for this run.
    """
    key = (tenant, host)
    with _RESOLVED_LOCK:
        cached = _RESOLVED_SITES.get(key)
    if cached and cached in candidates:
        # Reorder so the cached path is tried first.
        candidates = [cached] + [c for c in candidates if c != cached]

    for site in candidates:
        url = f"https://{tenant}.{host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
        body = {"searchText": "", "locations": [], "appliedFacets": {}, "limit": 1, "offset": 0}
        try:
            resp = requests.post(url, json=body, headers=_HEADERS, timeout=timeout)
        except requests.RequestException as e:
            logger.debug("Workday CXS %s/%s/%s probe network error: %s", tenant, host, site, e)
            continue
        if resp.status_code == 200:
            try:
                data = resp.json()
            except ValueError:
                continue
            with _RESOLVED_LOCK:
                _RESOLVED_SITES[key] = site
            return site, data
        # 404 = wrong path; 422 = path may exist but CXS gated; keep trying.
        logger.debug("Workday CXS %s/%s/%s probe %s", tenant, host, site, resp.status_code)
    return None


def _fetch_tenant(
    tenant: str,
    host: str,
    site_spec: Union[str, Sequence[str]],
    display: str,
    search_text: str,
    location: str,
    limit: int = 20,
    timeout: int = 8,
) -> List[Dict[str, Any]]:
    candidates = _candidates_for(site_spec)
    if not candidates:
        return []
    # First confirm a working site path (uses limit=1 probe so it's cheap),
    # then issue the real search against the resolved path.
    resolved = _resolve_site(tenant, host, candidates, timeout=timeout)
    if not resolved:
        return []
    site, _probe = resolved

    base = f"https://{tenant}.{host}.myworkdayjobs.com"
    url = f"{base}/wday/cxs/{tenant}/{site}/jobs"
    body = {
        "searchText": search_text or "",
        "locations": [],
        "appliedFacets": {},
        "limit": limit,
        "offset": 0,
    }
    try:
        resp = requests.post(url, json=body, headers=_HEADERS, timeout=timeout)
    except requests.RequestException as e:
        logger.debug("Workday CXS %s: network error %s", tenant, e)
        return []
    if not resp.ok:
        logger.debug("Workday CXS %s: %s %s", tenant, resp.status_code, resp.reason)
        return []
    try:
        data = resp.json()
    except ValueError:
        return []
    postings = data.get("jobPostings") or []
    location_lc = (location or "").strip().lower()
    out: List[Dict[str, Any]] = []
    for jp in postings:
        if not isinstance(jp, dict):
            continue
        title = (jp.get("title") or "").strip()
        if not title:
            continue
        loc_text = (jp.get("locationsText") or "").strip()
        if location_lc and location_lc not in {"united states", "us", "usa"}:
            haystack = loc_text.lower()
            if location_lc not in haystack and "remote" not in haystack:
                continue
        path = (jp.get("externalPath") or "").lstrip("/")
        apply_link = (
            f"{base}/en-US/{site}/{path}" if path else
            jp.get("externalPath") or ""
        )
        posted_on = jp.get("postedOn") or ""
        posted_iso = _approx_iso_from_relative(posted_on)
        req_id = jp.get("bulletFields", [None])[0] or jp.get("workdayJobId") or path
        job_id_seed = f"workday_direct|{tenant}|{req_id}|{title}".lower()
        job_id = "workday_direct:" + hashlib.md5(job_id_seed.encode()).hexdigest()[:24]
        out.append({
            "job_id": job_id,
            "title": title,
            "company": display,
            "logo": "",
            "location": loc_text or "Not specified",
            "apply_link": apply_link,
            "description": "",
            "salary": "",
            "employment_type": "",
            "date_posted": posted_iso,
            "posted_text": posted_on,
            "is_remote": "remote" in loc_text.lower(),
            "h1b_sponsor": False,
            "contract_friendly": False,
            "source": "Workday Direct",
        })
    return out


_DAY_RE = re.compile(r"(\d+)\+?\s+day", re.IGNORECASE)
_MONTH_RE = re.compile(r"(\d+)\+?\s+month", re.IGNORECASE)


def _approx_iso_from_relative(posted_on: str) -> str:
    """Convert "Posted 2 Days Ago" into an approximate ISO date."""
    text = (posted_on or "").strip().lower()
    if not text:
        return ""
    now = datetime.now(timezone.utc)
    if "today" in text or "just" in text:
        return now.isoformat()
    if "yesterday" in text:
        return (now - timedelta(days=1)).isoformat()
    m = _DAY_RE.search(text)
    if m:
        return (now - timedelta(days=int(m.group(1)))).isoformat()
    m = _MONTH_RE.search(text)
    if m:
        return (now - timedelta(days=int(m.group(1)) * 30)).isoformat()
    return ""


def fetch_workday_direct(
    search_text: str,
    location: str = "",
    tenants: Optional[List[TenantSpec]] = None,
    per_tenant_limit: int = 20,
    overall_timeout: int = 15,
    max_workers: int = 32,
) -> List[Dict[str, Any]]:
    """Fan out to every configured Workday tenant in parallel.

    Returns a flat list of normalized job dicts. Individual tenant failures
    are swallowed at debug level — a partial result is far better than a hard
    error when one of N tenants changes its URL path. Multi-candidate
    tenants self-resolve their site path on first call and cache it for the
    process lifetime.
    """
    tenant_list = tenants if tenants is not None else _load_tenants()
    if not tenant_list:
        return []
    out: List[Dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [
            pool.submit(
                _fetch_tenant,
                tenant, host, site_spec, display,
                search_text, location, per_tenant_limit,
            )
            for tenant, host, site_spec, display in tenant_list
        ]
        # Critical: when overall_timeout expires before every future is done,
        # as_completed raises TimeoutError on its next iteration. We absorb
        # it and return what we have so a single slow Workday tenant can't
        # kill the entire workday_direct source for the whole search.
        try:
            for fut in as_completed(futures, timeout=overall_timeout):
                try:
                    out.extend(fut.result(timeout=overall_timeout))
                except Exception as e:
                    logger.debug("Workday CXS tenant fetch failed: %s", e)
        except TimeoutError:
            done = sum(1 for f in futures if f.done())
            logger.info(
                "[workday-direct] overall timeout hit; returning partial result "
                "(%d/%d tenants completed)", done, len(tenant_list),
            )
            for f in futures:
                if not f.done():
                    f.cancel()
    logger.info(
        "[workday-direct] fetched %d total postings across %d tenants (cache size=%d)",
        len(out), len(tenant_list), len(_RESOLVED_SITES),
    )
    return out


def resolved_site_map() -> Dict[Tuple[str, str], str]:
    """Snapshot of the (tenant, host) → working site cache (for diagnostics)."""
    with _RESOLVED_LOCK:
        return dict(_RESOLVED_SITES)
