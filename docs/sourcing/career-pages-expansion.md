# Career-Pages Catalog — Expansion Findings

**Status:** ready to implement · **Last updated:** 2026-07-19
**Companion to:** [workday-tenant-expansion.md](workday-tenant-expansion.md)
**Scope:** grow the "Startups & scale-ups (career pages)" catalog with **US** companies
that use a **supported board ATS** and aren't already in the list.

> Nothing here has been added to `services/career_pages_catalog.py` yet — this doc IS
> the candidate set. All rows validated live (reachability stamp in §4).

---

## ▶ HANDOFF — implement in this order

**What this is:** ~133 net-new, live-validated career-pages companies (§4), each a
paste-ready `{...}` dict with an `ats` + `slug` and an `industry` from the taxonomy in
[workday-tenant-expansion.md §0](workday-tenant-expansion.md).

1. **Paste the rows.** Copy every `{"display_name": …}` **data row** from the §4 code
   blocks into `shared/python/services/career_pages_catalog.py` → `CAREER_PAGES_CATALOG`.
   **Skip every `#` comment line** (dead / already-in-catalog notes, not rows). Every row
   uses an already-supported ATS (`greenhouse`/`lever`/`ashby`/`smartrecruiters`/`workable`/`recruitee`)
   — **no new fetcher needed**.
2. **Backend: no other change** — `INDUSTRIES` is derived from the rows; new keys work immediately.
3. **Frontend labels** are the SAME shared edit as the Workday doc (one `INDUSTRY_LABELS`
   change covers both tabs) — see [§0.3 there](workday-tenant-expansion.md). `gaming` already labeled.
4. **`other` rows are intentional** (3): food-testing labs (Certified Group) and freight/logistics
   (PS Logistics, Circle Logistics, Double D, Cardinal) — no clean key. Candidate for a future
   `transportation`/`logistics` key; fine as `other` for now.
5. **Optional: re-validate first** — same tooling as the Workday doc (`extract_rows.py` →
   `scripts/discover_career_pages.py`); drop anything newly `dead`.

`industry` values are editable judgment calls; a few rows are reachable-but-thin (flagged inline).

---

## 1. What "career pages" covers

Catalog: `services/career_pages_catalog.py` — **~614** companies (`CAREER_PAGES_CATALOG`)
+ **~11** enterprise entries (`ENTERPRISE_CATALOG`).

**Supported ATSes** (a company is only addable if it runs one of these):

| Group | ATS keys | Fetch style |
|---|---|---|
| Board ATSes (`FETCHERS`) | `greenhouse`, `lever`, `ashby`, `smartrecruiters`, `workable`, `recruitee` | whole-board JSON, one call |
| Enterprise (`ENTERPRISE_FETCHERS`) | `amazon`, `eightfold`, `phenom`, `orc`, `uber` | per-term search API |

**NOT supported** (bot-walled / no anonymous JSON — cannot be added here):
`iCIMS`, `Taleo`, `Avature`, and fully custom career sites. Companies on these
(Delta, Hilton, SiriusXM, Fluor, NextEra, General Mills, Progressive, Starbucks…)
can only be reached via the MNC tab's `aggregator` (JSearch) engine.

## 2. Row format we need for coding

```python
# services/career_pages_catalog.py  ->  CAREER_PAGES_CATALOG
{"display_name": "AECOM", "ats": "smartrecruiters", "slug": "AECOM2", "industry": "consulting"},
```
- `ats` — one of the supported board keys above
- `slug` — the board slug from the ATS URL:
  - greenhouse `boards.greenhouse.io/<slug>` · lever `jobs.lever.co/<slug>` ·
    ashby `jobs.ashbyhq.com/<slug>` · smartrecruiters `careers.smartrecruiters.com/<slug>` ·
    workable `apply.workable.com/<slug>` · recruitee `<slug>.recruitee.com`
