"""
JD Fetcher — best-effort extraction of a job description from a posting URL.

Used by the Tailor flow when the pipeline scrape didn't include a description
(LinkedIn / Workday / Indeed / company sites can all skip it). The frontend
calls /api/jobs/fetch-jd?url=... once the user clicks "Tailor", and the
returned text seeds the JD textarea so the user doesn't have to paste it.

Strategy by URL kind:
  * LinkedIn /jobs/view/{id}    → use the public guest endpoint
                                   /jobs-guest/jobs/api/jobPosting/{id}
                                   which returns rendered HTML for the JD
                                   without requiring auth.
  * Greenhouse boards.greenhouse.io/{slug}/jobs/{id}
                                → call boards-api.greenhouse.io for the
                                   full content field. Reuses the listing
                                   API behavior the pipeline already trusts.
  * Lever jobs.lever.co/{slug}/{id}
                                → /v0/postings/{slug}/{id}?mode=json.
  * Ashby jobs.ashbyhq.com/{slug}/{id}
                                → /posting-api/job-board/{slug}/{id}.
  * Anything else (Workday, Greenhouse-on-custom-domain, company careers)
                                → plain HTTPS GET + HTML strip. Covers most
                                   server-rendered ATSes but won't help for
                                   single-page apps that hydrate client-side.

All paths are bounded with short timeouts and length caps so a slow / huge
posting can't hang the request thread.
"""
from __future__ import annotations

import ipaddress
import logging
import re
import socket
from typing import Optional, Tuple
from urllib.parse import urljoin, urlparse

import requests

logger = logging.getLogger(__name__)

# Reuse the HTML→text helper from daily_pipeline_service so behavior is
# consistent with the listing scrape.
from services.daily_pipeline_service import _html_to_text  # noqa: E402

_TIMEOUT_S = 12
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_0) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"
)
_MAX_JD_CHARS = 6000


# ---------------------------------------------------------------------------
# SSRF guard — applied to EVERY outbound fetch in this module
# ---------------------------------------------------------------------------
# The JD URL is untrusted user input (guest tailor AND the logged-in
# /api/jobs/fetch-jd path). Without a guard, requests.get(url) is a classic
# SSRF primitive against the Lambda's VPC/metadata endpoint. Stdlib-only:
# ipaddress + socket.getaddrinfo (no new deps).

_ALLOWED_SCHEMES = {"http", "https"}
_MAX_REDIRECTS = 5
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}


def _ip_is_public(ip) -> bool:
    """False for loopback / private (10/8, 172.16/12, 192.168/16, fc00::/7) /
    link-local (169.254/16 incl. 169.254.169.254, fe80::/10) / reserved /
    multicast / unspecified (0.0.0.0, ::) addresses."""
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped  # ::ffff:127.0.0.1 must fail like 127.0.0.1
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def url_is_safe(url: str) -> Tuple[bool, str]:
    """SSRF guard: (allowed, reason). Scheme must be http/https and the host
    — literal IP or EVERY address the name resolves to — must be public.

    DNS resolution failure fails CLOSED (we refuse rather than let requests
    resolve it a second time unchecked).
    """
    if not url or not isinstance(url, str):
        return False, "empty_url"
    try:
        parsed = urlparse(url.strip())
    except Exception:
        return False, "unparseable_url"
    scheme = (parsed.scheme or "").lower()
    if scheme not in _ALLOWED_SCHEMES:
        return False, f"scheme_not_allowed:{scheme or 'none'}"
    host = parsed.hostname
    if not host:
        return False, "no_host"

    # Literal IP (incl. bracketed IPv6) — validate directly, no DNS.
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        return (True, "") if _ip_is_public(literal) else (False, "private_ip_literal")

    try:
        port = parsed.port or (443 if scheme == "https" else 80)
    except ValueError:
        return False, "bad_port"
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, OSError, UnicodeError, ValueError):
        return False, "dns_failure"
    if not infos:
        return False, "dns_empty"
    for info in infos:
        addr = str(info[4][0]).split("%")[0]
        try:
            resolved = ipaddress.ip_address(addr)
        except ValueError:
            return False, "unparseable_resolved_addr"
        if not _ip_is_public(resolved):
            return False, "resolves_to_private_ip"
    return True, ""


