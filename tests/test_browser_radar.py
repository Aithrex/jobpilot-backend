"""Browser Radar (`radar-1`): Workday listings read by the owner's browser.

Since ~2026-11-14 the crawler's box is behind Workday's bot wall; a person's
own browser is not. These tests pin the server half of the contract
(docs/specs/browser-radar.md):

* the plan names the user's own Workday employers first, and boards the
  crawler cannot reach only for contributors;
* a batch is mapped the way the PRODUCTION crawl maps it (the legacy Workday
  listing path, driven for real with a fake fetch, gives the same document),
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
SECOND = "second@example.com"
NOW = datetime.now(timezone.utc)

BODY_HTML = ("<p>Requirements: 3+ years of Python and AWS. Bachelor's degree in Computer "
             "Science. You will build backend services and APIs.</p>") * 20

COLLECTIONS = ("job_postings", "job_source_state", "job_ingestion_runs", "job_parse_queue",
               "posting_sightings", radar.BATCHES, radar.USAGE, radar.UNKNOWN_BOARDS,
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
    monkeypatch.setenv("BROWSER_RADAR_CONTRIBUTORS", f"{CONTRIB},{SECOND}")
    # These tests cover the `listed` gate; the default (`all`) is tested below.
    monkeypatch.setenv("BROWSER_RADAR_CONTRIBUTORS_SCOPE", "listed")
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

def _production_crawl(monkeypatch, member, posting, *, body_html="", now=NOW):
    """Run the PRODUCTION Workday crawl over one board with a fake fetch.

    Nothing of the crawl is stubbed but the HTTP call itself: `run_shard`
    picks the listing path (`list_member`), which for Workday is the legacy
    `_list_workday` (the vendor is in `_LEGACY_LISTING_VENDORS`), and the
    legacy branch of `fetch_detail`. Returns the URLs it asked for."""
    asked = []

    def fake_http(source, url, policy, *, deadline=None, stop=None, timeout=None,
                  method="GET", json_body=None, **_kw):
        asked.append((method, url))
        if method == "POST":
            first = (json_body or {}).get("offset", 0) == 0
            return {"jobPostings": [dict(posting)] if first else [], "total": 1}
        if not body_html:
            return None
        return {"jobPostingInfo": {"jobDescription": body_html, "startDate": "2020-01-02",
                                   "location": "Somewhere Else", "timeType": "Part time"}}

    monkeypatch.setattr(w, "http_get_json", fake_http)
    monkeypatch.setitem(w.SHARD_COUNTS, w.SOURCE_WORKDAY, 1)
    monkeypatch.setattr(
        w, "registry_members",
        lambda source: [dict(member)] if w.normalize_source(source) == w.SOURCE_WORKDAY else [])
    out = w.run_shard(w.SOURCE_WORKDAY, w.shard_of(w.SOURCE_WORKDAY, member["member_key"]),
                      now=now)
    assert "error" not in out, out
    return asked


#: The only fields a browser-written posting and a crawl-written one may
#: differ in. Each is bookkeeping about WHO wrote the row, never content.
_WRITER_ONLY_FIELDS = {
    "_id",
    "origin",               # set on a posting a browser created
    "last_ingest_run_id",   # set by a crawl run; a browser write has no run
}


def test_the_production_crawl_lists_workday_through_the_legacy_path():
    """The parity tests below compare against the path production runs."""
    assert "workday" in w._LEGACY_LISTING_VENDORS
    assert w._uses_legacy_listing(dict(MEMBERS[0])) is True


def test_browser_row_equals_the_crawler_row(db, monkeypatch):
    """The same raw CXS posting through the production crawl and through a
    browser batch is the same LISTING ROW, key for key."""
    posting = _posting(posted="Posted 2 Days Ago")
    captured = []
    real = w._list_workday

    def spy(member, **kw):
        rows = real(member, **kw)
        captured.extend(dict(r) for r in rows)
        return rows

    monkeypatch.setattr(w, "_list_workday", spy)
    asked = _production_crawl(monkeypatch, dict(MEMBERS[0]), posting)
    assert asked[0] == ("POST", "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/External/jobs")
    assert len(captured) == 1

    browser = radar.map_posting(dict(MEMBERS[0]), radar._clean_posting(posting),
                                cutoff=NOW - timedelta(days=30), clock=NOW, now=NOW)
    assert browser == captured[0]
    assert browser["source_job_id"] == "acme:JR1001"
    assert browser["posted_at"] == NOW - timedelta(days=2)
    assert browser["url"] == ("https://acme.wd5.myworkdayjobs.com/External/job/"
                              "US-CA-Santa-Clara/Senior-Backend-Engineer_JR1001")
    # The crawl keeps the bare path, not a full CXS URL.
    assert browser["_detail_ref"] == "job/US-CA-Santa-Clara/Senior-Backend-Engineer_JR1001"


@pytest.mark.parametrize("with_body", [True, False])
def test_browser_document_equals_the_crawled_document(db, monkeypatch, with_body):
    """What reaches `job_postings` is the same document either way: every
    field but the three that say who wrote it."""
    posting = _posting(posted="Posted Yesterday")
    html = ("<div><p>About the role &amp; team.</p><ul><li>5+ years of Python</li>"
            "<li>AWS &rsquo;cloud&rsquo; experience&nbsp;required</li></ul>"
            "<p>Bachelor's degree in Computer Science.</p></div>") * 12
    asked = _production_crawl(monkeypatch, dict(MEMBERS[0]), posting,
                              body_html=html if with_body else "")
    # The legacy detail fetch: the bare path on the board's own CXS base.
    assert ("GET", "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/External/job/"
                   "US-CA-Santa-Clara/Senior-Backend-Engineer_JR1001") in asked
    crawled = inv.get_posting("workday", _sjid("JR1001"))
    assert crawled is not None
    assert crawled["body_state"] == ("full" if with_body else "teaser")
    assert (int(crawled.get("description_chars") or 0) > 500) is with_body
    for name in ("job_postings", "posting_sightings", "job_source_state",
                 "job_ingestion_runs", "job_parse_queue"):
        db[name].delete_many({})

    monkeypatch.setattr(w, "registry_members", lambda source: list(MEMBERS)
                        if w.normalize_source(source) == w.SOURCE_WORKDAY else [])
    details = [_detail(posting, body=html, start="2020-01-02")] if with_body else []
    out = radar.ingest_batch(CONTRIB, _batch([posting], details=details), now=NOW)
    assert out["postings"]["created"] == 1
    browser = inv.get_posting("workday", _sjid("JR1001"))

    differing = {k for k in set(crawled) | set(browser) if crawled.get(k) != browser.get(k)}
    if not with_body:
        # The crawl asked for the body and got none, and records that miss so
        # it asks again later. A browser asked nobody, so it records no miss.
        differing -= {"detail_misses", "detail_next_at", "detail_revision"}
    assert differing <= _WRITER_ONLY_FIELDS, {
        k: (crawled.get(k), browser.get(k)) for k in differing - _WRITER_ONLY_FIELDS}
    assert browser["origin"] == "browser" and "origin" not in crawled
    # The fields the notes found different, by name.
    for field in ("detail_ref", "posted_at", "posted_at_confidence", "raw_posted_text",
                  "content_hash", "description_chars", "locations", "canonical_url",
                  "job_id", "dedupe_group_id", "employer_id", "expire_at", "requirements"):
        assert crawled.get(field) == browser.get(field), field
    assert browser["detail_ref"] == "job/US-CA-Santa-Clara/Senior-Backend-Engineer_JR1001"


class TestParityWithTheCrawl:
    """Piece by piece, what the browser path does the crawl's way."""

    def _row(self, posted, **kw):
        return radar.workday_row(dict(MEMBERS[0]), radar._clean_posting(_posting(posted=posted)),
                                 cutoff=NOW - timedelta(days=30),
                                 clock=kw.get("clock", NOW), now=kw.get("now", NOW))

    @pytest.mark.parametrize("posted,days", [
        ("Posted Today", 0), ("Just Posted", 0), ("Posted Yesterday", 1),
        ("Posted 3 Days Ago", 3), ("Posted 7+ Days Ago", 7)])
    def test_relative_dates_are_read_the_crawls_way(self, db, posted, days):
        from services.workday_cxs_fetcher import _approx_iso_from_relative
        row, why = self._row(posted)
        assert why == "" and row["posted_at"] == NOW - timedelta(days=days)
        assert row["posted_at"] == w._parse_iso(_approx_iso_from_relative(posted, now=NOW))
        assert row["posted_at_confidence"] == inv.CONFIDENCE_EXACT

    @pytest.mark.parametrize("posted", ["", "Posted 5 Hours Ago", "Posted 2 Weeks Ago",
                                        "Posted 30 Minutes Ago", "Recently"])
    def test_a_date_the_crawl_cannot_read_is_skipped_not_written(self, db, posted):
        """The crawl skips such a row (ADR-001). The browser path used to
        write it undated; hours and weeks it used to date."""
        from services.workday_cxs_fetcher import _approx_iso_from_relative
        assert _approx_iso_from_relative(posted, now=NOW) == ""
        assert self._row(posted) == (None, "undated")
        out = radar.ingest_batch(CONTRIB, _batch([_posting("JR5", posted=posted)]), now=NOW)
        assert out["postings"]["rejected"] == 1 and out["postings"]["accepted"] == 0
        assert out["would"] == {"created": 0, "updated": 0, "touched": 0}
        assert db.job_postings.count_documents({}) == 0

    def test_relative_dates_count_back_from_when_the_batch_was_observed(self, db):
        """A parked batch arrives a day late. "Posted Today" is the day it
        was read, not the day it was uploaded."""
        seen = NOW - timedelta(hours=26)
        body = _batch([_posting("JR1", posted="Posted Today"),
                       _posting("JR2", title="Data Engineer", posted="Posted 3 Days Ago")])
        body["observed_at"] = seen.isoformat()
        radar.ingest_batch(CONTRIB, body, now=NOW)

        def posted(req):
            value = inv.get_posting("workday", _sjid(req))["posted_at"]
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        assert abs((posted("JR1") - seen).total_seconds()) < 1
        assert abs((posted("JR2") - (seen - timedelta(days=3))).total_seconds()) < 1

    @pytest.mark.parametrize("observed", [
        lambda: (NOW + timedelta(hours=2)).isoformat(),       # the future
        lambda: (NOW - timedelta(days=7, minutes=1)).isoformat(),   # too old
        lambda: "not a date", lambda: ""])
    def test_an_unusable_observed_at_falls_back_to_the_server_clock(self, db, observed):
        assert radar.batch_clock(observed(), NOW) == NOW
        body = _batch([_posting("JR1", posted="Posted Today")])
        body["observed_at"] = observed()
        out = radar.ingest_batch(CONTRIB, body, now=NOW)      # nothing is refused for it
        assert out["postings"]["created"] == 1
        value = inv.get_posting("workday", _sjid("JR1"))["posted_at"]
        value = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        assert abs((value - NOW).total_seconds()) < 1

    def test_observed_at_is_bounded_at_seven_days(self, db):
        edge = NOW - timedelta(days=6, hours=23)
        assert radar.batch_clock(edge.isoformat(), NOW) == edge
        assert radar.batch_clock(NOW.isoformat(), NOW) == NOW

    def test_a_date_in_the_future_is_unknown(self, db):
        """Only a clock ahead of the server can make a relative date land in
        the future. Such a row is undated, so it is skipped."""
        row, why = self._row("Posted Today", clock=NOW + timedelta(hours=3))
        assert (row, why) == (None, "undated")

    def test_start_date_is_not_adopted(self, db):
        """The crawl reads only the body from a Workday detail. A browser's
        `startDate`, past or future, moves nothing."""
        for req, start in (("JR1", "2021-03-04"), ("JR2", "2099-01-01")):
            p = _posting(req, posted="Posted Today")
            radar.ingest_batch(CONTRIB, _batch([p], details=[_detail(p, start=start)]), now=NOW)
            doc = inv.get_posting("workday", _sjid(req))
            value = doc["posted_at"]
            value = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
            assert abs((value - NOW).total_seconds()) < 1
            assert doc["raw_posted_text"] == "Posted Today"

    def test_a_detail_location_is_not_adopted_either(self, db):
        p = _posting("JR1", place="")
        detail = _detail(p)
        detail["jobPostingInfo"]["location"] = "Paris, France"
        radar.ingest_batch(CONTRIB, _batch([p], details=[detail]), now=NOW)
        assert inv.get_posting("workday", _sjid("JR1"))["locations"] == []

    def test_the_body_is_flattened_by_the_crawls_converter(self, db):
        from services.workday_cxs_fetcher import _detail_html_to_text
        html = "<p>Fish &amp; chips &rsquo;quoted&rsquo;</p><ul><li>One</li><li>Two&nbsp;x</li></ul>"
        assert radar.flatten_body(html) == _detail_html_to_text(html)
        assert radar.flatten_body("x" * 20000) == "x" * 15000

    def test_script_text_is_never_stored_as_prose(self, db):
        """The one deliberate difference from the crawl's converter. Workday
        bodies hold no script, so a real body is unchanged."""
        from services.workday_cxs_fetcher import _detail_html_to_text
        hostile = ("<p>Real words.</p><script>steal(document.cookie)</script>"
                   "<style>p{color:red}</style><noscript>tracker</noscript>")
        flat = radar.flatten_body(hostile)
        assert "steal" not in flat and "color" not in flat and "tracker" not in flat
        assert "Real words." in flat
        assert "steal" in _detail_html_to_text(hostile)       # what the crawl's would keep

    def test_a_myworkdaysite_board_keeps_the_providers_link(self, db, monkeypatch):
        """Deliberately not the crawl's: `_list_workday` builds only
        `myworkdayjobs.com` addresses and cannot read such a board at all."""
        snap = {"member_key": "workday-snapchat-snap", "source": w.SOURCE_WORKDAY,
                "vendor": "workday", "tenant": "snapchat", "wd_host": "wd1", "site": "snap",
                "display_name": "Snap Inc", "industry": "tech",
                "extra": {"wd_host": "wd1", "wd_domain": "myworkdaysite.com"}}
        monkeypatch.setattr(w, "registry_members", lambda source: [snap]
                            if w.normalize_source(source) == w.SOURCE_WORKDAY else [])
        out = radar.ingest_batch(CONTRIB, _batch(
            [_posting("JR1")], tenant="snapchat", site="snap", wd_host="wd1",
            domain="myworkdaysite.com"), now=NOW)
        assert out["postings"]["created"] == 1
        doc = inv.get_posting("workday", "workday-snapchat-snap:JR1")
        assert doc["canonical_url"] == ("https://wd1.myworkdaysite.com/recruiting/snapchat/snap/"
                                        "job/US-CA-Santa-Clara/Senior-Backend-Engineer_JR1")
        assert doc["detail_ref"] == "job/US-CA-Santa-Clara/Senior-Backend-Engineer_JR1"


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
        # The crawl's shape: the bare `externalPath`, not a full CXS URL.
        assert full["detail_ref"] == "job/US-CA-Santa-Clara/Senior-Backend-Engineer_JR1"
        # `_listing_row` labels every dated row exact, in the crawl and here.
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


