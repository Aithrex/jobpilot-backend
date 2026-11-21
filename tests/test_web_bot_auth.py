"""Web Bot Auth signing and the Workday cookie jar (2026-11-17), and the one
bot name Cloudflare's review asked for (2026-11-20).

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

    def test_every_host_is_signed_once_signing_is_on(self, configured, monkeypatch):
        """Cloudflare's review (2026-11-19): Web Bot Auth is our only
        verification method, so EVERY request must carry the signature. A host
        list leaves the rest unattributable, and the old setting is ignored."""
        monkeypatch.setenv("WEB_BOT_AUTH_HOSTS", ".myworkdayjobs.com")
        for url in ("https://acme.wd5.myworkdayjobs.com/x",
                    "https://crawltest.com/cdn-cgi/web-bot-auth",
                    "https://boards-api.greenhouse.io/v1/boards/x",
                    "https://careers.dollartree.com/widgets",
                    "http://example.org/robots.txt"):
            assert wba.should_sign(url), url
        assert not wba.should_sign("ftp://example.org/x")
        assert not wba.should_sign("not a url")


class TestOneName:
    """The bot is submitted as "Aspirely Bot"; its token must match."""

    def test_the_user_agent_names_aspirely_bot_and_links_the_page(self):
        assert wba.BOT_TOKEN == "AspirelyBot"
        assert "AspirelyBot/" in wba.USER_AGENT
        assert wba.USER_AGENT.endswith("+https://aspirely.me/bot)")
        assert "JobPilot" not in wba.USER_AGENT

    def test_every_crawler_path_uses_it(self):
        from services import career_pages_service, jd_fetcher, tenant_discovery, workday_cxs_fetcher
        from services.ats_providers import http as provider_http
        assert w._HTTP_HEADERS["User-Agent"] == wba.USER_AGENT
        assert w._PROBE_HEADERS["User-Agent"] == wba.USER_AGENT
        assert provider_http.DEFAULT_UA == wba.USER_AGENT
        assert workday_cxs_fetcher._HEADERS["User-Agent"] == wba.USER_AGENT
        assert career_pages_service._HEADERS["User-Agent"] == wba.USER_AGENT
        assert jd_fetcher._UA == wba.USER_AGENT
        assert tenant_discovery.USER_AGENT == wba.USER_AGENT

    def test_no_crawler_module_claims_to_be_a_browser_or_the_old_name(self):
        import pathlib
        root = pathlib.Path(wba.__file__).resolve().parent
        offenders = []
        for f in sorted(root.rglob("*.py")):
            if f.name in ("web_bot_auth.py", "browser_executor.py"):
                continue    # the auto-apply browser is a real browser, off in prod
            text = f.read_text(encoding="utf-8")
            for bad in ("Chrome/1", "Safari/537", "JobPilotIngest/", "JobPilotBot/",
                        "jobpilot.app)", "Sec-Fetch-Mode"):
                if bad in text:
                    offenders.append(f"{f.name}: {bad}")
        assert offenders == []

    def test_bot_headers_replace_the_agent_and_any_stale_signature(self, configured):
        h = wba.bot_headers("https://example.org/a", {
            "user-agent": "Mozilla/5.0 Chrome/126", "Accept": "text/html",
            "Signature": "sig1=:old:", "signature-input": "old"})
        assert h["User-Agent"] == wba.USER_AGENT and h["Accept"] == "text/html"
        assert "user-agent" not in h and "signature-input" not in h
        assert h["Signature"] != "sig1=:old:"
        params = h["Signature-Input"][len("sig1="):]
        _verify(_rfc_key(), '"@authority": example.org\n"signature-agent": "https://aspirely.me"\n'
                f'"@signature-params": {params}', h["Signature"])

    def test_bot_headers_without_signing_still_name_the_bot(self, monkeypatch):
        monkeypatch.setenv("WEB_BOT_AUTH_SIGN", "0")
        assert wba.bot_headers("https://example.org/") == {"User-Agent": wba.USER_AGENT}

    def test_robots_rules_for_the_old_name_are_still_obeyed(self):
        from urllib.robotparser import RobotFileParser
        for text in ("User-agent: JobPilotIngest\nDisallow: /\n",
                     "User-agent: AspirelyBot\nDisallow: /\n"):
            parser = RobotFileParser()
            parser.parse(text.splitlines())
            assert not wba.robots_allows(parser, "https://example.org/jobs")
        parser = RobotFileParser()
        parser.parse("User-agent: SomeoneElse\nDisallow: /\n".splitlines())
        assert wba.robots_allows(parser, "https://example.org/jobs")


class TestBotSessionSignsEveryHop:
    def test_each_redirect_hop_is_signed_for_its_own_host(self, configured):
        import requests
        from requests.adapters import BaseAdapter

        seen = []

        class Fake(BaseAdapter):
            def send(self, request, **kw):
                seen.append((request.url, dict(request.headers)))
                resp = requests.Response()
                resp.request, resp.url = request, request.url
                if len(seen) == 1:
                    resp.status_code = 302
                    resp.headers["Location"] = "https://jobs.example.net/board"
                else:
                    resp.status_code = 200
                    resp._content = b"ok"
                return resp

            def close(self):
                pass

        s = wba.BotSession()
        s.mount("https://", Fake())
        r = s.get("https://www.example.com/careers",
                  headers={"User-Agent": "Mozilla/5.0 Chrome/126"})
        assert r.status_code == 200 and len(seen) == 2
        for (url, headers), authority in zip(seen, ("www.example.com", "jobs.example.net")):
            assert headers["User-Agent"] == wba.USER_AGENT
            params = headers["Signature-Input"][len("sig1="):]
            _verify(_rfc_key(), f'"@authority": {authority}\n'
                    '"signature-agent": "https://aspirely.me"\n'
                    f'"@signature-params": {params}', headers["Signature"])


class TestBotPage:
    """Cloudflare's review asked the page (which loads without JavaScript) to
    name the operating company with its jurisdiction and number, and to say
    every request is signed, not that requests "can" be."""

    def _page(self):
        return wba.info_page()

    def test_it_names_the_bot_and_its_token(self):
        page = self._page()
        assert "<h1>Aspirely Bot</h1>" in page and "<code>AspirelyBot</code>" in page
        assert "<script" not in page.lower()

    def test_signed_crawler_says_every_request(self, configured):
        page = self._page()
        assert "Every request it sends carries Web Bot Auth signature headers" in page
        assert " can carry" not in page

    def test_unsigned_crawler_does_not_claim_it(self, configured, monkeypatch):
        monkeypatch.setenv("WEB_BOT_AUTH_SIGN", "0")
        page = self._page()
        assert "Every request" not in page and "not switched on yet" in page

    def test_operator_appears_only_when_all_three_facts_are_set(self, monkeypatch):
        for k in ("OPERATOR", "OPERATOR_JURISDICTION", "OPERATOR_NUMBER", "OPERATOR_ADDRESS"):
            monkeypatch.delenv("WEB_BOT_AUTH_" + k, raising=False)
        monkeypatch.setenv("WEB_BOT_AUTH_OPERATOR", "Example Labs Private Limited")
        assert "operated by" not in self._page()
        monkeypatch.setenv("WEB_BOT_AUTH_OPERATOR_JURISDICTION", "Telangana, India")
        monkeypatch.setenv("WEB_BOT_AUTH_OPERATOR_NUMBER", "U12345TG2026PTC000001")
        page = self._page()
        assert ("Aspirely Bot is operated by <strong>Example Labs Private Limited</strong>, "
                "a company registered in Telangana, India, company number "
                "U12345TG2026PTC000001.") in page

    def test_operator_text_is_escaped(self, monkeypatch):
        monkeypatch.setenv("WEB_BOT_AUTH_OPERATOR", "<b>A & B</b>")
        monkeypatch.setenv("WEB_BOT_AUTH_OPERATOR_JURISDICTION", "X")
        monkeypatch.setenv("WEB_BOT_AUTH_OPERATOR_NUMBER", "1")
        assert "&lt;b&gt;A &amp; B&lt;/b&gt;" in self._page()


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
        slash = c.get("/.well-known/http-message-signatures-directory/", headers={"Host": "aspirely.me"})
        assert slash.status_code == 200 and slash.headers["Content-Type"] == wba.DIRECTORY_CONTENT_TYPE
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

    def test_other_vendors_get_no_cookie_jar_but_are_signed(self, configured, monkeypatch):
        import requests
        sent = []
        monkeypatch.setattr(requests, "get", lambda url, **kw: sent.append(kw) or SimpleNamespace(
            status_code=200, headers={}, json=lambda: {"ok": 1}))
        w.http_get_json("career-pages", "https://boards-api.greenhouse.io/v1/boards/acme/jobs",
                        w.HostPolicy(), deadline=1e12)
        assert "cookies" not in sent[0]
        assert {"Signature", "Signature-Input", "Signature-Agent"} <= set(sent[0]["headers"])
        assert sent[0]["headers"]["User-Agent"] == wba.USER_AGENT

    def test_a_signer_failure_never_blocks_the_request(self, configured, monkeypatch):
        sent = self._capture(monkeypatch)
        monkeypatch.setattr(wba, "sign_request_headers", lambda *a, **k: 1 / 0)
        assert w.http_get_json(w.SOURCE_WORKDAY, self.WD, w.HostPolicy(), deadline=1e12,
                               method="POST") == {"ok": 1}
        assert "Signature" not in sent[0]["headers"]
