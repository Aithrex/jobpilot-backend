"""Auto-apply EDGE E2E — the §10 / QT4 cases not already covered elsewhere.

This file ONLY adds edges that the existing suites don't already prove. Each test
notes the sibling suite that owns the adjacent (already-covered) case so the split
is auditable:

  * EEO / self-identify GATING through the fill_driver — the driver-level cover in
    test_fill_driver.py exercises a work_auth (sponsorship) sensitive stop; here we
    prove a PROTECTED EEO field (gender / disability self-ID) with no declared
    value likewise stops to waiting_human and is NEVER guessed, AND that a user's
    explicit "decline" IS a valid declared value the driver fills (the positive
    half of the sensitivity gate).
  * option-mismatch — a REQUIRED select whose live options can't confidently match
    the resolved value → needs_input (no_option_match), never a wrong pick. The
    option matcher itself is unit-tested in the question-bank tests; this proves the
    fill_driver STOP wiring on top of it.
  * MFA / OTP wall MID-FLOW (after the single Submit click) → waiting_human with a
    concrete pending_action, clicked exactly once. test_ats_adapters.py covers the
    CAPTCHA + account gate; MFA-after-submit through the driver was uncovered.
  * selector drift / circuit breaker MID-WIZARD — an open Workday breaker (fleet
    state from prior drift) aborts a multi-step run at the terminal submit boundary
    → waiting_human (form_change), never submitting. test_workday_adapter.py opens
    the breaker at the adapter level; this proves the driver honours it across a
    multi-step advance.
  * consent revocation JUST BEFORE submit → presubmit_recheck blocks. The
    kill-switch / drift / dead-adapter blocks live in test_cloud_run_executor.py;
    the user-revoked-consent (autopilot paused/revoked) block was uncovered.
  * cross-user IDOR on the application read path — an owner-scoped read never leaks
    another user's row. test_mailbox_api.py covers the mailbox-thread IDOR; this is
    the application-endpoint spot-check.

Fully offline: mongomock (conftest) + the lxml FakePage harness. Zero browser,
zero network, zero LLM.
"""
import lxml.html
import pytest

from _vg_support import current_db

from ats_dom import FakePage
from services.ats_adapters import (
    GreenhouseAdapter, WorkdayAdapter, reset_circuit_breakers, get_circuit_breaker,
)
from services import fill_driver
from services.fill_driver import (
    FILL_WAITING_HUMAN, FILL_NEEDS_INPUT, FILL_PREPARED,
    MODE_APPROVAL, MODE_COPILOT,
)
import services.application_ledger_service as als
import services.auto_apply_service as aas

_GH_URL = "https://boards.greenhouse.io/aithrex/jobs/4200"
_WD_URL = "https://aithrex.wd1.myworkdayjobs.com/en-US/careers/job/1234"

import pathlib
_FIX = pathlib.Path(__file__).parent / "fixtures" / "ats"


@pytest.fixture(autouse=True)
def _reset():
    reset_circuit_breakers()
    d = current_db()
    for c in ("applications", "auto_apply_answers", "auto_apply_freezes",
              "auto_submit_audit", "auto_submit_approvals"):
        d[c].delete_many({})
    yield
    reset_circuit_breakers()
    aas.set_executor(None)


# ===========================================================================
# EEO / self-identify sensitivity gate through the fill_driver
# ===========================================================================
_EEO_GENDER_FORM = """
<html><body>
<form action="{url}" method="post">
  <label for="g">Gender</label>
  <select id="g" name="g" required>
    <option>Male</option><option>Female</option>
    <option>Decline to self-identify</option>
  </select>
  <button type="submit">Submit application</button>
</form></body></html>
""".format(url=_GH_URL)


def test_eeo_protected_field_unconfirmed_waits_human_and_is_never_guessed():
    # A REQUIRED protected EEO field (gender) with NO declared value must NEVER be
    # inferred from name/résumé/anything — the run stops and names the field.
    page = FakePage(_EEO_GENDER_FORM, url=_GH_URL)
    adapter = GreenhouseAdapter(page, _GH_URL)

    q = next(x for x in adapter.recon() if x["field_id"] == "gender")
    assert q["is_legal_attestation"] is True and q["required"] is True

    # No answer supplied for the protected field.
    out = fill_driver.drive(adapter, {}, mode=MODE_APPROVAL)
    assert out["outcome"] == FILL_WAITING_HUMAN, out
    assert out["field"] == "gender"
    assert out["reason"] == "sensitive_unconfirmed"
    # Never guessed, never clicked.
    assert page.clicks == 0 and adapter._submit_clicks == 0
    assert "gender" not in out["review_snapshot"]["values"]


