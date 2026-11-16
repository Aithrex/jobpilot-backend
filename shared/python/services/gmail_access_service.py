"""Who may connect Gmail while Google's consent screen is in Testing.

In Testing, only Google accounts on the project's test-user list can finish
Google's consent screen. The list holds at most 100 accounts and only the
project owner can add to it, by hand, in Google Cloud. A person who is not on
it is stopped on Google's own page ("Access blocked"), with no way back to the
app and nothing the app can say there.

So the app keeps its own copy of that list, and asks before it sends anyone to
Google:

  none       has not asked
  requested  asked; waiting for the owner to add the address at Google
  approved   the owner added the address at Google and recorded it here

Three gates:

  try     (default while GOOGLE_OAUTH_CONSENT_MODE is `testing`) Connect is
          offered to everyone. Most people who try are already on Google's
          list. Whoever Google stops comes back without a connection; the
          app saw the attempt (`attempted_at`) and offers "Ask for access",
          which records `requested` and emails the owner.
  invite  ask first: Connect only for `approved`, and for anyone who already
          holds a connection (they were on Google's list before this existed).
  open    once the consent screen is in `production`: everyone, no list.

GMAIL_ACCESS_GATE=try|invite|open overrides the default. Owner notices go to
GMAIL_ACCESS_NOTIFY_EMAIL (default harshith@aithrex.tech; `off` silences them).
Nothing here talks to Google: there is no API for the test-user list.

Collection: `gmail_access`, one row per person or per pre-approved address.
Additive. Dropping it returns everyone to `none`.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from utils.config import _get_config_value
from utils.db_connect import DBConnect

logger = logging.getLogger(__name__)

STATES = ("none", "requested", "approved")
GATES = ("try", "invite", "open")
# Who hears about each request. The owner keeps Google's test-user list.
DEFAULT_NOTIFY_EMAIL = "harshith@aithrex.tech"
# How long after a Connect press that came back empty the app keeps offering
# "Ask for access" up front.
ATTEMPT_WINDOW_HOURS = 72
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,190}\.[A-Za-z]{2,24}")


class GmailAccessError(ValueError):
    """A refused request. `code` is stable; str(error) is safe to show."""
    MESSAGES = {
        "gmail_address_invalid": "Enter the full Google address you will connect, like name@gmail.com.",
        "gmail_access_not_found": "No request was found for that address.",
    }

    def __init__(self, code: str):
        self.code = code
        super().__init__(self.MESSAGES[code])


def _rows():
    return DBConnect().get_db().gmail_access


_INDEXES_READY = False


def _ensure_indexes() -> None:
    global _INDEXES_READY
    if _INDEXES_READY:
        return
    _INDEXES_READY = True
    try:
        _rows().create_index("user_email", name="user_email")
        _rows().create_index("google_email", name="google_email")
        _rows().create_index([("state", 1), ("requested_at", 1)], name="state_requested_at")
    except Exception as e:  # pragma: no cover - an index must never block a request
        logger.warning("gmail access index setup skipped: %s", type(e).__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: Any) -> Optional[str]:
    if not isinstance(value, datetime):
        return None
    return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).isoformat()


def normalize_address(value: Any) -> Optional[str]:
    """A lower-cased email address, or None when it is not one."""
    if not isinstance(value, str):
        return None
    value = value.strip().lower()
    return value if _EMAIL.fullmatch(value) else None


def gate() -> str:
    """`try` (connect first, ask if Google stops you), `invite` (ask first) or
    `open` (no list at all)."""
    raw = (_get_config_value("GMAIL_ACCESS_GATE", "") or "").strip().lower()
    if raw in GATES:
        return raw
    from services import gmail_service
    return "open" if gmail_service.consent_mode() == "production" else "try"


def seat_limit() -> int:
    """Google's cap on the test-user list. A setting only so the app can say
    "full" at the same number Google does."""
    try:
        return max(1, min(100000, int(_get_config_value("GMAIL_TEST_USER_LIMIT", "100"))))
    except (TypeError, ValueError):
        return 100


def _recent_attempt(row: Optional[Dict[str, Any]]) -> Optional[str]:
    """When this person last pressed Connect, if that was recent."""
    at = (row or {}).get("last_attempt_at")
    if not isinstance(at, datetime):
        return None
    at = at if at.tzinfo else at.replace(tzinfo=timezone.utc)
    if (_now() - at).total_seconds() > ATTEMPT_WINDOW_HOURS * 3600:
        return None
    return at.isoformat()


def _public(row: Optional[Dict[str, Any]], state: str, current_gate: str,
            attempt_row: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    row = row or {}
    return {"gate": current_gate, "state": state,
            "google_email": row.get("google_email") if state != "none" else None,
            "requested_at": _iso(row.get("requested_at")),
            "decided_at": _iso(row.get("decided_at")) if state == "approved" else None,
            "attempted_at": _recent_attempt(attempt_row if attempt_row is not None else row)}


def access_for(user_email: str, has_connection: bool = False) -> Dict[str, Any]:
    """Where this person stands. Read-only."""
    current_gate = gate()
    if current_gate == "open":
        return _public(None, "approved", current_gate) | {"google_email": None}
    row = _rows().find_one({"user_email": user_email})
    if row and row.get("state") == "approved":
        return _public(row, "approved", current_gate)
    if has_connection:
        # Connected before the gate existed, so already on Google's list.
        return _public(row, "approved", current_gate) | {
            "google_email": (row or {}).get("google_email"), "attempted_at": None}
    # The owner may have recorded the address before the person asked.
    listed = _rows().find_one({"google_email": user_email.strip().lower(), "state": "approved"})
    if listed and not (row and row.get("google_email") not in (None, listed["google_email"])):
        return _public(listed, "approved", current_gate, attempt_row=row)
    if row and row.get("state") == "requested":
        return _public(row, "requested", current_gate)
    return _public(None, "none", current_gate, attempt_row=row)


def may_connect(user_email: str, has_connection: bool = False) -> bool:
    """`try` and `open` send everyone to Google; `invite` only the approved."""
    if gate() in ("try", "open"):
        return True
    return access_for(user_email, has_connection)["state"] == "approved"


def record_attempt(user_email: str) -> None:
    """Remember a Connect press, so a person Google stops is offered "Ask for
    access" when they come back. Best effort: never blocks the sign-in."""
    if gate() != "try":
        return
    try:
        _ensure_indexes()
        now = _now()
        _rows().update_one(
            {"user_email": user_email},
            {"$set": {"last_attempt_at": now},
             "$setOnInsert": {"state": "none", "google_email": None, "requested_at": None,
                              "created_at": now}},
            upsert=True)
    except Exception as e:  # pragma: no cover
        logger.warning("gmail connect attempt not recorded: %s", type(e).__name__)


