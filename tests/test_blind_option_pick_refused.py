"""A combobox we cannot match must ASK, never guess.

`_fill_select` ends in two blind fallbacks — "click the first filtered option"
and ArrowDown+Enter. They exist for location autocompletes, where every option is
a competing spelling of one place ("San Francisco, CA, USA").

On anything else they are a fabrication, and a silent one: `_fill_select`
returned normally, so the caller counted the field as `filled`, the pre-submit
gap check saw nothing missing, and the form went to a real employer carrying an
arbitrary answer. On a Greenhouse work-authorization or sponsorship combobox
that is an invented LEGAL ATTESTATION — the same shape as the two fabrication
paths already deleted from browser_executor (`_WD_DEFAULTS`, and the
validation-retry that blind-clicked the first element reading "No").

This pins the refusal, and pins that it stays narrow enough to leave the
location autocomplete working.
"""
import pytest

import services.browser_executor as be


class TestForbiddenSet:
    @pytest.mark.parametrize("field_id", [
        "work_authorized_us", "requires_sponsorship", "citizenship_countries",
        "criminal_history",
        "gender", "race_ethnicity", "veteran_status", "disability_status",
    ])
    def test_gated_fields_refuse_a_blind_pick(self, field_id):
        assert be._blind_pick_forbidden(field_id) is True

    @pytest.mark.parametrize("field_id", [
        "has_security_clearance", "current_salary",
    ])
    def test_sensitive_but_ungated_fields_also_refuse(self, field_id):
        """These are `sensitive`, NOT `gated` — an is_gated-only check would
        have let them through, and "first option" on a clearance dropdown
        claims a clearance the user may not hold."""
        assert be._blind_pick_forbidden(field_id) is True

    @pytest.mark.parametrize("field_id", ["country", "state", "work_auth_country"])
    def test_enumerated_geography_refuses_a_blind_pick(self, field_id):
        """"First option" on a country list is simply a different country than
        the one the user declared."""
        assert be._blind_pick_forbidden(field_id) is True

    @pytest.mark.parametrize("field_id", [
        "location", "how_did_you_hear", "referral_name",
    ])
    def test_free_form_fields_still_allow_the_fallback(self, field_id):
        """The narrowness matters: over-forbidding turns every unmatched
        autocomplete into a needs_input and buries the user in questions."""
        assert be._blind_pick_forbidden(field_id) is False


class TestFailsClosed:
    def test_an_unknown_field_id_is_allowed(self):
        """An unrecognised id is not gated and not enumerated, so the location
        fallback still applies — this is the deliberate default."""
        assert be._blind_pick_forbidden("some_vendor_custom_q_1234") is False

    def test_empty_field_id_refuses(self):
        assert be._blind_pick_forbidden("") is True

    def test_refuses_when_the_question_bank_cannot_be_read(self, monkeypatch):
        """Fail CLOSED. A missed fill parks the row and asks; a wrong one is
        submitted to an employer.

        Note `from services import application_question_bank` calls __import__
        with name="services" and the real module in `fromlist` — matching on
        `name` alone silently never fires and the test passes for the wrong
        reason.
        """
        import builtins
        real_import = builtins.__import__

        def boom(name, *a, **kw):
            fromlist = kw.get("fromlist") or (a[2] if len(a) > 2 else ()) or ()
            if "application_question_bank" in name or \
                    "application_question_bank" in tuple(fromlist):
                raise ImportError("simulated shared-layer build race")
            return real_import(name, *a, **kw)

        monkeypatch.setattr(builtins, "__import__", boom)
        # Even a normally-ALLOWED field must refuse when we cannot check it.
        assert be._blind_pick_forbidden("how_did_you_hear") is True
        assert be._blind_pick_forbidden("work_authorized_us") is True


class _Loc:
    """A locator that never matches — i.e. no option text equals the declared
    value, which is exactly the situation the blind fallbacks existed for."""

    def count(self):
        return 0

    def is_visible(self):
        return False

    def click(self, **kw):  # pragma: no cover - unreachable when count()==0
        raise AssertionError("clicked an option that does not match")

    @property
    def first(self):
        return self


class _Keyboard:
    def __init__(self, page):
        self._page = page

    def press(self, key):
        self._page.key_presses.append(key)


class _ComboPage:
    """Minimal combobox page. `FakePage` has no locator/keyboard, so a combobox
    driven through it is skipped for the WRONG reason (AttributeError) and could
    not prove anything about the guard."""

    def __init__(self):
        self.key_presses = []
        self.clicks = []
        self.keyboard = _Keyboard(self)

    def query_selector(self, sel):
        return None                      # not a native <select>

    def click(self, sel, **kw):
        self.clicks.append(sel)

    def fill(self, sel, val):
        pass

    def wait_for_selector(self, sel, **kw):
        raise Exception("no options rendered")

    def locator(self, sel):
        return _Loc()


class TestFillSelectHonoursTheGuard:
    """The integration point. Testing `_blind_pick_forbidden` alone passes even
    if `_fill_select` never calls it — that mutation has to fail here."""

    def _executor(self):
        return be.BrowserExecutor.__new__(be.BrowserExecutor)

    def test_a_gated_combobox_refuses_instead_of_guessing(self):
        page = _ComboPage()
        with pytest.raises(be._BlindPickRefused):
            self._executor()._fill_select(
                page, "#auth", "Yes", field_id="work_authorized_us")
        # The whole point: nothing was picked.
        assert page.key_presses == [], \
            "pressed ArrowDown/Enter on a legal attestation"

    def test_an_enumerated_combobox_refuses(self):
        page = _ComboPage()
        with pytest.raises(be._BlindPickRefused):
            self._executor()._fill_select(
                page, "#country", "Narnia", field_id="country")
        assert page.key_presses == []

    def test_a_safe_combobox_still_falls_back(self):
        """Narrowness check — the location autocomplete must keep working, or
        every unmatched dropdown becomes a needs_input."""
        page = _ComboPage()
        self._executor()._fill_select(
            page, "#loc", "San Francisco", field_id="location")
        assert page.key_presses == ["ArrowDown", "Enter"]


class TestRefusalShape:
    def test_the_exception_names_the_field(self):
        e = be._BlindPickRefused("requires_sponsorship")
        assert e.field_id == "requires_sponsorship"
        assert "requires_sponsorship" in str(e)

    def test_it_is_an_ordinary_exception_so_fill_treats_it_as_skipped(self):
        """`_fill_fields` wraps each field in `except Exception -> skipped`, so
        the refusal has to be a plain Exception subclass to land the field in
        the gap list rather than crashing the drive."""
        assert issubclass(be._BlindPickRefused, Exception)
