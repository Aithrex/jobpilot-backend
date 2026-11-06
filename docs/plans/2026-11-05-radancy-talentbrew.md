# TalentBrew public page reader implementation plan

> **For agentic workers:** Use executing-plans to implement these tasks in this isolated worktree. No commits, PRs, network requests or production access until coordinated with the parent task.

**Goal:** Crawl permitted TalentBrew listing and detail HTML while reporting partial coverage truthfully.

**Architecture:** A registered `radancy_talentbrew` provider uses a data table of five observed employer hosts, organization IDs and listing entries. It reads robots before public listing/detail requests, parses server-rendered cards and JobPosting JSON-LD, and never calls the prohibited Ajax routes or repairs the broken Next links. Optum requires its exact checked employer facet in the served page.

**Tech stack:** Python 3.12, existing lxml, FetchContext transports, pytest/mongomock.

## Constraints

- No dotenv, real MongoDB, browser, guessed endpoints, deployment, or credentials.
- LF only; one request at a time with at least one second per host.
- Dates come from the posting, never the current time.
- Stop on 403/405/429 or challenge bodies; robots refusal is blocked, never empty.
- Initial 15 rows are incomplete whenever the advertised total exceeds the observed unique rows.

## Task 1: Reader and contract tests

Files: `shared/python/services/ats_providers/radancy_talentbrew.py`, registry module tuple, `tests/test_radancy_talentbrew_provider.py`, real trimmed fixtures under `tests/fixtures/ats_providers/radancy_talentbrew/`.

- [x] Write tests through the registry first: `assert registry.get("radancy_talentbrew") is not None`, then verify real captured cards, details, scope, refusal and completeness.
- [x] Run `python3 scripts/verify.py --fast -- tests/test_radancy_talentbrew_provider.py`; confirm failure because the provider does not exist.
- [x] Implement `list_jobs(Endpoint, FetchContext)`, `fetch_detail`, `validate`, `board_url` and fingerprint matching. Read robots with the same transport and cache its text only in FetchContext; named host and route validation precedes every request.
- [x] Verify focused tests, including zero requests for unapproved hosts/routes, no Ajax requests, checked Optum facet required, valid zero distinct from unknown, and first-row truncation visible before yielding.

## Task 2: Catalog and evidence

Files: `shared/python/services/job_ingest_worker.py`, `scripts/data/direct_employer_plan.py`, `scripts/live_provider_check.py`, `docs/coverage/ats-vendor-coverage.md`, provider audit evidence.

- [x] Add only the five proven board identities after registered-provider validation; use observed numeric organization IDs as tenants.
- [x] Add a live probe endpoint, measured advertised totals and explicitly limited rows read. Preserve Intuit's refusal.
- [x] Validate catalog/plan agreement offline and perform coordinated live provider/onboarding checks when granted.
- [x] Run focused provider/registry/ownership/onboarding checks, then `python3 scripts/verify.py --full` under Linux Python 3.12.
- [x] Report exact outcomes, unresolved full-board pagination, diff scope and checks to the parent.

Full verification was invoked under Linux; its final result is unavailable because WSL stopped responding. The checked item records the required invocation, not a pass. Linux CI remains required before merge.
