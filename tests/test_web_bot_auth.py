"""Web Bot Auth signing and the Workday cookie jar (2026-11-17).

Workday's job sites sit behind Cloudflare bot management. The legitimate way
for a crawler to be recognised is Cloudflare's Verified Bots programme: sign
each request (RFC 9421, Ed25519, tag "web-bot-auth") and publish the public
key at /.well-known/http-message-signatures-directory. Separately, the crawler
threw away the cookies Cloudflare set, so every request looked like a new
visitor. Spec read on 2026-11-17 from
developers.cloudflare.com/bots/reference/bot-verification/web-bot-auth/.
"""
import base64
import json
import re
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from services import job_ingest_worker as w
from services import web_bot_auth as wba

# RFC 8037 Appendix A.1 test key.
RFC_D = "nWGxne_9WmC6hEr0kuwsxERJxWl7MmkZcDusAxyuf2A"
RFC_X = "11qYAYKxCrfVS_7TyWQHOg7hcvPapiMlrwIaaPcHURo"
RFC_THUMBPRINT = "kPrK_qmxVWaYVA9wwBF6Iuo3vVzz7TxHCTwXBygrS4k"   # RFC 8037 A.3


def _rfc_key():
    seed = base64.urlsafe_b64decode(RFC_D + "=")
    return Ed25519PrivateKey.from_private_bytes(seed)


@pytest.fixture()
def configured(monkeypatch):
    monkeypatch.setenv("WEB_BOT_AUTH_KEY", base64.b64encode(base64.urlsafe_b64decode(RFC_D + "=")).decode())
    monkeypatch.setenv("WEB_BOT_AUTH_AGENT", "https://aspirely.me")
    monkeypatch.setenv("WEB_BOT_AUTH_SIGN", "1")
    monkeypatch.delenv("WEB_BOT_AUTH_HOSTS", raising=False)
    yield


def _verify(key, base, signature_header):
    sig = base64.b64decode(re.fullmatch(r"sig1=:(.+):", signature_header).group(1))
    key.public_key().verify(sig, base.encode())          # raises if wrong


class TestKeyAndThumbprint:
    def test_jwk_and_thumbprint_match_rfc_8037(self):
        jwk = wba.public_jwk(_rfc_key())
        assert jwk == {"kty": "OKP", "crv": "Ed25519", "x": RFC_X}
        assert wba.thumbprint(jwk) == RFC_THUMBPRINT

    def test_a_pem_key_loads_too(self, monkeypatch):
        from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat
        pem = _rfc_key().private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode()
        monkeypatch.setenv("WEB_BOT_AUTH_KEY", pem)
        assert wba.public_jwk()["x"] == RFC_X

    def test_no_or_bad_key_means_nothing_is_signed(self, monkeypatch):
        monkeypatch.setenv("WEB_BOT_AUTH_SIGN", "1")
        for raw in ("", "not-base64!", base64.b64encode(b"short").decode()):
            monkeypatch.setenv("WEB_BOT_AUTH_KEY", raw)
            assert wba.private_key() is None
            assert wba.sign_request_headers("https://acme.wd5.myworkdayjobs.com/x") == {}
            assert not wba.should_sign("https://acme.wd5.myworkdayjobs.com/x")


class TestRequestSignature:
    def test_headers_have_the_shape_cloudflare_documents(self, configured):
        h = wba.sign_request_headers("https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/External/jobs",
                                     now=1735689600, nonce="bm9uY2U=")
        assert h["Signature-Agent"] == '"https://aspirely.me"'
        assert h["Signature-Input"] == (
            'sig1=("@authority" "signature-agent");created=1735689600;'
            f'keyid="{RFC_THUMBPRINT}";alg="ed25519";expires=1735689660;'
            'nonce="bm9uY2U=";tag="web-bot-auth"')
        assert re.fullmatch(r"sig1=:[A-Za-z0-9+/]+=*:", h["Signature"])

    def test_the_signature_verifies_over_the_rfc_9421_base(self, configured):
        h = wba.sign_request_headers("https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/External/jobs",
                                     now=1735689600, nonce="bm9uY2U=")
        params = h["Signature-Input"][len("sig1="):]
        base = ('"@authority": acme.wd5.myworkdayjobs.com\n'
                '"signature-agent": "https://aspirely.me"\n'
                f'"@signature-params": {params}')
        _verify(_rfc_key(), base, h["Signature"])

    def test_authority_keeps_only_a_non_default_port(self):
        assert wba._authority("https://Example.COM:443/a") == "example.com"
        assert wba._authority("https://example.com:8443/a") == "example.com:8443"

    def test_no_forbidden_parameters(self, configured):
        h = wba.sign_request_headers("https://crawltest.com/cdn-cgi/web-bot-auth")
        names = set(re.findall(r";([a-z]+)", h["Signature-Input"]))
        assert names == {"created", "keyid", "alg", "expires", "nonce", "tag"}
        assert not names & {"sf", "bs", "key", "req", "name"}
        assert "@query-params" not in h["Signature-Input"] and "@status" not in h["Signature-Input"]

    def test_signing_is_off_until_the_owner_turns_it_on(self, configured, monkeypatch):
        monkeypatch.setenv("WEB_BOT_AUTH_SIGN", "0")
        assert not wba.should_sign("https://acme.wd5.myworkdayjobs.com/x")

    def test_only_listed_hosts_are_signed(self, configured):
        assert wba.should_sign("https://acme.wd5.myworkdayjobs.com/x")
        assert wba.should_sign("https://crawltest.com/cdn-cgi/web-bot-auth")
        assert not wba.should_sign("https://boards-api.greenhouse.io/v1/boards/x")
        assert not wba.should_sign("https://evilmyworkdayjobs.com/x")


