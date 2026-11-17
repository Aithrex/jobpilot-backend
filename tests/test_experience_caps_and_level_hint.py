"""2026-11-17 owner report: "jobs are shown as suited when they ask for more
experience than I have", and "many jobs say no level".

* `experience_short` / `experience_stretch` (fit-v2.11): a minimum the posting
  STATES that the resume falls 2+ years short of caps the match at "possible"
  (64); more than one year short, never "strong" (79). Measured on production
  boards: 1,153 of 39,229 cards were strong or good matches 2+ years short.
* `level_hint` (EXTRACTOR_VERSION 17): read since v15, never stored - the
  storage allowlist dropped it, so no card ever showed "Likely Senior".
"""
from services import fit_score
from services import inventory_feed
from services import job_inventory_service as inv

SKILLS = "Python, SQL, AWS, Docker and PostgreSQL"


def _resume(dates):
    return {
        "contact": {"name": "Jordan Alvarez", "location": "Austin, TX"},
        "summary": "Backend engineer.",
        "skills": {"languages": ["Python", "SQL"], "cloud": ["AWS", "Docker", "PostgreSQL"]},
        "experience": [{"title": "Backend Engineer", "company": "Acme", "dates": dates,
                        "bullets": ["Built Python services on AWS with Docker, SQL and PostgreSQL."]}],
        "education": [{"degree": "BS Computer Science", "institution": "UT"}],
        "projects": [], "certifications": [],
    }


def _score(dates, years):
    jd = (f"Backend Engineer. Requirements: {years}+ years of professional experience "
          f"building backend services. {SKILLS}.")
    return fit_score.score(_resume(dates), {"title": "Backend Engineer", "description": jd,
                                            "location": "Austin, TX"})


def _types(v):
    return {c["type"] for c in v["hard_constraints"]}


class TestStatedYearsCaps:
    def test_two_or_more_years_short_is_at_most_a_possible_match(self):
        v = _score("Jan 2021 - Jan 2025", 7)                 # ~4 years (mid) vs 7
        assert "experience_short" in _types(v)
        assert v["score"] <= fit_score.CONSTRAINT_CAPS["experience_short"]
        assert v["band"] in ("possible match", "weak match", "unlikely match")

    def test_more_than_a_year_short_is_never_strong(self):
        v = _score("Jan 2022 - Jun 2025", 5)                 # ~3.4 years vs 5
        assert "experience_stretch" in _types(v)
        assert v["score"] <= fit_score.CONSTRAINT_CAPS["experience_stretch"]
        assert v["band"] != "strong match"

    def test_within_a_year_is_not_capped(self):
        v = _score("Jan 2021 - Jun 2025", 5)                 # ~4.4 years vs 5
        assert not _types(v) & {"experience_short", "experience_stretch"}

    def test_the_severe_gap_keeps_its_own_lower_cap(self):
        v = _score("Jan 2024 - Jun 2025", 8)                 # 6.5 short
        assert "experience_gap" in _types(v)
        assert not _types(v) & {"experience_short", "experience_stretch"}
        assert v["score"] <= fit_score.CONSTRAINT_CAPS["severe_experience_gap"]

    def test_a_short_resume_is_never_made_ineligible_by_years(self):
        v = _score("Jan 2024 - Jun 2025", 5)
        assert v["eligibility"] == fit_score.ELIGIBLE

    def test_no_stated_years_means_no_cap(self):
        assert fit_score._stated_years_short({"required_years": None}, {"years": 1}) is None
        assert fit_score._stated_years_short({"required_years": 5}, {"years": None}) is None
        assert fit_score._stated_years_short({"required_years": 5},
                                             {"years": 3, "experience_months": 44}) == \
            5 - 44 / 12


class TestLevelHintIsStored:
    def test_the_storage_allowlist_keeps_the_hint(self):
        block = inv._safe_jd_block({"v": "jd-1", "level_hint": {
            "level": "senior", "basis": "You will mentor junior engineers."}})
        assert block["level_hint"] == {"level": "senior",
                                       "basis": "You will mentor junior engineers."}

    def test_no_hint_stores_none(self):
        assert inv._safe_jd_block({"v": "jd-1"})["level_hint"] is None
        assert inv._safe_jd_block({"v": "jd-1", "level_hint": {"level": ""}})["level_hint"] is None

    def test_a_posting_read_end_to_end_shows_likely_senior(self):
        jd = ("Software Engineer. You will mentor junior engineers and own the architecture "
              "for our payments platform. Python, AWS.")
        reqs = fit_score.extract_requirements(jd, "Software Engineer")
        stored = {"jd": inv._safe_jd_block(reqs["jd"])}
        est = inventory_feed.level_estimate_of("unknown", stored)
        assert est and est["level"] == "senior"
        assert "mentor" in est["basis"].lower()

    def test_the_extractor_version_moved_so_stored_rows_are_re_read(self):
        assert inv.EXTRACTOR_VERSION == 17
