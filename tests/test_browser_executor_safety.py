"""Anti-fabrication + honest-outcome guards for ``services.browser_executor``.

These are REGRESSION tests for a live incident: the Workday drive path used to
merge a blanket ``_WD_DEFAULTS`` map into every application, which hardcoded a
literal person's name, invented employment dates and a role description, and —
worst — asserted work authorization, "no sponsorship required", US citizenship
and US-person export-control status on behalf of a user whose declared profile
says the opposite (F-1 OPT, citizenship India, sponsorship required). Those are
false legal attestations sent to real employers.

What is pinned here (AUTOAPPLY_REPAIR_SPEC §0):
  * NO identity / employment / legal value is ever defaulted — only the caller's
    resolved ``answers`` supply them, and a missing one stays missing so the fill
    driver parks the run instead of guessing (§0 rules 1, 2, 4);
  * the ONLY auto-defaults are non-assertive mechanics, today's signature date,
    and protected-EEO DECLINES (§0 rule 3 — declining asserts nothing);
  * an ATS we cannot drive returns the PERMANENT ``EXECUTOR_UNSUPPORTED`` result
    with a user-facing message, never the transient "unavailable" (§4);
  * ``prepared`` (dry-run / copilot) is a coherent, non-failure outcome (§3).

Zero browser, zero network, zero AWS: the executor's unsupported/recon paths are
pure, and the fill path runs the real Workday adapter over Aithrex-owned static
fixtures through the lxml ``tests/ats_dom`` harness.
"""
import pathlib

import pytest

from ats_dom import FakePage
from services import application_question_bank as qb
from services import fill_driver
from services.ats_adapters import WorkdayAdapter, reset_circuit_breakers
from services.browser_executor import (
    BrowserExecutor,
    EXECUTOR_PREPARED,
    EXECUTOR_UNSUPPORTED,
    UNSUPPORTED_USER_MESSAGE,
    _DECLINE,
    _SUPPORTED_FAMILIES,
    _WD_EEO_DECLINES,
    _workday_answers,
)
from services.executor_adapter import EXECUTOR_UNAVAILABLE, SUBMIT_SUCCESS

_FIX = pathlib.Path(__file__).parent / "fixtures" / "ats"
_WD_URL = "https://aithrex.wd1.myworkdayjobs.com/en-US/careers/job/1234"
_SRC = (pathlib.Path(__file__).parent.parent / "shared" / "python" / "services"
        / "browser_executor.py")

# The exact keys the deleted `_WD_DEFAULTS` asserted on the user's behalf. Not
# one of them may ever be auto-filled again: each is an identity fact, an
# employment fact, or a legal attestation.
_NEVER_DEFAULTED = (
    # identity
    "full_name", "name", "first_name", "last_name", "email", "phone",
    # employment history
    "work_start_date", "work_end_date", "earliest_start_date",
    "formfield_startdate", "formfield_enddate", "currently_work_here",
    "role_description", "job_description", "company", "job_title",
    # legal attestations (the dangerous ones)
    "work_authorized", "work_authorized_us", "requires_sponsorship",
    "require_sponsorship", "citizenship_countries", "countries_of_citizenship",
    "export_control_us_person", "over_18", "age_18_or_older",
    "previous_employee", "ey_employee", "family_member_auditor",
    "government_employee", "dod_employee", "noncompete_agreement",
    "own_ip_rights", "secondary_employment_intent", "accept_terms",
    "formfield_accepttermsandagreements", "has_security_clearance",
    "criminal_history",
)


@pytest.fixture(autouse=True)
def _reset():
    reset_circuit_breakers()
    yield
    reset_circuit_breakers()


def _executor():
    return BrowserExecutor(
        resume_resolver=lambda app, ref, ue: None,
        screenshot_sink=lambda app, png: "s3://audit/proof.png",
        user_email="ada@aspirely.me",
        headless=True,
    )


