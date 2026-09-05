"""Reader findings and before/after comparison (assessment-v2 Package 5).

Absence findings cite JD + search scope — never fabricated resume quotes.
Comparison stays coverage/clarity/evidence oriented; no new match formula.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from schemas.assessment_contracts import (
    AssessmentFinding,
    FindingReference,
    RequirementEvaluation,
)
from services.artifact_assessment import text_contains_capability
from services.assessment_policy import severity_for


def absence_finding(
    *,
    requirement_id: str,
    requirement_text: str,
    search_scope: str = "skills+experience+summary",
) -> AssessmentFinding:
    return AssessmentFinding(
        kind="absence",
        severity=severity_for("absence"),
        explanation=(
            f"The requirement “{requirement_text}” was not found after searching "
            f"{search_scope}."
        ),
        requirement_id=requirement_id,
        references=[
            FindingReference(kind="jd_requirement", value=requirement_text),
            FindingReference(kind="search_scope", value=search_scope),
        ],
        suggested_action=f"Add supported evidence for {requirement_text} if you have it.",
    )


def wording_finding(
    *,
    requirement_id: str,
    quote: str,
    explanation: str,
) -> AssessmentFinding:
    """Wording findings require a real resume span."""
    return AssessmentFinding(
        kind="wording",
        severity=severity_for("wording"),
        explanation=explanation,
        requirement_id=requirement_id,
        references=[FindingReference(kind="resume_span", value=quote)],
    )


def build_findings_from_evaluations(
    *,
    evaluations: Sequence[RequirementEvaluation],
    requirements: Sequence[Dict[str, Any]],
    document_text: str,
) -> List[AssessmentFinding]:
    """Deterministic findings: absences without fake quotes; no LLM required."""
    by_id = {r.get("id"): r for r in requirements}
    findings: List[AssessmentFinding] = []
    for ev in evaluations:
        req = by_id.get(ev.requirement_id) or {}
        text = str(req.get("display_text") or req.get("text") or ev.requirement_id)
        if ev.document_state == "not_demonstrated":
            findings.append(absence_finding(
                requirement_id=ev.requirement_id,
                requirement_text=text,
            ))
        elif ev.source_state == "contradicted" or ev.claim_state == "contradicted":
            findings.append(AssessmentFinding(
                kind="relationship",
                severity=severity_for("relationship"),
                explanation=(
                    f"Evidence for “{text}” appears under the wrong employer or "
                    "source scope."
                ),
                requirement_id=ev.requirement_id,
                references=[
                    FindingReference(kind="jd_requirement", value=text),
                    FindingReference(
                        kind="entity_pair",
                        value=",".join(r.entity for r in ev.source_refs) or "unknown",
                    ),
                ],
                suggested_action="Keep accomplishments under the employer where they happened.",
            ))
        elif ev.document_state == "satisfied" and document_text:
            # Optional clarity note only when a span exists in the document.
            for ref in ev.document_refs:
                if ref.span and text_contains_capability(document_text, ref.span):
                    # Strength — informational, not a score bump.
                    findings.append(AssessmentFinding(
                        kind="strength",
                        severity=severity_for("strength"),
                        explanation=f"“{ref.span}” is visible in the document.",
                        requirement_id=ev.requirement_id,
                        references=[FindingReference(kind="resume_span", value=ref.span)],
                    ))
                    break
    return findings


def derive_top_actions(
    findings: Sequence[AssessmentFinding],
    *,
    limit: int = 3,
) -> List[str]:
    """Top useful actions from findings — no overall formula."""
    order = {"critical": 0, "material": 1, "advisory": 2, "informational": 3}
    ranked = sorted(
        findings,
        key=lambda f: (order.get(f.severity, 9), 0 if f.kind == "absence" else 1),
    )
    actions: List[str] = []
    for f in ranked:
        action = (f.suggested_action or f.explanation or "").strip()
        if action and action not in actions:
            actions.append(action)
        if len(actions) >= limit:
            break
    return actions


def detect_coverage_bloat(
    before_text: str,
    after_text: str,
    before_eval: Dict[str, Any],
    after_eval: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Longer keyword-heavy draft with no coverage gain is a regression signal."""
    b_sat = int(((before_eval.get("counts") or {}).get("required") or {})
                .get("supported_and_satisfied") or 0)
    a_sat = int(((after_eval.get("counts") or {}).get("required") or {})
                .get("supported_and_satisfied") or 0)
    if a_sat > b_sat:
        return None
    if len(after_text or "") <= len(before_text or "") + 80:
        return None
    # Token repetition heuristic
    import re
    def _rep_score(t: str) -> int:
        words = re.findall(r"[a-z]{3,}", (t or "").lower())
        if not words:
            return 0
        from collections import Counter
        c = Counter(words)
        return sum(n - 1 for n in c.values() if n > 2)
    if _rep_score(after_text) <= _rep_score(before_text):
        return None
    return {
        "kind": "coverage_bloat",
        "before_supported": b_sat,
        "after_supported": a_sat,
        "explanation": "Longer keyword-heavy wording did not improve requirement coverage.",
    }


