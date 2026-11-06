"""Company-logo pipeline — offline test suite.

Covers:
  * logo_key determinism (sha1 of normalized company name);
  * domain resolution ladder (catalog domain -> careers host -> curated
    override -> slug guess);
  * fetch acceptance rules: svg rejected, 512KB cap, mislabeled .ico accepted
    by magic bytes, Google default-globe PNG detected via IHDR width (<32)
    and treated as a miss (secondary site ladder then runs);
  * negative cache written on failure, respected within 30 days, refetch
    eligible after;
  * serving route: bad-format key -> 404 with NO db/service hit; unknown key
    -> 404 and NEVER a fetch (no-doc-no-fetch / no SSRF-by-hash oracle);
    ok doc streams bytes with the immutable cache header; pending doc
    lazily fetches; never 401;
  * stamping: logo_url added to pipeline-shaped rows + guest cards, pending
    company_logos docs upserted; guest scheme guard still strips javascript:
    links and only allows the exact /api/jobs/logo/<sha1> relative path.

Fully offline: mongomock via tests/_vg_support, fetch + S3 monkeypatched.
"""
import hashlib
from datetime import datetime, timedelta, timezone

import pytest

from _vg_support import load_service_app

from services import company_logo_service as logo_svc
from services import guest_service
from services import platform_config_service as pconfig
from utils import db_connect

LOGO_COLLECTIONS = (
    "company_logos",
    "employers",
    "guest_sessions",
    "guest_quota_subjects",
    "guest_global_counters",
    "platform_config",
    "resume_jobs",
    "sponsorship_signals",
)


# ---------------------------------------------------------------------------
# helpers / fixtures
# ---------------------------------------------------------------------------

def _png(width, height=None, tail=b"fake-idat-tail"):
    """Minimal PNG header bytes: signature + IHDR with the given dimensions
    (bytes 16-20 = width big-endian) — enough for the no-Pillow width parse."""
    height = height if height is not None else width
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr_data = width.to_bytes(4, "big") + height.to_bytes(4, "big") + b"\x08\x06\x00\x00\x00"
    return sig + (13).to_bytes(4, "big") + b"IHDR" + ihdr_data + b"\x00\x00\x00\x00" + tail


class FakeResp:
    def __init__(self, status=200, content=b"", content_type="image/png", text=""):
        self.status_code = status
        self.content = content
        self.headers = {"Content-Type": content_type}
        self.text = text

    @property
    def ok(self):
        return self.status_code < 400


class FakeStorage:
    """Stands in for s3_service.get_storage_service() — dict-backed."""

    def __init__(self):
        self.objects = {}
        self._bucket = "unit-test-bucket"

    @property
    def bucket(self):
        return self._bucket

    @property
    def client(self):
        return self

    def put_object(self, Bucket=None, Key=None, Body=None, ContentType=None):
        self.objects[Key] = (Body, ContentType)

    def get_resume(self, s3_key):
        return self.objects[s3_key][0]


@pytest.fixture()
def logo_db():
    """Clean logo-related collections + config cache + the process-global IP
    rate limiter (route tests share one process)."""
    from utils.security import get_rate_limiter
    db = db_connect.DBConnect().get_db()
    for c in LOGO_COLLECTIONS:
        db[c].delete_many({})
    pconfig.invalidate_cache()
    get_rate_limiter()._requests.clear()
    yield db
    pconfig.invalidate_cache()
    get_rate_limiter()._requests.clear()


_REAL_GUESS_VERDICT = logo_svc._guess_verdict


@pytest.fixture(autouse=True)
def _guesses_belong(monkeypatch):
    """A guessed domain's homepage check is a network request; every test
    treats guesses as belonging unless it tests the check itself
    (TestGuessVerdict calls the real function)."""
    monkeypatch.setattr(logo_svc, "_guess_verdict", lambda domain, company, deadline: "yes")


@pytest.fixture()
def fake_s3(monkeypatch):
    import services.s3_service as s3_service
    fake = FakeStorage()
    monkeypatch.setattr(s3_service, "get_storage_service", lambda: fake)
    return fake


@pytest.fixture()
def no_network(monkeypatch):
    """Any outbound fetch attempt fails the test."""
    def _boom(url, deadline=None, **kwargs):
        raise AssertionError(f"unexpected outbound fetch: {url}")
    monkeypatch.setattr(logo_svc, "_guarded_get", _boom)
    return _boom


# ===========================================================================
# Key determinism
# ===========================================================================

class TestLogoKey:
    def test_deterministic_sha1_of_normalized_name(self):
        expected = hashlib.sha1(b"acme corp").hexdigest()
        assert logo_svc.logo_key("Acme Corp") == expected
        assert logo_svc.logo_key("  ACME   corp  ") == expected
        assert logo_svc.logo_key("acme\tcorp") == expected

    def test_shape_is_40_lowercase_hex(self):
        key = logo_svc.logo_key("Stripe")
        assert len(key) == 40
        assert key == key.lower()
        assert all(c in "0123456789abcdef" for c in key)

    def test_distinct_companies_distinct_keys(self):
        assert logo_svc.logo_key("Stripe") != logo_svc.logo_key("Square")


# ===========================================================================
# Domain resolution ladder
# ===========================================================================

class TestDomainResolution:
    def test_catalog_extra_domain_wins(self, logo_db):
        assert logo_svc.resolve_domain("Microsoft") == "microsoft.com"

    def test_careers_host_second(self, logo_db):
        assert logo_svc.resolve_domain("Merck") == "jobs.merck.com"

    def test_curated_override_third(self, logo_db):
        # General Motors: Workday-only catalog rows (no domain/host); the
        # slug guess would be generalmotors.com — the override says gm.com.
        assert logo_svc.resolve_domain("General Motors") == "gm.com"

    def test_slug_guess_last(self, logo_db):
        assert logo_svc.resolve_domain("Stripe") == "stripe.com"

    def test_unknown_company_slugified_guess(self, logo_db):
        assert logo_svc.resolve_domain("Totally Unknown Startup XYZ") == \
            "totallyunknownstartupxyz.com"

    def test_empty_company_no_domain(self, logo_db):
        assert logo_svc.resolve_domain("") == ""


