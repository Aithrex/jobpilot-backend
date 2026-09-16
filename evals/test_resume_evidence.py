"""What a RÉSUMÉ is allowed to say about itself, and what it is not.

`eval_fit_score_domains.py` measures ranking over 140 labelled scenarios.
This file asserts the individual RULES underneath it — the ones a ranking
metric cannot see because they are about what happens when evidence is absent,
weak, or of the wrong kind. Every class below exists because of a specific
measured failure; the docstrings name it.

Offline and deterministic: no database, no network, no LLM. `fit_score` and
`tech_taxonomy` are both pure.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT / "shared" / "python"), str(ROOT / "evals")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from services import fit_score                        # noqa: E402
from services import tech_taxonomy as tx              # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _resume(**over):
    base = {
        "contact": {"name": "T", "email": "t@example.invalid",
                    "location": "Austin, TX"},
        "summary": "",
        "skills": {}, "experience": [], "education": [],
        "projects": [], "certifications": [],
    }
    base.update(over)
    return base


def _job(title, body, **over):
    j = {"title": title, "description": body, "location": "Remote, US"}
    j.update(over)
    return j


# ---------------------------------------------------------------------------
class TestPackagedTitlesReachTheirDiscipline:
    """The live bug, 2026-09-15.

    `classify("Data Scientist")` -> data/data_science at 0.88.
    `classify("Data Science Intern")` -> NO family, and the résumé carrying it
    scored `role_alignment: unknown` against 4,000 of 4,000 live postings.
    """

    def test_an_intern_suffix_does_not_erase_the_discipline(self):
        got = tx.classify("Data Science Intern")
        assert got.tech_scope == tx.SCOPE_CORE
        assert "data" in got.role_families
        assert "data_science" in got.role_specialties

    def test_the_intern_signal_survives_as_seniority(self):
        """Stripping the modifier for MATCHING must not lose it for LEVELLING.
        Two questions, two readings of the same string."""
        assert tx.classify("Data Science Intern").seniority == "intern"
        assert tx.classify("Machine Learning Co-op").seniority == "intern"

    def test_level_codes_and_ladders(self):
        for title, family in (("SDE II", "software_engineering"),
                              ("Software Engineer 2", "software_engineering"),
                              ("L5 Software Engineer", "software_engineering"),
                              ("Software Development Engineer I",
                               "software_engineering")):
            got = tx.classify(title)
            assert family in got.role_families, (title, got.role_families)

    def test_department_tails_and_parentheticals(self):
        assert "data" in tx.classify("Senior Data Scientist, Fraud").role_families
        assert "software_engineering" in tx.classify(
            "Sr. Software Engineer (Payments), Seattle").role_families

    def test_stripping_a_modifier_never_manufactures_a_discipline(self):
        """The direction that must not move. "Marketing Intern" stripped is
        "marketing", which names nothing technical and must stay that way."""
        for title in ("Marketing Intern", "Warehouse Associate",
                      "Barista (Part-Time)", "Pharmacy Technician Intern"):
            got = tx.classify(title)
            assert got.tech_scope in (tx.SCOPE_UNKNOWN, tx.SCOPE_NON_TECH), (
                title, got.tech_scope, got.role_families)

    def test_the_raw_title_always_wins(self):
        """A stripped reading is a FALLBACK. Anything the title says as
        written must be unchanged by this machinery."""
        assert tx.title_variants("Backend Engineer")[0] == "backend engineer"
        got = tx.classify("Backend Engineer")
        assert got.role_specialties == ["backend"]
        assert got.classification_confidence >= 0.88


class TestSkillsAreFirstClassEvidence:
    """`classify_resume`'s body fallback called `classify("", description=...)`
    and `classify` returns immediately on an empty title — so the branch was
    dead code and a résumé's tools were never read at all."""

    AI_ML_SKILLS = ["Python", "PyTorch", "TensorFlow", "scikit-learn",
                    "XGBoost", "NLP", "computer vision", "LLMs", "RAG",
                    "Apache Spark", "SQL", "Pandas", "NumPy"]

    def test_tools_alone_classify_a_resume_whose_titles_say_nothing(self):
        got = tx.classify_resume(titles=["Technical Associate", "Analyst"],
                                 skills=self.AI_ML_SKILLS)
        assert got["tech_scope"] == tx.SCOPE_CORE
        assert set(got["role_families"]) & {"ai_ml", "data"}
        assert "skills" in got["evidence_sources"]

    def test_tools_alone_never_reach_the_cross_domain_veto_threshold(self):
        """`fit_score._eligibility` needs confidence >= 0.70 to declare a
        cross-domain INELIGIBLE. Nothing a person merely LISTS may do that."""
        got = tx.classify_resume(titles=[], skills=self.AI_ML_SKILLS)
        assert got["classification_confidence"] <= tx.SKILLS_ONLY_MAX_CONFIDENCE
        assert tx.SKILLS_ONLY_MAX_CONFIDENCE < 0.70

    def test_office_software_is_not_a_discipline(self):
        """The precision side. A teacher listing Google Classroom and a nurse
        listing Epic must read as exactly what they are."""
        assert tx.classify_skills(
            ["Google Classroom", "Excel", "PowerPoint", "Canvas LMS",
             "Lesson Planning", "Microsoft Word"])["families"] == {}
        assert tx.classify_skills(
            ["Patient Care", "EMR", "Epic", "IV Therapy", "Triage",
             "BLS"])["families"] == {}

    def test_one_shared_tool_is_not_a_career(self):
        assert tx.classify_skills(["SQL"])["families"] == {}
        assert tx.classify_skills(["SQL", "Excel", "Git"])["families"] == {}

    def test_a_non_tech_title_history_outranks_the_skills_block(self):
        """The safety property. The veto is decided on TITLES ALONE and
        returns before tools or targets are admitted, so every résumé vetoed
        before this change is vetoed identically after it."""
        got = tx.classify_resume(
            titles=["Registered Nurse", "Staff Nurse"],
            skills=["SQL", "Python", "Tableau", "Power BI", "Pandas"],
            target_roles=["Data Analyst", "Data Scientist"])
        assert got["tech_scope"] == tx.SCOPE_NON_TECH
        assert got["non_tech_domain"] == "clinical"
        assert got["role_families"] == []


