"""Find ATS tenants without owning a list of companies first.

The problem this solves
-----------------------
ADR-004 §2.1 chose fingerprinting over slug-guessing, and it was right: to learn
which ATS an employer uses, ask the employer's own website. But fingerprinting
is **company-first** — it can only reach employers we already know exist and
whose domain we already have. That is the binding constraint on the long tail:
`companies` is empty, state registries publish names without domains, and the
employers a job seeker most wants (small firms with few applicants) are exactly
the ones missing from every company list we can buy or harvest.

This module inverts the direction. Many ATS vendors give every customer their
own hostname:

    <tenant>.bamboohr.com        <tenant>.icims.com        <tenant>.applytojob.com
    <tenant>.breezy.hr           <tenant>.csod.com         <tenant>.dayforcehcm.com

Those hostnames are **public record**, in two independent archives that exist for
other reasons and that we do not have to crawl the web to read:

  * **Common Crawl's URL index** — every URL its crawler has fetched, queryable
    by host pattern. Gives hostnames that really serve pages.
  * **Certificate Transparency** — every TLS certificate ever issued, and a
    per-tenant hostname needs one. Gives hostnames that exist even if no crawler
    reached them.

So instead of "find 4,000 companies, then discover that 60 of them use iCIMS",
we read the tenant list off the vendor directly and work backwards to the
employer. It is vendor-first discovery, and it is the only channel that scales
to the employers nobody has heard of — which is the entire point.

What this module does NOT do
----------------------------
It does not decide that a tenant is real. A hostname in an archive is a
*candidate*: `*.icims.com` is wildcard DNS, Common Crawl records URLs that have
since 404'd, and a certificate outlives the customer that needed it. Only
`provider.validate()` promotes a candidate, exactly as ADR-004 §3.2 requires —
"only a successful content fetch does". Everything here returns candidates.

Nor does it guess. Every hostname returned was observed by a third party; none
is synthesised from a company name.
"""
from __future__ import annotations

import json
import logging
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

logger = logging.getLogger(__name__)

# A polite, identifiable agent. Both services below are public research
# archives; both ask that clients identify themselves and back off on 5xx.
USER_AGENT = ("JobPilotBot/1.0 (+https://aspirely.me; employer discovery; "
              "contact: support@aspirely.me)")

_CC_COLLINFO = "https://index.commoncrawl.org/collinfo.json"
_CC_INDEX = "https://index.commoncrawl.org/{index}-index"
_CERTSPOTTER = ("https://api.certspotter.com/v1/issuances"
                "?domain={domain}&include_subdomains=true&expand=dns_names")
_CRTSH = "https://crt.sh/?q=%25.{domain}&output=json&exclude=expired"

#: Labels that are vendor infrastructure on every multi-tenant ATS domain, not
#: an employer. Kept here rather than per provider because the set is the same
#: everywhere and a missed one manufactures a bogus endpoint on every vendor.
GENERIC_LABELS = frozenset({
    "www", "api", "app", "apps", "admin", "static", "assets", "cdn", "cdn01",
    "cdn02", "cdn03", "img", "images", "media", "files", "uploads", "docs",
    "doc", "help", "support", "status", "blog", "news", "resources", "about",
    "marketing", "marketplace", "partners", "developer", "developers", "dev",
    "test", "staging", "stage", "sandbox", "demo", "qa", "uat", "login",
    "auth", "sso", "id", "account", "accounts", "config", "provider", "mail",
    "smtp", "mx", "ns1", "ns2", "vpn", "secure", "static01", "smetrics",
    "metrics", "analytics", "track", "tracking", "email", "go", "get", "try",
    "my", "portal", "public", "internal", "private", "legacy", "old", "new",
})


class DiscoveryError(RuntimeError):
    """A source could not be read. Never fatal: sources are independent."""


