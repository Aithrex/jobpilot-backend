"""Openings an employer lists on its own careers page. Deliberately narrow:
read loosely, the same pages yield "Executive Team" and product pages as jobs
(measured on 200 H-1B sponsors, 2026-10-08)."""
import pytest

from services import job_ingest_worker as w
from services.ats_providers import own_site
from services.ats_providers.base import Endpoint, FetchContext, RawJob

HOST = "hectorsystems.com"
PAGE = "https://hectorsystems.com/careers/"
LISTING = """
<html><body><nav><a href="/careers/">Careers</a><a href="/about/">About us</a></nav>
<a href="/jobs/manual-tester/">Manual Tester</a>
<a href="/jobs/senior-software-developer/">Senior Software Developer</a>
<a href="https://hectorsystems.com/jobs/java-developer/">Java Developer</a>
<a href="/Careers/Executive-Management/">Executive Management</a>
<a href="/products/ai-supervisor-assist">AI Supervisor Assist</a>
<a href="https://www.linkedin.com/jobs/view/123">Data Engineer</a>
<a href="/jobs/benefits-overview/">Benefits Overview</a>
<a href="mailto:hr@hectorsystems.com">Recruiter</a>
</body></html>"""


def test_only_same_site_job_paths_with_a_job_title_are_postings():
    links = own_site.job_links(LISTING, PAGE, HOST)
    assert [title for _url, title in links] == [
        "Manual Tester", "Senior Software Developer", "Java Developer"]
    assert all(url.startswith("https://hectorsystems.com/jobs/") for url, _ in links)


@pytest.mark.parametrize("title,ok", [
    ("Senior Software Developer", True), ("Registered Nurse (RN)", True),
    ("Entry-Level IGA Analyst", True), ("Staff Accountant", True),
    ("Executive Management", False), ("Executive Team", False),
    ("Sales Team", False), ("Careers at Acme", False), ("Analyst Coverage", False),
    ("Meet our Engineers and Developer Relations", False),
    ("sales@girikon.com", False), ("Apply", False),
    ("THINK ALL PURE LEAD BATTERIES ARE THE SAME AS THE OTHERS THINK AGAIN", False),
])
def test_what_reads_as_a_job_title(title, ok):
    assert own_site.looks_like_a_job_title(title) is ok


def test_one_match_on_a_page_is_not_a_listing():
    page = '<a href="/careers/staff-accountant">Staff Accountant</a><a href="/about">About</a>'
    assert own_site.job_links(page, "https://acme.com/careers", "acme.com") == []


def test_discovery_picks_the_page_that_lists_the_most():
    endpoint = own_site.endpoint_for_pages("www.hectorsystems.com", [
        ("https://hectorsystems.com/", "<a href='/careers/'>Careers</a>"), (PAGE, LISTING)])
    assert (endpoint.vendor, endpoint.tenant, endpoint.host) == (
        "careers_page", HOST, HOST)
    assert endpoint.extra == {"listing_url": PAGE}
    assert own_site.endpoint_for_pages(HOST, [("https://hectorsystems.com/", "<p>hi</p>")]) is None


def _endpoint(url=PAGE):
    return Endpoint(vendor="careers_page", tenant=HOST, host=HOST, extra={"listing_url": url})


def _ctx(pages, asked):
    def get_text(url, **kwargs):
        asked.append(url)
        return (200, pages[url]) if url in pages else (404, "")
    return FetchContext(get_json=None, get_text=get_text)


def test_listing_and_detail():
    posting = ("<html><nav>Home Careers</nav><main><h1>Java Developer</h1><p>"
               + "Responsibilities: build services in Java and Spring. " * 12
               + "</p><p>Apply by email.</p></main><footer>(c) Hector</footer></html>")
    asked = []
    ctx = _ctx({PAGE: LISTING, "https://hectorsystems.com/jobs/java-developer/": posting}, asked)
    provider = own_site.OwnSiteCareersProvider()
    jobs = list(provider.list_jobs(_endpoint(), ctx))
    assert [j.source_job_id for j in jobs] == [
        "manual-tester", "senior-software-developer", "java-developer"]
    assert jobs[2].posted_at == "" and jobs[2].apply_url.endswith("/jobs/java-developer/")
    out = provider.fetch_detail(_endpoint(), jobs[2], ctx)
    assert "build services in Java" in out.description
    assert "Home Careers" not in out.description and "(c) Hector" not in out.description


def test_a_page_that_does_not_read_as_a_posting_is_not_described_as_one():
    asked = []
    ctx = _ctx({"https://hectorsystems.com/jobs/x-engineer/":
                "<html><main>" + "Our company was founded in 1999. " * 20 + "</main></html>"}, asked)
    job = RawJob(source_job_id="x", title="X Engineer",
                 apply_url="https://hectorsystems.com/jobs/x-engineer/")
    assert own_site.OwnSiteCareersProvider().fetch_detail(_endpoint(), job, ctx) is None


def test_an_unread_page_is_a_truncated_listing_not_an_empty_board():
    asked = []
    ctx = _ctx({}, asked)
    assert list(own_site.OwnSiteCareersProvider().list_jobs(_endpoint(), ctx)) == []
    assert ctx.listing_truncated is True


