"""Company logo pipeline — REAL favicons/brand icons only, cached in S3.

Every job card (Discover daily pipeline + guest matches) gets a ``logo_url``
pointing at ``GET /api/jobs/logo/<sha1(company)>``, which serves the company's
actual favicon from an S3 cache. We NEVER generate or synthesize logos — a
company with no fetchable icon simply 404s and the frontend renders a
monogram fallback.

Domain candidates per company, strongest evidence first (``candidate_domains``):

  1. the ``employers`` registry - ``canonical_domain`` / ``verified_domains``
     of the posting's ``canonical_employer_id``, else of the one employer
     whose ``name_key`` matches (an ambiguous name answers nothing);
  2. catalog ``extra.domain``, then the catalog careers host;
  3. curated DOMAIN_OVERRIDES;
  4. a domain written inside the name ("Amazon.com Services LLC");
  5. guesses: the name with its legal/region tail stripped + ".com"
     ("CROWDSTRIKE INC" -> crowdstrike.com, never crowdstrikeinc.com), then
     the catalog slug.

Each candidate is tried in order until one yields an icon. Fetch ladder per
domain (server-side only, SSRF-guarded via jd_fetcher's ``_safe_get`` -
public-IP-only, per-hop redirect re-validation):

  1. Google's s2 favicon service (``sz=128``; it redirects to gstatic).
  2. DuckDuckGo's icon service (``icons.duckduckgo.com/ip3/<domain>.ico``).
  3. The site itself - ``/favicon.ico``, ``/apple-touch-icon.png``,
     ``/apple-touch-icon-precomposed.png``, then a homepage parse for
     ``<link rel=...icon...>``.

An icon at least 32px wide ends the ladder. A real but small one (16-31px)
is kept and used only when no source has anything larger. Both services
answer "no icon" with a 404 AND a fixed placeholder image; the placeholder
bytes are recognized by hash (``PLACEHOLDER_SHA1``) so they can never be
stored even if a service starts returning them with a 200.

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
  {key (sha1 hex, unique), company (normalized), domain, domain_basis,
   canonical_employer_id, s3_key, content_type, bytes_len,
   status: "ok"|"missing"|"pending", source: "google"|"duckduckgo"|"site"|None,
   resolver_version, fetched_at}
"status: missing" is a negative cache - refetch-eligible after 30 days, or
as soon as ``RESOLVER_VERSION`` moves past the version that wrote it.
``refresh_due`` (the scheduler's ``logo_refresh``) does that refetching in
the background, so the serving route only ever fetches a never-tried company.
"""
from __future__ import annotations

import hashlib
import logging
import re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple
from urllib.parse import quote, urljoin, urlparse

from utils.db_connect import DBConnect

logger = logging.getLogger(__name__)

COLLECTION = "company_logos"
S3_PREFIX = "company-logos/v1"
LOGO_ROUTE_PREFIX = "/api/jobs/logo"

MISSING_RETRY_DAYS = 30
PER_REQUEST_TIMEOUT_S = 3          # per outbound request (spec: <=3s)
TOTAL_BUDGET_S = 8.0               # per-company budget on the serving route (spec: <=8s)
BACKGROUND_BUDGET_S = 15.0         # per-company budget in refresh_due (no user waiting)
PER_DOMAIN_BUDGET_S = 6.0          # one candidate domain may not eat the whole budget
MAX_LOGO_BYTES = 512 * 1024

# Bumped whenever candidate_domains or the fetch ladder changes what a company
# resolves to. A "missing" doc written by an older version is refetched by
# refresh_due right away instead of waiting out the 30-day negative cache, and
# an "ok" doc is rechecked against a stronger domain. v1 (no field) guessed
# "<whole name>.com" - "CROWDSTRIKE INC" -> crowdstrikeinc.com - and dropped
# every icon narrower than 32px.
RESOLVER_VERSION = 2

GOOGLE_FAVICON_URL = "https://www.google.com/s2/favicons?domain={domain}&sz=128"
DDG_ICON_URL = "https://icons.duckduckgo.com/ip3/{domain}.ico"
# An icon this wide ends the ladder. Narrower but >= MIN_USABLE_WIDTH is a
# real icon kept as a last resort (a 16px favicon still beats a monogram for
# recognition). Kept under its old name too: Google's real icons at sz=128
# come back >= 32px wide.
GOOD_MIN_WIDTH = 32
GOOGLE_DEFAULT_MIN_WIDTH = GOOD_MIN_WIDTH
MIN_USABLE_WIDTH = 16

