"""Relevance-aware resume content selection for page fitting.

Produces one explicit final resume revision used by preview and export.
The renderer must remain a faithful presentation layer — this module is the
only place that may drop or compress bullets for page budget.

Policy: docs/plans/2026-09-05-content-selection-policy.md
"""
from __future__ import annotations

import copy
import logging
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

logger = logging.getLogger(__name__)

SELECTION_VERSION = "2026-09-05.1"
_MAX_EXP_BULLETS = 8
_MAX_PROJ_BULLETS = 8
_FLOOR_RECENT = 3
_FLOOR_OTHER = 2
_W_UNIQUE_REQUIRED = 3.0
_W_REQUIRED = 1.5
_W_KEYWORD = 0.6
_W_SPECIFICITY = 0.4
_W_METRIC_TIE = 0.15
_W_REDUNDANCY = 1.2
_W_MODEL_RANK = 0.25
_W_EXPERIENCE = 1.8
_W_PINNED = 100.0

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
        r"reduced|improved|automated|deployed|authored|created)\b",
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


def _entry_name(section: str, entry: Dict[str, Any]) -> str:
    if section == "experience":
        return str(entry.get("company") or entry.get("title") or "role")
    return str(entry.get("name") or "project")


def _bullet_text(raw: Any) -> Tuple[str, str, bool]:
    if isinstance(raw, dict):
        text = str(raw.get("text") or "").strip()
        fact_id = str(raw.get("fact_id") or "")
        pinned = bool(raw.get("pinned") or raw.get("locked"))
        return text, fact_id, pinned
    return str(raw or "").strip(), "", False


