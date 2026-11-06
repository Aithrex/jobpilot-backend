"""Employer-scoped DirectEmployers NLX public Solr listings.

Stantec's served frontend selects this API with X-Origin and location=usa.
It publishes one GUID per location, but reqid identifies the requisition.
The frontend maps date_added to JobPosting.datePosted and builds public URLs
from location_exact, title_slug and guid. These are published fields, not dates
or routes inferred from crawl time. Only independently verified boards belong
in BOARDS; the shared API hostname alone proves no employer ownership.
"""
from __future__ import annotations

from datetime import datetime
import json
import re
import unicodedata
from typing import Any, Dict, Iterator, Optional
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

from services.ats_providers.base import (
    AtsProvider, ChallengeError, CrawlPolicy, DATE_EXACT, DATE_UNKNOWN,
    Endpoint, FetchContext, RawJob, ValidationResult, fp,
)
from services.ats_providers.registry import register
from services.ats_providers.util import html_to_text, looks_remote, to_iso

API_HOST = 'prod-search-api.jobsyn.org'
API = 'https://' + API_HOST + '/api/v1/solr/search'
BOARDS = {
    'stantec.jobs': {
        'tenant': 'stantec', 'site': 'usa', 'company': 'Stantec',
        'country': 'United States', 'path': '/locations/usa/jobs/',
    },
}
_GUID = re.compile(r'[A-Fa-f0-9]{32}')
_SLUG = re.compile(r'[a-z0-9]+(?:[-_][a-z0-9]+)*')
_REQID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,159}')


