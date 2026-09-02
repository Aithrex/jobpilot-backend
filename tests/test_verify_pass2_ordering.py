"""Pass 2 spends its budget on the employers the coverage number is scored on.

Pass 2 is the expensive pass — six to ten live requests per employer — and it
used to walk the whole `employers` collection ordered by identity confidence.
Of 23,782 employers only 2,622 are in the technology denominator, so a sweep
ordered that way spent most of its budget outside the number it was trying to
move, and the employers with thousands of open engineering roles waited behind
employers with none.
"""
import importlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

verify = importlib.import_module("scripts.verify_career_sources")


class _Col:
    def __init__(self, docs):
        self._docs = list(docs)

    def find(self, *_a, **_k):
        return _Cursor(list(self._docs))


class _Cursor:
    def __init__(self, docs):
        self._docs = docs

    def sort(self, key, direction):
        self._docs.sort(key=lambda d: d.get(key) or 0, reverse=direction < 0)
        return self

    def limit(self, n):
        return list(self._docs[:n])

    def __iter__(self):
        return iter(self._docs)


EMPLOYERS = [
    {"employer_id": "emp_big", "identity_confidence": 0.45},
    {"employer_id": "emp_small", "identity_confidence": 0.95},
    {"employer_id": "emp_nontech", "identity_confidence": 0.95},
]


def _report(core_by_id):
    class _M:
        @staticmethod
        def build(_db, mix_days=30):
            return {"employers": [{"employer_id": eid, "_mix": {"core": n}}
                                  for eid, n in core_by_id.items()]}
    return _M


def test_a_high_yield_employer_outranks_a_more_confident_one(monkeypatch):
    """0.45 confidence with 900 open engineering roles is worth more than 0.95
    confidence with two, and the old order had it exactly backwards."""
    monkeypatch.setitem(sys.modules, "employer_coverage_report",
                        _report({"emp_big": 900, "emp_small": 2}))
    got = verify._tech_yield_order(object(), _Col(EMPLOYERS), {}, 10)
    assert [d["employer_id"] for d in got][:2] == ["emp_big", "emp_small"]


def test_employers_outside_the_technology_denominator_come_last(monkeypatch):
    """They are not dropped — an uncovered employer never silently leaves the
    sweep — but they do not displace a technology employer."""
    monkeypatch.setitem(sys.modules, "employer_coverage_report",
                        _report({"emp_big": 900, "emp_small": 2}))
    got = [d["employer_id"] for d in verify._tech_yield_order(object(), _Col(EMPLOYERS), {}, 10)]
    assert got[-1] == "emp_nontech"
    assert len(got) == 3


def test_a_budget_smaller_than_the_denominator_never_reaches_the_others(monkeypatch):
    monkeypatch.setitem(sys.modules, "employer_coverage_report",
                        _report({"emp_big": 900, "emp_small": 2}))
    got = [d["employer_id"] for d in verify._tech_yield_order(object(), _Col(EMPLOYERS), {}, 2)]
    assert got == ["emp_big", "emp_small"]


def test_a_report_that_cannot_be_built_falls_back_rather_than_verifying_nothing(monkeypatch):
    """The report reads `job_postings`. A sweep that refuses to run because it
    could not compute an ORDERING is strictly worse than a sweep in the old
    order."""
    class _Broken:
        @staticmethod
        def build(_db, mix_days=30):
            raise RuntimeError("no job_postings here")

    monkeypatch.setitem(sys.modules, "employer_coverage_report", _Broken)
    got = [d["employer_id"] for d in verify._tech_yield_order(object(), _Col(EMPLOYERS), {}, 10)]
    assert len(got) == 3
    # confidence order, which is what the fallback promises
    assert got[0] in {"emp_small", "emp_nontech"}
