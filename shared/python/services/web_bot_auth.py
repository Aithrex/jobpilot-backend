"""Web Bot Auth: our crawler says who it is, cryptographically.

Cloudflare's bot management (in front of Workday's job sites since about
2026-11-14) challenges anonymous automated traffic. Its Verified Bots programme
accepts crawlers that sign every request with an HTTP Message Signature
(RFC 9421) whose public key is published at
`https://<our host>/.well-known/http-message-signatures-directory`.
Reference: developers.cloudflare.com/bots/reference/bot-verification/web-bot-auth/
(read 2026-11-17).

What this module does:

* `sign_request_headers(url)` returns the three headers for one outbound
  request: `Signature-Agent` (a quoted https URL), `Signature-Input` covering
  `("@authority" "signature-agent")` with created / expires / keyid / alg /
  nonce / tag="web-bot-auth", and `Signature`;
* `directory_response(host)` returns the key directory (JWKS of Ed25519 public
  keys, never the private part) and the signature over it that Cloudflare
  requires (tag="http-message-signatures-directory", `"@authority";req`);
* `crawler_identity_bp` serves that directory and a plain page describing the
  crawler, which the User-Agent links to.

Switched off unless configured. Cloudflare asks that a bot starts signing only
AFTER its submission is accepted, so publishing the directory (needs
`WEB_BOT_AUTH_KEY`) and signing requests (also needs `WEB_BOT_AUTH_SIGN=1`) are
two separate steps the owner takes.

Environment:
  WEB_BOT_AUTH_KEY    base64 of the 32-byte Ed25519 private seed, or a PEM
                      private key (`scripts/web_bot_auth_keygen.py` makes one)
  WEB_BOT_AUTH_AGENT  the https origin hosting the directory
                      (default https://aspirely.me)
  WEB_BOT_AUTH_SIGN   "1" to sign outbound crawler requests (default off)
  WEB_BOT_AUTH_HOSTS  comma-separated host suffixes to sign for
                      (default ".myworkdayjobs.com,crawltest.com")
  WEB_BOT_AUTH_TTL    seconds a signature stays valid (default 60)
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import threading
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

DEFAULT_AGENT = "https://aspirely.me"
DIRECTORY_PATH = "/.well-known/http-message-signatures-directory"
DIRECTORY_CONTENT_TYPE = "application/http-message-signatures-directory+json"
REQUEST_TAG = "web-bot-auth"
DIRECTORY_TAG = "http-message-signatures-directory"
INFO_PATH = "/bot"

_lock = threading.Lock()
_cached: Dict[str, Any] = {}


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def agent_origin() -> str:
    raw = str(os.getenv("WEB_BOT_AUTH_AGENT") or DEFAULT_AGENT).strip().rstrip("/")
    return raw if raw.startswith("https://") else DEFAULT_AGENT


def info_url() -> str:
    return agent_origin() + INFO_PATH


def _load_private_key(raw: str):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    raw = (raw or "").strip()
    if not raw:
        return None
    if "BEGIN" in raw:
        key = load_pem_private_key(raw.replace("\\n", "\n").encode(), password=None)
        return key if isinstance(key, Ed25519PrivateKey) else None
    seed = base64.b64decode(raw + "=" * (-len(raw) % 4))
    return Ed25519PrivateKey.from_private_bytes(seed) if len(seed) == 32 else None


def private_key():
    """The configured Ed25519 key, or None. Parsed once per value."""
    raw = os.getenv("WEB_BOT_AUTH_KEY") or ""
    with _lock:
        if _cached.get("raw") != raw:
            try:
                key = _load_private_key(raw)
            except Exception:
                key = None
            _cached.clear()
            _cached.update(raw=raw, key=key)
        return _cached["key"]


def public_jwk(key=None) -> Optional[Dict[str, str]]:
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    key = key if key is not None else private_key()
    if key is None:
        return None
    x = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return {"kty": "OKP", "crv": "Ed25519", "x": _b64url(x)}


def thumbprint(jwk: Dict[str, str]) -> str:
    """RFC 7638 JWK thumbprint: SHA-256 over the required members in
    lexicographic order with no whitespace, base64url without padding."""
    canonical = json.dumps({"crv": jwk["crv"], "kty": jwk["kty"], "x": jwk["x"]},
                           separators=(",", ":"), sort_keys=True)
    return _b64url(hashlib.sha256(canonical.encode()).digest())


def _authority(url_or_host: str) -> str:
    """RFC 9421 `@authority`: lower-case host, with the port only when it is
    not the scheme's default."""
    if "://" not in url_or_host:
        return url_or_host.strip().lower()
    p = urlparse(url_or_host)
    host = (p.hostname or "").lower()
    if p.port and not ((p.scheme == "https" and p.port == 443) or
                       (p.scheme == "http" and p.port == 80)):
        host = f"{host}:{p.port}"
    return host


def _sign(key, base: str) -> str:
    return _b64(key.sign(base.encode("utf-8")))


def signing_enabled() -> bool:
    return str(os.getenv("WEB_BOT_AUTH_SIGN") or "").strip() == "1" and private_key() is not None


def _signed_hosts() -> List[str]:
    raw = os.getenv("WEB_BOT_AUTH_HOSTS") or ".myworkdayjobs.com,crawltest.com"
    return [h.strip().lower() for h in raw.split(",") if h.strip()]


