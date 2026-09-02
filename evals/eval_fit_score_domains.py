#!/usr/bin/env python3
"""Fit-score evaluation across the fourteen technical domains.

Reproducible and offline: 115 hand-labelled scenarios over 29 synthetic
résumés and 30 synthetic jobs, no database, no network, no LLM.

    PYTHONPATH=shared/python python evals/eval_fit_score_domains.py

What it measures, and why each one separately
----------------------------------------------
A single "P@5 is 0.9" hides everything that matters about a scorer. Four
questions, four answers:

**Eligibility accuracy.** `eligibility` answers a different question from
`score` and must be measured on its own. Two numbers, because the two errors
cost different things: a FALSE INELIGIBLE removes a job the candidate could
have taken (silent, unrecoverable), a FALSE ELIGIBLE shows one they cannot
(visible, cheap). The first is held to a much tighter bound.

**Ranking.** P@5 and NDCG@5 per résumé, over that résumé's whole labelled
pool, ordered exactly as `tech_feed_service.rank` orders it: ineligible last,
then score. Reported as a distribution, not a mean — a mean of 0.85 across 29
résumés is consistent with one domain being 0.2. P@5 is normalised by the
number of relevant jobs actually in that résumé's pool, so a pool with three
relevant jobs is not scored out of five.

**Calibration.** Mean score per grade must be MONOTONE (grade 3 > 2 > 1 > 0)
and the gaps must be big enough to survive a tie-break. A scorer whose grade-3
mean is 71 and whose grade-0 mean is 68 is not ranking, it is jittering.

**Per component.** Every component's mean at grade 3 versus at grade 0. A
component that is flat across grades contributes nothing but noise to the
total, and a component that is INVERTED is actively wrong — and neither is
visible in the headline. `experience` is called out separately because it is
the component the brief names.
"""
from __future__ import annotations

import argparse
import collections
import json
import math
import statistics
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "shared" / "python"))
sys.path.insert(0, str(ROOT / "evals"))

from services import fit_score                                   # noqa: E402
from fixtures.fit_score_domain_cases import (                    # noqa: E402
    DOMAIN_KEYS, EXPECTED_ELIGIBILITY, JOBS, LABELS, RELEVANT_AT,
    RESUMES, SCENARIOS)

K = 5

# --- floors. Set from what the engine produces, and each one is a claim -----
#: A job the candidate CAN take must not be gated out. This is the expensive
#: error and it is held at zero.
MAX_FALSE_INELIGIBLE = 0
#: Showing a job they cannot take is visible and cheap; a small rate is
#: tolerable, a large one means the gates do not work.
MAX_FALSE_ELIGIBLE_RATE = 0.05
MIN_MEAN_PRECISION_AT_K = 0.70
MIN_MEAN_NDCG_AT_K = 0.80
#: Grade means must be strictly ordered, with room to spare.
MIN_GRADE_SEPARATION = 5.0
#: The brief's own criterion: relevant matches must score MEASURABLY above
#: irrelevant ones on the experience component, not merely on the total.
MIN_EXPERIENCE_SEPARATION = 6.0
#: A stated hard requirement must be SURFACED even when it cannot be
#: adjudicated. Silence in a résumé is `unknown`, not a mismatch — but a
#: posting that names a clearance must still say so on the card, or the rule
#: "unknown is surfaced, never priced" is only half implemented.
HARD_REQUIREMENT_JOBS: Tuple[str, ...] = ("j_security_clearance",
                                          "j_embedded_pe_license")


def _verdicts() -> Dict[Tuple[str, str], Dict[str, Any]]:
    """Score every labelled pair once. One profile build per résumé."""
    out: Dict[Tuple[str, str], Dict[str, Any]] = {}
    profiles = {rid: fit_score.build_resume_profile(r) for rid, r in RESUMES.items()}
    for rid, labels in LABELS.items():
        for jid in labels:
            out[(rid, jid)] = fit_score.score(RESUMES[rid], JOBS[jid],
                                              profile=profiles[rid])
    return out


def _dcg(grades: List[int]) -> float:
    return sum((2 ** g - 1) / math.log2(i + 2) for i, g in enumerate(grades))


