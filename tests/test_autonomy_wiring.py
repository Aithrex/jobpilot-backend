"""Offline tests for auto-apply autonomy wiring (Defect C).

The bug being fixed: `auto_apply_service._decide_autonomy` called
`autonomy_service.score(**signals)` / `decide(score, **signals)` with the WRONG
signature, the resulting TypeError was swallowed, and every application fell to
a hardcoded local threshold of 0.90 — the per-user `autonomy_threshold` was
never consulted. The fix delegates to `autonomy_service.evaluate(...)` with the
correct kwargs, consults the real per-user threshold, and keeps the local policy
ONLY for a genuine ImportError (parallel-build safety) — never for a signature
error, which must now surface.

Runs fully offline (mongomock; no network, no LLM).
"""
import builtins

import pytest

from _vg_support import current_db

import services.autonomy_service as auto
import services.auto_apply_service as aas
import services.application_ledger_service as ledger
from services.executor_adapter import StubExecutor

USER = "alice@aspirely.me"

_EMAIL_Q = "What is your email address?"
_PHONE_Q = "What is your phone number?"
_WORK_AUTH_Q = "Are you legally authorized to work in the United States?"


@pytest.fixture()
def wiring_db():
    d = current_db()
    for c in ("application_answer_library", "resume_assets", "users",
              "user_preferences", "visa_profiles", "applications",
              "auto_submit_audit", "auto_apply_answers", "auto_apply_freezes",
              "auto_submit_settings"):
        d[c].delete_many({})
    return d


@pytest.fixture(autouse=True)
def _reset_executor():
    yield
    aas._executor = None


def _seed_resume(d, structured):
    d.resume_assets.insert_one({
        "resume_id": "asset-wiring", "owner_type": "user",
        "user_email": USER, "structured": structured,
        "parse_status": "parsed", "parsed_at": "2026-01-01T00:00:00+00:00",
    })


def _recon_q(id, text, *, required=False, is_legal=False, type="text"):
    return {"id": id, "text": text, "type": type,
            "required": required, "is_legal_attestation": is_legal}


class _FakeExecutor(StubExecutor):
    name = "fake"

    def __init__(self, questions, needs_account=False):
        self._questions = questions
        self._needs_account = needs_account

    def recon_form(self, apply_url, ats_family):
        return {"questions": self._questions, "needs_account": self._needs_account}


def _make_app(ats_family="greenhouse"):
    view, _ = ledger.create_application(
        USER, company="Acme", title="Engineer", jd_text="Build things.",
        source="test", apply_url="https://boards.greenhouse.io/x",
        ats_family=ats_family)
    return view["application_id"]


def _resolvable_recon():
    return [_recon_q("q_email", _EMAIL_Q, required=True),
            _recon_q("q_phone", _PHONE_Q, required=True)]


# ---------------------------------------------------------------------------
# The real policy is consulted (not the hardcoded 0.90)
# ---------------------------------------------------------------------------
class TestRealPolicyWired:
    def test_user_threshold_of_one_never_auto(self, wiring_db):
        _seed_resume(wiring_db, {"contact": {
            "email": "alice@aspirely.me", "phone": "+1-555-0100"}})
        # "review everything": a threshold of 1.0 nothing can score above.
        auto.set_autonomy_threshold(USER, 1.0)
        aas.set_executor(_FakeExecutor(_resolvable_recon()))
        app_id = _make_app()
        res = aas.gather(app_id, USER)
        # Full coverage on greenhouse scores ~0.985 — auto under the default
        # 0.90, but NEVER auto under the user's own 1.0 threshold.
        assert res["autonomy"] != "auto"
        # F: a clean (no-gaps) non-auto decision now WAITS in ready_for_review for
        # a manual approval — the "review everything" intent — instead of the pre-F
        # needs_input (which is reserved for actual gaps / Oracle account walls).
        assert res["status"] == ledger.STATUS_READY_FOR_REVIEW

    def test_get_autonomy_threshold_is_actually_consulted(self, wiring_db, monkeypatch):
        _seed_resume(wiring_db, {"contact": {
            "email": "alice@aspirely.me", "phone": "+1-555-0100"}})
        calls = {"n": 0}
        real = auto.get_autonomy_threshold

        def spy(user_email):
            calls["n"] += 1
            return real(user_email)

        # evaluate() looks the function up from the module globals at call time,
        # so patching the attribute intercepts the real read.
        monkeypatch.setattr(auto, "get_autonomy_threshold", spy)
        aas.set_executor(_FakeExecutor(_resolvable_recon()))
        aas.gather(_make_app(), USER)
        assert calls["n"] >= 1

    def test_evaluate_called_with_correct_kwargs(self, wiring_db, monkeypatch):
        _seed_resume(wiring_db, {"contact": {
            "email": "alice@aspirely.me", "phone": "+1-555-0100"}})
        seen = []

        def spy_evaluate(user_email, application, **kwargs):
            seen.append((user_email, application, kwargs))
            return {"confidence_score": 0.99, "autonomy": "auto", "threshold": 0.9}

        monkeypatch.setattr(auto, "evaluate", spy_evaluate)
        aas.set_executor(_FakeExecutor(_resolvable_recon()))
        aas.gather(_make_app(), USER)

        assert len(seen) == 1
        ue, app_arg, kw = seen[0]
        assert ue == USER
        assert kw["answer_coverage"] == 1.0
        assert kw["ats_family"] == "greenhouse"
        assert kw["has_novel_free_text"] is False
        assert kw["has_unconfirmed_legal_attestation"] is False
        assert kw["blocked_on_oracle"] is False


