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

  The per-user `autonomy_threshold` (default 0.75) lives on the SAME
  `auto_submit_settings` doc as the opt-in toggle (Tranche 1 §3), so a user's
  autonomy appetite is one read alongside their master opt-in.

  A SECOND, unrelated threshold also lives on that doc: `match_threshold`
  (0-100, default 65). That one gates JOB MATCH (`fit_score`) for the
  new-posting sweep and has NOTHING to do with form-fillability. See the
  comment above `DEFAULT_MATCH_THRESHOLD` — conflating the two breaks
  `decide()`.

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
# Explicit autonomy MODES (auto-apply-defects-plan.md "## Autonomy MODES").
#
# The mode is the SINGLE source of truth for HOW MUCH latitude the pipeline has,
# and it sits OUTSIDE the per-application `decide()` brain: `decide()` computes a
# raw auto|hold|ask, then the mode caps that decision (see
# auto_apply_service._mode_decision). Stored on `auto_submit_settings.mode`;
# managed_mailboxes.mode reads THROUGH to it (one source, no drift).
#
#   * copilot   — prepare-only. NO browser automation, NO executor dispatch, NO
#                 submit; the user applies manually from the prepared answer
#                 sheet. A raw `auto` is capped to `ask` and the row is flagged
#                 manual_apply. `decide()` is effectively irrelevant (never auto).
#   * approval  — every application waits for an explicit user approval regardless
#                 of confidence: a raw `auto` is capped to `ask`.
#   * autopilot — full auto (no per-application permission) — but ONLY after the
#                 graduation gate (>= AUTOPILOT_GRADUATION_N user-approved
#                 confirmations); below it, `auto` is capped to `ask`. Hard
#                 never-fabricate rules (unconfirmed legal attestation -> ask)
#                 still win, always.
#   * aggressive — public "Auto Aggressive" mode: same as autopilot but skips
#                 the graduation gate so matching jobs can submit daily from day
#                 one (still subject to never-fabricate + quota). The hourly
#                 apply scheduler enqueues more matches for this mode.
#
# Product labels (FE): Manual=copilot, Auto=approval, Auto Aggressive=aggressive.
# `autopilot` remains a valid stored value for existing users.
#
# DECIDED (user, July 2026): there is NO implicit default — onboarding FORCES an
# explicit pick. `get_mode` returns None until it is set, and the scheduler/
# executor REFUSE to auto-act while it is unset (fail-safe). No default is ever
# written here.
# ---------------------------------------------------------------------------
MODE_COPILOT = "copilot"
MODE_APPROVAL = "approval"
MODE_AUTOPILOT = "autopilot"
MODE_AGGRESSIVE = "aggressive"
VALID_MODES = (MODE_COPILOT, MODE_APPROVAL, MODE_AUTOPILOT, MODE_AGGRESSIVE)

# Autopilot graduation gate: `auto` is honored only once the user has at least
# this many canonical applications confirmed via a USER-approved path. LOCKED at
# 10 (auto-apply-defects-plan.md "## Autonomy MODES").
AUTOPILOT_GRADUATION_N = 10

# ---------------------------------------------------------------------------
# Threshold (per user, on auto_submit_settings.autonomy_threshold)
#
# 0.75, LOWERED from 0.90 (July 2026). 0.90 was structurally UNREACHABLE for
# every account-gated vendor, which silently disabled auto-submit for the ATS
# that matters most. The arithmetic (see ATS_EASE + the weights below):
#
#     max achievable = 0.50*1.0 + 0.30*ats_ease + 0.20*1.0 = 0.70 + 0.30*ease
#
#   greenhouse/lever (0.95) -> 0.985   ashby (0.92) -> 0.976
#   workday          (0.50) -> 0.850   unknown (0.60) -> 0.880
#
# A Workday application with PERFECT answer coverage and NO essay tops out at
# 0.85, so at a 0.90 threshold `decide()` could never return "auto" for one -
# no matter how good the job match was. 0.75 leaves every implemented vendor
# reachable while still demanding real coverage (Workday needs >= 0.80).
# `max_achievable_score()` below makes that invariant testable.
# ---------------------------------------------------------------------------
DEFAULT_AUTONOMY_THRESHOLD = 0.75

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
#
# HONESTY NOTE (July 2026): this table used to list 17 vendors, which read as
# "we support 17 ATSs". We do not. Only the four in IMPLEMENTED_ATS below have
# a real fill+submit path (`browser_executor` / `ats_executor`); iCIMS,
# SmartRecruiters and Workable are DETECTED by `ats_detector` but have no
# executor, and everything else is unsupported (spec §4). The unimplemented
# entries are kept ONLY so that a detected-but-unsupported row scores honestly
# low instead of falling into the cautious-moderate unknown default — they are
# not a support claim. `application_ledger_service.STATUS_UNSUPPORTED` is what
# actually surfaces the gap to the user.
# ---------------------------------------------------------------------------

