"""Preservation invariants between source and selected/exported resumes.

A failed check must not look like a successful export. Callers keep the last
valid revision and surface an actionable result.
"""
from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from services.content_manifest import (
    ContentManifest,
    as_bullet_text,
    build_manifest,
    employer_key,
)

logger = logging.getLogger(__name__)


class PreservationExportError(Exception):
    """Raised when an export would look successful but failed preservation."""

    def __init__(self, result: PreservationResult):
        self.result = result
        msg = (result.errors[0] if result.errors else "Content preservation failed.")
        super().__init__(msg)


@dataclass
class PreservationResult:
    ok: bool
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "status": "ok" if self.ok else "failed",
            "errors": list(self.errors),
            "warnings": list(self.warnings),
        }


def _nonempty_section(sec_type: str, resume: Dict[str, Any]) -> bool:
    if sec_type == "summary":
        return bool(str(resume.get("summary") or "").strip())
    if sec_type == "skills":
        skills = resume.get("skills") or {}
        if isinstance(skills, dict):
            return any(v for v in skills.values())
        return bool(skills)
    if sec_type == "certifications":
        return any(str(c).strip() for c in (resume.get("certifications") or []))
    if sec_type in {"experience", "projects", "education"}:
        return bool(resume.get(sec_type))
    extras = resume.get("additional_sections") or []
    return any(
        isinstance(s, dict)
        and str(s.get("semantic_type") or "").lower() == sec_type
        and (
            s.get("entries")
            or str(s.get("raw_text") or "").strip()
            or str(s.get("parse_status") or "") == "unresolved"
        )
        for s in extras
    )


def _authorized_section_omission(item, prefs: Dict[str, Any]) -> bool:
    if item.user_omitted or item.reason in {"user_omission", "explicit_section_removal"}:
        return True
    omitted = {
        str(x).strip().lower()
        for x in (prefs.get("omitted_sections") or []) + (prefs.get("omitted_section_ids") or [])
    }
    return (
        item.semantic_type.lower() in omitted
        or item.content_id.lower() in omitted
        or str(item.original_heading).strip().lower() in omitted
    )


def compare_manifests(
    source: ContentManifest,
    output: ContentManifest,
    *,
    preferences: Optional[Dict[str, Any]] = None,
) -> PreservationResult:
    """Compare source vs output manifests after a transformation."""
    prefs = preferences or {}
    errors: List[str] = []
    warnings: List[str] = []

    out_by_type = {s.semantic_type: s for s in output.sections()}
    for sec in source.nonempty_source_sections():
        if sec.parse_status == "unresolved":
            out = out_by_type.get(sec.semantic_type)
            if out is None or out.parse_status not in {"unresolved", "ok"}:
                errors.append(
                    f"Unresolved source section '{sec.original_heading or sec.semantic_type}' "
                    "was dropped instead of staying unresolved."
                )
            continue
        if sec.parse_status == "empty":
            continue
        if _authorized_section_omission(sec, prefs):
            continue
        out = next(
            (s for s in output.sections() if s.content_id == sec.content_id
             or s.semantic_type == sec.semantic_type),
            None,
        )
        if out is None or (out.status == "omitted" and not out.user_omitted):
            errors.append(
                f"Nonempty source section '{sec.original_heading or sec.semantic_type}' "
                "disappeared without an authorized omission."
            )
            continue
        # Retained sections need meaningful content, not an empty heading.
        kids = [
            c for c in output.children(out.content_id)
            if c.status in {"selected", "shortened", "source"}
        ]
        if out.semantic_type == "summary":
            if not (out.text or "").strip() and (sec.text or "").strip():
                errors.append("Summary was emptied without an authorized omission.")
            continue
        if out.semantic_type == "skills":
            if not any((c.text or "").strip() for c in kids) and source.children(sec.content_id):
                errors.append("Skills section lost all categories without authorization.")
            continue
        if out.semantic_type == "certifications":
            if not any((c.text or "").strip() for c in kids) and source.children(sec.content_id):
                errors.append("Certifications were removed without an authorized omission.")
            continue
        if out.semantic_type == "education":
            if not kids:
                errors.append("Education section lost all entries without authorization.")
            continue
        meaningful = False
        for kid in kids:
            if kid.kind == "entry":
                bullets = [
                    b for b in output.children(kid.content_id)
                    if b.status in {"selected", "shortened", "source"} and (b.text or "").strip()
                ]
                if bullets or (kid.text or "").strip():
                    meaningful = True
                elif any((kid.identity or {}).get(k) for k in ("title", "organization")):
                    warnings.append(
                        f"Entry '{(kid.identity or {}).get('organization') or kid.content_id}' "
                        "retained only a heading."
                    )
            elif (kid.text or "").strip():
                meaningful = True
        if not meaningful:
            errors.append(
                f"Section '{sec.original_heading or sec.semantic_type}' was retained "
                "without meaningful content."
            )

    for cid, item in source.items.items():
        if not item.locked:
            continue
        out = output.items.get(cid)
        if out is None or out.status == "omitted":
            errors.append(f"Locked content {cid} vanished.")
            continue
        if item.kind == "bullet" and out.text and item.text:
            # Shortening locked bullets is forbidden.
            if out.status == "shortened" and out.text != item.text:
                errors.append(f"Locked content {cid} was rewritten.")

    return PreservationResult(ok=not errors, errors=errors, warnings=warnings)


def _export_haystack(export_text: str, resume: Dict[str, Any]) -> str:
    return " ".join(
        (export_text or "").lower().split()
        + str(resume.get("summary") or "").lower().split()
    )


