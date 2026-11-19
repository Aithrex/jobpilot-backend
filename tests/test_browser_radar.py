"""Browser Radar (`radar-1`): Workday listings read by the owner's browser.

Since ~2026-11-14 the crawler's box is behind Workday's bot wall; a person's
own browser is not. These tests pin the server half of the contract
(docs/specs/browser-radar.md):

* the plan names the user's own Workday employers first, and boards the
  crawler cannot reach only for contributors;
* a batch is mapped by the crawler's OWN provider code (byte-identical rows),
  written only for known boards, ON by default (dry_run and off by env),
  never closes anything,
  and is idempotent and rate limited.
"""
import time
from datetime import datetime, timedelta, timezone

import pytest

from _vg_support import current_db, load_service_app

from services import browser_radar_service as radar
from services import job_ingest_worker as w
from services import job_inventory_service as inv

CONTRIB = "contrib@example.com"
OTHER = "someone@example.com"
NOW = datetime.now(timezone.utc)

BODY_HTML = ("<p>Requirements: 3+ years of Python and AWS. Bachelor's degree in Computer "
             "Science. You will build backend services and APIs.</p>") * 20

COLLECTIONS = ("job_postings", "job_source_state", "job_ingestion_runs", "job_parse_queue",
               radar.BATCHES, radar.USAGE, radar.UNKNOWN_BOARDS,
               "board_requests", "board_snapshots", "users")

MEMBERS = [
    {"member_key": t, "source": w.SOURCE_WORKDAY, "vendor": "workday", "tenant": t,
     "wd_host": "wd5", "site": "External", "display_name": name, "industry": "tech"}
    for t, name in (("acme", "Acme Corp"), ("beta", "Beta Inc"), ("gamma", "Gamma LLC"))
]


@pytest.fixture()
def db(monkeypatch):
    d = current_db()
    for c in COLLECTIONS:
        d[c].delete_many({})
    radar.reset_stats_cache()
    monkeypatch.setattr(w, "registry_members",
                        lambda source: list(MEMBERS) if w.normalize_source(source) == w.SOURCE_WORKDAY else [])
    monkeypatch.setenv("BROWSER_RADAR_CONTRIBUTORS", CONTRIB)
    monkeypatch.delenv("BROWSER_RADAR_INGEST", raising=False)
    monkeypatch.delenv("BROWSER_RADAR_ENABLED", raising=False)
    yield d
    for c in COLLECTIONS:
        d[c].delete_many({})
    radar.reset_stats_cache()


def _posting(req="JR1001", title="Senior Backend Engineer", posted="Posted Today",
             place="US, CA, Santa Clara"):
    return {"title": title,
            "externalPath": f"/job/US-CA-Santa-Clara/{title.replace(' ', '-')}_{req}",
            "bulletFields": [req], "postedOn": posted, "locationsText": place,
            "timeType": "Full time"}


def _detail(posting, body=BODY_HTML, start=None):
    info = {"title": posting["title"], "jobDescription": body,
            "location": posting["locationsText"], "timeType": "Full time"}
    if start:
        info["startDate"] = start
    return {"externalPath": posting["externalPath"], "jobPostingInfo": info}


def _batch(postings, *, tenant="acme", details=(), batch_id=None, site="External",
           wd_host="wd5", domain="myworkdayjobs.com", extra_boards=()):
    return {
        "batch_id": batch_id or f"batch-{time.monotonic_ns()}",
        "client": {"name": "applypilot", "version": "0.4.0"},
        "observed_at": NOW.isoformat(),
        "boards": [{"tenant": tenant, "wd_host": wd_host, "site": site, "domain": domain,
                    "search_text": "backend engineer", "postings": list(postings),
                    "details": list(details)}, *extra_boards],
    }


def _sjid(req, tenant="acme"):
    return f"{tenant}:{req}"


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------

