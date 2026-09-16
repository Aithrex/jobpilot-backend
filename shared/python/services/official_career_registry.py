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

That sentence was aspirational until now. The script it names did not exist,
`_row` hard-coded `verified_at: None`, and nothing could ever have written a
different value — the field asserted a verification workflow that was not
there. It is now READ from `official_career_url_checks`, one row per domain,
written only by that script and only from an actual fetch. A constant cannot
record an event, so the event lives in the database and the constant is joined
to it: `verified_rows()` returns the registry with real verification stamps,
and `OFFICIAL_CAREER_URLS` keeps returning `None` for a domain nothing has
ever checked, which is the truthful answer.

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


#: The 94 employers the coverage target was set against. Rows added later
#: carry their own batch key, so "75 of 94" stays a checkable claim rather
#: than a number whose denominator quietly grew.
BATCH_BASELINE = "baseline"
BATCH_AUDIT_2026_09_02 = "audit-2026-09-02"
BATCH_INTAKE_2026_09_03 = "intake-2026-09-03"


def _row(display_name: str, domain: str, career_url: str, *,
         cohorts: Sequence[str] = (), categories: Sequence[str] = (),
         notes: str = "", batch: str = BATCH_BASELINE) -> Dict[str, Any]:
    return {"display_name": display_name, "domain": domain,
            "career_landing_url": career_url,
            "cohorts": list(cohorts), "categories": list(categories),
            "notes": notes, "batch": batch, "verified_at": None}


