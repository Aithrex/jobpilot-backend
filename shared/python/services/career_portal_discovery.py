"""Career portal discovery — turn "a company exists" into "a board we crawl".

The core inversion (ADR-004 §2.1)
---------------------------------
The obvious approach is to GUESS: for each company try
`boards.greenhouse.io/{slug}`, `jobs.lever.co/{slug}`,
`{slug}.wd1.myworkdayjobs.com` ... That is O(companies x vendors x host-variants)
- Workday alone shards tenants across `wd1`..`wd103`, so ruling out one vendor
for one company costs 103 requests - and it is wrong often enough to be useless,
because tenant slugs frequently do not match company names (Bread Financial's
Workday tenant is `alliancedata`).

Fingerprinting inverts it. Fetch the company's own site, find its careers page,
and match every link and script src against the vendor patterns declared by the
providers. The employer TELLS US which ATS they use and what their tenant is, in
the href of their own "View openings" button. Two requests per company, all
vendors evaluated at once, and it works regardless of how unrelated the slug is
to the name.

It also discovers vendors we have not implemented: recurring unmatched
ATS-looking hosts are counted in `unknown_hosts` so the next vendor to build is
chosen from evidence rather than intuition.

Politeness
----------
This crawls small businesses' own websites, so it is deliberately conservative:
robots.txt is honoured and cached, requests are paced per host, HTML is size
capped, and at most a handful of pages are read per company. A discovery pass
has no deadline to race - being slow is fine, being rude is not.
"""
from __future__ import annotations

import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
import re
import socket
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple
from urllib.parse import urljoin, urlparse

from services.ats_providers import registry as ats_registry
from services.ats_providers.base import (STATUS_BLOCKED, STATUS_CANDIDATE,
                                         ChallengeError, Endpoint,
                                         FetchContext, ValidationResult)

logger = logging.getLogger(__name__)

#: Paths tried on a company domain, in order. Ordered by observed hit rate, and
#: kept short because every entry is a request against a small business's site.
CAREER_PATHS: Tuple[str, ...] = (
    "/careers", "/jobs", "/company/careers", "/about/careers",
    "/careers/jobs", "/en/careers", "/about-us/careers", "/join-us",
    "/work-with-us", "/employment", "/opportunities", "/careers/open-positions",
)

#: Anchor text that marks a careers link when we crawl the homepage.
_CAREER_TEXT_RE = re.compile(
    r"\b(careers?|jobs?|join\s+(?:us|our\s+team)|work\s+(?:with|for)\s+us|"
    r"open\s+(?:roles|positions)|we[''']?re\s+hiring|employment|vacancies)\b",
    re.IGNORECASE)

_HREF_RE = re.compile(r"""(?:href|src|action|content|data-url)\s*=\s*["']([^"']{4,400})["']""",
                      re.IGNORECASE)
#: Bare URLs in inline JS/JSON config blobs — several site builders inject the
#: ATS endpoint there rather than as an href.
_BARE_URL_RE = re.compile(r"""https?://[A-Za-z0-9.\-]+\.[A-Za-z]{2,}[^\s"'<>)\]]{0,300}""")

MAX_HTML_BYTES = 1_500_000
MAX_PAGES_PER_COMPANY = 6

#: Hosts that are never an employer's own site and would waste a fetch.
_SKIP_HOST_RE = re.compile(
    r"(?:^|\.)(?:facebook|twitter|x|linkedin|instagram|youtube|tiktok|"
    r"google|googleapis|gstatic|doubleclick|cloudflare|jquery|bootstrapcdn|"
    r"fontawesome|w3\.org|schema\.org|wordpress|wixsite|godaddy)\.",
    re.IGNORECASE)


# ---------------------------------------------------------------------------
# robots.txt
# ---------------------------------------------------------------------------

class RobotsCache:
    """Per-run robots.txt cache.

    Fail-OPEN on a fetch error and fail-CLOSED on an explicit Disallow. A site
    that does not answer has not refused; a site that says no has.
    """

    def __init__(self, ctx: FetchContext, user_agent: str = "JobPilotIngest"):
        self._ctx = ctx
        self._ua = user_agent
        self._cache: Dict[str, Any] = {}

    def allows(self, url: str) -> bool:
        try:
            parsed = urlparse(url)
            origin = f"{parsed.scheme}://{parsed.netloc}"
        except Exception:
            return False
        parser = self._cache.get(origin)
        if parser is None:
            parser = self._load(origin)
            self._cache[origin] = parser
        if parser is False:
            return True          # no robots.txt / unreadable -> allowed
        try:
            return bool(parser.can_fetch(self._ua, url))
        except Exception:
            return True

    def _load(self, origin: str) -> Any:
        from urllib.robotparser import RobotFileParser
        status, body = self._ctx.get_text(f"{origin}/robots.txt", timeout=6.0,
                                          vendor="discovery")
        if status != 200 or not body:
            return False
        parser = RobotFileParser()
        try:
            parser.parse(body[:200_000].splitlines())
        except Exception:
            return False
        return parser


# ---------------------------------------------------------------------------
# Domain resolution
# ---------------------------------------------------------------------------

_LEGAL_WORDS = re.compile(
    r"\b(llc|inc|corp|corporation|company|co|ltd|limited|lp|llp|pllc|pc|the)\b",
    re.IGNORECASE)


