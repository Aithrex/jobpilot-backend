"""Payroll-attached ATS providers (UKG, Paylocity, Rippling).

Every fixture below is a verbatim slice of a live response captured on
2026-08-18, not a hand-written approximation. That distinction is the whole
reason these tests exist in this shape: the previous Jobvite and BambooHR tests
passed against providers that could never return a row, because their fixtures
invented the same fields the parsers read.
"""
import json

from services.ats_providers import registry as ats_registry
from services.ats_providers.base import Endpoint
from services.ats_providers.payroll import (PaylocityProvider, RipplingProvider,
                                            UkgProvider)


class _Ctx:
    def __init__(self, *, payload=None, text=None):
        self.payload = payload
        self.text = text
        self.cutoff_iso = None
        self.post_json = self._post_json

    def should_stop(self):
        return False

    def log(self, *args, **kwargs):
        pass

    def get_json(self, url, params=None, timeout=None, vendor=""):
        return 200, self.payload

    def get_text(self, url, params=None, timeout=None, vendor=""):
        return 200, self.text

    def _post_json(self, url, params=None, json_body=None, timeout=None,
                   vendor="", extra_headers=None):
        # One page only: a second call returns nothing so paging terminates.
        payload, self.payload = self.payload, {"opportunities": []}
        return 200, payload


# ---------------------------------------------------------------------------
# UKG
# ---------------------------------------------------------------------------

_UKG = {"totalCount": 1, "opportunities": [{
    "Id": "786b191c-a716-4a20-9b3a-39ca765213b8",
    "Title": "Community Manager",
    "RequisitionNumber": "COMMU005476",
    "FullTime": True,
    "JobCategoryName": "Community Management",
    "PostedDate": "2026-08-17T19:52:04.042Z",
    "BriefDescription": "Are you a dynamic problem-solver?",
    "Locations": [{
        "LocalizedDescription": "Myrtle Beach Office",
        "Address": {
            "Line1": "2411 N. Oak Street", "City": "Myrtle Beach",
            "PostalCode": "29577",
            # BOTH of these are objects, not strings.
            "State": {"Code": "SC", "Name": "South Carolina"},
            "Country": {"Code": "USA", "Name": "United States"},
        },
    }],
}]}


def test_ukg_flattens_the_nested_state_and_country_objects():
    """`str()` of `{"Code": "SC", ...}` would index a Python repr."""
    jobs = list(UkgProvider().list_jobs(
        Endpoint(vendor="ukg", tenant="AAM1000AAM", site="board-guid"),
        _Ctx(payload=_UKG)))
    assert len(jobs) == 1
    assert jobs[0].location == "Myrtle Beach, SC, USA"
    assert "{" not in jobs[0].location


def test_ukg_prefers_the_postal_address_over_the_site_label():
    """"Myrtle Beach Office" is the employer's name for a building."""
    jobs = list(UkgProvider().list_jobs(
        Endpoint(vendor="ukg", tenant="AAM1000AAM", site="board-guid"),
        _Ctx(payload=_UKG)))
    assert "Office" not in jobs[0].location


def test_ukg_reads_the_real_posted_date():
    jobs = list(UkgProvider().list_jobs(
        Endpoint(vendor="ukg", tenant="AAM1000AAM", site="board-guid"),
        _Ctx(payload=_UKG)))
    assert jobs[0].posted_at.startswith("2026-08-17")
    assert jobs[0].date_confidence == "exact"
    assert jobs[0].employment_type == "Full-Time"


def test_ukg_needs_both_ids_and_says_so_rather_than_guessing():
    """A UKG board is customer-code + board-GUID; neither is derivable."""
    provider = UkgProvider()
    assert provider.slug_probe is False
    assert list(provider.list_jobs(
        Endpoint(vendor="ukg", tenant="AAM1000AAM"), _Ctx(payload=_UKG))) == []


def test_ukg_fingerprint_captures_code_and_board():
    hit = ats_registry.match_url(
        "https://recruiting.ultipro.com/AAM1000AAM/JobBoard/"
        "c5a88c41-a6d1-4e5d-bf94-4d0432a0df30/OpportunityDetail?opportunityId=x")
    assert hit is not None
    assert hit[0].vendor == "ukg"
    assert hit[1]["tenant"] == "AAM1000AAM"
    assert hit[1]["site"] == "c5a88c41-a6d1-4e5d-bf94-4d0432a0df30"


# ---------------------------------------------------------------------------
# Paylocity
# ---------------------------------------------------------------------------

_PAYLOCITY_ROWS = [{
    "JobId": 4425403,
    "JobTitle": "Sales Representative – $150K+ Earning Potential",
    "LocationName": "BIS",
    "ShouldDisplayLocation": True,
    "PublishedDate": "2026-08-17T10:54:58-05:00",
    "Description": "Sales Representative teaser text",
    "IsInternal": False,
    "HiringDepartment": None,
    "JobLocation": {"Name": "BIS", "City": "Bismarck", "State": "ND",
                    "Zip": "58503", "Country": "USA"},
    "IsRemote": False,
}, {
    "JobId": 999, "JobTitle": "Internal Only Role", "IsInternal": True,
    "PublishedDate": "2026-08-17T10:54:58-05:00", "Description": "x",
    "JobLocation": {"City": "Bismarck", "State": "ND", "Country": "USA"},
}]

