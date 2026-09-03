"""Evidence-backed company intake that does not mint canonical identities.

A candidate is a LEAD: name, domain, career URL, ATS guess, discovery method
and the evidence that produced it. Promotion into `employers` happens only
through `backfill_employer_registry` / ownership verification. Two candidates
with similar names stay two rows.
"""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

CANDIDATES = "company_discovery_candidates"
INTAKE_VERSION = "company-intake-1.0.0"

STATE_CANDIDATE = "candidate"
STATE_BLOCKED = "blocked"
STATE_MOVED = "moved"
STATE_DEAD = "dead"
STATE_VERIFIED = "verified"
STATE_REJECTED = "rejected"
STATE_PROMOTED = "promoted"

DISCOVERY_METHODS = frozenset({
    "curated_registry",
    "user_request",
    "repeated_postings",
    "approved_provider_api",
    "operator_review",
})


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _db():
    from utils.db_connect import DBConnect
    return DBConnect().get_db()


def ensure_indexes(*, db=None) -> List[str]:
    db = db if db is not None else _db()
    created: List[str] = []
    try:
        col = db[CANDIDATES]
        created.append(col.create_index(
            [("intake_key", 1)], unique=True, name="intake_key_unique"))
        created.append(col.create_index(
            [("verification_state", 1), ("updated_at", -1)],
            name="intake_state_recent"))
        created.append(col.create_index(
            [("owner_domain", 1)], name="intake_domain"))
    except Exception as exc:  # pragma: no cover
        logger.warning("company_discovery_intake.ensure_indexes: %s", exc)
    return [c for c in created if c]


def _norm_name(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())[:120]


def _norm_domain(value: Any) -> str:
    raw = str(value or "").strip().lower()
    raw = re.sub(r"^https?://", "", raw)
    raw = raw.split("/")[0].split(":")[0]
    raw = raw[4:] if raw.startswith("www.") else raw
    return raw[:253]


def intake_key(*, owner_domain: str, career_url: str = "",
               board_url: str = "") -> str:
    """Identity of a CANDIDATE, domain-first. Never a name hash."""
    domain = _norm_domain(owner_domain)
    host = ""
    for url in (career_url, board_url):
        try:
            host = (urlparse(str(url or "")).hostname or "")
        except Exception:
            host = ""
        if host:
            break
    host = host.lower().lstrip("www.")
    material = f"domain:{domain}|host:{host}"
    return "cand_" + hashlib.sha1(material.encode("utf-8")).hexdigest()[:12]


def upsert_candidate(payload: Dict[str, Any], *, db=None) -> Dict[str, Any]:
    """Additive upsert. Will not merge two rows that only share a name."""
    db = db if db is not None else _db()
    ensure_indexes(db=db)
    name = _norm_name(payload.get("company_name") or payload.get("display_name"))
    domain = _norm_domain(payload.get("owner_domain") or payload.get("domain"))
    if not name or not domain:
        raise ValueError("company_name and owner_domain are required")
    career = str(payload.get("official_career_url")
                 or payload.get("career_url") or "").strip()
    board = str(payload.get("candidate_board_url")
                or payload.get("board_url") or "").strip()
    key = payload.get("intake_key") or intake_key(
        owner_domain=domain, career_url=career, board_url=board)
    method = str(payload.get("discovery_method") or "operator_review")
    if method not in DISCOVERY_METHODS:
        method = "operator_review"
    now = _now()
    existing = db[CANDIDATES].find_one({"intake_key": key}) or {}
    # Name-only match against a DIFFERENT domain is a flag, never a merge.
    name_key = re.sub(r"[^a-z0-9]+", "", name.lower())
    collisions = list(db[CANDIDATES].find(
        {"name_key": name_key, "intake_key": {"$ne": key}},
        {"_id": 0, "intake_key": 1, "company_name": 1, "owner_domain": 1}))
    doc = {
        "intake_key": key,
        "company_name": name,
        "name_key": name_key,
        "owner_domain": domain,
        "official_company_url": str(payload.get("official_company_url") or "").strip(),
        "official_career_url": career,
        "candidate_ats": str(payload.get("candidate_ats") or "").strip().lower(),
        "candidate_board_token": str(payload.get("candidate_board_token") or "").strip(),
        "candidate_board_url": board,
        "discovery_method": method,
        "evidence_url": str(payload.get("evidence_url") or "").strip(),
        "evidence_at": payload.get("evidence_at") or now,
        "verification_state": str(payload.get("verification_state")
                                  or existing.get("verification_state")
                                  or STATE_CANDIDATE),
        "verification_reason": str(payload.get("verification_reason") or ""),
        "cohort": str(payload.get("cohort") or ""),
        "tech_categories": list(payload.get("tech_categories") or ()),
        "retry_eligible": bool(payload.get("retry_eligible", True)),
        "redirect_history": list(payload.get("redirect_history")
                                 or existing.get("redirect_history") or ()),
        "possible_name_collisions": collisions[:8],
        "last_checked_at": payload.get("last_checked_at"),
        "intake_version": INTAKE_VERSION,
        "updated_at": now,
    }
    if payload.get("canonical_employer_id"):
        # Record a LINK, never mint one here.
        doc["linked_employer_id"] = str(payload["canonical_employer_id"])
    db[CANDIDATES].update_one(
        {"intake_key": key},
        {"$set": doc, "$setOnInsert": {"created_at": now}},
        upsert=True)
    return db[CANDIDATES].find_one({"intake_key": key}, {"_id": 0}) or doc


