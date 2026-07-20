"""Guest composite-identity resolution — v2 (keyed HMAC signals, soft, never-blocking).

Guest Onboarding spec v2 (docs/specs/guest-onboarding.md §Identity):
identity signals are stored ONLY as versioned keyed HMACs
(``"v1:" + hex(hmac_sha256(GUEST_SIGNAL_HMAC_KEY, value))``) — no plaintext
email/phone/resume-signature lookup fields. With the key unset we FAIL CLOSED:
signals are neither stored nor matched, quota subjects stay standalone, one
no-PII warning is logged, and the feature keeps working.

Resolution rules (quota linkage ONLY — never artifact access):
  * merge requires >=2 DISTINCT signal types with AT LEAST ONE strong
    (email / phone / resume-signature HMAC);
  * device_token + fingerprint count as ONE combined "device" type;
  * name alone, device alone, IP alone, or name+device NEVER merge —
    ambiguous identities stay separate (a false-positive merge is worse
    than a free retry).

Cluster model: ``merged_into`` on the NEWER session points at the canonical
(oldest) session; every session in a cluster shares one ``quota_subject_id``
(the server-only quota/abuse identity — see guest_service).

Security: every value used in a Mongo filter is sanitized/derived server-side
and appears only in VALUE position of exact-match / $in filters with hardcoded
field names (hard invariant #4). No resume PII (names/emails/phones) is ever
logged — only guest_ids and counts.
"""
from __future__ import annotations

import hashlib
import hmac as _hmac
import logging
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from utils.config import _get_config_value
from utils.db_connect import DBConnect
from utils.security import InputSanitizer

logger = logging.getLogger(__name__)

# v2 signal types. "device" = device_token OR fingerprint (one combined type).
STRONG_TYPES = frozenset({"email", "phone", "resume_sig"})
SIGNAL_VERSION = 1

_FINGERPRINT_SAFE = re.compile(r"[^A-Za-z0-9._:-]")
_MAX_CLUSTER_HOPS = 10  # merged_into chain bound (defensive vs. cycles)

_hmac_key_warned = False


def _sessions():
    return DBConnect().get_db().guest_sessions


# ---------------------------------------------------------------------------
# Keyed HMAC (fail closed when the key is unset)
# ---------------------------------------------------------------------------

def signal_key() -> Optional[bytes]:
    """The dedicated identity-signal HMAC key (``GUEST_SIGNAL_HMAC_KEY``,
    SSM ``SSM_GUEST_SIGNAL_HMAC_KEY``). Unset → None (fail closed): signals
    are not stored or matched. Warned once per process, no PII."""
    global _hmac_key_warned
    value = (_get_config_value("GUEST_SIGNAL_HMAC_KEY", "") or "").strip()
    if not value:
        if not _hmac_key_warned:
            logger.warning(
                "GUEST_SIGNAL_HMAC_KEY not configured — guest identity signals "
                "disabled (subjects stay standalone; set the secret to enable "
                "cross-session quota linkage)"
            )
            _hmac_key_warned = True
        return None
    return value.encode("utf-8")


def signal_hmac(value: str) -> Optional[str]:
    """``"v1:" + hex(hmac_sha256(key, value))`` — or None when the key is
    unset (fail closed; NO plaintext fallback) or the value is empty."""
    if not value:
        return None
    key = signal_key()
    if key is None:
        return None
    digest = _hmac.new(key, str(value).encode("utf-8"), hashlib.sha256).hexdigest()
    return f"v{SIGNAL_VERSION}:{digest}"


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


