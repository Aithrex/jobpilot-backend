"""Web Bot Auth: our crawler says who it is, cryptographically.

Cloudflare's bot management (in front of Workday's job sites since about
2026-11-14) challenges anonymous automated traffic. Its Verified Bots programme
accepts crawlers that sign every request with an HTTP Message Signature
(RFC 9421) whose public key is published at
`https://<our host>/.well-known/http-message-signatures-directory`.
Reference: developers.cloudflare.com/bots/reference/bot-verification/web-bot-auth/
(read 2026-11-17).

What this module does:

* `USER_AGENT` / `BOT_TOKEN` are THE crawler's name. Every request it sends
  to an employer, an ATS or a public data source uses them, through
  `bot_headers(url, headers)` or a `BotSession`, which also sign it;
* `sign_request_headers(url)` returns the three headers for one outbound
  request: `Signature-Agent` (a quoted https URL), `Signature-Input` covering
  `("@authority" "signature-agent")` with created / expires / keyid / alg /
  nonce / tag="web-bot-auth", and `Signature`;
* `directory_response(host)` returns the key directory (JWKS of Ed25519 public
  keys, never the private part) and the signature over it that Cloudflare
  requires (tag="http-message-signatures-directory", `"@authority";req`);
* `crawler_identity_bp` serves that directory and a plain page describing the
  crawler, which the User-Agent links to.

Switched off unless configured: publishing the directory needs
`WEB_BOT_AUTH_KEY`, and signing also needs `WEB_BOT_AUTH_SIGN=1`. Once signing
is on, EVERY request is signed, whatever the host. Cloudflare's review
(2026-11-19) rejected a crawler that "can" sign: Web Bot Auth is our only
verification method, so a request without a signature is a request Cloudflare
cannot attribute to us. The page at `/bot` states which of the two is true,
from the same setting, so it can never claim more than the crawler does.
Every process that sends crawler traffic (ingest-runner, the worker and the
gateway) must carry the same two settings.

Identity, after the same review: the bot is submitted as "Aspirely Bot", so
the User-Agent token is `AspirelyBot`. It was `JobPilotIngest`, which also
named an unrelated product. robots.txt rules written for the old token are
still obeyed (`ROBOTS_TOKENS`).

Environment:
  WEB_BOT_AUTH_KEY    base64 of the 32-byte Ed25519 private seed, or a PEM
                      private key (`scripts/web_bot_auth_keygen.py` makes one)
  WEB_BOT_AUTH_AGENT  the https origin hosting the directory
                      (default https://aspirely.me)
  WEB_BOT_AUTH_SIGN   "1" to sign every outbound crawler request (default off)
  WEB_BOT_AUTH_TTL    seconds a signature stays valid (default 60)
  WEB_BOT_AUTH_CONTACT                 address on the /bot page
  WEB_BOT_AUTH_OPERATOR                legal name of the operating company
  WEB_BOT_AUTH_OPERATOR_JURISDICTION   where it is registered
  WEB_BOT_AUTH_OPERATOR_NUMBER         its company registration number
  WEB_BOT_AUTH_OPERATOR_ADDRESS        registered address (optional)
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import threading
import time
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

DEFAULT_AGENT = "https://aspirely.me"
DIRECTORY_PATH = "/.well-known/http-message-signatures-directory"
DIRECTORY_CONTENT_TYPE = "application/http-message-signatures-directory+json"
REQUEST_TAG = "web-bot-auth"
DIRECTORY_TAG = "http-message-signatures-directory"
INFO_PATH = "/bot"

#: The bot's name as submitted to Cloudflare, and the User-Agent token that
#: must match it.
BOT_NAME = "Aspirely Bot"
BOT_TOKEN = "AspirelyBot"
BOT_VERSION = "1.2"
#: robots.txt groups we obey: ours, and the token we used before 2026-11-20,
#: so a site that wrote rules for the old name is not suddenly crawled.
ROBOTS_TOKENS = (BOT_TOKEN, "JobPilotIngest")
_SIGNATURE_HEADERS = ("signature", "signature-input", "signature-agent")

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


def should_sign(url: str) -> bool:
    """Every http(s) request, once signing is on. There is no host list: a
    request we leave unsigned is one Cloudflare cannot attribute to us."""
    if not signing_enabled():
        return False
    try:
        p = urlparse(str(url or ""))
    except Exception:
        return False
    return p.scheme in ("http", "https") and bool(p.hostname)


def user_agent() -> str:
    return f"Mozilla/5.0 (compatible; {BOT_TOKEN}/{BOT_VERSION}; +{info_url()})"


#: Read once at import, like every other header dict that embeds it.
USER_AGENT = user_agent()


def bot_headers(url: str, headers: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """`headers` (copied) with the crawler's User-Agent and, when signing is
    on, a fresh signature for `url`. Any signature already present is dropped
    first, because it was made for another request. Never raises: a request
    is never blocked on its signature."""
    out = {k: v for k, v in (headers or {}).items()
           if str(k).lower() not in _SIGNATURE_HEADERS and str(k).lower() != "user-agent"}
    out["User-Agent"] = USER_AGENT
    try:
        if should_sign(url):
            out.update(sign_request_headers(url))
    except Exception:
        pass
    return out


def robots_allows(parser: Any, url: str) -> bool:
    """A robots.txt parser's answer for every token we have used."""
    return all(bool(parser.can_fetch(token, url)) for token in ROBOTS_TOKENS)


