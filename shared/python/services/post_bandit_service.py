"""Signal-loop bandit over LinkedIn post archetypes (Compass §6.7 / §2.2).

The posting substrate already exists: `post_drafting_service.draft_post` writes
3 PRO-generated variants and the LinkedIn `w_member_social` route publishes one.
This module answers the "which archetype should we *try*?" question with a
Thompson-sampling multi-armed bandit, and closes the loop by folding real
engagement outcomes back into the posterior.

Design
------
The variant space is factored into independent DIMENSIONS, each a small set of
mutually-exclusive VALUES. Every (dimension, value) is one *arm* carrying a
Beta(alpha, beta) posterior over "does a post with this trait get engagement?":

    hook_style       : question | story | result | contrarian
    length           : short | medium
    has_code_snippet : y | n
    cta_type         : none | soft | ask
    hour_bucket      : morning | lunch | evening   (a *scheduling* hint, not text)

`pick_variant_spec()` samples one Beta per arm and takes the argmax value per
dimension (Thompson sampling) — this naturally balances explore/exploit and
needs no epsilon schedule. `update(spec, reward)` does the conjugate Bernoulli
update (alpha += reward, beta += 1 - reward) on exactly the arms that were used,
guarded to apply at-most-once per post_id.

This is a pure backend service: Python's `random` is the correct RNG here (no
browser `Math.random` seeding concern). It NEVER calls an LLM and NEVER touches
resume facts — it only decides *shape*. The cringe guardrail
(`cringe_classifier.filter_variants`) runs regardless of what the bandit prefers,
so the posterior can never converge onto engagement bait.

Collections
-----------
  bandit_arms    : {arm_id (unique), dimension, value, alpha, beta, ...}
  bandit_updates : {_id = post_id, spec, reward, applied_at}  # idempotency guard
"""
from __future__ import annotations

import logging
import random
from datetime import datetime, timezone
from typing import Dict, List, Optional

from pymongo.errors import DuplicateKeyError

from utils.db_connect import DBConnect

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Arm space
# --------------------------------------------------------------------------
DIMENSIONS: Dict[str, List[str]] = {
    "hook_style": ["question", "story", "result", "contrarian"],
    "length": ["short", "medium"],
    "has_code_snippet": ["y", "n"],
    "cta_type": ["none", "soft", "ask"],
    "hour_bucket": ["morning", "lunch", "evening"],
}

# Content dimensions steer the *text* of the post; scheduling dimensions steer
# only *when* to post (surfaced to the UI, never written into the copy).
_CONTENT_DIMENSIONS = ("hook_style", "length", "has_code_snippet", "cta_type")
_SCHEDULE_DIMENSIONS = ("hour_bucket",)

# Natural-language guidance the drafter can drop straight into its prompt so the
# generated variants actually *express* the sampled archetype.
_HINTS: Dict[str, Dict[str, str]] = {
    "hook_style": {
        "question": "Open with a genuine, specific question the reader actually cares about — not rhetorical bait.",
        "story": "Open in the middle of a concrete moment from the real experience (a scene, not a summary).",
        "result": "Lead with the concrete outcome first, then briefly how it happened.",
        "contrarian": "Open with a specific, defensible contrarian take that is grounded in the real experience.",
    },
    "length": {
        "short": "Keep it tight: ~40-70 words, one clear idea.",
        "medium": "Medium length: ~90-130 words with a little room to breathe.",
    },
    "has_code_snippet": {
        "y": "Include ONE short, real code snippet or concrete technical artifact — only if it is genuinely relevant and real.",
        "n": "No code snippet.",
    },
    "cta_type": {
        "none": "End with no call to action; let the post stand on its own.",
        "soft": "End with a soft, low-pressure invitation (e.g. inviting others' experience) — never engagement bait.",
        "ask": "End with one specific, honest ask (e.g. who is hiring for X, or feedback on Y).",
    },
}

