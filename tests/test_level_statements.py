"""The no-level audit (2026-11-05): postings that state their level or years in
ways the reader missed, and the look-alikes that must stay as they were.

Every case here is a live posting's wording (or the shape of one). The
measurements behind each rule are in `scripts/eval_level_hints.py`."""
from __future__ import annotations

import pytest

from services import fit_score as fs
from services import jd_profile


def years(text):
    return fs._required_years(fs._norm(text))


class TestYearsTheReaderMissed:
    @pytest.mark.parametrize("text,expected", [
        # SpaceX: two bullets glued together; the real requirement is the 1+.
        ("basic qualifications: bachelor's degree in computer science; or 2+ years of professional "
         "experience building software in lieu of a degree 1+ years of experience in full stack "
         "development preferred skills", 1),
        # Red Hat: "(u.s." split the sentence and the years read as the degree's alternative.
        ("bachelor's degree (u.s. or foreign equivalent) in computer science or related field and two "
         "(2) years of experience in the job offered or related role or master's degree (u.s. or "
         "foreign equivalent) and no experience.", 2),
        # Alo Yoga: years in a field, right after the label.
        ("qualifications: 2+ years in analytics, pricing, buying, merchandising, or planning;", 2),
        # Leidos: experience may stand in for the degree - the 2 is still the ask.
        ("Bachelor's Degree with 2-6 years of experience (additional experience/training/"
         "certifications may substitute for a degree)", 2),
    ])
    def test_now_read(self, text, expected):
        assert years(text) == expected

    @pytest.mark.parametrize("text,expected", [
        # Company history is not a requirement.
        ("Our company has 20+ years in business.", None),
        # The years that replace a degree are never the requirement.
        ("Bachelor's degree or 4+ years of experience in lieu of a degree.", None),
        ("Bachelor's degree (or 4+ years of experience in lieu of a degree).", None),
        # CACI: a DEGREE substituting for years lowers the ask; not read as 12.
        ("12+ Years of relevant experience (Bachelor's Degree in relevant field may be substituted "
         "for 5 years of relevant experience).", None),
        # Entergy: the 8 is the price of no degree; the bachelor's path asks 4.
        ("4+ years of professional experience with a bachelor's degree; in lieu of a degree 8 years "
         "of experience in cloud engineering is required", 4),
        # A field line never outranks the overall requirement.
        ("A minimum of 1 years in Information Technology. General Experience - 3 to 6 years", 3),
        ("7+ years of purchasing experience. Minimum 3 years in the medical device industry preferred.", 7),
        ("Requirements: 5+ years of experience. Must be a U.S. citizen.", 5),
    ])
    def test_look_alikes_unchanged(self, text, expected):
        assert years(text) == expected


class TestBodyStatesTheLevel:
    @pytest.mark.parametrize("body,level", [
        ("We're looking for a Senior SDET who already works this way.", "senior"),
        ("As a senior full stack developer at Booz Allen, you'll use your passion.", "senior"),
        ("This early-career role is ideal for engineers excited to work hands-on.", "entry"),
        ("It suits an early-career engineer who enjoys digging deep.", "entry"),
        ("Job Level\nEntry Level\nLocation: Toulouse", "entry"),
    ])
    def test_stated_in_words(self, body, level):
        assert fs.level_from_signals("Software Engineer", description=body) == level

    @pytest.mark.parametrize("body", [
        "Join our senior engineering team building payments.",
        "You will partner with senior leadership and senior stakeholders.",
        "Train early career engineers in engineering best practice.",
        "Opportunities span early-career through senior technical levels.",
        # Says both: the posting contradicts itself, so it stays unknown.
        "This early-career role suits you. As a senior engineer you will lead.",
    ])
    def test_not_a_statement_about_this_role(self, body):
        assert fs.level_from_signals("Software Engineer", description=body) is None

    def test_stated_years_still_win(self):
        assert fs.level_from_signals(
            "Software Engineer",
            description="Requirements: 2+ years of experience. We're looking for a senior engineer.") == "entry"

    def test_a_title_level_still_wins(self):
        assert fs.level_from_signals(
            "Junior Developer", description="We're looking for a senior developer.") == "entry"


class TestLikelySeniorHint:
    @pytest.mark.parametrize("body", [
        "- Mentor engineers and produce exemplary artifacts.",
        "We are looking for an experienced DevOps Engineer to join our team.",
    ])
    def test_reads_senior(self, body):
        assert jd_profile.read_level_hint(body)["level"] == "senior"

    @pytest.mark.parametrize("body", [
        "Mentor others to enhance their overall electrical skills.",
        "Provide guidance to less experienced team members when appropriate.",
        "May direct the work of other lower level professionals.",
    ])
    def test_measured_too_loose_and_left_out(self, body):
        assert jd_profile.read_level_hint(body) is None
