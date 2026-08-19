"""Sharded ingestion worker (ADR-003) - the invariants that replace a queue.

ADR-003 trades SQS away for cost, which removes the visibility timeout, the
DLQ, FIFO ordering and per-message retry. Everything those would have given us
has to hold here instead, so each test below pins one of them:

  partitioning   - stable, and covers the registry EXACTLY once
  re-run         - a repeated shard run does not duplicate or move first_seen_at
  duplicate      - a duplicate Lambda delivery is a no-op (lease)
  early stop     - newest-first paging stops at the 7-day boundary
  lease          - two concurrent runners cannot both own a shard
  watermark      - advances monotonically, never rewinds
  failure        - a failing shard backs off and leaves its neighbours alone
  concurrency    - the worker pool is genuinely bounded
  content_hash   - an unchanged JD is not re-fetched or re-parsed

Runs fully offline against the suite's mongomock DB. No outbound HTTP: every
listing/detail call is monkeypatched at the worker's fetch seam.
"""
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from services import job_ingest_worker as w
from services import job_inventory_service as inv

NOW = datetime(2026, 7, 21, 12, 0, 0, tzinfo=timezone.utc)

INVENTORY_COLLECTIONS = ("job_postings", "job_source_state",
                         "job_ingestion_runs", "job_match_snapshots")


@pytest.fixture()
def db():
    """Clean the inventory collections BEFORE and AFTER.

    The suite shares one process-wide mongomock database, so leaving ingested
    postings behind leaks into every later test that reads `job_postings`.
    """
    d = inv._db()
    for c in INVENTORY_COLLECTIONS:
        d[c].delete_many({})
    yield d
    for c in INVENTORY_COLLECTIONS:
        d[c].delete_many({})


# ---------------------------------------------------------------------------
# A tiny fake registry so tests do not depend on the 1013/1309-row catalogs
# ---------------------------------------------------------------------------

def _fake_members(source, n, prefix="co"):
    if source == w.SOURCE_WORKDAY:
        return [{"member_key": f"{prefix}{i}", "source": source, "tenant": f"{prefix}{i}",
                 "wd_host": "wd1", "site": "External",
                 "display_name": f"Company {i}", "industry": "tech"} for i in range(n)]
    return [{"member_key": f"greenhouse-{prefix}{i}", "source": source,
             "ats": "greenhouse", "slug": f"{prefix}{i}",
             "display_name": f"Company {i}", "industry": "tech"} for i in range(n)]


@pytest.fixture()
def small_registry(monkeypatch):
    """4 Workday shards over 12 tenants - small enough to reason about."""
    members = _fake_members(w.SOURCE_WORKDAY, 12)
    monkeypatch.setitem(w.SHARD_COUNTS, w.SOURCE_WORKDAY, 4)
    monkeypatch.setattr(w, "registry_members",
                        lambda source: members if w.normalize_source(source) == w.SOURCE_WORKDAY else [])
    return members


def _row(member, raw_id, *, days_ago=1, title="Backend Engineer",
         description="", now=NOW):
    """A listing row with NO body by default, so it exercises the detail path.

    Workday listings genuinely ship no description, and a row that already
    carries one now skips `fetch_detail` entirely (it would be a wasted
    request), so a default body here would silently stop these tests from
    covering the detail path at all. `test_listing_body_skips_the_detail_fetch`
    covers the opposite case explicitly.
    """
    return w._listing_row(
        member, raw_id, title=title,
        url=f"https://{member['member_key']}.wd1.myworkdayjobs.com/External/{raw_id}",
        posted_at=now - timedelta(days=days_ago), location="Austin, TX, USA",
        description=description, employment_type="Full time",
        detail_ref=str(raw_id))


# ---------------------------------------------------------------------------
# 1. Partitioning: stable + covers the registry exactly once
# ---------------------------------------------------------------------------

def test_partition_covers_registry_exactly_once():
    for source in w.SOURCES:
        members = w.registry_members(source)
        assert members, f"{source} registry must not be empty"
        keys = [m["member_key"] for m in members]
        assert len(keys) == len(set(keys)), "member_key must be unique"

        seen = []
        for shard in w.all_shards(source):
            seen.extend(m["member_key"] for m in w.shard_members(source, shard))

        assert sorted(seen) == sorted(keys), "every member appears in exactly one shard"
        assert len(seen) == len(set(seen)), "no member is in two shards"


def test_partition_is_stable_and_insertion_tolerant(monkeypatch):
    """Stability is the reason we hash instead of slicing by list index: the
    catalogs are regenerated, and an index partition would reshuffle nearly
    every member (invalidating every watermark) when one row is inserted."""
    base = _fake_members(w.SOURCE_WORKDAY, 50)
    monkeypatch.setitem(w.SHARD_COUNTS, w.SOURCE_WORKDAY, 8)

    monkeypatch.setattr(w, "registry_members", lambda s: base)
    before = {m["member_key"]: w.shard_of(w.SOURCE_WORKDAY, m["member_key"]) for m in base}

    # Same input, second call -> identical assignment (not process-salted).
    again = {m["member_key"]: w.shard_of(w.SOURCE_WORKDAY, m["member_key"]) for m in base}
    assert again == before

    # Insert a member at the FRONT and reverse the order: assignment of every
    # pre-existing member is untouched.
    grown = [{"member_key": "brand-new", "source": w.SOURCE_WORKDAY, "tenant": "brand-new",
              "wd_host": "wd1", "site": "External", "display_name": "New", "industry": "tech"}]
    grown += list(reversed(base))
    monkeypatch.setattr(w, "registry_members", lambda s: grown)
    after = {m["member_key"]: w.shard_of(w.SOURCE_WORKDAY, m["member_key"])
             for m in grown if m["member_key"] != "brand-new"}
    assert after == before