# The "no icon" images both services serve (with a 404), by sha1 of the bytes.
# Google's is a 16px globe; DuckDuckGo's is 48px, so a width test alone would
# store it as a logo. Captured 2026-11-05.
PLACEHOLDER_SHA1 = frozenset({
    "2d7c9b60d1e2b4f4726141de2e4ab738110b9287",  # google s2 / gstatic globe, 726 B
    "980aa215c45dd3b92f40b272234a21f6d850b14a",  # duckduckgo ip3 placeholder, 1478 B
})

# How a candidate domain was found. Everything but "guess" is evidence that
# the domain belongs to the company; refresh_due rechecks an "ok" logo only
# when one of these now names a different domain than the one it came from.
PROVEN_BASES = frozenset({"registry", "catalog", "override", "name"})

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


# Trailing words that name the legal entity or the national subsidiary, not
# the brand. Stripped only at the END of the name, repeatedly, so "Bank of
# America" and "Company Cam" keep their words. "group" and "holdings" are NOT
# here: "Zillow Group" really is zillowgroup.com.
_ENTITY_TAIL_RE = re.compile(
    r"(?:[\s,]+(?:inc|incorporated|corp|corporation|co|company|llc|l\.l\.c|"
    r"ltd|limited|plc|lp|llp|pllc|gmbh|ag|sa|s\.a|nv|n\.v|bv|b\.v|pte|pty|"
    r"srl|spa|usa|us|u\.s|u\.s\.a)\.?)+$")
_PAREN_RE = re.compile(r"\s*\([^)]*\)")
_DANGLING_AND_RE = re.compile(r"(?:\s*&|\s+and)\s*$")
_EMBEDDED_DOMAIN_RE = re.compile(
    r"(?<![a-z0-9-])([a-z0-9][a-z0-9-]*\.(?:com|io|ai|co|net|org|app|dev|tech))"
    r"(?![a-z0-9-])")
_DESCRIPTOR_TAIL_RE = re.compile(
    r"\s+(?:holdings?|group|technologies|technology|systems|solutions|labs)$")
_MAX_GUESSES = 4

# Hosts that belong to the hiring software, not the employer: their icon is
# the VENDOR's logo. JPMorgan Chase's catalog careers host is
# jpmc.fa.oraclecloud.com, which made its card show Oracle's icon.
_VENDOR_HOST_SUFFIXES = (
    "oraclecloud.com", "myworkdayjobs.com", "myworkdaysite.com", "workday.com",
    "icims.com", "eightfold.ai", "phenompeople.com", "taleo.net",
    "successfactors.com", "successfactors.eu", "jobvite.com",
    "smartrecruiters.com", "greenhouse.io", "lever.co", "ashbyhq.com",
    "workable.com", "recruitee.com", "avature.net", "brassring.com",
    "ultipro.com", "dayforcehcm.com", "paylocity.com", "adp.com",
    "breezy.hr", "bamboohr.com", "applytojob.com", "teamtailor.com",
    "personio.de", "personio.com", "rippling.com", "jazzhr.com",
    "talentbrew.com", "radancy.com", "tmp.com", "silkroad.com",
)


def _is_vendor_host(domain: str) -> bool:
    d = domain.lower().strip(".")
    return any(d == v or d.endswith("." + v) for v in _VENDOR_HOST_SUFFIXES)


# A guessed domain is someone else's more often than one would hope. Measured
# 2026-11-05 on 123 real employers: grubhubholdings.com is a GoDaddy
# "Protected Domain Holder" page (its icon is GoDaddy's heart),
# thermofisherscientific.com answers nothing while Google still holds an old
# marketplace icon for it, and thedowchemical.com redirects to an activist
# article about Bhopal. So a GUESS must answer, must not be a parking/sale
# page, and must not redirect to a site that does not carry the brand.
_PARKING_HOST_SUFFIXES = (
    "gcd.com", "godaddy.com", "afternic.com", "sedo.com", "dan.com",
    "hugedomains.com", "buydomains.com", "uniregistry.com", "uni.com",
    "bodis.com", "parkingcrew.net", "above.com", "undeveloped.com",
    "efty.com", "atom.com", "squadhelp.com", "brandbucket.com",
    "domainmarket.com", "sav.com", "epik.com",
)
_PARKING_TEXT_RE = re.compile(
    r"protected domain holder|domain (?:name )?(?:is|may be) for sale|"
    r"buy this domain|this domain is parked|parked free|domain parking|"
    r"parkingcrew|sedoparking|wsimg\.com/parking-lander|afternic\.com|"
    r"hugedomains\.com|dan\.com/buy", re.IGNORECASE)


