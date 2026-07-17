"""Guest composite-identity resolution (soft, never-blocking).

Guest Onboarding spec (docs/specs/guest-onboarding.md §Identity resolution):
a guest is recognized as "the same person" only when **2+ DISTINCT signal
types** match an existing guest session:

    name_normalized · resume email(s) · phone(s) (digits-only) ·
    resume content hash · device fingerprint

Name alone is a signal, never the key (namesakes are safe). Resolution is
SOFT: it merges quota + recognition across the cluster; it never refuses
service by itself.

Cluster model: `merged_into` on the NEWER session points at the canonical
(oldest) session. Effective tailors-used for a cluster is the SUM across all
member sessions — prevents quota splitting by rotating fingerprints/uploads.

Security: every value used in a Mongo filter is sanitized via InputSanitizer
first and only appears in VALUE position of exact-match / $in filters with
hardcoded field names (hard invariant #4). No resume PII (names/emails/
phones) is ever logged — only guest_ids and counts.
"""
from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from utils.db_connect import DBConnect
from utils.security import InputSanitizer

logger = logging.getLogger(__name__)

# Signal types considered for the 2-of-N rule. fingerprint counts as a type.
SIGNAL_TYPES = ("name", "email", "phone", "resume_hash", "fingerprint")

_FINGERPRINT_SAFE = re.compile(r"[^A-Za-z0-9._:-]")
_MAX_CLUSTER_HOPS = 10  # merged_into chain bound (defensive vs. cycles)


def _sessions():
    return DBConnect().get_db().guest_sessions


# ---------------------------------------------------------------------------
# Signal extraction / normalization
# ---------------------------------------------------------------------------

def normalize_name(name: str) -> str:
    """Lowercase, unicode-folded (NFKD, accents stripped), collapsed whitespace."""
    if not name:
        return ""
    name = InputSanitizer.sanitize_string(str(name), max_length=200)
    folded = unicodedata.normalize("NFKD", name)
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    return " ".join(folded.lower().split())


def normalize_phone(phone: str) -> str:
    """Digits-only normalization. Drops anything shorter than 7 digits."""
    digits = re.sub(r"\D", "", str(phone or ""))
    return digits if len(digits) >= 7 else ""


def sanitize_fingerprint(fingerprint_hash: Optional[str]) -> str:
    """FE-supplied device fingerprint: sanitized, charset-restricted, <=128."""
    if not fingerprint_hash:
        return ""
    fp = InputSanitizer.sanitize_string(str(fingerprint_hash), max_length=128)
    return _FINGERPRINT_SAFE.sub("", fp)


def resume_content_hash(raw_text: str, structured: Optional[Dict[str, Any]] = None) -> str:
    """sha256 of the canonicalized resume text (lowercased, whitespace-collapsed).

    Falls back to a stable serialization of the structured resume when no raw
    text is available, so the signal never silently disappears.
    """
    text = raw_text or ""
    if not text.strip() and structured:
        import json
        text = json.dumps(structured, sort_keys=True, default=str)
    canonical = " ".join(str(text).lower().split())
    if not canonical:
        return ""
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def extract_signals(
    parsed_resume: Optional[Dict[str, Any]],
    fingerprint_hash: Optional[str] = None,
    raw_text: str = "",
) -> Dict[str, Any]:
    """Extract the composite-identity signals from a parsed resume + device.

    Returns {name_normalized, emails, phones, resume_hash, fingerprint} —
    every element already sanitized/normalized and safe as a Mongo VALUE.
    """
    structured = parsed_resume or {}
    contact = structured.get("contact") or {}

    name_normalized = normalize_name(contact.get("name") or "")

    emails: List[str] = []
    raw_emails = contact.get("email")
    candidates = raw_emails if isinstance(raw_emails, list) else [raw_emails]
    for cand in candidates:
        clean = InputSanitizer.sanitize_email(str(cand or ""))
        if clean and clean not in emails:
            emails.append(clean)

    phones: List[str] = []
    raw_phones = contact.get("phone")
    p_candidates = raw_phones if isinstance(raw_phones, list) else [raw_phones]
    for cand in p_candidates:
        norm = normalize_phone(str(cand or ""))
        if norm and norm not in phones:
            phones.append(norm)

    return {
        "name_normalized": name_normalized,
        "emails": emails,
        "phones": phones,
        "resume_hash": resume_content_hash(raw_text, structured) if (raw_text or structured) else "",
        "fingerprint": sanitize_fingerprint(fingerprint_hash),
    }


# ---------------------------------------------------------------------------
# Cluster helpers
# ---------------------------------------------------------------------------

def canonical_of(session: Dict[str, Any]) -> Dict[str, Any]:
    """Follow the merged_into chain to the canonical session (bounded)."""
    current = session
    for _ in range(_MAX_CLUSTER_HOPS):
        target = current.get("merged_into")
        if not target:
            return current
        nxt = _sessions().find_one({"guest_id": str(target)})
        if not nxt or nxt.get("guest_id") == current.get("guest_id"):
            return current
        current = nxt
    return current


def cluster_ids(guest_id: str) -> List[str]:
    """All guest_ids in the identity cluster containing guest_id (incl. itself)."""
    doc = _sessions().find_one({"guest_id": str(guest_id)})
    if not doc:
        return [str(guest_id)]
    canonical = canonical_of(doc)
    canonical_id = canonical.get("guest_id")
    ids = {str(canonical_id)}
    for member in _sessions().find({"merged_into": str(canonical_id)}, {"guest_id": 1}):
        ids.add(str(member.get("guest_id")))
    ids.add(str(guest_id))
    return sorted(ids)


