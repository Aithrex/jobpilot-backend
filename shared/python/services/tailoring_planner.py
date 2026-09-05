"""Source-scoped requirements and selection, without lexical coverage quotas."""
from __future__ import annotations

import re
from schemas.tailoring_contracts import CandidateFact, Requirement, TailoringPlan
from schemas.resume_schemas import flatten_skills
from services.tailoring_policy import content_hash, digest
from utils.keyword_normalizer import get_all_forms


def contains(term: str, text: str) -> bool:
    return any(re.search(r"(?<!\w)" + re.escape(f) + r"(?!\w)", text, re.I)
               for f in get_all_forms(term) if f)


def tokens(text: str) -> set[str]:
    stop = {"with", "and", "the", "for", "that", "from", "have", "experience", "required", "using"}
    return {t for t in re.findall(r"[a-z][a-z0-9+#.-]+", text.lower()) if len(t) > 2 and t not in stop}


def semantic_support(requirement: str, text: str) -> bool:
    """Conservative action-based entailments, never title-based assumptions."""
    r, t = requirement.lower(), text.lower()
    if re.fullmatch(r"(?:failure recovery|retry handling|reliability)", r):
        return bool(re.search(r"\bretr(?:y|ies|ied)\b", t) and re.search(r"fail|unsuccessful", t))
    if r in {"database performance optimization", "query optimization", "sql optimization"}:
        return bool(re.search(r"index|indexes|indexing", t) and re.search(r"quer|sql", t)
                    and re.search(r"reduc|improv|optimi|faster", t))
    if r in {"cross-functional collaboration", "cross-functional delivery"}:
        return bool(re.search(r"work|collaborat|partner", t)
                    and sum(s in t for s in ("design", "qa", "sales", "marketing", "finance")) >= 2)
    if re.search(r"\bpython\b", r) and re.search(r"\b(?:django|flask|fastapi)\b", t):
        return True
    if re.search(r"\bkubernetes\b|\bk8s\b", r) and re.search(r"\b(?:helm|k8s|kubectl)\b", t):
        return True
    return False


def source_facts(source: dict) -> list[CandidateFact]:
    snapshot = content_hash(source)[:12]
    facts = []
    for section in ("experience", "projects"):
        for i, entry in enumerate(source.get(section) or []):
            entity = f"{section}.{i}"
            for j, bullet in enumerate(entry.get("bullets") or []):
                if isinstance(bullet, str) and bullet.strip():
                    address = f"{entity}.bullets.{j}"
                    facts.append(CandidateFact(f"{snapshot}:{address}", address, entity, bullet.strip()))
    for i, skill in enumerate(flatten_skills(source)):
        if isinstance(skill, str) and skill.strip():
            facts.append(CandidateFact(f"{snapshot}:skill:{i}", f"skills.{i}", "skills", skill, "skill"))
    for i, cert in enumerate(source.get("certifications") or []):
        if isinstance(cert, str):
            facts.append(CandidateFact(f"{snapshot}:cert:{i}", f"certifications.{i}", "certifications", cert, "credential"))
    for i, education in enumerate(source.get("education") or []):
        if isinstance(education, dict) and education.get("degree"):
            text = ", ".join(str(education[k]) for k in ("degree", "institution", "dates") if education.get(k))
            facts.append(CandidateFact(f"{snapshot}:education:{i}", f"education.{i}", f"education.{i}", text, "credential"))
    return facts


def requirements(jd: dict) -> list[Requirement]:
    out, seen = [], set()
    raw = jd.get("source_text") or ""
    try:
        from services.requirement_assessment import attach_expressions
        jd = attach_expressions(jd) if not any(
            isinstance(r, dict) and r.get("expression")
            for r in (jd.get("requirements") or [])
        ) else jd
    except Exception:
        pass
    records = jd.get("requirements") or []
    if not records:
        records = [{"text": t, "priority": p, "kind": k}
                   for field, p, k in (("required_skills", "required", "skill"),
                                       ("preferred_skills", "preferred", "skill"),
                                       ("responsibilities", "unspecified", "responsibility"),
                                       ("qualifications", "unspecified", "qualification"))
                   for t in jd.get(field) or [] if isinstance(t, str)]
    for record in records:
        text = str(record.get("text") or "").strip()
        if not text or text.lower() in seen:
            continue
        seen.add(text.lower())
        alts = record.get("alternatives") or re.split(r"\s+or\s+", text, flags=re.I)
        alts = [str(a).strip() for a in alts if str(a).strip()]
        excerpt = str(record.get("source_excerpt") or text)
        pos = raw.find(excerpt) if raw else -1
        priority = record.get("priority", "unspecified")
        if priority not in {"required", "preferred", "unspecified"}:
            priority = "unspecified"
        req = Requirement(digest(text.lower())[:12], text, priority,
                          record.get("kind", "skill"), alts,
                          excerpt if pos >= 0 else "", pos if pos >= 0 else None)
        if isinstance(record.get("expression"), dict):
            setattr(req, "expression", record["expression"])
        out.append(req)
    return out


def _expression_capabilities(expr: dict | None) -> list[str]:
    if not isinstance(expr, dict):
        return []
    kind = expr.get("kind")
    if kind == "atomic":
        pred = expr.get("predicate") or {}
        cap = str(pred.get("capability") or "").strip()
        return [cap] if cap else []
    if kind in {"and", "or"}:
        caps: list[str] = []
        for child in expr.get("children") or []:
            caps.extend(_expression_capabilities(child))
        return caps
    return []


