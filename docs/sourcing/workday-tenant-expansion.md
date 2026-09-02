# Workday Tenant Catalog — Expansion Findings

**Status:** ready to implement · **Last updated:** 2026-07-19
**Scope:** grow the Workday tab's `WORKDAY_TENANT_CATALOG` with more **US** companies.

> Nothing here has been added to `services/workday_tenant_catalog.py` yet — this doc
> IS the candidate set. All rows validated live (see the reachability stamp in §3).

---

## ▶ HANDOFF — implement in this order

**What this is:** ~394 net-new, live-validated Workday companies (batches 1–28 in §3),
each a paste-ready `{...}` dict already tagged with an `industry` from the §0 taxonomy.

1. **Paste the rows.** Copy every `{"display_name": …}` **data row** from the §3 code
   block into `shared/python/services/workday_tenant_catalog.py` → `WORKDAY_TENANT_CATALOG`.
   **Skip every `#` comment line** — those are notes (dead/gated/already-in-532), not rows.
2. **Backend: no other change.** `INDUSTRIES` is derived from the catalog rows and
   `catalog_summary()` has no allowlist (§0.3), so new `industry` values work immediately.
3. **Frontend: add 6 labels** to `INDUSTRY_LABELS` in
   `jobpilot-frontend/src/components/job-search/WorkdayJobsPanel.tsx` (~L95) — the exact
   snippet is in **§0.3**. Without it the new chips render raw snake_case. (`gaming` already labeled.)
4. **4 shared-tenant rows** point at the SAME `tenant` with a DIFFERENT `site` (distinct
   job sets — keep both): `wisconsin` (UW-Madison + Universities of Wisconsin), `publicmedia`
   (New England Public Media + GBH), `myworkdaycenter` (MediaNews + Tribune), `theclaremontcolleges`
   (Pomona). If your catalog de-dupes by `tenant` alone, key on `tenant`+`site` instead.
5. **Optional but recommended: re-validate first** (endpoints drift ~1–2%/month). Run
   `scripts/extract_rows.py`* → feed the output to `scripts/discover_workday_tenants.py`;
   drop anything newly `dead`. (*extractor currently in the session scratchpad — commit it if you want it permanent.)

**`industry` values are editable judgment calls** (they only affect filter grouping, not
reachability). A few rows are reachable-but-thin — flagged inline (`⚠ low US%`, `low volume`).

---

## 0. Industry taxonomy — NEW category keys + UI labels (decided this session)

**Problem this solves:** ~140 of the ~226 new Workday rows (and most non-tech in
the 532) had **no matching `industry` key**, so they were all dumped into `other`.
That makes the Workday-tab industry filter useless for the biggest new veins
(universities, government, healthcare-adjacent, hospitality…). We now **add 6 new
industry keys** and have **relabeled every `other`/`TODO` row in this doc + the
career-pages doc** to them.

### 0.1 The 6 new keys (with the UI label to show)

| new `industry` key | UI label (add to `INDUSTRY_LABELS`) | What it covers | Rows relabeled this session |
|---|---|---|---|
| `education`    | **Education**    | universities, colleges, community colleges, K-12 school districts, charter networks | **73** |
| `government`   | **Government**   | state / county / city govts **+ public authorities** (transit, airport, water, port, housing-finance) + public pension funds | **46** |
| `nonprofit`    | **Nonprofit**    | nonprofits, foundations, research institutes / think tanks | **11** |
| `hospitality`  | **Hospitality**  | hotels, restaurants / food-service chains, casinos & resorts, entertainment venues, hospitality services (valet, housekeeping, catering) | **9** |
| `travel`       | **Travel**       | airlines, cruise lines, aviation ground handling | **4** |
| `real_estate`  | **Real Estate**  | commercial-real-estate services, property management, REITs | **3** |

> Combined with the career-pages doc, this key set covers **every** previously-`other`
> row. Total valid `industry` keys becomes **21** (15 existing + 6 new):
> `tech, fintech, healthtech, banking, insurance, pharma, energy, telecom,
> consulting, ecommerce_retail, manufacturing_automotive, aerospace_defense,
> media, gaming, other` **+ `education, government, nonprofit, hospitality,
> travel, real_estate`**.

### 0.2 Grouping decisions (the judgment calls)

- **Public authorities → `government`, not a separate key.** Transit agencies,
  airport authorities, water/utility districts, port authorities, and
  housing-finance agencies are all public-sector employers, so they ride under
  `government` (≈10 of the 46). If that transit/airport/port vein grows, split it
  into a `transportation` key later — flagged, not done.
- **Airlines/cruise/ground-handling → `travel`.** This **resolves the two
  long-standing `TODO` rows** (Southwest, United) — they are now `travel`.
- **Restaurants + hotels + casinos + entertainment → one `hospitality` key**
  (kept `travel` separate for airlines/cruise, which are a distinct job market).
- **Food *distribution* (Sysco, US Foods, Gordon Food Service) stays
  `ecommerce_retail`** (wholesale/distribution), not `hospitality`.
- **Universities that also run a health system**: the academic/staff tenant →
  `education`; a separately-cataloged hospital tenant stays `healthtech`.
- **Sabre** stays `tech` (travel *technology* / GDS is a software employer).

### 0.3 What to change in code (verified against the live code this session)

**Backend — NO code change needed.** `INDUSTRIES` in both catalogs is *derived*
(`sorted({row["industry"] for row in CATALOG})` — `workday_tenant_catalog.py:563`,
`career_pages_catalog.py:627`), and `catalog_summary()` groups rows by their
`industry` value with **no allowlist** (`workday_jobs_service.py:1279`). So a new
key "just works" the moment a catalog row uses it — the filter chip appears
automatically.

**Frontend — ONE edit.** Add the 6 labels to `INDUSTRY_LABELS` in
`jobpilot-frontend/src/components/job-search/WorkdayJobsPanel.tsx` (~line 95).
This map is shared by **both** the Workday tab and the career-pages tab
(`WorkdayJobsPanel.tsx:244` — non-MNC variants use `INDUSTRY_LABELS`), so one edit
labels both. Chips render `industryLabels[key] ?? key` (`:609`), so **without this
edit the new chips still work but display the raw snake_case key**
(`real_estate`, `government`) instead of a friendly label.

```ts
// jobpilot-frontend/src/components/job-search/WorkdayJobsPanel.tsx — INDUSTRY_LABELS
  education: 'Education',
  government: 'Government',
  nonprofit: 'Nonprofit',
  hospitality: 'Hospitality',
  travel: 'Travel',
  real_estate: 'Real Estate',
```

> **Order note:** `INDUSTRY_LABELS` is a plain map; chip order in the UI comes from
> the backend `catalog_summary()` which sorts industries alphabetically by key —
> so no ordering work is needed here.

---

## 1. Where we stand today

| Catalog | File | Companies | Health (live probe) |
|---|---|---|---|
| **Workday tab** | `services/workday_tenant_catalog.py` | **532** | ✅ **525/532 return live jobs** — healthy, single validated paths, no runtime probing |
| Live-search helper | `services/workday_cxs_fetcher.py` (`DEFAULT_WORKDAY_TENANTS`) | 108 | ❌ **~5/108 still resolve** — stale, multi-candidate probing, mostly dead |

