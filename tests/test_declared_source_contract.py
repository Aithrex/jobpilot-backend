"""The provenance contract BETWEEN answer_library_service and auto_apply_service.

Why this file exists
-------------------
`answer_library_service.resolve()` stamps a `source` on every resolution, and
`auto_apply_service._resolve_answers` independently re-checks that source before
it will emit a legal attestation (`_is_declared_source`). Those are two separate
allow-lists in two separate modules, and nothing tied them together.

That gap shipped a silent no-op: the library correctly learned to auto-decline
protected-EEO fields (`source="policy_decline"`) and to escalate a résumé
company match to "Yes" (`source="resume_match"`), but `_DECLARED_SOURCES` had
never heard of either string, so every such answer was bounced straight back
into `needs_input`. Both features looked implemented and tested on their own side
while being completely inert end to end.

These tests pin the seam, not either implementation, so adding a new provenance
string on one side can never again quietly disable itself on the other.
"""
import services.answer_library_service as lib
import services.auto_apply_service as aas


def _hit(source, value="x"):
    return {"source": source, "value": value, "confidence": 1.0}


class TestProvenanceContract:
    def test_auto_apply_accepts_every_source_the_library_can_emit(self):
        """The core invariant. If this fails, some library resolution is inert."""
        rejected = sorted(s for s in lib.DECLARED_SOURCES
                          if not aas._is_declared_source(_hit(s)))
        assert not rejected, (
            f"answer_library_service can emit {rejected}, but "
            f"auto_apply_service._DECLARED_SOURCES rejects them, so answers "
            f"carrying those sources are bounced to needs_input and the feature "
            f"producing them is a no-op."
        )

    def test_declared_sources_is_a_superset(self):
        assert lib.DECLARED_SOURCES <= aas._DECLARED_SOURCES

    def test_policy_decline_is_accepted(self):
        """Protected-EEO auto-decline (spec §0.3). Declining asserts nothing
        about the user, so it is legitimately non-fabricated."""
        assert aas._is_declared_source(_hit(lib.SOURCE_POLICY_DECLINE))

    def test_resume_match_is_accepted(self):
        """Résumé escalation to "Yes" (spec §1). It can only ever RAISE an
        answer to Yes, never assert "No", so it cannot understate an
        obligation to an employer."""
        assert aas._is_declared_source(_hit(lib.SOURCE_RESUME_MATCH))


class TestFabricationStillRejected:
    """The contract must not have been widened into a rubber stamp."""

    def test_invented_sources_are_rejected(self):
        for bogus in ("invented", "llm_guess", "generated", "default",
                      "assumed", "inferred", "fallback", "", "guess"):
            assert not aas._is_declared_source(_hit(bogus)), (
                f"source {bogus!r} must NOT satisfy the declared-source guard — "
                f"that guard is what stops an invented legal attestation "
                f"reaching a real employer."
            )

    def test_generated_free_text_is_not_a_declared_source(self):
        """`generate_free_text_answers` stamps source="generated". An essay is
        fine to auto-write, but it must never be able to satisfy the guard that
        protects legal attestations."""
        assert "generated" not in aas._DECLARED_SOURCES

    def test_non_dict_and_missing_source_are_rejected(self):
        for bad in (None, "declared", 42, [], {}, {"value": "x"},
                    {"source": None}):
            assert not aas._is_declared_source(bad)
