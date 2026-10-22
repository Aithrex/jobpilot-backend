"""Jobs an employer lists on its OWN careers page, with no job-board system.

Why this exists, and how narrow it is on purpose
------------------------------------------------
Of the H-1B sponsors discovery reached a website for and found no board on,
most publish their openings as ordinary pages on that site. Measured on 200 of
them (2026-10-08): 156 careers pages read, and on 11 the openings were plain
same-site links under a jobs path whose text is a job title - 56 postings,
10 of the 11 genuine.

A general "read any careers page" scraper is a precision problem before it is
a coverage one: the same sample, read loosely, called "Executive Team", a
product page and a press release job postings on 50 other sites. So every
reader below needs POSITIVE evidence that what it found is a posting.

What a careers page can say, and how each is read (extended 2026-10-22)
-----------------------------------------------------------------------
Measured on the direct-employer audit's own-site employers (MATS Research,
Orange Logic, Rugged Science, Nous Research, Longbridge, Attune, GMI Cloud):

  * **Structured data first.** A schema.org `JobPosting` on the listing page,
    found inside a list, an `@graph`, or with an `@type` LIST
    (`["JobPosting", "Thing"]`); and HYDRATED job data the page renders from
    (`__NEXT_DATA__`, `__TANSTACK_DEHYDRATED__`, `__APOLLO_STATE__`,
    `__INITIAL_STATE__`): an object counts as a job only with a title, an id
    and at least three job fields, one of them unmistakable (department,
    employment type, apply URL, workplace type, site). Longbridge's page
    hydrates all 54 openings this way, each with its own site and country -
    its `/hk/en/` URL is the SITE locale, not where the job is.
  * **Links.** Same-site links under a jobs path (`/jobs/<slug>`,
    `/careers/<slug>`), and LinkedIn job links (`linkedin.com/jobs/view/<id>`)
    that an official page uses as its apply button (Attune, GMI Cloud). A
    card's link is often a button ("Learn more", "Read more", "View job
    description", or an image); the title is then taken from the link's
    `aria-label` or from the card around it - the nearest ancestor holding no
    other posting link. A title must read as a job (`looks_like_a_job_title`);
    a title that names no role word ("General Counsel" before this change,
    "Sheet Metal Fabricator") is accepted only beside at least one strict
    match on the same path pattern, which is what a listing of siblings is.
  * **One opening.** One strict match used to be rejected as "more often a
    menu item". It is now accepted when its card carries posting evidence
    (Location:, Apply, Full-time, Deadline, Salary...). A lone link with
    nothing around it is still rejected.
  * **Inline roles.** When a page lists roles as headings with no per-role
    link and an application address (`mailto:`), each heading that reads as a
    job title is a role, identified by its anchor id or a slug of its title.

Application method
------------------
Every row records how a person applies: `ats_url`, `email`, `linkedin`,
`form` or `page`. A `mailto:` link is an APPLICATION METHOD, never a crawl
endpoint, and nothing here sends mail. LinkedIn rows are discovery only: the
link is the hand-off, it is never fetched and never automated.

Purpose hints
-------------
A general application / expression of interest / talent network, a
fellowship or grant, and a volunteer form are listed beside requisitions and
are not requisitions. They are kept, with `posting_purpose_hint`, so the
policy layer can exclude them visibly instead of this reader guessing.

Paging and completeness
-----------------------
Pagination is followed only when the page itself declares it (`rel="next"`,
or a "Next" link to the same listing path), bounded by `MAX_PAGES`,
`MAX_LINKS_PER_PAGE` and `MAX_POSTINGS`. Each listing records
`ctx.options["careers_page_completeness"]` = `complete` | `budget_limited` |
`unverified`, and anything but `complete` sets `ctx.listing_truncated` so the
crawler never closes postings it did not get to.

A page that yields nothing is UNVERIFIED, not empty. A static "No jobs found"
template next to job data is ignored (Hover and Quantcast served exactly
that); a template with no job data beside it still proves nothing about a
page that may render its list in a browser.

Identity
--------
`source_job_id` is stable: the URL slug for a same-site link (unchanged from
the first version, so stored rows keep their deterministic job id), the
schema.org `identifier` or hydrated id when there is one, `li-<id>` for a
LinkedIn job, and the anchor id or title slug for an inline role.

SSRF
----
Every tenant is a different employer's domain, so there is no vendor host to
allowlist. `api_hosts` is therefore EMPTY and the crawler pins each member to
its own host instead (`job_ingest_worker._provider_fetch_context`); a link
that leaves the host is never followed.
"""
from __future__ import annotations

import html as _html
import json
import re
from typing import Any, Dict, Iterator, List, Optional, Tuple
from urllib.parse import parse_qs, urljoin, urlparse

from services.ats_providers.base import (DATE_EXACT, DATE_UNKNOWN, AtsProvider,
                                         ChallengeError, CrawlPolicy, Endpoint,
                                         FetchContext, RawJob)
from services.ats_providers.registry import register
from services.ats_providers.util import (html_to_text, looks_minted_now,
                                         looks_remote, slugify, to_iso)

VENDOR = "careers_page"
MIN_LINKS = 2
#: Explicit bounds, replacing the old 60-link cap and single page.
MAX_LINKS_PER_PAGE = 200
MAX_PAGES = 10
MAX_POSTINGS = 500
#: Kept for callers that imported the old name.
MAX_LINKS = MAX_LINKS_PER_PAGE

COMPLETE = "complete"
BUDGET_LIMITED = "budget_limited"
UNVERIFIED = "unverified"
COMPLETENESS_KEY = "careers_page_completeness"

_JOB_PATH_RE = re.compile(
    r"/(jobs?|careers?|positions?|openings?|vacanc(?:y|ies)|opportunit(?:y|ies)|"
    r"job-openings|current-openings|open-positions|join-us|employment)"
    r"/(?P<slug>[^/?#]{6,120})/?$", re.IGNORECASE)
