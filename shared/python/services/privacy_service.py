"""Privacy rights and email consent for the US launch (CCPA/CPRA, CAN-SPAM).

One module owns the account holder's privacy surface so the auth blueprint can
stay thin:

  * ``get_privacy`` / ``update_privacy``: the ``/api/auth/privacy`` shape.
    ``email`` is read from and written to the EXISTING notification store
    (``notifications.service``), never a second copy.
  * Global Privacy Control: ``gpc_signal`` + ``record_gpc``. Aspirely does not
    sell or share personal information, so ``sale_share_opt_out`` is always
    true and the signal is recorded as honored.
  * Sensitive data: immigration status is processed only with consent.
    Withdrawing it erases every stored visa and immigration field
    (``erase_immigration_data``); matching then sees ``unknown`` and the
    sponsorship rules simply do not apply.
  * Clickwrap: ``record_registration`` stores ``terms`` and ``age_confirmed``.
  * Unsubscribe tokens (RFC 8058 one-click): an HMAC over (email, list) with a
    purpose-scoped key derived from ``JWT_SECRET_KEY`` (never rotated, never
    used directly). The token carries the account id, not the email, so no
    address appears in a URL, and it has no expiry.
  * A hashed suppression list that survives account erasure, so a deleted
    user is never emailed again.
  * ``export_stream``: the right to know / portability, streamed so memory
    stays bounded.

Import-safe: no I/O at import time.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import math
import re
from datetime import date, datetime, timezone
from typing import Any, Dict, Iterator, List, Optional, Tuple

from utils.db_connect import DBConnect
from utils.security import InputSanitizer

logger = logging.getLogger(__name__)

PRIVACY_VERSION = "privacy-1.0.0"

EXPORT_FORMAT = "aspirely-data-export"
EXPORT_FORMAT_VERSION = 1
EXPORT_DAILY_LIMIT = 5
EXPORT_WINDOW_SECONDS = 24 * 3600
EXPORT_MAX_DOCS_PER_COLLECTION = 5000

SUPPRESSION_COLLECTION = "email_suppressions"
REASON_ACCOUNT_DELETED = "account_deleted"

_UNSUB_PURPOSE = "email-unsubscribe:v1"
_HASH_PURPOSE = "email-suppression-hash:v1"
_TERMS_VERSION_RE = re.compile(r"^[A-Za-z0-9._:\-]{1,40}$")
_UID_RE = re.compile(r"^[0-9a-f]{24}$")
_MAC_RE = re.compile(r"^[A-Za-z0-9_\-]{43}$")

LIST_LABELS = {
    "job_alerts": "job alerts",
    "digests": "digests",
    "product_updates": "product updates",
    "marketing": "marketing email",
}


class PrivacyError(Exception):
    """A request the caller can fix. ``status`` is 400 or 403, never 401."""

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _db():
    return DBConnect().get_db()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: Any) -> Optional[str]:
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return None


def _safe_email(email: Any) -> str:
    return InputSanitizer.sanitize_email(str(email or "")) or ""


def _notify():
    from services.notifications import service as notify
    return notify


def email_lists() -> Tuple[str, ...]:
    return tuple(_notify().EMAIL_LISTS)


def _derived_key(purpose: str) -> bytes:
    """A purpose-scoped key derived from the pinned JWT secret. The JWT secret
    itself is never used as a MAC key, so a token minted here can never be
    replayed as a JWT signature or vice versa."""
    from utils.config import AppConfig
    secret = str(AppConfig.JWT_SECRET_KEY or "").encode("utf-8")
    return hmac.new(secret, f"aspirely:{purpose}".encode("utf-8"), hashlib.sha256).digest()


def mask_email(email: str) -> str:
    """``jane.doe@gmail.com`` -> ``j***@gmail.com``."""
    if not email or "@" not in email:
        return "***"
    local, _, domain = email.partition("@")
    return f"{local[:1]}***@{domain}"


# ---------------------------------------------------------------------------
# Unsubscribe tokens
# ---------------------------------------------------------------------------
def _mac(uid: str, email: str, list_id: str) -> str:
    msg = f"v1\x00{uid}\x00{email}\x00{list_id}".encode("utf-8")
    digest = hmac.new(_derived_key(_UNSUB_PURPOSE), msg, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def make_unsubscribe_token(email: str, list_id: str) -> Optional[str]:
    """``<account id>.<list>.<mac>``, or None when there is no such account."""
    addr = _safe_email(email)
    if not addr or list_id not in email_lists():
        return None
    try:
        user = _db().users.find_one({"email": addr}, {"_id": 1})
    except Exception as exc:
        logger.warning("privacy: token mint lookup failed (%s)", type(exc).__name__)
        return None
    if not user:
        return None
    uid = str(user["_id"])
    if not _UID_RE.match(uid):
        return None
    return f"{uid}.{list_id}.{_mac(uid, addr, list_id)}"


def verify_unsubscribe_token(token: Any) -> Optional[Tuple[str, str]]:
    """``(email, list)`` for a genuine token, else None. Constant-time MAC
    compare; nothing from the token reaches a Mongo filter unvalidated."""
    if not isinstance(token, str) or not token or len(token) > 200:
        return None
    parts = token.strip().split(".")
    if len(parts) != 3:
        return None
    uid, list_id, mac = parts
    if not _UID_RE.match(uid) or list_id not in email_lists() or not _MAC_RE.match(mac):
        return None
    try:
        from bson import ObjectId
        user = _db().users.find_one({"_id": ObjectId(uid)}, {"email": 1})
    except Exception as exc:
        logger.warning("privacy: token verify lookup failed (%s)", type(exc).__name__)
        return None
    addr = _safe_email((user or {}).get("email"))
    if not addr:
        return None
    if not hmac.compare_digest(_mac(uid, addr, list_id), mac):
        return None
    return addr, list_id


def _site_base() -> str:
    try:
        from utils.config import AppUrlsConfig
        return AppUrlsConfig.FRONTEND_URL.rstrip("/")
    except Exception:  # pragma: no cover - defensive
        return "https://aspirely.me"


def one_click_url(token: str) -> str:
    """The RFC 8058 POST target (the API, proxied on the site origin)."""
    return f"{_site_base()}/api/auth/email/unsubscribe?t={token}"


def unsubscribe_page_url(token: str) -> str:
    """The frontend confirmation page linked from the email footer."""
    return f"{_site_base()}/unsubscribe?t={token}"


def unsubscribe(token: Any) -> Optional[Dict[str, Any]]:
    """Apply a one-click unsubscribe. Idempotent. None for an invalid token."""
    verified = verify_unsubscribe_token(token)
    if not verified:
        return None
    email, list_id = verified
    _notify().unsubscribe_list(email, list_id)
    logger.info("privacy: unsubscribed %s from %s", mask_email(email), list_id)
    return {"unsubscribed": True, "list": list_id}


def describe_token(token: Any) -> Optional[Dict[str, Any]]:
    """What a token points at, for the confirmation page. Changes nothing."""
    verified = verify_unsubscribe_token(token)
    if not verified:
        return None
    email, list_id = verified
    return {"list": list_id, "email_masked": mask_email(email)}


# ---------------------------------------------------------------------------
# Hashed suppression (survives erasure)
# ---------------------------------------------------------------------------
def email_hash(email: str) -> str:
    addr = _safe_email(email) or str(email or "").strip().lower()
    return hmac.new(_derived_key(_HASH_PURPOSE), addr.encode("utf-8"), hashlib.sha256).hexdigest()


def is_hash_suppressed(email: str) -> bool:
    """True when the address is on the hashed suppression list. Never raises."""
    if not email:
        return False
    try:
        return _db()[SUPPRESSION_COLLECTION].find_one(
            {"email_hash": email_hash(email)}, {"_id": 1}) is not None
    except Exception as exc:
        logger.warning("privacy: hashed suppression lookup failed (%s)", type(exc).__name__)
        return False


def suppress_hash(email: str, reason: str, detail: str = "") -> None:
    """Upsert one hashed row. No raw address is stored."""
    h = email_hash(email)
    now = _now()
    _db()[SUPPRESSION_COLLECTION].update_one(
        {"email_hash": h, "reason": str(reason)[:32]},
        {"$set": {"email_hash": h, "reason": str(reason)[:32],
                  "detail": InputSanitizer.sanitize_string(str(detail or ""), 120),
                  "hashed_at": now},
         "$setOnInsert": {"created_at": now}},
        upsert=True,
    )


def hash_existing_suppressions(email: str) -> int:
    """Convert raw-address suppression rows (bounce / complaint) for ``email``
    into hashed rows, so they outlive the account without keeping the address."""
    addr = _safe_email(email)
    if not addr:
        return 0
    h = email_hash(addr)
    res = _db()[SUPPRESSION_COLLECTION].update_many(
        {"email": addr},
        {"$set": {"email_hash": h, "hashed_at": _now()}, "$unset": {"email": ""}},
    )
    return int(res.modified_count or 0)


def clear_account_deleted_marker(email: str) -> None:
    """A NEW account on a previously erased address is a new relationship.
    Only the erasure marker goes; bounce and complaint rows stay."""
    try:
        _db()[SUPPRESSION_COLLECTION].delete_many(
            {"email_hash": email_hash(email), "reason": REASON_ACCOUNT_DELETED})
    except Exception as exc:
        logger.warning("privacy: clear erasure marker failed (%s)", type(exc).__name__)


def on_account_created(email: str) -> None:
    """Hook for every signup path (password and social)."""
    clear_account_deleted_marker(email)


# ---------------------------------------------------------------------------
# Global Privacy Control
# ---------------------------------------------------------------------------
def gpc_signal(headers: Any) -> bool:
    """True when the request carries ``Sec-GPC: 1`` (GPC spec section 3)."""
    try:
        return str(headers.get("Sec-GPC", "") or "").strip() == "1"
    except Exception:
        return False


GPC_REFRESH_SECONDS = 3600


def record_gpc(email: str) -> None:
    """Record that a GPC opt-out signal was seen. Best-effort, never raises.

    `/verify` runs on every app load, so ``last_seen_at`` is refreshed at most
    hourly: the filter matches nothing (no write) inside that hour."""
    addr = _safe_email(email)
    if not addr:
        return
    from datetime import timedelta
    now = _now()
    try:
        _db().users.update_one(
            {"email": addr, "$or": [
                {"privacy.gpc.detected": {"$ne": True}},
                {"privacy.gpc.last_seen_at": {"$lt": now - timedelta(seconds=GPC_REFRESH_SECONDS)}},
            ]},
            {"$set": {"privacy.gpc.detected": True, "privacy.gpc.last_seen_at": now}},
        )
    except Exception as exc:
        logger.warning("privacy: GPC record failed (%s)", type(exc).__name__)


# ---------------------------------------------------------------------------
# Clickwrap
# ---------------------------------------------------------------------------
def minimum_age() -> int:
    from utils.config import ComplianceConfig
    return ComplianceConfig.MINIMUM_AGE


def age_requirement_message() -> str:
    return f"You must be at least {minimum_age()} years old to use Aspirely."


def clean_terms_version(value: Any) -> Optional[str]:
    """A terms version string, or None when absent. Raises PrivacyError when
    present but malformed."""
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not _TERMS_VERSION_RE.match(value.strip()):
        raise PrivacyError("invalid_request", "terms_version must be a short version string.")
    return value.strip()


def _terms_update(version: str, now: datetime) -> Dict[str, Any]:
    return {
        "$set": {"terms": {"version": version, "accepted_at": now}},
        "$push": {"terms_history": {"$each": [{"version": version, "accepted_at": now}],
                                    "$slice": -20}},
    }


def record_registration(email: str, *, age_confirmed: Optional[bool],
                        terms_version: Optional[str]) -> None:
    """Store the clickwrap facts on the new users doc. Both are optional so
    OAuth signups and older clients keep working."""
    addr = _safe_email(email)
    if not addr:
        return
    now = _now()
    update: Dict[str, Any] = {"$set": {}}
    if age_confirmed is True:
        update["$set"].update({"age_confirmed": True, "age_confirmed_at": now,
                               "age_minimum": minimum_age()})
    if terms_version:
        tu = _terms_update(terms_version, now)
        update["$set"].update(tu["$set"])
        update["$push"] = tu["$push"]
    if not update["$set"]:
        return
    _db().users.update_one({"email": addr}, update)


# ---------------------------------------------------------------------------
# Sensitive data: immigration status
# ---------------------------------------------------------------------------
#: Flat `user_preferences` keys that hold immigration status. The first seven
#: are every `application_question_bank` field in the work-authorization
#: category; the rest are legacy and feed-side spellings.
IMMIGRATION_PREF_FIELDS: Tuple[str, ...] = (
    "requires_sponsorship", "work_authorized_us", "visa_status", "work_auth_country",
    "employer_immigration_filing", "export_control_us_person", "citizenship_countries",
    "citizenship", "needs_sponsorship", "work_authorization", "sponsorship_needed",
)
#: `application_answer_library.category` values that are immigration answers.
IMMIGRATION_ANSWER_CATEGORIES: Tuple[str, ...] = ("work_authorization", "sponsorship", "visa_status")


def _immigration_field_ids() -> Tuple[str, ...]:
    """The static list plus whatever the question bank declares today, so a
    new work-authorization field is erased without a code change here."""
    ids = list(IMMIGRATION_PREF_FIELDS)
    try:
        from services import application_question_bank as qb
        for f in qb.FIELDS:
            if f.get("category") == qb.CAT_WORK_AUTH and f["field_id"] not in ids:
                ids.append(f["field_id"])
    except Exception as exc:  # pragma: no cover - parallel build safety
        logger.warning("privacy: question bank unavailable (%s)", type(exc).__name__)
    return tuple(ids)


def erase_immigration_data(email: str) -> Dict[str, int]:
    """Erase every stored visa and immigration field for ``email``.

    Stores touched (each fail-soft, counts returned):
      * ``visa_profiles``: deleted outright. The collection exists only for
        immigration state (status, OPT dates, country of birth, declared work
        authorization and the two sponsorship feed filters).
      * ``user_preferences``: the flat onboarding keys, plus the nested
        ``work_auth`` map and the matching keys inside ``legal`` / ``attestations``.
      * ``application_answer_library``: remembered sponsorship, work
        authorization and visa answers.
      * ``application_profiles.vault``: the extension vault's copies.
      * ``auto_apply_answers.answers``: per-application resolved answers.

    Afterwards `work_authorization.get_profile` reads ``unknown`` for this
    user, so search and the inventory feed stop applying sponsorship rules.
    """
    addr = _safe_email(email)
    out: Dict[str, int] = {}
    if not addr:
        return out
    db = _db()
    fids = _immigration_field_ids()

    def _run(name: str, fn) -> None:
        try:
            out[name] = int(fn() or 0)
        except Exception as exc:
            logger.warning("privacy: immigration erase %s failed (%s)", name, type(exc).__name__)
            out[name] = -1

    _run("visa_profiles", lambda: db.visa_profiles.delete_many({"user_email": addr}).deleted_count)

    pref_unset: Dict[str, str] = {f: "" for f in fids}
    pref_unset["work_auth"] = ""
    for nested in ("legal", "attestations"):
        for f in fids:
            pref_unset[f"{nested}.{f}"] = ""
    _run("user_preferences", lambda: db.user_preferences.update_many(
        {"user_email": addr}, {"$unset": pref_unset}).modified_count)

    _run("application_answer_library", lambda: db.application_answer_library.delete_many({
        "user_email": addr,
        "$or": [
            {"category": {"$in": list(IMMIGRATION_ANSWER_CATEGORIES)}},
            {"field_id": {"$in": list(fids)}},
            {"field": {"$in": list(fids)}},
        ],
    }).deleted_count)

    _run("application_profiles", lambda: db.application_profiles.update_many(
        {"user_email": addr}, {"$unset": {f"vault.{f}": "" for f in fids}}).modified_count)

    _run("auto_apply_answers", lambda: db.auto_apply_answers.update_many(
        {"user_email": addr}, {"$unset": {f"answers.{f}": "" for f in fids}}).modified_count)

    logger.info("privacy: immigration data erased for %s: %s", mask_email(addr), out)
    return out


def set_immigration_consent(email: str, consented: bool) -> Dict[str, Any]:
    addr = _safe_email(email)
    now = _now()
    users = _db().users
    if consented:
        current = (users.find_one({"email": addr}, {"privacy.sensitive.immigration_status": 1})
                   or {}).get("privacy", {}).get("sensitive", {}).get("immigration_status") or {}
        if current.get("consented") and current.get("consented_at"):
            return {"erased": None}
        users.update_one({"email": addr}, {"$set": {
            "privacy.sensitive.immigration_status": {
                "consented": True, "consented_at": now, "withdrawn_at": None,
                "version": PRIVACY_VERSION,
            }}})
        return {"erased": None}
    erased = erase_immigration_data(addr)
    users.update_one({"email": addr}, {"$set": {
        "privacy.sensitive.immigration_status": {
            "consented": False, "consented_at": None, "withdrawn_at": now,
            "version": PRIVACY_VERSION,
        }}})
    return {"erased": erased}


# ---------------------------------------------------------------------------
# The /api/auth/privacy shape
# ---------------------------------------------------------------------------
def get_privacy(email: str) -> Dict[str, Any]:
    addr = _safe_email(email)
    user = {}
    try:
        user = _db().users.find_one(
            {"email": addr},
            {"_id": 0, "privacy": 1, "terms": 1, "age_confirmed": 1}) or {}
    except Exception as exc:
        logger.warning("privacy: users read failed (%s)", type(exc).__name__)
    privacy = user.get("privacy") if isinstance(user.get("privacy"), dict) else {}
    gpc = privacy.get("gpc") if isinstance(privacy.get("gpc"), dict) else {}
    imm = ((privacy.get("sensitive") or {}).get("immigration_status")
           if isinstance(privacy.get("sensitive"), dict) else None) or {}
    terms = user.get("terms") if isinstance(user.get("terms"), dict) else {}
    try:
        lists = _notify().email_list_state(addr)
    except Exception as exc:
        logger.warning("privacy: email list read failed (%s)", type(exc).__name__)
        lists = dict(_notify().LIST_DEFAULTS)
    return {
        # Aspirely does not sell or share personal information (CCPA 1798.120,
        # 1798.140(ad) and (ah)), so every account is opted out by default.
        "sale_share_opt_out": True,
        "gpc": {
            "detected": bool(gpc.get("detected")),
            "last_seen_at": _iso(gpc.get("last_seen_at")),
        },
        "sensitive": {
            "immigration_status": {
                "consented": bool(imm.get("consented")),
                "consented_at": _iso(imm.get("consented_at")),
            },
        },
        "email": {name: bool(lists.get(name)) for name in email_lists()},
        "terms": {
            "version": terms.get("version"),
            "accepted_at": _iso(terms.get("accepted_at")),
        },
        "age_confirmed": bool(user.get("age_confirmed")),
        "minimum_age": minimum_age(),
    }


def update_privacy(email: str, body: Any) -> Dict[str, Any]:
    """Apply a partial PUT. Validates everything before writing anything."""
    addr = _safe_email(email)
    if not addr:
        raise PrivacyError("invalid_request", "Account not found.")
    if not isinstance(body, dict):
        raise PrivacyError("invalid_request", "Send a JSON object.")

    consent = body.get("immigration_status_consent", None)
    if "immigration_status_consent" in body and not isinstance(consent, bool):
        raise PrivacyError("invalid_request", "immigration_status_consent must be true or false.")

    email_changes: Dict[str, bool] = {}
    if "email" in body:
        raw = body.get("email")
        if not isinstance(raw, dict):
            raise PrivacyError("invalid_request", "email must be an object of list names to true or false.")
        for key, val in raw.items():
            if key not in email_lists():
                raise PrivacyError("invalid_request", f"Unknown email list: {str(key)[:40]}.")
            if not isinstance(val, bool):
                raise PrivacyError("invalid_request", f"email.{key} must be true or false.")
            email_changes[key] = val

    terms_version = None
    if "accept_terms_version" in body:
        terms_version = clean_terms_version(body.get("accept_terms_version"))
        if not terms_version:
            raise PrivacyError("invalid_request", "accept_terms_version must be a short version string.")

    age = body.get("age_confirmed", None)
    if "age_confirmed" in body:
        if not isinstance(age, bool):
            raise PrivacyError("invalid_request", "age_confirmed must be true or false.")
        if age is False:
            raise PrivacyError("age_requirement", age_requirement_message())

    users = _db().users
    if not users.find_one({"email": addr}, {"_id": 1}):
        raise PrivacyError("invalid_request", "Account not found.")

    if isinstance(consent, bool):
        set_immigration_consent(addr, consent)
    if email_changes:
        _notify().set_email_lists(addr, email_changes)
    now = _now()
    if terms_version:
        users.update_one({"email": addr}, _terms_update(terms_version, now))
    if age is True:
        users.update_one({"email": addr}, {"$set": {
            "age_confirmed": True, "age_confirmed_at": now, "age_minimum": minimum_age()}})
    return get_privacy(addr)


# ---------------------------------------------------------------------------
# Data export (right to know / portability)
# ---------------------------------------------------------------------------
#: Collections that hold only credentials or one-time codes. They are named
#: in the export so the user knows they exist, but their contents are withheld.
WITHHELD_COLLECTIONS: Dict[str, str] = {
    "password_reset_tokens": "Password reset links",
    "approval_tokens": "Single-use approval links sent by email",
    "gmail_oauth_states": "Short-lived sign-in state for connecting Gmail",
    "connect_oauth_states": "Short-lived sign-in state for connecting GitHub or LinkedIn",
    "credential_reveal_challenges": "Verification codes for revealing a saved employer login",
    "mail_commit_tokens": "Internal upload tokens for your application inbox",
    "mail_forward_verifications": "Verification links for mail forwarding",
    "application_otp_grants": "One-time codes read from employer verification emails",
}

#: Field names never exported, at any depth: password hashes, OAuth and API
#: tokens, encryption material, one-time codes, device fingerprints.
_SECRET_KEY_RE = re.compile(
    r"(password|passwd|secret|token|cipher|encrypted|private_key|api_?key|apify|"
    r"nonce|fingerprint|fernet|signature)"
    r"|(^|_)(otp|kms|salt)($|_)"
    r"|(_enc|_hash|_blob|_iv)$"
    r"|^(enc_.*|hash|iv|dek|code|key_material|wrapped_key)$",
    re.IGNORECASE,
)


def is_secret_field(name: str) -> bool:
    return bool(_SECRET_KEY_RE.search(str(name or "")))


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items() if not is_secret_field(str(k))}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if isinstance(value, datetime):
        return _iso(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, bool) or value is None or isinstance(value, (str, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (bytes, bytearray)):
        return "<binary omitted>"
    return str(value)  # ObjectId, Decimal128, UUID, Regex, ...


def export_filename(now: Optional[datetime] = None) -> str:
    return f"aspirely-data-export-{(now or _now()).strftime('%Y-%m-%d')}.json"


def _export_sources(email: str, user_id: Optional[str]) -> List[Tuple[str, Dict[str, Any]]]:
    """Every (collection, filter) holding this user's rows. Built from the
    purge cascade so the two lists cannot drift apart."""
    from services import account_service
    sources: List[Tuple[str, Dict[str, Any]]] = [("users", {"email": email})]
    seen = {"users"}
    for coll, key in account_service.CASCADE_COLLECTIONS:
        if coll in seen or coll in WITHHELD_COLLECTIONS:
            continue
        seen.add(coll)
        if coll == "resume_assets" and user_id:
            sources.append((coll, {"$or": [{"user_email": email}, {"user_id": user_id}]}))
        else:
            sources.append((coll, {key: email}))
    for coll, flt in account_service.export_only_sources(email):
        if coll not in seen:
            seen.add(coll)
            sources.append((coll, flt))
    return sources


def export_stream(email: str) -> Iterator[str]:
    """Yield the export as JSON text in chunks. One document is held in memory
    at a time; each collection is capped at EXPORT_MAX_DOCS_PER_COLLECTION and
    a cap hit is reported in ``summary.truncated``."""
    addr = _safe_email(email)
    db = _db()
    now = _now()
    user_id = None
    try:
        u = db.users.find_one({"email": addr}, {"_id": 1})
        user_id = str(u["_id"]) if u else None
    except Exception as exc:
        logger.warning("privacy: export user lookup failed (%s)", type(exc).__name__)

    counts: Dict[str, int] = {}
    truncated: List[str] = []
    failed: List[str] = []

    yield '{"format":%s,"version":%d,"generated_at":%s,"account":%s,"collections":{' % (
        json.dumps(EXPORT_FORMAT), EXPORT_FORMAT_VERSION, json.dumps(_iso(now)),
        json.dumps({"email": addr}))
    first_coll = True
    for coll, flt in (_export_sources(addr, user_id) if addr else []):
        n = 0
        opened = False
        try:
            cursor = db[coll].find(flt).limit(EXPORT_MAX_DOCS_PER_COLLECTION + 1)
            for doc in cursor:
                if n >= EXPORT_MAX_DOCS_PER_COLLECTION:
                    truncated.append(coll)
                    break
                if not opened:
                    yield ("" if first_coll else ",") + json.dumps(coll) + ":["
                    first_coll = False
                    opened = True
                yield ("," if n else "") + json.dumps(_jsonable(doc), separators=(",", ":"))
                n += 1
        except Exception as exc:
            failed.append(coll)
            logger.warning("privacy: export read %s failed (%s)", coll, type(exc).__name__)
        if opened:
            yield "]"
        counts[coll] = n

    withheld = [{"collection": c, "description": d} for c, d in WITHHELD_COLLECTIONS.items()]
    summary = {
        "counts": counts,
        "searched": sorted(counts),
        "truncated": truncated,
        "unavailable": failed,
        "notes": [
            "Passwords, sign-in tokens, encryption keys and one-time codes are never included.",
            "Uploaded files such as resume PDFs and inbox attachments are not embedded. "
            "Download them from the app.",
            "Aspirely does not sell or share your personal information.",
        ],
    }
    yield '},"withheld":%s,"summary":%s}' % (json.dumps(withheld), json.dumps(summary))