# ===========================================================================
# §0 — nothing about the user is ever invented
# ===========================================================================
def test_no_hardcoded_person_appears_anywhere_in_the_module():
    """The literal name that used to be filled into every Workday application."""
    src = _SRC.read_text(encoding="utf-8")
    assert "Harshith" not in src
    # And no blanket default map may return: the helper must add no identity key.
    filled = _workday_answers({})
    assert "full_name" not in filled and "name" not in filled


def test_no_legal_attestation_is_ever_auto_filled_without_a_declared_value():
    """§0 rule 1: work auth / sponsorship / citizenship / export control / age /
    clearance / criminal history may ONLY come from declared data."""
    filled = _workday_answers({})
    for key in _NEVER_DEFAULTED:
        assert key not in filled, f"{key} was fabricated by the default map"

    # Catalog-wide: every GATED field (protected EEO + legal) is either absent or
    # — for protected EEO only — an explicit decline. No legal field may appear.
    for field_id in qb.gated_field_ids():
        if field_id in filled:
            entry = qb.get_field(field_id) or {}
            assert entry.get("sensitivity") == qb.SENS_PROTECTED, (
                f"{field_id} is a LEGAL attestation and must not be defaulted")
            assert filled[field_id] == _DECLINE


def test_only_non_assertive_defaults_are_added():
    """§0 rule 3: the complete, exhaustive allow-list of auto-defaults."""
    filled = _workday_answers({})
    expected = {
        "phone_device_type", "language",          # form mechanics, assert nothing
        "date_signed", "formfield_datesignedon",  # the signature date IS today
    } | set(_WD_EEO_DECLINES)                     # protected-EEO declines only
    assert set(filled) == expected
    assert filled["phone_device_type"] == "Mobile"
    assert filled["language"] == "English"


def test_protected_eeo_defaults_are_declines_not_assertions():
    filled = _workday_answers({})
    for field_id in _WD_EEO_DECLINES:
        assert filled[field_id] == _DECLINE
    # "Hispanic or Latino? -> No" is an ASSERTION about ethnicity; the old map
    # answered it "No". It must decline instead.
    assert filled["hispanic_latino"] == _DECLINE


def test_decline_token_stays_in_sync_with_the_question_bank():
    assert _DECLINE == qb.DECLINE
    # …and the driver's option matcher maps it onto real Workday decline wording.
    for wording in ("I Don't Wish To Answer",
                    "Decline to State (United States of America)",
                    "I do not wish to provide this information",
                    "Prefer not to answer"):
        match = qb.match_option("disability_status", _DECLINE, ["Yes", "No", wording])
        assert match and match["option"] == wording


def test_declared_answers_always_win_over_every_default():
    """The live user is F-1 OPT, citizenship India, sponsorship REQUIRED — the
    exact opposite of what the deleted map asserted. Declared data must survive
    untouched, including an explicit EEO answer."""
    declared = {
        "requires_sponsorship": "Yes",
        "work_authorized_us": "Yes",
        "citizenship_countries": "India",
        "full_name": "Ada Lovelace",
        "gender": "Female",
        "phone_device_type": "Home",
        "date_signed": "01/02/2026",
    }
    filled = _workday_answers(declared)
    for key, value in declared.items():
        assert filled[key] == value


def test_key_aliasing_is_preserved_and_copies_only_declared_values():
    """Aliasing is legitimate normalization — it moves a value the caller
    supplied, and invents nothing when the caller supplied nothing."""
    filled = _workday_answers({
        "address_line_1": "1 Main St", "job_title": "Engineer",
        "company": "Aithrex", "country": "India", "disability_status": "Yes",
    })
    assert filled["address_line1"] == "1 Main St"
    assert filled["current_title"] == "Engineer"
    assert filled["current_company"] == "Aithrex"
    assert filled["formfield_country"] == "India"
    # The alias carries the DECLARED disability answer, not the decline default.
    assert filled["formfield_disabilityform"] == "Yes"
    # No alias fabricated a value for a key the caller never supplied.
    assert "formfield_startdate" not in filled


