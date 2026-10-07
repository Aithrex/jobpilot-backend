"""USAJOBS, SuccessFactors and the config-driven employer boards.

Offline by construction: every fetch goes through a `_Ctx` stand-in, so nothing
here reaches the network and nothing depends on a credential existing.

The `_Ctx` shape is lifted from `test_ats_provider_plugins.py` and extended
with the two capabilities these providers need — `extra_headers` (USAJOBS
authenticates with headers) and `post_json` (Apple's board is a POST). It also
RECORDS every call, because "did it page?" and "did it send the credential?"
are the two questions a fabricated fixture cannot answer.
"""
import pytest

from services.ats_providers import registry as ats_registry
from services.ats_providers.base import (DATE_EXACT, DATE_UNKNOWN,
                                         STATUS_BLOCKED, Endpoint)
from services.ats_providers.config_boards import ConfigBoardProvider
from services.ats_providers.successfactors import SuccessFactorsProvider
from services.ats_providers.usajobs import UsaJobsProvider
from services.official_career_registry import CUSTOM_BOARD_ADAPTERS


class _Ctx:
    """Minimal FetchContext stand-in.

    `payload` may be a single object (every call answers it), a dict keyed by
    URL substring, or a LIST that is consumed one call at a time — the last is
    what makes a pagination assertion possible rather than assumed.
    """

    def __init__(self, payload=None, text=None, texts=None, status=200):
        self.payload = payload
        self.text = text
        self.texts = texts or {}
        self.status = status
        self.cutoff_iso = None
        self.options = {}
        self.post_json_calls = []
        self.calls = []
        self.headers = []
        self.params = []
        self.listing_truncated = False
        self.logger = None

    def should_stop(self):
        return False

    def log(self, *args, **kwargs):
        pass

    def _answer(self, url):
        payload = self.payload
        if isinstance(payload, list):
            index = len(self.calls) - 1
            return (self.status, payload[index]) if index < len(payload) \
                else (self.status, None)
        if isinstance(payload, dict):
            for key, value in payload.items():
                if isinstance(key, str) and key.startswith("http") and key in url:
                    return self.status, value
        return self.status, payload

    def get_json(self, url, params=None, timeout=None, vendor="",
                 extra_headers=None):
        self.calls.append(url)
        self.params.append(dict(params or {}))
        self.headers.append(dict(extra_headers or {}))
        return self._answer(url)

    def post_json(self, url, params=None, json_body=None, form_body=None,
                  timeout=None, vendor="", extra_headers=None, want_text=False):
        self.calls.append(url)
        self.params.append(dict(params or {}))
        self.post_json_calls.append(dict(json_body or {}))
        return self._answer(url)

    def get_text(self, url, params=None, timeout=None, vendor="",
                 extra_headers=None):
        self.calls.append(url)
        self.params.append(dict(params or {}))
        for key, body in self.texts.items():
            if key in url:
                return self.status, body
        return self.status, self.text


@pytest.fixture
def usajobs_credentials(monkeypatch):
    monkeypatch.setenv("USAJOBS_API_KEY", "test-key")
    monkeypatch.setenv("USAJOBS_USER_AGENT", "ops@example.com")
    monkeypatch.setenv("USE_SSM_SECRETS", "false")


@pytest.fixture
def usajobs_no_credentials(monkeypatch):
    monkeypatch.delenv("USAJOBS_API_KEY", raising=False)
    monkeypatch.delenv("USAJOBS_USER_AGENT", raising=False)
    monkeypatch.setenv("USE_SSM_SECRETS", "false")


# ---------------------------------------------------------------------------
# USAJOBS
# ---------------------------------------------------------------------------

#: The shape `https://data.usajobs.gov/api/search` documents and returns. Two
#: items so paging and the per-row mapping can both be checked; the second is
#: deliberately sparser than the first.
_USAJOBS_PAGE = {
    "LanguageCode": "EN",
    "SearchResult": {
        "SearchResultCount": 2,
        "SearchResultCountAll": 2,
        "SearchResultItems": [
            {
                "MatchedObjectId": "836271900",
                "MatchedObjectDescriptor": {
                    "PositionID": "DE-12520315-26-JM",
                    "PositionTitle": "IT Specialist (INFOSEC)",
                    "PositionURI": "https://www.usajobs.gov/job/836271900",
                    "ApplyURI": ["https://www.usajobs.gov/job/836271900/print"],
                    "PositionLocationDisplay": "Washington, District of Columbia",
                    "PositionLocation": [{
                        "LocationName": "Washington, District of Columbia",
                        "CountryCode": "United States",
                        "CityName": "Washington",
                    }],
                    "OrganizationName": "Naval Air Systems Command",
                    "DepartmentName": "Department of the Navy",
                    "JobCategory": [{"Name": "Information Technology Management",
                                     "Code": "2210"}],
                    "PositionSchedule": [{"Name": "Full-time", "Code": "1"}],
                    "PositionOfferingType": [{"Name": "Permanent",
                                              "Code": "15317"}],
                    "QualificationSummary": "You must meet the requirements.",
                    "PositionRemuneration": [{
                        "MinimumRange": "99200", "MaximumRange": "128956",
                        "RateIntervalCode": "PA", "Description": "Per Year"}],
                    "PublicationStartDate": "2026-08-27",
                    "ApplicationCloseDate": "2026-09-10",
                    "UserArea": {"Details": {
                        "JobSummary": "<p>Serve as an IT Specialist.</p>",
                        "MajorDuties": "Defend the network.",
                        "TeleworkEligible": True,
                        "RemoteIndicator": False,
                    }},
                },
                "RelevanceRank": 0,
            },
            {
                "MatchedObjectId": "836271901",
                "MatchedObjectDescriptor": {
                    "PositionID": "ST-12520316-26-JM",
                    "PositionTitle": "Program Analyst",
                    "PositionURI": "https://www.usajobs.gov/job/836271901",
                    "PositionLocationDisplay": "Remote",
                    "OrganizationName": "Naval Air Systems Command",
                    "DepartmentName": "Department of the Navy",
                    "UserArea": {"Details": {"RemoteIndicator": True}},
                },
                "RelevanceRank": 1,
            },
        ],
    },
}