# The vendors with a real end-to-end executor. This tuple is the support claim;
# ATS_EASE is only a scoring weight.
IMPLEMENTED_ATS: tuple = ("greenhouse", "lever", "ashby", "workday")

ATS_EASE: Dict[str, float] = {
    # --- implemented: fill + submit ----------------------------------------
    "greenhouse": 0.95,    # single clean form
    "lever": 0.95,         # single clean form
    "ashby": 0.92,         # single form, some dynamic sections
    "workday": 0.50,       # multi-page, ACCOUNT-GATED, state-resetting.
                           # Caps the blended score at 0.85 — see the threshold
                           # note above; do not raise this to paper over a
                           # threshold problem, it is an honest difficulty
                           # weight and `max_achievable_score()` asserts it.
    # --- detected by ats_detector, NO executor yet (spec §4) ---------------
    # These never auto-submit today; they route to `unsupported`. The weights
    # are retained so that if/when an executor lands, the score is already
    # calibrated rather than defaulting to the unknown 0.60.
    "workable": 0.85,
    "smartrecruiters": 0.80,
    "icims": 0.55,
    # --- not detected, not implemented: reference weights only -------------
    "jazzhr": 0.80,
    "breezy": 0.80,
    "recruitee": 0.80,
    "linkedin": 0.88,      # LinkedIn Easy Apply — short, structured
    "indeed": 0.70,
    "bamboohr": 0.70,
    "successfactors": 0.50,
    "taleo": 0.45,
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


def max_achievable_score(ats_family: Optional[str]) -> float:
    """The BEST score `score()` can return for this ATS: perfect answer coverage
    and no novel free-text. Anything above this is structurally unreachable.

    Exists because a threshold above this number is not "conservative", it is a
    silent OFF switch for that vendor — the 0.90/Workday defect. Tests assert
    `DEFAULT_AUTONOMY_THRESHOLD <= max_achievable_score(v)` for every vendor in
    IMPLEMENTED_ATS so the defect cannot come back by a table edit.
    """
    return _clamp01(_W_COVERAGE * 1.0 + _W_ATS_EASE * ats_ease(ats_family)
                    + _W_NO_NOVEL * 1.0)


def threshold_reachable(ats_family: Optional[str], threshold: float) -> bool:
    """Can an application on this ATS ever reach `threshold`? False means the
    row can only ever be hold/ask — worth logging, never worth silently
    overriding the user's stored preference."""
    try:
        th = max(0.0, min(1.0, float(threshold)))
    except (TypeError, ValueError):
        return False
    return max_achievable_score(ats_family) >= th


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
# MATCH threshold (per user, on auto_submit_settings.match_threshold)
#
# STRICTLY SEPARATE from `autonomy_threshold` above. Conflating them breaks
# `decide()`, so the difference is spelled out here once:
#
#   autonomy_threshold  0.0-1.0  FORM-FILLABILITY. "Can we fill and submit this
#                                form unattended?" = 0.50*coverage +
#                                0.30*ats_ease + 0.20*no_novel. Consumed by
#                                `decide()`. Says NOTHING about the job.
#   match_threshold     0-100    JOB MATCH. `fit_score.score_one()['match_score']`
#                                — how well the RESUME fits the POSTING. Consumed
#                                by `apply_match_sweep` to decide whether a newly
#                                discovered posting is worth an application at
#                                all. Says NOTHING about the form.
#
# 65 is the bottom of the fit_score "good match" band (fit_score.BANDS), which
# is the same line the guest/feed surfaces already draw. Below it we do not
# spend an application slot unattended.
# ---------------------------------------------------------------------------
DEFAULT_MATCH_THRESHOLD = 65


# ---------------------------------------------------------------------------
# Review gate for MODEL-WRITTEN FREE TEXT (essays: "why this company", etc.)
#
# `answer_generator` stamps every generated record `review_required: True`. This
# flag decides whether that stamp actually parks the row for a human read.
#
# DEFAULT_REVIEW_GENERATED_PROSE = False — the product owner chose hands-off
# submission: holding every posting that asks "why us?" would defeat applying
# overnight, which is the point of the feature.
#
# It is a per-user setting rather than a deleted branch because the risk is real
# (prose written in the user's voice reaching a real employer unread) and
# reverting has to be one settings write, not a deploy.
#
# SCOPE: free text ONLY. It cannot relax the hard rules in `decide()` — an
# unconfirmed legal attestation still forces `ask` and an oracle wall still
# forces `hold`. Generated answers are résumé-grounded and entitlement-gated;
# this flag never permits inventing a FACT, only sending prose unreviewed.
# ---------------------------------------------------------------------------
DEFAULT_REVIEW_GENERATED_PROSE = False


def review_generated_prose(user_email: str) -> bool:
    """True if model-written free text should park the row for a human read.

    Defaults to False (auto-submit). Any read error returns the default rather
    than raising — this sits on the gather path and must never break it.
    """
    if not user_email:
        return DEFAULT_REVIEW_GENERATED_PROSE
    try:
        doc = _settings().find_one(
            {"user_email": user_email}, {"_id": 0, "review_generated_prose": 1}
        )
    except Exception as e:  # pragma: no cover - defensive
        logger.warning(
            "autonomy: review_generated_prose read failed for %s: %s", user_email, e)
        return DEFAULT_REVIEW_GENERATED_PROSE
    raw = (doc or {}).get("review_generated_prose")
    if raw is None:
        return DEFAULT_REVIEW_GENERATED_PROSE
    return bool(raw)


def get_match_threshold(user_email: str) -> int:
    """Read the user's `match_threshold` (0-100) from auto_submit_settings.

    Defaults to 65 when unset, malformed, or on any read error. Clamped to
    [0, 100] so a bad stored value can never widen the auto-apply band. A
    threshold of 101+ is impossible by clamping; 100 means "only a perfect
    fit", which is a valid (effectively off) stance.
    """
    if not user_email:
        return DEFAULT_MATCH_THRESHOLD
    try:
        doc = _settings().find_one(
            {"user_email": user_email}, {"_id": 0, "match_threshold": 1}
        )
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("autonomy: match threshold read failed for %s: %s", user_email, e)
        return DEFAULT_MATCH_THRESHOLD
    raw = (doc or {}).get("match_threshold")
    if raw is None:
        return DEFAULT_MATCH_THRESHOLD
    try:
        return max(0, min(100, int(round(float(raw)))))
    except (TypeError, ValueError):
        return DEFAULT_MATCH_THRESHOLD


def set_match_threshold(user_email: str, threshold: Any) -> int:
    """Persist the user's match_threshold onto the SAME auto_submit_settings doc
    (upsert), so the whole autonomy posture stays one read / one write. Clamps to
    [0, 100]; returns the stored value. A non-numeric value raises ValueError —
    the route maps that to 400, NEVER 401.
    """
    if not user_email or not isinstance(user_email, str):
        raise ValueError("user_email is required")
    try:
        value = max(0, min(100, int(round(float(threshold)))))
    except (TypeError, ValueError):
        raise ValueError("match_threshold must be a number between 0 and 100")
    from datetime import datetime, timezone
    _settings().update_one(
        {"user_email": user_email},
        {"$set": {"user_email": user_email, "match_threshold": value,
                  "updated_at": datetime.now(timezone.utc)}},
        upsert=True,
    )
    return value


# ---------------------------------------------------------------------------
# Mode (per user, on auto_submit_settings.mode) — the SINGLE source of truth.
# user_email is the trusted JWT identity (same convention as
# get_autonomy_threshold), and lives on the SAME auto_submit_settings doc as the
# opt-in + threshold, so the whole autonomy posture is one read/one write.
# ---------------------------------------------------------------------------
def get_mode(user_email: str) -> Optional[str]:
    """The user's explicit autonomy mode, or None when unset.

    None is deliberate — there is NO implicit default (onboarding forces the
    pick), and the gather/executor path REFUSES to auto-act while it is None. A
    stored value outside VALID_MODES normalizes to None (never silently trusted).
    """
    if not user_email or not isinstance(user_email, str):
        return None
    try:
        doc = _settings().find_one({"user_email": user_email}, {"_id": 0, "mode": 1})
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("autonomy: mode read failed for %s: %s", user_email, e)
        return None
    mode = (doc or {}).get("mode")
    return mode if mode in VALID_MODES else None


def set_mode(user_email: str, mode: str) -> str:
    """Persist the user's autonomy mode onto the existing auto_submit_settings doc
    (upsert, coexisting with the opt-in + threshold). Validates the enum — a bad
    value raises ValueError (the route maps it to 400, NEVER 401). No default is
    written by any other path; this is the only writer of `mode`."""
    if not user_email or not isinstance(user_email, str):
        raise ValueError("user_email is required")
    if mode not in VALID_MODES:
        raise ValueError(f"mode must be one of {VALID_MODES}")
    from datetime import datetime, timezone
    _settings().update_one(
        {"user_email": user_email},
        {"$set": {"user_email": user_email, "mode": mode,
                  "updated_at": datetime.now(timezone.utc)}},
        upsert=True,
    )
    return mode


def autopilot_unlocked(user_email: str) -> bool:
    """Autopilot graduation gate: has the user EARNED hands-off `auto`?

    True once the user has at least AUTOPILOT_GRADUATION_N (10) canonical
    `applications` that reached `confirmed` via a USER-approved path
    (`approval_snapshot.approved_by == "user"`). Below that, autopilot's `auto` is
    capped to `ask` (see auto_apply_service._mode_decision).

    System-approved autopilot confirmations (`approved_by == "system"`) are
    DELIBERATELY excluded: counting them would let autopilot bootstrap its own
    unlock (the first 10 silent submits would graduate it with zero human
    review). The gate must be earned by real user-reviewed approvals — a
    documented anti-bootstrap choice.
    """
    if not user_email or not isinstance(user_email, str):
        return False
    try:
        n = DBConnect().get_db().applications.count_documents({
            "user_email": user_email,
            # canonical terminal-good (application_ledger_service.STATUS_CONFIRMED)
            "status": "confirmed",
            "approval_snapshot.approved_by": "user",
        })
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("autonomy: autopilot_unlocked count failed for %s: %s",
                       user_email, e)
        return False
    return n >= AUTOPILOT_GRADUATION_N


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
             "threshold": float, "threshold_reachable": bool}. Reads the user's
    threshold exactly once and reuses it for the decision.

    `threshold_reachable` is False when the user's stored threshold is above
    what this ATS can EVER score (the 0.90-vs-Workday defect). We log it rather
    than clamping it: silently overriding an explicit user setting would be
    worse than a row that never auto-submits. The live signal is what tells an
    operator to fix the setting.
    """
    if not ats_family and isinstance(application, dict):
        ats_family = application.get("ats_family")
    th = get_autonomy_threshold(user_email)
    s = score(application, answer_coverage, ats_family, has_novel_free_text)
    decision = decide(
        user_email, s,
        has_unconfirmed_legal_attestation, blocked_on_oracle,
        resolvable_by_one_tap=resolvable_by_one_tap,
        threshold=th,
    )
    reachable = threshold_reachable(ats_family, th)
    if not reachable:
        logger.warning(
            "autonomy: threshold %.2f is UNREACHABLE on ats=%s (max %.3f) — "
            "this row can never auto-submit; lower the user's autonomy_threshold",
            th, str(ats_family or "unknown"), max_achievable_score(ats_family))
    return {"confidence_score": s, "autonomy": decision, "threshold": th,
            "threshold_reachable": reachable}
