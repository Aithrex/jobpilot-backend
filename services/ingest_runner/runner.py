#!/usr/bin/env python3
"""Long-lived ingest runner — the crawl, off Lambda.

What this replaces
------------------
Today every ingest tick is an EventBridge rule -> the resume Lambda ->
`scheduled_jobs.dispatch_scheduled` -> a `job_ingest_dispatch` job that
SELF-INVOKES up to 16 more Lambdas, one per shard. Measured on
`jobpilot-resume-prod` (24h to 2026-09-02): 12,544 invocations and 200,948
billed seconds, of which the 4,377 invocations over 10s carry 91.6% of the
billed time. At 1536 MB that is ~301k GB-s/day, ~9.0M GB-s/month, ~$144/month
— against an August bill where Lambda was already the largest line at $50.46
of $108.53, fully covered by credits that will not last.

Three things this fixes, in order of how much they matter
--------------------------------------------------------
1. **Per-vendor pacing becomes true.** `HostPolicy` is constructed per
   `run_shard` and its own docstring justifies that with "a shard is a single
   Lambda invocation". With 16 shards in 16 Lambdas there are 16 independent
   pacing states, so `HOST_MIN_INTERVAL=0.15` against a shared host like
   boards-api.greenhouse.io is really up to ~107 req/s, and iCIMS — designed
   for `max_concurrency=1` at 2.5s spacing VENDOR-WIDE (ADR-004 §3.2 measured
   405 "Human Verification" on all 60 hostnames at six concurrent) — really
   runs at up to 16 concurrent connections. One process, one HostPolicy per
   run, one egress IP: the politeness the registry already declares is finally
   the politeness we apply. This is a correctness fix, not a cost fix.
2. **The 300s wall is gone**, so `JOB_INGEST_SHARD_BUDGET` can rise past 90s
   and the per-tick shard ceiling past 16. That is what actually converts newly
   discovered boards into postings: career-pages currently carries ~39 members
   per shard against shards sized for ~20, so members are starved by budget,
   not by schedule.
3. Cost, which is the least interesting of the three.

What it deliberately does NOT change
------------------------------------
`run_shard`, `run_discovery_worker` and every lease/watermark/backoff rule are
untouched. This process only replaces the DISPATCH mechanism: both
`job_ingest_worker.dispatch_due_shards` and
`career_portal_discovery.dispatch_due_discovery` already take an `enqueue`
callable, defaulting to "create a Mongo job row and invoke a Lambda". Here it
becomes "submit to a local thread pool". Because at-most-one-worker-per-shard
is enforced by `inv.acquire_lease` in Mongo and NOT by the dispatch path, a
stray EventBridge tick during the migration is harmless — it will find the
shard leased and skip it. That is what makes a source-at-a-time cutover safe.

Operating it
------------
    PYTHONPATH=shared/python python -m services.ingest_runner.runner
    ... --sources icims                  # migrate one source at a time
    ... --once                           # fire every due job once and exit

SIGTERM stops scheduling, drains in-flight shards, and exits. A heartbeat doc
is written every minute: Lambda could not silently stop, a box can, so the
heartbeat is the thing that must be alarmed on.
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import socket
import sys
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("ingest_runner")

HEARTBEAT_COLLECTION = "ingest_runner_heartbeat"
#: Written by `infra/ec2/install-ingest-runner.sh` next to `shared/` and
#: `services/`. Reported in the heartbeat so a deploy can be CONFIRMED from
#: Mongo (the box answers with the SHA it is running) rather than inferred
#: from an installer that exited 0.
REVISION_FILE = Path(__file__).resolve().parents[2] / "REVISION"


def read_revision() -> str:
    try:
        return REVISION_FILE.read_text(encoding="utf-8").strip()[:64] or "unknown"
    except OSError:
        return "unknown"
#: One row per daily job per run: started, finished, ok, error. Read by
#: `scripts/retention_watchdog.py`. It exists because this box ran with
#: maintenance disabled from 2026-09-02 to 2026-09-24 and its coverage job
#: failed every morning for the same three weeks, and nothing anyone looks at
#: said so: `_guard` logs to journald on a box nobody reads.
DAILY_RUNS_COLLECTION = "ingest_runner_daily_runs"
DAILY_RUNS_RETENTION = timedelta(days=90)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default


#: Worker threads running shards concurrently. This is the dial that replaces
#: `JOB_INGEST_SHARDS_PER_TICK`'s role as a Lambda-concurrency ceiling. Shards
#: are I/O bound (HTTP + Mongo), so this can exceed the core count comfortably;
#: it is bounded by memory and by the per-vendor pacing, not by CPU.
WORKERS = _env_int("INGEST_RUNNER_WORKERS", 24)

#: Discovery gets its OWN pool, and this is not a tuning preference.
#:
#: An ingest shard is bounded work that returns in tens of seconds. A discovery
#: worker is the opposite shape: `run_discovery_worker` LOOPS claiming batches
#: until `DISCOVERY_BUDGET_SECONDS` (240) is spent, and one tick dispatches
#: `DISCOVERY_WORKERS_PER_TICK` (16) of them. Sharing one pool would therefore
#: let a single discovery tick occupy every worker for four minutes and starve
#: the ingest shards queued behind it — on Lambda they were separate
#: invocations and could not contend at all, so this contention is created by
#: the move and has to be answered by the move.
#:
#: Sized small on purpose: each worker ALSO fans out `DISCOVERY_CONCURRENCY`
#: (16) threads internally, so 4 here is already up to 64 in-flight requests on
#: a 2-vCPU box.
DISCOVERY_WORKERS = _env_int("INGEST_RUNNER_DISCOVERY_WORKERS", 4)

#: Seconds between scheduler wake-ups. Small enough that a minute-aligned slot
#: is never missed, large enough to be free.
TICK_SECONDS = _env_float("INGEST_RUNNER_TICK", 20.0)

#: How long a SIGTERM waits for in-flight shards before giving up. A shard is
#: bounded by `JOB_INGEST_SHARD_BUDGET`, so this only needs to exceed it.
DRAIN_SECONDS = _env_float("INGEST_RUNNER_DRAIN", 150.0)

HEARTBEAT_SECONDS = _env_float("INGEST_RUNNER_HEARTBEAT", 60.0)


# ---------------------------------------------------------------------------
# Schedule — mirrors infra/terraform/schedulers.tf minute-for-minute
# ---------------------------------------------------------------------------
# Keeping the offsets identical is deliberate: the 7-minute stagger exists so
# four fan-outs never overlap, and reproducing it here means the box behaves
# like the rules it replaces, so a half-migrated system has no new interleaving
# to reason about.
QUARTER_HOURLY: Dict[str, int] = {
    "workday": 0,        # cron(0/15  ...)
    "icims": 4,          # cron(4/15  ...)
    "career-pages": 7,   # cron(7/15  ...)
    "oracle": 11,        # cron(11/15 ...)
}
DISCOVERY_MINUTE = 2     # cron(2/15  ...) — grows the registry, not the crawl
MAINTENANCE_UTC = (5, 20)  # cron(20 5 * * ? *) — daily retention purge
# After maintenance, never overlapping with it. The report takes ~2 minutes and
# is NEVER computed on an API request; this is the scheduled persist path.
COVERAGE_SNAPSHOT_UTC = (5, 45)


class Runner:
    def __init__(self, sources: List[str], *, discovery: bool = True,
                 maintenance: bool = True) -> None:
        self.sources = sources
        self.discovery = discovery
        self.maintenance = maintenance
        self.runner_id = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
        self.stop = threading.Event()
        self.pool = ThreadPoolExecutor(max_workers=WORKERS,
                                       thread_name_prefix="ingest")
        # Separate pool: see DISCOVERY_WORKERS. Created even when discovery is
        # off so shutdown paths stay uniform.
        self.discovery_pool = ThreadPoolExecutor(
            max_workers=max(1, DISCOVERY_WORKERS),
            thread_name_prefix="discovery")
        self._inflight: Dict[Future, str] = {}
        self._lock = threading.Lock()
        self._fired: Dict[str, str] = {}   # job name -> slot already fired
        self.counts: Dict[str, int] = {}

    # -- bookkeeping -------------------------------------------------------
    def _submit(self, label: str, fn: Callable[..., Any], *args,
                pool: Optional[ThreadPoolExecutor] = None, **kwargs) -> str:
        if self.stop.is_set():
            return ""
        target = pool if pool is not None else self.pool
        future = target.submit(self._guard, label, fn, *args, **kwargs)
        with self._lock:
            self._inflight[future] = label
        future.add_done_callback(self._done)
        return label

    def _guard(self, label: str, fn: Callable[..., Any], *args, **kwargs) -> Any:
        """Never let one shard's failure reach the pool.

        `run_shard` already promises not to raise, and `record_failure` already
        writes the backoff. This is the belt to that pair of braces: a thread
        that dies with an exception in a long-lived process is a silent gap in
        coverage, whereas a logged one is a metric.
        """
        started = time.monotonic()
        try:
            result = fn(*args, **kwargs)
            logger.info("%s finished in %.1fs", label, time.monotonic() - started)
            return result
        except Exception:
            logger.exception("%s FAILED after %.1fs", label,
                             time.monotonic() - started)
            return None

    def _done(self, future: Future) -> None:
        with self._lock:
            self._inflight.pop(future, None)

    def inflight(self) -> int:
        with self._lock:
            return len(self._inflight)

    # -- the jobs ----------------------------------------------------------
    def dispatch_source(self, source: str) -> None:
        """One ingest tick for one source, fanned out into the local pool."""
        from services import job_ingest_worker as worker

        limit = _env_int("INGEST_RUNNER_SHARDS_PER_TICK",
                         worker.MAX_SHARDS_PER_TICK)

        def enqueue(job_type: str, payload: Dict[str, Any]) -> str:
            # Replaces create_job + invoke_async. No `resume_jobs` row is
            # written: on Lambda that row was the only way to observe an
            # invocation, but `job_ingestion_runs` already records every shard
            # run with its counts, and one row per shard per tick is pure churn
            # once the work is in-process.
            src = payload.get("source", "")
            shard = payload.get("shard", "")
            return self._submit(f"shard {src}/{shard}",
                                self._run_shard, src, shard) or ""

        try:
            result = worker.dispatch_due_shards(source, limit=limit,
                                                enqueue=enqueue)
        except Exception:
            logger.exception("dispatch failed for %s", source)
            return
        n = len(result.get("enqueued") or [])
        self.counts[source] = self.counts.get(source, 0) + n
        logger.info("tick %s: due=%s enqueued=%d inflight=%d",
                    source, result.get("due"), n, self.inflight())

    def _run_shard(self, source: str, shard: str) -> Any:
        from services import job_ingest_worker as worker

        result = worker.run_shard(source, shard)
        # Requirement extraction stays a SEPARATE unit of work, exactly as the
        # Lambda branch had it, so no Bedrock call ever sits in the listing or
        # detail hot path.
        queue = (result or {}).get("parse_queue") or []
        if queue and worker.REQUIREMENTS_ENABLED:
            items = queue[:worker.REQUIREMENTS_BATCH]
            self._submit(f"requirements {source}/{shard}",
                         worker.extract_requirements, items)
        return result

    def dispatch_discovery(self) -> None:
        from services import career_portal_discovery as disc

        def enqueue(job_type: str, payload: Dict[str, Any]) -> str:
            return self._submit(
                f"discovery w{payload.get('worker')}",
                disc.run_discovery_worker,
                pool=self.discovery_pool,
                batch=payload.get("batch"), budget=payload.get("budget")) or ""

        try:
            result = disc.dispatch_due_discovery(enqueue=enqueue)
        except Exception:
            logger.exception("discovery dispatch failed")
            return
        logger.info("tick discovery: %s inflight=%d", result, self.inflight())

    #: Deletions per maintenance sweep. The 5,000 default exists because
    #: maintenance ran on Lambda under a 300-second wall; this box has no wall.
    #:
    #: 5,000/day could not keep up. Measured 2026-09-03: the crawl closes
    #: 6,134 postings/day on average and 17,639 on its busiest day, so the
    #: backlog grew ~1,100/day and had reached 172,335 rows -- 26 days
    #: overdue at the oldest, and 80% of the whole database sat in
    #: `job_postings` because of it. A retention window that cannot drain is
    #: not a retention window.
    #:
    #: Sized to clear a bad day in one sweep with headroom, not to be
    #: unbounded: the sweep is still bounded, still logged, and still
    #: double-checks each doc is closed before deleting it.
    MAINTENANCE_PURGE_LIMIT = int(os.getenv("INGEST_MAINTENANCE_PURGE_LIMIT", "50000"))
    MAINTENANCE_EXPIRE_LIMIT = int(os.getenv("INGEST_MAINTENANCE_EXPIRE_LIMIT", "50000"))
    #: The 72h unseen-posting backstop (`worker.verify_liveness`). It runs only
    #: when a limit is passed, and this call never passed one: measured
    #: 2026-10-25, 65,290 of 335,137 active postings (19.5%) had not been seen
    #: by the crawl in over 72 hours and were still served - dead Lever links,
    #: "position closed" pages. A closed posting REOPENS the moment the crawl
    #: sees it again (`upsert_posting`), so a board we merely failed to read
    #: comes back on its next good crawl. Bounded per day so a backlog drains
    #: over days instead of in one sweep.
    MAINTENANCE_VERIFY_LIMIT = int(os.getenv("INGEST_MAINTENANCE_VERIFY_LIMIT", "30000"))

    def run_maintenance(self) -> None:
        from services import job_ingest_worker as worker
        self._submit(
            "maintenance",
            self._daily("maintenance", lambda: worker.run_maintenance(
                purge_limit=self.MAINTENANCE_PURGE_LIMIT,
                expire_limit=self.MAINTENANCE_EXPIRE_LIMIT,
                verify_limit=self.MAINTENANCE_VERIFY_LIMIT,
            )),
        )

    def run_coverage_snapshot(self) -> None:
        """Persist the measured coverage snapshot. Failures keep the last good one."""
        self._submit(
            "coverage_snapshot",
            self._daily("coverage_snapshot", lambda: __import__(
                "services.coverage_snapshot_job", fromlist=["run"]
            ).run(persist=True)),
        )

    def _daily(self, name: str, fn: Callable[[], Any]) -> Callable[[], Any]:
        """Wrap a daily job so its outcome lands in MongoDB, not only in journald.

        A row is inserted when the job STARTS and completed when it ends, so the
        three failure shapes stay distinguishable from the database alone:
        raised (`ok: False` + error), returned a failure (`ok: False`, e.g. the
        coverage job, which catches its own exceptions), and never finished
        (`finished_at` missing - killed or hung). Recording is best-effort; it
        must never be the reason a job does not run.
        """
        def run() -> Any:
            rows = None
            row_id = None
            try:
                from utils.db_connect import DBConnect
                rows = DBConnect().get_db()[DAILY_RUNS_COLLECTION]
                now = datetime.now(timezone.utc)
                row_id = rows.insert_one({
                    "job": name, "runner_id": self.runner_id,
                    "started_at": now, "expire_at": now + DAILY_RUNS_RETENTION,
                }).inserted_id
            except Exception as exc:
                logger.warning("daily run record failed for %s: %s",
                               name, type(exc).__name__)

            def finish(fields: Dict[str, Any]) -> None:
                if rows is None or row_id is None:
                    return
                try:
                    rows.update_one({"_id": row_id}, {"$set": {
                        "finished_at": datetime.now(timezone.utc), **fields}})
                except Exception as exc:
                    logger.warning("daily run record failed for %s: %s",
                                   name, type(exc).__name__)

            try:
                result = fn()
            except Exception as exc:
                finish({"ok": False, "error": type(exc).__name__,
                        "message": str(exc)[:500]})
                raise
            ok = not (isinstance(result, dict) and result.get("ok") is False)
            summary = ({k: v for k, v in result.items()
                        if isinstance(v, (bool, int, float, str)) or v is None}
                       if isinstance(result, dict) else {})
            finish({"ok": ok, "error": summary.get("error") if not ok else None,
                    "result": summary})
            return result
        return run

    # -- heartbeat ---------------------------------------------------------
    def heartbeat(self) -> None:
        """The thing to alarm on.

        A Lambda that stops being invoked is visible as a missing metric; a
        systemd unit that wedges looks exactly like a quiet night. This is the
        only signal that separates them, so it is written unconditionally and
        its failure is logged rather than raised.
        """
        try:
            from utils.db_connect import DBConnect
            DBConnect().get_db()[HEARTBEAT_COLLECTION].update_one(
                {"runner_id": self.runner_id},
                {"$set": {
                    "runner_id": self.runner_id,
                    "hostname": socket.gethostname(),
                    "pid": os.getpid(),
                    "sources": self.sources,
                    "discovery": self.discovery,
                    # Recorded because it was NOT: `--no-maintenance` shipped
                    # at cutover and stayed on for three weeks, invisible from
                    # anywhere but the unit file on the box.
                    "maintenance": self.maintenance,
                    "revision": read_revision(),
                    "workers": WORKERS,
                    "inflight": self.inflight(),
                    "dispatched": dict(self.counts),
                    "updated_at": datetime.now(timezone.utc),
                },
                 "$setOnInsert": {"started_at": datetime.now(timezone.utc)}},
                upsert=True)
        except Exception as exc:
            logger.warning("heartbeat write failed: %s", type(exc).__name__)

    # -- scheduler ---------------------------------------------------------
    def _due(self, name: str, slot: str) -> bool:
        """Fire once per slot. `slot` is the identity of this occurrence, so a
        20-second tick inside the same minute cannot double-fire it."""
        if self._fired.get(name) == slot:
            return False
        self._fired[name] = slot
        return True

    def poll(self, now: Optional[datetime] = None) -> None:
        now = now or datetime.now(timezone.utc)
        slot = now.strftime("%Y-%m-%dT%H:%M")
        quarter = now.strftime("%Y-%m-%dT%H:") + str(now.minute // 15)

        for source, minute in QUARTER_HOURLY.items():
            if source not in self.sources:
                continue
            if now.minute % 15 == minute and self._due(f"ingest:{source}", quarter):
                self.dispatch_source(source)

        if self.discovery and now.minute % 15 == DISCOVERY_MINUTE \
                and self._due("discovery", quarter):
            self.dispatch_discovery()

        if self.maintenance and (now.hour, now.minute) == MAINTENANCE_UTC \
                and self._due("maintenance", slot):
            self.run_maintenance()

        if (now.hour, now.minute) == COVERAGE_SNAPSHOT_UTC \
                and self._due("coverage_snapshot", slot):
            self.run_coverage_snapshot()

    def serve(self, once: bool = False) -> int:
        logger.info("ingest runner %s starting: sources=%s discovery=%s "
                    "workers=%d", self.runner_id, self.sources, self.discovery,
                    WORKERS)
        if once:
            for source in self.sources:
                self.dispatch_source(source)
            if self.discovery:
                self.dispatch_discovery()
            self._drain()
            self.heartbeat()
            return 0

        last_beat = 0.0
        while not self.stop.is_set():
            try:
                self.poll()
            except Exception:
                logger.exception("scheduler poll failed")
            if time.monotonic() - last_beat >= HEARTBEAT_SECONDS:
                self.heartbeat()
                last_beat = time.monotonic()
            self.stop.wait(TICK_SECONDS)

        logger.info("SIGTERM: draining %d in-flight", self.inflight())
        self._drain()
        self.heartbeat()
        logger.info("ingest runner %s stopped", self.runner_id)
        return 0

    def _drain(self) -> None:
        deadline = time.monotonic() + DRAIN_SECONDS
        while self.inflight() and time.monotonic() < deadline:
            time.sleep(1.0)
        self.discovery_pool.shutdown(wait=False)
        remaining = self.inflight()
        if remaining:
            # Leases expire on their own (`acquire_lease` TTL), so an abandoned
            # shard is stealable by the next tick with no reaper. Say so rather
            # than implying data loss.
            logger.warning("%d shard(s) still running at drain deadline; their "
                           "leases will expire and be re-claimed", remaining)
        self.pool.shutdown(wait=False)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sources", default="",
                    help="comma-separated subset (default: all). Migrate one "
                         "source at a time by naming it here and disabling only "
                         "that EventBridge rule.")
    ap.add_argument("--no-discovery", action="store_true")
    ap.add_argument("--no-maintenance", action="store_true")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"))
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout)

    from services.job_ingest_worker import SOURCES
    if args.sources.strip():
        wanted = [s.strip() for s in args.sources.split(",") if s.strip()]
        unknown = [s for s in wanted if s not in SOURCES]
        if unknown:
            logger.error("unknown source(s): %s (known: %s)", unknown, list(SOURCES))
            return 2
        sources = wanted
    else:
        sources = list(SOURCES)

    runner = Runner(sources, discovery=not args.no_discovery,
                    maintenance=not args.no_maintenance)

    def _term(signum, _frame):
        logger.info("signal %s received", signum)
        runner.stop.set()

    signal.signal(signal.SIGTERM, _term)
    signal.signal(signal.SIGINT, _term)
    return runner.serve(once=args.once)


if __name__ == "__main__":
    raise SystemExit(main())