def candidate_domains(name: str, known: str = "") -> List[str]:
    """Plausible domains for a company, best first.

    Guessing a domain is far cheaper and more accurate than guessing an ATS
    slug (companies overwhelmingly own `<theirname>.com`), and every guess is
    confirmed by checking that the fetched page actually names the company -
    see `_page_mentions`. A wrong guess costs one HEAD-ish request and is
    discarded; it never produces an endpoint on its own.
    """
    out_known: List[str] = []
    if known:
        host = known.strip().lower()
        host = re.sub(r"^https?://", "", host).split("/")[0]
        if host:
            # FIRST, not ONLY. Returning just the stored value meant a bad
            # stored domain suppressed every derivable guess as well as
            # skipping confirmation - two failures compounding, and the reason
            # `amazon-com.com` could park Amazon at zero endpoints forever.
            out_known = [host]
    base = _LEGAL_WORDS.sub(" ", str(name or "").lower())
    base = re.sub(r"[^a-z0-9\s\-]", " ", base)
    tokens = [t for t in base.split() if t]
    if not tokens:
        return out_known
    joined = "".join(tokens)
    hyphen = "-".join(tokens)
    first = tokens[0] if len(tokens) > 1 else ""

    # BARE stems before their `www.` twins, and the first-token stem right
    # after the joined one. The old order was
    # [joined, www.joined, hyphen, www.hyphen, first] truncated to 4, which put
    # the single-word form - the one companies with a multi-word legal name
    # actually own - off the end of the list entirely.
    #
    # It cost real employers. Live discovery run, 2026-08-27:
    #   PALANTIR TECHNOLOGIES INC -> settled on `palantir-technologies.com`
    #                                and reported "no ATS link found";
    #                                `palantir.com` serves a Lever board.
    #   SAP SE                    -> settled on `www.sapse.com`; sap.com.
    # Both were then written as `no_portal`, which is terminal.
    # A DNS label is at most 63 characters. Registry names run far past that
    # ("ZION EVANGELICAL LUTHERAN CHURCH OF THE UNALTERED AUGSBURG CONFESSION
    # OF SOUTHINGTON"), and resolving such a label raises UnicodeError rather
    # than failing a lookup - which killed the attempt before it was recorded.
    stems = [t for t in dict.fromkeys([joined, first, hyphen])
             if t and 3 <= len(t) <= 63]
    out = [f"{stem}.com" for stem in stems]
    out += [f"www.{stem}.com" for stem in stems]
    return list(dict.fromkeys(out_known + out))[:6]


def _resolves(host: str) -> bool:
    try:
        socket.getaddrinfo(host, 443)
        return True
    except OSError:
        return False


def _name_tokens(name: str) -> List[str]:
    """Significant tokens of a company name (legal suffixes removed)."""
    return [t for t in re.split(r"\W+", str(name or "").lower())
            if len(t) > 3 and not _LEGAL_WORDS.match(t)]


def _page_mentions(body: str, name: str, *, host: str = "",
                   ticker: str = "") -> bool:
    """Cheap confirmation that a guessed domain belongs to this company.

    Guards the very common case where `<name>.com` is a parked domain or an
    unrelated business.

    Requires EVERY significant token, not just the longest one. The old
    longest-token rule confirmed "MICRON TECHNOLOGY INC" from any page saying
    "technology", which is most pages on the internet; a multi-token name is
    the main thing that makes a guess checkable at all, and using one token
    threw that away.

    A ticker printed on the page is accepted as proof on its own, because a
    site that names the registrant's ticker is that registrant's site.

    KNOWN LIMITATION - two companies can share a name, and no amount of reading
    the homepage distinguishes them. Live example from this registry:
    `alphabet.com` was stored as the domain for **Alphabet Inc.** (Google's
    parent) and is actually **BMW's Alphabet**, the fleet-leasing business. Its
    homepage says "Alphabet", so every text rule here passes it. What protects
    the inventory in that case is not this function but the fact that a guessed
    domain is now re-confirmed on every run rather than trusted once and stored
    forever (see `_resolve_domain`), plus the catalogs, which reach the large
    ambiguous names by fingerprint rather than by guess.
    """
    low = (body or "").lower()
    tick = str(ticker or "").strip().lower()
    if tick and len(tick) >= 3 and re.search(rf"\b{re.escape(tick)}\b", low):
        return True
    tokens = _name_tokens(name)
    if not tokens:
        return True
    if all(t in low for t in tokens):
        return True
    # A host whose registrable stem IS the name, on a page that says it, is the
    # ordinary "cognex.com belongs to COGNEX CORP" case that all-token matching
    # would be too strict for.
    #
    # SINGLE-token names only. Allowing it for multi-token names re-opens the
    # hole: `candidate_domains("Advanced Micro Devices Inc")` offers
    # `advanced.com`, whose stem is a name token and whose page says
    # "advanced", so the guess would confirm against an unrelated company.
    if len(tokens) != 1:
        return False
    stem = re.sub(r"^www\.", "", str(host or "").lower()).split(".")[0]
    return bool(stem) and stem == tokens[0] and stem in low


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------

@dataclass
class DiscoveryResult:
    company_id: str = ""
    name: str = ""
    domain: str = ""
    endpoints: List[Endpoint] = field(default_factory=list)
    validated: List[Tuple[Endpoint, ValidationResult]] = field(default_factory=list)
    pages_fetched: int = 0
    #: Of those, how many were a CAREERS page rather than marketing pages off
    #: the homepage. `no_portal` is gated on this - see `_persist`.
    career_pages_read: int = 0
    unknown_hosts: Dict[str, int] = field(default_factory=dict)
    blocked: bool = False
    detail: str = ""

    @property
    def found(self) -> int:
        return len(self.endpoints)

    @property
    def working(self) -> int:
        return sum(1 for _e, r in self.validated if r.ok)


