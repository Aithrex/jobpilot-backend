"""Notification router + preferences + consent ledger (Compass §2.5 / §6.4).

Public API
----------
    send(user_email, event, payload) -> dict     # route to the best channel
    get_prefs(user_email) -> dict                 # channel + per-event prefs
    set_prefs(user_email, prefs) -> dict          # partial update, sanitized
    set_unsubscribed(user_email, bool) -> dict    # master proactive kill-switch
    start_channel_optin(...) / confirm_channel_optin(...) / channel_consent(...)

Routing rule for ``send`` (proactive events also pass through the guards):
    1. respect the master unsubscribe (proactive only)
    2. respect the per-event toggle
    3. respect quiet hours in the user's timezone (proactive only)
    4. reserve one of the ``max 2 proactive/day`` slots atomically (proactive only)
    5. walk the user's preferred channels; pick the first ``available()`` one
       that also has consent (WhatsApp/SMS); ALWAYS fall back to email
    6. log every attempt to ``notification_log`` (one doc per user per day)

Collections (all user_email-scoped, singleton ``DBConnect``):
    notification_prefs   — one doc per user (unique user_email); default email-on
    notification_log     — one doc per (user_email, day): proactive counter
                           ($inc, atomic) + an append-only ``sends`` audit array
    notification_consents— per (user_email, scope) double-opt-in ledger for
                           WhatsApp/SMS: number + consent_text_version + ts + ip

Import-safe: Mongo/channels are only touched inside functions.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from utils.db_connect import DBConnect
from utils.security import InputSanitizer

from services.notifications import (
    WHATSAPP_CONSENT_SCOPE,
    SMS_CONSENT_SCOPE,
    CONSENT_TEXT_VERSION,
)

logger = logging.getLogger(__name__)

# --- constants --------------------------------------------------------------
CHANNEL_KEYS = ["email", "whatsapp", "sms", "push"]
MAX_PROACTIVE_PER_DAY = 2
_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")

# Event registry: which events exist, whether they count against the proactive
# cap / quiet hours, and whether they are on by default. Digests are proactive;
# transactional events (e.g. an interview reminder the user asked for) are not.
EVENTS: Dict[str, Dict[str, Any]] = {
    "daily_digest":     {"proactive": True,  "default_on": False, "label": "Morning job digest"},
    "evening_summary":  {"proactive": True,  "default_on": False, "label": "Evening summary"},
    "match_alert":      {"proactive": True,  "default_on": False, "label": "New strong-match alert"},
    "application_nudge":{"proactive": True,  "default_on": True,  "label": "Follow-up reminders"},
    "visa_cliff":       {"proactive": True,  "default_on": True,  "label": "Visa / OPT deadline alerts"},
    "interview_reminder":{"proactive": False,"default_on": True,  "label": "Interview reminders"},
    "saved_search_digest": {"proactive": True, "default_on": False,
                            "label": "Daily saved-search job alerts"},
    # An auto-apply row hit a wall only the user can clear on the employer's
    # site (CAPTCHA / email verification / candidate account). TRANSACTIONAL:
    # it is the direct consequence of a run the user started, and its whole
    # value is immediacy — quiet-housing it or spending a digest slot on it
    # would leave the application stalled until morning.
    "application_wall": {"proactive": False, "default_on": True,
                         "label": "Application needs you on the employer's site"},
}

_CONSENT_SCOPE = {"whatsapp": WHATSAPP_CONSENT_SCOPE, "sms": SMS_CONSENT_SCOPE}

# ---------------------------------------------------------------------------
# Email lists (CAN-SPAM opt-out scope), layered on THIS store.
#
# A list is what one unsubscribe link turns off. It lives in the same
# `notification_prefs` document as the per-event toggles above, so there is
# one source of truth: `email_lists.<list>` is the list-level subscription and
# the per-event toggles stay the finer, opt-in choice inside a list.
#
#   * Unsubscribing from a list turns its events OFF (after snapshotting them
#     in `email_lists_restore.<list>`) so the scheduler stops spending on mail
#     nobody will receive.
#   * Re-subscribing restores that snapshot, never a blanket "all on".
#   * Turning an event ON in Settings re-subscribes its list: the latest
#     explicit choice wins.
#   * The master `unsubscribed` switch still means "no proactive email at all"
#     and reads as every list off.
#
# Mail that is not driven by an event (auto-apply approvals, the welcome email)
# names its list at send time; `email_service.send_email(list_id=...)` refuses
# to send when that list is off.
# ---------------------------------------------------------------------------
EMAIL_LISTS = ("job_alerts", "digests", "product_updates", "marketing")
LIST_EVENTS: Dict[str, tuple] = {
    "job_alerts": ("match_alert", "saved_search_digest", "application_nudge", "visa_cliff"),
    "digests": ("daily_digest", "evening_summary"),
    "product_updates": (),
    "marketing": (),
}
# Marketing is OFF until a user opts in. Aspirely sends no marketing email
# today, and opt-in is the conservative default under state law.
LIST_DEFAULTS: Dict[str, bool] = {
    "job_alerts": True, "digests": True, "product_updates": True, "marketing": False,
}
EVENT_LIST: Dict[str, str] = {e: name for name, evs in LIST_EVENTS.items() for e in evs}


# --- collection helpers -----------------------------------------------------
def _db():
    return DBConnect().get_db()


def _prefs_col():
    return _db().notification_prefs


def _log_col():
    return _db().notification_log


def _consent_col():
    return _db().notification_consents


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _email(user_email: str) -> str:
    """Sanitize the identity (from JWT, but never trust into a Mongo filter)."""
    return InputSanitizer.sanitize_email(user_email) or ""


def _event_meta(event: str) -> Dict[str, Any]:
    return EVENTS.get(event, {"proactive": False, "default_on": True, "label": event})


# ===========================================================================
# Preferences
# ===========================================================================
def _default_prefs() -> Dict[str, Any]:
    """Default: EMAIL on, everything else off; proactive events on; quiet hours
    22:00–07:00 in the user's timezone (until they set their own)."""
    return {
        "unsubscribed": False,
        "channels": {
            "email": {"enabled": True},
            "whatsapp": {"enabled": False},
            "sms": {"enabled": False},
            "push": {"enabled": False, "subscription": None},
        },
        "channel_priority": ["whatsapp", "sms", "push", "email"],
        "events": {k: v["default_on"] for k, v in EVENTS.items()},
        "quiet_hours": {"enabled": True, "start": "22:00", "end": "07:00", "tz": "America/New_York"},
        "proactive_daily_cap": MAX_PROACTIVE_PER_DAY,
        "email_lists": dict(LIST_DEFAULTS),
    }