def test_shard_sizes_stay_bounded():
    """ADR-003: a shard is the retry unit, so re-running one must be cheap."""
    stats = w.registry_stats()
    for source, s in stats.items():
        assert s["members"] > 0
        # ~20 sources per shard on average; allow generous hash skew but keep a
        # hard ceiling so no single retry unit is disproportionately expensive.
        assert s["max_shard_size"] <= 60, f"{source} shard too large: {s}"


def test_unknown_shard_and_source_are_rejected():
    """The shard arrives in a job payload; payloads are not a trusted channel."""
    with pytest.raises(ValueError):
        w.normalize_source("evil-source")
    with pytest.raises(ValueError):
        w.normalize_shard(w.SOURCE_WORKDAY, "s999")
    with pytest.raises(ValueError):
        w.normalize_shard(w.SOURCE_WORKDAY, {"$ne": None})
    # run_shard swallows it into a payload error, never a shard backoff.
    out = w.run_shard(w.SOURCE_WORKDAY, "s999")
    assert out["error"] == "invalid_shard"


# ---------------------------------------------------------------------------
# 2. SSRF: server-controlled registries only
# ---------------------------------------------------------------------------

def test_only_registry_hosts_are_reachable():
    assert w.host_allowed(w.SOURCE_WORKDAY,
                          "https://acme.wd1.myworkdayjobs.com/wday/cxs/acme/External/jobs")
    assert w.host_allowed(w.SOURCE_CAREER,
                          "https://boards-api.greenhouse.io/v1/boards/x/jobs")
    for bad in (
        "http://acme.wd1.myworkdayjobs.com/x",          # not https
        "https://169.254.169.254/latest/meta-data/",    # metadata endpoint
        "https://localhost/x",
        "https://evil.com/wday/cxs/acme/External/jobs",
        "https://boards-api.greenhouse.io.evil.com/x",
        "file:///etc/passwd",
        "",
    ):
        assert not w.host_allowed(w.SOURCE_WORKDAY, bad), bad
        assert not w.host_allowed(w.SOURCE_CAREER, bad), bad
    with pytest.raises(ValueError):
        w.assert_outbound_url(w.SOURCE_WORKDAY, "https://169.254.169.254/")


# ---------------------------------------------------------------------------
# 3. Bounded concurrency + the deadline shape
# ---------------------------------------------------------------------------

def test_run_bounded_respects_the_worker_ceiling():
    live = 0
    peak = 0
    lock = threading.Lock()

    def task(_item):
        nonlocal live, peak
        with lock:
            live += 1
            peak = max(peak, live)
        time.sleep(0.02)
        with lock:
            live -= 1
        return "ok"

    out = w.run_bounded(task, range(60), max_workers=4,
                        deadline=time.monotonic() + 30)
    assert len(out) == 60
    assert all(err is None for _i, _r, err in out)
    assert peak <= 4, f"pool exceeded its ceiling: {peak}"


def test_run_bounded_stops_at_the_deadline_and_does_not_leak_threads():
    """The bug this shape exists to avoid: guest_service.py:1159-1194 calls
    shutdown(wait=False, cancel_futures=True) on callables that sit inside
    `with ThreadPoolExecutor(...)` blocks, whose __exit__ is shutdown(wait=True),
    so ~80 threads outlive the deadline and thaw into the next invocation."""
    started = []

    def task(item):
        started.append(item)
        time.sleep(0.05)
        return item

    before = threading.active_count()
    t0 = time.monotonic()
    out = w.run_bounded(task, range(500), max_workers=4,
                        deadline=time.monotonic() + 0.3)
    elapsed = time.monotonic() - t0

    assert elapsed < 3.0, "run_bounded must return near its deadline"
    assert len(started) < 500, "must stop submitting once the budget is gone"
    # Tasks submitted just before the budget lapsed report "deadline" rather
    # than running, so results can slightly exceed the number that ran.
    assert all(err in (None, "deadline") for _i, _r, err in out)

    # Cooperative stop: the few in-flight tasks drain quickly and thread count
    # returns to roughly the baseline rather than staying pinned at the ceiling.
    deadline = time.monotonic() + 5
    while threading.active_count() > before + 1 and time.monotonic() < deadline:
        time.sleep(0.05)
    assert threading.active_count() <= before + 1, "worker threads outlived the run"