class TestDeclaredTargetRoles:
    """`user_preferences.target_roles` is set by onboarding and, before 2.2,
    nothing in the scoring path read it. It is the WEAKEST of the three
    sources and it is fenced accordingly."""

    def test_declared_targets_produce_families_when_nothing_else_does(self):
        got = tx.classify_resume(
            titles=[], skills=[],
            target_roles=["Data Scientist", "Machine Learning Engineer"])
        assert set(got["role_families"]) == {"data", "ai_ml"}
        assert got["evidence_sources"] == ["target_roles"]

    def test_declared_targets_alone_are_never_confident(self):
        got = tx.classify_resume(
            titles=[], skills=[],
            target_roles=["Data Scientist", "Machine Learning Engineer",
                          "AI Engineer", "Data Engineer"])
        assert got["classification_confidence"] <= tx.TARGET_ONLY_MAX_CONFIDENCE
        assert tx.TARGET_ONLY_MAX_CONFIDENCE < 0.70

    def test_an_unsupported_target_is_recorded_and_not_counted(self):
        """Wanting a job must never look like having done it.

        The fence is `FAMILY_ADJACENCY`: a declared target counts only where
        the demonstrated evidence already reaches it or is adjacent to it. A
        chip-design target on a backend résumé IS admitted, at a third of a
        job title's weight, because software and semiconductor are declared
        adjacent and people do make that move. A HELP-DESK résumé declaring
        "Data Scientist" is not, because nothing on it reaches data.
        """
        got = tx.classify_resume(
            titles=["IT Support Specialist", "Help Desk Technician"],
            skills=["Active Directory", "ticketing", "Windows"],
            target_roles=["Data Scientist"])
        assert "data" not in got["role_families"]
        assert "data" in got["target_families_unsupported"]

    def test_an_adjacent_target_is_admitted_but_cannot_outvote_a_career(self):
        """The other side of the same fence. The target is heard; it is worth
        `TARGET_ROLE_WEIGHT` against a title's 1.0, so it cannot displace the
        discipline the résumé actually evidences."""
        got = tx.classify_resume(
            titles=["Backend Engineer", "Software Engineer"],
            skills=["Java", "Spring Boot", "PostgreSQL", "Kafka",
                    "REST API", "microservices"],
            target_roles=["ASIC Design Engineer"])
        assert got["target_families_unsupported"] == []
        assert got["role_families"][0] == "software_engineering"
        assert tx.TARGET_ROLE_WEIGHT < tx.TITLE_EVIDENCE_WEIGHT

    def test_declared_targets_do_not_manufacture_a_score(self):
        """The hard line. A declared target is evidence about DIRECTION; it is
        not something a posting's requirements can be compared against. A
        résumé with targets and no skills and no titles stays `not_scored` —
        the same answer "no parsed résumé" gets."""
        r = _resume(target_roles=["Machine Learning Engineer",
                                  "Data Scientist"])
        v = fit_score.score(r, _job("Machine Learning Engineer",
                                    "Requirements:\n- Python\n- PyTorch\n"
                                    "- 3+ years of experience"))
        assert v["eligibility"] == fit_score.NOT_SCORED

    def test_the_profile_reads_targets_off_the_resume_dict_too(self):
        prof = fit_score.build_resume_profile(
            _resume(skills={"core": ["Python", "PyTorch", "scikit-learn",
                                     "Pandas", "NumPy"]},
                    target_roles=["Machine Learning Engineer"]))
        assert prof["declared_target_roles"] == ["Machine Learning Engineer"]
        assert "ai_ml" in (prof["tech"] or {}).get("role_families", [])


