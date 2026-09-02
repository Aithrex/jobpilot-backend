"""Application readiness — qualification fit, not an ATS match score.

Increment 9. Recruiters and hiring managers decide; this module only answers:

  * which JD requirements the resume can evidence
  * which knockouts (license, clearance, work-auth) the candidate fails
  * whether a skill is *used* in experience, not only listed

It never estimates rejection probability. The existing overall ATS rubric is
left in place; readiness rides beside it.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

from schemas.resume_schemas import build_resume_text, flatten_skills
from schemas.tailoring_contracts import empty_readiness

logger = logging.getLogger(__name__)


def _haystacks(resume: Dict[str, Any]) -> Dict[str, str]:
    skills = " ".join(flatten_skills(resume)).lower()
    usage_parts: List[str] = [resume.get("summary") or ""]
    for exp in resume.get("experience") or []:
        usage_parts.append(exp.get("title") or "")
        usage_parts.extend(b for b in (exp.get("bullets") or []) if isinstance(b, str))
    for proj in resume.get("projects") or []:
        usage_parts.extend(b for b in (proj.get("bullets") or []) if isinstance(b, str))
        usage_parts.append(proj.get("tech") or "")
    licenses = " ".join(
        str(x) for x in (resume.get("licenses") or []) + (resume.get("certifications") or [])
    ).lower()
    return {
        "skills": skills,
        "usage": "\n".join(usage_parts).lower(),
        "credentials": licenses,
        "full": build_resume_text(resume or {}).lower(),
    }


def _term_hit(term: str, blob: str) -> bool:
    t = (term or "").strip().lower()
    if not t:
        return False
    if t in blob:
        return True
    tokens = [x for x in re.split(r"[^a-z0-9+.#]+", t) if len(x) > 2]
    return bool(tokens) and all(tok in blob for tok in tokens[:2])


def _cover_requirement(req: Dict[str, Any], bags: Dict[str, str]) -> Dict[str, Any]:
    term = req.get("normalized_term") or req.get("text") or ""
    kind = req.get("requirement_type") or "skill"
    listed = _term_hit(term, bags["skills"] + " " + bags["credentials"])
    used = _term_hit(term, bags["usage"])
    if kind in ("license", "credential"):
        status = "supported" if _term_hit(term, bags["credentials"] + " " + bags["full"]) else "missing"
        usage = "credential"
    elif kind in ("work_auth", "clearance", "travel", "work_mode", "location", "instruction"):
        status = "unknown"
        usage = "logistics"
    elif used:
        status = "supported"
        usage = "professional"
    elif listed:
        status = "partial"
        usage = "skills_list"
    else:
        status = "missing"
        usage = "none"
    return {
        "requirement_id": req.get("requirement_id"),
        "text": term,
        "requirement_type": kind,
        "priority": req.get("priority") or "contextual",
        "evidence_status": status,
        "usage": usage,
    }


def analyze(
    resume: Dict[str, Any],
    jd_analysis: Dict[str, Any],
    jd_text: str = "",
) -> Dict[str, Any]:
    """Return a readiness sidecar. Never raises; never claims a reject rate."""
    out = empty_readiness()
    resume = resume or {}
    jd_analysis = jd_analysis or {}
    try:
        from services.fit_score import score as fit_score
        from services.jd_analyzer import enrich

        analysis = jd_analysis
        if jd_text and not analysis.get("requirements"):
            analysis = enrich(dict(analysis), jd_text, resume=resume)

        bags = _haystacks(resume)
        coverage = [
            _cover_requirement(r, bags)
            for r in (analysis.get("requirements") or [])
            if isinstance(r, dict)
        ]
        out["requirement_coverage"] = coverage

        job = {
            "title": analysis.get("job_title") or "",
            "location": analysis.get("location") or "",
            "description": jd_text or " ".join(
                (analysis.get("qualifications") or [])
                + (analysis.get("responsibilities") or [])
            ),
        }
        fit = fit_score(resume, job)
        out["qualification_fit"] = int(fit.get("score") or 0)
        knockouts: List[str] = []
        for c in fit.get("hard_constraints") or []:
            if not isinstance(c, dict) or c.get("status") != "missing":
                continue
            kind = str(c.get("type") or "")
            detail = str(c.get("detail") or kind)
            if detail:
                knockouts.append(detail)
        # Mandatory resume-controllable gaps are also knockouts.
        for cov in coverage:
            if (
                cov.get("priority") == "mandatory"
                and cov.get("evidence_status") == "missing"
                and cov.get("usage") not in ("logistics",)
            ):
                knockouts.append(f"Missing required: {cov.get('text')}")
        # Dedupe
        seen = set()
        unique = []
        for k in knockouts:
            if k and k not in seen:
                seen.add(k)
                unique.append(k)
        out["knockout_failures"] = unique[:12]

        notes: List[str] = []
        listed_only = [
            c["text"] for c in coverage
            if c.get("usage") == "skills_list" and c.get("priority") == "mandatory"
        ]
        if listed_only:
            notes.append(
                "These required terms appear only in the skills list, not in "
                f"professional usage: {', '.join(listed_only[:6])}."
            )
        if unique:
            notes.append(
                "Resolve knockout gaps (licenses, credentials, or missing "
                "required evidence) before treating this as a strong apply."
            )
        out["notes"] = notes
    except Exception as e:
        logger.warning("readiness analyze failed (sidecar empty): %s", e)
        out["notes"] = ["Readiness check skipped."]
    return out
