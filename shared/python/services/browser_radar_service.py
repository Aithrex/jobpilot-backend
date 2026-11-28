"""Browser Radar: Workday listings read by a signed-in person's own browser.

Why this exists
---------------
Since about 2026-11-14 Workday's job sites sit behind Cloudflare bot
management. The crawler's box gets `429 Security Check` on most Workday
requests, so Workday listings fell from ~3M fetched a day to ~37k (see
`docs/coverage/workday-alternatives-2026-11-17.md`). A person's own browser
still reads the same public listings with plain 200s.

ApplyPilot (the owner's Chrome extension) reads Workday boards at a person's
pace for its owner's own job search. This module is the server half:

* `build_plan` tells the extension which boards are worth reading for this
  user (their own For-you Workday employers) and, for contributors, which
  boards the crawler has not seen for a while;
* `ingest_batch` takes what the browser saw and feeds it through the SAME
  mapping and write path the crawl uses.

Rules this module keeps (the contract is `radar-1`, written down in
`docs/specs/browser-radar.md`):

* **The server re-derives everything.** The browser sends Workday's raw CXS
  fields. `workday_row` maps them the way the PRODUCTION crawl does
  (`job_ingest_worker._list_workday`, the legacy listing path Workday is
  pinned to), so a browser sighting and a crawler sighting are the same row.
  A test drives the real crawl path and compares the stored documents.
* **Only boards the crawler already knows are written.** A board must resolve
  to a member of `registry_members("workday")`. Anything else is recorded in
  `browser_radar_unknown_boards` for discovery and never reaches
  `job_postings`.
* **Nothing is ever closed.** A browser batch is a search, not a census.
* **On by default (owner's call, 2026-11-19).** `BROWSER_RADAR_INGEST`
  unset means `on`; `dry_run` records the batch and reports what it would
  write without writing, and `off` refuses batches.
* **Every signed-in account may send (2026-11-26), so a batch is untrusted
  input.** What keeps it honest is in `_decide` and `_process`, not in who
  sent it: a browser sighting never replaces a body we already fetched, never
  changes a stored posting's title, link or detail address, and never reopens
  a posting whose own page said it was gone. Every row it writes is listed on
  the batch row (`written`), and a posting first written by a browser carries
  `origin: "browser"`, so one account's rows can be found and removed.

`radar-2` adds the fleet half, in `browser_radar_fleet.py`. `radar-1` gives
every contributor the same stale boards. `radar-2` keeps one row per registry
board (`browser_radar_board_leases`), leases due boards to one browser at a
time (`POST .../claims`), and counts a termless read of a known board as a
read of that board. This module calls it in three places: the plan says
`claims: true` when the account can claim, a written batch completes leases
(`ingest_batch`), and the admin payload carries `coverage` and `fleet`.
`radar-1` clients are unchanged, except that a termless read counts as a read
of the board only from the account that holds the board's lease.

`radar-3` adds one posting from any job page, in `browser_radar_posting.py`
(`POST .../posting/lookup` and `POST .../posting`). It reuses this module's
gates, `_decide`, the guard fields and `_write_rows`, so a single posting is
written under exactly the rules a batch is.

The crawler does not depend on any of this. Dropping the seven collections
(`browser_radar_batches`, `browser_radar_usage`,
`browser_radar_unknown_boards`, `browser_radar_board_leases`,
`browser_radar_coverage_snapshots`, `browser_radar_fleet`,
`browser_radar_staged_postings`) removes the feature's state and nothing else.
"""
from __future__ import annotations

import collections
import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

PLAN_VERSION = "radar-1"

MODE_OFF = "off"
MODE_DRY_RUN = "dry_run"
MODE_ON = "on"
MODES = (MODE_OFF, MODE_DRY_RUN, MODE_ON)

BATCHES = "browser_radar_batches"
USAGE = "browser_radar_usage"
UNKNOWN_BOARDS = "browser_radar_unknown_boards"
# `radar-2` (browser_radar_fleet.py). Named here so the index list is whole.
LEASES = "browser_radar_board_leases"
COVERAGE = "browser_radar_coverage_snapshots"
FLEET = "browser_radar_fleet"
# `radar-3` (browser_radar_posting.py): postings the registry cannot place.
STAGED = "browser_radar_staged_postings"

#: What the plan suggests. The extension clamps these to its own hard caps,
#: so the server can only ever LOWER what a browser does.
PLAN_LIMITS = {
    "max_requests_per_run": 40,
    "max_requests_per_day": 300,
    "min_interval_ms": 6000,
    "max_details_per_run": 15,
}

PLAN_DEFAULT_BOARDS = 40
PLAN_MAX_BOARDS = 80
PLAN_MAX_TERMS = 5
PLAN_TERMS_PER_BOARD = 2

#: A tenant whose newest sighting of an open posting is older than this is
#: one the crawler is not reaching.
STALE_HOURS = 12.0
STATS_TTL_SECONDS = 1800.0

# Batch bounds (contract section 2). Past any of them the whole request is
# refused with 400 `invalid_batch`.
MAX_BODY_BYTES = 2 * 1024 * 1024
MAX_BOARDS = 40
MAX_POSTINGS_PER_BOARD = 100
MAX_POSTINGS = 1000
MAX_DETAILS = 30
MAX_DESCRIPTION_CHARS = 200_000

# Per account. Fixed windows: an hour for batches, a UTC day for postings.
BATCHES_PER_HOUR = 30
POSTINGS_PER_DAY = 5000

BATCH_KEEP_DAYS = 30
#: How many posting ids one batch row lists (a batch holds at most 1,000).
LEDGER_MAX_IDS = 1000
#: A posting first written by a browser.
ORIGIN_BROWSER = "browser"
#: Closures the posting's OWN page reported. A listing that still shows the
#: requisition is not proof against them (boards keep dead rows listed), and
#: the body a browser sends is the sender's word, so neither reopens one.
NO_REOPEN_REASONS = frozenset({"detail-gone", "link-dead"})
#: The crawl keeps this much of a fetched body (`fetch_detail`); the browser
#: path keeps exactly as much.
DETAIL_KEEP_CHARS = 15000
#: A batch's own `observed_at` is the clock its relative dates ("Posted 3 Days
#: Ago") are read against, when it is not in the future and not older than
#: this. A parked batch can arrive a day late. Outside the bound the server
#: clock is used; nothing is refused for it.
OBSERVED_AT_MAX_AGE_DAYS = 7

# The admin set `services/auth/blueprints/admin.py` reads. Copied rather than
# imported: the jobs service must not import the auth blueprint.
_ADMIN_EMAILS = frozenset({"harshith@aithrex.tech", "mannesiddardha@gmail.com"})

