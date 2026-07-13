"""ATS detection — classify an ``apply_url`` into an ATS family and pull the
stable identifiers each family's public API needs.

Why this exists: the auto-apply orchestrator stores an ``ats_family`` on the
ledger row, but it is *client-supplied* and often absent (a raw apply URL is all
we have). The executor needs a reliable, dependency-free way to (a) decide which
recon/submit strategy applies and (b) extract the board token / site / org / job
id from the URL so the family's public JSON API can be called. This module is
that map — pure functions, no network, no state.

The family strings returned here match the lowercased vocabulary the rest of the
codebase already uses (``autonomy_service.ATS_EASE`` keys): greenhouse, lever,
ashby, workday, icims, smartrecruiters, workable, … plus ``None`` for unknown.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Family constants (subset we act on; superset is autonomy_service.ATS_EASE)
# ---------------------------------------------------------------------------
GREENHOUSE = "greenhouse"
LEVER = "lever"
ASHBY = "ashby"
WORKDAY = "workday"
ICIMS = "icims"
SMARTRECRUITERS = "smartrecruiters"
WORKABLE = "workable"

# Host substring → family. Ordered longest/most-specific first; matched against
# the URL host (case-insensitive). A substring test (not exact host) tolerates
# the many subdomain shapes each ATS uses (boards.greenhouse.io,
# job-boards.greenhouse.io, boards-api.greenhouse.io, {co}.jobs.ashbyhq.com …).
_HOST_MAP = (
    ("greenhouse.io", GREENHOUSE),
    ("grnh.se", GREENHOUSE),           # Greenhouse short-links
    ("lever.co", LEVER),
    ("ashbyhq.com", ASHBY),
    ("myworkdayjobs.com", WORKDAY),
    ("myworkdaysite.com", WORKDAY),
    ("icims.com", ICIMS),
    ("smartrecruiters.com", SMARTRECRUITERS),
    ("workable.com", WORKABLE),
)


def detect_family(apply_url: Optional[str]) -> Optional[str]:
    """Return the ATS family for ``apply_url`` (lowercased), or None if unknown
    / unparseable. Never raises."""
    if not apply_url or not isinstance(apply_url, str):
        return None
    try:
        host = (urlparse(apply_url.strip()).hostname or "").lower()
    except Exception:
        return None
    if not host:
        return None
    for needle, family in _HOST_MAP:
        if needle in host:
            return family
    return None


# --- per-family id extractors ----------------------------------------------
# Greenhouse: boards.greenhouse.io/{token}/jobs/{id}  |  boards-api.../boards/{token}/jobs/{id}
#             job-boards.greenhouse.io/{token}/jobs/{id}  |  ?gh_jid={id} embeds
_GH_TOKEN_JOB = re.compile(r"/(?:boards/)?([A-Za-z0-9_.-]+)/jobs/(\d+)")
_GH_EMBED = re.compile(r"[?&]gh_jid=(\d+)")

# Lever: jobs.lever.co/{site}/{uuid}  (uuid may be absent on the listing page)
_LEVER = re.compile(r"lever\.co/([A-Za-z0-9_.-]+)(?:/([0-9a-fA-F-]{16,}))?")

# Ashby: jobs.ashbyhq.com/{org}/{uuid}  |  {org}.ashbyhq.com/...
_ASHBY_PATH = re.compile(r"ashbyhq\.com/([A-Za-z0-9_.-]+)(?:/([0-9a-fA-F-]{16,}))?")


def _greenhouse_ids(url: str, host: str) -> Dict[str, Any]:
    m = _GH_TOKEN_JOB.search(url)
    ids: Dict[str, Any] = {}
    if m:
        token, jid = m.group(1), m.group(2)
        # Guard against matching the API's own "boards" path segment as a token.
        if token.lower() != "v1":
            ids["board_token"] = token
        ids["job_id"] = jid
    embed = _GH_EMBED.search(url)
    if embed and "job_id" not in ids:
        ids["job_id"] = embed.group(1)
    return ids


def _lever_ids(url: str) -> Dict[str, Any]:
    m = _LEVER.search(url)
    if not m:
        return {}
    ids: Dict[str, Any] = {"site": m.group(1)}
    if m.group(2):
        ids["posting_id"] = m.group(2)
    return ids


def _ashby_ids(url: str, host: str) -> Dict[str, Any]:
    m = _ASHBY_PATH.search(url)
    ids: Dict[str, Any] = {}
    if m:
        # jobs.ashbyhq.com/{org}/... — first path segment is the org.
        ids["org"] = m.group(1)
        if m.group(2):
            ids["posting_id"] = m.group(2)
    # {org}.ashbyhq.com subdomain form.
    if "org" not in ids:
        sub = host.split(".ashbyhq.com")[0].split(".")[-1]
        if sub and sub not in ("jobs", "www", "api"):
            ids["org"] = sub
    return ids


def detect_ats(apply_url: Optional[str],
               declared_family: Optional[str] = None) -> Tuple[Optional[str], Dict[str, Any]]:
    """Classify ``apply_url`` → ``(family, ids)``.

    ``declared_family`` (the client-supplied ledger ``ats_family``) is trusted as
    a hint but the URL is authoritative for the family we actually act on — a
    mismatch (client said 'lever', URL is greenhouse) resolves to the URL, since
    the id-extraction only works for the true host. ``ids`` is a family-specific
    dict (board_token/job_id, site/posting_id, org/posting_id) — possibly empty
    if the URL is a listing page without a job id. Never raises.
    """
    family = detect_family(apply_url)
    if family is None and declared_family:
        # URL unrecognized but the client told us — honor it (no ids extractable).
        fam = declared_family.strip().lower() or None
        return fam, {}
    if family is None:
        return None, {}

    try:
        host = (urlparse(apply_url.strip()).hostname or "").lower()
        url = apply_url.strip()
    except Exception:
        return family, {}

    if family == GREENHOUSE:
        return family, _greenhouse_ids(url, host)
    if family == LEVER:
        return family, _lever_ids(url)
    if family == ASHBY:
        return family, _ashby_ids(url, host)
    # workday/icims/smartrecruiters/workable: family known, ids left to the
    # browser executor (their URLs are tenant-specific and not needed for the
    # HTTP recon path, which we don't offer for them yet).
    return family, {}