# ---------------------------------------------------------------------------
# The engine
# ---------------------------------------------------------------------------

#: Hosts that clearly belong to SOME ATS but matched no provider. Surfaced so
#: the next vendor to implement is picked from evidence.
_ATS_HINT_RE = re.compile(
    r"(?:^|\.)(?:jobs?|careers?|recruit\w*|talent|hire|apply|workforce|hcm|ats)"
    r"[a-z0-9\-]*\.[a-z]{2,}$", re.IGNORECASE)


#: Total slug-guess requests one company may spend, across ALL vendors.
#:
#: Slug probing only runs when fingerprinting found nothing, which is the
#: majority of companies — so its cost is paid on almost every row. Unbounded it
#: was 8 vendors x 2 slugs = 16 requests, and every one lands on a SHARED board
#: API host (`boards-api.greenhouse.io`, `apply.workable.com`, `api.lever.co`).
#: With 16 companies in flight that is a burst of ~256 requests at six hosts,
#: which is both the throughput bottleneck (they serialise on one host bucket)
#: and rude: a live pass earned `apply.workable.com challenged, cooling down
#: 900s`, taking Workable offline for every OTHER company too.
#:
#: A budget makes the cost per company bounded and predictable. It is spent in
#: `_SLUG_PROBE_ORDER`, so the vendors most likely to hit go first and a company
#: that is on Greenhouse is still found on the first request.
SLUG_PROBE_BUDGET = int(os.getenv("DISCOVERY_SLUG_PROBE_BUDGET", "6") or 6)

#: Probe order, most-likely-first. Greenhouse and Lever dominate the population
#: this fallback exists for (JS-shell careers pages at tech companies); the
#: per-tenant-subdomain vendors are last because a miss there is a DNS lookup
#: rather than a cheap 404 on a shared host.
_SLUG_PROBE_ORDER = ("greenhouse", "lever", "ashby", "smartrecruiters",
                     "workable", "recruitee", "rippling", "bamboohr")


def _slug_probe_order() -> List[Any]:
    """Registered providers, ordered for probing."""
    providers = {p.vendor: p for p in ats_registry.all_providers()}
    ordered = [providers.pop(v) for v in _SLUG_PROBE_ORDER if v in providers]
    return ordered + sorted(providers.values(), key=lambda p: p.vendor)