@dataclass
class TenantCandidate:
    """One hostname that MIGHT be an employer's board on `vendor`.

    `sources` is a set rather than a single value because a hostname seen by
    both Common Crawl and Certificate Transparency is materially more likely to
    be a live portal than one seen by either alone, and the validator uses that
    to order its work.
    """

    vendor: str
    host: str
    label: str
    sources: Set[str] = field(default_factory=set)

    #: Ordering weight per source. A host seen under the vendor's BOARD path is
    #: a job board by construction; a host seen anywhere on the domain might be
    #: the vendor's billing portal; a certificate proves only that a name
    #: existed. Ranking by count alone made these indistinguishable, so an
    #: alphabetical tie-break sent the validator through `academy`, `agents`,
    #: `api-dev`, `autodiscover`, `billing`… and spent an entire 8-minute budget
    #: without reaching one employer.
    _WEIGHTS = {"commoncrawl:board": 100, "commoncrawl": 10, "certspotter": 1,
                "crtsh": 1}

    @property
    def confidence(self) -> int:
        return sum(self._WEIGHTS.get(s, 1) for s in self.sources)

    def to_doc(self) -> Dict[str, Any]:
        return {"vendor": self.vendor, "host": self.host, "tenant": self.label,
                "sources": sorted(self.sources)}


# ---------------------------------------------------------------------------
# HTTP with manners
# ---------------------------------------------------------------------------

def _get(url: str, *, timeout: float = 90.0, attempts: int = 4,
         backoff: float = 3.0) -> bytes:
    """GET with retry on the transient 5xx these archives return under load.

    Both endpoints answer 503/504 when busy rather than queueing, and a single
    unretried failure silently costs a whole vendor's tenant list — the caller
    would see "0 tenants" and conclude the vendor has none.
    """
    last = ""
    for attempt in range(attempts):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            last = f"HTTP {exc.code}"
            # 4xx other than 429 will not become true by waiting.
            if exc.code < 500 and exc.code != 429:
                raise DiscoveryError(f"{url}: {last}") from exc
        except Exception as exc:
            last = f"{type(exc).__name__}: {exc}"
        if attempt < attempts - 1:
            time.sleep(backoff * (2 ** attempt))
    raise DiscoveryError(f"{url}: {last} after {attempts} attempts")


# ---------------------------------------------------------------------------
# Common Crawl
# ---------------------------------------------------------------------------

_INDEX_CACHE: Optional[str] = None


def latest_index() -> str:
    """Newest Common Crawl index id, e.g. `CC-MAIN-2026-30`."""
    return recent_indexes(1)[0]


_INDEXES_CACHE: List[str] = []


def recent_indexes(count: int = 1) -> List[str]:
    """The `count` newest monthly crawl ids, newest first.

    Each monthly crawl samples the web independently, so a tenant missing from
    one is often present in another — accumulating across a few snapshots is
    the cheapest way to grow the candidate pool without touching the employer's
    own site. It is also why this is capped and paced rather than run over every
    archive: these are free public research indexes and the polite budget is
    small.
    """
    global _INDEXES_CACHE
    if len(_INDEXES_CACHE) < count:
        data = json.loads(_get(_CC_COLLINFO, timeout=45)
                          .decode("utf-8", "replace"))
        if not data:
            raise DiscoveryError("collinfo.json was empty")
        _INDEXES_CACHE = [str(c["id"]) for c in data if c.get("id")]
    if not _INDEXES_CACHE:
        raise DiscoveryError("no Common Crawl indexes available")
    return _INDEXES_CACHE[:max(1, count)]


def commoncrawl_page_count(pattern: str, *, index: Optional[str] = None
                           ) -> Optional[int]:
    """How many pages the index holds for `pattern`, or None if it will not say.

    Asking first matters for two reasons. A `page=` beyond the end is an HTTP
    **400**, which is indistinguishable from a malformed query in the logs; and
    for a small vendor domain the whole tenant list is one page, so five extra
    requests are five extra chances to be rate-limited for nothing.
    """
    idx = index or latest_index()
    url = (f"{_CC_INDEX.format(index=idx)}?url={urllib.parse.quote(pattern)}"
           "&output=json&showNumPages=true")
    try:
        data = json.loads(_get(url, timeout=60, attempts=2)
                          .decode("utf-8", "replace"))
    except (DiscoveryError, ValueError):
        # Large domains (icims.com) time this out. Unknown, not zero.
        return None
    try:
        return int(data.get("pages") or 0)
    except (TypeError, ValueError):
        return None


