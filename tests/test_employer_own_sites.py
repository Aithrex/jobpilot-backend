"""Workday employers' own career sites (2026-11-17).

Cloudflare now challenges Workday from the box, so the biggest employers' jobs
stopped arriving. Six of the top 40 run their own Phenom or Eightfold site,
linked from their own homepage, that serves the same requisitions: TJX, RTX,
CVS Health, DaVita, Thermo Fisher (Phenom) and Northrop Grumman (Eightfold).

Three things make them worth adding, and each is pinned here:
* the posting id is the employer's WORKDAY requisition number and the tenant is
  the Workday tenant, so the feed shows ONE card when both sources answer;
* Phenom's listing is a ~300-character teaser, so the job page's schema.org
  JobPosting is fetched for the full body (live: TJX 5,457 chars, CVS 9,946);
* placeholder rows ("SA TEST OCT 15 1" on jobs.thermofisher.com) are dropped.
"""
from datetime import datetime, timedelta, timezone

import pytest

from services import inventory_feed as feed
from services import job_ingest_worker as w
from services.ats_providers import enterprise as e
from services.ats_providers.base import ChallengeError, Endpoint, FetchContext, RawJob

TJX_ROW = {"title": "Merchandise Associate", "reqId": "R2325246", "jobId": "R2325246",
           "jobSeqNo": "TJCOGLOBALR2325246EXTERNALENGLOBAL", "locale": "en_GLOBAL",
           "applyUrl": "https://wd1.myworkdaysite.com/recruiting/tjx/TJX_EXTERNAL/job/Dresher-PA-19025/Merchandise-Associate_R2325246/apply",
           "cityState": "Dresher, PA", "country": "United States of America",
           "postedDate": "2026-11-16T00:00:00.000+0000", "descriptionTeaser": "HomeGoods. At TJX..."}
JOB_PAGE = ('<html><script type="application/ld+json">{"@context":"https://schema.org",'
            '"@type":"JobPosting","title":"Merchandise Associate","datePosted":"2026-11-17",'
            '"description":"<p>Full body. ' + "x" * 3000 + '</p>","hiringOrganization":{"name":"TJX"}}'
            '</script></html>')


def _ctx(**kw):
    return FetchContext(get_json=kw.get("get_json", lambda *a, **k: (404, None)),
                        get_text=kw.get("get_text", lambda *a, **k: (404, "")),
                        post_json=kw.get("post_json"))


class TestPhenomRequisitionIds:
    def test_requisition_ids_use_the_ats_number(self):
        raw = e.PhenomProvider._to_raw("jobs.tjx.com", TJX_ROW, req_ids=True)
        assert raw.source_job_id == "R2325246"

    def test_existing_boards_keep_jobseqno(self):
        raw = e.PhenomProvider._to_raw("careers.usbank.com", TJX_ROW)
        assert raw.source_job_id == "TJCOGLOBALR2325246EXTERNALENGLOBAL"

    def test_the_detail_url_is_the_phenom_job_page_not_the_workday_apply_link(self):
        raw = e.PhenomProvider._to_raw("jobs.tjx.com", TJX_ROW, req_ids=True)
        assert raw.detail_url == "https://jobs.tjx.com/global/en/job/R2325246/Merchandise-Associate"
        assert "myworkday" in raw.apply_url, "people still apply on the employer's ATS"

    def test_listing_passes_the_flag_through(self):
        def post_json(url, json_body=None, **k):
            if json_body["from"]:
                return 200, {"refineSearch": {"totalHits": 1, "data": {"jobs": []}}}
            return 200, {"refineSearch": {"totalHits": 1, "data": {"jobs": [TJX_ROW]}}}
        ep = Endpoint(vendor="phenom", tenant="tjx", host="jobs.tjx.com", extra={"requisition_ids": True})
        rows = list(e.PhenomProvider().list_jobs(ep, _ctx(post_json=post_json)))
        assert [r.source_job_id for r in rows] == ["R2325246"]


