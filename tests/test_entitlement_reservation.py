"""Offline tests for DEFECT G — atomic, exactly-once entitlement RESERVATION
for auto-apply (fixes the check-then-increment double-charge race for
``auto_submit`` / ``autoapply_answer``).

Two layers:

  * quota_ledger_service.reserve (the feature-parameterized atomic counterpart to
    entitlements_service.check_and_increment): a single guarded ``$inc`` bounded
    below the plan cap — exactly one winner at the boundary, idempotency-keyed
    redelivery never double-reserves, refund idempotent + floored at 0, and the
    per-plan cap NUMBERS come straight from entitlements_service (never a copy).
  * the auto-apply lifecycle wired through application_ledger_service: RESERVE at
    approval, FINALIZE on confirmed, RELEASE on a SAFE pre-submit terminal
    (failed/unsupported/cancelled/withdrawn), HOLD on submission_unknown, and
    RELEASE on approval-invalidation back to ready_for_review.

Runs fully offline (mongomock via _vg_support; no Bedrock / live Mongo / network).
"""
import pytest

from _vg_support import current_db

import services.application_ledger_service as ledger
import services.entitlements_service as ent
import services.quota_ledger_service as ql

USER = "reserve.user@aspirely.me"
_JD = "We are hiring a backend engineer to build distributed systems in Python."

# GOLDEN auto_submit cap numbers — G must preserve these while fixing atomicity.
FREE_AUTO_SUBMIT_CAP = 10
PREMIUM_AUTO_SUBMIT_CAP = 50
PREMIUM_PLUS_AUTO_SUBMIT_CAP = None

_COLLECTIONS = (
    "applications", "auto_submit_audit", "auto_apply_answers",
    "auto_apply_freezes", "quota_counters", "quota_ledger", "feature_usage",
    "users", "entitlement_overrides",
)


@pytest.fixture()
def db():
    d = current_db()
    for c in _COLLECTIONS:
        d[c].delete_many({})
    d.applications.create_index(
        [("user_email", 1), ("job_identity", 1)], unique=True,
        name="user_email_job_identity_unique")
    d.applications.create_index(
        [("application_id", 1)], unique=True, name="application_id_unique")
    d.users.insert_one({"email": USER, "plan": "free"})   # auto_submit cap 10
    return d


@pytest.fixture()
def reserve_on(monkeypatch):
    """Flip the dark-launch reservation gate ON for the wired-lifecycle tests."""
    monkeypatch.setattr(ledger, "_reserve_enabled", lambda: True)
    monkeypatch.setenv("AUTO_APPLY_RESERVE", "true")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _auto_submit_counter_id():
    return ql.counter_id_for(USER, feature="auto_submit")


def _auto_submit_count(db):
    doc = db.quota_counters.find_one({"_id": _auto_submit_counter_id()})
    return int((doc or {}).get("count", 0))


def _seat_counter(db, count):
    db.quota_counters.update_one(
        {"_id": _auto_submit_counter_id()},
        {"$set": {"count": count, "limit_snapshot": FREE_AUTO_SUBMIT_CAP}},
        upsert=True)


def _create(company="Acme", title="Software Engineer"):
    view, _ = ledger.create_application(
        USER, company=company, title=title, jd_text=_JD, source="greenhouse",
        ats_family="greenhouse", apply_url="https://boards.greenhouse.io/x/jobs/1")
    return view["application_id"]


def _seat_review(aid):
    ledger.mark_gathering(USER, aid)
    ledger.mark_ready_for_review(USER, aid)


def _approve(aid, snapshot=None):
    provider = (lambda a: snapshot) if snapshot is not None else None
    return ledger.approve(USER, [aid], approved_by="user", snapshot_provider=provider)


def _reservation_entry_ids(db, aid):
    row = db.applications.find_one({"application_id": aid, "user_email": USER})
    return ((row or {}).get("auto_submit_reservation") or {}).get("entry_ids") or []


def _entry_state(db, entry_id):
    e = db.quota_ledger.find_one({"entry_id": entry_id})
    return (e or {}).get("state")


