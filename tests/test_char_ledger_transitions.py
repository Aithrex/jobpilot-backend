"""CHARACTERIZATION — asserts CURRENT behavior to guard the E/F/G refactor;
update deliberately when that behavior is intentionally changed.

Scope: the applications LEDGER (application_ledger_service) — the reader/writer
surface the canonical-applications state-machine refactor (defects E/F/G) will
touch. We pin, as a golden safety net, exactly:

  * the full _TRANSITION_GUARDS legality matrix (which from->to moves are
    currently ALLOWED vs BLOCKED), enumerated over the entire status set;
  * create_application's default status + the persisted document shape;
  * approve_to_ready's CURRENT acceptance set (queued/gathering/needs_input) —
    this is defect F's present behavior, captured so the tightening is visible;
  * the get_application / list_applications _view output KEYS + shape.

Runs fully offline (mongomock via conftest/_vg_support; no Bedrock / live Mongo
/ network). The unique indexes are declared exactly as scripts/migrate_indexes.py
does so the insert-first / DuplicateKeyError resolution path is real under
mongomock. This file intentionally complements (does not duplicate) the
behavioral coverage in tests/test_application_ledger.py — its job is the golden
snapshot of the legality matrix + reader shapes so the refactor cannot silently
drift them.
"""
import pytest

from _vg_support import current_db

import services.application_ledger_service as als

USER = "alice@aspirely.me"
_JD = "We are hiring a backend engineer to build distributed systems in Python."


@pytest.fixture()
def led_db():
    d = current_db()
    for c in ("applications", "auto_submit_audit"):
        d[c].delete_many({})
    # Mirror scripts/migrate_indexes.py so DuplicateKeyError-backed paths are real.
    d.applications.create_index(
        [("user_email", 1), ("job_identity", 1)], unique=True,
        name="user_email_job_identity_unique",
    )
    d.applications.create_index(
        [("application_id", 1)], unique=True, name="application_id_unique",
    )
    return d


def _create(db_ready, *, company="Acme", title="Software Engineer", jd=_JD,
            source="greenhouse", **kw):
    return als.create_application(USER, company=company, title=title,
                                  jd_text=jd, source=source, **kw)


def _force_status(db, application_id, status):
    """Bypass the CAS guards to seat a row in an arbitrary start state, so the
    legality of EVERY from->to pair can be probed directly."""
    db.applications.update_one(
        {"application_id": application_id, "user_email": USER},
        {"$set": {"status": status}},
    )


# The full status universe the ledger currently knows about.
# E0: canonical states added additively (defect E) — the 10 legacy states below
# stay valid; the 12 canonical states are appended (guard keys now cover the
# whole target machine). `failed_pre_submit` is deliberately absent: it is a
# canonical read-space label, not a ledger status / transition target.
_LEGACY_STATES = (
    als.STATUS_QUEUED, als.STATUS_GATHERING, als.STATUS_NEEDS_INPUT,
    als.STATUS_READY, als.STATUS_SUBMITTING, als.STATUS_SUBMITTED,
    als.STATUS_CONFIRMED, als.STATUS_FAILED, als.STATUS_SKIPPED_DUPLICATE,
    als.STATUS_SKIPPED,
)
_CANONICAL_STATES = (
    als.STATUS_DRAFT, als.STATUS_PREPARING, als.STATUS_READY_FOR_REVIEW,
    als.STATUS_APPROVED, als.STATUS_QUEUED_FOR_EXECUTION, als.STATUS_RUNNING,
    als.STATUS_WAITING_OTP, als.STATUS_WAITING_HUMAN,
    als.STATUS_SUBMISSION_UNKNOWN, als.STATUS_CANCELLED, als.STATUS_UNSUPPORTED,
    als.STATUS_WITHDRAWN,
)
_ALL_STATES = _LEGACY_STATES + _CANONICAL_STATES


