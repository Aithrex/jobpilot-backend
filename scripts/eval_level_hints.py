"""Offline: how often does `jd_profile.read_level_hint` agree with the years a
posting STATES? No database, no network.

The hint is meant for postings that state no level and no years, which have
no answer key. Postings that DO state years have one: the band those years
give (`fit_score._level_from_years`). So the hint is run on those bodies and
compared with that band - a fair precision check, because the hint phrases
never read years. Coverage is measured on the silent postings.

    python scripts/eval_level_hints.py ~/jd_corpus/chunk_000.jsonl.gz [more ...] [--limit 30000]
"""
from __future__ import annotations

import argparse
import gzip
import html
import json
import os
import re
import sys
from collections import Counter

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared", "python"))

from services import fit_score, jd_profile  # noqa: E402

_TAG = re.compile(r"<[^>]+>")
#: Bands that count as agreeing with a hint.
AGREE = {"senior": {"senior", "staff"}, "entry": {"entry", "intern"}, "mid": {"mid"}}
NEAR = {"senior": {"mid"}, "entry": {"mid"}, "mid": {"entry", "senior"}}


def clean(text: str) -> str:
    text = html.unescape(_TAG.sub("\n", text or ""))
    return re.sub(r"[ \t]+", " ", text)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+")
    ap.add_argument("--limit", type=int, default=30000)
    ap.add_argument("--show", type=int, default=0, help="print N disagreeing sentences per band")
    a = ap.parse_args()

    agree, near, wrong = Counter(), Counter(), Counter()
    silent = silent_hit = 0
    silent_by = Counter()
    misses = {k: [] for k in AGREE}
    n = 0
    for path in a.files:
        with gzip.open(os.path.expanduser(path), "rt") as fh:
            for line in fh:
                if n >= a.limit:
                    break
                body = clean(json.loads(line).get("t") or "")
                if len(body) < 300:
                    continue
                n += 1
                norm = fit_score._norm(body)
                years = fit_score._required_years(norm)
                by_years = fit_score._level_from_years(years)
                early = bool(fit_score._EARLY_CAREER_JD.search(norm[:4000]))
                hint = jd_profile.read_level_hint(body)
                if by_years is None and not early:
                    silent += 1
                    if hint:
                        silent_hit += 1
                        silent_by[hint["level"]] += 1
                    continue
                if not hint or by_years is None:
                    continue
                lvl = hint["level"]
                if by_years in AGREE[lvl]:
                    agree[lvl] += 1
                elif by_years in NEAR[lvl]:
                    near[lvl] += 1
                else:
                    wrong[lvl] += 1
                if by_years not in AGREE[lvl] and len(misses[lvl]) < a.show:
                    misses[lvl].append((by_years, years, hint["basis"][:160]))
    print(f"JDs read: {n}")
    for lvl in AGREE:
        tot = agree[lvl] + near[lvl] + wrong[lvl]
        if tot:
            print(f"  {lvl:6s} hint on JDs stating years: {tot:5d}  exact {agree[lvl] / tot:6.1%}  "
                  f"one band off {near[lvl] / tot:6.1%}  wrong {wrong[lvl] / tot:6.1%}")
    print(f"silent JDs (no years, no early-career wording): {silent}; hint fires on {silent_hit} "
          f"({silent_hit / max(1, silent):.1%}) -> {dict(silent_by)}")
    for lvl, rows in misses.items():
        for by, yrs, basis in rows:
            print(f"  [{lvl} vs {by} {yrs}y] {basis}")


if __name__ == "__main__":
    main()
