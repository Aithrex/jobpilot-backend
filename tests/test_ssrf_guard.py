"""SSRF containment tests for the auto-apply browser-execution plane (audit P0-1).

Fully offline: DNS (``socket.getaddrinfo``) is injected via the guard's
``resolver`` seam — no network. Proves, per the task:

  * a metadata IP / localhost / RFC1918 / file: URL is blocked;
  * a DNS-rebinding host (public NAME -> private A record) is blocked;
  * an allow-listed ATS host passes both the envelope gate and the interceptor;
  * the Playwright ``context.route`` handler ABORTS a denied request and
    CONTINUES an allowed one.
"""
import socket

import pytest

import _vg_support  # noqa: F401 - bootstraps sys.path + offline env

from services import ssrf_guard as sg

PUBLIC_IP = "93.184.216.34"       # example.com
PRIVATE_IP = "10.0.0.5"


def _addrinfo(*ips):
    """getaddrinfo-shaped result for one or more addresses."""
    out = []
    for ip in ips:
        family = socket.AF_INET6 if ":" in ip else socket.AF_INET
        out.append((family, socket.SOCK_STREAM, 6, "", (ip, 443)))
    return out


def _resolver(*ips):
    return lambda host, port, **kw: _addrinfo(*ips)


# ===========================================================================
# Envelope gate — apply_url_allowed (https + ATS suffix-boundary allow-list)
# ===========================================================================
class TestApplyUrlAllowed:
    @pytest.mark.parametrize("url", [
        "https://boards.greenhouse.io/aithrex/jobs/4200",
        "https://job-boards.greenhouse.io/aithrex/jobs/4200",
        "https://grnh.se/abc123",
        "https://jobs.lever.co/aithrex/aabbccddeeff",
        "https://jobs.ashbyhq.com/aithrex/1234abcd/application",
        "https://aithrex.wd1.myworkdayjobs.com/en-US/careers/job/1234",
    ])
    def test_supported_ats_hosts_pass(self, url):
        allowed, reason = sg.apply_url_allowed(url)
        assert allowed is True, (url, reason)
        assert reason == ""

    @pytest.mark.parametrize("url", [
        "http://boards.greenhouse.io/aithrex/jobs/4200",   # not https
        "https://evilgreenhouse.io/steal",                  # suffix boundary
        "https://greenhouse.io.evil.com/steal",             # suffix boundary
        "https://careers.example-co.test/apply/eng",        # not an ATS host
        "https://169.254.169.254/latest/meta-data/",        # literal IP never listed
        "file:///etc/passwd",
        "data:text/html,x",
        "",
        None,
    ])
    def test_non_ats_or_non_https_rejected(self, url):
        allowed, reason = sg.apply_url_allowed(url)
        assert allowed is False
        assert reason


# ===========================================================================
# Interceptor gate — validate_request_url (scheme + IP + DNS-rebinding pin)
# ===========================================================================
class TestValidateRequestUrl:
    @pytest.mark.parametrize("url", [
        "http://127.0.0.1/admin",
        "http://10.0.0.5/x",
        "http://172.16.3.4/",
        "http://192.168.1.1/",
        "http://169.254.169.254/latest/meta-data/",   # AWS/GCP metadata (link-local)
        "http://0.0.0.0/",
        "http://[::1]/",
        "http://[fe80::1]/",
        "http://[fc00::1]/",
        "http://[fd00:ec2::254]/",                      # IPv6 metadata (unique-local)
        "http://[::ffff:127.0.0.1]/",                   # v4-mapped loopback
        "http://[::ffff:10.0.0.5]/",                    # v4-mapped private
    ])
    def test_private_and_metadata_literals_blocked(self, url):
        allowed, reason = sg.validate_request_url(url)
        assert allowed is False, url
        assert reason

    @pytest.mark.parametrize("url", [
        "file:///etc/passwd",
        "data:text/html,<script>x</script>",
        "blob:https://x/uuid",
        "javascript:alert(1)",
        "about:blank",
        "chrome://settings",
        "//no-scheme.example.com/x",
        "",
        None,
    ])
    def test_dangerous_schemes_blocked(self, url):
        allowed, _ = sg.validate_request_url(url)
        assert allowed is False

    def test_public_literal_ip_allowed(self):
        allowed, reason = sg.validate_request_url(f"https://{PUBLIC_IP}/x")
        assert allowed is True and reason == ""

    def test_rebinding_public_name_to_private_blocked(self):
        # DNS rebinding: a public-LOOKING host that resolves to RFC1918.
        allowed, reason = sg.validate_request_url(
            "https://boards.greenhouse.io/aithrex/jobs/1",
            resolver=_resolver(PRIVATE_IP))
        assert allowed is False and reason == "resolves_to_private_ip"

    def test_rebinding_to_metadata_blocked(self):
        allowed, _ = sg.validate_request_url(
            "https://careers.example.com/", resolver=_resolver("169.254.169.254"))
        assert allowed is False

    def test_mixed_resolution_blocked_if_any_private(self):
        allowed, _ = sg.validate_request_url(
            "https://dual.example.com/", resolver=_resolver(PUBLIC_IP, PRIVATE_IP))
        assert allowed is False

    def test_allowlisted_ats_host_resolving_public_passes(self):
        allowed, reason = sg.validate_request_url(
            "https://boards.greenhouse.io/aithrex/jobs/4200",
            resolver=_resolver(PUBLIC_IP))
        assert allowed is True and reason == ""

    def test_dns_failure_fails_closed(self):
        def boom(*a, **k):
            raise socket.gaierror("NXDOMAIN")
        allowed, reason = sg.validate_request_url(
            "https://does-not-exist.example/", resolver=boom)
        assert allowed is False and reason == "dns_failure"


# ===========================================================================
# Playwright route handler — abort denied, continue allowed
# ===========================================================================
class _FakeRequest:
    def __init__(self, url):
        self.url = url


class _FakeRoute:
    def __init__(self, url):
        self.request = _FakeRequest(url)
        self.aborted = False
        self.continued = False

    def abort(self):
        self.aborted = True

    def continue_(self):
        self.continued = True


class TestRouteHandler:
    def test_handler_aborts_metadata_request(self):
        handler = sg.make_route_handler()
        route = _FakeRoute("http://169.254.169.254/latest/meta-data/")
        handler(route, route.request)
        assert route.aborted is True and route.continued is False

    def test_handler_aborts_file_scheme(self):
        handler = sg.make_route_handler()
        route = _FakeRoute("file:///etc/passwd")
        handler(route, route.request)
        assert route.aborted is True

    def test_handler_aborts_rebinding_host(self):
        handler = sg.make_route_handler(resolver=_resolver(PRIVATE_IP))
        route = _FakeRoute("https://boards.greenhouse.io/aithrex/jobs/1")
        handler(route, route.request)
        assert route.aborted is True

    def test_handler_continues_allowlisted_public_host(self):
        handler = sg.make_route_handler(resolver=_resolver(PUBLIC_IP))
        route = _FakeRoute("https://boards.greenhouse.io/aithrex/jobs/4200")
        handler(route, route.request)
        assert route.continued is True and route.aborted is False

    def test_handler_reads_request_off_route_when_not_passed(self):
        handler = sg.make_route_handler(resolver=_resolver(PUBLIC_IP))
        route = _FakeRoute("https://jobs.lever.co/aithrex/x/apply")
        handler(route)                      # Playwright single-arg signature
        assert route.continued is True
