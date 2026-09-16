"""Sponsorship intel — honest H-1B sponsorship SIGNAL per employer (Compass §6.3).

This is a TRUST feature, not a prediction engine. It surfaces what an employer has
*actually done* in public government disclosure data — nothing about the individual
user's odds. Copy is always of the form
    "Sponsored 63 people in similar roles in FY2026"
and is ALWAYS shown with its source and data vintage. We NEVER phrase it as a
prediction of the applicant's outcome, eligibility, or any guarantee (see
``build_disclaimer`` below), and we return ``None`` — never a guess — when we have
no data.

**THE PROVENANCE COMES FROM THE ROW.** The window, the source label, the
disclaimer and the vintage are all derived from the document that was ingested
(``data_window``, ``source_label``), never from a module constant describing a
window we hope we loaded. ``DATA_WINDOW_YEARS`` / ``SOURCE_LABEL`` survive only
as the fallback for a legacy row that carries neither. This mattered on live
data: with FY2026-only USCIS rows loaded, every badge said "in the last 5 years"
from "DOL OFLC LCA Disclosure Data + USCIS", i.e. it claimed five years of two
datasets when we had one year of one. ``data_vintage`` is now the fiscal PERIOD
("FY2026"); when we imported it is ``data_imported_at``.

**A MISS AND A ZERO ARE DIFFERENT FACTS.** No row at all means we have not
looked at this employer — ``get_signal`` returns ``None`` and says so through
``lookup_status``. A row with ``census_absent: true`` means the opposite: the
USCIS Data Hub lists every employer that filed, we checked, and this employer is
not among them. Northrop Grumman is the worked example — 833 active postings in
our inventory, zero H-1B petitions in the FY2026 census.

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
      "data_window":     {                   # THE PROVENANCE. Read, not assumed.
          "fiscal_years": [int, ...],        #   what the file actually covered
          "years":        int,               #   len(fiscal_years), normally
          "label":        str,               #   the sentence a human should read
          "dol_lca_included": bool,
      },
      "source_label":    str,                # the dataset(s) this row came from
      "census_absent":   bool,               # optional: filed NOTHING (a finding)
      "naics"/"location"/"rollup":           # context, passed through to fit_score
    }

Only ``employer_norm`` is required. Every other field defaults on read, which is
how a USCIS-only row (no DOL half) scores on approvals alone instead of being
penalised for the wage and title fields nobody wrote.

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
# Presentation constants.
#
# THESE ARE FALLBACKS, NOT THE ANSWER. The window, the source and the vintage
# are properties of the ROW that was ingested, not of this module: a run that
# loaded one fiscal year of USCIS data must not be described with a sentence
# that says five years of DOL and USCIS. ``_score_doc`` therefore derives all
# three from ``doc["data_window"]`` / ``doc["source_label"]`` (written by
# ``scripts/ingest_sponsorship.py``) and falls back to the constants below only
# for a legacy row that carries neither.
#
# The failure this prevents was live: with FY2026-only rows loaded, every badge
# said "in the last 5 years" and "Source: DOL OFLC LCA Disclosure Data + USCIS"
# for data that is one year of USCIS with no DOL component at all.
# ---------------------------------------------------------------------------
DATA_WINDOW_YEARS = 5
SOURCE_LABEL = "DOL OFLC LCA Disclosure Data + USCIS H-1B Employer Data Hub"


def build_disclaimer(period_phrase: str, source: str) -> str:
    """The mandated framing, stated over the period and source we ACTUALLY have."""
    return (
        f"This reflects the employer's historical H-1B activity from public "
        f"government disclosure data ({source}) covering {period_phrase}. It is "
        f"NOT a prediction of your individual outcome, is not legal or "
        f"immigration advice, and is not a guarantee of sponsorship or "
        f"eligibility."
    )


DISCLAIMER = build_disclaimer(f"the last {DATA_WINDOW_YEARS} fiscal years",
                              SOURCE_LABEL)

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
#
# SATURATION IS PER YEAR, and that is a correctness fix, not a tuning knob. The
# constant was calibrated against a five-fiscal-year window. Held fixed, the
# same employer's band would change with how many years we happened to import —
# on the FY2026-only load "strong" started at 204 approvals IN ONE YEAR (the
# five-year equivalent of ~1,000), so the band silently measured a different
# thing than it was calibrated to mean. Scaling the saturation by the window
# makes a band a statement about the employer's RATE of sponsorship, which is
# stable across vintages: a 5-year file scores exactly as it always did
# (5 x 400 = 2000), and a 1-year file now reads "strong" from ~68 approvals.
_VOLUME_SATURATION = 2000
_VOLUME_SATURATION_PER_YEAR = _VOLUME_SATURATION / DATA_WINDOW_YEARS


def _saturation_for(window_years: Any) -> float:
    try:
        years = int(window_years)
    except (TypeError, ValueError):
        years = DATA_WINDOW_YEARS
    return _VOLUME_SATURATION_PER_YEAR * max(1, years)

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


def window_of(doc: Dict[str, Any]) -> Dict[str, Any]:
    """The window THIS ROW was built from: {years, fiscal_years, label, source}.

    Everything the public object says about provenance comes from here. A row
    written before ``data_window`` existed falls back to the module constants,
    which is the only place they are still used.
    """
    raw = doc.get("data_window")
    raw = raw if isinstance(raw, dict) else {}
    years_out: List[int] = []
    for value in (raw.get("fiscal_years") or []):
        try:
            years_out.append(int(value))
        except (TypeError, ValueError):
            continue
    fiscal_years = sorted(set(years_out))
    try:
        years = int(raw.get("years") or 0)
    except (TypeError, ValueError):
        years = 0
    if years <= 0:
        years = len(fiscal_years) or DATA_WINDOW_YEARS
    return {
        "years": years,
        "fiscal_years": fiscal_years,
        "label": str(raw.get("label") or ""),
        "source": str(doc.get("source_label") or SOURCE_LABEL),
        "measured": bool(raw),
    }


def _period_phrase(window: Dict[str, Any]) -> str:
    """"FY2026" / "FY2022-FY2026" / "the last 5 fiscal years" — never a guess."""
    fiscal_years = window.get("fiscal_years") or []
    if len(fiscal_years) == 1:
        return f"FY{fiscal_years[0]}"
    if fiscal_years:
        return f"FY{fiscal_years[0]}-FY{fiscal_years[-1]}"
    years = int(window.get("years") or DATA_WINDOW_YEARS)
    return f"the last {years} fiscal year{'s' if years != 1 else ''}"


def _score_signal(approvals: int, lca_filings: int, denials: int,
                  last_filed_at: Any, window_years: Any = DATA_WINDOW_YEARS) -> int:
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
    saturation = _saturation_for(window_years)
    volume = math.log10(1 + volume_basis) / math.log10(1 + saturation)
    volume = max(0.0, min(1.0, volume))

    decided = approvals + denials
    approval_rate = (approvals / decided) if decided > 0 else 1.0
    outcome_factor = 0.7 + 0.3 * approval_rate

    recency = _recency_multiplier(last_filed_at, has_activity=volume_basis > 0)
    return int(round(100 * volume * recency * outcome_factor))


def _build_summary(display: str, approvals: int, lca_filings: int,
                   window: Dict[str, Any], census_absent: bool = False) -> str:
    """Honest, numbers-first summary. Always carries the source + the PERIOD.

    The period is the fiscal span the row was built from, not the moment we
    imported it: "data through 2026-09-16" described our cron job, not the
    government's data.
    """
    who = display or "This employer"
    period = _period_phrase(window)
    source = window.get("source") or SOURCE_LABEL
    if approvals > 0:
        head = f"{who} had {approvals:,} H-1B approval(s) in {period}"
        if lca_filings > 0:
            head += f" across {lca_filings:,} LCA filing(s)"
    elif lca_filings > 0:
        head = (f"{who} filed {lca_filings:,} labor condition application(s) "
                f"in {period}")
    elif census_absent:
        # The Data Hub is a CENSUS of employers who filed, so absence from it
        # is a finding, not a gap. Saying so is the whole point of writing the
        # row: "no data" and "they filed nothing" look identical otherwise.
        head = (f"{who} filed no H-1B petitions in {period} — the source lists "
                f"every employer that filed, and they are not among them")
    else:
        head = f"{who} shows no H-1B sponsorship activity in {period}"
    return f"{head}. Source: {source} ({period})."


def _build_headline(approvals: int, top_titles: List[str],
                    window: Dict[str, Any], census_absent: bool = False) -> str:
    """Short badge line mirroring the Compass copy pattern."""
    period = _period_phrase(window)
    if approvals <= 0:
        if census_absent:
            return f"Filed no H-1B petitions in {period}"
        return f"No H-1B sponsorships on record in {period}"
    roles = "similar roles"
    if top_titles:
        roles = top_titles[0] if len(top_titles) == 1 else "similar roles"
    return f"Sponsored {approvals:,} people in {roles} in {period}"


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

    window = window_of(doc)
    census_absent = bool(doc.get("census_absent"))
    has_activity = (approvals + lca_filings) > 0
    score = _score_signal(approvals, lca_filings, denials, doc.get("last_filed_at"),
                          window_years=window["years"])
    band = _band_for_score(score, has_activity)
    decided = approvals + denials
    approval_rate = round(approvals / decided, 3) if decided > 0 else None
    period = _period_phrase(window)
    source = window["source"]

    if has_activity:
        evidence = "filed_in_window"
    elif census_absent:
        evidence = "absent_from_filing_census"
    else:
        evidence = "row_with_no_activity"

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
        # The VINTAGE is the fiscal period the data covers. It used to be
        # `computed_at`, i.e. when our ingest ran, which told a user when we
        # imported a file and nothing at all about the evidence.
        "data_vintage": period,
        "data_imported_at": _iso(doc.get("ingested_at")) or computed_at,
        "data_window_years": window["years"],
        "data_window_label": window["label"] or None,
        "fiscal_years": window["fiscal_years"],
        # Passed through for `fit_score._employer_sponsorship`, which states the
        # MEASURED window next to any advertised one.
        "data_window": doc.get("data_window") if isinstance(
            doc.get("data_window"), dict) else None,
        "naics": doc.get("naics"),
        "location": doc.get("location"),
        "evidence": evidence,
        "score": score,
        "band": band,
        "headline": _build_headline(approvals, top_titles, window, census_absent),
        "summary": _build_summary(display, approvals, lca_filings, window,
                                  census_absent),
        "source": source,
        "disclaimer": build_disclaimer(period, source),
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
        # A miss and a zero are DIFFERENT facts that look the same to a user,
        # so a miss is logged rather than silently swallowed. The durable fix
        # is upstream: `scripts/ingest_sponsorship.py` writes an explicit
        # `census_absent` row for employers we carry that provably filed
        # nothing, and an alias row for every spelling our feed uses, so this
        # branch should mean "we have never looked at this employer".
        logger.debug("sponsorship: no row for %r", norm)
        return None
    try:
        return _score_doc(doc)
    except Exception as e:
        logger.warning("sponsorship scoring failed for %r: %s", norm, e)
        return None


def lookup_status(company_name: Any) -> Dict[str, Any]:
    """Why a lookup answered the way it did. Diagnostics for scripts, not a route.

    ``matched`` false with ``reason == "no_row"`` means WE HAVE NOT LOOKED at
    this employer, which is not the same as the employer not sponsoring —
    `reason == "census_absent"` is that second answer, and it is a finding.
    """
    norm = _safe_norm_for_query(company_name)
    if not norm:
        return {"company": company_name, "normalized": "", "matched": False,
                "reason": "unnormalizable_name"}
    try:
        doc = _collection().find_one({"employer_norm": norm}, {"_id": 0})
    except Exception as e:
        return {"company": company_name, "normalized": norm, "matched": False,
                "reason": "lookup_failed", "detail": str(e)}
    if not doc:
        return {"company": company_name, "normalized": norm, "matched": False,
                "reason": "no_row"}
    rollup = doc.get("rollup") or {}
    return {
        "company": company_name,
        "normalized": norm,
        "matched": True,
        "reason": ("census_absent" if doc.get("census_absent")
                   else "alias_of_" + str(rollup.get("canonical_norm"))
                   if rollup.get("is_alias") else "employer_row"),
        "employer_display": doc.get("employer_display"),
        "approvals": _to_int(doc.get("approvals_5y")),
    }


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
