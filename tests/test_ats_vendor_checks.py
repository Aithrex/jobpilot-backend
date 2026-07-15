"""ats_vendor_checks — table-driven tests for the deterministic per-vendor
parse-compat checklists and the shared date helpers.

Pure functions: no Mongo, no LLM, no fixtures.
"""
import pytest

from services.ats_vendor_checks import (
    VENDORS,
    build_vendor_compat,
    date_format_class,
    dates_format_consistent,
    is_reverse_chronological,
    parse_end_date,
)


def clean_pdf_check(score=100, **check_overrides):
    checks = {
        "extraction_ok": True,
        "contact_name_found": True,
        "contact_email_found": True,
        "contact_phone_found": True,
        "contact_at_top": True,
        "sections_found": ["SUMMARY", "EXPERIENCE", "SKILLS", "EDUCATION"],
        "sections_missing": [],
        "sections_in_order": True,
        "skills_keywords_found": 10,
        "skills_keywords_total": 10,
        "experience_dates_parseable": 2,
        "experience_dates_total": 2,
        "no_glyph_corruption": True,
        "word_join_issues": [],
        "page_count": 1,
        "single_page": True,
    }
    checks.update(check_overrides)
    return {"pdf_parseable_score": score, "checks": checks, "warnings": []}


CLEAN_TAILORED = {
    "experience": [
        {"title": "Engineer", "dates": "March 2023 - Present", "bullets": []},
        {"title": "Analyst", "dates": "June 2020 - February 2023", "bullets": []},
    ],
    "education": [{"degree": "BS", "dates": "May 2020"}],
}


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("dates,expected", [
    ("March 2022 - Present", "full_month"),
    ("Mar. 2022 - Jun 2023", "abbr_month"),
    ("03/2022 - 06/2023", "numeric"),
    ("2020 - 2022", "year_only"),
    ("", None),
    ("Present", None),
])
def test_date_format_class(dates, expected):
    assert date_format_class(dates) == expected


def test_dates_consistent_when_single_class():
    assert dates_format_consistent(CLEAN_TAILORED) is True


def test_dates_inconsistent_when_mixed():
    mixed = {
        "experience": [
            {"dates": "March 2022 - Present"},
            {"dates": "05/2019 - 02/2022"},
        ],
        "education": [],
    }
    assert dates_format_consistent(mixed) is False


@pytest.mark.parametrize("dates,expected", [
    ("March 2022 - Present", (9999, 12)),
    ("June 2020 - February 2023", (2023, 2)),
    ("03/2021 - 08/2022", (2022, 8)),
    ("2019 - 2021", (2021, 12)),
    ("Jan 2020 - Mar 2021", (2021, 3)),
    ("", None),
    ("n/a", None),
])
def test_parse_end_date(dates, expected):
    assert parse_end_date(dates) == expected


def test_reverse_chronological_ordered():
    assert is_reverse_chronological(CLEAN_TAILORED) is True


def test_reverse_chronological_unordered():
    unordered = {
        "experience": [
            {"dates": "June 2018 - May 2020"},
            {"dates": "March 2022 - Present"},
        ]
    }
    assert is_reverse_chronological(unordered) is False


def test_reverse_chronological_vacuous_when_unparseable():
    assert is_reverse_chronological({"experience": [{"dates": "??"}]}) is True


# ---------------------------------------------------------------------------
# build_vendor_compat
# ---------------------------------------------------------------------------

def _statuses(compat):
    return {v: compat[v]["status"] for v in VENDORS}


def test_all_pass_on_clean_check():
    compat = build_vendor_compat(clean_pdf_check(), CLEAN_TAILORED)
    assert set(compat) == set(VENDORS)
    assert all(s == "pass" for s in _statuses(compat).values())
    for vendor in VENDORS:
        for chk in compat[vendor]["checks"]:
            assert set(chk) == {"id", "label", "status"}  # _hard stripped


def test_all_fail_without_pdf():
    compat = build_vendor_compat(None, CLEAN_TAILORED)
    assert all(s == "fail" for s in _statuses(compat).values())
    assert compat["workday"]["checks"][0]["id"] == "pdf_render"


# Each degraded field flips exactly the mapped vendor checks.
@pytest.mark.parametrize("overrides,tailored,expect_fail,expect_warn", [
    # Unparseable dates → Workday hard-fails
    ({"experience_dates_parseable": 1}, CLEAN_TAILORED, {"workday"}, set()),
    # Missing contact phone → Workday hard-fails
    ({"contact_phone_found": False}, CLEAN_TAILORED, {"workday"}, set()),
    # Missing section labels → Greenhouse + Taleo hard-fail
    ({"sections_missing": ["SKILLS"]}, CLEAN_TAILORED, {"greenhouse", "taleo"}, set()),
    # Out-of-order sections → Greenhouse warns (soft)
    ({"sections_in_order": False}, CLEAN_TAILORED, set(), {"greenhouse"}),
    # Word-join issues → Lever hard-fails (sidebar/linear-text proxy)
    ({"word_join_issues": ["Backendengineer"]}, CLEAN_TAILORED, {"lever"}, set()),
    # Skills extraction below 90% → iCIMS hard-fails
    ({"skills_keywords_found": 5}, CLEAN_TAILORED, {"icims"}, set()),
    # Glyph corruption → iCIMS + Taleo hard-fail
    ({"no_glyph_corruption": False}, CLEAN_TAILORED, {"icims", "taleo"}, set()),
    # Contact not at top → Taleo hard-fails
    ({"contact_at_top": False}, CLEAN_TAILORED, {"taleo"}, set()),
    # Name missing → SmartRecruiters hard-fails
    ({"contact_name_found": False}, CLEAN_TAILORED, {"smartrecruiters"}, set()),
])
def test_degraded_field_flips_mapped_vendor(overrides, tailored, expect_fail, expect_warn):
    compat = build_vendor_compat(clean_pdf_check(**overrides), tailored)
    statuses = _statuses(compat)
    for vendor in VENDORS:
        if vendor in expect_fail:
            assert statuses[vendor] == "fail", vendor
        elif vendor in expect_warn:
            assert statuses[vendor] == "warn", vendor
        else:
            assert statuses[vendor] == "pass", vendor


def test_low_parse_score_warns_smartrecruiters():
    compat = build_vendor_compat(clean_pdf_check(score=80), CLEAN_TAILORED)
    assert compat["smartrecruiters"]["status"] == "warn"


def test_mixed_date_formats_fail_workday():
    mixed = {
        "experience": [
            {"dates": "March 2022 - Present"},
            {"dates": "05/2019 - 02/2022"},
        ],
        "education": [],
    }
    compat = build_vendor_compat(clean_pdf_check(), mixed)
    assert compat["workday"]["status"] == "fail"


def test_unordered_experience_warns_lever():
    unordered = {
        "experience": [
            {"dates": "June 2018 - May 2020"},
            {"dates": "March 2022 - Present"},
        ],
        "education": [],
    }
    compat = build_vendor_compat(clean_pdf_check(), unordered)
    assert compat["lever"]["status"] == "warn"