def get_prefs(user_email: str) -> Dict[str, Any]:
    """Return the user's prefs merged onto defaults (email-on if unset)."""
    email = _email(user_email)
    defaults = _default_prefs()
    if not email:
        return defaults
    try:
        doc = _prefs_col().find_one({"user_email": email}, {"_id": 0}) or {}
    except Exception as e:
        logger.warning("[notify] get_prefs failed for %s: %s", email, e)
        doc = {}
    return _merge_prefs(defaults, doc)


def _merge_prefs(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    out["unsubscribed"] = bool(override.get("unsubscribed", base["unsubscribed"]))
    # channels
    ch = {k: dict(v) for k, v in base["channels"].items()}
    for key, val in (override.get("channels") or {}).items():
        if key in ch and isinstance(val, dict):
            if "enabled" in val:
                ch[key]["enabled"] = bool(val["enabled"])
            if key == "push" and "subscription" in val:
                ch[key]["subscription"] = val["subscription"]
    out["channels"] = ch
    # priority
    pr = [c for c in (override.get("channel_priority") or []) if c in CHANNEL_KEYS]
    out["channel_priority"] = pr or base["channel_priority"]
    # per-event toggles
    ev = dict(base["events"])
    for key, val in (override.get("events") or {}).items():
        if key in EVENTS:
            ev[key] = bool(val)
    out["events"] = ev
    # quiet hours
    qh = dict(base["quiet_hours"])
    for key in ("enabled", "start", "end", "tz"):
        if key in (override.get("quiet_hours") or {}):
            qh[key] = override["quiet_hours"][key]
    out["quiet_hours"] = qh
    cap = override.get("proactive_daily_cap")
    out["proactive_daily_cap"] = int(cap) if isinstance(cap, int) and 0 <= cap <= 10 else base["proactive_daily_cap"]
    lists = dict(base.get("email_lists") or LIST_DEFAULTS)
    stored_lists = override.get("email_lists")
    if isinstance(stored_lists, dict):
        for key in EMAIL_LISTS:
            if isinstance(stored_lists.get(key), bool):
                lists[key] = stored_lists[key]
    out["email_lists"] = lists
    return out


def set_prefs(user_email: str, prefs: Dict[str, Any]) -> Dict[str, Any]:
    """Validate + sanitize an untrusted partial prefs body, then upsert.

    Only whitelisted keys/shapes are persisted; unknown channels/events are
    dropped. All Mongo access is by the sanitized ``user_email`` equality only.
    """
    email = _email(user_email)
    if not email:
        raise ValueError("invalid user")
    prefs = prefs if isinstance(prefs, dict) else {}
    clean: Dict[str, Any] = {}

    if "unsubscribed" in prefs:
        clean["unsubscribed"] = bool(prefs["unsubscribed"])

    if isinstance(prefs.get("channels"), dict):
        channels: Dict[str, Any] = {}
        for key, val in prefs["channels"].items():
            key = InputSanitizer.sanitize_string(str(key), 16)
            if key not in CHANNEL_KEYS or not isinstance(val, dict):
                continue
            entry: Dict[str, Any] = {}
            if "enabled" in val:
                entry["enabled"] = bool(val["enabled"])
            if key == "push" and isinstance(val.get("subscription"), dict):
                entry["subscription"] = _clean_subscription(val["subscription"])
            if entry:
                channels[key] = entry
        if channels:
            clean["channels"] = channels

    if isinstance(prefs.get("channel_priority"), list):
        pr = [InputSanitizer.sanitize_string(str(c), 16) for c in prefs["channel_priority"]]
        pr = [c for c in pr if c in CHANNEL_KEYS]
        # de-dupe, preserve order
        seen, ordered = set(), []
        for c in pr:
            if c not in seen:
                seen.add(c); ordered.append(c)
        if ordered:
            clean["channel_priority"] = ordered

    if isinstance(prefs.get("events"), dict):
        events = {}
        for key, val in prefs["events"].items():
            key = InputSanitizer.sanitize_string(str(key), 40)
            if key in EVENTS:
                events[key] = bool(val)
        if events:
            clean["events"] = events

    # Turning an event ON is an explicit choice to get that mail, so it
    # re-subscribes the list the event belongs to (and drops the snapshot an
    # earlier unsubscribe left). Without this, Settings would show the event on
    # while send_email kept refusing the list.
    resubscribe = sorted({EVENT_LIST[e] for e, on in (clean.get("events") or {}).items()
                          if on and e in EVENT_LIST})

    if isinstance(prefs.get("quiet_hours"), dict):
        qh_in = prefs["quiet_hours"]
        qh: Dict[str, Any] = {}
        if "enabled" in qh_in:
            qh["enabled"] = bool(qh_in["enabled"])
        for edge in ("start", "end"):
            if edge in qh_in:
                t = InputSanitizer.sanitize_string(str(qh_in[edge]), 5)
                if _TIME_RE.match(t):
                    qh[edge] = t
        if "tz" in qh_in:
            tz = InputSanitizer.sanitize_string(str(qh_in["tz"]), 64)
            tz = re.sub(r"[^A-Za-z0-9_/+\-]", "", tz)
            if tz:
                qh["tz"] = tz
        if qh:
            clean["quiet_hours"] = qh

    if isinstance(prefs.get("proactive_daily_cap"), int):
        clean["proactive_daily_cap"] = max(0, min(int(prefs["proactive_daily_cap"]), 10))

    clean["user_email"] = email
    clean["updated_at"] = _now()
    # `events` is written key by key so a partial body cannot wipe toggles it
    # did not mention, and so the list re-subscribe below can sit beside it.
    set_doc = {k: v for k, v in clean.items() if k != "events"}
    for key, val in (clean.get("events") or {}).items():
        set_doc[f"events.{key}"] = val
    update: Dict[str, Any] = {"$set": set_doc}
    if resubscribe:
        for name in resubscribe:
            set_doc[f"email_lists.{name}"] = True
        update["$unset"] = {f"email_lists_restore.{name}": "" for name in resubscribe}
    _prefs_col().update_one({"user_email": email}, update, upsert=True)
    return get_prefs(email)


def set_unsubscribed(user_email: str, unsubscribed: bool = True) -> Dict[str, Any]:
    """Master proactive kill-switch (the one-click unsubscribe target)."""
    email = _email(user_email)
    if not email:
        raise ValueError("invalid user")
    _prefs_col().update_one(
        {"user_email": email},
        {"$set": {"user_email": email, "unsubscribed": bool(unsubscribed), "updated_at": _now()}},
        upsert=True,
    )
    return get_prefs(email)


# ===========================================================================
# Email lists (see the block comment at EMAIL_LISTS)
# ===========================================================================
def email_list_state(user_email: str) -> Dict[str, bool]:
    """``{list: subscribed}`` for every list. The master unsubscribe reads as
    every list off. Never raises: a store outage reports the defaults."""
    prefs = get_prefs(user_email)
    lists = prefs.get("email_lists") or dict(LIST_DEFAULTS)
    if prefs.get("unsubscribed"):
        return {name: False for name in EMAIL_LISTS}
    return {name: bool(lists.get(name, LIST_DEFAULTS[name])) for name in EMAIL_LISTS}


def list_allows(user_email: str, list_id: str) -> bool:
    """True when ``user_email`` may receive mail on ``list_id`` right now."""
    if list_id not in EMAIL_LISTS:
        return False
    return bool(email_list_state(user_email).get(list_id))


def _list_off_update(name: str, prefs: Dict[str, Any], set_doc: Dict[str, Any]) -> None:
    """Add the writes that turn list ``name`` off: snapshot its events (only
    when it is currently on, so a repeat unsubscribe cannot overwrite the real
    snapshot with all-False), then switch them off."""
    currently_on = bool((prefs.get("email_lists") or {}).get(name, LIST_DEFAULTS[name]))
    events = prefs.get("events") or {}
    if currently_on and LIST_EVENTS[name]:
        set_doc[f"email_lists_restore.{name}"] = {
            e: bool(events.get(e, EVENTS[e]["default_on"])) for e in LIST_EVENTS[name]
        }
    for e in LIST_EVENTS[name]:
        set_doc[f"events.{e}"] = False
    set_doc[f"email_lists.{name}"] = False


def set_email_lists(user_email: str, changes: Dict[str, bool]) -> Dict[str, bool]:
    """Apply ``{list: bool}`` changes and return the new :func:`email_list_state`.

    Idempotent: writing a list's current value changes nothing (so a client
    that echoes the whole object back on every toggle cannot flip events it
    never touched). Unknown list names raise ``ValueError``.
    """
    email = _email(user_email)
    if not email:
        raise ValueError("invalid user")
    changes = changes if isinstance(changes, dict) else {}
    unknown = [k for k in changes if k not in EMAIL_LISTS]
    if unknown:
        raise ValueError("unknown email list")

    try:
        raw = _prefs_col().find_one({"user_email": email}, {"_id": 0}) or {}
    except Exception as e:
        logger.warning("[notify] set_email_lists read failed: %s", type(e).__name__)
        raise
    prefs = _merge_prefs(_default_prefs(), raw)
    restore = raw.get("email_lists_restore") if isinstance(raw.get("email_lists_restore"), dict) else {}
    state = email_list_state(email)

    set_doc: Dict[str, Any] = {}
    unset_doc: Dict[str, Any] = {}
    master_off = bool(prefs.get("unsubscribed"))
    turning_on = [n for n, v in changes.items() if bool(v) and not state[n]]
    turning_off = [n for n, v in changes.items() if not bool(v) and state[n]]

    if master_off and turning_on:
        # Leaving "unsubscribe from everything" for ONE list must not quietly
        # re-enable the others: convert the master switch into per-list offs.
        for name in EMAIL_LISTS:
            if name not in turning_on:
                _list_off_update(name, prefs, set_doc)
        set_doc["unsubscribed"] = False

    for name in turning_off:
        _list_off_update(name, prefs, set_doc)

    for name in turning_on:
        set_doc[f"email_lists.{name}"] = True
        snap = restore.get(name) if isinstance(restore.get(name), dict) else None
        if snap:
            for e, on in snap.items():
                if e in LIST_EVENTS[name]:
                    set_doc[f"events.{e}"] = bool(on)
        unset_doc[f"email_lists_restore.{name}"] = ""
        # A master-off conversion above may have queued an off for this list.
        set_doc.pop(f"email_lists_restore.{name}", None)

    if not set_doc and not unset_doc:
        return state
    set_doc["user_email"] = email
    set_doc["updated_at"] = _now()
    update: Dict[str, Any] = {"$set": set_doc}
    if unset_doc:
        update["$unset"] = unset_doc
    _prefs_col().update_one({"user_email": email}, update, upsert=True)
    return email_list_state(email)


def unsubscribe_list(user_email: str, list_id: str) -> Dict[str, bool]:
    """The one-click target: turn one list off. Idempotent."""
    return set_email_lists(user_email, {list_id: False})


_PUSH_HOSTS = (
    "push.apple.com", "fcm.googleapis.com", "android.googleapis.com",
    "notify.windows.com", "push.services.mozilla.com",
)


def _allowed_push_endpoint(endpoint: str) -> bool:
    """SSRF guard: only accept HTTPS endpoints on known push-service hosts, so a
    stored subscription can't point the server at an internal/link-local address."""
    try:
        from urllib.parse import urlparse
        p = urlparse(endpoint)
        host = (p.hostname or "").lower()
        if p.scheme != "https" or not host:
            return False
        return any(host == h or host.endswith("." + h) for h in _PUSH_HOSTS)
    except Exception:
        return False


def _clean_subscription(sub: Dict[str, Any]) -> Dict[str, Any]:
    endpoint = InputSanitizer.sanitize_string(str(sub.get("endpoint") or ""), 1000)
    if endpoint and not _allowed_push_endpoint(endpoint):
        endpoint = ""  # reject non-push-service / private-range endpoints (SSRF)
    keys = sub.get("keys") if isinstance(sub.get("keys"), dict) else {}
    return {
        "endpoint": endpoint,
        "keys": {
            "p256dh": InputSanitizer.sanitize_string(str(keys.get("p256dh") or ""), 200),
            "auth": InputSanitizer.sanitize_string(str(keys.get("auth") or ""), 200),
        },
    }


# ===========================================================================
# Double opt-in consent (WhatsApp / SMS)
# ===========================================================================
def start_channel_optin(user_email: str, channel: str, number: str, ip: str = "",
                        version: str = CONSENT_TEXT_VERSION) -> Dict[str, Any]:
    """Record a PENDING opt-in for a phone channel and (when the channel is
    live) trigger the confirmation message. Double opt-in: not usable until
    :func:`confirm_channel_optin`. Stores number + consent text version + ts + ip.
    """
    email = _email(user_email)
    channel = (channel or "").strip().lower()
    scope = _CONSENT_SCOPE.get(channel)
    if not email or not scope:
        raise ValueError("invalid consent request")
    e164 = _sanitize_phone(number)
    if not e164:
        raise ValueError("invalid phone number")
    now = _now()
    _consent_col().update_one(
        {"user_email": email, "scope": scope},
        {"$set": {
            "user_email": email, "scope": scope, "channel": channel,
            "number": e164, "granted": False, "status": "pending",
            "consent_text_version": version,
            "requested_at": now, "granted_at": None, "revoked_at": None,
            "ip": InputSanitizer.sanitize_ip_address(ip) or "",
        }},
        upsert=True,
    )
    return channel_consent(email, channel)


def confirm_channel_optin(user_email: str, channel: str, ip: str = "") -> Dict[str, Any]:
    """Second leg of double opt-in — the user confirmed (replied YES / clicked
    the link). Flip the ledger row to granted."""
    email = _email(user_email)
    channel = (channel or "").strip().lower()
    scope = _CONSENT_SCOPE.get(channel)
    if not email or not scope:
        raise ValueError("invalid consent request")
    _consent_col().update_one(
        {"user_email": email, "scope": scope, "status": "pending"},
        {"$set": {"granted": True, "status": "confirmed", "granted_at": _now(),
                  "confirm_ip": InputSanitizer.sanitize_ip_address(ip) or ""}},
    )
    return channel_consent(email, channel)


def revoke_channel_consent(user_email: str, channel: str) -> Dict[str, Any]:
    email = _email(user_email)
    channel = (channel or "").strip().lower()
    scope = _CONSENT_SCOPE.get(channel)
    if not email or not scope:
        raise ValueError("invalid consent request")
    _consent_col().update_one(
        {"user_email": email, "scope": scope},
        {"$set": {"granted": False, "status": "revoked", "revoked_at": _now()}},
    )
    return channel_consent(email, channel)


def channel_consent(user_email: str, channel: str) -> Dict[str, Any]:
    email = _email(user_email)
    scope = _CONSENT_SCOPE.get((channel or "").strip().lower())
    if not email or not scope:
        return {"granted": False, "status": "none", "number": None}
    doc = _consent_col().find_one({"user_email": email, "scope": scope}) or {}
    return {
        "granted": bool(doc.get("granted")),
        "status": doc.get("status") or "none",
        "number": doc.get("number"),
        "version": doc.get("consent_text_version"),
    }


def _consented_number(email: str, channel: str) -> Optional[str]:
    c = channel_consent(email, channel)
    return c["number"] if c.get("granted") else None


def _sanitize_phone(number: str) -> str:
    raw = str(number or "").strip()
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) < 7 or len(digits) > 15:
        return ""
    return "+" + digits


