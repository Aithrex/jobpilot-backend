# Tech-search baseline and gap matrix

> **SNAPSHOT of 2026-09-01/02. Superseded on 2026-09-02 — kept as the BEFORE.**
>
> Every number here is a real measurement and none of them is current. It is
> retained because it is the baseline the work of 2026-09-02 was measured
> against, and a before/after with no before is an assertion.
>
> | what it says here | where the current number lives |
> |---|---|
> | "validated coverage 43.0%" | `docs/coverage/tech-employer-coverage.md` (generated). That 43.0% counted endpoints that ANSWERED; see [ADR-006](../decisions/ADR-006-canonical-employer-identity-and-owner-verified-coverage.md) for why that is not coverage. |
> | "129 tenants with more than one employer_name" | the `employers` collection: one canonical `employer_id`, name-only matches quarantined |
> | "core ~7.4% of active" | re-measured after `tech-tax-1.1.0`; `semiconductor_hardware` is now CORE |
> | "2,000 candidate ceiling" | removed — paging continues by keyset past the ranked window |
> | "work_mode 96.1% unknown" | still largely true, and it no longer COSTS anything: fit-v2.1 excludes an unmeasurable component instead of pricing it |
> | "country filter" | it had never matched anything on any country; fixed, with an ISO-normalised `locations.country_code` |

**Measured against live `aspirely_db` on 2026-09-01/02.** Every number below
came from a read-only query, not from reading code. Where a number describes
code capability rather than production reality, it says so.

Reproduce with:

```bash
PYTHONPATH=shared/python python scripts/tech_employer_coverage.py --json out.json
PYTHONPATH=shared/python python scripts/backfill_tech_taxonomy.py --sample 30000
```

---

## 1. Inventory

| | value |
|---|---:|
| `job_postings` total | 409,009 |
| active | **227,914** |
| closed / expired | 181,095 |

Active by recency:

| window | posted (dated) | first seen |
|---|---:|---:|
| 1 day | 28,025 | 31,740 |
| 7 days | 151,316 | 170,825 |
| 30 days | 209,692 | 226,584 |

Active by source and ATS:

| source | active | | ATS | active |
|---|---:|---|---|---:|
| workday | 148,460 | | workday | 148,460 |
| career-pages | 38,037 | | oracle-orc | 26,143 |
| oracle | 26,143 | | icims | 15,274 |
| icims | 15,274 | | smartrecruiters | 9,118 |
| | | | greenhouse | 8,779 |
| | | | lever | 6,875 |
| | | | + 16 more vendors | 21,455 |

**Cost class is 100% `free`** — every active posting is a direct ATS read.
No aggregator is currently in the inventory.

## 2. The headline: this is not a tech job board today

Active postings by legacy `role_family`, sorted:

| family | active | | family | active |
|---|---:|---|---|---:|
| **other (unclassified)** | **45,783** | | software_engineering | 10,128 |
| customer_support | 17,751 | | hardware_engineering | 7,161 |
| operations | 16,971 | | qa | 2,718 |
| sales | 14,148 | | devops | 2,432 |
| allied_health | 12,918 | | security | 2,373 |
| nursing | 11,269 | | data_analysis | 1,489 |
| legal | 11,036 | | data_science | 1,377 |
| retail | 10,001 | | data_engineering | 576 |

Re-classified with the new taxonomy (`tech_taxonomy`, title-only, over the
whole active set):

| tech_scope | share of active |
|---|---:|
| core | **~7.4%** (16,858 postings) |
| adjacent | ~1.4% |
| non_tech | ~26–30% |
| unknown | ~63% |

The `unknown` bucket is large because classification here is TITLE-ONLY —
descriptions live in S3 and are not re-read. At ingest the classifier sees the
body and resolves far more. This is the single largest argument for the
backfill (sec. 7).

**Conclusion: ~17k of 228k active postings are core-tech.** The product is
currently a general job board that happens to contain some engineering roles.

## 3. Data quality

