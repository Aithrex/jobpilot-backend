# H-1B sponsor coverage gap list

_Generated 2026-09-16T17:47:21.497116+00:00 · `sponsor-gap-1.0.0` · read-only_

## What this measures

Every employer in `sponsorship_signals` (USCIS H-1B Employer Data Hub) joined against `employers`, `career_sources`, `company_ats_endpoints` and live `job_postings`. The demand signal is the employer's OWN petition count; the coverage signal is ADR-006's — only `validated` and `empty` career sources count, and a posting count is the number we actually hold today.

**Data vintage: USCIS fiscal year 2026 (single fiscal year).** Source: USCIS H-1B Employer Data Hub.

> **The window is 1 fiscal year(s), not 5.** `sponsorship_service.DATA_WINDOW_YEARS` is 5 and every headline it renders says "in the last 5 years". Until that is fixed the product over-states the window for these rows; every document carries `data_window.service_claim_mismatch: true` so the mismatch is queryable, not folklore.

## Totals

- sponsors measured: **7,204** (of 42,814 in the collection; `--min-approvals 5`)
- their FY approvals: **219,727**
- active postings we hold for them today: **84,210** (20,809 core-tech)

## Buckets

| bucket | employers | approvals | software-weighted | postings today | est. postings if covered |
|---|---:|---:|---:|---:|---:|
| `covered` | 594 | 60,402 | 39,641 | 80,609 | — |
| `covered_under_read` | 45 | 7,963 | 6,276 | 3,601 | ~869 |
| `board_publishes_jobs_we_hold_none` | 32 | 1,484 | 1,037 | 0 | ~403 |
| `validated_but_zero_postings` | 13 | 2,424 | 1,904 | 0 | ~251 |
| `source_unvalidated` | 148 | 38,771 | 29,794 | 0 | ~2,367 |
| `no_career_source` | 179 | 4,418 | 2,463 | 0 | ~1,426 |
| `no_employer_row` | 6,193 | 104,265 | 50,469 | 0 | ~34,903 |

`est. postings if covered` is a MODEL, not a measurement: the median active postings held by sponsors we already have an `employers` row for, in the same approvals band, zeros included. It answers "is this bucket worth a week?" and nothing finer.

## The ranked gap list — 6,610 uncovered sponsors by `approvals x software_likelihood x conversion`

The first 100 are below. `h1b-sponsor-gaps.csv` and `.json` carry the top 1,000.

