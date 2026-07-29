"""GEOGRAPHY option matching — regression tests for a live production failure.

INCIDENT
--------
A real application failed with ``validation: country required/invalid`` and the
user was told "We couldn't finish this application. Nothing was submitted."
The user's declared ``user_preferences.country`` is ``"United States"``; the
form's option label is ``"United States of America"``. Country is an
ENUMERATION on every ATS, but the catalog types it as free text, and the legacy
Greenhouse/Lever/Ashby filler in ``browser_executor`` wrote the declared value
VERBATIM into the select — so the browser rejected it and the whole submission
died on a spelling difference.

WHAT IS PINNED HERE
-------------------
  * "United States" matches a "United States of America" option — and the
    REVERSE, and the abbreviations ("USA", "US", "U.S."), and "UK" / "Great
    Britain" for the United Kingdom;
  * matching is against the FORM'S OWN options, via the shared question-bank
    matcher, so no adapter needs a hardcoded vendor label list;
  * THE SAFETY BOUNDARY (AUTOAPPLY_REPAIR_SPEC §0): this is option MATCHING of a
    value the user DECLARED, never value INVENTION. A declared country with no
    matching option parks at ``needs_input`` — it is never guessed, never
    silently defaulted to the US, and never rounded onto a different country
    that merely shares words ("Guinea" is not "Papua New Guinea").
  * the live user is an F-1 OPT visa holder with ``citizenship_countries:
    "India"``. A US default on either field would be a FALSE legal statement to
    an employer. An end-to-end drive pins that it stays India.

Zero browser, zero network, zero LLM: the real adapters run over Aithrex-owned
synthetic HTML through the lxml ``tests/ats_dom`` harness, with the answer set
resolved from a seeded profile by the real ``answer_library_service``
(mongomock). NEVER a real employer, never a live submission.
"""
import pathlib

import pytest

from _vg_support import current_db

from ats_dom import FakePage
from services import application_question_bank as qb
from services import browser_executor
from services import fill_driver
from services.ats_adapters import GreenhouseAdapter, reset_circuit_breakers
from services.fill_driver import (
    FILL_CONFIRMED, FILL_NEEDS_INPUT, MODE_APPROVAL,
)

_BE_SRC = (pathlib.Path(__file__).parent.parent / "shared" / "python"
           / "services" / "browser_executor.py")

USER = "opt.holder@aspirely.me"
_GH_URL = "https://boards.greenhouse.io/aithrex/jobs/4200"

_CONFIRMATION = """<!doctype html><html><body>
<div id="application-confirmation">Thank you for applying. Your application has
been submitted.</div></body></html>"""


def _form(country_options, *, citizenship_options=None) -> str:
    """Aithrex-owned synthetic Greenhouse-style form with a country DROPDOWN
    (which is what every real ATS renders). NEVER a real employer."""
    def _opts(labels):
        return "\n".join('    <option value="%d">%s</option>' % (i, label)
                         for i, label in enumerate(labels, start=1))

    citizenship = ""
    if citizenship_options is not None:
        citizenship = """
  <label for="citizenship">Country of citizenship</label>
  <select id="citizenship" name="citizenship" required>
    <option value=""></option>
%s
  </select>""" % _opts(citizenship_options)

    return """<!doctype html><html><body>
<form id="application_form">
  <label for="first_name">First Name</label>
  <input id="first_name" name="first_name" type="text" required>
  <label for="last_name">Last Name</label>
  <input id="last_name" name="last_name" type="text" required>
  <label for="email">Email</label>
  <input id="email" name="email" type="email" required>
  <label for="country">Country</label>
  <select id="country" name="country" required>
    <option value=""></option>
%s
  </select>%s
  <div id="application-confirmation" style="display:none"></div>
  <button type="submit">Submit application</button>
</form>
</body></html>""" % (_opts(country_options), citizenship)


