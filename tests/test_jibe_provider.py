"""iCIMS Jibe public search. Fixtures trimmed from dish.jibeapply.com (EchoStar),
2026-10-22."""
import json
from pathlib import Path

import pytest

from services.ats_providers import registry
from services.ats_providers.base import DATE_EXACT, ChallengeError, Endpoint, FetchContext
from services.ats_providers.jibe import JibeProvider

FIX = Path(__file__).parent / "fixtures" / "ats_providers" / "jibe"
API = "https://dish.jibeapply.com/api/jobs"


def _page(name):
    return json.loads((FIX / name).read_text(encoding="utf8"))


def _ctx(pages, asked, cutoff=None):
    def get_json(url, **kwargs):
        params = dict(kwargs.get("params") or {})
        asked.append((url, params))
        return pages.get(params.get("page"), (200, {"jobs": [], "totalCount": 0}))
    return FetchContext(get_json=get_json, get_text=None, cutoff_iso=cutoff)


def _ep():
    return Endpoint(vendor="jibe", tenant="dish", host="dish.jibeapply.com")


@pytest.mark.parametrize("url", ["https://dish.jibeapply.com/jobs",
                                 "https://assets.jibecdn.com/prod/dish/0.0.144/assets/"])
def test_the_branded_frontend_names_tenant_dish(url):
    provider, fields, _ = registry.match_url(url)
    assert provider.endpoint_from_match(fields, url).tenant == "dish"


def test_app_cdn_is_not_a_tenant():
    hit = registry.match_url("https://app.jibecdn.com/prod/search/4.11.225/main.js")
    assert hit is None or hit[0].vendor != "jibe"


def test_listing_pages_the_public_search_and_keeps_icims_apply_identity():
    p1, p2 = _page("jobs_page1.json"), _page("jobs_page2.json")
    # Make the fixture look like a complete 13-row board: 10 + 3.
    rows = (p1["jobs"] * 4)[:10]
    for i, row in enumerate(rows):
        row = json.loads(json.dumps(row))
        row["data"]["req_id"] = f"9{i:04d}"
        rows[i] = row
    page1 = {"jobs": rows, "totalCount": 13}
    page2 = {"jobs": p2["jobs"] + p1["jobs"][:1], "totalCount": 13}
    asked = []
    ctx = _ctx({1: (200, page1), 2: (200, page2)}, asked)
    jobs = list(JibeProvider().list_jobs(_ep(), ctx))
    assert [a[1] for a in asked] == [{"page": 1, "internal": "false"},
                                     {"page": 2, "internal": "false"}]
    assert all(a[0] == API for a in asked)
    assert len(jobs) == 13 and not ctx.listing_truncated
    first = next(j for j in jobs if j.source_job_id == p1["jobs"][0]["data"]["req_id"])
    assert first.apply_url.startswith("https://attract-careers1-echostar.icims.com/jobs/")
    assert first.extra["hiring_organization"] == "EchoStar"
    assert first.extra["jibe_client"] == "dish"
    assert first.date_confidence == DATE_EXACT and first.posted_at.startswith("2026-10-21")
    assert first.description


def test_a_short_read_is_truncated_not_complete():
    p1 = _page("jobs_page1.json")
    ctx = _ctx({1: (200, p1)}, [])
    jobs = list(JibeProvider().list_jobs(_ep(), ctx))
    assert len(jobs) == 3 and ctx.listing_truncated     # 3 of a reported 656


def test_newest_first_stops_after_a_stale_page():
    old = json.loads(json.dumps(_page("jobs_page1.json")))
    for row in old["jobs"]:
        row["data"]["posted_date"] = "2025-01-01T00:00:00+0000"
    full_old = {"jobs": (old["jobs"] * 4)[:10], "totalCount": 999}
    for i, row in enumerate(full_old["jobs"]):
        full_old["jobs"][i] = json.loads(json.dumps(row))
        full_old["jobs"][i]["data"]["req_id"] = f"8{i:04d}"
    asked = []
    pages = {n: (200, json.loads(json.dumps(full_old).replace('"8', f'"{n}'))) for n in range(1, 6)}
    jobs = list(JibeProvider().list_jobs(_ep(), _ctx(pages, asked, cutoff="2026-10-01T00:00:00+00:00")))
    assert jobs == [] and len(asked) == 2


@pytest.mark.parametrize("response", [(403, None), (None, None), (200, {"results": []})])
def test_an_unread_or_reshaped_board_is_unverified(response):
    ctx = _ctx({1: response}, [])
    assert list(JibeProvider().list_jobs(_ep(), ctx)) == [] and ctx.listing_truncated


def test_a_challenge_raises():
    with pytest.raises(ChallengeError):
        list(JibeProvider().list_jobs(_ep(), _ctx({1: (429, None)}, [])))
