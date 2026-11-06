"""Replay two positively observed employer templates, without network access."""
from pathlib import Path

import pytest

from services.ats_providers.base import Endpoint, FetchContext
from services.ats_providers.own_site import OwnSiteCareersProvider, extract_postings

FIX = Path(__file__).parent / "fixtures/ats_providers/researched_ownsite"
KPMG = "https://www.kpmguscareers.com/job-search/"
DEEL = "https://www.deel.com/careers/"


def endpoint(url, host):
    return Endpoint(vendor="careers_page", tenant=host, host=host, extra={"listing_url": url})


def context(pages):
    asked = []
    def get(url, **kw):
        asked.append(url)
        return pages.get(url, (404, ""))
    return FetchContext(get_json=None, get_text=get), asked


def test_kpmg_query_ids_are_distinct_requisitions_with_card_titles_and_exact_urls():
    body = (FIX / "kpmg_listing.html").read_text(encoding="utf8")
    rows, info = extract_postings(body, KPMG, "kpmguscareers.com")
    assert [r["id"] for r in rows] == ["139724", "134771"]
    assert rows[0]["title"] == "Databricks Sales Executive (Federal sector)"
    assert rows[0]["detail_url"] == "https://www.kpmguscareers.com/jobdetail/?jobId=139724"
    assert info["pagination_unresolved"]
    ctx, asked = context({KPMG: (200, body)})
    jobs = list(OwnSiteCareersProvider().list_jobs(endpoint(KPMG, "kpmguscareers.com"), ctx))
    assert len(jobs) == 2 and asked == [KPMG] and ctx.listing_truncated


def test_kpmg_real_detail_keeps_published_description_and_location():
    provider = OwnSiteCareersProvider()
    listing = (FIX / "kpmg_listing.html").read_text(encoding="utf8")
    detail = (FIX / "kpmg_detail.html").read_text(encoding="utf8")
    ep = endpoint(KPMG, "kpmguscareers.com")
    ctx, _ = context({KPMG: (200, listing), "https://www.kpmguscareers.com/jobdetail/?jobId=139724": (200, detail)})
    job = next(provider.list_jobs(ep, ctx))
    out = provider.fetch_detail(ep, job, ctx)
    assert out is not None and len(out.description) > 1000
    assert "Databricks" in out.description
    assert out.location
    assert out.posted_at.startswith("2026-11-01")
    assert "&lt;p" not in out.description and "<p>" not in out.description


@pytest.mark.parametrize("url,host", [(KPMG, "kpmguscareers.com"), (DEEL, "deel.com"),
    ("https://jobs.deel.com/job-boards/klarna", "jobs.deel.com")])
def test_canonical_board_url_preserves_the_specific_employer_listing(url, host):
    assert OwnSiteCareersProvider().board_url(endpoint(url, host)) == url


@pytest.mark.parametrize("href", ["/profile/?jobId=139724", "/jobdetail/?id=139724", "/jobdetail/?jobId=abc/def", "/jobdetail/?jobId=139724&jobId=999"])
def test_query_id_support_does_not_turn_arbitrary_links_into_jobs(href):
    body = f'<article><h2>Software Engineer</h2><a href="{href}">Apply now</a><p>Location: Boston</p></article>'
    rows, _ = extract_postings(body, KPMG, "kpmguscareers.com")
    assert rows == []


