"""E3 (defect E) — the canonical-preferred DUAL-READ + the backfill migration.

Proves the reader cutover is SAFE:

  * reader PARITY — a record whose outcome lives ONLY in tailoring_records
    (legacy) and an equivalent record whose outcome lives ONLY in the canonical
    `applications` row produce the SAME funnel output (dual-read parity).
  * canonical PREFERRED — when BOTH a tailoring_records status and a canonical
    outcome exist and DIFFER, the canonical value WINS (both directions).
  * the same flip in job_intelligence_service.get_funnel_analytics (the resume
    local-package board reader), with the LLM insight call stubbed.
  * the backfill migration projects legacy tailoring_records + auto_submit_approvals
    onto canonical rows, and RE-RUNNING is a no-op (idempotent).

The char goldens (tests/test_char_kanban_read.py) seed tailoring_records ONLY,
so their empty-canonical fallback path stays byte-identical — they are NOT edited.

Runs fully offline (mongomock; no Bedrock/network).
"""
import importlib.util
from datetime import datetime, timezone

import pytest

from _vg_support import current_db, REPO_ROOT

import services.application_ledger_service as als
import services.pipeline_intel_service as pintel

USER = "alice@aspirely.me"
USER2 = "carol@aspirely.me"


@pytest.fixture()
def db():
    d = current_db()
    for c in ("applications", "tailoring_records", "auto_submit_audit",
              "auto_submit_approvals"):
        d[c].delete_many({})
    return d


def _seed_record(db, record_id, status, *, user=USER, company="Acme", title="Engineer"):
    db.tailoring_records.insert_one({
        "record_id": record_id,
        "user_email": user,
        "jd_analysis": {"company": company, "job_title": title},
        "application": {"status": status},
        "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
    })


# ---------------------------------------------------------------------------
# outcome_view — the two-axis -> coarse funnel-bucket collapse
# ---------------------------------------------------------------------------
class TestOutcomeView:
    def test_outcome_axis_wins_when_present(self):
        assert als.outcome_view({"outcome": "offer", "status": "external"}) == "offer"
        assert als.outcome_view({"outcome": "rejected", "status": "external"}) == "rejected"
        assert als.outcome_view({"outcome": "viewed", "status": "external"}) == "applied"
        assert als.outcome_view({"outcome": "screening", "status": "external"}) == "interviewing"

    def test_no_employer_response_falls_back_to_execution_status(self):
        # outcome none but JobPilot DID submit -> counts as applied.
        assert als.outcome_view({"outcome": "none", "status": als.STATUS_SUBMITTED}) == "applied"
        assert als.outcome_view({"outcome": "none", "status": als.STATUS_CONFIRMED}) == "applied"
        # legacy `submitting` peer normalizes but is pre-submit -> draft.
        assert als.outcome_view({"outcome": "none", "status": als.STATUS_QUEUED}) == "draft"

    def test_tolerant_of_junk(self):
        assert als.outcome_view(None) == "draft"
        assert als.outcome_view({}) == "draft"
        assert als.outcome_view({"outcome": "bogus", "status": "bogus"}) == "draft"


# ---------------------------------------------------------------------------
# Reader parity — legacy-only vs canonical-only produce identical funnels
# ---------------------------------------------------------------------------
class TestFunnelParity:
    def test_legacy_only_and_canonical_only_are_identical(self, db):
        # USER: the signal lives ONLY in tailoring_records (today's behavior).
        _seed_record(db, "rec-A", "offer", user=USER)

        # USER2: an equivalent record whose status is a bare draft in
        # tailoring_records, but whose OUTCOME lives ONLY on the canonical row.
        _seed_record(db, "rec-B", "draft", user=USER2)
        als.set_outcome(USER2, record_id="rec-B", outcome="offer", source="mail_ingress")

        out_legacy = pintel.funnel(USER)
        out_canonical = pintel.funnel(USER2)
        assert out_legacy == out_canonical
        # Both reached offer (cumulative), both track exactly one record.
        assert out_legacy["totals"] == {
            "tracked": 1, "applied": 1, "interview": 1, "offer": 1}


