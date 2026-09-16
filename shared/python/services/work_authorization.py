"""Work authorization as a FIRST-CLASS preference, and the feed policy it drives.

Why this module exists
----------------------
Measured on live `job_postings`, 2026-09-15, active + `tech.tech_scope ==
"core"` (31,220 rows):

    requirements.clearance_required            true on 3,004
    requirements.work_authorization_restricted true on 2,076

    Northrop Grumman   396 core postings, 396 no-sponsorship, 364 clearance
    RTX                558 core postings, 432 no-sponsorship, 486 clearance
    Booz Allen         327 core postings, 303 clearance
    Leidos             301 core postings, 251 clearance
    GDIT               227 core postings, 224 clearance, 224 no-sponsorship

Both flags were already populated at ingest and **nothing in retrieval or
ranking read either of them**. `fit_score._hard_constraints` turns them into a
score CAP, but only when the RESUME states a sponsorship need — which a resume
almost never does, so over a live run of 4,000 postings `constraints` came back
empty every single time. The signal existed and was thrown away, and an
international student on OPT got ~2,300 US-cleared defence-prime postings at
the top of their feed.

The three questions this module answers
---------------------------------------
1. **What is this user's work authorization?** One question, honest options,
   `unknown` by default, persisted in `visa_profiles` — the collection that
   ALREADY models this user's immigration state (`visa_timeline_service`) and
   that `answer_library_service._derive_legal_from_visa` already reads. A
   third home for the same fact is how two surfaces come to disagree.
2. **Given that status and a posting's flags, is this job reachable?**
   `assess()` — a pure function, no DB, no LLM, fully testable.
3. **What should that do to the ORDER?** `assess()` returns a multiplicative
   `penalty` on `rank_score` and a `tier` the feed sorts on. Never
   `match_score`: `fit_score` (ADR-002) is the one candidate-to-job score and
   this is a preference signal, so it lives in `rank_score`, exactly where
   employer diversity and source quality already do.

Filter or demote? DEMOTE, with an explicit opt-in filter
---------------------------------------------------------
The default is a hard DEMOTION plus a visible badge, and a user-controllable
filter (`hide_sponsorship_restricted` / `hide_clearance_required`) that removes
the rows entirely for anyone who wants that.

* A hard filter ON BY DEFAULT would silently delete reachable jobs. The
  posting's flag is an extraction from free text, the employer's statement is
  often about a DIFFERENT requisition family, and defence primes do hire
  non-cleared staff onto non-cleared programmes. This codebase's stated rule —
  `fit_score`'s false-INELIGIBLE gate is held at zero, `_ineligible` demotes
  only on a POSITIVE finding — is that removing a reachable job is the worst
  error available.
* A demotion that is merely arithmetic is not enough either. A -0.6 source
  penalty on a 0.15-weighted component moves a row by 0.09 of `rank_score`,
  which is why marketplace reposts still led the feed. So when the user has
  DECLARED they need sponsorship, an explicitly-restricted posting is relegated
  by a SORT TIER — below every reachable row — not by arithmetic. It is still
  in the result set, still counted in `total`, still reachable by paging.
* `unknown` is treated conservatively but never punitively: no relegation at
  all, a mild multiplicative penalty on postings that state "no sponsorship",
  and the badge either way. "No sponsorship" is a fact about the POSTING and
  is worth showing to everyone; not knowing the reader must never cost what
  being wrong costs (the same rule `tech_taxonomy` and `fit_score` apply).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

WORK_AUTH_VERSION = "work-auth-1.0.0"

# ---------------------------------------------------------------------------
# The statuses. One question, five honest answers, plus "we did not ask".
# ---------------------------------------------------------------------------
#: We have not asked, or the user declined. THE DEFAULT, and it must behave
#: conservatively-but-not-punitively everywhere.
UNKNOWN = "unknown"
#: US citizen. Clearance-eligible, never needs sponsorship.
US_CITIZEN = "us_citizen"
#: Lawful permanent resident / green card. Never needs sponsorship. NOT
#: generally clearance-eligible — most cleared roles require citizenship — so
#: this is deliberately a different answer from `us_citizen`.
US_PERMANENT_RESIDENT = "us_permanent_resident"
#: Needs an employer to petition BEFORE they can start (cap-exempt transfer,
#: H-1B transfer, a fresh petition). An explicit "we do not sponsor" is a
#: blocker today.
NEEDS_SPONSORSHIP_NOW = "needs_sponsorship_now"
#: Authorized to work now (CPT / OPT / STEM-OPT) and will need sponsorship to
#: continue. An explicit "we do not sponsor" is a blocker within the job's
#: horizon, which is why it is treated the same way for ordering.
NEEDS_SPONSORSHIP_FUTURE = "needs_sponsorship_future"
#: Not authorized to work in the US and not seeking a US role (non-US based).
NON_US = "non_us"

WORK_AUTH_STATUSES: Tuple[str, ...] = (
    UNKNOWN, US_CITIZEN, US_PERMANENT_RESIDENT,
    NEEDS_SPONSORSHIP_NOW, NEEDS_SPONSORSHIP_FUTURE, NON_US,
)

#: The question and its options, served to the client so the copy lives in ONE
#: place and the FE cannot invent a sixth option the backend does not model.
WORK_AUTH_QUESTION = "What is your work authorization in the United States?"
WORK_AUTH_OPTIONS: Tuple[Dict[str, str], ...] = (
    {"value": US_CITIZEN, "label": "U.S. citizen",
     "help": "No sponsorship needed. Eligible for roles that require a security clearance."},
    {"value": US_PERMANENT_RESIDENT, "label": "U.S. permanent resident (green card)",
     "help": "No sponsorship needed. Most clearance roles still require citizenship."},
    {"value": NEEDS_SPONSORSHIP_NOW, "label": "I need visa sponsorship to start",
     "help": "An employer has to petition before you can begin work."},
    {"value": NEEDS_SPONSORSHIP_FUTURE,
     "label": "I can work now (CPT / OPT / STEM-OPT) but will need sponsorship later",
     "help": "Authorized today; you will need H-1B or similar to continue."},
    {"value": NON_US, "label": "I am not authorized to work in the U.S.",
     "help": "We will not prioritise U.S.-only roles for you."},
    {"value": UNKNOWN, "label": "Prefer not to say",
     "help": "We will still flag clearance and no-sponsorship postings, but we "
             "will not reorder your feed around them."},
)

#: Statuses for which an explicit "no sponsorship" posting is a blocker.
_SPONSORSHIP_NEEDED = frozenset({NEEDS_SPONSORSHIP_NOW, NEEDS_SPONSORSHIP_FUTURE,
                                 NON_US})
#: Statuses that make a clearance requirement reachable. `us_permanent_resident`
#: is deliberately NOT here: the overwhelming majority of cleared US postings
#: require citizenship, and a green-card holder finding that out at the
#: interview is the failure mode we are removing.
_CLEARANCE_ELIGIBLE = frozenset({US_CITIZEN})
#: Statuses that need no petition at all.
_NO_SPONSORSHIP_NEEDED = frozenset({US_CITIZEN, US_PERMANENT_RESIDENT})


def normalize_status(value: Any) -> str:
    """Any input -> one of `WORK_AUTH_STATUSES`. Never raises, never guesses.

    Anything unrecognised becomes `unknown`, which is also what an absent value
    means. That is deliberate: a typo must not silently assert a status the
    user did not pick, and `unknown` is the only safe wrong answer here.
    """
    s = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if s in WORK_AUTH_STATUSES:
        return s
    # A small, EXPLICIT alias table. Not fuzzy matching: an unrecognised string
    # is `unknown`, because inferring "citizen" from an arbitrary free-text
    # answer is exactly how a user ends up mislabelled.
    return {
        "citizen": US_CITIZEN, "us": US_CITIZEN, "usc": US_CITIZEN,
        "green_card": US_PERMANENT_RESIDENT, "greencard": US_PERMANENT_RESIDENT,
        "permanent_resident": US_PERMANENT_RESIDENT, "lpr": US_PERMANENT_RESIDENT,
        "gc": US_PERMANENT_RESIDENT,
        "needs_sponsorship": NEEDS_SPONSORSHIP_NOW,
        "requires_sponsorship": NEEDS_SPONSORSHIP_NOW,
        "sponsorship_now": NEEDS_SPONSORSHIP_NOW,
        "sponsorship_future": NEEDS_SPONSORSHIP_FUTURE,
        "opt": NEEDS_SPONSORSHIP_FUTURE, "cpt": NEEDS_SPONSORSHIP_FUTURE,
        "stem_opt": NEEDS_SPONSORSHIP_FUTURE,
        "outside_us": NON_US, "international": NON_US,
        "": UNKNOWN, "prefer_not_to_say": UNKNOWN, "none": UNKNOWN,
    }.get(s, UNKNOWN)


def needs_sponsorship(status: Any) -> bool:
    """True when an explicit "we do not sponsor" is a blocker for this user."""
    return normalize_status(status) in _SPONSORSHIP_NEEDED


def clearance_eligible(status: Any) -> Optional[bool]:
    """True / False / None(unknown) for "can this user hold a cleared role?"

    `None` for `unknown` is the whole point: not knowing is a third answer and
    it must not be rendered as either of the other two.
    """
    s = normalize_status(status)
    if s == UNKNOWN:
        return None
    return s in _CLEARANCE_ELIGIBLE


def is_us_person(status: Any) -> bool:
    """Citizen or permanent resident — needs no petition of any kind."""
    return normalize_status(status) in _NO_SPONSORSHIP_NEEDED


# ---------------------------------------------------------------------------
# Persistence. `visa_profiles` is REUSED, not replaced.
# ---------------------------------------------------------------------------
#: `visa_timeline_service.VALID_VISA_STATUS` -> the work-auth answer it implies.
#:
#: This is the migration path for the users who already told us their F-1
#: state: they must not be asked a question they have effectively answered.
#: It mirrors `answer_library_service._derive_legal_from_visa`, which maps the
#: same five statuses to `requires_sponsorship: True`, so the auto-apply answer
#: bank and the feed cannot disagree about the same person.
_VISA_STATUS_TO_WORK_AUTH: Dict[str, str] = {
    "F1": NEEDS_SPONSORSHIP_FUTURE,
    "F1-CPT": NEEDS_SPONSORSHIP_FUTURE,
    "F1-OPT": NEEDS_SPONSORSHIP_FUTURE,
    "F1-STEM-OPT": NEEDS_SPONSORSHIP_FUTURE,
    # An H-1B holder still needs the next employer to petition for a transfer.
    "H1B": NEEDS_SPONSORSHIP_NOW,
}

#: Where a resolved status came from, strongest evidence first. Reported on the
#: API so the client can say "you told us" versus "we inferred this from your
#: visa timeline" versus "we are guessing nothing".
SOURCE_DECLARED = "declared"            #: the user answered the question
SOURCE_VISA_PROFILE = "visa_profile"    #: derived from `visa_profiles.visa_status`
SOURCE_LEGACY_PREFERENCE = "user_preferences"  #: legacy `requires_sponsorship` bool
SOURCE_NONE = "none"                    #: nobody has ever told us


def _db():
    from utils.db_connect import DBConnect
    return DBConnect().get_db()


def _safe_email(user_email: Any) -> str:
    """Sanitize before the value can reach a Mongo filter (invariant 4)."""
    try:
        from utils.security import InputSanitizer
        return InputSanitizer.sanitize_email(str(user_email or "")) or ""
    except Exception:      # pragma: no cover - a sanitizer outage must not 500
        return ""


def _as_iso(value: Any) -> Optional[str]:
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    return None


def blank_profile() -> Dict[str, Any]:
    """The shape every read returns, for a user we know nothing about."""
    return {
        "work_authorization": UNKNOWN,
        "source": SOURCE_NONE,
        "needs_sponsorship": False,
        "clearance_eligible": None,
        "is_us_person": False,
        "hide_sponsorship_restricted": False,
        "hide_clearance_required": False,
        "updated_at": None,
        "version": WORK_AUTH_VERSION,
    }


def get_profile(user_email: Any) -> Dict[str, Any]:
    """THE read. `visa_profiles` first, legacy preference second, else unknown.

    Never raises and never fails a search: a feed that silently stops
    reordering because a preference lookup broke is better than a 500, and the
    response carries `source` so the degradation is visible rather than silent.
    """
    out = blank_profile()
    email = _safe_email(user_email)
    if not email:
        return out
    try:
        db = _db()
        doc = db.visa_profiles.find_one(
            {"user_email": email},
            {"_id": 0, "work_authorization": 1, "visa_status": 1,
             "hide_sponsorship_restricted": 1, "hide_clearance_required": 1,
             "work_authorization_updated_at": 1, "updated_at": 1}) or {}
    except Exception as exc:      # pragma: no cover - never fail a search
        logger.warning("work_authorization: visa_profiles unavailable (%s)",
                       type(exc).__name__)
        doc = {}

    declared = normalize_status(doc.get("work_authorization"))
    if declared != UNKNOWN:
        out["work_authorization"] = declared
        out["source"] = SOURCE_DECLARED
    else:
        derived = _VISA_STATUS_TO_WORK_AUTH.get(str(doc.get("visa_status") or ""))
        if derived:
            out["work_authorization"] = derived
            out["source"] = SOURCE_VISA_PROFILE

    out["hide_sponsorship_restricted"] = bool(doc.get("hide_sponsorship_restricted"))
    out["hide_clearance_required"] = bool(doc.get("hide_clearance_required"))
    out["updated_at"] = _as_iso(doc.get("work_authorization_updated_at")
                                or doc.get("updated_at"))

    if out["work_authorization"] == UNKNOWN:
        # LEGACY. `inventory_feed.requires_sponsorship` has read this boolean
        # since before `visa_profiles` existed; honouring it means a user who
        # answered the old onboarding question is not asked again.
        try:
            prefs = _db().user_preferences.find_one(
                {"user_email": email}, {"_id": 0, "requires_sponsorship": 1}) or {}
        except Exception:      # pragma: no cover
            prefs = {}
        if prefs.get("requires_sponsorship"):
            out["work_authorization"] = NEEDS_SPONSORSHIP_NOW
            out["source"] = SOURCE_LEGACY_PREFERENCE

    status = out["work_authorization"]
    out["needs_sponsorship"] = needs_sponsorship(status)
    out["clearance_eligible"] = clearance_eligible(status)
    out["is_us_person"] = is_us_person(status)
    return out


def set_profile(user_email: Any, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Write the declared status and the two hide-toggles. Returns the new read.

    Additive on `visa_profiles`: nothing the Visa Timeline Copilot writes is
    touched, and a user who has never opened that feature gets a row holding
    only this answer. `$set` of literal keys only — no user value ever reaches
    an operator position.
    """
    email = _safe_email(user_email)
    if not email:
        return blank_profile()
    body = payload if isinstance(payload, dict) else {}

    update: Dict[str, Any] = {"user_email": email,
                              "work_authorization_updated_at": datetime.utcnow()}
    if "work_authorization" in body:
        update["work_authorization"] = normalize_status(body.get("work_authorization"))
    for key in ("hide_sponsorship_restricted", "hide_clearance_required"):
        if key in body:
            update[key] = bool(body.get(key))
    try:
        _db().visa_profiles.update_one({"user_email": email}, {"$set": update},
                                       upsert=True)
    except Exception as exc:
        logger.exception("work_authorization: save failed (%s)", type(exc).__name__)
        raise
    return get_profile(email)


