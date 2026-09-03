"""Saved searches that survive a new device — and a lost server.

The defect this fixes
---------------------
A saved search lived in `localStorage` under
`aspirely.techFeed.savedSearches` and nowhere else. That is not storage, it is
a cache with no origin: clearing site data, a private window, a new laptop or
a second browser all lose every saved search silently, and the user finds out
by looking for something that is gone. `tech_feed_feedback` already learned
this — its module docstring records the same move for suppressions, and
localStorage was demoted there to an optimistic cache.

The contract, and why the client keeps its local copy
------------------------------------------------------
The server is the SOURCE OF TRUTH and the client keeps a local mirror. Not
belt-and-braces: the feed must stay usable when the API is unreachable, and a
saved search the user can still see and still apply offline is worth more than
a correct 503. So the client reads local first, reconciles from the server on
mount, and writes through — the same shape as feedback suppression.

`client_id` is what makes reconciliation possible. The client mints an id
before it has a server, sends it, and the server keys on
`(user_email, client_id)`. A write that arrives twice — an offline queue
draining, a retried request — is one row, not two, and the client never has to
map its ids onto ours.

What is stored
--------------
The FILTER SET and a name. Nothing about results, nothing about the user
beyond the account the row belongs to, and no free text other than the name
the user typed. A saved search is a query, and a query is small.

`filters` is normalised through `tech_feed_service.normalize_request` before
storage, which is what stops this collection becoming an untyped bag: an
unrecognised key is dropped at the boundary rather than stored forever and
handed back to a future reader who trusts it.
"""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

SAVED_SEARCHES = "tech_feed_saved_searches"
SAVED_SEARCH_VERSION = "tech-feed-saved-search-1.0.0"

#: Per account. High enough that nobody reasonable meets it, low enough that a
#: runaway client cannot fill a collection.
MAX_PER_USER = 50
MAX_NAME_CHARS = 80

#: Request fields worth keeping. Everything else `normalize_request` produces
#: is either a paging detail (`cursor`, `page_size`), an identity
#: (`user_email`) or a per-request toggle (`personalize`, `async`) — none of
#: which describes the SEARCH, and all of which would be wrong to replay
#: verbatim months later.
_VOLATILE = frozenset({
    "cursor", "page_size", "user_email", "tz_offset_minutes", "personalize",
    "async", "search_id", "debug",
})


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _db():
    from utils.db_connect import DBConnect
    return DBConnect().get_db()


def ensure_indexes() -> List[str]:
    created: List[str] = []
    try:
        col = _db()[SAVED_SEARCHES]
        created.append(col.create_index(
            [("user_email", 1), ("client_id", 1)], unique=True,
            name="user_client_search_unique"))
        created.append(col.create_index(
            [("user_email", 1), ("updated_at", -1)], name="user_recent_searches"))
    except Exception as exc:      # pragma: no cover
        logger.warning("tech_feed_saved_searches.ensure_indexes: %s", exc)
    return [c for c in created if c]


def _clean_name(value: Any) -> str:
    name = re.sub(r"\s+", " ", str(value or "").strip())[:MAX_NAME_CHARS]
    return name or "Untitled search"


def _clean_client_id(value: Any) -> str:
    """A client id is an OPAQUE key, so it is constrained rather than trusted.

    It reaches a Mongo filter, so the shape is enforced here and nothing that
    is not `[A-Za-z0-9_-]` ever gets there.
    """
    cid = re.sub(r"[^A-Za-z0-9_-]", "", str(value or ""))[:64]
    return cid


def normalize_filters(payload: Any) -> Dict[str, Any]:
    """The stored filter set: `normalize_request` minus the volatile fields.

    Normalising on the WRITE means a saved search cannot carry a key the
    search endpoint does not accept, however the client changes.
    """
    from services import tech_feed_service as tfs
    req = tfs.normalize_request(payload if isinstance(payload, dict) else {})
    return {k: v for k, v in req.items()
            if k not in _VOLATILE and v not in (None, [], "", {})}


