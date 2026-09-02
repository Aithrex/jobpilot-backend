# What "covered" means, and the metrics that prove it

> **SUPERSEDED for the coverage DEFINITION, 2026-09-02 — see
> [ADR-006](../decisions/ADR-006-canonical-employer-identity-and-owner-verified-coverage.md).**
>
> Section 2 below defines `validated` as "we fetched content from their
> board". That is what `company_ats_endpoints.status` records, and it cannot
> distinguish a company's own board from a stranger's board with a similar
> slug — `slug_is_trustworthy` admitted `forward`, `beyond`, `public`, `cts`,
> `super`, `archer`, `costar` and `alphabet` to the wrong employers.
>
> Coverage is now counted in `career_sources`, where `validated` requires
> proven OWNERSHIP as well as content. The current numbers are generated, not
> written by hand:
>
> ```bash
> PYTHONPATH=shared/python python scripts/employer_coverage_report.py --markdown
> ```
>
> → [`tech-employer-coverage.md`](tech-employer-coverage.md), stamped with the
> source revision and the inventory watermark that produced it.
>
> The rest of this document is still accurate about the DENOMINATOR (the
> evidence ladder in section 1 is unchanged and still implemented in
> `employer_tech_classifier`) and about the dashboard metrics in section 3.
> The measured figures in it are a snapshot of 2026-09-01 and are not current.

"Every tech company" is a **measurable coverage objective**, not a claim of
completeness. This document defines the denominator, the numerator, and the
states an employer can be in — so a coverage number is falsifiable.

Implemented in `shared/python/services/employer_tech_classifier.py`;
reported by `scripts/tech_employer_coverage.py`.

---

## 1. The denominator: which employers count

An employer is in the tech denominator when
`employer_tech_classifier.classify_employer` returns `software` or
`tech_adjacent`. Evidence ladder, strongest first:

| # | signal | source | max confidence |
|---|---|---|---|
| 1 | SEC SIC code | `companies.sic` (EDGAR) | 0.92 |
| 2 | posting mix | the employer's OWN active board | 0.86 |
| 3 | curated seed | `scripts/data/tech_companies.py` | 0.80 |
| 4 | ATS vendor lean | Greenhouse/Lever/Ashby/Rippling customer | 0.35 |
| 5 | name keyword | the company name | **0.20, and `unknown` only** |

**A name keyword can never classify an employer.** It is a discovery signal.
The live registry contains "NUROTOCO OF MASSACHUSETTS", "SLACK TIDE CRUISES
LLC" and "NOTIONSBYNANCY LLC"; a keyword rule promotes all three.

When a high-confidence signal DISAGREES with the winner — the SIC says
commercial bank and the board is 60% software engineering, which is exactly
Capital One and Goldman Sachs — the verdict becomes `tech_adjacent` and both
signals are recorded, one flagged `_dissent`. That disagreement is
information, not noise.

`company_size` and `company_stage` are populated **only** from an authorized
source. Absent means "we were not told", which is a different claim from
"small".

### Measured, 2026-09-01

| classification | count |
|---|---:|
| software | 1,554 |
| tech_adjacent | 900 |
| non_tech | 5,260 |
| unknown | 75,101 |
| **tech denominator** | **2,454** |

The 75,101 `unknown` are overwhelmingly state-business-registry rows with no
SIC, no domain and no postings. They are not claimed as tech and not claimed
as non-tech.

## 2. The numerator: six states, and "a domain resolves" is not one

`coverage_state_for(company, endpoints)` returns exactly one of:

| state | means | evidence required |
|---|---|---|
| `validated` | we fetched content from their board | >=1 endpoint `status: validated`, OR a curated catalog row, OR active postings ingested under this employer |
| `verified_no_public_jobs` | their board answered, with nothing on it | endpoint `status: empty` and no dead/blocked sibling |
| `retrying` | a known failure with a next attempt | endpoint `dead`, or `discovery_status` in pending/deferred/running |
| `blocked` | a documented temporary blocker | endpoint `status: blocked` + `last_check_detail` |
| `unsupported_provider` | we identified the ATS and cannot crawl it | vendor in `UNSUPPORTED_PROVIDERS`, with the named blocker |
| `unresolved` | discovery ran and matched nothing, or never ran | the honest default |

**DNS resolution is never coverage.** ADR-004 sec. 3.2 measured that
`*.icims.com` is wildcard DNS and every candidate slug resolves. Only a
successful content fetch promotes a candidate.

