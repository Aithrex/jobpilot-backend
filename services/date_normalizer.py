"""
Date Normalizer — normalizes experience and education date strings to ATS-friendly format.

Pure Python, no AI calls. Converts varied date formats to consistent
"Month YYYY – Present" style (e.g., "December 2024 – Present", "January 2020 – May 2022").

Handles: "Dec 2024 - current", "2024-12 to present", "01/2020 - 05/2022",
         "May 2021 – Present", "2019 – 2021", "August 2023 - now", etc.
"""
import re
import logging
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# Regex patterns (aligned with resume_parser.py)
_YEAR_PATTERN = re.compile(r"(20\d{2}|19\d{2})")
_MONTH_ABBR_PATTERN = re.compile(r"(?i)\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*")
_NUMERIC_MONTH_YEAR = re.compile(r"\b(0?[1-9]|1[0-2])[/\-]((?:20|19)\d{2})")
_YEAR_DASH_MONTH = re.compile(r"((?:20|19)\d{2})[/\-](0?[1-9]|1[0-2])")
_PRESENT_KEYWORDS = frozenset({"present", "current", "now", "ongoing", "today"})

_MONTH_FULL = {
    "jan": "January", "feb": "February", "mar": "March",
    "apr": "April", "may": "May", "jun": "June",
    "jul": "July", "aug": "August", "sep": "September",
    "oct": "October", "nov": "November", "dec": "December",
}

_MONTH_NUM_TO_FULL = {
    1: "January", 2: "February", 3: "March", 4: "April",
    5: "May", 6: "June", 7: "July", 8: "August",
    9: "September", 10: "October", 11: "November", 12: "December",
}

_SEPARATOR = " - "  # ASCII hyphen \u2014 every major ATS (Workday, Greenhouse, iCIMS,
# Taleo, Lever) parses ' - ' reliably. The en-dash ' \u2013 ' previously here
# round-trips inconsistently through pypdf, breaking date parseability checks.


def normalize_dates(tailored: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize all experience and education date strings to 'Month YYYY – Present' format.

    Modifies the tailored dict in-place and returns it.
    If a date string cannot be parsed, it is left unchanged.
    """
    for exp in tailored.get("experience", []):
        raw = exp.get("dates", "")
        if raw:
            normalized = _normalize_date_string(raw)
            if normalized:
                exp["dates"] = normalized

    for edu in tailored.get("education", []):
        raw = edu.get("dates", "")
        if raw:
            normalized = _normalize_date_string(raw)
            if normalized:
                edu["dates"] = normalized

    return tailored


def _normalize_date_string(raw: str) -> Optional[str]:
    """Parse a date string and return normalized format, or None if unparseable."""
    if not raw or not raw.strip():
        return None

    raw_stripped = raw.strip()
    raw_lower = raw_stripped.lower()

    # Check if end is a "present" keyword
    is_present = any(kw in raw_lower for kw in _PRESENT_KEYWORDS)

    # Try to extract two date components (start and end)
    start, end = _extract_date_components(raw_stripped, is_present)

    if start is None:
        return None  # Unparseable — keep original

    if is_present:
        return f"{start}{_SEPARATOR}Present"
    elif end is not None:
        return f"{start}{_SEPARATOR}{end}"
    else:
        # Single date (e.g., graduation date "May 2024")
        return start


def _extract_date_components(
    raw: str, is_present: bool
) -> Tuple[Optional[str], Optional[str]]:
    """Extract start and end date components from a raw date string.

    Returns (start_str, end_str) where each is "Month YYYY" or "YYYY",
    or (None, None) if unparseable.
    """
    # Strategy 1: numeric month/year patterns like "01/2020 - 05/2022" or "2024-12"
    numeric_dates = _NUMERIC_MONTH_YEAR.findall(raw)
    year_dash_month = _YEAR_DASH_MONTH.findall(raw)

    # Strategy 2: abbreviated/full month names like "Dec 2024", "May 2021"
    month_matches = list(_MONTH_ABBR_PATTERN.finditer(raw))
    year_matches = _YEAR_PATTERN.findall(raw)

    # Collect all (month_num, year) pairs found
    date_pairs = []

    # From numeric MM/YYYY patterns
    for month_str, year_str in numeric_dates:
        date_pairs.append((int(month_str), int(year_str)))

    # From YYYY-MM patterns
    for year_str, month_str in year_dash_month:
        pair = (int(month_str), int(year_str))
        if pair not in date_pairs:
            date_pairs.append(pair)

    # From text month + year combinations
    if month_matches and year_matches:
        # Match months to years positionally
        for i, m_match in enumerate(month_matches):
            abbr = m_match.group(0).lower()[:3]
            if abbr in _MONTH_FULL:
                month_num = list(_MONTH_FULL.keys()).index(abbr) + 1
                # Find the closest year that comes after or at this position
                year_idx = min(i, len(year_matches) - 1)
                year_val = int(year_matches[year_idx])
                pair = (month_num, year_val)
                if pair not in date_pairs:
                    date_pairs.append(pair)

    if date_pairs:
        # Sort by year then month to get chronological order
        date_pairs.sort(key=lambda p: (p[1], p[0]))

        start_month, start_year = date_pairs[0]
        start_str = f"{_MONTH_NUM_TO_FULL[start_month]} {start_year}"

        if is_present:
            return start_str, None  # End will be "Present"
        elif len(date_pairs) >= 2:
            end_month, end_year = date_pairs[-1]
            # Don't use the same date for both start and end
            if (end_month, end_year) != (start_month, start_year):
                end_str = f"{_MONTH_NUM_TO_FULL[end_month]} {end_year}"
                return start_str, end_str
            else:
                return start_str, None
        else:
            return start_str, None

    # Fallback: year-only (e.g., "2019 – 2021")
    if year_matches:
        start_year = year_matches[0]
        if is_present:
            return start_year, None  # "2024 – Present"
        elif len(year_matches) >= 2 and year_matches[0] != year_matches[-1]:
            return start_year, year_matches[-1]
        else:
            return start_year, None

    return None, None
