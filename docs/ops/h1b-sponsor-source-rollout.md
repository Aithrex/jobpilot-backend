# Rollout — Apple + the Eightfold page-size fix

**Status:** code on `feat/h1b-sponsor-source-discovery`, **not deployed, not
started.** Every ingest source and every matching EventBridge rule is still
disabled (see [`ingest-runner-migration.md`](ingest-runner-migration.md)).
**Date:** 2026-09-15
**Evidence:** [`docs/sourcing/h1b-sponsor-board-discovery.md`](../sourcing/h1b-sponsor-board-discovery.md)

Turning the crawler on is a deploy and an operations decision. This file is the
exact sequence and the numbers to check it against — it does not perform it.

---

## What changed, and the blast radius of each

| Change | File | Blast radius |
| --- | --- | --- |
| `preflight` key on the config-board provider | `ats_providers/config_boards.py` | **One extra request per listing**, only for rows that declare it. Today that is Apple alone. |
| Apple adapter `blocked` → `verified`, + handshake, + `page_size`/`max_pages` | `official_career_registry.py` | Apple becomes a crawlable member. It was already a registered vendor (`custom_apple`) that reported `BLOCKED`. |
| Apple row in `ENTERPRISE_CATALOG` + `MNC_CATALOG` | `career_pages_service.py`, `mnc_catalog.py` | Adds `custom_apple-apple` to the career-pages shard, and moves Apple off the metered `aggregator` engine on the MNC tab. |
| Eightfold `page_size` 20 → **10**, `max_pages` 5 → **260** | `ats_providers/enterprise.py` | **Four existing members fetch ~50× more rows.** This is the one to watch. |
| Same page-size fix on the interactive fetcher | `career_pages_service._fetch_eightfold` | The MNC/career-pages tab, per request. Capped at 6 pages × 3 terms. |
| `_fetch_apple` / `_fetch_successfactors` in `ENTERPRISE_FETCHERS` | `career_pages_service.py` | New keys only; no existing key changed. |
| FY2026 approvals for 7 slugs | `h1b_sponsor_data*.py` | Badge text only. |

## What it is expected to yield

Measured live 2026-09-15, per member, end to end through the real provider:

| Member | before (active) | after | walk time |
| --- | ---: | ---: | ---: |
| `custom_apple-apple` | 0 | **≈3,600 US** (6,141 rows / 4,905 distinct; 4,503 US-located rows) | 85 s |
| `eightfold-microsoft` | 29 | **1,178** | 37 s |
| `eightfold-lockheedmartin` | 19 | **1,500+** (was capped at 150 pages; 260 now) | 57 s |
| `eightfold-qualcomm` | 4 | **741** | 22 s |
| `eightfold-netflix` | 10 | **204** | 6 s |
| | | **≈ +7,200** | ≈ 3.5 min total |

If the first crawl produces materially fewer than these, something is wrong —
these are not projections, they are counts.

---

## Sequence

Nothing here is Apple-specific until step 3; steps 1–2 are the normal deploy.

```bash
# 0. verification gate — CI is the authority, this is the local mirror
python3 scripts/verify.py --full

# 1. merge and let CI ship the layer + the box.
#    `deploy-ingest-runner` fires on any push touching shared/python/**,
#    which this branch does. Do NOT hand-copy: the box drifting 20 hours
#    behind main is the failure ingest-runner-migration.md documents.
git push origin feat/h1b-sponsor-source-discovery      # then PR + merge

# 2. confirm the box took the build BEFORE enabling anything
ssh-less, over SSM:
aws ssm send-command --profile aithrex --region us-east-1 \
  --instance-ids i-06bf0e03e60d9d409 \
  --document-name AWS-RunShellScript \
  --parameters 'commands=["systemctl is-active ingest-runner","git -C /opt/jobpilot rev-parse --short HEAD"]'

# 3. crawl ONE new member by hand first, on the box, before any rule is enabled.
#    This is the cheap proof that the deployed code reads what this branch read.
python3 - <<'PY'
import sys, time; sys.path.insert(0, "/opt/jobpilot/shared/python")
from datetime import datetime, timedelta, timezone
from services import job_ingest_worker as w
m = {x["member_key"]: x for x in w._load_career_members()}["custom_apple-apple"]
rows = w.list_member(m, cutoff=datetime.now(timezone.utc) - timedelta(days=30),
                     policy=w.HostPolicy(), deadline=time.monotonic() + 300)
print("apple rows:", len(rows), "truncated:", m["_listing_truncated"])
PY
# expect: ~4,500-6,100 rows, truncated False.
# If it prints 0 with no error, the preflight is not firing — check that the
# adapter row still carries request.preflight. 200-with-zero-rows is this
# board's failure mode and it looks exactly like "Apple is not hiring".

# 4. only then, widen the crawl the normal way
#    (career-pages is the shard that carries BOTH Apple and the four Eightfold
#     members, so one source flip covers this whole change)
INGEST_SOURCES=...,career-pages    # on the unit, then:
sudo systemctl restart ingest-runner
aws events disable-rule --profile aithrex --region us-east-1 \
    --name jobpilot-ingest_career-pages-prod
```