_BATCH_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{8,80}$")
_DOMAINS = ("myworkdayjobs.com", "myworkdaysite.com")
# A posting's path hangs off the board's own host (`{apply_base}/{site}/{path}`),
# so the host can never be chosen by the caller. The path is still constrained:
# Workday's own paths are `/job/<place>/<title>_<req>`.
_PATH_RE = re.compile(r"^/?job/[^\s?#\\]{1,480}$")


class RadarError(Exception):
    """A refusal with its HTTP status and machine-readable code."""

    def __init__(self, status: int, code: str, message: str,
                 retry_after: Optional[int] = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.retry_after = retry_after


# ---------------------------------------------------------------------------
# Switches and identity
# ---------------------------------------------------------------------------

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: Any) -> Optional[datetime]:
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def enabled() -> bool:
    """The server's kill switch (`BROWSER_RADAR_ENABLED`, default on)."""
    raw = str(os.environ.get("BROWSER_RADAR_ENABLED", "1")).strip().lower()
    return raw not in ("0", "false", "no", "off")


def ingest_mode() -> str:
    """`off` | `dry_run` | `on` from `BROWSER_RADAR_INGEST`. Unset is `on`
    (the owner's default). A value that is none of the three reads as
    `dry_run`: a typo of `off` must not write."""
    raw = str(os.environ.get("BROWSER_RADAR_INGEST", MODE_ON)).strip().lower()
    return raw if raw in MODES else MODE_DRY_RUN


def _contributor_list() -> frozenset:
    raw = str(os.environ.get("BROWSER_RADAR_CONTRIBUTORS", "") or "")
    return frozenset(e.strip().lower() for e in raw.split(",") if e.strip())


def _db():
    from utils.db_connect import DBConnect
    return DBConnect().get_db()


def contributor_scope() -> str:
    """Who may send listings (`BROWSER_RADAR_CONTRIBUTORS_SCOPE`).

    `all` (the default, owner's decision 2026-11-26): every signed-in account,
    so every existing and future user is a contributor. `listed`: only admins
    and `BROWSER_RADAR_CONTRIBUTORS`. Anything else reads as `listed`.
    """
    raw = str(os.environ.get("BROWSER_RADAR_CONTRIBUTORS_SCOPE", "all")).strip().lower()
    return "all" if raw == "all" else "listed"


def is_contributor(email: Any) -> bool:
    """Every signed-in account by default; with scope `listed`, an admin
    (`users.is_admin` or the admin set) or a listed email."""
    if not isinstance(email, str) or not email.strip():
        return False
    if contributor_scope() == "all":
        return True
    key = email.strip().lower()
    if key in _ADMIN_EMAILS or key in _contributor_list():
        return True
    try:
        doc = _db().users.find_one({"email": email.strip()}, {"is_admin": 1})
    except Exception as exc:  # pragma: no cover - a read failure is "no"
        logger.warning("radar: admin lookup failed (%s)", type(exc).__name__)
        return False
    return bool(doc and doc.get("is_admin"))


_INDEXED = False
_INDEX_LOCK = threading.Lock()


_TTL = {"expireAfterSeconds": 0, "name": "ttl_expire_at"}

#: Every Radar index. `scripts/migrate_indexes.py` mirrors this list.
INDEX_SPECS = (
    (BATCHES, [("expire_at", 1)], _TTL),
    (USAGE, [("expire_at", 1)], _TTL),
    # The claim: `in_registry` equal, `due_at` a range and the sort, `rank`
    # the tie-break, `parked_until` checked from the key.
    (LEASES, [("in_registry", 1), ("due_at", 1), ("rank", 1), ("parked_until", 1)],
     {"name": "radar_board_due"}),
    # A release finds its board by the lease id. Sparse: a free board has none.
    (LEASES, [("lease.id", 1)], {"name": "radar_board_lease_id", "sparse": True}),
    (COVERAGE, [("measured_at", -1)], {"name": "radar_coverage_recent"}),
    (COVERAGE, [("expire_at", 1)], _TTL),
    (FLEET, [("expire_at", 1)], _TTL),
    # `radar-3`: staged postings live 30 days past their last sighting. The
    # admin counts read `state`, and the top hosts read `host`.
    (STAGED, [("expire_at", 1)], _TTL),
    (STAGED, [("state", 1)], {"name": "radar_staged_state"}),
    (STAGED, [("host", 1)], {"name": "radar_staged_host"}),
)


def _ensure_indexes(db) -> None:
    """The Radar indexes, once per process. Best effort: a failure costs
    retention or speed, never a request."""
    global _INDEXED
    if _INDEXED:
        return
    with _INDEX_LOCK:
        if _INDEXED:
            return
        for name, keys, options in INDEX_SPECS:
            try:
                db[name].create_index(keys, **options)
            except Exception as exc:  # pragma: no cover
                logger.warning("radar: index on %s failed (%s)", name, type(exc).__name__)
        _INDEXED = True


# ---------------------------------------------------------------------------
# The registry, as boards
# ---------------------------------------------------------------------------

def _worker():
    from services import job_ingest_worker as w
    return w


def _provider():
    """The registered WorkdayProvider (the one the crawl uses)."""
    w = _worker()
    registry = w._ats_registry()
    provider = registry.get("workday") if registry is not None else None
    if provider is None:  # pragma: no cover - the registry always has it
        from services.ats_providers.workday import WorkdayProvider
        provider = WorkdayProvider()
    return provider


def _members() -> List[Dict[str, Any]]:
    w = _worker()
    try:
        return list(w.registry_members(w.SOURCE_WORKDAY))
    except Exception as exc:  # pragma: no cover
        logger.warning("radar: workday registry unavailable (%s)", type(exc).__name__)
        return []


def _board_of(member: Dict[str, Any]) -> Dict[str, str]:
    """A member's address as the extension needs it, through the provider's
    own readers so the pod, site and domain are exactly the crawl's."""
    w = _worker()
    from services.ats_providers.workday import WorkdayProvider
    endpoint = w._endpoint_from_member(member)
    return {
        "tenant": endpoint.tenant,
        "wd_host": WorkdayProvider._wd_host_of(endpoint),
        "site": WorkdayProvider._site_of(endpoint),
        "domain": WorkdayProvider._domain_of(endpoint),
        "employer": str(member.get("display_name") or endpoint.tenant),
    }


