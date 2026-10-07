"""Google and Goldman Sachs: no ATS board, read from their own careers stack.
Payload shapes copied from the live responses of 2026-10-07."""
import json

import pytest

from services import job_inventory_service as inv
from services.ats_providers import direct_boards as db
from services.ats_providers.base import Endpoint, FetchContext, RawJob

NEW, OLD = 1791401950, 1759000000        # 2026-10-07 and 2025-09-27


def _g_row(rid, title, *, created=NEW, sorted_on=NEW, places=(("Sunnyvale, CA, USA",),)):
    row = [None] * 21
    row[0], row[1] = rid, title
    row[3] = [None, "<ul><li>Write product code.</li></ul>"]
    row[4] = [None, "<h3>Minimum qualifications:</h3><ul><li>2 years of Python or C++.</li></ul>"]
    row[7] = "Google"
    row[9] = [[p[0], [], "", "", "", "US"] for p in places]
    row[10] = [None, "<p>Google's software engineers develop the next generation.</p>"]
    row[12], row[13], row[14] = [created, 0], [sorted_on, 0], [sorted_on, 0]
    return row


def _g_page(rows):
    data = json.dumps([rows, None, 1812, 20])
    return ("<html><script>AF_initDataCallback({key: 'ds:0', hash: '1', data:[], sideChannel: {}});"
            "</script><script>AF_initDataCallback({key: 'ds:1', hash: '2', data:%s, "
            "sideChannel: {}});</script></html>" % data)


class TestGoogle:
    def test_a_row_becomes_a_posting_with_its_whole_description(self):
        job = db.google_row_to_raw(_g_row(
            "114647189342298822", "Software Engineer III",
            places=(("Seattle, WA, USA",), ("Sunnyvale, CA, USA",))))
        assert job.source_job_id == "114647189342298822"
        assert job.apply_url.endswith("/jobs/results/114647189342298822")
        assert job.location == "Seattle, WA, USA; Sunnyvale, CA, USA"
        assert inv.normalize_location(job.location)["country_code"] == "US"
        assert job.posted_at.startswith("2026-10-07")
        assert "Qualifications\nMinimum qualifications:" in job.description
        assert "2 years of Python or C++." in job.description and "<li>" not in job.description

    def test_posted_at_is_when_it_was_created_not_when_it_was_republished(self):
        job = db.google_row_to_raw(_g_row("1", "Engineer", created=OLD, sorted_on=NEW))
        assert job.posted_at.startswith("2025-09-27")

    @pytest.mark.parametrize("body", ["", "<html>redesigned</html>",
                                      "AF_initDataCallback({key: 'ds:1', hash: '2', data:not json, sideChannel: {}});"])
    def test_a_page_that_does_not_fit_yields_nothing(self, body):
        assert db.parse_google_results(body) == []

    def test_a_row_that_does_not_fit_is_skipped(self):
        assert db.google_row_to_raw(["not-a-number", "Title"]) is None
        assert db.google_row_to_raw([]) is None

    def _walk(self, pages, cutoff):
        asked = []

        def get_text(url, **kwargs):
            asked.append(kwargs["params"]["page"])
            page = kwargs["params"]["page"]
            return 200, _g_page(pages[page - 1] if page <= len(pages) else [])

        ctx = FetchContext(get_json=None, get_text=get_text, cutoff_iso=cutoff)
        provider = db.GoogleCareersProvider()
        return list(provider.list_jobs(provider.default_endpoint(), ctx)), asked, ctx

    def test_the_walk_stops_on_the_field_the_page_is_sorted_on(self):
        full = lambda **kw: [_g_row(str(i), "Engineer", **kw) for i in range(1, 21)]
        republished = [_g_row("99", "Old role", created=OLD, sorted_on=NEW)] + full()[:19]
        jobs, asked, ctx = self._walk(
            [republished, full(created=OLD, sorted_on=OLD), full()],
            cutoff="2026-10-01T00:00:00+00:00")
        assert asked == [1, 2]                        # page 2 is wholly older: stop
        assert "99" not in {j.source_job_id for j in jobs}   # created last year
        assert len(jobs) == 19 and not ctx.listing_truncated


