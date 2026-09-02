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
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("ingest_runner")

HEARTBEAT_COLLECTION = "ingest_runner_heartbeat"


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
        self._inflight: Dict[Future, str] = {}
        self._lock = threading.Lock()
        self._fired: Dict[str, str] = {}   # job name -> slot already fired
        self.counts: Dict[str, int] = {}

    # -- bookkeeping -------------------------------------------------------
    def _submit(self, label: str, fn: Callable[..., Any], *args, **kwargs) -> str:
        if self.stop.is_set():
            return ""
        future = self.pool.submit(self._guard, label, fn, *args, **kwargs)
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
                batch=payload.get("batch"), budget=payload.get("budget")) or ""

        try:
            result = disc.dispatch_due_discovery(enqueue=enqueue)
        except Exception:
            logger.exception("discovery dispatch failed")
            return
        logger.info("tick discovery: %s inflight=%d", result, self.inflight())

    def run_maintenance(self) -> None:
        from services import job_ingest_worker as worker
        self._submit("maintenance", worker.run_maintenance)

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
                    "workers": WORKERS,
                    "inflight": self.inflight(),
                    "dispatched": dict(self.counts),
                    "updated_at": datetime.now(timezone.utc),
                }},
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