# ===========================================================================
# Quiet hours + proactive daily cap
# ===========================================================================
def _user_now(prefs: Dict[str, Any]) -> datetime:
    tz_name = (prefs.get("quiet_hours") or {}).get("tz") or "UTC"
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(tz_name))
    except Exception:
        # Missing tzdata on the runtime or a bad name — fall back to UTC rather
        # than silently dropping a message.
        return datetime.now(timezone.utc)


def _in_quiet_hours(prefs: Dict[str, Any], now_local: datetime) -> bool:
    qh = prefs.get("quiet_hours") or {}
    if not qh.get("enabled"):
        return False
    start, end = qh.get("start"), qh.get("end")
    if not (_TIME_RE.match(str(start or "")) and _TIME_RE.match(str(end or ""))):
        return False
    cur = now_local.strftime("%H:%M")
    if start == end:
        return False
    if start < end:            # same-day window, e.g. 01:00–06:00
        return start <= cur < end
    return cur >= start or cur < end   # overnight window, e.g. 22:00–07:00


def _reserve_proactive_slot(email: str, day: str, cap: int) -> bool:
    """Atomically claim one of the day's proactive slots. Returns True if a slot
    was reserved, False if the cap is already reached.

    Uses a conditional ``$inc`` on the per-(user, day) log doc; the unique index
    on (user_email, day) makes the upsert race-safe (a DuplicateKeyError means a
    concurrent writer created the doc — retry once against the now-existing doc)."""
    from pymongo import ReturnDocument
    from pymongo.errors import DuplicateKeyError
    col = _log_col()
    for _ in range(2):
        try:
            doc = col.find_one_and_update(
                {"user_email": email, "day": day, "proactive_count": {"$lt": cap}},
                {"$inc": {"proactive_count": 1},
                 "$setOnInsert": {"user_email": email, "day": day, "created_at": _now()},
                 "$set": {"updated_at": _now()}},
                upsert=True,
                return_document=ReturnDocument.AFTER,
            )
            return doc is not None
        except DuplicateKeyError:
            # Doc already exists (created concurrently or already at cap). If it
            # exists but is at cap, the filter won't match on retry -> no match,
            # which surfaces as the cap being reached below.
            existing = col.find_one({"user_email": email, "day": day})
            if existing and existing.get("proactive_count", 0) >= cap:
                return False
            continue
    return False


