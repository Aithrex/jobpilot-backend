"""Browser Radar, the fleet half (`radar-2`): leases, the brake and coverage.

`radar-1` hands every contributor the same stale boards. These tests pin the
part that shares the work out (docs/specs/browser-radar.md, section
`radar-2`):

* one ledger row per registry board, never shrunk by a failed registry read;
* a board is leased to one browser at a time, by one guarded write;
* a termless upload is a read of the board only from the account that holds
  its lease, and a title search never is;
* challenges park a board and, across accounts, pause the fleet;
* the coverage number is a stored snapshot served with its age.

Everything runs on mongomock through the shared fixtures.
"""
import json
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from _vg_support import current_db, load_service_app

from services import browser_radar_fleet as fleet
from services import browser_radar_service as radar
from services import job_ingest_worker as w

A = "a@example.com"
B = "b@example.com"
C = "c@example.com"
D = "d@example.com"
NOW = datetime.now(timezone.utc).replace(minute=20, second=0, microsecond=0)

COLLECTIONS = ("job_postings", "job_source_state", "job_ingestion_runs", "job_parse_queue",
               radar.BATCHES, radar.USAGE, radar.UNKNOWN_BOARDS,
               radar.LEASES, radar.COVERAGE, radar.FLEET,
               "board_requests", "board_snapshots", "users")

TENANTS = ("acme", "beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta")


def _member(tenant, site="External"):
    return {"member_key": tenant, "source": w.SOURCE_WORKDAY, "vendor": "workday",
            "tenant": tenant, "wd_host": "wd5", "site": site,
            "display_name": tenant.title() + " Corp", "industry": "tech"}


def _key(tenant, site="external"):
    return f"myworkdayjobs.com|wd5|{tenant}|{site}"


@pytest.fixture()
def registry():
    """The registry the patched `registry_members` answers with. A test
    changes the list to move boards in and out."""
    return {"members": [_member(t) for t in TENANTS], "fail": False}


@pytest.fixture()
def db(monkeypatch, registry):
    d = current_db()
    for c in COLLECTIONS:
        d[c].delete_many({})
    radar.reset_stats_cache()

    def members(source):
        if registry["fail"]:
            raise RuntimeError("registry down")
        return list(registry["members"]) if w.normalize_source(source) == w.SOURCE_WORKDAY else []

    monkeypatch.setattr(w, "registry_members", members)
    for name in ("BROWSER_RADAR_INGEST", "BROWSER_RADAR_ENABLED", "BROWSER_RADAR_CONTRIBUTORS",
                 "BROWSER_RADAR_CONTRIBUTORS_SCOPE", "BROWSER_RADAR_CLAIMS_PER_DAY",
                 "BROWSER_RADAR_TARGET_HOURS", "BROWSER_RADAR_BRAKE_ACCOUNTS"):
        monkeypatch.delenv(name, raising=False)
    yield d
    for c in COLLECTIONS:
        d[c].delete_many({})
    radar.reset_stats_cache()


def _aware(value):
    return value if value is None or value.tzinfo else value.replace(tzinfo=timezone.utc)


def _row(db, tenant, site="external"):
    return db[radar.LEASES].find_one({"_id": _key(tenant, site)})


def _sync(now=NOW):
    out = fleet.sync_boards(now=now, stats=radar._read_tenant_stats())
    assert out["ok"], out
    return out


def _claim(email, limit=None, now=NOW):
    return fleet.claim_boards(email, limit if limit is not None else fleet.CLAIM_DEFAULT, now=now)


def _tenants(answer):
    return [t["tenant"] for t in answer["tasks"]]


def _posting(req="JR1001", title="Senior Backend Engineer"):
    return {"title": title,
            "externalPath": f"/job/US-CA-Santa-Clara/{title.replace(' ', '-')}_{req}",
            "bulletFields": [req], "postedOn": "Posted Today",
            "locationsText": "US, CA, Santa Clara", "timeType": "Full time"}


def _board(tenant, postings=(), search_text="", site="External"):
    return {"tenant": tenant, "wd_host": "wd5", "site": site, "domain": "myworkdayjobs.com",
            "search_text": search_text, "postings": list(postings), "details": []}


def _batch(boards, reads=None):
    body = {"batch_id": f"batch-{time.monotonic_ns()}",
            "client": {"name": "applypilot", "version": "0.5.0"},
            "observed_at": NOW.isoformat(), "boards": list(boards)}
    if reads is not None:
        body["reads"] = reads
    return body


def _lease(db, email, *tenants, at=NOW, minutes=fleet.LEASE_MINUTES, site="external"):
    """Give `email` the lease on these boards as of `at`, as a claim would.
    For tests that need one named board held, whatever the claim order is."""
    for tenant in tenants:
        res = db[radar.LEASES].update_one(
            {"_id": _key(tenant, site)},
            {"$set": {"lease": {"id": uuid.uuid4().hex, "by": fleet.account_pseudonym(email),
                                "until": at + timedelta(minutes=minutes), "at": at}}})
        assert res.matched_count == 1, tenant


def _seed_postings(db, rows):
    """`rows`: (tenant, how many active postings, newest last_seen_at)."""
    docs = []
    for tenant, n, seen in rows:
        docs += [{"source": "workday", "source_job_id": f"{tenant}:seed{i}", "status": "active",
                  "tenant": tenant, "last_seen_at": seen} for i in range(n)]
    db.job_postings.insert_many(docs)


def _first(db, *tenants):
    """Make these tenants the first boards handed out, in this order: the
    crawler holds postings for them and has not seen them for days."""
    old = NOW - timedelta(days=5)
    _seed_postings(db, [(t, 10 * (len(tenants) - i), old) for i, t in enumerate(tenants)])


# ---------------------------------------------------------------------------
# The ledger
# ---------------------------------------------------------------------------