def commoncrawl_hosts(pattern: str, *, index: Optional[str] = None,
                      pages: int = 6, page_size: int = 200,
                      pause: float = 2.0) -> Set[str]:
    """Hostnames Common Crawl has fetched under `pattern` (e.g. `*.icims.com`).

    Paged deliberately small and defensively. The index answers a large query
    with a 504 often enough that one big request is *less* productive than
    several small ones, and a page that fails is skipped rather than aborting
    the vendor — partial tenant coverage beats none.

    The unpaged form is tried FIRST and is the one that works most reliably.
    `page=` is only used to reach past it, and only as far as the index says it
    goes, because a page beyond the end is a 400 rather than an empty result.
    """
    idx = index or latest_index()
    base = _CC_INDEX.format(index=idx)
    hosts: Set[str] = set()

    def _absorb(body: bytes) -> int:
        added = 0
        for line in body.decode("utf-8", "replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            host = urllib.parse.urlsplit(rec.get("url") or "").hostname
            if host:
                host = host.lower()
                if host not in hosts:
                    added += 1
                hosts.add(host)
        return added

    # Pass 1: no `page` parameter. This is the form that survives best.
    try:
        _absorb(_get(f"{base}?url={urllib.parse.quote(pattern)}&output=json"
                     f"&fl=url&limit={int(page_size)}"))
    except DiscoveryError as exc:
        logger.info("commoncrawl: %s unpaged query unavailable (%s)",
                    pattern, exc)

    # Pass 2: walk pages, but never past the end the index reports.
    available = commoncrawl_page_count(pattern, index=idx)
    limit = max(1, int(pages))
    if available is not None:
        limit = min(limit, max(0, available))
    for page in range(limit):
        time.sleep(pause)
        url = (f"{base}?url={urllib.parse.quote(pattern)}&output=json"
               f"&fl=url&limit={int(page_size)}&page={page}")
        try:
            added = _absorb(_get(url))
        except DiscoveryError as exc:
            logger.info("commoncrawl: %s page %d unavailable (%s)",
                        pattern, page, exc)
            continue
        # Pages come in order; nothing new means we have read what it holds.
        if added == 0 and page > 0:
            break
    return hosts


# ---------------------------------------------------------------------------
# Certificate Transparency
# ---------------------------------------------------------------------------

def certspotter_hosts(domain: str) -> Set[str]:
    """Subdomains of `domain` that have ever been issued a certificate."""
    body = _get(_CERTSPOTTER.format(domain=urllib.parse.quote(domain)),
                timeout=60)
    hosts: Set[str] = set()
    try:
        rows = json.loads(body.decode("utf-8", "replace"))
    except ValueError as exc:
        raise DiscoveryError(f"certspotter: unparsable response ({exc})")
    for row in rows if isinstance(rows, list) else []:
        for name in (row.get("dns_names") or []):
            name = str(name).strip().lower().lstrip("*.")
            if name.endswith("." + domain):
                hosts.add(name)
    return hosts


def crtsh_hosts(domain: str) -> Set[str]:
    """Same question, different log aggregator.

    crt.sh is frequently unavailable and is therefore a supplement, never the
    only source; callers treat a `DiscoveryError` here as "no extra rows".
    """
    body = _get(_CRTSH.format(domain=urllib.parse.quote(domain)), timeout=120,
                attempts=2)
    hosts: Set[str] = set()
    try:
        rows = json.loads(body.decode("utf-8", "replace"))
    except ValueError as exc:
        raise DiscoveryError(f"crt.sh: unparsable response ({exc})")
    for row in rows if isinstance(rows, list) else []:
        for name in str(row.get("name_value") or "").splitlines():
            name = name.strip().lower().lstrip("*.")
            if name.endswith("." + domain):
                hosts.add(name)
    return hosts


# ---------------------------------------------------------------------------
# Vendor-first discovery
# ---------------------------------------------------------------------------

_LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9\-_]{0,62}$")


def _label_of(host: str, domain: str) -> str:
    """The tenant label in `<label>.<domain>`, or "" if `host` is deeper."""
    suffix = "." + domain
    if not host.endswith(suffix):
        return ""
    label = host[: -len(suffix)]
    # A deeper name (`a.b.icims.com`) is not a tenant hostname.
    if not label or "." in label:
        return ""
    return label


