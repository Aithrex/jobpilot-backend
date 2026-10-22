"""What each researched lead becomes, and why. Hand-maintained; reviewed.

`direct_employer_leads.LEADS` holds what the 2026-10-21 research FOUND. This
file holds what we DO about it, one entry per eligible direct employer plus the
owner's top-employer list. `scripts/onboard_researched_companies.py` turns the
two into exactly one outcome per company:

    existing_covered            a crawlable row was already in the repo
    existing_unhealthy_or_moved a row exists but its board no longer answers
    new_validated               row added on an existing provider, validated live
    excluded                    staffing / consultancy / intermediary (no crawl)
    review                      mixed business or unresolved identity (no crawl)
    blocked                     endpoint known, live validation did not succeed
    unsupported                 no provider can read the surface (named)
    pending                     identity / provider evidence missing; never guessed

`plan` values in this file:

    "existing"     the row was in the repo before 2026-10-21 (do not add again)
    "added"        this change added the row (requires a recorded validation)
    "candidate"    exact endpoint known; not added (validation failed/blocked)
    "unsupported"  `provider_needed` names the missing provider (or `reason`)
    "pending"      `reason` says what evidence is missing

An endpoint is the EXACT identity the provider is called with: vendor, tenant
(case preserved), site (Workday site / ORC siteNumber / UKG board / Greenhouse
region), host and extra. Nothing here is a guessed slug: each one came from an
official link/embed chain or employer-hosted configuration recorded in the
research evidence, or from a live fetch named in `ownership`.

Rows that are "added" live in the catalog/seed table named by `where`, which is
exactly what `services/ingest_runner` on the Oracle box reads (see the script's
docstring for the path). This file is not a registry; it does not feed the
crawler.
"""
from __future__ import annotations

from typing import Any, Dict

RESEARCH_DATE = "2026-10-21"


def _ep(vendor: str, tenant: str, *, site: str = "", host: str = "",
        **extra: Any) -> Dict[str, Any]:
    return {"vendor": vendor, "tenant": tenant, "site": site, "host": host,
            "extra": dict(extra)}


