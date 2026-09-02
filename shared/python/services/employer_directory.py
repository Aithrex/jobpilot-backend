"""The public employer directory — names in, canonical ids out.

Why this exists
---------------
The company filter took a company ID. Not a canonical one: whichever of an
employer's several slugs the catalog happened to hand the UI, or a bare ATS
tenant. A user typing "Stripe" had no way to reach `emp_fd96667721fe`, and a
user who somehow had the id could not tell what it named.

This module is the read side of `employers`: type a name, get the canonical
id, the display name, what else the employer is known as, and enough
provenance to tell a confident identity from a provisional one.

What it will not return
-----------------------
Anything from `career_sources`. Ownership evidence names the exact endpoints
we fetch, the redirect chains we followed and the domains we probed — that is
operational detail about our crawl, not information about the employer, and it
has no business in a public payload. The one thing a caller legitimately needs
from that subsystem is whether we hold a verified career source at all, and
that is exposed as a BOOLEAN (`career_source_verified`) with no detail behind
it.

Ranking, and why it is not alphabetical
---------------------------------------
An autocomplete that answers "Micro" with "MICRO BUBBLE INC" before
"Microsoft" is not an autocomplete. Order is:

    exact name match      the user typed the whole name
    prefix match          the user is part-way through typing it
    active postings       among equals, the employer actually hiring wins
    identity confidence   a domain-verified row beats a name-derived one

The posting count is read from `canonical_employer_id`, so an employer whose
board ownership is unproven ranks below one whose is — which is the correct
bias for a filter whose whole purpose is to be applied to postings.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

logger = logging.getLogger(__name__)

DIRECTORY_VERSION = "employer-directory-1.0.0"

#: Candidates pulled from Mongo before ranking. Wide enough that a common
#: prefix ("micro", "data") still surfaces the big employer, bounded so a
#: one-letter query cannot scan the registry.
CANDIDATE_LIMIT = 200
#: Hard ceiling on what a caller may ask for.
MAX_RESULTS = 25
#: Below this the query is too broad to be an autocomplete.
MIN_QUERY_CHARS = 2


def _db():
    from utils.db_connect import DBConnect
    return DBConnect().get_db()


def _norm_query(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())[:120]


def _match_regex(query: str) -> Dict[str, Any]:
    """A CONTAINS match, anchored-first at ranking time rather than in Mongo.

    `re.escape` is not optional: `company_categories` and this both take user
    text, and an unescaped `(` in a Mongo `$regex` is a 500 at best and a
    catastrophic backtrack at worst.
    """
    from services.employer_registry import name_key
    key = name_key(query) or query.lower()
    return {"$regex": re.escape(key), "$options": "i"}


def _prefix_regex(key: str) -> Dict[str, Any]:
    """An anchored, case-SENSITIVE prefix — the only shape Mongo can index.

    `$options: "i"` disqualifies a regex from using an index no matter how it
    is anchored, and an unanchored one is a scan by definition. Measured on
    the live registry, the contains query examines all 23,782 employers; this
    one examines 22 keys for the same query and returns the same rows in the
    same order, because a prefix match outranks a contains match anyway.

    Safe to compare case-sensitively only because both sides are already
    folded: `name_key` and `aliases` are written through
    `employer_registry.name_key`, and so is the query.
    """
    return {"$regex": "^" + re.escape(key)}


def _posting_counts(db, employer_ids: Sequence[str]) -> Dict[str, Dict[str, int]]:
    """`{employer_id: {active, core_tech}}` in ONE index-backed aggregation."""
    ids = [e for e in employer_ids if e]
    if not ids:
        return {}
    out: Dict[str, Dict[str, int]] = {}
    try:
        rows = db.job_postings.aggregate([
            {"$match": {"status": "active", "canonical_employer_id": {"$in": ids}}},
            {"$group": {"_id": {"e": "$canonical_employer_id",
                                "s": "$tech.tech_scope"},
                        "n": {"$sum": 1}}},
        ], allowDiskUse=True)
        for row in rows:
            rid = row["_id"]
            bucket = out.setdefault(str(rid.get("e")), {"active": 0, "core_tech": 0})
            bucket["active"] += int(row["n"])
            if str(rid.get("s")) == "core":
                bucket["core_tech"] += int(row["n"])
    except Exception as exc:      # pragma: no cover - counts are decoration
        logger.warning("employer_directory: posting counts unavailable (%s)",
                       type(exc).__name__)
    return out


def _company_rows(db, employers: Sequence[Dict[str, Any]]
                  ) -> Dict[str, Dict[str, Any]]:
    """`employer_id -> its `companies` row`, in ONE query.

    The SEC-assigned SIC code lives there and nowhere else, and it is the
    strongest technology signal available for a public filer. Both spellings
    of the join key are read, for the same reason everything else here reads
    both: the backfill wrote `registry_refs.company` and the resolver was
    asking for `registry_refs.company_ids`.
    """
    wanted: Dict[str, List[str]] = {}
    for emp in employers:
        refs = emp.get("registry_refs") or {}
        cids = [str(c) for c in (list(refs.get("company_ids") or [])
                                 + list(refs.get("company") or []))]
        if cids:
            wanted[str(emp.get("employer_id"))] = cids
    all_ids = sorted({c for cids in wanted.values() for c in cids})
    if not all_ids:
        return {}
    try:
        rows = {str(r["company_id"]): r for r in db.companies.find(
            {"company_id": {"$in": all_ids}},
            {"_id": 0, "company_id": 1, "sic": 1, "sic_description": 1,
             "signals": 1, "cik": 1, "ticker": 1, "domain": 1, "tech": 1,
             "name": 1})}
    except Exception as exc:      # pragma: no cover
        logger.warning("employer_directory: companies unavailable (%s)",
                       type(exc).__name__)
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for eid, cids in wanted.items():
        for cid in cids:
            if cid in rows:
                out[eid] = rows[cid]
                break
    return out


def _verified_source_ids(db, employer_ids: Sequence[str]) -> Set[str]:
    """Which of these employers hold a COVERED career source. Boolean only.

    Deliberately returns a set of ids and nothing else: the caller learns
    "yes" or "no" and cannot learn which endpoint, which vendor or what
    evidence — see the module docstring.
    """
    ids = [e for e in employer_ids if e]
    if not ids:
        return set()
    try:
        from services import career_source_service as css
        return {str(d["employer_id"]) for d in db[css.CAREER_SOURCES].find(
            {"employer_id": {"$in": ids}, "state": {"$in": list(css.COVERED_STATES)}},
            {"_id": 0, "employer_id": 1})}
    except Exception as exc:      # pragma: no cover
        logger.warning("employer_directory: source states unavailable (%s)",
                       type(exc).__name__)
        return set()


def _public_row(emp: Dict[str, Any], counts: Dict[str, int],
                verified: bool) -> Dict[str, Any]:
    """One directory entry. Every field here is ABOUT THE EMPLOYER."""
    from services import employer_registry as er
    cats = [str(c) for c in (emp.get("employer_categories") or []) if str(c)]
    return {
        "employer_id": emp.get("employer_id"),
        "display_name": emp.get("display_name") or "",
        "aliases": [str(a) for a in (emp.get("aliases") or [])][:8],
        "legal_names": [str(a) for a in (emp.get("legal_names") or [])][:4],
        "brands": [str(a) for a in (emp.get("brands") or [])][:8],
        "domain": emp.get("canonical_domain") or None,
        "categories": [c for c in cats if c != "other"] or cats,
        "cohorts": [str(c) for c in (emp.get("cohorts") or [])],
        "countries": [str(c) for c in (emp.get("countries") or [])][:6],
        # Provenance a caller can act on: how the identity was established and
        # how much to trust it. `id_basis` is one of `domain | curated | name`
        # and nothing about our crawl can be reconstructed from it.
        "identity": {
            "basis": emp.get("id_basis") or er.id_basis(emp),
            "confidence": emp.get("identity_confidence"),
            "operating_status": emp.get("operating_status") or er.STATUS_ACTIVE,
            "redirects_to": emp.get("parent_employer_id") or None,
        },
        # Whether a career source is VERIFIED, with no detail behind it.
        "career_source_verified": bool(verified),
        "active_postings": int(counts.get("active", 0)),
        "core_tech_postings": int(counts.get("core_tech", 0)),
    }


def _rank_key(emp: Dict[str, Any], key: str, counts: Dict[str, int]
              ) -> Tuple[int, int, int, float, str]:
    """Exact name, then a `name_key` prefix, then postings.

    The prefix rung reads `name_key` and nothing else, deliberately: it is the
    same field `search`'s indexed fast query matches on, and the two agreeing
    is what makes skipping the contains scan exact rather than merely fast.
    Widening this rung to aliases would break that - the fast query cannot see
    an alias prefix, because aliases keep their original capitalisation and
    the anchored regex has to stay case-sensitive to use an index.
    """
    from services.employer_registry import name_key
    nk = str(emp.get("name_key") or name_key(emp.get("display_name")))
    exact = 0 if nk == key else 1
    prefix = 0 if nk.startswith(key) else 1
    return (exact, prefix, -int(counts.get("active", 0)),
            -float(emp.get("identity_confidence") or 0.0),
            str(emp.get("display_name") or ""))


def search(query: Any, *, limit: int = 10, db=None,
           categories: Sequence[str] = (), cohorts: Sequence[str] = (),
           technology_only: bool = False) -> Dict[str, Any]:
    """Autocomplete over the canonical employer registry.

    `technology_only` narrows to employers the registry holds technology
    evidence for — the SAME rule that builds the coverage denominator and the
    feed's `employer_scope=tech_only` filter, so a company picked from the
    technology-only list is guaranteed to survive the technology-only feed.
    """
    from services import employer_registry as er
    q = _norm_query(query)
    limit = max(1, min(int(limit or 10), MAX_RESULTS))
    if len(q) < MIN_QUERY_CHARS:
        return {"query": q, "companies": [], "truncated": False,
                "directory_version": DIRECTORY_VERSION,
                "detail": f"type at least {MIN_QUERY_CHARS} characters"}
    db = db if db is not None else _db()
    key = er.name_key(q) or q.lower()

    guards: Dict[str, Any] = {
        # A renamed / acquired / merged / brand row resolves to its parent, so
        # offering it as a pickable company would hand the caller an id the
        # feed immediately redirects away from.
        "operating_status": {"$nin": list(er.REDIRECTING_STATUSES)},
    }
    if categories:
        guards["employer_categories"] = {
            "$in": [er.canonical_category(c) for c in categories]}
    if cohorts:
        guards["cohorts"] = {"$in": [str(c).strip().lower() for c in cohorts]}

    def _find(match: Dict[str, Any]) -> List[Dict[str, Any]]:
        return list(db[er.EMPLOYERS].find({**match, **guards}, {
            "_id": 0, "employer_id": 1, "display_name": 1, "name_key": 1,
            "aliases": 1, "legal_names": 1, "brands": 1, "canonical_domain": 1,
            "employer_categories": 1, "cohorts": 1, "countries": 1,
            "id_basis": 1, "identity_confidence": 1, "operating_status": 1,
            "parent_employer_id": 1, "evidence": 1, "registry_refs": 1,
        }).limit(CANDIDATE_LIMIT))

    # PREFIX FIRST, contains only if the prefix did not fill the page.
    #
    # Typing a company name is a prefix in nearly every case, and an anchored
    # prefix is the one match shape Mongo can serve from an index: measured on
    # the live registry, the contains query examines all 23,782 employers and
    # this one examines 22 keys.
    #
    # It runs on `name_key` ALONE, and that is what makes the short-circuit
    # EXACT rather than merely fast. `name_key` is the only field written
    # through `employer_registry.name_key`, so it is the only one a
    # case-sensitive anchor can be trusted against - `aliases` keep their
    # original capitalisation, and `/^micro/` does not match "Micro Devices
    # Inc". Every row this finds therefore scores `prefix = 0` in `_rank_key`
    # and every row the scan could add scores 1, so once the fast query alone
    # has more rows than the caller asked for, the scan cannot change the
    # page - only how long it takes to produce it.
    prx = _prefix_regex(key)
    candidates = _find({"name_key": prx})
    scanned = False
    if len(candidates) < limit:
        rx = _match_regex(q)
        seen = {str(c.get("employer_id")) for c in candidates}
        for row in _find({"$or": [{"name_key": rx}, {"aliases": rx},
                                  {"display_name": rx}]}):
            if str(row.get("employer_id")) not in seen:
                candidates.append(row)
        scanned = True

    ids = [str(c.get("employer_id")) for c in candidates]
    counts = _posting_counts(db, ids)

    if technology_only:
        # The SAME inputs the coverage report gives the rule, or the
        # autocomplete and the filter it claims to match will disagree.
        #
        # Passing `{}` and `None` here — which is what this did first — throws
        # away the two signals that reach most employers: the registrant's own
        # filed SIC code, which lives on the `companies` row and nowhere else,
        # and the posting mix. An employer whose only technology evidence is a
        # SIC code would then be absent from the technology-only list and
        # present in the technology-only feed, which is the exact
        # inconsistency the shared rule exists to prevent.
        from services import employer_tech_classifier as etc
        companies = _company_rows(db, candidates)
        kept: List[Dict[str, Any]] = []
        for c in candidates:
            eid = str(c.get("employer_id"))
            mix = counts.get(eid) or {}
            ok, _why = etc.is_evidence_backed_tech(
                c,
                {"core": mix.get("core_tech", 0),
                 "adjacent": 0,
                 "non_tech": max(0, mix.get("active", 0) - mix.get("core_tech", 0)),
                 "unknown": 0},
                companies.get(eid))
            if ok:
                kept.append(c)
        candidates = kept
        ids = [str(c.get("employer_id")) for c in candidates]
        if len(candidates) < limit and not scanned:
            # The prefix pass filled the page and the technology rule then
            # emptied it. The contains pass has not run, so the page is short
            # for a reason that has nothing to do with the query.
            rx = _match_regex(q)
            seen = set(ids)
            extra = [r for r in _find({"$or": [{"name_key": rx},
                                               {"aliases": rx},
                                               {"display_name": rx}]})
                     if str(r.get("employer_id")) not in seen]
            if extra:
                extra_counts = _posting_counts(
                    db, [str(r.get("employer_id")) for r in extra])
                counts.update(extra_counts)
                companies.update(_company_rows(db, extra))
                for c in extra:
                    eid = str(c.get("employer_id"))
                    mix = counts.get(eid) or {}
                    ok, _why = etc.is_evidence_backed_tech(
                        c,
                        {"core": mix.get("core_tech", 0),
                         "adjacent": 0,
                         "non_tech": max(
                             0, mix.get("active", 0) - mix.get("core_tech", 0)),
                         "unknown": 0},
                        companies.get(eid))
                    if ok:
                        candidates.append(c)
                ids = [str(c.get("employer_id")) for c in candidates]

    verified = _verified_source_ids(db, ids)

    candidates.sort(key=lambda c: _rank_key(
        c, key, counts.get(str(c.get("employer_id")), {})))
    top = candidates[:limit]
    return {
        "query": q,
        "companies": [
            _public_row(c, counts.get(str(c.get("employer_id")), {}),
                        str(c.get("employer_id")) in verified)
            for c in top],
        "truncated": len(candidates) > limit,
        "directory_version": DIRECTORY_VERSION,
    }


def lookup(employer_ids: Sequence[str], *, db=None) -> Dict[str, Any]:
    """Resolve ids back to names — for rendering a saved or bookmarked filter.

    Follows `parent_employer_id`, so an id saved before an acquisition still
    renders as the employer that now holds the postings, and says so.
    """
    from services import employer_registry as er
    ids = [str(e).strip() for e in (employer_ids or []) if str(e or "").strip()][:MAX_RESULTS]
    if not ids:
        return {"companies": [], "unresolved": [],
                "directory_version": DIRECTORY_VERSION}
    db = db if db is not None else _db()
    rows = {str(d.get("employer_id")): d for d in
            db[er.EMPLOYERS].find({"employer_id": {"$in": ids}}, {"_id": 0})}
    resolved: Dict[str, Dict[str, Any]] = {}
    unresolved: List[str] = []
    for eid in ids:
        row = rows.get(eid)
        if row is None:
            unresolved.append(eid)
            continue
        followed = row
        seen: Set[str] = {eid}
        for _ in range(4):
            parent_id = str(followed.get("parent_employer_id") or "")
            if (followed.get("operating_status") not in er.REDIRECTING_STATUSES
                    or not parent_id or parent_id in seen):
                break
            seen.add(parent_id)
            parent = db[er.EMPLOYERS].find_one({"employer_id": parent_id}, {"_id": 0})
            if not parent:
                break
            followed = parent
        resolved[eid] = followed
    counts = _posting_counts(db, [str(r.get("employer_id")) for r in resolved.values()])
    verified = _verified_source_ids(db, [str(r.get("employer_id"))
                                         for r in resolved.values()])
    out: List[Dict[str, Any]] = []
    for eid, row in resolved.items():
        entry = _public_row(row, counts.get(str(row.get("employer_id")), {}),
                            str(row.get("employer_id")) in verified)
        if str(row.get("employer_id")) != eid:
            entry["requested_employer_id"] = eid
        out.append(entry)
    return {"companies": out, "unresolved": unresolved,
            "directory_version": DIRECTORY_VERSION}


def categories(*, db=None, technology_only: bool = True) -> Dict[str, Any]:
    """The canonical category facet, with employer counts.

    Replaces free-form category entry. `other` is reported LAST and labelled,
    rather than being hidden: an employer we have not categorised is a real
    and countable state, and dropping it from the list would make the counts
    not add up.
    """
    from services import employer_registry as er
    db = db if db is not None else _db()
    match: Dict[str, Any] = {
        "operating_status": {"$nin": list(er.REDIRECTING_STATUSES)}}
    counts: Dict[str, int] = {}
    try:
        for row in db[er.EMPLOYERS].aggregate([
                {"$match": match},
                {"$unwind": "$employer_categories"},
                {"$group": {"_id": "$employer_categories", "n": {"$sum": 1}}}]):
            counts[str(row["_id"])] = int(row["n"])
    except Exception as exc:      # pragma: no cover
        logger.warning("employer_directory: categories unavailable (%s)",
                       type(exc).__name__)
    ordered = sorted((c for c in counts if c != "other"),
                     key=lambda c: -counts[c])
    out = [{"value": c, "label": _label(c), "employers": counts[c]}
           for c in ordered]
    if "other" in counts:
        out.append({"value": "other", "label": "Not yet categorised",
                    "employers": counts["other"]})
    return {"categories": out, "directory_version": DIRECTORY_VERSION}


def _label(value: str) -> str:
    return str(value or "").replace("_", " ").title()
