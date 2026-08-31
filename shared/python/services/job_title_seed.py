"""Job-search title seed across ALL parsed resumes, cached by resume-set fingerprint.

Union of ``job_titles`` on each parsed ``resume_assets`` row is free (already
extracted at parse time). The LLM title synthesizer (``suggest_filters``)
runs ONLY when the resume set fingerprint changes AND we have no stored
synth for that fingerprint. Upload / delete / reparse changes the
fingerprint; browsing Jobs does not.
"""
from __future__ import annotations

import hashlib
import logging
import threading
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from services import resume_access
from utils.db_connect import DBConnect

logger = logging.getLogger(__name__)

TITLE_CAP = 40
_COL = "job_filter_preferences"

# Same-container lock so Jobs hydrate + Daily Pipeline mounting together
# cannot fire two PRO synths for one user. Cross-Lambda races are still
# possible; the fingerprint re-check immediately before the LLM call
# collapses most of those.
_synth_locks: Dict[str, threading.Lock] = {}
_synth_locks_guard = threading.Lock()


def _lock_for(user_email: str) -> threading.Lock:
    with _synth_locks_guard:
        return _synth_locks.setdefault(user_email, threading.Lock())


def _db():
    return DBConnect().get_db()


def _iso(value: Any) -> str:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value or "")


def fingerprint_assets(assets: List[Dict[str, Any]]) -> str:
    """Stable id of the parsed-resume set. Changes iff an asset is added,
    removed, or re-parsed."""
    parts = []
    for asset in sorted(assets, key=lambda a: str(a.get("resume_id") or "")):
        rid = str(asset.get("resume_id") or "")
        if not rid:
            continue
        parts.append(f"{rid}:{_iso(asset.get('parsed_at'))}")
    if not parts:
        return ""
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:24]


def union_titles_from_assets(assets: List[Dict[str, Any]]) -> List[str]:
    """Case-insensitive merge of stored job_titles, newest assets first."""
    seen: set = set()
    out: List[str] = []
    for asset in assets:
        raw = asset.get("job_titles") or []
        if not raw:
            structured = asset.get("structured") or {}
            if isinstance(structured, dict):
                from schemas.resume_schemas import extract_job_titles

                raw = extract_job_titles(structured)
        if not isinstance(raw, list):
            continue
        for title in raw:
            text = str(title or "").strip()
            if not text:
                continue
            key = text.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(text)
            if len(out) >= TITLE_CAP:
                return out
    return out