class TestPhenomDetail:
    EP = Endpoint(vendor="phenom", tenant="tjx", host="jobs.tjx.com", extra={"requisition_ids": True})

    def _job(self):
        return e.PhenomProvider._to_raw("jobs.tjx.com", TJX_ROW, req_ids=True)

    def test_the_full_body_comes_from_the_job_posting_ld(self):
        out = e.PhenomProvider().fetch_detail(self.EP, self._job(), _ctx(get_text=lambda *a, **k: (200, JOB_PAGE)))
        assert out.description.startswith("Full body.") and len(out.description) > 3000
        assert out.posted_at.startswith("2026-11-17")

    def test_a_page_without_job_data_is_no_body(self):
        assert e.PhenomProvider().fetch_detail(
            self.EP, self._job(), _ctx(get_text=lambda *a, **k: (200, "<html>search page</html>"))) is None

    def test_a_challenge_is_raised_not_swallowed(self):
        with pytest.raises(ChallengeError):
            e.PhenomProvider().fetch_detail(self.EP, self._job(), _ctx(get_text=lambda *a, **k: (429, "")))

    def test_no_detail_url_means_no_request(self):
        job = RawJob(source_job_id="R1", title="x", apply_url="https://a")
        calls = []
        e.PhenomProvider().fetch_detail(self.EP, job, _ctx(get_text=lambda *a, **k: calls.append(a) or (200, "")))
        assert calls == []


class TestTestPostings:
    @pytest.mark.parametrize("title", ["SA TEST OCT 15 1", "sourcing agent test oct 15",
                                       "Test Job", "DO NOT APPLY - internal"])
    def test_placeholders_are_dropped(self, title):
        assert e.looks_like_test_posting(title)
        assert e.PhenomProvider._to_raw("jobs.thermofisher.com", dict(TJX_ROW, title=title)) is None

    @pytest.mark.parametrize("title", ["Test Engineer", "Software Test Technician II",
                                       "Clinical Testing Specialist", "QA Test Lead"])
    def test_real_test_jobs_stay(self, title):
        assert not e.looks_like_test_posting(title)


NG_ROW = {"id": "1340072266314", "atsJobId": "R10236505", "displayJobId": "R10236505",
          "name": "Systems Engineer", "locations": ["United States-California-Sunnyvale"],
          "postedTs": 1794873600}


class TestEightfoldRequisitionIds:
    def test_posting_id_is_the_ats_number_and_urls_keep_the_position_id(self):
        raw = e.EightfoldProvider._to_raw("jobs.northropgrumman.com", "ngc.com", NG_ROW, req_ids=True)
        assert raw.source_job_id == "R10236505"
        assert raw.detail_url.endswith("/api/apply/v2/jobs/1340072266314")
        assert "/careers/job/1340072266314" in raw.apply_url

    def test_without_the_flag_nothing_changes(self):
        raw = e.EightfoldProvider._to_raw("apply.careers.microsoft.com", "microsoft.com", NG_ROW)
        assert raw.source_job_id == "1340072266314"

    def test_pcsx_detail_fallback_asks_by_position_id(self):
        asked = []

        def get_json(url, params=None, **k):
            asked.append((url, dict(params or {})))
            if "position_details" in url:
                return 200, {"data": {"jobDescription": "<p>Full NG body</p>"}}
            return 404, None
        ep = Endpoint(vendor="eightfold", tenant="ngc", host="jobs.northropgrumman.com",
                      extra={"domain": "ngc.com", "variant": "pcsx", "requisition_ids": True})
        raw = e.EightfoldProvider._to_raw("jobs.northropgrumman.com", "ngc.com", NG_ROW, req_ids=True)
        out = e.EightfoldProvider().fetch_detail(ep, raw, _ctx(get_json=get_json))
        assert out.description == "Full NG body"
        assert asked[-1][1]["position_id"] == "1340072266314"


SEEDS = {"jobs.tjx.com": "tjx", "careers.rtx.com": "globalhr", "jobs.cvshealth.com": "cvshealth",
         "careers.davita.com": "davita", "jobs.thermofisher.com": "thermofisher",
         "jobs.northropgrumman.com": "ngc"}


