"""Public TalentBrew HTML captured 2026-11-05; no live transport in tests."""
from pathlib import Path
import pytest
from services.ats_providers import registry
from services.ats_providers.base import ChallengeError, Endpoint, FetchContext

FIX = Path(__file__).parent / 'fixtures/ats_providers/radancy_talentbrew'
BOARDS = [
    ('synopsys', '44408', 'careers.synopsys.com', '/search-jobs'),
    ('spectrum', '4673', 'jobs.spectrum.com', '/search-jobs'),
    ('optum', '34088', 'careers.unitedhealthgroup.com', '/optum-jobs'),
    ('pge', '29673', 'jobs.pge.com', '/search-jobs'),
    ('solutionhealth', '47438', 'careers.solutionhealth.org', '/search-jobs'),
]

def provider():
    p = registry.get('radancy_talentbrew')
    assert p is not None, 'TalentBrew public-page provider must be registered'
    return p

def fixture(name):
    return (FIX / name).read_text(encoding='utf8')

def setup(board=BOARDS[0], listing=None, robots=(200, 'User-agent: *\nDisallow: /search-jobs/\n'), detail=None):
    name, tenant, host, path = board
    ep = Endpoint(vendor='radancy_talentbrew', tenant=tenant, host=host,
                  site='optum' if name == 'optum' else '')
    asked = []
    def get_text(url, **kwargs):
        asked.append(url)
        if url == 'https://' + host + '/robots.txt':
            return robots
        if url == 'https://' + host + path:
            return listing if listing is not None else (200, fixture(name + '_listing.html'))
        if detail is not None and url.startswith('https://' + host + '/job/'):
            return detail
        raise AssertionError('Unapproved request: ' + url)
    return ep, FetchContext(get_json=None, get_text=get_text), asked

@pytest.mark.parametrize('board', BOARDS)
def test_real_boards_fingerprint_and_list_only_permitted_ssr(board):
    name, tenant, host, path = board
    p = provider()
    hit = registry.match_url('https://' + host + path)
    assert hit is not None and hit[0].vendor == 'radancy_talentbrew'
    matched = hit[0].endpoint_from_match(hit[1], 'https://' + host + path)
    assert matched.tenant == tenant and matched.host == host
    ep, ctx, asked = setup(board)
    stream = p.list_jobs(ep, ctx)
    first = next(stream)
    assert ctx.listing_truncated, 'Flag partial census before the caller stops consuming'
    jobs = [first, *stream]
    assert len(jobs) == 3 and len({j.source_job_id for j in jobs}) == 3
    assert all(j.title and j.apply_url.startswith('https://' + host + '/job/') for j in jobs)
    assert all(j.detail_url == j.apply_url for j in jobs)
    assert asked == ['https://' + host + '/robots.txt', 'https://' + host + path]
    assert ctx.options['talentbrew_total'] > len(jobs)
    assert registry.readiness('radancy_talentbrew')['state'] == 'ready'

@pytest.mark.parametrize('url', [
    'https://tbcdn.talentbrew.com/company/4673/js/search.js',
    'https://unverified.example/search-jobs',
    'https://jobs.intuit.com/search-jobs',
    'https://careers.unitedhealthgroup.com/search-jobs',
])
def test_assets_unproven_hosts_and_unscoped_parent_are_not_boards(url):
    provider()
    hit = registry.match_url(url)
    assert hit is None or hit[0].vendor != 'radancy_talentbrew'

@pytest.mark.parametrize('status', [403, 405, 429])
def test_refusal_is_blocked_and_does_not_become_empty(status):
    p = provider(); ep, ctx, asked = setup(listing=(status, ''))
    with pytest.raises(ChallengeError):
        list(p.list_jobs(ep, ctx))
    ep, ctx, _ = setup(listing=(status, ''))
    assert p.validate(ep, ctx).status == 'blocked'

@pytest.mark.parametrize('body', ['Verify you are human', 'Request unsuccessful. Incapsula incident ID', 'Access Denied'])
def test_http_200_challenges_are_blocked(body):
    p = provider(); ep, ctx, _ = setup(listing=(200, body))
    with pytest.raises(ChallengeError):
        list(p.list_jobs(ep, ctx))

def test_robots_refusal_prevents_the_listing_request():
    p = provider(); ep, ctx, asked = setup(robots=(200, 'User-agent: *\nDisallow: /search-jobs\n'))
    assert p.validate(ep, ctx).status == 'blocked'
    assert len(asked) == 1 and asked[0].endswith('/robots.txt')

def test_unverified_robots_fails_closed():
    p = provider(); ep, ctx, asked = setup(robots=(503, 'unavailable'))
    assert p.validate(ep, ctx).status == 'blocked'
    assert len(asked) == 1

def test_optum_requires_checked_exact_entity_facet():
    p = provider(); unscoped = fixture('optum_listing.html').replace('checked="checked"', '')
    ep, ctx, _ = setup(BOARDS[2], listing=(200, unscoped))
    assert p.validate(ep, ctx).status == 'blocked'
    assert ctx.listing_truncated


def test_optum_rejects_additional_selected_brand_values():
    p = provider()
    mixed = fixture('optum_listing.html').replace('</section>', '<input checked="checked" data-field-name="custom_fields.UHGAJDType" data-id="UnitedHealthcare"/></section>', 1)
    ep, ctx, _ = setup(BOARDS[2], listing=(200, mixed))
    assert p.validate(ep, ctx).status == 'blocked'
    assert ctx.listing_truncated


