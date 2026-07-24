"""SSRF guard for services.jd_fetcher (guest-onboarding security review §2).

The JD URL is untrusted user input on BOTH the guest tailor path and the
pre-existing logged-in /api/jobs/fetch-jd + tailor-by-url path. The guard:

  * scheme must be http/https;
  * the host — literal IP or EVERY resolved address — must be public
    (rejects loopback, RFC1918, link-local incl. 169.254.169.254, ::1,
    fc00::/7, fe80::/10, 0.0.0.0, reserved, multicast, v4-mapped v6);
  * redirects are followed manually (allow_redirects=False) with each hop
    re-validated before it is fetched.

Fully offline: DNS (socket.getaddrinfo) and HTTP (requests.get) are
monkeypatched — no network, matching the suite posture in tests/_vg_support.
"""
import sys

import pytest

import _vg_support  # noqa: F401 - bootstraps sys.path + offline env

from services import jd_fetcher as jf

PUBLIC_IP = "93.184.216.34"  # example.com
PRIVATE_IP = "10.0.0.5"


def _addrinfo(ip):
    """Minimal getaddrinfo-shaped result for one address."""
    import socket
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    return [(family, socket.SOCK_STREAM, 6, "", (ip, 443))]


class FakeResponse:
    def __init__(self, status_code=200, text="", headers=None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}

    @property
    def ok(self):
        return self.status_code < 400


# ===========================================================================
# url_is_safe — literal IPs, schemes, DNS resolution
# ===========================================================================

class TestUrlIsSafe:
    @pytest.mark.parametrize("url", [
        "http://127.0.0.1/admin",
        "http://127.1.2.3/",
        "http://10.0.0.5/x",
        "http://172.16.3.4/",
        "http://172.31.255.255/",
        "http://192.168.1.1/",
        "http://169.254.169.254/latest/meta-data/",  # AWS metadata endpoint
        "http://169.254.0.99/",
        "http://0.0.0.0/",
        "http://[::1]/",
        "http://[fe80::1]/",
        "http://[fc00::1]/",
        "http://[fdff:ffff::1]/",
        "http://[::ffff:127.0.0.1]/",  # v4-mapped loopback
        "http://[::ffff:10.0.0.5]/",   # v4-mapped private
        "http://[::]/",
    ])
    def test_private_and_metadata_literals_rejected(self, url):
        allowed, reason = jf.url_is_safe(url)
        assert allowed is False, url
        assert reason

    @pytest.mark.parametrize("url", [
        "file:///etc/passwd",
        "ftp://internal.host/x",
        "gopher://internal.host/x",
        "javascript:alert(1)",
        "data:text/html,x",
        "//no-scheme.example.com/x",
        "http://",       # no host
        "",
        None,
    ])
    def test_disallowed_schemes_and_shapes_rejected(self, url):
        allowed, _ = jf.url_is_safe(url)
        assert allowed is False

    def test_public_literal_ip_allowed(self):
        allowed, reason = jf.url_is_safe(f"https://{PUBLIC_IP}/jobs/1")
        assert allowed is True and reason == ""

    def test_hostname_resolving_to_private_rejected(self, monkeypatch):
        # DNS-rebinding shape: a public-looking name pointing at RFC1918.
        monkeypatch.setattr(jf.socket, "getaddrinfo",
                            lambda *a, **k: _addrinfo(PRIVATE_IP))
        allowed, reason = jf.url_is_safe("https://evil.example.com/job")
        assert allowed is False and reason == "resolves_to_private_ip"

    def test_hostname_resolving_to_metadata_rejected(self, monkeypatch):
        monkeypatch.setattr(jf.socket, "getaddrinfo",
                            lambda *a, **k: _addrinfo("169.254.169.254"))
        allowed, _ = jf.url_is_safe("https://metadata.example.com/")
        assert allowed is False

    def test_mixed_resolution_rejected_if_any_private(self, monkeypatch):
        # A name with one public + one private A record must be rejected.
        monkeypatch.setattr(
            jf.socket, "getaddrinfo",
            lambda *a, **k: _addrinfo(PUBLIC_IP) + _addrinfo(PRIVATE_IP),
        )
        allowed, _ = jf.url_is_safe("https://dual.example.com/")
        assert allowed is False

    def test_public_hostname_allowed(self, monkeypatch):
        monkeypatch.setattr(jf.socket, "getaddrinfo",
                            lambda *a, **k: _addrinfo(PUBLIC_IP))
        allowed, reason = jf.url_is_safe("https://boards.greenhouse.io/acme/jobs/123")
        assert allowed is True and reason == ""

    def test_dns_failure_fails_closed(self, monkeypatch):
        import socket as real_socket

        def boom(*a, **k):
            raise real_socket.gaierror("NXDOMAIN")

        monkeypatch.setattr(jf.socket, "getaddrinfo", boom)
        allowed, reason = jf.url_is_safe("https://does-not-exist.example/")
        assert allowed is False and reason == "dns_failure"


# ===========================================================================
# _safe_get — redirect hops re-validated, public fetches still work
# ===========================================================================

