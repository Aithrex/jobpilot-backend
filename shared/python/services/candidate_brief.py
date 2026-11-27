"""Candidate brief: one markdown profile per user, built from what Aspirely holds.

The ApplyPilot extension and the answer generator both need "everything we know
about this person that a model may see". The brief is that, written once and
reused:

  * Sources: the ACTIVE parsed resume (resume_access.get_active_structured) and
    the user's declared application answers in ``user_preferences`` (only an
    allow-list: work authorization, sponsorship, logistics, links). Nothing else.
  * Never included: EEO / demographics (gender, race, veteran, disability),
    legal attestations, criminal history, contact details (email, phone,
    street address), passwords, tokens, and anything from Gmail.
  * Every bullet is a FACT with a STABLE id under ``srv.`` (``srv.exp[0].title``,
    ``srv.exp[0].highlights[2]``, ``srv.auth.sponsorship_now``), so an answer
    can cite it and both the server and the extension can verify the citation.
  * Stored in ``candidate_briefs`` keyed by email, with a hash of its sources.
    The read path rebuilds only when that hash changes, so a request costs one
    Mongo read plus one hash.

Why it saves tokens and time: the answer prompt puts the brief FIRST (after the
fixed instructions), identical for every question this person asks, so Vertex
AI's implicit context cache serves that prefix (``cached_tokens`` in the
llm_usage log line); only the job and the question at the end are new input.

Pure functions (``build_brief``) are separated from Mongo (``get_brief``) so the
building rules are testable without a database.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

COLLECTION = "candidate_briefs"
BRIEF_VERSION = "brief-1"
ID_PREFIX = "srv."

MAX_FACT_TEXT = 500

# Declared answers that belong in the brief: field_id -> (fact id, label).
# Everything not listed here (EEO, legal attestations, contact, screening
# employer questions) stays out by construction.
_DECLARED: List[Tuple[str, str, str]] = [
    ("work_authorized_us", "srv.auth.authorized_us", "Authorized to work in the US"),
    ("requires_sponsorship", "srv.auth.sponsorship_now", "Needs visa sponsorship now"),
    ("requires_sponsorship_future", "srv.auth.sponsorship_future", "Will need visa sponsorship in the future"),
    ("visa_status", "srv.auth.visa_status", "Visa status"),
    ("work_auth_country", "srv.auth.country", "Authorized to work in"),
    ("willing_to_relocate", "srv.pref.relocate", "Willing to relocate"),
    ("remote_preference", "srv.pref.remote", "Remote preference"),
    ("notice_period", "srv.pref.notice_period", "Notice period"),
    ("earliest_start_date", "srv.pref.start_date", "Earliest start date"),
    ("willing_to_travel", "srv.pref.travel", "Willing to travel"),
    ("current_location", "srv.pref.location", "Current location"),
    ("desired_salary", "srv.pref.desired_salary", "Desired salary"),
    ("linkedin_url", "srv.link.linkedin", "LinkedIn"),
    ("github_url", "srv.link.github", "GitHub"),
    ("portfolio_url", "srv.link.portfolio", "Portfolio"),
    ("website_url", "srv.link.website", "Website"),
]

# Defence in depth: keys that must never reach the brief even if a future edit
# adds them to the allow-list above.
_FORBIDDEN = frozenset({
    "gender", "hispanic_latino", "race_ethnicity", "veteran_status",
    "disability_status", "pronouns", "criminal_history", "email", "phone",
    "address_line1", "address_line2", "postal_code", "current_salary",
})


def _db():
    from utils.db_connect import DBConnect
    return DBConnect().get_db()


def _clean(text: Any) -> str:
    if text is None:
        return ""
    if isinstance(text, bool):
        return "Yes" if text else "No"
    s = " ".join(str(text).split())
    return s[:MAX_FACT_TEXT]


def _redact(text: str) -> str:
    try:
        from utils.llm_safety import redact_pii_for_model
        return redact_pii_for_model(text)
    except Exception:  # pragma: no cover - defensive; never block on redaction
        return text


def _skills(structured: Dict[str, Any]) -> List[str]:
    try:
        from schemas.resume_schemas import flatten_skills
        return [s for s in flatten_skills(structured) if isinstance(s, str) and s.strip()]
    except Exception:  # pragma: no cover
        raw = structured.get("skills") or []
        return [s for s in raw if isinstance(s, str) and s.strip()] if isinstance(raw, list) else []


def build_brief(structured: Optional[Dict[str, Any]],
                declared: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Pure: the brief's facts and markdown from a structured resume and the
    user's declared answers. Same input, same output (stable ids and order)."""
    structured = structured or {}
    declared = declared or {}
    facts: List[Dict[str, str]] = []
    md: List[str] = ["# Candidate brief", ""]

    def add(fid: str, kind: str, text: str) -> None:
        text = _redact(_clean(text))
        if text:
            facts.append({"id": fid, "kind": kind, "text": text})
            md.append(f"- [{fid}] {text}")

    summary = _clean(structured.get("summary"))
    if summary:
        md += ["## Summary"]
        add("srv.summary", "summary", summary)
        md.append("")

    exps = [e for e in (structured.get("experience") or []) if isinstance(e, dict)]
    if exps:
        md.append("## Experience")
        for i, e in enumerate(exps[:12]):
            title = _clean(e.get("title"))
            company = _clean(e.get("company"))
            dates = _clean(e.get("dates"))
            loc = _clean(e.get("location"))
            head = " at ".join(p for p in (title, company) if p)
            if dates:
                head += f" ({dates})"
            if loc:
                head += f", {loc}"
            md.append(f"### {head or 'Role'}")
            add(f"srv.exp[{i}].title", "experience", head)
            for j, b in enumerate((e.get("bullets") or e.get("highlights") or [])[:12]):
                if isinstance(b, str):
                    add(f"srv.exp[{i}].highlights[{j}]", "experience", b)
        md.append("")

    edus = [e for e in (structured.get("education") or []) if isinstance(e, dict)]
    if edus:
        md.append("## Education")
        for i, e in enumerate(edus[:6]):
            parts = [_clean(e.get("degree")), _clean(e.get("field") or e.get("major")),
                     _clean(e.get("institution") or e.get("school"))]
            line = ", ".join(p for p in parts if p)
            if _clean(e.get("dates")):
                line += f" ({_clean(e.get('dates'))})"
            if _clean(e.get("gpa")):
                line += f", GPA {_clean(e.get('gpa'))}"
            add(f"srv.edu[{i}]", "education", line)
        md.append("")

    projs = [p for p in (structured.get("projects") or []) if isinstance(p, dict)]
    if projs:
        md.append("## Projects")
        for i, p in enumerate(projs[:8]):
            name = _clean(p.get("name"))
            tech = _clean(p.get("tech"))
            add(f"srv.proj[{i}].name", "project", name + (f" (tech: {tech})" if tech else ""))
            for j, b in enumerate((p.get("bullets") or [])[:8]):
                if isinstance(b, str):
                    add(f"srv.proj[{i}].highlights[{j}]", "project", b)
        md.append("")

    skills = _skills(structured)
    if skills:
        md.append("## Skills")
        # Chunks of 25 so one fact stays under the text cap.
        for k in range(0, min(len(skills), 150), 25):
            add(f"srv.skills[{k // 25}]", "skills", ", ".join(skills[k:k + 25]))
        md.append("")

    certs = [c for c in (structured.get("certifications") or []) if isinstance(c, str) and c.strip()]
    if certs:
        md.append("## Certifications")
        for i, c in enumerate(certs[:20]):
            add(f"srv.cert[{i}]", "certification", c)
        md.append("")

    rows = [(fid, label, declared.get(key)) for key, fid, label in _DECLARED
            if key not in _FORBIDDEN and declared.get(key) not in (None, "", [])]
    if rows:
        md.append("## Application answers")
        for fid, label, value in rows:
            kind = "work_auth" if fid.startswith("srv.auth.") else "preference"
            add(fid, kind, f"{label}: {_clean(value)}")
        md.append("")

    text = "\n".join(md).rstrip() + "\n"
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    return {"md": text, "facts": facts, "hash": digest, "version": BRIEF_VERSION}


