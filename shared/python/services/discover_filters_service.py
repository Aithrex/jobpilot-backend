"""Discover filters — sponsorship / visa-aware, EXPLAINABLE job annotations (Compass §6.3).

The filters no mainstream board builds: the ones that actually matter to an F-1
international student deciding whether a posting is even worth an application. Each
filter is an HONEST annotation derived from the job's own text (JD + title + company)
and, where we have it, the employer's public H-1B sponsorship SIGNAL. We never guess:
a flag we cannot evidence is ``null``, never a fabricated ``true``/``false``.

Every flag is shaped ``{"value": true|false|null, "evidence": "<phrase matched>"}`` so
the UI can show *why* a job carries a badge — an annotation you can't explain is an
annotation a student can't trust.

The six flags on ``job["visa_flags"]``:
  * ``sponsors_h1b``            — from ``sponsorship_service.get_signal`` (public DOL/USCIS
                                   disclosure data). ``null`` when we have no data on the
                                   employer — we surface uncertainty, never invent it.
  * ``cap_exempt``             — heuristic: universities / nonprofits / research institutes
                                   / medical centers are typically H-1B cap-exempt.
  * ``accepts_cpt``            — JD mentions CPT / co-op / intern-friendly work.
  * ``no_citizenship_requirement`` — ``true`` UNLESS the JD asserts a US-citizen /
                                   green-card / permanent-resident requirement.
  * ``clearance_required``     — JD mentions a security clearance / TS-SCI / polygraph.
                                   For an F-1 student this is a HARD EXCLUDE, surfaced clearly.
  * ``e_verify``               — ``null`` unless real data is available (injection hook;
                                   never fabricated).

PURE + lazy + no LLM. This module does NO work at import time and opens NO Mongo
connection itself — the single sponsorship lookup it performs is delegated to
``sponsorship_service`` (which is itself lazy) and is fully guarded, so ``annotate``
works with no DB present (the sponsorship flag simply resolves to ``null``). For LIST
paths, ``annotate_jobs`` / ``filter_jobs`` pre-fetch every employer's signal in ONE
``bulk_signals`` query instead of one lookup per row.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Sentinel so callers can inject an explicit ``None`` (e.g. "looked up, no data")
# and have it respected, distinct from "not provided, please resolve".
_UNSET = object()

# The filter keys we understand. Anything else in a caller-supplied filters dict is
# ignored — a whitelist so a malformed / hostile payload can't smuggle behavior
# through (these filters are applied in-memory only; they never touch a Mongo query).
FILTER_KEYS = (
    "sponsors_h1b",
    "cap_exempt",
    "accepts_cpt",
    "no_citizenship_requirement",
    "exclude_clearance",
)

# The flags each annotated job carries (stable order for the UI).
FLAG_KEYS = (
    "sponsors_h1b",
    "cap_exempt",
    "accepts_cpt",
    "no_citizenship_requirement",
    "clearance_required",
    "e_verify",
)

# ---------------------------------------------------------------------------
# Phrase banks (deterministic text heuristics). Matched against a normalized
# lowercase blob with periods removed, so "U.S. citizen" and "US citizen" both hit.
# ---------------------------------------------------------------------------
# Cap-exempt employer markers (H-1B cap-exempt: universities, nonprofits, research
# institutes, affiliated medical centers). Scanned across company name + JD.
_CAP_EXEMPT_PHRASES = (
    "university",
    "college",
    "nonprofit",
    "non-profit",
    "non profit",
    "research institute",
    "medical center",
)

# A citizenship / green-card / permanent-resident REQUIREMENT (drops F-1 eligibility).
# Faithful to the Compass spec; normalization (dot-stripping) covers "U.S." variants.
_CITIZENSHIP_PHRASES = (
    "must be a us citizen",
    "us citizenship required",
    "us citizenship is required",
    "green card",
    "permanent resident",
)

# Security-clearance markers — a HARD EXCLUDE for this user (clearance requires
# citizenship the F-1 student does not have).
_CLEARANCE_PHRASES = (
    "security clearance",
    "secret clearance",
    "top secret",
    "ts/sci",
    "ts sci",
    "polygraph",
)

# CPT / co-op / intern-friendly signals. Word-boundary regexes so "intern" does NOT
# fire on "international" / "internal", and "co-op" catches "coop"/"co op".
_CPT_REGEXES = (
    re.compile(r"\bcpt\b"),
    re.compile(r"curricular practical training"),
    re.compile(r"\bopt\b"),
    re.compile(r"\bco[- ]?op\b"),
    re.compile(r"\bintern(?:ship)?s?\b"),
    re.compile(r"intern[- ]friendly"),
)


# ---------------------------------------------------------------------------
# Small helpers.
# ---------------------------------------------------------------------------
def _flag(value: Optional[bool], evidence: str = "") -> Dict[str, Any]:
    """A single explainable flag. ``value`` may be ``None`` (unknown / no data)."""
    return {"value": value, "evidence": evidence or ""}


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in ("true", "1", "yes", "on")


def _job_company(job: Dict[str, Any]) -> str:
    return str(job.get("company") or job.get("employer") or "").strip()


def _job_blob(job: Dict[str, Any]) -> str:
    """Combine the job's free-text fields into one normalized lowercase blob.

    Periods are stripped and whitespace collapsed so phrase matching is robust to
    "U.S." vs "US" and to line-wrapped JD text. Company name is included so
    cap-exempt markers in the employer name (e.g. "Stanford University") are caught.
    """
    parts: List[str] = []
    for key in ("title", "company", "employer", "description", "requirements",
                "job_description", "snippet", "summary"):
        val = job.get(key)
        if isinstance(val, (list, tuple)):
            val = " ".join(str(x) for x in val)
        if val:
            parts.append(str(val))
    blob = "\n".join(parts).lower().replace(".", "")
    return re.sub(r"\s+", " ", blob)


def _find_phrase(blob: str, phrases) -> Optional[str]:
    """Return the first phrase found in ``blob`` (used as human-readable evidence)."""
    for phrase in phrases:
        if phrase in blob:
            return phrase
    return None


def _find_regex(blob: str, regexes) -> Optional[str]:
    """Return the first regex match text in ``blob`` (evidence), or ``None``."""
    for rx in regexes:
        m = rx.search(blob)
        if m:
            return m.group(0)
    return None


# ---------------------------------------------------------------------------
# Sponsorship signal resolution (delegated + guarded; never raises).
# ---------------------------------------------------------------------------
def _safe_get_signal(company: str) -> Optional[Dict[str, Any]]:
    """Best-effort single sponsorship lookup. Returns ``None`` on empty name or any
    failure (missing DB, lookup error) — honest "no data", never a guess."""
    if not company:
        return None
    try:
        from services.sponsorship_service import get_signal
        return get_signal(company)
    except Exception as e:  # pragma: no cover - defensive; get_signal already guards
        logger.warning("discover_filters sponsorship lookup failed for %r: %s", company, e)
        return None


def _safe_bulk_signals(companies: List[str]) -> Dict[str, Optional[Dict[str, Any]]]:
    """Best-effort batch sponsorship lookup (ONE Mongo query). ``{}`` on failure."""
    try:
        from services.sponsorship_service import bulk_signals
        return bulk_signals(companies) or {}
    except Exception as e:
        logger.warning("discover_filters bulk sponsorship lookup failed: %s", e)
        return {}


def _sponsors_h1b_flag(signal: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Build the ``sponsors_h1b`` flag from an employer's sponsorship signal.

    ``null`` when we have no disclosure row for the employer (unknown, not "no").
    ``true`` when the employer shows a real H-1B track record in the window,
    ``false`` when we have a row but it shows no sponsorship activity.
    """
    if not isinstance(signal, dict):
        return _flag(None, "No H-1B disclosure data on record for this employer")
    approvals = signal.get("approvals_5y") or 0
    lca = signal.get("lca_filings_5y") or 0
    band = signal.get("band")
    has_activity = (approvals > 0) or (lca > 0) or (band in ("low", "moderate", "strong"))
    evidence = str(signal.get("headline") or signal.get("summary") or "").strip()
    return _flag(bool(has_activity), evidence)