class PortalDiscovery:
    def __init__(self, ctx: FetchContext, *, validate: bool = True,
                 max_pages: int = MAX_PAGES_PER_COMPANY):
        self.ctx = ctx
        self.robots = RobotsCache(ctx)
        self.validate = validate
        self.max_pages = max(1, int(max_pages))
        self.last_status: Any = None

    # -- fetching ------------------------------------------------------------

    def _get_html(self, url: str) -> Optional[str]:
        if not self.robots.allows(url):
            self.last_status = "robots"
            logger.debug("discovery: robots disallow %s", url)
            return None
        status, body = self.ctx.get_text(url, timeout=10.0, vendor="discovery")
        # Recorded so "found nothing" can be told apart from "was never let in".
        # Corporate sites behind a bot wall return 403/503 to any non-browser
        # UA, and that is a completely different problem from an employer who
        # genuinely has no careers page - it needs a different fix, so the two
        # must not look identical in the results.
        self.last_status = status if status is not None else "network"
        if status != 200 or not body:
            return None
        return body[:MAX_HTML_BYTES]

    # -- link extraction -----------------------------------------------------

    @staticmethod
    def extract_urls(html: str, base_url: str) -> List[str]:
        """Every absolute URL a page references.

        Attributes AND bare URLs in inline script/JSON, because site builders
        (Wix, Squarespace, Webflow) frequently inject the ATS embed as a config
        value rather than as an anchor href - and that config value is exactly
        the link that names the tenant.
        """
        out: List[str] = []
        seen: Set[str] = set()

        def add(raw: str) -> None:
            raw = (raw or "").strip()
            if not raw or raw.startswith(("data:", "javascript:", "mailto:",
                                          "tel:", "#")):
                return
            try:
                absolute = urljoin(base_url, raw)
            except Exception:
                return
            if not absolute.startswith(("http://", "https://")):
                return
            if absolute not in seen:
                seen.add(absolute)
                out.append(absolute[:600])

        for m in _HREF_RE.finditer(html):
            add(m.group(1))
        for m in _BARE_URL_RE.finditer(html):
            add(m.group(0))
        return out

    @staticmethod
    def career_links(html: str, base_url: str) -> List[str]:
        """Same-site links whose text or href looks like a careers page."""
        try:
            from lxml import html as lxml_html
            doc = lxml_html.fromstring(html)
        except Exception:
            return []
        base_host = (urlparse(base_url).hostname or "").lower()
        out: List[str] = []
        seen: Set[str] = set()
        for a in doc.xpath("//a[@href]"):
            href = (a.get("href") or "").strip()
            if not href or href.startswith(("#", "mailto:", "tel:")):
                continue
            text = " ".join((a.text_content() or "").split())[:120]
            if not (_CAREER_TEXT_RE.search(text) or _CAREER_TEXT_RE.search(href)):
                continue
            try:
                absolute = urljoin(base_url, href)
            except Exception:
                continue
            host = (urlparse(absolute).hostname or "").lower()
            # Off-site careers links are the JACKPOT (that IS the ATS URL), so
            # they are kept; same-site ones are pages to crawl next.
            if absolute in seen:
                continue
            seen.add(absolute)
            out.append(absolute)
            if len(out) >= 12:
                break
        return out

    # -- the pass ------------------------------------------------------------

    def discover(self, name: str, *, domain: str = "",
                 company_id: str = "", ticker: str = "") -> DiscoveryResult:
        res = DiscoveryResult(company_id=company_id, name=name)
        host = self._resolve_domain(name, domain, res, ticker=ticker)
        if not host:
            res.detail = res.detail or "no reachable domain"
            return res
        res.domain = host

        collected: Dict[str, Endpoint] = {}
        urls_seen: List[str] = []
        pages = 0

        home_html = self._get_html(f"https://{host}/")
        if home_html:
            pages += 1
            urls_seen.extend(self.extract_urls(home_html, f"https://{host}/"))
            targets = self.career_links(home_html, f"https://{host}/")
        else:
            targets = []

        # Homepage links first (they point where the employer actually publishes
        # jobs), then the conventional paths as a fallback.
        for path in CAREER_PATHS:
            targets.append(f"https://{host}{path}")

        visited: Set[str] = {f"https://{host}/"}
        queue: List[Tuple[str, int]] = [(t, 1) for t in targets]

        while queue:
            if pages >= self.max_pages or self.ctx.should_stop():
                break
            target, depth = queue.pop(0)
            if target in visited:
                continue
            visited.add(target)
            thost = (urlparse(target).hostname or "").lower()
            if not thost or _SKIP_HOST_RE.search(thost):
                continue
            # An off-site careers link may itself already BE the ATS URL, in
            # which case fingerprint it directly and skip the fetch entirely.
            if thost != host and not thost.endswith(f".{host}"):
                if ats_registry.match_url(target):
                    urls_seen.append(target)
                    continue
            html = self._get_html(target)
            if not html:
                continue
            pages += 1
            # Did we actually reach a CAREERS page, or just more marketing
            # site? `no_portal` is only a claim we can make about an employer
            # whose careers page we read, so the two are counted separately.
            if _CAREER_TEXT_RE.search(urlparse(target).path or "") or \
                    _CAREER_TEXT_RE.search(target):
                res.career_pages_read += 1
            urls_seen.append(target)
            urls_seen.extend(self.extract_urls(html, target))

            # Second hop. Large employers routinely put the ATS link one level
            # below /careers ("Search open roles" -> /careers/search -> the
            # board), so stopping at depth 1 misses them entirely. Only taken
            # while nothing has matched yet, so a company whose board we already
            # found costs no extra requests.
            if depth < 2 and not ats_registry.endpoints_from_urls(urls_seen):
                for link in self.career_links(html, target)[:4]:
                    if link not in visited:
                        queue.append((link, depth + 1))

        res.pages_fetched = pages
        if pages == 0:
            res.detail = f"no page fetched (last={self.last_status})"
        elif not urls_seen:
            res.detail = "pages fetched but no links extracted"
        for ep in ats_registry.endpoints_from_urls(urls_seen):
            ep.company_id = company_id
            ep.display_name = name
            collected.setdefault(ep.member_key, ep)
        res.unknown_hosts = self._unknown_ats_hosts(urls_seen, host)

        # Fallback: a modern careers page is often a React shell that loads its
        # jobs by XHR, so the board URL is nowhere in the served HTML and
        # fingerprinting - which is otherwise the better strategy - finds
        # nothing. For the handful of vendors with ONE shared API host, a slug
        # guess is a single definitive request, so a bounded probe recovers
        # exactly these companies. Strictly a fallback: it runs only when
        # fingerprinting came up empty, and only for `slug_probe` vendors.
        if not collected:
            for ep in self._slug_probe(name, host):
                collected.setdefault(ep.member_key, ep)
            if collected:
                res.detail = "found by slug probe"

        for ep in collected.values():
            ep.company_id = company_id
            ep.display_name = name
        res.endpoints = list(collected.values())

        if self.validate and res.endpoints:
            res.validated = self._validate(res.endpoints)
            if any(r.status == STATUS_BLOCKED for _e, r in res.validated):
                res.blocked = True
        return res

    def _slug_probe(self, name: str, domain: str) -> List[Endpoint]:
        """Try a small set of likely slugs against single-host board APIs.

        Bounded on purpose: `slug_probe` vendors only (see AtsProvider), the
        first two candidate slugs, and a stop on the first hit. Worst case is a
        handful of requests against APIs built to be polled, versus the hundreds
        that guessing Workday hosts would cost - which is why those vendors opt
        out rather than being probed here.

        CORROBORATED on purpose. A slug that validates only proves a board
        exists, not that it is THIS company's board, and a wrong guess that
        validates does not look like an error - it looks like coverage.
        Measured on a live sweep: `greenhouse/super`, guessed from "Super Micro
        Computer, Inc.", is a real board serving real jobs for **Super
        Technologies**; `greenhouse/genius`, from "Genius Group Ltd", is an
        unrelated "Invite-Only Job Board". So a hit is kept only when the board
        NAMES this company (`provider.board_owner`, which Greenhouse and
        SmartRecruiters answer) or, for vendors that publish no owner name, when
        the slug is distinctive enough to stand alone (`slug_is_trustworthy`).
        """
        from services.ats_providers.util import names_match, slug_is_trustworthy
        found: List[Endpoint] = []
        spent = 0
        for provider in _slug_probe_order():
            if not getattr(provider, "slug_probe", False):
                continue
            if self.ctx.should_stop() or spent >= SLUG_PROBE_BUDGET:
                break
            for slug in list(provider.slug_candidates(name, domain))[:2]:
                if spent >= SLUG_PROBE_BUDGET:
                    break
                spent += 1
                if self.ctx.should_stop():
                    break
                ep = Endpoint(vendor=provider.vendor, tenant=slug,
                              display_name=name, status=STATUS_CANDIDATE)
                try:
                    result = provider.validate(ep, self.ctx)
                except ChallengeError:
                    break
                except Exception:
                    continue
                if not result.ok:
                    continue
                owner = None
                try:
                    owner = provider.board_owner(ep, self.ctx)
                except Exception:
                    owner = None
                if owner is not None:
                    if not names_match(name, owner):
                        logger.info("discovery: %s/%s validates but belongs to "
                                    "%r, not %r - not claiming it",
                                    provider.vendor, slug, owner, name)
                        continue
                elif not slug_is_trustworthy(name, slug):
                    # The vendor cannot say who owns the board and the slug is
                    # not distinctive enough to stand on its own.
                    continue
                ep.status = result.status
                found.append(ep)
                break
        return found

    def _resolve_domain(self, name: str, domain: str, res: DiscoveryResult,
                        ticker: str = "") -> str:
        """The company's own site, confirmed - never merely supplied.

        A stored `domain` used to be returned unverified ("caller-supplied:
        trust it"). It is not ground truth: the stored values were written by
        an EARLIER discovery guess, so the shortcut promoted one unconfirmed
        guess into permanent fact and skipped the only check that would have
        caught it. Two live examples from this registry: `AMAZON COM INC` was
        stored as `amazon-com.com`, a parked host that answers 202 with an
        empty page, and `Alphabet Inc.` as `alphabet.com`, which is BMW's
        fleet-leasing business.

        Confirming costs nothing extra: the homepage is fetched moments later
        anyway to read its career links, so this is the same request either
        way. A supplied domain still goes FIRST - it is a strong hint - it just
        no longer bypasses the check.
        """
        for cand in candidate_domains(name, domain):
            if self.ctx.should_stop():
                return ""
            if not _resolves(cand):
                continue
            html = self._get_html(f"https://{cand}/")
            if html is None:
                continue
            res.pages_fetched += 1
            if _page_mentions(html, name, host=cand, ticker=ticker):
                return cand
            res.detail = "domain did not confirm"
        return ""

    @staticmethod
    def _unknown_ats_hosts(urls: Iterable[str], own_host: str) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for url in urls:
            host = (urlparse(url).hostname or "").lower()
            if not host or host == own_host or host.endswith(f".{own_host}"):
                continue
            if _SKIP_HOST_RE.search(host):
                continue
            if not _ATS_HINT_RE.search(host):
                continue
            if ats_registry.match_url(url):
                continue
            counts[host] = counts.get(host, 0) + 1
        return counts

    def _validate(self,
                  endpoints: Sequence[Endpoint]
                  ) -> List[Tuple[Endpoint, ValidationResult]]:
        out: List[Tuple[Endpoint, ValidationResult]] = []
        for ep in endpoints:
            if self.ctx.should_stop():
                break
            provider = ats_registry.get(ep.vendor)
            if provider is None:
                continue
            try:
                result = provider.validate(ep, self.ctx)
            except ChallengeError as exc:
                result = ValidationResult(STATUS_BLOCKED, detail=str(exc))
            except Exception as exc:
                logger.debug("discovery: validate %s failed: %s",
                             ep.member_key, type(exc).__name__)
                continue
            ep.status = result.status
            out.append((ep, result))
        return out


