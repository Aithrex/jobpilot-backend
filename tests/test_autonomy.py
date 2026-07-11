"""Offline tests for confidence-tiered autonomy (Tranche 1 §3).

Runs fully offline (mongomock; no network). Two pure decisions:

  * ``score`` — the weighted blend (coverage 0.50 / ats-ease 0.30 / no-novel
    0.20); we pin exact arithmetic so a weight change fails a test.
  * ``decide`` — the HARD RULES that override the score, in order:
      1. an unconfirmed LEGAL attestation → "ask" (NEVER auto), even far above
         threshold — the never-fabricate rule extended to form attestations;
      2. blocked-on-oracle → "hold";
     then the score band decides against the per-user threshold.
"""
import pytest

from _vg_support import current_db

import services.autonomy_service as auto

USER = "alice@aspirely.me"


@pytest.fixture()
def settings_db():
    d = current_db()
    d.auto_submit_settings.delete_many({})
    return d


# ---------------------------------------------------------------------------
# score() weighting
# ---------------------------------------------------------------------------
class TestScore:
    def test_perfect_greenhouse_no_essay(self):
        # 0.50*1.0 + 0.30*0.95 + 0.20*1.0 = 0.985
        s = auto.score(None, answer_coverage=1.0, ats_family="greenhouse",
                       has_novel_free_text=False)
        assert s == pytest.approx(0.985)

    def test_novel_free_text_costs_the_no_novel_weight(self):
        s = auto.score(None, answer_coverage=1.0, ats_family="greenhouse",
                       has_novel_free_text=True)
        # loses the full 0.20 no-novel term.
        assert s == pytest.approx(0.785)

    def test_unknown_ats_uses_cautious_default(self):
        # 0.50*1.0 + 0.30*0.60 + 0.20*1.0 = 0.88
        s = auto.score(None, answer_coverage=1.0, ats_family="obscure-ats",
                       has_novel_free_text=False)
        assert s == pytest.approx(0.88)

    def test_coverage_dominates(self):
        # Zero coverage caps the score well below any sane threshold even on the
        # easiest ATS with no essay: 0 + 0.285 + 0.20 = 0.485.
        s = auto.score(None, answer_coverage=0.0, ats_family="greenhouse",
                       has_novel_free_text=False)
        assert s == pytest.approx(0.485)

    def test_ats_family_falls_back_to_application_row(self):
        s = auto.score({"ats_family": "greenhouse"}, answer_coverage=1.0,
                       has_novel_free_text=False)
        assert s == pytest.approx(0.985)

    def test_coverage_clamped(self):
        assert auto.score(None, answer_coverage=9.0, ats_family="greenhouse") <= 1.0
        assert auto.score(None, answer_coverage=-9.0, ats_family="greenhouse") >= 0.0


# ---------------------------------------------------------------------------
# decide() HARD RULES
# ---------------------------------------------------------------------------
class TestDecideHardRules:
    def test_unconfirmed_legal_forces_ask_even_above_threshold(self):
        d = auto.decide(USER, score=1.0,
                        has_unconfirmed_legal_attestation=True,
                        blocked_on_oracle=False, threshold=0.5)
        assert d == auto.ASK

    def test_legal_beats_oracle(self):
        # Both hard conditions true — legal is checked first → ask, not hold.
        d = auto.decide(USER, score=1.0,
                        has_unconfirmed_legal_attestation=True,
                        blocked_on_oracle=True, threshold=0.5)
        assert d == auto.ASK

    def test_oracle_block_forces_hold_even_above_threshold(self):
        d = auto.decide(USER, score=1.0,
                        has_unconfirmed_legal_attestation=False,
                        blocked_on_oracle=True, threshold=0.5)
        assert d == auto.HOLD


class TestDecideScoreBand:
    def test_at_or_above_threshold_is_auto(self):
        assert auto.decide(USER, 0.90, False, False, threshold=0.90) == auto.AUTO
        assert auto.decide(USER, 0.95, False, False, threshold=0.90) == auto.AUTO

    def test_near_miss_within_band_holds_when_resolvable_unknown(self):
        # 0.80 is within HOLD_BAND (0.15) below the 0.90 threshold → hold.
        assert auto.decide(USER, 0.80, False, False, threshold=0.90) == auto.HOLD

    def test_large_gap_asks_when_resolvable_unknown(self):
        # 0.50 is far below 0.90 → ask.
        assert auto.decide(USER, 0.50, False, False, threshold=0.90) == auto.ASK

    def test_explicit_resolvable_flag_overrides_band(self):
        assert auto.decide(USER, 0.50, False, False, threshold=0.90,
                           resolvable_by_one_tap=True) == auto.HOLD
        assert auto.decide(USER, 0.89, False, False, threshold=0.90,
                           resolvable_by_one_tap=False) == auto.ASK


# ---------------------------------------------------------------------------
# threshold persistence + read
# ---------------------------------------------------------------------------
class TestThreshold:
    def test_default_when_unset(self, settings_db):
        assert auto.get_autonomy_threshold(USER) == auto.DEFAULT_AUTONOMY_THRESHOLD

    def test_set_then_read_roundtrip(self, settings_db):
        auto.set_autonomy_threshold(USER, 0.75)
        assert auto.get_autonomy_threshold(USER) == 0.75

    def test_stored_threshold_is_clamped(self, settings_db):
        assert auto.set_autonomy_threshold(USER, 5.0) == 1.0
        assert auto.get_autonomy_threshold(USER) == 1.0

    def test_decide_reads_user_threshold_when_not_passed(self, settings_db):
        auto.set_autonomy_threshold(USER, 0.50)
        # score 0.60 >= stored 0.50 → auto (proves the DB read is used).
        assert auto.decide(USER, 0.60, False, False) == auto.AUTO

    def test_threshold_of_one_disables_silent_auto(self, settings_db):
        auto.set_autonomy_threshold(USER, 1.0)
        # Nothing scores above 1.0, so even a near-perfect score cannot auto.
        assert auto.decide(USER, 0.99, False, False) != auto.AUTO


# ---------------------------------------------------------------------------
# evaluate() — score + decide in one shot
# ---------------------------------------------------------------------------
class TestEvaluate:
    def test_evaluate_bundles_score_decision_threshold(self, settings_db):
        out = auto.evaluate(USER, {"ats_family": "greenhouse"},
                            answer_coverage=1.0, has_novel_free_text=False)
        assert out["confidence_score"] == pytest.approx(0.985)
        assert out["autonomy"] == auto.AUTO
        assert out["threshold"] == auto.DEFAULT_AUTONOMY_THRESHOLD

    def test_evaluate_legal_forces_ask_regardless_of_high_score(self, settings_db):
        out = auto.evaluate(USER, None, answer_coverage=1.0, ats_family="greenhouse",
                            has_unconfirmed_legal_attestation=True)
        assert out["confidence_score"] == pytest.approx(0.985)
        assert out["autonomy"] == auto.ASK