# ===========================================================================
# Fetch acceptance rules (svg / cap / ico magic / Google globe)
# ===========================================================================

class TestAcceptance:
    def test_svg_rejected_by_content_type(self):
        assert logo_svc._accept_image("image/svg+xml", b"<svg></svg>") is None

    def test_svg_body_rejected_even_when_mislabeled(self):
        assert logo_svc._accept_image("image/png", b"<svg onload=alert(1)>") is None
        assert logo_svc._accept_image("image/png", b"<?xml version='1.0'?><svg/>") is None

    def test_unknown_content_type_rejected(self):
        assert logo_svc._accept_image("text/html", b"<html>404</html>") is None

    def test_512kb_cap(self):
        big = _png(128) + b"\x00" * (logo_svc.MAX_LOGO_BYTES + 1)
        assert logo_svc._accept_image("image/png", big) is None
        ok = _png(128)
        assert logo_svc._accept_image("image/png", ok) is not None

    def test_ico_magic_accepted_when_mislabeled(self):
        ico = b"\x00\x00\x01\x00" + b"\x01\x00" + b"rest-of-ico"
        got = logo_svc._accept_image("text/plain", ico)
        assert got == {"content_type": "image/x-icon", "ext": "ico"}

    def test_png_width_parse(self):
        assert logo_svc._png_width(_png(16)) == 16
        assert logo_svc._png_width(_png(128)) == 128
        assert logo_svc._png_width(b"not a png at all, definitely") is None

    def test_google_default_globe_falls_through_to_site(self, monkeypatch):
        calls = []

        def fake_get(url, deadline):
            calls.append(url)
            if url.startswith("https://www.google.com/s2/favicons"):
                return FakeResp(200, _png(16), "image/png")  # default globe
            if url.endswith("/apple-touch-icon.png"):
                return FakeResp(200, _png(180), "image/png")
            return FakeResp(404)

        monkeypatch.setattr(logo_svc, "_guarded_get", fake_get)
        got = logo_svc.fetch_logo_for_domain("acmecorp.com")
        assert got is not None
        assert got["source"] == "site"
        assert logo_svc._png_width(got["data"]) == 180
        assert calls[0].startswith("https://www.google.com/s2/favicons")

    def test_google_real_icon_is_primary(self, monkeypatch):
        def fake_get(url, deadline):
            if url.startswith("https://www.google.com/s2/favicons"):
                return FakeResp(200, _png(128), "image/png")
            raise AssertionError("site ladder must not run when Google hits")

        monkeypatch.setattr(logo_svc, "_guarded_get", fake_get)
        got = logo_svc.fetch_logo_for_domain("stripe.com")
        assert got is not None and got["source"] == "google"

    def test_homepage_link_rel_parse_resolves_relative(self, monkeypatch):
        html = '<html><head><link rel="shortcut icon" href="/static/fav.ico"></head></html>'

        def fake_get(url, deadline):
            if url.startswith("https://www.google.com/s2/favicons"):
                return FakeResp(404)
            if url.endswith("apple-touch-icon.png") or url.endswith("apple-touch-icon-precomposed.png"):
                return FakeResp(404)
            if url == "https://acmecorp.com/":
                return FakeResp(200, b"", "text/html", text=html)
            if url == "https://acmecorp.com/static/fav.ico":
                return FakeResp(200, b"\x00\x00\x01\x00rest", "image/x-icon")
            return FakeResp(404)

        monkeypatch.setattr(logo_svc, "_guarded_get", fake_get)
        got = logo_svc.fetch_logo_for_domain("acmecorp.com")
        assert got is not None
        assert got["ext"] == "ico" and got["source"] == "site"


# ===========================================================================
# get_or_fetch — S3 store, negative cache
# ===========================================================================

class TestGetOrFetch:
    def test_success_writes_ok_doc_and_s3(self, logo_db, fake_s3, monkeypatch):
        monkeypatch.setattr(
            logo_svc, "fetch_logo_for_domain",
            lambda domain, deadline=None: {"data": _png(128), "content_type": "image/png",
                            "ext": "png", "source": "google"},
        )
        res = logo_svc.get_or_fetch("Acme Corp")
        key = logo_svc.logo_key("Acme Corp")
        assert res == {"status": "ok",
                       "s3_key": f"company-logos/v1/{key}.png",
                       "content_type": "image/png"}
        assert fake_s3.objects[res["s3_key"]][1] == "image/png"
        doc = logo_db.company_logos.find_one({"key": key})
        assert doc["status"] == "ok" and doc["source"] == "google"
        assert doc["company"] == "acme corp"
        assert doc["bytes_len"] == len(_png(128))
        assert doc["domain"]  # resolution cached on the doc

    def test_store_failure_stays_pending_not_missing(self, logo_db, monkeypatch):
        """An S3 STORE failure (e.g. bucket unconfigured) is an infra failure,
        not a content miss — it must not poison the 30-day negative cache."""
        monkeypatch.setattr(
            logo_svc, "fetch_logo_for_domain",
            lambda domain, deadline=None: {"data": _png(128), "content_type": "image/png",
                            "ext": "png", "source": "google"},
        )

        def broken_store(key, data, content_type, ext):
            raise ValueError("RESUME_S3_BUCKET is not configured")

        monkeypatch.setattr(logo_svc, "_store_s3", broken_store)
        assert logo_svc.get_or_fetch("Acme Corp") is None
        key = logo_svc.logo_key("Acme Corp")
        doc = logo_db.company_logos.find_one({"key": key})
        assert doc["status"] == "pending"

        # Store comes back (bucket configured): the very next call succeeds —
        # no 30-day negative-cache lockout.
        stored = {}

        def working_store(key, data, content_type, ext):
            s3_key = f"company-logos/v1/{key}.{ext}"
            stored[s3_key] = data
            return s3_key

        monkeypatch.setattr(logo_svc, "_store_s3", working_store)
        res = logo_svc.get_or_fetch("Acme Corp")
        assert res is not None and res["status"] == "ok"
        assert logo_db.company_logos.find_one({"key": key})["status"] == "ok"

    def test_failure_writes_negative_cache_and_respects_it(self, logo_db, monkeypatch):
        calls = []

        def fake_fetch(domain, deadline=None):
            calls.append(domain)
            return None

        monkeypatch.setattr(logo_svc, "fetch_logo_for_domain", fake_fetch)
        assert logo_svc.get_or_fetch("Ghost Startup") is None
        key = logo_svc.logo_key("Ghost Startup")
        doc = logo_db.company_logos.find_one({"key": key})
        assert doc["status"] == "missing" and doc["s3_key"] is None
        assert len(calls) == 1

        # Second call within the 30-day window: negative cache holds, NO fetch.
        assert logo_svc.get_or_fetch("Ghost Startup") is None
        assert len(calls) == 1

    def test_negative_cache_refetch_eligible_after_30_days(self, logo_db, fake_s3, monkeypatch):
        key = logo_svc.logo_key("Ghost Startup")
        logo_db.company_logos.insert_one({
            "key": key, "company": "ghost startup", "domain": "ghoststartup.com",
            "s3_key": None, "content_type": None, "bytes_len": 0,
            "status": "missing", "source": None,
            "fetched_at": datetime.now(timezone.utc) - timedelta(days=31),
        })
        monkeypatch.setattr(
            logo_svc, "fetch_logo_for_domain",
            lambda domain, deadline=None: {"data": _png(64), "content_type": "image/png",
                            "ext": "png", "source": "site"},
        )
        res = logo_svc.get_or_fetch("Ghost Startup")
        assert res is not None and res["status"] == "ok"
        assert logo_db.company_logos.find_one({"key": key})["status"] == "ok"

    def test_empty_company_is_noop(self, logo_db, no_network):
        assert logo_svc.get_or_fetch("") is None
        assert logo_db.company_logos.count_documents({}) == 0