def _safe_get(url: str, *, timeout: int = _TIMEOUT_S, headers: Optional[dict] = None):
    """requests.get with the SSRF guard applied to the URL AND to every
    redirect hop (allow_redirects=False + manual follow, max 5, each target
    re-validated before it is fetched). Returns the final Response, or None
    when the URL/a hop was blocked or the redirect chain is too long.

    Note: validate-then-fetch has an inherent DNS TOCTOU window (rebinding);
    per-hop re-validation + private-range rejection is the accepted posture
    here (no connection pinning without new deps).
    """
    current = url
    for _ in range(_MAX_REDIRECTS + 1):
        allowed, reason = url_is_safe(current)
        if not allowed:
            logger.warning("jd_fetcher: blocked outbound fetch (%s)", reason)
            return None
        r = requests.get(current, timeout=timeout, headers=headers, allow_redirects=False)
        if r.status_code in _REDIRECT_STATUSES:
            location = r.headers.get("Location") or ""
            if not location:
                return r
            current = urljoin(current, location)
            continue
        return r
    logger.warning("jd_fetcher: redirect chain exceeded %d hops", _MAX_REDIRECTS)
    return None


# ---------------------------------------------------------------------------
# URL classifiers
# ---------------------------------------------------------------------------

# LinkedIn URLs come in two shapes:
#   /jobs/view/3845678920                           (id only)
#   /jobs/view/sde-i-at-amazon-3845678920            (slug-then-id)
# Either way the numeric ID is at the end of the path segment, so we anchor
# on a trailing digit cluster before the next slash / query / hash.
_LINKEDIN_VIEW_RE = re.compile(
    r"linkedin\.com/jobs/(?:view|collections)/(?:[^/?#]*?-)?(\d{6,})(?:[/?#]|$)",
    re.IGNORECASE,
)
_GREENHOUSE_RE = re.compile(r"boards\.greenhouse\.io/([^/]+)/jobs/(\d+)", re.IGNORECASE)
_GREENHOUSE_API_RE = re.compile(r"greenhouse\.io/embed/job_app\?token=([^&]+)", re.IGNORECASE)
_LEVER_RE = re.compile(r"jobs\.(?:eu\.)?lever\.co/([^/]+)/([^/?#]+)", re.IGNORECASE)
_ASHBY_RE = re.compile(r"jobs\.ashbyhq\.com/([^/]+)/([^/?#]+)", re.IGNORECASE)

# Workday URLs follow a stable shape across tenants:
#   https://{tenant}.{wd1|wd5|wd3...}.myworkdayjobs.com/{lang}/{site}/job/{location}/{title}_{id}
# We pull (host, site, jobReqId) and call the CXS JSON API which returns the
# raw JD text without needing browser rendering — fixes the #1 source of
# empty-JD frustration when clicking Tailor on Workday postings.
_WORKDAY_RE = re.compile(
    r"https?://([a-z0-9-]+\.(?:wd[0-9]+|wday)\.myworkdayjobs\.com)"
    r"/(?:[a-z-]+/)?(?:[a-z-]+/)?([A-Za-z0-9_-]+)?"
    r"/job/[^/]+/[^/]*?_(R-?\d+[A-Z0-9-]*|JR-?\d+[A-Z0-9-]*|[0-9A-Z-]{6,})",
    re.IGNORECASE,
)


