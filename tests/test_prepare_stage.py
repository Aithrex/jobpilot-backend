"""Offline tests for the REQUIRED tailored-résumé preparation stage (Defect D).

The bug being fixed: queue/scheduler create paths never set
``tailoring_record_id`` on the ledger row, so ``_resume_ref`` returned None and
``submit`` had no résumé. The fix is a new async phase ``prepare`` that reserves
a tailor slot (reserve-before-spend, idempotent), tailors + PINS an immutable
version + rendered PDF in S3, writes ``tailoring_record_id`` back, and only THEN
lands the row in ``ready`` via the SAME mark_ready path — so a row can reach
``ready`` (and thus submit) ONLY with a real, owner-scoped artifact pinned.

Runs fully offline (mongomock; no network, no LLM, no S3). The tailor/LLM and
S3 boundaries are monkeypatched so the pin/idempotency/quota logic runs for
real against mongomock. Covers, per the plan:
  * prepare pins exactly ONE immutable version and sets ledger.tailoring_record_id;
  * redelivery (prepare twice) is a no-op: no second tailor, same artifact,
    tailor quota reserved ONCE;
  * _resume_ref returns the ref after prepare (was None before);
  * submit passes the resume ref to the executor (spy);
  * prepare failure → needs_input (NOT ready) and the reservation is refunded;
  * a row never reaches `ready` without a pinned tailoring_record_id (gather
    wiring, flag ON, dispatches prepare and leaves the row `gathering`).
"""
from types import SimpleNamespace

import pytest

from _vg_support import current_db

import services.auto_apply_service as aas
import services.application_ledger_service as ledger
import services.resume_service as rs
from services.executor_adapter import StubExecutor, SUBMIT_SUCCESS

USER = "alice@aspirely.me"

_PREP_COLLECTIONS = (
    "applications", "auto_submit_audit", "auto_apply_answers",
    "auto_apply_freezes", "tailoring_records", "quota_counters",
    "quota_ledger", "resume_jobs", "users", "user_preferences",
    "resume_assets", "feature_usage",
)


@pytest.fixture(autouse=True)
def _reset_executor():
    yield
    aas._executor = None


class _FakeStorage:
    """Stand-in for s3_service — returns the deterministic immutable v3 key
    without touching S3, so the pinned files.pdf.s3_key is asserted for real."""

    def upload_tailoring_file(self, record_id, version_id, render_version,
                              file_bytes, fmt="pdf"):
        return f"tailoring/{record_id}/{version_id}/{render_version}.{fmt}"


@pytest.fixture()
def prep_env(monkeypatch):
    """Clean DB + monkeypatched tailor/LLM + S3 boundaries around the real
    resume_service.prepare_tailored_artifact pin path."""
    d = current_db()
    for c in _PREP_COLLECTIONS:
        d[c].delete_many({})

    svc = rs.get_resume_service()
    calls = {"tailor": 0}

    def fake_ensure(user_email=None, **_kw):
        return {"structured": {"contact": {"name": "Alice"}},
                "filename": "resume.pdf", "s3_key": "base/x.pdf"}

    def fake_extract_jd(jd_text):
        return {"job_title": "Engineer", "required_skills": ["python"]}

    def fake_tailor(structured, jd_analysis, *a, **kw):
        calls["tailor"] += 1
        return {"contact": {"name": "Alice"}, "experience": []}

    def fake_pdf(tailored):
        from services.resume_renderer import FontTier, GapPlan, PageResolution
        res = PageResolution(
            tier=FontTier(10.0, 3.4, 3.08),
            gaps=GapPlan(1.2, 0.9, 1.0, 0.5, 0.4, 15.24),
            stretch=1.0, pages=1, predicted_height_mm=200.0,
            fill_ratio=0.8, dropped=[], outcome="fit_one",
        )
        return b"%PDF-1.4\n" + b"x" * 400, res

    monkeypatch.setattr(svc.parser, "ensure_structured_resume", fake_ensure)
    monkeypatch.setattr(svc, "extract_jd", fake_extract_jd)
    monkeypatch.setattr(svc.tailor, "tailor", fake_tailor)
    monkeypatch.setattr(svc.renderer, "generate_pdf", fake_pdf)

    import services.s3_service as s3
    monkeypatch.setattr(s3, "get_storage_service", lambda: _FakeStorage())

    return SimpleNamespace(db=d, calls=calls)


