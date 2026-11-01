"""Score `jd_profile` against the labelled JD answer key.

    PYTHONPATH=shared/python python evals/eval_jd_answer_key.py
    PYTHONPATH=shared/python python evals/eval_jd_answer_key.py --split dev --errors sponsorship

The key is `evals/data/jd_answer_key.jsonl`: real JDs drawn from outside the set
the readers were written against, labelled blind from the text alone. The `dev`
splits may be read while changing a reader. `holdout` is the score, and reading
its errors to change a reader spends it. `review` on each row says whether a person has checked
that row's labels; a score over `draft` rows is a score against a careful reader,
not yet against a reviewed key.

Offline and deterministic: no database, no network, no model.
"""
from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "python"))

KEY_PATH = ROOT / "evals" / "data" / "jd_answer_key.jsonl"

EXACT_FIELDS = ("sponsorship", "authorization", "min_degree", "preferred_degree",
                "equivalent_experience", "min_years", "max_years",
                "clearance_required", "clearance_level", "employment_type",
                "work_mode")


def load_key(path: Path = KEY_PATH) -> List[Dict[str, Any]]:
    with open(path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def predict(text: str) -> Dict[str, Any]:
    """The pipeline's answer for one JD, in the key's vocabulary."""
    from services import jd_profile

    profile = jd_profile.build(text, "")
    eligibility = profile["work_eligibility"]
    education = profile["education"]
    return {
        "sponsorship": eligibility["sponsorship"]["status"],
        "authorization": eligibility["authorization"]["requirement"],
        "min_degree": education["min_level"],
        "preferred_degree": education["preferred_level"],
        "equivalent_experience": education["equivalent_experience_accepted"],
        "min_years": profile["experience"]["min_years"],
        "max_years": profile["experience"]["max_years"],
        "clearance_required": eligibility["clearance_required"],
        "clearance_level": eligibility["clearance_level"],
        "employment_type": profile["role"]["employment_type"],
        "work_mode": profile["role"]["work_mode"],
        "technical_skills": profile["skills"]["technical"],
        "certifications": profile["skills"]["certifications"],
    }


def _canonical_skills(names: Iterable[Any]) -> Set[str]:
    """Label spellings mapped onto lexicon names. A term the lexicon cannot name
    stays as written, so it counts as a miss rather than vanishing."""
    from services import fit_score

    out: Set[str] = set()
    for name in names or []:
        text = str(name).strip().lower()
        if text:
            found = fit_score.find_skills(text)
            out.update(found or [text])
    return out


def _same_certification(a: str, b: str) -> bool:
    a, b = a.lower().strip(), b.lower().strip()
    return bool(a and b) and (a in b or b in a)


def _tally_certifications(want: Iterable[Any], got: Iterable[Any]) -> Tuple[int, int, int]:
    want, got = [str(w) for w in want or []], [str(g) for g in got or []]
    hits = sum(any(_same_certification(w, g) for g in got) for w in want)
    extras = sum(not any(_same_certification(g, w) for w in want) for g in got)
    return hits, extras, len(want) - hits


def _precision_recall(tp: int, fp: int, fn: int) -> Tuple[Optional[float], Optional[float]]:
    return (tp / (tp + fp) if tp + fp else None, tp / (tp + fn) if tp + fn else None)


def score(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    exact: Dict[str, List[int]] = {f: [0, 0] for f in EXACT_FIELDS}
    status: Dict[str, List[int]] = collections.defaultdict(lambda: [0, 0, 0])
    skills = [0, 0, 0]
    certs = [0, 0, 0]
    errors: Dict[str, List[Dict[str, Any]]] = collections.defaultdict(list)
    for row in rows:
        labels = row["labels"]
        guess = predict(row["text"])
        for field in EXACT_FIELDS:
            exact[field][1] += 1
            if guess[field] == labels[field]:
                exact[field][0] += 1
            else:
                errors[field].append({"id": row["id"], "split": row["split"],
                                      "expected": labels[field], "got": guess[field]})
        for value in ("not_offered", "offered", "conditional"):
            said, truth = guess["sponsorship"] == value, labels["sponsorship"] == value
            status[value][0] += said and truth
            status[value][1] += said and not truth
            status[value][2] += truth and not said
        want = _canonical_skills(labels["technical_skills"])
        got = _canonical_skills(guess["technical_skills"])
        skills[0] += len(want & got)
        skills[1] += len(got - want)
        skills[2] += len(want - got)
        for i, n in enumerate(_tally_certifications(labels["certifications"],
                                                    guess["certifications"])):
            certs[i] += n

    all_right = 0
    for row in rows:
        guess = predict(row["text"])
        all_right += all(guess[f] == row["labels"][f] for f in EXACT_FIELDS)

    return {
        "rows": len(rows),
        "accuracy": {f: round(right / total, 4) if total else None
                     for f, (right, total) in exact.items()},
        "sponsorship": {
            value: dict(zip(("precision", "recall"),
                            _precision_recall(*counts)),
                        tp=counts[0], fp=counts[1], fn=counts[2])
            for value, counts in status.items()
        },
        "skills": dict(zip(("precision", "recall"), _precision_recall(*skills))),
        "certifications": dict(zip(("precision", "recall"), _precision_recall(*certs))),
        "all_fields_right": round(all_right / len(rows), 4) if rows else None,
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--split", choices=("dev", "dev2", "dev3", "dev4", "dev5", "dev6", "dev7", "tune", "holdout", "all"),
                        default="all")
    parser.add_argument("--stratum", choices=("random", "eligibility"))
    parser.add_argument("--errors", help="print the errors for this field")
    args = parser.parse_args()

    rows = load_key()
    if args.split == "tune":
        rows = [r for r in rows if r["split"] != "holdout"]
    elif args.split != "all":
        rows = [r for r in rows if r["split"] == args.split]
    if args.stratum:
        rows = [r for r in rows if r["stratum"] == args.stratum]
    if args.errors and any(r["split"] == "holdout" for r in rows):
        parser.error("--errors reads the held-out split; pass --split tune or a dev split")

    result = score(rows)
    errors = result.pop("errors")
    print(json.dumps(result, indent=1))
    if args.errors:
        for error in errors.get(args.errors, []):
            print(error)
    return 0


if __name__ == "__main__":
    sys.exit(main())
