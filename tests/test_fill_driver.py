"""Deterministic FILL DRIVER tests (auto-apply capstone — recon→resolve→fill→submit).

HARNESS CHOICE (same as test_ats_adapters.py): the driver runs the REAL
``services.ats_adapters`` adapters against static Aithrex-owned HTML fixtures via
the lightweight lxml-backed headless-DOM harness (``tests/ats_dom``), with the
answer set resolved from a SEEDED profile through the real
``answer_library_service`` (mongomock). ZERO browser, ZERO network, ZERO LLM.

Covers, per the task:
  * happy path — a Greenhouse form fully fills from a seeded profile, submits, and
    returns a confirmed outcome + receipt;
  * a required unresolved SENSITIVE (legal attestation) field -> waiting_human with
    the field NAMED, and no submit click (never a guess);
  * Workday MULTI-STEP advances (My Information -> Application Questions) and reaches
    Review;
  * polarity — an inverse "without sponsorship" field fills the INVERTED value;
  * submission_unknown is HELD and the submit is clicked EXACTLY once (never retried).
"""
import pathlib

import lxml.html
import pytest

from _vg_support import current_db

from ats_dom import FakePage
from services.ats_adapters import GreenhouseAdapter, WorkdayAdapter, reset_circuit_breakers
from services import fill_driver
from services.fill_driver import (
    FILL_CONFIRMED, FILL_WAITING_HUMAN, FILL_SUBMISSION_UNKNOWN, FILL_PREPARED,
    MODE_APPROVAL, MODE_COPILOT,
)

_FIX = pathlib.Path(__file__).parent / "fixtures" / "ats"
USER = "ada@aspirely.me"

_GH_URL = "https://boards.greenhouse.io/aithrex/jobs/4200"
_WD_URL = "https://aithrex.wd1.myworkdayjobs.com/en-US/careers/job/1234"


# ---------------------------------------------------------------------------
# Fixtures / seeding
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _reset():
    reset_circuit_breakers()
    d = current_db()
    for c in ("resume_assets", "users", "user_preferences", "visa_profiles",
              "application_answer_library"):
        d[c].delete_many({})
    yield
    reset_circuit_breakers()


def _seed_profile(*, sponsorship=None, years=None):
    """Seed the DECLARED profile the answer library resolves from: a parsed
    résumé (contact) + preferences (declared legal + logistics/screening)."""
    d = current_db()
    d.resume_assets.insert_one({
        "resume_id": "asset-ada", "owner_type": "user", "user_email": USER,
        "parse_status": "parsed", "parsed_at": "2026-01-01T00:00:00+00:00",
        "structured": {"contact": {
            "name": "Ada Lovelace", "email": "ada@example.com",
            "phone": "+15551234567", "location": "London",
            "linkedin": "https://linkedin.com/in/ada"}},
    })
    prefs = {"user_email": USER}
    if sponsorship is not None:
        prefs["requires_sponsorship"] = sponsorship
    if years is not None:
        prefs["years_experience"] = years
    d.user_preferences.insert_one(prefs)


def _page(name, next_name=None, url=_GH_URL):
    nxt = (_FIX / (next_name + ".html")) if next_name else None
    return FakePage.from_file(_FIX / (name + ".html"), url=url, next_file=nxt)


# ---------------------------------------------------------------------------
# Happy path — Greenhouse fills from a seeded profile, submits, receipt
# ---------------------------------------------------------------------------
def test_greenhouse_happy_path_fills_submits_and_receipts():
    _seed_profile(sponsorship=False)
    page = _page("greenhouse_form", next_name="confirmation")
    adapter = GreenhouseAdapter(page, _GH_URL)

    answers = fill_driver.resolve_answers(USER, adapter.recon())
    # Every REQUIRED field resolved from the seeded profile (nothing invented).
    assert {"first_name", "last_name", "email", "requires_sponsorship"} <= set(answers)

    out = fill_driver.drive(adapter, answers, mode=MODE_APPROVAL)
    assert out["outcome"] == FILL_CONFIRMED, out
    assert out["confirmation"]["confirmed"] is True
    assert out["receipt"]["confirmed"] is True
    # The review snapshot proves exactly what was filled from the profile.
    vals = out["review_snapshot"]["values"]
    assert vals["first_name"] == "Ada" and vals["email"] == "ada@example.com"
    assert page.clicks == 1


