"""Regression suite for a LIVE production incident: an Ashby application that
was submitted with ZERO fields filled.

WHAT HAPPENED
-------------
A real application to an Ashby-hosted posting came back:

    validation: Your form needs corrections
    Missing entry for required field: Name
    Missing entry for required field: Email
    Missing entry for required field: Resume

The resolved answers were perfect (``coverage: 1.0``) and keyed by the CANONICAL
question-bank ids ``full_name`` / ``email`` / … — the ids
``apply_forms.normalize_recon_questions`` derives from each question's LABEL.
``browser_executor._fill_fields`` then built its selector as ``#<answer key>``,
i.e. it assumed the canonical id IS the DOM element id. On Ashby the ids are
``_systemfield_name`` / ``_systemfield_email`` / per-question UUIDs, so every
lookup returned None, every field landed in ``skipped`` — and the drive clicked
Submit anyway. It only ever worked on Greenhouse, by coincidence: that vendor's
inputs happen to carry ids like ``#first_name``.

WHAT IS PINNED HERE
-------------------
1. The premise — on the real Ashby DOM the canonical ``field_id`` is NOT the
   element id, and the old ``#<field_id>`` guess finds nothing (the mutation
   guard: it proves the recon map is load-bearing, not decorative).
2. The fix — a drive over the REAL Ashby fixture fills name + email into the
   ``_systemfield_*`` inputs, attaches the résumé, clicks Submit exactly once
   and confirms. Same for Greenhouse and Lever from the same canonical keys.
3. THE SAFETY RAIL (the most valuable part): a required field that cannot be
   located, a required field with no vetted answer, and a form whose résumé
   never attached each STOP the run BEFORE the Submit click and name the field,
   exactly as ``fill_driver`` does for Workday. A partially-filled form is never
   submitted to a real employer.
4. The wire contract — that stop is the ``needs_input`` shape
   ``auto_apply_service`` parks in the one-tap queue, NOT a failed row.

Fully offline: the lxml ``tests/ats_dom`` harness over the Aithrex-owned static
fixtures. Zero browser, zero network, zero AWS.
"""
import pathlib

import pytest

from _vg_support import current_db

from ats_dom import FakePage
from services.ats_adapters import reset_circuit_breakers, select_adapter
from services.browser_executor import BrowserExecutor
from services.executor_adapter import (
    SUBMIT_SUCCESS, SUBMIT_FAILED, EXECUTOR_UNAVAILABLE,
)
import services.application_ledger_service as als
import services.auto_apply_service as aas

_FIX = pathlib.Path(__file__).parent / "fixtures" / "ats"
_PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF"

_ASHBY_URL = "https://jobs.ashbyhq.com/aithrex/1234abcd-5678-90ef-1234/application"
_GH_URL = "https://boards.greenhouse.io/aithrex/jobs/4200"
_LEVER_URL = "https://jobs.lever.co/aithrex/aabbccddeeff0011/apply"

# The EXACT answer-key vocabulary the failed production row carried: canonical
# question-bank field_ids, never DOM handles.
_CANONICAL_ANSWERS = {
    "full_name": "Ada Lovelace",
    "email": "ada@aspirely.me",
    "first_name": "Ada",
    "last_name": "Lovelace",
    "phone": "5551234567",
    "gender": "Female",
    "requires_sponsorship": "Yes",
}

_APP = {"application_id": "app1", "apply_url": _ASHBY_URL, "ats_family": "ashby"}


@pytest.fixture(autouse=True)
def _reset():
    reset_circuit_breakers()
    yield
    reset_circuit_breakers()
    aas.set_executor(None)


def _executor(shots=None):
    return BrowserExecutor(
        resume_resolver=lambda app, ref, ue: ("resume.pdf", _PDF),
        screenshot_sink=lambda app, png: ((shots if shots is not None else []).append(
            len(png)), "s3://audit/proof.png")[1],
        user_email="ada@aspirely.me",
        headless=True,
    )


def _page(fixture: str, url: str, *, confirm: bool = True) -> FakePage:
    return FakePage.from_file(
        _FIX / fixture, url=url,
        next_file=(_FIX / "confirmation.html") if confirm else None)


def _values(page: FakePage) -> dict:
    out = {}
    for el in page.query_selector_all("input") + page.query_selector_all("select"):
        handle = el.get_attribute("id") or el.get_attribute("name")
        out[handle] = el.get_attribute("value")
    return out


