"""The read-only inventory export, end to end on seeded mongomock data.

Pinned: every input row exactly once; a fixed `as_of` watermark; keyset paging
by `_id`; duplicates by PROVEN identity only (a generic portal never merges);
explicit `partial` on a failed page or a row cap; writes are refused.
"""
from __future__ import annotations

import csv
import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from services import job_inventory_service as inv

ROOT = Path(__file__).resolve().parent.parent
T0 = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
AS_OF = T0 + timedelta(hours=1)
# Generic portal with NO job id anywhere (the worst case): must never merge.
PORTAL = "https://www1.jobdiva.com/portal/?a=aljdnw0qolq9pzanc6e7lzw46uuoh200575&jr_id={}"


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "export_inventory_audit", ROOT / "scripts" / "export_inventory_audit.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def exp():
    return _load_script()


@pytest.fixture()
def db(mock_db, monkeypatch):
    monkeypatch.delenv("RESUME_S3_BUCKET", raising=False)
    for c in ("job_postings", "posting_sightings"):
        mock_db[c].delete_many({})
    _seed()
    yield mock_db
    for c in ("job_postings", "posting_sightings"):
        mock_db[c].delete_many({})


def _up(sjid, title, url, *, source="career-pages", company="Acme", ats="greenhouse",
        tenant="acme", location="Austin, TX, USA", now=T0):
    inv.upsert_posting({"source": source, "source_job_id": sjid, "url": url,
                        "company": company, "title": title, "location": [location],
                        "description": f"{title}. Python.", "cost_class": "free",
                        "ats": ats, "tenant": tenant, "posted_at": T0 - timedelta(days=2)},
                       now=now)


def _seed():
    _up("gh-acme:1", "Backend Engineer", "https://boards.greenhouse.io/acme/jobs/1")
    # Same requisition URL through an aggregator -> proven duplicate.
    _up("agg:991", "Backend Engineer", "https://boards.greenhouse.io/acme/jobs/1?utm_source=x",
        source="aggregator", ats="", tenant="")
    # Same company/title/city, different requisition -> possible duplicate (review).
    _up("gh-acme:2", "Backend Engineer", "https://boards.greenhouse.io/acme/jobs/2")
    # Three different roles behind one generic portal -> never merged.
    for i, t in enumerate(["Software Engineer I", "SRE II", "Python Data Engineer"]):
        _up(f"jd-scal:{i}", t, PORTAL.format(i), company="Scalence", ats="jobdiva",
            tenant="")
    # Closed.
    _up("gh-acme:3", "Frontend Engineer", "https://boards.greenhouse.io/acme/jobs/3")
    inv._postings().update_one({"source_job_id": "gh-acme:3"}, {"$set": {"status": "closed"}})
    # Arrives after the watermark -> counted, not exported.
    _up("gh-acme:4", "Data Engineer", "https://boards.greenhouse.io/acme/jobs/4",
        now=AS_OF + timedelta(minutes=5))


