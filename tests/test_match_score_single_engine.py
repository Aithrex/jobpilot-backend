"""ONE match score. Regression tests for the three-engine divergence.

Before this: `/api/jobs/search` (`job_service.match_jobs`) ran its own blend
(`title*35 + skill*30 + domain*20 + experience*15`, then flat `+8` H1B / `+5`
new-grad / `+2` remote bonuses) with a legacy no-resume fallback that scored a
resume-less account against a hardcoded `PORTFOLIO_SKILLS` list;
`/api/jobs/pipeline/match-scores` and the feed ran `services.fit_score`; and
`workday_jobs_service._attach_feasibility` post-processed fit_score's output
(`-11` per missing year, `min(score, 12)` for a `skip` verdict) AFTER the row had
already been stamped with fit_score's band and `score_version`.

Measured on the fixtures below, the same (resume, posting) pair scored 66 in the
search tab and 98 in the pipeline endpoint, and an account with NO parsed resume
scored **100 "Strong match"** on a posting the shared engine scores 20
("unlikely match") - an 80-point fabricated verdict, rendered in green, on a row
that had never been compared to a resume.

What these tests pin:
  1. `match_jobs` returns EXACTLY `fit_score`'s number - no bonuses folded in.
  2. Every scored row carries the `fit` / `score_version` marker the frontend
     gates its band words on.
  3. No parsed resume => NO score (0, no marker), never a number.
  4. Preference bonuses live in `rank_score` and never move `match_score`.
  5. `missing_skills` are the POSTING's asks, not the resume's own skills.
  6. `_attach_feasibility` never rewrites `match_score`.
  7. `resume_profiler.title_relevance` no longer rates a title the profile
     itself targets at the 0.3 generic-fallback level.
"""

import pytest

from _vg_support import current_db

from services import fit_score
from services.job_service import JobService

USER = "match-engine@example.com"
NO_RESUME_USER = "no-resume@example.com"

_STRUCTURED = {
    "contact": {"name": "R Kumar", "location": "Austin, TX"},
    "summary": "Software engineer building backend services.",
    "skills": {
        "Languages": ["Python", "Java", "SQL", "TypeScript"],
        "Cloud": ["AWS", "Docker", "Kubernetes", "Terraform"],
        "Data": ["PostgreSQL", "Redis", "Kafka"],
    },
    "experience": [
        {"title": "Software Engineer", "company": "Globex",
         "dates": "Jun 2021 - Present", "location": "Austin, TX",
         "bullets": [
             "Built REST APIs in Python and Flask serving 4M requests/day.",
             "Ran services on AWS with Docker and Kubernetes; Terraform IaC.",
             "Modelled data in PostgreSQL and streamed events through Kafka.",
             "Added CI/CD via GitHub Actions and unit testing with pytest.",
         ]},
        {"title": "Software Engineer I", "company": "Initech",
         "dates": "Jul 2019 - May 2021", "location": "Austin, TX",
         "bullets": ["Wrote Java microservices and SQL reporting jobs."]},
    ],
    "education": [{"degree": "B.S. Computer Science", "institution": "UT Austin",
                   "dates": "2015 - 2019"}],
    "projects": [],
    "certifications": [],
    "experience_years": 5,
}

_GOOD_JD = """
Software Engineer
Globodyne Cloud Platform - Austin, TX (hybrid)

Minimum qualifications:
- 3+ years of experience building backend services
- Strong Python or Java
- Experience with REST APIs and microservices
- Working knowledge of PostgreSQL and SQL
- Comfortable with Docker, Kubernetes and CI/CD
- Bachelor's degree in Computer Science or equivalent

Preferred qualifications:
- Kafka or another streaming platform
- Terraform
- Experience with AWS
"""

# The exact shape that made the legacy fallback print 100: an H1B sponsor, a
# remote row, and new-grad language, on top of ten PORTFOLIO_SKILLS hits.
_MAX_BONUS_JD = _GOOD_JD + "\nThis is a fully remote New Grad opportunity.\n"


def _job(job_id="j1", title="Software Engineer", description=_GOOD_JD,
         **extra):
    row = {"job_id": job_id, "title": title, "company": "Globodyne",
           "description": description, "location": "Austin, TX",
           "is_remote": False, "h1b_sponsor": False, "source": "jsearch"}
    row.update(extra)
    return row