# Bot-protection challenge hosts: being sent to one means the site is live
# and defending itself (persistent.com -> validate.perfdrive.com).
_BOT_WALL_HOST_SUFFIXES = (
    "perfdrive.com", "captcha-delivery.com", "datadome.co", "hcaptcha.com",
    "challenges.cloudflare.com", "perimeterx.net", "px-cloud.net",
)
_NAME_STOPWORDS = frozenset({"of", "and", "the", "for", "&"})


def _host_in(host: str, suffixes: Tuple[str, ...]) -> bool:
    return any(host == v or host.endswith("." + v) for v in suffixes)


def _brand_matches_host(company: str, host: str) -> bool:
    """Does a redirect target still carry the brand? Its host contains the
    name's first word ("dowchemical.com" -> www.dow.com), or its domain label
    is the name's initials ("educational testing service" -> ets.org,
    "bank of new york mellon" -> bny.com) or a prefix of its first word
    ("genentech" -> gene.com)."""
    core = _core_name(normalize_company(company))
    words = [w for w in re.split(r"[^a-z0-9]+", core) if w and w not in _NAME_STOPWORDS]
    if not words:
        return True
    token = words[0]
    if len(token) >= 3 and token in host.replace("-", ""):
        return True
    from services.employer_registry import normalize_domain
    label = (normalize_domain(host) or host).split(".")[0].replace("-", "")
    initials = "".join(w[0] for w in words)
    return ((len(label) >= 2 and label == initials)
            or (len(label) >= 3 and initials.startswith(label))
            or (len(label) >= 4 and token.startswith(label)))


def _guess_verdict(domain: str, company: str, deadline: float) -> str:
    """Is a GUESSED domain this company's? One homepage request answers
    "yes", "no" or "unknown":

      * "no"  - a parking or for-sale page, or a redirect to a site that does
                not carry the brand (thedowchemical.com -> an activist
                article; zoomcommunications.com -> an SEO agency);
      * "unknown" - nothing answered (DNS, refused, timed out). Big sites
                hang on bots (bestbuy.com, mphasis.com), but a dead domain
                looks the same and Google can still hold a years-old icon
                for it (thermofisherscientific.com), so the caller accepts
                only a full-size icon from Google or DuckDuckGo;
      * "yes" - anything else, including a bot wall (403/429 or a challenge
                host): the site is live.

    Name collisions (two real companies called "Persistent Systems") are NOT
    caught here; that is what the registry's verified domains are for."""
    r = _guarded_get(f"https://{domain}/", deadline)
    if r is None:
        return "unknown"
    final = (urlparse(str(getattr(r, "url", "") or "")).hostname or "").lower()
    if final and _host_in(final, _PARKING_HOST_SUFFIXES):
        return "no"
    if final and _host_in(final, _BOT_WALL_HOST_SUFFIXES):
        return "yes"
    try:
        text = (r.text or "")[:200_000]
    except Exception:
        text = ""
    if _PARKING_TEXT_RE.search(text):
        return "no"
    if final and not (final == domain or final.endswith("." + domain)):
        if not _brand_matches_host(company, final):
            return "no"
    return "yes"


def _core_name(normalized: str) -> str:
    """The brand part of a normalized name: parentheticals and trailing
    legal/region words removed ("crowdstrike inc" -> "crowdstrike",
    "shopify usa inc." -> "shopify", "cursor (anysphere)" -> "cursor")."""
    core = _PAREN_RE.sub("", normalized).strip(" ,.-")
    while True:
        # "jpmorgan chase & co." -> "jpmorgan chase &" -> "jpmorgan chase"
        stripped = _DANGLING_AND_RE.sub("", _ENTITY_TAIL_RE.sub("", core)).strip(" ,.-")
        if stripped == core or not stripped:
            break
        core = stripped
    return core


def _slug(name: str) -> str:
    return _SLUG_STRIP_RE.sub("", name)


