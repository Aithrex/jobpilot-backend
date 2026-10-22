"""Posting identity: what makes two job rows the SAME requisition.

One rule, shared by every place that collapses, matches or suppresses rows -
the daily pipeline, the served feeds' dedupe, the per-user dismissal set and
the read-only inventory export. Pure functions, no database, no network, so
any caller may import it on a cold path.

The rule (``identity-1.0.0``)
-----------------------------
Two rows are merged ONLY on proven identity, strongest first:

1. ``req``  - the same requisition on the same board: the canonical employer
   (``canonical_employer_id``) or the ATS tenant (``ats`` + ``tenant``),
   plus the provider's requisition id (``requisition_id``, else the id part of
   ``source_job_id``).
2. ``url``  - the same normalized ORIGINAL application URL
   (:func:`apply_url_key`). Tracking parameters are dropped; functional job
   identifiers (``gh_jid``, ``ashby_jid``, ``jobId``, ``token``...) are kept;
   and a URL that names no job at all - a generic ATS/portal landing page -
   yields ``""`` and can never merge anything.
3. ``job``  - the same stored ``job_id`` (the same posting row, seen twice).

Company + title, content similarity and location are NEVER sufficient. They
produce at most a ``possible_duplicate`` hint (:func:`possible_duplicate_key`)
that a reviewer or the card can show, and a row carrying one is still served.
Failing to merge shows a duplicate; wrongly merging hides a real job - the
asymmetry the whole module is built around.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit

IDENTITY_VERSION = "identity-1.0.0"

# ---------------------------------------------------------------------------
# apply_url_key - mirrors the frontend's `applyUrlKey` (src/lib/careerJobs.ts)
# ---------------------------------------------------------------------------

#: The frontend's tracking list, verbatim, plus three that are click ids and
#: never job ids: `jr_id` (Jobright appends one per card, so two cards for one
#: requisition never compared equal), and iCIMS' `iis` / `iisn` source tags.
_TRACKING_PARAM_RE = re.compile(
    r"^(utm_[a-z_]+|gh_src|lever-source|lever-origin|source|src|ref|referrer|trk|"
    r"trackingid|refid|mode|oga|fbclid|gclid|mc_[a-z]+|jr_id|iis|iisn)$",
    re.IGNORECASE)

#: Query parameters that name ONE job. Their presence makes a URL specific.
_JOB_ID_PARAMS = frozenset({
    "gh_jid", "ashby_jid", "jobid", "job_id", "jid", "id", "reqid", "req_id",
    "req", "requisitionid", "requisition_id", "postingid", "posting_id", "jk",
    "vjk", "pid", "opportunityid", "r_id", "jobreqid", "job", "jobreq",
    "currentjobid", "token", "jobpostingid", "jobcode", "job_code",
    "positionid", "position_id", "vacancyid", "jobnumber", "job_number",
})

#: Path words that describe a LISTING, not a job. A URL made only of these
#: (plus a tenant/locale) is a landing page.
_GENERIC_SEGMENTS = frozenset({
    "portal", "careers", "career", "jobs", "job", "search", "job-search",
    "jobsearch", "search-jobs", "searchjobs", "openings", "opportunities",
    "positions", "apply", "application", "applications", "home", "index",
    "index.html", "default.aspx", "candidate", "candidates", "join-us", "joinus",
    "work-with-us", "vacancies", "results", "list", "all", "listing", "listings",
    "jobboard", "board", "embed", "sites", "candidateexperience", "hcmui", "go",
    "pages", "page", "company", "about", "about-us", "en", "us", "global",
    "requisitions", "job-openings", "current-openings", "open-positions",
})

#: Path words after which the next non-generic segment names a job.
_COLLECTION_SEGMENTS = frozenset({
    "job", "jobs", "careers", "career", "position", "positions", "opening",
    "openings", "posting", "postings", "details", "detail", "vacancy",
    "vacancies", "opportunity", "opportunities", "role", "roles", "apply", "p",
    "o", "view", "requisition", "requisitions",
})

#: A job id carried in a hash-routed portal's FRAGMENT: JobDiva
#: `portal/?a=<company>#/jobs/33273835`, RippleHire `#detail/job/61907`. The
#: query names only the company, so without the fragment three different jobs
#: share one URL (Scalence, 2026-10-21 capture).
_FRAGMENT_JOB_RE = re.compile(r"(?:^|[/#?&])(?:jobs?|requisitions?|req|jobid|job_id)[/=]"
                              r"([A-Za-z0-9_-]*\d{2,}[A-Za-z0-9_-]*)", re.IGNORECASE)

_LOCALE_RE = re.compile(r"^[a-z]{2}([-_][a-z]{2,4})?$", re.IGNORECASE)
_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                      re.IGNORECASE)
_DIGIT_RUN_RE = re.compile(r"\d{3,}")

#: Multi-tenant ATS hosts -> how many leading path segments are the TENANT /
#: board (never the job). `None` key matching is by suffix.
_ATS_TENANT_SEGMENTS: Tuple[Tuple[str, int], ...] = (
    ("greenhouse.io", 1),
    ("lever.co", 1),
    ("ashbyhq.com", 1),
    ("apply.workable.com", 1),
    ("smartrecruiters.com", 1),
    ("jobs.jobvite.com", 1),
    ("myworkdayjobs.com", 1),
    ("recruiting.ultipro.com", 3),     # TENANT / JobBoard / <board guid>
    ("recruiting2.ultipro.com", 3),
    ("jobdiva.com", 0),
    ("applytojob.com", 0),
    ("teamtailor.com", 0),
    ("icims.com", 0),
    ("breezy.hr", 0),
    ("oraclecloud.com", 0),
    ("ripplehire.com", 0),
)


def _ats_tenant_segments(host: str) -> Optional[int]:
    for suffix, n in _ATS_TENANT_SEGMENTS:
        if host == suffix or host.endswith("." + suffix):
            return n
    return None


def _is_id_like(segment: str) -> bool:
    s = segment.lower()
    if _UUID_RE.search(s) or _DIGIT_RUN_RE.search(s):
        return True
    # An opaque token: long, mixed letters and digits ("aBc9x0Zq12").
    return len(s) >= 8 and re.search(r"[a-z]", s) is not None \
        and re.search(r"\d", s) is not None and "-" not in s


def is_job_specific_url(url: Any) -> bool:
    """True when the URL names ONE job; False for a listing/portal page.

    The three Scalence cards in the 2026-10-21 Jobright capture all link
    `www1.jobdiva.com/portal/?a=<company token>`: one URL, three different
    jobs. A key built from it would merge them, so it must not be a key.
    """
    parts = _split(url)
    if parts is None:
        return False
    host, path, kept = parts
    if any(k.lower() in _JOB_ID_PARAMS for k, _ in kept):
        return True
    if fragment_job_id(url):
        return True
    segments = [s for s in path.split("/") if s]
    segments = [s for s in segments if not _LOCALE_RE.match(s) or _is_id_like(s)]
    tenant_n = _ats_tenant_segments(host)
    if tenant_n:
        segments = segments[tenant_n:]
    remaining = [s for s in segments if s.lower() not in _GENERIC_SEGMENTS]
    if any(_is_id_like(s) for s in remaining):
        return True
    if tenant_n is not None:
        # A known ATS: anything left after the tenant names a posting.
        return bool(remaining)
    lowered = [s.lower() for s in segments]
    for i, seg in enumerate(lowered[:-1]):
        if seg in _COLLECTION_SEGMENTS and any(
                nxt not in _GENERIC_SEGMENTS for nxt in lowered[i + 1:]):
            return True
    return False


def fragment_job_id(url: Any) -> str:
    """The job id a hash-routed portal keeps after `#`, or ""."""
    raw = str(url or "")
    if "#" not in raw:
        return ""
    m = _FRAGMENT_JOB_RE.search(raw.split("#", 1)[1])
    return m.group(1) if m else ""


def _split(url: Any) -> Optional[Tuple[str, str, List[Tuple[str, str]]]]:
    raw = str(url or "").strip()
    if not raw:
        return None
    try:
        parts = urlsplit(raw)
        host = (parts.hostname or "").lower()
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not host:
        return None
    if host.startswith("www."):
        host = host[4:]
    path = re.sub(r"/+$", "", parts.path or "")
    path = re.sub(r"/(apply|application)$", "", path, flags=re.IGNORECASE)
    kept = sorted((k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                  if not _TRACKING_PARAM_RE.match(k))
    return host, path, kept


def apply_url_key(url: Any) -> str:
    """One posting's application link reduced to what identifies the job.

    Same output format as the frontend's `applyUrlKey` -
    ``host/path?sorted-functional-query`` - with three deliberate additions
    the frontend should adopt: `jr_id`/`iis`/`iisn` are tracking; a job id in
    a hash-routed portal's fragment is kept (`#job=<id>`); and a URL that
    names no specific job returns ``""`` (unusable as a key) rather than a key
    that would collapse every job behind one portal.

    ``""`` never matches anything.
    """
    parts = _split(url)
    if parts is None:
        return ""
    host, path, kept = parts
    if not is_job_specific_url(url):
        return ""
    query = ("?" + urlencode(kept)) if kept else ""
    frag = fragment_job_id(url)
    # The frontend drops the fragment entirely; for a hash-routed portal that
    # is the only place the job id lives, so it is kept here as `#job=<id>`.
    return f"{host}{path}{query}" + (f"#job={frag}" if frag else "")


# ---------------------------------------------------------------------------
# Requisition identity
# ---------------------------------------------------------------------------

def requisition_id_of(row: Dict[str, Any]) -> str:
    """The provider's own id for the requisition, or ""."""
    for key in ("requisition_id", "employer_requisition_id"):
        val = str(row.get(key) or "").strip()
        if val:
            return val
    sjid = str(row.get("source_job_id") or "").strip()
    if ":" in sjid:
        sjid = sjid.rsplit(":", 1)[-1].strip()
    return sjid


