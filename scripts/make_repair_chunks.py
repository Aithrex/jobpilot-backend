#!/usr/bin/env python3
"""Emit repair-target chunk files (ALIVE_EMPTY boards) for the agent sweep.
Each line: oldkey | display_name | current_url | industry
oldkey = 'career:<ats>/<slug>'  or  'workday:<tenant>/<site>'
"""
import json, os, sys

keep = json.load(open(sys.argv[1], encoding="utf-8"))
outdir = sys.argv[2]
os.makedirs(outdir, exist_ok=True)

def cur_url_career(r):
    a, s = r["ats"], r["slug"]
    return {
        "greenhouse": f"https://boards.greenhouse.io/{s}",
        "lever": f"https://jobs.lever.co/{s}",
        "ashby": f"https://jobs.ashbyhq.com/{s}",
        "smartrecruiters": f"https://careers.smartrecruiters.com/{s}",
        "workable": f"https://apply.workable.com/{s}",
        "recruitee": f"https://{s}.recruitee.com",
    }[a]

def line_career(r):
    return f"career:{r['ats']}/{r['slug']} | {r['display_name']} | {cur_url_career(r)} | {r['industry']}"

def line_workday(r):
    url = f"https://{r['tenant']}.{r['wd_host']}.myworkdayjobs.com/{r['site']}"
    return f"workday:{r['tenant']}/{r['site']} | {r['display_name']} | {url} | {r['industry']}"

wd = [r for r in keep["workday"] if r.get("_bucket") == "ALIVE_EMPTY"]
cp = [r for r in keep["career"] if r.get("_bucket") == "ALIVE_EMPTY"]

chunks = {}
chunks["A_workday"] = [line_workday(r) for r in wd]
for ats, tag in [("greenhouse", "B_greenhouse"), ("lever", "C_lever"),
                 ("ashby", "D_ashby"), ("smartrecruiters", "E_smartrecruiters"),
                 ("recruitee", "G_workable_recruitee")]:
    chunks.setdefault(tag, [])
wk = [r for r in cp if r["ats"] == "workable"]
half = (len(wk) + 1) // 2
chunks["B_greenhouse"] = [line_career(r) for r in cp if r["ats"] == "greenhouse"]
chunks["C_lever"] = [line_career(r) for r in cp if r["ats"] == "lever"]
chunks["D_ashby"] = [line_career(r) for r in cp if r["ats"] == "ashby"]
chunks["E_smartrecruiters"] = [line_career(r) for r in cp if r["ats"] == "smartrecruiters"]
chunks["F_workable1"] = [line_career(r) for r in wk[:half]]
chunks["G_workable2_recruitee"] = [line_career(r) for r in wk[half:]] + \
                                  [line_career(r) for r in cp if r["ats"] == "recruitee"]

for tag, lines in chunks.items():
    if not lines:
        continue
    p = os.path.join(outdir, f"chunk_{tag}.txt")
    open(p, "w", encoding="utf-8", newline="\n").write("\n".join(lines) + "\n")
    print(f"{p}: {len(lines)} targets")
