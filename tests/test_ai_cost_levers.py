"""The four AI cost/latency fixes measured on Vertex on 2026-11-10.

1. The summary writer and the claim guard agree on years of experience.
2. Prompts carry compact JSON.
3. Every Gemini call is recorded per feature (`llm_usage`).
4. JD analysis is served from an exact-prompt cache (`llm_response_cache`).
"""
import json
import re
from pathlib import Path

import pytest

from services import llm_response_cache, llm_usage
from services.claim_guard import audit_resume
from services.summary_tailor import _years_phrase
from utils.llm_safety import prompt_json

ROOT = Path(__file__).resolve().parents[1]
_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _resume(dates, summary="Backend engineer building Python services.", education=True):
    return {
        "summary": summary,
        "experience": [
            {"title": "Engineer", "company": f"Co{i}", "dates": d, "bullets": ["Built a FastAPI service."]}
            for i, d in enumerate(dates)
        ],
        "education": [{"degree": "B.Tech", "dates": "May 2021"}] if education else [],
    }


def _summary_flags(structured, summary):
    tailored = dict(structured, summary=summary)
    _, audit = audit_resume(tailored, structured, {}, mode="flag")
    return [f for addr, item in audit.items.items() if addr.startswith("summary")
            for f in item.get("flags") or []]


# ---------------------------------------------------------------------------
# 1. Years of experience
# ---------------------------------------------------------------------------

def test_live_case_nearly_four_years_is_grounded():
    # The synthetic resume from scripts/check_ai_features.py: 46 months from
    # dates that end in "Present". The writer cites "nearly 4 years"; the guard
    # used to stop at 3 (four-digit years only) and rejected it twice.
    structured = _resume(["Jan 2023 to Oct 2026", "May 2022 to Aug 2022"])
    assert _years_phrase(structured, {}) == "with nearly 4 years"
    assert _summary_flags(structured, "Backend engineer with nearly 4 years building Python services.") == []


def test_more_years_than_the_dates_support_are_still_flagged():
    structured = _resume(["Jan 2023 to Oct 2026", "May 2022 to Aug 2022"])
    assert _summary_flags(structured, "Backend engineer with 7 years building Python services.") == ["ungrounded_metric"]
    assert _summary_flags(structured, "Backend engineer with 9 years building Python services.") == ["ungrounded_metric"]


@pytest.mark.parametrize("months", list(range(12, 120, 5)))
def test_the_guard_accepts_every_years_figure_the_writer_cites(months):
    end = 2016 * 12 + months - 1
    dates = f"Jan 2016 to {_MONTHS[end % 12]} {end // 12}"
    structured = _resume([dates], education=False)
    phrase = _years_phrase(structured, {})
    number = re.search(r"\d+", phrase).group(0)
    assert _summary_flags(structured, f"Engineer {phrase} building Python services.") == []
    # and a figure past what the dates support is still flagged
    too_many = int(number) + 2
    assert _summary_flags(structured, f"Engineer with {too_many} years building Python services.")


# ---------------------------------------------------------------------------
# 2. Compact JSON
# ---------------------------------------------------------------------------

def test_prompt_json_is_compact_and_lossless():
    value = {"name": "Zoë Ångström", "skills": ["Python", "SQL"], "n": {"a": 1}}
    text = prompt_json(value)
    assert text == '{"name":"Zoë Ångström","skills":["Python","SQL"],"n":{"a":1}}'
    assert json.loads(text) == value
    assert len(text) < len(json.dumps(value, indent=2))


def test_no_prompt_builder_pretty_prints_json():
    for rel in ("services/resume_tailor.py", "services/resume_scorer.py", "services/resume_service.py"):
        source = (ROOT / "shared" / "python" / rel).read_text(encoding="utf-8")
        assert "indent=2" not in source, rel


def test_smart_truncate_keeps_compact_json_and_every_section():
    from services.resume_tailor import ResumeTailor
    resume = {"summary": "s", "skills": ["Python"],
              "experience": [{"company": f"C{i}", "bullets": ["x" * 400] * 6} for i in range(5)]}
    out = ResumeTailor._smart_truncate(prompt_json(resume), max_chars=6000)
    assert len(out) <= 6000 and "\n" not in out
    parsed = json.loads(out)
    assert [e["company"] for e in parsed["experience"]] == [f"C{i}" for i in range(5)]


