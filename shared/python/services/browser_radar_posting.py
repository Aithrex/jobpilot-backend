"""Browser Radar, one posting from any job page (`radar-3`).

When a person who opted in opens a single job posting on any ATS, the
ApplyPilot extension tells Aspirely about it, so people with similar titles
see it. Two calls:

* `lookup_posting` (`POST .../posting/lookup`): do we hold this posting, and
  what would be useful to send (`nothing`, a bare `sighting`, or the `body`).
* `ingest_posting` (`POST .../posting`): the posting itself.

Any signed-in account may send, so everything the client says is untrusted.
The rules, in the order they bind (contract: `docs/specs/browser-radar.md`,
section `radar-3`):

* **Identity comes from the URL, resolved here.** The provider registry
  (`ats_providers.registry.match_url`) names the vendor, and the registry
  member names the board and the employer. The posting's raw id is read from
  the URL by the vendor's own `posting_id_from_url`. Nothing in the body can
  choose a tenant, an employer, an apply link or a `source_job_id`.
* **The link is rebuilt.** The stored apply link comes from the member and
  the id (`posting_links`). A vendor with no builder keeps the page URL
  without its query and fragment, and only on that vendor's own host.
* **A known board is written through the batch's own guarded path**
  (`browser_radar_service._write_rows`): the same triage, the same guard on a
  held posting, the same `origin` label and ledger. A held posting is rebuilt
  from what we STORED (`_row_from_stored`), so a sighting can show it is
  still open or add a body to a teaser, and can change nothing else.
* **Anything else is staged, never written.** An unknown board, or a
  company's own page with a schema.org `JobPosting`, goes to
  `browser_radar_staged_postings` as `observed`. A second account reporting
  the same content moves it to `corroborated`. No feed reads that collection.
* **No request leaves the server.** The page is not fetched again, so a body
  is the sender's word. That is the open trust gap; the staged states are
  the path to closing it.

Reporters appear only as the salted pseudonym `browser_radar_fleet` uses.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import math
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit

from services import browser_radar_service as radar
from services.browser_radar_service import RadarError

logger = logging.getLogger(__name__)

POSTING_VERSION = "radar-3"

STAGED = radar.STAGED

# Request bounds. Past any of them the request is `400 invalid_request`.
MAX_BODY_BYTES = 300 * 1024
MAX_LOOKUP_BYTES = 8 * 1024
MAX_URL_CHARS = 2000
MAX_DESCRIPTION_CHARS = radar.MAX_DESCRIPTION_CHARS
MAX_JSON_LD_BYTES = 100 * 1024

#: A description with at least this much text is a body. Less is a teaser.
FULL_BODY_CHARS = 200

# Per account. Fixed windows: a clock hour and a UTC day.
LOOKUPS_PER_HOUR = 120
POSTINGS_PER_HOUR = 60
POSTINGS_PER_DAY = 300

#: A held posting seen this recently, with a full body, needs nothing.
FRESH_HOURS = 12.0

STAGED_KEEP_DAYS = 30
STAGED_EXCERPT_CHARS = 2000
#: Pseudonyms kept on one staged row. Corroboration needs two.
STAGED_MAX_REPORTERS = 50
STATE_OBSERVED = "observed"
STATE_CORROBORATED = "corroborated"

WANT_NOTHING = "nothing"
WANT_SIGHTING = "sighting"
WANT_BODY = "body"

#: Query parameters that name ONE job on some site. Every other parameter is
#: dropped before anything is matched or stored: tracking tags, session
#: tokens and candidate ids never reach the database.
_KEPT_QUERY = frozenset({
    "gh_jid", "ashby_jid", "jobid", "job_id", "job", "jid", "reqid", "req_id",
    "id", "career_job_req_id", "company", "jk", "pid",
})
_QUERY_VALUE_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,80}$")
_HOST_RE = re.compile(r"^[a-z0-9](?:[a-z0-9.\-]{0,251}[a-z0-9])?$")
#: Names that never resolve on the public internet.
_PRIVATE_SUFFIXES = (".localhost", ".local", ".internal", ".intranet", ".lan",
                     ".home", ".corp", ".test", ".invalid", ".example", ".onion")
_RAW_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,119}$")

#: Vendors whose crawled row carries an employment type. For the others the
#: crawl stores none, so a browser row stores none either (it feeds the
#: content hash).
_EMPLOYMENT_TYPE_VENDORS = frozenset({"workday", "ashby", "neogov", "icims",
                                      "oracle_orc", "bamboohr"})


def _invalid(message: str) -> RadarError:
    return RadarError(400, "invalid_request", message)


def _sanitize(value: Any, max_len: int) -> str:
    from utils.security import InputSanitizer
    return InputSanitizer.sanitize_string(value, max_len) if isinstance(value, str) else ""


# ---------------------------------------------------------------------------
# The URL
# ---------------------------------------------------------------------------

def _public_host(host: str) -> bool:
    """A hostname that can only be a public site. No DNS is asked: this is a
    shape test, and the server never fetches the URL anyway."""
    if not host or len(host) > 253 or not _HOST_RE.match(host):
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None:
        return bool(address.is_global)
    if host == "localhost" or host.endswith(_PRIVATE_SUFFIXES):
        return False
    labels = host.split(".")
    if len(labels) < 2 or any(not label for label in labels):
        return False
    # A numeric or hex last label is an address in disguise ("0x7f.0.0.1").
    last = labels[-1]
    return not (last.isdigit() or last.startswith("0x") or len(last) < 2)


def clean_url(raw: Any) -> Dict[str, str]:
    """The posting page URL, checked and reduced, or `RadarError(400)`.

    Kept: scheme, host (lowercased), port when it is not the default, path,
    and the few query parameters that name a job. Dropped: every other
    parameter and the fragment. Refused: anything that is not http(s), a
    URL with a user or password, and a private, loopback or unusable host."""
    if not isinstance(raw, str) or not raw.strip():
        raise _invalid("url must be a non-empty string")
    if len(raw) > MAX_URL_CHARS:
        raise _invalid(f"url is over {MAX_URL_CHARS} characters")
    text = _sanitize(raw, MAX_URL_CHARS)
    if not text or re.search(r"[\s\\<>\"'`{}|^]", text):
        raise _invalid("url has characters a URL cannot hold")
    try:
        parts = urlsplit(text)
        port = parts.port
        host = (parts.hostname or "").strip().rstrip(".").lower()
    except ValueError:
        raise _invalid("url is not a usable URL")
    scheme = (parts.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise _invalid("url must be http or https")
    if parts.username is not None or parts.password is not None or "@" in (parts.netloc or ""):
        raise _invalid("url must not carry a user or password")
    if not _ipv6_or_public(host):
        raise _invalid("url must name a public host")
    path = parts.path or "/"
    if ".." in path.split("/"):
        raise _invalid("url has a path that walks upward")
    kept = [(name.lower(), value) for name, value in parse_qsl(parts.query or "")
            if name.lower() in _KEPT_QUERY and _QUERY_VALUE_RE.match(value)]
    kept = sorted(dict(kept).items())
    netloc = f"[{host}]" if ":" in host else host
    if port and port != (443 if scheme == "https" else 80):
        netloc = f"{netloc}:{port}"
    url = f"{scheme}://{netloc}{path}"
    if kept:
        url = f"{url}?{urlencode(kept)}"
    return {"url": url, "scheme": scheme, "host": host, "path": path,
            "bare": f"https://{host}{path}"}


def _ipv6_or_public(host: str) -> bool:
    if ":" in host:
        try:
            return bool(ipaddress.ip_address(host).is_global)
        except ValueError:
            return False
    return _public_host(host)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

_BODY_STRINGS = (("title", 300), ("company", 200), ("location", 300),
                 ("employment_type", 80), ("date_posted", 40))


def validate_lookup(body: Any) -> Dict[str, str]:
    """`{"url": ...}` typed and reduced, or `RadarError(400)`."""
    if not isinstance(body, dict):
        raise _invalid("the body must be a JSON object")
    return clean_url(body.get("url"))


def _clean_json_ld(node: Any, depth: int = 0) -> Any:
    """A bounded copy of the page's JSON-LD that is safe to store: no key
    Mongo would read as an operator or a path, no deep nesting."""
    if depth > 8:
        return None
    if isinstance(node, dict):
        out: Dict[str, Any] = {}
        for key, value in list(node.items())[:80]:
            if not isinstance(key, str):
                continue
            name = _sanitize(key, 80)
            if not name or name.startswith("$") or "." in name:
                continue
            out[name] = _clean_json_ld(value, depth + 1)
        return out
    if isinstance(node, list):
        return [_clean_json_ld(value, depth + 1) for value in node[:50]]
    if isinstance(node, str):
        return _sanitize(node, 20000)
    if isinstance(node, bool) or node is None:
        return node
    if isinstance(node, int):
        return node if abs(node) < 2 ** 53 else None
    if isinstance(node, float):
        return node if math.isfinite(node) else None
    return None


def _is_job_posting(node: Any) -> bool:
    """Whether a JSON-LD object says it is a schema.org `JobPosting`."""
    if not isinstance(node, dict):
        return False
    kind = node.get("@type")
    kinds = kind if isinstance(kind, list) else [kind]
    for value in kinds:
        if isinstance(value, str) and value.strip().rsplit("/", 1)[-1] == "JobPosting":
            return True
    return False


def validate_posting(body: Any) -> Dict[str, Any]:
    """One posting, typed and bounded, or `RadarError(400)`.

    Only the contract's fields are read. Anything else in the body (cookies,
    a tenant, an apply link, a `source_job_id`) is never looked at."""
    if not isinstance(body, dict):
        raise _invalid("the body must be a JSON object")
    out: Dict[str, Any] = {"url": clean_url(body.get("url"))}
    for name, max_len in _BODY_STRINGS:
        value = body.get(name)
        if value is not None and not isinstance(value, str):
            raise _invalid(f"{name} must be a string")
        out[name] = _sanitize(value or "", max_len)
    description = body.get("description")
    if description is not None and not isinstance(description, str):
        raise _invalid("description must be a string")
    if isinstance(description, str) and len(description) > MAX_DESCRIPTION_CHARS:
        raise _invalid(f"description is over {MAX_DESCRIPTION_CHARS} characters")
    out["description"] = description or ""
    json_ld = body.get("json_ld")
    if json_ld is not None:
        if not isinstance(json_ld, dict):
            raise _invalid("json_ld must be an object")
        try:
            size = len(json.dumps(json_ld, ensure_ascii=False).encode("utf-8"))
        except (TypeError, ValueError):
            raise _invalid("json_ld is not usable JSON")
        if size > MAX_JSON_LD_BYTES:
            raise _invalid("json_ld is over 100 KB")
    out["json_ld"] = _clean_json_ld(json_ld) if _is_job_posting(json_ld) else None
    client = body.get("client") if isinstance(body.get("client"), dict) else {}
    out["client"] = {"name": radar._text(client.get("name"), 40),
                     "version": radar._text(client.get("version"), 40)}
    return out


# ---------------------------------------------------------------------------
# Gates and limits
# ---------------------------------------------------------------------------

def _gate(email: str) -> str:
    """The switches, in the contract's order. Returns the ingest mode."""
    mode = radar.ingest_mode()
    if mode == radar.MODE_OFF or not radar.enabled():
        raise RadarError(409, "ingest_off", "Browser listings are switched off.")
    if not radar.is_contributor(email):
        raise RadarError(403, "not_contributor",
                         "This account does not contribute listings.")
    return mode


