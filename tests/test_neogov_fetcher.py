"""NeoGov / GovernmentJobs.com fetcher.

The US public sector - states, counties, cities - reached through the agency
board's own job-list call. `docs/workday-tenant-expansion.md` §6.4 called this
the biggest unclaimed vein, and it is heavily NON-TECH, which is the market the
tech-only catalogs do not reach at all.

These tests pin the three things that were genuinely easy to get wrong, each of
which was found against live payloads rather than imagined:

  1. the human title is `Classification`; `JobTitle` is a URL slug
  2. dates are MM/DD/YY and an unparseable one must NOT become "today"
  3. there is no URL field - the apply link is built from the ID

No network: the payloads below are trimmed copies of real responses.
"""
import pytest

from services import career_pages_service as cps

AGENCY = {"display_name": "City of Seattle", "ats": "neogov",
          "slug": "seattle", "industry": "government"}

# Shape and field names copied from a live response (2026-08-05).
SAMPLE = {
    "success": True,
    "jobList": [
        {
            "ID": "5331580",
            "JobTitle": "utility-rate-analyst-senior-economist-senior",
            "Classification": "Utility Rate Analyst, Senior (Economist, Senior)",
            "PostingDate": "05/05/26",
            "Location": "Seattle, WA",
            "SalaryInfo": "$60.55 - $70.40 Hourly",
            "JobType": "Classified Civil Service, Regular, Full-Time",
            "DepartmentName": "Seattle Public Utilities",
            "FullDescription": "Priority review will be given to early applications.",
            "RemoteWorkOptionText": "",
        },
    ],
}


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


@pytest.fixture()
def posted(monkeypatch):
    """Capture the outbound call and serve a canned payload."""
    calls = {}

    def _post(url, data=None, headers=None, timeout=None):
        calls.update(url=url, data=data, headers=headers or {})
        return _Resp(SAMPLE)

    monkeypatch.setattr(cps._SESSION, "post", _post)
    return calls


def test_the_human_title_comes_from_classification(posted):
    """`JobTitle` is a URL slug. Using it would put
    "utility-rate-analyst-senior-economist-senior" on every card AND wreck
    title-based role classification downstream."""
    rows = cps.FETCHERS["neogov"](AGENCY)
    assert rows[0]["title"] == "Utility Rate Analyst, Senior (Economist, Senior)"


def test_the_apply_link_is_built_from_the_id(posted):
    """The payload carries no URL at all."""
    rows = cps.FETCHERS["neogov"](AGENCY)
    assert rows[0]["apply_link"] == \
        "https://www.governmentjobs.com/careers/seattle/jobs/5331580"


def test_the_mm_dd_yy_date_is_parsed(posted):
    rows = cps.FETCHERS["neogov"](AGENCY)
    assert rows[0]["date_posted"].startswith("2026-05-05")


@pytest.mark.parametrize("bad", ["", None, "not a date", "2026-05-05", "13/45/99"])
def test_an_unreadable_date_is_left_unknown_never_today(monkeypatch, bad):
    """ADR-001: an unknown posting date is never replaced with now(). Inventing
    one here would re-date an old requisition as "posted today", which is the
    single largest source of fake freshness the crawler already guards against
    for Greenhouse."""
    payload = {"jobList": [dict(SAMPLE["jobList"][0], PostingDate=bad)]}
    monkeypatch.setattr(cps._SESSION, "post",
                        lambda *a, **k: _Resp(payload))
    rows = cps.FETCHERS["neogov"](AGENCY)
    assert rows[0]["date_posted"] == ""
    assert rows[0]["days_ago"] is None


def test_the_xhr_header_is_sent(posted):
    """Without it the endpoint 404s, which reads as "no such agency" rather
    than "you asked wrong" - the reason this vendor was first written off as
    unfetchable. Measured: the BODY format does not matter, this header does."""
    cps.FETCHERS["neogov"](AGENCY)
    assert posted["headers"].get("X-Requested-With") == "XMLHttpRequest"
    assert "loadJobsOnMaps" in posted["url"]


def test_rows_without_an_id_or_title_are_dropped(monkeypatch):
    payload = {"jobList": [
        {"ID": "1", "Classification": ""},          # no title
        {"Classification": "Analyst"},               # no id
        SAMPLE["jobList"][0],                        # good
    ]}
    monkeypatch.setattr(cps._SESSION, "post", lambda *a, **k: _Resp(payload))
    assert len(cps.FETCHERS["neogov"](AGENCY)) == 1


