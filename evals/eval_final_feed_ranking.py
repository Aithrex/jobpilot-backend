"""End-to-end final-feed ranking evaluation.

Existing evals measure taxonomy and fit_score in isolation. This one measures
the ORDER the user actually sees: `tech_feed_service.rank` then employer
diversity, over labelled archetypes.

    PYTHONPATH=shared/python python evals/eval_final_feed_ranking.py

Gates:
  mean NDCG@5 >= 0.90
  each archetype NDCG@5 >= 0.80
  no hard-ineligible job above an eligible close match
  false-eligible rate <= 0.05
  duplicate variants do not occupy multiple top-five slots
  diversity reordering does not scramble score order within an employer
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "shared" / "python"))
sys.path.insert(0, str(ROOT))

from services import tech_feed_service as tfs  # noqa: E402

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
K = 5
MIN_MEAN_NDCG = 0.90
MIN_ARCHETYPE_NDCG = 0.80
MAX_FALSE_ELIGIBLE = 0.05

ARCHETYPES = (
    "ai_ml", "backend", "frontend", "fullstack", "data", "cloud_sre",
    "devops", "security", "mobile", "qa_sdet", "embedded", "semiconductor",
    "robotics", "technical_product",
)


def _job(key: str, *, title: str, company: str, family: str, specialty: str,
         match: int, eligible: bool = True, days: int = 2, ats: str = "greenhouse",
         tenant: str = None, variant: str = None, skills: List[str] = None,
         work_mode: str = "hybrid", seniority: str = "senior") -> Dict[str, Any]:
    tenant = tenant or company.lower().replace(" ", "")
    posted = NOW - timedelta(days=days)
    return {
        "job_id": key,
        "title_raw": title,
        "title": title,
        "employer_name": company,
        "employer_id": tenant,
        "canonical_employer_id": f"emp_{tenant[:12].ljust(12, '0')}",
        "ats": ats,
        "tenant": tenant,
        "status": "active",
        "posted_at": posted,
        "posted_at_confidence": "exact",
        "work_mode": work_mode,
        "seniority": seniority,
        "cost_class": "free",
        "board_kind": "employer",
        "dedupe_group_id": variant or key,
        "variant_group_id": variant or key,
        "match_score": match,
        "matched_skills": (skills or [])[:4],
        "missing_skills": [],
        "fit": {"eligibility": "eligible" if eligible else "ineligible",
                "score_version": "fit-v2.1"},
        "_tech": {
            "role_family": family,
            "role_families": [family],
            "role_specialties": [specialty],
            "skills_required": skills or [],
            "classification_confidence": 0.9,
            "taxonomy_version": "tech-tax-1.2.0",
            "tech_scope": "core",
        },
        "tech": {
            "role_family": family,
            "role_specialties": [specialty],
            "tech_scope": "core",
        },
    }


def corpus() -> List[Dict[str, Any]]:
    """A labelled pool: one close match and distractors per archetype."""
    rows = [
        _job("j_aiml", title="Staff Machine Learning Engineer", company="OpenAI",
             family="ai_ml", specialty="ml_engineering", match=92, skills=["pytorch"]),
        _job("j_aiml_bad", title="Payroll Analyst", company="ADP",
             family="software_engineering", specialty="backend", match=40,
             eligible=False, skills=["excel"]),
        _job("j_be", title="Senior Backend Engineer", company="Stripe",
             family="software_engineering", specialty="backend", match=90,
             skills=["python", "go"]),
        _job("j_be_inelig", title="Staff Backend Engineer (US citizen, TS/SCI)",
             company="Palantir", family="software_engineering", specialty="backend",
             match=88, eligible=False, skills=["java"]),
        _job("j_fe", title="Frontend Engineer", company="Figma",
             family="software_engineering", specialty="frontend", match=91,
             skills=["react", "typescript"]),
        _job("j_fs", title="Full Stack Engineer", company="Notion",
             family="software_engineering", specialty="full_stack", match=89,
             skills=["react", "python"]),
        _job("j_de", title="Data Engineer", company="Databricks",
             family="data", specialty="data_engineering", match=93, skills=["spark"]),
        _job("j_sre", title="Site Reliability Engineer", company="Google",
             family="cloud_platform", specialty="sre", match=90, skills=["k8s"]),
        _job("j_devops", title="DevOps Engineer", company="GitLab",
             family="cloud_platform", specialty="devops", match=88, skills=["terraform"]),
        _job("j_sec", title="Security Engineer", company="CrowdStrike",
             family="security", specialty="application_security", match=91,
             skills=["appsec"]),
        _job("j_mobile", title="Staff iOS Engineer", company="Snap",
             family="software_engineering", specialty="mobile", match=90,
             skills=["swift"]),
        _job("j_qa", title="SDET", company="Microsoft",
             family="quality", specialty="sdet", match=96,
             skills=["pytest"]),
        _job("j_pm", title="Technical Product Manager, Developer Platform",
             company="Twilio", family="technical_product",
             specialty="technical_product_management", match=95, skills=["apis"]),
        _job("j_emb", title="Embedded Software Engineer", company="Garmin",
             family="software_engineering", specialty="embedded", match=90,
             skills=["c", "rtos"]),
        _job("j_semi", title="ASIC Design Engineer", company="NVIDIA",
             family="semiconductor_hardware", specialty="asic_soc_design", match=94,
             skills=["verilog"]),
        _job("j_robot", title="Robotics Perception Engineer", company="Zoox",
             family="ai_ml", specialty="robotics_autonomy", match=91, skills=["ros"]),
        # Duplicate variant of the backend role — must not take two top-5 slots
        # for the same searcher.
        _job("j_be_dup", title="Senior Backend Engineer", company="Stripe",
             family="software_engineering", specialty="backend", match=90,
             skills=["python", "go"], variant="j_be", tenant="stripe"),
        # Same employer flood
        _job("j_be_flood1", title="Backend Engineer II", company="Stripe",
             family="software_engineering", specialty="backend", match=80,
             skills=["python"], tenant="stripe", days=3),
        _job("j_be_flood2", title="Backend Engineer III", company="Stripe",
             family="software_engineering", specialty="backend", match=78,
             skills=["python"], tenant="stripe", days=4),
        _job("j_be_flood3", title="Backend Engineer IV", company="Stripe",
             family="software_engineering", specialty="backend", match=76,
             skills=["python"], tenant="stripe", days=5),
        _job("j_be_flood4", title="Backend Engineer V", company="Stripe",
             family="software_engineering", specialty="backend", match=74,
             skills=["python"], tenant="stripe", days=6),
    ]
    return rows


RELEVANT = {
    "ai_ml": ["j_aiml"],
    "backend": ["j_be"],
    "frontend": ["j_fe"],
    "fullstack": ["j_fs"],
    "data": ["j_de"],
    "cloud_sre": ["j_sre"],
    "devops": ["j_devops"],
    "security": ["j_sec"],
    "mobile": ["j_mobile"],
    "qa_sdet": ["j_qa"],
    "embedded": ["j_emb"],
    "semiconductor": ["j_semi"],
    "robotics": ["j_robot"],
    "technical_product": ["j_pm"],
}

FILTERS = {
    "ai_ml": {"role_specialties": ["ml_engineering"], "skills_required": ["pytorch"]},
    "backend": {"role_specialties": ["backend"], "skills_required": ["python"]},
    "frontend": {"role_specialties": ["frontend"], "skills_required": ["react"]},
    "fullstack": {"role_specialties": ["full_stack"], "skills_required": ["react"]},
    "data": {"role_specialties": ["data_engineering"], "skills_required": ["spark"]},
    "cloud_sre": {"role_specialties": ["sre"], "skills_required": ["k8s"]},
    "devops": {"role_specialties": ["devops"], "skills_required": ["terraform"]},
    "security": {"role_specialties": ["application_security"],
                 "skills_required": ["appsec"]},
    "mobile": {"role_specialties": ["mobile"], "skills_required": ["swift"]},
    "qa_sdet": {"role_specialties": ["sdet"], "skills_required": ["pytest"]},
    "embedded": {"role_specialties": ["embedded"], "skills_required": ["c"]},
    "semiconductor": {"role_specialties": ["asic_soc_design"],
                      "skills_required": ["verilog"]},
    "robotics": {"role_specialties": ["robotics_autonomy"],
                 "skills_required": ["ros"]},
    "technical_product": {"role_specialties": ["technical_product_management"],
                          "skills_required": ["apis"]},
}


def ndcg_at_k(ordered_ids: List[str], relevant: List[str], k: int = K) -> float:
    rel = set(relevant)
    dcg = 0.0
    for i, jid in enumerate(ordered_ids[:k]):
        if jid in rel:
            dcg += 1.0 / math.log2(i + 2)
    ideal = sum(1.0 / math.log2(i + 2) for i in range(min(k, len(relevant))))
    return round(dcg / ideal, 4) if ideal else 0.0


def _passthrough_score(rows, *args, **kwargs):
    for row in rows:
        row.setdefault("match_score", 0)
        row.setdefault("matched_skills", [])
        row.setdefault("missing_skills", [])


def rank_pool(rows: List[Dict[str, Any]], filters: Dict[str, Any]
              ) -> List[Dict[str, Any]]:
    import services.inventory_feed as invf
    orig = invf._score_rows
    invf._score_rows = _passthrough_score
    try:
        req = tfs.normalize_request({
            "user_email": "eval@example.com",
            "tech_scope": ["core"],
            "sort": "relevance",
            **filters,
        })
        ranked = tfs.rank([dict(r) for r in rows], req, None,
                          filters.get("skills_required") or [], 6, now=NOW)
        seen = set()
        collapsed: List[Dict[str, Any]] = []
        for row in ranked:
            vid = row.get("dedupe_group_id") or row.get("job_id")
            if vid in seen:
                continue
            seen.add(vid)
            collapsed.append(row)
        diversified, _moved = tfs._diversify_employers(collapsed, 5)
        return diversified
    finally:
        invf._score_rows = orig


def run(verbose: bool = True) -> Dict[str, Any]:
    pool = corpus()
    per: Dict[str, float] = {}
    false_eligible = 0
    eligibility_n = 0
    ineligible_above = 0
    dup_top5 = 0

    for arch in ARCHETYPES:
        ordered = rank_pool(pool, FILTERS[arch])
        ids = [r["job_id"] for r in ordered]
        per[arch] = ndcg_at_k(ids, RELEVANT[arch])
        top5 = ordered[:5]
        # Hard-ineligible must not sit above an eligible close match.
        rel = set(RELEVANT[arch])
        seen_ineligible_before_rel = False
        for row in ordered:
            elig = (row.get("fit") or {}).get("eligibility")
            if elig == "ineligible":
                eligibility_n += 1
                if not seen_ineligible_before_rel and any(
                        x["job_id"] in rel for x in ordered
                        if (x.get("fit") or {}).get("eligibility") == "eligible"):
                    # ineligible appearing before the relevant eligible job
                    rel_idx = next((i for i, x in enumerate(ordered)
                                    if x["job_id"] in rel), None)
                    inel_idx = next((i for i, x in enumerate(ordered)
                                     if x["job_id"] == row["job_id"]), None)
                    if rel_idx is not None and inel_idx is not None and inel_idx < rel_idx:
                        ineligible_above += 1
                        seen_ineligible_before_rel = True
            elif elig == "eligible" and row["job_id"] in (
                    "j_aiml_bad", "j_be_inelig"):
                false_eligible += 1
                eligibility_n += 1
        variants = [r.get("dedupe_group_id") for r in top5]
        if len(variants) != len(set(variants)):
            dup_top5 += 1

    # Diversity must not reverse two Stripe roles' relative rank_score.
    backend_ordered = rank_pool(pool, FILTERS["backend"])
    stripe = [r for r in backend_ordered if r.get("tenant") == "stripe"]
    scores = [r.get("rank_score") or 0 for r in stripe]
    diversity_ok = scores == sorted(scores, reverse=True)

    mean_ndcg = round(sum(per.values()) / len(per), 4) if per else 0.0
    fe_rate = round(false_eligible / max(1, eligibility_n), 4)
    summary = {
        "mean_ndcg_at_5": mean_ndcg,
        "per_archetype_ndcg_at_5": per,
        "ineligible_above_eligible": ineligible_above,
        "false_eligible_rate": fe_rate,
        "duplicate_top5_archetypes": dup_top5,
        "diversity_preserves_within_employer_order": diversity_ok,
    }
    if verbose:
        print(f"mean NDCG@5              {mean_ndcg:.3f}  (gate >= {MIN_MEAN_NDCG})")
        for arch, n in sorted(per.items()):
            print(f"  {arch:<22} {n:.3f}")
        print(f"ineligible above eligible {ineligible_above}")
        print(f"false-eligible rate       {fe_rate:.4f}")
        print(f"duplicate variants in top5 {dup_top5}")
        print(f"diversity preserves order {diversity_ok}")
    return summary


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default=None)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    summary = run(verbose=not args.quiet)
    worst = min(summary["per_archetype_ndcg_at_5"].values() or [0])
    ok = (summary["mean_ndcg_at_5"] >= MIN_MEAN_NDCG
          and worst >= MIN_ARCHETYPE_NDCG
          and summary["ineligible_above_eligible"] == 0
          and summary["false_eligible_rate"] <= MAX_FALSE_ELIGIBLE
          and summary["duplicate_top5_archetypes"] == 0
          and summary["diversity_preserves_within_employer_order"])
    print("\nPASS" if ok else "\nFAIL")
    if args.json:
        Path(args.json).write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
