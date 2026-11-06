"""Observed public templates; boundary mutations are synthetic test cases."""
from dataclasses import replace
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import pytest

from services.ats_providers import registry
from services.ats_providers.avature import AvatureProvider, parse_listing
from services.ats_providers.base import ChallengeError, Endpoint, FetchContext
from services.ats_providers.successfactors import SuccessFactorsProvider

FIX = Path(__file__).parent / "fixtures" / "ats_providers" / "researched_templates"
NRG = "https://careers.nrgenergy.com"
QUERY = "q=&sortColumn=referencedate&sortDirection=desc&startrow="


def test_successfactors_does_not_promise_dates_on_undated_public_templates():
    body = (FIX / "supermicro_undated.html").read_text(encoding="utf8")
    ctx, _ = context(lambda _url: (200, body))
    provider = SuccessFactorsProvider()
    jobs = list(provider.list_jobs(Endpoint(vendor="successfactors", tenant="supermicro",
                                            host="jobs.supermicro.com"), ctx))
    assert len(jobs) == 2
    assert all(j.posted_at == "" and j.date_confidence == "unknown" for j in jobs)
    assert provider.date_confidence == "unknown"


def context(pages):
    asked = []
    def get_text(url, **kwargs):
        if kwargs.get("params"):
            url += "?" + urlencode(kwargs["params"])
        asked.append(url)
        return pages(url) if callable(pages) else pages.get(url, (500, ""))
    return FetchContext(get_json=None, get_text=get_text), asked


def sf_endpoint(**extra):
    return Endpoint(vendor="successfactors", tenant="nrgenergy", host="careers.nrgenergy.com", extra=extra)


def test_nrg_short_page_follows_the_published_next_offset_and_reports_partial():
    first = NRG + "/search/?" + QUERY + "0"
    body = (FIX / "nrg_search.html").read_text(encoding="utf8")
    ctx, asked = context({first: (200, body)})
    rows = list(SuccessFactorsProvider().list_jobs(sf_endpoint(), ctx))
    assert len(rows) == 10
    assert asked == [first, NRG + "/search/?" + QUERY + "10"]
    assert ctx.listing_truncated
    assert ctx.options["successfactors_counts"]["reported"] == 231


def test_search_path_stays_on_the_employers_linked_section():
    first = NRG + "/nrg/search/?" + QUERY + "0"
    body = (FIX / "nrg_search.html").read_text(encoding="utf8")
    ctx, asked = context({first: (200, body)})
    list(SuccessFactorsProvider().list_jobs(sf_endpoint(search_path="/nrg/search/"), ctx))
    assert asked == [first, NRG + "/nrg/search/?" + QUERY + "10"]
    assert SuccessFactorsProvider().board_url(sf_endpoint(search_path="/nrg/search/")) == NRG + "/nrg/search/"


@pytest.mark.parametrize("path", ["//other.example/search/", "/../search/", "/private/", "https://other.example/search/"])
def test_search_path_rejects_unpublished_or_cross_host_shapes(path):
    ctx, asked = context({})
    assert list(SuccessFactorsProvider().list_jobs(sf_endpoint(search_path=path), ctx)) == []
    assert asked == []


def test_duplicate_page_stops_incomplete_without_recounting_requisitions():
    body = (FIX / "nrg_search.html").read_text(encoding="utf8")
    ctx, asked = context(lambda url: (200, body))
    rows = list(SuccessFactorsProvider().list_jobs(sf_endpoint(), ctx))
    assert len(rows) == len({row.source_job_id for row in rows}) == 10
    assert len(asked) == 2
    assert ctx.listing_truncated


def test_page_budget_is_not_a_complete_census():
    provider = SuccessFactorsProvider()
    provider.policy = replace(provider.policy, max_pages=1)
    body = (FIX / "nrg_search.html").read_text(encoding="utf8")
    ctx, asked = context(lambda url: (200, body))
    assert len(list(provider.list_jobs(sf_endpoint(), ctx))) == 10
    assert len(asked) == 1 and ctx.listing_truncated


def test_missing_or_unsafe_next_page_does_not_hide_a_published_total():
    body = (FIX / "nrg_search.html").read_text(encoding="utf8").replace(
        'href="?q=', 'href="https://other.example/search/?q=')
    ctx, asked = context(lambda url: (200, body))
    rows = list(SuccessFactorsProvider().list_jobs(sf_endpoint(), ctx))
    assert len(rows) == 10 and len(asked) == 1
    assert ctx.listing_truncated


