"""Provenance on `job_postings`, and a publication date a re-crawl cannot reset.

CLAUDE-PROMPT.txt D: keep source_url, original_job_url, canonical_apply_url,
source_job_id and the employer requisition id; distinguish original
publication, first seen, last seen, repost and material content change; "a
fresh crawl/repost must not make an old requisition newly published".

`upsert_posting` used to `$set` `posted_at` unconditionally, so a write that
carried a body but no date ERASED a stored publication date, and a board that
re-stamped a requisition with today's date made a months-old job "new".
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from services import job_inventory_service as inv

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
SJID = "gh-acme:42"


@pytest.fixture(autouse=True)
def _clean(mock_db):
    for c in ("job_postings", "posting_sightings"):
        mock_db[c].delete_many({})
    yield
    for c in ("job_postings", "posting_sightings"):
        mock_db[c].delete_many({})


def _raw(**over):
    base = {"source": "career-pages", "source_job_id": SJID,
            "url": "https://boards.greenhouse.io/acme/jobs/42",
            "company": "Acme", "title": "Backend Engineer", "location": ["Austin, TX, USA"],
            "description": "Build services in Python.", "cost_class": "free",
            "ats": "greenhouse", "tenant": "acme"}
    base.update(over)
    return base


def _doc():
    return inv.get_posting("career-pages", SJID)


def _utc(value):
    return value if value is None or value.tzinfo else value.replace(tzinfo=timezone.utc)


class TestPostedAtIsNeverReset:
    def test_a_write_without_a_date_does_not_erase_the_stored_date(self):
        inv.upsert_posting(_raw(posted_at=T0 - timedelta(days=20)), now=T0)
        inv.upsert_posting(_raw(posted_at=None, description="Build services in Python. v2"),
                           now=T0 + timedelta(days=1))
        doc = _doc()
        assert _utc(doc["posted_at"]) == T0 - timedelta(days=20)
        assert doc["posted_at_confidence"] == inv.CONFIDENCE_EXACT

    def test_a_repost_does_not_make_the_requisition_newly_published(self):
        inv.upsert_posting(_raw(posted_at=T0 - timedelta(days=40)), now=T0)
        inv.upsert_posting(_raw(posted_at=T0 + timedelta(days=2)), now=T0 + timedelta(days=2))
        doc = _doc()
        assert _utc(doc["posted_at"]) == T0 - timedelta(days=40)
        assert _utc(doc["reposted_at"]) == T0 + timedelta(days=2)
        assert [h["event"] for h in doc["change_history"]][-1] == "reposted"

    def test_an_undated_posting_learns_its_date(self):
        inv.upsert_posting(_raw(posted_at=None), now=T0)
        inv.upsert_posting(_raw(posted_at=T0 - timedelta(days=3)), now=T0 + timedelta(hours=1))
        assert _utc(_doc()["posted_at"]) == T0 - timedelta(days=3)

    def test_an_exact_date_replaces_a_relative_estimate(self):
        inv.upsert_posting(_raw(posted_at=T0 - timedelta(days=30),
                                posted_at_confidence=inv.CONFIDENCE_RELATIVE), now=T0)
        inv.upsert_posting(_raw(posted_at=T0 - timedelta(days=12),
                                posted_at_confidence=inv.CONFIDENCE_EXACT), now=T0)
        doc = _doc()
        assert _utc(doc["posted_at"]) == T0 - timedelta(days=12)
        assert doc["posted_at_confidence"] == inv.CONFIDENCE_EXACT

    def test_first_seen_is_untouched_by_later_crawls(self):
        inv.upsert_posting(_raw(posted_at=None), now=T0)
        inv.upsert_posting(_raw(posted_at=None), now=T0 + timedelta(days=5))
        assert _utc(_doc()["first_seen_at"]) == T0


class TestChangeHistory:
    def test_material_changes_are_recorded_and_bounded(self):
        inv.upsert_posting(_raw(posted_at=T0), now=T0)
        for i in range(inv.CHANGE_HISTORY_LIMIT + 4):
            inv.upsert_posting(_raw(posted_at=T0, description=f"Build services, rev {i}."),
                               now=T0 + timedelta(hours=i + 1))
        hist = _doc()["change_history"]
        assert len(hist) == inv.CHANGE_HISTORY_LIMIT
        assert hist[-1]["event"] == "content_changed"
        assert hist[-1]["content_hash"] == _doc()["content_hash"]
        assert {"content_hash", "seen_at", "event"} <= set(hist[-1])

    def test_an_unchanged_recrawl_adds_nothing(self):
        inv.upsert_posting(_raw(posted_at=T0), now=T0)
        inv.upsert_posting(_raw(posted_at=T0), now=T0 + timedelta(hours=1))
        assert [h["event"] for h in _doc()["change_history"]] == ["first_seen"]


class TestProvenanceFields:
    def test_provider_supplied_provenance_is_kept(self):
        inv.upsert_posting(_raw(
            posted_at=T0,
            source_url="https://boards-api.greenhouse.io/v1/boards/acme/jobs",
            original_job_url="https://acme.com/careers?gh_jid=42",
            canonical_apply_url="https://boards.greenhouse.io/acme/jobs/42/apply",
            requisition_id="REQ-2026-001"), now=T0)
        doc = _doc()
        assert doc["source_job_id"] == SJID
        assert doc["source_url"].startswith("https://boards-api.greenhouse.io/")
        assert doc["original_job_url"] == "https://acme.com/careers?gh_jid=42"
        assert doc["canonical_apply_url"].endswith("/jobs/42/apply")
        assert doc["requisition_id"] == "REQ-2026-001"
        assert doc["apply_url_key"] == "boards.greenhouse.io/acme/jobs/42"

    def test_absent_provenance_stays_absent(self):
        inv.upsert_posting(_raw(posted_at=T0), now=T0)
        doc = _doc()
        for field in ("source_url", "original_job_url", "canonical_apply_url",
                      "requisition_id"):
            assert field not in doc