def rows_in_batch(batch: str = BATCH_BASELINE) -> List[Dict[str, Any]]:
    """The registry rows belonging to one accountability batch."""
    return [dict(r) for r in OFFICIAL_CAREER_URLS
            if str(r.get("batch") or BATCH_BASELINE) == batch]


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
         notes="the VMware careers URL redirects to Broadcom's, which is "
               "documentary; the acquisition itself is not asserted here. "
               "`operating_status` is set by evidence, not by this note."),
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
         notes="audit candidate. Probed 2026-09-02: domain confirmed, careers URL 200."),
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
         notes="audit candidate. Probed 2026-09-02: domain confirmed, careers "
               "URL 404. An acquisition is the likely cause and is NOT "
               "verified here - `operating_status` stays `active` until it "
               "is, because guessing `acquired` is the same class of error as "
               "guessing an owner."),
    _row("Lakera", "lakera.ai", "https://www.lakera.ai/careers",
         cohorts=("niche_startup",), categories=("security", "ai_ml"),
         notes="audit candidate. Probed 2026-09-02: domain confirmed, careers "
               "URL 200. Acquisition rumours are NOT verified here."),
    _row("HiddenLayer", "hiddenlayer.com", "https://hiddenlayer.com/careers/",
         cohorts=("niche_startup",), categories=("security", "ai_ml"),
         notes="audit candidate"),
    _row("Ampere Computing", "amperecomputing.com",
         "https://amperecomputing.com/careers", cohorts=("private_growth",),
         categories=("semiconductor",),
         notes="audit candidate. Probed 2026-09-02: domain confirmed, careers "
               "URL 403 (bot wall). BLOCKED, not empty."),
    _row("Celestial AI", "celestial.ai", "https://www.celestial.ai/careers",
         cohorts=("niche_startup",), categories=("semiconductor", "ai_ml"),
         notes="audit candidate. Probed 2026-09-02: careers URL 200, but the "
               "homepage does not NAME the company in visible text (a JS "
               "shell), so the domain is unconfirmed by our own check."),
    _row("d-Matrix", "d-matrix.ai", "https://www.d-matrix.ai/careers/",
         cohorts=("niche_startup",), categories=("semiconductor", "ai_ml"),
         notes="audit candidate"),
    _row("Enfabrica", "enfabrica.net", "https://www.enfabrica.net/careers",
         cohorts=("niche_startup",), categories=("semiconductor", "hardware"),
         notes="audit candidate. Probed 2026-09-02: domain confirmed, careers "
               "URL 404 - the path has moved; re-fingerprint from the domain."),

    # --- The 2026-09-02 audit list -------------------------------------------
    # Every row below was fetched by `scripts/verify_official_career_urls.py`
    # on 2026-09-02 and carries the outcome VERBATIM. Three states appear, and
    # the distinction between them is the whole point of recording them:
    #
    #   verified  the domain resolves, the page names the employer, and where
    #             an ATS was linked it is named here.
    #   blocked   the employer's own careers URL returned 403/429 TO US. That
    #             is a fact about our request and NOT about the employer; we
    #             do not attempt to bypass it, and the row stays a lead.
    #   error     a network failure on our side, retried and unresolved.
    #
    # None of them is marked covered here. A registry row is a starting point
    # for ownership verification; `career_sources` decides coverage, and it
    # still needs the state machine to pass.
    _row("xAI", "x.ai", "https://x.ai/careers",
         cohorts=("private_growth",), categories=("ai_ml",),
         batch=BATCH_AUDIT_2026_09_02,
         notes="2026-09-02: careers URL 403 to our prober (bot wall). BLOCKED, "
               "not empty and not gone. We do not bypass bot protection."),
    _row("World Labs", "worldlabs.ai", "https://www.worldlabs.ai/careers",
         cohorts=("niche_startup",), categories=("ai_ml",),
         batch=BATCH_AUDIT_2026_09_02,
         notes="2026-09-02 VERIFIED: domain confirmed, careers URL 200, page "
               "names the employer, links greenhouse:worldlabs."),
    _row("Cognition", "cognition.com", "https://cognition.com/careers",
         cohorts=("private_growth",), categories=("ai_ml", "devtools"),
         batch=BATCH_AUDIT_2026_09_02,
         notes="2026-09-02 VERIFIED: domain confirmed, careers URL 200, links "
               "ashby:cognition."),
    _row("MotherDuck", "motherduck.com", "https://motherduck.com/careers/",
         cohorts=("niche_startup",), categories=("cloud_data", "devtools"),
         batch=BATCH_AUDIT_2026_09_02,
         notes="2026-09-02 VERIFIED: domain confirmed, careers URL 200, links "
               "ashby:MotherDuck (tenant is case-sensitive at the vendor)."),
    _row("Axonius", "axonius.com",
         "https://www.axonius.com/company/careers/open-jobs",
         cohorts=("private_growth",), categories=("security",),
         batch=BATCH_AUDIT_2026_09_02,
         notes="2026-09-02: careers URL 429 to our prober, twice, including at "
               "0.4 req/s. BLOCKED, with a retry, not dead."),
    _row("Tines", "tines.com", "https://www.tines.com/careers/",
         cohorts=("private_growth",), categories=("security", "devtools"),
         batch=BATCH_AUDIT_2026_09_02,
         notes="2026-09-02 VERIFIED: domain confirmed, careers URL 200. The "
               "board is proxied under tines.com and links no ATS, but the "
               "job paths carry Greenhouse requisition ids and "
               "boards-api.greenhouse.io/v1/boards/tines answers 200 - so the "
               "generic provider reaches it and no custom adapter is needed."),
    _row("Cato Networks", "catonetworks.com", "https://www.catonetworks.com/careers/",
         cohorts=("private_growth",), categories=("security", "telecom"),
         batch=BATCH_AUDIT_2026_09_02,
         notes="2026-09-02 VERIFIED: domain confirmed, careers URL 200, links "
               "greenhouse:catonetworks."),
    _row("IonQ", "ionq.com", "https://www.ionq.com/careers",
         cohorts=("public_tech",), categories=("hardware", "research"),
         batch=BATCH_AUDIT_2026_09_02,
         notes="2026-09-02 VERIFIED: domain confirmed, careers URL 200, links "
               "greenhouse:ionq. Quantum hardware."),
    _row("Rigetti Computing", "rigetti.com", "https://www.rigetti.com/careers",
         cohorts=("public_tech",), categories=("hardware", "research"),
         batch=BATCH_AUDIT_2026_09_02,
         notes="2026-09-02: read timeout, twice, including at 0.4 req/s. "
               "Recorded as an error on OUR side; not blocked, not gone."),
    _row("Quantinuum", "quantinuum.com", "https://www.quantinuum.com/company/careers",
         cohorts=("private_growth",), categories=("hardware", "research"),
         batch=BATCH_AUDIT_2026_09_02,
         notes="2026-09-02 VERIFIED: domain confirmed, careers URL 200, links "
               "https://jobs.eu.lever.co/quantinuum - Lever's EU region, which "
               "no fingerprint matched and no host allowlist admitted until "
               "this audit. `api.lever.co/v0/postings/quantinuum` 404s; the EU "
               "host returns the board. Owner named by the provider as "
               "'Quantinuum'."),
    _row("Citadel", "citadel.com", "https://www.citadel.com/careers/",
         cohorts=("private_growth",), categories=("fintech",),
         batch=BATCH_AUDIT_2026_09_02,
         notes="2026-09-02: careers URL 403 to our prober (bot wall). BLOCKED. "
               "Citadel Securities is a SEPARATE legal entity and is not "
               "merged with this row on the strength of a shared name."),

    # --- 2026-09-03 evidence-backed intake ----------------------------------
    # These rows are LEADS. A registry row is not coverage. Ownership still
    # has to walk from the employer domain to the board, and content still
    # has to validate. Do not merge any of them on a similar display name.
    _row("Safe Superintelligence", "ssi.inc", "https://ssi.inc/",
         cohorts=("niche_startup",), categories=("ai_ml", "research"),
         batch=BATCH_INTAKE_2026_09_03,
         notes="Candidate. Official page https://ssi.inc/. Candidate board "
               "https://jobs.ashbyhq.com/ssi. Do NOT promote the Ashby "
               "source until a first-party link/redirect from ssi.inc to "
               "that exact board is confirmed. Category: AI/ML research."),
    _row("Thinking Machines Lab", "thinkingmachines.ai",
         "https://thinkingmachines.ai/",
         cohorts=("niche_startup",), categories=("ai_ml", "research"),
         batch=BATCH_INTAKE_2026_09_03,
         notes="Official site was observed linking to an obsolete Greenhouse "
               "board (moved/stale). Current candidate board "
               "https://jobs.ashbyhq.com/thinkingmachines. Do NOT promote "
               "Ashby without sufficient first-party or independent ownership "
               "evidence. Record Greenhouse as moved, not as a live source."),
    _row("Periodic Labs", "periodic.com", "https://periodic.com/",
         cohorts=("niche_startup",), categories=("ai_ml", "research"),
         batch=BATCH_INTAKE_2026_09_03,
         notes="Candidate. Official site was observed linking to "
               "https://jobs.ashbyhq.com/periodic-labs. Still needs the "
               "ownership walk and content validation before coverage."),
    _row("OpenEvidence", "openevidence.com", "https://www.openevidence.com/",
         cohorts=("private_growth",), categories=("healthtech", "ai_ml"),
         batch=BATCH_INTAKE_2026_09_03,
         notes="KEEP candidate/blocked until ownership is corroborated. The "
               "first-party site blocked automated inspection. Candidate "
               "board https://jobs.ashbyhq.com/openevidence. Do NOT confuse "
               "with open-evidence.com, a different organisation. Do not "
               "merge on the similar name."),
)