# ===========================================================================
# 1. The premise + the mutation guard
# ===========================================================================
def test_ashby_canonical_field_ids_are_not_the_dom_ids():
    """Recon DOES capture the real handle — the canonical id never equals it."""
    page = _page("ashby_form.html", _ASHBY_URL, confirm=False)
    questions = {q["field_id"]: q["recon_id"] for q in
                 select_adapter(page, _ASHBY_URL).recon()}

    assert questions["full_name"] == "_systemfield_name"
    assert questions["email"] == "_systemfield_email"
    for field_id, recon_id in questions.items():
        assert field_id != recon_id, "fixture no longer exercises the incident"


def test_the_old_id_guess_fills_nothing_on_ashby():
    """MUTATION GUARD. Drop the recon map (the pre-fix code path) and the exact
    production symptom reappears: every field skipped, nothing in the DOM."""
    page = _page("ashby_form.html", _ASHBY_URL, confirm=False)

    filled, skipped = _executor()._fill_fields(page, _CANONICAL_ANSWERS)

    assert filled == 0
    assert set(skipped) == set(_CANONICAL_ANSWERS)
    assert _values(page)["_systemfield_name"] is None


def test_recon_map_targets_the_element_recon_found():
    page = _page("ashby_form.html", _ASHBY_URL, confirm=False)
    ex = _executor()

    field_map = ex._field_selectors(ex._recon_live_fields(page, _ASHBY_URL))

    # Canonical id → the live selector; the raw handle is kept as an alias.
    assert field_map["full_name"] == '[id="_systemfield_name"]'
    assert field_map["email"] == '[id="_systemfield_email"]'
    assert field_map["_systemfield_name"] == field_map["full_name"]
    # No vendor prefix is hardcoded anywhere in the mapping.
    assert "_systemfield_" not in ex._selector_for_handle.__doc__.split("No per-vendor")[1]


# ===========================================================================
# 2. The fix, end to end, on the REAL fixtures
# ===========================================================================
class _RecordingPage(FakePage):
    """Snapshots the form at the instant Submit is clicked — i.e. exactly what
    the employer receives."""

    submitted_values = None

    def _on_click(self, element):
        self.submitted_values = _values(self)
        super()._on_click(element)


def test_ashby_drive_fills_name_email_and_attaches_the_resume():
    """THE regression. Name, email and résumé all land, then one Submit click."""
    page = _RecordingPage(
        (_FIX / "ashby_form.html").read_text(encoding="utf-8"), url=_ASHBY_URL,
        next_html=(_FIX / "confirmation.html").read_text(encoding="utf-8"))
    shots = []
    ex = _executor(shots)

    res = ex._drive_page(page, _ASHBY_URL, _CANONICAL_ANSWERS,
                         ("resume.pdf", _PDF), _APP, ex._fill_fields)

    assert res["status"] == SUBMIT_SUCCESS, res
    assert res["error"] is None and res["wall"] is None
    # The three fields the employer said were missing are the three that landed.
    assert page.submitted_values["_systemfield_name"] == "Ada Lovelace"
    assert page.submitted_values["_systemfield_email"] == "ada@aspirely.me"
    assert page.uploads == {'[id="_systemfield_resume"]': "resume.pdf"}
    assert res["screenshot_ref"] == "s3://audit/proof.png" and shots
    # Submitted exactly once (the fixture swaps to the confirmation page).
    assert page.clicks == 1


@pytest.mark.parametrize("fixture,url,fill,expected", [
    ("greenhouse_form.html", _GH_URL, "_fill_fields",
     {"first_name": "Ada", "last_name": "Lovelace", "email": "ada@aspirely.me"}),
    ("lever_form.html", _LEVER_URL, "_fill_lever",
     {"name": "Ada Lovelace", "email": "ada@aspirely.me", "phone": "5551234567"}),
])
def test_canonical_keys_fill_every_vendor(fixture, url, fill, expected):
    """Vendor-neutral: the SAME canonical answer keys reach Greenhouse's `#id`
    form and Lever's `[name]` form — Lever's handle for ``full_name`` is ``name``
    and nothing per-vendor is hardcoded to make that work."""
    page = _page(fixture, url, confirm=False)
    ex = _executor()

    field_map = ex._field_selectors(ex._recon_live_fields(page, url))
    filled, _skipped = getattr(ex, fill)(page, _CANONICAL_ANSWERS, field_map)

    values = _values(page)
    for handle, want in expected.items():
        assert values[handle] == want, (handle, values)
    assert filled >= len(expected)