def _adapter(country_options, **kw):
    page = FakePage(_form(country_options, **kw), url=_GH_URL,
                    next_html=_CONFIRMATION)
    return GreenhouseAdapter(page, _GH_URL), page


@pytest.fixture(autouse=True)
def _reset():
    reset_circuit_breakers()
    d = current_db()
    for c in ("resume_assets", "users", "user_preferences", "visa_profiles",
              "application_answer_library"):
        d[c].delete_many({})
    yield
    reset_circuit_breakers()


# ===========================================================================
# The exact production failure, at the matcher
# ===========================================================================
class TestCountrySpellings:
    def test_declared_united_states_matches_united_states_of_america_option(self):
        """THE incident: declared "United States", form says "United States of
        America". Before the fix this fell through and the raw value was typed
        into the select, which the browser rejected as invalid."""
        m = qb.match_option("country", "United States",
                            ["Canada", "India", "United States of America"])
        assert m is not None and m["option"] == "United States of America"

    def test_declared_united_states_of_america_matches_united_states_option(self):
        """The reverse spelling. This one NEVER worked — token containment only
        scored 0.47 (below the 0.60 floor), so the field died silently."""
        m = qb.match_option("country", "United States of America",
                            ["Canada", "India", "United States"])
        assert m is not None and m["option"] == "United States"

    @pytest.mark.parametrize("declared", [
        "USA", "US", "U.S.", "U.S.A.", "America", "united states",
        "  United States  ",
    ])
    def test_us_abbreviations_all_reach_the_form_label(self, declared):
        m = qb.match_option("country", declared,
                            ["India", "United States of America"])
        assert m is not None and m["option"] == "United States of America"

    @pytest.mark.parametrize("declared,option", [
        ("United Kingdom", "United Kingdom of Great Britain and Northern Ireland"),
        ("UK", "United Kingdom"),
        ("U.K.", "United Kingdom"),
        ("Great Britain", "United Kingdom"),
        ("England", "United Kingdom"),
        ("India", "India"),
        ("Republic of India", "India"),
        ("Canada", "CAN"),
        ("Deutschland", "Germany"),
        ("Holland", "Netherlands"),
    ])
    def test_common_country_synonyms(self, declared, option):
        m = qb.match_option("country", declared, [option, "Chile", "Japan"])
        assert m is not None and m["option"] == option

    @pytest.mark.parametrize("declared,option", [
        ("Cote d'Ivoire", "Côte d'Ivoire"),
        ("Côte d'Ivoire", "Cote d Ivoire"),
        ("Turkiye", "Türkiye"),
        ("Espana", "España"),
    ])
    def test_diacritics_and_punctuation_are_normalized(self, declared, option):
        m = qb.match_option("country", declared, [option, "Japan"])
        assert m is not None and m["option"] == option

    def test_the_same_table_serves_every_geography_field(self):
        for field_id in ("country", "work_auth_country", "citizenship_countries"):
            assert qb.is_enumerated(field_id)
            m = qb.match_option(field_id, "USA",
                                ["India", "United States of America"])
            assert m is not None and m["option"] == "United States of America"

    @pytest.mark.parametrize("declared,option", [
        ("CA", "California"),
        ("California", "CA"),
        ("NY", "New York"),
        ("District of Columbia", "DC"),
        ("Ontario", "ON"),
    ])
    def test_state_and_province_abbreviations(self, declared, option):
        assert qb.is_enumerated("state")
        m = qb.match_option("state", declared, [option, "Texas", "TX"])
        assert m is not None and m["option"] == option


