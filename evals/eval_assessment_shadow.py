"""Shadow measurement + live-evaluation harness (Package 7).

Offline by default. Live Bedrock runs only when EVAL_LIVE_LLM=1 and credentials
exist. Does not fabricate scores. Human multi-profession review is documented
as a blocker when access is unavailable.
"""
from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SHARED = str(_REPO_ROOT / "shared" / "python")
if _SHARED not in sys.path:
    sys.path.insert(0, _SHARED)

from services import assessment_service as assess
from services.requirement_assessment import evaluate_requirements, attach_expressions


@dataclass
class ShadowSample:
    sample_id: str
    profession: str  # software | operations | finance | sales | career_change | early_career
    split: str  # calibration | holdout
    resume: Dict[str, Any]
    jd: Dict[str, Any]


@dataclass
class ShadowResult:
    sample_id: str
    profession: str
    split: str
    latency_ms: float
    schema_version: Optional[int]
    overall_is_null: bool
    required_counts: Dict[str, int]
    stage_processing: str
    stage_reader: str
    error: str = ""
    findings_count: int = 0


def builtin_samples() -> List[ShadowSample]:
    """Small multi-profession offline cohort (not a statistical pilot)."""
    software = ShadowSample(
        "sw-1", "software", "calibration",
        {
            "contact": {"name": "A", "email": "a@x.com"},
            "summary": "Backend engineer",
            "skills": {"S": ["Python", "SQL"]},
            "experience": [{"company": "Acme", "title": "Eng", "dates": "2022 - Present",
                            "bullets": ["Built APIs in Python", "Wrote SQL queries"]}],
            "education": [], "projects": [], "certifications": [],
        },
        attach_expressions({
            "job_title": "Backend Engineer",
            "required_skills": ["Python", "SQL"],
            "requirements": [{"text": "Python or Java, plus SQL", "priority": "required",
                              "kind": "skill", "alternatives": ["Python", "Java"],
                              "source_excerpt": "Python or Java, plus SQL"}],
        }),
    )
    nurse = ShadowSample(
        "hc-1", "operations", "calibration",
        {
            "contact": {"name": "B", "email": "b@x.com"},
            "summary": "Registered nurse",
            "skills": {"Clinical": ["Bedside care"]},
            "experience": [{"company": "Clinic", "title": "RN", "dates": "2021 - Present",
                            "bullets": ["Provided bedside care", "Precepted new graduates"]}],
            "education": [], "projects": [], "certifications": [],
        },
        {"job_title": "RN", "required_skills": [], "requirements": [
            {"text": "bedside care", "priority": "required", "kind": "responsibility",
             "alternatives": [], "source_excerpt": "bedside care"}]},
    )
    sales = ShadowSample(
        "sl-1", "sales", "holdout",
        {
            "contact": {"name": "C", "email": "c@x.com"},
            "summary": "Account executive",
            "skills": {"S": ["Salesforce"]},
            "experience": [{"company": "Co", "title": "AE", "dates": "2020 - Present",
                            "bullets": ["Closed enterprise deals using Salesforce"]}],
            "education": [], "projects": [], "certifications": [],
        },
        {"job_title": "AE", "required_skills": ["Salesforce"], "requirements": []},
    )
    early = ShadowSample(
        "ec-1", "early_career", "holdout",
        {
            "contact": {"name": "D", "email": "d@x.com"},
            "summary": "",
            "skills": {"S": ["Excel"]},
            "experience": [{"company": "InternCo", "title": "Intern", "dates": "2024",
                            "bullets": ["Built Excel models for finance"]}],
            "education": [{"degree": "B.S.", "institution": "State", "dates": "2024"}],
            "projects": [], "certifications": [],
        },
        {"job_title": "Analyst", "required_skills": ["Excel"], "requirements": []},
    )
    return [software, nurse, sales, early]


