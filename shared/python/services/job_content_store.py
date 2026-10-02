"""S3-backed storage for job-posting BODIES and for archived postings.

Why this module exists
----------------------
`job_postings` (ADR-003) is the hot read path: one indexed query has to answer
"active + fresh + this role family" in milliseconds. Two things would wreck
that if they lived in Mongo:

1. **JD bodies.** A description is up to 40,000 characters. At a steady-state
   7-day inventory of tens of thousands of postings that is hundreds of MB on a
   shared-tier Atlas cluster, for text the matching path never reads (ADR-002
   scoring runs off the extracted `requirements` profile, not the raw body).
2. **Postings a user is attached to.** Retention deletes anything past the
   7-day window, but a posting somebody APPLIED to must survive forever. Keeping
   those in Mongo re-grows the collection without bound.

Both go to S3, which is the right storage class for cold, large, immutable blobs.

Key layout
----------
    job-jd/<hh>/<hh>/<content_hash>.txt      the JD body, content-addressed
    job-archive/<hh>/<job_id>.json           a full posting snapshot, forever

The JD key is the **content hash**, so the same requisition posted on two boards
(or re-observed unchanged) stores exactly one object. Content-addressed also
means the object is immutable: if the hash matches, the body matches, so a
re-write can be skipped entirely.

Failure posture
---------------
Nothing here raises into a crawl or a request. Every function fails soft and
returns ``None`` / ``False`` / ``0`` after logging, because the correct
behaviour when S3 hiccups is "this posting keeps its Mongo body" or "this
posting is not purged yet", never "the ingestion run dies".

The one hard rule, enforced by the caller in
`job_inventory_service.purge_expired`: a PINNED posting is never deleted from
Mongo unless `archive_posting` has confirmed the S3 object exists.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_HEX_RE = re.compile(r"^[0-9a-f]{8,64}$")

JD_PREFIX = "job-jd"
ARCHIVE_PREFIX = "job-archive"

# Bodies above this are truncated before storage. Generous: the cap exists to
# bound a pathological page, not to trim real postings.
MAX_BODY_CHARS = 200_000

_client = None
_client_lock = threading.Lock()

# Hashes this container has already confirmed present in S3. Purely an
# optimisation: a cold container just re-checks. Bounded so a long-lived
# container cannot grow it without limit.
_KNOWN_HASHES: set = set()
_KNOWN_MAX = 20_000

# Small read-through cache so scoring several postings that share one JD (the
# same req on two boards) does not pay repeated GETs inside one invocation.
_BODY_CACHE: Dict[str, str] = {}
_BODY_CACHE_MAX = 64

_missing_bucket_warned = False

# A DENIED write is billed exactly like a successful one, and a failed put_jd
# is never remembered, so a missing permission re-pays HEAD + PUT for every
# posting on every crawl tick. Measured 2026-09: the EC2 ingest role had no
# s3:PutObject on job-jd/*, which cost 1.99M PutObject requests (~90% of the
# S3 bill) and stored nothing. After a permission-shaped refusal, stop writing
# for a while; callers already treat "not stored" as "keep the body in Mongo".
_WRITE_PAUSE_SECONDS = 1800
_DENIED_CODES = frozenset({
    "AccessDenied", "AllAccessDisabled", "InvalidAccessKeyId",
    "SignatureDoesNotMatch", "NoSuchBucket",
})
_write_paused_until = 0.0


def bucket() -> str:
    return (os.getenv("RESUME_S3_BUCKET") or "").strip()


def available() -> bool:
    """True when object storage is usable. Callers fall back to keeping the
    body in Mongo when this is False, so local dev works with no S3 at all."""
    global _missing_bucket_warned
    if bucket():
        return True
    if not _missing_bucket_warned:
        logger.warning(
            "job_content_store: RESUME_S3_BUCKET unset - JD bodies will stay in "
            "Mongo and postings will not be archived"
        )
        _missing_bucket_warned = True
    return False


def _s3():
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                import boto3

                _client = boto3.client("s3")
    return _client


def _safe_hash(value: Any) -> str:
    h = str(value or "").strip().lower()
    return h if _HEX_RE.match(h) else ""


def _safe_id(value: Any) -> str:
    """job_id is a uuid4 hex we minted ourselves; still validated because it
    lands in an S3 key."""
    v = str(value or "").strip().lower()
    return v if re.match(r"^[0-9a-f]{8,64}$", v) else ""


def jd_key(content_hash: str) -> str:
    h = _safe_hash(content_hash)
    return f"{JD_PREFIX}/{h[:2]}/{h[2:4]}/{h}.txt" if h else ""


def archive_key(job_id: str) -> str:
    j = _safe_id(job_id)
    return f"{ARCHIVE_PREFIX}/{j[:2]}/{j}.json" if j else ""


def _writes_paused() -> bool:
    return time.monotonic() < _write_paused_until


def _note_write_failure(exc: Exception) -> None:
    global _write_paused_until
    code = (getattr(exc, "response", None) or {}).get("Error", {}).get("Code", "")
    if code not in _DENIED_CODES:
        return
    if not _writes_paused():
        logger.error(
            "job_content_store: S3 refused a write (%s) on bucket %s - pausing "
            "JD/archive writes for %ds; bodies stay in Mongo until then",
            code, bucket(), _WRITE_PAUSE_SECONDS)
    _write_paused_until = time.monotonic() + _WRITE_PAUSE_SECONDS


def _remember(content_hash: str) -> None:
    if len(_KNOWN_HASHES) >= _KNOWN_MAX:
        _KNOWN_HASHES.clear()
    _KNOWN_HASHES.add(content_hash)


# ---------------------------------------------------------------------------
# JD bodies - content addressed, immutable
# ---------------------------------------------------------------------------

def put_jd(content_hash: str, text: Any) -> Optional[str]:
    """Store a JD body under its content hash. Returns the hash on success.

    Idempotent by construction: the key IS the digest of the body, so writing
    the same body twice writes the same object. An existing object is left
    alone rather than re-uploaded.
    """
    h = _safe_hash(content_hash)
    body = str(text or "")
    if not h or not body.strip() or not available():
        return None
    if h in _KNOWN_HASHES:
        return h
    if _writes_paused():
        return None

    key = jd_key(h)
    try:
        client = _s3()
        try:
            client.head_object(Bucket=bucket(), Key=key)
            _remember(h)
            return h  # already stored; content-addressed so it cannot differ
        except Exception:
            pass  # not found (or head denied) - fall through to the write
        client.put_object(
            Bucket=bucket(),
            Key=key,
            Body=body[:MAX_BODY_CHARS].encode("utf-8"),
            ContentType="text/plain; charset=utf-8",
        )
        _remember(h)
        return h
    except Exception as exc:
        logger.warning("job_content_store: JD write failed for %s: %s",
                       h[:12], type(exc).__name__)
        _note_write_failure(exc)
        return None


def get_jd(content_hash: str) -> Optional[str]:
    """Read a JD body back. Returns None when absent or unreadable."""
    h = _safe_hash(content_hash)
    if not h or not available():
        return None
    cached = _BODY_CACHE.get(h)
    if cached is not None:
        return cached
    try:
        obj = _s3().get_object(Bucket=bucket(), Key=jd_key(h))
        text = obj["Body"].read().decode("utf-8", errors="replace")
    except Exception as exc:
        logger.info("job_content_store: JD read miss for %s: %s",
                    h[:12], type(exc).__name__)
        return None
    if len(_BODY_CACHE) >= _BODY_CACHE_MAX:
        _BODY_CACHE.clear()
    _BODY_CACHE[h] = text
    return text


def jd_exists(content_hash: str) -> bool:
    h = _safe_hash(content_hash)
    if not h or not available():
        return False
    if h in _KNOWN_HASHES:
        return True
    try:
        _s3().head_object(Bucket=bucket(), Key=jd_key(h))
        _remember(h)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Posting archives - the "a user applied to this" guarantee
# ---------------------------------------------------------------------------

def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        aware = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return aware.isoformat()
    return str(value)


def archive_posting(doc: Dict[str, Any], *, reason: str = "pinned") -> bool:
    """Write a durable snapshot of one posting, body included.

    This is what makes 7-day retention safe. Called when a posting is PINNED
    (a user applied / saved / tailored against it) and re-checked immediately
    before any purge that would delete a pinned row.

    Returns True only when the object is definitely in S3.
    """
    if not isinstance(doc, dict) or not available() or _writes_paused():
        return False
    job_id = _safe_id(doc.get("job_id"))
    key = archive_key(job_id)
    if not key:
        return False

    payload = {k: v for k, v in doc.items() if k != "_id"}
    # Inline the body so an archive is self-contained even if the JD blob is
    # ever lifecycle-expired.
    if not payload.get("description_text"):
        body = get_jd(doc.get("description_ref") or doc.get("content_hash"))
        if body:
            payload["description_text"] = body
    payload["archived_at"] = datetime.now(timezone.utc).isoformat()
    payload["archive_reason"] = str(reason or "pinned")[:60]

    try:
        _s3().put_object(
            Bucket=bucket(),
            Key=key,
            Body=json.dumps(payload, default=_json_default).encode("utf-8"),
            ContentType="application/json",
        )
        return True
    except Exception as exc:
        logger.error("job_content_store: archive FAILED for job %s: %s",
                     job_id[:12], type(exc).__name__)
        _note_write_failure(exc)
        return False


def archive_exists(job_id: str) -> bool:
    """Is this posting durably archived? The purge guard calls this before
    deleting a pinned row, so a False here must mean 'do not delete'."""
    key = archive_key(job_id)
    if not key or not available():
        return False
    try:
        _s3().head_object(Bucket=bucket(), Key=key)
        return True
    except Exception:
        return False


def get_archived(job_id: str) -> Optional[Dict[str, Any]]:
    """Read an archived posting back - the resolution path for a job that has
    left Mongo but that a user is still attached to."""
    key = archive_key(job_id)
    if not key or not available():
        return None
    try:
        obj = _s3().get_object(Bucket=bucket(), Key=key)
        data = json.loads(obj["Body"].read().decode("utf-8", errors="replace"))
        return data if isinstance(data, dict) else None
    except Exception as exc:
        logger.info("job_content_store: archive read miss for %s: %s",
                    str(job_id)[:12], type(exc).__name__)
        return None


def reset_caches() -> None:
    """Test seam."""
    global _write_paused_until
    _KNOWN_HASHES.clear()
    _BODY_CACHE.clear()
    _write_paused_until = 0.0