class TestLedger:
    def test_sync_makes_one_row_per_registry_board(self, db):
        out = _sync()
        assert out["added"] == len(TENANTS) and out["retired"] == 0
        assert out["registry"] == {"members": 8, "catalog": 8, "endpoints": 0, "boards": 8}
        assert db[radar.LEASES].count_documents({}) == len(TENANTS)
        row = _row(db, "acme")
        assert row["_id"] == "myworkdayjobs.com|wd5|acme|external"
        assert (row["tenant"], row["site"], row["wd_host"], row["domain"]) == (
            "acme", "External", "wd5", "myworkdayjobs.com")
        assert row["in_registry"] is True and row["lease"] is None
        assert row["last_read_at"] is None and row["last_read_by"] is None
        assert row["last_result"] is None and row["last_postings"] is None
        assert row["challenges"] == 0 and row["parked_until"] is None
        assert _aware(row["due_at"]) == fleet.EPOCH

    def test_a_second_sync_writes_nothing_new(self, db):
        _sync()
        out = _sync(NOW + timedelta(minutes=30))
        assert (out["added"], out["updated"], out["restored"], out["retired"]) == (0, 0, 0, 0)

    def test_the_key_is_the_address_ingest_matches_on(self, db):
        _sync()
        _, by_address = radar._index_members()
        assert {"|".join(a) for a in by_address} == {
            d["_id"] for d in db[radar.LEASES].find({})}

    def test_never_read_boards_go_largest_and_stalest_first(self, db):
        old = NOW - timedelta(days=3)
        _seed_postings(db, [("gamma", 40, old), ("beta", 5, old), ("acme", 90, NOW)])
        _sync()
        assert _row(db, "gamma")["boost"] > _row(db, "beta")["boost"] >= 1.0
        assert _row(db, "acme")["boost"] <= fleet.BOOST_MAX
        # gamma: stale and large. beta: stale and small. The boards with no
        # open posting are stale too. acme is large but the crawler just saw it.
        order = _tenants(_claim(A, 12))
        assert order[:2] == ["gamma", "beta"]
        assert order[-1] == "acme"
        assert sorted(order) == sorted(TENANTS)

    def test_a_failed_registry_read_changes_nothing(self, db, registry):
        _sync()
        before = list(db[radar.LEASES].find({}).sort("_id", 1))
        registry["fail"] = True
        out = fleet.sync_boards(now=NOW + timedelta(hours=1), stats=[])
        assert out["ok"] is False and out["reason"] == "registry_empty"
        registry["fail"] = False
        registry["members"] = []
        out = fleet.sync_boards(now=NOW + timedelta(hours=1), stats=[])
        assert out["ok"] is False and out["reason"] == "registry_empty"
        assert list(db[radar.LEASES].find({}).sort("_id", 1)) == before

    def test_a_registry_that_lost_many_boards_is_not_believed_at_once(self, db, registry):
        """The database half of the registry answers [] when it cannot be
        reached. That must not retire the boards it named."""
        registry["members"] = [_member(t) for t in TENANTS] + [
            _member(f"extra{i}") for i in range(32)]
        _sync()
        before = list(db[radar.LEASES].find({}).sort("_id", 1))
        registry["members"] = [_member(t) for t in TENANTS]
        later = NOW + timedelta(minutes=30)
        out = fleet.sync_boards(now=later, stats=[])
        assert out["ok"] is False and out["reason"] == "registry_shrank"
        assert list(db[radar.LEASES].find({}).sort("_id", 1)) == before
        # Still the same answer a day on: now it is the registry, and it is
        # believed. Rows are marked, never deleted.
        day_on = later + timedelta(hours=fleet.SYNC_SHRINK_ACCEPT_HOURS + 1)
        out = fleet.sync_boards(now=day_on, stats=[])
        assert out["ok"] is True and out["retired"] == 32
        assert db[radar.LEASES].count_documents({}) == 40
        assert db[radar.LEASES].count_documents({"in_registry": True}) == 8

    def test_a_board_that_left_is_marked_not_deleted_and_can_come_back(self, db, registry):
        registry["members"] = [_member(t) for t in TENANTS] + [
            _member(f"extra{i}") for i in range(32)]
        _sync()
        _lease(db, A, "extra0")
        radar.ingest_batch(A, _batch([_board("extra0", [_posting("JR1")])]), now=NOW)
        read_at = _row(db, "extra0")["last_read_at"]
        assert read_at is not None
        registry["members"] = [m for m in registry["members"] if m["tenant"] != "extra0"]
        out = _sync(NOW + timedelta(minutes=30))
        assert out["retired"] == 1
        gone = _row(db, "extra0")
        assert gone["in_registry"] is False and gone["left_registry_at"] is not None
        got = _tenants(_claim(B, 12, NOW + timedelta(days=5)))
        assert "extra0" not in got
        registry["members"].append(_member("extra0"))
        out = _sync(NOW + timedelta(hours=1))
        assert out["restored"] == 1
        back = _row(db, "extra0")
        assert back["in_registry"] is True and back["last_read_at"] == read_at

    def test_accounts_are_pseudonyms_never_emails(self, db):
        who = fleet.account_pseudonym(A)
        assert who and "@" not in who and "example" not in who
        assert who == fleet.account_pseudonym(" A@Example.com ")
        assert who != fleet.account_pseudonym(B)
        assert fleet.account_pseudonym("") == ""


# ---------------------------------------------------------------------------
# Claims
# ---------------------------------------------------------------------------

