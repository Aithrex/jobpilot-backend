"""Posting pages without a `JobPosting` block still yield their body (2026-11-05).

Live: Paylocity (AME, Inc.: 49 of 68 open postings without a body) and JazzHR
(Noodles & Co: 38 of 53, 638 attempts each) - the page loaded, the parser read
only the schema.org block, and these boards publish none.
"""
from services.ats_providers import payroll
from services.ats_providers.base import Endpoint, FetchContext, RawJob

PAYLOCITY_PAGE = """<html><body><div class="job-preview-details">
  <div class="vertical-padding"><div class="job-listing-header">Job Type</div><div>Full-time</div></div>
  <div class="job-listing-header">Description</div>
  <div><p>The Bending Machine Operator I learns machine operation.</p><div><p>Key Responsibilities:</p>
  <p>Read engineering drawings.</p></div><p>Benefits: medical.</p></div>
  <div class="job-listing-header">Salary Description</div><div>$20.00 to $22.00/hour</div>
</div></body></html>"""

JAZZ_PAGE_NO_LD = """<html><head><script type="application/ld+json">{"@type": "Organization", "name": "Noodles"}</script></head>
<body><div id="job-description"><p>We have a fun, friendly atmosphere.</p><div><p>Requirements: 2 years managing a restaurant.</p></div></div>
<div class="apply-section">Apply now</div></body></html>"""

JAZZ_PAGE_LD = """<html><head><script type="application/ld+json">{"@type": "JobPosting", "description": "<p>From the schema block.</p>", "datePosted": "2026-10-23"}</script></head>
<body><div id="job-description"><p>From the page.</p></div></body></html>"""


def _fetch(provider, html):
    ctx = FetchContext(get_json=lambda *a, **k: (404, None), get_text=lambda *a, **k: (200, html))
    url = "https://example.test/job/1"
    return provider.fetch_detail(Endpoint(vendor=provider.vendor, tenant="t"),
                                 RawJob(source_job_id="1", title="t", apply_url=url, detail_url=url), ctx)


def test_paylocity_reads_the_div_after_the_description_header():
    out = _fetch(payroll.PaylocityProvider(), PAYLOCITY_PAGE)
    assert out is not None
    assert "Bending Machine Operator" in out.description
    assert "Read engineering drawings." in out.description      # nested divs kept
    assert "Benefits: medical." in out.description              # the whole div, not the first child
    assert "$20.00" not in out.description                      # the salary header is a separate section


def test_jazzhr_reads_job_description_when_there_is_no_jobposting_block():
    out = _fetch(payroll.JazzHrProvider(), JAZZ_PAGE_NO_LD)
    assert out is not None
    assert "fun, friendly atmosphere" in out.description and "2 years managing" in out.description
    assert "Apply now" not in out.description


def test_the_schema_block_still_wins_when_present():
    out = _fetch(payroll.JazzHrProvider(), JAZZ_PAGE_LD)
    assert out.description.strip() == "From the schema block."
    assert out.posted_at.startswith("2026-10-23")


def test_a_page_with_neither_is_still_a_miss():
    assert _fetch(payroll.JazzHrProvider(), "<html><body><p>Gone.</p></body></html>") is None


def test_the_revision_moves_so_old_misses_stop_counting():
    assert payroll.PaylocityProvider.detail_revision >= 2
    assert payroll.JazzHrProvider.detail_revision >= 2


def test_an_unclosed_div_yields_nothing_rather_than_the_rest_of_the_page():
    assert payroll._div_inner_html("<div><p>never closed", 0) == ""