# ---------------------------------------------------------------------------
# Batch driver
# ---------------------------------------------------------------------------

#: Companies fingerprinted concurrently inside ONE worker.
#:
#: Almost all of a company's 8-20s is network wait — resolve a domain, fetch the
#: site, fetch `/careers` — and each company is a DIFFERENT host, so this does
#: not concentrate load anywhere. It is the difference between draining the
#: 59,673-company queue in days and draining it in hours.
DISCOVERY_CONCURRENCY = int(os.getenv("DISCOVERY_CONCURRENCY", "16") or 16)
#: Consecutive challenged employers after which a batch stops making
#: requests (see `discover_batch`).
DISCOVERY_CHALLENGE_BREAKER = int(os.getenv("DISCOVERY_CHALLENGE_BREAKER", "6") or 6)


def discover_batch(companies: Sequence[Dict[str, Any]], ctx: FetchContext, *,
                   persist: bool = True,
                   validate: bool = True,
                   concurrency: Optional[int] = None) -> Dict[str, Any]:
    """Run discovery over a claimed queue batch and record the outcome.

    Persistence is per company rather than at the end so a run that hits its
    deadline still banks everything it learned - a discovery pass is expensive
    and losing it to a timeout would be the costliest possible failure.

    Companies are fingerprinted CONCURRENTLY. Each gets its own
    `PortalDiscovery`: the engine carries per-company state (`last_status`, a
    robots cache) that would race if shared, and a robots cache is per-host
    anyway so nothing is lost by not sharing it.
    """
    workers = max(1, int(concurrency if concurrency is not None
                         else DISCOVERY_CONCURRENCY))
    stats = {"companies": 0, "endpoints": 0, "validated": 0, "blocked": 0,
             "no_portal": 0, "errors": 0, "unknown_hosts": {}}
    lock = threading.Lock()

    #: Consecutive bot-challenges across DIFFERENT employers. One site saying
    #: 429 is that site; a run of them is the CDNs in front of them limiting
    #: OUR address, and every further request both fails and extends it.
    streak = {"challenges": 0}

    def _one(row: Dict[str, Any]) -> None:
        if ctx.should_stop():
            return
        if streak["challenges"] >= DISCOVERY_CHALLENGE_BREAKER:
            # Leave the rest of this batch unattempted. Their leases lapse and
            # a later tick takes them, by which time the limit has cooled.
            with lock:
                stats["skipped_rate_limited"] = stats.get("skipped_rate_limited", 0) + 1
            return
        name = str(row.get("name") or "")
        if not name:
            return
        # One employer must never end the batch.
        #
        # `discover` reaches out to a company's OWN website, and any of them may
        # rate-limit, hang or serve something that breaks a parser. A
        # `ChallengeError` from a single site used to propagate out of here and
        # kill the run: a live pass ended on a 429 from `ironmountain.com` after
        # 46 companies, abandoning the other 59,672. For an unattended worker
        # that is the difference between draining the queue and stopping on the
        # first unlucky host.
        try:
            engine = PortalDiscovery(ctx, validate=validate)
            result = engine.discover(name, domain=str(row.get("domain") or ""),
                                     ticker=str(row.get("ticker") or ""),
                                     company_id=str(row.get("company_id") or ""))
        except Exception as exc:
            challenged = isinstance(exc, ChallengeError)
            with lock:
                stats["companies"] += 1
                stats["errors"] += 1
                if challenged:
                    stats["blocked"] += 1
                    streak["challenges"] += 1
            logger.info("discovery: %s failed (%s: %s)", name,
                        type(exc).__name__, str(exc)[:120])
            # RECORD IT. Returning here left the row `running`: its lease
            # expired, it was claimed again, and it failed the same way. Live
            # 2026-10-06: 502 rows had been attempted 50+ times each, the
            # refill kept selecting those same top-priority companies, found
            # them un-rearmable, and reported `refilled: 0` on every tick -
            # 69,785 companies due a retry got none, and the queue finished
            # ONE company in 24 hours. `deferred` is the honest state: we did
            # not get a fair look, and it comes back after the cooldown
            # instead of in fifteen minutes.
            company_id = str(row.get("company_id") or "")
            if persist and company_id:
                try:
                    from services import company_registry_service as reg
                    reg.complete_discovery(
                        company_id, state=reg.DISCOVERY_DEFERRED,
                        detail=("challenge: " if challenged else "error: ")
                        + f"{type(exc).__name__}: {str(exc)[:160]}")
                except Exception as rec_exc:
                    logger.warning("discovery: could not record failure for %s: %s",
                                   name, type(rec_exc).__name__)
            return
        with lock:
            streak["challenges"] = 0

        with lock:
            stats["companies"] += 1
            stats["endpoints"] += result.found
            stats["validated"] += result.working
            if result.blocked:
                stats["blocked"] += 1
            for host, n in (result.unknown_hosts or {}).items():
                stats["unknown_hosts"][host] = \
                    stats["unknown_hosts"].get(host, 0) + n

        if not persist:
            return
        try:
            _persist(result)
        except Exception as exc:
            logger.warning("discovery: persist failed for %s: %s", name,
                           type(exc).__name__)

    rows = [r for r in companies if r]
    if workers == 1:
        for row in rows:
            if ctx.should_stop():
                break
            _one(row)
        return stats

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_one, row) for row in rows]
        for future in futures:
            try:
                future.result()
            except Exception:  # pragma: no cover - `_one` swallows its own
                with lock:
                    stats["errors"] += 1
    return stats