# ---------------------------------------------------------------------------
# The 82 eligible direct employers from the 174 leads
# ---------------------------------------------------------------------------
LEAD_PLAN: Dict[str, Dict[str, Any]] = {
    # --- Greenhouse --------------------------------------------------------
    "Ascent Developer Solutions": dict(
        domain="ascentds.com", state="CHAIN", plan="added",
        endpoint=_ep("greenhouse", "ascentds"), where="career_pages_catalog",
        industry="fintech"),
    "Clear Street": dict(
        domain="clearstreet.io", state="CONFIG", plan="added",
        endpoint=_ep("greenhouse", "clearstreet"), where="career_pages_catalog",
        industry="fintech",
        note="Official /_nuxt/ module fetches boards-api.greenhouse.io/v1/boards/clearstreet."),
    "Hover": dict(
        domain="hover.to", state="CONFIG", plan="added",
        endpoint=_ep("greenhouse", "hover"), where="career_pages_catalog",
        industry="tech",
        note="Official job-post module inserts embed/job_board/js?for=hover. Its careers "
             "page also renders a static 'No jobs found' template next to real jobs; "
             "that template is not a zero."),
    "Re:Build": dict(
        domain="rebuildmanufacturing.com", state="CHAIN", plan="added",
        endpoint=_ep("greenhouse", "rebuildmanufacturing"),
        where="career_pages_catalog", industry="manufacturing_automotive"),
    "Cognite": dict(
        domain="cognite.com", state="CHAIN", plan="added",
        endpoint=_ep("greenhouse", "cognite", site="eu"), where="seed",
        industry="tech",
        note="EU Greenhouse region (job-boards.eu.greenhouse.io/cognite). Read "
             "through the region-aware Greenhouse provider; never the US API."),
    # --- Ashby (tenant case preserved as the employer links it) -----------
    "Chalk": dict(domain="chalk.ai", state="CHAIN", plan="added",
                  endpoint=_ep("ashby", "chalk"), where="career_pages_catalog",
                  industry="tech"),
    "ClarityPay": dict(domain="claritypay.com", state="CHAIN", plan="added",
                       endpoint=_ep("ashby", "claritypay"),
                       where="career_pages_catalog", industry="fintech"),
    "Fluidstack": dict(domain="fluidstack.io", state="CHAIN", plan="added",
                       endpoint=_ep("ashby", "fluidstack"),
                       where="career_pages_catalog", industry="tech"),
    "HavocAI": dict(domain="havocai.com", state="CHAIN", plan="added",
                    endpoint=_ep("ashby", "havocai"), where="career_pages_catalog",
                    industry="aerospace_defense"),
    "Northwood": dict(domain="northwoodspace.io", state="CHAIN", plan="added",
                      endpoint=_ep("ashby", "NorthwoodSpace"),
                      where="career_pages_catalog", industry="aerospace_defense",
                      note="Board carries a 'General Application' posting; it is "
                           "counted as non-requisition, not as a role."),
    "Quantcast": dict(domain="quantcast.com", state="CHAIN", plan="added",
                      endpoint=_ep("ashby", "quantcast"),
                      where="career_pages_catalog", industry="tech",
                      note="Official /careers/proxy JSON names Ashby/quantcast; the "
                           "careers page's static 'No jobs found' template is not a zero."),
    "Quindar": dict(domain="quindar.space", state="CONFIG", plan="added",
                    endpoint=_ep("ashby", "quindar"), where="career_pages_catalog",
                    industry="aerospace_defense",
                    note="`${job.id}` in the official config is a template "
                         "placeholder, not a job id."),
    "Rhoda AI": dict(domain="rhoda.ai", state="CHAIN", plan="added",
                     endpoint=_ep("ashby", "rhoda-ai"), where="career_pages_catalog",
                     industry="tech"),
    "Unison": dict(domain="unisonglobal.com", state="CHAIN", plan="added",
                   endpoint=_ep("ashby", "unison-software"),
                   where="career_pages_catalog", industry="tech"),
    "WisdomAI": dict(domain="wisdom.ai", state="CHAIN", plan="added",
                     endpoint=_ep("ashby", "Wisdom-AI"), where="career_pages_catalog",
                     industry="tech"),
    # --- Lever / Workable / SmartRecruiters --------------------------------
    "FieldAI": dict(domain="fieldai.com", state="CONFIG", plan="existing",
                    endpoint=_ep("lever", "field-ai"), where="career_pages_catalog",
                    note="Already in CAREER_PAGES_CATALOG as 'Field AI' (the "
                         "exact-name miss was a spelling difference)."),
    "Flexcompute": dict(domain="flexcompute.com", state="CHAIN", plan="added",
                        endpoint=_ep("workable", "flexcompute"),
                        where="career_pages_catalog", industry="tech"),
    "Trexquant Investment LP": dict(
        domain="trexquant.com", state="CONFIG", plan="added",
        endpoint=_ep("workable", "trexquant"), where="career_pages_catalog",
        industry="fintech",
        note="Workable widget repeats one shortcode per location (50 rows, 34 "
             "distinct ids on 2026-10-21); requisitions are counted by id."),
    "Intuitive": dict(domain="intuitive.com", state="CHAIN", plan="added",
                      endpoint=_ep("smartrecruiters", "Intuitive"),
                      where="career_pages_catalog", industry="healthtech",
                      note="The separate SmartRecruiters talent-community link is "
                           "not a source."),
    "Spring Venture Group": dict(
        domain="springventuregroup.com", state="CHAIN", plan="added",
        endpoint=_ep("smartrecruiters", "SpringVentureGroup1"),
        where="career_pages_catalog", industry="insurance",
        note="Talent-community link excluded; only requisitions are counted."),
    # --- Workday (tenant, wdN pod, site case as linked) --------------------
    "NCR Voyix": dict(domain="ncrvoyix.com", state="CHAIN", plan="added",
                      endpoint=_ep("workday", "ncr", site="ext_us", wd_host="wd1"),
                      where="workday_tenant_catalog", industry="tech",
                      note="Official page links ext_us, ext_non_us and ext_apac; "
                           "ext_us is the US board."),
    "Vantor": dict(domain="vantor.com", state="CHAIN", plan="added",
                   endpoint=_ep("workday", "maxar", site="Vantor", wd_host="wd1"),
                   where="workday_tenant_catalog", industry="aerospace_defense",
                   note="Maxar's tenant, Vantor's site: attributed to Vantor only."),
    "Vertex Pharmaceuticals": dict(
        domain="vrtx.com", state="CHAIN", plan="added",
        endpoint=_ep("workday", "vrtx", site="vertex_careers", wd_host="wd501"),
        where="workday_tenant_catalog", industry="pharma"),
    "OPENLANE": dict(domain="openlane.com", state="CHAIN", plan="added",
                     endpoint=_ep("workday", "openlane", site="OPENLANE_Careers",
                                  wd_host="wd115"),
                     where="workday_tenant_catalog", industry="manufacturing_automotive",
                     note="The linked URL carries q=Onsite/Field Operations; that "
                          "sample filter is NOT part of the source."),
    "SEI": dict(domain="seic.com", state="CHAIN", plan="added",
                endpoint=_ep("workday", "seic", site="SEI_Global_Services",
                             wd_host="wd1"),
                where="workday_tenant_catalog", industry="fintech"),
    "Swift": dict(domain="swift.com", state="CHAIN", plan="added",
                  endpoint=_ep("workday", "swift", site="Join-Swift", wd_host="wd3"),
                  where="workday_tenant_catalog", industry="fintech"),
    "The Home Depot": dict(
        domain="homedepot.com", state="CHAIN", plan="added",
        endpoint=_ep("workday", "homedepot", site="CareerDepot", wd_host="wd5"),
        where="workday_tenant_catalog", industry="ecommerce_retail",
        note="CareerDepot is the corporate/salaried board proven by Req185723's "
             "Apply link. Hourly in-store roles use BrassRing and the associate "
             "site is internal; neither is this source."),
    # --- Oracle Recruiting Cloud -------------------------------------------
    "Honeywell Technologies": dict(
        domain="honeywell.com", state="CHAIN", plan="existing",
        endpoint=_ep("oracle_orc", "ibqbjb", site="CX_1",
                     host="ibqbjb.fa.ocs.oraclecloud.com"),
        where="seed", industry="manufacturing_automotive",
        note="The official link names site 'Honeywell' on pod ibqbjb; the "
             "existing CX_1 seed on the same pod serves the SAME requisitions "
             "(first 50 ids identical on 2026-10-21, 1,288 reported). Not added "
             "twice: a second site would duplicate every Honeywell posting."),
    "Howmet Aerospace": dict(
        domain="howmet.com", state="CHAIN", plan="added",
        endpoint=_ep("oracle_orc", "fa-exty-saasfaprod1", site="CX_1",
                     host="fa-exty-saasfaprod1.fa.ocs.oraclecloud.com"),
        where="seed", industry="aerospace_defense"),
    "The Depository Trust & Clearing Corporation (DTCC)": dict(
        domain="dtcc.com", state="CHAIN", plan="added",
        endpoint=_ep("oracle_orc", "ebxr", site="CX_1",
                     host="ebxr.fa.us2.oraclecloud.com"),
        where="seed", industry="fintech",
        note="The official link carries a category facet; the source is the "
             "whole CX_1 site (all categories)."),
    "Verint": dict(domain="verint.com", state="CHAIN", plan="added",
                   endpoint=_ep("oracle_orc", "fa-epcb-saasfaprod1", site="CX",
                                host="fa-epcb-saasfaprod1.fa.ocs.oraclecloud.com"),
                   where="seed", industry="tech"),
    # --- iCIMS / Jibe ------------------------------------------------------
    "Applied Systems": dict(
        domain="appliedsystems.com", state="CHAIN", plan="added",
        endpoint=_ep("icims", "globalcareers-appliedsystems",
                     host="globalcareers-appliedsystems.icims.com"),
        where="seed", industry="tech"),
    "Delta Electronics Americas": dict(
        domain="delta-americas.com", state="CHAIN", plan="added",
        endpoint=_ep("icims", "careers-delta-americas",
                     host="careers-delta-americas.icims.com"),
        where="seed", industry="manufacturing_automotive"),
    "EchoStar Corporation": dict(
        domain="echostar.com", state="CHAIN", plan="added",
        endpoint=_ep("radancy", "echostar", host="jobs.echostar.com"),
        where="seed", industry="tech",
        note="jobs.echostar.com is an iCIMS Jibe front end (canonical "
             "dish.jibeapply.com). Its public /api/jobs is the contract the "
             "`radancy` provider already reads for AMD and Rivian."),
    "First Citizens Bank": dict(
        domain="firstcitizens.com", state="FRONTEND", plan="added",
        endpoint=_ep("radancy", "firstcitizens", host="jobs.firstcitizens.com"),
        where="seed", industry="banking",
        note="iCIMS-branded Jibe front end; listing verified through /api/jobs."),
    "ICE": dict(
        domain="ice.com", state="FRONTEND", plan="added",
        endpoint=_ep("radancy", "ice", host="careers.ice.com"),
        where="seed", industry="fintech",
        note="iCIMS-branded Jibe front end; listing verified through /api/jobs."),
    # --- Taleo / Jobvite / UKG / JazzHR / ADP WFN ---------------------------
    "AAR": dict(domain="aarcorp.com", state="CHAIN", plan="existing",
                endpoint=_ep("taleo", "aarcorp", host="aarcorp.taleo.net"),
                where="seed",
                note="Taleo seed already present (career section 2 is the "
                     "portal the provider discovers)."),
    "Uplight": dict(domain="uplight.com", state="CHAIN", plan="added",
                    endpoint=_ep("jobvite", "uplight"), where="seed",
                    industry="energy",
                    note="General Application posting is not a requisition."),
    "Schurz Communications": dict(
        domain="schurz.com", state="CHAIN", plan="added",
        endpoint=_ep("ukg", "SCH1012SHRZ",
                     site="813f24f9-0154-46d6-bbb4-966dd708b1ff",
                     host="recruiting.ultipro.com"),
        where="seed", industry="media",
        note="The official link recruiting.ultipro.com/SCH1012SHRZ/ redirects "
             "to JobBoard/813f24f9-... (observed 2026-10-21); the GUID is from "
             "that redirect, not guessed."),
    "Troon": dict(domain="troon.com", state="CHAIN", plan="added",
                  endpoint=_ep("ukg", "TRO1001TROO",
                               site="2b13054b-60bb-410d-9136-e52c1c7d9720",
                               host="recruiting2.ultipro.com"),
                  where="seed", industry="hospitality"),
    "SoftWriters": dict(
        domain="softwriters.com", state="CHAIN", plan="unsupported",
        endpoint=_ep("ukg", "ROP1001ROPER",
                     site="cf088029-453b-46f8-bcab-46ffb10fdf54",
                     host="recruiting.ultipro.com"),
        shared_board={"parent": "Roper Technologies", "entity": "SoftWriters",
                      "provider_entity_filter": False},
        reason="shared Roper UKG board; the UKG provider cannot filter to the "
               "hiring entity, and importing every Roper job as SoftWriters is wrong"),
    "Productive Playhouse": dict(
        domain="productiveplayhouse.com", state="CHAIN", plan="added",
        endpoint=_ep("jazzhr", "productiveplayhouse",
                     host="productiveplayhouse.applytojob.com"),
        where="seed", industry="tech"),
    "Texas Windstorm Insurance Association": dict(
        domain="twia.org", state="CHAIN", plan="added",
        endpoint=_ep("adp_wfn", "5a22940c-3629-44d9-813a-0ccee8ac9aac",
                     site="19000101_000001", host="workforcenow.adp.com"),
        where="seed", industry="insurance"),
    # --- SAP SuccessFactors RMK (employer-hosted) ---------------------------
    "Celestica": dict(domain="celestica.com", state="FRONTEND", plan="added",
                      endpoint=_ep("successfactors", "celestica",
                                   host="careers.celestica.com"),
                      where="seed", industry="manufacturing_automotive"),
    "Nomura": dict(domain="nomura.com", state="FRONTEND", plan="added",
                   endpoint=_ep("successfactors", "nomura",
                                host="careers.nomura.com"),
                   where="seed", industry="banking",
                   note="Americas channel only; early careers on Tal.net is a "
                        "separate channel and is not this source."),
    "PSEG Long Island LLC": dict(
        domain="psegliny.com", state="FRONTEND", plan="added",
        endpoint=_ep("successfactors", "psegliny", host="jobs.psegliny.com"),
        where="seed", industry="energy"),
    "Schaeffler": dict(domain="schaeffler.com", state="FRONTEND", plan="added",
                       endpoint=_ep("successfactors", "schaeffler",
                                    host="jobs.schaeffler.com"),
                       where="seed", industry="manufacturing_automotive"),
    "The Lubrizol Corporation": dict(
        domain="lubrizol.com", state="FRONTEND", plan="added",
        endpoint=_ep("successfactors", "lubrizol", host="jobs.lubrizol.com"),
        where="seed", industry="manufacturing_automotive"),
    "Ventura Foods": dict(domain="venturafoods.com", state="FRONTEND",
                          plan="added",
                          endpoint=_ep("successfactors", "venturafoods",
                                       host="jobs.venturafoods.com"),
                          where="seed", industry="other"),
    # --- Phenom / Radancy TalentBrew / Amazon -------------------------------
    "Acrisure": dict(domain="acrisure.com", state="FRONTEND", plan="added",
                     endpoint=_ep("phenom", "acrisure", host="careers.acrisure.com"),
                     where="seed", industry="insurance"),
    "BAE Systems, Inc.": dict(
        domain="baesystems.com", state="FRONTEND", plan="added",
        endpoint=_ep("phenom", "baesystems", host="jobs.baesystems.com"),
        where="seed", industry="aerospace_defense",
        note="A Taleo seed `baesystems` already exists; health is checked "
             "separately and it is not this US Phenom front end."),
    "SolutionHealth": dict(
        domain="solutionhealth.org", state="FRONTEND", plan="unsupported",
        provider_needed="radancy_talentbrew",
        reason="Radancy TalentBrew front end (/search-jobs); /api/jobs is not "
               "served there and no TalentBrew provider exists"),
    "Amazon Web Services (AWS)": dict(
        domain="amazon.com", state="FRONTEND", plan="existing",
        endpoint=_ep("amazon", "amazon", host="www.amazon.jobs"),
        where="enterprise_catalog", business_unit_of="Amazon",
        note="AWS is an Amazon business unit on amazon.jobs; no second employer."),
    "Annapurna Labs": dict(
        domain="amazon.com", state="FRONTEND", plan="existing",
        endpoint=_ep("amazon", "amazon", host="www.amazon.jobs"),
        where="enterprise_catalog", business_unit_of="Amazon",
        note="Annapurna Labs is an Amazon team page on amazon.jobs."),
    # --- Providers another agent is building --------------------------------
    "Destro": dict(domain="destroai.com", state="CHAIN", plan="unsupported",
                   provider_needed="breezyhr"),
    "GM Performance Power Units": dict(domain="gmppu.com", state="CHAIN",
                                       plan="unsupported", provider_needed="dayforce"),
    "Inspira Financial": dict(domain="inspirafinancial.com", state="CHAIN",
                              plan="unsupported", provider_needed="adp_myjobs"),
    "Lincoln Institute of Land Policy": dict(domain="lincolninst.edu", state="CHAIN",
                                             plan="unsupported",
                                             provider_needed="paycom"),
    "Universal Background Screening": dict(domain="universalbackground.com",
                                           state="CHAIN", plan="unsupported",
                                           provider_needed="appone"),
    "RevSpring": dict(domain="revspringinc.com", state="CHAIN", plan="unsupported",
                      provider_needed="newton_paycor"),
    "Spring Arbor University": dict(domain="springarbor.edu", state="CHAIN",
                                    plan="unsupported",
                                    provider_needed="newton_paycor"),
    "Epic": dict(domain="epic.com", state="FRONTEND", plan="unsupported",
                 provider_needed="avature"),
    "Frequentis": dict(domain="frequentis.com", state="FRONTEND", plan="unsupported",
                       provider_needed="avature"),
    "Flint Hills Resources": dict(
        domain="fhr.com", state="FRONTEND", plan="unsupported",
        provider_needed="avature",
        shared_board={"parent": "Koch", "entity": "Flint Hills Resources",
                      "provider_entity_filter": False},
        note="Shared Koch Avature front end; must filter to FHR when built."),
    "TikTok USDS Joint Venture": dict(domain="tiktokusds.com", state="OWN",
                                      plan="unsupported",
                                      provider_needed="tiktok_usds"),
    "Fervo Energy": dict(domain="fervoenergy.com", state="OWN", plan="unsupported",
                         provider_needed="own_site"),
    "Longbridge": dict(domain="longbridge.com", state="OWN", plan="unsupported",
                       provider_needed="own_site"),
    "MATS Research": dict(domain="matsprogram.org", state="OWN", plan="unsupported",
                          provider_needed="own_site",
                          note="Staff roles only; fellowships are not jobs."),
    "Orange Logic": dict(domain="orangelogic.com", state="OWN", plan="unsupported",
                         provider_needed="own_site"),
    "Rugged Science": dict(domain="ruggedscience.com", state="OWN",
                           plan="unsupported", provider_needed="own_site"),
    "Temple Health – Temple University Health System": dict(
        domain="templehealth.org", state="OWN", plan="unsupported",
        provider_needed="own_site"),
    "Attune": dict(domain="attune.ai", state="MANUAL", plan="unsupported",
                   provider_needed="own_site",
                   note="Official roles apply via LinkedIn: manual handoff only."),
    "GMI Cloud": dict(domain="gmicloud.ai", state="MANUAL", plan="unsupported",
                      provider_needed="own_site",
                      note="Official roles apply via LinkedIn: manual handoff only."),
    "Nous Research": dict(domain="nousresearch.com", state="MANUAL",
                          plan="unsupported", provider_needed="own_site",
                          note="Inline roles with email apply: manual handoff; "
                               "never email automatically."),
    # --- Pending: identity / provider evidence missing ----------------------
    "American Airlines": dict(domain="aa.com", state="PENDING", plan="pending",
                              reason="jobs.aa.com answered 403; SAP RMK is a "
                                     "candidate only, never inferred from an HR "
                                     "job description"),
    "Atlys": dict(domain="atlys.com", state="PENDING", plan="pending",
                  reason="official site links only a LinkedIn company-jobs page"),
    "Carvana": dict(domain="carvana.com", state="PENDING", plan="pending",
                    reason="Greenhouse and Tenstreet disclosed; board token "
                           "unresolved (pages 403); `carvana` is not inferred"),
    "DataVisor": dict(domain="datavisor.com", state="PENDING", plan="pending",
                      reason="Workable whr_embed(56790); account-to-slug link "
                             "for datavisor-jobs not observed"),
    "LendingTree": dict(domain="lendingtree.com", state="PENDING", plan="pending",
                        reason="gh_jid hints only; no board token observed"),
    "Reflex": dict(domain="reflex.dev", state="PENDING", plan="pending",
                   reason="YC jobs publisher only; not workreflex.com or "
                          "greenhouse/reflex"),
    "Verisk": dict(domain="verisk.com", state="PENDING", plan="pending",
                   reason="corporate page challenged; SmartRecruiters/Oracle "
                          "candidates unproven"),
    "Upwind Security": dict(domain="upwind.io", state="PENDING", plan="pending",
                            reason="Comeet plugin observed; company token/API "
                                   "unresolved (official page 403)"),
}