def _fact_supports_capability(fact: CandidateFact, capability: str) -> str:
    if not capability:
        return ""
    if any(contains(f, fact.text) for f in get_all_forms(capability) if f):
        if fact.kind == "skill":
            return "listed"
        return "demonstrated"
    if fact.kind == "accomplishment" and semantic_support(capability, fact.text):
        return "semantic"
    if fact.kind == "accomplishment" and len(tokens(capability) & tokens(fact.text)) >= 2:
        return "related"
    return ""


def _select_facts_for_expression(
    facts: list[CandidateFact],
    expr: dict | None,
    alternatives: list[str],
) -> tuple[str, str, list[str]]:
    """AND/OR-aware fact selection for planning."""
    kind = (expr or {}).get("kind") if isinstance(expr, dict) else None
    caps = _expression_capabilities(expr) if kind in {"and", "or", "atomic"} else []
    if not caps:
        caps = [a for a in alternatives if a]
    if kind == "unresolved":
        return "not_evidenced", "", []

    if kind == "or" or (kind is None and len(caps) > 1):
        best_state, best_basis, best_ids = "not_evidenced", "", []
        rank = {"demonstrated": 4, "listed": 3, "related": 2, "not_evidenced": 0}
        for cap in caps:
            state, basis, ids = "not_evidenced", "", []
            for f in facts:
                cls = _fact_supports_capability(f, cap)
                if cls == "demonstrated":
                    ids.append(f.id)
                    state, basis = "demonstrated", "explicit"
                elif cls == "semantic" and state != "demonstrated":
                    ids.append(f.id)
                    state, basis = "demonstrated", "semantic"
                elif cls == "listed" and state == "not_evidenced":
                    ids.append(f.id)
                    state, basis = "listed", "explicit"
                elif cls == "related" and state == "not_evidenced":
                    ids.append(f.id)
                    state, basis = "related", ""
            if rank.get(state, 0) > rank.get(best_state, 0):
                best_state, best_basis, best_ids = state, basis, ids
        return best_state, best_basis, best_ids

    if kind == "and":
        all_ids: list[str] = []
        child_states = []
        basis = "explicit"
        for cap in caps:
            state, b, ids = _select_facts_for_expression(
                facts, {"kind": "atomic", "predicate": {"capability": cap}}, [cap],
            )
            child_states.append(state)
            all_ids.extend(ids)
            if b == "semantic":
                basis = "semantic"
        if child_states and all(s == "demonstrated" for s in child_states):
            return "demonstrated", basis, list(dict.fromkeys(all_ids))
        if any(s == "not_evidenced" for s in child_states) and any(
            s in {"demonstrated", "listed", "related"} for s in child_states
        ):
            return "related", basis, list(dict.fromkeys(all_ids))
        if child_states and all(s == "listed" for s in child_states):
            return "listed", "explicit", list(dict.fromkeys(all_ids))
        if any(s == "demonstrated" for s in child_states):
            return "demonstrated", basis, list(dict.fromkeys(all_ids))
        return "not_evidenced", "", list(dict.fromkeys(all_ids))

    cap = caps[0] if caps else ""
    ids_demo, ids_list, ids_rel, ids_sem = [], [], [], []
    for f in facts:
        cls = _fact_supports_capability(f, cap)
        if cls == "demonstrated":
            ids_demo.append(f.id)
        elif cls == "semantic":
            ids_sem.append(f.id)
        elif cls == "listed":
            ids_list.append(f.id)
        elif cls == "related":
            ids_rel.append(f.id)
    if ids_demo:
        return "demonstrated", "explicit", ids_demo
    if ids_sem:
        return "demonstrated", "semantic", ids_sem
    if ids_list:
        return "listed", "explicit", ids_list
    if ids_rel:
        return "related", "", ids_rel
    return "not_evidenced", "", []


def build_plan(source: dict, jd: dict, extra_facts: list[CandidateFact] | None = None) -> TailoringPlan:
    """Select facts using AND/OR requirement expressions when present."""
    facts = source_facts(source) + (extra_facts or [])
    reqs = requirements(jd)
    for req in reqs:
        expr = getattr(req, "expression", None)
        state, basis, fact_ids = _select_facts_for_expression(
            facts, expr if isinstance(expr, dict) else None, list(req.alternatives or []),
        )
        req.state = state
        req.support_basis = basis
        req.fact_ids = fact_ids
    weights: dict[str, int] = {}
    for req in reqs:
        for fid in req.fact_ids:
            weights[fid] = weights.get(fid, 0) + (3 if req.priority == "required" else 1)
    selected = [f.id for f in sorted((f for f in facts if f.kind == "accomplishment"),
                                     key=lambda f: -weights.get(f.id, 0))]
    return TailoringPlan(content_hash(source), digest(jd), facts, reqs, selected,
                         jd.get("analysis_completeness", "structured_only"))


def questions(plan: TailoringPlan) -> list[dict]:
    return [{"id": r.id, "requirement": r.text,
             "question": f"Where have you used {r.text}, and what did you do with it?",
             "state": "optional"}
            for r in plan.requirements if r.priority == "required" and r.state == "listed"][:3]