def _rows(out, name):
    with open(out / f"{name}.csv", newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def test_every_row_exactly_once_with_decisions(db, exp, tmp_path):
    summary = exp.run_export(db, tmp_path, as_of=AS_OF, page_size=2, policy=None)
    rows = _rows(tmp_path, "all_rows")
    assert len(rows) == 7 == summary["denominators"]["input_rows_exported"]
    assert len({r["doc_id"] for r in rows}) == 7
    assert summary["denominators"]["arrived_after_as_of_not_exported"] == 1
    assert summary["every_row_once"] is True and summary["partial"] is False
    assert summary["pages_read"] == 4                       # 2+2+2+1, keyset by _id
    by_sjid = {r["source_job_id"]: r for r in rows}
    assert by_sjid["agg:991"]["decision"] == "excluded"
    assert by_sjid["agg:991"]["reason"].startswith("duplicate_of:")
    assert by_sjid["gh-acme:3"]["reason"] == "closed:closed"
    portal = [by_sjid[f"jd-scal:{i}"] for i in range(3)]
    assert not any(r["duplicate_of"] for r in portal)
    assert {r["dedupe_group"] for r in portal}.__len__() == 3
    # Without the policy module every active, non-duplicate row is reviewable.
    assert by_sjid["gh-acme:1"]["reason"] == "unclassified"
    files = summary["files"]
    assert files["accepted_unique"] + files["exclusions"] + files["unknown_review"] == 7
    json.loads((tmp_path / "summary.json").read_text())
    assert (tmp_path / "all_rows.jsonl").read_text().count("\n") == 7


class _FakePolicy:
    STRICT_EXCLUDE = {"staffing": True}

    @staticmethod
    def annotate(row):
        staffing = row.get("employer_name") == "Scalence"
        row["employer_profile"] = {"kind": "staffing" if staffing else "operating",
                                   "basis": "test", "evidence": [{"kind": "rule"}]}
        row["posting_profile"] = {"relationship": "unknown" if staffing else "own_role",
                                  "purpose": "requisition", "evidence": []}
        row["availability"] = {"state": "verified_open"}
        return row

    @staticmethod
    def reasons_for(row, exclude):
        return ["staffing"] if row["employer_profile"]["kind"] == "staffing" else []


def test_policy_classification_drives_exclusions_and_review(db, exp, tmp_path):
    summary = exp.run_export(db, tmp_path, as_of=AS_OF, policy=_FakePolicy())
    print("EXPORT_SUMMARY", json.dumps({k: summary[k] for k in ("as_of", "partial", "denominators", "files", "by_decision", "by_reason", "data_age", "every_row_once")}))
    by_sjid = {r["source_job_id"]: r for r in _rows(tmp_path, "all_rows")}
    assert by_sjid["jd-scal:0"]["reason"] == "policy:staffing"
    assert by_sjid["gh-acme:1"]["decision"] == "accepted"
    assert by_sjid["gh-acme:2"]["reason"].startswith("possible_duplicate_of:")
    assert by_sjid["gh-acme:1"]["business_model_kind"] == "operating"
    assert summary["by_decision"] == {"accepted": 1, "excluded": 5, "review": 1}
    assert [r["source_job_id"] for r in _rows(tmp_path, "accepted_unique")] == ["gh-acme:1"]


def test_a_failed_page_is_reported_partial_never_complete(db, exp, tmp_path):
    class Flaky:
        def __init__(self, real):
            self.real, self.calls = real, 0

        def __getitem__(self, name):
            outer = self
            real = self.real[name]

            class C:
                name = real.name

                def find(self, *a, **k):
                    outer.calls += 1
                    if outer.calls > 1:
                        raise RuntimeError("network blip")
                    return real.find(*a, **k)

                def count_documents(self, *a, **k):
                    return real.count_documents(*a, **k)
            return C()

    summary = exp.run_export(Flaky(db), tmp_path, as_of=AS_OF, page_size=3, policy=None)
    assert summary["partial"] is True
    assert summary["partial_reasons"][0].startswith("page_failed_after_id:")
    assert summary["denominators"]["input_rows_exported"] == 3


def test_a_row_cap_marks_the_export_partial(db, exp, tmp_path):
    summary = exp.run_export(db, tmp_path, as_of=AS_OF, max_rows=2, policy=None)
    assert summary["partial"] is True and "max_rows_reached:2" in summary["partial_reasons"]
    assert len(_rows(tmp_path, "all_rows")) == 2


def test_the_export_cannot_write(db, exp):
    ro = exp.ReadOnlyDB(db)
    with pytest.raises(RuntimeError, match="read-only export"):
        ro["job_postings"].delete_many({})
    with pytest.raises(RuntimeError, match="read-only export"):
        ro.job_postings.update_one({}, {"$set": {"x": 1}})
    assert db.job_postings.count_documents({}) == 8
