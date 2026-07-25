"""Managed Application Mailbox — inbound storage pipeline (managed-mailbox-v1 §2/§3/§6/§8).

This is the second half of the "apply from a managed address" feature: once
``managed_mailbox_service.resolve_alias`` has bound an envelope recipient to an
owner, THIS module takes the raw MIME the Cloudflare Worker quarantined in S3
and turns it into durable, queryable inbox state — threads, messages, and
attachments — while enforcing every fail-closed limit in the spec's §6.

Trust model (a recruiter/employer email is FULLY UNTRUSTED — it may be forged,
oversized, a zip bomb, a polyglot file, or an XSS payload):
  - The owner is ALWAYS the caller-supplied ``alias_info`` resolved from the
    envelope by ``resolve_alias`` — NEVER a name/address parsed from the body
    (D7 / IDOR defense). Every Mongo filter is owner-scoped and every untrusted
    value is sanitized with ``InputSanitizer`` before it reaches a filter
    (invariant #4, NoSQL-injection).
  - Raw MIME / base64 NEVER lands in Mongo (D6): Mongo holds only S3 keys +
    metadata; bodies/attachments live in the SSE-KMS ``APPLY_MAIL_BUCKET``.
  - Structural abuse (oversize, too many parts/headers, too deep, decoded-size
    blow-up) fail-closes: ``ingest_committed`` raises ``MailRejected`` and the
    message is NOT stored (the route maps it to 422).
  - Attachment abuse (magic-byte mismatch, disallowed type, encrypted/nested
    archive, zip-bomb expansion ratio) does NOT reject the whole message — the
    message is still visible in the Inbox, but the offending attachment is
    isolated (``scan_verdict`` "suspicious"/"blocked", ``quarantined=True``, no
    downloadable key) so it can never be handed back to the user.
  - HTML is sanitized SERVER-SIDE (script/style/iframe/forms/event-handlers
    stripped, remote images + javascript:/data: URLs neutralized) before it is
    stored; the FE sanitizes again.
  - SPF/DKIM/DMARC are read as EVIDENCE (pass/fail/none), never as identity
    proof. Correlation (§8) is the guessable-address defense: a message is
    ``verified`` only when it maps to an application the user actually submitted
    before the mail arrived; ``unverified`` mail NEVER auto-transitions an
    application's status in this pipeline (it only sets trust + a suggestion).

The Flask routes, the two-phase Cloudflare commit-token handshake, and the
forwarding send live in ``blueprints/mail_inbound.py`` and later tasks;
``maybe_forward`` here is an intentional no-op stub (forwarding is next).
"""

from __future__ import annotations

import io
import logging
import re
import secrets
import zipfile
from datetime import datetime, timedelta, timezone
from email import message_from_bytes
from typing import Any, Dict, List, Optional, Tuple

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from utils.config import _get_config_value
from utils.db_connect import DBConnect
from utils.security import InputSanitizer

# Reuse the untrusted-input helpers from the legacy ingress service (spec §9:
# "reused verbatim") rather than re-implementing them — same hardening.
from services.mail_ingress_service import _parse_address, _domain_of, _sha256

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# §6 limits (config-driven; defaults are the spec values). Read per-call via
# ``_get_config_value`` so a deployment (or a test) can tighten them without a
# code change — e.g. lower MAIL_MAX_PARTS to prove part-count enforcement.
# ---------------------------------------------------------------------------

_MiB = 1024 * 1024

_LIMIT_DEFAULTS = {
    "MAIL_MAX_RAW_BYTES": 25 * _MiB,
    "MAIL_MAX_HEADERS": 200,
    "MAIL_MAX_PARTS": 50,
    "MAIL_MAX_DEPTH": 10,
    "MAIL_MAX_DECODED_BYTES": 40 * _MiB,
    "MAIL_MAX_ATTACHMENTS": 20,
    "MAIL_MAX_ATTACHMENT_BYTES": 15 * _MiB,
    # Uncompressed/compressed ratio above which an archive entry is a zip bomb.
    "MAIL_MAX_ZIP_RATIO": 100,
}


def _limit(key: str) -> int:
    """A §6 limit as an int, config-overridable, falling back to the spec default."""
    raw = _get_config_value(key, None)
    if raw is None or str(raw).strip() == "":
        return int(_LIMIT_DEFAULTS[key])
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return int(_LIMIT_DEFAULTS[key])


# Cap classifier/thread-grouping subject length; keep a bounded text budget.
_MAX_SUBJECT = 400

# Commit-token TTL (spec §4: ~15 min). The token is a one-use capability that
# binds a presigned PUT to a resolved owner + message id.
_COMMIT_TOKEN_TTL_SECONDS = 15 * 60


# ---------------------------------------------------------------------------
# File-signature (magic-byte) allow-list. We validate CONTENT, not extension
# (§6): a ``report.pdf`` whose bytes are a PNG is a mismatch -> suspicious.
# ---------------------------------------------------------------------------

# family -> canonical content type we record as `sniffed_content_type`.
_FAMILY_CT = {
    "pdf": "application/pdf",
    "png": "image/png",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "zip": "application/zip",  # also every OOXML doc (docx/xlsx/pptx)
    "ole": "application/x-ole-storage",  # legacy .doc/.xls/.ppt
    "unknown": "application/octet-stream",
}

# Archive families need structural inspection before they can ever be "clean".
_ARCHIVE_FAMILIES = {"zip"}


