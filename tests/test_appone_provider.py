"""AppOne / Paychex Recruiting. Fixtures trimmed from Universal Background
Screening's board, 2026-10-22."""
from pathlib import Path

import pytest

from services.ats_providers import registry
from services.ats_providers.appone import AppOneProvider, browse_link
from services.ats_providers.base import (DATE_UNKNOWN, ChallengeError, Endpoint,
                                         FetchContext, RawJob)

FIX = Path(__file__).parent / "fixtures" / "ats_providers" / "appone"
SV = "universalbackgroundscreening.appone.com"
SEARCH = "https://www2.appone.com/Search/Search.aspx"
BROWSE = ("https://recruiting.myapps.paychex.com/appone/Branding/ReqTemplate/"
          "BrowseAllJobsbyCategory.asp?Type=REQ&FromID=1&ClientID=18117&B_ID=91&JobCode=0"
          f"&CountryID=3&LanguageID=2&servervar={SV}")
DETAIL = ("https://recruiting.myapps.paychex.com/appone/MainInfoReq.asp?R_ID=7194630&B_ID=91"
          "&fid=1&Adid=&SearchScreenID=17847&CountryID=3&LanguageID=2")


def _read(name):
    return (FIX / name).read_text(encoding="utf8")


def _ctx(responses, sent):
    def get_text(url, **kwargs):
        sent.append((url, dict(kwargs.get("params") or {})))
        return responses.get(url, (404, None))
    return FetchContext(get_json=None, get_text=get_text)


def _ok():
    return {SEARCH: (200, _read("search.html")),
            BROWSE: (200, _read("browse_by_category.html")),
            DETAIL: (200, _read("detail_7194630.html"))}


def _ep():
    return Endpoint(vendor="appone", tenant=SV, host="www2.appone.com")


@pytest.mark.parametrize("url", [
    f"https://www2.appone.com/Search/Search.aspx?ServerVar={SV}",
    BROWSE,
])
def test_the_server_var_is_the_tenant_exactly(url):
    provider, fields, _ = registry.match_url(url)
    ep = provider.endpoint_from_match(fields, url)
    assert (ep.vendor, ep.tenant) == ("appone", SV)


def test_a_browse_link_for_another_server_var_is_ignored():
    body = _read("search.html").replace(f"servervar={SV}", "servervar=other.appone.com")
    assert browse_link(body, SV) == ""


def test_listing_follows_the_employers_own_browse_link():
    sent = []
    jobs = list(AppOneProvider().list_jobs(_ep(), _ctx(_ok(), sent)))
    assert sent[0] == (SEARCH, {"ServerVar": SV})
    assert sent[1][0] == BROWSE                 # ClientID/B_ID read from the page
    assert len(jobs) == 8 and len({j.source_job_id for j in jobs}) == 8
    se = next(j for j in jobs if j.source_job_id == "7194630")
    assert se.title == "Software Engineer (Remote Opportunity)"
    assert se.location == "AZ, Phoenix" and se.department == "Information Technology"
    assert se.apply_url == DETAIL
    assert se.extra["board_employer"] == "Universal Background Screening"
    assert all(j.posted_at == "" and j.date_confidence == DATE_UNKNOWN for j in jobs)


def test_same_title_in_two_places_is_two_requisitions():
    jobs = list(AppOneProvider().list_jobs(_ep(), _ctx(_ok(), [])))
    csa = [(j.source_job_id, j.location) for j in jobs if j.title == "Client Success Advocate I"]
    assert csa == [("7216920", "AZ, Phoenix"), ("7216923", "SD, Watertown")]


def test_detail_reads_the_labelled_rows():
    out = AppOneProvider().fetch_detail(
        _ep(), RawJob(source_job_id="7194630", title="t", apply_url=DETAIL, detail_url=DETAIL),
        _ctx(_ok(), []))
    assert out.description.startswith("Universal Background provides")
    assert out.employment_type == "Full-Time" and out.location == "AZ Phoenix"
    assert out.extra["accepting_applications"] is True
    assert out.posted_at == ""


@pytest.mark.parametrize("override", [
    {SEARCH: (403, None)},
    {SEARCH: (200, "<html><title>x</title></html>")},     # no browse link
    {BROWSE: (None, None)},
])
def test_an_unread_board_is_unverified_never_empty(override):
    responses = {**_ok(), **override}
    ctx = _ctx(responses, [])
    assert list(AppOneProvider().list_jobs(_ep(), ctx)) == []
    assert ctx.listing_truncated


def test_a_challenge_raises():
    with pytest.raises(ChallengeError):
        list(AppOneProvider().list_jobs(_ep(), _ctx({SEARCH: (429, None)}, [])))


def test_reshaped_rows_yield_fewer_rows_not_a_crash():
    body = _read("browse_by_category.html").replace("MainInfoReq.asp", "Other.asp", 3)
    responses = {**_ok(), BROWSE: (200, body)}
    jobs = list(AppOneProvider().list_jobs(_ep(), _ctx(responses, [])))
    assert 0 < len(jobs) < 8


def test_detail_refuses_a_mismatched_or_foreign_url():
    sent = []
    for url in ("https://evil.example/appone/MainInfoReq.asp?R_ID=7194630",
                DETAIL.replace("R_ID=7194630", "R_ID=1")):
        job = RawJob(source_job_id="7194630", title="t", apply_url=url, detail_url=url)
        assert AppOneProvider().fetch_detail(_ep(), job, _ctx({}, sent)) is None
    assert sent == []