**Key facts established:**
- The **532 is well-maintained** and comprehensive for prominent US enterprises. Every famous company probed (Zoom, NXP, Amgen, Leidos, Guidewire, AutoNation, CNA…) was already in it.
- Total detectable Workday universe ≈ **~2,849 companies** globally ([bloomberry](https://bloomberry.com/data/workday/)); a large share are **non-US** or **long-tail/mid-market**, so the realistic US pool is ~1,000–1,500. We cover roughly a third to half of that.
- **You cannot guess coordinates** — a probe of 55 US companies with guessed `tenant/host/site` returned **0** hits. The `site` path is company-specific and unguessable.
- **Some big names gate the endpoint** (HTTP 422 on every path): Ford, GM, ServiceNow, VMware, American Express, PepsiCo, UPS, Honeywell, FedEx… Direct CXS can't reach them — those need the MNC tab's `aggregator` (JSearch) path instead.
- The remaining gap is the **long tail** (consumer / CPG / retail / food / airlines) — sectors the 532 under-covers.

---

## 2. The method (reusable)

**Discovery:** web-search `"<company> myworkdayjobs"` → the top hit is the company's Workday URL → read coordinates off it:
`https://{tenant}.{wd_host}.myworkdayjobs.com/[xx-XX/]{site}/…`
If no `myworkdayjobs` URL appears, the company uses a different ATS (skip).

**Validation + dedup:** [`scripts/discover_workday_tenants.py`](../scripts/discover_workday_tenants.py)
```
PYTHONPATH=shared/python python scripts/discover_workday_tenants.py candidates.txt
# candidates.txt lines: "Company Name | https://tenant.wdNN.myworkdayjobs.com/site"
```
For each candidate it: parses coordinates → probes CXS `/jobs` (200 + postings) →
US-checks sampled postings → dedups vs the 532 → prints paste-ready rows.

---

## 3. Validated NEW US companies — candidate additions (batches 1–24)

Live-validated 2026-07-18. Coordinates confirmed returning US jobs; **not** in the 532.
`industry` values are now assigned to the **§0 taxonomy** (the old `other`/`TODO`
rows were relabeled this session — no row below is `other` anymore).

> **Running tally: ~394 net-new validated companies** across batches 1–28.
> **✅ Full re-validation 2026-07-19:** batches 1–27 (345 rows) re-probed live via the
> real CXS fetcher → **345/345 reachable, 0 dead, 0 non-US**; batch 28 (+49) validated
> live on insertion. (Companion career-pages doc: 134/134 reachable.)
> - batches 13–15 (**50**): retail/apparel, food-service/hospitality/travel, state/local government.
> - batches 16–20 (**77**): CPG/food/beverage (16), universities/colleges (23), community-colleges/school-districts/transit/airport/water/port (15), healthcare/senior-care/behavioral/health-insurers (16), nonprofits/foundations/research (7).
> - batches 21–24 (**35**): energy/utilities/oil-&-gas (11), industrials/manufacturing/aerospace-defense (13), financial-services/insurance/real-estate (6), universities/government (5).
> - **batch 25 (session 2, +39):** healthcare/health-systems (15), health insurers (4), universities/community-colleges/K-12 (9), REITs/CRE/homebuilders (6), restaurants/hotels (2), car-rental (1), counties (2).
> - **batch 26 (session 2 r2, +37):** nonprofits/foundations/museums/arts/conservation (12), regional health systems/senior-care (10), banking/credit-unions/asset-mgmt (5), universities (4), public-media (2), health insurers (2), cities/counties (2).
> - **batch 27 (session 2 r3, +43):** industrials/chemicals/packaging (13), media/broadcast/publishing (8), retail/apparel/consumer/ag (8), consulting/E&C/staffing (9), aerospace-defense (2), telecom (2), energy (1).
> - **batch 28 (session 2 r4, +49):** private colleges/art-schools/CCs (20), medical-device/diagnostics/tools (10), counties/cities (12), pharma/CRO/CDMO (7).
>
> A **parallel career-pages sweep** (board-ATS companies, not Workday) added **129**
> more (51 + 17 + 36 + 15 + **10** in session-2..5) — see [career-pages-expansion.md](career-pages-expansion.md).
> Combined running total: **~527 net-new** across both catalogs.
>
> Discovery used parallel sector-scoped web-search agents → the reusable
> `discover_workday_tenants.py` validator (dedup vs 532 + live CXS + US-check).
> **Industry assignment (per §0 taxonomy, verified against the live 532 + frontend `INDUSTRY_LABELS`):**
> hospitals/health-systems/senior-care → `healthtech`; health insurers → `insurance`;
> banks/asset-managers/credit-unions → `banking`; food/CPG/food-distribution → `ecommerce_retail`;
> energy/utilities/oil-&-gas → `energy`; industrials → `manufacturing_automotive`
> (defense/aerospace → `aerospace_defense`); **universities/K-12 → `education`;
> government/transit/airport/water/port/housing → `government`; nonprofits/foundations → `nonprofit`;
> hotels/restaurants/casinos → `hospitality`; airlines/cruise/car-rental → `travel`;
> REITs/CRE/property-mgmt/homebuilders → `real_estate`** (the 6 NEW keys — see **§0**).

```python
# --- consumer / CPG / food ---
{"display_name": "Coca-Cola",          "tenant": "coke",         "wd_host": "wd1", "site": "coca-cola-careers",            "industry": "ecommerce_retail"},   # ~197 jobs, US 65%
{"display_name": "Tyson Foods",        "tenant": "tysonfoods",   "wd_host": "wd5", "site": "tsn",                         "industry": "ecommerce_retail"},   # ~602 jobs, US 100%
{"display_name": "Campbell's",         "tenant": "campbellsoup", "wd_host": "wd5", "site": "ExternalCareers_GlobalSite",  "industry": "ecommerce_retail"},   # ~301 jobs, US 100%
{"display_name": "J.M. Smucker",       "tenant": "smucker",      "wd_host": "wd5", "site": "us_external_careers",         "industry": "ecommerce_retail"},   # ~92 jobs,  US 100%
{"display_name": "Dollar Tree",        "tenant": "dollartree",   "wd_host": "wd5", "site": "dollartreeus",                "industry": "ecommerce_retail"},   # ~23,700 jobs, US 100%  (⚠ retail/store-heavy, low tech relevance)
# --- energy ---
{"display_name": "Plug Power",         "tenant": "plugpower",    "wd_host": "wd5", "site": "plug_power_inc",              "industry": "energy"},             # ~81 jobs,  US 90%
# --- airline (no exact category — needs a call) ---
{"display_name": "Southwest Airlines", "tenant": "swa",          "wd_host": "wd1", "site": "external",                    "industry": "travel"},             # ~47 jobs,  US 65%   (batch 1)
# --- batch 2 (2026-07-18) — food distribution + airline ---
{"display_name": "Sysco",              "tenant": "sysco",        "wd_host": "wd5", "site": "syscocareers",                "industry": "ecommerce_retail"},   # ~2,000 jobs, US 95%
{"display_name": "United Airlines",    "tenant": "uaa",          "wd_host": "wd12","site": "EXT",                         "industry": "travel"},             # ~2 jobs (low volume right now), US 100%
# --- batch 3 (2026-07-18) — industrials sweep (mostly custom/Oracle/iCIMS/Eightfold; only 1 Workday hit) ---
{"display_name": "Genuine Parts Company (NAPA)", "tenant": "genpt", "wd_host": "wd1", "site": "Careers",                 "industry": "ecommerce_retail"},   # ~2,000 jobs, US 100%
# --- batch 4 (2026-07-18) — universities + consumer/hospitality (non-tech-rich) ---
{"display_name": "Carnegie Mellon University",   "tenant": "cmu",          "wd_host": "wd5",  "site": "CMU",                     "industry": "education"},          # ~196 jobs,   US 100%  (education — no exact category)
{"display_name": "Georgetown University",        "tenant": "georgetown",   "wd_host": "wd1",  "site": "Georgetown_Admin_Careers","industry": "education"},          # ~99 jobs,    US 95%   (education)
{"display_name": "Choice Hotels",                "tenant": "choicehotels", "wd_host": "wd5",  "site": "External",                "industry": "hospitality"},        # ~42 jobs,    US 100%  (hospitality)
{"display_name": "Tapestry (Coach, Kate Spade)", "tenant": "tapestry",     "wd_host": "wd108","site": "Tapestry_Careers",        "industry": "ecommerce_retail"},   # ~1,753 jobs, US 95%
# Yale University — RESOLVED in batch 24 below (yale.wd1/temporary_career_site, ~188 US jobs). [this earlier batch-4 blocker is closed]
# --- batch 5 (2026-07-18) — more universities (education, non-tech) ---
{"display_name": "University of Southern California", "tenant": "usc",      "wd_host": "wd5", "site": "ExternalUSCCareers",  "industry": "education"}, # ~822 jobs, US 100%  (education)
{"display_name": "University of Chicago",             "tenant": "uchicago", "wd_host": "wd5", "site": "External",            "industry": "education"}, # ~376 jobs, US 100%  (education)
{"display_name": "Cornell University",                "tenant": "cornell",  "wd_host": "wd1", "site": "CornellCareerPage",   "industry": "education"}, # ~163 jobs, US 100%  (education)
# --- batch 6 (2026-07-18) — universities sweep (education, non-tech) ---
{"display_name": "Penn State University",             "tenant": "psu",      "wd_host": "wd1", "site": "PSU_Staff",           "industry": "education"}, # ~1,375 jobs, US 100%  (education)
{"display_name": "Washington University in St. Louis","tenant": "wustl",    "wd_host": "wd1", "site": "External",            "industry": "education"}, # ~632 jobs,   US 100%  (education)
{"display_name": "University of Pennsylvania",        "tenant": "upenn",    "wd_host": "wd1", "site": "careers-at-penn",     "industry": "education"}, # ~460 jobs,   US 100%  (education)
{"display_name": "Brown University",                  "tenant": "brown",    "wd_host": "wd5", "site": "staff-careers-brown", "industry": "education"}, # ~99 jobs,    US 100%  (education; distinct from "Brown University Health" already in the 532)
# --- batch 7 (2026-07-18) — big state universities (education, non-tech) ---
{"display_name": "Ohio State University",           "tenant": "osu",       "wd_host": "wd1", "site": "OSUCareers",     "industry": "education"}, # ~1,013 jobs, US 100%
{"display_name": "University of Texas at Austin",   "tenant": "utaustin",  "wd_host": "wd1", "site": "UTstaff",        "industry": "education"}, # ~553 jobs,   US 100%
{"display_name": "University of Washington",        "tenant": "uw",        "wd_host": "wd5", "site": "UWHires",        "industry": "education"}, # ~547 jobs,   US 100%
{"display_name": "University of Wisconsin-Madison", "tenant": "wisconsin", "wd_host": "wd1", "site": "UW_Madison",     "industry": "education"}, # ~322 jobs,   US 100%
{"display_name": "University of Maryland",          "tenant": "umd",       "wd_host": "wd1", "site": "UMCP",           "industry": "education"}, # ~174 jobs,   US 100%
{"display_name": "Texas A&M University",            "tenant": "tamus",     "wd_host": "wd1", "site": "TAMU_External",  "industry": "education"}, # ~122 jobs,   US 100%  (tenant=tamus is the A&M *System*; sibling campuses on same tenant: TAMUG/TAMUCC/TAMUK/... sites)
{"display_name": "University of North Florida",     "tenant": "unf",       "wd_host": "wd5", "site": "unfjobs",        "industry": "education"}, # ~108 jobs,   US 100%
{"display_name": "University of Louisville",        "tenant": "uofl",      "wd_host": "wd1", "site": "UofLCareerSite", "industry": "education"}, # ~103 jobs,   US 100%
{"display_name": "Florida Gulf Coast University",   "tenant": "fgcu",      "wd_host": "wd5", "site": "eaglejobs",      "industry": "education"}, # ~91 jobs,    US 100%
{"display_name": "Washington State University",     "tenant": "wsu",       "wd_host": "wd5", "site": "WSU_Jobs",       "industry": "education"}, # ~55 jobs,    US 100%
{"display_name": "Florida Atlantic University",     "tenant": "fau",       "wd_host": "wd1", "site": "FAU",            "industry": "education"}, # ~43 jobs,    US 100%
{"display_name": "Arizona State University",        "tenant": "adams",     "wd_host": "wd1", "site": "ASU",            "industry": "education"}, # ~25 jobs, US 100%  (RESOLVED 2026-07-19: academic tenant is 'adams'/ASU; the old 'asuep'/ASUEP was ASU Enterprise Partners — a separate foundation, not the university)
# batch 7 non-validating: UCF (ucf.wd1 -> HTTP 403), UVA (uva.wd1/UVAJobs empty — academic moved to Phenom). Non-Workday: Michigan State, Purdue (SuccessFactors), Indiana (PeopleAdmin), Minnesota, Colorado (Taleo), U-Florida, Princeton/Columbia/Stanford/NYU (from batch 6).
# --- batch 9 (2026-07-18) — more universities (mostly INCIDENTAL finds; many direct targets use PeopleAdmin/Taleo/Oracle) ---
{"display_name": "University of Miami",              "tenant": "umiami",      "wd_host": "wd1",  "site": "UMCareerStaff",             "industry": "education"}, # ~1,824 jobs, US 100%
{"display_name": "Miami University (Ohio)",          "tenant": "miamioh",     "wd_host": "wd5",  "site": "miamioh-staff",             "industry": "education"}, # ~148 jobs, US 100%
{"display_name": "Iowa State University",            "tenant": "isu",         "wd_host": "wd1",  "site": "IowaStateJobs",             "industry": "education"}, # ~69 jobs,  US 100%
{"display_name": "Northern Kentucky University",     "tenant": "nku",         "wd_host": "wd108","site": "NKU_External",              "industry": "education"}, # ~54 jobs,  US 100%
{"display_name": "Oregon State University",          "tenant": "oregonstate", "wd_host": "wd501","site": "OSU_Careers_Site",          "industry": "education"}, # ~50 jobs,  US 100%
{"display_name": "University of Northern Iowa",      "tenant": "uni",         "wd_host": "wd5",  "site": "UNI",                       "industry": "education"}, # ~28 jobs,  US 100%
{"display_name": "University of the Cumberlands",    "tenant": "ucumberlands","wd_host": "wd1",  "site": "careers",                   "industry": "education"}, # ~28 jobs,  US 100%
{"display_name": "Southern Oregon University",       "tenant": "sou",         "wd_host": "wd1",  "site": "Southern_Oregon_University", "industry": "education"}, # ~27 jobs,  US 100%
# batch 9 non-Workday (PeopleAdmin/Taleo/Oracle/SuccessFactors/PageUp): U Georgia, U Tennessee, U Kentucky, U Iowa, U Oregon, U Missouri, U Utah, U Cincinnati, U Pittsburgh, Rutgers, UConn, Virginia Tech, Georgia Tech, U Oklahoma. Willamette = Workday but gated (422).
# --- batch 10 (2026-07-18) — state/local GOVERNMENT (public sector, non-tech) ---
{"display_name": "State of Oregon",          "tenant": "oregon",          "wd_host": "wd5",  "site": "SOR_External_Career_Site", "industry": "government"}, # ~220 jobs, US 100%  (government)
{"display_name": "City of Austin",           "tenant": "austintexas",     "wd_host": "wd5",  "site": "COA_Careers",             "industry": "government"}, # ~115 jobs, US 100%  (government)
{"display_name": "Austin Community College", "tenant": "austincc",        "wd_host": "wd1",  "site": "External",                "industry": "education"}, # ~78 jobs,  US 100%  (education)
{"display_name": "Maricopa County",          "tenant": "maricopa",        "wd_host": "wd1",  "site": "MC_External",             "industry": "government"}, # ~60 jobs,  US 100%  (government)
{"display_name": "Multnomah County",         "tenant": "multco",          "wd_host": "wd1",  "site": "Multco_Jobs",             "industry": "government"}, # ~50 jobs,  US 100%  (government)
{"display_name": "Washington County",        "tenant": "wacounty",        "wd_host": "wd12", "site": "Careers",                 "industry": "government"}, # ~20 jobs,  US 100%  (government)
{"display_name": "City of Boulder",          "tenant": "bouldercolorado", "wd_host": "wd1",  "site": "External",                "industry": "government"}, # ~15 jobs,  US 100%  (government)
{"display_name": "City of Tucson",           "tenant": "tucsonaz",        "wd_host": "wd1",  "site": "Tucson_Talent",           "industry": "government"}, # ~15 jobs,  US 100%  (government)
# batch 10 non-Workday (GovernmentJobs.com/NeoGov or Taleo): State of Tennessee, State of Arizona, City of San Diego, King County, Fairfax County, Commonwealth of Massachusetts. Team Georgia (georgia.wd5/TGC) = Workday but auth-gated (401).
# --- batch 11 (2026-07-18) — mixed: government + universities + nonprofit + MEDIA ---
{"display_name": "Northeastern University",        "tenant": "northeastern",         "wd_host": "wd1",  "site": "careers",                                                               "industry": "education"}, # ~403 jobs, US 100%  (education)
{"display_name": "Louisiana State University",      "tenant": "lsu",                  "wd_host": "wd1",  "site": "LSU",                                                                   "industry": "education"}, # ~301 jobs, US 100%  (education)
{"display_name": "State of Nebraska",               "tenant": "son",                  "wd_host": "wd108","site": "NebraskaStateCareers",                                                  "industry": "government"}, # ~260 jobs, US 100%  (government)
{"display_name": "City and County of Denver",       "tenant": "denver",               "wd_host": "wd1",  "site": "CCD-denver-denvergov-CSC_Jobs-Civil_service_jobs-Police_Jobs-Fire_Jobs", "industry": "government"}, # ~97 jobs,  US 100%  (government)
{"display_name": "Wake Forest University",          "tenant": "wfu",                  "wd_host": "wd1",  "site": "Staff_Career_Website_live",                                             "industry": "education"}, # ~66 jobs,  US 100%  (education)
{"display_name": "Samuel Merritt University",       "tenant": "samuelmerritt",        "wd_host": "wd1",  "site": "SMUCareers",                                                            "industry": "education"}, # ~62 jobs,  US 100%  (education/health)
{"display_name": "The Washington Post",             "tenant": "washpost",             "wd_host": "wd5",  "site": "washingtonpostcareers",                                                 "industry": "media"},   # ~44 jobs,  US 95%   (media)
{"display_name": "City Year",                       "tenant": "cityyear",             "wd_host": "wd5",  "site": "CityYear",                                                              "industry": "nonprofit"}, # ~26 jobs,  US 100%  (nonprofit)
{"display_name": "Clark County Washington",         "tenant": "clarkcountywashington","wd_host": "wd1",  "site": "ClarkCountyJobs",                                                       "industry": "government"}, # ~25 jobs,  US 100%  (government)
{"display_name": "Northeastern Illinois University","tenant": "neiu",                 "wd_host": "wd1",  "site": "NEIU",                                                                  "industry": "education"}, # ~12 jobs,  US 100%  (education)
# batch 11 non-Workday: State of WA + City of Seattle (NeoGov), Miami-Dade (PeopleSoft), Cook County (Taleo), Baylor/Tulane (Oracle), SMU/Syracuse (Taleo/PeopleAdmin), City of Phoenix (custom). Louisiana Tech = Workday but gated (422).
# --- batch 12 (2026-07-18) — states + nonprofit + media/finance (lower yield: many privates=PeopleAdmin, states=NeoGov) ---
{"display_name": "State of North Carolina", "tenant": "nc",               "wd_host": "wd108","site": "NC_Careers",                 "industry": "government"}, # ~757 jobs, US 100%  (government — 76k employees)
{"display_name": "American Red Cross",      "tenant": "americanredcross", "wd_host": "wd1",  "site": "American_Red_Cross_Careers","industry": "nonprofit"}, # ~373 jobs, US 100%  (nonprofit)
{"display_name": "American University",      "tenant": "american",         "wd_host": "wd1",  "site": "AU",                        "industry": "education"}, # ~104 jobs, US 100%  (education)
{"display_name": "STRS Ohio",               "tenant": "strsoh",           "wd_host": "wd1",  "site": "STRS_Jobs",                 "industry": "government"}, # ~6 jobs,  US 100%  (pension fund; low volume)
{"display_name": "New England Public Media","tenant": "publicmedia",      "wd_host": "wd1",  "site": "NEPM_Careers",              "industry": "media"},   # ~1 job,   US 100%  (tiny — optional)
# batch 12 non-Workday: State of Ohio (Taleo), State of Michigan + City of Portland + City of Seattle (NeoGov), GWU/Villanova (PeopleAdmin), Fordham/DePaul (custom), Gannett (Dayforce/Ceridian), NPR (custom).
# --- batch 13 (2026-07-18) — RETAIL / APPAREL / CONSUMER (under-covered vein) ---
{"display_name": "Abercrombie & Fitch",       "tenant": "abercrombie",   "wd_host": "wd108", "site": "anf",                     "industry": "ecommerce_retail"},   # ~2,459 jobs, US 70%
{"display_name": "Academy Sports + Outdoors",  "tenant": "academy",       "wd_host": "wd1",   "site": "Careers",                 "industry": "ecommerce_retail"},   # ~791 jobs,   US 100%
{"display_name": "Advance Auto Parts",         "tenant": "advanceauto",   "wd_host": "wd5",   "site": "AdvanceExternalCareers",  "industry": "ecommerce_retail"},   # ~2,000 jobs, US 100%
{"display_name": "Belk",                       "tenant": "belk",          "wd_host": "wd1",   "site": "Jobs-Stores",             "industry": "ecommerce_retail"},   # ~595 jobs,   US 100%
{"display_name": "Carter's",                   "tenant": "carters",       "wd_host": "wd1",   "site": "CartersCareers",          "industry": "ecommerce_retail"},   # ~773 jobs,   US 75%
{"display_name": "Deckers Brands",             "tenant": "deckers",       "wd_host": "wd5",   "site": "Deckers",                 "industry": "ecommerce_retail"},   # ~237 jobs,   US 65%  (footwear — UGG/HOKA)
{"display_name": "J.Crew",                     "tenant": "jcrew",         "wd_host": "wd1",   "site": "JCrewCareers",            "industry": "ecommerce_retail"},   # ~842 jobs,   US 100%
{"display_name": "Kontoor Brands",             "tenant": "kbi",           "wd_host": "wd5",   "site": "Kontoor",                 "industry": "ecommerce_retail"},   # ~110 jobs,   US 100%  (Wrangler/Lee)
{"display_name": "Michaels",                   "tenant": "michaels",      "wd_host": "wd5",   "site": "External",                "industry": "ecommerce_retail"},   # ~2,000 jobs, US 100%
{"display_name": "Signet Jewelers",            "tenant": "signetjewelers","wd_host": "wd1",   "site": "SignetCorporateCareers",  "industry": "ecommerce_retail"},   # ~100 jobs,   US 95%   (corporate site — store roles elsewhere)
{"display_name": "Tractor Supply Company",     "tenant": "tsc",           "wd_host": "wd12",  "site": "TSC-Careers",             "industry": "ecommerce_retail"},   # ~85 jobs,    US 100%  (corporate/DC roles; store roles separate)
# batch 13 dead (HTTP 422 — gated): Kohl's (kohls.wd1/kohlscareers), Petco (petco.wd1/External), JD Finish Line (finishline.wd1/Store_Careers).
# batch 13 already in 532: Burlington Stores, Columbia Sportswear, Gap Inc., Levi Strauss, Nordstrom, VF Corporation.
# --- batch 14 (2026-07-18) — FOOD SERVICE / RESTAURANTS / HOSPITALITY / TRAVEL (non-tech; mostly 'other') ---
{"display_name": "US Foods",                   "tenant": "usfoods",       "wd_host": "wd1",   "site": "usfoodscareersExternal",  "industry": "ecommerce_retail"},   # ~635 jobs,   US 100%  (food distribution — like Sysco)
{"display_name": "Gordon Food Service",        "tenant": "gfs",           "wd_host": "wd5",   "site": "usjobs-gen-gfs",          "industry": "ecommerce_retail"},   # ~587 jobs,   US 100%  (food distribution)
{"display_name": "Panera Bread",               "tenant": "panerabread",   "wd_host": "wd5",   "site": "Panera_Careers",          "industry": "hospitality"},        # ~2,671 jobs, US 100%  (restaurant/food service)
{"display_name": "Little Caesars (Ilitch)",    "tenant": "ilitch",        "wd_host": "wd5",   "site": "LC",                      "industry": "hospitality"},        # ~992 jobs,   US 95%   (restaurant/food service)
{"display_name": "Wyndham Hotels & Resorts",   "tenant": "wynd",          "wd_host": "wd5",   "site": "External",                "industry": "hospitality"},        # ~851 jobs,   US 100%  (hospitality)
{"display_name": "Topgolf",                    "tenant": "topgolf",       "wd_host": "wd501", "site": "TopgolfCareers",          "industry": "hospitality"},        # ~746 jobs,   US 100%  (entertainment/hospitality)
{"display_name": "MGM Resorts International",   "tenant": "mgmresorts",    "wd_host": "wd5",   "site": "MGMCareers",              "industry": "hospitality"},        # ~505 jobs,   US 100%  (hospitality/casino)
{"display_name": "Towne Park",                 "tenant": "townepark",     "wd_host": "wd5",   "site": "ext",                     "industry": "hospitality"},        # ~470 jobs,   US 100%  (hospitality services — valet/parking)
{"display_name": "MasterCorp",                 "tenant": "mastercorp",    "wd_host": "wd1",   "site": "MC",                      "industry": "hospitality"},        # ~380 jobs,   US 100%  (hospitality services — housekeeping)
{"display_name": "Sabre",                      "tenant": "sabre",         "wd_host": "wd1",   "site": "SabreJobs",               "industry": "tech"},               # ~121 jobs,   US 40%   (travel technology / GDS)
{"display_name": "Alliance Ground International","tenant": "allianceground","wd_host": "wd1",  "site": "AGI_Careers",             "industry": "travel"},             # ~115 jobs,   US 90%   (aviation ground handling)
{"display_name": "Inspire Brands",             "tenant": "inspirebrands", "wd_host": "wd5",   "site": "InspireCareers",          "industry": "hospitality"},        # ~86 jobs,    US 95%   (restaurant group — Arby's/Dunkin/Sonic)
{"display_name": "Norwegian Cruise Line Holdings","tenant": "nclh",       "wd_host": "wd108", "site": "NCLH_Careers",            "industry": "travel"},             # ~18 jobs,    US 100%  (travel/cruise; shipboard roles elsewhere)
# batch 14 already in 532: Chipotle Mexican Grill, Dine Brands Global, Expedia Group, ASM Global.
# --- batch 15 (2026-07-18) — STATE / LOCAL GOVERNMENT + PUBLIC SECTOR (non-tech; all 'other'; near-infinite vein) ---
{"display_name": "State of Oklahoma",          "tenant": "okgov",         "wd_host": "wd1",   "site": "okgovjobs",               "industry": "government"}, # ~602 jobs, US 100%  (state government)
{"display_name": "State of Maine",             "tenant": "maine",         "wd_host": "wd5",   "site": "Executive",               "industry": "government"}, # ~230 jobs, US 100%  (state government)
{"display_name": "Pima County, Arizona",       "tenant": "pimacounty",    "wd_host": "wd5",   "site": "pimacareers",             "industry": "government"}, # ~138 jobs, US 100%  (county government)
{"display_name": "City of Baltimore, Maryland","tenant": "baltimorecity", "wd_host": "wd1",   "site": "External",                "industry": "government"}, # ~110 jobs, US 100%  (city government)
{"display_name": "Richland County, South Carolina","tenant": "richlandonline","wd_host": "wd5","site": "richlandcountygovcareers","industry": "government"}, # ~92 jobs,  US 100%  (county government)
{"display_name": "City of Dallas, Texas",      "tenant": "dallascityhall","wd_host": "wd5",   "site": "CODCareers",              "industry": "government"}, # ~74 jobs,  US 100%  (city government)
{"display_name": "Suffolk County, New York",   "tenant": "suffolkcountyny","wd_host": "wd1",  "site": "Suffolkcounty1",          "industry": "government"}, # ~71 jobs,  US 100%  (county government)
{"display_name": "Unified Gov. of Wyandotte County/KCK","tenant": "wycokck","wd_host": "wd1", "site": "RECExternalCareers",      "industry": "government"}, # ~66 jobs,  US 100%  (consolidated city-county)
{"display_name": "City of Charlotte, North Carolina","tenant": "charlottenc","wd_host": "wd12","site": "citgov",                 "industry": "government"}, # ~49 jobs,  US 100%  (city government)
{"display_name": "City of Aurora, Colorado",   "tenant": "auroragov",     "wd_host": "wd1",   "site": "Careers",                 "industry": "government"}, # ~47 jobs,  US 100%  (city government)
{"display_name": "Nueces County, Texas",       "tenant": "nuecesco",      "wd_host": "wd5",   "site": "External",                "industry": "government"}, # ~38 jobs,  US 100%  (county government)
{"display_name": "Oakland County, Michigan",   "tenant": "oakgov",        "wd_host": "wd5",   "site": "careers",                 "industry": "government"}, # ~34 jobs,  US 100%  (county government)
{"display_name": "City of Galveston, Texas",   "tenant": "cityofgalveston","wd_host": "wd12", "site": "CityofGalvestonTexasCareers","industry": "government"},# ~27 jobs,  US 100%  (city government)
{"display_name": "Utah County, Utah",          "tenant": "utahcounty",    "wd_host": "wd1",   "site": "Utah_County_Careers",     "industry": "government"}, # ~27 jobs,  US 100%  (county government)
{"display_name": "Buncombe County, North Carolina","tenant": "buncombecounty","wd_host": "wd1","site": "Buncombe_County_Careers","industry": "government"}, # ~22 jobs,  US 100%  (county government)
{"display_name": "Calvert County, Maryland",   "tenant": "calvertcountymd","wd_host": "wd1",  "site": "calvertcountymdemploymentopportunities","industry": "government"}, # ~20 jobs, US 100%  (county government)
{"display_name": "City of Vancouver, Washington","tenant": "cityofvancouver","wd_host": "wd5","site": "COV",                     "industry": "government"}, # ~19 jobs,  US 100%  (city government)
{"display_name": "City of Gainesville, Florida","tenant": "cityofgainesville","wd_host": "wd5","site": "Careers",                "industry": "government"}, # ~14 jobs,  US 100%  (city government)
{"display_name": "Anoka County, Minnesota",    "tenant": "anokacounty",   "wd_host": "wd1",   "site": "Anoka_County_Career_Opportunities","industry": "government"}, # ~10 jobs, US 100%  (county government)
{"display_name": "Scott County, Minnesota",    "tenant": "scottcountymn", "wd_host": "wd12",  "site": "Careers",                 "industry": "government"}, # ~7 jobs,   US 100%  (county government)
{"display_name": "City of Sarasota, Florida",  "tenant": "sarasotagov",   "wd_host": "wd1",   "site": "Careers",                 "industry": "government"}, # ~5 jobs,   US 100%  (city government)
# --- batch 15b (2026-07-18) — public-sector: school districts / transit / airport authorities (non-tech; 'other') ---
{"display_name": "Douglas County School District (CO)","tenant": "dcsd",  "wd_host": "wd5",   "site": "DCSD",                    "industry": "education"}, # ~287 jobs, US 100%  (K-12 school district)
{"display_name": "Cleveland Metropolitan School District","tenant": "clevelandmetroschools","wd_host": "wd1","site": "jobs",     "industry": "education"}, # ~52 jobs,  US 100%  (K-12 school district)
{"display_name": "Metropolitan Washington Airports Authority","tenant": "mwaa","wd_host": "wd1","site": "MWAA",                  "industry": "government"}, # ~25 jobs,  US 100%  (airport authority — DCA/IAD)
{"display_name": "Regional Transportation District (RTD Denver)","tenant": "rtddenver","wd_host": "wd5","site": "Careers",       "industry": "government"}, # ~12 jobs,  US 100%  (public transit)
{"display_name": "Ontario International Airport Authority (CA)","tenant": "ont","wd_host": "wd5","site": "External",              "industry": "government"}, # ~2 jobs,   US 100%  (airport authority; low volume — optional)
# batch 15 dead (HTTP 422 / empty): Clay County Utility Authority FL (clayutility.wd5), Great Lakes Water Authority (glwater.wd1), Santa Cruz Metro Transit (scmtd.wd12 — no postings).
# batch 15 already in 532: (none — all counties/cities net-new).
# --- batch 16 (2026-07-18) — CPG / FOOD / BEVERAGE (under-covered vein) ---
{"display_name": "Smithfield Foods",           "tenant": "smithfieldfoods","wd_host": "wd1",  "site": "Careers",                "industry": "ecommerce_retail"},   # ~476 jobs, US 100%  (pork/protein)
{"display_name": "Saputo",                     "tenant": "saputo",        "wd_host": "wd5",   "site": "Saputo_External_Careers","industry": "ecommerce_retail"},   # ~300 jobs, US 65%   (dairy — Canada-heavy)
{"display_name": "International Flavors & Fragrances","tenant": "iff",     "wd_host": "wd5",   "site": "IFF_Careers",            "industry": "ecommerce_retail"},   # ~374 jobs, US 25%   (ingredients — global-heavy, ⚠ low US%)
{"display_name": "Land O'Lakes",               "tenant": "landolakes",    "wd_host": "wd1",   "site": "LandOLakes",             "industry": "ecommerce_retail"},   # ~158 jobs, US 100%  (dairy/ag co-op)
{"display_name": "TreeHouse Foods",            "tenant": "treehouse",     "wd_host": "wd1",   "site": "TreeHouseCareers",       "industry": "ecommerce_retail"},   # ~159 jobs, US 100%  (private-label foods)
{"display_name": "Ingredion",                  "tenant": "ingredion",     "wd_host": "wd1",   "site": "IngredionCareers",       "industry": "ecommerce_retail"},   # ~169 jobs, US 75%   (food ingredients)
{"display_name": "Agropur",                    "tenant": "agropur",       "wd_host": "wd3",   "site": "Agropur_Careers",        "industry": "ecommerce_retail"},   # ~140 jobs, US 100%  (dairy co-op)
{"display_name": "Lamb Weston",                "tenant": "lambweston",    "wd_host": "wd1",   "site": "Lamb_External",          "industry": "ecommerce_retail"},   # ~111 jobs, US 100%  (frozen potato)
{"display_name": "Church & Dwight",            "tenant": "churchdwight",  "wd_host": "wd1",   "site": "chdcareers",             "industry": "ecommerce_retail"},   # ~75 jobs,  US 90%   (CPG household/personal-care)
{"display_name": "Constellation Brands",       "tenant": "cbrands",       "wd_host": "wd5",   "site": "CBI_External_Careers",   "industry": "ecommerce_retail"},   # ~75 jobs,  US 85%   (beer/wine/spirits)
{"display_name": "Ocean Spray",                "tenant": "oceanspray",    "wd_host": "wd5",   "site": "OceanSprayJobs",         "industry": "ecommerce_retail"},   # ~56 jobs,  US 95%   (beverage co-op)
{"display_name": "B&G Foods",                  "tenant": "bgfoods",       "wd_host": "wd1",   "site": "BG_Foods_Careers",       "industry": "ecommerce_retail"},   # ~50 jobs,  US 100%  (packaged foods)
{"display_name": "Simmons Foods",              "tenant": "simmons",       "wd_host": "wd1",   "site": "Simmons-Careers",        "industry": "ecommerce_retail"},   # ~50 jobs,  US 100%  (poultry/pet food)
{"display_name": "Nutrabolt",                  "tenant": "nutrabolt",     "wd_host": "wd108", "site": "Nutrabolt_Careers",      "industry": "ecommerce_retail"},   # ~34 jobs,  US 100%  (nutrition/supplements — C4)
{"display_name": "Brown-Forman",               "tenant": "bf",            "wd_host": "wd5",   "site": "USA_Canada",             "industry": "ecommerce_retail"},   # ~21 jobs,  US 95%   (spirits — Jack Daniel's)
{"display_name": "Blue Diamond Growers",       "tenant": "bdgrowers",     "wd_host": "wd1",   "site": "External",               "industry": "ecommerce_retail"},   # ~11 jobs,  US 100%  (almond co-op)
# batch 16 dead (HTTP 422 / empty): Schreiber Foods (schreiberfoods.wd5), Del Monte Foods (delmontefoods.wd5 — no postings; March 2026 asset sale).
# batch 16 already in 532: Conagra Brands, Mondelez International.
# --- batch 17 (2026-07-18) — UNIVERSITIES / COLLEGES (education, non-tech; 'other') ---
{"display_name": "Metropolitan State University of Denver","tenant": "msudenver","wd_host": "wd1","site": "MSUDenver",           "industry": "education"}, # ~118 jobs, US 100%
{"display_name": "Loyola Marymount University", "tenant": "lmu",          "wd_host": "wd1",   "site": "Careers",                "industry": "education"}, # ~116 jobs, US 100%
{"display_name": "Saint Louis University",      "tenant": "slu",          "wd_host": "wd5",   "site": "Careers",                "industry": "education"}, # ~85 jobs,  US 100%
{"display_name": "Santa Clara University",      "tenant": "scu",          "wd_host": "wd1",   "site": "scu",                    "industry": "education"}, # ~79 jobs,  US 100%
{"display_name": "Brandeis University",         "tenant": "brandeis",     "wd_host": "wd5",   "site": "Jobs",                   "industry": "education"}, # ~75 jobs,  US 100%
{"display_name": "University of Richmond",       "tenant": "richmond",     "wd_host": "wd5",   "site": "staff_faculty",          "industry": "education"}, # ~71 jobs,  US 100%
{"display_name": "Loyola University Maryland",  "tenant": "loyola",       "wd_host": "wd5",   "site": "External",               "industry": "education"}, # ~66 jobs,  US 100%
{"display_name": "Rochester Institute of Technology","tenant": "rit",     "wd_host": "wd12",  "site": "careers",                "industry": "education"}, # ~55 jobs,  US 100%
{"display_name": "Point Loma Nazarene University","tenant": "pointloma",  "wd_host": "wd1",   "site": "PLNUCareers",            "industry": "education"}, # ~53 jobs,  US 100%
{"display_name": "Marymount University",        "tenant": "marymount",    "wd_host": "wd5",   "site": "Careers",                "industry": "education"}, # ~47 jobs,  US 100%
{"display_name": "Stevens Institute of Technology","tenant": "stevens",   "wd_host": "wd5",   "site": "External",               "industry": "education"}, # ~44 jobs,  US 100%
{"display_name": "Fairfield University",        "tenant": "ffd",          "wd_host": "wd1",   "site": "EmploymentOpportunities","industry": "education"}, # ~40 jobs,  US 100%
{"display_name": "University of San Francisco", "tenant": "usfca",        "wd_host": "wd5",   "site": "USF_Staff",              "industry": "education"}, # ~39 jobs,  US 100%
{"display_name": "Xavier University",           "tenant": "xavier",       "wd_host": "wd108", "site": "XavierCareers",          "industry": "education"}, # ~38 jobs,  US 100%
{"display_name": "Emerson College",             "tenant": "emerson",      "wd_host": "wd5",   "site": "Emerson_College_Staff",  "industry": "education"}, # ~33 jobs,  US 100%
{"display_name": "Seattle Pacific University",   "tenant": "spu",          "wd_host": "wd12",  "site": "SPU",                    "industry": "education"}, # ~31 jobs,  US 100%
{"display_name": "College of the Holy Cross",   "tenant": "holycross",    "wd_host": "wd12",  "site": "Careers",                "industry": "education"}, # ~30 jobs,  US 100%
{"display_name": "Smith College",               "tenant": "smithcollege", "wd_host": "wd5",   "site": "smithcollege",           "industry": "education"}, # ~30 jobs,  US 100%
{"display_name": "Trinity University",          "tenant": "trinity",      "wd_host": "wd1",   "site": "Trinity_University",     "industry": "education"}, # ~26 jobs,  US 100%  (San Antonio, TX)
{"display_name": "Bucknell University",         "tenant": "bucknell",     "wd_host": "wd1",   "site": "External",               "industry": "education"}, # ~21 jobs,  US 100%
{"display_name": "Wellesley College",           "tenant": "wellesley",    "wd_host": "wd1",   "site": "wellesley-staff",        "industry": "education"}, # ~18 jobs,  US 100%
{"display_name": "Seattle University",          "tenant": "seattleu",     "wd_host": "wd108", "site": "SeattleUJobs",           "industry": "education"}, # ~14 jobs,  US 100%
{"display_name": "DePauw University",           "tenant": "depauw",       "wd_host": "wd5",   "site": "DePauw_University",       "industry": "education"}, # ~13 jobs,  US 100%
# batch 17 dead (HTTP 422 / empty): University of Virginia (uva.wd1/UVAJobs — still empty, academic on Phenom — confirms batch 7), Willamette University (willamette.wd1 — 422, confirms batch 9), Louisiana Tech (ulsltu.wd1 — 422, confirms batch 11).
# batch 17 already in 532: University of Rochester, Vanderbilt University Medical Center (VUMC).
# --- batch 18 (2026-07-18) — COMMUNITY COLLEGES / SCHOOL DISTRICTS / TRANSIT / AIRPORT / WATER / PORT (public sector; 'other') ---
{"display_name": "Columbus State Community College","tenant": "cscc",     "wd_host": "wd1",   "site": "CSCC_ext",               "industry": "education"}, # ~123 jobs, US 100%  (community college)
{"display_name": "Howard County Public School System","tenant": "hcpss",  "wd_host": "wd1",   "site": "hcpss",                  "industry": "education"}, # ~98 jobs,  US 100%  (K-12 school district)
{"display_name": "Dallas College",              "tenant": "dallascollege","wd_host": "wd1",   "site": "Dallas_College_Careers", "industry": "education"}, # ~86 jobs,  US 100%  (community college)
{"display_name": "Grossmont-Cuyamaca Community College District","tenant": "gcccd","wd_host": "wd1","site": "gcccdcareers",      "industry": "education"}, # ~86 jobs,  US 100%  (community college)
{"display_name": "NorthWest Arkansas Community College","tenant": "nwacc","wd_host": "wd1",   "site": "NWACC_External_Career_Site","industry": "education"},# ~54 jobs, US 100%  (community college)
{"display_name": "Montgomery College",          "tenant": "mc",           "wd_host": "wd1",   "site": "external",               "industry": "education"}, # ~49 jobs,  US 100%  (community college, MD)
{"display_name": "Hampton Roads Transit",       "tenant": "gohrt",        "wd_host": "wd12",  "site": "HRT",                    "industry": "government"}, # ~46 jobs,  US 100%  (public transit, VA)
{"display_name": "Davidson County, North Carolina","tenant": "davidsoncountync","wd_host": "wd501","site": "Davidson_County",   "industry": "government"}, # ~37 jobs,  US 100%  (county government)
{"display_name": "Collin College",              "tenant": "collin",       "wd_host": "wd1",   "site": "ExternalStaffCareerSite","industry": "education"}, # ~26 jobs,  US 100%  (community college, TX)
{"display_name": "Port Houston",                "tenant": "porthouston",  "wd_host": "wd5",   "site": "External_Careers",       "industry": "government"}, # ~18 jobs,  US 100%  (port authority)
{"display_name": "Columbus Regional Airport Authority","tenant": "columbusairports","wd_host": "wd1","site": "Columbus_Airports1","industry": "government"}, # ~10 jobs,  US 100%  (airport authority)
{"display_name": "Las Vegas Valley Water District","tenant": "lvvwd",     "wd_host": "wd1",   "site": "LVVWDWaterJobs",         "industry": "government"}, # ~9 jobs,   US 100%  (public water utility)
{"display_name": "Ben Franklin Transit",        "tenant": "bft",          "wd_host": "wd501", "site": "Ben_Franklin_Transit_External_Careers","industry": "government"},# ~7 jobs, US 100%  (public transit, WA)
{"display_name": "Tarrant Regional Water District","tenant": "trwd",      "wd_host": "wd1",   "site": "TRWDCareers",            "industry": "government"}, # ~7 jobs,   US 100%  (public water utility, TX)
{"display_name": "Memphis-Shelby County Airport Authority","tenant": "flymemphis","wd_host": "wd5","site": "MSCAAExternalCareerSite","industry": "government"},# ~4 jobs, US 75%  (airport authority; low volume)
# batch 18 dead (HTTP 422): Hillsborough Community College (hccfl.wd1), Minnesota State system (minnstate.wd1), Monroe County FL (monroecountyfl.wd1), City of Arlington TX (arlingtontx.wd1), City of Port Orange FL (portorange.wd5).
# --- batch 19 (2026-07-18) — HEALTHCARE SYSTEMS / SENIOR-CARE / BEHAVIORAL / HEALTH INSURERS ---
{"display_name": "Advocate Health",             "tenant": "aah",          "wd_host": "wd5",   "site": "External",               "industry": "healthtech"},   # ~2,000 jobs, US 100%  (hospital system)
{"display_name": "Sunrise Senior Living",       "tenant": "sunriseseniorliving","wd_host": "wd12","site": "SUNRISE_EXT_CAREERS","industry": "healthtech"},   # ~1,903 jobs, US 100%  (senior living/LTC)
{"display_name": "Erickson Senior Living",      "tenant": "erickson",     "wd_host": "wd108", "site": "External",               "industry": "healthtech"},   # ~487 jobs, US 100%  (senior living/LTC)
{"display_name": "GoHealth Urgent Care",        "tenant": "gohealthuc",   "wd_host": "wd12",  "site": "External",               "industry": "healthtech"},   # ~422 jobs, US 100%  (urgent care)
{"display_name": "Genesis HealthCare System",   "tenant": "genesishcs",   "wd_host": "wd1",   "site": "GenesisHCS",             "industry": "healthtech"},   # ~292 jobs, US 100%  (hospital system, OH)
{"display_name": "MBK Senior Living",           "tenant": "mbk",          "wd_host": "wd5",   "site": "MSLCareers",             "industry": "healthtech"},   # ~247 jobs, US 100%  (senior living/LTC)
{"display_name": "Rogers Behavioral Health",    "tenant": "rogersbh",     "wd_host": "wd1",   "site": "RBHCareer",              "industry": "healthtech"},   # ~206 jobs, US 100%  (behavioral health)
{"display_name": "MarinHealth (Marin Healthcare District)","tenant": "mymarinhealth","wd_host": "wd5","site": "MHCareers",       "industry": "healthtech"},   # ~148 jobs, US 100%  (public hospital district)
{"display_name": "Mental Health Cooperative",   "tenant": "mhctn",        "wd_host": "wd12",  "site": "mhc_careers",            "industry": "healthtech"},   # ~72 jobs,  US 100%  (behavioral health, TN)
{"display_name": "Vibrant Emotional Health",    "tenant": "vibrant",      "wd_host": "wd5",   "site": "VEH_EXTERNAL_CAREER_SITE","industry": "healthtech"},   # ~13 jobs,  US 100%  (988 crisis-line nonprofit)
{"display_name": "Centene",                     "tenant": "centene",      "wd_host": "wd5",   "site": "Centene_External",       "industry": "insurance"},   # ~120 jobs, US 100%  (managed care)
{"display_name": "Blue Cross Blue Shield of Arizona","tenant": "bcbsaz",  "wd_host": "wd1",   "site": "BCBSAZCareers",          "industry": "insurance"},   # ~52 jobs,  US 100%
{"display_name": "Blue Cross Blue Shield of Massachusetts","tenant": "bcbsma","wd_host": "wd5","site": "BCBSMA",                 "industry": "insurance"},   # ~41 jobs,  US 100%
{"display_name": "Blue Cross and Blue Shield of North Carolina","tenant": "bcbsnc","wd_host": "wd5","site": "BCBSNC",            "industry": "insurance"},   # ~37 jobs,  US 100%
{"display_name": "Blue Cross Blue Shield Association","tenant": "bcbsa",   "wd_host": "wd1",   "site": "Careers",                "industry": "insurance"},   # ~19 jobs,  US 100%  (national association)
{"display_name": "Blue Cross Blue Shield of Nebraska","tenant": "nebraskablue","wd_host": "wd1","site": "BCBSNE",                "industry": "insurance"},   # ~14 jobs,  US 100%
# batch 19 dead (HTTP 422): Memorial Sloan Kettering (msk.wd1), BlueCross BlueShield of South Carolina (ourhrconnect.wd5).
# batch 19 already in 532: Cleveland Clinic, Sutter Health, Trinity Health, Bon Secours Mercy Health, Methodist Health System, Kaweah Health, CVS Health, Humana.
# --- batch 20 (2026-07-18) — NONPROFITS / FOUNDATIONS / RESEARCH (non-tech; 'other') ---
{"display_name": "YMCA of Metropolitan Atlanta","tenant": "ymcaatlanta",  "wd_host": "wd1",   "site": "YMCA-Careers",           "industry": "nonprofit"}, # ~381 jobs, US 100%  (nonprofit)
{"display_name": "Youth Villages",              "tenant": "youthvillages","wd_host": "wd1",   "site": "youthvillagescareers",   "industry": "nonprofit"}, # ~162 jobs, US 100%  (child/family services nonprofit)
{"display_name": "Easterseals Southern California","tenant": "essc",      "wd_host": "wd501", "site": "ESSC_Careers",           "industry": "nonprofit"}, # ~146 jobs, US 100%  (disability services nonprofit)
{"display_name": "Howard Hughes Medical Institute","tenant": "hhmi",      "wd_host": "wd1",   "site": "External",               "industry": "nonprofit"}, # ~40 jobs,  US 100%  (research nonprofit)
{"display_name": "Gates Foundation",            "tenant": "gatesfoundation","wd_host": "wd1", "site": "Gates",                  "industry": "nonprofit"}, # ~20 jobs,  US 90%   (foundation)
{"display_name": "RAND Corporation",            "tenant": "rand",         "wd_host": "wd5",   "site": "External_Career_Site",   "industry": "nonprofit"}, # ~9 jobs,   US 100%  (research/think tank)
{"display_name": "American Heart Association",   "tenant": "aha",          "wd_host": "wd501", "site": "AHA",                    "industry": "nonprofit"}, # ~4 jobs,   US 100%  (nonprofit; low volume)
# batch 20 already in 532: Goodwill of Central & Southern Indiana.
# --- batch 21 (2026-07-18) — ENERGY / UTILITIES / OIL & GAS / RENEWABLES ---
{"display_name": "Sunrun",              "tenant": "sunrun",       "wd_host": "wd5",  "site": "Sunrun_Careers",     "industry": "energy"},   # ~361 jobs, US 100%  (residential solar)
{"display_name": "Bloom Energy",        "tenant": "bloomenergy",  "wd_host": "wd1",  "site": "BloomEnergyCareers", "industry": "energy"},   # ~237 jobs, US 60%   (fuel cells)
{"display_name": "Vistra",              "tenant": "vst",          "wd_host": "wd5",  "site": "vistra_careers",     "industry": "energy"},   # ~187 jobs, US 100%  (power generation/retail)
{"display_name": "ChampionX",           "tenant": "championx",    "wd_host": "wd1",  "site": "ChampionX_External", "industry": "energy"},   # ~123 jobs, US 100%  (oilfield services)
{"display_name": "Fluence Energy",      "tenant": "fluenceenergy","wd_host": "wd12", "site": "fluenceenergy-jobs", "industry": "energy"},   # ~104 jobs, US 55%   (energy storage)
{"display_name": "AES Corporation",     "tenant": "aes",          "wd_host": "wd1",  "site": "AES_US",             "industry": "energy"},   # ~99 jobs,  US 100%  (power)
{"display_name": "Alliant Energy",      "tenant": "alliantenergy","wd_host": "wd1",  "site": "alliant",            "industry": "energy"},   # ~54 jobs,  US 100%  (utility)
{"display_name": "Talen Energy",        "tenant": "talenenergy",  "wd_host": "wd1",  "site": "TalenCareers",       "industry": "energy"},   # ~42 jobs,  US 100%  (power generation)
{"display_name": "Array Technologies",  "tenant": "arraytechinc", "wd_host": "wd5",  "site": "Array_Careers",      "industry": "energy"},   # ~40 jobs,  US 75%   (solar trackers)
{"display_name": "Devon Energy",        "tenant": "devonenergy",  "wd_host": "wd5",  "site": "careers",            "industry": "energy"},   # ~23 jobs,  US 100%  (oil & gas)
{"display_name": "Ovintiv",             "tenant": "ovintiv",      "wd_host": "wd3",  "site": "ovintivcareers",     "industry": "energy"},   # ~1 job,    US 100%  (oil & gas; low volume — optional)
# batch 21 dead (HTTP 422): First Solar (firstsolar.wd1). Non-US: Air Products (airproducts.wd5/AP0001 — US 0%).
# batch 21 already in 532: Ameren, Avangrid, Baker Hughes, Chevron, Eversource, Invenergy, Marathon Petroleum, Portland General Electric, Williams Companies.
# --- batch 22 (2026-07-18) — INDUSTRIALS / MANUFACTURING / AEROSPACE-DEFENSE / PACKAGING ---
{"display_name": "Johnson Controls",    "tenant": "jci",          "wd_host": "wd5",  "site": "JCI",                "industry": "manufacturing_automotive"},   # ~2,681 jobs, US 85%  (building tech/HVAC)
{"display_name": "Trane Technologies",  "tenant": "tranetechnologies","wd_host": "wd12","site": "Trane_Technologies_Careers","industry": "manufacturing_automotive"},# ~1,452 jobs, US 85%  (HVAC)
{"display_name": "Regal Rexnord",       "tenant": "regalrexnord", "wd_host": "wd1",  "site": "Careers",            "industry": "manufacturing_automotive"},   # ~506 jobs, US 80%   (motors/power-transmission)
{"display_name": "Oshkosh Corporation", "tenant": "oshkoshcorporation","wd_host": "wd5","site": "Oshkosh",         "industry": "manufacturing_automotive"},   # ~427 jobs, US 85%   (specialty vehicles)
{"display_name": "Flowserve",           "tenant": "flowserve",    "wd_host": "wd1",  "site": "applied",            "industry": "manufacturing_automotive"},   # ~373 jobs, US 45%   (pumps/valves — ⚠ low US%)
{"display_name": "Westlake",            "tenant": "westlake",     "wd_host": "wd1",  "site": "westlake",           "industry": "manufacturing_automotive"},   # ~346 jobs, US 100%  (chemicals/building products)
{"display_name": "Curtiss-Wright",      "tenant": "curtisswright","wd_host": "wd1",  "site": "CW_External_Career_Site","industry": "aerospace_defense"},       # ~331 jobs, US 85%   (aerospace/defense)
{"display_name": "Crown Holdings",      "tenant": "crownholdings","wd_host": "wd501","site": "CrownHoldings",      "industry": "manufacturing_automotive"},   # ~262 jobs, US 100%  (metal packaging)
{"display_name": "Nordson",             "tenant": "nordsonhcm",   "wd_host": "wd501","site": "nordsoncareers",     "industry": "manufacturing_automotive"},   # ~222 jobs, US 100%  (precision dispensing)
{"display_name": "Sonoco",              "tenant": "sonoco",       "wd_host": "wd1",  "site": "CorporateCareers",   "industry": "manufacturing_automotive"},   # ~219 jobs, US 90%   (packaging)
{"display_name": "Woodward",            "tenant": "woodward",     "wd_host": "wd5",  "site": "woodward",           "industry": "aerospace_defense"},           # ~188 jobs, US 90%   (aerospace/industrial controls)
{"display_name": "Veritiv",             "tenant": "veritiv",      "wd_host": "wd5",  "site": "VeritivCareers",     "industry": "manufacturing_automotive"},   # ~158 jobs, US 100%  (packaging distribution)
{"display_name": "Allison Transmission","tenant": "allisontransmission","wd_host": "wd1","site": "ATI-External",   "industry": "manufacturing_automotive"},   # ~72 jobs,  US 100%  (transmissions)
# batch 22 dead (HTTP 4xx): Masonite (masonite.wd1 — 422), Wabtec (wabtec.wd1 — 403).
# batch 22 already in 532: BorgWarner, Carrier Global, Leidos, Ryder System.
# --- batch 23 (2026-07-18) — FINANCIAL SERVICES / INSURANCE / REAL ESTATE ---
{"display_name": "Cushman & Wakefield", "tenant": "cw",           "wd_host": "wd1",  "site": "External",           "industry": "real_estate"},  # ~2,000 jobs, US 75%  (CRE services — no real-estate key → 'other')
{"display_name": "Greystar",            "tenant": "greystar",     "wd_host": "wd1",  "site": "External",           "industry": "real_estate"},  # ~1,556 jobs, US 100% (property management — 'other')
{"display_name": "Franklin Templeton",  "tenant": "franklintempleton","wd_host": "wd5","site": "Primary-External-1","industry": "banking"},     # ~150 jobs, US 60%   (asset management)
{"display_name": "Voya Financial",      "tenant": "godirect",     "wd_host": "wd5",  "site": "voya_jobs",          "industry": "insurance"},    # ~96 jobs,  US 100%  (retirement/benefits)
{"display_name": "Prologis",            "tenant": "prologis",     "wd_host": "wd5",  "site": "Prologis_External_Careers","industry": "real_estate"}, # ~82 jobs,  US 75%   (industrial REIT — 'other')
{"display_name": "PSECU",               "tenant": "psecu",        "wd_host": "wd12", "site": "PSECU",              "industry": "banking"},      # ~36 jobs,  US 100%  (credit union)
# batch 23 dead (HTTP 422): Invitation Homes (invitationhomes.wd1), Global Payments (globalpay.wd1).
# batch 23 already in 532: American Family Insurance, BECU, Fifth Third Bank, FIS, Fiserv, KeyBank, Nationwide, Prudential Financial, Raymond James, Regions Bank, Rocket Companies, Truist, Unum Group, VyStar Credit Union, Worldpay.
# --- batch 24 (2026-07-18) — UNIVERSITIES / GOVERNMENT (non-tech; 'other') ---
{"display_name": "Yale University",     "tenant": "yale",         "wd_host": "wd1",  "site": "temporary_career_site","industry": "education"}, # ~188 jobs, US 100%  (education — RESOLVED; batch 4 had site path unknown)
{"display_name": "Howard University",   "tenant": "howard",       "wd_host": "wd1",  "site": "HU",                 "industry": "education"},  # ~147 jobs, US 100%  (education — HBCU)
{"display_name": "City of High Point, NC","tenant": "highpointnc","wd_host": "wd12", "site": "HighPointNCCareers", "industry": "government"}, # ~30 jobs,  US 100%  (city government)
{"display_name": "Guilford College",    "tenant": "guilford",     "wd_host": "wd1",  "site": "Guilford_Careers",   "industry": "education"},  # ~28 jobs,  US 100%  (education)
{"display_name": "Pennsylvania Housing Finance Agency","tenant": "phfa","wd_host": "wd5","site": "PHFA",           "industry": "government"}, # ~22 jobs,  US 100%  (state housing-finance agency)
# batch 24 dead/gated: State of Georgia / Team Georgia (georgia.wd5/TGC — 401 auth-gated, re-confirms batch 10), Housing Authority of Atlanta (vhr-atlantahousing.wd1 — 422), DSST Public Schools (dsstpublicschools.wd5 — 422).
# batch 24 already in 532: Boston Medical Center, Tufts Medicine.
# --- batch 25 (2026-07-18, session 2) — HEALTHCARE / EDUCATION / REAL-ESTATE / INSURANCE / etc. (validated + already tagged with the NEW §0 keys) ---
# healthtech — hospital & health systems, dialysis, home-health, physician groups
{"display_name": "Corewell Health",              "tenant": "spectrumhealth",       "wd_host": "wd5",  "site": "CorewellHealthCareers", "industry": "healthtech"},   # ~1,800 jobs, US 100%  (MI health system; tenant still 'spectrumhealth')
{"display_name": "UMass Memorial Health",        "tenant": "ummh",                 "wd_host": "wd1",  "site": "Careers",              "industry": "healthtech"},   # ~923 jobs, US 100%
{"display_name": "Essentia Health",              "tenant": "essentiahealth",       "wd_host": "wd1",  "site": "Essentia_Health",      "industry": "healthtech"},   # ~777 jobs, US 100%
{"display_name": "Carilion Clinic",              "tenant": "carilionclinic",       "wd_host": "wd12", "site": "External_Careers",      "industry": "healthtech"},   # ~730 jobs, US 100%  (VA health system)
{"display_name": "Capital Health",               "tenant": "capitalhealth",        "wd_host": "wd1",  "site": "CapitalHealthCareers", "industry": "healthtech"},   # ~561 jobs, US 100%  (NJ)
{"display_name": "Freeman Health System",        "tenant": "freemanhealth",        "wd_host": "wd1",  "site": "jointeamfreeman",      "industry": "healthtech"},   # ~503 jobs, US 100%  (MO)
{"display_name": "Fresenius Medical Care",       "tenant": "freseniusmedicalcare", "wd_host": "wd3",  "site": "fme",                  "industry": "healthtech"},   # ~2,000 jobs, US 95%  (dialysis)
{"display_name": "Millennium Physician Group",   "tenant": "verawholehealth",      "wd_host": "wd1",  "site": "MPG",                  "industry": "healthtech"},   # ~222 jobs, US 100%  (FL physician group)
{"display_name": "Presbyterian Healthcare Services","tenant": "phsorg",            "wd_host": "wd1",  "site": "Providers",            "industry": "healthtech"},   # ~107 jobs, US 100%  (NM)
{"display_name": "University of Minnesota Physicians","tenant": "umphysicians",     "wd_host": "wd1",  "site": "umpcareers",           "industry": "healthtech"},   # ~98 jobs,  US 100%  (academic physician group)
{"display_name": "Alpine Physicians",            "tenant": "alpinephysicians",     "wd_host": "wd1",  "site": "External",             "industry": "healthtech"},   # ~81 jobs,  US 100%
{"display_name": "The Vancouver Clinic",         "tenant": "tvc",                  "wd_host": "wd1",  "site": "TVC",                  "industry": "healthtech"},   # ~45 jobs,  US 100%  (WA)
{"display_name": "John Muir Health",             "tenant": "jmh",                  "wd_host": "wd5",  "site": "JohnMuirHealthCareers","industry": "healthtech"},   # ~43 jobs,  US 100%  (CA)
{"display_name": "Pennant Group",                "tenant": "pennant",              "wd_host": "wd1",  "site": "SequoiaHHHCareerSite", "industry": "healthtech"},   # ~31 jobs,  US 100%  (home health/hospice)
{"display_name": "Graham Healthcare Group",      "tenant": "ghc",                  "wd_host": "wd1",  "site": "GHG_Careers",          "industry": "healthtech"},   # ~17 jobs,  US 100%  (home health)
# insurance — health insurers (Blue plans)
{"display_name": "BlueCross BlueShield of South Carolina","tenant": "ourhrconnect","wd_host": "wd501","site": "SCBlues",              "industry": "insurance"},    # ~87 jobs,  US 100%  (RESOLVED — batch 19 had wrong tenant)
{"display_name": "Horizon Blue Cross Blue Shield of New Jersey","tenant": "bcbsnj", "wd_host": "wd5",  "site": "hc",                  "industry": "insurance"},    # ~50 jobs,  US 100%
{"display_name": "Blue Cross Blue Shield of Wyoming","tenant": "bcbswy",            "wd_host": "wd1",  "site": "careers",              "industry": "insurance"},    # ~11 jobs,  US 100%
{"display_name": "Blue Cross Blue Shield of Kansas City","tenant": "bcbskc",        "wd_host": "wd1",  "site": "BCBS_External_Career_Site","industry": "insurance"},# ~3 jobs,  US 100%  (low volume)
# education — universities / community colleges / online K-12
{"display_name": "Universities of Wisconsin",    "tenant": "wisconsin",            "wd_host": "wd1",  "site": "UW_Comprehensives",    "industry": "education"},    # ~356 jobs, US 100%  (⚠ shares tenant 'wisconsin' with the UW-Madison row (site UW_Madison) — this is the regional-campus site)
{"display_name": "University System of New Hampshire","tenant": "usnh",            "wd_host": "wd5",  "site": "Careers",              "industry": "education"},    # ~251 jobs, US 100%
{"display_name": "National University",          "tenant": "nus",                  "wd_host": "wd1",  "site": "Careers",              "industry": "education"},    # ~54 jobs,  US 100%  (private nonprofit university)
{"display_name": "Northampton Community College","tenant": "northampton",          "wd_host": "wd5",  "site": "NCC",                  "industry": "education"},    # ~104 jobs, US 100%  (PA)
{"display_name": "Metropolitan Community College (Nebraska)","tenant": "mccneb",    "wd_host": "wd5",  "site": "mccnebjobs",           "industry": "education"},    # ~89 jobs,  US 100%
{"display_name": "Aims Community College",       "tenant": "aims",                 "wd_host": "wd1",  "site": "Jobs",                 "industry": "education"},    # ~51 jobs,  US 100%  (CO)
{"display_name": "Clovis Community College",     "tenant": "chess",                "wd_host": "wd1",  "site": "CCC",                  "industry": "education"},    # ~51 jobs,  US 100%  (CA; tenant 'chess' = State Center CCD)
{"display_name": "Foundation for California Community Colleges","tenant": "foundationccc","wd_host": "wd1","site": "fccc-careers",     "industry": "education"},    # ~19 jobs,  US 100%  (CC-system nonprofit; education-sector)
{"display_name": "Florida Virtual School",       "tenant": "flvs",                 "wd_host": "wd1",  "site": "FLVS_Jobs",            "industry": "education"},    # ~20 jobs,  US 100%  (public online K-12)
# real_estate — REITs / CRE services / homebuilders
{"display_name": "AvalonBay Communities",        "tenant": "avalonbay",            "wd_host": "wd5",  "site": "AVBExternal",          "industry": "real_estate"},  # ~131 jobs, US 100%  (residential REIT)
{"display_name": "Colliers",                     "tenant": "colliers",             "wd_host": "wd3",  "site": "Colliers-External-Career-Site","industry": "real_estate"},# ~231 jobs, US 85%  (CRE services)
{"display_name": "Lennar",                       "tenant": "lennar",               "wd_host": "wd1",  "site": "Lennar_Jobs",          "industry": "real_estate"},  # ~344 jobs, US 100%  (homebuilder)
{"display_name": "Taylor Morrison",              "tenant": "taylormorrison",       "wd_host": "wd1",  "site": "TaylorMorrisonCareers","industry": "real_estate"},  # ~56 jobs,  US 100%  (homebuilder)
{"display_name": "Kimco Realty",                 "tenant": "kimcorealty",          "wd_host": "wd503","site": "KimcoCareers",         "industry": "real_estate"},  # ~24 jobs,  US 100%  (retail REIT)
{"display_name": "Realty Income",                "tenant": "realtyincome",         "wd_host": "wd108","site": "realty_income_careers","industry": "real_estate"},  # ~23 jobs,  US 60%   (net-lease REIT)
# hospitality — restaurants / hotels
{"display_name": "Shake Shack",                  "tenant": "shakeshack",           "wd_host": "wd5",  "site": "External",             "industry": "hospitality"},  # ~489 jobs, US 100%  (restaurant)
{"display_name": "Sonesta",                      "tenant": "reitmr",               "wd_host": "wd5",  "site": "Sonesta",              "industry": "hospitality"},  # ~296 jobs, US 90%   (hotels; tenant 'reitmr')
# travel — car rental
{"display_name": "Avis Budget Group",            "tenant": "avisbudget",           "wd_host": "wd1",  "site": "ABG_Careers",          "industry": "travel"},       # ~611 jobs, US 90%   (car rental — travel-adjacent)
# government — counties
{"display_name": "Sarasota County FL",           "tenant": "scgov",                "wd_host": "wd5",  "site": "SCGOV",                "industry": "government"},   # ~52 jobs,  US 100%  (distinct from City of Sarasota, tenant 'sarasotagov')
{"display_name": "Weld County CO",               "tenant": "weldgov",              "wd_host": "wd5",  "site": "WeldCountyCareers",    "industry": "government"},   # ~31 jobs,  US 100%
# batch 25 dead (HTTP 422 / no_postings — re-confirms prior deads): Invitation Homes (invitationhomes.wd1/INVH), Ventas (ventas.wd1), Atlanta Housing Authority (vhr-atlantahousing.wd1), Minnesota State (minnstate.wd1), DSST Public Schools (dsstpublicschools.wd5), Grand Traverse County MI (mygtcmi.wd1), Hillsborough Community College (hccfl.wd1/hccjobs), Success Academy Charter Schools (successacademies.wd1), CommuniCare Health (communicarehealth.wd1).
# batch 25 already in 532: Avera Health, Banner Health, Baptist Health, Cracker Barrel, DaVita, Dave & Buster's, HSHS, Intermountain Health, JLL, Lehigh Valley Health Network, MultiCare, Saint Luke's Health System, University of Arkansas System, University of Kansas Health System, UofL Health.
# --- batch 26 (2026-07-18, session 2 round 2) — NONPROFIT / HEALTHCARE / BANKING / MEDIA / EDUCATION (validated; §0 keys) ---
# nonprofit — foundations, museums, arts, conservation, social-services, library
{"display_name": "The Nature Conservancy",       "tenant": "nature",             "wd_host": "wd108","site": "ExternalCareers",       "industry": "nonprofit"},    # ~57 jobs,  US 100%  (conservation)
{"display_name": "American Cancer Society",       "tenant": "acs",                "wd_host": "wd5",  "site": "ACSCareers",            "industry": "nonprofit"},    # ~71 jobs,  US 100%
{"display_name": "Goodwill of Arizona",           "tenant": "goodwillaz",         "wd_host": "wd1",  "site": "GoodwillAZ",            "industry": "nonprofit"},    # ~519 jobs, US 100%
{"display_name": "Community Services Group",       "tenant": "csgonline",          "wd_host": "wd5",  "site": "CSG",                   "industry": "nonprofit"},    # ~44 jobs,  US 100%  (behavioral/IDD human-services nonprofit)
{"display_name": "National Audubon Society",       "tenant": "audubon",            "wd_host": "wd503","site": "Audubon",               "industry": "nonprofit"},    # ~33 jobs,  US 100%  (conservation)
{"display_name": "Woodruff Arts Center",           "tenant": "woodruffcenter",     "wd_host": "wd5",  "site": "woodruffcenter",        "industry": "nonprofit"},    # ~29 jobs,  US 100%  (arts)
{"display_name": "Brooklyn Public Library",        "tenant": "brooklynpubliclibrary","wd_host": "wd12","site": "BPL",                  "industry": "nonprofit"},    # ~33 jobs,  US 100%  (public library — nonprofit/cultural; could be government)
{"display_name": "Simons Foundation",              "tenant": "simonsfoundation",   "wd_host": "wd1",  "site": "simonsfoundationcareers","industry": "nonprofit"},   # ~11 jobs,  US 100%  (research foundation)
{"display_name": "MacArthur Foundation",           "tenant": "macfound",           "wd_host": "wd1",  "site": "MAC_FOUND_EXT_CAREERS", "industry": "nonprofit"},    # ~9 jobs,   US 89%   (foundation)
{"display_name": "Metropolitan Museum of Art",     "tenant": "metmuseum",          "wd_host": "wd5",  "site": "metmuseumcareers",      "industry": "nonprofit"},    # ~6 jobs,   US 100%  (museum)
{"display_name": "Boston Symphony Orchestra",      "tenant": "bso",                "wd_host": "wd1",  "site": "BSO",                   "industry": "nonprofit"},    # ~6 jobs,   US 100%  (arts)
{"display_name": "Ford Foundation",                "tenant": "fordfoundation",     "wd_host": "wd1",  "site": "FordFoundationCareerPage","industry": "nonprofit"},   # ~1 job,   US 100%  (foundation; low volume)
# healthtech — regional health systems, senior living, hospice, FQHC, behavioral
{"display_name": "Sentara Health",                 "tenant": "sentara",            "wd_host": "wd1",  "site": "SCS",                   "industry": "healthtech"},   # ~2,000 jobs, US 100%  (VA/NC health system)
{"display_name": "Compassus",                      "tenant": "hospicecom",         "wd_host": "wd5",  "site": "Compassus",             "industry": "healthtech"},   # ~1,027 jobs, US 100%  (hospice/home health)
{"display_name": "Cone Health",                    "tenant": "conehealth",         "wd_host": "wd12", "site": "Cone_Health-Careers",   "industry": "healthtech"},   # ~941 jobs, US 100%  (NC)
{"display_name": "Wellstar Health System",         "tenant": "wellstar",           "wd_host": "wd1",  "site": "wellstarcareers",       "industry": "healthtech"},   # ~865 jobs, US 100%  (GA)
{"display_name": "LCMC Health",                    "tenant": "lcmchealth",         "wd_host": "wd1",  "site": "LCMCHealth",            "industry": "healthtech"},   # ~812 jobs, US 100%  (LA)
{"display_name": "ChristianaCare",                 "tenant": "christianacare",     "wd_host": "wd5",  "site": "CCHS",                  "industry": "healthtech"},   # ~337 jobs, US 100%  (DE)
{"display_name": "AltaMed Health Services",        "tenant": "altamed",            "wd_host": "wd1",  "site": "Careers",               "industry": "healthtech"},   # ~244 jobs, US 100%  (CA FQHC)
{"display_name": "Covenant Living Communities",    "tenant": "crc",                "wd_host": "wd1",  "site": "CLExternal",            "industry": "healthtech"},   # ~226 jobs, US 100%  (senior living)
{"display_name": "Nebraska Medicine",              "tenant": "nebraskamed",        "wd_host": "wd5",  "site": "NM",                    "industry": "healthtech"},   # ~220 jobs, US 100%
{"display_name": "Cityblock Health",               "tenant": "cityblockhealth",    "wd_host": "wd1",  "site": "CityblockExternalCareerSite","industry": "healthtech"},# ~38 jobs, US 100%  (value-based care)
# insurance — health plan + specialty insurer
{"display_name": "Point32Health",                  "tenant": "point32health",      "wd_host": "wd5",  "site": "THP",                   "industry": "insurance"},    # ~47 jobs,  US 100%  (Harvard Pilgrim / Tufts HP)
{"display_name": "MagMutual",                      "tenant": "magmutual",          "wd_host": "wd1",  "site": "MagMutual_Career",      "industry": "insurance"},    # ~2 jobs,   US 100%  (medical malpractice)
# banking — credit unions, farm credit, asset management
{"display_name": "Broadview Federal Credit Union", "tenant": "broadviewfcu",       "wd_host": "wd1",  "site": "broadviewfcucareers",   "industry": "banking"},      # ~50 jobs,  US 100%
{"display_name": "Truliant Federal Credit Union",  "tenant": "truliantfcu",        "wd_host": "wd1",  "site": "External",              "industry": "banking"},      # ~20 jobs,  US 100%
{"display_name": "GTE Financial",                  "tenant": "gtefinancial",       "wd_host": "wd1",  "site": "GTE_Financial_Career",  "industry": "banking"},      # ~15 jobs,  US 100%  (credit union)
{"display_name": "Transamerica (Aegon Asset Mgmt)","tenant": "transamerica",       "wd_host": "wd5",  "site": "AegonAM",               "industry": "banking"},      # ~13 jobs,  US 100%  (asset management)
{"display_name": "American AgCredit",              "tenant": "agloan",             "wd_host": "wd5",  "site": "AmericanAgCredit",      "industry": "banking"},      # ~7 jobs,   US 100%  (farm-credit lender)
# media — public broadcasting
{"display_name": "GBH (WGBH)",                     "tenant": "publicmedia",        "wd_host": "wd1",  "site": "WGBH_Careers",          "industry": "media"},        # ~22 jobs,  US 100%  (⚠ shares tenant 'publicmedia' with New England Public Media (site NEPM_Careers))
{"display_name": "The WNET Group",                 "tenant": "wnet",               "wd_host": "wd1",  "site": "WNETCareers",           "industry": "media"},        # ~10 jobs,  US 100%  (public TV — PBS NY)
# education — universities
{"display_name": "Nevada State University",        "tenant": "nshe",               "wd_host": "wd1",  "site": "NSU-external",          "industry": "education"},    # ~47 jobs,  US 100%  (tenant 'nshe' = NV System of Higher Ed)
{"display_name": "Kansas City University",         "tenant": "kansascity",         "wd_host": "wd1",  "site": "Jobs",                  "industry": "education"},    # ~36 jobs,  US 100%  (health-sciences university)
{"display_name": "Grand Valley State University",  "tenant": "gvsu",               "wd_host": "wd1",  "site": "careers",               "industry": "education"},    # ~21 jobs,  US 100%  (MI)
{"display_name": "Coppin State University",        "tenant": "marylandconnect",    "wd_host": "wd1",  "site": "CSU_Careers",           "industry": "education"},    # ~13 jobs,  US 100%  (HBCU; tenant 'marylandconnect' = USM shared)
# government — city / county
{"display_name": "City of Lakewood, Colorado",     "tenant": "lakewood",           "wd_host": "wd503","site": "Careers",               "industry": "government"},   # ~23 jobs,  US 100%
{"display_name": "Waupaca County, Wisconsin",      "tenant": "waupacacountywi",    "wd_host": "wd108","site": "Career",                "industry": "government"},   # ~5 jobs,   US 100%
# batch 26 dead (HTTP 422 / no_postings): RBFCU (rbfcu.wd5), SchoolsFirst FCU (schoolsfirstfcu.wd5), HTLF/Heartland (htlf.wd1), Conning (conning.wd5), Fort Hays State (fhsu.wd1), Nemours (nemours.wd1), PBS (vhr-pbs.wd5), Urban Institute (urban.wd1), Year Up (yearup.wd1), Marshfield Clinic (marshfieldclinichealthsystems.wd5).
# batch 26 already in 532 (27): Cadence Bank, Columbia Bank, Connexus CU, Cook Children's, First Financial Bank, Geisinger, Global Payments/TSYS, Grady Health, Gundersen, Highmark, Huntington Bank, Kemper, LGE CCU, Lake Michigan CU, MassMutual Ascend, Mountain America CU, Nationwide Children's, Northwestern Mutual, Ochsner, Penn Mutual, Prisma Health, Sanford Health, Service CU, Texas Mutual, Valley Health, Webster Bank, Wintrust.
# --- batch 27 (2026-07-19, session 2 round 3) — MEDIA / INDUSTRIALS / RETAIL / CONSULTING / TELECOM (validated; §0 keys) ---
# media — broadcast / publishing / radio / talent
{"display_name": "Nexstar Media Group",            "tenant": "nexstar",            "wd_host": "wd5",  "site": "nexstar",               "industry": "media"},        # ~621 jobs, US 100%  (TV broadcasting)
{"display_name": "iHeartMedia",                    "tenant": "iheartmedia",        "wd_host": "wd5",  "site": "External_iHM",          "industry": "media"},        # ~298 jobs, US 100%  (radio)
{"display_name": "New York Post",                  "tenant": "dowjones",           "wd_host": "wd1",  "site": "New_York_Post_Careers", "industry": "media"},        # ~44 jobs,  US 100%  (tenant 'dowjones' = News Corp shared)
{"display_name": "MediaNews Group",                "tenant": "myworkdaycenter",    "wd_host": "wd5",  "site": "MNG",                   "industry": "media"},        # ~37 jobs,  US 100%  (newspapers; shared tenant 'myworkdaycenter')
{"display_name": "Atlantic Media",                 "tenant": "atlanticmedia",      "wd_host": "wd1",  "site": "Careers",               "industry": "media"},        # ~14 jobs,  US 100%
{"display_name": "Creative Artists Agency",        "tenant": "caa",                "wd_host": "wd1",  "site": "Careers",               "industry": "media"},        # ~111 jobs, US 80%   (talent agency)
{"display_name": "Bonneville International",        "tenant": "deseretmanagement",  "wd_host": "wd1",  "site": "BonPhoenix",            "industry": "media"},        # ~2 jobs,   US 100%  (broadcasting)
{"display_name": "Tribune Publishing",             "tenant": "myworkdaycenter",    "wd_host": "wd5",  "site": "TPCO",                  "industry": "media"},        # ~2 jobs,   US 100%  (⚠ shares tenant 'myworkdaycenter' with MediaNews Group (site MNG))
# telecom — fiber / cable ISPs
{"display_name": "Cable One",                      "tenant": "cableone",           "wd_host": "wd1",  "site": "Cable_One_External_Careers","industry": "telecom"},    # ~134 jobs, US 100%
{"display_name": "ALLO Communications",            "tenant": "allofiber",          "wd_host": "wd501","site": "careers",               "industry": "telecom"},      # ~55 jobs,  US 100%  (fiber ISP)
# manufacturing_automotive — chemicals / industrials / packaging / electrical
{"display_name": "Greif",                          "tenant": "greif",              "wd_host": "wd5",  "site": "Greif",                 "industry": "manufacturing_automotive"}, # ~231 jobs, US 95%  (industrial packaging)
{"display_name": "Valmont Industries",             "tenant": "valmont",            "wd_host": "wd1",  "site": "ValmontCareers",        "industry": "manufacturing_automotive"}, # ~231 jobs, US 100% (infrastructure/ag structures)
{"display_name": "Watts Water Technologies",       "tenant": "wattswater",         "wd_host": "wd5",  "site": "External",              "industry": "manufacturing_automotive"}, # ~226 jobs, US 80%  (water products)
{"display_name": "Huntsman",                       "tenant": "huntsman",           "wd_host": "wd1",  "site": "Huntsman",              "industry": "manufacturing_automotive"}, # ~176 jobs, US 60%  (chemicals)
{"display_name": "Zurn Elkay",                     "tenant": "elkay",              "wd_host": "wd1",  "site": "Elkay_External",        "industry": "manufacturing_automotive"}, # ~98 jobs,  US 95%  (water/plumbing)
{"display_name": "Ashland",                        "tenant": "ashland",            "wd_host": "wd12", "site": "AshlandCareers1",       "industry": "manufacturing_automotive"}, # ~74 jobs,  US 80%  (specialty chemicals)
{"display_name": "The Mosaic Company",             "tenant": "mosaic",             "wd_host": "wd5",  "site": "mosaic",                "industry": "manufacturing_automotive"}, # ~73 jobs,  US 100% (fertilizer/mining)
{"display_name": "Mueller Water Products",         "tenant": "muellerwaterproducts","wd_host": "wd5", "site": "Mueller",               "industry": "manufacturing_automotive"}, # ~60 jobs,  US 100% (water infrastructure)
{"display_name": "CF Industries",                  "tenant": "cfindustries",       "wd_host": "wd1",  "site": "careers",               "industry": "manufacturing_automotive"}, # ~53 jobs,  US 85%  (nitrogen/fertilizer)
{"display_name": "Koppers",                        "tenant": "koppers",            "wd_host": "wd5",  "site": "Koppers",               "industry": "manufacturing_automotive"}, # ~29 jobs,  US 100% (carbon/chemicals)
{"display_name": "Ingevity",                       "tenant": "ingevity",           "wd_host": "wd1",  "site": "Ingevity",              "industry": "manufacturing_automotive"}, # ~29 jobs,  US 100% (specialty chemicals)
{"display_name": "Cabot",                          "tenant": "cabotcorp",          "wd_host": "wd12", "site": "Careers",               "industry": "manufacturing_automotive"}, # ~89 jobs,  US 25%  (specialty chemicals — ⚠ low US%)
{"display_name": "nVent",                          "tenant": "nvent",              "wd_host": "wd5",  "site": "nVent",                 "industry": "manufacturing_automotive"}, # ~516 jobs, US 100% (electrical connection/protection)
# aerospace_defense
{"display_name": "Moog",                           "tenant": "moog",               "wd_host": "wd5",  "site": "MOOG_External_Career_Site","industry": "aerospace_defense"},     # ~420 jobs, US 100% (motion control)
{"display_name": "Hexcel",                         "tenant": "hexcel",             "wd_host": "wd5",  "site": "HexcelCareers",         "industry": "aerospace_defense"},        # ~159 jobs, US 100% (aerospace composites)
# energy
{"display_name": "PBF Energy",                     "tenant": "pbfenergy",          "wd_host": "wd1",  "site": "PBF",                   "industry": "energy"},       # ~40 jobs,  US 100%  (petroleum refining)
# ecommerce_retail — retail / apparel / consumer / grocery / ag
{"display_name": "Five Below",                     "tenant": "fivebelow",          "wd_host": "wd1",  "site": "fivebelowcareers",      "industry": "ecommerce_retail"},   # ~2,000 jobs, US 100%  (retail)
{"display_name": "PVH Corp",                       "tenant": "pvh",                "wd_host": "wd1",  "site": "PVH_Careers",           "industry": "ecommerce_retail"},   # ~1,520 jobs, US 90%   (apparel — Calvin Klein/Tommy)
{"display_name": "BJ's Wholesale Club",            "tenant": "bjswholesaleclub",   "wd_host": "wd1",  "site": "BJsCareers",            "industry": "ecommerce_retail"},   # ~1,528 jobs, US 100%  (warehouse club)
{"display_name": "SpartanNash",                    "tenant": "spartannash",        "wd_host": "wd1",  "site": "SpartanNash_Careers",   "industry": "ecommerce_retail"},   # ~1,103 jobs, US 90%   (grocery distribution)
{"display_name": "Scotts Miracle-Gro",             "tenant": "scottsmiraclegro",   "wd_host": "wd5",  "site": "SMGExternal",           "industry": "ecommerce_retail"},   # ~209 jobs, US 80%     (consumer lawn/garden)
{"display_name": "Wolverine Worldwide",            "tenant": "wwwinc",             "wd_host": "wd1",  "site": "WWW",                   "industry": "ecommerce_retail"},   # ~221 jobs, US 75%     (footwear)
{"display_name": "The Andersons",                  "tenant": "andersonsinc",       "wd_host": "wd1",  "site": "TheAndersonsCareers",   "industry": "ecommerce_retail"},   # ~94 jobs,  US 80%     (agribusiness)
{"display_name": "YETI",                           "tenant": "yeticoolers",        "wd_host": "wd5",  "site": "YETI",                  "industry": "ecommerce_retail"},   # ~66 jobs,  US 65%     (consumer products)
# consulting — accounting/advisory, engineering & construction (E&C), staffing/BPO
{"display_name": "Parsons",                        "tenant": "parsons",            "wd_host": "wd5",  "site": "Search",                "industry": "consulting"},   # ~1,895 jobs, US 100%  (engineering/defense services)
{"display_name": "Primoris Services",              "tenant": "prim",               "wd_host": "wd108","site": "Primoris",              "industry": "consulting"},   # ~511 jobs, US 100%  (E&C infrastructure)
{"display_name": "Huron",                          "tenant": "huron",              "wd_host": "wd1",  "site": "huroncareers",          "industry": "consulting"},   # ~261 jobs, US 60%   (management consulting)
{"display_name": "Tutor Perini",                   "tenant": "tutorperini",        "wd_host": "wd12", "site": "external",              "industry": "consulting"},   # ~235 jobs, US 100%  (construction)
{"display_name": "Comfort Systems USA",            "tenant": "comfortsystemsusa",  "wd_host": "wd1",  "site": "Corpcareers",           "industry": "consulting"},   # ~189 jobs, US 100%  (mechanical/HVAC services)
{"display_name": "Katz, Sapper & Miller",          "tenant": "ksmcpa",             "wd_host": "wd12", "site": "KSMCareers",            "industry": "consulting"},   # ~20 jobs,  US 100%  (accounting/advisory)
{"display_name": "Jackson Healthcare",             "tenant": "jacksonhealthcare",  "wd_host": "wd1",  "site": "careers-LocumTenens",   "industry": "consulting"},   # ~5 jobs,   US 100%  (healthcare staffing)
{"display_name": "Epic Staffing Group",            "tenant": "epicstaffinggroup",  "wd_host": "wd1",  "site": "Epic_Careers",          "industry": "consulting"},   # ~4 jobs,   US 75%   (staffing)
{"display_name": "Loop Recruiting",                "tenant": "looprecruiting",     "wd_host": "wd1",  "site": "External",              "industry": "consulting"},   # ~1 job,    US 100%  (staffing; low volume)
# batch 27 dead (HTTP 422 / no_postings): First Solar (firstsolar.wd1 — re-confirms), Kohl's (kohls.wd1 — re-confirms), E.W. Scripps (scripps.wd5), Newsday (newsday.wd1), Sterling Infrastructure (sterling.wd5), Helen of Troy (helenoftroy.wd1), Del Monte Foods (delmontefoods.wd5 — re-confirms).
# batch 27 already in 532 (21): Armanino, Baker Tilly, Chemours, Columbia Sportswear, Comcast, Conagra Brands, Crowe, Granite Construction, Guidehouse, Levi Strauss, Mondelez, Nordstrom, Portland General Electric, Robert Half, Skechers, TaskUs, Terex, VF Corporation, Vera Bradley, Weis Markets, Zayo.
# --- batch 28 (2026-07-19, session 2 round 4) — PHARMA/BIOTECH / MED-DEVICE / COLLEGES / COUNTIES (validated; §0 keys) ---
# pharma — drug/biotech developers + CDMO/CRO
{"display_name": "Syneos Health",                  "tenant": "syneoshealth",       "wd_host": "wd12", "site": "Syneos_Health_External_Site","industry": "pharma"},   # ~695 jobs, US 90%   (CRO)
{"display_name": "Fortrea",                        "tenant": "fortrea",            "wd_host": "wd1",  "site": "Fortrea",               "industry": "pharma"},       # ~335 jobs, US 55%   (CRO)
{"display_name": "Catalent",                       "tenant": "catalent",           "wd_host": "wd1",  "site": "External",              "industry": "pharma"},       # ~316 jobs, US 75%   (CDMO)
{"display_name": "Neurocrine Biosciences",         "tenant": "neurocrine",         "wd_host": "wd5",  "site": "Neurocrinecareers",     "industry": "pharma"},       # ~125 jobs, US 100%
{"display_name": "Exelixis",                       "tenant": "exelixis",           "wd_host": "wd1",  "site": "Exel",                  "industry": "pharma"},       # ~61 jobs,  US 100%
{"display_name": "BioMarin",                       "tenant": "biomarpeople",       "wd_host": "wd3",  "site": "BioMar",                "industry": "pharma"},       # ~28 jobs,  US 40%   (⚠ low US%)
{"display_name": "PTC Therapeutics",               "tenant": "ptcbio",             "wd_host": "wd5",  "site": "PTC_Careers",           "industry": "pharma"},       # ~15 jobs,  US 87%
# healthtech — medical devices, diagnostics, life-science tools
{"display_name": "Dexcom",                         "tenant": "dexcom",             "wd_host": "wd1",  "site": "Dexcom",                "industry": "healthtech"},   # ~309 jobs, US 80%   (CGM devices)
{"display_name": "Envista",                        "tenant": "envista",            "wd_host": "wd1",  "site": "envistacareers",        "industry": "healthtech"},   # ~251 jobs, US 70%   (dental devices)
{"display_name": "Avantor",                        "tenant": "vwr",                "wd_host": "wd1",  "site": "avantorJobs",           "industry": "healthtech"},   # ~227 jobs, US 95%   (life-science tools; tenant 'vwr')
{"display_name": "Insulet",                        "tenant": "insulet",            "wd_host": "wd5",  "site": "insuletcareers",        "industry": "healthtech"},   # ~191 jobs, US 80%   (insulin pumps)
{"display_name": "Integra LifeSciences",           "tenant": "integralife",        "wd_host": "wd1",  "site": "Careers",               "industry": "healthtech"},   # ~154 jobs, US 100%  (med devices)
{"display_name": "Exact Sciences",                 "tenant": "exactsciences",      "wd_host": "wd1",  "site": "Exact_Sciences",        "industry": "healthtech"},   # ~131 jobs, US 100%  (cancer diagnostics)
{"display_name": "Haemonetics",                    "tenant": "haemonetics",        "wd_host": "wd5",  "site": "HAE",                   "industry": "healthtech"},   # ~97 jobs,  US 95%   (blood-management devices)
{"display_name": "QuidelOrtho",                    "tenant": "orthoclinical",      "wd_host": "wd1",  "site": "Search",                "industry": "healthtech"},   # ~93 jobs,  US 90%   (diagnostics; tenant 'orthoclinical')
{"display_name": "Repligen",                       "tenant": "repligen",           "wd_host": "wd108","site": "Repligen_Careers",      "industry": "healthtech"},   # ~76 jobs,  US 80%   (bioprocessing tools)
{"display_name": "Guardant Health",                "tenant": "gh",                 "wd_host": "wd1",  "site": "gh",                    "industry": "healthtech"},   # ~71 jobs,  US 90%   (liquid-biopsy diagnostics)
# education — private colleges / art schools / community colleges
{"display_name": "Babson College",                 "tenant": "babson",             "wd_host": "wd1",  "site": "Staff",                 "industry": "education"},    # ~66 jobs,  US 100%
{"display_name": "Bucks County Community College",  "tenant": "bucks",              "wd_host": "wd1",  "site": "BCCC",                  "industry": "education"},    # ~61 jobs,  US 100%  (PA)
{"display_name": "Berklee College of Music",       "tenant": "berklee",            "wd_host": "wd1",  "site": "BerkleeCareers",        "industry": "education"},    # ~52 jobs,  US 100%
{"display_name": "Union College",                  "tenant": "unioncollege",       "wd_host": "wd5",  "site": "UnionCollegeCareers",   "industry": "education"},    # ~41 jobs,  US 100%  (NY)
{"display_name": "Wesleyan University",             "tenant": "wesleyan",           "wd_host": "wd5",  "site": "careers",               "industry": "education"},    # ~29 jobs,  US 100%
{"display_name": "Colby College",                  "tenant": "colby",              "wd_host": "wd5",  "site": "ColbyCareers",          "industry": "education"},    # ~28 jobs,  US 100%
{"display_name": "Bryn Mawr College",              "tenant": "brynmawr",           "wd_host": "wd12", "site": "External",              "industry": "education"},    # ~26 jobs,  US 100%
{"display_name": "Maryland Institute College of Art","tenant": "mica",             "wd_host": "wd5",  "site": "Faculty",               "industry": "education"},    # ~26 jobs,  US 100%
{"display_name": "Denison University",             "tenant": "denisonuniversity",  "wd_host": "wd12", "site": "DU",                    "industry": "education"},    # ~22 jobs,  US 100%  (OH)
{"display_name": "Pomona College",                 "tenant": "theclaremontcolleges","wd_host": "wd1", "site": "POM_Careers",           "industry": "education"},    # ~22 jobs,  US 100%  (tenant 'theclaremontcolleges' — shared Claremont consortium)
{"display_name": "Rhode Island School of Design",  "tenant": "risd",               "wd_host": "wd5",  "site": "RISD",                  "industry": "education"},    # ~20 jobs,  US 100%
{"display_name": "Carleton College",               "tenant": "carleton",           "wd_host": "wd1",  "site": "CarletonCareers",       "industry": "education"},    # ~19 jobs,  US 100%  (MN)
{"display_name": "Vassar College",                 "tenant": "vassar",             "wd_host": "wd1",  "site": "Vassar-External",       "industry": "education"},    # ~18 jobs,  US 100%
{"display_name": "Wofford College",                "tenant": "wofford",            "wd_host": "wd5",  "site": "Wofford",               "industry": "education"},    # ~16 jobs,  US 100%  (SC)
{"display_name": "Rhodes College",                 "tenant": "rhodes",             "wd_host": "wd5",  "site": "Rhodes_Careers",        "industry": "education"},    # ~15 jobs,  US 100%  (TN)
{"display_name": "Erie Community College",         "tenant": "ecc",                "wd_host": "wd5",  "site": "CareerOpportunities",   "industry": "education"},    # ~11 jobs,  US 100%  (NY)
{"display_name": "Lewis & Clark College",          "tenant": "lewisandclark",      "wd_host": "wd5",  "site": "staff",                 "industry": "education"},    # ~11 jobs,  US 100%  (OR)
{"display_name": "Ringling College of Art and Design","tenant": "ringling",        "wd_host": "wd1",  "site": "RinglingExternalCareers","industry": "education"},   # ~7 jobs,   US 100%  (FL)
{"display_name": "Haverford College",              "tenant": "haverford",          "wd_host": "wd1",  "site": "External",              "industry": "education"},    # ~4 jobs,   US 100%  (PA)
{"display_name": "Hamline University",             "tenant": "hamline",            "wd_host": "wd5",  "site": "Faculty_Career_Site",   "industry": "education"},    # ~2 jobs,   US 100%  (MN)
# government — counties + cities
{"display_name": "Bexar County, Texas",            "tenant": "bexar",              "wd_host": "wd12", "site": "BexarCountyExternalCareerSite","industry": "government"}, # ~89 jobs, US 100%  (San Antonio)
{"display_name": "City of Orlando, Florida",       "tenant": "cityoforlando",      "wd_host": "wd5",  "site": "CityofOrlandoCareers",  "industry": "government"},   # ~56 jobs,  US 100%
{"display_name": "Jackson County, Missouri",       "tenant": "jacksongov",         "wd_host": "wd5",  "site": "Court",                 "industry": "government"},   # ~56 jobs,  US 100%  (Kansas City)
{"display_name": "Spokane County, Washington",     "tenant": "spokanecounty",      "wd_host": "wd503","site": "Spokane",               "industry": "government"},   # ~53 jobs,  US 100%
{"display_name": "Tulsa County, Oklahoma",         "tenant": "tulsacounty",        "wd_host": "wd1",  "site": "TC",                    "industry": "government"},   # ~43 jobs,  US 100%
{"display_name": "Bernalillo County, New Mexico",  "tenant": "bernco",             "wd_host": "wd1",  "site": "berncocareers",         "industry": "government"},   # ~42 jobs,  US 100%  (Albuquerque)
{"display_name": "Jefferson County, Colorado",     "tenant": "jefferson",          "wd_host": "wd5",  "site": "External",              "industry": "government"},   # ~30 jobs,  US 100%
{"display_name": "Macomb County, Michigan",        "tenant": "macombgov",          "wd_host": "wd1",  "site": "External",              "industry": "government"},   # ~28 jobs,  US 100%
{"display_name": "City of Georgetown, Texas",      "tenant": "cog",                "wd_host": "wd5",  "site": "External_Prod",         "industry": "government"},   # ~25 jobs,  US 100%
{"display_name": "Hinds County, Mississippi",      "tenant": "hindscountyms",      "wd_host": "wd1",  "site": "HindsCareers",          "industry": "government"},   # ~12 jobs,  US 100%  (Jackson)
{"display_name": "Hamilton County, Indiana",       "tenant": "hamiltoncountyindiana","wd_host": "wd1","site": "Careers",               "industry": "government"},   # ~10 jobs,  US 100%
{"display_name": "Doña Ana County, New Mexico",    "tenant": "donaanacounty",      "wd_host": "wd503","site": "DAC",                   "industry": "government"},   # ~10 jobs,  US 100%  (Las Cruces)
# batch 28 dead (HTTP 422): United Therapeutics (vhr-unither.wd5), Jazz Pharmaceuticals (vhr-jazz.wd1), Halozyme (halozyme.wd1), Merit Medical (merit.wd1), Bentley University (bentley.wd1), Kean University (kean.wd1), College of Lake County (clc.wd5), Furman University (furman.wd5), Hamilton College (hamiltoncollege.wd5), Regis University (regis.wd5), Personnel Board of Jefferson County AL (pbjcal.wd5).
# batch 28 already in 532 (4): Azenta, Bio-Techne, Globus Medical, Revvity.
```

> **Government is a viable new non-tech vein** — states/counties/cities on Workday (though many use GovernmentJobs.com/NeoGov or Taleo). Roles are heavily non-tech (public works, admin, analysts, police). Effectively unlimited (50 states, thousands of counties/cities) — treat like universities: sweep as much as you want.

> **Higher-ed reality check:** Workday adoption is ~half, not universal. Flagships often run legacy PeopleAdmin/Taleo/Oracle. The high yield comes from harvesting the *incidental* Workday universities that surface in each search, not just the searched target.

> **✅ Taxonomy gap RESOLVED (see §0):** universities → `education`, hotels/restaurants → `hospitality`, airlines/cruise/car-rental → `travel`, govt/authorities → `government`, nonprofits → `nonprofit`, REITs/CRE → `real_estate`. All previously-`other`/`TODO` rows have been relabeled. The only code change is 6 new labels in the frontend `INDUSTRY_LABELS` (backend needs none — keys are derived from catalog rows).

**Notes / open items on this batch:**
- **Southwest / United Airlines** — ✅ resolved: now `travel` (see §0). No longer `TODO`.
- **Dollar Tree** — ~23,700 postings but almost all retail/store roles; high volume, low relevance for a tech-focused résumé. Consider whether to include.
- **Walmart** — external site is `walmart.wd504.myworkdayjobs.com/WalmartExternal` (NOT wd5 — our old `DEFAULT_WORKDAY_TENANTS` entry `walmart.wd5/WalmartExternal` now 410s). The wd504 endpoint returned **non-JSON** on probe → **needs investigation** before adding (likely gated/different shape).

---

## 4. Not-on-Workday (correctly skipped)
Searched but use a different ATS — do **not** add:
- batch 1: Peloton (SmartRecruiters), Rivian (iCIMS/Ashby), SiriusXM (iCIMS), Progressive, Starbucks, Aflac, Chubb (insurer), Kroger, Dollar General.
- batch 2: Delta (Avature), Hilton (Taleo), Fluor + NextEra (custom), AECOM + Colgate-Palmolive (SmartRecruiters), General Mills (custom).
- batch 3 (industrials): Sherwin-Williams (Oracle HCM), Whirlpool (Eightfold), Corning, Cintas, Ecolab, Hershey, McCormick, Keurig Dr Pepper, Fastenal (all custom / non-supported).
- batch 4: Emory University (iCIMS), Duke University (custom; the Workday hit was Duke *Energy*, already in 532), Ulta Beauty (custom), Darden (recruiting.com), Yum! Brands (custom).
- batch 5: Northwestern (PeopleSoft), Johns Hopkins (custom). Tech: Fortinet (custom); Snowflake/Datadog/GitLab (ATS not surfaced); Databricks + Palo Alto Networks already in **career-pages** catalog.
- batch 6 (universities NOT on Workday): Princeton (PeopleSoft), Columbia (custom), Dartmouth (custom), Boston University (SilkRoad), NYU (iCIMS), Notre Dame (custom), Rice (Oracle HCM), Stanford (Taleo), U-Michigan (PeopleSoft), Vanderbilt (only VUMC, already in 532).

> **Universities are the strongest Workday vein** (Workday Student) and heavily non-tech — batches 4–6 added **11 universities**. But adoption is mixed (~half): big public + many privates run Workday (Penn, Brown, Cornell, CMU, Georgetown, USC, UChicago, Penn State, WashU), while others use Taleo / iCIMS / PeopleSoft / Oracle (Stanford, Princeton, Columbia, NYU, Michigan…).

> Pattern worth noting: **many large non-tech enterprises are NOT on Workday** (airlines, hospitality, big CPG, industrials, EPC/construction often run Avature / Taleo / iCIMS / Eightfold / Oracle HCM / custom). Batch 3 was 1 Workday hit in 10. So the harvestable Workday long tail among *big names* is small — the 532 already captured most large US Workday users. Net-new comes from **mid-market**, and requires many searches per hit.

## 5. Gated (HTTP 422 — unreachable via direct CXS)
Ford, GM, ServiceNow, VMware, American Express, PepsiCo, UPS, Honeywell, FedEx. → route via the MNC `aggregator` engine, not the Workday catalog.

## 6. ATSes seen during discovery that need a NEW fetcher (engineering backlog)

Hunting Workday tenants + career-pages boards surfaced many high-value **US**
employers running an ATS we **don't yet fetch** (PeopleAdmin, NeoGov, Taleo…).
This section scopes and demand-ranks the "write a new fetcher" work so it isn't
lost in the per-batch prose above.

> **Where a new fetcher actually lives — NOT the Workday tab.** The Workday tab
> is CXS-only (fixed shape, `workday_cxs_fetcher.py`); it cannot host these.
> A new fetcher is a `_fetch_<ats>()` added to
> `services/career_pages_service.py`, registered in **`FETCHERS`** (whole-board
> JSON, one call — greenhouse/lever/ashby/…) or **`ENTERPRISE_FETCHERS`**
> (per-term search API — amazon/eightfold/phenom/`orc`/uber). The companies then
> go into the **career-pages** catalog, not the Workday one. Documented here only
> because this is where they were discovered — see companion
> [career-pages-expansion.md](career-pages-expansion.md).

### 6.1 The fetcher-adding recipe (mirror the `orc` precedent)

`_fetch_orc` (added 2026-07-17) is the reference for a config-driven enterprise
fetcher and the template to copy:

1. **Write `_fetch_<ats>(c)`** (board style) **or `_fetch_<ats>(c, terms)`**
   (search style). `c` is the catalog row dict; board fetchers read `c["slug"]`,
   enterprise fetchers read `c["host"]` + `c.get("extra")` (e.g. ORC's
   `{"site_number": "CX_1"}`). Hit the anonymous JSON endpoint, map each posting
   through **`_row(c, id, title, url, iso_date, location, description=…,
   is_remote=…, country=…)`**, wrap network work in try/except → `[]`.
2. **Register** the function in `FETCHERS` (board) or `ENTERPRISE_FETCHERS`
   (search) in `career_pages_service.py`.
3. **Add catalog rows** to `CAREER_PAGES_CATALOG` (or `ENTERPRISE_CATALOG` for
   host+extra rows). Downstream `_keep_us` / dedup / relevance are free — the
   dispatcher (`career_pages_service.py:~945`) already US-filters and dedups.
4. **Prereq is an anonymous JSON API.** No cookie/CSRF/JS-render wall. That
   single fact decides Tier B vs Tier C below.

### 6.2 Tier A — quick win, NO new fetcher needed (already on `orc`)

These universities/employers turned out to run **Oracle Recruiting Cloud**,
which `_fetch_orc` **already supports**. No code — just add `ENTERPRISE_CATALOG`
rows `{"ats":"orc","host":"…oraclecloud.com","extra":{"site_number":"<CX_n>"}}`
after a live validation batch. (Tracked as OPEN THREAD #1 in `SESSION_HANDOFF.md`.)

| Company | ORC host | site_number |
|---|---|---|
| University of Tennessee | `fa-ewlq-saasfaprod1.fa.ocs.oraclecloud.com` | `CX_1` |
| Baylor University | `ejof.fa.us2.oraclecloud.com` | `BaylorCareers` |
| Rice University | `emdz.fa.us2.oraclecloud.com` | `CX_2001` |
| Tulane University | (resolve host) | (resolve) |
| Sherwin-Williams | `ejhp.fa.us6.oraclecloud.com` | `CX_2` |

### 6.3 Tier B — FEASIBLE new fetchers (worth a bounded spike)

Each opens a large, mostly non-tech vein. Both are **whole-board-per-agency**
platforms → likely `FETCHERS` (board) style, one call per employer. Ship only
after a spike confirms an **anonymous JSON** response (no auth/CSRF wall).

**(B1) NeoGov — `governmentjobs.com` / `schooljobs.com`** — *highest priority*
- **Vein:** state/county/city governments + community colleges. Effectively
  unlimited (50 states, thousands of local govts) and heavily non-tech.
- **Board pattern:** per-agency at `governmentjobs.com/careers/<agency>` (govt)
  and `schooljobs.com/careers/<agency>` (education — same platform). A
  **"Career Pages API"** exists under `/careers/<agency>/…` (live example:
  `schooljobs.com/careers/APITestAgency/jobInterestCards/categories`).
- **Spike:** confirm an anonymous JSON jobs-list endpoint for `/careers/<agency>`
  and whether the `<agency>` slug is discoverable from the public board URL
  (like a greenhouse slug). Related prior art: `GSA/jobs_api` (GitHub).
- **Waiting behind it** (found non-Workday, GovernmentJobs/NeoGov): State of
  Washington, State of Michigan, City of Seattle, City of Portland, City of San
  Diego, King County, Fairfax County, Commonwealth of Massachusetts, State of
  Tennessee, State of Arizona *(some may be Taleo — confirm per-agency)*.

**(B2) PeopleAdmin (SelectSuite) — `<slug>.peopleadmin.com`** — *higher risk*
- **Vein:** universities (the big non-Workday higher-ed ATS; ~half of flagships).
- **Board pattern:** `<slug>.peopleadmin.com/postings/search`.
- **Risk:** no public JSON/RSS confirmed in research — the search UI may be
  HTML-only / session-gated. Spike must first prove an anonymous machine-readable
  feed exists; if it's HTML-only, this drops to Tier C.
- **Waiting behind it** (found on PeopleAdmin): Indiana University, University of
  Georgia, George Washington University, Villanova, SMU, Syracuse *(SMU/Syracuse
  possibly Taleo — confirm)*.

### 6.4 Tier C — NOT feasible (bot-walled / gated / no anonymous JSON)

Confirmed unreachable without a browser/session (per `SESSION_HANDOFF.md` thread
2 + career-pages doc). **Do not build fetchers** — route these employers via the
MNC tab's `aggregator` (JSearch) engine instead.

| ATS | Why blocked | Employers seen behind it |
|---|---|---|
| **iCIMS** | bot-walled, no anonymous JSON | Emory, NYU, CommonSpirit, SiriusXM, Rivian |
| **Taleo** (Oracle) | gated / session-walled | Stanford, Cook County, State of Ohio, Colorado, Hilton, Hyatt, Kaiser Permanente |
| **SuccessFactors** (SAP) | gated | Purdue |
| **PeopleSoft** (Oracle) | no public board API | Princeton, Northwestern, U-Michigan, Miami-Dade |
| **Avature** | gated | Delta |
| **Dayforce / Ceridian** | gated | Gannett |
| **SilkRoad** | no anonymous JSON | Boston University |
| **PageUp** | no anonymous JSON | (seen in batch 9 higher-ed sweep) |
| **Fully custom sites** | no ATS API at all | City of Phoenix, Fordham, DePaul, NPR, Columbia, Dartmouth, Notre Dame |

> **Net:** the only two new fetchers worth building from this discovery are
> **NeoGov** (big win, do first) and **PeopleAdmin** (spike-gated). Everything
> else is either already supported (`orc`, Tier A) or bot-walled (Tier C → MNC
> aggregator). `orc` already proved the config-driven enterprise-fetcher pattern
> scales, so B1/B2 are bounded, not open-ended.

## 7. Non-tech roles — can Career Portals serve finance / construction / etc.?

**Finding: the Career Portals tabs are résumé-driven, NOT hard-locked to tech — but there are tech *leanings* in retrieval/ranking.**

- The hardcoded tech-title filter `_ATS_TECH_TITLE` lives ONLY in `daily_pipeline_service.py` (the now-disabled Apify pipeline). The Workday / career-pages / MNC tabs do **not** use it.
- Relevance is driven by the user's résumé: `_derive_query_terms` builds terms from résumé **titles**; `_keep_relevant` tier-0 keeps postings whose title matches a résumé term. Both are domain-agnostic in principle — a finance/construction résumé with clear titles already surfaces those roles.

**Tech leanings that would weaken non-tech results:**
1. `_derive_query_terms` fallback is hardcoded `["software engineer"]`, and its lone-token allowlist is tech-only (`engineer/developer/scientist/analyst/architect/devops/sre/...`) — a single-word non-tech title ("Accountant", "Electrician", "Nurse") gets dropped.
2. `_keep_relevant` tier-1 recall favors **engineering** role nouns (`_ROLE_NOUN_RE`); non-tech titles survive only via tier-0 term match, so recall is thinner.
3. `_SKILL_TERM_STOPLIST` + skill scoring are tuned for tech skills.
4. Default seed titles / smart-suggest synthesizer skew tech.

**Companies are NOT the blocker** — the 532 already spans banks, insurers, healthcare, retail, manufacturing, energy, consulting; the additions we're finding (Sysco, Tyson, Coca-Cola, airlines…) are heavily non-tech employers.

**Recommendation:** supporting non-tech is a **distinct workstream** from catalog growth, feasible with targeted changes (not a rewrite): generalize `_derive_query_terms` (broaden allowlist, neutral fallback), broaden/adapt `_ROLE_NOUN_RE`, generalize skill scoring/stoplist, domain-aware defaults. Whether to do it depends on target audience (JobPilot is currently positioned for tech/new-grad seekers). **Catalog expansion helps all domains regardless**, so it's safe to keep doing now.

---

## 8. Next steps (later)
- [x] **Industry taxonomy decided (§0):** 6 new keys added, all `other`/`TODO` rows relabeled. Airlines (Southwest/United) → `travel`. **Remaining action: add the 6 labels to the frontend `INDUSTRY_LABELS` (§0.3) when the rows are pasted into the catalog.**
- [ ] Decide whether to include Dollar Tree (retail-heavy) and low-volume United (~2 jobs now).
- [ ] (Optional, separate) Decide whether to invest in non-tech role support (§7).
- [ ] Investigate Walmart `wd504` endpoint (bad_json).
- [ ] Run more candidate batches for under-covered sectors (CPG/food, airlines/travel, consumer retail).
- [ ] Add the finalized rows to `services/workday_tenant_catalog.py`.
- [ ] Separately: retire the dead `DEFAULT_WORKDAY_TENANTS` list and point live-search `workday_direct` at a fast slice of the validated 532.
- [ ] Consider committing `scripts/discover_workday_tenants.py` as the permanent Workday-tenant validator (fills the missing `validate_workday_tenants.py`).
- [ ] **New fetchers (§6):** validate the Tier A `orc` batch and add rows (no code). Spike **NeoGov** (`governmentjobs.com`/`schooljobs.com` Career Pages API) → if anonymous JSON confirmed, write `_fetch_neogov` + `FETCHERS` entry. Then spike **PeopleAdmin** (may be HTML-only → may drop to Tier C).

**Sources:** [bloomberry — companies using Workday](https://bloomberry.com/data/workday/)