def _sign_prepared(request: Any) -> None:
    for name in [k for k in request.headers if str(k).lower() in _SIGNATURE_HEADERS]:
        del request.headers[name]
    request.headers["User-Agent"] = USER_AGENT
    try:
        if should_sign(request.url):
            request.headers.update(sign_request_headers(request.url))
    except Exception:
        pass


def _session_class():
    import requests

    class BotSession(requests.Session):
        """A `requests.Session` that names the crawler and signs EVERY request
        it sends, including each hop of a redirect (`Session.send` runs once
        per hop, so a redirect to another host gets a signature for that
        host, not a stale one for the first)."""

        def __init__(self) -> None:
            super().__init__()
            self.headers["User-Agent"] = USER_AGENT

        def send(self, request, **kwargs):
            _sign_prepared(request)
            return super().send(request, **kwargs)

    return BotSession


BotSession = _session_class()


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
<title>Aspirely Bot</title>
<style>body{{font:16px/1.55 system-ui,sans-serif;max-width:42rem;margin:2rem auto;padding:0 1rem;color:#1b1b1b;background:#fff}}
code{{background:#f2f2f2;padding:0 .25rem}}</style></head><body>
<h1>Aspirely Bot</h1>
<p>Aspirely helps job seekers find openings that fit them. Aspirely Bot, our
crawler, reads <strong>public</strong> job postings from employers' career
sites and links every listing back to the employer's own page, where people
apply.</p>
{operator}
<h2>How it behaves</h2>
<ul>
<li>Its User-Agent is <code>{user_agent}</code>: the token
<code>{token}</code> and a link to this page.</li>
<li>It reads only public job listings and job descriptions. It never signs in,
never submits forms and never reads personal data.</li>
<li>It obeys <code>robots.txt</code> rules for <code>{token}</code>, and for
<code>JobPilotIngest</code>, its name before November 2026. Each site's file
is read at least once a day, and a page it disallows is not requested. If the file cannot be read, the
crawler waits and does not guess.</li>
<li>It spaces its requests per vendor, stops for minutes when a site
challenges or rate-limits it, and never retries into a challenge.</li>
<li>{signing}</li>
</ul>
<h2>Contact</h2>
<p>To ask us to slow down, stop, or change how we read your site, email
<a href="mailto:{contact}">{contact}</a>. We act on requests promptly.</p>
</body></html>
"""


def _env(name: str) -> str:
    return str(os.getenv(name) or "").strip()


def operator_html() -> str:
    """Who runs the bot, as Cloudflare's review asked: the operating company,
    where it is registered and its company number. Rendered only when the
    owner has set all three, so the page never states a guess."""
    from html import escape

    name = _env("WEB_BOT_AUTH_OPERATOR")
    where = _env("WEB_BOT_AUTH_OPERATOR_JURISDICTION")
    number = _env("WEB_BOT_AUTH_OPERATOR_NUMBER")
    if not (name and where and number):
        return ""
    address = _env("WEB_BOT_AUTH_OPERATOR_ADDRESS")
    rows = [f"<p>Aspirely Bot is operated by <strong>{escape(name)}</strong>, "
            f"a company registered in {escape(where)}, company number "
            f"{escape(number)}.</p>"]
    if address:
        rows.append(f"<p>Registered address: {escape(address)}</p>")
    return "\n".join(rows)


def signing_html() -> str:
    directory = agent_origin() + DIRECTORY_PATH
    link = f'<a href="{directory}">{directory}</a>'
    if signing_enabled():
        return ("Every request it sends carries Web Bot Auth signature headers "
                "(<code>Signature</code>, <code>Signature-Input</code> and "
                "<code>Signature-Agent</code>, HTTP Message Signatures per RFC 9421). "
                f"The public keys are published at {link}.")
    return ("Its Web Bot Auth key directory is published at "
            f"{link}; request signing is not switched on yet.")


def info_page() -> str:
    from html import escape

    contact = _env("WEB_BOT_AUTH_CONTACT") or "harshith@aithrex.tech"
    return INFO_HTML.format(operator=operator_html(), signing=signing_html(),
                            user_agent=escape(USER_AGENT), token=BOT_TOKEN,
                            contact=escape(contact))


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