# ===========================================================================
# 1. Generic atomic reserve — the check-then-increment fix
# ===========================================================================
class TestGenericReserveAtomicity:
    def test_concurrent_reserve_at_cap_boundary_one_wins(self, db):
        # Counter seated at cap-1: two competing reserves — the guarded
        # find_one_and_update lets EXACTLY ONE through (the race the bug lost).
        _seat_counter(db, FREE_AUTO_SUBMIT_CAP - 1)
        first = ql.reserve(USER, "auto_submit")
        second = ql.reserve(USER, "auto_submit")
        assert [first["reserved"], second["reserved"]].count(True) == 1
        assert _auto_submit_count(db) == FREE_AUTO_SUBMIT_CAP   # never above cap
        winner = first if first["reserved"] else second
        loser = second if first["reserved"] else first
        assert len(winner["entry_ids"]) == 1
        assert loser["entry_ids"] == []
        assert loser["usage"]["remaining"] == 0                # pre-attempt state

    def test_reserve_respects_entitlements_cap_numbers(self, db):
        # The cap comes from entitlements_service — assert both the NUMBERS and
        # that reserve enforces exactly that many (10 free auto_submits, then block).
        assert ent.PLAN_LIMITS[ent.FREE]["auto_submit"] == FREE_AUTO_SUBMIT_CAP
        assert ent.PLAN_LIMITS[ent.PREMIUM]["auto_submit"] == PREMIUM_AUTO_SUBMIT_CAP
        assert ent.PLAN_LIMITS[ent.PREMIUM_PLUS]["auto_submit"] == PREMIUM_PLUS_AUTO_SUBMIT_CAP

        flags = [ql.reserve(USER, "auto_submit")["reserved"]
                 for _ in range(FREE_AUTO_SUBMIT_CAP + 1)]
        assert flags == [True] * FREE_AUTO_SUBMIT_CAP + [False]
        assert _auto_submit_count(db) == FREE_AUTO_SUBMIT_CAP
        # The blocked usage reflects the pre-attempt state (hand to the 429 body).
        blocked = ql.reserve(USER, "auto_submit")
        assert blocked["reserved"] is False
        assert blocked["usage"]["used"] == FREE_AUTO_SUBMIT_CAP
        assert blocked["usage"]["remaining"] == 0
        assert blocked["usage"]["limit"] == FREE_AUTO_SUBMIT_CAP

    def test_unlimited_plan_never_blocks(self, db):
        db.users.update_one({"email": USER}, {"$set": {"plan": "premium_plus"}})
        for _ in range(FREE_AUTO_SUBMIT_CAP + 3):
            assert ql.reserve(USER, "auto_submit")["reserved"] is True
        u = ql.reserve(USER, "auto_submit")["usage"]
        assert u["limit"] is None and u["unlimited"] is True and u["remaining"] is None

    def test_idempotency_key_never_double_reserves(self, db):
        r1 = ql.reserve(USER, "auto_submit", idempotency_key="redeliver-1")
        r2 = ql.reserve(USER, "auto_submit", idempotency_key="redeliver-1")
        assert r1["reserved"] and r2["reserved"]
        assert r1["entry_ids"] == r2["entry_ids"]              # same reservation
        assert _auto_submit_count(db) == 1                     # not double-charged
        assert db.quota_ledger.count_documents({"feature": "auto_submit"}) == 1

    def test_refund_is_idempotent_and_floored(self, db):
        r = ql.reserve(USER, "auto_submit")
        eid = r["entry_ids"][0]
        assert ql.refund_entry(eid) is True
        assert _auto_submit_count(db) == 0
        # A second refund (Lambda at-least-once) never double-decrements/goes neg.
        assert ql.refund_entry(eid) is False
        assert _auto_submit_count(db) == 0

    def test_complete_then_refund_keeps_slot_consumed(self, db):
        r = ql.reserve(USER, "auto_submit")
        eid = r["entry_ids"][0]
        assert ql.complete_entry(eid) is True
        assert ql.refund_entry(eid) is False                  # completed != refundable
        assert _auto_submit_count(db) == 1
        assert _entry_state(db, eid) == "completed"


# ===========================================================================
# 2. Reserve at approval (defect G HOOK)
# ===========================================================================
class TestApprovalReserves:
    def test_approve_reserves_one_auto_submit_slot(self, db, reserve_on):
        aid = _create()
        _seat_review(aid)
        assert _approve(aid) == 1
        assert ledger.get_application(USER, aid)["status"] == ledger.STATUS_APPROVED
        eids = _reservation_entry_ids(db, aid)
        assert len(eids) == 1
        assert _entry_state(db, eids[0]) == "reserved"
        assert _auto_submit_count(db) == 1

    def test_over_cap_blocks_approval(self, db, reserve_on):
        _seat_counter(db, FREE_AUTO_SUBMIT_CAP)               # already at the cap
        aid = _create()
        _seat_review(aid)
        assert _approve(aid) == 0                             # reservation denied
        row = ledger.get_application(USER, aid)
        assert row["status"] == ledger.STATUS_READY_FOR_REVIEW  # NOT approved
        assert _reservation_entry_ids(db, aid) == []
        assert _auto_submit_count(db) == FREE_AUTO_SUBMIT_CAP  # unchanged

    def test_duplicate_approve_does_not_double_reserve(self, db, reserve_on):
        aid = _create()
        _seat_review(aid)
        assert _approve(aid) == 1
        # A replayed magic-link / duplicate click → no second transition, and the
        # snapshot-fingerprint idempotency key reuses the SAME reservation.
        assert _approve(aid) == 0
        assert _auto_submit_count(db) == 1
        assert db.quota_ledger.count_documents({"feature": "auto_submit"}) == 1