# ---------------------------------------------------------------------------
# 1. The transition-guard legality matrix (golden)
# ---------------------------------------------------------------------------
class TestTransitionGuardMatrix:
    def test_guard_table_is_exactly_this(self):
        """Golden snapshot of _TRANSITION_GUARDS. A refactor that adds/removes a
        state or re-parents a transition MUST update this deliberately.

        E0: canonical states added additively (defect E). Every LEGACY edge
        below is byte-for-byte the pre-E0 table; the ONLY changes are unions of
        canonical SOURCES onto existing targets (needs_input/submitted/failed/
        queued) and the appended canonical targets. No legacy edge was removed
        or re-parented — see test_all_legacy_edges_still_legal for the proof."""
        assert als._TRANSITION_GUARDS == {
            # --- legacy edges (canonical sources unioned in where noted) ---
            als.STATUS_GATHERING: (als.STATUS_QUEUED,),
            als.STATUS_NEEDS_INPUT: (
                als.STATUS_GATHERING, als.STATUS_READY, als.STATUS_SUBMITTING,
                als.STATUS_PREPARING,                        # E0: canonical source
                als.STATUS_RUNNING),                         # F: oracle-wall park
            als.STATUS_READY: (als.STATUS_GATHERING, als.STATUS_NEEDS_INPUT),
            als.STATUS_SUBMITTING: (als.STATUS_READY,),
            als.STATUS_SUBMITTED: (
                als.STATUS_SUBMITTING, als.STATUS_RUNNING,   # E0: canonical sources
                als.STATUS_WAITING_OTP, als.STATUS_WAITING_HUMAN),
            als.STATUS_CONFIRMED: (als.STATUS_SUBMITTED,),
            als.STATUS_FAILED: (
                als.STATUS_QUEUED, als.STATUS_GATHERING, als.STATUS_NEEDS_INPUT,
                als.STATUS_READY, als.STATUS_SUBMITTING, als.STATUS_SUBMITTED,
                # E0: canonical live states may also fall to failed.
                als.STATUS_DRAFT, als.STATUS_PREPARING,
                als.STATUS_READY_FOR_REVIEW, als.STATUS_APPROVED,
                als.STATUS_QUEUED_FOR_EXECUTION, als.STATUS_RUNNING,
                als.STATUS_WAITING_OTP, als.STATUS_WAITING_HUMAN),
            als.STATUS_SKIPPED_DUPLICATE: (als.STATUS_QUEUED, als.STATUS_GATHERING),
            als.STATUS_SKIPPED: (
                als.STATUS_QUEUED, als.STATUS_GATHERING, als.STATUS_NEEDS_INPUT,
                als.STATUS_READY),
            als.STATUS_QUEUED: (
                als.STATUS_FAILED, als.STATUS_SKIPPED_DUPLICATE,
                als.STATUS_DRAFT),                           # E0: canonical source
            # --- E0: canonical targets, additive (defect E) ---
            als.STATUS_DRAFT: (),
            als.STATUS_PREPARING: (als.STATUS_QUEUED,),
            als.STATUS_READY_FOR_REVIEW: (
                als.STATUS_NEEDS_INPUT, als.STATUS_PREPARING,
                als.STATUS_GATHERING,                        # F: live auto/prepare landing
                als.STATUS_APPROVED,                         # F: approval invalidation
                als.STATUS_QUEUED_FOR_EXECUTION),            # F: approval invalidation
            als.STATUS_APPROVED: (als.STATUS_READY_FOR_REVIEW,),
            als.STATUS_QUEUED_FOR_EXECUTION: (als.STATUS_APPROVED,),
            als.STATUS_RUNNING: (als.STATUS_QUEUED_FOR_EXECUTION,),
            als.STATUS_WAITING_OTP: (als.STATUS_RUNNING,),
            als.STATUS_WAITING_HUMAN: (als.STATUS_RUNNING,),
            als.STATUS_CANCELLED: (
                als.STATUS_DRAFT, als.STATUS_QUEUED, als.STATUS_PREPARING,
                als.STATUS_NEEDS_INPUT, als.STATUS_READY_FOR_REVIEW,
                als.STATUS_APPROVED, als.STATUS_QUEUED_FOR_EXECUTION),
            als.STATUS_UNSUPPORTED: (
                als.STATUS_PREPARING, als.STATUS_QUEUED_FOR_EXECUTION,
                als.STATUS_RUNNING),
            als.STATUS_WITHDRAWN: (als.STATUS_SUBMITTED,),
            als.STATUS_SUBMISSION_UNKNOWN: (
                als.STATUS_RUNNING, als.STATUS_WAITING_OTP,
                als.STATUS_WAITING_HUMAN),
        }

    def test_valid_target_statuses_are_exactly_the_guard_keys(self):
        # `transition` rejects any to_status not in this key set (unknown status).
        # E0: canonical states added additively (defect E) — _ALL_STATES now
        # carries the 10 legacy + 12 canonical guard keys.
        assert set(als._TRANSITION_GUARDS) == set(_ALL_STATES)

    def test_failed_pre_submit_is_canonical_only_not_a_ledger_target(self):
        # E0: `failed_pre_submit` is the canonical read-space peer of `failed`;
        # it is NOT a valid ledger status nor a transition target (the ledger
        # still stores `failed`). Guards it against accidental promotion.
        assert als.STATUS_FAILED_PRE_SUBMIT not in als._TRANSITION_GUARDS
        assert als.STATUS_FAILED_PRE_SUBMIT not in als.ALL_STATUSES

    def test_every_from_to_pair_matches_its_guard(self, led_db):
        """Enumerate the ENTIRE from x to matrix: a transition succeeds iff the
        source state is listed in the target's guard tuple, otherwise it is a
        409 InvalidTransition. This is the load-bearing legality contract E/F
        must preserve verbatim (or change on purpose)."""
        view, _ = _create(led_db)
        aid = view["application_id"]
        for to_status, guard in als._TRANSITION_GUARDS.items():
            for from_status in _ALL_STATES:
                _force_status(led_db, aid, from_status)
                if from_status in guard:
                    out = als.transition(USER, aid, to_status)
                    assert out["status"] == to_status, (
                        f"{from_status}->{to_status} should be ALLOWED")
                else:
                    with pytest.raises(als.InvalidTransition) as ei:
                        als.transition(USER, aid, to_status)
                    assert ei.value.status == 409, (
                        f"{from_status}->{to_status} should be BLOCKED (409)")

    def test_terminal_states_have_no_legal_forward_move(self, led_db):
        # confirmed / skipped are dead-ends (never a source in any guard);
        # skipped_duplicate + failed are sources ONLY for the explicit re-queue.
        sources = set()
        for guard in als._TRANSITION_GUARDS.values():
            sources.update(guard)
        assert als.STATUS_CONFIRMED not in sources
        assert als.STATUS_SKIPPED not in sources
        assert sources & {als.STATUS_FAILED, als.STATUS_SKIPPED_DUPLICATE} == {
            als.STATUS_FAILED, als.STATUS_SKIPPED_DUPLICATE}  # only feed queued


