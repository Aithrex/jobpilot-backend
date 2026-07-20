"""Offline tests for the answer library resolve()/remember() (Tranche 1 §3 support).

Runs fully offline (mongomock; no network, no LLM). Focus on the two properties
autonomy_service consumes and the never-fabricate legal rule:

  * LEGAL attestations are filled ONLY from declared data — an undeclared legal
    question comes back UNRESOLVED (kind legal_attestation) and sets
    ``has_unconfirmed_legal_attestation`` (→ autonomy "ask"); it is never guessed.
  * ESSAYS are flagged ``needs_generation`` (grounded generator), never answered
    here, and set ``has_novel_free_text``.
  * coverage math reflects answered / total over the resolvable fields.
  * remember() persists a user's own answer so the same question resolves from
    the user store next time (source "user_store").
"""
import pytest

from _vg_support import current_db

import services.answer_library_service as lib

USER = "alice@aspirely.me"

_WORK_AUTH_Q = "Are you legally authorized to work in the United States?"
_SPONSOR_Q = "Will you now or in the future require visa sponsorship?"
_EMAIL_Q = "What is your email address?"
_ESSAY_Q = "Why do you want to work here?"


@pytest.fixture()
def lib_db():
    d = current_db()
    for c in ("application_answer_library", "user_resumes", "resume_assets",
              "users", "user_preferences", "visa_profiles",
              "application_answers"):
        d[c].delete_many({})
    return d


def _seed_resume(d, structured):
    """Resume Platform v3: the canonical resume store is resume_assets
    (read through resume_access.get_active_structured)."""
    d.resume_assets.insert_one({
        "resume_id": "asset-answer-lib", "owner_type": "user",
        "user_email": USER, "structured": structured,
        "parse_status": "parsed",
        "parsed_at": "2026-01-01T00:00:00+00:00",
    })


# ---------------------------------------------------------------------------
# Legal attestations — declared-only, never guessed
# ---------------------------------------------------------------------------
class TestLegalAttestations:
    def test_undeclared_legal_is_unresolved_and_flags_ask(self, lib_db):
        out = lib.resolve(USER, [_WORK_AUTH_Q])
        assert out["answers"] == {}
        assert out["has_unconfirmed_legal_attestation"] is True
        assert out["unresolved"][0]["kind"] == lib.KIND_LEGAL
        assert out["unresolved"][0]["category"] == lib.CAT_WORK_AUTH

    def test_declared_legal_is_filled_from_declared_data(self, lib_db):
        lib_db.user_preferences.insert_one(
            {"user_email": USER, "work_authorized_us": True})
        out = lib.resolve(USER, [_WORK_AUTH_Q])
        assert out["has_unconfirmed_legal_attestation"] is False
        ans = out["answers"][_WORK_AUTH_Q]
        assert ans["value"] is True
        assert ans["source"] == "declared"
        assert ans["category"] == lib.CAT_WORK_AUTH

    def test_sponsorship_classified_as_legal(self, lib_db):
        out = lib.resolve(USER, [_SPONSOR_Q])
        assert out["has_unconfirmed_legal_attestation"] is True
        assert out["unresolved"][0]["category"] == lib.CAT_SPONSORSHIP


# ---------------------------------------------------------------------------
# Essays — flagged for grounded generation, never answered here
# ---------------------------------------------------------------------------
class TestEssays:
    def test_essay_is_flagged_needs_generation_not_answered(self, lib_db):
        out = lib.resolve(USER, [_ESSAY_Q])
        assert out["answers"] == {}
        assert len(out["needs_generation"]) == 1
        assert out["needs_generation"][0]["category"] == lib.CAT_ESSAY
        assert out["has_novel_free_text"] is True


# ---------------------------------------------------------------------------
# Profile fill + coverage math
# ---------------------------------------------------------------------------
class TestCoverage:
    def test_profile_field_filled_and_coverage_full(self, lib_db):
        _seed_resume(lib_db, {"contact": {"email": "alice@aspirely.me"}})
        out = lib.resolve(USER, [_EMAIL_Q])
        assert out["coverage"] == 1.0
        assert out["answers"][_EMAIL_Q]["source"] == "profile"
        assert out["answers"][_EMAIL_Q]["value"] == "alice@aspirely.me"

    def test_mixed_resolved_and_unresolved_coverage(self, lib_db):
        _seed_resume(lib_db, {"contact": {"email": "alice@aspirely.me"}})
        # one resolvable (email) + one undeclared legal → coverage = 1/2.
        out = lib.resolve(USER, [_EMAIL_Q, _WORK_AUTH_Q])
        assert out["coverage"] == pytest.approx(0.5)
        assert out["has_unconfirmed_legal_attestation"] is True

    def test_unrecognized_question_is_novel(self, lib_db):
        out = lib.resolve(USER, ["Zorp glimber wux plonk?"])
        assert out["unresolved"][0]["kind"] == lib.KIND_NOVEL
        assert out["has_novel_free_text"] is True

    def test_empty_or_bad_input_is_full_coverage_noop(self, lib_db):
        assert lib.resolve(USER, [])["coverage"] == 1.0
        assert lib.resolve(USER, "not a list")["coverage"] == 1.0


# ---------------------------------------------------------------------------
# remember() — the self-growing store
# ---------------------------------------------------------------------------
class TestRemember:
    def test_remembered_answer_resolves_from_user_store(self, lib_db):
        q = "What is your notice period?"
        stored = lib.remember(USER, q, "2 weeks")
        assert stored is not None
        out = lib.resolve(USER, [q])
        ans = out["answers"][q]
        assert ans["value"] == "2 weeks"
        assert ans["source"] == "user_store"

    def test_essays_are_not_banked(self, lib_db):
        assert lib.remember(USER, _ESSAY_Q, "Because I love it") is None

    def test_remember_is_user_scoped(self, lib_db):
        q = "What is your notice period?"
        lib.remember(USER, q, "2 weeks")
        # A different user must not inherit the answer.
        out = lib.resolve("bob@aspirely.me", [q])
        assert q not in out["answers"]