def _index_members() -> Tuple[Dict[str, Dict[str, Any]], Dict[Tuple[str, str, str, str], Dict[str, Any]]]:
    """`(by stored posting tenant, by full address)`, both lowercased."""
    w = _worker()
    by_tenant: Dict[str, Dict[str, Any]] = {}
    by_address: Dict[Tuple[str, str, str, str], Dict[str, Any]] = {}
    for member in _members():
        try:
            board = _board_of(member)
        except Exception:  # pragma: no cover - a malformed catalog row
            continue
        by_tenant.setdefault(w._member_tenant(member).lower(), member)
        address = (board["domain"], board["wd_host"].lower(),
                   board["tenant"].lower(), board["site"].lower())
        by_address.setdefault(address, member)
    return by_tenant, by_address


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------

def _search_terms(email: str) -> List[str]:
    """The user's board titles as phrases, at most `PLAN_MAX_TERMS`."""
    try:
        doc = _db()["board_requests"].find_one({"_id": email}, {"params.titles": 1}) or {}
    except Exception as exc:  # pragma: no cover
        logger.warning("radar: board request read failed (%s)", type(exc).__name__)
        return []
    titles = ((doc.get("params") or {}).get("titles")) or []
    if not isinstance(titles, list):
        return []
    from services.inventory_feed import title_phrases
    return title_phrases([str(t) for t in titles if isinstance(t, str)],
                         cap=PLAN_MAX_TERMS)


def _relevant_tenants(email: str) -> List[str]:
    """Workday tenants on the user's own For-you board, most cards first
    (then the higher summed match score)."""
    try:
        doc = _db()["board_snapshots"].find_one(
            {"_id": f"{email}|workday"}, {"result": 1}) or {}
    except Exception as exc:  # pragma: no cover
        logger.warning("radar: board snapshot read failed (%s)", type(exc).__name__)
        return []
    result = doc.get("result")
    cards: Iterable[Any] = []
    if isinstance(result, dict):
        cards = result.get("jobs") or result.get("results") or []
    elif isinstance(result, list):
        cards = result
    weight: Dict[str, List[float]] = {}
    for card in cards if isinstance(cards, list) else []:
        if not isinstance(card, dict):
            continue
        if str(card.get("ats") or "workday").lower() != "workday":
            continue
        tenant = str(card.get("tenant") or "").strip().lower()
        if not tenant:
            continue
        try:
            score = float(card.get("match_score") or 0)
        except (TypeError, ValueError):
            score = 0.0
        entry = weight.setdefault(tenant, [0.0, 0.0])
        entry[0] += 1
        entry[1] += score
    return [t for t, _ in sorted(weight.items(), key=lambda kv: (-kv[1][0], -kv[1][1], kv[0]))]


_STATS: Dict[str, Any] = {"at": 0.0, "rows": None}
_STATS_LOCK = threading.Lock()


def reset_stats_cache() -> None:
    """Test hook: the per-tenant stats are cached for the process."""
    with _STATS_LOCK:
        _STATS["at"] = 0.0
        _STATS["rows"] = None


def _tenant_stats() -> List[Tuple[str, int, Optional[datetime]]]:
    """`(tenant, active postings, newest last_seen_at)` per Workday tenant.

    One grouped read, cached `STATS_TTL_SECONDS` per process: it scans the
    open Workday inventory and the plan is asked for per user."""
    now = time.monotonic()
    with _STATS_LOCK:
        if _STATS["rows"] is not None and now - _STATS["at"] < STATS_TTL_SECONDS:
            return _STATS["rows"]
    rows = _read_tenant_stats()
    if rows is None:
        return []
    with _STATS_LOCK:
        _STATS["rows"] = rows
        _STATS["at"] = now
    return rows


def _read_tenant_stats() -> Optional[List[Tuple[str, int, Optional[datetime]]]]:
    """The grouped read itself, uncached. None when it failed, so a caller
    that must tell "no postings" from "could not read" can."""
    w = _worker()
    rows: List[Tuple[str, int, Optional[datetime]]] = []
    try:
        cursor = _db()["job_postings"].aggregate([
            {"$match": {"source": w.SOURCE_WORKDAY, "status": "active"}},
            {"$group": {"_id": "$tenant", "n": {"$sum": 1},
                        "seen": {"$max": "$last_seen_at"}}},
        ], maxTimeMS=20000)
        for doc in cursor:
            tenant = str(doc.get("_id") or "").strip().lower()
            if tenant:
                rows.append((tenant, int(doc.get("n") or 0), _aware(doc.get("seen"))))
    except Exception as exc:
        logger.warning("radar: tenant stats unavailable (%s)", type(exc).__name__)
        return None
    return rows


def _stale_tenants(now: datetime) -> List[str]:
    cutoff = now - timedelta(hours=STALE_HOURS)
    stale = [(t, n) for t, n, seen in _tenant_stats() if seen is None or seen < cutoff]
    return [t for t, _ in sorted(stale, key=lambda tn: (-tn[1], tn[0]))]


def build_plan(email: str, limit: int = PLAN_DEFAULT_BOARDS, *,
               now: Optional[datetime] = None) -> Dict[str, Any]:
    """The `radar-1` plan for one account. Never raises on missing data."""
    now = now or _utcnow()
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = PLAN_DEFAULT_BOARDS
    limit = max(1, min(PLAN_MAX_BOARDS, limit))
    on = enabled()
    contributor = is_contributor(email)
    terms = _search_terms(email) if on else []

    tasks: List[Dict[str, Any]] = []
    if on:
        relevant = _relevant_tenants(email)
        stale = _stale_tenants(now) if contributor else []
        relevant_set, stale_set = set(relevant), set(stale)
        order: List[Tuple[str, str]] = (
            [(t, "both") for t in relevant if t in stale_set]
            + [(t, "relevant") for t in relevant if t not in stale_set]
            + [(t, "stale") for t in stale if t not in relevant_set])
        by_tenant, _ = _index_members() if order else ({}, {})
        boards = 0
        for tenant, why in order:
            if boards >= limit:
                break
            member = by_tenant.get(tenant)
            if member is None:
                continue
            board = _board_of(member)
            if why == "stale":
                tasks.append(dict(board, search_text="", max_pages=1, why=why))
            else:
                for term in (terms[:PLAN_TERMS_PER_BOARD] or [""]):
                    tasks.append(dict(board, search_text=term, max_pages=1, why=why))
            boards += 1

    return {
        "plan_version": PLAN_VERSION,
        "enabled": on,
        "ingest_mode": ingest_mode(),
        "contributor": contributor,
        # `radar-2`: ask `POST .../claims` for shared boards instead of
        # reading the `stale` tasks below (kept for `radar-1` clients).
        "claims": bool(on and contributor and ingest_mode() == MODE_ON),
        "generated_at": now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "search_terms": terms,
        "limits": dict(PLAN_LIMITS),
        "tasks": tasks,
    }


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _invalid(message: str) -> RadarError:
    return RadarError(400, "invalid_batch", message)