def test_usajobs_without_credentials_is_blocked_not_an_exception(
        usajobs_no_credentials):
    """A missing key is "retry later", never "this board is dead".

    `discover_batch` and the endpoint audit call `validate` per candidate, so
    raising here would turn ONE unset SSM parameter into a stack trace per
    federal endpoint with no statement of the cause — and returning DEAD would
    retire employers that a single parameter restores.
    """
    provider = UsaJobsProvider()
    ctx = _Ctx(_USAJOBS_PAGE)
    result = provider.validate(Endpoint(vendor="usajobs", tenant="NV"), ctx)

    assert result.status == STATUS_BLOCKED
    assert result.detail == "usajobs credentials not configured"
    # It must not have looked: an unauthenticated request would 403 and cool
    # the whole vendor down for nothing.
    assert ctx.calls == []
    assert provider.enabled is False


def test_usajobs_without_credentials_lists_nothing_and_does_not_raise(
        usajobs_no_credentials):
    provider = UsaJobsProvider()
    ctx = _Ctx(_USAJOBS_PAGE)
    assert list(provider.list_jobs(Endpoint(vendor="usajobs", tenant="NV"),
                                   ctx)) == []
    assert ctx.calls == []


def test_usajobs_recovers_when_the_credential_arrives(
        usajobs_no_credentials, monkeypatch):
    """`enabled` is decided per CALL, not at import.

    In Lambda the values arrive from SSM after the module is imported, so a
    class-level decision would park the vendor for the life of the container
    even once the parameter was set.
    """
    provider = UsaJobsProvider()
    assert provider._credentials() is None
    assert provider.enabled is False

    monkeypatch.setenv("USAJOBS_API_KEY", "late-key")
    monkeypatch.setenv("USAJOBS_USER_AGENT", "ops@example.com")
    assert provider._credentials() == ("late-key", "ops@example.com")
    assert provider.enabled is True


def test_usajobs_parses_a_search_payload(usajobs_credentials):
    provider = UsaJobsProvider()
    ctx = _Ctx(_USAJOBS_PAGE)
    jobs = list(provider.list_jobs(Endpoint(vendor="usajobs", tenant="NV03"),
                                   ctx))

    assert [j.source_job_id for j in jobs] == ["836271900", "836271901"]
    first = jobs[0]
    assert first.title == "IT Specialist (INFOSEC)"
    # ApplyURI is where the candidate applies and beats the announcement URL.
    assert first.apply_url == "https://www.usajobs.gov/job/836271900/print"
    assert first.location == "Washington, District of Columbia"
    assert first.country == "United States"
    assert first.employment_type == "Full-time"
    assert first.department == "Information Technology Management"
    assert first.compensation["MaximumRange"] == "128956"
    assert "Serve as an IT Specialist" in first.description
    assert first.extra["organization"] == "Naval Air Systems Command"
    # The announcement page, not the apply wizard, is the canonical address.
    assert provider.canonical_url(Endpoint(vendor="usajobs", tenant="NV03"),
                                 first) == "https://www.usajobs.gov/job/836271900"

    # The agency filter and the credential headers actually went out.
    assert ctx.params[0]["Organization"] == "NV03"
    assert ctx.params[0]["Page"] == 1
    assert ctx.headers[0]["Authorization-Key"] == "test-key"
    assert ctx.headers[0]["User-Agent"] == "ops@example.com"


def test_usajobs_publication_start_date_is_exact_and_absence_is_unknown(
        usajobs_credentials):
    """ADR-001: `PublicationStartDate` is a real date; a row without one says
    so rather than being stamped with now()."""
    jobs = list(UsaJobsProvider().list_jobs(
        Endpoint(vendor="usajobs", tenant="NV03"), _Ctx(_USAJOBS_PAGE)))
    assert jobs[0].posted_at.startswith("2026-08-27")
    assert jobs[0].date_confidence == DATE_EXACT
    assert jobs[1].posted_at == ""
    assert jobs[1].date_confidence == DATE_UNKNOWN