def _gs_item(sid, title, corporate, *, status="POSTED"):
    return {"roleId": f"{sid}_GS", "corporateTitle": corporate, "jobTitle": title,
            "status": status, "division": "Engineering",
            "locations": [{"primary": True, "state": "NY", "country": "United States",
                           "city": "New York"}],
            "externalSource": {"sourceId": sid}}


class TestGoldmanSachs:
    @pytest.mark.parametrize("corporate,level", [
        ("Analyst", "entry"), ("Associate", "mid"), ("Vice President", "senior"),
        ("Executive Director", "staff"), ("Managing Director", ""), ("", "")])
    def test_the_corporate_title_is_the_stated_level(self, corporate, level):
        job = db.goldman_item_to_raw(_gs_item("186074", "Software Engineer - New York", corporate))
        assert job.extra["seniority"] == level
        assert job.apply_url == "https://higher.gs.com/roles/186074"
        assert job.location == "New York, NY, United States" and job.posted_at == ""

    def test_a_role_that_is_not_posted_is_skipped(self):
        assert db.goldman_item_to_raw(_gs_item("1", "T", "Analyst", status="CLOSED")) is None

    def test_listing_pages_until_the_total_and_detail_reads_the_description(self):
        sent = []

        def post_json(url, **kwargs):
            body = kwargs["json_body"]
            sent.append(body["operationName"])
            if body["operationName"] == "GetRoleById":
                return 200, {"data": {"role": {"descriptionHtml": "<p>Build <b>risk</b> systems.</p>"}}}
            page = body["variables"]["searchQueryInput"]["page"]["pageNumber"]
            items = [_gs_item(str(page * 100 + i), "Engineer", "Associate") for i in range(100 if page == 0 else 3)]
            return 200, {"data": {"roleSearch": {"totalCount": 103, "items": items}}}

        ctx = FetchContext(get_json=None, get_text=None, post_json=post_json)
        provider = db.GoldmanSachsProvider()
        jobs = list(provider.list_jobs(provider.default_endpoint(), ctx))
        assert len(jobs) == 103 and sent == ["GetRoles", "GetRoles"]
        out = provider.fetch_detail(provider.default_endpoint(), jobs[0], ctx)
        assert out.description == "Build risk systems."

    def test_an_error_payload_is_not_an_exception(self):
        ctx = FetchContext(get_json=None, get_text=None,
                           post_json=lambda url, **k: (200, {"errors": [{"message": "nope"}]}))
        provider = db.GoldmanSachsProvider()
        assert list(provider.list_jobs(provider.default_endpoint(), ctx)) == []
        job = RawJob(source_job_id="1", title="T", apply_url="https://higher.gs.com/roles/1")
        assert provider.fetch_detail(provider.default_endpoint(), job, ctx) is None


class TestBankCorporateTitles:
    """A Vice President at a bank is a senior individual contributor."""

    @pytest.mark.parametrize("title,employer,level", [
        ("Software Engineer, Vice President - New York", "Goldman Sachs", "senior"),
        ("Software Engineer III - Vice President", "JPMorgan Chase & Co.", "senior"),
        ("Assistant Vice President, Data Engineer", "Citi", "mid"),
        ("Executive Director, Software Engineering", "JPMorgan Chase", "staff"),
        # Above VP, and everyone who is not a named bank, is unchanged.
        ("Senior Vice President, Technology", "Citi", "executive"),
        ("Managing Director, Engineering", "Goldman Sachs", "executive"),
        ("Vice President of Engineering", "Stripe", "executive"),
        ("Vice President, Engineering", "Citizens Bank", "executive"),
        # A level the title states in ordinary words is not overruled.
        ("Accounting Control Senior Analyst, AVP", "Deutsche Bank", "senior"),
        ("Principal Software Engineer - Full Stack, AVP", "State Street", "principal"),
        ("Financial Controller - AVP", "Barclays", "mid"),
    ])
    def test_levels(self, title, employer, level):
        assert inv.seniority_of(title, employer=employer) == level
