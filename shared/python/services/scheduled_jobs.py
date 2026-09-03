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

    excluded = prefs.get("exclude_title_terms")
    excluded = ([str(t).strip() for t in excluded if str(t or "").strip()]
                if isinstance(excluded, list) else [])

    return {
        "user_email": email,
        "experience_level": level,
        "location": location,
        "workday_titles": roles,
        "custom_role_terms": roles,
        # Never `roles`. The scheduled digest is the one path a user cannot
        # see running, so an exclusion silently inverted here is invisible.
        "exclude_title_terms": excluded,
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


def run_apply_hourly() -> Dict[str, Any]:
    """Hourly auto-apply sweep: enqueue eligible applications, send the evening
    summary (self-filtered by local hour), and reconcile the Oracle verify
    freezes. Every leg is isolated so one failure never aborts the others.
    """
    result: Dict[str, Any] = {}
    from services import apply_scheduler

    try:
        result["hourly"] = apply_scheduler.run_apply_hourly()
    except Exception as e:
        logger.error("[scheduled] apply_hourly hourly sweep failed: %s", e, exc_info=True)
        result["hourly"] = {"error": "apply_hourly_failed"}

    try:
        result["evening"] = apply_scheduler.run_apply_evening_summary()
    except Exception as e:
        logger.error("[scheduled] apply_hourly evening summary failed: %s", e, exc_info=True)
        result["evening"] = {"error": "apply_evening_failed"}

    # Reconcile the Oracle re-check: drive due verify-freezes in this same sweep.
    # Wrapped per-freeze so one bad record never kills the sweep.
    rechecked = 0
    recheck_errors = 0
    try:
        from services import auto_apply_service
        for w in auto_apply_service.due_verify_waits(limit=200):
            try:
                auto_apply_service.recheck_verify(w["application_id"], w["user_email"])
                rechecked += 1
            except Exception as e:
                recheck_errors += 1
                logger.warning("[scheduled] apply_hourly recheck_verify failed: %s", e)
    except Exception as e:
        logger.error("[scheduled] apply_hourly verify sweep failed: %s", e, exc_info=True)
    result["verify_recheck"] = {"rechecked": rechecked, "errors": recheck_errors}

    return result


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------
def _source_for_ingest_rule(name: str) -> Optional[str]:
    """`ingest_career_pages` -> `career-pages`, for any source the worker knows.

    EventBridge rule names cannot contain a hyphen in the same places a source
    key can, so the rule is the source with `-` written as `_`. Deriving the map
    means a new source is reachable the moment the worker declares it.
    """
    try:
        from services.job_ingest_worker import SOURCES
    except Exception:  # pragma: no cover - defensive
        SOURCES = ("workday", "career-pages")
    suffix = (name or "")[len("ingest_"):]
    for source in SOURCES:
        if suffix == source.replace("-", "_"):
            return source
    return None


def dispatch_scheduled(name: str) -> Dict[str, Any]:
    """Route an EventBridge scheduled trigger to its sweep. Never raises."""
    try:
        if name == "digest_hourly":
            # One hourly trigger covers BOTH 07:00 morning digests and 19:00
            # evening summaries; each sweep self-filters by the user's local hour.
            morning = run_morning_digests()
            evening = run_evening_summaries()
            from services.resume_service import get_resume_service, ResumeService
            job_id = get_resume_service().create_job(
                "saved_search_alerts", {}, "system@internal")
            ResumeService.invoke_async(job_id, "saved_search_alerts", {})
            return {"job": name, "morning": morning, "evening": evening,
                    "saved_search_alerts": job_id}
        if name == "bandit_nightly":
            return {"job": name, "result": run_bandit_nightly()}
        if name == "learning_due":
            return {"job": name, "result": run_learning_due()}
        if name == "events_refresh":
            return {"job": name, "result": run_events_refresh()}
        if name == "apply_hourly":
            return {"job": name, "result": run_apply_hourly()}
        if name == "apply_match_sweep":
            # Spec §5 — the NEW-POSTING fan-out (`apply_hourly` only ever looked
            # at jobs the user had already SAVED by hand; this is the half that
            # was missing). Bridged to the resume `_process_job` job type of the
            # same name rather than run inline, like `ingest_*` and unlike
            # `apply_hourly`: the sweep does per-user S3 reads plus scoring, so
            # it needs its own Lambda budget instead of sharing the scheduled
            # tick's. System actor, empty payload; per-user gating, entitlement
            # checks and the daily cap all live inside the sweep.
            #
            # NOTE: unlike the `ingest_` prefix branch below, a new rule key
            # needs BOTH this branch AND an entry in infra/terraform/
            # schedulers.tf — there is no prefix match to fall through to.
            from services.resume_service import get_resume_service, ResumeService
            job_id = get_resume_service().create_job(
                "apply_match_sweep", {}, "system@internal")
            ResumeService.invoke_async(job_id, "apply_match_sweep", {})
            return {"job": name, "enqueued": job_id}
        if name == "trend_aggregate":
            # System/cron recompute of the cross-user market trend aggregate.
            # This bridges an EventBridge scheduled trigger to the resume
            # `_process_job` job type of the same name (system actor, no
            # user_email; the payload is ignored). Never exposed on a user route.
            from services.resume_service import get_resume_service, ResumeService
            job_id = get_resume_service().create_job("trend_aggregate", {}, "system@internal")
            ResumeService.invoke_async(job_id, "trend_aggregate", {})
            return {"job": name, "enqueued": job_id}
        if name == "guest_retention":
            # Guest Onboarding v2 — daily claim-aware retention sweep. Same
            # system-actor bridge as trend_aggregate: enqueue the resume
            # `_process_job` job `guest_retention_cleanup` (deletes ONLY
            # expired UNCLAIMED guest sessions/records/jobs; retention_days=0
            # → the job reports skipped). Also admin-invokable via
            # POST /api/admin/guest-retention-cleanup.
            from services.resume_service import get_resume_service, ResumeService
            job_id = get_resume_service().create_job("guest_retention_cleanup", {}, "system@internal")
            ResumeService.invoke_async(job_id, "guest_retention_cleanup", {})
            return {"job": name, "enqueued": job_id}
        if name == "ats_signal_aggregate":
            # System/cron recompute of the cross-user (role, keyword) -> mean-ATS
            # aggregate (Part B3). Same bridge pattern as trend_aggregate:
            # system actor, no user_email, aggregate-only output. Not user-routable.
            from services.resume_service import get_resume_service, ResumeService
            job_id = get_resume_service().create_job("ats_signal_aggregate", {}, "system@internal")
            ResumeService.invoke_async(job_id, "ats_signal_aggregate", {})
            return {"job": name, "enqueued": job_id}
        if name == "discover_companies":
            # ADR-004 career-portal discovery. Kept OUT of the `ingest_` family
            # on purpose: those dispatch shards of a known registry, this one
            # grows the registry itself, and conflating them would make the
            # ingest tick's concurrency ceiling govern discovery too.
            from services.resume_service import get_resume_service, ResumeService
            job_type, payload = "company_discovery_dispatch", {}
            job_id = get_resume_service().create_job(job_type, payload,
                                                     "system@internal")
            ResumeService.invoke_async(job_id, job_type, payload)
            return {"job": name, "enqueued": job_id}
        if name.startswith("ingest_"):
            # ADR-003 canonical job inventory. One prefix branch covers every
            # ingestion rule so adding a shard schedule is a Terraform-only
            # change, not a code change:
            #   ingest_workday / ingest_career_pages -> dispatch that source's
            #       due shards (bounded per tick; `due_shards` excludes leased
            #       and backed-off shards, which is the backpressure)
            #   ingest_maintenance -> the retention sweep
            # Both bridge to the resume `_process_job` job types of the same
            # family, system actor, exactly like trend_aggregate above.
            from services.resume_service import get_resume_service, ResumeService
            if name == "ingest_maintenance":
                job_type, payload = "job_ingest_maintenance", {}
            else:
                # DERIVED from the worker's source list, not hand-listed here.
                # The comment above promises that adding a shard schedule is a
                # Terraform-only change; with a literal dict that was false, and
                # it is why `oracle` and `icims` had EventBridge-shaped names
                # but no branch to receive them. A rule whose source is missing
                # here fires forever and dispatches nothing.
                source = _source_for_ingest_rule(name)
                if source is None:
                    logger.warning("[scheduled] unknown ingest rule: %s", name)
                    return {"error": "unknown job", "name": name}
                job_type, payload = "job_ingest_dispatch", {"source": source}
            job_id = get_resume_service().create_job(job_type, payload, "system@internal")
            ResumeService.invoke_async(job_id, job_type, payload)
            return {"job": name, "enqueued": job_id}
        logger.warning("[scheduled] unknown job: %s", name)
        return {"error": "unknown job", "name": name}
    except Exception as e:
        logger.error("[scheduled] dispatch_scheduled(%s) crashed: %s", name, e, exc_info=True)
        return {"error": "dispatch_failed", "name": name}
