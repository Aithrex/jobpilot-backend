"""CHARACTERIZATION — asserts CURRENT behavior to guard the E/F/G refactor;
update deliberately when that behavior is intentionally changed.

Scope: entitlements_service metering for the "auto_submit" monthly cap — the
billing semantics defect G ("atomic check_and_increment") must PRESERVE while it
fixes the atomicity race. We are NOT asserting atomicity here (that IS the bug G
fixes under concurrency); we pin the cap NUMBERS and the single-threaded cap
SEMANTICS so G cannot silently change how many auto-submits a plan gets or how
the combined check+increment behaves at the boundary.

The cap numbers are captured as module constants below so a later change to
PLAN_LIMITS is visible as a diff in THIS file, not a surprise in production.

Runs fully offline (mongomock; no network). Deterministic within a run: the
feature_usage counter is keyed by the UTC period ("month"), and every assertion is
made against a freshly-cleared counter within a single month.
"""
import pytest

from _vg_support import current_db

import services.entitlements_service as ent

FEATURE = "auto_submit"

# GOLDEN cap numbers for the metered auto_submit feature. A later change to
# PLAN_LIMITS[...]["auto_submit"] MUST update these deliberately.
FREE_CAP = 25
PREMIUM_CAP = 200
PREMIUM_PLUS_CAP = None          # None == unlimited (soft-capped elsewhere)

FREE_USER = "free@aspirely.me"
PLUS_USER = "plus@aspirely.me"


@pytest.fixture()
def ent_db():
    d = current_db()
    d.feature_usage.delete_many({})
    d.users.delete_many({})
    d.entitlement_overrides.delete_many({})
    return d


# ---------------------------------------------------------------------------
# Cap constants (golden) — the numbers G must preserve
# ---------------------------------------------------------------------------
class TestCapConstants:
    def test_auto_submit_is_a_monthly_metered_feature(self):
        assert ent.FEATURE_PERIODS[FEATURE] == ent.PERIOD_MONTH

    def test_plan_limits_are_this_golden(self):
        assert ent.PLAN_LIMITS[ent.FREE][FEATURE] == FREE_CAP
        assert ent.PLAN_LIMITS[ent.PREMIUM][FEATURE] == PREMIUM_CAP
        assert ent.PLAN_LIMITS[ent.PREMIUM_PLUS][FEATURE] == PREMIUM_PLUS_CAP

    def test_default_plan_resolves_free_cap(self, ent_db):
        # No users doc -> default FREE plan -> the free auto_submit cap applies.
        assert ent.quota(FREE_USER, FEATURE) == FREE_CAP


# ---------------------------------------------------------------------------
# check_quota at the cap boundary (no increment)
# ---------------------------------------------------------------------------
class TestCheckQuotaBoundary:
    def test_allowed_just_under_the_cap_blocked_at_the_cap(self, ent_db):
        # Seat usage at cap-1: one more is still allowed.
        for _ in range(FREE_CAP - 1):
            ent.increment_usage(FREE_USER, FEATURE)
        allowed, usage = ent.check_quota(FREE_USER, FEATURE, 1)
        assert allowed is True
        assert usage["used"] == FREE_CAP - 1
        assert usage["remaining"] == 1
        assert usage["limit"] == FREE_CAP

        # One more increment lands exactly at the cap: now blocked.
        ent.increment_usage(FREE_USER, FEATURE)
        allowed, usage = ent.check_quota(FREE_USER, FEATURE, 1)
        assert allowed is False
        assert usage["used"] == FREE_CAP
        assert usage["remaining"] == 0

    def test_unlimited_plan_never_blocks(self, ent_db):
        ent_db.users.insert_one({"email": PLUS_USER, "plan": ent.PREMIUM_PLUS})
        allowed, usage = ent.check_quota(PLUS_USER, FEATURE, 999)
        assert allowed is True
        assert usage["limit"] is None
        assert usage["unlimited"] is True
        assert usage["remaining"] is None


# ---------------------------------------------------------------------------
# increment_usage — the metered counter
# ---------------------------------------------------------------------------
class TestIncrementUsage:
    def test_increment_bumps_the_metered_counter(self, ent_db):
        u0 = ent.get_usage(FREE_USER, FEATURE)
        assert u0["used"] == 0
        u1 = ent.increment_usage(FREE_USER, FEATURE)
        assert u1["used"] == 1
        assert u1["remaining"] == FREE_CAP - 1
        u3 = ent.increment_usage(FREE_USER, FEATURE, 2)
        assert u3["used"] == 3            # $inc by n

    def test_counter_is_scoped_per_user_and_feature(self, ent_db):
        ent.increment_usage(FREE_USER, FEATURE)
        # A different user's counter is independent.
        assert ent.get_usage(PLUS_USER, FEATURE)["used"] == 0
        # A different feature's counter is independent.
        assert ent.get_usage(FREE_USER, "ats_scores")["used"] == 0


# ---------------------------------------------------------------------------
# check_and_increment — current combined behavior (NOT atomicity; that's G)
# ---------------------------------------------------------------------------
class TestCheckAndIncrement:
    def test_exactly_cap_successes_then_blocks(self, ent_db):
        # The combined call checks BEFORE incrementing, so exactly FREE_CAP calls
        # succeed and the (cap+1)th is blocked. This is the single-threaded cap
        # semantics G must preserve while fixing the concurrent race.
        results = [ent.check_and_increment(FREE_USER, FEATURE)
                   for _ in range(FREE_CAP + 1)]
        allowed_flags = [ok for ok, _ in results]
        assert allowed_flags == [True] * FREE_CAP + [False]
        # The final (blocked) usage reflects the pre-attempt state (used==cap),
        # so the caller can hand it straight back to the client.
        _, last_usage = results[-1]
        assert last_usage["used"] == FREE_CAP
        assert last_usage["remaining"] == 0
        # And no over-count occurred — the counter stops at the cap.
        assert ent.get_usage(FREE_USER, FEATURE)["used"] == FREE_CAP

    def test_blocked_call_does_not_increment(self, ent_db):
        for _ in range(FREE_CAP):
            ent.check_and_increment(FREE_USER, FEATURE)
        before = ent.get_usage(FREE_USER, FEATURE)["used"]
        ok, _ = ent.check_and_increment(FREE_USER, FEATURE)
        assert ok is False
        assert ent.get_usage(FREE_USER, FEATURE)["used"] == before   # unchanged

    def test_unlimited_plan_increments_but_always_allows(self, ent_db):
        ent_db.users.insert_one({"email": PLUS_USER, "plan": ent.PREMIUM_PLUS})
        for _ in range(FREE_CAP + 3):
            ok, _ = ent.check_and_increment(PLUS_USER, FEATURE)
            assert ok is True
        # Still metered even when unlimited (usage is tracked, just never gated).
        assert ent.get_usage(PLUS_USER, FEATURE)["used"] == FREE_CAP + 3