def _log_send(email: str, day: str, entry: Dict[str, Any]) -> None:
    """Append one attempt to the per-day audit array (never raises)."""
    try:
        _log_col().update_one(
            {"user_email": email, "day": day},
            {"$setOnInsert": {"user_email": email, "day": day, "created_at": _now()},
             "$set": {"updated_at": _now()},
             "$push": {"sends": {**entry, "ts": _now()}}},
            upsert=True,
        )
    except Exception as e:
        logger.warning("[notify] log_send failed for %s: %s", email, e)


# ===========================================================================
# The router
# ===========================================================================
def send(user_email: str, event: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Route ``event`` to the best available channel for ``user_email``.

    Returns a small result dict, e.g.
        {"ok": True,  "status": "sent",    "channel": "email", "event": "daily_digest"}
        {"ok": False, "status": "blocked", "reason": "quiet_hours", "event": ...}
    Never raises — a failure to notify must never break the caller (scheduler).
    """
    payload = payload if isinstance(payload, dict) else {}
    email = _email(user_email)
    if not email:
        return {"ok": False, "status": "blocked", "reason": "invalid_user", "event": event}

    meta = _event_meta(event)
    proactive = bool(meta["proactive"])
    prefs = get_prefs(email)

    # 1. master unsubscribe (proactive only)
    if proactive and prefs.get("unsubscribed"):
        return {"ok": False, "status": "blocked", "reason": "unsubscribed", "event": event}

    # 2. per-event toggle
    if not prefs["events"].get(event, meta["default_on"]):
        return {"ok": False, "status": "blocked", "reason": "event_off", "event": event}

    now_local = _user_now(prefs)
    day = now_local.strftime("%Y-%m-%d")

    # 3. quiet hours (proactive only)
    if proactive and _in_quiet_hours(prefs, now_local):
        _log_send(email, day, {"event": event, "channel": None, "status": "deferred", "reason": "quiet_hours"})
        return {"ok": False, "status": "blocked", "reason": "quiet_hours", "event": event}

    # 4. proactive daily cap (atomic reserve)
    if proactive:
        cap = int(prefs.get("proactive_daily_cap", MAX_PROACTIVE_PER_DAY))
        if not _reserve_proactive_slot(email, day, cap):
            _log_send(email, day, {"event": event, "channel": None, "status": "blocked", "reason": "daily_cap"})
            return {"ok": False, "status": "blocked", "reason": "daily_cap", "event": event}

    # 5. walk preferred channels; email is the guaranteed fallback
    for channel_key in _ordered_channels(prefs):
        result = _try_channel(email, channel_key, prefs, event, payload)
        if result is None:
            continue  # not eligible (disabled / no consent / unavailable)
        ok, to_meta = result
        _log_send(email, day, {"event": event, "channel": channel_key,
                               "status": "sent" if ok else "failed", "to": to_meta,
                               "proactive": proactive})
        if ok:
            return {"ok": True, "status": "sent", "channel": channel_key, "event": event}

    return {"ok": False, "status": "failed", "reason": "no_channel_delivered", "event": event}


def _ordered_channels(prefs: Dict[str, Any]) -> List[str]:
    """Enabled channels in the user's priority order, email guaranteed last."""
    enabled = {k for k, v in prefs["channels"].items() if v.get("enabled")}
    enabled.add("email")  # always the fallback
    ordered = [c for c in prefs.get("channel_priority", []) if c in enabled]
    for c in enabled:
        if c not in ordered:
            ordered.append(c)
    # email always sits last so richer channels win but delivery is guaranteed
    ordered = [c for c in ordered if c != "email"] + ["email"]
    return ordered


def _try_channel(email: str, channel_key: str, prefs: Dict[str, Any],
                 event: str, payload: Dict[str, Any]):
    """Attempt one channel. Returns (ok, to_meta) if it was ELIGIBLE and tried,
    or None if the channel should be skipped. Never raises."""
    from services.notifications import channels as ch_mod
    channel = ch_mod.get_channel(channel_key)
    if channel is None or not channel.available():
        return None

    # resolve the destination for this channel
    if channel_key == "email":
        to: Any = email
        to_meta = email
    elif channel_key in ch_mod.CONSENT_REQUIRED:
        number = _consented_number(email, channel_key)
        if not number:
            return None  # no confirmed double opt-in -> skip
        to, to_meta = number, ch_mod._mask(number)
    elif channel_key == "push":
        sub = (prefs["channels"].get("push") or {}).get("subscription")
        if not sub:
            return None
        to, to_meta = sub, "push"
    else:
        return None

    try:
        ok = bool(channel.send(to, event, payload))
    except Exception as e:  # channels never raise, but belt-and-suspenders
        logger.warning("[notify] channel %s raised for event=%s: %s", channel_key, event, e)
        ok = False
    return ok, to_meta
