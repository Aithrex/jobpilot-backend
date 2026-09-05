"""
Match-gap reporter — tells the USER which JD requirements they genuinely lack,
tiered by interview-defensibility.

Companion to the anti-fabrication work (fabrication_filter / IntegrityGuard).
Anti-fabrication keeps the *resume* honest; this keeps the *user* informed: after
we stop forcing JD skills onto a candidate, the user should see what's actually
missing — but **not all gaps are equal**:

  * Tier 1 "defensible"  — an adjacent skill whose core concepts transfer
      (missing C# but has Python; missing Angular but has React). Speak-to-it in
      an interview; not alarming.
  * Tier 2 "learnable"   — a real, in-domain gap the candidate could pick up
      (a backend dev missing Kafka).
  * Tier 3 "stretch"     — non-defensible: a certification not held, or a
      requirement from a different discipline. Signals the JD wants a different
      background.

Pipeline:
  1. Deterministic gap detection (reuse KeywordGapEngine) → the SHORT list of
     JD required skills genuinely missing from the (already-tailored) resume.
  2. If non-empty, one small LLM call (Claude via Bedrock — the `claude_json`
     name is a legacy alias) tiers each missing item with a one-line rationale.
  3. Graceful fallback: on any model error/timeout, every missing item is
     reported under "learnable" so the feature never blocks the tailor result.

Surfaced as `result["match_gaps"]` on the tailored resume (backend only; UI is a
separate task).
"""
import json
import logging
from typing import Any, Dict, List, Tuple

logger = logging.getLogger(__name__)


def _empty_report(
    req_coverage: int | None = None,
    coverage: int | None = None,
    *,
    incomplete: bool = False,
) -> Dict[str, Any]:
    return {
        "required_coverage": req_coverage,
        "coverage_percentage": coverage,
        "defensible": [],
        "learnable": [],
        "stretch": [],
        "already_demonstrated": [],
        "incomplete": incomplete,
    }


def _fallback_report(missing: List[str], req_coverage: int | None, coverage: int | None) -> Dict[str, Any]:
    """Classification failure is incomplete — not a candidate gap and not 100 coverage."""
    rep = _empty_report(req_coverage, coverage, incomplete=True)
    rep["learnable"] = []
    return rep


def _build_prompt(resume_text: str, job_title: str, missing: List[str]) -> str:
    """Ask the model to tier each missing requirement by interview-defensibility."""
    missing_block = "\n".join(f"  - {m}" for m in missing)
    return (
        "You are assessing how well a candidate matches a job, and specifically how "
        "DEFENSIBLE each missing requirement is in an interview given the candidate's "
        "actual background.\n\n"
        f"TARGET ROLE: {job_title or 'the role'}\n\n"
        "CANDIDATE RESUME (already tailored):\n"
        f"{resume_text}\n\n"
        "JD REQUIREMENTS THE RESUME DOES NOT CLEARLY SHOW:\n"
        f"{missing_block}\n\n"
        "Classify EACH missing requirement into exactly one tier:\n"
        "  - \"already_demonstrated\": the resume ALREADY shows this requirement with explicit\n"
        "    or clearly equivalent wording (including aliases such as Postgres/PostgreSQL, or\n"
        "    \"developed REST endpoints\" for \"REST API Development\"). Use this when your own\n"
        "    explanation would otherwise say the candidate already has the experience.\n"
        "  - \"defensible\": an adjacent skill whose CORE CONCEPTS TRANSFER from what the\n"
        "    candidate already does, so they could credibly speak to it (e.g. missing C# but\n"
        "    has strong Python/Java; missing Angular but has React; missing MySQL but has\n"
        "    PostgreSQL). The bridge must be real, not wishful. Related tools are not the same.\n"
        "  - \"learnable\": a genuine in-domain gap with no direct bridge, but within reach for\n"
        "    someone with this background (e.g. a backend engineer missing a specific message\n"
        "    broker or cloud service they haven't used).\n"
        "  - \"stretch\": NOT defensible — a specific CERTIFICATION the candidate does not hold,\n"
        "    a named proprietary product they've never used with no transferable analog, or a\n"
        "    requirement from a DIFFERENT profession/discipline than the candidate's.\n\n"
        "Return ONLY JSON with this exact shape (one entry per missing item, each with a short\n"
        "one-sentence 'why'):\n"
        "{\n"
        '  "already_demonstrated": [{"item": "...", "why": "..."}],\n'
        '  "defensible": [{"item": "...", "why": "..."}],\n'
        '  "learnable":  [{"item": "...", "why": "..."}],\n'
        '  "stretch":    [{"item": "...", "why": "..."}]\n'
        "}\n"
        "Every missing requirement listed above must appear in exactly one tier. If the resume\n"
        "already shows the work, use already_demonstrated — do not call it missing."
    )


_ALREADY_HINT = (
    "already", "explicit", "resume shows", "demonstrat", "present on the resume",
    "clearly shown", "already has",
)


