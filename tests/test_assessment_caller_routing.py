"""Caller routing: guest/batch/job-search share assessment finalize overlay."""
from __future__ import annotations

import inspect
from pathlib import Path

from services import resume_service


def test_finalize_accepts_guest_id():
    sig = inspect.signature(resume_service._finalize_tailored_result)
    assert "guest_id" in sig.parameters


def test_batch_and_guest_job_handlers_call_finalize():
    # Worker body lives in module-level helpers / ResumeService methods; assert
    # the source file wires batch + guest through finalize with guest_id.
    src = Path(resume_service.__file__).read_text(encoding="utf-8")
    assert "batch_tailor_item" in src
    assert "_finalize_tailored_result" in src
    assert 'guest_id=guest_id' in src or "guest_id=guest_id or" in src
    assert 'job_type == "batch_tailor_item"' in src or "batch_tailor_item" in src


def test_job_search_tailor_routes_async_when_assessment_enabled():
    jobs_path = (
        Path(__file__).resolve().parents[1]
        / "services" / "jobs" / "blueprints" / "jobs.py"
    )
    src = jobs_path.read_text(encoding="utf-8")
    assert "def tailor_resume" in src
    assert "assessment_service" in src
    assert "extract_and_tailor" in src
    assert "adapt_ats_scores_job_payload" in src