def test_host_policy_circuit_breaker_opens_and_stays_open():
    policy = w.HostPolicy(min_interval=0.0, jitter=0.0, failure_threshold=3)
    host = "boards-api.greenhouse.io"
    assert not policy.is_open(host)
    policy.note_failure(host)
    policy.note_failure(host)
    assert not policy.is_open(host)
    assert policy.note_failure(host) is True
    assert policy.is_open(host)
    assert policy.acquire(host, deadline=time.monotonic() + 5) is False
    # A different host is untouched: one dead board must not stall the shard.
    assert policy.acquire("api.lever.co", deadline=time.monotonic() + 5) is True


def test_host_policy_spaces_repeat_calls_to_one_host():
    """686 of 1309 career-page companies are Greenhouse, so a single shard can
    hit one host ~20 times; the limiter must actually space those."""
    policy = w.HostPolicy(min_interval=0.05, jitter=0.0, failure_threshold=9)
    deadline = time.monotonic() + 10
    t0 = time.monotonic()
    for _ in range(4):
        assert policy.acquire("boards-api.greenhouse.io", deadline=deadline)
    assert time.monotonic() - t0 >= 0.10


# ---------------------------------------------------------------------------
# 4. Newest-first early stop at the 7-day boundary
# ---------------------------------------------------------------------------

def test_workday_listing_stops_at_the_freshness_boundary(monkeypatch):
    """The board is newest-first, so the first row past the boundary ends BOTH
    the page and the paging. Anything else re-reads the whole board every tick,
    which is what made a guest request cost ~7,400 outbound calls."""
    pages_requested = []

    def fake_http(source, url, policy, *, deadline, stop=None, timeout=None,
                  method="GET", json_body=None):
        offset = (json_body or {}).get("offset", 0)
        pages_requested.append(offset)
        page = offset // 20
        postings = []
        for i in range(20):
            # Page 0: all 1 day old. Page 1: first 5 fresh, then 30 days old.
            days = 1 if page == 0 else (2 if i < 5 else 30)
            postings.append({
                "title": f"Engineer {page}-{i}",
                "postedOn": "Posted Today" if days <= 1 else f"Posted {days} Days Ago",
                "externalPath": f"/job/req-{page}-{i}",
                "bulletFields": [f"REQ-{page}-{i}"],
                "locationsText": "Austin, TX",
                "timeType": "Full time",
            })
        return {"jobPostings": postings}

    monkeypatch.setattr(w, "http_get_json", fake_http)
    member = _fake_members(w.SOURCE_WORKDAY, 1)[0]
    rows = w._list_workday(member, cutoff=NOW - timedelta(days=7),
                           policy=w.HostPolicy(min_interval=0, jitter=0),
                           deadline=time.monotonic() + 30, now=NOW)

    assert pages_requested == [0, 20], "must stop paging at the boundary, not exhaust the board"
    assert len(rows) == 25, "20 fresh on page 0 + 5 fresh on page 1"
    assert all(r["posted_at"] >= NOW - timedelta(days=7) for r in rows)


def test_career_listing_drops_rows_past_the_boundary(monkeypatch):
    def fake_http(source, url, policy, **kw):
        return {"jobs": [
            {"id": 1, "title": "Fresh", "absolute_url": "https://x/1",
             "first_published": (NOW - timedelta(days=2)).isoformat(),
             "location": {"name": "Remote"}},
            {"id": 2, "title": "Stale", "absolute_url": "https://x/2",
             "first_published": (NOW - timedelta(days=40)).isoformat(),
             "location": {"name": "Remote"}},
            # ADR-001: no first_published means the date is UNKNOWN, and an
            # unknown date is never promoted to "posted today".
            {"id": 3, "title": "Undated", "absolute_url": "https://x/3",
             "location": {"name": "Remote"}},
        ]}

    monkeypatch.setattr(w, "http_get_json", fake_http)
    member = _fake_members(w.SOURCE_CAREER, 1)[0]
    rows = w._list_career(member, cutoff=NOW - timedelta(days=7),
                          policy=w.HostPolicy(min_interval=0, jitter=0),
                          deadline=time.monotonic() + 30, now=NOW)
    assert [r["title"] for r in rows] == ["Fresh"]


# ---------------------------------------------------------------------------
# 5. Shard runs: idempotence, duplicate delivery, watermark, closure
# ---------------------------------------------------------------------------

def _patch_shard_io(monkeypatch, rows_by_member, detail_calls=None):
    """Stub the two IO seams (listing + detail); everything below them is real."""
    def fake_list(member, **kw):
        return [dict(r) for r in rows_by_member.get(member["member_key"], [])]

    def fake_detail(row, member, **kw):
        if detail_calls is not None:
            detail_calls.append(row["source_job_id"])
        return row.get("description") or "Fetched body: Python, MongoDB, AWS."

    monkeypatch.setattr(w, "list_member", fake_list)
    monkeypatch.setattr(w, "fetch_detail", fake_detail)


