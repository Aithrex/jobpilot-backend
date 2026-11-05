"""No open posting is left without its job description (2026-11-05).

Measured live: 10,794 of 303,224 open postings had no body. Three causes, one
test class each:

* a body was fetched only when the LISTING showed the posting again, and the
  listing reads 7 days - 6,401 Workday postings never had a single attempt;
* the per-run budget went newest first, so older owed rows always lost
  (Workday deferred 90,037 fetches a day);
* two misses meant "never again" - 2,251 postings abandoned, mostly on two
  boards whose route had broken.
"""
from datetime import datetime, timedelta, timezone

import pytest

from services import job_ingest_worker as w
from services import job_inventory_service as inv

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
TEASER = "Short listing text."
BODY = ("Requirements: 3+ years of Python and AWS. Bachelor's degree in Computer "
        "Science. You will build backend services. ") * 20
COLLECTIONS = ("job_postings", "job_source_state", "job_ingestion_runs", "job_parse_queue")


@pytest.fixture()
def db():
    d = inv._db()
    for c in COLLECTIONS:
        d[c].delete_many({})
    yield d
    for c in COLLECTIONS:
        d[c].delete_many({})


@pytest.fixture()
def board(monkeypatch):
    """ONE Workday shard holding two tenants."""
    members = [{"member_key": f"co{i}", "source": w.SOURCE_WORKDAY, "tenant": f"co{i}",
                "wd_host": "wd1", "site": "External", "display_name": f"Company {i}",
                "industry": "tech"} for i in range(2)]
    monkeypatch.setitem(w.SHARD_COUNTS, w.SOURCE_WORKDAY, 1)
    monkeypatch.setattr(w, "registry_members",
                        lambda source: members if w.normalize_source(source) == w.SOURCE_WORKDAY else [])
    return members


def _listing(member, raw_id, *, days_ago=1, description=""):
    return w._listing_row(
        member, raw_id, title="Backend Engineer",
        url=f"https://{member['tenant']}.wd1.myworkdayjobs.com/External/job/Austin/{raw_id}",
        posted_at=NOW - timedelta(days=days_ago), location="Austin, TX, USA",
        description=description, employment_type="Full time",
        detail_ref=f"https://{member['tenant']}.wd1.myworkdayjobs.com/wday/cxs/"
                   f"{member['tenant']}/External/job/Austin/{raw_id}")


def _store_owed(member, raw_id, *, age_days=10, misses=0, next_at=None, **extra):
    """An open posting written from its listing teaser and still owed a body."""
    row = _listing(member, raw_id, days_ago=age_days, description=TEASER)
    payload = dict(row, body_state="teaser", detail_ref=row["_detail_ref"])
    payload.pop("_detail_ref"); payload.pop("_member_key")
    inv.upsert_posting(payload, now=NOW - timedelta(days=age_days))
    sets = {"detail_misses": misses} if misses else {}
    if next_at is not None:
        sets["detail_next_at"] = next_at
    sets.update(extra)
    if sets:
        inv._db().job_postings.update_one({"source_job_id": row["source_job_id"]}, {"$set": sets})
    return row["source_job_id"]


def _patch_io(monkeypatch, listing_by_member, *, fetched=None, body=BODY, misses=()):
    fetched = fetched if fetched is not None else []

    def fake_list(member, **kw):
        member["_listing_truncated"] = True       # never a census: nothing is closed
        return [dict(r) for r in listing_by_member.get(member["member_key"], [])]

    def fake_detail(row, member, **kw):
        fetched.append(row["source_job_id"])
        if row["source_job_id"] in misses:
            return str(row.get("description") or "")   # what a failed fetch hands back
        return body

    monkeypatch.setattr(w, "list_member", fake_list)
    monkeypatch.setattr(w, "fetch_detail", fake_detail)
    return fetched


def _doc(sjid):
    return inv._db().job_postings.find_one({"source_job_id": sjid})


class TestRetrySchedule:
    def test_a_miss_waits_longer_each_time_and_is_never_final(self):
        assert w.detail_retry_at(1, NOW) == NOW + timedelta(hours=1)
        assert w.detail_retry_at(2, NOW) == NOW + timedelta(hours=6)
        assert w.detail_retry_at(9, NOW) == NOW + timedelta(hours=24)

    def test_many_misses_are_still_owed_once_the_window_passes(self, board):
        stored = {"body_state": "teaser", "detail_misses": 7,
                  "detail_next_at": NOW - timedelta(minutes=1)}
        row = _listing(board[0], "R1", description=TEASER)
        assert w._owes_a_detail_fetch(stored, row, board[0], now=NOW)

    def test_inside_the_window_it_waits(self, board):
        stored = {"body_state": "teaser", "detail_misses": 1,
                  "detail_next_at": NOW + timedelta(minutes=30)}
        row = _listing(board[0], "R1", description=TEASER)
        assert not w._owes_a_detail_fetch(stored, row, board[0], now=NOW)

    def test_a_fixed_route_is_asked_at_once(self, board, monkeypatch):
        monkeypatch.setattr(w, "_detail_revision", lambda member: 3)
        stored = {"body_state": "teaser", "detail_misses": 2, "detail_revision": 2,
                  "detail_next_at": NOW + timedelta(hours=20)}
        row = _listing(board[0], "R1", description=TEASER)
        assert w._owes_a_detail_fetch(stored, row, board[0], now=NOW)


