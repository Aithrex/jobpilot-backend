"""BreezyHR portals. Fixtures trimmed from destro-ai.breezy.hr, 2026-10-22."""
from datetime import datetime, timezone
from pathlib import Path

import pytest

from services.ats_providers import registry
from services.ats_providers.base import (DATE_EXACT, DATE_UNKNOWN, ChallengeError,
                                         Endpoint, FetchContext, RawJob)
from services.ats_providers.breezy import BreezyProvider, job_posting_ld

FIX = Path(__file__).parent / "fixtures" / "ats_providers" / "breezy"
DETAIL = ("https://destro-ai.breezy.hr/p/d62989845487-robotics-software-engineer-"
          "field-deployment-support")


def _read(name):
    return (FIX / name).read_text(encoding="utf8")


def _ctx(responses, sent):
    def get_text(url, **kwargs):
        sent.append(url)
        return responses.get(url, (404, None))
    return FetchContext(get_json=None, get_text=get_text)


def _ep():
    return Endpoint(vendor="breezy", tenant="destro-ai", host="destro-ai.breezy.hr")


def test_a_job_detail_link_yields_the_whole_board_not_one_role():
    provider, fields, _ = registry.match_url(DETAIL)
    ep = provider.endpoint_from_match(fields, DETAIL)
    assert (ep.vendor, ep.tenant) == ("breezy", "destro-ai")
    assert provider.board_url(ep) == "https://destro-ai.breezy.hr/"


@pytest.mark.parametrize("url", [
    "https://assets-cdn.breezy.hr/breezy-portal/stylesheets/v3/index.css",
    "https://app.breezy.hr/signin",
    "https://breezy.hr/m/portal?utm_source=Portal",
])
def test_vendor_infrastructure_is_not_a_board(url):
    hit = registry.match_url(url)
    assert hit is None or hit[0].endpoint_from_match(hit[1], url) is None


def test_listing_reads_every_card_and_keeps_same_title_requisitions_apart():
    sent = []
    jobs = list(BreezyProvider().list_jobs(
        _ep(), _ctx({"https://destro-ai.breezy.hr/": (200, _read("board.html"))}, sent)))
    assert sent == ["https://destro-ai.breezy.hr/"]
    assert len(jobs) == 9 and len({j.source_job_id for j in jobs}) == 9
    same = [j for j in jobs if j.title == "Robotics Software Engineer - Field Deployment & Support"]
    assert {(j.source_job_id, j.location) for j in same} == {
        ("d62989845487", "Sumner, WA"), ("692948f1419a", "Savannah, GA")}
    first = jobs[0]
    assert first.department == "Field Deployment & Operations"
    assert first.employment_type == "Full-Time"          # i18n placeholder resolved
    assert first.apply_url == DETAIL
    assert all(j.posted_at == "" and j.date_confidence == DATE_UNKNOWN for j in jobs)


def test_detail_upgrades_the_date_from_the_posting_itself():
    out = BreezyProvider().fetch_detail(
        _ep(), RawJob(source_job_id="d62989845487", title="t", apply_url=DETAIL,
                      detail_url=DETAIL),
        _ctx({DETAIL: (200, _read("detail.html"))}, []))
    assert out.posted_at == "2026-09-16T00:00:00+00:00"
    assert out.date_confidence == DATE_EXACT
    assert out.location == "Sumner, WA, US"
    assert out.extra["hiring_organization"] == "Destro AI"
    assert out.description.startswith("About Destro AI")


def test_a_timestamp_minted_from_the_render_clock_is_not_a_posting_date():
    now = datetime.now(timezone.utc)
    minted = now.strftime("%Y-%m-%dT%H:%M:%S") + ".517Z"
    body = _read("detail.html").replace('"datePosted": "2026-09-16"',
                                        f'"datePosted": "{minted}"')
    out = BreezyProvider().fetch_detail(
        _ep(), RawJob(source_job_id="d62989845487", title="t", apply_url=DETAIL,
                      detail_url=DETAIL), _ctx({DETAIL: (200, body)}, []))
    assert out.posted_at == "" and out.date_confidence == DATE_UNKNOWN


def test_json_ld_in_a_graph_with_a_type_list_is_found():
    body = ('<script type="application/ld+json">{"@graph": [{"@type": "WebSite"},'
            '{"@type": ["JobPosting", "Thing"], "title": "X"}]}</script>')
    assert job_posting_ld(body)["title"] == "X"


@pytest.mark.parametrize("response", [(403, None), (None, None), (200, "<html></html>")])
def test_an_unread_portal_is_unverified_never_empty(response):
    ctx = _ctx({"https://destro-ai.breezy.hr/": response}, [])
    assert list(BreezyProvider().list_jobs(_ep(), ctx)) == []
    assert ctx.listing_truncated


def test_a_challenge_raises():
    with pytest.raises(ChallengeError):
        list(BreezyProvider().list_jobs(
            _ep(), _ctx({"https://destro-ai.breezy.hr/": (429, None)}, [])))


def test_reshaped_cards_yield_fewer_rows_not_a_crash():
    body = _read("board.html").replace("<h2>Robotics Technician</h2>", "<h3>x</h3>")
    jobs = list(BreezyProvider().list_jobs(
        _ep(), _ctx({"https://destro-ai.breezy.hr/": (200, body)}, [])))
    assert 0 < len(jobs) < 9


def test_detail_refuses_another_tenant_or_host():
    sent = []
    for url in ("https://other.breezy.hr/p/d62989845487-x", "https://evil.example/p/d62989845487"):
        job = RawJob(source_job_id="d62989845487", title="t", apply_url=url, detail_url=url)
        assert BreezyProvider().fetch_detail(_ep(), job, _ctx({}, sent)) is None
    assert sent == []
