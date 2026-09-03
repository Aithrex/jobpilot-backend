# Browser-rendered discovery — coverage acceptance (2026-09-03)

**Session of 2026-09-03.** Built `--mode browser` on
`scripts/verify_career_sources.py`, pointed the existing
`BrowserExecutor`/Playwright stack at careers pages through
`Fetcher.render` (same SSRF / robots / pace / size-cap gates), and ran it
against the work queue.

## Before → after (persisted)

| metric | before (session start) | after `--persist` |
|---|---:|---:|
| technology denominator | 2,813 | **2,829** (not weakened) |
| covered employers | 1,005 (35.7%) | **1,027 (36.3%)** |
| target | ≥ 1,317 of ≥ 2,634 (≥ 50%) | **NOT met** — short by ~290 |
| Tier-A baseline (`baseline` batch) | 34/94 | **37/94** |
| Tier-A target | ≥ 75/94 | **NOT met** — short by 38 |

Denominator grew (2,813 → 2,829). Covered grew by 22. Neither goal is in
reach of another browser sweep at the measured hit rate.

## What was built

1. **`Fetcher.render`** in `career_source_ownership.py` — Playwright
   Chromium behind the same SSRF / robots / pace gates as `Fetcher.get`.
   Returns rendered HTML + `network_urls`. A Cloudflare / Access Denied /
   "Just a moment" page is `error: bot_challenge` — never solved.
2. **`walk_to_endpoints(..., render=True)`** fingerprints the rendered DOM
   **and** the XHR list through the existing `endpoints_from_page` →
   `adjudicate_ownership` path. No second ownership rule.
3. **`--mode browser`** on `verify_career_sources.py` — fifth mode, default
   concurrency clamped to 2, Chromium closed at end of sweep.
4. **`api.greenhouse.io` fingerprint** — JS shells (DigitalOcean) XHR this
   host, not only `boards-api.greenhouse.io`. Without it the rendered pass
   saw the request and still could not name the board.
5. **`ashby` reserved tenant `api`** — a rendered page linking
   `jobs.ashbyhq.com/api` was registering `ashby:api` as Aurora's board.

## Measured browser yield

| sweep | employers | endpoints found | ownership proven | blocked |
|---|---:|---:|---:|---:|
| Tier-A `source_no_endpoint` | 47 | 5 | 4 | 4 |
| work-queue top 80 | 80 | 51 | 12 | 6 |

Effective convert-to-covered on the Tier-A shell cohort was ~3 employers
(DigitalOcean, HiddenLayer, VMware). The high-yield sweep added more via
merge with prior content evidence (11 sources landed already covered).

## Why 50% / 75 Tier-A are not reachable from here

The remaining gap is **not** "we have not swept enough". Re-stated with
numbers from `coverage_work_queue.py` after this session:

```
technology denominator 2,829   covered 1,027   uncovered 1,802

bucket                 employers  core-tech    active  tier-A
  content_pending             71      1,245     5,830       5
  endpoint_unproven          362      3,380    21,276       8
  source_no_endpoint         801        271       686      43
  no_source                  568         87       652       1
```

Of the 43 Tier-A still in `source_no_endpoint` after the browser pass, the
failures fall into three honest buckets:

1. **JS shell, no ATS fingerprint even after render** — Atlassian, Shopify,
   Deel, Blue Origin, EPAM, Dell, Meta, Apple, Google, IBM, monday.com,
   Procore, Etsy, Zoom, Riot, Valve, … Their careers UI is first-party or
   white-labelled in a way no registered fingerprint matches. A browser
   does not invent an ATS vendor.
2. **Bot challenge / edge deny** — Salesforce (Access Denied), Cognizant
   (Cloudflare), Teradyne, Ampere/xAI-class 403/429. Recorded as `blocked`.
   **Not bypassed.**
3. **Domain never verified** — Fortinet, Tesla, Keysight, SAP, Etsy: the
   homepage does not name the employer under `page_confirms_employer`, so
   path (a) cannot start. Browser render of the careers page cannot fix a
   domain we refuse to call verified.

Extrapolating the measured ~8–15% ownership-proof rate across the remaining
~800 `source_no_endpoint` rows buys on the order of **60–120** covered
employers after a content pass — still ~170 short of 50%, and nowhere near
+38 Tier-A. Closing the rest requires either custom first-party adapters
(out of scope for "prefer generic ATS"), or bot-protection bypass (forbidden).

## Content-pass side effect (not solved here)

Running `--mode content` against the `ownership_verified` queue demoted
dozens of Oracle/Workday rows to `dead` when hydrate had no host
(`failure_detail: no host`). Those were never covered, so the coverage
percentage did not fall, but the path to covering them did. The inventory
guard protects `blocked`/`empty` against live postings; it does not protect
"we asked the wrong URL". Left as a follow-up — do not re-run a broad
content pass on hostless Oracle/Workday rows until hydrate is fixed.

## Verification

```
PYTHONPATH=shared/python pytest tests/test_browser_discovery.py \
    tests/test_ownership_walk_reach.py tests/test_reserved_tenants.py -q
# 78 passed

PYTHONPATH=shared/python python scripts/employer_coverage_report.py \
    --markdown --persist
# covered 1,027 / 2,829 (36.3%); Tier-A baseline 37/94

PYTHONPATH=shared/python python scripts/coverage_work_queue.py
```

Full `pytest tests evals -q` was started; 11 pre-existing failures in
`evals/test_tailor_quality.py` and `tests/test_resume_layout.py` are
documented in `docs/ops/manual-steps.md` and are not this work.
