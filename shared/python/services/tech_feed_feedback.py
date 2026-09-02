"""Per-user feedback on tech-feed results, and the suppression set it implies.

One document per `(user_email, job_id)` in `tech_feed_feedback`. Re-submitting
the same pair updates that row rather than adding a second one, so "I already
said no to this" is a fact with one representation and undo is a single delete.

The distinction this module exists to hold
------------------------------------------
The six reasons are NOT six flavours of "hide it". They split cleanly into two
kinds, and conflating them is how a feedback widget quietly starts censoring
the market:

* PREFERENCE - the user is telling us what they want to see:
    `not_interested`   - "not this job"
    `irrelevant_role`  - "this is not the kind of work I asked for"
    `duplicate`        - "I am already looking at this posting"
  These three, and only these three, SUPPRESS the job for that user.

* DATA QUALITY - the user is reporting that the RECORD is wrong:
    `wrong_company`      - the posting is attributed to the wrong employer
    `stale_job`          - the posting looks closed
    `incorrect_metadata` - salary / location / seniority / date is wrong
  These are recorded and counted so ingestion can be fixed, and they NEVER
  hide a posting from anyone.

`wrong_company` is the one that most invites the mistake, so it is worth
stating plainly: it is a report about ATTRIBUTION, not a preference. A user
who says "this is not really Google" is telling us our employer resolution is
wrong; they are not telling us to stop showing them the job, and they are
certainly not telling us to stop showing them that employer. Suppressing on it
would let one user's correction remove real postings from their own feed - and,
if it were ever aggregated, from everyone's - on the strength of an unverified
click. Fix the data instead; that is what `counts()` is for.

Same reasoning for `stale_job` and `incorrect_metadata`: liveness and field
accuracy are the crawler's job (ADR-003/004, `job_liveness_probe`). A user
report is a SIGNAL to re-verify, not an authority to delete. If we hid every
posting one person called stale, a single mis-click would remove a live job
from that user's feed permanently and silently, with no way for them to tell
the difference between "gone" and "wrong".

Retention
---------
Rows carry `expire_at` and a TTL index (default 180 days, env
`TECH_FEED_FEEDBACK_TTL_DAYS`). A dismissal is a preference about a posting
that will itself be closed and purged long before then; keeping it forever
would mean a feed silently narrowing for years with no way for the user to see
why. The clock is refreshed on every update, so feedback the user keeps
re-asserting keeps living.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

COLLECTION = "tech_feed_feedback"

#: The complete, closed set of reasons. The route rejects anything else with a
#: 400 rather than storing a value no downstream reader knows how to interpret.
REASONS: Tuple[str, ...] = (
    "not_interested",
    "irrelevant_role",
    "wrong_company",
    "duplicate",
    "stale_job",
    "incorrect_metadata",
)

#: The ONLY reasons that hide a posting. See the module docstring: the other
#: three are data-quality reports, not preferences, and are never used to
#: filter. This set is the single place that decision is encoded.
SUPPRESSING_REASONS = frozenset({"not_interested", "irrelevant_role", "duplicate"})

#: Reasons that describe the RECORD rather than the user's taste. Recorded and
#: counted; never consulted by `suppressed_for`.
DATA_QUALITY_REASONS = frozenset(set(REASONS) - SUPPRESSING_REASONS)

DEFAULT_TTL_DAYS = 180
MAX_NOTE_CHARS = 500

#: Cap on each list returned by `suppressed_for`.
#:
#: Why a cap at all: the returned set is (a) sent to the client on every GET,
#: and (b) held in memory and consulted once per candidate row inside
#: `tech_feed_service.hard_filter`. Both are O(len) per search, and a search
#: only ever considers `TECH_FEED_CANDIDATES` (2,000) postings - so a
#: suppression list longer than the candidate set cannot remove anything the
#: shorter list would not already have removed in practice.
#:
#: Why 2,000 specifically: it matches `tech_feed_service.CANDIDATE_LIMIT` /
#: `MAX_ORDERED`, the existing bound on how many postings one search can reach.
#: Using the same number means the suppression set can never be the reason a
#: reachable posting is missed. Newest feedback wins (sorted by `updated_at`
#: descending), because a user who has dismissed >2,000 postings cares most
#: about what they just dismissed.
MAX_SUPPRESSED = 2000


def _db():
    from utils.db_connect import DBConnect
    return DBConnect().get_db()


def _coll():
    return _db()[COLLECTION]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ttl_days() -> int:
    """Read at call time, not import time - no env work on a cold start."""
    try:
        days = int(os.getenv("TECH_FEED_FEEDBACK_TTL_DAYS") or DEFAULT_TTL_DAYS)
    except (TypeError, ValueError):
        days = DEFAULT_TTL_DAYS
    return days if 1 <= days <= 3650 else DEFAULT_TTL_DAYS


# ---------------------------------------------------------------------------
# Sanitization
#
# CLAUDE.md invariant 4: nothing untrusted may reach a Mongo OPERATOR position.
# Two layers here:
#   1. every value is coerced to `str` BEFORE anything else, so a smuggled
#      dict/list becomes a harmless string in a VALUE position and can never be
#      interpreted as `{"$ne": ...}`;
#   2. `InputSanitizer` strips control characters and bounds the length, and a
#      leading `$` is removed so the value cannot even LOOK like an operator to
#      a future reader that builds a key from it.
# Every key in every filter built below is a literal written in this file.
# ---------------------------------------------------------------------------

def _ident(value: Any, *, max_length: int = 128) -> str:
    """An opaque id (job/dedupe-group/employer/search), safe for a value slot."""
    from utils.security import InputSanitizer

    if value is None or isinstance(value, (dict, list, tuple, set)):
        # Coerce-to-str would produce "{'$ne': None}" here; an id is never a
        # container, so reject outright rather than store a stringified dict.
        return ""
    cleaned = InputSanitizer.sanitize_string(str(value), max_length=max_length)
    while cleaned.startswith("$"):
        cleaned = cleaned[1:]
    return cleaned.strip()


def _email(value: Any) -> str:
    """The owning identity. Invalid addresses yield "" and the caller no-ops."""
    from utils.security import InputSanitizer

    if value is None or isinstance(value, (dict, list, tuple, set)):
        return ""
    return InputSanitizer.sanitize_email(str(value)) or ""


def _note(value: Any) -> str:
    from utils.security import InputSanitizer

    if value is None or isinstance(value, (dict, list, tuple, set)):
        return ""
    return InputSanitizer.sanitize_string(str(value), max_length=MAX_NOTE_CHARS)


def _reason(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    v = value.strip().lower()
    return v if v in REASONS else ""


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------

def record(user_email: str, *, job_id: str, reason: str,
           dedupe_group_id: str = "", employer_id: str = "",
           search_id: str = "", note: str = "") -> Dict[str, Any]:
    """Record one piece of feedback. Idempotent per `(user_email, job_id)`.

    Upserts, so a user who dismisses the same posting twice (two devices, a
    double tap, a retried request) ends with ONE row whose `reason` is the most
    recent thing they said. `created_at` is preserved across updates; a changed
    reason is an amendment, not a new event.

    Raises `ValueError` for an unusable identity, job id or reason. The route
    validates first and returns 400; this is defence in depth so a service-level
    caller cannot write a row no reader can interpret.
    """
    email = _email(user_email)
    if not email:
        raise ValueError("user_email is required")
    jid = _ident(job_id)
    if not jid:
        raise ValueError("job_id is required")
    rsn = _reason(reason)
    if not rsn:
        raise ValueError("reason must be one of: " + ", ".join(REASONS))

    now = _now()
    # Only literal keys; every value is a sanitized scalar.
    key = {"user_email": email, "job_id": jid}
    doc = {
        "reason": rsn,
        "suppresses": rsn in SUPPRESSING_REASONS,
        "dedupe_group_id": _ident(dedupe_group_id),
        "employer_id": _ident(employer_id),
        "search_id": _ident(search_id, max_length=64),
        "note": _note(note),
        "updated_at": now,
        # Refreshed on every write: feedback the user keeps re-asserting keeps
        # living, feedback they stop repeating ages out.
        "expire_at": now + timedelta(days=_ttl_days()),
    }
    try:
        _coll().update_one(key, {"$set": doc, "$setOnInsert": {**key, "created_at": now}},
                           upsert=True)
    except Exception as exc:
        logger.warning("tech_feed_feedback: record failed (%s)", type(exc).__name__)
        raise
    out = dict(key)
    out.update(doc)
    out["created_at"] = now
    return out


def undo(user_email: str, job_id: str) -> bool:
    """Remove one piece of feedback. True if a row was actually deleted.

    Idempotent by construction: deleting nothing is not an error, which is why
    the route can answer 204 unconditionally.
    """
    email = _email(user_email)
    jid = _ident(job_id)
    if not email or not jid:
        return False
    try:
        res = _coll().delete_one({"user_email": email, "job_id": jid})
    except Exception as exc:
        logger.warning("tech_feed_feedback: undo failed (%s)", type(exc).__name__)
        return False
    return bool(getattr(res, "deleted_count", 0))


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def suppressed_for(user_email: str) -> Dict[str, Any]:
    """What to hide from THIS user's feed.

    Returns `{"job_ids": [...], "dedupe_group_ids": [...], "employer_ids": [...],
    "updated_at": datetime|None}`.

    Only `not_interested`, `irrelevant_role` and `duplicate` contribute. A
    `wrong_company`, `stale_job` or `incorrect_metadata` row is stored and
    counted but is invisible here - see the module docstring for why a
    data-quality report must not act as a filter.

    `employer_ids` is therefore ALWAYS empty today, and the key exists only so
    the client's shape is stable: no reason in `REASONS` suppresses at the
    employer level. In particular `wrong_company` does NOT - "you attributed
    this to the wrong company" is a correction to our record, not a request to
    hide an employer, and treating it as one would let a single mis-click erase
    every posting from a company the user may well want.
    """
    empty: Dict[str, Any] = {"job_ids": [], "dedupe_group_ids": [],
                             "employer_ids": [], "updated_at": None}
    email = _email(user_email)
    if not email:
        # Guest / unauthenticated: nothing to look up, and no query to run.
        return empty
    try:
        rows = list(_coll()
                    .find({"user_email": email,
                           # Literal list of literals - no user input reaches
                           # this operator's argument.
                           "reason": {"$in": list(SUPPRESSING_REASONS)}},
                          {"_id": 0, "job_id": 1, "dedupe_group_id": 1,
                           "updated_at": 1})
                    .sort("updated_at", -1)
                    .limit(MAX_SUPPRESSED))
    except Exception as exc:
        # A feedback outage must degrade to "nothing suppressed", never to a
        # failed search: the user would rather see a job twice than see none.
        logger.warning("tech_feed_feedback: suppressed_for failed (%s)",
                       type(exc).__name__)
        return empty

    job_ids: List[str] = []
    seen_jobs: Set[str] = set()
    groups: List[str] = []
    seen_groups: Set[str] = set()
    latest: Optional[datetime] = None
    for row in rows:
        jid = str(row.get("job_id") or "")
        if jid and jid not in seen_jobs:
            seen_jobs.add(jid)
            job_ids.append(jid)
        gid = str(row.get("dedupe_group_id") or "")
        if gid and gid not in seen_groups:
            seen_groups.add(gid)
            groups.append(gid)
        upd = row.get("updated_at")
        if isinstance(upd, datetime):
            if upd.tzinfo is None:
                upd = upd.replace(tzinfo=timezone.utc)
            if latest is None or upd > latest:
                latest = upd
    return {
        "job_ids": job_ids[:MAX_SUPPRESSED],
        "dedupe_group_ids": groups[:MAX_SUPPRESSED],
        "employer_ids": [],
        "updated_at": latest,
    }


def counts(db=None, *, hours: int = 24) -> Dict[str, Any]:
    """Feedback volume for the metrics snapshot. Read-only.

    Split by reason, because the two kinds mean different things operationally:
    a rise in `not_interested` is a RANKING problem, a rise in `wrong_company`
    or `incorrect_metadata` is an INGESTION problem. One number over both would
    hide whichever is moving.
    """
    coll = (db or _db())[COLLECTION]
    cut = _now() - timedelta(hours=max(1, int(hours)))

    def by_reason(match: Optional[Dict[str, Any]] = None) -> Dict[str, int]:
        pipeline: List[Dict[str, Any]] = []
        if match:
            pipeline.append({"$match": match})
        pipeline += [{"$group": {"_id": "$reason", "n": {"$sum": 1}}},
                     {"$sort": {"n": -1}}]
        try:
            return {str(r["_id"]): int(r["n"]) for r in coll.aggregate(pipeline)
                    if r.get("_id")}
        except Exception as exc:  # pragma: no cover - metrics must not raise
            logger.warning("tech_feed_feedback: counts failed (%s)",
                           type(exc).__name__)
            return {}

    recent = by_reason({"updated_at": {"$gte": cut}})
    total = by_reason()
    return {
        "window_hours": int(hours),
        "total": sum(total.values()),
        "total_by_reason": total,
        "last_24h": sum(recent.values()),
        "last_24h_by_reason": recent,
        # The operational split, precomputed so a dashboard cannot re-derive it
        # differently from `SUPPRESSING_REASONS`.
        "suppressing_total": sum(v for k, v in total.items()
                                 if k in SUPPRESSING_REASONS),
        "data_quality_total": sum(v for k, v in total.items()
                                  if k in DATA_QUALITY_REASONS),
    }


def ensure_indexes() -> List[str]:
    """Indexes this module's reads and its uniqueness rule depend on.

    Idempotent; safe to re-run. `scripts/migrate_indexes.py` asserts the same
    specs for a fresh database.
    """
    from pymongo import ASCENDING, DESCENDING

    created: List[str] = []
    coll = _coll()
    specs = [
        # The idempotency guarantee of `record()` in the schema itself.
        ([("user_email", ASCENDING), ("job_id", ASCENDING)],
         {"name": "user_job_unique", "unique": True}),
        # `suppressed_for`: equality on user_email, newest first.
        ([("user_email", ASCENDING), ("updated_at", DESCENDING)],
         {"name": "user_updated_desc"}),
        ([("expire_at", ASCENDING)],
         {"name": "feedback_ttl", "expireAfterSeconds": 0}),
    ]
    for keys, opts in specs:
        try:
            created.append(coll.create_index(keys, **opts))
        except Exception as exc:  # pragma: no cover
            logger.warning("tech_feed_feedback: index %s failed (%s)",
                           opts.get("name"), type(exc).__name__)
    return created