class TestIndustryComesFromEmployment:
    """Measured live: an MS Data Science new grad whose PROJECTS included a
    clinical decision-support system and a counselling chatbot was recorded as
    working in {education, healthcare, nonprofit, legal}. She has never worked
    in any of them, and `_score_domain` then scored her on all four."""

    PROJECTS = [{"name": "Clinical decision support", "tech": "PyTorch",
                 "bullets": ["Triage prototype for a hospital, built for a "
                             "university course with a nonprofit's data."]}]

    def test_a_project_domain_is_not_an_employment_domain(self):
        prof = fit_score.build_resume_profile(_resume(
            skills={"core": ["Python", "PyTorch"]},
            experience=[{"title": "Data Science Intern",
                         "company": "BrainOvision Solutions",
                         "dates": "May 2023 - June 2023",
                         "bullets": ["Built an ETL pipeline in Python."]}],
            projects=self.PROJECTS))
        assert prof["industries"] == set()

    def test_an_employer_that_names_an_industry_still_counts(self):
        prof = fit_score.build_resume_profile(_resume(
            experience=[{"title": "Staff Nurse", "company": "Mercy Hospital",
                         "dates": "Jan 2020 - Present", "bullets": []}]))
        assert "healthcare" in prof["industries"]

    def test_one_incidental_bullet_word_is_not_an_industry(self):
        prof = fit_score.build_resume_profile(_resume(
            experience=[{"title": "Backend Engineer", "company": "Acme Inc",
                         "dates": "Jan 2020 - Present",
                         "bullets": ["Integrated a clinical data feed."]}]))
        assert "healthcare" not in prof["industries"]


class TestDatedExperienceIsCoherent:
    """`has_dated_experience: True` beside `years: 0` was the visible symptom.
    Underneath it, five months of dated internships and NO dated experience at
    all both produced `0`, and the second of those actually scored BETTER,
    because `None` falls through to the kinder seniority-band branch."""

    R = _resume(
        skills={"core": ["Python", "SQL", "Pandas"]},
        experience=[
            {"title": "Data Science Intern", "company": "A",
             "dates": "March 2026 - May 2026", "bullets": ["Built models."]},
            {"title": "Data Science Intern", "company": "B",
             "dates": "May 2023 - June 2023", "bullets": ["Built a pipeline."]},
        ])

    def test_months_are_reported_and_years_stay_floored(self):
        prof = fit_score.build_resume_profile(self.R)
        assert prof["experience_months"] and prof["experience_months"] > 0
        assert prof["years"] == 0          # five months is not a year
        assert prof["has_dated_experience"] is True

    def test_undated_and_sub_year_are_no_longer_the_same_number(self):
        undated = fit_score.build_resume_profile(_resume(
            skills={"core": ["Python"]},
            experience=[{"title": "Data Science Intern", "company": "A",
                         "dates": "", "bullets": []}]))
        assert undated["experience_months"] is None
        assert undated["has_dated_experience"] is False

    def test_sub_year_experience_beats_no_experience_at_all(self):
        """The incoherence, as a comparison. Both résumés report `years: 0`;
        only one of them has actually worked."""
        job = _job("Data Analyst", "Requirements:\n- SQL\n- Python\n"
                                   "- 2+ years of experience")
        worked = fit_score.score(self.R, job)
        never = fit_score.score(
            _resume(skills={"core": ["Python", "SQL", "Pandas"]},
                    experience=[{"title": "Data Science Intern",
                                 "company": "A", "dates": "",
                                 "bullets": ["Built models."]}]),
            job)
        assert (worked["components"]["experience"]["score"]
                >= never["components"]["experience"]["score"])