class TestSeedsAndAllowlist:
    def test_each_site_is_seeded_with_its_workday_tenant(self):
        rows = {r["host"]: r for r in w._SEED_ENDPOINTS if r.get("host") in SEEDS}
        assert {h: r["tenant"] for h, r in rows.items()} == SEEDS
        assert all((r.get("extra") or {}).get("requisition_ids") for r in rows.values())

    def test_each_host_is_on_its_provider_allowlist(self):
        for host in SEEDS:
            prov = e.EightfoldProvider if "northrop" in host else e.PhenomProvider
            assert host in prov.api_hosts, host


def _doc(ats, tenant, member_key, req, url, posted):
    return {"job_id": f"{ats}-{req}", "title_raw": "Merchandise Associate", "employer_name": "TJX Companies",
            "tenant": tenant, "ats": ats, "source_job_id": f"{member_key}:{req}",
            "locations": [{"raw": "Dresher, PA", "country": "United States", "country_code": "US"}],
            "canonical_url": url, "posted_at": posted, "status": "active"}


class TestOneCardPerJob:
    def test_the_workday_copy_and_the_phenom_copy_merge(self):
        now = datetime.now(timezone.utc)
        wd = _doc("workday", "tjx", "tjx", "R2325246",
                  "https://tjx.wd1.myworkdayjobs.com/TJX_EXTERNAL/job/Dresher-PA/Merchandise-Associate_R2325246",
                  now - timedelta(days=3))
        ph = _doc("phenom", "tjx", "phenom-tjx", "R2325246",
                  "https://wd1.myworkdaysite.com/recruiting/tjx/TJX_EXTERNAL/job/Dresher-PA-19025/Merchandise-Associate_R2325246/apply",
                  now - timedelta(days=1))
        rows = [feed._row_from_posting(d, tz_offset=0) for d in (wd, ph)]
        out = feed._dedupe([r for r in rows if r])
        assert len(out) == 1
        assert out[0].get("duplicate_count") == 1

    def test_a_different_requisition_is_not_merged(self):
        now = datetime.now(timezone.utc)
        a = _doc("workday", "tjx", "tjx", "R1", "https://tjx.wd1.myworkdayjobs.com/x/job/a_R1", now)
        b = _doc("phenom", "tjx", "phenom-tjx", "R2", "https://jobs.tjx.com/global/en/job/R2/b", now)
        out = feed._dedupe([feed._row_from_posting(d, tz_offset=0) for d in (a, b)])
        assert len(out) == 2


class TestPhenomBigBoards:
    """2026-11-17: TJX (9,777 rows, relevance order) took 489 requests at 20 a
    page; its shard hit the 300 s budget and stored nothing."""

    def _pages(self, sizes, total):
        sent = []
        rows = iter(range(10 ** 6))

        def post_json(url, json_body=None, **k):
            sent.append(json_body["from"])
            n = sizes.pop(0) if sizes else 0
            jobs = [dict(TJX_ROW, reqId=f"R{next(rows)}", jobSeqNo=f"S{len(sent)}-{i}") for i in range(n)]
            return 200, {"refineSearch": {"totalHits": total, "data": {"jobs": jobs}}}
        return post_json, sent

    def test_pages_are_two_hundred_rows(self):
        assert e.PhenomProvider.policy.page_size == 200

    def test_a_tenant_that_caps_the_page_lower_is_read_to_its_total(self):
        post_json, sent = self._pages([50, 50, 50], total=150)
        ep = Endpoint(vendor="phenom", tenant="tjx", host="jobs.tjx.com", extra={"requisition_ids": True})
        rows = list(e.PhenomProvider().list_jobs(ep, _ctx(post_json=post_json)))
        assert len(rows) == 150
        assert sent == [0, 50, 100], "advances by what each page returned"

    def test_a_short_page_with_no_total_ends_the_board(self):
        post_json, sent = self._pages([30], total=None)
        ep = Endpoint(vendor="phenom", tenant="tjx", host="jobs.tjx.com")
        assert len(list(e.PhenomProvider().list_jobs(ep, _ctx(post_json=post_json)))) == 30
        assert sent == [0]
