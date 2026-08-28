"""Curated MNC / Big-Tech catalog for the MNCs tab (third career-pages variant).

Every DIRECT-fetch row (career_pages / workday / enterprise / orc / uber) was live-verified
on 2026-07-17: its public JSON endpoint returned HTTP 200 with real job rows
using the exact URL patterns the existing fetchers use (see
career_pages_service.FETCHERS / ENTERPRISE_FETCHERS and
workday_cxs_fetcher._fetch_tenant). Companies whose direct source is genuinely
bot-walled are engine "aggregator": fetched through JSearch with a STRICT
employer-name match so only that company's postings pass. Re-verify rather than
hand-guessing new rows.

Re-verified 2026-08-27. Five names previously listed here as bot-walled were
not, and are now direct: Salesforce (wd12/External_Career_Site, 1,546),
Accenture (wd103/AccentureCareers, 2,000), Johnson & Johnson (jj.wd5/JJ,
1,811), Lockheed Martin (Eightfold pcsx, 2,195) and AMD (Radancy, 1,205). Each
was found by fingerprinting the employer's own careers page rather than by
guessing a tenant, then validated through its provider.

Still genuinely unreachable from a plain HTTP client, re-probed the same day and
deliberately left on the metered path: Google, Meta, Apple, IBM, Tesla, Walmart,
Goldman Sachs, American Express, McKinsey (JS shells whose APIs need a browser
session or a CSRF handshake) and SAP (SuccessFactors RMK, which answers with
HTML tiles and needs a provider this repo does not have yet). UnitedHealth is on
Taleo but its portal id is not derivable, which `job_ingest_worker` already
documents as a configuration gap rather than an empty employer.

Row shape: {display_name, slug, sector, engine, ...engine-specific}:
  engine "career_pages": + ats                     (slug = board slug)
  engine "enterprise":   + fetcher, host, extra    (ENTERPRISE_FETCHERS config)
  engine "workday":      + tenant, wd_host, site   (CXS fetcher config)
  engine "orc":          + host, extra{site_number}  (Oracle Recruiting Cloud
                           public CE API; verified live 2026-07-17)
  engine "uber":         (no config — Uber's own careers search API;
                           verified live 2026-07-17)
  engine "aggregator":   + aliases (optional)      (extra employer-name forms)
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

# The 10 allowed sector keys (the MNC tab's "industries").
MNC_SECTORS: Tuple[str, ...] = (
    "big_tech",
    "semiconductors",
    "enterprise_software",
    "fintech",
    "finance_banking",
    "healthcare_pharma",
    "consulting",
    "retail_consumer",
    "automotive_aerospace",
    "media_entertainment",
)

MNC_ENGINES: Tuple[str, ...] = (
    "career_pages", "enterprise", "workday", "orc", "uber", "aggregator")

MNC_CATALOG: List[Dict[str, Any]] = [
    # ── big_tech ─────────────────────────────────────────────────────────
    {"display_name": "Amazon", "slug": "amazon", "sector": "big_tech",
     "engine": "enterprise", "fetcher": "amazon", "host": "www.amazon.jobs", "extra": {}},
    {"display_name": "Microsoft", "slug": "microsoft", "sector": "big_tech",
     "engine": "enterprise", "fetcher": "eightfold", "host": "apply.careers.microsoft.com",
     "extra": {"domain": "microsoft.com", "variant": "pcsx"}},
    {"display_name": "Google", "slug": "google", "sector": "big_tech",
     "engine": "aggregator", "aliases": ["alphabet"]},
    {"display_name": "Meta", "slug": "meta", "sector": "big_tech",
     "engine": "aggregator", "aliases": ["meta platforms"]},
    {"display_name": "Apple", "slug": "apple", "sector": "big_tech",
     "engine": "aggregator", "aliases": []},
    {"display_name": "IBM", "slug": "ibm", "sector": "big_tech",
     "engine": "aggregator", "aliases": ["international business machines"]},
    {"display_name": "Cisco", "slug": "cisco", "sector": "big_tech",
     "engine": "workday", "tenant": "cisco", "wd_host": "wd5", "site": "Cisco_Careers"},
    {"display_name": "Uber", "slug": "uber", "sector": "big_tech",
     "engine": "uber"},
    {"display_name": "Airbnb", "slug": "airbnb", "sector": "big_tech",
     "engine": "career_pages", "ats": "greenhouse"},

    # ── semiconductors ───────────────────────────────────────────────────
    {"display_name": "NVIDIA", "slug": "nvidia", "sector": "semiconductors",
     "engine": "workday", "tenant": "nvidia", "wd_host": "wd5", "site": "NVIDIAExternalCareerSite"},
    {"display_name": "Qualcomm", "slug": "qualcomm", "sector": "semiconductors",
     "engine": "enterprise", "fetcher": "eightfold", "host": "careers.qualcomm.com",
     "extra": {"domain": "qualcomm.com", "variant": "pcsx"}},
    {"display_name": "Intel", "slug": "intel", "sector": "semiconductors",
     "engine": "workday", "tenant": "intel", "wd_host": "wd1", "site": "External"},
    # Was `aggregator` (metered JSearch). AMD's careers page still links
    # `careers-amd.icims.com`, but validating that endpoint returns a stub
    # reading "migrated to https://careers.amd.com/jobs" - it answers 200 and
    # serves zero jobs. The Radancy host serves 1,205 (138 inside a 7-day
    # window), verified 2026-08-27.
    {"display_name": "AMD", "slug": "amd", "sector": "semiconductors",
     "engine": "enterprise", "fetcher": "radancy", "host": "careers.amd.com",
     "extra": {}},
    {"display_name": "Broadcom", "slug": "broadcom", "sector": "semiconductors",
     "engine": "workday", "tenant": "broadcom", "wd_host": "wd1", "site": "External_Career"},
    {"display_name": "Micron", "slug": "micron", "sector": "semiconductors",
     "engine": "workday", "tenant": "micron", "wd_host": "wd1", "site": "External"},
    {"display_name": "Applied Materials", "slug": "appliedmaterials", "sector": "semiconductors",
     "engine": "workday", "tenant": "amat", "wd_host": "wd1", "site": "External"},
    {"display_name": "ASML", "slug": "asml", "sector": "semiconductors",
     "engine": "workday", "tenant": "asml", "wd_host": "wd3", "site": "ASMLEXT1"},

    # ── enterprise_software ──────────────────────────────────────────────
    # Was `aggregator`. `salesforce.wd12/External_Career_Site` validated live
    # on 2026-08-27 with 1,546 postings.
    {"display_name": "Salesforce", "slug": "salesforce", "sector": "enterprise_software",
     "engine": "workday", "tenant": "salesforce", "wd_host": "wd12",
     "site": "External_Career_Site"},
    {"display_name": "Adobe", "slug": "adobe", "sector": "enterprise_software",
     "engine": "workday", "tenant": "adobe", "wd_host": "wd5", "site": "external_experienced"},
    {"display_name": "Oracle", "slug": "oracle", "sector": "enterprise_software",
     "engine": "orc", "host": "eeho.fa.us2.oraclecloud.com",
     "extra": {"site_number": "CX_1"}},
    {"display_name": "SAP", "slug": "sap", "sector": "enterprise_software",
     "engine": "aggregator", "aliases": ["sap america"]},
    {"display_name": "ServiceNow", "slug": "servicenow", "sector": "enterprise_software",
     "engine": "career_pages", "ats": "smartrecruiters"},
    {"display_name": "Workday", "slug": "workday", "sector": "enterprise_software",
     "engine": "workday", "tenant": "workday", "wd_host": "wd5", "site": "Workday"},
    {"display_name": "Databricks", "slug": "databricks", "sector": "enterprise_software",
     "engine": "career_pages", "ats": "greenhouse"},
    {"display_name": "Cloudflare", "slug": "cloudflare", "sector": "enterprise_software",
     "engine": "career_pages", "ats": "greenhouse"},

    # ── fintech ──────────────────────────────────────────────────────────
    {"display_name": "Stripe", "slug": "stripe", "sector": "fintech",
     "engine": "career_pages", "ats": "greenhouse"},
    {"display_name": "PayPal", "slug": "paypal", "sector": "fintech",
     "engine": "workday", "tenant": "paypal", "wd_host": "wd1", "site": "jobs"},
    {"display_name": "Visa", "slug": "visa", "sector": "fintech",
     "engine": "workday", "tenant": "visa", "wd_host": "wd5", "site": "Visa"},
    {"display_name": "Mastercard", "slug": "mastercard", "sector": "fintech",
     "engine": "workday", "tenant": "mastercard", "wd_host": "wd1", "site": "CorporateCareers"},
    {"display_name": "Coinbase", "slug": "coinbase", "sector": "fintech",
     "engine": "career_pages", "ats": "greenhouse"},
    {"display_name": "Robinhood", "slug": "robinhood", "sector": "fintech",
     "engine": "career_pages", "ats": "greenhouse"},

    # ── finance_banking ──────────────────────────────────────────────────
    {"display_name": "JPMorgan Chase", "slug": "jpmorganchase", "sector": "finance_banking",
     "engine": "orc", "host": "jpmc.fa.oraclecloud.com",
     "extra": {"site_number": "CX_1"}},
    {"display_name": "Goldman Sachs", "slug": "goldmansachs", "sector": "finance_banking",
     "engine": "aggregator", "aliases": ["the goldman sachs group"]},
    {"display_name": "Morgan Stanley", "slug": "morganstanley", "sector": "finance_banking",
     "engine": "workday", "tenant": "ms", "wd_host": "wd5", "site": "External"},
    {"display_name": "Citi", "slug": "citi", "sector": "finance_banking",
     "engine": "workday", "tenant": "citi", "wd_host": "wd5", "site": "2"},
    {"display_name": "Bank of America", "slug": "bankofamerica", "sector": "finance_banking",
     "engine": "workday", "tenant": "ghr", "wd_host": "wd1", "site": "Lateral-US"},
    {"display_name": "Wells Fargo", "slug": "wellsfargo", "sector": "finance_banking",
     "engine": "workday", "tenant": "wf", "wd_host": "wd1", "site": "WellsFargoJobs"},
    {"display_name": "American Express", "slug": "americanexpress", "sector": "finance_banking",
     "engine": "aggregator", "aliases": ["amex"]},
    {"display_name": "BlackRock", "slug": "blackrock", "sector": "finance_banking",
     "engine": "workday", "tenant": "blackrock", "wd_host": "wd1", "site": "BlackRock_Professional"},

    # ── healthcare_pharma ────────────────────────────────────────────────
    {"display_name": "Pfizer", "slug": "pfizer", "sector": "healthcare_pharma",
     "engine": "workday", "tenant": "pfizer", "wd_host": "wd1", "site": "PfizerCareers"},
    {"display_name": "Merck", "slug": "merck", "sector": "healthcare_pharma",
     "engine": "enterprise", "fetcher": "phenom", "host": "jobs.merck.com", "extra": {}},
    {"display_name": "Eli Lilly", "slug": "lilly", "sector": "healthcare_pharma",
     "engine": "enterprise", "fetcher": "phenom", "host": "careers.lilly.com", "extra": {}},
    # Was `aggregator`. Fingerprinted off careers.jnj.com and validated live on
    # 2026-08-27: 1,811 postings.
    {"display_name": "Johnson & Johnson", "slug": "jnj", "sector": "healthcare_pharma",
     "engine": "workday", "tenant": "jj", "wd_host": "wd5", "site": "JJ"},
    {"display_name": "UnitedHealth Group", "slug": "unitedhealthgroup", "sector": "healthcare_pharma",
     "engine": "aggregator", "aliases": ["unitedhealthcare", "optum"]},
    {"display_name": "CVS Health", "slug": "cvshealth", "sector": "healthcare_pharma",
     "engine": "workday", "tenant": "cvshealth", "wd_host": "wd1", "site": "CVS_Health_Careers"},
    {"display_name": "Medtronic", "slug": "medtronic", "sector": "healthcare_pharma",
     "engine": "workday", "tenant": "medtronic", "wd_host": "wd1", "site": "MedtronicCareers"},

    # ── consulting ───────────────────────────────────────────────────────
    {"display_name": "McKinsey & Company", "slug": "mckinsey", "sector": "consulting",
     "engine": "aggregator", "aliases": ["mckinsey and company"]},
    {"display_name": "Deloitte", "slug": "deloitte", "sector": "consulting",
     "engine": "aggregator", "aliases": ["deloitte us", "deloitte consulting"]},
    # Was `aggregator`. Fingerprinted off accenture.com/careers and validated
    # live on 2026-08-27: 2,000 postings. Note the pod - wd103, not wd1/wd5.
    {"display_name": "Accenture", "slug": "accenture", "sector": "consulting",
     "engine": "workday", "tenant": "accenture", "wd_host": "wd103",
     "site": "AccentureCareers"},
    {"display_name": "PwC", "slug": "pwc", "sector": "consulting",
     "engine": "workday", "tenant": "pwc", "wd_host": "wd3", "site": "Global_Experienced_Careers"},
    {"display_name": "Gartner", "slug": "gartner", "sector": "consulting",
     "engine": "workday", "tenant": "gartner", "wd_host": "wd5", "site": "EXT"},

    # ── retail_consumer ──────────────────────────────────────────────────
    {"display_name": "Walmart", "slug": "walmart", "sector": "retail_consumer",
     "engine": "aggregator", "aliases": ["wal mart"]},
    {"display_name": "Target", "slug": "target", "sector": "retail_consumer",
     "engine": "workday", "tenant": "target", "wd_host": "wd5", "site": "targetcareers"},
    {"display_name": "Nike", "slug": "nike", "sector": "retail_consumer",
     "engine": "workday", "tenant": "nike", "wd_host": "wd1", "site": "nke"},
    {"display_name": "Procter & Gamble", "slug": "pg", "sector": "retail_consumer",
     "engine": "workday", "tenant": "pg", "wd_host": "wd5", "site": "1000"},
    {"display_name": "Lowe's", "slug": "lowes", "sector": "retail_consumer",
     "engine": "enterprise", "fetcher": "phenom", "host": "talent.lowes.com", "extra": {}},

    # ── automotive_aerospace ─────────────────────────────────────────────
    {"display_name": "Tesla", "slug": "tesla", "sector": "automotive_aerospace",
     "engine": "aggregator", "aliases": ["tesla motors"]},
    {"display_name": "Boeing", "slug": "boeing", "sector": "automotive_aerospace",
     "engine": "workday", "tenant": "boeing", "wd_host": "wd1", "site": "EXTERNAL_CAREERS"},
    # Was `aggregator`. Eightfold, and specifically the `pcsx` variant with
    # `domain=lockheedmartin.com`: the `classic` /api/apply/v2/jobs path 403s on
    # this tenant while pcsx returns 2,195 postings. Verified 2026-08-27.
    {"display_name": "Lockheed Martin", "slug": "lockheedmartin", "sector": "automotive_aerospace",
     "engine": "enterprise", "fetcher": "eightfold",
     "host": "lockheedmartin.eightfold.ai",
     "extra": {"domain": "lockheedmartin.com", "variant": "pcsx"}},
    {"display_name": "Northrop Grumman", "slug": "northropgrumman", "sector": "automotive_aerospace",
     "engine": "workday", "tenant": "ngc", "wd_host": "wd1", "site": "Northrop_Grumman_External_Site"},
    {"display_name": "General Motors", "slug": "generalmotors", "sector": "automotive_aerospace",
     "engine": "workday", "tenant": "generalmotors", "wd_host": "wd5", "site": "Careers_GM"},
    {"display_name": "Toyota", "slug": "toyota", "sector": "automotive_aerospace",
     "engine": "enterprise", "fetcher": "phenom", "host": "careers.toyota.com", "extra": {}},
    {"display_name": "SpaceX", "slug": "spacex", "sector": "automotive_aerospace",
     "engine": "career_pages", "ats": "greenhouse"},

    # ── media_entertainment ──────────────────────────────────────────────
    {"display_name": "Netflix", "slug": "netflix", "sector": "media_entertainment",
     "engine": "enterprise", "fetcher": "eightfold", "host": "explore.jobs.netflix.net",
     "extra": {"domain": "netflix.com", "variant": "classic"}},
    {"display_name": "Disney", "slug": "disney", "sector": "media_entertainment",
     "engine": "workday", "tenant": "disney", "wd_host": "wd5", "site": "disneycareer"},
    {"display_name": "Warner Bros. Discovery", "slug": "warnerbros", "sector": "media_entertainment",
     "engine": "workday", "tenant": "warnerbros", "wd_host": "wd5", "site": "global"},
    {"display_name": "Comcast", "slug": "comcast", "sector": "media_entertainment",
     "engine": "workday", "tenant": "comcast", "wd_host": "wd5", "site": "Comcast_Careers"},
    {"display_name": "Spotify", "slug": "spotify", "sector": "media_entertainment",
     "engine": "career_pages", "ats": "lever"},
    {"display_name": "Sony", "slug": "sony", "sector": "media_entertainment",
     "engine": "workday", "tenant": "sonyglobal", "wd_host": "wd1", "site": "SonyGlobalCareers"},
]