def test_nothing_off_the_employers_host_is_ever_requested():
    asked = []
    ctx = _ctx({}, asked)
    provider = own_site.OwnSiteCareersProvider()
    assert list(provider.list_jobs(_endpoint("https://evil.example/careers/"), ctx)) == []
    job = RawJob(source_job_id="x", title="T", apply_url="https://evil.example/jobs/t-engineer/")
    assert provider.fetch_detail(_endpoint(), job, ctx) is None
    assert asked == []


class TestTheCrawlerPinsTheMemberToItsOwnHost:
    MEMBER = {"vendor": "careers_page", "tenant": HOST, "host": HOST, "source": "career-pages"}

    def test_only_careers_page_members_are_pinned(self):
        assert w._own_site_host(self.MEMBER) == HOST
        assert w._own_site_host({"vendor": "greenhouse", "tenant": "acme", "host": "x.com"}) == ""
        assert w._own_site_host(dict(self.MEMBER, host="not a host")) == ""

    @pytest.mark.parametrize("url", [
        "https://evil.example/jobs/a", "http://hectorsystems.com/jobs/a",
        "https://hectorsystems.com.evil.example/jobs/a", "https://nothectorsystems.com/x",
        "file:///etc/passwd"])
    def test_other_hosts_and_schemes_are_refused(self, url):
        with pytest.raises(ValueError):
            w.assert_own_site_url(HOST, url)

    def test_its_own_host_is_allowed_when_public(self, monkeypatch):
        from services import jd_fetcher
        monkeypatch.setattr(jd_fetcher, "url_is_safe", lambda url: (True, ""))
        for url in ("https://hectorsystems.com/jobs/a", "https://www.hectorsystems.com/careers",
                    "https://jobs.hectorsystems.com/x"):
            assert w.assert_own_site_url(HOST, url) == url
        monkeypatch.setattr(jd_fetcher, "url_is_safe", lambda url: (False, "private"))
        with pytest.raises(ValueError):
            w.assert_own_site_url(HOST, "https://hectorsystems.com/jobs/a")


def test_button_text_inside_the_link_is_not_part_of_the_title():
    page = ('<a href="/jobs/software-engineer/">Software Developers/Software Engineer More Details</a>'
            '<a href="/jobs/devops-engineer/">DevOps Engineer - Apply Now</a>')
    titles = [t for _u, t in own_site.job_links(page, "https://galey.com/careers", "galey.com")]
    assert titles == ["Software Developers/Software Engineer", "DevOps Engineer"]


class TestAServerIsNotAlwaysAWaiter:
    """Every "SQL Server Developer" and "Windows Server Administrator" was
    filed under hospitality."""

    @pytest.mark.parametrize("title,family", [
        ("SQL Server DBA", "data_engineering"), ("SQL Server Developer", "data_engineering"),
        ("Sr.DBA (MSSQL)", "data_engineering"), ("Database Administrator", "data_engineering"),
        ("Oracle DBA", "data_engineering"),
        ("Windows Server Administrator", None), ("Linux Server Engineer", None),
        # Still a waiter.
        ("Server", "hospitality"), ("Restaurant Server", "hospitality"),
        ("Banquet Server", "hospitality"), ("Lead Server", "hospitality"),
        ("Food Server - Web Cafe", "hospitality"),
        # And not everything with "database" in it is a DBA.
        ("Database Marketing Manager", "marketing"),
        ("Senior Software Engineer, Database", "software_engineering"),
    ])
    def test_families(self, title, family):
        from services import fit_score
        assert fit_score.classify_role(title) == family


class TestTheSiteMustBeTheEmployers:
    """ONE GLOBAL INFO SYSTEMS LLC was matched to one.com, and one.com's
    openings were then listed as theirs (seen live, 2026-10-08)."""

    @pytest.mark.parametrize("name,host,page,ok", [
        ("GALEY SYSTEMS INC", "galeysystems.com", "", True),                  # name spells the domain
        ("SOOTHSAYER ANALYTICS LLC", "www.soothsayeranalytics.com", "", True),
        ("OPENDOOR LABS INC", "opendoor.com", "<p>Sell your home</p>", True),  # distinctive first word
        ("CYBERTECH SYSTEMS & SOFTWARE INC", "cybertech.com",
         "<footer>&copy; CyberTech Systems and Software, Inc.</footer>", True),  # named as a phrase
        ("ONE GLOBAL INFO SYSTEMS LLC", "one.com", "<title>one.com - domains and hosting</title>", False),
        ("ALPHA TECHNOLOGIES SERVICES INC", "alpha.com", "<p>Alpha batteries by EnerSys</p>", False),
        ("STRATUS TECHNOLOGIES INC", "stratus.com", "<p>SMART Modular Stratus servers</p>", True),
        ("BRONX RIVER ALLIANCE", "bronx.com", "<p>The Bronx news and events</p>", False),
        ("AIVRES SYSTEMS INC.", "aivres.com", "<p>AI servers</p>", True),
        ("4WALL AI INC.", "4wall.com", "<p>4Wall Entertainment lighting rental</p>", False),
        ("GLOBAL DATA PARTNERS LLC", "global.com", "<p>Global payments</p>", False),
        ("", "acme.com", "", False),
    ])
    def test_evidence(self, name, host, page, ok):
        from services import career_portal_discovery as d
        assert d.site_is_the_employers(name, host, [page]) is ok
