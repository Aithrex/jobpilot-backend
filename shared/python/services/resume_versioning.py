"""Resume version helpers for tailoring_records.

Design: each tailoring_records doc has a `versions[]` array. Every regeneration
or edit appends a new version; downloads cache rendered files inside each
version's `files: {pdf, docx}` sub-doc. Content-hash keyed so that re-saves of
unchanged content are idempotent (no bloat).

Schema (per version):
  {
    version_id: UUID,
    version_number: 1..N,
    source: "initial" | "regenerated" | "edited",
    parent_version_id: UUID | None,
    content_hash: str,           # 16 hex chars of sha256(canonical JSON)
    tailored_resume: dict,
    ats_scores: dict | None,
    user_feedback: str | None,
    created_at: datetime,
    files: { pdf: {s3_key, size_bytes, rendered_at, filename} | None, docx: ... },
  }
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple


def _iso_utc(value: Any) -> Any:
    """Serialize a datetime as ISO with explicit UTC offset.

    Naive datetimes (produced by datetime.utcnow()) are treated as UTC so the
    resulting string has a +00:00 suffix. Without that, browsers parse the
    naive ISO as local time and the wall-clock day shifts.
    """
    if hasattr(value, "isoformat"):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    return value


def canonical_content_hash(tailored: Dict[str, Any]) -> str:
    """Stable 16-char hex hash of a tailored resume's content.

    Uses sorted-keys JSON so two logically equal resumes always hash the same
    regardless of dict insertion order.
    """
    canonical = json.dumps(tailored or {}, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def build_initial_version(
    tailored: Dict[str, Any],
    *,
    source: str = "initial",
    ats_scores: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    return {
        "version_id": str(uuid.uuid4()),
        "version_number": 1,
        "source": source,
        "parent_version_id": None,
        "content_hash": canonical_content_hash(tailored),
        "tailored_resume": tailored,
        "ats_scores": ats_scores,
        "user_feedback": None,
        "created_at": datetime.utcnow(),
        "files": {},
    }


def ensure_versions(record: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], bool]:
    """Lazy-migrate a legacy record that lacks `versions[]`.

    Returns (versions, migrated). If `migrated` is True, the caller should
    persist the `versions` array (and `current_version_id`) back to Mongo.
    """
    versions = record.get("versions")
    if versions:
        return versions, False

    tailored = record.get("tailored_resume") or {}
    ats = record.get("ats_scores")
    seed = build_initial_version(tailored, source="initial", ats_scores=ats)
    return [seed], True


def find_version(versions: List[Dict[str, Any]], version_id: str) -> Optional[Dict[str, Any]]:
    for v in versions:
        if v.get("version_id") == version_id:
            return v
    return None


def latest_version(versions: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not versions:
        return None
    return max(versions, key=lambda v: v.get("version_number", 0))


def append_version(
    versions: List[Dict[str, Any]],
    tailored: Dict[str, Any],
    *,
    source: str,
    parent_version_id: Optional[str] = None,
    ats_scores: Optional[Dict[str, Any]] = None,
    user_feedback: Optional[str] = None,
) -> Tuple[Dict[str, Any], bool]:
    """Append a version. If content_hash matches the latest version, reuse it.

    Returns (version, created). `created=False` means the content was identical
    to the latest version and no new entry was added.
    """
    new_hash = canonical_content_hash(tailored)
    last = latest_version(versions)
    if last and last.get("content_hash") == new_hash:
        # Same content — treat as a no-op. Optionally update ats_scores on the
        # existing version if the caller supplied fresher ones.
        if ats_scores and not last.get("ats_scores"):
            last["ats_scores"] = ats_scores
        return last, False

    next_number = (last.get("version_number") if last else 0) + 1
    version = {
        "version_id": str(uuid.uuid4()),
        "version_number": next_number,
        "source": source,
        "parent_version_id": parent_version_id or (last.get("version_id") if last else None),
        "content_hash": new_hash,
        "tailored_resume": tailored,
        "ats_scores": ats_scores,
        "user_feedback": user_feedback,
        "created_at": datetime.utcnow(),
        "files": {},
    }
    versions.append(version)
    return version, True


def serialize_record(record: Dict[str, Any], include_full: bool = False) -> Dict[str, Any]:
    """Serialize a record for the API, converting datetimes to ISO strings.

    If include_full=False, strip heavy `tailored_resume` bodies from each
    version to keep the list endpoint light.
    """
    out = dict(record)
    out.pop("_id", None)

    for key in ("created_at", "updated_at", "ats_scored_at"):
        if key in out:
            out[key] = _iso_utc(out.get(key))

    versions = out.get("versions") or []
    serialized = []
    for v in versions:
        vcopy = dict(v)
        for k in ("created_at",):
            if k in vcopy:
                vcopy[k] = _iso_utc(vcopy.get(k))
        files = vcopy.get("files") or {}
        for f in files.values():
            if isinstance(f, dict) and "rendered_at" in f:
                f["rendered_at"] = _iso_utc(f.get("rendered_at"))
        if not include_full:
            vcopy.pop("tailored_resume", None)
        serialized.append(vcopy)
    out["versions"] = serialized

    # Application tracker — convert datetimes
    app = out.get("application")
    if isinstance(app, dict):
        for k in ("applied_at", "next_action_date", "updated_at"):
            if k in app:
                app[k] = _iso_utc(app.get(k))
        dates = app.get("interview_dates")
        if isinstance(dates, list):
            app["interview_dates"] = [_iso_utc(d) for d in dates]

    # Interview prep — expose metadata in list view, full content in detail view
    prep = out.get("interview_prep")
    if isinstance(prep, dict):
        if "generated_at" in prep:
            prep = {**prep, "generated_at": _iso_utc(prep.get("generated_at"))}
        if not include_full:
            # List response: keep only the "has prep + when" metadata
            out["interview_prep"] = {
                "generated_at": prep.get("generated_at"),
                "grounded_version_id": prep.get("grounded_version_id"),
            }
        else:
            out["interview_prep"] = prep

    # Drop the top-level tailored_resume from list responses; versions[0] has it.
    if not include_full:
        out.pop("tailored_resume", None)
        out.pop("jd_text", None)
    return out
