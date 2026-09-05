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
    # Source-linked requirement records preserve employer alternatives.
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
        out.append(Requirement(digest(text.lower())[:12], text, priority,
                               record.get("kind", "skill"), alts,
                               excerpt if pos >= 0 else "", pos if pos >= 0 else None))
    return out


def build_plan(source: dict, jd: dict, extra_facts: list[CandidateFact] | None = None) -> TailoringPlan:
    facts = source_facts(source) + (extra_facts or [])
    reqs = requirements(jd)
    for req in reqs:
        exact = [f for f in facts if any(contains(a, f.text) for a in req.alternatives)]
        semantic = [f for f in facts if f.kind == "accomplishment"
                    and semantic_support(req.text, f.text)]
        demonstrated = [f for f in exact if f.kind in {"accomplishment", "credential"}]
        if demonstrated or semantic:
            req.state = "demonstrated"
            req.support_basis = "explicit" if demonstrated else "semantic"
            req.fact_ids = [f.id for f in demonstrated or semantic]
        elif exact:
            req.state, req.support_basis = "listed", "explicit"
            req.fact_ids = [f.id for f in exact]
        else:
            related = [f for f in facts if f.kind == "accomplishment"
                       and len(tokens(req.text) & tokens(f.text)) >= 2]
            if related:
                req.state = "related"
                req.fact_ids = [f.id for f in related]
    weights = {}
    for req in reqs:
        for fid in req.fact_ids:
            weights[fid] = weights.get(fid, 0) + (3 if req.priority == "required" else 1)
    selected = [f.id for f in sorted((f for f in facts if f.kind == "accomplishment"),
                                     key=lambda f: -weights.get(f.id, 0))]
    return TailoringPlan(content_hash(source), digest(jd), facts, reqs, selected,
                         jd.get("analysis_completeness", "structured_only"))


def questions(plan: TailoringPlan) -> list[dict]:
    # Only listed skills where work context would matter; never one question per gap.
    return [{"id": r.id, "requirement": r.text,
             "question": f"Where have you used {r.text}, and what did you do with it?",
             "state": "optional"}
            for r in plan.requirements if r.priority == "required" and r.state == "listed"][:3]