def board_key_of(row: Dict[str, Any]) -> str:
    """Who owns the requisition number space: canonical employer, else the
    ATS tenant. "" when neither is proven - a bare company NAME never is."""
    canonical = str(row.get("canonical_employer_id") or "").strip()
    if canonical:
        return f"emp:{canonical}"
    ats = str(row.get("ats") or "").strip().lower()
    tenant = str(row.get("tenant") or "").strip().lower()
    if ats and tenant:
        return f"ats:{ats}:{tenant}"
    return ""


def requisition_key(row: Dict[str, Any]) -> str:
    board = board_key_of(row)
    req = requisition_id_of(row)
    return f"{board}|{req}" if board and req else ""


def original_url_of(row: Dict[str, Any]) -> str:
    for key in ("canonical_apply_url", "original_job_url", "canonical_url",
                "apply_link", "apply_url", "url"):
        val = str(row.get(key) or "").strip()
        if val:
            return val
    return ""


def identity_keys(row: Dict[str, Any]) -> List[str]:
    """Every PROVEN identity this row has, strongest first."""
    keys: List[str] = []
    req = requisition_key(row)
    if req:
        keys.append("req|" + req)
    url = str(row.get("apply_url_key") or "") or apply_url_key(original_url_of(row))
    if url:
        keys.append("url|" + url)
    jid = str(row.get("job_id") or "").strip()
    if jid:
        keys.append("job|" + jid)
    return keys