# ---------------------------------------------------------------------------
# A required unresolved SENSITIVE field -> waiting_human, field named, no guess
# ---------------------------------------------------------------------------
def test_required_unresolved_sensitive_field_stops_waiting_human():
    # Seed contact but DO NOT declare sponsorship — the required work-auth
    # attestation is unresolvable and must NEVER be guessed.
    _seed_profile(sponsorship=None)
    page = _page("greenhouse_form", next_name="confirmation")
    adapter = GreenhouseAdapter(page, _GH_URL)

    answers = fill_driver.resolve_answers(USER, adapter.recon())
    assert "requires_sponsorship" not in answers      # unresolved (declared-only)

    out = fill_driver.drive(adapter, answers, mode=MODE_APPROVAL)
    assert out["outcome"] == FILL_WAITING_HUMAN, out
    assert out["field"] == "requires_sponsorship"     # the EXACT field is named
    assert out["reason"] == "sensitive_unconfirmed"
    # Stopped BEFORE any submit — the ambiguous form was never clicked.
    assert page.clicks == 0
    assert adapter._submit_clicks == 0


# ---------------------------------------------------------------------------
# Workday MULTI-STEP advances and reaches Review
# ---------------------------------------------------------------------------
class _WizardPage(FakePage):
    """A multi-step Workday wizard: a click on the ``pageFooterNextButton``
    advances to the next step's document; a submit click lands the final page.
    (The base FakePage only transitions on submit; Workday advances on Next.)"""

    def __init__(self, step_files, url, final_file=None):
        htmls = [_read(_FIX / (n + ".html")) for n in step_files]
        super().__init__(htmls[0], url=url)
        self._queue = htmls[1:]
        self._final = _read(_FIX / (final_file + ".html")) if final_file else None

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


def _read(path):
    with open(str(path), "r", encoding="utf-8") as fh:
        return fh.read()


def test_workday_multistep_advances_to_review():
    _seed_profile(years="5")
    page = _WizardPage(
        ["workday_my_information", "workday_application_questions", "workday_review"],
        url=_WD_URL, final_file="workday_confirmation")
    adapter = WorkdayAdapter(page, _WD_URL)

    # Resolve the union of every step's answerable fields from the seeded profile.
    answers = {"first_name": "Ada", "last_name": "Lovelace",
               "email": "ada@example.com", "years_experience": "5"}

    out = fill_driver.drive(adapter, answers, mode=MODE_COPILOT)
    # Copilot PREPARES only (never submits) — it advanced My Information ->
    # Application Questions -> Review across three steps.
    assert out["outcome"] == FILL_PREPARED, out
    assert out["current_step"] == "review"
    assert out["steps"] == 3
    # Fields from EARLIER steps persist in the final review snapshot.
    vals = out["review_snapshot"]["values"]
    assert vals["first_name"] == "Ada" and vals["years_experience"] == "5"


# ---------------------------------------------------------------------------
# Polarity — an inverse "without sponsorship" field fills the INVERTED value
# ---------------------------------------------------------------------------
_INVERSE_SPONSOR_FORM = """
<html><body>
<form action="https://boards.greenhouse.io/aithrex/jobs/4200" method="post">
  <label for="q_sponsor">Are you able to work without requiring visa sponsorship now or in the future?</label>
  <select id="q_sponsor" name="q_sponsor" required>
    <option>Yes</option><option>No</option>
  </select>
</form></body></html>
"""


def test_polarity_inverse_sponsorship_fills_inverted_option():
    # Declared: the user DOES require sponsorship (True). The form asks the
    # INVERSE ("able to work WITHOUT sponsorship") -> the answer must flip to No.
    _seed_profile(sponsorship=True)
    page = FakePage(_INVERSE_SPONSOR_FORM, url=_GH_URL)
    adapter = GreenhouseAdapter(page, _GH_URL)

    questions = adapter.recon()
    spons = next(q for q in questions if q["field_id"] == "requires_sponsorship")
    assert spons["invert"] is True                    # the normalizer flagged inversion

    answers = fill_driver.resolve_answers(USER, questions)
    # The resolved value is already flipped (declared True -> inverse -> False).
    assert answers["requires_sponsorship"] is False

    out = fill_driver.drive(adapter, answers, mode=MODE_COPILOT)
    # Option-matched to the form's actual "No" option — NOT a naive "Yes".
    assert out["review_snapshot"]["values"]["requires_sponsorship"] == "No"


# ---------------------------------------------------------------------------
# submission_unknown is HELD and NEVER retried
# ---------------------------------------------------------------------------
def test_submission_unknown_is_held_and_clicked_exactly_once():
    _seed_profile(sponsorship=False)
    page = _page("greenhouse_form", next_name="ambiguous")
    adapter = GreenhouseAdapter(page, _GH_URL)

    answers = fill_driver.resolve_answers(USER, adapter.recon())
    out = fill_driver.drive(adapter, answers, mode=MODE_APPROVAL)

    assert out["outcome"] == FILL_SUBMISSION_UNKNOWN, out
    assert out["receipt"]["confirmed"] is False
    # Clicked submit EXACTLY once — an ambiguous outcome is never auto-retried.
    assert page.clicks == 1
    assert adapter._submit_clicks == 1
