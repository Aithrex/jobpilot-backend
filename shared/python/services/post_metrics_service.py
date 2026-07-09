"""Post-metrics ingest + reward computation for the signal loop (Compass §6.7 / §2.2).

Closes the loop opened by `post_bandit_service`: real engagement on a published
LinkedIn post flows back in here, gets normalized to a reward in [0,1], and (once
the post's metrics window has settled) folds into the bandit posterior exactly
once.

Two ingest sources:
  - user_reported     : the "how did your post do?" 72h card the user fills in.
  - extension_observed: numbers the browser extension scrapes off the live post.
extension_observed is treated as more trustworthy when both exist.

Reward normalization is deterministic (no LLM): a saturating function of
engagement so a viral post and a merely-good post don't swamp the posterior.
When impressions are known we use an engagement RATE; when they aren't (common
for user-reported cards) we fall back to a saturating function of absolute
weighted interactions. Comments and reposts count for more than reactions — they
are stronger intent signals.

Collection
----------
  post_metrics : {post_id, user_email, source, metrics{impressions,reactions,
                  comments,reposts}, spec, recorded_at}
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from utils.db_connect import DBConnect
from utils.security import InputSanitizer

logger = logging.getLogger(__name__)

VALID_SOURCES = ("user_reported", "extension_observed")
_METRIC_FIELDS = ("impressions", "reactions", "comments", "reposts")

# Reward-shaping constants.
#   _RATE_SCALE: engagement rate that maps to ~0.63 reward (LinkedIn "good" is a
#                few %). reward = 1 - exp(-rate / _RATE_SCALE).
#   _ABS_SCALE : weighted interactions that map to ~0.63 reward when impressions
#                are unknown. reward = 1 - exp(-weighted / _ABS_SCALE).
_RATE_SCALE = 0.03
_ABS_SCALE = 25.0

# Interaction weights — intent-ordered (a repost > a comment > a reaction).
_W_REACTIONS = 1.0
_W_COMMENTS = 2.0
_W_REPOSTS = 3.0

# Default settle window before a post's outcome is frozen into the bandit.
_SETTLE_HOURS = 72


def _db():
    return DBConnect().get_db()


def _now():
    return datetime.now(timezone.utc)


def _as_naive_utc(dt: Any) -> Optional[datetime]:
    """Normalize a datetime for comparison. PyMongo reads BSON datetimes back
    timezone-NAIVE (BSON stores no tzinfo), so a value round-tripped through
    Mongo is naive even though we wrote it aware. Coerce everything to naive-UTC
    so nightly comparisons never hit an aware-vs-naive TypeError."""
    if not isinstance(dt, datetime):
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _nonneg_int(v: Any) -> int:
    try:
        n = int(float(v))
    except (TypeError, ValueError):
        return 0
    return n if n > 0 else 0


def _clean_metrics(metrics: Any) -> Dict[str, int]:
    metrics = metrics if isinstance(metrics, dict) else {}
    return {f: _nonneg_int(metrics.get(f)) for f in _METRIC_FIELDS}


# --------------------------------------------------------------------------
# Ingest
# --------------------------------------------------------------------------
def record_metric(user_email: str, post_id: str, source: str,
                   metrics: Dict[str, Any], spec: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Store one engagement observation for a post.

    `spec` is the bandit variant spec that was used for this post (from the
    drafted/posted variant); persisting it here is what lets the nightly job
    attribute the reward back to the right arms. It is optional — without it the
    metric is still recorded (for the UI card) but cannot train the bandit.

    Does NOT touch the bandit itself (the posterior is updated once, at settle
    time, by `nightly_update`) — this keeps recording cheap and idempotency
    clean. Returns the stored snapshot plus the current computed reward for the
    UI card.
    """
    user_email = InputSanitizer.sanitize_string(str(user_email or ""), 320)
    post_id = InputSanitizer.sanitize_string(str(post_id or ""), 120)
    source = str(source or "").strip().lower()

    if not post_id:
        raise ValueError("post_id is required")
    if source not in VALID_SOURCES:
        raise ValueError(f"source must be one of {' | '.join(VALID_SOURCES)}")

    clean = _clean_metrics(metrics)
    doc = {
        "post_id": post_id,
        "user_email": user_email,
        "source": source,
        "metrics": clean,
        "spec": spec if isinstance(spec, dict) and spec else None,
        "recorded_at": _now(),
    }
    _db().post_metrics.insert_one(doc)
    return {
        "post_id": post_id,
        "source": source,
        "metrics": clean,
        "reward": reward_for(post_id),
    }


# --------------------------------------------------------------------------
# Reward
# --------------------------------------------------------------------------
def _best_metrics(post_id: str) -> Dict[str, int]:
    """Latest metrics for a post, preferring the more trustworthy source.

    Takes the most-recent doc per source (docs sorted newest-first), then prefers
    extension_observed over user_reported."""
    docs = list(_db().post_metrics.find({"post_id": str(post_id)}).sort("recorded_at", -1))
    latest_by_source: Dict[str, Dict[str, int]] = {}
    for d in docs:
        s = d.get("source")
        if s not in latest_by_source:  # first seen == newest for that source
            latest_by_source[s] = _clean_metrics(d.get("metrics"))
    return (latest_by_source.get("extension_observed")
            or latest_by_source.get("user_reported")
            or {f: 0 for f in _METRIC_FIELDS})