# ---------------------------------------------------------------------------
# Possible duplicates - a HINT, never a merge
# ---------------------------------------------------------------------------

def _norm_text(value: Any) -> str:
    t = str(value or "").lower()
    t = re.sub(r"[\(\[][^)\]]*[\)\]]", " ", t)
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def possible_duplicate_key(row: Dict[str, Any]) -> str:
    """Employer + title + location: rows sharing this MAY be one job.

    Supporting evidence only. Two Microsoft "Software Engineer" cards in one
    city with different requisitions share it and are still two jobs.
    """
    employer = str(row.get("canonical_employer_id") or "").strip() or _norm_text(
        row.get("company") or row.get("employer_name"))
    title = _norm_text(row.get("title") or row.get("title_raw"))
    loc = _norm_text(row.get("location"))
    if not loc:
        locs = row.get("locations") or []
        if locs and isinstance(locs[0], dict):
            loc = _norm_text(locs[0].get("key") or locs[0].get("raw"))
    if not (employer and title):
        return ""
    return f"{employer}|{title}|{loc}"


# ---------------------------------------------------------------------------
# Grouping (daily pipeline, export)
# ---------------------------------------------------------------------------

def _title_tokens(row: Dict[str, Any]) -> set:
    return {t for t in _norm_text(row.get("title") or row.get("title_raw")).split()
            if t not in ("job", "details", "the", "a", "an", "and", "of")}


