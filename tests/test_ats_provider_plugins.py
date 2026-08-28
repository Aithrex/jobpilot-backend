"""ATS provider plugins: Oracle/iCIMS stay registered; Jobvite/BambooHR added."""
from services.ats_providers import registry as ats_registry
from services.ats_providers.base import Endpoint
from services.ats_providers.boards import BambooHrProvider, JobviteProvider


class _Ctx:
    """Minimal FetchContext stand-in.

    Carries BOTH fetchers: several providers are HTML, not JSON, and a context
    that only speaks `get_json` silently forces a test to be written against a
    protocol the vendor does not serve — which is exactly how Jobvite and
    BambooHR shipped parsing fabricated payloads while their tests passed.
    """

    def __init__(self, payload=None, text=None, texts=None):
        self.payload = payload
        self.text = text
        self.texts = texts or {}
        self.cutoff_iso = None
        self.calls = []

    def should_stop(self):
        return False

    def log(self, *args, **kwargs):
        pass

    def get_json(self, url, params=None, timeout=None, vendor=""):
        self.calls.append(url)
        if isinstance(self.payload, dict) and url in self.payload:
            return 200, self.payload[url]
        return 200, self.payload

    def get_text(self, url, params=None, timeout=None, vendor=""):
        self.calls.append(url)
        for key, body in self.texts.items():
            if key in url:
                return 200, body
        return 200, self.text


def test_at_least_twelve_vendors_including_oracle_and_icims():
    vendors = ats_registry.vendors()
    assert "oracle_orc" in vendors
    assert "icims" in vendors
    assert "workday" in vendors
    assert "jobvite" in vendors
    assert "bamboohr" in vendors
    assert "phenom" in vendors
    assert "eightfold" in vendors
    assert len(vendors) >= 14


#: A verbatim slice of `https://jobs.jobvite.com/actionet/search` (2026-08-18).
#: Kept as real markup rather than a tidied approximation: the reason the old
#: Jobvite test passed against a broken provider is that its fixture was
#: invented, so it could only ever prove the parser agreed with the test author.
_JOBVITE_HTML = """
<table class="jv-job-list">
  <tr>
    <td class="jv-job-list-name">
      <a href="/actionet/job/ouFyAfw1">Engineering and Installation Technician</a>
    </td>
    <td class="jv-job-list-location">
        Honolulu,
        Nizhny
    </td>
  </tr>
  <tr>
    <td class="jv-job-list-name">
      <a href="/actionet/job/o2cAzfw7">Enterprise Operations Manager</a>
    </td>
    <td class="jv-job-list-location">
      <div class="jv-meta">2 Locations</div>
    </td>
  </tr>
</table>
"""


def test_jobvite_parses_the_html_board_it_actually_serves():
    ctx = _Ctx(text=_JOBVITE_HTML)
    jobs = list(JobviteProvider().list_jobs(
        Endpoint(vendor="jobvite", tenant="actionet"), ctx))
    assert [j.source_job_id for j in jobs] == ["ouFyAfw1", "o2cAzfw7"]
    assert jobs[0].title == "Engineering and Installation Technician"
    assert jobs[0].apply_url == \
        "https://jobs.jobvite.com/actionet/job/ouFyAfw1"
    assert jobs[0].location == "Honolulu, Nizhny"


def test_jobvite_does_not_store_a_location_count_as_a_place():
    """"2 Locations" is a count. Storing it makes the req match nowhere."""
    ctx = _Ctx(text=_JOBVITE_HTML)
    jobs = list(JobviteProvider().list_jobs(
        Endpoint(vendor="jobvite", tenant="actionet"), ctx))
    assert jobs[1].location == ""


def test_jobvite_treats_the_marketing_redirect_as_not_a_board():
    """An unknown slug 200s onto Jobvite's own support page.

    Read as "a board with no jobs", such a tenant is kept and re-crawled for
    ever; it has to read as "not a board".
    """
    ctx = _Ctx(text='<html><body>job-seeker-support</body></html>')
    jobs = list(JobviteProvider().list_jobs(
        Endpoint(vendor="jobvite", tenant="nope"), ctx))
    assert jobs == []


def test_jobvite_never_claims_a_posting_date():
    """Jobvite publishes none, on listing or detail (ADR-001)."""
    assert JobviteProvider().date_confidence == "unknown"
    ctx = _Ctx(text=_JOBVITE_HTML)
    jobs = list(JobviteProvider().list_jobs(
        Endpoint(vendor="jobvite", tenant="actionet"), ctx))
    assert all(j.posted_at == "" for j in jobs)


#: The shape `24slg.bamboohr.com/careers/list` really returns (2026-08-18).
#: Note what is ABSENT: no `datePosted`, no `locationLabel`. Both were in the
#: previous fixture, and neither exists.
_BAMBOO_LIST = {"meta": {"totalCount": 1}, "result": [{
    "id": "549", "jobOpeningName": "Admin - Solar Cell",
    "departmentLabel": "Operation EV/Solar",
    "employmentStatusLabel": "Full-Time", "employmentType": None,
    "location": {"city": "Wattana", "state": "Bangkok"},
    "atsLocation": {"country": None, "state": None, "city": None},
    "isRemote": None, "locationType": "0",
}]}

_BAMBOO_DETAIL = {"meta": {}, "result": {"jobOpening": {
    "datePosted": "2026-01-26", "compensation": "17k-19k",
    "description": "<p>Role Summary</p><p>The Administrative Officer...</p>",
    "employmentStatusLabel": "Full-Time",
    "departmentLabel": "Operation EV/Solar",
    "jobOpeningShareUrl": "https://24slg.bamboohr.com/careers/549",
}}}


