"""Learning §6.6 step 8 — the get-noticed payoff: a LinkedIn post for VERIFIED work.

Two responsibilities, both gated by the same integrity rule the whole loop rests
on — *never post unverified work*:

  * `draft_completion_post(user_email, path_id, task_ref)` — refuses unless the
    task's LATEST verification in ``learning_progress`` is ``verified``, then
    builds an achievement grounded ONLY in the verified task (skill proven + what
    was built + the repo link, if we created one) and drafts it through
    ``post_drafting_service.draft_post`` — which already applies the post bandit
    and the cringe guardrail. Returns {draft_id, variants}.

  * `publish_completion_post(user_email, draft_id, variant_index, mode)` —
    ``mode`` in {"manual","auto"}. Manual returns the chosen variant for the user
    to post by hand. Auto posts via the consented ``linkedin_connect_service.
    create_post`` (scope ``w_member_social``) IF connected — but NEVER as a
    surprise: it SCHEDULES the post 30 minutes out with a cancel affordance
    (``scheduled_posts`` + a cancel path) and re-checks the task is still verified
    before it actually fires. Not connected → falls back to the draft.

Domain-local connect services live under ``services/resume/services/`` and are
imported LAZILY; when absent, auto degrades to a manual draft rather than failing.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from utils.db_connect import DBConnect

logger = logging.getLogger(__name__)

HOLD_MINUTES = 30  # auto-post hold window — cancellable, never a surprise post


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _db():
    return DBConnect().get_db()


def _now():
    return datetime.now(timezone.utc)


def _iso(dt) -> Optional[str]:
    return dt.isoformat() if hasattr(dt, "isoformat") else dt


def _linkedin():
    """Lazy handle to the domain-local LinkedIn connect service (or None)."""
    try:
        from services import linkedin_connect_service as li  # domain-local (resume)
        return li
    except Exception:  # pragma: no cover
        return None


def _find_task(path: Dict, task_ref: str) -> Optional[Dict]:
    for t in path.get("tasks") or []:
        if t.get("task_ref") == task_ref:
            return t
    for m in path.get("modules") or []:
        if m.get("task_ref") == task_ref:
            return {
                "task_ref": task_ref,
                "brief": m.get("objective") or m.get("title") or "",
                "proof_type": m.get("proof_type"),
                "acceptance_criteria": m.get("acceptance_criteria") or [],
            }
    return None


def _verification_status(prog: Dict, task_ref: str) -> Dict:
    return (prog.get("verifications") or {}).get(task_ref) or {}


def _require_verified(user_email: str, path_id: str, task_ref: str) -> Tuple[Dict, Dict, Dict, Dict]:
    """Load (path, task, progress, verification) only if the task is verified.
    Raises ValueError otherwise — the backstop that keeps unverified work offline."""
    path = _db().learning_paths.find_one({"user_email": user_email, "path_id": path_id})
    if not path:
        raise ValueError("Learning path not found")
    task = _find_task(path, task_ref)
    if not task:
        raise ValueError("Task not found on this path")
    prog = _db().learning_progress.find_one({"user_email": user_email, "path_id": path_id}) or {}
    v = _verification_status(prog, task_ref)
    if str(v.get("status") or "").lower() != "verified":
        raise ValueError("This task isn't verified yet — only verified, proven work can be posted.")
    return path, task, prog, v


def is_task_verified(user_email: str, path_id: str, task_ref: str) -> bool:
    """Fast, DB-only check for route-level prechecks (reject unverified BEFORE
    spending on a draft). Never raises — a missing path/task/verification is False."""
    try:
        prog = _db().learning_progress.find_one(
            {"user_email": user_email, "path_id": path_id}, {"verifications": 1}
        ) or {}
    except Exception:  # pragma: no cover
        return False
    return str(_verification_status(prog, task_ref).get("status") or "").lower() == "verified"


def _build_achievement(gap: str, brief: str, criteria: List[str], repo: Optional[str], v: Dict) -> str:
    """Grounded, factual achievement string for the drafter. States only what the
    verification actually established; the drafter's own integrity rules + the
    non-"won" relation keep it from being inflated."""
    parts: List[str] = []
    if gap:
        parts.append(f"I finished and independently VERIFIED a hands-on build that proves the skill \"{gap}\".")
    else:
        parts.append("I finished and independently VERIFIED a hands-on build.")
    if brief:
        parts.append(f"What I built: {brief}.")
    if criteria:
        parts.append("It was checked against concrete acceptance criteria: " + "; ".join(criteria[:4]) + ".")
    if repo:
        parts.append(f"Code: {repo}.")
    det = str((v or {}).get("details") or "").strip()
    if det:
        parts.append(f"Verification note: {det}")
    return " ".join(parts)


def _variant_text(variant: Dict) -> str:
    text = str((variant or {}).get("text") or "").strip()
    tags = (variant or {}).get("hashtags") or []
    tagline = " ".join(
        (t if str(t).startswith("#") else f"#{t}") for t in tags if str(t).strip()
    )
    if tagline and tagline not in text:
        text = (text + "\n\n" + tagline).strip()
    return text


# ---------------------------------------------------------------------------
# Draft (verified-only)
# ---------------------------------------------------------------------------
def draft_completion_post(user_email: str, path_id: str, task_ref: str) -> Dict:
    """Draft a completion post for a VERIFIED task. Refuses otherwise.

    Returns {draft_id, variants, grounded_in}. Delegates generation to
    post_drafting_service (bandit + cringe filter already applied)."""
    path, task, prog, v = _require_verified(user_email, path_id, task_ref)
    gap = str(path.get("topic") or (path.get("differentiator") or {}).get("gap") or "").strip()
    brief = str(task.get("brief") or "").strip()
    criteria = [str(c).strip() for c in (task.get("acceptance_criteria") or []) if str(c).strip()]
    repo = ((prog.get("task_repos") or {}).get(task_ref) or {}).get("repo_url")

    achievement = _build_achievement(gap, brief, criteria, repo, v)
    payload = {
        "user_email": user_email,
        "audience": "general",
        # NOT "won": a completed learning build is framed around what was built,
        # never as a competition win (post_drafting_service enforces this).
        "relation": "built",
        "achievement": achievement,
    }
    try:
        from services import post_drafting_service
    except Exception as e:  # pragma: no cover
        raise RuntimeError("Post drafting is unavailable in this deployment") from e

    res = post_drafting_service.draft_post(payload) or {}
    draft_id = res.get("draft_id")
    if draft_id:
        try:
            _db().post_drafts.update_one(
                {"draft_id": draft_id, "user_email": user_email},
                {"$set": {
                    "kind": "completion",
                    "path_id": path_id,
                    "task_ref": task_ref,
                    "grounded_repo_url": repo,
                    "verified_at": v.get("at"),
                }},
            )
        except Exception as e:  # pragma: no cover
            logger.warning("publish: could not tag completion draft %s: %s", draft_id, e)

    return {
        "draft_id": draft_id,
        "variants": res.get("variants") or [],
        "grounded_in": {"path_id": path_id, "task_ref": task_ref, "skill": gap, "repo_url": repo},
    }


# ---------------------------------------------------------------------------
# Publish (manual now / auto held 30 min, cancellable)
# ---------------------------------------------------------------------------
def publish_completion_post(user_email: str, draft_id: str, variant_index: int,
                            mode: str = "manual") -> Dict:
    """Publish a chosen variant of a completion draft.

    manual → returns the variant for the user to post themselves.
    auto   → schedules it 30 min out (cancellable) via the consented LinkedIn
             connection; if LinkedIn isn't connected, falls back to the draft.
    Re-checks that the underlying task is STILL verified before doing anything."""
    mode = (mode or "manual").strip().lower()
    if mode not in ("manual", "auto"):
        mode = "manual"

    draft = _db().post_drafts.find_one({"draft_id": draft_id, "user_email": user_email})
    if not draft:
        raise ValueError("Draft not found")
    variants = draft.get("variants") or []
    try:
        idx = int(variant_index)
    except (TypeError, ValueError):
        idx = 0
    if idx < 0 or idx >= len(variants):
        raise ValueError("variant_index out of range")
    text = _variant_text(variants[idx] or {})
    if not text:
        raise ValueError("Selected variant has no text to post")
    hashtags = (variants[idx] or {}).get("hashtags") or []

    # Backstop: never publish work that isn't (still) verified.
    path_id, task_ref = draft.get("path_id"), draft.get("task_ref")
    if path_id and task_ref:
        prog = _db().learning_progress.find_one({"user_email": user_email, "path_id": path_id}) or {}
        if str(_verification_status(prog, task_ref).get("status") or "").lower() != "verified":
            raise ValueError("This task is no longer verified — refusing to post.")

    if mode == "manual":
        _db().post_drafts.update_one(
            {"draft_id": draft_id, "user_email": user_email},
            {"$set": {"status": "approved_manual", "approved_variant_index": idx, "approved_at": _now()}},
        )
        return {
            "mode": "manual", "status": "ready_to_post", "draft_id": draft_id,
            "variant_index": idx, "text": text, "hashtags": hashtags,
            "message": "Review and post this yourself, or connect LinkedIn to auto-post.",
        }

    # mode == "auto"
    li = _linkedin()
    connected = False
    if li is not None:
        try:
            connected = bool((li.get_status(user_email) or {}).get("connected"))
        except Exception:  # pragma: no cover
            connected = False
    if not connected:
        _db().post_drafts.update_one(
            {"draft_id": draft_id, "user_email": user_email},
            {"$set": {"status": "auto_needs_linkedin", "approved_variant_index": idx}},
        )
        return {
            "mode": "auto", "status": "linkedin_not_connected", "draft_id": draft_id,
            "variant_index": idx, "text": text, "hashtags": hashtags,
            "message": "Connect LinkedIn to auto-post — until then, here's the draft to post yourself.",
        }

    # Connected: hold 30 min with a cancel affordance — never a surprise post.
    schedule_id = str(uuid.uuid4())
    scheduled_for = _now() + timedelta(minutes=HOLD_MINUTES)
    _db().scheduled_posts.insert_one({
        "schedule_id": schedule_id, "user_email": user_email, "draft_id": draft_id,
        "variant_index": idx, "path_id": path_id, "task_ref": task_ref,
        "text": text, "status": "scheduled",
        "scheduled_for": scheduled_for, "created_at": _now(),
    })
    _db().post_drafts.update_one(
        {"draft_id": draft_id, "user_email": user_email},
        {"$set": {"status": "auto_scheduled", "schedule_id": schedule_id, "approved_variant_index": idx}},
    )
    return {
        "mode": "auto", "status": "scheduled", "draft_id": draft_id, "variant_index": idx,
        "text": text, "schedule_id": schedule_id, "scheduled_for": _iso(scheduled_for),
        "hold_minutes": HOLD_MINUTES,
        "cancel_path": f"/api/resume/learning/posts/{schedule_id}/cancel",
        "message": f"Scheduled to post in {HOLD_MINUTES} minutes — cancel any time before then.",
    }


def cancel_scheduled_post(user_email: str, schedule_id: str) -> Dict:
    """Cancel a pending auto-post inside its hold window."""
    doc = _db().scheduled_posts.find_one_and_update(
        {"schedule_id": schedule_id, "user_email": user_email, "status": "scheduled"},
        {"$set": {"status": "cancelled", "cancelled_at": _now()}},
    )
    if not doc:
        raise ValueError("No pending scheduled post to cancel")
    return {"schedule_id": schedule_id, "status": "cancelled"}


def send_scheduled_post(schedule_id: str) -> Dict:
    """Fire a due, still-scheduled auto-post. Intended for a worker/scheduler after
    the hold window. Re-checks cancellation + verification before it posts —
    the send is idempotent (only a 'scheduled' doc is ever posted)."""
    doc = _db().scheduled_posts.find_one({"schedule_id": schedule_id})
    if not doc or doc.get("status") != "scheduled":
        return {"status": (doc or {}).get("status", "missing"), "posted": False}
    user_email = doc.get("user_email")

    path_id, task_ref = doc.get("path_id"), doc.get("task_ref")
    if path_id and task_ref:
        prog = _db().learning_progress.find_one({"user_email": user_email, "path_id": path_id}) or {}
        if str(_verification_status(prog, task_ref).get("status") or "").lower() != "verified":
            _db().scheduled_posts.update_one(
                {"schedule_id": schedule_id},
                {"$set": {"status": "aborted_unverified", "aborted_at": _now()}},
            )
            return {"status": "aborted_unverified", "posted": False}

    li = _linkedin()
    if li is None:
        return {"status": "linkedin_unavailable", "posted": False}
    try:
        result = li.create_post(user_email, doc.get("text") or "") or {}
    except Exception as e:
        _db().scheduled_posts.update_one(
            {"schedule_id": schedule_id},
            {"$set": {"status": "failed", "error": str(e)[:200], "failed_at": _now()}},
        )
        return {"status": "failed", "posted": False, "error": str(e)[:200]}

    _db().scheduled_posts.update_one(
        {"schedule_id": schedule_id},
        {"$set": {"status": "posted", "posted_at": _now(),
                  "post_url": result.get("post_url"), "post_id": result.get("post_id")}},
    )
    if doc.get("draft_id"):
        _db().post_drafts.update_one(
            {"draft_id": doc["draft_id"]},
            {"$set": {"status": "posted", "post_url": result.get("post_url")}},
        )
    return {"status": "posted", "posted": True, "post_url": result.get("post_url")}