def fetch_jd(url: str) -> Tuple[str, str]:
    """Return (description_text, source_kind). Empty text if extraction failed.

    `source_kind` lets the caller log which strategy was tried — useful in
    the UI's Tailor toast so the user can tell whether they got the rich
    Greenhouse JD or a generic HTML strip.

    Fallback chain: each direct strategy runs first (fast, free). If it
    returns empty AND an Apify token is configured, we retry once via a
    browser-rendered Apify actor — this rescues LinkedIn 403s and Workday
    SPA pages that don't expose the JD in their initial HTML.
    """
    if not url or not isinstance(url, str):
        return "", "invalid_url"
    url = url.strip()
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return "", "invalid_url"

    # SSRF guard up front (scheme + host/IP ranges). Every per-source fetch
    # below ALSO goes through _safe_get, which re-validates each redirect hop.
    allowed, reason = url_is_safe(url)
    if not allowed:
        logger.warning("jd_fetcher: rejected JD url (%s)", reason)
        return "", "blocked_url"

    # ---- LinkedIn (guest job-posting endpoint, no auth required) --------
    m = _LINKEDIN_VIEW_RE.search(url)
    if m:
        job_id = m.group(1)
        text = _fetch_linkedin_guest(job_id)
        if text:
            return text, "linkedin_guest"
        # Guest endpoint has been gating more aggressively (403/429 without
        # auth). Try the Apify browser scrape before giving up.
        text = _fetch_apify_fallback(url)
        if text:
            return text, "apify_browser"
        return "", "linkedin_guest"

    # ---- Greenhouse hosted board ----------------------------------------
    m = _GREENHOUSE_RE.search(url)
    if m:
        slug, job_id = m.group(1), m.group(2)
        return _fetch_greenhouse_detail(slug, job_id), "greenhouse_api"

    # ---- Lever hosted board ---------------------------------------------
    m = _LEVER_RE.search(url)
    if m:
        slug, posting_id = m.group(1), m.group(2)
        return _fetch_lever_detail(slug, posting_id), "lever_api"

    # ---- Ashby hosted board ---------------------------------------------
    m = _ASHBY_RE.search(url)
    if m:
        slug, posting_id = m.group(1), m.group(2)
        return _fetch_ashby_detail(slug, posting_id), "ashby_api"

    # ---- Workday CXS JSON API -------------------------------------------
    # Direct JSON endpoint that returns the JD HTML without browser rendering.
    # Try this BEFORE the generic HTML scrape — Workday SPAs hide the JD
    # behind hydration so the static HTML is useless.
    m = _WORKDAY_RE.search(url)
    if m:
        host, site, job_id = m.group(1), m.group(2) or "External", m.group(3)
        text = _fetch_workday_cxs(host, site, job_id, url)
        if text:
            return text, "workday_cxs"
        # Fall through to generic + Apify if the CXS endpoint shape varies.

    # ---- Generic HTML scrape (Workday, company careers, etc.) -----------
    # Workday URLs vary per tenant (myworkdayjobs.com, careers.<co>.com).
    # A vanilla GET works for server-rendered Workday pages; it won't help
    # for the client-rendered single-page versions — fall back to a
    # browser-rendered Apify scrape when the static fetch returns nothing.
    text = _fetch_generic_html(url)
    if text:
        return text, "html_scrape"
    text = _fetch_apify_fallback(url)
    if text:
        return text, "apify_browser"
    return "", "html_scrape"


# ---------------------------------------------------------------------------
# Per-source extractors
# ---------------------------------------------------------------------------

def _fetch_linkedin_guest(job_id: str) -> str:
    api_url = f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"
    try:
        r = _safe_get(api_url, headers={"User-Agent": _UA})
        if r is None or not r.ok:
            logger.info("LinkedIn guest JD %s returned %s", job_id, r.status_code)
            return ""
        return _html_to_text(r.text)[:_MAX_JD_CHARS]
    except Exception as e:
        logger.warning("LinkedIn guest JD %s failed: %s", job_id, e)
        return ""


