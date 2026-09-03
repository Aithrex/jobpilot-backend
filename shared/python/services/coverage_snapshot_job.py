"""Scheduled generation of the authoritative coverage snapshot.

Never compute the full report inside an API request. This job acquires a
lease, runs `employer_coverage_report.build`, and persists only a complete
payload. A failed run leaves the previous snapshot in place.
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parents[3]
_SCRIPTS = _ROOT / "scripts"
for _p in (str(_ROOT), str(_SCRIPTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

LEASES = "coverage_snapshot_leases"
RUNS = "coverage_snapshot_runs"
LEASE_NAME = "coverage_snapshot"
LEASE_SECONDS = int(os.getenv("COVERAGE_SNAPSHOT_LEASE_SECONDS", "900") or 900)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _db():
    from utils.db_connect import DBConnect
    return DBConnect().get_db()


def acquire_lease(*, db=None, now: Optional[datetime] = None) -> bool:
    db = db if db is not None else _db()
    now = now or _now()
    expiry = now + timedelta(seconds=LEASE_SECONDS)
    rec = db[LEASES].find_one({"_id": LEASE_NAME})
    if rec and rec.get("expires_at"):
        exp = rec["expires_at"]
        if isinstance(exp, datetime):
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            if exp > now:
                return False
    db[LEASES].update_one(
        {"_id": LEASE_NAME},
        {"$set": {"expires_at": expiry, "holder": os.getpid(), "started_at": now}},
        upsert=True)
    return True


def release_lease(*, db=None) -> None:
    db = db if db is not None else _db()
    db[LEASES].delete_one({"_id": LEASE_NAME})


def run(*, persist: bool = True, db=None) -> Dict[str, Any]:
    """Build the report. `persist=False` is the dry path used by tests."""
    import time
    db = db if db is not None else _db()
    started = time.perf_counter()
    now = _now()
    if not acquire_lease(db=db, now=now):
        result = {"status": "skipped_overlap", "ok": True}
        db[RUNS].insert_one({"at": now, **result})
        return result
    try:
        import employer_coverage_report as report
        from services import coverage_snapshot as snap

        built = report.build(db, mix_days=30)
        summary = report.report(db, built, mix_days=30)
        payload = report.snapshot_payload(summary)
        stored = None
        if persist:
            snap.ensure_indexes(db=db)
            stored = snap.save(payload, db=db)
            if stored:
                snap.prune(db=db)
        duration_ms = round((time.perf_counter() - started) * 1000, 1)
        result = {
            "status": "ok" if (stored or not persist) else "incomplete",
            "ok": bool(stored or not persist),
            "persisted": bool(stored),
            "duration_ms": duration_ms,
            "measured_at": payload.get("measured_at"),
        }
        db[RUNS].insert_one({"at": _now(), **result})
        return result
    except Exception as exc:
        duration_ms = round((time.perf_counter() - started) * 1000, 1)
        result = {"status": "failed", "ok": False, "error": type(exc).__name__,
                  "duration_ms": duration_ms, "persisted": False}
        logger.exception("coverage snapshot generation failed")
        try:
            db[RUNS].insert_one({"at": _now(), **result})
        except Exception:
            pass
        return result
    finally:
        release_lease(db=db)


def latest_run(*, db=None) -> Optional[Dict[str, Any]]:
    db = db if db is not None else _db()
    doc = db[RUNS].find_one({}, {"_id": 0}, sort=[("at", -1)])
    return doc
