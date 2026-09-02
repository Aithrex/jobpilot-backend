"""Provider-supplied fields, normalised without inventing anything.

Every spelling asserted here is one the LIVE inventory carries. The point of
the module under test is that a filter and the stored value share a
vocabulary; the point of this module is that normalising never manufactures a
value the posting did not state.
"""
from __future__ import annotations

import pytest

from services import job_inventory_service as inv


class TestCountryNormalisation:
    """`{"locations.country": {"$in": ["usa"]}}` compared a lowercased request
    against a stored "USA" and matched ZERO of 217,019 located postings. The
    country filter had never worked on any country."""

    @pytest.mark.parametrize("raw, want", [
        ("USA", "US"), ("usa", "US"), ("U.S.A.", "US"),
        ("United States", "US"), ("United States of America", "US"),
        ("India", "IN"), ("Canada", "CA"), ("Germany", "DE"),
        ("United Kingdom", "GB"), ("UK", "GB"), ("England", "GB"),
        ("Netherlands", "NL"), ("Singapore", "SG"), ("Brasil", "BR"),
        ("türkiye", "TR"), ("Viet Nam", "VN"),
    ])
    def test_every_spelling_reaches_one_code(self, raw, want):
        assert inv.normalize_country(raw) == want

    @pytest.mark.parametrize("raw", ["", None, "Mars", "Remote", "EMEA",
                                     "Anywhere", "Multiple Locations"])
    def test_an_unrecognised_country_stays_empty_rather_than_guessing(self, raw):
        assert inv.normalize_country(raw) == ""

    def test_a_two_part_location_puts_the_country_in_the_country_field(self):
        """"Berlin, Germany" was parsed positionally: city then REGION, so
        `country` was empty and every non-US two-part location was
        uncountable."""
        got = inv.normalize_location("Berlin, Germany")
        assert got["country"] == "Germany"
        assert got["country_code"] == "DE"
        assert got["region"] == ""

    def test_a_three_part_location_still_keeps_its_region(self):
        got = inv.normalize_location("Austin, TX, USA")
        assert (got["city"], got["region"], got["country_code"]) == ("Austin", "TX", "US")

    def test_remote_usa_is_a_country_not_a_region(self):
        got = inv.normalize_location("Remote, USA")
        assert got["country_code"] == "US"

    def test_the_raw_string_is_always_preserved(self):
        got = inv.normalize_location("Bengaluru, KA, India")
        assert got["raw"] == "Bengaluru, KA, India"


class TestEmploymentType:
    """174,563 rows blank plus a tail of 61+ spellings. An equality filter
    over that vocabulary was meaningless."""

    @pytest.mark.parametrize("raw, want", [
        ("full-time", "full_time"), ("fulltime", "full_time"),
        ("regular-full-time", "full_time"), ("full-time-permanent", "full_time"),
        ("ft-hourly-w-bn", "full_time"), ("cdi", "full_time"),
        ("salaried", "full_time"), ("exempt", "full_time"),
        ("contrat-dur-e-ind-termin-e", "full_time"),
        ("part-time", "part_time"), ("regular-part-time", "part_time"),
        ("contract", "contract"), ("contractor", "contract"),
        ("temporary", "temporary"), ("seasonal", "temporary"),
        ("prn", "temporary"), ("on-call", "temporary"),
        ("temporary-full-time-9-mos-or-less", "temporary"),
        ("intern", "internship"), ("internship", "internship"),
    ])
    def test_live_spellings_reach_one_vocabulary(self, raw, want):
        assert inv.normalize_employment_type(raw) == want

    @pytest.mark.parametrize("raw", ["foh", "sm", "career-service-exec",
                                     "various-job-types", "experienced",
                                     "entry-level", "", None])
    def test_a_code_that_names_no_employment_type_stays_unknown(self, raw):
        """`foh` is front-of-house, `sm` is a grade, `career-service-exec` is a
        civil-service classification. None of them says full-time or
        part-time, and guessing puts a filter in front of a user that quietly
        excludes real jobs."""
        assert inv.normalize_employment_type(raw) == "unknown"

    @pytest.mark.parametrize("raw, want", [
        ("Full-Time Internship", "internship"),
        ("Temporary Full-Time", "temporary"),
        ("Full-Time Permanent Contract", "contract"),
        ("Part-Time Temporary", "temporary"),
    ])
    def test_intent_wins_over_the_full_time_token(self, raw, want):
        """Reading the full-time token first collapsed three different
        employment types into one."""
        assert inv.normalize_employment_type(raw) == want

    def test_the_vocabulary_is_closed(self):
        for raw in ("full-time", "foh", "prn", "intern", None):
            assert inv.normalize_employment_type(raw) in inv.EMPLOYMENT_TYPES

    def test_the_raw_spelling_is_kept_for_the_content_hash(self):
        """`compute_content_hash` digests `employment_type`. Rewriting it in
        place would change every hash at once and re-parse 405,133 postings
        for a normalisation that changes no meaning."""
        doc = inv.normalize_posting({
            "source": "x", "source_job_id": "1", "title": "Backend Engineer",
            "company": "Acme", "employment_type": "regular-full-time",
            "description": "Python and SQL.", "location": ["Austin, TX, USA"]})
        assert doc["employment_type"] == "regular-full-time"
        assert doc["employment_type_normalized"] == "full_time"