# ---------------------------------------------------------------------------
# 3. Usage ledger
# ---------------------------------------------------------------------------

class _Usage:
    prompt_token_count = 1200
    candidates_token_count = 300
    thoughts_token_count = 50
    cached_content_token_count = None


def _call_from_a_service_module(fn):
    """Run `fn` from a frame whose file looks like services/fake_feature.py."""
    scope = {}
    code = compile("def build_prompt(fn):\n    return fn()\n", "/app/services/fake_feature.py", "exec")
    exec(code, scope)
    return scope["build_prompt"](fn)


def test_record_names_the_job_and_the_calling_feature(monkeypatch):
    monkeypatch.setenv("LLM_USAGE_LEDGER", "off")

    @llm_usage.tag_job
    def process(job_id, job_type, payload):
        return _call_from_a_service_module(
            lambda: llm_usage.record(model="gemini-3.8-flash", usage=_Usage(), seconds=1.5))

    row = process("j1", "tailor", {})
    assert row["job"] == "tailor"
    assert row["caller"] == "fake_feature.build_prompt"
    assert (row["prompt_tokens"], row["output_tokens"], row["thinking_tokens"], row["cached_tokens"]) == (1200, 300, 50, 0)
    assert row["ms"] == 1500
    assert llm_usage.current_job() == "-"  # reset after the job


def test_ledger_rows_aggregate_and_report(mock_db):
    mock_db[llm_usage.COLLECTION].delete_many({})
    base = {"day": "2026-11-10", "job": "tailor", "caller": "resume_tailor.tailor",
            "model": "gemini-3.8-flash", "ok": True, "cache_hit": False,
            "prompt_tokens": 1000, "output_tokens": 100, "thinking_tokens": 0, "cached_tokens": 0}
    llm_usage.write_row(mock_db, dict(base, ms=2000))
    llm_usage.write_row(mock_db, dict(base, ms=6000, ok=False))
    doc = mock_db[llm_usage.COLLECTION].find_one({})
    assert (doc["calls"], doc["failed"], doc["prompt_tokens"], doc["ms_total"], doc["ms_max"]) == (2, 1, 2000, 8000, 6000)

    assert doc["day"] == "2026-11-10" and doc["expire_at"]
    rows = llm_usage.usage_report(mock_db, days=3650)
    assert rows[0]["key"] == "tailor / resume_tailor.tailor"
    assert rows[0]["ms_avg"] == 4000
    assert rows[0]["usd_estimate"] == pytest.approx(2000 / 1e6 * 1.65 + 200 / 1e6 * 8.25, abs=1e-4)
    mock_db[llm_usage.COLLECTION].delete_many({})


def test_record_never_raises(monkeypatch):
    monkeypatch.setenv("LLM_USAGE_LEDGER", "on")
    monkeypatch.setattr(llm_usage, "_db", lambda: (_ for _ in ()).throw(RuntimeError("atlas down")))
    assert llm_usage.record(model="m", usage=object())["prompt_tokens"] == 0
    llm_usage.flush(2)


def test_gemini_reports_every_call_and_every_failure(monkeypatch):
    from services.llm_providers.gemini import GeminiProvider
    seen = []
    monkeypatch.setattr(llm_usage, "record", lambda **kw: seen.append(kw))

    class _Models:
        fail = False

        def generate_content(self, model, contents, config):
            if self.fail:
                raise ValueError("bad request")
            return type("R", (), {"usage_metadata": _Usage(), "text": "{}"})()

    provider = GeminiProvider()
    models = _Models()
    provider._client = type("C", (), {"models": models})()
    provider._generate_one(model="gemini-3.8-flash", contents=[], config=None, max_retries=0)
    assert seen[-1]["model"] == "gemini-3.8-flash" and isinstance(seen[-1]["usage"], _Usage)

    models.fail = True
    with pytest.raises(Exception):
        provider._generate_one(model="gemini-3.8-flash", contents=[], config=None, max_retries=0)
    assert seen[-1]["ok"] is False


# ---------------------------------------------------------------------------
# 4. Response cache
# ---------------------------------------------------------------------------