def _guesses(normalized: str, catalog_slug: str = "") -> List[str]:
    """"<brand>.com" guesses, most likely first. Benign when they miss: the
    icon services 404 an unknown domain and the next candidate gets its turn.

    Order, from measuring live names (2026-11-05):
      * without a leading "the" first - "The Dow Chemical Company" is
        dowchemical.com, and thedowchemical.com is someone else's site;
      * then with it ("The Trade Desk" is thetradedesk.com);
      * "&" spelled "and" (barnesandnoble.com);
      * a trailing descriptor dropped (zensar.com for "Zensar Technologies",
        grubhub.com for "Grubhub Holdings") - last, because a short remainder
        is often someone else's domain;
      * the catalog slug."""
    core = _core_name(normalized) or normalized
    names = [core[4:], core] if core.startswith("the ") and len(core) > 4 else [core]
    first = names[0]
    if "&" in first:
        names.append(first.replace("&", " and "))   # "barnes & noble" -> barnesandnoble
    bare = _DESCRIPTOR_TAIL_RE.sub("", first).strip()
    if bare != first and len(_slug(bare)) >= 4:
        names.append(bare)
    out: List[str] = []
    for n in names + ([catalog_slug] if catalog_slug else []):
        slug = _slug(n)
        if slug and f"{slug}.com" not in out:
            out.append(f"{slug}.com")
    return out[:_MAX_GUESSES]


def _catalog_entry(normalized: str) -> Dict[str, str]:
    index = _get_catalog_index()
    return index.get(normalized) or index.get(_core_name(normalized)) or {}


def _registry_domains(normalized: str, canonical_employer_id: Any = None) -> List[str]:
    """Domains the ``employers`` registry holds for this company: by the
    posting's canonical id first (exact), else by ``name_key`` when exactly
    ONE employer carries it - two employers sharing a name ("Forward",
    "Public") answer nothing, as in ``employer_registry.resolve_employer``.
    A renamed/acquired/brand row without a domain borrows its parent's.
    Best effort: a registry hiccup just means fewer candidates."""
    try:
        from services import employer_registry as er
        col = er._col(er.EMPLOYERS)
        proj = {"_id": 0, "canonical_domain": 1, "verified_domains": 1,
                "parent_employer_id": 1, "operating_status": 1}
        row = None
        cid = str(canonical_employer_id or "").strip()
        if cid.startswith("emp_"):
            row = col.find_one({"employer_id": cid}, proj)
        if row is None:
            nk = er.name_key(normalized)
            if nk and nk not in er.wrong_name_keys():
                hits = list(col.find({"name_key": nk}, proj).limit(2))
                if len(hits) == 1:
                    row = hits[0]
        if (row and not (row.get("canonical_domain") or row.get("verified_domains"))
                and row.get("parent_employer_id")
                and row.get("operating_status") in er.REDIRECTING_STATUSES):
            row = col.find_one({"employer_id": row["parent_employer_id"]}, proj) or row
        if not row:
            return []
        out: List[str] = []
        for d in [row.get("canonical_domain"), *(row.get("verified_domains") or [])]:
            nd = er.normalize_domain(d)
            if nd and nd not in out:
                out.append(nd)
        return out[:3]
    except Exception as e:
        logger.info("logo resolver: registry lookup failed (%s)", type(e).__name__)
        return []


def candidate_domains(company: str, canonical_employer_id: Any = None, *,
                      use_registry: bool = True) -> List[Tuple[str, str]]:
    """Every domain worth asking for this company's icon, as
    ``[(domain, basis), ...]`` in the order to try them (see the module
    docstring). ``use_registry=False`` keeps it pure (no database)."""
    n = normalize_company(company)
    if not n:
        return []
    out: List[Tuple[str, str]] = []
    seen = set()

    def add(domain: Any, basis: str) -> None:
        d = str(domain or "").strip().lower().strip(".")
        if d and d not in seen and not _is_vendor_host(d):
            seen.add(d)
            out.append((d, basis))

    if use_registry:
        for d in _registry_domains(n, canonical_employer_id):
            add(d, "registry")
    entry = _catalog_entry(n)
    add(entry.get("domain"), "catalog")
    add(entry.get("host"), "catalog")
    add(DOMAIN_OVERRIDES.get(n) or DOMAIN_OVERRIDES.get(_core_name(n)), "override")
    m = _EMBEDDED_DOMAIN_RE.search(n)
    if m:
        add(m.group(1), "name")
    for g in _guesses(n, entry.get("slug") or ""):
        add(g, "guess")
    return out


