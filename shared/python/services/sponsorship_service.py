"""Sponsorship intel — honest H-1B sponsorship SIGNAL per employer (Compass §6.3).

This is a TRUST feature, not a prediction engine. It surfaces what an employer has
*actually done* in public government disclosure data — nothing about the individual
user's odds. Copy is always of the form
    "Sponsored 63 people in similar roles in the last 5 years"
and is ALWAYS shown with its source and data vintage. We NEVER phrase it as a
prediction of the applicant's outcome, eligibility, or any guarantee (see
``DISCLAIMER`` below), and we return ``None`` — never a guess — when we have no data.

Data comes from two public federal datasets, joined per employer by a normalized
name (see ``normalize_employer``) and pre-aggregated into the ``sponsorship_signals``
Mongo collection by the human-run ``scripts/ingest_sponsorship.py``:
  * DOL OFLC LCA disclosure data  -> lca_filings_5y, median_wage, top_titles, last_filed_at
  * USCIS H-1B Employer Data Hub  -> approvals_5y, denials_5y

``sponsorship_signals`` document shape (READ-ONLY here; written by the ingest script):
    {
      "employer_norm":   str,      # normalized key (unique index)
      "employer_display":str,      # human-readable name for the badge
      "lca_filings_5y":  int,      # DOL certified/total LCA filings, last 5 FY
      "approvals_5y":    int,      # USCIS H-1B approvals (initial+continuing), last 5 FY
      "denials_5y":      int,      # USCIS H-1B denials (initial+continuing), last 5 FY
      "median_wage":     float|None,
      "top_titles":      [str, ...],
      "last_filed_at":   datetime|str|None,  # most recent LCA decision/received date
      "computed_at":     datetime|str|None,  # when the ingest aggregated this row
    }

This module is PURE + lazy: it does NO work at import time and opens NO Mongo
connection until a ``get_signal``/``bulk_signals`` call actually reads. That keeps
Lambda cold starts lean and lets the module import with no DB present.
"""
from __future__ import annotations

import logging
import math
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Presentation constants (single source of truth for the honest framing).
# ---------------------------------------------------------------------------
DATA_WINDOW_YEARS = 5
SOURCE_LABEL = "DOL OFLC LCA Disclosure Data + USCIS H-1B Employer Data Hub"
DISCLAIMER = (
    "This reflects the employer's historical H-1B activity from public DOL and "
    "USCIS disclosure data over the last {years} fiscal years. It is NOT a "
    "prediction of your individual outcome, is not legal or immigration advice, "
    "and is not a guarantee of sponsorship or eligibility."
).format(years=DATA_WINDOW_YEARS)

# Bands are derived from the honest 0..100 score below. "none" means we have a row
# but the employer shows no real sponsorship track record in the window.
BAND_NONE = "none"
BAND_LOW = "low"
BAND_MODERATE = "moderate"
BAND_STRONG = "strong"

# Score thresholds -> band (see _band_for_score).
_STRONG_MIN = 60
_MODERATE_MIN = 30
_LOW_MIN = 1

# Volume saturation for the log-scaled volume term: an employer with this many
# approvals in the window maps to a full volume score of 1.0. Chosen so a few
# thousand approvals reads as "strong" and a handful reads as "low".
_VOLUME_SATURATION = 2000

# ---------------------------------------------------------------------------
# Employer-name normalization (matching key). Deterministic + injection-safe:
# the output is restricted to [a-z0-9 ] so it can never sit in a Mongo operator
# position even before InputSanitizer runs.
# ---------------------------------------------------------------------------
# Corporate suffix tokens stripped from the tail during matching. Kept as a set
# of single tokens; multi-word forms (e.g. "pvt ltd") fall out because each token
# is removed independently from the tail.
_SUFFIX_TOKENS = {
    "inc", "incorporated", "llc", "llp", "lp", "ltd", "limited", "corp",
    "corporation", "co", "company", "companies", "plc", "pllc", "pc", "gmbh",
    "ag", "sa", "srl", "bv", "nv", "pvt", "pte", "private", "holdings",
    "holding", "group", "intl", "international", "worldwide", "global",
    "usa", "us", "na",
}
_PUNCT_RE = re.compile(r"[^a-z0-9]+")
_WS_RE = re.compile(r"\s+")


