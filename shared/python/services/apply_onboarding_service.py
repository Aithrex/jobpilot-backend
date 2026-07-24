"""Auto-apply onboarding — the ask-once declared-profile surface (QT2).

The auto-apply fill engine (docs/specs/auto-apply-fill-engine.md "## Onboarding
profile") needs a small set of DECLARED facts before it can fill a form correctly
and safely: contact identity, work-authorization (factual, never guessed), and —
optionally — EEO/legal answers the user chooses to state. This service turns the
CANONICAL question bank (``application_question_bank``) into a grouped onboarding
questionnaire and persists the user's answers to the SAME declared store the
answer library resolves from, so a value the user states here is immediately
fillable by the executor — no second copy of the catalog, no drift.

Design posture (matches answer_library_service / autonomy_service):
  * The catalog is the SINGLE source of the fields, their sensitivity, options,
    and policy — this module never re-declares a field. It only decides the
    "most-predicted default" (decline for protected/legal, a common value for
    logistics, resume-derived for identity) and reads the user's current value
    back THROUGH the resolver, so onboarding's ``current_value`` / ``ready`` gate
    can never diverge from what the executor will actually fill.
  * The declared store is ``user_preferences`` (flat catalog-field-id keys) — the
    exact place ``answer_library_service._load_canonical`` / ``_load_legal`` read
    from. A declared "decline" IS an explicit, honored value (never inferred).
  * NEVER-FABRICATE: protected (EEO) / legal fields are only ever DECLARED by the
    user; nothing here infers them. Their default is the form's decline option (a
    real, explicit "decline"), which the user may edit or leave.

Nothing here calls an LLM or spends entitlement — onboarding is a profile edit,
not a generation. The route enforces ``@jwt_required``; ``user_email`` is the
trusted JWT identity and is never placed in a Mongo operator position, and every
``field_id`` is validated against the CLOSED catalog before any write.
"""
from __future__ import annotations

import logging
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from utils.db_connect import DBConnect
from utils.security import InputSanitizer
from services import application_question_bank as qb
from services import answer_library_service as answer_lib
from services import autonomy_service

logger = logging.getLogger(__name__)


class OnboardingError(Exception):
    """Typed business error → the route maps it to its HTTP status (400 — NEVER
    401; a bad field_id / value / mode is a business failure, not a missing JWT)."""

    def __init__(self, error: str, message: str, status: int = 400):
        super().__init__(message)
        self.error = error
        self.message = message
        self.status = status


# Free-text essays (cover_letter / why_company / …) are GENERATED per-JD by the
# grounded answer generator, never declared once — so they are not onboarding
# questions. Everything else in the catalog is.
_EXCLUDED_CATEGORIES = frozenset({qb.CAT_FREE_TEXT})

# Category display order for the grouped payload (identity first, then the
# factual/sensitive/logistics groups the onboarding flow walks through).
_CATEGORY_ORDER = [
    qb.CAT_IDENTITY, qb.CAT_WORK_AUTH, qb.CAT_EEO,
    qb.CAT_LOGISTICS, qb.CAT_SCREENING, qb.CAT_LEGAL,
]

# The READY gate (spec: "Required set (contact + work_auth + sponsorship) blocks
# auto-apply until filled"). All must have a declared/derived value before the
# pipeline may auto-act. EEO/legal are deliberately NOT here — they are voluntary
# and default to decline.
READY_REQUIRED_FIELDS = (
    "full_name", "email", "phone",
    "work_authorized_us", "requires_sponsorship",
)

# Sensible one-tap-confirm defaults for a few logistics fields (the user just
# accepts). Everything else defaults to None (no guessed value).
_COMMON_DEFAULTS: Dict[str, Any] = {
    "how_did_you_hear": "Company website",
}

# Accepted truthy/falsy spellings for a Yes/No field submitted as a string.
_TRUE_STRINGS = frozenset({"yes", "y", "true", "t", "1"})
_FALSE_STRINGS = frozenset({"no", "n", "false", "f", "0"})


def _prefs():
    return DBConnect().get_db().user_preferences


def _onboarding_fields() -> List[Dict[str, Any]]:
    """The catalog fields that belong in onboarding (everything but free-text)."""
    return [f for f in qb.FIELDS if f["category"] not in _EXCLUDED_CATEGORIES]


def _is_required(field: Dict[str, Any]) -> bool:
    """Per-field `required` flag = the catalog's ``required_onboarding`` policy
    (the factual work-auth set the form filler cannot proceed without)."""
    return field["default_policy"] == qb.POLICY_REQUIRED_ONBOARDING