def _count(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    try:
        number = int(value)
        return number if number >= 0 and float(value) == number else None
    except (ValueError, OverflowError):
        return None


def _location_slug(location: str) -> str:
    # The served BfULQbXb.js uses NFD, removes diacritics and these punctuation
    # characters, then joins ASCII \w tokens. Keep the same URL contract.
    normalized = unicodedata.normalize('NFD', location)
    cleaned = ''.join(ch for ch in normalized
                      if not unicodedata.combining(ch) and ch not in '"\u2019+:/')
    return '-'.join(re.findall(r'[A-Za-z0-9_]+', cleaned)).lower()


def _published_date(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        return ''
    try:
        datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return ''
    return to_iso(value)


class DirectEmployersNLXProvider(AtsProvider):
    vendor = 'directemployers_nlx'
    label = 'DirectEmployers NLX'
    termless = True
    has_detail = False
    date_confidence = DATE_UNKNOWN
    slug_probe = False
    api_hosts = (API_HOST,) + tuple(BOARDS)
    portal_hosts = tuple(BOARDS)
    readiness_note = 'Verified employer and country scopes only; interrupted reads remain partial.'
    fingerprints = tuple(
        fp(r'^https?://(?P<host>' + re.escape(host) + ')'
           + re.escape(config['path'].rstrip('/')) + r'/?(?:[?#]|$)', confidence=95)
        for host, config in BOARDS.items()
    )
    policy = CrawlPolicy(
        pacing_key=API_HOST, min_interval=1.05, max_concurrency=1,
        timeout=15.0, page_size=50, max_pages=80,
        challenge_statuses=frozenset({403, 405, 429}),
        challenge_markers=('verify you are human', 'human verification',
                           'px-captcha', 'cf-chl-', 'just a moment',
                           'incapsula incident id', 'access denied',
                           'pardon our interruption'),
    )

    @staticmethod
    def _config(endpoint: Endpoint) -> Optional[Dict[str, Any]]:
        config = BOARDS.get(endpoint.host)
        if (not config or endpoint.tenant != config['tenant']
                or endpoint.site != config['site']):
            return None
        return config

    def endpoint_from_match(self, fields, url):
        host = fields.get('host', '').lower()
        config = BOARDS.get(host)
        if not config or urlparse(url).path.rstrip('/') != config['path'].rstrip('/'):
            return None
        return Endpoint(vendor=self.vendor, tenant=config['tenant'],
                        host=host, site=config['site'])

    def board_url(self, endpoint: Endpoint) -> str:
        config = self._config(endpoint)
        return 'https://' + endpoint.host + config['path'] if config else ''

    def _robots_allowed(self, url: str, ctx: FetchContext) -> bool:
        if ctx.should_stop():
            return False
        origin = 'https://' + urlparse(url).netloc
        cache = ctx.options.setdefault('_directemployers_nlx_robots', {})
        if origin not in cache:
            status, body = ctx.get_text(origin + '/robots.txt',
                                        timeout=self.policy.timeout, vendor=self.vendor)
            if self.policy.is_challenge(status, body or ''):
                raise ChallengeError(self.vendor, status, 'robots refusal')
            if status not in (200, 404, 410):
                raise ChallengeError(self.vendor, status, 'robots unverified')
            cache[origin] = body if status == 200 else ''
        robots = RobotFileParser()
        robots.parse((cache[origin] or '').splitlines())
        if not robots.can_fetch('JobPilotIngest', url):
            raise ChallengeError(self.vendor, detail='robots prohibits ' + url)
        return not ctx.should_stop()

    def _job(self, row: Dict[str, Any], endpoint: Endpoint,
             config: Dict[str, Any]) -> Optional[RawJob]:
        fields = ('reqid', 'guid', 'title_exact', 'location_exact', 'title_slug')
        if any(not isinstance(row.get(key), str) or not row[key].strip() for key in fields):
            return None
        reqid, guid = row['reqid'], row['guid']
        if not (_REQID.fullmatch(reqid) and _GUID.fullmatch(guid)
                and _SLUG.fullmatch(row['title_slug'])):
            return None
        location_slug = _location_slug(row['location_exact'])
        if not location_slug:
            return None
        url = ('https://' + endpoint.host + '/' + location_slug + '/'
               + row['title_slug'] + '/' + guid + '/job/')
        location = row['location_exact'].strip() + ', ' + config['country']
        posted = _published_date(row.get('date_added'))
        return RawJob(
            source_job_id=reqid, title=row['title_exact'].strip(), apply_url=url,
            location=location, country=config['country'], posted_at=posted,
            raw_posted_text=row.get('date_added') if posted else '',
            date_confidence=DATE_EXACT if posted else DATE_UNKNOWN,
            description=html_to_text(row.get('description') or ''),
            remote=looks_remote(row['title_exact'], location),
            extra={'locations': [location], 'nlx_guids': [guid],
                   'hiring_organization': config['company']},
        )

    def list_jobs(self, endpoint: Endpoint, ctx: FetchContext) -> Iterator[RawJob]:
        ctx.listing_truncated = True
        config = self._config(endpoint)
        if not config or ctx.should_stop():
            return
        if not self._robots_allowed(self.board_url(endpoint), ctx):
            return
        if not self._robots_allowed(API, ctx):
            return
        jobs: Dict[str, RawJob] = {}
        seen_guids = set()
        total = None
        damaged = False
        for page in range(1, self.policy.max_pages + 1):
            if ctx.should_stop():
                break
            status, body = ctx.get_json(
                API, params={'location': config['site'], 'page': page, 'num_items': 50},
                extra_headers={'X-Origin': endpoint.host},
                timeout=self.policy.timeout, vendor=self.vendor,
            )
            # Normal job descriptions can mention challenge words. Inspect an
            # error/interstitial body, not legitimate job content, for markers.
            rows = body.get('jobs') if isinstance(body, dict) else None
            error_body = json.dumps(
                {key: body.get(key) for key in ('error', 'message', 'detail')}
                if isinstance(rows, list) else body,
                default=str,
            )
            if self.policy.is_challenge(status, error_body):
                raise ChallengeError(self.vendor, status)
            if status != 200 or not isinstance(rows, list):
                break
            paging = body.get('pagination')
            if not isinstance(paging, dict):
                paging = {}
            stated_total = _count(paging.get('total'))
            if stated_total is not None:
                total = max(total or 0, stated_total)
            before = len(seen_guids)
            for row in rows:
                if not isinstance(row, dict):
                    damaged = True
                    continue
                if (row.get('company_exact') != config['company']
                        or row.get('country_exact') != config['country']):
                    raise ChallengeError(self.vendor, detail='posting employer or country scope is missing or mixed')
                job = self._job(row, endpoint, config)
                if job is None:
                    damaged = True
                    continue
                guid = row['guid'].upper()
                if guid in seen_guids:
                    damaged = True
                    continue
                seen_guids.add(guid)
                existing = jobs.get(job.source_job_id)
                if existing is None:
                    jobs[job.source_job_id] = job
                    continue
                for location in job.extra['locations']:
                    if location not in existing.extra['locations']:
                        existing.extra['locations'].append(location)
                existing.location = '; '.join(existing.extra['locations'])
                existing.extra['nlx_guids'].extend(job.extra['nlx_guids'])
                existing.remote = existing.remote or job.remote
                if len(job.description) > len(existing.description):
                    existing.description = job.description
                if job.posted_at and (not existing.posted_at or job.posted_at < existing.posted_at):
                    existing.posted_at = job.posted_at
                    existing.raw_posted_text = job.raw_posted_text
                    existing.date_confidence = DATE_EXACT
            # Count GUIDs, not requisitions: the published total includes each
            # location. Retain the highest total if the source changes mid-read.
            ctx.options['directemployers_nlx_total'] = total
            ctx.options['directemployers_nlx_guid_count'] = len(seen_guids)
            if paging.get('has_more_pages') is False:
                ctx.listing_truncated = damaged or total is None or len(seen_guids) != total
                break
            if not rows or len(seen_guids) == before:
                break
            if paging.get('has_more_pages') is not True:
                break
        # Buffered results remain useful after a fetch deadline. There is no
        # further I/O here; flag the interruption before the first row escapes.
        if ctx.should_stop():
            ctx.listing_truncated = True
        for job in jobs.values():
            if ctx.cutoff_iso and job.posted_at and job.posted_at < ctx.cutoff_iso:
                continue
            yield job

    def validate(self, endpoint: Endpoint, ctx: FetchContext) -> ValidationResult:
        try:
            jobs = list(self.list_jobs(endpoint, ctx))
            if jobs:
                return ValidationResult('validated', len(jobs), jobs[0].title)
            if not ctx.listing_truncated and ctx.options.get('directemployers_nlx_total') == 0:
                return ValidationResult('empty', detail='explicit zero job total')
            return ValidationResult('blocked', detail='listing unverified or incomplete')
        except ChallengeError as exc:
            return ValidationResult('blocked', detail=str(exc))
        except Exception as exc:
            return ValidationResult('blocked', detail=type(exc).__name__ + ': listing unavailable')


register(DirectEmployersNLXProvider())
