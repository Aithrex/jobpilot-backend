"""Claude-backed reader for assessment-v2 (Package 5 completion).

Uses the existing claude_json provider. Quote-backed findings must appear in
the document; absence findings use search_scope (never fabricated quotes).
Failures return explicit partial/unavailable — never fake defaults.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Sequence

from schemas.assessment_contracts import (
    AssessmentFinding,
    FindingReference,
    RequirementEvaluation,
)
from services.assessment_comparison import (
    absence_finding,
    build_findings_from_evaluations,
    derive_top_actions,
    detect_coverage_bloat,
    retention_findings,
)
from services.assessment_policy import severity_for
from services.artifact_assessment import token_boundary_match
from services.tailoring_policy import REVIEW_RULES
from utils.llm_safety import guarded_system, wrap_untrusted, redact_pii_for_model

logger = logging.getLogger(__name__)

_V2_READER_RULES = REVIEW_RULES + """
Additionally for assessment-v2:
- Prefer findings of kinds: strength, wording, clarity, absence, relationship, integrity.
- For absence, do NOT invent a resume quote; leave quote empty and name the missing requirement.
- Call out unnatural keyword stuffing or coverage bloat when present.
- Note when concrete accomplishments appear diluted or lost versus clear original wording.
- Unknown is unknown. Never invent vendor scores or overall percentages.
"""


def reader_pass_claude(
    document_text: str,
    jd: Dict[str, Any],
    evaluations: Sequence[RequirementEvaluation],
    requirements: Sequence[Dict[str, Any]],
    *,
    base_text: str = "",
) -> Dict[str, Any]:
    """Run Claude reader when available; always merge deterministic absences.

    Returns status: completed | partial | unavailable.
    """
    det = build_findings_from_evaluations(
        evaluations=evaluations,
        requirements=list(requirements),
        document_text=document_text or "",
    )
    # Retention vs base (deterministic)
    if base_text and document_text:
        det.extend(retention_findings(base_text, document_text))

    if not (document_text or "").strip():
        return {
            "status": "unavailable",
            "findings": [f.as_dict() for f in det],
            "top_actions": derive_top_actions(det),
            "reason": "No extracted document text.",
        }
    if len(document_text) > 45000:
        return {
            "status": "partial",
            "findings": [f.as_dict() for f in det],
            "top_actions": derive_top_actions(det),
            "reason": "Document exceeds review budget; deterministic findings only.",
        }

    claude_findings: List[AssessmentFinding] = []
    status = "completed"
    reason = ""
    try:
        from services.claude_client import claude_json, CLAUDE_PRO
        job = {k: jd.get(k) for k in (
            "job_title", "responsibilities", "required_skills",
            "preferred_skills", "qualifications",
        )}
        response = claude_json(
            prompt=(
                wrap_untrusted("visible_resume", redact_pii_for_model(document_text))
                + "\n"
                + wrap_untrusted("job", json.dumps(job))
            ),
            system=guarded_system(_V2_READER_RULES),
            model=CLAUDE_PRO,
            temperature=0,
            max_tokens=2500,
            schema={
                "apparent_role": str,
                "apparent_level": str,
                "findings": [{"kind": str, "quote": str, "explanation": str}],
            },
        )
        allowed = {
            "strength", "wording", "clarity", "absence", "relationship",
            "integrity", "concern", "over_tailoring", "hidden_fit", "visual",
        }
        for raw in response.get("findings") or []:
            if not isinstance(raw, dict):
                continue
            kind = str(raw.get("kind") or "").strip()
            if kind not in allowed:
                continue
            quote = str(raw.get("quote") or "").strip()
            explanation = str(raw.get("explanation") or "").strip()
            if not explanation:
                continue
            # Map legacy kinds
            mapped = {
                "concern": "clarity",
                "over_tailoring": "wording",
                "hidden_fit": "strength",
            }.get(kind, kind)
            if mapped == "absence":
                # Never invent resume quotes for absence
                claude_findings.append(AssessmentFinding(
                    kind="absence",
                    severity=severity_for("absence"),
                    explanation=explanation,
                    references=[
                        FindingReference(kind="jd_requirement", value=quote or explanation[:80]),
                        FindingReference(kind="search_scope", value="skills+experience+summary"),
                    ],
                    suggested_action=explanation[:160],
                ))
                continue
            if quote and not token_boundary_match(document_text, quote):
                # Quote must appear in the document (flexible whitespace)
                # Also try substring after normalize
                if quote.casefold() not in document_text.casefold():
                    continue
            if not quote and mapped in {"wording", "strength", "clarity"}:
                continue  # quote-backed kinds need a real span
            refs = []
            if quote:
                refs.append(FindingReference(kind="resume_span", value=quote[:240]))
            claude_findings.append(AssessmentFinding(
                kind=mapped,  # type: ignore[arg-type]
                severity=severity_for(mapped),
                explanation=explanation,
                references=refs,
                suggested_action=explanation[:160],
            ))
    except Exception as exc:
        logger.warning("assessment-v2 Claude reader unavailable: %s", exc)
        status = "partial" if det else "unavailable"
        reason = "Reader review unavailable."

    # Merge: deterministic absences + Claude (dedupe by explanation prefix)
    merged: List[AssessmentFinding] = []
    seen = set()
    for f in list(det) + claude_findings:
        key = (f.kind, f.explanation[:80])
        if key in seen:
            continue
        seen.add(key)
        merged.append(f)

    # Coverage bloat note when base provided
    if base_text and document_text:
        bloat = detect_coverage_bloat(
            base_text, document_text,
            {"counts": {"required": {}}},
            {"counts": {"required": {}}},
        )
        # Without counts, still flag extreme repetition
        import re
        from collections import Counter
        words = re.findall(r"[a-z]{4,}", document_text.lower())
        c = Counter(words)
        if any(n >= 8 for n in c.values()):
            merged.append(AssessmentFinding(
                kind="wording",
                severity="advisory",
                explanation="Repeated keywords appear densely; prefer concrete work over repetition.",
                references=[FindingReference(kind="search_scope", value="document")],
                suggested_action="Reduce keyword repetition while keeping supported evidence.",
            ))

    if status == "completed" and not claude_findings and reason:
        status = "partial"

    return {
        "status": status if status != "completed" or claude_findings or det else "partial",
        "findings": [f.as_dict() for f in merged],
        "top_actions": derive_top_actions(merged),
        "reason": reason,
        "apparent_role": "",
        "apparent_level": "",
    }


def reader_pass_v2(
    document_text: str,
    jd: Dict[str, Any],
    evaluations: Sequence[RequirementEvaluation],
    requirements: Sequence[Dict[str, Any]],
    *,
    base_text: str = "",
    run_claude: bool = True,
) -> Dict[str, Any]:
    """Canonical reader entry: Claude when requested, else deterministic."""
    if run_claude:
        return reader_pass_claude(
            document_text, jd, evaluations, requirements, base_text=base_text,
        )
    from services.assessment_comparison import reader_pass_deterministic
    out = reader_pass_deterministic(document_text, jd, evaluations, requirements)
    if base_text:
        extra = retention_findings(base_text, document_text or "")
        findings = list(out.get("findings") or [])
        findings.extend(f.as_dict() for f in extra)
        out["findings"] = findings
        from schemas.assessment_contracts import AssessmentFinding
        # rebuild top actions roughly
        out["top_actions"] = (out.get("top_actions") or [])[:3]
    return out