class TestEveryAccountByDefault:
    """Owner's decision 2026-11-26: every signed-in account is a contributor
    unless BROWSER_RADAR_CONTRIBUTORS_SCOPE=listed."""

    def test_default_scope_is_all(self, db, monkeypatch):
        monkeypatch.delenv("BROWSER_RADAR_CONTRIBUTORS_SCOPE", raising=False)
        assert radar.contributor_scope() == "all"
        assert radar.is_contributor(OTHER) is True
        assert radar.is_contributor("someone.new@example.com") is True
        assert radar.is_contributor("") is False
        assert radar.build_plan(OTHER)["contributor"] is True

    def test_default_scope_accepts_any_accounts_batch(self, db, monkeypatch):
        monkeypatch.delenv("BROWSER_RADAR_CONTRIBUTORS_SCOPE", raising=False)
        out = radar.ingest_batch(OTHER, _batch([_posting("JR9")]))
        assert out["mode"] == "on" and out["postings"]["created"] == 1

    def test_listed_scope_restores_the_gate(self, db, monkeypatch):
        monkeypatch.setenv("BROWSER_RADAR_CONTRIBUTORS_SCOPE", "listed")
        assert radar.is_contributor(OTHER) is False
        monkeypatch.setenv("BROWSER_RADAR_CONTRIBUTORS_SCOPE", "typo")
        assert radar.is_contributor(OTHER) is False