def test_usajobs_does_not_read_telework_eligible_as_remote(usajobs_credentials):
    """"May telework" is not "is a remote job"; only `RemoteIndicator` is."""
    jobs = list(UsaJobsProvider().list_jobs(
        Endpoint(vendor="usajobs", tenant="NV03"), _Ctx(_USAJOBS_PAGE)))
    assert jobs[0].remote is False
    assert jobs[1].remote is True


def test_usajobs_stops_when_the_api_reports_the_count_is_exhausted(
        usajobs_credentials):
    """Two rows against a declared total of two is the last page, so a second
    request would be a wasted call against a per-key quota."""
    ctx = _Ctx(_USAJOBS_PAGE)
    list(UsaJobsProvider().list_jobs(Endpoint(vendor="usajobs", tenant="NV03"),
                                     ctx))
    assert len(ctx.calls) == 1


def test_usajobs_names_the_agency_the_postings_declare(usajobs_credentials):
    """`board_owner` is what makes a tenant CODE checkable against reality."""
    provider = UsaJobsProvider()
    owner = provider.board_owner(Endpoint(vendor="usajobs", tenant="NV03"),
                                 _Ctx(_USAJOBS_PAGE))
    assert owner == "Naval Air Systems Command"


def test_usajobs_names_nobody_without_credentials(usajobs_no_credentials):
    assert UsaJobsProvider().board_owner(
        Endpoint(vendor="usajobs", tenant="NV03"), _Ctx(_USAJOBS_PAGE)) is None


def test_usajobs_fingerprints_capture_the_agency():
    hit = ats_registry.match_url(
        "https://www.usajobs.gov/Search/Results?a=NV03&hp=public")
    assert hit is not None
    assert hit[0].vendor == "usajobs"
    assert hit[1]["tenant"] == "NV03"


# ---------------------------------------------------------------------------
# SuccessFactors
# ---------------------------------------------------------------------------

#: The RMK search-results template: a table of `data-row`s. Transcribed from
#: the SuccessFactors career-site theme (see the module docstring's honesty
#: note about what was and was not verified live).
_SF_LISTING = """
<html><head><title>Careers at SAP</title></head><body>
<table id="searchresults">
  <tr class="data-row">
    <td class="colTitle">
      <span class="jobTitle visible-phone">
        <a class="jobTitle-link" href="/job/Walldorf-Senior-Developer-Job-BW-69190/1044321/">
          Senior Developer
        </a>
      </span>
    </td>
    <td class="colLocation"><span class="jobLocation">Walldorf, BW, DE</span></td>
    <td class="colDepartment"><span class="jobDepartment">Software Engineering</span></td>
    <td class="colDate"><span class="jobDate">Aug 27, 2026</span></td>
  </tr>
  <tr class="data-row">
    <td class="colTitle">
      <span class="jobTitle visible-phone">
        <a class="jobTitle-link" href="/job/Remote-Support-Engineer-Job/1044322/">
          Support Engineer
        </a>
      </span>
    </td>
    <td class="colLocation"><span class="jobLocation">Remote, US</span></td>
    <td class="colDepartment"><span class="jobDepartment">Customer Success</span></td>
    <td class="colDate"><span class="jobDate"></span></td>
  </tr>
</table>
</body></html>
"""


def _sf_endpoint():
    return Endpoint(vendor="successfactors", tenant="sap", host="jobs.sap.com")


def test_successfactors_parses_the_rmk_listing():
    ctx = _Ctx(text=_SF_LISTING)
    jobs = list(SuccessFactorsProvider().list_jobs(_sf_endpoint(), ctx))

    assert [j.source_job_id for j in jobs] == ["1044321", "1044322"]
    assert jobs[0].title == "Senior Developer"
    assert jobs[0].apply_url == (
        "https://jobs.sap.com/job/Walldorf-Senior-Developer-Job-BW-69190/1044321/")
    # The trailing ISO country is spelled out: read as a US address, "BW, DE"
    # is Delaware (and "Montreal, CA" is California).
    assert jobs[0].location == "Walldorf, BW, Germany"
    assert jobs[0].country == "Germany"
    assert jobs[0].department == "Software Engineering"
    assert jobs[1].remote is True


def test_successfactors_dates_are_exact_or_absent():
    """The `referencedate` column is a real date; an empty column stays empty
    rather than being back-filled (ADR-001)."""
    jobs = list(SuccessFactorsProvider().list_jobs(
        _sf_endpoint(), _Ctx(text=_SF_LISTING)))
    assert jobs[0].posted_at.startswith("2026-08-27")
    assert jobs[0].date_confidence == DATE_EXACT
    assert jobs[1].posted_at == ""
    assert jobs[1].date_confidence == DATE_UNKNOWN


