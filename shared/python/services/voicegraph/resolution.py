"""Entity resolution (§6.12.3 rule 3).

Merge the writing-sample graph with the user's resume-derived records
(skills / experiences / projects). A near-exact match auto-merges by adding a
`resume` evidence row to the graph node (co-evidence — this is what lets a node
survive a consent revocation) and marking it user_confirmed. An AMBIGUOUS match is
never guessed: it is recorded as a pending merge candidate for the user to confirm
or reject.

Similarity is lexical (difflib ratio + token Jaccard) — no embedding provider is
wired in this stack yet, and at a few hundred nodes per user lexical matching is
sufficient. Swapping in embeddings later is a drop-in change (ADR-002).
"""
import logging
from difflib import SequenceMatcher
from typing import Any, Dict, List, Tuple
from uuid import uuid4

from utils.db_connect import DBConnect

from services.voicegraph import store

logger = logging.getLogger(__name__)

AUTO_MERGE = 0.92
AMBIGUOUS = 0.75

# graph node type -> resume record bucket
_TYPE_BUCKET = {
    "Skill": "skills", "Technology": "skills",
    "Organization": "orgs", "Role": "roles", "Project": "projects",
}


def _col():
    return DBConnect().get_db().merge_candidates


def _norm(s: str) -> str:
    return " ".join((s or "").lower().split())


def _sim(a: str, b: str) -> float:
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ratio = SequenceMatcher(None, a, b).ratio()
    ta, tb = set(a.split()), set(b.split())
    jacc = len(ta & tb) / len(ta | tb) if (ta | tb) else 0.0
    return max(ratio, jacc)


def _resume_records(user_email: str) -> Dict[str, List[str]]:
    try:
        from services.resume_parser import ResumeParser
        doc = ResumeParser().get_structured_resume(user_email=user_email)
        s = (doc or {}).get("structured") or {}
    except Exception as e:
        logger.info("voicegraph resolution: no parsed resume (%s)", e)
        return {"skills": [], "orgs": [], "roles": [], "projects": []}

    skills: List[str] = []
    raw_skills = s.get("skills") or {}
    if isinstance(raw_skills, dict):
        for arr in raw_skills.values():
            if isinstance(arr, list):
                skills.extend(str(x) for x in arr)
    orgs = [e.get("company", "") for e in (s.get("experience") or []) if e.get("company")]
    roles = [e.get("title", "") for e in (s.get("experience") or []) if e.get("title")]
    projects = [p.get("name", "") for p in (s.get("projects") or []) if p.get("name")]
    return {"skills": skills, "orgs": orgs, "roles": roles, "projects": projects}


def _best(label: str, pool: List[str]) -> Tuple[str, float]:
    best, score = "", 0.0
    for cand in pool:
        s = _sim(label, cand)
        if s > score:
            best, score = cand, s
    return best, score


def resolve_entities(user_email: str) -> Dict[str, int]:
    records = _resume_records(user_email)
    if not any(records.values()):
        return {"auto_merged": 0, "pending": 0}

    graph = store.get_graph(user_email)
    auto = 0
    pending = 0
    for n in graph.get("nodes", []):
        bucket = _TYPE_BUCKET.get(n.get("type"))
        if not bucket:
            continue
        if n.get("user_confirmed"):
            continue
        best, score = _best(n.get("label", ""), records.get(bucket, []))
        if not best:
            continue
        if score >= AUTO_MERGE:
            store.add_evidence(user_email, "node", n["node_id"], [{
                "source": "resume", "source_id": "parsed_resume",
                "char_start": 0, "char_end": len(best), "quote": best,
            }])
            store.set_node_confirmed(user_email, n["node_id"], True)
            auto += 1
        elif score >= AMBIGUOUS:
            _col().update_one(
                {"user_email": user_email, "node_id": n["node_id"], "resume_value": best},
                {"$setOnInsert": {
                    "candidate_id": str(uuid4()),
                    "user_email": user_email,
                    "node_id": n["node_id"],
                    "node_label": n.get("label"),
                    "node_type": n.get("type"),
                    "resume_value": best,
                    "score": round(score, 3),
                    "status": "pending",
                }},
                upsert=True,
            )
            pending += 1
    return {"auto_merged": auto, "pending": pending}


def list_candidates(user_email: str) -> List[Dict[str, Any]]:
    out = []
    for c in _col().find({"user_email": user_email, "status": "pending"}):
        out.append({
            "candidate_id": c.get("candidate_id"),
            "node_id": c.get("node_id"),
            "node_label": c.get("node_label"),
            "node_type": c.get("node_type"),
            "resume_value": c.get("resume_value"),
            "score": c.get("score"),
        })
    return out


def resolve_candidate(user_email: str, candidate_id: str, accept: bool) -> bool:
    c = _col().find_one_and_update(
        {"candidate_id": candidate_id, "user_email": user_email, "status": "pending"},
        {"$set": {"status": "confirmed" if accept else "rejected"}},
    )
    if not c:
        return False
    if accept:
        # The node may have been removed by a re-extraction since the candidate was
        # created; don't resurrect evidence for a node that no longer exists.
        if not store.get_node(user_email, c["node_id"]):
            return False
        val = c.get("resume_value", "")
        store.add_evidence(user_email, "node", c["node_id"], [{
            "source": "resume", "source_id": "parsed_resume",
            "char_start": 0, "char_end": len(val), "quote": val,
        }])
        store.set_node_confirmed(user_email, c["node_id"], True)
    return True