# Human labels for the scheduling hint the UI shows next to the draft.
HOUR_LABELS: Dict[str, str] = {
    "morning": "Weekday morning (7-9am local)",
    "lunch": "Midday (11am-1pm local)",
    "evening": "Evening (5-8pm local)",
}

# Beta parameters must be strictly positive for random.betavariate; clamp so a
# degenerate all-zeros arm can never blow up the sampler.
_EPS = 1e-6

# Dedicated RNG instance (seedable in tests without perturbing global random).
_rng = random.Random()

# Seed-once-per-process guard to avoid re-upserting 14 arms on every pick.
_seeded = False


def _db():
    return DBConnect().get_db()


def _now():
    return datetime.now(timezone.utc)


def _arm_id(dimension: str, value: str) -> str:
    return f"{dimension}:{value}"


def _clamp01(x: float) -> float:
    try:
        x = float(x)
    except (TypeError, ValueError):
        return 0.0
    if x < 0.0:
        return 0.0
    if x > 1.0:
        return 1.0
    return x


# --------------------------------------------------------------------------
# Seeding
# --------------------------------------------------------------------------
def _ensure_seeded() -> None:
    """Idempotently create every arm with a Beta(1,1) uniform prior.

    Uses $setOnInsert so concurrent Lambdas racing on a cold collection never
    clobber an already-learned posterior. Cheap (14 tiny upserts) and only runs
    once per warm process.
    """
    global _seeded
    if _seeded:
        return
    col = _db().bandit_arms
    for dimension, values in DIMENSIONS.items():
        for value in values:
            col.update_one(
                {"arm_id": _arm_id(dimension, value)},
                {"$setOnInsert": {
                    "arm_id": _arm_id(dimension, value),
                    "dimension": dimension,
                    "value": value,
                    "alpha": 1.0,
                    "beta": 1.0,
                    "created_at": _now(),
                }},
                upsert=True,
            )
    _seeded = True


def _sample_beta(alpha, beta) -> float:
    a = max(float(alpha or 1.0), _EPS)
    b = max(float(beta or 1.0), _EPS)
    return _rng.betavariate(a, b)


# --------------------------------------------------------------------------
# Thompson sampling
# --------------------------------------------------------------------------
def pick_variant_spec() -> Dict[str, str]:
    """Thompson-sample one value per dimension → {dimension: value}.

    For each dimension, draw a Beta sample from each arm's posterior and take the
    argmax. On the very first use every arm is Beta(1,1) (uniform), so early
    picks are effectively random exploration; as rewards arrive the sampler
    concentrates on the arms that actually earn engagement. Fully self-seeding.
    """
    _ensure_seeded()

    arms_by_dim: Dict[str, List[dict]] = {}
    for arm in _db().bandit_arms.find({}):
        arms_by_dim.setdefault(arm.get("dimension"), []).append(arm)

    spec: Dict[str, str] = {}
    for dimension, values in DIMENSIONS.items():
        arms = list(arms_by_dim.get(dimension) or [])
        if not arms:
            # Degenerate fallback (collection wiped mid-flight) — explore uniformly.
            spec[dimension] = _rng.choice(values)
            continue
        # Shuffle so exact ties (common at Beta(1,1)) don't always pick the same arm.
        _rng.shuffle(arms)
        best_value, best_sample = None, -1.0
        for arm in arms:
            s = _sample_beta(arm.get("alpha"), arm.get("beta"))
            if s > best_sample:
                best_sample, best_value = s, arm.get("value")
        spec[dimension] = best_value if best_value in values else _rng.choice(values)
    return spec


