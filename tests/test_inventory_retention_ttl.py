"""Retention is enforced by MongoDB, not by a schedule.

Every posting carries `expire_at` from the moment it is inserted, and a TTL
index deletes it once that passes. Nothing has to run for retention to happen,
which is the point: the scheduled sweep this replaces stopped on 2026-09-02,
when the crawl moved to EC2, and did not run once in the three weeks it took the
cluster to climb back to 4 GB.

The rules these tests pin down:

* `expire_at` is derived from the posting's own date, so a fresh posting cannot
  be deleted early by any later write. It is set ONCE, on insert.
* A pinned posting (applied / saved / tailored) carries NO `expire_at`. A TTL
  index ignores a document without the field, so a pinned row cannot be
  deleted, and a re-crawl cannot give the field back.
* A posting first seen when it is already older than the window still lives a
  day, so a board that keeps listing an old requisition is re-inserted at most
  daily rather than on every crawl.
"""
from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from services import job_inventory_service as inv

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
WINDOW = timedelta(days=inv.MAX_INVENTORY_AGE_DAYS)


_OWN = ("job_postings", "saved_jobs", "tailoring_records", "applications",
        "job_match_snapshots", "resume_jobs")


def _drop_ttl(mock_db):
    try:
        mock_db.job_postings.drop_index(inv.RETENTION_INDEX_NAME)
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _isolated(mock_db):
    """`mock_db` clears only its own whitelist; these tests share inventory
    collections with the rest of the suite, so they clear what they touch.

    The TTL index is dropped on both sides. mongomock honours TTL indexes, and
    the suite shares ONE database: left in place, the index would expire rows
    other modules insert with fixed historical dates, making them fail on a
    calendar rather than on a bug."""
    for name in _OWN:
        mock_db[name].delete_many({})
    _drop_ttl(mock_db)
    yield
    _drop_ttl(mock_db)


def _raw(sjid="acme:1", **over):
    base = {
        "source": "career-pages",
        "source_job_id": sjid,
        "url": f"https://boards.greenhouse.io/acme/jobs/{sjid}",
        "company": "Acme, Inc.",
        "title": "Senior Platform Engineer",
        "location": ["Austin, TX, USA"],
        "description": "Build and run the platform. Python, Kubernetes, AWS.",
        "employment_type": "Full time",
        "posted_at": NOW - timedelta(days=2),
        "cost_class": "free",
        "source_quality": "direct",
    }
    base.update(over)
    return base


def _utc(dt):
    return inv._safe_dt(dt)


def _doc(mock_db, sjid="acme:1"):
    return mock_db.job_postings.find_one({"source_job_id": sjid})


class TestExpiryIsStampedOnInsert:

    def test_a_fresh_posting_expires_a_window_after_it_was_posted(self, mock_db):
        inv.upsert_posting(_raw(), now=NOW)
        assert _utc(_doc(mock_db)["expire_at"]) == NOW - timedelta(days=2) + WINDOW

    def test_an_undated_posting_expires_a_window_after_it_was_first_seen(self, mock_db):
        inv.upsert_posting(_raw(posted_at=None), now=NOW)
        assert _utc(_doc(mock_db)["expire_at"]) == NOW + WINDOW

    def test_a_future_posted_date_cannot_buy_a_longer_life(self, mock_db):
        """Boards publish nonsense dates (2027-01-01 was on live data). A
        posting may not claim to be newer than the moment we first saw it."""
        inv.upsert_posting(_raw(posted_at=NOW + timedelta(days=100)), now=NOW)
        assert _utc(_doc(mock_db)["expire_at"]) <= NOW + WINDOW

    def test_an_already_old_posting_still_lives_a_day(self, mock_db):
        """Without the floor, a board that keeps listing a month-old
        requisition would have it deleted within a minute and re-inserted on
        every crawl."""
        inv.upsert_posting(_raw(posted_at=NOW - timedelta(days=30)), now=NOW)
        assert _utc(_doc(mock_db)["expire_at"]) == NOW + timedelta(
            hours=inv.RELISTED_MIN_LIFETIME_HOURS)

    def test_a_recrawl_does_not_move_the_expiry(self, mock_db):
        inv.upsert_posting(_raw(), now=NOW)
        first = _utc(_doc(mock_db)["expire_at"])
        inv.upsert_posting(_raw(), now=NOW + timedelta(days=3))
        assert _utc(_doc(mock_db)["expire_at"]) == first