def _persist(result: DiscoveryResult) -> None:
    from services import company_registry_service as reg

    if result.domain and result.company_id:
        try:
            reg.upsert_company({"company_id": result.company_id,
                                "name": result.name, "domain": result.domain,
                                "source": "discovery"})
        except Exception:
            pass

    validated_keys = {e.member_key: r for e, r in result.validated}
    for ep in result.endpoints:
        key = reg.upsert_endpoint(ep, company_id=result.company_id,
                                  display_name=result.name,
                                  source="discovery")
        vr = validated_keys.get(key) or validated_keys.get(ep.member_key)
        if vr is not None:
            reg.record_validation(key, vr)

    if result.company_id:
        # `no_portal` is a CLAIM about the employer — "this company has no
        # careers page" — and it is terminal: `refill_queue` never looks at it
        # again. Recording it because we were rate-limited, bot-walled or could
        # not resolve the domain writes a false negative that no later run will
        # correct.
        #
        # That is not hypothetical. A burst at concurrency 48 got this host's IP
        # rate-limited by the CDNs in front of a large slice of the S&P 500;
        # every company probed during it returned zero endpoints and would have
        # been filed as having no careers page. Intuit, Iron Mountain, Crocs and
        # Qorvo all have one.
        #
        # So a result we could not trust is DEFERRED and tried again later.
        if result.endpoints:
            state = reg.DISCOVERY_DONE
        elif result.blocked or _inconclusive(result):
            state = reg.DISCOVERY_DEFERRED
        elif not result.career_pages_read:
            # We read the marketing site and never reached a careers page, so
            # we have not looked where the answer lives. Saying "this employer
            # has no careers page" on that basis is the same unfounded terminal
            # claim `scripts/requeue_unfounded_no_portal.py` had to repair 2,058
            # of - it just carries evidence now, which makes it auditable
            # rather than correct.
            state = reg.DISCOVERY_DEFERRED
            result.detail = (result.detail
                             or f"read {result.pages_fetched} page(s) on "
                                f"{result.domain or '?'}, none of them a "
                                f"careers page")
        else:
            state = reg.DISCOVERY_NO_PORTAL
        # A terminal state MUST carry its evidence. 2,058 of the 2,059
        # `no_portal` rows in the live queue recorded an empty detail, so
        # there is no way to tell from the record whether we ever fetched a
        # page - and the claim is unfalsifiable rather than merely wrong. It
        # matters most exactly where it is least believable: NVIDIA, Apple,
        # Microsoft, Salesforce, Broadcom, SAP, Dell, Palantir and Shopify
        # were all filed as having no careers page.
        detail = result.detail or (
            f"read {result.pages_fetched} page(s) on {result.domain or '?'}, "
            f"no ATS link found")
        reg.complete_discovery(result.company_id, state=state,
                               endpoints_found=result.found,
                               detail=detail)


