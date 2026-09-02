"""A career source is a board we can PROVE the employer owns, or it is a candidate.

The defect this fixes
---------------------
`company_ats_endpoints.status` goes to `"validated"` when the endpoint
*answers*. Measured on live `aspirely_db` 2026-09-02: 2,111 of 2,592 endpoint
rows are `validated`, and a representative one reads

    {"member_key": "ashby-uipath", "sources": ["discovery", "slug-probe"],
     "status": "validated", "tenant": "uipath", "job_count_last": 5}

There is no field anywhere in that document that answers "how do we know this
board is UiPath's?" — because the answer is "we guessed the slug and it
returned five jobs". `ats_providers.util.slug_is_trustworthy` is the only
gate, and it is a string-similarity heuristic. It admits:

    Forward        forward.com (healthcare) vs forwardnetworks / Forward Air
    Beyond         beyond.com   vs Beyond Meat vs Beyond Finance
    Public         public.com   vs Public Storage vs Public Consulting Group
    CTS            CTS Corp vs Cognizant Technology Solutions vs CTS Cement
    Super          super.com    vs Super Micro vs SuperAnnotate
    Archer         Archer Aviation vs Archer Daniels Midland vs Archer DX
    CoStar         `costar` on Greenhouse declares "Co-Star" — an astrology
                   app — and `names_match` normalises the two identically
    Alphabet       Alphabet Inc. vs ALPHABET, BMW's fleet-leasing arm
    Capital One    capitalone.com vs capital.com, a CFD trading site

Every one of those is currently reachable by the probe path.

This module owns the collection that fixes it. A career source is only
`validated` when BOTH questions have documented answers:

    ownership  — a link or redirect chain from an independently verified
                 official employer domain to this exact endpoint, OR two
                 INDEPENDENT corroborating signals
    content    — the endpoint returned parseable postings we can attribute

Neither answer may come from the slug. `SIGNAL_SLUG_SIMILARITY` exists in the
vocabulary precisely so it can be recorded and then explicitly excluded from
the count — a heuristic that is written down and ignored is auditable; one
that is absent is indistinguishable from one that was never checked.

Relationship to `company_ats_endpoints`
---------------------------------------
Additive. `career_sources` is a new collection keyed by `source_id` and
carrying `member_key`, so it joins to the existing endpoint row without
changing it. The crawler keeps reading endpoints; the COVERAGE NUMBER starts
reading career sources. That split is what lets the ownership work land
without a crawl outage, and it is why the migration is a backfill rather than
a rewrite.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

CAREER_SOURCE_VERSION = "career-source-1.0.0"

CAREER_SOURCES = "career_sources"

# --------------------------------------------------------------------------
# The state machine
# --------------------------------------------------------------------------
#: Discovered, unproven. A guessed slug that answers may only ever reach here.
STATE_CANDIDATE = "candidate"
#: We can show who owns it. Says nothing about whether it has jobs.
STATE_OWNERSHIP_VERIFIED = "ownership_verified"
#: It returned attributable postings. Says nothing about who owns it.
STATE_CONTENT_VALIDATED = "content_validated"
#: Both. The ONLY state that counts toward coverage.
STATE_VALIDATED = "validated"
#: Owner-verified and genuinely hiring nobody. A real, reportable answer.
STATE_EMPTY = "empty"
#: A documented temporary blocker (403/429/challenge) with a retry time.
STATE_BLOCKED = "blocked"
#: The board moved. `redirect_chain` holds where, and it is re-fingerprinted.
STATE_MOVED = "moved"
#: Repeated hard failure. Retained, never deleted — deleting is how an
#: employer silently leaves the denominator.
STATE_DEAD = "dead"
#: A human must look. Ambiguous ownership lands here, never in `validated`.
STATE_MANUAL_REVIEW = "manual_review"

STATES: Tuple[str, ...] = (
    STATE_CANDIDATE, STATE_OWNERSHIP_VERIFIED, STATE_CONTENT_VALIDATED,
    STATE_VALIDATED, STATE_EMPTY, STATE_BLOCKED, STATE_MOVED, STATE_DEAD,
    STATE_MANUAL_REVIEW,
)

#: States that count as "we have this employer's official career source".
#: `empty` counts: an owner-verified board with nothing on it is a verified
#: fact about the employer, not a gap in our crawl.
COVERED_STATES: Tuple[str, ...] = (STATE_VALIDATED, STATE_EMPTY)

#: States that must never silently vanish from a coverage report.
ACCOUNTABLE_STATES: Tuple[str, ...] = STATES

# --------------------------------------------------------------------------
# Failure codes — a closed vocabulary so the report can group them
# --------------------------------------------------------------------------
FAIL_NONE = ""
FAIL_HTTP_404 = "http_404"
FAIL_HTTP_403 = "http_403"
FAIL_HTTP_429 = "http_429"
FAIL_HTTP_5XX = "http_5xx"
FAIL_TIMEOUT = "timeout"
FAIL_DNS = "dns"
FAIL_ROBOTS = "robots_disallow"
FAIL_CHALLENGE = "bot_challenge"
FAIL_REDIRECTED = "redirected"
FAIL_PARSE = "unparseable_response"
FAIL_NO_POSTINGS = "no_postings"
FAIL_UNSUPPORTED = "unsupported_provider"
FAIL_OWNER_MISMATCH = "owner_mismatch"
FAIL_OWNER_UNPROVEN = "owner_unproven"
FAIL_AMBIGUOUS_OWNER = "ambiguous_owner"

FAILURE_CODES: Tuple[str, ...] = (
    FAIL_NONE, FAIL_HTTP_404, FAIL_HTTP_403, FAIL_HTTP_429, FAIL_HTTP_5XX,
    FAIL_TIMEOUT, FAIL_DNS, FAIL_ROBOTS, FAIL_CHALLENGE, FAIL_REDIRECTED,
    FAIL_PARSE, FAIL_NO_POSTINGS, FAIL_UNSUPPORTED, FAIL_OWNER_MISMATCH,
    FAIL_OWNER_UNPROVEN, FAIL_AMBIGUOUS_OWNER,
)

# --------------------------------------------------------------------------
# Discovery methods
# --------------------------------------------------------------------------
METHOD_OFFICIAL_REGISTRY = "official_registry"
METHOD_OFFICIAL_LINK = "official_domain_link"
METHOD_OFFICIAL_REDIRECT = "official_domain_redirect"
METHOD_PROVIDER_METADATA = "provider_owner_metadata"
METHOD_CURATED_CATALOG = "curated_catalog"
METHOD_SLUG_PROBE = "slug_probe"
METHOD_TENANT_ARCHIVE = "tenant_archive"
METHOD_FINGERPRINT = "page_fingerprint"

DISCOVERY_METHODS: Tuple[str, ...] = (
    METHOD_OFFICIAL_REGISTRY, METHOD_OFFICIAL_LINK, METHOD_OFFICIAL_REDIRECT,
    METHOD_PROVIDER_METADATA, METHOD_CURATED_CATALOG, METHOD_SLUG_PROBE,
    METHOD_TENANT_ARCHIVE, METHOD_FINGERPRINT,
)

# --------------------------------------------------------------------------
# Evidence signals and their independence classes
# --------------------------------------------------------------------------
#: A signal's CLASS is what makes it independent. Two signals of the same
#: class are one signal repeated, and never satisfy the two-signal rule.
CLASS_EMPLOYER_DOMAIN = "employer_domain"
CLASS_ATS_PROVIDER = "ats_provider"
CLASS_CURATED = "curated_registry"
CLASS_HEURISTIC = "heuristic"

#: Sufficient ON ITS OWN: the employer's own verified domain points here.
SIGNAL_DOMAIN_LINK = "official_domain_link"
SIGNAL_DOMAIN_REDIRECT = "official_domain_redirect"
#: Corroborating: the provider itself names the board's owner.
SIGNAL_PROVIDER_OWNER = "provider_owner_metadata"
#: Corroborating: the board's own metadata points back at the verified domain.
SIGNAL_BOARD_BACKLINK = "board_declares_employer_domain"
#: Corroborating: a human-maintained registry row naming this exact endpoint.
SIGNAL_CURATED_ROW = "curated_registry_row"
#: RECORDED AND NEVER COUNTED. This is the slug-only trust, made visible.
SIGNAL_SLUG_SIMILARITY = "slug_similarity"
#: RECORDED AND NEVER COUNTED. "The endpoint answered" is content, not owner.
SIGNAL_ENDPOINT_RESPONDS = "endpoint_responds"

SIGNAL_CLASSES: Dict[str, str] = {
    SIGNAL_DOMAIN_LINK: CLASS_EMPLOYER_DOMAIN,
    SIGNAL_DOMAIN_REDIRECT: CLASS_EMPLOYER_DOMAIN,
    SIGNAL_BOARD_BACKLINK: CLASS_EMPLOYER_DOMAIN,
    SIGNAL_PROVIDER_OWNER: CLASS_ATS_PROVIDER,
    SIGNAL_CURATED_ROW: CLASS_CURATED,
    SIGNAL_SLUG_SIMILARITY: CLASS_HEURISTIC,
    SIGNAL_ENDPOINT_RESPONDS: CLASS_HEURISTIC,
}

#: Any one of these proves ownership by itself (rule 4a).
SUFFICIENT_SIGNALS: Tuple[str, ...] = (SIGNAL_DOMAIN_LINK, SIGNAL_DOMAIN_REDIRECT)

#: Two of these, from two DIFFERENT classes, prove ownership (rule 4b).
CORROBORATING_SIGNALS: Tuple[str, ...] = (
    SIGNAL_PROVIDER_OWNER, SIGNAL_BOARD_BACKLINK, SIGNAL_CURATED_ROW,
)

#: Never counted, in either rule. Listed so the exclusion is testable.
NON_PROBATIVE_SIGNALS: Tuple[str, ...] = (
    SIGNAL_SLUG_SIMILARITY, SIGNAL_ENDPOINT_RESPONDS,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def evidence(kind: str, *, source: str = "", value: Any = "",
             confirmed: bool = True, at: Optional[datetime] = None) -> Dict[str, Any]:
    """One evidence record. `confirmed=False` records a check that RAN and
    came back negative — which is different from a check that never ran, and
    the difference is the whole audit trail."""
    return {
        "kind": str(kind),
        "signal_class": SIGNAL_CLASSES.get(str(kind), CLASS_HEURISTIC),
        "source": str(source or "")[:200],
        "value": str(value or "")[:500],
        "confirmed": bool(confirmed),
        "at": at or _now(),
    }


def adjudicate_ownership(
    signals: Sequence[Dict[str, Any]],
) -> Tuple[bool, str, str]:
    """`(owned, failure_code, reason)` from the evidence gathered so far.

    This function is the entire ownership policy, and it is deliberately
    boring: no scores, no thresholds, no "close enough". Either a verified
    employer domain led here, or two independent parties said so, or we do not
    know. `slug_is_trustworthy` cannot appear anywhere in this decision — the
    signal is in the vocabulary only so a report can say we looked at it and
    declined to count it.
    """
    confirmed = [s for s in (signals or ()) if isinstance(s, dict) and s.get("confirmed")]
    kinds = {str(s.get("kind")) for s in confirmed}

    # A confirmed mismatch is decisive and beats every positive signal: if the
    # provider says this board belongs to somebody else, it belongs to
    # somebody else. This is the CoStar / Alphabet / Capital One case.
    for s in (signals or ()):
        if isinstance(s, dict) and s.get("kind") == SIGNAL_PROVIDER_OWNER \
                and not s.get("confirmed"):
            return False, FAIL_OWNER_MISMATCH, (
                f"provider names a different owner: {s.get('value') or 'unknown'}")

    strong = kinds & set(SUFFICIENT_SIGNALS)
    if strong:
        return True, FAIL_NONE, f"verified employer domain -> endpoint ({sorted(strong)[0]})"

    corroborating = [s for s in confirmed
                     if str(s.get("kind")) in CORROBORATING_SIGNALS]
    classes = {str(s.get("signal_class")) for s in corroborating}
    classes.discard(CLASS_HEURISTIC)
    if len(classes) >= 2:
        return True, FAIL_NONE, (
            "two independent corroborating signals: " + ", ".join(sorted(classes)))

    if kinds & set(NON_PROBATIVE_SIGNALS) and not corroborating:
        return False, FAIL_OWNER_UNPROVEN, (
            "only non-probative signals present "
            "(a responding endpoint and a similar slug are not ownership)")
    if len(classes) == 1:
        return False, FAIL_OWNER_UNPROVEN, (
            f"one corroborating signal ({sorted(classes)[0]}); the rule needs two "
            f"independent classes")
    return False, FAIL_OWNER_UNPROVEN, "no ownership evidence gathered"


def next_state(
    *,
    owned: bool,
    content_ok: Optional[bool],
    job_count: int = 0,
    failure_code: str = FAIL_NONE,
    moved_to: str = "",
    consecutive_failures: int = 0,
    ambiguous: bool = False,
) -> str:
    """The single place a career source's state is decided.

    `content_ok is None` means content was not checked this pass, which is
    NOT the same as a failed check — it leaves the content half of the verdict
    exactly where it was rather than demoting on absence of evidence.
    """
    if ambiguous:
        return STATE_MANUAL_REVIEW
    if moved_to:
        return STATE_MOVED
    if failure_code in (FAIL_HTTP_403, FAIL_HTTP_429, FAIL_CHALLENGE, FAIL_ROBOTS):
        return STATE_BLOCKED
    if failure_code == FAIL_UNSUPPORTED:
        return STATE_CANDIDATE
    if failure_code in (FAIL_HTTP_404, FAIL_DNS) or consecutive_failures >= DEAD_AFTER_FAILURES:
        return STATE_DEAD
    if not owned:
        # Content alone can never promote past `content_validated` - that is
        # the whole point of splitting the two halves.
        if content_ok and job_count > 0:
            return STATE_CONTENT_VALIDATED
        return STATE_CANDIDATE
    if content_ok is None:
        return STATE_OWNERSHIP_VERIFIED
    if content_ok and job_count > 0:
        return STATE_VALIDATED
    if content_ok:
        return STATE_EMPTY
    return STATE_OWNERSHIP_VERIFIED


DEAD_AFTER_FAILURES = 8

#: How long a validated source stays trusted. Shorter than the employer
#: identity interval because boards migrate far more often than companies
#: rename: the Rivian iCIMS tenant became a 143-byte redirect and nothing
#: re-fingerprinted it for months.
RECHECK_DAYS_VALIDATED = int(os.getenv("CAREER_SOURCE_RECHECK_DAYS", "14") or 14)
RECHECK_DAYS_EMPTY = 21
RECHECK_DAYS_BLOCKED = 3
RECHECK_DAYS_MOVED = 1
RECHECK_DAYS_DEAD = 45
RECHECK_DAYS_CANDIDATE = 7

_RECHECK: Dict[str, int] = {
    STATE_VALIDATED: RECHECK_DAYS_VALIDATED,
    STATE_CONTENT_VALIDATED: RECHECK_DAYS_CANDIDATE,
    STATE_OWNERSHIP_VERIFIED: RECHECK_DAYS_CANDIDATE,
    STATE_EMPTY: RECHECK_DAYS_EMPTY,
    STATE_BLOCKED: RECHECK_DAYS_BLOCKED,
    STATE_MOVED: RECHECK_DAYS_MOVED,
    STATE_DEAD: RECHECK_DAYS_DEAD,
    STATE_CANDIDATE: RECHECK_DAYS_CANDIDATE,
    STATE_MANUAL_REVIEW: 30,
}


def next_check_after(state: str, *, now: Optional[datetime] = None) -> datetime:
    return (now or _now()) + timedelta(days=_RECHECK.get(state, 7))


# --------------------------------------------------------------------------
# Identity
# --------------------------------------------------------------------------
def _norm_url(value: Any) -> str:
    s = str(value or "").strip()
    if not s:
        return ""
    s = re.sub(r"#.*$", "", s)
    return s[:500]


def source_id_for(*, employer_id: Any, ats_vendor: Any, tenant: Any,
                  canonical_endpoint: Any = "") -> str:
    """Stable id for one (employer, vendor, tenant) board.

    `canonical_endpoint` is deliberately NOT in the key: a board that changes
    its URL is the same career source at a new address, and re-keying on
    migration would make `moved` unrepresentable.
    """
    key = "|".join([
        str(employer_id or "").strip(),
        re.sub(r"[^a-z0-9_]+", "", str(ats_vendor or "").lower()),
        re.sub(r"[^a-z0-9]+", "", str(tenant or "").lower()),
    ])
    if not key.strip("|"):
        raise ValueError("source_id_for: needs at least employer_id or vendor+tenant")
    return "cs_" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def build_source(
    *,
    employer_id: Any,
    ats_vendor: Any = "",
    tenant: Any = "",
    verified_domain: Any = "",
    career_landing_url: Any = "",
    official_jobs_url: Any = "",
    canonical_endpoint: Any = "",
    redirect_chain: Sequence[Any] = (),
    discovery_method: str = METHOD_SLUG_PROBE,
    ownership_evidence: Sequence[Dict[str, Any]] = (),
    content_validation_evidence: Sequence[Dict[str, Any]] = (),
    member_key: Any = "",
    state: Optional[str] = None,
    failure_code: str = FAIL_NONE,
    failure_detail: Any = "",
    job_count_last: int = 0,
    first_seen_at: Optional[datetime] = None,
) -> Dict[str, Any]:
    """A complete `career_sources` document. Pure — no DB, no network.

    When `state` is omitted it is DERIVED from the evidence rather than
    supplied, so no caller can hand-write `validated` onto a row that has not
    earned it.
    """
    eid = str(employer_id or "").strip()
    vendor = re.sub(r"[^a-z0-9_]+", "", str(ats_vendor or "").lower())
    ten = str(tenant or "").strip()[:200]
    own_ev = [dict(e) for e in (ownership_evidence or ()) if isinstance(e, dict)]
    con_ev = [dict(e) for e in (content_validation_evidence or ()) if isinstance(e, dict)]

    owned, own_fail, own_reason = adjudicate_ownership(own_ev)
    content_ok: Optional[bool] = None
    if con_ev:
        content_ok = any(e.get("confirmed") for e in con_ev)

    resolved = state or next_state(
        owned=owned, content_ok=content_ok, job_count=int(job_count_last or 0),
        failure_code=failure_code or (own_fail if not owned else FAIL_NONE))
    if resolved not in STATES:
        resolved = STATE_CANDIDATE

    method = discovery_method if discovery_method in DISCOVERY_METHODS else METHOD_SLUG_PROBE
    code = failure_code if failure_code in FAILURE_CODES else FAIL_NONE
    if not code and not owned:
        code = own_fail
    detail = str(failure_detail or ("" if owned else own_reason))[:400]

    now = _now()
    return {
        "source_id": source_id_for(employer_id=eid, ats_vendor=vendor, tenant=ten),
        "employer_id": eid,
        "member_key": str(member_key or "")[:200],
        "verified_domain": str(verified_domain or "").strip().lower()[:200],
        "career_landing_url": _norm_url(career_landing_url),
        "official_jobs_url": _norm_url(official_jobs_url),
        "ats_vendor": vendor,
        "tenant": ten,
        "canonical_endpoint": _norm_url(canonical_endpoint),
        "redirect_chain": [_norm_url(u) for u in (redirect_chain or ())][:10],
        "discovery_method": method,
        "state": resolved,
        "ownership_verified": bool(owned),
        "ownership_reason": own_reason[:300],
        "ownership_evidence": own_ev[:20],
        "content_validation_evidence": con_ev[:20],
        "job_count_last": int(job_count_last or 0),
        "consecutive_failures": 0,
        "failure_code": code,
        "failure_detail": detail,
        "first_seen_at": first_seen_at or now,
        "last_verified_at": now if resolved in COVERED_STATES else None,
        "last_checked_at": now,
        "next_check_at": next_check_after(resolved, now=now),
        "career_source_version": CAREER_SOURCE_VERSION,
        "updated_at": now,
    }


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------
def _db():
    from utils.db_connect import DBConnect
    return DBConnect().get_db()


def _col():
    return _db()[CAREER_SOURCES]


def ensure_indexes() -> List[str]:
    created: List[str] = []
    try:
        col = _col()
        created.append(col.create_index("source_id", unique=True, name="source_id_unique"))
        created.append(col.create_index([("employer_id", 1), ("state", 1)],
                                        name="employer_state_idx"))
        created.append(col.create_index([("state", 1), ("next_check_at", 1)],
                                        name="due_check_idx"))
        created.append(col.create_index([("ats_vendor", 1), ("tenant", 1)],
                                        name="vendor_tenant_idx"))
        created.append(col.create_index("member_key", name="member_key_idx", sparse=True))
        created.append(col.create_index([("state", 1), ("ats_vendor", 1)],
                                        name="state_vendor_idx"))
        created.append(col.create_index("verified_domain", name="verified_domain_idx",
                                        sparse=True))
    except Exception as exc:  # pragma: no cover
        logger.warning("career_source_service.ensure_indexes: %s", exc)
    return [c for c in created if c]


#: Fields a re-check may overwrite. Everything else is append-only or
#: insert-only, so a transient failure can never erase the ownership proof
#: that a slower, more expensive check established.
_MUTABLE = (
    "state", "ownership_verified", "ownership_reason", "job_count_last",
    "failure_code", "failure_detail", "last_checked_at", "next_check_at",
    "canonical_endpoint", "redirect_chain", "career_landing_url",
    "official_jobs_url", "verified_domain", "member_key", "discovery_method",
    "career_source_version", "updated_at",
)


#: States that mean CONTENT HAS BEEN SEEN. `validated` and `empty` are the two
#: covered states; `content_validated` is content without ownership.
_CONTENT_STATES = (STATE_VALIDATED, STATE_EMPTY, STATE_CONTENT_VALIDATED)


def _registration_state(stored: str, incoming: str, *, owned: bool) -> str:
    """The state a REGISTRATION may write over `stored`.

    Registration answers one of the state machine's two questions — who owns
    this board — and says nothing about whether it serves jobs. So:

      * a stored state carrying a CONTENT verdict is never overwritten by one
        that does not (`validated` -> `ownership_verified` was removing
        employers from coverage on a pass that had just PROVEN their boards);
      * except that `content_validated` PLUS newly proven ownership is exactly
        `validated` — content was already read and found postings, and the
        only thing it was missing has just arrived. Leaving it at
        `content_validated` would be the same bug facing the other way.
    """
    if not stored or stored == incoming:
        return incoming
    if stored == STATE_CONTENT_VALIDATED and owned:
        return STATE_VALIDATED
    if _demotes_content(stored, incoming):
        return stored
    return incoming


def _demotes_content(stored: str, incoming: str) -> bool:
    """Would writing `incoming` over `stored` forget a content verdict?

    Registration answers ONE of the two questions the state machine asks —
    who owns this board — and says nothing at all about whether it serves
    jobs. `upsert_source` nevertheless `$set` the state unconditionally, so
    re-registering an employer whose board was already `validated` rewrote it
    to `ownership_verified` and removed the employer from coverage. Measured:
    `verify_official_career_urls.py --apply --register` moved Intel, Adobe,
    Accenture, Palantir, Splunk, DeepL, d-Matrix, Aikido and Lakera out of the
    covered set in one run, and Tier-A coverage read 24/94 instead of 30/94 —
    a reporting number moving on a write that proved something GOOD.

    Demotion is the content pass's job, through `record_failure`, which has
    evidence for it. This one does not.
    """
    return bool(stored) and stored in _CONTENT_STATES \
        and incoming not in _CONTENT_STATES


def upsert_source(record: Dict[str, Any]) -> str:
    """Insert or update one career source. Returns `source_id`.

    Evidence lists are `$addToSet`-merged and capped, never replaced — the
    trail of how ownership was established outlives any single check.
    """
    sid = record.get("source_id")
    if not sid:
        raise ValueError("upsert_source: record has no source_id")
    col = _col()
    now = _now()
    set_fields = {k: record[k] for k in _MUTABLE if k in record}
    set_fields["updated_at"] = now
    if record.get("last_verified_at"):
        set_fields["last_verified_at"] = record["last_verified_at"]
    if "state" in set_fields:
        stored = col.find_one({"source_id": sid}, {"_id": 0, "state": 1}) or {}
        # Everything else this registration learned still lands - the
        # endpoint, the redirect chain, the verified domain, the evidence.
        # Only the STATE is adjudicated.
        set_fields["state"] = _registration_state(
            str(stored.get("state") or ""), str(set_fields["state"] or ""),
            owned=bool(record.get("ownership_verified")))

    push: Dict[str, Any] = {}
    for field in ("ownership_evidence", "content_validation_evidence"):
        items = [e for e in (record.get(field) or []) if isinstance(e, dict)]
        if items:
            push[field] = {"$each": items, "$slice": -20}

    update: Dict[str, Any] = {
        "$set": set_fields,
        "$setOnInsert": {
            "source_id": sid,
            "employer_id": record.get("employer_id") or "",
            "ats_vendor": record.get("ats_vendor") or "",
            "tenant": record.get("tenant") or "",
            "first_seen_at": record.get("first_seen_at") or now,
            "consecutive_failures": 0,
        },
    }
    if push:
        update["$push"] = push
    col.update_one({"source_id": sid}, update, upsert=True)
    return sid


def bulk_upsert_sources(records: Iterable[Dict[str, Any]], *,
                        batch_size: int = 500) -> Dict[str, int]:
    """`upsert_source` for a whole seeding run, in batches. `{seen, written}`."""
    from pymongo import UpdateOne
    from pymongo.errors import BulkWriteError

    col = _col()
    now = _now()
    seen = written = 0
    ops: List[Any] = []

    def _flush() -> int:
        if not ops:
            return 0
        try:
            res = col.bulk_write(ops, ordered=False)
        except BulkWriteError as exc:
            errs = exc.details.get("writeErrors") or []
            logger.error("bulk_upsert_sources: %d write error(s); first: %s",
                         len(errs), (errs[0].get("errmsg") if errs else "")[:300])
            return int((exc.details.get("nUpserted") or 0)
                       + (exc.details.get("nModified") or 0))
        except (TypeError, NotImplementedError) as exc:
            logger.debug("bulk_upsert_sources: bulk unavailable (%s); "
                         "falling back to sequential", type(exc).__name__)
            n = 0
            for op in ops:
                try:
                    col.update_one(op._filter, op._doc, upsert=True)
                    n += 1
                except Exception as inner:  # pragma: no cover
                    logger.warning("bulk_upsert_sources: %s", inner)
            return n
        return int((res.upserted_count or 0) + (res.modified_count or 0))

    # The same guard `upsert_source` applies, at seeding scale: one read per
    # BATCH rather than one per record. Chunked at `batch_size` because a
    # seeding run passes every source in the registry and an `$in` of eleven
    # thousand ids is a query document nobody wants to explain.
    batch = [r for r in records if r.get("source_id")]
    stored_states: Dict[str, str] = {}
    for i in range(0, len(batch), max(1, int(batch_size))):
        ids = [r["source_id"] for r in batch[i:i + max(1, int(batch_size))]]
        for d in col.find({"source_id": {"$in": ids}},
                          {"_id": 0, "source_id": 1, "state": 1}):
            stored_states[str(d.get("source_id"))] = str(d.get("state") or "")

    for record in batch:
        sid = record.get("source_id")
        seen += 1
        set_fields = {k: record[k] for k in _MUTABLE if k in record}
        set_fields["updated_at"] = now
        if record.get("last_verified_at"):
            set_fields["last_verified_at"] = record["last_verified_at"]
        if "state" in set_fields:
            set_fields["state"] = _registration_state(
                stored_states.get(str(sid), ""),
                str(set_fields["state"] or ""),
                owned=bool(record.get("ownership_verified")))
        update: Dict[str, Any] = {
            "$set": set_fields,
            "$setOnInsert": {
                "source_id": sid,
                "employer_id": record.get("employer_id") or "",
                "ats_vendor": record.get("ats_vendor") or "",
                "tenant": record.get("tenant") or "",
                "first_seen_at": record.get("first_seen_at") or now,
                "consecutive_failures": 0,
            },
        }
        push: Dict[str, Any] = {}
        for field in ("ownership_evidence", "content_validation_evidence"):
            items = [e for e in (record.get(field) or []) if isinstance(e, dict)]
            if items:
                push[field] = {"$each": items, "$slice": -20}
        if push:
            update["$push"] = push
        ops.append(UpdateOne({"source_id": sid}, update, upsert=True))
        if len(ops) >= batch_size:
            written += _flush()
            ops = []
    written += _flush()
    return {"seen": seen, "written": written}


def record_failure(source_id: str, *, failure_code: str, detail: str = "") -> str:
    """Count a failure and let the state machine decide if it is fatal.

    Returns the resulting state. A blocked or moved source never accrues
    failures — those are conditions, not errors, and treating them as errors
    is how an employer that merely rate-limits us ends up `dead` and drops out
    of the denominator.
    """
    col = _col()
    row = col.find_one({"source_id": source_id})
    if not row:
        return ""
    code = failure_code if failure_code in FAILURE_CODES else FAIL_PARSE
    transient = code in (FAIL_HTTP_403, FAIL_HTTP_429, FAIL_CHALLENGE, FAIL_ROBOTS)
    fails = int(row.get("consecutive_failures") or 0) + (0 if transient else 1)
    state = next_state(
        owned=bool(row.get("ownership_verified")),
        content_ok=False,
        job_count=0,
        failure_code=code,
        consecutive_failures=fails,
    )
    col.update_one({"source_id": source_id}, {"$set": {
        "consecutive_failures": fails,
        "state": state,
        "failure_code": code,
        "failure_detail": str(detail or "")[:400],
        "last_checked_at": _now(),
        "next_check_at": next_check_after(state),
        "updated_at": _now(),
    }})
    return state


def record_note(source_id: str, detail: Any) -> str:
    """Record an observation WITHOUT touching the state.

    For a check that ran and whose verdict we are declining to act on, which
    is a real and important case: a 403 to our prober is a fact about our
    request, not about the board, and when the live inventory holds fresh
    postings from that exact endpoint the board is plainly serving jobs. The
    note keeps the audit trail — the check happened, and here is why nothing
    moved — instead of silently skipping.
    """
    col = _col()
    now = _now()
    row = col.find_one({"source_id": source_id}, {"state": 1})
    if not row:
        return ""
    col.update_one({"source_id": source_id}, {
        "$set": {"last_checked_at": now,
                 "next_check_at": next_check_after(str(row.get("state") or "")),
                 "last_note": str(detail or "")[:400],
                 "updated_at": now},
    })
    return str(row.get("state") or "")


def record_success(source_id: str, *, job_count: int, sample_title: str = "") -> str:
    """A successful content fetch. Promotes only if ownership already holds."""
    col = _col()
    row = col.find_one({"source_id": source_id})
    if not row:
        return ""
    owned = bool(row.get("ownership_verified"))
    state = next_state(owned=owned, content_ok=True, job_count=int(job_count or 0))
    now = _now()
    col.update_one({"source_id": source_id}, {
        "$set": {
            "state": state, "job_count_last": int(job_count or 0),
            "consecutive_failures": 0, "failure_code": FAIL_NONE,
            "failure_detail": "", "last_checked_at": now,
            "last_verified_at": now if state in COVERED_STATES else row.get("last_verified_at"),
            "next_check_at": next_check_after(state, now=now), "updated_at": now,
        },
        "$push": {"content_validation_evidence": {
            "$each": [evidence("content_fetch", source=row.get("ats_vendor") or "",
                               value=f"{int(job_count or 0)} postings"
                                     + (f"; e.g. {sample_title[:80]}" if sample_title else ""))],
            "$slice": -20}},
    })
    return state


def record_move(source_id: str, destination: str, *, chain: Sequence[str] = ()) -> str:
    """A portal migration. Recorded, not swallowed — `moved` is due for
    re-fingerprinting tomorrow, which is what nothing did for Rivian."""
    now = _now()
    _col().update_one({"source_id": source_id}, {"$set": {
        "state": STATE_MOVED,
        "failure_code": FAIL_REDIRECTED,
        "failure_detail": f"moved to {str(destination or '')[:200]}",
        "redirect_chain": [_norm_url(u) for u in (chain or (destination,))][:10],
        "last_checked_at": now,
        "next_check_at": next_check_after(STATE_MOVED, now=now),
        "updated_at": now,
    }})
    return STATE_MOVED


def get_source(source_id: str) -> Optional[Dict[str, Any]]:
    return _col().find_one({"source_id": str(source_id or "")})


def sources_for_employer(employer_id: str) -> List[Dict[str, Any]]:
    return list(_col().find({"employer_id": str(employer_id or "")}))


def due_for_check(limit: int = 500, *, states: Sequence[str] = ()) -> List[Dict[str, Any]]:
    q: Dict[str, Any] = {"$or": [{"next_check_at": {"$lte": _now()}},
                                 {"next_check_at": None}]}
    if states:
        q["state"] = {"$in": list(states)}
    return list(_col().find(q).sort("next_check_at", 1).limit(int(limit)))


def iter_sources(*, states: Sequence[str] = (), vendor: str = "",
                 limit: int = 0) -> Iterator[Dict[str, Any]]:
    q: Dict[str, Any] = {}
    if states:
        q["state"] = {"$in": list(states)}
    if vendor:
        q["ats_vendor"] = vendor
    cur = _col().find(q)
    if limit:
        cur = cur.limit(int(limit))
    return iter(cur)


def counts() -> Dict[str, Any]:
    """Career-source shape, for the coverage report and metrics snapshot."""
    col = _col()

    def _by(field: str) -> Dict[str, int]:
        return {str(r["_id"]): int(r["n"]) for r in
                col.aggregate([{"$group": {"_id": f"${field}", "n": {"$sum": 1}}}])}

    total = col.estimated_document_count()
    by_state = _by("state")
    covered = sum(by_state.get(s, 0) for s in COVERED_STATES)
    return {
        "total": total,
        "by_state": by_state,
        "by_vendor": _by("ats_vendor"),
        "by_discovery_method": _by("discovery_method"),
        "by_failure_code": _by("failure_code"),
        "covered": covered,
        "covered_pct": round(covered / total, 4) if total else 0.0,
        "ownership_verified": col.count_documents({"ownership_verified": True}),
        "career_source_version": CAREER_SOURCE_VERSION,
    }


def coverage_by_employer(employer_ids: Iterable[str]) -> Dict[str, Dict[str, Any]]:
    """`{employer_id: {state, sources, covered}}` for a cohort, in one query.

    Every requested employer appears in the result, including those with no
    career source at all — an employer that is absent from the output is
    exactly how one silently leaves the denominator.
    """
    ids = [str(e) for e in employer_ids if e]
    out: Dict[str, Dict[str, Any]] = {
        eid: {"state": STATE_CANDIDATE, "sources": 0, "covered": False,
              "detail": "no career source discovered"} for eid in ids}
    if not ids:
        return out
    # Rank states so an employer with one validated and three dead sources
    # reads as validated. Ordering is "best evidence wins".
    rank = {STATE_VALIDATED: 0, STATE_EMPTY: 1, STATE_CONTENT_VALIDATED: 2,
            STATE_OWNERSHIP_VERIFIED: 3, STATE_MOVED: 4, STATE_BLOCKED: 5,
            STATE_MANUAL_REVIEW: 6, STATE_CANDIDATE: 7, STATE_DEAD: 8}
    for row in _col().find({"employer_id": {"$in": ids}},
                           {"employer_id": 1, "state": 1, "failure_detail": 1,
                            "ownership_reason": 1}):
        eid = str(row.get("employer_id"))
        cur = out.get(eid)
        if cur is None:
            continue
        cur["sources"] += 1
        st = str(row.get("state") or STATE_CANDIDATE)
        if rank.get(st, 9) < rank.get(cur["state"], 9) or cur["sources"] == 1:
            cur["state"] = st
            cur["detail"] = str(row.get("failure_detail")
                                or row.get("ownership_reason") or "")[:300]
        cur["covered"] = cur["state"] in COVERED_STATES
    return out