| field | populated | gap |
|---|---:|---|
| canonical_url | 227,914 (100%) | — |
| locations | 217,019 (95.2%) | 4.8% |
| requirements parsed | 216,687 (95.1%) | 4.9% |
| description (S3 ref) | 215,319 (94.5%) | 5.5% |
| posted_at | 209,692 (92.0%) | **18,222 undated (8.0%)** |
| compensation | 42,244 (18.5%) | **81.5% unpriced** |
| seniority known | 129,103 (56.7%) | 43.3% unknown |
| role_family classified | 182,131 (79.9%) | 20.1% `other` |
| **work_mode known** | **8,876 (3.9%)** | **96.1% unknown** |
| employment_type stated | 53,351 (23.4%) | 76.6% blank |

Two of these are product-breaking as they stand:

* **`work_mode` is 96.1% unknown.** A remote filter over this inventory
  returns 6,264 of 227,914 rows. That is not a market signal; it is a parsing
  gap. The filter must default to including `unknown` and must say so.
* **`employment_type` is unnormalized.** 174,563 blank plus a long tail of
  61+ distinct raw values (`fulltime`, `regular-full-time`, `ft-hourly-w-bn`,
  `cdi`, `foh`, `sm`). An equality filter on it is meaningless today.

## 4. Duplicates

| grouping | groups | rows | excess rows |
|---|---:|---:|---:|
| identical `content_hash` | 9,180 | 22,628 | 13,448 (**5.9% of active**) |
| identical `canonical_url` | 733 | 1,466 | 733 |
| same (employer, normalized title) | 18,927 | 93,447 | 74,520 (32.7%) |

The third number is mostly legitimate multi-city variants, not duplicates —
which is exactly why they are now preserved as `location_variants` rather than
dropped.

## 5. Employer identity is broken for 12.5% of the inventory

**129 `(ats, tenant)` pairs carry more than one `employer_name`, covering
28,386 active postings.** The endpoint's `display_name` is written from
whichever `companies` row discovered it, so a state-registry shell that is
name-adjacent to a large employer claims the tenant:

| tenant | active | names observed |
|---|---:|---|
| `oracle_orc/eofd` | 5,414 | RANDALL'S INC. / SAFEWAY PRODUCTS INC. / SAFEWAY INC. |
| `oracle_orc/jpmc` | 3,998 | **NUTMEG CONTAINER CORPORATION** / **CHASE FAMILY LIMITED PARTNERSHIP NO. 5** / JPMorgan Chase |
| `workday/abbott` | 1,410 | ABBOTT SOLUTIONS LLC / **Abbott BBQ** / Abbott / Abbott's AI |
| `workday/boeing` | 593 | BOEING CO / THE BOEING COMPANY / **M.J. WILLIAMS COMPANY** |
| `workday/roche` | 562 | **GENE F. LANG & CO.** / **COBAS SERVICES LLC** |
| `workday/att` | 650 | AT&T / **AIO GROUND BREAKING LLC** |
| `workday/salesforce` | 432 | Salesforce / Slack |

688 validated endpoints have a `display_name` unrelated to their tenant.

`employer_identity.resolve_employer_name` now resolves **94 of the 129
conflicts (16,156 postings)** at read time and flags the other 35 (12,230
postings) rather than guessing. Stage where this breaks: **normalization**.

## 6. Employer universe and coverage

`companies` — 82,815 rows (was 82,492 on 2026-08-22):

| discovery_status | count | share |
|---|---:|---:|
| deferred | 70,473 | 85.1% |
| no_portal | 8,967 | 10.8% |
| done | 2,831 | 3.4% |
| pending | 544 | 0.7% |

`company_ats_endpoints` — 2,590 rows (was 1,111): 2,110 validated, 297 empty,
138 dead, 43 blocked, 2 candidate. 130 have `consecutive_failures > 0`.

Discovery throughput has **recovered** since `TASK-company-discovery-
throughput.md` was written: 81,449 companies touched in 7 days, 501 in the
last 24h (was 4/day). The remaining problem is not throughput, it is that 85%
land in `deferred`.

### Employer tech classification (`employer_tech_classifier`)

| classification | count |
|---|---:|
| software | 1,554 |
| tech_adjacent | 900 |
| non_tech | 5,260 |
| unknown | 75,101 |