def test_shard_rerun_is_idempotent(db, small_registry, monkeypatch):
    shard = w.shard_of(w.SOURCE_WORKDAY, small_registry[0]["member_key"])
    members = w.shard_members(w.SOURCE_WORKDAY, shard)
    rows = {m["member_key"]: [_row(m, f"REQ-{i}") for i in range(3)] for m in members}
    detail_calls = []
    _patch_shard_io(monkeypatch, rows, detail_calls)

    first = w.run_shard(w.SOURCE_WORKDAY, shard, now=NOW)
    assert "error" not in first
    count_after_first = db.job_postings.count_documents({})
    assert count_after_first == 3 * len(members)
    first_seen = {d["source_job_id"]: d["first_seen_at"]
                  for d in db.job_postings.find({}, {"source_job_id": 1, "first_seen_at": 1})}
    assert len(detail_calls) == count_after_first

    # Re-run 10 minutes later, same board.
    detail_calls.clear()
    second = w.run_shard(w.SOURCE_WORKDAY, shard, now=NOW + timedelta(minutes=10))
    assert "error" not in second

    assert db.job_postings.count_documents({}) == count_after_first, "re-run must not duplicate"
    after = {d["source_job_id"]: d["first_seen_at"]
             for d in db.job_postings.find({}, {"source_job_id": 1, "first_seen_at": 1})}
    assert after == first_seen, "first_seen_at must never move on a re-run"
    # Nothing changed, so nothing was re-fetched: the content_hash / listing
    # equivalence check spent zero detail requests.
    assert detail_calls == [], "unchanged postings must not be re-fetched"
    assert second["counts"].get("unchanged", 0) == count_after_first
    assert second["counts"].get("closed", 0) == 0, "a clean re-run must close nothing"


def test_duplicate_lambda_delivery_is_a_noop(db, small_registry, monkeypatch):
    """Lambda self-invocation is at-least-once. The SECOND delivery must not
    double-write, double-count or re-dispatch external work."""
    shard = w.shard_of(w.SOURCE_WORKDAY, small_registry[0]["member_key"])
    members = w.shard_members(w.SOURCE_WORKDAY, shard)
    rows = {m["member_key"]: [_row(m, "REQ-1")] for m in members}
    detail_calls = []
    _patch_shard_io(monkeypatch, rows, detail_calls)

    w.run_shard(w.SOURCE_WORKDAY, shard, now=NOW)
    baseline = db.job_postings.count_documents({})
    runs_before = db.job_ingestion_runs.count_documents({})

    # Simulate the duplicate arriving while a worker still holds the lease.
    holder = "other-container:deadbeef"
    assert inv.acquire_lease(w.SOURCE_WORKDAY, shard, holder, now=NOW) is not None
    detail_calls.clear()
    dup = w.run_shard(w.SOURCE_WORKDAY, shard, now=NOW + timedelta(seconds=5))

    assert dup["skipped"] == "lease_unavailable"
    assert detail_calls == [], "a duplicate delivery must dispatch no external work"
    assert db.job_postings.count_documents({}) == baseline
    assert db.job_ingestion_runs.count_documents({}) == runs_before, "no run row for a no-op"


def test_lease_prevents_two_concurrent_runners(db, small_registry, monkeypatch):
    shard = w.shard_of(w.SOURCE_WORKDAY, small_registry[0]["member_key"])
    members = w.shard_members(w.SOURCE_WORKDAY, shard)
    _patch_shard_io(monkeypatch, {m["member_key"]: [_row(m, "REQ-1")] for m in members})

    gate = threading.Event()
    outcomes = []
    original = w.list_member

    def slow_list(member, **kw):
        gate.wait(timeout=5)
        return original(member, **kw)

    monkeypatch.setattr(w, "list_member", slow_list)

    def runner():
        outcomes.append(w.run_shard(w.SOURCE_WORKDAY, shard, now=NOW))

    t1 = threading.Thread(target=runner)
    t1.start()
    time.sleep(0.2)          # let t1 take the lease and block in listing
    second = w.run_shard(w.SOURCE_WORKDAY, shard, now=NOW)
    gate.set()
    t1.join(timeout=15)

    assert second["skipped"] == "lease_unavailable"
    assert len(outcomes) == 1 and "error" not in outcomes[0]


def test_watermark_advances_monotonically(db, small_registry, monkeypatch):
    shard = w.shard_of(w.SOURCE_WORKDAY, small_registry[0]["member_key"])
    members = w.shard_members(w.SOURCE_WORKDAY, shard)

    _patch_shard_io(monkeypatch, {m["member_key"]: [_row(m, "REQ-1", days_ago=3)]
                                  for m in members})
    w.run_shard(w.SOURCE_WORKDAY, shard, now=NOW)
    wm1 = inv.get_source_state(w.SOURCE_WORKDAY, shard)["watermark"]
    assert wm1 is not None

    # A newer posting moves it forward.
    _patch_shard_io(monkeypatch, {m["member_key"]: [_row(m, "REQ-2", days_ago=1)]
                                  for m in members})
    w.run_shard(w.SOURCE_WORKDAY, shard, now=NOW + timedelta(hours=1))
    wm2 = inv.get_source_state(w.SOURCE_WORKDAY, shard)["watermark"]
    assert wm2 > wm1

    # A replayed / out-of-order run carrying only OLDER rows must not rewind.
    _patch_shard_io(monkeypatch, {m["member_key"]: [_row(m, "REQ-3", days_ago=6)]
                                  for m in members})
    w.run_shard(w.SOURCE_WORKDAY, shard, now=NOW + timedelta(hours=2))
    wm3 = inv.get_source_state(w.SOURCE_WORKDAY, shard)["watermark"]
    assert wm3 == wm2, "watermark must never move backwards"


