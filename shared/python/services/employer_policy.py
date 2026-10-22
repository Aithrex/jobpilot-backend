"""The direct-employer POLICY (`employer-policy-1.0.0`, contract §C5/§C6).

ONE function decides what a user is shown and what is applied to on their
behalf: `apply(rows, prefs) -> (kept, policy_block)`. Every serving path
(tech feed + facets + both pagination phases, inventory tabs, legacy search,
the pipeline, saved-search alerts, digests) and every application path
(match sweep, hourly scheduler, the in-Lambda submit, the worker's
pre-submit re-check) calls it, so the policy cannot differ between surfaces.

Preferences (`user_preferences.employer_policy`)::

    {"preset": "strict_direct|custom|off",
     "exclude": {"staffing", "consultancy", "client_placement",
                 "talent_marketplace", "task_gig", "talent_community",
                 "unknown_business_model"},
     "set_by": "user|default", "version": "employer-policy-1.0.0"}

DEFAULT for a user with no stored choice: `strict_direct`, with
`unknown_business_model: false` - an employer we cannot classify is SHOWN and
labelled unverified, never silently treated as direct and never silently
hidden. A stored choice is never overwritten by a default; only a PUT from
the user writes it.

What the policy is NOT: a score. It never touches `match_score` / `fit_score`
and never reorders; it removes rows and says how many, by reason. It is
never relaxed to fill a page - a short page with honest counts is the
contract.

Decisions that are not obvious, and why:

* MIXED businesses (IBM, defense/government contractors) are excluded under
  `consultancy` unless the POSTING proves an internal role, because the
  register's guidance is that unresolved service/product scope stays out of
  strict mode until a business-unit decision is justified. Counted in
  `excluded_counts.consultancy`, itemised in `reason_counts.mixed_unresolved`.
* A staffing firm's or consultancy's INTERNAL role (relationship `own_role`
  on evidence) is kept and labelled: it is a direct job at that firm.
* A marketplace BOARD (Jobgether-style reposts) counts as
  `talent_marketplace` whatever employer name the repost prints.
* Availability is not a preference, but strict/custom never serve a CLOSED or
  hard-expired posting (`excluded_counts.closed`, additive). A posting whose
  last verification is older than
  `posting_profile.ACCEPTABLE_VERIFICATION_AGE_HOURS`, or that has no
  verification record at all, is SHOWN labelled unverified and counted in
  `availability_unverified_shown`; `excluded_counts.stale_verification` stays
  0 on serving paths. Verification age is ENFORCED at the submission gate,
  where acting on a dead posting costs the user something.
* `direct_apply` (an application-path fact) is never read here: applying on
  the employer's own ATS does not make the employer a direct employer.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

POLICY_VERSION = "employer-policy-1.0.0"

PRESET_STRICT = "strict_direct"
PRESET_CUSTOM = "custom"
PRESET_OFF = "off"
PRESETS = (PRESET_STRICT, PRESET_CUSTOM, PRESET_OFF)

EXCLUDE_KEYS = ("staffing", "consultancy", "client_placement",
                "talent_marketplace", "task_gig", "talent_community",
                "unknown_business_model")
STRICT_EXCLUDE = {"staffing": True, "consultancy": True, "client_placement": True,
                  "talent_marketplace": True, "task_gig": True,
                  "talent_community": True, "unknown_business_model": False}
OFF_EXCLUDE = {k: False for k in EXCLUDE_KEYS}

#: Additive count keys beyond the seven preference reasons.
AVAILABILITY_KEYS = ("closed", "stale_verification")

#: Order in which ONE reason is charged to an excluded row. Every matching
#: reason is still listed on the row (`policy.reasons`) for inspection.
_REASON_ORDER = ("staffing", "consultancy", "talent_marketplace",
                 "client_placement", "task_gig", "talent_community",
                 "unknown_business_model")

#: request `employer_kind` -> the exclusion a user who ASKED for that kind
#: obviously does not want applied to this request.
_KIND_OVERRIDES = {"staffing": ("staffing",), "consultancy": ("consultancy",),
                   "marketplace": ("talent_marketplace",),
                   "mixed": ("consultancy",), "unknown": ("unknown_business_model",)}

PREFS_FIELD = "employer_policy"


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Preferences
# ---------------------------------------------------------------------------
def default_prefs() -> Dict[str, Any]:
    return {"preset": PRESET_STRICT, "exclude": dict(STRICT_EXCLUDE),
            "set_by": "default", "version": POLICY_VERSION}


def normalize_prefs(payload: Any) -> Optional[Dict[str, Any]]:
    """Untrusted body -> a valid preference, or None when it is unusable.

    A whitelist: only the documented keys survive, every value is coerced to
    `bool`, and the preset is DERIVED so it can never lie - a strict preset
    whose toggles differ from strict (other than `unknown_business_model`,
    which strict lets the user choose) becomes `custom`.
    """
    if not isinstance(payload, dict):
        return None
    preset = str(payload.get("preset") or "").strip().lower()
    raw = payload.get("exclude")
    if preset not in PRESETS and not isinstance(raw, dict):
        return None
    if preset == PRESET_OFF:
        exclude = dict(OFF_EXCLUDE)
    else:
        base = dict(STRICT_EXCLUDE)
        if isinstance(raw, dict):
            for k in EXCLUDE_KEYS:
                if k in raw:
                    base[k] = bool(raw[k])
        exclude = base
        same = all(exclude[k] == STRICT_EXCLUDE[k] for k in EXCLUDE_KEYS
                   if k != "unknown_business_model")
        if preset not in PRESETS:
            preset = PRESET_STRICT if same else PRESET_CUSTOM
        elif preset == PRESET_STRICT and not same:
            preset = PRESET_CUSTOM
        if preset == PRESET_CUSTOM and not any(exclude.values()):
            preset = PRESET_OFF
    return {"preset": preset, "exclude": exclude, "version": POLICY_VERSION}


def _prefs_col():
    from utils.db_connect import DBConnect
    return DBConnect().get_db().user_preferences


def get_prefs(user_email: Optional[str]) -> Dict[str, Any]:
    """The user's stored choice, or the documented default. Never raises."""
    email = str(user_email or "").strip()[:200]
    if not email:
        return default_prefs()
    try:
        doc = _prefs_col().find_one({"user_email": email}, {"_id": 0, PREFS_FIELD: 1}) or {}
    except Exception as exc:
        logger.warning("employer_policy: prefs read failed (%s); using default",
                       type(exc).__name__)
        return default_prefs()
    stored = doc.get(PREFS_FIELD)
    norm = normalize_prefs(stored) if isinstance(stored, dict) else None
    if norm is None:
        return default_prefs()
    norm["set_by"] = str(stored.get("set_by") or "user")
    if stored.get("updated_at"):
        norm["updated_at"] = stored["updated_at"]
    return norm


