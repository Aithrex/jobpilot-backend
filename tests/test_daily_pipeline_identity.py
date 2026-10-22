"""Gap 7 (CLAUDE-PROMPT.txt:192) in the legacy daily pipeline.

* rows were deduped on (company, title): two different requisitions with one
  title collapsed and one silently vanished;
* `applicantsCount: 0` became "" (the `or ""` fallback), so the fewest-
  applicants bonus never fired for the least-contested postings;
* >= 500 applicants excluded a row as a "ghost job" - popularity is not
  evidence that a posting is fake or closed;
* "already applied" was keyed on (company, title), so applying to one Stripe
  "Software Engineer" tagged every other Stripe "Software Engineer".
"""
from __future__ import annotations

from services import daily_pipeline_service as dps

_KW = {"experience_level": "any", "past_days": 30}


def _rec(url, **kw):
    base = {"source": "Greenhouse", "company": "Google", "title": "Backend Software Engineer",
            "location": "Mountain View, CA", "posted": "", "salary": "-",
            "applicants": "", "url": url, "description": ""}
    base.update(kw)
    return base


def _shown(apply_now, verify):
    return apply_now + verify


class TestIdentityDedupe:
    def test_two_requisitions_with_one_title_both_survive(self):
        rows = [_rec("https://boards.greenhouse.io/google/jobs/111"),
                _rec("https://boards.greenhouse.io/google/jobs/222")]
        a, v, _ = dps._filter_and_score(rows, "2000-01-01", **_KW)
        urls = sorted(r["url"] for r in _shown(a, v))
        assert urls == ["https://boards.greenhouse.io/google/jobs/111",
                        "https://boards.greenhouse.io/google/jobs/222"]
        # Same company, title AND city: flagged for review, not merged.
        assert sum(1 for r in _shown(a, v) if r.get("possible_duplicate_of")) == 1

    def test_one_requisition_through_two_sources_is_one_row_with_provenance(self):
        rows = [_rec("https://boards.greenhouse.io/google/jobs/111?gh_src=li", source="LinkedIn"),
                _rec("https://boards.greenhouse.io/google/jobs/111", posted="2026-10-20")]
        a, v, _ = dps._filter_and_score(rows, "2000-01-01", **_KW)
        shown = _shown(a, v)
        assert len(shown) == 1
        assert shown[0]["posted"] == "2026-10-20"           # richer copy wins
        assert shown[0]["duplicate_sources"] == ["LinkedIn"]  # loser's provenance kept

    def test_rows_without_any_identity_are_never_merged_on_company_title(self):
        rows = [_rec(""), _rec("")]
        a, v, _ = dps._filter_and_score(rows, "2000-01-01", **_KW)
        assert len(_shown(a, v)) == 2


class TestNumericZeroAndPopularity:
    def test_zero_applicants_is_kept_as_zero(self):
        rec = dps._linkedin_to_record({"companyName": "Google", "title": "SWE",
                                       "applicantsCount": 0, "link": "https://x.com/jobs/1"})
        assert rec["applicants"] == 0

    def test_zero_applicants_earns_the_low_competition_bonus(self):
        _, flags = dps._score(_rec("https://x.com/jobs/1", applicants=0))
        assert any(f.startswith("<30 apps (0)") for f in flags)

    def test_popularity_is_not_a_ghost_job(self):
        assert dps._ghost_job_reason(_rec("https://x.com/jobs/1", applicants=900)) is None
        a, v, excluded = dps._filter_and_score(
            [_rec("https://boards.greenhouse.io/google/jobs/9", applicants=900)],
            "2000-01-01", **_KW)
        assert len(_shown(a, v)) == 1, [e.get("reason") for e in excluded]

    def test_linkedin_record_keeps_its_source_job_id(self):
        rec = dps._linkedin_to_record({"companyName": "Google", "title": "SWE", "id": "4481153600",
                                       "link": "https://www.linkedin.com/jobs/view/4481153600"})
        assert rec["source_job_id"] == "4481153600"


class TestAppliedIndex:
    def _seed(self, mock_db, url, status="applied", job_id="j1"):
        mock_db.saved_jobs.insert_one({"user_email": "u@x.com", "job_id": job_id,
                                       "status": status,
                                       "job_data": {"company": "Google",
                                                    "title": "Backend Software Engineer",
                                                    "apply_link": url}})

    def test_applied_matches_by_apply_url_not_company_title(self, mock_db):
        mock_db.saved_jobs.delete_many({})
        self._seed(mock_db, "https://boards.greenhouse.io/google/jobs/111?utm_source=x")
        idx = dps._load_applied_index("u@x.com")
        rows = [_rec("https://boards.greenhouse.io/google/jobs/111"),
                _rec("https://boards.greenhouse.io/google/jobs/222")]
        a, v, _ = dps._filter_and_score(rows, "2000-01-01", applied_index=idx, **_KW)
        tagged = {r["url"]: r.get("previously_applied_status") for r in _shown(a, v)}
        assert tagged["https://boards.greenhouse.io/google/jobs/111"] == "applied"
        assert tagged["https://boards.greenhouse.io/google/jobs/222"] is None
        mock_db.saved_jobs.delete_many({})
