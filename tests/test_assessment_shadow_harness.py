"""Offline tests for shadow harness + go/no-go honesty."""
from __future__ import annotations

from pathlib import Path

from evals.eval_assessment_shadow import (
    builtin_samples,
    run_shadow_sample,
    summarize,
    write_report,
)


def test_builtin_cohort_splits_and_professions():
    samples = builtin_samples()
    assert len(samples) >= 4
    assert {s.split for s in samples} >= {"calibration", "holdout"}
    assert len({s.profession for s in samples}) >= 3


def test_shadow_run_overall_null_and_report(tmp_path, monkeypatch):
    monkeypatch.delenv("EVAL_LIVE_LLM", raising=False)
    monkeypatch.delenv("RESUME_ASSESSMENT_V2_ENABLED", raising=False)
    results = [run_shadow_sample(s) for s in builtin_samples()]
    summary = summarize(results)
    assert summary["ok"] == summary["n"]
    assert summary["all_overall_null"] is True
    path = tmp_path / "shadow.json"
    doc = write_report(results, path)
    assert path.exists()
    assert doc["go_nogo"]["decision"] == "no-go"
    assert any("human" in b.lower() or "blinded" in b.lower() for b in doc["go_nogo"]["blockers"])
    assert doc["flag_default"].endswith("false")