def _fetch_greenhouse_detail(slug: str, job_id: str) -> str:
    api_url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{job_id}?content=true"
    try:
        r = _safe_get(api_url)
        if r is None or not r.ok:
            return ""
        body = r.json() or {}
        content = body.get("content") or ""
        return _html_to_text(content)[:_MAX_JD_CHARS]
    except Exception as e:
        logger.warning("Greenhouse JD %s/%s failed: %s", slug, job_id, e)
        return ""


def _fetch_lever_detail(slug: str, posting_id: str) -> str:
    api_url = f"https://api.lever.co/v0/postings/{slug}/{posting_id}?mode=json"
    try:
        r = _safe_get(api_url)
        if r is None or not r.ok:
            return ""
        body = r.json() or {}
        plain = (body.get("descriptionPlain") or "").strip()
        if not plain:
            plain = _html_to_text(body.get("descriptionBody") or "")
        # Append lists (Responsibilities / Qualifications / etc.).
        for sec in (body.get("lists") or []):
            if not isinstance(sec, dict):
                continue
            heading = (sec.get("text") or "").strip()
            content = _html_to_text(sec.get("content") or "")
            if heading or content:
                plain += f"\n\n{heading}\n{content}".rstrip()
        return plain[:_MAX_JD_CHARS]
    except Exception as e:
        logger.warning("Lever JD %s/%s failed: %s", slug, posting_id, e)
        return ""


def _fetch_ashby_detail(slug: str, posting_id: str) -> str:
    api_url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}/{posting_id}"
    try:
        r = _safe_get(api_url)
        if r is None or not r.ok:
            return ""
        body = r.json() or {}
        # Ashby wraps the posting in a `job` field on detail endpoints.
        job = body.get("job") if isinstance(body.get("job"), dict) else body
        plain = (job.get("descriptionPlain") or "").strip()
        if not plain:
            plain = _html_to_text(job.get("descriptionHtml") or job.get("description") or "")
        return plain[:_MAX_JD_CHARS]
    except Exception as e:
        logger.warning("Ashby JD %s/%s failed: %s", slug, posting_id, e)
        return ""


def _fetch_workday_cxs(host: str, site: str, job_id: str, original_url: str) -> str:
    """Pull the JD via Workday's public CXS JSON API.

    The shape used by every public Workday tenant is:
        https://{host}/wday/cxs/{tenant}/{site}/job/{jobReqId}

    Where {tenant} is the subdomain (e.g. "stripe" in "stripe.wd1.myworkdayjobs.com").
    {site} is the site key in the original URL path — defaults to "External"
    when not present. Returns plain text JD on success, "" on any failure.
    """
    tenant = host.split(".", 1)[0]
    cxs_url = f"https://{host}/wday/cxs/{tenant}/{site}/job/{job_id}"
    headers = {
        "User-Agent": _UA,
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": original_url,
    }
    try:
        r = _safe_get(cxs_url, headers=headers)
        if r is None:
            return ""
        if not r.ok:
            logger.info("Workday CXS %s -> %s", cxs_url, r.status_code)
            return ""
        body = r.json() or {}
    except Exception as e:
        logger.warning("Workday CXS %s failed: %s", cxs_url, e)
        return ""
    job = body.get("jobPostingInfo") if isinstance(body.get("jobPostingInfo"), dict) else body
    desc = (
        job.get("jobDescription")
        or job.get("description")
        or ""
    )
    if not desc:
        return ""
    return _html_to_text(str(desc))[:_MAX_JD_CHARS]


