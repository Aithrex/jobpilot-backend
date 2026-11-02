"""The For-you board, built ahead of time and kept warm.

Opening the board used to start three scans and wait for them: the two
inventory tabs score up to ~8,000 postings each, and the MNC tab crawls about
seventy employers live. On Lambda each scan had a function to itself. On the
Oracle box they share one API process, so every open, by every user, paid for
all three at once and the page waited on the slowest.

Now the board is a stored answer:

* `board_requests` holds, per user, the exact request their board last made
  (titles, scope, timezone). The board records it on every open.
* `board_snapshots` holds one built tab per (user, tab), stamped with the
  request's signature and the time it was built.
* The scheduler runs `refresh_due` every ten minutes, on the worker, and
  rebuilds each recently active user's board once it is `REFRESH_MINUTES` old,
  one user at a time, through the SAME `run_board_source` a live scan runs.
  A live scan's result is saved here too, so the two never disagree.
* Opening the board reads the snapshot (`load`). A FRESH one is the answer and
  no scan starts. A stale one is shown at once while a scan replaces it.

Fresh means: built from this exact request, within `FRESH_SECONDS`, and on the
viewer's current calendar day. The last rule exists because a card's "Today"
is the viewer's calendar day; a board built at 23:50 must not call yesterday's
postings today.

Both collections are additive. Dropping them returns the board to scanning on
every open, and nothing else reads them.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

REQUESTS = "board_requests"
SNAPSHOTS = "board_snapshots"

#: Board key (what the frontend calls a source) -> `_process_job` job type.
SOURCES: Dict[str, str] = {
    "workday": "workday_jobs",
    "careers": "career_pages",
    "mncs": "mnc_jobs",
}
_KEY_OF_JOB = {job: key for key, job in SOURCES.items()}

#: The value the frontend puts in a scan's `board` field for this surface.
BOARD_FOR_YOU = "for_you"


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, "") or default))
    except (TypeError, ValueError):
        return default


REFRESH_MINUTES = _env_int("BOARD_REFRESH_MINUTES", 30)
#: A snapshot is served as the answer up to this age. One scheduler tick (10
#: minutes) past the refresh period, so a board that is due but not yet rebuilt
#: is still served rather than re-scanned by every tab that opens meanwhile.
FRESH_SECONDS = (REFRESH_MINUTES + 10) * 60
#: Older than this, a snapshot is not shown at all.
MAX_SERVE_SECONDS = _env_int("BOARD_MAX_SERVE_HOURS", 24) * 3600
#: Users whose board opened within this many days are kept warm.
ACTIVE_DAYS = _env_int("BOARD_ACTIVE_DAYS", 14)
#: Rows expire on their own (a TTL index on `expire_at`).
KEEP_DAYS = _env_int("BOARD_KEEP_DAYS", 3)

#: The request fields that decide what a board contains. Everything else in a
#: scan payload (force_refresh, the email) is not part of the answer.
_PARAM_KEYS = ("titles", "industries", "companies", "location", "remote_only",
               "us_only", "min_salary", "include_unknown_salary",
               "tz_offset_minutes")


def _db():
    from utils.db_connect import DBConnect
    return DBConnect().get_db()


_INDEXED = False


def _ensure_indexes(db) -> None:
    global _INDEXED
    if _INDEXED:
        return
    try:
        db[SNAPSHOTS].create_index("expire_at", expireAfterSeconds=0,
                                   name="ttl_expire_at")
        db[SNAPSHOTS].create_index("user_email", name="user_email")
        db[REQUESTS].create_index("requested_at", name="requested_at")
    except Exception as exc:  # pragma: no cover - an index is an optimisation
        logger.warning("board_snapshot: index ensure failed: %s", type(exc).__name__)
    _INDEXED = True


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: Any) -> Optional[datetime]:
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _tz_offset(params: Dict[str, Any]) -> int:
    from services.workday_jobs_service import _clamp_tz_offset
    return _clamp_tz_offset(params.get("tz_offset_minutes"))


def params_of(payload: Dict[str, Any]) -> Dict[str, Any]:
    """The answer-deciding part of a scan payload, normalised."""
    out: Dict[str, Any] = {}
    for key in _PARAM_KEYS:
        value = payload.get(key)
        if key in ("titles", "industries", "companies"):
            value = [str(v).strip() for v in (value or []) if str(v or "").strip()]
        elif key == "location":
            value = str(value or "").strip()
        elif key in ("remote_only",):
            value = bool(value)
        elif key == "us_only":
            value = True if value is None else bool(value)
        elif key == "tz_offset_minutes":
            value = _tz_offset(payload)
        elif key == "min_salary":
            # 0, None and "" all mean "no floor" to every engine.
            try:
                value = float(value) if value else None
            except (TypeError, ValueError):
                value = None
            value = value or None
        elif key == "include_unknown_salary":
            # Unset means keep unpriced rows (`inventory_feed.build_feed`).
            value = True if value is None else bool(value)
        out[key] = value
    return out


def signature(params: Dict[str, Any]) -> str:
    """Stable id of a request. Title ORDER is kept: the crawl searches the
    first few titles, so a reordered list is a different request."""
    canon = dict(params)
    for key in ("titles", "industries", "companies"):
        canon[key] = [str(v).strip().casefold() for v in (canon.get(key) or [])]
    canon["location"] = str(canon.get("location") or "").casefold()
    blob = json.dumps(canon, sort_keys=True, default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:20]


def _local_day(at: datetime, tz_offset_minutes: int):
    return (at + timedelta(minutes=tz_offset_minutes)).date()


def is_board_request(payload: Dict[str, Any]) -> bool:
    return str(payload.get("board") or "") == BOARD_FOR_YOU and bool(payload.get("user_email"))


# --------------------------------------------------------------------------
# Writes
# --------------------------------------------------------------------------

def record_request(payload: Dict[str, Any]) -> Optional[str]:
    """Remember the request a user's board just made. Returns its signature.

    Called on every open, so `requested_at` is also "last seen on the board",
    which is what decides who is kept warm.
    """
    email = str(payload.get("user_email") or "")
    if not email:
        return None
    params = params_of(payload)
    sig = signature(params)
    db = _db()
    _ensure_indexes(db)
    now = _now()
    db[REQUESTS].update_one(
        {"_id": email},
        {"$set": {"params": params, "sig": sig, "requested_at": now},
         "$setOnInsert": {"created_at": now}},
        upsert=True,
    )
    return sig


def save(email: str, key: str, sig: str, result: Dict[str, Any], *,
         built_by: str, seconds: Optional[float] = None) -> None:
    """Store one built tab. Sized like a poll response, so a stored board can
    always be served."""
    if key not in SOURCES or not isinstance(result, dict):
        return
    from services.resume_service import fit_feed_result
    db = _db()
    _ensure_indexes(db)
    now = _now()
    db[SNAPSHOTS].update_one(
        {"_id": f"{email}|{key}"},
        {"$set": {
            "user_email": email, "source": key, "sig": sig,
            "result": fit_feed_result(result),
            "built_at": now, "built_by": built_by,
            "build_seconds": round(seconds, 2) if seconds is not None else None,
            "expire_at": now + timedelta(days=KEEP_DAYS),
        }},
        upsert=True,
    )


def save_from_scan(payload: Dict[str, Any], job_type: str, result: Any) -> bool:
    """A live For-you scan just finished: keep its result as the snapshot."""
    key = _KEY_OF_JOB.get(job_type)
    if not key or not isinstance(result, dict) or not is_board_request(payload):
        return False
    if result.get("ok") is False:
        return False
    sig = signature(params_of(payload))
    save(str(payload["user_email"]), key, sig, result, built_by="scan")
    return True


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------

def load(payload: Dict[str, Any], *, now: Optional[datetime] = None) -> Dict[str, Any]:
    """The stored board for this exact request, with its age.

    `fresh` is True only when ALL tabs are present, built from this request,
    within `FRESH_SECONDS`, and on the viewer's current calendar day. A tab
    built from another request is never returned.
    """
    now = now or _now()
    email = str(payload.get("user_email") or "")
    params = params_of(payload)
    sig = signature(params)
    tz = int(params["tz_offset_minutes"])
    out: Dict[str, Any] = {
        "available": False, "fresh": False, "built_at": None, "age_seconds": None,
        "refresh_minutes": REFRESH_MINUTES, "sources": {k: None for k in SOURCES},
    }
    if not email:
        return out
    oldest: Optional[datetime] = None
    all_fresh = True
    for doc in _db()[SNAPSHOTS].find({"user_email": email, "sig": sig}):
        key = doc.get("source")
        built = _aware(doc.get("built_at"))
        if key not in SOURCES or built is None:
            continue
        age = (now - built).total_seconds()
        if age > MAX_SERVE_SECONDS:
            continue
        out["sources"][key] = doc.get("result")
        oldest = built if oldest is None or built < oldest else oldest
        if age > FRESH_SECONDS or _local_day(built, tz) != _local_day(now, tz):
            all_fresh = False
    present = [k for k, v in out["sources"].items() if v is not None]
    if not present:
        return out
    out["available"] = True
    out["fresh"] = all_fresh and len(present) == len(SOURCES)
    out["built_at"] = oldest.isoformat() if oldest else None
    out["age_seconds"] = int((now - oldest).total_seconds()) if oldest else None
    return out


# --------------------------------------------------------------------------
# The scheduled refresh
# --------------------------------------------------------------------------

def _is_due(email: str, sig: str, tz: int, now: datetime, db) -> bool:
    built: List[datetime] = []
    for doc in db[SNAPSHOTS].find({"user_email": email, "sig": sig},
                                  {"source": 1, "built_at": 1}):
        at = _aware(doc.get("built_at"))
        if doc.get("source") in SOURCES and at is not None:
            built.append(at)
    if len(built) < len(SOURCES):
        return True
    oldest = min(built)
    # A few minutes early, so a board rebuilt on one tick is not a hair short
    # of due on the next and left to age past FRESH_SECONDS.
    if (now - oldest).total_seconds() >= REFRESH_MINUTES * 60 - 300:
        return True
    return _local_day(oldest, tz) != _local_day(now, tz)


def build_user(email: str, params: Dict[str, Any], sig: str) -> Dict[str, Any]:
    """Rebuild every tab of one user's board, one tab at a time."""
    from services.resume_service import run_board_source

    payload = dict(params, user_email=email, board=BOARD_FOR_YOU, force_refresh=False)
    report: Dict[str, Any] = {}
    for key, job_type in SOURCES.items():
        started = time.monotonic()
        try:
            result = run_board_source(job_type, dict(payload))
        except Exception as exc:
            logger.warning("board refresh: %s for %s failed: %s",
                           key, email[:3] + "***", type(exc).__name__)
            report[key] = "failed"
            continue
        took = time.monotonic() - started
        if isinstance(result, dict) and result.get("ok") is not False:
            save(email, key, sig, result, built_by="refresh", seconds=took)
            report[key] = len(result.get("jobs") or [])
        else:
            report[key] = "empty"
    return report