def test_resume_input_is_found_by_walking_file_inputs_not_by_vendor_id():
    """`#resume` is Greenhouse's id, `#_systemfield_resume` ONE Ashby template.
    Each form's own upload is discovered from the input's own attributes."""
    ex = _executor()
    for fixture, url, want in (
        ("ashby_form.html", _ASHBY_URL, '[id="_systemfield_resume"]'),
        ("greenhouse_form.html", _GH_URL, '[id="resume"]'),
        ("lever_form.html", _LEVER_URL, '[name="resume"]'),
    ):
        page = _page(fixture, url, confirm=False)
        assert ex._resume_input_selectors(page)[0] == want
        assert ex._attach_resume(page, ("resume.pdf", _PDF)) is True

    # A DIFFERENT Ashby template (Aithrex-owned synthetic): the upload carries a
    # generated id matching NONE of the historical hardcoded guesses (`#resume`,
    # `#_systemfield_resume`, `[name=resume]`). Only reading the input's own
    # attributes finds it — which is why no vendor prefix may be hardcoded.
    variant = FakePage(
        "<html><body class='ashby-application-form'><form>"
        "<input id='_systemfield_name'>"
        "<input type='file' id='f_9f2c1e' aria-label='Resume / CV'>"
        "<button type='submit'>Submit Application</button></form></body></html>",
        url=_ASHBY_URL)
    assert ex._resume_input_selectors(variant)[0] == '[id="f_9f2c1e"]'
    assert ex._attach_resume(variant, ("resume.pdf", _PDF)) is True

    # A form with no upload at all: nothing to attach, and no false "resume gap".
    bare = FakePage("<html><body><form><input id='email'>"
                    "<button type='submit'>Submit</button></form></body></html>",
                    url=_GH_URL)
    assert ex._resume_input_selectors(bare) == []
    assert ex._attach_resume(bare, ("resume.pdf", _PDF)) is False


# ===========================================================================
# 3. THE SAFETY RAIL — a form with a required gap is NEVER submitted
# ===========================================================================
class _DriftPage(FakePage):
    """A form that RENAMED a handle between recon and fill — the selector-drift
    case the executor must survive without submitting a half-filled form."""

    def __init__(self, *args, hide: str = "", **kwargs):
        super().__init__(*args, **kwargs)
        self._hide = hide
        self._recon_done = False

    def query_selector(self, css: str):
        if self._recon_done and self._hide and self._hide in css:
            return None
        return super().query_selector(css)


def test_required_field_that_cannot_be_located_is_never_submitted():
    """The element recon found is gone by fill time → park, do not submit."""
    page = _DriftPage(
        (_FIX / "ashby_form.html").read_text(encoding="utf-8"), url=_ASHBY_URL,
        next_html=(_FIX / "confirmation.html").read_text(encoding="utf-8"),
        hide="_systemfield_email")
    ex = _executor()
    fields = ex._recon_live_fields(page, _ASHBY_URL)
    page._recon_done = True   # the email handle disappears AFTER recon

    res = ex._drive_page(page, _ASHBY_URL, _CANONICAL_ANSWERS,
                         ("resume.pdf", _PDF), _APP, ex._fill_fields)

    assert page.clicks == 0, "a form with an unfillable required field was SUBMITTED"
    assert res["status"] == SUBMIT_FAILED and res["error"] == "needs_input"
    assert res["field"] == "email"                 # the EXACT field is named
    assert res["reason"] == "field_not_on_page"
    assert res["submitted"] is False
    assert {"email"} == {g["field"] for g in res["gaps"]}
    assert {q["field_id"] for q in fields} >= {"full_name", "email"}


def test_required_field_with_no_answer_is_never_submitted():
    """No vetted value → never guessed, and never submitted half-filled."""
    page = _page("ashby_form.html", _ASHBY_URL)
    ex = _executor()
    answers = {k: v for k, v in _CANONICAL_ANSWERS.items() if k != "email"}

    res = ex._drive_page(page, _ASHBY_URL, answers, ("resume.pdf", _PDF),
                         _APP, ex._fill_fields)

    assert page.clicks == 0
    assert res["error"] == "needs_input" and res["field"] == "email"
    assert res["reason"] == "unresolved"
    # The name that DID resolve was still filled — the stop is about the gap.
    assert _values(page)["_systemfield_name"] == "Ada Lovelace"


