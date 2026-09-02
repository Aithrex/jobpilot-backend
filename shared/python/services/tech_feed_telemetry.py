"""Product telemetry for the tech feed, with no PII in it by construction.

What "non-PII" means here, precisely
------------------------------------
No email, no name, no IP, no free text, and no posting-level identifier that
can be joined back to a person. The account is represented by a
**pseudonym** — `sha256(TELEMETRY_SALT + email)`, truncated — so "how many
distinct accounts saw a zero-result search" is answerable and "which account"
is not, from this collection, by anyone including us. The salt is server-side
config; rotating it breaks the link permanently and deliberately.

That is a design choice, not a compliance box. A telemetry collection is the
one that gets exported to a spreadsheet, and the safest way for it not to leak
identity is not to hold it.

What is recorded, and why each one
-----------------------------------
    search              a search happened, with WHICH FILTER KEYS were set —
                        never their values. "somebody filtered by company" is
                        a product fact; "somebody filtered by Stripe" is that
                        person's job hunt.
    zero_result         a search returned nothing, with the filter keys and
                        the drop reason the server itself computed. This is
                        the single most actionable event in the product.
    filter_applied      one filter turned on or off. Keys only.
    card_impression     a card was rendered. Counted per search, not per row,
                        so a scroll does not become a hundred writes.
    card_open           a card was expanded.
    card_apply_click    the apply link was followed. The closest thing the
                        feed has to an outcome.
    card_save           saved, or fed back as not-interested.
    source_failure      a source the user was told about failed. Recorded
                        because a user-visible failure and an internal crawl
                        failure are different events and only the second one
                        was ever counted.

Everything is a COUNT of an event with a small, closed set of dimensions.
Nothing here reconstructs a session, and nothing joins to `job_postings` by
anything finer than the ATS vendor.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

TELEMETRY_VERSION = "tech-feed-telemetry-1.0.0"
EVENTS = "tech_feed_events"
RETENTION_DAYS = 90

EV_SEARCH = "search"
EV_ZERO_RESULT = "zero_result"
EV_FILTER_APPLIED = "filter_applied"
EV_CARD_IMPRESSION = "card_impression"
EV_CARD_OPEN = "card_open"
EV_CARD_APPLY_CLICK = "card_apply_click"
EV_CARD_SAVE = "card_save"
EV_SOURCE_FAILURE = "source_failure"

#: A CLOSED set. An unrecognised event name is dropped at the boundary rather
#: than stored, so this collection cannot grow a schema by accident.
EVENT_TYPES = frozenset({
    EV_SEARCH, EV_ZERO_RESULT, EV_FILTER_APPLIED, EV_CARD_IMPRESSION,
    EV_CARD_OPEN, EV_CARD_APPLY_CLICK, EV_CARD_SAVE, EV_SOURCE_FAILURE,
})

#: Dimensions a client may set, and the shape each one must have. Anything
#: else on the event is discarded. This is the whole PII defence: a client
#: cannot put a name in a field that does not exist.
_DIM_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
ALLOWED_DIMENSIONS = frozenset({
    "filter_keys",       # list of filter NAMES, never values
    "result_bucket",     # "0" | "1-9" | "10-49" | "50+"
    "sort",              # relevance | newest | salary
    "tech_scope",        # core | adjacent | ...
    "employer_scope",    # any | tech_only
    "ats",               # vendor key, for source_failure and card events
    "source",            # feed source key
    "reason",            # a closed reason code the SERVER defines
    "surface",           # which UI surface raised it
    "page",              # page index, integer
})

MAX_EVENTS_PER_BATCH = 50
MAX_DIMENSION_VALUES = 12

#: Buckets rather than raw counts: a result count of 1 on a narrow filter set
#: is closer to an identifier than a metric.
_RESULT_BUCKETS = ((0, 0, "0"), (1, 9, "1-9"), (10, 49, "10-49"))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _db():
    from utils.db_connect import DBConnect
    return DBConnect().get_db()


def result_bucket(total: Any) -> str:
    try:
        n = int(total or 0)
    except (TypeError, ValueError):
        n = 0
    for lo, hi, label in _RESULT_BUCKETS:
        if lo <= n <= hi:
            return label
    return "50+"


def pseudonym(user_email: Any) -> str:
    """A stable, salted, one-way account handle. Never the email.

    Salted so the mapping cannot be rebuilt by hashing a list of addresses,
    which an unsalted digest of an email address makes trivial. The salt lives
    in config beside every other secret; a deployment that has not set one
    gets a per-process random value, which degrades the metric to
    "within this process" rather than degrading the privacy.
    """
    email = str(user_email or "").strip().lower()
    if not email:
        return ""
    salt = _salt()
    return hashlib.sha256((salt + "|" + email).encode("utf-8")).hexdigest()[:20]


_PROCESS_SALT: Optional[str] = None


def _salt() -> str:
    global _PROCESS_SALT
    configured = os.getenv("TELEMETRY_SALT") or ""
    if configured:
        return configured
    try:
        from utils.config import Config
        configured = str(getattr(Config, "TELEMETRY_SALT", "") or "")
    except Exception:      # pragma: no cover - config is optional here
        configured = ""
    if configured:
        return configured
    if _PROCESS_SALT is None:
        # Fail SAFE, not open: an unconfigured deployment loses cross-process
        # aggregation, which is a metrics problem. Falling back to an unsalted
        # hash would lose the privacy property, which is not.
        _PROCESS_SALT = hashlib.sha256(os.urandom(32)).hexdigest()
        logger.warning("tech_feed_telemetry: TELEMETRY_SALT is not configured; "
                       "account pseudonyms are per-process only")
    return _PROCESS_SALT


def _clean_dimensions(raw: Any) -> Dict[str, Any]:
    """Keep the allowed keys, coerce the values, drop everything else."""
    out: Dict[str, Any] = {}
    if not isinstance(raw, dict):
        return out
    for key, value in raw.items():
        k = str(key or "").strip().lower()
        if k not in ALLOWED_DIMENSIONS or not _DIM_KEY_RE.match(k):
            continue
        if isinstance(value, (list, tuple, set)):
            vals = [re.sub(r"[^a-z0-9_\-]", "", str(v).strip().lower())[:40]
                    for v in list(value)[:MAX_DIMENSION_VALUES]]
            out[k] = sorted({v for v in vals if v})
        elif isinstance(value, bool):
            out[k] = bool(value)
        elif isinstance(value, (int, float)):
            out[k] = int(value)
        else:
            # Alphanumerics, underscore and hyphen. Nothing else: every
            # allowed dimension is a slug (`icims`, `tech_only`, `relevance`),
            # and widening the class to admit one convenient character is how
            # a free-text field gets in.
            cleaned = re.sub(r"[^A-Za-z0-9_\-]", "", str(value).strip())[:40]
            if cleaned:
                out[k] = cleaned
    return out


def ensure_indexes() -> List[str]:
    created: List[str] = []
    try:
        col = _db()[EVENTS]
        created.append(col.create_index([("at", -1)], name="event_recent"))
        created.append(col.create_index([("event", 1), ("at", -1)],
                                        name="event_type_recent"))
        created.append(col.create_index(
            "expires_at", expireAfterSeconds=0, name="event_ttl"))
    except Exception as exc:      # pragma: no cover
        logger.warning("tech_feed_telemetry.ensure_indexes: %s", exc)
    return [c for c in created if c]


def record(events: Iterable[Dict[str, Any]], *, user_email: Any = "",
           db=None) -> Dict[str, Any]:
    """Write a batch. Returns `{accepted, rejected}`; never raises.

    Telemetry must not be able to fail a user action, so a bad batch is
    counted and dropped rather than surfaced. `rejected` is returned so a
    client that is sending nonsense can be told, once.
    """
    db = db if db is not None else _db()
    subject = pseudonym(user_email)
    now = _now()
    expires = now + timedelta(days=RETENTION_DAYS)
    docs: List[Dict[str, Any]] = []
    rejected = 0
    for raw in list(events or [])[:MAX_EVENTS_PER_BATCH]:
        if not isinstance(raw, dict):
            rejected += 1
            continue
        name = str(raw.get("event") or "").strip().lower()
        if name not in EVENT_TYPES:
            rejected += 1
            continue
        try:
            count = max(1, min(500, int(raw.get("count") or 1)))
        except (TypeError, ValueError):
            count = 1
        docs.append({
            "event": name,
            "subject": subject,
            "count": count,
            "dimensions": _clean_dimensions(raw.get("dimensions")),
            "at": now,
            "expires_at": expires,
            "telemetry_version": TELEMETRY_VERSION,
        })
    if docs:
        try:
            db[EVENTS].insert_many(docs, ordered=False)
        except Exception as exc:      # pragma: no cover
            logger.warning("tech_feed_telemetry.record: %s", type(exc).__name__)
            return {"accepted": 0, "rejected": rejected + len(docs)}
    return {"accepted": len(docs), "rejected": rejected}


def summary(*, hours: int = 24, db=None) -> Dict[str, Any]:
    """Counts by event, plus the filter keys actually used and the top
    zero-result filter combinations. All aggregate, all non-identifying."""
    db = db if db is not None else _db()
    since = _now() - timedelta(hours=max(1, int(hours)))
    out: Dict[str, Any] = {"window_hours": hours,
                           "telemetry_version": TELEMETRY_VERSION}
    try:
        by_event: Dict[str, Dict[str, int]] = {}
        for row in db[EVENTS].aggregate([
                {"$match": {"at": {"$gte": since}}},
                {"$group": {"_id": "$event", "events": {"$sum": "$count"},
                            "subjects": {"$addToSet": "$subject"}}}]):
            by_event[str(row["_id"])] = {
                "events": int(row["events"]),
                "accounts": len([s for s in (row.get("subjects") or []) if s]),
            }
        out["by_event"] = by_event

        filters_used: Dict[str, int] = {}
        for row in db[EVENTS].aggregate([
                {"$match": {"at": {"$gte": since},
                            "event": {"$in": [EV_SEARCH, EV_FILTER_APPLIED]}}},
                {"$unwind": "$dimensions.filter_keys"},
                {"$group": {"_id": "$dimensions.filter_keys",
                            "n": {"$sum": "$count"}}},
                {"$sort": {"n": -1}}, {"$limit": 40}]):
            filters_used[str(row["_id"])] = int(row["n"])
        out["filter_use"] = filters_used

        zero: Dict[str, int] = {}
        for row in db[EVENTS].aggregate([
                {"$match": {"at": {"$gte": since}, "event": EV_ZERO_RESULT}},
                {"$group": {"_id": "$dimensions.reason", "n": {"$sum": "$count"}}},
                {"$sort": {"n": -1}}, {"$limit": 20}]):
            zero[str(row["_id"] or "unattributed")] = int(row["n"])
        out["zero_result_reasons"] = zero

        failures: Dict[str, int] = {}
        for row in db[EVENTS].aggregate([
                {"$match": {"at": {"$gte": since}, "event": EV_SOURCE_FAILURE}},
                {"$group": {"_id": "$dimensions.ats", "n": {"$sum": "$count"}}},
                {"$sort": {"n": -1}}, {"$limit": 30}]):
            failures[str(row["_id"] or "unknown")] = int(row["n"])
        out["source_failures_by_vendor"] = failures

        # Engagement, as a funnel rather than four unrelated counts.
        impressions = by_event.get(EV_CARD_IMPRESSION, {}).get("events", 0)
        for stage in (EV_CARD_OPEN, EV_CARD_APPLY_CLICK, EV_CARD_SAVE):
            n = by_event.get(stage, {}).get("events", 0)
            out[f"{stage}_rate"] = round(n / impressions, 4) if impressions else None
    except Exception as exc:      # pragma: no cover
        logger.warning("tech_feed_telemetry.summary: %s", type(exc).__name__)
        out["error"] = type(exc).__name__
    return out
