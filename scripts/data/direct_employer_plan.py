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
                    industry="aerospace"),
    "Northwood": dict(domain="northwoodspace.io", state="CHAIN", plan="added",
                      endpoint=_ep("ashby", "NorthwoodSpace"),
                      where="career_pages_catalog", industry="aerospace",
                      note="Board carries a 'General Application' posting; it is "
                           "counted as non-requisition, not as a role."),
    "Quantcast": dict(domain="quantcast.com", state="CHAIN", plan="added",
                      endpoint=_ep("ashby", "quantcast"),
                      where="career_pages_catalog", industry="tech",
                      note="Official /careers/proxy JSON names Ashby/quantcast; the "
                           "careers page's static 'No jobs found' template is not a zero."),
    "Quindar": dict(domain="quindar.space", state="CONFIG", plan="added",
                    endpoint=_ep("ashby", "quindar"), where="career_pages_catalog",
                    industry="aerospace",
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
    "NCR Voyix": dict(domain="ncrvoyix.com", state="CHAIN", plan="candidate",
                      endpoint=_ep("workday", "ncr", site="ext_us", wd_host="wd1"),
                      where="workday_tenant_catalog", industry="tech",
                      note="Official page links ext_us, ext_non_us and ext_apac; "
                           "ext_us is the US board."),
    "Vantor": dict(domain="vantor.com", state="CHAIN", plan="candidate",
                   endpoint=_ep("workday", "maxar", site="Vantor", wd_host="wd1"),
                   where="workday_tenant_catalog", industry="aerospace",
                   note="Maxar's tenant, Vantor's site: attributed to Vantor only."),
    "Vertex Pharmaceuticals": dict(
        domain="vrtx.com", state="CHAIN", plan="candidate",
        endpoint=_ep("workday", "vrtx", site="vertex_careers", wd_host="wd501"),
        where="workday_tenant_catalog", industry="pharma"),
    "OPENLANE": dict(domain="openlane.com", state="CHAIN", plan="candidate",
                     endpoint=_ep("workday", "openlane", site="OPENLANE_Careers",
                                  wd_host="wd115"),
                     where="workday_tenant_catalog", industry="automotive",
                     note="The linked URL carries q=Onsite/Field Operations; that "
                          "sample filter is NOT part of the source."),
    "SEI": dict(domain="seic.com", state="CHAIN", plan="candidate",
                endpoint=_ep("workday", "seic", site="SEI_Global_Services",
                             wd_host="wd1"),
                where="workday_tenant_catalog", industry="fintech"),
    "Swift": dict(domain="swift.com", state="CHAIN", plan="candidate",
                  endpoint=_ep("workday", "swift", site="Join-Swift", wd_host="wd3"),
                  where="workday_tenant_catalog", industry="fintech"),
    "The Home Depot": dict(
        domain="homedepot.com", state="CHAIN", plan="candidate",
        endpoint=_ep("workday", "homedepot", site="CareerDepot", wd_host="wd5"),
        where="workday_tenant_catalog", industry="ecommerce_retail",
        note="CareerDepot is the corporate/salaried board proven by Req185723's "
             "Apply link. Hourly in-store roles use BrassRing and the associate "
             "site is internal; neither is this source."),
    # --- Oracle Recruiting Cloud -------------------------------------------
    "Honeywell Technologies": dict(
        domain="honeywell.com", state="CHAIN", plan="candidate",
        endpoint=_ep("oracle_orc", "ibqbjb", site="Honeywell",
                     host="ibqbjb.fa.ocs.oraclecloud.com"),
        where="seed", industry="manufacturing",
        note="A seed for the same pod with site CX_1 already exists; the "
             "official link names site 'Honeywell'."),
    "Howmet Aerospace": dict(
        domain="howmet.com", state="CHAIN", plan="candidate",
        endpoint=_ep("oracle_orc", "fa-exty-saasfaprod1", site="CX_1",
                     host="fa-exty-saasfaprod1.fa.ocs.oraclecloud.com"),
        where="seed", industry="aerospace"),
    "The Depository Trust & Clearing Corporation (DTCC)": dict(
        domain="dtcc.com", state="CHAIN", plan="candidate",
        endpoint=_ep("oracle_orc", "ebxr", site="CX_1",
                     host="ebxr.fa.us2.oraclecloud.com"),
        where="seed", industry="fintech",
        note="The official link carries a category facet; the source is the "
             "whole CX_1 site (all categories)."),
    "Verint": dict(domain="verint.com", state="CHAIN", plan="candidate",
                   endpoint=_ep("oracle_orc", "fa-epcb-saasfaprod1", site="CX",
                                host="fa-epcb-saasfaprod1.fa.ocs.oraclecloud.com"),
                   where="seed", industry="tech"),
    # --- iCIMS / Jibe ------------------------------------------------------
    "Applied Systems": dict(
        domain="appliedsystems.com", state="CHAIN", plan="candidate",
        endpoint=_ep("icims", "globalcareers-appliedsystems",
                     host="globalcareers-appliedsystems.icims.com"),
        where="seed", industry="tech"),
    "Delta Electronics Americas": dict(
        domain="delta-americas.com", state="CHAIN", plan="candidate",
        endpoint=_ep("icims", "careers-delta-americas",
                     host="careers-delta-americas.icims.com"),
        where="seed", industry="manufacturing"),
    "EchoStar Corporation": dict(
        domain="echostar.com", state="CHAIN", plan="candidate",
        endpoint=_ep("radancy", "echostar", host="jobs.echostar.com"),
        where="seed", industry="tech",
        note="jobs.echostar.com is an iCIMS Jibe front end (canonical "
             "dish.jibeapply.com). Its public /api/jobs is the contract the "
             "`radancy` provider already reads for AMD and Rivian."),
    "First Citizens Bank": dict(
        domain="firstcitizens.com", state="FRONTEND", plan="candidate",
        endpoint=_ep("radancy", "firstcitizens", host="jobs.firstcitizens.com"),
        where="seed", industry="banking",
        note="iCIMS-branded Jibe front end; listing verified through /api/jobs."),
    "ICE": dict(
        domain="ice.com", state="FRONTEND", plan="candidate",
        endpoint=_ep("radancy", "ice", host="careers.ice.com"),
        where="seed", industry="fintech",
        note="iCIMS-branded Jibe front end; listing verified through /api/jobs."),
    # --- Taleo / Jobvite / UKG / JazzHR / ADP WFN ---------------------------
    "AAR": dict(domain="aarcorp.com", state="CHAIN", plan="existing",
                endpoint=_ep("taleo", "aarcorp", host="aarcorp.taleo.net"),
                where="seed",
                note="Taleo seed already present (career section 2 is the "
                     "portal the provider discovers)."),
    "Uplight": dict(domain="uplight.com", state="CHAIN", plan="candidate",
                    endpoint=_ep("jobvite", "uplight"), where="career_pages_catalog",
                    industry="energy",
                    note="General Application posting is not a requisition."),
    "Schurz Communications": dict(
        domain="schurz.com", state="CHAIN", plan="candidate",
        endpoint=_ep("ukg", "SCH1012SHRZ", site="",
                     host="recruiting.ultipro.com"),
        where="seed", industry="media",
        note="UKG needs the JobBoard GUID; it must come from an official link."),
    "Troon": dict(domain="troon.com", state="CHAIN", plan="candidate",
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
        domain="productiveplayhouse.com", state="CHAIN", plan="candidate",
        endpoint=_ep("jazzhr", "productiveplayhouse",
                     host="productiveplayhouse.applytojob.com"),
        where="seed", industry="tech"),
    "Texas Windstorm Insurance Association": dict(
        domain="twia.org", state="CHAIN", plan="candidate",
        endpoint=_ep("adp_wfn", "5a22940c-3629-44d9-813a-0ccee8ac9aac",
                     site="19000101_000001", host="workforcenow.adp.com"),
        where="seed", industry="insurance"),
    # --- SAP SuccessFactors RMK (employer-hosted) ---------------------------
    "Celestica": dict(domain="celestica.com", state="FRONTEND", plan="candidate",
                      endpoint=_ep("successfactors", "celestica",
                                   host="careers.celestica.com"),
                      where="seed", industry="manufacturing"),
    "Nomura": dict(domain="nomura.com", state="FRONTEND", plan="candidate",
                   endpoint=_ep("successfactors", "nomura",
                                host="careers.nomura.com"),
                   where="seed", industry="banking",
                   note="Americas channel only; early careers on Tal.net is a "
                        "separate channel and is not this source."),
    "PSEG Long Island LLC": dict(
        domain="psegliny.com", state="FRONTEND", plan="candidate",
        endpoint=_ep("successfactors", "psegliny", host="jobs.psegliny.com"),
        where="seed", industry="energy"),
    "Schaeffler": dict(domain="schaeffler.com", state="FRONTEND", plan="candidate",
                       endpoint=_ep("successfactors", "schaeffler",
                                    host="jobs.schaeffler.com"),
                       where="seed", industry="manufacturing_automotive"),
    "The Lubrizol Corporation": dict(
        domain="lubrizol.com", state="FRONTEND", plan="candidate",
        endpoint=_ep("successfactors", "lubrizol", host="jobs.lubrizol.com"),
        where="seed", industry="manufacturing"),
    "Ventura Foods": dict(domain="venturafoods.com", state="FRONTEND",
                          plan="candidate",
                          endpoint=_ep("successfactors", "venturafoods",
                                       host="jobs.venturafoods.com"),
                          where="seed", industry="other"),
    # --- Phenom / Radancy TalentBrew / Amazon -------------------------------
    "Acrisure": dict(domain="acrisure.com", state="FRONTEND", plan="candidate",
                     endpoint=_ep("phenom", "acrisure", host="careers.acrisure.com"),
                     where="seed", industry="insurance"),
    "BAE Systems, Inc.": dict(
        domain="baesystems.com", state="FRONTEND", plan="candidate",
        endpoint=_ep("phenom", "baesystems", host="jobs.baesystems.com"),
        where="seed", industry="aerospace",
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
                        "no crawlable catalog row in the repo."),
}
