"""Résumé format preferences: length, spacing, accent and section order.

One home for what the user chose about the LOOK of their résumé, carried on
the tailored résumé as ``tailoring_preferences`` (the key content selection,
the bullet allocator and the renderer already read ``page_length`` from):

    page_length    "one" | "two"                       (absent = one page)
    density        "compact" | "comfortable"           (absent = compact)
    accent         "black" | "navy"                    (absent = black)
    section_order  "auto" | "education_first" | "experience_first"

None of this spends a model call. The default for every key is exactly what
rendered before these options existed, so a résumé without preferences is
byte-identical.

"Not overloaded" is enforced in two places, never by shrinking type further:
content selection drops the lowest-ranked lines until the chosen length fits
at the chosen spacing, and the page-fill step never pads a two-page résumé
toward a second full page.
"""
from __future__ import annotations

import copy
import re
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

PAGE_LENGTHS = {"one", "two"}
DENSITIES = {"compact", "comfortable"}
ACCENTS: Dict[str, Tuple[int, int, int]] = {
    "black": (0, 0, 0),
    # RenderCV's classic navy: 8.3:1 on white, prints dark in greyscale.
    "navy": (0, 79, 144),
}
SECTION_ORDERS = {"auto", "education_first", "experience_first"}

# Spec overrides for the comfortable density: half-inch sides, a 9.7pt body
# floor. Content selection measures at this floor, so it trims lines instead
# of the renderer shrinking type to make them fit.
COMFORTABLE = {
    "side_margin_dense_in": 0.50,
    "top_margin_dense_in": 0.40,
    "bottom_margin_dense_in": 0.40,
    "side_margin_sparse_in": 0.75,
    "top_margin_sparse_in": 0.50,
    "bottom_margin_sparse_in": 0.45,
    "min_body_pt": 9.7,
}

DEFAULT_ORDER: Tuple[str, ...] = (
    "summary", "experience", "projects", "skills", "education", "certifications",
)
EDUCATION_FIRST: Tuple[str, ...] = (
    "summary", "education", "experience", "projects", "skills", "certifications",
)

# Education leads for a current student or a graduate of the last year who
# has less than this much work experience (US new-grad convention).
EDUCATION_FIRST_MAX_YEARS = 3.0
_ENROLLED_RE = re.compile(r"\b(expected|anticipated|present|current|ongoing|pursuing|in progress)\b", re.I)


def _clean(value: Any, allowed) -> Optional[str]:
    v = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {"1": "one", "one_page": "one", "2": "two", "two_page": "two",
               "standard": "compact", "dense": "compact", "roomy": "comfortable"}
    v = aliases.get(v, v)
    return v if v in allowed else None


def normalize_preferences(raw: Any) -> Dict[str, str]:
    """Only known keys with known values; anything else is dropped."""
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, str] = {}
    for key, allowed in (("page_length", PAGE_LENGTHS), ("density", DENSITIES),
                         ("accent", set(ACCENTS)), ("section_order", SECTION_ORDERS)):
        v = _clean(raw.get(key), allowed)
        if v:
            out[key] = v
    return out