@pytest.fixture
def cache_on(monkeypatch, mock_db):
    monkeypatch.setenv("LLM_RESPONSE_CACHE", "on")
    monkeypatch.setenv("LLM_USAGE_LEDGER", "off")
    monkeypatch.setattr(llm_response_cache, "_db", lambda: mock_db)
    mock_db[llm_response_cache.COLLECTION].delete_many({})
    yield mock_db
    mock_db[llm_response_cache.COLLECTION].delete_many({})


def _counting_provider(monkeypatch, answer):
    import services.claude_client as cc
    calls = []

    def fake_json(**kw):
        calls.append(kw)
        return json.loads(json.dumps(answer))

    monkeypatch.setattr(cc._provider, "json", fake_json)
    return cc, calls


def test_cache_serves_an_identical_request_once(cache_on, monkeypatch):
    cc, calls = _counting_provider(monkeypatch, {"job_title": "Backend Engineer"})
    first = cc.claude_json("JD text", system="sys", model="m", cache="jd_analysis")
    second = cc.claude_json("JD text", system="sys", model="m", cache="jd_analysis")
    assert first == second == {"job_title": "Backend Engineer"}
    assert len(calls) == 1


@pytest.mark.parametrize("change", [
    {"prompt": "another JD"}, {"system": "edited instructions"}, {"model": "gemini-4-argon"},
    {"max_tokens": 999}, {"schema": {"job_title": int}},
])
def test_any_change_to_the_request_is_a_miss(cache_on, monkeypatch, change):
    cc, calls = _counting_provider(monkeypatch, {"job_title": "x"})
    request = {"prompt": "JD text", "system": "sys", "model": "m", "schema": {"job_title": str}}
    cc.claude_json(**request, cache="jd_analysis")
    cc.claude_json(**dict(request, **change), cache="jd_analysis")
    assert len(calls) == 2


def test_uncached_calls_and_file_uploads_bypass_the_cache(cache_on, monkeypatch):
    cc, calls = _counting_provider(monkeypatch, {"a": 1})
    cc.claude_json("p", model="m")
    cc.claude_json("p", model="m")
    cc.claude_json("p", model="m", parts=[b"pdf"], cache="jd_analysis")
    cc.claude_json("p", model="m", parts=[b"pdf"], cache="jd_analysis")
    assert len(calls) == 4
    assert cache_on[llm_response_cache.COLLECTION].count_documents({}) == 0


def test_a_failed_call_is_not_cached(cache_on, monkeypatch):
    import services.claude_client as cc

    def boom(**kw):
        raise RuntimeError("quota")

    monkeypatch.setattr(cc._provider, "json", boom)
    with pytest.raises(RuntimeError):
        cc.claude_json("p", model="m", cache="jd_analysis")
    assert cache_on[llm_response_cache.COLLECTION].count_documents({}) == 0


def test_an_unreachable_database_is_a_miss(monkeypatch):
    monkeypatch.setenv("LLM_RESPONSE_CACHE", "on")
    monkeypatch.setattr(llm_response_cache, "_db", lambda: (_ for _ in ()).throw(RuntimeError("down")))
    cc, calls = _counting_provider(monkeypatch, {"a": 1})
    assert cc.claude_json("p", model="m", cache="jd_analysis") == {"a": 1}
    assert len(calls) == 1


def test_cache_is_off_in_the_test_runner_by_default(monkeypatch):
    monkeypatch.delenv("LLM_RESPONSE_CACHE", raising=False)
    assert llm_response_cache.enabled() is False


def test_jd_analysis_hits_the_cache_and_still_post_processes(cache_on, monkeypatch):
    monkeypatch.delenv("EVIDENCE_TAILORING_ENABLED", raising=False)
    from services.resume_service import ResumeService
    _, calls = _counting_provider(monkeypatch, {
        "job_title": "Backend Engineer", "company": "Acme", "required_skills": ["python", "postgres"],
        "preferred_skills": [], "responsibilities": [], "qualifications": [],
        "experience_years": "2+ years", "industry": "Logistics", "keywords": ["python"],
    })
    jd = "Backend Engineer at Acme. 2+ years of Python and PostgreSQL."
    first = ResumeService._analyze_jd_with_llm(jd)
    second = ResumeService._analyze_jd_with_llm(jd)
    assert len(calls) == 1
    assert first == second
    assert calls[0]["prompt"] and "Acme" in calls[0]["prompt"]