def test_a_form_that_wants_a_resume_is_never_submitted_without_one():
    """The third line of the production error. No résumé bytes resolved → stop."""
    page = _page("ashby_form.html", _ASHBY_URL)
    ex = _executor()

    res = ex._drive_page(page, _ASHBY_URL, _CANONICAL_ANSWERS, None,
                         _APP, ex._fill_fields)

    assert page.clicks == 0
    assert res["error"] == "needs_input" and res["field"] == "resume"
    assert res["reason"] == "resume_missing"


def test_a_form_where_nothing_could_be_filled_is_never_submitted():
    """THE SPAM-FLAG CLASS. Two live Ashby postings answered our empty
    submissions with "Your application submission was flagged as possible
    spam" — a pristine form clicking Submit is maximally bot-like. Whatever the
    reason nothing landed (wrong vocabulary, template drift, failed recon), an
    EMPTY form must never reach an employer."""
    page = _page("ashby_form.html", _ASHBY_URL)
    ex = _executor()

    # Answers that match no field on this form at all (the pre-fix state).
    res = ex._drive_page(page, _ASHBY_URL, {"totally_unknown_key": "x"},
                         ("resume.pdf", _PDF), _APP, ex._fill_fields)

    assert page.clicks == 0, "an EMPTY form was submitted to an employer"
    assert res["error"] == "needs_input" and res["submitted"] is False
    assert "no_fields_filled" in {g["reason"] for g in res["gaps"]}


def test_the_no_fields_filled_backstop_survives_a_failed_recon():
    """The backstop must not depend on recon: if the adapter cannot read the
    form, we still refuse to submit an empty one."""
    page = _page("ashby_form.html", _ASHBY_URL)
    ex = _executor()
    ex._recon_live_fields = lambda *a, **k: []      # recon totally unavailable

    res = ex._drive_page(page, _ASHBY_URL, {"full_name": "Ada"},
                         ("resume.pdf", _PDF), _APP, ex._fill_fields)

    assert page.clicks == 0
    assert [g["reason"] for g in res["gaps"]] == ["no_fields_filled"]


# ---------------------------------------------------------------------------
# The Greenhouse mask: `validation: country required/invalid`
# ---------------------------------------------------------------------------
# Aithrex-owned synthetic modern-Greenhouse form. The country control is
# React-rendered: it carries `name="country"` and a GENERATED id, so the old
# `#country` guess misses an element that is plainly there — and the employer
# reports a missing-country validation instead of a missing-name one. (The
# executor's own error string, `validation: <id or name> required/invalid`,
# is what named "country" in production.)
_GH_REACT_COUNTRY = """<!doctype html><html><body>
<form id="application_form" action="https://boards.greenhouse.io/aithrex/jobs/77">
  <label for="first_name">First Name</label>
  <input id="first_name" name="first_name" type="text" required>
  <label for="last_name">Last Name</label>
  <input id="last_name" name="last_name" type="text" required>
  <label for="email">Email</label>
  <input id="email" name="email" type="email" required>
  <label for="q_9a71c">Country</label>
  <select id="q_9a71c" name="country" required>
    <option>United States</option><option>India</option><option>Canada</option>
  </select>
  <input id="resume" name="resume" type="file">
  <button type="submit">Submit application</button>
</form></body></html>"""


def test_greenhouse_country_with_a_generated_id_is_still_filled():
    """Greenhouse is NOT immune: only the fields whose id happens to equal the
    canonical key ever worked. A country control with a generated id was
    skipped, option matching never ran, and the form failed validation."""
    ex = _executor()
    page = FakePage(_GH_REACT_COUNTRY, url=_GH_URL)

    # Pre-fix path: `#country` addresses nothing on this form.
    assert page.query_selector("#country") is None

    field_map = ex._field_selectors(ex._recon_live_fields(page, _GH_URL))
    assert field_map["country"] == '[id="q_9a71c"]'
    ex._fill_fields(page, {**_CANONICAL_ANSWERS, "country": "India"}, field_map)

    assert _values(page)["q_9a71c"] == "India"
    assert _values(page)["first_name"] == "Ada"