def test_successfactors_requests_the_documented_search_shape():
    ctx = _Ctx(text=_SF_LISTING)
    list(SuccessFactorsProvider().list_jobs(_sf_endpoint(), ctx))
    assert ctx.calls[0] == "https://jobs.sap.com/search/"
    assert ctx.params[0]["sortColumn"] == "referencedate"
    assert ctx.params[0]["sortDirection"] == "desc"
    # `startrow` is a ROW offset, not a page number. Sending a page index here
    # re-reads the first 25 rows for ever.
    assert ctx.params[0]["startrow"] == 0


@pytest.mark.parametrize("body", [
    "<html><body><p>Something else entirely</p></body></html>",
    "<<<not markup at all >>> {\"json\": true}",
    "",
])
def test_successfactors_degrades_to_nothing_on_garbage(body):
    """A redesigned or broken page is zero rows, never an exception: one
    employer must not end a shard's crawl."""
    ctx = _Ctx(text=body)
    assert list(SuccessFactorsProvider().list_jobs(_sf_endpoint(), ctx)) == []


def test_successfactors_reads_the_owner_from_the_page_title():
    provider = SuccessFactorsProvider()
    assert provider.board_owner(_sf_endpoint(), _Ctx(text=_SF_LISTING)) == "SAP"
    assert provider.board_owner(
        _sf_endpoint(), _Ctx(text="<title>Jobs</title>")) is None


def test_successfactors_will_not_mint_a_tenant_from_a_shared_pod():
    """`career5.successfactors.eu` is a POD shared by thousands of customers.

    Deriving a tenant from that subdomain would file every one of them under
    `career5` — one `member_key`, and therefore one `source_job_id` prefix, for
    unrelated employers (ADR-003).
    """
    provider = SuccessFactorsProvider()
    assert provider.endpoint_from_match(
        {"host": "career5.successfactors.eu"},
        "https://career5.successfactors.eu/career") is None

    endpoint = provider.endpoint_from_match(
        {"host": "career5.successfactors.eu"},
        "https://career5.successfactors.eu/career?company=acmecorp")
    assert endpoint is not None
    assert endpoint.tenant == "acmecorp"


def test_successfactors_fingerprints_the_hosts_it_claims():
    for url, tenant in [
        ("https://career4.successfactors.eu/career?company=exampleco", "exampleco"),
        ("https://jobs.sap.com/search/?q=", "sap"),
    ]:
        hit = ats_registry.match_url(url)
        assert hit is not None, url
        assert hit[0].vendor == "successfactors"
        endpoint = hit[0].endpoint_from_match(hit[1], url)
        assert endpoint is not None and endpoint.tenant == tenant


# ---------------------------------------------------------------------------
# Config-driven employer boards
# ---------------------------------------------------------------------------

#: A fake employer, described exactly the way a real registry row is. Using an
#: invented row rather than a shipped one is deliberate: the point of the
#: provider is that it is driven by the SCHEMA, so a test that only ever ran
#: against Apple would not prove that.
_FAKE_ROW = {
    "key": "acme",
    "display_name": "Acme",
    "domain": "acme.example",
    "vendor": "custom_acme_test",
    "career_landing_url": "https://careers.acme.example/jobs",
    "request": {
        "method": "GET",
        "url": "https://careers.acme.example/api/search",
        "params": {"locale": "en-us"},
    },
    "pagination": {"style": "page", "page_start": 1, "page_size": 2,
                   "max_pages": 5},
    "records_path": "data.results",
    "total_path": "data.total",
    "fields": {
        "source_job_id": ["requisitionId", "id"],
        "title": ["postingTitle", "title"],
        "location": ["locations.0.name"],
        "department": ["team"],
        "posted_at": ["postedOn"],
        "description": ["summary"],
        "apply_url_template": "https://careers.acme.example/job/{source_job_id}",
    },
}


def _fake_page(ids, total=4):
    return {"data": {"total": total, "results": [
        {"requisitionId": rid, "postingTitle": f"Engineer {rid}",
         "locations": [{"name": "Austin, TX"}], "team": "Platform",
         "postedOn": "2026-08-20T00:00:00Z",
         "summary": "<p>Build things.</p>"} for rid in ids]}}


def test_config_board_maps_fields_and_builds_the_apply_url():
    provider = ConfigBoardProvider(_FAKE_ROW)
    ctx = _Ctx([_fake_page(["A1", "A2"]), _fake_page(["A3"], total=4)])
    jobs = list(provider.list_jobs(provider.default_endpoint(), ctx))

    assert [j.source_job_id for j in jobs] == ["A1", "A2", "A3"]
    assert jobs[0].title == "Engineer A1"
    assert jobs[0].location == "Austin, TX"
    assert jobs[0].department == "Platform"
    assert jobs[0].apply_url == "https://careers.acme.example/job/A1"
    assert jobs[0].description == "Build things."
    assert jobs[0].posted_at.startswith("2026-08-20")
    assert jobs[0].date_confidence == DATE_EXACT


