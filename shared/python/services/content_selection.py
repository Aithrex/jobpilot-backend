"""Relevance-aware resume content selection for page fitting.

Produces one explicit final resume revision used by preview and export.
The renderer must remain a faithful presentation layer — this module is the
only place that may drop or compress bullets for page budget.

Page fitting must not silently erase an entire nonempty source section.
A generic one-page preference is not permission to delete a section.

Policy: docs/plans/2026-09-08-content-preservation.md
"""
from __future__ import annotations

import copy
import logging
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from services.content_manifest import (
    ContentManifest,
    build_manifest,
    bullet_text,
    infer_structure_signals,
    section_weights,
    stamp_resume_ids,
    update_item_status,
)
from services.content_preservation import assert_preservation, attach_check

logger = logging.getLogger(__name__)

SELECTION_VERSION = "2026-09-08.1"
_W_UNIQUE_REQUIRED = 3.0
_W_REQUIRED = 1.5
_W_KEYWORD = 0.6
_W_SPECIFICITY = 0.4
_W_METRIC_TIE = 0.15
_W_REDUNDANCY = 1.2
_W_MODEL_RANK = 0.25
_W_PINNED = 100.0
_W_SECTION = 1.0

_METRIC_RE = re.compile(
    r"(\d+(?:\.\d+)?\s*%|\$\s*\d|\b\d+\s*x\b|\b\d{2,}\b|\b\d+\s*"
    r"(?:ms|s|sec|min|hours?|days?|weeks?|months?|k|m|bn?)\b)",
    re.I,
)
_FILLER_RE = re.compile(
    r"\b(?:successfully|effectively|efficiently|various|multiple|"
    r"in order to|the ability to|a number of|a variety of|"
    r"carefully|proactively)\b[, ]*",
    re.I,
)
_SPACE_RE = re.compile(r"\s{2,}")


@dataclass
class OmittedBullet:
    section: str
    entry_idx: int
    text: str
    reason: str
    entry_name: str = ""
    fact_id: str = ""
    content_id: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ContentSelectionResult:
    resume: Dict[str, Any]
    omitted: List[OmittedBullet] = field(default_factory=list)
    page_outcome: str = "fit_one"
    disclosure: str = ""
    attempts: int = 0

    def metadata(self) -> Dict[str, Any]:
        return {
            "selection_version": SELECTION_VERSION,
            "page_outcome": self.page_outcome,
            "disclosure": self.disclosure,
            "omitted_bullets": [o.as_dict() for o in self.omitted],
            "attempts": self.attempts,
        }


def _jd_terms(jd: Dict[str, Any]) -> Dict[str, List[Tuple[str, List[str]]]]:
    from utils.keyword_normalizer import get_all_forms, normalize_single

    def expand(terms: Sequence[str]) -> List[Tuple[str, List[str]]]:
        out: List[Tuple[str, List[str]]] = []
        seen: Set[str] = set()
        for t in terms or []:
            if not isinstance(t, str) or not t.strip():
                continue
            canon = (normalize_single(t) or t).lower()
            if canon in seen:
                continue
            seen.add(canon)
            forms = [f.lower() for f in get_all_forms(t) if f]
            out.append((canon, forms or [canon]))
        return out

    required = list(jd.get("required_skills") or [])
    for rec in jd.get("requirements") or []:
        if isinstance(rec, dict) and rec.get("priority") == "required":
            text = str(rec.get("text") or "").strip()
            if text:
                required.append(text)
    return {
        "required": expand(required),
        "keywords": expand(list(jd.get("keywords") or [])),
    }


def _contains(text_lower: str, form: str) -> bool:
    if not form:
        return False
    if len(form) <= 2:
        return form in text_lower
    return re.search(
        r"(?<![a-zA-Z])" + re.escape(form) + r"(?![a-zA-Z])", text_lower,
    ) is not None


def _covered_canons(text: str, expanded: List[Tuple[str, List[str]]]) -> Set[str]:
    tl = text.lower()
    return {c for c, forms in expanded if any(_contains(tl, f) for f in forms)}