def _collect(
    resume: Dict[str, Any],
    *,
    locked_ids: Set[str],
    omitted_ids: Set[str],
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for ei, exp in enumerate(resume.get("experience") or []):
        if not isinstance(exp, dict):
            continue
        pinned_texts = {str(x) for x in (exp.get("pinned_bullets") or [])}
        for rank, raw in enumerate(list(exp.get("bullets") or [])[:_MAX_EXP_BULLETS]):
            text, fact_id, pinned = _bullet_text(raw)
            if not text:
                continue
            if fact_id and fact_id in omitted_ids:
                continue
            if fact_id and fact_id in locked_ids:
                pinned = True
            if text in pinned_texts:
                pinned = True
            out.append({
                "section": "experience", "entry_idx": ei, "rank": rank,
                "text": text, "fact_id": fact_id, "pinned": pinned,
                "entry_name": _entry_name("experience", exp),
            })
    for pi, proj in enumerate(resume.get("projects") or []):
        if not isinstance(proj, dict):
            continue
        pinned_texts = {str(x) for x in (proj.get("pinned_bullets") or [])}
        for rank, raw in enumerate(list(proj.get("bullets") or [])[:_MAX_PROJ_BULLETS]):
            text, fact_id, pinned = _bullet_text(raw)
            if not text:
                continue
            if fact_id and fact_id in omitted_ids:
                continue
            if fact_id and fact_id in locked_ids:
                pinned = True
            if text in pinned_texts or proj.get("pinned"):
                pinned = True
            out.append({
                "section": "project", "entry_idx": pi, "rank": rank,
                "text": text, "fact_id": fact_id, "pinned": pinned,
                "entry_name": _entry_name("project", proj),
            })
    return out


def _score(
    cand: Dict[str, Any],
    *,
    jd: Dict[str, List[Tuple[str, List[str]]]],
    covered: Set[str],
    selected_texts: Sequence[str],
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
    score = (
        _W_UNIQUE_REQUIRED * len(unique)
        + _W_REQUIRED * len(req)
        + _W_KEYWORD * len(kw)
        + _W_SPECIFICITY * _specificity(cand["text"])
        - _W_REDUNDANCY * red
        - _W_MODEL_RANK * cand["rank"]
    )
    if _METRIC_RE.search(cand["text"]):
        score += _W_METRIC_TIE
    if cand["section"] == "experience":
        score += _W_EXPERIENCE + (0.4 / (cand["entry_idx"] + 1))
    if cand.get("pinned"):
        score += _W_PINNED
    return score


def _apply(
    resume: Dict[str, Any],
    selected: Dict[Tuple[str, int], List[str]],
    *,
    project_templates: Optional[List[Dict[str, Any]]] = None,
    drop_empty_projects: bool = False,
) -> Dict[str, Any]:
    for ei, exp in enumerate(resume.get("experience") or []):
        if isinstance(exp, dict):
            exp["bullets"] = list(selected.get(("experience", ei), []))
    templates = project_templates
    if templates is None:
        templates = list(resume.get("projects") or [])
    rebuilt = []
    for pi, proj in enumerate(templates):
        if not isinstance(proj, dict):
            continue
        bullets = list(selected.get(("project", pi), []))
        if not bullets and drop_empty_projects:
            continue
        proj = dict(proj)
        proj["bullets"] = bullets
        if bullets or not drop_empty_projects:
            rebuilt.append(proj)
    # Keep index-stable placeholders during selection; drop empties only at end.
    if drop_empty_projects:
        resume["projects"] = [p for p in rebuilt if p.get("bullets")]
    else:
        resume["projects"] = rebuilt
    return resume


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


def select_resume_content(
    resume: Dict[str, Any],
    jd_analysis: Optional[Dict[str, Any]] = None,
    renderer=None,
    *,
    preferences: Optional[Dict[str, Any]] = None,
    max_pages: Optional[int] = None,
) -> Dict[str, Any]:
    """Select page-fitting bullets; attach content_selection metadata.

    Assessment and Recheck must not call this — they are read-only.
    """
    from services.resume_renderer import ResumeRenderer

    renderer = renderer or ResumeRenderer()
    jd = jd_analysis or {}
    prefs = preferences or resume.get("tailoring_preferences") or {}
    locked = {str(x) for x in (prefs.get("locked_fact_ids") or [])}
    omitted_ids = {str(x) for x in (prefs.get("omitted_fact_ids") or [])}
    working = copy.deepcopy(resume)
    user_omitted_texts = {
        str(x).strip().lower()
        for x in (prefs.get("omitted_bullet_texts") or [])
        if str(x).strip()
    }

    pref = _page_pref(working, prefs)
    # Default: pack for one page. Two-page budget only when the user
    # explicitly asks for two pages — keeps second page rare.
    if max_pages is None:
        max_pages = 2 if pref == "two" else 1
    avail = renderer._AVAIL_H * max(1, max_pages)

    candidates = _collect(working, locked_ids=locked, omitted_ids=omitted_ids)
    if user_omitted_texts:
        candidates = [
            c for c in candidates
            if c["text"].strip().lower() not in user_omitted_texts
        ]

    omitted: List[OmittedBullet] = []
    jd_terms = _jd_terms(jd)
    project_templates = copy.deepcopy(working.get("projects") or [])
    if not candidates:
        working["content_selection"] = ContentSelectionResult(
            resume=working, page_outcome="fit_one",
        ).metadata()
        return working

    by_entry: Dict[Tuple[str, int], List[Dict[str, Any]]] = {}
    for c in candidates:
        by_entry.setdefault((c["section"], c["entry_idx"]), []).append(c)

    selected: Dict[Tuple[str, int], List[str]] = {}
    remaining: List[Dict[str, Any]] = []
    for key, lst in by_entry.items():
        section, idx = key
        if section == "experience":
            pinned = [c for c in lst if c.get("pinned")]
            others = [c for c in lst if not c.get("pinned")]
            others.sort(
                key=lambda c: _score(c, jd=jd_terms, covered=set(), selected_texts=[]),
                reverse=True,
            )
            floor_n = _FLOOR_RECENT if idx == 0 else _FLOOR_OTHER
            seed = pinned + others
            seed = seed[: max(floor_n, len(pinned))]
            selected[key] = [c["text"] for c in seed]
            seeded = set(selected[key])
            remaining.extend([c for c in lst if c["text"] not in seeded])
        else:
            pinned = [c for c in lst if c.get("pinned")]
            selected[key] = [c["text"] for c in pinned]
            seeded = set(selected[key])
            remaining.extend([c for c in lst if c["text"] not in seeded])

    def apply_sel(*, drop_empty: bool = False) -> None:
        _apply(
            working, selected,
            project_templates=project_templates,
            drop_empty_projects=drop_empty,
        )

    attempts = 0
    apply_sel()
    attempts += 1

    def covered_now() -> Set[str]:
        texts = [t for bullets in selected.values() for t in bullets]
        cov: Set[str] = set()
        for t in texts:
            cov |= _covered_canons(t, jd_terms.get("required", []))
        return cov

    def try_make_room_for(cand: Dict[str, Any]) -> bool:
        """Drop redundant non-pinned bullets so a unique-coverage candidate can fit."""
        need = _covered_canons(cand["text"], jd_terms.get("required", [])) - covered_now()
        if not need and not cand.get("pinned"):
            return False
        # Rank drop targets: lowest score among bullets that do not uniquely
        # cover a required term still uncovered after this candidate is added.
        drop_cands: List[Tuple[float, Tuple[str, int], str]] = []
        for key, bullets in selected.items():
            for text in bullets:
                meta = next((c for c in by_entry.get(key, []) if c["text"] == text), None)
                if meta and meta.get("pinned"):
                    continue
                if key[0] == "experience" and len(bullets) <= (
                    _FLOOR_RECENT if key[1] == 0 else _FLOOR_OTHER
                ):
                    # Allow dipping to 1 when making room for unique required evidence.
                    if len(bullets) <= 1:
                        continue
                others = [
                    t for k2, bs in selected.items() for t in bs
                    if not (k2 == key and t == text)
                ]
                others_cov: Set[str] = set()
                for t in others:
                    others_cov |= _covered_canons(t, jd_terms.get("required", []))
                # After adding cand, unique coverage of `text`
                text_unique = _covered_canons(text, jd_terms.get("required", [])) - (
                    others_cov | need
                )
                if text_unique:
                    continue  # do not drop complementary evidence
                sc = _score(
                    meta or {
                        "section": key[0], "entry_idx": key[1], "rank": 9,
                        "text": text, "pinned": False,
                    },
                    jd=jd_terms, covered=others_cov, selected_texts=others,
                )
                drop_cands.append((sc, key, text))
        if not drop_cands:
            return False
        drop_cands.sort(key=lambda x: x[0])
        _, key, text = drop_cands[0]
        selected[key] = [t for t in selected[key] if t != text]
        omitted.append(OmittedBullet(
            section=key[0], entry_idx=key[1], text=text,
            reason="displaced_for_unique_coverage",
            entry_name=next(
                (c.get("entry_name") or "" for c in by_entry.get(key, []) if c["text"] == text),
                "",
            ),
        ))
        apply_sel()
        return True

    while remaining and attempts < 160:
        cov = covered_now()
        selected_texts = [t for bullets in selected.values() for t in bullets]
        remaining.sort(
            key=lambda c: _score(
                c, jd=jd_terms, covered=cov, selected_texts=selected_texts,
            ),
            reverse=True,
        )
        c = remaining.pop(0)
        key = (c["section"], c["entry_idx"])
        prev = list(selected.get(key, []))
        trial_texts = prev + [c["text"]]
        order = [x["text"] for x in by_entry[key] if x["text"] in set(trial_texts)]
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

        # Unique required coverage: displace redundant content instead of dropping.
        unique = _covered_canons(c["text"], jd_terms.get("required", [])) - covered_now()
        if unique or c.get("pinned"):
            selected[key] = prev  # revert trial
            apply_sel()
            made = try_make_room_for(c)
            if made:
                selected[key] = list(selected.get(key, [])) + [c["text"]]
                # restore order
                order2 = [x["text"] for x in by_entry[key] if x["text"] in set(selected[key])]
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
            section=c["section"],
            entry_idx=c["entry_idx"],
            text=c["text"],
            reason="page_budget",
            entry_name=c.get("entry_name") or "",
            fact_id=c.get("fact_id") or "",
        ))

    apply_sel()
    while _measure(renderer, working) > avail and attempts < 200:
        attempts += 1
        elongated: List[Tuple[Tuple[str, int], int, str]] = []
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
            break
        if compressed_any:
            continue

        cov = covered_now()
        drop_cands: List[Tuple[float, Tuple[str, int], str]] = []
        for key, bullets in selected.items():
            for text in bullets:
                meta = next((c for c in by_entry.get(key, []) if c["text"] == text), None)
                if meta and meta.get("pinned"):
                    continue
                if key[0] == "experience" and len(bullets) <= 1:
                    continue
                # Unique coverage raises keep-score so complementary evidence survives.
                text_req = _covered_canons(text, jd_terms.get("required", []))
                others_cov = set()
                for k2, bs in selected.items():
                    for t2 in bs:
                        if t2 == text:
                            continue
                        others_cov |= _covered_canons(t2, jd_terms.get("required", []))
                unique = text_req - others_cov
                base = _score(
                    meta or {
                        "section": key[0], "entry_idx": key[1], "rank": 9,
                        "text": text, "pinned": False,
                    },
                    jd=jd_terms, covered=others_cov, selected_texts=[],
                )
                drop_cands.append((base + 5.0 * len(unique), key, text))
        if not drop_cands:
            break
        drop_cands.sort(key=lambda x: x[0])
        _, key, text = drop_cands[0]
        selected[key] = [t for t in selected[key] if t != text]
        omitted.append(OmittedBullet(
            section=key[0], entry_idx=key[1], text=text,
            reason="page_budget",
            entry_name=next(
                (c.get("entry_name") or "" for c in by_entry.get(key, []) if c["text"] == text),
                "",
            ),
        ))
        apply_sel()

    apply_sel(drop_empty=True)
    height = _measure(renderer, working)
    one_h = renderer._AVAIL_H
    if height <= one_h + 0.5:
        page_outcome = "fit_one"
    elif pref == "one":
        page_outcome = "one_page_not_possible"
    elif height <= avail + 0.5:
        page_outcome = "fit_two" if max_pages >= 2 else "fit_one"
    else:
        page_outcome = "needs_shortening_choice"

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
        resume=working,
        omitted=omitted,
        page_outcome=page_outcome,
        disclosure=disclosure,
        attempts=attempts,
    ).metadata()
    meta["candidate_pool_size"] = len(candidates)
    working["content_selection"] = meta
    logger.info(
        "content_selection outcome=%s omitted=%d attempts=%d height=%.1f avail=%.1f",
        page_outcome, len(omitted), attempts, height, avail,
    )
    return working