- `industry` — one of the **21** taxonomy keys (15 original + 6 new — see the
  authoritative taxonomy + UI-label list in
  [workday-tenant-expansion.md §0](workday-tenant-expansion.md#0-industry-taxonomy--new-category-keys--ui-labels-decided-this-session)):
  `tech, fintech, healthtech, banking, insurance, pharma, energy, telecom,
  consulting, ecommerce_retail, manufacturing_automotive, aerospace_defense,
  media, gaming, other` **+ `education, government, nonprofit, hospitality,
  travel, real_estate`** (this doc's `other` rows were relabeled to the new keys).
  The catalog `industry` field is free-form (no backend enum); the only UI change
  is the 6 new labels in the frontend `INDUSTRY_LABELS` map, which is shared by
  the career-pages tab and the Workday tab.

## 3. Method

**Discovery:** when a company is NOT on Workday, web-search
`"<company> careers greenhouse OR lever OR ashby OR smartrecruiters"` (or just
read the ATS host from a normal careers search) → grab the `ats` + `slug` from
the URL.

**Validation + dedup:** call the real fetcher and confirm US jobs, dedup vs the
existing catalog. Quick check:
```python
from services.career_pages_catalog import CAREER_PAGES_CATALOG
from services.career_pages_service import FETCHERS, _keep_us
jobs = FETCHERS["smartrecruiters"]({"display_name":"AECOM","ats":"smartrecruiters","slug":"AECOM2","industry":"consulting"})
print(len(jobs), sum(1 for j in jobs if _keep_us(j)))
```
**Reusable script (built + tested):** [`scripts/discover_career_pages.py`](../scripts/discover_career_pages.py)
— mirrors the Workday one. Give it ATS URLs; it detects `ats`+`slug`, calls the
real fetcher, US-checks, dedups vs `CAREER_PAGES_CATALOG` + `ENTERPRISE_CATALOG`,
and prints paste-ready rows.
```
PYTHONPATH=shared/python python scripts/discover_career_pages.py candidates.txt
# candidates.txt lines: "Company | https://careers.smartrecruiters.com/<slug>"
```
Detects: greenhouse (`boards.greenhouse.io/<slug>`), lever (`jobs.lever.co/<slug>`),
ashby (`jobs.ashbyhq.com/<slug>`), smartrecruiters (`careers|jobs.smartrecruiters.com/<slug>`),
workable (`apply.workable.com/<slug>` or `<slug>.workable.com`), recruitee (`<slug>.recruitee.com`).

---

## 4. Validated NEW US companies — candidate additions (2026-07-18)

**~133 net-new** validated career-pages companies (4 incidental + 51 sweep +
17 session-2 + 36 session-3 + 15 session-4 + **10 session-5**); **not** in the
existing ~614 list; validated live returning US jobs. **✅ Full re-validation
2026-07-19: 124 rows re-probed live → 124 reachable, 0 dead, 0 non-US; session-5
(+10) validated live on insertion.** All `industry` values use the **§0 taxonomy**
(only genuinely-uncategorizable rows stay `other` — food-testing labs +
freight/logistics, the latter a candidate for a future `transportation`/`logistics` key).

```python
{"display_name": "AECOM",                          "ats": "smartrecruiters", "slug": "AECOM2",       "industry": "consulting"},              # 200 jobs, all US   (batch 1)
{"display_name": "Rivian & VW Group Technologies", "ats": "ashby",           "slug": "rivianvw.tech", "industry": "manufacturing_automotive"}, # 124 jobs, 95 US    (batch 1)
# --- batch 5 (2026-07-18) — tech ---
{"display_name": "Pure Storage",                   "ats": "greenhouse",      "slug": "purestorage",  "industry": "tech"},                    # 332 jobs, 150 US
# --- batch 8 (2026-07-18) — tech ---
{"display_name": "Confluent",                      "ats": "ashby",           "slug": "confluent",    "industry": "tech"},                    # 31 jobs, 21 US
# ============================================================================
# DEDICATED CAREER-PAGES SWEEP (2026-07-18) — 51 new, validated via
# scripts/discover_career_pages.py (dedup vs CAREER_PAGES_CATALOG + live fetch + US-count).
# Discovered by parallel sector-scoped web-search agents, not incidental to Workday.
# ============================================================================
# --- sweep A — tech / AI / robotics / dev-tools / security ---
{"display_name": "Field AI",                       "ats": "lever",           "slug": "field-ai",             "industry": "tech"},   # 99 jobs, 92 US   (robotics/AI)
{"display_name": "Hive",                           "ats": "lever",           "slug": "hive",                 "industry": "tech"},   # 75 jobs, 74 US   (AI/ML)
{"display_name": "Tutor Intelligence",             "ats": "lever",           "slug": "tutorintelligence",    "industry": "tech"},   # 37 jobs, 37 US   (robotics)
{"display_name": "Machina Labs",                   "ats": "lever",           "slug": "MachinaLabs",          "industry": "tech"},   # 25 jobs, 25 US   (robotic manufacturing)
{"display_name": "ISEE",                           "ats": "lever",           "slug": "isee",                 "industry": "tech"},   # 25 jobs, 25 US   (autonomous trucking)
{"display_name": "Shepherd",                       "ats": "ashby",           "slug": "shepherd",             "industry": "tech"},   # 15 jobs, 15 US
{"display_name": "StackBlitz",                     "ats": "greenhouse",      "slug": "stackblitz",           "industry": "tech"},   # 11 jobs, 11 US   (dev tools / bolt.new)
{"display_name": "Confluera",                      "ats": "lever",           "slug": "confluera",            "industry": "tech"},   # 11 jobs, 10 US   (security)
{"display_name": "Teleo",                          "ats": "lever",           "slug": "teleo",                "industry": "tech"},   # 9 jobs, 9 US    (autonomous heavy equipment)
{"display_name": "Multiply Labs",                  "ats": "lever",           "slug": "multiplylabs",         "industry": "tech"},   # 8 jobs, 8 US    (robotics/pharma automation)
{"display_name": "Langfuse",                       "ats": "ashby",           "slug": "langfuse",             "industry": "tech"},   # 8 jobs, 1 US    (LLM observability — ⚠ low US)
{"display_name": "Oso",                            "ats": "ashby",           "slug": "Oso",                  "industry": "tech"},   # 7 jobs, 7 US    (authorization infra)
{"display_name": "Ambi Robotics",                  "ats": "lever",           "slug": "ambirobotics",         "industry": "tech"},   # 6 jobs, 6 US    (robotics)
{"display_name": "Gravity Climate",                "ats": "ashby",           "slug": "gravityclimate",       "industry": "tech"},   # 5 jobs, 5 US    (climate software)
{"display_name": "Kled AI",                        "ats": "ashby",           "slug": "kled-ai",              "industry": "tech"},   # 4 jobs, 4 US
{"display_name": "Radical AI",                     "ats": "lever",           "slug": "RadicalAI",            "industry": "tech"},   # 3 jobs, 3 US
{"display_name": "Meshy",                          "ats": "ashby",           "slug": "meshy",                "industry": "tech"},   # 31 jobs, 12 US   (AI 3D generation)
# --- sweep B — healthcare / biotech / pharma ---
{"display_name": "Munson Healthcare",              "ats": "smartrecruiters", "slug": "MunsonHealthcare1",    "industry": "healthtech"},   # 200 jobs, 200 US  (health system, MI)
{"display_name": "SmithRx",                        "ats": "greenhouse",      "slug": "smithrx",              "industry": "healthtech"},   # 36 jobs, 36 US    (PBM)
{"display_name": "Virta Health",                   "ats": "ashby",           "slug": "virtahealth",          "industry": "healthtech"},   # 24 jobs, 24 US    (metabolic care)
{"display_name": "GRAIL",                          "ats": "lever",           "slug": "grailbio",             "industry": "healthtech"},   # 22 jobs, 22 US    (cancer detection)
{"display_name": "Everlywell",                     "ats": "lever",           "slug": "everlywell",           "industry": "healthtech"},   # 22 jobs, 22 US    (at-home testing)
{"display_name": "Layer Health",                   "ats": "greenhouse",      "slug": "layerhealth",          "industry": "healthtech"},   # 18 jobs, 18 US    (clinical AI)
{"display_name": "Benedictine Health System",      "ats": "smartrecruiters", "slug": "BenedictineHealthSystem","industry": "healthtech"}, # 16 jobs, 16 US    (senior care)
{"display_name": "Karius",                         "ats": "lever",           "slug": "kariusdx",             "industry": "healthtech"},   # 6 jobs, 6 US      (diagnostics)
{"display_name": "Lycia Therapeutics",             "ats": "lever",           "slug": "LyciaTherapeutics",    "industry": "pharma"},       # 5 jobs, 5 US      (biotech)
# --- sweep C — fintech / banking / insurance ---
{"display_name": "Focus Financial Partners",       "ats": "greenhouse",      "slug": "focusfinancialpartners","industry": "banking"},     # 131 jobs, 122 US  (wealth-management holding)
{"display_name": "FinQuery",                       "ats": "lever",           "slug": "finquery",             "industry": "fintech"},      # 17 jobs, 13 US    (lease/spend accounting)
{"display_name": "Climate First Bank",             "ats": "greenhouse",      "slug": "climatefirstbank",     "industry": "banking"},      # 20 jobs, 20 US
{"display_name": "Capital Farm Credit",            "ats": "greenhouse",      "slug": "capitalfarmcredit",    "industry": "banking"},      # 16 jobs, 16 US    (ag lender)
{"display_name": "Credit Union of Colorado",       "ats": "greenhouse",      "slug": "creditunionofcolorado","industry": "banking"},      # 10 jobs, 10 US
{"display_name": "Finch",                          "ats": "lever",           "slug": "finch",                "industry": "fintech"},      # 9 jobs, 9 US      (HR/payroll API)
{"display_name": "Finix",                          "ats": "lever",           "slug": "finix",                "industry": "fintech"},      # 9 jobs, 9 US      (payments)
{"display_name": "Combined Insurance",             "ats": "smartrecruiters", "slug": "CombinedInsurance1",   "industry": "insurance"},    # 2 jobs, 2 US      (Chubb subsidiary)
{"display_name": "Farmers Insurance",              "ats": "smartrecruiters", "slug": "FarmersInsurance6",    "industry": "insurance"},    # 2 jobs, 2 US
{"display_name": "Pronto Insurance",               "ats": "smartrecruiters", "slug": "ProntoInsurance1",     "industry": "insurance"},    # 1 job, 1 US
# --- sweep D — consumer / retail / manufacturing / media / nonprofit / education ---
{"display_name": "CROSSMARK",                      "ats": "smartrecruiters", "slug": "CROSSMARK1",           "industry": "consulting"},         # 200 jobs, 200 US  (retail merchandising services)
{"display_name": "Red Wing Shoe Company",          "ats": "smartrecruiters", "slug": "RedWingShoeCompany",   "industry": "ecommerce_retail"},   # 52 jobs, 52 US    (footwear)
{"display_name": "Kennametal",                     "ats": "smartrecruiters", "slug": "Kennametal1",          "industry": "manufacturing_automotive"},  # 37 jobs, 37 US (tooling/materials)
{"display_name": "The Normal Brand",               "ats": "workable",        "slug": "the-normal-brand",     "industry": "ecommerce_retail"},   # 29 jobs, 29 US    (apparel DTC)
{"display_name": "Huckberry",                      "ats": "workable",        "slug": "huckberry",            "industry": "ecommerce_retail"},   # 12 jobs, 12 US    (apparel/outdoor DTC)
{"display_name": "BCI Brands",                     "ats": "workable",        "slug": "bci-brands",           "industry": "ecommerce_retail"},   # 10 jobs, 10 US    (apparel)
{"display_name": "Progressive Metal Manufacturing","ats": "smartrecruiters", "slug": "ProgressiveMetalManufacturingCom","industry": "manufacturing_automotive"}, # 6 jobs, 6 US
{"display_name": "OUAI",                           "ats": "workable",        "slug": "theouai",              "industry": "ecommerce_retail"},   # 2 jobs, 2 US      (beauty DTC)
{"display_name": "Hagie Manufacturing",            "ats": "smartrecruiters", "slug": "HagieManufacturingCompany","industry": "manufacturing_automotive"},  # 1 job, 1 US (ag sprayers)
{"display_name": "ConsumerAffairs",                "ats": "workable",        "slug": "consumeraffairs-1",    "industry": "media"},              # 3 jobs, 3 US      (reviews/marketplace)
{"display_name": "ProPublica",                     "ats": "greenhouse",      "slug": "propublica",           "industry": "media"},              # 7 jobs, 7 US      (nonprofit newsroom)
{"display_name": "Columbia University",            "ats": "smartrecruiters", "slug": "ColumbiaUniversity1",  "industry": "education"},          # 28 jobs, 28 US    (education — Columbia is SmartRecruiters, NOT Workday)
{"display_name": "Augsburg University",            "ats": "smartrecruiters", "slug": "AugsburgUniversity-MinneapolisMN","industry": "education"}, # 43 jobs, 43 US    (education)
{"display_name": "World Central Kitchen",          "ats": "workable",        "slug": "world-central-kitchen","industry": "nonprofit"},          # 6 jobs, 6 US      (nonprofit)
{"display_name": "Nonprofit Finance Fund",         "ats": "greenhouse",      "slug": "nonprofitfinancefund", "industry": "nonprofit"},          # 1 job, 1 US       (nonprofit CDFI)
# sweep dead (no_postings): Congo Brands (workable), JADCO Manufacturing (SR), Mercury Insurance (SR), Pie Insurance (SR), Visby Medical (lever).
# sweep already in catalog: Boulevard, Charlie Health, Fireworks AI, Formation Bio, Hex, Hightouch, Suno, Torq, WorkOS.
# ============================================================================
# SESSION-2 SWEEP (2026-07-18) — 17 new, validated via scripts/discover_career_pages.py
# (dedup vs CAREER_PAGES_CATALOG + live fetch + US-count). Industries use the §0 taxonomy.
# Discovered by a parallel non-tech-focused board-ATS web-search agent.
# ============================================================================
# --- healthcare / senior-care / care-coordination ---
{"display_name": "AssistRx",                       "ats": "workable",        "slug": "assist-rx",              "industry": "healthtech"},         # 92 jobs, 92 US    (specialty-pharma patient access)
{"display_name": "CareHarmony",                    "ats": "workable",        "slug": "care-harmony",           "industry": "healthtech"},         # 62 jobs, 62 US    (chronic-care coordination)
{"display_name": "CircleLink Health",              "ats": "workable",        "slug": "circlelink-health",      "industry": "healthtech"},         # 42 jobs, 42 US    (remote care management)
{"display_name": "Vatica Health",                  "ats": "workable",        "slug": "vatica-health",          "industry": "healthtech"},         # 10 jobs, 10 US    (risk-adjustment/care)
{"display_name": "Experience Senior Living",       "ats": "workable",        "slug": "experience-sl-co",       "industry": "healthtech"},         # 130 jobs, 130 US  (senior living)
{"display_name": "Lap of Love",                    "ats": "workable",        "slug": "lapoflovejobs",          "industry": "healthtech"},         # 942 jobs, 940 US  (veterinary hospice — ⚠ large mobile-vet network)
# --- consumer / retail / food brands ---
{"display_name": "Authentic Brands Group",         "ats": "smartrecruiters", "slug": "authenticbrandsgroup",   "industry": "ecommerce_retail"},   # 1 job, 1 US       (brand mgmt — Reebok/Forever 21)
{"display_name": "Mammoth Brands",                 "ats": "greenhouse",      "slug": "mammothbrands",          "industry": "ecommerce_retail"},   # 22 jobs, 21 US    (DTC — Harry's/Flamingo)
{"display_name": "Retail Business Services",       "ats": "smartrecruiters", "slug": "RetailBusinessServices", "industry": "ecommerce_retail"},   # 6 jobs, 6 US      (Ahold Delhaize USA services arm)
{"display_name": "Rise Baking Company",            "ats": "smartrecruiters", "slug": "RiseBakingCompany",      "industry": "ecommerce_retail"},   # 2 jobs, 2 US      (bakery manufacturer/food)
# --- banking / insurance ---
{"display_name": "Banner Bank",                    "ats": "lever",           "slug": "bannerbank",             "industry": "banking"},            # 64 jobs, 64 US    (Pacific NW bank)
{"display_name": "Redwood Credit Union",           "ats": "lever",           "slug": "redwoodcu",              "industry": "banking"},            # 33 jobs, 33 US    (CA credit union)
{"display_name": "SmartFinancial",                 "ats": "workable",        "slug": "smartfinancial",         "industry": "insurance"},          # 21 jobs, 21 US    (insurance marketplace/insurtech)
# --- manufacturing / real-estate / hospitality / other (NEW §0 keys) ---
{"display_name": "Fortune Brands",                 "ats": "smartrecruiters", "slug": "FortuneBrands",          "industry": "manufacturing_automotive"}, # 126 jobs, 126 US (home & security — Moen/Master Lock)
{"display_name": "Flats LLC",                      "ats": "workable",        "slug": "flats-llc",              "industry": "real_estate"},        # 5 jobs, 5 US      (residential property mgmt)
{"display_name": "Hospitality Staffing Solutions", "ats": "smartrecruiters", "slug": "HospitalityStaffingSolutions1","industry": "hospitality"},   # 5 jobs, 5 US      (hospitality staffing services)
{"display_name": "Certified Group",                "ats": "greenhouse",      "slug": "certifiedgroup",         "industry": "other"},              # 72 jobs, 64 US    (food/consumer-product testing labs — no clean key → other)
# session-2 dead (no_postings / error): Charlesgate Realty Group (workable/charlesgate), LARC Staffing (SR/larcstaffingllc), Karya Property Management (workable — timeout), PAI (recruitee — HTTPError).
# session-2 already in catalog: Equinox (SR).
# ============================================================================
# SESSION-3 SWEEP (2026-07-18) — 36 new, validated via scripts/discover_career_pages.py.
# Two parallel non-tech board-ATS agents (mfg/logistics/construction/DTC + health/edu/nonprofit/finance).
# ============================================================================
# --- healthcare / senior-care / behavioral / biotech ---
{"display_name": "Senior Lifestyle",               "ats": "smartrecruiters", "slug": "SeniorLifestyle1",       "industry": "healthtech"},         # 200 jobs, 200 US  (senior living)
{"display_name": "Senior Resource Group",          "ats": "smartrecruiters", "slug": "SeniorResourceGroupLLC", "industry": "healthtech"},         # 95 jobs, 95 US    (senior living)
{"display_name": "Millennium Health",              "ats": "workable",        "slug": "millennium-health",      "industry": "healthtech"},         # 89 jobs, 89 US    (diagnostics lab)
{"display_name": "Kittitas Valley Healthcare",     "ats": "smartrecruiters", "slug": "KittitasValleyHealthcare","industry": "healthtech"},        # 52 jobs, 52 US    (WA hospital district)
{"display_name": "WES Health System",              "ats": "workable",        "slug": "wes-health-system-2",    "industry": "healthtech"},         # 33 jobs, 33 US    (behavioral health)
{"display_name": "San Antonio Behavioral Health",  "ats": "workable",        "slug": "sanantoniobehavioral",   "industry": "healthtech"},         # 25 jobs, 25 US    (behavioral health)
{"display_name": "Texas Health Action",            "ats": "workable",        "slug": "texashealthaction",      "industry": "healthtech"},         # 12 jobs, 12 US    (nonprofit health clinics)
{"display_name": "BridgeBio Pharma",               "ats": "greenhouse",      "slug": "bridgebio",              "industry": "pharma"},             # 110 jobs, 108 US
{"display_name": "Twist Bioscience",               "ats": "greenhouse",      "slug": "twistbioscience",        "industry": "pharma"},             # 24 jobs, 21 US    (synthetic biology)
# --- consumer / retail / food / DTC brands ---
{"display_name": "Faherty Brand",                  "ats": "greenhouse",      "slug": "fahertybrand",           "industry": "ecommerce_retail"},   # 110 jobs, 110 US  (apparel DTC)
{"display_name": "Hill House Home",                "ats": "greenhouse",      "slug": "hillhousehome",          "industry": "ecommerce_retail"},   # 44 jobs, 44 US    (apparel/home DTC)
{"display_name": "Prestage Foods",                 "ats": "workable",        "slug": "prestage",               "industry": "ecommerce_retail"},   # 54 jobs, 54 US    (pork producer/food)
{"display_name": "Spindrift",                      "ats": "workable",        "slug": "spindrift",              "industry": "ecommerce_retail"},   # 8 jobs, 8 US      (beverage)
{"display_name": "Hint",                           "ats": "workable",        "slug": "hint",                   "industry": "ecommerce_retail"},   # 1 job, 1 US       (beverage)
{"display_name": "NAPA Auto Parts",                "ats": "smartrecruiters", "slug": "NAPAAutoParts33",        "industry": "ecommerce_retail"},   # 7 jobs, 7 US      (auto-parts retail)
{"display_name": "Sonic Automotive",               "ats": "smartrecruiters", "slug": "SonicAutomotive",        "industry": "ecommerce_retail"},   # 200 jobs, 200 US  (auto dealer group)
{"display_name": "Hayward Lumber",                 "ats": "workable",        "slug": "hayward-lumber-1",       "industry": "ecommerce_retail"},   # 54 jobs, 54 US    (building-materials dealer)
# --- manufacturing / industrial ---
{"display_name": "O-I (Owens-Illinois)",           "ats": "smartrecruiters", "slug": "O-I",                    "industry": "manufacturing_automotive"}, # 28 jobs, 28 US (glass packaging)
{"display_name": "Ulbrich Stainless Steels",       "ats": "smartrecruiters", "slug": "UlbrichSteel",           "industry": "manufacturing_automotive"}, # 8 jobs, 8 US  (specialty metals)
{"display_name": "KSI Auto Parts",                 "ats": "smartrecruiters", "slug": "KSIAutoParts",           "industry": "manufacturing_automotive"}, # 1 job, 1 US   (auto-parts distribution)
# --- energy (solar) ---
{"display_name": "Silfab Solar",                   "ats": "smartrecruiters", "slug": "silfabsolar",            "industry": "energy"},             # 45 jobs, 45 US    (solar module mfr)
{"display_name": "Freedom Solar Power",            "ats": "lever",           "slug": "freedomsolarpower",      "industry": "energy"},             # 24 jobs, 24 US    (residential solar)
# --- consulting / engineering (AEC) ---
{"display_name": "Langan Engineering",             "ats": "greenhouse",      "slug": "langanengineeringandenvironmentalservicesllc","industry": "consulting"}, # 364 jobs, 355 US (civil/geotech eng.)
{"display_name": "Apex Companies",                 "ats": "greenhouse",      "slug": "apexcompanies",          "industry": "consulting"},         # 190 jobs, 190 US  (environmental/water services)
{"display_name": "Woolpert",                       "ats": "greenhouse",      "slug": "woolpert",               "industry": "consulting"},         # 119 jobs, 105 US  (architecture/eng/geospatial)
{"display_name": "MGAC",                           "ats": "greenhouse",      "slug": "mgac",                   "industry": "consulting"},         # 28 jobs, 27 US    (construction project mgmt)
# --- real_estate (property mgmt / SFR) ---
{"display_name": "Asset Living",                   "ats": "greenhouse",      "slug": "assetliving",            "industry": "real_estate"},        # 33 jobs, 33 US    (property management)
{"display_name": "Weinstein Properties",           "ats": "greenhouse",      "slug": "weinsteinproperties",    "industry": "real_estate"},        # 65 jobs, 65 US    (apartment owner/operator)
{"display_name": "Roofstock",                      "ats": "greenhouse",      "slug": "roofstock",              "industry": "real_estate"},        # 57 jobs, 57 US    (SFR investing platform)
# --- insurance / education / nonprofit ---
{"display_name": "GEICO",                          "ats": "smartrecruiters", "slug": "GEICO",                  "industry": "insurance"},          # 1 job, 1 US       (⚠ SR board thin — most GEICO roles elsewhere)
{"display_name": "Unitek Learning",                "ats": "smartrecruiters", "slug": "UnitekLearning",         "industry": "education"},          # 180 jobs, 180 US  (nursing/healthcare schools)
{"display_name": "Amity Foundation",               "ats": "lever",           "slug": "amityfdn",               "industry": "nonprofit"},          # 130 jobs, 130 US  (addiction recovery)
{"display_name": "ClimateWorks Foundation",        "ats": "workable",        "slug": "climateworks-foundation-1","industry": "nonprofit"},        # 2 jobs, 1 US      (climate philanthropy — ⚠ low US)
# --- logistics / freight (NO clean key yet → 'other'; candidate for a future `transportation`/`logistics` key) ---
{"display_name": "PS Logistics",                   "ats": "smartrecruiters", "slug": "PSLogistics",            "industry": "other"},              # 49 jobs, 49 US    (flatbed trucking/3PL)
{"display_name": "Circle Logistics",               "ats": "smartrecruiters", "slug": "CircleLogistics1",       "industry": "other"},              # 48 jobs, 48 US    (freight brokerage/3PL)
{"display_name": "Double D Transportation",        "ats": "workable",        "slug": "double-d-transportation","industry": "other"},              # 5 jobs, 5 US      (trucking)
# session-3 dead (no_postings): America's Credit Union (SR), Greater Texas CU (SR/GTFCU1), Challenge Manufacturing (SR), Nuvalent (greenhouse), Whitfield Transportation (workable).
# session-3 already in catalog: Included Health, Legend Biotech, Natera, Northwestern Medicine, OLIPOP, Reformation.
# ============================================================================
# SESSION-4 SWEEP (2026-07-19) — 15 new, validated via scripts/discover_career_pages.py.
# Mixed-sector board-ATS agent (property mgmt, home-health, restaurants, insurance, ed-tech, media).
# ============================================================================
# --- real_estate — property mgmt / student & multifamily housing ---
{"display_name": "The Scion Group",                "ats": "greenhouse",      "slug": "thesciongroupllc",       "industry": "real_estate"},        # 387 jobs, 387 US  (student housing operator)
{"display_name": "Hillpointe",                     "ats": "greenhouse",      "slug": "hillpointe",             "industry": "real_estate"},        # 42 jobs, 42 US    (multifamily dev)
{"display_name": "John M. Corcoran & Company",     "ats": "greenhouse",      "slug": "johnmcorcorancompany",   "industry": "real_estate"},        # 23 jobs, 23 US    (property management)
{"display_name": "Ackermann Group",                "ats": "greenhouse",      "slug": "ackermanngroup",         "industry": "real_estate"},        # 7 jobs, 7 US      (real-estate dev/mgmt)
# --- healthcare — home health / hospice ---
{"display_name": "Kinder Hearts Home Health",      "ats": "smartrecruiters", "slug": "KinderHeartsHomeHealth", "industry": "healthtech"},         # 10 jobs, 10 US
{"display_name": "Connected Home Health & Hospice","ats": "smartrecruiters", "slug": "ConnectedHomeHealthHospice","industry": "healthtech"},      # 2 jobs, 2 US
# --- hospitality / consumer / retail ---
{"display_name": "Hillstone Restaurant Group",     "ats": "smartrecruiters", "slug": "HillstoneRestaurantGroup","industry": "hospitality"},       # 200 jobs, 200 US  (restaurants)
{"display_name": "Lakeshore Learning Materials",   "ats": "smartrecruiters", "slug": "Lakeshore",              "industry": "ecommerce_retail"},   # 61 jobs, 61 US    (educational products retailer)
{"display_name": "Frette",                         "ats": "smartrecruiters", "slug": "Frette",                 "industry": "ecommerce_retail"},   # 16 jobs, 16 US    (luxury linens DTC)
{"display_name": "Skylight",                       "ats": "workable",        "slug": "skylight-frame",         "industry": "ecommerce_retail"},   # 13 jobs, 13 US    (consumer electronics DTC)
# --- insurance ---
{"display_name": "Slide Insurance",                "ats": "greenhouse",      "slug": "slideinsurance",         "industry": "insurance"},          # 20 jobs, 20 US    (insurtech — FL homeowners)
{"display_name": "Lamb Insurance Services",        "ats": "greenhouse",      "slug": "lambcareers",            "industry": "insurance"},          # 1 job, 1 US       (nonprofit-focused broker)
# --- education / media / logistics ---
{"display_name": "Ellevation Education",           "ats": "lever",           "slug": "ellevationeducation",    "industry": "education"},          # 2 jobs, 2 US      (ed-tech for EL students)
{"display_name": "Vevo",                           "ats": "lever",           "slug": "vevo",                   "industry": "media"},              # 8 jobs, 6 US      (music video network)
{"display_name": "Cardinal Logistics",             "ats": "smartrecruiters", "slug": "CardinalLogistics1",     "industry": "other"},              # 8 jobs, 8 US      (trucking/3PL — no clean key → other)
# session-4 dead: 174 Power Global (greenhouse/174powerglobal — HTTPError), Age of Learning (lever/aofl — no_postings).
# session-4 already in catalog: Amwins, GiveDirectly, Perch Energy.
# session-4 dup vs this doc (skipped): Retail Business Services, Kittitas Valley Healthcare, Red Wing Shoe, Kennametal, Congo Brands, JADCO Manufacturing.
# ============================================================================
# SESSION-5 SWEEP (2026-07-19) — 10 new, validated via scripts/discover_career_pages.py.
# Mixed board-ATS agent emphasizing biotech/med-device, GAMING, cleantech, food.
# ============================================================================
# --- healthtech — medical devices / diagnostics / specialty-pharmacy / RPM ---
{"display_name": "Shields Health Solutions",       "ats": "greenhouse",      "slug": "shieldshealthsolutions", "industry": "healthtech"},         # 98 jobs, 98 US    (specialty pharmacy services)
{"display_name": "Circadia Health",                "ats": "lever",           "slug": "circadiahealth",         "industry": "healthtech"},         # 25 jobs, 22 US    (remote patient monitoring)
{"display_name": "Avive Solutions",                "ats": "lever",           "slug": "AviveSolutions",         "industry": "healthtech"},         # 21 jobs, 21 US    (AED devices)
{"display_name": "CVRx",                           "ats": "lever",           "slug": "CVRx",                   "industry": "healthtech"},         # 7 jobs, 7 US      (cardiac neuromodulation device)
{"display_name": "Cellanome",                      "ats": "greenhouse",      "slug": "cellanome",              "industry": "healthtech"},         # 7 jobs, 7 US      (single-cell biology tools)
# --- gaming (first entries under the existing `gaming` key) ---
{"display_name": "Cloud Chamber",                  "ats": "greenhouse",      "slug": "cloudchamberen",         "industry": "gaming"},             # 16 jobs, 8 US     (2K studio — BioShock)
{"display_name": "Exploding Kittens",              "ats": "lever",           "slug": "explodingkittens",       "industry": "gaming"},             # 5 jobs, 5 US      (games — card + mobile)
# --- energy (community/commercial solar) ---
{"display_name": "Solar Landscape",                "ats": "lever",           "slug": "solarlandscape",         "industry": "energy"},             # 25 jobs, 25 US    (community solar)
{"display_name": "Pivot Energy",                   "ats": "lever",           "slug": "pivotenergy",            "industry": "energy"},             # 9 jobs, 9 US      (solar developer)
# --- food ---
{"display_name": "J&J Snack Foods",                "ats": "greenhouse",      "slug": "jjsnackfoods",           "industry": "ecommerce_retail"},   # 72 jobs, 72 US    (branded snack foods)
# session-5 dead (no_postings): BlueRock Therapeutics (lever/bluerocktx), Wildlight Entertainment (lever/wildlight).
# session-5 already in catalog: Coalition, Ethos Life, Ghost Story Games.
```

**Already in the career-pages catalog (skipped):** Peloton (`smartrecruiters/pelotoninc`),
Databricks + Palo Alto Networks (batch 5), Cloudflare (batch 8). The ~614 catalog is already
comprehensive for well-known tech — most big tech names on Greenhouse/Lever/Ashby are present.

**Batch 8 non-tech giants — NOT addable** (unsupported ATS / gated): Kaiser Permanente (Taleo),
CommonSpirit (iCIMS), Providence/REI/Ross Stores (custom), Hyatt (Taleo), Petco (Workday but the
endpoint gates → 422), CBRE/Cushman (`cw.wd1` Workday but global-heavy, not US-focused).

## 5. Seen but NOT addable to career-pages (unsupported ATS)
Route these via the MNC `aggregator` engine instead — do not add here:
SiriusXM, Rivian (main), Delta, Hilton, Fluor, NextEra, General Mills,
Colgate-Palmolive, Progressive, Starbucks, Aflac, Chubb, Kroger, Dollar General.

---

## 6. Next steps (later)
- [x] **Industry taxonomy decided** — see [workday-tenant-expansion.md §0](workday-tenant-expansion.md#0-industry-taxonomy--new-category-keys--ui-labels-decided-this-session); the 4 `other` rows here were relabeled (`education`/`nonprofit`), session-2 rows tagged directly.
- [ ] Confirm the `Rivian & VW Group Technologies` naming (it's the Rivian–VW JV board, `rivianvw.tech`, not Rivian proper — decide display name).
- [ ] Add the finalized rows to `services/career_pages_catalog.py` **and** add the 6 new labels to the frontend `INDUSTRY_LABELS` (§0.3 of the Workday doc — one shared edit covers both tabs).