def _gathering_app(autonomy="auto", confidence=0.95):
    """A ledger row parked in `gathering` (the clean-auto prepare entry state),
    with the autonomy decision stashed the way gather would have."""
    view, _ = ledger.create_application(
        USER, company="Acme", title="Engineer", jd_text="Build things.",
        source="greenhouse", apply_url="https://boards.greenhouse.io/x",
        ats_family="greenhouse")
    aid = view["application_id"]
    ledger.mark_gathering(USER, aid)
    aas._persist_answers(aid, USER, {}, 1.0, False,
                         autonomy=autonomy, confidence_score=confidence)
    return aid


# ---------------------------------------------------------------------------
# prepare(): pins exactly one immutable version + sets ledger.tailoring_record_id
# ---------------------------------------------------------------------------
def test_prepare_pins_one_version_and_sets_ledger_trid(prep_env):
    aid = _gathering_app()
    res = aas.prepare(aid, USER)

    # F: an `auto` row is SYSTEM-approved through ready_for_review → approved
    # (snapshot frozen) — the pre-F `ready` landing is now `approved`.
    assert res["status"] == ledger.STATUS_APPROVED
    trid = res["tailoring_record_id"]
    assert trid
    assert res["resume_version_id"]

    # Ledger row now carries the pinned artifact + landed in `approved`.
    row = ledger.get_application(USER, aid)
    assert row["status"] == ledger.STATUS_APPROVED
    assert row["tailoring_record_id"] == trid

    # Exactly ONE immutable version with a pinned, immutable-keyed PDF.
    rec = prep_env.db.tailoring_records.find_one({"record_id": trid})
    assert rec is not None
    assert rec["user_email"] == USER
    assert len(rec["versions"]) == 1
    pdf = rec["versions"][0]["files"]["pdf"]
    assert pdf["s3_key"].startswith(f"tailoring/{trid}/")
    assert pdf["render_version"]
    assert pdf["size_bytes"] > 0

    assert prep_env.calls["tailor"] == 1


# ---------------------------------------------------------------------------
# redelivery: no second tailor, same artifact, tailor quota reserved ONCE
# ---------------------------------------------------------------------------
def test_prepare_redelivery_is_noop_single_tailor_single_reservation(prep_env):
    aid = _gathering_app()
    res1 = aas.prepare(aid, USER)
    trid = res1["tailoring_record_id"]

    res2 = aas.prepare(aid, USER)
    assert res2.get("idempotent") is True
    assert res2["tailoring_record_id"] == trid

    # No second tailor, no second record.
    assert prep_env.calls["tailor"] == 1
    assert prep_env.db.tailoring_records.count_documents({"user_email": USER}) == 1

    # Tailor quota reserved EXACTLY once (idempotency_key = application_id),
    # settled to `completed` (a real slot consumed, never double-counted).
    entries = list(prep_env.db.quota_ledger.find(
        {"user_email": USER, "feature": "tailor"}))
    assert len(entries) == 1
    assert entries[0]["state"] == "completed"


# ---------------------------------------------------------------------------
# _resume_ref: None before prepare, a real ref after
# ---------------------------------------------------------------------------
def test_resume_ref_none_before_and_real_after_prepare(prep_env):
    aid = _gathering_app()

    before = ledger.get_application(USER, aid)
    assert aas._resume_ref(before, USER) is None

    res = aas.prepare(aid, USER)
    after = ledger.get_application(USER, aid)
    ref = aas._resume_ref(after, USER)
    assert ref is not None
    assert ref["tailoring_record_id"] == res["tailoring_record_id"]
    assert ref["user_email"] == USER


# ---------------------------------------------------------------------------
# submit passes the resume ref to the executor
# ---------------------------------------------------------------------------
class _SpyExecutor(StubExecutor):
    name = "spy"

    def __init__(self):
        self.resume_pdf = "UNSET"
        self.submit_called = False

    def is_available(self):
        return True

    def submit(self, application, answers, resume_pdf):
        self.submit_called = True
        self.resume_pdf = resume_pdf
        return {"status": SUBMIT_SUCCESS, "confirmation": "OK-123"}


def test_submit_passes_resume_ref_to_executor(prep_env):
    aid = _gathering_app()
    res = aas.prepare(aid, USER)
    trid = res["tailoring_record_id"]

    spy = _SpyExecutor()
    aas.set_executor(spy)
    out = aas.submit(aid, USER)

    assert out["status"] == ledger.STATUS_SUBMITTED
    assert spy.submit_called is True
    assert spy.resume_pdf is not None
    assert spy.resume_pdf["tailoring_record_id"] == trid
    assert spy.resume_pdf["user_email"] == USER