class TestClearanceAndWorkAuthorizationExtraction:
    """`extract_requirements` is what stamps these flags at ingest. The 2.1
    patterns read only the narrowest phrasings; everything below is a real
    spelling from live inventory that was silent."""

    CLEARANCE = (
        "Must be able to obtain and maintain a security clearance.",
        "Clearance eligible candidates preferred.",
        "Applicants must be a U.S. Person as defined by ITAR.",
        "This position is subject to export control regulations (EAR/ITAR).",
        "Must be able to obtain a Public Trust.",
        "This government contract requires US citizenship.",
        "An active TS/SCI with polygraph is required.",
    )
    WORKAUTH = (
        "Must be authorized to work in the US without sponsorship now or in "
        "the future.",
        "No C2C. No third party candidates.",
        "OPT/CPT candidates are not eligible.",
        "Must be a Green Card holder or US Citizen.",
        "Sponsorship is not available for this position.",
        "Permanent US work authorization required.",
        "We do not offer relocation or sponsorship.",
        "We are unable to provide visa sponsorship.",
    )
    QUIET = (
        "This role does not provide relocation assistance.",
        "Please wear ear protection in the lab.",
        "We offer a generous benefits package and equity.",
        "Build search and ranking systems in Go.",
        # FORM LABELS, not requirements. Measured on 4,000 live bodies: a bare
        # "clearance required" is a field name far more often than a statement,
        # and it produced 97 false flags from one employer's Q&A template
        # alone. Every genuine statement carries a qualifier.
        "Government clearance required? no",
        "Available for work visa sponsorship? Government clearance required? "
        "Job posting end date October 7, 2026",
        "Minimum clearance required to start: none",
        # And the left-boundary case: "indigenous persons" is not "US persons".
        "An equal opportunity employer of women, indigenous persons and "
        "people with disabilities.",
    )

    def _reqs(self, body):
        return fit_score.extract_requirements(
            "Requirements:\n- Python\n- Kubernetes\n" + body, "Engineer")

    def test_clearance_phrasings_are_read(self):
        for body in self.CLEARANCE:
            assert self._reqs(body)["clearance_required"], body

    def test_work_authorization_phrasings_are_read(self):
        for body in self.WORKAUTH:
            assert self._reqs(body)["work_authorization_restricted"], body

    def test_a_quiet_posting_stays_quiet(self):
        """Silence is NEVER a mismatch, and a widened pattern must not invent
        one. "does not provide relocation assistance" is not a work-auth
        statement."""
        for body in self.QUIET:
            r = self._reqs(body)
            assert not r["clearance_required"], body
            assert not r["work_authorization_restricted"], body

    def test_an_unstated_clearance_costs_nothing(self):
        """A résumé cannot CONTRADICT a clearance requirement, so a posting
        that names one against a silent résumé is `unknown`, surfaced and not
        priced."""
        r = _resume(skills={"core": ["Python", "Kubernetes"]},
                    experience=[{"title": "Backend Engineer", "company": "A",
                                 "dates": "Jan 2020 - Present",
                                 "bullets": ["Python services."]}])
        v = fit_score.score(r, _job(
            "Backend Engineer",
            "Requirements:\n- Python\n- Kubernetes\n"
            "Must be able to obtain and maintain a security clearance."))
        clearance = [c for c in v["hard_constraints"]
                     if c.get("type") == "clearance"]
        assert clearance and clearance[0]["status"] == "unknown"
        assert "clearance" not in v["applied_caps"]
        assert v["eligibility"] == fit_score.ELIGIBLE


