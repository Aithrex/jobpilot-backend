"""Train `services/jd_field_model.py`. Needs scikit-learn, which production does not.

    PYTHONPATH=shared/python python scripts/train_jd_field_model.py cv
    PYTHONPATH=shared/python python scripts/train_jd_field_model.py train

`cv` is five folds over the tuning rows, always tested on answer-key rows only:
the disagreement rows in `evals/data/jd_field_hard_rows.jsonl` are a deliberately
hard sample, so they train and are never scored. `holdout` is never read here.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import sys
from typing import Any, Dict, List

import numpy as np
from scipy.sparse import csr_matrix, hstack
from sklearn.feature_extraction import DictVectorizer
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression

from services import jd_field_model as M
from services import jd_profile, fit_score

KEY = "evals/data/jd_answer_key.jsonl"
HARD = "evals/data/jd_field_hard_rows.jsonl"
YEARS_THRESHOLD = 0.7
VOCABULARY = int(os.environ.get("MODE_VOCABULARY", "15000"))


def read(path: str) -> List[Dict[str, Any]]:
    with open(path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle]


def rule_answers(row: Dict[str, Any]) -> None:
    """What the rules alone say, so `check_*` cannot feed on its own output."""
    blob = fit_score._norm(row["text"])
    strict = fit_score._required_years(blob, preferred_fallback=False)
    row["rule_years"] = jd_profile.read_experience(blob, strict, None)["min_years"]
    row["rule_mode"] = jd_profile.read_work_mode(blob)


def fold_of(row: Dict[str, Any]) -> int:
    return int(hashlib.md5(row["id"].encode()).hexdigest(), 16) % 5


def fit_years(rows: List[Dict[str, Any]]):
    X, y = [], []
    for row in rows:
        want = row["labels"].get("min_years")
        for mention in row["mentions"]:
            X.append(M.year_features(mention, row["rule_years"]))
            y.append(int(want is not None and float(want) == mention[0]))
    vectorizer = DictVectorizer(sparse=True)
    model = LogisticRegression(C=2, max_iter=500).fit(vectorizer.fit_transform(X), y)
    return vectorizer, model


def predict_years(vectorizer, model, row: Dict[str, Any]):
    if not row["mentions"]:
        return row["rule_years"]
    X = vectorizer.transform([M.year_features(m, row["rule_years"]) for m in row["mentions"]])
    chance = model.predict_proba(X)[:, 1]
    best = int(chance.argmax())
    return row["mentions"][best][0] if chance[best] >= YEARS_THRESHOLD else row["rule_years"]


def rule_columns(rows: List[Dict[str, Any]]) -> csr_matrix:
    return csr_matrix(np.array([[row["rule_mode"] == m for m in M.MODES] for row in rows],
                               dtype=float) * 3.0)


def fit_mode(rows: List[Dict[str, Any]]):
    vectorizer = TfidfVectorizer(ngram_range=(1, 3), min_df=2, sublinear_tf=True,
                                 max_features=VOCABULARY, token_pattern=r"\b\w\w+\b")
    text = vectorizer.fit_transform([row["mode_text"] for row in rows])
    labels = [row["labels"].get("work_mode", "unknown") for row in rows]
    model = LogisticRegression(C=30, max_iter=600).fit(hstack([text, rule_columns(rows)]).tocsr(),
                                                       labels)
    return vectorizer, model


def predict_mode(vectorizer, model, rows: List[Dict[str, Any]]) -> List[str]:
    text = vectorizer.transform([row["mode_text"] for row in rows])
    return list(model.predict(hstack([text, rule_columns(rows)]).tocsr()))


def prepare(rows: List[Dict[str, Any]]) -> None:
    for row in rows:
        rule_answers(row)
        row["mentions"] = M.year_mentions(row["text"])
        row["mode_text"] = M.mode_text(row["text"])


def cross_validate(key: List[Dict[str, Any]], hard: List[Dict[str, Any]]) -> None:
    years, modes = {}, {}
    for k in range(5):
        train = [r for r in key if fold_of(r) != k] + hard
        test = [r for r in key if fold_of(r) == k]
        vectorizer, model = fit_years(train)
        for row in test:
            years[row["id"]] = predict_years(vectorizer, model, row)
        vectorizer, model = fit_mode(train)
        for row, mode in zip(test, predict_mode(vectorizer, model, test)):
            modes[row["id"]] = mode
    same = lambda a, b: (a is None and b is None) or (a is not None and b is not None
                                                      and float(a) == float(b))
    n = len(key)
    print("years rule %.4f model %.4f" % (
        sum(same(r["labels"]["min_years"], r["rule_years"]) for r in key) / n,
        sum(same(r["labels"]["min_years"], years[r["id"]]) for r in key) / n))
    print("mode  rule %.4f model %.4f" % (
        sum(r["labels"]["work_mode"] == r["rule_mode"] for r in key) / n,
        sum(r["labels"]["work_mode"] == modes[r["id"]] for r in key) / n))


def export(key: List[Dict[str, Any]], hard: List[Dict[str, Any]], out: str) -> None:
    rows = key + hard
    vectorizer, model = fit_years(rows)
    weights = {f: round(float(w), 5) for f, w in zip(vectorizer.get_feature_names_out(),
                                                     model.coef_[0]) if abs(w) > 1e-4}
    years = {"weights": weights, "bias": float(model.intercept_[0]),
             "threshold": YEARS_THRESHOLD}
    text_vectorizer, mode_model = fit_mode(rows)
    size = len(text_vectorizer.vocabulary_)
    assert list(mode_model.classes_) == list(M.MODES)
    mode = {
        "vocabulary": {g: int(i) for g, i in text_vectorizer.vocabulary_.items()},
        "idf": [round(float(v), 5) for v in text_vectorizer.idf_],
        "weights": [[round(float(v), 4) for v in row[:size]] for row in mode_model.coef_],
        "rule_weights": [[round(float(v), 5) for v in row[size:]] for row in mode_model.coef_],
        "bias": [float(v) for v in mode_model.intercept_],
    }
    with gzip.open(out, "wt", encoding="utf-8", compresslevel=9) as handle:
        json.dump({"version": "fields-1.0.0", "rows": len(rows), "years": years, "mode": mode},
                  handle, separators=(",", ":"))
    M._model = None
    sklearn_mode = predict_mode(text_vectorizer, mode_model, rows)
    wrong = sum(M.check_work_mode(r["text"], r["rule_mode"]) != m
                for r, m in zip(rows, sklearn_mode))
    wrong_years = 0
    for row in rows:
        ours = M.check_years(row["text"], row["rule_years"])
        theirs = predict_years(vectorizer, model, row)
        wrong_years += (ours is None) != (theirs is None) or (
            ours is not None and float(ours) != float(theirs))
    print("exported", out, os.path.getsize(out), "bytes; disagree with sklearn:",
          "mode", wrong, "years", wrong_years, "of", len(rows))
    if wrong > len(rows) * 0.002 or wrong_years > len(rows) * 0.002:
        sys.exit("exported weights do not match the trained model")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("action", choices=("cv", "train"))
    parser.add_argument("--out", default=M.WEIGHTS_FILE)
    args = parser.parse_args()
    key = [r for r in read(KEY) if r["split"] != "holdout"]
    hard = read(HARD)
    prepare(key)
    prepare(hard)
    print("key rows", len(key), "hard rows", len(hard), flush=True)
    if args.action == "cv":
        cross_validate(key, hard)
    else:
        export(key, hard, args.out)


if __name__ == "__main__":
    main()