# ---------------------------------------------------------------------------
# prepare failure → needs_input (NOT ready), reservation refunded
# ---------------------------------------------------------------------------
def test_prepare_failure_holds_needs_input_and_refunds(prep_env, monkeypatch):
    aid = _gathering_app()

    def boom(structured, jd_analysis, *a, **kw):
        raise RuntimeError("tailor blew up")

    svc = rs.get_resume_service()
    monkeypatch.setattr(svc.tailor, "tailor", boom)

    res = aas.prepare(aid, USER)
    assert res["status"] == ledger.STATUS_NEEDS_INPUT

    row = ledger.get_application(USER, aid)
    assert row["status"] != ledger.STATUS_READY
    assert row["status"] == ledger.STATUS_NEEDS_INPUT
    # No artifact was pinned on a failed tailor.
    assert row["tailoring_record_id"] is None

    # The reserved tailor slot was refunded (state flipped, counter given back).
    entries = list(prep_env.db.quota_ledger.find(
        {"user_email": USER, "feature": "tailor"}))
    assert len(entries) == 1
    assert entries[0]["state"] == "refunded"
    counter = prep_env.db.quota_counters.find_one(
        {"_id": entries[0]["counter_id"]})
    assert counter is not None and counter.get("count", 0) == 0


# ---------------------------------------------------------------------------
# a row never reaches `ready` without a pinned tailoring_record_id
# (gather wiring, flag ON, dispatches prepare and leaves the row `gathering`)
# ---------------------------------------------------------------------------
class _FakeExecutor(StubExecutor):
    """Recon returns resolvable required questions so the gather auto path runs
    end-to-end without a browser."""
    name = "fake"

    def recon_form(self, apply_url, ats_family):
        return {"questions": [
            {"id": "q_email", "text": "What is your email address?",
             "type": "text", "required": True, "is_legal_attestation": False},
            {"id": "q_phone", "text": "What is your phone number?",
             "type": "text", "required": True, "is_legal_attestation": False},
        ], "needs_account": False}


def _seed_resume_assets(d):
    d.resume_assets.insert_one({
        "resume_id": "asset-prep", "owner_type": "user", "user_email": USER,
        "structured": {"contact": {"email": "alice@aspirely.me",
                                   "phone": "+1-555-0100"}},
        "parse_status": "parsed", "parsed_at": "2026-01-01T00:00:00+00:00",
    })


def test_gather_flag_on_dispatches_prepare_and_stays_gathering(prep_env, monkeypatch):
    monkeypatch.setenv("AUTO_APPLY_PREPARE", "true")
    _seed_resume_assets(prep_env.db)

    # Capture the async dispatch instead of spawning the daemon-thread worker,
    # so the row's state is asserted deterministically (prepare is NOT run here).
    dispatched = []

    def _spy_invoke(job_id, job_type, payload):
        dispatched.append((job_type, payload.get("application_id")))

    monkeypatch.setattr(rs.ResumeService, "invoke_async", staticmethod(_spy_invoke))

    aas.set_executor(_FakeExecutor())
    view, _ = ledger.create_application(
        USER, company="Acme", title="Cloud Engineer", jd_text="Build AWS infra.",
        source="greenhouse", apply_url="https://boards.greenhouse.io/y",
        ats_family="greenhouse")
    aid = view["application_id"]

    res = aas.gather(aid, USER)

    # Clean auto, but NOT ready — a tailored artifact is not pinned yet.
    assert res["autonomy"] == "auto"
    assert res["prepare_dispatched"] is True
    assert res["status"] == ledger.STATUS_GATHERING

    row = ledger.get_application(USER, aid)
    assert row["status"] != ledger.STATUS_READY
    assert row["status"] == ledger.STATUS_GATHERING
    assert row["tailoring_record_id"] is None

    # prepare WAS dispatched for exactly this row.
    assert dispatched == [("auto_apply_prepare", aid)]


def test_gather_flag_off_keeps_todays_direct_ready_path(prep_env, monkeypatch):
    # Flag default OFF → no behavior change: gather still lands `ready` directly.
    monkeypatch.delenv("AUTO_APPLY_PREPARE", raising=False)
    _seed_resume_assets(prep_env.db)
    aas.set_executor(_FakeExecutor())
    view, _ = ledger.create_application(
        USER, company="Acme", title="Cloud Engineer", jd_text="Build AWS infra.",
        source="greenhouse", apply_url="https://boards.greenhouse.io/z",
        ats_family="greenhouse")
    aid = view["application_id"]

    res = aas.gather(aid, USER)
    # F: the clean-auto gather path now lands ready_for_review → system-approved →
    # `approved` (was the pre-F direct `ready`); prepare stays dark (flag off).
    assert res["status"] == ledger.STATUS_APPROVED
    assert res["prepare_dispatched"] is False
