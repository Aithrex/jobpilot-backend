#!/usr/bin/env python3
"""
probe_new_ats.py — find out whether an UNSUPPORTED ATS can be fetched, and what
its responses actually look like, before anyone writes a fetcher for it.

Why this is a different tool from discover_career_pages.py
----------------------------------------------------------
That script validates a candidate company against an ATS we already support: it
calls the real `FETCHERS[ats]` and asks "does this slug return postings". That
only works because the endpoint and the response shape are already known.

For an ATS we do NOT support, none of that is known. Writing such a fetcher from
memory produces code that looks complete, passes tests written to match the same
guesses, and returns nothing in production. `mnc_catalog.py` says it plainly:
"Re-verify rather than hand-guessing new rows."

So this script does the step that has to come first. It treats every endpoint
below as a HYPOTHESIS, tries it against real tenants, and reports what came
back: status, content type, whether the payload is JSON, where the job array
lives, and which keys carry the title / url / date / location a fetcher needs.

It asserts nothing. It prints evidence.

What it has already established (2026-08-05)
--------------------------------------------
Run against the employers `docs/workday-tenant-expansion.md` §6.4 names. Recorded
here so the next person does not repeat the requests:

* **iCIMS** (emory, rivian): every pattern returns HTML, no JSON, no embedded job
  payload. INDEPENDENTLY CONFIRMS the doc's Tier C verdict. Do not build.
* **NeoGov / governmentjobs.com** (washington, seattle): **FETCHABLE**, via
  `POST /careers/{slug}/home/loadJobsOnMaps` with an `X-Requested-With:
  XMLHttpRequest` header and an empty form body. 706 jobs for `washington` and
  75 for `seattle` in ONE request. Records carry 41 fields including
  `JobTitle`, `PostingDate` (a real date, so CONFIDENCE_EXACT rather than the
  ADR-001 unknown case), `Location`, `FullDescription` and `SalaryInfo`
  ("$60.55 - $70.40 Hourly", which `salary_normalizer` parses). No URL field:
  the apply link has to be built as `/careers/{slug}/jobs/{ID}`.

  Worth recording HOW this was nearly missed. Every GET returned 404 and the
  served HTML says "0 jobs found", so the first pass concluded "client-rendered,
  no static route" - confidently, and wrongly. The endpoint was found by reading
  the page's own JS bundle (`AgencyPages/search`), which builds
  `routePrefix + "/home/loadJobsOnMaps"`. A GET-only prober cannot see a
  POST-only API, which is why `expand()` now carries a method.

* Taleo, SuccessFactors, Jobvite, BambooHR: NOT yet probed against verified
  tenants. The slugs were not confirmed, and probing guessed slugs proves
  nothing - a 404 from a wrong slug looks exactly like a dead endpoint.

Usage
-----
    # probe one vendor against a few known tenant slugs
    PYTHONPATH=shared/python python scripts/probe_new_ats.py bamboohr \\
        --tenants acme,globex

    # a file of "Display Name | slug" lines, and dump a sample record
    PYTHONPATH=shared/python python scripts/probe_new_ats.py icims \\
        --file tenants.txt --sample

    # every vendor at once
    PYTHONPATH=shared/python python scripts/probe_new_ats.py all --tenants acme

Exit code is 0 if at least one (vendor, pattern) returned a usable job array,
1 otherwise - so it can gate a follow-up task in CI or a runbook step.

Nothing here writes to a catalog or to Mongo.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

try:
    import requests
except ImportError:  # pragma: no cover - dev tool
    print("requests is required: pip install -r shared/python/requirements.txt",
          file=sys.stderr)
    raise SystemExit(2)


# ---------------------------------------------------------------------------
# The hypotheses.
#
# Every URL here is a CANDIDATE, not a documented fact. They are the shapes
# these vendors are commonly deployed behind; which (if any) answers for a given
# tenant is exactly what this script exists to find out. Several of these
# vendors are HTML-first with no public JSON at all, and establishing THAT is a
# real result - it means the employer needs a different acquisition route, not a
# fetcher.
#
# `{slug}` is the tenant identifier. `{n}` is expanded over `NUMERIC_RANGE` for
# vendors that shard tenants across numbered hosts (SuccessFactors does this).
# ---------------------------------------------------------------------------
NUMERIC_RANGE = (1, 2, 3, 4, 5, 8, 10, 11, 12)

PATTERNS: Dict[str, List[str]] = {
    "bamboohr": [
        "https://{slug}.bamboohr.com/careers/list",
        "https://{slug}.bamboohr.com/jobs/embed2.php",
    ],
    "jobvite": [
        "https://jobs.jobvite.com/api/v1/company/{slug}/jobs",
        "https://jobs.jobvite.com/careers/{slug}/jobs.json",
        "https://jobs.jobvite.com/{slug}/search",
    ],
    "icims": [
        "https://careers-{slug}.icims.com/jobs/search?in_iframe=1",
        "https://{slug}.icims.com/jobs/search?in_iframe=1",
        "https://careers-{slug}.icims.com/jobs/search?pr=0&in_iframe=1&format=json",
    ],
    "successfactors": [
        "https://career{n}.successfactors.com/career?company={slug}",
        "https://career{n}.successfactors.eu/career?company={slug}",
        "https://performancemanager{n}.successfactors.com/career?company={slug}",
    ],
    "taleo": [
        "https://{slug}.taleo.net/careersection/rest/jobboard/searchjobs",
        "https://tbe.taleo.net/CR/ats/careersite/searchResults.jsp?org={slug}",
        "https://{slug}.taleo.net/careersection/jobsearch.ftl",
    ],
    # The two docs/workday-tenant-expansion.md §6.4 actually recommends
    # ("NeoGov (big win, do first) and PeopleAdmin"), as opposed to the three
    # above, which the same section lists as Tier C - bot-walled or gated, with
    # an explicit "Do not build fetchers". They are kept here anyway so the
    # claim can be RE-TESTED cheaply rather than inherited: a vendor that was
    # gated a year ago may not be, and vice versa.
    # neogov GRADUATED (2026-08-05): it is now a real fetcher
    # (`career_pages_service._fetch_neogov`) with 10 verified agencies in the
    # catalog, so it is deliberately no longer probed here - this script is for
    # vendors we cannot read yet. The discovery record stays in the module
    # docstring above, because the lesson generalises: it was found only after
    # switching from GET to POST.
    "peopleadmin": [
        "https://{slug}.peopleadmin.com/postings/search.json",
        "https://{slug}.peopleadmin.com/postings/search",
        "https://jobs.{slug}.edu/postings/search.json",
    ],
}

# Keys that, in a record, plausibly carry each field a fetcher must map. Ordered
# best-guess first; the report prints which ones were actually present so the
# mapping is read off evidence rather than assumed.
FIELD_HINTS: Dict[str, Tuple[str, ...]] = {
    "id": ("id", "jobid", "job_id", "requisitionid", "reqid", "shortcode", "slug",
           "externalid", "jobreqid"),
    "title": ("title", "jobtitle", "name", "text", "postingtitle", "job_title"),
    "url": ("url", "absolute_url", "hostedurl", "joburl", "applyurl", "link",
            "canonicalurl", "externalpath", "apply_link"),
    "posted_at": ("posted", "posteddate", "postedon", "publisheddate", "publishedat",
                  "first_published", "createdat", "created_at", "datePosted",
                  "postingdate", "releaseddate", "job_posted_date"),
    "location": ("location", "locations", "city", "joblocation", "office",
                 "primarylocation", "locationstext", "region"),
    "description": ("description", "descriptionplain", "jobdescription", "content",
                    "summary", "body"),
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; JobPilotDiscovery/1.0)",
    "Accept": "application/json, text/html;q=0.9",
}
TIMEOUT = 15.0
# Politeness: these are real employer endpoints and this script can fan out over
# dozens of tenants. Same reasoning as HostPolicy in the ingest worker, minus
# the concurrency - a discovery run has no deadline to race.
HOST_MIN_INTERVAL = 1.0
_last_hit: Dict[str, float] = defaultdict(float)


def _polite(url: str) -> None:
    host = (urlparse(url).hostname or "").lower()
    wait = _last_hit[host] + HOST_MIN_INTERVAL - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _last_hit[host] = time.monotonic()


# ---------------------------------------------------------------------------
# Response inspection
# ---------------------------------------------------------------------------

def _norm_key(k: Any) -> str:
    return str(k).lower().replace("-", "").replace("_", "")


def _lower_keys(d: Dict[str, Any]) -> Dict[str, Any]:
    return {_norm_key(k): v for k, v in d.items()}


# Hints normalized the SAME way as the keys they are matched against. Written
# with underscores above for readability, which meant `first_published` never
# matched a key normalized to `firstpublished` - a vendor shipping a perfectly
# good date field would have been reported as having none.
_NORM_HINTS: Dict[str, Tuple[str, ...]] = {
    field: tuple(_norm_key(h) for h in hints) for field, hints in FIELD_HINTS.items()
}


def find_job_array(payload: Any, _depth: int = 0) -> Tuple[Optional[List[Dict]], str]:
    """The most job-like list of dicts in a payload, and the path to it.

    Vendors bury the array under wildly different keys (`jobs`, `result`,
    `content`, `requisitionList`, `data.jobs`), so this looks for the SHAPE -
    the largest list of mappings whose records carry a title-ish key - instead
    of a key name. That is the same reasoning `salary_normalizer` uses: match
    structure, because names are the part that varies per vendor.
    """
    # (jobs, path, rank) where rank is (has_title, length). A titled array beats
    # an untitled one at ANY length: payloads routinely carry a 50-entry facet or
    # location list next to a 2-job result, and scoring on length alone picked
    # the facets. "Looks like a job" is the signal; "is big" is not.
    best: Tuple[Optional[List[Dict]], str, Tuple[int, int]] = (None, "", (0, 0))

    def walk(node: Any, path: str, depth: int) -> None:
        nonlocal best
        if depth > 6:
            return
        if isinstance(node, list):
            dicts = [x for x in node if isinstance(x, dict)]
            if dicts:
                keys = _lower_keys(dicts[0])
                titled = any(h in keys for h in _NORM_HINTS["title"])
                rank = (1 if titled else 0, len(dicts))
                if rank > best[2]:
                    best = (dicts, path or "<root>", rank)
            for i, item in enumerate(node[:3]):
                walk(item, f"{path}[{i}]", depth + 1)
        elif isinstance(node, dict):
            for k, v in node.items():
                walk(v, f"{path}.{k}" if path else str(k), depth + 1)

    walk(payload, "", _depth)
    return best[0], best[1]


def infer_fields(record: Dict[str, Any]) -> Dict[str, Optional[str]]:
    """Which real key carries each field a fetcher needs. None = not found,
    which is itself the finding (no date key means no honest `posted_at`, and
    ADR-001 forbids substituting now())."""
    keys = _lower_keys(record)
    out: Dict[str, Optional[str]] = {}
    for field, hints in _NORM_HINTS.items():
        found = next((h for h in hints if h in keys and keys[h] not in (None, "", [], {})), None)
        if found is None:
            # second pass: substring match, for keys like "jobPostingDate"
            found = next((k for k in keys
                          if any(h in k for h in hints) and keys[k] not in (None, "", [], {})),
                         None)
        out[field] = found
    return out


_BLOB_PATTERNS: Tuple[Tuple[str, str], ...] = (
    ("__INITIAL_STATE__", r"__INITIAL_STATE__\s*=\s*(\{.*?\})\s*;?\s*</script>"),
    ("__NEXT_DATA__", r'__NEXT_DATA__[^>]*>\s*(\{.*?\})\s*</script>'),
    ("__NUXT__", r"__NUXT__\s*=\s*(\{.*?\})\s*;?\s*</script>"),
    ("ld+json", r'<script[^>]*application/ld\+json[^>]*>\s*(.*?)\s*</script>'),
    ("apollo", r"__APOLLO_STATE__\s*=\s*(\{.*?\})\s*;?\s*</script>"),
)


def _embedded_jobs(body: str) -> Tuple[Optional[List[Dict]], str, str]:
    """A job array embedded in an HTML page, if one is really there.

    Returns (jobs, path_within_blob, blob_name). Parsing is what makes this
    evidence rather than a guess - a blob that will not parse, or parses to
    something with no job-shaped array in it, is not a fetching route.
    """
    for name, rx in _BLOB_PATTERNS:
        for match in re.finditer(rx, body, re.S):
            raw = match.group(1).strip()
            if len(raw) < 40:
                continue
            try:
                data = json.loads(raw)
            except ValueError:
                continue
            jobs, path = find_job_array(data)
            if jobs:
                return jobs, path, name
    return None, "", ""


def probe(url: str, *, method: str = "GET",
          data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """One request. Never raises - a dead endpoint is a result, not an error."""
    _polite(url)
    result: Dict[str, Any] = {"url": url, "method": method}
    headers = dict(HEADERS)
    if method == "POST":
        # These endpoints are the site's own XHR calls; several 404 or 500
        # without it, which reads as "no such endpoint".
        headers["X-Requested-With"] = "XMLHttpRequest"
    try:
        if method == "POST":
            resp = requests.post(url, data=data or {}, headers=headers,
                                 timeout=TIMEOUT, allow_redirects=True)
        else:
            resp = requests.get(url, headers=headers, timeout=TIMEOUT,
                                allow_redirects=True)
    except Exception as exc:
        result.update(status=None, verdict="network", detail=type(exc).__name__)
        return result

    result["status"] = resp.status_code
    ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
    result["content_type"] = ctype
    result["bytes"] = len(resp.content or b"")

    if resp.status_code != 200:
        result["verdict"] = "http_error"
        return result

    body = resp.text or ""
    try:
        payload = resp.json()
    except ValueError:
        # HTML is a legitimate answer and worth characterising: an embedded JSON
        # payload means a fetcher is still possible, a pure server-rendered page
        # means this employer needs a different route entirely.
        #
        # The blob must actually CONTAIN JOBS to count. Merely finding
        # `window.<something> = {` is not evidence: governmentjobs.com carries
        # `window.topResume`, `window.topInterview` and `window.Resources` on a
        # fully server-rendered page, and matching on the assignment alone
        # reported it as "fetchable, needs extraction" when there is nothing
        # there to extract. That is the exact class of confident-but-wrong claim
        # this script exists to prevent, so the verdict is now earned by parsing
        # the blob and finding a job array in it.
        jobs, path, blob_name = _embedded_jobs(body)
        if jobs:
            result.update(verdict="html_embedded_jobs", count=len(jobs),
                          array_path=f"{blob_name}:{path}",
                          fields=infer_fields(jobs[0]), sample=jobs[0])
        else:
            result["verdict"] = "html_only"
            result["detail"] = f"{len(body)} chars"
        return result

    jobs, path = find_job_array(payload)
    if not jobs:
        result["verdict"] = "json_no_jobs"
        result["detail"] = (", ".join(list(payload)[:8])
                            if isinstance(payload, dict) else type(payload).__name__)
        return result

    result.update(verdict="json_jobs", count=len(jobs), array_path=path,
                  fields=infer_fields(jobs[0]), sample=jobs[0])
    return result


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

_VERDICT_NOTE = {
    "json_jobs": "USABLE - write a fetcher against array_path + fields",
    "json_no_jobs": "JSON, but no job array found (may need auth or a param)",
    "html_embedded_jobs": "USABLE via extraction - jobs found in an embedded blob",
    "html_only": "server-rendered HTML - no JSON route from this URL",
    "http_error": "endpoint refused or does not exist for this tenant",
    "network": "did not resolve / connect",
}


def expand(pattern: Any, slug: str) -> List[Dict[str, Any]]:
    """One pattern -> the concrete request(s) to make.

    A pattern is either a URL string (GET) or a mapping with `url`/`method`/
    `data`. POST is not an optional extra: NeoGov's job list, and Workday's CXS
    API that this repo already fetches, are both POST-only, so a GET-only prober
    reports "no endpoint" for vendors that plainly have one.
    """
    spec = {"url": pattern} if isinstance(pattern, str) else dict(pattern)
    url = str(spec.get("url") or "")
    method = str(spec.get("method") or "GET").upper()
    data = spec.get("data")
    urls = ([url.format(slug=slug)] if "{n}" not in url
            else [url.format(slug=slug, n=n) for n in NUMERIC_RANGE])
    return [{"url": u, "method": method, "data": data} for u in urls]


def run(vendors: List[str], tenants: List[Tuple[str, str]], *,
        show_sample: bool) -> int:
    usable = 0
    for vendor in vendors:
        patterns = PATTERNS.get(vendor)
        if not patterns:
            print(f"!! unknown vendor {vendor}; known: {', '.join(sorted(PATTERNS))}",
                  file=sys.stderr)
            continue
        print(f"\n{'=' * 78}\n{vendor.upper()}  ({len(patterns)} candidate pattern(s))\n{'=' * 78}")
        for name, slug in tenants:
            label = f"{name or slug}"
            print(f"\n  tenant: {label}  (slug={slug})")
            for pattern in patterns:
                for spec in expand(pattern, slug):
                    url = spec["url"]
                    res = probe(url, method=spec["method"], data=spec.get("data"))
                    verdict = res.get("verdict", "?")
                    status = res.get("status")
                    head = (f"    {spec['method']:<4} [{str(status or '---'):>3}] "
                            f"{verdict:<24}")
                    if verdict in ("json_jobs", "html_embedded_jobs"):
                        usable += 1
                        print(f"{head} {res['count']:>4} jobs at '{res['array_path']}'")
                        print(f"          {url}")
                        fields = res["fields"]
                        for field in ("id", "title", "url", "posted_at", "location"):
                            got = fields.get(field)
                            mark = "ok  " if got else "MISS"
                            print(f"          {mark} {field:<10} -> {got or '(not present)'}")
                        if not fields.get("posted_at"):
                            print("          ^ no date key: ADR-001 forbids substituting now(),")
                            print("            so postings from here would be CONFIDENCE_UNKNOWN")
                        if show_sample:
                            print("          sample: " +
                                  json.dumps(res["sample"], default=str)[:600])
                    else:
                        detail = res.get("detail") or ""
                        print(f"{head} {detail[:40]}")
                        print(f"          {url}")

    print(f"\n{'=' * 78}")
    if usable:
        print(f"{usable} usable endpoint(s). Next: write `_fetch_<ats>()` against the")
        print("array_path + field mapping above, register it in "
              "career_pages_service.FETCHERS,")
        print("add the probe host to job_ingest_worker._PROBE_HOST_ALLOW and the API host")
        print("to _HOST_ALLOW, then add catalog rows with today's verification date.")
    else:
        print("No usable JSON endpoint found. That is a RESULT, not a failure: these")
        print("tenants are not fetchable through the patterns tried, so do not write a")
        print("fetcher for them. Widen NUMERIC_RANGE, add a pattern, or route these")
        print("employers through the aggregator engine instead (see mnc_catalog.py).")
    return 0 if usable else 1


def read_tenants(args: argparse.Namespace) -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []
    if args.tenants:
        for token in args.tenants.split(","):
            token = token.strip()
            if token:
                out.append(("", token))
    for path in args.file or []:
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "|" in line:
                        name, slug = (p.strip() for p in line.split("|", 1))
                    else:
                        name, slug = "", line
                    out.append((name, slug))
        except OSError as exc:
            print(f"!! could not read {path}: {exc}", file=sys.stderr)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Probe unsupported ATS vendors for a fetchable endpoint.")
    ap.add_argument("vendor", help="one of: " + ", ".join(sorted(PATTERNS)) + ", or 'all'")
    ap.add_argument("--tenants", help="comma-separated tenant slugs")
    ap.add_argument("--file", action="append",
                    help="file of 'Display Name | slug' lines (repeatable)")
    ap.add_argument("--sample", action="store_true",
                    help="print a sample record for each usable endpoint")
    args = ap.parse_args()

    tenants = read_tenants(args)
    if not tenants:
        ap.error("no tenants given - pass --tenants or --file")

    vendors = sorted(PATTERNS) if args.vendor == "all" else [args.vendor]
    return run(vendors, tenants, show_sample=args.sample)


if __name__ == "__main__":
    raise SystemExit(main())
