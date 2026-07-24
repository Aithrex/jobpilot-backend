"""Atomic authenticated tailor quota — Resume Platform v3 (spec 1.6).

Two-part design:

* Counter docs (the atomic authority): collection ``quota_counters``,
  ``_id = "tailor:{pool_key}:{YYYY-MM-DD}"`` where ``pool_key`` preserves the
  existing linked free-pool semantics (``usage_service._linked_pool_emails``:
  subject-linked FREE accounts share one pool; everyone else meters solo under
  their own email). RESERVE is a single guarded ``find_one_and_update`` with
  ``count`` bounded below the limit — no check-then-increment anywhere. A
  missing counter doc is created via a unique-``_id`` insert (races collapse
  onto the guarded update).

* Ledger entries: collection ``quota_ledger`` — one row per reserved slot:
  ``{entry_id, idempotency_key?, user_email, feature: "tailor", date,
     counter_id, state: reserved|completed|refunded, job_id, record_id?,
     reserved_at, settled_at}``.
  ``complete_entry`` settles reserved -> completed (a successful tailoring
  consumed the slot). ``refund_entry`` settles reserved -> refunded and
  decrements the counter — the state transition is the idempotency guard
  (a second refund matches nothing → no double decrement), and the counter
  decrement is floored at 0 via ``count > 0``.

Pool-key note: pool membership can change mid-day (a new /guest/claim links
another free account). ``_pool_counter_ids`` therefore reads the CURRENT pool
counter plus each member's solo counter, so usage recorded before the link
still counts against the shared limit (mirrors the old read-time aggregation).

``feature_usage`` continues to NOT meter tailor (no double count) —
``usage_service.get_usage`` reads these counters.
"""
from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from utils.db_connect import DBConnect

logger = logging.getLogger(__name__)

FEATURE_TAILOR = "tailor"


def _db():
    return DBConnect().get_db()


def _counters():
    return _db().quota_counters


def _ledger():
    return _db().quota_ledger


def _today_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _safe_email(email) -> str:
    """Equality-position guard: emails must be plain strings (never dicts
    that could smuggle a Mongo operator)."""
    return email if isinstance(email, str) else ""


# ---------------------------------------------------------------------------
# Pool / counter key resolution (reuses usage_service pool semantics)
# ---------------------------------------------------------------------------

def pool_key_for(email: str, pool: Optional[List[str]] = None) -> str:
    """The linked free-pool key: solo callers use their email; a multi-member
    free pool uses a deterministic digest of the sorted member list."""
    email = _safe_email(email)
    if pool is None:
        from services.usage_service import _linked_pool_emails
        pool = _linked_pool_emails(email)
    members = sorted({m for m in (pool or []) if isinstance(m, str) and m})
    if len(members) <= 1:
        return email
    digest = hashlib.sha256("|".join(members).encode("utf-8")).hexdigest()[:16]
    return f"pool-{digest}"


def counter_id_for(pool_key: str, date: Optional[str] = None,
                   feature: str = FEATURE_TAILOR) -> str:
    return f"{feature}:{pool_key}:{date or _today_key()}"


def _pool_counter_ids(email: str, date: str) -> Tuple[str, List[str]]:
    """Return (primary_counter_id, residual_counter_ids).

    The primary is the current pool counter; residuals are the members' solo
    counters (usage recorded before a mid-day pool-membership change), which
    still count against the shared limit but are never incremented anew.
    """
    from services.usage_service import _linked_pool_emails
    email = _safe_email(email)
    pool = _linked_pool_emails(email)
    pkey = pool_key_for(email, pool=pool)
    primary = counter_id_for(pkey, date)
    residuals = []
    if len(pool) > 1:
        for member in pool:
            solo = counter_id_for(member, date)
            if solo != primary:
                residuals.append(solo)
    return primary, residuals


def _counts_for(counter_ids: List[str]) -> int:
    if not counter_ids:
        return 0
    total = 0
    for doc in _counters().find({"_id": {"$in": counter_ids}}, {"count": 1}):
        try:
            total += max(0, int(doc.get("count", 0)))
        except (TypeError, ValueError):
            continue
    return total


def used_today(email: str, date: Optional[str] = None) -> int:
    """Pool-aware used count for today — the reader behind
    ``usage_service.get_usage`` (QuotaMeter endpoints keep their shapes)."""
    date = date or _today_key()
    primary, residuals = _pool_counter_ids(email, date)
    return _counts_for([primary] + residuals)


def increment_counter(email: str, n: int = 1) -> None:
    """Unconditional atomic $inc on the caller's current pool counter.

    Used by non-reserving writers (guest-claim day-1 carry-in seeding, admin
    adjustments) — NOT by the tailor routes, which must go through
    ``reserve_tailor``.
    """
    email = _safe_email(email)
    if not email or n <= 0:
        return
    from services.usage_service import get_user_limit
    date = _today_key()
    primary, _ = _pool_counter_ids(email, date)
    _counters().update_one(
        {"_id": primary},
        {
            "$inc": {"count": int(n)},
            "$set": {"updated_at": _now(), "limit_snapshot": get_user_limit(email)},
        },
        upsert=True,
    )


# ---------------------------------------------------------------------------
# Reserve / settle
# ---------------------------------------------------------------------------

