# Direct-employer onboarding — outcomes per company

Generated 2026-10-22T02:00:13Z by `scripts/onboard_researched_companies.py --write-docs` (direct-employer-onboarding-1.0.0). Research reviewed 2026-10-21.

**Coverage basis:** in-repo catalogs and seed tables the crawler loads (no production inventory was read). `existing_covered` means the crawl tables already carry the exact endpoint; it is not a claim about live inventory counts.

Validation counts: `source` = total the vendor reported (blank when the API reports none), `parsed` = rows our provider produced, `req` = distinct requisitions after removing General Application/talent-community rows (`non-req`). Status is complete | budget_limited | blocked | failed | not_run. A blocked/failed run is unverified, never zero.

## 174 researched LinkedIn leads

Denominator: **174**. existing_covered: 4, new_validated: 19, excluded: 61, review: 31, blocked: 29, unsupported: 22, pending: 8

| company | evidence | provider | endpoint | outcome | why | validated at | status | source | parsed | req | non-req |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 7Seventy Recruiting | excluded_by_preference |  |  | excluded | staffing |  | not_run |  |  |  |  |
| AAR | CHAIN | taleo | `taleo/aarcorp.taleo.net/aarcorp` | existing_covered | row already in the crawl tables | 2026-10-22T01:59:32Z | complete |  | 190 | 190 | 0 |
| AARATECH | identity_or_business_model_review |  |  | review | mixed_review |  | not_run |  |  |  |  |
| AbilityLinks | excluded_by_preference |  |  | excluded | intermediary |  | not_run |  |  |  |  |
| Accenture Federal Services | excluded_by_preference |  |  | excluded | consultancy |  | not_run |  |  |  |  |
| Acrisure | FRONTEND | phenom | `phenom/careers.acrisure.com/acrisure` | blocked | not added; validation not_run |  | not_run |  |  |  |  |
| Agento | identity_unresolved |  |  | review | identity_unresolved |  | not_run |  |  |  |  |
| Alt | identity_unresolved |  |  | review | mixed_review |  | not_run |  |  |  |  |
| Amazon Web Services (AWS) | FRONTEND | amazon | `amazon/www.amazon.jobs/amazon` | existing_covered | row already in the crawl tables |  | not_run |  |  |  |  |
| Amentum | official_career_provider_unresolved |  |  | review | mixed_review |  | not_run |  |  |  |  |
| American Airlines | PENDING | Unresolved; SAP RMK candidate only |  | pending | jobs.aa.com answered 403; SAP RMK is a candidate only, never inferred from an HR job description |  | not_run |  |  |  |  |
| Amtex Systems Inc | excluded_by_preference |  |  | excluded | staffing |  | not_run |  |  |  |  |
| Anchorstacktech | excluded_by_preference |  |  | excluded | consultancy |  | not_run |  |  |  |  |
| Annapurna Labs | FRONTEND | amazon | `amazon/www.amazon.jobs/amazon` | existing_covered | row already in the crawl tables |  | not_run |  |  |  |  |
| Apollo Solutions | excluded_by_preference |  |  | excluded | staffing |  | not_run |  |  |  |  |
| Applied Systems | CHAIN | icims | `icims/globalcareers-appliedsystems.icims.com/globalcareers-appliedsystems` | blocked | not added; validation not_run |  | not_run |  |  |  |  |
| ApTask | excluded_by_preference |  |  | excluded | staffing |  | not_run |  |  |  |  |
| Ascent Developer Solutions | CHAIN | greenhouse | `greenhouse/ascentds` | new_validated | row added; live validation returned requisitions | 2026-10-22T01:59:20Z | complete | 11 | 11 | 11 | 0 |
| Astrion | identity_or_business_model_review |  |  | review | mixed_review |  | not_run |  |  |  |  |
| Atlys | PENDING |  |  | pending | official site links only a LinkedIn company-jobs page |  | not_run |  |  |  |  |
| Attune | MANUAL | own_site |  | unsupported | unsupported_pending_provider:own_site |  | not_run |  |  |  |  |
| Avalore.ai | official_ats_chain | Workable |  | review | mixed_review |  | not_run |  |  |  |  |
| BAE Systems, Inc. | FRONTEND | phenom | `phenom/jobs.baesystems.com/baesystems` | blocked | not added; validation not_run |  | not_run |  |  |  |  |
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
| Celestica | FRONTEND | successfactors | `successfactors/careers.celestica.com/celestica` | blocked | not added; validation not_run |  | not_run |  |  |  |  |
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
| DataVisor | PENDING | Workable |  | pending | Workable whr_embed(56790); account-to-slug link for datavisor-jobs not observed |  | not_run |  |  |  |  |
| Delta Electronics Americas | CHAIN | icims | `icims/careers-delta-americas.icims.com/careers-delta-americas` | blocked | not added; validation not_run |  | not_run |  |  |  |  |
| Destro | CHAIN | breezyhr |  | unsupported | unsupported_pending_provider:breezyhr |  | not_run |  |  |  |  |
| Digivance Solutions | excluded_by_preference |  |  | excluded | consultancy |  | not_run |  |  |  |  |
| E-Solutions | excluded_by_preference |  |  | excluded | staffing |  | not_run |  |  |  |  |
| EchoStar Corporation | CHAIN | radancy | `radancy/jobs.echostar.com/echostar` | blocked | not added; validation not_run |  | not_run |  |  |  |  |
| ECI | identity_or_business_model_review |  |  | review | mixed_review |  | not_run |  |  |  |  |
| ECS | identity_or_business_model_review |  |  | review | mixed_review |  | not_run |  |  |  |  |
| Enlighten | identity_or_business_model_review |  |  | review | mixed_review |  | not_run |  |  |  |  |
| Epic | FRONTEND | avature |  | unsupported | unsupported_pending_provider:avature |  | not_run |  |  |  |  |
| Evlo AI | identity_unresolved |  |  | review | identity_unresolved |  | not_run |  |  |  |  |
| FDE Times | identity_or_business_model_review |  |  | review | identity_unresolved |  | not_run |  |  |  |  |
| FDM Group | excluded_by_preference |  |  | excluded | consultancy |  | not_run |  |  |  |  |
| Fervo Energy | OWN | own_site |  | unsupported | unsupported_pending_provider:own_site |  | not_run |  |  |  |  |
| FieldAI | CONFIG | lever | `lever/field-ai` | existing_covered | row already in the crawl tables | 2026-10-22T01:59:29Z | complete |  | 109 | 108 | 1 |
| First Citizens Bank | FRONTEND | radancy | `radancy/jobs.firstcitizens.com/firstcitizens` | blocked | not added; validation not_run |  | not_run |  |  |  |  |
| firstPRO US | excluded_by_preference |  |  | excluded | staffing |  | not_run |  |  |  |  |
| Flexcompute | CHAIN | workable | `workable/flexcompute` | new_validated | row added; live validation returned requisitions | 2026-10-22T01:59:29Z | complete |  | 20 | 20 | 0 |
| Flint Hills Resources | FRONTEND | avature |  | unsupported | unsupported_pending_provider:avature |  | not_run |  |  |  |  |
| Fluidstack | CHAIN | ashby | `ashby/fluidstack` | new_validated | row added; live validation returned requisitions | 2026-10-22T01:59:25Z | complete |  | 254 | 253 | 1 |
| Fonzi AI | excluded_by_preference |  |  | excluded | intermediary |  | not_run |  |  |  |  |
| Frequentis | FRONTEND | avature |  | unsupported | unsupported_pending_provider:avature |  | not_run |  |  |  |  |
| Fueled | excluded_by_preference |  |  | excluded | consultancy |  | not_run |  |  |  |  |
| Fulcronix | excluded_by_preference |  |  | excluded | consultancy |  | not_run |  |  |  |  |
| GM Performance Power Units | CHAIN | dayforce |  | unsupported | unsupported_pending_provider:dayforce |  | not_run |  |  |  |  |
| GMI Cloud | MANUAL | own_site |  | unsupported | unsupported_pending_provider:own_site |  | not_run |  |  |  |  |
| Harnham | excluded_by_preference |  |  | excluded | staffing |  | not_run |  |  |  |  |
| Harrison Clarke | excluded_by_preference |  |  | excluded | staffing |  | not_run |  |  |  |  |
| HavocAI | CHAIN | ashby | `ashby/havocai` | new_validated | row added; live validation returned requisitions | 2026-10-22T01:59:26Z | complete |  | 26 | 26 | 0 |
| Haystack | identity_or_business_model_review |  |  | review | mixed_review |  | not_run |  |  |  |  |
| Honeywell Technologies | CHAIN | oracle_orc | `oracle_orc/ibqbjb.fa.ocs.oraclecloud.com/ibqbjb/Honeywell` | blocked | not added; validation not_run |  | not_run |  |  |  |  |
| Hover | CONFIG | greenhouse | `greenhouse/hover` | new_validated | row added; live validation returned requisitions | 2026-10-22T01:59:22Z | complete | 19 | 19 | 19 | 0 |
| Howmet Aerospace | CHAIN | oracle_orc | `oracle_orc/fa-exty-saasfaprod1.fa.ocs.oraclecloud.com/fa-exty-saasfaprod1/CX_1` | blocked | not added; validation not_run |  | not_run |  |  |  |  |
| ICE | FRONTEND | radancy | `radancy/careers.ice.com/ice` | blocked | not added; validation not_run |  | not_run |  |  |  |  |
| Infinite Computer Solutions | excluded_by_preference |  |  | excluded | consultancy |  | not_run |  |  |  |  |
| Innovative Defense Technologies (IDT) | official_ats_chain | Pinpoint |  | review | mixed_review |  | not_run |  |  |  |  |
| Inspira Financial | CHAIN | adp_myjobs |  | unsupported | unsupported_pending_provider:adp_myjobs |  | not_run |  |  |  |  |
| InstantServe LLC | excluded_by_preference |  |  | excluded | staffing |  | not_run |  |  |  |  |
| Intuitive | CHAIN | smartrecruiters | `smartrecruiters/Intuitive` | new_validated | row added; live validation returned requisitions | 2026-10-22T02:00:10Z | budget_limited | 450 | 200 | 200 | 0 |
| Iperium | excluded_by_preference |  |  | excluded | staffing |  | not_run |  |  |  |  |
| KTek Resourcing | excluded_by_preference |  |  | excluded | staffing |  | not_run |  |  |  |  |
| Largeton Group | excluded_by_preference |  |  | excluded | staffing |  | not_run |  |  |  |  |
| LendingTree | PENDING | Greenhouse integration suspected; tenant unresolved |  | pending | gh_jid hints only; no board token observed |  | not_run |  |  |  |  |
| Lincoln Institute of Land Policy | CHAIN | paycom |  | unsupported | unsupported_pending_provider:paycom |  | not_run |  |  |  |  |
| Longbridge | OWN | own_site |  | unsupported | unsupported_pending_provider:own_site |  | not_run |  |  |  |  |
| MANTECH | official_career_provider_unresolved |  |  | review | mixed_review |  | not_run |  |  |  |  |
| Maritz | official_ats_chain | Workday |  | review | mixed_review |  | not_run |  |  |  |  |
| Marlin Selection Recruitment | excluded_by_preference |  |  | excluded | staffing |  | not_run |  |  |  |  |
| MatchPoint | identity_or_business_model_review |  |  | review | mixed_review |  | not_run |  |  |  |  |
| MATS Research | OWN | own_site |  | unsupported | unsupported_pending_provider:own_site |  | not_run |  |  |  |  |
| Mission Technologies, a division of HII | official_career_provider_unresolved |  |  | review | mixed_review |  | not_run |  |  |  |  |
| Morales Professional, a morales group company | excluded_by_preference |  |  | excluded | staffing |  | not_run |  |  |  |  |
| Motion Recruitment | excluded_by_preference |  |  | excluded | staffing |  | not_run |  |  |  |  |
| mroads | excluded_by_preference |  |  | excluded | consultancy |  | not_run |  |  |  |  |
| NCR Voyix | CHAIN | workday | `workday/ncr/ext_us/wd1` | blocked | not added; validation not_run |  | not_run |  |  |  |  |
| Netrolynx AI | identity_unresolved |  |  | review | identity_unresolved |  | not_run |  |  |  |  |
| NexGen Tech Solutions | excluded_by_preference |  |  | excluded | staffing |  | not_run |  |  |  |  |
| Nomura | FRONTEND | successfactors | `successfactors/careers.nomura.com/nomura` | blocked | not added; validation not_run |  | not_run |  |  |  |  |
| Northwood | CHAIN | ashby | `ashby/NorthwoodSpace` | new_validated | row added; live validation returned requisitions | 2026-10-22T01:59:26Z | complete |  | 63 | 62 | 1 |
| Nous Research | MANUAL | own_site |  | unsupported | unsupported_pending_provider:own_site |  | not_run |  |  |  |  |
| OPENLANE | CHAIN | workday | `workday/openlane/OPENLANE_Careers/wd115` | blocked | not added; validation not_run |  | not_run |  |  |  |  |
| Optomi | excluded_by_preference |  |  | excluded | staffing |  | not_run |  |  |  |  |
| Orange Logic | OWN | own_site |  | unsupported | unsupported_pending_provider:own_site |  | not_run |  |  |  |  |
| Peraton | official_ats_chain | iCIMS |  | review | mixed_review |  | not_run |  |  |  |  |
| Premier Global Links LLC | excluded_by_preference |  |  | excluded | consultancy |  | not_run |  |  |  |  |
| Productive Playhouse | CHAIN | jazzhr | `jazzhr/productiveplayhouse.applytojob.com/productiveplayhouse` | blocked | not added; validation not_run |  | not_run |  |  |  |  |
| PSEG Long Island LLC | FRONTEND | successfactors | `successfactors/jobs.psegliny.com/psegliny` | blocked | not added; validation not_run |  | not_run |  |  |  |  |
| Quantcast | CHAIN | ashby | `ashby/quantcast` | new_validated | row added; live validation returned requisitions | 2026-10-22T01:59:27Z | complete |  | 15 | 15 | 0 |
| Quindar | CONFIG | ashby | `ashby/quindar` | new_validated | row added; live validation returned requisitions | 2026-10-22T01:59:27Z | complete |  | 20 | 20 | 0 |
| Radiant Systems Inc | excluded_by_preference |  |  | excluded | staffing |  | not_run |  |  |  |  |
| Raft | official_career_provider_unresolved |  |  | review | mixed_review |  | not_run |  |  |  |  |
| Re:Build | CHAIN | greenhouse | `greenhouse/rebuildmanufacturing` | new_validated | row added; live validation returned requisitions | 2026-10-22T01:59:22Z | complete | 130 | 130 | 130 | 0 |
| Recruiting from Scratch | excluded_by_preference |  |  | excluded | staffing |  | not_run |  |  |  |  |
| Reflex | PENDING | YC jobs publisher; ATS unresolved |  | pending | YC jobs publisher only; not workreflex.com or greenhouse/reflex |  | not_run |  |  |  |  |
| RemoteHunter | excluded_by_preference |  |  | excluded | intermediary |  | not_run |  |  |  |  |
| RevSpring | CHAIN | newton_paycor |  | unsupported | unsupported_pending_provider:newton_paycor |  | not_run |  |  |  |  |
| Rhoda AI | CHAIN | ashby | `ashby/rhoda-ai` | new_validated | row added; live validation returned requisitions | 2026-10-22T01:59:28Z | complete |  | 45 | 45 | 0 |
| Rise Internet | identity_unresolved |  |  | review | identity_unresolved |  | not_run |  |  |  |  |
| Rugged Science | OWN | own_site |  | unsupported | unsupported_pending_provider:own_site |  | not_run |  |  |  |  |
| Saragossa | excluded_by_preference |  |  | excluded | staffing |  | not_run |  |  |  |  |
| Schaeffler | FRONTEND | successfactors | `successfactors/jobs.schaeffler.com/schaeffler` | blocked | not added; validation not_run |  | not_run |  |  |  |  |
| Schurz Communications | CHAIN | ukg | `ukg/recruiting.ultipro.com/SCH1012SHRZ` | blocked | not added; validation not_run |  | not_run |  |  |  |  |
| SEI | CHAIN | workday | `workday/seic/SEI_Global_Services/wd1` | blocked | not added; validation not_run |  | not_run |  |  |  |  |
| Sigma Defense Systems LLC | official_career_provider_unresolved |  |  | review | mixed_review |  | not_run |  |  |  |  |
| SimVentions | official_ats_chain | iCIMS |  | review | mixed_review |  | not_run |  |  |  |  |
| SoftWriters | CHAIN | ukg | `ukg/recruiting.ultipro.com/ROP1001ROPER/cf088029-453b-46f8-bcab-46ffb10fdf54` | unsupported | shared Roper UKG board; the UKG provider cannot filter to the hiring entity, and importing every Roper job as SoftWriter |  | not_run |  |  |  |  |
| SolutionHealth | FRONTEND | radancy_talentbrew |  | unsupported | unsupported_pending_provider:radancy_talentbrew |  | not_run |  |  |  |  |
| SoTalent | excluded_by_preference |  |  | excluded | intermediary |  | not_run |  |  |  |  |
| Spring Arbor University | CHAIN | newton_paycor |  | unsupported | unsupported_pending_provider:newton_paycor |  | not_run |  |  |  |  |
| Spring Venture Group | CHAIN | smartrecruiters | `smartrecruiters/SpringVentureGroup1` | new_validated | row added; live validation returned requisitions | 2026-10-22T01:59:32Z | complete | 6 | 6 | 6 | 0 |
| STR | official_ats_chain | Greenhouse |  | review | mixed_review |  | not_run |  |  |  |  |
| Suron | identity_or_business_model_review |  |  | review | mixed_review |  | not_run |  |  |  |  |
| Swift | CHAIN | workday | `workday/swift/Join-Swift/wd3` | blocked | not added; validation not_run |  | not_run |  |  |  |  |
| TalentAlly | excluded_by_preference |  |  | excluded | intermediary |  | not_run |  |  |  |  |
| TalentHop | identity_or_business_model_review |  |  | review | mixed_review |  | not_run |  |  |  |  |
| TalentOla | excluded_by_preference |  |  | excluded | staffing |  | not_run |  |  |  |  |
| TaskVerse | excluded_by_preference |  |  | excluded | intermediary |  | not_run |  |  |  |  |
| Tech Consulting | excluded_by_preference |  |  | excluded | staffing |  | not_run |  |  |  |  |
| Tecology, Inc. | excluded_by_preference |  |  | excluded | consultancy |  | not_run |  |  |  |  |
| Temple Health – Temple University Health System | OWN | own_site |  | unsupported | unsupported_pending_provider:own_site |  | not_run |  |  |  |  |
| Texas Windstorm Insurance Association | CHAIN | adp_wfn | `adp_wfn/workforcenow.adp.com/5a22940c-3629-44d9-813a-0ccee8ac9aac/19000101_000001` | blocked | not added; validation not_run |  | not_run |  |  |  |  |
| The Depository Trust & Clearing Corporation (DTCC) | CHAIN | oracle_orc | `oracle_orc/ebxr.fa.us2.oraclecloud.com/ebxr/CX_1` | blocked | not added; validation not_run |  | not_run |  |  |  |  |
| The Home Depot | CHAIN | workday | `workday/homedepot/CareerDepot/wd5` | blocked | not added; validation not_run |  | not_run |  |  |  |  |
| The Lubrizol Corporation | FRONTEND | successfactors | `successfactors/jobs.lubrizol.com/lubrizol` | blocked | not added; validation not_run |  | not_run |  |  |  |  |
| TikTok USDS Joint Venture | OWN | tiktok_usds |  | unsupported | unsupported_pending_provider:tiktok_usds |  | not_run |  |  |  |  |
| Torentify | excluded_by_preference |  |  | excluded | intermediary |  | not_run |  |  |  |  |
| Trexquant Investment LP | CONFIG | workable | `workable/trexquant` | new_validated | row added; live validation returned requisitions | 2026-10-22T01:59:29Z | complete |  | 50 | 34 | 0 |
| Troon | CHAIN | ukg | `ukg/recruiting2.ultipro.com/TRO1001TROO/2b13054b-60bb-410d-9136-e52c1c7d9720` | blocked | not added; validation not_run |  | not_run |  |  |  |  |
| UICGS / Bowhead Family of Companies | official_ats_chain | iCIMS |  | review | mixed_review |  | not_run |  |  |  |  |
| Unison | CHAIN | ashby | `ashby/unison-software` | new_validated | row added; live validation returned requisitions | 2026-10-22T01:59:28Z | complete |  | 13 | 13 | 0 |
| Universal Background Screening | CHAIN | appone |  | unsupported | unsupported_pending_provider:appone |  | not_run |  |  |  |  |
| Uplight | CHAIN | jobvite | `jobvite/uplight` | blocked | not added; validation not_run |  | not_run |  |  |  |  |
| Upwind Security | PENDING | Comeet plugin |  | pending | Comeet plugin observed; company token/API unresolved (official page 403) |  | not_run |  |  |  |  |
| V-Soft Consulting Group, Inc. | excluded_by_preference |  |  | excluded | staffing |  | not_run |  |  |  |  |
| Vantor | CHAIN | workday | `workday/maxar/Vantor/wd1` | blocked | not added; validation not_run |  | not_run |  |  |  |  |
| Vaspire Technologies Inc. | excluded_by_preference |  |  | excluded | staffing |  | not_run |  |  |  |  |
| Ventura Foods | FRONTEND | successfactors | `successfactors/jobs.venturafoods.com/venturafoods` | blocked | not added; validation not_run |  | not_run |  |  |  |  |
| Ventures Unlimited Inc | excluded_by_preference |  |  | excluded | staffing |  | not_run |  |  |  |  |
| Verint | CHAIN | oracle_orc | `oracle_orc/fa-epcb-saasfaprod1.fa.ocs.oraclecloud.com/fa-epcb-saasfaprod1/CX` | blocked | not added; validation not_run |  | not_run |  |  |  |  |
| Verisk | PENDING |  |  | pending | corporate page challenged; SmartRecruiters/Oracle candidates unproven |  | not_run |  |  |  |  |
| Vertex Pharmaceuticals | CHAIN | workday | `workday/vrtx/vertex_careers/wd501` | blocked | not added; validation not_run |  | not_run |  |  |  |  |
| Visionist, Inc. | official_ats_chain | Jobvite |  | review | mixed_review |  | not_run |  |  |  |  |
| Vizient | excluded_by_preference |  |  | excluded | consultancy |  | not_run |  |  |  |  |
| Voto Consulting LLC | excluded_by_preference |  |  | excluded | staffing |  | not_run |  |  |  |  |
| Wiraa | excluded_by_preference |  |  | excluded | intermediary |  | not_run |  |  |  |  |
| WisdomAI | CHAIN | ashby | `ashby/Wisdom-AI` | new_validated | row added; live validation returned requisitions | 2026-10-22T01:59:29Z | complete |  | 19 | 19 | 0 |
| WTW | excluded_by_preference |  |  | excluded | consultancy |  | not_run |  |  |  |  |
| Xforia Global Talent  & Technology Solutions | excluded_by_preference |  |  | excluded | staffing |  | not_run |  |  |  |  |

## Owner priority: top employers per domain

Denominator: **0**. 

| company | domain | evidence | provider | endpoint | outcome | why | validated at | status | source | parsed | req | non-req |
|---|---|---|---|---|---|---|---|---|---|---|---|---|

## Existing rows health-checked (no duplicates added)

Denominator: **3**. existing_covered: 2, existing_unhealthy_or_moved: 1

| company | evidence | provider | endpoint | outcome | why | validated at | status | source | parsed | req | non-req |
|---|---|---|---|---|---|---|---|---|---|---|---|
| Duolingo |  | greenhouse | `greenhouse/duolingo` | existing_covered | row already in the crawl tables | 2026-10-22T01:59:41Z | complete | 61 | 61 | 61 | 0 |
| Nordstrom |  | workday | `workday/nordstrom/nordstrom_careers/wd501` | existing_covered | row already in the crawl tables | 2026-10-22T01:59:41Z | budget_limited | 1519 | 400 | 400 | 0 |
| Garmin |  |  |  | existing_unhealthy_or_moved | Only an official_career_registry row (careers.garmin.com); no crawlable catalog row in the repo. |  | not_run |  |  |  |  |