def resume_canonical_sha(raw_text: str, structured: Optional[Dict[str, Any]] = None) -> str:
    """sha256 of the canonicalized resume text (lowercased, whitespace-collapsed).

    This intermediate digest is NEVER stored — the stored v2 signal is
    ``signal_hmac(resume_canonical_sha(...))`` (keyed), which also lets the
    backfill re-key v1's stored sha256 hashes without the original text.
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
    """Extract v2 identity signals from a parsed resume + device.

    Returns ``{name_normalized, email_hmacs, phone_hmacs, resume_sig_hmac,
    fingerprint}``. HMAC fields are empty when GUEST_SIGNAL_HMAC_KEY is unset
    (fail closed). Every element is safe as a Mongo VALUE.
    """
    structured = parsed_resume or {}
    contact = structured.get("contact") or {}

    name_normalized = normalize_name(contact.get("name") or "")

    email_hmacs: List[str] = []
    raw_emails = contact.get("email")
    candidates = raw_emails if isinstance(raw_emails, list) else [raw_emails]
    for cand in candidates:
        clean = InputSanitizer.sanitize_email(str(cand or ""))
        mac = signal_hmac(clean) if clean else None
        if mac and mac not in email_hmacs:
            email_hmacs.append(mac)

    phone_hmacs: List[str] = []
    raw_phones = contact.get("phone")
    p_candidates = raw_phones if isinstance(raw_phones, list) else [raw_phones]
    for cand in p_candidates:
        norm = normalize_phone(str(cand or ""))
        mac = signal_hmac(norm) if norm else None
        if mac and mac not in phone_hmacs:
            phone_hmacs.append(mac)

    sig_sha = resume_canonical_sha(raw_text, structured) if (raw_text or structured) else ""
    resume_sig_hmac = signal_hmac(sig_sha) if sig_sha else None

    return {
        "name_normalized": name_normalized,
        "email_hmacs": email_hmacs,
        "phone_hmacs": phone_hmacs,
        "resume_sig_hmac": resume_sig_hmac or "",
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


# ---------------------------------------------------------------------------
# Resolution (v2: >=2 distinct types with >=1 strong; device is ONE type)
# ---------------------------------------------------------------------------

def _match_counts(
    signals: Dict[str, Any],
    exclude_guest_id: str = "",
    device_token_hash: Optional[str] = None,
) -> Dict[str, set]:
    """For each candidate guest_id: the SET of signal TYPES that matched.
    Each lookup is an exact-match on a hardcoded field with server-derived
    values only (HMACs / restricted charsets)."""
    coll = _sessions()
    matched: Dict[str, set] = {}

    def _record(cursor, signal_type: str):
        for doc in cursor:
            gid = str(doc.get("guest_id") or "")
            if not gid or gid == exclude_guest_id:
                continue
            matched.setdefault(gid, set()).add(signal_type)

    email_hmacs = [e for e in (signals.get("email_hmacs") or []) if e]
    if email_hmacs:
        _record(coll.find({"identity.email_hmacs": {"$in": email_hmacs}}, {"guest_id": 1}), "email")

    phone_hmacs = [p for p in (signals.get("phone_hmacs") or []) if p]
    if phone_hmacs:
        _record(coll.find({"identity.phone_hmacs": {"$in": phone_hmacs}}, {"guest_id": 1}), "phone")

    sig = signals.get("resume_sig_hmac") or ""
    if sig:
        _record(coll.find({"identity.resume_sig_hmacs": sig}, {"guest_id": 1}), "resume_sig")

    # device_token + fingerprint are ONE combined "device" evidence type —
    # shared-device users must stay separate subjects.
    fp = signals.get("fingerprint") or ""
    if fp:
        _record(coll.find({"fingerprint_hash": fp}, {"guest_id": 1}), "device")
    if device_token_hash:
        _record(coll.find({"device_token_hash": str(device_token_hash)}, {"guest_id": 1}), "device")

    # Name is checked ONLY against candidates already found via other signals
    # (name-only lookups are gone — no name index, no name oracle).
    name = signals.get("name_normalized") or ""
    if name and matched:
        ids = sorted(matched.keys())
        for doc in coll.find(
            {"guest_id": {"$in": ids}, "identity.name_normalized": name},
            {"guest_id": 1},
        ):
            gid = str(doc.get("guest_id") or "")
            if gid:
                matched.setdefault(gid, set()).add("name")

    return matched


def resolve(
    signals: Dict[str, Any],
    exclude_guest_id: str = "",
    device_token_hash: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Find the canonical (oldest) existing session matching the v2 rule:
    >=2 distinct signal types with at least one STRONG type. Returns the
    canonical session doc, or None (ambiguous identities stay standalone).

    With GUEST_SIGNAL_HMAC_KEY unset, no strong signals exist → always None.
    """
    matched = _match_counts(
        signals, exclude_guest_id=exclude_guest_id, device_token_hash=device_token_hash
    )
    candidate_ids = [
        gid for gid, types in matched.items()
        if len(types) >= 2 and (types & STRONG_TYPES)
    ]
    if not candidate_ids:
        return None
    docs = list(_sessions().find({"guest_id": {"$in": candidate_ids}}))
    if not docs:
        return None
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
    """SOFT merge: point the newer session at the canonical, union the newer
    session's HMAC signal arrays onto the canonical, and unify the two
    quota subjects (counters aggregate onto the canonical subject — see
    guest_service.unify_subjects; counters never go negative)."""
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
    for field in ("email_hmacs", "phone_hmacs", "resume_sig_hmacs"):
        values = [v for v in (identity.get(field) or []) if v]
        if values:
            add_to_set[f"identity.{field}"] = {"$each": values}
    update: Dict[str, Any] = {"$set": {"last_seen_at": datetime.now(timezone.utc)}}
    if add_to_set:
        update["$addToSet"] = add_to_set
    coll.update_one({"guest_id": canonical_id}, update)

    # Unify quota subjects (lazy import — guest_service imports this module).
    try:
        from services import guest_service
        guest_service.unify_subjects(canonical_id, newer_guest_id)
    except Exception as e:  # pragma: no cover - defensive (soft merge)
        logger.warning("guest identity: subject unification failed: %s", e)

    logger.info("guest identity: merged %s into cluster %s", newer_guest_id, canonical_id)


def resolve_and_merge(
    guest_id: str,
    signals: Dict[str, Any],
    device_token_hash: Optional[str] = None,
) -> Dict[str, Any]:
    """Run v2 resolution for freshly-extracted signals; on a qualifying match,
    soft-merge this session into the canonical cluster.

    Returns ``{recognized: bool, canonical_guest_id}``. NEVER returns another
    session's name (v1's cross-session name disclosure is removed). Never
    raises: recognition is best-effort and must not break onboarding.
    """
    guest_id = str(guest_id)
    try:
        canonical = resolve(
            signals, exclude_guest_id=guest_id, device_token_hash=device_token_hash
        )
        if canonical is None:
            return {"recognized": False, "canonical_guest_id": guest_id}
        own = _sessions().find_one({"guest_id": guest_id})
        if own is not None and str(canonical_of(own).get("guest_id")) == str(canonical.get("guest_id")):
            pass  # already in this cluster
        else:
            merge_sessions(canonical, guest_id)
        return {
            "recognized": True,
            "canonical_guest_id": str(canonical.get("guest_id")),
        }
    except Exception as e:  # pragma: no cover - defensive (soft resolution)
        logger.warning("guest identity resolution failed (soft, continuing): %s", e)
        return {"recognized": False, "canonical_guest_id": guest_id}