class TestPlan:
    def test_no_data_is_an_empty_plan_not_an_error(self, db):
        plan = radar.build_plan(OTHER)
        assert plan["plan_version"] == "radar-1"
        assert plan["enabled"] is True
        assert plan["ingest_mode"] == "on"
        assert plan["contributor"] is False
        assert plan["search_terms"] == []
        assert plan["tasks"] == []
        assert plan["limits"] == {"max_requests_per_run": 40, "max_requests_per_day": 300,
                                  "min_interval_ms": 6000, "max_details_per_run": 15}

    def _seed(self, d, email):
        d.board_requests.insert_one({"_id": email, "params": {
            "titles": ["Senior Backend Engineer", "Data Engineer", "Engineer"]}})
        d.board_snapshots.insert_one({"_id": f"{email}|workday", "result": {"jobs": [
            {"tenant": "acme", "ats": "workday", "match_score": 80},
            {"tenant": "acme", "ats": "workday", "match_score": 70},
            {"tenant": "beta", "ats": "workday", "match_score": 90},
            {"tenant": "elsewhere", "ats": "greenhouse", "match_score": 99},
        ]}})
        old = NOW - timedelta(days=2)
        rows = ([("acme", NOW)] + [("beta", old)] + [("gamma", old)] * 3)
        d.job_postings.insert_many([
            {"source": "workday", "source_job_id": f"{t}:{i}", "status": "active",
             "tenant": t, "last_seen_at": seen} for i, (t, seen) in enumerate(rows)])

    def test_contributor_gets_relevant_and_stale_in_order(self, db):
        self._seed(db, CONTRIB)
        plan = radar.build_plan(CONTRIB)
        assert plan["contributor"] is True
        assert plan["search_terms"] == ["backend engineer", "data engineer"]
        got = [(t["tenant"], t["search_text"], t["why"]) for t in plan["tasks"]]
        assert got == [
            ("beta", "backend engineer", "both"), ("beta", "data engineer", "both"),
            ("acme", "backend engineer", "relevant"), ("acme", "data engineer", "relevant"),
            ("gamma", "", "stale"),
        ]
        task = plan["tasks"][0]
        assert task == {"tenant": "beta", "wd_host": "wd5", "site": "External",
                        "domain": "myworkdayjobs.com", "employer": "Beta Inc",
                        "search_text": "backend engineer", "max_pages": 1, "why": "both"}

    def test_stale_boards_are_for_contributors_only(self, db):
        self._seed(db, OTHER)
        plan = radar.build_plan(OTHER)
        assert plan["contributor"] is False
        assert {t["why"] for t in plan["tasks"]} == {"relevant"}
        assert [t["tenant"] for t in plan["tasks"]] == ["acme", "acme", "beta", "beta"]

    def test_limit_counts_boards(self, db):
        self._seed(db, CONTRIB)
        plan = radar.build_plan(CONTRIB, limit=1)
        assert {t["tenant"] for t in plan["tasks"]} == {"beta"}

    def test_kill_switch(self, db, monkeypatch):
        self._seed(db, CONTRIB)
        monkeypatch.setenv("BROWSER_RADAR_ENABLED", "0")
        plan = radar.build_plan(CONTRIB)
        assert plan["enabled"] is False and plan["tasks"] == [] and plan["search_terms"] == []

    def test_admin_flag_makes_a_contributor(self, db):
        db.users.insert_one({"email": OTHER, "is_admin": True})
        assert radar.is_contributor(OTHER) is True
        assert radar.is_contributor("nobody@example.com") is False

    def test_unset_mode_is_on(self, monkeypatch):
        monkeypatch.delenv("BROWSER_RADAR_INGEST", raising=False)
        assert radar.ingest_mode() == "on"

    def test_a_mode_typo_is_dry_run(self, monkeypatch):
        monkeypatch.setenv("BROWSER_RADAR_INGEST", "ON!")
        assert radar.ingest_mode() == "dry_run"


# ---------------------------------------------------------------------------
# Mapping is the crawler's own
# ---------------------------------------------------------------------------

def test_browser_row_equals_the_crawler_row(db, monkeypatch):
    """The same raw CXS posting through the crawl's provider path and through
    the browser path produces the same listing row."""
    from services.ats_providers.base import FetchContext

    posting = _posting(posted="Posted 2 Days Ago")

    def fake_ctx(member, *, cutoff, deadline, stop, session=None):
        return FetchContext(
            get_json=lambda *a, **k: (404, None), get_text=lambda *a, **k: (404, ""),
            post_json=lambda url, **k: (200, {"jobPostings": [posting], "total": 1}),
            deadline=deadline, stop=stop,
            cutoff_iso=cutoff.astimezone(timezone.utc).isoformat())

    monkeypatch.setattr(w, "_provider_fetch_context", fake_ctx)
    member = dict(MEMBERS[0])
    cutoff = NOW - timedelta(days=30)
    crawled = w._list_via_provider(member, cutoff=cutoff, deadline=time.monotonic() + 10)
    assert len(crawled) == 1

    job = radar.map_posting(dict(MEMBERS[0]), radar._clean_posting(posting))
    browser = w._raw_job_to_listing(dict(MEMBERS[0]), job, cutoff=cutoff)
    # A relative date ("Posted 2 Days Ago") is read against the clock, so the
    # two reads differ by the microseconds between them; everything else is equal.
    crawled_at, browser_at = crawled[0].pop("posted_at"), browser.pop("posted_at")
    assert abs((crawled_at - browser_at).total_seconds()) < 5
    assert browser == crawled[0]
    assert browser["source_job_id"] == "acme:JR1001"
    assert browser["url"] == ("https://acme.wd5.myworkdayjobs.com/External/job/"
                              "US-CA-Santa-Clara/Senior-Backend-Engineer_JR1001")


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------

