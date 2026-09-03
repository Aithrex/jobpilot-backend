# Tech feed — migration, backfill, rollout and rollback

**Status:** code landed. Staged audiences:

1. Internal/admin (`isAdmin`) — feed on unless `?techfeed=0`.
2. `VITE_TECH_FEED_BETA_PCT` (0–100) hashes the account email.
3. Wider beta after gates in `tech-feed-rollout-gates.md`.
4. Default-on only via `VITE_TECH_FEED=1` after measurable traffic.

The link override is unchanged and still beats everything for rollback.

Two steps below are **gated on explicit approval** and have NOT been run:
the taxonomy backfill (step 3) and the discovery re-arm (step 5.2).

---

## 0. What has already changed in production

Only one thing: **17 indexes were created** (step 2). Everything else is code
and documentation on disk.

## 1. Ship order

Each step is independently revertible and each is safe with the flag off.

| # | step | prod impact | approval |
|---|---|---|---|
| 1 | P0 correctness fixes | fixes live behaviour immediately | none — they are bug fixes |
| 2 | indexes | **done** | none — additive |
| 3 | taxonomy + identity backfill | 228k additive field writes | **REQUIRED** |
| 4 | unified backend contract | new routes, unused until step 6 | none |
| 5 | employer coverage + discovery re-arm | 5.1 read-only; 5.2 writes | **5.2 REQUIRED** |
| 6 | frontend behind `VITE_TECH_FEED` | none until enabled | none |
| 7 | shadow comparison | read-only | none |
| 8 | gradual rollout | user-visible | product decision |
| 9 | remove the legacy feeds | irreversible | **REQUIRED** |

## 2. Indexes (DONE)

17 created, +28 MB of 337 MB total. Build times 150 ms – 3.1 s each; none
locked.

```
companies                 discovery_status_priority, domain_idx,
                          name_normalized_idx, software_signal_priority
company_ats_endpoints     company_id_idx, status_vendor_idx,
                          vendor_tenant_idx, member_key_idx
discovery_queue           company_id_idx
job_postings              active_tech_scope_posted_at,
                          active_tech_family_posted_at,
                          active_tech_specialty_posted_at,
                          active_dedupe_group, active_variant_group,
                          active_tenant_posted_at
tech_feed_searches        search_id_unique, session_ttl
```

`companies` previously had ONE index (`company_id`) and
`company_ats_endpoints` had NONE. Every discovery tick COLLSCANned 82,815
documents. Measured after:

```
db.companies.find({discovery_status:"pending"}).sort({priority:-1}).limit(200)
  before: COLLSCAN, 82,815 docs examined
  after:  IXSCAN,      200 docs examined, 1 ms
```

This is very likely a material part of the throughput problem in
`TASK-company-discovery-throughput.md`, independent of everything else here.

**Rollback:** `db.<coll>.dropIndex("<name>")`. No data change.

## 3. Taxonomy + identity backfill — **NEEDS APPROVAL**

```bash
# what it would do (read-only)
PYTHONPATH=shared/python python scripts/backfill_tech_taxonomy.py --sample 30000

# apply
PYTHONPATH=shared/python python scripts/backfill_tech_taxonomy.py --apply --batch 1000

# undo
PYTHONPATH=shared/python python scripts/backfill_tech_taxonomy.py --rollback --apply
```

**Scope:** 228,183 active documents. **Additive only** — it `$set`s `tech`,
`dedupe_group_id`, `variant_group_id` and `canonical_url_key`. It never
deletes, never touches `status`, `posted_at`, `compensation`, or any field the
crawler owns. Rollback is a single `$unset` keyed on
`tech.backfill_source: "title+requirements"`, so it cannot remove a `tech`
written at ingest.

**Measured on a 30,000-document dry run:** 1,257 docs/s (~3 minutes for the
whole active set). 100% receive a canonical identity. Resulting distribution:
core 5.4%, adjacent 1.1%, non_tech 29.8%, unknown 63.6%.

**Why it matters,** measured on the live feed:

| | before backfill | after (expected) |
|---|---|---|
| candidates fetched per search | 2,000, of which 454 survive (4.4x over-fetch) | ~the page, filtered in Mongo |
| `active_tech_scope_posted_at` index | unusable | the primary access path |
| read-time classification | 2,000 titles per search, ~263 ms | 0 |
| classification quality | title only | title + requirements |
| dedupe | display-string fallback | stored canonical identity |

**Limitation, stated plainly:** descriptions live in S3 and are NOT re-fetched.
Backfilled rows are classified from title + already-parsed `requirements`,
which is better than the read path (title only) and worse than a fresh ingest
(full body). They are marked `tech.backfill_source` so a later pass can
re-run them with the body if that is ever worth doing.

