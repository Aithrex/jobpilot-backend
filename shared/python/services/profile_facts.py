"""
Profile Facts — the fact-locked candidate profile behind claim verification.

Design: a COMPUTED VIEW over existing documents, not a synced mutable store.
The structured resume in `user_resumes` is replaced wholesale on re-upload;
a mirrored "facts" collection would drift. Instead, facts are derived
deterministically (no LLM — LLM extraction would reintroduce hallucination
into the source of truth) each time they're needed:

  - resume facts: implicit, always trusted (the user uploaded them)
  - linkedin / github facts (M6): enter ONLY after explicit user
    confirmation, recorded in the thin `enrichment_confirmations`
    collection keyed by content-hashed fact ids

Fact IDs: `res:exp:0`, `res:edu:1`, `res:proj:0`, `res:skill:<norm>`,
`res:exp:0:b3` (original bullets are themselves facts — prime entailment
evidence). LinkedIn/GitHub ids (`li:pos:<sha1>`, `gh:repo:<sha1>`) are
content-hashed so re-imports map onto prior decisions.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from schemas.resume_schemas import flatten_skills

logger = logging.getLogger(__name__)

CONFIRMATIONS_COLLECTION = "enrichment_confirmations"
_VALID_DECISIONS = ("confirmed", "rejected")


def _sha(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8", "replace")).hexdigest()[:10]


def _db():
    from utils.db_connect import DBConnect
    return DBConnect().get_db()


def build_fact_store(
    user_email: str = "",
    structured_resume: Optional[Dict[str, Any]] = None,
    include_enrichment: bool = True,
) -> Dict[str, Any]:
    """Compile the candidate's fact store.

    `structured_resume` short-circuits the Mongo read (the tailor pipeline
    already holds the doc). Enrichment (confirmed LinkedIn/GitHub facts)
    is merged when available — resume-only stores are always valid.
    """
    if structured_resume is None and user_email:
        from services.resume_parser import ResumeParser
        from utils.db_connect import DBConnect
        parser = ResumeParser(DBConnect().get_db())
        doc = parser.get_structured_resume(user_email)
        structured_resume = (doc or {}).get("structured") or {}
    structured_resume = structured_resume or {}

    facts: List[Dict[str, Any]] = list(_resume_facts(structured_resume))
    sources = {"resume": bool(facts), "linkedin": False, "github": False}

    if include_enrichment and user_email:
        try:
            enriched = _confirmed_enrichment_facts(user_email)
            for fact in enriched:
                facts.append(fact)
                sources[fact["source"]] = True
        except Exception as e:
            # Enrichment must never break tailoring — resume-only is valid.
            logger.warning("profile_facts: enrichment merge failed: %s", e)

    return {
        "facts": facts,
        "by_id": {f["fact_id"]: f for f in facts},
        "sources": sources,
    }


def _resume_facts(structured: Dict[str, Any]):
    """Deterministic fact extraction from the parsed resume. No LLM."""
    for i, exp in enumerate(structured.get("experience") or []):
        company = exp.get("company") or "?"
        title = exp.get("title") or "?"
        dates = exp.get("dates") or ""
        yield {
            "fact_id": f"res:exp:{i}",
            "type": "employment",
            "source": "resume",
            "display": f"EMPLOYMENT: {title} at {company}"
                       + (f", {dates}" if dates else ""),
        }
        for j, bullet in enumerate(exp.get("bullets") or []):
            if isinstance(bullet, str) and bullet.strip():
                yield {
                    "fact_id": f"res:exp:{i}:b{j}",
                    "type": "bullet",
                    "source": "resume",
                    "display": f'ORIGINAL BULLET ({company}): "{bullet.strip()}"',
                }

    for i, edu in enumerate(structured.get("education") or []):
        degree = edu.get("degree") or "?"
        institution = edu.get("institution") or "?"
        yield {
            "fact_id": f"res:edu:{i}",
            "type": "education",
            "source": "resume",
            "display": f"EDUCATION: {degree}, {institution}"
                       + (f", {edu['dates']}" if edu.get("dates") else ""),
        }

    for i, proj in enumerate(structured.get("projects") or []):
        name = proj.get("name") or "?"
        tech = proj.get("tech") or ""
        yield {
            "fact_id": f"res:proj:{i}",
            "type": "project",
            "source": "resume",
            "display": f"PROJECT: {name}" + (f" (tech: {tech})" if tech else ""),
        }
        for j, bullet in enumerate(proj.get("bullets") or []):
            if isinstance(bullet, str) and bullet.strip():
                yield {
                    "fact_id": f"res:proj:{i}:b{j}",
                    "type": "bullet",
                    "source": "resume",
                    "display": f'ORIGINAL PROJECT BULLET ({name}): "{bullet.strip()}"',
                }

    summary = (structured.get("summary") or "").strip()
    if summary:
        yield {
            "fact_id": "res:summary",
            "type": "summary",
            "source": "resume",
            "display": f'ORIGINAL SUMMARY: "{summary}"',
        }

    skills = flatten_skills(structured)
    if skills:
        yield {
            "fact_id": "res:skills",
            "type": "skills",
            "source": "resume",
            "display": "SKILLS (from resume): " + ", ".join(
                s for s in skills if isinstance(s, str)
            ),
        }

    certs = structured.get("certifications") or []
    if certs:
        yield {
            "fact_id": "res:certs",
            "type": "certifications",
            "source": "resume",
            "display": "CERTIFICATIONS: " + ", ".join(
                c for c in certs if isinstance(c, str)
            ),
        }


def _confirmed_enrichment_facts(user_email: str) -> List[Dict[str, Any]]:
    """Confirmed LinkedIn/GitHub facts from `enrichment_confirmations`.

    Returns [] until the M6 confirmation surface populates decisions. The
    decision payload snapshots are the fact content (never re-read from the
    volatile archive docs, so a re-import can't silently change a confirmed
    fact). PII boundary: candidates are derived without Connections data.
    """
    from utils.security import InputSanitizer

    email = InputSanitizer.sanitize_string(user_email or "")
    if not email:
        return []
    doc = _db()[CONFIRMATIONS_COLLECTION].find_one({"user_email": email})
    if not doc:
        return []

    facts: List[Dict[str, Any]] = []
    for fact_id, decision in (doc.get("decisions") or {}).items():
        if not isinstance(decision, dict) or decision.get("status") != "confirmed":
            continue
        payload = decision.get("payload") or {}
        source = "linkedin" if fact_id.startswith("li:") else (
            "github" if fact_id.startswith("gh:") else "user"
        )
        display = payload.get("display") or ""
        if not display:
            continue
        facts.append({
            "fact_id": fact_id,
            "type": decision.get("kind") or "confirmed",
            "source": source,
            "display": f"CONFIRMED ({source.capitalize()}): {display}",
        })
    return facts


def build_fact_sheet(fact_store: Dict[str, Any], max_chars: int = 9000) -> str:
    """Render numbered `[F1] ...` lines for the verifier prompt.

    Resume facts are prioritized on overflow (they're the structural truth);
    enrichment lines are dropped first, then original bullets are trimmed
    from the oldest roles.
    """
    facts = fact_store.get("facts") or []
    resume_first = sorted(
        facts, key=lambda f: 0 if f.get("source") == "resume" else 1
    )
    lines: List[str] = []
    total = 0
    for n, fact in enumerate(resume_first, start=1):
        line = f"[F{n}] {fact.get('display', '')}"
        if total + len(line) + 1 > max_chars:
            break
        lines.append(line)
        total += len(line) + 1
        # Remember the sheet number so verifier evidence ids resolve.
        fact["sheet_ref"] = f"F{n}"
    return "\n".join(lines)


def grounding_texts(fact_store: Dict[str, Any]) -> List[str]:
    """Confirmed-enrichment display texts — extra grounding for claim_guard
    (a metric backed by a confirmed GitHub/LinkedIn fact is grounded)."""
    return [
        f.get("display", "")
        for f in (fact_store.get("facts") or [])
        if f.get("source") in ("linkedin", "github")
    ]


def enrichment_context(fact_store: Dict[str, Any], max_chars: int = 1500) -> str:
    """Confirmed LinkedIn/GitHub fact lines for the tailor prompt (capped)."""
    lines: List[str] = []
    total = 0
    for f in fact_store.get("facts") or []:
        if f.get("source") not in ("linkedin", "github"):
            continue
        line = f"- {f.get('display', '')}"
        if total + len(line) + 1 > max_chars:
            break
        lines.append(line)
        total += len(line) + 1
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Candidate facts + confirmation lifecycle (the M6 enrichment surface)
# ---------------------------------------------------------------------------

def _linkedin_candidates(archive: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Candidate facts from the user's own LinkedIn data export.

    PII boundary: `connections` is third-party data — the caller projects it
    out of the Mongo read and this function never touches it.
    """
    cands: List[Dict[str, Any]] = []

    for p in archive.get("positions") or []:
        company = (p.get("company") or "").strip()
        title = (p.get("title") or "").strip()
        if not company and not title:
            continue
        fid = "li:pos:" + _sha(
            f"{company.lower()}|{title.lower()}|{p.get('started_on') or ''}"
        )
        desc = (p.get("description") or "").strip()[:240]
        ground = f"at {company}: {title}" + (f' — "{desc}"' if desc else "")
        cands.append({
            "fact_id": fid,
            "kind": "position",
            "display": f"{title} at {company}" + (f" — {desc}" if desc else ""),
            "payload": {"display": ground},
        })

    for s in archive.get("skills") or []:
        if not isinstance(s, str) or not s.strip():
            continue
        skill = s.strip()
        cands.append({
            "fact_id": "li:skill:" + _sha(skill.lower()),
            "kind": "skill",
            "display": f"Skill: {skill}",
            "payload": {"display": f'skill "{skill}"'},
        })

    for ed in archive.get("educations") or []:
        school = (ed.get("school") or "").strip()
        degree = (ed.get("degree") or "").strip()
        if not school:
            continue
        fid = "li:edu:" + _sha(f"{school.lower()}|{degree.lower()}")
        cands.append({
            "fact_id": fid,
            "kind": "education",
            "display": f"{degree or 'Studied'} at {school}",
            "payload": {"display": f"education: {degree} at {school}".strip()},
        })

    for c in archive.get("certifications") or []:
        name = (c.get("name") or "").strip()
        if not name:
            continue
        authority = (c.get("authority") or "").strip()
        fid = "li:cert:" + _sha(f"{name.lower()}|{authority.lower()}")
        cands.append({
            "fact_id": fid,
            "kind": "certification",
            "display": f"Certification: {name}" + (f" ({authority})" if authority else ""),
            "payload": {"display": f'certification "{name}"'
                        + (f" from {authority}" if authority else "")},
        })

    return cands


def _github_candidates(profile: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Candidate facts from the linked GitHub profile — evidence FOR project
    claims, never employment. Only observable repo facts are asserted."""
    cands: List[Dict[str, Any]] = []
    for repo in profile.get("top_repos") or []:
        name = (repo.get("name") or "").strip()
        if not name:
            continue
        language = (repo.get("language") or "").strip()
        stars = int(repo.get("stars") or 0)
        desc = (repo.get("description") or "").strip()
        bits = [b for b in (
            language,
            f"{stars} stars" if stars >= 5 else "",
            desc,
        ) if b]
        detail = " — ".join([", ".join(bits[:2])] + ([bits[2]] if len(bits) > 2 else [])) if bits else ""
        cands.append({
            "fact_id": "gh:repo:" + _sha(name.lower()),
            "kind": "repo",
            "display": f"Repository: {name}" + (f" ({detail})" if detail else ""),
            "payload": {"display": f'repo "{name}"' + (f": {detail}" if detail else "")},
        })
    return cands


def _all_candidates(user_email: str) -> tuple:
    """(candidates, sources_meta) derived on the fly from the enrichment docs."""
    db = _db()
    candidates: List[Dict[str, Any]] = []
    sources: Dict[str, Any] = {"linkedin": None, "github": None}

    # Projection strips `connections` — third-party PII never leaves Mongo.
    archive = db["linkedin_archives"].find_one(
        {"user_email": user_email}, {"connections": 0}
    )
    if archive:
        cands = _linkedin_candidates(archive)
        for c in cands:
            c["source"] = "linkedin"
        candidates.extend(cands)
        sources["linkedin"] = {
            "imported_at": str(archive.get("imported_at") or archive.get("updated_at") or ""),
            "counts": {k: len(archive.get(k) or []) for k in
                       ("positions", "skills", "educations", "certifications")},
        }

    profile = db["github_profiles"].find_one({"user_email": user_email})
    if profile:
        cands = _github_candidates(profile)
        for c in cands:
            c["source"] = "github"
        candidates.extend(cands)
        sources["github"] = {
            "username": profile.get("username") or profile.get("login") or "",
            "fetched_at": str(profile.get("fetched_at") or profile.get("updated_at") or ""),
        }

    return candidates, sources


def list_candidate_facts(user_email: str) -> Dict[str, Any]:
    """The GET /profile-facts payload: pending candidates + prior decisions."""
    from utils.security import InputSanitizer

    email = InputSanitizer.sanitize_string(user_email or "")
    if not email:
        return {"candidates": [], "confirmed": [], "rejected": [], "sources": {}}

    candidates, sources = _all_candidates(email)
    doc = _db()[CONFIRMATIONS_COLLECTION].find_one({"user_email": email}) or {}
    decisions = doc.get("decisions") or {}

    pending, confirmed, rejected = [], [], []
    for cand in candidates:
        decision = decisions.get(cand["fact_id"])
        status = (decision or {}).get("status")
        entry = {k: cand[k] for k in ("fact_id", "kind", "display", "source")}
        if status == "confirmed":
            confirmed.append(entry)
        elif status == "rejected":
            rejected.append(entry)
        else:
            pending.append(entry)

    return {
        "candidates": pending,
        "confirmed": confirmed,
        "rejected": rejected,
        "sources": sources,
    }


def record_decisions(user_email: str, decisions: List[Dict[str, Any]]) -> int:
    """Persist confirm/reject decisions. Payload snapshots come from the
    server-side candidate derivation (never from the client), so a re-import
    can't silently change what a confirmed fact asserts.

    Note for the UI: confirmed facts ENRICH tailoring (skills, summary,
    bullets of existing roles) — they never add new experience entries; the
    uploaded resume stays the structural source of truth.
    """
    from utils.security import InputSanitizer

    email = InputSanitizer.sanitize_string(user_email or "")
    if not email or not isinstance(decisions, list):
        return 0

    candidates, _ = _all_candidates(email)
    by_id = {c["fact_id"]: c for c in candidates}

    updates: Dict[str, Any] = {}
    now = datetime.now(timezone.utc)
    for d in decisions:
        if not isinstance(d, dict):
            continue
        fid = InputSanitizer.sanitize_string(str(d.get("fact_id") or ""))
        status = str(d.get("status") or "").strip().lower()
        if status not in _VALID_DECISIONS or fid not in by_id:
            continue
        cand = by_id[fid]
        updates[f"decisions.{fid}"] = {
            "status": status,
            "kind": cand["kind"],
            "payload": cand["payload"],
            "decided_at": now,
        }

    if not updates:
        return 0
    updates["updated_at"] = now
    _db()[CONFIRMATIONS_COLLECTION].update_one(
        {"user_email": email}, {"$set": updates}, upsert=True
    )
    return len(updates) - 1
