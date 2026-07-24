"""Unit tests for IntegrityGuard.fix_terminal_periods (Craft-overhaul Part C1).

The bullet trailing-semicolon defect: a bullet ending in a connector (';', ',',
':', '-') — or a connector sitting just before a wrapping closer like ')' or '"'
— must be normalized to end in a period. The original implementation only
inspected the single last character, so ``"...service;)"`` (last char ')') and
semicolon-splices slipped through. These exercise the pure static guard directly
— no LLM, no DB.
"""
import pytest

from services.integrity_guard import IntegrityGuard


@pytest.fixture
def guard():
    return IntegrityGuard()


def _bullet(text):
    return {
        "experience": [
            {"company": "Acme", "title": "SWE", "bullets": [text]},
        ]
    }


def _first_bullet(result):
    return result["experience"][0]["bullets"][0]


# --- Terminal connectors (the original single-char path, still must work) ---

@pytest.mark.parametrize("bad", [";", ",", ":", "-", "—", "–"])
def test_bare_terminal_connector_becomes_period(guard, bad):
    fixed, changes = guard.fix_terminal_periods(_bullet(f"Built the payments pipeline{bad}"))
    assert _first_bullet(fixed) == "Built the payments pipeline."
    assert changes  # a fix was recorded


def test_missing_terminator_gets_period_appended(guard):
    fixed, changes = guard.fix_terminal_periods(_bullet("Built the payments pipeline"))
    assert _first_bullet(fixed) == "Built the payments pipeline."
    assert changes


def test_already_terminated_is_untouched(guard):
    fixed, changes = guard.fix_terminal_periods(_bullet("Built the payments pipeline."))
    assert _first_bullet(fixed) == "Built the payments pipeline."
    assert not changes


# --- The C1 regression: connector hidden behind a trailing closer ---

@pytest.mark.parametrize(
    "text,expected",
    [
        # semicolon just before a closing paren
        ("Migrated the service;)", "Migrated the service.)"),
        # semicolon just before a closing double-quote
        ('Shipped the "fast path";"', 'Shipped the "fast path"."'),
        # comma before a closing bracket
        ("Owned the queue [P0],]", "Owned the queue [P0].]"),
        # connector before a closing single-quote
        ("Reduced latency to 'p99';'", "Reduced latency to 'p99'.'"),
    ],
)
def test_connector_before_closer_is_fixed(guard, text, expected):
    fixed, changes = guard.fix_terminal_periods(_bullet(text))
    assert _first_bullet(fixed) == expected
    assert changes


def test_legit_closer_without_connector_is_untouched(guard):
    # A bullet legitimately ending in a quote/paren with no dangling connector
    # must NOT be altered.
    text = 'Built the "fast path" cache.'
    fixed, changes = guard.fix_terminal_periods(_bullet(text))
    assert _first_bullet(fixed) == text
    assert not changes


# --- Semicolon-splice with a dropped second clause ---

def test_dangling_semicolon_splice_collapses(guard):
    # Model emitted a two-clause splice whose 2nd clause never materialized.
    fixed, changes = guard.fix_terminal_periods(_bullet("Built the pipeline in Python;  "))
    assert _first_bullet(fixed) == "Built the pipeline in Python."
    assert changes


def test_run_of_trailing_connectors_and_spaces_collapses(guard):
    fixed, changes = guard.fix_terminal_periods(_bullet("Owned deployments; ,  "))
    assert _first_bullet(fixed) == "Owned deployments."
    assert changes


# --- Applies to summary and project bullets too ---

def test_summary_is_fixed(guard):
    fixed, _ = guard.fix_terminal_periods(
        {"summary": "Backend engineer focused on payments;"}
    )
    assert fixed["summary"] == "Backend engineer focused on payments."


def test_project_bullets_are_fixed(guard):
    fixed, _ = guard.fix_terminal_periods(
        {"projects": [{"name": "Toolkit", "bullets": ["Parsed telemetry at scale;)"]}]}
    )
    assert fixed["projects"][0]["bullets"][0] == "Parsed telemetry at scale.)"


# --- Degenerate inputs don't crash ---

@pytest.mark.parametrize("weird", ["", "   ", ";", ")", ");"])
def test_degenerate_inputs_do_not_crash(guard, weird):
    fixed, _ = guard.fix_terminal_periods(_bullet(weird))
    # Just assert it returns a string and doesn't raise.
    assert isinstance(_first_bullet(fixed), str)


def test_non_string_bullet_is_passed_through(guard):
    fixed, _ = guard.fix_terminal_periods(_bullet(None))
    assert _first_bullet(fixed) is None