# ===========================================================================
# 3. Settle at the terminal execution state
# ===========================================================================
class TestReservationLifecycle:
    def _approve_row(self, db, aid):
        _seat_review(aid)
        assert _approve(aid) == 1
        return _reservation_entry_ids(db, aid)[0]

    def test_confirmed_finalizes_slot_consumed(self, db, reserve_on):
        aid = _create()
        eid = self._approve_row(db, aid)
        ledger.transition(USER, aid, ledger.STATUS_QUEUED_FOR_EXECUTION)
        ledger.transition(USER, aid, ledger.STATUS_RUNNING)
        ledger.transition(USER, aid, ledger.STATUS_SUBMITTED)
        ledger.transition(USER, aid, ledger.STATUS_CONFIRMED)
        assert _entry_state(db, eid) == "completed"           # settled, not refunded
        assert _auto_submit_count(db) == 1                    # slot stays consumed

    @pytest.mark.parametrize("drive", [
        # each -> the SAFE pre-submit terminal reached, releasing the slot.
        lambda aid: ledger.transition(USER, aid, ledger.STATUS_CANCELLED),
        lambda aid: ledger.transition(USER, aid, ledger.STATUS_FAILED,
                                      error="boom"),
        lambda aid: (ledger.transition(USER, aid, ledger.STATUS_QUEUED_FOR_EXECUTION),
                     ledger.transition(USER, aid, ledger.STATUS_UNSUPPORTED)),
        lambda aid: (ledger.transition(USER, aid, ledger.STATUS_QUEUED_FOR_EXECUTION),
                     ledger.transition(USER, aid, ledger.STATUS_RUNNING),
                     ledger.transition(USER, aid, ledger.STATUS_SUBMITTED),
                     ledger.transition(USER, aid, ledger.STATUS_WITHDRAWN)),
    ], ids=["cancelled", "failed_pre_submit", "unsupported", "withdrawn"])
    def test_safe_pre_submit_terminal_releases_slot(self, db, reserve_on, drive):
        aid = _create()
        eid = self._approve_row(db, aid)
        assert _auto_submit_count(db) == 1
        drive(aid)
        assert _entry_state(db, eid) == "refunded"            # slot returned
        assert _auto_submit_count(db) == 0                    # floored at 0

    def test_submission_unknown_holds_reservation(self, db, reserve_on):
        aid = _create()
        eid = self._approve_row(db, aid)
        ledger.transition(USER, aid, ledger.STATUS_QUEUED_FOR_EXECUTION)
        ledger.transition(USER, aid, ledger.STATUS_RUNNING)
        ledger.transition(USER, aid, ledger.STATUS_SUBMISSION_UNKNOWN)
        # HELD pending reconciliation — the slot stays consumed (NOT refunded).
        assert _entry_state(db, eid) == "reserved"
        assert _auto_submit_count(db) == 1

    def test_invalidation_back_to_review_releases_and_reapproval_reserves_fresh(
            self, db, reserve_on):
        aid = _create()
        _seat_review(aid)
        assert _approve(aid, snapshot={"application_hash": "h1"}) == 1
        eid = _reservation_entry_ids(db, aid)[0]
        assert _auto_submit_count(db) == 1
        # Approval invalidated back to review (F stale-snapshot path) → release.
        ledger.transition(USER, aid, ledger.STATUS_READY_FOR_REVIEW)
        assert _entry_state(db, eid) == "refunded"
        assert _auto_submit_count(db) == 0
        assert _reservation_entry_ids(db, aid) == []          # linkage cleared
        # Re-approval with the DRIFTED snapshot (the real reason a row is kicked
        # back to review) mints a fresh reservation under a new idempotency key.
        assert _approve(aid, snapshot={"application_hash": "h2"}) == 1
        assert _auto_submit_count(db) == 1


# ===========================================================================
# 4. autoapply_answer migrates to the same atomic reserve+complete (route spend)
# ===========================================================================
class TestAutoApplyAnswerSpend:
    def test_reserve_then_complete_meters_answer_bucket(self, db):
        # The extension answer route now does a single reserve immediately
        # completed — the point is removing the check-then-increment race, not
        # holding the slot. Free autoapply_answer cap is entitlements' number.
        cap = ent.PLAN_LIMITS[ent.FREE]["autoapply_answer"]
        for _ in range(cap):
            r = ql.reserve(USER, "autoapply_answer")
            assert r["reserved"] is True
            for eid in r["entry_ids"]:
                assert ql.complete_entry(eid) is True
        blocked = ql.reserve(USER, "autoapply_answer")
        assert blocked["reserved"] is False
        assert blocked["usage"]["remaining"] == 0
        # No feature_usage double count — this bucket is metered on the ledger now.
        assert db.feature_usage.count_documents({"feature": "autoapply_answer"}) == 0
