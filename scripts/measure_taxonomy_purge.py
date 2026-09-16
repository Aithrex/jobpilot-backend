#!/usr/bin/env python3
"""Measure the junk-vocabulary purge against live inventory (read-only).

Compares the PRE-1.3.0 promotion clause (`len(tech_terms) >= 3` with no title
gate and no weak-term filter) against the current classifier on the same
bodies, then measures recall on genuine Analyst/Specialist/Engineer rows.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "python"))

from dotenv import load_dotenv
load_dotenv(str(ROOT / ".env"))

from services import tech_taxonomy as tx  # noqa: E402
from utils.db_connect import DBConnect  # noqa: E402

JUNK_TITLE_RE = re.compile(
    r"\b(marketer|marketing|sales\b|account\s+executive|recruiter|"
    r"elettric|electrician|nurse|pharmacy|cashier|barista|"
    r"growth\s+hacker|key\s+accounts|tecnico)\b", re.I)


def _body(doc) -> str:
    text = str(doc.get("description_text") or "")
    if len(text) >= 200:
        return text
    req = doc.get("requirements") if isinstance(doc.get("requirements"), dict) else {}
    bits = []
    for k in ("must_have_skills", "skills_required", "skills", "summary",
              "responsibilities"):
        v = req.get(k)
        if isinstance(v, list):
            bits.extend(str(x) for x in v[:40])
        elif v:
            bits.append(str(v)[:2000])
    return (text + " " + " ".join(bits)).strip()


def _old_clause_promotes(title: str, body: str) -> bool:
    """The pre-purge promotion: len(tech_terms) >= 3, no title gate."""
    terms = tx._skills_in(body)
    return len(terms) >= 3


def measure(db, sample_n: int) -> dict:
    jp = db.job_postings
    # Prefer rows that already look like the defect class: core, no family.
    cnf = list(jp.find(
        {"active": True, "tech.tech_scope": "core",
         "$or": [
             {"tech.role_families": {"$exists": False}},
             {"tech.role_families": {"$size": 0}},
             {"tech.role_families": None},
         ]},
        {"title_raw": 1, "description_text": 1, "requirements": 1,
         "department": 1, "tech": 1, "employer_name": 1},
    ).sort("last_seen_at", -1).limit(sample_n).allow_disk_use(True))

    # Also a broader core sample so we can report "of N core, how many cnf".
    core_sample = list(jp.find(
        {"active": True, "tech.tech_scope": "core"},
        {"tech.role_families": 1},
    ).sort("last_seen_at", -1).limit(sample_n).allow_disk_use(True))
    cnf_in_core = sum(
        1 for d in core_sample
        if not ((d.get("tech") or {}).get("role_families") or []))

    promoted_by_old_only = []
    stayed_both = []
    neither = []
    old_yes = new_yes = 0

    for doc in cnf:
        title = doc.get("title_raw") or ""
        body = _body(doc)
        if len(body) < 80:
            continue
        dept = doc.get("department") or ""
        old = _old_clause_promotes(title, body)
        got = tx.classify(title, description=body, department=dept)
        new = got.tech_scope == tx.SCOPE_CORE and not (got.role_families or [])
        # Also count any demotion out of core entirely:
        new_core = got.tech_scope == tx.SCOPE_CORE
        if old:
            old_yes += 1
        if new_core:
            new_yes += 1
        row = {
            "title": title,
            "employer": doc.get("employer_name"),
            "old_clause": old,
            "new_scope": got.tech_scope,
            "new_families": list(got.role_families or []),
            "strong": tx.discriminating_terms(tx._skills_in(body))[:6],
            "weak_only": [t for t in tx._skills_in(body)
                          if t not in set(tx.discriminating_terms(tx._skills_in(body)))][:6],
        }
        if old and not new_core:
            promoted_by_old_only.append(row)
        elif old and new_core and not (got.role_families or []):
            stayed_both.append(row)
        elif not old and not new_core:
            neither.append(row)

    demoted = len(promoted_by_old_only)
    stayed_cnf = len(stayed_both)
    trade = f"{demoted}:{max(stayed_cnf, 1)}"
    still_junk = [r for r in stayed_both if JUNK_TITLE_RE.search(r["title"] or "")]
    likely_genuine = [r for r in stayed_both if not JUNK_TITLE_RE.search(r["title"] or "")]

    # Recall: Analyst/Specialist/Engineer with family + real body.
    tech_rows = list(jp.find(
        {"active": True, "tech.tech_scope": "core",
         "tech.role_families.0": {"$exists": True},
         "title_raw": {"$regex": r"(Analyst|Specialist|Engineer)", "$options": "i"},
         "description_text": {"$type": "string"}},
        {"title_raw": 1, "description_text": 1, "department": 1,
         "tech": 1, "employer_name": 1},
    ).sort("last_seen_at", -1).limit(500).allow_disk_use(True))

    recall_ok = recall_miss = 0
    misses = []
    for doc in tech_rows:
        body = doc.get("description_text") or ""
        if len(body) < 300:
            continue
        title = doc.get("title_raw") or ""
        got = tx.classify(title, description=body,
                          department=doc.get("department") or "")
        if got.tech_scope == tx.SCOPE_CORE:
            recall_ok += 1
        else:
            recall_miss += 1
            misses.append({
                "title": title,
                "employer": doc.get("employer_name"),
                "scope": got.tech_scope,
                "stored_families": (doc.get("tech") or {}).get("role_families"),
            })
        if recall_ok + recall_miss >= 200:
            break

    return {
        "core_sample_n": len(core_sample),
        "core_no_family_in_core_sample": cnf_in_core,
        "cnf_rows_measured": old_yes + len(neither),
        "old_clause_would_promote_cnf": old_yes,
        "new_still_core": new_yes,
        "demoted_out_of_core_by_purge": demoted,
        "stayed_core_no_family": stayed_cnf,
        "trade_demoted_to_stayed_cnf": trade,
        "stayed_still_looks_junk_by_title": len(still_junk),
        "stayed_likely_genuine_by_title": len(likely_genuine),
        "demoted_examples": [
            {"title": r["title"], "employer": r["employer"],
             "weak_only": r["weak_only"], "strong": r["strong"]}
            for r in promoted_by_old_only[:20]
        ],
        "stayed_junk_examples": [
            {"title": r["title"], "employer": r["employer"],
             "strong": r["strong"]}
            for r in still_junk[:10]
        ],
        "stayed_genuine_examples": [
            {"title": r["title"], "employer": r["employer"],
             "strong": r["strong"]}
            for r in likely_genuine[:10]
        ],
        "recall_n": recall_ok + recall_miss,
        "recall_still_core": recall_ok,
        "recall_demoted": recall_miss,
        "recall_rate": round(recall_ok / max(1, recall_ok + recall_miss), 4),
        "recall_misses": misses[:25],
        "tighten_ambiguous_2hit": False,  # set below
        "recommendation": "",
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=800)
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    summary = measure(DBConnect().get_db(), args.sample)
    # Only tighten if recall is intact AND a large junk residual remains.
    residual = summary["stayed_still_looks_junk_by_title"]
    recall = summary["recall_rate"]
    tighten = residual >= 30 and recall >= 0.98 and summary["recall_demoted"] == 0
    # Handoff rule: false non_tech is worse. Do not tighten on residual alone
    # when stayed rows are mostly genuine.
    genuine = summary["stayed_likely_genuine_by_title"]
    if genuine > residual * 3:
        tighten = False
        summary["recommendation"] = (
            f"Do NOT tighten _AMBIGUOUS_TITLE_RE 2-hit clause. Trade "
            f"{summary['trade_demoted_to_stayed_cnf']}; stayed rows are mostly "
            f"genuine ({genuine} vs {residual} junk-looking). Recall "
            f"{summary['recall_rate']:.3f} on {summary['recall_n']} technical "
            f"Analyst/Specialist/Engineer bodies.")
    elif tighten:
        summary["recommendation"] = (
            "Residual junk remains after purge; consider tightening 2-hit.")
    else:
        summary["recommendation"] = (
            f"Leave 2-hit clause alone. Purge demoted "
            f"{summary['demoted_out_of_core_by_purge']}; recall "
            f"{summary['recall_rate']:.3f}; junk residual {residual}.")
    summary["tighten_ambiguous_2hit"] = tighten
    text = json.dumps(summary, indent=2, default=str)
    print(text)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
