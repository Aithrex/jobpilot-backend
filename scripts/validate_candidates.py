#!/usr/bin/env python3
"""
validate_candidates.py — turn a mixed list of candidate career URLs into
validated, paste-ready catalog rows in one pass.

Where discover_workday_tenants.py handles ONLY Workday URLs and
discover_career_pages.py handles ONLY board-ATS URLs, this accepts BOTH in one
file, auto-routes each URL to the right validator, and carries an industry tag
through so the output rows need no hand-editing.

Input lines (blank / '#' ignored):
    Display Name | https://<career-or-workday-url> | industry_key
`industry_key` is optional; unknown/missing -> "other".

Usage
-----
    PYTHONPATH=shared/python:. python scripts/validate_candidates.py candidates.txt out.json

Writes a verdict JSON ({"workday":{"new":[...]},"career":{"new":[...]}}) holding
only rows that probed live, look US, and are not already in the catalog.
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import scripts.discover_workday_tenants as wd
import scripts.discover_career_pages as cp

VALID_INDUSTRIES = {
    "tech", "fintech", "healthtech", "banking", "insurance", "pharma", "energy",
    "telecom", "consulting", "ecommerce_retail", "manufacturing_automotive",
    "aerospace_defense", "media", "gaming", "other", "education", "government",
    "nonprofit", "hospitality", "travel", "real_estate",
}


def parse_line(line):
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    parts = [p.strip() for p in line.split("|")]
    if len(parts) < 2:
        return None
    industry = parts[2] if len(parts) >= 3 and parts[2] in VALID_INDUSTRIES else "other"
    return parts[0], parts[1], industry


def main(in_path, out_path):
    seen_wd, seen_cp = set(), set()
    wd_cands, cp_cands = [], []
    with open(in_path, encoding="utf-8") as fh:
        for raw in fh:
            parsed = parse_line(raw)
            if not parsed:
                continue
            name, url, industry = parsed
            w = wd.parse_url(url)
            if w:
                tenant, host, site = w
                if (tenant, site.lower()) not in seen_wd:
                    seen_wd.add((tenant, site.lower()))
                    wd_cands.append((name, tenant, host, site, industry))
                continue
            c = cp.parse_ats_url(url)
            if c:
                ats, slug = c
                if (ats, slug.lower()) not in seen_cp:
                    seen_cp.add((ats, slug.lower()))
                    cp_cands.append((name, ats, slug, industry))
            else:
                print(f"!! unrecognized URL, skipped: {url}", file=sys.stderr)

    print(f"parsed {len(wd_cands)} workday + {len(cp_cands)} career candidates")

    def wd_work(c):
        name, tenant, host, site, industry = c
        v = wd.classify(name, tenant.lower(), host.lower(), site)
        v["industry"] = industry
        return v

    def cp_work(c):
        name, ats, slug, industry = c
        v = cp.classify(name, ats, str(slug))
        v["industry"] = industry
        return v

    with ThreadPoolExecutor(max_workers=16) as pool:
        wd_res = list(pool.map(wd_work, wd_cands)) if wd_cands else []
    with ThreadPoolExecutor(max_workers=12) as pool:
        cp_res = list(pool.map(cp_work, cp_cands)) if cp_cands else []

    def collect(res, kind):
        out = []
        for r in res:
            if r["status"] != "new":
                continue
            if kind == "workday":
                out.append({"display_name": r["name"], "tenant": r["tenant"],
                            "wd_host": r["host"], "site": r["site"], "industry": r["industry"]})
            else:
                out.append({"display_name": r["name"], "ats": r["ats"],
                            "slug": r["slug"], "industry": r["industry"]})
        return out

    for res, label in ((wd_res, "workday"), (cp_res, "career")):
        print(f"[{label}] {dict(Counter(r['status'] for r in res))}")
        for r in res:
            if r["status"] in ("dead", "non_us"):
                print(f"   {r['status'].upper():6} {r.get('name')}: {r.get('reason','')}")

    payload = {"workday": {"new": collect(wd_res, "workday")},
               "career": {"new": collect(cp_res, "career")}}
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    print(f"NEW workday: {len(payload['workday']['new'])}  "
          f"NEW career: {len(payload['career']['new'])}  -> {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1], sys.argv[2]))
