"""Deterministic ATS adapter framework tests (auto-apply defect P4).

HARNESS CHOICE (noted): these drive the adapters against static Aithrex-owned HTML
fixtures via a lightweight lxml-backed headless-DOM harness (``tests/ats_dom``),
NOT a live browser — so the suite runs offline with no Chromium and never
self-skips (unlike test_browser_executor.py, which needs real Playwright). The
adapters are page-agnostic; the same code drives a real Playwright page in prod.

Covers: per-ATS detection confidence + evidence, canonical identity, deterministic
fill, stable review-snapshot hash, normalized recon (field_id shape + legal flag),
validation capture, the submit boundary (submission_unknown on ambiguity, never
retries; submitted on confirmation), confirmation detection, selector-drift opening
the circuit breaker + flipping health, the generic fallback defaulting to review,
account-required → waiting_human, CAPTCHA → waiting_human (never solved), and the
Workday registered-but-unsupported stub + the LinkedIn policy refusal.
"""
import pathlib

import pytest

from ats_dom import FakePage
from services.ats_adapters import (
    AtsAdapter, GreenhouseAdapter, LeverAdapter, AshbyAdapter, WorkdayAdapter,
    GenericAdapter, LinkedInAdapter,
    detect_adapter, select_adapter, reset_circuit_breakers, get_circuit_breaker,
    ADAPTER_SUBMITTED, ADAPTER_SUBMISSION_UNKNOWN, ADAPTER_WAITING_HUMAN,
    ADAPTER_VALIDATION_FAILED,
    HEALTH_OPEN, HEALTH_HEALTHY,
    TAKEOVER_ACCOUNT_REQUIRED, TAKEOVER_CAPTCHA, TAKEOVER_FORM_CHANGED,
    TAKEOVER_UNSUPPORTED,
)

_FIX = pathlib.Path(__file__).parent / "fixtures" / "ats"

_URLS = {
    "greenhouse_form": "https://boards.greenhouse.io/aithrex/jobs/4200",
    "greenhouse_drift": "https://boards.greenhouse.io/aithrex/jobs/4200",
    "greenhouse_captcha": "https://boards.greenhouse.io/aithrex/jobs/4200",
    "account_required": "https://boards.greenhouse.io/aithrex/jobs/4200",
    "validation_failure": "https://boards.greenhouse.io/aithrex/jobs/4200",
    "lever_form": "https://jobs.lever.co/aithrex/aabbccddeeff0011/apply",
    "ashby_form": "https://jobs.ashbyhq.com/aithrex/1234abcd-5678-90ef-1234/application",
    "generic_form": "https://careers.example-co.test/careers/apply/software-engineer",
    "confirmation": "https://boards.greenhouse.io/aithrex/jobs/4200",
    "ambiguous": "https://boards.greenhouse.io/aithrex/jobs/4200",
}


def _page(name: str, next_name: str = None) -> FakePage:
    nxt = (_FIX / (next_name + ".html")) if next_name else None
    return FakePage.from_file(_FIX / (name + ".html"), url=_URLS.get(name, ""),
                              next_file=nxt)


@pytest.fixture(autouse=True)
def _reset_breakers():
    """Circuit breakers are a per-family module-global fleet-state — clear them so
    every test is order-independent."""
    reset_circuit_breakers()
    yield
    reset_circuit_breakers()


# ---------------------------------------------------------------------------
# Detection confidence + evidence, per ATS
# ---------------------------------------------------------------------------
def test_detect_greenhouse_high_confidence():
    det = GreenhouseAdapter.sniff(_page("greenhouse_form"), _URLS["greenhouse_form"])
    assert det["ats"] == "greenhouse"
    assert det["confidence"] >= 0.9
    assert any(e.startswith("host:") for e in det["evidence"])
    assert any(e.startswith("dom:") for e in det["evidence"])


def test_detect_lever_high_confidence():
    det = LeverAdapter.sniff(_page("lever_form"), _URLS["lever_form"])
    assert det["ats"] == "lever" and det["confidence"] >= 0.9


def test_detect_ashby_high_confidence():
    det = AshbyAdapter.sniff(_page("ashby_form"), _URLS["ashby_form"])
    assert det["ats"] == "ashby" and det["confidence"] >= 0.9


