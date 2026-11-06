"""TalentBrew public server-rendered pages, measured 2026-11-05.

Official Synopsys, Spectrum, Optum, PG&E and SolutionHealth pages expose
15 cards under #search-results-list, with data-total-job-results on the
wrapper. Their JobPosting JSON-LD supplies description/datePosted on detail.
Measured advertised totals: 676, 1458, 5933, 149 and 510 respectively.

The observed /search-jobs/results and /resultspost Ajax routes are prohibited
by robots on the three sampled detail tenants. Spectrum's literal Next URL
(/search-jobs&p=2) redirects home. We read one public page and mark the missing
remainder truncated BEFORE yielding; neither advertised totals nor a broken
Next link makes a full census. This differs from radancy.py's /api/jobs JSON.

Only named, employer-linked hosts are permitted. All requests use FetchContext,
check robots, and stay on the observed listing or numeric posting route. The
shared Optum destination additionally must retain its checked entity facet.
"""
from __future__ import annotations

from dataclasses import replace
import json
import re
from typing import Any, Dict, Iterator, Optional
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

from services.ats_providers.base import (
    AtsProvider, ChallengeError, CrawlPolicy, DATE_EXACT, DATE_UNKNOWN,
    Endpoint, FetchContext, RawJob, ValidationResult, fp,
)
from services.ats_providers.registry import register
from services.ats_providers.util import html_to_text, looks_remote, to_iso

# Observed organization IDs and routes are data, never company-specific parsers.
BOARDS = {
    'careers.synopsys.com': {'tenant': '44408', 'path': '/search-jobs'},
    'jobs.spectrum.com': {'tenant': '4673', 'path': '/search-jobs'},
    'careers.unitedhealthgroup.com': {
        'tenant': '34088', 'path': '/optum-jobs', 'site': 'optum',
        'facets': {'custom_fields.UHGAJDType': 'Optum'},
        'card_brand': 'brand-facet__optum',
    },
    'jobs.pge.com': {'tenant': '29673', 'path': '/search-jobs'},
    'careers.solutionhealth.org': {'tenant': '47438', 'path': '/search-jobs'},
}
_JOB_PATH = re.compile(r'^/job/[^/]+/[^/]+/(?P<org>\d+)/(?P<id>\d+)/?$')


def _document(body: str):
    from lxml import html
    try:
        return html.fromstring(body or '<html/>')
    except (ValueError, TypeError):
        return html.fromstring('<html/>')


def _text(node) -> str:
    return ' '.join(node.text_content().split()) if node is not None else ''


def _schema_postings(doc):
    def walk(value):
        if isinstance(value, list):
            for item in value:
                yield from walk(item)
        elif isinstance(value, dict):
            types = value.get('@type', [])
            if types == 'JobPosting' or isinstance(types, list) and 'JobPosting' in types:
                yield value
            yield from walk(value.get('@graph', []))
    for script in doc.xpath('//script[@type="application/ld+json"]'):
        try:
            yield from walk(json.loads(script.text or ''))
        except (ValueError, TypeError):
            continue