def put_prefs(user_email: str, payload: Any) -> Optional[Dict[str, Any]]:
    """Persist the USER's choice. Returns the stored preference, or None when
    the payload is invalid. The ONLY writer of `employer_policy`."""
    norm = normalize_prefs(payload)
    if norm is None:
        return None
    now = _now()
    doc = dict(norm, set_by="user", updated_at=now)
    _prefs_col().update_one({"user_email": str(user_email)[:200]},
                            {"$set": {PREFS_FIELD: doc},
                             "$setOnInsert": {"user_email": str(user_email)[:200]}},
                            upsert=True)
    return doc


def effective_prefs(prefs: Dict[str, Any], *, employer_kind: Any = None
                    ) -> Dict[str, Any]:
    """Prefs for ONE request. A request that explicitly asks for an employer
    kind (a saved search's `employer_kind: staffing`) is the user's
    intentional choice for that request, so the matching exclusion is lifted
    for it - and the policy block says so."""
    out = {**prefs, "exclude": dict(prefs.get("exclude") or STRICT_EXCLUDE)}
    lifted = _KIND_OVERRIDES.get(str(employer_kind or "").lower(), ())
    if lifted and out.get("preset") != PRESET_OFF:
        for k in lifted:
            out["exclude"][k] = False
        out["overridden_by_request"] = {"employer_kind": str(employer_kind),
                                        "lifted": list(lifted)}
    return out