def test_an_empty_board_is_not_an_error(monkeypatch):
    monkeypatch.setattr(cps._SESSION, "post", lambda *a, **k: _Resp({"jobList": []}))
    assert cps.FETCHERS["neogov"](AGENCY) == []


def test_salary_from_the_board_parses_with_the_shared_normalizer():
    """SalaryInfo arrives as a rendered string. It has to survive the same
    normalizer the inventory uses, or the salary filter cannot see it."""
    from services import salary_normalizer as sal
    comp = sal.parse(SAMPLE["jobList"][0]["SalaryInfo"])
    assert comp["period"] == "hour"
    assert comp["annual_min"] == 60.55 * 2080


def test_the_catalog_rows_are_wired_to_a_real_fetcher():
    from services.career_pages_catalog import CAREER_PAGES_CATALOG

    rows = [r for r in CAREER_PAGES_CATALOG if r["ats"] == "neogov"]
    assert rows, "the verified government agencies must stay in the catalog"
    assert "neogov" in cps.FETCHERS
    for row in rows:
        assert row["industry"] == "government"
        assert row["slug"] and " " not in row["slug"]


def test_the_crawler_is_allowed_to_reach_governmentjobs():
    """A catalog row with no matching outbound allowlist entry is a row that
    silently fetches nothing in production."""
    from services import job_ingest_worker as worker

    url = "https://www.governmentjobs.com/careers/seattle/home/loadJobsOnMaps"
    assert worker.host_allowed(worker.SOURCE_CAREER, url)
    assert worker.probe_host_allowed(
        worker.SOURCE_CAREER,
        "https://www.governmentjobs.com/careers/seattle/jobs/5331580")


def test_the_live_path_shows_the_same_salary_as_the_inventory_path(posted):
    """Both serving paths must agree. Only the inventory path carried salary at
    first, so the same job showed a pay band on one and nothing on the other -
    which reads as a bug rather than as a missing field."""
    rows = cps.FETCHERS["neogov"](AGENCY)
    assert rows[0]["salary"] == "$60.55 - $70.40 an hour"


def test_no_published_salary_is_an_empty_string_not_a_guess(monkeypatch):
    payload = {"jobList": [dict(SAMPLE["jobList"][0], SalaryInfo="",
                                FullDescription="Great team, founded 2019.")]}
    monkeypatch.setattr(cps._SESSION, "post", lambda *a, **k: _Resp(payload))
    assert cps.FETCHERS["neogov"](AGENCY)[0]["salary"] == ""


# ---------------------------------------------------------------------------
# Wiring. Each of these was a real gap found only by asking "does the crawler
# actually reach these rows?" - the fetcher and the catalog rows can both be
# perfect while the source still returns nothing in production.
# ---------------------------------------------------------------------------

def test_every_catalog_ats_is_actually_crawlable():
    """`_load_career_members` skips any catalog row whose ATS is not registered
    as termless. A row it skips is never crawled, so the source silently
    contributes zero postings while every test still passes."""
    from services.career_pages_catalog import CAREER_PAGES_CATALOG
    from services import job_ingest_worker as worker

    catalog_ats = {r["ats"] for r in CAREER_PAGES_CATALOG}
    missing = catalog_ats - set(worker._TERMLESS_ATS)
    assert not missing, f"in the catalog but never crawled: {sorted(missing)}"


def test_the_government_agencies_survive_registry_loading():
    """The end-to-end version of the test above: the rows must come out the
    other side of the registry, not just be present in the catalog file."""
    from services import job_ingest_worker as worker

    worker.reset_registry_cache()
    members = worker.registry_members(worker.SOURCE_CAREER)
    neogov = [m for m in members if m.get("ats") == "neogov"]
    assert len(neogov) >= 10, f"only {len(neogov)} government agencies reached the crawler"


def test_the_feed_labels_government_rows_correctly():
    """The feed keeps its OWN label map, so a source added to the live-crawl
    map alone shows up as a generic "Career Page" on the main feed."""
    from services import inventory_feed as feed
    assert feed._ATS_LABEL.get("neogov") == "GovernmentJobs"
