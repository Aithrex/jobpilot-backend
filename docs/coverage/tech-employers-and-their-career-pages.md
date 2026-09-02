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

## 4a. The slug-probe channel does not work, and here is the evidence

An earlier revision of this document claimed a probe had found **134 new career
pages**. That claim is withdrawn, and the follow-up work says something
stronger: **slug guessing cannot currently be made safe for this cohort.**

### What was wrong with the original claim

The run's output was piped through `tail -60`, discarding most of the log. Of
the 47 hits that survived, **all 47 were already rows in
`company_ats_endpoints`**. The prober does not skip already-covered companies
and was pointed at `signals.software` sorted by priority - the cohort most
likely to already be covered. **Verified new boards: 0.**

### The correct experiment

1,398 tech employers with no validated endpoint, untruncated log:

```
probed 1,398 companies in 1,866s
boards found: 56 (4.0%)
rejected at probe time: 15 named another company, 16 too weak a slug
```

### Auditing those 56 destroyed them

Every hit was re-judged against the vendor's own declared board owner. **16 of
56 (29%) were wrong** even before the guards were tightened - `greenhouse/public`
is Public.com the investing app, claimed for "Public Policy Holding Company";
`greenhouse/cts` is "CTS Cloud Trading Solutions", claimed for CTS Corp;
`ashby/sphere`, `ashby/twenty`, `ashby/sierra`, `ashby/odyssey` are ordinary
nouns.

Both holes were fixed (`names_match` now requires 40% name coverage;
`slug_is_trustworthy`'s blocklist was widened). **The fix was not enough.** Seven
of the 40 surviving hits were spot-checked against the boards' actual job
listings, and **all seven were wrong**:

| board | probe assigned it to | what it actually is | tell |
|---|---|---|---|
| `greenhouse/forward` | Forward Industries | Forward (healthcare) | jobs link to `getfwd.com` |
| `greenhouse/beyond` | Beyond Air | Beyond Pricing | jobs link to `beyondpricing.com` |
| `greenhouse/future` | Future FinTech Group | Future (fitness app) | "Health Coach Manager" |
| `greenhouse/orchestra` | Orchestra BioMed | Orchestra (PR agency) | "Account Coordinator, Public Relations" |
| `greenhouse/encore` | Encore Medical | Encore (travel programmes) | "Encore - Istanbul April-May 2027" |
| `greenhouse/costar` | CoStar Group | Co-Star (astrology app) | "Haskell Developer", "Brand Designer" |
| `greenhouse/universal` | Universal Token | a London design studio | "Freelance Designer" |

Seven for seven. The survivor set is majority-contaminated, and the only signal
that separated them was **reading the jobs**.

### Why the automated checks cannot save it

Three independent signals were tried and all three fail here:

1. **Slug heuristic** - 29% wrong, and it is the ONLY guard on Ashby and Lever
   because neither vendor declares a board owner at all.
2. **Vendor-declared owner name** - Greenhouse and SmartRecruiters do declare
   one, and it still passed all seven rows above. A board named "Forward"
   genuinely is named Forward.
3. **Board domain vs company domain** - unusable, because of sec. 4b.

**Recommendation: do not apply slug-probe output.** Not with `--apply`, not
after review of the summary line. For this cohort the channel produces
majority-wrong attributions in the most dangerous available form - a live
board, correctly fetched, filed under the wrong employer, which reads as
coverage rather than as an error and which nobody will go looking for.

## 4b. Root cause: 76% of company domains are guesses

Measured over 40,000 `companies` rows:

| | rows | share of those with a domain |
|---|---:|---:|
| domain is the NAME with punctuation stripped + `.com` | 8,030 | **76%** |
| domain differs from the name | 2,593 | 24% |
| no domain at all | 29,377 | - |

The guess is right often enough to look like it works - `nvidia.com`,
`microsoft.com`, `intel.com`, `visa.com` - and wrong often enough to lose
employers silently: `alphabet.com` for Alphabet (not Google's domain),
`amazon-com.com`, `capital.com` for Capital One (a CFD trading site),
`stripes-solutions.com` for Stripe, `payoneer-global.com` for Payoneer (the
real one is `payoneer.com`).

This matters beyond the probe. `career_portal_discovery` is **company-first**:
it fetches `companies.domain` and follows `/careers`. If the domain is a wrong
guess, discovery crawls a stranger's website, finds no ATS fingerprint, and
records `no_portal` - which is indistinguishable from an employer that has no
portal.

That is a credible explanation for why NVIDIA, Microsoft, Apple, Salesforce,
Atlassian, Dell and Unity are all `no_portal` while all seven run a public ATS,
and it means **the next real coverage work is verifying employer domains, not
guessing more slugs.** A wrong domain poisons every downstream stage, and no
amount of probing recovers from it.

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
