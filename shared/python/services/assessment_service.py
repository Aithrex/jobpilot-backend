"""Canonical resume assessment service (assessment-v2).

Package 2: ownership resolution, immutable snapshots, resume_assessments
persistence, idempotency, empty-stage async skeleton behind
``RESUME_ASSESSMENT_V2_ENABLED``. Later packages fill artifact / requirement /
reader stages; this module must not invent overall scores.
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from functools import wraps
from typing import Any, Dict, List, Optional, Tuple

from schemas.assessment_contracts import (
    ASSESSMENT_VERSION,
    SCHEMA_VERSION,
    AssessmentResult,
    AssessmentSnapshot,
    StageStatus,
    validate_assessment_result,
)
from services.assessment_policy import POLICY_VERSION, RUBRIC_VERSION

logger = logging.getLogger(__name__)

COLLECTION = "resume_assessments"
FLAG_ENV = "RESUME_ASSESSMENT_V2_ENABLED"

_active = ContextVar("resume_assessment_v2_policy", default=False)

# Error codes returned to adapters / tests (Package 2 exit criteria).
ERR_FORBIDDEN = "assessment_forbidden"
ERR_NOT_FOUND = "assessment_not_found"
ERR_STALE = "assessment_stale"
ERR_IDEMPOTENCY_CONFLICT = "assessment_idempotency_conflict"
ERR_INVALID = "assessment_invalid"
ERR_DISABLED = "assessment_disabled"


class AssessmentError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def enabled() -> bool:
    """Explicit opt-in in every environment. Default off."""
    return _active.get() or os.environ.get(FLAG_ENV, "false").lower() in {
        "true", "1", "on",
    }


def policy_context(fn):
    """Pin assessment-v2 for an in-flight job even if rollout is disabled later."""
    @wraps(fn)
    def wrapped(*args, **kwargs):
        token = _active.set(True)
        try:
            return fn(*args, **kwargs)
        finally:
            _active.reset(token)
    return wrapped


def _db():
    from utils.db_connect import DBConnect
    return DBConnect().get_db()


def _col():
    return _db()[COLLECTION]


def ensure_indexes(*, db=None) -> List[str]:
    """Create resume_assessments indexes. Call from migrate_indexes — not import."""
    database = db if db is not None else _db()
    col = database[COLLECTION]
    created: List[str] = []
    specs = [
        ([("assessment_id", 1)], {"unique": True, "name": "assessment_id_unique"}),
        (
            [("owner_scope", 1), ("idempotency_key", 1)],
            {
                "unique": True,
                "name": "owner_idempotency_unique",
                "partialFilterExpression": {"idempotency_key": {"$type": "string"}},
            },
        ),
        (
            [("owner_scope", 1), ("record_id", 1), ("version_id", 1),
             ("format", 1), ("created_at", -1)],
            {"name": "owner_record_version_format_created"},
        ),
        (
            [("dependency_fingerprint", 1)],
            {"name": "dependency_fingerprint_idx", "sparse": True},
        ),
        (
            [("owner_scope", 1), ("status", 1), ("created_at", -1)],
            {"name": "owner_status_created"},
        ),
    ]
    for keys, opts in specs:
        try:
            created.append(col.create_index(keys, **opts))
        except Exception as exc:
            logger.warning("resume_assessments.ensure_indexes: %s", exc)
    return created


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False,
                   separators=(",", ":"), default=str).encode()
    ).hexdigest()


def owner_scope_for(*, user_email: str = "", guest_id: str = "") -> str:
    email = (user_email or "").strip().lower()
    gid = (guest_id or "").strip()
    if email:
        return f"user:{email}"
    if gid:
        return f"guest:{gid}"
    raise AssessmentError(ERR_FORBIDDEN, "owner_scope required")


def resolve_owned_record(
    *,
    record_id: str,
    user_email: str = "",
    guest_id: str = "",
) -> Dict[str, Any]:
    """Load a tailoring record only when the caller owns it."""
    rid = (record_id or "").strip()
    if not rid:
        raise AssessmentError(ERR_INVALID, "record_id required")
    query: Dict[str, Any] = {"record_id": rid}
    email = (user_email or "").strip().lower()
    gid = (guest_id or "").strip()
    if email:
        query["user_email"] = email
    elif gid:
        query["guest_id"] = gid
    else:
        raise AssessmentError(ERR_FORBIDDEN, "authentication required")
    rec = _db().tailoring_records.find_one(query)
    if not rec:
        raise AssessmentError(ERR_NOT_FOUND, "record not found for owner")
    return rec


def resolve_version(record: Dict[str, Any], version_id: str = "") -> Dict[str, Any]:
    versions = list(record.get("versions") or [])
    if not versions and record.get("tailored_resume"):
        # Legacy single-doc shape — synthesize a version view.
        return {
            "version_id": version_id or record.get("current_version_id") or "legacy",
            "tailored_resume": record.get("tailored_resume"),
            "ats_scores": record.get("ats_scores"),
            "content_hash": "",
        }
    vid = (version_id or record.get("current_version_id") or "").strip()
    if vid:
        for v in versions:
            if v.get("version_id") == vid:
                return v
        raise AssessmentError(ERR_STALE, "version_id does not match owned record")
    if versions:
        # Prefer current; else highest version_number.
        cur = record.get("current_version_id")
        for v in versions:
            if v.get("version_id") == cur:
                return v
        return sorted(versions, key=lambda x: x.get("version_number") or 0)[-1]
    raise AssessmentError(ERR_NOT_FOUND, "no version on record")


def build_snapshot(
    *,
    owner_scope: str,
    record: Dict[str, Any],
    version: Dict[str, Any],
    fmt: str = "pdf",
    jd_analysis: Optional[Dict[str, Any]] = None,
    idempotency_key: str = "",
    previous_assessment_id: str = "",
) -> AssessmentSnapshot:
    fmt_n = (fmt or "pdf").strip().lower()
    if fmt_n not in {"pdf", "docx"}:
        raise AssessmentError(ERR_INVALID, "format must be pdf or docx")
    resume = version.get("tailored_resume") or record.get("tailored_resume") or {}
    jd = jd_analysis if jd_analysis is not None else (record.get("jd_analysis") or {})
    from services.tailoring_policy import content_hash, digest

    doc_hash = version.get("content_hash") or content_hash(resume) if resume else ""
    source_hash = str(record.get("source_hash") or resume.get("tailoring_run", {}).get("source_hash") or "")
    return AssessmentSnapshot(
        owner_scope=owner_scope,
        record_id=str(record.get("record_id") or ""),
        version_id=str(version.get("version_id") or ""),
        format=fmt_n,
        source_snapshot_id=str(record.get("source_snapshot_id") or record.get("base_resume_id") or ""),
        source_hash=source_hash,
        confirmed_evidence_hash=str(record.get("confirmed_evidence_hash") or ""),
        jd_hash=digest(jd) if jd else str(record.get("jd_hash") or ""),
        requirement_version_hash="",
        document_content_hash=doc_hash,
        preferences_hash=digest(resume.get("tailoring_preferences") or {}),
        policy_version=POLICY_VERSION,
        previous_assessment_id=previous_assessment_id or "",
        idempotency_key=idempotency_key or "",
    )


def dependency_fingerprint(snapshot: AssessmentSnapshot) -> str:
    return _digest({
        "record_id": snapshot.record_id,
        "version_id": snapshot.version_id,
        "format": snapshot.format,
        "document_content_hash": snapshot.document_content_hash,
        "source_hash": snapshot.source_hash,
        "jd_hash": snapshot.jd_hash,
        "preferences_hash": snapshot.preferences_hash,
        "policy_version": snapshot.policy_version,
        "rubric": RUBRIC_VERSION,
    })


def request_fingerprint(snapshot: AssessmentSnapshot) -> str:
    """Fingerprint bound to idempotency — different inputs with same key conflict."""
    return dependency_fingerprint(snapshot)


def empty_stage_result(assessment_id: str, snapshot: AssessmentSnapshot) -> AssessmentResult:
    """Package 2 skeleton: stages present, no invented judgments or overall."""
    return AssessmentResult(
        assessment_id=assessment_id,
        schema_version=SCHEMA_VERSION,
        assessment_version=ASSESSMENT_VERSION,
        snapshot=snapshot,
        stages=StageStatus(
            processing="running",
            artifact="unchecked",
            source_check="unresolved",
            reader="unavailable",
            comparison="unavailable",
        ),
        evaluations=[],
        findings=[],
        top_actions=[],
        overall=None,
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def create_or_get_assessment(
    *,
    user_email: str = "",
    guest_id: str = "",
    record_id: str,
    version_id: str = "",
    fmt: str = "pdf",
    jd_analysis: Optional[Dict[str, Any]] = None,
    idempotency_key: str = "",
    previous_assessment_id: str = "",
    pin_policy: bool = True,
) -> Dict[str, Any]:
    """Create a running assessment document or return the idempotent match.

    Raises AssessmentError for forged/stale/cross-user/conflict cases.
    When the flag is off and pin_policy is False, raises ERR_DISABLED.
    """
    if not enabled() and not pin_policy:
        raise AssessmentError(ERR_DISABLED, f"{FLAG_ENV} is off")
    if not enabled() and pin_policy:
        # Allow only when caller already pinned via policy_context / job.
        if not _active.get():
            raise AssessmentError(ERR_DISABLED, f"{FLAG_ENV} is off")

    owner = owner_scope_for(user_email=user_email, guest_id=guest_id)
    record = resolve_owned_record(
        record_id=record_id, user_email=user_email, guest_id=guest_id,
    )
    version = resolve_version(record, version_id)
    snapshot = build_snapshot(
        owner_scope=owner,
        record=record,
        version=version,
        fmt=fmt,
        jd_analysis=jd_analysis,
        idempotency_key=idempotency_key,
        previous_assessment_id=previous_assessment_id,
    )
    fp = request_fingerprint(snapshot)
    dep = dependency_fingerprint(snapshot)

    if idempotency_key:
        existing = _col().find_one({
            "owner_scope": owner,
            "idempotency_key": idempotency_key,
        })
        if existing:
            if existing.get("request_fingerprint") != fp:
                raise AssessmentError(
                    ERR_IDEMPOTENCY_CONFLICT,
                    "idempotency key reused with different inputs",
                )
            existing.pop("_id", None)
            return existing

    assessment_id = str(uuid.uuid4())
    result = empty_stage_result(assessment_id, snapshot)
    payload = result.as_dict()
    errs = validate_assessment_result(payload)
    if errs:
        raise AssessmentError(ERR_INVALID, "; ".join(errs))

    doc = {
        "assessment_id": assessment_id,
        "owner_scope": owner,
        "record_id": snapshot.record_id,
        "version_id": snapshot.version_id,
        "format": snapshot.format,
        "status": "running",
        "schema_version": SCHEMA_VERSION,
        "assessment_version": ASSESSMENT_VERSION,
        "policy_version": POLICY_VERSION,
        "rubric_version": RUBRIC_VERSION,
        "snapshot": snapshot.as_dict(),
        "request_fingerprint": fp,
        "dependency_fingerprint": dep,
        "idempotency_key": idempotency_key or None,
        "result": payload,
        "created_at": _now(),
        "updated_at": _now(),
        "completed_at": None,
        "superseded_by": None,
    }
    try:
        _col().insert_one(doc)
    except Exception as exc:
        # Race on owner+idempotency unique index.
        if idempotency_key:
            existing = _col().find_one({
                "owner_scope": owner,
                "idempotency_key": idempotency_key,
            })
            if existing:
                if existing.get("request_fingerprint") != fp:
                    raise AssessmentError(
                        ERR_IDEMPOTENCY_CONFLICT,
                        "idempotency key reused with different inputs",
                    ) from exc
                existing.pop("_id", None)
                return existing
        raise
    doc.pop("_id", None)
    return doc


def run_assessment_stages(
    assessment_id: str,
    *,
    artifact_bytes: bytes | None = None,
    render_error: str | None = None,
    resume: Optional[Dict[str, Any]] = None,
    jd_analysis: Optional[Dict[str, Any]] = None,
    previous_result: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Run Packages 3–5 stages when inputs are available; honest partial otherwise."""
    col = _col()
    doc = col.find_one({"assessment_id": assessment_id})
    if not doc:
        raise AssessmentError(ERR_NOT_FOUND, "assessment not found")
    if doc.get("status") == "superseded":
        return doc

    result = copy.deepcopy(doc.get("result") or {})
    stages = dict(result.get("stages") or {})
    snap = doc.get("snapshot") or {}
    fmt = snap.get("format") or "pdf"
    resume = resume or {}
    jd = jd_analysis if jd_analysis is not None else {}
    document_text = ""
    text_for_req = ""

    # --- Artifact (Package 3) ---
    if artifact_bytes is not None or render_error:
        try:
            from services.artifact_assessment import assess_artifact
            artifact_payload = assess_artifact(
                artifact_bytes, resume, fmt=fmt, error=render_error,
            )
            document_text = artifact_payload.get("raw_text") or ""
            status = artifact_payload.get("status") or "unchecked"
            stages["artifact"] = {
                "passed": "passed", "failed": "failed", "issues": "issues",
            }.get(status, "unchecked")
            result["artifact"] = {
                k: artifact_payload[k]
                for k in ("format", "status", "issues", "bytes_hash", "content_hash",
                          "cache_key", "extractor_version", "page_count",
                          "extraction_complete")
                if k in artifact_payload
            }
        except Exception as exc:
            logger.warning("artifact stage failed: %s", exc)
            stages["artifact"] = "failed"
    else:
        stages["artifact"] = stages.get("artifact") or "unchecked"

    # --- Requirements (Package 4) ---
    try:
        from services.requirement_assessment import evaluate_requirements
        text_for_req = document_text
        if not text_for_req and resume:
            # Detached draft: observe structured resume text when no bytes.
            parts = [str(resume.get("summary") or "")]
            for exp in resume.get("experience") or []:
                parts.extend(exp.get("bullets") or [])
                parts.append(str(exp.get("title") or ""))
            from schemas.resume_schemas import flatten_skills
            parts.extend(flatten_skills(resume))
            text_for_req = "\n".join(str(p) for p in parts if p)
        req_out = evaluate_requirements(
            document_text=text_for_req,
            jd=jd,
            resume=resume,
        )
        result["canonical_requirements"] = req_out.get("requirements") or []
        result["evaluations"] = req_out.get("evaluations") or []
        result["counts"] = req_out.get("counts") or {}
        stages["source_check"] = "complete" if result["evaluations"] else "unresolved"
    except Exception as exc:
        logger.warning("requirement stage failed: %s", exc)
        stages["source_check"] = "partial"

    # --- Reader + comparison (Package 5) ---
    try:
        from schemas.assessment_contracts import RequirementEvaluation
        from services.assessment_comparison import compare_assessments
        from services.assessment_reader import reader_pass_v2
        evals = []
        for e in result.get("evaluations") or []:
            try:
                evals.append(RequirementEvaluation(
                    requirement_id=e["requirement_id"],
                    document_state=e["document_state"],
                    source_state=e["source_state"],
                    claim_state=e["claim_state"],
                    match_method=e.get("match_method") or "none",
                    unmet_qualifiers=list(e.get("unmet_qualifiers") or []),
                ))
            except Exception:
                continue
        base_text = ""
        if isinstance(previous_result, dict):
            base_text = str((previous_result.get("artifact") or {}).get("raw_text") or "")
        reader = reader_pass_v2(
            document_text or text_for_req,
            jd,
            evals,
            result.get("canonical_requirements") or [],
            base_text=base_text,
            run_claude=True,
        )
        result["findings"] = reader.get("findings") or []
        result["top_actions"] = reader.get("top_actions") or []
        stages["reader"] = reader.get("status") or "unavailable"
        comparison = compare_assessments(
            previous_result, result,
            before_text=base_text,
            after_text=document_text or text_for_req,
        )
        result["comparison"] = comparison
        stages["comparison"] = comparison.get("status") or "unavailable"
    except Exception as exc:
        logger.warning("reader/comparison stage failed: %s", exc)
        stages["reader"] = "partial"
        stages["comparison"] = "unavailable"

    stages["processing"] = "complete" if stages.get("artifact") in {
        "passed", "issues",
    } and stages.get("source_check") == "complete" else "partial"
    result["stages"] = stages
    result["overall"] = None
    now = _now()
    status = "complete" if stages["processing"] == "complete" else "partial"
    col.update_one(
        {"assessment_id": assessment_id, "status": {"$in": ["running", "pending", "partial"]}},
        {"$set": {
            "status": status,
            "result": result,
            "updated_at": now,
            "completed_at": now,
        }},
    )
    out = col.find_one({"assessment_id": assessment_id})
    if out:
        out.pop("_id", None)
    return out or doc


