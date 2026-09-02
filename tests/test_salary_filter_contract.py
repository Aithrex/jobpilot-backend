"""P0: `min_salary` reaches the feed, and unknown salary stays unknown.

`inventory_feed.build_feed` has read `payload["min_salary"]` since it was
written, but the three search routes build their payload from an explicit
whitelist that never included it - so the filter was dead code and a user who
set a salary floor got an unfiltered feed with no indication that the control
did nothing.
"""
from __future__ import annotations

import pytest

from services.salary_normalizer import apply_salary_floor


def _row(lo=None, hi=None):
    return {"salary_min_annual": lo, "salary_max_annual": hi}


class TestSalaryFloor:
    def test_known_band_below_floor_is_dropped(self):
        kept, dropped = apply_salary_floor([_row(80000, 95000)], 120000)
        assert kept == [] and dropped == 1

    def test_known_band_at_or_above_floor_is_kept(self):
        kept, _ = apply_salary_floor([_row(120000, 160000)], 120000)
        assert len(kept) == 1

    def test_top_of_band_is_what_is_compared(self):
        """A 90k-150k posting passes a 120k floor: the employer will pay it."""
        kept, _ = apply_salary_floor([_row(90000, 150000)], 120000)
        assert len(kept) == 1

    def test_unknown_salary_is_kept_by_default(self):
        kept, dropped = apply_salary_floor([_row()], 120000)
        assert len(kept) == 1 and dropped == 0

    def test_unknown_salary_is_dropped_only_on_explicit_opt_out(self):
        kept, dropped = apply_salary_floor([_row()], 120000, include_unknown=False)
        assert kept == [] and dropped == 1

    def test_published_pay_only_is_a_filter_without_a_floor(self):
        kept, dropped = apply_salary_floor([_row(), _row(50000)], 0,
                                           include_unknown=False)
        assert len(kept) == 1 and dropped == 1

    def test_no_floor_and_unknown_allowed_is_a_no_op(self):
        rows = [_row(), _row(50000), _row(None, 200000)]
        kept, dropped = apply_salary_floor(rows, 0)
        assert kept == rows and dropped == 0

    def test_zero_is_not_unknown(self):
        """A stated 0 is a real (bad) band and fails a floor; it must not be
        treated as 'undisclosed' and kept."""
        kept, dropped = apply_salary_floor([{"salary_min_annual": 0,
                                             "salary_max_annual": 0}], 1000)
        assert kept == [] and dropped == 1


class TestRouteValidation:
    """The route helper is where the field was missing entirely."""

    @staticmethod
    def _f(data):
        import sys
        sys.path.insert(0, "services/jobs")
        from blueprints.jobs import _salary_filter
        return _salary_filter(data)

    def test_min_salary_is_parsed_and_forwarded(self):
        assert self._f({"min_salary": 120000})["min_salary"] == 120000.0

    def test_min_salary_accepts_a_string(self):
        assert self._f({"min_salary": "150000"})["min_salary"] == 150000.0

    def test_absent_min_salary_is_none_not_zero(self):
        assert self._f({})["min_salary"] is None

    def test_garbage_min_salary_does_not_500(self):
        assert self._f({"min_salary": "abc"})["min_salary"] is None
        assert self._f({"min_salary": -5})["min_salary"] is None
        assert self._f({"min_salary": 10 ** 12})["min_salary"] is None

    def test_include_unknown_defaults_true(self):
        assert self._f({})["include_unknown_salary"] is True

    def test_include_unknown_respects_explicit_false(self):
        assert self._f({"include_unknown_salary": False})["include_unknown_salary"] is False
