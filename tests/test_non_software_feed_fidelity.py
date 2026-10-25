"""The For-you board for people outside software.

Replaying every real account's board (`scripts/user_feed_fidelity_audit.py`,
2026-10-21) found three accounts the feed had never been measured for:

* a power-systems graduate whose titles were assistantships. Their resume
  classified as no family, retrieval ran over the whole market, and 0 of their
  top 25 were electrical while software, billing and sleep-lab roles filled it;
* a Workday HCM analyst, 1 of 25 in HR systems, under HR generalists and
  "Floral Manager", which is filed `hr` only because its posting mentions HR;
* a data analyst whose only held title was "Assistant Engineer", searching for
  "Professional Upskilling & Career Break" while their declared targets were
  "Data Scientist" and "data analyst".

Every fix here is generic; each test names the class it protects.
"""
from datetime import datetime, timezone

import pytest

from _vg_support import current_db

from services import experience_view, fit_score, inventory_feed
from services.job_title_seed import canonical_search_titles, match_titles_for


def _power_resume(**extra):
    resume = {
        "summary": ("Electrical Power & Energy Systems graduate student with "
                    "experience in power system analysis and load flow studies."),
        "skills": {"technical": ["Load Flow Analysis", "Transformer Modeling", "Python"]},
        "experience": [
            {"title": "Graduate Service Assistant", "company": "State University",
             "dates": "Aug 2025 - Present", "bullets": ["Supported lab sessions."]},
            {"title": "Graduate Research Assistant", "company": "State University",
             "dates": "Jan 2025 - Aug 2025", "bullets": ["Modelled feeders."]},
            {"title": "Entry-Level Electrical Design Trainee", "company": "Carrier",
             "dates": "Nov 2023 - Aug 2024", "bullets": ["Drew panel schedules."]},
            {"title": "Intern", "company": "Bosch", "dates": "Jul 2022 - Aug 2023",
             "bullets": ["Assisted the controls team."]},
        ],
        "education": [
            {"degree": "Master of Science, Electrical Power & Energy Systems"},
            {"degree": "B.E. in Electrical and Electronics Engineering"},
        ],
    }
    resume.update(extra)
    return resume


def _hris_resume():
    return {
        "summary": "Workday HCM analyst supporting HRIS operations.",
        "skills": {"hr": ["Workday HCM", "HRIS", "Reporting"]},
        "experience": [
            {"title": "Workday Analyst", "company": "Health Co",
             "dates": "Jan 2022 - Present", "bullets": ["Configured business processes."]},
            {"title": "HCM Analyst", "company": "IT Services",
             "dates": "Jan 2020 - Dec 2021", "bullets": ["Built calculated fields."]},
        ],
        "education": [{"degree": "Masters in Business Analytics"}],
    }


class TestRoleVocabulary:
    @pytest.mark.parametrize("title", [
        "Power Systems Studies Engineer II",     # was stored `legal`
        "Senior Electrical Power Engineer",      # was stored `operations`
        "Power Systems Engineer I",
        "Substation Engineer",
        "Protection and Control Engineer",
        "Entry-Level Electrical Design Trainee",
        "Chemical Engineering Intern",
    ])
    def test_power_and_discipline_titles_are_engineering(self, title):
        assert fit_score.classify_role(title) == "hardware_engineering"

    @pytest.mark.parametrize("title", [
        "Workday Analyst", "HCM Analyst", "Workday Consultant",
        # The HRIS market's own titles, filed as it_support or nothing before
        # 2026-10-24 and so never retrieved for an HRIS analyst.
        "Workday Systems Analyst - Benefits & HCM", "Workday HCM Specialist",
        "Workday HCM Administrator - Benefits", "SAP SuccessFactors Employee Central Consultant",
        "UKG Administrator",
    ])
    def test_hr_systems_titles_are_hr(self, title):
        assert fit_score.classify_role(title) == "hr"

    def test_a_software_role_on_an_hr_platform_stays_software(self):
        assert fit_score.classify_role("Workday HCM Software Engineer") == "software_engineering"

    def test_the_degree_form_names_the_discipline(self):
        assert fit_score.classify_role(
            "B.E. in Electrical and Electronics Engineering") == "hardware_engineering"

    def test_electronics_and_communication_decides_nothing(self):
        # An Indian ECE degree feeds software and data at least as often.
        assert fit_score.classify_role(
            "B.Tech Electronics and Communication Engineering") is None


class TestResumeFamilies:
    def test_a_resume_of_assistantships_reads_its_summary(self):
        prof = fit_score.build_resume_profile(_power_resume())
        assert prof["families"] == {"hardware_engineering"}

    ASSISTANTSHIPS = [{"title": "Graduate Research Assistant", "dates": "2024 - 2025"},
                      {"title": "Intern", "dates": "2023 - 2024"}]

    def test_declared_targets_before_the_degree(self):
        resume = _power_resume(summary="", experience=self.ASSISTANTSHIPS)
        prof = fit_score.build_resume_profile(resume, target_roles=["Data Scientist"])
        assert prof["families"] == {"data_science"}

    def test_the_degree_is_the_last_resort(self):
        prof = fit_score.build_resume_profile(
            _power_resume(summary="", experience=self.ASSISTANTSHIPS))
        assert prof["families"] == {"hardware_engineering"}

    def test_titles_still_lead(self):
        prof = fit_score.build_resume_profile(
            _power_resume(experience=[{"title": "Backend Engineer", "dates": "2020 - 2024"}]))
        assert prof["families"] == {"software_engineering"}


