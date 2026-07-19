#!/usr/bin/env python3
"""
discover_career_pages.py — validate candidate career-page ATS URLs and emit
paste-ready CAREER_PAGES_CATALOG rows for **US companies not already in the
catalog**.

Companion to scripts/discover_workday_tenants.py. Same idea, different catalog:
here we cover companies that host jobs on a supported board ATS (Greenhouse /
Lever / Ashby / SmartRecruiters / Workable / Recruitee) rather than Workday.

Usage
-----
    # one candidate per line. Two accepted forms:
    #   https://careers.smartrecruiters.com/AECOM2
    #   AECOM | https://careers.smartrecruiters.com/AECOM2
    PYTHONPATH=shared/python python scripts/discover_career_pages.py candidates.txt

    # or pass URLs straight on the command line:
    PYTHONPATH=shared/python python scripts/discover_career_pages.py \
        https://jobs.ashbyhq.com/rivianvw.tech \
        https://boards.greenhouse.io/somecompany

For each candidate it:
  1. detects the ATS + slug from the URL
  2. calls the real career_pages_service fetcher  -> must return postings
  3. counts US postings (reuses career_pages_service._keep_us)
  4. skips anything whose (ats, slug) or company name is already in the catalog
  5. prints ready-to-paste rows, plus a rejection report

Nothing here writes to the catalog — it only prints. Review, set the `industry`,
and paste the rows into services/career_pages_catalog.py by hand.
"""
from __future__ import annotations

import re
import sys
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional, Tuple

from services.career_pages_catalog import CAREER_PAGES_CATALOG
from services.career_pages_service import FETCHERS, ENTERPRISE_CATALOG, _keep_us

# ---- ATS URL detection ----------------------------------------------------
# Order matters: specific hosts before broad "{slug}.<ats>.com" fallbacks.
_ATS_PATTERNS: List[Tuple[str, re.Pattern]] = [
    ("greenhouse",      re.compile(r"(?:boards|job-boards)\.greenhouse\.io/([^/?#]+)", re.I)),
    ("lever",           re.compile(r"jobs\.(?:eu\.)?lever\.co/([^/?#]+)", re.I)),
    ("ashby",           re.compile(r"jobs\.ashbyhq\.com/([^/?#]+)", re.I)),
    ("smartrecruiters", re.compile(r"(?:careers|jobs)\.smartrecruiters\.com/([^/?#]+)", re.I)),
    ("workable",        re.compile(r"apply\.workable\.com/([^/?#]+)", re.I)),
    ("workable",        re.compile(r"([a-z0-9-]+)\.workable\.com", re.I)),
    ("recruitee",       re.compile(r"([a-z0-9-]+)\.recruitee\.com", re.I)),
    ("greenhouse",      re.compile(r"([a-z0-9-]+)\.greenhouse\.io", re.I)),
]


def parse_ats_url(url: str) -> Optional[Tuple[str, str]]:
    """(ats, slug) from a supported ATS URL, or None."""
    u = url.strip()
    for ats, rx in _ATS_PATTERNS:
        m = rx.search(u)
        if m:
            slug = m.group(1)
            # ignore obvious non-slug segments
            if slug.lower() in {"www", "en", "en-us", "job", "jobs", "postings"}:
                continue
            return ats, slug
    return None


# ---- dedup index against the existing catalog(s) --------------------------
def _norm_name(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(s).lower()).strip()


_ALL_ROWS = list(CAREER_PAGES_CATALOG) + list(ENTERPRISE_CATALOG)
_HAVE_SLUGS = {(r.get("ats", ""), str(r.get("slug", "")).lower()) for r in _ALL_ROWS}
_HAVE_NAMES = {_norm_name(r.get("display_name", "")) for r in _ALL_ROWS}


