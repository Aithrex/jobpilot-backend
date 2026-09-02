"""The human queues are ordered by CONSEQUENCE, not by arrival.

1,443 open merge reviews sorted by `created_at` is a queue nobody finishes,
and the rows that matter — an employer holding 839 core-tech postings, an
employer somebody put on the strategic list — sit wherever they happened to
land. `employer_registry.score_review` is the ordering, and it is pure so it
can be argued with.
"""
from __future__ import annotations

import pytest

from services import employer_registry as er


def _score(**kw) -> float:
    return er.score_review(**kw)[0]


class TestWhatMovesAReviewUp:

    def test_a_tier_a_employer_outranks_a_busy_one(self):
        """Strategic beats volume. That is a product decision and it is
        written down as a weight rather than argued about per row."""
        tier_a = _score(tier_a=True)
        busy = _score(core_tech_postings=200, active_postings=800)
        assert tier_a > busy

    def test_core_tech_postings_are_worth_three_active_ones(self):
        assert _score(core_tech_postings=100) > _score(active_postings=100)
        assert _score(core_tech_postings=100) == pytest.approx(
            _score(active_postings=300))

    def test_evidence_already_gathered_breaks_a_tie(self):
        """A pair one signal from a decision is worth a human minute in a way
        a pair with nothing behind it is not."""
        near = _score(active_postings=10, evidence_classes=1)
        far = _score(active_postings=10, evidence_classes=0)
        assert near > far

    def test_a_review_with_no_consequence_scores_zero_and_says_so(self):
        score, reasons = er.score_review()
        assert score == 0.0
        assert reasons == ["no postings, no evidence, not strategic"]


class TestWhatMovesAReviewDown:

    def test_two_domain_keyed_sides_sink(self):
        """Two VERIFIED domains is already a `distinct` verdict from
        `merge_decision`. Such a review is a confirmation, not a decision."""
        with_penalty = _score(active_postings=500, both_domain_keyed=True)
        without = _score(active_postings=500)
        assert with_penalty < without

    def test_the_penalty_cannot_make_a_tier_a_review_disappear(self):
        assert _score(tier_a=True, both_domain_keyed=True) > 0

    def test_the_penalty_demotes_but_never_inverts_the_order(self):
        """A review with 400 postings behind it that is probably `distinct`
        still deserves a human before one with no postings, no evidence and no
        strategic claim. Without a floor the -800 pushed it below zero and
        therefore below exactly that."""
        penalised = _score(active_postings=400, both_domain_keyed=True)
        nothing = _score()
        assert penalised > nothing


class TestTheReasonsAreReadable:

    def test_every_contributing_factor_is_named(self):
        _score_value, reasons = er.score_review(
            active_postings=800, core_tech_postings=200, tier_a=True,
            evidence_classes=2)
        blob = " ".join(reasons)
        assert "Tier-A" in blob
        assert "200 core-tech postings" in blob
        assert "800 active postings" in blob
        assert "2 independent evidence class" in blob

    def test_the_strongest_reason_comes_first(self):
        _s, reasons = er.score_review(active_postings=5, tier_a=True)
        assert reasons[0].startswith("strategic")


class TestOrderingEndToEnd:

    def test_a_realistic_queue_sorts_the_way_a_human_would(self):
        rows = [
            ("noise", _score()),
            ("small employer, one signal", _score(active_postings=3,
                                                  evidence_classes=1)),
            ("busy employer", _score(active_postings=839,
                                     core_tech_postings=219)),
            ("strategic employer", _score(tier_a=True, active_postings=0)),
            ("duplicate identity, both domain-keyed",
             _score(active_postings=400, both_domain_keyed=True)),
        ]
        ordered = [name for name, _s in sorted(rows, key=lambda r: -r[1])]
        assert ordered[0] == "strategic employer"
        assert ordered[1] == "busy employer"
        assert ordered[-1] == "noise"