class TestAPinnedPostingCanNeverExpire:

    def test_pinning_removes_the_expiry(self, mock_db):
        out = inv.upsert_posting(_raw(), now=NOW)
        inv.pin_posting(out["job_id"], reason="applied", now=NOW)
        doc = _doc(mock_db)
        assert doc["pinned"] is True
        assert "expire_at" not in doc

    def test_a_recrawl_after_pinning_does_not_restore_it(self, mock_db):
        out = inv.upsert_posting(_raw(), now=NOW)
        inv.pin_posting(out["job_id"], reason="saved", now=NOW)
        inv.upsert_posting(_raw(), now=NOW + timedelta(hours=1))
        assert "expire_at" not in _doc(mock_db)


class TestBackfill:

    def _insert(self, mock_db, sjid, **fields):
        mock_db.job_postings.insert_one({
            "job_id": sjid, "source": "career-pages", "source_job_id": sjid,
            "status": "active", "first_seen_at": NOW - timedelta(days=1), **fields,
        })

    def test_rows_written_before_the_ttl_existed_get_an_expiry(self, mock_db):
        self._insert(mock_db, "old", posted_at=NOW - timedelta(days=20))
        assert inv.backfill_retention_expiry() == 1
        assert _utc(_doc(mock_db, "old")["expire_at"]) == NOW - timedelta(days=20) + WINDOW

    def test_the_backfill_applies_no_floor(self, mock_db):
        """The floor exists to stop re-insert churn on NEW rows. A row that has
        been sitting past the window is exactly what the backfill is for."""
        self._insert(mock_db, "stale", posted_at=NOW - timedelta(days=20))
        inv.backfill_retention_expiry()
        assert _utc(_doc(mock_db, "stale")["expire_at"]) < datetime.now(timezone.utc)

    def test_the_backfill_never_touches_a_pinned_row(self, mock_db):
        self._insert(mock_db, "mine", posted_at=NOW - timedelta(days=20), pinned=True)
        assert inv.backfill_retention_expiry() == 0
        assert "expire_at" not in _doc(mock_db, "mine")

    def test_the_backfill_leaves_an_existing_expiry_alone(self, mock_db):
        keep = NOW + timedelta(days=3)
        self._insert(mock_db, "set", posted_at=NOW - timedelta(days=20), expire_at=keep)
        assert inv.backfill_retention_expiry() == 0
        assert _utc(_doc(mock_db, "set")["expire_at"]) == keep

    def test_a_row_with_no_date_at_all_is_left_undeletable(self, mock_db):
        """No anchor means no honest expiry. Leaving the field absent is the
        safe failure: the row is kept, and the watchdog reports it."""
        mock_db.job_postings.insert_one({"job_id": "x", "source": "s",
                                         "source_job_id": "x", "status": "active"})
        assert inv.backfill_retention_expiry() == 0
        assert "expire_at" not in _doc(mock_db, "x")


class TestTheIndex:

    def test_ensure_creates_a_ttl_index_on_expire_at(self, mock_db):
        inv.ensure_retention_index()
        info = mock_db.job_postings.index_information()
        ttl = [v for v in info.values() if v.get("key") == [("expire_at", 1)]]
        assert ttl and ttl[0].get("expireAfterSeconds") == 0

    def test_ensure_is_idempotent(self, mock_db):
        inv.ensure_retention_index()
        inv.ensure_retention_index()