| # | employer | approvals | sw | conv | score | bucket | NAICS | where | postings |
|---:|---|---:|---:|---:|---:|---|---|---|---:|
| 1 | Apple | 3,885 | 0.85 | 1.00 | 3,302 | `source_unvalidated` | 31-33 | Cupertino, CA | 0 |
| 2 | Google | 3,183 | 0.85 | 1.00 | 2,706 | `source_unvalidated` | 54 | Mountain View, CA | 0 |
| 3 | Meta | 2,563 | 0.85 | 1.00 | 2,178 | `source_unvalidated` | 51 | Menlo Park, CA | 0 |
| 4 | Tesla | 1,574 | 0.75 | 1.00 | 1,180 | `source_unvalidated` | 31-33 | Austin, TX | 0 |
| 5 | IBM | 1,230 | 0.85 | 1.00 | 1,046 | `source_unvalidated` | 54 | Durham, NC | 0 |
| 6 | Accenture | 1,251 | 0.80 | 1.00 | 1,001 | `covered_under_read` | 54 | Chicago, IL | 85 |
| 7 | EY (Ernst & Young) | 1,359 | 0.65 | 1.00 | 883 | `no_employer_row` | 54 | Secaucus, NJ | 0 |
| 8 | Infosys | 4,152 | 0.80 | 0.25 | 830 | `source_unvalidated` | 54 | Richardson, TX | 0 |
| 9 | TT COMMERCE AND GLOBAL SERVICES LLC | 1,070 | 0.75 | 1.00 | 801 | `no_employer_row` | 51 | Culver City, CA | 0 |
| 10 | Tata Consultancy Services (TCS) | 4,450 | 0.65 | 0.25 | 723 | `source_unvalidated` | 54 | Rockville, MD | 0 |
| 11 | Cognizant | 3,530 | 0.80 | 0.25 | 706 | `source_unvalidated` | 54 | College Station, TX | 0 |
| 12 | Goldman Sachs | 1,050 | 0.60 | 1.00 | 630 | `source_unvalidated` | 52 | New York, NY | 0 |
| 13 | Intuit | 640 | 0.85 | 1.00 | 544 | `source_unvalidated` | 54 | Mountain View, CA | 0 |
| 14 | Adobe | 525 | 0.85 | 1.00 | 446 | `covered_under_read` | 54 | San Jose, CA | 136 |
| 15 | BLOOMBERG LP | 587 | 0.75 | 1.00 | 440 | `source_unvalidated` | 51 | New York, NY | 0 |
| 16 | Uber | 480 | 0.85 | 1.00 | 408 | `board_publishes_jobs_we_hold_none` | 54 | San Francisco, CA | 0 |
| 17 | TikTok | 474 | 0.85 | 1.00 | 403 | `source_unvalidated` | 51 | Culver City, CA | 0 |
| 18 | McKinsey & Company | 467 | 0.80 | 1.00 | 374 | `no_career_source` | 54 | New York, NY | 0 |
| 19 | ByteDance | 435 | 0.85 | 1.00 | 370 | `source_unvalidated` | 51 | San Jose, CA | 0 |
| 20 | Dell Technologies | 425 | 0.85 | 1.00 | 361 | `source_unvalidated` | 42 | Round Rock, TX | 0 |
| 21 | Intel | 478 | 0.70 | 1.00 | 335 | `covered_under_read` | 31-33 | Chandler, AZ | 163 |
| 22 | SAP | 393 | 0.85 | 1.00 | 334 | `validated_but_zero_postings` | 54 | Newtown Square, PA | 0 |
| 23 | Capgemini | 1,612 | 0.80 | 0.25 | 322 | `validated_but_zero_postings` | 54 | Chicago, IL | 0 |
| 24 | HCLTech | 1,525 | 0.80 | 0.25 | 305 | `source_unvalidated` | 54 | Santa Clara, CA | 0 |
| 25 | Ford | 627 | 0.45 | 1.00 | 282 | `source_unvalidated` | 31-33 | Dearborn, MI | 0 |
| 26 | CHARTER COMMUNICATIONS INC | 371 | 0.75 | 1.00 | 278 | `no_employer_row` | 51 | St Louis, MO | 0 |
| 27 | Boston Consulting Group | 405 | 0.65 | 1.00 | 263 | `no_employer_row` | 54 | Boston, MA | 0 |
| 28 | LTIMindtree | 1,289 | 0.80 | 0.25 | 258 | `source_unvalidated` | 54 | Edison, NJ | 0 |
| 29 | Rivian | 325 | 0.75 | 1.00 | 244 | `source_unvalidated` | 31-33 | Irvine, CA | 0 |
| 30 | Wipro | 1,192 | 0.80 | 0.25 | 238 | `source_unvalidated` | 54 | East Brunswick, NJ | 0 |
| 31 | Siemens | 274 | 0.85 | 1.00 | 233 | `source_unvalidated` | 51 | Plano, TX | 0 |
| 32 | COMCAST CABLE COMMUNICATIONS, LLC | 307 | 0.75 | 1.00 | 230 | `no_employer_row` | 51 | Philadelphia, PA | 0 |
| 33 | Optum (UnitedHealth) | 335 | 0.63 | 1.00 | 212 | `no_employer_row` | 54 | Minnetonka, MN | 0 |
| 34 | KPMG | 295 | 0.65 | 1.00 | 192 | `no_employer_row` | 54 | Dallas, TX | 0 |
| 35 | COMPUNNEL SOFTWARE GROUP INC | 953 | 0.75 | 0.25 | 179 | `no_employer_row` | 54 | Princeton, NJ | 0 |
| 36 | CUMMINS INC. | 398 | 0.45 | 1.00 | 179 | `no_employer_row` | 31-33 | Nashville, TN | 0 |
| 37 | ADP TECHNOLOGY SERVICES INC | 219 | 0.75 | 1.00 | 164 | `no_employer_row` | 51 | Roseland, NJ | 0 |
| 38 | NOKIA OF AMERICA CORPORATION | 219 | 0.75 | 1.00 | 164 | `no_employer_row` | 51 | Murray Hill, NJ | 0 |
| 39 | Microsoft | 3,688 | 0.85 | 0.05 | 157 | `covered_under_read` | 51 | Redmond, WA | 19 |
| 40 | ROCKET LIMITED PARTNERSHIP | 230 | 0.65 | 1.00 | 149 | `no_employer_row` | 54 | Detroit, MI | 0 |
| 41 | Texas Instruments | 202 | 0.70 | 1.00 | 141 | `covered_under_read` | 31-33 | Dallas, TX | 88 |
| 42 | SNAP INC | 159 | 0.85 | 1.00 | 135 | `covered_under_read` | 51 | Santa Monica, CA | 35 |
| 43 | MathWorks | 178 | 0.75 | 1.00 | 134 | `no_employer_row` | 51 | Natick, MA | 0 |
| 44 | Mphasis | 657 | 0.80 | 0.25 | 131 | `source_unvalidated` | 54 | New York, NY | 0 |
| 45 | Tech Mahindra | 636 | 0.80 | 0.25 | 127 | `source_unvalidated` | 54 | Bedminster, NJ | 0 |
| 46 | ATLASSIAN US INC | 135 | 0.85 | 1.00 | 115 | `source_unvalidated` | 54 | Mountain View, CA | 0 |
| 47 | ARM INC | 199 | 0.56 | 1.00 | 112 | `source_unvalidated` | 54 | San Jose, CA | 0 |
| 48 | ZS ASSOCIATES INC | 172 | 0.65 | 1.00 | 112 | `no_employer_row` | 54 | Evanston, IL | 0 |
| 49 | ZENSAR TECHNOLOGIES INC | 137 | 0.80 | 1.00 | 110 | `covered_under_read` | 54 | Chicago, IL | 105 |
| 50 | CGI TECHNOLOGIES AND SOLUTIONS INC | 557 | 0.75 | 0.25 | 104 | `no_employer_row` | 54 | Fairfax, VA | 0 |
| 51 | UBS BUSINESS SOLUTIONS US LLC | 208 | 0.50 | 1.00 | 104 | `no_employer_row` | 52 | Nashville, TN | 0 |
| 52 | AMGEN INC | 158 | 0.65 | 1.00 | 103 | `covered_under_read` | 54 | Thousand Oaks, CA | 318 |
| 53 | NETAPP INC | 127 | 0.80 | 1.00 | 102 | `validated_but_zero_postings` | 54 | San Jose, CA | 0 |
| 54 | MANHATTAN ASSOCIATES INC | 114 | 0.85 | 1.00 | 97 | `no_career_source` | 54 | Atlanta, GA | 0 |
| 55 | VMWARE LLC | 112 | 0.85 | 1.00 | 95 | `covered_under_read` | 54 | Palo Alto, CA | 64 |
| 56 | AKAMAI TECHNOLOGIES INC | 118 | 0.80 | 1.00 | 94 | `no_career_source` | 51 | Cambridge, MA | 0 |
| 57 | DB GLOBAL TECHNOLOGY INC | 125 | 0.75 | 1.00 | 94 | `no_employer_row` | 52 | Cary, NC | 0 |
| 58 | Charles Schwab | 266 | 0.35 | 1.00 | 93 | `no_employer_row` | 52 | Westlake, TX | 0 |
| 59 | WSP USA INC | 140 | 0.65 | 1.00 | 91 | `no_career_source` | 54 | New York, NY | 0 |
| 60 | Anthropic | 143 | 0.85 | 0.74 | 90 | `covered_under_read` | 54 | San Francisco, CA | 42 |
| 61 | UT-BATTELLE LLC ORNL | 135 | 0.65 | 1.00 | 88 | `no_employer_row` | 54 | Oak Ridge, TN | 0 |
| 62 | SYNOPSYS INC | 119 | 0.70 | 1.00 | 83 | `source_unvalidated` | 54 | Sunnyvale, CA | 0 |
| 63 | TSMC | 118 | 0.70 | 1.00 | 83 | `source_unvalidated` | 31-33 | Phoenix, AZ | 0 |
| 64 | MASSACHUSETTS INSTITUTE OF TECHNOLOGY | 110 | 0.75 | 1.00 | 82 | `no_employer_row` | 61 | Cambridge, MA | 0 |
| 65 | GENENTECH USA INC | 122 | 0.65 | 1.00 | 79 | `no_career_source` | 54 | South San Francisco, CA | 0 |
| 66 | THERMO FISHER SCIENTIFIC INC | 122 | 0.65 | 1.00 | 79 | `source_unvalidated` | 54 | Waltham, MA | 0 |
| 67 | AECOM TECHNICAL SERVICES INC | 120 | 0.65 | 1.00 | 78 | `no_employer_row` | 54 | Dallas, TX | 0 |
| 68 | HCL GLOBAL SYSTEMS LLC | 120 | 0.65 | 1.00 | 78 | `no_employer_row` | 54 | Farmington Hills, MI | 0 |
| 69 | Bain & Company | 119 | 0.65 | 1.00 | 78 | `source_unvalidated` | 54 | Boston, MA | 0 |
| 70 | MOODYS ANALYTICS INC | 103 | 0.75 | 1.00 | 77 | `no_employer_row` | 56 | New York, NY | 0 |
| 71 | SK HYNIX NAND PRODUCT SOLUTIONS CORP DBA S | 138 | 0.55 | 1.00 | 76 | `no_employer_row` | 31-33 | Rancho Cordova, CA | 0 |
| 72 | Collins Aerospace (RTX) | 168 | 0.45 | 1.00 | 76 | `source_unvalidated` | 31-33 | Cedar Rapids, IA | 0 |
| 73 | COTIVITI INC | 115 | 0.65 | 1.00 | 75 | `no_employer_row` | 54 | South Jordan, UT | 0 |
| 74 | RANDSTAD DIGITAL LLC | 395 | 0.75 | 0.25 | 74 | `no_employer_row` | 54 | Woburn, MA | 0 |
| 75 | DISH WIRELESS LLC | 96 | 0.75 | 1.00 | 72 | `no_employer_row` | 51 | Englewood, CO | 0 |
| 76 | SLALOM INC | 87 | 0.80 | 1.00 | 70 | `source_unvalidated` | 54 | Seattle, WA | 0 |
| 77 | UST | 427 | 0.65 | 0.25 | 69 | `no_employer_row` | 54 | Aliso Viejo, CA | 0 |
| 78 | INTUITIVE SURGICAL OPERATIONS INC | 154 | 0.45 | 1.00 | 69 | `no_employer_row` | 31-33 | Sunnyvale, CA | 0 |
| 79 | Marvell | 153 | 0.45 | 1.00 | 69 | `no_employer_row` | 31-33 | Santa Clara, CA | 0 |
| 80 | FCA US LLC | 152 | 0.45 | 1.00 | 68 | `no_employer_row` | 31-33 | Auburn Hills, MI | 0 |
| 81 | FIS MANAGEMENT SERVICES LLC | 105 | 0.65 | 1.00 | 68 | `no_employer_row` | 54 | New York, NY | 0 |
| 82 | CRUSOE ENERGY SYSTEMS LLC | 91 | 0.75 | 1.00 | 68 | `no_employer_row` | 51 | San Francisco, CA | 0 |
| 83 | INTRAEDGE, INC. | 104 | 0.65 | 1.00 | 68 | `no_employer_row` | 54 | Chandler, AZ | 0 |
| 84 | SCHLUMBERGER TECHNOLOGY CORPORATION | 90 | 0.75 | 1.00 | 68 | `no_employer_row` | 21 | Katy, TX | 0 |
| 85 | BIRLASOFT SOLUTIONS INC | 103 | 0.65 | 1.00 | 67 | `no_employer_row` | 54 | Edison, NJ | 0 |
| 86 | GRANT THORNTON ADVISORS LLC | 103 | 0.65 | 1.00 | 67 | `no_employer_row` | 54 | Chicago, IL | 0 |
| 87 | MASTECH DIGITAL TECHNOLOGIES, INC., A MAST | 89 | 0.75 | 1.00 | 67 | `no_employer_row` | 54 | Moon Township, PA | 0 |
| 88 | ERICSSON INC | 78 | 0.85 | 1.00 | 66 | `source_unvalidated` | 31-33 | Plano, TX | 0 |
| 89 | DIGIPULSE TECHNOLOGIES INC | 88 | 0.75 | 1.00 | 66 | `no_employer_row` | 54 | Cherry Hill, NJ | 0 |
| 90 | KPIT TECHNOLOGIES INC | 88 | 0.75 | 1.00 | 66 | `no_employer_row` | 54 | Novi, MI | 0 |
| 91 | PHOTON INFOTECH INC | 101 | 0.65 | 1.00 | 66 | `no_employer_row` | 54 | Dallas, TX | 0 |
| 92 | SCHNEIDER ELECTRIC USA INC | 77 | 0.85 | 1.00 | 66 | `source_unvalidated` | 31-33 | Boston, MA | 0 |
| 93 | ENVIRONMENTAL SYSTEMS RESEARCH INSTITUTE I | 87 | 0.75 | 1.00 | 65 | `no_employer_row` | 51 | Redlands, CA | 0 |
| 94 | MAPLEBEAR INC D B A INSTACART | 87 | 0.75 | 1.00 | 65 | `no_employer_row` | 51 | San Francisco, CA | 0 |
| 95 | DISH NETWORK LLC | 86 | 0.75 | 1.00 | 64 | `no_employer_row` | 51 | Englewood, CO | 0 |
| 96 | TATA TECHNOLOGIES INC | 85 | 0.75 | 1.00 | 64 | `no_employer_row` | 54 | Detroit, MI | 0 |
| 97 | ARTECH, LLC. AKA ARTECH INFORMATION SYSTEM | 98 | 0.65 | 1.00 | 63 | `no_employer_row` | 54 | Morristown, NJ | 0 |
| 98 | Lam Research | 90 | 0.70 | 1.00 | 63 | `validated_but_zero_postings` | 31-33 | Fremont, CA | 0 |
| 99 | Electronic Arts | 84 | 0.75 | 1.00 | 63 | `source_unvalidated` | 54 | Redwood City, CA | 0 |
| 100 | MSR TECHNOLOGY GROUP LLC | 84 | 0.75 | 1.00 | 63 | `no_employer_row` | 54 | Stockton, CA | 0 |
| … | _900 more rows in `h1b-sponsor-gaps.csv` / `.json`_ | | | | | | | | |