def test_published_page_walk_can_pass_the_old_twelve_page_ceiling():
    # Synthetic boundary: reuse observed RMK structure with distinct IDs.
    from lxml import html
    template = (FIX / "nrg_search.html").read_text(encoding="utf8")
    def page(url):
        offset = int(parse_qs(urlparse(url).query)["startrow"][0])
        doc = html.fromstring(template)
        for anchor in doc.xpath('//a[contains(@href,"startrow=")]'):
            anchor.getparent().remove(anchor)
        for i, row in enumerate(doc.xpath('//tr[contains(@class,"data-row")]')):
            for anchor in row.xpath('.//a[contains(@class,"jobTitle-link")]'):
                anchor.set("href", f"/nrg/job/test/{1000 + offset + i}/")
        label = doc.xpath('//*[contains(@class,"paginationLabel")]')[0]
        label.clear()
        label.set("class", "paginationLabel")
        label.text = f"Results {offset + 1} - {offset + 10} of 140"
        if offset < 130:
            node = html.Element("a", href="?" + QUERY + str(offset + 10))
            node.text = "Next"
            doc.append(node)
        return 200, html.tostring(doc, encoding="unicode")
    ctx, asked = context(page)
    rows = list(SuccessFactorsProvider().list_jobs(sf_endpoint(), ctx))
    assert len(rows) == 140 and len(asked) == 14
    assert not ctx.listing_truncated


def test_deadline_stops_before_any_request():
    ctx, asked = context({})
    ctx.deadline = 1
    assert list(SuccessFactorsProvider().list_jobs(sf_endpoint(), ctx)) == []
    assert asked == [] and ctx.listing_truncated


def test_transport_or_policy_refusal_cannot_be_a_complete_census():
    def refused(url):
        raise RuntimeError("public fetch refused")
    ctx, asked = context(refused)
    assert list(SuccessFactorsProvider().list_jobs(sf_endpoint(), ctx)) == []
    assert len(asked) == 1 and ctx.listing_truncated


@pytest.mark.parametrize("mode", ["shrinking_total", "unknown_overlap", "stale_boundary"])
def test_successfactors_partial_reads_do_not_become_complete(mode):
    from lxml import html
    body = (FIX / "nrg_search.html").read_text(encoding="utf8")
    def page(url):
        offset = int(parse_qs(urlparse(url).query)["startrow"][0])
        doc = html.fromstring(body)
        if mode == "unknown_overlap":
            for label in doc.xpath('//*[contains(@class,"paginationLabel")]'):
                label.getparent().remove(label)
        elif offset and mode == "shrinking_total":
            for label in doc.xpath('//*[contains(@class,"paginationLabel")]'):
                label.text = "Results 11 - 20 of 5"
                for child in list(label):
                    label.remove(child)
        for i, row in enumerate(doc.xpath('//tr[contains(@class,"data-row")]')):
            rid = 1000 + offset + i
            if mode == "unknown_overlap" and offset:
                if i > 1:
                    row.getparent().remove(row)
                    continue
                rid = 1000 if i == 0 else 2000
            for a in row.xpath('.//a[contains(@class,"jobTitle-link")]'):
                a.set("href", f"/nrg/job/test/{rid}/")
            if mode == "stale_boundary":
                for date in row.xpath('.//span[contains(@class,"jobDate")]'):
                    date.text = "Jan 1, 2000"
        if offset:
            for a in doc.xpath('//a[contains(@href,"startrow=")]'):
                a.getparent().remove(a)
        return 200, html.tostring(doc, encoding="unicode")
    ctx, asked = context(page)
    if mode == "stale_boundary":
        ctx.cutoff_iso = "2026-11-01T00:00:00+00:00"
    list(SuccessFactorsProvider().list_jobs(sf_endpoint(), ctx))
    assert len(asked) == 2 and ctx.listing_truncated
    if mode == "shrinking_total":
        assert ctx.options["successfactors_counts"]["reported"] == 231


@pytest.mark.parametrize("url,fixture,route", [
    ("https://careers.twosigma.com/careers/OpenRoles", "two_sigma.html", "OpenRoles"),
    ("https://jobs.ea.com/en_US/careers/SearchJobs", "ea.html", "SearchJobs"),
    ("https://apply.deloitte.com/en_US/careers/SearchJobs?3_5_3=477%2C478%2C480&sort=relevancy", "deloitte.html", "SearchJobs"),
])
def test_named_avature_hosts_keep_the_official_route_and_paging_filters(url, fixture, route):
    match = registry.match_url(url)
    assert match is not None
    provider, fields, _ = match
    ep = provider.endpoint_from_match(fields, url)
    assert provider.board_url(ep) == url
    body = (FIX / fixture).read_text(encoding="utf8")
    ctx, asked = context({url: (200, body)})
    rows = list(provider.list_jobs(ep, ctx))
    assert len(rows) == 2 and len(asked) == 2 and ctx.listing_truncated
    assert "/" + route + "/?" in asked[1]
    assert all(not row.posted_at for row in rows)
    if fixture == "deloitte.html":
        assert "3_5_3=%5B%22477%22%2C%22478%22%2C%22480%22%5D" in asked[1]