def test_failing_shard_backs_off_without_poisoning_others(db, small_registry, monkeypatch):
    """No DLQ (ADR-003): backoff + the job_ingestion_runs row IS the failure
    isolation mechanism, and run_shard must not raise."""
    bad = w.shard_of(w.SOURCE_WORKDAY, small_registry[0]["member_key"])
    good = next(s for s in w.all_shards(w.SOURCE_WORKDAY)
                if s != bad and w.shard_members(w.SOURCE_WORKDAY, s))

    def fake_list(member, **kw):
        if member["member_key"] in {m["member_key"]
                                    for m in w.shard_members(w.SOURCE_WORKDAY, bad)}:
            raise RuntimeError("board unreachable")
        return [_row(member, "REQ-1")]

    monkeypatch.setattr(w, "list_member", fake_list)
    monkeypatch.setattr(w, "fetch_detail", lambda row, member, **kw: "Body.")

    out = w.run_shard(w.SOURCE_WORKDAY, bad, now=NOW)
    assert "error" in out, "an all-members-failed shard is a shard failure"

    state = inv.get_source_state(w.SOURCE_WORKDAY, bad)
    assert state["retry_count"] == 1
    assert state["next_attempt_at"] is not None, "must back off"
    assert state["lease_owner"] is None, "the lease is released even on failure"
    run = db.job_ingestion_runs.find_one({"source": w.SOURCE_WORKDAY, "shard": bad})
    assert run["status"] == "failed" and run["failures"]

    # A failing shard is in backoff, so it is no longer due...
    due = {(s["source"], s["shard"]) for s in inv.due_shards(w.SOURCE_WORKDAY, now=NOW)}
    assert (w.SOURCE_WORKDAY, bad) not in due

    # ...and its neighbour is completely unaffected.
    ok = w.run_shard(w.SOURCE_WORKDAY, good, now=NOW)
    assert "error" not in ok
    assert inv.get_source_state(w.SOURCE_WORKDAY, good)["retry_count"] == 0
    assert db.job_postings.count_documents({"source": w.SOURCE_WORKDAY}) > 0

    # And a second failure backs off strictly further than the first.
    w.run_shard(w.SOURCE_WORKDAY, bad, now=NOW + timedelta(hours=8))
    state2 = inv.get_source_state(w.SOURCE_WORKDAY, bad)
    assert state2["retry_count"] == 2
    assert state2["backoff_seconds"] > state["backoff_seconds"]


# ---------------------------------------------------------------------------
# 6. content_hash: fetch-once and parse-once
# ---------------------------------------------------------------------------

def test_changed_title_forces_a_refetch_but_unchanged_does_not(db, small_registry, monkeypatch):
    shard = w.shard_of(w.SOURCE_WORKDAY, small_registry[0]["member_key"])
    member = w.shard_members(w.SOURCE_WORKDAY, shard)[0]
    others = w.shard_members(w.SOURCE_WORKDAY, shard)[1:]
    calls = []

    def rows_for(title):
        table = {member["member_key"]: [_row(member, "REQ-1", title=title)]}
        table.update({m["member_key"]: [] for m in others})
        return table

    _patch_shard_io(monkeypatch, rows_for("Backend Engineer"), calls)
    w.run_shard(w.SOURCE_WORKDAY, shard, now=NOW)
    assert calls == [f"{member['member_key']}:REQ-1"]

    # Same listing fields -> the content_hash provably cannot have moved.
    calls.clear()
    _patch_shard_io(monkeypatch, rows_for("Backend Engineer"), calls)
    w.run_shard(w.SOURCE_WORKDAY, shard, now=NOW + timedelta(hours=1))
    assert calls == [], "an unchanged JD must not be re-fetched"

    # Retitled -> the hash inputs moved, so we must re-fetch and re-hash.
    calls.clear()
    _patch_shard_io(monkeypatch, rows_for("Staff Backend Engineer"), calls)
    out = w.run_shard(w.SOURCE_WORKDAY, shard, now=NOW + timedelta(hours=2))
    assert calls == [f"{member['member_key']}:REQ-1"]
    assert out["counts"]["changed"] == 1
    assert db.job_postings.count_documents({}) == 1, "a retitle updates, never forks"


