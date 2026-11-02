#!/usr/bin/env python
"""Trains `services/jd_line_model`. Offline: no database, no network, no model calls.

    python scripts/train_jd_line_model.py silver --corpus ~/jd_corpus --out silver.jsonl
    python scripts/train_jd_line_model.py cv --silver silver.jsonl
    python scripts/train_jd_line_model.py train --silver silver.jsonl

`silver` turns unlabelled postings into labelled lines. A heading labels the lines
under it, a few patterns correct what headings get wrong (equal-opportunity text
trailing a Benefits section), and a line seen across many postings takes the role most
of them gave it.

`train` fits the first stage on those lines and the second on the `dev` half of the
blind line key. The first stage never sees the key, so the second learns from the same
kind of evidence it will meet in production. `cv` scores that recipe on `dev` JDs the
second stage did not see. The `test` half is never loaded here: it is what tells us
whether this worked.

Needs scikit-learn and numpy, which are deliberately not runtime dependencies:
production only reads the weights file this writes.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import os
import random
import re
import sys
from collections import Counter
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [os.path.join(ROOT, "shared", "python"), ROOT]

from services import jd_line_model as model  # noqa: E402

KEY = os.path.join(ROOT, "evals", "data", "jd_line_key.jsonl")
#: chunk 015 and 029 hold the fresh JDs and the line key's JDs. Neither may train.
HELD_OUT_CHUNKS = ("015", "029")
MIN_SHARE = 0.6
MIN_FEATURE_COUNT = 8
WEIGHT_FLOOR = 0.04
SEED = 7

ROLES = model.WORD_ROLES
INDEX = {role: i for i, role in enumerate(ROLES)}
TREE_DEPTH = 3
TREE_ROUNDS = 120
TREE_RATE = 0.08
TREE_LEAF = 12

# High-precision cues for what section headings get wrong. They train the model and
# never run in production: the model has to learn the language, not these lists.
_LEGAL = re.compile(
    r"equal (?:employment )?opportunit|without regard to|regardless of (?:race|age|sex|gender|their)|"
    r"reasonable accommodation|e-verify|protected (?:veteran|class|status|characteristic)|"
    r"sexual orientation|national origin|gender identity|affirmative action|"
    r"background (?:check|screening|investigation)|drug (?:test|screen)|at-will|discriminat|"
    r"authorized to work|work authorization|visa sponsorship|sponsor(?:ship)?\b.*visa|"
    r"security clearance|u\.?s\.? citizen|privacy (?:policy|notice|statement)|recruitment fraud|"
    r"know your rights|applicants? with disabilit|fair chance|pay transparency|"
    r"(?:we|employer) (?:will not|does not|do not) (?:sponsor|discriminate)", re.I)
_BENEFIT = re.compile(
    r"401\(?k|paid time off|\bpto\b|medical,? dental|health insurance|dental(?:,| and) vision|"
    r"life insurance|parental leave|tuition (?:reimbursement|assistance)|salary range|pay range|"
    r"base (?:pay|salary)|hourly (?:rate|range|pay)|\$\s?\d|compensation (?:range|package)|"
    r"bonus eligib|sign-on bonus|employee discount|wellness program|paid holidays|"
    r"retirement (?:plan|savings)|comprehensive benefits|benefits package", re.I)
_COMPANY = re.compile(
    r"\bour (?:mission|values|culture|customers|people|vision|purpose)\b|"
    r"\b(?:is|are) (?:a|an|the) (?:leading|global|world|premier|largest|nation)|"
    r"\bfounded in\b|\bheadquartered in\b|\bwe believe\b|\bfortune 500\b", re.I)
_SUMMARY = re.compile(
    r"^(?:we are|we're) (?:seeking|looking for|hiring)|\bis (?:seeking|looking for|hiring)\b|"
    r"\bthis (?:role|position) (?:is|will|reports|plays)\b|\breports (?:directly )?to\b|"
    r"^as (?:a|an|the) \w+", re.I)
_REQUIRED = re.compile(
    r"^\W*\d+\+? ?(?:years?|yrs)|\bbachelor|\bmaster's|\bdegree\b|\bmust (?:have|be|possess)\b|"
    r"^\W*(?:ability to|able to|proficien|experience (?:with|in)|knowledge of|strong|excellent)\b",
    re.I)


def weak_label(line: str, heading_label: Optional[str]) -> Optional[str]:
    """The heading's reading, corrected where the words plainly say otherwise."""
    if heading_label == "heading":
        return heading_label
    if _LEGAL.search(line):
        return "legal"
    if _BENEFIT.search(line):
        return "benefit"
    if heading_label is not None:
        return heading_label
    # No heading vouches for it: only a clear cue gets a label.
    if _COMPANY.search(line):
        return "company"
    if _SUMMARY.search(line):
        return "summary"
    if _REQUIRED.search(line):
        return "required"
    return None


def _chunks(corpus: str) -> List[str]:
    names = sorted(n for n in os.listdir(corpus)
                   if n.startswith("chunk_") and n.endswith(".jsonl.gz"))
    return [os.path.join(corpus, n) for n in names
            if not any(n == f"chunk_{held}.jsonl.gz" for held in HELD_OUT_CHUNKS)]