@pytest.fixture()
def svc(mock_db):
    d = current_db()
    for c in ("users", "resume_assets", "saved_jobs"):
        d[c].delete_many({})
    d.users.insert_one({"email": USER, "active_resume_id": "asset-match-1"})
    d.resume_assets.insert_one({
        "resume_id": "asset-match-1", "owner_type": "user", "user_email": USER,
        "structured": _STRUCTURED, "parse_status": "parsed",
        "parsed_at": "2026-01-01T00:00:00+00:00",
    })
    d.users.insert_one({"email": NO_RESUME_USER})
    service = JobService()
    yield service
    for c in ("users", "resume_assets", "saved_jobs"):
        d[c].delete_many({})


# ---------------------------------------------------------------------------
# 1 + 2 — search now speaks fit_score, and says so
# ---------------------------------------------------------------------------
class TestSearchUsesFitScore:
    def test_match_score_is_exactly_the_shared_engine(self, svc):
        rows = svc.match_jobs([_job()], user_email=USER)
        expected = fit_score.score_one(
            jd_text=_GOOD_JD, title="Software Engineer",
            profile=fit_score.build_resume_profile(_STRUCTURED),
            location="Austin, TX")
        assert rows[0]["match_score"] == expected["match_score"]
        # Mutation guard: a re-introduced blend cannot coincide with this.
        assert rows[0]["match_score"] > 65, (
            "a well-matched mid-level SWE row must clear the auto-apply gate; "
            "the removed blend scored this same pair 66 with the title bug")

    def test_pipeline_endpoint_and_search_agree_on_the_same_pair(self, svc):
        """The whole point: two surfaces, one number."""
        search = svc.match_jobs([_job()], user_email=USER)[0]["match_score"]
        scores, meta = fit_score.score_batch_for_user(USER, [_job()])
        assert meta["has_resume"] is True
        assert scores["j1"]["match_score"] == search

    def test_scored_rows_carry_the_fit_marker(self, svc):
        row = svc.match_jobs([_job()], user_email=USER)[0]
        assert row["score_version"] == fit_score.FIT_SCORE_VERSION
        assert row["fit"]["score_version"] == fit_score.FIT_SCORE_VERSION
        assert row["fit"]["band"] == fit_score.band_for(row["match_score"])
        assert row["fit"]["components"]
        assert row["fit"]["explanation"]

    def test_band_and_score_never_disagree(self, svc):
        rows = svc.match_jobs(
            [_job("a"), _job("b", title="Registered Nurse",
                             description="Current RN license and BLS required. "
                                         "Bedside patient care, med administration.")],
            user_email=USER)
        for row in rows:
            assert row["fit"]["band"] == fit_score.band_for(row["match_score"])


# ---------------------------------------------------------------------------
# 3 — the no-resume case
# ---------------------------------------------------------------------------
class TestNoParsedResume:
    def test_no_resume_yields_no_score_not_a_hundred(self, svc):
        rows = svc.match_jobs(
            [_job("bonus", description=_MAX_BONUS_JD, title="Software Engineer, New Grad",
                  is_remote=True, h1b_sponsor=True)],
            user_email=NO_RESUME_USER)
        row = rows[0]
        # The removed fallback returned 100 here (70 skill cap + 15 H1B + 10
        # new-grad + 5 remote), which the UI rendered as a green "Strong match".
        assert row["match_score"] == 0
        assert row["matched_skills"] == []
        assert row["missing_skills"] == []
        # No marker => the frontend cannot attach fit_score's band words.
        assert "fit" not in row
        assert "score_version" not in row

    def test_unknown_account_is_also_unscored(self, svc):
        row = svc.match_jobs([_job()], user_email="nobody@example.com")[0]
        assert row["match_score"] == 0
        assert "fit" not in row

    def test_empty_user_email_is_unscored(self, svc):
        row = svc.match_jobs([_job()], user_email="")[0]
        assert row["match_score"] == 0
        assert "fit" not in row

    def test_explicit_none_profile_means_unscored(self, svc):
        """`None` is meaningful, not "resolve it for me" - the streaming search
        resolves once and passes the result (possibly None) to every call."""
        row = svc.match_jobs([_job()], user_email=USER, profile=None)[0]
        assert row["match_score"] == 0
        assert "fit" not in row

    def test_unscored_rows_still_order_by_justifiable_signals(self, svc):
        rows = svc.match_jobs(
            [_job("plain"),
             _job("sponsor", h1b_sponsor=True),
             _job("remote", is_remote=True)],
            user_email=NO_RESUME_USER)
        assert [r["job_id"] for r in rows] == ["sponsor", "remote", "plain"]
        assert all(r["match_score"] == 0 for r in rows)

    def test_load_profile_for_user_is_the_single_definition(self):
        assert fit_score.load_profile_for_user("") is None
        assert fit_score.load_profile_for_user("nobody@example.com") is None
        assert fit_score.unscored_meta()["has_resume"] is False