def test_requirements_are_extracted_inline_and_reused_across_boards(db, small_registry,
                                                                    monkeypatch):
    """Requirement extraction happens IN the crawl, deterministically.

    This is what lets the request path score without loading a JD body:
    `fit_score.extract_requirements` is pure regex, so there is no reason to
    defer it to a queue and no Bedrock call to save. Parse-once still applies
    inventory-wide, so the same requisition on a second board reuses the stored
    profile rather than re-extracting.
    """
    from services import fit_score

    shard = w.shard_of(w.SOURCE_WORKDAY, small_registry[0]["member_key"])
    members = w.shard_members(w.SOURCE_WORKDAY, shard)
    assert len(members) >= 2, "this test needs a shard with two members"
    # Same employer name on both members, so the two postings hash IDENTICALLY:
    # this is the cross-board duplicate requisition that parse-once exists for
    # (employer_id is one of compute_content_hash's inputs).
    for m in members:
        m["display_name"] = "Acme Corp"
    body = ("We need 5+ years of Python, MongoDB and AWS. "
            "Preferred: Terraform. Identical body on both boards.")
    rows = {m["member_key"]: [_row(m, "REQ-1", description=body)] for m in members}
    _patch_shard_io(monkeypatch, rows)

    out = w.run_shard(w.SOURCE_WORKDAY, shard, now=NOW)

    assert out["counts"].get("parsed_deterministic", 0) >= 1
    # The LLM queue stays empty: the deterministic profile is the whole answer,
    # and the token-spending path is an optional enrichment, not a prerequisite.
    assert not out["parse_queue"]

    doc = db.job_postings.find_one({"source_job_id": {"$regex": "REQ-1$"}})
    reqs = doc["requirements"]
    assert reqs["score_version"] == fit_score.FIT_SCORE_VERSION
    assert "python" in reqs["required_skills"]
    assert reqs["source_content_hash"] == doc["content_hash"]
    # The stored profile must be COMPLETE enough for the scorer to trust it.
    # A partial profile is worse than none: absent requirements earn neutral
    # credit, so a lossy record scores HIGH rather than failing loudly.
    assert fit_score._precomputed_requirements(reqs) is not None

    # An identical body on a second board is one hash, so it is parsed once.
    hashes = {d["content_hash"] for d in db.job_postings.find({}, {"content_hash": 1})}
    assert len(hashes) == 1, "an identical JD on two boards is one hash"

    rows2 = {m["member_key"]: [_row(m, "REQ-2", description=body)] for m in members}
    _patch_shard_io(monkeypatch, rows2)
    out2 = w.run_shard(w.SOURCE_WORKDAY, shard, now=NOW + timedelta(hours=1))
    assert out2["counts"].get("parsed_reused", 0) >= 1
    assert not out2["parse_queue"]


def test_listing_body_skips_the_detail_fetch(db, small_registry, monkeypatch):
    """The detail budget bounds OUTBOUND REQUESTS, so only rows that would make
    one may spend it.

    Lever, Ashby and Recruitee ship the body in the listing payload. Charging
    those against `max_details` throttled ~450 of the 1309 career-page companies
    against a budget they never spend, and rows past the budget are not written
    at all, so the overflow was silently dropped from the inventory.
    """
    shard = w.shard_of(w.SOURCE_WORKDAY, small_registry[0]["member_key"])
    members = w.shard_members(w.SOURCE_WORKDAY, shard)
    rows = {}
    for i, m in enumerate(members):
        rows[m["member_key"]] = [
            _row(m, "WITH-BODY", description="Python, MongoDB and AWS required."),
            _row(m, "NO-BODY"),
        ]
    detail_calls = []
    _patch_shard_io(monkeypatch, rows, detail_calls)

    out = w.run_shard(w.SOURCE_WORKDAY, shard, now=NOW)

    assert all("NO-BODY" in c for c in detail_calls), \
        "a row that already has a body must not be fetched again"
    assert len(detail_calls) == len(members)
    assert out["counts"].get("detail_free", 0) == len(members)
    # Both kinds still land in the inventory.
    assert db.job_postings.count_documents({}) == 2 * len(members)


def test_detail_budget_only_counts_rows_that_would_fetch(db, small_registry, monkeypatch):
    """A shard whose rows all carry bodies must not report skipped work."""
    shard = w.shard_of(w.SOURCE_WORKDAY, small_registry[0]["member_key"])
    members = w.shard_members(w.SOURCE_WORKDAY, shard)
    rows = {m["member_key"]: [_row(m, f"REQ-{i}", description="Python and AWS.")
                              for i in range(5)] for m in members}
    _patch_shard_io(monkeypatch, rows)

    out = w.run_shard(w.SOURCE_WORKDAY, shard, now=NOW, max_details=1)

    assert out["counts"].get("detail_deferred", 0) == 0
    assert db.job_postings.count_documents({}) == 5 * len(members), \
        "a body-in-listing row must never be dropped by the detail budget"


def test_rows_past_the_detail_budget_are_written_not_discarded(
        db, small_registry, monkeypatch):
    """The budget bounds outbound REQUESTS, never the inventory.

    Overflow rows used to be dropped. For a vendor whose listing carries no
    body that meant losing nearly the whole employer: Rippling publishes 704
    such postings, every one needed a fetch, and 5 survived a shard-wide budget
    shared with twenty other boards. A posting with no description is still a
    job a seeker can see; an unwritten one is not.
    """
    shard = w.shard_of(w.SOURCE_WORKDAY, small_registry[0]["member_key"])
    members = w.shard_members(w.SOURCE_WORKDAY, shard)
    # No description anywhere, so every row wants a detail fetch.
    rows = {m["member_key"]: [_row(m, f"REQ-{i}") for i in range(5)]
            for m in members}
    _patch_shard_io(monkeypatch, rows)

    out = w.run_shard(w.SOURCE_WORKDAY, shard, now=NOW, max_details=1)

    assert out["counts"].get("detail_deferred", 0) == 5 * len(members) - 1
    assert db.job_postings.count_documents({}) == 5 * len(members), \
        "every listed row must reach the inventory, body or no body"


