"""Stable content identity from parse through selection, preview, and export.

The source snapshot is immutable. Items are addressed by content_id, never by
mutable wording or current array index. Compression, reordering, and employer
grouping keep the same IDs and locks.

Policy: docs/plans/2026-09-08-content-preservation.md
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

MANIFEST_VERSION = "2026-09-08.1"

# First-class resume arrays that already have dedicated schema + paint paths.
CANONICAL_SECTIONS: Tuple[str, ...] = (
    "summary",
    "experience",
    "projects",
    "skills",
    "education",
    "certifications",
)

# Semantic types used for allocation and coverage — not a profession enum.
SEMANTIC_TYPES: Tuple[str, ...] = CANONICAL_SECTIONS + (
    "licenses",
    "research",
    "publications",
    "awards",
    "volunteer",
    "teaching",
    "clinical",
    "portfolio",
    "presentations",
    "affiliations",
    "languages",
    "custom",
    "unresolved",
)

_STATUS = ("source", "selected", "shortened", "omitted", "unresolved")
_STAGES = ("parse", "tailor", "select", "export", "user")

# Headings → semantic type. Profession-agnostic; used to preserve, not to
# invent software-only sections.
_HEADING_ALIASES: Tuple[Tuple[str, str], ...] = (
    ("summary", "summary"),
    ("objective", "summary"),
    ("professional summary", "summary"),
    ("experience", "experience"),
    ("work experience", "experience"),
    ("professional experience", "experience"),
    ("employment", "experience"),
    ("work history", "experience"),
    ("projects", "projects"),
    ("project", "projects"),
    ("personal projects", "projects"),
    ("selected projects", "projects"),
    ("academic projects", "projects"),
    ("skills", "skills"),
    ("technical skills", "skills"),
    ("core skills", "skills"),
    ("core competencies", "skills"),
    ("competencies", "skills"),
    ("education", "education"),
    ("academic background", "education"),
    ("certifications", "certifications"),
    ("certificates", "certifications"),
    ("licenses", "licenses"),
    ("licensure", "licenses"),
    ("licenses and certifications", "licenses"),
    ("research", "research"),
    ("research experience", "research"),
    ("research projects", "research"),
    ("publications", "publications"),
    ("papers", "publications"),
    ("peer-reviewed publications", "publications"),
    ("awards", "awards"),
    ("honors", "awards"),
    ("honors and awards", "awards"),
    ("awards and honors", "awards"),
    ("volunteer", "volunteer"),
    ("volunteering", "volunteer"),
    ("volunteer experience", "volunteer"),
    ("community service", "volunteer"),
    ("teaching", "teaching"),
    ("teaching experience", "teaching"),
    ("academic appointments", "teaching"),
    ("clinical", "clinical"),
    ("clinical rotations", "clinical"),
    ("clinical experience", "clinical"),
    ("portfolio", "portfolio"),
    ("selected work", "portfolio"),
    ("presentations", "presentations"),
    ("conferences", "presentations"),
    ("talks", "presentations"),
    ("affiliations", "affiliations"),
    ("professional affiliations", "affiliations"),
    ("memberships", "affiliations"),
    ("languages", "languages"),
)

_SKIP_HEADINGS = {
    "references",
    "references available upon request",
    "contact",
    "contact information",
}

_SPACE_RE = re.compile(r"\s+")
_TOP_LEVEL_PASSTHROUGH = {
    "content_manifest",
    "content_selection",
    "content_id",
    "tailoring_run",
    "tailoring_preferences",
    "document_assessment",
    "ats_keyword_audit",
    "ats_scores",
    "match_gaps",
    "summary_write",
    "verification",
    "skills_layout",
    "skills_section_title",
    "_skill_headings",
    "target_role",
    "page_length",
    "parse_diagnostics",
}


def _norm(text: Any) -> str:
    return _SPACE_RE.sub(" ", str(text or "").strip()).lower()


def _sha(parts: Sequence[Any]) -> str:
    blob = json.dumps(list(parts), ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def content_id(*parts: Any) -> str:
    """Stable id from identity fields — not array position."""
    return "cid:" + _sha(parts)


def heading_semantic_type(heading: str) -> str:
    key = _norm(heading)
    if not key or key in _SKIP_HEADINGS:
        return ""
    for alias, semantic in _HEADING_ALIASES:
        if key == alias:
            return semantic
    return "custom"


def source_hash(resume: Dict[str, Any]) -> str:
    payload = {
        k: resume.get(k)
        for k in (
            "summary", "experience", "projects", "skills", "education",
            "certifications", "additional_sections",
        )
    }
    return "src:" + _sha([payload])


def bullet_text(raw: Any) -> Tuple[str, str, bool]:
    """Return (text, fact_or_content_id, pinned)."""
    if isinstance(raw, dict):
        text = str(raw.get("text") or "").strip()
        ident = str(raw.get("content_id") or raw.get("fact_id") or "")
        pinned = bool(raw.get("pinned") or raw.get("locked"))
        return text, ident, pinned
    return str(raw or "").strip(), "", False


def as_bullet_text(raw: Any) -> str:
    text, _, _ = bullet_text(raw)
    return text


def employer_key(entry: Dict[str, Any]) -> str:
    company = _norm(entry.get("company") or entry.get("organization") or "")
    company = re.sub(r"[.,]$", "", company)
    company = re.sub(r"\s*\([^)]*\)\s*$", "", company).strip()
    return company


def entry_identity(section: str, entry: Dict[str, Any]) -> Tuple[str, ...]:
    if section == "experience":
        return (
            "experience",
            employer_key(entry),
            _norm(entry.get("title")),
            _norm(entry.get("dates")),
        )
    if section == "projects":
        return ("projects", _norm(entry.get("name")), _norm(entry.get("dates")))
    if section == "education":
        return (
            "education",
            _norm(entry.get("institution")),
            _norm(entry.get("degree")),
            _norm(entry.get("dates")),
        )
    return (
        section,
        _norm(entry.get("title") or entry.get("name") or entry.get("organization")),
        _norm(entry.get("dates")),
        _norm(entry.get("body") or ""),
    )


def stamp_entry_id(section: str, entry: Dict[str, Any]) -> str:
    if not isinstance(entry, dict):
        return ""
    existing = str(entry.get("content_id") or "").strip()
    if existing.startswith("cid:"):
        return existing
    cid = content_id(*entry_identity(section, entry))
    entry["content_id"] = cid
    return cid


def stamp_resume_ids(resume: Dict[str, Any]) -> Dict[str, Any]:
    """Write stable content_id on entries. Source text is not rewritten."""
    for i, exp in enumerate(resume.get("experience") or []):
        if isinstance(exp, dict):
            stamp_entry_id("experience", exp)
    for proj in resume.get("projects") or []:
        if isinstance(proj, dict):
            stamp_entry_id("projects", proj)
    for edu in resume.get("education") or []:
        if isinstance(edu, dict):
            stamp_entry_id("education", edu)
    for sec in resume.get("additional_sections") or []:
        if not isinstance(sec, dict):
            continue
        semantic = str(sec.get("semantic_type") or heading_semantic_type(sec.get("heading") or ""))
        heading = str(sec.get("original_heading") or sec.get("heading") or semantic)
        if not str(sec.get("content_id") or "").startswith("cid:"):
            sec["content_id"] = content_id("section", semantic, _norm(heading))
        for entry in sec.get("entries") or []:
            if isinstance(entry, dict):
                stamp_entry_id(semantic or "custom", entry)
    return resume


@dataclass
class ManifestItem:
    content_id: str
    kind: str
    semantic_type: str
    original_heading: str = ""
    parent_id: str = ""
    source_path: str = ""
    identity: Dict[str, str] = field(default_factory=dict)
    text: str = ""
    locked: bool = False
    user_omitted: bool = False
    status: str = "source"
    reason: str = ""
    stage: str = "parse"
    final_path: str = ""
    layout_supported: str = "supported"
    parse_status: str = "ok"

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ContentManifest:
    version: str = MANIFEST_VERSION
    source_hash: str = ""
    items: Dict[str, ManifestItem] = field(default_factory=dict)
    section_ids: List[str] = field(default_factory=list)
    source_snapshot: Dict[str, Any] = field(default_factory=dict)
    layout_warnings: List[str] = field(default_factory=list)
    parse_unresolved: List[str] = field(default_factory=list)

    def get(self, cid: str) -> Optional[ManifestItem]:
        return self.items.get(cid)

    def children(self, parent_id: str) -> List[ManifestItem]:
        return [it for it in self.items.values() if it.parent_id == parent_id]

    def sections(self) -> List[ManifestItem]:
        return [self.items[i] for i in self.section_ids if i in self.items]

    def nonempty_source_sections(self) -> List[ManifestItem]:
        out = []
        for sec in self.sections():
            if sec.parse_status == "empty":
                continue
            if sec.parse_status == "unresolved":
                out.append(sec)
                continue
            if _section_has_source_content(self, sec):
                out.append(sec)
        return out

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "source_hash": self.source_hash,
            "section_ids": list(self.section_ids),
            "items": {k: v.as_dict() for k, v in self.items.items()},
            "source_snapshot": copy.deepcopy(self.source_snapshot),
            "layout_warnings": list(self.layout_warnings),
            "parse_unresolved": list(self.parse_unresolved),
        }

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "ContentManifest":
        data = data or {}
        items = {}
        fields = ManifestItem.__dataclass_fields__
        for k, raw in (data.get("items") or {}).items():
            if not isinstance(raw, dict):
                continue
            kwargs = {name: raw[name] for name in fields if name in raw}
            kwargs.setdefault("content_id", k)
            items[k] = ManifestItem(**kwargs)
        return cls(
            version=str(data.get("version") or MANIFEST_VERSION),
            source_hash=str(data.get("source_hash") or ""),
            items=items,
            section_ids=list(data.get("section_ids") or []),
            source_snapshot=copy.deepcopy(data.get("source_snapshot") or {}),
            layout_warnings=list(data.get("layout_warnings") or []),
            parse_unresolved=list(data.get("parse_unresolved") or []),
        )


def _section_has_source_content(manifest: ContentManifest, sec: ManifestItem) -> bool:
    kids = manifest.children(sec.content_id)
    if sec.semantic_type == "summary":
        return bool(sec.text.strip())
    if sec.semantic_type == "skills":
        return any(k.text.strip() for k in kids)
    if sec.semantic_type == "certifications":
        return any(k.text.strip() for k in kids)
    if not kids:
        return bool(sec.text.strip())
    for kid in kids:
        if kid.kind == "entry":
            bullets = [b for b in manifest.children(kid.content_id) if b.text.strip()]
            if bullets or (kid.text or "").strip() or any(kid.identity.values()):
                return True
        elif kid.text.strip():
            return True
    return False


def _add(manifest: ContentManifest, item: ManifestItem) -> ManifestItem:
    manifest.items[item.content_id] = item
    return item


def _locked_ids(preferences: Optional[Dict[str, Any]]) -> Set[str]:
    prefs = preferences or {}
    return {str(x) for x in (prefs.get("locked_fact_ids") or []) if str(x).strip()}


def _omitted_ids(preferences: Optional[Dict[str, Any]]) -> Set[str]:
    prefs = preferences or {}
    return {str(x) for x in (prefs.get("omitted_fact_ids") or []) if str(x).strip()}


def _omitted_texts(preferences: Optional[Dict[str, Any]]) -> Set[str]:
    prefs = preferences or {}
    return {
        _norm(x) for x in (prefs.get("omitted_bullet_texts") or []) if str(x).strip()
    }


def _section_omitted(preferences: Optional[Dict[str, Any]], semantic: str) -> bool:
    prefs = preferences or {}
    omitted = {str(x).strip().lower() for x in (prefs.get("omitted_section_ids") or [])}
    omitted |= {str(x).strip().lower() for x in (prefs.get("omitted_sections") or [])}
    return semantic.lower() in omitted


def build_manifest(
    resume: Dict[str, Any],
    *,
    preferences: Optional[Dict[str, Any]] = None,
    raw_text: str = "",
    stage: str = "parse",
) -> ContentManifest:
    """Build a manifest from structured resume JSON.

    `resume` is treated as the source of record for this snapshot. The
    snapshot stored on the manifest is a deepcopy and is never mutated.
    """
    stamp_resume_ids(resume)
    prefs = preferences or resume.get("tailoring_preferences") or {}
    locked = _locked_ids(prefs)
    omitted_ids = _omitted_ids(prefs)
    omitted_texts = _omitted_texts(prefs)
    source = copy.deepcopy({
        "summary": resume.get("summary") or "",
        "experience": resume.get("experience") or [],
        "projects": resume.get("projects") or [],
        "skills": resume.get("skills") or {},
        "education": resume.get("education") or [],
        "certifications": resume.get("certifications") or [],
        "additional_sections": resume.get("additional_sections") or [],
    })
    manifest = ContentManifest(
        source_hash=source_hash(resume),
        source_snapshot=source,
    )

    def mark_lock(item: ManifestItem, fact_id: str = "") -> None:
        if item.content_id in locked or (fact_id and fact_id in locked):
            item.locked = True
        if item.content_id in omitted_ids or (fact_id and fact_id in omitted_ids):
            item.user_omitted = True
            item.status = "omitted"
            item.reason = "user_omission"
            item.stage = "user"
        if item.text and _norm(item.text) in omitted_texts:
            item.user_omitted = True
            item.status = "omitted"
            item.reason = "user_omission"
            item.stage = "user"

    # Summary
    summary_text = str(resume.get("summary") or "").strip()
    summary_id = content_id("section", "summary")
    summary_item = _add(manifest, ManifestItem(
        content_id=summary_id, kind="section", semantic_type="summary",
        original_heading="SUMMARY", text=summary_text, stage=stage,
        parse_status="ok" if summary_text else "empty",
        status="source" if summary_text else "omitted",
        reason="" if summary_text else "empty_source",
    ))
    if prefs.get("omit_summary"):
        summary_item.user_omitted = True
        summary_item.status = "omitted"
        summary_item.reason = "user_omission"
        summary_item.stage = "user"
    if prefs.get("summary_locked"):
        summary_item.locked = True
    manifest.section_ids.append(summary_id)

    def add_entry_section(
        semantic: str,
        heading: str,
        entries: Sequence[Any],
        *,
        name_fields: Sequence[str],
        layout_supported: str = "supported",
        parent_section_id: str = "",
        source_prefix: str = "",
    ) -> str:
        sid = parent_section_id or content_id("section", semantic, _norm(heading))
        nonempty = False
        sec = manifest.items.get(sid) or _add(manifest, ManifestItem(
            content_id=sid, kind="section", semantic_type=semantic,
            original_heading=heading, stage=stage,
            layout_supported=layout_supported,
        ))
        for i, raw in enumerate(entries or []):
            if not isinstance(raw, dict):
                continue
            eid = stamp_entry_id(semantic, raw)
            title = str(
                raw.get("title") or raw.get("name") or raw.get("degree")
                or raw.get("organization") or ""
            )
            org = str(
                raw.get("company") or raw.get("organization")
                or raw.get("institution") or raw.get("name") or ""
            )
            dates = str(raw.get("dates") or "")
            body = str(raw.get("body") or "").strip()
            identity = {
                "title": title, "organization": org, "dates": dates,
            }
            entry_item = _add(manifest, ManifestItem(
                content_id=eid, kind="entry", semantic_type=semantic,
                original_heading=heading, parent_id=sid,
                source_path=f"{source_prefix or semantic}[{i}]",
                identity=identity, text=body, stage=stage,
                layout_supported=layout_supported,
            ))
            mark_lock(entry_item, str(raw.get("fact_id") or ""))
            pinned_texts = {_norm(x) for x in (raw.get("pinned_bullets") or [])}
            bullets = list(raw.get("bullets") or [])
            if not bullets and body:
                nonempty = True
            seen_text: Dict[str, int] = {}
            for j, braw in enumerate(bullets):
                text, fact_id, pinned = bullet_text(braw)
                if not text:
                    continue
                occ = seen_text.get(_norm(text), 0)
                seen_text[_norm(text)] = occ + 1
                bid = content_id("bullet", eid, _norm(text), occ)
                b_item = _add(manifest, ManifestItem(
                    content_id=bid, kind="bullet", semantic_type=semantic,
                    original_heading=heading, parent_id=eid,
                    source_path=f"{entry_item.source_path}.bullets[{j}]",
                    identity={"entry": eid}, text=text, stage=stage,
                    locked=pinned or _norm(text) in pinned_texts,
                ))
                mark_lock(b_item, fact_id)
                nonempty = True
            if title or org or dates:
                nonempty = True
        sec.parse_status = "ok" if nonempty else "empty"
        if _section_omitted(prefs, semantic) or _section_omitted(prefs, sid):
            sec.user_omitted = True
            sec.status = "omitted"
            sec.reason = "user_omission"
            sec.stage = "user"
        return sid

    exp_id = add_entry_section(
        "experience", "EXPERIENCE", resume.get("experience") or [],
        name_fields=("company", "title"), source_prefix="experience",
    )
    manifest.section_ids.append(exp_id)

    proj_id = add_entry_section(
        "projects", "PROJECTS", resume.get("projects") or [],
        name_fields=("name",), source_prefix="projects",
    )
    manifest.section_ids.append(proj_id)

    skills_id = content_id("section", "skills")
    skills = resume.get("skills") or {}
    skill_nonempty = False
    _add(manifest, ManifestItem(
        content_id=skills_id, kind="section", semantic_type="skills",
        original_heading=str(resume.get("skills_section_title") or "SKILLS"),
        stage=stage,
    ))
    if isinstance(skills, dict):
        for cat, items in skills.items():
            cid = content_id("skill_category", _norm(cat))
            labels = [str(x).strip() for x in (items or []) if str(x).strip()]
            cat_item = _add(manifest, ManifestItem(
                content_id=cid, kind="skill_category", semantic_type="skills",
                original_heading=str(cat), parent_id=skills_id,
                source_path=f"skills.{cat}", text=", ".join(labels), stage=stage,
                identity={"label": str(cat)},
            ))
            mark_lock(cat_item)
            for n, lab in enumerate(labels):
                kid = content_id("skill", cid, _norm(lab), n)
                _add(manifest, ManifestItem(
                    content_id=kid, kind="skill_item", semantic_type="skills",
                    parent_id=cid, text=lab, stage=stage,
                    source_path=f"skills.{cat}[{n}]",
                ))
                skill_nonempty = True
    manifest.items[skills_id].parse_status = "ok" if skill_nonempty else "empty"
    manifest.section_ids.append(skills_id)

    edu_id = add_entry_section(
        "education", "EDUCATION", resume.get("education") or [],
        name_fields=("institution", "degree"), source_prefix="education",
    )
    manifest.section_ids.append(edu_id)

    certs_id = content_id("section", "certifications")
    certs = [str(c).strip() for c in (resume.get("certifications") or []) if str(c).strip()]
    _add(manifest, ManifestItem(
        content_id=certs_id, kind="section", semantic_type="certifications",
        original_heading="CERTIFICATIONS", stage=stage,
        parse_status="ok" if certs else "empty",
    ))
    for i, cert in enumerate(certs):
        cid = content_id("cert", _norm(cert), i)
        item = _add(manifest, ManifestItem(
            content_id=cid, kind="cert", semantic_type="certifications",
            parent_id=certs_id, text=cert, stage=stage,
            source_path=f"certifications[{i}]",
        ))
        mark_lock(item)
    if _section_omitted(prefs, "certifications"):
        manifest.items[certs_id].user_omitted = True
        manifest.items[certs_id].status = "omitted"
        manifest.items[certs_id].reason = "user_omission"
    manifest.section_ids.append(certs_id)

    for sec in resume.get("additional_sections") or []:
        if not isinstance(sec, dict):
            continue
        heading = str(sec.get("original_heading") or sec.get("heading") or "ADDITIONAL")
        semantic = str(sec.get("semantic_type") or heading_semantic_type(heading) or "custom")
        layout = str(sec.get("layout_supported") or "generic")
        parse_status = str(sec.get("parse_status") or "ok")
        sid = str(sec.get("content_id") or content_id("section", semantic, _norm(heading)))
        sec["content_id"] = sid
        add_entry_section(
            semantic, heading, sec.get("entries") or [],
            name_fields=("title", "organization", "name"),
            layout_supported=layout,
            parent_section_id=sid,
            source_prefix=f"additional.{semantic}",
        )
        item = manifest.items[sid]
        item.layout_supported = layout
        if parse_status == "unresolved":
            item.parse_status = "unresolved"
            item.status = "unresolved"
            item.reason = str(sec.get("reason") or "parse_unresolved")
            manifest.parse_unresolved.append(sid)
            raw_body = str(sec.get("raw_text") or item.text or "")
            item.text = raw_body
        if layout in {"unsupported", "false", "no"}:
            manifest.layout_warnings.append(
                f"Section '{heading}' uses a layout this renderer cannot reproduce "
                "exactly; content is preserved in a generic entry layout."
            )
        if sid not in manifest.section_ids:
            manifest.section_ids.append(sid)

    if raw_text:
        mark_unresolved_headings(manifest, resume, raw_text)

    return manifest


_HEADING_LINE_RE = re.compile(
    r"^\s*([A-Z][A-Z0-9 /&-]{2,40})\s*$",
    re.M,
)


def mark_unresolved_headings(
    manifest: ContentManifest,
    resume: Dict[str, Any],
    raw_text: str,
) -> None:
    """Headings present in source text but missing structured content stay unresolved.

    Truly empty parsed sections stay empty. A heading with no extracted body is
    unresolved — not an authorized omission.
    """
    present_types = {
        it.semantic_type
        for it in manifest.nonempty_source_sections()
    }
    # First-class empty-but-ok (author never had the section): heading absent.
    found_headings: List[Tuple[str, str]] = []
    contact_name = _norm((resume.get("contact") or {}).get("name") or "")
    for line in raw_text.splitlines():
        stripped = line.strip().rstrip(":")
        if contact_name and _norm(stripped) == contact_name:
            continue
        semantic = heading_semantic_type(stripped)
        if not semantic or semantic in _SKIP_HEADINGS:
            continue
        # Recognized aliases may use title case; arbitrary prose and contact
        # lines must not become custom sections merely because they are unknown.
        if semantic == "custom" and not _HEADING_LINE_RE.fullmatch(stripped):
            continue
        found_headings.append((stripped, semantic))

    for heading, semantic in found_headings:
        if semantic in present_types:
            continue
        # Canonical section exists but is empty while the heading is in the
        # source — treat as parse failure, not "author had no such section".
        existing = next(
            (s for s in manifest.sections() if s.semantic_type == semantic),
            None,
        )
        if existing and existing.parse_status == "empty":
            existing.parse_status = "unresolved"
            existing.status = "unresolved"
            existing.reason = "heading_present_extraction_empty"
            existing.original_heading = heading
            existing.stage = "parse"
            manifest.parse_unresolved.append(existing.content_id)
            continue
        if existing:
            continue
        sid = content_id("section", semantic, _norm(heading), "unresolved")
        _add(manifest, ManifestItem(
            content_id=sid, kind="section", semantic_type=semantic,
            original_heading=heading, parse_status="unresolved",
            status="unresolved", reason="heading_present_not_extracted",
            stage="parse", layout_supported="generic",
        ))
        manifest.section_ids.append(sid)
        manifest.parse_unresolved.append(sid)


def lift_unknown_sections(data: Dict[str, Any]) -> Dict[str, Any]:
    """Move unexpected top-level section keys into additional_sections.

    Prevents validate_and_coerce from silently dropping volunteer/research/etc.
    when a model emits them as sibling keys instead of additional_sections.
    """
    if not isinstance(data, dict):
        return data
    known = set(CANONICAL_SECTIONS) | {
        "contact", "skills_layout", "skills_section_title", "additional_sections",
        "raw_text", "extracted_urls",
    } | _TOP_LEVEL_PASSTHROUGH
    extra = list(data.get("additional_sections") or [])
    if not isinstance(extra, list):
        extra = []
    lifted = []
    for key, value in list(data.items()):
        if key in known:
            continue
        semantic = heading_semantic_type(key)
        if not semantic:
            continue
        lifted.append(_coerce_unknown_section(key, semantic, value))
        del data[key]
    if lifted:
        extra.extend(lifted)
        data["additional_sections"] = extra
    return data


def _coerce_unknown_section(key: str, semantic: str, value: Any) -> Dict[str, Any]:
    heading = str(key).replace("_", " ").strip() or semantic
    entries: List[Dict[str, Any]] = []
    raw_text = ""
    parse_status = "ok"
    if isinstance(value, str) and value.strip():
        entries.append({"title": "", "organization": "", "dates": "", "bullets": [], "body": value.strip()})
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, str) and item.strip():
                entries.append({
                    "title": item.strip(), "organization": "", "dates": "",
                    "bullets": [], "body": "",
                })
            elif isinstance(item, dict):
                entries.append(_normalize_extra_entry(item))
    elif isinstance(value, dict):
        if any(k in value for k in ("entries", "items", "bullets", "title", "name")):
            for item in value.get("entries") or value.get("items") or [value]:
                if isinstance(item, dict):
                    entries.append(_normalize_extra_entry(item))
        else:
            raw_text = json.dumps(value, ensure_ascii=False)
            parse_status = "unresolved"
    else:
        parse_status = "empty" if not value else "unresolved"
        raw_text = str(value or "")
    if not entries and not raw_text:
        parse_status = "empty"
    return {
        "content_id": content_id("section", semantic, _norm(heading)),
        "heading": heading.title() if heading.isupper() else heading,
        "original_heading": heading,
        "semantic_type": semantic,
        "layout_supported": "generic",
        "parse_status": parse_status,
        "raw_text": raw_text,
        "entries": entries,
    }


def _normalize_extra_entry(item: Dict[str, Any]) -> Dict[str, Any]:
    bullets = item.get("bullets") or []
    if isinstance(bullets, str):
        bullets = [bullets]
    bullets = [as_bullet_text(b) for b in bullets if as_bullet_text(b)]
    body = str(item.get("body") or item.get("description") or item.get("text") or "").strip()
    if not bullets and not body:
        # Paragraph-only leftover fields
        extras = [
            str(v).strip() for k, v in item.items()
            if k not in {
                "title", "name", "organization", "institution", "company",
                "location", "dates", "url", "content_id", "tech",
            } and isinstance(v, str) and v.strip()
        ]
        body = extras[0] if extras else ""
    return {
        "content_id": str(item.get("content_id") or ""),
        "title": str(item.get("title") or item.get("name") or ""),
        "organization": str(
            item.get("organization") or item.get("institution")
            or item.get("company") or ""
        ),
        "location": str(item.get("location") or ""),
        "dates": str(item.get("dates") or ""),
        "url": str(item.get("url") or ""),
        "bullets": bullets,
        "body": body,
    }


ADDITIONAL_ENTRY_SCHEMA = {
    "content_id": str,
    "title": str,
    "organization": str,
    "location": str,
    "dates": str,
    "url": str,
    "bullets": [str],
    "body": str,
}

ADDITIONAL_SECTION_SCHEMA = {
    "content_id": str,
    "heading": str,
    "original_heading": str,
    "semantic_type": str,
    "layout_supported": str,
    "parse_status": str,
    "raw_text": str,
    "reason": str,
    "entries": [ADDITIONAL_ENTRY_SCHEMA],
}


def infer_structure_signals(manifest: ContentManifest) -> Dict[str, Any]:
    """Profession-agnostic signals from the actual document + emptiness."""
    counts = {t: 0 for t in SEMANTIC_TYPES}
    bullet_counts = {t: 0 for t in SEMANTIC_TYPES}
    for item in manifest.items.values():
        if item.status == "omitted" and item.user_omitted:
            continue
        if item.kind == "entry":
            counts[item.semantic_type] = counts.get(item.semantic_type, 0) + 1
        if item.kind in {"bullet", "cert", "paragraph"} and item.text.strip():
            bullet_counts[item.semantic_type] = (
                bullet_counts.get(item.semantic_type, 0) + 1
            )
        if item.kind == "section" and item.semantic_type == "summary" and item.text.strip():
            bullet_counts["summary"] += 1
    n_jobs = counts.get("experience", 0)
    n_projects = counts.get("projects", 0)
    n_research = counts.get("research", 0)
    n_pubs = counts.get("publications", 0)
    n_creds = counts.get("certifications", 0) + counts.get("licenses", 0)
    n_clinical = counts.get("clinical", 0)
    n_teach = counts.get("teaching", 0)
    student_like = n_jobs == 0 and (n_projects + n_research + n_pubs) > 0
    credential_like = n_creds > 0 or n_clinical > 0
    academic_like = n_pubs + n_research + n_teach >= 2
    return {
        "entry_counts": counts,
        "unit_counts": bullet_counts,
        "n_jobs": n_jobs,
        "n_projects": n_projects,
        "student_like": student_like,
        "credential_like": credential_like,
        "academic_like": academic_like,
        "project_heavy": n_projects >= max(2, n_jobs),
        "research_heavy": n_research + n_pubs > n_jobs,
    }


def section_weights(
    manifest: ContentManifest,
    signals: Dict[str, Any],
    jd: Optional[Dict[str, Any]] = None,
) -> Dict[str, float]:
    """Allocate relative keep-priority across sections.

    Locks and nonempty source representation dominate. JD relevance tilts
    weights but never zeroes a nonempty unprotected section.
    """
    weights: Dict[str, float] = {}
    jd = jd or {}
    jd_blob = " ".join(
        str(x) for x in (
            jd.get("job_title") or "",
            " ".join(jd.get("required_skills") or []),
            " ".join(jd.get("keywords") or []),
            " ".join(jd.get("qualifications") or []),
            " ".join(jd.get("responsibilities") or []),
        )
    ).lower()

    def jd_hit(semantic: str, heading: str) -> float:
        if not jd_blob:
            return 0.0
        tokens = {_norm(semantic), _norm(heading)}
        score = 0.0
        for tok in tokens:
            if tok and tok in jd_blob:
                score += 0.6
        cues = {
            "licenses": ("license", "licensure", "rn", "cpa", "bar ", "certified"),
            "certifications": ("certif", "license", "credential"),
            "clinical": ("clinical", "rotation", "patient", "hospital"),
            "research": ("research", "lab", "publication"),
            "publications": ("publication", "journal", "paper"),
            "teaching": ("teach", "faculty", "curriculum", "classroom"),
            "projects": ("project", "portfolio", "built"),
            "volunteer": ("volunteer", "community"),
            "experience": ("experience", "years"),
        }
        for cue in cues.get(semantic, ()):
            if cue in jd_blob:
                score += 0.35
        return min(1.5, score)

    for sec in manifest.nonempty_source_sections():
        if sec.user_omitted:
            weights[sec.content_id] = 0.0
            continue
        base = 1.0
        st = sec.semantic_type
        if st == "experience":
            base = 1.15 if signals.get("n_jobs") else 0.2
        elif st == "projects":
            base = 1.35 if signals.get("student_like") or signals.get("project_heavy") else 1.0
        elif st in {"research", "publications"}:
            base = 1.4 if signals.get("research_heavy") or signals.get("academic_like") else 1.0
        elif st in {"certifications", "licenses"}:
            base = 1.45 if signals.get("credential_like") else 1.05
        elif st == "clinical":
            base = 1.4 if signals.get("credential_like") else 1.0
        elif st == "teaching":
            base = 1.35 if signals.get("academic_like") else 1.0
        elif st == "education":
            base = 1.2 if signals.get("student_like") else 0.9
        elif st == "summary":
            base = 0.7
        elif st == "skills":
            base = 0.95
        else:
            base = 1.0
        if sec.locked:
            base += 2.0
        if sec.parse_status == "unresolved":
            base = max(base, 0.5)
        weights[sec.content_id] = base + jd_hit(st, sec.original_heading)
    return weights


def update_item_status(
    manifest: ContentManifest,
    content_id: str,
    *,
    status: str,
    reason: str = "",
    stage: str = "select",
    final_path: str = "",
    text: Optional[str] = None,
) -> None:
    item = manifest.items.get(content_id)
    if not item:
        return
    item.status = status
    item.reason = reason
    item.stage = stage
    if final_path:
        item.final_path = final_path
    if text is not None:
        item.text = text


def selected_ids(manifest: ContentManifest) -> Set[str]:
    return {
        cid for cid, it in manifest.items.items()
        if it.status in {"selected", "shortened", "source"} and not it.user_omitted
    }
