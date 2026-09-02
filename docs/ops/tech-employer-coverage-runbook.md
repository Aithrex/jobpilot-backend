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

**Run it after the 2026-09-02 deploy specifically**, and not only because
there are new collections. Two of the canonical-employer posting indexes were
REDECLARED, not added: their `partialFilterExpression` changed from
`{"$type": "string"}` to `{"$exists": true}`, which is the difference between
an index the planner can use and one it cannot. Mongo will not alter an
existing index in place, so the migration DROPS the old spec before creating
the new one — an index whose name already exists with different options is
otherwise reported as a failure and silently left as it was.

```bash
# Verify afterwards. It fails loudly when a shape is answered by an index
# other than the one built for it, which is exactly how the $type defect
# survived: an IXSCAN on the wrong index reads as a green line.
PYTHONPATH=shared/python python scripts/explain_job_inventory.py
```

## 0b. The 2026-09-02 additions, in the order they are worth running

The passes below still work exactly as written. These are the newer steps that
sit around them, and they are listed here because running them out of order
wastes a sweep.

```bash
# Nothing to work on until you know what is blocked on what. READ-ONLY.
PYTHONPATH=shared/python python scripts/coverage_work_queue.py

# Junk first, so nothing below spends a request on it. Removes career sources
# and ATS endpoints that name a VENDOR'S OWN infrastructure - every Phenom
# customer's careers page loads the same four `*.phenompeople.com` asset
# hosts, and each one got registered as that employer's owner-verified board.
# It NEVER deletes a row the crawler is reading postings from: `greenhouse:css`
# is CloudKitchens and `greenhouse:manifest` is Manifest Cyber.
PYTHONPATH=shared/python python scripts/purge_infrastructure_sources.py --apply

# Then repair the ADDRESSES. `https://icims.com/<tenant>` is nobody's board -
# it was frozen onto 631 sources before `AtsProvider.board_url` existed, and
# the content pass fetches whatever is stored. No network requests: the real
# host is already recorded in the endpoint catalog, the redirect chain, or a
# posting the crawler fetched from that board.
PYTHONPATH=shared/python python scripts/repair_canonical_endpoints.py --apply

# Metadata first: it is DB-only, it needs no network, and the category and
# geography fields it fixes are read by the report you are about to run.
PYTHONPATH=shared/python python scripts/repair_employer_metadata.py --apply

# Attribution. Re-run after ANY verification sweep - newly proven ownership is
# newly attributable inventory. Reversible with --unstamp.
PYTHONPATH=shared/python python scripts/backfill_canonical_employer_id.py --apply

# The official-URL verification events `verified_at` is read from. --register
# hands what it finds to career_source_service, which decides the state.
PYTHONPATH=shared/python python scripts/verify_official_career_urls.py \
    --apply --register --workers 3 --rate 1.5

# Order the human queues by consequence rather than by arrival.
PYTHONPATH=shared/python python scripts/prioritize_reviews.py --apply

# THE number, and --persist is what makes the product read the same one.
PYTHONPATH=shared/python python scripts/employer_coverage_report.py \
    --markdown --persist
```

### Cadence, and what happens if nobody runs it

`--persist` is what the PRODUCT reads. The health banner serves the stored
snapshot with `measured_at`, `age_hours` and `stale`, so the failure mode of
forgetting is visible rather than silent: after
`coverage_snapshot.STALE_AFTER_HOURS` (3 days) the banner labels the figure
with its age and tells the reader to treat it as history.

That is the honest degradation and it is deliberate, but it is degradation.
**Re-run `--persist` after every verification sweep**, and at minimum weekly.

There is no EventBridge rule for it yet. Adding one is a Terraform change
outside both repos plus a `dispatch_scheduled` branch, and it was left undone
rather than shipped dark — a scheduled job that cannot fire is harder to
notice than a runbook step nobody has run.

> **Run ONE network pass at a time.** This is not politeness advice, it is a
> measured failure: one content pass run beside two others turned 830 sources
> `blocked` in a single sweep — boards the crawler had read that morning — and
> cut validated coverage by a third. The guard that now checks a `blocked`
> verdict against the live inventory was added because of it, and it is a
> backstop, not a licence to fan out.

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

```bash
# Pass 3 - content. Ownership proven, content never read: pass 2 walks TO the
# endpoint and does not read it, so a newly-proven source sits in
# `ownership_verified`, which is NOT coverage. This is the step that turns
# proven ownership into a coverage number.
PYTHONPATH=shared/python python scripts/verify_career_sources.py \
    --mode content --apply --workers 12 --rate 12
```

Run them in order. `--mode both` is passes 1 and 2; pass 3 is separate because
it is only worth running after pass 2 has produced something to validate.

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

## Re-verification is normal, and it is how a tightened check propagates

`career_sources.next_check_at` is set from the state
(`career_source_service._RECHECK`): 14 days for `validated`, 3 for `blocked`,
1 for `moved`. Re-running pass 2 re-adjudicates from the evidence, so a
STRICTER check demotes rows that no longer clear it — nothing has to be
deleted by hand.

That matters after a change to what counts as evidence. For example
`page_confirms_employer` originally matched the employer name anywhere in the
HTML, so a page that named the employer only inside an `href` read as "this
page names them". It now matches VISIBLE TEXT only (tags, scripts and
attributes stripped; `<title>` and `og:site_name` kept). Any row verified
before that change should be re-verified:

```bash
PYTHONPATH=shared/python python scripts/verify_career_sources.py \
    --mode employers --apply --workers 14 --rate 14
```

Pass 2 skips employers that already have a COVERED source, so to force a
re-check of those specifically, pass their ids:

```bash
PYTHONPATH=shared/python python scripts/verify_career_sources.py \
    --mode employers --apply --employer-id emp_xxxx --employer-id emp_yyyy
```
