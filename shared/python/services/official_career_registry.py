"""The employer's own words about where its jobs are.

Why a registry of URLs, and why it is not truth
-----------------------------------------------
Ownership verification needs a starting point that did not come from us
guessing. This file is that starting point: for each employer, the career
landing URL the employer publishes. `career_source_ownership` fetches it,
follows the redirects, and derives the ATS endpoint from what it finds.

A row here therefore asserts exactly one thing — "on the date recorded, this
employer published this careers URL". It does NOT assert:

  * which ATS they use (derived, then owner-verified);
  * that the URL still resolves (revalidated on a schedule; `moved` is a
    first-class state precisely because these rot);
  * that they are hiring (that is content validation);
  * that the employer still exists under this name (that is the employer
    registry's `operating_status`).

`verified_at` is the date the URL was last confirmed by a live fetch, and it
is written by `scripts/verify_official_career_urls.py`, never by hand. A row
whose `verified_at` is absent has never been confirmed and is treated as a
lead, not as evidence.

Config-driven custom boards
---------------------------
Several large employers run their own board with a public JSON/XHR API and no
third-party ATS. Historically that meant a company-specific branch in the
crawler. `CUSTOM_BOARD_ADAPTERS` replaces those branches with data: a row
describes the request to make, where the records live in the response, and
which field is which. `ats_providers.config_boards.ConfigBoardProvider` reads
it, so adding an employer is a dict, not a code path, and a broken employer
cannot take the crawler down with it.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

REGISTRY_VERSION = "official-career-registry-1.0.0"


def _row(display_name: str, domain: str, career_url: str, *,
         cohorts: Sequence[str] = (), categories: Sequence[str] = (),
         notes: str = "") -> Dict[str, Any]:
    return {"display_name": display_name, "domain": domain,
            "career_landing_url": career_url,
            "cohorts": list(cohorts), "categories": list(categories),
            "notes": notes, "verified_at": None}


#: The URL registry supplied with the coverage brief, plus the employers whose
#: absence the live audit named. Ordered by cohort so the file reads as the
#: shortlist it is, not as an alphabetical dump.
OFFICIAL_CAREER_URLS: Tuple[Dict[str, Any], ...] = (
    # --- Named in the brief -------------------------------------------------
    _row("Google", "google.com",
         "https://www.google.com/about/careers/applications/jobs/results/",
         cohorts=("mnc", "public_tech"), categories=("software_saas", "ai_ml")),
    _row("Apple", "apple.com", "https://jobs.apple.com/en-us/search",
         cohorts=("mnc", "public_tech"), categories=("consumer", "hardware")),
    _row("Meta", "meta.com", "https://www.metacareers.com/jobs/",
         cohorts=("mnc", "public_tech"), categories=("consumer", "ai_ml")),
    _row("Microsoft", "microsoft.com", "https://apply.careers.microsoft.com/careers",
         cohorts=("mnc", "public_tech"), categories=("software_saas", "cloud_data")),
    _row("IBM", "ibm.com", "https://www.ibm.com/careers/search",
         cohorts=("mnc", "public_tech"), categories=("it_services", "cloud_data")),
    _row("SAP", "sap.com", "https://jobs.sap.com/",
         cohorts=("mnc", "public_tech"), categories=("software_saas",)),
    _row("Salesforce", "salesforce.com",
         "https://www.salesforce.com/company/careers/jobs/",
         cohorts=("mnc", "public_tech"), categories=("software_saas",)),
    _row("Tesla", "tesla.com", "https://www.tesla.com/careers/search/",
         cohorts=("mnc", "public_tech"), categories=("automotive", "robotics")),
    _row("Atlassian", "atlassian.com",
         "https://www.atlassian.com/company/careers/all-jobs",
         cohorts=("public_tech",), categories=("software_saas", "devtools")),
    _row("Shopify", "shopify.com", "https://www.shopify.com/careers",
         cohorts=("public_tech",), categories=("ecommerce", "software_saas")),
    _row("DigitalOcean", "digitalocean.com",
         "https://www.digitalocean.com/careers/open-roles",
         cohorts=("public_tech",), categories=("cloud_data",)),
    _row("Procore", "procore.com", "https://careers.procore.com/jobs/search",
         cohorts=("public_tech",), categories=("software_saas",)),
    _row("monday.com", "monday.com", "https://monday.com/careers",
         cohorts=("public_tech",), categories=("software_saas",)),
    _row("Fortinet", "fortinet.com", "https://www.fortinet.com/corporate/careers",
         cohorts=("public_tech",), categories=("security",)),
    _row("SentinelOne", "sentinelone.com", "https://www.sentinelone.com/jobs/",
         cohorts=("public_tech",), categories=("security", "ai_ml")),
    _row("TSMC", "tsmc.com",
         "https://www.tsmc.com/static/english/careers/index.htm",
         cohorts=("mnc",), categories=("semiconductor",)),
    _row("Synopsys", "synopsys.com", "https://careers.synopsys.com/",
         cohorts=("public_tech",), categories=("semiconductor",)),
    _row("Keysight", "keysight.com", "https://careers.keysight.com/external",
         cohorts=("public_tech",), categories=("hardware", "semiconductor")),
    _row("Teradyne", "teradyne.com", "https://jobs.teradyne.com/viewalljobs/",
         cohorts=("public_tech",), categories=("semiconductor", "robotics")),
    _row("Garmin", "garmin.com", "https://careers.garmin.com/jobs",
         cohorts=("public_tech",), categories=("hardware",)),
    _row("Dell Technologies", "dell.com", "https://jobs.dell.com/",
         cohorts=("mnc", "public_tech"), categories=("hardware", "cloud_data")),
    _row("Etsy", "etsy.com", "https://careers.etsy.com/home",
         cohorts=("public_tech",), categories=("ecommerce",)),
    _row("Booking.com", "booking.com", "https://jobs.booking.com/",
         cohorts=("mnc", "public_tech"), categories=("consumer", "software_saas")),
    _row("EPAM Systems", "epam.com", "https://www.epam.com/careers/job-listings",
         cohorts=("mnc", "public_tech"), categories=("it_services",)),
    _row("Rivian", "rivian.com", "https://careers.rivian.com/work/jobs/categories",
         cohorts=("public_tech",), categories=("automotive",),
         notes="iCIMS tenant us-careers-rivian returns a 143-byte redirect; "
               "re-fingerprint from this landing URL"),
    _row("Snap", "snap.com", "https://careers.snap.com/jobs",
         cohorts=("public_tech",), categories=("consumer",)),
    _row("Klarna", "klarna.com", "https://www.klarna.com/careers/",
         cohorts=("private_growth",), categories=("fintech",)),
    _row("Whatnot", "whatnot.com", "https://www.whatnot.com/careers/discover",
         cohorts=("private_growth",), categories=("ecommerce",)),

    # --- Named by the live audit as absent or under-crawled ------------------
    _row("Amazon", "amazon.com", "https://www.amazon.jobs/en/",
         cohorts=("mnc", "public_tech"), categories=("ecommerce", "cloud_data")),
    _row("Netflix", "netflix.com", "https://explore.jobs.netflix.net/careers",
         cohorts=("public_tech",), categories=("media",)),
    _row("Oracle", "oracle.com", "https://careers.oracle.com/jobs/",
         cohorts=("mnc", "public_tech"), categories=("software_saas", "cloud_data")),
    _row("Cisco", "cisco.com", "https://jobs.cisco.com/jobs/SearchJobs",
         cohorts=("mnc", "public_tech"), categories=("hardware", "telecom")),
    _row("Adobe", "adobe.com", "https://careers.adobe.com/us/en/search-results",
         cohorts=("public_tech",), categories=("software_saas",)),
    _row("Intel", "intel.com", "https://jobs.intel.com/en/search-jobs",
         cohorts=("mnc", "public_tech"), categories=("semiconductor",)),
    _row("Qualcomm", "qualcomm.com", "https://careers.qualcomm.com/careers",
         cohorts=("public_tech",), categories=("semiconductor",)),
    _row("Broadcom", "broadcom.com", "https://www.broadcom.com/company/careers",
         cohorts=("public_tech",), categories=("semiconductor",)),
    _row("VMware", "vmware.com", "https://www.broadcom.com/company/careers",
         cohorts=("public_tech",), categories=("cloud_data",),
         notes="acquired by Broadcom; identity resolves to Broadcom"),
    _row("Uber", "uber.com", "https://www.uber.com/us/en/careers/list/",
         cohorts=("public_tech",), categories=("consumer",)),
    _row("Lyft", "lyft.com", "https://www.lyft.com/careers",
         cohorts=("public_tech",), categories=("consumer",)),
    _row("Airbnb", "airbnb.com", "https://careers.airbnb.com/",
         cohorts=("public_tech",), categories=("consumer",)),
    _row("Spotify", "spotify.com", "https://www.lifeatspotify.com/jobs",
         cohorts=("public_tech",), categories=("media",)),
    _row("HubSpot", "hubspot.com", "https://www.hubspot.com/careers/jobs",
         cohorts=("public_tech",), categories=("software_saas",)),
    _row("Splunk", "splunk.com", "https://www.splunk.com/en_us/careers.html",
         cohorts=("public_tech",), categories=("cloud_data", "security")),
    _row("Zoom", "zoom.com", "https://careers.zoom.us/jobs/search",
         cohorts=("public_tech",), categories=("software_saas",)),
    _row("Intuit", "intuit.com", "https://jobs.intuit.com/search-jobs",
         cohorts=("public_tech",), categories=("fintech", "software_saas")),
    _row("Confluent", "confluent.io", "https://careers.confluent.io/jobs",
         cohorts=("public_tech",), categories=("cloud_data",)),
    _row("Arm", "arm.com", "https://careers.arm.com/search-jobs",
         cohorts=("public_tech",), categories=("semiconductor",)),
    _row("Seagate", "seagate.com", "https://www.seagate.com/careers/",
         cohorts=("public_tech",), categories=("hardware",)),
    _row("Lam Research", "lamresearch.com", "https://careers.lamresearch.com/",
         cohorts=("public_tech",), categories=("semiconductor",)),
    _row("Siemens", "siemens.com", "https://jobs.siemens.com/careers",
         cohorts=("mnc",), categories=("hardware", "automotive")),
    _row("Infosys", "infosys.com", "https://career.infosys.com/joblist",
         cohorts=("mnc",), categories=("it_services",)),
    _row("Wipro", "wipro.com", "https://careers.wipro.com/careers-home/jobs",
         cohorts=("mnc",), categories=("it_services",)),
    _row("Cognizant", "cognizant.com", "https://careers.cognizant.com/global-en/jobs/",
         cohorts=("mnc",), categories=("it_services",)),
    _row("Capgemini", "capgemini.com", "https://www.capgemini.com/careers/join-capgemini/job-search/",
         cohorts=("mnc",), categories=("it_services",)),
    _row("Accenture", "accenture.com", "https://www.accenture.com/us-en/careers/jobsearch",
         cohorts=("mnc",), categories=("it_services",)),
    _row("Aurora Innovation", "aurora.tech", "https://aurora.tech/careers",
         cohorts=("public_tech",), categories=("robotics", "automotive")),
    _row("Goldman Sachs", "goldmansachs.com",
         "https://www.goldmansachs.com/careers/our-firm/opportunities",
         cohorts=("mnc",), categories=("fintech",)),
    _row("Bloomberg", "bloomberg.com", "https://careers.bloomberg.com/job/search",
         cohorts=("mnc",), categories=("fintech", "media")),
    _row("Nutanix", "nutanix.com", "https://www.nutanix.com/company/careers",
         cohorts=("public_tech",), categories=("cloud_data",)),
    _row("NetApp", "netapp.com", "https://careers.netapp.com/search-jobs/",
         cohorts=("public_tech",), categories=("cloud_data", "hardware")),
    _row("Akamai", "akamai.com", "https://akamaicareers.inflightcloud.com/search",
         cohorts=("public_tech",), categories=("cloud_data", "security")),
    _row("X", "x.com", "https://careers.x.com/en",
         cohorts=("private_growth",), categories=("consumer",)),
    _row("Blue Origin", "blueorigin.com", "https://www.blueorigin.com/careers",
         cohorts=("private_growth",), categories=("aerospace_defense",)),
    _row("Deel", "deel.com", "https://www.deel.com/careers/",
         cohorts=("private_growth",), categories=("software_saas",)),
    _row("Palantir", "palantir.com", "https://www.palantir.com/careers/",
         cohorts=("public_tech",), categories=("software_saas", "ai_ml")),
    _row("F5", "f5.com", "https://www.f5.com/company/careers",
         cohorts=("public_tech",), categories=("security", "cloud_data")),
    _row("Unity", "unity.com", "https://careers.unity.com/",
         cohorts=("public_tech",), categories=("gaming", "devtools")),
    _row("SpaceX", "spacex.com", "https://www.spacex.com/careers/jobs/",
         cohorts=("private_growth",), categories=("aerospace_defense",)),
    _row("Zoox", "zoox.com", "https://zoox.com/careers/",
         cohorts=("private_growth",), categories=("robotics", "automotive")),
    _row("Rippling", "rippling.com", "https://www.rippling.com/careers/open-roles",
         cohorts=("private_growth",), categories=("software_saas",)),

    # --- Gaming, an acceptance cohort with no seed rows before this ----------
    _row("Electronic Arts", "ea.com", "https://jobs.ea.com/en_US/careers",
         cohorts=("public_tech",), categories=("gaming",)),
    _row("Roblox", "roblox.com", "https://careers.roblox.com/jobs",
         cohorts=("public_tech",), categories=("gaming",)),
    _row("Riot Games", "riotgames.com", "https://www.riotgames.com/en/work-with-us",
         cohorts=("private_growth",), categories=("gaming",)),
    _row("Epic Games", "epicgames.com", "https://www.epicgames.com/site/en-US/careers",
         cohorts=("private_growth",), categories=("gaming",)),
    _row("Take-Two Interactive", "take2games.com",
         "https://www.take2games.com/careers", cohorts=("public_tech",),
         categories=("gaming",)),
    _row("Ubisoft", "ubisoft.com", "https://www.ubisoft.com/en-us/company/careers",
         cohorts=("mnc",), categories=("gaming",)),
    _row("Valve", "valvesoftware.com", "https://www.valvesoftware.com/en/jobs",
         cohorts=("private_growth",), categories=("gaming",)),
    _row("Bungie", "bungie.net", "https://www.bungie.net/7/en/Careers",
         cohorts=("private_growth",), categories=("gaming",)),
    _row("Discord", "discord.com", "https://discord.com/careers",
         cohorts=("private_growth",), categories=("gaming", "consumer")),

    # --- The audit candidates: status must be verified before they are used --
    _row("DeepL", "deepl.com", "https://jobs.deepl.com/",
         cohorts=("private_growth",), categories=("ai_ml",)),
    _row("Pydantic", "pydantic.dev", "https://pydantic.dev/careers",
         cohorts=("niche_startup",), categories=("devtools",),
         notes="audit candidate - confirm public hiring before counting"),
    _row("Turso", "turso.tech", "https://turso.tech/careers",
         cohorts=("niche_startup",), categories=("cloud_data", "devtools"),
         notes="audit candidate"),
    _row("Wasmer", "wasmer.io", "https://wasmer.io/careers",
         cohorts=("niche_startup",), categories=("devtools",),
         notes="audit candidate"),
    _row("Deno", "deno.com", "https://deno.com/jobs",
         cohorts=("niche_startup",), categories=("devtools",),
         notes="audit candidate"),
    _row("Oven (Bun)", "bun.sh", "https://bun.sh/careers",
         cohorts=("niche_startup",), categories=("devtools",),
         notes="audit candidate - legal entity is Oven"),
    _row("Oxide Computer", "oxide.computer", "https://oxide.computer/careers",
         cohorts=("niche_startup",), categories=("hardware", "cloud_data"),
         notes="audit candidate"),
    _row("Aikido Security", "aikido.dev", "https://www.aikido.dev/careers",
         cohorts=("niche_startup",), categories=("security",),
         notes="audit candidate"),
    _row("Protect AI", "protectai.com", "https://protectai.com/careers",
         cohorts=("niche_startup",), categories=("security", "ai_ml"),
         notes="audit candidate - acquired by Palo Alto Networks 2025; "
               "confirm operating_status before counting as an employer"),
    _row("Lakera", "lakera.ai", "https://www.lakera.ai/careers",
         cohorts=("niche_startup",), categories=("security", "ai_ml"),
         notes="audit candidate - acquired by Check Point 2025; confirm status"),
    _row("HiddenLayer", "hiddenlayer.com", "https://hiddenlayer.com/careers/",
         cohorts=("niche_startup",), categories=("security", "ai_ml"),
         notes="audit candidate"),
    _row("Ampere Computing", "amperecomputing.com",
         "https://amperecomputing.com/careers", cohorts=("private_growth",),
         categories=("semiconductor",), notes="audit candidate"),
    _row("Celestial AI", "celestial.ai", "https://www.celestial.ai/careers",
         cohorts=("niche_startup",), categories=("semiconductor", "ai_ml"),
         notes="audit candidate"),
    _row("d-Matrix", "d-matrix.ai", "https://www.d-matrix.ai/careers/",
         cohorts=("niche_startup",), categories=("semiconductor", "ai_ml"),
         notes="audit candidate"),
    _row("Enfabrica", "enfabrica.net", "https://www.enfabrica.net/careers",
         cohorts=("niche_startup",), categories=("semiconductor", "hardware"),
         notes="audit candidate"),
)


def by_domain() -> Dict[str, Dict[str, Any]]:
    return {r["domain"]: r for r in OFFICIAL_CAREER_URLS if r.get("domain")}


def by_name_key() -> Dict[str, Dict[str, Any]]:
    from services.employer_registry import name_key
    return {name_key(r["display_name"]): r for r in OFFICIAL_CAREER_URLS}


def lookup(*, domain: Any = None, name: Any = None) -> Optional[Dict[str, Any]]:
    """The registry row for an employer, domain first."""
    from services.employer_registry import name_key, normalize_domain
    d = normalize_domain(domain)
    if d:
        hit = by_domain().get(d)
        if hit:
            return hit
    k = name_key(name)
    if k:
        return by_name_key().get(k)
    return None


# ---------------------------------------------------------------------------
# Config-driven custom boards
# ---------------------------------------------------------------------------
#: One row = one employer-run board with a public JSON API. Read by
#: `ats_providers.config_boards.ConfigBoardProvider`.
#:
#: `records_path` / `total_path` are dotted paths into the decoded JSON, where
#: a numeric segment indexes a list. `fields` maps our canonical `RawJob`
#: attribute to a path in one record; a list of paths is tried in order and
#: the first non-empty wins, so a provider that renamed a field does not need
#: a new adapter.
#:
#: `pagination.style`:
#:   `page`   - `params[page_param] = page_index` from `page_start`
#:   `offset` - `params[offset_param] = page_index * page_size`
#:   `none`   - a single request returns everything
CUSTOM_BOARD_ADAPTERS: Tuple[Dict[str, Any], ...] = (
    {
        "key": "apple",
        "display_name": "Apple",
        "domain": "apple.com",
        "vendor": "custom_apple",
        "career_landing_url": "https://jobs.apple.com/en-us/search",
        "request": {
            "method": "POST",
            "url": "https://jobs.apple.com/api/role/search",
            "json_body": {"query": "", "filters": {}, "page": 1,
                          "locale": "en-us", "sort": "newest"},
            "page_field": "page",
        },
        "pagination": {"style": "page", "page_start": 1, "max_pages": 20},
        "records_path": "searchResults",
        "total_path": "totalRecords",
        "fields": {
            "source_job_id": ["positionId", "id"],
            "title": ["postingTitle", "title"],
            "location": ["locations.0.name", "postingLocation"],
            "posted_at": ["postDateInGMT", "postingDate"],
            "description": ["jobSummary"],
            "apply_url_template": "https://jobs.apple.com/en-us/details/{source_job_id}",
        },
    },
    {
        "key": "tesla",
        "display_name": "Tesla",
        "domain": "tesla.com",
        "vendor": "custom_tesla",
        "career_landing_url": "https://www.tesla.com/careers/search/",
        "request": {
            "method": "GET",
            "url": "https://www.tesla.com/cua-api/apps/careers/state",
        },
        "pagination": {"style": "none"},
        "records_path": "listings",
        "fields": {
            "source_job_id": ["id"],
            "title": ["t", "title"],
            "location": ["l", "location"],
            "department": ["dp", "department"],
            "apply_url_template": "https://www.tesla.com/careers/search/job/{source_job_id}",
        },
    },
    {
        "key": "spacex",
        "display_name": "SpaceX",
        "domain": "spacex.com",
        "vendor": "custom_spacex",
        "career_landing_url": "https://www.spacex.com/careers/jobs/",
        "request": {"method": "GET", "url": "https://boards-api.greenhouse.io/v1/boards/spacex/jobs",
                    "params": {"content": "true"}},
        "pagination": {"style": "none"},
        "records_path": "jobs",
        "fields": {
            "source_job_id": ["id"],
            "title": ["title"],
            "location": ["location.name"],
            "posted_at": ["updated_at"],
            "description": ["content"],
            "apply_url": ["absolute_url"],
        },
        "notes": "SpaceX publishes through Greenhouse; kept here so the "
                 "official landing URL and the endpoint stay joined.",
    },
)


def custom_adapter_for(key_or_domain: Any) -> Optional[Dict[str, Any]]:
    k = str(key_or_domain or "").strip().lower()
    for row in CUSTOM_BOARD_ADAPTERS:
        if k in (row["key"], row.get("domain", "")):
            return row
    return None


def dig(payload: Any, path: Any) -> Any:
    """Follow a dotted path into decoded JSON; numeric segments index lists.

    Returns `None` for any miss rather than raising, because a provider that
    changed its response shape must degrade to "field absent", never to a
    crawl-wide exception.
    """
    if path in (None, ""):
        return None
    cur = payload
    for seg in str(path).split("."):
        if cur is None:
            return None
        if isinstance(cur, dict):
            cur = cur.get(seg)
        elif isinstance(cur, (list, tuple)):
            if not re.fullmatch(r"\d+", seg):
                return None
            idx = int(seg)
            cur = cur[idx] if 0 <= idx < len(cur) else None
        else:
            return None
    return cur


def first_present(payload: Any, paths: Sequence[Any]) -> Any:
    for p in paths or ():
        got = dig(payload, p)
        if got not in (None, "", [], {}):
            return got
    return None