class TestKeyDirectory:
    def test_directory_lists_the_public_key_only_and_is_signed(self, configured):
        body, headers = wba.directory_response("aspirely.me", now=1735689600, nonce="bm9uY2U=")
        assert json.loads(body) == {"keys": [{"kty": "OKP", "crv": "Ed25519", "x": RFC_X}]}
        assert '"d"' not in body
        assert headers["Content-Type"] == "application/http-message-signatures-directory+json"
        params = headers["Signature-Input"][len("sig1="):]
        assert params.startswith('("@authority";req);alg="ed25519";')
        assert 'tag="http-message-signatures-directory"' in params
        _verify(_rfc_key(), f'"@authority";req: aspirely.me\n"@signature-params": {params}',
                headers["Signature"])

    def test_routes_serve_the_directory_and_the_info_page(self, configured):
        from flask import Flask
        app = Flask(__name__)
        app.register_blueprint(wba.crawler_identity_bp)
        c = app.test_client()
        r = c.get("/.well-known/http-message-signatures-directory", headers={"Host": "aspirely.me"})
        assert r.status_code == 200
        assert r.headers["Content-Type"] == wba.DIRECTORY_CONTENT_TYPE
        assert r.headers["Signature"].startswith("sig1=:")
        page = c.get("/bot")
        assert page.status_code == 200 and b"robots.txt" in page.data

    def test_no_key_answers_404_not_an_empty_directory(self, monkeypatch):
        monkeypatch.setenv("WEB_BOT_AUTH_KEY", "")
        from flask import Flask
        app = Flask(__name__)
        app.register_blueprint(wba.crawler_identity_bp)
        assert app.test_client().get("/.well-known/http-message-signatures-directory").status_code == 404


class TestCrawlerSendsCookiesAndSignatures:
    WD = "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/External/jobs"

    def _capture(self, monkeypatch, set_cookie=None):
        import requests
        from requests.cookies import RequestsCookieJar
        sent = []

        def fake_post(url, **kw):
            jar = RequestsCookieJar()
            if set_cookie:
                jar.set(*set_cookie, domain="acme.wd5.myworkdayjobs.com", path="/")
            sent.append(kw)
            return SimpleNamespace(status_code=200, headers={}, cookies=jar, json=lambda: {"ok": 1})
        monkeypatch.setattr(requests, "post", fake_post)
        return sent

    def test_cookies_the_site_set_are_sent_back(self, monkeypatch):
        sent = self._capture(monkeypatch, set_cookie=("__cf_bm", "abc"))
        for _ in range(2):
            w.http_get_json(w.SOURCE_WORKDAY, self.WD, w.HostPolicy(), deadline=1e12, method="POST")
        assert sent[0]["cookies"].get("__cf_bm") is None or len(sent) == 2
        assert sent[1]["cookies"].get("__cf_bm") == "abc"

    def test_a_signed_request_carries_the_three_headers(self, configured, monkeypatch):
        sent = self._capture(monkeypatch)
        w.http_get_json(w.SOURCE_WORKDAY, self.WD, w.HostPolicy(), deadline=1e12, method="POST")
        h = sent[0]["headers"]
        assert {"Signature", "Signature-Input", "Signature-Agent"} <= set(h)
        assert "aspirely.me/bot" in h["User-Agent"]

    def test_other_vendors_get_no_cookie_jar_and_no_signature(self, configured, monkeypatch):
        import requests
        sent = []
        monkeypatch.setattr(requests, "get", lambda url, **kw: sent.append(kw) or SimpleNamespace(
            status_code=200, headers={}, json=lambda: {"ok": 1}))
        w.http_get_json("career-pages", "https://boards-api.greenhouse.io/v1/boards/acme/jobs",
                        w.HostPolicy(), deadline=1e12)
        assert "cookies" not in sent[0] and "Signature" not in sent[0]["headers"]

    def test_a_signer_failure_never_blocks_the_request(self, configured, monkeypatch):
        sent = self._capture(monkeypatch)
        monkeypatch.setattr(wba, "sign_request_headers", lambda *a, **k: 1 / 0)
        assert w.http_get_json(w.SOURCE_WORKDAY, self.WD, w.HostPolicy(), deadline=1e12,
                               method="POST") == {"ok": 1}
        assert "Signature" not in sent[0]["headers"]
