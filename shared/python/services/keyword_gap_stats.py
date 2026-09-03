"""Cross-record missing-keyword counters.

A term that stays missing on more than ~1% of observations is a matching
bug until proven otherwise. KeywordGapEngine records every analyze() here
so operators can read the list without a warehouse job.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Iterable, List, Sequence

logger = logging.getLogger(__name__)

COLLECTION = "keyword_gap_stats"
# Floor so a single miss cannot scream. Tests monkeypatch these.
MIN_SEEN = 50
PERPETUAL_RATE = 0.01


def _col():
    from utils.db_connect import DBConnect
    return DBConnect().get_db()[COLLECTION]


def record_observation(
    catalog: Sequence[str],
    missing: Sequence[str],
) -> List[str]:
    """Increment seen/missing per term. Returns terms now over the 1% bar."""
    catalog_keys = [str(t).strip().lower() for t in catalog if str(t).strip()]
    missing_set = {str(t).strip().lower() for t in missing if str(t).strip()}
    if not catalog_keys:
        return []
    try:
        col = _col()
        now = datetime.now(timezone.utc)
        for key in dict.fromkeys(catalog_keys):
            inc = {"seen": 1}
            if key in missing_set:
                inc["missing"] = 1
            col.update_one(
                {"term": key},
                {"$inc": inc, "$set": {"updated_at": now}},
                upsert=True,
            )
        return flag_perpetual(missing_set)
    except Exception:
        logger.debug("keyword_gap_stats: persist skipped", exc_info=True)
        return []


def flag_perpetual(restrict: Iterable[str] | None = None) -> List[str]:
    """Terms whose miss rate is ≥ PERPETUAL_RATE with enough samples."""
    try:
        col = _col()
        restrict_set = {r for r in (restrict or []) if r}
        query = {"seen": {"$gte": MIN_SEEN}}
        if restrict_set:
            query["term"] = {"$in": list(restrict_set)}
        flagged: List[str] = []
        for doc in col.find(query, {"term": 1, "seen": 1, "missing": 1}):
            seen = int(doc.get("seen") or 0)
            missed = int(doc.get("missing") or 0)
            if seen < MIN_SEEN:
                continue
            rate = missed / seen
            if rate >= PERPETUAL_RATE:
                term = str(doc.get("term") or "")
                flagged.append(term)
                logger.warning(
                    "keyword_gap.perpetual term=%s rate=%.3f missing=%d seen=%d",
                    term, rate, missed, seen,
                )
        return flagged
    except Exception:
        logger.debug("keyword_gap_stats: flag skipped", exc_info=True)
        return []