def preferences_of(resume: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    prefs = (resume or {}).get("tailoring_preferences")
    return prefs if isinstance(prefs, dict) else {}


def apply_preferences(resume: Dict[str, Any], prefs: Dict[str, str]) -> Dict[str, Any]:
    """Merge normalized format preferences into ``tailoring_preferences``.

    Other keys there (wording locks, omissions) are kept untouched.
    """
    if not prefs or not isinstance(resume, dict):
        return resume
    merged = dict(preferences_of(resume))
    merged.update(prefs)
    resume["tailoring_preferences"] = merged
    return resume


def page_budget(resume: Optional[Dict[str, Any]]) -> int:
    raw = str((resume or {}).get("page_length") or preferences_of(resume).get("page_length") or "")
    return 2 if _clean(raw, PAGE_LENGTHS) == "two" else 1


def layout_overrides(prefs: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """LayoutSpec field overrides for these preferences ({} = the default spec)."""
    p = prefs or {}
    over: Dict[str, Any] = {}
    if _clean(p.get("density"), DENSITIES) == "comfortable":
        over.update(COMFORTABLE)
    accent = _clean(p.get("accent"), set(ACCENTS))
    if accent and ACCENTS[accent] != ACCENTS["black"]:
        over["accent_rgb"] = ACCENTS[accent]
    return over


def _recent_or_enrolled(dates: str, today: date) -> bool:
    if not dates or not str(dates).strip():
        return False
    if _ENROLLED_RE.search(str(dates)):
        return True
    from services.ats_vendor_checks import parse_end_date
    end = parse_end_date(str(dates))
    if not end:
        return False
    if end[0] == 9999:
        return True
    months_ago = (today.year - end[0]) * 12 + (today.month - end[1])
    return months_ago <= 12


def education_first(resume: Optional[Dict[str, Any]], today: Optional[date] = None) -> bool:
    """Should Education come before Experience?

    An explicit ``section_order`` wins. ``auto`` (the default) puts Education
    first for a current student or a graduate of the last 12 months with less
    than three years of work, the US new-grad convention, where the degree and
    graduation month are what a screener checks first.
    """
    resume = resume or {}
    order = _clean(preferences_of(resume).get("section_order"), SECTION_ORDERS) or "auto"
    if order == "education_first":
        return True
    if order == "experience_first":
        return False
    education = [e for e in (resume.get("education") or []) if isinstance(e, dict)]
    if not education:
        return False
    today = today or date.today()
    if not any(_recent_or_enrolled(str(e.get("dates") or ""), today) for e in education):
        return False
    from services.ats_vendor_checks import total_experience_years
    return total_experience_years(resume) < EDUCATION_FIRST_MAX_YEARS


def section_sequence(resume: Optional[Dict[str, Any]], today: Optional[date] = None) -> Tuple[str, ...]:
    return EDUCATION_FIRST if education_first(resume, today) else DEFAULT_ORDER


def restore_omitted(resume: Dict[str, Any]) -> Tuple[Dict[str, Any], int]:
    """Put back the bullets content selection dropped for space.

    Used when the user moves from one page to two: the lines are their own,
    already tailored and checked, and were removed only because of length.
    Each goes back to the end of the entry it came from (by section and
    entry index); selection then re-fits the page. Returns (resume, restored).
    """
    sel = resume.get("content_selection") if isinstance(resume, dict) else None
    omitted = (sel or {}).get("omitted_bullets") or []
    if not omitted:
        return resume, 0
    out = copy.deepcopy(resume)
    restored = 0
    for item in omitted:
        if not isinstance(item, dict):
            continue
        section = item.get("section")
        text = str(item.get("text") or "").strip()
        idx = item.get("entry_idx")
        entries = out.get(section) if section in ("experience", "projects") else None
        if not text or not isinstance(entries, list) or not isinstance(idx, int):
            continue
        if not (0 <= idx < len(entries)) or not isinstance(entries[idx], dict):
            continue
        bullets: List[str] = list(entries[idx].get("bullets") or [])
        if any(str(b).strip() == text for b in bullets):
            continue
        bullets.append(text)
        entries[idx]["bullets"] = bullets
        restored += 1
    return out, restored


def reformat(
    tailored: Dict[str, Any],
    prefs: Dict[str, str],
    *,
    jd_analysis: Optional[Dict[str, Any]] = None,
    renderer=None,
) -> Dict[str, Any]:
    """Re-fit a tailored résumé to new format choices. No model call.

    Moving to two pages first puts back the bullets selection dropped for
    space (the user's own, already tailored lines); content selection then
    fits the chosen length at the chosen spacing. The screener's view is
    refreshed for the new page, keeping the earlier evidence verdicts because
    the source résumé did not change.
    """
    from services.content_selection import select_resume_content

    working = copy.deepcopy(tailored)
    apply_preferences(working, normalize_preferences(prefs))
    if page_budget(working) >= 2:
        working, _restored = restore_omitted(working)
    prior_screener = working.get("screener") if isinstance(working.get("screener"), dict) else None
    result = select_resume_content(
        working,
        jd_analysis=jd_analysis or {},
        renderer=renderer,
        preferences=preferences_of(working),
        max_pages=page_budget(working),
    )
    if prior_screener and prior_screener.get("criteria"):
        try:
            from services import screener_sim
            if screener_sim.mode() != "off":
                result["screener"] = screener_sim.screen(
                    result, jd_analysis or {}, source=None, renderer=renderer,
                    use_llm=False, prior=prior_screener,
                )
        except Exception:  # advisory; the reformatted résumé still returns
            result["screener"] = {**prior_screener, "stale": True}
    return result
