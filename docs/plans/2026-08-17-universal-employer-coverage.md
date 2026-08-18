# Universal Employer Coverage — Execution Plan

> **For agentic workers:** implement task-by-task. Steps use checkbox syntax.
> This is ADR-004 completion, not a greenfield crawler.

**Goal:** Make Oracle Recruiting Cloud and iCIMS first-class ingest sources (same as Workday), harvest employers from every US state + DC, fingerprint their career portals onto `company_ats_endpoints`, and keep a measured loop running until ADR-004 G1–G8 can pass on live data.

**Architecture:** Invert "catalog is a Python literal" into a self-expanding graph: state/federal identity → `companies` → career-portal fingerprinting → `company_ats_endpoints` → `ats_providers` → `job_postings`. Listing for new vendors goes through `provider.list_jobs`. Search-only SoS portals are covered by a federal employment-signal proxy (EDGAR + H-1B seed), not by pretending a name-search form enumerates a registry.

**Tech Stack:** Flask ingest worker, MongoDB (`companies`, `company_ats_endpoints`, `discovery_queue`, `job_postings`), `ats_providers` plugins, Socrata/SODA, Sunbiz fixed-width, PCC bizfile JSON, pytest + mongomock.

## Refined product spec (from the raw ask)

1. **ATS parity with Workday.** Oracle ORC and iCIMS URLs differ per tenant. Discover them by fingerprinting the employer's own site (not slug-guessing). Ingest through dedicated providers that speak the real protocol (ORC paginated REST; iCIMS two-step `hashed` token + HTML). Background crawl must actually call those providers — a registered class that `list_member` never reaches is not coverage.
2. **Every US jurisdiction.** 50 states + DC. Where the SoS publishes bulk/API/open data, harvest it. Where it is search-only or paid (DE, TX SOSDirect, CA bizfileOnline, …), do **not** scrape the search UI; cover the *employers that hire* via federal signals (H-1B LCA, SEC EDGAR) plus a per-state seed of operating companies, then fingerprint their career portals.
3. **Career portals.** If the company is already in Workday/career-pages catalogs, seed as validated. Otherwise fetch site → `/careers` → match provider fingerprints → validate → ingest.
4. **Accuracy.** No fabricated jobs, dates, or tenants. iCIMS wildcard DNS is not proof a tenant exists. Dead/dissolved entities are dropped. Undated postings are not promoted to "today".
5. **Loop until measured.** `coverage_report.py` is the instrument. `employer_coverage.py loop` is the operator. Exit is MET-on-live-data, not a passing unit test.

## Goals (ADR-004 §5)

| # | Goal | Target | Binding constraint this change attacks |
|---|---|---|---|
| G1 | Oracle ORC reqs ingested / vendor TotalJobsCount | ≥ 95% | `list_member` never called `OracleOrcProvider` |
| G2 | Validated iCIMS endpoints + postings | ≥ 500, >0 jobs | same, plus `IcimsProvider` pacing |
| G3 | ATS vendors registered | ≥ 12 | 11 today; add Jobvite + BambooHR |
| G4 | Rows in `companies` | ≥ 2,000,000 | only NY+CO harvestable |
| G5 | States with a working adapter | 51 | 49 marked `search`/`paid` |
| G6 | Discovery yield | ≥ 8% | queue + fingerprinting already exist |
| G7 | Endpoints seen in 7d | ≥ 90% | revalidate path exists |
| G8 | Legacy source throughput | no regression | keep `_list_workday`/`_list_career` for the eight live vendors |

## The loop

```
measure (coverage_report.py)
  → pick lowest MISS/UNKNOWN
  → run the binding operator (seed / ingest-state / ingest-federal / discover / revalidate)
  → verify pytest for the touched surface
  → repeat until all measurable goals MET or rounds exhausted
```

## Global constraints

- Backend contract first. No new `/api/<prefix>` (existing ingest + inventory feed).
- Playwright is apply-only; discovery is HTTP.
- Honour robots.txt on employer sites. State adapters use published bulk/API/open data only.
- Auth/entitlements/JWT invariants unchanged.
- `pnpm build` is FE-only; this work is `jobpilot-backend`.
- Offline tests: mongomock, no network.

---