def test_deel_flight_retains_real_ids_urls_dates_and_multiple_locations():
    body = (FIX / "deel_flight.html").read_text(encoding="utf8")
    rows, info = extract_postings(body, DEEL, "deel.com")
    assert len(rows) == 4
    assert "Staff Product Operations Manager - Benefits" in {r["title"] for r in rows}
    assert "Associate Customer Operations Manager | Scale Team" in {r["title"] for r in rows}
    row = next(r for r in rows if r["id"] == "8c6883db-c84e-4c75-ae99-d72d4fbf7cad")
    assert row["apply_url"] == "https://jobs.deel.com/deel/job-details/8c6883db-c84e-4c75-ae99-d72d4fbf7cad/application"
    assert row["detail_url"] == "https://www.deel.com/careers/position/?ashby_jid=8c6883db-c84e-4c75-ae99-d72d4fbf7cad"
    assert row["posted_at"].startswith("2026-10-22T15:37:26")
    assert row["department"] == "sales" and row["employment_type"] == "Full-time"
    assert row["locations"] == ["Sweden", "Norway"]
    assert info["hydrated_total"] == 251
    ctx, _ = context({DEEL: (200, body)})
    jobs = list(OwnSiteCareersProvider().list_jobs(endpoint(DEEL, "deel.com"), ctx))
    assert jobs[0].extra["locations"]
    assert ctx.listing_truncated  # The fixture retains only four of 251 jobs.


def test_kpmg_opt_in_uses_the_published_spage_mapping_and_keeps_partial():
    ep = endpoint(KPMG, "kpmguscareers.com")
    ep.extra["pagination_param"] = "spage"
    first = (FIX / "kpmg_listing.html").read_text(encoding="utf8")
    second = (FIX / "kpmg_page2.html").read_text(encoding="utf8")
    ctx, asked = context({KPMG: (200, first), KPMG + "?spage=2": (200, second)})
    jobs = list(OwnSiteCareersProvider().list_jobs(ep, ctx))
    assert [j.source_job_id for j in jobs] == ["139724", "134771", "134611", "138252"]
    assert asked == [KPMG, KPMG + "?spage=2", KPMG + "?spage=3"]
    assert ctx.listing_truncated


def test_shopify_marketing_ctas_are_not_jobs_and_aria_title_stays_separate():
    body = (FIX / "shopify_listing.html").read_text(encoding="utf8")
    rows, _ = extract_postings(body, "https://www.shopify.com/careers", "shopify.com")
    assert len(rows) == 25
    assert all("extraordinary" not in r["url"] for r in rows)
    job = next(r for r in rows if "streaming-compute_" in r["url"])
    assert job["title"] == "Senior Infrastructure Engineer - Streaming Compute"


@pytest.mark.parametrize("later", ["<html>Loading</html>", "repeat", "overlap"])
def test_ownsite_zero_progress_later_page_is_not_a_complete_census(later):
    body = (FIX / "kpmg_listing.html").read_text(encoding="utf8")
    ep = endpoint(KPMG, "kpmguscareers.com")
    ep.extra["pagination_param"] = "spage"
    if later in ("repeat", "overlap"):
        from lxml import html
        doc = html.fromstring(body)
        for nav in doc.xpath("//nav"):
            nav.getparent().remove(nav)
        later = html.tostring(doc, encoding="unicode").replace("134771", "999999") if later == "overlap" else html.tostring(doc, encoding="unicode")
    ctx, _ = context({KPMG: (200, body), KPMG + "?spage=2": (200, later)})
    assert len(list(OwnSiteCareersProvider().list_jobs(ep, ctx))) == (3 if "999999" in later else 2)
    assert ctx.listing_truncated


def test_published_query_pagination_can_pass_the_old_ten_page_limit():
    from lxml import html
    template = (FIX / "kpmg_listing.html").read_text(encoding="utf8")
    pages = {}
    for page in range(1, 12):
        doc = html.fromstring(template)
        for i, a in enumerate(doc.xpath('//a[contains(@href,"jobId=")]')):
            a.set("href", f"/jobdetail/?jobId={1000 + page * 2 + i}")
        for a in doc.xpath('//a[@data-href]'):
            if page == 11:
                a.getparent().remove(a)
            else:
                a.set("data-href", str(page + 1))
        pages[KPMG if page == 1 else KPMG + f"?spage={page}"] = (200, html.tostring(doc, encoding="unicode"))
    ep = endpoint(KPMG, "kpmguscareers.com")
    ep.extra["pagination_param"] = "spage"
    ctx, asked = context(pages)
    assert len(list(OwnSiteCareersProvider().list_jobs(ep, ctx))) == 22
    assert len(asked) == 11 and not ctx.listing_truncated


