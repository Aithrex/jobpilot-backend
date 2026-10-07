"""Roles found on employers' own boards, held in the inventory and not shown
(all-users cross-match, 2026-10-07)."""
import pytest

from services import fit_score, tech_taxonomy


class TestATitleThatSaysSoftwareIsSoftware:
    @pytest.mark.parametrize("title", [
        "Software Engineer, Test Frameworks & Tooling",      # Roblox, vetoed by "tooling"
        "Software Engineer, Tooling",
        "Full Stack Software Engineer, Manufacturing Systems",
        "Web Developer, HVAC Portal",
    ])
    def test_a_team_name_does_not_veto_it(self, title):
        assert tech_taxonomy.classify(title).tech_scope == "core"

    @pytest.mark.parametrize("title", [
        "Tooling Engineer", "Global Tooling Engineering Manager",
        "Mechanical Engineer", "Plant Systems Engineer",
    ])
    def test_the_veto_still_holds_for_a_role_noun(self, title):
        assert tech_taxonomy.classify(title).tech_scope == "non_tech"


class TestAClassYearMarksAGraduateIntake:
    @pytest.mark.parametrize("title", [
        "Software Engineer Grad 2027",                       # Pure Storage
        "2027 Grad Software Engineer",
        "Software Engineer, 2026 Graduate",
    ])
    def test_entry(self, title):
        assert fit_score.title_level(title) == "entry"

    @pytest.mark.parametrize("title,level", [
        ("Senior Software Engineer", "senior"),
        ("Grad School Advisor", None),
        ("Class of 2027 Graduation Coordinator", None),
    ])
    def test_unrelated_titles_are_unchanged(self, title, level):
        assert fit_score.title_level(title) == level