class TestClaims:
    def test_a_claim_leases_due_boards(self, db):
        _sync()
        out = _claim(A)
        assert out["plan_version"] == "radar-2"
        assert out["lease_minutes"] == 60 and out["paused"] is False and out["reason"] == ""
        assert len(out["tasks"]) == fleet.CLAIM_DEFAULT == 6
        task = out["tasks"][0]
        assert set(task) == {"tenant", "wd_host", "site", "domain", "employer",
                             "search_text", "max_pages", "why", "lease_id"}
        assert (task["search_text"], task["max_pages"], task["why"]) == ("", 1, "shared")
        assert task["site"] == "External" and task["employer"].endswith(" Corp")
        row = _row(db, task["tenant"])
        assert row["lease"]["id"] == task["lease_id"]
        assert row["lease"]["by"] == fleet.account_pseudonym(A)
        assert _aware(row["lease"]["until"]) == NOW + timedelta(minutes=60)

    def test_limit_is_clamped(self, db):
        _sync()
        assert len(_claim(A, 99)["tasks"]) == len(TENANTS)       # max 12, 8 boards
        assert len(_claim(B, 0, NOW)["tasks"]) == 0
        assert fleet._clean_limit(99) == 12 and fleet._clean_limit(-3) == 1
        assert fleet._clean_limit("9") == 6 and fleet._clean_limit(True) == 6

    def test_interleaved_claims_never_share_a_board(self, db, monkeypatch):
        monkeypatch.setattr(fleet, "CLAIMS_PER_DAY", 1000)
        for round_ in range(25):
            db[radar.LEASES].delete_many({})
            _sync()
            held = {A: [], B: [], C: []}
            order = [A, B, C] if round_ % 2 else [C, A, B]
            while True:
                got = 0
                for i, email in enumerate(order):
                    tasks = _tenants(_claim(email, 1 + (round_ + i) % 3))
                    held[email] += tasks
                    got += len(tasks)
                if not got:
                    break
            every = held[A] + held[B] + held[C]
            assert len(every) == len(set(every)) == len(TENANTS)

    def test_concurrent_claims_never_share_a_board(self, db, monkeypatch, registry):
        """Eight threads claim at once. In MongoDB one find_one_and_update is
        atomic on its document. mongomock's is a read then a write with no
        lock between them, so the test serialises THAT ONE CALL to stand in
        for the server. Everything else in the claim runs unlocked."""
        import mongomock

        registry["members"] = [_member(f"t{i:03d}") for i in range(120)]
        _sync()
        monkeypatch.setattr(fleet, "CLAIMS_PER_DAY", 1000)
        real = mongomock.collection.Collection.find_one_and_update
        atomic = threading.Lock()

        def locked(self, *args, **kwargs):
            with atomic:
                return real(self, *args, **kwargs)

        monkeypatch.setattr(mongomock.collection.Collection, "find_one_and_update", locked)
        emails = [f"user{i}@example.com" for i in range(8)]
        held = {e: [] for e in emails}
        errors = []
        start = threading.Barrier(len(emails))

        def run(email):
            try:
                start.wait(timeout=10)
                for _ in range(6):
                    held[email] += [t["tenant"] for t in
                                    fleet.claim_boards(email, 3, now=NOW)["tasks"]]
            except Exception as exc:  # pragma: no cover - reported below
                errors.append(exc)

        threads = [threading.Thread(target=run, args=(e,)) for e in emails]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        assert not errors
        every = [t for e in emails for t in held[e]]
        assert len(every) == len(set(every)) == 120      # 8 x 6 x 3 = 144 asked
        leased = {d["tenant"]: d["lease"]["by"] for d in db[radar.LEASES].find({})}
        for email in emails:
            assert all(leased[t] == fleet.account_pseudonym(email) for t in held[email])

    def test_each_board_is_taken_by_one_guarded_write(self, db, monkeypatch):
        """What makes a double claim impossible on a real server: the whole
        guard is in the filter of the single write that takes the board."""
        import mongomock

        _sync()
        real = mongomock.collection.Collection.find_one_and_update
        seen = []

        def spy(self, flt, update, *args, **kwargs):
            if self.name == radar.LEASES:
                seen.append((flt, update, kwargs.get("sort")))
            return real(self, flt, update, *args, **kwargs)

        monkeypatch.setattr(mongomock.collection.Collection, "find_one_and_update", spy)
        free = fleet._claim_filter(NOW)
        assert db[radar.LEASES].count_documents(free) == len(TENANTS)
        out = _claim(A, 3)
        assert len(out["tasks"]) == 3 and len(seen) == 3
        for flt, update, sort in seen:
            assert flt == free and sort == fleet.CLAIM_SORT
            assert set(update["$set"]) == {"lease"}
        for task in out["tasks"]:
            taken = {"$and": [free, {"_id": _key(task["tenant"])}]}
            assert db[radar.LEASES].count_documents(taken) == 0
        assert db[radar.LEASES].count_documents(free) == len(TENANTS) - 3

    def test_an_expired_lease_is_handed_out_again(self, db):
        _sync()
        first = _claim(A, 12)
        assert len(first["tasks"]) == len(TENANTS)
        assert _claim(B, 12, NOW + timedelta(minutes=59))["reason"] == "none_due"
        again = _claim(B, 12, NOW + timedelta(minutes=61))
        assert sorted(_tenants(again)) == sorted(TENANTS)
        assert {t["lease_id"] for t in again["tasks"]}.isdisjoint(
            {t["lease_id"] for t in first["tasks"]})
        assert _row(db, "acme")["lease"]["by"] == fleet.account_pseudonym(B)

    def test_the_daily_cap(self, db, monkeypatch):
        _sync()
        monkeypatch.setenv("BROWSER_RADAR_CLAIMS_PER_DAY", "5")
        assert len(_claim(A, 3)["tasks"]) == 3
        assert len(_claim(A, 3)["tasks"]) == 2          # only 2 left of 5
        over = _claim(A, 3)
        assert over["tasks"] == [] and over["reason"] == "daily_limit"
        usage = db[radar.USAGE].find_one({"_id": {"$regex": r"\|c\|"}})
        assert usage["claims"] == 5                      # refusals cost nothing
        assert len(_claim(B, 3)["tasks"]) == 3           # per account
        tomorrow = (NOW + timedelta(days=1)).replace(hour=0, minute=5)
        assert len(_claim(A, 3, tomorrow)["tasks"]) == 3  # A's own leases lapsed

    def test_allowance_not_used_is_handed_back(self, db):
        _sync()
        assert len(_claim(A, 12)["tasks"]) == 8
        assert _claim(A, 12)["reason"] == "none_due"
        usage = db[radar.USAGE].find_one({"_id": {"$regex": r"\|c\|"}})
        assert usage["claims"] == 8

    def test_non_contributors_get_no_tasks(self, db, monkeypatch):
        _sync()
        monkeypatch.setenv("BROWSER_RADAR_CONTRIBUTORS_SCOPE", "listed")
        monkeypatch.setenv("BROWSER_RADAR_CONTRIBUTORS", A)
        out = _claim(B)
        assert out["tasks"] == [] and out["reason"] == "not_contributor"
        assert out["plan_version"] == "radar-2" and out["paused"] is False
        assert len(_claim(A)["tasks"]) == 6

    @pytest.mark.parametrize("name,value,reason", [
        ("BROWSER_RADAR_INGEST", "off", "ingest_off"),
        ("BROWSER_RADAR_INGEST", "dry_run", "ingest_dry_run"),
        ("BROWSER_RADAR_ENABLED", "0", "disabled"),
    ])
    def test_switches_stop_claims(self, db, monkeypatch, name, value, reason):
        _sync()
        monkeypatch.setenv(name, value)
        out = _claim(A)
        assert out["tasks"] == [] and out["reason"] == reason
        assert db[radar.LEASES].count_documents({"lease": {"$ne": None}}) == 0
        assert db[radar.USAGE].count_documents({}) == 0

    def test_an_empty_ledger_is_none_due_not_an_error(self, db):
        out = _claim(A)
        assert out["tasks"] == [] and out["reason"] == "none_due"

    def test_an_unreadable_ledger_is_unavailable_not_an_error(self, db, monkeypatch):
        def boom():
            raise RuntimeError("down")
        monkeypatch.setattr(radar, "_db", boom)
        out = _claim(A)
        assert out["tasks"] == [] and out["reason"] == "unavailable"


class TestPlan:
    def test_the_plan_says_claims_are_live(self, db, monkeypatch):
        plan = radar.build_plan(A)
        assert plan["plan_version"] == "radar-1" and plan["claims"] is True
        monkeypatch.setenv("BROWSER_RADAR_INGEST", "dry_run")
        assert radar.build_plan(A)["claims"] is False
        monkeypatch.delenv("BROWSER_RADAR_INGEST")
        monkeypatch.setenv("BROWSER_RADAR_CONTRIBUTORS_SCOPE", "listed")
        assert radar.build_plan(A)["claims"] is False
        assert fleet.claims_live(A) is False

    def test_the_stale_tasks_are_still_there_for_radar_1(self, db, monkeypatch):
        _seed_postings(db, [("gamma", 3, NOW - timedelta(days=2))])
        plan = radar.build_plan(A)
        assert [(t["tenant"], t["why"]) for t in plan["tasks"]] == [("gamma", "stale")]


# ---------------------------------------------------------------------------
# Uploads complete a lease
# ---------------------------------------------------------------------------