`no_portal` maps to `unresolved`, not to "no jobs". On live data NVIDIA,
Microsoft, Apple and Salesforce are all `no_portal` while all four run a
public ATS — that is a discovery failure, not an employer fact.

### The join that was missing

`companies` and the three Python catalogs are **disjoint registries**. NVIDIA
has 672 active postings crawled via `workday_tenant_catalog` while its
`companies` row reads `no_portal, endpoints_found: 0`. Any coverage number
computed from `companies` alone understates reality. The script joins
registry + endpoints + catalogs + live postings before counting.

### Measured, 2026-09-01

| state | count | share of 2,454 | software only |
|---|---:|---:|---:|
| **validated** | **1,056** | **43.0%** | 815 |
| retrying | 780 | 31.8% | 391 |
| unresolved | 597 | 24.3% | 334 |
| verified_no_public_jobs | 19 | 0.8% | 12 |
| blocked | 2 | 0.1% | 2 |
| unsupported_provider | 0 | 0.0% | 0 |

**Validated coverage: 43.0%.** Private vs public: 1,768 public (EDGAR filers),
686 unknown — the private cohort is under-represented because the registry was
seeded from EDGAR, which only knows public filers.

## 3. Dashboard metrics

Emitted by `shared/python/services/tech_feed_metrics.py::snapshot()` into
`tech_feed_metrics` (one document per run, TTL 90 days). Every metric names
the query that produces it, so a dashboard cannot drift from the definition.

### Employer universe and coverage
| metric | definition |
|---|---|
| `employer_universe_total` | `companies` count |
| `tech_denominator` | software + tech_adjacent |
| `validated_coverage_pct` | validated / tech denominator |
| `coverage_by_state` | the six states |
| `coverage_by_category` | by `tech_categories[]` |
| `coverage_by_ats_vendor` | validated endpoints per vendor |
| `endpoint_validation_rate` | validated / total endpoints |
| `discovery_throughput_24h` | `companies.last_discovered_at >= now-24h` |
| `endpoint_discovery_24h` | `company_ats_endpoints.created_at >= now-24h` |

### Ingestion health
| metric | definition |
|---|---|
| `crawl_lag_minutes` (per source) | now - `max(job_source_state.last_success_at)` |
| `ingest_runner_heartbeat_age` | now - `max(ingest_runner_heartbeat.updated_at)`; **alarm > 5 min** |
| `shards_in_backoff` | `job_source_state.retry_count > 0` |
| `source_yield_per_1k` | active postings / validated boards, per vendor |
| `core_tech_yield_per_board` | core-tech postings / validated boards, per vendor |

### Inventory quality
| metric | definition |
|---|---|
| `active_by_tech_scope` | core / adjacent / non_tech / unknown |
| `active_by_role_family`, `active_by_specialty` | facet counts |
| `duplicate_rate` | excess rows in `content_hash` groups / active |
| `stale_closed_rate` | closed+expired / total |
| `unknown_posting_date_rate` | `posted_at_confidence = unknown` / active |
| `missing_field_rate` | per field: salary, locations, work_mode, seniority, employment_type, requirements |
| `employer_name_conflict_rate` | postings on tenants with >1 employer_name / active |

### Search
| metric | definition |
|---|---|
| `search_zero_result_rate` | searches returning `total = 0` |
| `search_p50_ms`, `search_p95_ms` | `diagnostics.total_ms` |
| `cursor_expiry_rate` | 410 responses / cursor requests |
| `read_time_classified_rate` | `diagnostics.read_time_classified / candidates` — **should fall to 0 as the backfill lands** |
| `candidate_truncation_rate` | searches hitting `CANDIDATE_LIMIT` |

### Relevance and feedback
| metric | definition |
|---|---|
| `classification_precision/recall` | `evals/eval_tech_taxonomy.py` per run |
| `not_interested_rate_by_reason` | wrong role / level / location / technology / staffing / duplicate / not eligible |

## 4. Per-employer accountability

Every employer in an agreed golden cohort must resolve to one of the six
states with a recorded reason. `scripts/tech_employer_coverage.py --json`
emits one row per tech employer carrying `coverage_state`, `coverage_detail`,
`tech_classification`, `confidence`, `evidence_source`, `evidence[]`,
`validated_career_endpoint_count`, `active_postings` and `core_tech_postings`.

That file IS the accountability artifact. "We do not know" is a permitted
answer (`unresolved`); "we did not check" is not.