def cluster_tailors_used(guest_id: str) -> int:
    """Effective tailors used = SUM across the identity cluster (spec: SUM,
    prevents quota splitting across merged sessions)."""
    ids = cluster_ids(guest_id)
    total = 0
    for doc in _sessions().find({"guest_id": {"$in": ids}}, {"tailors_used": 1}):
        try:
            total += max(0, int(doc.get("tailors_used") or 0))
        except (TypeError, ValueError):
            continue
    return total


# ---------------------------------------------------------------------------
# Resolution (2-of-N distinct signal types)
# ---------------------------------------------------------------------------

def _match_counts(signals: Dict[str, Any], exclude_guest_id: str = "") -> Dict[str, set]:
    """For each candidate session _id-by-guest_id: the SET of signal TYPES that
    matched. Each lookup is an exact-match on a hardcoded field with sanitized
    values only."""
    coll = _sessions()
    matched: Dict[str, set] = {}

    def _record(cursor, signal_type: str):
        for doc in cursor:
            gid = str(doc.get("guest_id") or "")
            if not gid or gid == exclude_guest_id:
                continue
            matched.setdefault(gid, set()).add(signal_type)

    name = signals.get("name_normalized") or ""
    if name:
        _record(coll.find({"identity.name_normalized": name}, {"guest_id": 1}), "name")

    emails = [e for e in (signals.get("emails") or []) if e]
    if emails:
        _record(coll.find({"identity.emails": {"$in": emails}}, {"guest_id": 1}), "email")

    phones = [p for p in (signals.get("phones") or []) if p]
    if phones:
        _record(coll.find({"identity.phones": {"$in": phones}}, {"guest_id": 1}), "phone")

    rh = signals.get("resume_hash") or ""
    if rh:
        _record(coll.find({"identity.resume_hashes": rh}, {"guest_id": 1}), "resume_hash")

    fp = signals.get("fingerprint") or ""
    if fp:
        _record(coll.find({"fingerprint_hash": fp}, {"guest_id": 1}), "fingerprint")

    return matched


def resolve(signals: Dict[str, Any], exclude_guest_id: str = "") -> Optional[Dict[str, Any]]:
    """Find the canonical (oldest) existing session matching >=2 DISTINCT
    signal types. Returns the canonical session doc, or None (0-1 matches →
    standalone; a single signal NEVER links identities)."""
    matched = _match_counts(signals, exclude_guest_id=exclude_guest_id)
    candidate_ids = [gid for gid, types in matched.items() if len(types) >= 2]
    if not candidate_ids:
        return None
    docs = list(_sessions().find({"guest_id": {"$in": candidate_ids}}))
    if not docs:
        return None
    # Resolve each candidate to its canonical, pick the oldest cluster root.
    canonicals: Dict[str, Dict[str, Any]] = {}
    for doc in docs:
        root = canonical_of(doc)
        canonicals[str(root.get("guest_id"))] = root
    oldest = min(
        canonicals.values(),
        key=lambda d: d.get("created_at") or datetime.now(timezone.utc),
    )
    return oldest


def merge_sessions(canonical: Dict[str, Any], newer_guest_id: str) -> None:
    """SOFT merge: point the newer session at the canonical and union the
    newer session's identity arrays onto the canonical. tailors_used stays on
    each member doc — effective count is the cluster SUM (cluster_tailors_used)."""
    canonical_id = str(canonical.get("guest_id"))
    newer_guest_id = str(newer_guest_id)
    if not canonical_id or canonical_id == newer_guest_id:
        return
    coll = _sessions()
    newer = coll.find_one({"guest_id": newer_guest_id})
    if not newer:
        return
    coll.update_one(
        {"guest_id": newer_guest_id},
        {"$set": {"merged_into": canonical_id}},
    )
    identity = newer.get("identity") or {}
    add_to_set: Dict[str, Any] = {}
    for field in ("emails", "phones", "resume_hashes"):
        values = [v for v in (identity.get(field) or []) if v]
        if values:
            add_to_set[f"identity.{field}"] = {"$each": values}
    update: Dict[str, Any] = {"$set": {"last_seen_at": datetime.now(timezone.utc)}}
    if add_to_set:
        update["$addToSet"] = add_to_set
    coll.update_one({"guest_id": canonical_id}, update)
    logger.info("guest identity: merged %s into cluster %s", newer_guest_id, canonical_id)


def resolve_and_merge(guest_id: str, signals: Dict[str, Any]) -> Dict[str, Any]:
    """Run resolution for a session's freshly-extracted signals; on a 2+-signal
    match, soft-merge this session into the canonical cluster.

    Returns {recognized: bool, canonical_guest_id, recognized_name} — quota
    carry (cluster SUM) is read separately via cluster_tailors_used. Never
    raises: recognition is best-effort and must not break onboarding.
    """
    guest_id = str(guest_id)
    try:
        canonical = resolve(signals, exclude_guest_id=guest_id)
        if canonical is None:
            return {"recognized": False, "canonical_guest_id": guest_id, "recognized_name": ""}
        # Don't merge a session into itself / its own cluster root twice.
        own = _sessions().find_one({"guest_id": guest_id})
        if own is not None and str(canonical_of(own).get("guest_id")) == str(canonical.get("guest_id")):
            pass  # already in this cluster
        else:
            merge_sessions(canonical, guest_id)
        identity = canonical.get("identity") or {}
        return {
            "recognized": True,
            "canonical_guest_id": str(canonical.get("guest_id")),
            "recognized_name": identity.get("name_normalized") or "",
        }
    except Exception as e:  # pragma: no cover - defensive (soft resolution)
        logger.warning("guest identity resolution failed (soft, continuing): %s", e)
        return {"recognized": False, "canonical_guest_id": guest_id, "recognized_name": ""}