# ---------------------------------------------------------------------------
# The policy. Pure functions over (status, posting flags).
# ---------------------------------------------------------------------------
#: `assess().reachability` values.
REACH_LIKELY = "likely"        #: nothing on the posting stands in this user's way
REACH_UNLIKELY = "unlikely"    #: the posting STATES something this user cannot meet
REACH_UNKNOWN = "unknown"      #: the posting said nothing, or we do not know the user

#: Badge keys. The FE renders copy for these; the backend also ships a `label`
#: so a client that has not shipped copy yet still says something true.
BADGE_NO_SPONSORSHIP = "no_sponsorship"
BADGE_SPONSORSHIP_OFFERED = "sponsorship_offered"
BADGE_CLEARANCE_REQUIRED = "clearance_required"

_BADGE_LABELS = {
    BADGE_NO_SPONSORSHIP: "No visa sponsorship",
    BADGE_SPONSORSHIP_OFFERED: "Sponsorship available",
    BADGE_CLEARANCE_REQUIRED: "Security clearance required",
}

#: Multiplicative penalties on `rank_score`. Multiplicative rather than
#: subtractive for two reasons: it cannot produce a negative score, and it
#: preserves the relative order WITHIN a demoted class, so the best of the
#: unreachable rows is still the first one the user meets when they page down.
#:
#: These are stated, not tuned. They are an ordering heuristic over an already
#: filtered set and every one of them is reported in `why_ranked` so a user can
#: see what moved a row.
PENALTY_BLOCKED = 0.25          #: declared need + the posting states the opposite
PENALTY_CLEARANCE_BLOCKED = 0.30
#: A permanent resident DECLARED their status, and most cleared US postings
#: require citizenship — but some accept an LPR (public trust, some IC
#: contracts), so this is a firm demotion and never a relegation.
PENALTY_NOT_CLEARANCE_ELIGIBLE = 0.45
#: UNKNOWN. Deliberately MILD. The undeclared population contains US citizens
#: for whom neither flag costs anything, so a heavy penalty here would bury
#: reachable work on a question we never asked. Enough to stop an unreachable
#: row leading the page, not enough to hide the defence sector from someone who
#: can work in it. The BADGE, not the penalty, is what `unknown` is really for.
PENALTY_UNKNOWN_NO_SPONSOR = 0.85   #: nobody asked the user; the POSTING still said it
PENALTY_UNKNOWN_CLEARANCE = 0.92