def test_signature_date_is_today_utc():
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).strftime("%m/%d/%Y")
    filled = _workday_answers({})
    assert filled["date_signed"] == today
    assert filled["formfield_datesignedon"] == today


# ===========================================================================
# §0 end-to-end: a missing legal attestation PARKS, it never guesses
# ===========================================================================
# Aithrex-owned synthetic Workday step carrying the three attestations the
# deleted default map used to answer for the user. NEVER a real employer.
_WD_LEGAL_STEP = """<!doctype html><html><body class="wd-theme-canvas">
<div data-automation-id="questionnairePage" class="wd-step">
  <h1>Application Questions</h1>
  <div data-automation-id="formField-workAuth" class="wd-field">
    <button data-automation-id="workAuthorization" aria-haspopup="listbox"
      aria-required="true" type="button"
      aria-label="Are you legally authorized to work in the United States?">Select One</button>
  </div>
  <div data-automation-id="formField-sponsorship" class="wd-field">
    <button data-automation-id="sponsorshipRequired" aria-haspopup="listbox"
      aria-required="true" type="button"
      aria-label="Will you now or in the future require sponsorship for employment visa status?">Select One</button>
  </div>
  <div data-automation-id="formField-citizenship" class="wd-field">
    <input data-automation-id="citizenship" aria-required="true" type="text"
      aria-label="Please list your countries of citizenship">
  </div>
  <div data-automation-id="pageFooter" class="wd-footer">
    <button data-automation-id="pageFooterNextButton" type="button">Save and Continue</button>
  </div>
</div></body></html>"""


def test_required_legal_attestation_with_no_declared_value_parks_never_guesses():
    """THE regression: drive the REAL Workday adapter over required work-auth /
    sponsorship / citizenship questions with an EMPTY resolved answer map. The
    executor's allowed defaults cover NONE of them, so the run stops and names
    the field — previously it answered all three, wrongly, and submitted."""
    adapter = WorkdayAdapter(FakePage(_WD_LEGAL_STEP, url=_WD_URL), _WD_URL)
    recon = {q["field_id"] for q in adapter.recon()}
    assert {"work_authorized_us", "requires_sponsorship",
            "citizenship_countries"} <= recon

    out = fill_driver.drive(adapter, _workday_answers({}),
                            mode=fill_driver.MODE_AUTOPILOT)

    assert out["outcome"] == fill_driver.FILL_WAITING_HUMAN, out
    assert out["field"] == "work_authorized_us"     # the EXACT field is named
    assert out["reason"] == "sensitive_unconfirmed"
    assert adapter._submit_clicks == 0              # nothing was submitted
    # NOT ONE attestation reached the form.
    assert (out.get("review_snapshot") or {}).get("values") == {}


def test_declared_legal_answers_are_filled_verbatim():
    """The live user's truth (F-1 OPT: authorized now, sponsorship REQUIRED,
    citizenship India) flows through untouched — proving the park above is about
    the missing datum, not a blanket refusal."""
    adapter = WorkdayAdapter(FakePage(_WD_LEGAL_STEP, url=_WD_URL), _WD_URL)
    answers = _workday_answers({
        "work_authorized_us": "Yes",
        "requires_sponsorship": "Yes",
        "citizenship_countries": "India",
    })

    fill_driver.drive(adapter, answers, mode=fill_driver.MODE_AUTOPILOT)

    values = adapter.review_snapshot()["values"]
    assert values["work_authorized_us"] == "Yes"
    assert values["requires_sponsorship"] == "Yes"
    assert values["citizenship_countries"] == "India"


