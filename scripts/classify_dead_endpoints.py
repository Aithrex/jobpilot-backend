#!/usr/bin/env python3
"""
Re-classify the health-check failures by ENDPOINT LIVENESS, so a board that is
merely empty today (weekend / between reqs) is kept, and only genuinely broken
URLs (404 / HTTP error / slug removed) are flagged for removal.

Buckets per entry:
  BROKEN       -> endpoint errors / 404 / slug gone           => REMOVE
  ALIVE_EMPTY  -> board resolves 200 but 0 jobs right now     => KEEP (re-check midweek)
  ALIVE_NONUS  -> board has jobs globally, 0 US right now (SR) => KEEP
  RECOVERED    -> now returns jobs on re-probe                 => KEEP

Writes broken-only lists in health.json shape (for remove_dead.py) + a full report.

Run: PYTHONPATH=shared/python:. python classify_dead.py <health.json> <out_broken.json>
"""
from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor

import requests

from services.workday_cxs_fetcher import _HEADERS

UA = {"User-Agent": "Mozilla/5.0 (health-check)"}
TIMEOUT = 15


def probe_career(row):
    ats, slug = row["ats"], str(row["slug"])
    try:
        if ats == "greenhouse":
            meta = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}",
                                headers=UA, timeout=TIMEOUT)
            if meta.status_code == 404:
                return "BROKEN", "board_404"
            jobs = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
                                headers=UA, timeout=TIMEOUT)
            if jobs.status_code >= 400:
                return "BROKEN", f"jobs_{jobs.status_code}"
            n = len((jobs.json() or {}).get("jobs") or [])
            return ("RECOVERED", n) if n else ("ALIVE_EMPTY", 0)
        if ats == "lever":
            r = requests.get(f"https://api.lever.co/v0/postings/{slug}?mode=json",
                             headers=UA, timeout=TIMEOUT)
            if r.status_code == 404:
                return "BROKEN", "acct_404"
            if r.status_code >= 400:
                return "BROKEN", f"http_{r.status_code}"
            data = r.json()
            n = len(data) if isinstance(data, list) else 0
            return ("RECOVERED", n) if n else ("ALIVE_EMPTY", 0)
        if ats == "ashby":
            r = requests.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}",
                             headers=UA, timeout=TIMEOUT)
            if r.status_code == 404:
                return "BROKEN", "board_404"
            if r.status_code >= 400:
                return "BROKEN", f"http_{r.status_code}"
            n = len((r.json() or {}).get("jobs") or [])
            return ("RECOVERED", n) if n else ("ALIVE_EMPTY", 0)
        if ats == "smartrecruiters":
            g = requests.get(
                f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=10",
                headers=UA, timeout=TIMEOUT)
            if g.status_code == 404:
                return "BROKEN", "company_404"
            if g.status_code >= 400:
                return "BROKEN", f"http_{g.status_code}"
            gtot = (g.json() or {}).get("totalFound", 0)
            u = requests.get(
                f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=10&country=us",
                headers=UA, timeout=TIMEOUT)
            utot = (u.json() or {}).get("totalFound", 0) if u.status_code < 400 else 0
            if utot:
                return "RECOVERED", utot
            if gtot:
                return "ALIVE_NONUS", gtot
            return "ALIVE_EMPTY", 0
        if ats == "workable":
            r = requests.get(
                f"https://apply.workable.com/api/v1/widget/accounts/{slug}",
                headers=UA, timeout=TIMEOUT)
            if r.status_code == 404:
                return "BROKEN", "acct_404"
            if r.status_code >= 400:
                return "BROKEN", f"http_{r.status_code}"
            n = len((r.json() or {}).get("jobs") or [])
            return ("RECOVERED", n) if n else ("ALIVE_EMPTY", 0)
        if ats == "recruitee":
            r = requests.get(f"https://{slug}.recruitee.com/api/offers/",
                             headers=UA, timeout=TIMEOUT)
            if r.status_code >= 400:
                return "BROKEN", f"http_{r.status_code}"
            n = len((r.json() or {}).get("offers") or [])
            return ("RECOVERED", n) if n else ("ALIVE_EMPTY", 0)
    except requests.RequestException as e:
        return "BROKEN", f"net_{type(e).__name__}"
    return "ALIVE_EMPTY", 0


def probe_workday(row):
    tenant, host, site = row["tenant"].lower(), row["wd_host"].lower(), row["site"]
    url = f"https://{tenant}.{host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
    body = {"searchText": "", "locations": [], "appliedFacets": {}, "limit": 20, "offset": 0}
    try:
        r = requests.post(url, json=body, headers=_HEADERS, timeout=TIMEOUT)
    except requests.RequestException as e:
        return "BROKEN", f"net_{type(e).__name__}"
    if r.status_code == 404:
        return "BROKEN", "cxs_404"
    if r.status_code >= 400:
        return "BROKEN", f"http_{r.status_code}"
    try:
        total = r.json().get("total", 0)
    except ValueError:
        return "BROKEN", "bad_json"
    return ("RECOVERED", total) if total else ("ALIVE_EMPTY", 0)


def main(health_path, out_path):
    health = json.load(open(health_path, encoding="utf-8"))

    def run(rows, fn, workers):
        with ThreadPoolExecutor(max_workers=workers) as pool:
            verdicts = list(pool.map(fn, rows))
        return [{"row": r, "bucket": v[0], "detail": v[1]} for r, v in zip(rows, verdicts)]

    wd = run(health["workday_dead"], probe_workday, 8)
    cp = run(health["career_dead"], probe_career, 10)

    def report(items, label, namekey):
        from collections import Counter
        print(f"\n=== {label} ({len(items)}) ===")
        print("  ", dict(Counter(i["bucket"] for i in items)))
        for b in ("BROKEN", "ALIVE_NONUS", "ALIVE_EMPTY", "RECOVERED"):
            sub = [i for i in items if i["bucket"] == b]
            if not sub:
                continue
            print(f"  -- {b} ({len(sub)}):")
            for i in sub:
                r = i["row"]
                who = f"{r['display_name']} ({r.get(namekey)}/{r.get('slug', r.get('site'))})"
                print(f"       {who} :: {i['detail']}")

    report(wd, "WORKDAY", "tenant")
    report(cp, "CAREER", "ats")

    broken = {
        "workday_dead": [i["row"] for i in wd if i["bucket"] == "BROKEN"],
        "career_dead": [i["row"] for i in cp if i["bucket"] == "BROKEN"],
    }
    keep = {
        "workday": [{**i["row"], "_bucket": i["bucket"], "_detail": i["detail"]}
                    for i in wd if i["bucket"] != "BROKEN"],
        "career": [{**i["row"], "_bucket": i["bucket"], "_detail": i["detail"]}
                   for i in cp if i["bucket"] != "BROKEN"],
    }
    json.dump(broken, open(out_path, "w", encoding="utf-8"), indent=2)
    json.dump(keep, open(out_path.replace(".json", "_keep.json"), "w", encoding="utf-8"), indent=2)
    print(f"\nBROKEN -> remove: workday={len(broken['workday_dead'])} "
          f"career={len(broken['career_dead'])}  (wrote {out_path})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1], sys.argv[2]))