def should_sign(url: str) -> bool:
    if not signing_enabled():
        return False
    host = (urlparse(url).hostname or "").lower()
    return any(host == s.lstrip(".") or host.endswith(s if s.startswith(".") else "." + s)
               for s in _signed_hosts())


def sign_request_headers(url: str, *, now: Optional[float] = None, key=None,
                         nonce: Optional[str] = None,
                         ttl: Optional[int] = None) -> Dict[str, str]:
    """`Signature-Agent`, `Signature-Input` and `Signature` for one request
    to `url`. Empty when no key is configured."""
    key = key if key is not None else private_key()
    if key is None:
        return {}
    created = int(now if now is not None else time.time())
    life = int(ttl if ttl is not None else (os.getenv("WEB_BOT_AUTH_TTL") or 60))
    agent_header = f'"{agent_origin()}"'
    params = (f'("@authority" "signature-agent");created={created};'
              f'keyid="{thumbprint(public_jwk(key))}";alg="ed25519";'
              f'expires={created + max(1, life)};'
              f'nonce="{nonce or _b64(secrets.token_bytes(32))}";tag="{REQUEST_TAG}"')
    base = (f'"@authority": {_authority(url)}\n'
            f'"signature-agent": {agent_header}\n'
            f'"@signature-params": {params}')
    return {
        "Signature-Agent": agent_header,
        "Signature-Input": f"sig1={params}",
        "Signature": f"sig1=:{_sign(key, base)}:",
    }


def directory_response(host: str, *, now: Optional[float] = None, key=None,
                       nonce: Optional[str] = None) -> Optional[Tuple[str, Dict[str, str]]]:
    """(JSON body, headers) for the key directory, signed as Cloudflare
    requires, or None when no key is configured. `host` is the request's
    authority (the directory signature covers `"@authority";req`)."""
    key = key if key is not None else private_key()
    if key is None:
        return None
    jwk = public_jwk(key)
    body = json.dumps({"keys": [jwk]}, separators=(",", ":"))
    created = int(now if now is not None else time.time())
    params = (f'("@authority";req);alg="ed25519";keyid="{thumbprint(jwk)}";'
              f'nonce="{nonce or _b64(secrets.token_bytes(32))}";tag="{DIRECTORY_TAG}";'
              f'created={created};expires={created + 300}')
    base = (f'"@authority";req: {_authority(host)}\n'
            f'"@signature-params": {params}')
    return body, {
        "Content-Type": DIRECTORY_CONTENT_TYPE,
        "Cache-Control": "max-age=86400",
        "Signature-Input": f"sig1={params}",
        "Signature": f"sig1=:{_sign(key, base)}:",
    }


INFO_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Aspirely crawler</title>
<style>body{{font:16px/1.55 system-ui,sans-serif;max-width:42rem;margin:2rem auto;padding:0 1rem;color:#1b1b1b;background:#fff}}
code{{background:#f2f2f2;padding:0 .25rem}}</style></head><body>
<h1>Aspirely job crawler</h1>
<p>Aspirely helps job seekers find openings that fit them. Our crawler reads
<strong>public</strong> job postings from employers' career sites and links
every listing back to the employer's own page, where people apply.</p>
<h2>How it behaves</h2>
<ul>
<li>User-Agent contains <code>JobPilotIngest</code> and a link to this page.</li>
<li>It reads only public job listings and job descriptions. It never signs in,
never submits forms and never reads personal data.</li>
<li>It obeys <code>robots.txt</code>: each site's file is read at least once a
day, and a page it disallows is not requested. If the file cannot be read, the
crawler waits and does not guess.</li>
<li>It spaces its requests per vendor, stops for minutes when a site
challenges or rate-limits it, and never retries into a challenge.</li>
<li>Requests can carry an HTTP Message Signature (Web Bot Auth). The public
keys are published at <a href="{directory}">{directory}</a>.</li>
</ul>
<h2>Contact</h2>
<p>To ask us to slow down, stop, or change how we read your site, email
<a href="mailto:{contact}">{contact}</a>. We act on requests promptly.</p>
</body></html>
"""


def info_page() -> str:
    contact = str(os.getenv("WEB_BOT_AUTH_CONTACT") or "harshith@aithrex.tech").strip()
    return INFO_HTML.format(directory=agent_origin() + DIRECTORY_PATH, contact=contact)


def _blueprint():
    from flask import Blueprint, Response, request

    bp = Blueprint("crawler_identity", __name__)

    @bp.route(DIRECTORY_PATH, methods=["GET"], strict_slashes=False)
    @bp.route(DIRECTORY_PATH + "/", methods=["GET"])
    def key_directory():
        out = directory_response(request.host)
        if out is None:
            return Response("not configured\n", status=404, mimetype="text/plain")
        body, headers = out
        resp = Response(body, status=200)
        for k, v in headers.items():
            resp.headers[k] = v
        return resp

    @bp.route(INFO_PATH, methods=["GET"])
    def crawler_info():
        resp = Response(info_page(), status=200, mimetype="text/html")
        resp.headers["Cache-Control"] = "max-age=3600"
        return resp

    return bp


crawler_identity_bp = _blueprint()