# ---------------------------------------------------------------------------
# Public: annotate one job.
# ---------------------------------------------------------------------------
def annotate(job: Dict[str, Any],
             signal: Any = _UNSET,
             e_verify: Any = _UNSET) -> Dict[str, Any]:
    """Return a shallow copy of ``job`` with an explainable ``visa_flags`` object.

    Pure + idempotent. Does not mutate the input.

    Sponsorship resolution order (so LIST paths never do one Mongo lookup per row):
      1. an explicit ``signal`` argument (may be ``None`` = looked up, no data),
      2. a pre-attached ``job["sponsorship"]`` (e.g. from ``sponsorship_service``),
      3. a guarded ``get_signal(company)`` fallback (single job / detail path).

    ``e_verify`` is a hook: pass a bool to record a known E-Verify status; left
    unset it resolves to ``null`` (we never fabricate an E-Verify claim).
    """
    if not isinstance(job, dict):
        return {}
    out = dict(job)

    company = _job_company(job)
    if signal is not _UNSET:
        sig = signal
    elif "sponsorship" in job:
        sig = job.get("sponsorship")
    else:
        sig = _safe_get_signal(company)

    blob = _job_blob(job)

    # cap_exempt — scan company + JD; boolean heuristic (false, not null, when absent).
    cap_hit = _find_phrase(blob, _CAP_EXEMPT_PHRASES)
    cap_flag = _flag(bool(cap_hit), cap_hit or "")

    # accepts_cpt — CPT / co-op / intern-friendly mention.
    cpt_hit = _find_regex(blob, _CPT_REGEXES)
    cpt_flag = _flag(bool(cpt_hit), cpt_hit or "")

    # no_citizenship_requirement — true UNLESS the JD asserts a citizenship/GC req.
    citizen_hit = _find_phrase(blob, _CITIZENSHIP_PHRASES)
    citizen_flag = _flag(citizen_hit is None, citizen_hit or "")

    # clearance_required — hard exclude for an F-1 student.
    clearance_hit = _find_phrase(blob, _CLEARANCE_PHRASES)
    clearance_flag = _flag(bool(clearance_hit), clearance_hit or "")

    # e_verify — null unless injected (do not fabricate).
    if e_verify is _UNSET or e_verify is None:
        everify_flag = _flag(None, "")
    else:
        everify_flag = _flag(bool(e_verify), "E-Verify status provided by data source")

    out["visa_flags"] = {
        "sponsors_h1b": _sponsors_h1b_flag(sig),
        "cap_exempt": cap_flag,
        "accepts_cpt": cpt_flag,
        "no_citizenship_requirement": citizen_flag,
        "clearance_required": clearance_flag,
        "e_verify": everify_flag,
    }
    return out