def _specificity(text: str) -> float:
    words = [w for w in re.findall(r"[A-Za-z]{3,}", text)]
    long = sum(1 for w in words if len(w) >= 7)
    actions = len(re.findall(
        r"\b(?:built|designed|led|owned|implemented|shipped|migrated|"
        r"reduced|improved|automated|deployed|authored|created|"
        r"taught|published|diagnosed|operated|managed|sold|"
        r"reconciled|audited|treated|presented)\b",
        text, re.I,
    ))
    return min(2.0, 0.15 * long + 0.35 * actions)


def compress_bullet(text: str) -> str:
    """Bounded wording compression — factual meaning preserved."""
    original = (text or "").strip()
    if len(original) < 90:
        return original
    out = _FILLER_RE.sub(" ", original)
    out = _SPACE_RE.sub(" ", out).strip(" ,;")
    if len(out) < len(original) * 0.55:
        return original
    if abs(len(original) - len(out)) < 8:
        return original
    return out


def _page_pref(resume: Dict[str, Any], preferences: Optional[Dict[str, Any]]) -> str:
    prefs = preferences or {}
    raw = (
        resume.get("page_length")
        or prefs.get("page_length")
        or prefs.get("page_preference")
        or "auto"
    )
    raw = str(raw).strip().lower()
    if raw in {"one", "1", "one_page"}:
        return "one"
    if raw in {"two", "2", "two_page"}:
        return "two"
    return "auto"


def page_budget_for_resume(
    resume: Optional[Dict[str, Any]] = None,
    preferences: Optional[Dict[str, Any]] = None,
) -> int:
    """1 by default; 2 only when the user explicitly requests two pages."""
    return 2 if _page_pref(resume or {}, preferences) == "two" else 1


def _measure(renderer, resume: Dict[str, Any]) -> float:
    _, height = renderer._render_pdf(
        resume,
        section_gap=renderer._MIN_SECTION_GAP,
        entry_gap=renderer._MIN_ENTRY_GAP,
        post_header=renderer._MIN_POST_HEADER,
        header_gap=renderer._MIN_HEADER_GAP,
        skill_gap=renderer._MIN_SKILL_GAP,
        body_size=10.0, lh=3.10, lh_s=3.08,
        measure_only=True,
    )
    return height


def _entry_name(section: str, entry: Dict[str, Any]) -> str:
    if section == "experience":
        return str(entry.get("company") or entry.get("title") or "role")
    if section == "education":
        return str(entry.get("institution") or entry.get("degree") or "education")
    return str(
        entry.get("name") or entry.get("title") or entry.get("organization")
        or section
    )


def _collect_units(
    resume: Dict[str, Any],
    manifest: ContentManifest,
) -> List[Dict[str, Any]]:
    """All supporting units (bullets, bodies, certs) with stable IDs."""
    out: List[Dict[str, Any]] = []

    def add_from_entries(section_key: str, semantic: str, entries: List[Any], section_id: str) -> None:
        for ei, entry in enumerate(entries or []):
            if not isinstance(entry, dict):
                continue
            eid = str(entry.get("content_id") or "")
            pinned_texts = {str(x) for x in (entry.get("pinned_bullets") or [])}
            bullets = list(entry.get("bullets") or [])
            body = str(entry.get("body") or "").strip()
            if not bullets and body:
                bid = ""
                for child in manifest.children(eid):
                    if child.kind in {"bullet", "paragraph"} and child.text == body:
                        bid = child.content_id
                        break
                out.append({
                    "section": semantic, "section_key": section_key,
                    "section_id": section_id, "entry_id": eid,
                    "entry_idx": ei, "rank": 0, "kind": "body",
                    "text": body, "content_id": bid, "fact_id": "",
                    "pinned": bool(entry.get("pinned") or entry.get("locked")),
                    "entry_name": _entry_name(semantic, entry),
                })
                continue
            for rank, raw in enumerate(bullets):
                text, fact_id, pinned = bullet_text(raw)
                if not text:
                    continue
                if text in pinned_texts or entry.get("pinned"):
                    pinned = True
                cid = ""
                for child in manifest.children(eid):
                    if child.kind == "bullet" and child.text == text:
                        cid = child.content_id
                        if child.locked:
                            pinned = True
                        if child.user_omitted:
                            text = ""
                        break
                if not text:
                    continue
                out.append({
                    "section": semantic, "section_key": section_key,
                    "section_id": section_id, "entry_id": eid,
                    "entry_idx": ei, "rank": rank, "kind": "bullet",
                    "text": text, "content_id": cid, "fact_id": fact_id,
                    "pinned": pinned, "entry_name": _entry_name(semantic, entry),
                })

    exp_sec = next((s for s in manifest.sections() if s.semantic_type == "experience"), None)
    add_from_entries("experience", "experience", resume.get("experience") or [],
                     exp_sec.content_id if exp_sec else "")
    proj_sec = next((s for s in manifest.sections() if s.semantic_type == "projects"), None)
    add_from_entries("projects", "projects", resume.get("projects") or [],
                     proj_sec.content_id if proj_sec else "")
    for extra in resume.get("additional_sections") or []:
        if not isinstance(extra, dict):
            continue
        sid = str(extra.get("content_id") or "")
        semantic = str(extra.get("semantic_type") or "custom")
        add_from_entries("additional", semantic, extra.get("entries") or [], sid)

    cert_sec = next((s for s in manifest.sections() if s.semantic_type == "certifications"), None)
    for i, cert in enumerate(resume.get("certifications") or []):
        text = str(cert).strip()
        if not text:
            continue
        cid = ""
        if cert_sec:
            for child in manifest.children(cert_sec.content_id):
                if child.text == text:
                    cid = child.content_id
                    if child.user_omitted:
                        text = ""
                    break
        if not text:
            continue
        out.append({
            "section": "certifications", "section_key": "certifications",
            "section_id": cert_sec.content_id if cert_sec else "",
            "entry_id": cert_sec.content_id if cert_sec else "",
            "entry_idx": i, "rank": i, "kind": "cert",
            "text": text, "content_id": cid, "fact_id": "",
            "pinned": False, "entry_name": "certification",
        })
    return out