def fingerprint(filters: Dict[str, Any]) -> str:
    """Stable hash of a filter set, so a duplicate save is detectable."""
    import json
    blob = json.dumps(filters, sort_keys=True, default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def _public_alerts(row: Dict[str, Any]) -> Dict[str, Any]:
    raw = row.get("alerts") if isinstance(row.get("alerts"), dict) else {}
    return {
        "enabled": bool(raw.get("enabled")),
        "frequency": "daily",
        "timezone": raw.get("timezone") or "UTC",
        "last_evaluated_at": (
            raw["last_evaluated_at"].isoformat()
            if isinstance(raw.get("last_evaluated_at"), datetime)
            else raw.get("last_evaluated_at")),
        "last_delivered_at": (
            raw["last_delivered_at"].isoformat()
            if isinstance(raw.get("last_delivered_at"), datetime)
            else raw.get("last_delivered_at")),
        "paused_reason": raw.get("paused_reason"),
    }


def _public(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": row.get("client_id"),
        "name": row.get("name"),
        "filters": row.get("filters") or {},
        "alerts": _public_alerts(row),
        "createdAt": (row.get("created_at").isoformat()
                      if isinstance(row.get("created_at"), datetime)
                      else row.get("created_at")),
        "updatedAt": (row.get("updated_at").isoformat()
                      if isinstance(row.get("updated_at"), datetime)
                      else row.get("updated_at")),
        "fingerprint": row.get("fingerprint"),
    }


def list_for(user_email: str, *, limit: int = MAX_PER_USER) -> Dict[str, Any]:
    rows = list(_db()[SAVED_SEARCHES]
                .find({"user_email": str(user_email or "")}, {"_id": 0})
                .sort("updated_at", -1).limit(int(limit)))
    return {"searches": [_public(r) for r in rows],
            "saved_search_version": SAVED_SEARCH_VERSION}


def save(user_email: str, *, client_id: Any, name: Any,
         filters: Any, alerts: Any = None) -> Dict[str, Any]:
    """Upsert one saved search. Idempotent on `(user_email, client_id)`."""
    email = str(user_email or "")
    if not email:
        raise ValueError("save: user_email is required")
    cid = _clean_client_id(client_id)
    normalized = normalize_filters(filters)
    if not cid:
        # No usable client id: derive one from the filters, so a retry that
        # lost its id still lands on the same row rather than duplicating.
        cid = "srv_" + fingerprint(normalized)
    col = _db()[SAVED_SEARCHES]
    now = _now()
    existing = col.count_documents({"user_email": email})
    if existing >= MAX_PER_USER and not col.find_one(
            {"user_email": email, "client_id": cid}, {"_id": 1}):
        raise ValueError(f"save: at most {MAX_PER_USER} saved searches per account")
    from services import tech_feed_alerts as alerts_mod
    fields = {"name": _clean_name(name), "filters": normalized,
              "fingerprint": fingerprint(normalized), "updated_at": now,
              "saved_search_version": SAVED_SEARCH_VERSION}
    on_insert = {"user_email": email, "client_id": cid, "created_at": now}
    if alerts is not None:
        fields["alerts"] = alerts_mod.normalize_alerts(alerts)
    else:
        on_insert["alerts"] = alerts_mod.normalize_alerts(None)
    col.update_one(
        {"user_email": email, "client_id": cid},
        {"$set": fields, "$setOnInsert": on_insert},
        upsert=True)
    return list_for(email)


def delete(user_email: str, client_id: Any) -> Dict[str, Any]:
    email = str(user_email or "")
    cid = _clean_client_id(client_id)
    if email and cid:
        _db()[SAVED_SEARCHES].delete_one({"user_email": email, "client_id": cid})
        try:
            from services import tech_feed_alerts as alerts_mod
            _db()[alerts_mod.DELIVERIES].delete_many(
                {"user_email": email, "search_id": cid})
        except Exception:
            pass
    return list_for(email)


def reconcile(user_email: str, local: Any) -> Dict[str, Any]:
    """Merge a client's local list into the server's, then return the server's.

    Called once on mount. The client may hold searches saved before it had an
    account, or while the API was unreachable; those are pushed up. The server
    never deletes on reconcile — a row absent from the client's list is a row
    saved on another device, not a row the user removed. Deletion is explicit,
    and that asymmetry is what makes an offline client safe to reconnect.
    """
    email = str(user_email or "")
    if not email:
        return {"searches": [], "saved_search_version": SAVED_SEARCH_VERSION}
    rows = local if isinstance(local, list) else []
    known = {str(r.get("client_id")) for r in
             _db()[SAVED_SEARCHES].find({"user_email": email}, {"client_id": 1})}
    pushed = 0
    for entry in rows[:MAX_PER_USER]:
        if not isinstance(entry, dict):
            continue
        cid = _clean_client_id(entry.get("id") or entry.get("client_id"))
        if not cid or cid in known:
            continue
        try:
            save(email, client_id=cid, name=entry.get("name"),
                 filters=entry.get("filters"))
            pushed += 1
        except ValueError:
            break      # hit the per-user cap; stop rather than half-succeed
    out = list_for(email)
    out["pushed_from_client"] = pushed
    return out
