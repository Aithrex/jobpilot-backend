"""Learning Hub — Submission Service.

Ties Challenge -> Verification -> Progress together for a single day submission.
Server-authoritative: a day can only be completed when it is currently `current`
(else 409). Completion is written once (atomic guard on completed_at == null) so a
double-submit can never double-award XP.

Today verification is the NoOp (manual) validator (mark-complete via UI). When a
future GitHub/CI validator is registered for a challenge_type, this same flow
runs it and only completes the day on `passed` — no other code changes.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Dict, Optional

from utils.db_connect import DBConnect
from services import sprint_curriculum as curriculum
from services import sprint_challenge_service as challenge_service
from services import sprint_progress_service as progress_service
from services import sprint_verification as verification


logger = logging.getLogger(__name__)


class SubmissionError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _db():
    return DBConnect().get_db()


def _now():
    return datetime.now(timezone.utc)


def precheck(user_email: str, ctx: Dict, day_key: str, tz_offset_minutes: int = 0) -> None:
    """Fast, network-free validation before queueing a verification job.

    Verification itself is slow (repo download + LLM judge) so it runs as an
    async job — but state errors ("already completed", "locked") should surface
    immediately at the route instead of after a poll cycle.
    """
    path_doc = challenge_service.get_path_doc(ctx["path_key"])
    if not path_doc or path_doc.get("status") != "ready":
        raise SubmissionError("Sprint path is not ready yet.", 409)
    if not any(d["key"] == day_key for d in path_doc.get("days", [])):
        raise SubmissionError("Unknown challenge.", 404)

    progress = challenge_service.get_or_init_progress(user_email, ctx)
    states = {d["key"]: d for d in challenge_service.compute_day_states(
        ctx["season_id"], progress, tz_offset_minutes=tz_offset_minutes)}
    st = states.get(day_key)
    if not st:
        raise SubmissionError("Unknown challenge.", 404)
    if st["state"] == "completed":
        raise SubmissionError("This challenge is already completed.", 409)
    if st["state"] == "coming_soon":
        raise SubmissionError("This challenge will be available in a future update.", 409)
    if st["state"] != "current":
        raise SubmissionError("This challenge is locked. Complete the previous day first.", 409)


def verify_job(payload: Dict) -> Dict:
    """Async entry point for job_type='sprint_verify' (see resume_service)."""
    return submit(
        payload.get("user_email", ""),
        payload.get("ctx") or {},
        payload.get("day_key", ""),
        note=payload.get("note", ""),
        evidence=payload.get("evidence") or {},
        tz_offset_minutes=int(payload.get("tz_offset_minutes") or 0),
    )


def submit(user_email: str, ctx: Dict, day_key: str,
           note: str = "", evidence: Optional[Dict] = None,
           tz_offset_minutes: int = 0) -> Dict:
    """Verify + complete a day. Returns the reward + refreshed view."""
    path_doc = challenge_service.get_path_doc(ctx["path_key"])
    if not path_doc or path_doc.get("status") != "ready":
        raise SubmissionError("Sprint path is not ready yet.", 409)

    day_doc = next((d for d in path_doc.get("days", []) if d["key"] == day_key), None)
    if not day_doc:
        raise SubmissionError("Unknown challenge.", 404)

    progress = challenge_service.get_or_init_progress(user_email, ctx)
    states = {d["key"]: d for d in challenge_service.compute_day_states(
        ctx["season_id"], progress, tz_offset_minutes=tz_offset_minutes)}
    st = states.get(day_key)
    if not st:
        raise SubmissionError("Unknown challenge.", 404)
    if st["state"] == "completed":
        raise SubmissionError("This challenge is already completed.", 409)
    if st["state"] == "coming_soon":
        raise SubmissionError("This challenge will be available in a future update.", 409)
    if st["state"] != "current":
        raise SubmissionError("This challenge is locked. Complete the previous day first.", 409)

    challenge_type = (day_doc.get("ticket") or {}).get("challenge_type", day_key)

    # Verification (NoOp today; pluggable per challenge_type).
    vctx = verification.VerificationContext(
        user_email=user_email,
        path_key=ctx["path_key"],
        day_key=day_key,
        challenge_type=challenge_type,
        submission_note=note or "",
        evidence=evidence or {},
    )
    result = verification.verify(vctx)

    submission_id = str(uuid.uuid4())
    submission_doc = {
        "submission_id": submission_id,
        "user_email": user_email,
        "path_key": ctx["path_key"],
        "day": day_doc["day"],
        "day_key": day_key,
        "challenge_type": challenge_type,
        "submitted_at": _now(),
        "note": note or "",
        "verification": result.to_doc(),
        "is_preview": ctx.get("is_preview", False),
    }

    if not result.passed:
        _db().sprint_submissions.insert_one(submission_doc)
        # Surface the grader's specific feedback — that message IS the teaching
        # moment. The day stays `current`, so retrying costs nothing.
        ev = result.evidence or {}
        return {
            "verified": False,
            "verification": {**result.to_doc(), "checked_at": None},
            "message": ev.get("details") or "Submission recorded but verification did not pass.",
            "criteria_results": ev.get("criteria_results") or [],
        }

    # Atomic completion guard — only set completed_at if still null.
    upd = _db().sprint_progress.update_one(
        {"user_email": user_email, "path_key": ctx["path_key"],
         f"day_status.{day_key}.completed_at": {"$in": [None]}},
        {"$set": {f"day_status.{day_key}": {
            "day": day_doc["day"], "completed_at": _now(), "submission_id": submission_id,
        }}},
    )
    if upd.modified_count == 0:
        # Either not present yet, or a race set it. Upsert the field if absent.
        _db().sprint_progress.update_one(
            {"user_email": user_email, "path_key": ctx["path_key"],
             f"day_status.{day_key}": {"$exists": False}},
            {"$set": {f"day_status.{day_key}": {
                "day": day_doc["day"], "completed_at": _now(), "submission_id": submission_id,
            }}},
        )

    submission_doc["xp_awarded"] = curriculum.xp_for_day(ctx["season_id"], day_key)
    _db().sprint_submissions.insert_one(submission_doc)

    fresh = _db().sprint_progress.find_one({"user_email": user_email, "path_key": ctx["path_key"]})
    reward = progress_service.on_day_completed(fresh, day_key, submission_id)

    view = challenge_service.build_path_view(user_email, ctx, tz_offset_minutes=tz_offset_minutes)
    completed_day = next((d for d in (view or {}).get("days", []) if d["key"] == day_key), None)

    return {
        "verified": True,
        "verification": result.to_doc(),
        "day": completed_day,
        "reward": reward,
        "xp": reward["xp"],
        "level": reward["level"],
        "xp_gained": reward["xp_gained"],
        "leveled_up": reward["leveled_up"],
        "achievement_unlocked": reward["achievement_unlocked"],
        "path": view,
    }
