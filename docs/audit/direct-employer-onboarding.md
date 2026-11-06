# Direct-employer onboarding — outcomes per company

Generated 2026-11-06T00:40:57Z by `scripts/onboard_researched_companies.py --write-docs` (direct-employer-onboarding-1.0.0). Research reviewed 2026-10-21.

**Coverage basis:** in-repo catalogs and seed tables the crawler loads (no production inventory was read). `existing_covered` means the crawl tables already carry the exact endpoint; it is not a claim about live inventory counts.

Validation counts: `source` = total the vendor reported (blank when the API reports none), `parsed` = rows our provider produced, `req` = distinct requisitions after removing General Application/talent-community rows (`non-req`). Status is complete | budget_limited | blocked | failed | not_run. A blocked/failed run is unverified, never zero.

## 174 researched LinkedIn leads

Denominator: **174**. existing_covered: 5, existing_unhealthy_or_moved: 4, new_validated: 59, excluded: 61, review: 31, unsupported: 9, pending: 5

| company | evidence | provider | endpoint | outcome | why | validated at | status | source | parsed | req | non-req |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 7Seventy Recruiting | excluded_by_preference |  |  | excluded | staffing |  | not_run |  |  |  |  |
| AAR | CHAIN | taleo | `taleo/aarcorp.taleo.net/aarcorp` | existing_covered | row already in the crawl tables | 2026-10-22T01:59:32Z | complete |  | 190 | 190 | 0 |
| AARATECH | identity_or_business_model_review |  |  | review | mixed_review |  | not_run |  |  |  |  |
| AbilityLinks | excluded_by_preference |  |  | excluded | intermediary |  | not_run |  |  |  |  |
| Accenture Federal Services | excluded_by_preference |  |  | excluded | consultancy |  | not_run |  |  |  |  |
| Acrisure | FRONTEND | phenom | `phenom/careers.acrisure.com/acrisure` | new_validated | row added; live validation returned requisitions | 2026-10-22T02:04:59Z | budget_limited | 224 | 100 | 100 | 0 |
| Agento | identity_unresolved |  |  | review | identity_unresolved |  | not_run |  |  |  |  |
| Alt | identity_unresolved |  |  | review | mixed_review |  | not_run |  |  |  |  |
| Amazon Web Services (AWS) | FRONTEND | amazon | `amazon/www.amazon.jobs/amazon` | existing_covered | row already in the crawl tables |  | not_run |  |  |  |  |
| Amentum | official_career_provider_unresolved |  |  | review | mixed_review |  | not_run |  |  |  |  |
| American Airlines | PENDING | Unresolved; SAP RMK candidate only |  | pending | jobs.aa.com answered 403; SAP RMK is a candidate only, never inferred from an HR job description |  | not_run |  |  |  |  |
| Amtex Systems Inc | excluded_by_preference |  |  | excluded | staffing |  | not_run |  |  |  |  |
| Anchorstacktech | excluded_by_preference |  |  | excluded | consultancy |  | not_run |  |  |  |  |
| Annapurna Labs | FRONTEND | amazon | `amazon/www.amazon.jobs/amazon` | existing_covered | row already in the crawl tables |  | not_run |  |  |  |  |
| Apollo Solutions | excluded_by_preference |  |  | excluded | staffing |  | not_run |  |  |  |  |
| Applied Systems | CHAIN | icims | `icims/globalcareers-appliedsystems.icims.com/globalcareers-appliedsystems` | new_validated | row added; live validation returned requisitions | 2026-10-22T02:02:22Z | complete |  | 19 | 19 | 0 |
| ApTask | excluded_by_preference |  |  | excluded | staffing |  | not_run |  |  |  |  |
| Ascent Developer Solutions | CHAIN | greenhouse | `greenhouse/ascentds` | new_validated | row added; live validation returned requisitions | 2026-10-22T01:59:20Z | complete | 11 | 11 | 11 | 0 |
| Astrion | identity_or_business_model_review |  |  | review | mixed_review |  | not_run |  |  |  |  |
| Atlys | PENDING |  |  | pending | official site links only a LinkedIn company-jobs page |  | not_run |  |  |  |  |
| Attune | MANUAL | own_site |  | unsupported | unsupported_pending_provider:own_site - 2026-11-05: official company page lists 3 LinkedIn apply links. Existing own-page discovery remains manual handoff; no LinkedIn fetching or application automation. |  | not_run |  |  |  |  |
| Avalore.ai | official_ats_chain | Workable |  | review | mixed_review |  | not_run |  |  |  |  |
| BAE Systems, Inc. | FRONTEND | phenom | `phenom/jobs.baesystems.com/baesystems` | new_validated | row added; live validation returned requisitions | 2026-10-22T02:05:05Z | budget_limited | 2042 | 100 | 100 | 0 |
| BeaconFire Inc. | excluded_by_preference |  |  | excluded | consultancy |  | not_run |  |  |  |  |
| Bixal | excluded_by_preference |  |  | excluded | consultancy |  | not_run |  |  |  |  |
| BLN24 | excluded_by_preference |  |  | excluded | consultancy |  | not_run |  |  |  |  |
| Booker DiMaio | excluded_by_preference |  |  | excluded | consultancy |  | not_run |  |  |  |  |
| Bowman Consulting | excluded_by_preference |  |  | excluded | consultancy |  | not_run |  |  |  |  |
| Brain Co. | official_ats_chain | Ashby |  | review | mixed_review |  | not_run |  |  |  |  |
| BrickRed Systems | excluded_by_preference |  |  | excluded | staffing |  | not_run |  |  |  |  |
| Brooksource | excluded_by_preference |  |  | excluded | staffing |  | not_run |  |  |  |  |
| Carvana | PENDING | Greenhouse disclosed; Tenstreet also disclosed; exact endpoint unresolved |  | pending | Greenhouse and Tenstreet disclosed; board token unresolved (pages 403); `carvana` is not inferred |  | not_run |  |  |  |  |
| Cbase Inc | excluded_by_preference |  |  | excluded | staffing |  | not_run |  |  |  |  |
| Celestica | FRONTEND | successfactors | `successfactors/careers.celestica.com/celestica` | new_validated | row added; live validation returned requisitions | 2026-10-22T02:03:52Z | budget_limited |  | 200 | 200 | 0 |
| CGI | excluded_by_preference |  |  | excluded | consultancy |  | not_run |  |  |  |  |
| Chalk | CHAIN | ashby | `ashby/chalk` | new_validated | row added; live validation returned requisitions | 2026-10-22T01:59:25Z | complete |  | 19 | 19 | 0 |
| ClarityPay | CHAIN | ashby | `ashby/claritypay` | new_validated | row added; live validation returned requisitions | 2026-10-22T01:59:25Z | complete |  | 12 | 12 | 0 |
| Clear Street | CONFIG | greenhouse | `greenhouse/clearstreet` | new_validated | row added; live validation returned requisitions | 2026-10-22T01:59:22Z | complete | 47 | 47 | 47 | 0 |
| Clinisoltech | excluded_by_preference |  |  | excluded | staffing |  | not_run |  |  |  |  |
| Code Connect Inc | excluded_by_preference |  |  | excluded | staffing |  | not_run |  |  |  |  |
| Cognite | CHAIN | greenhouse | `greenhouse/cognite/eu` | new_validated | row added; live validation returned requisitions | 2026-10-22T01:59:23Z | complete |  | 51 | 51 | 0 |
| ComResource | excluded_by_preference |  |  | excluded | consultancy |  | not_run |  |  |  |  |
| Creative Global Consulting | excluded_by_preference |  |  | excluded | consultancy |  | not_run |  |  |  |  |
| crewlia | excluded_by_preference |  |  | excluded | staffing |  | not_run |  |  |  |  |
| CrossTalent | identity_or_business_model_review |  |  | review | mixed_review |  | not_run |  |  |  |  |
| Dark Wolf | official_ats_chain | Greenhouse |  | review | mixed_review |  | not_run |  |  |  |  |
| DataVisor | CHAIN | workable | `workable/datavisor-jobs` | new_validated | row added; live validation returned requisitions | 2026-11-05T23:50:54.934428Z | complete |  | 33 | 27 | 0 |
| Delta Electronics Americas | CHAIN | icims | `icims/careers-delta-americas.icims.com/careers-delta-americas` | new_validated | row added; live validation returned requisitions | 2026-10-22T02:02:26Z | complete |  | 187 | 187 | 0 |
| Destro | CHAIN | breezy | `breezy/destro-ai.breezy.hr/destro-ai` | new_validated | row added; live validation returned requisitions | 2026-11-05T22:44:59.834641Z | complete |  | 13 | 13 | 0 |
| Digivance Solutions | excluded_by_preference |  |  | excluded | consultancy |  | not_run |  |  |  |  |
| E-Solutions | excluded_by_preference |  |  | excluded | staffing |  | not_run |  |  |  |  |
| EchoStar Corporation | CHAIN | radancy | `radancy/jobs.echostar.com/echostar` | new_validated | row added; live validation returned requisitions | 2026-10-22T02:03:42Z | budget_limited | 656 | 200 | 200 | 0 |
| ECI | identity_or_business_model_review |  |  | review | mixed_review |  | not_run |  |  |  |  |
| ECS | identity_or_business_model_review |  |  | review | mixed_review |  | not_run |  |  |  |  |
| Enlighten | identity_or_business_model_review |  |  | review | mixed_review |  | not_run |  |  |  |  |
| Epic | FRONTEND | avature |  | unsupported | unsupported_pending_provider:avature - 2026-11-05: official /jobs publishes a Next.js avaturePositions payload with open/published flags. Generic own-site parser returned 10 navigation/category links while the rendered b |  | not_run |  |  |  |  |
| Evlo AI | identity_unresolved |  |  | review | identity_unresolved |  | not_run |  |  |  |  |
| FDE Times | identity_or_business_model_review |  |  | review | identity_unresolved |  | not_run |  |  |  |  |
| FDM Group | excluded_by_preference |  |  | excluded | consultancy |  | not_run |  |  |  |  |
| Fervo Energy | CHAIN | greenhouse | `greenhouse/fervoenergy` | new_validated | row added; live validation returned requisitions | 2026-11-05T22:45:27.569747Z | complete | 68 | 68 | 68 | 0 |
| FieldAI | CONFIG | lever | `lever/field-ai` | existing_covered | row already in the crawl tables | 2026-10-22T01:59:29Z | complete |  | 109 | 108 | 1 |
| First Citizens Bank | FRONTEND | radancy | `radancy/jobs.firstcitizens.com/firstcitizens` | new_validated | row added; live validation returned requisitions | 2026-10-22T02:03:47Z | budget_limited | 389 | 200 | 200 | 0 |
| firstPRO US | excluded_by_preference |  |  | excluded | staffing |  | not_run |  |  |  |  |
| Flexcompute | CHAIN | workable | `workable/flexcompute` | new_validated | row added; live validation returned requisitions | 2026-10-22T01:59:29Z | complete |  | 20 | 20 | 0 |
| Flint Hills Resources | FRONTEND | avature | `avature/koch.avature.net/koch.avature.net/en_US/careers?732=6297&tags=rm.kcm.web.kcm-004` | new_validated | row added; live validation returned requisitions | 2026-11-05T22:45:16.297954Z | complete |  | 42 | 42 | 0 |
| Fluidstack | CHAIN | ashby | `ashby/fluidstack` | new_validated | row added; live validation returned requisitions | 2026-10-22T01:59:25Z | complete |  | 254 | 253 | 1 |
| Fonzi AI | excluded_by_preference |  |  | excluded | intermediary |  | not_run |  |  |  |  |
| Frequentis | FRONTEND | avature | `avature/jobs.frequentis.com/jobs.frequentis.com/careers` | new_validated | row added; live validation returned requisitions | 2026-11-05T22:45:10.369103Z | complete |  | 62 | 62 | 0 |
| Fueled | excluded_by_preference |  |  | excluded | consultancy |  | not_run |  |  |  |  |
| Fulcronix | excluded_by_preference |  |  | excluded | consultancy |  | not_run |  |  |  |  |
| GM Performance Power Units | CHAIN | dayforce |  | unsupported | unsupported_pending_provider:dayforce |  | not_run |  |  |  |  |
| GMI Cloud | MANUAL | own_site |  | unsupported | unsupported_pending_provider:own_site - 2026-11-05: official careers page lists 10 LinkedIn apply links. Existing own-page discovery remains manual handoff; no LinkedIn fetching or application automation. |  | not_run |  |  |  |  |
| Harnham | excluded_by_preference |  |  | excluded | staffing |  | not_run |  |  |  |  |
| Harrison Clarke | excluded_by_preference |  |  | excluded | staffing |  | not_run |  |  |  |  |
| HavocAI | CHAIN | ashby | `ashby/havocai` | new_validated | row added; live validation returned requisitions | 2026-10-22T01:59:26Z | complete |  | 26 | 26 | 0 |
| Haystack | identity_or_business_model_review |  |  | review | mixed_review |  | not_run |  |  |  |  |
| Honeywell Technologies | CHAIN | oracle_orc | `oracle_orc/ibqbjb.fa.ocs.oraclecloud.com/ibqbjb/CX_1` | existing_covered | row already in the crawl tables | 2026-10-22T02:03:36Z | budget_limited | 1288 | 200 | 200 | 0 |
| Hover | CONFIG | greenhouse | `greenhouse/hover` | new_validated | row added; live validation returned requisitions | 2026-10-22T01:59:22Z | complete | 19 | 19 | 19 | 0 |
| Howmet Aerospace | CHAIN | oracle_orc | `oracle_orc/fa-exty-saasfaprod1.fa.ocs.oraclecloud.com/fa-exty-saasfaprod1/CX_1` | new_validated | row added; live validation returned requisitions | 2026-10-22T02:02:15Z | budget_limited | 790 | 200 | 200 | 0 |
| ICE | FRONTEND | radancy | `radancy/careers.ice.com/ice` | new_validated | row added; live validation returned requisitions | 2026-10-22T02:03:50Z | budget_limited | 293 | 200 | 200 | 0 |
| Infinite Computer Solutions | excluded_by_preference |  |  | excluded | consultancy |  | not_run |  |  |  |  |
| Innovative Defense Technologies (IDT) | official_ats_chain | Pinpoint |  | review | mixed_review |  | not_run |  |  |  |  |
| Inspira Financial | CHAIN | adp_myjobs |  | unsupported | unsupported_pending_provider:adp_myjobs |  | not_run |  |  |  |  |
| InstantServe LLC | excluded_by_preference |  |  | excluded | staffing |  | not_run |  |  |  |  |
| Intuitive | CHAIN | smartrecruiters | `smartrecruiters/Intuitive` | new_validated | row added; live validation returned requisitions | 2026-10-22T02:00:10Z | budget_limited | 450 | 200 | 200 | 0 |
| Iperium | excluded_by_preference |  |  | excluded | staffing |  | not_run |  |  |  |  |
| KTek Resourcing | excluded_by_preference |  |  | excluded | staffing |  | not_run |  |  |  |  |
| Largeton Group | excluded_by_preference |  |  | excluded | staffing |  | not_run |  |  |  |  |
| LendingTree | CHAIN | greenhouse | `greenhouse/lendingtree` | new_validated | row added; live validation returned requisitions | 2026-11-05T23:18:23.240473Z | complete | 23 | 23 | 23 | 0 |
| Lincoln Institute of Land Policy | CHAIN | paycom | `paycom/www.paycomonline.net/AC25DFC6E5002C200B92C92109B3F768` | existing_unhealthy_or_moved | validation blocked - 2026-11-05: existing seed, ownership chain reconfirmed; Refusal: robots_refusal: https://www.paycomonline.net/v4/ats/web.php/jobs?clientkey=AC25DFC6E5002C200B92C92109B3F768. No blocked request was se | 2026-11-05T22:45:02.171749Z | blocked |  | 0 | 0 |  |
| Longbridge | OWN | own_site |  | unsupported | unsupported_pending_provider:own_site - 2026-11-05: longbridge.com/robots.txt answered 403; stopped host, no listing probe or new row. |  | not_run |  |  |  |  |
| MANTECH | official_career_provider_unresolved |  |  | review | mixed_review |  | not_run |  |  |  |  |
| Maritz | official_ats_chain | Workday |  | review | mixed_review |  | not_run |  |  |  |  |
| Marlin Selection Recruitment | excluded_by_preference |  |  | excluded | staffing |  | not_run |  |  |  |  |
| MatchPoint | identity_or_business_model_review |  |  | review | mixed_review |  | not_run |  |  |  |  |
| MATS Research | CONFIG | rippling | `rippling/mats-research` | new_validated | row added; live validation returned requisitions | 2026-11-05T22:46:08.431257Z | complete |  | 19 | 9 | 1 |
| Mission Technologies, a division of HII | official_career_provider_unresolved |  |  | review | mixed_review |  | not_run |  |  |  |  |
| Morales Professional, a morales group company | excluded_by_preference |  |  | excluded | staffing |  | not_run |  |  |  |  |
| Motion Recruitment | excluded_by_preference |  |  | excluded | staffing |  | not_run |  |  |  |  |
| mroads | excluded_by_preference |  |  | excluded | consultancy |  | not_run |  |  |  |  |
| NCR Voyix | CHAIN | workday | `workday/ncr/ext_us/wd1` | new_validated | row added; live validation returned requisitions | 2026-10-22T02:01:17Z | complete |  | 175 | 175 | 0 |
| Netrolynx AI | identity_unresolved |  |  | review | identity_unresolved |  | not_run |  |  |  |  |
| NexGen Tech Solutions | excluded_by_preference |  |  | excluded | staffing |  | not_run |  |  |  |  |
| Nomura | FRONTEND | successfactors | `successfactors/careers.nomura.com/nomura` | new_validated | row added; live validation returned requisitions | 2026-10-22T02:03:58Z | budget_limited |  | 200 | 200 | 0 |
| Northwood | CHAIN | ashby | `ashby/NorthwoodSpace` | new_validated | row added; live validation returned requisitions | 2026-10-22T01:59:26Z | complete |  | 63 | 62 | 1 |
| Nous Research | MANUAL | careers_page | `careers_page/nousresearch.com/nousresearch.com` | new_validated | row added; live validation returned requisitions | 2026-11-05T22:45:08.982887Z | complete |  | 13 | 13 | 0 |
| OPENLANE | CHAIN | workday | `workday/openlane/OPENLANE_Careers/wd115` | new_validated | row added; live validation returned requisitions | 2026-10-22T02:01:46Z | complete |  | 139 | 139 | 0 |
| Optomi | excluded_by_preference |  |  | excluded | staffing |  | not_run |  |  |  |  |
| Orange Logic | OWN | careers_page | `careers_page/orangelogic.com/orangelogic.com` | new_validated | row added; live validation returned requisitions | 2026-11-05T22:45:06.415954Z | complete |  | 7 | 7 | 0 |
| Peraton | official_ats_chain | iCIMS |  | review | mixed_review |  | not_run |  |  |  |  |
| Premier Global Links LLC | excluded_by_preference |  |  | excluded | consultancy |  | not_run |  |  |  |  |
| Productive Playhouse | CHAIN | jazzhr | `jazzhr/productiveplayhouse.applytojob.com/productiveplayhouse` | new_validated | row added; live validation returned requisitions | 2026-10-22T02:02:42Z | complete |  | 79 | 79 | 0 |
| PSEG Long Island LLC | FRONTEND | successfactors | `successfactors/jobs.psegliny.com/psegliny` | new_validated | row added; live validation returned requisitions | 2026-10-22T02:04:04Z | complete |  | 32 | 32 | 0 |
| Quantcast | CHAIN | ashby | `ashby/quantcast` | new_validated | row added; live validation returned requisitions | 2026-10-22T01:59:27Z | complete |  | 15 | 15 | 0 |
| Quindar | CONFIG | ashby | `ashby/quindar` | new_validated | row added; live validation returned requisitions | 2026-10-22T01:59:27Z | complete |  | 20 | 20 | 0 |
| Radiant Systems Inc | excluded_by_preference |  |  | excluded | staffing |  | not_run |  |  |  |  |
| Raft | official_career_provider_unresolved |  |  | review | mixed_review |  | not_run |  |  |  |  |
| Re:Build | CHAIN | greenhouse | `greenhouse/rebuildmanufacturing` | new_validated | row added; live validation returned requisitions | 2026-10-22T01:59:22Z | complete | 130 | 130 | 130 | 0 |
| Recruiting from Scratch | excluded_by_preference |  |  | excluded | staffing |  | not_run |  |  |  |  |
| Reflex | PENDING | YC jobs publisher; ATS unresolved |  | pending | YC jobs publisher only; not workreflex.com or greenhouse/reflex |  | not_run |  |  |  |  |
| RemoteHunter | excluded_by_preference |  |  | excluded | intermediary |  | not_run |  |  |  |  |
| RevSpring | CHAIN | paycor | `paycor/recruitingbypaycor.com/8a7883d0798f66af0179905b7890010b/aggregate` | existing_unhealthy_or_moved | validation blocked - 2026-11-05: existing seed, ownership chain reconfirmed; Refusal: robots_refusal: https://recruitingbypaycor.com/career/AggregateCareerHome.action?clientId=8a7883d0798f66af0179905b7890010b. No blocked | 2026-11-05T22:44:59.474378Z | blocked |  | 0 | 0 |  |
| Rhoda AI | CHAIN | ashby | `ashby/rhoda-ai` | new_validated | row added; live validation returned requisitions | 2026-10-22T01:59:28Z | complete |  | 45 | 45 | 0 |
| Rise Internet | identity_unresolved |  |  | review | identity_unresolved |  | not_run |  |  |  |  |
| Rugged Science | OWN | careers_page | `careers_page/ruggedscience.com/ruggedscience.com` | new_validated | row added; live validation returned requisitions | 2026-11-05T22:45:07.600909Z | complete |  | 9 | 9 | 0 |
| Saragossa | excluded_by_preference |  |  | excluded | staffing |  | not_run |  |  |  |  |
| Schaeffler | FRONTEND | successfactors | `successfactors/jobs.schaeffler.com/schaeffler` | new_validated | row added; live validation returned requisitions | 2026-10-22T02:04:06Z | budget_limited |  | 200 | 200 | 0 |
| Schurz Communications | CHAIN | ukg | `ukg/recruiting.ultipro.com/SCH1012SHRZ/813f24f9-0154-46d6-bbb4-966dd708b1ff` | new_validated | row added; live validation returned requisitions | 2026-10-22T02:03:52Z | complete | 18 | 18 | 18 | 0 |
| SEI | CHAIN | workday | `workday/seic/SEI_Global_Services/wd1` | new_validated | row added; live validation returned requisitions | 2026-10-22T02:01:54Z | complete | 78 | 78 | 78 | 0 |
| Sigma Defense Systems LLC | official_career_provider_unresolved |  |  | review | mixed_review |  | not_run |  |  |  |  |
| SimVentions | official_ats_chain | iCIMS |  | review | mixed_review |  | not_run |  |  |  |  |
| SoftWriters | CHAIN | ukg | `ukg/recruiting.ultipro.com/ROP1001ROPER/cf088029-453b-46f8-bcab-46ffb10fdf54` | unsupported | shared Roper UKG board; the UKG provider cannot filter to the hiring entity, and importing every Roper job as SoftWriters is wrong - 2026-11-05: official page still links Roper shared UKG board. recruiting.ultipro.com ro |  | not_run |  |  |  |  |
| SolutionHealth | FRONTEND | radancy_talentbrew | `radancy_talentbrew/careers.solutionhealth.org/47438` | new_validated | row added; live validation returned requisitions | 2026-11-05T23:24:09Z | budget_limited |  | 15 | 15 | 0 |
| SoTalent | excluded_by_preference |  |  | excluded | intermediary |  | not_run |  |  |  |  |
| Spring Arbor University | CHAIN | paycor | `paycor/recruitingbypaycor.com/8a7883c66ee9a5cc016ef0ea2d5b06cb` | existing_unhealthy_or_moved | validation blocked - 2026-11-05: existing seed, ownership chain reconfirmed; Refusal: robots_refusal: https://recruitingbypaycor.com/career/CareerHome.action?clientId=8a7883c66ee9a5cc016ef0ea2d5b06cb. No blocked request  | 2026-11-05T22:44:59.823428Z | blocked |  | 0 | 0 |  |
| Spring Venture Group | CHAIN | smartrecruiters | `smartrecruiters/SpringVentureGroup1` | new_validated | row added; live validation returned requisitions | 2026-10-22T01:59:32Z | complete | 6 | 6 | 6 | 0 |
| STR | official_ats_chain | Greenhouse |  | review | mixed_review |  | not_run |  |  |  |  |
| Suron | identity_or_business_model_review |  |  | review | mixed_review |  | not_run |  |  |  |  |
| Swift | CHAIN | workday | `workday/swift/Join-Swift/wd3` | new_validated | row added; live validation returned requisitions | 2026-10-22T02:01:58Z | complete |  | 47 | 47 | 0 |
| TalentAlly | excluded_by_preference |  |  | excluded | intermediary |  | not_run |  |  |  |  |
| TalentHop | identity_or_business_model_review |  |  | review | mixed_review |  | not_run |  |  |  |  |
| TalentOla | excluded_by_preference |  |  | excluded | staffing |  | not_run |  |  |  |  |
| TaskVerse | excluded_by_preference |  |  | excluded | intermediary |  | not_run |  |  |  |  |
| Tech Consulting | excluded_by_preference |  |  | excluded | staffing |  | not_run |  |  |  |  |
| Tecology, Inc. | excluded_by_preference |  |  | excluded | consultancy |  | not_run |  |  |  |  |
| Temple Health – Temple University Health System | OWN | silkroad |  | unsupported | unsupported_pending_provider:silkroad - 2026-11-05: careers.templehealth.org redirects to templehealth.org/careers; official page links https://jobs.silkroad.com/templehealth/Careers. No SilkRoad provider; do not treat n |  | not_run |  |  |  |  |
| Texas Windstorm Insurance Association | CHAIN | adp_wfn | `adp_wfn/workforcenow.adp.com/5a22940c-3629-44d9-813a-0ccee8ac9aac/19000101_000001` | new_validated | row added; live validation returned requisitions | 2026-10-22T02:02:42Z | complete |  | 5 | 5 | 0 |
| The Depository Trust & Clearing Corporation (DTCC) | CHAIN | oracle_orc | `oracle_orc/ebxr.fa.us2.oraclecloud.com/ebxr/CX_1` | new_validated | row added; live validation returned requisitions | 2026-10-22T02:02:19Z | budget_limited | 452 | 200 | 200 | 0 |
| The Home Depot | CHAIN | workday | `workday/homedepot/CareerDepot/wd5` | new_validated | row added; live validation returned requisitions | 2026-10-22T02:02:01Z | budget_limited | 985 | 200 | 200 | 0 |
| The Lubrizol Corporation | FRONTEND | successfactors | `successfactors/jobs.lubrizol.com/lubrizol` | new_validated | row added; live validation returned requisitions | 2026-10-22T02:04:15Z | budget_limited |  | 200 | 200 | 0 |
| TikTok USDS Joint Venture | OWN | tiktok_usds |  | unsupported | unsupported_pending_provider:tiktok_usds |  | not_run |  |  |  |  |
| Torentify | excluded_by_preference |  |  | excluded | intermediary |  | not_run |  |  |  |  |
| Trexquant Investment LP | CONFIG | workable | `workable/trexquant` | new_validated | row added; live validation returned requisitions | 2026-10-22T01:59:29Z | complete |  | 50 | 34 | 0 |
| Troon | CHAIN | ukg | `ukg/recruiting2.ultipro.com/TRO1001TROO/2b13054b-60bb-410d-9136-e52c1c7d9720` | new_validated | row added; live validation returned requisitions | 2026-10-22T02:02:38Z | budget_limited | 2115 | 200 | 200 | 0 |
| UICGS / Bowhead Family of Companies | official_ats_chain | iCIMS |  | review | mixed_review |  | not_run |  |  |  |  |
| Unison | CHAIN | ashby | `ashby/unison-software` | new_validated | row added; live validation returned requisitions | 2026-10-22T01:59:28Z | complete |  | 13 | 13 | 0 |
| Universal Background Screening | CHAIN | appone | `appone/www2.appone.com/universalbackgroundscreening.appone.com` | existing_unhealthy_or_moved | validation blocked - 2026-11-05: existing seed, ownership chain reconfirmed; Refusal: robots_refusal: https://recruiting.myapps.paychex.com/appone/Branding/ReqTemplate/BrowseAllJobsbyCategory.asp?Type=REQ&FromID=1&Client | 2026-11-05T22:45:02.706048Z | blocked |  | 0 | 0 |  |
| Uplight | CHAIN | jobvite | `jobvite/uplight` | new_validated | row added; live validation returned requisitions | 2026-10-22T02:02:38Z | complete |  | 12 | 12 | 0 |
| Upwind Security | PENDING | Comeet plugin |  | pending | Comeet plugin observed; company token/API unresolved (official page 403) |  | not_run |  |  |  |  |
| V-Soft Consulting Group, Inc. | excluded_by_preference |  |  | excluded | staffing |  | not_run |  |  |  |  |
| Vantor | CHAIN | workday | `workday/maxar/Vantor/wd1` | new_validated | row added; live validation returned requisitions | 2026-10-22T02:01:31Z | complete | 143 | 143 | 143 | 0 |
| Vaspire Technologies Inc. | excluded_by_preference |  |  | excluded | staffing |  | not_run |  |  |  |  |
| Ventura Foods | FRONTEND | successfactors | `successfactors/jobs.venturafoods.com/venturafoods` | new_validated | row added; live validation returned requisitions | 2026-10-22T02:04:23Z | complete |  | 98 | 98 | 0 |
| Ventures Unlimited Inc | excluded_by_preference |  |  | excluded | staffing |  | not_run |  |  |  |  |
| Verint | CHAIN | oracle_orc | `oracle_orc/fa-epcb-saasfaprod1.fa.ocs.oraclecloud.com/fa-epcb-saasfaprod1/CX` | new_validated | row added; live validation returned requisitions | 2026-10-22T02:02:21Z | complete | 33 | 33 | 33 | 0 |
| Verisk | CHAIN | oracle_orc | `oracle_orc/fa-ewmy-saasfaprod1.fa.ocs.oraclecloud.com/fa-ewmy-saasfaprod1/CX_1` | new_validated | row added; live validation returned requisitions | 2026-11-05T23:18:24.370878Z | complete | 162 | 162 | 162 | 0 |
| Vertex Pharmaceuticals | CHAIN | workday | `workday/vrtx/vertex_careers/wd501` | new_validated | row added; live validation returned requisitions | 2026-10-22T02:01:36Z | budget_limited | 281 | 200 | 199 | 1 |
| Visionist, Inc. | official_ats_chain | Jobvite |  | review | mixed_review |  | not_run |  |  |  |  |
| Vizient | excluded_by_preference |  |  | excluded | consultancy |  | not_run |  |  |  |  |
| Voto Consulting LLC | excluded_by_preference |  |  | excluded | staffing |  | not_run |  |  |  |  |
| Wiraa | excluded_by_preference |  |  | excluded | intermediary |  | not_run |  |  |  |  |
| WisdomAI | CHAIN | ashby | `ashby/Wisdom-AI` | new_validated | row added; live validation returned requisitions | 2026-10-22T01:59:29Z | complete |  | 19 | 19 | 0 |
| WTW | excluded_by_preference |  |  | excluded | consultancy |  | not_run |  |  |  |  |
| Xforia Global Talent  & Technology Solutions | excluded_by_preference |  |  | excluded | staffing |  | not_run |  |  |  |  |