def _decline_option(field: Dict[str, Any]) -> Optional[str]:
    """The form's DECLINE option for a select, or None when it has none."""
    opts = field.get("options") or []
    return qb.DECLINE if qb.DECLINE in opts else None


def _default_for(field: Dict[str, Any]) -> Any:
    """The safe / most-predicted default the FE pre-fills (the user confirms/edits):

      * protected (EEO) + decline_default  -> the form's DECLINE option (a real,
        explicit "decline"; NEVER an inferred demographic value);
      * confirm_default logistics          -> a common value (e.g. "Company
        website") where we have one, else None;
      * required_onboarding / never_default / from_profile -> None (no guess — the
        value must be DECLARED, or is resume-derived and carried by current_value).
    """
    if field["sensitivity"] == qb.SENS_PROTECTED:
        return _decline_option(field)
    policy = field["default_policy"]
    if policy == qb.POLICY_DECLINE_DEFAULT:
        return _decline_option(field)
    if policy == qb.POLICY_CONFIRM_DEFAULT:
        return _COMMON_DEFAULTS.get(field["field_id"])
    return None


def _declared_view(user_email: str) -> Dict[str, Any]:
    """Everything the answer library sees as DECLARED for this user, keyed by
    catalog field_id.

    Primary source is the RESOLVER itself: we hand every onboarding field to
    ``answer_library_service.resolve`` and read the resolved value back, so
    ``current_value`` / the ready gate use the exact same declared surface the
    executor fills from (identity from the résumé, work-auth/EEO from declared
    prefs, etc.). We then overlay any onboarding-written flat pref so a field the
    resolver does not map still reflects a just-saved value.
    """
    view: Dict[str, Any] = {}

    questions = [
        {"field_id": f["field_id"], "label": f["label"],
         "required": _is_required(f)}
        for f in _onboarding_fields()
    ]
    try:
        res = answer_lib.resolve(user_email, questions)
        for fid, meta in (res.get("resolved") or {}).items():
            val = meta.get("value") if isinstance(meta, dict) else None
            if val not in (None, ""):
                view[fid] = val
    except Exception as e:  # pragma: no cover - defensive; a resolver hiccup
        logger.warning("onboarding: resolve view failed for %s: %s", user_email, e)

    # Overlay explicit onboarding writes (flat field_id keys on user_preferences)
    # so a just-declared field the resolver does not map still shows as current.
    try:
        prefs = _prefs().find_one({"user_email": user_email}) or {}
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("onboarding: prefs load failed for %s: %s", user_email, e)
        prefs = {}
    for f in _onboarding_fields():
        fid = f["field_id"]
        pv = prefs.get(fid)
        if pv not in (None, ""):
            view[fid] = pv

    return view


def _ready_for_autoapply(view: Dict[str, Any]) -> bool:
    """True once every field in the READY set has a declared/derived value."""
    return all(view.get(fid) not in (None, "") for fid in READY_REQUIRED_FIELDS)


def build_questions_payload(user_email: str) -> Dict[str, Any]:
    """The grouped onboarding questionnaire + readiness/mode.

    Shape:
        {
          "groups": [ {category, fields: [ {field_id, label, category,
              sensitivity, value_type, options, required, default, current_value,
              missing}, ... ]}, ... ],
          "ready_for_autoapply": bool,     # all READY_REQUIRED_FIELDS present
          "mode": "copilot"|"approval"|"autopilot"|null,   # null until picked
          "required_fields": [...],        # the READY set, for the FE
        }
    """
    view = _declared_view(user_email)

    groups_map: "OrderedDict[str, List[Dict[str, Any]]]" = OrderedDict()
    for f in _onboarding_fields():
        fid = f["field_id"]
        current = view.get(fid)
        current = current if current not in (None, "") else None
        required = _is_required(f)
        entry = {
            "field_id": fid,
            "label": f["label"],
            "category": f["category"],
            "sensitivity": f["sensitivity"],
            "value_type": f["value_type"],
            "options": list(f.get("options") or []),
            "required": required,
            "default": _default_for(f),
            "current_value": current,
            "missing": bool(required and current is None),
        }
        groups_map.setdefault(f["category"], []).append(entry)

    groups = [
        {"category": cat, "fields": groups_map[cat]}
        for cat in _CATEGORY_ORDER if cat in groups_map
    ]

    return {
        "groups": groups,
        "ready_for_autoapply": _ready_for_autoapply(view),
        "mode": autonomy_service.get_mode(user_email),
        "required_fields": list(READY_REQUIRED_FIELDS),
    }


