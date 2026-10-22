"""New-posting -> auto-apply fan-out (spec §5) — the missing discovery step.

THE GAP THIS CLOSES
-------------------
Two halves of auto-apply were built and never joined:

  * `job_ingest_worker.run_shard` crawls 112 shards into the SHARED
    `job_postings` inventory. On a brand-new posting it increments a counter
    (`outcome["created"]`) and stops. No per-user fan-out, no event, no
    notification. `match_alert` is a declared notification event with zero
    senders.
  * `apply_scheduler.run_apply_hourly` enqueues applications — but its
    candidate set is `saved_jobs`, i.e. postings the USER manually clicked
    save on. So the user was still the discovery step, and "auto"-apply only
    ever applied to jobs the user had already found by hand.

This module is the join: opted-in user -> active résumé -> postings this user
has never been shown -> fit score -> threshold -> ledger -> gather. It runs
unattended on a 15-minute tick so a job posted at 06:00 is applied to long
before the user wakes up.

WHY A WATERMARK ON `first_seen_at`
----------------------------------
`posted_at` is what the EMPLOYER claims; it is frequently absent, relative
("3 days ago"), or backdated, and it does not move monotonically as we crawl.
`first_seen_at` is stamped by `job_inventory_service.upsert_posting` in
`$setOnInsert` — written exactly once, at the instant WE first observed the
posting, never rewritten by a re-crawl. That makes it the only field in the
collection usable as a resumable cursor: read in ascending `first_seen_at`
order, advance the watermark only over rows actually EXAMINED, and no posting
is ever skipped or reconsidered. (Same reasoning as the ingest watermark in
`job_ingest_worker.run_shard`.)

Served by the `active_cost_first_seen_at` index (scripts/migrate_indexes.py).

SAFETY — every one of these is load-bearing, do not remove
---------------------------------------------------------
1. Master kill-switch (`auto_submit_service.auto_submit_enabled`) first.
2. Explicit autonomy MODE required. `get_mode` returns None until onboarding
   forces a pick, and an unset mode REFUSES to auto-act (fail-safe). `copilot`
   is excluded: that user asked to apply manually, so spending their quota on
   an unattended fan-out is not what they bought.
3. Entitlement gate BEFORE any spend, and a per-day cap (`auto_apply_daily`,
   20/day on EVERY plan) reserved ATOMICALLY per enqueue. Without it, 96 ticks
   a day x 50 matches per tick = ~4,800 unattended applications/day for an
   unlimited-plan user. The reservation is not refunded on downstream failure:
   the cap bounds ATTEMPTS, which is the point.
4. NO POSTING WITHOUT READABLE EVIDENCE. A row whose requirements cannot be
   read (no JD body in S3, nothing extractable) is dropped, never scored on
   title alone. `fit_score` would cap such a row at 45 anyway
   (`insufficient_job_requirements`), but dropping it is the honest version:
   we do not spend an application on a posting we could not read.
5. `dedup_gate` + the unique `(user_email, job_identity)` index, exactly as the
   hourly sweep does — a posting is applied to at most once per user, ever.
6. Closed-posting skip, re-checked immediately before the ledger write (the
   scoring pass can take seconds, and a shard may close a posting inside it).
7. Nothing here submits. It creates a `queued` ledger row and enqueues
   `auto_apply_gather`; every downstream guard (`autonomy_service.decide`'s
   never-fabricate hard rules, `presubmit_recheck`, the mode cap, the monthly
   `auto_submit` pool) still applies unchanged.

No LLM call happens anywhere in this module. `fit_score.extract_requirements`
is the deterministic regex/lexicon extractor, not a Bedrock call.

Import-safe: Mongo / fit_score / ledger / resume imports all live INSIDE
functions, so importing this module does no I/O and does not worsen cold starts.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from utils.db_connect import DBConnect

logger = logging.getLogger(__name__)


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, "") or default))
    except (TypeError, ValueError):
        return default


# How many NEW postings one user's tick considers. This is a per-tick bound on
# a cursor, not a corpus scan: in steady state a 15-minute window across the
# user's role families is a handful of rows, and the watermark carries the
# remainder into the next tick rather than dropping it.
CANDIDATE_LIMIT = _env_int("MATCH_SWEEP_CANDIDATES", 300)
# How many candidates may have their JD body hydrated from S3 in one tick.
# Bodies live in `job_content_store` (one GET each, 64-entry in-process cache),
# so this is the tick's real I/O budget.
HYDRATE_LIMIT = _env_int("MATCH_SWEEP_HYDRATE", 120)
# Cold start: a user with no watermark looks back this far rather than at the
# entire inventory. 24h is one full crawl cycle (~1h45m) many times over.
COLD_START_LOOKBACK_HOURS = _env_int("MATCH_SWEEP_LOOKBACK_HOURS", 24)

# Per-tick enqueue caps. The DAILY cap below is the real bound; these just stop
# one tick from consuming the whole day's budget in one burst.
MAX_ENQUEUE_PER_TICK = _env_int("MATCH_SWEEP_MAX_PER_TICK", 5)
MAX_ENQUEUE_PER_TICK_AGGRESSIVE = _env_int("MATCH_SWEEP_MAX_PER_TICK_AGGRESSIVE", 10)

# The per-day blast-radius throttle (entitlements_service.FEATURE_PERIODS).
DAILY_CAP_FEATURE = "auto_apply_daily"
# The boolean capability gate checked before any spend.
FEATURE_KEY = "auto_apply"
# Ledger provenance: this row was discovered by the sweep, not saved by hand.
LEDGER_SOURCE = "match-sweep"

# Where the per-user cursor lives — the SAME `auto_submit_settings` doc as the
# opt-in / mode / thresholds, so the whole autonomy posture is one document.
WATERMARK_FIELD = "match_sweep_watermark"


def _settings_col():
    return DBConnect().get_db().auto_submit_settings


def _postings_col():
    return DBConnect().get_db().job_postings


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_aware(dt: Optional[datetime]) -> Optional[datetime]:
    if isinstance(dt, datetime) and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


# ---------------------------------------------------------------------------
# Per-user watermark
# ---------------------------------------------------------------------------
def get_watermark(user_email: str) -> datetime:
    """The user's `first_seen_at` cursor, or a cold-start lookback.

    Cold start deliberately looks back COLD_START_LOOKBACK_HOURS instead of at
    the epoch: a user who opts in today should see today's market, not have
    18k historical postings scored against them in one tick.
    """
    floor = _now() - timedelta(hours=COLD_START_LOOKBACK_HOURS)
    if not user_email:
        return floor
    try:
        doc = _settings_col().find_one(
            {"user_email": user_email}, {"_id": 0, WATERMARK_FIELD: 1}
        ) or {}
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("[match_sweep] watermark read failed for %s: %s", user_email, e)
        return floor
    stored = _as_aware(doc.get(WATERMARK_FIELD))
    if not isinstance(stored, datetime):
        return floor
    # Never look back further than the cold-start floor even if a stale
    # watermark says to: a user who was inactive for a month must not have a
    # month of inventory replayed at them in a single tick.
    return max(stored, floor)


def advance_watermark(user_email: str, seen_at: Optional[datetime]) -> bool:
    """Move the cursor forward, NEVER backward, and never create a settings doc
    (upsert=False — the sweep only ever runs for users who already have one).

    The guard lives in the FILTER, not in application code: a concurrent tick
    that read an older window cannot rewind the cursor, because its update
    simply matches nothing. Returns True when the stored value moved.
    """
    seen_at = _as_aware(seen_at)
    if not user_email or not isinstance(seen_at, datetime):
        return False
    try:
        res = _settings_col().update_one(
            {"user_email": user_email,
             "$or": [{WATERMARK_FIELD: {"$exists": False}},
                     {WATERMARK_FIELD: None},
                     {WATERMARK_FIELD: {"$lt": seen_at}}]},
            {"$set": {WATERMARK_FIELD: seen_at, "match_sweep_at": _now()}},
            upsert=False,
        )
        return bool(getattr(res, "modified_count", 0))
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("[match_sweep] watermark write failed: %s", type(e).__name__)
        return False


# ---------------------------------------------------------------------------
# Candidate retrieval
# ---------------------------------------------------------------------------
# Everything `inventory_feed._row_from_posting` reads, plus what hydration and
# the ledger write need. Explicit so a large `description_text` (only present
# when the S3 offload was unavailable) is the ONLY heavy field we ever pull.
_PROJECTION = {
    "_id": 0, "job_id": 1, "title_raw": 1, "employer_name": 1, "tenant": 1,
    "ats": 1, "industry": 1, "locations": 1, "work_mode": 1, "employment_type": 1,
    "seniority": 1, "canonical_url": 1, "posted_at": 1, "source": 1,
    "requirements": 1, "content_hash": 1, "description_ref": 1,
    "description_text": 1, "first_seen_at": 1,
    # employer-policy-1.0.0: what the shared policy reads.
    "canonical_employer_id": 1, "canonical_url": 1, "board_kind": 1,
    "status": 1, "last_verified_at": 1, "last_seen_at": 1, "valid_through": 1,
    "closed_reason": 1, "posting_signals": 1, "source_job_id": 1,
}


def _new_postings(role_families: List[str], since: datetime,
                  limit: int = CANDIDATE_LIMIT) -> List[Dict[str, Any]]:
    """Postings first observed AFTER `since`, oldest-first.

    Filter shape (matches `active_cost_first_seen_at`):
        {status:"active", cost_class:"free", first_seen_at:{$gt: since},
         posted_at_confidence:{$ne:"unknown"}, role_family:{$in:[...]}}
        sort {first_seen_at: 1}

    * `cost_class:"free"` — direct-ATS postings only (ADR-004). Metered
      aggregator rows are neither cheap nor reliably applicable.
    * `posted_at_confidence != unknown` — ADR-001; a posting whose date we
      cannot establish is not auto-applied to.
    * role_family is a residual filter (never a key field) so the watermark
      sort stays an index walk. Families come from the RESUME
      (`fit_score.build_resume_profile`), not from user free-text, and are
      normalized by `classify_role` — nothing user-supplied reaches an
      operator position.
    """
    from services import job_inventory_service as inv

    families = [str(f) for f in (role_families or []) if isinstance(f, str) and f]
    query: Dict[str, Any] = {
        "status": inv.STATUS_ACTIVE,
        "cost_class": inv.COST_CLASS_FREE,
        "first_seen_at": {"$gt": since},
        "posted_at_confidence": {"$ne": inv.CONFIDENCE_UNKNOWN},
    }
    if families:
        query["role_family"] = {"$in": families} if len(families) > 1 else families[0]
    try:
        return list(
            _postings_col()
            .find(query, _PROJECTION)
            .sort("first_seen_at", 1)
            .limit(max(1, int(limit)))
        )
    except Exception as e:
        logger.warning("[match_sweep] candidate query failed: %s", e)
        return []


def _jd_body(doc: Dict[str, Any]) -> str:
    """The posting's JD text: inline if it survived the offload, else one S3
    GET through `job_content_store` (content-hash keyed, cached). Never raises —
    an unreadable body means the row is dropped, not that the sweep fails."""
    text = str(doc.get("description_text") or "")
    if text:
        return text
    if not doc.get("description_ref"):
        return ""
    try:
        from services import job_content_store
        return str(job_content_store.get_jd(doc.get("content_hash") or "") or "")
    except Exception as e:  # pragma: no cover - storage is best effort
        logger.warning("[match_sweep] JD fetch failed for %s: %s",
                       str(doc.get("job_id"))[:12], type(e).__name__)
        return ""


def _hydrate(docs: List[Dict[str, Any]], limit: int = HYDRATE_LIMIT,
             excluded_ids: Optional[set] = None
             ) -> Tuple[List[Dict[str, Any]], Dict[str, Dict[str, Any]], int,
                        Optional[datetime]]:
    """Inventory docs -> scoreable rows.

    Returns ``(rows, context_by_job_id, dropped_no_evidence, examined_max)``. A
    row is produced ONLY when we have requirements the scorer can measure —
    either a persisted `requirements` profile from the ingestion path, or a
    deterministic extraction off the JD body. Rows without either are DROPPED:
    `fit_score` caps a body-less posting at 45 ("this fit is based on the job
    title alone"), and an unattended application must never be placed on that
    basis.

    `context_by_job_id` carries the jd_text and vendor fields the ledger write
    needs, keyed by the canonical inventory job_id.

    `examined_max` is the high-water `first_seen_at` over EVERY doc this call
    actually looked at — surviving rows AND dropped ones. It is what the caller
    advances the cursor to, and it is returned from here (rather than derived
    from `context`) because a context-only high-water mark LIVELOCKS: a window
    in which every candidate is dropped for no readable evidence yields an empty
    context, so the cursor never moves, the same candidates are re-read (and
    their bodies re-fetched) every 15 minutes forever, and postings behind the
    stuck window are never reached — auto-apply silently stops for that user.
    A doc we stopped BEFORE examining (the I/O budget below) is deliberately NOT
    included, which is what keeps the cursor lossless. The trade-off is explicit:
    a posting whose body we could not read is skipped for good rather than
    re-attempted forever — re-reading it never made it readable.
    """
    from services import fit_score, us_geo
    from services.inventory_feed import _row_from_posting

    rows: List[Dict[str, Any]] = []
    context: Dict[str, Dict[str, Any]] = {}
    dropped = 0
    dropped_non_us = 0
    hydrated = 0
    examined_max: Optional[datetime] = None

    def _examined(doc: Dict[str, Any]) -> None:
        nonlocal examined_max
        seen = _as_aware(doc.get("first_seen_at"))
        if isinstance(seen, datetime) and (examined_max is None or seen > examined_max):
            examined_max = seen

    for doc in docs:
        job_id = str(doc.get("job_id") or "")
        if not job_id:
            _examined(doc)
            continue
        # US-ONLY, enforced HERE and not only on the browse feed. This sweep had
        # no geo filter at all, so the unattended fan-out enqueued whatever the
        # crawler happened to surface: a State Street "Krakow, Poland" req and a
        # Revvity Workday role are both in the live ledger. Applying on the
        # user's behalf to a country they cannot work in is worse than a bad
        # search result — it burns a daily-cap slot and reaches a real employer.
        # Same `us_geo` decision the feed uses, so what a user is SHOWN and what
        # we APPLY TO can never disagree.
        if not us_geo.keep_us_posting(doc):
            dropped_non_us += 1
            _examined(doc)
            continue
        # The user's direct-employer policy, decided by the caller with the
        # SAME function the feed uses. EXAMINED, so the watermark moves past
        # it: a window of excluded postings must not pin the cursor forever.
        if excluded_ids and job_id in excluded_ids:
            _examined(doc)
            continue
        row = _row_from_posting(doc, tz_offset=0)
        if row is None:      # no usable posted_at -> ADR-001 exclusion
            dropped += 1
            _examined(doc)
            continue

        reqs = doc.get("requirements")
        jd_text = ""
        if not fit_score.has_job_requirements(reqs):
            if hydrated >= limit:
                # Out of I/O budget this tick. Leave it for the next one: this
                # doc is NOT marked examined, so the watermark stops short of it.
                break
            hydrated += 1
            jd_text = _jd_body(doc)
            reqs = fit_score.extract_requirements(jd_text, row.get("title") or "")
            if not fit_score.has_job_requirements(reqs):
                dropped += 1
                _examined(doc)
                continue

        row["_requirements"] = reqs
        row["job_id"] = job_id
        rows.append(row)
        _examined(doc)
        context[job_id] = {
            "jd_text": jd_text,
            "ats_family": (doc.get("ats")
                           or ("workday" if doc.get("source") == "workday" else "")),
            "tenant": doc.get("tenant") or "",
            "first_seen_at": _as_aware(doc.get("first_seen_at")),
        }
    if dropped_non_us:
        # Logged separately from the no-evidence drops so the two reasons a
        # candidate disappears stay distinguishable in prod.
        logger.info("[match_sweep] dropped %d non-US candidate(s)", dropped_non_us)
    return rows, context, dropped + dropped_non_us, examined_max


def _still_open(job_id: str) -> bool:
    """Re-check the posting is STILL active immediately before the ledger write.

    Scoring a tick's candidates involves S3 reads and can take seconds, and a
    concurrent ingest shard may close a posting inside that window. Cheap
    (indexed on `job_id_unique`) and only run for the handful of rows that
    actually passed the fit gate. Fails CLOSED."""
    from services import job_inventory_service as inv
    try:
        doc = _postings_col().find_one({"job_id": job_id}, {"_id": 0, "status": 1})
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("[match_sweep] open re-check failed for %s: %s", job_id[:12], e)
        return False
    return bool(doc) and doc.get("status") == inv.STATUS_ACTIVE


# ---------------------------------------------------------------------------
# The sweep
# ---------------------------------------------------------------------------
# Modes the unattended sweep acts for. `copilot` (prepare-only, user applies by
# hand) is excluded deliberately, and an UNSET mode (None) refuses to auto-act —
# the fail-safe documented in autonomy_service.
def _sweep_modes() -> Tuple[str, ...]:
    from services import autonomy_service as autonomy
    return (autonomy.MODE_APPROVAL, autonomy.MODE_AUTOPILOT, autonomy.MODE_AGGRESSIVE)


def run_match_sweep() -> Dict[str, Any]:
    """Fan every opted-in user out over postings new since their watermark.

    Never raises; one user's failure never aborts the rest. Returns aggregate
    counters for the dispatcher log (no PII — emails are never logged at INFO).
    """
    try:
        from services import auto_submit_service as ass
        if not ass.auto_submit_enabled():
            logger.info("[match_sweep] master flag off, skipping")
            return {"skipped": "master_off"}
    except Exception as e:
        logger.warning("[match_sweep] master-flag check failed: %s", e)
        return {"skipped": "master_check_failed"}

    from services.apply_scheduler import _opted_in_users

    users = _opted_in_users()
    totals: Dict[str, Any] = {
        "users": len(users), "swept": 0, "candidates": 0, "scored": 0,
        "enqueued": 0, "below_threshold": 0, "no_evidence": 0,
        "duplicates": 0, "closed": 0, "daily_capped": 0, "alerts": 0,
        "skipped": {},
    }
    for email in users:
        try:
            res = run_user_sweep(email)
        except Exception as e:  # per-user isolation
            logger.warning("[match_sweep] user sweep failed: %s", type(e).__name__)
            continue
        reason = res.get("skipped")
        if reason:
            totals["skipped"][reason] = totals["skipped"].get(reason, 0) + 1
            continue
        totals["swept"] += 1
        for key in ("candidates", "scored", "enqueued", "below_threshold",
                    "no_evidence", "duplicates", "closed", "daily_capped", "alerts"):
            totals[key] += int(res.get(key) or 0)
    logger.info("[match_sweep] run_match_sweep %s", totals)
    return totals


def run_user_sweep(user_email: str) -> Dict[str, Any]:
    """One user's tick. Returns counters, or ``{"skipped": <reason>}``.

    Gating order is deliberate — cheapest and most decisive first, and every
    spend gate is passed BEFORE a single posting is read:
      mode -> entitlement -> monthly pool -> résumé -> candidates -> score ->
      threshold -> dedup -> still-open -> DAILY CAP -> ledger -> gather.
    """
    from services import autonomy_service as autonomy
    from services import entitlements_service as ent

    if not user_email or not isinstance(user_email, str):
        return {"skipped": "invalid_user"}

    # 1. Explicit autonomy mode. None = onboarding never forced the pick, and an
    #    unattended sweep must not act on an unstated preference.
    mode = autonomy.get_mode(user_email)
    if mode not in _sweep_modes():
        return {"skipped": "mode_" + (mode or "unset")}

    # 2. Capability gate (403 `upgrade_required` equivalent for a background
    #    actor: there is no request to answer, so we simply do not spend).
    if not ent.feature_allowed(user_email, FEATURE_KEY):
        return {"skipped": "not_entitled"}

    # 3. Monthly auto_submit pool. Non-incrementing check — the pool is charged
    #    later, at approval (application_ledger_service), so this only avoids
    #    queueing rows that could never submit. Fails OPEN by design: when the
    #    atomic reservation path (AUTO_APPLY_RESERVE) is on, usage lives in
    #    quota_counters rather than feature_usage and the real denial happens at
    #    approve. A pre-check that guesses wrong must not silently disable the
    #    sweep, so it only ever short-circuits on a definite exhaustion.
    try:
        allowed, usage = ent.check_quota(user_email, "auto_submit")
        if not allowed and (usage or {}).get("used"):
            return {"skipped": "monthly_pool_exhausted"}
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("[match_sweep] monthly pool check failed: %s", type(e).__name__)

    # 4. The résumé IS the matcher. No résumé -> nothing to match against, and
    #    nothing to apply with. Never fabricate one.
    from services import fit_score, resume_access

    try:
        record = resume_access.get_active_structured(user_email)
    except Exception as e:
        logger.warning("[match_sweep] resume load failed: %s", type(e).__name__)
        return {"skipped": "resume_error"}
    structured = (record or {}).get("structured") or {}
    if not structured:
        return {"skipped": "no_resume"}

    profile = fit_score.build_resume_profile(structured)
    families = sorted(f for f in (profile.get("families") or set()) if f)

    threshold = autonomy.get_match_threshold(user_email)
    tick_cap = (MAX_ENQUEUE_PER_TICK_AGGRESSIVE
                if mode == autonomy.MODE_AGGRESSIVE else MAX_ENQUEUE_PER_TICK)

    since = get_watermark(user_email)
    docs = _new_postings(families, since)
    out: Dict[str, Any] = {
        "candidates": len(docs), "scored": 0, "enqueued": 0,
        "below_threshold": 0, "no_evidence": 0, "duplicates": 0,
        "closed": 0, "daily_capped": 0, "alerts": 0,
        "threshold": threshold, "mode": mode,
    }
    if not docs:
        return out

    # `examined_max` is the high-water mark of every candidate `_hydrate` looked
    # at — INCLUDING the ones it dropped. Deriving it from `context` (surviving
    # rows only) livelocked the cursor whenever a whole window was dropped for no
    # readable evidence. `_hydrate` still stops short of anything it did not
    # examine (its I/O budget), so the cursor stays lossless.
    # What the user SEES is what we APPLY TO: the same policy, the same
    # prefs, before a single JD body is fetched.
    from services import employer_policy as ep
    allowed, policy_block = ep.apply([dict(d) for d in docs], ep.get_prefs(user_email))
    allowed_ids = {str(d.get("job_id") or "") for d in allowed}
    excluded_ids = {str(d.get("job_id") or "") for d in docs} - allowed_ids
    out["policy_excluded"] = len(excluded_ids)
    out["policy_excluded_counts"] = {k: v for k, v in
                                     policy_block["excluded_counts"].items() if v}

    rows, context, dropped, examined_max = _hydrate(docs, excluded_ids=excluded_ids)
    out["no_evidence"] = dropped
    out["scored"] = len(rows)

    if rows:
        # Reuse the shared scorer (the same engine the feed tabs and the guest
        # path use) rather than a second, drifting implementation. It writes
        # `match_score` / `matched_skills` / `missing_skills` / `fit` in place.
        from services.inventory_feed import _score_rows

        _score_rows(rows, user_email, [], None)
        rows.sort(key=lambda r: -(r.get("match_score") or 0))

    enqueued: List[Dict[str, Any]] = []
    for index, row in enumerate(rows):
        score = int(row.get("match_score") or 0)
        if score < threshold:
            # Rows are sorted by score descending, so the first miss ends the
            # scan and everything from here down is below the bar.
            out["below_threshold"] = len(rows) - index
            break
        if len(enqueued) >= tick_cap:
            break
        result = _enqueue_one(user_email, row, context.get(row.get("job_id")) or {},
                              score=score)
        if result == "enqueued":
            enqueued.append(row)
        elif result == "duplicate":
            out["duplicates"] += 1
        elif result == "closed":
            out["closed"] += 1
        elif result == "daily_capped":
            out["daily_capped"] += 1
            break  # the day's budget is gone; stop, do not keep probing
    out["enqueued"] = len(enqueued)

    # Advance the cursor only over examined rows, and only after the enqueues.
    # A crash before this point re-examines the same window next tick, which is
    # harmless (dedup_gate + the unique index absorb it) — the opposite ordering
    # would silently lose postings.
    advance_watermark(user_email, examined_max)

    if enqueued and _notify_matches(user_email, enqueued):
        out["alerts"] = 1
    return out


def _enqueue_one(user_email: str, row: Dict[str, Any], ctx: Dict[str, Any],
                 *, score: int) -> str:
    """Dedup -> still-open -> reserve the daily slot -> ledger row -> gather.

    Returns "enqueued" | "duplicate" | "closed" | "daily_capped" | "error".
    Never raises. The daily slot is reserved AFTER the dedup gate (so a posting
    we were never going to queue cannot consume the day's budget) and refunded
    if the ledger write turns out to be a no-op.
    """
    from services import application_ledger_service as als
    from services import quota_ledger_service as ql

    company = str(row.get("company") or "").strip()
    title = str(row.get("title") or "").strip()
    job_id = str(row.get("job_id") or "")
    if not company or not title or not job_id:
        return "error"
    jd_text = str(ctx.get("jd_text") or "")

    try:
        identity = als.normalized_job_identity(company, title, jd_text)
        skip, _existing = als.dedup_gate(user_email, identity)
        if skip:
            return "duplicate"
    except Exception as e:
        logger.warning("[match_sweep] dedup gate failed: %s", type(e).__name__)
        return "error"

    if not _still_open(job_id):
        return "closed"

    # THE per-day blast-radius cap. A guarded atomic $inc under the plan limit —
    # no check-then-increment, so overlapping ticks cannot both slip through.
    # The idempotency key is the (USER, posting) identity: `identity` alone is
    # NOT unique to a user — `job_postings` is shared inventory, so every user
    # who matches one posting derives the SAME hash. Without the user_email
    # prefix the key namespace is cross-tenant: the second user's reserve found
    # the FIRST user's entry, returned `reserved: True` with its own counter
    # untouched (so the 20/day cap stopped binding for everyone but the first
    # user), and a refund handed back a slot belonging to someone else.
    reservation = {"reserved": False, "entry_ids": [], "replayed": False}
    try:
        reservation = ql.reserve(
            user_email, DAILY_CAP_FEATURE,
            idempotency_key=f"{DAILY_CAP_FEATURE}:{user_email}:{identity}")
    except Exception as e:
        logger.warning("[match_sweep] daily cap reserve failed: %s", type(e).__name__)
        return "error"
    if not reservation.get("reserved"):
        logger.info("[match_sweep] daily auto-apply cap reached (limit=%s)",
                    (reservation.get("usage") or {}).get("limit"))
        return "daily_capped"

    def _refund() -> None:
        # A REPLAYED reservation charged nothing (the key matched a live entry
        # from a concurrent or redelivered tick), so refunding it would hand back
        # a slot the OTHER tick's real application still owes — the row exists and
        # nothing is charged for it. Only ever give back what this call took.
        if reservation.get("replayed"):
            return
        try:
            ql.refund_entries(reservation.get("entry_ids") or [],
                              user_email=user_email)
        except Exception:  # pragma: no cover - refund is best effort
            pass

    try:
        record, outcome = als.create_application(
            user_email,
            company=company,
            title=title,
            jd_text=jd_text,
            source=LEDGER_SOURCE,
            apply_url=str(row.get("apply_link") or row.get("url") or ""),
            ats_family=str(ctx.get("ats_family") or "") or None,
            tenant=str(ctx.get("tenant") or "") or None,
            # Pins the inventory row so the posting outlives the 7-day
            # retention window (archived to S3 before any delete).
            posting_job_id=job_id,
        )
    except als.ApplicationLedgerError as e:
        logger.warning("[match_sweep] ledger create rejected: %s", e)
        _refund()
        return "error"
    except Exception as e:
        logger.warning("[match_sweep] ledger create errored: %s", type(e).__name__)
        _refund()
        return "error"

    if outcome == "skipped_duplicate":
        _refund()
        return "duplicate"

    application_id = record.get("application_id")
    if not application_id:
        _refund()
        return "error"

    # Phase-1 recon runs in the BACKGROUND, never inline: gather probes the
    # form, resolves answers and stamps the autonomy decision. A failure there
    # is isolated to that job and never breaks the sweep.
    try:
        from services.resume_service import get_resume_service, ResumeService

        payload = {"user_email": user_email, "application_id": application_id,
                   "apply_url": str(row.get("apply_link") or ""),
                   "company": company, "title": title, "source": LEDGER_SOURCE,
                   "match_score": score}
        gather_id = get_resume_service().create_job(
            "auto_apply_gather", payload, user_email=user_email)
        ResumeService.invoke_async(gather_id, "auto_apply_gather", payload)
    except Exception as e:
        logger.warning("[match_sweep] gather enqueue failed for %s: %s",
                       str(application_id)[:8], type(e).__name__)
    return "enqueued"


def _notify_matches(user_email: str, rows: List[Dict[str, Any]]) -> bool:
    """Send ONE `match_alert` for this tick's enqueued matches.

    `match_alert` has been a declared notification event with zero senders since
    it was written (notifications/service.py). This is its first. The
    notification layer enforces opt-in (`default_on: False`), quiet hours and
    the proactive daily cap, and never raises — so this is safe to call on every
    tick without becoming a 96-emails-a-day machine.
    """
    try:
        import html as _html
        from services.notifications import service as notify

        lines = []
        for row in rows[:10]:
            lines.append("{} — {} ({}% match)".format(
                str(row.get("company") or "")[:80],
                str(row.get("title") or "")[:120],
                int(row.get("match_score") or 0)))
        text = "We started applications for:\n" + "\n".join("• " + l for l in lines)
        items = "".join("<li>{}</li>".format(_html.escape(l)) for l in lines)
        subject = ("New match — applying for you" if len(rows) == 1
                   else f"{len(rows)} new matches — applying for you")
        result = notify.send(user_email, "match_alert", {
            "title": subject,
            "text": text,
            "email": {
                "subject": subject,
                "html": ("<p>We found new postings that match your résumé and "
                         "started applications:</p><ul>" + items + "</ul>"),
                "text": text,
            },
        })
        return bool(result.get("ok"))
    except Exception as e:  # pragma: no cover - notification must never break the sweep
        logger.warning("[match_sweep] match_alert failed: %s", type(e).__name__)
        return False