def normalize_employer(name: Any) -> str:
    """Normalize an employer name into a stable matching key.

    lowercase -> drop parenthetical asides -> strip punctuation to spaces ->
    remove trailing corporate-suffix tokens (repeatedly) -> collapse whitespace.
    Returns "" for empty/garbage input. Output is always ``[a-z0-9 ]`` only, so
    it is safe to place directly in an equality filter.
    """
    if not name:
        return ""
    text = str(name).lower()
    # Drop parenthetical / bracketed asides, e.g. "Google (Alphabet)".
    text = re.sub(r"[\(\[\{].*?[\)\]\}]", " ", text)
    text = text.replace("&", " and ")
    # Non-alphanumerics -> spaces (also removes any '$', '.', operators).
    text = _PUNCT_RE.sub(" ", text)
    tokens = [t for t in text.split() if t]
    # Drop a leading "the".
    if tokens and tokens[0] == "the":
        tokens = tokens[1:]
    # Repeatedly strip trailing corporate-suffix tokens ("Foo Corp Inc" -> "foo").
    while tokens and tokens[-1] in _SUFFIX_TOKENS:
        tokens.pop()
    return _WS_RE.sub(" ", " ".join(tokens)).strip()


# ---------------------------------------------------------------------------
# Lazy Mongo access — never at import time.
# ---------------------------------------------------------------------------
def _collection():
    """Return the ``sponsorship_signals`` collection (opens the singleton lazily)."""
    from utils.db_connect import DBConnect
    return DBConnect().get_db().sponsorship_signals


def _safe_norm_for_query(name: Any) -> str:
    """Sanitize then normalize a user-supplied name for use as a filter value."""
    try:
        from utils.security import InputSanitizer
        cleaned = InputSanitizer.sanitize_string(str(name or ""), max_length=200)
    except Exception:
        cleaned = str(name or "")
    return normalize_employer(cleaned)


# ---------------------------------------------------------------------------
# Scoring — recency-weighted, log-scaled volume. Deterministic, no LLM.
# ---------------------------------------------------------------------------
def _to_int(value: Any) -> int:
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        return 0


def _to_float(value: Any) -> Optional[float]:
    try:
        f = float(value)
        return f if f > 0 else None
    except (TypeError, ValueError):
        return None


