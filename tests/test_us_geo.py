"""Golden cases for `services.us_geo` — THE US filter.

Every string here was taken from a live `job_postings` row. The leaks and the
false drops are labelled with what actually went wrong, because the reason a
case exists is the only thing that stops it being "simplified" away later.
"""
import pytest

from services import us_geo
from services.career_pages_service import _keep_us
from services.workday_jobs_service import _geo_keep_us


# --------------------------------------------------------------------------
# Leaks: non-US rows that the pre-`us_geo` filter served in a US-only slice.
# All four were caused by a two-letter state code matching a substring that is
# not a state, which is why the code tier is now POSITIONAL.
# --------------------------------------------------------------------------
LEAKED = [
    ("Hyderabad-Hi-Tec City", "'Hi' in 'Hi-Tec' read as Hawaii"),
    ("Montreal - 1000 Rue De La Gauchetiere Ouest", "'De' read as Delaware"),
    ("CA - Toronto", "'CA' read as California, not Canada"),
    ("CA-Toronto-York St 24/25", "same, no separator"),
    ("CA, Ontario", "'CA' read as California"),
    ("BR - Rio de Janeiro", "'de' read as Delaware"),
    ("IN-HR-Gurgaon-US Holidays-DLF Cyber Greens",
     "the office name contains 'US', which must not read as eligibility"),
    ("Remote, Sao Paulo", "foreign hub city"),
    ("Remote, São Paulo", "accented spelling must fold onto the marker"),
]

# --------------------------------------------------------------------------
# False drops: real US rows the pre-`us_geo` filter hid. The counts are how
# many active postings carried that exact string when this was measured.
# --------------------------------------------------------------------------
FALSE_DROPS = [
    ("US", "bare 'US' was not in the country-word tier (688 rows)"),
    ("San Francisco", "bare major US city, no state token (94 rows)"),
    ("San Jose", "(33 rows)"),
    ("Boston", "(32 rows)"),
    ("Indianapolis, IN", "'in' is excluded from the state-code set (34 rows)"),
    ("Boca Raton", "(16 rows)"),
    ("UT MAIN CAMPUS", "leading state code, whitespace separator"),
    ("IL, Peoria", "'IL' collided with the Israel ISO-2 prefix"),
    ("IL-Chicago; 303 E Wacker Drive", "same"),
    ("Newport Beach CA-700", "state code before a facility number"),
    ("North Bethesda MD - Corporate Headquarters", "uppercase code mid-string"),
    ("Kernersville-NC 1045 Industrial Park Dr", "same"),
    ("Panama City, FL", "'panama' matched the country marker"),
    ("Jamaica Plain-MA", "'jamaica' matched the country marker"),
    ("Albuquerque, New Mexico, United States", "'mexico' inside 'New Mexico'"),
    ("US and Canada Offices", "multi-region posting that includes the US"),
    ("Remote, Canada; Remote, United States", "same"),
]

# Rows that must survive because a state token rescues a foreign namesake.
US_NAMESAKES = [
    "Berlin, CT", "Berlin, VT", "Vienna, OH", "Vancouver, WA",
    "Vancouver, Washington", "Athens, Georgia", "Dublin, OH", "Paris, TX",
    "Melbourne, FL", "London, KY", "Manchester, NH", "Ontario, OH",
    "Ontario, California", "Lisbon, IA", "Lima, OH", "Delhi, NY",
    "Milan, MO", "Rome, GA - Riverbend Branch", "Ontario, CA", "Bogota, CO",
]

# Unambiguously foreign.
FOREIGN = [
    "India", "Canada", "Brazil", "Germany", "UK", "Ireland", "Portugal",
    "Turkey", "United Arab Emirates", "Bengaluru, India", "Pune",
    "Remote - India", "Toronto, ON", "Vancouver, BC", "Krakow",
    "Canada - Remote (ON, AB, or NS Only)", "PL - Krakow", "JP - Tokyo",
    "SG - Singapore", "AU - Sydney", "London", "Singapore", "Tokyo",
    "Dublin", "Tel Aviv", "Hong Kong", "Manila", "Berlin", "Munich",
    "CA - Esterhazy, SK", "Cuajimalpa de Morelos, CDMX",
    "Calvin Klein La Defense", "Telstra Store Mt Gambier",
    "Resende, Rio de Janeiro - Brasil", "Panama City, Panama",
    "Kingston, Jamaica", "Zürich", "Kraków", "Montréal",
    "Bogotá", "München",
]

# Ambiguous — the hard-filter contract DROPS these.
AMBIGUOUS = ["2 Locations", "3 Locations", "Main Campus",
             "Eisenhower Medical Center", "Penn State University Park", ""]


@pytest.mark.parametrize("location,why", LEAKED)
def test_known_leaks_are_dropped(location, why):
    assert us_geo.keep_us(location) is False, why


@pytest.mark.parametrize("location,why", FALSE_DROPS)
def test_known_false_drops_are_kept(location, why):
    assert us_geo.keep_us(location) is True, why


@pytest.mark.parametrize("location", US_NAMESAKES)
def test_us_namesakes_of_foreign_cities_survive(location):
    assert us_geo.keep_us(location) is True


@pytest.mark.parametrize("location", FOREIGN)
def test_foreign_locations_drop(location):
    assert us_geo.keep_us(location) is False


