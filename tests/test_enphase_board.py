"""Replay the employer's published JSON, including incomplete-page safety."""
import copy
import json
from pathlib import Path

import pytest

from services.ats_providers import registry
from services.ats_providers.base import FetchContext, ChallengeError
from services.ats_providers.config_boards import ConfigBoardProvider
from services.official_career_registry import CUSTOM_BOARD_ADAPTERS

FIXTURE = Path(__file__).parent / "fixtures/ats_providers/researched_templates/enphase_page.json"


def provider():
    row = next(row for row in CUSTOM_BOARD_ADAPTERS if row["key"] == "enphase")
    return ConfigBoardProvider(copy.deepcopy(row))


def context(payloads):
    calls = []
    def get(url, **kw):
        calls.append((url, kw))
        return payloads.pop(0)
    return FetchContext(get_json=get, get_text=lambda *_a, **_kw: (404, "")), calls


def test_real_employer_json_maps_requisitions_without_inventing_dates():
    p = provider()
    page = json.loads(FIXTURE.read_text())
    page["pager"]["total_items"] = "2"
    ctx, calls = context([(200, page)])
    jobs = list(p.list_jobs(p.default_endpoint(), ctx))
    assert len(jobs) == 2
    assert jobs[0].source_job_id == "o9WQAfwf"
    assert jobs[0].title == "Senior Power Electronics Engineer"
    assert jobs[0].location == "Petaluma, CA, United States"
    assert jobs[0].apply_url == "https://enphase.com/job/o9WQAfwf"
    assert "power" in jobs[0].description.lower()
    assert "<span" not in jobs[0].description and "<p" not in jobs[0].description
    assert jobs[0].posted_at == "" and jobs[0].date_confidence == "unknown"
    assert not ctx.listing_truncated
    assert calls[0][1]["params"]["page"] == 0
    assert registry.readiness("custom_enphase")["state"] == "ready"


def test_short_page_below_published_total_is_partial():
    p = provider()
    ctx, _ = context([(200, json.loads(FIXTURE.read_text()))])
    assert len(list(p.list_jobs(p.default_endpoint(), ctx))) == 2
    assert ctx.listing_truncated


@pytest.mark.parametrize("status", [403, 405, 429])
def test_refusals_are_not_empty_boards(status):
    p = provider()
    ctx, _ = context([(status, {})])
    with pytest.raises(ChallengeError):
        list(p.list_jobs(p.default_endpoint(), ctx))
    assert ctx.listing_truncated


def test_repeated_page_cannot_prove_the_reported_total():
    p = provider()
    p.page_size = 2
    page = json.loads(FIXTURE.read_text())
    page["pager"]["total_items"] = "4"
    ctx, _ = context([(200, page), (200, page)])
    jobs = list(p.list_jobs(p.default_endpoint(), ctx))
    assert len(jobs) == 2
    assert ctx.listing_truncated


@pytest.mark.parametrize("payload", [{}, {"rows": None}, {"rows": [None]}])
def test_reshaped_response_without_a_total_is_partial(payload):
    p = provider()
    p.records_path = "rows"
    p.total_path = ""
    ctx, _ = context([(200, payload)])
    assert list(p.list_jobs(p.default_endpoint(), ctx)) == []
    assert ctx.listing_truncated


def test_transport_exception_preserves_partial_listing_state():
    p = provider()
    def fail(*_a, **_kw):
        raise TimeoutError("bounded transport timed out")
    ctx = FetchContext(get_json=fail, get_text=lambda *_a, **_kw: (404, ""))
    with pytest.raises(TimeoutError):
        list(p.list_jobs(p.default_endpoint(), ctx))
    assert ctx.listing_truncated


def test_overlapping_short_page_without_total_is_partial():
    p = provider()
    p.total_path = ""
    p.page_size = 4
    page = json.loads(FIXTURE.read_text())
    page["rows"].append(copy.deepcopy(page["rows"][0]))
    ctx, _ = context([(200, page)])
    assert len(list(p.list_jobs(p.default_endpoint(), ctx))) == 2
    assert ctx.listing_truncated


@pytest.mark.parametrize("terminal_empty", [False, True])
def test_earlier_overlap_without_total_cannot_be_erased_by_final_page(terminal_empty):
    p = provider()
    p.total_path = ""
    p.page_size = 2
    page = json.loads(FIXTURE.read_text())
    second = copy.deepcopy(page)
    second["rows"][0]["jid"] = "third"
    last = copy.deepcopy(page)
    last["rows"] = [] if terminal_empty else [dict(last["rows"][0], jid="fourth")]
    ctx, _ = context([(200, page), (200, second), (200, last)])
    assert len(list(p.list_jobs(p.default_endpoint(), ctx))) == (3 if terminal_empty else 4)
    assert ctx.listing_truncated


def test_expired_context_sends_no_preflight_request():
    p = provider()
    p.preflight_url = "https://enphase.com/example"
    ctx, calls = context([(200, {})])
    ctx.deadline = 0
    assert list(p.list_jobs(p.default_endpoint(), ctx)) == []
    assert ctx.listing_truncated
    assert calls == []
