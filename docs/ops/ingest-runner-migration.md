# Moving the 15-minute crawls off Lambda

> **Retired 2026-10-31.** This describes the AWS setup, which has been deleted. It is kept for history. Current hosting: [`infra/compose/README.md`](../../infra/compose/README.md).

**Status:** code landed, not yet cut over. Every EventBridge ingest rule is
still `ENABLED` and the box is not yet running the unit.

## In one paragraph

Every 15 minutes EventBridge wakes `jobpilot-resume-prod`, which self-invokes up
to 16 more Lambdas — one per shard — to crawl job boards. That is ~8,450 of the
Lambda's 12,544 daily invocations and 91.6% of its billed seconds. This moves
that crawling to the EC2 box we already run, and leaves Lambda doing only what a
user triggers and waits on: tailoring, auth, search, digests.

## Why, in priority order

1. **Per-vendor pacing is currently wrong, not just expensive.** `HostPolicy` is
   built per `run_shard` and its docstring says why: "a shard is a single Lambda
   invocation". Sixteen shards means sixteen independent pacing states, so
   `HOST_MIN_INTERVAL=0.15s` against the shared `boards-api.greenhouse.io` host
   is really up to ~107 req/s, and iCIMS — which ADR-004 §3.2 measured returning
   405 "Human Verification" on all sixty hostnames at six concurrent, and
   therefore paces at `max_concurrency=1` / 2.5s **vendor-wide** — really runs at
   up to sixteen concurrent connections. One process restores the politeness the
   registry already declares. This is very likely a contributor to the 70,470
   companies (85% of the registry) sitting in `deferred`.
2. **The 300s wall blocks coverage.** career-pages carries ~39 members per shard
   against shards sized for ~20, inside a 90s budget, so members are starved by
   BUDGET, not by schedule. Off Lambda, `JOB_INGEST_SHARD_BUDGET` and the
   per-tick shard count can both rise — which is what turns newly discovered
   boards into actual postings.
3. **Cost.** ~$144/month of Lambda becomes ~$6-25/month plus the box.

## Measured baseline (24h to 2026-09-02, `jobpilot-resume-prod`)

| | value |
|---|---|
| invocations | 12,544 |
| billed seconds | 200,948 |
| invocations ≥10s | 4,377 (carry **91.6%** of billed time) |
| memory used, runs >60s | avg 290 MB / p95 519 MB of **1536 MB** allocated |
| Aug pre-credit Lambda cost | **$50.46** of $108.53 total (all credit-covered) |
| current run rate | ~9.0M GB-s/month ≈ **$144/month** |

## Expected end state

| | now | after |
|---|---|---|
| invocations/day | 12,544 | ~4,100 |
| billed sec/day | 200,948 | ~16,900 |
| Lambda cost/month | ~$144 | ~$6-25 |

The range is honest: some of the 10-60s bucket is user work (tailoring, Gmail
sync), not ingest, and that part stays on Lambda.

## The four code fixes this required

Lambda's short-lived containers were hiding all four. They are already merged
and the full suite (2,959 tests) is green with them.

| fix | what it was | why a box breaks it |
|---|---|---|
| `_REGISTRY_CACHE` TTL (`job_ingest_worker.py`) | cached at module scope, cleared only by a test hook | permanent in a long-lived process: `discover_companies` writes new `company_ats_endpoints` rows every 15 min and they would never be crawled until a restart — silently capping the exact pipeline this work widens |
| `run_bounded` bounded drain | `shutdown(wait=False, cancel_futures=True)`, no join | correct for Lambda (the container freezes and takes the threads); an accumulating thread/socket leak on a box |
| `MongoClient(maxPoolSize=…)` (`utils/db_connect.py`) | PyMongo default 100 per client | fine at one shard per container; 16 shards × ~12 threads now share ONE client |
| lease owner id | `AWS_LAMBDA_LOG_STREAM_NAME` or the literal `"local"` | `job_source_state` now names `hostname:pid`, so you can see WHICH box holds a shard |

## Cutover

"Cutover" = flipping one crawl from Lambda to the box. Order matters:
**start the box first, disable the rule second.** Running a source in both
places is safe — `inv.acquire_lease` makes the loser a no-op — but pays twice.
Running it in *neither* silently stops that crawl.

```bash
# 0. one-time: resize the box 2 GB -> 4 GB
aws ec2 stop-instances  --profile aithrex --region us-east-1 --instance-ids i-06bf0e03e60d9d409
aws ec2 wait instance-stopped --profile aithrex --region us-east-1 --instance-ids i-06bf0e03e60d9d409
aws ec2 modify-instance-attribute --profile aithrex --region us-east-1 \
    --instance-id i-06bf0e03e60d9d409 --instance-type t3.medium
aws ec2 start-instances --profile aithrex --region us-east-1 --instance-ids i-06bf0e03e60d9d409

# 1. ship code + unit, start on ONE source
./infra/ec2/deploy-ingest-runner.sh
sudo cp infra/ec2/ingest-runner.service /etc/systemd/system/   # on the box
sudo systemctl daemon-reload && sudo systemctl enable --now ingest-runner

# 2. confirm it is really running before switching anything off
#    (heartbeat is the ONLY thing separating "quiet night" from "wedged")
mongosh "$MONGO_URI" --eval 'db.ingest_runner_heartbeat.find().pretty()'

# 3. now disable the matching rule
aws events disable-rule --profile aithrex --region us-east-1 --name jobpilot-ingest_icims-prod
```

Then widen `INGEST_SOURCES` one source at a time (`icims` → `oracle` →
`career-pages` → `workday`), disabling each rule after the box has taken it,
and finally drop `--no-discovery` and disable `jobpilot-discover_companies-prod`.

`ingest_maintenance` is a DAILY retention purge that archives pinned postings to
S3 before deleting them. It stays on Lambda until the four crawls are proven.

### Rollback

Re-enable the one rule and drop that source from `INGEST_SOURCES`:

```bash
aws events enable-rule --profile aithrex --region us-east-1 --name jobpilot-ingest_icims-prod
sudo systemctl restart ingest-runner
```

No data migration, no schema change, nothing to undo — the leases and watermarks
in `job_source_state` are the same rows either way.

## What to watch

* **`ingest_runner_heartbeat`** — stale > 5 min means the box is wedged. Lambda
  could not silently stop; a systemd unit can. **Alarm on this.**
* `job_ingestion_runs` — `counts.fetched` per shard should go UP once the budget
  rises, and `status` should stay `succeeded`.
* `job_source_state` — `consecutive_failures` should go DOWN if the pacing theory
  is right. That is the falsifiable prediction of this whole migration.
* Box memory. Runs >60s used p95 519 MB *per Lambda*, but most of that is the
  interpreter and the shared layer, paid once here. `MemoryMax=2500M` on the unit
  is the guard.

## Known tripwire

The box also hosts the Playwright/Chromium auto-apply executor. Auto-apply is
currently dark (both schedulers `DISABLED`, `AUTO_SUBMIT_ENABLED=false`), which
is the only reason co-tenanting is acceptable. **When auto-apply is re-enabled,
split the crawler onto its own instance** — Chromium and a 16+ shard fan-out
should not share 4 GB.
