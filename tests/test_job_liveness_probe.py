"""Apply-URL liveness probing - the one death signal that is not an inference.

Everything else in the ingest worker infers that a posting is gone from its
ABSENCE: `_close_stale_in_shard` when a listing stops returning it,
`verify_liveness` when no crawl has confirmed it for days. Both are blind to
the common case where an ATS keeps a requisition in its list API after the
detail page has closed, so the posting looks live to us and dead to whoever
clicks Apply.

These tests pin the asymmetry the feature is built around: a wrong "dead"
costs a candidate an application they would have made, a wrong "alive" costs
one wasted click. So every inconclusive answer must mean alive, and closing
must need corroboration.

Runs against the suite's in-memory mongomock DB (tests/_vg_support.py).
"""
from datetime import datetime, timedelta, timezone

import pytest

from services import job_ingest_worker as worker
from services import job_inventory_service as inv

NOW = datetime(2026, 7, 21, 12, 0, 0, tzinfo=timezone.utc)
OLD = NOW - timedelta(days=3)          # past the probe grace period
URL = "https://boards.greenhouse.io/acme/jobs/{}"

INVENTORY_COLLECTIONS = ("job_postings", "job_source_state",
                         "job_ingestion_runs", "job_match_snapshots")


@pytest.fixture()
def db():
    d = inv._db()
    for c in INVENTORY_COLLECTIONS:
        d[c].delete_many({})
    return d


def _seed(n=1, *, first_seen=OLD, source="career-pages", host=None):
    """n active career-page postings, old enough to be probe-eligible."""
    ids = []
    for i in range(n):
        sjid = f"job-{i}"
        url = (host or URL).format(i)
        inv.upsert_posting({
            "source": source, "source_job_id": sjid, "url": url,
            "company": "Acme", "title": f"Engineer {i}",
            "location": ["Austin, TX, USA"], "description": "x" * 400,
            "cost_class": "free", "posted_at": OLD,
        }, now=first_seen)
        ids.append(sjid)
    return ids


def _status_map(mapping, default=200):
    """Stub `http_probe_status`, keyed by the job index in the URL."""
    def _fn(source, url, policy, *, deadline, stop=None, timeout=None):
        idx = url.rstrip("/").rsplit("/", 1)[-1]
        return mapping.get(idx, default)
    return _fn


def _status_of(doc_id, source="career-pages"):
    doc = inv._postings().find_one({"source": source, "source_job_id": doc_id})
    return doc.get("status"), doc.get("liveness_dead_strikes", 0)


# ---------------------------------------------------------------------------
# Closing needs corroboration
# ---------------------------------------------------------------------------

def test_one_404_records_a_strike_but_does_not_close(db, monkeypatch):
    """A single dead reading is not enough. CDN and regional blips 404 once;
    a candidate who loses a real posting to one never gets it back."""
    _seed(1)
    monkeypatch.setattr(worker, "http_probe_status", _status_map({"0": 404}))

    out = worker.probe_liveness(limit=10, now=NOW)

    assert out["dead"] == 1
    assert out["closed"] == 0
    status, strikes = _status_of("job-0")
    assert status == inv.STATUS_ACTIVE
    assert strikes == 1


def test_a_second_consecutive_404_closes_it(db, monkeypatch):
    _seed(1)
    monkeypatch.setattr(worker, "http_probe_status", _status_map({"0": 404}))

    worker.probe_liveness(limit=10, now=NOW)
    # The interval gate would skip it on the same day; a later sweep is the
    # real second chance.
    later = NOW + timedelta(days=2)
    out = worker.probe_liveness(limit=10, now=later)

    assert out["closed"] == 1
    doc = inv._postings().find_one({"source": "career-pages", "source_job_id": "job-0"})
    assert doc["status"] in inv.CLOSED_STATUSES
    assert doc["closed_reason"] == inv.CLOSED_REASON_LINK_DEAD


def test_410_gone_counts_as_dead(db, monkeypatch):
    _seed(1)
    monkeypatch.setattr(worker, "http_probe_status", _status_map({"0": 410}))
    assert worker.probe_liveness(limit=10, now=NOW)["dead"] == 1


def test_a_live_reading_clears_earlier_strikes(db, monkeypatch):
    """Strikes mean 'dead this many times IN A ROW'. A posting that 404s once
    during a blip and answers next sweep must not be one reading from closure
    forever after."""
    _seed(1)
    monkeypatch.setattr(worker, "http_probe_status", _status_map({"0": 404}))
    worker.probe_liveness(limit=10, now=NOW)
    assert _status_of("job-0")[1] == 1

    monkeypatch.setattr(worker, "http_probe_status", _status_map({"0": 200}))
    worker.probe_liveness(limit=10, now=NOW + timedelta(days=2))
    assert _status_of("job-0") == (inv.STATUS_ACTIVE, 0)


# ---------------------------------------------------------------------------
# Everything inconclusive means alive
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status", [200, 301, 302, 403, 429, 500, 503, None])
def test_no_other_answer_ever_closes_or_strikes(db, monkeypatch, status):
    """403 is a fact about us, not the requisition; 5xx and timeouts are facts
    about the moment. None of them is the posting saying it is gone."""
    _seed(1)
    monkeypatch.setattr(worker, "http_probe_status", _status_map({"0": status}))

    out = worker.probe_liveness(limit=10, now=NOW)

    assert out["closed"] == 0
    assert out["dead"] == 0
    assert _status_of("job-0") == (inv.STATUS_ACTIVE, 0)


