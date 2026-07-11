"""Dispatcher for EventBridge-triggered scheduled jobs (Compass scheduler).

The resume Lambda is self-invoked with ``{"scheduled_job": "<name>"}`` on a
cron/rate schedule (EventBridge -> Lambda). :func:`dispatch_scheduled` routes
the name to one of the sweep functions below. Everything here is best-effort
and defensive: a scheduled run must NEVER raise out of the handler, and one
user's failure must never abort the sweep for the rest.

Timezone model
--------------
The digest sweeps are triggered ONCE PER HOUR. Each sweep self-filters users by
their *local* hour, so a single hourly trigger delivers the 07:00 morning
digest and the 19:00 evening summary to every user at their own wall-clock time
regardless of timezone — no per-timezone schedules required.

Import-safe: Mongo / resume_service / notifications are only touched inside
functions, so importing this module never does I/O or worsens cold starts.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from utils.db_connect import DBConnect

logger = logging.getLogger(__name__)

# Internal, non-user-supplied event keys we sweep on. Kept explicit so a key can
# never end up in a Mongo operator position from an untrusted source.
_DIGEST_EVENTS = ("daily_digest", "evening_summary")

_DEFAULT_TZ = "America/New_York"

_VALID_EXPERIENCE = ("any", "internship", "entry", "associate", "mid", "senior")


def _prefs_col():
    return DBConnect().get_db().notification_prefs


def _user_prefs_col():
    return DBConnect().get_db().user_preferences


def _scheduled_posts_col():
    return DBConnect().get_db().scheduled_posts


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Audience selection
# ---------------------------------------------------------------------------
def _target_users_for_digest(event_key: str) -> List[str]:
    """Emails of users who EXPLICITLY enabled ``event_key``.

    Digests are opt-in (``default_on`` is False), so only users with a prefs
    doc where the event toggle is truthy, email delivery is enabled, and who are
    not unsubscribed qualify. ``event_key`` is an internal literal, never user
    input, so it is safe in the filter (we still restrict it to known keys).
    """
    if event_key not in _DIGEST_EVENTS:
        return []
    try:
        cur = _prefs_col().find(
            {
                "unsubscribed": {"$ne": True},
                "channels.email.enabled": True,
                f"events.{event_key}": True,
            },
            {"_id": 0, "user_email": 1},
        )
        emails = [d["user_email"] for d in cur if d.get("user_email")]
        return emails
    except Exception as e:
        logger.warning("[scheduled] target lookup failed for %s: %s", event_key, e)
        return []


def _local_hour(email: str) -> Optional[int]:
    """The user's CURRENT local hour (0-23) using their quiet_hours.tz.

    Falls back to America/New_York when the user has no tz or the zone can't be
    resolved (missing tzdata / bad name). Returns None only if the lookup itself
    fails hard.
    """
    tz_name = _DEFAULT_TZ
    try:
        doc = _prefs_col().find_one({"user_email": email}, {"_id": 0, "quiet_hours": 1}) or {}
        tz_name = ((doc.get("quiet_hours") or {}).get("tz")) or _DEFAULT_TZ
    except Exception as e:
        logger.warning("[scheduled] tz lookup failed for %s: %s", email, e)
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(tz_name)).hour
    except Exception:
        try:
            from zoneinfo import ZoneInfo
            return datetime.now(ZoneInfo(_DEFAULT_TZ)).hour
        except Exception:
            return _now_utc().hour


# ---------------------------------------------------------------------------
# Pipeline payload from the user's declared preferences
# ---------------------------------------------------------------------------
def _pipeline_payload(email: str) -> Dict[str, Any]:
    """Build a FREE daily_pipeline payload from the user's ``user_preferences``.

    ``apify_token=""`` is REQUIRED so the scheduled digest pipeline stays on the
    free direct sources and never spends paid Apify credits.
    """
    prefs: Dict[str, Any] = {}
    try:
        prefs = _user_prefs_col().find_one({"user_email": email}, {"_id": 0}) or {}
    except Exception as e:
        logger.warning("[scheduled] user_preferences lookup failed for %s: %s", email, e)
        prefs = {}

    level = str(prefs.get("experience_level") or "").lower()
    if level not in _VALID_EXPERIENCE:
        level = "entry"

    locations = prefs.get("target_locations")
    if isinstance(locations, list) and locations and str(locations[0]).strip():
        location = str(locations[0]).strip()
    else:
        location = "United States"

    roles = prefs.get("target_roles")
    roles = [str(r).strip() for r in roles if str(r or "").strip()] if isinstance(roles, list) else []

    return {
        "user_email": email,
        "experience_level": level,
        "location": location,
        "workday_titles": roles,
        "custom_role_terms": roles,
        "linkedin_keywords": roles,
        "past_days": 1,
        "apify_token": "",
        "skip_apify": True,  # free path: direct Workday CXS + ATS boards only, no Apify
        "include_indeed": False,
        "send_digest": True,
    }


# ---------------------------------------------------------------------------
# Sweeps
# ---------------------------------------------------------------------------
def run_morning_digests() -> Dict[str, Any]:
    """Enqueue a free daily_pipeline (which self-sends the digest on completion)
    for every opted-in user whose local hour is 07:00."""
    enqueued = 0
    for email in _target_users_for_digest("daily_digest"):
        try:
            if _local_hour(email) != 7:
                continue
            from services.resume_service import get_resume_service, ResumeService
            payload = _pipeline_payload(email)
            job_id = get_resume_service().create_job("daily_pipeline", payload, user_email=email)
            ResumeService.invoke_async(job_id, "daily_pipeline", payload)
            enqueued += 1
        except Exception as e:
            logger.warning("[scheduled] morning digest enqueue failed for %s: %s", email, e)
    logger.info("[scheduled] run_morning_digests enqueued=%s", enqueued)
    return {"enqueued": enqueued}


def run_evening_summaries() -> Dict[str, Any]:
    """Send the evening summary to every opted-in user whose local hour is 19:00."""
    sent = 0
    skipped = 0
    for email in _target_users_for_digest("evening_summary"):
        try:
            if _local_hour(email) != 19:
                skipped += 1
                continue
            from services.notifications import digests, service as notify
            payload = digests.build_evening_summary(email)
            notify.send(email, "evening_summary", payload)
            sent += 1
        except Exception as e:
            logger.warning("[scheduled] evening summary failed for %s: %s", email, e)
    logger.info("[scheduled] run_evening_summaries sent=%s skipped=%s", sent, skipped)
    return {"sent": sent, "skipped": skipped}


def run_bandit_nightly() -> Dict[str, Any]:
    """Nightly signal-bandit update over settled post metrics."""
    try:
        from services import post_metrics_service
        result = post_metrics_service.nightly_update()
        logger.info("[scheduled] run_bandit_nightly -> %s", result)
        return result if isinstance(result, dict) else {"ok": True}
    except Exception as e:
        logger.warning("[scheduled] run_bandit_nightly failed: %s", e)
        return {"error": "bandit_nightly_failed"}


def run_learning_due() -> Dict[str, Any]:
    """Publish every learning auto-post whose schedule is due (status==scheduled
    and scheduled_for <= now). Internal data only — no user-supplied filter."""
    sent = 0
    failed = 0
    try:
        cur = _scheduled_posts_col().find(
            {"status": "scheduled", "scheduled_for": {"$lte": _now_utc()}},
            {"_id": 0, "schedule_id": 1},
        )
        due = [d.get("schedule_id") for d in cur if d.get("schedule_id")]
    except Exception as e:
        logger.warning("[scheduled] learning due lookup failed: %s", e)
        due = []

    for schedule_id in due:
        try:
            from services.learning import publish
            publish.send_scheduled_post(schedule_id)
            sent += 1
        except Exception as e:
            failed += 1
            logger.warning("[scheduled] learning send failed for %s: %s", schedule_id, e)

    logger.info("[scheduled] run_learning_due sent=%s failed=%s", sent, failed)
    return {"sent": sent, "failed": failed}


def run_events_refresh() -> Dict[str, Any]:
    """Refresh the global hackathon/webinar catalog from the free sources
    (Devpost + confs.tech). use_llm=False keeps it free — the legitimacy score
    rides the deterministic heuristic + source trust for these curated sources."""
    try:
        from services import event_scraper
        result = event_scraper.run_scrape({
            "sources": ["devpost", "confstech"],
            "use_llm": False,
        })
        logger.info("[scheduled] run_events_refresh -> %s", result)
        return result if isinstance(result, dict) else {"ok": True}
    except Exception as e:
        logger.warning("[scheduled] run_events_refresh failed: %s", e)
        return {"error": "events_refresh_failed"}


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------
def dispatch_scheduled(name: str) -> Dict[str, Any]:
    """Route an EventBridge scheduled trigger to its sweep. Never raises."""
    try:
        if name == "digest_hourly":
            # One hourly trigger covers BOTH 07:00 morning digests and 19:00
            # evening summaries; each sweep self-filters by the user's local hour.
            morning = run_morning_digests()
            evening = run_evening_summaries()
            return {"job": name, "morning": morning, "evening": evening}
        if name == "bandit_nightly":
            return {"job": name, "result": run_bandit_nightly()}
        if name == "learning_due":
            return {"job": name, "result": run_learning_due()}
        if name == "events_refresh":
            return {"job": name, "result": run_events_refresh()}
        logger.warning("[scheduled] unknown job: %s", name)
        return {"error": "unknown job", "name": name}
    except Exception as e:
        logger.error("[scheduled] dispatch_scheduled(%s) crashed: %s", name, e, exc_info=True)
        return {"error": "dispatch_failed", "name": name}
