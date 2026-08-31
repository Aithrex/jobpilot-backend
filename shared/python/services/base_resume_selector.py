"""Pick the best base resume for a single tailor job.

Per-job override — NEVER writes ``users.active_resume_id``. Ranking uses the
canonical ``fit_score.score`` (fit-v2.0); there is no second formula and no
LLM call. One-resume users short-circuit to that asset (mode ``single``).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from services import resume_access

logger = logging.getLogger(__name__)

# Public sidecar on the job result / tailoring_records. Keep this small.
_MAX_PUBLIC_CANDIDATES = 8

_VALID_MODES = frozenset({"auto", "active"})


@dataclass
class BaseSelectionResult:
    asset: Optional[Dict[str, Any]] = None
    resume_id: str = ""
    filename: str = ""
    mode: str = "single"  # auto | pinned | active | single
    match_score: Optional[int] = None
    reason: str = ""
    candidates: List[Dict[str, Any]] = field(default_factory=list)
    error: Optional[str] = None  # not_found | not_parsed | none_parsed

    def public_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "resume_id": self.resume_id,
            "filename": self.filename,
            "mode": self.mode,
            "reason": self.reason,
        }
        if self.match_score is not None:
            out["match_score"] = int(self.match_score)
        if self.candidates:
            out["candidates"] = self.candidates[:_MAX_PUBLIC_CANDIDATES]
        return out


def _filename(asset: Optional[Dict[str, Any]]) -> str:
    if not asset:
        return ""
    name = (asset.get("filename") or "").strip()
    return name


def _ts(asset: Dict[str, Any]) -> datetime:
    v = asset.get("parsed_at") or asset.get("created_at")
    if isinstance(v, datetime):
        if v.tzinfo is None:
            return v.replace(tzinfo=timezone.utc)
        return v
    return datetime.min.replace(tzinfo=timezone.utc)


def _active_id(user_email: str) -> str:
    return resume_access.get_active_resume_id(user_email)


def _candidate(
    asset: Dict[str, Any],
    score: int,
    *,
    selected: bool,
    active_id: str,
) -> Dict[str, Any]:
    rid = str(asset.get("resume_id") or "")
    return {
        "resume_id": rid,
        "filename": _filename(asset) or "Untitled resume",
        "match_score": int(score),
        "is_selected": bool(selected),
        "is_active": rid == active_id and bool(rid),
    }


def _score_asset(asset: Dict[str, Any], job: Dict[str, Any]) -> Optional[int]:
    structured = asset.get("structured")
    if not isinstance(structured, dict) or not structured:
        return None
    try:
        from services.fit_score import score as fit_score

        result = fit_score(structured, job)
        raw = result.get("score")
        if raw is None:
            return None
        return int(raw)
    except Exception:
        logger.warning(
            "base_resume_selector: fit_score failed for asset %s",
            asset.get("resume_id"),
            exc_info=True,
        )
        return None


def _reason(mode: str, filename: str, job_title: str, score: Optional[int], n: int) -> str:
    label = filename or "your resume"
    title = (job_title or "").strip() or "this role"
    if mode == "single":
        return f"Using {label} — this is your only parsed resume."
    if mode == "pinned":
        if score is None:
            return f"Using {label} (the resume you chose)."
        return f"Using {label} (the resume you chose). Fit for {title}: {score}/100."
    if mode == "active":
        if score is None:
            return f"Using {label} (your active resume)."
        return f"Using {label} (your active resume). Fit for {title}: {score}/100."
    if score is None:
        return f"Using {label} for {title}."
    if n <= 1:
        return f"Using {label}."
    return f"Best match for {title} ({score}/100). Tailored from {label}."


def _pick_active_or_newest(parsed: List[Dict[str, Any]], active_id: str) -> Optional[Dict[str, Any]]:
    if not parsed:
        return None
    if active_id:
        for a in parsed:
            if a.get("resume_id") == active_id:
                return a
    return max(parsed, key=_ts)


def select_base_resume(
    user_email: str,
    *,
    jd_text: str = "",
    jd_analysis: Optional[Dict[str, Any]] = None,
    resume_id: Optional[str] = None,
    selection_mode: str = "auto",
) -> BaseSelectionResult:
    """Resolve which parsed asset this tailor job should use.

    Never writes the active pointer. ``resume_id`` is always owner-scoped.
    """
    mode = (selection_mode or "auto").strip().lower() or "auto"
    if mode not in _VALID_MODES:
        mode = "auto"
    ja = jd_analysis if isinstance(jd_analysis, dict) else {}
    job_title = str(ja.get("job_title") or "")
    job = {
        "title": job_title,
        "description": jd_text or "",
        "location": str(ja.get("location") or ""),
    }
    active_id = _active_id(user_email)
    pinned = (resume_id or "").strip()

    if pinned:
        asset = resume_access.get_owned_asset(user_email, pinned)
        if not asset:
            return BaseSelectionResult(error="not_found", mode="pinned")
        if asset.get("parse_status") != "parsed" or not asset.get("structured"):
            return BaseSelectionResult(
                error="not_parsed",
                mode="pinned",
                resume_id=str(asset.get("resume_id") or pinned),
                filename=_filename(asset),
            )
        shaped = resume_access.shape_asset(asset) or asset
        score = _score_asset(shaped, job)
        return BaseSelectionResult(
            asset=shaped,
            resume_id=str(shaped.get("resume_id") or pinned),
            filename=_filename(shaped),
            mode="pinned",
            match_score=score,
            reason=_reason("pinned", _filename(shaped), job_title, score, 1),
        )

    parsed = resume_access.list_parsed_assets(user_email)
    if not parsed:
        return BaseSelectionResult(error="none_parsed")

    if len(parsed) == 1 or mode == "active":
        chosen = (
            parsed[0]
            if len(parsed) == 1
            else _pick_active_or_newest(parsed, active_id)
        )
        if chosen is None:
            return BaseSelectionResult(error="none_parsed")
        result_mode = "single" if len(parsed) == 1 else "active"
        score = _score_asset(chosen, job)
        candidates: List[Dict[str, Any]] = []
        if len(parsed) > 1:
            # Still surface the field of options so the UI can explain the
            # opt-out, but don't pretend we ranked when the user asked for active.
            scored_rows = []
            for a in parsed:
                s = _score_asset(a, job)
                scored_rows.append((a, 0 if s is None else s))
            scored_rows.sort(
                key=lambda row: (
                    row[1],
                    row[0].get("resume_id") == active_id,
                    _ts(row[0]),
                ),
                reverse=True,
            )
            candidates = [
                _candidate(a, s, selected=a.get("resume_id") == chosen.get("resume_id"), active_id=active_id)
                for a, s in scored_rows
            ]
        return BaseSelectionResult(
            asset=chosen,
            resume_id=str(chosen.get("resume_id") or ""),
            filename=_filename(chosen),
            mode=result_mode,
            match_score=score,
            reason=_reason(result_mode, _filename(chosen), job_title, score, len(parsed)),
            candidates=candidates,
        )

    # N≥2, auto: rank with fit-v2.0. Fail open to active/newest if every score blows up.
    scored: List[tuple] = []
    any_ok = False
    for asset in parsed:
        s = _score_asset(asset, job)
        if s is None:
            scored.append((asset, -1))
        else:
            any_ok = True
            scored.append((asset, s))

    if not any_ok:
        logger.warning(
            "base_resume_selector: all fit_score calls failed for %s — using active/newest",
            user_email,
        )
        chosen = _pick_active_or_newest(parsed, active_id)
        if chosen is None:
            return BaseSelectionResult(error="none_parsed")
        return BaseSelectionResult(
            asset=chosen,
            resume_id=str(chosen.get("resume_id") or ""),
            filename=_filename(chosen),
            mode="active",
            reason=_reason("active", _filename(chosen), job_title, None, len(parsed)),
        )

    def _rank_key(row: tuple) -> tuple:
        asset, s = row
        rid = str(asset.get("resume_id") or "")
        return (s, 1 if rid == active_id else 0, _ts(asset))

    scored.sort(key=_rank_key, reverse=True)
    chosen, best_score = scored[0]
    if best_score < 0:
        best_score = 0
    candidates = [
        _candidate(
            a,
            max(s, 0),
            selected=a.get("resume_id") == chosen.get("resume_id"),
            active_id=active_id,
        )
        for a, s in scored
    ]
    return BaseSelectionResult(
        asset=chosen,
        resume_id=str(chosen.get("resume_id") or ""),
        filename=_filename(chosen),
        mode="auto",
        match_score=int(best_score),
        reason=_reason("auto", _filename(chosen), job_title, int(best_score), len(parsed)),
        candidates=candidates,
    )