def retention_findings(base_text: str, tailored_text: str) -> List[AssessmentFinding]:
    """Flag concrete accomplishment lines present in base but missing in tailored."""
    import re
    findings: List[AssessmentFinding] = []
    base_lines = [ln.strip() for ln in (base_text or "").splitlines() if len(ln.strip()) > 40]
    tailored = tailored_text or ""
    lost = 0
    for line in base_lines:
        # Concrete if has a digit or strong verb+object pattern
        if not re.search(r"\d", line) and not re.search(
            r"\b(?:reduced|improved|built|led|owned|delivered)\b", line, re.I,
        ):
            continue
        # Lost if no substantial token overlap
        tokens = set(re.findall(r"[a-z]{4,}", line.lower()))
        if len(tokens) < 3:
            continue
        overlap = sum(1 for t in tokens if t in tailored.lower())
        if overlap < max(3, len(tokens) // 3):
            lost += 1
            findings.append(AssessmentFinding(
                kind="clarity",
                severity="material",
                explanation="A concrete accomplishment from the base resume is no longer evident.",
                references=[
                    FindingReference(kind="resume_span", value=line[:180]),
                    FindingReference(kind="search_scope", value="base_vs_tailored"),
                ],
                suggested_action="Restore the concrete outcome while keeping the clearer wording.",
            ))
        if lost >= 2:
            break
    return findings


def compare_assessments(
    before: Optional[Dict[str, Any]],
    after: Optional[Dict[str, Any]],
    *,
    before_text: str = "",
    after_text: str = "",
) -> Dict[str, Any]:
    """Compare two assessment-v2 results without inventing a match score."""
    if not before or not after:
        return {
            "status": "unavailable",
            "changes": [],
            "reason": "Missing before or after assessment",
        }
    b_snap = before.get("snapshot") or {}
    a_snap = after.get("snapshot") or {}
    if b_snap.get("jd_hash") and a_snap.get("jd_hash"):
        if b_snap["jd_hash"] != a_snap["jd_hash"]:
            return {
                "status": "not_comparable",
                "changes": [],
                "reason": "JD snapshot changed",
            }
    if before.get("assessment_version") != after.get("assessment_version"):
        return {
            "status": "not_comparable",
            "changes": [],
            "reason": "Rubric version changed",
        }

    def _bucket_map(payload: Dict[str, Any]) -> Dict[str, tuple]:
        out = {}
        for ev in payload.get("evaluations") or []:
            rid = ev.get("requirement_id")
            if not rid:
                continue
            out[rid] = (
                ev.get("document_state") or "unknown",
                ev.get("source_state") or "unknown",
            )
        return out

    b_map = _bucket_map(before)
    a_map = _bucket_map(after)
    changes: List[Dict[str, Any]] = []
    for rid in sorted(set(b_map) | set(a_map)):
        b = b_map.get(rid)
        a = a_map.get(rid)
        if b == a:
            continue
        changes.append({
            "requirement_id": rid,
            "before": b,
            "after": a,
            "kind": "coverage",
        })

    b_counts = (before.get("counts") or {}).get("required") or {}
    a_counts = (after.get("counts") or {}).get("required") or {}
    b_sat = int(b_counts.get("supported_and_satisfied") or 0)
    a_sat = int(a_counts.get("supported_and_satisfied") or 0)
    if a_sat < b_sat:
        changes.append({
            "kind": "coverage_lost",
            "before_supported": b_sat,
            "after_supported": a_sat,
        })
    elif a_sat > b_sat:
        changes.append({
            "kind": "coverage_gained",
            "before_supported": b_sat,
            "after_supported": a_sat,
        })

    bloat = detect_coverage_bloat(before_text, after_text, before, after)
    if bloat:
        changes.append(bloat)

    for f in retention_findings(before_text, after_text):
        changes.append({
            "kind": "retention_loss",
            "explanation": f.explanation,
            "span": f.references[0].value if f.references else "",
        })

    return {
        "status": "comparable",
        "changes": changes,
        "reason": "",
    }


def reader_pass_deterministic(
    document_text: str,
    jd: Dict[str, Any],
    evaluations: Sequence[RequirementEvaluation],
    requirements: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    """Non-LLM reader stage: findings + top actions. Optional Claude path stays separate."""
    findings = build_findings_from_evaluations(
        evaluations=evaluations,
        requirements=list(requirements),
        document_text=document_text or "",
    )
    # Drop strength spam — keep at most one strength.
    trimmed: List[AssessmentFinding] = []
    strength_kept = False
    for f in findings:
        if f.kind == "strength":
            if strength_kept:
                continue
            strength_kept = True
        trimmed.append(f)
    return {
        "status": "completed" if document_text else "unavailable",
        "findings": [f.as_dict() for f in trimmed],
        "top_actions": derive_top_actions(trimmed),
    }