# ---------------------------------------------------------------------------
# Existing static rows the brief asks us to health-check, not duplicate
# ---------------------------------------------------------------------------
HEALTH_CHECKS: Dict[str, Dict[str, Any]] = {
    "Duolingo": dict(domain="duolingo.com", plan="existing",
                     endpoint=_ep("greenhouse", "duolingo"),
                     where="career_pages_catalog"),
    "Nordstrom": dict(domain="nordstrom.com", plan="existing",
                      endpoint=_ep("workday", "nordstrom", site="nordstrom_careers",
                                   wd_host="wd501"),
                      where="workday_tenant_catalog"),
    "Garmin": dict(domain="garmin.com", plan="existing", endpoint=None,
                   where="official_registry",
                   note="Only an official_career_registry row (careers.garmin.com); "
                        "no crawlable catalog row in the repo. careers.garmin.com "
                        "is an iCIMS Jibe front end whose public /api/jobs reported "
                        "434 jobs on 2026-10-21 (readable by `radancy`). Not added: "
                        "production may already crawl Garmin through a discovered "
                        "iCIMS row in company_ats_endpoints, which this repo cannot "
                        "see, and a second vendor row would duplicate every Garmin "
                        "posting. Owner check before adding: "
                        "db.company_ats_endpoints.find({display_name: /garmin/i})."),
    "BAE Systems (Taleo seed)": dict(
        domain="baesystems.com", plan="existing",
        endpoint=_ep("taleo", "baesystems", host="baesystems.taleo.net"),
        where="seed",
        note="Pre-existing Taleo seed; the US Phenom front end is onboarded "
             "separately as BAE Systems, Inc."),
}


