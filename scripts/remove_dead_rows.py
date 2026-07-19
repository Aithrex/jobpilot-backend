#!/usr/bin/env python3
"""
Remove dead rows (from health.json) out of a catalog file, line by line, so
section comments and formatting of the surviving rows are untouched.

Run:  python remove_dead.py <health.json> <workday|career>
"""
from __future__ import annotations

import ast
import json
import re
import sys

CATALOGS = {
    "workday": "shared/python/services/workday_tenant_catalog.py",
    "career": "shared/python/services/career_pages_catalog.py",
}
_ROW_RE = re.compile(r"\{[^{}]*\}")


def key_of(d, kind):
    if kind == "workday":
        return (str(d["tenant"]).lower(), str(d.get("site", "")).lower())
    return (str(d["ats"]), str(d["slug"]).lower())


def main(health_path, kind):
    health = json.load(open(health_path, encoding="utf-8"))
    dead = health[f"{kind}_dead"]
    dead_keys = {key_of(d, kind) for d in dead}
    print(f"[{kind}] removing {len(dead_keys)} dead keys")

    path = CATALOGS[kind]
    lines = open(path, encoding="utf-8").read().splitlines()
    out, removed = [], 0
    for line in lines:
        s = line.strip()
        if s.startswith("{"):
            m = _ROW_RE.search(line)
            if m:
                try:
                    d = ast.literal_eval(m.group(0))
                    if isinstance(d, dict) and key_of(d, kind) in dead_keys:
                        removed += 1
                        continue
                except (ValueError, SyntaxError, KeyError):
                    pass
        out.append(line)

    open(path, "w", encoding="utf-8", newline="\n").write("\n".join(out) + "\n")
    print(f"[{kind}] removed {removed} rows from {path} "
          f"(expected {len(dead)})")
    if removed != len(dead):
        print("!! WARNING: removed count != dead count — investigate")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1], sys.argv[2]))