def test_a_non_allowlisted_url_is_not_a_reading(db, monkeypatch):
    """A URL we refuse to probe tells us nothing, so it must not accumulate a
    strike - otherwise refusing to look would eventually close the posting."""
    _seed(1, host="https://evil.example.com/jobs/{}")

    def _boom(*a, **k):
        raise AssertionError("must not probe a non-allowlisted host")

    monkeypatch.setattr(worker, "http_probe_status", _boom)
    # The real function raises ValueError before any request; assert the guard
    # itself rather than the stub.
    with pytest.raises(ValueError):
        worker.assert_probe_url("career-pages", "https://evil.example.com/jobs/0")


# ---------------------------------------------------------------------------
# Systemic failure must not cascade
# ---------------------------------------------------------------------------

def test_a_host_that_looks_dead_everywhere_closes_nothing(db, monkeypatch):
    """If most of a host's postings read dead in one sweep, the likely cause is
    that the ATS changed its URL scheme or is 404ing under load - not that every
    employer on it closed at once. This is the 686-Greenhouse-companies failure
    the crawl's own comments worry about, arriving through a faster signal."""
    _seed(12)
    monkeypatch.setattr(worker, "http_probe_status",
                        _status_map({str(i): 404 for i in range(12)}))

    out = worker.probe_liveness(limit=50, now=NOW)

    assert out["dead"] == 12
    assert out["closed"] == 0
    assert out["skipped_host"] == 12
    # And critically: no strikes banked, or the next sweep closes all 12.
    assert all(_status_of(f"job-{i}")[1] == 0 for i in range(12))


def test_a_few_dead_among_many_live_still_close(db, monkeypatch):
    """The guard must not become a blanket amnesty: normal churn is a minority
    of a host's postings going dead, and that is exactly what we want closed."""
    _seed(12)
    monkeypatch.setattr(worker, "http_probe_status",
                        _status_map({"0": 404, "1": 404}, default=200))

    first = worker.probe_liveness(limit=50, now=NOW)
    assert first["dead"] == 2 and first["skipped_host"] == 0

    worker.probe_liveness(limit=50, now=NOW + timedelta(days=2))
    assert _status_of("job-0")[0] in inv.CLOSED_STATUSES
    assert _status_of("job-2")[0] == inv.STATUS_ACTIVE


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------

def test_a_brand_new_posting_is_not_probed(db, monkeypatch):
    """Several ATSs publish to the list API before the public page propagates,
    so a minutes-old posting can be genuinely 404 while being perfectly real."""
    _seed(1, first_seen=NOW - timedelta(minutes=5))
    monkeypatch.setattr(worker, "http_probe_status", _status_map({"0": 404}))

    out = worker.probe_liveness(limit=10, now=NOW)

    assert out["probed"] == 0
    assert _status_of("job-0") == (inv.STATUS_ACTIVE, 0)


def test_least_recently_probed_first(db):
    """A bounded sweep only covers the inventory over time if it always takes
    the oldest readings; re-probing the same head makes no progress."""
    _seed(3)
    inv.record_liveness_probe("career-pages", "job-0", alive=True, now=NOW)
    inv.record_liveness_probe("career-pages", "job-1", alive=True,
                              now=NOW - timedelta(days=5))

    rows = inv.find_for_liveness_probe(
        "career-pages", limit=10, first_seen_before=NOW,
        probed_before=NOW + timedelta(hours=1))
    order = [r["source_job_id"] for r in rows]

    assert order[0] == "job-2", "never-probed must come before ever-probed"
    assert order.index("job-1") < order.index("job-0")


def test_a_closed_posting_is_never_probed_again(db):
    _seed(1)
    inv.close_posting("career-pages", "job-0", reason="not-seen", now=NOW)
    assert inv.find_for_liveness_probe("career-pages", limit=10) == []


def test_a_posting_without_a_url_is_never_selected(db):
    """No URL is nothing to ask, and a missing field must never read as dead."""
    _seed(1)
    inv._postings().update_one({"source_job_id": "job-0"},
                               {"$set": {"canonical_url": ""}})
    assert inv.find_for_liveness_probe("career-pages", limit=10) == []


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------

def test_probing_is_off_unless_a_limit_is_passed(db, monkeypatch):
    """Reaching out to employer infrastructure is an explicit decision at the
    call site, never a default that arrives with a deploy."""
    def _boom(*a, **k):
        raise AssertionError("probe_liveness must not run without probe_limit")

    monkeypatch.setattr(worker, "probe_liveness", _boom)
    assert "probed" not in worker.run_maintenance(now=NOW)


def test_run_maintenance_reports_probe_counts(db, monkeypatch):
    monkeypatch.setattr(worker, "probe_liveness",
                        lambda **k: {"probed": 3, "dead": 1, "closed": 0,
                                     "inconclusive": 2, "skipped_host": 0})
    out = worker.run_maintenance(now=NOW, probe_limit=10)
    assert out["probed"]["probed"] == 3
