"""Compensation extraction - the field the inventory never had.

`job_postings` stored no salary and the feed row hard-coded `"salary": ""`, so
salary filtering was not weak, it was impossible. These tests pin the two
things that make the new field safe to filter on: every ATS shape lands on the
same normalized structure, and anything that cannot be read confidently returns
None instead of a number. A wrong salary sorts real jobs out of a feed, which
is strictly worse than an unknown one.
"""
import pytest

from services import salary_normalizer as sal


# ---------------------------------------------------------------------------
# The shapes real boards publish. No two agree.
# ---------------------------------------------------------------------------

def test_lever_style_nested_range():
    got = sal.parse({"min": 120000, "max": 150000, "currency": "USD",
                     "interval": "per-year-salary"})
    assert (got["annual_min"], got["annual_max"]) == (120000, 150000)
    assert got["currency"] == "USD" and got["period"] == "year"
    assert got["confidence"] == "structured"


def test_greenhouse_style_cents():
    """`pay_input_ranges` publishes cents; read as dollars it is a 100x error
    that would push every posting above any salary floor."""
    got = sal.parse([{"min_cents": 12000000, "max_cents": 15000000,
                      "currency_type": "USD"}])
    assert (got["annual_min"], got["annual_max"]) == (120000, 150000)


def test_ashby_style_rendered_string():
    got = sal.parse("$120K – $150K")
    assert (got["annual_min"], got["annual_max"]) == (120000, 150000)


def test_a_nested_payload_is_still_found():
    got = sal.parse({"compensation": {"tiers": [{"min": 90000, "max": 110000,
                                                 "currency": "USD"}]}})
    assert got["annual_min"] == 90000


def test_hourly_is_annualized_so_it_compares_to_salaried():
    """The whole point of annualizing: a filter that cannot compare $60/hr with
    $120,000/yr is not a salary filter."""
    got = sal.parse({"min": 60, "max": 75, "currency": "USD", "interval": "hourly"})
    assert got["period"] == "hour"
    assert (got["annual_min"], got["annual_max"]) == (60 * 2080, 75 * 2080)


def test_monthly_is_annualized():
    got = sal.parse({"min": 8000, "max": 10000, "interval": "monthly"})
    assert (got["annual_min"], got["annual_max"]) == (96000, 120000)


def test_a_reversed_range_is_ordered():
    got = sal.parse({"min": 150000, "max": 120000})
    assert got["min"] == 120000 and got["max"] == 150000


def test_non_usd_currency_survives():
    got = sal.parse("£45,000 - £55,000 per year")
    assert got["currency"] == "GBP" and got["annual_max"] == 55000


# ---------------------------------------------------------------------------
# Text fallback, and why it is marked
# ---------------------------------------------------------------------------

def test_text_fallback_reads_a_range_out_of_a_description():
    got = sal.parse(None, text="We offer $130,000 - $160,000 per year plus equity.")
    assert (got["annual_min"], got["annual_max"]) == (130000, 160000)
    assert got["confidence"] == "text"


def test_structured_beats_text():
    """A JD mentioning a number may be describing a bonus or last year's band;
    a stated range is the employer's own answer."""
    got = sal.parse({"min": 100000, "max": 120000},
                    text="Our top performers earned $400,000 last year.")
    assert got["annual_max"] == 120000
    assert got["confidence"] == "structured"


def test_an_hourly_rate_in_text_is_recognized_by_its_magnitude():
    got = sal.parse(None, text="Pay is $55 - $65 per hour, W2.")
    assert got["period"] == "hour"
    assert got["annual_min"] == 55 * 2080


# ---------------------------------------------------------------------------
# Refusing to guess. Each of these would sort real jobs out of a feed.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,text", [
    (None, "Founded in 2024, we are a team of 50."),      # a year and a headcount
    (None, "Equity: 0.25% - 0.75%"),                       # percentages
    ({"min": 0, "max": 0}, ""),                            # empty range
    ({"min": 12, "max": 20}, ""),                          # implausible as annual
    ({"min": 50_000_000, "max": 90_000_000}, ""),          # implausible entirely
    ({"min": 900000, "max": 1200000, "interval": "hourly"}, ""),  # mislabelled period
    (None, ""),                                            # nothing at all
    ("", ""),
    ({}, ""),
])
def test_an_unreadable_posting_yields_no_number(raw, text):
    assert sal.parse(raw, text=text) is None


def test_a_bare_year_is_not_a_salary():
    assert sal.parse(None, text="Posted 2026. Req 4471.") is None


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ({"min": 120000, "max": 150000, "currency": "USD"}, "$120,000 - $150,000 a year"),
    ({"min": 60, "max": 75, "currency": "USD", "interval": "hourly"}, "$60 - $75 an hour"),
    ({"min": 45000, "max": 55000, "currency": "GBP"}, "£45,000 - £55,000 a year"),
    ({"min": 120000, "currency": "USD"}, "$120,000 a year"),
])
def test_display_strings(raw, expected):
    assert sal.display(sal.parse(raw)) == expected


def test_display_of_nothing_is_empty_not_a_phrase():
    """"Not disclosed" is the frontend's wording decision, not this module's."""
    assert sal.display(None) == ""


def test_normalize_attaches_the_display_string():
    got = sal.normalize({"min": 120000, "max": 150000, "currency": "USD"})
    assert got["display"] == "$120,000 - $150,000 a year"


@pytest.mark.parametrize("raw,expected", [
    # Cents are all-or-nothing. "$70.4" reads as a typo on a money figure...
    ({"min": 60.55, "max": 70.40, "currency": "USD", "interval": "hourly"},
     "$60.55 - $70.40 an hour"),
    # ...but a whole number should not carry ".00".
    ({"min": 70.0, "currency": "USD", "interval": "hourly"}, "$70 an hour"),
    ({"min": 32.5, "currency": "USD", "interval": "hourly"}, "$32.50 an hour"),
])
def test_cents_are_rendered_all_or_nothing(raw, expected):
    assert sal.display(sal.parse(raw)) == expected
