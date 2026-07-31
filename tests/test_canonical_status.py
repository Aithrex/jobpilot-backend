"""Unit tests for the E0 read-side normalizer `canonical_status` (defect E).

E0 is PURELY ADDITIVE: `canonical_status` maps the legacy ledger vocabulary
onto the canonical target-machine vocabulary and is idempotent on canonical
inputs. It is NOT yet called by any writer/reader (that is E1/E3) — these tests
pin its contract so the later cutover has a stable base.

Contract:
  * TOTAL over {all legacy states} ∪ {all canonical states}: every result is a
    valid canonical state (a member of `CANONICAL_STATUSES`).
  * idempotent: canonical_status(canonical_status(x)) == canonical_status(x).
  * the 4 legacy→canonical remaps are exact.
  * unknown/garbage passes through unchanged and never raises.

Runs fully offline — `canonical_status` is a pure function (no Mongo/LLM).
"""
import services.application_ledger_service as als

# The legacy ledger vocabulary (pre-E0), exactly the ledger's ALL states minus
# the E0 canonical additions.
_LEGACY_STATES = (
    als.STATUS_QUEUED, als.STATUS_GATHERING, als.STATUS_NEEDS_INPUT,
    als.STATUS_READY, als.STATUS_SUBMITTING, als.STATUS_SUBMITTED,
    als.STATUS_CONFIRMED, als.STATUS_FAILED, als.STATUS_SKIPPED_DUPLICATE,
    als.STATUS_SKIPPED,
)

# The canonical target-machine vocabulary (defect E target). Legacy-only aliases
# (gathering/ready/submitting/failed) are NOT here — they normalize away.
_CANONICAL_STATES = als.CANONICAL_STATUSES

# The 4 legacy→canonical remaps that E0 introduces.
_REMAPS = {
    als.STATUS_GATHERING: als.STATUS_PREPARING,
    als.STATUS_READY: als.STATUS_READY_FOR_REVIEW,
    als.STATUS_SUBMITTING: als.STATUS_RUNNING,
    als.STATUS_FAILED: als.STATUS_FAILED_PRE_SUBMIT,
}


class TestCanonicalStatusTotality:
    def test_total_over_all_legacy_and_canonical_states(self):
        """Every legacy or canonical input maps to a valid canonical state."""
        for state in set(_LEGACY_STATES) | set(_CANONICAL_STATES):
            result = als.canonical_status(state)
            assert result in _CANONICAL_STATES, (
                f"canonical_status({state!r}) = {result!r} is not canonical")

    def test_idempotent(self):
        """Applying the normalizer twice equals applying it once."""
        for state in set(_LEGACY_STATES) | set(_CANONICAL_STATES):
            once = als.canonical_status(state)
            assert als.canonical_status(once) == once, (
                f"not idempotent at {state!r}: {once!r} -> "
                f"{als.canonical_status(once)!r}")

    def test_canonical_inputs_are_fixed_points(self):
        """Canonical states map to themselves (no double-remap)."""
        for state in _CANONICAL_STATES:
            assert als.canonical_status(state) == state


class TestCanonicalStatusRemaps:
    def test_the_four_remaps_are_exact(self):
        assert als.canonical_status(als.STATUS_GATHERING) == als.STATUS_PREPARING
        assert als.canonical_status(als.STATUS_READY) == als.STATUS_READY_FOR_REVIEW
        assert als.canonical_status(als.STATUS_SUBMITTING) == als.STATUS_RUNNING
        assert als.canonical_status(als.STATUS_FAILED) == als.STATUS_FAILED_PRE_SUBMIT

    def test_remap_table_matches_documented_intent(self):
        for legacy, canonical in _REMAPS.items():
            assert als.canonical_status(legacy) == canonical

    def test_pass_through_legacy_states_are_unchanged(self):
        # These legacy names ARE canonical vocabulary (map to themselves).
        for state in (als.STATUS_QUEUED, als.STATUS_NEEDS_INPUT,
                      als.STATUS_SUBMITTED, als.STATUS_CONFIRMED,
                      als.STATUS_SKIPPED, als.STATUS_SKIPPED_DUPLICATE,
                      als.STATUS_DRAFT):
            assert als.canonical_status(state) == state


class TestCanonicalStatusUnknown:
    def test_unknown_passes_through_unchanged(self):
        for junk in ("teleported", "", "QUEUED", "ready_for_review ", "xyz"):
            assert als.canonical_status(junk) == junk

    def test_non_string_never_raises_and_returns_input(self):
        for junk in (None, 123, ["ready"], {"status": "ready"}):
            assert als.canonical_status(junk) == junk