# ===========================================================================
# Serving route — GET /api/jobs/logo/<key>
# ===========================================================================

class TestServingRoute:
    def _client(self):
        return load_service_app("jobs").test_client()

    def test_bad_format_key_404_without_service_hit(self, logo_db, monkeypatch):
        # Recorder (not an exception sentinel): the route's try/except would
        # swallow a raised AssertionError, so we assert on the call list.
        calls = []
        monkeypatch.setattr(
            logo_svc, "get_logo_bytes", lambda key: calls.append(key) or None
        )
        client = self._client()
        for bad in ("nothex", "A" * 40, "a" * 39, "a" * 41, "a" * 39 + "$", "a" * 39 + "."):
            resp = client.get(f"/api/jobs/logo/{bad}")
            assert resp.status_code == 404, bad
        assert calls == []  # format check fires BEFORE any db/service work

    def test_unknown_key_404_and_never_fetches(self, logo_db, monkeypatch):
        calls = []
        monkeypatch.setattr(
            logo_svc, "fetch_logo_for_domain",
            lambda domain, deadline=None: calls.append(domain) or None,
        )
        resp = self._client().get(f"/api/jobs/logo/{'a' * 40}")
        assert resp.status_code == 404
        assert calls == []  # no doc -> NO fetch (no SSRF-by-hash oracle)

    def test_ok_doc_streams_bytes_with_cache_headers(self, logo_db, fake_s3, no_network):
        key = logo_svc.logo_key("Acme Corp")
        s3_key = f"company-logos/v1/{key}.png"
        fake_s3.objects[s3_key] = (_png(128), "image/png")
        logo_db.company_logos.insert_one({
            "key": key, "company": "acme corp", "domain": "acmecorp.com",
            "s3_key": s3_key, "content_type": "image/png",
            "bytes_len": len(_png(128)), "status": "ok", "source": "google",
            "fetched_at": datetime.now(timezone.utc),
        })
        resp = self._client().get(f"/api/jobs/logo/{key}")
        assert resp.status_code == 200
        assert resp.data == _png(128)
        assert resp.headers["Content-Type"].startswith("image/png")
        assert resp.headers["Cache-Control"] == "public, max-age=604800, immutable"

    def test_pending_doc_lazily_fetches_then_serves(self, logo_db, fake_s3, monkeypatch):
        key = logo_svc.logo_key("acme corp")
        logo_db.company_logos.insert_one({
            "key": key, "company": "acme corp", "domain": None,
            "s3_key": None, "content_type": None, "bytes_len": 0,
            "status": "pending", "source": None, "fetched_at": None,
        })
        monkeypatch.setattr(
            logo_svc, "fetch_logo_for_domain",
            lambda domain, deadline=None: {"data": _png(64), "content_type": "image/png",
                            "ext": "png", "source": "site"},
        )
        resp = self._client().get(f"/api/jobs/logo/{key}")
        assert resp.status_code == 200
        assert resp.data == _png(64)
        assert logo_db.company_logos.find_one({"key": key})["status"] == "ok"

    def test_missing_doc_within_window_404_no_fetch(self, logo_db, monkeypatch):
        key = logo_svc.logo_key("ghost startup")
        logo_db.company_logos.insert_one({
            "key": key, "company": "ghost startup", "domain": "ghoststartup.com",
            "s3_key": None, "content_type": None, "bytes_len": 0,
            "status": "missing", "source": None,
            "fetched_at": datetime.now(timezone.utc),
        })
        calls = []
        monkeypatch.setattr(
            logo_svc, "fetch_logo_for_domain",
            lambda domain, deadline=None: calls.append(domain) or None,
        )
        resp = self._client().get(f"/api/jobs/logo/{key}")
        assert resp.status_code == 404
        assert calls == []

    def test_route_never_401(self, logo_db):
        # Public asset — no JWT, and even garbage never triggers a 401.
        client = self._client()
        for path in (f"/api/jobs/logo/{'f' * 40}", "/api/jobs/logo/garbage"):
            assert client.get(path).status_code != 401


# ===========================================================================
# Stamping — pipeline rows + guest cards
# ===========================================================================

