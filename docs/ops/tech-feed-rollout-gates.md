# Tech feed rollout gates

The flag is unchanged and a staged rollout is still a link:
`localStorage['aspirely.techFeed.enabled']`, written by `?techfeed=1` /
`?techfeed=0` (`src/lib/techFeedFlag.ts`). What this document adds is the set
of gates that must pass BEFORE the flag is widened, and — more importantly —
the exact command that produces each number, so nobody has to take the answer
on trust.

A gate with no command behind it is an opinion.

## How to run all of them

```bash
# backend, from jobpilot-backend/
PYTHONPATH=shared/python pytest tests evals -q
PYTHONPATH=shared/python python evals/eval_live_titles.py
PYTHONPATH=shared/python python evals/eval_fit_score_domains.py
PYTHONPATH=shared/python python evals/eval_tech_taxonomy.py
PYTHONPATH=shared/python python scripts/employer_coverage_report.py --persist
PYTHONPATH=shared/python python scripts/backfill_canonical_employer_id.py     # dry run
PYTHONPATH=shared/python python scripts/coverage_work_queue.py
PYTHONPATH=shared/python python scripts/explain_job_inventory.py

# frontend, from jobpilot-frontend/
pnpm lint && pnpm build && pnpm test
```

## 1. Correctness gates

| gate | threshold | command |
|---|---|---|
| Offline suite green | 0 failures | `pytest tests evals -q` |
| Taxonomy golden set | core precision >= 0.95, recall >= 0.92, non-tech leakage <= 0.02 | `evals/eval_tech_taxonomy.py` |
| Live-title classification | scope accuracy >= 0.90, reviewer-override accuracy >= 0.90, non-tech leakage <= 0.02 | `evals/eval_live_titles.py` |
| Fit scoring | zero false-INELIGIBLE, false-eligible rate <= 0.05, mean NDCG@5 >= 0.80, grade means monotone with a gap >= 5, experience separation >= 6 | `evals/eval_fit_score_domains.py` |
| Frontend suite | 0 failures | `pnpm test` |
| Frontend build | succeeds | `pnpm build` |

**Not a gate, and deliberately so:** `pnpm lint` currently reports 106
pre-existing `@typescript-eslint/no-explicit-any` errors, every one of them in
files this work did not touch. Gating on a number that was already red would
either block the rollout for unrelated debt or invite somebody to lower the
bar. The gate that IS enforced is that the tech-feed files added or changed
here lint clean, which they do.

## 2. Coverage gates

Coverage is not a release gate in the sense that a low number blocks a
rollout — the feed is useful at 33% and at 60%. What IS a gate is that the
number shown to a user is measured, dated and honest.

| gate | threshold | command |
|---|---|---|
| A coverage measurement exists and is fresh | `measured.available` true, `stale` false | `scripts/employer_coverage_report.py --persist`, then `GET /api/jobs/tech-feed/coverage` |
| Canonical attribution of owner-verified postings | >= 95% | `scripts/backfill_canonical_employer_id.py` (dry run prints it) |
| No employer silently leaves the denominator | every Tier-A row has a state, `not_in_registry` included | the Tier-A roll call in the report |
| The four coverage counts are reported separately | `covered_employers`, `covered_career_sources`, `ownership_verified_unvalidated_sources`, `employers_yielding_postings` all present | the report's `coverage` block |

## 3. Latency gates

| gate | threshold | command |
|---|---|---|
| Every tech-feed query path is served by the index BUILT FOR IT | the script names the expected index per shape and fails when a different one answers; no blocking sort | `scripts/explain_job_inventory.py` |
| The canonical indexes are partial on `$exists`, not `$type` | a `$type` filter makes the index ineligible for every equality we issue. 1,544 ms vs 17 ms, measured | `scripts/migrate_indexes.py` — and it DROPS the old spec, because Mongo will not alter one in place |
| Search p95 | <= 2,500 ms | `tech_feed_metrics.search_metrics(hours=24)` |
| Coverage block is cached, not recomputed per search | `COVERAGE_TTL_SECONDS` respected; the measured half comes from a stored snapshot | `tech_feed_service.coverage_block` |
| Company autocomplete | at most two bounded queries per keystroke burst — an indexed `name_key` prefix, and the contains scan only when the prefix did not fill the page — 220 ms client debounce, 60 req/min per IP | `employer_directory.search` |

## 4. Error-rate gates

| gate | threshold | how |
|---|---|---|
| Search 5xx rate | < 0.5% over 24h | Lambda metrics for the `jobs` service |
| Ingest runner alive | heartbeat < 90 min | `source_health_block()` — the banner already alarms |
| No source silently stale | every stale source named in the banner | `source_health_block()["sources"]` |
| Telemetry cannot fail a user action | every client path `void`-returning and try/caught; server returns 204 on a dropped batch | `src/lib/__tests__/techFeedTelemetry.test.ts`, `tests/test_tech_feed_telemetry.py` |

## 5. What to watch in the first week, and where it comes from

All of it is non-PII by construction — filter KEYS, bucketed counts and vendor
slugs, against a salted one-way account pseudonym.

```bash
PYTHONPATH=shared/python python -c \
  "from services import tech_feed_telemetry as t; import json; print(json.dumps(t.summary(hours=168), indent=2))"
```

* **`zero_result_reasons`** — which filter is emptying pages. The server
  computes it, so this is not a guess about the user's intent.
* **`filter_use`** — which filters anyone actually touches. A filter nobody
  uses and a filter that does nothing look identical from the code.
* **`card_apply_click_rate`** — the closest thing the feed has to an outcome.
* **`source_failures_by_vendor`** — failures the USER was told about, which is
  a different event from an internal crawl failure and only one of them is
  visible to somebody looking for a job.

## 6. Rollback

Three independent levers, in increasing order of blast radius:

1. **The flag.** `?techfeed=0`, or clear
   `localStorage['aspirely.techFeed.enabled']`. Instant, per user.
2. **The canonical id.**
   `python scripts/backfill_canonical_employer_id.py --apply --unstamp`
   removes the three fields. Every reader falls back to `(ats, tenant)` and
   then to the name, which is what they did before — this is why the field is
   additive and why `employer_id` was never touched.
3. **The registries.** `employers`, `career_sources`,
   `employer_board_conflicts`, `coverage_snapshots`,
   `official_career_url_checks`, `tech_feed_saved_searches` and
   `tech_feed_events` are all additive collections. Dropping any of them loses
   that feature and nothing else; `companies`, `company_ats_endpoints` and
   `job_postings` are untouched by design, so the crawler reads exactly the
   rows it read before.