def login_hint(user_email: str) -> Optional[str]:
    """The Google account to preselect on Google's screen: the one the owner
    added. Choosing another account there is what gets a person blocked."""
    if gate() == "open":
        return None
    row = _rows().find_one({"user_email": user_email, "state": "approved"}) or _rows().find_one(
        {"google_email": user_email.strip().lower(), "state": "approved"})
    return (row or {}).get("google_email")


def request_access(user_email: str, google_email: Any) -> Dict[str, Any]:
    """Record that this person wants Gmail for `google_email`. Idempotent."""
    address = normalize_address(google_email if google_email else user_email)
    if not address:
        raise GmailAccessError("gmail_address_invalid")
    _ensure_indexes()
    now = _now()
    current = _rows().find_one({"user_email": user_email})
    if current and current.get("google_email") == address and current.get("state") in ("requested", "approved"):
        return access_for(user_email)
    listed = _rows().find_one({"google_email": address, "state": "approved"})
    if listed and listed.get("user_email") in (None, user_email):
        # Already added at Google for this address: nothing to wait for.
        _rows().update_one({"_id": listed["_id"]}, {"$set": {
            "user_email": user_email, "requested_at": listed.get("requested_at") or now}})
        if current and current["_id"] != listed["_id"]:
            _rows().delete_one({"_id": current["_id"]})
        return access_for(user_email)
    _rows().update_one(
        {"user_email": user_email},
        {"$set": {"google_email": address, "state": "requested", "requested_at": now,
                  "decided_at": None, "decided_by": None},
         "$setOnInsert": {"created_at": now}},
        upsert=True)
    _notify_owner(user_email, address)
    return access_for(user_email)


def _notify_owner(user_email: str, address: str) -> None:
    """Tell the person who keeps Google's list. Best effort, never fatal."""
    raw = (_get_config_value("GMAIL_ACCESS_NOTIFY_EMAIL", "") or "").strip()
    if raw.lower() in ("off", "none", "false", "0"):
        return
    to = normalize_address(raw or DEFAULT_NOTIFY_EMAIL)
    if not to:
        return
    try:
        from services import email_service
        waiting = _rows().count_documents({"state": "requested"})
        lead = ("A person could not connect Gmail and asked for access. While the app is in "
                "Testing, Google lets only listed test users through.\n\n"
                if gate() == "try" else "A person asked for Gmail updates.\n\n")
        text = (lead +
                f"Google address to add: {address}\nAspirely account: {user_email}\n"
                f"Waiting in total: {waiting}\n\n"
                "1. Google Cloud > Google Auth Platform > Audience > Test users > Add users.\n"
                "2. Admin portal > Gmail access > Mark as added.\n")
        email_service.send_email(to, f"Gmail access request: {address}",
                                 "<pre style=\"font-family:inherit\">" + text.replace("<", "&lt;") + "</pre>", text)
    except Exception as e:  # pragma: no cover
        logger.warning("gmail access owner notice failed: %s", type(e).__name__)


