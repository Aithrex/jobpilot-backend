"""A reader upgrade reaches postings whose body we already hold.

Measured 2026-11-03: 96k of 282k active postings carried an old reading
(74,660 Workday at v8, before `jd_profile` existed), and 84k of them had the
full body in our own store. An unchanged listing never re-fetched its detail,
and the only path that re-parsed was a detail fetch - so an EXTRACTOR_VERSION
bump reached the inventory at the speed of the detail budget.
"""
from datetime import datetime, timedelta, timezone

import pytest

from services import job_ingest_worker as w
from services import job_inventory_service as inv

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)
BODY = ("Senior Data Engineer. Requirements: 5+ years of experience with Python, "
        "Spark and Airflow. Bachelor's degree in Computer Science required. "
        "This role is fully remote. We are unable to sponsor visas.") * 3


@pytest.fixture()
def db(monkeypatch):
    d = inv._db()
    d.job_postings.delete_many({})
    monkeypatch.setattr(inv, "_now", lambda: NOW)
    yield d
    d.job_postings.delete_many({})


def _post(sjid="R1", body=BODY):
    inv.upsert_posting({
        "source": "workday", "source_job_id": sjid, "company": "Acme",
        "url": f"https://acme.wd1.myworkdayjobs.com/x/job/{sjid}", "title": "Senior Data Engineer",
        "location": ["Austin, TX, USA"], "description": body, "employment_type": "Full time",
        "posted_at": NOW - timedelta(days=3), "cost_class": "free", "source_quality": "direct"},
        now=NOW - timedelta(days=3))
    return inv._db().job_postings.find_one({"source_job_id": sjid})


def _age_the_reading(db, sjid="R1", version=8):
    doc = db.job_postings.find_one({"source_job_id": sjid})
    db.job_postings.update_one({"source_job_id": sjid}, {"$set": {
        "requirements": {"extractor_version": version, "source_content_hash": doc["content_hash"],
                         "required_skills": []}}})
    return inv.get_postings_map("workday", [sjid])[sjid]


def _listing():
    return {"title": "Senior Data Engineer", "employment_type": "Full time",
            "locations": ["Austin, TX, USA"]}


def test_only_the_reader_changed_is_recognised(db):
    _post()
    existing = _age_the_reading(db)
    assert w._listing_implies_same_content(existing, _listing()) is False
    assert w._only_reader_is_stale(existing, _listing()) is True
    # A different listing is a different posting: that still goes to detail.
    assert w._only_reader_is_stale(existing, dict(_listing(), title="Staff Data Engineer")) is False
    # A current reading needs nothing.
    current = dict(existing, requirements={"extractor_version": inv.EXTRACTOR_VERSION})
    assert w._only_reader_is_stale(current, _listing()) is False


def test_the_stored_body_is_re_read_with_the_current_reader(db):
    _post()
    existing = _age_the_reading(db)
    counts = {}
    from collections import defaultdict
    counts = defaultdict(int)
    w._reread_stored("workday", [(existing, "Senior Data Engineer")],
                     deadline=float("inf"), stop=None, counts=counts, now=NOW)
    req = db.job_postings.find_one({"source_job_id": "R1"})["requirements"]
    assert counts["reread_parsed"] == 1
    assert req["extractor_version"] == inv.EXTRACTOR_VERSION
    assert req["required_years"] == 5
    assert req.get("jd"), "the jd_profile block the old reader never wrote"


def test_a_tick_re_reads_at_most_its_cap(db, monkeypatch):
    from collections import defaultdict
    pending = []
    for i in range(3):
        _post(f"R{i}")
        pending.append((_age_the_reading(db, f"R{i}"), "Senior Data Engineer"))
    monkeypatch.setattr(w, "REREAD_PER_SHARD", 2)
    counts = defaultdict(int)
    w._reread_stored("workday", pending, deadline=float("inf"), stop=None, counts=counts, now=NOW)
    # Same body twice: the second reuses the first's fresh reading (parse-once).
    assert counts["reread_parsed"] + counts["reread_reused"] == 2
    assert counts["reread_deferred"] == 1
    versions = sorted(d["requirements"]["extractor_version"] for d in db.job_postings.find({}))
    assert versions == [8, inv.EXTRACTOR_VERSION, inv.EXTRACTOR_VERSION]


def test_an_expired_deadline_re_reads_nothing(db):
    from collections import defaultdict
    _post()
    existing = _age_the_reading(db)
    counts = defaultdict(int)
    w._reread_stored("workday", [(existing, "x")], deadline=0.0, stop=None, counts=counts, now=NOW)
    assert counts["reread_skipped_budget"] == 1
    assert db.job_postings.find_one({})["requirements"]["extractor_version"] == 8


def test_the_offloaded_copy_is_the_one_read(db, monkeypatch):
    from services import job_content_store
    monkeypatch.setattr(job_content_store, "get_jd", lambda ref: "FULL BODY" if ref == "h" * 64 else None)
    assert inv.stored_description("workday", {"description_ref": "h" * 64,
                                              "description_text": "prefix"}) == "FULL BODY"
    assert inv.stored_description("workday", {"description_text": "inline body"}) == "inline body"
    assert inv.stored_description("workday", {}) == ""


def _script():
    import importlib.util, pathlib
    p = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "reread_stale_requirements.py"
    spec = importlib.util.spec_from_file_location("reread_stale_requirements", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_backlog_script_is_a_dry_run_until_told(db, monkeypatch, capsys):
    import sys as _sys
    for i in range(2):
        _post(f"S{i}", body=BODY + f" Team {i}.")
        _age_the_reading(db, f"S{i}")
    mod = _script()
    monkeypatch.setattr(_sys, "argv", ["x"])
    assert mod.main() == 0
    assert "dry run - nothing was written" in capsys.readouterr().out
    assert {d["requirements"]["extractor_version"] for d in db.job_postings.find({})} == {8}

    monkeypatch.setattr(_sys, "argv", ["x", "--apply"])
    assert mod.main() == 0
    assert {d["requirements"]["extractor_version"] for d in db.job_postings.find({})} == {inv.EXTRACTOR_VERSION}