#: Result details that mean "we did not get a fair look", not "no portal".
_INCONCLUSIVE = ("no reachable domain", "challenge", "429", "403", "robots",
                 "blocked", "timeout", "network")


def _inconclusive(result: "DiscoveryResult") -> bool:
    detail = str(result.detail or "").lower()
    if not result.domain:
        # We never even reached a site, so we learned nothing about the employer.
        return True
    return any(marker in detail for marker in _INCONCLUSIVE)


# ---------------------------------------------------------------------------
# Scheduled worker
# ---------------------------------------------------------------------------
#
# Discovery is not a script you run when you remember to. Measured on live data
# (2026-08-19): 20.8 s per company, 59,673 companies pending — 344 hours
# single-threaded. A one-off `employer_coverage.py discover` will never drain
# that, which is why 99.9% of the company registry had never been probed while
# the pipeline was, technically, "implemented".
#
# So it runs the way ingestion runs (ADR-003): an EventBridge tick fans out N
# bounded workers, each claiming a leased batch. The lease is what makes the
# fan-out safe — `claim_discovery_batch` hands a company to exactly one worker,
# so workers need no shard assignment and no coordination, and a worker that
# dies simply lets its lease lapse.
#
# The cost per company is almost all network wait (resolve a domain, fetch the
# site, fetch `/careers`), so concurrency converts directly into throughput:
# at these defaults the queue drains in hours rather than weeks.

#: Workers fanned out per tick. Matches `job_ingest_worker.MAX_SHARDS_PER_TICK`
#: so discovery and ingestion put comparable pressure on the account's Lambda
#: concurrency.
#:
#: The arithmetic, measured rather than assumed (2026-08-19): 4.85s per company
#: at `DISCOVERY_CONCURRENCY=8`, so one worker clears its 40-company batch in
#: ~194s inside a 240s budget. 16 workers x 40 companies x 4 ticks/hour =
#: 2,560 companies/hour, which puts a 59,673-company queue at roughly a day of
#: continuous running — and the queue keeps growing as the registry does, which
#: is exactly why this is a schedule and not a script.
DISCOVERY_WORKERS_PER_TICK = int(
    os.getenv("DISCOVERY_WORKERS_PER_TICK", "16") or 16)
#: Companies claimed per batch. Smaller than it looks like it should be on
#: purpose: the worker now loops batches until its deadline, and a SHORT batch
#: wastes less of the tail. With a 40-company batch a worker that has 90s left
#: cannot start one at all; with 25 it can.
DISCOVERY_BATCH_SIZE = int(os.getenv("DISCOVERY_BATCH_SIZE", "25") or 25)
DISCOVERY_BUDGET_SECONDS = float(
    os.getenv("DISCOVERY_BUDGET_SECONDS", "240") or 240)
#: Keep the queue deeper than one tick can consume, so workers never idle
#: waiting for a refill.
#: Slack left at the end of a worker's budget. The caller is a Lambda with a
#: hard 300s ceiling; a worker killed mid-batch loses its run record and holds
#: its leases until they lapse, so it stops with room rather than squeezing in
#: one more batch it might not finish.
#: Outbound requests per second across ALL hosts, for discovery only.
#:
#: Per-host pacing never engages here — every company is a different site — but
#: a large share of them sit behind a few CDNs that limit by SOURCE IP. Measured
#: at concurrency 48: twelve `429`s inside two seconds from unrelated companies
#: (intuit.com, crocs.com, callawaygolf.com, ncino.com), each then parked in a
#: 900s cooldown, so the company was lost for that run. Concurrency past this
#: point buys nothing and costs coverage.
DISCOVERY_GLOBAL_RATE = float(
    os.getenv("DISCOVERY_GLOBAL_RATE", "12") or 12)

DISCOVERY_RESERVE_SECONDS = float(
    os.getenv("DISCOVERY_RESERVE_SECONDS", "45") or 45)

DISCOVERY_REFILL_MULTIPLE = int(
    os.getenv("DISCOVERY_REFILL_MULTIPLE", "4") or 4)


