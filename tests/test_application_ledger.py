"""Offline tests for the auto-apply applications ledger (Tranche 1 §1).

Runs fully offline (mongomock via conftest/_vg_support; no Bedrock, no live
Mongo, no network). Covers the correctness spine of auto-apply:

  * ``normalized_job_identity`` stability — the same posting seen with
    seniority / location / req-id noise collapses to ONE stable dedup key.
  * ``dedup_gate`` skip rules — already-handled vs. retryable vs. novel.
  * ``create_application`` insert-conflict → ``skipped_duplicate`` / ``requeued``
    (backed by the real unique (user_email, job_identity) index in mongomock).
  * the status-transition compare-and-set — legal transitions succeed, illegal
    ones raise 409 InvalidTransition, and every transition is user-scoped.

The unique indexes are created in the ``led_db`` fixture the same way
scripts/migrate_indexes.py declares them, so the DuplicateKeyError backstop the
service relies on actually fires under mongomock.
"""
import pytest

from _vg_support import current_db

import services.application_ledger_service as als

USER_A = "alice@aspirely.me"
USER_B = "bob@aspirely.me"

_JD = "We are hiring a backend engineer to build distributed systems in Python."


@pytest.fixture()
def led_db():
    """Clean ledger collections + the unique indexes the service depends on."""
    d = current_db()
    for c in ("applications", "auto_submit_audit"):
        d[c].delete_many({})
    # Mirror scripts/migrate_indexes.py — mongomock enforces these, so the
    # insert-first / DuplicateKeyError resolution path is exercised for real.
    d.applications.create_index(
        [("user_email", 1), ("job_identity", 1)], unique=True,
        name="user_email_job_identity_unique",
    )
    d.applications.create_index(
        [("application_id", 1)], unique=True, name="application_id_unique",
    )
    return d


def _create(db_ready, user=USER_A, *, company="Acme", title="Software Engineer",
            jd=_JD, source="greenhouse", **kw):
    return als.create_application(user, company=company, title=title,
                                  jd_text=jd, source=source, **kw)


# ---------------------------------------------------------------------------
# normalized_job_identity + canonical_title stability
# ---------------------------------------------------------------------------
class TestJobIdentity:
    def test_seniority_location_reqid_noise_collapse_to_same_stem(self):
        base = als.normalized_job_identity("Acme", "Software Engineer", _JD)
        noisy = als.normalized_job_identity(
            "Acme", "Sr. Software Engineer II (Remote) - REQ-4711", _JD)
        located = als.normalized_job_identity(
            "Acme", "Software Engineer, Seattle, WA", _JD)
        assert base == noisy == located

    def test_canonical_title_strips_noise(self):
        assert als.canonical_title(
            "Sr. Software Engineer II (Remote) - REQ-4711") == "software engineer"
        assert als.canonical_title("Software Engineer, Seattle, WA") == "software engineer"

    def test_canonical_title_preserves_language_symbols(self):
        # +/# must survive tokenization so c++ / c# roles don't collide.
        assert "c++" in als.canonical_title("Senior C++ Developer")
        assert "c#" in als.canonical_title("C# Engineer")

    def test_identity_is_64_char_lowercase_hex(self):
        ident = als.normalized_job_identity("Acme", "Software Engineer", _JD)
        assert als._HEX64.match(ident)

    def test_different_company_yields_different_identity(self):
        a = als.normalized_job_identity("Acme", "Software Engineer", _JD)
        b = als.normalized_job_identity("Globex", "Software Engineer", _JD)
        assert a != b

    def test_jd_hash_is_whitespace_and_case_insensitive(self):
        a = als.normalized_job_identity("Acme", "Software Engineer", _JD)
        b = als.normalized_job_identity(
            "Acme", "Software Engineer", "  " + _JD.upper() + "\n\t ")
        assert a == b

    def test_genuinely_different_req_at_same_company_disambiguated(self):
        a = als.normalized_job_identity("Acme", "Software Engineer", _JD)
        b = als.normalized_job_identity(
            "Acme", "Software Engineer", "Totally different role: data scientist role.")
        assert a != b


