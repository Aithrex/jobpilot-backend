"""Paycom career portals. Fixtures trimmed from the Lincoln Institute of Land
Policy board, 2026-10-22 (session token replaced with a placeholder)."""
import json
from pathlib import Path

import pytest

from services.ats_providers import registry
from services.ats_providers.base import (DATE_EXACT, DATE_RELATIVE, DATE_UNKNOWN,
                                         ChallengeError, Endpoint, FetchContext, RawJob)
from services.ats_providers.paycom import PaycomProvider, session_from_page

FIX = Path(__file__).parent / "fixtures" / "ats_providers" / "paycom"
KEY = "AC25DFC6E5002C200B92C92109B3F768"
SERVICE = "https://portal-applicant-tracking.us-cent.paycomonline.net/"
TOKEN = "eyJ0eXAiOiJKV1QifQ.eyJjbGllbnRjb2RlIjoiMTBBMDcifQ.fixture-signature"


def _page():
    return (FIX / "career_page.html").read_text(encoding="utf8")


def _search():
    return json.loads((FIX / "search.json").read_text(encoding="utf8"))


class _Net:
    def __init__(self, page=(200, None), pages=None, detail=(200, None)):
        self.page = page if page[1] is not None or page[0] != 200 else (200, _page())
        self.pages = pages
        self.detail = detail
        self.sent = []

    def ctx(self):
        def get_text(url, **kw):
            self.sent.append(("GET", url, dict(kw.get("params") or {}), kw.get("extra_headers")))
            return self.page

        def post_json(url, **kw):
            self.sent.append(("POST", url, kw.get("json_body"), kw.get("extra_headers")))
            skip = kw["json_body"]["skip"]
            pages = self.pages if self.pages is not None else [_search()]
            index = skip // 10
            return pages[index] if index < len(pages) else (200, {
                "jobPostingPreviews": [], "jobPostingPreviewsCount": 0})

        def get_json(url, **kw):
            self.sent.append(("GET", url, None, kw.get("extra_headers")))
            return self.detail

        pages = self.pages
        if pages is None:
            self.pages = [(200, _search())]
        return FetchContext(get_json=get_json, get_text=get_text, post_json=post_json,
                            options={})


def _ep():
    return Endpoint(vendor="paycom", tenant=KEY, host="www.paycomonline.net")


@pytest.mark.parametrize("url", [
    f"https://www.paycomonline.net/v4/ats/web.php/jobs?clientkey={KEY}",
    f"https://www.paycomonline.net/v4/ats/web.php/jobs?lang=en&amp;clientkey={KEY}",
    f"https://www.paycomonline.net/v4/ats/web.php/portal/{KEY}/career-page",
])
def test_the_clientkey_is_kept_exactly(url):
    provider, fields, _ = registry.match_url(url)
    ep = provider.endpoint_from_match(fields, url)
    assert (ep.vendor, ep.tenant) == ("paycom", KEY)
    assert provider.board_url(ep).endswith(f"clientkey={KEY}")


def test_session_and_api_origin_come_from_the_page():
    assert session_from_page(_page()) == (TOKEN, SERVICE)


def test_an_api_origin_off_paycom_is_refused():
    body = _page().replace("portal-applicant-tracking.us-cent.paycomonline.net",
                           "evil.example")
    token, service = session_from_page(body)
    assert service == ""


def test_listing_uses_the_page_session_and_reads_every_row():
    net = _Net()
    ctx = net.ctx()
    jobs = list(PaycomProvider().list_jobs(_ep(), ctx))
    assert net.sent[0][:3] == ("GET", "https://www.paycomonline.net/v4/ats/web.php/jobs",
                               {"clientkey": KEY})
    method, url, body, headers = net.sent[1]
    assert (method, url) == ("POST", f"{SERVICE}api/ats/job-posting-previews/search")
    assert body["skip"] == 0 and body["take"] == 10
    assert headers["Authorization"] == TOKEN
    assert len(jobs) == 7 and not ctx.listing_truncated
    first = jobs[0]
    assert first.source_job_id == "316836"
    assert first.apply_url == f"https://www.paycomonline.net/v4/ats/web.php/portal/{KEY}/jobs/316836"
    assert first.date_confidence == DATE_RELATIVE and first.raw_posted_text == "2 days ago"
    hidden = [j for j in jobs if j.raw_posted_text == ""]
    assert hidden and all(j.posted_at == "" and j.date_confidence == DATE_UNKNOWN
                          for j in hidden)
    # the token never lands on the endpoint, whose document is persisted
    assert TOKEN not in json.dumps(_ep().to_doc())


def test_pages_until_the_reported_count_and_drops_duplicates():
    rows = _search()["jobPostingPreviews"]
    page1 = {"jobPostingPreviews": (rows * 2)[:10], "jobPostingPreviewsCount": 12}
    page2 = {"jobPostingPreviews": rows[:2], "jobPostingPreviewsCount": 12}
    net = _Net(pages=[(200, page1), (200, page2)])
    ctx = net.ctx()
    jobs = list(PaycomProvider().list_jobs(_ep(), ctx))
    assert [s[2]["skip"] for s in net.sent if s[0] == "POST"] == [0, 10]
    assert len(jobs) == 7 and len({j.source_job_id for j in jobs}) == 7
    # 7 distinct rows against a reported 12: not a complete census
    assert ctx.listing_truncated


@pytest.mark.parametrize("page", [(403, None), (None, None)])
def test_an_unread_page_is_unverified_never_empty(page):
    net = _Net(page=page)
    net.page = page
    ctx = net.ctx()
    assert list(PaycomProvider().list_jobs(_ep(), ctx)) == []
    assert ctx.listing_truncated


def test_a_page_without_a_session_is_unverified():
    net = _Net(page=(200, "<html><body>maintenance</body></html>"))
    ctx = net.ctx()
    assert list(PaycomProvider().list_jobs(_ep(), ctx)) == []
    assert ctx.listing_truncated and all(s[0] == "GET" for s in net.sent)


def test_a_refused_search_is_unverified_and_a_challenge_raises():
    net = _Net(pages=[(401, None)])
    ctx = net.ctx()
    assert list(PaycomProvider().list_jobs(_ep(), ctx)) == []
    assert ctx.listing_truncated
    net = _Net(pages=[(429, None)])
    with pytest.raises(ChallengeError):
        list(PaycomProvider().list_jobs(_ep(), net.ctx()))


def test_a_reshaped_response_is_fewer_rows_not_a_crash():
    net = _Net(pages=[(200, {"results": [], "count": 7})])
    ctx = net.ctx()
    assert list(PaycomProvider().list_jobs(_ep(), ctx)) == []
    assert ctx.listing_truncated


def test_detail_gives_the_body_and_the_exact_date():
    detail = json.loads((FIX / "detail_316836.json").read_text(encoding="utf8"))
    net = _Net(detail=(200, detail))
    ctx = net.ctx()
    list(PaycomProvider().list_jobs(_ep(), ctx))
    job = RawJob(source_job_id="316836", title="Backend Software Engineer I/II (55745)",
                 apply_url="x")
    out = PaycomProvider().fetch_detail(_ep(), job, ctx)
    assert net.sent[-1][1] == f"{SERVICE}api/ats/job-postings/316836"
    assert net.sent[-1][3]["Authorization"] == TOKEN
    assert out.posted_at == "2026-10-19T00:00:00+00:00" and out.date_confidence == DATE_EXACT
    assert "Center for Geospatial Solutions" in out.description
    assert "Qualifications" in out.description
    assert out.extra["salaryRange"].startswith("$76,000.00")
    assert out.department == "Information Technology"