## The inverse: employers we carry who sponsor nobody

458 employers we already hold **12,753 active postings** for (2,079 core-tech) filed **no H-1B petition at all** in this window. The USCIS Data Hub lists every employer that filed, so this is a measurement, not an absence of data — `sponsorship_signals` carries an explicit `census_absent` row for each so the badge can say it.

| employer | active postings | core-tech |
|---|---:|---:|
| Repwest Insurance Company | 1,272 | 26 |
| Northrop Grumman | 894 | 438 |
| The Talbots, LLC | 726 | 3 |
| MARGARITAVILLE, INC. | 714 | 12 |
| COBAS SERVICES LLC | 449 | 141 |
| CENTIMARK CORPORATION | 425 | 0 |
| EquipmentShare.com Inc | 354 | 20 |
| CROSSROADS AUTO BODY, INC. | 317 | 0 |
| Anduril Industries | 285 | 192 |
| Chanel | 237 | 18 |
| SpartanNash | 229 | 1 |
| AdaptHealth Corp. | 183 | 0 |
| FERRELLGAS, INC. | 180 | 0 |
| MASONICARE HEALTH CENTER | 180 | 0 |
| LAUREATE, LTD. | 174 | 0 |

## How to use this

`h1b-sponsor-gaps.csv` is the work queue, already ordered. For each row: `employer_norm` is the `sponsorship_signals` key, `domain` is populated ONLY when the `employers` registry already holds one (`domain_source` says which kind of evidence) — an empty `domain` means we do not know it, never that it does not exist. Do not derive one from the name; that is the mistake ADR-006 exists to stop.

