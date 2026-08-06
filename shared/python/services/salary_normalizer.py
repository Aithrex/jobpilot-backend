"""Compensation extraction and normalization for job postings.

The inventory stored no salary at all: `job_postings` had no field for it and
the feed row hard-coded `"salary": ""`. So salary filtering was not weak, it was
impossible - there was nothing to filter on. This module is the missing half.

Two inputs, in order of trust:

1. STRUCTURED, whatever shape the ATS used. There is no single convention -
   Lever nests `{min, max, currency, interval}`, Greenhouse publishes cents in
   `pay_input_ranges`, Ashby hands back a rendered string like "$120K - $150K".
   Rather than encode one vendor's field names (and quietly capture nothing the
   day a vendor renames them), `parse` accepts the SHAPES: a mapping with
   min/max-ish keys, a list of such mappings, or a string.
2. TEXT, scanned out of the description. Marked `confidence="text"` so a caller
   can prefer a stated range over a parsed one, because a JD mentioning
   "$150,000" may be describing a bonus, a budget or last year's band.

Everything is annualized (`annual_min` / `annual_max`) so an hourly contract and
a salaried role are comparable by one number. That is the whole point: a filter
that cannot compare $60/hr to $120,000/yr is not a salary filter.

Deliberately conservative. A wrong number here is worse than no number - it
would sort real jobs out of a feed - so anything that fails a plausibility
check returns None rather than a guess.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

# Hours/periods per year. `day` and `week` assume a working calendar, not a
# literal one, which is what an employer means by "per day".
_PERIOD_MULTIPLIER: Dict[str, float] = {
    "hour": 2080.0,     # 40h x 52w
    "day": 260.0,
    "week": 52.0,
    "biweek": 26.0,
    "semimonth": 24.0,
    "month": 12.0,
    "quarter": 4.0,
    "year": 1.0,
}

_PERIOD_ALIASES = (
    ("hour", ("hour", "hourly", "hr", "per-hour", "perhour", "/hr", "an hour")),
    ("day", ("day", "daily", "per-day", "per diem", "perday")),
    ("biweek", ("biweekly", "bi-weekly", "fortnight")),
    ("semimonth", ("semimonthly", "semi-monthly")),
    ("week", ("week", "weekly", "per-week", "perweek", "wk")),
    ("month", ("month", "monthly", "per-month", "permonth", "mo")),
    ("quarter", ("quarter", "quarterly")),
    ("year", ("year", "yearly", "annual", "annually", "annum", "salary", "pa", "p.a")),
)

_CURRENCY_SYMBOL = {
    "$": "USD", "US$": "USD", "usd": "USD",
    "£": "GBP", "gbp": "GBP",
    "€": "EUR", "eur": "EUR",
    "₹": "INR", "inr": "INR", "rs": "INR", "rs.": "INR",
    "c$": "CAD", "cad": "CAD",
    "a$": "AUD", "aud": "AUD",
    "¥": "JPY", "jpy": "JPY",
}

# Plausibility gates, applied AFTER annualizing. Anything outside is a parse
# error dressed as a number - a stray year ("2024"), a headcount, an equity
# figure, an INR band read as USD.
_MIN_PLAUSIBLE_ANNUAL = 5_000.0
_MAX_PLAUSIBLE_ANNUAL = 10_000_000.0
# Below this an annual figure is almost certainly a typo or a different unit;
# above it an "hourly" rate is not hourly.
_MAX_PLAUSIBLE_HOURLY = 2_000.0

_AMOUNT = r"(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*([kKmM])?"
# "$120,000 - $150,000", "120K–150K", "$60 to $75"
_RANGE_RE = re.compile(
    r"(?P<cur>[$£€₹]|US\$|C\$|A\$)?\s*" + _AMOUNT +
    r"\s*(?:-|--|–|—|to|and)\s*" +
    r"(?:[$£€₹]|US\$|C\$|A\$)?\s*" + _AMOUNT,
    re.IGNORECASE,
)
_SINGLE_RE = re.compile(r"(?P<cur>[$£€₹]|US\$|C\$|A\$)\s*" + _AMOUNT)
_PERIOD_NEAR_RE = re.compile(
    r"(per\s+|an?\s+|/\s*|a\s+)?(hour|hourly|hr|year|yearly|annual(?:ly)?|annum|"
    r"month(?:ly)?|week(?:ly)?|day|daily)\b",
    re.IGNORECASE,
)

_MIN_KEYS = ("min", "minimum", "from", "low", "lower", "min_value", "minvalue",
             "min_amount", "start", "min_cents", "mincents")
_MAX_KEYS = ("max", "maximum", "to", "high", "upper", "max_value", "maxvalue",
             "max_amount", "end", "max_cents", "maxcents")
_CURRENCY_KEYS = ("currency", "currency_type", "currencycode", "currency_code", "unit")
_PERIOD_KEYS = ("interval", "period", "frequency", "unit", "pay_period",
                "payperiod", "type", "cadence")


def _period_of(value: Any, default: Optional[str] = None) -> Optional[str]:
    blob = str(value or "").strip().lower()
    if not blob:
        return default
    for canonical, aliases in _PERIOD_ALIASES:
        for alias in aliases:
            if alias in blob:
                return canonical
    return default


def _currency_of(value: Any, default: str = "USD") -> str:
    blob = str(value or "").strip().lower()
    if not blob:
        return default
    for token, code in _CURRENCY_SYMBOL.items():
        if token in blob:
            return code
    if len(blob) == 3 and blob.isalpha():
        return blob.upper()
    return default


def _number(value: Any, suffix: str = "", *, cents: bool = False) -> Optional[float]:
    try:
        n = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    s = (suffix or "").lower()
    if s == "k":
        n *= 1_000
    elif s == "m":
        n *= 1_000_000
    if cents:
        n /= 100.0
    return n


def _get(mapping: Dict[str, Any], keys) -> Any:
    lowered = {str(k).lower(): v for k, v in mapping.items()}
    for k in keys:
        if k in lowered and lowered[k] not in (None, ""):
            return lowered[k]
    return None


def _finalize(lo: Optional[float], hi: Optional[float], currency: str,
              period: str, *, confidence: str, raw: str) -> Optional[Dict[str, Any]]:
    """Order, annualize, sanity-check. Returns None rather than a bad number."""
    if lo is None and hi is None:
        return None
    if lo is not None and hi is not None and lo > hi:
        lo, hi = hi, lo
    mult = _PERIOD_MULTIPLIER.get(period)
    if not mult:
        return None
    # An "hourly" rate in the hundreds of thousands is a mislabelled period, not
    # a very good contract.
    if period == "hour":
        for v in (lo, hi):
            if v is not None and v > _MAX_PLAUSIBLE_HOURLY:
                return None
    annual_lo = lo * mult if lo is not None else None
    annual_hi = hi * mult if hi is not None else None
    for v in (annual_lo, annual_hi):
        if v is not None and not (_MIN_PLAUSIBLE_ANNUAL <= v <= _MAX_PLAUSIBLE_ANNUAL):
            return None
    return {
        "min": lo,
        "max": hi,
        "currency": currency,
        "period": period,
        "annual_min": annual_lo,
        "annual_max": annual_hi,
        "confidence": confidence,
        "raw": str(raw or "")[:200],
    }


def _from_mapping(data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    cents = any("cents" in str(k).lower() for k in data.keys())
    lo = _number(_get(data, _MIN_KEYS), cents=cents)
    hi = _number(_get(data, _MAX_KEYS), cents=cents)
    if lo is None and hi is None:
        # Some boards publish a single `value` / `amount`.
        lo = _number(_get(data, ("value", "amount", "salary", "pay", "rate")), cents=cents)
    currency = _currency_of(_get(data, _CURRENCY_KEYS))
    period = _period_of(_get(data, _PERIOD_KEYS), default="year") or "year"
    return _finalize(lo, hi, currency, period, confidence="structured",
                     raw=str(data)[:200])


def _from_text(blob: str, *, confidence: str) -> Optional[Dict[str, Any]]:
    text = str(blob or "")
    if not text:
        return None
    match = _RANGE_RE.search(text)
    if match:
        groups = match.groups()
        # groups: cur, n1, suf1, n2, suf2
        lo = _number(groups[1], groups[2] or "")
        hi = _number(groups[3], groups[4] or "")
        currency = _currency_of(groups[0])
        tail = text[match.end(): match.end() + 40]
        period = _period_of(tail) or _period_of(text[max(0, match.start() - 40):match.start()]) \
            or ("hour" if (lo is not None and lo < 500) else "year")
        return _finalize(lo, hi, currency, period, confidence=confidence,
                         raw=match.group(0))
    match = _SINGLE_RE.search(text)
    if match:
        lo = _number(match.group(2), match.group(3) or "")
        currency = _currency_of(match.group("cur"))
        tail = text[match.end(): match.end() + 40]
        period = _period_of(tail) or ("hour" if (lo is not None and lo < 500) else "year")
        return _finalize(lo, None, currency, period, confidence=confidence,
                         raw=match.group(0))
    return None


def parse(raw: Any = None, *, text: str = "") -> Optional[Dict[str, Any]]:
    """Best available compensation, or None.

    `raw` is whatever the ATS gave us (mapping, list of mappings, or string);
    `text` is the description, used only as a fallback and marked as such.
    """
    if isinstance(raw, dict):
        got = _from_mapping(raw)
        if got:
            return got
        # Some payloads nest the real thing one level down.
        for value in raw.values():
            if isinstance(value, (dict, list)):
                got = parse(value)
                if got:
                    return got
    elif isinstance(raw, (list, tuple)):
        for item in raw:
            got = parse(item)
            if got:
                return got
    elif isinstance(raw, str) and raw.strip():
        got = _from_text(raw, confidence="structured")
        if got:
            return got

    if text:
        return _from_text(text[:4000], confidence="text")
    return None


_PERIOD_LABEL = {
    "hour": "an hour", "day": "a day", "week": "a week", "biweek": "biweekly",
    "semimonth": "semimonthly", "month": "a month", "quarter": "a quarter",
    "year": "a year",
}
_SYMBOL = {"USD": "$", "GBP": "£", "EUR": "€", "INR": "₹", "CAD": "C$",
           "AUD": "A$", "JPY": "¥"}


def display(comp: Optional[Dict[str, Any]]) -> str:
    """Human string for the card. Empty when unknown - never "Not disclosed",
    because that is the FE's wording decision, not this module's."""
    if not comp:
        return ""
    sym = _SYMBOL.get(str(comp.get("currency") or "USD"), "")
    period = _PERIOD_LABEL.get(str(comp.get("period") or "year"), "")

    def fmt(v: Optional[float]) -> str:
        if v is None:
            return ""
        if v >= 1000:
            return f"{sym}{v:,.0f}"
        # Cents are all-or-nothing. Stripping trailing zeros turned $70.40 into
        # "$70.4", which reads as a typo on a money figure - but $70.00 really
        # should render as "$70", so the test is on the CENTS being zero rather
        # than on the last character.
        if abs(v - round(v)) < 0.005:
            return f"{sym}{v:,.0f}"
        return f"{sym}{v:,.2f}"

    lo, hi = fmt(comp.get("min")), fmt(comp.get("max"))
    if lo and hi and lo != hi:
        return f"{lo} - {hi} {period}".strip()
    return f"{lo or hi} {period}".strip()


def normalize(raw: Any = None, *, text: str = "") -> Optional[Dict[str, Any]]:
    """`parse` plus the rendered display string, ready to store."""
    comp = parse(raw, text=text)
    if not comp:
        return None
    comp["display"] = display(comp)
    return comp