# --------------------------------------------------------------------------
# Posterior update (idempotent per post)
# --------------------------------------------------------------------------
def update(spec: Dict[str, str], reward: float, post_id: Optional[str] = None) -> Dict:
    """Conjugate Bernoulli update on the arms named by `spec`.

    reward is clamped to [0,1] (normalized engagement). For each chosen arm:
        alpha += reward ; beta += 1 - reward
    which is the standard Beta-Bernoulli posterior update for a fractional
    reward.

    Idempotency: the update is applied AT MOST ONCE per post_id. We claim the
    post_id by inserting into `bandit_updates` (post_id is the natural `_id`, so
    the write is atomic and needs no extra unique index); a DuplicateKeyError
    means this post's outcome was already folded in and we no-op. Call without a
    post_id (e.g. offline replay) to force an unguarded update.
    """
    reward = _clamp01(reward)

    if post_id:
        pid = str(post_id)
        try:
            _db().bandit_updates.insert_one({
                "_id": pid,
                "spec": spec if isinstance(spec, dict) else {},
                "reward": reward,
                "applied_at": _now(),
            })
        except DuplicateKeyError:
            return {"applied": False, "reason": "already_applied", "post_id": pid, "reward": reward}

    _ensure_seeded()

    updated: List[str] = []
    for dimension, value in (spec or {}).items():
        if dimension not in DIMENSIONS or value not in DIMENSIONS[dimension]:
            # Ignore unknown arms rather than minting junk documents.
            continue
        _db().bandit_arms.update_one(
            {"arm_id": _arm_id(dimension, value)},
            {"$inc": {"alpha": reward, "beta": 1.0 - reward},
             "$set": {"updated_at": _now()}},
        )
        updated.append(_arm_id(dimension, value))

    return {"applied": True, "post_id": str(post_id) if post_id else None,
            "reward": reward, "arms": updated}


# --------------------------------------------------------------------------
# Generation-time helpers (consumed by post_drafting_service)
# --------------------------------------------------------------------------
def describe_spec(spec: Dict[str, str]) -> str:
    """Render the CONTENT dimensions of a spec as prompt guidance the drafter can
    inject to bias generation toward the sampled archetype. Scheduling
    dimensions (hour_bucket) are intentionally excluded — they steer *when* to
    post, not the copy. Returns "" for an empty/None spec."""
    if not isinstance(spec, dict) or not spec:
        return ""
    lines: List[str] = []
    for dim in _CONTENT_DIMENSIONS:
        value = spec.get(dim)
        hint = _HINTS.get(dim, {}).get(value)
        if hint:
            lines.append(f"- {hint}")
    if not lines:
        return ""
    return ("ARCHETYPE TO EXPRESS (a bandit is A/B-testing post shapes — honor these "
            "*structural* choices; they never override the integrity rules):\n" + "\n".join(lines))


def suggested_hour(spec: Dict[str, str]) -> Optional[str]:
    """Human label for the sampled posting-time bucket, for the UI to surface as
    a 'best time to post' nudge. None when unset."""
    if not isinstance(spec, dict):
        return None
    return HOUR_LABELS.get(spec.get("hour_bucket"))


def get_arms() -> List[dict]:
    """Introspection: current posterior of every arm (means for a debug view)."""
    out: List[dict] = []
    for arm in _db().bandit_arms.find({}).sort("arm_id", 1):
        a = float(arm.get("alpha") or 1.0)
        b = float(arm.get("beta") or 1.0)
        out.append({
            "arm_id": arm.get("arm_id"),
            "dimension": arm.get("dimension"),
            "value": arm.get("value"),
            "alpha": a,
            "beta": b,
            "mean": a / (a + b) if (a + b) else 0.0,
            "trials": (a + b) - 2.0,  # observations beyond the Beta(1,1) prior
        })
    return out


def ensure_indexes() -> None:
    """Create the indexes this service relies on. Idempotent; call from a
    bootstrap/migration step, NOT at import time (keep cold starts lean).

    - bandit_arms.arm_id  → unique (one posterior per arm; guards the seed race).
    - bandit_updates._id is the post_id, so its uniqueness is free (no index).
    """
    _db().bandit_arms.create_index("arm_id", unique=True, name="uniq_arm_id")


__all__ = [
    "DIMENSIONS",
    "HOUR_LABELS",
    "pick_variant_spec",
    "update",
    "describe_spec",
    "suggested_hour",
    "get_arms",
    "ensure_indexes",
]