# ---------------------------------------------------------------------------
# 1b. E0 no-regression: legacy edges still legal + canonical edges legal (defect E)
# ---------------------------------------------------------------------------
# Every (from, to) pair that was legal in the PRE-E0 guard matrix, captured as a
# literal so the union additions cannot silently drop one of the original True
# cells. (Enumerated straight from the pre-E0 golden table.)
_LEGACY_LEGAL_EDGES = (
    (als.STATUS_QUEUED, als.STATUS_GATHERING),
    (als.STATUS_GATHERING, als.STATUS_NEEDS_INPUT),
    (als.STATUS_READY, als.STATUS_NEEDS_INPUT),
    (als.STATUS_SUBMITTING, als.STATUS_NEEDS_INPUT),
    (als.STATUS_GATHERING, als.STATUS_READY),
    (als.STATUS_NEEDS_INPUT, als.STATUS_READY),
    (als.STATUS_READY, als.STATUS_SUBMITTING),
    (als.STATUS_SUBMITTING, als.STATUS_SUBMITTED),
    (als.STATUS_SUBMITTED, als.STATUS_CONFIRMED),
    (als.STATUS_QUEUED, als.STATUS_FAILED),
    (als.STATUS_GATHERING, als.STATUS_FAILED),
    (als.STATUS_NEEDS_INPUT, als.STATUS_FAILED),
    (als.STATUS_READY, als.STATUS_FAILED),
    (als.STATUS_SUBMITTING, als.STATUS_FAILED),
    (als.STATUS_SUBMITTED, als.STATUS_FAILED),
    (als.STATUS_QUEUED, als.STATUS_SKIPPED_DUPLICATE),
    (als.STATUS_GATHERING, als.STATUS_SKIPPED_DUPLICATE),
    (als.STATUS_QUEUED, als.STATUS_SKIPPED),
    (als.STATUS_GATHERING, als.STATUS_SKIPPED),
    (als.STATUS_NEEDS_INPUT, als.STATUS_SKIPPED),
    (als.STATUS_READY, als.STATUS_SKIPPED),
    (als.STATUS_FAILED, als.STATUS_QUEUED),
    (als.STATUS_SKIPPED_DUPLICATE, als.STATUS_QUEUED),
)