def test_greenhouse_signals_do_not_fire_on_lever_page():
    det = GreenhouseAdapter.sniff(_page("lever_form"), _URLS["lever_form"])
    assert det["confidence"] < GreenhouseAdapter.MIN_CONFIDENCE


def test_select_adapter_routes_each_family():
    assert isinstance(select_adapter(_page("greenhouse_form"),
                                     _URLS["greenhouse_form"]), GreenhouseAdapter)
    assert isinstance(select_adapter(_page("lever_form"),
                                     _URLS["lever_form"]), LeverAdapter)
    assert isinstance(select_adapter(_page("ashby_form"),
                                     _URLS["ashby_form"]), AshbyAdapter)


def test_canonical_job_identity_is_structured_and_stable():
    gh = GreenhouseAdapter(_page("greenhouse_form"), _URLS["greenhouse_form"])
    assert gh.canonical_job_identity() == "greenhouse:aithrex:4200"
    assert gh.canonical_job_identity() == gh.canonical_job_identity()  # deterministic
    lv = LeverAdapter(_page("lever_form"), _URLS["lever_form"])
    assert lv.canonical_job_identity() == "lever:aithrex:aabbccddeeff0011"
    ab = AshbyAdapter(_page("ashby_form"), _URLS["ashby_form"])
    assert ab.canonical_job_identity() == "ashby:aithrex:1234abcd-5678-90ef-1234"


# ---------------------------------------------------------------------------
# Recon → normalized field_id questions
# ---------------------------------------------------------------------------
def test_greenhouse_recon_normalized_shape_and_legal_flag():
    gh = GreenhouseAdapter(_page("greenhouse_form"), _URLS["greenhouse_form"])
    qs = gh.recon()
    by_recon = {q["recon_id"]: q for q in qs}
    # Every normalized question carries the apply_forms field_id shape.
    for q in qs:
        assert {"field_id", "label", "type", "required", "is_legal_attestation",
                "recon_id"} <= set(q)
    # Résumé file field is executor-supplied → excluded; hidden utm dropped.
    assert "resume" not in by_recon and "utm_source" not in by_recon
    # The sponsorship question is a legal attestation.
    assert by_recon["question_100"]["is_legal_attestation"] is True
    # A plain screening question is not legal.
    assert by_recon["question_200"]["is_legal_attestation"] is False


def test_lever_recon_excludes_resume_flags_eeo():
    lv = LeverAdapter(_page("lever_form"), _URLS["lever_form"])
    qs = {q["recon_id"]: q for q in lv.recon()}
    assert "resume" not in qs
    assert qs["eeo[gender]"]["type"] == "select"
    assert qs["eeo[gender]"]["is_legal_attestation"] is True


# ---------------------------------------------------------------------------
# Deterministic fill + stable review snapshot hash
# ---------------------------------------------------------------------------
def test_deterministic_fill_records_values():
    gh = GreenhouseAdapter(_page("greenhouse_form"), _URLS["greenhouse_form"])
    gh.recon()
    r = gh.fill("first_name", "Ada")
    assert r["found"] is True and r["handle"] == "first_name"
    snap = gh.review_snapshot()
    assert snap["values"]["first_name"] == "Ada"


def test_review_snapshot_hash_is_stable_and_value_sensitive():
    def _filled(value):
        a = GreenhouseAdapter(_page("greenhouse_form"), _URLS["greenhouse_form"])
        a.recon()
        a.fill("first_name", "Ada")
        a.fill("email", value)
        a.upload_file("resume", "/tmp/resume.pdf")
        return a.review_snapshot()

    s1, s2 = _filled("ada@example.com"), _filled("ada@example.com")
    assert s1["hash"] == s2["hash"]              # same inputs → same hash
    assert s1["values"]["resume"] == "<file:resume.pdf>"
    assert _filled("other@example.com")["hash"] != s1["hash"]  # value-sensitive


# ---------------------------------------------------------------------------
# Validation capture
# ---------------------------------------------------------------------------
def test_capture_validation_errors():
    gh = GreenhouseAdapter(_page("validation_failure"), _URLS["validation_failure"])
    errs = gh.capture_validation_errors()
    assert errs
    assert any(e.get("field") == "email" and e["evidence"] == "aria-invalid" for e in errs)
    assert any("Email is required" in (e.get("message") or "") for e in errs)