## Owner priority: top employers per domain

Denominator: **182**. existing_covered: 76, existing_unhealthy_or_moved: 4, new_validated: 69, blocked: 18, unsupported: 5, pending: 10

| company | domain | evidence | provider | endpoint | outcome | why | validated at | status | source | parsed | req | non-req |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Synopsys | researched_boards | FRONTEND | radancy_talentbrew | `radancy_talentbrew/careers.synopsys.com/44408` | new_validated | row added; live validation returned requisitions | 2026-11-05T23:23:56Z | budget_limited |  | 15 | 15 | 0 |
| Charter Communications | researched_boards | FRONTEND | radancy_talentbrew | `radancy_talentbrew/jobs.spectrum.com/4673` | new_validated | row added; live validation returned requisitions | 2026-11-05T23:23:59Z | budget_limited |  | 15 | 15 | 0 |
| Optum | researched_boards | CHAIN | radancy_talentbrew | `radancy_talentbrew/careers.unitedhealthgroup.com/34088/optum` | new_validated | row added; live validation returned requisitions | 2026-11-05T23:24:05Z | budget_limited |  | 15 | 15 | 0 |
| Microsoft | software | EXISTING | eightfold | `eightfold/apply.careers.microsoft.com/microsoft` | existing_unhealthy_or_moved | validation blocked - Eightfold PCSX answered 403 to our validation (may be IP-specific; production inventory not visible here) | 2026-10-22T02:17:30Z | blocked |  | 0 | 0 | 0 |
| Google | software | EXISTING | google_careers | `google_careers/www.google.com/google` | existing_covered | row already in the crawl tables | 2026-10-22T02:17:32Z | budget_limited |  | 30 | 30 | 0 |
| Apple | software | EXISTING | custom_apple | `custom_apple/jobs.apple.com/apple` | existing_covered | row already in the crawl tables | 2026-10-22T02:17:34Z | budget_limited |  | 30 | 30 | 0 |
| Amazon | software | EXISTING | amazon | `amazon/www.amazon.jobs/amazon` | existing_covered | row already in the crawl tables | 2026-10-22T02:17:36Z | budget_limited |  | 30 | 30 | 0 |
| Meta | software | OWN | meta_careers |  | unsupported | unsupported_pending_provider:meta_careers - metacareers.com is a JS app needing a browser session (mnc_catalog record); left on the metered path. |  | not_run |  |  |  |  |
| Oracle | software | EXISTING | oracle_orc | `oracle_orc/eeho.fa.us2.oraclecloud.com/eeho/CX_1` | existing_covered | row already in the crawl tables | 2026-10-22T02:17:37Z | budget_limited | 2367 | 30 | 30 | 0 |
| Salesforce | software | EXISTING | workday | `workday/salesforce/External_Career_Site/wd12` | existing_covered | row already in the crawl tables | 2026-10-22T02:17:41Z | budget_limited |  | 30 | 30 | 0 |
| Adobe | software | EXISTING | workday | `workday/adobe/external_experienced/wd5` | existing_covered | row already in the crawl tables | 2026-10-22T02:17:45Z | budget_limited | 195 | 30 | 30 | 0 |
| IBM | software | EXISTING | custom_ibm | `custom_ibm/www-api.ibm.com/ibm` | existing_covered | row already in the crawl tables | 2026-10-22T02:17:46Z | budget_limited |  | 30 | 30 | 0 |
| Intuit | software | FRONTEND | radancy_talentbrew |  | unsupported | unsupported_pending_provider:radancy_talentbrew - jobs.intuit.com is TalentBrew (/search-jobs). |  | not_run |  |  |  |  |
| ServiceNow | software | EXISTING | smartrecruiters | `smartrecruiters/servicenow` | existing_covered | row already in the crawl tables | 2026-10-22T02:17:48Z | budget_limited | 430 | 30 | 30 | 0 |
| Workday | software | EXISTING | workday | `workday/workday/Workday/wd5` | existing_covered | row already in the crawl tables | 2026-10-22T02:17:49Z | budget_limited |  | 30 | 30 | 0 |
| Atlassian | software | EXISTING | custom_atlassian | `custom_atlassian/www.atlassian.com/atlassian` | existing_covered | row already in the crawl tables | 2026-10-22T02:17:58Z | budget_limited |  | 30 | 30 | 0 |
| Uber | software | OWN | uber_careers |  | unsupported | unsupported_pending_provider:uber_careers - MNC catalog engine `uber` has no termless provider (only Uber Freight, a separate company, is crawled). |  | not_run |  |  |  |  |
| Airbnb | software | EXISTING | greenhouse | `greenhouse/airbnb` | existing_covered | row already in the crawl tables | 2026-10-22T02:17:59Z | budget_limited | 193 | 30 | 30 | 0 |
| Stripe | software | EXISTING | greenhouse | `greenhouse/stripe` | existing_covered | row already in the crawl tables | 2026-10-22T02:18:00Z | budget_limited | 726 | 30 | 30 | 0 |
| Shopify | software | CHAIN | careers_page | `careers_page/shopify.com/shopify.com` | new_validated | row added; live validation returned requisitions | 2026-11-06T00:01:45.240401Z | complete |  | 25 | 25 | 0 |
| Cisco | software | EXISTING | workday | `workday/cisco/Cisco_Careers/wd5` | existing_covered | row already in the crawl tables | 2026-10-22T02:18:00Z | budget_limited | 1479 | 30 | 30 | 0 |
| Autodesk | software | EXISTING | workday | `workday/autodesk/Ext/wd1` | existing_covered | row already in the crawl tables | 2026-10-22T02:18:02Z | budget_limited | 32 | 30 | 30 | 0 |
| PayPal | software | EXISTING | workday | `workday/paypal/jobs/wd1` | existing_covered | row already in the crawl tables | 2026-10-22T02:18:04Z | budget_limited |  | 30 | 30 | 0 |
| Block | software | EXISTING | greenhouse | `greenhouse/block` | existing_covered | row already in the crawl tables | 2026-10-22T02:18:10Z | budget_limited | 206 | 30 | 30 | 0 |
| Netflix | software | EXISTING | eightfold | `eightfold/explore.jobs.netflix.net/netflix` | existing_covered | row already in the crawl tables | 2026-10-22T02:18:10Z | budget_limited |  | 30 | 30 | 0 |
| DoorDash | software | EXISTING | greenhouse | `greenhouse/doordashusa` | existing_covered | row already in the crawl tables | 2026-10-22T02:18:12Z | budget_limited | 457 | 30 | 30 | 0 |
| Bloomberg | software | BLOCKED |  |  | blocked | PerimeterX captcha (mnc_catalog record); not bypassed |  | not_run |  |  |  |  |
| Qualcomm | software | EXISTING | eightfold | `eightfold/careers.qualcomm.com/qualcomm` | existing_unhealthy_or_moved | validation blocked - Eightfold answered 403 to our validation (may be IP-specific) | 2026-10-22T02:18:13Z | blocked |  | 0 | 0 | 0 |
| OpenAI | ai_ml | EXISTING | ashby | `ashby/openai` | existing_covered | row already in the crawl tables | 2026-10-22T02:18:14Z | budget_limited |  | 30 | 30 | 0 |
| Anthropic | ai_ml | EXISTING | greenhouse | `greenhouse/anthropic` | existing_covered | row already in the crawl tables | 2026-10-22T02:18:17Z | budget_limited | 678 | 30 | 30 | 0 |
| Google DeepMind | ai_ml | PENDING |  |  | pending | official careers page is client-rendered; board link not in served HTML |  | not_run |  |  |  |  |
| NVIDIA | ai_ml | EXISTING | workday | `workday/nvidia/NVIDIAExternalCareerSite/wd5` | existing_covered | row already in the crawl tables | 2026-10-22T02:18:18Z | budget_limited |  | 30 | 30 | 0 |
| xAI | ai_ml | EXISTING | greenhouse | `greenhouse/xai` | existing_covered | row already in the crawl tables | 2026-10-22T02:18:22Z | budget_limited | 293 | 30 | 30 | 0 |
| Scale AI | ai_ml | EXISTING | greenhouse | `greenhouse/scaleai` | existing_covered | row already in the crawl tables | 2026-10-22T02:18:23Z | budget_limited | 174 | 30 | 30 | 0 |
| Cohere | ai_ml | EXISTING | ashby | `ashby/cohere` | existing_covered | row already in the crawl tables | 2026-10-22T02:18:20Z | budget_limited |  | 30 | 30 | 0 |
| Perplexity | ai_ml | EXISTING | ashby | `ashby/perplexity` | existing_covered | row already in the crawl tables | 2026-10-22T02:18:22Z | budget_limited |  | 30 | 30 | 0 |
| Character.AI | ai_ml | EXISTING | ashby | `ashby/character` | existing_covered | row already in the crawl tables | 2026-10-22T02:18:22Z | complete |  | 13 | 13 | 0 |
| Cursor | ai_ml | EXISTING | ashby | `ashby/cursor` | existing_covered | row already in the crawl tables | 2026-10-22T02:18:23Z | budget_limited |  | 30 | 30 | 0 |
| Hugging Face | ai_ml | EXISTING | workable | `workable/huggingface` | existing_covered | row already in the crawl tables | 2026-10-22T02:18:24Z | complete |  | 6 | 6 | 0 |
| Runway | ai_ml | CHAIN | ashby | `ashby/runway-ml` | new_validated | row added; live validation returned requisitions | 2026-10-22T02:14:37Z | complete |  | 48 | 48 | 0 |
| Glean | ai_ml | EXISTING | greenhouse | `greenhouse/gleanwork` | existing_covered | row already in the crawl tables | 2026-10-22T02:18:24Z | budget_limited | 120 | 30 | 30 | 0 |
| Harvey | ai_ml | EXISTING | ashby | `ashby/harvey` | existing_covered | row already in the crawl tables | 2026-10-22T02:18:25Z | budget_limited |  | 30 | 30 | 0 |
| Cerebras | ai_ml | EXISTING | ashby | `ashby/cerebras` | existing_covered | row already in the crawl tables | 2026-10-22T02:18:27Z | budget_limited |  | 30 | 30 | 0 |
| SambaNova | ai_ml | EXISTING | greenhouse | `greenhouse/sambanovasystems` | existing_covered | row already in the crawl tables | 2026-10-22T02:18:27Z | budget_limited | 61 | 30 | 30 | 0 |
| Groq | ai_ml | PENDING |  |  | pending | groq.com/careers redirects to the homepage; no board link served |  | not_run |  |  |  |  |
| Together AI | ai_ml | EXISTING | greenhouse | `greenhouse/togetherai` | existing_covered | row already in the crawl tables | 2026-10-22T02:18:27Z | budget_limited | 79 | 30 | 30 | 0 |
| Figure AI | ai_ml | PENDING |  |  | pending | official careers page served no ATS link |  | not_run |  |  |  |  |
| Waymo | ai_ml | EXISTING | greenhouse | `greenhouse/waymo` | existing_covered | row already in the crawl tables | 2026-10-22T02:18:28Z | budget_limited | 361 | 30 | 30 | 0 |
| Aurora Innovation | ai_ml | CHAIN | ashby | `ashby/aurora-operations-inc` | new_validated | row added; live validation returned requisitions | 2026-11-05T23:25:39.293769Z | complete |  | 90 | 90 | 0 |
| Physical Intelligence | ai_ml | EXISTING | ashby | `ashby/physicalintelligence` | existing_covered | row already in the crawl tables | 2026-10-22T02:18:28Z | budget_limited |  | 30 | 30 | 0 |
| Mistral AI | ai_ml | EXISTING | ashby | `ashby/mistral.ai` | existing_covered | row already in the crawl tables | 2026-10-22T05:09:40Z | complete |  | 222 | 222 | 0 |
| ElevenLabs | ai_ml | EXISTING | ashby | `ashby/elevenlabs` | existing_covered | row already in the crawl tables | 2026-10-22T02:18:29Z | budget_limited |  | 30 | 30 | 0 |
| Abridge | ai_ml | EXISTING | ashby | `ashby/abridge` | existing_covered | row already in the crawl tables | 2026-10-22T02:18:29Z | budget_limited |  | 30 | 30 | 0 |
| Databricks | data_science | EXISTING | greenhouse | `greenhouse/databricks` | existing_covered | row already in the crawl tables | 2026-10-22T02:18:30Z | budget_limited | 897 | 30 | 30 | 0 |
| Snowflake | data_science | FRONTEND | phenom | `phenom/careers.snowflake.com/snowflake` | new_validated | row added; live validation returned requisitions | 2026-10-22T02:14:41Z | budget_limited | 361 | 100 | 100 | 0 |
| Palantir | data_science | EXISTING | lever | `lever/palantir` | existing_covered | row already in the crawl tables | 2026-10-22T02:18:30Z | budget_limited |  | 30 | 30 | 0 |
| Confluent | data_science | EXISTING | ashby | `ashby/confluent` | existing_unhealthy_or_moved | validation blocked - ashby/confluent answers 200 with zero jobs; careers.confluent.io answered 429 | 2026-10-22T02:18:31Z | blocked |  | 0 | 0 | 0 |
| MongoDB | data_science | EXISTING | greenhouse | `greenhouse/mongodb` | existing_covered | row already in the crawl tables | 2026-10-22T02:18:32Z | budget_limited | 390 | 30 | 30 | 0 |
| Datadog | data_science | EXISTING | greenhouse | `greenhouse/datadog` | existing_covered | row already in the crawl tables | 2026-10-22T02:18:32Z | budget_limited | 440 | 30 | 30 | 0 |
| Elastic | data_science | EXISTING | greenhouse | `greenhouse/elastic` | existing_covered | row already in the crawl tables | 2026-10-22T02:18:32Z | budget_limited | 379 | 30 | 30 | 0 |
| Fivetran | data_science | EXISTING | greenhouse | `greenhouse/fivetran` | existing_covered | row already in the crawl tables | 2026-10-22T02:18:33Z | budget_limited | 166 | 30 | 29 | 1 |
| dbt Labs | data_science | EXISTING | greenhouse | `greenhouse/dbtlabsinc` | existing_unhealthy_or_moved | validation failed - greenhouse/dbtlabsinc answers 404; getdbt.com careers page shows Jibe/RMK hints, board not established | 2026-10-22T02:18:33Z | failed |  | 0 | 0 | 0 |
| Teradata | data_science | PENDING |  |  | pending | official careers page served no ATS link |  | not_run |  |  |  |  |
| Cloudera | data_science | EXISTING | workday | `workday/cloudera/External_Career/wd5` | existing_covered | row already in the crawl tables | 2026-10-22T02:18:33Z | budget_limited | 20 | 30 | 20 | 0 |
| Informatica | data_science | EXISTING | workday | `workday/salesforce/External_Career_Site/wd12` | existing_covered | row already in the crawl tables |  | not_run |  |  |  |  |
| Alteryx | data_science | CHAIN | workday | `workday/alteryx/AlteryxCareers/wd108` | new_validated | row added; live validation returned requisitions | 2026-10-22T02:14:38Z | complete | 50 | 50 | 50 | 0 |
| Domo | data_science | BLOCKED |  |  | blocked | domo.com/company/careers answered 405 on 2026-10-21 |  | not_run |  |  |  |  |
| ThoughtSpot | data_science | CHAIN | rippling | `rippling/thoughtspot` | new_validated | row added; live validation returned requisitions | 2026-11-06T00:14:28.510501Z | complete |  | 49 | 48 | 0 |
| Sigma Computing | data_science | EXISTING | greenhouse | `greenhouse/sigmacomputing` | existing_covered | row already in the crawl tables | 2026-10-22T02:18:35Z | budget_limited | 77 | 30 | 30 | 0 |
| Hex | data_science | CHAIN | ashby | `ashby/hex` | new_validated | row added; live validation returned requisitions | 2026-11-05T23:25:40.344476Z | complete |  | 39 | 39 | 0 |
| Starburst | data_science | EXISTING | greenhouse | `greenhouse/starburst` | existing_covered | row already in the crawl tables | 2026-10-22T02:18:35Z | budget_limited | 33 | 30 | 30 | 0 |
| ClickHouse | data_science | CHAIN | ashby | `ashby/clickhouse` | new_validated | row added; live validation returned requisitions | 2026-11-05T23:25:41.396673Z | complete |  | 202 | 202 | 0 |
| Amplitude | data_science | CHAIN | ashby | `ashby/amplitude` | new_validated | row added; live validation returned requisitions | 2026-10-22T05:09:44Z | complete |  | 41 | 41 | 0 |
| Two Sigma | data_science | CHAIN | avature | `avature/careers.twosigma.com/careers.twosigma.com/careers` | new_validated | row added; live validation returned requisitions | 2026-11-05T23:35:16.247200Z | complete |  | 66 | 66 | 0 |
| Capital One | data_science | EXISTING | workday | `workday/capitalone/Capital_One/wd12` | existing_covered | row already in the crawl tables | 2026-10-22T02:18:36Z | budget_limited | 1998 | 30 | 30 | 0 |
| Nielsen | data_science | PENDING |  |  | pending | careers URL answered 404; NielsenIQ (crawled) is a different company |  | not_run |  |  |  |  |
| Experian | data_science | EXISTING | smartrecruiters | `smartrecruiters/experian` | existing_covered | row already in the crawl tables | 2026-10-22T02:18:38Z | complete | 26 | 26 | 26 | 0 |
| Splunk | data_science | EXISTING | workday | `workday/cisco/Cisco_Careers/wd5` | existing_covered | row already in the crawl tables |  | not_run |  |  |  |  |
| Cloudflare | cloud | EXISTING | greenhouse | `greenhouse/cloudflare` | existing_covered | row already in the crawl tables | 2026-10-22T02:18:38Z | budget_limited | 443 | 30 | 30 | 0 |
| DigitalOcean | cloud | CHAIN | greenhouse | `greenhouse/digitalocean98` | new_validated | row added; live validation returned requisitions | 2026-11-05T23:41:50.775144Z | complete | 152 | 152 | 152 | 0 |
| CoreWeave | cloud | EXISTING | greenhouse | `greenhouse/coreweave` | existing_covered | row already in the crawl tables | 2026-10-22T02:18:39Z | budget_limited | 321 | 30 | 30 | 0 |
| Akamai | cloud | BLOCKED |  |  | blocked | akamai.com/careers answered 403 on 2026-10-21 |  | not_run |  |  |  |  |
| Fastly | cloud | EXISTING | greenhouse | `greenhouse/fastly` | existing_covered | row already in the crawl tables | 2026-10-22T02:18:39Z | budget_limited | 53 | 30 | 30 | 0 |
| HashiCorp | cloud | EXISTING | custom_ibm | `custom_ibm/www-api.ibm.com/ibm` | existing_covered | row already in the crawl tables |  | not_run |  |  |  |  |
| Broadcom | cloud | EXISTING | workday | `workday/broadcom/External_Career/wd1` | existing_covered | row already in the crawl tables | 2026-10-22T02:18:39Z | budget_limited | 372 | 30 | 30 | 0 |
| Red Hat | cloud | EXISTING | workday | `workday/redhat/jobs/wd5` | existing_covered | row already in the crawl tables | 2026-10-22T02:18:41Z | budget_limited |  | 30 | 30 | 0 |
| Nutanix | cloud | BLOCKED |  |  | blocked | careers.nutanix.com answered 403 on 2026-10-21 |  | not_run |  |  |  |  |
| NetApp | cloud | CHAIN | eightfold | `eightfold/netapp.eightfold.ai/netapp` | blocked | plan says added but no crawl member serves it - careers.netapp.com (TalentBrew front end) links netapp.eightfold.ai. | 2026-10-22T02:14:45Z | complete |  | 136 | 136 | 0 |
| Equinix | cloud | EXISTING | workday | `workday/equinix/External/wd1` | existing_covered | row already in the crawl tables | 2026-10-22T02:18:44Z | budget_limited |  | 30 | 30 | 0 |
| Digital Realty | cloud | BLOCKED |  |  | blocked | TLS error fetching digitalrealty.com/about/careers on 2026-10-21 |  | not_run |  |  |  |  |
| Rackspace | cloud | EXISTING | workday | `workday/rackspace/External/wd1` | existing_covered | row already in the crawl tables | 2026-10-22T02:18:48Z | budget_limited | 35 | 30 | 30 | 0 |
| Lambda | cloud | EXISTING | ashby | `ashby/lambda` | existing_covered | row already in the crawl tables | 2026-10-22T02:18:50Z | budget_limited |  | 30 | 30 | 0 |
| Crusoe | cloud | EXISTING | ashby | `ashby/crusoe` | existing_covered | row already in the crawl tables | 2026-10-22T02:18:50Z | budget_limited |  | 30 | 30 | 0 |
| Vercel | cloud | EXISTING | greenhouse | `greenhouse/vercel` | existing_covered | row already in the crawl tables | 2026-10-22T02:18:54Z | budget_limited | 95 | 30 | 30 | 0 |
| Okta | cloud | EXISTING | greenhouse | `greenhouse/okta` | existing_covered | row already in the crawl tables | 2026-10-22T02:18:55Z | budget_limited | 398 | 30 | 30 | 0 |
| Zscaler | cloud | EXISTING | greenhouse | `greenhouse/zscaler` | existing_covered | row already in the crawl tables | 2026-10-22T02:18:56Z | budget_limited | 359 | 30 | 30 | 0 |
| Palo Alto Networks | cloud | EXISTING | workday | `workday/paloaltonetworks/panwexternalcareers/wd5` | existing_covered | row already in the crawl tables | 2026-10-22T02:18:56Z | budget_limited | 1425 | 30 | 30 | 0 |
| Dell Technologies | cloud | EXISTING | oracle_orc | `oracle_orc/enterpriseplatform.dell.com/dell/CX_1001` | existing_covered | row already in the crawl tables | 2026-10-22T02:18:58Z | budget_limited | 493 | 30 | 30 | 0 |
| Hewlett Packard Enterprise | cloud | EXISTING | workday | `workday/hpe/Jobsathpe/wd5` | existing_covered | row already in the crawl tables | 2026-10-22T02:19:03Z | budget_limited |  | 30 | 30 | 0 |
| Arista Networks | cloud | BLOCKED |  |  | blocked | arista.com/en/careers answered 406 on 2026-10-21 |  | not_run |  |  |  |  |
| Pure Storage | cloud | EXISTING | greenhouse | `greenhouse/purestorage` | existing_covered | row already in the crawl tables | 2026-10-22T02:19:09Z | budget_limited | 369 | 30 | 30 | 0 |
| Twilio | cloud | EXISTING | greenhouse | `greenhouse/twilio` | existing_covered | row already in the crawl tables | 2026-10-22T02:19:09Z | budget_limited | 131 | 30 | 30 | 0 |
| Supermicro | cloud | CHAIN | successfactors | `successfactors/jobs.supermicro.com/jobs.supermicro.com` | new_validated | row added; live validation returned requisitions | 2026-11-05T23:32:54.359419Z | complete |  | 1000 | 1000 | 0 |
| Duke Energy | electrical_power | EXISTING | workday | `workday/dukeenergy/Search/wd1` | existing_covered | row already in the crawl tables | 2026-10-22T02:19:10Z | budget_limited |  | 30 | 30 | 0 |
| Southern Company | electrical_power | PENDING |  |  | pending | only a DirectEmployers .jobs site (southerncompany.jobs) observed; employer ATS not established |  | not_run |  |  |  |  |
| NextEra Energy | electrical_power | CHAIN | successfactors | `successfactors/jobs.nexteraenergy.com/nexteraenergy` | new_validated | row added; live validation returned requisitions | 2026-10-22T02:15:20Z | budget_limited |  | 200 | 200 | 0 |
| Dominion Energy | electrical_power | FRONTEND | successfactors | `successfactors/careers.dominionenergy.com/dominionenergy` | new_validated | row added; live validation returned requisitions | 2026-10-22T02:14:56Z | complete |  | 145 | 145 | 0 |
| American Electric Power | electrical_power | EXISTING | workday | `workday/aep/AEPCareerSite/wd1` | existing_covered | row already in the crawl tables | 2026-10-22T02:19:14Z | budget_limited | 287 | 30 | 30 | 0 |
| Xcel Energy | electrical_power | EXISTING | workday | `workday/xcelenergy/External/wd1` | existing_covered | row already in the crawl tables | 2026-10-22T02:19:16Z | budget_limited |  | 30 | 30 | 0 |
| Exelon | electrical_power | FRONTEND | radancy | `radancy/jobs.exeloncorp.com/exeloncorp` | new_validated | row added; live validation returned requisitions | 2026-10-22T02:15:51Z | complete | 124 | 124 | 124 | 0 |
| PG&E | electrical_power | FRONTEND | radancy_talentbrew | `radancy_talentbrew/jobs.pge.com/29673` | new_validated | row added; live validation returned requisitions | 2026-11-05T23:24:06Z | budget_limited |  | 15 | 15 | 0 |
| Southern California Edison | electrical_power | FRONTEND | symphony_cws |  | unsupported | unsupported_pending_provider:symphony_cws - edisoncareers.com is a Symphony/CWS front end (jobsapi-google.m-cloud.io) over SAP RCM C0003043182P. |  | not_run |  |  |  |  |
| Entergy | electrical_power | CHAIN | successfactors | `successfactors/jobs.entergy.com/entergy` | new_validated | row added; live validation returned requisitions | 2026-10-22T02:15:02Z | budget_limited |  | 200 | 200 | 0 |
| Con Edison | electrical_power | CHAIN | oracle_orc | `oracle_orc/ejcu.fa.us6.oraclecloud.com/ejcu/CX_1033` | new_validated | row added; live validation returned requisitions | 2026-10-22T02:15:49Z | complete | 49 | 49 | 49 | 0 |
| National Grid | electrical_power | BLOCKED |  |  | blocked | careers.nationalgrid.com answered 403 on 2026-10-21 |  | not_run |  |  |  |  |
| Eversource | electrical_power | EXISTING | workday | `workday/eversource/ExternalSite/wd1` | existing_covered | row already in the crawl tables | 2026-10-22T02:19:20Z | budget_limited |  | 30 | 30 | 0 |
| PSEG | electrical_power | FRONTEND | successfactors | `successfactors/jobs.pseg.com/pseg` | new_validated | row added; live validation returned requisitions | 2026-10-22T02:15:31Z | complete |  | 32 | 32 | 0 |
| DTE Energy | electrical_power | FRONTEND | successfactors | `successfactors/careers.dteenergy.com/dteenergy` | new_validated | row added; live validation returned requisitions | 2026-10-22T02:15:12Z | complete |  | 83 | 83 | 0 |
| Consumers Energy | electrical_power | CHAIN | successfactors | `successfactors/careers.consumersenergy.com/consumersenergy` | new_validated | row added; live validation returned requisitions | 2026-10-22T02:15:16Z | complete |  | 52 | 52 | 0 |
| GE Vernova | electrical_power | EXISTING | workday | `workday/gevernova/Vernova_ExternalSite/wd5` | existing_covered | row already in the crawl tables | 2026-10-22T02:19:22Z | budget_limited | 2280 | 30 | 30 | 0 |
| Siemens Energy | electrical_power | FRONTEND | avature | `avature/jobs.siemens-energy.com/jobs.siemens-energy.com/en_US/jobs` | new_validated | row added; live validation returned requisitions | 2026-11-05T22:52:55.127411Z | complete |  | 1905 | 1905 | 0 |
| Hitachi Energy | electrical_power | FRONTEND |  |  | unsupported | 2026-11-05: official careers/open-jobs is an XHR shell; shared Hitachi Workday link is published but no public entity-filter identity has been proven. No unfiltered shared board added. - 2026-11-05: official careers/open |  | not_run |  |  |  |  |
| Schneider Electric | electrical_power | BLOCKED |  |  | blocked | careers.se.com answered 403 on 2026-10-21 |  | not_run |  |  |  |  |
| ABB | electrical_power | FRONTEND | phenom | `phenom/careers.abb/abb` | new_validated | row added; live validation returned requisitions | 2026-10-22T02:15:53Z | budget_limited | 2067 | 100 | 100 | 0 |
| Eaton | electrical_power | CHAIN | eightfold | `eightfold/eaton.eightfold.ai/eaton` | blocked | not added; validation blocked - 2026-11-05: jobs.eaton.com redirects to eaton.eightfold.ai/careers; robots disallows /careers, so no listing API request sent. Earlier 403 is not reinterpreted as empty. | 2026-10-22T02:15:53Z | blocked |  | 0 | 0 | 0 |
| Hubbell | electrical_power | FRONTEND | successfactors | `successfactors/careers.hubbell.com/hubbell` | new_validated | row added; live validation returned requisitions | 2026-10-22T02:15:33Z | budget_limited |  | 200 | 200 | 0 |
| Quanta Services | electrical_power | CHAIN | icims | `icims/allcareers-quanta.icims.com/allcareers-quanta` | new_validated | row added; live validation returned requisitions | 2026-10-22T02:15:56Z | complete |  | 19 | 18 | 1 |
| Burns & McDonnell | electrical_power | PENDING |  |  | pending | official link is burnsmcd.jobs (DirectEmployers); underlying ATS not established |  | not_run |  |  |  |  |
| Black & Veatch | electrical_power | FRONTEND | successfactors | `successfactors/careers.bv.com/bv` | new_validated | row added; live validation returned requisitions | 2026-10-22T02:15:41Z | budget_limited |  | 200 | 200 | 0 |
| Sargent & Lundy | electrical_power | CHAIN | icims | `icims/careers-sargentlundy.icims.com/careers-sargentlundy` | new_validated | row added; live validation returned requisitions | 2026-10-22T02:15:59Z | budget_limited |  | 200 | 200 | 0 |
| POWER Engineers | electrical_power | BLOCKED |  |  | blocked | powereng.com/careers redirects to wsp.com (now WSP), which answered 403 on 2026-10-21 |  | not_run |  |  |  |  |
| Texas Instruments | electrical_power | EXISTING | oracle_orc | `oracle_orc/edbz.fa.us2.oraclecloud.com/edbz/CX_1` | existing_covered | row already in the crawl tables | 2026-10-22T02:19:24Z | budget_limited | 874 | 30 | 30 | 0 |
| Analog Devices | electrical_power | EXISTING | workday | `workday/analogdevices/External/wd1` | existing_covered | row already in the crawl tables | 2026-10-22T02:19:27Z | budget_limited | 318 | 30 | 30 | 0 |
| FirstEnergy | electrical_power | CHAIN | oracle_orc | `oracle_orc/fa-etjd-saasfaprod1.fa.ocs.oraclecloud.com/fa-etjd-saasfaprod1/CX_1` | new_validated | row added; live validation returned requisitions | 2026-10-22T05:19:38Z | budget_limited | 334 | 200 | 200 | 0 |
| Evergy | electrical_power | CHAIN | taleo | `taleo/evergy.taleo.net/evergy` | blocked | not added; validation blocked - 2026-11-05: official careers page links /careersection/evergy_external_career_section/jobsearch.ftl?lang=en. Current Taleo reader returned 0 from an HTML shell with no resolved portal; zer | 2026-11-05T22:45:29.976010Z | blocked |  | 0 | 0 |  |
| PacifiCorp | electrical_power | CHAIN | successfactors | `successfactors/careers.pacificorp.com/pacificorp` | new_validated | row added; live validation returned requisitions | 2026-10-22T05:19:42Z | complete |  | 87 | 87 | 0 |
| PPL | electrical_power | CHAIN | radancy | `radancy/careers.pplweb.com/pplweb` | new_validated | row added; live validation returned requisitions | 2026-10-22T05:28:35Z | complete | 168 | 168 | 168 | 0 |
| WEC Energy Group | electrical_power | CHAIN | successfactors | `successfactors/careers.wecenergygroup.com/wecenergygroup` | new_validated | row added; live validation returned requisitions | 2026-10-22T05:19:48Z | complete |  | 74 | 74 | 0 |
| NiSource | electrical_power | CHAIN | workday | `workday/nisource/NiSource/wd1` | new_validated | row added; live validation returned requisitions | 2026-10-22T05:19:52Z | complete | 37 | 37 | 37 | 0 |
| Arizona Public Service | electrical_power | CHAIN | successfactors | `successfactors/careers.aps.com/aps` | new_validated | row added; live validation returned requisitions | 2026-10-22T05:19:53Z | complete |  | 10 | 10 | 0 |
| CenterPoint Energy | electrical_power | CHAIN | successfactors | `successfactors/careers.centerpointenergy.com/centerpointenergy` | new_validated | row added; live validation returned requisitions | 2026-10-22T05:19:55Z | complete |  | 28 | 28 | 0 |
| Puget Sound Energy | electrical_power | CHAIN | successfactors | `successfactors/jobs.pse.com/pse` | new_validated | row added; live validation returned requisitions | 2026-10-22T05:19:59Z | complete |  | 49 | 49 | 0 |
| Constellation Energy | electrical_power | CHAIN | radancy | `radancy/jobs.constellationenergy.com/constellationenergy` | new_validated | row added; live validation returned requisitions | 2026-10-22T05:20:01Z | budget_limited | 343 | 200 | 200 | 0 |
| Schweitzer Engineering Laboratories | electrical_power | CHAIN | workday | `workday/selinc/SEL/wd1` | new_validated | row added; live validation returned requisitions | 2026-10-22T05:20:02Z | budget_limited |  | 200 | 200 | 0 |
| S&C Electric | electrical_power | CHAIN | oracle_orc | `oracle_orc/ejia.fa.us6.oraclecloud.com/ejia/CX_1001` | new_validated | row added; live validation returned requisitions | 2026-10-22T05:20:14Z | budget_limited | 203 | 200 | 200 | 0 |
| Ulteig | electrical_power | CHAIN | ukg | `ukg/recruiting2.ultipro.com/ULT1008ULTE/71bc1da6-c7b0-4b37-9f7b-076d7d68c57b` | new_validated | row added; live validation returned requisitions | 2026-10-22T05:20:17Z | complete | 109 | 109 | 109 | 0 |
| HDR | electrical_power | CHAIN | taleo | `taleo/hdr.taleo.net/hdr` | new_validated | row added; live validation returned requisitions | 2026-10-22T05:20:19Z | budget_limited |  | 200 | 200 | 0 |
| Bechtel | electrical_power | CHAIN | phenom | `phenom/jobs.bechtel.com/bechtel` | new_validated | row added; live validation returned requisitions | 2026-10-22T05:20:28Z | budget_limited | 1150 | 100 | 100 | 0 |
| Emerson | electrical_power | CHAIN | oracle_orc | `oracle_orc/hdjq.fa.us2.oraclecloud.com/hdjq/CX_1` | new_validated | row added; live validation returned requisitions | 2026-10-22T05:20:33Z | budget_limited | 1169 | 200 | 200 | 0 |
| Vertiv | electrical_power | CHAIN | oracle_orc | `oracle_orc/egup.fa.us2.oraclecloud.com/egup/CX` | new_validated | row added; live validation returned requisitions | 2026-10-22T05:20:35Z | budget_limited | 2385 | 200 | 199 | 1 |
| First Solar | electrical_power | CHAIN | oracle_orc | `oracle_orc/fa-esbv-saasfaprod1.fa.ocs.oraclecloud.com/fa-esbv-saasfaprod1/CX_1` | new_validated | row added; live validation returned requisitions | 2026-10-22T05:20:39Z | budget_limited | 260 | 200 | 200 | 0 |
| Landis+Gyr | electrical_power | CHAIN | successfactors | `successfactors/careers.landisgyr.com/landisgyr` | new_validated | row added; live validation returned requisitions | 2026-10-22T05:20:41Z | complete |  | 60 | 60 | 0 |
| Southwire | electrical_power | CHAIN | successfactors | `successfactors/careers.southwire.com/southwire` | new_validated | row added; live validation returned requisitions | 2026-10-22T05:20:44Z | budget_limited |  | 200 | 200 | 0 |
| Westinghouse Electric | electrical_power | CHAIN | successfactors | `successfactors/careers.westinghousenuclear.com/westinghousenuclear` | new_validated | row added; live validation returned requisitions | 2026-10-22T05:21:01Z | budget_limited |  | 200 | 200 | 0 |
| Sempra | electrical_power | BLOCKED |  |  | blocked | sempra.com/careers answered 403 on 2026-10-21 |  | not_run |  |  |  |  |
| Tennessee Valley Authority | electrical_power | BLOCKED |  |  | blocked | tva.com/careers answered 403 on 2026-10-21 |  | not_run |  |  |  |  |
| Littelfuse | electrical_power | BLOCKED |  |  | blocked | littelfuse.com/careers answered 403 on 2026-10-21 |  | not_run |  |  |  |  |
| Avangrid | electrical_power | BLOCKED |  |  | blocked | avangrid.com/careers timed out on 2026-10-21 |  | not_run |  |  |  |  |
| Oncor | electrical_power | BLOCKED |  |  | blocked | oncor.com careers page timed out on 2026-10-21 |  | not_run |  |  |  |  |
| Siemens | electrical_power | FRONTEND | avature | `avature/jobs.siemens.com/jobs.siemens.com/en_US/externaljobs` | new_validated | row added; live validation returned requisitions | 2026-11-05T22:50:21.493331Z | budget_limited |  | 516 | 516 | 0 |
| Kiewit | electrical_power | CHAIN | successfactors | `successfactors/kiewitcareers.kiewit.com/kiewitcareers.kiewit.com` | new_validated | row added; live validation returned requisitions | 2026-11-05T23:33:36.779911Z | complete |  | 995 | 995 | 0 |
| Stantec | electrical_power | CHAIN | directemployers_nlx | `directemployers_nlx/stantec.jobs/stantec/usa` | new_validated | row added; live validation returned requisitions | 2026-11-06T00:19:00Z | budget_limited |  | 400 | 400 | 0 |
| Mott MacDonald | electrical_power | CHAIN | custom_mottmac | `custom_mottmac/www.mottmac.com/mottmac` | new_validated | row added; live validation returned requisitions | 2026-11-06T00:15:41.459006Z | complete |  | 1070 | 1063 | 7 |
| Jacobs | electrical_power | PENDING |  |  | pending | jobs.jacobs.com does not resolve; current board not observed |  | not_run |  |  |  |  |
| NRG Energy | electrical_power | CHAIN | successfactors | `successfactors/careers.nrgenergy.com/careers.nrgenergy.com` | new_validated | row added; live validation returned requisitions | 2026-11-05T23:35:07.582871Z | complete |  | 75 | 75 | 0 |
| Enphase Energy | electrical_power | CHAIN | custom_enphase | `custom_enphase/enphase` | new_validated | row added; live validation returned requisitions | 2026-11-05T23:39:01.305764Z | complete |  | 33 | 33 | 0 |
| Mitsubishi Electric Power Products | electrical_power | PENDING |  |  | pending | Greenhouse referenced but no board token in served HTML |  | not_run |  |  |  |  |
| Wabtec | electrical_power | PENDING |  |  | pending | careers.wabtec.com is a custom front end over SmartRecruiters; company id not observed |  | not_run |  |  |  |  |
| Fluor | electrical_power | BLOCKED |  |  | blocked | careers.fluor.com is disallowed to our agent by robots.txt |  | not_run |  |  |  |  |
| TSMC | november_2026_research | CHAIN | successfactors | `successfactors/ro.careers.tsmc.com/ro.careers.tsmc.com` | new_validated | row added; live validation returned requisitions | 2026-11-05T23:34:27.972547Z | complete |  | 339 | 339 | 0 |
| Teradyne | november_2026_research | CHAIN | successfactors | `successfactors/jobs.teradyne.com/jobs.teradyne.com` | new_validated | row added; live validation returned requisitions | 2026-11-05T23:34:42.979461Z | complete |  | 570 | 570 | 0 |
| Boston Consulting Group | november_2026_research | CHAIN | phenom | `phenom/careers.bcg.com/bcg` | new_validated | row added; live validation returned requisitions | 2026-11-05T23:19:30.554525Z | budget_limited | 939 | 100 | 100 | 0 |
| Riot Games | november_2026_research | CHAIN | greenhouse | `greenhouse/riotgames` | new_validated | row added; live validation returned requisitions | 2026-11-05T23:19:36.160020Z | complete | 187 | 187 | 187 | 0 |
| Pydantic | november_2026_research | CHAIN | careers_page | `careers_page/pydantic.dev/pydantic.dev` | new_validated | row added; live validation returned requisitions | 2026-11-05T23:19:36.361280Z | complete |  | 12 | 12 | 0 |
| Procore | november_2026_research | CHAIN | careers_page | `careers_page/careers.procore.com/careers.procore.com` | new_validated | row added; live validation returned requisitions | 2026-11-05T23:19:38.795445Z | complete |  | 110 | 110 | 0 |
| Etsy | november_2026_research | CHAIN | careers_page | `careers_page/careers.etsy.com/careers.etsy.com` | new_validated | row added; live validation returned requisitions | 2026-11-05T23:19:44.285393Z | budget_limited |  | 30 | 30 | 0 |
| EY | november_2026_research | CHAIN | successfactors | `successfactors/careers.ey.com/careers.ey.com` | new_validated | row added; live validation returned requisitions | 2026-11-06T00:06:33.887876Z | budget_limited |  | 1974 | 1974 | 0 |
| Electronic Arts | november_2026_research | CHAIN | avature | `avature/jobs.ea.com/jobs.ea.com/en_US/careers` | new_validated | row added; live validation returned requisitions | 2026-11-05T23:35:24.781862Z | complete |  | 342 | 342 | 0 |
| Deloitte | november_2026_research | CHAIN | avature | `avature/apply.deloitte.com/apply.deloitte.com/en_US/careers?3_5_3=477%2C478%2C480&sort=relevancy` | new_validated | row added; live validation returned requisitions | 2026-11-05T23:35:54.890520Z | complete |  | 19 | 19 | 0 |
| Oxide Computer | november_2026_research | CHAIN | careers_page | `careers_page/oxide.computer/oxide.computer` | new_validated | row added; live validation returned requisitions | 2026-11-05T23:39:06.722362Z | complete |  | 15 | 15 | 0 |
| KPMG US | november_2026_research | CHAIN | careers_page | `careers_page/kpmguscareers.com/kpmguscareers.com` | new_validated | row added; live validation returned requisitions | 2026-11-06T00:00:06.338893Z | budget_limited |  | 692 | 692 | 0 |
| Deel | november_2026_research | CHAIN | careers_page | `careers_page/deel.com/deel.com` | new_validated | row added; live validation returned requisitions | 2026-11-06T00:01:43.932298Z | complete |  | 251 | 251 | 0 |