class TestPhoneWidgetDialCodeLabels:
    """A phone-widget country picker appends the DIAL CODE to every label.

    INCIDENT (prod, 2026-07-29). Greenhouse renders the phone fieldset's country
    control as a react-select whose rows read "United States +1". `_norm` folds
    that to "united states 1", which is a key in NO geo table, so the declared
    "United States of America" matched nothing. The executor refused the blind
    pick (correctly — spec §0 rule 1) and an application with 17 of its 18
    fields filled parked on that one control.

    Verified against the live form with a real browser: opening the listbox
    renders 244 options labelled "<Country> +<dial code>"; typing "United
    States" filters to exactly one, "United States +1". So the ONLY gap was the
    matcher — the driver reached the right option and could not name it.
    """

    @pytest.mark.parametrize("declared", [
        "United States of America", "United States", "USA", "US",
    ])
    def test_declared_us_matches_a_dial_code_label(self, declared):
        m = qb.match_option("country", declared,
                            ["United States +1", "India +91", "Canada +1"])
        assert m is not None and m["option"] == "United States +1"

    def test_it_works_for_the_users_own_country_too(self):
        """Not a US special case — the same normalization for any country."""
        m = qb.match_option("country", "India",
                            ["United States +1", "India +91", "Canada +1"])
        assert m is not None and m["option"] == "India +91"

    @pytest.mark.parametrize("declared,expected", [
        ("Niger", "Niger +227"),
        ("Nigeria", "Nigeria +234"),
    ])
    def test_dial_codes_do_not_blur_lookalike_countries(self, declared,
                                                        expected):
        """Stripping trailing digits must not make "Niger" reach "Nigeria"."""
        m = qb.match_option("country", declared,
                            ["Niger +227", "Nigeria +234"])
        assert m is not None and m["option"] == expected

    def test_an_absent_country_is_still_refused(self):
        """THE safety boundary: the dial-code suffix must not become a loophole
        that lets an unlisted country round onto a listed one."""
        assert qb.match_option(
            "country", "Bhutan", ["United States +1", "Canada +1"]) is None
        assert qb.match_option(
            "country", "India", ["United States +1", "Canada +1"]) is None

    def test_a_bare_number_never_matches_a_country(self):
        """A label that is ONLY a dial code carries no country — stripping it
        leaves nothing, which must match nothing rather than everything."""
        assert qb.match_option("country", "United States",
                               ["+1", "+91"]) is None

    def test_a_plain_label_is_unaffected(self):
        """The normalization is additive — it must not regress the ordinary
        no-dial-code case that already worked."""
        m = qb.match_option("country", "United States of America",
                            ["Canada", "India", "United States"])
        assert m is not None and m["option"] == "United States"


