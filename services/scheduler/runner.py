"""Scheduled jobs off EventBridge: the same names, on the same clock.

Each minute, any job whose latest slot is unclaimed gets claimed in Mongo and
handed to services.scheduled_jobs.dispatch_scheduled. The claim is what makes a
deploy safe: a slot missed while the worker restarts is picked up by the new
worker (inside CATCH_UP_SECONDS), and two workers can never both run one slot.

Jobs it enqueues run on the bounded pool in THIS process, so a stop waits for
them (ThreadPoolExecutor joins at interpreter exit).

ingest_maintenance is not here: the ingest runner owns it on this box.
"""
import logging
import os
import signal
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

backend_dir = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(backend_dir / 'shared' / 'python'))
for svc in ['auth', 'jobs', 'resume', 'billing', 'learning']:
    sys.path.insert(0, str(backend_dir / 'services' / svc))

logging.basicConfig(
    level=getattr(logging, os.getenv('LOG_LEVEL', 'INFO')),
    format='%(asctime)s:%(levelname)s:%(name)s:%(message)s',
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

CATCH_UP_SECONDS = 600

# name -> (minutes between runs) or (hour, minute) daily UTC.
EVERY = {"digest_hourly": 60, "learning_due": 10}
DAILY = {
    "bandit_nightly": (3, 0),
    "trend_aggregate": (4, 0),
    "ats_signal_aggregate": (4, 30),
    "guest_retention": (5, 45),
    "events_refresh": (6, 0),
}


def latest_slot(name: str, now: datetime) -> datetime:
    """The most recent time `name` was due, at or before `now`."""
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if name in EVERY:
        step = EVERY[name] * 60
        elapsed = int((now - midnight).total_seconds())
        return midnight + timedelta(seconds=elapsed - elapsed % step)
    hour, minute = DAILY[name]
    slot = midnight.replace(hour=hour, minute=minute)
    return slot if slot <= now else slot - timedelta(days=1)


def claim(coll, name: str, slot: datetime) -> bool:
    """True for exactly one caller per (name, slot)."""
    from pymongo.errors import DuplicateKeyError
    try:
        result = coll.update_one(
            {"_id": name, "last_slot": {"$lt": slot}},
            {"$set": {"last_slot": slot, "claimed_at": datetime.now(timezone.utc)}},
            upsert=True,
        )
    except DuplicateKeyError:
        return False
    return bool(result.modified_count or result.upserted_id)


def _run(name: str, busy: set) -> None:
    from services.scheduled_jobs import dispatch_scheduled
    try:
        logger.info("scheduled_job %s -> %s", name, dispatch_scheduled(name))
    finally:
        busy.discard(name)


def tick(coll, now: datetime, busy: set) -> None:
    for name in [*EVERY, *DAILY]:
        slot = latest_slot(name, now)
        if name in busy or (now - slot).total_seconds() > CATCH_UP_SECONDS:
            continue
        if claim(coll, name, slot):
            busy.add(name)
            threading.Thread(target=_run, args=(name, busy), name=f"sched-{name}").start()


def main() -> None:
    from utils.db_connect import DBConnect
    coll = DBConnect().get_collection("scheduler_slots")
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    busy: set = set()
    logger.info("Scheduler started")
    while not stop.is_set():
        try:
            tick(coll, datetime.now(timezone.utc), busy)
        except Exception:
            logger.exception("Scheduler tick failed")
        stop.wait(20)
    logger.info("Scheduler stopping; waiting for running jobs")


if __name__ == '__main__':
    main()
