"""Saving a job must protect it from the retention sweep.

Applying pins the posting (`application_ledger_service` ->
`pin_posting(reason="applied")`). Saving never did, and `purge_expired`
deletes any closed posting past `purge_at` that is not pinned.

Measured on live data 2026-09-03: of 342 saved `job_id`s, 218 were still in
Mongo, exactly ONE was pinned, and 69 were already past their purge date. The
only reason 69 saved jobs had not been deleted is that the sweep was running
too far behind to reach them -- a catch-up would have destroyed them.
"""
from __future__ import annotations

import pytest

from services import job_inventory_service as inv


@pytest.fixture()
def svc(mock_db, monkeypatch):
    from services.job_service import JobService
    s = JobService()
    return s


def _posting(mock_db, job_id="j-1"):
    mock_db.job_postings.insert_one({
        "job_id": job_id, "source": "greenhouse", "source_job_id": "1",
        "status": "active", "title": "Backend Engineer",
    })
    return job_id


class TestSavingPinsThePosting:

    def test_a_saved_job_is_pinned(self, svc, mock_db, monkeypatch):
        jid = _posting(mock_db)
        monkeypatch.setattr(inv, "pin_posting",
                            lambda job_id, reason="": mock_db.job_postings.update_one(
                                {"job_id": job_id},
                                {"$set": {"pinned": True, "pin_reason": reason}}) and True)
        svc.save_job({"job_id": jid, "title": "Backend Engineer"}, "a@b.com")
        assert mock_db.job_postings.find_one({"job_id": jid})["pinned"] is True

    def test_the_reason_says_SAVED_not_applied(self, svc, mock_db, monkeypatch):
        jid = _posting(mock_db, "j-2")
        seen = {}
        monkeypatch.setattr(inv, "pin_posting",
                            lambda job_id, reason="": seen.update(id=job_id, reason=reason) or True)
        svc.save_job({"job_id": jid}, "a@b.com")
        assert seen == {"id": jid, "reason": "saved"}

    def test_a_pin_failure_never_fails_the_SAVE(self, svc, mock_db, monkeypatch):
        """Object storage being down must not stop a user saving a job. The
        posting is retained rather than deleted when the archive cannot be
        written, so the protective property still holds."""
        jid = _posting(mock_db, "j-3")

        def _boom(job_id, reason=""):
            raise RuntimeError("s3 unavailable")

        monkeypatch.setattr(inv, "pin_posting", _boom)
        out = svc.save_job({"job_id": jid}, "a@b.com")
        assert out["job_id"] == jid
        assert mock_db.saved_jobs.find_one({"job_id": jid, "user_email": "a@b.com"})

    def test_an_empty_job_id_is_not_pinned(self, svc, mock_db, monkeypatch):
        calls = []
        monkeypatch.setattr(inv, "pin_posting",
                            lambda job_id, reason="": calls.append(job_id) or True)
        svc.save_job({"job_id": ""}, "a@b.com")
        assert calls == []


class TestThePurgeRespectsThePin:

    def test_a_pinned_closed_posting_is_not_deleted_without_an_archive(self, mock_db, monkeypatch):
        """The guarantee behind "a job you saved is never lost": a pinned row
        whose archive is missing is RETAINED, not deleted."""
        from datetime import datetime, timedelta, timezone
        past = datetime.now(timezone.utc) - timedelta(days=1)
        mock_db.job_postings.insert_one({
            "job_id": "j-pin", "source": "greenhouse", "source_job_id": "9",
            "status": "closed", "purge_at": past, "pinned": True,
        })
        import services.job_content_store as jcs
        monkeypatch.setattr(jcs, "archive_exists", lambda job_id: False)
        monkeypatch.setattr(jcs, "archive_posting", lambda doc, reason="": False)
        out = inv.purge_expired(limit=10)
        assert out["deleted"] == 0
        assert out["retained_unarchived"] == 1
        assert mock_db.job_postings.find_one({"job_id": "j-pin"}) is not None

    def test_an_unpinned_closed_posting_IS_deleted(self, mock_db):
        from datetime import datetime, timedelta, timezone
        past = datetime.now(timezone.utc) - timedelta(days=1)
        mock_db.job_postings.insert_one({
            "job_id": "j-free", "source": "greenhouse", "source_job_id": "8",
            "status": "closed", "purge_at": past,
        })
        out = inv.purge_expired(limit=10)
        assert out["deleted"] >= 1
        assert mock_db.job_postings.find_one({"job_id": "j-free"}) is None


class TestUndatedPostingsStillAgeOut:
    """`posted_at < cutoff` never matches a null, so a posting the provider
    gave no date could only leave `active` if its board delisted it. A board
    that lists an undated job forever kept it here forever.

    Measured 2026-09-03: 18,460 active postings had no `posted_at`, 7,874 of
    them first seen over a week earlier.
    """

    @pytest.fixture(autouse=True)
    def _only_these_rows(self, mock_db):
        # The sweep counts every aged row in the collection. Rows other test
        # modules leave behind carry FIXED dates, so they cross the retention
        # window as the calendar moves and this count went from 1 to 2 on
        # 2026-10-07 with no code change.
        mock_db.job_postings.delete_many({})

    def _row(self, mock_db, jid, **extra):
        doc = {"job_id": jid, "source": "greenhouse", "source_job_id": jid,
               "status": "active"}
        doc.update(extra)
        mock_db.job_postings.insert_one(doc)

    def test_an_undated_posting_ages_out_on_first_seen_at(self, mock_db):
        from datetime import datetime, timedelta, timezone
        old = datetime.now(timezone.utc) - timedelta(days=30)
        self._row(mock_db, "undated", first_seen_at=old)
        assert inv.expire_aged_postings(limit=10) == 1
        assert mock_db.job_postings.find_one({"job_id": "undated"})["status"] != "active"

    def test_a_RECENT_undated_posting_is_left_alone(self, mock_db):
        from datetime import datetime, timezone
        self._row(mock_db, "fresh", first_seen_at=datetime.now(timezone.utc))
        assert inv.expire_aged_postings(limit=10) == 0
        assert mock_db.job_postings.find_one({"job_id": "fresh"})["status"] == "active"

    def test_a_DATED_posting_still_ages_on_posted_at(self, mock_db):
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        self._row(mock_db, "dated", posted_at=now - timedelta(days=30),
                  first_seen_at=now)
        assert inv.expire_aged_postings(limit=10) == 1

    def test_a_fresh_dated_posting_survives_an_old_first_seen(self, mock_db):
        """`posted_at` wins when present: a long-running requisition we first
        saw weeks ago but which the board re-dated is still current."""
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        self._row(mock_db, "redated", posted_at=now,
                  first_seen_at=now - timedelta(days=30))
        assert inv.expire_aged_postings(limit=10) == 0
