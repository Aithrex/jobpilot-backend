"""Company logo pipeline — REAL favicons/brand icons only, cached in S3.

Every job card (Discover daily pipeline + guest matches) gets a ``logo_url``
pointing at ``GET /api/jobs/logo/<sha1(company)>``, which serves the company's
actual favicon from an S3 cache. We NEVER generate or synthesize logos — a
company with no fetchable icon simply 404s and the frontend renders a
monogram fallback.

Fetch ladder per resolved domain (server-side only, SSRF-guarded via
jd_fetcher's ``url_is_safe``/``_safe_get`` — public-IP-only, per-hop redirect
re-validation):

  1. PRIMARY: Google's s2 favicon service
     (``https://www.google.com/s2/favicons?domain=<domain>&sz=128``).
     When Google has no icon it still returns 200 with a small default globe
     PNG — detected WITHOUT Pillow by parsing the PNG IHDR header (width at
     bytes 16-20, big-endian): width < 32 → treated as a miss.
  2. SECONDARY (only when Google yields nothing usable): the site itself —
     ``/apple-touch-icon.png``, ``/apple-touch-icon-precomposed.png``, then a
     homepage HTML parse for ``<link rel="icon"|"shortcut icon"|
     "apple-touch-icon">`` (relative hrefs resolved, each fetch re-guarded).

Accepted content: png/jpeg/webp/gif/ico (ico also via magic bytes when
servers mislabel). SVG and everything else is rejected (XSS posture). Bytes
are capped at 512KB and served as-is (Pillow is NOT in the Lambda layer;
browsers render .ico inside <img> fine).

Security invariant (no SSRF-by-hash oracle): the serving route NEVER fetches
for a key that has no ``company_logos`` doc. Docs are created only by the
stamp/warm paths, which derive keys from real scraped company names — so an
attacker can't use the public route to make the server probe arbitrary
domains by submitting hashes.

Mongo collection ``company_logos``:
  {key (sha1 hex, unique), company (normalized), domain, s3_key,
   content_type, bytes_len, status: "ok"|"missing"|"pending",
   source: "site"|"google"|None, fetched_at}
"status: missing" is a negative cache — refetch-eligible after 30 days.
"""
from __future__ import annotations

import hashlib
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urljoin

from utils.db_connect import DBConnect

logger = logging.getLogger(__name__)

COLLECTION = "company_logos"
S3_PREFIX = "company-logos/v1"
LOGO_ROUTE_PREFIX = "/api/jobs/logo"

MISSING_RETRY_DAYS = 30
PER_REQUEST_TIMEOUT_S = 3          # per outbound request (spec: <=3s)
TOTAL_BUDGET_S = 8.0               # per-company total fetch budget (spec: <=8s)
MAX_LOGO_BYTES = 512 * 1024

GOOGLE_FAVICON_URL = "https://www.google.com/s2/favicons?domain={domain}&sz=128"
# Google returns 200 + a small default globe PNG when it has no icon for a
# domain; real icons at sz=128 come back >= 32px wide.
GOOGLE_DEFAULT_MIN_WIDTH = 32

# Content types we accept (value = S3 key extension). SVG is REJECTED — it
# can carry scripts and we serve these bytes to browsers.
ACCEPTED_CONTENT_TYPES = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
    "image/x-icon": "ico",
    "image/vnd.microsoft.icon": "ico",
}

_ICO_MAGIC = (b"\x00\x00\x01\x00", b"\x00\x00\x02\x00")
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126 Safari/537.36"
    ),
    "Accept": "image/avif,image/webp,image/png,image/*;q=0.8,*/*;q=0.5",
}

# Curated overrides for big names whose catalog slug is not their domain.
# Keys are normalized (lowercase, collapsed-whitespace) company names.
DOMAIN_OVERRIDES: Dict[str, str] = {
    "google": "google.com",
    "alphabet": "google.com",
    "jpmorgan chase": "jpmorganchase.com",
    "johnson & johnson": "jnj.com",
    "johnson and johnson": "jnj.com",
    "procter & gamble": "pg.com",
    "unitedhealth group": "unitedhealthgroup.com",
    "us bank": "usbank.com",
    "deutsche bank": "db.com",
    "eli lilly": "lilly.com",
    "applied materials": "appliedmaterials.com",
    "general motors": "gm.com",
    "warner bros. discovery": "wbd.com",
    "mckinsey & company": "mckinsey.com",
    "land o'lakes": "landolakes.com",
    "character.ai": "character.ai",
    "cursor (anysphere)": "cursor.com",
    "bank of america": "bankofamerica.com",
    "wells fargo": "wellsfargo.com",
    "american express": "americanexpress.com",
}

