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


# ---------------------------------------------------------------------------
# A guessed slug has to be corroborated
# ---------------------------------------------------------------------------

class TestSlugCorroboration:
    """Validating a guessed slug proves a board EXISTS, not whose it is.

    A wrong guess that validates is the worst shape of wrong: it serves real
    jobs, so it reads as coverage rather than as an error, and those jobs are
    then shown under another employer's name. Found in production - 8 of 352
    board endpoints were somebody else's board, including "Garage Door
    Specialist" filed as GDS Holdings, "Space Programs" as SpaceX, and "SIE
    TAIWAN" as TSMC.
    """

    def test_a_generic_first_token_is_not_a_claimable_slug(self):
        from services.ats_providers.util import slug_is_trustworthy

        for name, slug in [("Super Micro Computer, Inc.", "super"),
                           ("Genius Group Ltd", "genius"),
                           ("Airship AI Holdings, Inc.", "airship"),
                           ("Quantum Computing Inc.", "quantum"),
                           ("APPLIED OPTOELECTRONICS, INC.", "applied"),
                           ("PAID INC", "paid")]:
            assert slug_is_trustworthy(name, slug) is False, f"{name} -> {slug}"

    def test_a_distinctive_slug_still_stands_on_its_own(self):
        """Lever and Ashby publish no owner name, so tightening this must not
        cost the guesses that are plainly right."""
        from services.ats_providers.util import slug_is_trustworthy

        for name, slug in [("Palantir Technologies Inc.", "palantir"),
                           ("Cerebras Systems Inc.", "cerebras"),
                           ("VEEVA SYSTEMS INC", "veeva"),
                           ("ServiceTitan, Inc.", "servicetitan"),
                           ("Hinge Health, Inc.", "hinge-health"),
                           ("Snowflake Inc.", "snowflake")]:
            assert slug_is_trustworthy(name, slug) is True, f"{name} -> {slug}"

    def test_a_board_title_is_not_a_different_company(self):
        """"Careers at KKR" is KKR. Judging that a mismatch retires real
        coverage to remove bad coverage."""
        from services.ats_providers.util import names_match

        assert names_match("KKR & Co. Inc.", "Careers at KKR")
        assert names_match("BuzzFeed, Inc.", "Employment Opportunities at BuzzFeed, Inc.")
        assert names_match("Clear Secure, Inc.", "CLEAR - Corporate")
        assert names_match("CS Disco, Inc.", "DISCO")
        assert names_match("ASURE SOFTWARE INC", "Asure Software")
        assert not names_match("Super League Enterprise, Inc.", "Super Technologies")
        assert not names_match("SPACE EXPLORATION TECHNOLOGIES CORP", "Space Programs")

    def test_a_short_declared_name_must_cover_most_of_the_company_name(self):
        """Greenhouse's `public` board declares `name: "Public"` - it is
        Public.com, the investing app. A bare prefix rule matched that to
        "Public Policy Holding Company, Inc" on a live run, which would have
        filed a stranger's board under a real employer. Verified against the
        vendor: GET /v1/boards/public -> 200 {"name": "Public"}.
        """
        from services.ats_providers.util import names_match

        assert not names_match("Public Policy Holding Company, Inc", "Public")
        # ...while the pairings that ARE the same company still pass:
        assert names_match("Clear Secure, Inc.", "CLEAR")          # 0.45
        assert names_match("Payoneer Global Inc.", "Payoneer")     # 0.57
        assert names_match("CS Disco, Inc.", "DISCO")              # 0.71

    def test_a_long_declared_name_stands_on_its_own_whatever_the_ratio(self):
        from services.ats_providers.util import names_match

        assert names_match("ServiceTitan Holdings International Inc.",
                           "ServiceTitan")

    def test_an_ordinary_noun_is_not_a_claimable_slug_for_the_blind_vendors(self):
        """Ashby and Lever publish no owner name, so `slug_is_trustworthy` is
        the ONLY guard on those two. A live run over the uncovered tech cohort
        claimed all four of these; each is somebody else's board.
        """
        from services.ats_providers.util import slug_is_trustworthy

        for name, slug in [("Sphere 3D Corp.", "sphere"),
                           ("Twenty One Capital, Inc.", "twenty"),
                           ("SIERRA INTERNATIONAL NETWORK INC", "sierra"),
                           ("Odyssey Health, Inc.", "odyssey"),
                           ("Uniti Group Inc.", "uniti"),
                           ("Public Policy Holding Company, Inc", "public")]:
            assert slug_is_trustworthy(name, slug) is False, f"{name} -> {slug}"

    def test_greenhouse_and_smartrecruiters_can_name_their_owner(self):
        """The two vendors that publish it must expose it, because that is
        what makes a guess checkable rather than merely plausible."""
        from services.ats_providers import registry as ats_registry
        from services.ats_providers.base import AtsProvider

        for vendor in ("greenhouse", "smartrecruiters"):
            provider = ats_registry.get(vendor)
            assert provider.board_owner.__func__ is not AtsProvider.board_owner, \
                f"{vendor} publishes an owner name and must return it"
        # Lever and Ashby do not publish one; "cannot say" must stay None
        # rather than becoming "does not match".
        for vendor in ("lever", "ashby"):
            provider = ats_registry.get(vendor)
            assert provider.board_owner.__func__ is AtsProvider.board_owner


def test_the_provider_registry_is_safe_to_load_concurrently():
    """`_load_builtins` set its "already loaded" flag BEFORE importing, so a
    second thread calling `get()` mid-import saw the flag, returned at once,
    and read an empty registry - `get("greenhouse")` returned None for a
    registered vendor. Measured: an audit on six threads resolved a provider
    for 1 of 342 rows and silently skipped the rest. Every concurrent caller
    here is exposed (`job_ingest_worker` fans out over a shard's members,
    `discover_batch` over a batch of companies), and the symptom is not an
    error but a member that quietly lists nothing.
    """
    # A FRESH interpreter, not `importlib.reload`. Reloading the registry
    # clears `_PROVIDERS` but leaves the provider modules in `sys.modules`, so
    # `__import__` is a no-op, `register` never runs again, and the registry
    # stays permanently empty - it would fail this test for the wrong reason
    # and poison every test after it in the same process.
    import subprocess
    import sys
    import textwrap
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    script = textwrap.dedent("""
        import concurrent.futures as cf
        from services.ats_providers import registry as r
        with cf.ThreadPoolExecutor(max_workers=16) as ex:
            got = list(ex.map(lambda _: r.get("greenhouse") is not None,
                              range(64)))
        print(sum(got))
    """)
    out = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True,
        cwd=str(root),
        env={"PYTHONPATH": str(root / "shared" / "python"), "PATH": "/usr/bin:/bin"},
        timeout=120)
    assert out.returncode == 0, out.stderr[-800:]
    assert out.stdout.strip() == "64", \
        f"registry resolved {out.stdout.strip()}/64 under concurrency"
