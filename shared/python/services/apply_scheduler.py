"""Auto-apply scheduler sweeps (Auto-Apply Tranche 1 §5).

Two best-effort, per-user error-isolated sweeps invoked from the resume
Lambda's scheduled-event branch (``services.scheduled_jobs.dispatch_scheduled``
-> EventBridge). They are the hourly nudge + EOD actionable digest that sit on
TOP of the already-built spine:

  * ``application_ledger_service``  — dedup gate + ledger rows + status machine
  * ``approval_token_service``      — signed single-use magic-link tokens
  * ``apply_email_templates``       — the 3 pure email builders + ``send``
  * ``today_inbox_service``         — the "what still needs you" aggregator
  * ``auto_submit_service``         — the ``AUTO_SUBMIT_ENABLED`` master flag +
                                      per-user opt-in (``auto_submit_settings``)

run_apply_hourly()
    For every opted-in user (master flag ON + per-user opt-in), do the three
    things §5 asks for, each independently isolated:
      1. Re-drive stuck submissions: ledger rows sitting in the submit-pickup
         state get ``auto_apply_submit`` re-enqueued (Oracle re-check). The
         ledger's compare-and-set guards make a double-submit impossible, so a
         re-drive is always safe.
      2. Enqueue new matches: the user's saved/matched postings pass through the
         dedup gate -> a ledger row is created -> ``auto_apply_gather`` is
         enqueued (recon done in the background job, NEVER inline here). Postings
         past ``posting_closes_at`` are skipped.
      3. Nudge: the matches freshly queued THIS tick that still need the user's
         go-ahead (autonomy ask/hold) get ONE ``apply_approval_request`` email
         with scoped magic-link buttons — but only outside the user's quiet
         hours. Emailing only this tick's fresh rows makes the nudge naturally
         idempotent (the dedup gate guarantees a posting is queued exactly once,
         so it is nudged exactly once).

run_apply_evening_summary()
    At the user's local 19:00, the actionable ``apply_daily_summary``: what was
    applied today (submitted/confirmed ledger rows) + what still needs them
    (``today_inbox_service``), with live buttons. Skipped when there is nothing
    to report so we never send an empty nightly email.

Timezone model mirrors the digest sweep: a single hourly trigger fans out to
every user at their own wall-clock time; each sweep self-filters by the user's
local hour / quiet hours (from ``notification_prefs.quiet_hours``). Nothing here
calls an LLM or spends résumé quota. Import-safe: Mongo / resume_service /
notifications / template imports all live INSIDE functions, so importing this
module never does I/O or worsens cold starts.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from utils.db_connect import DBConnect

logger = logging.getLogger(__name__)

# Fall back here when a user has no declared timezone (matches scheduled_jobs).
_DEFAULT_TZ = "America/New_York"

# Per-user hourly work bounds — a single noisy account can never monopolise the
# sweep's Lambda budget. Postings beyond this per tick simply roll to next hour.
_MAX_MATCHES_PER_USER = 25       # new postings enqueued per user per tick
_MAX_MATCHES_AGGRESSIVE = 50     # Auto Aggressive — more daily matching volume
_MAX_APPROVAL_MATCHES = 25       # rows listed in one approval email
_MAX_REDRIVE_PER_USER = 25       # stuck submissions re-driven per user per tick
_MAX_SUMMARY_ITEMS = 25          # rows shown in one EOD summary section

# Errors that mean "our side broke", never "this application is bad". ONLY
# these are auto-requeued (see step 1b-ii); a validation failure or an employer
# rejection is a real outcome and stays failed. Matched exactly, not by
# substring, so a new error string cannot accidentally join the retry set.
_INFRA_ERRORS = frozenset({
    "executor_unavailable_midflight",
    "executor_unavailable",
    "no_confirmation_after_submit",
})
# Attempts before an infrastructure failure is left alone. Three ticks of the
# hourly sweep is enough to ride out a deploy or a drained executor pool; more
# than that is a standing outage a human needs to look at.
_MAX_INFRA_RETRIES = 3


def _is_non_us(row: Dict[str, Any]) -> bool:
    """Does this ledger row point at a posting we can prove is not US?

    Requires a POSITIVE foreign signal, not merely a failed keep-test: an
    unresolvable "2 Locations" rollup must not cost a real US application.
    Same asymmetry the cleanup script uses.
    """
    from urllib.parse import unquote

    from services import us_geo

    location, country = "", None
    job_id = row.get("posting_job_id")
    if job_id:
        try:
            doc = DBConnect().get_db().job_postings.find_one(
                {"job_id": job_id}, {"locations": 1})
            locs = (doc or {}).get("locations") or []
            first = locs[0] if locs else {}
            if isinstance(first, dict):
                location, country = str(first.get("raw") or ""), first.get("country")
        except Exception:  # pragma: no cover - fall through to the URL
            pass
    if not location:
        # Every Workday tenant builds `.../job/<Location>/<Title>_<req>`.
        url = str(row.get("apply_url") or "")
        if "/job/" in url:
            location = unquote(url.split("/job/", 1)[1].split("/", 1)[0]
                               ).replace("-", " ").strip()
    if not location:
        return False
    return (not us_geo.keep_us(location, country)
            and us_geo.has_foreign_marker(location))


def _count_infra_retry(user_email: str, application_id: str) -> None:
    """Record one infrastructure retry so the bound above is enforceable.

    Kept on the application row rather than in a side collection: it must be
    read in the same query the redrive already runs, and it must survive a
    requeue (the ledger's status transitions do not touch unknown fields)."""
    try:
        DBConnect().get_db().applications.update_one(
            {"user_email": user_email, "application_id": application_id},
            {"$inc": {"infra_retries": 1}})
    except Exception as e:  # pragma: no cover - the retry itself already ran
        logger.warning("[apply_scheduler] infra_retries bump failed (%s): %s",
                       application_id, e)

# The EOD summary and the hourly nudge both fire at the user's LOCAL hour off a
# single UTC hourly trigger (same idiom as run_evening_summaries).
_EVENING_HOUR = 19


# ---------------------------------------------------------------------------
# Collections (internal, non-user-supplied filters only)
# ---------------------------------------------------------------------------
def _auto_submit_settings_col():
    return DBConnect().get_db().auto_submit_settings


def _applications_col():
    return DBConnect().get_db().applications


def _prefs_col():
    return DBConnect().get_db().notification_prefs


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if isinstance(dt, datetime) and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _parse_dt(value: Any) -> Optional[datetime]:
    """Best-effort parse of a datetime or ISO-8601 string to an aware UTC dt."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return _as_aware(value)
    if isinstance(value, str):
        try:
            return _as_aware(datetime.fromisoformat(value.replace("Z", "+00:00")))
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------------------
# Audience + timezone (opt-in comes from auto_submit_settings; tz from prefs)
# ---------------------------------------------------------------------------
def _opted_in_users() -> List[str]:
    """Emails of users who EXPLICITLY opted into auto-apply.

    The per-user opt-in lives in ``auto_submit_settings.auto_submit_enabled``
    (the same toggle auto_submit_service.user_opted_in reads). The filter is a
    literal we control — no user input reaches an operator position.
    """
    try:
        cur = _auto_submit_settings_col().find(
            {"auto_submit_enabled": True},
            {"_id": 0, "user_email": 1},
        )
        return [d["user_email"] for d in cur if d.get("user_email")]
    except Exception as e:
        logger.warning("[apply_scheduler] opted-in lookup failed: %s", e)
        return []


def _tz_name(email: str) -> str:
    try:
        doc = _prefs_col().find_one(
            {"user_email": email}, {"_id": 0, "quiet_hours": 1}
        ) or {}
        return ((doc.get("quiet_hours") or {}).get("tz")) or _DEFAULT_TZ
    except Exception as e:
        logger.warning("[apply_scheduler] tz lookup failed for %s: %s", email, e)
        return _DEFAULT_TZ


def _local_hour(email: str) -> int:
    """The user's CURRENT local hour (0-23), falling back to the default tz then
    UTC. Mirrors scheduled_jobs._local_hour so the two sweeps agree on time."""
    tz_name = _tz_name(email)
    for name in (tz_name, _DEFAULT_TZ):
        try:
            from zoneinfo import ZoneInfo
            return datetime.now(ZoneInfo(name)).hour
        except Exception:
            continue
    return _now().hour


def _local_day_start_utc(email: str) -> datetime:
    """UTC instant of the user's local midnight — the lower bound for "applied
    today". Falls back to a rolling 24h window if the zone can't be resolved."""
    try:
        from zoneinfo import ZoneInfo
        now_local = datetime.now(ZoneInfo(_tz_name(email)))
        start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        return start_local.astimezone(timezone.utc)
    except Exception:
        return _now() - timedelta(hours=24)


def _in_quiet_hours(email: str) -> bool:
    """True when the user is currently inside their quiet-hours window — reuses
    the notifications service's exact logic so the nudge respects the same
    do-not-disturb the digests do. Defensive: any failure => not quiet (we would
    rather nudge than silently swallow, and email_service still suppression-
    checks)."""
    try:
        from services.notifications import service as notify
        prefs = notify.get_prefs(email)
        now_local = notify._user_now(prefs)
        return bool(notify._in_quiet_hours(prefs, now_local))
    except Exception as e:
        logger.warning("[apply_scheduler] quiet-hours check failed for %s: %s", email, e)
        return False


# ---------------------------------------------------------------------------
# Match extraction from saved/matched postings
# ---------------------------------------------------------------------------
def _saved_jobs(email: str) -> List[Dict[str, Any]]:
    """The user's saved/matched postings via the job_service reader (which also
    backs the Applications board). Best-effort — an empty list on any failure."""
    try:
        from services.job_service import JobService
        return JobService().get_saved_jobs(email) or []
    except Exception as e:
        logger.warning("[apply_scheduler] saved-jobs lookup failed for %s: %s", email, e)
        return []


# Tracker statuses that mean "this user has NOT acted on this posting yet", and
# is therefore the ONLY set the auto-apply sweep may enqueue from.
#
# This is an ALLOW-list, and deliberately so. `saved_jobs.status` is the manual
# tracker; `applications` is the auto-apply ledger. They are separate stores, so
# the ledger's `(user_email, job_identity)` unique index CANNOT see a job the
# user applied to by hand — its dedup gate passes and the sweep happily queues a
# second application to an employer who already has one.
#
# Measured on live prod 2026-08-22: one `apply_hourly` tick queued 49 rows, of
# which **44 were postings the user had already applied to manually** (Vanguard
# twice, eBay, JPMorgan, AbbVie, Kong, Zipline, Woolpert, Pathward, ...). With
# autonomy at the user's configured 0.6 those would have submitted duplicates
# under their real name — the exact spam signal that gets a candidate filtered.
#
# A deny-list would reintroduce the bug the moment a new status is added, so any
# UNRECOGNISED status is treated as "already acted on" and skipped. Missing the
# occasional application is recoverable; sending a duplicate to a real employer
# is not.
_SWEEPABLE_SAVED_STATUSES = frozenset({"", "interested", "saved", "new", "matched"})


def _is_sweepable(saved: Dict[str, Any]) -> bool:
    """True only when the tracker says the user has not yet acted on this row."""
    status = str((saved or {}).get("status") or "").strip().lower()
    return status in _SWEEPABLE_SAVED_STATUSES


def _match_fields(saved: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Normalize one saved_job doc into the fields the ledger needs. The raw
    posting lives under ``job_data`` (the shape job_service._normalize_job emits:
    title/company/location/apply_link/description/source). Returns None when it
    lacks the company+title the dedup key requires."""
    jd = saved.get("job_data") or {}
    company = str(jd.get("company") or "").strip()
    title = str(jd.get("title") or "").strip()
    if not company or not title:
        return None
    return {
        "company": company,
        "title": title,
        "jd_text": str(jd.get("description") or ""),
        "apply_url": str(jd.get("apply_link") or jd.get("apply_url")
                         or jd.get("url") or ""),
        "source": str(jd.get("source") or "saved"),
        "location": str(jd.get("location") or ""),
        "posting_closes_at": (jd.get("posting_closes_at") or jd.get("valid_through")
                              or jd.get("closes_at") or jd.get("expires_at")),
    }


def _is_closed(value: Any, *, now: datetime) -> bool:
    dt = _parse_dt(value)
    return dt is not None and dt <= now


# ---------------------------------------------------------------------------
# Hourly sweep
# ---------------------------------------------------------------------------
def run_apply_hourly() -> Dict[str, Any]:
    """The §5 hourly auto-apply sweep. Never raises; one user's failure never
    aborts the rest. Returns aggregate counters for the dispatcher log."""
    # Master kill-switch first — inert until an operator flips AUTO_SUBMIT_ENABLED.
    try:
        from services import auto_submit_service as ass
        if not ass.auto_submit_enabled():
            logger.info("[apply_scheduler] hourly: master flag off, skipping")
            return {"skipped": "master_off"}
    except Exception as e:
        logger.warning("[apply_scheduler] hourly: master-flag check failed: %s", e)
        return {"skipped": "master_check_failed"}

    users = _opted_in_users()
    totals = {"users": len(users), "enqueued": 0, "redriven": 0,
              "gather_redriven": 0, "infra_retried": 0, "reresolved": 0,
              "emailed": 0, "skipped_closed": 0}
    for email in users:
        try:
            res = _run_user_hourly(email)
            totals["enqueued"] += res["enqueued"]
            totals["redriven"] += res["redriven"]
            totals["gather_redriven"] += res.get("gather_redriven", 0)
            totals["infra_retried"] += res.get("infra_retried", 0)
            totals["reresolved"] += res.get("reresolved", 0)
            totals["emailed"] += res["emailed"]
            totals["skipped_closed"] += res["skipped_closed"]
        except Exception as e:  # per-user isolation
            logger.warning("[apply_scheduler] hourly failed for %s: %s", email, e)
    logger.info("[apply_scheduler] run_apply_hourly %s", totals)
    return totals


def _run_user_hourly(email: str) -> Dict[str, int]:
    from services import application_ledger_service as als
    from services import autonomy_service
    from services.resume_service import get_resume_service, ResumeService

    now = _now()
    out = {"enqueued": 0, "redriven": 0, "gather_redriven": 0,
           "infra_retried": 0, "reresolved": 0,
           "emailed": 0, "skipped_closed": 0}
    mode = autonomy_service.get_mode(email)
    match_cap = (
        _MAX_MATCHES_AGGRESSIVE
        if mode == autonomy_service.MODE_AGGRESSIVE
        else _MAX_MATCHES_PER_USER
    )

    # 1. Re-drive stuck submissions (Oracle re-check). Rows sitting in the
    #    submit-pickup state get auto_apply_submit re-enqueued. The ledger's CAS
    #    transition guards make a double-submit structurally impossible, so a
    #    re-drive is always idempotent. (F: `approved` is the submit-pickup state —
    #    submit consumes approved/queued_for_execution; getattr keeps this
    #    forward-compatible if a later tranche adds a dedicated waiting_verify.)
    redrive_status = getattr(als, "STATUS_WAITING_VERIFY", als.STATUS_APPROVED)
    try:
        stuck = als.list_applications(email, status=redrive_status,
                                      limit=_MAX_REDRIVE_PER_USER)
    except Exception as e:
        logger.warning("[apply_scheduler] redrive list failed for %s: %s", email, e)
        stuck = []
    for row in stuck:
        aid = row.get("application_id")
        if not aid:
            continue
        # Never re-submit a posting whose window already closed.
        if _is_closed(row.get("posting_closes_at"), now=now):
            out["skipped_closed"] += 1
            continue
        try:
            payload = {"user_email": email, "application_id": aid,
                       "apply_url": row.get("apply_url") or ""}
            job_id = get_resume_service().create_job(
                "auto_apply_submit", payload, user_email=email)
            ResumeService.invoke_async(job_id, "auto_apply_submit", payload)
            out["redriven"] += 1
        except Exception as e:
            logger.warning("[apply_scheduler] redrive enqueue failed (%s): %s", aid, e)

    # 1b. Re-drive rows that never GATHERED.
    #
    # `apply_match_sweep` (and /api/apply/queue) create the ledger row and then
    # fire `auto_apply_gather` as an async Lambda self-invoke. That invoke is
    # best-effort: if it is throttled, dropped, or its Lambda dies before the
    # job runs, the row sits at `queued` forever. Nothing re-drove it — step 1
    # above only looks at the SUBMIT-pickup state — so the application was
    # silently orphaned in a state the UI does not even render.
    #
    # Observed in production: of 10 rows the sweep created in one tick, 4
    # gathered and 6 were still `queued` with a single `application_created`
    # audit line and no answers document, half an hour later.
    #
    # Re-enqueueing gather is safe: `gather` is idempotent (it early-returns for
    # any row that has already left `queued`/`gathering`), and the dedup gate
    # plus the ledger's CAS transitions mean a duplicate cannot create a second
    # application or a second submission.
    gather_redrive_statuses = (als.STATUS_QUEUED, als.STATUS_GATHERING)
    for status in gather_redrive_statuses:
        try:
            ungathered = als.list_applications(email, status=status,
                                               limit=_MAX_REDRIVE_PER_USER)
        except Exception as e:
            logger.warning("[apply_scheduler] gather-redrive list failed (%s) "
                           "for %s: %s", status, email, e)
            continue
        for row in ungathered:
            aid = row.get("application_id")
            if not aid:
                continue
            if _is_closed(row.get("posting_closes_at"), now=now):
                out["skipped_closed"] += 1
                continue
            try:
                payload = {"user_email": email, "application_id": aid,
                           "apply_url": row.get("apply_url") or ""}
                job_id = get_resume_service().create_job(
                    "auto_apply_gather", payload, user_email=email)
                ResumeService.invoke_async(job_id, "auto_apply_gather", payload)
                out["gather_redriven"] = out.get("gather_redriven", 0) + 1
            except Exception as e:
                logger.warning(
                    "[apply_scheduler] gather-redrive enqueue failed (%s): %s",
                    aid, e)

    # 1b-ii. Requeue rows that failed for an INFRASTRUCTURE reason.
    #
    # `auto_apply_service` marks a mid-flight executor drain as
    # `failed / executor_unavailable_midflight` and calls it "retryable" — but
    # nothing ever retried it. `application_ledger_service.RETRYABLE_STATUSES`
    # is only consulted by `dedup_gate`, i.e. when a NEW enqueue happens to hit
    # the same posting, and `apply_match_sweep` advances its watermark past a
    # posting once it has been examined, so that second enqueue never comes.
    #
    # Observed in the live ledger: 3 of 6 failed applications (Dovetail, NXP,
    # K2 Space) carry exactly this error. The executor was simply not
    # available; nothing was wrong with the application, and nothing was ever
    # sent to the employer. Leaving them dead means a user loses three real
    # applications to an ops event they never saw.
    #
    # Bounded by `_MAX_INFRA_RETRIES` attempts so a persistently unavailable
    # executor cannot spin the row forever, and deliberately restricted to a
    # CLOSED LIST of infrastructure errors — a genuine validation failure or an
    # employer rejection must stay failed.
    try:
        broken = als.list_applications(email, status=als.STATUS_FAILED,
                                       limit=_MAX_REDRIVE_PER_USER)
    except Exception as e:
        logger.warning("[apply_scheduler] infra-retry list failed for %s: %s", email, e)
        broken = []
    for row in broken:
        aid = row.get("application_id")
        err = str(row.get("error") or "").strip()
        if not aid or err not in _INFRA_ERRORS:
            continue
        if _is_closed(row.get("posting_closes_at"), now=now):
            out["skipped_closed"] += 1
            continue
        if int(row.get("infra_retries") or 0) >= _MAX_INFRA_RETRIES:
            continue
        # Never resurrect a non-US row. Some failed rows pre-date the geo
        # filter on the intake sweep (a Bucharest NXP req is in the live
        # ledger with exactly this error), and `failed` cannot transition to
        # `skipped`, so the cleanup script cannot retire them either. Without
        # this check the retry would put them straight back into the queue.
        if _is_non_us(row):
            continue
        try:
            als.requeue(email, aid)
            _count_infra_retry(email, aid)
            payload = {"user_email": email, "application_id": aid,
                       "apply_url": row.get("apply_url") or ""}
            job_id = get_resume_service().create_job(
                "auto_apply_gather", payload, user_email=email)
            ResumeService.invoke_async(job_id, "auto_apply_gather", payload)
            out["infra_retried"] = out.get("infra_retried", 0) + 1
        except Exception as e:
            logger.warning("[apply_scheduler] infra retry failed (%s): %s", aid, e)

    # 1c. Re-resolve STALE `needs_input` rows against the CURRENT profile.
    #
    # A row is parked with the questions that were unresolvable at GATHER time
    # and then never looked at again. So finishing onboarding — or answering the
    # same question on any other application — does not release the rows already
    # waiting on exactly that fact. Observed in production: two rows (EnergyHub,
    # Adyen) parked on "Country" four minutes BEFORE the user's onboarding wrote
    # `country`; both then sat asking for a value the platform already held.
    #
    # `resume_after_answers` with an EMPTY answer map is precisely this
    # re-resolution: it merges nothing, re-resolves every still-open question,
    # shrinks the queue to what is genuinely still missing, and promotes the row
    # to `ready_for_review` (then auto-approve + submit under an effective
    # `auto`) only when nothing is left. Walls it cannot clear stay walls.
    #
    # Idempotent: a row with nothing newly resolvable comes back unchanged.
    try:
        parked = als.list_applications(email, status=als.STATUS_NEEDS_INPUT,
                                       limit=_MAX_REDRIVE_PER_USER)
    except Exception as e:
        logger.warning("[apply_scheduler] needs_input list failed for %s: %s",
                       email, e)
        parked = []
    for row in parked:
        aid = row.get("application_id")
        if not aid:
            continue
        if _is_closed(row.get("posting_closes_at"), now=now):
            out["skipped_closed"] += 1
            continue
        try:
            from services import auto_apply_service as _aas
            result = _aas.resume_after_answers(email, aid, {})
            if result.get("application_status") != als.STATUS_NEEDS_INPUT:
                out["reresolved"] = out.get("reresolved", 0) + 1
                logger.info(
                    "[apply_scheduler] re-resolved stale needs_input %s -> %s",
                    aid, result.get("application_status"))
        except Exception as e:
            # Per-row isolation: a 409/404 or a resolver error must never abort
            # the sweep for the remaining rows.
            logger.warning("[apply_scheduler] re-resolve failed (%s): %s", aid, e)

    # 2. Enqueue new matches through the dedup gate -> ledger row -> gather job.
    #    Auto Aggressive / Autopilot users get matching postings queued daily
    #    (hourly sweep) without waiting for a manual click.
    fresh: List[Dict[str, Any]] = []  # this tick's rows that still need a go-ahead
    for saved in _saved_jobs(email)[: match_cap * 3]:
        if out["enqueued"] >= match_cap:
            break
        # The user already applied / interviewed / was rejected -> never re-offer.
        # See _SWEEPABLE_SAVED_STATUSES: the ledger dedup cannot see manual
        # applications, so this is the only thing standing between the sweep and
        # a duplicate submission.
        if not _is_sweepable(saved):
            out["skipped_already_actioned"] = out.get("skipped_already_actioned", 0) + 1
            continue
        fields = _match_fields(saved)
        if not fields:
            continue
        if _is_closed(fields["posting_closes_at"], now=now):
            out["skipped_closed"] += 1
            continue
        try:
            identity = als.normalized_job_identity(
                fields["company"], fields["title"], fields["jd_text"])
            skip, _existing = als.dedup_gate(email, identity)
            if skip:
                continue  # already handled — do not re-offer
            record, outcome = als.create_application(
                email,
                company=fields["company"],
                title=fields["title"],
                jd_text=fields["jd_text"],
                source=fields["source"],
                apply_url=fields["apply_url"],
                posting_closes_at=fields["posting_closes_at"],
            )
            if outcome == "skipped_duplicate":
                continue
        except als.ApplicationLedgerError as e:
            logger.warning("[apply_scheduler] ledger create failed for %s: %s", email, e)
            continue
        except Exception as e:
            logger.warning("[apply_scheduler] ledger create errored for %s: %s", email, e)
            continue

        aid = record.get("application_id")
        if not aid:
            continue
        out["enqueued"] += 1
        # Kick off Phase-1 recon in the background (NEVER inline). If the
        # auto_apply_gather branch is not yet live, the async job fails in
        # isolation and never touches this sweep.
        try:
            payload = {"user_email": email, "application_id": aid,
                       "apply_url": fields["apply_url"], "company": fields["company"],
                       "title": fields["title"], "source": fields["source"]}
            job_id = get_resume_service().create_job(
                "auto_apply_gather", payload, user_email=email)
            ResumeService.invoke_async(job_id, "auto_apply_gather", payload)
        except Exception as e:
            logger.warning("[apply_scheduler] gather enqueue failed (%s): %s", aid, e)

        # F: approval emails fire ONLY once a row reaches `ready_for_review` — the
        # manual-approval wait state — so we never email a link the user cannot yet
        # act on (approve is tightened to ready_for_review). A freshly-created row
        # is `queued`, so this tick's inline collection is normally empty; a
        # dedicated ready_for_review nudge sweep drives these emails after gather
        # lands the row (follow-up). canonical_status tolerates a legacy `ready`.
        if als.canonical_status(record.get("status")) == als.STATUS_READY_FOR_REVIEW:
            fresh.append({"application_id": aid, "title": fields["title"],
                          "company": fields["company"], "location": fields["location"]})

    # 3. Nudge — one approval email over THIS tick's fresh ask/hold rows, but
    #    only outside quiet hours (background work above always runs).
    if fresh and not _in_quiet_hours(email):
        if _send_approval_email(email, fresh[:_MAX_APPROVAL_MATCHES]):
            out["emailed"] += 1

    return out


def _send_approval_email(email: str, fresh: List[Dict[str, Any]]) -> bool:
    """Mint scoped magic links + build & send apply_approval_request. Best-effort:
    a link-minting or send failure logs and returns False (never raises)."""
    try:
        from services import approval_token_service as ats
        from services import apply_email_templates as tmpl

        ids = [m["application_id"] for m in fresh if m.get("application_id")]
        if not ids:
            return False
        approve_all_url = ats.make_link("approve_all", email, ids)
        review_url = ats.make_link("review", email, ids)
        skip_url = ats.make_link("skip", email, ids)

        matches: List[Dict[str, Any]] = []
        for m in fresh:
            aid = m.get("application_id")
            row = {"title": m.get("title"), "company": m.get("company"),
                   "location": m.get("location")}
            if aid:
                # Per-row "Approve this one" — a token scoped to just this id.
                try:
                    row["approve_url"] = ats.make_link("approve_one", email, [aid])
                except Exception:
                    pass
            matches.append(row)

        template = tmpl.apply_approval_request(
            matches, approve_all_url=approve_all_url,
            review_url=review_url, skip_url=skip_url)
        return bool(tmpl.send(email, template))
    except Exception as e:
        logger.warning("[apply_scheduler] approval email failed for %s: %s", email, e)
        return False


# ---------------------------------------------------------------------------
# Evening summary sweep
# ---------------------------------------------------------------------------
def run_apply_evening_summary() -> Dict[str, Any]:
    """The §5 actionable EOD digest. Fires off the same hourly trigger; each user
    is served at their local 19:00. Never raises; per-user isolated."""
    try:
        from services import auto_submit_service as ass
        if not ass.auto_submit_enabled():
            return {"skipped": "master_off"}
    except Exception as e:
        logger.warning("[apply_scheduler] evening: master-flag check failed: %s", e)
        return {"skipped": "master_check_failed"}

    users = _opted_in_users()
    totals = {"users": len(users), "sent": 0, "skipped_hour": 0, "skipped_empty": 0}
    for email in users:
        try:
            if _local_hour(email) != _EVENING_HOUR:
                totals["skipped_hour"] += 1
                continue
            outcome = _run_user_evening(email)
            if outcome == "sent":
                totals["sent"] += 1
            else:
                totals["skipped_empty"] += 1
        except Exception as e:  # per-user isolation
            logger.warning("[apply_scheduler] evening failed for %s: %s", email, e)
    logger.info("[apply_scheduler] run_apply_evening_summary %s", totals)
    return totals


def _submitted_today(email: str) -> List[Dict[str, Any]]:
    """Ledger rows submitted/confirmed since the user's local midnight. The
    filter is fully internal (email is from the opted-in DB list, statuses are
    literals) — nothing user-supplied reaches an operator position."""
    from services import application_ledger_service as als
    since = _local_day_start_utc(email)
    try:
        cur = _applications_col().find(
            {"user_email": email,
             "status": {"$in": [als.STATUS_SUBMITTED, als.STATUS_CONFIRMED]},
             "submitted_at": {"$gte": since}},
            {"_id": 0, "title": 1, "company": 1},
        ).limit(_MAX_SUMMARY_ITEMS)
        return [{"title": d.get("title"), "company": d.get("company")} for d in cur]
    except Exception as e:
        logger.warning("[apply_scheduler] submitted-today lookup failed for %s: %s", email, e)
        return []


def _needs_you(email: str) -> List[Dict[str, Any]]:
    """What still needs the user, from the today_inbox aggregator, mapped to the
    apply_daily_summary shape. needs_input rows get a per-row scoped `review`
    magic link; the inbox title ("Company — Role") is split back into fields."""
    try:
        from services import today_inbox_service as inbox
        from services import approval_token_service as ats
        items = (inbox.get_today_inbox(email) or {}).get("items") or []
    except Exception as e:
        logger.warning("[apply_scheduler] today_inbox lookup failed for %s: %s", email, e)
        return []

    out: List[Dict[str, Any]] = []
    for it in items[:_MAX_SUMMARY_ITEMS]:
        raw_title = str(it.get("title") or "")
        if " — " in raw_title:
            company, _, role = raw_title.partition(" — ")
        else:
            company, role = "", raw_title
        ident = str(it.get("id") or "")
        aid = ident.split(":", 1)[1] if ":" in ident else ""
        action_url = None
        if aid and it.get("kind") == "needs_input":
            try:
                action_url = ats.make_link("review", email, [aid])
            except Exception:
                action_url = None
        out.append({
            "title": role or raw_title,
            "company": company,
            "reason": str(it.get("detail") or "")[:160],
            "action_url": action_url,
        })
    return out


def _run_user_evening(email: str) -> str:
    """Build + send one user's actionable EOD summary. Returns "sent" or
    "skipped" (nothing to report — we never send an empty nightly email)."""
    from services import approval_token_service as ats
    from services import apply_email_templates as tmpl

    submitted = _submitted_today(email)
    needs_you = _needs_you(email)
    if not submitted and not needs_you:
        return "skipped"

    # Bottom "Open your applications" button — a scoped review link over the
    # needs-input ids we can resolve (empty scope still deep-links the board).
    ids: List[str] = []
    try:
        from services import today_inbox_service as inbox
        for it in (inbox.get_today_inbox(email) or {}).get("items") or []:
            ident = str(it.get("id") or "")
            if it.get("kind") == "needs_input" and ":" in ident:
                ids.append(ident.split(":", 1)[1])
    except Exception:
        ids = []
    try:
        review_url = ats.make_link("review", email, ids[:_MAX_SUMMARY_ITEMS])
    except Exception as e:
        logger.warning("[apply_scheduler] evening review link failed for %s: %s", email, e)
        return "skipped"

    template = tmpl.apply_daily_summary(submitted, needs_you, review_url=review_url)
    tmpl.send(email, template)
    return "sent"
