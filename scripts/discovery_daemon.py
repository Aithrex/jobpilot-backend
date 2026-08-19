#!/usr/bin/env python3
"""discovery_daemon.py — drain the career-portal discovery queue, locally.

Why this exists alongside the scheduled Lambda fleet
----------------------------------------------------
`discover_companies` (EventBridge, every 15 min) is the RIGHT shape for steady
state: it runs forever, costs nothing when the queue is empty, and needs no
one's laptop to be awake. It is the wrong shape for a BACKFILL, and the backfill
is what stands between ~80,000 harvested companies and knowing which of them
have a careers page.

Two ceilings make the fleet slow at that job, and neither is a tuning mistake:

  * a Lambda dies at 300s, so a worker must stop and be re-provisioned every few
    minutes even though its next batch is already claimable; and
  * a 15-minute tick means the fleet is idle roughly three quarters of the time
    — the work finishes in ~200s and then nothing happens for 11 minutes.

A local process has neither. It holds one warm connection pool, runs as wide as
the network allows, and stops when the queue is actually empty.

    PYTHONPATH=shared/python python3 scripts/discovery_daemon.py
    PYTHONPATH=shared/python python3 scripts/discovery_daemon.py --concurrency 64
    PYTHONPATH=shared/python python3 scripts/discovery_daemon.py --max-hours 8

Safe to stop and restart at any point. Work is claimed under the same
`discovery_queue` lease the Lambda workers use, so a killed run loses at most
one in-flight batch, and nothing is ever probed twice concurrently. It is also
safe to run WHILE the scheduled fleet runs — they compete for leases rather than
duplicating work — though the queue drains fastest if you pick one.

Politeness is unchanged and not negotiable here: this reaches ~80,000 employers'
own websites, one request each for the site and its careers page, with slug
probing bounded per company (`SLUG_PROBE_BUDGET`) precisely so a burst of
guesses cannot land on a handful of shared board APIs. Raising `--concurrency`
spreads load across MORE distinct hosts; it does not hit any one host harder.
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

sys.path.insert(0, "shared/python")

logger = logging.getLogger("discovery_daemon")

_STOP = False


def _install_signal_handlers() -> None:
    """Finish the batch in flight, then exit. Never kill mid-company.

    A hard kill drops the leases held by the running batch, so those companies
    sit unprobed until the lease lapses. Draining cleanly costs a few seconds.
    """
    def _handler(signum, _frame):
        global _STOP
        if _STOP:
            logger.warning("second signal — exiting now")
            raise SystemExit(130)
        _STOP = True
        logger.warning("signal %s received — finishing the current batch, "
                       "then stopping (press again to force)", signum)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handler)
        except Exception:  # pragma: no cover - not all platforms
            pass


def _counts() -> Dict[str, int]:
    from collections import Counter
    from utils.db_connect import DBConnect

    db = DBConnect().get_db()
    status = Counter(
        row.get("discovery_status")
        for row in db["companies"].find({}, {"_id": 0, "discovery_status": 1}))
    endpoints = db["company_ats_endpoints"]
    return {
        "pending": status.get("pending", 0),
        "done": status.get("done", 0),
        "no_portal": status.get("no_portal", 0),
        "endpoints": endpoints.estimated_document_count(),
        "validated": endpoints.count_documents({"status": "validated"}),
    }


def _fmt_eta(seconds: float) -> str:
    if seconds <= 0 or seconds != seconds or seconds == float("inf"):
        return "unknown"
    hours, rem = divmod(int(seconds), 3600)
    return f"{hours}h{rem // 60:02d}m"


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--concurrency", type=int, default=0,
                    help="companies fingerprinted at once (default: the "
                         "DISCOVERY_CONCURRENCY setting)")
    ap.add_argument("--batch", type=int, default=0,
                    help="companies claimed per batch (default: "
                         "DISCOVERY_BATCH_SIZE)")
    ap.add_argument("--max-hours", type=float, default=0.0,
                    help="stop after this long (default: run until drained)")
    ap.add_argument("--refill", type=int, default=5000,
                    help="companies to move into the queue when it runs dry")
    ap.add_argument("--rate", type=float, default=0.0,
                    help="outbound requests/second across ALL hosts "
                         "(default: DISCOVERY_GLOBAL_RATE). This, not "
                         "--concurrency, is what keeps CDNs from 429-ing us")
    ap.add_argument("--no-adapt", action="store_true",
                    help="disable automatic rate backoff on 429 bursts")
    ap.add_argument("--report-every", type=float, default=120.0,
                    help="seconds between progress lines")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S")
    for noisy in ("pymongo", "urllib3", "services.ats_providers.http"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    from services import career_portal_discovery as disc
    from services import company_registry_service as reg

    if args.concurrency:
        disc.DISCOVERY_CONCURRENCY = max(1, args.concurrency)
    if args.rate:
        disc.DISCOVERY_GLOBAL_RATE = max(0.5, args.rate)
    batch = max(1, args.batch or disc.DISCOVERY_BATCH_SIZE)
    floor_rate = max(1.0, disc.DISCOVERY_GLOBAL_RATE / 8.0)
    ceil_rate = disc.DISCOVERY_GLOBAL_RATE

    _install_signal_handlers()
    started = time.monotonic()
    hard_deadline = (started + args.max_hours * 3600) if args.max_hours else None

    opening = _counts()
    start_pending = opening["pending"]
    logger.info("start: pending=%(pending)s done=%(done)s "
                "no_portal=%(no_portal)s endpoints=%(endpoints)s",
                opening)
    logger.info("concurrency=%d batch=%d rate=%.1f/s slug_probe_budget=%d",
                disc.DISCOVERY_CONCURRENCY, batch,
                disc.DISCOVERY_GLOBAL_RATE, disc.SLUG_PROBE_BUDGET)

    processed = 0
    endpoints_found = 0
    validated_found = 0
    batches = 0
    last_report = time.monotonic()

    while not _STOP:
        if hard_deadline and time.monotonic() >= hard_deadline:
            logger.info("--max-hours reached")
            break

        # One worker call = one batch here, so the daemon keeps control between
        # batches: that is what makes Ctrl-C prompt and the progress line honest.
        result = disc.run_discovery_worker(batch=batch, budget=3600.0,
                                           owner=f"daemon-{os.getpid()}")
        stats = result.get("stats") or {}
        claimed = int(result.get("claimed") or 0)

        if result.get("error"):
            logger.warning("batch error: %s — backing off 30s",
                           result["error"])
            time.sleep(30)
            continue

        if not claimed:
            # Queue empty: either genuinely drained, or just needs a refill.
            moved = 0
            try:
                moved = reg.refill_queue(limit=max(1, args.refill))
            except Exception as exc:
                logger.warning("refill failed (%s)", type(exc).__name__)
            if not moved:
                logger.info("queue drained and nothing left to refill — done")
                break
            logger.info("refilled queue with %d companies", moved)
            continue

        batches += 1
        did = int(stats.get("companies") or 0)
        errs = int(stats.get("errors") or 0)
        processed += did
        endpoints_found += int(stats.get("endpoints") or 0)
        validated_found += int(stats.get("validated") or 0)

        # Self-tune, because the right rate is a property of the network and the
        # CDNs in front of these employers, not something to guess once. A batch
        # that mostly 429s is the signal that the shared bucket is still too
        # fast; a clean batch earns some of the rate back.
        if not args.no_adapt and did:
            fail_ratio = errs / did
            if fail_ratio > 0.25 and disc.DISCOVERY_GLOBAL_RATE > floor_rate:
                disc.DISCOVERY_GLOBAL_RATE = max(
                    floor_rate, disc.DISCOVERY_GLOBAL_RATE / 2.0)
                logger.warning(
                    "%.0f%% of the batch was rate-limited — dropping to "
                    "%.1f req/s", fail_ratio * 100, disc.DISCOVERY_GLOBAL_RATE)
            elif fail_ratio < 0.05 and disc.DISCOVERY_GLOBAL_RATE < ceil_rate:
                disc.DISCOVERY_GLOBAL_RATE = min(
                    ceil_rate, disc.DISCOVERY_GLOBAL_RATE * 1.25)
                logger.info("clean batch — easing back up to %.1f req/s",
                            disc.DISCOVERY_GLOBAL_RATE)

        now = time.monotonic()
        if now - last_report >= args.report_every:
            last_report = now
            elapsed = now - started
            rate = processed / elapsed if elapsed else 0.0
            live = _counts()
            eta = live["pending"] / rate if rate > 0 else float("inf")
            logger.info(
                "%s | probed %s this run (%.1f/min) | pending %s | "
                "endpoints %s (+%s this run) | ETA %s",
                datetime.now(timezone.utc).strftime("%H:%M:%S"),
                f"{processed:,}", rate * 60, f"{live['pending']:,}",
                f"{live['endpoints']:,}", f"{endpoints_found:,}",
                _fmt_eta(eta))

    elapsed = time.monotonic() - started
    closing = _counts()
    rate = processed / elapsed if elapsed else 0.0
    logger.info("-" * 72)
    logger.info("stopped after %s", _fmt_eta(elapsed))
    logger.info("probed %s companies in %d batches (%.1f/min)",
                f"{processed:,}", batches, rate * 60)
    logger.info("endpoints %s -> %s (validated %s -> %s)",
                f"{opening['endpoints']:,}", f"{closing['endpoints']:,}",
                f"{opening['validated']:,}", f"{closing['validated']:,}")
    logger.info("pending %s -> %s", f"{start_pending:,}",
                f"{closing['pending']:,}")
    # Non-zero only when something is actually left to do, so a wrapper script
    # can loop on this without treating a clean drain as a failure.
    return 0 if closing["pending"] == 0 or _STOP else 1


if __name__ == "__main__":
    raise SystemExit(main())
