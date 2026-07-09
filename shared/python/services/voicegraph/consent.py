"""Per-scope consent ledger (§9.2 / §6.12.8).

There is no generic consent ledger in this backend yet; this introduces a small,
reusable `consents` collection keyed by (user_email, scope). The Voice Graph uses
scope `voice_graph`. Every grant records the exact consent-text version, timestamp,
and client IP; revocation stamps `revoked_at` and flips `granted` to False (the
row is kept as an audit trail, not deleted).

Off by default: absence of a granted row means no consent.
"""
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from utils.db_connect import DBConnect

from services.voicegraph import CONSENT_SCOPE

logger = logging.getLogger(__name__)

# Bump when the consent copy materially changes; stored per grant for auditability.
CONSENT_TEXT_VERSION = "voice_graph_v1"


def _col():
    return DBConnect().get_db().consents


def _now() -> datetime:
    return datetime.now(timezone.utc)


def grant(user_email: str, ip: str = "", version: str = CONSENT_TEXT_VERSION) -> Dict[str, Any]:
    """Record (or refresh) consent for the Voice Graph scope."""
    now = _now()
    _col().update_one(
        {"user_email": user_email, "scope": CONSENT_SCOPE},
        {
            "$set": {
                "user_email": user_email,
                "scope": CONSENT_SCOPE,
                "granted": True,
                "consent_text_version": version,
                "granted_at": now,
                "ip": ip or "",
                "revoked_at": None,
            }
        },
        upsert=True,
    )
    return status(user_email)


def revoke(user_email: str) -> Dict[str, Any]:
    """Withdraw consent. The data-deletion cascade lives in store.revoke_cascade
    (VG Step 8); this only flips the ledger row."""
    _col().update_one(
        {"user_email": user_email, "scope": CONSENT_SCOPE},
        {"$set": {"granted": False, "revoked_at": _now()}},
    )
    return status(user_email)


def status(user_email: str) -> Dict[str, Any]:
    doc = _col().find_one({"user_email": user_email, "scope": CONSENT_SCOPE})
    if not doc or not doc.get("granted"):
        return {"granted": False, "version": None, "granted_at": None}
    granted_at = doc.get("granted_at")
    return {
        "granted": True,
        "version": doc.get("consent_text_version"),
        "granted_at": granted_at.isoformat() if hasattr(granted_at, "isoformat") else None,
    }


def is_granted(user_email: str) -> bool:
    doc = _col().find_one({"user_email": user_email, "scope": CONSENT_SCOPE})
    return bool(doc and doc.get("granted"))