# ===========================================================================
# SAFETY: matching a declared value, never inventing one
# ===========================================================================
class TestNeverInventsAPlace:
    def test_unmatchable_country_returns_none_never_a_default(self):
        """No option means the same place → None → the caller asks. It must NOT
        fall back to the first option, nor to the US."""
        assert qb.match_option("country", "Bhutan",
                               ["United States of America", "Canada"]) is None
        assert qb.match_option("country", "India",
                               ["United States of America", "Canada"]) is None
        assert qb.match_option("country", "Atlantis",
                               ["United States of America"]) is None

    @pytest.mark.parametrize("declared,options", [
        # Fuzzy token overlap used to answer ALL of these with the wrong country.
        ("Guinea", ["Papua New Guinea", "Equatorial Guinea", "Guinea-Bissau"]),
        ("Sudan", ["South Sudan"]),
        ("Congo", ["Democratic Republic of the Congo"]),
        ("Dominica", ["Dominican Republic"]),
        ("Ireland", ["Northern Ireland", "United Kingdom"]),
        ("Korea", ["South Korea", "North Korea"]),
        ("China", ["Hong Kong", "Taiwan", "Macau"]),
        ("Niger", ["Nigeria"]),
    ])
    def test_a_country_is_never_rounded_onto_a_neighbour(self, declared, options):
        assert qb.match_option("country", declared, options) is None

    def test_option_candidates_only_respell_the_declared_value(self):
        """The blind-typing candidate list (for widgets whose options cannot be
        read) may only reword the SAME place."""
        us = qb.option_candidates("country", "United States")
        assert us[0] == "United States"                  # declared value first
        assert "United States of America" in us
        india = qb.option_candidates("country", "India")
        assert india[0] == "India"
        # Not one candidate for an Indian citizen may name another country.
        assert not any("united states" in c.lower() or "usa" in c.lower()
                       for c in india)
        # An unknown place gets no expansion at all — never a substitute.
        assert qb.option_candidates("country", "Atlantis") == ["Atlantis"]

    @pytest.mark.parametrize("label", [
        "Country of citizenship",
        "Country/Region of citizenship",
        "What is your country of citizenship?",
        "Citizenship country",
        "Countries of citizenship",
    ])
    def test_singular_citizenship_wording_is_not_the_residence_country(self, label):
        """A second fabrication path found while fixing this: the SINGULAR
        "Country of citizenship" fell through to the identity ``country`` field,
        so the live user's declared RESIDENCE ("United States") would have been
        submitted as their CITIZENSHIP. That is a false legal attestation."""
        assert qb.normalize_field(label, allow_llm=False)["field_id"] == \
            "citizenship_countries"

    def test_geo_alias_tables_have_no_cross_cluster_collisions(self):
        """Two different places may never claim the same spelling — that is what
        would let one country be filled in for another."""
        for clusters in (qb._COUNTRY_CLUSTERS, qb._REGION_CLUSTERS):
            claimed = {}
            for cluster in clusters:
                key = cluster[0]
                for alias in cluster:
                    a = qb._norm(alias)
                    assert claimed.setdefault(a, key) == key, \
                        "%r claimed by %r and %r" % (a, claimed[a], key)


# ===========================================================================
# The two seams that used to skip option matching entirely
# ===========================================================================
class _StubAdapter:
    """Minimal adapter surface: a control the recon typed as TEXT that
    nonetheless enumerates options (an autocomplete / datalist country box)."""

    def __init__(self, options):
        self._options = options

    def options_for(self, field_id):
        return list(self._options)


class TestOptionMatchingSeams:
    def test_a_text_typed_country_control_is_still_option_matched(self):
        """``fill_driver`` used to option-match ONLY select/radio widgets, so a
        country control the recon typed as text was filled verbatim. Geography is
        an enumeration whatever the widget says."""
        adapter = _StubAdapter(["India", "United States of America"])
        q = {"type": "text", "field_id": "country"}
        assert fill_driver._match_value(adapter, q, "country", "United States") \
            == "United States of America"
        # And an unmatchable one still stops the run rather than filling.
        assert fill_driver._match_value(adapter, q, "country", "Bhutan") \
            is fill_driver._NO_MATCH
        # A genuinely free-text field is untouched by any of this.
        assert fill_driver._match_value(
            _StubAdapter(["x"]), {"type": "text"}, "first_name", "Ada") == "Ada"

    def test_native_select_fills_the_forms_own_label(self):
        """The legacy Greenhouse/Lever/Ashby filler wrote the declared value
        straight into the select — the exact production failure."""
        chosen = {}

        class _Page:
            def query_selector_all(self, css):
                assert css == "#country option"
                return [type("O", (), {"inner_text": staticmethod(lambda t=t: t)})()
                        for t in ("", "India", "United States of America")]

            def select_option(self, sel, label=None):
                if label != "United States of America":
                    raise RuntimeError("no such option: %r" % label)
                chosen["label"] = label

        ex = browser_executor.BrowserExecutor.__new__(
            browser_executor.BrowserExecutor)
        ex._fill_native_select(_Page(), "#country", "country", "United States")
        assert chosen["label"] == "United States of America"

    def test_alias_expansion_is_vendor_neutral_not_hardcoded_in_the_executor(self):
        """The only alias expansion that existed was a Greenhouse-specific,
        hardcoded US list inside ``browser_executor._fill_select``. It is gone —
        every adapter now shares the question bank's clusters."""
        src = _BE_SRC.read_text(encoding="utf-8")
        assert '"United States of America", "United States", "USA"' not in src
        assert 'value.lower() in ("united states", "usa", "us")' not in src
        # Any declared value, any adapter, one shared source of spellings.
        assert browser_executor._option_spellings("country", "USA")[0] == "USA"
        assert "United States of America" in browser_executor._option_spellings(
            "country", "USA")
        assert browser_executor._match_form_option(
            "country", "United States", ["India", "United States of America"]
        ) == "United States of America"
        # No match means None — the caller leaves the field alone.
        assert browser_executor._match_form_option(
            "country", "India", ["United States of America"]) is None