class TestRetrievalFamilies:
    def test_unreadable_titles_fall_back_to_the_resume(self):
        fams = inventory_feed._role_families(
            ["Graduate Service Assistant"], [], ["hardware_engineering"])
        assert fams == ["hardware_engineering"]

    def test_no_titles_still_means_everything(self):
        assert inventory_feed._role_families([], [], ["hardware_engineering"]) == []


class TestSearchTitles:
    def test_the_discipline_leads_and_assistantships_go(self):
        titles = canonical_search_titles(
            ["Graduate Service Assistant", "Graduate Research Assistant",
             "Entry-Level Electrical Design Trainee", "Intern"],
            degrees=["Master of Science, Electrical Power & Energy Systems"])
        assert set(titles[:2]) == {"Power Systems Engineer", "Electrical Engineer"}
        assert "Graduate Service Assistant" not in titles
        assert "Graduate Research Assistant" not in titles

    def test_a_technician_is_not_handed_an_engineer_search(self):
        assert canonical_search_titles(["Electrical Technician"]) == ["Electrical Technician"]

    def test_a_nurse_keeps_their_own_titles(self):
        assert canonical_search_titles(
            ["Registered Nurse"], degrees=["BSN Nursing"]) == ["Registered Nurse"]

    def test_never_empty_when_every_title_is_an_assistantship(self):
        assert canonical_search_titles(["Graduate Research Assistant"]) == [
            "Graduate Research Assistant"]


class TestDeclaredTargetsDriveTheSearch:
    USER = "fidelity@example.com"
    COLLECTIONS = ("resume_assets", "users", "user_preferences", "job_filter_preferences")

    @pytest.fixture()
    def db(self):
        d = current_db()
        for c in self.COLLECTIONS:
            d[c].delete_many({})
        yield d
        for c in self.COLLECTIONS:
            d[c].delete_many({})

    def test_a_declared_target_replaces_a_career_break(self, db):
        now = datetime.now(timezone.utc)
        db.users.insert_one({"email": self.USER, "active_resume_id": "ra-fid"})
        db.user_preferences.insert_one({
            "user_email": self.USER, "target_roles": ["Data Scientist", "data analyst"]})
        db.resume_assets.insert_one({
            "resume_id": "ra-fid", "user_email": self.USER, "owner_type": "user",
            "filename": "r.pdf", "parse_status": "parsed", "created_at": now,
            "parsed_at": now,
            "structured": {
                "summary": "", "skills": {"data": ["SQL", "Power BI"]},
                "experience": [{"title": "Assistant Engineer", "dates": "2019 - 2021"},
                               {"title": "Professional Upskilling & Career Break"}],
                "education": [],
            },
        })
        titles = match_titles_for(self.USER, ["Assistant Engineer",
                                              "Professional Upskilling & Career Break"])
        assert "Data Scientist" in titles and "Data Analyst" in titles
        assert "Professional Upskilling & Career Break" not in titles


class TestEarlyCareerLevel:
    STUDENT = ["Graduate Service Assistant", "Graduate Research Assistant",
               "Entry-Level Electrical Design Trainee", "Intern"]

    def test_student_roles_cannot_make_someone_mid_level(self):
        assert fit_score.early_career_only(self.STUDENT)
        assert fit_score.resume_level(self.STUDENT, 3) == "entry"

    def test_one_regular_role_and_the_years_ladder_applies(self):
        titles = ["Software Engineer, Contract", "Software Engineering Intern"]
        assert not fit_score.early_career_only(titles)
        assert fit_score.resume_level(titles, 3) == "mid"

    def test_the_board_opens_on_internships_and_new_grad_roles(self):
        view = experience_view.resolve(None, 3, early_career=True)
        assert view["levels"] == ["intern", "entry"]
        assert view["years"] == 3

    def test_a_stated_level_still_wins(self):
        view = experience_view.resolve("Senior", 3, early_career=True)
        assert view["levels"] == ["senior"]


class TestSpecialtyInsideALegacyFamily:
    @staticmethod
    def _tier(resume, title, family):
        prof = fit_score.build_resume_profile(resume)
        return fit_score._legacy_role_tier({"role_family": family}, prof, title)

    def test_same_engineering_discipline_beats_a_different_one(self):
        same = self._tier(_power_resume(), "Electrical Design Engineer - Electric Power",
                          "hardware_engineering")
        other = self._tier(_power_resume(), "Mechanical Engineer", "hardware_engineering")
        assert same[0] == "adjacent_specialty" and other[0] == "adjacent_family"
        assert same[1] > other[1]

    def test_hr_systems_is_not_hr_generalist(self):
        assert self._tier(_hris_resume(), "Senior HRIS Analyst", "hr")[0] == "adjacent_specialty"
        assert self._tier(_hris_resume(), "HR Generalist", "hr")[0] == "adjacent_family"

    def test_no_specialty_on_one_side_keeps_same_family(self):
        assert self._tier(_hris_resume(), "Human Resources Manager", "hr")[0] == "same_family"

    def test_a_family_only_the_body_supplied_is_partial_credit(self):
        # Not None: an unknown component is excluded from the average and
        # would score the row ABOVE a title-confirmed match.
        tier = self._tier(_hris_resume(), "Floral Department Manager", "hr")
        assert tier is not None and tier[0] == "adjacent_family"

    def test_the_score_ranks_the_resume_s_own_discipline_first(self):
        prof = fit_score.build_resume_profile(_power_resume())
        reqs = {"role_family": "hardware_engineering"}
        electrical, _, _, _ = fit_score._score_role(reqs, prof, "Electrical Engineer I")
        mechanical, _, _, _ = fit_score._score_role(reqs, prof, "Mechanical Engineer I")
        assert electrical > mechanical
