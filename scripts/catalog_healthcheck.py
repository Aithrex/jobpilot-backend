#!/usr/bin/env python3
"""
Full-catalog health check: probe EVERY company in both catalogs live and confirm
it returns job postings. Failures are retried (transient rate-limits / timeouts
must not be mistaken for dead) before being reported.

Run:  PYTHONPATH=shared/python:. python health_check.py <out.json>
"""
from __future__ import annotations

import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from services.workday_tenant_catalog import WORKDAY_TENANT_CATALOG
from services.career_pages_catalog import CAREER_PAGES_CATALOG

import scripts.discover_workday_tenants as wd
import scripts.discover_career_pages as cp


def check_wd(row):
    v = wd.validate(row["tenant"].lower(), row["wd_host"].lower(), row["site"])
    return {"ok": bool(v.get("ok")), "reason": v.get("reason", ""),
            "total": v.get("total", 0), "row": row}


def check_cp(row):
    v = cp.validate(row["display_name"], row["ats"], str(row["slug"]))
    return {"ok": bool(v.get("ok")), "reason": v.get("reason", ""),
            "total": v.get("total", 0), "row": row}


def sweep(rows, fn, workers, label):
    with ThreadPoolExecutor(max_workers=workers) as pool:
        res = list(pool.map(fn, rows))
    ok = [r for r in res if r["ok"]]
    bad = [r for r in res if not r["ok"]]
    print(f"[{label}] pass1: {len(ok)} ok / {len(bad)} failing (of {len(rows)})")
    # retry failures up to 2 more times, lower concurrency, small backoff
    for attempt in (2, 3):
        if not bad:
            break
        time.sleep(3)
        with ThreadPoolExecutor(max_workers=max(4, workers // 3)) as pool:
            retry = list(pool.map(fn, [b["row"] for b in bad]))
        still, revived = [], 0
        for r in retry:
            if r["ok"]:
                revived += 1
            else:
                still.append(r)
        print(f"[{label}] retry{attempt}: revived {revived}, still failing {len(still)}")
        bad = still
    return bad


def main(out_path="health.json"):
    wd_dead = sweep(list(WORKDAY_TENANT_CATALOG), check_wd, 14, "workday")
    cp_dead = sweep(list(CAREER_PAGES_CATALOG), check_cp, 10, "career")

    def dump(dead, keyfields):
        out = []
        for d in dead:
            r = d["row"]
            out.append({**{k: r.get(k) for k in keyfields},
                        "reason": d["reason"]})
        return out

    payload = {
        "workday_total": len(WORKDAY_TENANT_CATALOG),
        "career_total": len(CAREER_PAGES_CATALOG),
        "workday_dead": dump(wd_dead, ["display_name", "tenant", "wd_host", "site", "industry"]),
        "career_dead": dump(cp_dead, ["display_name", "ats", "slug", "industry"]),
    }
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)

    print("\n" + "=" * 64)
    print(f"WORKDAY dead: {len(wd_dead)} / {len(WORKDAY_TENANT_CATALOG)}")
    for d in wd_dead:
        print(f"   {d['row']['display_name']} ({d['row']['tenant']}/{d['row']['site']}) -> {d['reason']}")
    print(f"CAREER dead: {len(cp_dead)} / {len(CAREER_PAGES_CATALOG)}")
    for d in cp_dead:
        print(f"   {d['row']['display_name']} ({d['row']['ats']}/{d['row']['slug']}) -> {d['reason']}")
    print(f"\nwrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "health.json"))