class TestFeedResultsDoNotLiveAWeek:

    def _complete(self, job_type):
        from services.resume_service import get_resume_service
        svc = get_resume_service()
        job_id = svc.create_job(job_type, {}, "a@b.com")
        svc.complete_job(job_id, {"jobs": []})
        return svc.resume_jobs.find_one({"job_id": job_id})

    def test_a_job_feed_result_expires_within_a_day(self, mock_db):
        row = self._complete("career_pages")
        left = _utc(row["expire_at"]) - datetime.now(timezone.utc)
        assert timedelta(hours=23) < left <= timedelta(hours=24)

    def test_a_users_own_work_keeps_the_standard_week(self, mock_db):
        row = self._complete("tailor")
        left = _utc(row["expire_at"]) - datetime.now(timezone.utc)
        assert left > timedelta(days=6)


def _load_script(name):
    path = Path(__file__).resolve().parents[1] / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    # The scripts call load_dotenv() at import. Under test that would pull the
    # developer's real .env - JWT_SECRET_KEY, MONGO_URI - into the process for
    # every test that runs afterwards. CI has no .env; a laptop does.
    import dotenv
    real = dotenv.load_dotenv
    dotenv.load_dotenv = lambda *a, **k: False
    try:
        spec.loader.exec_module(mod)
    finally:
        dotenv.load_dotenv = real
    return mod


class TestPinBackfill:

    def test_a_saved_posting_is_pinned_and_loses_its_expiry(self, mock_db):
        jid = "a" * 32
        mock_db.job_postings.insert_one({"job_id": jid, "source": "s", "source_job_id": "1",
                                         "status": "active", "expire_at": NOW})
        mock_db.saved_jobs.insert_one({"job_id": jid, "user_email": "a@b.com"})
        report = _load_script("backfill_pins").pin_user_attached(mock_db, apply=True)
        doc = mock_db.job_postings.find_one({"job_id": jid})
        assert report["pinned"] == 1
        assert doc["pinned"] is True and "expire_at" not in doc

    def test_a_pinned_row_still_carrying_an_expiry_is_repaired(self, mock_db):
        jid = "b" * 32
        mock_db.job_postings.insert_one({"job_id": jid, "source": "s", "source_job_id": "2",
                                         "status": "active", "pinned": True,
                                         "expire_at": NOW})
        report = _load_script("backfill_pins").pin_user_attached(mock_db, apply=True)
        assert report["expiry_removed"] == 1
        assert "expire_at" not in mock_db.job_postings.find_one({"job_id": jid})

    def test_a_dry_run_writes_nothing(self, mock_db):
        jid = "c" * 32
        mock_db.job_postings.insert_one({"job_id": jid, "source": "s", "source_job_id": "3",
                                         "status": "active", "expire_at": NOW})
        mock_db.saved_jobs.insert_one({"job_id": jid, "user_email": "a@b.com"})
        _load_script("backfill_pins").pin_user_attached(mock_db, apply=False)
        doc = mock_db.job_postings.find_one({"job_id": jid})
        assert "pinned" not in doc and "expire_at" in doc


class TestWatchdogRestampsOldFeedResults:
    """Rows completed before `complete_job` learned the short expiry still say
    'a week'. The watchdog brings them in line; it never touches other types."""

    def test_an_old_feed_row_is_brought_forward_and_a_tailor_row_is_not(self, mock_db):
        now = datetime.now(timezone.utc).replace(microsecond=0)
        done = now - timedelta(hours=30)
        mock_db.resume_jobs.insert_many([
            {"job_id": "feed", "job_type": "career_pages", "status": "completed",
             "completed_at": done, "expire_at": done + timedelta(days=7)},
            {"job_id": "mine", "job_type": "tailor", "status": "completed",
             "completed_at": done, "expire_at": done + timedelta(days=7)},
        ])
        assert _load_script("retention_watchdog").shorten_feed_results(mock_db, now) == 1
        feed = mock_db.resume_jobs.find_one({"job_id": "feed"})
        mine = mock_db.resume_jobs.find_one({"job_id": "mine"})
        # Already past its 24h, so it is due now rather than in the past.
        assert _utc(feed["expire_at"]) == now
        assert _utc(mine["expire_at"]) == done + timedelta(days=7)
