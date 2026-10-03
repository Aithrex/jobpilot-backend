"""A posting's level must agree with what its own description asks for.

Measured on live inventory, 2026-10-02: an "entry" label sat on postings that
asked for 3-12 years - "Software Engineer II" (3+ years), "Program Manager 2"
(6-8 years), "Private Client Advisor II" (12+ years), "Business Intelligence
Developer" (5 years). Early-career users filtered for entry and got them.

Five defects compounded, and each test below pins one:

1. "II" / "2" / "Level 2" in a title read as ENTRY. On almost every ladder
   that names one, I is the entry rung and II is the next one up.
2. The title always won, so a stated "6+ years" was never consulted once the
   title carried any marker at all.
3. `_required_years` returned the SMALLEST number in the body, so "5+ years
   of engineering, 2+ years with AWS" read as 2.
4. An early-career or internship phrase anywhere in the body - including
   boilerplate like "we also run an internship program" - beat stated years.
5. Only the first 4,000 characters were read, and requirements sections are
   usually at the END of a long description.
"""
from services import fit_score as fs

FILLER = "About us. We build software for customers worldwide. " * 120  # > 4,000 chars


def level(title, body="", years=None):
    return fs.level_from_signals(title, description=body, required_years=years)


class TestTitleNumerals:
    def test_roman_one_is_entry(self):
        assert fs.title_level("Software Engineer I") == "entry"

    def test_roman_two_is_mid_not_entry(self):
        assert fs.title_level("Software Engineer II") == "mid"
        assert fs.title_level("Site Reliability Engineer II (GCP & Azure)") == "mid"
        assert fs.title_level("Technical Business Analyst - Advisor II") == "mid"

    def test_arabic_two_is_mid_not_entry(self):
        assert fs.title_level("Program Manager 2") == "mid"

    def test_level_two_is_mid(self):
        assert fs.title_level("Support Engineer Level 2") == "mid"

    def test_level_one_is_still_entry(self):
        assert fs.title_level("Support Engineer Level 1") == "entry"

    def test_three_is_still_senior(self):
        assert fs.title_level("Software Engineer III") == "senior"

    def test_a_count_of_years_in_a_title_is_not_a_rung(self):
        """Live 2026-10-02: "Mechanical Engineer (M.S. + 3 years of consulting
        experience)" read `senior` because the bare 3 matched the III/3 rung.
        The years still count - as years."""
        t = "Mechanical Engineer (M.S. + 3 years of consulting experience)"
        assert fs.title_level(t) is None
        assert level(t) == "mid"
        assert fs.title_level("Research Scientist (M.S. + 4 yrs of exp)") is None
        assert fs.title_level("Nurse - 2 years experience required") is None


class TestStatedYearsOutrankTheTitle:
    def test_years_raise_an_entry_title(self):
        body = "Requirements: 6+ years of experience in software development."
        assert level("Associate Software Engineer", body) == "senior"

    def test_years_raise_a_numbered_title(self):
        body = "Basic qualifications: 3+ years of hands-on Java development experience."
        assert level("Software Engineer II", body) == "mid"
        body = "8 years’ experience supporting customers in a project/technical lead role."
        assert level("Program Manager 2", body) == "staff"

    def test_years_never_lower_a_stated_level(self):
        body = "2+ years of professional experience."
        assert level("Senior Software Engineer", body) == "senior"

    def test_a_lead_title_with_senior_years_is_senior(self):
        body = "7+ years of experience in data science or machine learning."
        assert level("Lead Data Scientist", body) == "senior"

    def test_an_intern_title_stays_intern(self):
        body = "Currently enrolled. 3+ years of programming experience through coursework."
        assert level("Software Engineering Intern", body) == "intern"

    def test_an_unmarked_title_reads_its_years(self):
        assert level("Business Intelligence Developer",
                     "5 years of experience with BI tooling required.") == "senior"
        assert level("Software Engineer", "0-2 years of experience.") == "entry"

    def test_explicit_required_years_still_count(self):
        assert level("Data Analyst", years=4) == "mid"


class TestBodyPhrasesDoNotBeatStatedYears:
    def test_new_grad_phrase_with_senior_years(self):
        body = ("We hire new grads across many teams. For this role: "
                "5+ years of professional software engineering experience.")
        assert level("Software Engineer", body) == "senior"

    def test_internship_boilerplate_with_senior_years(self):
        body = ("7+ years of experience building distributed systems. "
                "We also run a summer internship program for students.")
        assert level("Software Engineer", body) == "senior"

    def test_new_grad_phrase_alone_is_still_entry(self):
        assert level("Software Engineer", "This role is for new grads.") == "entry"


