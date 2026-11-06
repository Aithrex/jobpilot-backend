"""Public Stantec NLX samples captured 2026-11-05; all transports are offline."""
import copy
import json
from dataclasses import replace
from pathlib import Path
import pytest
from services.ats_providers import registry
from services.ats_providers.base import ChallengeError, Endpoint, FetchContext

FIX = Path(__file__).parent / 'fixtures/ats_providers/directemployers_nlx'
API = 'https://prod-search-api.jobsyn.org/api/v1/solr/search'
BOARD = 'https://stantec.jobs/locations/usa/jobs/'

def fixture(n):
    return json.loads((FIX / f'page{n}.json').read_text(encoding='utf8'))

def provider():
    p = registry.get('directemployers_nlx')
    assert p is not None, 'Public NLX reader must be registered'
    return p

def context(*, pages=None, status=200, robots='User-agent: *\nDisallow:\n'):
    asked = []
    pages = pages or [fixture(1), fixture(2)]
    def text(url, **kwargs):
        asked.append(url)
        assert url in ('https://stantec.jobs/robots.txt', 'https://prod-search-api.jobsyn.org/robots.txt')
        return 200, robots
    def get(url, **kwargs):
        asked.append((url, kwargs))
        assert url == API
        params = kwargs.get('params') or {}
        assert params['location'] == 'usa' and params['num_items'] == 50
        assert 'offset' not in params
        assert kwargs['extra_headers']['X-Origin'] == 'stantec.jobs'
        return status, copy.deepcopy(pages[min(int(params['page']) - 1, len(pages) - 1)])
    return FetchContext(get_text=text, get_json=get), asked

def endpoint():
    return Endpoint(vendor='directemployers_nlx', tenant='stantec', host='stantec.jobs', site='usa')

def test_real_pages_merge_locations_by_requisition_and_preserve_content():
    p = provider(); ctx, asked = context()
    jobs = list(p.list_jobs(endpoint(), ctx))
    assert len(jobs) == 4 and len({j.source_job_id for j in jobs}) == 4
    repeated = next(j for j in jobs if len(j.extra.get('locations', [])) == 3)
    assert all(x.endswith('United States') for x in repeated.extra['locations'])
    assert all(j.description and j.title and j.country == 'United States' for j in jobs)
    assert all(j.apply_url.startswith('https://stantec.jobs/') and j.apply_url.endswith('/job/') for j in jobs)
    assert all(j.posted_at and j.date_confidence == 'exact' for j in jobs)
    assert not ctx.listing_truncated
    assert len([x for x in asked if isinstance(x, tuple)]) == 2
    assert p.board_url(endpoint()) == BOARD
    assert registry.readiness(p.vendor)['state'] == 'ready'

def test_only_proven_scoped_employer_board_fingerprints():
    p = provider()
    match = registry.match_url(BOARD)
    assert match and match[0].vendor == p.vendor
    for url in ('https://prod-search-api.jobsyn.org/api/v1/solr/search', 'https://burnsmcd.jobs/jobs/', 'https://unknown.jobs/locations/usa/jobs/'):
        match = registry.match_url(url)
        assert not match or match[0].vendor != p.vendor

def test_capped_read_marks_incomplete_before_yield(monkeypatch):
    p = provider(); monkeypatch.setattr(p, 'policy', replace(p.policy, max_pages=1))
    ctx, _ = context(); stream = p.list_jobs(endpoint(), ctx)
    assert next(stream)
    assert ctx.listing_truncated

def test_deadline_and_unproven_host_never_request():
    p = provider(); ctx, asked = context(); ctx.deadline = 0
    assert list(p.list_jobs(endpoint(), ctx)) == [] and ctx.listing_truncated and not asked
    ctx, asked = context(); bad = endpoint(); bad.host = 'unverified.jobs'
    assert list(p.list_jobs(bad, ctx)) == [] and not asked

@pytest.mark.parametrize('field,value', [('company_exact','Unrelated employer'),('country_exact','Canada')])
def test_mixed_entity_or_country_is_blocked(field, value):
    p = provider(); first = fixture(1); first['jobs'][0][field] = value
    ctx, _ = context(pages=[first])
    with pytest.raises(ChallengeError): list(p.list_jobs(endpoint(), ctx))
    assert ctx.listing_truncated

@pytest.mark.parametrize('status', [403,405,429])
def test_refusal_is_blocked_never_empty(status):
    p = provider(); ctx, _ = context(status=status)
    assert p.validate(endpoint(), ctx).status == 'blocked'

def test_robots_prohibition_prevents_api_request():
    p = provider(); ctx, asked = context(robots='User-agent: *\nDisallow: /\n')
    assert p.validate(endpoint(), ctx).status == 'blocked'
    assert not any(isinstance(x, tuple) for x in asked)

def test_repeated_page_or_unread_total_is_incomplete():
    p = provider(); ctx, asked = context(pages=[fixture(1)])
    assert list(p.list_jobs(endpoint(), ctx))
    assert ctx.listing_truncated
    assert len([x for x in asked if isinstance(x, tuple)]) == 2
    short = fixture(1); short['pagination']['has_more_pages'] = False
    ctx, _ = context(pages=[short])
    assert list(p.list_jobs(endpoint(), ctx)) and ctx.listing_truncated

