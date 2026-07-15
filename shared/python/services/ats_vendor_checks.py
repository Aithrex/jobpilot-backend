"""
ATS Vendor Compatibility Checks — deterministic per-vendor parse checklists.

Replaces the LLM-simulated per-vendor "scanner scores" (Claude guesses coaxed
into the 85-95 band). Real ATS platforms don't emit a score; what actually
differs between them is PARSING STRICTNESS. Each vendor here gets a
pass/warn/fail checklist derived from the real pypdf round-trip
(`pdf_ats_check.check_pdf_parseability`) plus structural checks on the
tailored resume JSON — zero LLM involvement, fully reproducible.

Vendor quirks encoded (research-backed, 2025-2026):
- Workday: strict date validation — spelled-out month + full year parses,
  abbreviated/mixed formats break tenure math; contact must extract.
- Greenhouse: best parser overall BUT zero-tolerance for non-standard
  section labels; expects conventional section order.
- Lever: most aggressive sidebar/multi-column dropping — a clean linear
  text layer (no word-joins) is the proxy; expects reverse-chronological.
- iCIMS: strict post-parse validation — skills must survive extraction;
  glyph corruption routes the resume to manual review.
- Taleo: oldest/strictest — exact section labels, contact at the top of
  the body (header/footer content is dropped), glyph-sensitive.
- SmartRecruiters: semantic + keyword matching; needs the candidate name
  and a generally clean text layer.

Also hosts shared deterministic date helpers used by the scorer
(readability date-consistency + recency ordering).
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

VENDORS = ("workday", "greenhouse", "lever", "icims", "taleo", "smartrecruiters")

_MONTH_ORDER = (
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
)
_MONTH_NUM = {m: i + 1 for i, m in enumerate(_MONTH_ORDER)}

_FULL_MONTH_RE = re.compile(
    r"(?i)\b(january|february|march|april|may|june|july|august|september|"
    r"october|november|december)\s+((?:19|20)\d{2})\b"
)
_ABBR_MONTH_RE = re.compile(
    r"(?i)\b(jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)\.?\s+((?:19|20)\d{2})\b"
)
_NUMERIC_RE = re.compile(r"\b(0?[1-9]|1[0-2])[/\-]((?:19|20)\d{2})\b")
_BARE_YEAR_RE = re.compile(r"\b((?:19|20)\d{2})\b")
# Mirrors date_normalizer's present vocabulary.
_PRESENT_KEYWORDS = ("present", "current", "now", "ongoing", "today")


# ---------------------------------------------------------------------------
# Shared date helpers (also used by ResumeScorer)
# ---------------------------------------------------------------------------

def date_format_class(dates: str) -> Optional[str]:
    """Classify a date string's format family.

    Returns "full_month" | "abbr_month" | "numeric" | "year_only" | None.
    "May 2023" is ambiguous (full == abbr) — counted as full_month since
    the full-month pattern claims it first.
    """
    if not dates or not dates.strip():
        return None
    if _FULL_MONTH_RE.search(dates):
        return "full_month"
    if _ABBR_MONTH_RE.search(dates):
        return "abbr_month"
    if _NUMERIC_RE.search(dates):
        return "numeric"
    if _BARE_YEAR_RE.search(dates):
        return "year_only"
    return None


def dates_format_consistent(tailored: Dict[str, Any]) -> bool:
    """True when every experience + education date string belongs to ONE
    format class. Mixing "March 2022" with "05/2021" breaks strict
    validators (Workday). Entries with no recognizable date are skipped;
    an all-empty resume is vacuously consistent.
    """
    classes = set()
    for section in ("experience", "education"):
        for entry in tailored.get(section) or []:
            cls = date_format_class(entry.get("dates") or "")
            if cls:
                classes.add(cls)
    return len(classes) <= 1


def parse_end_date(dates: str) -> Optional[Tuple[int, int]]:
    """Best-effort (year, month) for the END of a date range.

    "Present"-style ranges sort newest: (9999, 12). Otherwise the LAST
    month+year token in the string is taken as the end. Bare-year-only
    strings resolve to (year, 12) so "2020 - 2022" orders sensibly.
    Returns None when nothing parseable is found.
    """
    if not dates or not dates.strip():
        return None
    low = dates.lower()
    if any(kw in low for kw in _PRESENT_KEYWORDS):
        return (9999, 12)

    candidates: List[Tuple[int, int, int]] = []  # (position, year, month)
    for m in _FULL_MONTH_RE.finditer(dates):
        candidates.append((m.start(), int(m.group(2)), _MONTH_NUM[m.group(1).lower()]))
    for m in _ABBR_MONTH_RE.finditer(dates):
        abbr = m.group(1).lower()[:3]
        month = next(
            (num for name, num in _MONTH_NUM.items() if name.startswith(abbr)), None
        )
        if month:
            candidates.append((m.start(), int(m.group(2)), month))
    for m in _NUMERIC_RE.finditer(dates):
        candidates.append((m.start(), int(m.group(2)), int(m.group(1))))

    if candidates:
        _, year, month = max(candidates, key=lambda c: c[0])
        return (year, month)

    years = _BARE_YEAR_RE.findall(dates)
    if years:
        return (int(years[-1]), 12)
    return None


def parse_start_date(dates: str) -> Optional[Tuple[int, int]]:
    """Best-effort (year, month) for the START of a date range — the FIRST
    month+year token in the string. Bare-year fallback resolves to (year, 1).
    """
    if not dates or not dates.strip():
        return None
    candidates: List[Tuple[int, int, int]] = []  # (position, year, month)
    for m in _FULL_MONTH_RE.finditer(dates):
        candidates.append((m.start(), int(m.group(2)), _MONTH_NUM[m.group(1).lower()]))
    for m in _ABBR_MONTH_RE.finditer(dates):
        abbr = m.group(1).lower()[:3]
        month = next(
            (num for name, num in _MONTH_NUM.items() if name.startswith(abbr)), None
        )
        if month:
            candidates.append((m.start(), int(m.group(2)), month))
    for m in _NUMERIC_RE.finditer(dates):
        candidates.append((m.start(), int(m.group(2)), int(m.group(1))))
    if candidates:
        _, year, month = min(candidates, key=lambda c: c[0])
        return (year, month)
    years = _BARE_YEAR_RE.findall(dates)
    if years:
        return (int(years[0]), 1)
    return None


def total_experience_years(structured: Dict[str, Any]) -> float:
    """Total professional experience in years, from the immutable experience
    date ranges. Overlapping roles are merged so concurrent jobs don't double
    count; "Present" end dates count up to the current month. Unparseable
    entries are skipped; no experience → 0.0.
    """
    from datetime import date

    now = date.today()
    now_idx = now.year * 12 + (now.month - 1)

    intervals: List[Tuple[int, int]] = []  # (start_month_idx, end_month_idx)
    for exp in structured.get("experience") or []:
        dates = exp.get("dates") or ""
        start = parse_start_date(dates)
        end = parse_end_date(dates)
        if not start or not end:
            continue
        start_idx = start[0] * 12 + (start[1] - 1)
        end_idx = now_idx if end[0] == 9999 else end[0] * 12 + (end[1] - 1)
        end_idx = min(end_idx, now_idx)  # future-dated ends clamp to now
        if end_idx >= start_idx:
            intervals.append((start_idx, end_idx))

    if not intervals:
        return 0.0

    merged: List[List[int]] = []
    for s, e in sorted(intervals):
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    total_months = sum(e - s + 1 for s, e in merged)
    return round(total_months / 12.0, 2)


def is_reverse_chronological(tailored: Dict[str, Any]) -> bool:
    """True when experience entries with parseable end dates are ordered
    newest-first. Unparseable entries are skipped; <2 parseable dates is
    vacuously ordered.
    """
    keys = [
        parse_end_date(e.get("dates") or "")
        for e in tailored.get("experience") or []
    ]
    known = [k for k in keys if k]
    if len(known) < 2:
        return True
    return all(known[i] >= known[i + 1] for i in range(len(known) - 1))


# ---------------------------------------------------------------------------
# Vendor checklists
# ---------------------------------------------------------------------------

def _check(check_id: str, label: str, ok: bool, hard: bool = True) -> Dict[str, Any]:
    return {"id": check_id, "label": label, "status": "pass" if ok else "fail", "_hard": hard}


def _vendor_status(checks: List[Dict[str, Any]]) -> str:
    """fail if any hard check fails; warn if only soft checks fail; else pass."""
    hard_fail = any(c["status"] == "fail" and c["_hard"] for c in checks)
    soft_fail = any(c["status"] == "fail" and not c["_hard"] for c in checks)
    if hard_fail:
        return "fail"
    if soft_fail:
        return "warn"
    return "pass"


def build_vendor_compat(
    pdf_check: Optional[Dict[str, Any]],
    tailored: Dict[str, Any],
) -> Dict[str, Any]:
    """Build the per-vendor compat block from real parse evidence.

    `pdf_check` is the dict returned by `check_pdf_parseability`, or None
    when the PDF failed to render — in which case every vendor fails loudly
    (a resume that can't render can't be parsed by anyone).
    """
    if not pdf_check:
        failed = {
            "status": "fail",
            "checks": [{
                "id": "pdf_render",
                "label": "Resume PDF failed to render or parse",
                "status": "fail",
            }],
        }
        return {v: dict(failed) for v in VENDORS}

    c = pdf_check.get("checks") or {}
    score = int(pdf_check.get("pdf_parseable_score") or 0)

    dates_total = int(c.get("experience_dates_total") or 0)
    dates_ok = (
        int(c.get("experience_dates_parseable") or 0) == dates_total
    )
    skills_total = int(c.get("skills_keywords_total") or 0)
    skills_cov_ok = (
        (int(c.get("skills_keywords_found") or 0) / skills_total >= 0.9)
        if skills_total else True
    )
    sections_ok = not (c.get("sections_missing") or [])
    consistent_dates = dates_format_consistent(tailored)
    reverse_chron = is_reverse_chronological(tailored)

    vendor_checks: Dict[str, List[Dict[str, Any]]] = {
        "workday": [
            _check("dates_parseable",
                   "Experience dates use ATS-standard formats (tenure math)",
                   dates_ok),
            _check("dates_consistent",
                   "One consistent date format throughout",
                   consistent_dates),
            _check("contact_extracted",
                   "Email and phone extracted from body text",
                   bool(c.get("contact_email_found")) and bool(c.get("contact_phone_found"))),
        ],
        "greenhouse": [
            _check("standard_sections",
                   "All standard section headings recognized",
                   sections_ok),
            _check("sections_in_order",
                   "Sections in conventional order (Summary → Experience → Skills → Education)",
                   bool(c.get("sections_in_order")), hard=False),
        ],
        "lever": [
            _check("linear_text",
                   "Single-column linear text layer (no sidebar content to drop)",
                   bool(c.get("extraction_ok")) and not (c.get("word_join_issues") or [])),
            _check("reverse_chronological",
                   "Experience in reverse-chronological order",
                   reverse_chron, hard=False),
        ],
        "icims": [
            _check("skills_extracted",
                   "At least 90% of listed skills survive PDF extraction",
                   skills_cov_ok),
            _check("no_glyph_corruption",
                   "No corrupted glyphs that trigger manual-review fallback",
                   bool(c.get("no_glyph_corruption"))),
        ],
        "taleo": [
            _check("no_glyph_corruption",
                   "No corrupted glyphs (strictest parser of the six)",
                   bool(c.get("no_glyph_corruption"))),
            _check("standard_sections",
                   "Exact standard section labels present",
                   sections_ok),
            _check("contact_at_top",
                   "Contact info at top of body (not header/footer)",
                   bool(c.get("contact_at_top"))),
        ],
        "smartrecruiters": [
            _check("name_extracted",
                   "Candidate name extracted from text layer",
                   bool(c.get("contact_name_found"))),
            _check("clean_parse",
                   "Overall text layer parses cleanly (parseability ≥ 85)",
                   score >= 85, hard=False),
        ],
    }

    result: Dict[str, Any] = {}
    for vendor, checks in vendor_checks.items():
        status = _vendor_status(checks)
        result[vendor] = {
            "status": status,
            "checks": [{k: v for k, v in chk.items() if k != "_hard"} for chk in checks],
        }
    return result
