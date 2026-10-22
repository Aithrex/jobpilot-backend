"""Avature SearchJobs/JobDetail sites. Fixtures trimmed from Frequentis and the
Koch board filtered to Flint Hills Resources, captured 2026-10-22."""
from pathlib import Path

import pytest

from services.ats_providers import registry
from services.ats_providers.avature import AvatureProvider
from services.ats_providers.base import (DATE_EXACT, DATE_UNKNOWN, ChallengeError,
                                         Endpoint, FetchContext, RawJob)

FIX = Path(__file__).parent / "fixtures" / "ats_providers" / "avature"
FREQ = "https://jobs.frequentis.com/careers/SearchJobs"
KOCH = ("https://koch.avature.net/en_US/careers/SearchJobs?732=6297&tags=rm.kcm.web.kcm-004")
KOCH_SITE = "en_US/careers?732=6297&tags=rm.kcm.web.kcm-004"


def _read(name):
    return (FIX / name).read_text(encoding="utf8")


def _ctx(responses, asked):
    def get_text(url, **kwargs):
        asked.append(url)
        for key, value in responses.items():
            if url == key:
                return value
        return 404, None
    return FetchContext(get_json=None, get_text=get_text, options={})


def _freq():
    return Endpoint(vendor="avature", tenant="jobs.frequentis.com", site="careers",
                    host="jobs.frequentis.com")


def _koch(entity="Flint Hills Resources"):
    return Endpoint(vendor="avature", tenant="koch.avature.net", site=KOCH_SITE,
                    host="koch.avature.net", extra={"entity_name": entity} if entity else {})


def test_the_employers_filtered_link_is_kept_exactly():
    url = KOCH.replace("&", "&amp;")
    provider, fields, _ = registry.match_url(url)
    ep = provider.endpoint_from_match(fields, url)
    assert (ep.vendor, ep.tenant, ep.site) == ("avature", "koch.avature.net", KOCH_SITE)
    assert provider.board_url(ep) == KOCH
    unfiltered = Endpoint(vendor="avature", tenant="koch.avature.net", site="en_US/careers")
    assert ep.member_key != unfiltered.member_key


def test_paging_offsets_are_not_identity():
    url = "https://jobs.frequentis.com/careers/SearchJobs/?jobOffset=20"
    provider, fields, _ = registry.match_url(url)
    assert provider.endpoint_from_match(fields, url).site == "careers"


def test_an_unlisted_custom_host_is_not_avature():
    assert registry.match_url("https://jobs.example.com/careers/SearchJobs") is None


def test_listing_follows_the_pages_own_offsets_and_drops_duplicates():
    page2 = _read("frequentis_page2.html").replace(
        "</ul>", '<li><a href="https://jobs.frequentis.com/careers/JobDetail/x/3588">'
        "ATM Service Engineer - Voice Solutions (all genders)</a></li></ul>")
    asked = []
    responses = {FREQ: (200, _read("frequentis_page1.html")),
                 "https://jobs.frequentis.com/careers/SearchJobs/?jobOffset=20": (200, page2),
                 "https://jobs.frequentis.com/careers/SearchJobs/?jobOffset=40": (200, page2),
                 "https://jobs.frequentis.com/careers/SearchJobs/?jobOffset=60": (200, page2)}
    ctx = _ctx(responses, asked)
    jobs = list(AvatureProvider().list_jobs(_freq(), ctx))
    assert asked[:2] == [FREQ, "https://jobs.frequentis.com/careers/SearchJobs/?jobOffset=20"]
    assert [j.source_job_id for j in jobs] == ["3588", "3652", "3522", "3653", "3648"]
    assert all(j.posted_at == "" and j.date_confidence == DATE_UNKNOWN for j in jobs)
    # 5 distinct against a reported 64: a partial census, never complete.
    assert ctx.listing_truncated
    assert ctx.options["avature_counts"]["reported"] == 64


def test_a_shared_board_keeps_only_the_named_entity():
    body = _read("koch_fhr_page1.html").replace("Flint Hills Resources", "Georgia-Pacific", 1)
    asked = []
    ctx = _ctx({KOCH: (200, body)}, asked)
    jobs = list(AvatureProvider().list_jobs(_koch(), ctx))
    assert asked[0] == KOCH                      # the filtered URL, never all of Koch
    assert len(jobs) == 1 and ctx.options["avature_counts"]["filtered"] == 1
    assert jobs[0].location


def test_fhr_rows_carry_their_location():
    jobs = list(AvatureProvider().list_jobs(_koch(), _ctx({KOCH: (200, _read("koch_fhr_page1.html"))}, [])))
    assert [(j.source_job_id, j.location) for j in jobs][0] == ("195994", "Corpus Christi, Texas")


@pytest.mark.parametrize("response", [(403, None), (None, None), (200, "<html>No jobs found</html>")])
def test_an_unread_board_is_unverified_never_empty(response):
    ctx = _ctx({FREQ: response}, [])
    assert list(AvatureProvider().list_jobs(_freq(), ctx)) == [] and ctx.listing_truncated


def test_a_challenge_raises():
    with pytest.raises(ChallengeError):
        list(AvatureProvider().list_jobs(_freq(), _ctx({FREQ: (429, None)}, [])))


def test_detail_reads_the_page_body_and_the_exact_date():
    url = "https://jobs.frequentis.com/careers/JobDetail/FUSA-Lead-Systems-Engineer/3268"
    out = AvatureProvider().fetch_detail(
        _freq(), RawJob(source_job_id="3268", title="t", apply_url=url, detail_url=url),
        _ctx({url: (200, _read("frequentis_detail_3268.html"))}, []))
    assert out.posted_at == "2025-10-10T00:00:00+00:00" and out.date_confidence == DATE_EXACT
    assert out.description and out.description.lower() != "none"
    assert out.location == "Maryland, Columbia"
    assert out.extra["legal_entity"] == "FREQUENTIS USA, Inc."


def test_detail_refuses_another_host():
    asked = []
    url = "https://koch.avature.net/en_US/careers/JobDetail/x/1"
    assert AvatureProvider().fetch_detail(
        _freq(), RawJob(source_job_id="1", title="t", apply_url=url, detail_url=url),
        _ctx({}, asked)) is None
    assert asked == []
