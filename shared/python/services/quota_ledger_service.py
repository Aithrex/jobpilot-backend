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


def _safe_feature(feature) -> str:
    """Equality-position guard for the feature key (reaches a counter/ledger
    filter position)."""
    return feature if isinstance(feature, str) and feature else ""


def _idem_value(email: str, feature: str, date: str, key) -> str:
    """The STORED idempotency key: the caller's key NAMESPACED by
    (owner, feature, period).

    Namespacing the VALUE — not just the lookup filter — is what makes the
    UNIQUE index on ``idempotency_key`` a per-owner, per-period guard regardless
    of which index is deployed. Under the legacy GLOBAL unique index a raw key
    reused by a second user (or by the same user in the next period) collided on
    insert, which surfaced as a spurious denial; namespaced, it cannot.
    """
    return f"{_safe_feature(feature)}:{date}:{_safe_email(email)}:{key}"


def _idem_filter(email: str, feature: str, date: str, key) -> Dict:
    """The idempotency lookup NAMESPACE: a caller-supplied key is honoured ONLY
    within the same (owner, feature, period).

    Both scopes are load-bearing, not defensive decoration:

    * OWNER — ``job_postings`` is SHARED inventory, so two users' match sweeps
      derive the same ``normalized_job_identity`` for one posting. An
      owner-less lookup let the second user hit the FIRST user's entry:
      ``reserved: True`` with the second user's counter untouched (the daily cap
      silently stopped binding), and a refund that returned ANOTHER tenant's
      slot. A caller-supplied key (the tailor routes accept one from the client)
      made that reachable on request, too.
    * PERIOD — a key with no period outlives its own quota window, so a
      next-day retry of the same posting re-uses yesterday's entry and charges
      nothing. Scoping by ``date`` is what makes a key expire with its period.

    Every value lands in an EQUALITY position and is string-guarded first, so
    nothing user-supplied can reach an operator position.
    """
    return {
        "idempotency_key": _idem_value(email, feature, date, key),
        "user_email": _safe_email(email),
        "feature": _safe_feature(feature),
        "date": date,
    }


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
    date = _today_key()

    if idempotency_key:
        existing = list(_ledger().find(
            _idem_filter(email, FEATURE_TAILOR, date, idempotency_key),
            {"entry_id": 1}
        ))
        if existing:
            return True, [e["entry_id"] for e in existing], get_usage(email)

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
            entry["idempotency_key"] = _idem_value(
                email, FEATURE_TAILOR, date, idempotency_key)
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
                _idem_filter(email, FEATURE_TAILOR, date, idempotency_key),
                {"entry_id": 1}
            ))
            if existing:
                return True, [e["entry_id"] for e in existing], get_usage(email)
        return False, [], get_usage(email)

    return True, [e["entry_id"] for e in entries], get_usage(email)


# ---------------------------------------------------------------------------
# Feature-parameterized reservation (defect G) — the exactly-once, atomic
# counterpart to entitlements_service.check_and_increment for auto_submit /
# autoapply_answer. Tailor KEEPS its pool-aware path (reserve_tailor) so it is
# never double-counted; every other metered feature meters a solo per-email
# counter whose per-plan cap + usage snapshot come from entitlements_service
# (the single limit authority — never a second copy).
# ---------------------------------------------------------------------------
def _generic_usage(email: str, feature: str) -> Dict:
    """Usage snapshot for a non-tailor feature, read from the ATOMIC quota
    counter (not feature_usage) so reservations are reflected. Mirrors the
    entitlements_service.get_usage shape byte-for-byte so a 429 body is
    unchanged for the client."""
    from services import entitlements_service as ent
    date = ent._period_key(feature)
    counter_id = counter_id_for(email, date, feature)
    used = _counts_for([counter_id])
    limit = ent.quota(email, feature)
    resets = ent._resets_at(feature)
    remaining = None if limit is None else max(0, limit - used)
    return {
        "feature": feature,
        "used": used,
        "limit": limit,
        "remaining": remaining,
        "unlimited": limit is None,
        "period": ent.FEATURE_PERIODS.get(feature, ent.PERIOD_DAY),
        "resets_at": resets.isoformat(),
        "resets_in_seconds": int((resets - _now()).total_seconds()),
    }