class TestUploads:
    def test_a_termless_upload_completes_the_lease_and_moves_due_at(self, db):
        _sync()
        task = _claim(A, 1)["tasks"][0]
        tenant = task["tenant"]
        at = NOW + timedelta(minutes=10)
        out = radar.ingest_batch(A, _batch([_board(tenant, [_posting("JR1")])]), now=at)
        assert out["postings"]["created"] == 1 and "reads" not in out
        row = _row(db, tenant)
        assert row["lease"] is None
        assert _aware(row["last_read_at"]) == at
        assert row["last_read_by"] == fleet.account_pseudonym(A)
        assert (row["last_result"], row["last_postings"]) == ("ok", 1)
        assert _aware(row["due_at"]) == at + timedelta(hours=24)
        # Not due, so not handed out, until the interval has passed.
        assert tenant not in _tenants(_claim(B, 12, at + timedelta(hours=23)))
        assert _tenants(_claim(C, 12, at + timedelta(hours=25)))[-1] == tenant   # the newest read is last

    def test_a_read_from_an_account_that_does_not_hold_the_lease_is_not_a_read(self, db):
        """Any account could once mark every board read without reading one,
        and inflate `by_browser`. Its postings are still ingested."""
        _sync()
        task = _claim(A, 1)["tasks"][0]
        tenant = task["tenant"]
        at = NOW + timedelta(minutes=10)
        out = radar.ingest_batch(B, _batch([_board(tenant, [_posting("JR1")])]), now=at)
        assert out["postings"]["created"] == 1           # ingested as before
        row = _row(db, tenant)
        assert row["lease"]["id"] == task["lease_id"]    # A still holds it
        assert row["last_read_at"] is None and row["last_read_by"] is None
        assert row["last_result"] is None and _aware(row["due_at"]) == fleet.EPOCH
        assert _aware(row["last_seen_unleased_at"]) == at
        assert row["last_unleased_postings"] == 1
        # And the holder's own read still completes it afterwards.
        radar.ingest_batch(A, _batch([_board(tenant, [_posting("JR1")])]),
                           now=at + timedelta(minutes=1))
        row = _row(db, tenant)
        assert row["lease"] is None and row["last_read_by"] == fleet.account_pseudonym(A)

    def test_a_read_of_a_board_nobody_holds_is_not_a_read(self, db):
        """A `radar-1` client's `stale` task carries no lease."""
        _sync()
        radar.ingest_batch(A, _batch([_board("acme", [_posting("JR1")]), _board("beta")]),
                           now=NOW)
        for tenant in ("acme", "beta"):
            row = _row(db, tenant)
            assert row["last_read_at"] is None and _aware(row["due_at"]) == fleet.EPOCH
            assert _aware(row["last_seen_unleased_at"]) == NOW
        assert "acme" in _tenants(_claim(B, 12))         # still due, still handed out

    def test_a_lease_that_lapsed_inside_the_grace_still_completes(self, db):
        _sync()
        tenant = _claim(A, 1)["tasks"][0]["tenant"]
        at = NOW + timedelta(minutes=fleet.LEASE_MINUTES + fleet.LEASE_GRACE_MINUTES - 1)
        radar.ingest_batch(A, _batch([_board(tenant, [_posting("JR1")])]), now=at)
        row = _row(db, tenant)
        assert row["lease"] is None and _aware(row["last_read_at"]) == at
        assert row["last_read_by"] == fleet.account_pseudonym(A)
        assert "last_seen_unleased_at" not in row

    def test_a_lease_that_lapsed_beyond_the_grace_does_not_complete(self, db):
        _sync()
        tenant = _claim(A, 1)["tasks"][0]["tenant"]
        at = NOW + timedelta(minutes=fleet.LEASE_MINUTES + fleet.LEASE_GRACE_MINUTES + 1)
        radar.ingest_batch(A, _batch([_board(tenant, [_posting("JR1")])]), now=at)
        row = _row(db, tenant)
        assert row["last_read_at"] is None and _aware(row["due_at"]) == fleet.EPOCH
        assert _aware(row["last_seen_unleased_at"]) == at

    def test_a_lapsed_lease_another_account_took_is_theirs(self, db):
        """Inside the grace, but the board was claimed again: the first
        holder's late upload no longer counts."""
        _first(db, "acme")
        _sync()
        assert _claim(A, 1)["tasks"][0]["tenant"] == "acme"
        later = NOW + timedelta(minutes=fleet.LEASE_MINUTES + 5)
        task = _claim(B, 1, later)["tasks"][0]
        assert task["tenant"] == "acme"
        radar.ingest_batch(A, _batch([_board("acme", [_posting("JR1")])]),
                           now=later + timedelta(minutes=1))
        row = _row(db, "acme")
        assert row["lease"]["id"] == task["lease_id"] and row["last_read_at"] is None

    def test_a_large_board_is_due_sooner_but_never_under_the_floor(self, db, monkeypatch):
        _seed_postings(db, [("acme", 2000, NOW - timedelta(days=2))])
        _sync()
        assert _row(db, "acme")["boost"] == fleet.BOOST_MAX
        _lease(db, A, "acme", "beta")
        radar.ingest_batch(A, _batch([_board("acme", [_posting("JR1")]),
                                      _board("beta", [_posting("JR2")])]), now=NOW)
        assert _aware(_row(db, "acme")["due_at"]) == NOW + timedelta(hours=6)
        assert _aware(_row(db, "beta")["due_at"]) == NOW + timedelta(hours=24)
        monkeypatch.setenv("BROWSER_RADAR_TARGET_HOURS", "1")       # clamped to 3
        assert fleet.target_hours() == 3.0
        _lease(db, A, "acme")
        radar.ingest_batch(A, _batch([_board("acme", [_posting("JR1")])]), now=NOW)
        assert _aware(_row(db, "acme")["due_at"]) == NOW + timedelta(hours=3)
        monkeypatch.setenv("BROWSER_RADAR_TARGET_HOURS", "500")     # clamped to 72
        assert fleet.target_hours() == 72.0

    def test_a_title_search_is_not_a_read_of_the_board(self, db):
        _sync()
        task = _claim(A, 1)["tasks"][0]
        tenant = task["tenant"]
        out = radar.ingest_batch(
            A, _batch([_board(tenant, [_posting("JR1")], search_text="backend engineer")]),
            now=NOW + timedelta(minutes=5))
        assert out["postings"]["created"] == 1
        row = _row(db, tenant)
        assert row["last_read_at"] is None and row["last_result"] is None
        assert row["lease"]["id"] == task["lease_id"]
        assert _aware(row["due_at"]) == fleet.EPOCH

    def test_an_empty_termless_read_counts(self, db):
        _sync()
        tenant = _claim(A, 1)["tasks"][0]["tenant"]
        out = radar.ingest_batch(A, _batch([_board(tenant)]), now=NOW)
        assert out["boards"] == {"accepted": 1, "unknown": 0}
        assert out["postings"]["accepted"] == 0
        assert db.job_postings.count_documents({}) == 0
        row = _row(db, tenant)
        assert (row["last_result"], row["last_postings"]) == ("empty", 0)
        assert row["lease"] is None and _aware(row["last_read_at"]) == NOW
        assert _aware(row["due_at"]) == NOW + timedelta(hours=24)

    def test_an_upload_before_the_first_sync_makes_the_row_unread(self, db):
        """No row means no lease, so this is not a read. The row is made as
        the sync would make it: never read, due at once."""
        radar.ingest_batch(A, _batch([_board("acme", [_posting("JR1")])]), now=NOW)
        row = _row(db, "acme")
        assert row["in_registry"] is True and row["last_result"] is None
        assert row["last_read_at"] is None and _aware(row["due_at"]) == fleet.EPOCH
        assert _aware(row["last_seen_unleased_at"]) == NOW
        assert (row["tenant"], row["site"], row["employer"]) == ("acme", "External", "Acme Corp")
        assert _sync()["added"] == len(TENANTS) - 1
        assert _row(db, "acme")["last_read_at"] is None
        assert "acme" in _tenants(_claim(B, 12))

    def test_an_unknown_board_is_never_a_ledger_row(self, db):
        _sync()
        radar.ingest_batch(A, _batch([_board("nobody", [_posting("JR1")])]), now=NOW)
        assert db[radar.LEASES].count_documents({}) == len(TENANTS)
        assert db[radar.UNKNOWN_BOARDS].count_documents({}) == 1

    def test_a_dry_run_completes_nothing(self, db, monkeypatch):
        _sync()
        tenant = _claim(A, 1)["tasks"][0]["tenant"]
        monkeypatch.setenv("BROWSER_RADAR_INGEST", "dry_run")
        radar.ingest_batch(A, _batch([_board(tenant, [_posting("JR1")])]), now=NOW)
        row = _row(db, tenant)
        assert row["last_read_at"] is None and row["lease"] is not None

    def test_a_repeated_batch_is_counted_once(self, db):
        _sync()
        _lease(db, A, "acme")
        body = _batch([_board("acme", [_posting("JR1")])])
        radar.ingest_batch(A, body, now=NOW)
        _lease(db, A, "acme", at=NOW + timedelta(hours=2))
        radar.ingest_batch(A, body, now=NOW + timedelta(hours=2))
        assert _aware(_row(db, "acme")["last_read_at"]) == NOW

    def test_reads_gone_parks_the_board(self, db):
        _sync()
        tenant = _claim(A, 1)["tasks"][0]["tenant"]
        gone = {"tenant": tenant, "site": "External", "wd_host": "wd5",
                "domain": "myworkdayjobs.com", "result": "gone"}
        # A batch may carry nothing but `reads`.
        body = _batch([], reads=[gone])
        del body["boards"]
        out = radar.ingest_batch(A, body, now=NOW)
        assert out["reads"] == {"recorded": 1, "ignored": 0}
        row = _row(db, tenant)
        assert row["last_result"] == "gone" and row["lease"] is None
        assert row["last_read_at"] is None           # it was not read
        assert _aware(row["parked_until"]) == NOW + timedelta(days=7)
        assert tenant not in _tenants(_claim(B, 12, NOW + timedelta(days=6)))
        assert tenant in _tenants(_claim(C, 12, NOW + timedelta(days=7, minutes=1)))

    def test_reads_robots_parks_for_a_day(self, db):
        _sync()
        tenant = _claim(A, 1)["tasks"][0]["tenant"]
        radar.ingest_batch(A, _batch([_board("theta", [_posting("JR1")], "sre")], reads=[
            {"tenant": tenant, "site": "External", "wd_host": "wd5", "result": "robots"}]),
            now=NOW)
        row = _row(db, tenant)
        assert row["last_result"] == "robots"
        assert _aware(row["parked_until"]) == NOW + timedelta(hours=24)

    def test_reads_from_an_account_that_does_not_hold_the_lease_park_nothing(self, db):
        """Parking is taken only from the lease holder, or one account could
        take every board out of rotation for a week."""
        _sync()
        tenant = _claim(A, 1)["tasks"][0]["tenant"]
        reads = [{"tenant": t, "site": "External", "wd_host": "wd5", "result": "gone"}
                 for t in (tenant, "theta", "nobody")]
        out = radar.ingest_batch(B, _batch([_board("zeta", [_posting("JR1")], "sre")],
                                           reads=reads), now=NOW)
        assert out["reads"] == {"recorded": 0, "ignored": 3}
        assert db[radar.LEASES].count_documents({"parked_until": {"$ne": None}}) == 0
        assert _row(db, tenant)["lease"] is not None

    @pytest.mark.parametrize("reads", [
        "gone",
        [{"tenant": "acme", "site": "External", "wd_host": "wd5", "result": "ok"}],
        [{"tenant": "bad tenant", "site": "External", "wd_host": "wd5", "result": "gone"}],
        [{"tenant": "acme", "site": "External", "wd_host": "evil.com", "result": "gone"}],
        [{"tenant": "acme", "site": "External", "wd_host": "wd5", "domain": "example.com",
          "result": "gone"}],
        [{"tenant": {"$ne": ""}, "site": "External", "wd_host": "wd5", "result": "gone"}],
        ["nope"],
        [{"tenant": "acme", "site": "External", "wd_host": "wd5", "result": "gone"}] * 41,
    ])
    def test_bad_reads_refuse_the_batch(self, db, reads):
        with pytest.raises(radar.RadarError) as e:
            radar.ingest_batch(A, _batch([_board("acme", [_posting("JR1")])], reads=reads))
        assert (e.value.status, e.value.code) == (400, "invalid_batch")
        assert db.job_postings.count_documents({}) == 0

    def test_a_batch_with_no_boards_and_no_reads_is_still_refused(self, db):
        for body in ({"batch_id": "batch-empty-1", "boards": []},
                     {"batch_id": "batch-empty-2", "boards": [], "reads": []},
                     {"batch_id": "batch-empty-3"}):
            with pytest.raises(radar.RadarError) as e:
                radar.ingest_batch(A, body)
            assert e.value.status == 400


