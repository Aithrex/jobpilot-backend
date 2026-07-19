#!/usr/bin/env python3
"""
Apply repaired careers URLs. Reads the agents' out_*.txt decisions, LIVE-validates
each proposed new URL (must actually return US jobs), then rewrites the matching
catalog row in place (keeps display_name + industry; only coordinates change).

Decision grammar per line:  oldkey || decision || industry
  oldkey  = 'career:<ats>/<slug>'  or  'workday:<tenant>/<site>'
  decision= SAME | NONE | <full new career/Workday URL>

Run: PYTHONPATH=shared/python:. python apply_repairs.py <repair_dir>
"""
from __future__ import annotations

import ast
import glob
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor

import scripts.discover_workday_tenants as wd
import scripts.discover_career_pages as cp

CAT = {
    "workday": "shared/python/services/workday_tenant_catalog.py",
    "career": "shared/python/services/career_pages_catalog.py",
}
_ROW_RE = re.compile(r"\{[^{}]*\}")


def parse_decisions(repair_dir):
    out = {}
    for path in glob.glob(os.path.join(repair_dir, "out_*.txt")):
        for raw in open(path, encoding="utf-8"):
            line = raw.strip()
            if not line or line.startswith("#") or "||" not in line:
                continue
            parts = [p.strip() for p in line.split("||")]
            if len(parts) < 2:
                continue
            oldkey, decision = parts[0], parts[1]
            out[oldkey] = decision
    return out


def oldkey_of_row(row, kind):
    if kind == "workday":
        return f"workday:{str(row['tenant']).lower()}/{row.get('site','')}"
    return f"career:{row['ats']}/{row['slug']}"


def validate_new(decision):
    """Return ('workday', tenant, host, site) or ('career', ats, slug) if the URL
    validates live with US jobs; else None."""
    w = wd.parse_url(decision)
    if w:
        tenant, host, site = w
        v = wd.validate(tenant, host, site)
        if v.get("ok") and v.get("total", 0) and v.get("us_frac", 0) >= 0.10:
            return ("workday", tenant, host, site)
        return None
    c = cp.parse_ats_url(decision)
    if c:
        ats, slug = c
        v = cp.validate("x", ats, str(slug))
        if v.get("ok") and v.get("us", 0) >= 1:
            return ("career", ats, str(slug))
    return None


def main(repair_dir):
    decisions = parse_decisions(repair_dir)
    print(f"parsed {len(decisions)} decisions")

    # keep only URL decisions (not SAME/NONE)
    url_dec = {k: v for k, v in decisions.items()
               if v.upper() not in ("SAME", "NONE") and "://" in v}
    same = [k for k, v in decisions.items() if v.upper() == "SAME"]
    none = [k for k, v in decisions.items() if v.upper() == "NONE"]
    print(f"  URL proposals={len(url_dec)}  SAME={len(same)}  NONE={len(none)}")

    # validate proposals live
    items = list(url_dec.items())
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(lambda kv: (kv[0], kv[1], validate_new(kv[1])), items))

    updates = {}   # oldkey -> new coords tuple
    failed = []
    for oldkey, url, coords in results:
        if coords is None:
            failed.append((oldkey, url))
        else:
            updates[oldkey] = coords
    print(f"  validated-new={len(updates)}  failed-validation={len(failed)}")
    for ok_, url in failed:
        print(f"     FAIL {ok_} -> {url}")

    # migrations to inject into the OTHER catalog after removal from this one
    migrate_into = {"workday": [], "career": []}   # target-kind -> list of new rows

    applied_total = 0
    for kind in ("workday", "career"):
        path = CAT[kind]
        lines = open(path, encoding="utf-8").read().splitlines()
        existing = set()
        for ln in lines:
            if ln.strip().startswith("{"):
                m = _ROW_RE.search(ln)
                if m:
                    try:
                        existing.add(_key(ast.literal_eval(m.group(0)), kind))
                    except Exception:
                        pass

        out_lines, applied, removed, migrated = [], 0, 0, 0
        for ln in lines:
            if ln.strip().startswith("{"):
                m = _ROW_RE.search(ln)
                if m:
                    try:
                        d = ast.literal_eval(m.group(0))
                    except Exception:
                        out_lines.append(ln); continue
                    ok_ = oldkey_of_row(d, kind)
                    if ok_ in updates:
                        coords = updates[ok_]
                        newkind = coords[0]
                        if newkind == kind:
                            newd = apply_coords(d, coords, kind)
                            newkey = _key(newd, kind)
                            if newkey == _key(d, kind):
                                out_lines.append(ln)
                            elif newkey in existing:
                                removed += 1
                            else:
                                indent = ln[:len(ln) - len(ln.lstrip())]
                                out_lines.append(indent + fmt(newd, kind))
                                existing.add(newkey)
                                applied += 1
                            continue
                        else:
                            # cross-catalog move: drop here, queue for other catalog
                            newd = apply_coords({"display_name": d["display_name"],
                                                 "industry": d["industry"]}, coords, newkind)
                            migrate_into[newkind].append(newd)
                            migrated += 1
                            continue
            out_lines.append(ln)
        open(path, "w", encoding="utf-8", newline="\n").write("\n".join(out_lines) + "\n")
        print(f"[{kind}] updated {applied}, dropped-dup {removed}, migrated-out {migrated} -> {path}")
        applied_total += applied

    # inject migrations before each catalog's closing ]
    for kind, rows in migrate_into.items():
        if not rows:
            continue
        path = CAT[kind]
        lines = open(path, encoding="utf-8").read().splitlines()
        existing = set()
        for ln in lines:
            if ln.strip().startswith("{"):
                m = _ROW_RE.search(ln)
                if m:
                    try:
                        existing.add(_key(ast.literal_eval(m.group(0)), kind))
                    except Exception:
                        pass
        close_idx = next(i for i in range(len(lines) - 1, -1, -1) if lines[i].strip() == "]")
        block = ["    # repaired-migration 2026-07-19 (moved ATS, live-validated)"]
        injected = 0
        for r in rows:
            if _key(r, kind) in existing:
                continue
            block.append("    " + fmt(r, kind))
            existing.add(_key(r, kind))
            injected += 1
        lines = lines[:close_idx] + block + lines[close_idx:]
        open(path, "w", encoding="utf-8", newline="\n").write("\n".join(lines) + "\n")
        print(f"[{kind}] injected {injected} migrated rows")
        applied_total += injected

    print(f"\nTOTAL rows repaired/migrated: {applied_total}")
    return 0


def _key(d, kind):
    if kind == "workday":
        return (str(d["tenant"]).lower(), str(d.get("site", "")).lower())
    return (d["ats"], str(d["slug"]).lower())


def apply_coords(d, coords, kind):
    nd = dict(d)
    if kind == "workday":
        _, tenant, host, site = coords
        nd["tenant"], nd["wd_host"], nd["site"] = tenant, host, site
    else:
        _, ats, slug = coords
        nd["ats"], nd["slug"] = ats, slug
    return nd


def fmt(d, kind):
    order = (["display_name", "tenant", "wd_host", "site", "industry"] if kind == "workday"
             else ["display_name", "ats", "slug", "industry"])
    import json
    parts = [f'"{k}": {json.dumps(str(d[k]))}' for k in order]
    return "{" + ", ".join(parts) + "},"


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