def reserve(user_email: str, feature: str, n: int = 1,
            idempotency_key: Optional[str] = None) -> Dict:
    """Atomically reserve ``n`` slots of ``feature`` for the current period.

    Returns ``{reserved, entry_ids, entry_id: first|None, usage, replayed}``.
    ``replayed`` is True when the key matched an EXISTING reservation, i.e. this
    call charged NOTHING — a caller that refunds on a downstream no-op MUST skip
    the refund then, or it hands back a slot the live reservation still owns
    (the concurrent/redelivered-tick refund bug).
    The reservation is a single guarded ``$inc`` bounded below the plan limit —
    no check-then-increment anywhere (the fix for the auto_submit /
    autoapply_answer race). ``usage`` is post-reserve on success, pre-attempt on
    denial (hand straight to the 429 body).

    ``idempotency_key`` (per-feature): a redelivery with the same key returns the
    SAME reservation without incrementing again — so an at-least-once Lambda /
    replayed approval never double-reserves. Tailor delegates to the pool-aware
    ``reserve_tailor`` (unchanged; never double-counted).
    """
    user_email = _safe_email(user_email)
    feature = _safe_feature(feature)
    n = max(1, int(n))

    if feature == FEATURE_TAILOR:
        allowed, entry_ids, usage = reserve_tailor(
            user_email, n=n, idempotency_key=idempotency_key)
        # Tailor keeps its pool-aware path; it does not report replay separately,
        # so callers that need `replayed` (refund-on-no-op) must not meter tailor
        # through here.
        return {"reserved": allowed, "entry_ids": entry_ids,
                "entry_id": entry_ids[0] if entry_ids else None, "usage": usage,
                "replayed": False}

    if not user_email or not feature:
        return {"reserved": False, "entry_ids": [], "entry_id": None, "usage": {},
                "replayed": False}

    from services import entitlements_service as ent

    date = ent._period_key(feature)

    # Idempotent redelivery — the SAME key from the SAME owner for the SAME
    # feature+period → the existing reservation, and nothing is charged.
    if idempotency_key:
        existing = list(_ledger().find(
            _idem_filter(user_email, feature, date, idempotency_key),
            {"entry_id": 1}))
        if existing:
            eids = [e["entry_id"] for e in existing]
            return {"reserved": True, "entry_ids": eids, "entry_id": eids[0],
                    "usage": _generic_usage(user_email, feature),
                    "replayed": True}

    counter_id = counter_id_for(user_email, date, feature)
    limit = ent.quota(user_email, feature)

    if limit is None:
        # Unlimited plan: still ledger-track (so refunds/audit work), no cap guard.
        _counters().update_one(
            {"_id": counter_id},
            {"$inc": {"count": int(n)}, "$set": {"updated_at": _now()}},
            upsert=True)
        reserved = True
    else:
        reserved = _atomic_reserve(counter_id, n, int(limit), int(limit))

    if not reserved:
        return {"reserved": False, "entry_ids": [], "entry_id": None,
                "usage": _generic_usage(user_email, feature), "replayed": False}

    now = _now()
    entries = []
    for i in range(n):
        entry = {
            "entry_id": uuid.uuid4().hex,
            "user_email": user_email,
            "feature": feature,
            "date": date,
            "counter_id": counter_id,
            "state": "reserved",
            "job_id": None,
            "reserved_at": now,
            "settled_at": None,
        }
        if idempotency_key and i == 0:
            entry["idempotency_key"] = _idem_value(
                user_email, feature, date, idempotency_key)
        entries.append(entry)
    try:
        _ledger().insert_many(entries)
    except Exception as e:
        # Ledger write failed after the counter moved — give the slots back
        # rather than silently over-charging.
        logger.error("quota_ledger insert failed for %s (refunding %d slot(s)): %s",
                     feature, n, e)
        _counters().update_one(
            {"_id": counter_id, "count": {"$gte": n}}, {"$inc": {"count": -n}})
        if idempotency_key:
            existing = list(_ledger().find(
                _idem_filter(user_email, feature, date, idempotency_key),
                {"entry_id": 1}))
            if existing:
                eids = [e["entry_id"] for e in existing]
                return {"reserved": True, "entry_ids": eids, "entry_id": eids[0],
                        "usage": _generic_usage(user_email, feature),
                        "replayed": True}
        return {"reserved": False, "entry_ids": [], "entry_id": None,
                "usage": _generic_usage(user_email, feature), "replayed": False}

    eids = [e["entry_id"] for e in entries]
    return {"reserved": True, "entry_ids": eids, "entry_id": eids[0],
            "usage": _generic_usage(user_email, feature), "replayed": False}


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


def refund_entry(entry_id: str, user_email: Optional[str] = None) -> bool:
    """Settle reserved -> refunded and give the slot back.

    Idempotent: the reserved->refunded transition is the guard — a second
    call (or a refund after completion) matches nothing and never
    double-decrements. The counter decrement is floored via ``count > 0``.

    ``user_email`` (when supplied) is an OWNERSHIP guard in the filter: the
    refund REFUSES an entry owned by anyone else, so a caller holding a foreign
    entry_id can never credit a slot back to another tenant's counter. Pass it
    wherever the owner is known — it is the backstop for the cross-tenant
    idempotency-key namespace bug, independent of the key scoping itself.
    """
    if not isinstance(entry_id, str) or not entry_id:
        return False
    query = {"entry_id": entry_id, "state": "reserved"}
    if user_email is not None:
        owner = _safe_email(user_email)
        if not owner:
            logger.warning("quota_ledger refund refused: invalid owner scope")
            return False
        query["user_email"] = owner
    entry = _ledger().find_one_and_update(
        query,
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


def refund_entries(entry_ids: List[str], user_email: Optional[str] = None) -> int:
    """Refund a batch of reservations (each idempotently).

    ``user_email`` scopes every refund to its owner (see ``refund_entry``): an
    entry belonging to a different tenant is REFUSED, not silently credited."""
    return sum(1 for eid in (entry_ids or [])
               if refund_entry(eid, user_email=user_email))


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