def resolve_domain(company: str) -> str:
    """The first domain ``candidate_domains`` would try, without the registry
    (pure: catalog domain -> careers host -> override -> name -> guess).
    Kept for the warm script's offline dry run and older callers."""
    got = candidate_domains(company, use_registry=False)
    return got[0][0] if got else ""


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


def _image_width(data: bytes) -> Optional[int]:
    """Pixel width without Pillow: PNG IHDR, the widest ICO directory entry
    (a 0 byte means 256), or the GIF screen width. None = could not tell
    (JPEG, WebP, a malformed header) - such an image is accepted as before."""
    if data.startswith(_PNG_MAGIC):
        return _png_width(data)
    if data[:4] in _ICO_MAGIC and len(data) >= 6:
        count = int.from_bytes(data[4:6], "little")
        if 0 < count <= 64 and len(data) >= 6 + 16 * count:
            return max((data[6 + 16 * i] or 256) for i in range(count))
        return None
    if data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
        return int.from_bytes(data[6:8], "little")
    return None


def _grade(data: bytes) -> Optional[str]:
    """"good" (ends the ladder), "small" (a real 16-31px icon, kept as a last
    resort) or None (a known placeholder, or too small to be a logo)."""
    if hashlib.sha1(data).hexdigest() in PLACEHOLDER_SHA1:
        return None
    width = _image_width(data)
    if width is None or width >= GOOD_MIN_WIDTH:
        return "good"
    if width >= MIN_USABLE_WIDTH:
        return "small"
    return None


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


def _tagged(got: Optional[Dict[str, Any]], source: str) -> Optional[Dict[str, Any]]:
    if got:
        got["source"] = source
    return got


def _fetch_google_favicon(domain: str, deadline: float) -> Optional[Dict[str, Any]]:
    """Google's s2 service. "No icon" is a 404 (plus the globe placeholder),
    which _fetch_image_url already treats as a miss."""
    url = GOOGLE_FAVICON_URL.format(domain=quote(domain, safe=""))
    return _tagged(_fetch_image_url(url, deadline), "google")


def _fetch_ddg_icon(domain: str, deadline: float) -> Optional[Dict[str, Any]]:
    """DuckDuckGo's icon service: it holds icons Google does not (measured
    2026-11-05: LTIMindtree, Rockwell Automation)."""
    url = DDG_ICON_URL.format(domain=quote(domain, safe=""))
    return _tagged(_fetch_image_url(url, deadline), "duckduckgo")


def _site_icons(domain: str, deadline: float) -> Iterator[Dict[str, Any]]:
    """The site itself: /favicon.ico, the apple-touch-icon conventions, then
    the homepage's <link rel=...icon...> hrefs (relative ones resolved).
    Cheapest first - /favicon.ico is one request, and on a slow site the
    homepage parse used to eat the budget before it was ever asked
    (zensar.com). Lazy, so the caller stops once it has a good icon. Every
    fetch is SSRF-guarded again by _guarded_get."""
    base = f"https://{domain}"
    tried = set()
    for path in ("/favicon.ico", "/apple-touch-icon.png", "/apple-touch-icon-precomposed.png"):
        tried.add(base + path)
        got = _fetch_image_url(base + path, deadline)
        if got:
            yield _tagged(got, "site")

    r = _guarded_get(base + "/", deadline)
    if r is None or not r.ok:
        return
    html = (r.text or "")[:300_000]
    linked = 0
    for m in _LINK_ICON_RE.finditer(html):
        href_m = _HREF_RE.search(m.group(0))
        if not href_m:
            continue
        href = href_m.group(1).strip()
        if not href or href.lower().endswith(".svg") or href.startswith("data:"):
            continue
        candidate = urljoin(base + "/", href)
        if candidate in tried:
            continue
        tried.add(candidate)
        linked += 1
        got = _fetch_image_url(candidate, deadline)
        if got:
            yield _tagged(got, "site")
        if linked >= _MAX_HTML_ICON_CANDIDATES:
            break


def _icon_sources(domain: str, deadline: float) -> Iterator[Optional[Dict[str, Any]]]:
    yield _fetch_google_favicon(domain, deadline)
    yield _fetch_ddg_icon(domain, deadline)
    yield from _site_icons(domain, deadline)