def run_empty_stages(assessment_id: str, *, artifact_bytes: bytes | None = None,
                     render_error: str | None = None,
                     resume: Optional[Dict[str, Any]] = None,
                     jd_analysis: Optional[Dict[str, Any]] = None,
                     previous_result: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Backward-compatible entry: prefer full stage runner when JD/resume given."""
    if resume is not None or jd_analysis is not None or artifact_bytes is not None:
        return run_assessment_stages(
            assessment_id,
            artifact_bytes=artifact_bytes,
            render_error=render_error,
            resume=resume,
            jd_analysis=jd_analysis,
            previous_result=previous_result,
        )
    # Minimal skeleton when no content is available.
    col = _col()
    doc = col.find_one({"assessment_id": assessment_id})
    if not doc:
        raise AssessmentError(ERR_NOT_FOUND, "assessment not found")
    if doc.get("status") == "superseded":
        doc.pop("_id", None)
        return doc
    result = doc.get("result") or {}
    stages = dict(result.get("stages") or {})
    stages.update({
        "processing": "partial",
        "artifact": "unchecked",
        "source_check": "unresolved",
        "reader": "unavailable",
        "comparison": "unavailable",
    })
    result["stages"] = stages
    result["overall"] = None
    result["top_actions"] = [
        "File and requirement checks are not fully available yet for this review.",
    ]
    now = _now()
    col.update_one(
        {"assessment_id": assessment_id, "status": {"$in": ["running", "pending"]}},
        {"$set": {
            "status": "partial",
            "result": result,
            "updated_at": now,
            "completed_at": now,
        }},
    )
    out = col.find_one({"assessment_id": assessment_id})
    if out:
        out.pop("_id", None)
    return out or doc


def get_assessment(
    assessment_id: str,
    *,
    user_email: str = "",
    guest_id: str = "",
) -> Dict[str, Any]:
    owner = owner_scope_for(user_email=user_email, guest_id=guest_id)
    doc = _col().find_one({
        "assessment_id": assessment_id,
        "owner_scope": owner,
    })
    if not doc:
        raise AssessmentError(ERR_NOT_FOUND, "assessment not found for owner")
    doc.pop("_id", None)
    return doc


def supersede_assessment(
    assessment_id: str,
    *,
    replaced_by: str,
    user_email: str = "",
    guest_id: str = "",
) -> Dict[str, Any]:
    owner = owner_scope_for(user_email=user_email, guest_id=guest_id)
    now = _now()
    res = _col().update_one(
        {"assessment_id": assessment_id, "owner_scope": owner,
         "status": {"$ne": "superseded"}},
        {"$set": {
            "status": "superseded",
            "superseded_by": replaced_by,
            "updated_at": now,
            "result.stages.processing": "superseded",
        }},
    )
    if res.matched_count == 0:
        raise AssessmentError(ERR_NOT_FOUND, "assessment not found or already superseded")
    return get_assessment(assessment_id, user_email=user_email, guest_id=guest_id)


def attach_assessment_to_version(
    *,
    record_id: str,
    version_id: str,
    assessment_id: str,
    user_email: str = "",
    guest_id: str = "",
    expected_content_hash: str = "",
) -> bool:
    """Atomically attach assessment_id only if version identity still matches.

    Returns True when attached; False when stale (late result retained but not current).
    """
    owner = owner_scope_for(user_email=user_email, guest_id=guest_id)
    assessment = get_assessment(
        assessment_id, user_email=user_email, guest_id=guest_id,
    )
    snap = assessment.get("snapshot") or {}
    if snap.get("record_id") != record_id or snap.get("version_id") != version_id:
        raise AssessmentError(ERR_STALE, "assessment snapshot does not match target version")
    if expected_content_hash and snap.get("document_content_hash") != expected_content_hash:
        # Keep history; do not make current.
        _col().update_one(
            {"assessment_id": assessment_id},
            {"$set": {"status": "superseded", "superseded_by": "stale_attach",
                      "updated_at": _now()}},
        )
        return False

    rec = resolve_owned_record(
        record_id=record_id, user_email=user_email, guest_id=guest_id,
    )
    # Verify version content hash still matches snapshot when available.
    version = resolve_version(rec, version_id)
    current_hash = version.get("content_hash") or ""
    snap_hash = snap.get("document_content_hash") or ""
    if current_hash and snap_hash and current_hash != snap_hash:
        _col().update_one(
            {"assessment_id": assessment_id},
            {"$set": {"status": "superseded", "superseded_by": "content_changed",
                      "updated_at": _now()}},
        )
        return False

    query: Dict[str, Any] = {"record_id": record_id}
    email = (user_email or "").strip().lower()
    gid = (guest_id or "").strip()
    if email:
        query["user_email"] = email
    else:
        query["guest_id"] = gid

    # Read-modify-write versions[] so attach works without arrayFilters
    # (mongomock and some Atlas tiers). Atomic enough for Package 2 with
    # content-hash guards above.
    fresh = _db().tailoring_records.find_one(query)
    if not fresh:
        raise AssessmentError(ERR_NOT_FOUND, "record not found for owner")
    versions = list(fresh.get("versions") or [])
    updated_versions = False
    for v in versions:
        if v.get("version_id") == version_id:
            v["assessment_id"] = assessment_id
            updated_versions = True
            break
    set_fields: Dict[str, Any] = {"updated_at": _now()}
    if updated_versions:
        set_fields["versions"] = versions
    if fresh.get("current_version_id") == version_id or not versions:
        set_fields["current_assessment_id"] = assessment_id
    if not versions:
        set_fields["assessment_id"] = assessment_id
    _db().tailoring_records.update_one(query, {"$set": set_fields})
    return True


def score_payload_v2(
    *,
    user_email: str = "",
    guest_id: str = "",
    record_id: str = "",
    version_id: str = "",
    tailored_resume: Optional[Dict[str, Any]] = None,
    jd_analysis: Optional[Dict[str, Any]] = None,
    fmt: str = "pdf",
    idempotency_key: str = "",
    artifact_bytes: bytes | None = None,
    render_error: str | None = None,
    source_resume: Optional[Dict[str, Any]] = None,
    previous_result: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Adapter used by ResumeScorer / workers when the flag is on.

    Always runs real stages when resume/JD/bytes are available. Owned records
    persist to resume_assessments; detached drafts evaluate in-memory.
    """
    resume = tailored_resume or {}
    jd = {k: v for k, v in (jd_analysis or {}).items() if k != "_assessment_meta"}

    if record_id and (user_email or guest_id):
        with _pinned():
            doc = create_or_get_assessment(
                user_email=user_email,
                guest_id=guest_id,
                record_id=record_id,
                version_id=version_id,
                fmt=fmt,
                jd_analysis=jd,
                idempotency_key=idempotency_key,
                pin_policy=True,
            )
            finished = run_assessment_stages(
                doc["assessment_id"],
                artifact_bytes=artifact_bytes,
                render_error=render_error,
                resume=resume,
                jd_analysis=jd,
                previous_result=previous_result,
            )
            # Re-run requirement stage with source_resume if provided
            result = copy.deepcopy(finished.get("result") or {})
            if source_resume is not None:
                try:
                    from services.requirement_assessment import evaluate_requirements
                    text = (result.get("artifact") or {}).get("raw_text") or ""
                    if not text:
                        parts = [str(resume.get("summary") or "")]
                        for exp in resume.get("experience") or []:
                            parts.extend(exp.get("bullets") or [])
                        from schemas.resume_schemas import flatten_skills
                        parts.extend(flatten_skills(resume))
                        text = "\n".join(str(p) for p in parts if p)
                    req_out = evaluate_requirements(
                        document_text=text, jd=jd, resume=resume,
                        source_resume=source_resume,
                    )
                    result["canonical_requirements"] = req_out.get("requirements") or []
                    result["evaluations"] = req_out.get("evaluations") or []
                    result["counts"] = req_out.get("counts") or {}
                except Exception as exc:
                    logger.warning("source_resume re-eval failed: %s", exc)
            result["overall"] = None
            result["schema_version"] = SCHEMA_VERSION
            result["assessment_version"] = ASSESSMENT_VERSION
            result["assessment_id"] = finished.get("assessment_id")
            # Do not auto-attach here — save_version binds assessment_id atomically
            # after the content hash is confirmed (Package 6).
            return result

    # Detached draft: evaluate in-memory without ownership persistence.
    assessment_id = f"draft-{uuid.uuid4()}"
    snap = AssessmentSnapshot(
        owner_scope=owner_scope_for(user_email=user_email, guest_id=guest_id)
        if (user_email or guest_id) else "draft:anonymous",
        record_id=record_id or "",
        version_id=version_id or "",
        format=fmt,
        jd_hash=_digest(jd),
        document_content_hash=_digest(resume.get("summary") if resume else ""),
        policy_version=POLICY_VERSION,
    )
    result = empty_stage_result(assessment_id, snap).as_dict()
    try:
        from services.artifact_assessment import assess_artifact
        from services.requirement_assessment import evaluate_requirements
        from services.assessment_comparison import reader_pass_deterministic
        from schemas.assessment_contracts import RequirementEvaluation

        art = assess_artifact(artifact_bytes, resume, fmt=fmt, error=render_error)
        document_text = art.get("raw_text") or ""
        if not document_text and resume:
            parts = [str(resume.get("summary") or "")]
            for exp in resume.get("experience") or []:
                parts.extend(exp.get("bullets") or [])
                parts.append(str(exp.get("title") or ""))
            from schemas.resume_schemas import flatten_skills
            parts.extend(flatten_skills(resume))
            document_text = "\n".join(str(p) for p in parts if p)
        result["artifact"] = {
            k: art[k] for k in (
                "format", "status", "issues", "bytes_hash", "content_hash",
                "cache_key", "extractor_version", "page_count", "extraction_complete",
            ) if k in art
        }
        result["stages"]["artifact"] = {
            "passed": "passed", "failed": "failed", "issues": "issues",
        }.get(art.get("status") or "", "unchecked")

        req_out = evaluate_requirements(
            document_text=document_text, jd=jd, resume=resume,
            source_resume=source_resume or resume,
        )
        result["canonical_requirements"] = req_out.get("requirements") or []
        result["evaluations"] = req_out.get("evaluations") or []
        result["counts"] = req_out.get("counts") or {}
        result["stages"]["source_check"] = (
            "complete" if result["evaluations"] else "unresolved"
        )

        evals = []
        for e in result["evaluations"]:
            try:
                evals.append(RequirementEvaluation(
                    requirement_id=e["requirement_id"],
                    document_state=e["document_state"],
                    source_state=e["source_state"],
                    claim_state=e["claim_state"],
                    match_method=e.get("match_method") or "none",
                    unmet_qualifiers=list(e.get("unmet_qualifiers") or []),
                ))
            except Exception:
                continue
        reader = reader_pass_deterministic(
            document_text, jd, evals, result["canonical_requirements"],
        )
        result["findings"] = reader.get("findings") or []
        result["top_actions"] = reader.get("top_actions") or []
        result["stages"]["reader"] = reader.get("status") or "unavailable"
        result["stages"]["processing"] = (
            "complete" if result["stages"]["artifact"] in {"passed", "issues"}
            and result["stages"]["source_check"] == "complete"
            else "partial"
        )
    except Exception as exc:
        logger.warning("detached assessment-v2 failed: %s", exc)
        result["stages"]["processing"] = "partial"
        result["top_actions"] = [
            "Assessment checks could not be completed for this draft.",
        ]
    result["overall"] = None
    result["schema_version"] = SCHEMA_VERSION
    result["assessment_version"] = ASSESSMENT_VERSION
    result["assessment_id"] = assessment_id
    return result


class _pinned:
    def __enter__(self):
        self._token = _active.set(True)
        return self

    def __exit__(self, *exc):
        _active.reset(self._token)


def adapt_ats_scores_job_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Annotate legacy ats_scores job payloads with assessment-v2 identity fields."""
    out = dict(payload or {})
    out["assessment_policy_version"] = POLICY_VERSION
    out["assessment_rubric"] = RUBRIC_VERSION
    out["assessment_schema_version"] = SCHEMA_VERSION
    return out


def assess_resume_revision(
    *,
    tailored_resume: Dict[str, Any],
    jd_analysis: Dict[str, Any],
    renderer=None,
    user_email: str = "",
    guest_id: str = "",
    record_id: str = "",
    version_id: str = "",
    fmt: str = "pdf",
    source_resume: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Canonical finalize helper: render + score_payload_v2 when flag/pin active."""
    pdf_bytes = None
    render_error = None
    if renderer is not None:
        try:
            pdf_bytes, _resolution = renderer.generate_pdf(tailored_resume)
        except Exception as exc:
            render_error = f"PDF render failed: {exc}"
    return score_payload_v2(
        user_email=user_email,
        guest_id=guest_id,
        record_id=record_id,
        version_id=version_id,
        tailored_resume=tailored_resume,
        jd_analysis=jd_analysis,
        fmt=fmt,
        artifact_bytes=pdf_bytes,
        render_error=render_error,
        source_resume=source_resume,
    )