# High-value source coverage — operator commands

**Do not run `--apply` or `--persist` until an operator authorizes it.**

Live queue (2026-09-03, read-only):

| Bucket | Employers | Core-tech jobs |
|---|---:|---:|
| content_pending | 71 | 1,543 |
| endpoint_unproven | 384 | 4,523 |
| Combined | 455 | 6,066 (~93% of uncovered core mass) |
| Salesforce | source_no_endpoint | 224 core |

Priority names: Oracle, Salesforce, Unity, F5.

## 1. Content pass (ownership already proven)

```bash
cd jobpilot-backend
PYTHONPATH=shared/python python scripts/coverage_work_queue.py --bucket content_pending --ids > /tmp/content_pending.txt
# ONE network pass at a time:
PYTHONPATH=shared/python python scripts/verify_career_sources.py --mode content --limit 400
# then, with authorization:
PYTHONPATH=shared/python python scripts/verify_career_sources.py --apply --mode content --limit 400
```

## 2. Ownership walk for endpoint_unproven (Oracle, Unity, F5, …)

```bash
PYTHONPATH=shared/python python scripts/coverage_work_queue.py --bucket endpoint_unproven --ids > /tmp/unproven.txt
PYTHONPATH=shared/python python scripts/verify_career_sources.py --mode employers --order tech-yield --limit 200
# authorized:
PYTHONPATH=shared/python python scripts/verify_career_sources.py --apply --mode employers --order tech-yield --limit 200
PYTHONPATH=shared/python python scripts/verify_career_sources.py --apply --mode backlink --limit 400
```

## 3. Salesforce (no endpoint on the source row)

```bash
PYTHONPATH=shared/python python scripts/verify_official_career_urls.py
PYTHONPATH=shared/python python scripts/verify_career_sources.py --mode both --order tech-yield --limit 50
```

## 4. Registry rows that exist in code but not live `employers`

xAI, Axonius, Tines, Cato Networks, Citadel, plus 2026-09-03 intake (SSI, Thinking Machines Lab, Periodic Labs, OpenEvidence):

```bash
PYTHONPATH=shared/python python scripts/backfill_employer_registry.py
# authorized:
PYTHONPATH=shared/python python scripts/backfill_employer_registry.py --apply
PYTHONPATH=shared/python python scripts/seed_company_discovery_candidates.py
# authorized:
PYTHONPATH=shared/python python scripts/seed_company_discovery_candidates.py --apply
```

## 5. Canonical attribution + conflict queue (~70 boards / ~9,500 postings)

```bash
PYTHONPATH=shared/python python scripts/backfill_canonical_employer_id.py
PYTHONPATH=shared/python python scripts/prioritize_reviews.py --top 40
# authorized:
PYTHONPATH=shared/python python scripts/backfill_canonical_employer_id.py --apply
PYTHONPATH=shared/python python scripts/prioritize_reviews.py --apply
```

## 6. Persist coverage snapshot (after verification, not before)

```bash
PYTHONPATH=shared/python python scripts/employer_coverage_report.py --markdown
# authorized:
PYTHONPATH=shared/python python scripts/employer_coverage_report.py --markdown --persist
```

Recovery if a scheduled persist fails: the previous snapshot remains. Re-run the persist command above. Check `GET /api/admin/ops/tech-feed` for `coverage_run`.

## 7. Indexes (additive)

```bash
PYTHONPATH=shared/python python scripts/migrate_indexes.py
```

## Rollback

- Canonical ids: `python scripts/backfill_canonical_employer_id.py --apply --unstamp`
- Coverage snapshot: do not persist; the product keeps the last good document
- Unified feed: `?techfeed=0` writes `localStorage['aspirely.techFeed.enabled']=0`