# ---------------------------------------------------------------------------
# Submit boundary
# ---------------------------------------------------------------------------
def test_submit_ambiguous_is_submission_unknown_and_never_retries():
    page = _page("greenhouse_form", next_name="ambiguous")
    gh = GreenhouseAdapter(page, _URLS["greenhouse_form"])
    gh.recon()
    for fid in ("first_name", "last_name", "email", "question_100"):
        gh.fill(fid, "x")
    res = gh.submit()
    assert res["status"] == ADAPTER_SUBMISSION_UNKNOWN
    assert res["receipt"]["confirmed"] is False
    # Clicked submit EXACTLY once — the ambiguous result is never auto-retried.
    assert page.clicks == 1
    assert gh._submit_clicks == 1


def test_submit_success_on_confirmation_transition():
    page = _page("greenhouse_form", next_name="confirmation")
    gh = GreenhouseAdapter(page, _URLS["greenhouse_form"])
    gh.recon()
    for fid in ("first_name", "last_name", "email", "question_100"):
        gh.fill(fid, "x")
    res = gh.submit()
    assert res["status"] == ADAPTER_SUBMITTED
    assert res["receipt"]["confirmed"] is True
    assert res["confirmation"]["confirmed"] is True
    assert page.clicks == 1


def test_detect_confirmation_on_confirmation_fixture():
    gh = GreenhouseAdapter(_page("confirmation"), _URLS["confirmation"])
    conf = gh.detect_confirmation()
    assert conf["confirmed"] is True and conf["evidence"]


def test_hidden_empty_confirmation_is_not_confirmed():
    gh = GreenhouseAdapter(_page("greenhouse_form"), _URLS["greenhouse_form"])
    assert gh.detect_confirmation()["confirmed"] is False


# ---------------------------------------------------------------------------
# Selector drift → circuit breaker + health
# ---------------------------------------------------------------------------
def test_selector_drift_opens_circuit_and_flips_health():
    # Pin the baseline fingerprint from the known-good form.
    baseline = GreenhouseAdapter(_page("greenhouse_form"),
                                 _URLS["greenhouse_form"]).form_fingerprint()

    # Healthy adapter first: fingerprint matches baseline, circuit closed.
    healthy = GreenhouseAdapter(_page("greenhouse_form"), _URLS["greenhouse_form"],
                                baseline_fingerprint=baseline)
    healthy.recon()
    assert healthy.health()["status"] == HEALTH_HEALTHY
    assert healthy.health()["circuit_open"] is False

    # Now the form silently drifts. Repeated recon against the pinned baseline
    # trips the breaker OPEN (threshold = 3).
    last = None
    for _ in range(3):
        last = GreenhouseAdapter(_page("greenhouse_drift"), _URLS["greenhouse_drift"],
                                 baseline_fingerprint=baseline)
        last.recon()
    assert get_circuit_breaker("greenhouse").is_open() is True
    assert last.health()["status"] == HEALTH_OPEN
    assert last.health()["circuit_open"] is True

    # Support label flips to unsupported, and submit refuses → human takeover.
    sr = last.supported_reason()
    assert sr["supported"] is False and "circuit_open" in sr["reason"]
    res = last.submit()
    assert res["status"] == ADAPTER_WAITING_HUMAN
    assert res["takeover"]["reason"] == TAKEOVER_FORM_CHANGED


def test_supported_reason_healthy_greenhouse():
    gh = GreenhouseAdapter(_page("greenhouse_form"), _URLS["greenhouse_form"])
    sr = gh.supported_reason()
    assert sr["supported"] is True
    assert sr["recommended_action"] == "autonomous_fill"
    assert gh.health()["status"] == HEALTH_HEALTHY


# ---------------------------------------------------------------------------
# Generic fallback defaults to human review
# ---------------------------------------------------------------------------
def test_generic_fallback_defaults_to_review():
    adapter = select_adapter(_page("generic_form"), _URLS["generic_form"])
    assert isinstance(adapter, GenericAdapter)
    sr = adapter.supported_reason()
    assert sr["supported"] is False
    assert sr["recommended_action"] == "human_review"
    # It may prepare/fill, but it will NOT autonomously submit.
    adapter.recon()
    adapter.fill("full_name", "Ada")
    res = adapter.submit()
    assert res["status"] == ADAPTER_WAITING_HUMAN
    assert res["takeover"]["reason"] == TAKEOVER_UNSUPPORTED