#: The Comeet WordPress plugin renders positions on the employer's own site
#: as `/careers/co/<location>/<position uid>/<slug>/all` (Upwind, observed
#: 2026-10-22). The uid ("AE.E69") is Comeet's stable position id.
_COMEET_PATH_RE = re.compile(
    r"/co/(?P<loc>[^/?#]{2,80})/(?P<uid>[0-9A-F]{2}\.[0-9A-F]{2,5})/"
    r"(?P<slug>[^/?#]{2,120})(?:/all)?/?$")
_LINKEDIN_JOB_RE = re.compile(
    r"^https?://(?:[a-z]{2,3}\.)?(?:www\.)?linkedin\.com/jobs/view/(?:[^/?#]*-)?(?P<id>\d{6,14})",
    re.IGNORECASE)

#: A job title names a role.
_ROLE_RE = re.compile(
    r"\b(engineer|developer|analyst|manager|specialist|scientist|architect|"
    r"consultant|designer|technician|administrator|coordinator|associate|"
    r"director|accountant|recruiter|intern|representative|nurse|physician|"
    r"assistant|lead|officer|programmer|tester|supervisor|professor|lecturer|"
    r"therapist|pharmacist|attorney|paralegal|operator|mechanic|planner|"
    r"strategist|writer|editor|researcher|instructor|teacher|buyer|estimator|"
    r"superintendent|drafter|chemist|biologist|executive|clerk|controller|"
    r"auditor|bookkeeper|dba|sdet|counsel|generalist|inspector|fabricator|"
    r"machinist|welder|electrician|assembler|hacker)\b", re.IGNORECASE)

#: ...and a section of the site does not, even when it borrows the word.
_SECTION_RE = re.compile(
    r"\b(team|management|leadership|benefits|programs?|services|solutions|"
    r"board|committee|relations|coverage|protection|news|stories|blog|"
    r"training|resources|portal|login|policy|overview|directory|testimonials?)\s*$"
    r"|^(careers?|jobs?|about|our|meet|why|life at|working at|join)\b"
    r"|@",
    re.IGNORECASE)
#: The lighter test a SIBLING of strict matches must pass.
_RELAXED_SECTION_RE = re.compile(
    r"\b(team|benefits|news|stories|blog|resources|portal|login|policy|overview|"
    r"directory|testimonials?|culture|values|faq|events?)\s*$"
    r"|^(careers?|jobs?|about|our|meet|why|life at|working at|join|home|contact|"
    r"view all|see all|all jobs|open positions|open roles|search)\b"
    r"|@|^\W*$",
    re.IGNORECASE)

#: A card's link often wraps its button too: "Software Engineer More Details".
_BUTTON_TEXT_RE = re.compile(
    r"\s*[-|:]?\s*\b(more details|view details|read more|learn more|apply now|"
    r"view job description|job description|apply|view job|view|details)\s*$",
    re.IGNORECASE)
_BUTTON_ONLY_RE = re.compile(
    r"^\s*(more details|view details|read more|learn more|apply now|apply|"
    r"view job description|job description|view job|view|details|see more|"
    r"find out more|more|open|go)\s*[>→›]*\s*$", re.IGNORECASE)
_ARIA_PREFIX_RE = re.compile(
    r"^\s*(read more|learn more|apply now|apply(?: for| to)?|view(?: job)?|"
    r"details)\s*[-:–—]?\s*", re.IGNORECASE)
_POSTING_TEXT_RE = re.compile(
    r"\b(responsibilit|qualification|requirement|experience|apply|"
    r"job description|what you|skills)", re.IGNORECASE)
_EVIDENCE_RE = re.compile(
    r"\b(location|apply|full[- ]?time|part[- ]?time|salary|compensation|"
    r"deadline|remote|hybrid|on[- ]?site|employment type|department|"
    r"job type|contract|posted)\b", re.IGNORECASE)
_EMPTY_TEMPLATE_RE = re.compile(
    r"\bno (?:open |current )?(?:jobs|positions|openings|roles|vacancies)"
    r"(?: (?:found|available|at this time|right now))?\b"
    r"|\bthere are (?:currently )?no (?:open )?(?:positions|openings|jobs|roles)\b",
    re.IGNORECASE)
_LOCATION_LABEL_RE = re.compile(r"\bLocation\s*:\s*([^\n|]{2,120}?)(?=\s+(?:Updated|Deadline|"
                                r"Department|Type|Salary|Posted)\b|$|\n)", re.IGNORECASE)
_DEPARTMENT_LABEL_RE = re.compile(
    r"\b(?:Department|Team)\s*:\s*([^\n|:]{2,60}?)(?=\s+(?:Education|Employment|Location|Type|"
    r"Salary|Posted|Deadline)\b|$|\n)", re.IGNORECASE)
_LOCATIONISH_RE = re.compile(
    r",\s*[A-Z]{2}\b|\b(remote|hybrid|on[- ]?site|united states|usa|anywhere)\b"
    r"|^[A-Z][a-zA-Z .'-]+,\s*[A-Z][a-zA-Z .'-]+$", re.IGNORECASE)

_EMPLOYMENT_RE = re.compile(
    r"^(full[- ]?time|part[- ]?time|contract(?:or)?|temporary|internship|intern|"
    r"permanent|fixed[- ]term|seasonal|freelance|per diem)$", re.IGNORECASE)