def test_requirements_extraction_is_disabled_by_default():
    """The only token-spending step in ingestion fails closed."""
    assert w.REQUIREMENTS_ENABLED is False
    out = w.extract_requirements([{"source": "workday", "source_job_id": "a:1",
                                   "content_hash": "a" * 64}])
    assert out["skipped"] == "requirements_disabled"


# ---------------------------------------------------------------------------
# 7. Cost class + closure safety
# ---------------------------------------------------------------------------

def test_ingested_rows_are_tagged_free_so_guests_can_read_them(db, small_registry, monkeypatch):
    """ADR-004: guest retrieval excludes paid sources STRUCTURALLY. This worker
    has no code path that can write a paid row."""
    shard = w.shard_of(w.SOURCE_WORKDAY, small_registry[0]["member_key"])
    members = w.shard_members(w.SOURCE_WORKDAY, shard)
    _patch_shard_io(monkeypatch, {m["member_key"]: [_row(m, "REQ-1")] for m in members})
    w.run_shard(w.SOURCE_WORKDAY, shard, now=NOW)

    assert db.job_postings.count_documents({"cost_class": "paid"}) == 0
    assert db.job_postings.count_documents({"cost_class": "free"}) > 0
    # And the guest read path actually returns them.
    found = inv.find_active_fresh(cost_classes=["free"], max_age_days=7, now=NOW)
    assert found and all(f["cost_class"] == "free" for f in found)


def test_closure_never_touches_postings_aged_past_the_listing_boundary(db, small_registry, monkeypatch):
    """Listing stops at 7 days, so an 8-day-old posting is legitimately absent
    from every future listing. Closing it because it was 'not seen' would delete
    real inventory on every single tick."""
    shard = w.shard_of(w.SOURCE_WORKDAY, small_registry[0]["member_key"])
    member = w.shard_members(w.SOURCE_WORKDAY, shard)[0]
    others = w.shard_members(w.SOURCE_WORKDAY, shard)[1:]

    old_table = {member["member_key"]: [_row(member, "OLD-1", days_ago=6),
                                        _row(member, "GONE-1", days_ago=6)]}
    old_table.update({m["member_key"]: [] for m in others})
    _patch_shard_io(monkeypatch, old_table)
    w.run_shard(w.SOURCE_WORKDAY, shard, now=NOW)
    assert db.job_postings.count_documents({"status": "active"}) == 2

    # Three days later both are 9 days old, so neither is listed any more.
    later = NOW + timedelta(days=3)
    empty = {m["member_key"]: [] for m in w.shard_members(w.SOURCE_WORKDAY, shard)}
    _patch_shard_io(monkeypatch, empty)
    w.run_shard(w.SOURCE_WORKDAY, shard, now=later)
    assert db.job_postings.count_documents({"status": "active"}) == 2, \
        "aged-out postings must not be closed as 'not seen'"

    # But a posting still INSIDE the window that vanished from the board IS closed.
    fresh_table = {member["member_key"]: [_row(member, "STAY-1", days_ago=1, now=later)],
                   **{m["member_key"]: [] for m in others}}
    _patch_shard_io(monkeypatch, fresh_table)
    w.run_shard(w.SOURCE_WORKDAY, shard, now=later)
    gone_table = {member["member_key"]: [], **{m["member_key"]: [] for m in others}}
    _patch_shard_io(monkeypatch, gone_table)
    w.run_shard(w.SOURCE_WORKDAY, shard, now=later + timedelta(hours=2))

    stay = inv.get_posting(w.SOURCE_WORKDAY, f"{member['member_key']}:STAY-1")
    assert stay["status"] == "closed" and stay["closed_reason"] == "not-seen"


def test_a_member_that_failed_to_list_never_has_its_jobs_closed(db, small_registry, monkeypatch):
    """A timed-out board must not read as 'this company closed every req'."""
    shard = w.shard_of(w.SOURCE_WORKDAY, small_registry[0]["member_key"])
    members = w.shard_members(w.SOURCE_WORKDAY, shard)
    victim = members[0]
    _patch_shard_io(monkeypatch, {m["member_key"]: [_row(m, "REQ-1")] for m in members})
    w.run_shard(w.SOURCE_WORKDAY, shard, now=NOW)
    assert db.job_postings.count_documents({"status": "active"}) == len(members)

    def flaky_list(member, **kw):
        if member["member_key"] == victim["member_key"]:
            raise RuntimeError("timeout")
        return [_row(member, "REQ-1", now=NOW + timedelta(hours=1))]

    monkeypatch.setattr(w, "list_member", flaky_list)
    out = w.run_shard(w.SOURCE_WORKDAY, shard, now=NOW + timedelta(hours=1))
    assert "error" not in out, "one dead member must not fail the shard"

    survivor = inv.get_posting(w.SOURCE_WORKDAY, f"{victim['member_key']}:REQ-1")
    assert survivor["status"] == "active", "a failed listing must not close its jobs"