def discover_vendor_tenants(vendor: str, domain: str, *,
                            use_commoncrawl: bool = True,
                            use_certspotter: bool = True,
                            use_crtsh: bool = False,
                            cc_pages: int = 6,
                            cc_snapshots: int = 1,
                            path_pattern: str = "",
                            exclude: Iterable[str] = (),
                            ) -> List[TenantCandidate]:
    """Candidate tenant hostnames for a vendor whose tenants are subdomains.

    Returns candidates ordered by how many independent archives saw them, so a
    caller with a limited validation budget spends it on the likeliest first.
    """
    domain = domain.strip().lower().lstrip(".")
    skip = set(GENERIC_LABELS) | {str(x).strip().lower() for x in exclude}
    found: Dict[str, TenantCandidate] = {}

    def add(host: str, source: str) -> None:
        host = host.strip().lower()
        label = _label_of(host, domain)
        if not label or label in skip or not _LABEL_RE.match(label):
            return
        cand = found.get(host)
        if cand is None:
            cand = TenantCandidate(vendor=vendor, host=host, label=label)
            found[host] = cand
        cand.sources.add(source)

    if use_commoncrawl:
        # A PATH-scoped pattern is both cheaper and more precise where a vendor
        # has one. `*.icims.com` is large enough that the index times out on it
        # (so iCIMS enumeration returned only Certificate Transparency's view,
        # which for a wildcard-certificate vendor is the vendor's OWN
        # infrastructure and not one employer). `*.icims.com/jobs/*` is four
        # pages, and every host it returns is by construction a job board.
        patterns = [f"*.{domain}{path_pattern}"] if path_pattern else []
        patterns.append(f"*.{domain}")
        try:
            snapshots = recent_indexes(max(1, int(cc_snapshots)))
        except DiscoveryError as exc:
            logger.warning("tenant_discovery: no crawl index (%s)", exc)
            snapshots = []
        for pattern in patterns:
            answered = False
            for index in snapshots:
                try:
                    before = len(found)
                    scoped = bool(path_pattern) and pattern.endswith(path_pattern)
                    label = "commoncrawl:board" if scoped else "commoncrawl"
                    for host in commoncrawl_hosts(pattern, index=index,
                                                  pages=cc_pages):
                        add(host, label)
                    gained = len(found) - before
                    logger.info("tenant_discovery: %s @%s -> %d new hosts",
                                pattern, index, gained)
                    answered = True
                except DiscoveryError as exc:
                    logger.warning("tenant_discovery: commoncrawl %s @%s "
                                   "failed (%s)", pattern, index, exc)
            if answered and path_pattern and pattern.endswith(path_pattern):
                # The scoped query answered; the broad one would only add
                # infrastructure hosts and risk a timeout.
                break
    if use_certspotter:
        try:
            for host in certspotter_hosts(domain):
                add(host, "certspotter")
        except DiscoveryError as exc:
            logger.warning("tenant_discovery: certspotter %s failed (%s)",
                           domain, exc)
    if use_crtsh:
        try:
            for host in crtsh_hosts(domain):
                add(host, "crtsh")
        except DiscoveryError as exc:
            logger.info("tenant_discovery: crt.sh %s unavailable (%s)",
                        domain, exc)

    return sorted(found.values(),
                  key=lambda c: (-c.confidence, c.host))


#: Vendors whose tenants are their own hostname under one vendor domain, so
#: this module can enumerate them. A vendor NOT listed here (Greenhouse, Lever,
#: Ashby, …) puts every tenant on one shared host as a path segment, which
#: neither archive can enumerate — those are reached by fingerprinting instead.
#: `(vendor, tenant domain, board path pattern)`.
#:
#: The path is optional and exists for vendors whose bare domain is too large
#: for the Common Crawl index to answer. It also raises precision: a host that
#: appears under `/jobs/` is a board, whereas a host that merely appears under
#: the domain may be the vendor's billing portal.
SUBDOMAIN_VENDORS: Tuple[Tuple[str, str, str], ...] = (
    ("icims", "icims.com", "/jobs/*"),
    ("bamboohr", "bamboohr.com", "/careers*"),
    ("taleo", "taleo.net", "/careersection/*"),
    ("recruitee", "recruitee.com", ""),
    ("jazzhr", "applytojob.com", "/apply*"),
    ("cornerstone", "csod.com", "/ux/ats/careersite/*"),
)


def discover_all(vendors: Optional[Sequence[str]] = None, **kwargs
                 ) -> Dict[str, List[TenantCandidate]]:
    """Run `discover_vendor_tenants` for every enumerable vendor."""
    wanted = {v.strip().lower() for v in vendors} if vendors else None
    out: Dict[str, List[TenantCandidate]] = {}
    for vendor, domain, path in SUBDOMAIN_VENDORS:
        if wanted and vendor not in wanted:
            continue
        out[vendor] = discover_vendor_tenants(vendor, domain,
                                              path_pattern=path, **kwargs)
    return out