# ---------------------------------------------------------------------------
# Public: annotate a list (ONE bulk sponsorship query for the whole page).
# ---------------------------------------------------------------------------
def annotate_jobs(jobs: List[Dict[str, Any]],
                  signals: Optional[Dict[str, Optional[Dict[str, Any]]]] = None,
                  ) -> List[Dict[str, Any]]:
    """Annotate every job with ``visa_flags``, resolving all employer signals in ONE
    ``bulk_signals`` query (keyed by the original company string). Idempotent: safe to
    call on jobs that already carry a ``sponsorship`` object.
    """
    items = list(jobs or [])
    if not items:
        return []
    cache = signals
    if cache is None:
        companies = [_job_company(j) for j in items if isinstance(j, dict)]
        cache = _safe_bulk_signals(companies)
    out: List[Dict[str, Any]] = []
    for job in items:
        if not isinstance(job, dict):
            continue
        company = _job_company(job)
        cached = cache.get(company) if cache else None
        if cached is not None:
            # Bulk query resolved real disclosure data for this employer.
            sig = cached
        elif "sponsorship" in job:
            # No bulk hit, but the caller pre-attached a signal — defer to it.
            sig = _UNSET
        else:
            # No data anywhere. Record "unknown" (null) WITHOUT a per-row Mongo
            # lookup, so annotating a page stays at one bulk query, not N.
            sig = None
        out.append(annotate(job, signal=sig))
    return out


# ---------------------------------------------------------------------------
# Public: filtering.
# ---------------------------------------------------------------------------
def _clean_filters(filters: Any) -> Dict[str, bool]:
    """Whitelist + coerce the caller's filter selections to booleans.

    Only ``FILTER_KEYS`` that are truthy survive. Unknown keys and falsy toggles are
    dropped, so an unselected filter never constrains the result set.
    """
    if not isinstance(filters, dict):
        return {}
    active: Dict[str, bool] = {}
    for key in FILTER_KEYS:
        if key in filters and _as_bool(filters[key]):
            active[key] = True
    return active


def matches_filters(job: Dict[str, Any], filters: Dict[str, Any]) -> bool:
    """Does ``job`` pass the user's selected Discover filters?

    Reads ``job["visa_flags"]`` (annotating on the fly if absent). Positive filters
    (``sponsors_h1b``, ``cap_exempt``, ``accepts_cpt``, ``no_citizenship_requirement``)
    require the corresponding flag to be explicitly ``true`` — an ``unknown``/``null``
    flag does NOT satisfy a "show me only…" request. ``exclude_clearance`` hard-drops
    any job whose ``clearance_required`` flag is ``true``.
    """
    active = _clean_filters(filters)
    if not active:
        return True

    flags = job.get("visa_flags")
    if not isinstance(flags, dict):
        flags = annotate(job).get("visa_flags", {})

    def _val(name: str) -> Optional[bool]:
        f = flags.get(name)
        return f.get("value") if isinstance(f, dict) else None

    for key in ("sponsors_h1b", "cap_exempt", "accepts_cpt", "no_citizenship_requirement"):
        if active.get(key) and _val(key) is not True:
            return False

    if active.get("exclude_clearance") and _val("clearance_required") is True:
        return False

    return True


def filter_jobs(jobs: List[Dict[str, Any]], filters: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Annotate a list of jobs, then return only those passing the selected filters.

    One ``bulk_signals`` query for the whole list. When ``filters`` is empty every
    (still-annotated) job is returned, so callers get ``visa_flags`` for free even
    with no filters selected.
    """
    annotated = annotate_jobs(jobs)
    active = _clean_filters(filters)
    if not active:
        return annotated
    return [job for job in annotated if matches_filters(job, active)]