def run(verbose: bool = True) -> Dict[str, Any]:
    verdicts = _verdicts()

    # --- eligibility --------------------------------------------------------
    elig_total = elig_correct = 0
    false_ineligible: List[str] = []
    false_eligible: List[str] = []
    for rid, jobs in EXPECTED_ELIGIBILITY.items():
        for jid, want in jobs.items():
            got = verdicts[(rid, jid)].get("eligibility")
            elig_total += 1
            if got == want:
                elig_correct += 1
            elif want == fit_score.ELIGIBLE:
                false_ineligible.append(f"{rid} -> {jid}")
            else:
                false_eligible.append(f"{rid} -> {jid}")

    # --- ranking ------------------------------------------------------------
    per_resume: List[Dict[str, Any]] = []
    for rid, labels in LABELS.items():
        rows = []
        for jid, grade in labels.items():
            v = verdicts[(rid, jid)]
            rows.append((jid, int(v["score"]), int(grade),
                         v.get("eligibility")))
        rows.sort(key=lambda r: (r[3] == fit_score.INELIGIBLE, -r[1], r[0]))
        top = [g for _j, _s, g, _e in rows[:K]]
        ideal = sorted((g for _j, _s, g, _e in rows), reverse=True)[:K]
        ndcg = (_dcg(top) / _dcg(ideal)) if _dcg(ideal) else 1.0
        # P@k normalised by what is ACHIEVABLE. A résumé whose labelled pool
        # holds two relevant jobs cannot exceed 2/5, and scoring it 0.4
        # measures the fixture, not the engine. The denominator is therefore
        # `min(K, relevant_in_pool)` — the number a perfect ranker would put
        # in the top K — which is the standard correction for small pools and
        # is the difference between "the scorer is at 0.58" and "the fixtures
        # are small".
        n_relevant = sum(1 for _j, _s, g, _e in rows if g >= RELEVANT_AT)
        achievable = max(1, min(K, n_relevant))
        per_resume.append({
            "resume": rid,
            "pool": len(rows),
            "relevant_in_pool": n_relevant,
            "precision_at_k": round(sum(1 for g in top if g >= RELEVANT_AT)
                                    / achievable, 4),
            "ndcg_at_k": round(ndcg, 4),
            "top": [(j, s, g) for j, s, g, _e in rows[:3]],
        })

    # --- calibration + components ------------------------------------------
    by_grade: Dict[int, List[int]] = collections.defaultdict(list)
    comp_by_grade: Dict[str, Dict[int, List[float]]] = collections.defaultdict(
        lambda: collections.defaultdict(list))
    for (rid, jid), v in verdicts.items():
        grade = int(LABELS[rid][jid])
        by_grade[grade].append(int(v["score"]))
        for comp, val in (v.get("component_scores") or {}).items():
            comp_by_grade[comp][grade].append(float(val))

    grade_means = {g: round(statistics.mean(v), 2) for g, v in sorted(by_grade.items())}
    grades_sorted = sorted(grade_means)
    monotone = all(grade_means[a] < grade_means[b]
                   for a, b in zip(grades_sorted, grades_sorted[1:]))
    separations = [round(grade_means[b] - grade_means[a], 2)
                   for a, b in zip(grades_sorted, grades_sorted[1:])]

    # How often each component had evidence on BOTH sides. A component that
    # is flat because it is almost never KNOWN is excluded from the average
    # by `_normalized_score` and costs nothing; a component that is known and
    # flat is 5 points of noise. The two look identical in a mean and are
    # completely different defects, so the rate is reported.
    known_counts: Dict[str, List[int]] = collections.defaultdict(lambda: [0, 0])
    for v in verdicts.values():
        for comp, meta in (v.get("components") or {}).items():
            known_counts[comp][1] += 1
            if meta.get("known"):
                known_counts[comp][0] += 1

    components: Dict[str, Any] = {}
    for comp, grades in sorted(comp_by_grade.items()):
        hi = statistics.mean(grades.get(3) or [0.0])
        lo = statistics.mean(grades.get(0) or [0.0])
        known_hits, known_all = known_counts.get(comp, [0, 1])
        components[comp] = {
            "mean_at_grade_3": round(hi, 2),
            "mean_at_grade_0": round(lo, 2),
            "separation": round(hi - lo, 2),
            "max": fit_score.COMPONENT_MAX.get(comp),
            "known_rate": round(known_hits / max(1, known_all), 3),
        }

    # --- hard requirements are SURFACED, not priced ------------------------
    surfaced_missing: List[str] = []
    for (rid, jid), v in verdicts.items():
        if jid not in HARD_REQUIREMENT_JOBS:
            continue
        hard = [c for c in (v.get("hard_constraints") or [])
                if c.get("hard_requirement")]
        if not hard:
            surfaced_missing.append(f"{rid} -> {jid}")

    mean_p = statistics.mean(r["precision_at_k"] for r in per_resume)
    mean_n = statistics.mean(r["ndcg_at_k"] for r in per_resume)
    exp_sep = components.get("experience", {}).get("separation", 0.0)

    summary = {
        "scenarios": len(SCENARIOS),
        "domains": len(DOMAIN_KEYS),
        "resumes": len(RESUMES),
        "jobs": len(JOBS),
        "fit_score_version": fit_score.FIT_SCORE_VERSION,
        "eligibility": {
            "asserted": elig_total,
            "accuracy": round(elig_correct / elig_total, 4) if elig_total else 1.0,
            "false_ineligible": false_ineligible,
            "false_eligible": false_eligible,
            "false_eligible_rate": round(
                len(false_eligible) / elig_total, 4) if elig_total else 0.0,
        },
        "ranking": {
            "mean_precision_at_k": round(mean_p, 4),
            "min_precision_at_k": round(min(r["precision_at_k"] for r in per_resume), 4),
            "mean_ndcg_at_k": round(mean_n, 4),
            "min_ndcg_at_k": round(min(r["ndcg_at_k"] for r in per_resume), 4),
            "per_resume": sorted(per_resume, key=lambda r: r["ndcg_at_k"]),
        },
        "calibration": {
            "mean_score_by_grade": grade_means,
            "monotone": monotone,
            "adjacent_separations": separations,
            "min_separation": min(separations) if separations else 0.0,
        },
        "components": components,
        "experience_separation": exp_sep,
        "hard_requirements_not_surfaced": surfaced_missing,
    }

    if verbose:
        e = summary["eligibility"]
        print(f"scenarios               {len(SCENARIOS)} across {len(DOMAIN_KEYS)} domains "
              f"({len(RESUMES)} résumés, {len(JOBS)} jobs)")
        print(f"scorer                  {fit_score.FIT_SCORE_VERSION}")
        print(f"\neligibility accuracy    {e['accuracy']:.3f}  ({e['asserted']} asserted)")
        print(f"  false INELIGIBLE      {len(e['false_ineligible'])}   "
              f"(target <= {MAX_FALSE_INELIGIBLE}) - removes a reachable job")
        for row in e["false_ineligible"][:8]:
            print(f"      {row}")
        print(f"  false eligible        {len(e['false_eligible'])}   "
              f"rate {e['false_eligible_rate']:.3f} "
              f"(target <= {MAX_FALSE_ELIGIBLE_RATE})")
        for row in e["false_eligible"][:8]:
            print(f"      {row}")
        r = summary["ranking"]
        print(f"\nP@{K}                    mean {r['mean_precision_at_k']:.3f}   "
              f"min {r['min_precision_at_k']:.3f}   "
              f"(target mean >= {MIN_MEAN_PRECISION_AT_K})")
        print(f"NDCG@{K}                 mean {r['mean_ndcg_at_k']:.3f}   "
              f"min {r['min_ndcg_at_k']:.3f}   "
              f"(target mean >= {MIN_MEAN_NDCG_AT_K})")
        print("  weakest résumés:")
        for row in r["per_resume"][:5]:
            print(f"      {row['resume']:<24} NDCG {row['ndcg_at_k']:.3f} "
                  f"P@{K} {row['precision_at_k']:.3f}  top: "
                  + ", ".join(f"{j}({s},g{g})" for j, s, g in row["top"]))
        c = summary["calibration"]
        print(f"\ncalibration             mean score by grade "
              + "  ".join(f"g{g}={m}" for g, m in c["mean_score_by_grade"].items()))
        print(f"  monotone              {c['monotone']}   "
              f"smallest gap {c['min_separation']} "
              f"(target >= {MIN_GRADE_SEPARATION})")
        print("\nper component           grade 3 vs grade 0 "
              "(`known` = had evidence on both sides)")
        for comp, m in sorted(summary["components"].items(),
                              key=lambda kv: -kv[1]["separation"]):
            if m["separation"] < 0:
                flag = "  <-- INVERTED"
            elif abs(m["separation"]) < 1.0:
                flag = ("  <-- never known here"
                        if m["known_rate"] < 0.1 else "  <-- KNOWN AND FLAT")
            else:
                flag = ""
            print(f"  {comp:<18} max {str(m['max']):>3}   "
                  f"g3 {m['mean_at_grade_3']:>6}   g0 {m['mean_at_grade_0']:>6}   "
                  f"sep {m['separation']:>6}   known {m['known_rate']:.2f}{flag}")
        print(f"\nexperience separation   {exp_sep}   "
              f"(target >= {MIN_EXPERIENCE_SEPARATION})")
        print(f"hard reqs not surfaced  "
              f"{len(summary['hard_requirements_not_surfaced'])}   (target 0)")
        for row in summary["hard_requirements_not_surfaced"][:5]:
            print(f"      {row}")
    return summary


