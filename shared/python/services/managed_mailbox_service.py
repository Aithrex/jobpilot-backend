"""Managed Application Mailbox — provisioning + alias lifecycle (managed-mailbox-v1 §1/§2).

This is the "apply from a managed address" feature (the Tsenta-style relay): each
user gets ONE stable, human-readable managed address of the form
``firstname.lastname@<APPLY_MAIL_DOMAIN>`` (D1) that they hand to employers/ATS
platforms. It is a NEW, separate feature from ``mail_ingress_service.py``
("forward your own Gmail to us") — see the spec's §9 for how the two relate.

This module owns ONLY the address model (provisioning, the immutable primary
alias, collision handling, tombstone anti-recycle, and the mailbox controls). The
inbound Cloudflare webhook, S3 quarantine, thread/message storage, forwarding
send and the Flask routes are LATER tasks and are intentionally NOT here.

Design invariants (from the spec's §0 locked decisions):
  - D1  Address = slug of the user's verified name; user-readable, ATS-friendly.
  - D2  Collision rule: ``base``, then ``base2``, ``base3`` … capped at N, then a
        final ``base.<4-hex>`` fallback. Two identical names never share an
        address; a suffix only ever appears on a REAL collision.
  - D3  Domain is a neutral registered domain we control (``APPLY_MAIL_DOMAIN``);
        empty -> the feature is inert (we still reserve local parts).
  - D7  Owner identity is ALWAYS the caller-supplied ``user_email`` (resolved from
        the JWT by the route) — NEVER a name parsed from a resume or a request
        body. Every query is owner-scoped.
  - §1.3 Tombstones: a rotated/retired alias lives forever in ``mail_aliases`` with
        ``state="retired"`` so its local part can never be reassigned to anyone.
        Uniqueness on ``local_part`` spans ALL states — that IS the anti-recycle
        guard.

Security model (untrusted input):
  - Every value that reaches a Mongo filter is sanitized first (NoSQL-injection
    defense, invariant #4). ``resolve_alias`` runs on webhook-derived envelope
    recipients, so it treats its input as fully hostile.
  - The unique index on ``local_part`` is the real race/anti-recycle backstop; the
    in-code collision scan is the fast path and ``DuplicateKeyError`` is the
    authority, exactly like ``mail_ingress_service._mint_address``.

Feature flag: ``APPLY_MAILBOX_ENABLED`` (default OFF EVERYWHERE, including dev —
unlike ``mail_ingress`` this needs external infra: a neutral domain + Cloudflare
Email Routing + an S3/KMS bucket + an edge route; see the spec's §11/§12).
Provisioning the address model itself is a pure DB operation and is not gated by
the flag — the flag gates the live inbound/forwarding pipeline (later tasks).
"""

from __future__ import annotations

import logging
import re
import secrets
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterator, Optional

from pymongo.errors import DuplicateKeyError

