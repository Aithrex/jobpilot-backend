#!/usr/bin/env python3
"""Live-title evaluation for `services.tech_taxonomy.classify`.

Reproducible and offline: it reads one checked-in dataset
(`evals/data/tech_taxonomy_live_titles.py`, 559 rows) and touches no database
and no network. `PYTHONPATH=shared/python python evals/eval_live_titles.py`
prints the same numbers on any machine, which is the only property that makes
a classifier change arguable.

What this measures that the golden set does not
-----------------------------------------------
`eval_tech_taxonomy.py` runs 143 rows written to probe a hypothesis. This runs
559 titles taken verbatim from live `job_postings`, weighted the way the market
actually is: 334 of them are non-technical, because 334/559 of the titles a
seeker meets are non-technical, and a classifier evaluated only on a balanced
fixture set is evaluated on a market that does not exist.

Two numbers matter and they are reported separately:

  SCOPE ACCURACY over all 559 rows — the headline.
  OVERRIDE ACCURACY over the 148 rows where the reviewer DISAGREED with the
  classifier when the set was built. Those are the known defects. A change
  that improves the headline while regressing these has traded a measured
  bug for an unmeasured one.

Posting-weighted accuracy is reported alongside the row-weighted one, because
one wrong answer on "Pharmacy Technician" (1,482 live postings) and one wrong
answer on a title with 1 posting are not the same mistake.
"""
from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "shared" / "python"))
sys.path.insert(0, str(ROOT / "evals" / "data"))

from services import tech_taxonomy as tx                     # noqa: E402
from tech_taxonomy_live_titles import LIVE_TITLES, OVERRIDDEN  # noqa: E402

#: Row-weighted scope accuracy the suite will not go below.
MIN_SCOPE_ACCURACY = 0.90
#: The reviewer's corrections are the known defects. They are held higher.
MIN_OVERRIDE_ACCURACY = 0.90
#: A non-technical title predicted `core` is the expensive error: it puts a
#: Pharmacy Technician in front of a backend engineer.
MAX_NON_TECH_LEAKAGE = 0.02


def run(verbose: bool = True) -> Dict[str, Any]:
    rows = 0
    correct = 0
    weighted = weighted_correct = 0
    nontech = leaked = 0
    nontech_weighted = leaked_weighted = 0
    confusion: Dict[str, collections.Counter] = collections.defaultdict(
        collections.Counter)
    misses: List[Dict[str, Any]] = []
    override_total = override_correct = 0
    override_keys = {r[0] for r in OVERRIDDEN}

    for title, want_scope, want_family, note, postings in LIVE_TITLES:
        got = tx.classify(title)
        gs = got.tech_scope
        n = max(1, int(postings or 1))
        rows += 1
        weighted += n
        ok = (gs == want_scope)
        if want_family and ok:
            ok = want_family in (got.role_families or [])
        if ok:
            correct += 1
            weighted_correct += n
        else:
            misses.append({"title": title, "expected": want_scope,
                           "expected_family": want_family, "got": gs,
                           "got_families": list(got.role_families or []),
                           "postings": n, "note": note})
        confusion[want_scope][gs] += 1
        if title in override_keys:
            override_total += 1
            override_correct += 1 if ok else 0
        if want_scope == tx.SCOPE_NON_TECH:
            nontech += 1
            nontech_weighted += n
            if gs == tx.SCOPE_CORE:
                leaked += 1
                leaked_weighted += n

    summary = {
        "dataset_rows": rows,
        "live_postings_represented": weighted,
        "scope_accuracy": round(correct / rows, 4) if rows else 0.0,
        "posting_weighted_scope_accuracy":
            round(weighted_correct / weighted, 4) if weighted else 0.0,
        "reviewer_override_rows": override_total,
        "reviewer_override_accuracy":
            round(override_correct / override_total, 4) if override_total else 0.0,
        "non_tech_rows": nontech,
        "non_tech_leaked_to_core": leaked,
        "non_tech_leakage": round(leaked / nontech, 4) if nontech else 0.0,
        "posting_weighted_non_tech_leakage":
            round(leaked_weighted / nontech_weighted, 4) if nontech_weighted else 0.0,
        "confusion": {k: dict(v) for k, v in confusion.items()},
        "taxonomy_version": tx.TAXONOMY_VERSION,
    }

    if verbose:
        print(f"dataset                 {rows} reviewed live titles "
              f"({weighted:,} live postings)")
        print(f"taxonomy                {tx.TAXONOMY_VERSION}")
        print(f"scope accuracy          {summary['scope_accuracy']:.3f}   "
              f"(target >= {MIN_SCOPE_ACCURACY})")
        print(f"  posting-weighted      "
              f"{summary['posting_weighted_scope_accuracy']:.3f}")
        print(f"reviewer overrides      "
              f"{summary['reviewer_override_accuracy']:.3f}   "
              f"({override_correct}/{override_total}, target >= "
              f"{MIN_OVERRIDE_ACCURACY})")
        print(f"non-tech leakage        {summary['non_tech_leakage']:.4f}   "
              f"({leaked}/{nontech}, target <= {MAX_NON_TECH_LEAKAGE})")
        print(f"  posting-weighted      "
              f"{summary['posting_weighted_non_tech_leakage']:.4f}")
        print("\nconfusion  expected -> predicted")
        for want in ("core", "adjacent", "non_tech", "unknown"):
            got_counts = confusion.get(want) or {}
            total = sum(got_counts.values())
            if not total:
                continue
            parts = ", ".join(f"{k}={v}" for k, v in
                              sorted(got_counts.items(), key=lambda kv: -kv[1]))
            print(f"  {want:<10} n={total:<4} {parts}")
        if misses:
            print(f"\n{len(misses)} miss(es), heaviest first:")
            for m in sorted(misses, key=lambda m: -m["postings"])[:30]:
                print(f"  [{m['postings']:>5}] want {m['expected']:<9} "
                      f"got {m['got']:<9} {m['title'][:58]}")
    return summary


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default=None)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()
    summary = run(verbose=not args.quiet)
    ok = (summary["scope_accuracy"] >= MIN_SCOPE_ACCURACY
          and summary["reviewer_override_accuracy"] >= MIN_OVERRIDE_ACCURACY
          and summary["non_tech_leakage"] <= MAX_NON_TECH_LEAKAGE)
    print("\nPASS" if ok else "\nFAIL")
    if args.json:
        Path(args.json).write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(f"wrote {args.json}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