def test_deloitte_official_filters_remain_distinct_endpoint_identities():
    provider = AvatureProvider()
    endpoints = []
    for query in ("3_5_3=477%2C478%2C480&sort=relevancy", "3_5_3=479&sort=relevancy"):
        url = "https://apply.deloitte.com/en_US/careers/SearchJobs?" + query
        match = registry.match_url(url)
        assert match is not None
        endpoints.append(provider.endpoint_from_match(match[1], url))
    assert endpoints[0].site != endpoints[1].site
    assert all("3_5_3=" in provider.board_url(ep) for ep in endpoints)


@pytest.mark.parametrize("later", ["<html>Loading search</html>", "repeat"])
def test_avature_unknown_total_zero_progress_is_partial(later):
    from lxml import html
    url = "https://careers.twosigma.com/careers/OpenRoles"
    body = (FIX / "two_sigma.html").read_text(encoding="utf8")
    if later == "repeat":
        doc = html.fromstring(body)
        for a in doc.xpath('//a[contains(@href,"jobOffset=")]'):
            a.getparent().remove(a)
        later = html.tostring(doc, encoding="unicode")
    ctx, asked = context(lambda target: (200, body if target == url else later))
    ep = Endpoint(vendor="avature", tenant="careers.twosigma.com", site="careers", extra={"listing_route": "OpenRoles"})
    assert len(list(AvatureProvider().list_jobs(ep, ctx))) == 2
    assert len(asked) == 2 and ctx.listing_truncated


@pytest.mark.parametrize("mode", ["shrinking_total", "unknown_overlap"])
def test_avature_changing_board_cannot_hide_unread_requisitions(mode):
    from lxml import html
    url = "https://careers.twosigma.com/careers/OpenRoles"
    first = (FIX / "two_sigma.html").read_text(encoding="utf8")
    doc = html.fromstring(first)
    for a in doc.xpath('//a[contains(@href,"jobOffset=")]'):
        a.getparent().remove(a)
    articles = doc.xpath("//article")
    for i, article in enumerate(articles):
        if i or mode == "shrinking_total":
            for a in article.xpath('.//a[contains(@href,"/JobDetail/")]'):
                a.set("href", f"https://careers.twosigma.com/careers/JobDetail/test/{999998 + i}")
    later = html.tostring(doc, encoding="unicode")
    if mode == "shrinking_total":
        first = first.replace("</body>", "<p>100 results</p></body>")
        later = "<div>" + later + "<p>2 results</p></div>"
    ctx, asked = context(lambda target: (200, first if target == url else later))
    ep = Endpoint(vendor="avature", tenant="careers.twosigma.com", site="careers", extra={"listing_route": "OpenRoles"})
    rows = list(AvatureProvider().list_jobs(ep, ctx))
    assert len(rows) == (4 if mode == "shrinking_total" else 3)
    assert len(asked) == 2 and ctx.listing_truncated
    if mode == "shrinking_total":
        assert ctx.options["avature_counts"]["reported"] == 100


def test_avature_does_not_shorten_a_comma_separated_board_total():
    url = "https://apply.deloitte.com/en_US/careers/SearchJobs"
    body = (FIX / "deloitte.html").read_text(encoding="utf8")
    body = body.replace("</body>", "<p>1 - 10 of 5,216 results</p></body>")
    _, info = parse_listing(body, url, "apply.deloitte.com")
    assert info["reported"] == 5216


def test_avature_can_pass_the_old_twenty_five_page_ceiling():
    from lxml import html
    url = "https://careers.twosigma.com/careers/OpenRoles"
    template = (FIX / "two_sigma.html").read_text(encoding="utf8")
    def page(target):
        offset = int(parse_qs(urlparse(target).query).get("jobOffset", ["0"])[0])
        doc = html.fromstring(template)
        for a in doc.xpath('//a[contains(@href,"jobOffset=")]'):
            a.getparent().remove(a)
        for i, article in enumerate(doc.xpath("//article")):
            for a in article.xpath('.//a[contains(@href,"/JobDetail/")]'):
                a.set("href", f"https://careers.twosigma.com/careers/JobDetail/test/{1000 + offset + i}")
        if offset < 250:
            node = html.Element("a", href=url + "/?jobRecordsPerPage=10&jobOffset=" + str(offset + 10))
            node.text = "Next"
            doc.append(node)
        return 200, html.tostring(doc, encoding="unicode")
    ctx, asked = context(page)
    ep = Endpoint(vendor="avature", tenant="careers.twosigma.com", site="careers", extra={"listing_route": "OpenRoles"})
    assert len(list(AvatureProvider().list_jobs(ep, ctx))) == 52
    assert len(asked) == 26 and not ctx.listing_truncated


@pytest.mark.parametrize("status", [403, 405])
def test_avature_refusal_is_a_block_not_empty(status):
    ep = Endpoint(vendor="avature", tenant="careers.twosigma.com", site="careers", extra={"listing_route": "OpenRoles"})
    ctx, asked = context(lambda url: (status, ""))
    with pytest.raises(ChallengeError):
        list(AvatureProvider().list_jobs(ep, ctx))
    assert len(asked) == 1