def _take(db, key: str, field: str, cap: int, expire_at: datetime) -> bool:
    """Claim one unit of a counter with one atomic `$inc`, and hand it back
    when the claim went over (the `_rate_limit` idiom). True when it fit."""
    from pymongo import ReturnDocument

    doc = db[radar.USAGE].find_one_and_update(
        {"_id": key}, {"$inc": {field: 1}, "$setOnInsert": {"expire_at": expire_at}},
        upsert=True, return_document=ReturnDocument.AFTER)
    if int((doc or {}).get(field) or 0) > cap:
        db[radar.USAGE].update_one({"_id": key}, {"$inc": {field: -1}})
        return False
    return True


def _windows(now: datetime) -> Tuple[str, str, datetime, datetime]:
    hour_end = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    day_end = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    return now.strftime("%Y%m%d%H"), now.strftime("%Y%m%d"), hour_end, day_end


def _limit_lookup(db, email: str, now: datetime) -> None:
    hour, _day, hour_end, _day_end = _windows(now)
    if not _take(db, f"{email}|pl|{hour}", "lookups", LOOKUPS_PER_HOUR,
                 hour_end + timedelta(days=1)):
        raise RadarError(429, "rate_limited", "Too many lookups this hour.",
                         retry_after=max(1, int((hour_end - now).total_seconds())))