def _looks_demonstrated(why: str) -> bool:
    low = (why or "").lower()
    return any(h in low for h in _ALREADY_HINT)


def _coerce_tiers(raw: Any, missing: List[str]) -> Dict[str, List[Dict[str, str]]]:
    """Normalize model output. Already-demonstrated is a first-class correction."""
    tiers = {
        "already_demonstrated": [],
        "defensible": [],
        "learnable": [],
        "stretch": [],
    }
    seen = set()
    if isinstance(raw, dict):
        for tier in tiers:
            for entry in raw.get(tier, []) or []:
                if isinstance(entry, dict) and entry.get("item"):
                    item = str(entry["item"]).strip()
                    why = str(entry.get("why", "")).strip()
                elif isinstance(entry, str):
                    item, why = entry.strip(), ""
                else:
                    continue
                if item and item.lower() not in seen:
                    dest = "already_demonstrated" if (
                        tier != "already_demonstrated" and _looks_demonstrated(why)
                    ) else tier
                    payload = {"item": item, "why": why}
                    if dest == "already_demonstrated":
                        payload["match_method"] = "semantic"
                    tiers[dest].append(payload)
                    seen.add(item.lower())
    for m in missing:
        if m.lower() not in seen:
            tiers["learnable"].append(
                {"item": m, "why": "JD requirement not clearly shown in resume."}
            )
            seen.add(m.lower())
    return tiers


def _correct_already_demonstrated(
    tailored: Dict[str, Any],
    missing: List[str],
) -> Tuple[List[str], List[Dict[str, str]]]:
    """Move canonically matched terms out of the missing list."""
    from services.requirement_matcher import locate_term_in_resume

    still: List[str] = []
    demonstrated: List[Dict[str, str]] = []
    for item in missing:
        hit = locate_term_in_resume(tailored, item)
        if hit.found:
            demonstrated.append({
                "item": item,
                "why": "Already shown on the final resume.",
                "span": hit.span,
                "section": hit.section,
                "match_method": hit.method,
            })
        else:
            still.append(item)
    return still, demonstrated


def build_match_gaps(
    tailored: Dict[str, Any],
    jd_analysis: Dict[str, Any],
    gap: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Build the user-facing match-gap report for a tailored resume + JD.

    Deterministic gap detection first (or a precomputed KeywordGapEngine
    result); model tiering only when there are missing required skills.
    Never raises — degrades to a fallback report on any error.
    """
    try:
        from services.keyword_gap_engine import KeywordGapEngine
        from schemas.resume_schemas import build_resume_text

        if gap is None:
            gap = KeywordGapEngine().analyze(tailored, jd_analysis)
        missing = list(gap.get("required_missing") or [])
        req_cov = gap.get("required_coverage")
        cov = gap.get("coverage_percentage")
        try:
            req_cov = int(req_cov) if req_cov is not None else None
        except (TypeError, ValueError):
            req_cov = None
        try:
            cov = int(cov) if cov is not None else None
        except (TypeError, ValueError):
            cov = None

        missing, demonstrated = _correct_already_demonstrated(tailored, missing)
        if not missing:
            rep = _empty_report(req_cov, cov)
            rep["already_demonstrated"] = demonstrated
            return rep

        # Model tiering: FLASH (Haiku on Bedrock) is enough for gap tiering.
        try:
            from services.claude_client import (
                claude_json, CLAUDE_FLASH, LLMRetriesExhaustedError,
            )
            prompt = _build_prompt(
                build_resume_text(tailored),
                jd_analysis.get("job_title", ""),
                missing,
            )
            raw = claude_json(
                prompt=prompt,
                model=CLAUDE_FLASH,   # cheapest/fastest Claude tier — small call
                max_tokens=1500,
                temperature=0.2,
            )
            tiers = _coerce_tiers(raw, missing)
            if demonstrated:
                existing = {
                    str(e.get("item") or "").lower()
                    for e in tiers.get("already_demonstrated") or []
                }
                for row in demonstrated:
                    if row["item"].lower() not in existing:
                        tiers.setdefault("already_demonstrated", []).append(row)
            return {
                "required_coverage": req_cov,
                "coverage_percentage": cov,
                "incomplete": False,
                **tiers,
            }
        except LLMRetriesExhaustedError:
            logger.warning("match_gaps: model tiering exhausted retries — fallback")
            fb = _fallback_report(missing, req_cov, cov)
            fb["already_demonstrated"] = demonstrated
            return fb
        except Exception:  # noqa: BLE001 — never block the tailor result
            logger.exception("match_gaps: model tiering failed — fallback")
            fb = _fallback_report(missing, req_cov, cov)
            fb["already_demonstrated"] = demonstrated
            return fb

    except Exception:  # noqa: BLE001 — gap detection itself failed; stay safe
        logger.exception("match_gaps: gap detection failed — incomplete report")
        return _empty_report(incomplete=True)
