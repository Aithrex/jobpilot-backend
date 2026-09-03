"""Proving whose board this is, from the employer's own domain outward.

The rule, stated once
---------------------
A career source is owner-verified when EITHER

  (a) a link or redirect chain from an INDEPENDENTLY VERIFIED official
      employer domain reaches the exact career/ATS endpoint; or
  (b) two INDEPENDENT corroborating signals agree — for example the ATS
      provider's own owner metadata plus evidence from the verified employer
      domain.

Nothing else. In particular a guessed slug that returns jobs is not evidence
of anything except that somebody has a board there, and `slug_is_trustworthy`
is a string-similarity function that cannot distinguish Archer Aviation from
Archer Daniels Midland. That check still runs — its result is recorded as
`SIGNAL_SLUG_SIMILARITY` — and `career_source_service.adjudicate_ownership`
then ignores it. Recording and ignoring is deliberate: a report can say "we
checked, and declined to count it", which a silent omission cannot.

Direction of travel
-------------------
Discovery today runs employer -> guess a slug -> did it answer? This module
runs employer -> verify the domain -> read the employer's own careers page ->
follow where it points -> that is the endpoint. The second direction cannot
produce a wrong owner, because the employer told us.

Where it cannot reach (large employers behind an XHR-only board, employers
whose careers page is a JS shell) it falls back to (b) and, failing that,
leaves the source as `candidate` with `failure_code: owner_unproven`. A
candidate is a truthful answer. `validated` on a guess is not.

Safety
------
Every fetch goes through, in this order: SSRF check (`jd_fetcher.url_is_safe`,
unpacked as the 2-tuple it actually returns), robots.txt for the origin, a
per-host pace, a hard timeout, and a response-size cap. Redirects are followed
but the whole chain is recorded and each hop is re-checked, so an open
redirect on an employer's domain cannot walk us into a private network.
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple
from urllib.parse import urljoin, urlparse

logger = logging.getLogger(__name__)

from services import career_source_service as css  # noqa: E402
from services.career_source_service import evidence  # noqa: E402

OWNERSHIP_VERSION = "career-source-ownership-1.0.0"

#: Response-size cap. Above this we stop reading: a careers page that is
#: bigger than this is not a careers page.
MAX_HTML_BYTES = 1_500_000
#: Per-URL timeout. Generous enough for a slow enterprise portal, short
#: enough that one employer cannot consume a verification pass.
FETCH_TIMEOUT = 12.0
#: Redirect hops followed. Portal migrations are typically 1-3 hops; more
#: than this is a loop or a tracker.
MAX_REDIRECTS = 6
#: Careers pages read per employer before we give up on path (a).
MAX_CAREER_PAGES = 10
#: Browser-rendered pages per employer. Each one launches Chromium work
#: against a 2 GB executor budget; four is enough to try the official URL,
#: the careers host, one homepage path and one follow-on link.
MAX_BROWSER_PAGES = 4

#: Visible text that means a bot challenge, not a careers page. Matched
#: against title + body after a RENDER — we do not solve these; `blocked`
#: is the honest state.
_CHALLENGE_RE = re.compile(
    r"(just a moment[\s.]*\.\.\.|attention required|access denied|"
    r"verify you are (a )?human|are you a (human|robot)|"
    r"enable javascript and cookies|cf-browser-verification|"
    r"checking your browser|sorry,? you have been blocked|"
    r"security check (failed|required)|unusual (traffic|activity))",
    re.IGNORECASE)

#: Paths tried on a verified domain when the employer publishes no career URL.
#: Ordered by how often each is the real one, so the budget is spent well.
CAREER_PATHS: Tuple[str, ...] = (
    "/careers", "/jobs", "/careers/jobs", "/company/careers",
    "/about/careers", "/en/careers", "/careers/open-positions",
    "/about-us/careers", "/join-us", "/work-with-us", "/careers/search",
    "/company/jobs", "/employment", "/opportunities",
)

#: SUBDOMAINS tried on a verified domain, before the paths.
#:
#: `CAREER_PATHS` only ever looked at `www.<domain>/<path>`, and that is not
#: where a large employer's board lives. An enterprise runs a white-labelled
#: board on its own careers HOST — `careers.<domain>`, `jobs.<domain>` — which
#: usually redirects straight onto the ATS. The walk could not reach any of
#: them, so every employer whose board is one DNS label away from their
#: homepage read as "no ATS fingerprint found" no matter how many paths were
#: tried on the wrong host.
#:
#: Two labels, not ten: each one is a request per employer against a host that
#: usually does not exist, and the yield falls off a cliff after these.
CAREER_SUBDOMAINS: Tuple[str, ...] = ("careers", "jobs")

_LEGAL_WORDS = frozenset({
    "inc", "incorporated", "corp", "corporation", "co", "company", "llc",
    "ltd", "limited", "plc", "lp", "llp", "holdings", "holding", "group",
    "the", "sa", "nv", "ag", "se", "gmbh", "pte", "pty", "technologies",
    "technology", "systems", "solutions", "labs", "software", "international",
})

_SKIP_HOST_RE = re.compile(
    r"(facebook|twitter|x\.com|linkedin|instagram|youtube|tiktok|reddit|"
    r"google(apis|tagmanager)?|gstatic|doubleclick|cloudflare|jquery|"
    r"bootstrapcdn|fontawesome|w3\.org|schema\.org|wordpress|wixsite|"
    r"godaddy|cookielaw|onetrust|segment|hotjar|newrelic)\.", re.IGNORECASE)

_HREF_RE = re.compile(
    r"""(?:href|src|action|data-url|content)\s*=\s*["']([^"'\s]{4,400})["']""",
    re.IGNORECASE)
_BARE_URL_RE = re.compile(r"https?://[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]{6,400}")
_CAREER_TEXT_RE = re.compile(
    r"(career|job|opening|vacanc|position|hiring|join[\s-]?us|work[\s-]?with[\s-]?us|"
    r"opportunit|talent|recruit)", re.IGNORECASE)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Safe fetching
# ---------------------------------------------------------------------------
def _ssrf_ok(url: str) -> Tuple[bool, str]:
    """`jd_fetcher.url_is_safe` returns a 2-TUPLE.

    `career_portal_discovery._worker_guard` does `if not url_is_safe(url)`,
    and a non-empty tuple is always truthy, so that guard has never blocked
    anything. Unpacking is the entire fix, and it is why this wrapper exists
    rather than a bare call at each site.
    """
    try:
        from services.jd_fetcher import url_is_safe
        ok, reason = url_is_safe(url)
        return bool(ok), str(reason or "")
    except Exception as exc:  # pragma: no cover - never fail open on an import error
        return False, f"ssrf_check_unavailable:{type(exc).__name__}"


class RobotsGate:
    """robots.txt per origin, cached for one verification pass.

    Fails OPEN when robots.txt is missing or unreadable (that is what the
    standard says an absent file means) and CLOSED on an explicit Disallow.
    """

    def __init__(self, user_agent: str = "JobPilotIngest") -> None:
        self.user_agent = user_agent
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
            return True
        try:
            return bool(parser.can_fetch(self.user_agent, url))
        except Exception:
            return True

    def _load(self, origin: str) -> Any:
        from urllib.robotparser import RobotFileParser
        ok, _reason = _ssrf_ok(origin + "/robots.txt")
        if not ok:
            return False
        try:
            import requests
            resp = requests.get(origin + "/robots.txt", timeout=6.0,
                                headers={"User-Agent": self.user_agent})
            if resp.status_code != 200:
                return False
            parser = RobotFileParser()
            parser.parse(resp.text[:200_000].splitlines())
            return parser
        except Exception:
            return False


class Fetcher:
    """Safe HTML fetch that records the redirect chain.

    The chain matters twice: a portal migration is only detectable from it
    (Rivian's iCIMS tenant is a 143-byte redirect), and an ownership claim of
    the form "the employer's domain leads here" is exactly a chain.

    `render` is the SAME path with a browser: SSRF, robots, pace and the
    size cap still gate every navigation, and the only extra output is the
    rendered DOM plus the network requests the page made. An XHR to
    `boards-api.greenhouse.io` (or `api.greenhouse.io`) names the board
    directly and is often the only place it appears on a JS-shell careers
    page. There is no second request path that bypasses these gates.
    """

    def __init__(self, *, robots: Optional[RobotsGate] = None,
                 min_interval: float = 0.4, user_agent: str = "JobPilotIngest",
                 enable_browser: bool = False) -> None:
        self.robots = robots or RobotsGate(user_agent)
        self.user_agent = user_agent
        self.min_interval = float(min_interval)
        self.enable_browser = bool(enable_browser)
        self._next_at: Dict[str, float] = {}
        self.fetches = 0
        self.renders = 0
        self._pw = None
        self._browser = None

    def _pace(self, host: str) -> None:
        now = time.monotonic()
        due = self._next_at.get(host, 0.0)
        if due > now:
            time.sleep(min(5.0, due - now))
        self._next_at[host] = max(now, due) + self.min_interval

    def get(self, url: str) -> Dict[str, Any]:
        """`{status, text, chain, final_url, error, network_urls, rendered}`.

        Never raises. `network_urls` is empty and `rendered` is False — the
        browser path fills both via `render`.
        """
        out: Dict[str, Any] = {"status": None, "text": "", "chain": [],
                               "final_url": url, "error": "",
                               "network_urls": [], "rendered": False}
        ok, reason = _ssrf_ok(url)
        if not ok:
            out["error"] = f"ssrf:{reason}"
            return out
        if not self.robots.allows(url):
            out["error"] = "robots"
            return out
        try:
            import requests
        except Exception as exc:  # pragma: no cover
            out["error"] = f"requests_unavailable:{type(exc).__name__}"
            return out

        self._pace(urlparse(url).netloc.lower())
        self.fetches += 1
        try:
            resp = requests.get(
                url, timeout=FETCH_TIMEOUT, allow_redirects=True,
                headers={"User-Agent": self.user_agent,
                         "Accept": "text/html,application/xhtml+xml,"
                                   "application/xml;q=0.9,*/*;q=0.8",
                         "Accept-Language": "en-US,en;q=0.9"},
                stream=True)
        except Exception as exc:
            out["error"] = f"network:{type(exc).__name__}"
            return out

        chain = [h.url for h in getattr(resp, "history", [])[:MAX_REDIRECTS]]
        chain.append(resp.url)
        out["chain"] = chain
        out["final_url"] = resp.url
        out["status"] = resp.status_code
        # Every hop is re-checked: an open redirect on an employer's own
        # domain must not become a route into a private network.
        for hop in chain:
            hop_ok, hop_reason = _ssrf_ok(hop)
            if not hop_ok:
                out["error"] = f"ssrf_redirect:{hop_reason}"
                out["text"] = ""
                resp.close()
                return out
        try:
            body = b""
            for part in resp.iter_content(65536):
                body += part
                if len(body) >= MAX_HTML_BYTES:
                    break
            out["text"] = body.decode(resp.encoding or "utf-8", errors="ignore")
        except Exception as exc:
            out["error"] = f"read:{type(exc).__name__}"
        finally:
            resp.close()
        return out

    def _ensure_browser(self) -> Any:
        """Lazy Chromium. One browser for the Fetcher's lifetime so a sweep
        of ~50 employers does not pay a cold start each time."""
        if self._browser is not None:
            return self._browser
        from playwright.sync_api import sync_playwright
        from services.browser_executor import chromium_launch_args
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=True, args=chromium_launch_args())
        return self._browser

    def close(self) -> None:
        """Drop the browser so a sweep does not leave Chromium warm."""
        try:
            if self._browser is not None:
                self._browser.close()
        except Exception:  # pragma: no cover
            pass
        self._browser = None
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception:  # pragma: no cover
            pass
        self._pw = None

    def __del__(self):  # pragma: no cover - best-effort cleanup
        try:
            self.close()
        except Exception:
            pass

    def render(self, url: str) -> Dict[str, Any]:
        """Browser-rendered fetch. Same contract as `get`, plus `network_urls`.

        A 403 / Cloudflare challenge is returned as `error: bot_challenge`
        — never solved, never spoofed around. The network request list is
        what makes a JS-shell careers page yield an ATS endpoint: the board
        link often appears ONLY as an XHR.
        """
        out: Dict[str, Any] = {"status": None, "text": "", "chain": [],
                               "final_url": url, "error": "",
                               "network_urls": [], "rendered": True}
        if not self.enable_browser:
            out["error"] = "browser_disabled"
            return out
        ok, reason = _ssrf_ok(url)
        if not ok:
            out["error"] = f"ssrf:{reason}"
            return out
        if not self.robots.allows(url):
            out["error"] = "robots"
            return out

        self._pace(urlparse(url).netloc.lower())
        self.renders += 1
        self.fetches += 1
        network: List[str] = []
        context = None
        try:
            from services import ssrf_guard
            from services.browser_executor import browser_context_kwargs
            browser = self._ensure_browser()
            context = browser.new_context(**browser_context_kwargs())
            ssrf_guard.install_route_guard(context)
            page = context.new_page()
            page.on("request", lambda req: network.append(req.url))
            resp = page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            try:
                page.wait_for_load_state("networkidle", timeout=12_000)
            except Exception:
                # networkidle is best-effort: a careers page that keeps a
                # websocket open would hang forever otherwise.
                pass
            # Short settle for late XHRs that fire after DOMContentLoaded
            # but before the idle signal (Greenhouse embed boards do this).
            try:
                page.wait_for_timeout(1_500)
            except Exception:
                pass

            final = page.url or url
            chain = [url]
            if final and final != url:
                chain.append(final)
            out["chain"] = chain
            out["final_url"] = final
            out["status"] = int(resp.status) if resp is not None else None
            for hop in chain:
                hop_ok, hop_reason = _ssrf_ok(hop)
                if not hop_ok:
                    out["error"] = f"ssrf_redirect:{hop_reason}"
                    return out

            html = page.content() or ""
            if len(html) > MAX_HTML_BYTES:
                html = html[:MAX_HTML_BYTES]
            out["text"] = html
            out["network_urls"] = list(dict.fromkeys(network))[:2_000]

            title = ""
            body = ""
            try:
                title = page.title() or ""
            except Exception:
                pass
            try:
                body = page.inner_text("body") or ""
            except Exception:
                pass
            if _CHALLENGE_RE.search(f"{title}\n{body[:4_000]}"):
                out["error"] = "bot_challenge"
                if out["status"] not in (403, 429):
                    out["status"] = 403
                return out
            if out["status"] in (403, 429):
                out["error"] = ("bot_challenge" if out["status"] == 403
                                else "http_429")
            return out
        except Exception as exc:
            out["error"] = f"browser:{type(exc).__name__}"
            out["network_urls"] = list(dict.fromkeys(network))[:2_000]
            return out
        finally:
            if context is not None:
                try:
                    context.close()
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# Domain verification
# ---------------------------------------------------------------------------
def _name_tokens(name: Any) -> List[str]:
    toks = [t for t in re.split(r"[^a-z0-9]+", str(name or "").lower()) if t]
    return [t for t in toks if len(t) > 2 and t not in _LEGAL_WORDS]


_SCRIPT_STYLE_RE = re.compile(r"<(script|style|noscript)\b.*?</\1>",
                              re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_TITLE_RE = re.compile(r"<title[^>]*>(.{0,300}?)</title>", re.IGNORECASE | re.DOTALL)
_META_NAME_RE = re.compile(
    r"""<meta[^>]+(?:property|name)\s*=\s*["'](?:og:site_name|og:title|"""
    r"""application-name|apple-mobile-web-app-title)["'][^>]+content\s*=\s*"""
    r"""["']([^"']{0,200})["']""", re.IGNORECASE)


def visible_text(html: str) -> str:
    """The page's TEXT, plus the title and site-name metadata.

    Matching an employer name against raw markup is how a page that merely
    LINKS to `boards.greenhouse.io/databricks` reads as "this page names
    Databricks" — the name is in an href, not in anything a human would see.
    That is the exact confusion this whole module exists to prevent, one level
    up: an attribute is not a statement about whose page this is.
    """
    text = _SCRIPT_STYLE_RE.sub(" ", html or "")
    parts = [m.group(1) for m in _TITLE_RE.finditer(text)]
    parts += [m.group(1) for m in _META_NAME_RE.finditer(text)]
    parts.append(_TAG_RE.sub(" ", text))
    return re.sub(r"\s+", " ", " ".join(parts))


def page_confirms_employer(html: str, name: Any, *, host: str = "",
                           ticker: str = "") -> Tuple[bool, str]:
    """Does this page belong to this employer? `(ok, why)`.

    Three independent ways to say yes, ordered by strength. All of them read
    the page's VISIBLE TEXT; none of them read the URL we guessed, and none of
    them read markup, because both are the thing being tested.
    """
    body = visible_text(html).lower()
    if not body.strip():
        return False, "empty body"
    tick = str(ticker or "").strip().lower()
    if len(tick) >= 3 and re.search(rf"(?<![a-z0-9]){re.escape(tick)}(?![a-z0-9])", body):
        return True, f"ticker {tick.upper()} present"
    toks = _name_tokens(name)
    if toks and all(re.search(rf"(?<![a-z0-9]){re.escape(t)}", body) for t in toks):
        return True, f"all name tokens present ({'+'.join(toks)})"
    if len(toks) == 1:
        stem = re.sub(r"[^a-z0-9]", "", str(host or "").split(".")[0].lower())
        if stem and stem == toks[0] and toks[0] in body:
            return True, f"single-token name matches host stem {stem!r}"
    return False, "page does not name the employer"


def verify_domain(
    fetcher: Fetcher,
    *,
    display_name: Any,
    domain: Any,
    ticker: str = "",
) -> Tuple[bool, str, List[Dict[str, Any]]]:
    """Is `domain` this employer's official domain? `(ok, final_url, evidence)`.

    Refuses outright for any domain a curated identity row has blacklisted —
    `capital.com` is a CFD trading site and will never be Capital One's,
    however convincingly its homepage reads.
    """
    from services import employer_registry as er
    dom = er.normalize_domain(domain)
    ev: List[Dict[str, Any]] = []
    if not dom:
        return False, "", [evidence("domain_check", source="registry",
                                    value=f"unusable domain {domain!r}", confirmed=False)]
    bad = er.wrong_domains().get(dom)
    if bad and er.name_key(bad) == er.name_key(display_name):
        return False, "", [evidence("domain_check", source="identity_override",
                                    value=f"{dom} is explicitly not {display_name}",
                                    confirmed=False)]
    for scheme_host in (f"https://www.{dom}/", f"https://{dom}/"):
        got = fetcher.get(scheme_host)
        if got["error"] or got["status"] != 200 or not got["text"]:
            ev.append(evidence("domain_check", source=scheme_host,
                               value=got["error"] or f"status {got['status']}",
                               confirmed=False))
            continue
        ok, why = page_confirms_employer(got["text"], display_name,
                                         host=dom, ticker=ticker)
        ev.append(evidence("domain_check", source=got["final_url"], value=why, confirmed=ok))
        if ok:
            return True, got["final_url"], ev
    return False, "", ev


# ---------------------------------------------------------------------------
# Walking from the verified domain to the endpoint
# ---------------------------------------------------------------------------
def extract_urls(html: str, base_url: str) -> List[str]:
    out: List[str] = []
    seen: Set[str] = set()
    for m in _HREF_RE.finditer(html or ""):
        raw = m.group(1)
        if raw.startswith(("data:", "javascript:", "mailto:", "tel:", "#")):
            continue
        try:
            url = urljoin(base_url, raw)
        except Exception:
            continue
        if url not in seen and url.startswith(("http://", "https://")):
            seen.add(url)
            out.append(url[:600])
    for m in _BARE_URL_RE.finditer(html or ""):
        url = m.group(0)[:600]
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


def career_links(html: str, base_url: str, *, limit: int = 12) -> List[str]:
    """Links whose href OR anchor text looks like a careers page."""
    out: List[str] = []
    seen: Set[str] = set()
    for m in re.finditer(r"<a\b[^>]*href\s*=\s*[\"']([^\"'\s]{2,400})[\"'][^>]*>(.{0,200}?)</a>",
                         html or "", re.IGNORECASE | re.DOTALL):
        href, text = m.group(1), re.sub(r"<[^>]+>", " ", m.group(2))
        if not (_CAREER_TEXT_RE.search(href) or _CAREER_TEXT_RE.search(text)):
            continue
        try:
            url = urljoin(base_url, href)
        except Exception:
            continue
        if not url.startswith(("http://", "https://")) or _SKIP_HOST_RE.search(url):
            continue
        if url in seen:
            continue
        seen.add(url)
        out.append(url[:600])
        if len(out) >= limit:
            break
    return out


def _is_asset_url(url: Any) -> bool:
    """Kept as a name here because the ownership walk reads it too; the rule
    itself lives with the fingerprints, in `ats_providers.base`."""
    from services.ats_providers.base import is_asset_url
    return is_asset_url(url)


def endpoints_from_page(urls: Sequence[str]) -> List[Any]:
    """ATS endpoints fingerprinted out of a page's outgoing links.

    Asset references are dropped before fingerprinting, in
    `registry.endpoints_from_urls` — so `career_portal_discovery`, which calls
    it directly, gets the same guard. A stylesheet hosted on a vendor's CDN is
    not evidence that the CDN is the employer's job board.
    """
    try:
        from services.ats_providers import registry as ats_registry
        return ats_registry.endpoints_from_urls(urls or ())
    except Exception as exc:  # pragma: no cover
        logger.debug("endpoints_from_page: registry unavailable: %s", exc)
        return []


def walk_to_endpoints(
    fetcher: Fetcher,
    *,
    display_name: Any,
    verified_domain: str,
    start_url: str = "",
    ticker: str = "",
    render: bool = False,
) -> Dict[str, Any]:
    """From a verified domain, find the employer's ATS endpoints.

    Returns `{endpoints, career_landing_url, official_jobs_url, chain,
    pages_read, unknown_hosts, detail, blocked}`. `chain` is the redirect
    chain of the page that produced the endpoints — it IS the ownership
    evidence, so it is carried out of here rather than logged and dropped.

    `render=True` swaps `fetcher.get` for `fetcher.render` so a JS-shell
    careers page yields the board from its RENDERED DOM and its network
    requests. The fingerprint and ownership rules are unchanged — only the
    HTML source is richer. A bot challenge is recorded as `blocked` and
    never worked around.
    """
    out: Dict[str, Any] = {"endpoints": [], "career_landing_url": "",
                           "official_jobs_url": "", "chain": [], "pages_read": 0,
                           "unknown_hosts": {}, "detail": "", "blocked": False}
    page_budget = MAX_BROWSER_PAGES if render else MAX_CAREER_PAGES
    fetch = fetcher.render if render else fetcher.get
    tried: Set[str] = set()
    queue: List[str] = []
    if start_url:
        queue.append(start_url)
    # The employer's own careers HOST before any path on the homepage host.
    # This is where a white-labelled enterprise board lives and it is usually
    # one redirect from the ATS itself.
    queue.extend(f"https://{sub}.{verified_domain}/" for sub in CAREER_SUBDOMAINS)
    # Browser budget is tight: two paths plus the homepage is enough when the
    # official URL and careers host have already been tried.
    path_n = 2 if render else 6
    queue.extend(f"https://www.{verified_domain}{p}" for p in CAREER_PATHS[:path_n])
    queue.append(f"https://www.{verified_domain}/")

    def _fingerprint(got: Dict[str, Any]) -> List[Any]:
        """Every URL this fetch produced, including the ones it WAS.

        The redirect chain and the final URL are fingerprinted alongside the
        page's outgoing links, and leaving them out was a real miss: a careers
        host that 302s straight onto the ATS —
        `careers.rivian.com -> careers-rivian.icims.com/jobs/search` — has the
        endpoint in its OWN address and, very often, nowhere in its body. The
        walk read that page, looked only at what it linked to, found nothing,
        and reported "no ATS fingerprint found" while standing on the board.

        Under `render=True`, `network_urls` is the XHR/fetch list — often the
        ONLY place a JS shell names its Greenhouse/Ashby/Lever board.
        """
        candidates = list(got.get("chain") or [])
        if got.get("final_url"):
            candidates.append(got["final_url"])
        candidates.extend(extract_urls(got.get("text") or "", got.get("final_url") or ""))
        candidates.extend(got.get("network_urls") or ())
        return endpoints_from_page(candidates)

    def _saw_challenge(got: Dict[str, Any]) -> bool:
        err = str(got.get("error") or "")
        return err in ("bot_challenge", "http_429") or got.get("status") in (403, 429)

    for url in queue:
        if out["endpoints"] or out["pages_read"] >= page_budget:
            break
        if url in tried:
            continue
        tried.add(url)
        got = fetch(url)
        # A REDIRECT is evidence even when the destination refuses to answer.
        # An employer host that 301s onto `<tenant>.icims.com` has named its
        # board; whether iCIMS then served us a bot wall is a fact about our
        # request. Fingerprinting the chain before the status check is what
        # separates the two.
        chain_eps = endpoints_from_page(
            list(got.get("chain") or [])
            + ([got["final_url"]] if got.get("final_url") else [])
            + list(got.get("network_urls") or ()))
        if chain_eps:
            out["endpoints"] = chain_eps
            out["career_landing_url"] = got.get("final_url") or url
            out["chain"] = got.get("chain") or []
            out["detail"] = (f"{url} resolves to "
                             f"{got.get('final_url') or 'the ATS'}"
                             + (" (rendered)" if render else ""))
            break
        if _saw_challenge(got):
            out["blocked"] = True
            out["detail"] = f"bot challenge at {got.get('final_url') or url}"
            # Keep looking at other URLs — a homepage challenge does not
            # mean the careers host is also blocked — but do not read the
            # challenge body as if it were a careers page.
            continue
        if got["error"] or got["status"] != 200 or not got["text"]:
            continue
        out["pages_read"] += 1
        html, base = got["text"], got["final_url"]
        urls = extract_urls(html, base)
        eps = _fingerprint(got)
        if eps:
            out["endpoints"] = eps
            out["career_landing_url"] = base
            out["chain"] = got["chain"]
            out["detail"] = (
                f"ATS endpoint {'from rendered page/network at' if render else 'linked from'} {base}")
            for ep in eps:
                try:
                    prov = _provider(ep.vendor)
                    if prov:
                        out["official_jobs_url"] = prov.canonical_url(ep, None) or ""
                except Exception:
                    pass
            break
        # No ATS fingerprint here. Follow this page's own careers links once —
        # a corporate homepage usually points at a careers microsite, which is
        # where the board actually lives. Browser mode follows at most two
        # (each is another Chromium navigation).
        follow_n = 2 if render else 4
        for link in career_links(html, base, limit=follow_n):
            if out["pages_read"] >= page_budget or link in tried:
                continue
            tried.add(link)
            sub = fetch(link)
            sub_chain_eps = endpoints_from_page(
                list(sub.get("chain") or [])
                + ([sub["final_url"]] if sub.get("final_url") else [])
                + list(sub.get("network_urls") or ()))
            if sub_chain_eps:
                out["endpoints"] = sub_chain_eps
                out["career_landing_url"] = sub.get("final_url") or link
                out["chain"] = list(got["chain"]) + list(sub.get("chain") or [])
                out["detail"] = f"{link} resolves to {sub.get('final_url')}"
                break
            if _saw_challenge(sub):
                out["blocked"] = True
                continue
            if sub["error"] or sub["status"] != 200 or not sub["text"]:
                continue
            out["pages_read"] += 1
            sub_eps = _fingerprint(sub)
            if sub_eps:
                out["endpoints"] = sub_eps
                out["career_landing_url"] = sub["final_url"]
                out["chain"] = got["chain"] + sub["chain"]
                out["detail"] = f"ATS endpoint linked from {sub['final_url']}"
                break
        if out["endpoints"]:
            break
        for u in urls:
            host = urlparse(u).netloc.lower()
            if host and not _SKIP_HOST_RE.search(u) and verified_domain not in host:
                if re.match(r"^(jobs?|careers?|recruit|talent|hire|apply|workforce|hcm)"
                            r"[.\-]", host):
                    out["unknown_hosts"][host] = out["unknown_hosts"].get(host, 0) + 1

    if not out["endpoints"] and not out["detail"]:
        kind = "rendered" if render else "read"
        out["detail"] = (f"{kind} {out['pages_read']} page(s) on {verified_domain}; "
                         f"no ATS fingerprint found")
    return out


def _provider(vendor: Any) -> Any:
    try:
        from services.ats_providers import registry as ats_registry
        return ats_registry.get(str(vendor or ""))
    except Exception:  # pragma: no cover
        return None


# ---------------------------------------------------------------------------
# Corroborating signals
# ---------------------------------------------------------------------------
def provider_owner_signal(
    endpoint: Any,
    ctx: Any,
    *,
    display_name: Any,
) -> Optional[Dict[str, Any]]:
    """Ask the ATS who owns this board. `None` when the provider cannot say.

    A provider that names a DIFFERENT owner produces `confirmed: False`, which
    `adjudicate_ownership` treats as decisive against ownership. That is the
    CoStar case: the Greenhouse board `costar` declares "Co-Star", an
    astrology app, and `names_match` normalises the two identically — so the
    mismatch must be recorded with the owner string for a human to read.
    """
    prov = _provider(getattr(endpoint, "vendor", ""))
    if prov is None:
        return None
    try:
        owner = prov.board_owner(endpoint, ctx)
    except Exception as exc:
        logger.debug("provider_owner_signal: %s", exc)
        return None
    if owner is None:
        return None
    try:
        from services.ats_providers.util import names_match
        ok = bool(names_match(str(display_name), str(owner)))
    except Exception:  # pragma: no cover
        ok = False
    return evidence(css.SIGNAL_PROVIDER_OWNER,
                    source=f"{getattr(endpoint, 'vendor', '')}:{getattr(endpoint, 'tenant', '')}",
                    value=str(owner)[:200], confirmed=ok)


def slug_similarity_signal(endpoint: Any, *, display_name: Any) -> Dict[str, Any]:
    """The old heuristic, recorded so the report can show it was declined."""
    try:
        from services.ats_providers.util import slug_is_trustworthy
        ok = bool(slug_is_trustworthy(str(display_name),
                                      str(getattr(endpoint, "tenant", ""))))
    except Exception:  # pragma: no cover
        ok = False
    return evidence(css.SIGNAL_SLUG_SIMILARITY, source="ats_providers.util",
                    value=f"slug_is_trustworthy={ok} (never counted toward ownership)",
                    confirmed=ok)


def curated_row_signal(endpoint: Any, *, display_name: Any) -> Optional[Dict[str, Any]]:
    """A human-maintained catalog row naming this exact (vendor, tenant)."""
    from services import employer_identity as ei
    vendor = str(getattr(endpoint, "vendor", "") or "")
    tenant = str(getattr(endpoint, "tenant", "") or "")
    try:
        curated = ei._catalog_names().get(
            (re.sub(r"[^a-z0-9]+", "", vendor.lower()),
             re.sub(r"[^a-z0-9]+", "", tenant.lower())))
    except Exception:  # pragma: no cover
        curated = None
    if not curated:
        return None
    ok = ei.name_key(curated) == ei.name_key(display_name)
    return evidence(css.SIGNAL_CURATED_ROW, source="python_catalog",
                    value=str(curated)[:200], confirmed=ok)


def backlink_signal(
    fetcher: Fetcher,
    endpoint: Any,
    *,
    verified_domain: str,
) -> Optional[Dict[str, Any]]:
    """Does the board itself point back at the verified employer domain?

    This is an `employer_domain`-class signal even though it is read off the
    board, because the CLAIM being checked is about the employer's domain. A
    board that links to `stripe.com` is asserting it belongs to whoever owns
    `stripe.com`, and that is independent of what the ATS API says.
    """
    if not verified_domain:
        return None
    url = _endpoint_url(endpoint)
    if not url:
        return None
    got = fetcher.get(url)
    if got["error"] or got["status"] != 200 or not got["text"]:
        return None
    found = re.search(rf"https?://(?:[a-z0-9-]+\.)*{re.escape(verified_domain)}\b",
                      got["text"], re.IGNORECASE)
    return evidence(css.SIGNAL_BOARD_BACKLINK, source=got["final_url"],
                    value=(found.group(0)[:200] if found
                           else f"no link to {verified_domain} on the board"),
                    confirmed=bool(found))


# ---------------------------------------------------------------------------
# The pass
# ---------------------------------------------------------------------------
#: Hosts a board links to that are never the employer's own domain.
_BOARD_INFRA_RE = re.compile(
    r"(greenhouse|lever|ashbyhq|smartrecruiters|workable|recruitee|myworkday|"
    r"icims|taleo|jobvite|bamboohr|paylocity|ukg|rippling|jazzhr|cornerstone|"
    r"successfactors|oraclecloud|eightfold|phenom|indeed|glassdoor|ziprecruiter|"
    r"linkedin|facebook|twitter|x\.com|instagram|youtube|tiktok|github|"
    r"googleapis|gstatic|cloudfront|cloudflare|amazonaws|akamai|jsdelivr|"
    r"unpkg|typekit|fontawesome|w3\.org|schema\.org|adobe|hubspot|segment|"
    r"onetrust|cookielaw|sentry|datadoghq|newrelic|gravatar|licdn)",
    re.IGNORECASE)


def candidate_domains_from_board(fetcher: "Fetcher", endpoint: Any,
                                 *, limit: int = 4) -> List[str]:
    """Domains the BOARD itself links to, most-linked first.

    An employer that exists only as an ATS `display_name` has no domain, and
    without one the decisive ownership path — walk from the employer's own
    site to the board — cannot start. The board usually links back to the
    employer ("databricks.com" from `boards.greenhouse.io/databricks`), which
    turns an unidentifiable employer into a CANDIDATE domain.

    A candidate, not a conclusion. It is handed to `verify_domain`, which
    fetches it and checks the page names the employer. A board link that
    survives that is two independent parties agreeing; a board link on its own
    is one page's assertion about itself.
    """
    url = _endpoint_url(endpoint)
    if not url:
        return []
    got = fetcher.get(url)
    if got["error"] or got["status"] != 200 or not got["text"]:
        return []
    from services.employer_registry import normalize_domain
    counts: Dict[str, int] = {}
    for candidate in extract_urls(got["text"], got["final_url"]):
        if _BOARD_INFRA_RE.search(candidate):
            continue
        dom = normalize_domain(candidate)
        if dom and not _BOARD_INFRA_RE.search(dom):
            counts[dom] = counts.get(dom, 0) + 1
    return [d for d, _n in sorted(counts.items(), key=lambda kv: -kv[1])][:limit]


def verify_employer_source(
    *,
    employer: Dict[str, Any],
    fetcher: Optional[Fetcher] = None,
    ctx: Any = None,
    candidate_endpoints: Sequence[Any] = (),
    official_url: str = "",
    ticker: str = "",
    use_browser: bool = False,
) -> List[Dict[str, Any]]:
    """Verify one employer end to end. Returns `career_sources` records.

    PURE with respect to the database — it builds records and returns them.
    `scripts/verify_career_sources.py` decides whether to write, and its
    default is not to. Probing is dry-run by default precisely because the
    thing being fixed is a system that wrote `validated` too eagerly.

    `use_browser=True` is the fifth verification pass: render the careers
    page, fingerprint the rendered DOM AND the network requests, then feed
    the same evidence through `adjudicate_ownership`. It does not invent a
    second ownership rule.
    """
    from services import employer_registry as er
    if use_browser:
        fetcher = fetcher or Fetcher(enable_browser=True)
        if not getattr(fetcher, "enable_browser", False):
            fetcher.enable_browser = True
    else:
        fetcher = fetcher or Fetcher()
    name = employer.get("display_name") or ""
    eid = employer.get("employer_id") or ""
    domain = employer.get("canonical_domain") or ""
    records: List[Dict[str, Any]] = []

    # 1. Verify the domain itself. Without this, path (a) is unavailable and
    #    every "the employer's site links here" claim is unfounded.
    #    Domain verification stays on plain HTTP — a homepage that needs a
    #    browser to name the employer is not an independently verified domain
    #    under the ownership rule, and burning Chromium on it would not help.
    domain_ok, domain_url, domain_ev = verify_domain(
        fetcher, display_name=name, domain=domain, ticker=ticker)
    verified_domain = er.normalize_domain(domain) if domain_ok else ""

    # 1b. No domain on record. Ask the boards we already know about what
    #     domain they link to, then verify THAT the same way. This is the only
    #     route to path (a) for an employer that exists in the registry solely
    #     as an ATS display name — which is most of the ones discovered by
    #     fingerprinting, and therefore most of the uncovered denominator.
    if not verified_domain:
        for ep in list(candidate_endpoints or [])[:2]:
            for cand in candidate_domains_from_board(fetcher, ep):
                ok, url, ev = verify_domain(fetcher, display_name=name,
                                            domain=cand, ticker=ticker)
                domain_ev.extend(ev)
                if ok:
                    verified_domain = er.normalize_domain(cand)
                    domain_url = url
                    domain_ev.append(evidence(
                        "domain_from_board", source=_endpoint_url(ep),
                        value=f"board links to {verified_domain}, and that site "
                              f"names {name}"))
                    break
            if verified_domain:
                break

    # 2. Walk from the verified domain to the endpoints. This is path (a).
    linked: Dict[str, Any] = {}
    linked_keys: Set[str] = set()
    if verified_domain:
        linked = walk_to_endpoints(
            fetcher, display_name=name, verified_domain=verified_domain,
            start_url=official_url, ticker=ticker, render=use_browser)
        linked_keys = {getattr(e, "member_key", "") for e in linked.get("endpoints") or []}

    seen: Set[str] = set()
    all_endpoints: List[Any] = list(linked.get("endpoints") or []) + list(candidate_endpoints or [])
    for ep in all_endpoints:
        key = getattr(ep, "member_key", "")
        if not key or key in seen:
            continue
        seen.add(key)

        own_ev: List[Dict[str, Any]] = list(domain_ev)
        if key in linked_keys:
            own_ev.append(evidence(
                css.SIGNAL_DOMAIN_LINK,
                source=linked.get("career_landing_url") or verified_domain,
                value=f"{verified_domain} -> {key}"))
            method = css.METHOD_OFFICIAL_LINK
            if len(linked.get("chain") or []) > 1:
                own_ev.append(evidence(
                    css.SIGNAL_DOMAIN_REDIRECT,
                    source=" -> ".join((linked.get("chain") or [])[:MAX_REDIRECTS]),
                    value=f"{len(linked.get('chain') or [])} hop(s)"))
                method = css.METHOD_OFFICIAL_REDIRECT
        else:
            method = css.METHOD_SLUG_PROBE

        # 3. Corroborating signals. Gathered for EVERY endpoint, including the
        #    domain-linked ones, so a source that later loses its link still
        #    has an evidence trail rather than dropping to `candidate`.
        for sig in (provider_owner_signal(ep, ctx, display_name=name),
                    curated_row_signal(ep, display_name=name),
                    backlink_signal(fetcher, ep, verified_domain=verified_domain)):
            if sig:
                own_ev.append(sig)
        own_ev.append(slug_similarity_signal(ep, display_name=name))

        records.append(css.build_source(
            employer_id=eid,
            ats_vendor=getattr(ep, "vendor", ""),
            tenant=getattr(ep, "tenant", ""),
            verified_domain=verified_domain,
            career_landing_url=linked.get("career_landing_url") or official_url,
            official_jobs_url=linked.get("official_jobs_url") or "",
            canonical_endpoint=_endpoint_url(ep),
            redirect_chain=linked.get("chain") or (),
            discovery_method=method,
            ownership_evidence=own_ev,
            member_key=key,
        ))

    if not records:
        # An employer with no endpoint at all still gets a row. An employer
        # with no row is an employer that silently left the denominator, and
        # that is the failure mode this whole subsystem exists to prevent.
        detail = (linked.get("detail") if linked
                  else ("domain not verified" if not verified_domain
                        else "no endpoint found"))
        failure = css.FAIL_OWNER_UNPROVEN
        state = css.STATE_CANDIDATE
        if linked.get("blocked") and not linked.get("endpoints"):
            failure = css.FAIL_CHALLENGE
            state = css.STATE_BLOCKED
        records.append(css.build_source(
            employer_id=eid, ats_vendor="", tenant="",
            verified_domain=verified_domain,
            career_landing_url=domain_url or official_url,
            discovery_method=css.METHOD_OFFICIAL_REGISTRY if official_url
            else css.METHOD_FINGERPRINT,
            ownership_evidence=domain_ev,
            state=state,
            failure_code=failure,
            failure_detail=str(detail or "")[:400],
        ))
    return records


def _endpoint_url(ep: Any) -> str:
    """The PUBLIC board page, asked of the provider.

    This used to be `f"https://{hosts[0].lstrip('.')}/{tenant}"`, and
    `portal_hosts` entries are SUFFIXES for every vendor that gives each
    customer its own hostname. `.icims.com` became `icims.com` and the URL
    became `https://icims.com/careers-rivian` — a page that is nobody's board.
    So iCIMS, BambooHR, Cornerstone, Taleo, Recruitee, Workday and Oracle had
    no route to the `employer_domain` evidence class at all, which is the
    class most of the uncovered queue is missing. Measured on the last pass-1
    sweep: iCIMS 240 sources checked, 0 ownerships proven; BambooHR 95 and 0.

    `AtsProvider.board_url` knows the shape per vendor and returns "" rather
    than guessing when the board is not derivable (an Oracle pod hostname is
    not derivable from a company name).
    """
    prov = _provider(getattr(ep, "vendor", ""))
    if prov is None:
        return ""
    try:
        return str(prov.board_url(ep) or "")
    except Exception as exc:      # pragma: no cover - a bad row is not fatal
        logger.debug("_endpoint_url: %s", exc)
        return ""


def detect_migration(fetcher: Fetcher, url: str) -> Tuple[bool, str, List[str]]:
    """`(moved, destination, chain)` for a career source URL.

    A "move" is a redirect that leaves the original host, or a body small
    enough to be nothing but a redirect shim. Rivian's iCIMS tenant answers
    with 143 bytes; treating that as a live board is how it stayed broken.
    """
    got = fetcher.get(url)
    chain = list(got.get("chain") or [])
    if got.get("error") or not chain:
        return False, "", chain
    start_host = urlparse(url).netloc.lower().lstrip("www.")
    final_host = urlparse(got["final_url"]).netloc.lower().lstrip("www.")
    if final_host and final_host != start_host:
        return True, got["final_url"], chain
    body = got.get("text") or ""
    if got.get("status") == 200 and 0 < len(body) < 400:
        m = re.search(r"""(?:url=|href=|location(?:\.href)?\s*=\s*)["']?"""
                      r"""(https?://[^"'>\s]+)""", body, re.IGNORECASE)
        if m:
            return True, m.group(1)[:400], chain + [m.group(1)[:400]]
    return False, "", chain
