"""Plan-guided writers and cross-section reconciler (Increment 8).

Deterministic: no LLM. These stages do not invent content. They:

  * attach ``evidence_ids`` for every published bullet
  * drop skills that are neither on the original resume nor a confirmed fact
  * dedupe identical bullets across experience / projects
  * persist the content plan and a Review list of withheld adjacent skills

Cancel is checked at each writer boundary by the caller.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Set

from schemas.resume_schemas import flatten_skills

logger = logging.getLogger(__name__)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _skill_key(s: str) -> str:
    return re.sub(r"[^a-z0-9+.#]+", "", (s or "").lower())


def _fact_ids_for_bullet(
    bullet: str,
    fact_store: Dict[str, Any],
    role_fact_id: str = "",
) -> List[str]:
    """Best-effort evidence: original-bullet facts that share tokens, else the role."""
    by_id = fact_store.get("by_id") or {}
    facts = fact_store.get("facts") or []
    text = _norm(bullet)
    if not text:
        return [role_fact_id] if role_fact_id else []

    hits: List[str] = []
    for fact in facts:
        fid = fact.get("fact_id") or ""
        if not fid:
            continue
        display = _norm(str(fact.get("display") or fact.get("value") or ""))
        if not display:
            continue
        # Original-bullet facts carry the verbatim text in quotes.
        if fact.get("type") == "bullet" or fact.get("fact_type") == "bullet":
            quoted = display
            if quoted and (quoted in text or text in quoted or _overlap(text, quoted)):
                hits.append(fid)
        elif fid == role_fact_id:
            continue
    if hits:
        return hits[:3]
    if role_fact_id and role_fact_id in by_id:
        return [role_fact_id]
    # Last resort: any employment/project fact that shares a company/name token.
    for fact in facts:
        if fact.get("fact_type") in ("employment", "project") or fact.get("type") in (
            "employment", "project",
        ):
            val = _norm(str(fact.get("value") or fact.get("display") or ""))
            if val and _overlap(text, val):
                return [fact["fact_id"]]
    return [role_fact_id] if role_fact_id else []


def _overlap(a: str, b: str) -> bool:
    ta = {t for t in re.split(r"[^a-z0-9]+", a) if len(t) > 3}
    tb = {t for t in re.split(r"[^a-z0-9]+", b) if len(t) > 3}
    return bool(ta and tb and len(ta & tb) >= 2)


def attach_evidence_ids(
    result: Dict[str, Any],
    fact_store: Optional[Dict[str, Any]],
) -> Dict[str, List[str]]:
    """Write ``_evidence`` map: address -> fact_ids. Every remaining bullet gets one."""
    store = fact_store or {"facts": [], "by_id": {}}
    evidence: Dict[str, List[str]] = {}

    for i, exp in enumerate(result.get("experience") or []):
        role_id = f"res:exp:{i}"
        for j, bullet in enumerate(exp.get("bullets") or []):
            if not isinstance(bullet, str) or not bullet.strip():
                continue
            addr = f"experience.{i}.b{j}"
            ids = _fact_ids_for_bullet(bullet, store, role_id)
            evidence[addr] = ids or [role_id]

    for i, proj in enumerate(result.get("projects") or []):
        role_id = f"res:proj:{i}"
        for j, bullet in enumerate(proj.get("bullets") or []):
            if not isinstance(bullet, str) or not bullet.strip():
                continue
            addr = f"projects.{i}.b{j}"
            ids = _fact_ids_for_bullet(bullet, store, role_id)
            evidence[addr] = ids or [role_id]

    if (result.get("summary") or "").strip():
        evidence["summary"] = (
            ["res:summary"] if (store.get("by_id") or {}).get("res:summary")
            else [f.get("fact_id") for f in (store.get("facts") or [])[:1] if f.get("fact_id")]
        )
        if not evidence["summary"]:
            evidence["summary"] = ["res:summary"]

    result["_evidence"] = evidence
    return evidence


def drop_unconfirmed_skills(
    result: Dict[str, Any],
    original: Dict[str, Any],
    fact_store: Optional[Dict[str, Any]],
) -> List[str]:
    """Remove skills that are not on the original resume and not confirmed.

    Withheld terms go to ``_review_skills`` so the Review panel can ask the
    candidate to confirm them. They do not publish.
    """
    allowed: Set[str] = set()
    for s in flatten_skills(original):
        if isinstance(s, str) and s.strip():
            allowed.add(_skill_key(s))
    for fact in (fact_store or {}).get("facts") or []:
        if not fact.get("candidate_confirmed", fact.get("source") == "resume"):
            continue
        for key in (fact.get("value"), fact.get("display")):
            if isinstance(key, str) and key.strip():
                allowed.add(_skill_key(key))
                # Confirmed skill facts are often "Skill: Terraform"
                if ":" in key:
                    allowed.add(_skill_key(key.split(":", 1)[-1]))

    withheld: List[str] = []
    skills = result.get("skills")
    if not isinstance(skills, dict):
        return withheld
    cleaned: Dict[str, List[str]] = {}
    for heading, items in skills.items():
        kept: List[str] = []
        for item in items or []:
            if not isinstance(item, str) or not item.strip():
                continue
            if _skill_key(item) in allowed or any(
                _skill_key(item) in a or a in _skill_key(item)
                for a in allowed if len(a) > 2
            ):
                kept.append(item)
            else:
                withheld.append(item)
        if kept:
            cleaned[heading] = kept
    result["skills"] = cleaned
    if withheld:
        result["_review_skills"] = sorted(set(withheld))
        logger.info("plan_writers: withheld %d unconfirmed skill(s)", len(withheld))
    return withheld


def reconcile_sections(result: Dict[str, Any]) -> int:
    """Drop exact-duplicate bullets (experience wins over projects)."""
    seen: Set[str] = set()
    removed = 0

    def _filter(entries: List[Dict[str, Any]], key: str) -> None:
        nonlocal removed
        for entry in entries:
            kept = []
            for b in entry.get(key) or []:
                if not isinstance(b, str) or not b.strip():
                    continue
                n = _norm(b)
                if n in seen:
                    removed += 1
                    continue
                seen.add(n)
                kept.append(b)
            entry[key] = kept

    _filter(result.get("experience") or [], "bullets")
    _filter(result.get("projects") or [], "bullets")
    # Empty project shells after dedupe are noise.
    result["projects"] = [
        p for p in (result.get("projects") or [])
        if (p.get("name") or "").strip() and (p.get("bullets") or [])
    ]
    return removed


def experience_writer(
    result: Dict[str, Any],
    content_plan: Dict[str, Any],
) -> Dict[str, Any]:
    """Cap bullets per role from the plan. Never invents bullets."""
    budget = (content_plan or {}).get("bullet_budget") or {}
    per = int(budget.get("bullets_per_role") or 4)
    per = max(2, min(per, 6))
    for exp in result.get("experience") or []:
        bullets = [b for b in (exp.get("bullets") or []) if isinstance(b, str) and b.strip()]
        exp["bullets"] = bullets[:per]
    return result


def skills_writer(
    result: Dict[str, Any],
    original: Dict[str, Any],
    fact_store: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    drop_unconfirmed_skills(result, original, fact_store)
    return result


def evidence_planner(
    result: Dict[str, Any],
    original: Dict[str, Any],
) -> Dict[str, Any]:
    """Publish only original credentials and optional extras."""
    for key in (
        "licenses", "certifications", "languages", "awards", "publications",
        "patents", "leadership", "volunteering", "associations", "clearance",
    ):
        src = original.get(key) or []
        result[key] = [c for c in src if isinstance(c, str) and c.strip()]
    if (original.get("headline") or "").strip():
        result["headline"] = original["headline"].strip()
    return result


def summary_writer(
    result: Dict[str, Any],
    original: Dict[str, Any],
) -> Dict[str, Any]:
    """Keep the tailored summary if present; otherwise the original. No LLM."""
    if not (result.get("summary") or "").strip():
        result["summary"] = (original.get("summary") or "").strip()
    return result


def apply_plan(
    result: Dict[str, Any],
    original: Dict[str, Any],
    content_plan: Dict[str, Any],
    fact_store: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Wave C: experience / skills / evidence in parallel, then summary, then reconcile."""
    result["content_plan"] = content_plan
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=3) as pool:
        f_exp = pool.submit(experience_writer, result, content_plan)
        f_sk = pool.submit(skills_writer, result, original, fact_store)
        f_ev = pool.submit(evidence_planner, result, original)
        f_exp.result()
        f_sk.result()
        f_ev.result()
    summary_writer(result, original)
    reconcile_sections(result)
    attach_evidence_ids(result, fact_store)
    return result