_PURPOSE_RULES = (
    ("talent_community", re.compile(
        r"\b(general application|expression of interest|open application|"
        r"talent (?:pool|community|network)|future opportunit|"
        r"don.t see (?:a|your) (?:role|position))", re.IGNORECASE)),
    ("fellowship_or_grant", re.compile(
        r"\b(fellowship|fellows? program|scholars?\b|residency|grants?\b|stipend)",
        re.IGNORECASE)),
    ("volunteer", re.compile(r"\bvolunteer\b", re.IGNORECASE)),
)

_STRIP_RE = re.compile(
    r"<(script|style|nav|header|footer|form|noscript|svg)\b.*?</\1>",
    re.IGNORECASE | re.DOTALL)
_LD_RE = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL)
_HYDRATION_RES = (
    re.compile(r'<script[^>]*id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
               re.IGNORECASE | re.DOTALL),
    re.compile(r"window\.__TANSTACK_DEHYDRATED__\s*=\s*(\{.*?\})\s*;?\s*</script>",
               re.DOTALL),
    re.compile(r"window\.__APOLLO_STATE__\s*=\s*(\{.*?\})\s*;?\s*</script>", re.DOTALL),
    re.compile(r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\})\s*;?\s*</script>", re.DOTALL),
)
_ATS_HOST_RE = re.compile(
    r"(greenhouse\.io|lever\.co|ashbyhq\.com|workable\.com|breezy\.hr|bamboohr\.com|"
    r"rippling\.com|applytojob\.com|recruitee\.com|smartrecruiters\.com|paylocity\.com|"
    r"icims\.com|myworkdayjobs\.com|hibob\.com|jobvite\.com|ultipro\.com|paycomonline\.net|"
    r"recruitingbypaycor\.com|appone\.com|teamtailor\.com|personio\.(?:de|com)|"
    r"pinpointhq\.com|comeet\.(?:com|co)|dover\.com|wellfound\.com|ycombinator\.com)$",
    re.IGNORECASE)
_FORM_HOST_RE = re.compile(
    r"(^forms?\.|typeform\.com$|tally\.so$|jotform\.com$|airtable\.com$|"
    r"docs\.google\.com$|forms\.gle$|hsforms\.com$|formstack\.com$|cognitoforms\.com$)",
    re.IGNORECASE)

_TITLE_KEYS = ("title", "jobTitle", "positionTitle", "name")
_ID_KEYS = ("id", "jobId", "uuid", "requisitionId", "reqId", "jobReqId", "slug")
_STRONG_KEYS = frozenset({"department", "departmentName", "employmentType", "jobType",
                          "applyUrl", "applicationUrl", "applyLink", "workplaceType",
                          "workspaceType", "site", "requisitionId", "jobReqId"})
_EVIDENCE_KEYS = _STRONG_KEYS | frozenset({
    "location", "locations", "locationName", "city", "country", "countryCode",
    "descriptionHtml", "description", "jobDescription", "publishedAt", "postedAt",
    "datePosted", "postedDate", "remote", "isRemote", "requirementsHtml"})


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def _own_host(host: str) -> str:
    return re.sub(r"^www\.", "", str(host or "").strip().lower())


def _same_site(url: str, host: str) -> bool:
    theirs = _own_host(urlparse(url).hostname or "")
    ours = _own_host(host)
    return bool(ours) and (theirs == ours or theirs.endswith(f".{ours}"))


def _ws(value: Any) -> str:
    return re.sub(r"\s+", " ", _html.unescape(str(value or ""))).replace(" ", " ").strip()


def looks_like_a_job_title(text: str) -> bool:
    text = _ws(text)
    if not (6 <= len(text) <= 90) or len(text.split()) > 12:
        return False
    return bool(_ROLE_RE.search(text)) and not _SECTION_RE.search(text)


def _relaxed_title(text: str) -> bool:
    text = _ws(text)
    if not (3 <= len(text) <= 90) or len(text.split()) > 12:
        return False
    if _BUTTON_ONLY_RE.match(text) or not re.search(r"[^\W\d_]{2}", text):
        return False
    return not _RELAXED_SECTION_RE.search(text)


def purpose_hint(*parts: str) -> str:
    """From the posting's own TITLE. Card text is not used: a card's blurb
    ("...work with scholars", "don't see a role?") describes the employer,
    not this posting."""
    for hint, rule in _PURPOSE_RULES:
        if any(rule.search(p or "") for p in parts):
            return hint
    return ""


def application_method_for(url: str) -> str:
    """How a person applies through this URL. `page` = the posting itself."""
    url = str(url or "").strip()
    if url.lower().startswith("mailto:"):
        return "email"
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return "page"
    if host.endswith("linkedin.com"):
        return "linkedin"
    if _ATS_HOST_RE.search(host):
        return "ats_url"
    if _FORM_HOST_RE.search(host):
        return "form"
    return "page"


def _mail_address(href: str) -> str:
    addr = href.split(":", 1)[1] if ":" in href else href
    addr = addr.split("?", 1)[0].strip().strip("\\").lower()
    return addr if re.match(r"^[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,24}$", addr) else ""


def _pattern_key(url: str, host: str) -> str:
    m = _LINKEDIN_JOB_RE.match(url)
    if m:
        return "linkedin"
    if _COMEET_PATH_RE.search(urlparse(url).path):
        return "comeet"
    jm = _JOB_PATH_RE.search(urlparse(url).path)
    if not jm:
        return ""
    path = urlparse(url).path
    return path[: jm.start("slug")].lower()


# ---------------------------------------------------------------------------
# JSON-LD
# ---------------------------------------------------------------------------

