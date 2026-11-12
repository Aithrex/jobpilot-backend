"""The punctuation-only finalizer for AI-written text (services/copy_finalizer.py).

Two promises are tested here:
  1. dash punctuation is gone from prose a person reads or sends;
  2. nothing else changes. Words, numbers and names survive exactly, because
     IntegrityGuard and the faithfulness checks compare facts token by token,
     and a finalizer that rewrote a word would be a fabrication path.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from services.copy_finalizer import (
    NO_DASH_RULE,
    finalize_bullet,
    finalize_date_range,
    finalize_fields,
    finalize_text,
    has_dash_punctuation,
)

EM = "—"
EN = "–"
ROOT = Path(__file__).resolve().parent.parent


def _facts(text: str):
    """Case-folded words and numbers, minus the "to" a range rewrite inserts."""
    # Words and numbers, with internal "." or "," kept ("2.3M", "120,000") and
    # sentence punctuation dropped, since punctuation is what is allowed to move.
    return [t for t in re.findall(r"[\w$%+#@]+(?:[.,/'][\w$%+#@]+)*", text.lower()) if t != "to"]


# --------------------------------------------------------------------------
# What it rewrites.
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "before,after",
    [
        (f"Built a data pipeline {EM} cutting latency by 40%.", "Built a data pipeline, cutting latency by 40%."),
        (f"Built a data pipeline{EM}cutting latency by 40%.", "Built a data pipeline, cutting latency by 40%."),
        (f"Led the migration {EM} it saved $2M annually.", "Led the migration. It saved $2M annually."),
        (f"The platform {EM} built in Go {EM} handles 1M requests per second.",
         "The platform, built in Go, handles 1M requests per second."),
        (f"2{EN}4 years of Python.", "2 to 4 years of Python."),
        (f"Salary $120k {EN} $150k per year.", "Salary $120k to $150k per year."),
        (f"Worked 2019{EN}2021 at Stripe.", "Worked 2019 to 2021 at Stripe."),
        (f"Jan 2020 {EN} Present", "Jan 2020 to Present"),
        ("Managed a team of 5 - 8 engineers.", "Managed a team of 5 to 8 engineers."),
        (f"Cut costs by $2M {EM} 30% below budget.", "Cut costs by $2M, 30% below budget."),
        ("Revenue grew 10% -- driven by onboarding fixes.", "Revenue grew 10%, driven by onboarding fixes."),
        ("Built client–server sync for New York–based teams.",
         "Built client-server sync for New York-based teams."),
        (f"Thanks for reading {EM}", "Thanks for reading"),
        (f"Hi Priya {EM} I saw the opening on your team.", "Hi Priya, I saw the opening on your team."),
        (f"- First item\n- Second item {EM} with detail\n{EM} Third",
         "• First item\n• Second item, with detail\n• Third"),
    ],
)
def test_rewrites_dash_punctuation(before, after):
    out = finalize_text(before)
    assert out == after
    assert not has_dash_punctuation(out)


def test_label_takes_a_colon():
    assert finalize_text(f"Toolkit {EM} a personal library", label=True) == "Toolkit: a personal library"


def test_single_bullet_drops_its_list_marker():
    assert finalize_bullet(f"- Built X {EM} cutting Y by 20%.") == "Built X, cutting Y by 20%."
    assert finalize_bullet("-5% churn after the redesign.") == "-5% churn after the redesign."


def test_resume_dates_keep_the_ascii_hyphen():
    assert finalize_date_range(f"Jan 2020 {EN} Present") == "Jan 2020 - Present"
    assert finalize_date_range(f"2019{EN}2021") == "2019-2021"


# --------------------------------------------------------------------------
# What it never touches.
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text",
    [
        "Full-time, H-1B, one-click, COVID-19, 555-123-4567, -5% drift, e-mail.",
        "Use the --force flag carefully.",
        "Reduced p99 from 120ms to 45ms across 3 regions.",
        "",
        "No dashes here at all.",
    ],
)
def test_leaves_clean_text_alone(text):
    assert finalize_text(text) == text


def test_urls_emails_and_code_are_protected():
    text = (f"See https://github.com/a--b/c-d {EM} or write to jane-doe@ex-ample.com. "
            "Run `a - b` then:\n```\nx = y - z  # keep\n```")
    out = finalize_text(text)
    assert "https://github.com/a--b/c-d" in out
    assert "jane-doe@ex-ample.com" in out
    assert "`a - b`" in out
    assert "x = y - z  # keep" in out
    assert EM not in out


# Realistic model output: the facts must survive the rewrite token for token.
_LLM_SAMPLES = [
    f"Designed a Kafka-based ingestion layer {EM} 120,000 events/sec at 99.95% uptime {EM} for Acme Corp.",
    f"Owned the billing migration (Stripe {EN} Adyen) for 2.3M users; reduced chargebacks 18% {EM} saving $410K/yr.",
    f"I'm excited about the Staff Engineer role at Datadog {EM} it maps directly to my 6 years on observability.",
    f"Shipped v2 of the API in Q3 2024 {EM} 3{EN}5 weeks ahead of plan {EM} with Maria Lopez and the SRE team.",
    "Mentored 4 interns -- two received return offers -- and led weekly design reviews.",
    f"Earned AWS Solutions Architect (2023) {EM} plus CKA {EN} and taught a 12-week course.",
    f"Hi Priya {EM} I saw the opening on the Payments team (Req #48213) and would love 15 minutes.",
]


@pytest.mark.parametrize("text", _LLM_SAMPLES)
def test_words_numbers_and_names_survive(text):
    out = finalize_text(text)
    assert not has_dash_punctuation(out), out
    assert _facts(out) == _facts(text), out
    # Every number in the input is still there, in order.
    assert re.findall(r"\d+(?:[.,]\d+)*", out) == re.findall(r"\d+(?:[.,]\d+)*", text)


def test_finalize_fields_walks_prose_and_spares_identifiers():
    payload = {
        "id": "abc--def",
        "status": "needs - input",
        "dates": f"Jan 2020 {EN} Present",
        "summary": f"Backend engineer {EM} 6 years in payments.",
        "bullets": [f"- Built X {EM} cut Y by 20%."],
        "variants": [{"text": f"One {EM} two", "angle": f"Story {EM} lead"}],
        "score": 87,
    }
    out = finalize_fields(payload)
    assert out["id"] == "abc--def"
    assert out["status"] == "needs - input"
    assert out["dates"] == "Jan 2020 - Present"
    assert out["summary"] == "Backend engineer, 6 years in payments."
    assert out["bullets"] == ["Built X, cut Y by 20%."]
    assert out["variants"][0]["text"] == "One, two"
    assert out["score"] == 87


def test_finalize_fields_with_a_key_list_only_touches_those_keys():
    payload = {"text": f"A {EM} b", "hook": f"C {EM} d", "spec": {"note": f"e {EM} f"}}
    out = finalize_fields(payload, keys=("text",))
    assert out["text"] == "A, b"
    assert out["hook"] == f"C {EM} d"


def test_integrity_guard_uses_the_finalizer_and_keeps_dates_hyphenated():
    from services.integrity_guard import IntegrityGuard

    tailored = {
        "summary": f"Data engineer {EM} 5 years building pipelines.",
        "experience": [{
            "company": "Acme", "title": "Engineer", "dates": f"Jan 2020 {EN} Present",
            "bullets": [f"Cut cost 30% {EM} saving $1.2M.", "Built a 2 - 4 node cluster."],
        }],
        "projects": [{"name": f"Toolkit {EM} a personal library", "dates": f"2021{EN}2022",
                      "bullets": [f"Wrote it in Go {EM} 4K stars."]}],
        "education": [{"dates": f"2016 {EN} 2020"}],
    }
    out, subs = IntegrityGuard().fix_unicode_dashes(tailored)
    assert out["summary"] == "Data engineer, 5 years building pipelines."
    assert out["experience"][0]["bullets"] == ["Cut cost 30%, saving $1.2M.", "Built a 2 to 4 node cluster."]
    assert out["experience"][0]["dates"] == "Jan 2020 - Present"
    assert out["projects"][0]["name"] == "Toolkit: a personal library"
    assert out["projects"][0]["dates"] == "2021-2022"
    assert out["education"][0]["dates"] == "2016 - 2020"
    assert subs


def test_prompt_rule_is_one_line_and_dash_free():
    assert "\n" not in NO_DASH_RULE
    assert not has_dash_punctuation(NO_DASH_RULE)


# --------------------------------------------------------------------------
# Coverage: every generation path a user reads or sends routes through it.
# A structural check, so a refactor that drops the call fails here, not in
# front of a user.
# --------------------------------------------------------------------------
ROUTED = {
    "tailored resume (IntegrityGuard step 7, augmenter phase 7, summary cosmetics)":
        "shared/python/services/integrity_guard.py",
    "tailored resume (final pass for every tailor path)": "shared/python/services/resume_service.py",
    "application answers (gather path and extension)": "shared/python/services/answer_generator.py",
    "outreach email draft": "services/jobs/blueprints/jobs.py",
    "interview coach chat and mock feedback": "services/resume/blueprints/resume.py",
    "career copilot chat": "services/resume/services/career_copilot_service.py",
    "LinkedIn audit": "services/resume/services/linkedin_audit_service.py",
    "mentor chat": "shared/python/services/sprint_mentor_service.py",
    "screener notes": "shared/python/services/screener_sim.py",
    "LinkedIn post drafts": "shared/python/services/post_drafting_service.py",
    "six-second recruiter read": "shared/python/services/recruiter_gaze_service.py",
    "voice-matched resume lines": "shared/python/services/voicegraph/authored.py",
}


@pytest.mark.parametrize("path_name,rel", sorted(ROUTED.items()))
def test_generation_path_calls_the_finalizer(path_name, rel):
    tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "services.copy_finalizer"
        for alias in node.names
    }
    assert imported & {"finalize_text", "finalize_bullet", "finalize_fields", "finalize_date_range"}, (
        f"{path_name} ({rel}) no longer routes its output through services.copy_finalizer"
    )
