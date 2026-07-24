#!/usr/bin/env python3
"""
discover_workday_tenants.py — validate candidate Workday career URLs and emit
paste-ready WORKDAY_TENANT_CATALOG rows for **US companies not already in the
catalog**.

This is the "check the coordinates and add them" half of growing the Workday
tab. The *discovery* half (finding candidate companies) is done by web-searching
"<company> myworkdayjobs" and collecting the resulting myworkdayjobs.com URLs —
feed those URLs (or the tenant/host/site directly) into this script.

Usage
-----
    # one candidate per line in a file. Two accepted forms:
    #   https://guidewire.wd5.myworkdayjobs.com/external
    #   Guidewire | https://guidewire.wd5.myworkdayjobs.com/external
    PYTHONPATH=shared/python python scripts/discover_workday_tenants.py candidates.txt

    # or pass URLs straight on the command line:
    PYTHONPATH=shared/python python scripts/discover_workday_tenants.py \
        https://guidewire.wd5.myworkdayjobs.com/external \
        https://leidos.wd5.myworkdayjobs.com/External

For each candidate it:
  1. parses (tenant, wd_host, site) out of the myworkdayjobs.com URL
  2. probes the public CXS `/jobs` endpoint  -> must return HTTP 200 with postings
  3. samples postings to confirm a real US presence (reuses the tab's US filter)
  4. skips anything whose tenant slug / company name is already in the 532
  5. prints ready-to-paste WORKDAY_TENANT_CATALOG rows, plus a rejection report

Nothing here writes to the catalog — it only prints. Review, set the `industry`,
and paste the rows into services/workday_tenant_catalog.py by hand.
"""
from __future__ import annotations

import re
import sys
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional, Tuple

import requests

from services.workday_tenant_catalog import WORKDAY_TENANT_CATALOG
from services.workday_cxs_fetcher import _HEADERS
from services.workday_jobs_service import _geo_keep_us

# ---- URL parsing ----------------------------------------------------------
# https://{tenant}.{wdNN}.myworkdayjobs.com/[xx-XX/]{site}[/...]
_URL_RE = re.compile(
    r"https?://([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/"
    r"(?:[a-z]{2}-[A-Z]{2}/)?([^/?#]+)",
    re.IGNORECASE,
)


def parse_url(url: str) -> Optional[Tuple[str, str, str]]:
    """(tenant, wd_host, site) from a myworkdayjobs.com URL, or None."""
    m = _URL_RE.search(url.strip())
    if not m:
        return None
    return m.group(1).lower(), m.group(2).lower(), m.group(3)


# ---- dedup index against the existing 532 ---------------------------------
def _norm_name(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(s).lower()).strip()


_HAVE_TENANTS = {r["tenant"].lower() for r in WORKDAY_TENANT_CATALOG}
_HAVE_NAMES = {_norm_name(r["display_name"]) for r in WORKDAY_TENANT_CATALOG}


# ---- live validation ------------------------------------------------------
def validate(tenant: str, host: str, site: str) -> Dict:
    """Probe the CXS endpoint. Returns a verdict dict."""
    url = f"https://{tenant}.{host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
    body = {"searchText": "", "locations": [], "appliedFacets": {}, "limit": 20, "offset": 0}
    try:
        r = requests.post(url, json=body, headers=_HEADERS, timeout=10)
    except requests.RequestException as e:
        return {"ok": False, "reason": f"network:{type(e).__name__}"}
    if r.status_code != 200:
        return {"ok": False, "reason": f"http_{r.status_code}"}
    try:
        data = r.json()
    except ValueError:
        return {"ok": False, "reason": "bad_json"}
    postings = data.get("jobPostings") or []
    total = data.get("total", 0) or len(postings)
    if not total:
        return {"ok": False, "reason": "no_postings"}
    # US presence: fraction of the sampled page that looks US-based.
    us_hits = sum(1 for p in postings if isinstance(p, dict)
                  and _geo_keep_us((p.get("locationsText") or "")))
    us_frac = (us_hits / len(postings)) if postings else 0.0
    return {"ok": True, "total": total, "us_frac": round(us_frac, 2),
            "us": us_frac >= 0.15}


