#!/usr/bin/env python3
"""
extract_doc_rows.py — pull every paste-ready `{...}` data row out of the two
expansion docs, dedup against the live catalogs, and re-probe each candidate
through the EXISTING discovery scripts' own classify() logic.

Companion to scripts/discover_workday_tenants.py + scripts/discover_career_pages.py.
Those turn *URLs* into validated rows; this turns the already-written **doc rows**
back into a live verdict (so a doc can be re-validated before its rows are pasted
into the catalog — endpoints drift ~1-2%/month).

Usage
-----
    PYTHONPATH=shared/python:. python scripts/extract_doc_rows.py [out.json]

Reads docs/workday-tenant-expansion.md and docs/career-pages-expansion.md, only
lines whose stripped form starts with `{` (never a `# ...` comment line). Writes a
verdict JSON bucketed into new / dup / non_us / dead for each catalog. Nothing is
written to a catalog — feed the `new` rows to a splice step by hand.
"""
from __future__ import annotations

import ast
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor

from services.workday_tenant_catalog import WORKDAY_TENANT_CATALOG
from services.career_pages_catalog import CAREER_PAGES_CATALOG
from services.career_pages_service import ENTERPRISE_CATALOG

import scripts.discover_workday_tenants as wd
import scripts.discover_career_pages as cp

DOCS = {
    "workday": "docs/workday-tenant-expansion.md",
    "career": "docs/career-pages-expansion.md",
}
_ROW_RE = re.compile(r"\{[^{}]*\}")


def extract_rows(path):
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if not line.strip().startswith("{"):
                continue
            m = _ROW_RE.search(line)
            if not m:
                continue
            try:
                d = ast.literal_eval(m.group(0))
            except (ValueError, SyntaxError):
                print(f"!! unparseable in {path}: {line.strip()[:80]}", file=sys.stderr)
                continue
            if isinstance(d, dict) and d.get("display_name"):
                rows.append(d)
    return rows


def dedup_within(rows, keyfn):
    seen, out = set(), []
    for r in rows:
        k = keyfn(r)
        if k not in seen:
            seen.add(k)
            out.append(r)
    return out


def bucket(res):
    return {s: [r for r in res if r["status"] == s]
            for s in ("new", "dup", "non_us", "dead")}


def main(out_path="verdict.json"):
    wd_rows = dedup_within(extract_rows(DOCS["workday"]),
                           lambda r: (r["tenant"].lower(), str(r.get("site", "")).lower()))
    cp_rows = dedup_within(extract_rows(DOCS["career"]),
                           lambda r: (r["ats"], str(r["slug"]).lower()))
    print(f"[workday] {len(wd_rows)} rows   [career] {len(cp_rows)} rows")

    def wd_work(r):
        v = wd.classify(r["display_name"], r["tenant"].lower(), r["wd_host"].lower(), r["site"])
        v["row"] = r
        return v

    def cp_work(r):
        v = cp.classify(r["display_name"], r["ats"], str(r["slug"]))
        v["row"] = r
        return v

    with ThreadPoolExecutor(max_workers=16) as pool:
        wd_res = list(pool.map(wd_work, wd_rows))
    with ThreadPoolExecutor(max_workers=12) as pool:
        cp_res = list(pool.map(cp_work, cp_rows))

    wd_b, cp_b = bucket(wd_res), bucket(cp_res)
    for name, b in (("workday", wd_b), ("career", cp_b)):
        print(f"[{name}] new={len(b['new'])} dup={len(b['dup'])} "
              f"non_us={len(b['non_us'])} dead={len(b['dead'])}")
        for r in b["dead"] + b["non_us"]:
            print(f"   {r['status'].upper():6} {r['row']['display_name']}: {r.get('reason','')}")

    payload = {
        "existing_counts": {"workday": len(WORKDAY_TENANT_CATALOG),
                            "career": len(CAREER_PAGES_CATALOG),
                            "enterprise": len(ENTERPRISE_CATALOG)},
        "workday": {k: [r["row"] for r in v] for k, v in wd_b.items()},
        "career": {k: [r["row"] for r in v] for k, v in cp_b.items()},
    }
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "verdict.json"))
