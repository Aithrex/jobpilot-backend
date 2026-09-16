"""The name a posting's employer is SHOWN under.

The defect, measured live on 2026-09-15
---------------------------------------
Three of the twenty largest employers by active core-tech postings were not
employers:

     255  NUTMEG CONTAINER CORPORATION
     252  CHASE FAMILY LIMITED PARTNERSHIP NO. 5
     174  M.J. WILLIAMS COMPANY

681 core postings shown under a name no job seeker has heard of. All three are
the documented JPMorgan / Boeing Oracle-and-Workday slug bug: one tenant, many
state-registry shells, and `job_ingest_worker` stamps `employer_name` from
whichever `companies` row discovered the endpoint.

Why the existing machinery did not catch it
-------------------------------------------
`employer_identity.resolve_employer_name` DOES adjudicate this, and given all
three names for tenant `jpmc` it picks "JPMorgan Chase" (the tenant is that
name's initialism — `_acronym_forms`). But `tech_feed_service._resolve_employers`
builds its candidate pool from the ROWS ON THE PAGE. A page holding only Nutmeg
rows offers one candidate, `resolve_employer_name` returns `sole_candidate`, and
the junk name is confirmed rather than corrected.

`employers.display_name` does not rescue it either. All three slugs share ONE
canonical id — `emp_31ec06b38f21`, `canonical_domain: chase.com`, which is the
attribution working correctly — but the registry row's own `display_name` is
"CHASE FAMILY LIMITED PARTNERSHIP NO. 5", because it was built from the same
`companies` row.

What does work, and costs nothing
----------------------------------
`employer_registry.IDENTITY_OVERRIDES` already names all three, as
`wrong_names` on the curated JPMorgan and Boeing rows, and
`employer_registry.wrong_name_keys()` is the `{name_key: correct display name}`
map built from them. It is an in-process dict, built once, no query — and
nothing in the display path read it.

Resolution order, strongest evidence first
-------------------------------------------
1. **A curated `wrong_names` correction.** A human declared this exact name
   wrong AND named the right employer. Applied to the posting's name and, just
   as importantly, to `employers.display_name` — the registry inherited the
   same bad name from the same source.
2. **What `employer_identity` resolved**, when it was CONFIDENT (a curated
   catalog row for the tenant, or a strong tenant-slug affinity). That path
   validated its answer against a live board.
3. **`employers.display_name` for the canonical id**, per the CLAUDE.md
   contract ("canonical is the real key"), when the resolution above was weak
   (`sole_candidate` / `fallback` / an unadjudicated conflict).
4. **The name already on the row.** Never invent one: a confident wrong name is
   what caused this in the first place.

`employer_id` is NOT rewritten anywhere here. It backs three production indexes
(`employer_id_status`, `active_role_cost_posted_at` and the source-id unique),
and this is a read-time display decision, reversible by not calling it.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Sequence

logger = logging.getLogger(__name__)

DISPLAY_VERSION = "employer-display-1.0.0"

#: `employer_name_source` values this module can write. The two from
#: `employer_identity` (`curated_catalog`, `tenant_slug_affinity`, …) pass
#: through untouched.
SOURCE_IDENTITY_OVERRIDE = "identity_override"
SOURCE_CANONICAL_REGISTRY = "canonical_registry"

#: Resolutions from `employer_identity` weak enough to be superseded by the
#: canonical registry. `sole_candidate` is the important one: it is what a page
#: holding a single junk name produces, and it means "nothing to compare
#: against", not "verified".
_WEAK_IDENTITY_SOURCES = frozenset({"sole_candidate", "fallback", "none",
                                    "unresolved_conflict"})


# ---------------------------------------------------------------------------
# Canonical display names, cached.
#
# The lookup is one `$in` on `employers.employer_id` — the registry's own
# primary key, 911 index keys and 460 documents for a full 2,000-row candidate
# set, 3 ms server-side. Measured from this dev box against live Atlas it is
# 282 ms, essentially all of it round-trip latency, and it ran on EVERY search
# because `_resolve_employers` does. An employer's display name changes on a
# registry backfill cadence, not on a request cadence, so it is cached for the
# process with a TTL and a hard size bound: a warm Lambda pays the query once.
#
# NEGATIVE results are cached too (`None` = "the registry has no name for this
# id"), or the ids that are missing would be re-queried on every single search
# and the cache would never converge.
# ---------------------------------------------------------------------------
import os
import time

NAME_CACHE_TTL_SECONDS = int(
    os.getenv("EMPLOYER_DISPLAY_CACHE_TTL", "900") or 900)
#: Bounded so a long-lived process cannot grow this without limit. 23,791
#: employers exist; this holds the working set and resets wholesale when it is
#: exceeded, which is correct for a cache whose entries are all equally cheap
#: to refetch.
NAME_CACHE_MAX = int(os.getenv("EMPLOYER_DISPLAY_CACHE_MAX", "20000") or 20000)

_NAME_CACHE: Dict[str, Optional[str]] = {}
_NAME_CACHE_AT: float = 0.0


def reset_name_cache() -> None:
    """Drop the cached names.

    Exists for the same reason `tech_feed_service.reset_coverage_cache` does:
    the cache is PROCESS-global and the offline suite shares one mongomock
    database, so a test that warms it would otherwise pin the answer for every
    later test whose database looks nothing like it.
    """
    global _NAME_CACHE_AT
    _NAME_CACHE.clear()
    _NAME_CACHE_AT = 0.0


def registry_display_names(ids: Sequence[str]) -> Dict[str, Optional[str]]:
    """`{canonical_employer_id: display_name or None}` for these ids.

    ONE `$in` for whatever is not already cached, and nothing at all when
    everything is. Never raises: a registry outage degrades to "no canonical
    name", which falls back to the name the posting already carried.
    """
    global _NAME_CACHE_AT
    wanted = sorted({str(i).strip() for i in (ids or []) if str(i or "").strip()})
    if not wanted:
        return {}

    now = time.time()
    if _NAME_CACHE and (now - _NAME_CACHE_AT) > NAME_CACHE_TTL_SECONDS:
        reset_name_cache()
        now = time.time()

    missing = [i for i in wanted if i not in _NAME_CACHE]
    if missing:
        try:
            from utils.db_connect import DBConnect
            # `missing` holds only values that came off a posting document and
            # were coerced to `str` — never a dict, never an operator.
            cursor = DBConnect().get_db()["employers"].find(
                {"employer_id": {"$in": missing}},
                {"_id": 0, "employer_id": 1, "display_name": 1})
            found = {str(d.get("employer_id")): (str(d.get("display_name") or "")
                                                 or None) for d in cursor}
        except Exception as exc:      # pragma: no cover - degrade, never fail
            logger.warning("employer_display: registry unavailable (%s)",
                           type(exc).__name__)
            found = {}
        if len(_NAME_CACHE) + len(missing) > NAME_CACHE_MAX:
            _NAME_CACHE.clear()
        for i in missing:
            _NAME_CACHE[i] = found.get(i)
        if not _NAME_CACHE_AT:
            _NAME_CACHE_AT = now
    return {i: _NAME_CACHE.get(i) for i in wanted}


def _corrections() -> Dict[str, str]:
    """`{name_key: correct display name}` from the curated identity overrides.

    In-process and cached by `employer_registry`; no Mongo, no network. An
    import failure degrades to "no corrections", never to an exception.
    """
    try:
        from services.employer_registry import wrong_name_keys
        return wrong_name_keys() or {}
    except Exception as exc:      # pragma: no cover - degrade, never fail
        logger.debug("employer_display: overrides unavailable (%s)",
                     type(exc).__name__)
        return {}


def _is_registry_shell(name: Any) -> bool:
    """Is this a state-business-registry entity rather than an operating employer?

    `employer_identity._REGISTRY_SHELL_RE` is the codebase's existing answer —
    "family limited partnership", "trust", "container corporation", "no. N",
    "estate of", "realty", "ground breaking" — every one of them a live
    `companies` row that claimed a large employer's ATS tenant.
    """
    try:
        from services.employer_identity import _REGISTRY_SHELL_RE
    except Exception:      # pragma: no cover
        return False
    return bool(_REGISTRY_SHELL_RE.search(str(name or "")))


def correct_name(name: Any) -> Optional[str]:
    """The curated replacement for a known-wrong employer name, or None."""
    try:
        from services.employer_identity import name_key
    except Exception:      # pragma: no cover
        return None
    key = name_key(name)
    if not key:
        return None
    return _corrections().get(key)


def display_name_for(row: Dict[str, Any], *,
                     registry_row: Optional[Dict[str, Any]] = None
                     ) -> Optional[Dict[str, str]]:
    """`{"name", "source"}` when this row should be shown under a DIFFERENT
    name than it currently carries, else None.

    `row` is a feed row AFTER `employer_identity.apply_resolution` has run, so
    it may already carry `employer_name_source` / `employer_name_confidence`.
    `registry_row` is this row's `employers` document, when one was fetched.
    """
    current = str(row.get("employer_name") or "").strip()

    fixed = correct_name(current)
    if fixed and fixed != current:
        return {"name": fixed, "source": SOURCE_IDENTITY_OVERRIDE}

    identity_source = str(row.get("employer_name_source") or "")
    identity_is_weak = (not identity_source) or identity_source in _WEAK_IDENTITY_SOURCES
    if not identity_is_weak:
        # A curated catalog row or a strong tenant-slug affinity already
        # adjudicated this tenant against a live board. Do not second-guess it.
        return None

    registry_name = str((registry_row or {}).get("display_name") or "").strip()
    if not registry_name:
        return None
    # The registry inherited its name from the same `companies` row the posting
    # did, so it can carry the SAME junk. Correct it before preferring it.
    registry_fixed = correct_name(registry_name)
    if registry_fixed:
        if registry_fixed != current:
            return {"name": registry_fixed, "source": SOURCE_IDENTITY_OVERRIDE}
        return None

    # The canonical name is preferred ONLY when the name we hold is a REGISTRY
    # SHELL. Measured over a 2,000-row live candidate set, preferring it
    # unconditionally made 77 substitutions and several were strictly worse:
    #
    #   Johnson & Johnson                -> JNJ CORPORATION        (20 rows)
    #   TOYOTA MOTOR SALES, U.S.A., INC. -> TOYOTA MOTOR CORP/
    #   Axis Communications, Inc.        -> Axis Business Technologies
    #
    # `employers.display_name` is derived from the same `companies` scrape the
    # posting's name is, so "canonical" means the right EMPLOYER, not
    # necessarily the better NAME. The shell test is the codebase's own
    # existing signal for "this is a state-business-registry entity, not the
    # operating employer", and it is exactly the population this rung is for.
    if not _is_registry_shell(current) or _is_registry_shell(registry_name):
        return None
    if registry_name.isupper() and not current.isupper():
        # Never trade a properly-cased name for a SHOUTED one; the identity
        # resolver applies the same preference for the same reason.
        return None
    try:
        from services.employer_identity import name_key
        if name_key(registry_name) == name_key(current):
            return None
    except Exception:      # pragma: no cover
        if registry_name == current:
            return None
    return {"name": registry_name, "source": SOURCE_CANONICAL_REGISTRY}


def apply_display_names(rows: Sequence[Dict[str, Any]],
                        registry: Optional[Dict[str, Dict[str, Any]]] = None,
                        *, name_field: str = "employer_name") -> int:
    """Rewrite the display name on each row in place. Returns how many changed.

    `registry` is an optional `{canonical_employer_id: employers document}` map
    the caller has ALREADY fetched (`tech_feed_service._registry_rows_for` on a
    `tech_only` search); pass it and rung 3 costs nothing extra. Otherwise the
    names are read through `registry_display_names`, which is cached for the
    process and issues at most one `$in` per cold search.

    The original is preserved on `employer_name_original` (the same key
    `employer_identity.apply_resolution` uses) so nothing is silently discarded
    and a card can still say "also listed as".
    """
    registry = registry or {}
    names: Dict[str, Optional[str]] = {}
    unresolved = [str(r.get("canonical_employer_id") or "").strip()
                  for r in rows
                  if str(r.get("canonical_employer_id") or "").strip()
                  and str(r.get("canonical_employer_id") or "").strip() not in registry]
    if unresolved:
        names = registry_display_names(unresolved)

    changed = 0
    for row in rows:
        canonical = str(row.get("canonical_employer_id") or "").strip()
        registry_row = registry.get(canonical)
        if registry_row is None and canonical and canonical in names:
            registry_row = {"display_name": names[canonical]}
        verdict = display_name_for(row, registry_row=registry_row)
        if not verdict:
            continue
        original = str(row.get(name_field) or "")
        row.setdefault("employer_name_original", original)
        row[name_field] = verdict["name"]
        row["employer_name_source"] = verdict["source"]
        # A curated override is a human decision about a named employer; the
        # canonical registry is our own strongest key. Both are stronger than
        # the `sole_candidate` 0.7 they replace.
        row["employer_name_confidence"] = (
            0.95 if verdict["source"] == SOURCE_IDENTITY_OVERRIDE else 0.85)
        variants = list(row.get("employer_name_variants") or [])
        if original and original not in variants and original != verdict["name"]:
            row["employer_name_variants"] = (variants + [original])[:4]
        # Keep the legacy display alias in step, or dedupe groups by the old
        # name while the card shows the new one.
        if name_field == "employer_name":
            row["company"] = verdict["name"]
        changed += 1
    return changed