def classify(name: str, tenant: str, host: str, site: str) -> Dict:
    if tenant in _HAVE_TENANTS or _norm_name(name) in _HAVE_NAMES:
        return {"status": "dup", "name": name, "tenant": tenant}
    v = validate(tenant, host, site)
    if not v["ok"]:
        return {"status": "dead", "name": name, "tenant": tenant,
                "host": host, "site": site, "reason": v["reason"]}
    if not v["us"]:
        return {"status": "non_us", "name": name, "tenant": tenant,
                "host": host, "site": site, "us_frac": v["us_frac"], "total": v["total"]}
    return {"status": "new", "name": name, "tenant": tenant, "host": host,
            "site": site, "total": v["total"], "us_frac": v["us_frac"]}


# ---- input parsing --------------------------------------------------------
def read_candidates(args: List[str]) -> List[Tuple[str, str]]:
    """Return list of (display_name, url). Accepts files or bare URLs."""
    raw: List[str] = []
    for a in args:
        if a.lower().startswith("http"):
            raw.append(a)
        else:
            try:
                with open(a, encoding="utf-8") as fh:
                    raw.extend(line for line in fh.read().splitlines())
            except OSError as e:
                print(f"!! could not read {a}: {e}", file=sys.stderr)
    out: List[Tuple[str, str]] = []
    for line in raw:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "|" in line:
            name, url = (p.strip() for p in line.split("|", 1))
        else:
            url = line
            name = ""
        parsed = parse_url(url)
        if not parsed:
            print(f"!! not a myworkdayjobs URL, skipped: {line}", file=sys.stderr)
            continue
        tenant, _, _ = parsed
        out.append((name or tenant.capitalize(), url))
    return out


def main(argv: List[str]) -> int:
    if not argv:
        print(__doc__)
        return 1
    cands = read_candidates(argv)
    if not cands:
        print("No valid candidate URLs found.")
        return 1

    def work(item: Tuple[str, str]) -> Dict:
        name, url = item
        tenant, host, site = parse_url(url)  # already validated in read_candidates
        return classify(name, tenant, host, site)

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(work, cands))

    new = [r for r in results if r["status"] == "new"]
    dup = [r for r in results if r["status"] == "dup"]
    nonus = [r for r in results if r["status"] == "non_us"]
    dead = [r for r in results if r["status"] == "dead"]

    print(f"\n{'='*70}\nCANDIDATES: {len(cands)}  |  NEW(US): {len(new)}  "
          f"already-in-532: {len(dup)}  non-US: {len(nonus)}  dead: {len(dead)}\n{'='*70}")

    if new:
        print("\n### NEW US Workday companies — paste into WORKDAY_TENANT_CATALOG "
              "(set the industry!):\n")
        for r in sorted(new, key=lambda x: x["name"].lower()):
            print(f'    {{"display_name": "{r["name"]}", "tenant": "{r["tenant"]}", '
                  f'"wd_host": "{r["host"]}", "site": "{r["site"]}", "industry": "TODO"}},'
                  f'   # ~{r["total"]} jobs, US {int(r["us_frac"]*100)}%')

    if nonus:
        print("\n### Reachable but looks NON-US (skipped per US-only rule):")
        for r in sorted(nonus, key=lambda x: x["name"].lower()):
            print(f"  - {r['name']} ({r['tenant']}.{r['host']}/{r['site']}) "
                  f"US {int(r['us_frac']*100)}%")

    if dead:
        print("\n### Did not validate (path changed / gated / retired):")
        for r in sorted(dead, key=lambda x: x["name"].lower()):
            print(f"  - {r['name']} ({r['tenant']}.{r['host']}/{r['site']}) -> {r['reason']}")

    if dup:
        print(f"\n### Already in the 532 ({len(dup)}): "
              + ", ".join(sorted(r["name"] for r in dup)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