# ===========================================================================
# End to end through the real adapter + fill driver
# ===========================================================================
def _seed_profile(**prefs):
    d = current_db()
    d.resume_assets.insert_one({
        "resume_id": "asset-opt", "owner_type": "user", "user_email": USER,
        "parse_status": "parsed", "parsed_at": "2026-01-01T00:00:00+00:00",
        "structured": {"contact": {
            "name": "Ada Lovelace", "email": "ada@example.com",
            "phone": "+15551234567"}},
    })
    d.user_preferences.insert_one(dict({"user_email": USER}, **prefs))


def test_declared_country_fills_the_forms_own_label_and_submits():
    """The incident, end to end: the run now completes instead of dying on
    ``validation: country required/invalid``."""
    adapter, page = _adapter(["India", "Canada", "United States of America"])
    answers = {"first_name": "Ada", "last_name": "Lovelace",
               "email": "ada@example.com", "country": "United States"}

    out = fill_driver.drive(adapter, answers, mode=MODE_APPROVAL)

    assert out["outcome"] == FILL_CONFIRMED, out
    # The FORM'S OWN label was submitted, not the declared spelling.
    assert out["review_snapshot"]["values"]["country"] == "United States of America"
    assert page.clicks == 1


def test_unmatchable_country_parks_at_needs_input_and_never_defaults_to_us():
    """An Indian citizen's declared country against a form that does not offer
    India: the run STOPS and names the field. Filling "United States of America"
    here — the only country the form offers — would be a fabricated fact."""
    adapter, page = _adapter(["United States of America", "Canada"])
    answers = {"first_name": "Ada", "last_name": "Lovelace",
               "email": "ada@example.com", "country": "India"}

    out = fill_driver.drive(adapter, answers, mode=MODE_APPROVAL)

    assert out["outcome"] == FILL_NEEDS_INPUT, out
    assert out["field"] == "country"                  # the EXACT field is named
    assert out["reason"] == "no_option_match"
    assert page.clicks == 0                           # nothing was submitted
    values = out["review_snapshot"]["values"]
    assert "country" not in values                    # nothing was written
    assert not any("United States" in str(v) for v in values.values())


def test_f1_opt_holder_keeps_india_citizenship_while_country_resolves_to_us():
    """The live user: resident in the US, CITIZEN OF INDIA. Both fields are
    geography enumerations on the same form and both must be matched from
    DECLARED data — country onto the form's US label, citizenship onto India.
    A US citizenship answer would be a false legal attestation (spec §0.1)."""
    _seed_profile(country="United States", citizenship_countries="India")
    adapter, page = _adapter(
        ["Canada", "India", "United States of America"],
        citizenship_options=["Canada", "India", "United States of America"])

    questions = adapter.recon()
    assert {"country", "citizenship_countries"} <= {q["field_id"] for q in questions}

    answers = fill_driver.resolve_answers(USER, questions)
    assert answers["citizenship_countries"] == "India"     # declared, untouched

    out = fill_driver.drive(adapter, answers, mode=MODE_APPROVAL)

    assert out["outcome"] == FILL_CONFIRMED, out
    values = out["review_snapshot"]["values"]
    assert values["country"] == "United States of America"
    assert values["citizenship_countries"] == "India"      # NOT the US
