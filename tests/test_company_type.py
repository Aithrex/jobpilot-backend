"""`company_type` groups board cards by the kind of employer, with evidence,
and the Saved/Applied pages get a `next_step` per saved job."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from services import company_type as ct
from services import mnc_jobs_service as mnc


def _clf(by_name=None, names=None, tenants=None):
    return ct.Classifier(catalog=(set(tenants or ()), set(names or ())),
                         open_roles=({}, dict(by_name or {})))


class TestClassify:
    def test_curated_catalog_name_is_big_tech(self):
        out = _clf(names={"nvidia"}).classify({"company": "NVIDIA Corporation", "ats": "workday"})
        assert out["type"] == ct.BIG_TECH_MNC
        assert "curated" in out["basis"]

    def test_staffing_kind_wins_over_size(self):
        row = {"company": "Acme Staffing", "ats": "workday",
               "employer_profile": {"kind": "staffing"}}
        out = _clf(by_name={"acme staffing": 5000}).classify(row)
        assert out["type"] == ct.STAFFING

    def test_hiring_volume_sets_the_band(self):
        clf = _clf(by_name={"bigco": 1200, "midco": 200})
        assert clf.classify({"company": "BigCo Inc.", "ats": "greenhouse"})["type"] == ct.BIG_TECH_MNC
        assert clf.classify({"company": "MidCo", "ats": "greenhouse"})["type"] == ct.ENTERPRISE

    def test_ats_is_the_weakest_rung_and_says_so(self):
        clf = _clf()
        wd = clf.classify({"company": "Regional Hospital", "ats": "workday"})
        assert wd["type"] == ct.ENTERPRISE and "Workday" in wd["basis"]
        gh = clf.classify({"company": "Tiny AI", "ats": "ashby"})
        assert gh["type"] == ct.STARTUP_MIDSIZE and "Ashby" in gh["basis"]

    def test_nothing_known_is_unknown_not_a_guess(self):
        assert _clf().classify({"company": "Somewhere LLC", "ats": "custom"})["type"] == ct.UNKNOWN

    def test_a_short_catalog_tenant_on_a_startup_board_is_not_the_mnc(self):
        clf = _clf(tenants={"cisco"})
        out = clf.classify({"company": "Cisco Meraki fans", "tenant": "cisco", "ats": "greenhouse"})
        assert out["type"] != ct.BIG_TECH_MNC

    def test_annotate_stamps_every_card_and_counts_add_up(self):
        rows = [{"company": "NVIDIA", "ats": "workday"}, {"company": "X", "ats": "lever"},
                {"company": "Y", "ats": ""}]
        ct.annotate(rows, classifier=_clf(names={"nvidia"}))
        assert all(r["company_type"]["version"] == ct.COMPANY_TYPE_VERSION for r in rows)
        counts = ct.group_counts(rows)
        assert sum(counts.values()) == len(rows)
        assert set(counts) == set(ct.TYPES)


class TestInventoryCounts:
    @pytest.fixture(autouse=True)
    def _clean(self, mock_db):
        mock_db.job_postings.delete_many({})
        mnc.reset_inventory_held_cache()
        yield
        mock_db.job_postings.delete_many({})
        mnc.reset_inventory_held_cache()

    def test_counts_come_from_the_shared_grouped_read(self, mock_db):
        mock_db.job_postings.insert_many([
            {"status": "active", "source": "workday", "tenant": "acme", "employer_name": "Acme Corp",
             "source_job_id": f"a{i}"} for i in range(160)])
        clf = ct.Classifier(catalog=(set(), set()))
        assert clf.classify({"company": "Acme", "ats": "lever"})["type"] == ct.ENTERPRISE
        # held-slugs and company_type share ONE read
        assert mnc._groups_cache["groups"] is not None


class TestSavedNextStep:
    @pytest.fixture(autouse=True)
    def _clean(self, mock_db):
        for c in ("job_postings", "tailoring_records", "saved_jobs"):
            mock_db[c].delete_many({})
        yield
        for c in ("job_postings", "tailoring_records", "saved_jobs"):
            mock_db[c].delete_many({})

    def test_open_closed_unknown_applied_and_tailored(self, mock_db):
        from services.job_service import attach_next_step
        now = datetime.now(timezone.utc)
        mock_db.job_postings.insert_many([
            {"job_id": "open1", "status": "active", "posted_at": now - timedelta(days=3)},
            {"job_id": "gone1", "status": "closed", "closed_reason": "detail-gone"},
        ])
        mock_db.tailoring_records.insert_many([
            {"user_email": "u@x.com", "source_job_id": "open1", "current_version_id": "v1",
             "application": {"status": "interested"}},
            {"user_email": "u@x.com", "source_job_id": "gone1",
             "application": {"status": "applied", "applied_at": now - timedelta(days=9)}},
            {"user_email": "other@x.com", "source_job_id": "open1", "current_version_id": "v9"},
        ])
        rows = [{"job_id": "open1"}, {"job_id": "gone1"}, {"job_id": "jsearch-7"}]
        attach_next_step(rows, "u@x.com")
        a, b, c = (r["next_step"] for r in rows)
        assert a["posting_state"] == "open" and a["tailored"] is True and a["posted_at"]
        assert b["posting_state"] == "closed" and b["tailored"] is False
        assert b["applied_at"] and b["tracker_status"] == "applied"
        assert c["posting_state"] == "unknown", "no posting held is unknown, never closed"

    def test_another_users_tailoring_is_not_mine(self, mock_db):
        from services.job_service import attach_next_step
        mock_db.tailoring_records.insert_one(
            {"user_email": "other@x.com", "source_job_id": "j1", "current_version_id": "v"})
        rows = [{"job_id": "j1"}]
        attach_next_step(rows, "me@x.com")
        assert rows[0]["next_step"]["tailored"] is False


class TestLevelHint:
    def test_duty_wording_reads_senior_with_its_sentence(self):
        from services import jd_profile
        hint = jd_profile.read_level_hint(
            "About us. You will mentor junior engineers and review designs. Python, AWS.")
        assert hint["level"] == "senior"
        assert "mentor junior engineers" in hint["basis"]

    def test_generic_wording_reads_nothing(self):
        from services import jd_profile
        for text in ("Work independently with minimal supervision.",
                     "Willingness to mentor junior staff.",
                     "Paid training will be provided. No experience required.",
                     "Serve as the subject matter expert for the platform."):
            assert jd_profile.read_level_hint(text) is None, text

    def test_stated_years_suppress_the_hint(self):
        from services import fit_score
        r = fit_score.extract_requirements(
            "Requirements: 2+ years of Python. You will mentor junior engineers.", "Software Engineer")
        assert r["jd"]["level_hint"] is None
        r = fit_score.extract_requirements(
            "You will mentor junior engineers and lead the design of our APIs.", "Software Engineer")
        assert r["jd"]["level_hint"]["level"] == "senior"

    def test_card_estimate_only_when_no_level_is_stated(self):
        from services.inventory_feed import level_estimate_of
        req = {"jd": {"level_hint": {"level": "senior", "basis": "You will mentor junior engineers."}}}
        assert level_estimate_of("unknown", req)["level"] == "senior"
        assert level_estimate_of("", req)["level"] == "senior"
        assert level_estimate_of("mid", req) is None
        assert level_estimate_of("unknown", {"jd": {}}) is None
        assert level_estimate_of("unknown", None) is None