def test_generic_does_not_claim_support_but_recons():
    adapter = GenericAdapter(_page("generic_form"), _URLS["generic_form"])
    qs = {q["recon_id"]: q for q in adapter.recon()}
    assert "full_name" in qs and "email" in qs  # it can prepare
    assert adapter.supported_reason()["supported"] is False  # but not claim autonomy


# ---------------------------------------------------------------------------
# Human takeover: account gate + CAPTCHA (never bypassed)
# ---------------------------------------------------------------------------
def test_account_required_pauses_for_human():
    gh = GreenhouseAdapter(_page("account_required"), _URLS["account_required"])
    assert gh.required_account() is True
    gh.recon()
    res = gh.submit()
    assert res["status"] == ADAPTER_WAITING_HUMAN
    assert res["takeover"]["reason"] == TAKEOVER_ACCOUNT_REQUIRED
    # No submit click was attempted into the account wall.
    assert gh._submit_clicks == 0


def test_captcha_pauses_for_human_and_is_never_solved():
    page = _page("greenhouse_captcha", next_name="confirmation")
    gh = GreenhouseAdapter(page, _URLS["greenhouse_captcha"])
    gh.recon()
    gh.fill("first_name", "Ada")
    gh.fill("email", "ada@example.com")
    res = gh.submit()
    assert res["status"] == ADAPTER_WAITING_HUMAN
    assert res["takeover"]["reason"] == TAKEOVER_CAPTCHA
    # Never clicked submit (never tried to power through the challenge).
    assert gh._submit_clicks == 0 and page.clicks == 0


# ---------------------------------------------------------------------------
# Workday registered-but-unsupported stub + LinkedIn policy refusal
# ---------------------------------------------------------------------------
def test_workday_is_now_supported_with_caveat():
    # Workday is NO LONGER a stub: it is a deterministic, data-automation-id-keyed
    # multi-step adapter, supported WITH the honest caveat (account-gated wizard →
    # frequently human takeover). See tests/test_workday_adapter.py for full cover.
    url = "https://aithrex.wd1.myworkdayjobs.com/en-US/careers/job/1234"
    page = FakePage('<html><body><div data-automation-id="myInformationPage">'
                    '<input data-automation-id="email" aria-label="Email" type="email">'
                    '<button data-automation-id="pageFooterNextButton" type="button">'
                    'Continue</button></div></body></html>', url=url)
    adapter = select_adapter(page, url)
    assert isinstance(adapter, WorkdayAdapter)
    sr = adapter.supported_reason()
    assert sr["supported"] is True and "workday" in sr["reason"].lower()
    assert "caveat" in sr  # honest: NOT full autonomy


def test_linkedin_is_never_automated():
    url = "https://www.linkedin.com/jobs/view/4200"
    page = FakePage('<html><body><a data-tracking-control-name="apply">Easy Apply</a>'
                    '</body></html>', url=url)
    adapter = select_adapter(page, url)
    assert isinstance(adapter, LinkedInAdapter)
    sr = adapter.supported_reason()
    assert sr["supported"] is False and "never automated" in sr["reason"].lower()


# ---------------------------------------------------------------------------
# detect_adapter (pure detection, no instantiation side effects)
# ---------------------------------------------------------------------------
def test_detect_adapter_picks_highest_confidence():
    det = detect_adapter(_page("greenhouse_form"), _URLS["greenhouse_form"])
    assert det["adapter_cls"] is GreenhouseAdapter and det["confidence"] >= 0.9


def test_unknown_host_falls_back_to_generic():
    det = detect_adapter(_page("generic_form"), _URLS["generic_form"])
    assert det["adapter_cls"] is GenericAdapter


def test_base_adapter_is_abstract_contract():
    # The concrete adapters implement the full contract; spot-check the surface.
    for meth in ("detect", "canonical_job_identity", "recon", "required_account",
                 "upload_file", "fill", "capture_validation_errors",
                 "review_snapshot", "submit", "detect_confirmation", "receipt",
                 "form_fingerprint", "health", "supported_reason"):
        assert callable(getattr(GreenhouseAdapter, meth))
    assert issubclass(GreenhouseAdapter, AtsAdapter)
