"""Shared helpers for provider modules.

Kept separate from `base.py` so the contract stays readable, and separate from
`workday_jobs_service._html_to_text` so importing one provider does not drag the
whole Workday scoring stack (resume profiles, regex matchers, Mongo caches) into
a cold start.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

# ---------------------------------------------------------------------------
# HTML -> text
# ---------------------------------------------------------------------------

_BLOCK_TAGS = ("p", "div", "li", "br", "tr", "h1", "h2", "h3", "h4", "h5",
               "h6", "section", "article")
_SCRIPT_RE = re.compile(r"<(script|style|noscript)\b[^>]*>.*?</\1>",
                        re.IGNORECASE | re.DOTALL)
_BLOCK_RE = re.compile(r"</?(?:%s)\b[^>]*>" % "|".join(_BLOCK_TAGS),
                       re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\x0b\f\r]+")
_NL_RE = re.compile(r"\n{3,}")


def html_to_text(value: Any) -> str:
    """Readable plain text from an HTML fragment.

    lxml when available (correct entity handling, tolerant of the malformed
    markup ATS vendors ship), regex otherwise. Block tags become newlines so
    bullet lists survive as lines rather than collapsing into one run-on
    sentence - requirement extraction downstream reads much better for it.
    """
    raw = str(value or "")
    if not raw:
        return ""
    if "<" not in raw:
        return _tidy(raw)
    try:
        from lxml import html as lxml_html

        cleaned = _SCRIPT_RE.sub(" ", raw)
        doc = lxml_html.fromstring(cleaned)
        for bad in doc.xpath("//script|//style|//noscript"):
            bad.getparent().remove(bad)
        text = doc.text_content()
        # lxml drops block boundaries; re-derive them from the source so list
        # items do not run together.
        if "\n" not in text:
            text = _TAG_RE.sub(" ", _BLOCK_RE.sub("\n", cleaned))
            import html as _html
            text = _html.unescape(text)
        return _tidy(text)
    except Exception:
        pass
    text = _SCRIPT_RE.sub(" ", raw)
    text = _BLOCK_RE.sub("\n", text)
    text = _TAG_RE.sub(" ", text)
    try:
        import html as _html
        text = _html.unescape(text)
    except Exception:
        pass
    return _tidy(text)


def _tidy(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("\u00a0", " ")
    text = _WS_RE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    return _NL_RE.sub("\n\n", text).strip()


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------

_ISO_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")
_US_DATE_RE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")
_MONTH_NAME_RE = re.compile(
    r"^([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})$")
_MONTHS = {m.lower(): i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"], start=1)}
_MONTHS.update({m[:3]: i for m, i in list(_MONTHS.items())})

_RELATIVE_RE = re.compile(
    r"(?:posted\s+)?(?:(today|yesterday)|(\d+)\+?\s*(day|week|month|hour|minute)s?\s*(?:ago)?)",
    re.IGNORECASE)


def to_iso(value: Any) -> str:
    """Best-effort ISO-8601 instant from whatever a vendor published.

    Returns "" rather than guessing. ADR-001: a missing date must stay missing;
    substituting now() re-dates months-old requisitions as fresh, which is worse
    than showing no date at all.
    """
    if value in (None, "", 0):
        return ""
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    if isinstance(value, (int, float)):
        return from_epoch(value)

    text = str(value).strip()
    if not text:
        return ""

    if _ISO_DATE_RE.match(text):
        # Already ISO-ish. Add a time component when it is a bare date so
        # lexicographic comparison against other ISO instants is sound.
        if "T" not in text:
            return f"{text[:10]}T00:00:00+00:00"
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if not dt.tzinfo:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).isoformat()
        except ValueError:
            return f"{text[:10]}T00:00:00+00:00"

    m = _US_DATE_RE.match(text)
    if m:
        mm, dd, yy = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            return datetime(yy, mm, dd, tzinfo=timezone.utc).isoformat()
        except ValueError:
            return ""

    m = _MONTH_NAME_RE.match(re.sub(r"\s+", " ", text))
    if m:
        month = _MONTHS.get(m.group(1).lower())
        if month:
            try:
                return datetime(int(m.group(3)), month, int(m.group(2)),
                                tzinfo=timezone.utc).isoformat()
            except ValueError:
                return ""
    return ""


def from_epoch(ts: Any) -> str:
    """Epoch seconds or milliseconds -> ISO. Vendors ship both."""
    try:
        n = float(ts)
    except (TypeError, ValueError):
        return ""
    if n <= 0:
        return ""
    if n > 1e11:      # milliseconds
        n /= 1000.0
    try:
        return datetime.fromtimestamp(n, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return ""


def from_relative(text: Any, *, now: Optional[datetime] = None) -> str:
    """"Posted 3 Days Ago" -> an approximate ISO instant.

    ADR-001 CONFIDENCE_RELATIVE: the caller must record that this is derived,
    not published. Returns "" when nothing parses.
    """
    raw = str(text or "").strip()
    if not raw:
        return ""
    base = now or datetime.now(timezone.utc)
    m = _RELATIVE_RE.search(raw)
    if not m:
        return ""
    if m.group(1):
        word = m.group(1).lower()
        delta = timedelta(days=0 if word == "today" else 1)
    else:
        try:
            n = int(m.group(2))
        except (TypeError, ValueError):
            return ""
        unit = (m.group(3) or "day").lower()
        delta = {
            "minute": timedelta(minutes=n),
            "hour": timedelta(hours=n),
            "day": timedelta(days=n),
            "week": timedelta(weeks=n),
            "month": timedelta(days=30 * n),
        }.get(unit, timedelta(days=n))
    return (base - delta).isoformat()


# ---------------------------------------------------------------------------
# Text / identity
# ---------------------------------------------------------------------------

def slugify(value: Any, limit: int = 140) -> str:
    raw = str(value or "").strip().lower()
    out = [ch if (ch.isalnum() or ch in "-_") else "-" for ch in raw]
    return "-".join(p for p in "".join(out).split("-") if p)[:limit]


_LEGAL_SUFFIX_RE = re.compile(
    r"\b(llc|l\.l\.c|inc|incorporated|corp|corporation|company|co|ltd|limited|"
    r"lp|llp|pllc|pc|holdings|group|the)\b", re.IGNORECASE)


def slug_variants(name: str, domain: str = "") -> List[str]:
    """Tenant slugs worth trying, best first.

    Used only as discovery's bounded FALLBACK when fingerprinting found nothing
    (see AtsProvider.slug_probe). The domain stem leads because it is the single
    best predictor - a company on `databricks.com` is `databricks` on whichever
    board it uses far more reliably than any transformation of its legal name.
    """
    out: List[str] = []

    def add(candidate: str) -> None:
        c = (candidate or "").strip("-").lower()
        if 2 < len(c) <= 60 and c not in out:
            out.append(c)

    if domain:
        stem = re.sub(r"^www\.", "", str(domain).strip().lower()).split("/")[0]
        stem = stem.split(".")[0]
        add(stem)

    base = _LEGAL_SUFFIX_RE.sub(" ", str(name or "").lower())
    base = re.sub(r"[^a-z0-9\s\-]", " ", base)
    tokens = [t for t in base.split() if t]
    if tokens:
        add("".join(tokens))
        add("-".join(tokens))
        if len(tokens) > 1:
            add(tokens[0])
    return out[:5]


_REMOTE_RE = re.compile(r"\b(remote|work from home|wfh|virtual|telecommute)\b",
                        re.IGNORECASE)


def looks_remote(*parts: Any) -> Optional[bool]:
    """True when any part advertises remote work; None when nothing says.

    None rather than False on purpose - "we could not tell" and "this is
    onsite" are different claims and the inventory records them differently.
    """
    for p in parts:
        if p and _REMOTE_RE.search(str(p)):
            return True
    return None


# ---------------------------------------------------------------------------
# Fabricated timestamps
# ---------------------------------------------------------------------------

def _seconds_into_day(dt: datetime) -> float:
    return (dt.hour * 3600 + dt.minute * 60 + dt.second
            + dt.microsecond / 1_000_000.0)


def looks_minted_now(value: Any, *, now: Optional[datetime] = None,
                     tolerance_seconds: float = 300.0) -> bool:
    """True when a published timestamp was minted from the page's OWN clock.

    Some ATS portals emit schema.org `datePosted` for SEO without having a real
    posting date to put in it, and fill it from `now()` shifted by a constant.
    Observed live on `staff-emory.icims.com` (2026-08-18): every requisition
    reported

        datePosted   2024-08-18T04:54:40.821Z     <- render time minus 2 years
        validThrough 2027-08-18T04:54:40.821Z     <- render time plus 1 year

    and the sub-second component moved with each request. Meanwhile
    `careers-fremontbank.icims.com` published `2026-08-14T04:00:00.000Z`, a
    genuine date. So this is a per-portal misconfiguration, not a vendor-wide
    one, and it cannot be handled by disbelieving the vendor as a whole.

    Ingesting the synthetic value is not a cosmetic problem. A date two years
    old is dropped outright by the freshness cutoff, so the employer silently
    contributes zero postings; a date shifted the other way would be worse,
    re-dating stale requisitions as fresh — the exact harm ADR-001 exists to
    prevent.

    The discriminator is deliberately narrow, because a false positive discards
    a real date:

      * a NON-ZERO sub-second component — real feeds publish whole seconds,
        usually midnight or a whole hour (`.000Z` above), because a posting date
        is a date, not an instant; and
      * a time-of-day within `tolerance_seconds` of ours, comparing time-of-day
        ONLY so the vendor's year/date offset is irrelevant.

    Both must hold. A `.000Z` stamp is never rejected however close to now it
    falls, and a millisecond-precision stamp from an unrelated hour is kept.
    """
    text = str(value or "").strip()
    if not text:
        return False
    iso = to_iso(text)
    if not iso:
        return False
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    if dt.microsecond == 0:
        return False

    ref = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    delta = abs(_seconds_into_day(dt) - _seconds_into_day(ref))
    delta = min(delta, 86400.0 - delta)   # wrap around midnight
    return delta <= float(tolerance_seconds)
