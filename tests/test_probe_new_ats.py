"""Response-shape inference for `scripts/probe_new_ats.py`.

The script's whole value is that it reports EVIDENCE about an unsupported ATS
instead of letting someone write a fetcher from memory. That only holds if the
inference is right, so the two pure parts - finding the job array and mapping
the fields - are tested here against the shapes vendors actually ship.

No network: every payload below is a literal. The HTTP half is a thin `requests`
call whose failure modes are all reported rather than raised.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import probe_new_ats as p  # noqa: E402


# ---------------------------------------------------------------------------
# Finding the job array. Every vendor buries it somewhere different, which is
# exactly why this matches on SHAPE rather than on a key name.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("payload,path,count", [
    ({"result": [{"id": 1, "jobTitle": "Engineer"},
                 {"id": 2, "jobTitle": "Analyst"}]}, "result", 2),
    ({"jobs": [{"id": 1, "title": "Engineer"}]}, "jobs", 1),
    ({"data": {"jobs": [{"id": 1, "title": "Engineer"}]}}, "data.jobs", 1),
    ({"requisitionList": [{"jobReqId": 9, "jobTitle": "Nurse"}]}, "requisitionList", 1),
    ([{"id": 1, "text": "Engineer"}], "<root>", 1),
])
def test_the_job_array_is_found_wherever_it_is_buried(payload, path, count):
    jobs, found = p.find_job_array(payload)
    assert found == path
    assert jobs is not None and len(jobs) == count


def test_a_titled_array_beats_a_longer_untitled_one():
    """Payloads carry big lists of things that are not jobs - facets, filters,
    locations. Length alone would pick the wrong one."""
    payload = {
        "facets": [{"id": i, "count": i} for i in range(50)],
        "jobs": [{"id": 1, "title": "Engineer"}, {"id": 2, "title": "Analyst"}],
    }
    jobs, path = p.find_job_array(payload)
    assert path == "jobs" and len(jobs) == 2


def test_no_job_array_is_reported_as_such():
    jobs, path = p.find_job_array({"error": "unauthorized", "code": 401})
    assert jobs is None


def test_a_scalar_payload_is_not_a_job_array():
    assert p.find_job_array("just a string")[0] is None
    assert p.find_job_array(42)[0] is None


# ---------------------------------------------------------------------------
# Field mapping - what a fetcher would actually have to read.
# ---------------------------------------------------------------------------

def test_fields_are_mapped_to_the_real_key_names():
    got = p.infer_fields({
        "id": 4471, "jobTitle": "Senior Engineer",
        "applyUrl": "https://example.com/j/1",
        "postedDate": "2026-07-01", "primaryLocation": "Austin, TX",
    })
    assert got["id"] == "id"
    assert got["title"] == "jobtitle"
    assert got["url"] == "applyurl"
    assert got["posted_at"] == "posteddate"
    assert got["location"] == "primarylocation"


def test_snake_and_kebab_keys_are_normalized():
    got = p.infer_fields({"job_id": 1, "job_title": "Engineer",
                          "absolute_url": "https://x", "first_published": "2026-07-01"})
    assert got["id"] and got["title"] and got["url"] and got["posted_at"]


def test_a_missing_date_key_is_reported_not_invented():
    """The finding that matters most. ADR-001 forbids substituting now() for an
    unknown posting date, so a vendor with no date key produces postings that
    are permanently CONFIDENCE_UNKNOWN - which the operator has to know BEFORE
    building the fetcher, not after."""
    got = p.infer_fields({"id": 1, "title": "Engineer", "url": "https://x"})
    assert got["posted_at"] is None


def test_empty_values_do_not_count_as_present():
    """A key that exists but is always blank is not a usable mapping."""
    got = p.infer_fields({"title": "Engineer", "postedDate": "", "location": []})
    assert got["posted_at"] is None
    assert got["location"] is None


# ---------------------------------------------------------------------------
# Pattern expansion
# ---------------------------------------------------------------------------

def test_a_numbered_pattern_expands_over_the_host_shards():
    """SuccessFactors shards tenants across numbered hosts, so a single URL
    cannot answer "is this tenant reachable"."""
    specs = p.expand("https://career{n}.successfactors.com/career?company={slug}", "acme")
    assert len(specs) == len(p.NUMERIC_RANGE)
    assert any(s["url"] == "https://career1.successfactors.com/career?company=acme"
               for s in specs)


def test_every_registered_vendor_has_at_least_one_pattern():
    assert set(p.PATTERNS) >= {"icims", "jobvite", "bamboohr",
                               "successfactors", "taleo"}
    for vendor, patterns in p.PATTERNS.items():
        assert patterns, vendor


def test_no_probed_vendor_already_has_a_fetcher():
    """The one invariant that actually matters: this tool is for vendors we
    cannot read yet. Probing one we already fetch is wasted requests against an
    employer, and a sign the script and `FETCHERS` have drifted apart."""
    from services.career_pages_service import FETCHERS

    for vendor in p.PATTERNS:
        assert vendor not in FETCHERS, f"{vendor} already has a fetcher"


def test_the_vendors_recommended_by_the_expansion_doc_are_covered():
    """`docs/workday-tenant-expansion.md` §6.4 concluded that the only two new
    fetchers worth building are NeoGov and PeopleAdmin - the rest of that
    section's table is Tier C, "Do not build fetchers". An earlier version of
    this script probed only the ATSs in `TRUSTED_ATS_DOMAINS`, which is a
    reasonable signal but is about whether we TRUST a posting, not about
    whether we can fetch one.

    NeoGov has since GRADUATED: it is a real fetcher now, so it is correctly
    absent from a tool for vendors we cannot read. PeopleAdmin has not, and
    must not quietly fall out."""
    from services.career_pages_service import FETCHERS

    assert "peopleadmin" in p.PATTERNS
    assert "neogov" in FETCHERS and "neogov" not in p.PATTERNS


# ---------------------------------------------------------------------------
# HTML pages. The verdict has to be earned, not pattern-matched.
# ---------------------------------------------------------------------------

def test_unrelated_script_blobs_are_not_reported_as_fetchable():
    """governmentjobs.com carries `window.topResume`, `window.topInterview` and
    `window.Resources` on a fully server-rendered page. Matching on the
    assignment alone reported it as "fetchable, needs extraction" when there is
    nothing there to extract - a confident, wrong claim of exactly the kind this
    script exists to prevent."""
    body = """<html><script>window.topResume = {"a":1};
              window.Resources = {"labels":{"x":"y"}};</script></html>"""
    jobs, path, blob = p._embedded_jobs(body)
    assert jobs is None


def test_a_real_embedded_job_array_is_found():
    body = ('<html><script>window.__INITIAL_STATE__ = '
            '{"search":{"results":[{"id":1,"title":"Engineer","postedDate":"2026-07-01"}]}};'
            '</script></html>')
    jobs, path, blob = p._embedded_jobs(body)
    assert jobs and len(jobs) == 1
    assert blob == "__INITIAL_STATE__"
    assert p.infer_fields(jobs[0])["title"] == "title"


def test_an_unparseable_blob_is_not_evidence():
    body = '<html><script>window.__INITIAL_STATE__ = {not valid json,,,};</script></html>'
    assert p._embedded_jobs(body)[0] is None


# ---------------------------------------------------------------------------
# POST patterns. A GET-only prober reports "no endpoint" for a vendor that
# plainly has one - which is exactly what happened with NeoGov.
# ---------------------------------------------------------------------------

def test_a_string_pattern_is_a_get():
    got = p.expand("https://{slug}.bamboohr.com/careers/list", "acme")
    assert got == [{"url": "https://acme.bamboohr.com/careers/list",
                    "method": "GET", "data": None}]


def test_a_mapping_pattern_carries_its_method_and_body():
    got = p.expand({"url": "https://x.com/careers/{slug}/home/loadJobsOnMaps",
                    "method": "POST", "data": {}}, "washington")
    assert got == [{"url": "https://x.com/careers/washington/home/loadJobsOnMaps",
                    "method": "POST", "data": {}}]


def test_numbered_expansion_still_works_for_mapping_patterns():
    got = p.expand({"url": "https://career{n}.x.com/?c={slug}", "method": "POST"}, "acme")
    assert len(got) == len(p.NUMERIC_RANGE)
    assert all(g["method"] == "POST" for g in got)


def test_post_support_survives():
    """POST is not an optional extra. NeoGov and Workday CXS are both POST-only,
    so a GET-only prober structurally cannot see them - which is exactly how
    NeoGov was nearly written off as unfetchable."""
    spec = p.expand({"url": "https://x.com/{slug}/jobs", "method": "POST", "data": {}}, "a")[0]
    assert spec["method"] == "POST" and spec["data"] == {}


def test_every_vendor_pattern_is_a_string_or_a_mapping_with_a_url():
    for vendor, patterns in p.PATTERNS.items():
        for pattern in patterns:
            if isinstance(pattern, str):
                assert "{slug}" in pattern, vendor
            else:
                assert "{slug}" in pattern["url"], vendor
                assert pattern.get("method") in (None, "GET", "POST"), vendor