class TestTheWritePathKeepsWhatABacklogNeeds:
    def test_the_detail_ref_is_kept_on_the_posting(self, db, board):
        sjid = _store_owed(board[0], "R1")
        assert _doc(sjid)["detail_ref"].endswith("/wday/cxs/co0/External/job/Austin/R1")

    def test_a_miss_schedules_the_next_try_and_a_body_clears_it(self, db, board):
        row = _listing(board[0], "R2", description=TEASER)
        payload = dict(row, body_state="teaser", detail_attempted=True,
                       detail_next_at=NOW + timedelta(hours=1))
        inv.upsert_posting(payload, now=NOW)
        assert inv._safe_dt(_doc(row["source_job_id"])["detail_next_at"]) == NOW + timedelta(hours=1)
        inv.upsert_posting(dict(row, description=BODY, body_state="full"), now=NOW)
        doc = _doc(row["source_job_id"])
        assert doc["body_state"] == "full" and "detail_next_at" not in doc

    def test_a_stored_row_rebuilds_into_the_same_posting(self, db, board):
        """The backlog writes through `upsert_posting`; the rebuilt row must
        normalise to the very posting it came from, or a fetch would fork it."""
        sjid = _store_owed(board[0], "R3", age_days=12)
        doc = _doc(sjid)
        row = w._row_from_stored(doc, board[0])
        again = inv.normalize_posting(dict(row, description=TEASER))
        assert row["source_job_id"] == sjid
        assert again["content_hash"] == doc["content_hash"]
        assert [l["key"] for l in again["locations"]] == [l["key"] for l in doc["locations"]]
        assert row["_detail_ref"] == doc["detail_ref"]


class TestTheBacklogIsReached:
    def test_old_owed_postings_get_a_share_of_the_budget(self, db, board, monkeypatch):
        old = [_store_owed(board[0], f"OLD{i}", age_days=10 + i) for i in range(5)]
        fresh = {board[1]["member_key"]: [_listing(board[1], f"NEW{i}") for i in range(20)]}
        fetched = _patch_io(monkeypatch, fresh)
        out = w.run_shard(w.SOURCE_WORKDAY, w.shard_of(w.SOURCE_WORKDAY, "co0"),
                          now=NOW, max_details=10)
        c = out["counts"]
        assert c["backlog-owed"] == 5 and c["backlog-attempted"] == 2 and c["backlog-filled"] == 2
        # Oldest first: the two oldest owed postings are the ones fetched.
        assert set(old[-2:]) <= set(fetched)
        for sjid in old[-2:]:
            assert _doc(sjid)["body_state"] == "full"
        assert sum(1 for s in fetched if "NEW" in s) == 8

    def test_with_no_backlog_the_listing_keeps_the_whole_budget(self, db, board, monkeypatch):
        fresh = {board[1]["member_key"]: [_listing(board[1], f"NEW{i}") for i in range(20)]}
        fetched = _patch_io(monkeypatch, fresh)
        w.run_shard(w.SOURCE_WORKDAY, w.shard_of(w.SOURCE_WORKDAY, "co0"), now=NOW, max_details=10)
        assert len(fetched) == 10

    def test_a_backlog_miss_is_recorded_and_never_marks_the_posting_live(self, db, board, monkeypatch):
        sjid = _store_owed(board[0], "GONE", age_days=9)
        seen_before = _doc(sjid)["last_seen_at"]
        _patch_io(monkeypatch, {}, misses={sjid})
        out = w.run_shard(w.SOURCE_WORKDAY, w.shard_of(w.SOURCE_WORKDAY, "co0"),
                          now=NOW, max_details=10, backlog_only=True)
        doc = _doc(sjid)
        assert out["counts"]["backlog-missed"] == 1
        assert doc["detail_misses"] == 1 and doc["body_state"] == "teaser"
        assert inv._safe_dt(doc["detail_next_at"]) == NOW + timedelta(hours=1)
        assert doc["last_seen_at"] == seen_before, "a failed fetch is not a sighting"

    def test_a_posting_inside_its_retry_window_waits(self, db, board, monkeypatch):
        waiting = _store_owed(board[0], "WAIT", misses=1, next_at=NOW + timedelta(hours=3))
        due = _store_owed(board[0], "DUE", misses=1, next_at=NOW - timedelta(hours=1))
        fetched = _patch_io(monkeypatch, {})
        w.run_shard(w.SOURCE_WORKDAY, w.shard_of(w.SOURCE_WORKDAY, "co0"),
                    now=NOW, max_details=10, backlog_only=True)
        assert due in fetched and waiting not in fetched

    def test_a_posting_the_listing_shows_is_not_fetched_twice(self, db, board, monkeypatch):
        sjid = _store_owed(board[0], "BOTH", age_days=2)
        fetched = _patch_io(monkeypatch, {board[0]["member_key"]: [
            _listing(board[0], "BOTH", days_ago=2, description=TEASER)]})
        w.run_shard(w.SOURCE_WORKDAY, w.shard_of(w.SOURCE_WORKDAY, "co0"), now=NOW, max_details=10)
        assert fetched.count(sjid) == 1

    def test_backlog_only_never_lists(self, db, board, monkeypatch):
        _store_owed(board[0], "B1")
        _patch_io(monkeypatch, {})
        monkeypatch.setattr(w, "list_member", lambda *a, **k: pytest.fail("listed"))
        out = w.run_shard(w.SOURCE_WORKDAY, w.shard_of(w.SOURCE_WORKDAY, "co0"),
                          now=NOW, max_details=10, backlog_only=True)
        assert "error" not in out and out["counts"]["backlog-filled"] == 1

    def test_closed_postings_are_not_owed(self, db, board):
        sjid = _store_owed(board[0], "SHUT")
        inv._db().job_postings.update_one({"source_job_id": sjid}, {"$set": {"status": "closed"}})
        assert inv.find_owed_details(w.SOURCE_WORKDAY, ["co0"], now=NOW, limit=10) == []


