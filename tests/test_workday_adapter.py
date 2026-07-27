"""Workday ATS adapter tests (auto-apply — real Workday adapter, replaces the stub).

HARNESS CHOICE (same as test_ats_adapters.py): these drive the deterministic
WorkdayAdapter against static Aithrex-owned HTML fixtures via the lightweight
lxml-backed headless-DOM harness (``tests/ats_dom``), NOT a live browser — so the
suite runs offline with no Chromium and never self-skips. The adapter is
page-agnostic; the same code drives a real Playwright page in prod.

Workday is a per-tenant, THEMED, MULTI-STEP wizard behind a candidate-ACCOUNT gate,
so EVERYTHING keys off ``data-automation-id`` (Workday's stable, theme-independent
hook), never CSS classes. Covered here:

  * detection by host + data-automation-id across TWO tenant themes (theme-agnostic),
    and by DOM signature alone (host-less);
  * canonical_job_identity parses tenant + job req id (deterministic);
  * required_account is a hard TRUE; the on-screen account gate → waiting_human with
    a clear pending_action (never auto-creates an account, never clicks through);
  * multi-step recon per step (My Information / Application Questions / Voluntary
    Disclosures EEO / Self-Identify CC-305) mapped to bank field_ids, with the
    EEO/disability fields flagged protected/legal;
  * the sponsorship question normalizes with the correct INVERT polarity;
  * deterministic data-automation-id fill; a missing automation id → needs_input
    (never silently recorded);
  * the submit boundary → submission_unknown on ambiguity (never retried) and
    submitted on the confirmation page;
  * selector drift (a renamed automation id) opens the circuit breaker + flips health;
  * supported WITH the honest caveat (multi-step + account-gated), not full autonomy.
"""
import pathlib

import pytest

from ats_dom import FakePage
from services.ats_adapters import (
    WorkdayAdapter, select_adapter, detect_adapter,
    reset_circuit_breakers, get_circuit_breaker,
    ADAPTER_SUBMITTED, ADAPTER_SUBMISSION_UNKNOWN, ADAPTER_WAITING_HUMAN,
    HEALTH_OPEN, HEALTH_HEALTHY,
    TAKEOVER_ACCOUNT_REQUIRED, TAKEOVER_FORM_CHANGED,
)

_FIX = pathlib.Path(__file__).parent / "fixtures" / "ats"

# One representative Workday apply URL (tenant + wd pod + job path).
_WD_URL = "https://aithrex.wd1.myworkdayjobs.com/en-US/careers/job/1234"


def _page(name: str, next_name: str = None, url: str = _WD_URL) -> FakePage:
    nxt = (_FIX / (next_name + ".html")) if next_name else None
    return FakePage.from_file(_FIX / (name + ".html"), url=url, next_file=nxt)


@pytest.fixture(autouse=True)
def _reset_breakers():
    """Circuit breakers are per-family module-global fleet state — clear them so
    every test is order-independent."""
    reset_circuit_breakers()
    yield
    reset_circuit_breakers()


# ---------------------------------------------------------------------------
# Detection — host + data-automation-id, theme-independent
# ---------------------------------------------------------------------------
def test_detect_by_host_and_automation_id():
    det = WorkdayAdapter.sniff(_page("workday_my_information"), _WD_URL)
    assert det["ats"] == "workday"
    assert det["confidence"] >= 0.9
    assert any(e.startswith("host:") for e in det["evidence"])
    assert "dom:workday-automation-id" in det["evidence"]


def test_detection_is_theme_independent():
    # Two tenant themes render DIFFERENT CSS but the SAME data-automation-id hooks.
    a = WorkdayAdapter.sniff(_page("workday_my_information"), _WD_URL)
    b = WorkdayAdapter.sniff(_page("workday_my_information_themeb"), _WD_URL)
    assert a["confidence"] == b["confidence"]
    assert "dom:workday-automation-id" in a["evidence"]
    assert "dom:workday-automation-id" in b["evidence"]
    # And the form fingerprint (recon structure) is identical across themes.
    fp_a = WorkdayAdapter(_page("workday_my_information"), _WD_URL).form_fingerprint()
    fp_b = WorkdayAdapter(_page("workday_my_information_themeb"), _WD_URL).form_fingerprint()
    assert fp_a == fp_b


def test_detect_by_dom_signature_without_host():
    # Host-less (e.g. an embedded/proxied apply frame) still clears threshold on the
    # data-automation-id DOM signature alone.
    det = WorkdayAdapter.sniff(_page("workday_my_information", url=""), "")
    assert det["confidence"] >= WorkdayAdapter.MIN_CONFIDENCE
    assert "dom:workday-automation-id" in det["evidence"]
    assert not any(e.startswith("host:") for e in det["evidence"])


