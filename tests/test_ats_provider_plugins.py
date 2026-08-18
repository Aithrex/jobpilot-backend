"""ATS provider plugins: Oracle/iCIMS stay registered; Jobvite/BambooHR added."""
from services.ats_providers import registry as ats_registry
from services.ats_providers.base import Endpoint
from services.ats_providers.boards import BambooHrProvider, JobviteProvider


class _Ctx:
    def __init__(self, payload):
        self.payload = payload
        self.cutoff_iso = None

    def should_stop(self):
        return False

    def get_json(self, url, params=None, timeout=None, vendor=""):
        return 200, self.payload


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


def test_jobvite_parses_positions_payload():
    ctx = _Ctx({"positions": [{
        "id": "jv-1", "title": "Staff Engineer",
        "applyUrl": "https://jobs.jobvite.com/acme/job/jv-1",
        "postedDate": "2026-08-01", "location": "Austin, TX",
        "jobDescription": "<p>Build things</p>",
    }]})
    jobs = list(JobviteProvider().list_jobs(
        Endpoint(vendor="jobvite", tenant="acme"), ctx))
    assert len(jobs) == 1
    assert jobs[0].title == "Staff Engineer"
    assert jobs[0].source_job_id == "jv-1"
    assert "Build things" in jobs[0].description


def test_bamboohr_parses_careers_list():
    ctx = _Ctx({"result": [{
        "id": "42", "jobOpeningName": "Backend Engineer",
        "locationLabel": "Remote, US", "datePosted": "2026-08-10",
        "departmentLabel": "Engineering", "employmentStatusLabel": "Full-Time",
    }]})
    jobs = list(BambooHrProvider().list_jobs(
        Endpoint(vendor="bamboohr", tenant="acme"), ctx))
    assert jobs[0].title == "Backend Engineer"
    assert jobs[0].apply_url == "https://acme.bamboohr.com/careers/42"
    assert jobs[0].department == "Engineering"


def test_jobvite_and_bamboohr_fingerprints_capture_tenant():
    hit = ats_registry.match_url("https://jobs.jobvite.com/rivian/job/123")
    assert hit is not None
    assert hit[0].vendor == "jobvite"
    assert hit[1]["tenant"] == "rivian"
    hit = ats_registry.match_url("https://acme.bamboohr.com/careers/list")
    assert hit is not None
    assert hit[0].vendor == "bamboohr"
    assert hit[1]["tenant"] == "acme"
