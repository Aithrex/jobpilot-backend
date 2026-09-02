"""One employer, one name - resolved from the ATS tenant, not from whoever
happened to discover it.

The defect this fixes
---------------------
`company_ats_endpoints.display_name` is written from the `companies` row that
discovered the endpoint, and `job_ingest_worker` stamps `employer_name` on
every posting from that. When a state-registry LLC is name-adjacent to, or
shares a domain guess with, a large employer, it claims the tenant and every
posting under it.

Measured on live `aspirely_db`, 2026-09-01:

    129 (ats, tenant) pairs carry MORE THAN ONE employer_name,
    covering 28,386 active postings - 12.5% of the entire active inventory.

    oracle_orc/jpmc  ->  'NUTMEG CONTAINER CORPORATION' (1,840 postings),
                         'CHASE FAMILY LIMITED PARTNERSHIP NO. 5' (1,078),
                         'JPMorgan Chase' (1,080)
    workday/boeing   ->  'M.J. WILLIAMS COMPANY' (350), 'BOEING CO', ...
    workday/roche    ->  'COBAS SERVICES LLC' (474), 'GENE F. LANG & CO.'
    workday/att      ->  'AIO GROUND BREAKING LLC'
    workday/salesforce -> 'Salesforce', 'Slack'

Three things break at once:

* **Search.** A user filtering to JPMorgan Chase misses 2,918 of its 3,998
  postings, including 198 core-tech ones.
* **Dedupe.** The variant key is `(employer_id, title)`, and `employer_id` is
  derived from the name - so one requisition published under two names is two
  employers and never collapses.
* **The tech-only filter.** Employer classification is per `companies` row, so
  the tech verdict for JPMorgan does not reach postings labelled Nutmeg.

Resolution order, strongest evidence first
------------------------------------------
1. **A curated catalog row for that tenant.** `workday_tenant_catalog`,
   `career_pages_catalog` and `mnc_catalog` are human-maintained and each row
   was validated against a live board. If the catalog names the tenant, that
   is the answer.
2. **The candidate name closest to the tenant slug.** `boeing` -> "BOEING CO"
   beats "M.J. WILLIAMS COMPANY" on any reasonable measure.
3. **No change.** An opaque tenant (`oracle_orc/ibwsjb`) with no catalog row
   and no name resembling it gets left alone. Renaming on a guess would be a
   different wrong answer, and this module's whole point is that a confident
   wrong name is what caused the problem.

Nothing here deletes or rewrites a posting. It is a READ-TIME resolver, so it
is reversible by not calling it, and a backfill (`scripts/backfill_employer_
identity.py`) can apply the same function to stored documents once the
resolution has been reviewed.
"""
from __future__ import annotations

import functools
import logging
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

RESOLVER_VERSION = "employer-identity-1.0.0"

_LEGAL_SUFFIX_RE = re.compile(
    r"\b(inc|incorporated|corp|corporation|co|company|llc|l\.l\.c|ltd|limited|"
    r"plc|lp|llp|holdings?|group|the|sa|s\.a|nv|n\.v|ag|se|gmbh|pte|pty|"
    r"ab|as|oy|bv|b\.v|kk|srl|spa)\b", re.IGNORECASE)


def name_key(value: Any) -> str:
    """Comparable identity key: lowercase, legal suffixes stripped, squeezed."""
    s = re.sub(r"[^a-z0-9]+", " ", str(value or "").lower())
    s = _LEGAL_SUFFIX_RE.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


#: Vendor-added tenant prefixes that are not part of the employer's identity.
#: iCIMS hostnames are `careers-<slug>`, `us-careers-<slug>`, `staff-<slug>`;
#: comparing the raw host against a display name therefore always loses the
#: prefix's worth of similarity.
_TENANT_PREFIX_RE = re.compile(
    r"^(?:us[-_]?)?(?:careers?|jobs?|staff|employment|recruiting|talent)[-_]", re.IGNORECASE)


def _slug_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _tenant_key(value: Any) -> str:
    return _slug_key(_TENANT_PREFIX_RE.sub("", str(value or "").strip()))