**Tech denominator: 2,454.** Evidence source: SEC SIC 1,678 · curated seed 325
· posting mix 228 · ATS vendor 218 · registry signal 5. Public 1,768 /
unknown 686.

### Coverage of the tech denominator

| state | count | share |
|---|---:|---:|
| **validated** | **1,056** | **43.0%** |
| retrying | 780 | 31.8% |
| unresolved | 597 | 24.3% |
| verified_no_public_jobs | 19 | 0.8% |
| blocked | 2 | 0.1% |
| unsupported_provider | 0 | 0.0% |

## 7. Representative missing employers, and the stage each disappears at

Of 128 well-known technology employers checked, **91 have active postings and
37 have none**.

**Zero active postings:** Google, Apple, Meta, Tesla, Atlassian, IBM, Dell,
VMware, Shopify, Snap, Fortinet, Splunk, Confluent, Zoom, Intuit, Deel, Arm,
Seagate, Lam Research, Synopsys, SAP, Siemens, Infosys, Wipro, Cognizant,
Capgemini, EPAM, Rivian, Aurora, Goldman Sachs, Bloomberg, Nutanix, NetApp,
Akamai, Twitter/X, Booking, Blue Origin.

**Present but drastically under-crawled:** Microsoft 67 (Eightfold board only;
the real board is `jobs.careers.microsoft.com`), Qualcomm 3, Palantir 2, F5 1.

| employer | stage it disappears at | evidence |
|---|---|---|
| NVIDIA | **coverage accounting** | 672 active postings via `workday_tenant_catalog`, yet its `companies` row says `no_portal, endpoints_found: 0`. The two registries are disjoint. |
| Microsoft, Apple, Salesforce, Atlassian, Broadcom, Dell, VMware, Shopify, Uber, HubSpot, Unity, Synopsys, SAP, Zoom, SpaceX, Zoox, Aurora, Rippling, Deel | **discovery / fingerprinting** | `discovery_status: no_portal` — "read 5 page(s), no ATS link found" — while all run a public ATS. |
| Google, Meta, Amazon, Netflix, Oracle, Cisco, IBM, Qualcomm, Coinbase, Spotify, Accenture, Infosys | **discovery queue** | `discovery_status: deferred`, never re-armed. |
| Stripe, Notion, Ramp, Capital One, Intuit | **registry identity** | The name matched the wrong entity: `stripe` -> "STRIPES SOLUTIONS LLC", `notion` -> "NOTIONSBYNANCY LLC", `ramp` -> "RAMPART RANGE MOTORCYCLE MANAGEMENT COMMITTEE", Capital One -> domain `capital.com` (a CFD trading site). |
| Rivian | **ingestion** | iCIMS tenant `us-careers-rivian` returns a 143-byte redirect; `PortalMovedError` records the destination but nothing re-fingerprints it. |
| JPMorgan Chase, Boeing, Roche, AT&T, Abbott | **normalization** | Real postings ingested under another company's name (sec. 5). |
| Ambiguous graduate/rotational programmes | **classification** | Resolvable only from the body; the read path has titles only until the backfill runs. |

## 8. Search + ingest health at time of measurement

* Ingest runner heartbeat: **healthy**, 4 sources on one box
  (`ip-172-31-6-99`), 48–96 shards each.
* Crawl lag: career-pages 14 min, icims 2 min, oracle 10 min, workday 4 min.
  No source stale, no shard in backoff (`retry_count: 0` everywhere).
* Endpoint validation success: 2,110 / 2,590 = **81.5%**.

## 9. Index state before this work

| collection | indexes |
|---|---|
| `job_postings` | 13 (well covered) |
| `companies` | **1** (`company_id` only) — every discovery tick COLLSCANned 82,815 docs |
| `company_ats_endpoints` | **0** (`_id` only) — every registry read COLLSCANned 2,590 docs |
| `discovery_queue` | 2 |

17 indexes were added (see `docs/ops/tech-feed-rollout.md` sec. 2). The
discovery due-set query went from a COLLSCAN of 82,815 documents to
`IXSCAN`, `docsExamined=200`, **1 ms**.
