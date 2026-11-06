"""The newly proven public boards are schedulable, with distinct member identities."""
import runpy
from pathlib import Path

import pytest

from services import job_ingest_worker as jw
from services.ats_providers import registry
from services.ats_providers.base import Endpoint

ROOT = Path(__file__).resolve().parents[1]
PLAN = runpy.run_path(str(ROOT / "scripts/data/direct_employer_plan.py"))
NAMES = (
    "LendingTree", "Verisk", "Riot Games", "DigitalOcean", "DataVisor",
    "Pydantic", "Procore", "Etsy", "Boston Consulting Group", "Supermicro",
    "Kiewit", "TSMC", "Teradyne", "NRG Energy", "Two Sigma", "Electronic Arts",
    "Deloitte", "Enphase Energy", "Oxide Computer", "KPMG US", "Deel", "Shopify", "EY",
    "ThoughtSpot", "Mott MacDonald",
)


@pytest.mark.parametrize("name", NAMES)
def test_proven_plan_endpoint_is_a_schedulable_member(name, monkeypatch):
    entry = PLAN["LEAD_PLAN"].get(name) or PLAN["TOP_EMPLOYER_PLAN"][name]
    assert entry["plan"] == "added" and entry["checked_at"].startswith("2026-11-")
    endpoint = Endpoint(**entry["endpoint"])
    members = list(jw._load_career_members())
    if not any(m["member_key"] == endpoint.member_key for m in members):
        # Registry-native seeds are loaded separately from legacy board catalogs.
        members += list(jw._load_members(jw.source_of_vendor(endpoint.vendor)))
    assert any(m["member_key"] == endpoint.member_key for m in members)
    board = registry.get(endpoint.vendor).board_url(endpoint)
    assert board.startswith("https://")
    if endpoint.vendor == "careers_page":
        monkeypatch.setattr("services.jd_fetcher.url_is_safe", lambda _url: (True, "public fixture"))
        member = next(m for m in members if m["member_key"] == endpoint.member_key)
        assert jw.assert_own_site_url(jw._own_site_host(member), board) == board
        with pytest.raises(ValueError):
            jw.assert_own_site_url(jw._own_site_host(member), "https://unrelated.example/jobs")
    elif endpoint.vendor == "greenhouse":
        assert jw.host_allowed(jw.source_of_vendor(endpoint.vendor),
                               "https://boards-api.greenhouse.io/v1/boards/" + endpoint.tenant + "/jobs")
    else:
        assert jw.host_allowed(jw.source_of_vendor(endpoint.vendor), board)


def test_onboarding_uses_the_crawlers_own_site_guard(monkeypatch):
    import sys
    sys.path.insert(0, str(ROOT / "scripts"))
    import onboard_researched_companies as ob
    from services.ats_providers.base import FetchContext
    checked = []
    def offline_context(**kwargs):
        checked.append(kwargs["guard"]("careers_page", "https://www.deel.com/careers/"))
        with pytest.raises(ValueError):
            kwargs["guard"]("careers_page", "https://unrelated.example/jobs")
        return FetchContext(get_text=lambda *_a, **_kw: (200, ""),
                            get_json=lambda *_a, **_kw: (200, {}))
    monkeypatch.setattr("services.jd_fetcher.url_is_safe", lambda _url: (True, "public fixture"))
    monkeypatch.setattr("services.ats_providers.http.simple_context", offline_context)
    ob.validate_endpoint({"vendor": "careers_page", "tenant": "deel.com", "host": "deel.com",
                          "extra": {"listing_url": "https://www.deel.com/careers/"}})
    assert checked == ["https://www.deel.com/careers/"]


def test_deloitte_career_paths_keep_distinct_member_keys():
    rows = [row for row in jw._SEED_ENDPOINTS if row.get("display_name") == "Deloitte"]
    assert len(rows) == 2
    endpoints = [Endpoint(**{k: v for k, v in row.items() if k != "industry"}) for row in rows]
    assert len({e.member_key for e in endpoints}) == 2
    assert {e.site for e in endpoints} == {
        "en_US/careers?3_5_3=477%2C478%2C480&sort=relevancy",
        "en_US/careers?3_5_3=479&sort=relevancy",
    }


def test_every_researched_employer_is_present_in_owner_onboarding_report():
    import sys
    sys.path.insert(0, str(ROOT / "scripts"))
    import onboard_researched_companies as ob

    report = ob.build_report(validations={})
    represented = {row["company"] for cohort in report["cohorts"].values()
                   for row in cohort["rows"]}
    assert set(NAMES) <= represented, sorted(set(NAMES) - represented)