def _score(
    cand: Dict[str, Any],
    *,
    jd: Dict[str, List[Tuple[str, List[str]]]],
    covered: Set[str],
    selected_texts: Sequence[str],
    section_weight: float = 1.0,
) -> float:
    req = _covered_canons(cand["text"], jd.get("required", []))
    kw = _covered_canons(cand["text"], jd.get("keywords", []))
    unique = req - covered
    tokens = set(re.findall(r"[a-z]{4,}", cand["text"].lower()))
    red = 0.0
    for prev in selected_texts:
        prev_t = set(re.findall(r"[a-z]{4,}", prev.lower()))
        if not tokens or not prev_t:
            continue
        overlap = len(tokens & prev_t) / max(1, len(tokens))
        if overlap >= 0.55:
            red += overlap
    recency = 0.25 / (cand.get("entry_idx", 0) + 1)
    score = (
        _W_UNIQUE_REQUIRED * len(unique)
        + _W_REQUIRED * len(req)
        + _W_KEYWORD * len(kw)
        + _W_SPECIFICITY * _specificity(cand["text"])
        + _W_SECTION * section_weight
        + recency
        - _W_REDUNDANCY * red
        - _W_MODEL_RANK * cand.get("rank", 0)
    )
    if _METRIC_RE.search(cand["text"]):
        score += _W_METRIC_TIE
    if cand.get("pinned"):
        score += _W_PINNED
    return score


def _shorten_summary(text: str) -> str:
    original = (text or "").strip()
    if len(original) < 220:
        return original
    out = compress_bullet(original)
    if out != original:
        return out
    # Split on sentence boundaries and drop the last low-information clause.
    parts = re.split(r"(?<=[.!?])\s+", original)
    if len(parts) >= 3:
        kept = " ".join(parts[:-1]).strip()
        if len(kept) >= 80:
            return kept
    return original


def _consolidate_skills(skills: Any) -> Any:
    """Merge redundant categories without dropping supported terms."""
    if not isinstance(skills, dict) or len(skills) < 2:
        return skills
    items: Dict[str, List[str]] = {}
    for cat, vals in skills.items():
        labels = [str(v).strip() for v in (vals or []) if str(v).strip()]
        if labels:
            items[str(cat)] = labels
    if len(items) < 2:
        return skills
    # Merge singleton leftovers into the largest category when labels overlap
    # or the singleton name is generic.
    generic = {"skills", "other", "additional", "tools", "general"}
    ranked = sorted(items.items(), key=lambda kv: -len(kv[1]))
    changed = dict(items)
    for cat, labels in list(ranked):
        if len(labels) != 1:
            continue
        if cat.lower() not in generic and len(changed) <= 3:
            continue
        target = next((k for k, _ in ranked if k != cat and k in changed), None)
        if not target:
            continue
        for lab in labels:
            if lab not in changed[target]:
                changed[target].append(lab)
        del changed[cat]
    return changed