def _fetch_site_icon(domain: str, deadline: float) -> Optional[Dict[str, Any]]:
    """The site ladder alone (first acceptable icon). Kept for callers that
    want only the employer's own site."""
    for got in _site_icons(domain, deadline):
        if got and _grade(got["data"]):
            return got
    return None


def fetch_logo_for_domain(domain: str, deadline: Optional[float] = None
                          ) -> Optional[Dict[str, Any]]:
    """Run the full fetch ladder for a domain within the time budget (its own
    TOTAL_BUDGET_S unless the caller passes a shared ``deadline``). Returns
    {"data", "content_type", "ext", "source"} or None. The first icon at least
    GOOD_MIN_WIDTH wide wins; otherwise the widest real small icon seen.
    Module-level so tests (and the warm script) can patch/observe it."""
    domain = (domain or "").strip().lower()
    if not domain:
        return None
    if deadline is None:
        deadline = time.monotonic() + TOTAL_BUDGET_S
    best_small: Optional[Dict[str, Any]] = None
    best_width = -1
    for got in _icon_sources(domain, deadline):
        if got:
            grade = _grade(got["data"])
            if grade == "good":
                return got
            if grade == "small":
                width = _image_width(got["data"]) or 0
                if width > best_width:
                    best_small, best_width = got, width
        if time.monotonic() >= deadline:
            break
    return best_small


def fetch_for_candidates(candidates: List[Tuple[str, str]], budget_s: float,
                         company: str = "") -> Optional[Tuple[str, str, Dict[str, Any]]]:
    """Try each (domain, basis) in order; the first domain that yields any
    usable icon wins - a 16px icon from the registry's domain beats a big one
    from a guess. One domain gets at most PER_DOMAIN_BUDGET_S of the budget
    so a slow site cannot starve the next candidate. A guessed domain must
    pass ``_guess_verdict`` first."""
    deadline = time.monotonic() + budget_s
    for domain, basis in candidates:
        if deadline - time.monotonic() < 1:
            break
        per_domain = min(deadline, time.monotonic() + PER_DOMAIN_BUDGET_S)
        verdict = _guess_verdict(domain, company, per_domain) if basis == "guess" else "yes"
        if verdict == "no":
            continue
        got = fetch_logo_for_domain(domain, deadline=per_domain)
        if got and verdict == "unknown" and not (
                got.get("source") in ("google", "duckduckgo")
                and _grade(got["data"]) == "good"):
            got = None
        if got:
            return domain, basis, got
    return None


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