# ---------------------------------------------------------------------------
# A batch is untrusted: what a browser sighting may not do
# ---------------------------------------------------------------------------

class TestGuards:
    def _seed_full(self, db):
        p1 = _posting("JR1")
        radar.ingest_batch(CONTRIB, _batch([p1], details=[_detail(p1)]), now=NOW)
        return p1, inv.get_posting("workday", _sjid("JR1"))

    def test_a_fetched_body_is_never_replaced(self, db):
        p1, first = self._seed_full(db)
        forged = dict(p1, title="Senior Backend Engineer (pay a fee to apply)",
                      locationsText="Remote")
        scam = _detail(forged, body="<p>Contact recruiter@evil.example to pay the fee.</p>" * 40)
        out = radar.ingest_batch(SECOND, _batch([forged], details=[scam]),
                                 now=NOW + timedelta(hours=1))
        assert out["postings"]["updated"] == 0 and out["postings"]["touched"] == 1
        after = inv.get_posting("workday", _sjid("JR1"))
        assert after["content_hash"] == first["content_hash"]
        assert after["title_raw"] == first["title_raw"]
        assert after["locations"] == first["locations"]

    def test_a_held_teaser_keeps_its_title_link_and_detail_address(self, db):
        p1 = _posting("JR1")
        radar.ingest_batch(CONTRIB, _batch([p1]), now=NOW)
        first = inv.get_posting("workday", _sjid("JR1"))
        moved = dict(p1, title="Something Else Entirely",
                     externalPath="/job/Elsewhere/Another-Job_JR9999")
        out = radar.ingest_batch(SECOND, _batch([moved], details=[_detail(moved)]),
                                 now=NOW + timedelta(hours=1))
        assert out["postings"]["updated"] == 1
        after = inv.get_posting("workday", _sjid("JR1"))
        assert after["body_state"] == "full"
        assert after["title_raw"] == first["title_raw"]
        assert after["canonical_url"] == first["canonical_url"]
        assert after.get("detail_ref") == first.get("detail_ref")

    @pytest.mark.parametrize("reason", ["detail-gone", "link-dead"])
    def test_a_posting_its_own_page_closed_stays_closed(self, db, reason):
        p1, _ = self._seed_full(db)
        assert inv.close_posting("workday", _sjid("JR1"), reason=reason, now=NOW)
        out = radar.ingest_batch(SECOND, _batch([p1], details=[_detail(p1)]))
        assert out["postings"]["held"] == 1
        assert out["postings"]["reopened"] == out["postings"]["accepted"] == 0
        assert inv.get_posting("workday", _sjid("JR1"))["status"] != "active"

    def test_the_batch_row_lists_what_it_wrote(self, db):
        p1, p2 = _posting("JR1"), _posting("JR2", title="Data Engineer")
        radar.ingest_batch(CONTRIB, _batch([p1]), now=NOW)
        radar.ingest_batch(CONTRIB, _batch([p1, p2], batch_id="ledger-batch-01"),
                           now=NOW + timedelta(hours=1))
        row = db[radar.BATCHES].find_one({"batch_id": "ledger-batch-01"})
        assert sorted((e["id"], e["did"]) for e in row["written"]) == [
            (_sjid("JR1"), "touched"), (_sjid("JR2"), "created")]
        assert row["sent"] == 2 and row["written_truncated"] is False

    def test_a_browser_created_posting_says_so(self, db):
        radar.ingest_batch(CONTRIB, _batch([_posting("JR1")]))
        assert inv.get_posting("workday", _sjid("JR1"))["origin"] == "browser"

    def test_a_refused_day_limit_costs_no_quota(self, db, monkeypatch):
        monkeypatch.setattr(radar, "POSTINGS_PER_DAY", 2)
        radar.ingest_batch(CONTRIB, _batch([_posting("A1"), _posting("A2")]))
        for _ in range(3):
            with pytest.raises(radar.RadarError):
                radar.ingest_batch(CONTRIB, _batch([_posting("A3")]))
        usage = db[radar.USAGE].find_one({"_id": {"$regex": r"\|d\|"}})
        assert usage["postings"] == 2


