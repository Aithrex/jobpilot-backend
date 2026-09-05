"""Date-interval helpers for duration requirements (assessment-v2).

Union evidence-backed intervals only. Overlapping roles are not double-counted.
Full job tenure does not establish continuous use of every tool in that job.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Iterable, List, Optional, Sequence, Tuple


@dataclass(frozen=True)
class DateInterval:
    start: date
    end: date  # inclusive-ish; open-ended uses today-ish caller-supplied end
    entity: str = ""
    capability: str = ""
    uncertain: bool = False

    def months(self) -> float:
        if self.end < self.start:
            return 0.0
        return (self.end.year - self.start.year) * 12 + (self.end.month - self.start.month) + 1


_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}


def parse_role_dates(raw: str, *, today: Optional[date] = None) -> Tuple[Optional[date], Optional[date], bool]:
    """Parse common resume date ranges. Returns (start, end, uncertain)."""
    today = today or date.today()
    text = (raw or "").strip()
    if not text:
        return None, None, True
    uncertain = bool(re.search(r"\b(?:approx|~|circa|around)\b", text, re.I))
    # YYYY - Present / YYYY–YYYY
    m = re.search(
        r"(?P<y1>19|20)\d{2}(?:\s*[-/]\s*(?P<m1>[A-Za-z]{3,9}|\d{1,2}))?"
        r"\s*[-–—to]+\s*"
        r"(?P<y2>Present|Current|(?:19|20)\d{2})(?:\s*[-/]\s*(?P<m2>[A-Za-z]{3,9}|\d{1,2}))?",
        text,
        re.I,
    )
    if not m:
        # Single year
        y = re.search(r"((?:19|20)\d{2})", text)
        if y:
            year = int(y.group(1))
            return date(year, 1, 1), date(year, 12, 31), True
        return None, None, True
    y1 = int(re.search(r"(?:19|20)\d{2}", m.group(0)).group(0))
    # Re-parse more carefully
    parts = re.split(r"\s*[-–—to]+\s*", text, maxsplit=1, flags=re.I)
    start = _parse_one(parts[0], default_month=1, default_day=1)
    end_raw = parts[1] if len(parts) > 1 else ""
    if re.search(r"present|current", end_raw, re.I):
        end = today
    else:
        end = _parse_one(end_raw, default_month=12, default_day=28)
    if not start or not end:
        return start, end, True
    return start, end, uncertain


def _parse_one(raw: str, *, default_month: int, default_day: int) -> Optional[date]:
    raw = (raw or "").strip()
    ym = re.search(r"([A-Za-z]{3,9})\s*((?:19|20)\d{2})", raw)
    if ym:
        mon = _MONTHS.get(ym.group(1).lower()[:3]) or _MONTHS.get(ym.group(1).lower())
        if mon:
            return date(int(ym.group(2)), mon, min(default_day, 28))
    ym2 = re.search(r"((?:19|20)\d{2})\s*[-/]\s*(\d{1,2})", raw)
    if ym2:
        return date(int(ym2.group(1)), int(ym2.group(2)), min(default_day, 28))
    y = re.search(r"((?:19|20)\d{2})", raw)
    if y:
        return date(int(y.group(1)), default_month, min(default_day, 28))
    return None


def merge_intervals(intervals: Sequence[DateInterval]) -> List[DateInterval]:
    """Union overlapping intervals (same capability optionally filtered by caller)."""
    items = sorted(
        [i for i in intervals if i.start and i.end and i.end >= i.start],
        key=lambda i: (i.start, i.end),
    )
    if not items:
        return []
    out = [items[0]]
    for cur in items[1:]:
        last = out[-1]
        # Overlap or abut (within 1 month)
        if cur.start <= date(last.end.year, last.end.month, 28) or (
            cur.start.year == last.end.year and cur.start.month <= last.end.month + 1
        ) or (cur.start.year == last.end.year + 1 and last.end.month == 12 and cur.start.month == 1):
            # Merge — do not double-count overlap
            new_end = max(last.end, cur.end)
            out[-1] = DateInterval(
                start=last.start,
                end=new_end,
                entity=last.entity if last.entity == cur.entity else f"{last.entity}|{cur.entity}",
                capability=last.capability or cur.capability,
                uncertain=last.uncertain or cur.uncertain,
            )
        else:
            out.append(cur)
    return out


def total_months(intervals: Sequence[DateInterval]) -> float:
    return sum(i.months() for i in merge_intervals(intervals))


def tool_scoped_intervals(
    *,
    roles: Sequence[dict],
    capability: str,
    today: Optional[date] = None,
) -> List[DateInterval]:
    """Intervals only for roles that evidence the capability in bullets/skills — not bare tenure.

    A role with dates 2018-2024 that merely lists the company does NOT credit
    6 years of tool use unless bullets/context mention the capability.
    """
    from services.artifact_assessment import token_boundary_match

    today = today or date.today()
    out: List[DateInterval] = []
    for i, role in enumerate(roles or []):
        blobs = [str(b) for b in (role.get("bullets") or [])]
        blobs.append(str(role.get("title") or ""))
        text = "\n".join(blobs)
        if not token_boundary_match(text, capability):
            continue
        # Prefer explicit duration near the tool over full role tenure.
        local = re.search(
            rf"(?P<span>\d+\+?\s*(?:years?|months?)).{{0,40}}{re.escape(capability)}"
            rf"|{re.escape(capability)}.{{0,40}}(?P<span2>\d+\+?\s*(?:years?|months?))",
            text,
            re.I,
        )
        if local:
            span = local.group("span") or local.group("span2") or ""
            months = _span_to_months(span)
            if months:
                # Synthetic interval from today backward — marked uncertain if no calendar dates
                end = today
                # Approximate start
                y = months // 12
                m = int(months % 12)
                start_month = end.month - m
                start_year = end.year - y
                while start_month <= 0:
                    start_month += 12
                    start_year -= 1
                out.append(DateInterval(
                    start=date(start_year, max(1, start_month), 1),
                    end=end,
                    entity=f"experience.{i}",
                    capability=capability,
                    uncertain=True,
                ))
                continue
        start, end, uncertain = parse_role_dates(str(role.get("dates") or ""), today=today)
        if start and end:
            # Only credit role tenure when tool is evidenced in that role —
            # still mark as tenure-based so callers can require explicit duration.
            out.append(DateInterval(
                start=start, end=end, entity=f"experience.{i}",
                capability=capability, uncertain=True,  # tenure≠proven continuous use
            ))
    return out


def _span_to_months(span: str) -> float:
    m = re.search(r"(\d+)\+?\s*(year|month)", span or "", re.I)
    if not m:
        return 0.0
    n = float(m.group(1))
    return n * 12 if m.group(2).lower().startswith("year") else n


def meets_duration_months(
    intervals: Sequence[DateInterval],
    required_months: float,
    *,
    allow_uncertain: bool = False,
) -> Tuple[bool, float, bool]:
    """Return (met, total_months, any_uncertain). Uncertain intervals don't meet hard requirements."""
    merged = merge_intervals(intervals)
    if not allow_uncertain and any(i.uncertain for i in merged):
        # Conservative: uncertain tenure-based credit does not satisfy explicit duration
        total = total_months([i for i in merged if not i.uncertain])
        return total >= required_months, total_months(merged), True
    total = total_months(merged)
    return total >= required_months, total, any(i.uncertain for i in merged)