# ===========================================================================
# §4 — unsupported vendor is PERMANENT, distinct, and user-facing
# ===========================================================================
@pytest.mark.parametrize("url", [
    "https://careers-acme.icims.com/jobs/1234/apply",
    "https://jobs.smartrecruiters.com/Acme/743999",
    "https://apply.workable.com/acme/j/ABC123/",
    "https://careers.acme.example.com/apply/999",
])
def test_submit_to_an_unsupported_vendor_returns_the_unsupported_status(url):
    res = _executor().submit({"application_id": "app1", "apply_url": url}, {}, None)

    assert res["status"] == EXECUTOR_UNSUPPORTED
    assert res["status"] != EXECUTOR_UNAVAILABLE     # NOT the transient one
    assert res["user_message"] == UNSUPPORTED_USER_MESSAGE
    assert res["retryable"] is False
    assert res["submitted"] is False
    assert res["reason"].startswith("unsupported_ats:")
    # `error` mirrors `reason` so an orchestrator that has not learned the new
    # status still records a legible cause (never a blank failure).
    assert res["error"] == res["reason"]
    assert res["wall"] is None and res["confirmation"] is None


def test_unsupported_never_launches_a_browser_or_resolves_a_resume():
    """The unsupported branch returns BEFORE any résumé/S3 or Playwright work."""
    calls = []
    ex = BrowserExecutor(
        resume_resolver=lambda *a: calls.append("resume"),
        screenshot_sink=lambda *a: calls.append("shot"),
        headless=True,
    )
    res = ex.submit(
        {"application_id": "a", "apply_url": "https://x.icims.com/jobs/1/apply"},
        {}, {"tailoring_record_id": "t1"})
    assert res["status"] == EXECUTOR_UNSUPPORTED
    assert calls == []


def test_supported_families_still_route_to_their_driver():
    """The unsupported branch must not swallow a family we DO drive."""
    ex = _executor()
    seen = {}

    def _fake_driver(target, answers, resume, application):
        seen["target"] = target
        return {"status": SUBMIT_SUCCESS, "confirmation": "ok",
                "screenshot_ref": None, "wall": None, "error": None}

    ex._drive_greenhouse = _fake_driver
    res = ex.submit({"application_id": "a",
                     "apply_url": "https://job-boards.greenhouse.io/acme/jobs/1"},
                    {}, None)
    assert res["status"] == SUBMIT_SUCCESS
    assert "greenhouse.io" in seen["target"]
    assert {"greenhouse", "lever", "ashby", "workday"} == set(_SUPPORTED_FAMILIES)


def test_recon_of_an_unsupported_vendor_flags_it_without_faking_a_form():
    res = _executor().recon_form("https://careers-acme.icims.com/jobs/1/apply", None)

    assert res["questions"] == [] and res["needs_account"] is False
    assert res["unsupported"] is True
    assert res["unsupported_status"] == EXECUTOR_UNSUPPORTED
    assert res["user_message"] == UNSUPPORTED_USER_MESSAGE
    assert res["ats_family"] == "icims"
    # Compat: an orchestrator that only knows the old vocabulary must still take
    # its SAFE path (leave the row queued) rather than treat [] as a real form.
    assert res["status"] == EXECUTOR_UNAVAILABLE


# ===========================================================================
# §3 — "prepared" is one coherent, non-failure outcome
# ===========================================================================
def test_prepared_vocabulary_is_shared_between_executor_and_fill_driver():
    assert EXECUTOR_PREPARED == fill_driver.FILL_PREPARED
    assert fill_driver.FILL_PREPARED in fill_driver.FILL_OUTCOMES
    assert fill_driver.FILL_PREPARED in fill_driver.FILL_NON_FAILURE_OUTCOMES
    # Only `prepared` is a non-failure, non-submit stop.
    assert fill_driver.FILL_NON_FAILURE_OUTCOMES == {fill_driver.FILL_PREPARED}


def test_copilot_prepare_marks_itself_as_not_submitted():
    page = FakePage.from_file(_FIX / "workday_application_questions.html", url=_WD_URL)
    adapter = WorkdayAdapter(page, _WD_URL)

    out = fill_driver.drive(adapter, _workday_answers({"requires_sponsorship": "No",
                                                       "years_experience": "5"}),
                            mode=fill_driver.MODE_COPILOT)
    assert out["outcome"] == fill_driver.FILL_PREPARED
    assert out["submitted"] is False
    assert out["reason"] == "copilot_prepare_only"
    assert adapter._submit_clicks == 0