def _limit_posting(db, email: str, now: datetime) -> None:
    hour, day, hour_end, day_end = _windows(now)
    day_key = f"{email}|pd|{day}"
    if not _take(db, day_key, "postings", POSTINGS_PER_DAY, day_end + timedelta(days=2)):
        raise RadarError(429, "rate_limited", "Daily posting limit reached.",
                         retry_after=max(1, int((day_end - now).total_seconds())))
    if not _take(db, f"{email}|ph|{hour}", "postings", POSTINGS_PER_HOUR,
                 hour_end + timedelta(days=1)):
        # A refused request spends nothing: the day's unit goes back too.
        db[radar.USAGE].update_one({"_id": day_key}, {"$inc": {"postings": -1}})
        raise RadarError(429, "rate_limited", "Too many postings this hour.",
                         retry_after=max(1, int((hour_end - now).total_seconds())))


# ---------------------------------------------------------------------------
# Resolving a URL to a board and a posting
# ---------------------------------------------------------------------------

def _vendor_host(provider: Any, host: str) -> bool:
    """Whether `host` is one of this vendor's own hosts. A fingerprint is a
    search over the whole URL, so a vendor's address inside another site's
    query string would otherwise pass for that vendor."""
    for entry in tuple(getattr(provider, "portal_hosts", ()) or ()) + \
            tuple(getattr(provider, "api_hosts", ()) or ()):
        entry = str(entry or "").strip().lower()
        if not entry:
            continue
        if entry.startswith("."):
            if host.endswith(entry) or host == entry[1:]:
                return True
        elif host == entry:
            return True
    return False


