# Employer Board Coverage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use executing-plans to implement this plan task-by-task (dispatch a fresh subagent per task when subagents are available). Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add proven employer-owned boards and recover under-read jobs across every requested backlog item without touching production.

**Architecture:** Keep listing behavior in registered ATS providers and employer identity in existing catalog/seed data. Preserve FetchContext.listing_truncated as the boundary between a complete census and an incomplete read. Save HTTP evidence and measurements separately from production ownership state, which only the owner may update.

**Tech Stack:** Python, requests through FetchContext, pytest, mongomock, git worktrees. Backend only; no API contract or frontend changes.

## Global Constraints

- Never load, source, copy or read `.env`. No MongoDB connections, including reads. Confirm `.env` absent in each worktree and its parents before execution.
- Create every phase worktree from `origin/main`; do not checkout/switch/reset/stash the shared main worktree. One PR per phase, one per newly built provider, no merging. Rebase each branch before opening its PR.
- LF only. Never run `--apply`, `--persist`, `--register` or `--i-authorize-live-writes`.
- All tests under tests/ or evals/, using existing conftest mongomock. Preserve RealDatabaseInTestRun.
- No browsers, proxy rotation, logins, cookies borrowed from a browser, CAPTCHA solving or spoofing. Stop at the first 403/429/challenge; classify 403/405/challenge as blocked.
- Honor robots and visible automated-collection refusals. Meta, Google results paging, Intuit search-jobs and Fluor remain excluded under the documented refusals.
- One live-probe coordinator. Serial hosts, >=1 second per host; all icims.com traffic shares >=2.5 seconds and one connection. Use only endpoints published by the employer page/script or documented public API.
- Do not edit protected job_ingest_worker backfill/detail/closure functions, scorers, identity normalization, auth, billing, frontend or infrastructure.
- Shared boards require a proven entity filter. Never merge employers because their names look alike.

## Inventory and ownership

`docs/coverage/board-work-items-2026-11.json` enumerates all 150 requested items: 23 quick wins, 16 under-read fixes/reprobes, 73 research names, 17 provider value tests and 21 wall retests. Its catalog matches are static observations, not live validation or ownership proof. Its historical starting URLs must be rechecked.

- Coordinator owns `scripts/data/direct_employer_plan.py`, catalog/seed additions, `docs/coverage/board-backlog-2026-11.md`, generated onboarding docs and PR integration.
- Provider specialist owns the explicitly assigned ATS modules and associated tests/fixtures.
- Research specialist supplies evidence rows and counts; catalog edits are applied by the coordinator to avoid collisions.
- The new JSON manifest and this plan are orchestration deliverables; they do not feed the crawl.

## Task 1: Quick wins and first evidence backlog

**Files:** scripts/data/direct_employer_plan.py; shared/python/services/job_ingest_worker.py seed table only; shared/python/services/career_pages_catalog.py; shared/python/services/workday_tenant_catalog.py; docs/coverage/board-backlog-2026-11.md; docs/audit/direct-employer-onboarding.md; tests/test_direct_employer_onboarding.py.

**Interface:** Existing `Endpoint(vendor, tenant, site, host, extra)` data and `onboard_researched_companies.validate_endpoint`; onboarding plan records do not themselves feed the crawl.

- [ ] In boards/quick-wins, create a local venv and install shared/python/requirements.txt plus requirements-dev.txt.
- [ ] For all 23 quick-win entries, inspect the static matches before any HTTP. Thirteen already have source rows; revalidate instead of duplicating them.
- [ ] Fetch official starting page, obey robots/notices, follow its exact board link/embed and validate through the registered provider. Record UTC timestamp, URLs, returned requisitions, board total, US rows and any truncation. Keep unknown measurements null.
- [ ] Use existing Avature entity filtering for FHR. Add UKG/Workday entity boundaries only when the published page/API provides proof. Otherwise record dated refusal to add the shared board.
- [ ] Leave Attune/GMI Cloud manual as explicitly requested, documenting that existing source rows differ from the stale plan. Reprobe Eaton/Evergy once.
- [ ] Update only proven catalog/seed data and stale plan outcomes; regenerate onboarding docs with `--write-docs`. Run focused onboarding/provider tests and full verification before PR.

## Task 2: Prevent incomplete reads from closing live jobs

**Files:** shared/python/services/ats_providers/oracle_orc.py, icims.py, taleo.py; tests/test_new_ats_providers.py; tests/test_list_via_provider.py; tests/test_job_ingest_worker.py.

**Interface:** `FetchContext.listing_truncated` (base.py:372) flows through `_list_via_provider` (job_ingest_worker.py:2501); run_shard includes only complete members in censused_keys (line 3370). Existing `_close_stale_in_shard` remains unchanged.

- [ ] Write failing fixture cases for page cap, deadline/cancellation, short page below declared total and a genuinely complete board.
- [ ] Run focused selectors and capture the failures.
- [ ] Set listing_truncated whenever exhaustion has not been demonstrated. Preserve complete/newest-first cutoff behavior only where the published ordering establishes it.
- [ ] Seed mongomock postings and exercise existing shard census behavior to prove all three truncated providers preserve unreached live postings. Run the tests again and record results.

