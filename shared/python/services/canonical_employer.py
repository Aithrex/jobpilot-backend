"""Which canonical employer does this posting belong to — and when to refuse.

The defect this fixes
---------------------
`job_postings.employer_id` is a slug of whatever name the board printed.
Measured live on 2026-09-02, the newest 5,000 active postings contain **zero**
canonical employer ids, because there was no field to put one in. Every
employer-shaped operation therefore keys on a string the crawler invented:

    company filters      `employer_id` OR `tenant`, either of which can name
                         a legal entity rather than an employer
    company facets       one employer counted three times under three of its
                         own subsidiary names
    deduplication        `variant_group_id = hash(employer_id, title)`, so one
                         requisition published under two names never collapses
    employer diversity   keyed on `(ats, tenant)` because the slug was unusable
    feedback analytics   "wrong company" reports attributed to a slug
    saved searches       a saved employer filter that silently drifts

`employer_registry` already mints the canonical id. What was missing is the
join from a POSTING to it, and — more importantly — a rule for when NOT to
make that join.

The rule
--------
A posting receives `canonical_employer_id` only when the board it came from
has **owner-verified** career-source evidence naming exactly one employer:

    career_sources.ownership_verified == True

which `career_source_service.adjudicate_ownership` sets only for a documented
chain from a verified employer domain to the exact endpoint, or two
corroborating signals from two DIFFERENT evidence classes. A guessed slug that
returns jobs never reaches it, and neither does a name that merely matches.

Everything else is QUARANTINED, with the reason recorded:

    conflicting_owner_verified_sources  two employers own-verify the same
                                        board and neither is the other's
                                        parent — a human decides, not us
    marketplace_or_shared_board         the board lists many employers' roles,
                                        so its owner is not the job's employer
    ownership_not_verified              we have no accepted evidence yet
    no_board_key                        the posting names no (ats, tenant)

Quarantine is not failure. A posting with no `canonical_employer_id` reads
exactly as it read before this module existed — `employer_id` is untouched and
every legacy filter still matches it. That is the whole reason this is
additive: the wrong canonical id is far more expensive than none, because a
wrong one is silently authoritative.

Parents are followed, subsidiaries are not conflicts
-----------------------------------------------------
Two career sources naming "Slack" and "Salesforce" for the same board is not a
conflict — `employer_registry` records Slack as `product_or_brand` with
`parent_employer_id` pointing at Salesforce, so both resolve to Salesforce and
the board is unambiguous. The same is true for `acquired`, `merged` and
`renamed`. Only two employers that survive the parent walk as DIFFERENT
employers are a genuine conflict, and those are the ones a human should see.

Evidence outranks evidence — it does not outrank a name
-------------------------------------------------------
When two employers survive the parent walk on one board, one more question is
asked, and it is the same question `adjudicate_ownership` already answers:
**how strong is each claim?** A chain from the employer's OWN verified domain
to this exact endpoint (`SUFFICIENT_SIGNALS`, rule 4a) is the employer telling
us the board is theirs. Two third parties agreeing (rule 4b) is weaker, and
the ownership module already says so by making 4a sufficient alone. So when
exactly ONE claimant on a board holds a domain chain, that claimant is the
employer, and the basis is recorded as `domain_chain_precedence` so a report
can always separate it from an unambiguous board.

Measured on the live registry: 111 boards carry two or more surviving
claimants, and on 47 of them exactly one claimant holds a chain. Those 47 are
overwhelmingly ONE employer twice — a domain-keyed row (`CITIGROUP INC`,
`citigroup.com`) beside an unpromoted name-keyed row (`Citi`,
`identity_confidence 0.45`) that exists only because no domain had been
verified for it yet.

This is deliberately NOT a name comparison, and it does not merge anything.
The two employer rows stay separate, the board is STILL written to the
conflict queue — carrying `resolved_by: domain_chain_precedence` — and a human
still gets to decide whether they are one employer. What changes is only which
of two documented claims a posting is attributed to, and it is attributed to
the stronger one. Where two claimants both hold chains (Truist and the AFCO
entities it acquired, 64 boards) nothing is attributed, because there the
question really is open.

Nothing here writes to `job_postings`. `resolve_board` is pure given a
`BoardOwnership`, so it is callable from the ingest write path, from the
backfill and from a test with no database at all.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, NamedTuple, Optional, Sequence, Set, Tuple

logger = logging.getLogger(__name__)

CANONICAL_EMPLOYER_VERSION = "canonical-employer-1.0.0"

#: The posting field this module owns. `employer_id` is NEVER touched.
FIELD = "canonical_employer_id"
#: How the id was arrived at, stored beside it so an audit needs no re-derivation.
BASIS_FIELD = "canonical_employer_basis"
#: The stamp that says which rule version decided. A rule change is then a
#: selectable backfill rather than an untraceable rewrite.
VERSION_FIELD = "canonical_employer_version"

#: One employer own-verified the board and nobody else did.
BASIS_OWNER_VERIFIED_BOARD = "owner_verified_board"
#: Several own-verified it and exactly one holds a verified-domain chain. See
#: "Evidence outranks evidence" above. Named separately so the report never
#: has to present the two as one number.
BASIS_DOMAIN_CHAIN_PRECEDENCE = "domain_chain_precedence"

ASSIGNMENT_BASES: Tuple[str, ...] = (
    BASIS_OWNER_VERIFIED_BOARD, BASIS_DOMAIN_CHAIN_PRECEDENCE,
)

# --------------------------------------------------------------------------
# Quarantine reasons — a closed vocabulary, so the report can group them
# --------------------------------------------------------------------------
Q_CONFLICTING_OWNERS = "conflicting_owner_verified_sources"
Q_MARKETPLACE_BOARD = "marketplace_or_shared_board"
Q_OWNERSHIP_UNVERIFIED = "ownership_not_verified"
Q_NO_BOARD_KEY = "no_board_key"
Q_EMPLOYER_MISSING = "employer_row_missing"

QUARANTINE_REASONS: Tuple[str, ...] = (
    Q_CONFLICTING_OWNERS, Q_MARKETPLACE_BOARD, Q_OWNERSHIP_UNVERIFIED,
    Q_NO_BOARD_KEY, Q_EMPLOYER_MISSING,
)

#: Board kinds whose owner is NOT the employer of the jobs on it.
NON_ATTRIBUTABLE_BOARD_KINDS: Tuple[str, ...] = ("marketplace", "staffing")

#: Where a conflicting board is recorded for a human. Additive; dropping it
#: loses the queue and nothing else.
BOARD_CONFLICTS = "employer_board_conflicts"


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------
# Key normalization — the join that was dead
# --------------------------------------------------------------------------
def vendor_key(value: Any) -> str:
    """`oracle-orc` on a posting and `oracle_orc` on a source are one vendor.

    Postings carry the crawler's spelling and `career_sources` carries
    `build_source`'s, and the two differ by punctuation on three vendors. A
    join on the raw strings silently drops 28,834 active Oracle postings, so
    both sides are squeezed to alphanumerics here and nowhere else.
    """
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def tenant_key(value: Any) -> str:
    """Same, for the tenant, which is stored case-sensitively on both sides.

    `AveryDennison` and `averydennison` are one board.
    """
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def board_key(ats: Any, tenant: Any) -> Tuple[str, str]:
    return vendor_key(ats), tenant_key(tenant)


class Decision(NamedTuple):
    """`employer_id` is empty whenever `reason` is set. Never both."""
    employer_id: str
    basis: str
    reason: str

    @property
    def assigned(self) -> bool:
        return bool(self.employer_id)


_REFUSE_NO_KEY = Decision("", "", Q_NO_BOARD_KEY)
_REFUSE_MARKETPLACE = Decision("", "", Q_MARKETPLACE_BOARD)
_REFUSE_UNVERIFIED = Decision("", "", Q_OWNERSHIP_UNVERIFIED)


# --------------------------------------------------------------------------
# The board -> employer map
# --------------------------------------------------------------------------
class BoardOwnership:
    """`(vendor, tenant) -> the one employer that own-verified it`, or refusal.

    Built ONCE per pass and then queried in memory. The alternative — a lookup
    per posting — is 238,000 round trips for a backfill, and the map is 2,255
    entries.

    `claims` is `{board key: {employer_id: holds a verified-domain chain}}`.
    One claimant is unambiguous; several, with exactly one chain, is settled by
    precedence and STILL filed as a conflict; several with none or with more
    than one chain attributes nothing.
    """

    def __init__(self, claims: Dict[Tuple[str, str], Dict[str, bool]],
                 *, built_at: Optional[datetime] = None,
                 sources_read: int = 0) -> None:
        self._owners: Dict[Tuple[str, str], Tuple[str, str]] = {}
        self._conflicts: Dict[Tuple[str, str], List[str]] = {}
        #: Boards attributed on evidence precedence. STILL conflicts — the
        #: identity duplication behind them is unresolved — so they are kept
        #: separately rather than being quietly folded into `_owners` alone.
        self._precedence: Dict[Tuple[str, str], str] = {}
        for key, by_employer in claims.items():
            clean = {str(e): bool(v) for e, v in by_employer.items()
                     if str(e or "").strip()}
            if not clean:
                continue
            if len(clean) == 1:
                self._owners[key] = (next(iter(clean)), BASIS_OWNER_VERIFIED_BOARD)
                continue
            self._conflicts[key] = sorted(clean)
            chained = sorted(e for e, has_chain in clean.items() if has_chain)
            if len(chained) == 1:
                self._owners[key] = (chained[0], BASIS_DOMAIN_CHAIN_PRECEDENCE)
                self._precedence[key] = chained[0]
        self.built_at = built_at or _now()
        self.sources_read = int(sources_read or 0)

    # -- introspection, for the report and the tests --------------------
    @property
    def boards(self) -> int:
        """Boards that yield an attribution, on either basis."""
        return len(self._owners)

    @property
    def unambiguous_boards(self) -> int:
        return sum(1 for _e, b in self._owners.values()
                   if b == BASIS_OWNER_VERIFIED_BOARD)

    @property
    def conflicts(self) -> Dict[Tuple[str, str], List[str]]:
        """Every board with more than one surviving claimant, attributed or not."""
        return dict(self._conflicts)

    @property
    def precedence_boards(self) -> Dict[Tuple[str, str], str]:
        """Conflicting boards a domain chain settled. A subset of `conflicts`."""
        return dict(self._precedence)

    @property
    def unresolved_conflicts(self) -> Dict[Tuple[str, str], List[str]]:
        return {k: v for k, v in self._conflicts.items()
                if k not in self._precedence}

    def owner_of(self, ats: Any, tenant: Any) -> str:
        hit = self._owners.get(board_key(ats, tenant))
        return hit[0] if hit else ""

    def is_owner_verified(self, ats: Any, tenant: Any) -> bool:
        """Does ANY employer own-verify this board — attributable or not?

        The denominator the acceptance criterion names. A contested board IS
        owner-verified; it just does not yield an attribution, and counting it
        as unverified would flatter the number.
        """
        key = board_key(ats, tenant)
        return key in self._owners or key in self._conflicts

    # -- the decision ---------------------------------------------------
    def resolve_board(self, *, ats: Any, tenant: Any,
                      board_kind: Any = "") -> Decision:
        """Pure. The whole assignment rule, in one place."""
        if str(board_kind or "").strip().lower() in NON_ATTRIBUTABLE_BOARD_KINDS:
            return _REFUSE_MARKETPLACE
        key = board_key(ats, tenant)
        if not key[1]:
            return _REFUSE_NO_KEY
        hit = self._owners.get(key)
        if hit:
            return Decision(hit[0], hit[1], "")
        if key in self._conflicts:
            return Decision("", "", Q_CONFLICTING_OWNERS)
        return _REFUSE_UNVERIFIED

    def resolve_posting(self, posting: Dict[str, Any]) -> Decision:
        return self.resolve_board(ats=posting.get("ats"),
                                  tenant=posting.get("tenant"),
                                  board_kind=posting.get("board_kind"))


# --------------------------------------------------------------------------
# Construction from the database
# --------------------------------------------------------------------------
def _db():
    from utils.db_connect import DBConnect
    return DBConnect().get_db()


def _parent_index(db) -> Dict[str, str]:
    """`{employer_id: parent_employer_id}` for the redirecting statuses only.

    A `closed` employer keeps its own identity — its postings are its own —
    so it is deliberately absent here.
    """
    from services import employer_registry as er
    out: Dict[str, str] = {}
    cur = db[er.EMPLOYERS].find(
        {"operating_status": {"$in": list(er.REDIRECTING_STATUSES)},
         "parent_employer_id": {"$nin": [None, ""]}},
        {"_id": 0, "employer_id": 1, "parent_employer_id": 1})
    for row in cur:
        eid = str(row.get("employer_id") or "")
        pid = str(row.get("parent_employer_id") or "")
        if eid and pid and eid != pid:
            out[eid] = pid
    return out


def follow_parent(employer_id: str, parents: Dict[str, str],
                  *, max_depth: int = 4) -> str:
    """Walk `parent_employer_id` to the surviving employer. Cycle-safe.

    Depth-capped for the same reason `employer_registry._follow` is: a cycle
    in the parent graph is a data error, and looping on a hot path would take
    the backfill down with it rather than reporting the bad row.
    """
    seen: Set[str] = set()
    cur = str(employer_id or "")
    for _ in range(max_depth):
        nxt = parents.get(cur)
        if not nxt or nxt in seen:
            break
        seen.add(cur)
        cur = nxt
    return cur


def _holds_domain_chain(source: Dict[str, Any]) -> bool:
    """Does this source's evidence include a CONFIRMED verified-domain chain?

    Reads `career_source_service.SUFFICIENT_SIGNALS` rather than restating the
    list, so a change to the ownership rule reaches this precedence rule
    automatically. `discovery_method` is deliberately not consulted: it records
    how the endpoint was FOUND, and a board found by slug probe can later be
    confirmed by a domain walk.
    """
    from services import career_source_service as css
    for ev in (source.get("ownership_evidence") or ()):
        if not isinstance(ev, dict) or not ev.get("confirmed"):
            continue
        if str(ev.get("kind")) in css.SUFFICIENT_SIGNALS:
            return True
    return False


def build_board_ownership(db=None, *, include_states: Sequence[str] = ()
                          ) -> BoardOwnership:
    """Read `career_sources` once and fold it into the board map.

    `include_states` is a diagnostic lever only — the default reads OWNERSHIP,
    not state, because ownership is a fact about who runs the board and stays
    true while the board is temporarily blocked, empty or even dead. Coverage
    asks a different question and `career_source_service.COVERED_STATES`
    answers it; conflating the two is what produced a coverage number that
    moved when our own prober got a 403.
    """
    from services import career_source_service as css
    db = db if db is not None else _db()
    query: Dict[str, Any] = {"ownership_verified": True}
    if include_states:
        query = {"state": {"$in": list(include_states)}}
    parents = _parent_index(db)
    claims: Dict[Tuple[str, str], Dict[str, bool]] = {}
    read = 0
    for src in db[css.CAREER_SOURCES].find(
            query, {"_id": 0, "ats_vendor": 1, "tenant": 1, "employer_id": 1,
                    "ownership_evidence": 1}):
        read += 1
        key = board_key(src.get("ats_vendor"), src.get("tenant"))
        if not key[1]:
            continue
        eid = follow_parent(str(src.get("employer_id") or ""), parents)
        if not eid:
            continue
        bucket = claims.setdefault(key, {})
        # OR across an employer's several sources for one board: a chain found
        # on any of them is a chain the employer holds.
        bucket[eid] = bool(bucket.get(eid)) or _holds_domain_chain(src)
    return BoardOwnership(claims, sources_read=read)


# --------------------------------------------------------------------------
# The ingest-path cache
# --------------------------------------------------------------------------
#: How long a built map is reused on the write path. Ownership changes at the
#: pace of a verification sweep (hours), not of a crawl (seconds), so a short
#: TTL would buy nothing and cost one full `career_sources` read per shard.
CACHE_TTL_SECONDS = float(os.getenv("CANONICAL_EMPLOYER_CACHE_TTL", "900") or 900)

_CACHE_LOCK = threading.Lock()
_CACHE: Optional[Tuple[float, BoardOwnership]] = None


def cached_board_ownership(db=None, *, ttl: Optional[float] = None
                           ) -> Optional[BoardOwnership]:
    """The board map, rebuilt at most every `CACHE_TTL_SECONDS`.

    Returns `None` — never raises — when the registry is unreachable. An
    ingest run must not fail because an ADDITIVE attribution field could not
    be computed; the posting is written without the field and the backfill
    picks it up. Failing closed here would trade a complete crawl for a
    nice-to-have column.
    """
    global _CACHE
    window = CACHE_TTL_SECONDS if ttl is None else float(ttl)
    now = time.monotonic()
    with _CACHE_LOCK:
        if _CACHE is not None and (now - _CACHE[0]) < window:
            return _CACHE[1]
    try:
        built = build_board_ownership(db)
    except Exception as exc:      # pragma: no cover - never break ingest
        logger.warning("canonical_employer: board map unavailable (%s)",
                       type(exc).__name__)
        return None
    with _CACHE_LOCK:
        _CACHE = (now, built)
    return built


def reset_cache() -> None:
    """Drop the cached map. Called by tests and after a verification sweep."""
    global _CACHE
    with _CACHE_LOCK:
        _CACHE = None


def stamp_fields(posting: Dict[str, Any], *, ownership: Optional[BoardOwnership] = None,
                 db=None) -> Tuple[Dict[str, Any], List[str]]:
    """`({fields to $set}, [fields to $unset])` for one posting.

    Both halves matter. A board that BECOMES contested must lose the id it was
    given, or the feed keeps filtering on an attribution the evidence no
    longer supports — and a stale wrong id is worse than none, which is the
    premise of this whole module.
    """
    own = ownership if ownership is not None else cached_board_ownership(db)
    if own is None:
        return {}, []
    decision = own.resolve_posting(posting)
    if decision.assigned:
        return {FIELD: decision.employer_id,
                BASIS_FIELD: decision.basis,
                VERSION_FIELD: CANONICAL_EMPLOYER_VERSION}, []
    return {}, [FIELD, BASIS_FIELD, VERSION_FIELD]


# --------------------------------------------------------------------------
# The quarantine queue
# --------------------------------------------------------------------------
def record_board_conflict(db, key: Tuple[str, str], employer_ids: Sequence[str],
                          *, postings: int = 0,
                          display_names: Sequence[str] = (),
                          attributed_to: str = "",
                          resolved_by: str = "") -> str:
    """Write one contested board to the review queue. Idempotent by key.

    Every board with more than one surviving claimant is written here,
    INCLUDING the ones a domain chain settled. Attribution and identity are
    two questions: precedence answers "whose posting is this?" and leaves
    "are these two rows one employer?" open. Recording only the unattributed
    boards would hide 47 duplicate identities behind a working feature, which
    is precisely how the previous coverage number came to look healthy.

    `postings` is carried so the queue can be worked in the order that buys
    the most attribution, which is the same prioritisation rule the coverage
    work queue uses.
    """
    vendor, tenant = key
    review_key = f"{vendor}|{tenant}"
    db[BOARD_CONFLICTS].update_one(
        {"review_key": review_key},
        {"$setOnInsert": {"review_key": review_key, "created_at": _now(),
                          "resolved": False},
         "$set": {"ats_vendor": vendor, "tenant": tenant,
                  "employer_ids": sorted({str(e) for e in employer_ids}),
                  "display_names": [str(n)[:120] for n in (display_names or ())][:8],
                  "active_postings": int(postings or 0),
                  "attributed_to": str(attributed_to or ""),
                  "resolved_by": str(resolved_by or ""),
                  "attribution_blocked": not bool(attributed_to),
                  "last_seen_at": _now(),
                  "canonical_employer_version": CANONICAL_EMPLOYER_VERSION}},
        upsert=True)
    return review_key


def open_board_conflicts(db=None, *, limit: int = 500,
                         blocked_only: bool = False) -> List[Dict[str, Any]]:
    db = db if db is not None else _db()
    q: Dict[str, Any] = {"resolved": False}
    if blocked_only:
        q["attribution_blocked"] = True
    return list(db[BOARD_CONFLICTS].find(q, {"_id": 0})
                .sort("active_postings", -1).limit(int(limit)))


# --------------------------------------------------------------------------
# Indexes
# --------------------------------------------------------------------------
def ensure_indexes() -> List[str]:
    """Indexes the canonical-company query shapes actually need.

    Measured shapes, not speculative ones: every tech-feed query is
    `status: active` first, then one equality, then the `(posted_at DESC,
    job_id ASC)` keyset. So the canonical filter gets the same two-index
    treatment `tenant` already has, and nothing else.

    **The partial filter is `$exists`, and that is load-bearing.** A partial
    index is only ELIGIBLE for a query the planner can prove is a subset of
    the filter expression, and `{FIELD: "emp_..."}` does not prove
    `{FIELD: {"$type": "string"}}` — the subset check does not reason about
    types. Both indexes existed, and neither was ever used: measured on live
    inventory the posting-count aggregation took **1,544 ms** on
    `active_cost_first_seen_at` with a FETCH filter, and **17 ms** on the same
    index declared with `$exists`. An equality (and an `$in` of equalities)
    DOES prove existence, so the same rows are indexed and the planner can
    now say so.
    """
    created: List[str] = []
    db = _db()
    try:
        created.append(db["job_postings"].create_index(
            [("status", 1), (FIELD, 1), ("posted_at", -1)],
            name="active_canonical_employer_posted_at",
            partialFilterExpression={FIELD: {"$exists": True}}))
        created.append(db["job_postings"].create_index(
            [("status", 1), (FIELD, 1), ("posted_at", -1), ("job_id", 1)],
            name="active_canonical_employer_keyset",
            partialFilterExpression={FIELD: {"$exists": True}}))
        # The company filter accepts BOTH spellings while saved searches and
        # the extension still hold legacy ones, and a mixed request becomes a
        # `$or` of a canonical branch and two legacy ones. Mongo subplans ONE
        # rooted `$or`, and the freshness window is already one — so the
        # company `$or` is applied as a FILTER, and every branch has to be
        # independently indexable or the whole thing walks `posted_at`. The
        # two `status`-prefixed indexes above cannot serve a branch that does
        # not carry `status`; this one, prefixed on the field itself, can.
        # Measured live: a mixed request went 8,033 ms / 36,964 docs examined
        # to 467 ms / 678. It is the canonical twin of `employer_id_status`,
        # which is why the legacy branches were always fast and this one was
        # not.
        created.append(db["job_postings"].create_index(
            [(FIELD, 1), ("status", 1)],
            name="canonical_employer_status",
            partialFilterExpression={FIELD: {"$exists": True}}))
        created.append(db[BOARD_CONFLICTS].create_index(
            "review_key", unique=True, name="board_conflict_key_unique"))
        created.append(db[BOARD_CONFLICTS].create_index(
            [("resolved", 1), ("active_postings", -1)],
            name="board_conflict_queue"))
    except Exception as exc:      # pragma: no cover - index creation is best-effort
        logger.warning("canonical_employer.ensure_indexes: %s", exc)
    return [c for c in created if c]


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------
def attribution_stats(db=None, *, only_active: bool = True) -> Dict[str, Any]:
    """How much of the inventory carries a canonical id, and why the rest does not.

    Reported as a SHARE OF OWNER-VERIFIED POSTINGS, not of the whole
    inventory. The whole inventory includes boards we have deliberately
    declined to attribute, and a metric that counts a correct refusal as a
    miss would reward attributing them wrongly.
    """
    db = db if db is not None else _db()
    match: Dict[str, Any] = {"status": "active"} if only_active else {}
    own = build_board_ownership(db)
    rows = db["job_postings"].aggregate([
        {"$match": match},
        {"$group": {"_id": {"a": "$ats", "t": "$tenant", "k": "$board_kind",
                            "c": {"$type": f"${FIELD}"}},
                    "n": {"$sum": 1}}},
    ], allowDiskUse=True)
    total = attributable = stamped = 0
    by_reason: Dict[str, int] = {}
    by_basis: Dict[str, int] = {}
    #: Postings on a board whose ownership is verified at all, attributed or
    #: not. This — not the whole inventory — is the denominator the acceptance
    #: criterion names, because a board we have deliberately declined to
    #: attribute must not read as a miss.
    owner_verified_postings = 0
    for row in rows:
        n = int(row["n"])
        total += n
        rid = row["_id"]
        if own.is_owner_verified(rid.get("a"), rid.get("t")):
            owner_verified_postings += n
        d = own.resolve_board(ats=rid.get("a"), tenant=rid.get("t"),
                              board_kind=rid.get("k"))
        if d.assigned:
            attributable += n
            by_basis[d.basis] = by_basis.get(d.basis, 0) + n
            if rid.get("c") == "string":
                stamped += n
        else:
            by_reason[d.reason] = by_reason.get(d.reason, 0) + n
    return {
        "canonical_employer_version": CANONICAL_EMPLOYER_VERSION,
        "postings_considered": total,
        "owner_verified_postings": owner_verified_postings,
        "attributable_postings": attributable,
        "attributed_postings": stamped,
        # Of what CAN be attributed, how much IS. Answers "did the backfill
        # finish?"
        "attribution_pct": round(stamped / attributable, 4) if attributable else 0.0,
        # Of everything on an owner-verified board, how much carries an id.
        # Answers the acceptance criterion, and is always the lower number
        # because a contested board is owner-verified and unattributed.
        "owner_verified_attribution_pct": round(
            stamped / owner_verified_postings, 4) if owner_verified_postings else 0.0,
        "attributed_by_basis": by_basis,
        "quarantined_by_reason": by_reason,
        "attributing_boards": own.boards,
        "unambiguous_boards": own.unambiguous_boards,
        "contested_boards": len(own.conflicts),
        "contested_boards_settled_by_precedence": len(own.precedence_boards),
        "contested_boards_unresolved": len(own.unresolved_conflicts),
        "career_sources_read": own.sources_read,
    }