#: Where a verification EVENT is recorded. One row per domain, upserted by
#: `scripts/verify_official_career_urls.py`. Additive: dropping it loses the
#: stamps and nothing else, and every row goes back to `verified_at: None`,
#: which is exactly what "never confirmed" should look like.
URL_CHECKS = "official_career_url_checks"

#: A check outcome. `verified` is the ONLY one that sets `verified_at`.
CHECK_VERIFIED = "verified"        # fetched, 200, and the page names the employer
CHECK_REACHABLE = "reachable"      # fetched, 200, but the page does not name them
CHECK_MOVED = "moved"              # redirected off the registered host
CHECK_BLOCKED = "blocked"          # 403 / 429 / robots — a fact about US
CHECK_MISSING = "missing"          # 404 / DNS — the URL is wrong or gone
CHECK_ERROR = "error"              # timeout, TLS, unreadable

CHECK_OUTCOMES: Tuple[str, ...] = (CHECK_VERIFIED, CHECK_REACHABLE, CHECK_MOVED,
                                   CHECK_BLOCKED, CHECK_MISSING, CHECK_ERROR)


def _db():
    from utils.db_connect import DBConnect
    return DBConnect().get_db()


def checks_for(domains: Sequence[str] = (), *, db=None) -> Dict[str, Dict[str, Any]]:
    """`{domain: check row}` — the recorded verification events."""
    db = db if db is not None else _db()
    query: Dict[str, Any] = {}
    if domains:
        query = {"domain": {"$in": [str(d).strip().lower() for d in domains]}}
    try:
        return {str(r.get("domain")): r for r in
                db[URL_CHECKS].find(query, {"_id": 0})}
    except Exception:      # pragma: no cover - a missing collection is "none checked"
        return {}