## Existing rows health-checked (no duplicates added)

Denominator: **4**. existing_covered: 3, existing_unhealthy_or_moved: 1

| company | evidence | provider | endpoint | outcome | why | validated at | status | source | parsed | req | non-req |
|---|---|---|---|---|---|---|---|---|---|---|---|
| Duolingo |  | greenhouse | `greenhouse/duolingo` | existing_covered | row already in the crawl tables | 2026-10-22T01:59:41Z | complete | 61 | 61 | 61 | 0 |
| Nordstrom |  | workday | `workday/nordstrom/nordstrom_careers/wd501` | existing_covered | row already in the crawl tables | 2026-10-22T01:59:41Z | budget_limited | 1519 | 400 | 400 | 0 |
| Garmin |  |  |  | existing_unhealthy_or_moved | Only an official_career_registry row (careers.garmin.com); no crawlable catalog row in the repo. careers.garmin.com is an iCIMS Jibe front end whose public /api/jobs reported 434 jobs on 2026-10-21 (readable by `radancy` |  | not_run |  |  |  |  |
| BAE Systems (Taleo seed) |  | taleo | `taleo/baesystems.taleo.net/baesystems` | existing_covered | row already in the crawl tables | 2026-10-22T02:06:57Z | complete |  | 146 | 146 | 0 |

## Next batch (work these regularly)

Top-employer rows that are pending, blocked, or whose existing board no longer answers. For each: record the official career page and the exact ATS endpoint in `scripts/data/direct_employer_plan.py` (`plan="candidate"`), run `--validate --only <name>`, add the catalog/seed row only when the run returns requisitions, flip `plan` to "added", re-run `--write-docs`. Unsupported rows wait for the provider named.

- **Microsoft** (software, existing_unhealthy_or_moved): validation blocked
- **Bloomberg** (software, blocked): PerimeterX captcha (mnc_catalog record); not bypassed
- **Qualcomm** (software, existing_unhealthy_or_moved): validation blocked
- **Google DeepMind** (ai_ml, pending): official careers page is client-rendered; board link not in served HTML
- **Groq** (ai_ml, pending): groq.com/careers redirects to the homepage; no board link served
- **Figure AI** (ai_ml, pending): official careers page served no ATS link
- **Confluent** (data_science, existing_unhealthy_or_moved): validation blocked
- **dbt Labs** (data_science, existing_unhealthy_or_moved): validation failed
- **Teradata** (data_science, pending): official careers page served no ATS link
- **Domo** (data_science, blocked): domo.com/company/careers answered 405 on 2026-10-21
- **Nielsen** (data_science, pending): careers URL answered 404; NielsenIQ (crawled) is a different company
- **Akamai** (cloud, blocked): akamai.com/careers answered 403 on 2026-10-21
- **Nutanix** (cloud, blocked): careers.nutanix.com answered 403 on 2026-10-21
- **NetApp** (cloud, blocked): plan says added but no crawl member serves it
- **Digital Realty** (cloud, blocked): TLS error fetching digitalrealty.com/about/careers on 2026-10-21
- **Arista Networks** (cloud, blocked): arista.com/en/careers answered 406 on 2026-10-21
- **Southern Company** (electrical_power, pending): only a DirectEmployers .jobs site (southerncompany.jobs) observed; employer ATS not established
- **National Grid** (electrical_power, blocked): careers.nationalgrid.com answered 403 on 2026-10-21
- **Schneider Electric** (electrical_power, blocked): careers.se.com answered 403 on 2026-10-21
- **Eaton** (electrical_power, blocked): not added; validation blocked
- **Burns & McDonnell** (electrical_power, pending): official link is burnsmcd.jobs (DirectEmployers); underlying ATS not established
- **POWER Engineers** (electrical_power, blocked): powereng.com/careers redirects to wsp.com (now WSP), which answered 403 on 2026-10-21
- **Evergy** (electrical_power, blocked): not added; validation blocked
- **Sempra** (electrical_power, blocked): sempra.com/careers answered 403 on 2026-10-21
- **Tennessee Valley Authority** (electrical_power, blocked): tva.com/careers answered 403 on 2026-10-21
- **Littelfuse** (electrical_power, blocked): littelfuse.com/careers answered 403 on 2026-10-21
- **Avangrid** (electrical_power, blocked): avangrid.com/careers timed out on 2026-10-21
- **Oncor** (electrical_power, blocked): oncor.com careers page timed out on 2026-10-21
- **Jacobs** (electrical_power, pending): jobs.jacobs.com does not resolve; current board not observed
- **Mitsubishi Electric Power Products** (electrical_power, pending): Greenhouse referenced but no board token in served HTML
- **Wabtec** (electrical_power, pending): careers.wabtec.com is a custom front end over SmartRecruiters; company id not observed
- **Fluor** (electrical_power, blocked): careers.fluor.com is disallowed to our agent by robots.txt

## How an added row reaches the Oracle ingester

The Oracle ingest runner reads the union of the in-repository catalogs, seed
endpoints and owner-verified dynamic sources. A new catalog/seed member can be
scheduled after deployment. Canonical employer attribution requires the separate
ownership records in `career_sources`; adding a token alone does not prove that
ownership. The owner's onboarding `--apply` writes that ownership half only for
rows whose exact endpoint has recorded successful validation.

Validation timestamps above are the original public observations. An offline
replay or documentation regeneration does not make an older observation fresh.
Counts describe public responses and are not production inventory measurements.
The header research-reviewed date identifies the original lead-registry audit,
not the date of later public observations.
The combined report retains the newest recorded validation matching each current
exact endpoint, including its filters, route and host.

## Owner steps (in order)

1. Review the combined employer-coverage PR and require the existing Linux CI
   checks before merge. Deployment ships the catalog/seed and provider changes;
   this task did not apply ownership records or write production data.
2. On the deployed backend, run onboarding without flags to review its dry-run.
   After reviewing the proposed ownership records, the owner may run `--apply`.
3. Run content and backlink verification one network pass at a time. Review each
   dry-run before its paired write. Keep all recorded robots, terms and challenge
   refusals; a reachable parent board is not proof of subsidiary ownership.
4. Review the coverage report before persisting its snapshot. Production coverage
   numbers come from that snapshot, not the public sample counts in this report.

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

The existing USAJOBS provider needs owner-configured `USAJOBS_API_KEY` and
`USAJOBS_USER_AGENT`; obtain the free key through the official USAJOBS developer
site. This task did not request, read or configure those credentials.

Rollback: revert the combined code/catalog change to restore its prior source
configuration. Any ownership records subsequently applied by the owner are
separate additive data; review their recorded evidence and operator date before
planning a data rollback. Do not reset or reseed production collections.

## Coverage and safety fixes included

- Employer-linked endpoints and actual crawl membership are reconciled together;
  moved boards retain verified replacement tokens and exact canonical URLs.
- Oracle ORC, iCIMS, Taleo, Phenom, SmartRecruiters, Eightfold, Avature and
  SuccessFactors preserve partial reads at deadlines, caps, malformed responses,
  repeated pages and unreached totals. Higher observed totals are retained when
  the board changes during paging. A partial read must not close unseen jobs.
- Phenom and SmartRecruiters have larger bounded page budgets. Public before/after
  measurements and remaining partial reads are recorded in the coverage ledger.
- Workable and Rippling merge repeated requisitions while preserving separate
  locations. SuccessFactors detail locations fill missing listing locations;
  explicit multi-location detail fields remain available to normalization.
- Eightfold classic keeps missing posting dates unknown. Other detail-only or
  undated templates also retain unknown dates rather than inventing current ones.
- Avature template variants, bounded own-site hydration/query-ID listings, and
  the proven Mott MacDonald and Enphase contracts use published paths and fields.
- TalentBrew and DirectEmployers/NLX readers use the validated public contracts;
  access restrictions and bounded reads remain visible. Source location rows,
  distinct requisitions and advertised totals are measured separately.

## Remaining restrictions and owner verification

Blocked, unsupported, manual and unverified rows remain separate from genuinely
empty boards. Shared-parent hiring-entity boundaries remain unresolved where the
public evidence does not identify the requested employer. No parser or ownership
filter broadens a subsidiary into every job on its parent board.

Meta terms, Google results-paging robots, Intuit search-jobs robots and Fluor
robots exclusions remain in force. Other robots refusals and host challenges are
listed in `docs/coverage/board-backlog-2026-11.md`; neither an HTTP200 shell nor a
zero-row parser result proves an empty inventory.

Nine research employers already had static crawl rows and still need the owner's
content/backlink verification: Upwind Security, Figure AI, Zoom, Ubisoft, Axonius,
Tines, Cato Networks, Comcast and Rocket. The owner must also establish ownership
for each independently filtered additional member, including Deloitte's second
member, before relying on canonical attribution.

The exact requested denominator, all final outcomes, provider value decisions,
dated request chains, sample limits and owner handoffs are in the coverage ledger
and its linked evidence JSON files. No database-writing command above was applied
by this task.