def test_bamboohr_reads_the_nested_location_object():
    """`location` is a dict; `str()` of it would index literal Python."""
    ctx = _Ctx(_BAMBOO_LIST)
    jobs = list(BambooHrProvider().list_jobs(
        Endpoint(vendor="bamboohr", tenant="24slg"), ctx))
    assert jobs[0].title == "Admin - Solar Cell"
    assert jobs[0].location == "Wattana, Bangkok"
    assert jobs[0].department == "Operation EV/Solar"
    assert jobs[0].apply_url == "https://24slg.bamboohr.com/careers/549"


def test_bamboohr_listing_claims_no_date_and_detail_supplies_it():
    """The listing has no date field at all; `/detail` is where it lives."""
    assert BambooHrProvider().date_confidence == "unknown"
    ctx = _Ctx(_BAMBOO_LIST)
    endpoint = Endpoint(vendor="bamboohr", tenant="24slg")
    listed = list(BambooHrProvider().list_jobs(endpoint, ctx))[0]
    assert listed.posted_at == ""
    assert listed.date_confidence == "unknown"

    detail = BambooHrProvider().fetch_detail(
        endpoint, listed, _Ctx(_BAMBOO_DETAIL))
    assert detail is not None
    assert detail.posted_at.startswith("2026-01-26")
    assert detail.date_confidence == "exact"
    assert detail.compensation == "17k-19k"
    assert "Role Summary" in detail.description


def test_bamboohr_skips_vendor_infrastructure_labels():
    """`www.bamboohr.com/careers/list` is marketing HTML, not a board."""
    provider = BambooHrProvider()
    assert provider.endpoint_from_match({"tenant": "www"}, "") is None
    assert provider.endpoint_from_match({"tenant": "24slg"}, "") is not None


def test_jobvite_and_bamboohr_fingerprints_capture_tenant():
    hit = ats_registry.match_url("https://jobs.jobvite.com/rivian/job/123")
    assert hit is not None
    assert hit[0].vendor == "jobvite"
    assert hit[1]["tenant"] == "rivian"
    hit = ats_registry.match_url("https://acme.bamboohr.com/careers/list")
    assert hit is not None
    assert hit[0].vendor == "bamboohr"
    assert hit[1]["tenant"] == "acme"


# ---------------------------------------------------------------------------
# Catalog <-> registry coverage
# ---------------------------------------------------------------------------

def test_every_catalog_vendor_has_a_registered_provider():
    """A catalog row naming a vendor with no provider is dropped in silence.

    That is not a hypothetical. `ENTERPRISE_CATALOG` carried
    `{"display_name": "Amazon", "ats": "amazon", ...}` from the day it was
    written, no `amazon` provider was ever registered, and
    `job_ingest_worker._load_career_members` skips any row whose `ats` is not
    in the registry - so a live `job_postings` query for Amazon returned ZERO
    while the catalog claimed to cover it, and `inventory_feed` even carried an
    "Amazon Jobs" badge for a source that had never produced a posting.

    The crawler now logs that skip. This test is the other half: the catalogs
    and the registry must not drift apart in the first place.
    """
    from services.ats_providers import registry as ats_registry

    known = set(ats_registry.vendors())
    missing = {}

    from services.career_pages_catalog import CAREER_PAGES_CATALOG
    for row in CAREER_PAGES_CATALOG:
        ats = str(row.get("ats") or "").strip().lower()
        if ats and ats not in known:
            missing.setdefault(ats, []).append(row.get("display_name"))

    from services.career_pages_service import ENTERPRISE_CATALOG
    for row in ENTERPRISE_CATALOG:
        ats = str(row.get("ats") or "").strip().lower()
        ats = "oracle_orc" if ats == "orc" else ats
        if ats and ats not in known:
            missing.setdefault(ats, []).append(row.get("display_name"))

    from services.mnc_catalog import MNC_CATALOG
    for row in MNC_CATALOG:
        engine = str(row.get("engine") or "")
        if engine in ("aggregator", "uber", "workday"):
            continue          # metered / no provider / its own loader
        ats = ("oracle_orc" if engine == "orc"
               else str(row.get("ats") or row.get("fetcher") or "").strip().lower())
        if ats and ats not in known:
            missing.setdefault(ats, []).append(row.get("display_name"))

    assert not missing, f"catalog vendors with no provider: {missing}"


def test_the_amazon_provider_is_registered_and_termless():
    from services.ats_providers import registry as ats_registry

    provider = ats_registry.get("amazon")
    assert provider is not None, "ENTERPRISE_CATALOG's Amazon row needs a provider"
    assert provider.termless, "the background crawl only sweeps termless vendors"
    assert "amazon" in ats_registry.termless_vendors()
    # The listing carries the whole description, so a detail fetch buys nothing.
    assert provider.has_detail is False


def test_amazon_maps_a_search_row_onto_a_raw_job():
    from services.ats_providers.amazon import AmazonProvider

    raw = AmazonProvider._to_raw({
        "id_icims": "10517649",
        "title": "Software Development Engineer Intern, Annapurna Labs - 2027",
        "job_path": "/en/jobs/10517649/software-development-engineer-intern",
        "normalized_location": "Cupertino, California, USA",
        "posted_date": "August 27, 2026",
        "country_code": "USA",
        "job_schedule_type": "full-time",
        "description": "Build things.",
        "basic_qualifications": "Enrolled in a degree program.",
        "preferred_qualifications": "Python.",
        "is_intern": True,
    })
    assert raw is not None
    assert raw.source_job_id == "10517649"
    assert raw.posted_at.startswith("2026-08-27")
    assert raw.apply_url.startswith("https://www.amazon.jobs/en/jobs/")
    assert "Enrolled in a degree program" in raw.description
    # The employer's OWN statement of the band beats any title guess.
    assert raw.extra.get("seniority") == "intern"
