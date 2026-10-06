"""A non-technical title does not borrow an ENGINEERING family from its body.

Live, 2026-10-03, on a backend/DevOps candidate's board: GitLab "Social Impact
Manager" (stored `devops`), Oscar Health "Provider Network Associate"
(`software_engineering`), Applied Materials "Field Service Engineer", a wind
technician and two refrigeration engineers. Their titles name no family, and
`mark_requirements_parsed` adopted whatever family the BODY implied - and a
body describes the company as much as the job.
"""
from services import fit_score as fs
from services import inventory_feed
from services import job_inventory_service as inv

CH = "c" * 64


def _seed(mock_db, title, family="other"):
    mock_db.job_postings.delete_many({})
    mock_db.job_postings.insert_one({"source": "workday", "source_job_id": "f1", "content_hash": CH,
                                     "title_raw": title, "role_family": family, "seniority": "unknown"})


def _parse(title, body):
    req = fs.extract_requirements(body, title)
    req["role_family"] = "devops"  # what the body implies at a DevOps company
    return inv.mark_requirements_parsed("workday", "f1", CH, req)


class TestTheGate:
    def test_technical_titles_still_take_the_body_family(self):
        for t in ("Technology Analyst", "Member of Technical Staff",
                  "Sr. Forward Deployed Engineer", "Customer Engineer"):
            assert inv.title_admits_body_family(t), t

    def test_non_technical_titles_do_not(self):
        for t in ("Social Impact Manager", "Provider Network Associate",
                  "Field Service Engineer", "Wind Technician", "Solar O&M Technician (NJ)"):
            assert not inv.title_admits_body_family(t), t

    def test_social_impact_manager_stays_other(self, mock_db):
        _seed(mock_db, "Social Impact Manager")
        assert _parse("Social Impact Manager", "Our DevOps platform serves millions.")
        assert mock_db.job_postings.find_one({"source_job_id": "f1"})["role_family"] == "other"

    def test_member_of_technical_staff_is_upgraded(self, mock_db):
        _seed(mock_db, "Member of Technical Staff")
        assert _parse("Member of Technical Staff", "Run our Kubernetes fleet.")
        assert mock_db.job_postings.find_one({"source_job_id": "f1"})["role_family"] == "devops"

    def test_a_non_engineering_body_family_is_not_gated(self, mock_db):
        _seed(mock_db, "Charge RN - Nights")
        req = fs.extract_requirements("Registered nurse, med-surg unit.", "Charge RN - Nights")
        req["role_family"] = "nursing"
        assert inv.mark_requirements_parsed("workday", "f1", CH, req)
        assert mock_db.job_postings.find_one({"source_job_id": "f1"})["role_family"] == "nursing"


class TestTheFeedDropsAContradiction:
    def test_an_engineering_family_on_a_non_tech_posting_is_dropped(self):
        doc = {"role_family": "devops", "tech": {"tech_scope": "non_tech"}}
        assert inventory_feed._contradicts_its_family(doc)

    def test_non_engineering_families_and_unknown_scope_are_kept(self):
        assert not inventory_feed._contradicts_its_family(
            {"role_family": "nursing", "tech": {"tech_scope": "non_tech"}})
        assert not inventory_feed._contradicts_its_family(
            {"role_family": "devops", "tech": {"tech_scope": "unknown"}})
        assert not inventory_feed._contradicts_its_family({"role_family": "devops"})


class TestTheRepair:
    def _script(self):
        import importlib.util, pathlib
        p = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "repair_body_role_families.py"
        spec = importlib.util.spec_from_file_location("repair_body_role_families", p)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_resets_only_borrowed_engineering_families(self):
        mod = self._script()
        assert mod.should_reset({"title_raw": "Social Impact Manager", "role_family": "devops"})
        assert not mod.should_reset({"title_raw": "DevOps Engineer", "role_family": "devops"})
        assert not mod.should_reset({"title_raw": "Member of Technical Staff", "role_family": "software_engineering"})
        assert not mod.should_reset({"title_raw": "Charge RN", "role_family": "nursing"})


class TestEngineeringNounsWithCoreScope:
    """The first gate's dry run would have stripped 1,239 families, including
    real engineering roles whose titles the phrase rules do not know:
    "Microservices Engineer", "System Engineer", "Lead Engineer", "Sr
    Advanced Cyb Sec Archt/Engr". Each is stored `core`."""

    def test_an_engineering_noun_with_a_core_classification_is_admitted(self):
        for t in ("Microservices Engineer", "System Engineer", "Lead Engineer",
                  "Sr Advanced Cyb Sec Archt/Engr", "Applications Development Intermediate Analyst"):
            assert inv.title_admits_body_family(t, "core"), t

    def test_a_noun_alone_or_a_core_scope_alone_is_not_enough(self):
        assert not inv.title_admits_body_family("Field Service Engineer", "unknown")
        assert not inv.title_admits_body_family("Social Impact Manager", "core")
        assert not inv.title_admits_body_family("Team Lead", "core")

    def test_engineering_managers_and_unreadable_scripts_with_core_scope(self):
        assert inv.title_admits_body_family("Manager, Engineering", "core")
        assert inv.title_admits_body_family("对日软件开发实习生", "core")
        assert not inv.title_admits_body_family("对日软件开发实习生", "unknown")


class TestInternshipsKeepTheirFamily:
    """Cross-checked against employers' own boards for every real user
    (2026-10-06): 39 of 128 in-inventory roles missing from their boards had
    the wrong family. The phrase table knew "software engineer" and not
    "software engineering", so "Software Engineering Intern, Android" had no
    family from its title - and took `hr` from a body about recruiting."""

    def test_discipline_titles_classify(self):
        cases = {
            "Software Engineering Intern, Android (Summer 2027)": "software_engineering",
            "Software Engineering Winter Internship": "software_engineering",
            "High School Internship, Software Engineering": "software_engineering",
            "Web Development Intern": "software_engineering",
            "PhD Intern, Data Science (2027)": "data_science",
            "Machine Learning Intern": "data_science",
            "Data Science Intern": "data_science",
            "Test Engineering Intern": "qa",
            "Mechanical Engineering Intern": "hardware_engineering",
        }
        for title, family in cases.items():
            assert fs.classify_role(title) == family, title

    def test_non_engineering_internships_are_not_swept_in(self):
        for title in ("Marketing Intern", "Recruiting Coordinator Intern",
                      "Product Design Intern", "Sales Engineering Intern"):
            assert fs.classify_role(title) not in inv.ENGINEERING_FAMILIES, title

    def test_a_team_name_is_not_the_job(self):
        assert fs.classify_role("Software Engineer, Test Frameworks & Tooling") == "software_engineering"
        assert fs.classify_role("Data Engineer, Hardware & Consumer Devices") == "data_engineering"

    def test_the_physical_veto_still_stands_where_it_should(self):
        for title in ("Staff Hardware Reliability Engineer", "Corrosion Reliability Engineer II",
                      "Senior Reliability Engineer - Power Electronics",
                      "Build Reliability Engineer II, Propulsion", "Manufacturing Software Engineer"):
            assert fs.classify_role(title) == "hardware_engineering", title

    def test_a_technical_title_never_takes_a_non_technical_body_family(self, mock_db):
        _seed(mock_db, "Technology Analyst")
        req = fs.extract_requirements("Our recruiting team will contact you.",
                                      "Technology Analyst")
        req["role_family"] = "hr"
        assert inv.mark_requirements_parsed("workday", "f1", CH, req)
        assert mock_db.job_postings.find_one({"source_job_id": "f1"})["role_family"] == "other"