# ---------------------------------------------------------------------------
# Releases, challenges and the brake
# ---------------------------------------------------------------------------

class TestRelease:
    def test_release_frees_only_the_callers_own_leases(self, db):
        _sync()
        mine = _claim(A, 2)["tasks"]
        theirs = _claim(B, 2)["tasks"]
        out = fleet.release_leases(
            A, [mine[0]["lease_id"], theirs[0]["lease_id"]], "budget", now=NOW)
        assert out == {"released": 1, "reason": "budget", "paused": False}
        assert _row(db, mine[0]["tenant"])["lease"] is None
        assert _row(db, mine[1]["tenant"])["lease"] is not None
        assert _row(db, theirs[0]["tenant"])["lease"]["id"] == theirs[0]["lease_id"]
        # Freed and still due: the next caller gets it at once.
        assert mine[0]["tenant"] in _tenants(_claim(C, 12))

    @pytest.mark.parametrize("reason,result,wait", [
        ("gone", "gone", timedelta(days=7)), ("robots", "robots", timedelta(hours=24))])
    def test_release_gone_and_robots_park(self, db, reason, result, wait):
        _sync()
        task = _claim(A, 1)["tasks"][0]
        assert fleet.release_leases(A, [task["lease_id"]], reason, now=NOW)["released"] == 1
        row = _row(db, task["tenant"])
        assert row["lease"] is None and row["last_result"] == result
        assert _aware(row["parked_until"]) == NOW + wait

    @pytest.mark.parametrize("body", [
        None, [], "x", {}, {"lease_ids": [], "reason": "budget"},
        {"lease_ids": ["a" * 32], "reason": "bored"},
        {"lease_ids": ["a" * 32]},
        {"lease_ids": "a" * 32, "reason": "budget"},
        {"lease_ids": ["short"], "reason": "budget"},
        {"lease_ids": [{"$ne": ""}], "reason": "budget"},
        {"lease_ids": ["A" * 32], "reason": "budget"},
        {"lease_ids": [f"{i:032x}" for i in range(13)], "reason": "budget"},
        {"lease_ids": ["a" * 32], "reason": {"$ne": ""}},
    ])
    def test_release_input_is_validated(self, db, body):
        with pytest.raises(radar.RadarError) as e:
            fleet.validate_release(body)
        assert (e.value.status, e.value.code) == (400, "invalid_request")

    def test_one_accounts_challenge_does_not_park_a_board(self, db):
        _first(db, "acme")
        _sync()
        for i in range(4):
            at = NOW + timedelta(minutes=i)
            task = _claim(A, 1, at)["tasks"][0]
            assert task["tenant"] == "acme"       # freed, still the first one due
            out = fleet.release_leases(A, [task["lease_id"]], "challenge", now=at)
            assert out["released"] == 1 and out["paused"] is False
        row = _row(db, "acme")
        assert row["challenges"] == 1 and row["parked_until"] is None

    def test_three_accounts_challenges_park_a_board(self, db, monkeypatch):
        monkeypatch.setenv("BROWSER_RADAR_BRAKE_ACCOUNTS", "50")   # the board rule only
        _first(db, "acme")
        _sync()
        for i, email in enumerate((A, B, C)):
            at = NOW + timedelta(hours=i)
            task = _claim(email, 1, at)["tasks"][0]
            assert task["tenant"] == "acme"
            assert _row(db, "acme")["parked_until"] is None
            fleet.release_leases(email, [task["lease_id"]], "challenge", now=at)
        row = _row(db, "acme")
        assert row["challenges"] == 3 and row["parked_reason"] == "challenge"
        assert _aware(row["parked_until"]) == NOW + timedelta(hours=2 + 24)
        assert "acme" not in _tenants(_claim(D, 12, NOW + timedelta(hours=20)))
        assert "acme" in _tenants(_claim(D, 12, NOW + timedelta(hours=27)))
        stored = json.dumps(db[radar.LEASES].find_one({"_id": _key("acme")}), default=str)
        assert "@" not in stored and "example.com" not in stored

    def test_challenges_a_day_apart_do_not_add_up(self, db, monkeypatch):
        monkeypatch.setenv("BROWSER_RADAR_BRAKE_ACCOUNTS", "50")
        _first(db, "acme")
        _sync()
        for i, email in enumerate((A, B, C)):
            at = NOW + timedelta(hours=13 * i)
            task = _claim(email, 1, at)["tasks"][0]
            fleet.release_leases(email, [task["lease_id"]], "challenge", now=at)
        row = _row(db, "acme")
        assert row["challenges"] == 2 and row["parked_until"] is None