class TestHeadlineYears:
    def test_the_headline_requirement_not_the_smallest_number(self):
        body = ("5+ years of software engineering experience. "
                "2+ years of experience with AWS.")
        assert fs._required_years(body) == 5

    def test_years_after_the_first_4000_characters_are_read(self):
        body = FILLER + " Qualifications: 6+ years of relevant industry experience."
        assert fs._required_years(fs._norm(body)) == 6
        assert level("Software Engineer", body) == "senior"

    def test_age_and_company_history_are_not_experience(self):
        assert fs._required_years("Must be 18 years of age. Over 100 years in business.") is None

    def test_award_streaks_are_not_experience(self):
        """Live false positives from the pre-apply spot check, 2026-10-02:
        a nursing assistant and a maintenance assistant read as `staff`."""
        assert fs._required_years(
            'Voted 13 years in a row in the Baltimore Sun as a "top workplace".') is None
        assert fs._required_years(
            'Named a "top workplace" for 14 years in a row and recognized by Newsweek.') is None
        assert level("Maintenance Assistant",
                     "Named a top workplace for 14 years in a row.") == "entry"

    def test_real_experience_phrasings_still_count(self):
        assert fs._required_years("5+ years running production Kubernetes clusters.") == 5
        assert fs._required_years("3 years of experience with award-winning teams.") == 3

    def test_work_must_be_the_word_work(self):
        assert fs._required_years("Proud of 12 years as a great workplace.") is None
        assert fs._required_years("3 years of work experience in retail.") == 3

    def test_curly_apostrophe_and_hyphenated_context(self):
        assert fs._required_years("8 years’ experience leading programs") == 8
        assert fs._required_years("3+ years of hands-on Java development experience") == 3

    def test_preferred_does_not_outrank_required(self):
        body = ("Required: 3+ years of experience in backend development. "
                "Preferred: 5+ years of experience in fintech.")
        assert fs._required_years(body) == 3

    def test_preferred_only_still_counts(self):
        assert fs._required_years("Nice to have: 4+ years of experience with Go.") == 4

    def test_degree_alternatives_take_the_bachelor_path(self):
        body = ("Bachelor's degree and 8 years of related experience, "
                "or a Master's degree and 6 years of related experience.")
        assert fs._required_years(body) == 8

    def test_nothing_stated_is_none(self):
        assert fs._required_years("Join a great team building great things.") is None


class TestRequirementsAgree:
    def test_extracted_requirements_carry_the_headline(self):
        body = ("Responsibilities: build services.\n"
                "Qualifications: 6+ years of experience in software development; "
                "2+ years of experience with Kubernetes.")
        req = fs.extract_requirements(body, "Software Engineer I")
        assert req["required_years"] == 6
        assert req["job_level"] == "senior"


class TestTheScoreSeesTheRealRequirement:
    """The scorer reads the same extraction, so a junior candidate stops
    scoring a disguised senior posting as a fit. Live 2026-10-02: a two-year
    candidate scored 77 against "Senior Manager, Scrum Master" (7 years)."""

    JD = ("We are hiring across many teams.\n"
          "Qualifications:\n"
          "6+ years of professional software engineering experience.\n"
          "2+ years of experience with Python and SQL.\n"
          "Experience with Git and unit testing.")

    def _junior(self):
        from evals.fixtures.fit_score_cases import RESUMES
        return RESUMES["r_swe_junior"]

    def test_a_disguised_senior_posting_is_capped_for_a_junior(self):
        v = fs.score(self._junior(), {"title": "Software Engineer II", "description": self.JD})
        assert v["score"] <= fs.CONSTRAINT_CAPS["severe_experience_gap"], v["score"]

    def test_the_same_skills_at_the_right_level_still_score_well(self):
        honest = self.JD.replace("6+ years of professional", "0-2 years of professional")
        fair = fs.score(self._junior(), {"title": "Software Engineer I", "description": honest})
        disguised = fs.score(self._junior(), {"title": "Software Engineer II", "description": self.JD})
        assert fair["score"] > disguised["score"] + 15, (fair["score"], disguised["score"])


