# Employer board backlog — November 2026

This ledger tracks all 150 items requested for this pass. Measurements are public HTTP observations or offline regression results, **not production inventory**. No `.env` was read and no database was connected. Unknown counts are never zero. Later phase PRs update their own rows; `queued` is unfinished work, not a finding.

## Phase 1 — quick wins

| Employer | Outcome | Evidence / observed UTC | Rows / distinct requisitions / US / board total | Detail | PR |
|---|---|---|---|---|---|
| Epic | needs_new_template:avature_nextjs | [https://careers.epic.com/](https://careers.epic.com/); 2026-11-05T22:45:28.793360Z | unknown / unknown / unknown / unknown | The own-site parser yielded 10 navigation/category links, not the 46 published positions. No row added. | boards/quick-wins |
| Frequentis | existing_revalidated | [https://www.frequentis.com/en/careers](https://www.frequentis.com/en/careers); 2026-11-05T22:45:10.369103Z | 62 / 62 / unknown / 62 | Already seeded in origin/main; reconcile stale unsupported plan without adding duplicate crawl member. | boards/quick-wins |
| Siemens | new_validated | [https://www.siemens.com/global/en/company/jobs.html](https://www.siemens.com/global/en/company/jobs.html); 2026-11-05T22:50:21.493331Z | 516 / 516 / 94 / unknown | Bounded at 150 seconds; 999+ is not an exact total. Locations in Siemens Energy listing are unknown; detail pages publish them. **Partial read.** | boards/quick-wins |
| Siemens Energy | new_validated | [https://www.siemens-energy.com/global/en/home/careers.html](https://www.siemens-energy.com/global/en/home/careers.html); 2026-11-05T22:52:55.127411Z | 1905 / 1905 / unknown / 1905 | Followed every published next-page link to exhaustion. Locations in Siemens Energy listing are unknown; detail pages publish them. | boards/quick-wins |
| Flint Hills Resources | existing_revalidated | [https://www.fhr.com/careers](https://www.fhr.com/careers); 2026-11-05T22:45:16.297954Z | 42 / 42 / unknown / 42 | Already seeded in origin/main; reconcile stale unsupported plan without adding duplicate crawl member. | boards/quick-wins |
| Destro | existing_revalidated | [https://www.destroai.com/careers](https://www.destroai.com/careers); 2026-11-05T22:44:59.834641Z | 13 / 13 / unknown / unknown | Already seeded in origin/main; reconcile stale unsupported plan without adding duplicate crawl member. | boards/quick-wins |
| Lincoln Institute of Land Policy | robots_refusal | [https://www.lincolninst.edu/about-lincoln-institute/careers/](https://www.lincolninst.edu/about-lincoln-institute/careers/); 2026-11-05T22:45:02.171749Z | unknown / unknown / unknown / unknown | Refusal: robots_refusal: https://www.paycomonline.net/v4/ats/web.php/jobs?clientkey=AC25DFC6E5002C200B92C92109B3F768 | boards/quick-wins |
| RevSpring | robots_refusal | [https://revspringinc.com/careers/](https://revspringinc.com/careers/); 2026-11-05T22:44:59.474378Z | unknown / unknown / unknown / unknown | Refusal: robots_refusal: https://recruitingbypaycor.com/career/AggregateCareerHome.action?clientId=8a7883d0798f66af0179905b7890010b | boards/quick-wins |
| Spring Arbor University | robots_refusal | [https://springarbor.edu/about/offices-and-schools/human-resources/careers.php](https://springarbor.edu/about/offices-and-schools/human-resources/careers.php); 2026-11-05T22:44:59.823428Z | unknown / unknown / unknown / unknown | Refusal: robots_refusal: https://recruitingbypaycor.com/career/CareerHome.action?clientId=8a7883c66ee9a5cc016ef0ea2d5b06cb | boards/quick-wins |
| Universal Background Screening | robots_refusal | [https://universalbackground.com/career-opportunities-with-universal-background-screening/](https://universalbackground.com/career-opportunities-with-universal-background-screening/); 2026-11-05T22:45:02.706048Z | unknown / unknown / unknown / unknown | Refusal: robots_refusal: https://recruiting.myapps.paychex.com/appone/Branding/ReqTemplate/BrowseAllJobsbyCategory.asp?Type=REQ&FromID=1&ClientID=18117&B_ID=91&JobCode=0&CountryID=3&LanguageID=2&servervar=universalbackgroundscreening.appone.com | boards/quick-wins |
| Fervo Energy | new_validated | [https://fervoenergy.com/jobs/fervoenergy/asset-manager-market-asset-operations/?gh_jid=4474295009](https://fervoenergy.com/jobs/fervoenergy/asset-manager-market-asset-operations/?gh_jid=4474295009); 2026-11-05T22:45:27.569747Z | 68 / 68 / unknown / unknown | Official job detail names exact Greenhouse board token; 68 real requisitions. | boards/quick-wins |
| Longbridge | blocked | [https://longbridge.com/hk/en/jobs/974e6b1b-0661-4d31-8c08-b28758e6d139](https://longbridge.com/hk/en/jobs/974e6b1b-0661-4d31-8c08-b28758e6d139); 2026-11-05T22:42:46.723027Z | unknown / unknown / unknown / unknown | 403 on robots.txt; host stopped. | boards/quick-wins |
| MATS Research | migrated_validated | [https://www.matsprogram.org/careers](https://www.matsprogram.org/careers); 2026-11-05T22:46:08.431257Z | 19 / 9 / 8 / 10 | 19 location rows, 10 IDs, 9 staff requisitions + 1 Expression of Interest. 8 requisitions have a US location; no fellowship rows. Old own-site listing yielded 0/unverified. Phase 2 fixes duplicate-location loss. | boards/quick-wins |
| Orange Logic | existing_revalidated | [https://www.orangelogic.com/about/careers](https://www.orangelogic.com/about/careers); 2026-11-05T22:45:06.415954Z | 7 / 7 / unknown / unknown | Already seeded in origin/main; reconcile stale unsupported plan without adding duplicate crawl member. | boards/quick-wins |
| Rugged Science | existing_revalidated | [https://ruggedscience.com/careers/](https://ruggedscience.com/careers/); 2026-11-05T22:45:07.600909Z | 9 / 9 / unknown / unknown | Already seeded in origin/main; reconcile stale unsupported plan without adding duplicate crawl member. | boards/quick-wins |
| Temple Health | needs_new_provider:silkroad | [https://www.templehealth.com/careers](https://www.templehealth.com/careers); 2026-11-05T22:42:49.155202Z | unknown / unknown / unknown / unknown | Official page links SilkRoad, which has no registered provider. | boards/quick-wins |
| Nous Research | existing_revalidated | [https://nousresearch.com/careers](https://nousresearch.com/careers); 2026-11-05T22:45:08.982887Z | 13 / 13 / unknown / unknown | Already seeded in origin/main; reconcile stale unsupported plan without adding duplicate crawl member. | boards/quick-wins |
| SoftWriters | robots_refusal | [https://softwriters.com/career-opportunities](https://softwriters.com/career-opportunities); 2026-11-05T22:42:52.184011Z | unknown / unknown / unknown / unknown | UKG robots disallows the linked OpportunityDetail; shared Roper entity boundary remains unproven. | boards/quick-wins |
| Hitachi Energy | needs_entity_filter | [https://www.hitachienergy.com/careers](https://www.hitachienergy.com/careers); 2026-11-05T22:43:03.072192Z | unknown / unknown / unknown / unknown | Official jobs page is an XHR shell; no proven hiring-entity filter on shared Hitachi Workday. | boards/quick-wins |
| Attune | manual | [https://www.attune.ai/company](https://www.attune.ai/company); 2026-11-05T22:42:53.361039Z | unknown / unknown / unknown / unknown | 3 official LinkedIn application links; manual handoff only. | boards/quick-wins |
| GMI Cloud | manual | [https://www.gmicloud.ai/en/company/career](https://www.gmicloud.ai/en/company/career); 2026-11-05T22:42:54.498965Z | unknown / unknown / unknown / unknown | 10 official LinkedIn application links; manual handoff only. | boards/quick-wins |
| Eaton | robots_refusal | [https://jobs.eaton.com/](https://jobs.eaton.com/); 2026-11-05T22:43:04.283975Z | unknown / unknown / unknown / unknown | Official redirect reaches Eightfold /careers, disallowed by robots; no API request sent. | boards/quick-wins |
| Evergy | unverified | [https://www.evergy.com/careers](https://www.evergy.com/careers); 2026-11-05T22:45:29.976010Z | unknown / unknown / unknown / unknown | Official link uses careersection/evergy_external_career_section. Reader returned 0, portal unresolved; not confirmed empty. | boards/quick-wins |

## Phase 2 — incomplete reads and board repairs

| Item | Category | Outcome | Evidence / UTC | Rows / US / total | PR |
|---|---|---|---|---|---|
| oracle_orc truncation | under-read | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| icims truncation | under-read | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| taleo truncation | under-read | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| Phenom paging | under-read | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| SmartRecruiters paging | under-read | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| Workable locations | under-read | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| SuccessFactors missing locations | under-read | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| Eightfold classic dates | under-read | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| Uber termless crawl | under-read | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| Microsoft | under-read | existing source row; owner verification remains (static audit) | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| Qualcomm | under-read | existing source row; owner verification remains (static audit) | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| Confluent | under-read | existing source row; owner verification remains (static audit) | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| Aurora Innovation | under-read | existing source row; owner verification remains (static audit) | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| dbt Labs | under-read | existing source row; owner verification remains (static audit) | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| Hex | under-read | existing source row; owner verification remains (static audit) | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| ClickHouse | under-read | existing source row; owner verification remains (static audit) | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |

## Phase 3 — employer research

| Item | Category | Outcome | Evidence / UTC | Rows / US / total | PR |
|---|---|---|---|---|---|
| American Airlines | research | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| Atlys | research | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| Carvana | research | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| DataVisor | research | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| LendingTree | research | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| Reflex | research | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| Verisk | research | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| Upwind Security | research | existing source row; owner verification remains (static audit) | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| Google DeepMind | research | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| Groq | research | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| Figure AI | research | existing source row; owner verification remains (static audit) | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| Teradata | research | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| ThoughtSpot | research | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| Two Sigma | research | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| DigitalOcean | research | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| Shopify | research | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| Supermicro | research | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| Nielsen | research | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| Southern Company | research | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| Burns & McDonnell | research | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| Kiewit | research | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| Stantec | research | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| Mott MacDonald | research | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| Jacobs | research | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| NRG Energy | research | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| Enphase Energy | research | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| Mitsubishi Electric Power Products | research | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| Wabtec | research | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| Procore | research | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| Fortinet | research | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| SentinelOne | research | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| TSMC | research | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| Synopsys | research | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| Teradyne | research | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| Etsy | research | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| EPAM | research | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| Klarna | research | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| Splunk | research | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| Zoom | research | existing source row; owner verification remains (static audit) | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| Seagate | research | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| X | research | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| Blue Origin | research | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| Deel | research | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| Electronic Arts | research | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| Riot Games | research | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| Take-Two Interactive | research | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| Ubisoft | research | existing source row; owner verification remains (static audit) | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| Valve | research | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| Pydantic | research | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| Turso | research | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| Wasmer | research | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| Oxide Computer | research | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| Protect AI | research | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| Ampere Computing | research | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| Celestial AI | research | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| Enfabrica | research | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| Axonius | research | existing source row; owner verification remains (static audit) | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| Tines | research | existing source row; owner verification remains (static audit) | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| Cato Networks | research | existing source row; owner verification remains (static audit) | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| EY | research | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| McKinsey | research | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| Boston Consulting Group | research | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| KPMG US | research | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| Deloitte | research | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| Walmart | research | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| Charter Communications | research | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| Comcast | research | existing source row; owner verification remains (static audit) | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| Optum | research | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| Cummins | research | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| ADP | research | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| Nokia | research | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| MathWorks | research | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| Rocket | research | existing source row; owner verification remains (static audit) | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |

## Phase 4 — provider value tests

| Item | Category | Outcome | Evidence / UTC | Rows / US / total | PR |
|---|---|---|---|---|---|
| Radancy TalentBrew search-jobs | new provider | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| SuccessFactors Career Site Builder | new provider | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| Uber termless reader | new provider | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| RippleHire | new provider | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| Teamtailor | new provider | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| Zoho Recruit | new provider | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| Personio | new provider | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| Pinpoint | new provider | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| Comeet | new provider | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| HiBob | new provider | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| Dover | new provider | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| Symphony Talent CWS | new provider | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| PageUp | new provider | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| PeopleAdmin | new provider | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| Interfolio | new provider | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| DirectEmployers jobs | new provider | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| Capgemini own API | new provider | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |

## Phase 5 — one wall retest (with phase 3)

| Item | Category | Outcome | Evidence / UTC | Rows / US / total | PR |
|---|---|---|---|---|---|
| Dayforce | wall | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| ADP MyJobs | wall | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| Bloomberg | wall | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| Tesla | wall | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| SAIC | wall | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| General Dynamics | wall | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| Cognizant | wall | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| Schneider Electric | wall | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| National Grid | wall | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| WSP | wall | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| Nutanix | wall | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| Akamai | wall | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| Arista Networks | wall | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| Domo | wall | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| Digital Realty | wall | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| Sempra | wall | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| Tennessee Valley Authority | wall | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| Littelfuse | wall | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| Avangrid | wall | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| Oncor | wall | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |
| Pharmacyclics | wall | queued | Awaiting public evidence | unknown / unknown / unknown | pending phase PR |

## Interpretation and contradictions

- Several quick-win seed rows already existed on `origin/main`; the onboarding plan was stale. Existing rows were reconciled instead of duplicated.
- MATS migrated to Rippling; Temple Health links SilkRoad. Epic uses an Avature/Next.js template that the existing generic own-page reader mistakes for navigation. No false-positive Epic row was added.
- Siemens and Siemens Energy use another Avature template. The extension follows only the page’s published pagination and marks bounded reads incomplete.
- The manifest found nine research employers with existing source rows. A registry coverage gap is not necessarily a missing crawler.
- SuccessFactors unified search, Wipro/HCLTech, Capgemini RMK, and Upwind’s Comeet HTML were already implemented; the cited historical documents predate them.
- Current main contains a Google paging reader even though the brief records a robots refusal. This pass does not exercise it; the owner should reconcile that existing source with the refusal before running it.

## Owner steps

Only the owner runs the following commands on the deployed backend after reviewing and merging the relevant PRs. Each database-writing pass has its dry-run immediately before it. Run one network pass at a time. No PR is merged by this task.

```bash
PYTHONPATH=shared/python python3 scripts/onboard_researched_companies.py
PYTHONPATH=shared/python python3 scripts/onboard_researched_companies.py --apply

PYTHONPATH=shared/python python3 scripts/verify_career_sources.py --mode content --workers 1
PYTHONPATH=shared/python python3 scripts/verify_career_sources.py --mode content --workers 1 --apply

PYTHONPATH=shared/python python3 scripts/verify_career_sources.py --mode backlink --workers 1
PYTHONPATH=shared/python python3 scripts/verify_career_sources.py --mode backlink --workers 1 --apply

PYTHONPATH=shared/python python3 scripts/employer_coverage_report.py --markdown
PYTHONPATH=shared/python python3 scripts/employer_coverage_report.py --markdown --persist
```

Configure `USAJOBS_API_KEY` and `USAJOBS_USER_AGENT` on the box using the free key from [USAJOBS developer access](https://developer.usajobs.gov/). The provider already exists; this task does not request, read, or set the credential.

The nine static-existing research entries still need the owner’s content/backlink verification. These commands are the exact read-only handoff for production evidence this task deliberately did not collect. Keep blocked and unknown boards distinct from genuinely empty boards.

## Evidence files

- `board-work-items-2026-11.json`: complete requested denominator and static source locations.
- `quick-wins-evidence-2026-11.json`: current phase-1 HTTP evidence, dates, counts and refusal reasons.
- `docs/audit/direct-employer-onboarding.{json,md}`: regenerated operational plan; historical validations retain their original dates.