# ---------------------------------------------------------------------------
# dedup_gate skip rules
# ---------------------------------------------------------------------------
class TestDedupGate:
    def test_no_row_enqueue_freely(self, led_db):
        ident = als.normalized_job_identity("Acme", "Software Engineer", _JD)
        skip, view = als.dedup_gate(USER_A, ident)
        assert skip is False and view is None

    def test_active_row_is_skipped(self, led_db):
        view, outcome = _create(led_db)
        assert outcome == "created"
        skip, existing = als.dedup_gate(USER_A, view["job_identity"])
        assert skip is True
        assert existing["application_id"] == view["application_id"]

    def test_failed_row_is_retryable_not_skipped(self, led_db):
        view, _ = _create(led_db)
        als.mark_failed(USER_A, view["application_id"], "boom")
        skip, existing = als.dedup_gate(USER_A, view["job_identity"])
        assert skip is False
        assert existing["status"] == als.STATUS_FAILED

    def test_user_skipped_row_is_treated_as_handled(self, led_db):
        view, _ = _create(led_db)
        als.user_skip(USER_A, [view["application_id"]])
        skip, existing = als.dedup_gate(USER_A, view["job_identity"])
        assert skip is True
        assert existing["status"] == als.STATUS_SKIPPED

    def test_dedup_gate_is_user_scoped(self, led_db):
        view, _ = _create(led_db, USER_A)
        # Same posting, different user — invisible to B.
        skip, existing = als.dedup_gate(USER_B, view["job_identity"])
        assert skip is False and existing is None

    def test_dedup_gate_rejects_non_hex_identity(self, led_db):
        with pytest.raises(als.InvalidApplicationInput):
            als.dedup_gate(USER_A, {"$ne": None})  # injection-shaped
        with pytest.raises(als.InvalidApplicationInput):
            als.dedup_gate(USER_A, "not-a-sha256")


# ---------------------------------------------------------------------------
# create_application — insert / requeue / skipped_duplicate
# ---------------------------------------------------------------------------
class TestCreateApplication:
    def test_create_inserts_queued_row(self, led_db):
        view, outcome = _create(led_db, autonomy="auto", confidence_score=0.9)
        assert outcome == "created"
        assert view["status"] == als.STATUS_QUEUED
        assert view["autonomy"] == "auto"
        assert view["confidence_score"] == 0.9
        assert led_db.applications.count_documents({"user_email": USER_A}) == 1

    def test_duplicate_active_insert_is_skipped_duplicate_noop(self, led_db):
        first, _ = _create(led_db)
        second, outcome = _create(led_db)  # same identity, active row exists
        assert outcome == "skipped_duplicate"
        assert second["application_id"] == first["application_id"]
        # No second row written — the ledger holds exactly one.
        assert led_db.applications.count_documents({"user_email": USER_A}) == 1

    def test_duplicate_over_failed_row_requeues(self, led_db):
        first, _ = _create(led_db)
        als.mark_failed(USER_A, first["application_id"], "network")
        again, outcome = _create(led_db)
        assert outcome == "requeued"
        assert again["application_id"] == first["application_id"]
        assert again["status"] == als.STATUS_QUEUED
        assert again["error"] is None

    def test_confidence_score_clamped(self, led_db):
        view, _ = _create(led_db, confidence_score=5.0)
        assert view["confidence_score"] == 1.0

    def test_missing_company_or_title_rejected(self, led_db):
        with pytest.raises(als.InvalidApplicationInput):
            als.create_application(USER_A, company="", title="Eng",
                                   jd_text=_JD, source="greenhouse")
        with pytest.raises(als.InvalidApplicationInput):
            als.create_application(USER_A, company="Acme", title="",
                                   jd_text=_JD, source="greenhouse")

    def test_invalid_autonomy_rejected(self, led_db):
        with pytest.raises(als.InvalidApplicationInput):
            _create(led_db, autonomy="sometimes")

    def test_create_writes_audit_entry(self, led_db):
        view, _ = _create(led_db)
        audit = led_db.auto_submit_audit.find_one(
            {"application_id": view["application_id"], "action": "application_created"})
        assert audit is not None
        # PII-redacted: only a prefix of the identity is recorded.
        assert audit["meta"]["job_identity_prefix"] == view["job_identity"][:12]