def _is_job_posting(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    kind = item.get("@type")
    kinds = kind if isinstance(kind, list) else [kind]
    return any(str(k).lower() == "jobposting" for k in kinds)


def ld_postings(body: str) -> List[Dict[str, Any]]:
    """Every schema.org JobPosting on a page: top level, in a list, inside
    an `@graph`, or typed with an `@type` list."""
    out: List[Dict[str, Any]] = []
    for raw in _LD_RE.findall(body or ""):
        try:
            data = json.loads(raw.strip())
        except (TypeError, ValueError):
            continue
        stack = list(data) if isinstance(data, list) else [data]
        while stack:
            item = stack.pop(0)
            if not isinstance(item, dict):
                continue
            if _is_job_posting(item):
                out.append(item)
            graph = item.get("@graph")
            if isinstance(graph, list):
                stack.extend(graph)
            elif isinstance(item.get("itemListElement"), list):
                for el in item["itemListElement"]:
                    if isinstance(el, dict):
                        stack.append(el.get("item") if isinstance(el.get("item"), dict) else el)
    return out


def _ld_posting(body: str) -> Dict[str, Any]:
    """Back-compat: the first JobPosting on a page, or {}."""
    found = ld_postings(body)
    return found[0] if found else {}


def _ld_location(ld: Dict[str, Any]) -> Tuple[str, str]:
    places = ld.get("jobLocation")
    places = places if isinstance(places, list) else [places]
    out: List[str] = []
    country = ""
    for place in places:
        if not isinstance(place, dict):
            continue
        addr = place.get("address") if isinstance(place.get("address"), dict) else {}
        c = addr.get("addressCountry")
        c = c.get("name") if isinstance(c, dict) else c
        text = ", ".join(x for x in (_ws(addr.get("addressLocality")),
                                     _ws(addr.get("addressRegion")), _ws(c)) if x)
        if text and text not in out:
            out.append(text)
        country = country or _ws(c)
    remote = str(ld.get("jobLocationType") or "").upper() == "TELECOMMUTE"
    loc = "; ".join(out[:4])
    if remote and "remote" not in loc.lower():
        loc = f"{loc}; Remote" if loc else "Remote"
    return loc, country


def _ld_identifier(ld: Dict[str, Any]) -> str:
    ident = ld.get("identifier")
    if isinstance(ident, dict):
        ident = ident.get("value") or ident.get("@id") or ident.get("name")
    ident = _ws(ident)
    return ident if re.match(r"^[A-Za-z0-9._:-]{1,80}$", ident or "") else ""


def _ld_date(ld: Dict[str, Any]) -> Tuple[str, str]:
    raw = ld.get("datePosted")
    if not raw or looks_minted_now(raw):
        return "", _ws(raw)
    return to_iso(raw), _ws(raw)


# ---------------------------------------------------------------------------
# Hydrated job data
# ---------------------------------------------------------------------------

def _job_like(obj: Dict[str, Any]) -> bool:
    title = next((obj.get(k) for k in _TITLE_KEYS if isinstance(obj.get(k), str)), None)
    ident = next((obj.get(k) for k in _ID_KEYS
                  if isinstance(obj.get(k), (str, int)) and str(obj.get(k)).strip()), None)
    if not title or ident is None or not _relaxed_title(title):
        return False
    keys = set(obj)
    return len(keys & _EVIDENCE_KEYS) >= 3 and bool(keys & _STRONG_KEYS)


def hydrated_jobs(body: str) -> List[Dict[str, Any]]:
    """Job-shaped objects in the page's own hydration payloads, deduped."""
    found: Dict[str, Dict[str, Any]] = {}
    for pattern in _HYDRATION_RES:
        for raw in pattern.findall(body or ""):
            try:
                data = json.loads(raw)
            except (TypeError, ValueError):
                continue
            stack: List[Any] = [data]
            visited = 0
            while stack and visited < 200000:
                visited += 1
                node = stack.pop()
                if isinstance(node, dict):
                    if _job_like(node):
                        key = str(next(node.get(k) for k in _ID_KEYS
                                       if node.get(k) not in (None, "")))
                        found.setdefault(key, node)
                        continue
                    stack.extend(node.values())
                elif isinstance(node, list):
                    stack.extend(node)
    return list(found.values())


def _hydrated_row(obj: Dict[str, Any], page_url: str) -> Optional[Dict[str, Any]]:
    title = _ws(next(obj.get(k) for k in _TITLE_KEYS if isinstance(obj.get(k), str)))
    ident = str(next(obj.get(k) for k in _ID_KEYS if obj.get(k) not in (None, "")))
    rid = slugify(ident, 80) if not re.match(r"^[A-Za-z0-9-]{1,80}$", ident) else ident
    if not title or not rid:
        return None
    apply = next((str(obj.get(k)) for k in ("applyUrl", "applicationUrl", "applyLink")
                  if isinstance(obj.get(k), str) and obj.get(k).startswith(("https://", "mailto:"))),
                 "")
    loc_val = obj.get("site") or obj.get("locationName") or obj.get("location") or obj.get("city")
    if isinstance(loc_val, dict):
        loc_val = loc_val.get("name") or loc_val.get("city")
    if isinstance(loc_val, list):
        loc_val = "; ".join(_ws(x.get("name") if isinstance(x, dict) else x) for x in loc_val[:4])
    country = _ws(obj.get("country") if not isinstance(obj.get("country"), dict)
                  else obj["country"].get("name"))
    desc_parts = [html_to_text(obj.get(k) or "") for k in
                  ("descriptionHtml", "description", "jobDescription", "requirementsHtml",
                   "responsibilitiesHtml") if isinstance(obj.get(k), str)]
    raw_posted = next((obj.get(k) for k in ("publishedAt", "postedAt", "datePosted",
                                            "postedDate") if obj.get(k)), None)
    posted = "" if (not raw_posted or looks_minted_now(raw_posted)) else to_iso(raw_posted)
    work = _ws(obj.get("workspaceType") or obj.get("workplaceType") or "")
    return {
        "id": rid, "title": title, "url": apply or page_url, "detail_url": "",
        "apply_url": apply or page_url,
        "location": _ws(loc_val), "country": country,
        "department": _ws(obj.get("department") or obj.get("departmentName") or ""),
        "employment_type": _ws(obj.get("employmentType") or obj.get("jobType") or ""),
        "description": "\n\n".join(p for p in desc_parts if p.strip()),
        "posted_at": posted, "raw_posted": _ws(raw_posted),
        "application_method": application_method_for(apply) if apply else "page",
        "workplace": work, "source": "hydrated",
    }


# ---------------------------------------------------------------------------
# Links and cards
# ---------------------------------------------------------------------------

def _card_of(anchor: Any, candidate_urls: set, own_url: str) -> Any:
    """The largest ancestor (<= 6 levels) holding no OTHER posting link."""
    card = anchor
    node = anchor.getparent()
    depth = 0
    while node is not None and depth < 6:
        hrefs = set()
        for a in node.iter("a"):
            href = str(a.get("_resolved") or "")
            if href in candidate_urls:
                hrefs.add(href)
        if hrefs - {own_url}:
            break
        card = node
        node = node.getparent()
        depth += 1
    return card


def _card_title(card: Any, anchor: Any) -> str:
    for tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
        for h in card.iter(tag):
            text = _BUTTON_TEXT_RE.sub("", _ws(h.text_content())).strip(" -|:")
            if _relaxed_title(text):
                return text
    for el in card.iter():
        if not isinstance(el.tag, str) or el.tag in ("script", "style", "svg", "path"):
            continue
        cls = str(el.get("class") or "").lower()
        if any(k in cls for k in ("title", "job-name", "position-name", "role-name")):
            text = _ws(el.text_content())
            if _relaxed_title(text):
                return text
    for el in card.iter():
        if not isinstance(el.tag, str) or el.tag in ("script", "style", "svg", "path", "a"):
            continue
        if len(el) and any(isinstance(c.tag, str) for c in el):
            continue                       # leaves only
        text = _ws(el.text_content())
        if looks_like_a_job_title(text):
            return text
    return ""


def _card_fields(card: Any, title: str) -> Dict[str, str]:
    texts: List[str] = []
    for el in card.iter():
        if not isinstance(el.tag, str) or el.tag in ("script", "style", "svg", "path"):
            continue
        if len(el) and any(isinstance(c.tag, str) for c in el):
            continue
        text = _ws(el.text_content())
        if text and text != title and not _BUTTON_ONLY_RE.match(text) and len(text) <= 160:
            texts.append(text)
    whole = _ws(" ".join(t for t in card.itertext()))
    out: Dict[str, str] = {"text": whole[:600]}
    for text in texts:
        lm = re.match(r"^(location|department|team)\s*:\s*(.{2,120})$", text, re.IGNORECASE)
        if lm:
            key = "location" if lm.group(1).lower() == "location" else "department"
            out.setdefault(key, lm.group(2).strip())
    m = _LOCATION_LABEL_RE.search(whole)
    if m and "location" not in out:
        out["location"] = m.group(1).strip()
    for text in texts:
        if "location" not in out and len(text) <= 60 and _LOCATIONISH_RE.search(text) \
                and not text.lower().startswith(("location", "updated", "deadline")):
            out["location"] = text
        elif _EMPLOYMENT_RE.match(text):
            out.setdefault("employment_type", text)
    # Department only when the card LABELS it; an unlabelled short string is
    # as often a city or a status line as a team.
    dm = _DEPARTMENT_LABEL_RE.search(whole)
    if dm and "department" not in out:
        out["department"] = dm.group(1).strip()
    return out


def _link_rows(doc: Any, page_url: str, host: str) -> List[Dict[str, Any]]:
    page = page_url.split("#")[0].rstrip("/")
    anchors = []
    for anchor in doc.xpath("//a[@href]"):
        href = str(anchor.get("href") or "").strip()
        if not href or href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        try:
            url = urljoin(page_url, href).split("#")[0]
        except ValueError:
            continue
        li = _LINKEDIN_JOB_RE.match(url)
        if li:
            url = f"https://www.linkedin.com/jobs/view/{li.group('id')}"
        elif not url.startswith("https://") or not _same_site(url, host) or \
                url.rstrip("/") == page or not (
                    _JOB_PATH_RE.search(urlparse(url).path)
                    or _COMEET_PATH_RE.search(urlparse(url).path)):
            continue
        anchor.set("_resolved", url)
        anchors.append((anchor, url))
    candidate_urls = {u for _a, u in anchors}

    per_url: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for anchor, url in anchors:
        # A link that wraps several blocks (name, location, type) names its
        # title block by class; prefer it to the joined text.
        named = [el for el in anchor.iter() if el is not anchor and isinstance(el.tag, str)
                 and re.search(r"(position|job|role)[-_]?(name|title)|\btitle\b",
                               str(el.get("class") or ""), re.IGNORECASE)]
        raw_text = _ws(named[0].text_content()) if named else _ws(anchor.text_content())
        text = _BUTTON_TEXT_RE.sub("", raw_text).strip(" -|:")
        strict = looks_like_a_job_title(text)
        title = text if strict else ""
        if not title:
            aria = _ARIA_PREFIX_RE.sub("", _ws(anchor.get("aria-label") or anchor.get("title")))
            if looks_like_a_job_title(aria):
                title, strict = aria, True
        card = _card_of(anchor, candidate_urls, url)
        if not title:
            title = _card_title(card, anchor)
            strict = looks_like_a_job_title(title)
        if not title and _relaxed_title(text):
            title = text
        if not title:
            continue
        entry = per_url.get(url)
        rank = 2 if strict else 1
        if entry is None:
            order.append(url)
            per_url[url] = {"url": url, "title": title, "strict": strict, "rank": rank,
                            "fields": _card_fields(card, title)}
        elif rank > entry["rank"]:
            entry.update(title=title, strict=strict, rank=rank,
                         fields=_card_fields(card, title))
        if len(order) >= MAX_LINKS_PER_PAGE:
            break

    entries = [per_url[u] for u in order]
    strict_patterns = {_pattern_key(e["url"], host) for e in entries if e["strict"]}
    accepted = [e for e in entries
                if e["strict"] or (_pattern_key(e["url"], host) in strict_patterns
                                   and _relaxed_title(e["title"]))]
    if len(accepted) < MIN_LINKS:
        # One opening is accepted only with posting evidence around it.
        accepted = [e for e in accepted
                    if e["strict"] and _EVIDENCE_RE.search(e["fields"].get("text", "")
                                                           .replace(e["title"], " "))]
        if len(accepted) != 1:
            return []
    rows: List[Dict[str, Any]] = []
    for e in accepted:
        url = e["url"]
        li = _LINKEDIN_JOB_RE.match(url)
        if li:
            rid = f"li-{li.group('id')}"
            method, detail = "linkedin", ""
        else:
            cm = _COMEET_PATH_RE.search(urlparse(url).path)
            match = _JOB_PATH_RE.search(urlparse(url).path)
            if cm:
                rid = f"comeet-{cm.group('uid').lower()}"
            else:
                rid = slugify(match.group("slug") if match else url, 80)
            method, detail = "page", url
        if not rid:
            continue
        rows.append({"id": rid, "title": e["title"], "url": url, "detail_url": detail,
                     "apply_url": url, "location": e["fields"].get("location", ""),
                     "department": e["fields"].get("department", ""),
                     "employment_type": e["fields"].get("employment_type", ""),
                     "application_method": method, "source": "link",
                     "card_text": e["fields"].get("text", "")})
    return rows


def job_links(body: str, page_url: str, host: str) -> List[Tuple[str, str]]:
    """`(url, title)` for every SAME-SITE link on a page that is a posting by
    the rules in the module docstring. Kept for discovery and its tests."""
    try:
        from lxml import html as lxml_html
        doc = lxml_html.fromstring(body or "")
    except Exception:
        return []
    return [(r["url"], r["title"]) for r in _link_rows(doc, page_url, host)
            if r["application_method"] != "linkedin"]


# ---------------------------------------------------------------------------
# Inline role sections
# ---------------------------------------------------------------------------

def _inline_rows(doc: Any, page_url: str) -> List[Dict[str, Any]]:
    mails = [_mail_address(str(a.get("href"))) for a in doc.xpath("//a[starts-with(@href,'mailto:')]")]
    mails = [m for m in mails if m]
    if not mails:
        return []
    rows: Dict[str, Dict[str, Any]] = {}
    for level in ("h2", "h3", "h4"):
        heads = [h for h in doc.iter(level) if looks_like_a_job_title(_ws(h.text_content()))]
        if not heads:
            continue
        for h in heads:
            title = _ws(h.text_content())
            anchor_id = str(h.get("id") or "").strip()
            rid = slugify(anchor_id or title, 80)
            if not rid:
                continue
            # The section body: following siblings up to the next heading of
            # this level.
            parts: List[str] = []
            email = ""
            sib = h.getnext()
            while sib is not None and sib.tag != level:
                if isinstance(sib.tag, str):
                    parts.append(_ws(sib.text_content()))
                    for a in sib.iter("a"):
                        if str(a.get("href") or "").startswith("mailto:") and not email:
                            email = _mail_address(str(a.get("href")))
                sib = sib.getnext()
            rows.setdefault(rid, {
                "id": rid, "title": title,
                "url": f"{page_url.split('#')[0]}#{anchor_id or rid}", "detail_url": "",
                "apply_url": f"mailto:{email or mails[0]}",
                "application_method": "email", "application_email": email or mails[0],
                "description": "\n".join(p for p in parts if p)[:15000],
                "location": "", "department": "", "source": "inline"})
        break
    return list(rows.values())


# ---------------------------------------------------------------------------
# One listing page -> rows
# ---------------------------------------------------------------------------

def next_page_url(doc: Any, page_url: str, host: str) -> str:
    """The page's OWN declared next page, same site and same listing path."""
    cands: List[str] = []
    for el in doc.xpath("//link[@rel='next'][@href] | //a[@rel='next'][@href]"):
        cands.append(str(el.get("href")))
    for a in doc.xpath("//a[@href]"):
        text = _ws(a.text_content()).lower()
        label = _ws(a.get("aria-label")).lower()
        if text in ("next", "next page", "next ›", "›", "»", ">", "older") \
                or label in ("next", "next page"):
            cands.append(str(a.get("href")))
    base = urlparse(page_url)
    for href in cands:
        try:
            url = urljoin(page_url, href).split("#")[0]
        except ValueError:
            continue
        u = urlparse(url)
        if u.scheme != "https" or not _same_site(url, host) or url == page_url:
            continue
        same_path = u.path.rstrip("/") == base.path.rstrip("/")
        paged_path = re.sub(r"/page/\d+/?$", "", u.path.rstrip("/")) == \
            re.sub(r"/page/\d+/?$", "", base.path.rstrip("/"))
        if same_path or paged_path:
            return url
    return ""


def extract_postings(body: str, page_url: str, host: str) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Everything one careers page says about its openings. Never raises."""
    info: Dict[str, Any] = {"next_url": "", "empty_template": bool(
        _EMPTY_TEMPLATE_RE.search(html_to_text(_STRIP_RE.sub(" ", body or "")))),
        "sources": {}}
    rows: Dict[str, Dict[str, Any]] = {}

    def add(row: Optional[Dict[str, Any]], source: str) -> None:
        if not row or not row.get("id") or not row.get("title"):
            return
        key = row["url"] if row.get("source") == "link" else f"{source}:{row['id']}"
        for existing in rows.values():
            if row.get("url") and existing.get("url") == row.get("url") and \
                    existing.get("source") != "hydrated" and source != "hydrated":
                for k, v in row.items():
                    if v and not existing.get(k):
                        existing[k] = v
                return
        rows.setdefault(key, row)
        info["sources"][source] = info["sources"].get(source, 0) + 1

    for ld in ld_postings(body):
        title = _ws(ld.get("title") or ld.get("name"))
        if not _relaxed_title(title):
            continue
        url = _ws(ld.get("url"))
        url = urljoin(page_url, url).split("#")[0] if url else ""
        if url and not _same_site(url, host):
            url = ""
        loc, country = _ld_location(ld)
        posted, raw = _ld_date(ld)
        ident = _ld_identifier(ld)
        slug = ""
        if url:
            jm = _JOB_PATH_RE.search(urlparse(url).path)
            slug = slugify(jm.group("slug"), 80) if jm else ""
        contact = ld.get("applicationContact") if isinstance(ld.get("applicationContact"), dict) else {}
        email = _mail_address("mailto:" + _ws(contact.get("email"))) if contact.get("email") else ""
        add({"id": slug or ident or slugify(title, 80), "title": title,
             "url": url or page_url, "detail_url": url if url and url != page_url else "",
             "apply_url": url or page_url, "location": loc, "country": country,
             "department": "", "description": html_to_text(ld.get("description") or ""),
             "posted_at": posted, "raw_posted": raw,
             "employment_type": _ws(", ".join(ld["employmentType"]) if isinstance(
                 ld.get("employmentType"), list) else ld.get("employmentType")),
             "application_method": "email" if (email and not url) else "page",
             "application_email": email, "source": "jsonld"}, "jsonld")

    for obj in hydrated_jobs(body):
        add(_hydrated_row(obj, page_url), "hydrated")

    try:
        from lxml import html as lxml_html
        doc = lxml_html.fromstring(body) if (body or "").strip() else None
    except Exception:
        doc = None
    if doc is not None:
        for row in _link_rows(doc, page_url, host):
            add(row, "link")
        if not rows:
            for row in _inline_rows(doc, page_url):
                add(row, "inline")
        info["next_url"] = next_page_url(doc, page_url, host)
    return list(rows.values()), info


def endpoint_for_pages(host: str, pages: List[Tuple[str, str]]) -> Optional[Endpoint]:
    """The endpoint for the careers page that lists the most postings, if any
    page a discovery walk fetched lists them at all."""
    best_url, best = "", 0
    for url, body in pages or []:
        count = len(job_links(body, url, host))
        if count > best:
            best_url, best = url, count
    if not best_url:
        return None
    return Endpoint(vendor=VENDOR, tenant=_own_host(host), host=_own_host(host),
                    extra={"listing_url": best_url})


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------

class OwnSiteCareersProvider(AtsProvider):
    vendor = VENDOR
    label = "Company careers page"

    termless = True
    has_detail = True
    date_confidence = DATE_UNKNOWN
    slug_probe = False
    fingerprints: tuple = ()

    #: Empty on purpose - see the SSRF note above.
    api_hosts: tuple = ()
    portal_hosts: tuple = ()

    policy = CrawlPolicy(
        pacing_key=None,
        min_interval=1.0,
        jitter=0.25,
        max_concurrency=1,
        timeout=10.0,
        detail_timeout=10.0,
        challenge_statuses=frozenset({429, 503}),
        page_size=MAX_LINKS_PER_PAGE,
        max_pages=MAX_PAGES,
        stale_page_tolerance=0,
    )

    @staticmethod
    def _listing(endpoint: Endpoint) -> Tuple[str, str]:
        host = _own_host(endpoint.host or endpoint.tenant)
        url = str((endpoint.extra or {}).get("listing_url") or "").strip()
        if not host or not url.startswith("https://") or not _same_site(url, host):
            return "", ""
        return host, url

    @staticmethod
    def _set_completeness(ctx: FetchContext, value: str) -> None:
        try:
            ctx.options[COMPLETENESS_KEY] = value
        except Exception:
            pass
        if value != COMPLETE:
            ctx.listing_truncated = True

    def list_jobs(self, endpoint: Endpoint,
                  ctx: FetchContext) -> Iterator[RawJob]:
        host, url = self._listing(endpoint)
        if not url:
            return
        seen_pages = set()
        seen_ids = set()
        emitted = 0
        state = COMPLETE
        page_url = url
        for page_no in range(MAX_PAGES):
            if ctx.should_stop():
                state = BUDGET_LIMITED
                break
            seen_pages.add(page_url)
            status, body = ctx.get_text(page_url, params={}, timeout=self.policy.timeout,
                                        vendor=self.vendor)
            if self.policy.is_challenge(status, ""):
                raise ChallengeError(self.vendor, status)
            if status != 200 or not body:
                # Not "no jobs": we did not read the page.
                state = UNVERIFIED if page_no == 0 else BUDGET_LIMITED
                break
            rows, info = extract_postings(body, page_url, host)
            if not rows and page_no == 0:
                # Nothing readable - an empty template, a JS shell, or a
                # layout we do not understand. None of those is a zero.
                state = UNVERIFIED
                break
            for row in rows:
                if row["id"] in seen_ids:
                    continue
                if emitted >= MAX_POSTINGS:
                    state = BUDGET_LIMITED
                    break
                seen_ids.add(row["id"])
                emitted += 1
                yield self._to_raw(row)
            if state != COMPLETE:
                break
            nxt = info.get("next_url") or ""
            if not nxt or nxt in seen_pages:
                break
            if page_no + 1 >= MAX_PAGES:
                state = BUDGET_LIMITED
                break
            page_url = nxt
        self._set_completeness(ctx, state)

    @staticmethod
    def _to_raw(row: Dict[str, Any]) -> RawJob:
        extra: Dict[str, Any] = {"application_method": row.get("application_method") or "page",
                                 "listing_source": row.get("source", "")}
        if row.get("application_email"):
            extra["application_email"] = row["application_email"]
        if row.get("application_method") in ("email", "linkedin"):
            # A person applies; nothing is sent or automated on their behalf.
            extra["manual_handoff"] = True
        hint = purpose_hint(row.get("title", ""))
        if hint:
            extra["posting_purpose_hint"] = hint
        if row.get("workplace"):
            extra["workplace"] = row["workplace"]
        posted = row.get("posted_at") or ""
        location = row.get("location") or ""
        return RawJob(
            source_job_id=row["id"],
            title=row["title"][:200],
            apply_url=row.get("apply_url") or row["url"],
            detail_url=row.get("detail_url") or "",
            location=location[:300],
            country=row.get("country") or "",
            department=(row.get("department") or "")[:120],
            employment_type=row.get("employment_type") or "",
            description=(row.get("description") or "")[:15000],
            posted_at=posted,
            date_confidence=DATE_EXACT if posted else DATE_UNKNOWN,
            raw_posted_text=row.get("raw_posted") or "",
            remote=looks_remote(row["title"], location, row.get("workplace", "")),
            extra=extra,
        )

    #: A date the page itself STATES, for pages with no JSON-LD. AGAP
    #: Technologies' page reads "Date Posted:December 12, 2025" and was served
    #: as new ten months later, dated only by our first sighting.
    _STATED_DATE_RE = re.compile(
        r"\b(?:date posted|posted on|posting date|date of posting)\s*[:\-]?\s*"
        r"((?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4}|"
        r"\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{4})", re.IGNORECASE)

    def fetch_detail(self, endpoint: Endpoint, job: RawJob,
                     ctx: FetchContext) -> Optional[RawJob]:
        host, _listing = self._listing(endpoint)
        url = job.detail_url if str(job.detail_url).startswith("https://") \
            else job.apply_url
        if not host or not str(url).startswith("https://") or not _same_site(str(url), host):
            # LinkedIn hand-offs, mailto rows and anything off the employer's
            # host are never fetched.
            return None
        status, body = ctx.get_text(url, params={}, timeout=self.policy.detail_timeout,
                                    vendor=self.vendor)
        if self.policy.is_challenge(status, ""):
            raise ChallengeError(self.vendor, status)
        if status != 200 or not body:
            return None
        posting = _ld_posting(body)
        text = html_to_text(posting.get("description") or "").strip()
        main = _STRIP_RE.sub(" ", body)
        page_text = html_to_text(main).strip()
        if len(text) < 300 and len(page_text) > len(text):
            # A JSON-LD description is sometimes only a summary.
            if len(page_text) >= 300 and _POSTING_TEXT_RE.search(page_text):
                text = page_text if not text else f"{text}\n\n{page_text}"
        if not text:
            return None
        if not posting and (len(text) < 300 or not _POSTING_TEXT_RE.search(text)):
            # A page that does not read as a posting is not described as one.
            return None
        extra = self._application_from_detail(main, posting, url)
        out = RawJob(source_job_id=job.source_job_id, title=job.title,
                     apply_url=job.apply_url, description=text[:15000], extra=extra)
        if not posting:
            m = self._STATED_DATE_RE.search(page_text)
            stated = to_iso(m.group(1)) if m else ""
            if stated:
                out.posted_at, out.raw_posted_text = stated, _ws(m.group(0))
                out.date_confidence = DATE_EXACT
        if posting:
            posted, raw = _ld_date(posting)
            if posted:
                out.posted_at, out.raw_posted_text = posted, raw
                out.date_confidence = DATE_EXACT
            loc, country = _ld_location(posting)
            out.location, out.country = loc, country
            org = posting.get("hiringOrganization")
            if isinstance(org, dict) and org.get("name"):
                out.extra["hiring_organization"] = _ws(org.get("name"))
        return out

    @staticmethod
    def _application_from_detail(main_html: str, posting: Dict[str, Any],
                                 page_url: str) -> Dict[str, Any]:
        """How the posting page says to apply, read from its own content."""
        try:
            from lxml import html as lxml_html
            doc = lxml_html.fromstring(main_html or "<p/>")
        except Exception:
            return {}
        apply_links: List[str] = []
        mails: List[str] = []
        for a in doc.xpath("//a[@href]"):
            href = str(a.get("href") or "").strip()
            label = _ws(a.text_content()) + " " + _ws(a.get("aria-label"))
            if href.startswith("mailto:"):
                addr = _mail_address(href)
                if addr:
                    mails.append(addr)
                continue
            if re.search(r"\bapply\b|application", label, re.IGNORECASE):
                try:
                    apply_links.append(urljoin(page_url, href))
                except ValueError:
                    continue
        for href in apply_links:
            method = application_method_for(href)
            if method in ("ats_url", "linkedin", "form"):
                return {"application_method": method, "application_url": href,
                        **({"manual_handoff": True} if method == "linkedin" else {})}
        contact = posting.get("applicationContact") if isinstance(
            posting.get("applicationContact"), dict) else {}
        email = (mails[0] if mails else "") or (
            _mail_address("mailto:" + _ws(contact.get("email"))) if contact.get("email") else "")
        if email and not apply_links:
            return {"application_method": "email", "application_email": email,
                    "manual_handoff": True}
        if apply_links:
            return {"application_method": "page", "application_url": apply_links[0]}
        return {}

    def board_owner(self, endpoint: Endpoint,
                    ctx: FetchContext) -> Optional[str]:
        return None


register(OwnSiteCareersProvider())