from utils.config import _get_config_value
from utils.db_connect import DBConnect
from utils.security import InputSanitizer

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Max numeric suffix tried before the opaque token fallback (D2's "capped at N").
# base + base2..base<CAP> = CAP candidates, then base.<4-hex>.
_COLLISION_CAP = 20

# How many opaque `base.<4-hex>` fallbacks to try before giving up (each is a
# fresh random token; a collision here is astronomically unlikely).
_FALLBACK_ATTEMPTS = 5

# forward_to verification token TTL (short-lived; the user clicks the emailed
# link once). A day is generous but bounded; the token is single-use anyway.
_FORWARD_VERIFY_TTL_SECONDS = 24 * 60 * 60

# Longest local part we mint (spec §1.1). ATS platforms and RFC 5321 both tolerate
# far more, but a bounded slug keeps the address readable.
_MAX_LOCAL_LEN = 40

_TRUE = {"1", "true", "yes", "on"}

# The only characters a minted local part can contain (lowercase ASCII alnum +
# the dot separator). Used to normalize a hostile inbound recipient for lookup
# WITHOUT destroying the structural dots in `firstname.lastname`.
_LOCAL_ALLOWED = re.compile(r"[^a-z0-9.]")


# ---------------------------------------------------------------------------
# Config / feature flag
# ---------------------------------------------------------------------------

def apply_mail_domain() -> str:
    """The neutral managed-mail domain (``APPLY_MAIL_DOMAIN``, e.g. ``pilotmail.co``).

    Empty -> the feature is inert (D3): we still reserve local parts so the model
    is ready, but a formed address has no host until the domain is configured."""
    return (_get_config_value("APPLY_MAIL_DOMAIN", "") or "").strip().lower()


def mailbox_enabled() -> bool:
    """``APPLY_MAILBOX_ENABLED`` master flag. Default OFF everywhere (including
    dev) — the live inbound/forwarding pipeline needs external infra that does
    not exist yet (spec §11). Unset/anything-but-truthy -> False."""
    raw = (_get_config_value("APPLY_MAILBOX_ENABLED", "") or "").strip().lower()
    return raw in _TRUE


# ---------------------------------------------------------------------------
# Mongo collections (created on first use)
# ---------------------------------------------------------------------------

def _mailboxes():
    return DBConnect().get_db().managed_mailboxes


def _aliases():
    return DBConnect().get_db().mail_aliases


def _forward_verifications():
    return DBConnect().get_db().mail_forward_verifications


def ensure_indexes() -> None:
    """Create the collections' indexes. Call from scripts/migrate_indexes.py —
    NOT at import time (keeps cold starts lean). Safe to run repeatedly.

    The ``mail_aliases.local_part`` unique index spans ALL states (active,
    paused, retired) — it is the anti-recycle tombstone guard (spec §1.3) AND the
    authoritative race backstop for the collision scan in ``provision``/``rotate``.
    Local parts are always stored lower-cased, so the index is effectively
    case-folded."""
    _mailboxes().create_index("user_email", unique=True, name="uniq_user_email")
    _mailboxes().create_index("mailbox_id", unique=True, name="uniq_mailbox_id")
    _aliases().create_index("local_part", unique=True, name="uniq_local_part")
    _aliases().create_index("alias_id", unique=True, name="uniq_alias_id")
    _aliases().create_index("mailbox_id", name="by_mailbox_id")
    _aliases().create_index("user_email", name="by_user_email")
    # forward_to verification tokens: one-use, TTL-reaped (~24h).
    _forward_verifications().create_index("token", unique=True, name="uniq_fv_token")
    _forward_verifications().create_index("user_email", name="fv_by_user_email")
    _forward_verifications().create_index(
        "expires_at", expireAfterSeconds=0, name="fv_ttl_expires_at"
    )


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _slug_name(display_name: str) -> str:
    """Slug a display name into a managed local part (D1 / spec §1.1):
    ASCII-fold (NFKD, drop combining marks), lowercase, non-alnum -> ``.``,
    collapse repeat dots, strip leading/trailing dots, cap at 40 chars.

    ``Harshith Manne`` -> ``harshith.manne`` · ``José Núñez`` -> ``jose.nunez``.
    Returns ``""`` for empty/degenerate input (the caller falls back to an opaque
    privacy local part) — a name of pure symbols must never yield an empty or
    dotted-only address."""
    if not display_name:
        return ""
    # NFKD decomposes accented letters into base + combining mark; dropping the
    # combining marks yields the ASCII base (é->e, ñ->n, ü->u).
    decomposed = unicodedata.normalize("NFKD", str(display_name))
    ascii_only = "".join(c for c in decomposed if not unicodedata.combining(c))
    ascii_only = ascii_only.encode("ascii", "ignore").decode("ascii").lower()
    # Any run of non-alnum becomes a single dot separator.
    slug = re.sub(r"[^a-z0-9]+", ".", ascii_only)
    # Collapse repeats + strip edges, cap, then strip edges again in case the
    # cap landed on a separator.
    slug = re.sub(r"\.{2,}", ".", slug).strip(".")
    return slug[:_MAX_LOCAL_LEN].strip(".")


def _privacy_local() -> str:
    """Opaque privacy local part (``career.<6-hex>``) for users who do not want
    their name exposed, and the fallback when a name degenerates to empty."""
    return "career." + secrets.token_hex(3)


def _normalize_inbound_local(value: str) -> str:
    """Normalize a hostile inbound recipient (envelope-to or bare local) to the
    minted local-part form, NoSQL-safely.

    NOTE: we deliberately do NOT pass the whole local part through
    ``InputSanitizer.sanitize_mongo_query`` — that helper rewrites ``.`` to ``_``,
    which would destroy the structural dot in ``firstname.lastname`` and break
    every legitimate delivery. Instead we sanitize each dot-separated LABEL with
    it (stripping ``$``/control chars, so ``$ne`` -> ``ne``) and rejoin with dots.
    The value is always coerced to a plain ``str`` that only contains
    ``[a-z0-9.]``, so it can never occupy a Mongo operator position."""
    raw = (value or "").strip().lower()
    if not raw:
        return ""
    local = raw.split("@", 1)[0]
    # Per-label operator/injection strip (dots are structural — preserve them).
    labels = [InputSanitizer.sanitize_mongo_query(p) for p in local.split(".")]
    joined = ".".join(p for p in labels if p)
    # Belt-and-suspenders: keep only the minted alphabet.
    return _LOCAL_ALLOWED.sub("", joined).strip(".")


def _candidate_locals(base: str) -> Iterator[str]:
    """Yield local-part candidates in D2 order: ``base``, ``base2`` … ``base<CAP>``,
    then up to ``_FALLBACK_ATTEMPTS`` opaque ``base.<4-hex>`` fallbacks."""
    yield base
    for i in range(2, _COLLISION_CAP + 1):
        yield f"{base}{i}"
    for _ in range(_FALLBACK_ATTEMPTS):
        yield f"{base}.{secrets.token_hex(2)}"


def _read_through_mode(user_email: Optional[str], fallback: Optional[str]) -> Optional[str]:
    """The autonomy mode is a SINGLE source of truth on
    ``auto_submit_settings.mode`` (autonomy_service); this mailbox field READS
    THROUGH to it so the two can never drift. When settings has no mode yet, fall
    back to the legacy mailbox-stored value (back-compat for pre-modes mailboxes).
    Never raises — a settings read failure degrades to the legacy value."""
    try:
        from services import autonomy_service
        m = autonomy_service.get_mode(user_email)
        if m is not None:
            return m
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("managed_mailbox: mode read-through failed for %s: %s",
                       user_email, e)
    return fallback


def _serialize_mailbox(mailbox: Dict[str, Any]) -> Dict[str, Any]:
    """Shape a ``managed_mailboxes`` doc into the spec §5 GET response. The
    address is resolved from the primary alias (the mailbox doc stores only the
    ``primary_alias_id``, per §2). ``mode`` reads THROUGH to
    ``auto_submit_settings.mode`` (the single source of truth)."""
    alias = _aliases().find_one({"alias_id": mailbox.get("primary_alias_id")})
    return {
        "provisioned": True,
        "address": (alias or {}).get("address"),
        "mode": _read_through_mode(mailbox.get("user_email"),
                                   mailbox.get("mode", "approval")),
        "paused": bool(mailbox.get("paused", False)),
        "forwarding_enabled": bool(mailbox.get("forwarding_enabled", False)),
        "forward_to": mailbox.get("forward_to"),
        "forward_to_verified": bool(mailbox.get("forward_to_verified", False)),
        "unread_count": 0,
    }


def _not_provisioned() -> Dict[str, Any]:
    """The spec §5 GET shape for a user who has no mailbox yet."""
    return {
        "provisioned": False,
        "address": None,
        "mode": "approval",
        "paused": False,
        "forwarding_enabled": False,
        "forward_to": None,
        "forward_to_verified": False,
        "unread_count": 0,
    }


# ---------------------------------------------------------------------------
# Alias minting (immutable primary — reused by provision + rotate)
# ---------------------------------------------------------------------------

def _mint_primary_alias(
    *,
    mailbox_id: str,
    user_email: str,
    base: str,
    domain: str,
    kind: str,
    alias_id: str,
) -> Dict[str, Any]:
    """Mint a fresh ACTIVE primary alias with the given (pre-generated)
    ``alias_id``, resolving collisions per D2 across ALL alias states (the
    tombstone-spanning uniqueness). The DB unique index on ``local_part`` is the
    authority: the in-code ``find_one`` is the fast path, and a racing
    ``DuplicateKeyError`` just advances to the next candidate."""
    for local in _candidate_locals(base):
        if _aliases().find_one({"local_part": local}):
            continue
        address = f"{local}@{domain}" if domain else local
        try:
            doc = {
                "alias_id": alias_id,
                "mailbox_id": mailbox_id,
                "user_email": user_email,
                "local_part": local,
                "address": address,
                "kind": kind,
                "state": "active",
                "thread_id": None,
                "created_at": _now(),
            }
            _aliases().insert_one(doc)
            return doc
        except DuplicateKeyError:
            # A concurrent mint took this local (or already minted this
            # alias_id for us). If the alias_id is now present, adopt it;
            # otherwise advance to the next candidate.
            won = _aliases().find_one({"alias_id": alias_id})
            if won:
                return won
            continue
    raise RuntimeError("failed to mint a unique managed alias")


# ---------------------------------------------------------------------------
# Provisioning (idempotent per user)
# ---------------------------------------------------------------------------

def provision(
    user_email: str,
    display_name: Optional[str] = None,
    privacy_alias: bool = False,
) -> Dict[str, Any]:
    """Create (or return the existing) managed mailbox + immutable primary alias
    for a user. Idempotent per user.

    ``user_email`` is the OWNER (D7) — the route resolves it from the JWT and it
    is NEVER taken from a request body. ``display_name`` seeds the readable local
    part; ``privacy_alias=True`` mints an opaque ``career.<6-hex>`` instead.

    Returns the mailbox object in the spec §5 GET shape.
    """
    email = InputSanitizer.sanitize_email(user_email)
    if not email:
        raise ValueError("invalid_user_email")

    domain = apply_mail_domain()

    mailbox = _mailboxes().find_one({"user_email": email})
    if mailbox is None:
        # Insert the mailbox FIRST, guarded by the unique ``user_email`` index:
        # under a concurrent provision only ONE container wins, so at most one
        # container ever proceeds to mint an alias (no orphan aliases).
        mailbox_id = "mbx_" + secrets.token_hex(8)
        alias_id = "alias_" + secrets.token_hex(8)
        now = _now()
        mailbox = {
            "user_email": email,
            "mailbox_id": mailbox_id,
            "primary_alias_id": alias_id,
            "mode": "approval",
            "paused": False,
            "forwarding_enabled": False,
            "forward_to": None,
            "forward_to_verified": False,
            "created_at": now,
            "updated_at": now,
        }
        try:
            _mailboxes().insert_one(mailbox)
        except DuplicateKeyError:
            # Concurrent provision won the race for this user — return theirs.
            mailbox = _mailboxes().find_one({"user_email": email})
            if mailbox is None:  # pragma: no cover - defensive
                raise

    # Ensure the immutable primary alias exists. Fresh provisions mint it here;
    # a mailbox that somehow lost its alias (a crash between the two inserts) is
    # repaired using its already-reserved primary_alias_id.
    if not _aliases().find_one({"alias_id": mailbox["primary_alias_id"]}):
        base = _privacy_local() if privacy_alias else (_slug_name(display_name) or _privacy_local())
        _mint_primary_alias(
            mailbox_id=mailbox["mailbox_id"],
            user_email=email,
            base=base,
            domain=domain,
            kind="privacy" if privacy_alias else "primary",
            alias_id=mailbox["primary_alias_id"],
        )
        mailbox = _mailboxes().find_one({"user_email": email})

    return _serialize_mailbox(mailbox)


# ---------------------------------------------------------------------------
# Resolution (webhook-facing — hostile input)
# ---------------------------------------------------------------------------

def resolve_alias(envelope_to_or_local: str) -> Optional[Dict[str, Any]]:
    """Resolve an envelope recipient (or bare local part) to its owner, or None.

    Only ``active``/``paused`` aliases resolve to an owner for delivery; a
    ``retired`` alias is a tombstone and returns None (its local part is reserved
    forever but can never receive again). Unknown -> None. Input is treated as
    fully untrusted (this runs on webhook data): it is normalized to the minted
    local-part alphabet before it ever reaches a Mongo filter, so injection input
    like ``$ne`` simply fails to match."""
    local = _normalize_inbound_local(envelope_to_or_local)
    if not local:
        return None
    doc = _aliases().find_one({"local_part": local})
    if not doc:
        return None
    state = doc.get("state")
    if state not in ("active", "paused"):
        return None  # retired tombstone — never reveal an owner
    return {
        "user_email": doc.get("user_email"),
        "mailbox_id": doc.get("mailbox_id"),
        "alias_id": doc.get("alias_id"),
        "state": state,
    }


# ---------------------------------------------------------------------------
# Controls (all owner-scoped, JWT-derived caller)
# ---------------------------------------------------------------------------

def get_mailbox(user_email: str) -> Dict[str, Any]:
    """Return the user's mailbox in the spec §5 GET shape, or a not-provisioned
    stub. Owner-scoped: another user's mailbox is never returned."""
    email = InputSanitizer.sanitize_email(user_email)
    if not email:
        return _not_provisioned()
    mailbox = _mailboxes().find_one({"user_email": email})
    if not mailbox:
        return _not_provisioned()
    return _serialize_mailbox(mailbox)


def _require_mailbox(email: str) -> Dict[str, Any]:
    mailbox = _mailboxes().find_one({"user_email": email})
    if not mailbox:
        raise LookupError("mailbox_not_provisioned")
    return mailbox


def set_paused(user_email: str, paused: bool) -> Dict[str, Any]:
    """Pause/resume acting on the mailbox. Owner-scoped."""
    email = InputSanitizer.sanitize_email(user_email)
    if not email:
        raise ValueError("invalid_user_email")
    _require_mailbox(email)
    _mailboxes().update_one(
        {"user_email": email},
        {"$set": {"paused": bool(paused), "updated_at": _now()}},
    )
    return _serialize_mailbox(_mailboxes().find_one({"user_email": email}))


def set_forwarding(
    user_email: str,
    enabled: bool,
    forward_to: Optional[str] = None,
) -> Dict[str, Any]:
    """Toggle forwarding and/or (re)set the forwarding destination. Changing
    ``forward_to`` ALWAYS resets ``forward_to_verified`` to False — the new
    destination must be re-verified before any content is forwarded (spec §1.2).
    Owner-scoped."""
    email = InputSanitizer.sanitize_email(user_email)
    if not email:
        raise ValueError("invalid_user_email")
    mailbox = _require_mailbox(email)

    updates: Dict[str, Any] = {"forwarding_enabled": bool(enabled), "updated_at": _now()}
    if forward_to is not None:
        target = InputSanitizer.sanitize_email(forward_to)
        if not target:
            raise ValueError("invalid_forward_to")
        if target != mailbox.get("forward_to"):
            updates["forward_to"] = target
            updates["forward_to_verified"] = False  # re-verify the new destination

    _mailboxes().update_one({"user_email": email}, {"$set": updates})
    return _serialize_mailbox(_mailboxes().find_one({"user_email": email}))


# ---------------------------------------------------------------------------
# forward_to verification (mint token -> emailed link -> confirm)
# ---------------------------------------------------------------------------

def _fv_expired(expires_at: Any) -> bool:
    """Tolerant expiry check (mongomock may hand back a tz-naive datetime)."""
    if expires_at is None:
        return False
    now = _now()
    try:
        return expires_at <= now
    except TypeError:
        e = expires_at.replace(tzinfo=None) if hasattr(expires_at, "replace") else expires_at
        return e <= now.replace(tzinfo=None)


def mint_forward_verification(user_email: str, forward_to: str) -> str:
    """Mint an opaque, short-TTL, single-use verification token bound to
    ``{user_email, forward_to}``. The caller emails the user a link carrying this
    token; ``confirm_forward_verification`` consumes it. Owner-scoped: the token
    is bound to the resolved user (never a request-body identity)."""
    email = InputSanitizer.sanitize_email(user_email)
    if not email:
        raise ValueError("invalid_user_email")
    target = InputSanitizer.sanitize_email(forward_to)
    if not target:
        raise ValueError("invalid_forward_to")

    token = "fv_" + secrets.token_hex(24)
    now = _now()
    _forward_verifications().insert_one(
        {
            "token": token,
            "user_email": email,
            "forward_to": target,
            "used": False,
            "created_at": now,
            "expires_at": now + timedelta(seconds=_FORWARD_VERIFY_TTL_SECONDS),
        }
    )
    return token


def confirm_forward_verification(user_email: str, token: str) -> Dict[str, Any]:
    """Consume a verification token and set ``forward_to_verified=True`` — but
    ONLY if the token is this user's, unused, unexpired, and its ``forward_to``
    still equals the mailbox's CURRENT ``forward_to`` (so a token minted for an
    OLD destination the user has since changed is rejected).

    Owner-scoped by ``user_email`` (IDOR: user A's token can never verify user B).
    Raises ``LookupError`` (not found / wrong owner / no mailbox) or ``ValueError``
    (used / expired / destination changed / bad input). The route maps these to
    404/400/409 — NEVER 401."""
    email = InputSanitizer.sanitize_email(user_email)
    if not email:
        raise ValueError("invalid_user_email")
    tok = InputSanitizer.sanitize_string(str(token or ""), 128)
    if not tok:
        raise ValueError("invalid_token")

    mailbox = _require_mailbox(email)

    doc = _forward_verifications().find_one({"token": tok})
    # Owner-scope the lookup result: a token for another user is "not found"
    # (never reveal it exists — IDOR defense).
    if not doc or doc.get("user_email") != email:
        raise LookupError("verification_not_found")
    if doc.get("used"):
        raise ValueError("verification_consumed")
    if _fv_expired(doc.get("expires_at")):
        raise ValueError("verification_expired")

    current = mailbox.get("forward_to")
    if not current or doc.get("forward_to") != current:
        # The destination changed since the token was minted — reject and let the
        # user re-request verification for the new address.
        raise ValueError("forward_to_changed")

    # Atomically consume the token (one-use); a concurrent claim loses here.
    consumed = _forward_verifications().find_one_and_update(
        {"token": tok, "used": False},
        {"$set": {"used": True, "used_at": _now()}},
    )
    if not consumed:
        raise ValueError("verification_consumed")

    # Verify only while forward_to is still the confirmed value (guards a race
    # where forward_to changed between the check and the write).
    _mailboxes().update_one(
        {"user_email": email, "forward_to": current},
        {"$set": {"forward_to_verified": True, "updated_at": _now()}},
    )
    return _serialize_mailbox(_mailboxes().find_one({"user_email": email}))


def rotate(user_email: str, display_name: Optional[str] = None) -> Dict[str, Any]:
    """Rotate the primary alias: retire the current one (``state="retired"`` +
    ``retired_at``) and mint a NEW primary. The old local part stays uniquely
    reserved forever (§1.3 tombstone), so it can never be recycled to another
    user. Owner-scoped."""
    email = InputSanitizer.sanitize_email(user_email)
    if not email:
        raise ValueError("invalid_user_email")
    mailbox = _require_mailbox(email)

    old_alias_id = mailbox.get("primary_alias_id")
    if old_alias_id:
        _aliases().update_one(
            {"alias_id": old_alias_id, "user_email": email},
            {"$set": {"state": "retired", "retired_at": _now()}},
        )

    domain = apply_mail_domain()
    base = _slug_name(display_name) or _privacy_local()
    new_alias_id = "alias_" + secrets.token_hex(8)
    new_alias = _mint_primary_alias(
        mailbox_id=mailbox["mailbox_id"],
        user_email=email,
        base=base,
        domain=domain,
        kind="primary",
        alias_id=new_alias_id,
    )
    _mailboxes().update_one(
        {"user_email": email},
        {"$set": {"primary_alias_id": new_alias["alias_id"], "updated_at": _now()}},
    )
    return _serialize_mailbox(_mailboxes().find_one({"user_email": email}))