_INDEX: Dict[str, Tuple[Any, Dict[Tuple[str, str, str], Dict[str, Any]],
                        Dict[str, List[Dict[str, Any]]]]] = {}


def _source_index(source: str):
    """`(by identity, by own host)` for one source's registry members. Built
    once per registry list (the worker caches that list for ten minutes)."""
    w = radar._worker()
    try:
        members = w.registry_members(source)
    except Exception as exc:  # pragma: no cover - then the board is unknown
        logger.warning("radar: registry unavailable for %s (%s)", source, type(exc).__name__)
        return {}, {}
    cached = _INDEX.get(source)
    if cached is not None and cached[0] is members:
        return cached[1], cached[2]
    by_identity: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    by_host: Dict[str, List[Dict[str, Any]]] = {}
    for member in members:
        try:
            by_identity.setdefault(w._identity_of(member), member)
        except Exception:  # pragma: no cover - a malformed registry row
            continue
        host = str(member.get("host") or "").strip().lower()
        if host and not host.startswith("."):
            by_host.setdefault(host, []).append(member)
    _INDEX[source] = (members, by_identity, by_host)
    return by_identity, by_host


_WORKDAY_INDEX: List[Any] = [None, {}]


def _workday_index() -> Dict[Tuple[str, str, str, str], Dict[str, Any]]:
    """Workday members by `(domain, wd_host, tenant, site)`, the address a
    batch is matched on (`_index_members`). Built once per registry list: a
    lookup is asked for every job page a person opens."""
    w = radar._worker()
    try:
        members = w.registry_members(w.SOURCE_WORKDAY)
    except Exception as exc:  # pragma: no cover - then the board is unknown
        logger.warning("radar: workday registry unavailable (%s)", type(exc).__name__)
        return {}
    if _WORKDAY_INDEX[0] is not members:
        _, by_address = radar._index_members()
        _WORKDAY_INDEX[0], _WORKDAY_INDEX[1] = members, by_address
    return _WORKDAY_INDEX[1]


def _member_for(provider: Any, endpoint: Any, host: str) -> Optional[Dict[str, Any]]:
    """The registry member this endpoint is, or None."""
    w = radar._worker()
    vendor = w._slug(provider.vendor, 60)
    if vendor == "workday":
        by_address = _workday_index()
        return by_address.get((provider._domain_of(endpoint),
                               provider._wd_host_of(endpoint).lower(),
                               str(endpoint.tenant or "").lower(),
                               provider._site_of(endpoint).lower()))
    by_identity, _ = _source_index(w.source_of_vendor(vendor))
    member = by_identity.get((vendor, str(endpoint.tenant or "").strip().lower(),
                              str(endpoint.site or "").strip().lower()))
    if member is None:
        return None
    own = str(member.get("host") or "").strip().lower()
    if own and not own.startswith(".") and own != host:
        # The same tenant label on another host is another board.
        return None
    return member


def _member_by_host(host: str) -> Optional[Dict[str, Any]]:
    """A board that lives on the employer's own host (a SuccessFactors
    career site, say), found by that host. Only when exactly one member
    claims the host: two boards on one host cannot be told apart by it."""
    w = radar._worker()
    found: List[Dict[str, Any]] = []
    for source in w.SOURCES:
        if source == w.SOURCE_WORKDAY:
            continue
        _, by_host = _source_index(source)
        found.extend(by_host.get(host) or [])
    return found[0] if len(found) == 1 else None


