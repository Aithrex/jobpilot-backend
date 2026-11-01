#!/usr/bin/env python
"""Trains `services/jd_skill_model`. Offline: no database, no network, no model calls.

    python scripts/train_jd_skill_model.py stats --corpus corpus.jsonl --out stats.json
    python scripts/train_jd_skill_model.py train --stats stats.json

`stats` reads unlabelled postings (`{"id", "t"}` per line) and records how each
phrase behaves across them. Every answer-key row is left out.

`train` learns from the answer key's tuning rows. The `holdout` split is never
loaded here: it is what tells us whether this worked.

Needs scikit-learn, which is deliberately not a runtime dependency: production
only reads the weights file this writes.
"""

from __future__ import annotations

import argparse
import gzip
import json
import math
import os
import random
import sys
from collections import Counter
from typing import Any, Dict, List, Sequence, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [os.path.join(ROOT, "shared", "python"), ROOT]

from services import fit_score, jd_skill_model as model  # noqa: E402
from services.jd_skill_vocabulary import JD_SKILL_VOCABULARY  # noqa: E402

KEY = os.path.join(ROOT, "evals", "data", "jd_answer_key.jsonl")
NEGATIVE_RATE = 0.12
KEEP_SHARE = 0.10
MIN_FEATURE_COUNT = 3
WEIGHT_FLOOR = 0.01
TREES = 250
SEED = 7


def _key_rows() -> List[Dict[str, Any]]:
    with open(KEY, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle]


def build_stats(corpus: str, out: str, limit: int, min_seen: int) -> None:
    keyed = {row["id"] for row in _key_rows()}
    stats: Dict[str, List[int]] = {}
    read = 0
    with open(corpus, encoding="utf-8") as handle:
        for line in handle:
            posting = json.loads(line)
            if posting["id"] in keyed:
                continue
            flags: Dict[str, List[int]] = {}
            for phrase, features in model.mentions(posting["t"]):
                present = set(features)
                row = flags.setdefault(phrase, [0, 0, 0, 0, 0, 0])
                row[0] |= bool(present & {"sec=required", "sec=preferred"})
                row[1] |= "listed" in present
                row[2] |= "cue_before" in present
                row[3] |= any(f.startswith("shape=") and f[6:] not in ("lower", "first")
                              for f in present)
                row[4] |= bool(present & {"sec=benefits", "sec=legal", "sec=about"})
                row[5] |= "bullet" in present
            for phrase, row in flags.items():
                total = stats.setdefault(phrase, [0, 0, 0, 0, 0, 0, 0])
                total[0] += 1
                for i, value in enumerate(row):
                    total[i + 1] += value
            read += 1
            if read % 2000 == 0:  # a phrase seen once stays seen once, mostly
                stats = {k: v for k, v in stats.items() if v[0] > 1}
            if read >= limit:
                break
    kept = {k: v for k, v in stats.items() if v[0] >= min_seen}
    with open(out, "w", encoding="utf-8") as handle:
        json.dump(kept, handle, separators=(",", ":"))
    print(f"{read} postings, {len(kept)} phrases -> {out}")


def _targets(names: Sequence[Any]) -> set:
    out = set()
    for name in names or []:
        text = str(name).strip().lower()
        if text:
            out.add(text)
            out.update(fit_score.find_skills(text))
    return out