@pytest.mark.parametrize('brand', ['brand-facet__unitedhealthcare', 'brand-facet__optum brand-facet__unitedhealthcare', ''])
def test_optum_rejects_mismatching_or_missing_card_brands(brand):
    p = provider()
    mixed = fixture('optum_listing.html').replace('brand-facet__optum', brand, 1)
    ep, ctx, _ = setup(BOARDS[2], listing=(200, mixed))
    assert p.validate(ep, ctx).status == 'blocked'
    assert ctx.listing_truncated

@pytest.mark.parametrize('board', BOARDS[:3])
def test_detail_uses_published_date_description_and_location(board):
    p = provider(); ep, ctx, asked = setup(board, detail=(200, fixture(board[0] + '_detail.html')))
    job = next(p.list_jobs(ep, ctx)); original_id = job.source_job_id
    detail = p.fetch_detail(ep, job, ctx)
    assert detail is not None and len(detail.description) > 100
    assert detail.source_job_id == original_id
    assert detail.country == 'United States'
    assert detail.posted_at.startswith('2026-') and detail.date_confidence == 'exact'
    assert detail.title and detail.location
    # Only Spectrum publishes employmentType in these three real schemas.
    assert bool(detail.employment_type) == (board[0] == 'spectrum')
    assert len(asked) == 3 and asked[-1] == job.detail_url

def test_detail_cannot_fetch_cross_host_or_ajax_path():
    p = provider(); ep, ctx, asked = setup(); job = next(p.list_jobs(ep, ctx))
    for url in ['https://evil.example/job/a/b/44408/1', 'https://careers.synopsys.com/search-jobs/results']:
        job.detail_url = url
        assert p.fetch_detail(ep, job, ctx) is None
    assert len(asked) == 2


def test_multicountry_details_preserve_separate_normalizable_locations():
    import json
    schema = {'@type': 'JobPosting', 'description': 'Published description', 'jobLocation': [
        {'address': {'addressLocality': 'Austin', 'addressRegion': 'Texas', 'addressCountry': 'United States'}},
        {'address': {'addressLocality': 'Toronto', 'addressRegion': 'Ontario', 'addressCountry': {'name': 'Canada'}}},
    ]}
    body = '<script type="application/ld+json">' + json.dumps(schema) + '</script>'
    p = provider(); ep, ctx, _ = setup(detail=(200, body))
    job = next(p.list_jobs(ep, ctx))
    detail = p.fetch_detail(ep, job, ctx)
    assert detail.country == ''
    assert detail.extra['locations'] == ['Austin, Texas, United States', 'Toronto, Ontario, Canada']

def test_zero_total_is_empty_but_missing_template_is_unverified():
    p = provider()
    ep, ctx, _ = setup(listing=(200, '<section id="search-results" data-total-job-results="0"></section>'))
    assert p.validate(ep, ctx).status == 'empty' and not ctx.listing_truncated
    ep, ctx, _ = setup(listing=(200, '<html>Loading jobs...</html>'))
    assert p.validate(ep, ctx).status == 'blocked' and ctx.listing_truncated

def test_deadline_and_bad_endpoint_do_not_make_requests():
    p = provider(); ep, ctx, asked = setup(); ctx.deadline = 0
    assert list(p.list_jobs(ep, ctx)) == [] and ctx.listing_truncated and asked == []
    ep, ctx, asked = setup(); ep.host = 'unverified.example'
    assert list(p.list_jobs(ep, ctx)) == [] and asked == []

def test_no_missing_date_is_minted_and_actual_short_board_can_be_complete():
    p = provider(); body = fixture('spectrum_listing.html').replace('data-total-job-results="1458"', 'data-total-job-results="3"').replace('data-total-pages="98"', 'data-total-pages="1"')
    ep, ctx, _ = setup(BOARDS[1], listing=(200, body))
    jobs = list(p.list_jobs(ep, ctx))
    assert len(jobs) == 3 and not ctx.listing_truncated
    assert all(not j.posted_at and j.date_confidence == 'unknown' for j in jobs)

def test_permanently_missing_listing_is_dead_not_empty():
    p = provider(); ep, ctx, _ = setup(listing=(404, 'Not found'))
    assert p.validate(ep, ctx).status == 'dead'

def test_stale_row_is_filtered_without_claiming_unread_pages_are_complete():
    p = provider(); ep, ctx, _ = setup()
    ctx.cutoff_iso = '2026-11-01T00:00:00+00:00'
    jobs = list(p.list_jobs(ep, ctx))
    assert all(not j.posted_at or j.posted_at >= ctx.cutoff_iso for j in jobs)
    assert ctx.listing_truncated


def test_all_five_seeded_employers_reach_the_owner_onboarding_report():
    import importlib.util
    import sys
    root = Path(__file__).resolve().parents[1]
    for path in (root / 'scripts', root / 'scripts/data'):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))
    spec = importlib.util.spec_from_file_location('talentbrew_onboarding', root / 'scripts/onboard_researched_companies.py')
    onboarding = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(onboarding)
    names = {'Synopsys', 'Charter Communications', 'Optum', 'PG&E', 'SolutionHealth'}
    validations = {name: {'status': 'budget_limited', 'requisitions': 15, 'parsed': 15,
                          'at': '2026-11-05T23:24:00Z'} for name in names}
    report = onboarding.build_report(validations=validations)
    rows = {row['company']: row for cohort in report['cohorts'].values() for row in cohort['rows']}
    assert names <= rows.keys(), 'A plan entry omitted from cohort membership cannot be registered by the owner'
    assert all(rows[name]['outcome'] == 'new_validated' for name in names)
    for name in names:
        entry = onboarding._entry(name, 'leads' if name == 'SolutionHealth' else 'top')
        records = onboarding.registration_records(rows[name], entry)
        at = records['candidate']['evidence_at']
        assert at.isoformat().startswith('2026-11-05T')
        assert at.isoformat() in records['candidate']['verification_reason']