def titles_compatible(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    """Do two titles plausibly describe one job? Used ONLY to refuse a URL
    merge whose URL turns out to front several different roles."""
    ta, tb = _title_tokens(a), _title_tokens(b)
    if not ta or not tb:
        return True
    small, big = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    return len(small & big) / len(small) >= 0.6


def group_rows(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Union rows on proven identity. Returns
    ``{"group_of": [group_id per row], "possible_duplicate_of": [row index|None],
    "basis": [key kind that merged it | ""]}``.

    A URL key that would join rows with incompatible titles is treated as a
    portal for that pair (no merge) - the content check can VETO a merge,
    it can never CREATE one.
    """
    n = len(rows)
    parent = list(range(n))
    basis = [""] * n

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    first_by_key: Dict[str, int] = {}
    for i, row in enumerate(rows):
        for key in identity_keys(row):
            j = first_by_key.get(key)
            if j is None:
                first_by_key[key] = i
                continue
            if key.startswith("url|") and not titles_compatible(rows[i], rows[j]):
                continue
            ri, rj = find(i), find(j)
            if ri != rj:
                parent[ri] = rj
                if not basis[i]:
                    basis[i] = key.split("|", 1)[0]
    group_of = []
    label_of_root: Dict[int, str] = {}
    used: set = set()
    for i in range(n):
        root = find(i)
        label = label_of_root.get(root)
        if label is None:
            keys = identity_keys(rows[root])
            label = keys[0] if keys else f"row|{root}"
            if label in used:          # a vetoed shared URL: keep groups apart
                label = f"{label}#{root}"
            used.add(label)
            label_of_root[root] = label
        group_of.append(label)
    # Possible duplicates: same soft key, DIFFERENT proven group.
    soft_first: Dict[str, int] = {}
    possible: List[Optional[int]] = [None] * n
    for i, row in enumerate(rows):
        soft = possible_duplicate_key(row)
        if not soft:
            continue
        j = soft_first.get(soft)
        if j is None:
            soft_first[soft] = i
        elif group_of[j] != group_of[i]:
            possible[i] = j
    return {"group_of": group_of, "possible_duplicate_of": possible, "basis": basis}


def annotate_possible_duplicates(rows: List[Dict[str, Any]]) -> int:
    """Stamp ``possible_duplicate_of`` (the earlier card's job_id) on served
    cards that share employer+title+location with a card of a DIFFERENT
    identity. Nothing is removed. Returns the number stamped."""
    seen: Dict[str, Dict[str, Any]] = {}
    stamped = 0
    for row in rows:
        soft = possible_duplicate_key(row)
        if not soft:
            continue
        prev = seen.get(soft)
        if prev is None:
            seen[soft] = row
            continue
        if prev.get("job_id") and prev.get("job_id") != row.get("job_id"):
            row["possible_duplicate_of"] = prev.get("job_id")
            stamped += 1
    return stamped


# ---------------------------------------------------------------------------
# Repost hint - a REVIEW signal, never a hide
# ---------------------------------------------------------------------------

def location_key_of(row: Dict[str, Any]) -> str:
    locs = row.get("locations") or []
    if isinstance(locs, list) and locs and isinstance(locs[0], dict):
        loc = _norm_text(locs[0].get("raw") or locs[0].get("key"))
        if loc:
            return loc
    return _norm_text(row.get("location"))


def repost_key(row: Dict[str, Any]) -> str:
    """Board-owner + normalized title + location.

    Production probe 2026-10-21: of 579 applied saved_jobs whose posting is
    gone, ZERO live postings carry the same normalized apply link - an
    employer that reposts issues a NEW requisition id and URL (Rippling uuid,
    JazzHR id). This key is how such a repost is RECOGNISED; it is never how
    it is hidden, because the same shape also describes genuine multiple
    openings (JazzHR: 91 tenant+title+location groups / 207 active rows).
    Requires a proven board owner - a company NAME never qualifies.
    """
    board = board_key_of(row)
    title = _norm_text(row.get("title") or row.get("title_raw"))
    if not (board and title):
        return ""
    return f"{board}|{title}|{location_key_of(row)}"


# ---------------------------------------------------------------------------
# Suppression (dismissed / not interested) and prior applications
# ---------------------------------------------------------------------------

def suppression_keys(row: Dict[str, Any]) -> Dict[str, str]:
    """The keys a dismissal of THIS row is remembered under."""
    return {"apply_url_key": str(row.get("apply_url_key") or "")
            or apply_url_key(original_url_of(row)),
            "requisition_key": requisition_key(row),
            "repost_key": repost_key(row)}


class EngagementIndex:
    """What this user already said no to or applied to, matched in layers.

    HIDE (dismissals only): job_id -> dedupe group -> apply-URL key ->
    (board, requisition id). ANNOTATE (never hide): a prior application
    matched the same way -> ``previously_applied``; a dismissal or application
    whose board + title + location matches -> ``possible_repost_of``.
    Company + title alone never matches anything.
    """

    def __init__(self, suppressed: Optional[Dict[str, Any]]):
        s = suppressed or {}
        self.job_ids = set(s.get("job_ids") or ())
        self.groups = set(s.get("dedupe_group_ids") or ())
        self.urls = set(s.get("apply_url_keys") or ())
        self.reqs = set(s.get("requisition_keys") or ())
        self.dismissed_reposts: Dict[str, str] = dict(s.get("repost_keys") or {})
        applied = s.get("applied") or {}
        self.applied: Dict[str, Dict[str, Any]] = dict(applied.get("keys") or {})
        self.applied_reposts: Dict[str, Dict[str, Any]] = dict(applied.get("repost") or {})

    def empty(self) -> bool:
        return not (self.job_ids or self.groups or self.urls or self.reqs
                    or self.dismissed_reposts or self.applied or self.applied_reposts)

    def _keys(self, row: Dict[str, Any]) -> Dict[str, str]:
        if self.urls or self.reqs or self.applied or self.dismissed_reposts \
                or self.applied_reposts:
            return suppression_keys(row)
        return {"apply_url_key": "", "requisition_key": "", "repost_key": ""}

    def hides(self, row: Dict[str, Any], keys: Optional[Dict[str, str]] = None) -> str:
        """The layer that hides this row ("" = not hidden)."""
        if str(row.get("job_id") or "") in self.job_ids:
            return "job_id"
        gid = str(row.get("dedupe_group_id") or "")
        if gid and gid in self.groups:
            return "dedupe_group"
        keys = keys if keys is not None else self._keys(row)
        if keys["apply_url_key"] and keys["apply_url_key"] in self.urls:
            return "apply_url"
        if keys["requisition_key"] and keys["requisition_key"] in self.reqs:
            return "requisition"
        return ""

    def annotate(self, row: Dict[str, Any], keys: Optional[Dict[str, str]] = None) -> None:
        keys = keys if keys is not None else self._keys(row)
        for layer, key in (("job_id", "job|" + str(row.get("job_id") or "")),
                           ("apply_url", "url|" + keys["apply_url_key"]),
                           ("requisition", "req|" + keys["requisition_key"])):
            if key.endswith("|"):
                continue
            hit = self.applied.get(key)
            if hit:
                row["_previously_applied"] = {"status": hit.get("status"),
                                              "matched_on": layer,
                                              "job_id": hit.get("job_id")}
                return
        rk = keys["repost_key"]
        if not rk:
            return
        hit = self.applied_reposts.get(rk)
        if hit and hit.get("job_id") != row.get("job_id"):
            row["_possible_repost_of"] = {"kind": "applied", "job_id": hit.get("job_id"),
                                          "status": hit.get("status"),
                                          "basis": "same employer board, title and location"}
            return
        dismissed = self.dismissed_reposts.get(rk)
        if dismissed and dismissed != row.get("job_id"):
            row["_possible_repost_of"] = {"kind": "dismissed", "job_id": dismissed,
                                          "basis": "same employer board, title and location"}


def is_suppressed(row: Dict[str, Any], suppressed: Optional[Dict[str, Any]]) -> bool:
    """Does a stored dismissal cover this row? Convenience over EngagementIndex."""
    return bool(EngagementIndex(suppressed).hides(row))
