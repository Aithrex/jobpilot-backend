"""Exact-prompt cache for deterministic, user-independent LLM calls.

JD analysis ran on every tailor, cover letter, ATS score and JD import, so one
user tailoring and then writing a cover letter for the same posting paid for
the same analysis twice, and every user tailoring to the same board job paid
again (one PRO call, 4-21 s, measured 2026-11-10).

The key is a SHA-256 of everything that decides the answer: provider, model,
thinking level, system text, prompt, schema, max_tokens and temperature. A
prompt edit, a model switch or a new schema is therefore a miss by
construction; nothing has to remember to bump a version. Only the model's raw
JSON is stored, so the deterministic post-processing (keyword normalisation,
sanitising, `jd_profile`) still runs fresh on every hit.

Use it ONLY where the prompt holds no user data: a cached answer is served to
anyone who sends the same prompt. JD text qualifies (it is the posting, PII
redacted before it reaches the prompt).

Never raises; a database error is a miss. Off under pytest unless
`LLM_RESPONSE_CACHE=on`; `LLM_RESPONSE_CACHE=off` disables it anywhere.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

COLLECTION = "llm_response_cache"
TTL_DAYS = 7
_indexed = False


def enabled() -> bool:
    flag = os.environ.get("LLM_RESPONSE_CACHE", "").strip().lower()
    if flag in {"off", "false", "0"}:
        return False
    if flag in {"on", "true", "1"}:
        return True
    from utils.db_connect import _under_test_runner
    return not _under_test_runner()


def cache_key(name: str, **parts: Any) -> str:
    material = {
        "name": name,
        "provider": os.environ.get("LLM_PROVIDER", ""),
        "thinking": os.environ.get("GEMINI_THINKING_LEVEL", ""),
        **parts,
    }
    blob = json.dumps(material, sort_keys=True, default=repr, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _db():
    from utils.db_connect import DBConnect
    return DBConnect().get_db()


def _coll(db):
    global _indexed
    coll = db[COLLECTION]
    if not _indexed:
        try:
            coll.create_index("expire_at", expireAfterSeconds=0, name="ttl_expire_at")
        except Exception as exc:  # an index is an optimisation
            logger.debug("llm_response_cache: index ensure failed: %r", exc)
        _indexed = True
    return coll


def get(key: str, db=None) -> Optional[Dict[str, Any]]:
    try:
        doc = _coll(db if db is not None else _db()).find_one({"_id": key})
        if not doc:
            return None
        expire_at = doc.get("expire_at")
        if isinstance(expire_at, datetime):
            if expire_at.tzinfo is None:
                expire_at = expire_at.replace(tzinfo=timezone.utc)
            if expire_at <= datetime.now(timezone.utc):
                return None  # the TTL monitor runs once a minute
        value = json.loads(doc.get("value") or "null")
        return value if isinstance(value, dict) else None
    except Exception as exc:
        logger.debug("llm_response_cache: get failed: %r", exc)
        return None


def put(key: str, name: str, model: str, value: Dict[str, Any], db=None) -> None:
    try:
        if not isinstance(value, dict) or not value:
            return
        now = datetime.now(timezone.utc)
        _coll(db if db is not None else _db()).replace_one(
            {"_id": key},
            {
                "_id": key, "name": name, "model": model,
                # A string, so model output can never become a Mongo key.
                "value": json.dumps(value, ensure_ascii=False),
                "created_at": now, "expire_at": now + timedelta(days=TTL_DAYS),
            },
            upsert=True,
        )
    except Exception as exc:
        logger.debug("llm_response_cache: put failed: %r", exc)


def cached_json(name: str, model: str, key_parts: Dict[str, Any],
                call: Callable[[], Dict[str, Any]]) -> Dict[str, Any]:
    """Return the cached answer for this exact request, else `call()` and store it."""
    if not enabled():
        return call()
    key = cache_key(name, model=model, **key_parts)
    hit = get(key)
    if hit is not None:
        from services import llm_usage
        llm_usage.record(model=model, cache_hit=True)
        logger.info("llm_response_cache: hit name=%s", name)
        return hit
    value = call()
    put(key, name, model, value)
    return value
