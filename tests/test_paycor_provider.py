"""Paycor Recruiting (Newton) public embeds. Fixtures trimmed from live pages
captured 2026-10-21 (RevSpring aggregate, Spring Arbor single board)."""
from pathlib import Path

import pytest

from services.ats_providers import registry
from services.ats_providers.base import (DATE_UNKNOWN, ChallengeError, Endpoint,
                                         FetchContext, RawJob)
from services.ats_providers.paycor import PaycorRecruitingProvider, parse_listing

FIX = Path(__file__).parent / "fixtures" / "ats_providers" / "paycor"
REVSPRING = "8a7883d0798f66af0179905b7890010b"
APEX = "8a7883d08d19fedb018d1f0ad6d504c7"
SPRING_ARBOR = "8a7883c66ee9a5cc016ef0ea2d5b06cb"


def _read(name):
    return (FIX / name).read_text(encoding="utf8")


def _ctx(responses, sent):
    def get_text(url, **kwargs):
        sent.append((url, dict(kwargs.get("params") or {})))
        for key, value in responses.items():
            if key in url:
                return value
        return 404, None
    return FetchContext(get_json=None, get_text=get_text)


@pytest.mark.parametrize("url,tenant,site", [
    (f"https://newton.newtonsoftware.com/career/iframe.action?clientId={SPRING_ARBOR}&test=testing",
     SPRING_ARBOR, ""),
    (f"https://recruitingbypaycor.com/career/aggregateIframe.action?clientId={REVSPRING}",
     REVSPRING, "aggregate"),
    (f"https://recruitingbypaycor.com/career/CareerHome.action?parentUrl=x&amp;clientId={SPRING_ARBOR}",
     SPRING_ARBOR, ""),
])
def test_the_embed_is_recognised_and_the_client_id_kept_exactly(url, tenant, site):
    provider, fields, _ = registry.match_url(url)
    endpoint = provider.endpoint_from_match(fields, url)
    assert (endpoint.vendor, endpoint.tenant, endpoint.site) == ("paycor", tenant, site)
    # `test=testing` is not identity: the embed script never forwards it.
    assert "test" not in endpoint.extra


def test_the_aggregate_and_single_board_are_different_members():
    a = Endpoint(vendor="paycor", tenant=REVSPRING, site="aggregate").member_key
    b = Endpoint(vendor="paycor", tenant=REVSPRING).member_key
    assert a != b and a.endswith("-aggregate")


def test_recruiting_paycor_com_is_not_assumed_to_be_this_protocol():
    assert registry.match_url(
        f"https://recruiting.paycor.com/career/iframe.action?clientId={REVSPRING}") is None
    # A posting link is a job, not a board: its clientId may be a business unit.
    assert registry.match_url(
        f"https://recruitingbypaycor.com/career/JobIntroduction.action?clientId={APEX}"
        "&id=8a7887a18f504183018f586a606a549a") is None


def test_aggregate_listing_keeps_every_business_unit_on_the_row():
    sent = []
    ep = Endpoint(vendor="paycor", tenant=REVSPRING, site="aggregate")
    jobs = list(PaycorRecruitingProvider().list_jobs(
        ep, _ctx({"AggregateCareerHome.action": (200, _read("aggregate_home.html"))}, sent)))
    assert sent == [("https://recruitingbypaycor.com/career/AggregateCareerHome.action",
                     {"clientId": REVSPRING})]
    assert len(jobs) == 7 and len({j.source_job_id for j in jobs}) == 7
    first = jobs[0]
    assert first.title == "Hand Fulfillment Operator"
    assert first.location == "Antioch, TN"            # group header, column says City
    assert first.extra["business_unit"] == "Apex Revenue Technologies"
    assert first.extra["business_unit_client_id"] == APEX
    assert f"clientId={APEX}" in first.apply_url and "fromAggregate=true" in first.apply_url
    assert {j.extra["business_unit"] for j in jobs} == {"Apex Revenue Technologies",
                                                        "RevSpring Inc"}
    assert all(j.posted_at == "" and j.date_confidence == DATE_UNKNOWN for j in jobs)


def test_a_shared_board_can_be_restricted_to_named_units():
    ep = Endpoint(vendor="paycor", tenant=REVSPRING, site="aggregate",
                  extra={"business_units": [REVSPRING]})
    jobs = list(PaycorRecruitingProvider().list_jobs(
        ep, _ctx({"AggregateCareerHome.action": (200, _read("aggregate_home.html"))}, [])))
    assert jobs and {j.extra["business_unit_client_id"] for j in jobs} == {REVSPRING}