# ---------------------------------------------------------------------------
# 4 — preference bonuses cannot move the fit score
# ---------------------------------------------------------------------------
class TestBonusesStayOutOfTheScore:
    def test_h1b_remote_newgrad_do_not_change_match_score(self, svc):
        plain = svc.match_jobs([_job("p")], user_email=USER)[0]
        boosted = svc.match_jobs(
            [_job("b", description=_MAX_BONUS_JD, is_remote=True,
                  h1b_sponsor=True)],
            user_email=USER)[0]
        bare = svc.match_jobs(
            [_job("c", description=_MAX_BONUS_JD)], user_email=USER)[0]
        # Same JD text => same fit. Only rank_score moves.
        assert boosted["match_score"] == bare["match_score"]
        assert boosted["rank_score"] >= bare["rank_score"]
        assert plain["rank_score"] == plain["match_score"]

    def test_a_bonus_lifts_rank_score_when_there_is_headroom(self, svc):
        """The same property, measured where it is observable.

        Under fit-v2.1 this fixture's résumé fully covers `_MAX_BONUS_JD`, so
        the fit itself reaches 100 and `rank_score` — which is capped at 100 —
        has nowhere to move. That is a ceiling, not a bug, and asserting a
        strict increase against it would be asserting that the fit is
        imperfect. A partially-covered JD leaves room, and there the bonus
        must show.
        """
        partial = ("Software Engineer. Requirements: Python, SQL, Kubernetes, "
                   "Terraform, Kafka, Scala, Elixir and Rust.\n")
        bare = svc.match_jobs([_job("d", description=partial)],
                              user_email=USER)[0]
        boosted = svc.match_jobs(
            [_job("e", description=partial + "This is a fully remote New Grad "
                  "opportunity.\n", is_remote=True, h1b_sponsor=True)],
            user_email=USER)[0]
        assert bare["rank_score"] < 100
        assert boosted["rank_score"] > bare["rank_score"]

    def test_rank_score_never_exceeds_100(self, svc):
        row = svc.match_jobs(
            [_job(description=_MAX_BONUS_JD, is_remote=True, h1b_sponsor=True)],
            user_email=USER)[0]
        assert row["rank_score"] <= 100

    def test_response_trim_orders_by_rank_score(self, svc):
        rows = [
            {"job_id": "hi-fit", "title": "x", "company": "c", "source": "s",
             "match_score": 70, "rank_score": 70, "description": ""},
            {"job_id": "sponsor", "title": "y", "company": "c", "source": "s",
             "match_score": 68, "rank_score": 76, "description": ""},
        ]
        out = svc._prepare_jobs_for_response(rows)
        assert [r["job_id"] for r in out] == ["sponsor", "hi-fit"]
        # ...and the DISPLAYED score is untouched by the ordering bonus.
        assert {r["job_id"]: r["match_score"] for r in out} == {
            "hi-fit": 70, "sponsor": 68}


# ---------------------------------------------------------------------------
# 5 — the two skill lists mean what the UI says they mean
# ---------------------------------------------------------------------------
class TestSkillListSemantics:
    def test_missing_skills_are_the_postings_asks(self, svc):
        """The old list was inverted: resume skills the JD failed to mention.
        The UI labels it "Asked for, not evidenced"."""
        jd = ("Requirements:\n- Expert Rust and Scala\n"
              "- Deep experience with Elasticsearch\n- 3+ years backend\n")
        row = svc.match_jobs([_job(description=jd)], user_email=USER)[0]
        missing = {s.lower() for s in row["missing_skills"]}
        assert {"rust", "scala", "elasticsearch"} <= missing
        # Kafka/Terraform are on the RESUME and absent from this JD - they must
        # NOT show up as a gap.
        assert "kafka" not in missing
        assert "terraform" not in missing

    def test_matched_skills_are_evidenced_requirements(self, svc):
        row = svc.match_jobs([_job()], user_email=USER)[0]
        matched = {s.lower() for s in row["matched_skills"]}
        assert "python" in matched
        assert "postgresql" in matched