class TestIngest:
    def test_dry_run_writes_nothing_but_says_what_it_would(self, db, monkeypatch):
        monkeypatch.setenv("BROWSER_RADAR_INGEST", "dry_run")
        p1, p2 = _posting("JR1"), _posting("JR2", title="Data Engineer")
        out = radar.ingest_batch(CONTRIB, _batch([p1, p2], details=[_detail(p1)]))
        assert out["mode"] == "dry_run"
        assert out["boards"] == {"accepted": 1, "unknown": 0}
        assert out["postings"]["accepted"] == 2
        assert out["postings"]["created"] == 0
        assert out["would"] == {"created": 2, "updated": 0, "touched": 0}
        assert db.job_postings.count_documents({}) == 0
        assert db[radar.BATCHES].count_documents({}) == 1

    def test_on_creates_with_full_body_from_the_detail(self, db, monkeypatch):
        monkeypatch.setenv("BROWSER_RADAR_INGEST", "on")
        p1, p2 = _posting("JR1"), _posting("JR2", title="Data Engineer")
        out = radar.ingest_batch(CONTRIB, _batch([p1, p2], details=[_detail(p1, start="2026-11-10")]))
        assert out["mode"] == "on"
        assert out["postings"]["created"] == 2
        full = inv.get_posting("workday", _sjid("JR1"))
        teaser = inv.get_posting("workday", _sjid("JR2"))
        assert full["status"] == "active" and full["body_state"] == "full"
        assert int(full.get("description_chars") or 0) > 500
        assert full["detail_ref"].startswith("https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/External/job/")
        # `startDate` is an exact date and replaces "Posted Today" (as the crawl does).
        assert full["posted_at_confidence"] == inv.CONFIDENCE_EXACT
        assert teaser["body_state"] == "teaser"

    def test_unchanged_active_posting_is_only_touched(self, db, monkeypatch):
        monkeypatch.setenv("BROWSER_RADAR_INGEST", "on")
        p1 = _posting("JR1")
        radar.ingest_batch(CONTRIB, _batch([p1], details=[_detail(p1)]), now=NOW)
        first = inv.get_posting("workday", _sjid("JR1"))
        later = NOW + timedelta(hours=3)
        out = radar.ingest_batch(CONTRIB, _batch([p1]), now=later)
        assert out["postings"]["touched"] == 1
        assert out["postings"]["updated"] == out["postings"]["created"] == 0
        after = inv.get_posting("workday", _sjid("JR1"))
        assert after["content_hash"] == first["content_hash"]
        assert after["body_state"] == "full"
        seen = after["last_seen_at"]
        seen = seen if seen.tzinfo else seen.replace(tzinfo=timezone.utc)
        assert seen >= later - timedelta(seconds=1)

    def test_a_closed_posting_reopens(self, db, monkeypatch):
        monkeypatch.setenv("BROWSER_RADAR_INGEST", "on")
        p1 = _posting("JR1")
        radar.ingest_batch(CONTRIB, _batch([p1], details=[_detail(p1)]))
        assert inv.close_posting("workday", _sjid("JR1"), reason="unverified", now=NOW)
        assert inv.get_posting("workday", _sjid("JR1"))["status"] != "active"
        out = radar.ingest_batch(CONTRIB, _batch([p1]))
        assert out["postings"]["reopened"] == 1
        assert inv.get_posting("workday", _sjid("JR1"))["status"] == "active"

    def test_a_teaser_gets_its_body_from_a_later_detail(self, db, monkeypatch):
        monkeypatch.setenv("BROWSER_RADAR_INGEST", "on")
        p1 = _posting("JR1")
        radar.ingest_batch(CONTRIB, _batch([p1]))
        assert inv.get_posting("workday", _sjid("JR1"))["body_state"] == "teaser"
        out = radar.ingest_batch(CONTRIB, _batch([p1], details=[_detail(p1)]))
        assert out["postings"]["updated"] == 1
        assert inv.get_posting("workday", _sjid("JR1"))["body_state"] == "full"

    def test_a_browser_batch_never_closes_anything(self, db, monkeypatch):
        monkeypatch.setenv("BROWSER_RADAR_INGEST", "on")
        p1, p2 = _posting("JR1"), _posting("JR2", title="Data Engineer")
        radar.ingest_batch(CONTRIB, _batch([p1, p2]))
        # A later batch that sees only one of them closes neither.
        radar.ingest_batch(CONTRIB, _batch([p1]))
        statuses = {d["source_job_id"]: d["status"] for d in db.job_postings.find({})}
        assert statuses == {_sjid("JR1"): "active", _sjid("JR2"): "active"}

    def test_old_postings_age_out(self, db, monkeypatch):
        monkeypatch.setenv("BROWSER_RADAR_INGEST", "on")
        out = radar.ingest_batch(CONTRIB, _batch([_posting("JR9", posted="Posted 30+ Days Ago")]))
        assert out["postings"]["aged_out"] == 1 and out["postings"]["accepted"] == 0
        assert db.job_postings.count_documents({}) == 0

    def test_unknown_board_is_recorded_never_ingested(self, db, monkeypatch):
        monkeypatch.setenv("BROWSER_RADAR_INGEST", "on")
        out = radar.ingest_batch(CONTRIB, _batch([_posting("JR1")], tenant="zzz"))
        assert out["boards"] == {"accepted": 0, "unknown": 1}
        assert db.job_postings.count_documents({}) == 0
        doc = db[radar.UNKNOWN_BOARDS].find_one({})
        assert doc["tenant"] == "zzz" and doc["sightings"] == 1 and doc["postings"] == 1

    def test_a_known_tenant_on_another_site_is_unknown(self, db):
        out = radar.ingest_batch(CONTRIB, _batch([_posting("JR1")], site="Careers"))
        assert out["boards"]["unknown"] == 1

    def test_bad_postings_are_rejected_not_fatal(self, db):
        bad = [{"title": "x", "externalPath": "https://evil.example/job/x"},
               {"title": "y", "externalPath": "/job/../../admin"}, "nope"]
        out = radar.ingest_batch(CONTRIB, _batch([_posting("JR1"), *bad]))
        assert out["postings"]["rejected"] == 3
        assert out["postings"]["accepted"] == 1

    def test_repeated_batch_id_returns_the_first_answer(self, db, monkeypatch):
        monkeypatch.setenv("BROWSER_RADAR_INGEST", "on")
        body = _batch([_posting("JR1")], batch_id="fixed-batch-0001")
        first = radar.ingest_batch(CONTRIB, body)
        again = radar.ingest_batch(CONTRIB, body)
        assert again == first and first["postings"]["created"] == 1
        assert db[radar.BATCHES].count_documents({}) == 1
        usage = db[radar.USAGE].find_one({"_id": {"$regex": r"\|d\|"}})
        assert usage["postings"] == 1