class TestWorkMode:
    """96.1% of the live inventory states no work mode. That gap is ours;
    widening the signal is the fix, guessing is not."""

    @pytest.mark.parametrize("desc, want", [
        ("This is a fully remote position.", "remote"),
        ("Remote-first company, work from anywhere.", "remote"),
        ("You will be in office 3 days per week.", "hybrid"),
        ("Hybrid: in the office 2 days a week.", "hybrid"),
        ("This role is on-site in Austin.", "onsite"),
    ])
    def test_an_explicit_statement_in_the_body_is_read(self, desc, want):
        assert inv.normalize_work_mode(None, description=desc) == want

    @pytest.mark.parametrize("desc", [
        "Great team, great benefits, competitive salary.",
        "Our headquarters are in Austin, Texas.",
        "",
    ])
    def test_silence_stays_unknown(self, desc):
        """A posting that names a city has not said it is onsite. Guessing
        `onsite` from a location would mark 96% of the inventory as onsite on
        no evidence at all."""
        assert inv.normalize_work_mode(None, description=desc) == "unknown"

    def test_an_explicit_provider_value_still_wins(self):
        assert inv.normalize_work_mode("Hybrid") == "hybrid"
        assert inv.normalize_work_mode("Remote") == "remote"


class TestSponsorshipAndClearanceAreNeverManufactured:
    def test_sponsorship_is_none_unless_the_posting_said_something(self):
        from services import tech_feed_service as tfs
        assert tfs._sponsorship_of({"title_raw": "Backend Engineer",
                                    "description_text": "Python and SQL."}) is None

    def test_an_explicit_no_sponsorship_statement_is_read(self):
        from services import tech_feed_service as tfs
        assert tfs._sponsorship_of({
            "title_raw": "Backend Engineer",
            "description_text": "We are unable to sponsor visas for this role.",
        }) == "not_offered"

    def test_an_explicit_offer_is_read(self):
        from services import tech_feed_service as tfs
        assert tfs._sponsorship_of({
            "title_raw": "Backend Engineer",
            "description_text": "Visa sponsorship is available for this role.",
        }) == "offered"

    def test_clearance_is_true_or_none_never_false(self):
        """"This posting did not mention a clearance" is not the same claim as
        "this posting does not require one"."""
        from services import tech_feed_service as tfs
        assert tfs._clearance_of({"title_raw": "Backend Engineer"}) is None
        assert tfs._clearance_of(
            {"title_raw": "Software Engineer - TS/SCI"}) is True
        assert tfs._clearance_of(
            {"title_raw": "x", "requirements": {"clearance_required": True}}) is True