# The canonical machine's forward edges (defect E target) that E0 makes legal.
_CANONICAL_LEGAL_EDGES = (
    (als.STATUS_DRAFT, als.STATUS_QUEUED),
    (als.STATUS_QUEUED, als.STATUS_PREPARING),
    (als.STATUS_PREPARING, als.STATUS_NEEDS_INPUT),
    (als.STATUS_PREPARING, als.STATUS_READY_FOR_REVIEW),
    (als.STATUS_NEEDS_INPUT, als.STATUS_READY_FOR_REVIEW),
    (als.STATUS_READY_FOR_REVIEW, als.STATUS_APPROVED),
    (als.STATUS_APPROVED, als.STATUS_QUEUED_FOR_EXECUTION),
    (als.STATUS_QUEUED_FOR_EXECUTION, als.STATUS_RUNNING),
    (als.STATUS_RUNNING, als.STATUS_WAITING_OTP),
    (als.STATUS_RUNNING, als.STATUS_WAITING_HUMAN),
    (als.STATUS_RUNNING, als.STATUS_SUBMITTED),
    (als.STATUS_WAITING_OTP, als.STATUS_SUBMITTED),
    (als.STATUS_WAITING_HUMAN, als.STATUS_SUBMITTED),
    # canonical terminals from the appropriate in-flight states
    (als.STATUS_APPROVED, als.STATUS_CANCELLED),
    (als.STATUS_PREPARING, als.STATUS_UNSUPPORTED),
    (als.STATUS_RUNNING, als.STATUS_UNSUPPORTED),
    (als.STATUS_SUBMITTED, als.STATUS_WITHDRAWN),
    (als.STATUS_RUNNING, als.STATUS_SUBMISSION_UNKNOWN),
    (als.STATUS_WAITING_OTP, als.STATUS_SUBMISSION_UNKNOWN),
    (als.STATUS_RUNNING, als.STATUS_FAILED),          # canonical live → failed
)


class TestE0AdditiveNoRegression:
    """Defect E / E0: the guard-table change is a strict UNION — no pre-existing
    legal edge is lost, and the canonical edges are now legal."""

    @pytest.mark.parametrize("from_status,to_status", _LEGACY_LEGAL_EDGES)
    def test_all_legacy_edges_still_legal(self, led_db, from_status, to_status):
        view, _ = _create(led_db)
        aid = view["application_id"]
        _force_status(led_db, aid, from_status)
        out = als.transition(USER, aid, to_status)
        assert out["status"] == to_status

    @pytest.mark.parametrize("from_status,to_status", _CANONICAL_LEGAL_EDGES)
    def test_canonical_edges_are_legal(self, led_db, from_status, to_status):
        view, _ = _create(led_db)
        aid = view["application_id"]
        _force_status(led_db, aid, from_status)
        out = als.transition(USER, aid, to_status)
        assert out["status"] == to_status


# ---------------------------------------------------------------------------
# 2. create_application default status + persisted document shape (golden)
# ---------------------------------------------------------------------------
class TestCreateShape:
    def test_default_status_and_defaults(self, led_db):
        view, outcome = _create(led_db)
        assert outcome == "created"
        assert view["status"] == als.STATUS_QUEUED
        assert view["needs_input"] == []
        assert view["error"] is None
        assert view["autonomy"] == "ask"          # default when unspecified
        assert view["confidence_score"] == 0.0
        assert view["submitted_at"] is None
        assert view["confirmed_at"] is None
        # audit_ref is the application_id (the auto_submit_audit grouping key).
        assert view["audit_ref"] == view["application_id"]

    def test_persisted_document_carries_scope_and_identity(self, led_db):
        view, _ = _create(led_db)
        raw = led_db.applications.find_one(
            {"application_id": view["application_id"]})
        assert raw["user_email"] == USER
        assert als._HEX64.match(raw["job_identity"])
        assert raw["status"] == als.STATUS_QUEUED
        assert raw["created_at"] is not None and raw["updated_at"] is not None