_PAYLOCITY_HTML = ('<html><script>window.state = {"Jobs": '
                   + json.dumps(_PAYLOCITY_ROWS)
                   + ', "Other": 1};</script></html>')

_PAYLOCITY_DETAIL = '''<html><head>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"JobPosting",
 "title":"Sales Representative",
 "datePosted":"2026-08-17T15:55:02-05:00",
 "description":"<p>The full job description, far longer than the teaser.</p>",
 "baseSalary":{"@type":"MonetaryAmount","currency":"USD",
   "value":{"@type":"QuantitativeValue","minValue":100000.0,
            "maxValue":200000.0,"unitText":"YEAR"}}}
</script></head><body></body></html>'''


def test_paylocity_reads_the_embedded_job_array():
    jobs = list(PaylocityProvider().list_jobs(
        Endpoint(vendor="paylocity", tenant="guid"),
        _Ctx(text=_PAYLOCITY_HTML)))
    assert [j.source_job_id for j in jobs] == ["4425403"]
    assert jobs[0].location == "Bismarck, ND, USA"
    assert jobs[0].posted_at.startswith("2026-08-17")


def test_paylocity_drops_internal_only_postings():
    """An outsider cannot apply to these, so listing them wastes a seeker."""
    jobs = list(PaylocityProvider().list_jobs(
        Endpoint(vendor="paylocity", tenant="guid"),
        _Ctx(text=_PAYLOCITY_HTML)))
    assert all(j.title != "Internal Only Role" for j in jobs)


def test_paylocity_ignores_the_internal_location_code():
    """`LocationName` is "BIS"; the real place is in `JobLocation`."""
    jobs = list(PaylocityProvider().list_jobs(
        Endpoint(vendor="paylocity", tenant="guid"),
        _Ctx(text=_PAYLOCITY_HTML)))
    assert jobs[0].location != "BIS"


def test_paylocity_detail_supplies_the_real_description_and_salary():
    """The listing `Description` is a fixed-length teaser, not the JD."""
    provider = PaylocityProvider()
    assert provider.has_detail is True
    listed = list(provider.list_jobs(
        Endpoint(vendor="paylocity", tenant="guid"),
        _Ctx(text=_PAYLOCITY_HTML)))[0]
    detail = provider.fetch_detail(
        Endpoint(vendor="paylocity", tenant="guid"), listed,
        _Ctx(text=_PAYLOCITY_DETAIL))
    assert detail is not None
    assert "far longer than the teaser" in detail.description
    assert detail.posted_at.startswith("2026-08-17")
    assert detail.compensation["value"]["minValue"] == 100000.0


# ---------------------------------------------------------------------------
# Rippling
# ---------------------------------------------------------------------------

_RIPPLING = [{
    "uuid": "2f0674e6-f01f-4ecd-b459-e947241c211f",
    "name": "Account Executive - Accountants Channel",
    "department": {"id": "Sales", "label": "Sales"},
    "url": "https://ats.rippling.com/rippling/jobs/2f0674e6",
    "workLocation": {"label": "Remote (Massachusetts, US)",
                     "id": "Remote (Massachusetts, US)"},
}]

_RIPPLING_DETAIL = {
    "createdOn": "2023-10-31T12:00:00Z",
    "description": "<p>Own the accountants channel.</p>",
    "employmentType": {"label": "Full time"},
    "payRangeDetails": [{"min": 100, "max": 200}],
}


def test_rippling_listing_claims_no_date():
    """Neither a date nor a description is in the list payload (ADR-001)."""
    provider = RipplingProvider()
    assert provider.date_confidence == "unknown"
    jobs = list(provider.list_jobs(
        Endpoint(vendor="rippling", tenant="rippling"),
        _Ctx(payload=_RIPPLING)))
    assert jobs[0].posted_at == ""
    assert jobs[0].description == ""
    assert jobs[0].location == "Remote (Massachusetts, US)"
    assert jobs[0].department == "Sales"


def test_rippling_detail_upgrades_the_row():
    provider = RipplingProvider()
    listed = list(provider.list_jobs(
        Endpoint(vendor="rippling", tenant="rippling"),
        _Ctx(payload=_RIPPLING)))[0]
    detail = provider.fetch_detail(
        Endpoint(vendor="rippling", tenant="rippling"), listed,
        _Ctx(payload=_RIPPLING_DETAIL))
    assert detail.posted_at.startswith("2023-10-31")
    assert detail.date_confidence == "exact"
    assert "accountants channel" in detail.description
    assert detail.employment_type == "Full time"


# ---------------------------------------------------------------------------
# Registration wiring
# ---------------------------------------------------------------------------

