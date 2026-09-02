# Running the employer-identity and coverage pipeline

Every step is **dry-run by default**. `--apply` is the only thing that writes,
and each script says what it would do before it does it.

## 0. Once, after a deploy

```bash
cd jobpilot-backend
PYTHONPATH=shared/python python scripts/migrate_indexes.py
```

Creates the `employers`, `career_sources`, `tech_feed_feedback` and keyset /
country indexes. Idempotent. Three `[FAILED]` lines for pre-existing
duplicate-name specs on `company_ats_endpoints` and `discovery_queue` are
expected and harmless — both members of each pair already exist.

## 1. Build the registry

```bash
PYTHONPATH=shared/python python scripts/backfill_employer_registry.py            # report
PYTHONPATH=shared/python python scripts/backfill_employer_registry.py --apply
```

Joins `companies`, the curated seed, the official career-URL registry, the
three Python catalogs, `company_ats_endpoints` and active `job_postings` into
`employers` + `career_sources`. Re-runnable; `$addToSet` everywhere, so a
second run adds evidence and duplicates nothing.

**Coverage DROPS here, to roughly zero.** That is the number becoming true:
the old figure counted endpoints that answered, and nothing in the database
yet records WHO OWNS a board. Step 2 earns it back.

## 2. Prove ownership

```bash
# Pass 1 - cheap. Asks each ATS who owns the board. ~2 requests per source.
PYTHONPATH=shared/python python scripts/verify_career_sources.py \
    --mode endpoints --apply --workers 10 --rate 12

# Pass 2 - decisive. Verifies the employer's domain and walks from it to the
# board. ~6-10 requests per employer. The ONLY path that reaches Workday,
# iCIMS and Oracle, which name no owner.
PYTHONPATH=shared/python python scripts/verify_career_sources.py \
    --mode employers --apply --workers 14 --rate 14 --budget 8000
```

Politeness is not optional: one global rate limit, a per-host pace, robots.txt
on every origin, and an SSRF check on every hop. A sweep that gets us blocked
costs more coverage than it buys. Start at `--rate 8` on an unfamiliar
network and watch `pass*_error:*` in the summary.

Pass 1 prints a per-vendor table. `no signal` for workday / icims / oracle /
ukg / paylocity / bamboohr is CORRECT — those APIs publish no owner, which is
exactly why pass 2 exists.

## 3. Re-arm what is still uncovered

```bash
PYTHONPATH=shared/python python scripts/rearm_uncovered_employers.py           # report
PYTHONPATH=shared/python python scripts/rearm_uncovered_employers.py --apply --limit 2000
```

Ordered by MEASURED technology-job yield, not by provider board count.
Employers with an open row in `employer_merge_reviews` are reported and
deliberately NOT enqueued: re-crawling before the identity is settled files
the postings under the wrong name again.

## 4. Report

```bash
PYTHONPATH=shared/python python scripts/employer_coverage_report.py --markdown
```

Read-only. Regenerates `docs/coverage/tech-employer-coverage.md`, stamped with
the source revision and the inventory watermark, including the Tier-A roll
call (every employer in the official career-URL registry, and the state it is
actually in).

## 5. Taxonomy backfill, after any taxonomy change

```bash
PYTHONPATH=shared/python python scripts/backfill_tech_taxonomy.py --sample 3000   # report
PYTHONPATH=shared/python python scripts/backfill_tech_taxonomy.py --apply --status all
```

Version-aware: it selects rows whose `tech.taxonomy_version` is not current,
so a version bump is backfillable without `--all`.

## Rolling back

| what | how |
|---|---|
| the registry | `db.employers.drop()`, `db.career_sources.drop()`, `db.employer_merge_reviews.drop()`. Nothing else reads them yet; the crawler is untouched. |
| the taxonomy backfill | `scripts/backfill_tech_taxonomy.py --rollback` (`$unset tech` + the identity fields it wrote) |
| the tech feed | the frontend flag: `localStorage['aspirely.techFeed.enabled'] = 'false'`, or `?techfeed=0`, or `VITE_TECH_FEED=0` at build time |
| the feedback endpoint | `db.tech_feed_feedback.drop()`; suppression degrades to "nothing suppressed" |

## Explain-plan evidence for the finalized query paths

```bash
PYTHONPATH=shared/python python scripts/explain_job_inventory.py --no-scratch
```

Read-only; also seeds and deletes a scratch corpus unless `--no-scratch`.
Measured against live `job_postings` on 2026-09-02:

| path | index | keys examined | returned | ms |
|---|---|---:|---:|---:|
| tech-feed retrieve (default scope) | `active_tech_scope_keyset` | 2,000 | 2,000 | 15 |
| tech-feed retrieve (family filter) | `active_tech_family_keyset` | 2,000 | 2,000 | 15 |
| tech-feed retrieve (specialty filter) | `active_tech_specialty_keyset` | 123 | 123 | 9 |
| keyset continuation | `active_keyset_posted_job` | 250 | 250 | **24** |
| keyset continuation, WITHOUT the range bound | `active_keyset_posted_job` | 71,333 | 250 | **13,540** |
| keyset continuation (undated tail) | `active_keyset_posted_job` | 250 | 250 | 13 |
| country filter | `active_country_posted_at` | 1,086 | 1,086 | 9 |

No blocking sort on any of them. The last-but-one row is kept in the harness on
purpose: it is the SAME query without `{"posted_at": {"$lte": <cursor>}}`, and
it is 565x slower. An `$or` alone cannot bound an index range, so Mongo used
only the base freshness bound and evaluated the tie-break as a residual over
everything after it. If that clause is ever removed, this line moves and the
harness says so.

The two `*_keyset` index families exist because the scope/family/specialty
indexes end at `posted_at`, and a retrieval sorted `(posted_at DESC, job_id
ASC)` — which it must be, or the keyset order is not total — fell back to a
blocking sort over the whole matched set before the limit.