def _token_present(needle: str, haystack: str) -> bool:
    toks = [t for t in (needle or "").split() if len(t) >= 4]
    if not toks:
        compact = "".join(ch for ch in (needle or "").lower() if ch.isalnum())
        return bool(compact) and compact[:12] in haystack.replace(" ", "")
    return any(tok.lower() in haystack for tok in toks[:3])


def assert_preservation(
    source_resume: Dict[str, Any],
    output_resume: Dict[str, Any],
    *,
    export_text: str = "",
    preferences: Optional[Dict[str, Any]] = None,
    source_manifest: Optional[ContentManifest] = None,
    output_manifest: Optional[ContentManifest] = None,
) -> PreservationResult:
    """Full pre-export preservation check."""
    prefs = preferences or output_resume.get("tailoring_preferences") or {}
    src_m = source_manifest or build_manifest(copy.deepcopy(source_resume), preferences=prefs)
    if output_manifest is None:
        attached = output_resume.get("content_manifest")
        output_manifest = (
            ContentManifest.from_dict(attached)
            if isinstance(attached, dict) and attached.get("items")
            else build_manifest(copy.deepcopy(output_resume), preferences=prefs)
        )
    result = compare_manifests(src_m, output_manifest, preferences=prefs)
    errors = list(result.errors)
    warnings = list(result.warnings)

    # Selected content IDs must appear in the output document.
    hay = (export_text or "").lower()
    if export_text:
        for sec in src_m.nonempty_source_sections():
            if _authorized_section_omission(sec, prefs):
                continue
            if sec.parse_status == "unresolved":
                continue
            heading = (sec.original_heading or sec.semantic_type or "").lower()
            markers = [heading, sec.semantic_type]
            if sec.semantic_type == "projects":
                markers.extend(["project", "projects"])
            if not any(m and m in hay for m in markers if m):
                # Entry names can stand in for a heading the renderer shortened.
                names = [
                    (c.identity or {}).get("organization") or (c.identity or {}).get("title") or ""
                    for c in output_manifest.children(sec.content_id)
                ]
                if not any(_token_present(n, hay) for n in names if n):
                    errors.append(
                        f"Section '{sec.original_heading or sec.semantic_type}' is missing from the export."
                    )
    for cid, item in output_manifest.items.items():
        if item.status not in {"selected", "shortened"}:
            continue
        if item.kind not in {"bullet", "cert", "skill_item"} and item.kind != "section":
            if item.kind == "entry":
                org = (item.identity or {}).get("organization") or ""
                title = (item.identity or {}).get("title") or ""
                for label in (org, title):
                    if label and export_text and not _token_present(label, hay):
                        # Employer grouping may omit a repeated company name.
                        if item.semantic_type == "experience" and org:
                            continue
                        warnings.append(
                            f"Selected entry {cid} ({label!r}) not found in export text."
                        )
            continue
        if item.user_omitted:
            continue
        if export_text and item.text and not _token_present(item.text, hay):
            # PDF text extraction often glues words; fail only for short
            # distinctive tokens that should survive, otherwise warn.
            distinctive = [t for t in item.text.split() if len(t) >= 8]
            if distinctive and not any(t.lower() in hay for t in distinctive[:2]):
                warnings.append(
                    f"Selected content {cid} is not clearly represented in the export."
                )

    # Omitted content must have a reason and remain recoverable from snapshot.
    snapshot = output_manifest.source_snapshot or src_m.source_snapshot
    for cid, item in output_manifest.items.items():
        if item.status != "omitted":
            continue
        if not item.reason:
            errors.append(f"Omitted content {cid} has no recorded reason.")
        if item.kind == "bullet" and item.text:
            recoverable = False
            for section in ("experience", "projects"):
                for entry in snapshot.get(section) or []:
                    if not isinstance(entry, dict):
                        continue
                    for b in entry.get("bullets") or []:
                        if as_bullet_text(b) == item.text:
                            recoverable = True
                            break
            for sec in snapshot.get("additional_sections") or []:
                for entry in (sec or {}).get("entries") or []:
                    for b in (entry or {}).get("bullets") or []:
                        if as_bullet_text(b) == item.text:
                            recoverable = True
                    if (entry or {}).get("body") == item.text:
                        recoverable = True
            if not recoverable and item.text:
                # Snapshot may store original wording before compression.
                warnings.append(f"Omitted content {cid} was not found in the source snapshot.")

    # Attribution: selected experience bullets stay under the right employer.
        src_owners: Dict[str, set] = {}
        for exp in src_m.source_snapshot.get("experience") or []:
            if not isinstance(exp, dict):
                continue
            emp = employer_key(exp)
            for b in exp.get("bullets") or []:
                text = as_bullet_text(b)
                src_owners.setdefault(text, set()).add(emp)
        for exp in output_resume.get("experience") or []:
            if not isinstance(exp, dict):
                continue
            emp = employer_key(exp)
            for b in exp.get("bullets") or []:
                text = as_bullet_text(b)
                owners = src_owners.get(text) or set()
                if owners and emp not in owners:
                    errors.append(
                        f"Bullet moved from employer {sorted(owners)!r} to {emp!r}."
                    )

    ok = not errors
    if not ok:
        logger.error("preservation_check failed: %s", errors)
    return PreservationResult(ok=ok, errors=errors, warnings=warnings)


def attach_check(resume: Dict[str, Any], result: PreservationResult) -> Dict[str, Any]:
    resume = resume if isinstance(resume, dict) else {}
    sel = dict(resume.get("content_selection") or {})
    sel["preservation"] = result.as_dict()
    if not result.ok:
        sel["page_outcome"] = sel.get("page_outcome") or "preservation_failed"
        extra = (
            "Content preservation failed; the last valid revision was kept. "
            + (result.errors[0] if result.errors else "")
        )
        sel["disclosure"] = extra.strip()
    resume["content_selection"] = sel
    return resume
