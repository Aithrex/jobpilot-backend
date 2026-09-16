# H-1B sponsor coverage gap list

_Generated 2026-09-16T06:59:16.882826+00:00 · `sponsor-gap-1.0.0` · read-only_

## What this measures

Every employer in `sponsorship_signals` (USCIS H-1B Employer Data Hub) joined against `employers`, `career_sources`, `company_ats_endpoints` and live `job_postings`. The demand signal is the employer's OWN petition count; the coverage signal is ADR-006's — only `validated` and `empty` career sources count, and a posting count is the number we actually hold today.

**Data vintage: USCIS fiscal year 2026 (single fiscal year).** Source: USCIS H-1B Employer Data Hub.

> **The window is 1 fiscal year(s), not 5.** `sponsorship_service.DATA_WINDOW_YEARS` is 5 and every headline it renders says "in the last 5 years". Until that is fixed the product over-states the window for these rows; every document carries `data_window.service_claim_mismatch: true` so the mismatch is queryable, not folklore.

## Totals

- sponsors measured: **7,226** (of 42,420 in the collection; `--min-approvals 5`)
- their FY approvals: **219,641**
- active postings we hold for them today: **61,539** (14,748 core-tech)

## Buckets

| bucket | employers | approvals | software-weighted | postings today | est. postings if covered |
|---|---:|---:|---:|---:|---:|
| `covered` | 592 | 64,590 | 43,990 | 61,539 | — |
| `validated_but_zero_postings` | 49 | 4,057 | 3,002 | 0 | ~518 |
| `source_unvalidated` | 152 | 38,685 | 29,674 | 0 | ~1,797 |
| `no_career_source` | 182 | 4,470 | 2,494 | 0 | ~1,121 |
| `no_employer_row` | 6,251 | 107,839 | 52,015 | 0 | ~29,663 |

`est. postings if covered` is a MODEL, not a measurement: the median active postings held by sponsors we already have an `employers` row for, in the same approvals band, zeros included. It answers "is this bucket worth a week?" and nothing finer.

## The ranked gap list — 6,634 uncovered sponsors by `approvals x software_likelihood`

The first 100 are below. `h1b-sponsor-gaps.csv` and `.json` carry the top 1,000.