class TalentBrewProvider(AtsProvider):
    vendor = 'radancy_talentbrew'
    label = 'Radancy TalentBrew'
    termless = True
    has_detail = True
    date_confidence = DATE_UNKNOWN
    slug_probe = False
    api_hosts = tuple(BOARDS)
    portal_hosts = api_hosts
    reserved_tenants = ('tbcdn', 'assets', 'static')
    readiness_note = 'Public first page only; prohibited Ajax pagination is never requested and unread inventory is marked truncated.'
    fingerprints = tuple(
        fp(r'^https?://(?P<host>' + re.escape(host) + ')' + re.escape(config['path']) + r'(?:[?#]|$)', confidence=92)
        for host, config in BOARDS.items()
    )
    policy = CrawlPolicy(
        min_interval=1.05, max_concurrency=1, timeout=15.0,
        detail_timeout=10.0, page_size=15, max_pages=1,
        challenge_statuses=frozenset({403, 405, 429, 503}),
        challenge_markers=('verify you are human', 'human verification', 'px-captcha',
                           'cf-chl-', 'just a moment', 'incapsula incident id',
                           '_incapsula_resource', 'access denied', 'pardon our interruption'),
    )

    @staticmethod
    def _config(endpoint: Endpoint) -> Optional[Dict[str, Any]]:
        config = BOARDS.get(endpoint.host)
        if not config or endpoint.tenant != config['tenant'] or endpoint.site != config.get('site', ''):
            return None
        return config

    def endpoint_from_match(self, fields, url):
        host = fields.get('host', '').lower()
        config = BOARDS.get(host)
        if not config or urlparse(url).path != config['path']:
            return None
        return Endpoint(vendor=self.vendor, tenant=config['tenant'], host=host,
                        site=config.get('site', ''))

    def board_url(self, endpoint: Endpoint) -> str:
        config = self._config(endpoint)
        return 'https://' + endpoint.host + config['path'] if config else ''

    def _read(self, url: str, endpoint: Endpoint, ctx: FetchContext):
        origin = 'https://' + endpoint.host
        cache = ctx.options.setdefault('_talentbrew_robots', {})
        if origin not in cache:
            status, body = ctx.get_text(origin + '/robots.txt', timeout=self.policy.timeout, vendor=self.vendor)
            if self.policy.is_challenge(status, body or ''):
                raise ChallengeError(self.vendor, status, 'robots refusal')
            if status not in (200, 404, 410):
                raise ChallengeError(self.vendor, status, 'robots unverified')
            cache[origin] = body if status == 200 else ''
        robots = RobotFileParser()
        robots.parse((cache[origin] or '').splitlines())
        if not robots.can_fetch('JobPilotIngest', url):
            raise ChallengeError(self.vendor, detail='robots prohibits ' + url)
        if ctx.should_stop():
            ctx.listing_truncated = True
            return None, ''
        status, body = ctx.get_text(url, timeout=self.policy.detail_timeout, vendor=self.vendor)
        if self.policy.is_challenge(status, body or ''):
            raise ChallengeError(self.vendor, status)
        return status, body or ''

    def list_jobs(self, endpoint: Endpoint, ctx: FetchContext) -> Iterator[RawJob]:
        config = self._config(endpoint)
        if not config or ctx.should_stop():
            ctx.listing_truncated = True
            return
        url = self.board_url(endpoint)
        status, body = self._read(url, endpoint, ctx)
        ctx.options['talentbrew_listing_status'] = status
        if status != 200:
            ctx.listing_truncated = True
            return
        doc = _document(body)
        for field, value in config.get('facets', {}).items():
            selected = set(doc.xpath('//*[@id="search-filters"]//input[@checked and @data-field-name=$field]/@data-id', field=field))
            if selected != {value}:
                ctx.listing_truncated = True
                raise ChallengeError(self.vendor, detail='required employer facet is missing or mixed')
        boxes = doc.xpath('//*[@id="search-results"]')
        if not boxes:
            ctx.listing_truncated = True
            return
        box = boxes[0]
        try:
            total = int(box.get('data-total-job-results'))
        except (TypeError, ValueError):
            total = None
        ctx.options['talentbrew_total'] = total
        rows = {}
        for a in box.xpath('.//*[@id="search-results-list"]//a[@data-job-id and @href]'):
            if config.get('card_brand'):
                brands = {token for token in (a.get('class') or '').split() if token.startswith('brand-facet__')}
                if brands != {config['card_brand']}:
                    ctx.listing_truncated = True
                    raise ChallengeError(self.vendor, detail='posting employer brand is missing or mixed')
            detail_url = urljoin(url, a.get('href'))
            parsed = urlparse(detail_url)
            match = _JOB_PATH.fullmatch(parsed.path)
            if parsed.scheme != 'https' or parsed.netloc != endpoint.host or not match or match['org'] != config['tenant'] or match['id'] != a.get('data-job-id'):
                continue
            title_nodes = a.xpath('.//h2|.//h3')
            title = _text(title_nodes[0] if title_nodes else a)
            if not title:
                continue
            cards = a.xpath('ancestor::li[1]')
            card = cards[0] if cards else a
            locations = card.xpath('.//*[contains(@class,"job-location")]')
            location = _text(locations[0]) if locations else ''
            dates = card.xpath('.//*[contains(@class,"job-date-posted")]')
            stated_date = re.sub(r'^Posted:\s*', '', _text(dates[0]), flags=re.I) if dates else ''
            posted = to_iso(stated_date) or ''
            rows[match['id']] = RawJob(
                source_job_id=match['id'], title=title, apply_url=detail_url,
                detail_url=detail_url, location=location, posted_at=posted,
                date_confidence=DATE_EXACT if posted else DATE_UNKNOWN,
                remote=looks_remote(title, location),
                extra={'talentbrew_organization_id': config['tenant'], 'listing_page': url},
            )
        # Establish completeness before yield: validators often stop after five.
        if total is None or total != len(rows):
            ctx.listing_truncated = True
        ctx.options['talentbrew_rows_read'] = len(rows)
        for row in rows.values():
            if ctx.should_stop():
                ctx.listing_truncated = True
                return
            if ctx.cutoff_iso and row.posted_at and row.posted_at < ctx.cutoff_iso:
                continue
            yield row

    def fetch_detail(self, endpoint: Endpoint, job: RawJob, ctx: FetchContext):
        config = self._config(endpoint)
        if not config or ctx.should_stop():
            return None
        parsed = urlparse(job.detail_url or '')
        match = _JOB_PATH.fullmatch(parsed.path)
        if parsed.scheme != 'https' or parsed.netloc != endpoint.host or not match or match['org'] != config['tenant'] or match['id'] != job.source_job_id:
            return None
        status, body = self._read(job.detail_url, endpoint, ctx)
        if status != 200:
            return None
        for posting in _schema_postings(_document(body)):
            if posting.get('url') and urlparse(posting['url']).path.rstrip('/') != parsed.path.rstrip('/'):
                continue
            locations = posting.get('jobLocation') or []
            locations = locations if isinstance(locations, list) else [locations]
            labels, countries, full_locations = [], [], []
            for location in locations:
                address = location.get('address', {}) if isinstance(location, dict) else {}
                if not isinstance(address, dict):
                    continue
                label = ', '.join(str(address[k]) for k in ('addressLocality', 'addressRegion') if address.get(k))
                labels.append(label)
                country = address.get('addressCountry', '')
                if isinstance(country, dict):
                    country = country.get('name', '')
                if country and country not in countries:
                    countries.append(str(country))
                full = ', '.join(x for x in (label, str(country or '')) if x)
                if full and full not in full_locations:
                    full_locations.append(full)
            location = '; '.join(dict.fromkeys(x for x in labels if x)) or job.location
            posted = to_iso(posting.get('datePosted')) or job.posted_at
            employment = posting.get('employmentType') or ''
            employment = ', '.join(employment) if isinstance(employment, list) else str(employment)
            employer = posting.get('hiringOrganization') or {}
            extra = dict(job.extra)
            if full_locations:
                extra['locations'] = full_locations
            if isinstance(employer, dict) and employer.get('name'):
                extra['hiring_organization'] = employer['name']
            return replace(job, title=posting.get('title') or job.title,
                           description=html_to_text(posting.get('description') or ''),
                           location=location, country=countries[0] if len(countries) == 1 else '',
                           employment_type=employment, posted_at=posted,
                           date_confidence=DATE_EXACT if posted else DATE_UNKNOWN,
                           remote=looks_remote(posting.get('title'), location, posting.get('jobLocationType')),
                           extra=extra)
        return None

    def validate(self, endpoint: Endpoint, ctx: FetchContext) -> ValidationResult:
        try:
            jobs = list(self.list_jobs(endpoint, ctx))
            if jobs:
                return ValidationResult('validated', len(jobs), jobs[0].title)
            if ctx.options.get('talentbrew_listing_status') in (404, 410):
                return ValidationResult('dead', detail='listing no longer exists')
            if ctx.listing_truncated:
                return ValidationResult('blocked', detail='listing unverified or incomplete')
            return ValidationResult('empty', detail='explicit zero job total')
        except ChallengeError as exc:
            return ValidationResult('blocked', detail=str(exc))


register(TalentBrewProvider())