def _parse_dt(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value.strip():
        raw = value.strip().replace("Z", "+00:00")
        for candidate in (raw, raw[:10]):
            try:
                dt = datetime.fromisoformat(candidate)
                return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
            except ValueError:
                continue
    return None


def _iso(value: Any) -> Optional[str]:
    dt = _parse_dt(value)
    return dt.isoformat() if dt else None


def _recency_multiplier(last_filed_at: Any, has_activity: bool) -> float:
    """1.0 for filings within the last year, tapering to a 0.3 floor by ~4 years.

    When we have USCIS approvals but no LCA ``last_filed_at`` the row is, by
    construction, inside the 5-fiscal-year window, so we assume recent-ish (0.85)
    rather than penalizing it.
    """
    dt = _parse_dt(last_filed_at)
    if dt is None:
        return 0.85 if has_activity else 0.5
    months = (datetime.now(timezone.utc) - dt).days / 30.44
    if months <= 12:
        return 1.0
    # Linear taper from 1.0 at 12 months to 0.3 at 60 months.
    mult = 1.0 - (months - 12) * (0.7 / 48.0)
    return max(0.3, min(1.0, mult))


def _band_for_score(score: int, has_activity: bool) -> str:
    if not has_activity:
        return BAND_NONE
    if score >= _STRONG_MIN:
        return BAND_STRONG
    if score >= _MODERATE_MIN:
        return BAND_MODERATE
    if score >= _LOW_MIN:
        return BAND_LOW
    return BAND_NONE


def _score_signal(approvals: int, lca_filings: int, denials: int,
                  last_filed_at: Any) -> int:
    """Honest 0..100 sponsorship signal.

    volume  : log10-scaled on the stronger of USCIS approvals / DOL filings,
              saturating at ``_VOLUME_SATURATION``.
    recency : multiplier from ``last_filed_at`` (see _recency_multiplier).
    outcome : mild confidence factor from the approval rate (never below 0.7),
              so a heavy denial record tempers — but does not erase — the signal.
    """
    volume_basis = max(approvals, lca_filings)
    if volume_basis <= 0:
        return 0
    volume = math.log10(1 + volume_basis) / math.log10(1 + _VOLUME_SATURATION)
    volume = max(0.0, min(1.0, volume))

    decided = approvals + denials
    approval_rate = (approvals / decided) if decided > 0 else 1.0
    outcome_factor = 0.7 + 0.3 * approval_rate

    recency = _recency_multiplier(last_filed_at, has_activity=volume_basis > 0)
    return int(round(100 * volume * recency * outcome_factor))


def _build_summary(display: str, approvals: int, lca_filings: int,
                   vintage: Optional[str]) -> str:
    """Honest, numbers-first summary. Always carries the source + vintage."""
    who = display or "This employer"
    if approvals > 0:
        head = (
            f"{who} had {approvals:,} H-1B approval(s) in the last "
            f"{DATA_WINDOW_YEARS} fiscal years"
        )
        if lca_filings > 0:
            head += f" across {lca_filings:,} LCA filing(s)"
    elif lca_filings > 0:
        head = (
            f"{who} filed {lca_filings:,} labor condition application(s) "
            f"in the last {DATA_WINDOW_YEARS} fiscal years"
        )
    else:
        head = (
            f"{who} shows no H-1B sponsorship activity in the last "
            f"{DATA_WINDOW_YEARS} fiscal years"
        )
    vintage_txt = f" (data through {vintage[:10]})" if vintage else ""
    return f"{head}. Source: {SOURCE_LABEL}{vintage_txt}."


def _build_headline(approvals: int, top_titles: List[str]) -> str:
    """Short badge line mirroring the Compass copy pattern."""
    if approvals <= 0:
        return f"No H-1B sponsorships on record in the last {DATA_WINDOW_YEARS} years"
    roles = "similar roles"
    if top_titles:
        roles = top_titles[0] if len(top_titles) == 1 else "similar roles"
    return (
        f"Sponsored {approvals:,} people in {roles} in the last "
        f"{DATA_WINDOW_YEARS} years"
    )


def _score_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Turn a raw ``sponsorship_signals`` doc into the public signal object."""
    display = str(doc.get("employer_display") or "").strip()
    approvals = _to_int(doc.get("approvals_5y"))
    denials = _to_int(doc.get("denials_5y"))
    lca_filings = _to_int(doc.get("lca_filings_5y"))
    median_wage = _to_float(doc.get("median_wage"))
    top_titles = [str(t) for t in (doc.get("top_titles") or []) if t][:5]
    last_filed_at = _iso(doc.get("last_filed_at"))
    computed_at = _iso(doc.get("computed_at"))

    has_activity = (approvals + lca_filings) > 0
    score = _score_signal(approvals, lca_filings, denials, doc.get("last_filed_at"))
    band = _band_for_score(score, has_activity)
    decided = approvals + denials
    approval_rate = round(approvals / decided, 3) if decided > 0 else None

    return {
        "employer_display": display or None,
        "employer_norm": str(doc.get("employer_norm") or ""),
        "lca_filings_5y": lca_filings,
        "approvals_5y": approvals,
        "denials_5y": denials,
        "approval_rate": approval_rate,
        "median_wage": median_wage,
        "top_titles": top_titles,
        "last_filed_at": last_filed_at,
        "computed_at": computed_at,
        "data_vintage": last_filed_at or computed_at,
        "data_window_years": DATA_WINDOW_YEARS,
        "score": score,
        "band": band,
        "headline": _build_headline(approvals, top_titles),
        "summary": _build_summary(display, approvals, lca_filings,
                                  last_filed_at or computed_at),
        "source": SOURCE_LABEL,
        "disclaimer": DISCLAIMER,
    }


# ---------------------------------------------------------------------------
# Public read API.
# ---------------------------------------------------------------------------
def get_signal(company_name: Any) -> Optional[Dict[str, Any]]:
    """Look up an employer's honest sponsorship signal by name.

    Returns the scored signal dict, or ``None`` (never a guess) when the company
    is empty or we have no disclosure data for it.
    """
    norm = _safe_norm_for_query(company_name)
    if not norm:
        return None
    try:
        # ``norm`` is [a-z0-9 ] only -> safe as an equality filter value.
        doc = _collection().find_one({"employer_norm": norm}, {"_id": 0})
    except Exception as e:
        logger.warning("sponsorship get_signal lookup failed for %r: %s", norm, e)
        return None
    if not doc:
        return None
    try:
        return _score_doc(doc)
    except Exception as e:
        logger.warning("sponsorship scoring failed for %r: %s", norm, e)
        return None


def bulk_signals(company_names: List[Any]) -> Dict[str, Optional[Dict[str, Any]]]:
    """Batch variant for list views: ONE Mongo query for many companies.

    Returns a map keyed by the ORIGINAL input name -> signal dict or ``None``.
    Distinct names that normalize to the same key share the same signal. Safe to
    pass duplicates and empty strings.
    """
    names = list(company_names or [])
    if not names:
        return {}
    # Map each original name to its normalized key; collect distinct keys.
    name_to_norm: Dict[str, str] = {}
    norms: set = set()
    for raw in names:
        key = str(raw or "")
        norm = _safe_norm_for_query(raw)
        name_to_norm[key] = norm
        if norm:
            norms.add(norm)

    by_norm: Dict[str, Dict[str, Any]] = {}
    if norms:
        try:
            # Every element of ``norms`` is [a-z0-9 ] only -> safe inside $in.
            cursor = _collection().find(
                {"employer_norm": {"$in": list(norms)}}, {"_id": 0}
            )
            for doc in cursor:
                try:
                    scored = _score_doc(doc)
                except Exception:
                    continue
                key = str(doc.get("employer_norm") or "")
                if key:
                    by_norm[key] = scored
        except Exception as e:
            logger.warning("sponsorship bulk_signals query failed: %s", e)

    out: Dict[str, Optional[Dict[str, Any]]] = {}
    for original, norm in name_to_norm.items():
        out[original] = by_norm.get(norm)
    return out


def attach_signal(job: Dict[str, Any],
                  signal_cache: Optional[Dict[str, Optional[Dict[str, Any]]]] = None,
                  ) -> Dict[str, Any]:
    """Attach a ``sponsorship`` object (or ``None``) to a job dict. Pure + idempotent.

    Reads the job's ``company`` (falls back to ``employer``) and sets
    ``job["sponsorship"]``. Does not mutate the input — returns a shallow copy.

    For LIST paths, pre-compute ``bulk_signals([...companies])`` once and pass it
    as ``signal_cache`` (keyed by the original company string) to avoid one Mongo
    lookup per row. For a single DETAIL job, call with no cache.
    """
    out = dict(job) if isinstance(job, dict) else {}
    company = out.get("company") or out.get("employer") or ""
    if not company:
        out["sponsorship"] = None
        return out
    if signal_cache is not None:
        out["sponsorship"] = signal_cache.get(str(company))
    else:
        out["sponsorship"] = get_signal(company)
    return out