def _notify_ready(user_email: str, address: str) -> bool:
    """Tell the person their address is on Google's list. Best effort."""
    try:
        from services import email_service
        app_url = (_get_config_value("APP_URL", "") or _get_config_value("FRONTEND_URL", "")
                   or "https://aspirely.me").rstrip("/")
        link = f"{app_url}/app/apply/tracker"
        name = getattr(email_service, "APP_NAME", "Aspirely")
        html_body = f"""\
<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;max-width:480px;margin:auto;color:#101422">
  <h2 style="margin:0 0 12px;font-weight:600">Gmail updates are ready for you</h2>
  <p style="color:#444A5A;line-height:1.5;margin:0 0 16px">You asked {name} to keep your application tracker
  up to date from Gmail. Your address <strong>{address}</strong> can now connect.</p>
  <p style="margin:20px 0"><a href="{link}" style="background:#2648D6;color:#fff;padding:12px 20px;border-radius:8px;
     text-decoration:none;display:inline-block;font-weight:600">Connect Gmail</a></p>
  <p style="color:#5C6170;font-size:13px;line-height:1.5;margin:0">On Google's screen choose {address}. Access is
  read only: {name} never sends, deletes or changes email. Google will ask you to reconnect after 7 days
  while it reviews {name}.</p>
</div>"""
        text_body = (f"Gmail updates are ready for you.\n\nYour address {address} can now connect: {link}\n\n"
                     f"On Google's screen choose {address}. Access is read only. Google will ask you to "
                     f"reconnect after 7 days while it reviews {name}.")
        return bool(email_service.send_email(user_email, f"Gmail updates are ready on {name}",
                                             html_body, text_body))
    except Exception as e:  # pragma: no cover
        logger.warning("gmail access ready notice failed: %s", type(e).__name__)
        return False


# ---------------------------------------------------------------------------
# For the owner (admin blueprint)
# ---------------------------------------------------------------------------

def _admin_row(row: Dict[str, Any]) -> Dict[str, Any]:
    return {"user_email": row.get("user_email"), "google_email": row.get("google_email"),
            "state": row.get("state"), "requested_at": _iso(row.get("requested_at")),
            "decided_at": _iso(row.get("decided_at")), "decided_by": row.get("decided_by")}


def overview() -> Dict[str, Any]:
    """Everything the owner needs to keep the two lists the same."""
    from services import gmail_service
    rows = list(_rows().find({}).sort([("requested_at", 1), ("_id", 1)]))
    connected = {str(c.get("gmail_address") or "").lower(): c.get("user_email")
                 for c in DBConnect().get_db().gmail_connections.find({}, {"gmail_address": 1, "user_email": 1})
                 if c.get("gmail_address")}
    approved = [r for r in rows if r.get("state") == "approved"]
    # Every address the owner has added at Google that the app knows of: the
    # approvals here, plus mailboxes connected before this list existed.
    listed = {r.get("google_email") for r in approved if r.get("google_email")} | set(connected)
    return {
        "gate": gate(), "consent_mode": gmail_service.consent_mode(),
        "limit": seat_limit(), "used": len(listed),
        "pending": [_admin_row(r) for r in rows if r.get("state") == "requested"],
        "approved": [_admin_row(r) | {"connected": r.get("google_email") in connected} for r in approved],
        "connected_without_a_record": sorted(set(connected) - {r.get("google_email") for r in approved}),
    }


def approve(by: str, google_email: Any = None, user_email: Any = None) -> Dict[str, Any]:
    """Record that an address is now on Google's test-user list.

    With `user_email`, approves that person's request. With only
    `google_email`, records an address ahead of any request, so whoever signs
    in with it (or asks for it) is let through.
    """
    _ensure_indexes()
    now = _now()
    row = None
    if isinstance(user_email, str) and user_email.strip():
        row = _rows().find_one({"user_email": user_email.strip()})
        if not row:
            raise GmailAccessError("gmail_access_not_found")
        address = row.get("google_email")
    else:
        address = normalize_address(google_email)
        if not address:
            raise GmailAccessError("gmail_address_invalid")
        row = _rows().find_one({"google_email": address})
    change = {"state": "approved", "decided_at": now, "decided_by": by}
    if row:
        was_requested = row.get("state") == "requested"
        _rows().update_one({"_id": row["_id"]}, {"$set": change})
        notified = bool(was_requested and row.get("user_email")
                        and _notify_ready(row["user_email"], address))
    else:
        _rows().insert_one({"user_email": None, "google_email": address, "requested_at": None,
                            "created_at": now, **change})
        notified = False
    saved = _rows().find_one({"google_email": address, "state": "approved"}) or {}
    return _admin_row(saved) | {"notified": notified}


def remove(google_email: Any = None, user_email: Any = None) -> bool:
    """Forget a request or an approval. The person is back to `none`. This does
    not remove anyone from Google's list, and does not disconnect a mailbox."""
    if isinstance(user_email, str) and user_email.strip():
        return bool(_rows().delete_one({"user_email": user_email.strip()}).deleted_count)
    address = normalize_address(google_email)
    if not address:
        raise GmailAccessError("gmail_address_invalid")
    return bool(_rows().delete_many({"google_email": address}).deleted_count)