class TestBrake:
    def _challenge(self, email, at):
        task = _claim(email, 1, at)["tasks"][0]
        return fleet.release_leases(email, [task["lease_id"]], "challenge", now=at)

    def test_the_brake_pauses_and_later_resumes(self, db):
        _sync()
        assert self._challenge(A, NOW)["paused"] is False
        assert self._challenge(B, NOW + timedelta(minutes=10))["paused"] is False
        assert self._challenge(A, NOW + timedelta(minutes=15))["paused"] is False  # A again
        tripped = NOW + timedelta(minutes=20)
        assert self._challenge(C, tripped)["paused"] is True

        out = _claim(D, 3, tripped + timedelta(minutes=1))
        assert out == {"plan_version": "radar-2", "tasks": [], "lease_minutes": 60,
                       "paused": True, "reason": "paused"}
        assert _claim(D, 3, tripped + timedelta(hours=23))["paused"] is True
        state = fleet.fleet_state(now=tripped + timedelta(hours=1))
        assert state["paused"] is True and state["paused_until"].endswith("Z")
        # The state is one small document, not process memory.
        brake = db[radar.FLEET].find_one({"_id": "brake"})
        assert _aware(brake["paused_until"]) == tripped + timedelta(hours=24)
        assert brake["challenged_accounts"] == 3

        after = tripped + timedelta(hours=24, minutes=1)
        resumed = _claim(D, 3, after)
        assert resumed["paused"] is False and len(resumed["tasks"]) == 3
        assert fleet.fleet_state(now=after) == {"paused": False, "paused_until": None}

    def test_challenges_spread_over_hours_do_not_trip_it(self, db):
        _sync()
        for i, email in enumerate((A, B, C)):
            assert self._challenge(email, NOW + timedelta(minutes=50 * i))["paused"] is False
        assert _claim(D, 1, NOW + timedelta(hours=2))["paused"] is False

    def test_a_large_fleet_needs_one_percent(self, db, registry):
        """With 500 accounts claiming in the hour, 3 challenges are noise:
        the threshold is 1 percent of them, 5."""
        registry["members"] = [_member(f"t{i:03d}") for i in range(20)]
        _sync()
        # 495 here, and the 5 below claim a board each before their challenge.
        claimers = {f"claimers.{fleet.account_pseudonym(f'u{i}@example.com')}": NOW
                    for i in range(495)}
        db[radar.FLEET].update_one({"_id": fleet._hour_id(NOW)}, {"$set": claimers}, upsert=True)
        accounts = [f"w{i}@example.com" for i in range(5)]
        for email in accounts[:4]:
            assert self._challenge(email, NOW + timedelta(minutes=1))["paused"] is False
        assert self._challenge(accounts[4], NOW + timedelta(minutes=2))["paused"] is True

    def test_an_account_that_never_claimed_cannot_move_the_brake(self, db):
        _sync()
        for email in (A, B, C, D):
            out = fleet.release_leases(email, ["f" * 32], "challenge", now=NOW)
            assert out == {"released": 0, "reason": "challenge", "paused": False}
        assert db[radar.FLEET].count_documents({}) == 0

    def test_the_threshold_is_tunable(self, db, monkeypatch):
        monkeypatch.setenv("BROWSER_RADAR_BRAKE_ACCOUNTS", "1")
        _sync()
        assert self._challenge(A, NOW)["paused"] is True


# ---------------------------------------------------------------------------
# Coverage
# ---------------------------------------------------------------------------

class TestCoverage:
    def test_the_snapshot_counts_both_readers_and_their_union(self, db):
        fresh, old = NOW - timedelta(hours=2), NOW - timedelta(days=3)
        # The crawler saw beta and gamma lately; delta only long ago.
        _seed_postings(db, [("beta", 2, fresh), ("gamma", 4, fresh), ("delta", 3, old),
                            ("stranger", 9, fresh)])
        _sync(NOW - timedelta(hours=3))
        # Browsers read acme and beta inside the window, eta before it.
        _lease(db, A, "eta", at=NOW - timedelta(hours=30))
        radar.ingest_batch(A, _batch([_board("eta")]), now=NOW - timedelta(hours=30))
        _lease(db, A, "acme", "beta", at=NOW - timedelta(hours=1))
        radar.ingest_batch(A, _batch([_board("acme"), _board("beta")]),
                           now=NOW - timedelta(hours=1))
        # An account that holds no lease read gamma and delta: not coverage.
        radar.ingest_batch(C, _batch([_board("gamma"), _board("delta")]),
                           now=NOW - timedelta(hours=1))
        radar.ingest_batch(B, _batch([_board("theta", [_posting("JR5")], "sre")]),
                           now=NOW - timedelta(hours=1))       # a search: not a read
        claimed = _claim(B, 2, NOW - timedelta(minutes=5))["tasks"]
        fleet.release_leases(B, [claimed[0]["lease_id"]], "gone", now=NOW - timedelta(minutes=4))

        out = fleet.run_coverage(now=NOW)
        assert out["stored"] is True and out["sync"]["ok"] is True
        assert db[radar.COVERAGE].count_documents({}) == 1
        snap = db[radar.COVERAGE].find_one({})
        assert _aware(snap["measured_at"]) == NOW and snap["window_hours"] == 24
        assert snap["tenants_total"] == 8 and snap["boards_total"] == 8
        assert snap["by_browser"] == 2            # acme, beta
        # beta, gamma, and theta: its searched posting was written an hour ago.
        # `stranger` is not in the registry and is not counted.
        assert snap["by_crawler"] == 3
        assert snap["tenants_refreshed"] == 4     # acme, beta, gamma, theta
        assert snap["boards_refreshed"] == 2
        assert snap["never_read"] == 5            # all but acme, beta, eta
        assert snap["leased_now"] == 1 and snap["parked"] == 1
        assert snap["claims_24h"] == 2 and snap["accounts_24h"] == 1
        assert snap["registry"] == {"members": 8, "catalog": 8, "endpoints": 0, "boards": 8}
        assert _aware(snap["expire_at"]) == NOW + timedelta(days=14)
        assert "@" not in json.dumps(snap, default=str)

    def test_two_sites_of_one_tenant_are_one_tenant_two_boards(self, db, registry):
        second = dict(_member("acme", "Careers"), member_key="workday-acme-careers")
        registry["members"] = [_member("acme"), second, _member("beta")]
        _sync()
        _lease(db, A, "acme", site="careers")
        radar.ingest_batch(A, _batch([_board("acme", site="Careers")]), now=NOW)
        fleet.run_coverage(now=NOW)
        snap = db[radar.COVERAGE].find_one({})
        assert (snap["tenants_total"], snap["boards_total"]) == (2, 3)
        assert (snap["by_browser"], snap["boards_refreshed"]) == (1, 1)

    def test_a_failed_registry_read_stores_no_snapshot(self, db, registry):
        fleet.run_coverage(now=NOW - timedelta(hours=1))
        before = list(db[radar.LEASES].find({}).sort("_id", 1))
        assert db[radar.COVERAGE].count_documents({}) == 1
        registry["fail"] = True
        out = fleet.run_coverage(now=NOW)
        assert out["stored"] is False and out["reason"] == "registry_empty"
        registry["fail"] = False
        registry["members"] = []
        assert fleet.run_coverage(now=NOW)["stored"] is False
        assert db[radar.COVERAGE].count_documents({}) == 1
        assert list(db[radar.LEASES].find({}).sort("_id", 1)) == before
        # The last good measurement stays, and it ages.
        block = fleet.coverage_block(now=NOW + timedelta(hours=2))
        assert block["available"] is True and block["stale"] is True

    def test_unreadable_crawler_stats_store_no_snapshot(self, db, monkeypatch):
        monkeypatch.setattr(radar, "_read_tenant_stats", lambda: None)
        out = fleet.run_coverage(now=NOW)
        assert out["stored"] is False and out["reason"] == "stats_unavailable"
        assert db[radar.COVERAGE].count_documents({}) == 0
        assert db[radar.LEASES].count_documents({}) == len(TENANTS)   # the sync still ran

    def test_disabled_measures_nothing(self, db, monkeypatch):
        monkeypatch.setenv("BROWSER_RADAR_ENABLED", "0")
        assert fleet.run_coverage(now=NOW) == {"stored": False, "reason": "disabled",
                                               "seconds": 0.0}
        assert db[radar.LEASES].count_documents({}) == 0

    def test_coverage_block_with_no_snapshot_is_unknown(self, db):
        block = fleet.coverage_block(now=NOW)
        assert block["available"] is False and block["detail"]
        assert "tenants_refreshed" not in block and "tenants_total" not in block

    def test_coverage_block_that_cannot_be_read_is_unknown(self, db, monkeypatch):
        fleet.run_coverage(now=NOW)

        def boom():
            raise RuntimeError("down")
        monkeypatch.setattr(radar, "_db", boom)
        block = fleet.coverage_block(now=NOW)
        assert block["available"] is False and "tenants_total" not in block

    def test_coverage_block_serves_the_newest_with_its_age(self, db):
        fleet.run_coverage(now=NOW - timedelta(hours=5))
        _lease(db, A, "acme", at=NOW - timedelta(hours=1))
        radar.ingest_batch(A, _batch([_board("acme")]), now=NOW - timedelta(hours=1))
        fleet.run_coverage(now=NOW - timedelta(minutes=30))
        block = fleet.coverage_block(now=NOW)
        assert block["available"] is True and block["stale"] is False
        assert block["age_hours"] == 0.5 and block["stale_after_hours"] == 2.0
        assert block["measured_at"].endswith("Z")
        assert (block["tenants_total"], block["by_browser"], block["tenants_refreshed"]) == (8, 1, 1)
        for field in ("window_hours", "by_crawler", "boards_total", "boards_refreshed",
                      "never_read", "leased_now", "parked", "claims_24h", "accounts_24h",
                      "registry"):
            assert field in block
        assert "_id" not in block and "expire_at" not in block
        json.dumps(block)                                         # JSON-safe as it is
        assert fleet.coverage_block(now=NOW + timedelta(hours=1, minutes=25))["stale"] is False
        assert fleet.coverage_block(now=NOW + timedelta(hours=1, minutes=45))["stale"] is True