def test_single_board_reads_the_inline_department_arrays():
    ctx = _ctx({"CareerHome.action": (200, _read("career_home.html"))}, [])
    jobs = list(PaycorRecruitingProvider().list_jobs(
        Endpoint(vendor="paycor", tenant=SPRING_ARBOR), ctx))
    rows, info = parse_listing(_read("career_home.html"))
    assert info["reported"] == len(rows) == len(jobs) == 9
    assert not ctx.listing_truncated
    assert jobs[0].title == "Anatomy & Physiology - Adjunct Faculty"
    assert jobs[0].department == "Adjunct Faculty"
    assert all(f"clientId={SPRING_ARBOR}" in j.apply_url for j in jobs)


def test_fewer_rows_than_the_page_reports_is_truncated_not_complete():
    body = _read("career_home.html").replace("(8 positions)", "(80 positions)")
    ctx = _ctx({"CareerHome.action": (200, body)}, [])
    jobs = list(PaycorRecruitingProvider().list_jobs(
        Endpoint(vendor="paycor", tenant=SPRING_ARBOR), ctx))
    assert len(jobs) == 9 and ctx.listing_truncated


@pytest.mark.parametrize("response", [(403, None), (None, None), (200, "<html>shell</html>")])
def test_an_unread_board_is_unverified_never_empty(response):
    ctx = _ctx({"CareerHome.action": response}, [])
    jobs = list(PaycorRecruitingProvider().list_jobs(
        Endpoint(vendor="paycor", tenant=SPRING_ARBOR), ctx))
    assert jobs == [] and ctx.listing_truncated


def test_a_challenge_raises():
    ctx = _ctx({"CareerHome.action": (429, None)}, [])
    with pytest.raises(ChallengeError):
        list(PaycorRecruitingProvider().list_jobs(
            Endpoint(vendor="paycor", tenant=SPRING_ARBOR), ctx))


def test_schema_change_yields_fewer_rows_not_a_crash():
    body = _read("aggregate_home.html").replace("JobIntroduction.action", "JobView.action")
    ctx = _ctx({"AggregateCareerHome.action": (200, body)}, [])
    jobs = list(PaycorRecruitingProvider().list_jobs(
        Endpoint(vendor="paycor", tenant=REVSPRING, site="aggregate"), ctx))
    assert jobs == []


def test_detail_reads_description_requisition_and_openings():
    url = ("https://recruitingbypaycor.com/career/JobIntroduction.action?fromAggregate=true"
           f"&clientId={REVSPRING}&id=8a78879ea06420fb01a0a1a8f3ab7f0f&lang=en")
    job = RawJob(source_job_id="8a78879ea06420fb01a0a1a8f3ab7f0f", title="t",
                 apply_url=url, detail_url=url)
    out = PaycorRecruitingProvider().fetch_detail(
        Endpoint(vendor="paycor", tenant=REVSPRING, site="aggregate"), job,
        _ctx({"JobIntroduction.action": (200, _read("detail_aggregate.html"))}, []))
    assert out.description.startswith("Job Title: Product Engineer")
    assert out.location == "Atlanta, GA"
    assert out.extra == {"requisition_number": "1335", "openings": "1"}
    assert out.posted_at == ""                          # never minted


def test_detail_refuses_a_url_off_the_vendor_host():
    sent = []
    job = RawJob(source_job_id="x", title="t", apply_url="https://evil.example/",
                 detail_url="https://evil.example/career/JobIntroduction.action?clientId="
                 f"{REVSPRING}&id={REVSPRING}")
    assert PaycorRecruitingProvider().fetch_detail(
        Endpoint(vendor="paycor", tenant=REVSPRING), job, _ctx({}, sent)) is None
    assert sent == []


def test_a_job_pool_is_flagged_as_not_a_requisition():
    body = _read("career_home.html")
    rows, _ = parse_listing(body)
    assert rows   # sanity
    pool = body.replace("Anatomy &amp; Physiology - Adjunct Faculty",
                        "General Staff Applications - Job Pool")
    jobs = list(PaycorRecruitingProvider().list_jobs(
        Endpoint(vendor="paycor", tenant=SPRING_ARBOR),
        _ctx({"CareerHome.action": (200, pool)}, [])))
    assert jobs[0].extra["posting_purpose_hint"] == "talent_community"