def test_explicit_zero_is_empty_but_unknown_response_is_not():
    p = provider(); empty = {'jobs': [], 'pagination': {'total': 0, 'has_more_pages': False}}
    ctx, _ = context(pages=[empty]); assert p.validate(endpoint(), ctx).status == 'empty'
    ctx, _ = context(pages=[{}]); assert p.validate(endpoint(), ctx).status == 'blocked'

def test_missing_published_date_is_not_replaced_by_salted_or_new_date():
    p = provider(); first = fixture(1)
    for row in first['jobs']:
        row.pop('date_added', None); row['salted_date'] = '2099-01-01T00:00:00Z'
    first['pagination'].update(total=3, has_more_pages=False)
    ctx, _ = context(pages=[first]); jobs = list(p.list_jobs(endpoint(), ctx))
    assert all(not j.posted_at and j.date_confidence == 'unknown' for j in jobs)


@pytest.mark.parametrize('first_total,second_total,expected_partial', [(8, 6, True), (3, 6, False), (6, 8, True)])
def test_highest_published_guid_total_controls_completeness(first_total, second_total, expected_partial):
    first, second = fixture(1), fixture(2)
    first['pagination']['total'] = first_total
    second['pagination']['total'] = second_total
    ctx, _ = context(pages=[first, second])
    assert len(list(provider().list_jobs(endpoint(), ctx))) == 4
    assert ctx.listing_truncated is expected_partial
    assert ctx.options['directemployers_nlx_total'] == max(first_total, second_total)


def test_overlapping_page_preserves_unique_jobs_but_not_complete_census():
    first, second = fixture(1), fixture(2)
    second['jobs'].append(copy.deepcopy(first['jobs'][0]))
    ctx, _ = context(pages=[first, second])
    assert len(list(provider().list_jobs(endpoint(), ctx))) == 4
    assert ctx.listing_truncated


@pytest.mark.parametrize('damage', ['non_object', 'missing_guid', 'unsafe_title', 'unsafe_id'])
def test_invalid_identity_rows_cannot_be_counted_as_complete(damage):
    first = fixture(1)
    if damage == 'non_object': first['jobs'][0] = None
    elif damage == 'missing_guid': first['jobs'][0].pop('guid')
    elif damage == 'unsafe_title': first['jobs'][0]['title_slug'] = '../../unrelated'
    else: first['jobs'][0]['reqid'] = 'https://untrusted.example/job'
    ctx, _ = context(pages=[first, fixture(2)])
    jobs = list(provider().list_jobs(endpoint(), ctx))
    assert jobs and ctx.listing_truncated
    assert all('/../' not in j.apply_url and j.apply_url.startswith('https://stantec.jobs/') for j in jobs)


@pytest.mark.parametrize('status', [403, 405, 429, 500])
def test_unknown_or_refused_robots_cannot_trigger_api(status):
    ctx, asked = context()
    def text(url, **kwargs):
        asked.append(url)
        return status, ''
    ctx.get_text = text
    assert provider().validate(endpoint(), ctx).status == 'blocked'
    assert len(asked) == 1 and not any(isinstance(x, tuple) for x in asked)


def test_deadline_expiring_during_first_robots_check_prevents_further_io():
    ctx, asked = context()
    def text(url, **kwargs):
        asked.append(url)
        ctx.deadline = 0
        return 200, 'User-agent: *\nDisallow:\n'
    ctx.get_text = text
    assert list(provider().list_jobs(endpoint(), ctx)) == []
    assert ctx.listing_truncated and len(asked) == 1


def test_structured_access_denial_with_zero_jobs_is_not_an_empty_board():
    ctx, _ = context(pages=[{'jobs': [], 'message': 'Access denied',
                           'pagination': {'total': 0, 'has_more_pages': False}}])
    assert provider().validate(endpoint(), ctx).status == 'blocked'


def test_proven_seed_has_reviewed_endpoint_and_current_ownership_date():
    import importlib.util
    import sys
    root = Path(__file__).resolve().parents[1]
    for path in (root / 'scripts', root / 'scripts/data'):
        if str(path) not in sys.path: sys.path.insert(0, str(path))
    spec = importlib.util.spec_from_file_location('nlx_onboarding', root / 'scripts/onboard_researched_companies.py')
    onboarding = importlib.util.module_from_spec(spec); spec.loader.exec_module(onboarding)
    validation = {'status': 'budget_limited', 'requisitions': 300, 'parsed': 300, 'at': '2026-11-05T23:52:57Z'}
    report = onboarding.build_report(validations={'Stantec': validation})
    rows = [r for c in report['cohorts'].values() for r in c['rows'] if r['company'] == 'Stantec']
    assert len(rows) == 1 and rows[0]['outcome'] == 'new_validated'
    assert rows[0]['endpoint']['vendor'] == 'directemployers_nlx' and rows[0]['endpoint']['site'] == 'usa'
    entry = onboarding._entry('Stantec', 'top')
    records = onboarding.registration_records(rows[0], entry)
    at = records['candidate']['evidence_at']
    assert at.isoformat().startswith(('2026-11-05T', '2026-11-06T'))
    assert at.isoformat() in records['candidate']['verification_reason']
