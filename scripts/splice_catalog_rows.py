#!/usr/bin/env python3
"""
splice_catalog_rows.py — append validated `new` rows from a verdict JSON into a
catalog module, inserting them just before the list's closing `]`.

Consumes the output of scripts/extract_doc_rows.py or
scripts/validate_candidates.py. Idempotent: refuses to insert if <marker> is
already present in the target file.

Usage
-----
    python scripts/splice_catalog_rows.py <verdict.json> <workday|career> "<marker text>"

`INDUSTRIES` in each catalog is derived from the rows, so new industry keys work
the instant a row uses them — no other edit needed on the backend.
"""
from __future__ import annotations

import json
import sys

CATALOGS = {
    "workday": "shared/python/services/workday_tenant_catalog.py",
    "career": "shared/python/services/career_pages_catalog.py",
}
KEY_ORDER = {
    "workday": ["display_name", "tenant", "wd_host", "site", "industry"],
    "career": ["display_name", "ats", "slug", "industry"],
}


def fmt_row(row, kind):
    parts = [f'"{k}": {json.dumps(str(row[k]), ensure_ascii=True)}' for k in KEY_ORDER[kind]]
    return "    {" + ", ".join(parts) + "},"


def main(verdict_path, kind, marker):
    with open(verdict_path, encoding="utf-8") as fh:
        rows = json.load(fh)[kind]["new"]
    rows = [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows]
    if not rows:
        print("no rows to insert")
        return 0

    path = CATALOGS[kind]
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    if marker in src:
        print(f"!! marker {marker!r} already present in {path} — skipping")
        return 0

    lines = src.splitlines()
    close_idx = next((i for i in range(len(lines) - 1, -1, -1)
                      if lines[i].strip() == "]"), None)
    if close_idx is None:
        print("!! could not find closing ] of the catalog list")
        return 1

    block = [f"    # {marker}"] + [fmt_row(r, kind) for r in rows]
    new_lines = lines[:close_idx] + block + lines[close_idx:]
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(new_lines) + "\n")
    print(f"inserted {len(rows)} rows into {path} before line {close_idx + 1}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1], sys.argv[2], sys.argv[3]))