# ---------------------------------------------------------------------------
# Status-transition compare-and-set
# ---------------------------------------------------------------------------
class TestTransitions:
    def test_full_legal_lifecycle(self, led_db):
        view, _ = _create(led_db)
        aid = view["application_id"]
        assert als.mark_gathering(USER_A, aid)["status"] == als.STATUS_GATHERING
        assert als.mark_ready(USER_A, aid)["status"] == als.STATUS_READY
        assert als.mark_submitting(USER_A, aid)["status"] == als.STATUS_SUBMITTING
        submitted = als.mark_submitted(USER_A, aid)
        assert submitted["status"] == als.STATUS_SUBMITTED
        assert submitted["submitted_at"] is not None
        confirmed = als.mark_confirmed(USER_A, aid)
        assert confirmed["status"] == als.STATUS_CONFIRMED
        assert confirmed["confirmed_at"] is not None

    def test_illegal_transition_is_409(self, led_db):
        view, _ = _create(led_db)  # queued
        with pytest.raises(als.InvalidTransition) as ei:
            als.mark_submitted(USER_A, view["application_id"])  # queued→submitted illegal
        assert ei.value.status == 409

    def test_transition_on_unknown_row_is_404(self, led_db):
        with pytest.raises(als.ApplicationNotFound) as ei:
            als.mark_gathering(USER_A, "does-not-exist")
        assert ei.value.status == 404

    def test_transition_is_user_scoped(self, led_db):
        view, _ = _create(led_db, USER_A)
        # B cannot advance A's row — it is invisible, so a 404 not a silent write.
        with pytest.raises(als.ApplicationNotFound):
            als.mark_gathering(USER_B, view["application_id"])
        # A's row is untouched.
        assert als.get_application(USER_A, view["application_id"])["status"] == als.STATUS_QUEUED

    def test_needs_input_stores_bounded_questions_and_ready_clears_them(self, led_db):
        view, _ = _create(led_db)
        aid = view["application_id"]
        als.mark_gathering(USER_A, aid)
        ni = als.mark_needs_input(
            USER_A, aid,
            [{"question": "Salary?", "kind": "value", "field_id": "f1"}],
            autonomy="ask", confidence_score=0.4)
        assert ni["status"] == als.STATUS_NEEDS_INPUT
        assert len(ni["needs_input"]) == 1
        assert ni["autonomy"] == "ask"
        ready = als.mark_ready(USER_A, aid)
        assert ready["needs_input"] == []

    def test_failed_stores_error_and_requeue_wipes_it(self, led_db):
        view, _ = _create(led_db)
        aid = view["application_id"]
        failed = als.mark_failed(USER_A, aid, "captcha wall")
        assert failed["status"] == als.STATUS_FAILED
        assert failed["error"] == "captcha wall"
        requeued = als.requeue(USER_A, aid)
        assert requeued["status"] == als.STATUS_QUEUED
        assert requeued["error"] is None

    def test_unknown_target_status_rejected(self, led_db):
        view, _ = _create(led_db)
        with pytest.raises(als.InvalidApplicationInput):
            als.transition(USER_A, view["application_id"], "teleported")


# ---------------------------------------------------------------------------
# Bulk email-loop set operations (approve / skip) — user-scoped + idempotent
# ---------------------------------------------------------------------------
class TestBulkOps:
    def test_approve_to_ready_moves_only_own_pre_submit_rows(self, led_db):
        a, _ = _create(led_db, USER_A)
        b, _ = _create(led_db, USER_B, company="Globex")
        moved = als.approve_to_ready(USER_A, [a["application_id"], b["application_id"]])
        assert moved == 1  # only A's row, and only because it is approvable
        assert als.get_application(USER_A, a["application_id"])["status"] == als.STATUS_READY
        # B's row untouched by A's link.
        assert als.get_application(USER_B, b["application_id"])["status"] == als.STATUS_QUEUED

    def test_approve_is_idempotent(self, led_db):
        a, _ = _create(led_db, USER_A)
        assert als.approve_to_ready(USER_A, [a["application_id"]]) == 1
        # Already ready → nothing more to move.
        assert als.approve_to_ready(USER_A, [a["application_id"]]) == 0