def _apply(
    resume: Dict[str, Any],
    selected: Dict[str, List[str]],
    *,
    templates: Dict[str, List[Dict[str, Any]]],
    keep_empty_entries: Set[str],
) -> Dict[str, Any]:
    """Write selected units back. Never drop a protected nonempty section."""

    def rebuild(section_key: str, name_is_project: bool = False) -> List[Dict[str, Any]]:
        rebuilt = []
        for entry in templates.get(section_key) or []:
            if not isinstance(entry, dict):
                continue
            eid = str(entry.get("content_id") or "")
            entry = dict(entry)
            units = list(selected.get(eid, []))
            if entry.get("body") and not entry.get("bullets"):
                entry["body"] = units[0] if units else (entry.get("body") if eid in keep_empty_entries else "")
                if units or eid in keep_empty_entries or entry.get("body"):
                    rebuilt.append(entry)
                continue
            entry["bullets"] = units
            if units or eid in keep_empty_entries:
                rebuilt.append(entry)
        return rebuilt

    resume["experience"] = rebuild("experience")
    resume["projects"] = rebuild("projects")

    extras = []
    for sec in templates.get("additional") or []:
        if not isinstance(sec, dict):
            continue
        sec = dict(sec)
        entries = []
        for entry in sec.get("entries") or []:
            if not isinstance(entry, dict):
                continue
            eid = str(entry.get("content_id") or "")
            entry = dict(entry)
            units = list(selected.get(eid, []))
            if entry.get("body") and not (entry.get("bullets") or []):
                entry["body"] = units[0] if units else entry.get("body")
            else:
                entry["bullets"] = units
            if units or eid in keep_empty_entries or str(entry.get("body") or "").strip():
                entries.append(entry)
        sec["entries"] = entries
        if entries or str(sec.get("parse_status") or "") == "unresolved":
            extras.append(sec)
        elif str(sec.get("content_id") or "") in keep_empty_entries:
            extras.append(sec)
    resume["additional_sections"] = extras

    cert_id = templates.get("certifications_id") or ""
    if cert_id:
        resume["certifications"] = list(selected.get(cert_id, resume.get("certifications") or []))
    return resume


def _section_unit_count(selected: Dict[str, List[str]], entry_ids: Sequence[str]) -> int:
    return sum(len(selected.get(eid, [])) for eid in entry_ids)