def refresh_due(*, budget_seconds: float = 540.0,
                now: Optional[datetime] = None) -> Dict[str, Any]:
    """Rebuild the boards that are due, oldest request first, within budget.

    Users are processed one at a time so the refresh never holds more than one
    board in memory. `behind` counts the due boards the budget did not reach;
    if it stays above zero the cadence is too tight for the box.
    """
    now = now or _now()
    started = time.monotonic()
    db = _db()
    _ensure_indexes(db)
    cutoff = now - timedelta(days=ACTIVE_DAYS)
    due: List[Dict[str, Any]] = []
    for req in db[REQUESTS].find({"requested_at": {"$gte": cutoff}}).sort("requested_at", -1):
        params = req.get("params") or {}
        sig = req.get("sig") or signature(params)
        if not params.get("titles"):
            continue
        if _is_due(req["_id"], sig, int(params.get("tz_offset_minutes") or 0), now, db):
            due.append({"email": req["_id"], "params": params, "sig": sig})
    built = 0
    for item in due:
        # At least one board per tick, so a slow box still makes progress.
        if built and time.monotonic() - started > budget_seconds:
            break
        build_user(item["email"], item["params"], item["sig"])
        built += 1
    summary = {"due": len(due), "built": built, "behind": len(due) - built,
               "seconds": round(time.monotonic() - started, 1)}
    logger.info("board refresh: %s", summary)
    return summary