def _declared_for(email: str) -> Dict[str, Any]:
    try:
        doc = _db().user_preferences.find_one({"user_email": email}) or {}
    except Exception as e:  # pragma: no cover
        logger.warning("candidate_brief: prefs read failed: %s", type(e).__name__)
        return {}
    keys = {k for k, _, _ in _DECLARED} - _FORBIDDEN
    return {k: doc.get(k) for k in keys if k in doc}


def _structured_for(email: str) -> Dict[str, Any]:
    try:
        from services import resume_access
        asset = resume_access.get_active_structured(email) or {}
    except Exception as e:  # pragma: no cover
        logger.warning("candidate_brief: resume read failed: %s", type(e).__name__)
        return {}
    s = asset.get("structured")
    return s if isinstance(s, dict) else {}


def _sources_hash(structured: Dict[str, Any], declared: Dict[str, Any]) -> str:
    blob = json.dumps({"v": BRIEF_VERSION, "r": structured, "d": declared},
                      sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def get_brief(email: str, *, now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
    """The user's brief, rebuilt only when its sources changed. None when the
    user has neither a parsed resume nor declared answers."""
    if not email or not isinstance(email, str):
        return None
    structured = _structured_for(email)
    declared = _declared_for(email)
    if not structured and not declared:
        return None
    src = _sources_hash(structured, declared)
    coll = _db()[COLLECTION]
    try:
        cached = coll.find_one({"_id": email})
    except Exception:  # pragma: no cover
        cached = None
    if cached and cached.get("sources_hash") == src:
        return _public(cached)
    built = build_brief(structured, declared)
    doc = {
        "_id": email,
        "md": built["md"],
        "facts": built["facts"],
        "hash": built["hash"],
        "version": BRIEF_VERSION,
        "sources_hash": src,
        "built_at": now or datetime.now(timezone.utc),
    }
    try:
        coll.replace_one({"_id": email}, doc, upsert=True)
    except Exception as e:  # pragma: no cover - a write failure still serves the brief
        logger.warning("candidate_brief: store failed: %s", type(e).__name__)
    return _public(doc)


def _public(doc: Dict[str, Any]) -> Dict[str, Any]:
    built = doc.get("built_at")
    if isinstance(built, datetime):
        if built.tzinfo is None:
            built = built.replace(tzinfo=timezone.utc)
        built = built.isoformat()
    return {"md": doc.get("md", ""), "facts": list(doc.get("facts") or []),
            "hash": doc.get("hash", ""), "built_at": built}


# ---------------------------------------------------------------------------
# Facts merge and the answer cache (used by POST /api/apply/answer)
# ---------------------------------------------------------------------------
CACHE_COLLECTION = "answer_cache"
CACHE_TTL_SECONDS = 30 * 24 * 3600
MAX_MERGED_FACTS = 220
_cache_indexed = False


def _norm(text: str) -> str:
    return " ".join(str(text or "").lower().split())


def merge_facts(client: List[Dict[str, str]], brief: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """Client facts first (their ids are what the extension verifies), then any
    brief fact whose text the client did not already send."""
    out = list(client or [])
    seen_text = {_norm(f.get("text", "")) for f in out}
    seen_id = {f.get("id") for f in out}
    for f in brief or []:
        if len(out) >= MAX_MERGED_FACTS:
            break
        if f.get("id") in seen_id or _norm(f.get("text", "")) in seen_text:
            continue
        out.append({"id": f["id"], "kind": f.get("kind", ""), "text": f["text"]})
        seen_text.add(_norm(f["text"]))
        seen_id.add(f["id"])
    return out


def cache_key(email: str, brief_hash: str, question: str, company: str, role: str,
              facts: List[Dict[str, str]]) -> str:
    """sha256 over who asks, which brief, the normalized question, the job and
    the exact facts in play (a changed profile or job never reuses an answer)."""
    facts_digest = hashlib.sha256(
        json.dumps([[f.get("id"), f.get("text")] for f in facts], sort_keys=True).encode("utf-8")
    ).hexdigest()
    raw = "\x1f".join([email, brief_hash or "", _norm(question), _norm(company),
                       _norm(role), facts_digest])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _cache():
    global _cache_indexed
    coll = _db()[CACHE_COLLECTION]
    if not _cache_indexed:
        try:
            coll.create_index("created_at", expireAfterSeconds=CACHE_TTL_SECONDS)
        except Exception as e:  # pragma: no cover
            logger.info("answer_cache: index create soft-fail: %s", type(e).__name__)
        _cache_indexed = True
    return coll


def cache_get(key: str) -> Optional[List[Dict[str, Any]]]:
    try:
        doc = _cache().find_one({"_id": key})
    except Exception:  # pragma: no cover
        return None
    v = (doc or {}).get("variants")
    return v if isinstance(v, list) and v else None


def cache_put(key: str, email: str, variants: List[Dict[str, Any]],
              now: Optional[datetime] = None) -> None:
    try:
        _cache().replace_one({"_id": key}, {
            "_id": key, "user_email": email, "variants": variants,
            "created_at": now or datetime.now(timezone.utc),
        }, upsert=True)
    except Exception as e:  # pragma: no cover - a cache write never fails a request
        logger.info("answer_cache: write soft-fail: %s", type(e).__name__)


def cited_server_facts(variants: List[Dict[str, Any]],
                       facts: List[Dict[str, str]]) -> Dict[str, str]:
    """{id: text} for every srv.* fact any returned variant cites, so the
    extension can verify a citation to a fact it never sent."""
    by_id = {f["id"]: f["text"] for f in facts}
    out: Dict[str, str] = {}
    for v in variants:
        for fid in v.get("used_fact_ids") or []:
            if isinstance(fid, str) and fid.startswith(ID_PREFIX) and fid in by_id:
                out[fid] = by_id[fid]
    return out