@functools.lru_cache(maxsize=1)
def _catalog_names() -> Dict[Tuple[str, str], str]:
    """`{(vendor, tenant): curated display name}` from the Python catalogs.

    Cached for the process. These are the 2,352 employers a human validated
    against a live board, and they are the authority for their tenant.
    """
    out: Dict[Tuple[str, str], str] = {}

    def put(vendor: Any, tenant: Any, name: Any) -> None:
        v, t = _slug_key(vendor), _slug_key(tenant)
        if v and t and name:
            out.setdefault((v, t), str(name))

    try:
        from services.workday_tenant_catalog import WORKDAY_TENANT_CATALOG
        for r in WORKDAY_TENANT_CATALOG:
            put("workday", r.get("tenant"), r.get("display_name"))
    except Exception as exc:  # pragma: no cover
        logger.debug("employer_identity: workday catalog unavailable: %s", exc)
    try:
        from services.career_pages_catalog import CAREER_PAGES_CATALOG
        for r in CAREER_PAGES_CATALOG:
            put(r.get("ats"), r.get("slug"), r.get("display_name"))
    except Exception as exc:  # pragma: no cover
        logger.debug("employer_identity: career catalog unavailable: %s", exc)
    try:
        from services.mnc_catalog import MNC_CATALOG
        for r in MNC_CATALOG:
            put(r.get("ats"), r.get("slug"), r.get("display_name"))
    except Exception:  # pragma: no cover
        pass
    return out


def reset_cache() -> None:
    """Test hook - the catalog map is cached for the process."""
    _catalog_names.cache_clear()


#: Name shapes that are state-business-registry shells rather than the
#: operating employer. All of these are live `companies` rows that claimed a
#: large employer's ATS tenant.
_REGISTRY_SHELL_RE = re.compile(
    r"\b(family (?:limited )?partnership|limited partnership|"
    r"holdings? (?:llc|inc|corp)|"
    r"(?:no|number)\.?\s*\d+$|"
    r"trust|estate of|revocable|properties|realty|"
    r"container corporation|ground breaking)\b", re.IGNORECASE)


def _acronym_forms(name: Any) -> List[str]:
    """Plausible initialisms for a display name.

    `jpmc.fa.oraclecloud.com` is JPMorgan Chase's Oracle pod, and the tenant
    `jpmc` matches neither a prefix nor a substring of "JPMorgan Chase" - but
    it is exactly the name's capital letters. Without this the largest
    single mis-attribution in the inventory (3,998 postings across three
    names) stays unresolvable.
    """
    raw = str(name or "")
    if not raw:
        return []
    forms: List[str] = []
    caps = re.sub(r"[^A-Z0-9]", "", raw)
    if len(caps) >= 3:
        forms.append(caps.lower())
    words = [w for w in re.split(r"[^A-Za-z0-9]+", raw) if w]
    initials = "".join(w[0] for w in words if w).lower()
    if len(initials) >= 3:
        forms.append(initials)
    return forms


def _slug_affinity(name: Any, tenant: Any) -> float:
    """0..1 - how much a display name looks like it owns this tenant slug.

    `boeing` vs "BOEING CO" scores 1.0 (the slug is contained in the key);
    `boeing` vs "M.J. WILLIAMS COMPANY" scores 0. An opaque Oracle pod id like
    `ibwsjb` scores 0 against everything, which is the correct answer: it
    carries no evidence either way.
    """
    nk = _slug_key(name_key(name))
    tk = _tenant_key(tenant)
    if not nk or not tk:
        return 0.0
    if nk == tk:
        return 1.0
    if tk in nk or nk in tk:
        # Containment alone is far too weak on a SHORT tenant slug: `ryan`
        # matched "RYAN OIL COMPANY, INC." and `taylor` matched "TAYLOR MADE
        # TREATS, LLC", neither of which owns that Workday tenant. Require the
        # shorter string to be most of the longer one, so `boeing`/"Boeing"
        # and `alarmcom`/"Alarm.com Holdings" pass while a shared first word
        # does not.
        ratio = min(len(nk), len(tk)) / max(len(nk), len(tk))
        return 0.9 if ratio >= 0.6 else 0.0
    if tk in _acronym_forms(name):
        return 0.85
    # Longest shared prefix, as a fraction of the shorter string. Catches
    # "salesforce" vs "salesforceinc" and "tysonfoods" vs "tyson".
    n = 0
    for a, b in zip(nk, tk):
        if a != b:
            break
        n += 1
    shorter = min(len(nk), len(tk))
    return (n / shorter) if shorter and n >= 4 else 0.0