Work the buckets in this order, which is cheapest-proof-first. The first three are NOT discovery work and must not be sent to a discovery pass:

1. `covered_under_read` — we have the board and read part of it. Eightfold's `page_size=20` against a 10-row API cost Microsoft 1,149 postings, Qualcomm 737 and Lockheed ~1,480; no amount of discovery would have found them.
2. `board_publishes_jobs_we_hold_none` — the board is proven ours and last published jobs we hold none of. Re-read the endpoint.
3. `validated_but_zero_postings` — proven board, and it published nothing either. Believe it, or check the endpoint address (CLAUDE.md: asking the wrong URL reads as a broken board).
4. `source_unvalidated` — the board is known and unproven. This is `verify_career_sources.py --mode backlink`, not a new network sweep.
5. `no_career_source` — the employer is known, the board is not. Discovery, one employer at a time.
6. `no_employer_row` — we have never heard of them. Cheapest to add a registry row, longest path to a posting.

**Read `conversion` before you spend a day on a row.** Approvals are demand; `conversion` is how much of it could become inventory. The client-site staffing cohort (Infosys, TCS, Cognizant, Wipro, HCLTech, Capgemini, LTIMindtree, Tech Mahindra, Mphasis — 14,421 approvals between them, more than Apple, Microsoft and Google combined) hires against client requisitions that never reach a careers site. LTIMindtree is the proof: its board is reachable, owned and fully readable, and it publishes 54 jobs, none of them in the US.

Run ONE network pass at a time (CLAUDE.md: a content pass beside two others turned 830 sources `blocked` in one sweep).