# ---- live validation ------------------------------------------------------
def validate(name: str, ats: str, slug: str) -> Dict:
    fetcher = FETCHERS.get(ats)
    if fetcher is None:
        return {"ok": False, "reason": f"unsupported_ats:{ats}"}
    company = {"display_name": name, "ats": ats, "slug": slug, "industry": "TODO"}
    try:
        jobs = fetcher(company)
    except Exception as e:
        return {"ok": False, "reason": f"fetch_error:{type(e).__name__}"}
    if not jobs:
        return {"ok": False, "reason": "no_postings"}
    us = sum(1 for j in jobs if _keep_us(j))
    return {"ok": True, "total": len(jobs), "us": us,
            "is_us": us >= max(1, int(0.15 * len(jobs)))}


def classify(name: str, ats: str, slug: str) -> Dict:
    if (ats, slug.lower()) in _HAVE_SLUGS or _norm_name(name) in _HAVE_NAMES:
        return {"status": "dup", "name": name, "ats": ats, "slug": slug}
    v = validate(name, ats, slug)
    if not v["ok"]:
        return {"status": "dead", "name": name, "ats": ats, "slug": slug,
                "reason": v["reason"]}
    if not v["is_us"]:
        return {"status": "non_us", "name": name, "ats": ats, "slug": slug,
                "total": v["total"], "us": v["us"]}
    return {"status": "new", "name": name, "ats": ats, "slug": slug,
            "total": v["total"], "us": v["us"]}


# ---- input parsing --------------------------------------------------------
def read_candidates(args: List[str]) -> List[Tuple[str, str, str]]:
    """Return list of (display_name, ats, slug)."""
    raw: List[str] = []
    for a in args:
        if a.lower().startswith("http"):
            raw.append(a)
        else:
            try:
                with open(a, encoding="utf-8") as fh:
                    raw.extend(fh.read().splitlines())
            except OSError as e:
                print(f"!! could not read {a}: {e}", file=sys.stderr)
    out: List[Tuple[str, str, str]] = []
    for line in raw:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, url = (p.strip() for p in line.split("|", 1)) if "|" in line else ("", line)
        parsed = parse_ats_url(url)
        if not parsed:
            print(f"!! not a supported ATS URL, skipped: {line}", file=sys.stderr)
            continue
        ats, slug = parsed
        out.append((name or slug, ats, slug))
    return out


def main(argv: List[str]) -> int:
    if not argv:
        print(__doc__)
        return 1
    cands = read_candidates(argv)
    if not cands:
        print("No valid candidate ATS URLs found.")
        return 1

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(lambda c: classify(*c), cands))

    new = [r for r in results if r["status"] == "new"]
    dup = [r for r in results if r["status"] == "dup"]
    nonus = [r for r in results if r["status"] == "non_us"]
    dead = [r for r in results if r["status"] == "dead"]

    print(f"\n{'='*70}\nCANDIDATES: {len(cands)}  |  NEW(US): {len(new)}  "
          f"already-in-catalog: {len(dup)}  non-US: {len(nonus)}  dead: {len(dead)}\n{'='*70}")

    if new:
        print("\n### NEW US career-pages companies — paste into CAREER_PAGES_CATALOG "
              "(set the industry!):\n")
        for r in sorted(new, key=lambda x: x["name"].lower()):
            print(f'    {{"display_name": "{r["name"]}", "ats": "{r["ats"]}", '
                  f'"slug": "{r["slug"]}", "industry": "TODO"}},'
                  f'   # {r["total"]} jobs, {r["us"]} US')

    if nonus:
        print("\n### Reachable but looks NON-US (skipped per US-only rule):")
        for r in sorted(nonus, key=lambda x: x["name"].lower()):
            print(f"  - {r['name']} ({r['ats']}/{r['slug']}) {r['us']}/{r['total']} US")

    if dead:
        print("\n### Did not validate:")
        for r in sorted(dead, key=lambda x: x["name"].lower()):
            print(f"  - {r['name']} ({r['ats']}/{r['slug']}) -> {r['reason']}")

    if dup:
        print(f"\n### Already in the catalog ({len(dup)}): "
              + ", ".join(sorted(r["name"] for r in dup)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