def test_select_adapter_routes_to_workday():
    adapter = select_adapter(_page("workday_my_information"), _WD_URL)
    assert isinstance(adapter, WorkdayAdapter)
    assert detect_adapter(_page("workday_my_information"), _WD_URL)["adapter_cls"] is WorkdayAdapter


def test_canonical_job_identity_parses_tenant_and_req_id():
    wd = WorkdayAdapter(_page("workday_my_information"), _WD_URL)
    assert wd.canonical_job_identity() == "workday:aithrex:1234"
    assert wd.canonical_job_identity() == wd.canonical_job_identity()  # deterministic
    # Title_REQID trailing-segment form.
    url2 = ("https://aithrex.wd5.myworkdayjobs.com/en-US/careers/job/"
            "San-Francisco/Senior-Backend-Engineer_R-2026-4200")
    wd2 = WorkdayAdapter(_page("workday_my_information", url=url2), url2)
    assert wd2.canonical_job_identity() == "workday:aithrex:R-2026-4200"


# ---------------------------------------------------------------------------
# required_account + on-screen account gate → waiting_human
# ---------------------------------------------------------------------------
def test_required_account_is_true():
    wd = WorkdayAdapter(_page("workday_my_information"), _WD_URL)
    assert wd.required_account() is True


def test_account_gate_pauses_for_human_with_pending_action():
    wd = WorkdayAdapter(_page("workday_account_required"), _WD_URL)
    assert wd.required_account() is True
    res = wd.submit()
    assert res["status"] == ADAPTER_WAITING_HUMAN
    assert res["takeover"]["reason"] == TAKEOVER_ACCOUNT_REQUIRED
    pa = res["pending_action"]
    assert pa["action"] == "create_or_sign_in_candidate_account"
    assert pa["never_auto_create_account"] is True
    # Never clicked through the account wall.
    assert wd._submit_clicks == 0


def test_supported_reason_recommends_takeover_on_account_gate():
    wd = WorkdayAdapter(_page("workday_account_required"), _WD_URL)
    sr = wd.supported_reason()
    assert sr["supported"] is True                      # family is supported
    assert sr["recommended_action"] == "human_takeover"  # but THIS page needs a human
    assert "caveat" in sr


# ---------------------------------------------------------------------------
# Multi-step recon → bank field_ids + wizard position
# ---------------------------------------------------------------------------
def test_my_information_step_recon_and_wizard_position():
    wd = WorkdayAdapter(_page("workday_my_information"), _WD_URL)
    qs = wd.recon()
    by_fid = {q["field_id"]: q for q in qs}
    for q in qs:
        assert {"field_id", "label", "type", "required", "is_legal_attestation",
                "recon_id"} <= set(q)
    assert {"first_name", "last_name", "email", "phone"} <= set(by_fid)
    # Résumé file field is executor-supplied → excluded.
    assert "resume" not in {q["recon_id"] for q in qs}
    # recon_id is the data-automation-id (theme-independent fill-back handle).
    # Workday often wraps inputs in formField-* containers — prefer that when present.
    assert by_fid["email"]["recon_id"] in ("email", "formField-email")
    assert wd.current_step() == "my_information"
    assert wd.has_next_step() is True


def test_application_questions_step_sponsorship_invert_polarity():
    wd = WorkdayAdapter(_page("workday_application_questions"), _WD_URL)
    by_fid = {q["field_id"]: q for q in wd.recon()}
    assert wd.current_step() == "application_questions"
    # Sponsorship phrased in the inverse ("without requiring sponsorship") → the
    # normalizer flips polarity so a naive Yes/No is not submitted backwards.
    spons = by_fid["requires_sponsorship"]
    assert spons["invert"] is True
    assert spons["is_legal_attestation"] is True
    # A plain screening field is neither inverted nor legal.
    yrs = by_fid["years_experience"]
    assert yrs["invert"] is False
    assert yrs["is_legal_attestation"] is False


def test_voluntary_disclosures_eeo_fields_flagged_protected_legal():
    wd = WorkdayAdapter(_page("workday_voluntary_disclosures"), _WD_URL)
    by_fid = {q["field_id"]: q for q in wd.recon()}
    assert wd.current_step() == "voluntary_disclosures"
    for fid in ("gender", "race_ethnicity", "veteran_status"):
        assert fid in by_fid, fid
        assert by_fid[fid]["is_legal_attestation"] is True


def test_self_identify_disability_flagged_protected_legal():
    wd = WorkdayAdapter(_page("workday_self_identify"), _WD_URL)
    by_fid = {q["field_id"]: q for q in wd.recon()}
    assert wd.current_step() == "self_identify"
    assert by_fid["disability_status"]["is_legal_attestation"] is True