def test_new_vendors_reach_the_crawler_not_just_the_registry():
    """Registration must derive the crawler tables, or the source ships zero.

    This is the failure ADR-004 §4.1 was written to make impossible: a provider
    can be registered, tested and correct, and still never fetch anything
    because its host is not on the SSRF allowlist or its vendor is not in the
    termless sweep.
    """
    import services.job_ingest_worker as worker

    for vendor in ("ukg", "paylocity", "rippling"):
        assert vendor in ats_registry.vendors()
        assert vendor in worker._TERMLESS_ATS

    allowed = set(worker._HOST_ALLOW[worker.SOURCE_CAREER])
    assert "recruiting.ultipro.com" in allowed
    assert "recruiting.paylocity.com" in allowed
    assert "ats.rippling.com" in allowed


# ---------------------------------------------------------------------------
# Cornerstone
# ---------------------------------------------------------------------------

_CSOD_SHELL = ('<html><body><script>'
               'if(!csod.context || !csod.context.token) csod.context='
               '{"corp":"arizona","user":-100,"endpoints":{"cloud":"https://x/",'
               '"api":"/"},"token":"jwt-token-value","version":"1"};'
               '</script></body></html>')

_CSOD_SEARCH = {"totalCount": 1, "requisitions": [{
    "requisitionId": 26826,
    "postingEffectiveDate": "8/17/2026",
    "displayJobTitle": "Regulatory Clinical Research Coordinator",
    "locations": [{"city": "Tucson", "state": "AZ", "country": "US"}],
}]}


class _CsodCtx:
    """Serves the shell page to `get_text` and the search result to `post_json`."""

    def __init__(self, shell=_CSOD_SHELL, search=_CSOD_SEARCH):
        self.shell = shell
        self.search = search
        self.cutoff_iso = None
        self.posts = []
        self.post_json = self._post_json

    def should_stop(self):
        return False

    def log(self, *a, **k):
        pass

    def get_text(self, url, params=None, timeout=None, vendor=""):
        return 200, self.shell

    def get_json(self, url, params=None, timeout=None, vendor=""):
        return 200, None

    def _post_json(self, url, params=None, json_body=None, timeout=None,
                   vendor="", extra_headers=None):
        self.posts.append((json_body, extra_headers or {}))
        if (json_body or {}).get("pageNumber", 1) > 1:
            return 200, {"data": {"totalCount": 1, "requisitions": []}}
        return 200, {"data": self.search}


def _csod_rows(endpoint, ctx):
    from services.ats_providers.payroll import CornerstoneProvider
    return list(CornerstoneProvider().list_jobs(endpoint, ctx))


def test_cornerstone_reads_the_token_the_shell_page_publishes():
    """The 401 is not "login required" — the credential is in the HTML."""
    ctx = _CsodCtx()
    rows = _csod_rows(Endpoint(vendor="cornerstone", tenant="arizona",
                               site="4", host="arizona.csod.com"), ctx)
    assert len(rows) == 1
    assert ctx.posts, "no search was issued"
    _body, headers = ctx.posts[0]
    assert headers.get("Authorization") == "Bearer jwt-token-value"


def test_cornerstone_sends_the_required_culture_name():
    """Omitting it is a 400 that names the field."""
    ctx = _CsodCtx()
    _csod_rows(Endpoint(vendor="cornerstone", tenant="arizona", site="4"), ctx)
    body, _headers = ctx.posts[0]
    assert body["CultureName"] == "en-US"


def test_cornerstone_maps_the_row():
    rows = _csod_rows(Endpoint(vendor="cornerstone", tenant="arizona",
                               site="4", host="arizona.csod.com"), _CsodCtx())
    row = rows[0]
    assert row.source_job_id == "26826"
    assert row.location == "Tucson, AZ, US"
    assert row.posted_at.startswith("2026-08-17")
    assert row.apply_url == ("https://arizona.csod.com/ux/ats/careersite/4"
                             "/home/requisition/26826?c=arizona")


def test_cornerstone_discovers_the_site_id_when_it_is_unknown():
    """Site ids are not derivable and are not always 1.

    Arizona serves 324 requisitions on site 4 and zero on site 1, so a provider
    that assumed 1 would report the employer as having no openings.
    """
    class _OnlySiteThree(_CsodCtx):
        def _post_json(self, url, params=None, json_body=None, timeout=None,
                       vendor="", extra_headers=None):
            self.posts.append((json_body, extra_headers or {}))
            if (json_body or {}).get("careerSiteId") != 3:
                return 200, {"data": {"totalCount": 0, "requisitions": []}}
            if (json_body or {}).get("pageNumber", 1) > 1:
                return 200, {"data": {"totalCount": 1, "requisitions": []}}
            return 200, {"data": self.search}

    endpoint = Endpoint(vendor="cornerstone", tenant="arizona",
                        host="arizona.csod.com")
    rows = _csod_rows(endpoint, _OnlySiteThree())
    assert len(rows) == 1
    assert endpoint.site == "3", "the working site id must be remembered"


def test_cornerstone_publishes_no_description_and_does_not_invent_one():
    """No public route returns a JD; an empty string is the honest answer."""
    rows = _csod_rows(Endpoint(vendor="cornerstone", tenant="arizona",
                               site="4"), _CsodCtx())
    assert rows[0].description == ""
