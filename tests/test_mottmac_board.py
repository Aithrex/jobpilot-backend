"""Replay the public employer JSON and its first-party description page."""
import copy
import json
from pathlib import Path
import pytest

from services.ats_providers.base import FetchContext, ChallengeError
from services.ats_providers.config_boards import ConfigBoardProvider
from services.official_career_registry import CUSTOM_BOARD_ADAPTERS

FIX = Path(__file__).parent / "fixtures/ats_providers/researched_templates"


def provider():
    return ConfigBoardProvider(copy.deepcopy(next(r for r in CUSTOM_BOARD_ADAPTERS if r["key"] == "mottmac")))


def test_real_listing_uses_the_published_requisition_id_and_literal_url():
    p = provider()
    payload = json.loads((FIX / "mottmac_listing.json").read_text())
    ctx = FetchContext(get_json=lambda *_a, **_kw: (200, payload), get_text=None)
    jobs = list(p.list_jobs(p.default_endpoint(), ctx))
    assert len(jobs) == 2 and ctx.listing_truncated
    assert jobs[0].source_job_id == "13569"
    assert jobs[0].apply_url == payload["items"][0]["url"]
    assert jobs[0].title == "Civil Engineer"
    assert jobs[0].location == "Exeter, United Kingdom"
    assert jobs[0].posted_at == "" and jobs[0].date_confidence == "unknown"
    assert jobs[0].employment_type == "Permanent"


def test_configured_html_detail_fills_only_the_published_description():
    p = provider()
    job = p._to_raw(json.loads((FIX / "mottmac_listing.json").read_text())["items"][0])
    asked = []
    def get(url, **_kw):
        asked.append(url)
        return 200, (FIX / "mottmac_detail.html").read_text()
    ctx = FetchContext(get_text=get, get_json=None)
    detail = p.fetch_detail(p.default_endpoint(), job, ctx)
    assert asked == [job.apply_url]
    assert "Mott MacDonald Bentley" in detail.description
    assert len(detail.description) > 1000
    assert detail.posted_at == "" and detail.date_confidence == "unknown"


def test_missing_or_ambiguous_numeric_id_does_not_create_a_posting():
    p = provider()
    record = json.loads((FIX / "mottmac_listing.json").read_text())["items"][0]
    record["url"] = "https://www.mottmac.com/en-us/careers/explore-our-careers/"
    assert p._to_raw(record) is None


@pytest.mark.parametrize("url", [
    "https://unrelated.example/job-13569/",
    "http://www.mottmac.com/en-us/careers/explore-our-careers/civil-engineer-13569/",
    "https://someone@www.mottmac.com/en-us/careers/explore-our-careers/civil-engineer-13569/",
])
def test_unpublished_posting_hosts_and_url_shapes_are_rejected(url):
    p = provider()
    record = json.loads((FIX / "mottmac_listing.json").read_text())["items"][0]
    record["url"] = url
    assert p._to_raw(record) is None


def test_html_challenge_is_not_a_job_description():
    p = provider()
    job = p._to_raw(json.loads((FIX / "mottmac_listing.json").read_text())["items"][0])
    body = '<title>Access denied</title><section class="rich-text__section">Verify you are human</section>'
    ctx = FetchContext(get_text=lambda *_a, **_kw: (200, body), get_json=None)
    with pytest.raises(ChallengeError):
        p.fetch_detail(p.default_endpoint(), job, ctx)


def test_pagination_preserves_the_frontends_reset_and_uuid_contract(monkeypatch):
    p = provider()
    p.page_size = 2
    page = json.loads((FIX / "mottmac_listing.json").read_text())
    page["totalItems"] = 4
    second = copy.deepcopy(page)
    for i, row in enumerate(second["items"]):
        row["url"] = "https://www.mottmac.com/en-us/careers/explore-our-careers/test-role-" + str(20000+i) + "/"
    pages = iter([page, second])
    request_ids = iter(["37957d0b-c02c-472e-897c-f04232f25c13", "d47cd07c-8f19-46e1-9679-ca0d5c7daf6f"])
    monkeypatch.setattr("services.ats_providers.config_boards.uuid.uuid4", lambda: next(request_ids))
    asked = []
    def get(_url, **kw):
        asked.append(kw["params"])
        return 200, next(pages)
    ctx = FetchContext(get_json=get, get_text=None)
    assert len(list(p.list_jobs(p.default_endpoint(), ctx))) == 4
    assert [r["from"] for r in asked] == [0, 2]
    assert len({r["id"] for r in asked}) == 2
    assert all(r["reset"] == "false" and r["country"] == "" for r in asked)
    assert not ctx.listing_truncated