class TestStamping:
    def test_stamp_adds_logo_url_and_upserts_pending_docs(self, logo_db, no_network):
        rows = [
            {"title": "SWE", "company": "Acme Corp", "score": 90},
            {"title": "SWE II", "company": "acme  CORP"},   # same normalized company
            {"title": "Mystery", "company": ""},            # no company -> no stamp
            {"title": "Other", "company": "Stripe"},
        ]
        logo_svc.stamp_logo_urls(rows)
        acme_key = logo_svc.logo_key("Acme Corp")
        stripe_key = logo_svc.logo_key("Stripe")
        assert rows[0]["logo_url"] == f"/api/jobs/logo/{acme_key}"
        assert rows[1]["logo_url"] == f"/api/jobs/logo/{acme_key}"
        assert "logo_url" not in rows[2]
        assert rows[3]["logo_url"] == f"/api/jobs/logo/{stripe_key}"
        # Docs upserted (status pending) — the route's fetch entitlement.
        assert logo_db.company_logos.count_documents({}) == 2
        doc = logo_db.company_logos.find_one({"key": acme_key})
        assert doc["status"] == "pending" and doc["company"] == "acme corp"

    def test_stamp_never_disturbs_existing_ok_docs(self, logo_db, no_network):
        key = logo_svc.logo_key("Acme Corp")
        logo_db.company_logos.insert_one({
            "key": key, "company": "acme corp", "domain": "acmecorp.com",
            "s3_key": f"company-logos/v1/{key}.png", "content_type": "image/png",
            "bytes_len": 42, "status": "ok", "source": "google",
            "fetched_at": datetime.now(timezone.utc),
        })
        logo_svc.stamp_logo_urls([{"company": "Acme Corp"}])
        doc = logo_db.company_logos.find_one({"key": key})
        assert doc["status"] == "ok" and doc["s3_key"]  # $setOnInsert only

    def test_discover_search_results_and_partials_are_stamped(self, logo_db, no_network):
        """The interactive Discover searches (workday / career-pages / mnc) are a
        DIFFERENT path from the daily pipeline: their worker branches stream
        partials and complete with {"jobs": [...]}. Both must be stamped, or job
        cards in the app silently lose their company icons."""
        from services.resume_service import _stamp_job_logos

        partial = {"jobs": [{"title": "SWE", "company": "Acme Corp"}], "progress": 12}
        out = _stamp_job_logos(partial)
        acme_key = logo_svc.logo_key("Acme Corp")
        assert out["jobs"][0]["logo_url"] == f"/api/jobs/logo/{acme_key}"
        assert out["progress"] == 12  # untouched passthrough

        result = {"jobs": [{"title": "SWE II", "company": "Stripe"}], "total": 1}
        assert _stamp_job_logos(result)["jobs"][0]["logo_url"] == (
            f"/api/jobs/logo/{logo_svc.logo_key('Stripe')}"
        )

        # Shapes without jobs, and stamping failures, must never break a search.
        assert _stamp_job_logos({"progress": 5}) == {"progress": 5}
        assert _stamp_job_logos({}) == {}
        assert _stamp_job_logos(None) is None

    def test_guest_cards_carry_logo_url(self, logo_db, no_network, monkeypatch):
        monkeypatch.setenv("GUEST_SIGNAL_HMAC_KEY", "unit-test-signal-key")
        pconfig.update_guest_config({"guest_jobs_min_score": 0}, "admin@test")
        s = guest_service.create_or_get_session(None, None, "iphash")
        guest_service.attach_parsed_resume(
            s["guest_id"],
            {
                "contact": {"name": "Jordan Alvarez", "email": "j@example.com",
                            "phone": "", "linkedin": "", "github": ""},
                "summary": "Backend engineer.",
                "skills": {"languages": ["Python", "Go", "SQL"]},
                "experience": [{"title": "Backend Engineer", "company": "Acme",
                                "dates": "2020 - 2024",
                                "bullets": ["Built APIs in Python"]}],
                "education": [], "projects": [], "certifications": [],
            },
            "guest logo test resume " * 5,
            "resume.pdf",
        )
        rows = [{"title": "Python Engineer", "company": "Acme Corp", "location": "US",
                 "apply_link": "https://a.example/1", "source": "Greenhouse",
                 "days_ago": 1, "description": "Python Go SQL backend"}]
        monkeypatch.setattr(guest_service, "_fetch_free_jobs", lambda structured: rows)
        result = guest_service.compute_guest_matches(s["guest_id"])
        assert len(result["jobs"]) == 1
        card = result["jobs"][0]
        assert card["logo_url"] == f"/api/jobs/logo/{logo_svc.logo_key('Acme Corp')}"
        # Doc upserted at cache-build time -> the route may lazily fetch it.
        assert logo_db.company_logos.find_one(
            {"key": logo_svc.logo_key("Acme Corp")}) is not None
        # The cached card keeps the stamp too.
        cached = guest_service.get_cached_matches(s["guest_id"])
        assert cached["jobs"][0]["logo_url"] == card["logo_url"]

    def test_guest_scheme_guard_still_strips_javascript(self, logo_db, no_network, monkeypatch):
        monkeypatch.setenv("GUEST_SIGNAL_HMAC_KEY", "unit-test-signal-key")
        pconfig.update_guest_config({"guest_jobs_min_score": 0}, "admin@test")
        s = guest_service.create_or_get_session(None, None, "iphash")
        guest_service.attach_parsed_resume(
            s["guest_id"],
            {
                "contact": {"name": "Jordan Alvarez", "email": "j@example.com",
                            "phone": "", "linkedin": "", "github": ""},
                "summary": "Backend engineer.",
                "skills": {"languages": ["Python"]},
                "experience": [{"title": "Backend Engineer", "company": "Acme",
                                "dates": "2020 - 2024", "bullets": ["Python"]}],
                "education": [], "projects": [], "certifications": [],
            },
            "guest guard test resume " * 5,
            "resume.pdf",
        )
        rows = [{
            "title": "Python Engineer", "company": None, "location": "US",
            "apply_link": "javascript:alert(1)",         # stripped
            "url": "https://ok.example/job",             # kept
            "logo_url": "javascript:alert(2)",           # stripped (no company to re-stamp)
            "days_ago": 1,
            "source": "Greenhouse", "description": "Python",
        }]
        monkeypatch.setattr(guest_service, "_fetch_free_jobs", lambda structured: rows)
        result = guest_service.compute_guest_matches(s["guest_id"])
        card = result["jobs"][0]
        assert "apply_link" not in card
        assert card["url"] == "https://ok.example/job"
        assert "logo_url" not in card

    def test_safe_logo_url_guard_shapes(self):
        good = f"/api/jobs/logo/{'a' * 40}"
        assert guest_service._safe_logo_url(good) == good
        assert guest_service._safe_logo_url("https://cdn.example/x.png") == \
            "https://cdn.example/x.png"
        assert guest_service._safe_logo_url("javascript:alert(1)") is None
        assert guest_service._safe_logo_url("/api/jobs/logo/../../auth") is None
        assert guest_service._safe_logo_url(f"/api/other/{'a' * 40}") is None
        assert guest_service._safe_logo_url(f"/api/jobs/logo/{'A' * 40}") is None
        assert guest_service._safe_job_url("javascript:alert(1)") is None