def test_default_mode_writes(db):
    """Unset `BROWSER_RADAR_INGEST` is `on`: a contributor's batch is written."""
    out = radar.ingest_batch(CONTRIB, _batch([_posting("JR7")]))
    assert out["mode"] == "on" and out["postings"]["created"] == 1
    assert inv.get_posting("workday", _sjid("JR7"))["status"] == "active"


def test_default_mode_still_refuses_non_contributors(db):
    with pytest.raises(radar.RadarError) as e:
        radar.ingest_batch(OTHER, _batch([_posting("JR8")]))
    assert e.value.status == 403
    assert current_db().job_postings.count_documents({}) == 0


class TestRefusals:
    def test_not_a_contributor(self, db):
        with pytest.raises(radar.RadarError) as e:
            radar.ingest_batch(OTHER, _batch([_posting()]))
        assert (e.value.status, e.value.code) == (403, "not_contributor")

    def test_ingest_off(self, db, monkeypatch):
        monkeypatch.setenv("BROWSER_RADAR_INGEST", "off")
        with pytest.raises(radar.RadarError) as e:
            radar.ingest_batch(CONTRIB, _batch([_posting()]))
        assert (e.value.status, e.value.code) == (409, "ingest_off")

    @pytest.mark.parametrize("mutate", [
        lambda b: b.update(batch_id="short"),
        lambda b: b.update(boards=[]),
        lambda b: b.update(boards=[dict(b["boards"][0]) for _ in range(41)]),
        lambda b: b["boards"][0].update(postings=[_posting(f"R{i}") for i in range(101)]),
        lambda b: b["boards"][0].update(tenant="bad tenant"),
        lambda b: b["boards"][0].update(wd_host="evil.com"),
        lambda b: b["boards"][0].update(domain="example.com"),
        lambda b: b["boards"][0].update(details=[_detail(_posting(), body="x" * 200_001)]),
    ])
    def test_bounds_and_shape(self, db, mutate):
        body = _batch([_posting()])
        mutate(body)
        with pytest.raises(radar.RadarError) as e:
            radar.ingest_batch(CONTRIB, body)
        assert (e.value.status, e.value.code) == (400, "invalid_batch")

    def test_total_postings_bound(self, db):
        boards = [{"tenant": "acme", "wd_host": "wd5", "site": "External",
                   "postings": [_posting(f"R{b}-{i}") for i in range(100)]} for b in range(11)]
        with pytest.raises(radar.RadarError) as e:
            radar.ingest_batch(CONTRIB, {"batch_id": "batch-total-1", "boards": boards})
        assert e.value.status == 400

    def test_rate_limited_per_hour(self, db, monkeypatch):
        monkeypatch.setattr(radar, "BATCHES_PER_HOUR", 1)
        radar.ingest_batch(CONTRIB, _batch([_posting()]))
        with pytest.raises(radar.RadarError) as e:
            radar.ingest_batch(CONTRIB, _batch([_posting()]))
        assert (e.value.status, e.value.code) == (429, "rate_limited")
        assert 0 < e.value.retry_after <= 3600

    def test_rate_limited_per_day(self, db, monkeypatch):
        monkeypatch.setattr(radar, "POSTINGS_PER_DAY", 2)
        radar.ingest_batch(CONTRIB, _batch([_posting("A1"), _posting("A2")]))
        with pytest.raises(radar.RadarError) as e:
            radar.ingest_batch(CONTRIB, _batch([_posting("A3")]))
        assert e.value.status == 429


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@pytest.fixture()
def app():
    return load_service_app("jobs")


