# The tech employers we know, and where their career pages are

Generated 2026-09-01 by `scripts/tech_employer_coverage.py`. The full row-level
data is **[`tech-employers-2026-09-01.csv`](tech-employers-2026-09-01.csv)** —
2,374 employers, one row each, with classification, evidence, coverage state,
career-page registry, vendor, tenant and posting counts.

Regenerate:

```bash
PYTHONPATH=shared/python python scripts/tech_employer_coverage.py --json out.json
```

---

## 1. The list

**2,374 unique technology employers** identified in `companies` (82,815 rows) —
1,507 `software` and 867 `tech_adjacent`. Before name-key dedupe the registry
holds 2,445 tech rows; the difference is one employer filed under several
legal names (Leidos x3, RTX x2, Medtronic x3, Amgen x3).

Classified by `employer_tech_classifier` from SEC SIC codes, the employer's own
posting mix, and the curated seed. **Never from a keyword in the company name.**
The registry contains "SLACK TIDE CRUISES LLC", "NOTIONSBYNANCY LLC" and
"NUROTOCO OF MASSACHUSETTS, INC."; a name rule promotes all three.

By category:

| category | employers | | category | employers |
|---|---:|---|---|---:|
| software | 529 | | telecom | 117 |
| fintech | 214 | | cloud_infrastructure | 113 |
| it_services | 210 | | data_services | 113 |
| medtech | 181 | | automotive | 74 |
| semiconductor | 161 | | ai-ml | 59 |
| hardware | 154 | | consulting | 57 |
| business_services | 125 | | saas | 56 |
| (uncategorised) | 271 | | systems_integration | 54 |

## 2. Where their career pages are

| coverage state | employers | share |
|---|---:|---:|
| **validated** — we fetch content from their board | **982** | **41.4%** |
| retrying — a known failure with a next attempt | 1,136 | 47.9% |
| unresolved — discovery matched nothing, or never ran | 236 | 9.9% |
| verified_no_public_jobs — board answered, nothing on it | 18 | 0.8% |
| blocked — documented temporary blocker | 2 | 0.1% |
| unsupported_provider | 0 | 0.0% |

**A resolving domain is not coverage.** ADR-004 sec. 3.2 measured that
`*.icims.com` is wildcard DNS, so every candidate slug resolves. Only a
successful content fetch counts.

Of the 982 validated, the career page was found through:

| registry | employers |
|---|---:|
| `company_ats_endpoints` (discovered) + live postings | 587 |
| `career_pages_catalog` (Greenhouse / Lever / Ashby / ...) | 244 |
| `workday_tenant_catalog` | 131 |
| `mnc_catalog` | 20 |

**These four registries were disjoint before this work.** NVIDIA has 672 active
postings crawled through `workday_tenant_catalog` while its `companies` row
still reads `no_portal, endpoints_found: 0`. Any coverage figure computed from
`companies` alone is wrong in a direction that looks like failure; the script
joins all four before counting anything.

## 3. Implemented in job search

All 982 validated employers are reachable through
`POST /api/jobs/tech-feed/search`:

* **by employer** — `{"company_ids": ["nvidia", "stripe"]}` accepts either the
  normalized employer id or the ATS tenant slug;
* **by employer class** — `{"employer_scope": "tech_only"}` narrows to
  `software` / `tech_adjacent` employers and KEEPS `unknown` (an employer we
  cannot classify must not silently disappear from the market);
* **by category** — `{"company_categories": ["semiconductor"]}`;
* **by kind** — `{"employer_kind": "direct_employer"}` excludes staffing firms
  and marketplace reposts.

The `employer` facet on every response lists who is actually in the current
result set, with counts, so the filter list is generated from the data rather
than from a catalog that may have drifted.

**523 of these employers currently yield at least one core-tech posting.**
Top 50 by core-tech volume — names resolved by `employer_identity`, which is
why JPMorgan Chase appears once with 776 core-tech postings rather than three
times as "NUTMEG CONTAINER CORPORATION", "CHASE FAMILY LIMITED PARTNERSHIP
NO. 5" and "JPMorgan Chase":