def test_flight_decode_budget_is_not_silent_completeness(monkeypatch):
    from services.ats_providers import own_site
    body = (FIX / "deel_flight.html").read_text(encoding="utf8")
    monkeypatch.setattr(own_site, "_HYDRATION_CHAR_CAP", 100)
    rows, info = extract_postings(body, DEEL, "deel.com")
    assert rows == [] and info["hydration_truncated"]


@pytest.mark.parametrize("counts", [(923,), (8, 3)])
def test_published_result_count_cannot_silently_shrink_to_complete(counts):
    from lxml import html
    template = (FIX / "kpmg_listing.html").read_text(encoding="utf8")
    pages = {}
    for page, count in enumerate(counts, 1):
        doc = html.fromstring(template)
        if page > 1:
            for a in doc.xpath('//a[contains(@href,"jobId=")]'):
                a.set("href", a.get("href").replace("134771", "999999"))
        if page == len(counts):
            for nav in doc.xpath("//nav"):
                nav.getparent().remove(nav)
        count_node = html.fragment_fromstring(f'<div><span data-action="count">{count}</span> Results</div>')
        doc.append(count_node)
        pages[KPMG if page == 1 else KPMG + "?spage=2"] = (200, html.tostring(doc, encoding="unicode"))
    ep = endpoint(KPMG, "kpmguscareers.com")
    ep.extra["pagination_param"] = "spage"
    ctx, _ = context(pages)
    jobs = list(OwnSiteCareersProvider().list_jobs(ep, ctx))
    assert len(jobs) == (2 if len(counts) == 1 else 3)
    assert ctx.listing_truncated
    assert ctx.options["careers_page_counts"]["reported"] == counts[0]


def test_link_extraction_budget_does_not_authorize_closure(monkeypatch):
    from services.ats_providers import own_site
    monkeypatch.setattr(own_site, "MAX_LINKS_PER_PAGE", 2)
    body = ''.join(f'<a href="/jobs/engineer-{i}">Software Engineer {i}</a>' for i in range(4))
    ep = endpoint("https://example.com/careers", "example.com")
    ctx, _ = context({ep.extra["listing_url"]: (200, body)})
    assert len(list(OwnSiteCareersProvider().list_jobs(ep, ctx))) == 2
    assert ctx.listing_truncated


@pytest.mark.parametrize("failure", [RuntimeError("network stopped"), (405, "blocked"), (403, "Access denied"), (200, "Verify you are human")])
def test_fetch_failure_marks_partial_before_raising(failure):
    from services.ats_providers.base import ChallengeError
    def get(url, **kw):
        if isinstance(failure, Exception):
            raise failure
        return failure
    ctx = FetchContext(get_json=None, get_text=get)
    with pytest.raises((RuntimeError, ChallengeError)):
        list(OwnSiteCareersProvider().list_jobs(endpoint(KPMG, "kpmguscareers.com"), ctx))
    assert ctx.listing_truncated
    assert ctx.options["careers_page_completeness"] == "unverified"


def test_expired_deadline_after_parsing_emits_no_rows(monkeypatch):
    from services.ats_providers import own_site
    original = own_site.extract_postings
    def parse_then_stop(*args, **kwargs):
        result = original(*args, **kwargs)
        ctx.deadline = 0
        return result
    monkeypatch.setattr(own_site, "extract_postings", parse_then_stop)
    body = (FIX / "kpmg_listing.html").read_text(encoding="utf8")
    ctx, _ = context({KPMG: (200, body)})
    assert list(OwnSiteCareersProvider().list_jobs(endpoint(KPMG, "kpmguscareers.com"), ctx)) == []
    assert ctx.listing_truncated


def test_missing_listing_url_is_unverified():
    ctx, asked = context({})
    assert list(OwnSiteCareersProvider().list_jobs(Endpoint(vendor="careers_page", tenant="example.com"), ctx)) == []
    assert ctx.listing_truncated and asked == []
    assert ctx.options["careers_page_completeness"] == "unverified"