def seed_researched_intake(*, db=None) -> List[Dict[str, Any]]:
    """The four 2026-09-03 researched leads. Does not create employers."""
    now = _now()
    rows = [
        dict(company_name="Safe Superintelligence", owner_domain="ssi.inc",
             official_company_url="https://ssi.inc/",
             official_career_url="https://ssi.inc/",
             candidate_ats="ashby", candidate_board_token="ssi",
             candidate_board_url="https://jobs.ashbyhq.com/ssi",
             discovery_method="curated_registry",
             evidence_url="https://ssi.inc/", evidence_at=now,
             verification_state=STATE_CANDIDATE,
             verification_reason="Official homepage recorded; Ashby board is "
                                 "a candidate until the first-party link is confirmed.",
             cohort="niche_startup", tech_categories=["ai_ml", "research"]),
        dict(company_name="Thinking Machines Lab",
             owner_domain="thinkingmachines.ai",
             official_company_url="https://thinkingmachines.ai/",
             official_career_url="https://thinkingmachines.ai/",
             candidate_ats="ashby", candidate_board_token="thinkingmachines",
             candidate_board_url="https://jobs.ashbyhq.com/thinkingmachines",
             discovery_method="curated_registry",
             evidence_url="https://thinkingmachines.ai/", evidence_at=now,
             verification_state=STATE_CANDIDATE,
             verification_reason="Greenhouse board observed as moved/stale. "
                                 "Ashby is not promoted without ownership evidence.",
             cohort="niche_startup", tech_categories=["ai_ml", "research"],
             redirect_history=[{
                 "from": "greenhouse", "state": STATE_MOVED,
                 "reason": "Official site previously linked an obsolete Greenhouse board.",
                 "at": now.isoformat()}]),
        dict(company_name="Periodic Labs", owner_domain="periodic.com",
             official_company_url="https://periodic.com/",
             official_career_url="https://periodic.com/",
             candidate_ats="ashby", candidate_board_token="periodic-labs",
             candidate_board_url="https://jobs.ashbyhq.com/periodic-labs",
             discovery_method="curated_registry",
             evidence_url="https://periodic.com/", evidence_at=now,
             verification_state=STATE_CANDIDATE,
             verification_reason="Official site was observed linking to the Ashby board.",
             cohort="niche_startup", tech_categories=["ai_ml", "research"]),
        dict(company_name="OpenEvidence", owner_domain="openevidence.com",
             official_company_url="https://www.openevidence.com/",
             official_career_url="https://www.openevidence.com/",
             candidate_ats="ashby", candidate_board_token="openevidence",
             candidate_board_url="https://jobs.ashbyhq.com/openevidence",
             discovery_method="curated_registry",
             evidence_url="https://www.openevidence.com/", evidence_at=now,
             verification_state=STATE_BLOCKED,
             verification_reason="First-party site blocked automated inspection. "
                                 "Keep blocked until ownership is corroborated. "
                                 "Not open-evidence.com.",
             retry_eligible=True,
             cohort="private_growth", tech_categories=["healthtech", "ai_ml"]),
    ]
    return [upsert_candidate(row, db=db) for row in rows]


def public_row(doc: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "intake_key": doc.get("intake_key"),
        "company_name": doc.get("company_name"),
        "owner_domain": doc.get("owner_domain"),
        "verification_state": doc.get("verification_state"),
        "discovery_method": doc.get("discovery_method"),
        "retry_eligible": bool(doc.get("retry_eligible")),
        "updated_at": (doc.get("updated_at").isoformat()
                       if isinstance(doc.get("updated_at"), datetime)
                       else doc.get("updated_at")),
    }


def list_for_ops(*, limit: int = 50, db=None) -> List[Dict[str, Any]]:
    db = db if db is not None else _db()
    rows = db[CANDIDATES].find({}, {"_id": 0}).sort("updated_at", -1).limit(int(limit))
    return [public_row(r) for r in rows]


def observe_repeated_postings(*, min_postings: int = 8, db=None) -> int:
    """Queue employers seen often in inventory that have no registry row.

    Does not create canonical ids. Keys on posting domain when present, else
    skips the row (a name alone is not identity).
    """
    db = db if db is not None else _db()
    added = 0
    known = {str(r.get("canonical_domain") or "").lower()
             for r in db.employers.find({}, {"canonical_domain": 1})}
    for row in db.job_postings.aggregate([
            {"$match": {"status": "active", "employer_domain": {"$nin": [None, ""]}}},
            {"$group": {"_id": "$employer_domain", "n": {"$sum": 1},
                        "name": {"$first": "$employer_name"}}},
            {"$match": {"n": {"$gte": int(min_postings)}}}], allowDiskUse=True):
        domain = _norm_domain(row["_id"])
        if not domain or domain in known:
            continue
        upsert_candidate({
            "company_name": row.get("name") or domain,
            "owner_domain": domain,
            "discovery_method": "repeated_postings",
            "verification_state": STATE_CANDIDATE,
            "verification_reason": f"Observed on {int(row['n'])} active postings.",
            "evidence_url": f"domain:{domain}",
        }, db=db)
        added += 1
    return added
