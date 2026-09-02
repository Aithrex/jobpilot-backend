"""Golden-set evaluation for `services.tech_taxonomy.classify`.

Acceptance targets (mission):
  core-tech precision >= 95%
  core-tech recall    >= 92%
  non-tech leakage    <= 2%    (non-tech rows predicted core)

Reported PER FAMILY as well as in aggregate, and per stratum. Stratum H is
sampled verbatim from live `job_postings` titles that the previous taxonomy
version classified `unknown`, so the eval measures the classifier against the
job market and not only against fixtures its author wrote.

"Leakage" is measured against the truly nontechnical strata only. An
`unknown` prediction on a `core` row costs recall - that is intended: the
engine is allowed to say it does not know, and it pays for it in recall
rather than in precision.
"""
from __future__ import annotations

import collections
import sys
from pathlib import Path
from typing import Dict, List

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "shared" / "python"))
sys.path.insert(0, str(ROOT / "evals" / "data"))

from services import tech_taxonomy as tx          # noqa: E402
from tech_taxonomy_golden import GOLDEN           # noqa: E402


def run(verbose: bool = True):
    tp = fp = fn = 0
    nontech_rows = 0
    nontech_leaked = 0
    family_ok = family_total = 0
    misses = []

    # PER-FAMILY confusion. An aggregate of 95% can hide a family that is
    # wrong every time, and with 14 families and 102 specialties an aggregate
    # is exactly the wrong resolution to report at: the question a candidate
    # cares about is "does it find MY discipline", not "is it usually right".
    fam_tp: Dict[str, int] = collections.defaultdict(int)
    fam_fp: Dict[str, int] = collections.defaultdict(int)
    fam_fn: Dict[str, int] = collections.defaultdict(int)
    # Strata, so the LIVE-sourced rows (stratum H) can be reported apart from
    # the hand-written hypotheses. A fixture set the author invented and a
    # sample of what the inventory actually contains are different evidence.
    strata: Dict[str, List[int]] = collections.defaultdict(lambda: [0, 0])

    for title, desc, dept, want_scope, want_family, note in GOLDEN:
        got = tx.classify(title, description=desc, department=dept)
        gs = got.tech_scope

        if want_scope == tx.SCOPE_CORE:
            if gs == tx.SCOPE_CORE:
                tp += 1
            else:
                fn += 1
                misses.append(("RECALL", title, want_scope, gs, got.role_family, note))
        else:
            if gs == tx.SCOPE_CORE:
                fp += 1
                misses.append(("PRECISION", title, want_scope, gs, got.role_family, note))

        if want_scope == tx.SCOPE_NON_TECH:
            nontech_rows += 1
            if gs == tx.SCOPE_CORE:
                nontech_leaked += 1

        # scope agreement for adjacent/unknown too
        if want_scope in (tx.SCOPE_ADJACENT, tx.SCOPE_UNKNOWN) and gs != want_scope:
            misses.append(("SCOPE", title, want_scope, gs, got.role_family, note))

        stratum = str(note or "?").split(" ")[0].split("-")[0].strip() or "?"
        strata[stratum][1] += 1
        if gs == want_scope:
            strata[stratum][0] += 1

        if want_family:
            family_total += 1
            if got.role_family == want_family or want_family in got.role_families:
                family_ok += 1
                fam_tp[want_family] += 1
            else:
                fam_fn[want_family] += 1
                for f in (got.role_families or ([got.role_family] if got.role_family else [])):
                    fam_fp[f] += 1
                misses.append(("FAMILY", title, want_family,
                               got.role_family, got.role_families, note))
        else:
            # No family expected. Predicting one on a non-tech row is a false
            # positive FOR THAT FAMILY, which is how "software_engineering
            # swallows the retail floor" would show up here.
            if want_scope == tx.SCOPE_NON_TECH:
                for f in (got.role_families or []):
                    fam_fp[f] += 1

        if got.tech_scope != tx.SCOPE_UNKNOWN and not got.classification_evidence:
            misses.append(("NO_EVIDENCE", title, want_scope, gs, None, note))
        if got.taxonomy_version != tx.TAXONOMY_VERSION:
            misses.append(("NO_VERSION", title, "", "", None, note))

    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    leakage = nontech_leaked / nontech_rows if nontech_rows else 0.0
    fam_acc = family_ok / family_total if family_total else 0.0

    per_family = {}
    for fam in sorted(set(fam_tp) | set(fam_fp) | set(fam_fn)):
        t, f, n = fam_tp[fam], fam_fp[fam], fam_fn[fam]
        per_family[fam] = {
            "precision": (t / (t + f)) if (t + f) else None,
            "recall": (t / (t + n)) if (t + n) else None,
            "tp": t, "fp": f, "fn": n,
        }
    per_stratum = {k: {"correct": v[0], "rows": v[1],
                       "accuracy": (v[0] / v[1]) if v[1] else 0.0}
                   for k, v in sorted(strata.items())}

    if verbose:
        print(f"taxonomy_version   {tx.TAXONOMY_VERSION}")
        print(f"rows               {len(GOLDEN)}")
        print(f"core precision     {precision:.3f}   (target >= 0.95)  tp={tp} fp={fp}")
        print(f"core recall        {recall:.3f}   (target >= 0.92)  fn={fn}")
        print(f"non-tech leakage   {leakage:.3f}   (target <= 0.02)  {nontech_leaked}/{nontech_rows}")
        print(f"family accuracy    {fam_acc:.3f}   {family_ok}/{family_total}")

        print("\nper-family precision / recall")
        print(f"  {'family':<26}{'prec':>8}{'rec':>8}{'tp':>6}{'fp':>6}{'fn':>6}")
        for fam, m in per_family.items():
            pr = f"{m['precision']:.2f}" if m["precision"] is not None else "  - "
            rc = f"{m['recall']:.2f}" if m["recall"] is not None else "  - "
            print(f"  {fam:<26}{pr:>8}{rc:>8}{m['tp']:>6}{m['fp']:>6}{m['fn']:>6}")

        print("\nper-stratum accuracy   (H = titles taken verbatim from live "
              "job_postings)")
        for st, m in per_stratum.items():
            print(f"  {st:<6}{m['accuracy']:>8.3f}   {m['correct']}/{m['rows']}")

        if misses:
            print(f"\n--- {len(misses)} issue(s) ---")
            for m in misses:
                print("  ", m)
    return {"precision": precision, "recall": recall, "leakage": leakage,
            "family_accuracy": fam_acc, "misses": misses,
            "per_family": per_family, "per_stratum": per_stratum,
            "tp": tp, "fp": fp, "fn": fn}


if __name__ == "__main__":
    r = run()
    ok = (r["precision"] >= 0.95 and r["recall"] >= 0.92 and r["leakage"] <= 0.02)
    print("\nPASS" if ok else "\nFAIL")
    sys.exit(0 if ok else 1)