class TestReparsingMovesTheStoredLevel:
    """`mark_requirements_parsed` is how a re-extraction reaches the posting.
    It used to update `requirements` and `role_family` only, so a corrected
    `job_level` landed in `requirements` while `seniority` - the field the
    feed FILTERS on - and `tech.seniority` - the one the card SHOWS - kept
    the old answer forever."""

    CH = "a" * 64
    JD = "Qualifications: 6+ years of professional software engineering experience."

    def _seed(self, mock_db, **extra):
        mock_db.job_postings.delete_many({})  # mock_db keeps documents across tests
        doc = {"source": "workday", "source_job_id": "r1", "content_hash": self.CH,
               "title_raw": "Software Engineer II", "seniority": "entry",
               "tech": {"seniority": "entry", "required_years_min": 2}}
        doc.update(extra)
        mock_db.job_postings.insert_one(doc)

    def _parse(self):
        from services import job_inventory_service as inv
        req = fs.extract_requirements(self.JD, "Software Engineer II")
        return inv.mark_requirements_parsed("workday", "r1", self.CH, req)

    def test_seniority_and_the_card_follow_the_extraction(self, mock_db):
        self._seed(mock_db)
        assert self._parse()
        doc = mock_db.job_postings.find_one({"source_job_id": "r1"})
        assert doc["seniority"] == "senior"
        assert doc["tech"]["seniority"] == "senior"
        assert doc["tech"]["required_years_min"] == 6
        assert doc["requirements"]["required_years"] == 6

    def test_a_posting_without_a_tech_block_is_fine(self, mock_db):
        self._seed(mock_db, tech=None)
        assert self._parse()
        doc = mock_db.job_postings.find_one({"source_job_id": "r1"})
        assert doc["seniority"] == "senior"
        assert doc["tech"] is None

    def test_no_level_in_the_extraction_never_blanks_a_stated_one(self, mock_db):
        from services import job_inventory_service as inv
        self._seed(mock_db, title_raw="Engineer", seniority="mid")
        req = fs.extract_requirements("Build things with us.", "Engineer")
        assert req["job_level"] is None
        assert inv.mark_requirements_parsed("workday", "r1", self.CH, req)
        assert mock_db.job_postings.find_one({"source_job_id": "r1"})["seniority"] == "mid"


class TestTheRederiveBackfill:
    """`scripts/rederive_posting_levels.py` goes through the crawler's own
    write and never trusts a truncated body over a full one."""

    def _script(self):
        import importlib.util, pathlib
        p = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "rederive_posting_levels.py"
        spec = importlib.util.spec_from_file_location("rederive_posting_levels", p)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_the_full_offloaded_body_beats_the_inline_prefix(self):
        mod = self._script()
        doc = {"description_text": "About us...", "description_ref": "a" * 64}
        full = "About us... Qualifications: 6+ years of experience in software."
        assert mod.full_body(doc, lambda ref: full) == full
        assert mod.full_body({"description_text": "inline only"}, None) == "inline only"

    def test_apply_moves_the_stored_level(self, mock_db):
        mod = self._script()
        mock_db.job_postings.delete_many({})
        ch = "b" * 64
        mock_db.job_postings.insert_one({
            "source": "workday", "source_job_id": "x1", "content_hash": ch,
            "title_raw": "Program Manager 2", "seniority": "entry", "status": "active"})
        doc = mock_db.job_postings.find_one({"source_job_id": "x1"})
        body = "8 years' experience supporting customers in a technical lead role."
        assert mod.rederive_one(doc, body, apply=False) == ("would_write", "staff")
        assert mock_db.job_postings.find_one({"source_job_id": "x1"})["seniority"] == "entry"
        assert mod.rederive_one(doc, body, apply=True) == ("written", "staff")
        assert mock_db.job_postings.find_one({"source_job_id": "x1"})["seniority"] == "staff"

    def test_no_body_is_left_for_the_crawler(self):
        assert self._script().rederive_one({}, "   ", apply=True) == ("no_body", None)


class TestLeadAndHighRungs:
    """From one candidate's live feed, 2026-10-03: JPMorgan's "Lead Software
    Engineer" (a VP-grade role asking 5+ years) read `mid` and scored 98 for a
    junior; Netflix's "Software Engineer 5" read `unknown`."""

    def test_a_lead_engineer_is_senior(self):
        for t in ("Lead Software Engineer - Java/Python/AWS",
                  "Lead Data Scientist", "React/Java Lead Fullstack Engineer",
                  "Lead Backend Developer"):
            assert fs.title_level(t) == "senior", t

    def test_a_lead_that_is_not_engineering_is_unchanged(self):
        assert fs.title_level("Lead Cashier") == "mid"
        assert fs.title_level("Team Lead, Customer Support") == "mid"

    def test_rungs_five_and_six_are_senior(self):
        assert fs.title_level("Software Engineer 5 - Partner Test Tech") == "senior"
        assert fs.title_level("Systems Engineer V") == "senior"
        assert fs.title_level("Software Engineer VI") == "senior"