def test_config_board_paginates_with_the_rows_own_style():
    """`page_start: 1` means the first request is page 1, not page 0, and a
    short page ends the walk."""
    provider = ConfigBoardProvider(_FAKE_ROW)
    ctx = _Ctx([_fake_page(["A1", "A2"]), _fake_page(["A3"], total=4)])
    list(provider.list_jobs(provider.default_endpoint(), ctx))

    assert [p.get("page") for p in ctx.params] == [1, 2]
    assert all(p.get("locale") == "en-us" for p in ctx.params)
    assert len(ctx.calls) == 2
    assert ctx.listing_truncated is False


def test_config_board_stops_on_the_total_the_board_reports():
    provider = ConfigBoardProvider(_FAKE_ROW)
    ctx = _Ctx([_fake_page(["A1", "A2"], total=2), _fake_page(["A3"])])
    jobs = list(provider.list_jobs(provider.default_endpoint(), ctx))
    assert [j.source_job_id for j in jobs] == ["A1", "A2"]
    assert len(ctx.calls) == 1


def test_config_board_degrades_to_nothing_when_the_shape_changes():
    """A renamed field is a missing value and a moved list is zero rows.

    Both are survivable; an exception in one employer's mapper is not, because
    its blast radius is the shard.
    """
    provider = ConfigBoardProvider(_FAKE_ROW)
    endpoint = provider.default_endpoint()

    # The records list moved.
    assert list(provider.list_jobs(
        endpoint, _Ctx({"results": [{"requisitionId": "A1"}]}))) == []
    # The whole response is a list where a dict was documented.
    assert list(provider.list_jobs(endpoint, _Ctx([["A1", "A2"]]))) == []
    # Records are there but the id/title fields were renamed.
    assert list(provider.list_jobs(endpoint, _Ctx(
        {"data": {"results": [{"jobId": "A1", "name": "Engineer"}]}}))) == []
    # A non-200 answer.
    assert list(provider.list_jobs(
        endpoint, _Ctx(_fake_page(["A1"]), status=404))) == []


def test_config_board_will_not_emit_a_half_built_apply_url():
    """`https://careers.acme.example/job/` is not a job; storing it sends a
    candidate to a 404 while counting as coverage."""
    provider = ConfigBoardProvider(_FAKE_ROW)
    ctx = _Ctx({"data": {"results": [
        {"postingTitle": "Engineer", "locations": [{"name": "Austin, TX"}]}]}})
    assert list(provider.list_jobs(provider.default_endpoint(), ctx)) == []


def test_config_board_percent_encodes_the_vendors_id():
    """A vendor id is untrusted input: one carrying `/` or `?` would otherwise
    rewrite the URL's path or query."""
    provider = ConfigBoardProvider(_FAKE_ROW)
    ctx = _Ctx({"data": {"results": [
        {"requisitionId": "A1/../evil?x=1", "postingTitle": "Engineer"}]}})
    job = list(provider.list_jobs(provider.default_endpoint(), ctx))[0]
    assert job.apply_url == (
        "https://careers.acme.example/job/A1%2F..%2Fevil%3Fx%3D1")


def test_config_board_posts_the_body_and_pages_inside_it():
    """Apple's board pages via a field in the POST body, not a query param."""
    row = dict(_FAKE_ROW, vendor="custom_acme_post_test", request={
        "method": "POST",
        "url": "https://careers.acme.example/api/role/search",
        "json_body": {"query": "", "page": 1, "sort": "newest"},
        "page_field": "page",
    })
    provider = ConfigBoardProvider(row)
    ctx = _Ctx([_fake_page(["A1", "A2"]), _fake_page(["A3"])])
    list(provider.list_jobs(provider.default_endpoint(), ctx))

    assert [b["page"] for b in ctx.post_json_calls] == [1, 2]
    # The base body must not be mutated across pages — it is deep-copied.
    assert provider.base_body["page"] == 1
    assert all(b["sort"] == "newest" for b in ctx.post_json_calls)


def test_config_board_without_a_date_field_claims_no_date():
    """Tesla's board publishes none. ADR-001: the row decides, not the crawler."""
    row = dict(_FAKE_ROW, vendor="custom_acme_undated_test",
               pagination={"style": "none"},
               fields={k: v for k, v in _FAKE_ROW["fields"].items()
                       if k != "posted_at"})
    provider = ConfigBoardProvider(row)
    assert provider.date_confidence == DATE_UNKNOWN
    ctx = _Ctx(_fake_page(["A1"]))
    jobs = list(provider.list_jobs(provider.default_endpoint(), ctx))
    assert jobs[0].posted_at == ""
    assert jobs[0].date_confidence == DATE_UNKNOWN
    # `style: none` is one request, and it is not a truncated listing.
    assert len(ctx.calls) == 1
    assert ctx.listing_truncated is False


def test_config_board_applies_the_cutoff_as_a_row_filter():
    provider = ConfigBoardProvider(_FAKE_ROW)
    ctx = _Ctx([_fake_page(["A1", "A2"], total=2)])
    ctx.cutoff_iso = "2026-08-25T00:00:00+00:00"
    assert list(provider.list_jobs(provider.default_endpoint(), ctx)) == []