## Task 3: Recover rows and published fields

**Files:** shared/python/services/ats_providers/enterprise.py, boards.py, successfactors.py; provider tests/fixtures; scripts/live_provider_check.py only if probe entries need updating.

- [ ] Phenom: measure BAE/ABB/Bechtel; raise the current 5-page budget to a measured bound, preserve totalHits and truncation, use sorting only if published.
- [ ] SmartRecruiters: prove Intuitive paging completes or marks truncation; check both registered and legacy listing paths.
- [ ] Workable: fixture repeated shortcodes across locations; emit one requisition with all published locations.
- [ ] SuccessFactors: recover missing PSEG/PSEG LI location from existing detail/JSON-LD flow without modifying protected backfill functions.
- [ ] Eightfold classic: retain unknown dates unless the tenant publishes a demonstrable posting date.
- [ ] Uber: inspect its published public request once for termless support. Implement only if demonstrated; phase-4 Uber points at this same result.
- [ ] Reprobe Microsoft, Qualcomm, Confluent, Aurora Innovation, dbt Labs, Hex and ClickHouse. Follow official pages before changing tokens.
- [ ] Record before/after counts with exact requests and time budgets; a blocked live check is a reported limitation, not a passing vendor check.

## Task 4: Exhaustive employer research and one wall retest

**Files:** catalogs/seeds and direct_employer_plan.py as needed; docs/coverage/board-backlog-2026-11.md. Branch boards/employer-research; phase-5 wall outcomes share this PR.

- [ ] Process all 73 research names. Nine already have source rows: Upwind Security, Figure AI (Figure), Zoom, Ubisoft, Axonius, Tines, Cato Networks, Comcast and Rocket (Rocket Companies). Leave their production ownership verification to the owner.
- [ ] Research the remaining 64. McKinsey, Deloitte and Walmart aggregator-only records do not count as background coverage. Find official careers chain, validate supported provider, then add a proven row or record blocked/robots_refusal/tos_notice/no_public_board/needs_new_provider/not_worth_it with evidence.
- [ ] Measure public US rows first for the 14 H-1B sponsor names; sponsorship approvals are not board-size measurements.
- [ ] Retest each of the 21 walls with one polite request, stopping on the first refusal. Do not convert transport failure or a shell into a verified empty board.
- [ ] Reconcile one dated outcome per requested entry and rerun focused plus full verification before the PR.

## Task 5: Measure each proposed provider before implementation

**Files:** shared/python/services/tenant_discovery.py (read/use only), candidate provider modules and fixtures, registry.py module tuple, live_provider_check.py PROBES, vendor docs, backlog. One boards/provider-<vendor> branch per built provider.

- [ ] For all 17 candidates, count known employer uses, archived tenants, and three sample public board sizes with US shares. Preserve unknown measurements when evidence cannot be obtained; do not label unknown as zero or as not_worth_it.
- [ ] Build only if >=5 known employers or >=300 open US postings are reachable. Check existing implementations first: SuccessFactors already contains unified-search support and Wipro/Capgemini seeds; Upwind already reads Comeet-rendered HTML through careers_page.
- [ ] Follow the pasted provider recipe: observed protocol docstring, bounded/paced public requests, exact fingerprint and hosts, honest dates, mapping, detail, validation and truncation. Add trimmed real fixtures and refusal/movement/cap/deadline tests, register provider and add a live probe.
- [ ] Add only proven employer rows, update provider/backlog docs, run focused tests, one live vendor check, full suite and create the provider PR.

## Verification and delivery

Run from each worktree using its venv: `python scripts/verify.py --fast -- <specific tests>` during work, then `python scripts/verify.py --full` before its PR. Record exact commands and test pass/fail counts. The known Longbridge wall-clock flake occurs around 04:23-04:33 UTC; rerun outside the window without weakening it.

Prepare PR bodies from final diffs with added/fixed/skipped rows, reasons, live probe output and verification. Rebase on origin/main immediately before opening. Do not merge or deploy. Later PR backlog edits may overlap the first PR while all are unmerged; document merge order rather than duplicating earlier source commits.

Owner-only handoff: after merge/deploy run onboarding dry-run then --apply; career-source content dry-run then owner apply, backlink dry-run then owner apply, one pass at a time; coverage --markdown then --persist; configure USAJOBS_API_KEY and USAJOBS_USER_AGENT on the box. The agent runs none of the live database steps.

## Static contradictions found

- Thirteen quick-win/manual entries already have seed rows although the plan says unsupported.
- Axonius, Tines and Cato Networks are already in the crawl catalog despite the dated report saying not_in_registry.
- Upwind has a careers_page/Comeet HTML reader; a new Comeet provider is not prerequisite for that employer.
- SuccessFactors already has unified-search support; the cited historical Wipro gap predates it.
- Vendor coverage section 6 is explicitly historical. Registered readiness, source rows and fresh live evidence outrank it.
- Oracle ORC/iCIMS/Taleo still lack listing_truncated assignment; Phenom still uses max_pages=5 at the audited main SHA.
- Backend AGENTS.md is not tracked in this fresh worktree; platform AGENTS.md plus backend CLAUDE.md supply the working instructions.