class TestAdmin:
    def test_the_admin_payload_carries_coverage_and_fleet_and_no_emails(self, db):
        _first(db, "acme", "beta", "gamma")
        _sync()
        tasks = _claim(A, 3)["tasks"]
        fleet.release_leases(A, [tasks[0]["lease_id"]], "robots", now=NOW)
        radar.ingest_batch(A, _batch([_board("theta", [_posting("JR1")])]), now=NOW)
        fleet.run_coverage(now=NOW)
        out = radar.uploads_summary(7, now=NOW + timedelta(minutes=6))
        assert out["available"] is True
        assert out["coverage"]["available"] is True and out["coverage"]["age_hours"] == 0.1
        assert out["coverage"]["tenants_total"] == 8
        assert tasks[0]["tenant"] == "acme"
        flt = out["fleet"]
        assert (flt["paused"], flt["paused_until"]) == (False, None)
        assert (flt["leased_now"], flt["parked"]) == (2, 1)
        assert flt["parked_boards"] == [{
            "tenant": tasks[0]["tenant"], "site": "External", "wd_host": "wd5",
            "domain": "myworkdayjobs.com", "reason": "robots", "challenges": 0,
            "parked_until": (NOW + timedelta(hours=24)).isoformat().replace("+00:00", "Z")}]
        boards = json.dumps({"coverage": out["coverage"], "fleet": out["fleet"]})
        assert "@" not in boards and fleet.account_pseudonym(A) not in boards
        # The three fleet collections never hold an email either.
        for name in (radar.LEASES, radar.COVERAGE, radar.FLEET):
            for doc in db[name].find({}):
                assert "@" not in json.dumps(doc, default=str)

    def test_no_measurement_is_unknown_on_the_admin_payload_too(self, db):
        out = radar.uploads_summary(7)
        assert out["coverage"]["available"] is False
        assert out["fleet"] == {"available": True, "paused": False, "paused_until": None,
                                "leased_now": 0, "parked": 0, "parked_boards": []}

    def test_a_paused_fleet_shows_on_the_admin_payload(self, db, monkeypatch):
        monkeypatch.setenv("BROWSER_RADAR_BRAKE_ACCOUNTS", "1")
        _sync()
        task = _claim(A, 1)["tasks"][0]
        fleet.release_leases(A, [task["lease_id"]], "challenge", now=NOW)
        flt = radar.uploads_summary(7, now=NOW + timedelta(hours=1))["fleet"]
        assert flt["paused"] is True
        assert flt["paused_until"] == (NOW + timedelta(hours=24)).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@pytest.fixture()
def app():
    return load_service_app("jobs")


def _call(app, email, path, json=None, data=None):
    from flask_jwt_extended import create_access_token
    with app.app_context():
        tok = create_access_token(identity=email)
    kwargs = {"headers": {"Authorization": f"Bearer {tok}"}}
    if json is not None:
        kwargs["json"] = json
    if data is not None:
        kwargs["data"] = data
        kwargs["content_type"] = "application/json"
    return app.test_client().post(path, **kwargs)


CLAIMS = "/api/jobs/browser-radar/claims"
RELEASE = "/api/jobs/browser-radar/claims/release"


