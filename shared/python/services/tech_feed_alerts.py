"""Daily job alerts from saved tech-feed searches.

Saved searches stay the source of truth. Alerts are an optional field on the
same row. Matching and email are never done in a request handler.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DELIVERIES = "tech_feed_alert_deliveries"
FAILURES = "tech_feed_alert_failures"
METRICS = "tech_feed_alert_metrics"
ALERT_VERSION = "tech-feed-alert-1.0.0"
EVENT = "saved_search_digest"
MAX_JOBS_PER_DIGEST = 15


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _db():
    from utils.db_connect import DBConnect
    return DBConnect().get_db()


def ensure_indexes(*, db=None) -> List[str]:
    db = db if db is not None else _db()
    created: List[str] = []
    try:
        created.append(db[DELIVERIES].create_index(
            [("idempotency_key", 1)], unique=True,
            name="alert_delivery_idempotency"))
        created.append(db[DELIVERIES].create_index(
            [("user_email", 1), ("search_id", 1), ("delivered_at", -1)],
            name="alert_delivery_user_search"))
        created.append(db[FAILURES].create_index(
            [("at", -1)], name="alert_failure_recent"))
    except Exception as exc:  # pragma: no cover
        logger.warning("tech_feed_alerts.ensure_indexes: %s", exc)
    return [c for c in created if c]


def normalize_alerts(payload: Any) -> Dict[str, Any]:
    raw = payload if isinstance(payload, dict) else {}
    enabled = bool(raw.get("enabled"))
    tz = str(raw.get("timezone") or raw.get("tz") or "UTC").strip()[:64] or "UTC"
    paused = str(raw.get("paused_reason") or "").strip()[:200]
    return {
        "enabled": enabled,
        "frequency": "daily",
        "timezone": tz,
        "last_evaluated_at": raw.get("last_evaluated_at"),
        "last_delivered_at": raw.get("last_delivered_at"),
        "paused_reason": paused or None,
    }


def idempotency_key(user_email: str, search_id: str, job_id: str) -> str:
    blob = f"{user_email}|{search_id}|{job_id}".encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:32]


def _claim_delivery(user_email: str, search_id: str, job_id: str, *,
                    db=None) -> bool:
    """Insert-once. False means this job was already notified for this search."""
    db = db if db is not None else _db()
    key = idempotency_key(user_email, search_id, job_id)
    col = db[DELIVERIES]
    if col.find_one({"idempotency_key": key}, {"_id": 1}):
        return False
    try:
        col.insert_one({
            "idempotency_key": key,
            "user_email": user_email,
            "search_id": search_id,
            "job_id": job_id,
            "delivered_at": _now(),
            "alert_version": ALERT_VERSION,
        })
        return True
    except Exception:
        return False


def _eligible_job(row: Dict[str, Any]) -> bool:
    if str(row.get("status") or "active") != "active":
        return False
    if (row.get("fit") or {}).get("eligibility") == "ineligible":
        return False
    return True


def evaluate_search(row: Dict[str, Any], *, now: Optional[datetime] = None,
                    db=None) -> Dict[str, Any]:
    """Match jobs newer than the watermark. Returns a digest payload or skip."""
    from services import tech_feed_saved_searches as tfss
    from services import tech_feed_service as tfs

    db = db if db is not None else _db()
    now = now or _now()
    email = str(row.get("user_email") or "")
    search_id = str(row.get("client_id") or "")
    alerts = normalize_alerts(row.get("alerts"))
    stats = {"delivered": 0, "skipped": 0, "suppressed": 0, "failed": 0}

    live = db[tfss.SAVED_SEARCHES].find_one(
        {"user_email": email, "client_id": search_id}, {"_id": 1})
    if not live:
        stats["skipped"] += 1
        return {"status": "deleted", **stats}
    if not alerts.get("enabled") or alerts.get("paused_reason"):
        stats["skipped"] += 1
        return {"status": "paused", **stats}

    try:
        from services.notifications import service as notify
        prefs = notify.get_prefs(email)
        if prefs.get("unsubscribed"):
            stats["suppressed"] += 1
            return {"status": "unsubscribed", **stats}
        events = prefs.get("events") or {}
        if events.get(EVENT) is False:
            stats["suppressed"] += 1
            return {"status": "event_disabled", **stats}
        if (prefs.get("channels") or {}).get("email", {}).get("enabled") is False:
            stats["suppressed"] += 1
            return {"status": "email_disabled", **stats}
    except Exception as exc:
        logger.warning("alert prefs read failed (%s)", type(exc).__name__)

    watermark = alerts.get("last_evaluated_at")
    if isinstance(watermark, str):
        try:
            watermark = datetime.fromisoformat(watermark.replace("Z", "+00:00"))
        except ValueError:
            watermark = None
    filters = dict(row.get("filters") or {})
    filters["user_email"] = email
    filters["page_size"] = MAX_JOBS_PER_DIGEST
    filters["personalize"] = True
    if isinstance(watermark, datetime):
        age = max(1, int((now - watermark).total_seconds() // 86400) + 1)
        filters["posted_within_days"] = min(age, 14)
    try:
        result = tfs.search(filters)
    except Exception as exc:
        _record_failure(email, search_id, str(type(exc).__name__), db=db)
        stats["failed"] += 1
        return {"status": "search_failed", **stats}

    jobs: List[Dict[str, Any]] = []
    seen_variant = set()
    for job in result.get("jobs") or []:
        if not _eligible_job(job):
            stats["skipped"] += 1
            continue
        posted = job.get("posted_at")
        if isinstance(watermark, datetime) and isinstance(posted, datetime):
            if posted.tzinfo is None:
                posted = posted.replace(tzinfo=timezone.utc)
            wm = watermark if watermark.tzinfo else watermark.replace(tzinfo=timezone.utc)
            if posted <= wm:
                stats["skipped"] += 1
                continue
        variant = job.get("dedupe_group_id") or job.get("job_id")
        if variant in seen_variant:
            stats["skipped"] += 1
            continue
        seen_variant.add(variant)
        job_id = str(job.get("job_id") or "")
        if not job_id:
            continue
        if not _claim_delivery(email, search_id, job_id, db=db):
            stats["skipped"] += 1
            continue
        jobs.append({
            "title": job.get("title") or job.get("title_raw"),
            "company": job.get("employer_name") or job.get("company"),
            "job_id": job_id,
            "why": (job.get("why_matched") or [])[:3],
        })
        stats["delivered"] += 1
        if len(jobs) >= MAX_JOBS_PER_DIGEST:
            break

    db[tfss.SAVED_SEARCHES].update_one(
        {"user_email": email, "client_id": search_id},
        {"$set": {"alerts.last_evaluated_at": now,
                  "alerts.last_delivered_at": now if jobs else alerts.get("last_delivered_at")}})

    if not jobs:
        return {"status": "empty", **stats}

    payload = _digest_payload(row.get("name") or "Saved search", jobs)
    try:
        from services.notifications import service as notify
        notify.send(email, EVENT, payload)
    except Exception as exc:
        _record_failure(email, search_id, str(type(exc).__name__), db=db)
        stats["failed"] += 1
        return {"status": "send_failed", **stats}
    return {"status": "sent", "jobs": len(jobs), **stats}


def _digest_payload(name: str, jobs: List[Dict[str, Any]]) -> Dict[str, Any]:
    lines = [f"{j.get('title')} — {j.get('company')}" for j in jobs]
    body = "New roles matching \"%s\":\n\n%s" % (name, "\n".join(lines))
    return {
        "email": {
            "subject": f"{len(jobs)} new roles for {name}",
            "text": body,
            "html": "<p>" + body.replace("\n", "<br>") + "</p>",
        },
        "jobs": jobs,
        "search_name": name,
    }


def _record_failure(email: str, search_id: str, reason: str, *, db=None) -> None:
    db = db if db is not None else _db()
    try:
        db[FAILURES].insert_one({
            "user_hash": hashlib.sha256(email.encode("utf-8")).hexdigest()[:16],
            "search_id": search_id,
            "reason": reason[:80],
            "at": _now(),
        })
    except Exception:
        logger.warning("alert failure log write failed")


def run_daily_sweep(*, limit: int = 200, db=None) -> Dict[str, Any]:
    """Evaluate enabled alerts. Safe to re-run: deliveries are idempotent."""
    from services import tech_feed_saved_searches as tfss

    db = db if db is not None else _db()
    ensure_indexes(db=db)
    now = _now()
    since = now - timedelta(hours=20)
    query = {
        "alerts.enabled": True,
        "$or": [
            {"alerts.last_evaluated_at": {"$exists": False}},
            {"alerts.last_evaluated_at": None},
            {"alerts.last_evaluated_at": {"$lte": since}},
        ],
    }
    tally = {"searches": 0, "sent": 0, "empty": 0, "suppressed": 0,
             "failed": 0, "deleted": 0}
    for row in db[tfss.SAVED_SEARCHES].find(query).limit(int(limit)):
        tally["searches"] += 1
        out = evaluate_search(row, now=now, db=db)
        key = out.get("status") or "empty"
        if key in tally:
            tally[key] += 1
        elif key in ("paused", "unsubscribed", "event_disabled", "email_disabled"):
            tally["suppressed"] += 1
        elif key in ("search_failed", "send_failed"):
            tally["failed"] += 1
    try:
        db[METRICS].insert_one({"at": now, **tally, "alert_version": ALERT_VERSION})
    except Exception:
        pass
    return tally