# ---------------------------------------------------------------------------
# 8. Dispatch: bounded per tick, fair, and backpressured by leases
# ---------------------------------------------------------------------------

def test_dispatch_is_bounded_and_skips_leased_shards(db, small_registry, monkeypatch):
    enqueued = []

    def fake_enqueue(job_type, payload):
        enqueued.append((job_type, payload))
        return f"job-{len(enqueued)}"

    # Lease one shard: it is "already running", so this tick must skip it.
    busy = w.all_shards(w.SOURCE_WORKDAY)[0]
    inv.ensure_source_state(w.SOURCE_WORKDAY, busy, now=NOW)
    inv.acquire_lease(w.SOURCE_WORKDAY, busy, "busy-worker", now=NOW)

    out = w.dispatch_due_shards(w.SOURCE_WORKDAY, limit=2, now=NOW,
                                enqueue=fake_enqueue)

    assert out["count"] == 2, "the per-tick limit is the concurrency ceiling"
    assert len(enqueued) == 2
    shards = {p["shard"] for _t, p in enqueued}
    assert busy not in shards, "a leased shard is not due: that is the backpressure"
    assert all(t == "job_ingest_shard" for t, _p in enqueued)
    assert all(p["source"] == w.SOURCE_WORKDAY for _t, p in enqueued)


def test_dispatch_prefers_the_least_recently_succeeded_shard(db, small_registry, monkeypatch):
    """Fair rotation is the fix for the old nondeterministic 9% coverage."""
    shards = w.all_shards(w.SOURCE_WORKDAY)
    for i, shard in enumerate(shards):
        inv.ensure_source_state(w.SOURCE_WORKDAY, shard, now=NOW)
        inv._source_state().update_one(
            {"source": w.SOURCE_WORKDAY, "shard": shard},
            {"$set": {"last_success_at": NOW - timedelta(hours=len(shards) - i)}})

    picked = []
    out = w.dispatch_due_shards(w.SOURCE_WORKDAY, limit=2, now=NOW,
                                enqueue=lambda t, p: picked.append(p["shard"]) or "id")
    assert out["count"] == 2
    assert picked == shards[:2], "oldest success first"


def test_dispatch_registers_every_shard_idempotently(db, small_registry):
    w.dispatch_due_shards(w.SOURCE_WORKDAY, limit=0, now=NOW, enqueue=lambda t, p: "x")
    assert db.job_source_state.count_documents({"source": w.SOURCE_WORKDAY}) == 4
    # Second tick must not disturb existing state.
    inv.acquire_lease(w.SOURCE_WORKDAY, "s000", "keeper", now=NOW)
    w.dispatch_due_shards(w.SOURCE_WORKDAY, limit=0, now=NOW, enqueue=lambda t, p: "x")
    assert db.job_source_state.count_documents({"source": w.SOURCE_WORKDAY}) == 4
    assert inv.get_source_state(w.SOURCE_WORKDAY, "s000")["lease_owner"] == "keeper"


def test_write_stage_respects_the_shard_budget(db, small_registry, monkeypatch):
    """The write loop is deadline-checked like the IO stages.

    Writes are several database round trips per posting, so on a slow or
    distant cluster THIS is the stage that overruns. Unchecked, a shard sailed
    past its 90s budget and would be killed mid-loop at the Lambda's 300s
    timeout, losing the run record and holding the lease until its TTL.
    """
    shard = w.shard_of(w.SOURCE_WORKDAY, small_registry[0]["member_key"])
    members = w.shard_members(w.SOURCE_WORKDAY, shard)
    rows = {m["member_key"]: [_row(m, f"REQ-{i}") for i in range(4)] for m in members}
    _patch_shard_io(monkeypatch, rows)

    real_upsert = inv.upsert_posting
    clock = {"t": 0.0}
    monkeypatch.setattr(w.time, "monotonic", lambda: clock["t"])

    def slow_upsert(*a, **kw):
        clock["t"] += 40.0          # each write "takes" 40s of the 90s budget
        return real_upsert(*a, **kw)

    monkeypatch.setattr(inv, "upsert_posting", slow_upsert)

    out = w.run_shard(w.SOURCE_WORKDAY, shard, now=NOW, budget_seconds=90)

    assert "error" not in out
    assert out["counts"].get("write_skipped_budget", 0) > 0, \
        "the loop must stop at the deadline instead of running to the Lambda timeout"
    written = db.job_postings.count_documents({})
    assert 0 < written < 4 * len(members), "a partial write set, not all or nothing"
    # Safe to resume: the watermark only advanced over what was actually written.
    state = inv.get_source_state(w.SOURCE_WORKDAY, shard)
    assert state["lease_owner"] is None, "the lease is released even on a short run"
