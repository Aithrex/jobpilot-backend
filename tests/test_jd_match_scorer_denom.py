"""Denominator clamp regression for the deterministic JD scorer.

The 2026-07-20 incident: a 28-skill resume needed 12 skills named in one
posting to clear the guest min_score of 40, so skill-rich resumes saw zero
matches everywhere. The denominator is now clamped to
[min_resume_skills, max_denom_skills].
"""

from services.jd_match_scorer import score_one


def _skills(n):
    return [f"skill{i}" for i in range(n)]


def _jd_mentioning(skills):
    return "We want experience with " + ", ".join(skills) + "."


class TestDenomClamp:
    def test_rich_resume_no_longer_starved(self):
        # 28 skills, 6 mentioned in the JD: old score = 6/28 = 21 (dropped);
        # new score = 6/14 = 43 (clears the guest bar of 40).
        skills = _skills(28)
        verdict = score_one(_jd_mentioning(skills[:6]), "Software Engineer", skills)
        assert verdict["match_score"] >= 40
        assert verdict["explain"] == "6/14"

    def test_mid_size_resume_unchanged(self):
        # 12 skills sits inside the clamp: denominator stays 12.
        skills = _skills(12)
        verdict = score_one(_jd_mentioning(skills[:5]), "Engineer", skills)
        assert verdict["explain"] == "5/12"
        assert verdict["match_score"] == 42

    def test_small_resume_floor_unchanged(self):
        # 3 skills all mentioned: floor of 8 still prevents a free 100.
        skills = _skills(3)
        verdict = score_one(_jd_mentioning(skills), "Engineer", skills)
        assert verdict["explain"] == "3/8"
        assert verdict["match_score"] == 38

    def test_cap_never_inflates_past_100(self):
        skills = _skills(30)
        verdict = score_one(_jd_mentioning(skills), "Engineer", skills)
        assert verdict["match_score"] <= 100