def resolve(clean: Dict[str, str]) -> Dict[str, Any]:
    """Who this URL belongs to, from the URL alone.

    `vendor`: the provider that owns the host ("" for none). `member`: the
    registry board, or None. `raw_id`: the posting's id as the crawl would
    store it ("" when the URL is not a single posting, or the vendor has no
    id rule). `apply_url`: the link to store, rebuilt here."""
    w = radar._worker()
    registry = w._ats_registry()
    out: Dict[str, Any] = {"vendor": "", "provider": None, "member": None, "board": None,
                           "raw_id": "", "apply_url": "", "detail_url": "", "path": "",
                           "has_rule": False}
    if registry is None:  # pragma: no cover - the registry always loads
        return out
    url, host = clean["url"], clean["host"]
    provider = endpoint = member = None
    hit = registry.match_url(url)
    if hit is not None and _vendor_host(hit[0], host):
        provider = hit[0]
        try:
            endpoint = provider.endpoint_from_match(hit[1], url)
        except Exception:  # pragma: no cover - a provider is defensive
            endpoint = None
        if endpoint is not None:
            member = _member_for(provider, endpoint, host)
    if member is None:
        own = _member_by_host(host)
        own_provider = registry.get(w._vendor_of(own)) if own is not None else None
        if own_provider is not None:
            provider, member = own_provider, own
    if provider is None:
        return out

    from services.ats_providers.base import AtsProvider
    out["vendor"] = str(provider.vendor)
    out["provider"] = provider
    out["has_rule"] = type(provider).posting_id_from_url is not AtsProvider.posting_id_from_url
    out["member"] = member
    board = w._endpoint_from_member(member) if member is not None else endpoint
    out["board"] = board
    if board is None or not out["has_rule"]:
        return out
    # The member's own endpoint first (the registry's spelling of the board),
    # then the one read from this URL: a catalog row may not carry the host
    # the page is served from.
    raw_id = apply_url = detail_url = ""
    try:
        for candidate in (board, endpoint):
            if candidate is None:
                continue
            raw_id = str(provider.posting_id_from_url(candidate, url) or "")
            if raw_id:
                board = candidate
                break
        if not raw_id or not _RAW_ID_RE.match(_sanitize(raw_id, 120)):
            return out
        apply_url, detail_url = provider.posting_links(board, raw_id, url)
    except Exception as exc:  # pragma: no cover - a provider is defensive
        logger.warning("radar: posting url not read (%s)", type(exc).__name__)
        return out
    out["board"] = board
    out["raw_id"] = raw_id
    # No builder: the page URL without its query and fragment. The host is
    # the vendor's own or the registry member's own, never a third site.
    out["apply_url"] = str(apply_url or clean["bare"])
    out["detail_url"] = str(detail_url or "")
    if out["vendor"] == "workday":
        out["path"] = provider.posting_path_from_url(board, url)
    return out


def _held(db, resolved: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], str]:
    """`(the stored posting or None, its source_job_id)` for a known board.

    First by the id the URL gives. Then, for a posting stored under ANOTHER
    id on the same board, by its canonical URL: a Workday URL ends with a
    requisition id that can differ from the one the crawl keyed on, and a
    second row for the same page would be a twin."""
    from services import job_inventory_service as inv

    member = resolved["member"]
    src = str(member["source"])
    prefix = f"{member['member_key']}:"
    sjid = prefix + resolved["raw_id"]
    doc = inv.get_posting(src, sjid)
    if doc:
        return doc, sjid
    key = inv.canonical_url_key(resolved["apply_url"])
    if not key:
        return None, sjid
    group = inv.compute_dedupe_identity(
        employer_id="", ats="", source=src, source_job_id=sjid,
        canonical_url=resolved["apply_url"], content_hash="",
        title_normalized="")["dedupe_group_id"]
    if not group:
        return None, sjid
    # `(status, dedupe_group_id)` is indexed; the three statuses are all
    # there are, so this is three index seeks and no scan.
    statuses = [inv.STATUS_ACTIVE, inv.STATUS_CLOSED, inv.STATUS_EXPIRED]
    for twin in db["job_postings"].find(
            {"status": {"$in": statuses}, "dedupe_group_id": group}).limit(10):
        twin_id = str(twin.get("source_job_id") or "")
        if twin.get("source") == src and twin.get("canonical_url_key") == key \
                and twin_id.startswith(prefix):
            return twin, twin_id
    return None, sjid


def _has_full_body(doc: Dict[str, Any]) -> bool:
    w = radar._worker()
    if doc.get("body_state") == w.BODY_FULL:
        return True
    if doc.get("body_state") == w.BODY_TEASER:
        return False
    # Written before `body_state` existed: judge it by its size.
    try:
        return int(doc.get("description_chars") or 0) >= w.FULL_LISTING_BODY_CHARS
    except (TypeError, ValueError):
        return False


def _job_id(doc: Optional[Dict[str, Any]], src: str, sjid: str) -> str:
    from services import job_inventory_service as inv
    return str((doc or {}).get("job_id") or inv.posting_job_id(src, sjid))


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------