---

## What to watch, in the first two runs

1. **`job_ingestion_runs`, the `career-pages` shard.** `counts.fetched` should
   rise by roughly 7,000 across the first full sweep. A rise of ~200 means the
   Eightfold policy did not reach the box.
2. **Pacing.** Eightfold now makes up to 260 requests per tenant instead of 5,
   across four tenants — ~1,000 requests where there were 20. `min_interval` is
   0.25 s and `max_concurrency` 4, keyed per host, and each tenant is its own
   host, so this is ~65 s of steady traffic per tenant and nothing bursty. If
   `job_source_state.consecutive_failures` climbs for `eightfold`, drop
   `max_pages` to 120 first — that still clears Microsoft.
3. **`jobs.apple.com` politeness.** One preflight + up to 340 POSTs per sweep,
   one host, 0.25 s apart. `jobs.apple.com` serves no `robots.txt` (404 →
   allowed). If it ever returns 403 with a challenge marker, the vendor cools
   down for 900 s on its own — do not add retries.
4. **Attribution.** Apple postings will land with `employer_id: "apple"` and
   **no `canonical_employer_id`** until `career_sources` carries an
   owner-verified row for `custom_apple:apple`. Until then Apple shows as 0 in
   any count that uses the canonical id — which is exactly how this whole gap
   was mis-measured in the first place. Run:

   ```bash
   PYTHONPATH=shared/python python scripts/verify_career_sources.py \
       --mode employers --employer-id emp_d471a0526f61
   # dry run. Add --apply --register only after reading the proposed evidence.
   ```

   The evidence is a 4a domain chain (`apple.com` → `jobs.apple.com`), which
   `adjudicate_ownership` treats as sufficient alone.
5. **Retail volume.** ~60% of Apple's US board is Retail (Specialist, Genius,
   Business Expert). They are real US postings and real sponsorship-relevant
   rows for some users, but they are not software. `tech_taxonomy` will class
   them `non_tech`, which keeps them out of the default search — check that it
   does, because 2,700 retail rows leaking into the tech feed would be the
   visible regression of this change.

## Rollback

Each piece reverts independently and none of them writes a schema:

```bash
# Eightfold only — back to one page per tenant (the old behaviour)
#   ats_providers/enterprise.py: _EIGHTFOLD_POLICY page_size=20, max_pages=5

# Apple only — no code change needed
#   official_career_registry.py: set the apple row's "status" back to "blocked".
#   ConfigBoardProvider.validate() then reports BLOCKED and the member stops
#   being crawled, without touching the catalog rows or the provider.

# Everything — revert the branch and redeploy. Postings already written stay;
# they are ordinary rows and `_close_stale_in_shard` retires them normally.
```

## Not in this rollout

* **The robots.txt work**, which is two changes and neither is here. The
  parser (`career_portal_discovery.RobotsCache`) uses stdlib
  `urllib.robotparser`, which is first-match rather than longest-match and
  mangles `Disallow: /?` into `Disallow: /`; and `RobotsCache` has exactly one
  caller, in DISCOVERY — **the listing path consults robots.txt nowhere**.
  Both change what the crawler is permitted to fetch and deserve their own
  review. §4 of the sourcing doc.
* **EY / SuccessFactors RMK.** The provider is verified live but EY's ownership
  is one evidence class short and it has no `employers` row. §2 of the sourcing
  doc.
* **Google and Meta.** A robots refusal and a ToS notice respectively. Neither
  is a technical blocker and neither is ours to decide.
* **The Indian IT-services cohort** (Infosys, TCS, HCLTech, Capgemini,
  LTIMindtree, Tech Mahindra, Mphasis, Wipro — 14,421 FY2026 approvals
  between them). Two need a new vendor (RippleHire), two need a rendered
  pass, and LTIMindtree — the one that reads perfectly today — publishes
  **54 jobs, none of them US**. Before any of that work is funded, run the
  one-request board-size test in §3.1 of the sourcing doc: this cohort is
  top-ranked by approvals and may be near-worthless as US inventory.