def test_a_required_field_addressable_only_by_name_still_fills():
    """Belt and braces for the same mask: even with recon unavailable, the
    answer key is tried as a live id AND as a name before the `#id` guess."""
    ex = _executor()
    page = FakePage(_GH_REACT_COUNTRY.replace(' id="q_9a71c"', ""), url=_GH_URL)

    filled, skipped = ex._fill_fields(page, {"country": "Canada"}, {})

    assert filled == 1 and skipped == []
    assert _values(page)["country"] == "Canada"


def test_required_country_that_cannot_be_filled_parks_instead_of_failing():
    """And when it genuinely cannot be filled, the employer never sees the
    half-filled form — the row parks naming `country`."""
    ex = _executor()
    page = FakePage(_GH_REACT_COUNTRY, url=_GH_URL,
                    next_html=(_FIX / "confirmation.html").read_text(encoding="utf-8"))
    answers = {k: v for k, v in _CANONICAL_ANSWERS.items() if k != "country"}

    res = ex._drive_page(page, _GH_URL, answers, ("resume.pdf", _PDF),
                         _APP, ex._fill_fields)

    assert page.clicks == 0
    assert res["error"] == "needs_input" and res["field"] == "country"
    assert res["reason"] == "unresolved"


# ---------------------------------------------------------------------------
# Composition with the blind-pick refusal (test_blind_option_pick_refused.py)
# ---------------------------------------------------------------------------
_GH_COMBOBOX = """<!doctype html><html><body>
<form id="application_form" action="https://boards.greenhouse.io/aithrex/jobs/88">
  <label for="first_name">First Name</label>
  <input id="first_name" name="first_name" type="text" required>
  <div class="select__control">
    <label for="q_auth">Are you legally authorized to work in the US?</label>
    <input id="q_auth" name="q_auth" class="select__input" role="combobox" required>
  </div>
  <button type="submit">Submit application</button>
</form></body></html>"""


class _SubmitCountingPage(FakePage):
    """Counts SUBMIT clicks only — driving a combobox legitimately clicks the
    control itself, which `FakePage.clicks` also counts."""

    submit_clicks = 0

    def _on_click(self, element):
        node = element._node
        typ = (node.get("type") or "").lower()
        if ((node.tag == "button" and typ in ("", "submit"))
                or (node.tag == "input" and typ == "submit")):
            self.submit_clicks += 1
        super()._on_click(element)


def test_a_refused_blind_pick_on_a_required_field_parks_instead_of_submitting():
    """`_fill_select` REFUSES to blind-pick an option for a gated field rather
    than inventing a legal attestation. That refusal only protects the user if
    the drive then STOPS: the refused field lands in `skipped`, and the
    pre-submit gate turns it into a park — it must never be counted as filled
    and submitted."""
    ex = _executor()
    page = _SubmitCountingPage(
        _GH_COMBOBOX, url=_GH_URL,
        next_html=(_FIX / "confirmation.html").read_text(encoding="utf-8"))

    res = ex._drive_page(page, _GH_URL,
                         {"first_name": "Ada", "work_authorized_us": "Yes"},
                         None, _APP, ex._fill_fields)

    assert page.submit_clicks == 0, "a fabricated legal attestation was submitted"
    assert res["error"] == "needs_input"
    assert res["field"] == "work_authorized_us"
    assert res["reason"] == "field_not_on_page"      # we had a value, could not place it
    # The refusal is what put it there: the field is reported skipped, not filled.
    # (The combobox's own text was type-FILTERED, never committed to an option —
    # on a real react-select only clicking an option commits a value.)
    assert "work_authorized_us" in res["skipped"]


# ---------------------------------------------------------------------------
# The lifecycle mask: "Target page, context or browser has been closed"
# ---------------------------------------------------------------------------
class _ClosedPage(FakePage):
    """A page whose context died under the drive — every probe raises the
    Playwright closed-target error, exactly as seen in production."""

    _ERR = "Page.query_selector: Target page, context or browser has been closed"

    def __init__(self, *args, dead_after: int = 0, **kwargs):
        super().__init__(*args, **kwargs)
        self._probes = 0
        self._dead_after = dead_after

    def _die(self):
        self._probes += 1
        if self._probes > self._dead_after:
            raise RuntimeError(self._ERR)

    def query_selector(self, css: str):
        self._die()
        return super().query_selector(css)

    def query_selector_all(self, css: str):
        self._die()
        return super().query_selector_all(css)