def lookup_posting(email: str, body: Any, *, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Do we hold the posting this URL shows, and what is worth sending.

    Read-only apart from the hourly counter. Raises `RadarError` for every
    refusal the contract names."""
    from services import job_inventory_service as inv

    now = now or radar._utcnow()
    _gate(email)
    clean = validate_lookup(body)
    db = radar._db()
    radar._ensure_indexes(db)
    _limit_lookup(db, email, now)

    resolved = resolve(clean)
    out: Dict[str, Any] = {"supported": bool(resolved["raw_id"]), "vendor": resolved["vendor"],
                           "known_board": resolved["member"] is not None, "known": False,
                           "job_id": None, "want": WANT_NOTHING}
    if not out["supported"]:
        return out
    if resolved["member"] is None:
        out["want"] = WANT_BODY
        return out
    doc, sjid = _held(db, resolved)
    if doc is None:
        out["want"] = WANT_BODY
        return out
    out["known"] = True
    out["job_id"] = _job_id(doc, str(resolved["member"]["source"]), sjid)
    active = doc.get("status") == inv.STATUS_ACTIVE
    if not active and str(doc.get("closed_reason") or "") in radar.NO_REOPEN_REASONS:
        # Its own page said it was gone. A sighting would change nothing.
        return out
    if not _has_full_body(doc):
        out["want"] = WANT_BODY
        return out
    seen = radar._aware(doc.get("last_seen_at"))
    fresh = seen is not None and seen >= now - timedelta(hours=FRESH_HOURS)
    out["want"] = WANT_NOTHING if (active and fresh) else WANT_SIGHTING
    return out


# ---------------------------------------------------------------------------
# Rows: a held posting from what we stored, a new one in the crawl's shape
# ---------------------------------------------------------------------------

_INTERNAL_ROW_KEYS = ("_backlog", "_detail_misses", "_stored_revision", "_gone_seen_at",
                      "_restart_misses")


def _held_row(doc: Dict[str, Any], member: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """A held posting as the row that wrote it (`_row_from_stored`), so the
    sighting carries OUR title, link, place and date. The browser's words
    for them are not used."""
    w = radar._worker()
    row = w._row_from_stored(doc, member)
    if row is None:
        return None
    for key in _INTERNAL_ROW_KEYS:
        row.pop(key, None)
    return row


def _stated_date(text: str, now: datetime) -> Optional[datetime]:
    """The page's posting date. A date in the future is not believed."""
    w = radar._worker()
    if not re.match(r"^\d{4}-\d{2}-\d{2}", text or ""):
        return None
    stated = w._parse_iso(text)
    if stated is None or stated > now:
        return None
    return stated.astimezone(timezone.utc)


def _new_row(resolved: Dict[str, Any], posting: Dict[str, Any], body: str, *,
             now: datetime, cutoff: datetime) -> Tuple[Optional[Dict[str, Any]], str]:
    """A posting we do not hold, as the listing row THIS vendor's crawl
    builds, or `(None, why)`.

    The id, the board and the link are the server's. The title, the place,
    the date and the body are the page's, as the browser read them."""
    w = radar._worker()
    member = resolved["member"]
    vendor = w._vendor_of(member)
    raw_id = resolved["raw_id"]
    title = posting["title"].strip()
    if not title:
        return None, "no_title"
    posted_at = _stated_date(posting["date_posted"], now)
    employment = posting["employment_type"] if vendor in _EMPLOYMENT_TYPE_VENDORS else ""

    if member.get("source") == w.SOURCE_WORKDAY:
        # `_list_workday`'s row. It never writes an undated one.
        if posted_at is None:
            return None, "undated"
        if posted_at < cutoff:
            return None, "aged_out"
        base, site = radar._workday_apply_base(member)
        path = resolved["path"]
        return w._listing_row(
            member, raw_id, title=title, url=f"{base}/{site}/{path}",
            posted_at=posted_at, location=posting["location"],
            employment_type=employment, posted_text=posting["date_posted"],
            detail_ref=path), ""

    if w._uses_legacy_listing(member):
        # `_list_career`'s row. Each of its branches skips an undated one.
        if posted_at is None:
            return None, "undated"
        if posted_at < cutoff:
            return None, "aged_out"
        return w._listing_row(
            member, raw_id, title=title, url=resolved["apply_url"],
            posted_at=posted_at, location=posting["location"], description=body,
            employment_type=employment,
            detail_ref=raw_id if vendor == "workable" else ""), ""

    from services.ats_providers.base import DATE_EXACT, DATE_UNKNOWN, RawJob
    job = RawJob(
        source_job_id=raw_id, title=title, apply_url=resolved["apply_url"],
        location=posting["location"],
        posted_at=posted_at.isoformat() if posted_at else "",
        date_confidence=DATE_EXACT if posted_at else DATE_UNKNOWN,
        raw_posted_text=posting["date_posted"] if posted_at else "",
        description=body, employment_type=employment,
        detail_url=resolved["detail_url"])
    row = w._raw_job_to_listing(member, job, cutoff=cutoff)
    if row is None:
        return None, "aged_out"
    return row, ""


# ---------------------------------------------------------------------------
# Staging
# ---------------------------------------------------------------------------

def _stage(db, email: str, clean: Dict[str, str], posting: Dict[str, Any],
           resolved: Dict[str, Any], kind: str, body: str, now: datetime) -> str:
    """Record a posting the registry cannot place. Returns the staged key.

    Three small writes, each guarded by its own filter. The sighting first
    (an upsert), then the move to `corroborated` (only for a row another
    account reported, with the same content), then this account's pseudonym.
    """
    from services import browser_radar_fleet as fleet

    key = hashlib.sha256(clean["url"].encode("utf-8")).hexdigest()[:32]
    who = fleet.account_pseudonym(email)
    title = posting["title"]
    digest = hashlib.sha256("\x1f".join((
        re.sub(r"\s+", " ", title).strip().lower(),
        re.sub(r"\s+", " ", body).strip().lower())).encode("utf-8")).hexdigest()
    board = resolved.get("board")
    on_insert = {
        "url": clean["url"], "host": clean["host"], "vendor": resolved["vendor"],
        "kind": kind,
        "tenant": str(getattr(board, "tenant", "") or "")[:120],
        "site": str(getattr(board, "site", "") or "")[:120],
        "title": title, "company": posting["company"], "location": posting["location"],
        "excerpt": body[:STAGED_EXCERPT_CHARS], "content_hash": digest,
        "json_ld": posting["json_ld"], "first_seen_at": now,
        "state": STATE_OBSERVED, "reporters": [who] if who else [],
    }
    db[STAGED].update_one(
        {"_id": key},
        {"$set": {"last_seen_at": now, "expire_at": now + timedelta(days=STAGED_KEEP_DAYS)},
         "$inc": {"sightings": 1}, "$setOnInsert": on_insert},
        upsert=True)
    if who:
        db[STAGED].update_one(
            {"_id": key, "state": STATE_OBSERVED, "content_hash": digest,
             "reporters": {"$ne": who}},
            {"$set": {"state": STATE_CORROBORATED, "corroborated_at": now}})
        db[STAGED].update_one(
            {"_id": key, "reporters": {"$ne": who},
             f"reporters.{STAGED_MAX_REPORTERS - 1}": {"$exists": False}},
            {"$addToSet": {"reporters": who}})
    return key


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------

def _answer(result: str, job_id: Optional[str] = None, reason: str = "") -> Dict[str, Any]:
    return {"result": result, "job_id": job_id, "reason": reason}


_DECIDED = {"created": ("created", ""), "updated": ("updated", ""),
            "reopened": ("updated", "reopened"), "touched": ("touched", ""),
            "held": ("ignored", "closed"), "aged_out": ("ignored", "aged_out"),
            "rejected": ("ignored", "rejected")}


def ingest_posting(email: str, body: Any, *, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Validate, gate and record one posting a browser read.

    Raises `RadarError` for every refusal the contract names. In `dry_run`
    nothing is written to `job_postings` or to staging; the answer is
    `ignored` / `dry_run`, with what `on` would have done under `would`."""
    now = now or radar._utcnow()
    mode = _gate(email)
    posting = validate_posting(body)
    clean = posting["url"]
    db = radar._db()
    radar._ensure_indexes(db)
    _limit_posting(db, email, now)

    resolved = resolve(clean)
    member = resolved["member"]
    text = radar.flatten_body(posting["description"])
    counts = radar._empty_counts()
    counts["postings"]["staged"] = 0
    written: List[Dict[str, str]] = []
    staged_key = ""

    if member is not None and resolved["raw_id"]:
        counts["boards"]["accepted"] = 1
        answer = _known_board(db, resolved, posting, text, mode=mode, now=now,
                              counts=counts, written=written)
    else:
        if resolved["raw_id"]:
            kind = "unknown_board"
        elif resolved["has_rule"]:
            # The vendor's own host, and not one of its posting pages.
            kind = ""
        else:
            kind = "company_site" if posting["json_ld"] is not None else ""
        if not kind:
            counts["postings"]["rejected"] = 1
            answer = _answer("ignored", None,
                             "not_a_posting" if resolved["has_rule"] else "unsupported")
        else:
            counts["boards"]["unknown"] = 1 if kind == "unknown_board" else 0
            if mode == radar.MODE_ON:
                staged_key = _stage(db, email, clean, posting, resolved, kind, text, now)
                counts["postings"]["staged"] = 1
            answer = _answer("staged", None, kind)

    would = answer["result"]
    if mode != radar.MODE_ON:
        answer = dict(_answer("ignored", answer["job_id"], "dry_run"), would=would)
    logger.info("radar: posting mode=%s result=%s reason=%s vendor=%s known_board=%s",
                mode, would, answer["reason"] or "-", resolved["vendor"] or "-",
                member is not None)
    _record(db, email, posting, resolved, clean, mode=mode, now=now, counts=counts,
            written=written, staged_key=staged_key)
    return answer


def _known_board(db, resolved: Dict[str, Any], posting: Dict[str, Any], text: str, *,
                 mode: str, now: datetime, counts: Dict[str, Any],
                 written: List[Dict[str, str]]) -> Dict[str, Any]:
    from services import job_inventory_service as inv
    w = radar._worker()

    member = resolved["member"]
    src = str(member["source"])
    fresh_cutoff = now - timedelta(days=max(0, int(w.FRESHNESS_BOUNDARY_DAYS)))
    early_cutoff = min(fresh_cutoff,
                       now - timedelta(days=max(0, int(inv.EARLY_CAREER_MAX_AGE_DAYS))))
    full = len(text) >= FULL_BODY_CHARS
    doc, sjid = _held(db, resolved)
    if doc is not None:
        row = _held_row(doc, member)
        if row is None:  # pragma: no cover - a stored row its board cannot rebuild
            counts["postings"]["rejected"] += 1
            return _answer("ignored", _job_id(doc, src, sjid), "rejected")
        # A held posting keeps its own teaser text. Only a full body is new.
        entry = {"member": member, "row": row, "body": text if full else "", "full": full}
        admit_age = False
    else:
        row, why = _new_row(resolved, posting, text, now=now, cutoff=early_cutoff)
        if row is None:
            counts["postings"]["aged_out" if why == "aged_out" else "rejected"] += 1
            return _answer("ignored", None, why)
        sjid = row["source_job_id"]
        entry = {"member": member, "row": row, "body": text, "full": full}
        admit_age = True
    decided = radar._write_rows(db, src, [entry], mode=mode, now=now, counts=counts,
                                written=written, fresh_cutoff=fresh_cutoff,
                                admit_age=admit_age)
    result, reason = _DECIDED.get(decided.get(sjid, "rejected"), ("ignored", "rejected"))
    job_id = _job_id(doc, src, sjid) if (doc is not None or result != "ignored") else None
    return _answer(result, job_id, reason)


def _record(db, email: str, posting: Dict[str, Any], resolved: Dict[str, Any],
            clean: Dict[str, str], *, mode: str, now: datetime, counts: Dict[str, Any],
            written: List[Dict[str, str]], staged_key: str) -> None:
    """One batch row for this posting (`kind: "posting"`), so the account's
    "sent today" line and the admin table count it like a batch. The page
    URL is not kept here: the ledger names the posting, or the staged key."""
    token = uuid.uuid4().hex
    board = resolved.get("board")
    try:
        db[radar.BATCHES].insert_one({
            "_id": f"{email}|p|{token}", "user_email": email, "batch_id": f"p-{token}",
            "kind": "posting", "client": posting["client"], "observed_at": "",
            "mode": mode, "received_at": now,
            "boards": [{"vendor": resolved["vendor"], "host": clean["host"],
                        "tenant": str(getattr(board, "tenant", "") or "")[:120],
                        "site": str(getattr(board, "site", "") or "")[:120],
                        "search_text": "", "submitted": 1}],
            "response": {"batch_id": f"p-{token}", "mode": mode, **counts},
            "sent": 1, "written": written[:radar.LEDGER_MAX_IDS],
            "written_truncated": False, "staged_key": staged_key,
            "expire_at": now + timedelta(days=radar.BATCH_KEEP_DAYS),
        })
    except Exception as exc:  # pragma: no cover - the audit must not fail a write
        logger.warning("radar: posting record failed (%s)", type(exc).__name__)


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------

def staged_counts(db=None) -> Dict[str, Any]:
    """How much is staged: totals and the ten hosts with the most. No
    reporter, no email and no URL."""
    try:
        db = db if db is not None else radar._db()
        total = db[STAGED].count_documents({})
        corroborated = db[STAGED].count_documents({"state": STATE_CORROBORATED})
        hosts = [{"host": doc.get("_id"), "postings": int(doc.get("n") or 0)}
                 for doc in db[STAGED].aggregate([
                     {"$group": {"_id": "$host", "n": {"$sum": 1}}},
                     {"$sort": {"n": -1, "_id": 1}}, {"$limit": 10}], maxTimeMS=15000)]
    except Exception as exc:
        logger.warning("radar: staged counts failed (%s)", type(exc).__name__)
        # Not known is not zero.
        return {"staged_total": None, "staged_corroborated": None, "staged_by_host": None}
    return {"staged_total": int(total), "staged_corroborated": int(corroborated),
            "staged_by_host": hosts}