_WS_RE = re.compile(r"\s+")
_SLUG_STRIP_RE = re.compile(r"[^a-z0-9]")

# <link rel="...icon..."> — covers icon / shortcut icon / apple-touch-icon.
_LINK_ICON_RE = re.compile(
    r"<link\b[^>]*\brel=[\"']?[^\"'>]*icon[^\"'>]*[\"']?[^>]*>",
    re.IGNORECASE,
)
_HREF_RE = re.compile(r"""href=["']?([^"' >]+)""", re.IGNORECASE)
_MAX_HTML_ICON_CANDIDATES = 4


# ---------------------------------------------------------------------------
# Keys + normalization
# ---------------------------------------------------------------------------

def normalize_company(company: Any) -> str:
    """Lowercase + collapsed whitespace. The canonical form for keys, the
    catalog index, and the stored ``company`` field."""
    return _WS_RE.sub(" ", str(company or "").strip().lower()).strip()


def logo_key(company: str) -> str:
    """Deterministic sha1 hex of the normalized company name (40 lowercase
    hex chars) — the public cache key and the S3 basename."""
    return hashlib.sha1(normalize_company(company).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Mongo
# ---------------------------------------------------------------------------

_index_ensured = False


def _coll():
    global _index_ensured
    coll = DBConnect().get_db()[COLLECTION]
    if not _index_ensured:
        try:  # best-effort, once per process (upserts stay correct without it)
            coll.create_index("key", unique=True)
        except Exception:  # pragma: no cover - index creation is advisory
            pass
        _index_ensured = True
    return coll


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _refetch_eligible(doc: Dict[str, Any]) -> bool:
    """A negative-cached ("missing") doc becomes refetch-eligible after 30
    days. Absent/broken fetched_at → eligible."""
    fetched = doc.get("fetched_at")
    if not isinstance(fetched, datetime):
        return True
    if fetched.tzinfo is None:  # real Mongo returns naive UTC datetimes
        fetched = fetched.replace(tzinfo=timezone.utc)
    return (_now() - fetched) >= timedelta(days=MISSING_RETRY_DAYS)


# ---------------------------------------------------------------------------
# Domain resolution ladder (catalog domain → careers host → override → guess)
# ---------------------------------------------------------------------------

_catalog_index: Optional[Dict[str, Dict[str, str]]] = None


def _build_catalog_index() -> Dict[str, Dict[str, str]]:
    """One resolver index over every company catalog we ship. Values merge
    per normalized name: {domain, host, slug} — a later catalog fills fields
    an earlier one left empty, never overwrites."""
    index: Dict[str, Dict[str, str]] = {}

    def add(name: Any, domain: str = "", host: str = "", slug: str = "") -> None:
        n = normalize_company(name)
        if not n:
            return
        entry = index.setdefault(n, {"domain": "", "host": "", "slug": ""})
        if domain and not entry["domain"]:
            entry["domain"] = domain.strip().lower()
        if host and not entry["host"]:
            entry["host"] = host.strip().lower()
        if slug and not entry["slug"]:
            entry["slug"] = slug.strip().lower()

    # MNC catalog — extra.domain (Eightfold rows) + enterprise/orc hosts.
    try:
        from services.mnc_catalog import MNC_CATALOG
        for row in MNC_CATALOG:
            domain = str((row.get("extra") or {}).get("domain") or "")
            host = str(row.get("host") or "")
            slug = str(row.get("slug") or row.get("tenant") or "")
            add(row.get("display_name"), domain, host, slug)
            for alias in row.get("aliases") or []:
                add(alias, domain, host, slug)
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("logo resolver: mnc catalog unavailable: %s", e)

    # Enterprise career-pages catalog — careers hosts (the careers host
    # virtually always serves the company favicon) + extra.domain.
    try:
        from services.career_pages_service import ENTERPRISE_CATALOG
        for row in ENTERPRISE_CATALOG:
            add(
                row.get("display_name"),
                str((row.get("extra") or {}).get("domain") or ""),
                str(row.get("host") or ""),
                str(row.get("slug") or ""),
            )
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("logo resolver: enterprise catalog unavailable: %s", e)

    # Board-ATS catalog — slug only (greenhouse/lever/ashby slugs usually
    # match the company domain label).
    try:
        from services.career_pages_catalog import CAREER_PAGES_CATALOG
        for row in CAREER_PAGES_CATALOG:
            add(row.get("display_name"), slug=str(row.get("slug") or ""))
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("logo resolver: career pages catalog unavailable: %s", e)

    # Workday tenant catalog — tenant as slug (myworkdayjobs hosts serve
    # Workday's own favicon, so the host is deliberately NOT used).
    try:
        from services.workday_tenant_catalog import WORKDAY_TENANT_CATALOG
        for row in WORKDAY_TENANT_CATALOG:
            add(row.get("display_name"), slug=str(row.get("tenant") or ""))
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("logo resolver: workday catalog unavailable: %s", e)

    return index


def _get_catalog_index() -> Dict[str, Dict[str, str]]:
    global _catalog_index
    if _catalog_index is None:
        _catalog_index = _build_catalog_index()
    return _catalog_index


def resolve_domain(company: str) -> str:
    """Domain resolution ladder for a company name:
      (a) catalog ``extra.domain`` when present;
      (b) careers catalog ``host``;
      (c) curated DOMAIN_OVERRIDES;
      (d) ``slug + ".com"`` guess (benign failure: fetch misses → monogram).
    """
    n = normalize_company(company)
    if not n:
        return ""
    entry = _get_catalog_index().get(n) or {}
    if entry.get("domain"):
        return entry["domain"]
    if entry.get("host"):
        return entry["host"]
    override = DOMAIN_OVERRIDES.get(n)
    if override:
        return override
    slug = entry.get("slug") or _SLUG_STRIP_RE.sub("", n)
    return f"{slug}.com" if slug else ""


# ---------------------------------------------------------------------------
# Fetch pipeline (SSRF-guarded; Google primary, site secondary)
# ---------------------------------------------------------------------------

def _accept_image(content_type: str, data: bytes) -> Optional[Dict[str, str]]:
    """Validate fetched bytes: accepted image content-type (or ico by magic
    bytes when mislabeled), <= 512KB, never SVG. Returns
    {"content_type", "ext"} or None."""
    if not data or len(data) > MAX_LOGO_BYTES:
        return None
    ct = (content_type or "").split(";")[0].strip().lower()
    if "svg" in ct or data.lstrip()[:4] == b"<svg" or data.lstrip()[:5] == b"<?xml":
        return None
    if ct in ACCEPTED_CONTENT_TYPES:
        return {"content_type": ct, "ext": ACCEPTED_CONTENT_TYPES[ct]}
    # Servers routinely mislabel .ico (text/plain, application/octet-stream).
    if data[:4] in _ICO_MAGIC:
        return {"content_type": "image/x-icon", "ext": "ico"}
    return None


def _png_width(data: bytes) -> Optional[int]:
    """PNG IHDR width (bytes 16-20, big-endian) — no Pillow in the layer."""
    if len(data) < 24 or not data.startswith(_PNG_MAGIC):
        return None
    return int.from_bytes(data[16:20], "big")


def _guarded_get(url: str, deadline: float):
    """jd_fetcher._safe_get (SSRF guard + redirect re-validation) bounded by
    the per-request timeout AND the remaining total budget. None when the
    budget is spent, the URL is blocked, or the request errors."""
    remaining = deadline - time.monotonic()
    if remaining < 1:
        return None
    from services.jd_fetcher import _safe_get
    timeout = int(min(PER_REQUEST_TIMEOUT_S, max(1, remaining)))
    try:
        return _safe_get(url, timeout=timeout, headers=_HEADERS)
    except Exception as e:
        logger.info("logo fetch error (%s)", type(e).__name__)
        return None


def _fetch_image_url(url: str, deadline: float) -> Optional[Dict[str, Any]]:
    r = _guarded_get(url, deadline)
    if r is None or not r.ok:
        return None
    accepted = _accept_image(r.headers.get("Content-Type", ""), r.content or b"")
    if not accepted:
        return None
    return {"data": r.content, **accepted}


def _fetch_google_favicon(domain: str, deadline: float) -> Optional[Dict[str, Any]]:
    """PRIMARY source. Google 200s a small default globe PNG for unknown
    domains — width < 32 (via IHDR parse) is treated as a miss so the
    secondary (direct site) ladder gets its turn."""
    url = GOOGLE_FAVICON_URL.format(domain=quote(domain, safe=""))
    got = _fetch_image_url(url, deadline)
    if not got:
        return None
    if got["content_type"] == "image/png":
        width = _png_width(got["data"])
        if width is not None and width < GOOGLE_DEFAULT_MIN_WIDTH:
            return None  # default globe → not this company's icon
    got["source"] = "google"
    return got


def _fetch_site_icon(domain: str, deadline: float) -> Optional[Dict[str, Any]]:
    """SECONDARY source: apple-touch-icon conventions, then a homepage HTML
    parse for <link rel=...icon...> (relative hrefs resolved; every fetch is
    SSRF-guarded again by _guarded_get)."""
    base = f"https://{domain}"
    for path in ("/apple-touch-icon.png", "/apple-touch-icon-precomposed.png"):
        got = _fetch_image_url(base + path, deadline)
        if got:
            got["source"] = "site"
            return got

    r = _guarded_get(base + "/", deadline)
    if r is None or not r.ok:
        return None
    html = (r.text or "")[:300_000]
    tried = 0
    for m in _LINK_ICON_RE.finditer(html):
        href_m = _HREF_RE.search(m.group(0))
        if not href_m:
            continue
        href = href_m.group(1).strip()
        if not href or href.lower().endswith(".svg") or href.startswith("data:"):
            continue
        candidate = urljoin(base + "/", href)
        tried += 1
        got = _fetch_image_url(candidate, deadline)
        if got:
            got["source"] = "site"
            return got
        if tried >= _MAX_HTML_ICON_CANDIDATES:
            break
    return None


def fetch_logo_for_domain(domain: str) -> Optional[Dict[str, Any]]:
    """Run the full fetch ladder for a domain within the total time budget.
    Returns {"data", "content_type", "ext", "source"} or None. Module-level
    so tests (and the warm script) can patch/observe it."""
    domain = (domain or "").strip().lower()
    if not domain:
        return None
    deadline = time.monotonic() + TOTAL_BUDGET_S
    got = _fetch_google_favicon(domain, deadline)
    if got is None:
        got = _fetch_site_icon(domain, deadline)
    return got


# ---------------------------------------------------------------------------
# S3
# ---------------------------------------------------------------------------

def _store_s3(key: str, data: bytes, content_type: str, ext: str) -> str:
    from services.s3_service import get_storage_service
    svc = get_storage_service()
    if not svc.bucket:
        raise ValueError("RESUME_S3_BUCKET is not configured")
    s3_key = f"{S3_PREFIX}/{key}.{ext}"
    svc.client.put_object(
        Bucket=svc.bucket, Key=s3_key, Body=data, ContentType=content_type
    )
    return s3_key


def _read_s3(s3_key: str) -> Optional[bytes]:
    try:
        from services.s3_service import get_storage_service
        return get_storage_service().get_resume(s3_key)
    except Exception as e:
        logger.warning("logo s3 read failed (%s): %s", s3_key, type(e).__name__)
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_or_fetch(company: str) -> Optional[Dict[str, Any]]:
    """Return {status:"ok", s3_key, content_type} for a company, fetching and
    caching inline (bounded by TOTAL_BUDGET_S) on a miss. Any failure writes
    the negative cache (status "missing", refetch-eligible after 30 days) and
    returns None."""
    name = normalize_company(company)
    if not name:
        return None
    key = logo_key(name)
    coll = _coll()
    doc = coll.find_one({"key": key}) or {}

    if doc.get("status") == "ok" and doc.get("s3_key"):
        return {
            "status": "ok",
            "s3_key": doc["s3_key"],
            "content_type": str(doc.get("content_type") or "image/png"),
        }
    if doc.get("status") == "missing" and not _refetch_eligible(doc):
        return None  # negative cache holds

    domain = str(doc.get("domain") or "") or resolve_domain(name)
    got = None
    if domain:
        try:
            got = fetch_logo_for_domain(domain)
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("logo fetch pipeline failed for key %s: %s", key, type(e).__name__)
            got = None

    if got:
        try:
            s3_key = _store_s3(key, got["data"], got["content_type"], got["ext"])
        except Exception as e:
            logger.warning("logo s3 store failed for key %s: %s", key, type(e).__name__)
            s3_key = None
        if s3_key:
            coll.update_one(
                {"key": key},
                {"$set": {
                    "key": key,
                    "company": name,
                    "domain": domain,
                    "s3_key": s3_key,
                    "content_type": got["content_type"],
                    "bytes_len": len(got["data"]),
                    "status": "ok",
                    "source": got.get("source") or "site",
                    "fetched_at": _now(),
                }},
                upsert=True,
            )
            return {"status": "ok", "s3_key": s3_key, "content_type": got["content_type"]}
        # Fetch succeeded but the STORE failed (e.g. bucket unconfigured):
        # an infra failure must not become a 30-day content negative-cache.
        # Leave the doc pending so the next request retries the store.
        coll.update_one(
            {"key": key},
            {"$set": {"key": key, "company": name, "domain": domain,
                      "status": "pending", "fetched_at": _now()}},
            upsert=True,
        )
        return None

    # Negative cache — graceful absence (FE monogram fallback).
    coll.update_one(
        {"key": key},
        {"$set": {
            "key": key,
            "company": name,
            "domain": domain,
            "s3_key": None,
            "content_type": None,
            "bytes_len": 0,
            "status": "missing",
            "source": None,
            "fetched_at": _now(),
        }},
        upsert=True,
    )
    return None


def stamp_logo_urls(rows: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """Add ``logo_url: /api/jobs/logo/<key>`` to every job dict with a company
    (always stamped — the route lazily populates on first request) and upsert
    a ``pending`` company_logos doc per company. The doc upsert is what
    entitles the serving route to fetch: keys without a doc are never fetched
    (no SSRF-by-hash oracle), so docs must exist for every stamped company."""
    rows = rows if isinstance(rows, list) else []
    keys_by_name: Dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = normalize_company(row.get("company"))
        if not name:
            continue
        key = keys_by_name.get(name)
        if key is None:
            key = logo_key(name)
            keys_by_name[name] = key
        row["logo_url"] = f"{LOGO_ROUTE_PREFIX}/{key}"
    if keys_by_name:
        _ensure_docs(keys_by_name)
    return rows


def _ensure_docs(keys_by_name: Dict[str, str]) -> None:
    """Upsert pending docs for stamped companies ($setOnInsert only —
    existing ok/missing docs are never disturbed). Per-key update_one (the
    set is small and deduped; the callers are async workers, and bulk_write
    trips a pymongo/mongomock kwarg incompatibility). Best-effort: a Mongo
    hiccup must never break a pipeline/matches response."""
    try:
        coll = _coll()
        for name, key in keys_by_name.items():
            coll.update_one(
                {"key": key},
                {"$setOnInsert": {
                    "key": key,
                    "company": name,
                    "domain": None,
                    "s3_key": None,
                    "content_type": None,
                    "bytes_len": 0,
                    "status": "pending",
                    "source": None,
                    "fetched_at": None,
                    "created_at": _now(),
                }},
                upsert=True,
            )
    except Exception as e:
        logger.warning("company_logos ensure-docs failed: %s", type(e).__name__)


def get_logo_bytes(key: str) -> Optional[Any]:
    """Serving path for GET /api/jobs/logo/<key>. The key MUST already be
    format-validated (40 lowercase hex) by the route.

    * no doc → None (NEVER fetch — the no-doc-no-fetch invariant);
    * status ok → S3 bytes;
    * status pending, or missing past the 30-day negative-cache window →
      one inline get_or_fetch (bounded), then serve or None.

    Returns (bytes, content_type) or None.
    """
    doc = _coll().find_one({"key": str(key)})
    if not doc:
        return None

    info: Optional[Dict[str, Any]] = None
    if doc.get("status") == "ok" and doc.get("s3_key"):
        info = {"s3_key": doc["s3_key"], "content_type": doc.get("content_type")}
    elif doc.get("status") == "pending" or (
        doc.get("status") == "missing" and _refetch_eligible(doc)
    ):
        info = get_or_fetch(doc.get("company") or "")

    if not info or not info.get("s3_key"):
        return None
    data = _read_s3(str(info["s3_key"]))
    if not data:
        return None
    return data, str(info.get("content_type") or "image/png")