| employer | tech class | core-tech | active | via |
|---|---|---:|---:|---|
| JPMORGAN CHASE & CO | tech_adjacent | 776 | 3,999 | mnc catalog |
| Amazon | software | 523 | 2,902 | mnc catalog |
| Booz Allen Hamilton | software | 367 | 792 | workday tenant catalog |
| NVIDIA CORP | software | 339 | 673 | workday tenant catalog |
| Leidos Holdings, Inc. | software | 217 | 819 | workday tenant catalog |
| RTX Corporation | software | 203 | 1,278 | workday tenant catalog |
| Oracle | software | 175 | 438 | mnc catalog |
| BOEING CO | software | 144 | 593 | workday tenant catalog |
| BARCLAYS PLC | tech_adjacent | 141 | 493 | workday tenant catalog |
| Mastercard Inc | software | 132 | 426 | workday tenant catalog |
| Hewlett Packard Enterprise | tech_adjacent | 115 | 331 | workday tenant catalog |
| Rippling | software | 110 | 390 | endpoints/postings |
| Genpact LTD | software | 101 | 612 | workday tenant catalog |
| STATE STREET CORP | tech_adjacent | 97 | 510 | workday tenant catalog |
| CATERPILLAR INC. | tech_adjacent | 91 | 775 | workday tenant catalog |
| Salesforce, Inc. | software | 88 | 434 | workday tenant catalog |
| RELX PLC | software | 85 | 302 | workday tenant catalog |
| Kyndryl | software | 82 | 289 | workday tenant catalog |
| AMERICAN SYSTEMS CORPORATION | software | 78 | 303 | endpoints/postings |
| DXC Technology | software | 77 | 280 | workday tenant catalog |
| KBR, INC. | tech_adjacent | 75 | 451 | workday tenant catalog |
| Palo Alto Networks Inc | software | 75 | 254 | workday tenant catalog |
| Anduril Industries | software | 73 | 300 | career pages catalog |
| General Motors Co | software | 71 | 394 | workday tenant catalog |
| AMERICAN EXPRESS CO | software | 71 | 332 | mnc catalog |
| Micron Technology | software | 68 | 586 | workday tenant catalog |
| Expedia Group, Inc. | tech_adjacent | 66 | 130 | workday tenant catalog |
| Medtronic plc | tech_adjacent | 62 | 708 | workday tenant catalog |
| COBAS SERVICES LLC | software | 61 | 474 | endpoints/postings |
| Elastic | software | 58 | 110 | career pages catalog |
| FISERV INC | software | 53 | 249 | workday tenant catalog |
| Workday, Inc. | software | 53 | 179 | workday tenant catalog |
| Honeywell Aerospace Inc. | tech_adjacent | 52 | 363 | endpoints/postings |
| Databricks | software | 52 | 103 | career pages catalog |
| AMD | software | 50 | 255 | mnc catalog |
| MANULIFE FINANCIAL CORP | tech_adjacent | 49 | 392 | endpoints/postings |
| ADOBE INC. | software | 49 | 138 | workday tenant catalog |
| JOHNSON CONTROLS, INC. | tech_adjacent | 48 | 844 | workday tenant catalog |
| BNY Investments, LC | software | 48 | 246 | endpoints/postings |
| ServiceNow | software | 45 | 142 | career pages catalog |
| VISA INC. | software | 44 | 236 | workday tenant catalog |
| Autodesk | software | 44 | 111 | workday tenant catalog |
| APPLIED MATERIALS, INC. | software | 43 | 519 | workday tenant catalog |
| FLEX LTD. | software | 41 | 668 | workday tenant catalog |
| AMGEN INC. | software | 40 | 519 | workday tenant catalog |
| S&P Global Inc. | software | 39 | 161 | workday tenant catalog |
| Zensar Technologies | software | 39 | 91 | endpoints/postings |
| SpaceX | software | 38 | 223 | mnc catalog |
| LOCKHEED MARTIN CORP | tech_adjacent | 38 | 53 | mnc catalog |
| HP | tech_adjacent | 37 | 313 | workday tenant catalog |

## 4. The gap, and what closes it

**1,392 tech employers have no validated career page.** The largest blocks, in
the order they should be worked:

1. **236 `unresolved`** — discovery read their site and matched no ATS
   fingerprint. Includes Dell, Atlassian, Unity, Seagate, Super Micro, Procore,
   Bentley Systems, C3.ai, Lattice Semiconductor, Garmin, Accenture, Arm,
   NETSCOUT, Amkor, Advantest and Arteris.
   `scripts/probe_software_company_boards.py` already documents why: a modern
   software company's careers page is a React shell that loads its board by
   XHR, so the ATS URL appears in no served HTML. **The fix is the slug probe,
   not more crawling** — and that tool already exists. What was missing is that
   it selects on `signals.software` (1,554 rows) rather than on this
   classification (2,454), so ~900 real technology employers were invisible to
   it. `--apply --persist-classification` closes that.