@pytest.mark.parametrize("location", AMBIGUOUS)
def test_ambiguous_drops_by_contract(location):
    assert us_geo.keep_us(location) is False


class TestAuthoritativeCountry:
    def test_country_code_us_beats_a_foreign_looking_string(self):
        assert us_geo.keep_us("Bengaluru", "United States") is True

    def test_country_code_foreign_beats_a_us_looking_string(self):
        assert us_geo.keep_us("Austin, TX", "India") is False

    @pytest.mark.parametrize("spelling", ["US", "usa", "United States",
                                          "United States of America", "U.S.A."])
    def test_us_spellings_all_normalize(self, spelling):
        assert us_geo.keep_us("Anywhere", spelling) is True


class TestEmptyLocation:
    def test_empty_and_not_remote_drops(self):
        assert us_geo.keep_us("", None, is_remote=False) is False

    def test_empty_but_remote_keeps(self):
        assert us_geo.keep_us("", None, is_remote=True) is True


class TestMultiLocationRollup:
    def test_dropped_when_serving(self):
        assert us_geo.keep_us("5 Locations") is False

    def test_kept_at_the_list_stage_for_enrichment(self):
        assert us_geo.keep_us("5 Locations", defer_multi=True) is True


class TestKeepAmbiguous:
    """The lenient pre-hardening contract (the GUEST_US_STRICT=0 rollback)."""

    def test_ambiguous_kept_when_asked(self):
        assert us_geo.keep_us("2 Locations", keep_ambiguous=True) is True

    def test_explicit_foreign_still_drops(self):
        assert us_geo.keep_us("Bengaluru, India", keep_ambiguous=True) is False


class TestDelegation:
    """Both legacy entry points must now give the SAME answer — they used to
    disagree, and `inventory_feed` served both source tabs through the weaker
    of the two."""

    @pytest.mark.parametrize("location", [
        "US", "San Francisco", "Hyderabad-Hi-Tec City", "CA - Toronto",
        "Austin, TX", "Bengaluru, India", "Berlin, CT", "2 Locations",
    ])
    def test_career_pages_and_workday_agree(self, location):
        row = {"location": location, "country_hint": "", "is_remote": False}
        assert _keep_us(row) == _geo_keep_us(location) == us_geo.keep_us(location)


class TestPostingDocument:
    def test_reads_locations_array_and_work_mode(self):
        doc = {"locations": [{"raw": "Bengaluru", "country": ""}],
               "work_mode": "onsite"}
        assert us_geo.keep_us_posting(doc) is False

    def test_no_locations_but_remote_keeps(self):
        assert us_geo.keep_us_posting({"locations": [], "work_mode": "remote"}) is True

    def test_country_on_the_location_wins(self):
        doc = {"locations": [{"raw": "Anywhere", "country": "India"}]}
        assert us_geo.keep_us_posting(doc) is False


class TestHasForeignMarker:
    """The purge script retires a ledger row only on a POSITIVE foreign
    signal, so this predicate must not fire on mere ambiguity."""

    @pytest.mark.parametrize("location", ["Bengaluru, India", "London",
                                          "Toronto, ON", "CA - Toronto"])
    def test_true_for_foreign(self, location):
        assert us_geo.has_foreign_marker(location) is True

    @pytest.mark.parametrize("location", ["2 Locations", "Main Campus",
                                          "Austin, TX", "US", ""])
    def test_false_for_ambiguous_or_us(self, location):
        assert us_geo.has_foreign_marker(location) is False


# ---------------------------------------------------------------------------
# Foreign hub whose subdivision code collides with a US state code
# ---------------------------------------------------------------------------

class TestForeignHubStateCodeCollision:
    """The tier-2 rescue must not fire on a city's OWN region.

    Audited against live inventory (2026-08-27): of 17,208 clearly-non-US
    active postings, `keep_us` kept 248 - and 243 of those were genuine US
    towns the audit's own regex had flagged by name (Dublin OH, Paris KY,
    Vancouver WA, Melbourne FL). The other five were real leaks, all one shape:

        "Chennai, TN"  -> TN is Tamil Nadu, read as Tennessee   (4, JLL)
        "Jakarta, ID"  -> ID is Indonesia,  read as Idaho       (1, S&P Global)

    There is no Chennai, Tennessee. For a hub with no US namesake the rescue
    can only ever be wrong.
    """

    @pytest.mark.parametrize("loc", [
        "Chennai, TN", "Jakarta, ID", "Bengaluru, KA", "Bhubaneswar, OR",
        "Hyderabad, TS", "Pune, MH", "Shenzhen, GD", "Dubai, DU",
    ])
    def test_a_hub_with_no_us_namesake_is_never_rescued(self, loc):
        assert us_geo.keep_us(loc) is False, loc

    @pytest.mark.parametrize("loc", [
        "Dublin, OH", "Dublin, CA", "Paris, KY", "Vancouver, WA",
        "Berlin, CT", "London, KY", "Ontario, CA",
        "United States-Florida-Melbourne", "Delhi, NY",
    ])
    def test_a_genuine_us_namesake_still_survives(self, loc):
        """The rescue exists for these; narrowing it must not cost them."""
        assert us_geo.keep_us(loc) is True, loc

    @pytest.mark.parametrize("loc", [
        "Toronto, Ontario, Canada", "Bangalore, India", "London, UK",
    ])
    def test_explicitly_foreign_still_drops(self, loc):
        assert us_geo.keep_us(loc) is False, loc