def _examples(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rng = random.Random(SEED)
    known = set(JD_SKILL_VOCABULARY)
    data = []
    for row in rows:
        want = _targets(row["labels"].get("technical_skills"))
        picked: List[Tuple[str, List[str], int]] = []
        for phrase, features in model.mentions(row["text"], known):
            found = set(fit_score.find_skills(phrase))
            label = int(phrase in want or bool(found & want))
            if label or rng.random() < NEGATIVE_RATE:
                picked.append((phrase, features, label))
        data.append({"split": row["split"], "picked": picked})
    return data


def _first_stage(rows: Sequence[Dict[str, Any]], index: Dict[str, int]):
    from scipy.sparse import csr_matrix
    from sklearn.linear_model import SGDClassifier

    columns, pointers, labels = [], [0], []
    for row in rows:
        for _, features, label in row["picked"]:
            columns.extend(index[f] for f in features if f in index)
            pointers.append(len(columns))
            labels.append(label)
    matrix = csr_matrix(([1.0] * len(columns), columns, pointers), shape=(len(labels), len(index)))
    clf = SGDClassifier(loss="log_loss", alpha=3e-6, max_iter=12, tol=None, random_state=1)
    clf.fit(matrix, labels)
    return float(clf.intercept_[0]), clf.coef_[0]


def _first_pass(bias: float, coef, index: Dict[str, int], features: Sequence[str]) -> float:
    return bias + sum(float(coef[index[f]]) for f in features if f in index)


def _export_tree(predictor) -> List[List[float]]:
    return [[-1 if node["is_leaf"] else int(node["feature_idx"]),
             float(node["num_threshold"]),
             int(node["left"]), int(node["right"]), float(node["value"])]
            for node in predictor.nodes]


def train(stats_path: str, threshold: float, min_seen: int, out: str,
          held_out: Sequence[str] = ()) -> None:
    from sklearn.ensemble import HistGradientBoostingClassifier

    with open(stats_path, encoding="utf-8") as handle:
        stats = {k: v for k, v in json.load(handle).items() if v[0] >= min_seen}
    rows = [r for r in _key_rows() if r["split"] not in {"holdout", *held_out}]
    data = _examples(rows)
    seen = Counter(f for row in data for _, features, _ in row["picked"] for f in features)
    index = {f: i for i, f in enumerate(sorted(f for f, c in seen.items() if c >= MIN_FEATURE_COUNT))}

    # The trees must see first-stage scores it made WITHOUT having learned that row.
    splits = sorted({row["split"] for row in data})
    grid, labels = [], []
    for split in splits:
        bias, coef = _first_stage([r for r in data if r["split"] != split], index)
        for row in data:
            if row["split"] != split:
                continue
            for phrase, features, label in row["picked"]:
                first = _first_pass(bias, coef, index, features)
                grid.append(model.signals(phrase, features, stats, first,
                                          bool(fit_score.find_skills(phrase))))
                labels.append(label)
    trees = HistGradientBoostingClassifier(max_iter=TREES, learning_rate=0.08, max_leaf_nodes=31,
                                           l2_regularization=1.0, random_state=1)
    trees.fit(grid, labels)
    bias, coef = _first_stage(data, index)
    weights = {f: round(float(coef[i]), 4) for f, i in index.items()
               if abs(float(coef[i])) >= WEIGHT_FLOOR}
    payload = {
        "version": "skills-1.0.0",
        "trained_on": len(rows),
        "threshold": threshold,
        "keep_share": KEEP_SHARE,
        "negative_rate": NEGATIVE_RATE,
        "bias": round(bias, 4),
        "baseline": float(trees._baseline_prediction.ravel()[0]),
        "weights": weights,
        "trees": [_export_tree(group[0]) for group in trees._predictors],
        "stats": stats,
    }
    with gzip.GzipFile(out, "wb", mtime=0) as handle:
        handle.write(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))

    # The plain-Python trees must agree with what scikit-learn learned.
    model._model = None
    sample = grid[:: max(1, len(grid) // 2000)]
    worst = max(abs(float(payload["baseline"]) + sum(model._tree_value(t, r) for t in payload["trees"])
                    - float(trees.decision_function([r])[0])) for r in sample[:300])
    print(f"{len(rows)} JDs, {len(labels)} examples, {len(weights)} weights, "
          f"{len(payload['trees'])} trees, {len(stats)} phrases")
    print(f"{os.path.getsize(out) / 1e6:.1f} MB -> {out}; worst tree disagreement {worst:.5f}")
    if worst > 0.01:
        raise SystemExit("exported trees do not match the trained model")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    one = sub.add_parser("stats")
    one.add_argument("--corpus", required=True)
    one.add_argument("--out", required=True)
    one.add_argument("--limit", type=int, default=math.inf)
    one.add_argument("--min-seen", type=int, default=3)
    two = sub.add_parser("train")
    two.add_argument("--stats", required=True)
    two.add_argument("--threshold", type=float, default=0.40)
    two.add_argument("--min-seen", type=int, default=10)
    two.add_argument("--out", default=model.WEIGHTS_FILE)
    two.add_argument("--hold-out", nargs="*", default=[],
                     help="extra splits to keep out, to test on them before they join training")
    args = parser.parse_args()
    if args.command == "stats":
        build_stats(args.corpus, args.out, args.limit, args.min_seen)
    else:
        train(args.stats, args.threshold, args.min_seen, args.out, args.hold_out)


if __name__ == "__main__":
    main()