# ---------------------------------------------------------------------------
# Hard rules through the real evaluate()
# ---------------------------------------------------------------------------
class TestHardRulesThroughGather:
    def test_unresolved_legal_asks_regardless_of_score(self, wiring_db):
        _seed_resume(wiring_db, {"contact": {"email": "alice@aspirely.me"}})
        # a required, undeclared legal attestation among otherwise-fillable fields
        aas.set_executor(_FakeExecutor([
            _recon_q("q_email", _EMAIL_Q, required=True),
            _recon_q("q_auth", _WORK_AUTH_Q, required=True, is_legal=True),
        ]))
        res = aas.gather(_make_app(), USER)
        assert res["autonomy"] == "ask"
        assert res["status"] == ledger.STATUS_NEEDS_INPUT

    def test_needs_account_holds(self, wiring_db):
        _seed_resume(wiring_db, {"contact": {
            "email": "alice@aspirely.me", "phone": "+1-555-0100"}})
        aas.set_executor(_FakeExecutor(_resolvable_recon(), needs_account=True))
        res = aas.gather(_make_app(), USER)
        assert res["autonomy"] == "hold"
        assert res["status"] == ledger.STATUS_NEEDS_INPUT


# ---------------------------------------------------------------------------
# Fallback boundary: ImportError yes, signature error NO
# ---------------------------------------------------------------------------
class TestFallbackBoundary:
    def test_import_error_still_yields_valid_decision(self, wiring_db, monkeypatch):
        real_import = builtins.__import__

        def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "services" and "autonomy_service" in (fromlist or ()):
                raise ImportError("simulated parallel-build gap")
            return real_import(name, globals, locals, fromlist, level)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        autonomy, confidence = aas._decide_autonomy(
            {"ats_family": "greenhouse"}, 1.0,
            user_email=USER, has_novel_free_text=False,
            has_unresolved_legal=False, needs_account=False)
        assert autonomy in ledger._VALID_AUTONOMY
        assert 0.0 <= confidence <= 1.0

    def test_wrong_signature_now_raises_not_swallowed(self, wiring_db, monkeypatch):
        _seed_resume(wiring_db, {"contact": {
            "email": "alice@aspirely.me", "phone": "+1-555-0100"}})

        def bad_evaluate(*args, **kwargs):
            raise TypeError(
                "evaluate() got an unexpected keyword argument 'signals'")

        monkeypatch.setattr(auto, "evaluate", bad_evaluate)
        aas.set_executor(_FakeExecutor(_resolvable_recon()))
        app_id = _make_app()
        # The swallow-into-fallback is gone: a real signature bug surfaces
        # (gather marks the row failed and re-raises) instead of silently
        # decaying to the hardcoded 0.90 policy.
        with pytest.raises(TypeError):
            aas.gather(app_id, USER)

    def test_local_fallback_decisions_are_valid(self):
        for cov in (0.0, 0.4, 0.6, 1.0):
            d, s = aas._local_autonomy(
                "greenhouse", cov, has_novel_free_text=False,
                has_unresolved_legal=False, needs_account=False)
            assert d in ledger._VALID_AUTONOMY
            assert 0.0 <= s <= 1.0
        # hard rules still fire in the fallback.
        assert aas._local_autonomy(
            "greenhouse", 1.0, has_novel_free_text=False,
            has_unresolved_legal=True, needs_account=False)[0] == "ask"
        assert aas._local_autonomy(
            "greenhouse", 1.0, has_novel_free_text=False,
            has_unresolved_legal=False, needs_account=True)[0] == "hold"