#: Sort tiers. 0 sorts first. Used as a key AFTER `fit_score`'s `ineligible`
#: verdict and BEFORE `rank_score`, so a relegation is decisive rather than
#: arithmetic — a -0.6 adjustment on a 0.15-weighted component moves a row by
#: 0.09, which is why the marketplace penalty never actually worked.
TIER_OK = 0
TIER_DEMOTED = 1


def assess(*, status: Any, clearance_required: Optional[bool],
           sponsorship: Optional[str]) -> Dict[str, Any]:
    """`(reachability, badges, penalty, tier, reasons)` for ONE posting.

    Pure: no DB, no network, no clock. `clearance_required` is `True` or `None`
    (never `False` — see `tech_feed_service._clearance_of`: "this posting did
    not mention a clearance" is not "this posting does not require one") and
    `sponsorship` is `"offered"` / `"not_offered"` / `None`.
    """
    s = normalize_status(status)
    restricted = (str(sponsorship or "") == "not_offered")
    offers = (str(sponsorship or "") == "offered")
    cleared = bool(clearance_required)

    badges = []
    if cleared:
        badges.append(BADGE_CLEARANCE_REQUIRED)
    if restricted:
        badges.append(BADGE_NO_SPONSORSHIP)
    elif offers:
        badges.append(BADGE_SPONSORSHIP_OFFERED)

    penalty = 1.0
    tier = TIER_OK
    reasons = []
    reachability = REACH_UNKNOWN if (cleared or restricted) else REACH_LIKELY

    if needs_sponsorship(s):
        if restricted:
            penalty = min(penalty, PENALTY_BLOCKED)
            tier = TIER_DEMOTED
            reachability = REACH_UNLIKELY
            reasons.append("This posting states it does not offer visa sponsorship")
        if cleared:
            penalty = min(penalty, PENALTY_CLEARANCE_BLOCKED)
            tier = TIER_DEMOTED
            reachability = REACH_UNLIKELY
            reasons.append("This posting requires an active U.S. security clearance")
    elif is_us_person(s):
        # No petition needed, so a no-sponsorship statement is irrelevant to
        # this reader and must not cost the posting anything.
        if cleared and clearance_eligible(s) is False:
            penalty = min(penalty, PENALTY_NOT_CLEARANCE_ELIGIBLE)
            reachability = REACH_UNKNOWN
            reasons.append("Most cleared roles require U.S. citizenship")
        elif cleared:
            reachability = REACH_LIKELY
        else:
            reachability = REACH_LIKELY
    else:
        # UNKNOWN. Conservative, not punitive: no relegation, a mild penalty,
        # and the badge either way — "no sponsorship" is a fact about the
        # posting regardless of who is reading it.
        if restricted:
            penalty = min(penalty, PENALTY_UNKNOWN_NO_SPONSOR)
            reasons.append("This posting states it does not offer visa sponsorship")
        if cleared:
            penalty = min(penalty, PENALTY_UNKNOWN_CLEARANCE)
            reasons.append("This posting requires an active U.S. security clearance")

    return {
        "reachability": reachability,
        "badges": [{"key": b, "label": _BADGE_LABELS[b]} for b in badges],
        "badge_keys": badges,
        "penalty": penalty,
        "tier": tier,
        "reasons": reasons,
        "work_authorization": s,
    }


def should_hide(*, assessment: Dict[str, Any], hide_sponsorship_restricted: bool,
                hide_clearance_required: bool) -> bool:
    """The OPT-IN filter. Never on by default; see the module docstring.

    Reads the badges rather than the reachability, because the toggles are
    about the POSTING's stated requirement, not about our verdict on the
    reader: "hide jobs that say they do not sponsor" must do exactly that for
    an `unknown` user too, if they asked for it.
    """
    keys = set(assessment.get("badge_keys") or ())
    if hide_sponsorship_restricted and BADGE_NO_SPONSORSHIP in keys:
        return True
    if hide_clearance_required and BADGE_CLEARANCE_REQUIRED in keys:
        return True
    return False