# ---------------------------------------------------------------------------
# Owner priority: top hiring employers per domain (2026-10-21)
# ---------------------------------------------------------------------------
#: ~25 well-known large US hirers per domain (30 for electrical/power, the
#: measured gap in docs/audit/feed-fidelity.md). A company may matter to two
#: domains; it is listed once, under the domain it is best known for.
TOP_EMPLOYERS: Dict[str, Any] = {
    "software": ["Microsoft", "Google", "Apple", "Amazon", "Meta", "Oracle",
                 "Salesforce", "Adobe", "IBM", "Intuit", "ServiceNow", "Workday",
                 "Atlassian", "Uber", "Airbnb", "Stripe", "Shopify", "Cisco",
                 "Autodesk", "PayPal", "Block", "Netflix", "DoorDash", "Bloomberg",
                 "Qualcomm"],
    "ai_ml": ["OpenAI", "Anthropic", "Google DeepMind", "NVIDIA", "xAI", "Scale AI",
              "Cohere", "Perplexity", "Character.AI", "Cursor", "Hugging Face",
              "Runway", "Glean", "Harvey", "Cerebras", "SambaNova", "Groq",
              "Together AI", "Figure AI", "Waymo", "Aurora Innovation",
              "Physical Intelligence", "Mistral AI", "ElevenLabs", "Abridge"],
    "data_science": ["Databricks", "Snowflake", "Palantir", "Confluent", "MongoDB",
                     "Datadog", "Elastic", "Fivetran", "dbt Labs", "Teradata",
                     "Cloudera", "Informatica", "Alteryx", "Domo", "ThoughtSpot",
                     "Sigma Computing", "Hex", "Starburst", "ClickHouse",
                     "Amplitude", "Two Sigma", "Capital One", "Nielsen",
                     "Experian", "Splunk"],
    "cloud": ["Cloudflare", "DigitalOcean", "CoreWeave", "Akamai", "Fastly",
              "HashiCorp", "Broadcom", "Red Hat", "Nutanix", "NetApp", "Equinix",
              "Digital Realty", "Rackspace", "Lambda", "Crusoe", "Vercel", "Okta",
              "Zscaler", "Palo Alto Networks", "Dell Technologies",
              "Hewlett Packard Enterprise", "Arista Networks", "Pure Storage",
              "Twilio", "Supermicro"],
    "electrical_power": ["Duke Energy", "Southern Company", "NextEra Energy",
                         "Dominion Energy", "American Electric Power", "Xcel Energy",
                         "Exelon", "PG&E", "Southern California Edison", "Entergy",
                         "Con Edison", "National Grid", "Eversource", "PSEG",
                         "DTE Energy", "Consumers Energy", "GE Vernova",
                         "Siemens Energy", "Hitachi Energy", "Schneider Electric",
                         "ABB", "Eaton", "Hubbell", "Quanta Services",
                         "Burns & McDonnell", "Black & Veatch", "Sargent & Lundy",
                         "POWER Engineers", "Texas Instruments", "Analog Devices"],
}