def test_eeo_declared_decline_is_an_explicit_value_and_is_filled():
    # The positive half of the gate: a user's explicit "decline" IS a declared
    # value, so the driver fills the form's DECLINE option — it does not block.
    page = FakePage(_EEO_GENDER_FORM, url=_GH_URL)
    adapter = GreenhouseAdapter(page, _GH_URL)

    out = fill_driver.drive(adapter, {"gender": "decline"}, mode=MODE_COPILOT)
    assert out["outcome"] == FILL_PREPARED, out
    # Option-matched to THIS form's exact decline wording, never a raw "decline".
    assert out["review_snapshot"]["values"]["gender"] == "Decline to self-identify"


# ===========================================================================
# Option-mismatch — required select, no confident option match → needs_input
# ===========================================================================
_OPTION_MISMATCH_FORM = """
<html><body>
<form action="{url}" method="post">
  <label for="h">How did you hear about us?</label>
  <select id="h" name="h" required>
    <option>LinkedIn</option><option>Indeed</option><option>Referral</option>
  </select>
  <button type="submit">Submit application</button>
</form></body></html>
""".format(url=_GH_URL)


def test_required_select_option_mismatch_stops_needs_input_never_wrong_pick():
    page = FakePage(_OPTION_MISMATCH_FORM, url=_GH_URL)
    adapter = GreenhouseAdapter(page, _GH_URL)

    # The resolved value ("Company website") matches NONE of this form's options.
    out = fill_driver.drive(adapter, {"how_did_you_hear": "Company website"},
                            mode=MODE_APPROVAL)
    assert out["outcome"] == FILL_NEEDS_INPUT, out
    assert out["field"] == "how_did_you_hear"
    assert out["reason"] == "no_option_match"
    # No wrong-but-plausible option was written, and submit was never clicked.
    assert "how_did_you_hear" not in out["review_snapshot"]["values"]
    assert page.clicks == 0 and adapter._submit_clicks == 0


# ===========================================================================
# MFA / OTP wall AFTER the single Submit click → waiting_human + pending_action
# ===========================================================================
_OTP_WALL = ("<html><body><h1>Verify it's you</h1>"
             "<p>Enter the verification code we sent to your email.</p>"
             "<input name='otp'></body></html>")

_GH_MIN_FORM = ("<html><body><form action='{url}' method='post'>"
                "<label for='fn'>First name</label>"
                "<input id='fn' name='fn' required>"
                "<button type='submit'>Submit application</button>"
                "</form></body></html>").format(url=_GH_URL)


def test_mfa_wall_after_submit_click_waits_human_with_pending_action():
    # The form submits and lands on an OTP/MFA challenge — a takeover we DETECT to
    # pause, never solve. Clicked exactly once (the post-click boundary catches it).
    page = FakePage(_GH_MIN_FORM, url=_GH_URL, next_html=_OTP_WALL)
    adapter = GreenhouseAdapter(page, _GH_URL)

    out = fill_driver.drive(adapter, {"first_name": "Ada"}, mode=MODE_APPROVAL)
    assert out["outcome"] == FILL_WAITING_HUMAN, out
    assert out["reason"] == "mfa"
    assert out["takeover"]["reason"] == "mfa"
    pa = out["pending_action"]
    assert pa and pa["reason"] == "mfa" and pa["job_identity"] == "greenhouse:aithrex:4200"
    # Submitted exactly once, then held — never powered through the challenge.
    assert page.clicks == 1 and adapter._submit_clicks == 1


# ===========================================================================
# Selector drift / circuit breaker MID-WIZARD — advance then abort to human
# ===========================================================================
def _read(name):
    with open(str(_FIX / (name + ".html")), "r", encoding="utf-8") as fh:
        return fh.read()


class _WizardPage(FakePage):
    """Multi-step Workday wizard: a ``pageFooterNextButton`` click advances to the
    next step's document; a submit click lands the final page (same harness as
    test_fill_driver.py)."""

    def __init__(self, step_files, url, final_file=None):
        htmls = [_read(n) for n in step_files]
        super().__init__(htmls[0], url=url)
        self._queue = htmls[1:]
        self._final = _read(final_file) if final_file else None

    def _on_click(self, element):
        self.clicks += 1
        node = element._node
        if (node.get("data-automation-id") or "") == "pageFooterNextButton" and self._queue:
            self._tree = lxml.html.fromstring(self._queue.pop(0))
            return
        typ = (node.get("type") or "").lower()
        is_submit = (node.tag == "button" and typ in ("", "submit")) \
            or (node.tag == "input" and typ == "submit")
        if is_submit and self._final is not None:
            self._tree = lxml.html.fromstring(self._final)
            self._final = None