def merge_structured_for_titles(assets: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """One structured doc covering every parsed resume — one LLM call, not N."""
    if not assets:
        return None
    summaries: List[str] = []
    experience: List[Dict[str, Any]] = []
    projects: List[Dict[str, Any]] = []
    education: List[Dict[str, Any]] = []
    certifications: List[str] = []
    skills: Dict[str, List[str]] = {}
    seen_exp: set = set()
    seen_proj: set = set()
    seen_edu: set = set()
    seen_cert: set = set()
    seen_skill: Dict[str, set] = {}

    for asset in assets:
        structured = asset.get("structured")
        if not isinstance(structured, dict):
            continue
        fname = (asset.get("filename") or "resume").strip()
        summary = (structured.get("summary") or "").strip()
        if summary:
            summaries.append(f"{fname}: {summary[:400]}")
        for exp in structured.get("experience") or []:
            if not isinstance(exp, dict):
                continue
            key = (
                (exp.get("title") or "").strip().lower(),
                (exp.get("company") or "").strip().lower(),
            )
            if not key[0] or key in seen_exp:
                continue
            seen_exp.add(key)
            experience.append(exp)
        for proj in structured.get("projects") or []:
            if not isinstance(proj, dict):
                continue
            key = (proj.get("name") or "").strip().lower()
            if not key or key in seen_proj:
                continue
            seen_proj.add(key)
            projects.append(proj)
        for edu in structured.get("education") or []:
            if not isinstance(edu, dict):
                continue
            key = (
                (edu.get("degree") or "").strip().lower(),
                (edu.get("institution") or "").strip().lower(),
            )
            if key in seen_edu:
                continue
            seen_edu.add(key)
            education.append(edu)
        for cert in structured.get("certifications") or []:
            text = str(cert).strip()
            if not text:
                continue
            key = text.lower()
            if key in seen_cert:
                continue
            seen_cert.add(key)
            certifications.append(text)
        raw_skills = structured.get("skills") or {}
        if isinstance(raw_skills, dict):
            for cat, items in raw_skills.items():
                if not isinstance(items, list):
                    continue
                bucket = skills.setdefault(str(cat), [])
                seen_cat = seen_skill.setdefault(str(cat), set())
                for item in items:
                    text = str(item).strip()
                    if not text or text.lower() in seen_cat:
                        continue
                    seen_cat.add(text.lower())
                    bucket.append(text)
        elif isinstance(raw_skills, list):
            bucket = skills.setdefault("Skills", [])
            seen_cat = seen_skill.setdefault("Skills", set())
            for item in raw_skills:
                text = str(item).strip()
                if not text or text.lower() in seen_cat:
                    continue
                seen_cat.add(text.lower())
                bucket.append(text)

    if not experience and not summaries:
        return None
    return {
        "summary": " | ".join(summaries)[:1200],
        "experience": experience[:16],
        "projects": projects[:10],
        "education": education[:6],
        "certifications": certifications[:12],
        "skills": skills,
    }


def load_title_seed(user_email: str) -> Dict[str, Any]:
    if not user_email:
        return {}
    doc = _db()[_COL].find_one({"user_email": user_email}, {"title_seed": 1}) or {}
    seed = doc.get("title_seed")
    return dict(seed) if isinstance(seed, dict) else {}


def save_title_seed(user_email: str, seed: Dict[str, Any]) -> None:
    if not user_email:
        return
    payload = dict(seed)
    payload["updated_at"] = datetime.now(timezone.utc)
    _db()[_COL].update_one(
        {"user_email": user_email},
        {"$set": {"title_seed": payload, "user_email": user_email}},
        upsert=True,
    )


def _persist_union(user_email: str, fp: str, resume_titles: List[str], stored: Dict[str, Any]) -> None:
    """Write fingerprint + union only. Never clobber a concurrent synth.

    When the resume set actually changed, drop the stale synth so the next
    ``suggest_filters_cached`` re-runs. A first-visit GET (no prior synth)
    only $sets the union fields so a parallel suggest can still land.
    """
    now = datetime.now(timezone.utc)
    patch: Dict[str, Any] = {
        "user_email": user_email,
        "title_seed.fingerprint": fp,
        "title_seed.resume_titles": resume_titles,
        "title_seed.union_updated_at": now,
    }
    update: Dict[str, Any] = {"$set": patch}
    prior_synth_fp = stored.get("synth_fingerprint")
    if prior_synth_fp and prior_synth_fp != fp:
        update["$unset"] = {
            "title_seed.suggestions": "",
            "title_seed.synth_titles": "",
            "title_seed.synth_fingerprint": "",
        }
    _db()[_COL].update_one({"user_email": user_email}, update, upsert=True)


def public_title_seed(user_email: str) -> Dict[str, Any]:
    """Cheap GET payload. Recomputes the union; never calls the LLM.

    When the fingerprint matches the stored synth, ``synth_titles`` come from
    cache. When resumes changed, synth_titles is empty until the next
    ``suggest_filters_cached`` (or stays empty if the union is already thick).
    """
    assets = resume_access.list_parsed_assets(user_email)
    fp = fingerprint_assets(assets)
    resume_titles = union_titles_from_assets(assets)
    stored = load_title_seed(user_email)
    synth: List[str] = []
    if stored.get("synth_fingerprint") == fp:
        raw = stored.get("synth_titles") or []
        if isinstance(raw, list):
            synth = [str(t).strip() for t in raw if str(t).strip()][:TITLE_CAP]
    if stored.get("fingerprint") != fp or stored.get("resume_titles") != resume_titles:
        _persist_union(user_email, fp, resume_titles, stored)
    return {
        "fingerprint": fp,
        "resume_titles": resume_titles,
        "synth_titles": synth,
        "resume_count": len(assets),
        "cached": True,
    }


def _cached_suggestions(stored: Dict[str, Any], fp: str) -> Optional[Dict[str, Any]]:
    suggestions = stored.get("suggestions")
    if (
        stored.get("synth_fingerprint") == fp
        and isinstance(suggestions, dict)
        and (suggestions.get("workday_titles") or suggestions.get("linkedin_keyword_sets"))
    ):
        return suggestions
    return None


def suggest_filters_cached(
    user_email: str,
    synthesizer: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
) -> Tuple[Dict[str, Any], bool]:
    """Resume-aware filter suggestions, LLM only on fingerprint miss.

    ``synthesizer`` is injected by the jobs route (``suggest_filters``) so
    this shared module never imports the jobs-Lambda-local suggester.

    Returns ``(suggestions, from_cache)``. No parsed resume → ValueError
    (same contract as the route's 404).
    """
    assets = resume_access.list_parsed_assets(user_email)
    if not assets:
        raise ValueError("No resume found. Upload a resume to enable smart filters.")
    fp = fingerprint_assets(assets)

    with _lock_for(user_email):
        stored = load_title_seed(user_email)
        hit = _cached_suggestions(stored, fp)
        if hit is not None:
            logger.info("title_seed: cache hit fingerprint=%s user=%s", fp, user_email)
            return hit, True

        merged = merge_structured_for_titles(assets)
        if not merged:
            raise ValueError("No resume found. Upload a resume to enable smart filters.")

        if synthesizer is None:
            # Jobs Lambda only. Kept lazy so resume/auth Lambdas can import
            # public_title_seed without the domain-local suggester.
            from services.filter_suggester_service import suggest_filters as synthesizer

        suggestions = synthesizer(merged)
        resume_titles = union_titles_from_assets(assets)
        synth_titles = [
            str(t).strip()
            for t in (suggestions.get("workday_titles") or [])
            if str(t).strip()
        ][:TITLE_CAP]
        save_title_seed(user_email, {
            "fingerprint": fp,
            "resume_titles": resume_titles,
            "synth_titles": synth_titles,
            "synth_fingerprint": fp,
            "suggestions": suggestions,
        })
        logger.info(
            "title_seed: synthesized fingerprint=%s titles=%d user=%s",
            fp, len(synth_titles), user_email,
        )
        return suggestions, False