def test_every_custom_adapter_row_resolves_to_a_registered_provider():
    """A row naming a vendor with no provider is dropped in silence.

    That is the exact failure the Amazon catalog row shipped with:
    `job_ingest_worker._load_career_members` skips any row whose vendor is not
    in the registry, so the catalog claimed coverage that produced zero rows.
    """
    missing = []
    for row in CUSTOM_BOARD_ADAPTERS:
        vendor = str(row.get("vendor") or "").strip().lower()
        provider = ats_registry.get(vendor)
        if provider is None:
            missing.append(row.get("key"))
            continue
        assert isinstance(provider, ConfigBoardProvider)
        assert provider.key == str(row.get("key") or "").lower()
        # Every row must describe a fetchable request, or it is decoration.
        assert provider.url.startswith("https://")
        assert provider.records_path
        assert provider.api_hosts
    assert not missing, f"adapter rows with no provider: {missing}"


def test_no_company_specific_branch_lives_in_the_config_board_module():
    """The whole point of the module: a new employer is one dict, not a branch.

    Checked mechanically rather than by review, because a single `if
    row["key"] == ...` is exactly how the data-driven design erodes back into
    the per-company code it replaced.
    """
    import pathlib

    from services.ats_providers import config_boards

    source = pathlib.Path(config_boards.__file__).read_text(encoding="utf-8")
    keys = {str(r.get("key") or "").lower() for r in CUSTOM_BOARD_ADAPTERS}
    keys |= {str(r.get("vendor") or "").lower() for r in CUSTOM_BOARD_ADAPTERS}
    # Company names appear only in the docstring's worked examples, so the test
    # looks at CODE lines.
    code = "\n".join(line for line in source.splitlines()
                     if not line.strip().startswith("#"))
    body = code.split('"""', 2)[-1]
    for key in keys:
        for token in (f'"{key}"', f"'{key}'"):
            assert token not in body, f"company-specific literal {token} in code"


def test_the_registry_carries_the_three_new_vendors():
    vendors = ats_registry.vendors()
    assert "usajobs" in vendors
    assert "successfactors" in vendors
    for row in CUSTOM_BOARD_ADAPTERS:
        assert row["vendor"] in vendors


def test_the_new_vendors_declare_their_hosts_and_join_the_termless_sweep():
    """A provider missing from `termless_vendors` never gets crawled, and one
    missing from `api_hosts` is refused by the SSRF guard — both are silent."""
    termless = ats_registry.termless_vendors()
    hosts = ats_registry.api_hosts()
    for vendor in ("usajobs", "successfactors",
                   *(r["vendor"] for r in CUSTOM_BOARD_ADAPTERS)):
        assert vendor in termless, vendor
        assert ats_registry.api_hosts(vendor), vendor
        assert set(ats_registry.api_hosts(vendor)) <= set(hosts), vendor


class TestAdapterStatusIsHonest:
    """`blocked` and `empty` are different claims about an employer.

    A board we cannot READ is a fact about us. Reporting it as "no postings"
    puts the employer in the covered column with the explanation "they are not
    hiring", which is false and which the coverage report would then repeat.
    """

    def _provider(self, vendor):
        from services.ats_providers import registry as ats_registry
        return ats_registry.get(vendor)

    def test_a_blocked_adapter_validates_as_BLOCKED_not_EMPTY(self):
        from services.ats_providers.base import STATUS_BLOCKED, Endpoint
        from services import official_career_registry as ocr

        blocked = [r for r in ocr.CUSTOM_BOARD_ADAPTERS
                   if r.get("status") == "blocked"]
        assert blocked, "the fixture depends on at least one blocked adapter"
        for row in blocked:
            provider = self._provider(row["vendor"])
            got = provider.validate(
                Endpoint(vendor=row["vendor"], tenant=row["key"]), None)
            assert got.status == STATUS_BLOCKED, row["vendor"]
            assert got.detail, row["vendor"]

    def test_every_adapter_records_what_a_live_probe_saw(self):
        from services import official_career_registry as ocr

        for row in ocr.CUSTOM_BOARD_ADAPTERS:
            assert row.get("status") in ("verified", "blocked", "unverified"), row["key"]
            assert row.get("status_detail"), row["key"]

    def test_an_unprobed_adapter_is_treated_as_blocked_not_trusted(self):
        from services.ats_providers.base import STATUS_BLOCKED, Endpoint
        from services.ats_providers.config_boards import ConfigBoardProvider

        provider = ConfigBoardProvider({
            "key": "never-probed", "vendor": "custom_never_probed",
            "display_name": "Never Probed",
            "request": {"method": "GET", "url": "https://example.test/api"},
            "pagination": {"style": "none"}, "records_path": "jobs",
            "fields": {"source_job_id": ["id"], "title": ["title"]},
        })
        got = provider.validate(Endpoint(vendor="custom_never_probed",
                                         tenant="never-probed"), None)
        assert got.status == STATUS_BLOCKED
        assert "never probed" in got.detail