def test_open_circuit_breaker_aborts_workday_wizard_midflow_to_human():
    # Fleet state: the Workday breaker is already OPEN from prior selector drift
    # (threshold 3). A multi-step run then ADVANCES through the wizard but the
    # terminal submit boundary refuses — waiting_human (form_change), never submits.
    baseline = WorkdayAdapter(
        FakePage(_read("workday_my_information"), url=_WD_URL), _WD_URL).form_fingerprint()
    for _ in range(3):
        d = WorkdayAdapter(FakePage(_read("workday_my_information_drift"), url=_WD_URL),
                           _WD_URL, baseline_fingerprint=baseline)
        d.recon()
    assert get_circuit_breaker("workday").is_open() is True

    page = _WizardPage(
        ["workday_my_information", "workday_application_questions", "workday_review"],
        url=_WD_URL, final_file="workday_confirmation")
    adapter = WorkdayAdapter(page, _WD_URL, baseline_fingerprint=baseline)
    answers = {"first_name": "Ada", "last_name": "Lovelace",
               "email": "ada@example.com", "years_experience": "5"}

    out = fill_driver.drive(adapter, answers, mode=MODE_APPROVAL)
    assert out["outcome"] == FILL_WAITING_HUMAN, out
    assert out["reason"] == "form_change"
    assert out["steps"] == 3                 # advanced across the full wizard
    assert adapter._submit_clicks == 0       # but never clicked the final submit


# ===========================================================================
# Consent revocation JUST BEFORE submit → presubmit_recheck blocks
# ===========================================================================
_JD = "We are hiring a backend engineer to build distributed systems in Python."


class _AvailExecutor:
    def is_available(self):
        return True


def _seat_running_row(db, user):
    db.applications.create_index([("application_id", 1)], unique=True,
                                 name="application_id_unique")
    view, _ = als.create_application(
        user, company="GitLab", title="AI Engineer", jd_text=_JD,
        source="greenhouse", ats_family="greenhouse",
        apply_url="https://boards.greenhouse.io/gitlab/jobs/1")
    aid = view["application_id"]
    als.mark_gathering(user, aid)
    als.mark_ready_for_review(user, aid, autonomy="auto", confidence_score=0.95,
                              tailoring_record_id=None)
    aas._system_approve(user, aid)
    als.transition(user, aid, als.STATUS_QUEUED_FOR_EXECUTION)
    als.transition(user, aid, als.STATUS_RUNNING)
    return aid


def test_presubmit_recheck_blocks_when_consent_revoked_before_submit(monkeypatch):
    # The user pauses/revokes auto-apply between dispatch and the click. Drive the
    # consent seam (the same way the sibling executor tests drive the kill-switch
    # seam / executor) and assert presubmit_recheck WIRES it into a hard block —
    # the contract the remote worker relies on to abort a now-unconsented submit.
    user = "alice@aspirely.me"
    db = current_db()
    aid = _seat_running_row(db, user)
    # Everything else holds — the ONLY failing gate is the user's revoked consent.
    monkeypatch.setattr(aas, "_auto_submit_enabled", lambda: True)
    monkeypatch.setattr(aas, "_autopilot_active", lambda user_email, app: False)
    aas.set_executor(_AvailExecutor())

    res = aas.presubmit_recheck(aid, user)
    assert res["ok"] is False
    assert res["checks"]["autopilot_active"] is False
    assert "autopilot_paused_or_revoked" in res["reasons"]
    # The gates that DID hold prove the block is specifically the consent gate.
    assert res["checks"]["kill_switch"] is True
    assert res["checks"]["row_live"] is True


# ===========================================================================
# Cross-user IDOR — an owner-scoped application read never leaks another user
# ===========================================================================
def test_application_read_is_owner_scoped_no_cross_user_leak():
    user_a = "alice@aspirely.me"
    user_b = "mallory@aspirely.me"
    db = current_db()
    db.applications.create_index([("application_id", 1)], unique=True,
                                 name="application_id_unique")
    view, _ = als.create_application(
        user_a, company="Acme", title="Engineer", jd_text=_JD,
        source="greenhouse", apply_url="https://boards.greenhouse.io/acme/jobs/1")
    aid = view["application_id"]

    # The owner reads their own row; a different user gets nothing (never the row).
    assert als.get_application(user_a, aid)["application_id"] == aid
    assert als.get_application(user_b, aid) is None
    # And a cross-user mutation touches nothing (owner-scoped write).
    als.set_outcome(user_b, record_id="nope", outcome="offer", source="mail_ingress")
    assert als.get_application(user_a, aid)["status"] != als.STATUS_EXTERNAL
