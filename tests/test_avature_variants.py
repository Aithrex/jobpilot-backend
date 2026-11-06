"""Avature's employer-hosted Jobs / FolderDetail template, captured 2026-11-05."""
from dataclasses import replace
from pathlib import Path

import pytest

from services.ats_providers import registry
from services.ats_providers.avature import AvatureProvider, parse_listing
from services.ats_providers.base import Endpoint, FetchContext, RawJob

FIX = Path(__file__).parent / "fixtures" / "ats_providers" / "avature"
ENERGY = "https://jobs.siemens-energy.com/en_US/jobs/Jobs"
SIEMENS = "https://jobs.siemens.com/en_US/externaljobs/SearchJobs"


def context(pages, asked):
    def get_text(url, **kwargs):
        asked.append(url)
        return pages.get(url, (404, None))
    return FetchContext(get_json=None, get_text=get_text)


@pytest.mark.parametrize("url,host,site,route", [
    (SIEMENS + "/?folderRecordsPerPage=6&folderOffset=6", "jobs.siemens.com", "en_US/externaljobs", "SearchJobs"),
    (ENERGY + "/?folderRecordsPerPage=20&folderOffset=20", "jobs.siemens-energy.com", "en_US/jobs", "Jobs"),
])
def test_observed_custom_host_and_listing_route_are_fingerprinted(url, host, site, route):
    match = registry.match_url(url)
    assert match is not None
    provider, fields, _ = match
    ep = provider.endpoint_from_match(fields, url)
    assert (ep.tenant, ep.site) == (host, site)
    assert provider.board_url(ep) == f"https://{host}/{site}/{route}"
    assert "folderOffset" not in ep.site


def test_siemens_cards_keep_location_and_the_own_folder_offset():
    body = (FIX / "siemens_listing.html").read_text(encoding="utf8")
    rows, info = parse_listing(body, SIEMENS, "jobs.siemens.com")
    assert rows[0]["id"] == "528626"
    assert rows[0]["location"] == "Kitchener, Ontario, Canada"
    assert info["offsets"][6] == SIEMENS + "/?folderRecordsPerPage=6&folderOffset=6"


def test_folder_detail_rows_follow_the_pages_own_paging_link():
    body = (FIX / "siemens_energy_listing.html").read_text(encoding="utf8")
    next_url = ENERGY + "/?folderRecordsPerPage=20&folderOffset=20"
    last = '<article><a href="https://jobs.siemens-energy.com/en_US/jobs/FolderDetail/Gas-Turbine-Lathe-Operator-Mon-Thurs-3-45pm-2-15am/304439">Gas Turbine Lathe Operator</a></article>'
    asked = []
    ctx = context({ENERGY: (200, body), next_url: (200, last)}, asked)
    ep = Endpoint(vendor="avature", tenant="jobs.siemens-energy.com", site="en_US/jobs", extra={"listing_route": "Jobs"})
    rows = list(AvatureProvider().list_jobs(ep, ctx))
    assert [j.source_job_id for j in rows] == ["306123", "306779", "304439"]
    assert asked == [ENERGY, next_url]
    assert not ctx.listing_truncated
    assert all(j.date_confidence == "unknown" and not j.posted_at for j in rows)


def test_folder_offset_page_cap_is_incomplete():
    body = (FIX / "siemens_energy_listing.html").read_text(encoding="utf8")
    provider = AvatureProvider()
    provider.policy = replace(provider.policy, max_pages=1)
    ep = Endpoint(vendor="avature", tenant="jobs.siemens-energy.com", site="en_US/jobs", extra={"listing_route": "Jobs"})
    ctx = context({ENERGY: (200, body)}, [])
    assert len(list(provider.list_jobs(ep, ctx))) == 2
    assert ctx.listing_truncated