def gates(summary: Dict[str, Any]) -> List[str]:
    """The failing gate names, empty when everything passes."""
    bad: List[str] = []
    if len(summary["eligibility"]["false_ineligible"]) > MAX_FALSE_INELIGIBLE:
        bad.append("false_ineligible")
    if summary["eligibility"]["false_eligible_rate"] > MAX_FALSE_ELIGIBLE_RATE:
        bad.append("false_eligible_rate")
    if summary["ranking"]["mean_precision_at_k"] < MIN_MEAN_PRECISION_AT_K:
        bad.append("mean_precision_at_k")
    if summary["ranking"]["mean_ndcg_at_k"] < MIN_MEAN_NDCG_AT_K:
        bad.append("mean_ndcg_at_k")
    if not summary["calibration"]["monotone"]:
        bad.append("calibration_monotone")
    if summary["calibration"]["min_separation"] < MIN_GRADE_SEPARATION:
        bad.append("calibration_separation")
    if summary["experience_separation"] < MIN_EXPERIENCE_SEPARATION:
        bad.append("experience_separation")
    if summary["hard_requirements_not_surfaced"]:
        bad.append("hard_requirements_surfaced")
    return bad


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default=None)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()
    summary = run(verbose=not args.quiet)
    bad = gates(summary)
    print("\nPASS" if not bad else "\nFAIL: " + ", ".join(bad))
    if args.json:
        Path(args.json).write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(f"wrote {args.json}")
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