def _fetch_apify_fallback(url: str) -> str:
    """Last-resort browser-rendered scrape via Apify.

    Triggered only when the direct strategies returned empty (LinkedIn guest
    API 403'd, Workday SPA hid the JD behind hydration, etc.). Uses Apify's
    official ``apify/website-content-crawler`` actor — it renders the page
    in a real browser, strips navigation/cookie banners, and returns clean
    article text.

    No-ops if no Apify token is configured. Bounded to ~60s; on any error
    (timeout, credits exhausted, actor failure) returns "" and the caller
    surfaces the same "couldn't fetch — paste manually" UX as before. This
    is purely additive — never invoked when the direct path succeeded.
    """
    # --- APIFY DISABLED (temporary): the browser-rendered Apify JD fallback is
    # turned off. Return "" so callers fall through to the existing "couldn't
    # fetch — paste manually" UX. Remove this return to re-enable. ---
    return ""
    from urllib.parse import quote
    try:
        from services.daily_pipeline_service import _get_apify_token
    except Exception:
        return ""
    token = _get_apify_token()
    if not token:
        return ""

    actor = "apify~website-content-crawler"
    api_url = f"https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items?token={quote(token)}"
    payload = {
        "startUrls": [{"url": url}],
        "maxCrawlPages": 1,
        "maxCrawlDepth": 0,
        "crawlerType": "playwright:chrome",
        "removeCookieWarnings": True,
        "removeElementsCssSelector": (
            "nav, footer, header, aside, "
            "[class*='cookie'], [class*='banner'], [class*='consent'], "
            "[class*='nav'], [class*='footer'], [class*='sidebar']"
        ),
        "saveMarkdown": False,
        "saveHtml": False,
        # Keep payload small — we only need plain text for the JD textarea.
        "maxResults": 1,
    }
    try:
        r = requests.post(api_url, json=payload, timeout=60)
    except requests.RequestException as e:
        logger.info("Apify JD fallback request failed: %s", e)
        return ""
    if r.status_code >= 400:
        logger.info("Apify JD fallback %s -> HTTP %s", url, r.status_code)
        return ""
    try:
        items = r.json() or []
    except Exception:
        return ""
    if not items:
        return ""
    first = items[0] if isinstance(items, list) else {}
    if not isinstance(first, dict):
        return ""
    text = (first.get("text") or first.get("markdown") or "").strip()
    if not text and isinstance(first.get("html"), str):
        text = _html_to_text(first["html"])
    return text[:_MAX_JD_CHARS] if text else ""


def _fetch_generic_html(url: str) -> str:
    # Full set of browser-like headers — many ATSes 403 a bare requests.get
    # without Accept/Accept-Language/Sec-Fetch-* even though they'd serve the
    # page to a real Chrome. Mimicking the standard Chrome request raises the
    # success rate from ~40% to ~75% on company-careers pages.
    headers = {
        "User-Agent": _UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
    }
    try:
        # _safe_get = SSRF guard + manual redirect follow with per-hop
        # re-validation (replaces the old allow_redirects=True).
        r = _safe_get(url, headers=headers)
        if r is None or not r.ok:
            return ""
        # Cap raw body before regex work — some pages are MB of HTML and most
        # of it is non-content (navs, footers, scripts).
        raw = r.text[:300_000]
        # Strip <script>/<style>/<svg> blocks before the generic HTML→text
        # pass so their contents don't leak into the JD as garbage.
        raw = re.sub(r"<(script|style|svg)\b[^>]*>.*?</\1>", " ", raw, flags=re.IGNORECASE | re.DOTALL)
        # Some Workday pages embed the JD inside a JSON-LD <script type="application/ld+json">
        # block we just removed. Try one focused extraction before falling
        # back to the body strip.
        ld_match = re.search(
            r"<script[^>]*application/ld\+json[^>]*>(.*?)</script>",
            r.text[:300_000],
            flags=re.IGNORECASE | re.DOTALL,
        )
        if ld_match:
            try:
                import json as _json
                ld = _json.loads(ld_match.group(1))
                if isinstance(ld, list):
                    ld = next((x for x in ld if isinstance(x, dict) and x.get("@type") == "JobPosting"), None)
                if isinstance(ld, dict) and ld.get("description"):
                    text = _html_to_text(str(ld["description"]))
                    if text and len(text) > 80:
                        return text[:_MAX_JD_CHARS]
            except Exception:
                pass
        text = _html_to_text(raw)
        return text[:_MAX_JD_CHARS]
    except Exception as e:
        logger.warning("Generic JD scrape %s failed: %s", url, e)
        return ""