class TestTheNvidiaEngineeringManager:
    """Shown on an Entry + Mid board, 2026-10-03: NVIDIA "Software Engineering
    Manager - Cloud Streaming" asks "8+ overall years ... with 3+ years ...
    leading teams". Three defects at once:

    1. Workday serves descriptions HTML-entity-encoded ("8&#43; years"), and
       `_norm` stripped tags but never decoded entities - so no "+" ever
       matched. Sampled: 62% of Workday bodies are encoded, and decoding
       changes the years reading for 22% of Workday postings.
    2. "8+ overall years" - a qualifier between the number and "years" - was
       not read at all.
    3. "Software Engineering Manager" fell to the generic manager rule (mid).
    """

    JD = ("What we need to see:\\n"
          "BS/MS degree in Computer Science or related areas\\n"
          "8&#43; overall years of overall experience designing and building "
          "distributed software systems, including edge and networking "
          "services, with 3&#43; years of experience leading and developing "
          "teams of engineers.")

    def test_entities_are_decoded(self):
        assert "8+" in fs._norm("8&#43; years")
        assert fs._norm("C&#43;&#43; &amp; Go") == "c++ & go"

    def test_a_qualifier_before_years_is_read(self):
        assert fs._required_years("8+ overall years of overall experience in software") == 8
        assert fs._required_years("5+ total years of professional experience") == 5
        assert fs._required_years("minimum 6 combined years of relevant experience") == 6

    def test_engineering_managers_are_not_mid(self):
        for t in ("Software Engineering Manager - Cloud Streaming",
                  "Manager, Software Engineering", "Engineering Manager, Platform",
                  "Software Development Manager"):
            assert fs.title_level(t) == "senior", t
        assert fs.title_level("Restaurant Manager") == "mid"

    def test_the_posting_is_read_as_staff(self):
        assert fs._required_years(fs._norm(self.JD)) == 8
        assert fs.level_from_signals("Software Engineering Manager - Cloud Streaming",
                                     description=self.JD) == "staff"


class TestLeadSuffixAndEmployerLadders:
    """Open items from 2026-10-03: "Site Reliability Engineering Lead" read
    `mid` (the lead rule only matched "Lead <...> Engineer"), and employer
    level codes - Netflix "(L5)", Meta "E4", Apple "ICT3" - were not read."""

    def test_lead_after_the_discipline_is_senior(self):
        for t in ("Site Reliability Engineering Lead", "Software Engineering Lead",
                  "Tech Lead, Payments", "Technical Lead - Platform", "Data Engineering Lead"):
            assert fs.title_level(t) == "senior", t
        assert fs.title_level("Team Lead, Customer Support") == "mid"
        assert fs.title_level("Shift Lead") != "senior"  # an existing rule reads it entry

    def test_employer_ladder_codes(self):
        cases = [("Software Engineer (L5) - JavaScript Platform", "Netflix", "senior"),
                 ("Distributed Systems Engineer (L4) - Data Platform", "Netflix", "mid"),
                 ("Software Engineer III, L4", "Google", "mid"),
                 ("Software Engineer, E4", "Meta", "mid"),
                 ("Software Development Engineer, L5", "Amazon", "mid"),
                 ("Software Engineer (ICT3)", "Apple Inc.", "mid"),
                 ("Software Engineer, L3", "Uber Technologies", "entry")]
        for title, employer, want in cases:
            assert fs.employer_ladder_level(title, employer) == want, (title, employer)

    def test_unknown_employers_and_plain_titles_have_no_ladder(self):
        assert fs.employer_ladder_level("Software Engineer (L5)", "Acme Corp") is None
        assert fs.employer_ladder_level("Software Engineer", "Netflix") is None

    def test_the_ladder_is_the_stated_level_and_years_still_raise(self):
        assert fs.level_from_signals("Software Engineer (L5)", employer="Netflix") == "senior"
        assert fs.level_from_signals("Software Engineer (L4)", employer="Netflix",
                                     required_years=6) == "senior"
        assert fs.level_with_employer("unknown", "Software Engineer (L5)", "Netflix", None) == "senior"
        assert fs.level_with_employer("mid", "Software Engineer", "Netflix", None) == "mid"