def _call(app, email, method, path, json=None, data=None):
    from flask_jwt_extended import create_access_token
    with app.app_context():
        tok = create_access_token(identity=email)
    c = app.test_client()
    kwargs = {"headers": {"Authorization": f"Bearer {tok}"}}
    if json is not None:
        kwargs["json"] = json
    if data is not None:
        kwargs["data"] = data
        kwargs["content_type"] = "application/json"
    return getattr(c, method)(path, **kwargs)


class TestRoutes:
    def test_plan_route(self, db, app):
        r = _call(app, OTHER, "get", "/api/jobs/browser-radar/plan")
        assert r.status_code == 200
        assert r.get_json()["plan_version"] == "radar-1"

    def test_plan_needs_a_token(self, db, app):
        assert app.test_client().get("/api/jobs/browser-radar/plan").status_code == 401

    def test_listings_route_dry_run(self, db, app, monkeypatch):
        monkeypatch.setenv("BROWSER_RADAR_INGEST", "dry_run")
        r = _call(app, CONTRIB, "post", "/api/jobs/browser-radar/listings",
                  json=_batch([_posting("JR1")]))
        assert r.status_code == 200
        body = r.get_json()
        assert body["mode"] == "dry_run" and body["would"]["created"] == 1

    def test_listings_refusals_are_never_401(self, db, app, monkeypatch):
        r = _call(app, OTHER, "post", "/api/jobs/browser-radar/listings",
                  json=_batch([_posting("JR1")]))
        assert r.status_code == 403 and r.get_json()["error"] == "not_contributor"
        r = _call(app, CONTRIB, "post", "/api/jobs/browser-radar/listings", json={"boards": []})
        assert r.status_code == 400 and r.get_json()["error"] == "invalid_batch"
        monkeypatch.setattr(radar, "BATCHES_PER_HOUR", 0)
        r = _call(app, CONTRIB, "post", "/api/jobs/browser-radar/listings",
                  json=_batch([_posting("JR1")]))
        assert r.status_code == 429
        assert r.get_json()["retry_after_seconds"] > 0
        assert r.headers["Retry-After"]
        monkeypatch.setenv("BROWSER_RADAR_INGEST", "off")
        r = _call(app, CONTRIB, "post", "/api/jobs/browser-radar/listings",
                  json=_batch([_posting("JR1")]))
        assert r.status_code == 409

    def test_body_over_2mb(self, db, app):
        r = _call(app, CONTRIB, "post", "/api/jobs/browser-radar/listings",
                  data="x" * (radar.MAX_BODY_BYTES + 1))
        assert r.status_code == 400 and r.get_json()["error"] == "invalid_batch"
