"""Regression suite for a LIVE production loop: the react-select `country`.

WHAT HAPPENED (prod, 2026-07-28)
--------------------------------
Two rows re-parked on the SAME question over and over — Adyen six times,
EnergyHub four — with the row's own answer sheet already holding
``country: "United States of America"`` at ``coverage: 1.0``.

``/aws/lambda/jobpilot-apply-executor-prod`` says exactly why, once per drive::

    fill_select: no option matched 'United States of America' for country
        — refusing the blind fallback on a gated/enumerated field
    fill skip country: blind option pick refused for 'country'
    browser submit BLOCKED pre-submit: 2 required gap(s),
        first=country reason=field_not_on_page (filled=6)

Modern Greenhouse renders its enumerated controls as react-select comboboxes:
an ``<input role="combobox" class="select__input">`` whose choices do not exist
in the DOM until the listbox is OPENED. So recon reported ``type: text`` with no
options, and ``_fill_select`` typed a filter string and tried to click an option
by text — into a listbox that was never opened. Nothing matched, the (correct)
refusal fired, and the row parked naming ``country``.

The refusal was doing its job. What was broken was that we could neither READ
nor SELECT a react-select. This suite pins the repair:

1. the premise — recon genuinely cannot see the options (mutation guard);
2. the fix — open the listbox, read the labels, match them through
   ``application_question_bank.match_option``, click the match;
3. the safety that must survive it — same-cluster-only geography (never a
   neighbouring country), and a value the form does not offer still REFUSES and
   parks instead of guessing.

Fully offline: the lxml ``tests/ats_dom`` harness over an Aithrex-owned static
fixture. Zero browser, zero network, zero AWS.
"""
import pathlib

import pytest

from ats_dom import LocatorPage
from services.ats_adapters import reset_circuit_breakers, select_adapter
import services.browser_executor as be
from services.browser_executor import BrowserExecutor
from services.executor_adapter import SUBMIT_SUCCESS

_FIX = pathlib.Path(__file__).parent / "fixtures" / "ats"
_FIXTURE = _FIX / "greenhouse_react_select.html"
_GH_URL = "https://boards.greenhouse.io/aithrex/jobs/4200"
_PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF"

_APP = {"application_id": "app1", "apply_url": _GH_URL, "ats_family": "greenhouse"}


@pytest.fixture(autouse=True)
def _reset():
    reset_circuit_breakers()
    yield
    reset_circuit_breakers()


def _executor():
    return BrowserExecutor(
        resume_resolver=lambda app, ref, ue: ("resume.pdf", _PDF),
        screenshot_sink=lambda app, png: "s3://audit/proof.png",
        user_email="ada@aspirely.me",
        headless=True,
    )


def _page(confirm: bool = False) -> LocatorPage:
    # LocatorPage, not FakePage: `_fill_select`'s substring (`:has-text`) option
    # click and its blind fallbacks all go through `page.locator(...)`. On a page
    # without one they raise AttributeError into an `except: continue` and the
    # tests below could not tell a refusal from an unreachable code path.
    return LocatorPage.from_file(
        _FIXTURE, url=_GH_URL,
        next_file=(_FIX / "confirmation.html") if confirm else None)


def _value(page: LocatorPage, handle: str):
    el = page.query_selector('[id="%s"]' % handle)
    return None if el is None else el.get_attribute("value")


def _map(ex, page):
    return ex._field_selectors(ex._recon_live_fields(page, _GH_URL))


# ===========================================================================
# 1. The premise + the mutation guard
# ===========================================================================
def test_recon_cannot_see_a_react_selects_options():
    """The production item shape, reproduced exactly: `country` comes back as a
    REQUIRED text field with no options, because in the DOM it IS an <input> and
    the choices do not exist yet. Any fix that relies on recon-supplied options
    is fixing the wrong layer."""
    questions = {q["field_id"]: q for q in
                 select_adapter(_page(), _GH_URL).recon()}

    country = questions["country"]
    assert country["type"] == "text"
    assert country.get("options") is None
    assert country["required"] is True
    # And the form's own choices are genuinely in the document — just closed.
    assert _page().query_selector_all(".select__option")
    assert not [o for o in _page().query_selector_all(".select__option")
                if o.is_visible()], "the listbox is supposed to start closed"


# ===========================================================================
# 2. The fix — open, read, match, click
# ===========================================================================
def test_the_declared_country_is_selected_from_the_opened_listbox():
    """The whole repair in one assertion. The user declared "United States";
    the form only offers "United States of America". Nothing can be typed into a
    react-select — the value is committed by CLICKING the row whose label the
    question bank says means the declared value."""
    ex, page = _executor(), _page()

    filled, skipped = ex._fill_fields(
        page, {"first_name": "Ada", "email": "ada@aspirely.me",
               "country": "United States"}, _map(ex, page))

    assert _value(page, "country") == "United States of America"
    assert skipped == []
    assert filled == 3


def test_the_open_listbox_is_actually_read_not_guessed():
    """`_rendered_options` is the new load-bearing step: with the listbox open
    it returns the form's OWN spellings, which is what makes `match_option`
    possible at all."""
    ex, page = _executor(), _page()
    opts = ex._open_listbox(page, '[id="country"]')

    assert [t for t, _ in opts] == [
        "British Indian Ocean Territory", "Canada", "India",
        "United Kingdom of Great Britain and Northern Ireland",
        "United States of America",
    ]


