"""Browser Radar, the fleet half (`radar-2`): leases, the brake and coverage.

`radar-1` gives every contributor the same largest stale boards, so N
browsers read the same boards N times. This module shares the work out:

* **One row per registry board** in `browser_radar_board_leases`. The row is
  both "who last read this board" and the lease on it. `sync_boards` builds
  the rows from `registry_members("workday")` and never deletes one.
* **Claims.** `claim_boards` hands a browser a few boards that are due. Each
  board is taken with ONE guarded `find_one_and_update` (the crawler's
  `acquire_lease` idiom), so two callers can never hold the same board. A
  lease lasts 60 minutes. Expiry lives in the claim filter, so a browser that
  went away costs the board an hour and needs no reaper.
* **A read completes a lease.** `record_reads` runs after a written batch.
  A known board read with no search term is a read of that board, whoever
  held the lease. A title search is not: it proves nothing about the board.
* **The brake.** Workday walls an address, not an account. One browser's
  challenge is that browser's problem. Three accounts challenged on one board
  park the board. Three accounts challenged inside an hour pause every claim
  for a day. The state is in Mongo because the gateway runs several workers.
* **Coverage.** `run_coverage` (the `radar_coverage` job, every 30 minutes)
  syncs the rows and stores one snapshot in
  `browser_radar_coverage_snapshots`. `coverage_block` serves the newest one
  with its age. No snapshot means unknown, never zero.

Accounts appear here only as a salted one-way pseudonym (`account_pseudonym`).
No email is written to a board row, a fleet counter or a snapshot.

The contract is in `docs/specs/browser-radar.md`, section `radar-2`. Dropping
the three collections here removes this state and nothing else.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import math
import os
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from services import browser_radar_service as radar
from services.browser_radar_service import RadarError

logger = logging.getLogger(__name__)

CLAIMS_VERSION = "radar-2"

LEASES = radar.LEASES
COVERAGE = radar.COVERAGE
FLEET = radar.FLEET

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

CLAIM_DEFAULT = 6
CLAIM_MAX = 12
LEASE_MINUTES = 60
#: Boards one account may claim in a UTC day (`BROWSER_RADAR_CLAIMS_PER_DAY`).
CLAIMS_PER_DAY = 60

#: Every board is due again this long after a read, divided by its boost
#: (`BROWSER_RADAR_TARGET_HOURS`, clamped to 3..72).
TARGET_HOURS = 24.0
TARGET_MIN_HOURS = 3.0
TARGET_MAX_HOURS = 72.0
#: No board is read more often than this, whatever its boost.
MIN_INTERVAL_HOURS = 3.0
#: A large board comes due sooner, by at most this factor. Bounded, so the
#: smallest board is still due one target interval after its read.
BOOST_MAX = 4.0

PARK_GONE_DAYS = 7
PARK_ROBOTS_HOURS = 24
PARK_CHALLENGE_HOURS = 24
#: Different accounts challenged on one board inside 24 hours that park it.
BOARD_CHALLENGE_ACCOUNTS = 3
BOARD_CHALLENGE_WINDOW_HOURS = 24
BOARD_CHALLENGE_KEEP = 20

#: Different accounts challenged inside an hour that pause every claim
#: (`BROWSER_RADAR_BRAKE_ACCOUNTS`), and the share of that hour's claiming
#: accounts it must also reach once that share is the larger number.
BRAKE_ACCOUNTS = 3
BRAKE_SHARE = 0.01
BRAKE_HOURS = 24

COVERAGE_WINDOW_HOURS = 24
COVERAGE_STALE_HOURS = 2.0
COVERAGE_KEEP_DAYS = 14

MAX_READS = 40
READ_RESULTS = ("gone", "robots")
RELEASE_REASONS = ("challenge", "budget", "deferred", "robots", "gone")

#: A sync that would retire more than this share of the live rows (and more
#: than `SYNC_SHRINK_FLOOR` rows) is read as a failed registry read: the
#: database half of the registry answers [] when it cannot be reached. It is
#: believed only once it has said the same thing for this many hours.
SYNC_MAX_SHRINK = 0.2
SYNC_SHRINK_FLOOR = 5
SYNC_SHRINK_ACCEPT_HOURS = 24

_RANK_SPAN = 1_000_000
_LEASE_ID_RE = re.compile(r"^[a-f0-9]{32}$")
_BRAKE_ID = "brake"
_SYNC_ID = "sync"


# ---------------------------------------------------------------------------
# Settings and identity
# ---------------------------------------------------------------------------

def _env_number(name: str, default: float) -> float:
    raw = str(os.environ.get(name, "") or "").strip()
    if not raw:
        return float(default)
    try:
        value = float(raw)
    except ValueError:
        return float(default)
    return value if math.isfinite(value) else float(default)


def claims_per_day() -> int:
    return max(0, min(1000, int(_env_number("BROWSER_RADAR_CLAIMS_PER_DAY", CLAIMS_PER_DAY))))


def target_hours() -> float:
    value = _env_number("BROWSER_RADAR_TARGET_HOURS", TARGET_HOURS)
    return max(TARGET_MIN_HOURS, min(TARGET_MAX_HOURS, value))


def brake_accounts() -> int:
    return max(1, min(1000, int(_env_number("BROWSER_RADAR_BRAKE_ACCOUNTS", BRAKE_ACCOUNTS))))


def account_pseudonym(email: Any) -> str:
    """A salted one-way handle for an account. Never the email.

    It must be the SAME in every gateway worker: a lease is released by the
    worker that gets the call, and "three different accounts" is counted
    across workers. `tech_feed_telemetry.pseudonym` is that when
    `TELEMETRY_SALT` is set. When it is not, that helper salts per process,
    so this keys an HMAC on the JWT secret instead (set in every deployment,
    shared by every worker, and never recoverable from the digest)."""
    key = str(email or "").strip().lower()
    if not key:
        return ""
    from services import tech_feed_telemetry as telemetry

    configured = os.getenv("TELEMETRY_SALT") or ""
    secret = ""
    try:
        from utils.config import Config
        configured = configured or str(getattr(Config, "TELEMETRY_SALT", "") or "")
        if not configured:
            secret = str(getattr(Config, "JWT_SECRET_KEY", "") or "")
    except Exception:  # pragma: no cover - config is optional here
        secret = ""
    if configured or not secret:
        return telemetry.pseudonym(key)
    return hmac.new(secret.encode("utf-8"), ("radar-account|" + key).encode("utf-8"),
                    hashlib.sha256).hexdigest()[:20]


def board_key(domain: Any, wd_host: Any, tenant: Any, site: Any) -> str:
    """`domain|wd_host|tenant|site`, lowercased: the address ingest matches on."""
    return "|".join(str(part or "").strip().lower() for part in (domain, wd_host, tenant, site))


def claims_live(email: Any) -> bool:
    """Whether `claim_boards` can hand this account boards at all. A paused
    fleet is still live: the pause is temporary and the answer says so."""
    return bool(radar.enabled() and radar.ingest_mode() == radar.MODE_ON
                and radar.is_contributor(email))


def _hours(delta: timedelta) -> float:
    return delta.total_seconds() / 3600.0


# ---------------------------------------------------------------------------
# The registry, as ledger rows
# ---------------------------------------------------------------------------

def _registry_boards() -> Tuple[Dict[str, Dict[str, Any]], Dict[str, int]]:
    """`(rows by board key, source counts)` for every Workday member.

    The first member wins an address, as in `_index_members`."""
    w = radar._worker()
    boards: Dict[str, Dict[str, Any]] = {}
    counts = {"members": 0, "catalog": 0, "endpoints": 0}
    for member in radar._members():
        try:
            board = radar._board_of(member)
            stats_tenant = w._member_tenant(member).lower()
        except Exception:  # pragma: no cover - a malformed registry row
            continue
        counts["members"] += 1
        # A member read from `company_ats_endpoints` (or a seed) carries
        # `extra`; a row of the in-repo catalogs does not.
        counts["endpoints" if "extra" in member else "catalog"] += 1
        key = board_key(board["domain"], board["wd_host"], board["tenant"], board["site"])
        boards.setdefault(key, {
            "tenant": board["tenant"], "site": board["site"],
            "wd_host": board["wd_host"], "domain": board["domain"],
            "employer": board["employer"], "stats_tenant": stats_tenant,
        })
    counts["boards"] = len(boards)
    return boards, counts


def _boost(active_postings: int) -> float:
    """1.0 for a small board, up to `BOOST_MAX` for a very large one."""
    n = max(0, int(active_postings or 0))
    return round(max(1.0, min(BOOST_MAX, 1.0 + 0.5 * math.log2(1.0 + n / 20.0))), 2)


def _rank(active_postings: int, seen: Optional[datetime], now: datetime) -> int:
    """The tie-break among boards equally due (lower goes first): tenants the
    crawler has not seen for `STALE_HOURS` before the rest, larger first."""
    n = max(0, min(_RANK_SPAN - 1, int(active_postings or 0)))
    stale = seen is None or seen < now - timedelta(hours=radar.STALE_HOURS)
    return (0 if stale else _RANK_SPAN) + (_RANK_SPAN - 1 - n)


def _new_row_defaults(now: datetime) -> Dict[str, Any]:
    return {"last_read_at": None, "last_read_by": None, "last_result": None,
            "last_postings": None, "due_at": EPOCH, "lease": None,
            "challenges": 0, "parked_until": None, "created_at": now}


def _priority_defaults() -> Dict[str, Any]:
    return {"active_postings": 0, "boost": 1.0, "rank": _RANK_SPAN - 1}


_SYNC_FIELDS = ("tenant", "site", "wd_host", "domain", "employer", "stats_tenant",
                "in_registry", "active_postings", "boost", "rank")


def sync_boards(*, now: Optional[datetime] = None,
                stats: Optional[List[Tuple[str, int, Optional[datetime]]]] = None,
                deadline: Optional[float] = None) -> Dict[str, Any]:
    """Bring the ledger in line with `registry_members("workday")`.

    Upserts a row for every board, restores a row whose board came back, and
    marks a row whose board left (`in_registry: false`) so it is not handed
    out. It never deletes. A registry that cannot be read, is empty, or has
    suddenly lost a large share of its boards changes NOTHING: a failed read
    must not shrink the board set.

    `stats` is `_tenant_stats()`'s rows; with it each row gets its size boost
    and its tie-break rank. Without it those are left as they are."""
    now = now or radar._utcnow()
    boards, registry = _registry_boards()
    out: Dict[str, Any] = {"ok": False, "reason": "", "registry": registry,
                           "added": 0, "updated": 0, "restored": 0, "retired": 0}
    if not boards:
        out["reason"] = "registry_empty"
        return out
    try:
        db = radar._db()
        radar._ensure_indexes(db)
        existing = {doc["_id"]: doc for doc in db[LEASES].find(
            {}, {field: 1 for field in _SYNC_FIELDS})}
        note = db[FLEET].find_one({"_id": _SYNC_ID}) or {}
    except Exception as exc:
        logger.warning("radar: ledger read failed (%s)", type(exc).__name__)
        out["reason"] = "ledger_unreadable"
        return out

    live = {key for key, doc in existing.items() if doc.get("in_registry")}
    leaving = sorted(live - set(boards))
    if leaving and len(leaving) > max(SYNC_SHRINK_FLOOR, SYNC_MAX_SHRINK * len(live)):
        since = radar._aware(note.get("shrink_since"))
        if since is None:
            db[FLEET].update_one({"_id": _SYNC_ID}, {"$set": {
                "shrink_since": now, "shrink_rows": len(leaving)}}, upsert=True)
            since = now
        if _hours(now - since) < SYNC_SHRINK_ACCEPT_HOURS:
            logger.warning("radar: registry lost %d of %d boards; ledger left as it is",
                           len(leaving), len(live))
            out["reason"] = "registry_shrank"
            return out
    if note.get("shrink_since") is not None:
        db[FLEET].update_one({"_id": _SYNC_ID}, {"$unset": {"shrink_since": "",
                                                           "shrink_rows": ""}})

    by_tenant = {t: (n, seen) for t, n, seen in (stats or [])}
    for key, board in boards.items():
        if deadline is not None and time.monotonic() > deadline:
            out["reason"] = "budget"
            return out
        want: Dict[str, Any] = dict(board, in_registry=True)
        if stats is not None:
            n, seen = by_tenant.get(board["stats_tenant"], (0, None))
            want.update(active_postings=int(n), boost=_boost(n), rank=_rank(n, seen, now))
        have = existing.get(key)
        if have is None:
            on_insert = _new_row_defaults(now)
            if stats is None:
                on_insert.update(_priority_defaults())
            db[LEASES].update_one({"_id": key}, {"$set": want, "$setOnInsert": on_insert},
                                  upsert=True)
            out["added"] += 1
            continue
        changed = {f: v for f, v in want.items() if have.get(f) != v}
        if not changed:
            continue
        update: Dict[str, Any] = {"$set": changed}
        if "in_registry" in changed:
            update["$unset"] = {"left_registry_at": ""}
            out["restored"] += 1
        else:
            out["updated"] += 1
        db[LEASES].update_one({"_id": key}, update)

    for start in range(0, len(leaving), 500):
        res = db[LEASES].update_many(
            {"_id": {"$in": leaving[start:start + 500]}, "in_registry": True},
            {"$set": {"in_registry": False, "left_registry_at": now, "lease": None}})
        out["retired"] += int(res.modified_count or 0)
    out["ok"] = True
    return out


# ---------------------------------------------------------------------------
# The brake (fleet state, in Mongo)
# ---------------------------------------------------------------------------

def _hour_id(at: datetime) -> str:
    return "h|" + at.astimezone(timezone.utc).strftime("%Y%m%d%H")


def _bump_hour(db, now: datetime, *, claims: int = 0, claimer: str = "",
               challenger: str = "") -> None:
    """One small counter document per clock hour: how many boards were
    claimed, and which accounts (pseudonyms) claimed or were challenged."""
    update: Dict[str, Any] = {"$setOnInsert": {
        "expire_at": now.replace(minute=0, second=0, microsecond=0) + timedelta(days=3)}}
    newest: Dict[str, Any] = {}
    if claimer:
        newest[f"claimers.{claimer}"] = now
    if challenger:
        newest[f"challengers.{challenger}"] = now
    if newest:
        update["$max"] = newest
    if claims:
        update["$inc"] = {"claims": int(claims)}
    db[FLEET].update_one({"_id": _hour_id(now)}, update, upsert=True)


def _recent_accounts(db, now: datetime, hours: int, field: str) -> Dict[str, datetime]:
    """Pseudonym -> newest time, for `field` entries inside the last `hours`."""
    ids = [_hour_id(now - timedelta(hours=back)) for back in range(hours + 1)]
    cutoff = now - timedelta(hours=hours)
    out: Dict[str, datetime] = {}
    for doc in db[FLEET].find({"_id": {"$in": ids}}, {field: 1}):
        for who, at in (doc.get(field) or {}).items():
            at = radar._aware(at)
            if at is not None and at > cutoff and (who not in out or at > out[who]):
                out[who] = at
    return out


def fleet_state(db=None, *, now: Optional[datetime] = None) -> Dict[str, Any]:
    """`{paused, paused_until}`. The pause ends by itself when its time passes."""
    now = now or radar._utcnow()
    db = db if db is not None else radar._db()
    doc = db[FLEET].find_one({"_id": _BRAKE_ID}) or {}
    until = radar._aware(doc.get("paused_until"))
    paused = bool(until and until > now)
    return {"paused": paused, "paused_until": radar._iso(until) if paused else None}


def _check_brake(db, now: datetime) -> bool:
    """Pause every claim when enough different accounts were challenged in
    the last hour. Returns whether the fleet is paused after this check."""
    challenged = len(_recent_accounts(db, now, 1, "challengers"))
    claimed = len(_recent_accounts(db, now, 1, "claimers"))
    threshold = max(brake_accounts(), int(math.ceil(round(BRAKE_SHARE * claimed, 6))))
    if challenged < threshold:
        return fleet_state(db, now=now)["paused"]
    db[FLEET].update_one(
        {"_id": _BRAKE_ID},
        {"$max": {"paused_until": now + timedelta(hours=BRAKE_HOURS)},
         "$set": {"tripped_at": now, "challenged_accounts": challenged,
                  "claiming_accounts": claimed, "threshold": threshold}},
        upsert=True)
    logger.warning("radar: fleet brake on, %d accounts challenged in an hour "
                   "(threshold %d, %d claiming)", challenged, threshold, claimed)
    return True


# ---------------------------------------------------------------------------
# Claims
# ---------------------------------------------------------------------------

def _claim_filter(now: datetime) -> Dict[str, Any]:
    """Due, in the registry, not leased, not parked. The whole guard is in
    the filter of the one write that takes the board, so a board another
    caller took a moment ago no longer matches.

    `{"lease": None}` and `{"parked_until": None}` match a null and a missing
    field alike."""
    return {
        "in_registry": True,
        "due_at": {"$lte": now},
        "$and": [
            {"$or": [{"lease": None}, {"lease.until": {"$lte": now}}]},
            {"$or": [{"parked_until": None}, {"parked_until": {"$lte": now}}]},
        ],
    }


#: Oldest due first; `rank` orders boards equally due (never read: the epoch).
CLAIM_SORT = [("due_at", 1), ("rank", 1)]


def _reserve_claims(db, email: str, want: int, now: datetime) -> int:
    """Take up to `want` of today's claim allowance, atomically.

    Claim first, then hand back what went over, so concurrent calls cannot
    each read "under the limit" and all pass (the `_rate_limit` idiom)."""
    from pymongo import ReturnDocument

    cap = claims_per_day()
    day_end = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    key = {"_id": f"{email}|c|{now.strftime('%Y%m%d')}"}
    doc = db[radar.USAGE].find_one_and_update(
        key, {"$inc": {"claims": int(want)},
              "$setOnInsert": {"expire_at": day_end + timedelta(days=2)}},
        upsert=True, return_document=ReturnDocument.AFTER)
    over = int((doc or {}).get("claims") or 0) - cap
    allowed = want if over <= 0 else max(0, want - over)
    if allowed < want:
        db[radar.USAGE].update_one(key, {"$inc": {"claims": allowed - want}})
    return allowed


def _task_of(doc: Dict[str, Any]) -> Dict[str, Any]:
    return {"tenant": doc.get("tenant"), "wd_host": doc.get("wd_host"),
            "site": doc.get("site"), "domain": doc.get("domain"),
            "employer": doc.get("employer") or doc.get("tenant"),
            "search_text": "", "max_pages": 1, "why": "shared",
            "lease_id": (doc.get("lease") or {}).get("id")}


def _clean_limit(limit: Any) -> int:
    if isinstance(limit, bool) or not isinstance(limit, (int, float)):
        return CLAIM_DEFAULT
    if isinstance(limit, float) and not math.isfinite(limit):
        return CLAIM_DEFAULT
    return max(1, min(CLAIM_MAX, int(limit)))


def claim_boards(email: str, limit: Any = CLAIM_DEFAULT, *,
                 now: Optional[datetime] = None) -> Dict[str, Any]:
    """Lease up to `limit` due boards to this account (`radar-2`).

    Never raises. No tasks comes with a `reason`: `disabled`, `ingest_off`,
    `ingest_dry_run`, `not_contributor`, `paused`, `daily_limit`, `none_due`
    or `unavailable`."""
    from pymongo import ReturnDocument

    now = now or radar._utcnow()
    want = _clean_limit(limit)
    out: Dict[str, Any] = {"plan_version": CLAIMS_VERSION, "tasks": [],
                           "lease_minutes": LEASE_MINUTES, "paused": False, "reason": ""}
    if not radar.enabled():
        out["reason"] = "disabled"
        return out
    mode = radar.ingest_mode()
    if mode != radar.MODE_ON:
        # A lease is completed by a WRITTEN batch. Off refuses batches and a
        # dry run writes none, so a board handed out then could never be read.
        out["reason"] = "ingest_off" if mode == radar.MODE_OFF else "ingest_dry_run"
        return out
    if not radar.is_contributor(email):
        out["reason"] = "not_contributor"
        return out
    who = account_pseudonym(email)
    reserved = got = 0
    db = None
    try:
        db = radar._db()
        radar._ensure_indexes(db)
        if fleet_state(db, now=now)["paused"]:
            out["paused"] = True
            out["reason"] = "paused"
            return out
        reserved = _reserve_claims(db, email, want, now)
        if reserved <= 0:
            out["reason"] = "daily_limit"
            return out
        until = now + timedelta(minutes=LEASE_MINUTES)
        for _ in range(reserved):
            doc = db[LEASES].find_one_and_update(
                _claim_filter(now),
                {"$set": {"lease": {"id": uuid.uuid4().hex, "by": who,
                                    "until": until, "at": now}},
                 "$inc": {"leases_issued": 1}},
                sort=CLAIM_SORT, return_document=ReturnDocument.AFTER)
            if doc is None:
                break
            got += 1
            out["tasks"].append(_task_of(doc))
        if got:
            _bump_hour(db, now, claims=got, claimer=who)
        else:
            out["reason"] = "none_due"
    except Exception as exc:
        logger.warning("radar: claim failed (%s)", type(exc).__name__)
        if not out["tasks"]:
            out["reason"] = "unavailable"
    finally:
        if db is not None and reserved > got:
            try:
                key = {"_id": f"{email}|c|{now.strftime('%Y%m%d')}"}
                db[radar.USAGE].update_one(key, {"$inc": {"claims": got - reserved}})
            except Exception as exc:  # pragma: no cover - costs allowance only
                logger.warning("radar: claim hand-back failed (%s)", type(exc).__name__)
    if out["tasks"]:
        logger.info("radar: claimed boards=%d of %d", got, want)
    return out


# ---------------------------------------------------------------------------
# Releases
# ---------------------------------------------------------------------------

def _invalid(message: str) -> RadarError:
    return RadarError(400, "invalid_request", message)


def validate_claim_request(body: Any) -> int:
    """The optional `{"limit": n}` of a claim, or `RadarError(400)`."""
    if body is None:
        return CLAIM_DEFAULT
    if not isinstance(body, dict):
        raise _invalid("the body must be a JSON object")
    limit = body.get("limit")
    if limit is None:
        return CLAIM_DEFAULT
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise _invalid("limit must be a whole number")
    return max(1, min(CLAIM_MAX, limit))


def validate_release(body: Any) -> Tuple[List[str], str]:
    """`(lease ids, reason)`, typed and bounded, or `RadarError(400)`."""
    if not isinstance(body, dict):
        raise _invalid("the body must be a JSON object")
    ids = body.get("lease_ids")
    reason = body.get("reason")
    if not isinstance(reason, str) or reason not in RELEASE_REASONS:
        raise _invalid("reason must be one of: " + ", ".join(RELEASE_REASONS))
    if not isinstance(ids, list) or not ids:
        raise _invalid("lease_ids must be a non-empty list")
    if len(ids) > CLAIM_MAX:
        raise _invalid(f"at most {CLAIM_MAX} lease_ids")
    for value in ids:
        if not isinstance(value, str) or not _LEASE_ID_RE.match(value):
            raise _invalid("a lease id is not usable")
    return list(dict.fromkeys(ids)), reason


def _park_update(result: str, now: datetime) -> Dict[str, Any]:
    """A board that could not be read: recorded, freed and backed off."""
    wait = (timedelta(days=PARK_GONE_DAYS) if result == "gone"
            else timedelta(hours=PARK_ROBOTS_HOURS))
    # A parked board is not handed out, so no later report can arrive to
    # shorten a longer park: a plain `$set` is enough.
    return {"$set": {"lease": None, "last_result": result, "last_attempt_at": now,
                     "parked_reason": result, "parked_until": now + wait}}


def _record_board_challenge(db, key: str, doc: Dict[str, Any], now: datetime) -> None:
    """Count the different accounts challenged on this board in 24 hours and
    park it at `BOARD_CHALLENGE_ACCOUNTS`."""
    cutoff = now - timedelta(hours=BOARD_CHALLENGE_WINDOW_HOURS)
    accounts = set()
    for entry in doc.get("challenged") or []:
        at = radar._aware((entry or {}).get("at"))
        if at is not None and at > cutoff and entry.get("by"):
            accounts.add(entry["by"])
    update: Dict[str, Any] = {"$set": {"challenges": len(accounts)}}
    if len(accounts) >= BOARD_CHALLENGE_ACCOUNTS:
        update["$set"]["parked_reason"] = "challenge"
        update["$set"]["parked_until"] = now + timedelta(hours=PARK_CHALLENGE_HOURS)
    db[LEASES].update_one({"_id": key}, update)


def release_leases(email: str, lease_ids: Iterable[str], reason: str, *,
                   now: Optional[datetime] = None) -> Dict[str, Any]:
    """Free the leases this account holds, and record why.

    The filter names the holder, so one account cannot free another's lease
    (the crawler's `release_lease` rule). `challenge` is counted on the board
    and for the fleet; `robots` and `gone` park the board."""
    from pymongo import ReturnDocument

    now = now or radar._utcnow()
    ids, reason = validate_release({"lease_ids": list(lease_ids), "reason": reason})
    who = account_pseudonym(email)
    db = radar._db()
    released = 0
    for lease_id in ids:
        if reason in READ_RESULTS:
            update = _park_update(reason, now)
        elif reason == "challenge":
            update = {"$set": {"lease": None, "last_challenge_at": now},
                      "$push": {"challenged": {"$each": [{"by": who, "at": now}],
                                               "$slice": -BOARD_CHALLENGE_KEEP}}}
        else:
            update = {"$set": {"lease": None}}
        doc = db[LEASES].find_one_and_update(
            {"lease.id": lease_id, "lease.by": who}, update,
            return_document=ReturnDocument.AFTER)
        if doc is None:
            continue
        released += 1
        if reason == "challenge":
            _record_board_challenge(db, doc["_id"], doc, now)
    # A challenge counts for the fleet when it freed a lease, or when the
    # lease had lapsed but this account did claim boards in the last two
    # hours. The wall is about the browser's address, not about the lease. An
    # account that never claimed cannot move the brake.
    if reason == "challenge" and (
            released or who in _recent_accounts(db, now, 2, "claimers")):
        _bump_hour(db, now, challenger=who)
        paused = _check_brake(db, now)
    else:
        paused = fleet_state(db, now=now)["paused"]
    logger.info("radar: leases released=%d of %d reason=%s", released, len(ids), reason)
    return {"released": released, "reason": reason, "paused": paused}


# ---------------------------------------------------------------------------
# A written batch completes leases
# ---------------------------------------------------------------------------

def validate_reads(raw: Any) -> List[Dict[str, str]]:
    """The optional batch field `reads`: boards that could not be read.

    At most `MAX_READS` of `{tenant, site, wd_host, domain, result}` with
    `result` in `gone` | `robots`. A bad entry refuses the batch (400)."""
    from services.ats_providers.workday import _SITE_RE, _TENANT_RE, _WD_HOST_RE

    if raw is None:
        return []
    if not isinstance(raw, list):
        raise RadarError(400, "invalid_batch", "reads must be a list")
    if len(raw) > MAX_READS:
        raise RadarError(400, "invalid_batch", f"at most {MAX_READS} reads per batch")
    out: List[Dict[str, str]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise RadarError(400, "invalid_batch", "each read must be an object")
        tenant, site, wd_host = entry.get("tenant"), entry.get("site"), entry.get("wd_host")
        domain = entry.get("domain") or "myworkdayjobs.com"
        result = entry.get("result")
        if (not isinstance(tenant, str) or not _TENANT_RE.match(tenant)
                or not isinstance(site, str) or not _SITE_RE.match(site)
                or not isinstance(wd_host, str) or not _WD_HOST_RE.match(wd_host)
                or domain not in radar._DOMAINS):
            raise RadarError(400, "invalid_batch", "a read names an unusable board")
        if not isinstance(result, str) or result not in READ_RESULTS:
            raise RadarError(400, "invalid_batch", "a read result must be gone or robots")
        out.append({"tenant": tenant, "site": site, "wd_host": wd_host.lower(),
                    "domain": domain, "result": result})
    return out


def record_reads(db, email: str, boards_read: List[Dict[str, Any]],
                 reads: List[Dict[str, str]], *, now: datetime) -> Dict[str, int]:
    """What one WRITTEN batch says about boards.

    `boards_read`: the known boards it read with no search term, each
    `{key, board, postings}`. Each is a read of that board whoever holds the
    lease: the read is stamped, the lease cleared and the board made due
    again one interval on.

    `reads`: boards the browser could not read. Parking a board for days is
    worth more to an abuser than marking one read, so it is taken only from
    the account that holds the board's lease.

    Best effort: a failure here never fails the upload."""
    from pymongo import ReturnDocument

    counts = {"completed": 0, "recorded": 0, "ignored": 0}
    who = account_pseudonym(email)
    try:
        merged: Dict[str, Dict[str, Any]] = {}
        for item in boards_read:
            entry = merged.setdefault(item["key"], {"board": item["board"], "postings": 0})
            entry["postings"] += int(item.get("postings") or 0)
        boosts: Dict[str, float] = {}
        if merged:
            for doc in db[LEASES].find({"_id": {"$in": list(merged)}}, {"boost": 1}):
                try:
                    boosts[doc["_id"]] = float(doc.get("boost") or 1.0)
                except (TypeError, ValueError):
                    boosts[doc["_id"]] = 1.0
        target = target_hours()
        for key, entry in merged.items():
            boost = max(1.0, min(BOOST_MAX, boosts.get(key, 1.0)))
            interval = max(MIN_INTERVAL_HOURS, target / boost)
            postings = entry["postings"]
            fields = dict(entry["board"], in_registry=True, last_read_at=now,
                          last_read_by=who, last_postings=postings,
                          last_result="ok" if postings else "empty",
                          due_at=now + timedelta(hours=interval), lease=None)
            if postings:
                # Postings on the page are proof the board can be read.
                fields["parked_until"] = None
            on_insert = dict(_priority_defaults(), challenges=0, created_at=now)
            if not postings:
                on_insert["parked_until"] = None
            db[LEASES].update_one({"_id": key}, {"$set": fields, "$setOnInsert": on_insert},
                                  upsert=True)
            counts["completed"] += 1
        for read in reads:
            if read.get("key") is None:
                counts["ignored"] += 1
                continue
            doc = db[LEASES].find_one_and_update(
                {"_id": read["key"], "lease.by": who}, _park_update(read["result"], now),
                return_document=ReturnDocument.AFTER)
            counts["recorded" if doc is not None else "ignored"] += 1
    except Exception as exc:
        logger.warning("radar: board read record failed (%s)", type(exc).__name__)
    return counts


# ---------------------------------------------------------------------------
# Coverage
# ---------------------------------------------------------------------------

def _measure(db, now: datetime, stats: List[Tuple[str, int, Optional[datetime]]],
             registry: Dict[str, int]) -> Dict[str, Any]:
    window = now - timedelta(hours=COVERAGE_WINDOW_HOURS)
    tenants, by_browser = set(), set()
    boards_total = boards_refreshed = never_read = leased_now = parked = 0
    for doc in db[LEASES].find({"in_registry": True},
                               {"tenant": 1, "stats_tenant": 1, "last_read_at": 1,
                                "lease": 1, "parked_until": 1}):
        boards_total += 1
        tenant = str(doc.get("stats_tenant") or doc.get("tenant") or "").lower()
        tenants.add(tenant)
        read_at = radar._aware(doc.get("last_read_at"))
        if read_at is None:
            never_read += 1
        elif read_at >= window:
            boards_refreshed += 1
            by_browser.add(tenant)
        until = radar._aware((doc.get("lease") or {}).get("until"))
        if until is not None and until > now:
            leased_now += 1
        parked_until = radar._aware(doc.get("parked_until"))
        if parked_until is not None and parked_until > now:
            parked += 1
    by_crawler = {t for t, _n, seen in stats
                  if seen is not None and seen >= window and t in tenants}

    hours = COVERAGE_WINDOW_HOURS
    ids = [_hour_id(now - timedelta(hours=back)) for back in range(hours)]
    claims = sum(int(doc.get("claims") or 0)
                 for doc in db[FLEET].find({"_id": {"$in": ids}}, {"claims": 1}))
    accounts = len(_recent_accounts(db, now, hours, "claimers"))
    return {
        "measured_at": now,
        "window_hours": COVERAGE_WINDOW_HOURS,
        "target_hours": target_hours(),
        "tenants_total": len(tenants),
        "tenants_refreshed": len(by_browser | by_crawler),
        "by_browser": len(by_browser),
        "by_crawler": len(by_crawler),
        "boards_total": boards_total,
        "boards_refreshed": boards_refreshed,
        "never_read": never_read,
        "leased_now": leased_now,
        "parked": parked,
        "claims_24h": claims,
        "accounts_24h": accounts,
        "registry": dict(registry),
        "expire_at": now + timedelta(days=COVERAGE_KEEP_DAYS),
    }


def run_coverage(budget_seconds: int = 120, *,
                 now: Optional[datetime] = None) -> Dict[str, Any]:
    """The `radar_coverage` job: sync the ledger, then store one snapshot.

    Stores nothing when the registry or the crawler's stats could not be
    read. The last good snapshot then ages into `stale`, which is the honest
    answer. Never raises."""
    started = time.monotonic()
    deadline = started + max(5, int(budget_seconds))
    now = now or radar._utcnow()
    out: Dict[str, Any] = {"stored": False, "reason": ""}
    if not radar.enabled():
        out.update(reason="disabled", seconds=0.0)
        return out
    try:
        stats = radar._read_tenant_stats()
        sync = sync_boards(now=now, stats=stats, deadline=deadline)
        out["sync"] = {k: sync[k] for k in ("ok", "reason", "added", "updated",
                                             "restored", "retired")}
        if not sync["ok"]:
            out["reason"] = sync["reason"]
        elif stats is None:
            out["reason"] = "stats_unavailable"
        elif time.monotonic() > deadline:
            out["reason"] = "budget"
        else:
            db = radar._db()
            snapshot = _measure(db, now, stats, sync["registry"])
            db[COVERAGE].insert_one(dict(snapshot))
            out.update(stored=True, tenants_total=snapshot["tenants_total"],
                       tenants_refreshed=snapshot["tenants_refreshed"],
                       boards_total=snapshot["boards_total"],
                       boards_refreshed=snapshot["boards_refreshed"])
    except Exception as exc:
        logger.warning("radar: coverage run failed (%s)", type(exc).__name__)
        out["reason"] = "failed"
    out["seconds"] = round(time.monotonic() - started, 1)
    logger.info("radar: coverage stored=%s reason=%s tenants=%s/%s boards=%s/%s in %.1fs",
                out["stored"], out["reason"] or "-", out.get("tenants_refreshed", "-"),
                out.get("tenants_total", "-"), out.get("boards_refreshed", "-"),
                out.get("boards_total", "-"), out["seconds"])
    return out


_FIGURES = ("window_hours", "target_hours", "tenants_total", "tenants_refreshed",
            "by_browser", "by_crawler", "boards_total", "boards_refreshed", "never_read",
            "leased_now", "parked", "claims_24h", "accounts_24h", "registry")


def coverage_block(*, now: Optional[datetime] = None) -> Dict[str, Any]:
    """The newest stored snapshot with its age, or `available: false`.

    `available: false` means not known. It is never a coverage of zero."""
    now = now or radar._utcnow()
    try:
        doc = radar._db()[COVERAGE].find_one({}, sort=[("measured_at", -1)])
    except Exception as exc:
        logger.warning("radar: coverage read failed (%s)", type(exc).__name__)
        return {"available": False,
                "detail": "The Radar coverage measurement could not be read."}
    measured = radar._aware((doc or {}).get("measured_at"))
    if measured is None:
        return {"available": False,
                "detail": "No Radar coverage measurement has been stored yet."}
    age = round(max(0.0, _hours(now - measured)), 2)
    out: Dict[str, Any] = {"available": True, "measured_at": radar._iso(measured),
                           "age_hours": age, "stale": age > COVERAGE_STALE_HOURS,
                           "stale_after_hours": COVERAGE_STALE_HOURS}
    for field in _FIGURES:
        out[field] = doc.get(field)
    return out


def fleet_block(*, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Admin: the brake and the leases as they are now. Board data only: no
    email and no account handle."""
    now = now or radar._utcnow()
    try:
        db = radar._db()
        state = fleet_state(db, now=now)
        leased = db[LEASES].count_documents({"in_registry": True,
                                             "lease.until": {"$gt": now}})
        parked_filter = {"in_registry": True, "parked_until": {"$gt": now}}
        parked = db[LEASES].count_documents(parked_filter)
        boards = [{"tenant": d.get("tenant"), "site": d.get("site"),
                   "wd_host": d.get("wd_host"), "domain": d.get("domain"),
                   "reason": d.get("parked_reason"),
                   "parked_until": radar._iso(d.get("parked_until")),
                   "challenges": int(d.get("challenges") or 0)}
                  for d in db[LEASES].find(parked_filter).sort("parked_until", -1).limit(25)]
    except Exception as exc:
        logger.warning("radar: fleet read failed (%s)", type(exc).__name__)
        return {"available": False}
    return {"available": True, "paused": state["paused"],
            "paused_until": state["paused_until"], "leased_now": int(leased),
            "parked": int(parked), "parked_boards": boards}