def test_config_board_preflights_once_before_the_first_page():
    """Some boards answer 200-with-zero-rows until the caller holds a session.

    Apple is the case that put this here: `jobs.apple.com/api/v1/search`
    returns `{"res":{"searchResults":[],"totalRecords":0}}` to a cookieless
    client and all 6,141 rows once `/api/v1/CSRFToken` has been called on the
    same session. 200-with-zero-rows is the worst shape a source can fail in,
    because it reads as "this employer is not hiring" rather than as an error.

    The preflight goes through `ctx`, so it is inside the same SSRF guard and
    pacing as every other fetch, and it runs ONCE per listing, not per page.
    """
    row = dict(_FAKE_ROW, vendor="custom_acme_preflight_test", request={
        "method": "GET",
        "url": "https://careers.acme.example/api/search",
        "preflight": {"method": "GET",
                      "url": "https://careers.acme.example/api/session"},
    })
    provider = ConfigBoardProvider(row)
    ctx = _Ctx([None, _fake_page(["A1", "A2"]), _fake_page(["A3"], total=4)])
    jobs = list(provider.list_jobs(provider.default_endpoint(), ctx))

    assert ctx.calls[0].startswith("https://careers.acme.example/api/session")
    assert sum(1 for c in ctx.calls if "/api/session" in c) == 1
    assert [j.source_job_id for j in jobs] == ["A1", "A2", "A3"]


def test_config_board_survives_a_failing_preflight():
    """A handshake that 404s must not cost us the postings search still returns.

    The board may have stopped needing it, and a preflight is a convenience,
    never the contract.
    """
    row = dict(_FAKE_ROW, vendor="custom_acme_preflight_fail_test",
               pagination={"style": "none"}, request={
                   "method": "GET",
                   "url": "https://careers.acme.example/api/search",
                   "preflight": {"url": "https://careers.acme.example/nope"},
               })

    class _Boom(_Ctx):
        def get_json(self, url, params=None, timeout=None, vendor="",
                     extra_headers=None):
            if "/nope" in url:
                raise RuntimeError("preflight exploded")
            return super().get_json(url, params=params, timeout=timeout,
                                    vendor=vendor, extra_headers=extra_headers)

    provider = ConfigBoardProvider(row)
    jobs = list(provider.list_jobs(provider.default_endpoint(),
                                   _Boom(_fake_page(["A1"]))))
    assert [j.source_job_id for j in jobs] == ["A1"]


def test_apple_adapter_is_verified_and_declares_its_handshake():
    """Apple was `blocked` on the adapter's own evidence; it is now read.

    Probed live 2026-09-15: 6,141 rows / 4,905 distinct requisitions, 4,503 of
    them in the United States. The row must carry the preflight, or the
    provider silently reads an empty board again.
    """
    from services.official_career_registry import custom_adapter_for

    row = custom_adapter_for("apple")
    assert row is not None
    assert row["status"] == "verified"
    assert row["request"]["preflight"]["url"] == \
        "https://jobs.apple.com/api/v1/CSRFToken"
    provider = ats_registry.get("custom_apple")
    assert provider.preflight_url.endswith("/api/v1/CSRFToken")
    # A ten-row page must not read as the last page, and 20 is what the API
    # returns; declaring it is what makes the short-page stop meaningful.
    assert provider.page_size == 20


@pytest.mark.parametrize("raw,host,location,country", [
    ("Munich, DE, DE", "", "Munich, DE, Germany", "Germany"),
    ("Atlanta, GA, US", "", "Atlanta, GA, United States", "United States"),
    ("Gda\u0144sk, PL +4 more\u2026", "", "Gda\u0144sk, Poland", "Poland"),
    ("London, GB", "", "London, United Kingdom", "United Kingdom"),
    ("Remote", "", "Remote", ""),
    # A code that is also a US state is only a country where that is proven.
    ("San Francisco, CA", "", "San Francisco, CA", ""),
    ("Montreal, CA", "careers.capgemini.com", "Montreal, Canada", "Canada"),
    ("Munich, DE", "careers.capgemini.com", "Munich, Germany", "Germany"),
])
def test_successfactors_spells_out_the_trailing_country(raw, host, location, country):
    """RMK prints the ISO country last; read as a US address "Montreal, CA" is
    California and "Munich, DE" is Delaware."""
    from services.ats_providers.successfactors import _place
    assert _place(raw, host) == (location, country)