def _to_bool(value: Any) -> Optional[bool]:
    """Coerce a Yes/No answer to a real bool, or None when it is not clearly
    boolean (then the value is rejected — never guessed)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value in (0, 1):
            return bool(value)
        return None
    if isinstance(value, str):
        v = value.strip().lower()
        if v in _TRUE_STRINGS:
            return True
        if v in _FALSE_STRINGS:
            return False
    return None


def _coerce_and_validate(field: Dict[str, Any], value: Any) -> Any:
    """Validate a submitted value against the field's type/options and return the
    stored form. Raises OnboardingError (400) on anything the field can't take."""
    fid = field["field_id"]
    if value is None:
        raise OnboardingError("invalid_value", f"{fid}: a value is required", 400)

    vt = field["value_type"]
    opts = field.get("options") or []

    if vt == qb.VT_BOOLEAN:
        b = _to_bool(value)
        if b is None:
            raise OnboardingError(
                "invalid_value", f"{fid}: expected Yes/No, got {value!r}", 400)
        return b

    if vt == qb.VT_SELECT:
        # bool is a subclass of int/str-free — a select must be one of its options.
        sval = InputSanitizer.sanitize_string(str(value), max_length=200)
        if opts and sval not in opts:
            raise OnboardingError(
                "invalid_value",
                f"{fid}: {sval!r} is not one of {opts}", 400)
        return sval

    if vt in (qb.VT_NUMBER, qb.VT_YEARS):
        if isinstance(value, bool):
            raise OnboardingError("invalid_value", f"{fid}: expected a number", 400)
        if isinstance(value, (int, float)):
            return value
        try:
            sval = str(value).strip()
            return float(sval) if ("." in sval) else int(sval)
        except (TypeError, ValueError):
            raise OnboardingError("invalid_value", f"{fid}: expected a number", 400)

    # text / email / phone / url / money / date -> sanitized string.
    if isinstance(value, bool):
        raise OnboardingError("invalid_value", f"{fid}: expected text", 400)
    sval = InputSanitizer.sanitize_string(str(value), max_length=2000)
    if not sval:
        raise OnboardingError("invalid_value", f"{fid}: a value is required", 400)
    return sval


def save_answers(user_email: str, answers: Dict[str, Any],
                 mode: Optional[str] = None) -> Dict[str, Any]:
    """Persist onboarding answers to the declared store and return the refreshed
    questionnaire.

      * Each ``field_id`` MUST be in the closed catalog (and not a free-text
        essay) — an unknown id is a 400 ``unknown_field``.
      * Each value is validated against its field's type/options (a bad value is a
        400 ``invalid_value``) BEFORE any write, so a partial payload never
        half-persists an invalid field.
      * Validated values are written as flat ``user_preferences[field_id]`` keys —
        the exact surface ``answer_library_service`` resolves from, so a declared
        value (incl. an EEO "decline") is immediately fillable by the executor.
      * ``mode`` (optional) is the autonomy pick; validated + set via
        ``autonomy_service.set_mode`` (a bad mode is a 400 ``invalid_mode``).
    """
    if not user_email:
        raise OnboardingError("invalid_request", "authentication required", 400)
    if not isinstance(answers, dict):
        raise OnboardingError(
            "invalid_answers", "answers must be an object of {field_id: value}", 400)

    # Validate the mode (if any) up front so a bad mode never half-applies with a
    # partial answer write.
    if mode is not None and mode not in autonomy_service.VALID_MODES:
        raise OnboardingError(
            "invalid_mode",
            f"mode must be one of {list(autonomy_service.VALID_MODES)}", 400)

    # Validate + coerce EVERY answer before persisting anything (all-or-nothing).
    validated: Dict[str, Any] = {}
    for raw_fid, raw_val in answers.items():
        fid = InputSanitizer.sanitize_string(str(raw_fid or ""), max_length=200)
        field = qb.get_field(fid)
        if field is None or field["category"] in _EXCLUDED_CATEGORIES:
            raise OnboardingError(
                "unknown_field", f"Unknown onboarding field_id: {fid}", 400)
        validated[fid] = _coerce_and_validate(field, raw_val)

    if validated:
        now = datetime.now(timezone.utc)
        # field_ids are catalog-whitelisted ([a-z0-9_]) — safe as $set keys; the
        # user_email filter is the trusted JWT identity (never an operator).
        set_doc: Dict[str, Any] = dict(validated)
        set_doc["user_email"] = user_email
        set_doc["onboarding_updated_at"] = now
        try:
            _prefs().update_one(
                {"user_email": user_email},
                {"$set": set_doc},
                upsert=True,
            )
        except Exception as e:
            logger.exception("onboarding: persist failed for %s: %s", user_email, e)
            raise OnboardingError(
                "persist_failed", "Failed to save onboarding answers", 500)

    if mode is not None:
        autonomy_service.set_mode(user_email, mode)

    return build_questions_payload(user_email)