def _existing(endpoint: Dict[str, Any], **kw: Any) -> Dict[str, Any]:
    return dict(plan="existing", endpoint=endpoint, where="catalog_or_seed",
                state="EXISTING", **kw)


def _blocked(reason: str, **kw: Any) -> Dict[str, Any]:
    return dict(plan="blocked", reason=reason, state="BLOCKED", **kw)


def _pending(reason: str, **kw: Any) -> Dict[str, Any]:
    return dict(plan="pending", reason=reason, state="PENDING", **kw)


TOP_EMPLOYER_PLAN: Dict[str, Dict[str, Any]] = {
    # --- already crawled: the exact rows the crawl tables carry -------------
    "Microsoft": _existing(_ep("eightfold", "microsoft", host="apply.careers.microsoft.com"), note='Eightfold PCSX answered 403 to our validation (may be IP-specific; production inventory not visible here)'),
    "Google": _existing(_ep("google_careers", "google", host="www.google.com")),
    "Apple": _existing(_ep("custom_apple", "apple", host="jobs.apple.com")),
    "Amazon": _existing(_ep("amazon", "amazon", host="www.amazon.jobs")),
    "Oracle": _existing(_ep("oracle_orc", "eeho", site="CX_1", host="eeho.fa.us2.oraclecloud.com")),
    "Salesforce": _existing(_ep("workday", "salesforce", site="External_Career_Site", wd_host="wd12")),
    "Adobe": _existing(_ep("workday", "adobe", site="external_experienced", wd_host="wd5")),
    "IBM": _existing(_ep("custom_ibm", "ibm", host="www-api.ibm.com")),
    "ServiceNow": _existing(_ep("smartrecruiters", "servicenow")),
    "Workday": _existing(_ep("workday", "workday", site="Workday", wd_host="wd5")),
    "Atlassian": _existing(_ep("custom_atlassian", "atlassian", host="www.atlassian.com")),
    "Airbnb": _existing(_ep("greenhouse", "airbnb")),
    "Stripe": _existing(_ep("greenhouse", "stripe")),
    "Cisco": _existing(_ep("workday", "cisco", site="Cisco_Careers", wd_host="wd5")),
    "Autodesk": _existing(_ep("workday", "autodesk", site="Ext", wd_host="wd1")),
    "PayPal": _existing(_ep("workday", "paypal", site="jobs", wd_host="wd1")),
    "Block": _existing(_ep("greenhouse", "block")),
    "Netflix": _existing(_ep("eightfold", "netflix", host="explore.jobs.netflix.net")),
    "DoorDash": _existing(_ep("greenhouse", "doordashusa")),
    "Qualcomm": _existing(_ep("eightfold", "qualcomm", host="careers.qualcomm.com"), note='Eightfold answered 403 to our validation (may be IP-specific)'),
    "OpenAI": _existing(_ep("ashby", "openai")),
    "Anthropic": _existing(_ep("greenhouse", "anthropic")),
    "NVIDIA": _existing(_ep("workday", "nvidia", site="NVIDIAExternalCareerSite", wd_host="wd5")),
    "xAI": _existing(_ep("greenhouse", "xai")),
    "Scale AI": _existing(_ep("greenhouse", "scaleai")),
    "Cohere": _existing(_ep("ashby", "cohere")),
    "Perplexity": _existing(_ep("ashby", "perplexity")),
    "Character.AI": _existing(_ep("ashby", "character")),
    "Cursor": _existing(_ep("ashby", "cursor")),
    "Hugging Face": _existing(_ep("workable", "huggingface")),
    "Glean": _existing(_ep("greenhouse", "gleanwork")),
    "Harvey": _existing(_ep("ashby", "harvey")),
    "Cerebras": _existing(_ep("ashby", "cerebras")),
    "SambaNova": _existing(_ep("greenhouse", "sambanovasystems")),
    "Together AI": _existing(_ep("greenhouse", "togetherai")),
    "Waymo": _existing(_ep("greenhouse", "waymo")),
    "Aurora Innovation": _existing(_ep("greenhouse", "aurorainnovation"), note='greenhouse/aurorainnovation answers 404; aurora.tech/careers serves no board link (JS)'),
    "Physical Intelligence": _existing(_ep("ashby", "physicalintelligence")),
    "Mistral AI": _existing(_ep("ashby", "mistral.ai"),
                            note="Catalog slug `mistral.ai` was crawled as `mistral-ai` "
                                 "(404) because the loader slugged the dot; fixed in "
                                 "job_ingest_worker._board_token. 222 jobs on 2026-10-21."),
    "ElevenLabs": _existing(_ep("ashby", "elevenlabs")),
    "Abridge": _existing(_ep("ashby", "abridge")),
    "Databricks": _existing(_ep("greenhouse", "databricks")),
    "Palantir": _existing(_ep("lever", "palantir")),
    "Confluent": _existing(_ep("ashby", "confluent"), note='ashby/confluent answers 200 with zero jobs; careers.confluent.io answered 429'),
    "MongoDB": _existing(_ep("greenhouse", "mongodb")),
    "Datadog": _existing(_ep("greenhouse", "datadog")),
    "Elastic": _existing(_ep("greenhouse", "elastic")),
    "Fivetran": _existing(_ep("greenhouse", "fivetran")),
    "dbt Labs": _existing(_ep("greenhouse", "dbtlabsinc"), note='greenhouse/dbtlabsinc answers 404; getdbt.com careers page shows Jibe/RMK hints, board not established'),
    "Cloudera": _existing(_ep("workday", "cloudera", site="External_Career", wd_host="wd5")),
    "Sigma Computing": _existing(_ep("greenhouse", "sigmacomputing")),
    "Hex": _existing(_ep("greenhouse", "hextechnologies"), note='greenhouse/hextechnologies answers 404; hex.tech/careers serves no board link'),
    "Starburst": _existing(_ep("greenhouse", "starburst")),
    "ClickHouse": _existing(_ep("greenhouse", "clickhouse"), note='greenhouse/clickhouse answers 404; clickhouse.com careers serves no board link'),
    "Amplitude": dict(domain="amplitude.com", state="CHAIN", plan="added",
                      endpoint=_ep("ashby", "amplitude"), where="career_pages_catalog",
                      industry="tech", career_url="https://amplitude.com/careers",
                      note="Moved: greenhouse/amplitude 404s; the official careers page "
                           "links jobs.ashbyhq.com/amplitude. Catalog row repaired in place."),
    "Capital One": _existing(_ep("workday", "capitalone", site="Capital_One", wd_host="wd12")),
    "Experian": _existing(_ep("smartrecruiters", "experian")),
    "Cloudflare": _existing(_ep("greenhouse", "cloudflare")),
    "CoreWeave": _existing(_ep("greenhouse", "coreweave")),
    "Fastly": _existing(_ep("greenhouse", "fastly")),
    "Broadcom": _existing(_ep("workday", "broadcom", site="External_Career", wd_host="wd1")),
    "Red Hat": _existing(_ep("workday", "redhat", site="jobs", wd_host="wd5")),
    "Equinix": _existing(_ep("workday", "equinix", site="External", wd_host="wd1")),
    "Rackspace": _existing(_ep("workday", "rackspace", site="External", wd_host="wd1")),
    "Lambda": _existing(_ep("ashby", "lambda")),
    "Crusoe": _existing(_ep("ashby", "crusoe")),
    "Vercel": _existing(_ep("greenhouse", "vercel")),
    "Okta": _existing(_ep("greenhouse", "okta")),
    "Zscaler": _existing(_ep("greenhouse", "zscaler")),
    "Palo Alto Networks": _existing(_ep("workday", "paloaltonetworks", site="panwexternalcareers", wd_host="wd5")),
    "Dell Technologies": _existing(_ep("oracle_orc", "dell", site="CX_1001", host="enterpriseplatform.dell.com")),
    "Hewlett Packard Enterprise": _existing(_ep("workday", "hpe", site="Jobsathpe", wd_host="wd5")),
    "Pure Storage": _existing(_ep("greenhouse", "purestorage")),
    "Twilio": _existing(_ep("greenhouse", "twilio")),
    "Duke Energy": _existing(_ep("workday", "dukeenergy", site="Search", wd_host="wd1")),
    "American Electric Power": _existing(_ep("workday", "aep", site="AEPCareerSite", wd_host="wd1")),
    "Xcel Energy": _existing(_ep("workday", "xcelenergy", site="External", wd_host="wd1")),
    "Eversource": _existing(_ep("workday", "eversource", site="ExternalSite", wd_host="wd1")),
    "GE Vernova": _existing(_ep("workday", "gevernova", site="Vernova_ExternalSite", wd_host="wd5")),
    "Texas Instruments": _existing(_ep("oracle_orc", "edbz", site="CX_1", host="edbz.fa.us2.oraclecloud.com")),
    "Analog Devices": _existing(_ep("workday", "analogdevices", site="External", wd_host="wd1")),
    # Acquired; their roles are on the parent's board (no second employer).
    "Informatica": _existing(_ep("workday", "salesforce", site="External_Career_Site", wd_host="wd12"),
                             business_unit_of="Salesforce",
                             note="informatica.com/careers redirects to Salesforce careers "
                                  "(search=informatica), observed 2026-10-21."),
    "HashiCorp": _existing(_ep("custom_ibm", "ibm", host="www-api.ibm.com"),
                           business_unit_of="IBM", note="HashiCorp is an IBM company."),
    "Splunk": _existing(_ep("workday", "cisco", site="Cisco_Careers", wd_host="wd5"),
                        business_unit_of="Cisco", note="Splunk is a Cisco company."),

    # --- validated this pass on existing providers ---------------------------
    "Runway": dict(domain="runwayml.com", state="CHAIN", plan="added",
                   endpoint=_ep("ashby", "runway-ml"), where="career_pages_catalog",
                   industry="tech", career_url="https://runwayml.com/careers",
                   note="runwayml.com/careers -> runway.com/careers links jobs.ashbyhq.com/runway-ml."),
    "Alteryx": dict(domain="alteryx.com", state="CHAIN", plan="added",
                    endpoint=_ep("workday", "alteryx", site="AlteryxCareers", wd_host="wd108"),
                    where="workday_tenant_catalog", industry="tech",
                    career_url="https://www.alteryx.com/about-us/careers"),
    "Snowflake": dict(domain="snowflake.com", state="FRONTEND", plan="added",
                      endpoint=_ep("phenom", "snowflake", host="careers.snowflake.com"),
                      where="seed", industry="tech",
                      career_url="https://careers.snowflake.com/us/en"),
    "NetApp": dict(domain="netapp.com", state="CHAIN", plan="added",
                   endpoint=_ep("eightfold", "netapp", host="netapp.eightfold.ai",
                                domain="netapp.com"),
                   where="seed", industry="tech", career_url="https://careers.netapp.com/",
                   note="careers.netapp.com (TalentBrew front end) links netapp.eightfold.ai."),
    "Dominion Energy": dict(domain="dominionenergy.com", state="FRONTEND", plan="added",
                            endpoint=_ep("successfactors", "dominionenergy",
                                         host="careers.dominionenergy.com"),
                            where="seed", industry="energy",
                            career_url="https://careers.dominionenergy.com/"),
    "Entergy": dict(domain="entergy.com", state="CHAIN", plan="added",
                    endpoint=_ep("successfactors", "entergy", host="jobs.entergy.com"),
                    where="seed", industry="energy",
                    career_url="https://www.entergy.com/careers/",
                    note="entergy.com/careers redirects to jobs.entergy.com (RMK)."),
    "DTE Energy": dict(domain="dteenergy.com", state="FRONTEND", plan="added",
                       endpoint=_ep("successfactors", "dteenergy", host="careers.dteenergy.com"),
                       where="seed", industry="energy",
                       career_url="https://careers.dteenergy.com/"),
    "Consumers Energy": dict(domain="consumersenergy.com", state="CHAIN", plan="added",
                             endpoint=_ep("successfactors", "consumersenergy",
                                          host="careers.consumersenergy.com"),
                             where="seed", industry="energy",
                             career_url="https://www.consumersenergy.com/company/careers"),
    "NextEra Energy": dict(domain="nexteraenergy.com", state="CHAIN", plan="added",
                           endpoint=_ep("successfactors", "nexteraenergy",
                                        host="jobs.nexteraenergy.com"),
                           where="seed", industry="energy",
                           career_url="https://www.nexteraenergy.com/careers.html",
                           note="Includes Florida Power & Light (an NextEra company)."),
    "PSEG": dict(domain="pseg.com", state="FRONTEND", plan="added",
                 endpoint=_ep("successfactors", "pseg", host="jobs.pseg.com"),
                 where="seed", industry="energy", career_url="https://jobs.pseg.com/",
                 note="PSEG Long Island's own host (jobs.psegliny.com) is a separate source."),
    "Hubbell": dict(domain="hubbell.com", state="FRONTEND", plan="added",
                    endpoint=_ep("successfactors", "hubbell", host="careers.hubbell.com"),
                    where="seed", industry="manufacturing_automotive",
                    career_url="https://careers.hubbell.com/"),
    "Black & Veatch": dict(domain="bv.com", state="FRONTEND", plan="added",
                           endpoint=_ep("successfactors", "bv", host="careers.bv.com"),
                           where="seed", industry="energy",
                           career_url="https://careers.bv.com/"),
    "Con Edison": dict(domain="coned.com", state="CHAIN", plan="added",
                       endpoint=_ep("oracle_orc", "ejcu", site="CX_1033",
                                    host="ejcu.fa.us6.oraclecloud.com"),
                       where="seed", industry="energy",
                       career_url="https://www.coned.com/en/about-us/careers"),
    "Exelon": dict(domain="exeloncorp.com", state="FRONTEND", plan="added",
                   endpoint=_ep("radancy", "exeloncorp", host="jobs.exeloncorp.com"),
                   where="seed", industry="energy",
                   career_url="https://jobs.exeloncorp.com/careers-home/",
                   note="iCIMS Jibe front end (careers-exeloncorp.icims.com behind it)."),
    "Eaton": dict(domain="eaton.com", state="CHAIN", plan="candidate",
                  endpoint=_ep("eightfold", "eaton", host="eaton.eightfold.ai",
                               domain="eaton.com"),
                  where="seed", industry="manufacturing_automotive",
                  career_url="https://jobs.eaton.com/",
                  note="jobs.eaton.com redirects to eaton.eightfold.ai/careers. The "
                       "Eightfold API answered 403 to our validation on 2026-10-21, "
                       "so no row was added."),
    "ABB": dict(domain="abb.com", state="FRONTEND", plan="added",
                endpoint=_ep("phenom", "abb", host="careers.abb"),
                where="seed", industry="manufacturing_automotive",
                career_url="https://careers.abb/global/en"),
    "Quanta Services": dict(domain="quantaservices.com", state="CHAIN", plan="added",
                            endpoint=_ep("icims", "allcareers-quanta",
                                         host="allcareers-quanta.icims.com"),
                            where="seed", industry="energy",
                            career_url="https://www.quantaservices.com/careers",
                            note="Quanta's all-companies board; Quanta operating "
                                 "companies hire under the Quanta family."),
    "Sargent & Lundy": dict(domain="sargentlundy.com", state="CHAIN", plan="added",
                            endpoint=_ep("icims", "careers-sargentlundy",
                                         host="careers-sargentlundy.icims.com"),
                            where="seed", industry="energy",
                            career_url="https://www.sargentlundy.com/careers/",
                            note="US board; international-sargentlundy is separate."),

    # --- surfaces no existing provider reads ---------------------------------
    "PG&E": dict(plan="unsupported", state="FRONTEND", provider_needed="radancy_talentbrew",
                 domain="pge.com",
                 note="jobs.pge.com is TalentBrew over an SAP RCM tenant (C0000161245P); "
                      "RCM is not anonymously listable."),
    "Intuit": dict(plan="unsupported", state="FRONTEND", provider_needed="radancy_talentbrew",
                   domain="intuit.com", note="jobs.intuit.com is TalentBrew (/search-jobs)."),
    "Southern California Edison": dict(
        plan="unsupported", state="FRONTEND", provider_needed="symphony_cws",
        domain="edison.com",
        note="edisoncareers.com is a Symphony/CWS front end (jobsapi-google.m-cloud.io) "
             "over SAP RCM C0003043182P."),
    "Siemens Energy": dict(plan="unsupported", state="FRONTEND", provider_needed="avature",
                           domain="siemens-energy.com",
                           note="jobs.siemens-energy.com is Avature."),
    "Hitachi Energy": dict(plan="unsupported", state="FRONTEND",
                           domain="hitachienergy.com",
                           shared_board={"parent": "Hitachi", "entity": "Hitachi Energy",
                                         "provider_entity_filter": False},
                           reason="links Hitachi's shared Workday board "
                                  "(hitachi.wd1/hitachi); needs an entity filter"),
    "Uber": dict(plan="unsupported", state="OWN", provider_needed="uber_careers",
                 domain="uber.com",
                 note="MNC catalog engine `uber` has no termless provider (only "
                      "Uber Freight, a separate company, is crawled)."),
    "Meta": dict(plan="unsupported", state="OWN", provider_needed="meta_careers",
                 domain="meta.com",
                 note="metacareers.com is a JS app needing a browser session "
                      "(mnc_catalog record); left on the metered path."),

    # --- blocked to our fetcher (unverified, never zero) ---------------------
    "Bloomberg": _blocked("PerimeterX captcha (mnc_catalog record); not bypassed",
                          domain="bloomberg.com"),
    "Schneider Electric": _blocked("careers.se.com answered 403 on 2026-10-21",
                                   domain="se.com"),
    "National Grid": _blocked("careers.nationalgrid.com answered 403 on 2026-10-21",
                              domain="nationalgrid.com"),
    "POWER Engineers": _blocked("powereng.com/careers redirects to wsp.com (now WSP), "
                                "which answered 403 on 2026-10-21",
                                domain="powereng.com"),
    "Nutanix": _blocked("careers.nutanix.com answered 403 on 2026-10-21",
                        domain="nutanix.com"),
    "Akamai": _blocked("akamai.com/careers answered 403 on 2026-10-21",
                       domain="akamai.com"),
    "Arista Networks": _blocked("arista.com/en/careers answered 406 on 2026-10-21",
                                domain="arista.com"),
    "Domo": _blocked("domo.com/company/careers answered 405 on 2026-10-21",
                     domain="domo.com"),
    "Digital Realty": _blocked("TLS error fetching digitalrealty.com/about/careers "
                               "on 2026-10-21", domain="digitalrealty.com"),

    # --- next batch: official page is a JS shell / chain not yet observed ----
    "Google DeepMind": _pending("official careers page is client-rendered; board link "
                                "not in served HTML", domain="deepmind.google"),
    "Groq": _pending("groq.com/careers redirects to the homepage; no board link served",
                     domain="groq.com"),
    "Figure AI": _pending("official careers page served no ATS link", domain="figure.ai"),
    "Teradata": _pending("official careers page served no ATS link", domain="teradata.com"),
    "ThoughtSpot": _pending("official careers page served no ATS link",
                            domain="thoughtspot.com"),
    "Two Sigma": _pending("official careers page served no ATS link", domain="twosigma.com"),
    "DigitalOcean": _pending("open-roles page is a JS shell (XHRs Greenhouse per "
                             "CLAUDE.md); token not observed in served HTML",
                             domain="digitalocean.com"),
    "Shopify": _pending("official careers page served no ATS link", domain="shopify.com"),
    "Supermicro": _pending("official careers URL answered 404; current page unknown",
                           domain="supermicro.com"),
    "Nielsen": _pending("careers URL answered 404; NielsenIQ (crawled) is a different "
                        "company", domain="nielsen.com"),
    "Southern Company": _pending("only a DirectEmployers .jobs site "
                                 "(southerncompany.jobs) observed; employer ATS not "
                                 "established", domain="southerncompany.com"),
    "Burns & McDonnell": _pending("official link is burnsmcd.jobs (DirectEmployers); "
                                  "underlying ATS not established",
                                  domain="burnsmcd.com"),
}
