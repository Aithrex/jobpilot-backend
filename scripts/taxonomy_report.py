"""Measure `fit_score.classify_role` coverage against REAL inventory titles.

Recall on this classifier is not cosmetic. `role_family` is the retrieval key
for both the guest path and the feed tabs, so a title the classifier cannot
read is a job no user can reach, no matter how well they match it.

Coverage alone is the wrong target though - "classify everything" is trivially
achievable and actively harmful, because a posting filed under the wrong family
surfaces to the wrong person. So this prints BOTH:

  * coverage        - what fraction of live titles classify at all;
  * per-family samples - so a human can eyeball whether the new matches are
                      right, which is the only real check on precision here.

    python scripts/taxonomy_report.py                # coverage + samples
    python scripts/taxonomy_report.py --family sales # audit one family
    python scripts/taxonomy_report.py --unmatched 60 # what still fails

Read-only.
"""
from __future__ import annotations

import argparse
import collections
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "shared" / "python"))

try:
    from dotenv import load_dotenv

    load_dotenv(_ROOT / ".env")
except Exception:
    pass

CACHE = _ROOT / "scripts" / ".titles_cache.txt"


def load_titles(limit: int = 40000, refresh: bool = False) -> list:
    """Distinct live titles. Cached so an iteration loop does not re-query."""
    if CACHE.exists() and not refresh:
        return [t for t in CACHE.read_text(encoding="utf-8").splitlines() if t.strip()]

    from utils.db_connect import DBConnect

    db = DBConnect().get_db()
    seen = []
    uniq = set()
    for doc in db.job_postings.find({}, {"title_raw": 1, "_id": 0}).limit(limit):
        title = (doc.get("title_raw") or "").strip()
        key = title.lower()
        if title and key not in uniq:
            uniq.add(key)
            seen.append(title)
    CACHE.write_text("\n".join(seen), encoding="utf-8")
    return seen


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true", help="re-query titles")
    ap.add_argument("--family", help="show every title matched into this family")
    ap.add_argument("--unmatched", type=int, default=30)
    ap.add_argument("--samples", type=int, default=4)
    args = ap.parse_args()

    from services import fit_score

    titles = load_titles(refresh=args.refresh)
    if not titles:
        print("no titles cached; run once with a reachable database")
        return 1

    by_family = collections.defaultdict(list)
    unmatched = []
    for title in titles:
        fam = fit_score.classify_role(title)
        if fam:
            by_family[fam].append(title)
        else:
            unmatched.append(title)

    matched = len(titles) - len(unmatched)
    print(f"distinct titles : {len(titles)}")
    print(f"classified      : {matched}  ({matched / len(titles) * 100:.1f}%)")
    print(f"unclassified    : {len(unmatched)}")
    print()

    if args.family:
        rows = by_family.get(args.family) or []
        print(f"--- {args.family}: {len(rows)} titles ---")
        for t in rows[:400]:
            print("  ", t[:90])
        return 0

    print(f"{'family':<24} {'n':>6}   samples")
    for fam, rows in sorted(by_family.items(), key=lambda kv: -len(kv[1])):
        sample = " | ".join(t[:34] for t in rows[:args.samples])
        print(f"  {fam:<22} {len(rows):>6}   {sample}")

    print()
    print(f"--- still unclassified (top tokens) ---")
    counter = collections.Counter()
    for title in unmatched:
        for word in set(re.sub(r"[^a-z ]", " ", title.lower()).split()):
            if len(word) > 3:
                counter[word] += 1
    print("  " + ", ".join(f"{w}({n})" for w, n in counter.most_common(24)))
    print()
    for title in unmatched[:args.unmatched]:
        print("   ", title[:88])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