# ===========================================================================
# Resolver v2 — candidate domains, ladder grading, refetching old misses
# (2026-11-05: on 123 real employers the v1 resolver found 48 logos; the
# registry's domains found 109, the suffix-stripped guess 94.)
# ===========================================================================

def _ico(*widths):
    """ICO header + one 16-byte directory entry per width (0 byte = 256)."""
    head = b"\x00\x00\x01\x00" + len(widths).to_bytes(2, "little")
    entries = b"".join(bytes([w % 256, w % 256]) + b"\x00" * 14 for w in widths)
    return head + entries + b"image-data"


def _employer(db, employer_id, name, domain=None, verified=(), **extra):
    from services.employer_registry import name_key
    db.employers.insert_one({
        "employer_id": employer_id, "display_name": name, "name_key": name_key(name),
        "canonical_domain": domain, "verified_domains": list(verified or ([domain] if domain else [])),
        "operating_status": "active", **extra,
    })


class TestCandidateDomains:
    def test_legal_tail_stripped_before_guessing(self, logo_db):
        assert logo_svc.resolve_domain("CROWDSTRIKE INC") == "crowdstrike.com"
        assert logo_svc.resolve_domain("SHOPIFY USA INC.") == "shopify.com"
        assert logo_svc.resolve_domain("Deel US LLC") == "deel.com"

    def test_brand_words_are_kept(self, logo_db):
        # "group" is part of the brand, and words inside the name stay.
        assert logo_svc.resolve_domain("Zillow Group") == "zillowgroup.com"
        assert logo_svc._core_name("bank of america") == "bank of america"
        assert logo_svc._core_name("jpmorgan chase & co.") == "jpmorgan chase"

    def test_leading_the_is_dropped_first(self, logo_db):
        # thedowchemical.com is someone else's site (measured 2026-11-05).
        got = [d for d, _ in logo_svc.candidate_domains("THE DOW CHEMICAL COMPANY", use_registry=False)]
        assert got[:2] == ["dowchemical.com", "thedowchemical.com"]

    def test_descriptor_dropped_as_a_later_guess(self, logo_db):
        got = [d for d, _ in logo_svc.candidate_domains("ZENSAR TECHNOLOGIES INC", use_registry=False)]
        assert got == ["zensartechnologies.com", "zensar.com"]
        # Too short a remainder is someone else's domain: never guessed.
        got = [d for d, _ in logo_svc.candidate_domains("NCC Group", use_registry=False)]
        assert got == ["nccgroup.com"]

    def test_domain_written_in_the_name(self, logo_db):
        got = logo_svc.candidate_domains("Amazon.com Services LLC", use_registry=False)
        assert got[0] == ("amazon.com", "name")

    def test_ampersand_and_leading_the_get_alternatives(self, logo_db):
        got = [d for d, _ in logo_svc.candidate_domains("The Home Depot", use_registry=False)]
        assert "homedepot.com" in got
        got = [d for d, _ in logo_svc.candidate_domains("Barnes & Noble, Inc.", use_registry=False)]
        assert "barnesandnoble.com" in got

    def test_vendor_careers_host_is_never_a_logo_domain(self, logo_db):
        # JPMorgan's catalog careers host is jpmc.fa.oraclecloud.com, whose
        # icon is ORACLE's.
        got = logo_svc.candidate_domains("JPMorgan Chase", use_registry=False)
        assert all("oraclecloud" not in d for d, _ in got)
        assert got[0] == ("jpmorganchase.com", "override")
        assert logo_svc._is_vendor_host("acme.wd5.myworkdayjobs.com")
        assert not logo_svc._is_vendor_host("careers.acme.com")

    def test_registry_domain_by_canonical_id_comes_first(self, logo_db):
        _employer(logo_db, "emp_aaaaaaaaaaaa", "Texas Instruments", "ti.com")
        got = logo_svc.candidate_domains("Texas Instruments", "emp_aaaaaaaaaaaa")
        assert got[0] == ("ti.com", "registry")
        assert ("texasinstruments.com", "guess") in got

    def test_registry_domain_by_unique_name(self, logo_db):
        _employer(logo_db, "emp_bbbbbbbbbbbb", "Amgen", "amgen.com")
        assert logo_svc.candidate_domains("AMGEN INC")[0] == ("amgen.com", "registry")

    def test_ambiguous_registry_name_answers_nothing(self, logo_db):
        _employer(logo_db, "emp_cccccccccccc", "Forward", "forward.com")
        _employer(logo_db, "emp_dddddddddddd", "Forward", "goforward.com")
        got = logo_svc.candidate_domains("Forward")
        assert all(b != "registry" for _, b in got)

    def test_brand_without_domain_borrows_parent(self, logo_db):
        _employer(logo_db, "emp_parent00000", "Salesforce", "salesforce.com")
        _employer(logo_db, "emp_brand000000", "Tableau", None,
                  operating_status="acquired", parent_employer_id="emp_parent00000")
        got = logo_svc.candidate_domains("Tableau", "emp_brand000000")
        assert got[0] == ("salesforce.com", "registry")