class TestRoutes:
    def test_claims_route_shape(self, db, app):
        _sync(datetime.now(timezone.utc))
        r = _call(app, A, CLAIMS, json={"limit": 2})
        assert r.status_code == 200
        body = r.get_json()
        assert set(body) == {"plan_version", "tasks", "lease_minutes", "paused", "reason"}
        assert body["plan_version"] == "radar-2" and body["lease_minutes"] == 60
        assert body["paused"] is False and body["reason"] == "" and len(body["tasks"]) == 2
        assert set(body["tasks"][0]) == {"tenant", "wd_host", "site", "domain", "employer",
                                         "search_text", "max_pages", "why", "lease_id"}
        # No body at all is the default limit.
        r = _call(app, B, CLAIMS)
        assert r.status_code == 200 and len(r.get_json()["tasks"]) == 6
        # A limit over the maximum is clamped, not refused.
        r = _call(app, C, CLAIMS, json={"limit": 500})
        assert r.status_code == 200 and r.get_json()["reason"] == "none_due"

    def test_release_route_shape(self, db, app):
        _sync(datetime.now(timezone.utc))
        task = _call(app, A, CLAIMS, json={"limit": 1}).get_json()["tasks"][0]
        r = _call(app, B, RELEASE, json={"lease_ids": [task["lease_id"]], "reason": "deferred"})
        assert r.status_code == 200
        assert r.get_json() == {"released": 0, "reason": "deferred", "paused": False}
        r = _call(app, A, RELEASE, json={"lease_ids": [task["lease_id"]], "reason": "deferred"})
        assert r.get_json() == {"released": 1, "reason": "deferred", "paused": False}
        assert _row(db, task["tenant"])["lease"] is None

    def test_both_routes_need_a_token(self, db, app):
        client = app.test_client()
        assert client.post(CLAIMS, json={}).status_code == 401
        assert client.post(RELEASE, json={}).status_code == 401

    def test_refusals_are_never_401(self, db, app, monkeypatch):
        _sync(datetime.now(timezone.utc))
        for body in ({"limit": "six"}, {"limit": True}, {"limit": 2.5},
                     {"limit": {"$gt": 0}}, [1, 2]):
            r = _call(app, A, CLAIMS, json=body)
            assert r.status_code == 400 and r.get_json()["error"] == "invalid_request"
        r = _call(app, A, CLAIMS, data="{not json")
        assert r.status_code == 400
        r = _call(app, A, CLAIMS, data="x" * 5000)
        assert r.status_code == 400
        for body in ({}, {"lease_ids": ["a" * 32], "reason": "nope"},
                     {"lease_ids": ["zz"], "reason": "budget"},
                     {"lease_ids": [f"{i:032x}" for i in range(13)], "reason": "budget"}):
            r = _call(app, A, RELEASE, json=body)
            assert r.status_code == 400 and r.get_json()["error"] == "invalid_request"
        assert _call(app, A, RELEASE).status_code == 400
        assert db[radar.LEASES].count_documents({"lease": {"$ne": None}}) == 0

        # No tasks is an answer, not a refusal: 200 with the reason.
        monkeypatch.setenv("BROWSER_RADAR_CONTRIBUTORS_SCOPE", "listed")
        r = _call(app, A, CLAIMS, json={})
        assert r.status_code == 200 and r.get_json()["reason"] == "not_contributor"
        monkeypatch.delenv("BROWSER_RADAR_CONTRIBUTORS_SCOPE")
        monkeypatch.setenv("BROWSER_RADAR_INGEST", "off")
        r = _call(app, A, CLAIMS, json={})
        assert r.status_code == 200 and r.get_json()["reason"] == "ingest_off"
        assert r.get_json()["tasks"] == []

    def test_claims_are_rate_limited_with_429(self, db, app, monkeypatch):
        from blueprints import jobs as jobs_module
        monkeypatch.setattr(jobs_module, "check_rate_limit", lambda *a, **k: True)
        for path in (CLAIMS, RELEASE):
            r = _call(app, A, path, json={})
            assert r.status_code == 429 and r.get_json()["error"] == "rate_limited"
            assert r.headers["Retry-After"]

    def test_a_paused_fleet_answers_200_paused(self, db, app):
        _sync(datetime.now(timezone.utc))
        db[radar.FLEET].insert_one({"_id": "brake", "paused_until":
                                    datetime.now(timezone.utc) + timedelta(hours=3)})
        r = _call(app, A, CLAIMS, json={"limit": 3})
        assert r.status_code == 200
        assert r.get_json() == {"plan_version": "radar-2", "tasks": [], "lease_minutes": 60,
                                "paused": True, "reason": "paused"}

    def test_the_listings_route_takes_reads(self, db, app):
        _first(db, "acme")
        _sync(datetime.now(timezone.utc))
        task = _call(app, A, CLAIMS, json={"limit": 1}).get_json()["tasks"][0]
        gone = {k: task[k] for k in ("tenant", "site", "wd_host", "domain")}
        _lease(db, A, "theta", at=datetime.now(timezone.utc))
        r = _call(app, A, "/api/jobs/browser-radar/listings",
                  json=_batch([_board("theta")], reads=[dict(gone, result="gone")]))
        assert r.status_code == 200
        assert r.get_json()["reads"] == {"recorded": 1, "ignored": 0}
        assert _row(db, task["tenant"])["last_result"] == "gone"
        assert _row(db, "theta")["last_result"] == "empty"


# ---------------------------------------------------------------------------
# Indexes and the schedule
# ---------------------------------------------------------------------------

class TestWiring:
    def test_the_indexes_exist(self, db, monkeypatch):
        monkeypatch.setattr(radar, "_INDEXED", False)
        radar._ensure_indexes(db)
        due = db[radar.LEASES].index_information()["radar_board_due"]
        assert due["key"] == [("in_registry", 1), ("due_at", 1), ("rank", 1),
                              ("parked_until", 1)]
        assert db[radar.LEASES].index_information()["radar_board_lease_id"]["sparse"] is True
        for name in (radar.COVERAGE, radar.FLEET, radar.BATCHES, radar.USAGE, radar.STAGED):
            ttl = db[name].index_information()["ttl_expire_at"]
            assert ttl["expireAfterSeconds"] == 0 and ttl["key"] == [("expire_at", 1)]
        assert db[radar.COVERAGE].index_information()["radar_coverage_recent"]["key"] == [
            ("measured_at", -1)]

    def test_the_claim_sorts_on_the_index(self):
        keys = next(k for _coll, k, o in radar.INDEX_SPECS if o["name"] == "radar_board_due")
        flt = fleet._claim_filter(NOW)
        assert keys[0][0] == "in_registry" and flt["in_registry"] is True
        assert [k for k, _ in keys[1:3]] == [k for k, _ in fleet.CLAIM_SORT]

    def test_the_index_migration_declares_the_same_indexes(self):
        import sys
        from pathlib import Path

        scripts = Path(__file__).resolve().parents[1] / "scripts"
        if str(scripts) not in sys.path:
            sys.path.insert(0, str(scripts))
        import migrate_indexes as mi

        def shape(specs):
            return sorted((coll, [tuple(k) for k in keys], sorted(opts.items()))
                          for coll, keys, opts in specs)

        declared = [s for s in mi.INDEX_SPECS if s[0].startswith("browser_radar_")]
        assert shape(declared) == shape(radar.INDEX_SPECS)
        assert {s[0] for s in declared} == {radar.BATCHES, radar.USAGE, radar.LEASES,
                                            radar.COVERAGE, radar.FLEET, radar.STAGED}

    def test_the_worker_schedules_radar_coverage(self, monkeypatch):
        import importlib.util
        from pathlib import Path
        from services import scheduled_jobs

        path = Path(__file__).resolve().parents[1] / "services" / "scheduler" / "runner.py"
        spec = importlib.util.spec_from_file_location("sched_runner_radar", path)
        runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner)
        assert runner.EVERY["radar_coverage"] == 30
        at = datetime(2026, 11, 28, 14, 47, tzinfo=timezone.utc)
        assert runner.latest_slot("radar_coverage", at) == at.replace(minute=30)
        seen = {}
        monkeypatch.setattr(fleet, "run_coverage",
                            lambda **kw: seen.update(kw) or {"stored": True})
        out = scheduled_jobs.dispatch_scheduled("radar_coverage")
        assert out == {"job": "radar_coverage", "result": {"stored": True}}
        assert seen["budget_seconds"] < runner.EVERY["radar_coverage"] * 60

    def test_the_job_runs_end_to_end_through_the_dispatcher(self, db):
        from services import scheduled_jobs
        out = scheduled_jobs.dispatch_scheduled("radar_coverage")
        assert out["job"] == "radar_coverage" and out["result"]["stored"] is True
        assert db[radar.LEASES].count_documents({}) == len(TENANTS)
        assert db[radar.COVERAGE].count_documents({}) == 1
