"""Measure the guest matching path against whatever inventory is configured.

Answers the only question that matters after the ADR-003 cutover: how long does
a guest wait? Runs the REAL retrieval and the REAL scorer, so the number is the
server-side cost of `POST /api/jobs/guest/matches` minus HTTP and Lambda init.

    python scripts/guest_match_bench.py
    python scripts/guest_match_bench.py --runs 5

Read-only: it queries `job_postings` and scores in memory. Nothing is written.
"""
from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "shared" / "python"))

try:
    from dotenv import load_dotenv

    load_dotenv(_ROOT / ".env")
except Exception:
    pass

RESUMES = {
    "cloud engineer": {
        "name": "Bench Candidate",
        "skills": ["Python", "AWS", "Kubernetes", "Terraform", "Docker", "Linux", "CI/CD"],
        "experience": [{
            "title": "Cloud Engineer", "company": "Acme",
            "start_date": "2020-06", "end_date": "2025-06",
            "description": ("Built Python services on AWS with Kubernetes, Terraform "
                            "and Docker. Ran CI/CD pipelines on Linux."),
        }],
        "education": [{"degree": "BS Computer Science", "school": "State University"}],
        "projects": [], "certifications": [],
    },
    "registered nurse": {
        "name": "Bench Candidate",
        "skills": ["ICU", "ACLS", "BLS", "Patient Care", "Triage"],
        "experience": [{
            "title": "Registered Nurse", "company": "General Hospital",
            "start_date": "2019-01", "end_date": "2025-01",
            "description": "ICU nurse. ACLS and BLS certified. Triage and patient care.",
        }],
        "education": [{"degree": "BSN Nursing", "school": "State University"}],
        "projects": [], "certifications": [],
    },
    "sales": {
        "name": "Bench Candidate",
        "skills": ["Salesforce", "Prospecting", "Negotiation", "CRM"],
        "experience": [{
            "title": "Account Executive", "company": "SaaSCo",
            "start_date": "2019-03", "end_date": "2025-03",
            "description": "Closed enterprise SaaS deals. Salesforce CRM, prospecting.",
        }],
        "education": [], "projects": [], "certifications": [],
    },
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=3)
    args = ap.parse_args()

    from services import fit_score, guest_service

    print(f"{'resume':<20} {'families':<34} {'cands':>6} {'scored':>7} "
          f"{'>=40':>5} {'fetch':>8} {'score':>8} {'total':>8}")
    print("-" * 104)

    for label, resume in RESUMES.items():
        fetch_ms, score_ms, rows, qualified = [], [], 0, 0
        families = []
        for _ in range(max(1, args.runs)):
            t0 = time.perf_counter()
            got, report = guest_service._fetch_from_inventory(
                resume, None, resume_skills=resume["skills"],
                resume_years=5, tz_offset_minutes=-420)
            t1 = time.perf_counter()

            profile = fit_score.build_resume_profile(resume)
            verdicts = [
                fit_score.score_one(
                    jd_text="", title=str(r.get("title") or ""),
                    resume_skills=resume["skills"], profile=profile,
                    location=str(r.get("location") or ""),
                    job={"requirements": r.get("requirements")},
                )
                for r in got
            ]
            t2 = time.perf_counter()

            fetch_ms.append((t1 - t0) * 1000)
            score_ms.append((t2 - t1) * 1000)
            rows = len(got)
            qualified = sum(1 for v in verdicts if v["match_score"] >= 40)
            families = report.get("role_families") or []

        f = statistics.median(fetch_ms)
        s = statistics.median(score_ms)
        fam = ",".join(families)[:32]
        print(f"{label:<20} {fam:<34} {rows:>6} {rows:>7} {qualified:>5} "
              f"{f:>7.0f}ms {s:>7.0f}ms {f + s:>7.0f}ms")

    print()
    print("fetch = one indexed Mongo query (network-bound; in-region Lambda is faster)")
    print("score = deterministic ranking over every candidate, no LLM, no JD fetch")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