class TestLadderGrading:
    def test_ico_width_is_the_widest_entry(self):
        assert logo_svc._image_width(_ico(16, 32, 48)) == 48
        assert logo_svc._image_width(_ico(0)) == 256          # 0 byte means 256

    def test_known_placeholder_is_never_a_logo(self, monkeypatch):
        fake = _png(48)
        monkeypatch.setattr(logo_svc, "PLACEHOLDER_SHA1",
                            frozenset({hashlib.sha1(fake).hexdigest()}))
        assert logo_svc._grade(fake) is None
        assert logo_svc._grade(_png(48, tail=b"other")) == "good"

    def test_real_placeholder_hashes_are_listed(self):
        assert "2d7c9b60d1e2b4f4726141de2e4ab738110b9287" in logo_svc.PLACEHOLDER_SHA1
        assert "980aa215c45dd3b92f40b272234a21f6d850b14a" in logo_svc.PLACEHOLDER_SHA1

    def test_duckduckgo_answers_when_google_has_nothing(self, monkeypatch):
        calls = []

        def fake_get(url, deadline):
            calls.append(url)
            if url.startswith("https://icons.duckduckgo.com/ip3/rockwellautomation.com"):
                return FakeResp(200, _png(64), "image/png")
            return FakeResp(404, _png(16), "image/png")

        monkeypatch.setattr(logo_svc, "_guarded_get", fake_get)
        got = logo_svc.fetch_logo_for_domain("rockwellautomation.com")
        assert got["source"] == "duckduckgo"
        assert len(calls) == 2   # google, then duckduckgo; the site is never asked

    def test_favicon_ico_is_asked_before_the_homepage(self, monkeypatch):
        def fake_get(url, deadline):
            if url == "https://zensar.com/favicon.ico":
                return FakeResp(200, _ico(32), "image/x-icon")
            if url == "https://zensar.com/":
                return FakeResp(200, b"", "text/html", text="<html>no icon links</html>")
            return FakeResp(404)

        monkeypatch.setattr(logo_svc, "_guarded_get", fake_get)
        got = logo_svc.fetch_logo_for_domain("zensar.com")
        assert got["source"] == "site" and got["ext"] == "ico"

    def test_small_real_icon_is_kept_when_nothing_larger_exists(self, monkeypatch):
        # Google 200s a real 16px icon (tsmc.com, measured): v1 called that a
        # miss because the "no icon" globe used to be a 16px 200.
        def fake_get(url, deadline):
            if url.startswith("https://www.google.com/s2/favicons"):
                return FakeResp(200, _png(16), "image/png")
            return FakeResp(404)

        monkeypatch.setattr(logo_svc, "_guarded_get", fake_get)
        got = logo_svc.fetch_logo_for_domain("tsmc.com")
        assert got is not None and got["source"] == "google"
        assert logo_svc._png_width(got["data"]) == 16

    def test_tiny_pixel_is_not_a_logo(self, monkeypatch):
        monkeypatch.setattr(logo_svc, "_guarded_get",
                            lambda url, deadline: FakeResp(200, _png(1), "image/png"))
        assert logo_svc.fetch_logo_for_domain("pixel.example") is None


class TestResolverV2Fetching:
    def _ok_fetch(self, good_domain, calls):
        def fake(domain, deadline=None):
            calls.append(domain)
            if domain == good_domain:
                return {"data": _png(64), "content_type": "image/png",
                        "ext": "png", "source": "google"}
            return None
        return fake

    def test_candidates_tried_in_order_and_basis_recorded(self, logo_db, fake_s3, monkeypatch):
        _employer(logo_db, "emp_aaaaaaaaaaaa", "Texas Instruments", "ti.com")
        calls = []
        monkeypatch.setattr(logo_svc, "fetch_logo_for_domain",
                            self._ok_fetch("texasinstruments.com", calls))
        res = logo_svc.get_or_fetch("Texas Instruments", canonical_employer_id="emp_aaaaaaaaaaaa")
        assert res and res["status"] == "ok"
        assert calls == ["ti.com", "texasinstruments.com"]
        doc = logo_db.company_logos.find_one({"key": logo_svc.logo_key("Texas Instruments")})
        assert doc["domain"] == "texasinstruments.com" and doc["domain_basis"] == "guess"
        assert doc["resolver_version"] == logo_svc.RESOLVER_VERSION
        assert doc["canonical_employer_id"] == "emp_aaaaaaaaaaaa"

    def test_stored_wrong_domain_is_not_reused(self, logo_db, fake_s3, monkeypatch):
        key = logo_svc.logo_key("CrowdStrike Inc")
        logo_db.company_logos.insert_one({
            "key": key, "company": "crowdstrike inc", "domain": "crowdstrikeinc.com",
            "status": "missing", "s3_key": None,
            "fetched_at": datetime.now(timezone.utc) - timedelta(days=31),
        })
        calls = []
        monkeypatch.setattr(logo_svc, "fetch_logo_for_domain",
                            self._ok_fetch("crowdstrike.com", calls))
        assert logo_svc.get_or_fetch("CrowdStrike Inc")["status"] == "ok"
        assert "crowdstrikeinc.com" not in calls

    def test_miss_records_every_domain_tried(self, logo_db, monkeypatch):
        monkeypatch.setattr(logo_svc, "fetch_logo_for_domain", lambda domain, deadline=None: None)
        assert logo_svc.get_or_fetch("The Home Depot") is None
        doc = logo_db.company_logos.find_one({"key": logo_svc.logo_key("The Home Depot")})
        assert doc["status"] == "missing" and doc["resolver_version"] == logo_svc.RESOLVER_VERSION
        assert "homedepot.com" in doc["domains_tried"]

    def test_force_bypasses_negative_cache(self, logo_db, fake_s3, monkeypatch):
        key = logo_svc.logo_key("Ghost Startup")
        logo_db.company_logos.insert_one({
            "key": key, "company": "ghost startup", "status": "missing",
            "s3_key": None, "fetched_at": datetime.now(timezone.utc),
        })
        calls = []
        monkeypatch.setattr(logo_svc, "fetch_logo_for_domain",
                            self._ok_fetch("ghoststartup.com", calls))
        assert logo_svc.get_or_fetch("Ghost Startup") is None and calls == []
        assert logo_svc.get_or_fetch("Ghost Startup", force=True)["status"] == "ok"

    def test_serving_route_does_not_refetch_an_old_resolver_miss(self, logo_db, monkeypatch):
        """Old misses are refetched by refresh_due in the background, never
        inline on a page load (the gateway has 32 threads)."""
        key = logo_svc.logo_key("Ghost Startup")
        logo_db.company_logos.insert_one({
            "key": key, "company": "ghost startup", "status": "missing",
            "s3_key": None, "fetched_at": datetime.now(timezone.utc),
        })
        monkeypatch.setattr(logo_svc, "fetch_logo_for_domain",
                            lambda domain, deadline=None: (_ for _ in ()).throw(AssertionError("fetched")))
        assert logo_svc.get_logo_bytes(key) is None


