"""Score `jd_line_model` against the blind line key.

    PYTHONPATH=shared/python python evals/eval_jd_line_key.py
    PYTHONPATH=shared/python python evals/eval_jd_line_key.py --split dev --errors

`evals/data/jd_line_key.jsonl` holds 100 random JDs, each line given one role by a
reader who saw only the guide and the posting. Twenty were read twice, which gives
the ceiling: what two careful readers agree on. `dev` may be read while changing the
model and trains it. `test` is the score, and this script refuses to print its errors.

Whole-line accuracy is reported, but tailoring cares about three narrower questions:
did we find what the job does, did we find what it asks for, and how much of what we
hand a résumé prompt is really legal or company text.

Offline and deterministic: no database, no network, no model calls.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "python"))

KEY_PATH = ROOT / "evals" / "data" / "jd_line_key.jsonl"

ROLES = ("company", "summary", "duty", "required", "preferred", "benefit", "legal",
         "logistics", "heading")
#: Lines a résumé prompt may see, and the wider truth each is judged against.
DOES = frozenset({"duty", "summary"})
ASKS = frozenset({"required", "preferred"})
USED = DOES | ASKS
#: Lines that must never reach a résumé prompt.
NEVER = frozenset({"legal", "benefit", "company"})


def load_key(path: Path = KEY_PATH, split: Optional[str] = None) -> List[Dict[str, Any]]:
    with open(path, encoding="utf-8") as handle:
        rows = [json.loads(line) for line in handle if line.strip()]
    return [r for r in rows if split is None or r["split"] == split]


def predict(lines: Sequence[str]) -> List[Optional[str]]:
    """The model's role for each line of a key row, reading it as production would."""
    from services import jd_line_model

    found = jd_line_model.read_lines("\n".join(lines))
    by_text: Dict[str, List[Optional[str]]] = {}
    for item in found:
        by_text.setdefault(item["text"], []).append(item["role"])
    # `read_lines` may re-cut a line. Each key line takes its first piece's role.
    out: List[Optional[str]] = []
    for line in lines:
        roles = by_text.get(line)
        if roles:
            out.append(roles.pop(0))
            continue
        piece = next((k for k, v in by_text.items() if v and line.startswith(k)), None)
        out.append(by_text[piece].pop(0) if piece else None)
    return out


def _f1(tp: int, fp: int, fn: int) -> Tuple[float, float, float]:
    p = tp / max(1, tp + fp)
    r = tp / max(1, tp + fn)
    return round(p, 4), round(r, 4), round(2 * p * r / max(1e-9, p + r), 4)


def _set_score(pairs: Iterable[Tuple[Any, Any]], want: frozenset, said: frozenset):
    tp = fp = fn = 0
    for truth, guess in pairs:
        tp += truth in want and guess in said
        fp += truth not in want and guess in said
        fn += truth in want and guess not in said
    return _f1(tp, fp, fn)


def compare(pairs: List[Tuple[Any, Any]]) -> Dict[str, Any]:
    """Every measure for one list of `(truth, guess)` lines."""
    total = max(1, len(pairs))
    roles = {r: _set_score(pairs, frozenset({r}), frozenset({r})) for r in ROLES}
    handed = [(t, g) for t, g in pairs if g in USED]
    return {
        "lines": len(pairs),
        "accuracy": round(sum(t == g for t, g in pairs) / total, 4),
        "macro_f1": round(sum(v[2] for v in roles.values()) / len(roles), 4),
        "roles": roles,
        # Did we find what the job does, whichever of duty or summary we called it?
        "does": _set_score(pairs, DOES, DOES),
        # Did we find what it asks for, required or preferred?
        "asks": _set_score(pairs, ASKS, ASKS),
        "required_v_preferred": round(
            sum(t == g for t, g in pairs if t in ASKS and g in ASKS)
            / max(1, sum(t in ASKS and g in ASKS for t, g in pairs)), 4),
        # Of everything a résumé prompt would see, how much must it never see?
        "leak": round(sum(t in NEVER for t, _ in handed) / max(1, len(handed)), 4),
    }


def score(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    pairs: List[Tuple[Any, Any]] = []
    for row in rows:
        pairs.extend(zip(row["roles"], predict(row["lines"])))
    return compare(pairs)


def ceiling(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """The same measures between two readers of the same posting."""
    pairs: List[Tuple[Any, Any]] = []
    for row in rows:
        if row.get("second_reader"):
            pairs.extend(zip(row["roles"], row["second_reader"]))
    return compare(pairs)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", default="test", choices=("dev", "test"))
    parser.add_argument("--errors", action="store_true")
    args = parser.parse_args()
    rows = load_key(split=args.split)
    print(json.dumps({"split": args.split, "jds": len(rows), **score(rows)}, indent=1))
    print("ceiling", json.dumps(ceiling(load_key()), indent=1))
    if args.errors:
        if args.split == "test":
            print("refusing: the test split's errors stay unread")
            return 2
        mix: Counter = Counter()
        for row in rows:
            mix.update((t, g) for t, g in zip(row["roles"], predict(row["lines"])) if t != g)
        print(mix.most_common(20))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