# ---------------------------------------------------------------------------
# 6 — engine C no longer writes a second number under `match_score`
# ---------------------------------------------------------------------------
class TestFeasibilityDoesNotRewriteTheScore:
    def test_skip_verdict_leaves_match_score_alone(self):
        from services.workday_jobs_service import _attach_feasibility
        jobs = [{"title": "Registered Nurse", "description":
                 "Bedside patient care, medication administration, RN license.",
                 "match_score": 47, "fit_band": "weak match",
                 "score_version": fit_score.FIT_SCORE_VERSION}]
        _attach_feasibility(jobs, ["python", "aws", "docker"], 5)
        assert jobs[0]["tailor_feasibility"] == "skip"
        assert jobs[0]["match_score"] == 47, (
            "feasibility used to clamp a skip row to 12 while leaving the band "
            "and score_version stamped beside it untouched")

    def test_years_gap_stretch_leaves_match_score_alone(self):
        from services.workday_jobs_service import _attach_feasibility
        jobs = [{"title": "Senior Software Engineer",
                 "description": "Python and AWS microservices. "
                                "Requires 6+ years of experience.",
                 "match_score": 61,
                 "score_version": fit_score.FIT_SCORE_VERSION}]
        _attach_feasibility(jobs, ["python", "aws", "docker"], 3)
        assert jobs[0]["tailor_feasibility"] == "stretch"
        assert jobs[0]["match_score"] == 61  # was 61 - (gap-1)*11

    def test_non_tech_resume_gap_leaves_match_score_alone(self):
        from services.workday_jobs_service import _attach_feasibility
        jobs = [{"title": "Charge Nurse",
                 "description": "Requires 6+ years of experience in acute care.",
                 "match_score": 58}]
        _attach_feasibility(jobs, ["patient care", "charting"], 3)
        assert jobs[0]["tailor_feasibility"] == "stretch"
        assert jobs[0]["match_score"] == 58

    def test_feasibility_verdict_is_still_produced(self):
        """The verdict is the clearly-labelled field that replaces the clamp."""
        from services.workday_jobs_service import _attach_feasibility
        jobs = [{"title": "Software Engineer",
                 "description": "Python, AWS, Docker microservices.",
                 "match_score": 72}]
        _attach_feasibility(jobs, ["python", "aws", "docker"], 5)
        assert jobs[0]["tailor_feasibility"] in ("strong", "possible")
        assert jobs[0]["feasibility_reason"]


# ---------------------------------------------------------------------------
# 7 — finding (e): the title_relevance deflation
# ---------------------------------------------------------------------------
class TestTitleRelevance:
    def test_a_targeted_title_is_not_a_generic_fallback(self):
        from services.resume_profiler import build_profile, title_relevance
        profile = build_profile(_STRUCTURED)
        # This profile's own `target_titles` contain "Software Engineer I"
        # (backend) - i.e. the platform searches boards for this exact title -
        # yet the bare posting title used to score 0.3 (the generic net).
        assert title_relevance(profile, "Software Engineer") == 0.9
        assert title_relevance(profile, "Software Engineer II") == 0.9
        assert title_relevance(profile, "Senior Software Engineer") == 0.9

    def test_a_domain_title_still_beats_a_targeted_one(self):
        from services.resume_profiler import build_profile, title_relevance
        profile = build_profile(_STRUCTURED)
        assert title_relevance(profile, "Backend Engineer") == 1.0

    def test_an_unrelated_title_is_still_zero(self):
        from services.resume_profiler import build_profile, title_relevance
        profile = build_profile(_STRUCTURED)
        assert title_relevance(profile, "Registered Nurse") == 0.0
        assert title_relevance(profile, "Staff Accountant") == 0.0

    def test_empty_title_is_zero(self):
        from services.resume_profiler import build_profile, title_relevance
        assert title_relevance(build_profile(_STRUCTURED), "") == 0.0