class TestStampEmployerId:
    def test_unanimous_canonical_id_is_remembered(self, logo_db, no_network):
        logo_svc.stamp_logo_urls([
            {"company": "Texas Instruments", "canonical_employer_id": "emp_aaaaaaaaaaaa"},
            {"company": "texas instruments", "canonical_employer_id": "emp_aaaaaaaaaaaa"},
            {"company": "Contested Co", "canonical_employer_id": "emp_111111111111"},
            {"company": "Contested Co", "canonical_employer_id": "emp_222222222222"},
        ])
        ti = logo_db.company_logos.find_one({"key": logo_svc.logo_key("Texas Instruments")})
        assert ti["canonical_employer_id"] == "emp_aaaaaaaaaaaa" and ti["status"] == "pending"
        contested = logo_db.company_logos.find_one({"key": logo_svc.logo_key("Contested Co")})
        assert "canonical_employer_id" not in contested


class TestRefreshDue:
    def _seed(self, db, name, status, **extra):
        doc = {"key": logo_svc.logo_key(name), "company": logo_svc.normalize_company(name),
               "status": status, "s3_key": None, "fetched_at": datetime.now(timezone.utc)}
        doc.update(extra)
        db.company_logos.insert_one(doc)

    def test_picks_what_is_due_and_skips_what_is_current(self, logo_db, fake_s3, monkeypatch):
        now = datetime.now(timezone.utc)
        self._seed(logo_db, "Pending Co", "pending", fetched_at=None)
        self._seed(logo_db, "CROWDSTRIKE INC", "missing")                       # v1 miss
        self._seed(logo_db, "Fresh Miss", "missing", resolver_version=logo_svc.RESOLVER_VERSION)
        self._seed(logo_db, "Old Miss", "missing", resolver_version=logo_svc.RESOLVER_VERSION,
                   fetched_at=now - timedelta(days=31))
        calls = []

        def fake(domain, deadline=None):
            calls.append(domain)
            return {"data": _png(64), "content_type": "image/png", "ext": "png", "source": "google"}

        monkeypatch.setattr(logo_svc, "fetch_logo_for_domain", fake)
        res = logo_svc.refresh_due(budget_seconds=60, workers=1)
        assert res["picked"] == 3
        assert res["outcomes"] == {"missing_expired:ok": 1, "missing_old_resolver:ok": 1,
                                   "pending:ok": 1}
        assert "crowdstrike.com" in calls and "freshmiss.com" not in calls
        assert logo_db.company_logos.count_documents({"status": "ok"}) == 3
        # Nothing is due any more except the fresh miss's 30-day wait.
        assert logo_svc.refresh_due(budget_seconds=60)["picked"] == 0

    def test_ok_logo_replaced_only_by_proven_domain(self, logo_db, fake_s3, monkeypatch):
        _employer(logo_db, "emp_aaaaaaaaaaaa", "Texas Instruments", "ti.com")
        self._seed(logo_db, "Texas Instruments", "ok", domain="texasinstruments.com",
                   s3_key="company-logos/v1/old.png", content_type="image/png",
                   canonical_employer_id="emp_aaaaaaaaaaaa")
        self._seed(logo_db, "Guess Only Co", "ok", domain="guessonly.com",
                   s3_key="company-logos/v1/g.png", content_type="image/png")
        calls = []

        def fake(domain, deadline=None):
            calls.append(domain)
            return {"data": _png(64), "content_type": "image/png", "ext": "png", "source": "google"}

        monkeypatch.setattr(logo_svc, "fetch_logo_for_domain", fake)
        res = logo_svc.refresh_due(budget_seconds=60, workers=1)
        assert res["outcomes"] == {"ok_recheck:kept": 1, "ok_recheck:replaced": 1}
        assert calls == ["ti.com"]   # a guess-only company is never refetched
        ti = logo_db.company_logos.find_one({"key": logo_svc.logo_key("Texas Instruments")})
        assert ti["domain"] == "ti.com" and ti["domain_basis"] == "registry"
        assert logo_db.company_logos.count_documents(
            {"resolver_version": logo_svc.RESOLVER_VERSION}) == 2

    def test_recheck_that_finds_nothing_keeps_the_working_logo(self, logo_db, fake_s3, monkeypatch):
        _employer(logo_db, "emp_aaaaaaaaaaaa", "Texas Instruments", "ti.com")
        self._seed(logo_db, "Texas Instruments", "ok", domain="texasinstruments.com",
                   s3_key="company-logos/v1/old.png", content_type="image/png")
        monkeypatch.setattr(logo_svc, "fetch_logo_for_domain", lambda domain, deadline=None: None)
        res = logo_svc.refresh_due(budget_seconds=60, workers=1)
        assert res["outcomes"] == {"ok_recheck:kept": 1}
        doc = logo_db.company_logos.find_one({"key": logo_svc.logo_key("Texas Instruments")})
        assert doc["status"] == "ok" and doc["s3_key"] == "company-logos/v1/old.png"

    def test_budget_spent_defers_the_rest(self, logo_db, monkeypatch):
        self._seed(logo_db, "Pending Co", "pending", fetched_at=None)
        monkeypatch.setattr(logo_svc, "fetch_logo_for_domain",
                            lambda domain, deadline=None: (_ for _ in ()).throw(AssertionError("fetched")))
        res = logo_svc.refresh_due(budget_seconds=-1)
        assert res["outcomes"] == {"pending:deferred": 1}
        assert logo_db.company_logos.find_one({})["status"] == "pending"

    def test_store_failure_is_reported_and_stays_pending(self, logo_db, monkeypatch):
        self._seed(logo_db, "Pending Co", "pending", fetched_at=None)
        monkeypatch.setattr(logo_svc, "fetch_logo_for_domain",
                            lambda domain, deadline=None: {"data": _png(64), "content_type": "image/png",
                                                           "ext": "png", "source": "google"})
        monkeypatch.setattr(logo_svc, "_store_s3",
                            lambda *a: (_ for _ in ()).throw(ValueError("no bucket")))
        res = logo_svc.refresh_due(budget_seconds=60)
        assert res["outcomes"] == {"pending:store_failed": 1}


