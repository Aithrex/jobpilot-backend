"""Confidence-tiered autonomy — the per-application "auto / hold / ask" brain.

Auto-Apply Tranche 1 §3. This is NOT a global on/off toggle: every application
gets its OWN confidence score and its OWN routing decision, computed at the end
of the gather phase (Tranche 2 §6 `auto_apply_gather`) and stamped onto the
ledger row via `application_ledger_service.mark_ready/mark_needs_input`.

Two pure decisions live here; NEITHER calls an LLM and NEITHER writes to Mongo
except the single cheap read of the user's threshold:

  1. score(application, answer_coverage, ats_family, has_novel_free_text)
        -> float in [0, 1]
     A weighted blend of three signals:
        * ats_ease[ats_family]  — how mechanically submittable this ATS is
          (greenhouse/lever/ashby fill cleanly and predictably; workday/taleo
          are multi-page, JS-heavy, and reset state — lower).
        * answer_coverage       — the fraction of required form fields we could
          resolve from the canonical profile + answer library (from
          answer_library_service.resolve()['coverage']). The dominant term:
          if we can't fill the form, nothing else matters.
        * no_novel_free_text    — 1.0 when there is NO unseen essay / screening
          question, 0.0 when there is (a novel free-text box is the single
          biggest silent-submit risk, so its absence is rewarded directly).

  2. decide(user_email, score, has_unconfirmed_legal_attestation,
            blocked_on_oracle) -> "auto" | "hold" | "ask"
     HARD RULES override the score, ALWAYS, in this order:
        * an unconfirmed LEGAL ATTESTATION (work-auth / sponsorship / EEO) whose
          value is NOT in the user's declared onboarding/visa data  -> "ask".
          NEVER "auto". We will not silently answer a legal question on someone's
          behalf — the IntegrityGuard never-fabricate principle, extended from
          résumé facts to form attestations.
        * blocked on an Oracle step (email verify / password reset / CAPTCHA)
          -> "hold" (a single human tap unblocks it; nothing to fabricate).
     Only if no hard rule fires does the score decide:
        * score >= threshold                -> "auto"  (submit silently)
        * else resolvable by one tap        -> "hold"  (one-tap review queue)
        * else                              -> "ask"   (gather more / escalate)

  The per-user `autonomy_threshold` (default 0.90) lives on the SAME
  `auto_submit_settings` doc as the opt-in toggle (Tranche 1 §3), so a user's
  autonomy appetite is one read alongside their master opt-in.

Layering: the route still enforces the master `AUTO_SUBMIT_ENABLED` kill-switch,
the per-user opt-in, and entitlement/quota BEFORE any submit acts on an "auto"
decision — this module only classifies; it never submits.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from utils.db_connect import DBConnect

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Decisions
# ---------------------------------------------------------------------------
AUTO = "auto"
HOLD = "hold"
ASK = "ask"
VALID_DECISIONS = (AUTO, HOLD, ASK)

# ---------------------------------------------------------------------------
# Threshold (per user, on auto_submit_settings.autonomy_threshold)
# ---------------------------------------------------------------------------
DEFAULT_AUTONOMY_THRESHOLD = 0.90

# How far BELOW the threshold a score may fall and still be "resolvable by one
# tap" (→ hold) rather than "ask". Used only when the caller does not tell us
# explicitly whether the remaining gap is one-tap resolvable. A near-miss is a
# hold (worth a glance); a large gap is an ask (needs real gathering).
HOLD_BAND = 0.15

# ---------------------------------------------------------------------------
# ATS ease table (Tranche 1 §3) — how mechanically submittable each ATS is.
# Higher = a clean, single-form, predictable autofill; lower = multi-step,
# JS-heavy, state-resetting flows where a silent submit is riskier. Unknown
# families get a cautious-moderate default. Keys are lowercased ats_family.
# ---------------------------------------------------------------------------
ATS_EASE: Dict[str, float] = {
    "greenhouse": 0.95,
    "lever": 0.95,
    "ashby": 0.92,
    "workable": 0.85,
    "smartrecruiters": 0.80,
    "jazzhr": 0.80,
    "breezy": 0.80,
    "recruitee": 0.80,
    "linkedin": 0.88,      # LinkedIn Easy Apply — short, structured
    "indeed": 0.70,
    "bamboohr": 0.70,
    "icims": 0.55,
    "successfactors": 0.50,
    "taleo": 0.45,
    "workday": 0.50,       # multi-page, account-gated, state-resetting
    "brassring": 0.45,
    "oracle": 0.45,
}
_DEFAULT_ATS_EASE = 0.60   # unknown / unmapped family: cautious-moderate

# ---------------------------------------------------------------------------
# Score weights (sum to 1.0). answer_coverage dominates: if we cannot fill the
# form, ATS ease and the absence of essays are moot.
# ---------------------------------------------------------------------------
_W_COVERAGE = 0.50
_W_ATS_EASE = 0.30
_W_NO_NOVEL = 0.20


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _clamp01(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def ats_ease(ats_family: Optional[str]) -> float:
    """The ease weight for an ATS family (lowercased lookup), default-moderate
    for anything unknown. Never raises — an odd/None family is just the default."""
    if not ats_family or not isinstance(ats_family, str):
        return _DEFAULT_ATS_EASE
    return ATS_EASE.get(ats_family.strip().lower(), _DEFAULT_ATS_EASE)


def _settings():
    return DBConnect().get_db().auto_submit_settings


def get_autonomy_threshold(user_email: str) -> float:
    """Read the user's `autonomy_threshold` from auto_submit_settings.

    Defaults to 0.90 (conservative) when unset, malformed, or on any read error.
    Clamped to [0, 1] so a bad stored value can never widen the auto band.
    A threshold of 1.0 effectively disables silent auto-submit (nothing scores
    above it) — a valid "review everything" stance.
    """
    if not user_email:
        return DEFAULT_AUTONOMY_THRESHOLD
    try:
        doc = _settings().find_one(
            {"user_email": user_email}, {"_id": 0, "autonomy_threshold": 1}
        )
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("autonomy: threshold read failed for %s: %s", user_email, e)
        return DEFAULT_AUTONOMY_THRESHOLD
    raw = (doc or {}).get("autonomy_threshold")
    if raw is None:
        return DEFAULT_AUTONOMY_THRESHOLD
    try:
        return max(0.0, min(1.0, float(raw)))
    except (TypeError, ValueError):
        return DEFAULT_AUTONOMY_THRESHOLD


def set_autonomy_threshold(user_email: str, threshold: float) -> float:
    """Persist the user's autonomy_threshold onto the existing auto_submit_settings
    doc (upsert, so it coexists with the opt-in toggle without a second collection).
    Clamps to [0, 1]; returns the stored value. The route validates/authz first.
    """
    if not user_email:
        raise ValueError("user_email is required")
    value = max(0.0, min(1.0, float(threshold)))
    from datetime import datetime, timezone
    _settings().update_one(
        {"user_email": user_email},
        {"$set": {"user_email": user_email, "autonomy_threshold": value,
                  "updated_at": datetime.now(timezone.utc)}},
        upsert=True,
    )
    return value


# ---------------------------------------------------------------------------
# score() — the weighted confidence blend (Tranche 1 §3)
# ---------------------------------------------------------------------------
def score(application: Optional[Dict[str, Any]],
          answer_coverage: float,
          ats_family: Optional[str] = None,
          has_novel_free_text: bool = False) -> float:
    """Per-application confidence in [0, 1].

    Inputs:
      * application         — the ledger row (used only as a fallback source for
                              ats_family and for log context; never trusted for
                              scoring math). May be None.
      * answer_coverage     — fraction of required fields resolved from profile +
                              answer library. THIS is answer_library_service
                              .resolve()['coverage']. Dominant term.
      * ats_family          — the ATS the form lives on; falls back to
                              application['ats_family'] when not passed.
      * has_novel_free_text — True if the form has an unseen essay / screening
                              question we have not resolved. Its ABSENCE is
                              rewarded (no_novel_free_text = 1.0).

    Returns a clamped float. Pure — no I/O, no fabrication.
    """
    if not ats_family and isinstance(application, dict):
        ats_family = application.get("ats_family")

    coverage = _clamp01(answer_coverage)
    ease = ats_ease(ats_family)
    no_novel = 0.0 if has_novel_free_text else 1.0

    blended = (
        _W_COVERAGE * coverage
        + _W_ATS_EASE * ease
        + _W_NO_NOVEL * no_novel
    )
    return _clamp01(blended)


# ---------------------------------------------------------------------------
# decide() — hard rules first, then the score (Tranche 1 §3)
# ---------------------------------------------------------------------------
def decide(user_email: str,
           score: float,
           has_unconfirmed_legal_attestation: bool,
           blocked_on_oracle: bool,
           *,
           resolvable_by_one_tap: Optional[bool] = None,
           threshold: Optional[float] = None) -> str:
    """Route ONE application to "auto" | "hold" | "ask".

    HARD RULES (override the score, evaluated in this order):
      1. has_unconfirmed_legal_attestation -> "ask"  (NEVER auto). A work-auth /
         sponsorship / EEO answer that is not in the user's DECLARED data cannot
         be silently answered or one-tap-submitted — we must gather the real,
         user-stated value first. This is the non-negotiable never-fabricate rule
         for legal form fields, and it is checked before the Oracle rule because
         an unconfirmed attestation blocks even a one-tap submit.
      2. blocked_on_oracle -> "hold". An email verify / password reset / CAPTCHA
         is a single human tap away; nothing to fabricate, so it holds (not asks).

    Otherwise the score decides against the user's threshold:
      * score >= threshold                       -> "auto"
      * else resolvable_by_one_tap               -> "hold"
      * else                                     -> "ask"

    `resolvable_by_one_tap`: pass True/False when the gather phase knows whether
    the remaining gap is a single tap (e.g. one unseen yes/no with a suggested
    answer) vs. real missing data. When None (unknown), we treat a near-miss
    (within HOLD_BAND below threshold) as one-tap-resolvable -> hold, and a
    larger gap as -> ask.

    `threshold`: override the per-user lookup (used by the gather job when it has
    already read the threshold; avoids a second DB round trip).
    """
    # --- HARD RULES ---------------------------------------------------------
    if has_unconfirmed_legal_attestation:
        return ASK
    if blocked_on_oracle:
        return HOLD

    # --- SCORE-DRIVEN -------------------------------------------------------
    th = threshold if threshold is not None else get_autonomy_threshold(user_email)
    th = max(0.0, min(1.0, float(th)))
    s = _clamp01(score)

    if s >= th:
        return AUTO

    if resolvable_by_one_tap is None:
        resolvable = s >= max(0.0, th - HOLD_BAND)
    else:
        resolvable = bool(resolvable_by_one_tap)
    return HOLD if resolvable else ASK


# ---------------------------------------------------------------------------
# Convenience: score + decide in one call for the gather job.
# ---------------------------------------------------------------------------
def evaluate(user_email: str,
             application: Optional[Dict[str, Any]],
             *,
             answer_coverage: float,
             ats_family: Optional[str] = None,
             has_novel_free_text: bool = False,
             has_unconfirmed_legal_attestation: bool = False,
             blocked_on_oracle: bool = False,
             resolvable_by_one_tap: Optional[bool] = None) -> Dict[str, Any]:
    """Compute the score AND the decision together — the shape the Tranche 2
    `auto_apply_gather` job stamps onto the ledger row (autonomy + confidence).

    Returns {"confidence_score": float, "autonomy": "auto"|"hold"|"ask",
             "threshold": float}. Reads the user's threshold exactly once and
    reuses it for the decision.
    """
    th = get_autonomy_threshold(user_email)
    s = score(application, answer_coverage, ats_family, has_novel_free_text)
    decision = decide(
        user_email, s,
        has_unconfirmed_legal_attestation, blocked_on_oracle,
        resolvable_by_one_tap=resolvable_by_one_tap,
        threshold=th,
    )
    return {"confidence_score": s, "autonomy": decision, "threshold": th}