def test_successfactors_reads_the_description_from_the_posting_page():
    page = ('<div class="job"><span class="jobdescription" itemprop="description">'
            '<p>Build SAP integrations.</p><ul><li>5+ years of Java</li></ul>'
            '</span>\n</div>')
    from services.ats_providers.base import RawJob
    job = RawJob(source_job_id="1", title="Developer",
                 apply_url="https://careers.capgemini.com/job/x/1/")
    out = SuccessFactorsProvider().fetch_detail(_sf_endpoint(), job, _Ctx(text=page))
    assert "Build SAP integrations." in out.description and "5+ years of Java" in out.description
    assert "<li>" not in out.description
    assert SuccessFactorsProvider().fetch_detail(
        _sf_endpoint(), job, _Ctx(text="<html>no body here</html>")) is None
    # The crawler's reference for this vendor is the requisition id; the page
    # to read is still the apply URL.
    job.detail_url = "1"
    assert SuccessFactorsProvider().fetch_detail(_sf_endpoint(), job, _Ctx(text=page))


class TestSuccessFactorsUnifiedSearch:
    """Wipro and HCL render their boards from `POST /services/recruiting/v1/jobs`;
    the search page itself carries no rows. Shapes copied from the live
    responses of 2026-10-07."""

    WIPRO = {"id": "208155", "unifiedStandardTitle": "TECHNICAL LEAD L1",
             "unifiedUrlTitle": "TECHNICAL-LEAD-L1", "unifiedStandardStart": "10/7/26",
             "jobLocationCountry": ["United States"], "jobLocationState": ["Kentucky"],
             "sfstd_jobLocation_obj": ["Louisville"]}
    HCL = {"id": "171140", "unifiedStandardTitle": "Subject Matter Expert",
           "unifiedUrlTitle": "Subject-Matter-Expert-%28Support&amp;Ops%29",
           "unifiedStandardStart": "9/1/26", "custCountryRegion": ["United States"],
           "custprimecity": "Others"}

    def _run(self, pages, *, extra, cutoff=""):
        from services.ats_providers.base import Endpoint, FetchContext
        sent = []

        def post_json(url, **kwargs):
            sent.append((url, kwargs["json_body"]))
            page = kwargs["json_body"]["pageNumber"]
            rows = pages[page] if page < len(pages) else []
            return 200, {"totalJobs": 999,
                         "jobSearchResult": [{"response": r} for r in rows]}

        ctx = FetchContext(get_json=None, get_text=None, post_json=post_json,
                           cutoff_iso=cutoff)
        endpoint = Endpoint(vendor="successfactors", tenant="wipro",
                            host="careers.wipro.com", extra=extra)
        return list(SuccessFactorsProvider().list_jobs(endpoint, ctx)), sent

    def test_rows_dates_and_the_country_filter(self):
        jobs, sent = self._run([[self.WIPRO, self.HCL]],
                               extra={"unified": True, "location": "United States"})
        assert sent[0][0] == "https://careers.wipro.com/services/recruiting/v1/jobs"
        assert sent[0][1]["location"] == "United States" and sent[0][1]["sortBy"] == "date"
        first, second = jobs
        assert first.source_job_id == "208155"
        assert first.location == "Louisville, Kentucky, United States"
        assert first.posted_at.startswith("2026-10-07")
        assert first.apply_url == "https://careers.wipro.com/job/TECHNICAL-LEAD-L1/208155-en_US"
        assert second.location == "United States"          # "Others" is not a city
        assert "&amp;" not in second.apply_url and "Support&Ops" in second.apply_url

    def test_the_walk_stops_at_the_first_wholly_stale_page(self):
        fresh, old = dict(self.WIPRO), dict(self.WIPRO, id="1", unifiedStandardStart="9/1/26")
        jobs, sent = self._run([[fresh], [old], [dict(fresh, id="3")]],
                               extra={"unified": True}, cutoff="2026-10-01T00:00:00+00:00")
        assert [j.source_job_id for j in jobs] == ["208155"]
        assert len(sent) == 2

    def test_it_is_opt_in(self):
        from services.ats_providers.base import Endpoint, FetchContext
        calls = []
        ctx = FetchContext(get_json=None, post_json=lambda *a, **k: calls.append(a) or (200, {}),
                           get_text=lambda url, **k: (200, "<html></html>"))
        endpoint = Endpoint(vendor="successfactors", tenant="x", host="jobs.sap.com")
        list(SuccessFactorsProvider().list_jobs(endpoint, ctx))
        assert calls == []

    def test_the_description_is_every_itemprop_block(self):
        from services.ats_providers.base import RawJob
        page = ('<span itemprop="description" class="rtl"><p>About Wipro.</p></span>'
                '<div><span itemprop="description"><p>Mandatory Skills: Tosca.</p></span></div>')
        job = RawJob(source_job_id="1", title="T",
                     apply_url="https://careers.wipro.com/job/T/1-en_US")
        out = SuccessFactorsProvider().fetch_detail(_sf_endpoint(), job, _Ctx(text=page))
        assert "About Wipro." in out.description and "Mandatory Skills: Tosca." in out.description

    @pytest.mark.parametrize("raw,iso", [("10/7/26", "2026-10-07"), ("2/13/2026", "2026-02-13"),
                                         ("13/40/26", ""), ("Oct 7", ""), ("", "")])
    def test_us_short_dates(self, raw, iso):
        from services.ats_providers.successfactors import _us_date
        assert _us_date(raw)[:10] == iso