class TestLogoRefreshSchedule:
    def test_scheduler_runs_logo_refresh_every_15_minutes(self):
        import importlib.util
        from pathlib import Path
        path = Path(__file__).resolve().parents[1] / "services" / "scheduler" / "runner.py"
        spec = importlib.util.spec_from_file_location("sched_runner_logo", path)
        runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner)
        assert runner.EVERY["logo_refresh"] == 15

    def test_dispatch_routes_to_refresh_due(self, monkeypatch):
        from services import scheduled_jobs
        seen = {}
        monkeypatch.setattr(logo_svc, "refresh_due",
                            lambda **kw: seen.update(kw) or {"picked": 0})
        out = scheduled_jobs.dispatch_scheduled("logo_refresh")
        assert out == {"job": "logo_refresh", "result": {"picked": 0}}
        assert seen["budget_seconds"] < 15 * 60


class TestGuessVerdict:
    """A guessed domain must answer, must not be a parking/sale page, and must
    not redirect away from the brand (all measured 2026-11-05)."""

    class Page:
        def __init__(self, url, status=200, text="<html>ok</html>"):
            self.url, self.status_code, self.text = url, status, text
            self.headers = {"Content-Type": "text/html"}

        @property
        def ok(self):
            return self.status_code < 400

    def _check(self, monkeypatch, page, domain, company):
        monkeypatch.setattr(logo_svc, "_guarded_get", lambda url, deadline: page)
        return _REAL_GUESS_VERDICT(domain, company, deadline=10**12)

    def test_nothing_answering_is_unknown(self, monkeypatch):
        assert self._check(monkeypatch, None, "bestbuy.com", "best buy co inc") == "unknown"

    def test_parking_hosts_are_not_theirs(self, monkeypatch):
        for host in ("https://gcd.com/protected-domain-holder/", "https://www.hugedomains.com/x",
                     "https://www.atom.com/name/c3ai"):
            assert self._check(monkeypatch, self.Page(host), "x.com", "x inc") == "no"

    def test_for_sale_text_is_not_theirs(self, monkeypatch):
        page = self.Page("https://acme.com/", text="<h1>This domain is for sale!</h1>")
        assert self._check(monkeypatch, page, "acme.com", "acme inc") == "no"

    def test_redirect_away_from_the_brand_is_not_theirs(self, monkeypatch):
        page = self.Page("https://atmos.earth/political-landscapes/meet-the-women/")
        assert self._check(monkeypatch, page, "thedowchemical.com", "the dow chemical company") == "no"
        page = self.Page("https://www.worldwidewebfx.com/")
        assert self._check(monkeypatch, page, "zoomcommunications.com", "zoom communications inc") == "no"

    @pytest.mark.parametrize("company,guess,final", [
        ("the dow chemical company", "dowchemical.com", "www.dow.com"),
        ("wsp usa inc", "wspusa.com", "www.wsp.com"),
        ("electronic arts", "electronicarts.com", "www.ea.com"),
        ("educational testing service", "educationaltestingservice.com", "www.ets.org"),
        ("the bank of new york mellon", "bankofnewyorkmellon.com", "www.bny.com"),
        ("genentech usa inc", "genentech.com", "www.gene.com"),
        ("schneider electric usa inc", "schneiderelectric.com", "www.se.com"),
        ("the sherwin-williams company", "sherwinwilliams.com", "www.sherwin-williams.com"),
    ])
    def test_redirect_to_the_brand_is_theirs(self, monkeypatch, company, guess, final):
        assert self._check(monkeypatch, self.Page(f"https://{final}/"), guess, company) == "yes"

    def test_bot_wall_is_an_answer(self, monkeypatch):
        page = self.Page("https://www.crowdstrike.com/", status=403, text="Access denied")
        assert self._check(monkeypatch, page, "crowdstrike.com", "crowdstrike inc") == "yes"
        page = self.Page("https://validate.perfdrive.com/?ssa=1")
        assert self._check(monkeypatch, page, "persistent.com", "persistent systems inc") == "yes"

    def _fake_fetch(self, calls, png):
        def fake(domain, deadline=None):
            calls.append(domain)
            return {"data": png, "content_type": "image/png", "ext": "png", "source": "google"}
        return fake

    def test_rejected_guess_is_skipped_for_the_next_candidate(self, logo_db, fake_s3, monkeypatch):
        monkeypatch.setattr(logo_svc, "_guess_verdict", lambda domain, company, deadline:
                            "no" if domain == "thedowchemical.com" else "yes")
        calls = []
        monkeypatch.setattr(logo_svc, "fetch_logo_for_domain", self._fake_fetch(calls, _png(64)))
        monkeypatch.setattr(logo_svc, "candidate_domains", lambda name, cid=None: [
            ("thedowchemical.com", "guess"), ("dowchemical.com", "guess")])
        assert logo_svc.get_or_fetch("The Dow Chemical Company")["status"] == "ok"
        assert calls == ["dowchemical.com"]

    def test_unknown_guess_needs_a_full_size_service_icon(self, logo_db, fake_s3, monkeypatch):
        monkeypatch.setattr(logo_svc, "_guess_verdict", lambda domain, company, deadline: "unknown")
        calls = []
        monkeypatch.setattr(logo_svc, "fetch_logo_for_domain", self._fake_fetch(calls, _png(16)))
        assert logo_svc.get_or_fetch("Thermo Fisher Scientific Inc") is None   # stale 16px icon
        monkeypatch.setattr(logo_svc, "fetch_logo_for_domain", self._fake_fetch(calls, _png(48)))
        assert logo_svc.get_or_fetch("Best Buy Co Inc", force=True)["status"] == "ok"

    def test_proven_domains_skip_the_check(self, logo_db, fake_s3, monkeypatch):
        monkeypatch.setattr(logo_svc, "_guess_verdict",
                            lambda *a: (_ for _ in ()).throw(AssertionError("checked")))
        calls = []
        monkeypatch.setattr(logo_svc, "fetch_logo_for_domain", self._fake_fetch(calls, _png(16)))
        assert logo_svc.get_or_fetch("General Motors")["status"] == "ok"   # gm.com, override
        assert calls == ["gm.com"]