# ---------------------------------------------------------------------------
# Per-row evaluation
# ---------------------------------------------------------------------------
_TRUST_FN: Any = None
_TRUST_KW: Optional[Dict[str, Any]] = None
_TRUST_RESOLVED = False
#: Keyword arguments that put the trust assessment in an OFFLINE mode. The
#: serving path calls it once per card, so it may only run if it can promise
#: no network fetch (its redirect-following is for ingest, not for a feed).
_OFFLINE_KWARGS = (("allow_network", False), ("network", False), ("offline", True),
                   ("fetch", False))


def _resolve_trust_fn():
    global _TRUST_FN, _TRUST_KW, _TRUST_RESOLVED
    if _TRUST_RESOLVED:
        return _TRUST_FN, _TRUST_KW
    _TRUST_RESOLVED = True
    try:
        import inspect
        from services.job_legitimacy_service import assess_posting_trust  # type: ignore
        params = inspect.signature(assess_posting_trust).parameters
        for name, value in _OFFLINE_KWARGS:
            if name in params:
                _TRUST_FN, _TRUST_KW = assess_posting_trust, {name: value}
                break
        else:
            logger.warning("employer_policy: assess_posting_trust has no offline "
                           "switch; trust block omitted on serving paths")
    except Exception:
        _TRUST_FN, _TRUST_KW = None, None
    return _TRUST_FN, _TRUST_KW


