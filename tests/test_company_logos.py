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
            lambda domain: {"data": _png(128), "content_type": "image/png",
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
            lambda domain: {"data": _png(128), "content_type": "image/png",
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

        def fake_fetch(domain):
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
            lambda domain: {"data": _png(64), "content_type": "image/png",
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
            lambda domain: calls.append(domain) or None,
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
            lambda domain: {"data": _png(64), "content_type": "image/png",
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
            lambda domain: calls.append(domain) or None,
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
                 "description": "Python Go SQL backend"}]
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