def _normalize(m: Dict[str, int]) -> float:
    """Saturating map from raw engagement → reward in [0,1]."""
    imp = _nonneg_int(m.get("impressions"))
    weighted = (_W_REACTIONS * _nonneg_int(m.get("reactions"))
                + _W_COMMENTS * _nonneg_int(m.get("comments"))
                + _W_REPOSTS * _nonneg_int(m.get("reposts")))
    if weighted <= 0:
        # A real, informative outcome: nobody engaged → reward 0 (arms get penalized).
        return 0.0
    if imp > 0:
        rate = weighted / imp
        reward = 1.0 - math.exp(-rate / _RATE_SCALE)
    else:
        reward = 1.0 - math.exp(-weighted / _ABS_SCALE)
    return max(0.0, min(1.0, reward))


def reward_for(post_id: str) -> float:
    """Normalized engagement reward in [0,1] for a post (0.0 if no metrics yet)."""
    post_id = str(post_id or "")
    if not post_id:
        return 0.0
    docs_exist = _db().post_metrics.find_one({"post_id": post_id}, {"_id": 1})
    if not docs_exist:
        return 0.0
    return _normalize(_best_metrics(post_id))


def _spec_for(post_id: str) -> Optional[Dict[str, str]]:
    """The bandit spec associated with a post (most recent non-null), or None."""
    doc = _db().post_metrics.find_one(
        {"post_id": str(post_id), "spec": {"$ne": None}},
        sort=[("recorded_at", -1)],
    )
    spec = (doc or {}).get("spec")
    return spec if isinstance(spec, dict) and spec else None


# --------------------------------------------------------------------------
# Feed the bandit
# --------------------------------------------------------------------------
def update_bandit_for(post_id: str) -> Dict[str, Any]:
    """Compute a post's reward and fold it into the bandit posterior ONCE.

    Idempotency is enforced inside `post_bandit_service.update` (guarded by
    post_id), so calling this repeatedly for the same post is safe — only the
    first application counts."""
    from services import post_bandit_service

    post_id = str(post_id or "")
    reward = reward_for(post_id)
    spec = _spec_for(post_id)
    if not spec:
        return {"post_id": post_id, "reward": reward, "applied": False, "reason": "no_spec"}
    res = post_bandit_service.update(spec, reward, post_id=post_id)
    return {
        "post_id": post_id,
        "reward": reward,
        "applied": bool(res.get("applied")),
        "reason": res.get("reason"),
        "arms": res.get("arms"),
    }


def nightly_update(settle_hours: int = _SETTLE_HOURS, limit: int = 1000) -> Dict[str, Any]:
    """Scheduled job: fold settled post outcomes into the bandit posterior.

    Intended to run once a day (see the wiring note — an EventBridge scheduled
    Lambda / cron self-invoke). For every post whose FIRST metric is older than
    `settle_hours` (its engagement window has effectively closed) and that has
    not already been applied, it recomputes the reward from the latest, most
    complete numbers and applies it exactly once. Posts still inside the window
    are skipped so we train on settled data, and the per-post idempotency guard
    means a post is never counted twice even if this job runs more than once.

    Returns a summary: {scanned, applied, skipped}.
    """
    cutoff = _as_naive_utc(_now() - timedelta(hours=int(settle_hours)))
    applied_ids = {d.get("_id") for d in _db().bandit_updates.find({}, {"_id": 1})}

    post_ids = _db().post_metrics.distinct("post_id")
    applied = skipped = 0
    for pid in post_ids[: int(limit)]:
        if pid in applied_ids:
            skipped += 1
            continue
        first = _db().post_metrics.find_one({"post_id": pid}, sort=[("recorded_at", 1)])
        first_at = _as_naive_utc((first or {}).get("recorded_at"))
        # Skip posts whose window hasn't settled yet (still collecting engagement).
        if first_at is not None and first_at > cutoff:
            skipped += 1
            continue
        res = update_bandit_for(pid)
        if res.get("applied"):
            applied += 1
        else:
            skipped += 1
    return {"scanned": len(post_ids), "applied": applied, "skipped": skipped}


def ensure_indexes() -> None:
    """Create the indexes this service relies on. Idempotent; call from a
    bootstrap/migration step, NOT at import time (keep cold starts lean).

    - post_metrics.post_id                      → the primary lookup key.
    - post_metrics.(post_id, recorded_at desc)  → backs _best_metrics/_spec_for's
                                                   'latest per post' sort.
    """
    _db().post_metrics.create_index("post_id", name="post_metrics_post_id")
    _db().post_metrics.create_index(
        [("post_id", 1), ("recorded_at", -1)], name="post_metrics_post_recent"
    )


__all__ = [
    "VALID_SOURCES",
    "record_metric",
    "reward_for",
    "update_bandit_for",
    "nightly_update",
    "ensure_indexes",
]