**Rollout:** run it once, off-peak, from the EC2 box (same region as Atlas).
Watch `db.serverStatus().opcounters` and the ingest heartbeat; the crawlers
write to the same collection but to different documents and different fields,
so there is no write conflict. If the heartbeat goes stale, stop the backfill
first — the crawl matters more.

## 4. Backend contract (safe to deploy now)

New routes under the ALREADY REGISTERED `/api/jobs` edge prefix:

```
GET  /api/jobs/tech-feed/taxonomy
POST /api/jobs/tech-feed/search
GET  /api/jobs/tech-feed/coverage
```

No Terraform change (CLAUDE.md invariant 6). No entitlement gate. Nothing
calls them until the frontend flag is on.

`resume_service._process_job` gains a `tech_feed_search` branch for the
`async: true` path; it neither crawls nor calls an LLM.

## 5. Employer coverage

### 5.1 Reporting (read-only, run it now)

```bash
PYTHONPATH=shared/python python scripts/tech_employer_coverage.py \
    --json coverage.json --gaps 200
```

### 5.2 Discovery re-arm — **NEEDS APPROVAL**

```bash
PYTHONPATH=shared/python python scripts/tech_employer_coverage.py \
    --apply --apply-limit 500
```

Sets `discovery_status: pending` and `priority: 90` on up to 500 tech
employers currently `unresolved`, and upserts their `discovery_queue` row.

**This is a change to what the production crawler will do next.** It does not
change a schedule, a rate limit or a pacing key — the same worker, at the same
cadence, works a re-prioritised queue. But it front-loads ~500 employers ahead
of the existing backlog, so it needs a decision.

The employers it targets include Microsoft, Apple, Salesforce, Atlassian,
Dell, VMware, Shopify, Unity, Synopsys, SAP, Zoom, SpaceX, Zoox, Aurora,
Seagate, Cisco, Accenture and EPAM — all currently `no_portal` or with no
discovery attempt recorded, and all running a public ATS.

**Rollback:** the script records `rearmed_reason: "tech_employer_coverage"`;
`updateMany({rearmed_reason:"tech_employer_coverage"}, {$set:{discovery_status:"deferred"}})`.

## 6. Frontend (flag off by default)

`VITE_TECH_FEED`, with a localStorage override
(`aspirely.techFeed.enabled`) so it can be toggled per-session without a
deploy. With the flag off, the three legacy tabs behave exactly as today —
none of their code was modified.

## 7. Shadow comparison (before any user sees it)

For a fixed set of ~20 representative searches, run the legacy tab and the new
feed and compare:

| check | pass condition |
|---|---|
| recall | new feed returns >= 90% of the legacy tab's core-tech job_ids |
| precision | manual read of 25 new-only rows shows >= 95% genuinely tech |
| dupes | no job_id twice within a result set |
| latency | new-feed p95 <= legacy p95 |
| dates | no row shows a date the legacy tab called unknown |

Anything the new feed DROPS must be explainable by a filter the user set. A
drop with no reason is a bug, not an improvement.

## 8. Gradual rollout

1. internal accounts only (localStorage override)
2. 5% of authenticated users for 48h
3. 25% for 72h
4. 100%

Watch at each step: `search_zero_result_rate`, `search_p95_ms`,
`cursor_expiry_rate`, `not_interested_rate_by_reason`, and error rate on
`/api/jobs/tech-feed/*`. Roll back by flipping the flag; there is no data to
undo.

## 9. Removing the legacy feeds — **NEEDS APPROVAL**

Not before: 100% rollout has held for two weeks, the backfill is complete,
`read_time_classified_rate` is 0, and no open regression references the legacy
tabs. Deleting `workday_jobs_service` / `career_pages_service` /
`mnc_jobs_service` also deletes the `FEED_MATCH_MODE=scan` rollback, so it is
the last irreversible step, not the first tidy-up.

---

## Rollback summary

| change | how to undo | data loss |
|---|---|---|
| indexes | `dropIndex` | none |
| backfill | `--rollback --apply` | none (additive fields only) |
| backend routes | revert the deploy | none |
| frontend | `VITE_TECH_FEED=0` or clear the localStorage key | none |
| discovery re-arm | reset `discovery_status` by `rearmed_reason` | none |
| P0 fixes | revert individually; each is independent | none |

## What to watch

* `tech_feed_metrics.alarms()` — one call, the conditions that should page.
* `ingest_runner_heartbeat` — stale > 5 min means the box is wedged. This is
  the only thing separating "quiet night" from "the crawler died".
* `employer_name_conflict_rate` — currently **12.4%**, alarming above 5%. It
  falls when the 35 unresolved tenant conflicts are adjudicated.
* `read_time_classified_rate` — the backfill's progress bar. It should reach 0.