class TestUploadsRead:
    def test_today_is_this_accounts_own_count(self, db, app):
        p1, p2 = _posting("JR1"), _posting("JR2", title="Data Engineer")
        radar.ingest_batch(CONTRIB, _batch([p1, p2]))
        radar.ingest_batch(CONTRIB, _batch([p1]))
        r = _call(app, CONTRIB, "get", "/api/jobs/browser-radar/uploads/today")
        assert r.status_code == 200
        body = r.get_json()
        assert body["available"] is True
        assert (body["batches"], body["sent"], body["created"], body["touched"]) == (2, 3, 2, 1)
        assert body["recorded"] == 3 and body["last_at"]
        other = _call(app, OTHER, "get", "/api/jobs/browser-radar/uploads/today").get_json()
        assert other["available"] is True and other["batches"] == 0 and other["recorded"] == 0

    def test_dry_runs_are_not_counted_as_sent(self, db, monkeypatch):
        monkeypatch.setenv("BROWSER_RADAR_INGEST", "dry_run")
        radar.ingest_batch(CONTRIB, _batch([_posting("JR1")]))
        assert radar.uploads_today(CONTRIB)["batches"] == 0

    def test_an_unreadable_count_is_unknown_not_zero(self, db, monkeypatch):
        def boom():
            raise RuntimeError("down")
        monkeypatch.setattr(radar, "_db", boom)
        out = radar.uploads_today(CONTRIB)
        assert out["available"] is False and "batches" not in out

    def test_admin_summary_groups_by_account_and_day(self, db):
        radar.ingest_batch(CONTRIB, _batch([_posting("JR1")]))
        radar.ingest_batch(CONTRIB, _batch([_posting("JR2")], tenant="zzz"))
        out = radar.uploads_summary(7)
        assert out["available"] is True and out["accounts"] == 1
        row = out["rows"][0]
        assert row["email"] == CONTRIB and row["batches"] == 2 and row["created"] == 1
        assert row["unknown_boards"] == 1
        assert out["unknown_boards"][0]["tenant"] == "zzz"
        assert out["unknown_boards"][0]["reporters"] == 1
        assert "reported_by" not in out["unknown_boards"][0]