# ---------------------------------------------------------------------------
# 3. approve acceptance set — DEFECT F (approval tightened to ready_for_review)
# ---------------------------------------------------------------------------
class TestApprovePostF:
    # F tightened approval: `approve` moves ONLY `ready_for_review` (tolerating a
    # legacy `ready`) → `approved`, freezing a snapshot — NOT the pre-F
    # queued/gathering/needs_input → `ready`. This is the deliberate golden update
    # captured here (was TestApproveToReadyPreF).
    @pytest.mark.parametrize("start", [
        als.STATUS_READY_FOR_REVIEW,
        als.STATUS_READY,   # F: legacy `ready` tolerated as ready_for_review
    ])
    def test_approvable_from_ready_for_review(self, led_db, start):
        view, _ = _create(led_db)
        _force_status(led_db, view["application_id"], start)
        moved = als.approve(USER, [view["application_id"]])
        assert moved == 1
        assert als.get_application(
            USER, view["application_id"])["status"] == als.STATUS_APPROVED

    def test_approvable_set_constant_is_this_golden(self):
        # F: approval tightened to ready_for_review.
        assert als._APPROVABLE_STATUSES == (als.STATUS_READY_FOR_REVIEW,)

    @pytest.mark.parametrize("start", [
        als.STATUS_QUEUED, als.STATUS_GATHERING, als.STATUS_NEEDS_INPUT,
        als.STATUS_APPROVED, als.STATUS_SUBMITTING, als.STATUS_SUBMITTED,
        als.STATUS_CONFIRMED, als.STATUS_SKIPPED,
    ])
    def test_non_approvable_states_are_left_untouched(self, led_db, start):
        view, _ = _create(led_db)
        _force_status(led_db, view["application_id"], start)
        moved = als.approve(USER, [view["application_id"]])
        assert moved == 0
        assert als.get_application(
            USER, view["application_id"])["status"] == start


# ---------------------------------------------------------------------------
# 4. Reader output shape — get_application / list_applications _view keys
# ---------------------------------------------------------------------------
_VIEW_KEYS = {
    "application_id", "job_identity", "source", "ats_family", "tenant",
    "company", "title", "jd_hash", "apply_url", "posting_closes_at",
    "tailoring_record_id", "autonomy", "confidence_score", "status",
    "needs_input", "error", "created_at", "updated_at", "submitted_at",
    "confirmed_at", "audit_ref",
}


class TestReaderShape:
    def test_get_application_view_keys_are_exactly_this(self, led_db):
        view, _ = _create(led_db)
        got = als.get_application(USER, view["application_id"])
        assert set(got) == _VIEW_KEYS

    def test_get_application_unknown_is_none(self, led_db):
        assert als.get_application(USER, "nope") is None

    def test_list_applications_returns_list_of_views(self, led_db):
        _create(led_db)
        _create(led_db, company="Globex", title="Data Scientist")
        rows = als.list_applications(USER)
        assert isinstance(rows, list) and len(rows) == 2
        for r in rows:
            assert set(r) == _VIEW_KEYS

    def test_list_applications_status_filter_and_ordering(self, led_db):
        a, _ = _create(led_db)
        b, _ = _create(led_db, company="Globex", title="Data Scientist")
        # newest-first (created_at desc); both queued.
        rows = als.list_applications(USER, status=als.STATUS_QUEUED)
        assert [r["application_id"] for r in rows][0] in (
            a["application_id"], b["application_id"])
        assert all(r["status"] == als.STATUS_QUEUED for r in rows)

    def test_list_applications_rejects_unknown_status(self, led_db):
        with pytest.raises(als.InvalidApplicationInput):
            als.list_applications(USER, status="teleported")