def verified_rows(*, db=None) -> List[Dict[str, Any]]:
    """`OFFICIAL_CAREER_URLS` with `verified_at` filled in from real events.

    The registry constant is the CLAIM; this is the claim joined to whether
    anybody has checked it. Callers that report coverage must use this one —
    `OFFICIAL_CAREER_URLS` alone cannot distinguish "confirmed last week" from
    "typed in from a brief and never fetched".
    """
    checks = checks_for(db=db)
    out: List[Dict[str, Any]] = []
    for row in OFFICIAL_CAREER_URLS:
        row = dict(row)
        chk = checks.get(str(row.get("domain") or "").lower())
        if chk:
            row["verified_at"] = chk.get("verified_at")
            row["last_checked_at"] = chk.get("checked_at")
            row["check_outcome"] = chk.get("outcome")
            row["check_detail"] = chk.get("detail")
            row["final_url"] = chk.get("final_url")
        out.append(row)
    return out


def record_check(db, *, domain: Any, display_name: Any, career_landing_url: Any,
                 outcome: str, detail: Any = "", http_status: Any = None,
                 final_url: Any = "", redirect_chain: Sequence[str] = (),
                 endpoints: Sequence[str] = (), now=None) -> str:
    """Write one verification EVENT. `verified_at` moves only on `verified`.

    A 403 does not clear a previous verification and does not set a new one:
    it is a fact about our request, recorded as its own outcome with its own
    timestamp. That is the same rule `career_source_service.record_failure`
    learned the hard way, when one blocked sweep turned 830 sources `blocked`
    and cut validated coverage by a third.
    """
    from datetime import datetime, timezone
    now = now or datetime.now(timezone.utc)
    dom = str(domain or "").strip().lower()
    if outcome not in CHECK_OUTCOMES:
        outcome = CHECK_ERROR
    update: Dict[str, Any] = {
        "domain": dom, "display_name": str(display_name or "")[:200],
        "career_landing_url": str(career_landing_url or "")[:500],
        "outcome": outcome, "detail": str(detail or "")[:400],
        "http_status": http_status,
        "final_url": str(final_url or "")[:500],
        "redirect_chain": [str(u)[:500] for u in (redirect_chain or ())][:10],
        "ats_endpoints": [str(e)[:200] for e in (endpoints or ())][:8],
        "checked_at": now,
        "registry_version": REGISTRY_VERSION,
    }
    if outcome == CHECK_VERIFIED:
        update["verified_at"] = now
    db[URL_CHECKS].update_one({"domain": dom},
                              {"$set": update,
                               "$setOnInsert": {"first_checked_at": now}},
                              upsert=True)
    return dom