def dispatch_due_discovery(*, limit: Optional[int] = None,
                           enqueue: Optional[Any] = None) -> Dict[str, Any]:
    """Refill the queue and fan out bounded discovery workers.

    Backpressure comes from the queue itself rather than from a shard table:
    if the previous tick's workers still hold their leases, `refill_queue` has
    less to add and the new workers claim smaller batches. Duplicate delivery of
    the tick is harmless — the companies it would re-select are leased.
    """
    from services import company_registry_service as reg

    workers = max(1, int(limit if limit is not None
                         else DISCOVERY_WORKERS_PER_TICK))
    want = workers * DISCOVERY_BATCH_SIZE * max(1, DISCOVERY_REFILL_MULTIPLE)
    try:
        refilled = reg.refill_queue(limit=want)
    except Exception as exc:
        logger.warning("discovery dispatch: refill failed (%s)",
                       type(exc).__name__)
        refilled = 0

    if enqueue is None:
        def enqueue(job_type: str, payload: Dict[str, Any]) -> str:
            from services.resume_service import ResumeService, get_resume_service
            job_id = get_resume_service().create_job(job_type, payload,
                                                     "system@internal")
            ResumeService.invoke_async(job_id, job_type, payload)
            return job_id

    enqueued: List[str] = []
    for index in range(workers):
        payload = {"batch": DISCOVERY_BATCH_SIZE,
                   "budget": DISCOVERY_BUDGET_SECONDS,
                   "worker": index}
        try:
            enqueued.append(enqueue("company_discovery_worker", payload))
        except Exception as exc:
            logger.warning("discovery dispatch: enqueue %d failed (%s)",
                           index, type(exc).__name__)
    try:
        depth = reg.queue_depth()
    except Exception:
        depth = {}
    return {"refilled": refilled, "workers": len(enqueued),
            "queue_depth": depth, "jobs": enqueued}


def run_discovery_worker(*, batch: Optional[int] = None,
                         budget: Optional[float] = None,
                         owner: str = "") -> Dict[str, Any]:
    """Keep claiming leased batches until the budget runs out.

    This used to do ONE batch and return, which left the pipeline idle roughly
    three quarters of the time: a 40-company batch finishes in ~194s and the
    next tick is 15 minutes away, so the expensive part — a warm Lambda with
    network in flight — was thrown away and re-provisioned for every batch.
    Looping until the deadline turns the same tick into several batches.

    `DISCOVERY_RESERVE_SECONDS` is why the loop stops early rather than at the
    deadline: the caller is a Lambda with a hard 300s ceiling, and a worker
    killed mid-batch loses its run record and holds its leases until they lapse.
    Returning with time to spare is worth more than one extra company.

    Never raises. A worker that blew up would tell the scheduler nothing; every
    failure mode below it is already per-company inside `discover_batch`.
    """
    import time as _time
    from services import company_registry_service as reg
    from services.ats_providers.http import simple_context

    size = max(1, int(batch or DISCOVERY_BATCH_SIZE))
    seconds = float(budget or DISCOVERY_BUDGET_SECONDS)
    owner = owner or f"discovery-{int(_time.time() * 1000)}"
    deadline = _time.monotonic() + seconds

    totals: Dict[str, int] = {"companies": 0, "endpoints": 0, "validated": 0,
                              "blocked": 0, "no_portal": 0, "errors": 0}
    claimed_total = 0
    batches = 0
    drained = False

    while True:
        remaining = deadline - _time.monotonic()
        # No point claiming a batch we cannot finish: the lease would be held
        # while nothing happens to it.
        if remaining <= DISCOVERY_RESERVE_SECONDS:
            break
        try:
            claimed = reg.claim_discovery_batch(owner, size=size)
        except Exception as exc:
            logger.warning("discovery worker: claim failed (%s)",
                           type(exc).__name__)
            return {"owner": owner, "claimed": claimed_total,
                    "batches": batches, "stats": totals,
                    "error": type(exc).__name__}
        if not claimed:
            drained = True          # queue empty, not out of time
            break

        claimed_total += len(claimed)
        batches += 1
        companies: List[Dict[str, Any]] = []
        for item in claimed:
            doc = {}
            try:
                doc = reg.get_company(item["company_id"]) or {}
            except Exception:
                pass
            companies.append({"company_id": item["company_id"],
                              "name": doc.get("name") or "",
                              "domain": doc.get("domain") or "",
                              # Proof a guessed domain belongs to THIS
                              # registrant: a site printing the ticker is that
                              # registrant's site. Positive signal only.
                              "ticker": doc.get("ticker") or ""})

        ctx = simple_context(budget_seconds=max(1.0, remaining),
                             global_rate=DISCOVERY_GLOBAL_RATE,
                             guard=_worker_guard())
        try:
            stats = discover_batch(companies, ctx, persist=True, validate=True)
        except Exception as exc:  # pragma: no cover - discover_batch is guarded
            logger.warning("discovery worker: batch failed (%s)",
                           type(exc).__name__)
            return {"owner": owner, "claimed": claimed_total,
                    "batches": batches, "stats": totals,
                    "error": type(exc).__name__}
        stats.pop("unknown_hosts", None)
        for key, value in stats.items():
            if isinstance(value, int):
                totals[key] = totals.get(key, 0) + value

    return {"owner": owner, "claimed": claimed_total, "batches": batches,
            "queue_drained": drained, "stats": totals}


def _worker_guard():
    """SSRF guard for outbound discovery fetches.

    Discovery deliberately reaches hosts that are not on any allowlist — that
    is the point, it is visiting employers we have never seen — so the guard
    here is `jd_fetcher.url_is_safe`, which blocks private/link-local space and
    non-HTTP schemes rather than checking a vendor list.
    """
    def guard(_vendor: str, url: str) -> str:
        # `url_is_safe` returns a 2-TUPLE `(ok, reason)`. `not (False, "...")`
        # is False, so the previous `if not url_is_safe(url)` never fired and
        # the scheduled worker's guard was a no-op against every unsafe URL it
        # was written to block. Unpack it.
        from services.jd_fetcher import url_is_safe
        ok, reason = url_is_safe(url)
        if not ok:
            raise ValueError(f"blocked: {reason or 'unsafe_url'}")
        return url
    return guard