# ---------------------------------------------------------------------------
# Canonical preferred — when both exist and differ, canonical wins
# ---------------------------------------------------------------------------
class TestCanonicalPreferred:
    def test_canonical_upgrades_over_legacy(self, db):
        # Legacy says merely "applied"; the canonical outcome says "offer".
        _seed_record(db, "rec-1", "applied", user=USER)
        als.set_outcome(USER, record_id="rec-1", outcome="offer", source="mail_ingress")

        out = pintel.funnel(USER)
        # Canonical offer WINS -> the row reaches offer (cumulative applied+iv+offer).
        assert out["totals"] == {
            "tracked": 1, "applied": 1, "interview": 1, "offer": 1}

    def test_canonical_downgrades_over_legacy(self, db):
        # Legacy says "offer"; the canonical outcome says only "applied".
        _seed_record(db, "rec-1", "offer", user=USER)
        als.set_outcome(USER, record_id="rec-1", outcome="applied", source="mail_ingress")

        out = pintel.funnel(USER)
        # Canonical applied WINS -> offer + interview drop to 0, applied stays 1.
        assert out["totals"] == {
            "tracked": 1, "applied": 1, "interview": 0, "offer": 0}

    def test_no_canonical_row_falls_back_to_legacy(self, db):
        # No canonical row for this record -> today's tailoring_records value.
        _seed_record(db, "rec-1", "interviewing", user=USER)
        out = pintel.funnel(USER)
        assert out["totals"]["interview"] == 1


# ---------------------------------------------------------------------------
# job_intelligence_service.get_funnel_analytics — the resume-local board reader
# ---------------------------------------------------------------------------
def _load_job_intel():
    """Load the resume local-package reader by file path (avoids namespace
    __path__ caching issues) so we can stub its bound claude_json."""
    path = REPO_ROOT / "services" / "resume" / "services" / "job_intelligence_service.py"
    spec = importlib.util.spec_from_file_location("_ji_dualread", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestJobIntelDualRead:
    def test_get_funnel_analytics_prefers_canonical(self, db, monkeypatch):
        ji = _load_job_intel()
        # Stub the LLM insight call (get_funnel_analytics always calls it).
        monkeypatch.setattr(ji, "claude_json", lambda **kw: {
            "headline": "h", "insight": "i", "action_items": ["a", "b", "c"]})

        _seed_record(db, "rec-1", "applied", user=USER)
        als.set_outcome(USER, record_id="rec-1", outcome="rejected", source="mail_ingress")

        out = ji.get_funnel_analytics(USER)
        counts = out["counts"]
        # Canonical rejected WINS over the legacy `applied`.
        assert counts["rejected"] == 1
        assert counts["applied"] == 0


# ---------------------------------------------------------------------------
# Backfill migration — projects legacy state; re-run is a no-op
# ---------------------------------------------------------------------------
def _load_backfill():
    path = REPO_ROOT / "scripts" / "backfill_applications_canonical.py"
    spec = importlib.util.spec_from_file_location("_backfill_canonical", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestBackfill:
    def test_outcome_projection_and_idempotent_rerun(self, db):
        backfill = _load_backfill()
        _seed_record(db, "rec-1", "applied", user=USER)
        _seed_record(db, "rec-2", "rejected", user=USER)
        _seed_record(db, "rec-3", "draft", user=USER)   # skipped (no funnel signal)

        projected, skipped = backfill.backfill_outcomes(db, als, dry_run=False)
        assert projected == 2 and skipped == 1

        rows = {r["tailoring_record_id"]: r for r in
                db.applications.find({"user_email": USER}, {"_id": 0})}
        assert set(rows) == {"rec-1", "rec-2"}
        assert rows["rec-1"]["outcome"] == "applied"
        assert rows["rec-2"]["outcome"] == "rejected"
        assert rows["rec-1"]["outcome_source"] == "backfill"

        # Re-run: still 2 canonical rows (no duplicates) — idempotent.
        projected2, _ = backfill.backfill_outcomes(db, als, dry_run=False)
        assert projected2 == 2
        assert db.applications.count_documents({"user_email": USER}) == 2

    def test_clean_db_is_a_noop(self, db):
        backfill = _load_backfill()
        projected, skipped = backfill.backfill_outcomes(db, als, dry_run=False)
        assert projected == 0
        assert db.applications.count_documents({}) == 0

    def test_approval_fold_projection_and_idempotent(self, db):
        backfill = _load_backfill()
        # A canonical row driven to ready_for_review (the F approvable state).
        view, _ = als.create_application(
            USER, company="Acme", title="Engineer",
            jd_text="Backend engineer building distributed systems.",
            source="greenhouse", apply_url="https://x/y")
        aid = view["application_id"]
        als.mark_gathering(USER, aid)
        als.mark_ready_for_review(USER, aid, autonomy="auto", confidence_score=0.9)
        # A live legacy approval doc pointing at that ledger row.
        db.auto_submit_approvals.insert_one({
            "approval_id": "ap-1", "user_email": USER,
            "application_id": aid, "status": "approved"})

        folded, _ = backfill.backfill_approvals(db, als, dry_run=False)
        assert folded == 1
        assert als.get_application(USER, aid)["status"] == als.STATUS_APPROVED

        # Re-run: already approved -> nothing folded (idempotent), row unchanged.
        folded2, _ = backfill.backfill_approvals(db, als, dry_run=False)
        assert folded2 == 0
        assert als.get_application(USER, aid)["status"] == als.STATUS_APPROVED