| # | employer | approvals | sw | score | bucket | NAICS | where | postings |
|---:|---|---:|---:|---:|---|---|---|---:|
| 1 | Infosys | 4,152 | 0.80 | 3,322 | `source_unvalidated` | 54 | Richardson, TX | 0 |
| 2 | Apple | 3,885 | 0.85 | 3,302 | `source_unvalidated` | 31-33 | Cupertino, CA | 0 |
| 3 | Tata Consultancy Services (TCS) | 4,450 | 0.65 | 2,892 | `source_unvalidated` | 54 | Rockville, MD | 0 |
| 4 | Cognizant | 3,530 | 0.80 | 2,824 | `source_unvalidated` | 54 | College Station, TX | 0 |
| 5 | Google | 3,183 | 0.85 | 2,706 | `source_unvalidated` | 54 | Mountain View, CA | 0 |
| 6 | Meta | 2,563 | 0.85 | 2,178 | `source_unvalidated` | 51 | Menlo Park, CA | 0 |
| 7 | Capgemini | 1,612 | 0.80 | 1,290 | `validated_but_zero_postings` | 54 | Chicago, IL | 0 |
| 8 | HCLTech | 1,525 | 0.80 | 1,220 | `source_unvalidated` | 54 | Santa Clara, CA | 0 |
| 9 | Tesla | 1,574 | 0.75 | 1,180 | `source_unvalidated` | 31-33 | Austin, TX | 0 |
| 10 | IBM | 1,230 | 0.85 | 1,046 | `source_unvalidated` | 54 | Durham, NC | 0 |
| 11 | LTIMindtree | 1,289 | 0.80 | 1,031 | `source_unvalidated` | 54 | Edison, NJ | 0 |
| 12 | Wipro | 1,192 | 0.80 | 954 | `source_unvalidated` | 54 | East Brunswick, NJ | 0 |
| 13 | EY (Ernst & Young) | 1,359 | 0.65 | 883 | `no_employer_row` | 54 | Secaucus, NJ | 0 |
| 14 | TT COMMERCE AND GLOBAL SERVICES LLC | 1,070 | 0.75 | 801 | `no_employer_row` | 51 | Culver City, CA | 0 |
| 15 | COMPUNNEL SOFTWARE GROUP INC | 953 | 0.75 | 715 | `no_employer_row` | 54 | Princeton, NJ | 0 |
| 16 | Goldman Sachs | 1,050 | 0.60 | 630 | `source_unvalidated` | 52 | New York, NY | 0 |
| 17 | Intuit | 640 | 0.85 | 544 | `source_unvalidated` | 54 | Mountain View, CA | 0 |
| 18 | Mphasis | 657 | 0.80 | 526 | `source_unvalidated` | 54 | New York, NY | 0 |
| 19 | Tech Mahindra | 636 | 0.80 | 509 | `source_unvalidated` | 54 | Bedminster, NJ | 0 |
| 20 | BLOOMBERG LP | 587 | 0.75 | 440 | `source_unvalidated` | 51 | New York, NY | 0 |
| 21 | CGI TECHNOLOGIES AND SOLUTIONS INC | 557 | 0.75 | 418 | `no_employer_row` | 54 | Fairfax, VA | 0 |
| 22 | Uber | 480 | 0.85 | 408 | `validated_but_zero_postings` | 54 | San Francisco, CA | 0 |
| 23 | TikTok | 474 | 0.85 | 403 | `source_unvalidated` | 51 | Culver City, CA | 0 |
| 24 | McKinsey & Company | 467 | 0.80 | 374 | `no_career_source` | 54 | New York, NY | 0 |
| 25 | ByteDance | 435 | 0.85 | 370 | `source_unvalidated` | 51 | San Jose, CA | 0 |
| 26 | Dell Technologies | 425 | 0.85 | 361 | `source_unvalidated` | 42 | Round Rock, TX | 0 |
| 27 | SAP | 393 | 0.85 | 334 | `validated_but_zero_postings` | 54 | Newtown Square, PA | 0 |
| 28 | RANDSTAD DIGITAL LLC | 395 | 0.75 | 296 | `no_employer_row` | 54 | Woburn, MA | 0 |
| 29 | Ford | 627 | 0.45 | 282 | `source_unvalidated` | 31-33 | Dearborn, MI | 0 |
| 30 | CHARTER COMMUNICATIONS INC | 371 | 0.75 | 278 | `no_employer_row` | 51 | St Louis, MO | 0 |
| 31 | UST | 427 | 0.65 | 278 | `no_employer_row` | 54 | Aliso Viejo, CA | 0 |
| 32 | Boston Consulting Group | 405 | 0.65 | 263 | `no_employer_row` | 54 | Boston, MA | 0 |
| 33 | VIRTUSA CORPORATION | 376 | 0.65 | 244 | `no_employer_row` | 54 | Southborough, MA | 0 |
| 34 | Rivian | 325 | 0.75 | 244 | `source_unvalidated` | 31-33 | Irvine, CA | 0 |
| 35 | Siemens | 274 | 0.85 | 233 | `source_unvalidated` | 51 | Plano, TX | 0 |
| 36 | COMCAST CABLE COMMUNICATIONS, LLC | 307 | 0.75 | 230 | `no_employer_row` | 51 | Philadelphia, PA | 0 |
| 37 | L AND T TECHNOLOGY SERVICES LTD | 299 | 0.75 | 224 | `no_employer_row` | 54 | Edison, NJ | 0 |
| 38 | INFINITE COMPUTER SOLUTIONS, INC. | 291 | 0.75 | 218 | `no_employer_row` | 54 | Rockville, MD | 0 |
| 39 | KFORCE INC | 327 | 0.65 | 213 | `no_career_source` | 54 | Tampa, FL | 0 |
| 40 | Optum (UnitedHealth) | 335 | 0.63 | 212 | `no_employer_row` | 54 | Minnetonka, MN | 0 |
| 41 | HEXAWARE TECHNOLOGIES INC | 280 | 0.75 | 210 | `no_employer_row` | 54 | Iselin, NJ | 0 |
| 42 | KPMG | 295 | 0.65 | 192 | `no_employer_row` | 54 | Dallas, TX | 0 |
| 43 | CUMMINS INC. | 398 | 0.45 | 179 | `no_employer_row` | 31-33 | Nashville, TN | 0 |
| 44 | FISERV SOLUTIONS LLC | 232 | 0.75 | 174 | `no_employer_row` | 51 | Milwaukee, WI | 0 |
| 45 | EXLSERVICE COM LLC | 264 | 0.65 | 172 | `no_employer_row` | 54 | New York, NY | 0 |
| 46 | ADP TECHNOLOGY SERVICES INC | 219 | 0.75 | 164 | `no_employer_row` | 51 | Roseland, NJ | 0 |
| 47 | NOKIA OF AMERICA CORPORATION | 219 | 0.75 | 164 | `no_employer_row` | 51 | Murray Hill, NJ | 0 |
| 48 | BARCLAYS SERVICES CORP | 460 | 0.35 | 160 | `no_employer_row` | 52 | New York, NY | 0 |
| 49 | ROCKET LIMITED PARTNERSHIP | 230 | 0.65 | 149 | `no_employer_row` | 54 | Detroit, MI | 0 |
| 50 | INNOVA SOLUTIONS INC | 218 | 0.65 | 142 | `no_employer_row` | 54 | Atlanta, GA | 0 |
| 51 | INSIGHT GLOBAL LLC | 218 | 0.63 | 137 | `no_employer_row` | 54 | Atlanta, GA | 0 |
| 52 | COFORGE LIMITED | 209 | 0.65 | 136 | `no_employer_row` | 54 | Princeton, NJ | 0 |
| 53 | MathWorks | 178 | 0.75 | 134 | `no_employer_row` | 51 | Natick, MA | 0 |
| 54 | U S BANK NATIONAL ASSOCIATION | 372 | 0.35 | 130 | `no_employer_row` | 52 | Minneapolis, MN | 0 |
| 55 | CITIUSTECH INC | 195 | 0.65 | 127 | `no_employer_row` | 54 | Princeton, NJ | 0 |
| 56 | ATLASSIAN US INC | 135 | 0.85 | 115 | `source_unvalidated` | 54 | Mountain View, CA | 0 |
| 57 | ARM INC | 199 | 0.56 | 112 | `source_unvalidated` | 54 | San Jose, CA | 0 |
| 58 | ZS ASSOCIATES INC | 172 | 0.65 | 112 | `no_employer_row` | 54 | Evanston, IL | 0 |
| 59 | PERSISTENT SYSTEMS INC | 135 | 0.80 | 108 | `source_unvalidated` | 54 | Santa Clara, CA | 0 |
| 60 | HTC GLOBAL SERVICES INC | 166 | 0.65 | 108 | `no_employer_row` | 54 | Troy, MI | 0 |
| 61 | UBS BUSINESS SOLUTIONS US LLC | 208 | 0.50 | 104 | `no_employer_row` | 52 | Nashville, TN | 0 |
| 62 | GLOBALLOGIC INC | 158 | 0.65 | 103 | `no_employer_row` | 54 | Santa Clara, CA | 0 |
| 63 | CIGNA-EVERNORTH SERVICES INC | 291 | 0.35 | 102 | `no_employer_row` | 52 | St Louis, MO | 0 |
| 64 | NETAPP INC | 127 | 0.80 | 102 | `validated_but_zero_postings` | 54 | San Jose, CA | 0 |
| 65 | NTT DATA AMERICAS INC | 152 | 0.65 | 99 | `no_employer_row` | 54 | Plano, TX | 0 |
| 66 | MANHATTAN ASSOCIATES INC | 114 | 0.85 | 97 | `no_career_source` | 54 | Atlanta, GA | 0 |
| 67 | AKAMAI TECHNOLOGIES INC | 118 | 0.80 | 94 | `no_career_source` | 51 | Cambridge, MA | 0 |
| 68 | DB GLOBAL TECHNOLOGY INC | 125 | 0.75 | 94 | `no_employer_row` | 52 | Cary, NC | 0 |
| 69 | Charles Schwab | 266 | 0.35 | 93 | `no_employer_row` | 52 | Westlake, TX | 0 |
| 70 | WSP USA INC | 140 | 0.65 | 91 | `no_career_source` | 54 | New York, NY | 0 |
| 71 | TIGER ANALYTICS INC | 119 | 0.75 | 89 | `no_employer_row` | 54 | Santa Clara, CA | 0 |
| 72 | ATOS SYNTEL INC | 135 | 0.65 | 88 | `no_employer_row` | 54 | Troy, MI | 0 |
| 73 | UT-BATTELLE LLC ORNL | 135 | 0.65 | 88 | `no_employer_row` | 54 | Oak Ridge, TN | 0 |
| 74 | NUTANIX INC | 106 | 0.80 | 85 | `validated_but_zero_postings` | 51 | San Jose, CA | 0 |
| 75 | SYNOPSYS INC | 119 | 0.70 | 83 | `source_unvalidated` | 54 | Sunnyvale, CA | 0 |
| 76 | TSMC | 118 | 0.70 | 83 | `source_unvalidated` | 31-33 | Phoenix, AZ | 0 |
| 77 | MASSACHUSETTS INSTITUTE OF TECHNOLOGY | 110 | 0.75 | 82 | `no_employer_row` | 61 | Cambridge, MA | 0 |
| 78 | INTELLECTT, INC. | 123 | 0.65 | 80 | `no_employer_row` | 54 | Iselin, NJ | 0 |
| 79 | GENENTECH USA INC | 122 | 0.65 | 79 | `no_career_source` | 54 | South San Francisco, CA | 0 |
| 80 | THERMO FISHER SCIENTIFIC INC | 122 | 0.65 | 79 | `source_unvalidated` | 54 | Waltham, MA | 0 |
| 81 | AECOM TECHNICAL SERVICES INC | 120 | 0.65 | 78 | `no_employer_row` | 54 | Dallas, TX | 0 |
| 82 | BIRLASOFT INC | 120 | 0.65 | 78 | `no_employer_row` | 54 | Edison, NJ | 0 |
| 83 | HCL GLOBAL SYSTEMS LLC | 120 | 0.65 | 78 | `no_employer_row` | 54 | Farmington Hills, MI | 0 |
| 84 | Bain & Company | 119 | 0.65 | 78 | `source_unvalidated` | 54 | Boston, MA | 0 |
| 85 | MOODYS ANALYTICS INC | 103 | 0.75 | 77 | `no_employer_row` | 56 | New York, NY | 0 |
| 86 | BRILLIO LLC | 118 | 0.65 | 77 | `no_employer_row` | 54 | New York, NY | 0 |
| 87 | SK HYNIX NAND PRODUCT SOLUTIONS CORP DBA S | 138 | 0.55 | 76 | `no_employer_row` | 31-33 | Rancho Cordova, CA | 0 |
| 88 | Collins Aerospace (RTX) | 168 | 0.45 | 76 | `source_unvalidated` | 31-33 | Cedar Rapids, IA | 0 |
| 89 | V-SOFT CONSULTING GROUP, INC. | 116 | 0.65 | 75 | `no_employer_row` | 54 | Louisville, KY | 0 |
| 90 | QUADRANT TECHNOLOGIES LLC | 100 | 0.75 | 75 | `no_employer_row` | 54 | Redmond, WA | 0 |
| 91 | COTIVITI INC | 115 | 0.65 | 75 | `no_employer_row` | 54 | South Jordan, UT | 0 |
| 92 | NAGARRO INC | 91 | 0.80 | 73 | `validated_but_zero_postings` | 54 | San Jose, CA | 0 |
| 93 | DISH WIRELESS LLC | 96 | 0.75 | 72 | `no_employer_row` | 51 | Englewood, CO | 0 |
| 94 | SLK AMERICA INC | 109 | 0.65 | 71 | `no_employer_row` | 54 | Cincinnati, OH | 0 |
| 95 | SLALOM INC | 87 | 0.80 | 70 | `source_unvalidated` | 54 | Seattle, WA | 0 |
| 96 | INTUITIVE SURGICAL OPERATIONS INC | 154 | 0.45 | 69 | `no_employer_row` | 31-33 | Sunnyvale, CA | 0 |
| 97 | Marvell | 153 | 0.45 | 69 | `no_employer_row` | 31-33 | Santa Clara, CA | 0 |
| 98 | FCA US LLC | 152 | 0.45 | 68 | `no_employer_row` | 31-33 | Auburn Hills, MI | 0 |
| 99 | TRUIST BANK | 193 | 0.35 | 68 | `no_employer_row` | 52 | Charlotte, NC | 0 |
| 100 | ERP ANALYSTS INC | 105 | 0.65 | 68 | `no_employer_row` | 54 | Dublin, OH | 0 |
| … | _900 more rows in `h1b-sponsor-gaps.csv` / `.json`_ | | | | | | | |

## How to use this

`h1b-sponsor-gaps.csv` is the work queue, already ordered. For each row: `employer_norm` is the `sponsorship_signals` key, `domain` is populated ONLY when the `employers` registry already holds one (`domain_source` says which kind of evidence) — an empty `domain` means we do not know it, never that it does not exist. Do not derive one from the name; that is the mistake ADR-006 exists to stop.

Work the buckets in this order, which is cheapest-proof-first:

1. `validated_but_zero_postings` — we already proved the board. A zero here is a fetch problem, not a discovery problem.
2. `source_unvalidated` — the board is known and unproven. This is `verify_career_sources.py --mode backlink`, not a new network sweep.
3. `no_career_source` — the employer is known, the board is not. Discovery, one employer at a time.
4. `no_employer_row` — we have never heard of them. Cheapest to add a registry row, longest path to a posting.

Run ONE network pass at a time (CLAUDE.md: a content pass beside two others turned 830 sources `blocked` in one sweep).
