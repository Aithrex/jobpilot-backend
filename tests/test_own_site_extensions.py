"""Own-site careers pages beyond plain title links (2026-10-22).

Real fixtures are trimmed from the employers' official pages captured on
2026-10-22 (MATS Research, Rugged Science, Orange Logic, Nous Research,
GMI Cloud, Attune, Longbridge). The few pages written by hand below are
labelled SYNTHETIC and model one rule each."""
from datetime import datetime, timezone
from pathlib import Path

import pytest

from services.ats_providers import own_site
from services.ats_providers.base import (DATE_EXACT, DATE_UNKNOWN, Endpoint,
                                         FetchContext, RawJob)

FIX = Path(__file__).parent / "fixtures" / "ats_providers" / "own_site"


@pytest.fixture(autouse=True)
def _fixed_clock_for_minted_dates(monkeypatch):
    """`looks_minted_now` compares a date's time of day with the CLOCK, and the
    Longbridge fixture is stamped 04:28:07.797. Against the real clock this file
    failed for ten minutes every day, and blocked a deploy on 2026-11-01."""
    real = own_site.looks_minted_now
    noon = datetime(2026, 10, 31, 16, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(own_site, "looks_minted_now",
                        lambda value, **_: real(value, now=noon))


def _read(name):
    return (FIX / name).read_text(encoding="utf8")


def _ctx(pages, asked):
    def get_text(url, **kwargs):
        asked.append(url)
        return (200, pages[url]) if url in pages else (404, "")
    return FetchContext(get_json=None, get_text=get_text, options={})


def _ep(host, url):
    return Endpoint(vendor="careers_page", tenant=host, host=host, extra={"listing_url": url})


def _list(host, url, pages):
    asked = []
    ctx = _ctx(pages, asked)
    jobs = list(own_site.OwnSiteCareersProvider().list_jobs(_ep(host, url), ctx))
    return jobs, ctx, asked


# ---------------------------------------------------------------------------
# Real pages
# ---------------------------------------------------------------------------

def test_mats_cards_take_their_title_and_location_and_ignore_the_button():
    url = "https://www.matsprogram.org/careers"
    jobs, ctx, _ = _list("matsprogram.org", url, {url: _read("mats_listing.html")})
    assert [j.title for j in jobs] == [
        "General Counsel", "Research Manager", "(Senior) Program Manager, Mentor Selection"]
    assert [j.source_job_id for j in jobs][0] == "general-counsel"
    assert jobs[1].location == "Berkeley, London, DC"
    # Staff vacancies only: the General Application / Expression of Interest
    # forms are off-site form links, not postings.
    assert not any("Application" in j.title or "Interest" in j.title for j in jobs)
    assert ctx.options[own_site.COMPLETENESS_KEY] == own_site.COMPLETE
    assert not ctx.listing_truncated


def test_mats_detail_takes_the_exact_date_from_its_json_ld():
    detail = "https://www.matsprogram.org/careers/general-counsel"
    job = RawJob(source_job_id="general-counsel", title="General Counsel",
                 apply_url=detail, detail_url=detail)
    out = own_site.OwnSiteCareersProvider().fetch_detail(
        _ep("matsprogram.org", "https://www.matsprogram.org/careers"), job,
        _ctx({detail: _read("mats_detail_general_counsel.html")}, []))
    assert out.posted_at.startswith("2026-10-01") and out.date_confidence == DATE_EXACT
    assert out.location == "Berkeley"
    assert out.extra["hiring_organization"] == "MATS Research"


def test_rugged_science_learn_more_buttons_use_the_card_heading():
    url = "https://ruggedscience.com/careers/"
    jobs, _ctx_, _ = _list("ruggedscience.com", url, {url: _read("rugged_listing.html")})
    assert [(j.source_job_id, j.title, j.department) for j in jobs] == [
        ("quality-inspector", "Quality Inspector", "Quality Testing"),
        ("electronics-systems-engineer", "Electronics Systems Engineer", "Engineering"),
        ("sheet-metal-fabricator", "Sheet Metal Fabricator", "Manufacturing")]


def test_orange_logic_invisible_leftover_links_are_not_jobs():
    url = "https://www.orangelogic.com/about/careers"
    jobs, _c, _a = _list("orangelogic.com", url, {url: _read("orangelogic_listing.html")})
    ids = [j.source_job_id for j in jobs]
    assert len(jobs) == 8 and "ai-hacker-orange-logic" not in ids
    assert "delivery-operations-coordinator-orange-logic" not in ids
    # A consultant TITLE at a software company is a job title, nothing more.
    assert "Integrations Consultant, US" in [j.title for j in jobs]


def test_nous_roles_are_listed_and_the_detail_hands_off_to_email():
    url = "https://nousresearch.com/careers"
    jobs, _c, _a = _list("nousresearch.com", url, {url: _read("nous_listing.html")})
    assert [j.source_job_id for j in jobs] == ["software-engineer", "security-engineer",
                                               "office-manager"]
    detail = "https://nousresearch.com/careers/software-engineer"
    out = own_site.OwnSiteCareersProvider().fetch_detail(
        _ep("nousresearch.com", url), jobs[0],
        _ctx({detail: _read("nous_detail_software_engineer.html")}, []))
    assert out.extra["application_method"] == "email"
    assert out.extra["application_email"] == "recruiting@nousresearch.com"
    assert out.extra["manual_handoff"] is True


@pytest.mark.parametrize("fixture,host,url,count", [
    ("gmi_listing.html", "gmicloud.ai", "https://www.gmicloud.ai/en/company/career", 3),
    ("attune_listing.html", "attune.ai", "https://www.attune.ai/company", 2),
])
def test_linkedin_apply_links_are_discovery_with_a_manual_handoff(fixture, host, url, count):
    jobs, _c, asked = _list(host, url, {url: _read(fixture)})
    assert len(jobs) == count
    for job in jobs:
        assert job.source_job_id.startswith("li-")
        assert job.apply_url.startswith("https://www.linkedin.com/jobs/view/")
        assert "?" not in job.apply_url                    # tracking stripped
        assert job.extra["application_method"] == "linkedin"
        assert job.extra["manual_handoff"] is True
        assert job.detail_url == ""
    # LinkedIn is never fetched, not even for a detail.
    out = own_site.OwnSiteCareersProvider().fetch_detail(_ep(host, url), jobs[0],
                                                         _ctx({}, asked))
    assert out is None and asked == [url]


def test_gmi_rows_carry_their_own_location():
    url = "https://www.gmicloud.ai/en/company/career"
    jobs, _c, _a = _list("gmicloud.ai", url, {url: _read("gmi_listing.html")})
    assert jobs[0].title == "Infrastructure Engineer, LLM Inference Optimization"
    assert jobs[0].location == "Mountain View, CA"


def test_longbridge_hydrated_jobs_keep_the_jobs_location_not_the_site_locale():
    url = "https://longbridge.com/hk/en/jobs/974e6b1b-0661-4d31-8c08-b28758e6d139"
    jobs, ctx, _ = _list("longbridge.com", url, {url: _read("longbridge_job.html")})
    sre = [j for j in jobs if j.title == "Site Reliability Engineer"]
    assert sorted((j.location, j.country) for j in sre) == [
        ("US - Dallas, Texas", "United States"), ("US - New York", "United States")]
    assert not any("Hong Kong" in j.location for j in sre)
    first = next(j for j in sre if j.source_job_id.startswith("974e6b1b"))
    assert first.posted_at.startswith("2026-08-19") and first.date_confidence == DATE_EXACT
    assert first.apply_url.startswith("https://longbridgegroup.careers.hibob.com/jobs/")
    assert first.extra["application_method"] == "ats_url"
    assert "Longbridge" in first.description
    assert len({j.source_job_id for j in jobs}) == len(jobs) == 4
    assert ctx.options[own_site.COMPLETENESS_KEY] == own_site.COMPLETE


# ---------------------------------------------------------------------------
# SYNTHETIC pages, one rule each
# ---------------------------------------------------------------------------

GRAPH_LISTING = """<html><body>
<script type="application/ld+json">{"@context":"https://schema.org","@graph":[
 {"@type":"Organization","name":"Acme Robotics"},
 {"@type":["JobPosting","Thing"],"title":"Firmware Engineer","url":"/jobs/firmware-engineer",
  "identifier":{"@type":"PropertyValue","value":"REQ-1042"},"datePosted":"2026-10-14",
  "jobLocation":{"@type":"Place","address":{"addressLocality":"Austin","addressRegion":"TX","addressCountry":"US"}},
  "description":"<p>Write firmware.</p>"},
 {"@type":"JobPosting","title":"Test Technician","datePosted":"2026-10-12",
  "jobLocationType":"TELECOMMUTE","description":"<p>Test boards.</p>"}]}</script>
<div class="empty-state" hidden><p>No jobs found</p></div>
</body></html>"""


def test_json_ld_in_a_graph_with_a_type_list_beside_an_empty_template():
    url = "https://acme-robotics.com/careers"
    jobs, ctx, _ = _list("acme-robotics.com", url, {url: GRAPH_LISTING})
    by_title = {j.title: j for j in jobs}
    assert set(by_title) == {"Firmware Engineer", "Test Technician"}
    fw = by_title["Firmware Engineer"]
    assert fw.source_job_id == "firmware-engineer"
    assert fw.detail_url == "https://acme-robotics.com/jobs/firmware-engineer"
    assert fw.location == "Austin, TX, US" and fw.posted_at.startswith("2026-10-14")
    assert by_title["Test Technician"].location == "Remote"
    assert ctx.options[own_site.COMPLETENESS_KEY] == own_site.COMPLETE


def test_an_empty_template_alone_is_unverified_not_zero():
    url = "https://acme-robotics.com/careers"
    page = '<html><body><div id="app"></div><p>No jobs found</p></body></html>'
    jobs, ctx, _ = _list("acme-robotics.com", url, {url: page})
    assert jobs == []
    assert ctx.options[own_site.COMPLETENESS_KEY] == own_site.UNVERIFIED
    assert ctx.listing_truncated


INLINE = """<html><body><h1>Careers</h1>
<p>Apply by emailing <a href="mailto:jobs@acme-labs.ai?subject=Application">jobs@acme-labs.ai</a>.</p>
<h2 id="research-engineer">Research Engineer</h2><p>Train models. Remote.</p>
<h2>Founding Designer</h2><p>Own the product's look.</p>
<h2>Our Values</h2><p>Be kind.</p>
</body></html>"""


def test_inline_role_sections_with_an_email_application():
    url = "https://acme-labs.ai/careers"
    jobs, _c, _a = _list("acme-labs.ai", url, {url: INLINE})
    assert [(j.source_job_id, j.title) for j in jobs] == [
        ("research-engineer", "Research Engineer"), ("founding-designer", "Founding Designer")]
    job = jobs[0]
    assert job.apply_url == "mailto:jobs@acme-labs.ai"
    assert job.extra["application_method"] == "email"
    assert job.extra["application_email"] == "jobs@acme-labs.ai"
    assert job.extra["manual_handoff"] is True
    assert "Train models" in job.description
    # mailto is never requested
    out = own_site.OwnSiteCareersProvider().fetch_detail(_ep("acme-labs.ai", url), job, _ctx({}, []))
    assert out is None


def test_one_role_employer_with_posting_evidence_is_read():
    url = "https://onerole.com/careers"
    page = ('<div class="job"><h3><a href="/careers/backend-engineer">Backend Engineer</a></h3>'
            '<p>Location: Denver, CO</p><p>Full-time</p><a href="/careers/backend-engineer">Apply</a></div>'
            '<a href="/about">About</a>')
    jobs, _c, _a = _list("onerole.com", url, {url: page})
    assert [j.source_job_id for j in jobs] == ["backend-engineer"]
    assert jobs[0].location == "Denver, CO"


def test_a_lone_link_with_no_posting_evidence_is_still_a_menu_item():
    url = "https://onerole.com/careers"
    page = '<nav><a href="/careers/staff-accountant">Staff Accountant</a><a href="/about">About</a></nav>'
    jobs, ctx, _a = _list("onerole.com", url, {url: page})
    assert jobs == [] and ctx.options[own_site.COMPLETENESS_KEY] == own_site.UNVERIFIED


def _paged(n, total_pages, *, dup=False):
    links = "".join(f'<a href="/jobs/data-engineer-{n}-{i}">Data Engineer {n}.{i}</a>'
                    for i in range(3))
    if dup:
        links += '<a href="/jobs/data-engineer-1-0">Data Engineer 1.0</a>'
    nxt = (f'<a rel="next" href="/careers?page={n + 1}">Next</a>' if n < total_pages else "")
    return f"<html><body>{links}{nxt}</body></html>"


def test_declared_pagination_is_followed_with_duplicates_dropped():
    base = "https://paged.com/careers"
    pages = {base: _paged(1, 3), f"{base}?page=2": _paged(2, 3, dup=True),
             f"{base}?page=3": _paged(3, 3)}
    jobs, ctx, asked = _list("paged.com", base, pages)
    assert asked == [base, f"{base}?page=2", f"{base}?page=3"]
    assert len(jobs) == 9 and len({j.source_job_id for j in jobs}) == 9
    assert ctx.options[own_site.COMPLETENESS_KEY] == own_site.COMPLETE
    assert not ctx.listing_truncated


def test_pagination_past_the_budget_is_budget_limited(monkeypatch):
    monkeypatch.setattr(own_site, "MAX_PAGES", 2)
    base = "https://paged.com/careers"
    pages = {base: _paged(1, 5), f"{base}?page=2": _paged(2, 5), f"{base}?page=3": _paged(3, 5)}
    jobs, ctx, asked = _list("paged.com", base, pages)
    assert len(asked) == 2 and len(jobs) == 6
    assert ctx.options[own_site.COMPLETENESS_KEY] == own_site.BUDGET_LIMITED
    assert ctx.listing_truncated


def test_a_failed_later_page_is_budget_limited_not_complete():
    base = "https://paged.com/careers"
    jobs, ctx, _a = _list("paged.com", base, {base: _paged(1, 2)})
    assert len(jobs) == 3
    assert ctx.options[own_site.COMPLETENESS_KEY] == own_site.BUDGET_LIMITED


def test_a_next_link_off_the_listing_path_is_not_pagination():
    base = "https://paged.com/careers"
    page = _paged(1, 1) + '<a rel="next" href="/blog/page/2">Next</a>'
    jobs, _ctx2, asked = _list("paged.com", base, {base: page})
    assert asked == [base]


@pytest.mark.parametrize("title,hint", [
    ("General Application", "talent_community"),
    ("Expression of Interest", "talent_community"),
    ("Research Fellowship, Summer 2027", "fellowship_or_grant"),
    ("Volunteer Coordinator Application", "volunteer"),
    ("Research Manager", ""),
])
def test_purpose_hints_come_from_the_title(title, hint):
    assert own_site.purpose_hint(title) == hint


@pytest.mark.parametrize("url,method", [
    ("mailto:jobs@x.com", "email"), ("https://www.linkedin.com/jobs/view/123456789", "linkedin"),
    ("https://job-boards.greenhouse.io/acme/jobs/1", "ats_url"),
    ("https://acme.careers.hibob.com/jobs/1", "ats_url"),
    ("https://forms.acme.org/job-application", "form"), ("https://tally.so/r/abc", "form"),
    ("https://acme.com/careers/x", "page"),
])
def test_application_method(url, method):
    assert own_site.application_method_for(url) == method


def test_comeet_plugin_positions_use_the_comeet_uid_as_identity():
    url = "https://www.upwind.io/careers"
    jobs, ctx, _ = _list("upwind.io", url, {url: _read("upwind_comeet_listing.html")})
    assert [j.source_job_id for j in jobs][:1] == ["comeet-ae.e69"]
    # The trimmed fixture keeps one group with one position: a single
    # opening, accepted because its card says "US (Remote) · Full-time".
    assert len(jobs) == 1
    assert jobs[0].title == "Revenue Operations Manager"       # not the meta line
    assert jobs[0].detail_url.endswith("/careers/co/us-remote/AE.E69/revenue-operations-manager/all")
    detail = jobs[0].detail_url
    out = own_site.OwnSiteCareersProvider().fetch_detail(
        _ep("upwind.io", url), jobs[0], _ctx({detail: _read("upwind_comeet_detail.html")}, []))
    assert out.posted_at.startswith("2026-10-21") and out.date_confidence == DATE_EXACT
    assert out.extra["hiring_organization"] == "Upwind Security"