def select_resume_content(
    resume: Dict[str, Any],
    jd_analysis: Optional[Dict[str, Any]] = None,
    renderer=None,
    *,
    preferences: Optional[Dict[str, Any]] = None,
    max_pages: Optional[int] = None,
) -> Dict[str, Any]:
    """Select page-fitting content; attach content_selection + manifest.

    Assessment and Recheck must not call this — they are read-only.
    """
    from services.resume_renderer import ResumeRenderer

    renderer = renderer or ResumeRenderer()
    jd = jd_analysis or {}
    prefs = preferences or resume.get("tailoring_preferences") or {}
    working = copy.deepcopy(resume)
    stamp_resume_ids(working)
    source_for_check = copy.deepcopy(working)
    last_valid = copy.deepcopy(working)
    manifest = build_manifest(working, preferences=prefs, stage="select")
    signals = infer_structure_signals(manifest)
    weights = section_weights(manifest, signals, jd)

    pref = _page_pref(working, prefs)
    if max_pages is None:
        max_pages = 2 if pref == "two" else 1
    avail = renderer._AVAIL_H * max(1, max_pages)

    candidates = _collect_units(working, manifest)
    omitted: List[OmittedBullet] = []
    jd_terms = _jd_terms(jd)

    user_omitted_section_types = {
        s.semantic_type for s in manifest.sections() if s.user_omitted
    }
    if user_omitted_section_types:
        candidates = [
            c for c in candidates
            if c.get("section") not in user_omitted_section_types
        ]
        if "projects" in user_omitted_section_types:
            working["projects"] = []
        working["additional_sections"] = [
            s for s in (working.get("additional_sections") or [])
            if str((s or {}).get("semantic_type") or "") not in user_omitted_section_types
        ]

    templates = {
        "experience": copy.deepcopy(working.get("experience") or []),
        "projects": copy.deepcopy(working.get("projects") or []),
        "additional": copy.deepcopy(working.get("additional_sections") or []),
        "certifications_id": next(
            (s.content_id for s in manifest.sections() if s.semantic_type == "certifications"),
            "",
        ),
    }

    # Entries that exist in source with identity must remain addressable.
    keep_empty_entries: Set[str] = set()
    protected_section_ids: Set[str] = set()
    for sec in manifest.nonempty_source_sections():
        if sec.user_omitted:
            continue
        if sec.parse_status == "empty":
            continue
        protected_section_ids.add(sec.content_id)

    by_entry: Dict[str, List[Dict[str, Any]]] = {}
    for c in candidates:
        if not c.get("entry_id"):
            continue
        by_entry.setdefault(c["entry_id"], []).append(c)

    selected: Dict[str, List[str]] = {}
    remaining: List[Dict[str, Any]] = []

    def weight_for(cand: Dict[str, Any]) -> float:
        return float(weights.get(cand.get("section_id") or "", 1.0))

    # Seed: every protected entry keeps at least one meaningful unit (or all
    # pinned units). No experience-only floor that starves other evidence.
    for eid, lst in by_entry.items():
        pinned = [c for c in lst if c.get("pinned")]
        others = [c for c in lst if not c.get("pinned")]
        others.sort(
            key=lambda c: _score(
                c, jd=jd_terms, covered=set(), selected_texts=[],
                section_weight=weight_for(c),
            ),
            reverse=True,
        )
        sec_id = lst[0].get("section_id") or ""
        protected = sec_id in protected_section_ids
        seed = list(pinned)
        if protected and not seed and others:
            seed = [others[0]]
        elif not protected:
            seed = list(pinned)
        selected[eid] = [c["text"] for c in seed]
        seeded = set(selected[eid])
        remaining.extend([c for c in lst if c["text"] not in seeded])
        if protected:
            keep_empty_entries.add(eid)

    cert_id = templates["certifications_id"]
    cert_cands = [c for c in candidates if c.get("kind") == "cert"]
    if cert_id:
        selected[cert_id] = [c["text"] for c in cert_cands]
        # Certs start fully included; overflow may drop extras but not the last.

    def apply_sel() -> None:
        _apply(working, selected, templates=templates, keep_empty_entries=keep_empty_entries)

    attempts = 0
    apply_sel()
    attempts += 1

    def covered_now() -> Set[str]:
        texts = [t for bullets in selected.values() for t in bullets]
        cov: Set[str] = set()
        for t in texts:
            cov |= _covered_canons(t, jd_terms.get("required", []))
        return cov

    def entry_ids_for_section(section_id: str) -> List[str]:
        ids = []
        for lst in by_entry.values():
            if lst and lst[0].get("section_id") == section_id:
                ids.append(lst[0]["entry_id"])
        if cert_id and section_id == cert_id:
            ids.append(cert_id)
        return ids

    def is_last_unit(cand_or_key: str, text: str) -> bool:
        """True if dropping this would empty a protected section."""
        meta = None
        for lst in by_entry.values():
            for c in lst:
                if c["text"] == text and c.get("entry_id") == cand_or_key:
                    meta = c
                    break
        if meta is None and cand_or_key == cert_id:
            sec_id = cert_id
            units = list(selected.get(cert_id) or [])
            return sec_id in protected_section_ids and len(units) <= 1
        if meta is None:
            return False
        sec_id = meta.get("section_id") or ""
        if sec_id not in protected_section_ids:
            return False
        total = _section_unit_count(selected, entry_ids_for_section(sec_id))
        return total <= 1

    def try_make_room_for(cand: Dict[str, Any]) -> bool:
        need = _covered_canons(cand["text"], jd_terms.get("required", [])) - covered_now()
        if not need and not cand.get("pinned"):
            return False
        drop_cands: List[Tuple[float, str, str]] = []
        for key, bullets in selected.items():
            for text in bullets:
                meta = next((c for c in by_entry.get(key, []) if c["text"] == text), None)
                if meta and meta.get("pinned"):
                    continue
                if is_last_unit(key, text):
                    continue
                others = [
                    t for k2, bs in selected.items() for t in bs
                    if not (k2 == key and t == text)
                ]
                others_cov: Set[str] = set()
                for t in others:
                    others_cov |= _covered_canons(t, jd_terms.get("required", []))
                text_unique = _covered_canons(text, jd_terms.get("required", [])) - (
                    others_cov | need
                )
                if text_unique:
                    continue
                sc = _score(
                    meta or {
                        "section": (meta or {}).get("section") or "",
                        "entry_idx": 9, "rank": 9, "text": text, "pinned": False,
                    },
                    jd=jd_terms, covered=others_cov, selected_texts=others,
                    section_weight=weight_for(meta or cand),
                )
                drop_cands.append((sc, key, text))
        if not drop_cands:
            return False
        drop_cands.sort(key=lambda x: x[0])
        _, key, text = drop_cands[0]
        selected[key] = [t for t in selected[key] if t != text]
        omitted.append(OmittedBullet(
            section=(next((c.get("section") for c in by_entry.get(key, []) if c["text"] == text), "")),
            entry_idx=next((c.get("entry_idx", 0) for c in by_entry.get(key, []) if c["text"] == text), 0),
            text=text, reason="displaced_for_unique_coverage",
            entry_name=next((c.get("entry_name") or "" for c in by_entry.get(key, []) if c["text"] == text), ""),
            content_id=next((c.get("content_id") or "" for c in by_entry.get(key, []) if c["text"] == text), ""),
        ))
        apply_sel()
        return True

    # Add remaining units by global score (no experience-only bonus).
    while remaining and attempts < 220:
        cov = covered_now()
        selected_texts = [t for bullets in selected.values() for t in bullets]
        remaining.sort(
            key=lambda c: _score(
                c, jd=jd_terms, covered=cov, selected_texts=selected_texts,
                section_weight=weight_for(c),
            ),
            reverse=True,
        )
        c = remaining.pop(0)
        key = c["entry_id"]
        prev = list(selected.get(key, []))
        trial_texts = prev + [c["text"]]
        order = [x["text"] for x in by_entry.get(key, []) if x["text"] in set(trial_texts)]
        if c["text"] not in order:
            order = prev + [c["text"]]
        selected[key] = order
        apply_sel()
        attempts += 1
        if _measure(renderer, working) <= avail:
            continue

        compressed = compress_bullet(c["text"])
        if compressed != c["text"]:
            selected[key] = prev + [compressed]
            apply_sel()
            attempts += 1
            if _measure(renderer, working) <= avail:
                c["text"] = compressed
                continue

        unique = _covered_canons(c["text"], jd_terms.get("required", [])) - covered_now()
        if unique or c.get("pinned"):
            selected[key] = prev
            apply_sel()
            made = try_make_room_for(c)
            if made:
                selected[key] = list(selected.get(key, [])) + [c["text"]]
                order2 = [x["text"] for x in by_entry.get(key, []) if x["text"] in set(selected[key])]
                if c["text"] not in order2:
                    order2 = list(selected[key])
                selected[key] = order2
                apply_sel()
                attempts += 1
                if _measure(renderer, working) <= avail:
                    continue
                compressed = compress_bullet(c["text"])
                if compressed != c["text"]:
                    selected[key] = [t for t in selected[key] if t != c["text"]] + [compressed]
                    apply_sel()
                    if _measure(renderer, working) <= avail:
                        c["text"] = compressed
                        continue

        selected[key] = prev
        apply_sel()
        omitted.append(OmittedBullet(
            section=c["section"], entry_idx=c.get("entry_idx", 0),
            text=c["text"], reason="page_budget",
            entry_name=c.get("entry_name") or "",
            fact_id=c.get("fact_id") or "",
            content_id=c.get("content_id") or "",
        ))
        if c.get("content_id"):
            update_item_status(
                manifest, c["content_id"], status="omitted",
                reason="page_budget", stage="select",
            )

    apply_sel()

    # Overflow cascade: summary → skills → compress → drop unprotected extras.
    while _measure(renderer, working) > avail and attempts < 260:
        attempts += 1
        summary = str(working.get("summary") or "")
        shortened = _shorten_summary(summary)
        if shortened != summary:
            working["summary"] = shortened
            if _measure(renderer, working) <= avail:
                continue

        consolidated = _consolidate_skills(working.get("skills"))
        if consolidated != working.get("skills"):
            working["skills"] = consolidated
            if _measure(renderer, working) <= avail:
                continue

        elongated: List[Tuple[str, int, str]] = []
        for key, bullets in selected.items():
            for bi, text in enumerate(bullets):
                elongated.append((key, bi, text))
        elongated.sort(key=lambda x: len(x[2]), reverse=True)
        compressed_any = False
        for key, bi, text in elongated:
            meta = next((c for c in by_entry.get(key, []) if c["text"] == text), None)
            if meta and meta.get("pinned"):
                continue
            new = compress_bullet(text)
            if new == text:
                continue
            bullets = list(selected[key])
            bullets[bi] = new
            selected[key] = bullets
            apply_sel()
            compressed_any = True
            if meta and meta.get("content_id"):
                update_item_status(
                    manifest, meta["content_id"], status="shortened",
                    reason="page_budget", stage="select", text=new,
                )
            break
        if compressed_any:
            continue

        cov = covered_now()
        drop_cands: List[Tuple[float, str, str]] = []
        for key, bullets in selected.items():
            for text in bullets:
                meta = next((c for c in by_entry.get(key, []) if c["text"] == text), None)
                if meta and meta.get("pinned"):
                    continue
                if is_last_unit(key, text):
                    continue
                text_req = _covered_canons(text, jd_terms.get("required", []))
                others_cov: Set[str] = set()
                for k2, bs in selected.items():
                    for t2 in bs:
                        if t2 == text:
                            continue
                        others_cov |= _covered_canons(t2, jd_terms.get("required", []))
                unique = text_req - others_cov
                base = _score(
                    meta or {
                        "section": "", "entry_idx": 9, "rank": 9,
                        "text": text, "pinned": False,
                    },
                    jd=jd_terms, covered=others_cov, selected_texts=[],
                    section_weight=weight_for(meta or {}),
                )
                drop_cands.append((base + 5.0 * len(unique), key, text))
        if not drop_cands:
            break
        drop_cands.sort(key=lambda x: x[0])
        _, key, text = drop_cands[0]
        selected[key] = [t for t in selected[key] if t != text]
        omitted.append(OmittedBullet(
            section=next((c.get("section") or "" for c in by_entry.get(key, []) if c["text"] == text), ""),
            entry_idx=next((c.get("entry_idx", 0) for c in by_entry.get(key, []) if c["text"] == text), 0),
            text=text, reason="page_budget",
            entry_name=next((c.get("entry_name") or "" for c in by_entry.get(key, []) if c["text"] == text), ""),
            content_id=next((c.get("content_id") or "" for c in by_entry.get(key, []) if c["text"] == text), ""),
        ))
        apply_sel()

    apply_sel()

    # Ensure protected sections still have meaningful content after apply.
    for sec in manifest.nonempty_source_sections():
        if sec.user_omitted or sec.content_id not in protected_section_ids:
            continue
        if sec.semantic_type == "projects":
            if not working.get("projects"):
                # Restore representative entries from templates.
                restored = []
                for proj in templates.get("projects") or []:
                    if not isinstance(proj, dict):
                        continue
                    eid = str(proj.get("content_id") or "")
                    units = list(selected.get(eid) or [])
                    if not units:
                        cands = by_entry.get(eid) or []
                        if cands:
                            best = max(
                                cands,
                                key=lambda c: _score(
                                    c, jd=jd_terms, covered=covered_now(),
                                    selected_texts=[], section_weight=weight_for(c),
                                ),
                            )
                            units = [best["text"]]
                            selected[eid] = units
                    proj = dict(proj)
                    proj["bullets"] = units
                    if units:
                        restored.append(proj)
                if restored:
                    working["projects"] = restored
        elif sec.semantic_type not in {
            "summary", "skills", "experience", "education", "certifications",
        }:
            extras = working.get("additional_sections") or []
            present = any(
                isinstance(s, dict) and (
                    s.get("content_id") == sec.content_id
                    or str(s.get("semantic_type")) == sec.semantic_type
                )
                for s in extras
            )
            if not present:
                for tmpl in templates.get("additional") or []:
                    if str(tmpl.get("content_id")) == sec.content_id:
                        working.setdefault("additional_sections", []).append(copy.deepcopy(tmpl))

    height = _measure(renderer, working)
    one_h = renderer._AVAIL_H
    if height <= one_h + 0.5:
        page_outcome = "fit_one"
    elif pref == "one":
        page_outcome = "one_page_not_possible"
    elif height <= avail + 0.5:
        page_outcome = "fit_two" if max_pages >= 2 else "one_page_not_possible"
    else:
        page_outcome = "needs_shortening_choice"

    # Mark selected units on the manifest.
    selected_texts = {t for bullets in selected.values() for t in bullets}
    for item in manifest.items.values():
        if item.user_omitted:
            continue
        if item.kind in {"bullet", "cert"} and item.text:
            if item.text in selected_texts or any(
                item.text == t or compress_bullet(item.text) == t for t in selected_texts
            ):
                if item.status != "shortened":
                    item.status = "selected"
                    item.stage = "select"
            elif item.status != "omitted":
                item.status = "omitted"
                item.reason = item.reason or "page_budget"
                item.stage = "select"
        if item.kind == "section" and item.content_id in protected_section_ids:
            if item.status != "omitted":
                item.status = "selected"
                item.stage = "select"

    out_manifest = build_manifest(working, preferences=prefs, stage="select")
    # Carry omission reasons / locks from the working manifest.
    for cid, item in manifest.items.items():
        if cid in out_manifest.items:
            if item.status in {"omitted", "shortened", "selected"}:
                out_manifest.items[cid].status = item.status
                out_manifest.items[cid].reason = item.reason
                out_manifest.items[cid].stage = item.stage
                out_manifest.items[cid].locked = item.locked or out_manifest.items[cid].locked
                out_manifest.items[cid].user_omitted = (
                    item.user_omitted or out_manifest.items[cid].user_omitted
                )
    out_manifest.source_snapshot = manifest.source_snapshot
    out_manifest.source_hash = manifest.source_hash
    out_manifest.layout_warnings = list(manifest.layout_warnings)
    out_manifest.parse_unresolved = list(manifest.parse_unresolved)

    check = assert_preservation(
        source_for_check, working, preferences=prefs,
        source_manifest=manifest, output_manifest=out_manifest,
    )
    if not check.ok:
        logger.error(
            "content_selection preservation failed; restoring pre-selection revision: %s",
            check.errors,
        )
        restored = copy.deepcopy(last_valid)
        restored["content_manifest"] = manifest.to_dict()
        meta = ContentSelectionResult(
            resume=restored, omitted=omitted,
            page_outcome="preservation_failed",
            disclosure=(
                "Selection could not fit the page without dropping a required "
                "section; the previous revision was kept. "
                + (check.errors[0] if check.errors else "")
            ),
            attempts=attempts,
        ).metadata()
        meta["candidate_pool_size"] = len(candidates)
        restored["content_selection"] = meta
        attach_check(restored, check)
        return restored

    last_valid = copy.deepcopy(working)

    disclosure = ""
    if omitted:
        disclosure = (
            f"Content omitted to fit: {len(omitted)} bullet"
            f"{'s' if len(omitted) != 1 else ''} retained for optional restore."
        )
    if page_outcome == "one_page_not_possible":
        extra = (
            "Selected content needs two pages at readable size; "
            "optional shortening is available without silent truncation."
        )
        disclosure = f"{disclosure} {extra}".strip()
    elif page_outcome == "needs_shortening_choice":
        extra = "Content exceeds the readable page budget; choose optional shortening."
        disclosure = f"{disclosure} {extra}".strip()

    meta = ContentSelectionResult(
        resume=working, omitted=omitted, page_outcome=page_outcome,
        disclosure=disclosure, attempts=attempts,
    ).metadata()
    meta["candidate_pool_size"] = len(candidates)
    meta["structure_signals"] = {
        k: signals[k] for k in (
            "n_jobs", "n_projects", "student_like", "credential_like",
            "academic_like", "project_heavy", "research_heavy",
        ) if k in signals
    }
    working["content_selection"] = meta
    working["content_manifest"] = out_manifest.to_dict()
    attach_check(working, check)
    logger.info(
        "content_selection outcome=%s omitted=%d attempts=%d height=%.1f avail=%.1f",
        page_outcome, len(omitted), attempts, height, avail,
    )
    return working