def _text(value: Any, max_len: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return ""
    return str(value).strip()[:max_len]


def _clean_posting(raw: Any) -> Optional[Dict[str, Any]]:
    """Only Workday's own listing keys, typed. None = rejected."""
    if not isinstance(raw, dict):
        return None
    path = raw.get("externalPath")
    if not isinstance(path, str) or ".." in path or not _PATH_RE.match(path.strip()):
        return None
    bullets = raw.get("bulletFields")
    bullets = [_text(b, 120) for b in bullets[:10]] if isinstance(bullets, list) else []
    return {
        "title": _text(raw.get("title"), 300),
        "externalPath": path.strip(),
        "bulletFields": [b for b in bullets if b],
        "postedOn": _text(raw.get("postedOn"), 80),
        "locationsText": _text(raw.get("locationsText"), 300),
        "timeType": _text(raw.get("timeType"), 80),
    }


def _clean_detail(raw: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    path = raw.get("externalPath")
    info = raw.get("jobPostingInfo")
    if not isinstance(path, str) or not _PATH_RE.match(path.strip()) or ".." in path:
        return None
    if not isinstance(info, dict):
        return None
    body = info.get("jobDescription")
    if body is not None and not isinstance(body, str):
        return None
    if isinstance(body, str) and len(body) > MAX_DESCRIPTION_CHARS:
        raise _invalid(f"a jobDescription is over {MAX_DESCRIPTION_CHARS} characters")
    # Only the body is read. The production crawl reads nothing else from a
    # Workday detail (`fetch_detail`): not `startDate`, not `location`.
    return {
        "externalPath": path.strip(),
        "jobPostingInfo": {"jobDescription": body or ""},
    }


def validate_batch(body: Any) -> Dict[str, Any]:
    """The batch, typed and bounded, or `RadarError(400)`.

    Board identity is constrained by the provider's own regexes (they become
    URL path segments). A posting or detail of the wrong shape is REJECTED
    (counted), not fatal; a batch past any bound is refused whole."""
    from services.ats_providers.workday import _SITE_RE, _TENANT_RE, _WD_HOST_RE

    if not isinstance(body, dict):
        raise _invalid("the body must be a JSON object")
    batch_id = body.get("batch_id")
    if not isinstance(batch_id, str) or not _BATCH_ID_RE.match(batch_id):
        raise _invalid("batch_id must be 8-80 letters, digits, '-' or '_'")
    # `radar-2`: boards that could not be read. A batch may carry only these.
    from services import browser_radar_fleet as fleet
    reads = fleet.validate_reads(body.get("reads"))
    boards = body.get("boards")
    if boards is None and reads:
        boards = []
    if not isinstance(boards, list) or not (boards or reads):
        raise _invalid("boards must be a non-empty list")
    if len(boards) > MAX_BOARDS:
        raise _invalid(f"at most {MAX_BOARDS} boards per batch")

    out_boards: List[Dict[str, Any]] = []
    total_postings = total_details = 0
    for board in boards:
        if not isinstance(board, dict):
            raise _invalid("each board must be an object")
        tenant = board.get("tenant")
        site = board.get("site")
        wd_host = board.get("wd_host")
        domain = board.get("domain") or "myworkdayjobs.com"
        if not isinstance(tenant, str) or not _TENANT_RE.match(tenant):
            raise _invalid("a board has an unusable tenant")
        if not isinstance(site, str) or not _SITE_RE.match(site):
            raise _invalid("a board has an unusable site")
        if not isinstance(wd_host, str) or not _WD_HOST_RE.match(wd_host):
            raise _invalid("a board has an unusable wd_host")
        if domain not in _DOMAINS:
            raise _invalid("a board has an unknown domain")
        postings = board.get("postings") or []
        details = board.get("details") or []
        if not isinstance(postings, list) or not isinstance(details, list):
            raise _invalid("postings and details must be lists")
        if len(postings) > MAX_POSTINGS_PER_BOARD:
            raise _invalid(f"at most {MAX_POSTINGS_PER_BOARD} postings per board")
        total_postings += len(postings)
        total_details += len(details)
        if total_postings > MAX_POSTINGS:
            raise _invalid(f"at most {MAX_POSTINGS} postings per batch")
        if total_details > MAX_DETAILS:
            raise _invalid(f"at most {MAX_DETAILS} details per batch")
        cleaned_postings = [_clean_posting(p) for p in postings]
        cleaned_details = [_clean_detail(d) for d in details]
        out_boards.append({
            "tenant": tenant, "site": site, "wd_host": wd_host.lower(), "domain": domain,
            "search_text": _text(board.get("search_text"), 200),
            "postings": [p for p in cleaned_postings if p is not None],
            "details": [d for d in cleaned_details if d is not None],
            "rejected": sum(1 for p in cleaned_postings if p is None),
            "submitted": len(postings),
        })
    client = body.get("client") if isinstance(body.get("client"), dict) else {}
    return {
        "batch_id": batch_id,
        "client": {"name": _text(client.get("name"), 40),
                   "version": _text(client.get("version"), 40)},
        "observed_at": _text(body.get("observed_at"), 40),
        "boards": out_boards,
        "reads": reads,
        "postings_submitted": total_postings,
    }


# ---------------------------------------------------------------------------
# Rate limits
# ---------------------------------------------------------------------------

def _rate_limit(db, email: str, postings: int, now: datetime) -> None:
    """30 batches an hour and 5,000 postings a UTC day, per account.

    Each counter is claimed with one atomic `$inc` and handed back when the
    claim goes over, so concurrent batches cannot each read "under the limit"
    and all pass."""
    from pymongo import ReturnDocument

    hour = now.strftime("%Y%m%d%H")
    day = now.strftime("%Y%m%d")
    hour_end = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    day_end = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    day_key = {"_id": f"{email}|d|{day}"}
    if postings > 0:
        daily = db[USAGE].find_one_and_update(
            day_key,
            {"$inc": {"postings": int(postings)},
             "$setOnInsert": {"expire_at": day_end + timedelta(days=2)}},
            upsert=True, return_document=ReturnDocument.AFTER)
        if int((daily or {}).get("postings") or 0) > POSTINGS_PER_DAY:
            db[USAGE].update_one(day_key, {"$inc": {"postings": -int(postings)}})
            raise RadarError(429, "rate_limited", "Daily posting limit reached.",
                             retry_after=max(1, int((day_end - now).total_seconds())))
    doc = db[USAGE].find_one_and_update(
        {"_id": f"{email}|h|{hour}"},
        {"$inc": {"batches": 1}, "$setOnInsert": {"expire_at": hour_end + timedelta(days=1)}},
        upsert=True, return_document=ReturnDocument.AFTER)
    if int((doc or {}).get("batches") or 0) > BATCHES_PER_HOUR:
        if postings > 0:
            db[USAGE].update_one(day_key, {"$inc": {"postings": -int(postings)}})
        raise RadarError(429, "rate_limited", "Too many batches this hour.",
                         retry_after=max(1, int((hour_end - now).total_seconds())))


# ---------------------------------------------------------------------------
# Mapping: Workday's raw fields -> the crawler's rows
# ---------------------------------------------------------------------------

_SCRIPT_BLOCK_RE = re.compile(r"<(script|style|noscript)\b[^>]*>.*?</\1\s*>",
                              re.IGNORECASE | re.DOTALL)


def flatten_body(html: Any) -> str:
    """A posting body as the plain text the crawl stores.

    The converter is the crawl's own (`workday_cxs_fetcher._detail_html_to_text`,
    the one `fetch_detail` runs on a Workday `jobDescription`), cut to the same
    length, so the same body gives the same text, the same `content_hash` and
    the same requirements whichever side read it.

    One deliberate difference: `<script>`, `<style>` and `<noscript>` blocks
    are removed first. Workday's own bodies never hold one, so a real body is
    unchanged; a hostile sender's script text is not stored as prose."""
    from services.workday_cxs_fetcher import _detail_html_to_text

    raw = str(html or "")
    if not raw:
        return ""
    return _detail_html_to_text(_SCRIPT_BLOCK_RE.sub(" ", raw))[:DETAIL_KEEP_CHARS]


def batch_clock(observed_at: Any, now: datetime) -> datetime:
    """The instant a batch's relative dates are read against.

    The batch's own `observed_at`, when it parses, is not in the future and
    is at most `OBSERVED_AT_MAX_AGE_DAYS` old. Otherwise the server clock."""
    w = _worker()
    seen = w._parse_iso(observed_at) if isinstance(observed_at, str) and observed_at else None
    if seen is None or seen > now or seen < now - timedelta(days=OBSERVED_AT_MAX_AGE_DAYS):
        return now
    return seen.astimezone(timezone.utc)


def _workday_apply_base(member: Dict[str, Any]) -> Tuple[str, str]:
    """`(apply base, site)` for a Workday member.

    On `myworkdayjobs.com` this is the string `_list_workday` builds, from
    the member's own fields. That path cannot read a `myworkdaysite.com`
    board at all, so there the provider's form is used: such a board's rows
    can only ever come from a browser."""
    w = _worker()
    provider = _provider()
    endpoint = w._endpoint_from_member(member)
    if provider._domain_of(endpoint) == _DOMAINS[0]:
        tenant = str(member.get("tenant") or endpoint.tenant)
        host = str(member.get("wd_host") or provider._wd_host_of(endpoint))
        site = str(member.get("site") or provider._site_of(endpoint))
        return f"https://{tenant}.{host}.{_DOMAINS[0]}", site
    return provider._apply_base(endpoint), provider._site_of(endpoint)


def workday_row(member: Dict[str, Any], posting: Dict[str, Any], *,
                cutoff: datetime, clock: datetime,
                now: datetime) -> Tuple[Optional[Dict[str, Any]], str]:
    """One raw CXS posting as the listing row the PRODUCTION crawl builds.

    This mirrors the loop body of `job_ingest_worker._list_workday` line for
    line (Workday is in `_LEGACY_LISTING_VENDORS`, so that is the path the
    crawl runs): the same relative-date reader, the same skip of a row with
    no readable date, the same requisition id, apply link and bare-path
    detail address, through the same `_listing_row`.
    `test_browser_row_equals_the_crawler_row` drives the real function and
    holds the two equal.

    Returns `(row, "")`, or `(None, why)` with `why` one of `untitled`,
    `undated`, `aged`, `no_id`. `clock` dates the relative text; a date that
    lands after `now` is not believed and reads as undated."""
    from services.workday_cxs_fetcher import _approx_iso_from_relative
    w = _worker()

    title = str(posting.get("title") or "").strip()
    if not title:
        return None, "untitled"
    posted_text = str(posting.get("postedOn") or "").strip()
    posted_at = w._parse_iso(_approx_iso_from_relative(posted_text, now=clock))
    if posted_at is None or posted_at > now:
        # ADR-001, as in the crawl: an unknown date is never "today".
        return None, "undated"
    if posted_at < cutoff:
        return None, "aged"
    path = str(posting.get("externalPath") or "").lstrip("/")
    req_id = (posting.get("bulletFields") or [None])[0] or path
    if not req_id:
        return None, "no_id"
    base, site = _workday_apply_base(member)
    return w._listing_row(
        member, req_id,
        title=title,
        url=f"{base}/{site}/{path}" if path else base,
        posted_at=posted_at,
        location=str(posting.get("locationsText") or "").strip(),
        employment_type=str(posting.get("timeType") or "").strip(),
        posted_text=posted_text,
        detail_ref=path,
    ), ""


def map_posting(member: Dict[str, Any], posting: Dict[str, Any], *,
                cutoff: datetime, clock: Optional[datetime] = None,
                now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
    """`workday_row` without the reason (a listing row or None)."""
    now = now or _utcnow()
    return workday_row(member, posting, cutoff=cutoff, clock=clock or now, now=now)[0]


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------

def _record_unknown(db, board: Dict[str, Any], email: str, now: datetime) -> None:
    key = "|".join((board["domain"], board["wd_host"], board["tenant"].lower(),
                    board["site"].lower()))
    try:
        db[UNKNOWN_BOARDS].update_one(
            {"_id": key},
            {"$set": {"tenant": board["tenant"], "site": board["site"],
                      "wd_host": board["wd_host"], "domain": board["domain"],
                      "last_seen_at": now},
             "$setOnInsert": {"first_seen_at": now},
             "$inc": {"sightings": 1, "postings": len(board["postings"])},
             "$addToSet": {"reported_by": email}},
            upsert=True)
    except Exception as exc:  # pragma: no cover - discovery is best effort
        logger.warning("radar: unknown-board record failed (%s)", type(exc).__name__)


def _empty_counts() -> Dict[str, Any]:
    return {
        "boards": {"accepted": 0, "unknown": 0},
        "postings": {"accepted": 0, "rejected": 0, "created": 0, "updated": 0,
                     "touched": 0, "reopened": 0, "aged_out": 0, "held": 0},
        "would": {"created": 0, "updated": 0, "touched": 0},
    }


def ingest_batch(email: str, body: Any, *, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Validate, gate, and write (or, in dry run, count) one browser batch.

    Raises `RadarError` for every refusal the contract names."""
    now = now or _utcnow()
    mode = ingest_mode()
    if mode == MODE_OFF or not enabled():
        raise RadarError(409, "ingest_off", "Browser listings are switched off.")
    if not is_contributor(email):
        raise RadarError(403, "not_contributor",
                         "This account does not contribute listings.")
    batch = validate_batch(body)

    db = _db()
    _ensure_indexes(db)
    batch_key = f"{email}|{batch['batch_id']}"
    prior = db[BATCHES].find_one({"_id": batch_key}, {"response": 1})
    if prior and isinstance(prior.get("response"), dict):
        return prior["response"]

    _rate_limit(db, email, batch["postings_submitted"], now)

    written: List[Dict[str, str]] = []
    boards_read: List[Dict[str, Any]] = []
    response = _process(db, email, batch, mode=mode, now=now, written=written,
                        boards_read=boards_read)
    if mode == MODE_ON:
        _complete_leases(db, email, batch, boards_read, response, now)
    posted = response["postings"]
    logger.info(
        "radar: batch accepted mode=%s boards=%d unknown=%d sent=%d created=%d "
        "updated=%d touched=%d reopened=%d held=%d aged_out=%d rejected=%d",
        mode, response["boards"]["accepted"], response["boards"]["unknown"],
        batch["postings_submitted"], posted["created"], posted["updated"],
        posted["touched"], posted["reopened"], posted["held"], posted["aged_out"],
        posted["rejected"])
    try:
        db[BATCHES].update_one(
            {"_id": batch_key},
            {"$setOnInsert": {
                "user_email": email, "batch_id": batch["batch_id"],
                "client": batch["client"], "observed_at": batch["observed_at"],
                "mode": mode, "received_at": now,
                "boards": [{k: b[k] for k in ("tenant", "site", "wd_host", "domain",
                                               "search_text", "submitted")}
                           for b in batch["boards"]],
                "response": response,
                "sent": batch["postings_submitted"],
                # Which postings this batch wrote or refreshed, and how, so one
                # account's rows can be found again.
                "written": written[:LEDGER_MAX_IDS],
                "written_truncated": len(written) > LEDGER_MAX_IDS,
                "expire_at": now + timedelta(days=BATCH_KEEP_DAYS),
            }},
            upsert=True)
    except Exception as exc:  # pragma: no cover - the audit must not fail a write
        logger.warning("radar: batch record failed (%s)", type(exc).__name__)
    return response


def _complete_leases(db, email: str, batch: Dict[str, Any],
                     boards_read: List[Dict[str, Any]], response: Dict[str, Any],
                     now: datetime) -> None:
    """`radar-2`: tell the board ledger what this written batch read.

    A known board read with no search term is a read of that board when the
    uploading account holds its lease, and only then (`record_reads`). A read
    with a term is a search of it and moves nothing. `reads` names boards the
    browser could not read. The response gains a `reads` count only when the
    batch sent `reads`, so a `radar-1` answer is unchanged."""
    reads = batch.get("reads") or []
    termless = [b for b in boards_read if not b["search_text"]]
    if not termless and not reads:
        return
    try:
        from services import browser_radar_fleet as fleet
        if reads:
            _, by_address = _index_members()
            for read in reads:
                address = (read["domain"], read["wd_host"], read["tenant"].lower(),
                           read["site"].lower())
                read["key"] = "|".join(address) if address in by_address else None
        counts = fleet.record_reads(db, email, termless, reads, now=now)
        if reads:
            response["reads"] = {"recorded": counts["recorded"],
                                 "ignored": counts["ignored"]}
    except Exception as exc:  # pragma: no cover - the ledger must not fail a write
        logger.warning("radar: lease completion failed (%s)", type(exc).__name__)


def _decide(existing: Optional[Dict[str, Any]], row: Dict[str, Any],
            has_body: bool, guard: Optional[Dict[str, Any]] = None) -> str:
    """What a browser sighting may do with this row, given what we hold.

    `created` (new), `reopened` (held but closed: a live listing is proof it
    is open), `held` (closed because its own page said so: left closed),
    `touched` (held and open: refreshed as still live), `reread` (as touched,
    but the stored body is owed the current reader), else `updated` (held,
    open, no fetched body yet, and the browser brings one).

    A batch is untrusted, so this is narrower than the crawl's triage: once a
    posting has a fetched body, a browser sighting only ever says "still
    listed". It cannot replace that body or change the row's fields."""
    w = _worker()
    if not existing:
        return "created"
    if existing.get("status") != "active":
        if str((guard or {}).get("closed_reason") or "") in NO_REOPEN_REASONS:
            return "held"
        return "reopened"
    if has_body and existing.get("body_state") != w.BODY_FULL:
        return "updated"
    if (not w._listing_implies_same_content(existing, row)
            and w._only_reader_is_stale(existing, row)):
        return "reread"
    return "touched"


def _guard_fields(db, source: str, ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """The stored fields a browser sighting must not change, for held rows."""
    out: Dict[str, Dict[str, Any]] = {}
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        try:
            cursor = db["job_postings"].find(
                {"source": source, "source_job_id": {"$in": chunk}},
                {"_id": 0, "source_job_id": 1, "closed_reason": 1, "title_raw": 1,
                 "canonical_url": 1, "detail_ref": 1})
            for doc in cursor:
                out[str(doc.get("source_job_id"))] = doc
        except Exception as exc:  # pragma: no cover - then nothing is changed
            logger.warning("radar: guard read failed (%s)", type(exc).__name__)
            return {i: {"unreadable": True} for i in ids}
    return out


def _process(db, email: str, batch: Dict[str, Any], *, mode: str,
             now: datetime,
             written: Optional[List[Dict[str, str]]] = None,
             boards_read: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    from services import job_inventory_service as inv
    w = _worker()

    written = written if written is not None else []
    boards_read = boards_read if boards_read is not None else []
    counts = _empty_counts()
    _, by_address = _index_members()
    fresh_cutoff = now - timedelta(days=max(0, int(w.FRESHNESS_BOUNDARY_DAYS)))
    early_cutoff = min(fresh_cutoff,
                       now - timedelta(days=max(0, int(inv.EARLY_CAREER_MAX_AGE_DAYS))))
    # "Posted 3 Days Ago" is counted back from when the browser READ the
    # board, not from when the batch arrived: a parked batch can be a day late.
    clock = batch_clock(batch.get("observed_at"), now)

    # Map every posting, deduped by its id across boards and search terms
    # (the same posting under two terms is one sighting; keep a detail if
    # either carried one).
    rows: Dict[str, List[Any]] = {}
    order: List[str] = []
    for board in batch["boards"]:
        counts["postings"]["rejected"] += board["rejected"]
        address = (board["domain"], board["wd_host"], board["tenant"].lower(),
                   board["site"].lower())
        member = by_address.get(address)
        if member is None:
            counts["boards"]["unknown"] += 1
            # Never ingested; remembered so discovery can look at it.
            counts["postings"]["rejected"] += len(board["postings"])
            _record_unknown(db, board, email, now)
            continue
        counts["boards"]["accepted"] += 1
        # For the board ledger (`radar-2`): which known boards this batch read.
        try:
            known = _board_of(member)
            boards_read.append({
                "key": "|".join(address), "search_text": board["search_text"],
                "postings": board["submitted"],
                "board": dict(known, stats_tenant=w._member_tenant(member).lower())})
        except Exception:  # pragma: no cover - a malformed catalog row
            pass
        details = {d["externalPath"].lstrip("/"): d for d in board["details"]}
        for posting in board["postings"]:
            row, why = workday_row(member, posting, cutoff=early_cutoff,
                                   clock=clock, now=now)
            if row is None:
                # A row with no readable date is skipped, as the crawl skips
                # it. It is counted with the rejected ones.
                counts["postings"]["aged_out" if why == "aged" else "rejected"] += 1
                continue
            detail = details.get(posting["externalPath"].lstrip("/"))
            sjid = row["source_job_id"]
            if sjid in rows:
                if detail is not None and rows[sjid][2] is None:
                    rows[sjid][2] = detail
                continue
            rows[sjid] = [member, row, detail]
            order.append(sjid)

    entries: List[Dict[str, Any]] = []
    for sjid in order:
        member, row, detail = rows[sjid]
        body = flatten_body(detail["jobPostingInfo"]["jobDescription"]) if detail else ""
        entries.append({"member": member, "row": row, "body": body, "full": bool(body)})
    if entries:
        _write_rows(db, w.SOURCE_WORKDAY, entries, mode=mode, now=now, counts=counts,
                    written=written, fresh_cutoff=fresh_cutoff)
    return {"batch_id": batch["batch_id"], "mode": mode, **counts}


def _write_rows(db, src: str, entries: List[Dict[str, Any]], *, mode: str,
                now: datetime, counts: Dict[str, Any],
                written: List[Dict[str, str]], fresh_cutoff: datetime,
                admit_age: bool = True) -> Dict[str, str]:
    """The ONE guarded write for everything a browser reports.

    A Radar batch and a single posting (`radar-3`) both end here, so both get
    the same triage (`_decide`), the same guard on a held posting's title,
    link and detail address, the same `origin` label, the same ledger and the
    crawl's own way of reading requirements.

    `entries`: `{member, row, body, full}`, one per posting of source `src`.
    `row` is a listing row (`_listing_row`). `body` is the flattened text the
    browser brought, and `full` says whether it is a whole description or
    only a teaser.

    Returns what was decided per `source_job_id`: `created`, `updated`,
    `reopened`, `touched`, `held`, `aged_out` or `rejected`. In a dry run the
    decision is returned and nothing is written."""
    from services import job_inventory_service as inv
    w = _worker()

    decided: Dict[str, str] = {}
    order = [e["row"]["source_job_id"] for e in entries]
    by_id = {e["row"]["source_job_id"]: e for e in entries}
    existing_by_id = inv.get_postings_map(src, order)
    if admit_age:
        pairs = [(by_id[s]["member"], by_id[s]["row"]) for s in order]
        kept, aged = w._admit_by_age(pairs, existing_by_id, fresh_cutoff=fresh_cutoff)
        counts["postings"]["aged_out"] += aged
        kept_ids = {row["source_job_id"] for _m, row in kept}
    else:
        kept_ids = set(order)
    guards = _guard_fields(db, src, [i for i in order if i in existing_by_id])

    touch_ids: List[str] = []
    reread: List[Tuple[Dict[str, Any], str]] = []
    writes: List[Tuple[Dict[str, Any], str]] = []
    for sjid in order:
        if sjid not in kept_ids:
            decided[sjid] = "aged_out"
            continue
        entry = by_id[sjid]
        row = entry["row"]
        counts["postings"]["accepted"] += 1
        existing = existing_by_id.get(sjid)
        guard = guards.get(sjid) or {}
        if existing and guard.get("unreadable"):
            # What must be kept could not be read, so nothing is changed.
            decision = "held"
        else:
            decision = _decide(existing, row, bool(entry["body"] and entry["full"]), guard)
        if decision == "held":
            counts["postings"]["accepted"] -= 1
            counts["postings"]["held"] += 1
            decided[sjid] = "held"
        elif decision in ("touched", "reread"):
            counts["would"]["touched"] += 1
            touch_ids.append(sjid)
            decided[sjid] = "touched"
            if decision == "reread":
                reread.append((existing, str(row.get("title") or "")))
        elif decision == "created":
            counts["would"]["created"] += 1
            writes.append((entry, decision))
            decided[sjid] = "created"
        else:
            counts["would"]["updated"] += 1
            writes.append((entry, decision))
            decided[sjid] = decision

    if mode != MODE_ON:
        return decided

    # --- on: the crawl's own writes, and nothing that closes ---------------
    for start in range(0, len(touch_ids), 500):
        inv.touch_seen(src, touch_ids[start:start + 500], now=now)
    counts["postings"]["touched"] += len(touch_ids)
    written.extend({"id": i, "did": "touched"} for i in touch_ids)

    for entry, decision in writes:
        member, row, body = entry["member"], entry["row"], entry["body"]
        full = bool(body and entry["full"])
        payload = dict(row)
        detail_ref = str(payload.pop("_detail_ref", None) or "")
        if detail_ref:
            payload["detail_ref"] = detail_ref
        payload.pop("_member_key", None)
        # A held row keeps its own title, link and detail address: the browser
        # may add a body or show the posting is listed, nothing else.
        if existing_by_id.get(row["source_job_id"]):
            guard = guards.get(row["source_job_id"]) or {}
            for field, stored in (("title", guard.get("title_raw")),
                                  ("url", guard.get("canonical_url")),
                                  ("detail_ref", guard.get("detail_ref"))):
                if stored:
                    payload[field] = stored
        if body:
            payload["description"] = body
        if full:
            payload["body_state"] = w.BODY_FULL
            payload["detail_revision"] = w._detail_revision(member)
        else:
            payload["body_state"] = w.BODY_TEASER
        sjid = row["source_job_id"]
        try:
            outcome = inv.upsert_posting(payload, now=now)
        except ValueError as exc:
            counts["postings"]["rejected"] += 1
            counts["postings"]["accepted"] -= 1
            decided[sjid] = "rejected"
            logger.warning("radar: write refused for %s (%s)", str(sjid)[:60], exc)
            continue
        if outcome.get("created"):
            counts["postings"]["created"] += 1
            did = "created"
            try:
                db["job_postings"].update_one(
                    {"source": src, "source_job_id": sjid},
                    {"$set": {"origin": ORIGIN_BROWSER}})
            except Exception as exc:  # pragma: no cover - a label, not the write
                logger.warning("radar: origin label failed (%s)", type(exc).__name__)
        elif outcome.get("reopened") or decision == "reopened":
            counts["postings"]["reopened"] += 1
            did = "reopened"
        else:
            counts["postings"]["updated"] += 1
            did = "updated"
        decided[sjid] = did
        written.append({"id": sjid, "did": did})

        if outcome.get("kept_stored_body"):
            prior = existing_by_id.get(sjid)
            if outcome.get("needs_parse") and prior:
                reread.append((dict(prior, content_hash=outcome.get("content_hash")),
                               str(row.get("title") or "")))
            continue
        if outcome.get("needs_parse") and body:
            reusable = inv.find_parsed_requirements(outcome.get("content_hash"))
            if reusable and inv.mark_requirements_parsed(
                    src, sjid, outcome.get("content_hash"), reusable, now=now):
                continue
            w._persist_requirements(src, sjid, outcome.get("content_hash") or "",
                                    body, str(row.get("title") or ""), now=now)

    if reread:
        w._reread_stored(src, reread, deadline=time.monotonic() + 15.0, stop=None,
                         counts=collections.Counter(), now=now)
    return decided


# ---------------------------------------------------------------------------
# What was received (the extension's own line, and Admin)
# ---------------------------------------------------------------------------

_SUM_FIELDS = ("created", "updated", "touched", "reopened", "held", "aged_out", "rejected")


def _iso(value: Any) -> Optional[str]:
    value = _aware(value)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if value else None


def _upload_groups(db, match: Dict[str, Any], group_id: Any) -> List[Dict[str, Any]]:
    group: Dict[str, Any] = {
        "_id": group_id, "batches": {"$sum": 1},
        "sent": {"$sum": {"$ifNull": ["$sent", {"$sum": "$boards.submitted"}]}},
        "boards": {"$sum": "$response.boards.accepted"},
        "unknown_boards": {"$sum": "$response.boards.unknown"},
        "last_at": {"$max": "$received_at"},
    }
    for field in _SUM_FIELDS + ("staged",):
        group[field] = {"$sum": {"$ifNull": [f"$response.postings.{field}", 0]}}
    return list(db[BATCHES].aggregate([{"$match": match}, {"$group": group}],
                                      maxTimeMS=15000))


def _upload_row(doc: Dict[str, Any]) -> Dict[str, Any]:
    row = {"batches": int(doc.get("batches") or 0), "sent": int(doc.get("sent") or 0),
           "boards": int(doc.get("boards") or 0),
           "unknown_boards": int(doc.get("unknown_boards") or 0),
           "last_at": _iso(doc.get("last_at"))}
    for field in _SUM_FIELDS:
        row[field] = int(doc.get(field) or 0)
    # `radar-3`: single postings the registry could not place. Staged, so
    # not part of `recorded`.
    row["staged"] = int(doc.get("staged") or 0)
    # What reached the shared list: written new, given a body, or kept alive.
    row["recorded"] = row["created"] + row["updated"] + row["touched"] + row["reopened"]
    return row


def uploads_today(email: str, *, now: Optional[datetime] = None) -> Dict[str, Any]:
    """This account's own received batches for the current UTC day. A single
    posting (`radar-3`) is one batch row of its own, so it is counted here.

    `available: false` means the count could not be read, which is not zero."""
    now = now or _utcnow()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        docs = _upload_groups(_db(), {"user_email": email, "mode": MODE_ON,
                                      "received_at": {"$gte": start}}, None)
    except Exception as exc:
        logger.warning("radar: uploads read failed (%s)", type(exc).__name__)
        return {"available": False, "day": start.strftime("%Y-%m-%d")}
    row = _upload_row(docs[0] if docs else {})
    return {"available": True, "day": start.strftime("%Y-%m-%d"), **row}


def uploads_summary(days: int = 7, *, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Admin: received batches per account and UTC day, newest first, plus the
    boards the registry does not know. Batch rows live 30 days."""
    now = now or _utcnow()
    try:
        days = int(days)
    except (TypeError, ValueError):
        days = 7
    days = max(1, min(BATCH_KEEP_DAYS, days))
    start = (now - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    out: Dict[str, Any] = {"generated_at": _iso(now), "days": days,
                           "contributor_scope": contributor_scope(),
                           "ingest_mode": ingest_mode(), "enabled": enabled()}
    # `radar-2`: the stored coverage measurement (with its age) and the fleet
    # as it is now. Each reads on its own and says so when it cannot.
    from services import browser_radar_fleet as fleet
    out["coverage"] = fleet.coverage_block(now=now)
    out["fleet"] = fleet.fleet_block(now=now)
    # `radar-3`: how much is staged, by host. Counts only: no reporter.
    from services import browser_radar_posting as posting
    out.update(posting.staged_counts())
    try:
        db = _db()
        docs = _upload_groups(
            db, {"received_at": {"$gte": start}},
            {"email": "$user_email", "mode": "$mode",
             "day": {"$dateToString": {"format": "%Y-%m-%d", "date": "$received_at"}}})
        rows = []
        for doc in docs:
            key = doc.get("_id") or {}
            rows.append({"email": key.get("email"), "mode": key.get("mode"),
                         "day": key.get("day"), **_upload_row(doc)})
        rows.sort(key=lambda r: (r["day"] or "", r["last_at"] or ""), reverse=True)
        unknown = [
            {"tenant": d.get("tenant"), "site": d.get("site"), "wd_host": d.get("wd_host"),
             "domain": d.get("domain"), "sightings": int(d.get("sightings") or 0),
             "postings": int(d.get("postings") or 0),
             "reporters": len(d.get("reported_by") or []),
             "last_seen_at": _iso(d.get("last_seen_at"))}
            for d in db[UNKNOWN_BOARDS].find({}).sort("sightings", -1).limit(25)]
    except Exception as exc:
        logger.warning("radar: uploads summary failed (%s)", type(exc).__name__)
        out["available"] = False
        return out
    out.update({"available": True, "rows": rows, "unknown_boards": unknown,
                "accounts": len({r["email"] for r in rows})})
    return out