def resolve_employer_name(
    *,
    ats: Any,
    tenant: Any,
    candidates: Sequence[Any],
    fallback: Any = "",
) -> Dict[str, Any]:
    """Pick THE name for one (ats, tenant), with the reason.

    `candidates` are the employer names actually observed on that tenant's
    postings (or on its endpoint rows). Returns
    `{name, source, confidence, variants}`; `variants` is every other observed
    name, preserved so a card can say "also listed as" and so nothing is
    silently discarded.
    """
    names = [str(c).strip() for c in (candidates or []) if str(c or "").strip()]
    # Dedupe case-insensitively but keep the best-cased original.
    seen: Dict[str, str] = {}
    for n in names:
        k = name_key(n)
        if not k:
            continue
        prev = seen.get(k)
        # Prefer mixed case over SHOUTING: "JPMorgan Chase" over "JPMORGAN CHASE & CO".
        if prev is None or (prev.isupper() and not n.isupper()):
            seen[k] = n
    uniques = list(seen.values())

    curated = _catalog_names().get((_slug_key(ats), _slug_key(tenant)))
    if curated:
        variants = [n for n in uniques if name_key(n) != name_key(curated)]
        return {"name": curated, "source": "curated_catalog", "confidence": 0.95,
                "variants": variants}

    if not uniques:
        fb = str(fallback or "").strip()
        return {"name": fb, "source": "fallback" if fb else "none",
                "confidence": 0.2 if fb else 0.0, "variants": []}

    if len(uniques) == 1:
        return {"name": uniques[0], "source": "sole_candidate",
                "confidence": 0.7, "variants": []}

    def _rank(n: str) -> Tuple[float, int, int, int]:
        # A registry shell is demoted but never eliminated: if it is the ONLY
        # candidate it is still what we have, and inventing a name is worse.
        shell = 1 if _REGISTRY_SHELL_RE.search(n) else 0
        # A "name" that is just the tenant slug is an ingest fallback, not an
        # employer name ("careers-superioraluminumalloys"). Demote below any
        # real name; keep it only if it is all there is.
        slug_echo = 1 if _slug_key(n) in (_slug_key(tenant), _tenant_key(tenant)) else 0
        return (-_slug_affinity(n, tenant), slug_echo, shell, len(n))

    scored = sorted(uniques, key=_rank)
    best = scored[0]
    if _slug_affinity(best, tenant) <= 0.0:
        # Nothing resembles the tenant. Do NOT pick one - an opaque pod id
        # (oracle_orc/ibwsjb) genuinely cannot be adjudicated from here, and a
        # confident wrong name is exactly the failure this module exists to
        # undo. The caller keeps what it had and the conflict is reportable.
        fb = str(fallback or "").strip() or uniques[0]
        return {"name": fb, "source": "unresolved_conflict", "confidence": 0.0,
                "variants": [n for n in uniques if name_key(n) != name_key(fb)]}

    return {"name": best, "source": "tenant_slug_affinity",
            "confidence": round(min(0.9, 0.5 + _slug_affinity(best, tenant) * 0.4), 3),
            "variants": [n for n in scored[1:]]}


def build_resolution_map(
    observations: Iterable[Dict[str, Any]],
) -> Dict[Tuple[str, str], Dict[str, Any]]:
    """`{(ats, tenant): resolution}` from `[{ats, tenant, employer_name}, ...]`.

    One pass over an aggregation result, so a caller can resolve a whole feed
    page without a lookup per row.
    """
    buckets: Dict[Tuple[str, str], List[str]] = {}
    for obs in observations:
        key = (_slug_key(obs.get("ats")), _slug_key(obs.get("tenant")))
        if not key[1]:
            continue
        buckets.setdefault(key, []).append(str(obs.get("employer_name") or ""))
    return {
        key: resolve_employer_name(ats=key[0], tenant=key[1], candidates=names)
        for key, names in buckets.items()
    }


def apply_resolution(
    row: Dict[str, Any],
    resolution_map: Dict[Tuple[str, str], Dict[str, Any]],
    *,
    name_field: str = "company",
) -> Dict[str, Any]:
    """Rewrite one card row's employer name in place, recording what changed.

    A row whose tenant could not be adjudicated is returned untouched with
    `employer_name_confidence: 0.0` - the surface can then show the name it
    has and flag it, rather than showing a name we invented.
    """
    key = (_slug_key(row.get("ats")), _slug_key(row.get("tenant")))
    res = resolution_map.get(key)
    if not res or not res.get("name"):
        return row
    original = str(row.get(name_field) or "")
    if res["source"] == "unresolved_conflict":
        row["employer_name_confidence"] = 0.0
        row["employer_name_conflict"] = True
        if res.get("variants"):
            row["employer_name_variants"] = res["variants"][:4]
        return row
    if name_key(original) != name_key(res["name"]):
        row["employer_name_original"] = original
        row[name_field] = res["name"]
    row["employer_name_confidence"] = res["confidence"]
    row["employer_name_source"] = res["source"]
    if res.get("variants"):
        row["employer_name_variants"] = res["variants"][:4]
    return row