class TestWorkdayRefRebuild:
    """Postings stored before `detail_ref` was kept: Workday mints the apply
    link and the detail URL from one `externalPath`, so one gives the other."""

    def _endpoint(self, member):
        return w._endpoint_from_member(member)

    def _provider(self):
        from services.ats_providers.workday import WorkdayProvider
        return WorkdayProvider()

    def test_the_apply_link_gives_the_detail_url(self, board):
        url = "https://co0.wd1.myworkdayjobs.com/External/job/Austin-TX/Backend-Engineer_R123"
        ref = self._provider().detail_ref_from_apply_url(self._endpoint(board[0]), url)
        assert ref == ("https://co0.wd1.myworkdayjobs.com/wday/cxs/co0/External/"
                       "job/Austin-TX/Backend-Engineer_R123")

    def test_a_locale_segment_is_tolerated(self, board):
        url = "https://co0.wd1.myworkdayjobs.com/en-US/External/job/X/Y_R1?source=li"
        ref = self._provider().detail_ref_from_apply_url(self._endpoint(board[0]), url)
        assert ref.endswith("/wday/cxs/co0/External/job/X/Y_R1")

    def test_another_board_is_refused(self, board):
        p, e = self._provider(), self._endpoint(board[0])
        assert p.detail_ref_from_apply_url(e, "https://other.wd1.myworkdayjobs.com/External/job/X") == ""
        assert p.detail_ref_from_apply_url(e, "https://co0.wd1.myworkdayjobs.com/Internal/job/X") == ""
        assert p.detail_ref_from_apply_url(e, "") == ""

    def test_a_stored_posting_without_a_ref_gets_one(self, db, board):
        sjid = _store_owed(board[0], "R9")
        inv._db().job_postings.update_one({"source_job_id": sjid}, {"$unset": {"detail_ref": ""}})
        doc = _doc(sjid)
        assert w._stored_detail_ref(doc, board[0]).endswith("/wday/cxs/co0/External/job/Austin/R9")


class TestDescriptionHealth:
    def test_owed_share_new_share_and_broken_boards(self, db, board):
        for i in range(25):
            _store_owed(board[0], f"X{i}", age_days=3)
        row = _listing(board[1], "OK1")
        inv.upsert_posting(dict(row, description=BODY, body_state="full"), now=NOW - timedelta(hours=5))
        h = inv.description_health(NOW)
        assert h["active"] == 26 and h["owed"] == 25
        assert h["with_body"] == round(1 / 26, 4) and not h["meets_slo"]
        assert h["new_total"] == 1 and h["new_owed"] == 0
        assert h["broken_boards"][0]["tenant"] == "co0" and h["broken_boards"][0]["owed"] == 25


class TestWorkableShipsItsBodiesInTheListing:
    """Workable meets a burst of detail requests with 429 and a vendor-wide
    cooldown; Toloka's 2,156-posting board had 346 open postings abandoned
    without a body. `?details=true` puts every body in the one listing call."""

    def test_the_listing_asks_for_and_keeps_the_bodies(self):
        from services.ats_providers.base import Endpoint, FetchContext
        from services.ats_providers.boards import WorkableProvider
        asked = []

        def get_json(url, params=None, **kw):
            asked.append(url)
            return 200, {"jobs": [{"title": "Backend Engineer", "shortcode": "ABC123",
                                   "url": "https://apply.workable.com/j/ABC123",
                                   "published_on": "2026-11-04", "city": "Austin",
                                   "description": "<p>" + "Build APIs in Python. " * 80 + "</p>"}]}

        ctx = FetchContext(get_json=get_json, get_text=lambda *a, **k: (404, ""))
        jobs = list(WorkableProvider().list_jobs(Endpoint(vendor="workable", tenant="acme"), ctx))
        assert asked and asked[0].endswith("/widget/accounts/acme?details=true")
        assert len(jobs) == 1 and "Build APIs in Python." in jobs[0].description
        assert "<p>" not in jobs[0].description
