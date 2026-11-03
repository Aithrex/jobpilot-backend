"""The For-you board is ONE list over every employer we crawl.

Live-crawled cards were scored from a bare skill list with no requirement
profile (Big Tech median 40 against 52-62 for inventory cards, same user,
2026-11-02), and the Big Tech crawl re-fetched 60 of 69 employers the
inventory tabs already carried.
"""
from __future__ import annotations

import pytest

from services import fit_score
from services import inventory_feed
from services import mnc_jobs_service as mnc
from services import resume_service as rs
from services import workday_jobs_service as wjs

EMAIL = "seeker@example.com"
DEVOPS_JD = ("We need a DevOps Engineer. Requirements: 3+ years with Kubernetes, Terraform, "
             "AWS and Python. Build CI/CD pipelines and own our infrastructure as code.")


def _live_rows():
    return [
        {"job_id": "n1", "title": "Registered Nurse", "company": "Meta", "location": "Austin, TX",
         "days_ago": 0, "match_score": 91, "fit_band": "strong match", "score_version": "old"},
        {"job_id": "d1", "title": "DevOps Engineer", "company": "Meta", "location": "Austin, TX",
         "days_ago": 1, "match_score": 40, "description": DEVOPS_JD},
    ]


@pytest.fixture()
def resume(monkeypatch):
    skills = ["python", "kubernetes", "terraform", "aws", "docker", "ci/cd"]
    profile = fit_score._profile_from_skills(skills, ["DevOps Engineer"], 3)
    monkeypatch.setattr(fit_score, "load_profiles_for_user",
                        lambda email: [{"resume_id": "r1", "filename": "cv.pdf", "profile": profile}])
    monkeypatch.setattr(wjs, "_load_resume_profile", lambda email: (skills, 3))


class TestOneScorer:
    def test_live_cards_are_scored_like_inventory_cards(self, resume):
        rows = inventory_feed.score_live_rows(_live_rows(), EMAIL)
        devops, nurse = rows[0], rows[1]
        assert devops["job_id"] == "d1", "best match first"
        assert devops["match_score"] > nurse["match_score"]
        assert devops["fit"]["score_version"] == fit_score.FIT_SCORE_VERSION
        # The legacy copies agree with the score beside them, and the
        # requirement profile used to score never reaches the client.
        assert devops["score_version"] == devops["fit"]["score_version"]
        assert nurse["fit_band"] == nurse["fit"]["band"]
        assert all("_requirements" not in r for r in rows)

    def test_the_description_is_read_as_requirements(self, monkeypatch, resume):
        seen = {}

        def spy(rows, *a, **k):
            seen.update({r["job_id"]: bool(r.get("_requirements")) for r in rows})
            for r in rows:
                r["match_score"] = 50
        monkeypatch.setattr(inventory_feed, "_score_rows", spy)
        inventory_feed.score_live_rows(_live_rows(), EMAIL)
        assert seen == {"d1": True, "n1": False}

    def test_no_resume_means_not_scored(self, monkeypatch):
        monkeypatch.setattr(fit_score, "load_profiles_for_user", lambda email: [])
        monkeypatch.setattr(wjs, "_load_resume_profile", lambda email: ([], None))
        rows = inventory_feed.score_live_rows(_live_rows(), EMAIL)
        assert {r["match_score"] for r in rows} == {0}
        assert all("fit" not in r and "score_version" not in r for r in rows)


class TestBigTechCrawlsOnlyWhatTheInventoryLacks:
    @pytest.fixture()
    def crawl(self, monkeypatch):
        calls = []

        def fake_run(payload, partial_cb=None):
            calls.append(payload)
            return {"ok": True, "jobs": _live_rows(), "total": 2}
        monkeypatch.setattr(mnc, "run_mnc_search", fake_run)
        monkeypatch.setattr(inventory_feed, "score_live_rows",
                            lambda rows, email: [r.update(rescored=True) for r in rows] and rows)
        return calls

    def test_held_employers_are_not_crawled_again(self, monkeypatch, crawl):
        slugs = [r["slug"] for r in mnc.MNC_CATALOG]
        monkeypatch.setattr(mnc, "inventory_held_slugs", lambda: set(slugs[2:]))
        out = rs.run_board_source("mnc_jobs", {"user_email": EMAIL, "titles": ["DevOps Engineer"]})
        assert crawl[0]["companies"] == slugs[:2]
        assert all(j.get("rescored") for j in out["jobs"]), "live cards go through the one scorer"

    def test_nothing_left_to_crawl_means_no_crawl(self, monkeypatch, crawl):
        monkeypatch.setattr(mnc, "inventory_held_slugs",
                            lambda: {r["slug"] for r in mnc.MNC_CATALOG})
        out = rs.run_board_source("mnc_jobs", {"user_email": EMAIL, "titles": ["DevOps Engineer"]})
        assert crawl == []
        assert out["ok"] is True and out["jobs"] == [] and out["served_from"] == "inventory"

    def test_unreadable_inventory_crawls_the_whole_catalog(self, monkeypatch, crawl):
        monkeypatch.setattr(mnc, "inventory_held_slugs", lambda: None)
        rs.run_board_source("mnc_jobs", {"user_email": EMAIL, "titles": ["DevOps Engineer"]})
        assert "companies" not in crawl[0]

    def test_an_explicit_company_filter_is_kept(self, monkeypatch, crawl):
        monkeypatch.setattr(mnc, "inventory_held_slugs", lambda: pytest.fail("not consulted"))
        rs.run_board_source("mnc_jobs", {"user_email": EMAIL, "companies": ["amazon"]})
        assert crawl[0]["companies"] == ["amazon"]


class TestInventoryHeldSlugs:
    @pytest.fixture(autouse=True)
    def _clean(self, mock_db):
        mock_db.job_postings.delete_many({})
        yield
        mock_db.job_postings.delete_many({})

    def _post(self, db, n, **doc):
        db.job_postings.insert_many([dict(doc, status="active", source_job_id=f"{doc.get('tenant')}-{i}-"
                                          f"{doc.get('employer_name')}") for i in range(n)])

    def test_tenant_and_exact_name_both_count(self, mock_db):
        mnc.reset_inventory_held_cache()
        self._post(mock_db, 25, source="workday", tenant="nvidia", employer_name="NVIDIA Corp")
        self._post(mock_db, 25, source="oracle", tenant="jpmc", employer_name="JPMorgan Chase & Co.")
        self._post(mock_db, 25, source="career-pages", tenant="metafin", employer_name="Meta Financial")
        self._post(mock_db, 5, source="workday", tenant="comcast", employer_name="Comcast")
        held = mnc.inventory_held_slugs(now=1.0)
        assert {"nvidia", "jpmorganchase"} <= held
        assert "meta" not in held, "a near-miss name is not the employer"
        assert "comcast" not in held, "below the threshold"

    def test_a_tenant_counts_only_under_its_own_source(self, mock_db):
        mnc.reset_inventory_held_cache()
        self._post(mock_db, 30, source="career-pages", tenant="cisco", employer_name="Cisco Meraki fans")
        assert "cisco" not in mnc.inventory_held_slugs(now=1.0)

    def test_cached_for_six_hours(self, mock_db):
        mnc.reset_inventory_held_cache()
        assert "nvidia" not in mnc.inventory_held_slugs(now=1.0)
        self._post(mock_db, 25, source="workday", tenant="nvidia", employer_name="NVIDIA")
        assert "nvidia" not in mnc.inventory_held_slugs(now=2.0)
        assert "nvidia" in mnc.inventory_held_slugs(now=1.0 + 6 * 3600 + 1)