class TestEmployerSponsorshipIsBesideTheScore:
    """`sponsorship_service` is a TRUST feature with a mandated framing. It
    may be SHOWN with a score; it may never be an input to one."""

    SIGNAL = {"employer_display": "Acme", "band": "strong", "score": 82,
              "approvals_5y": 63, "headline": "Sponsored 63 people",
              "summary": "...", "source": "DOL + USCIS",
              "disclaimer": "not a prediction", "data_vintage": "2026-01-01",
              "internal_field": "must not leak"}

    def _pair(self, body):
        r = _resume(skills={"core": ["Python", "Kubernetes", "Terraform"]},
                    experience=[{"title": "Backend Engineer", "company": "A",
                                 "dates": "Jan 2020 - Present",
                                 "bullets": ["Python services on AWS."]}])
        base = _job("Backend Engineer",
                    "Requirements:\n- Python\n- Kubernetes\n" + body)
        withsig = dict(base, sponsorship_signal=self.SIGNAL)
        return fit_score.score(r, base), fit_score.score(r, withsig)

    def test_it_does_not_move_the_score_or_the_eligibility(self):
        for body in ("", "We do not sponsor visas for this role."):
            plain, signed = self._pair(body)
            assert plain["score"] == signed["score"]
            assert plain["band"] == signed["band"]
            assert plain["eligibility"] == signed["eligibility"]
            assert plain["components"] == signed["components"]

    def test_no_signal_is_none_and_never_a_zero(self):
        plain, _ = self._pair("")
        assert plain["employer_sponsorship"] is None

    def test_the_mandated_framing_survives_and_nothing_else_leaks(self):
        _, signed = self._pair("We do not sponsor visas for this role.")
        s = signed["employer_sponsorship"]
        assert s["source"] and s["disclaimer"] and s["data_vintage"]
        assert s["affects_match_score"] is False
        assert "internal_field" not in s
        # The sponsorship strength is renamed so it cannot be mistaken for the
        # match score sitting beside it in the same payload.
        assert s["signal_score"] == 82 and "score" not in s

    def test_the_postings_own_statement_is_not_rebutted(self):
        _, signed = self._pair("We do not sponsor visas for this role.")
        assert signed["employer_sponsorship"]["posting_states_no_sponsorship"]

    def test_an_overstated_window_is_named_not_republished(self):
        """`sponsorship_service` advertises five fiscal years; the ingested
        USCIS rows carry one. The larger number may not be passed on as if it
        had been measured."""
        sig = dict(self.SIGNAL, data_window_years=5, data_window={
            "fiscal_years": [2026], "years": 1, "service_claim_years": 5,
            "service_claim_mismatch": True, "dol_lca_included": False})
        r = _resume(skills={"core": ["Python", "Kubernetes", "Terraform"]},
                    experience=[{"title": "Backend Engineer", "company": "A",
                                 "dates": "Jan 2020 - Present",
                                 "bullets": ["Python services on AWS."]}])
        v = fit_score.score(r, _job("Backend Engineer",
                                    "Requirements:\n- Python\n- Kubernetes",
                                    sponsorship_signal=sig))
        s = v["employer_sponsorship"]
        assert s["data_window_years_measured"] == 1
        assert s["data_window_claim_unverified"] is True
        assert "1" in s["window_note"]


class TestVersionsAreStampedAndCompatible:
    """A bump used to be a cliff: `_precomputed_requirements` rejected any
    stored profile whose `score_version` was not current, the fallback
    extracted from an empty body, and every posting became evidence-less at
    once."""

    def test_stored_profiles_from_earlier_scorers_are_still_accepted(self):
        for v in ("fit-v2.0", "fit-v2.1", "fit-v2.2"):
            assert v in fit_score._COMPATIBLE_REQUIREMENT_VERSIONS
        assert (fit_score.FIT_SCORE_VERSION
                in fit_score._COMPATIBLE_REQUIREMENT_VERSIONS)

    def test_the_extraction_contract_is_unchanged(self):
        reqs = fit_score.extract_requirements(
            "Requirements:\n- Python\n- Kubernetes\n- 3+ years", "Engineer")
        for key in fit_score._REQUIRED_PROFILE_KEYS:
            assert key in reqs, key

    def test_every_classification_carries_its_taxonomy_version(self):
        assert tx.classify("Backend Engineer").taxonomy_version == (
            tx.TAXONOMY_VERSION)
        assert tx.classify_resume(titles=["Backend Engineer"])[
            "taxonomy_version"] == tx.TAXONOMY_VERSION
        assert tx.role_alignment(
            job_families=["data"], resume_families=["data"]
        )["adjacency_version"] == tx.ADJACENCY_VERSION
