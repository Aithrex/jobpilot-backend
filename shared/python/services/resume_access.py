"""Resume asset access layer — Resume Platform v3 (spec sections 1.1 / 1.2 / 2).

Canonical storage is the ``resume_assets`` collection: ONE document per
uploaded file, keyed by an immutable ``resume_id`` (uuid4 hex). The user's
current resume is a single atomic pointer, ``users.active_resume_id``.

ALL matching and tailoring resolve structured content ONLY through
``get_active_structured(user_email)`` — ``resume_parser.get_structured_resume``
delegates here so its 35+ call sites keep working unchanged.

Write order for uploads (orphan rule, spec section 2):
  1. ``create_pending_asset`` (Mongo insert FIRST, ``parse_status='pending'``);
  2. S3 put to ``resume-assets/{resume_id}/original``;
  3. ``attach_object_key`` ($set). A failed S3 write marks the asset failed;
     an asset stuck pending with no object_key >24h is swept by retention.

Active-pointer rules (spec 1.2):
  * The upload route stamps ``users.pending_active_resume_id = resume_id``.
  * The parse worker promotes it with a guarded find_one_and_update — a
    stale/failed parse can NEVER overwrite a pointer the user changed
    meanwhile (the stamp is gone or points at a newer upload).
  * ``PUT /api/resume/active`` $sets the pointer unconditionally after an
    ownership + parse_status check (and clears any pending stamp).
  * Deleting the active asset atomically re-points to the newest remaining
    parsed asset, or unsets the pointer.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from utils.db_connect import DBConnect
from utils.security import InputSanitizer

logger = logging.getLogger(__name__)

# S3 key scheme for base uploads (spec section 2): no names, emails, original
# filenames, job titles, or timestamps in NEW keys.
ASSET_KEY_TEMPLATE = "resume-assets/{resume_id}/original"

# raw_text stored on the asset is capped (spec 1.1).
RAW_TEXT_CAP = 60000


def _db():
    return DBConnect().get_db()


def _assets():
    return _db().resume_assets


def asset_object_key(resume_id: str) -> str:
    return ASSET_KEY_TEMPLATE.format(resume_id=resume_id)


def _safe_id(resume_id: Any) -> Optional[str]:
    """Validate a resume_id capability before it enters a Mongo filter."""
    if not isinstance(resume_id, str):
        return None
    rid = resume_id.strip()
    if not rid or len(rid) > 64 or not all(c.isalnum() or c == "-" for c in rid):
        return None
    return rid


def _safe_email(user_email: Any) -> Optional[str]:
    """Equality-position guard: the value must be a plain string (never a
    dict that could smuggle a Mongo operator)."""
    if not isinstance(user_email, str) or not user_email.strip():
        return None
    return user_email


# ---------------------------------------------------------------------------
# Asset lifecycle
# ---------------------------------------------------------------------------

def create_pending_asset(
    *,
    owner_type: str,
    user_email: Optional[str] = None,
    user_id: Optional[str] = None,
    guest_id: Optional[str] = None,
    quota_subject_id: Optional[str] = None,
    filename: str = "",
    mime_type: str = "",
    size_bytes: int = 0,
    content_hash: str = "",
    purge_after: Optional[datetime] = None,
) -> str:
    """Insert the asset doc (parse_status='pending') and return its resume_id.

    Mongo-insert-first: the doc exists BEFORE any S3 write so a failed write
    can be marked on it (never an untracked S3 orphan).
    """
    rid = uuid.uuid4().hex
    now = datetime.now(timezone.utc)
    _assets().insert_one({
        "resume_id": rid,
        "owner_type": "guest" if owner_type == "guest" else "user",
        "user_id": user_id or None,
        "user_email": user_email or None,
        "guest_id": guest_id or None,
        "quota_subject_id": quota_subject_id or None,  # server-only, never exposed
        "object_key": None,
        # Protected original filename: Mongo only — never in S3 keys or logs.
        "filename": InputSanitizer.sanitize_string(filename or "", max_length=255),
        "mime_type": str(mime_type or ""),
        "size_bytes": int(size_bytes or 0),
        "content_hash": str(content_hash or ""),
        "parse_status": "pending",
        "parse_error": None,
        "scan_status": "unscanned",  # explicit quarantine seam (no scanner yet)
        "structured": None,
        "raw_text": None,
        "claim_status": "unclaimed" if owner_type == "guest" else "n/a",
        "created_at": now,
        "parsed_at": None,
        "purge_after": purge_after,
    })
    return rid


def attach_object_key(resume_id: str, object_key: str) -> None:
    rid = _safe_id(resume_id)
    if not rid:
        return
    _assets().update_one({"resume_id": rid}, {"$set": {"object_key": str(object_key)}})


def mark_asset_parsing(resume_id: str) -> None:
    rid = _safe_id(resume_id)
    if not rid:
        return
    _assets().update_one(
        {"resume_id": rid, "parse_status": {"$in": ["pending", "failed", "parsing"]}},
        {"$set": {"parse_status": "parsing"}},
    )


def mark_asset_failed(resume_id: str, error: str) -> None:
    rid = _safe_id(resume_id)
    if not rid:
        return
    _assets().update_one(
        {"resume_id": rid},
        {"$set": {"parse_status": "failed", "parse_error": str(error)[:2000]}},
    )


def build_parsed_fields(
    structured: dict, raw_text: str, content_hash: str = ""
) -> Dict[str, Any]:
    """Compute the flat status fields stored alongside ``structured`` on the
    asset (same derivations the legacy user_resumes doc carried)."""
    from schemas.resume_schemas import flatten_skills, extract_job_titles
    from services.resume_parser import ResumeParser

    raw_text = raw_text or ""
    fields: Dict[str, Any] = {
        "structured": structured,
        "raw_text": raw_text[:RAW_TEXT_CAP],
        "raw_text_length": len(raw_text),
        "skills": flatten_skills(structured),
        "experience_years": ResumeParser._estimate_experience_years(structured),
        "job_titles": extract_job_titles(structured),
        "summary": structured.get("summary", ""),
        "education": [
            {
                "degree": edu.get("degree", ""),
                "institution": edu.get("institution", ""),
                "year": (
                    edu.get("dates", "").split("–")[-1].strip() if edu.get("dates") else ""
                ),
            }
            for edu in structured.get("education", [])
        ],
        "certifications": [
            c for c in (structured.get("certifications") or [])
            if isinstance(c, str) and c.strip()
        ],
        "parse_status": "parsed",
        "parse_error": None,
        "parsed_at": datetime.now(timezone.utc),
    }
    if content_hash:
        fields["content_hash"] = content_hash
    return fields


def save_parsed_fields(resume_id: str, fields: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """$set the parsed content INTO the owning asset doc (no delete/insert).

    Returns the updated asset doc, or None if the resume_id is unknown.
    """
    rid = _safe_id(resume_id)
    if not rid:
        return None
    from pymongo import ReturnDocument
    try:
        return _assets().find_one_and_update(
            {"resume_id": rid},
            {"$set": fields},
            return_document=ReturnDocument.AFTER,
        )
    except TypeError:
        # mongomock compatibility fallback (older signatures)
        _assets().update_one({"resume_id": rid}, {"$set": fields})
        return _assets().find_one({"resume_id": rid})


# ---------------------------------------------------------------------------
# Active-pointer management (users.active_resume_id)
# ---------------------------------------------------------------------------

def stamp_pending_active(user_email: str, resume_id: str) -> None:
    email = _safe_email(user_email)
    rid = _safe_id(resume_id)
    if not email or not rid:
        return
    _db().users.update_one({"email": email}, {"$set": {"pending_active_resume_id": rid}})


def clear_pending_active(user_email: str, resume_id: str) -> None:
    """Remove the pending stamp ONLY if it still points at this resume_id
    (a newer upload's stamp must survive)."""
    email = _safe_email(user_email)
    rid = _safe_id(resume_id)
    if not email or not rid:
        return
    _db().users.update_one(
        {"email": email, "pending_active_resume_id": rid},
        {"$unset": {"pending_active_resume_id": ""}},
    )


def promote_active(user_email: str, resume_id: str) -> bool:
    """Worker-side promotion after a successful parse (spec 1.2).

    Sets ``active_resume_id = resume_id`` ONLY if the pending stamp recorded
    at upload time still matches (stale-parse guard), else adopts the asset
    only when the user has no active resume at all.
    """
    email = _safe_email(user_email)
    rid = _safe_id(resume_id)
    if not email or not rid:
        return False
    users = _db().users
    res = users.find_one_and_update(
        {"email": email, "pending_active_resume_id": rid},
        {"$set": {"active_resume_id": rid}, "$unset": {"pending_active_resume_id": ""}},
    )
    if res is not None:
        return True
    res = users.find_one_and_update(
        {
            "email": email,
            "$or": [
                {"active_resume_id": None},
                {"active_resume_id": {"$exists": False}},
            ],
        },
        {"$set": {"active_resume_id": rid}},
    )
    return res is not None


def adopt_active_if_unset(user_email: str, resume_id: str) -> bool:
    """Set the pointer only when the user has none (no pending semantics)."""
    email = _safe_email(user_email)
    rid = _safe_id(resume_id)
    if not email or not rid:
        return False
    res = _db().users.find_one_and_update(
        {
            "email": email,
            "$or": [
                {"active_resume_id": None},
                {"active_resume_id": {"$exists": False}},
            ],
        },
        {"$set": {"active_resume_id": rid}},
    )
    return res is not None


def repoint_active_after_delete(user_email: str, deleted_resume_id: str) -> None:
    """After deleting an asset: if it was active, atomically re-point to the
    newest remaining parsed asset, or unset the pointer (spec 1.2 / 2)."""
    email = _safe_email(user_email)
    rid = _safe_id(deleted_resume_id)
    if not email or not rid:
        return
    newest = _assets().find_one(
        {"user_email": email, "parse_status": "parsed", "resume_id": {"$ne": rid}},
        sort=[("created_at", -1)],
    )
    if newest:
        _db().users.update_one(
            {"email": email, "active_resume_id": rid},
            {"$set": {"active_resume_id": newest["resume_id"]}},
        )
    else:
        _db().users.update_one(
            {"email": email, "active_resume_id": rid},
            {"$unset": {"active_resume_id": ""}},
        )


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def get_asset(resume_id: str) -> Optional[Dict[str, Any]]:
    rid = _safe_id(resume_id)
    if not rid:
        return None
    return _assets().find_one({"resume_id": rid})


def get_owned_asset(user_email: str, resume_id: str) -> Optional[Dict[str, Any]]:
    """Owner-scoped asset read. Unlike ``get_asset``, the resume_id ALONE never
    resolves: the owner is folded into the query, so an id that travelled through
    an executor payload (auto-apply's base-résumé reference) can never pull
    another user's résumé — the most sensitive artifact in the product."""
    email = _safe_email(user_email)
    rid = _safe_id(resume_id)
    if not email or not rid:
        return None
    return _assets().find_one({"resume_id": rid, "user_email": email})


def list_parsed_assets(user_email: str) -> list:
    """Owner-scoped parsed assets with structured JSON, newest-parsed first.

    Pending/failed/empty parses are excluded — they cannot be tailored or
    ranked against a JD. Never returns another user's assets.
    """
    email = _safe_email(user_email)
    if not email:
        return []
    cursor = _assets().find(
        {
            "user_email": email,
            "parse_status": "parsed",
            "structured": {"$ne": None},
        },
    ).sort("parsed_at", -1)
    shaped = []
    for asset in cursor:
        doc = shape_asset(asset)
        if doc:
            shaped.append(doc)
    return shaped


def get_active_resume_id(user_email: str) -> str:
    """Return users.active_resume_id or '' — never infers a fallback."""
    email = _safe_email(user_email)
    if not email:
        return ""
    user = _db().users.find_one({"email": email}, {"active_resume_id": 1}) or {}
    return str(user.get("active_resume_id") or "")


def get_active_asset(user_email: str) -> Optional[Dict[str, Any]]:
    """Resolve users.active_resume_id -> asset doc; falls back to the user's
    newest asset (any parse state) so a failed-parse upload can be re-parsed."""
    email = _safe_email(user_email)
    if not email:
        return None
    user = _db().users.find_one({"email": email}, {"active_resume_id": 1}) or {}
    rid = user.get("active_resume_id")
    if rid:
        asset = _assets().find_one({"resume_id": rid})
        if asset:
            return asset
    return _assets().find_one({"user_email": email}, sort=[("created_at", -1)])


def shape_asset(asset: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Return the asset in the legacy resume-doc shape callers expect
    (adds the ``s3_key`` alias, drops _id, normalizes the contact name)."""
    if not asset:
        return None
    doc = dict(asset)
    doc.pop("_id", None)
    doc.pop("quota_subject_id", None)  # server-only, never exposed
    doc.setdefault("s3_key", doc.get("object_key") or "")
    structured = doc.get("structured")
    if isinstance(structured, dict):
        contact = structured.get("contact")
        if isinstance(contact, dict) and contact.get("name"):
            from services.resume_parser import ResumeParser
            contact["name"] = ResumeParser._normalize_name(contact["name"])
    return doc


def get_active_structured(user_email: str = "") -> Optional[Dict[str, Any]]:
    """THE canonical read path for structured resume content.

    Resolves ``users.active_resume_id`` -> resume_assets. Falls back to the
    user's newest parsed asset when the pointer is unset (defensive; the
    worker's promotion normally guarantees a pointer). Returns None when the
    user has no parsed resume.
    """
    asset = None
    email = _safe_email(user_email)
    if email:
        user = _db().users.find_one({"email": email}, {"active_resume_id": 1}) or {}
        rid = user.get("active_resume_id")
        if rid:
            asset = _assets().find_one(
                {"resume_id": rid, "structured": {"$ne": None}}
            )
        if asset is None:
            asset = _assets().find_one(
                {"user_email": email, "structured": {"$ne": None}},
                sort=[("parsed_at", -1)],
            )
    else:
        # Legacy no-scope call (single-user dev flows): newest parsed asset.
        asset = _assets().find_one(
            {"structured": {"$ne": None}}, sort=[("parsed_at", -1)]
        )
    return shape_asset(asset)