def ensure_indexes(*, db=None) -> List[str]:
    db = db if db is not None else _db()
    try:
        return [db[URL_CHECKS].create_index("domain", unique=True,
                                            name="url_check_domain_unique"),
                db[URL_CHECKS].create_index([("outcome", 1), ("checked_at", -1)],
                                            name="url_check_outcome_idx")]
    except Exception:      # pragma: no cover
        return []


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
#: `status` records what a LIVE probe observed, with the date:
#:   `verified`   - the endpoint returned postings
#:   `blocked`    - the endpoint exists and refuses us (bot wall, session
#:                  token). `validate()` returns BLOCKED, never EMPTY: "we
#:                  cannot read this board" and "this board has no jobs" are
#:                  different claims about the employer, and only one of them
#:                  is ours to make.
#:   `unverified` - added but never probed. Treated as blocked until it is.
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
        # Re-probed 2026-09-15 and now READ. The 2026-09-02 note was right about
        # the symptom and one step short of the cause: `/api/v1/search` really
        # does answer 200 with `{"res":{"searchResults":[],"totalRecords":0}}`,
        # but the missing half is a COOKIE, not a credential we do not have.
        # `jobs.apple.com/static/jobsite.main.*.js` builds every API call from
        # `{csrf:{token:{url:"/api/v1/CSRFToken"}}}` and one GET there is the
        # whole handshake: the same session then returns the full board.
        # Measured live by walking the whole board: 6,141 ROWS and 4,905
        # DISTINCT requisition ids (a multi-location req repeats), of which
        # 4,503 rows carry a United States location — so on the order of 3,600
        # distinct US postings. Both numbers are stated because the row count
        # is what the crawl fetches and the distinct count is what lands.
        #
        # The `X-Apple-CSRF-Token` header the bundle also sends turned out NOT
        # to be required once the session cookie is held (tested three ways:
        # token+cookies, cookies only, token only). So the row asks for the
        # cheapest honest thing — the preflight — and sends no invented header.
        # `/api/role/search` is still a 404; it was renamed, not gated.
        #
        # No working country filter was found. `filters.locations` is ignored
        # for every value tried (`postLocation-USA`, `united-states-USA`,
        # `USA`) and an unknown filter key falls through to an UNFILTERED
        # result rather than erroring - so a location filter here would be a
        # silent lie. The whole board is walked and the US filter stays where
        # it already is, on the row.
        "status": "verified",
        "status_detail": ("jobs.apple.com/api/v1/search returns 6,141 rows / "
                          "4,905 distinct reqs (4,503 US-located rows) after a "
                          "GET to /api/v1/CSRFToken on the same session "
                          "(probed 2026-09-15)"),
        "request": {
            "method": "POST",
            "url": "https://jobs.apple.com/api/v1/search",
            "json_body": {"query": "", "filters": {}, "page": 1,
                          "locale": "en-us", "sort": "newest",
                          "format": {"longDate": "MMMM D, YYYY",
                                     "mediumDate": "MMM D, YYYY"}},
            "page_field": "page",
            "preflight": {"method": "GET",
                          "url": "https://jobs.apple.com/api/v1/CSRFToken"},
        },
        # 20 rows a page, measured, and paging answers well past page 300.
        # `max_pages` covers the whole 6,140 with headroom; the total-count stop
        # in `list_jobs` ends the walk before it, so the budget is a ceiling
        # rather than a cost.
        "pagination": {"style": "page", "page_start": 1, "page_size": 20,
                       "max_pages": 340},
        "records_path": "res.searchResults",
        "total_path": "res.totalRecords",
        # Confirmed against a live record 2026-09-15; `postDateInGMT` is a real
        # ISO timestamp and `postingDate` is the same date rendered for humans,
        # so the first path wins and `date_confidence` is EXACT.
        "fields": {
            "source_job_id": ["positionId", "id"],
            "title": ["postingTitle", "title"],
            "location": ["locations.0.name", "postingLocation"],
            "country": ["locations.0.countryName"],
            "posted_at": ["postDateInGMT", "postingDate"],
            "description": ["jobSummary"],
            "department": ["team.teamName"],
            "apply_url_template": "https://jobs.apple.com/en-us/details/{source_job_id}",
        },
    },
    {
        "key": "tesla",
        "display_name": "Tesla",
        "domain": "tesla.com",
        "vendor": "custom_tesla",
        "career_landing_url": "https://www.tesla.com/careers/search/",
        # Probed 2026-09-02: Akamai returns "Access Denied" to this endpoint.
        # The board exists and we cannot read it, which is `blocked` -- a state
        # with a retry -- and not `empty`.
        "status": "blocked",
        "status_detail": "Akamai denies www.tesla.com/cua-api (probed 2026-09-02)",
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
        "status": "verified",
        "status_detail": "returns postings (probed 2026-09-02)",
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