def test_a_closed_page_is_a_transient_unavailable_never_a_submit():
    """The Harvey mask. A dead browser must not look like a needs_input park
    (the user has no question to answer) and must never click Submit."""
    page = _ClosedPage((_FIX / "ashby_form.html").read_text(encoding="utf-8"),
                       url=_ASHBY_URL)
    ex = _executor()

    res = ex._drive_page(page, _ASHBY_URL, _CANONICAL_ANSWERS,
                         ("resume.pdf", _PDF), _APP, ex._fill_fields)

    assert res["status"] == EXECUTOR_UNAVAILABLE      # TRANSIENT — retry later
    assert page.clicks == 0
    assert res.get("error") is None


def test_submit_maps_a_mid_drive_browser_death_to_transient_unavailable():
    """Same fault raised from deeper in the drive (e.g. at the Submit click):
    surfaced as transient, never as an opaque `failed`."""
    ex = _executor()

    def _boom(*_a, **_k):
        raise RuntimeError("Page.click: Target page, context or browser "
                           "has been closed")

    ex._drive_ashby = _boom
    res = ex.submit(_APP, _CANONICAL_ANSWERS, None)

    assert res["status"] == EXECUTOR_UNAVAILABLE
    assert res["confirmation"] is None and res["wall"] is None


def test_drive_page_never_touches_the_page_after_teardown():
    """The split of `_drive` must keep every page read INSIDE the browser's
    lifetime: `_drive` returns the fully-materialised result of `_drive_page`
    and only then runs `finally: browser.close()`."""
    import inspect
    from services import browser_executor as be

    src = inspect.getsource(be.BrowserExecutor._drive)
    body = src.split("finally:")[0]
    assert "return self._drive_page(" in body          # computed before teardown
    assert "browser.close()" in src.split("finally:")[1]
    # Nothing after the teardown may read the page.
    assert "page." not in src.split("finally:")[1]


def test_dry_run_still_prepares_and_reports_the_gaps():
    """Copilot/ops dry-run is not a failure: it prepares, never clicks, and now
    hands back the gaps it found."""
    page = _page("ashby_form.html", _ASHBY_URL)
    ex = _executor()

    res = ex._drive_page(page, _ASHBY_URL, _CANONICAL_ANSWERS, None,
                         {**_APP, "dry_run": True}, ex._fill_fields)

    assert res["status"] == "prepared" and res["submitted"] is False
    assert page.clicks == 0
    assert [g["field"] for g in res["gaps"]] == ["resume"]


# ===========================================================================
# 4. The wire contract — a pre-submit stop parks needs_input, never `failed`
# ===========================================================================
class _BlockedExecutor:
    """An executor whose drive stopped before Submit (the shape above)."""

    def is_available(self):
        return True

    def submit(self, app, answers, resume_pdf):
        return {"status": SUBMIT_FAILED, "confirmation": None,
                "screenshot_ref": "s3://audit/proof.png", "wall": None,
                "error": "needs_input", "field": "email",
                "reason": "field_not_on_page", "submitted": False}


def test_presubmit_stop_parks_the_row_in_the_one_tap_queue():
    user = "ada@aspirely.me"
    db = current_db()
    for coll in ("applications", "auto_apply_answers", "auto_submit_audit"):
        db[coll].delete_many({})
    db.applications.create_index([("application_id", 1)], unique=True,
                                 name="application_id_unique")
    view, _ = als.create_application(
        user, company="Aithrex", title="Backend Engineer",
        jd_text="We are hiring a backend engineer to build systems in Python.",
        source="ashby", ats_family="ashby", apply_url=_ASHBY_URL)
    aid = view["application_id"]
    als.mark_gathering(user, aid)
    als.mark_ready_for_review(user, aid, autonomy="auto", confidence_score=0.95,
                              tailoring_record_id=None)
    aas._system_approve(user, aid)
    aas.set_executor(_BlockedExecutor())

    res = aas.submit(aid, user)

    assert res["status"] == als.STATUS_NEEDS_INPUT
    assert res["field"] == "email"
    row = als.get_application(user, aid)
    assert row["status"] == als.STATUS_NEEDS_INPUT      # NOT `failed`
    assert [i["field_id"] for i in row["needs_input"]] == ["email"]