class TestSafeGet:
    def test_public_fetch_passes_through(self, monkeypatch):
        monkeypatch.setattr(jf.socket, "getaddrinfo",
                            lambda *a, **k: _addrinfo(PUBLIC_IP))
        calls = []

        def fake_get(url, **kwargs):
            calls.append((url, kwargs))
            return FakeResponse(200, "<html><body>Great job</body></html>")

        monkeypatch.setattr(jf.requests, "get", fake_get)
        r = jf._safe_get("https://careers.example.com/job/1")
        assert r is not None and r.ok
        assert len(calls) == 1
        assert calls[0][1]["allow_redirects"] is False  # manual redirect follow

    def test_redirect_to_metadata_ip_blocked(self, monkeypatch):
        monkeypatch.setattr(jf.socket, "getaddrinfo",
                            lambda *a, **k: _addrinfo(PUBLIC_IP))
        calls = []

        def fake_get(url, **kwargs):
            calls.append(url)
            return FakeResponse(
                302, "", headers={"Location": "http://169.254.169.254/latest/meta-data/"}
            )

        monkeypatch.setattr(jf.requests, "get", fake_get)
        r = jf._safe_get("https://careers.example.com/job/1")
        assert r is None                 # hop blocked
        assert calls == ["https://careers.example.com/job/1"]  # metadata never fetched

    def test_redirect_to_private_host_blocked(self, monkeypatch):
        monkeypatch.setattr(jf.socket, "getaddrinfo",
                            lambda *a, **k: _addrinfo(PUBLIC_IP))

        def fake_get(url, **kwargs):
            return FakeResponse(301, "", headers={"Location": "http://127.0.0.1:8080/"})

        monkeypatch.setattr(jf.requests, "get", fake_get)
        assert jf._safe_get("https://careers.example.com/job/1") is None

    def test_redirect_to_public_host_followed(self, monkeypatch):
        monkeypatch.setattr(jf.socket, "getaddrinfo",
                            lambda *a, **k: _addrinfo(PUBLIC_IP))
        calls = []

        def fake_get(url, **kwargs):
            calls.append(url)
            if len(calls) == 1:
                return FakeResponse(302, "", headers={"Location": "https://jobs.example.com/1"})
            return FakeResponse(200, "<html>JD text here</html>")

        monkeypatch.setattr(jf.requests, "get", fake_get)
        r = jf._safe_get("https://careers.example.com/job/1")
        assert r is not None and r.ok
        assert calls == ["https://careers.example.com/job/1", "https://jobs.example.com/1"]

    def test_redirect_loop_bounded(self, monkeypatch):
        monkeypatch.setattr(jf.socket, "getaddrinfo",
                            lambda *a, **k: _addrinfo(PUBLIC_IP))
        calls = []

        def fake_get(url, **kwargs):
            calls.append(url)
            return FakeResponse(302, "", headers={"Location": "https://a.example.com/loop"})

        monkeypatch.setattr(jf.requests, "get", fake_get)
        assert jf._safe_get("https://a.example.com/loop") is None
        assert len(calls) <= jf._MAX_REDIRECTS + 1


# ===========================================================================
# fetch_jd — entrypoint gate + a normal public JD still extracts
# ===========================================================================

class TestFetchJd:
    def test_blocked_url_short_circuits(self, monkeypatch):
        def no_fetch(*a, **k):  # pragma: no cover - must never run
            raise AssertionError("outbound fetch attempted for blocked URL")

        monkeypatch.setattr(jf.requests, "get", no_fetch)
        assert jf.fetch_jd("http://169.254.169.254/latest/meta-data/") == ("", "blocked_url")
        assert jf.fetch_jd("file:///etc/passwd") == ("", "blocked_url")
        assert jf.fetch_jd("http://10.0.0.5/jobs/1") == ("", "blocked_url")

    def test_public_company_page_still_fetches(self, monkeypatch):
        # Representative public careers page (generic HTML strip path).
        monkeypatch.setattr(jf.socket, "getaddrinfo",
                            lambda *a, **k: _addrinfo(PUBLIC_IP))
        html = "<html><body><h1>Backend Engineer</h1><p>Build Python APIs at scale.</p></body></html>"
        monkeypatch.setattr(jf.requests, "get",
                            lambda url, **kw: FakeResponse(200, html))
        text, kind = jf.fetch_jd("https://careers.example.com/openings/backend-123")
        assert kind == "html_scrape"
        assert "Backend Engineer" in text and "Python APIs" in text

    def test_public_ats_url_passes_guard(self, monkeypatch):
        # Greenhouse-hosted board (the logged-in tailor path's #1 source):
        # the guard must not block it when DNS resolves public.
        monkeypatch.setattr(jf.socket, "getaddrinfo",
                            lambda *a, **k: _addrinfo(PUBLIC_IP))
        seen = []

        class JsonResponse(FakeResponse):
            def json(self):
                return {"content": "<p>Own the roadmap</p>"}

        monkeypatch.setattr(jf.requests, "get",
                            lambda url, **kw: (seen.append(url) or JsonResponse(200)))
        text, kind = jf.fetch_jd("https://boards.greenhouse.io/acme/jobs/4001")
        assert kind == "greenhouse_api"
        assert "Own the roadmap" in text
        assert seen and seen[-1].startswith("https://boards-api.greenhouse.io/")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