2. **1,136 `retrying`** — mostly `discovery_status: deferred`, never re-armed.
   Includes Etsy, EPAM, Keysight, Tempus AI, Chegg, Rivian, A10 Networks and
   3D Systems.
3. **Registry identity failures** — Stripe resolves to "STRIPES SOLUTIONS LLC",
   Notion to "NOTIONSBYNANCY LLC", Ramp to "RAMPART RANGE MOTORCYCLE MANAGEMENT
   COMMITTEE", Capital One to the domain `capital.com` (a CFD trading site).
   These are not discovery failures; the registry is pointing at the wrong
   company, so discovery cannot succeed no matter how often it runs.
4. **Migrated portals** — `PortalMovedError` already records the destination
   (Rivian moved off iCIMS to `careers.rivian.com`) and nothing re-fingerprints
   the new host.

## 4a. CORRECTION: the first probe found no new boards

An earlier revision of this document claimed a read-only slug probe had found
**134 new career pages**. That claim was wrong and is withdrawn.

What actually happened:

* The probe's output was captured through `tail -60`, which discarded most of
  the log. Only 47 of the reported 134 hit lines survived, so 87 of them have
  no evidence at all.
* Of the 47 that can be checked, **all 47 were already rows in
  `company_ats_endpoints`**. Rubrik, GoDaddy, Elastic, VeriSign, Commvault,
  TripAdvisor - every one already known.
* `probe_software_company_boards.py` does not skip already-covered companies,
  and it was run over `signals.software` sorted by priority, which is exactly
  the cohort most likely to already be covered. The run largely re-confirmed
  boards we had.

**Verified new boards from that run: 0.**

The script now takes `--skip-covered` and `--company-ids`, so the correct
experiment can be run: probe only employers with no validated endpoint.

```bash
PYTHONPATH=shared/python python scripts/probe_software_company_boards.py \
    --company-ids uncovered_tech_ids.txt --limit 2000 --rate 6 --workers 4
```

Early returns from that run are far lower than the first, which points the
opposite way from the withdrawn claim: the uncovered tech employers mostly do
**not** have findable boards on the four shared-host vendors (Greenhouse,
Lever, Ashby, SmartRecruiters). That is consistent with them being uncovered -
`career_portal_discovery` already crawled their sites and matched nothing, and
a slug guess against four shared hosts is a narrower instrument than the site
crawl, not a wider one.

If that holds, the boards for Dell, Atlassian, Unity, Seagate, Super Micro,
Procore and Synopsys are on per-employer hosts (Workday, Eightfold, Phenom,
Oracle ORC) that a shared-host slug probe cannot reach by construction, and
finding them needs a different instrument.

**Owner verification is doing real work** and is worth keeping whatever the hit
rate: the runs rejected `greenhouse/archer` ("Archer Veterinary Clinic", not
Archer Aviation), `greenhouse/super` ("Super Technologies", not Super League),
`greenhouse/gds` ("Garage Door Specialist", not GDS Holdings) and
`greenhouse/nve` ("NVE Experience Agency", not NVE Corp). Recording any of
those would have read as coverage rather than as an error.

See [`source-provider-priority.md`](source-provider-priority.md) for which
vendors to pursue first, ranked by core-tech yield per board rather than by raw
posting count.

## 5. What the CSV contains

| column | meaning |
|---|---|
| `name`, `domain`, `ticker` | registry identity |
| `tech_classification` | software / tech_adjacent |
| `tech_categories` | pipe-separated |
| `confidence`, `evidence_source` | how we know |
| `public_private_status` | public (EDGAR filer) / unknown |
| `coverage_state`, `coverage_detail` | one of the six states, with the reason |
| `active_postings`, `core_tech_postings` | what they currently yield |
| `validated_career_endpoint_count` | validated boards on file |
| `catalog_registry`, `catalog_vendor`, `catalog_tenant` | where the career page lives |
| `discovery_status`, `company_id` | enough to re-run discovery on a row |

Every row accounts for itself. "We do not know" (`unresolved`) is a permitted
answer; "we did not check" is not.