# ---------------------------------------------------------------------------
# Deterministic fill keyed by data-automation-id
# ---------------------------------------------------------------------------
def test_fill_is_deterministic_by_automation_id():
    wd = WorkdayAdapter(_page("workday_my_information"), _WD_URL)
    wd.recon()
    r = wd.fill("email", "ada@example.com")
    assert r["found"] is True and r["status"] == "filled"
    assert r["handle"] in ("email", "formField-email")
    snap = wd.review_snapshot()
    assert snap["values"]["email"] == "ada@example.com"
    # A custom dropdown fills deterministically by its automation id too.
    wd2 = WorkdayAdapter(_page("workday_application_questions"), _WD_URL)
    wd2.recon()
    d = wd2.fill("requires_sponsorship", "No")
    assert d["found"] is True and d["widget"] == "dropdown"
    assert wd2.review_snapshot()["values"]["requires_sponsorship"] == "No"


def test_missing_automation_id_is_not_silently_recorded():
    wd = WorkdayAdapter(_page("workday_my_information"), _WD_URL)
    wd.recon()
    r = wd.fill("nonexistent_automation_id", "oops")
    assert r["found"] is False
    assert r["status"] == "needs_input"
    # It must NOT appear in the snapshot — no wrong-but-plausible value written.
    assert "nonexistent_automation_id" not in wd.review_snapshot()["values"]


# ---------------------------------------------------------------------------
# Submit boundary — review step is terminal
# ---------------------------------------------------------------------------
def test_review_step_is_terminal():
    wd = WorkdayAdapter(_page("workday_review"), _WD_URL)
    wd.recon()
    assert wd.current_step() == "review"
    assert wd.has_next_step() is False


def test_submit_success_on_confirmation_transition():
    page = _page("workday_review", next_name="workday_confirmation")
    wd = WorkdayAdapter(page, _WD_URL)
    wd.recon()
    res = wd.submit()
    assert res["status"] == ADAPTER_SUBMITTED
    assert res["confirmation"]["confirmed"] is True
    assert res["receipt"]["confirmed"] is True
    assert page.clicks == 1


def test_submit_ambiguous_is_submission_unknown_and_never_retries():
    page = _page("workday_review", next_name="workday_ambiguous")
    wd = WorkdayAdapter(page, _WD_URL)
    wd.recon()
    res = wd.submit()
    assert res["status"] == ADAPTER_SUBMISSION_UNKNOWN
    assert res["receipt"]["confirmed"] is False
    # Clicked submit EXACTLY once — an ambiguous result is never auto-retried.
    assert page.clicks == 1
    assert wd._submit_clicks == 1


def test_detect_confirmation_on_confirmation_fixture():
    wd = WorkdayAdapter(_page("workday_confirmation"), _WD_URL)
    conf = wd.detect_confirmation()
    assert conf["confirmed"] is True and conf["evidence"]


# ---------------------------------------------------------------------------
# Selector drift → circuit breaker + health
# ---------------------------------------------------------------------------
def test_selector_drift_opens_circuit_and_flips_health():
    baseline = WorkdayAdapter(_page("workday_my_information"), _WD_URL).form_fingerprint()

    healthy = WorkdayAdapter(_page("workday_my_information"), _WD_URL,
                             baseline_fingerprint=baseline)
    healthy.recon()
    assert healthy.health()["status"] == HEALTH_HEALTHY
    assert healthy.health()["circuit_open"] is False

    # The template silently renames an automation id. Repeated recon against the
    # pinned baseline trips the breaker OPEN (threshold = 3).
    last = None
    for _ in range(3):
        last = WorkdayAdapter(_page("workday_my_information_drift"), _WD_URL,
                              baseline_fingerprint=baseline)
        last.recon()
    assert get_circuit_breaker("workday").is_open() is True
    assert last.health()["status"] == HEALTH_OPEN
    assert last.health()["circuit_open"] is True

    sr = last.supported_reason()
    assert sr["supported"] is False and "circuit_open" in sr["reason"]
    res = last.submit()
    assert res["status"] == ADAPTER_WAITING_HUMAN
    assert res["takeover"]["reason"] == TAKEOVER_FORM_CHANGED


# ---------------------------------------------------------------------------
# Evidence-based support verdict WITH the honest caveat
# ---------------------------------------------------------------------------
def test_supported_with_honest_caveat_not_full_autonomy():
    wd = WorkdayAdapter(_page("workday_my_information"), _WD_URL)
    sr = wd.supported_reason()
    assert sr["supported"] is True
    assert "workday" in sr["reason"].lower()
    assert "caveat" in sr
    # The caveat is explicit that this is not full autonomy.
    assert "human takeover" in sr["caveat"].lower()
    assert wd.health()["status"] == HEALTH_HEALTHY