def test_a_gated_screening_combobox_is_answered_from_its_own_options():
    """This was not a country-only outage: the same drive refused
    work_authorized_us / visa_status / gender / veteran_status. A declared
    boolean now lands on the form's own "Yes"."""
    ex, page = _executor(), _page()

    filled, skipped = ex._fill_fields(
        page, {"work_authorized_us": True}, _map(ex, page))

    assert _value(page, "question_100") == "Yes"
    assert (filled, skipped) == (1, [])


# ---------------------------------------------------------------------------
# The opener itself. In production the listbox NEVER rendered: the executor's
# 5 000 ms `wait_for_selector` for `.select__option` timed out on every gated
# field (consecutive refusals are 5.1 s apart in the CloudWatch stream). Clicking
# the input is not a reliable opener — react-select's input is a ~2 px grid cell
# inside `.select__input-container`. So the fallbacks are the load-bearing part,
# and each gets its own page that refuses every OTHER opener.
# ---------------------------------------------------------------------------
class _ToggleOnlyPage(LocatorPage):
    """Clicking the combobox input does nothing; only the vendor's own
    "Toggle flyout" button opens the listbox."""

    def _react_select_click(self, element):
        node = element._node
        if node.tag == "input":
            return True                 # consumed, but the menu stays closed
        return super()._react_select_click(element)


class _Keyboard:
    def __init__(self, page):
        self._page = page
        self.presses = []

    def press(self, key):
        self.presses.append(key)
        if key == "ArrowDown" and self._page.focused is not None:
            LocatorPage._react_select_click(
                self._page, self._page.focused)


class _KeyboardOnlyPage(LocatorPage):
    """Neither the input nor the toggle button opens it — only ArrowDown on the
    focused input, which is react-select's own keyboard opener."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.focused = None
        self.keyboard = _Keyboard(self)

    def focus(self, selector):
        super().focus(selector)
        self.focused = self.query_selector(selector)

    def _react_select_click(self, element):
        classes = (element._node.get("class") or "").split()
        if "select__option" in classes:
            return super()._react_select_click(element)   # committing still works
        return True                     # every OPENING click is swallowed


@pytest.mark.parametrize("page_cls", [_ToggleOnlyPage, _KeyboardOnlyPage])
def test_a_listbox_the_input_click_will_not_open_is_still_opened(page_cls):
    ex = _executor()
    page = page_cls.from_file(_FIXTURE, url=_GH_URL)

    ex._fill_fields(page, {"country": "United States"}, _map(ex, page))

    assert _value(page, "country") == "United States of America"


# ===========================================================================
# 3. The safety that has to survive the fix
# ===========================================================================
def test_geography_stays_same_cluster_and_never_takes_a_substring_neighbour():
    """The trap the old `:has-text('India')` fallback walked into: on the real
    (alphabetical) list "British Indian Ocean Territory" comes FIRST, CONTAINS
    "India", and is a different country. Only the exact cluster may win."""
    ex, page = _executor(), _page()

    ex._fill_fields(page, {"country": "India"}, _map(ex, page))

    assert _value(page, "country") == "India"


def test_a_country_this_form_does_not_offer_still_refuses():
    """Reading the options must not become a licence to pick one. No option in
    the declared value's cluster → refuse → the field lands in `skipped` and the
    row asks, exactly as before the fix."""
    ex, page = _executor(), _page()

    with pytest.raises(be._BlindPickRefused):
        ex._fill_select(page, '[id="country"]', "Japan", field_id="country")

    # The input may hold the text we TYPED to filter the listbox (that is what a
    # react-select's input is for); what must never happen is a committed
    # option. None of the form's own choices was selected.
    offered = [o.inner_text() for o in page.query_selector_all(".select__option")]
    assert _value(page, "country") not in offered


def test_an_unofferable_answer_parks_the_row_instead_of_submitting():
    """And that refusal still stops the drive BEFORE Submit."""
    ex, page = _executor(), _page(confirm=True)

    res = ex._drive_page(
        page, _GH_URL,
        {"first_name": "Ada", "email": "ada@aspirely.me", "phone": "5551234567",
         "country": "Japan", "work_authorized_us": True},
        ("resume.pdf", _PDF), _APP, ex._fill_fields)

    assert res["error"] == "needs_input"
    assert res["field"] == "country"
    assert "country" in res["skipped"]
    assert res.get("submitted") is False


# ===========================================================================
# 4. End to end — the drive that used to loop now submits
# ===========================================================================
def test_the_drive_that_used_to_park_forever_now_clears_the_presubmit_gate():
    """Same answer sheet the production row carried. Before the fix this drive
    reported `needs_input`/`field_not_on_page` on `country` every single time;
    now both comboboxes commit, the gate finds no gap and the application is
    actually submitted."""
    ex, page = _executor(), _page(confirm=True)

    res = ex._drive_page(
        page, _GH_URL,
        {"first_name": "Ada", "email": "ada@aspirely.me", "phone": "5551234567",
         "country": "United States", "work_authorized_us": True},
        ("resume.pdf", _PDF), _APP, ex._fill_fields)

    assert res["status"] == SUBMIT_SUCCESS, res
    assert res.get("error") in (None, "")
    assert "submitted" in (res.get("confirmation") or "").lower()