@pytest.mark.parametrize("later", ["<html><body>Search page loading</body></html>", "repeat"])
def test_unknown_total_never_calls_a_later_zero_progress_page_complete(later):
    body = (FIX / "siemens_listing.html").read_text(encoding="utf8")
    next_url = SIEMENS + "/?folderRecordsPerPage=6&folderOffset=6"
    if later == "repeat":
        # A vendor can repeat an earlier page but omit its stale paging links.
        from lxml import html
        doc = html.fromstring(body)
        for a in doc.xpath('//a[contains(@href,"folderOffset=")]'):
            a.getparent().remove(a)
        later = html.tostring(doc, encoding="unicode")
    ctx = context({SIEMENS: (200, body), next_url: (200, later)}, [])
    ep = Endpoint(vendor="avature", tenant="jobs.siemens.com", site="en_US/externaljobs")
    rows = list(AvatureProvider().list_jobs(ep, ctx))
    assert len(rows) == 2
    assert ctx.options["avature_counts"]["reported"] is None
    assert ctx.listing_truncated


@pytest.mark.parametrize("mode", ["shrinking_total", "unknown_overlap"])
def test_changing_board_cannot_hide_unread_requisitions(mode):
    from lxml import html
    first = (FIX / "siemens_listing.html").read_text(encoding="utf8")
    doc = html.fromstring(first)
    for a in doc.xpath('//a[contains(@href,"folderOffset=")]'):
        a.getparent().remove(a)
    later = html.tostring(doc, encoding="unicode").replace("528623", "999999")
    if mode == "shrinking_total":
        first = first.replace("</body>", "<p>100 results</p></body>")
        later = "<div>" + later.replace("528626", "999998") + "<p>2 results</p></div>"
    next_url = SIEMENS + "/?folderRecordsPerPage=6&folderOffset=6"
    ctx = context({SIEMENS: (200, first), next_url: (200, later)}, [])
    ep = Endpoint(vendor="avature", tenant="jobs.siemens.com", site="en_US/externaljobs")
    rows = list(AvatureProvider().list_jobs(ep, ctx))
    assert len(rows) == (4 if mode == "shrinking_total" else 3)
    assert ctx.listing_truncated
    if mode == "shrinking_total":
        assert ctx.options["avature_counts"]["reported"] == 100


def test_large_reported_total_is_not_read_as_its_last_three_digits():
    body = (FIX / "siemens_listing.html").read_text(encoding="utf8").replace(
        "</body>", '<p>1 - 6 of 5,216 results</p></body>')
    _, info = parse_listing(body, SIEMENS, "jobs.siemens.com")
    assert info["reported"] == 5216


def test_arbitrary_listing_routes_and_unverified_hosts_are_refused():
    provider = AvatureProvider()
    ep = Endpoint(vendor="avature", tenant="jobs.siemens-energy.com", site="en_US/jobs", extra={"listing_route": "../../private"})
    assert provider.board_url(ep) == ""
    assert registry.match_url("https://jobs.unverified.example/en_US/jobs/Jobs") is None


@pytest.mark.parametrize("host,site,route,job_id,slug,fixture,location", [
    ("jobs.siemens.com", "en_US/externaljobs", "SearchJobs", "528626", "", "siemens_detail.html",
     "Kitchener - Ontario - Canada"),
    ("jobs.siemens-energy.com", "en_US/jobs", "Jobs", "306123", "Finance-Head-of-Regional-Service-Middle-East/",
     "siemens_energy_detail.html", "Abu Dhabi, United Arab Emirates; Dammam, Eastern Province, SAUDI ARABIA; Doha, Ad Dawhah, QATAR"),
])
def test_observed_detail_templates_keep_all_published_locations(host, site, route, job_id, slug, fixture, location):
    path = "FolderDetail" if route == "Jobs" else "JobDetail"
    url = f"https://{host}/{site}/{path}/{slug}{job_id}"
    ep = Endpoint(vendor="avature", tenant=host, site=site, extra={"listing_route": route})
    job = RawJob(source_job_id=job_id, title="job", apply_url=url, detail_url=url)
    ctx = context({url: (200, (FIX / fixture).read_text(encoding="utf8"))}, [])
    out = AvatureProvider().fetch_detail(ep, job, ctx)
    assert out is not None and len(out.description) > 500
    assert out.location == location
    assert out.extra["legal_entity"].startswith("Siemens")
    if route == "Jobs":
        assert out.posted_at == "2026-11-06T00:00:00+00:00"
        assert out.extra["locations"] == [
            "Abu Dhabi, United Arab Emirates", "Dammam, Eastern Province, SAUDI ARABIA",
            "Doha, Ad Dawhah, QATAR",
        ]
    else:
        assert not out.posted_at and out.date_confidence == "unknown"
