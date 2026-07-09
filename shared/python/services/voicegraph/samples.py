"""Writing-sample capture (§6.12.2).

A writing sample is the most personal artifact in the product. It is stored
Fernet-encrypted at rest, scoped by `user_email`, never logged. Samples have a
lightweight lifecycle:

  * draft  — being written; autosaved every few seconds; free, does not count
             against the plan cap.
  * ready  — finalized by the user; counts against the plan cap; in VG Step 2 a
             `ready` transition enqueues graph extraction.

Signal thresholds (§6.12.2): 150 words is the minimum viable signal for a voice
profile, ~300 is the sweet spot, past ~600 there are diminishing returns. Below
150 we refuse to *build a profile* (enforced in VG Step 4) but still let the user
save what they have.
"""
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4

from utils.db_connect import DBConnect

from services.voicegraph.crypto import decrypt, encrypt

logger = logging.getLogger(__name__)

MIN_SIGNAL_WORDS = 150
SWEET_SPOT_WORDS = 300
DIMINISHING_WORDS = 600
MAX_SAMPLE_CHARS = 20000  # hard cap on a single stored sample

_STATUS_DRAFT = "draft"
_STATUS_READY = "ready"
_WORD_RE = re.compile(r"\b[\w'-]+\b", re.UNICODE)


def _col():
    return DBConnect().get_db().writing_samples


def _now() -> datetime:
    return datetime.now(timezone.utc)


def word_count(text: str) -> int:
    if not text:
        return 0
    return len(_WORD_RE.findall(text))


def signal(words: int) -> Dict[str, Any]:
    """Per-sample encouragement for the writing counter — never blocks."""
    if words < MIN_SIGNAL_WORDS:
        return {
            "level": "insufficient",
            "ready_for_profile": False,
            "message": f"Keep going — around {MIN_SIGNAL_WORDS} words lets us learn your voice. You're at {words}.",
        }
    if words <= DIMINISHING_WORDS:
        return {
            "level": "good",
            "ready_for_profile": True,
            "message": f"{words} words — plenty. {SWEET_SPOT_WORDS} is the sweet spot.",
        }
    return {
        "level": "diminishing",
        "ready_for_profile": True,
        "message": f"{words} words. Past here it's diminishing returns — this is more than enough.",
    }


def _public(doc: Dict[str, Any], include_text: bool = True) -> Dict[str, Any]:
    words = int(doc.get("word_count") or 0)
    out = {
        "sample_id": doc.get("sample_id"),
        "status": doc.get("status", _STATUS_DRAFT),
        "source": doc.get("source", "typed"),
        "prompt_id": doc.get("prompt_id"),
        "word_count": words,
        "signal": signal(words),
        "created_at": _iso(doc.get("created_at")),
        "updated_at": _iso(doc.get("updated_at")),
    }
    if include_text:
        try:
            out["text"] = decrypt(doc.get("text_enc", ""))
        except Exception:
            logger.warning("voicegraph: failed to decrypt sample %s", doc.get("sample_id"))
            out["text"] = ""
    return out


def _iso(dt) -> Optional[str]:
    return dt.isoformat() if hasattr(dt, "isoformat") else None


def save_sample(
    user_email: str,
    text: str,
    sample_id: Optional[str] = None,
    source: str = "typed",
    prompt_id: Optional[str] = None,
    finalize: bool = False,
    consent_version: Optional[str] = None,
) -> Dict[str, Any]:
    """Create or autosave a sample. Raises KeyError('sample_not_found') if an
    unknown sample_id is supplied for the user."""
    text = (text or "")[:MAX_SAMPLE_CHARS]
    words = word_count(text)
    now = _now()
    col = _col()

    if sample_id:
        existing = col.find_one({"sample_id": sample_id, "user_email": user_email})
        if not existing:
            raise KeyError("sample_not_found")
        update: Dict[str, Any] = {
            "text_enc": encrypt(text),
            "word_count": words,
            "source": source,
            "updated_at": now,
        }
        if prompt_id is not None:
            update["prompt_id"] = prompt_id
        if finalize:
            update["status"] = _STATUS_READY
            update["finalized_at"] = now
        col.update_one({"sample_id": sample_id, "user_email": user_email}, {"$set": update})
    else:
        sample_id = str(uuid4())
        col.insert_one(
            {
                "sample_id": sample_id,
                "user_email": user_email,
                "text_enc": encrypt(text),
                "word_count": words,
                "source": source,
                "prompt_id": prompt_id,
                "status": _STATUS_READY if finalize else _STATUS_DRAFT,
                "consent_version": consent_version,
                "created_at": now,
                "updated_at": now,
                "finalized_at": now if finalize else None,
            }
        )

    doc = col.find_one({"sample_id": sample_id, "user_email": user_email})
    return _public(doc)


def get_sample(user_email: str, sample_id: str) -> Optional[Dict[str, Any]]:
    doc = _col().find_one({"sample_id": sample_id, "user_email": user_email})
    return _public(doc) if doc else None


def list_samples(user_email: str, include_text: bool = True) -> List[Dict[str, Any]]:
    cur = _col().find({"user_email": user_email}).sort("updated_at", -1)
    return [_public(d, include_text=include_text) for d in cur]


def delete_sample(user_email: str, sample_id: str) -> bool:
    """Delete a single sample. NOTE: the full revocation cascade over graph nodes
    whose only evidence is this sample lands in VG Step 8 (store.revoke_cascade)."""
    res = _col().find_one_and_delete({"sample_id": sample_id, "user_email": user_email})
    return res is not None


def delete_all_samples(user_email: str) -> int:
    """Delete every writing sample for a user (revocation cascade, VG Step 8)."""
    res = _col().delete_many({"user_email": user_email})
    return res.deleted_count


def sample_span_text(user_email: str, sample_id: str, char_start: int, char_end: int) -> str:
    """Return the exact source substring for a provenance report. Owner-scoped."""
    doc = _col().find_one({"sample_id": sample_id, "user_email": user_email})
    if not doc:
        return ""
    try:
        text = decrypt(doc.get("text_enc", ""))
    except Exception:
        return ""
    if 0 <= char_start < char_end <= len(text):
        return text[char_start:char_end]
    return ""


def count_ready(user_email: str, exclude_sample_id: Optional[str] = None) -> int:
    q: Dict[str, Any] = {"user_email": user_email, "status": _STATUS_READY}
    if exclude_sample_id:
        q["sample_id"] = {"$ne": exclude_sample_id}
    return _col().count_documents(q)


def ready_word_total(user_email: str) -> int:
    total = 0
    for d in _col().find({"user_email": user_email, "status": _STATUS_READY}, {"word_count": 1}):
        total += int(d.get("word_count") or 0)
    return total