class MailRejected(Exception):
    """Raised when a message violates a STRUCTURAL §6 limit (oversize / too many
    parts or headers / too deep / decoded-size blow-up). The whole message is
    rejected and never stored; the route maps this to 422 (never 401)."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


# ---------------------------------------------------------------------------
# Collections (created on first use)
# ---------------------------------------------------------------------------

def _db():
    return DBConnect().get_db()


def _threads():
    return _db().mail_threads


def _messages():
    return _db().mail_messages


def _attachments():
    return _db().mail_attachments


def _commit_tokens():
    return _db().mail_commit_tokens


def ensure_indexes() -> None:
    """Create the collections' indexes (managed-mailbox-v1 §2). Call from
    scripts/migrate_indexes.py — NOT at import time (cold-start budget). Safe to
    re-run; names/keys mirror the migration specs so a re-assert is a no-op."""
    _threads().create_index("thread_id", unique=True, name="uniq_thread_id")
    _threads().create_index(
        [("user_email", 1), ("last_message_at", -1)], name="user_last_message_at"
    )
    _threads().create_index(
        [("user_email", 1), ("application_id", 1)], name="user_application_id"
    )
    # Idempotency backstop (spec §2/D8): one message per (alias, provider id).
    _messages().create_index(
        [("alias_id", 1), ("provider_message_id", 1)],
        unique=True,
        name="uniq_alias_provider_message_id",
    )
    _messages().create_index("message_id", unique=True, name="uniq_message_id")
    _messages().create_index("thread_id", name="by_thread_id")
    _messages().create_index([("user_email", 1), ("read", 1)], name="user_read")
    _attachments().create_index("attachment_id", unique=True, name="uniq_attachment_id")
    _attachments().create_index("message_id", name="by_message_id")
    # Commit tokens: one-use, TTL-reaped (~15 min).
    _commit_tokens().create_index("commit_token", unique=True, name="uniq_commit_token")
    _commit_tokens().create_index(
        "expires_at", expireAfterSeconds=0, name="ttl_expires_at"
    )


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _san(value: str, n: int = 320) -> str:
    """Sanitize an id/value before it reaches a Mongo filter (invariant #4)."""
    return InputSanitizer.sanitize_string(str(value or ""), n)


def _apply_mail_bucket() -> str:
    return (_get_config_value("APPLY_MAIL_BUCKET", "") or "").strip()


def _kms_key_id() -> Optional[str]:
    return (_get_config_value("APPLY_MAIL_KMS_KEY_ID", "") or "").strip() or None


def get_storage_service():
    """Lazy accessor for the S3 storage singleton (imported on first use, not at
    module import, to keep the cold-start budget lean). Resolving through this
    thin wrapper each call also keeps the boundary patchable in tests."""
    from services.s3_service import get_storage_service as _get
    return _get()


def _safe_key_component(value: str) -> str:
    """Coerce an untrusted string (a provider Message-ID, a filename) into a safe
    S3 key path segment: strip angle brackets, keep a conservative alphabet."""
    v = str(value or "").strip().strip("<>")
    v = re.sub(r"[^A-Za-z0-9._@\-]", "_", v)
    return v[:200] or "nomsgid"


def raw_key_for(user_email: str, message_id: str) -> str:
    """Deterministic raw-MIME quarantine key (§3): ``mail/raw/{hash}/{id}.eml``.

    Used BOTH by the slot route (to sign the presigned PUT the Worker uploads to)
    and by ``ingest_committed`` (to record where the raw lives) — so both sides
    agree on the exact object without threading the key through the Worker."""
    email = InputSanitizer.sanitize_email(user_email) or str(user_email or "")
    return f"mail/raw/{_sha256(email)}/{_safe_key_component(message_id)}.eml"


def _norm_subject(subject: str) -> str:
    """Normalize a subject for thread grouping: strip leading Re:/Fwd: markers,
    lowercase, collapse whitespace."""
    s = (subject or "").strip()
    while True:
        stripped = re.sub(r"^(re|fwd|fw)\s*:\s*", "", s, flags=re.IGNORECASE)
        if stripped == s:
            break
        s = stripped
    return re.sub(r"\s+", " ", s).strip().lower()


# ---------------------------------------------------------------------------
# MIME parsing (extends the ingress parser to capture attachments + limits)
# ---------------------------------------------------------------------------

def _decode_text(part, payload: Optional[bytes]) -> str:
    if payload is None:
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except (LookupError, ValueError):
        return payload.decode("utf-8", errors="replace")


def _walk(part, depth: int, ctx: Dict[str, Any]) -> None:
    """Recursively walk the MIME tree, enforcing structural §6 limits AS WE GO
    (so a hostile message is rejected before we ever buffer it all), and
    collecting text / html / attachment leaves."""
    ctx["parts"] += 1
    ctx["headers"] += len(part.keys())
    if ctx["parts"] > ctx["max_parts"]:
        raise MailRejected("too_many_parts")
    if depth > ctx["max_depth"]:
        raise MailRejected("mime_too_deep")
    if ctx["headers"] > ctx["max_headers"]:
        raise MailRejected("too_many_headers")

    if part.is_multipart():
        for sub in part.get_payload():
            _walk(sub, depth + 1, ctx)
        return

    ctype = (part.get_content_type() or "").lower()
    filename = part.get_filename()
    disposition = (part.get("Content-Disposition") or "").lower()

    try:
        payload = part.get_payload(decode=True)
    except Exception:  # malformed encoding — treat as empty, don't crash
        payload = None

    if payload:
        ctx["decoded"] += len(payload)
        if ctx["decoded"] > ctx["max_decoded"]:
            raise MailRejected("decoded_too_large")

    is_attachment = ("attachment" in disposition) or bool(filename)
    if is_attachment:
        ctx["attachments"].append(
            {
                "filename": filename or "attachment",
                "declared_content_type": ctype or "application/octet-stream",
                "data": payload or b"",
            }
        )
    elif ctype == "text/plain":
        ctx["text_parts"].append(_decode_text(part, payload))
    elif ctype == "text/html":
        ctx["html_parts"].append(_decode_text(part, payload))
    else:
        # Inline non-text without a filename (e.g. an inline image): quarantine
        # it as an attachment so nothing untrusted is ever inlined into a body.
        if payload:
            ctx["attachments"].append(
                {
                    "filename": filename or "inline",
                    "declared_content_type": ctype or "application/octet-stream",
                    "data": payload,
                }
            )


def _parse_auth_results(msg) -> Tuple[str, str, str]:
    """Read SPF/DKIM/DMARC verdicts from ``Authentication-Results`` /
    ``Received-SPF`` as EVIDENCE (pass/fail/none) — never identity proof."""
    blob = " ".join(
        str(v) for v in (
            msg.get_all("Authentication-Results") or []
        ) + (msg.get_all("Received-SPF") or [])
    ).lower()

    def _verdict(mech: str) -> str:
        m = re.search(mech + r"\s*[=:]\s*(pass|fail|none|neutral|softfail|permerror|temperror)", blob)
        if not m:
            return "none"
        v = m.group(1)
        if v == "pass":
            return "pass"
        if v in ("fail", "softfail", "permerror", "temperror"):
            return "fail"
        return "none"

    # Received-SPF header starts with the bare verdict (e.g. "Received-SPF: pass").
    spf = _verdict("spf")
    if spf == "none":
        rspf = " ".join(str(v) for v in (msg.get_all("Received-SPF") or [])).lower().strip()
        if rspf.startswith("pass"):
            spf = "pass"
        elif rspf.startswith(("fail", "softfail")):
            spf = "fail"
    return spf, _verdict("dkim"), _verdict("dmarc")


def _parse_mime(raw_bytes: bytes) -> Dict[str, Any]:
    """Parse raw RFC-822 MIME into everything the inbox needs, enforcing the
    structural §6 limits. Raises ``MailRejected`` on a structural violation."""
    msg = message_from_bytes(raw_bytes)

    ctx: Dict[str, Any] = {
        "parts": 0,
        "headers": 0,
        "decoded": 0,
        "max_parts": _limit("MAIL_MAX_PARTS"),
        "max_depth": _limit("MAIL_MAX_DEPTH"),
        "max_headers": _limit("MAIL_MAX_HEADERS"),
        "max_decoded": _limit("MAIL_MAX_DECODED_BYTES"),
        "text_parts": [],
        "html_parts": [],
        "attachments": [],
    }
    _walk(msg, 0, ctx)

    from_name, from_addr = _parse_address(msg.get("From") or "")
    text = "\n".join(t for t in ctx["text_parts"] if t).strip()
    html = "\n".join(h for h in ctx["html_parts"] if h).strip()
    spf, dkim, dmarc = _parse_auth_results(msg)

    return {
        "subject": (msg.get("Subject") or "").strip()[:_MAX_SUBJECT],
        "from_name": from_name,
        "from_addr": from_addr,
        "from_domain": _domain_of(from_addr),
        "text": text,
        "html": html,
        "attachments": ctx["attachments"],
        "part_count": ctx["parts"],
        "spf": spf,
        "dkim": dkim,
        "dmarc": dmarc,
        # Loop marker: an inbound message that already carries our forwarding
        # header must never be re-forwarded (open-relay / mail-loop guard). We
        # capture presence only (a bool) — the value is never trusted. Absent ->
        # not forwarded.
        "x_jp_forwarded": bool(msg.get("X-JP-Forwarded")),
    }


# ---------------------------------------------------------------------------
# Attachment validation (magic bytes + archive/zip-bomb inspection)
# ---------------------------------------------------------------------------

def _sniff_family(data: bytes) -> str:
    """Identify a file by its SIGNATURE (magic bytes), not its extension."""
    if not data:
        return "unknown"
    if data[:5] == b"%PDF-":
        return "pdf"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"PK\x03\x04":
        return "zip"
    if data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        return "ole"
    return "unknown"


def _declared_family(declared_ct: str, filename: str) -> str:
    """Best-effort family from the DECLARED content type / extension (only used
    to detect a signature MISMATCH, never trusted on its own)."""
    ct = (declared_ct or "").lower()
    name = (filename or "").lower()
    if "pdf" in ct or name.endswith(".pdf"):
        return "pdf"
    if "png" in ct or name.endswith(".png"):
        return "png"
    if "jpeg" in ct or "jpg" in ct or name.endswith((".jpg", ".jpeg")):
        return "jpeg"
    if "gif" in ct or name.endswith(".gif"):
        return "gif"
    if name.endswith((".docx", ".xlsx", ".pptx")) or "officedocument" in ct or "zip" in ct:
        return "zip"
    if name.endswith((".doc", ".xls", ".ppt")) or ct == "application/msword":
        return "ole"
    return ""


def _inspect_zip(data: bytes) -> str:
    """Return a verdict for a ZIP/OOXML payload: ``clean`` only if it is not
    encrypted, contains no nested archive, and no entry (or the total) exceeds
    the §6 expansion caps. Any parse error is ``suspicious`` (fail-closed)."""
    ratio_cap = _limit("MAIL_MAX_ZIP_RATIO")
    max_total = _limit("MAIL_MAX_DECODED_BYTES")
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
        total = 0
        for zi in zf.infolist():
            if zi.flag_bits & 0x1:  # encrypted entry
                return "blocked"
            inner = zi.filename.lower()
            if inner.endswith((".zip", ".gz", ".rar", ".7z", ".tar", ".bz2", ".xz")):
                return "blocked"  # nested archive
            if zi.compress_size and (zi.file_size / zi.compress_size) > ratio_cap:
                return "blocked"  # zip bomb
            total += zi.file_size
            if total > max_total:
                return "blocked"
        return "clean"
    except Exception:
        return "suspicious"


def _scan_attachment(filename: str, declared_ct: str, data: bytes) -> Tuple[str, str]:
    """Validate one attachment. Returns ``(sniffed_content_type, verdict)`` where
    verdict is ``clean`` | ``suspicious`` | ``blocked``. Anything not ``clean``
    is quarantined (download blocked)."""
    if len(data) > _limit("MAIL_MAX_ATTACHMENT_BYTES"):
        return _FAMILY_CT["unknown"], "blocked"

    family = _sniff_family(data)
    sniffed_ct = _FAMILY_CT.get(family, _FAMILY_CT["unknown"])

    if family == "unknown":
        # No recognized signature -> we cannot vouch for it. Isolate.
        return sniffed_ct, "suspicious"

    if family in _ARCHIVE_FAMILIES:
        verdict = _inspect_zip(data)
        declared = _declared_family(declared_ct, filename)
        # Content is a real zip; if it was DECLARED as something non-archive
        # (e.g. a .pdf that is actually a zip), that polyglot is suspicious.
        if verdict == "clean" and declared and declared not in ("zip",):
            return sniffed_ct, "suspicious"
        return sniffed_ct, verdict

    declared = _declared_family(declared_ct, filename)
    if declared and declared != family:
        # Extension/type lies about the content (report.pdf that is a PNG).
        return sniffed_ct, "suspicious"
    return sniffed_ct, "clean"


# ---------------------------------------------------------------------------
# HTML sanitization (server-side; the FE sanitizes again)
# ---------------------------------------------------------------------------

_DROP_TAGS = {
    "script", "style", "iframe", "form", "object", "embed", "link", "meta",
    "base", "noscript", "frame", "frameset", "applet", "svg", "math",
}
_URL_ATTRS = {"href", "src", "action", "background", "formaction", "xlink:href", "poster"}
_DANGEROUS_URL = re.compile(r"^\s*(javascript|data|vbscript):", re.IGNORECASE)
_REMOTE_URL = re.compile(r"^\s*https?://", re.IGNORECASE)


def _sanitize_html(raw_html: str) -> str:
    """Strip script/style/iframe/forms/event-handlers, neutralize javascript:/
    data: URLs, and turn OFF remote images. Falls back to tag-stripping if lxml
    is unavailable so a message is never lost to a parser error."""
    if not raw_html:
        return ""
    try:
        from lxml import html as lxml_html
        root = lxml_html.fromstring(raw_html)
    except Exception:
        return re.sub(r"<[^>]+>", " ", raw_html)

    # 1. Drop dangerous elements wholesale (with their subtrees).
    xpath = "|".join("//" + t for t in _DROP_TAGS)
    for el in root.xpath(xpath):
        parent = el.getparent()
        if parent is not None:
            parent.remove(el)

    # 2. Scrub attributes on everything that remains.
    for el in root.iter():
        tag = el.tag if isinstance(el.tag, str) else None
        if tag is None:
            continue
        tag = tag.lower()
        for name in list(el.attrib.keys()):
            low = name.lower()
            val = el.attrib.get(name, "")
            if low.startswith("on"):
                del el.attrib[name]  # event handler
                continue
            if low == "style":
                del el.attrib[name]  # inline style can carry url(javascript:)
                continue
            if low in _URL_ATTRS:
                if _DANGEROUS_URL.match(val or ""):
                    del el.attrib[name]
                    continue
                if tag == "img" and low == "src" and _REMOTE_URL.match(val or ""):
                    # Remote images OFF by default (tracking-pixel + privacy).
                    del el.attrib[name]
                    el.attrib["data-blocked-src"] = "1"

    try:
        from lxml.html import tostring
        return tostring(root, encoding="unicode")
    except Exception:
        return re.sub(r"<[^>]+>", " ", raw_html)


def _text_from_html(html: str) -> str:
    if not html:
        return ""
    try:
        from lxml import html as lxml_html
        return re.sub(r"\s+", " ", lxml_html.fromstring(html).text_content()).strip()
    except Exception:
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


# ---------------------------------------------------------------------------
# Commit-token store (two-phase Cloudflare handshake — §4)
# ---------------------------------------------------------------------------

def mint_commit_token(
    *,
    alias_info: Dict[str, Any],
    envelope_to: str,
    envelope_from: str,
    message_id: str,
    raw_s3_key: str,
    done: bool = False,
) -> str:
    """Mint a one-use, short-TTL commit token binding a presigned PUT to the
    RESOLVED owner + message id (never a request-body identity). ``done=True``
    marks an already-seen message so the commit is a no-op (idempotency)."""
    token = "ct_" + secrets.token_hex(24)
    _commit_tokens().insert_one(
        {
            "commit_token": token,
            "alias_id": alias_info.get("alias_id"),
            "mailbox_id": alias_info.get("mailbox_id"),
            "user_email": alias_info.get("user_email"),
            "envelope_to": envelope_to,
            "envelope_from": envelope_from,
            "message_id": message_id,
            "raw_s3_key": raw_s3_key,
            "used": False,
            "done": bool(done),
            "created_at": _now(),
            "expires_at": _now() + timedelta(seconds=_COMMIT_TOKEN_TTL_SECONDS),
        }
    )
    return token


def claim_commit_token(commit_token: str) -> Optional[Dict[str, Any]]:
    """Atomically flip a token to used exactly once. Returns the token doc, or
    None if it is missing / already used / expired (the route maps None -> 409)."""
    tok = _san(commit_token, 128)
    if not tok:
        return None
    doc = _commit_tokens().find_one_and_update(
        {"commit_token": tok, "used": False},
        {"$set": {"used": True, "used_at": _now()}},
        return_document=ReturnDocument.AFTER,
    )
    if not doc:
        return None
    expires_at = doc.get("expires_at")
    # mongomock (and some drivers) can hand back a tz-naive datetime; compare
    # tolerantly so a naive/aware mismatch never crashes the claim.
    if expires_at is not None and _before(expires_at, _now()):
        return None  # expired between mint and claim (TTL may lag)
    return doc


# ---------------------------------------------------------------------------
# Inbound ingest rate limit (P1-RATE-IN — spam/flood backstop)
#
# A per-alias (daily + hourly) AND per-sender-domain (hourly) atomic counter,
# using the SAME bucketed ``$inc`` idiom as
# ``mail_forward_service._within_daily_cap``. Concurrent Lambdas are safe (each
# window bucket is a single atomic increment). This is a spam/flood backstop,
# NOT a product quota — the HMAC + alias resolution are the hard gates; this
# caps how much a single alias or a single hostile sender-domain can push
# through the ingest pipeline. Best-effort: a counter-store error never blocks a
# legitimate message (fail-open, like the forward cap).
#
# Caps are config-overridable; the RECOMMENDED defaults below are generous
# enough that no real recruiter thread trips them, but low enough to blunt a
# flood. Call ONLY for a genuinely NEW message — an idempotent replay (same
# provider_message_id already stored) must NOT consume budget (dedup first).
# ---------------------------------------------------------------------------

_INBOUND_RATE_DEFAULTS = {
    "MAIL_INBOUND_ALIAS_DAILY_CAP": 200,    # per managed alias, per day
    "MAIL_INBOUND_ALIAS_HOURLY_CAP": 30,    # per managed alias, per hour (burst)
    "MAIL_INBOUND_DOMAIN_HOURLY_CAP": 500,  # per sender-domain, global, per hour
}


def _rate_cap(key: str) -> int:
    """An inbound rate cap as a non-negative int, config-overridable, falling
    back to the RECOMMENDED default."""
    raw = _get_config_value(key, None)
    if raw is None or str(raw).strip() == "":
        return int(_INBOUND_RATE_DEFAULTS[key])
    try:
        return max(0, int(str(raw).strip()))
    except (TypeError, ValueError):
        return int(_INBOUND_RATE_DEFAULTS[key])


def _rate_counters():
    return _db().mail_inbound_rate_counters


def _bump_bucket(scope: str, key: str, window: str) -> int:
    """Atomically increment one (scope, key, window) rate bucket and return the
    new count. A single ``$inc`` upsert — concurrent Lambdas are safe."""
    doc = _rate_counters().find_one_and_update(
        {"scope": scope, "key": key, "window": window},
        {"$inc": {"count": 1}, "$setOnInsert": {"created_at": _now()}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    return int(doc.get("count", 1))


def check_inbound_rate(alias_id: str, envelope_from: str) -> Tuple[bool, Optional[str]]:
    """Atomically account one inbound message against the per-alias (daily +
    hourly) and per-sender-domain (hourly) caps.

    Returns ``(allowed, reason)``. ``reason`` is a PII-free counter label
    (``alias_daily`` / ``alias_hourly`` / ``domain_hourly``) when a cap is
    exceeded, else None. NEVER logs / returns the alias, the sender, or any body
    content.

    Best-effort: a counter-store error returns ``(True, None)`` so a transient
    Mongo blip never rejects legitimate mail (the HMAC + alias gates already
    ran). Call ONLY for a genuinely new message (dedup pre-check first) so an
    idempotent replay does not consume budget."""
    now = _now()
    day = now.strftime("%Y-%m-%d")
    hour = now.strftime("%Y-%m-%dT%H")

    aid = _san(alias_id, 64)
    dom = re.sub(r"[^a-z0-9.\-]", "", _domain_of(envelope_from))[:255]

    try:
        if aid:
            if _bump_bucket("alias_day", aid, day) > _rate_cap("MAIL_INBOUND_ALIAS_DAILY_CAP"):
                return False, "alias_daily"
            if _bump_bucket("alias_hour", aid, hour) > _rate_cap("MAIL_INBOUND_ALIAS_HOURLY_CAP"):
                return False, "alias_hourly"
        if dom:
            if _bump_bucket("domain_hour", dom, hour) > _rate_cap("MAIL_INBOUND_DOMAIN_HOURLY_CAP"):
                return False, "domain_hourly"
    except Exception as e:  # pragma: no cover - best effort, never block on a blip
        logger.warning("mail-store: inbound rate check failed: %s", type(e).__name__)
        return True, None
    return True, None


# ---------------------------------------------------------------------------
# Correlation (§8 — the guessable-address defense)
# ---------------------------------------------------------------------------

def correlate(user_email: str, from_domain: str, received_at: datetime) -> Dict[str, Any]:
    """Bind an inbound message to a real application, EVIDENCE-only.

    Returns ``{trust, application_id, suggestion}``. ``trust`` is ``verified``
    ONLY when the sender's domain matches the employer/ATS domain of an
    application the user actually SUBMITTED before ``received_at``; otherwise
    ``unverified``.

    This function NEVER mutates application state — an ``unverified`` (or even a
    ``verified``) message only carries a suggestion; the tracker transition is a
    separate, code-gated decision made elsewhere (D8)."""
    result: Dict[str, Any] = {"trust": "unverified", "application_id": None, "suggestion": None}

    email = InputSanitizer.sanitize_email(user_email)
    if not email:
        return result
    dom = re.sub(r"[^a-z0-9.\-]", "", (from_domain or "").lower())[:255]
    if not dom:
        return result

    # Owner-scoped read; from_domain is compared in Python, never placed in the
    # Mongo filter (so it can never occupy an operator position).
    try:
        cursor = _db().tailoring_records.find(
            {"user_email": email},
            {
                "record_id": 1,
                "application": 1,
                "jd_analysis.company": 1,
                "_id": 0,
            },
        )
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("mail-store correlate read failed: %s", e)
        return result

    for rec in cursor:
        app = rec.get("application") or {}
        submitted = app.get("submitted_at") or app.get("applied_at")
        emp_dom = str(
            app.get("employer_domain") or app.get("ats_domain") or ""
        ).strip().lower()
        if not submitted or not emp_dom:
            continue
        if _before(submitted, received_at) and _domain_match(dom, emp_dom):
            result["trust"] = "verified"
            result["application_id"] = rec.get("record_id")
            result["suggestion"] = {
                "suggested": None,  # LLM classification (async) fills this
                "confidence": 0.0,
                "reason": "domain+timing correlated to a submitted application",
            }
            return result
    return result


def _before(submitted: Any, received_at: datetime) -> bool:
    try:
        return submitted <= received_at
    except TypeError:
        # Mixed tz-aware/naive — normalize both to naive UTC for the compare.
        s = submitted.replace(tzinfo=None) if hasattr(submitted, "replace") else submitted
        r = received_at.replace(tzinfo=None)
        return s <= r


def _domain_match(a: str, b: str) -> bool:
    """True if two domains are equal or one is a subdomain of the other."""
    if not a or not b:
        return False
    return a == b or a.endswith("." + b) or b.endswith("." + a)


# ---------------------------------------------------------------------------
# Ingest (the commit route's entry point — idempotent per (alias, message id))
# ---------------------------------------------------------------------------

def ingest_committed(
    alias_info: Dict[str, Any],
    envelope_to: str,
    envelope_from: str,
    raw_bytes: bytes,
    provider_message_id: str,
) -> Dict[str, Any]:
    """Turn quarantined raw MIME into durable inbox state.

    ``alias_info`` is the OWNER resolved from the envelope (D7) — never a body
    identity. Returns ``{thread_id, message_id, deduplicated}``. Raises
    ``MailRejected`` on a structural §6 violation (route -> 422)."""
    user_email = InputSanitizer.sanitize_email((alias_info or {}).get("user_email"))
    alias_id_raw = (alias_info or {}).get("alias_id")
    if not user_email or not alias_id_raw:
        raise MailRejected("unresolved_owner")

    alias_id = _san(alias_id_raw, 64)
    mailbox_id = (alias_info or {}).get("mailbox_id")
    pmid = _san(provider_message_id, 998)
    if not pmid:
        raise MailRejected("missing_message_id")

    if len(raw_bytes or b"") > _limit("MAIL_MAX_RAW_BYTES"):
        raise MailRejected("raw_too_large")

    # --- Idempotency pre-check on (alias_id, provider_message_id). -----------
    existing = _messages().find_one(
        {"alias_id": alias_id, "provider_message_id": pmid},
        {"_id": 0, "message_id": 1, "thread_id": 1},
    )
    if existing:
        return {
            "thread_id": existing.get("thread_id"),
            "message_id": existing.get("message_id"),
            "deduplicated": True,
        }

    # --- Parse + enforce structural limits (may raise MailRejected). --------
    parsed = _parse_mime(raw_bytes or b"")

    if len(parsed["attachments"]) > _limit("MAIL_MAX_ATTACHMENTS"):
        raise MailRejected("too_many_attachments")

    received_at = _now()
    message_id = "msg_" + secrets.token_hex(12)
    user_hash = _sha256(user_email)
    bucket = _apply_mail_bucket()
    kms = _kms_key_id()

    storage = get_storage_service()

    # --- Sanitize + store bodies to S3 (never raw MIME/base64 in Mongo). ----
    safe_html = _sanitize_html(parsed["html"])
    text = parsed["text"] or _text_from_html(safe_html)

    raw_s3_key = raw_key_for(user_email, pmid)
    text_s3_key = f"mail/text/{user_hash}/{message_id}.txt"
    storage.put_object_bytes(
        text_s3_key, text.encode("utf-8"), bucket=bucket,
        content_type="text/plain; charset=utf-8", kms_key_id=kms,
    )
    safe_html_s3_key = None
    if safe_html:
        safe_html_s3_key = f"mail/html/{user_hash}/{message_id}.sanitized.html"
        storage.put_object_bytes(
            safe_html_s3_key, safe_html.encode("utf-8"), bucket=bucket,
            content_type="text/html; charset=utf-8", kms_key_id=kms,
        )

    # --- Validate + (for clean files) store attachments. --------------------
    attachment_docs: List[Dict[str, Any]] = []
    for att in parsed["attachments"]:
        data = att["data"] or b""
        attachment_id = "att_" + secrets.token_hex(12)
        sniffed_ct, verdict = _scan_attachment(
            att["filename"], att["declared_content_type"], data
        )
        att_s3_key = None
        if verdict == "clean" and data:
            att_s3_key = (
                f"mail/att/{user_hash}/{attachment_id}/"
                f"{_safe_key_component(att['filename'])}"
            )
            storage.put_object_bytes(
                att_s3_key, data, bucket=bucket,
                content_type=sniffed_ct, kms_key_id=kms,
            )
        attachment_docs.append(
            {
                "attachment_id": attachment_id,
                "message_id": message_id,
                "user_email": user_email,
                "filename": InputSanitizer.sanitize_string(str(att["filename"]), 255),
                "declared_content_type": att["declared_content_type"][:150],
                "sniffed_content_type": sniffed_ct,
                "size_bytes": len(data),
                "sha256": _sha256_bytes(data),
                "s3_key": att_s3_key,
                # Anything not clean is isolated: no downloadable key, blocked.
                "quarantined": verdict != "clean",
                "scan_verdict": verdict,
                "created_at": received_at,
            }
        )

    # --- Correlate (evidence only; never mutates application status). -------
    corr = correlate(user_email, parsed["from_domain"], received_at)

    # --- Resolve the thread (group by subject + alias) WITHOUT touching it
    #     until the message insert succeeds (avoids orphan/double-count). -----
    subject_norm = _norm_subject(parsed["subject"])
    thread = _threads().find_one(
        {"user_email": user_email, "alias_id": alias_id, "subject_norm": subject_norm}
    )
    thread_id = thread["thread_id"] if thread else "thr_" + secrets.token_hex(12)

    message_doc = {
        "message_id": message_id,
        "provider_message_id": pmid,
        "thread_id": thread_id,
        "user_email": user_email,
        "alias_id": alias_id,
        "mailbox_id": mailbox_id,
        "direction": "inbound",
        "from_addr": parsed["from_addr"],
        "from_name": parsed["from_name"],
        "to_addr": (envelope_to or "").strip().lower(),
        "subject": parsed["subject"],
        "received_at": received_at,
        "spf": parsed["spf"],
        "dkim": parsed["dkim"],
        "dmarc": parsed["dmarc"],
        "raw_s3_key": raw_s3_key,
        "text_s3_key": text_s3_key,
        "safe_html_s3_key": safe_html_s3_key,
        "size_bytes": len(raw_bytes or b""),
        "part_count": parsed["part_count"],
        "attachment_ids": [a["attachment_id"] for a in attachment_docs],
        "read": False,
        "is_phishing_reported": False,
        "trust": corr["trust"],
        "classification": None,  # LLM suggestion is offloaded (async) — spec §4.2
        # Loop guard evidence for maybe_forward (never re-forward our own mail).
        "from_domain": parsed["from_domain"],
        "x_jp_forwarded": parsed["x_jp_forwarded"],
        "created_at": received_at,
    }

    # The unique (alias_id, provider_message_id) index is the race backstop: if a
    # concurrent commit beat us here, treat it as a dedup rather than double-store.
    try:
        _messages().insert_one(message_doc)
    except DuplicateKeyError:
        prior = _messages().find_one(
            {"alias_id": alias_id, "provider_message_id": pmid},
            {"_id": 0, "message_id": 1, "thread_id": 1},
        ) or {}
        return {
            "thread_id": prior.get("thread_id"),
            "message_id": prior.get("message_id"),
            "deduplicated": True,
        }

    # Denormalized inbox-list fields (so ThreadSummary needs no per-thread body
    # fetch — §5): a short preview snippet, the latest sender, and a sticky
    # has-attachments flag. Snippet is derived from the already-sanitized text.
    snippet = " ".join((text or "").split())[:160]
    last_from = parsed["from_name"] or parsed["from_addr"] or ""
    has_attachments = bool(attachment_docs)

    # Message is durably stored — NOW create/update the thread + attachments.
    if thread is None:
        _threads().insert_one(
            {
                "thread_id": thread_id,
                "user_email": user_email,
                "alias_id": alias_id,
                "mailbox_id": mailbox_id,
                "application_id": corr["application_id"],
                "subject": parsed["subject"],
                "subject_norm": subject_norm,
                "participants": sorted(
                    {p for p in (parsed["from_addr"], (envelope_to or "").strip().lower()) if p}
                ),
                "message_count": 1,
                "unread_count": 1,
                "last_message_at": received_at,
                "snippet": snippet,
                "last_from": last_from,
                "has_attachments": has_attachments,
                "trust": corr["trust"],
                "status_suggestion": corr["suggestion"],
                "created_at": received_at,
                "updated_at": received_at,
            }
        )
    else:
        updates: Dict[str, Any] = {
            "last_message_at": received_at,
            "updated_at": received_at,
            # The list previews the latest message, so snippet/sender track it.
            "snippet": snippet,
            "last_from": last_from,
        }
        # has_attachments is sticky — once a thread has any attachment it stays
        # flagged; a later attachment-free reply must not clear the paperclip.
        if has_attachments:
            updates["has_attachments"] = True
        # A message that correlates upgrades the thread to verified; we never
        # downgrade a thread that was already verified.
        if corr["trust"] == "verified":
            updates["trust"] = "verified"
            if corr["application_id"]:
                updates["application_id"] = corr["application_id"]
            if corr["suggestion"]:
                updates["status_suggestion"] = corr["suggestion"]
        _threads().update_one(
            {"thread_id": thread_id, "user_email": user_email},
            {
                "$set": updates,
                "$inc": {"message_count": 1, "unread_count": 1},
                "$addToSet": {
                    "participants": {
                        "$each": [p for p in (parsed["from_addr"],) if p]
                    }
                },
            },
        )

    if attachment_docs:
        try:
            _attachments().insert_many(attachment_docs, ordered=False)
        except DuplicateKeyError:  # pragma: no cover - uuid ids don't collide
            pass

    logger.info(
        "mail-store: stored message thread=%s parts=%d attachments=%d trust=%s",
        thread_id, parsed["part_count"], len(attachment_docs), corr["trust"],
    )
    return {"thread_id": thread_id, "message_id": message_id, "deduplicated": False}


def _sha256_bytes(data: bytes) -> str:
    import hashlib
    return hashlib.sha256(data or b"").hexdigest()


# ---------------------------------------------------------------------------
# User-facing READ API (managed-mailbox-v1 §5) — owner-scoped, never leaks.
#
# Every helper below is called with ``user_email`` = the JWT identity (the
# route resolves it, never a request body — D7). EVERY Mongo filter carries
# ``user_email`` so a cross-user id is simply "not found" (404), never a leak
# or a 403 that confirms existence (spec §10 IDOR line). Untrusted path/query
# ids are sanitized before they reach a filter (invariant #4). Bodies are NEVER
# returned as raw MIME: text is served inline (from the sanitized S3 text
# object) and HTML only as a short-TTL presigned URL (safe_html_url).
# ---------------------------------------------------------------------------

# Short TTL for every presigned GET the read API hands the client (§5).
_READ_PRESIGN_TTL_SECONDS = 5 * 60


class AttachmentBlocked(Exception):
    """Raised when an owned attachment exists but is quarantined
    (``scan_verdict`` suspicious/blocked, or no downloadable key). The route
    maps this to 403 — the file is real but must never be handed back."""


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    try:
        return value.isoformat()
    except Exception:  # pragma: no cover - defensive
        return None


def _thread_summary(thread: Dict[str, Any]) -> Dict[str, Any]:
    """Shape a ``mail_threads`` doc into a ThreadSummary (§5). No S3, no body —
    just the inbox-list metadata."""
    return {
        "thread_id": thread.get("thread_id"),
        "subject": thread.get("subject"),
        "participants": list(thread.get("participants") or []),
        "application_id": thread.get("application_id"),
        "message_count": int(thread.get("message_count", 0) or 0),
        "unread_count": int(thread.get("unread_count", 0) or 0),
        "last_message_at": _iso(thread.get("last_message_at")),
        "snippet": thread.get("snippet") or "",
        "last_from": thread.get("last_from") or "",
        "has_attachments": bool(thread.get("has_attachments", False)),
        "trust": thread.get("trust", "unverified"),
        "status_suggestion": thread.get("status_suggestion"),
    }


def _message_view(message: Dict[str, Any], storage: Any, bucket: str) -> Dict[str, Any]:
    """Shape a ``mail_messages`` doc into a MessageView (§5): sanitized text
    INLINE, the sanitized HTML only as a short-TTL presigned URL, and the
    attachment list — NEVER the raw MIME (``raw_s3_key`` is never exposed)."""
    text = ""
    text_key = message.get("text_s3_key")
    if text_key:
        try:
            text = storage.get_object_bytes(text_key, bucket=bucket).decode("utf-8", "replace")
        except Exception:  # pragma: no cover - a missing body must not 500 the view
            text = ""

    # Inline the already-sanitized HTML so the FE can render it in a sandboxed
    # iframe via srcdoc (its primary path); keep a short-TTL presigned URL as a
    # large-body fallback. Both come from the SAME sanitized S3 object — the raw
    # MIME (raw_s3_key) is never exposed either way.
    safe_html = ""
    safe_html_url = None
    html_key = message.get("safe_html_s3_key")
    if html_key:
        try:
            safe_html = storage.get_object_bytes(html_key, bucket=bucket).decode("utf-8", "replace")
        except Exception:  # pragma: no cover - a missing body must not 500 the view
            safe_html = ""
        try:
            safe_html_url = storage.generate_presigned_get(
                html_key, bucket=bucket, expiry=_READ_PRESIGN_TTL_SECONDS
            )
        except Exception:  # pragma: no cover - presign failure degrades gracefully
            safe_html_url = None

    attachments = [
        {
            "id": a.get("attachment_id"),
            "filename": a.get("filename"),
            "size": a.get("size_bytes"),
            "verdict": a.get("scan_verdict"),
        }
        for a in _attachments().find({"message_id": message.get("message_id")})
    ]

    return {
        "message_id": message.get("message_id"),
        "from": message.get("from_addr"),
        "from_name": message.get("from_name"),
        "subject": message.get("subject"),
        "received_at": _iso(message.get("received_at")),
        "spf": message.get("spf"),
        "dkim": message.get("dkim"),
        "dmarc": message.get("dmarc"),
        "trust": message.get("trust", "unverified"),
        "read": bool(message.get("read", False)),
        "text": text,
        "safe_html": safe_html,
        "safe_html_url": safe_html_url,
        "attachments": attachments,
    }


def unread_count(user_email: str) -> int:
    """Total unread messages across the user's threads (for the GET /mailbox
    surface). Owner-scoped."""
    email = InputSanitizer.sanitize_email(user_email)
    if not email:
        return 0
    total = 0
    for t in _threads().find({"user_email": email}, {"unread_count": 1, "_id": 0}):
        total += int(t.get("unread_count", 0) or 0)
    return total


def list_threads(
    user_email: str,
    *,
    trust: Optional[str] = None,
    application_id: Optional[str] = None,
    unread: Optional[bool] = None,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """Owner-scoped inbox list, newest first (§5). Filters are code-controlled:
    ``trust`` is clamped to the enum, ``application_id`` is sanitized, and
    ``unread`` only ever builds a code-owned ``{"$gt": 0}`` (never a
    user-supplied operator)."""
    email = InputSanitizer.sanitize_email(user_email)
    if not email:
        return []
    query: Dict[str, Any] = {"user_email": email}
    if trust in ("verified", "unverified"):
        query["trust"] = trust
    if application_id:
        query["application_id"] = _san(application_id, 64)
    if unread:
        query["unread_count"] = {"$gt": 0}
    try:
        capped = max(1, min(int(limit), 200))
    except (TypeError, ValueError):
        capped = 100
    cursor = _threads().find(query).sort("last_message_at", -1).limit(capped)
    return [_thread_summary(t) for t in cursor]


def get_thread_detail(user_email: str, thread_id: str) -> Optional[Dict[str, Any]]:
    """Return ``{thread, messages:[MessageView]}`` for an owned thread, or None
    (route -> 404) when the thread does not exist OR is not this user's — the
    ``user_email`` in the filter makes a cross-user id indistinguishable from a
    missing one (no IDOR leak)."""
    email = InputSanitizer.sanitize_email(user_email)
    tid = _san(thread_id, 64)
    if not email or not tid:
        return None
    thread = _threads().find_one({"thread_id": tid, "user_email": email})
    if not thread:
        return None
    storage = get_storage_service()
    bucket = _apply_mail_bucket()
    messages = _messages().find({"thread_id": tid, "user_email": email}).sort("received_at", 1)
    return {
        "thread": _thread_summary(thread),
        "messages": [_message_view(m, storage, bucket) for m in messages],
    }


def set_thread_read(user_email: str, thread_id: str, read: bool) -> bool:
    """Mark every message in an owned thread read/unread and reconcile the
    thread's ``unread_count``. Returns False (route -> 404) when the thread is
    not the user's."""
    email = InputSanitizer.sanitize_email(user_email)
    tid = _san(thread_id, 64)
    if not email or not tid:
        return False
    if not _threads().find_one({"thread_id": tid, "user_email": email}, {"_id": 1}):
        return False
    _messages().update_many(
        {"thread_id": tid, "user_email": email},
        {"$set": {"read": bool(read)}},
    )
    unread = 0 if read else _messages().count_documents(
        {"thread_id": tid, "user_email": email}
    )
    _threads().update_one(
        {"thread_id": tid, "user_email": email},
        {"$set": {"unread_count": int(unread), "updated_at": _now()}},
    )
    return True


def attach_thread(user_email: str, thread_id: str, application_id: str) -> bool:
    """Bind an owned thread to one of the user's OWN applications. Raises
    ``LookupError`` when the thread OR the application is not the user's (route
    -> 404) and ``ValueError`` for a missing application id (route -> 400).

    Ownership of the application is validated against BOTH the canonical
    ``applications`` ledger and the interim ``tailoring_records`` store (spec
    §8.1 uses ``tailoring_records.record_id`` as the application id) — always
    owner-scoped, so a foreign id is a 404, never an attach."""
    email = InputSanitizer.sanitize_email(user_email)
    tid = _san(thread_id, 64)
    if not email or not tid:
        raise LookupError("thread_not_found")
    app_id = _san(application_id, 64)
    if not app_id:
        raise ValueError("application_id_required")
    if not _threads().find_one({"thread_id": tid, "user_email": email}, {"_id": 1}):
        raise LookupError("thread_not_found")
    owned = _db().tailoring_records.find_one(
        {"record_id": app_id, "user_email": email}, {"_id": 1}
    ) or _db().applications.find_one(
        {"application_id": app_id, "user_email": email}, {"_id": 1}
    )
    if not owned:
        raise LookupError("application_not_found")
    _threads().update_one(
        {"thread_id": tid, "user_email": email},
        {"$set": {"application_id": app_id, "updated_at": _now()}},
    )
    return True


def detach_thread(user_email: str, thread_id: str) -> bool:
    """Unbind an owned thread from its application. Returns False (route -> 404)
    when the thread is not the user's."""
    email = InputSanitizer.sanitize_email(user_email)
    tid = _san(thread_id, 64)
    if not email or not tid:
        return False
    if not _threads().find_one({"thread_id": tid, "user_email": email}, {"_id": 1}):
        return False
    _threads().update_one(
        {"thread_id": tid, "user_email": email},
        {"$set": {"application_id": None, "updated_at": _now()}},
    )
    return True


def get_attachment_download(user_email: str, attachment_id: str) -> Dict[str, str]:
    """Return ``{download_url}`` (short-TTL presigned GET) for an owned, CLEAN
    attachment. Raises ``LookupError`` (route -> 404) when it is not the user's,
    and ``AttachmentBlocked`` (route -> 403) when it is quarantined
    (suspicious/blocked or no stored key) — an isolated file is never handed
    back."""
    email = InputSanitizer.sanitize_email(user_email)
    aid = _san(attachment_id, 64)
    if not email or not aid:
        raise LookupError("attachment_not_found")
    att = _attachments().find_one({"attachment_id": aid, "user_email": email})
    if not att:
        raise LookupError("attachment_not_found")
    if att.get("scan_verdict") != "clean" or not att.get("s3_key"):
        raise AttachmentBlocked(str(att.get("scan_verdict") or "blocked"))
    url = get_storage_service().generate_presigned_get(
        att["s3_key"], bucket=_apply_mail_bucket(), expiry=_READ_PRESIGN_TTL_SECONDS
    )
    return {"download_url": url}


def report_phishing(user_email: str, thread_id: str) -> bool:
    """Flag every message in an owned thread as user-reported phishing and drop
    the thread's trust to ``unverified`` (so it can never drive an auto-action).
    Returns False (route -> 404) when the thread is not the user's. Logs only
    the thread id — never sender/subject/body."""
    email = InputSanitizer.sanitize_email(user_email)
    tid = _san(thread_id, 64)
    if not email or not tid:
        return False
    if not _threads().find_one({"thread_id": tid, "user_email": email}, {"_id": 1}):
        return False
    _messages().update_many(
        {"thread_id": tid, "user_email": email},
        {"$set": {"is_phishing_reported": True}},
    )
    _threads().update_one(
        {"thread_id": tid, "user_email": email},
        {"$set": {"trust": "unverified", "updated_at": _now()}},
    )
    logger.info("mail-store: phishing reported thread=%s", tid)
    return True


# ---------------------------------------------------------------------------
# Forwarding — SAFE notify-to-original-inbox path (managed-mailbox-v1 §4.2/§8.1)
# ---------------------------------------------------------------------------

def maybe_forward(message_doc: Dict[str, Any]) -> Dict[str, Any]:
    """Fire the SAFE forwarding path for a freshly-ingested message.

    Delegates to ``mail_forward_service.forward_notification`` (kept in its own
    module so this storage service stays focused on ingest). ``message_doc`` is
    the small result dict ``ingest_committed`` returns
    (``{thread_id, message_id, deduplicated}``); the forwarder loads the durable
    message/thread it needs from Mongo by ``message_id``.

    NEVER raises into the commit path — a forwarding failure must not fail the
    inbound commit (the message is already durably stored). Returns a small
    ``{sent: bool, reason}`` result for logging/tests.

    NOTE: this is ONLY the notify-to-original-inbox half. The recruiter
    reply-relay / reply-bound-alias INBOUND relay (a real two-way conversation
    from the user's inbox back to the recruiter) is a separate, later P2 task
    that ships with the reply UI — see ``mail_forward_service.reply_relay_stub``.
    """
    try:
        from services.mail_forward_service import forward_notification
        return forward_notification(message_doc)
    except Exception as e:  # pragma: no cover - forwarding is best-effort
        logger.warning("maybe_forward: forward_notification raised: %s", type(e).__name__)
        return {"sent": False, "reason": "error"}
