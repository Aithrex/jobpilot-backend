"""Golden-set evaluation for `services.tech_taxonomy.classify`.

Acceptance targets (mission):
  core-tech precision >= 95%
  core-tech recall    >= 92%
  non-tech leakage    <= 2%    (non-tech rows predicted core)

"Leakage" is measured against the truly nontechnical strata only. An
`unknown` prediction on a `core` row costs recall - that is intended: the
engine is allowed to say it does not know, and it pays for it in recall
rather than in precision.
"""
from __future__ import annotations

import sys
from pathlib import Path

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

        if want_family:
            family_total += 1
            if got.role_family == want_family or want_family in got.role_families:
                family_ok += 1
            else:
                misses.append(("FAMILY", title, want_family,
                               got.role_family, got.role_families, note))

        if got.tech_scope != tx.SCOPE_UNKNOWN and not got.classification_evidence:
            misses.append(("NO_EVIDENCE", title, want_scope, gs, None, note))
        if got.taxonomy_version != tx.TAXONOMY_VERSION:
            misses.append(("NO_VERSION", title, "", "", None, note))

    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    leakage = nontech_leaked / nontech_rows if nontech_rows else 0.0
    fam_acc = family_ok / family_total if family_total else 0.0

    if verbose:
        print(f"taxonomy_version   {tx.TAXONOMY_VERSION}")
        print(f"rows               {len(GOLDEN)}")
        print(f"core precision     {precision:.3f}   (target >= 0.95)  tp={tp} fp={fp}")
        print(f"core recall        {recall:.3f}   (target >= 0.92)  fn={fn}")
        print(f"non-tech leakage   {leakage:.3f}   (target <= 0.02)  {nontech_leaked}/{nontech_rows}")
        print(f"family accuracy    {fam_acc:.3f}   {family_ok}/{family_total}")
        if misses:
            print(f"\n--- {len(misses)} issue(s) ---")
            for m in misses:
                print("  ", m)
    return {"precision": precision, "recall": recall, "leakage": leakage,
            "family_accuracy": fam_acc, "misses": misses,
            "tp": tp, "fp": fp, "fn": fn}


if __name__ == "__main__":
    r = run()
    ok = (r["precision"] >= 0.95 and r["recall"] >= 0.92 and r["leakage"] <= 0.02)
    print("\nPASS" if ok else "\nFAIL")
    sys.exit(0 if ok else 1)