def run_shadow_sample(sample: ShadowSample, *, use_v2: bool = True) -> ShadowResult:
    t0 = time.perf_counter()
    err = ""
    payload: Dict[str, Any] = {}
    try:
        if use_v2:
            # Detached offline evaluation — no flag required for harness measurement
            text_parts = [str(sample.resume.get("summary") or "")]
            for exp in sample.resume.get("experience") or []:
                text_parts.extend(exp.get("bullets") or [])
            from schemas.resume_schemas import flatten_skills
            text_parts.extend(flatten_skills(sample.resume))
            text = "\n".join(text_parts)
            payload = evaluate_requirements(
                document_text=text, jd=sample.jd, resume=sample.resume,
                source_resume=sample.resume,
            )
            # Wrap as assessment-shaped for stage fields
            wrapped = assess.score_payload_v2(
                tailored_resume=sample.resume,
                jd_analysis=sample.jd,
                fmt="pdf",
                artifact_bytes=None,
            )
            payload = {**wrapped, "counts": payload.get("counts") or wrapped.get("counts")}
        else:
            payload = evaluate_requirements(
                document_text="", jd=sample.jd, resume=sample.resume,
            )
    except Exception as exc:
        err = str(exc)
    ms = (time.perf_counter() - t0) * 1000
    counts = (payload.get("counts") or {}).get("required") or {}
    stages = payload.get("stages") or {}
    return ShadowResult(
        sample_id=sample.sample_id,
        profession=sample.profession,
        split=sample.split,
        latency_ms=ms,
        schema_version=payload.get("schema_version"),
        overall_is_null=payload.get("overall") is None,
        required_counts={k: int(counts.get(k) or 0) for k in (
            "supported_and_satisfied", "partial_or_related", "not_demonstrated",
            "conflict", "unknown",
        )},
        stage_processing=str(stages.get("processing") or ""),
        stage_reader=str(stages.get("reader") or ""),
        error=err,
        findings_count=len(payload.get("findings") or []),
    )


def summarize(results: List[ShadowResult]) -> Dict[str, Any]:
    ok = [r for r in results if not r.error]
    return {
        "n": len(results),
        "ok": len(ok),
        "errors": [r.sample_id for r in results if r.error],
        "all_overall_null": all(r.overall_is_null for r in ok) if ok else False,
        "professions": sorted({r.profession for r in results}),
        "splits": {
            "calibration": sum(1 for r in results if r.split == "calibration"),
            "holdout": sum(1 for r in results if r.split == "holdout"),
        },
        "p95_latency_ms": sorted(r.latency_ms for r in ok)[int(0.95 * (len(ok) - 1))] if ok else None,
        "mean_latency_ms": (sum(r.latency_ms for r in ok) / len(ok)) if ok else None,
    }


def write_report(results: List[ShadowResult], path: Path) -> Dict[str, Any]:
    summary = summarize(results)
    live = os.environ.get("EVAL_LIVE_LLM", "").lower() in {"1", "true", "on"}
    go_nogo = {
        "decision": "no-go",
        "reasons": [],
        "blockers": [],
    }
    if not summary["all_overall_null"] and summary["ok"]:
        go_nogo["reasons"].append("One or more samples emitted non-null overall")
    if summary["errors"]:
        go_nogo["reasons"].append(f"Errors on samples: {summary['errors']}")
    if summary["ok"] == summary["n"] and summary["all_overall_null"]:
        go_nogo["reasons"].append("Offline integrity checks passed on builtin cohort")
    # Live / human review blockers (honest)
    go_nogo["blockers"].append(
        "Multi-profession live ≥60-pair pilot with two blinded human reviewers "
        "was NOT run in this environment."
    )
    if not live:
        go_nogo["blockers"].append(
            "EVAL_LIVE_LLM is off; no paid Bedrock shadow comparison was executed."
        )
    else:
        go_nogo["blockers"].append(
            "Even with EVAL_LIVE_LLM, consented multi-resume corpus and human "
            "reviewers are not available in this automation session."
        )
    go_nogo["decision"] = "no-go"  # production enable requires clearing blockers
    if summary["ok"] == summary["n"] and summary["all_overall_null"] and not live:
        go_nogo["staging_recommendation"] = (
            "Offline cohort OK for continued staging with flag still default-off; "
            "do not expand production cohort until live/human gates clear."
        )

    doc = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "flag_default": "RESUME_ASSESSMENT_V2_ENABLED=false",
        "summary": summary,
        "results": [asdict(r) for r in results],
        "go_nogo": go_nogo,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    return doc


def main():
    samples = builtin_samples()
    results = [run_shadow_sample(s) for s in samples]
    out = Path(__file__).resolve().parents[1] / "docs" / "plans" / "2026-09-05-ats-shadow-results.json"
    doc = write_report(results, out)
    print(json.dumps(doc["summary"], indent=2))
    print("go_nogo:", doc["go_nogo"]["decision"])
    print("wrote", out)


if __name__ == "__main__":
    main()
