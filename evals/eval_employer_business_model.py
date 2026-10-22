"""Held-out evaluation of the employer business-model classifier (ebm-1.0.0).

What it measures
----------------
`evals/data/employer_business_model_holdout.json` holds 60 of the 174
companies in the 2026-10-21 research register (ceil(n/3) per disposition
stratum, chosen by sha1 BEFORE any rule was written). For the evaluation
those companies are REMOVED from the seed (`build_seed_index(exclude_names=...)`)
so the classifier meets them as it would meet an unseen employer: by domain
(which no longer hits the seed) and by name. The number therefore measures
the RULES on companies they have not memorised - the long tail - not the
seed's recall of itself.

Binary ground truth: register `exclude_staffing` / `exclude_consultancy` /
`exclude_intermediary` are POSITIVE (strict mode should exclude),
`eligible_direct` is NEGATIVE. The review queue (mixed / identity-unresolved)
is ambiguous and is kept OUT of binary truth; its predicted distribution is
reported separately.

"Excluded under strict" = predicted kind in staffing / consultancy /
talent_marketplace / mixed. `unknown` is SHOWN (labelled unverified) under
the default policy, so an unknown prediction on a positive is a miss.

Run: PYTHONPATH=shared/python python3 evals/eval_employer_business_model.py
"""
from __future__ import annotations

import json
import math
import os
import sys
from collections import Counter
from typing import Any, Dict, List

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _p in (os.path.join(_ROOT, "shared", "python"),):
    if _p not in sys.path:
        sys.path.insert(0, _p)

HOLDOUT_PATH = os.path.join(_HERE, "data", "employer_business_model_holdout.json")
POSITIVE = {"staffing", "consultancy", "talent_marketplace"}
EXCLUDED_KINDS = {"staffing", "consultancy", "talent_marketplace", "mixed"}


def load_holdout() -> Dict[str, Any]:
    with open(HOLDOUT_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def wilson(k: int, n: int, z: float = 1.96):
    if n == 0:
        return (None, None)
    p = k / n
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return (round(max(0.0, centre - half), 3), round(min(1.0, centre + half), 3))


def evaluate(*, memorised: bool = False) -> Dict[str, Any]:
    from services import employer_business_model as ebm
    data = load_holdout()
    rows = data["rows"]
    names = [r["company"] for r in rows]
    seed = ebm.build_seed_index() if memorised else ebm.build_seed_index(exclude_names=names)

    tp = fp = fn = tn = 0
    kind_correct = 0
    misses: List[Dict[str, Any]] = []
    false_exclusions: List[Dict[str, Any]] = []
    ambiguous = Counter()
    bases = Counter()
    for r in rows:
        model = ebm.classify_employer(None, domains=[r["domain"]] if r.get("domain") else [],
                                      name=r["company"], seed=seed)
        bases[model["basis"]] += 1
        excluded = model["kind"] in EXCLUDED_KINDS
        if r["label"] == "ambiguous":
            ambiguous[("excluded" if excluded else "shown") + ":" + model["kind"]] += 1
            continue
        positive = r["label"] in POSITIVE
        if positive and excluded:
            tp += 1
            kind_correct += int(model["kind"] == r["label"])
        elif positive:
            fn += 1
            misses.append({"company": r["company"], "label": r["label"],
                           "predicted": model["kind"]})
        elif excluded:
            fp += 1
            false_exclusions.append({"company": r["company"], "predicted": model["kind"],
                                     "basis": model["basis"]})
        else:
            tn += 1
    n_pos, n_neg = tp + fn, fp + tn
    return {
        "holdout_version": data["version"],
        "seed": "full seed (memorised; NOT a quality claim)" if memorised
                else "seed minus holdout (unseen companies)",
        "rows": len(rows), "binary_rows": n_pos + n_neg, "ambiguous_rows": sum(ambiguous.values()),
        "positives": n_pos, "negatives": n_neg,
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "precision": round(tp / (tp + fp), 3) if (tp + fp) else None,
        "precision_ci95": wilson(tp, tp + fp),
        "recall": round(tp / n_pos, 3) if n_pos else None,
        "recall_ci95": wilson(tp, n_pos),
        "false_exclusion_rate_operating": round(fp / n_neg, 3) if n_neg else None,
        "false_exclusion_ci95": wilson(fp, n_neg),
        "kind_correct_among_tp": kind_correct,
        "ambiguous_predictions": dict(ambiguous),
        "basis_counts": dict(bases),
        "misses": misses,
        "false_exclusions": false_exclusions,
    }


def main() -> None:
    for memorised in (False, True):
        res = evaluate(memorised=memorised)
        print(f"== {res['seed']}  ({res['holdout_version']})")
        print(f"   rows={res['rows']} binary={res['binary_rows']} "
              f"(pos={res['positives']}, neg={res['negatives']}) ambiguous={res['ambiguous_rows']}")
        print(f"   staffing+consultancy+intermediary exclusion: precision {res['tp']}/"
              f"{res['tp'] + res['fp']} = {res['precision']} CI95 {res['precision_ci95']}; "
              f"recall {res['tp']}/{res['positives']} = {res['recall']} CI95 {res['recall_ci95']}")
        print(f"   false exclusions of operating employers: {res['fp']}/{res['negatives']} "
              f"= {res['false_exclusion_rate_operating']} CI95 {res['false_exclusion_ci95']}")
        print(f"   ambiguous (review queue) predictions: {res['ambiguous_predictions']}")
        print(f"   basis: {res['basis_counts']}")
        if not memorised:
            print(f"   misses: {[m['company'] for m in res['misses']]}")
            print(f"   false exclusions: {res['false_exclusions']}")


if __name__ == "__main__":
    main()