def _trust_block(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """§C4 trust, from the legitimacy agent's function when it is present AND
    offers an offline mode. Absent, or network-only -> no block."""
    fn, kw = _resolve_trust_fn()
    if fn is None:
        return None
    try:
        block = fn(row, **(kw or {}))
        return block if isinstance(block, dict) else None
    except Exception as exc:     # a trust heuristic must never 500 a feed
        logger.warning("employer_policy: trust assessment failed (%s)", type(exc).__name__)
        return None


def annotate(row: Dict[str, Any], *, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Attach `employer_profile` / `posting_profile` / `availability` (+ `trust`)
    to a row whose `_employer_profile` is already resolved. Returns the row."""
    from services import employer_business_model as ebm
    from services import posting_profile as pp
    model = row.get("_employer_profile") or row.get("_employer_profile_offline") \
        or ebm.profile_for_row(row, employers=None)
    row["_employer_profile"] = model
    row["employer_profile"] = ebm.public_profile(model)
    row["posting_profile"] = pp.classify_posting(row, str(model.get("kind") or "unknown"))
    row["availability"] = pp.availability(row, now=now)
    trust = _trust_block(row)
    if trust is not None:
        row["trust"] = trust
    return row


def reasons_for(row: Dict[str, Any], exclude: Dict[str, bool]) -> List[str]:
    """Every preference reason this row matches under `exclude` (ordered)."""
    model = row.get("employer_profile") or {}
    prof = row.get("posting_profile") or {}
    kind = str(model.get("kind") or "unknown")
    rel = str(prof.get("relationship") or "unknown")
    purpose = str(prof.get("purpose") or "unknown")
    internal = rel == "own_role"
    marketplace_board = str(row.get("board_kind") or "").lower() == "marketplace"
    hits: List[str] = []
    if exclude.get("staffing") and kind == "staffing" and not internal:
        hits.append("staffing")
    if exclude.get("consultancy") and kind in ("consultancy", "mixed") and not internal:
        hits.append("consultancy")
    if exclude.get("talent_marketplace") and (kind == "talent_marketplace" or marketplace_board) \
            and not (internal and not marketplace_board):
        hits.append("talent_marketplace")
    if exclude.get("client_placement") and rel in ("named_client", "undisclosed_client"):
        hits.append("client_placement")
    if exclude.get("task_gig") and rel == "freelance_task":
        hits.append("task_gig")
    if exclude.get("talent_community") and purpose in ("talent_community", "resume_collection"):
        hits.append("talent_community")
    if exclude.get("unknown_business_model") and kind == "unknown":
        hits.append("unknown_business_model")
    return [r for r in _REASON_ORDER if r in hits]


def _empty_block(prefs: Dict[str, Any]) -> Dict[str, Any]:
    block = {
        "preset": prefs.get("preset") or PRESET_STRICT,
        "exclude": dict(prefs.get("exclude") or STRICT_EXCLUDE),
        "set_by": prefs.get("set_by") or "default",
        "version": POLICY_VERSION,
        "excluded_counts": {k: 0 for k in EXCLUDE_KEYS + AVAILABILITY_KEYS},
        "unverified_shown": 0,
        "availability_unverified_shown": 0,
        "examined": 0,
        "kept": 0,
        "reason_counts": {},
        "max_verification_age_hours": None,
    }
    if prefs.get("overridden_by_request"):
        block["overridden_by_request"] = prefs["overridden_by_request"]
    return block


def empty_block(prefs: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """A zero-count policy block for an empty or failed list."""
    return _empty_block(prefs or default_prefs())


def apply(rows: Sequence[Dict[str, Any]], prefs: Optional[Dict[str, Any]] = None,
          *, now: Optional[datetime] = None, resolve: bool = True
          ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """THE policy. `(kept rows in input order, policy block)`.

    Annotates every row (kept or not), resolves employer business models with
    at most ONE registry read (`resolve=False` for pure/offline callers), and
    never reorders. `prefs=None` means the documented default.
    """
    from services import employer_business_model as ebm
    from services import posting_profile as pp
    prefs = prefs or default_prefs()
    now = now or _now()
    block = _empty_block(prefs)
    block["max_verification_age_hours"] = pp.ACCEPTABLE_VERIFICATION_AGE_HOURS
    rows = list(rows or [])
    if resolve:
        ebm.resolve_rows(rows)
    preset = block["preset"]
    exclude = block["exclude"]
    kept: List[Dict[str, Any]] = []
    for row in rows:
        annotate(row, now=now)
        block["examined"] += 1
        av = row["availability"]
        reasons = reasons_for(row, exclude) if preset != PRESET_OFF else []
        charged: Optional[str] = None
        if preset != PRESET_OFF and av.get("state") == pp.AV_CLOSED:
            charged = "closed"
        elif reasons:
            charged = reasons[0]
        # A STALE verification is shown, labelled (`availability.state:
        # unknown`, `reason: verification_stale`) and counted in
        # `availability_unverified_shown` - never hidden on a serving path. A
        # crawl that lags for a day must not empty every user's feed; the
        # submission gate is where verification age is enforced.
        row["policy"] = {"excluded": charged is not None, "reasons": reasons,
                         "charged": charged}
        if charged is not None:
            block["excluded_counts"][charged] += 1
            detail = charged
            if charged == "consultancy" and row["employer_profile"].get("kind") == "mixed":
                detail = "mixed_unresolved"
            block["reason_counts"][detail] = block["reason_counts"].get(detail, 0) + 1
            continue
        kept.append(row)
        if row["employer_profile"].get("kind") == "unknown":
            block["unverified_shown"] += 1
        if av.get("state") != pp.AV_OPEN:
            block["availability_unverified_shown"] += 1
    block["kept"] = len(kept)
    return kept, block


def merge_blocks(blocks: Iterable[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Sum the counts of several blocks of the SAME prefs (paged / per-hop)."""
    out: Optional[Dict[str, Any]] = None
    for b in blocks:
        if not b:
            continue
        if out is None:
            out = {**b, "excluded_counts": dict(b["excluded_counts"]),
                   "reason_counts": dict(b.get("reason_counts") or {})}
            continue
        for k, v in (b.get("excluded_counts") or {}).items():
            out["excluded_counts"][k] = out["excluded_counts"].get(k, 0) + int(v or 0)
        for k, v in (b.get("reason_counts") or {}).items():
            out["reason_counts"][k] = out["reason_counts"].get(k, 0) + int(v or 0)
        for k in ("unverified_shown", "availability_unverified_shown", "examined", "kept"):
            out[k] = int(out.get(k) or 0) + int(b.get(k) or 0)
    return out


_PUBLIC_ROW_KEYS = ("employer_profile", "posting_profile", "availability", "trust")
_INTERNAL_ROW_KEYS = ("_employer_profile", "_employer_profile_offline", "policy")


def strip_internal(rows: Iterable[Dict[str, Any]]) -> None:
    """Drop the policy's working keys from rows a legacy payload returns as-is."""
    for row in rows or ():
        for k in _INTERNAL_ROW_KEYS:
            row.pop(k, None)


def public_blocks(row: Dict[str, Any]) -> Dict[str, Any]:
    return {k: row[k] for k in _PUBLIC_ROW_KEYS if k in row}


def apply_to_payload(payload: Dict[str, Any], prefs: Optional[Dict[str, Any]],
                     list_keys: Sequence[str] = ("jobs",), *,
                     resolve: bool = True) -> Dict[str, Any]:
    """Filter the lists of a legacy result dict in place and attach `policy`.

    `total`-style counters that described the unfiltered list are corrected
    when they equal the list's previous length, so a payload never claims
    more rows than it holds.
    """
    if not isinstance(payload, dict):
        return payload
    blocks = []
    for key in list_keys:
        rows = payload.get(key)
        if not isinstance(rows, list):
            continue
        before = len(rows)
        kept, block = apply([r for r in rows if isinstance(r, dict)], prefs,
                            resolve=resolve)
        strip_internal(kept)
        payload[key] = kept
        blocks.append(block)
        if key == "jobs" and payload.get("total") == before:
            payload["total"] = len(kept)
    merged = merge_blocks(blocks)
    payload["policy"] = merged or _empty_block(prefs or default_prefs())
    return payload


# ---------------------------------------------------------------------------
# The final submission gate
# ---------------------------------------------------------------------------
def submission_gate(user_email: str, app: Dict[str, Any], *,
                    now: Optional[datetime] = None) -> Dict[str, Any]:
    """Re-check policy, hard expiry and verification age right before submit.

    `{ok, reasons, checks, policy}`. Reads the inventory posting behind the
    ledger row (`posting_job_id`), never the network. A row with no inventory
    posting is judged on what the ledger holds (company, URL, closes-at):
    preference exclusions and hard expiry still apply, verification age
    cannot, and the checks say so.
    """
    from services import posting_profile as pp
    now = now or _now()
    prefs = get_prefs(user_email)
    posting: Optional[Dict[str, Any]] = None
    pjid = str((app or {}).get("posting_job_id") or "").strip()[:64]
    if pjid:
        try:
            from services import job_inventory_service as inv
            posting = inv._postings().find_one({"job_id": pjid}, {"_id": 0, "requirements": 0})
        except Exception as exc:
            logger.warning("employer_policy: posting read failed (%s)", type(exc).__name__)
            posting = None
    row = dict(posting) if posting else {
        "employer_name": (app or {}).get("company") or "",
        "title": (app or {}).get("title") or "",
        "apply_link": (app or {}).get("apply_url") or "",
        "posting_closes_at": (app or {}).get("posting_closes_at"),
        "job_id": pjid or None,
    }
    kept, block = apply([row], prefs, now=now)
    checks: Dict[str, bool] = {}
    reasons: List[str] = []
    av = row.get("availability") or {}
    pol = row.get("policy") or {}

    def check(name: str, ok: bool, reason: str) -> None:
        checks[name] = bool(ok)
        if not ok:
            reasons.append(reason)

    check("posting_not_expired", av.get("state") != pp.AV_CLOSED,
          "posting_closed_or_expired")
    pref_reasons = pol.get("reasons") or []
    check("employer_policy", block["preset"] == PRESET_OFF or not pref_reasons,
          "employer_policy:" + (pref_reasons[0] if pref_reasons else ""))
    if posting is not None:
        stale = av.get("reason") == "verification_stale"
        check("verification_fresh", not stale, "posting_verification_stale")
        if block["preset"] != PRESET_OFF:
            check("posting_verified", av.get("state") == pp.AV_OPEN,
                  "posting_" + str(av.get("state") or "unknown"))
    else:
        checks["verification_fresh"] = True   # not applicable: no inventory record
    return {"ok": all(checks.values()), "reasons": reasons, "checks": checks,
            "availability": av, "policy": {k: block[k] for k in ("preset", "version")},
            "posting_profile": row.get("posting_profile"),
            "employer_profile": row.get("employer_profile")}