def _atomic_reserve(counter_id: str, n: int, effective_limit: int,
                    limit_snapshot: int) -> bool:
    """Guarded $inc: succeeds only while count + n <= effective_limit.

    Missing doc: unique-_id insert with count=n (a lost insert race retries
    the guarded update once). Returns True when the slots were taken.
    """
    counters = _counters()
    guard = {"_id": counter_id, "count": {"$lte": effective_limit - n}}
    update = {
        "$inc": {"count": int(n)},
        "$set": {"updated_at": _now(), "limit_snapshot": int(limit_snapshot)},
    }
    if counters.find_one_and_update(guard, update) is not None:
        return True
    if counters.find_one({"_id": counter_id}, {"_id": 1}) is None:
        if n > effective_limit:
            return False
        try:
            counters.insert_one({
                "_id": counter_id,
                "count": int(n),
                "limit_snapshot": int(limit_snapshot),
                "updated_at": _now(),
            })
            return True
        except Exception:
            # Unique-_id race: another Lambda inserted first — one retry of
            # the guarded update settles it.
            return counters.find_one_and_update(guard, update) is not None
    return False


def reserve_tailor(
    email: str,
    n: int = 1,
    idempotency_key: Optional[str] = None,
) -> Tuple[bool, List[str], Dict]:
    """Atomically reserve ``n`` tailor slots for today (all-or-nothing).

    Returns (allowed, entry_ids, usage). ``usage`` is the standard
    usage_service snapshot — post-reserve on success, pre-attempt on denial
    (so callers can hand it straight to the 429 body, shape unchanged).

    ``idempotency_key`` (unique sparse): a retry with the same key returns the
    existing reservation without incrementing again.
    """
    from services.usage_service import get_usage, get_user_limit

    email = _safe_email(email)
    n = max(1, int(n))

    if idempotency_key:
        existing = list(_ledger().find(
            {"idempotency_key": str(idempotency_key)}, {"entry_id": 1}
        ))
        if existing:
            return True, [e["entry_id"] for e in existing], get_usage(email)

    date = _today_key()
    limit = get_user_limit(email)
    primary, residuals = _pool_counter_ids(email, date)
    residual_used = _counts_for(residuals)
    effective_limit = limit - residual_used

    if effective_limit < n or not _atomic_reserve(primary, n, effective_limit, limit):
        return False, [], get_usage(email)

    now = _now()
    entries = []
    for i in range(n):
        entry = {
            "entry_id": uuid.uuid4().hex,
            "user_email": email,
            "feature": FEATURE_TAILOR,
            "date": date,
            "counter_id": primary,
            "state": "reserved",
            "job_id": None,
            "reserved_at": now,
            "settled_at": None,
        }
        if idempotency_key and i == 0:
            entry["idempotency_key"] = str(idempotency_key)
        entries.append(entry)
    try:
        _ledger().insert_many(entries)
    except Exception as e:
        # Ledger write failed after the counter moved — give the slots back
        # rather than silently over-charging the pool.
        logger.error("quota_ledger insert failed (refunding %d slot(s)): %s", n, e)
        _counters().update_one(
            {"_id": primary, "count": {"$gte": n}}, {"$inc": {"count": -n}}
        )
        if idempotency_key:
            existing = list(_ledger().find(
                {"idempotency_key": str(idempotency_key)}, {"entry_id": 1}
            ))
            if existing:
                return True, [e["entry_id"] for e in existing], get_usage(email)
        return False, [], get_usage(email)

    return True, [e["entry_id"] for e in entries], get_usage(email)


def attach_job(entry_id: str, job_id: str) -> None:
    """Best-effort: link the reservation to its dispatched async job."""
    if not isinstance(entry_id, str) or not entry_id:
        return
    try:
        _ledger().update_one(
            {"entry_id": entry_id}, {"$set": {"job_id": str(job_id)}}
        )
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("quota_ledger attach_job failed for %s: %s", entry_id, e)


def complete_entry(entry_id: str, job_id: Optional[str] = None,
                   record_id: Optional[str] = None) -> bool:
    """Settle reserved -> completed (the slot is consumed). Idempotent."""
    if not isinstance(entry_id, str) or not entry_id:
        return False
    update = {"state": "completed", "settled_at": _now()}
    if job_id:
        update["job_id"] = str(job_id)
    if record_id:
        update["record_id"] = str(record_id)
    res = _ledger().find_one_and_update(
        {"entry_id": entry_id, "state": "reserved"}, {"$set": update}
    )
    return res is not None


def refund_entry(entry_id: str) -> bool:
    """Settle reserved -> refunded and give the slot back.

    Idempotent: the reserved->refunded transition is the guard — a second
    call (or a refund after completion) matches nothing and never
    double-decrements. The counter decrement is floored via ``count > 0``.
    """
    if not isinstance(entry_id, str) or not entry_id:
        return False
    entry = _ledger().find_one_and_update(
        {"entry_id": entry_id, "state": "reserved"},
        {"$set": {"state": "refunded", "settled_at": _now()}},
    )
    if entry is None:
        return False
    counter_id = entry.get("counter_id")
    if counter_id:
        _counters().update_one(
            {"_id": counter_id, "count": {"$gt": 0}},
            {"$inc": {"count": -1}, "$set": {"updated_at": _now()}},
        )
    return True


def refund_entries(entry_ids: List[str]) -> int:
    """Refund a batch of reservations (each idempotently)."""
    return sum(1 for eid in (entry_ids or []) if refund_entry(eid))


def entries_count_for_users(emails: List[str], date: Optional[str] = None) -> Dict[str, int]:
    """Per-actor live entry counts (reserved + completed) for today — feeds
    the admin per-user 'tailored today' column."""
    if not emails:
        return {}
    date = date or _today_key()
    out: Dict[str, int] = {}
    cursor = _ledger().find(
        {
            "user_email": {"$in": [e for e in emails if isinstance(e, str)]},
            "date": date,
            "state": {"$in": ["reserved", "completed"]},
        },
        {"user_email": 1},
    )
    for doc in cursor:
        key = doc.get("user_email") or ""
        out[key] = out.get(key, 0) + 1
    return out