def get_or_fetch(company: str, *, canonical_employer_id: Any = None,
                 force: bool = False, budget_s: float = TOTAL_BUDGET_S,
                 only_proven: bool = False) -> Optional[Dict[str, Any]]:
    """Return {status:"ok", s3_key, content_type} for a company, fetching and
    caching inline (bounded by ``budget_s``) on a miss. Any failure writes
    the negative cache (status "missing", refetch-eligible after 30 days or a
    resolver bump) and returns None.

    ``force`` skips both caches (refresh_due uses it for docs an older
    resolver wrote). The domain is re-resolved on every fetch: the domain a
    doc remembers is a record of what was tried, never an input, so a wrong
    v1 guess can no longer pin a company to a dead domain forever.
    ``only_proven`` drops guessed domains (the recheck of a working logo
    must not swap it for another guess)."""
    name = normalize_company(company)
    if not name:
        return None
    key = logo_key(name)
    coll = _coll()
    doc = coll.find_one({"key": key}) or {}

    if not force:
        if doc.get("status") == "ok" and doc.get("s3_key"):
            return {
                "status": "ok",
                "s3_key": doc["s3_key"],
                "content_type": str(doc.get("content_type") or "image/png"),
            }
        if doc.get("status") == "missing" and not _refetch_eligible(doc):
            return None  # negative cache holds

    cid = str(canonical_employer_id or doc.get("canonical_employer_id") or "") or None
    candidates = candidate_domains(name, cid)
    if only_proven:
        candidates = [(d, b) for d, b in candidates if b in PROVEN_BASES]
    found = None
    if candidates:
        try:
            found = fetch_for_candidates(candidates, budget_s, company=name)
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("logo fetch pipeline failed for key %s: %s", key, type(e).__name__)
            found = None
    ident = {"key": key, "company": name}
    if cid:
        ident["canonical_employer_id"] = cid

    if found:
        domain, basis, got = found
        try:
            s3_key = _store_s3(key, got["data"], got["content_type"], got["ext"])
        except Exception as e:
            logger.warning("logo s3 store failed for key %s: %s", key, type(e).__name__)
            s3_key = None
        if s3_key:
            coll.update_one(
                {"key": key},
                {"$set": {
                    **ident,
                    "domain": domain,
                    "domain_basis": basis,
                    "s3_key": s3_key,
                    "content_type": got["content_type"],
                    "bytes_len": len(got["data"]),
                    "status": "ok",
                    "source": got.get("source") or "site",
                    "resolver_version": RESOLVER_VERSION,
                    "fetched_at": _now(),
                }},
                upsert=True,
            )
            return {"status": "ok", "s3_key": s3_key, "content_type": got["content_type"]}
        # Fetch succeeded but the STORE failed (e.g. bucket unconfigured):
        # an infra failure must not become a 30-day content negative-cache.
        # Leave the doc pending so the next request retries the store. An ok
        # doc being rechecked keeps its working logo.
        if doc.get("status") != "ok":
            coll.update_one(
                {"key": key},
                {"$set": {**ident, "domain": domain, "domain_basis": basis,
                          "status": "pending", "fetched_at": _now()}},
                upsert=True,
            )
        return None

    if doc.get("status") == "ok" and doc.get("s3_key"):
        return None  # a forced recheck that found nothing keeps the old logo

    # Negative cache — graceful absence (FE monogram fallback).
    primary, primary_basis = candidates[0] if candidates else ("", None)
    coll.update_one(
        {"key": key},
        {"$set": {
            **ident,
            "domain": primary,
            "domain_basis": primary_basis,
            "domains_tried": [d for d, _ in candidates][:8],
            "s3_key": None,
            "content_type": None,
            "bytes_len": 0,
            "status": "missing",
            "source": None,
            "resolver_version": RESOLVER_VERSION,
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
    (no SSRF-by-hash oracle), so docs must exist for every stamped company.

    A row's ``canonical_employer_id`` is remembered on the doc when every row
    of that company agrees on one, so resolution can ask the registry by id
    instead of by name."""
    rows = rows if isinstance(rows, list) else []
    keys_by_name: Dict[str, str] = {}
    ids_by_name: Dict[str, set] = {}
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
        cid = str(row.get("canonical_employer_id") or "").strip()
        if cid.startswith("emp_"):
            ids_by_name.setdefault(name, set()).add(cid)
        row["logo_url"] = f"{LOGO_ROUTE_PREFIX}/{key}"
    if keys_by_name:
        employer_ids = {n: next(iter(ids)) for n, ids in ids_by_name.items() if len(ids) == 1}
        _ensure_docs(keys_by_name, employer_ids)
    return rows


def _ensure_docs(keys_by_name: Dict[str, str],
                 employer_ids: Optional[Dict[str, str]] = None) -> None:
    """Upsert pending docs for stamped companies ($setOnInsert only —
    existing ok/missing docs are never disturbed; the one field ever $set is
    the employer id the rows agree on). Per-key update_one (the set is small
    and deduped; the callers are async workers, and bulk_write trips a
    pymongo/mongomock kwarg incompatibility). Best-effort: a Mongo hiccup
    must never break a pipeline/matches response."""
    employer_ids = employer_ids or {}
    try:
        coll = _coll()
        for name, key in keys_by_name.items():
            update: Dict[str, Any] = {"$setOnInsert": {
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
            }}
            if employer_ids.get(name):
                update["$set"] = {"canonical_employer_id": employer_ids[name]}
            coll.update_one({"key": key}, update, upsert=True)
    except Exception as e:
        logger.warning("company_logos ensure-docs failed: %s", type(e).__name__)


# ---------------------------------------------------------------------------
# Background refresh (scheduler job ``logo_refresh``)
# ---------------------------------------------------------------------------

_DOC_FIELDS = {"_id": 0, "key": 1, "company": 1, "status": 1, "domain": 1,
               "domain_basis": 1, "canonical_employer_id": 1}


def _due_queues(now: datetime) -> List[Tuple[str, Dict[str, Any]]]:
    """What refresh_due works on, most valuable first: companies never
    fetched (or whose store failed), misses an older resolver wrote, misses
    past the 30-day window, then working logos an older resolver chose."""
    old_resolver = {"$ne": RESOLVER_VERSION}   # also matches docs without the field
    stale = now - timedelta(days=MISSING_RETRY_DAYS)
    return [
        ("pending", {"status": "pending"}),
        ("missing_old_resolver", {"status": "missing", "resolver_version": old_resolver}),
        ("missing_expired", {"status": "missing", "fetched_at": {"$lt": stale}}),
        ("ok_recheck", {"status": "ok", "resolver_version": old_resolver}),
    ]


def due_docs(limit: int) -> List[Tuple[str, Dict[str, Any]]]:
    """Up to ``limit`` (queue, doc) pairs, in queue order, each key once."""
    coll = _coll()
    picked: List[Tuple[str, Dict[str, Any]]] = []
    seen = set()
    for label, query in _due_queues(_now()):
        room = limit - len(picked)
        if room <= 0:
            break
        for doc in coll.find(query, _DOC_FIELDS).limit(room):
            if doc.get("key") in seen or not doc.get("company"):
                continue
            seen.add(doc["key"])
            picked.append((label, doc))
    return picked


def _recheck_ok(doc: Dict[str, Any], budget_s: float) -> str:
    """A working logo an older resolver chose is replaced only when PROVEN
    evidence (registry, catalog, override, a domain in the name) now names a
    different domain AND that domain yields an icon. Otherwise it stays, and
    is stamped current so it is not rechecked again."""
    cid = doc.get("canonical_employer_id")
    proven = [(d, b) for d, b in candidate_domains(doc["company"], cid)
              if b in PROVEN_BASES]
    stored = str(doc.get("domain") or "").lower()
    if proven and proven[0][0] != stored:
        res = get_or_fetch(doc["company"], canonical_employer_id=cid,
                           force=True, budget_s=budget_s, only_proven=True)
        if res:
            fresh = _coll().find_one({"key": doc["key"]}, {"domain": 1}) or {}
            if str(fresh.get("domain") or "").lower() != stored:
                return "replaced"
    _coll().update_one({"key": doc["key"], "status": "ok"},
                       {"$set": {"resolver_version": RESOLVER_VERSION}})
    return "kept"


def refresh_due(*, budget_seconds: float = 240.0, limit: int = 600,
                workers: int = 4, per_company_budget_s: float = BACKGROUND_BUDGET_S,
                on_result: Optional[Callable[[str, Dict[str, Any], str], None]] = None,
                ) -> Dict[str, Any]:
    """Fetch the logos that are due (see ``_due_queues``), a few companies at
    a time, inside ``budget_seconds``. This is what keeps "set them right"
    true without anyone running a script: new companies get their icon
    before most users ever request it, and a resolver improvement reaches
    every company that missed under the old one.

    Returns counts per ``queue:outcome``. Items the budget did not reach are
    counted as ``deferred`` and stay due for the next run."""
    started = time.monotonic()
    picked = due_docs(limit)

    def work(item: Tuple[str, Dict[str, Any]]) -> Tuple[str, Dict[str, Any], str]:
        label, doc = item
        if time.monotonic() - started > budget_seconds:
            return label, doc, "deferred"
        try:
            if label == "ok_recheck":
                return label, doc, _recheck_ok(doc, per_company_budget_s)
            res = get_or_fetch(doc["company"],
                               canonical_employer_id=doc.get("canonical_employer_id"),
                               force=True, budget_s=per_company_budget_s)
            if res:
                return label, doc, "ok"
            status = (_coll().find_one({"key": doc["key"]}, {"status": 1}) or {}).get("status")
            return label, doc, "store_failed" if status == "pending" else "missing"
        except Exception as e:  # one company must never stop the run
            logger.warning("logo refresh failed for key %s: %s", doc.get("key"), type(e).__name__)
            return label, doc, "error"

    counts: Counter = Counter()
    if picked:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            for label, doc, outcome in pool.map(work, picked):
                counts[f"{label}:{outcome}"] += 1
                if on_result:
                    on_result(label, doc, outcome)
    return {"picked": len(picked), "outcomes": dict(sorted(counts.items())),
            "resolver_version": RESOLVER_VERSION,
            "seconds": round(time.monotonic() - started, 1)}


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