def _postings(corpus: str, limit: int) -> Iterator[str]:
    seen = 0
    # Spread the sample across the corpus rather than reading the first chunks.
    files = _chunks(corpus)
    per_file = max(1, limit // len(files))
    for path in files:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            for i, row in enumerate(handle):
                if i >= per_file or seen >= limit:
                    break
                seen += 1
                yield json.loads(row)["t"]


def _key(line: str) -> bytes:
    return hashlib.md5(re.sub(r"\d+", "0", line.lower()).encode()).digest()[:8]


def build_silver(corpus: str, out: str, limit: int) -> None:
    votes: Dict[bytes, List[int]] = {}
    texts: Dict[bytes, str] = {}
    jobs = lines_seen = labelled = 0
    for raw in _postings(corpus, limit):
        jobs += 1
        lines = model.lines_of(raw)
        for line, heading in zip(lines, model.silver_labels(lines)):
            lines_seen += 1
            role = weak_label(line, heading)
            if role is None or len(line) < 4:
                continue
            labelled += 1
            key = _key(line)
            row = votes.get(key)
            if row is None:
                row = votes[key] = [0] * len(ROLES)
                texts[key] = line[:320]
            row[INDEX[role]] += 1
        if jobs % 25000 == 0:
            print(jobs, lines_seen, labelled, len(votes), flush=True)
    written: Counter = Counter()
    with open(out, "w", encoding="utf-8") as handle:
        for key, row in votes.items():
            total = sum(row)
            top = max(range(len(ROLES)), key=lambda i: row[i])
            if row[top] / total < MIN_SHARE:
                continue
            written[ROLES[top]] += 1
            handle.write(json.dumps({"t": texts[key], "r": ROLES[top], "n": total}) + "\n")
    print(f"{jobs} postings, {lines_seen} lines, {labelled} labelled "
          f"({100 * labelled / max(1, lines_seen):.1f}%), {len(votes)} distinct", dict(written))


def key_rows(split: Optional[str] = None) -> List[Dict[str, Any]]:
    if not os.path.exists(KEY):
        return []
    with open(KEY, encoding="utf-8") as handle:
        rows = [json.loads(line) for line in handle if line.strip()]
    return [r for r in rows if split is None or r["split"] == split]


def _load_silver(path: str, cap: int) -> List[Tuple[str, int, float]]:
    """Every role capped, so the commonest lines cannot decide the model."""
    rng = random.Random(SEED)
    by_role: Dict[str, List[Tuple[str, float]]] = {r: [] for r in ROLES}
    with open(path, encoding="utf-8") as handle:
        for row in handle:
            item = json.loads(row)
            by_role[item["r"]].append((item["t"], 1.0 + min(2.0, math.log10(item["n"]))))
    out: List[Tuple[str, int, float]] = []
    for role, rows in by_role.items():
        rng.shuffle(rows)
        out.extend((text, INDEX[role], weight) for text, weight in rows[:cap])
    print("silver lines", {r: min(cap, len(v)) for r, v in by_role.items()}, flush=True)
    return out


def _matrix(texts: Sequence[str], vocab: Dict[str, int]):
    """The same arithmetic as `jd_line_model._log_odds`: unit-scaled binary-ish counts."""
    import numpy as np
    from scipy.sparse import csr_matrix

    data: List[float] = []
    columns: List[int] = []
    pointers = [0]
    for text in texts:
        found = model.features(text)
        scale = 1.0 / math.sqrt(max(1, len(found)))
        counts = Counter(vocab[f] for f in found if f in vocab)
        columns.extend(counts)
        data.extend(c * scale for c in counts.values())
        pointers.append(len(columns))
    return csr_matrix((np.array(data, dtype=np.float32), np.array(columns, dtype=np.int32),
                       np.array(pointers, dtype=np.int64)), shape=(len(texts), len(vocab)))


def fit(rows: List[Tuple[str, int, float]], alpha: float = 1e-6, epochs: int = 4):
    """One-vs-rest logistic regression, returned as `(vocab, bias, coef)`."""
    import numpy as np
    from sklearn.linear_model import SGDClassifier

    counts: Counter = Counter()
    for text, _, _ in rows:
        counts.update(set(model.features(text)))
    vocab = {f: i for i, (f, n) in enumerate(counts.most_common()) if n >= MIN_FEATURE_COUNT}
    print("features", len(vocab), flush=True)
    del counts
    clf = SGDClassifier(loss="log_loss", alpha=alpha, random_state=SEED)
    classes = np.arange(len(ROLES))
    rng = random.Random(SEED)
    batch = 40000
    for epoch in range(epochs):
        rng.shuffle(rows)
        for start in range(0, len(rows), batch):
            part = rows[start:start + batch]
            clf.partial_fit(_matrix([r[0] for r in part], vocab), [r[1] for r in part],
                            classes=classes, sample_weight=[r[2] for r in part])
        print("epoch", epoch, flush=True)
    return vocab, clf.intercept_, clf.coef_


def stage_one(vocab: Dict[str, int], bias, coef) -> Dict[str, Any]:
    """The first stage as `jd_line_model` reads it, pruned the way it ships."""
    floor = WEIGHT_FLOOR * float(abs(coef).max())
    weights: Dict[str, List[float]] = {}
    for feature, column in vocab.items():
        row = coef[:, column]
        if float(abs(row).max()) >= floor:
            weights[feature] = [round(float(v), 2) for v in row]
    return {"bias": [round(float(b), 3) for b in bias], "weights": weights}


def _context(first: Dict[str, Any], rows: Sequence[Dict[str, Any]]):
    import numpy as np

    data: List[List[float]] = []
    roles: List[int] = []
    for item in rows:
        scores = [model.word_scores(first, line) for line in item["lines"]]
        data.extend(model.context_rows(item["lines"], scores))
        roles.extend(model.ROLES.index(role) for role in item["roles"])
    return np.array(data, dtype=np.float64), np.array(roles)


def stage_two(first: Dict[str, Any], rows: Sequence[Dict[str, Any]]):
    from sklearn.ensemble import HistGradientBoostingClassifier

    data, roles = _context(first, rows)
    clf = HistGradientBoostingClassifier(
        max_depth=TREE_DEPTH, max_iter=TREE_ROUNDS, learning_rate=TREE_RATE,
        min_samples_leaf=TREE_LEAF, l2_regularization=1.0, random_state=SEED)
    clf.fit(data, roles)
    if list(clf.classes_) != list(range(len(model.ROLES))):
        raise SystemExit("The key must hold every role at least once")
    return clf


def _export_tree(predictor) -> List[List[float]]:
    return [[-1 if node["is_leaf"] else int(node["feature_idx"]),
             round(float(node["num_threshold"]), 4),
             int(node["left"]), int(node["right"]), round(float(node["value"]), 4)]
            for node in predictor.nodes]


def export(first: Dict[str, Any], clf, out: str) -> None:
    payload = dict(first)
    payload["version"] = model.LINE_MODEL_VERSION
    payload["baseline"] = [round(float(v), 4) for v in clf._baseline_prediction.ravel()]
    payload["trees"] = [[_export_tree(rounds[k]) for rounds in clf._predictors]
                        for k in range(len(model.ROLES))]
    with gzip.open(out, "wt", encoding="utf-8", compresslevel=9) as handle:
        json.dump(payload, handle, separators=(",", ":"))
    print(f"wrote {out}: {len(first['weights'])} features, "
          f"{os.path.getsize(out) / 1e6:.2f} MB")


def _same_answers(first: Dict[str, Any], clf, rows: Sequence[Dict[str, Any]], out: str) -> None:
    """Plain Python must answer as the library did, or the file is not the model."""
    model._model = None
    model.WEIGHTS_FILE = out
    data, _ = _context(first, rows)
    want = [model.ROLES[i] for i in clf.predict(data)]
    got: List[Any] = []
    for item in rows:
        got.extend(entry["role"] for entry in model.read_lines("\n".join(item["lines"])))
    same = sum(a == b for a, b in zip(want, got))
    print(f"plain Python agrees with training on {same} of {len(want)} lines")
    if len(want) != len(got) or same < 0.99 * len(want):
        raise SystemExit("The exported model does not match what was trained")


def cross_validate(first: Dict[str, Any], rows: Sequence[Dict[str, Any]], folds: int = 5) -> None:
    from evals import eval_jd_line_key as scorer

    def fold(item: Dict[str, Any]) -> int:
        return int(hashlib.md5(item["id"].encode()).hexdigest(), 16) % folds

    pairs: List[Tuple[str, str]] = []
    for k in range(folds):
        clf = stage_two(first, [r for r in rows if fold(r) != k])
        for item in (r for r in rows if fold(r) == k):
            data, _ = _context(first, [item])
            pairs.extend(zip(item["roles"], (model.ROLES[i] for i in clf.predict(data))))
    print(json.dumps(scorer.compare(pairs), indent=1))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    one = sub.add_parser("silver")
    one.add_argument("--corpus", default=os.path.expanduser("~/jd_corpus"))
    one.add_argument("--out", required=True)
    one.add_argument("--limit", type=int, default=75000)
    for name in ("cv", "train"):
        two = sub.add_parser(name)
        two.add_argument("--silver", required=True)
        two.add_argument("--cap", type=int, default=120000)
        two.add_argument("--out", default=model.WEIGHTS_FILE)
    args = parser.parse_args()
    if args.command == "silver":
        build_silver(args.corpus, args.out, args.limit)
        return
    dev = key_rows("dev")
    first = stage_one(*fit(_load_silver(args.silver, args.cap)))
    if args.command == "cv":
        cross_validate(first, dev)
        return
    clf = stage_two(first, dev)
    export(first, clf, args.out)
    _same_answers(first, clf, dev, args.out)


if __name__ == "__main__":
    main()
