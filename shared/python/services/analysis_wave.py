"""Wave A — evidence store + JD analysis, parallel and cached by hash."""
from __future__ import annotations

import copy
import hashlib
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

_JD_CACHE: Dict[str, Dict[str, Any]] = {}
_JD_CACHE_MAX = 64


def jd_text_hash(jd_text: str) -> str:
    return hashlib.sha256((jd_text or "").encode("utf-8", "replace")).hexdigest()


def cache_jd_analysis(jd_text: str, analysis: Dict[str, Any]) -> None:
    key = jd_text_hash(jd_text)
    if len(_JD_CACHE) >= _JD_CACHE_MAX:
        _JD_CACHE.pop(next(iter(_JD_CACHE)), None)
    _JD_CACHE[key] = copy.deepcopy(analysis)


def cached_jd_analysis(jd_text: str) -> Optional[Dict[str, Any]]:
    hit = _JD_CACHE.get(jd_text_hash(jd_text))
    return copy.deepcopy(hit) if hit else None


def run_wave_a(
    user_email: str,
    structured: Dict[str, Any],
    jd_text: str,
    jd_analysis: Dict[str, Any],
    fact_store: Optional[Dict[str, Any]] = None,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Evidence + JD enrich in parallel. Returns (fact_store, jd_analysis)."""
    from services.jd_analyzer import enrich
    from services.profile_facts import build_fact_store

    def _facts():
        if fact_store is not None:
            return fact_store
        return build_fact_store(
            user_email=user_email, structured_resume=structured
        )

    def _jd():
        return enrich(jd_analysis or {}, jd_text or "", resume=structured)

    with ThreadPoolExecutor(max_workers=2) as pool:
        f_ev = pool.submit(_facts)
        f_jd = pool.submit(_jd)
        return f_ev.result(), f_jd.result()
