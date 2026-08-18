"""iCIMS card parsing, migrated portals, and fabricated posting dates.

The markup below is a verbatim slice of `staff-emory.icims.com` (2026-08-18).
Everything it exercises was a live defect: the provider returned rows, and every
row was wrong.
"""
from datetime import datetime, timezone

import pytest

from services.ats_providers.base import Endpoint, PortalMovedError, RawJob
from services.ats_providers.icims import IcimsProvider
from services.ats_providers.util import looks_minted_now

_CARD_HTML = """
<ul class="container-fluid iCIMS_JobsTable">
<li class="iCIMS_JobCardItem">
  <div class="row">
    <div class="col-xs-12 title">
      <a href="https://staff-emory.icims.com/jobs/172004/program-assistant/job?in_iframe=1"
         class="iCIMS_Anchor" title="172004 - Program Assistant (NGO)">
        <span class="sr-only field-label">Job Title </span>
        <h3>Program Assistant (NGO) - The Carter Center</h3>
      </a>
    </div>
    <div class="col-xs-12 description">
      KEY RESPONSIBILITIES: Provides administrative and logistical support for
      projects, initiatives, and programs within an NGO environment.
    </div>
    <div class="col-xs-12 additionalFields">
      <dl class="iCIMS_JobHeaderGroup">
        <div class="iCIMS_JobHeaderTag">
          <dt class="iCIMS_JobHeaderField">Job Type</dt>
          <dd class="iCIMS_JobHeaderData"><span>Temporary Full-Time</span></dd>
        </div>
        <div class="iCIMS_JobHeaderTag">
          <dt class="iCIMS_JobHeaderField">Department</dt>
          <dd class="iCIMS_JobHeaderData"><span>The Carter Center</span></dd>
        </div>
        <div class="iCIMS_JobHeaderTag">
          <dt class="iCIMS_JobHeaderField">
            <span class="sr-only field-label">Campus Location (For Posting) : Location</span>
          </dt>
          <dd class="iCIMS_JobHeaderData"><span>US-GA-Atlanta</span></dd>
        </div>
        <div class="iCIMS_JobHeaderTag">
          <dt class="iCIMS_JobHeaderField">
            <span class="sr-only field-label">Location : Name</span>
          </dt>
          <dd class="iCIMS_JobHeaderData"><span>Carter Center</span></dd>
        </div>
        <div class="iCIMS_JobHeaderTag">
          <dt class="iCIMS_JobHeaderField">Remote Work Classification</dt>
          <dd class="iCIMS_JobHeaderData"><span>N/A or Other</span></dd>
        </div>
      </dl>
    </div>
  </div>
</li>
</ul>
"""


def _rows(html=_CARD_HTML):
    provider = IcimsProvider()
    endpoint = Endpoint(vendor="icims", tenant="staff-emory",
                        host="staff-emory.icims.com")
    return list(provider._parse_rows(endpoint, "staff-emory.icims.com", html))


def test_title_excludes_the_screen_reader_field_label():
    """`<span class="sr-only">Job Title </span>` is chrome, not the title."""
    rows = _rows()
    assert len(rows) == 1
    assert rows[0].title == "Program Assistant (NGO) - The Carter Center"
    assert not rows[0].title.startswith("Job Title")


def test_coded_location_is_rewritten_for_matching():
    """`US-GA-Atlanta` matches no resume; "Atlanta, GA, US" does."""
    assert _rows()[0].location == "Atlanta, GA, US"


def test_coded_location_beats_the_site_name():
    """Both are published; "Carter Center" is a building, not a place."""
    assert "Carter Center" not in _rows()[0].location


def test_structured_header_fields_are_read():
    row = _rows()[0]
    assert row.employment_type == "Temporary Full-Time"
    assert row.department == "The Carter Center"
    assert len(row.description) > 40


def test_na_remote_classification_is_not_read_as_onsite():
    """"N/A or Other" is the portal declining to classify, not a No."""
    assert _rows()[0].remote is not False


def test_listing_does_not_claim_a_date_it_does_not_have():
    assert IcimsProvider().date_confidence == "unknown"
    assert _rows()[0].posted_at == ""


# ---------------------------------------------------------------------------
# Migrated portals
# ---------------------------------------------------------------------------

_MOVED_BODY = ("\n" * 30 + "<script type=\"text/javascript\">\n"
               "window.top.location.href = 'https:\\/\\/careers.rivian.com\\/jobs';\n"
               "</script>\n")


def test_a_migrated_portal_is_recognised_and_its_destination_kept():
    """HTTP 200 + a JS redirect is a MOVE, not an empty board.

    Read as "empty", such a tenant is re-crawled for ever and the employer is
    silently lost — their jobs moved to a vendor we do support.
    """
    assert IcimsProvider.moved_to(_MOVED_BODY) == "https://careers.rivian.com/jobs"


def test_a_real_listing_page_is_not_mistaken_for_a_redirect():
    """Live boards also contain `location.href` somewhere in their scripts."""
    big = _CARD_HTML + "<script>window.location.href='https://x.test/'</script>"
    assert IcimsProvider.moved_to(big) == ""


def test_bootstrap_raises_portal_moved(monkeypatch):
    provider = IcimsProvider()

    class _Ctx:
        cutoff_iso = None

        def should_stop(self):
            return False

        def log(self, *a, **k):
            pass

        def get_text(self, url, params=None, timeout=None, vendor=""):
            return 200, _MOVED_BODY

    with pytest.raises(PortalMovedError) as excinfo:
        provider._bootstrap(
            Endpoint(vendor="icims", tenant="us-careers-rivian",
                     host="us-careers-rivian.icims.com"), _Ctx())
    assert excinfo.value.destination == "https://careers.rivian.com/jobs"


# ---------------------------------------------------------------------------
# Fabricated posting dates
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 8, 18, 4, 56, 13, tzinfo=timezone.utc)


@pytest.mark.parametrize("value,expected,why", [
    # staff-emory: render time minus two years, sub-second precision.
    ("2024-08-18T04:54:40.821Z", True, "minted from the server clock"),
    # careers-fremontbank / careers-fresenius-kabi: genuine dates.
    ("2026-08-14T04:00:00.000Z", False, "real date, whole second"),
    ("2026-08-12T23:00:00.000Z", False, "real date, whole second"),
    ("2026-08-18", False, "bare date"),
    ("", False, "absent"),
    ("not a date", False, "unparsable"),
    # Sub-second precision alone is not enough; the time of day must match too.
    ("2026-08-17T19:22:11.500Z", False, "precise but unrelated hour"),
    ("2026-08-17T04:56:01.500Z", True, "precise AND matches our time of day"),
])
def test_render_time_stamps_are_rejected_and_real_dates_kept(value, expected,
                                                             why):
    assert looks_minted_now(value, now=_NOW) is expected, why


def test_a_rejected_date_is_dropped_not_replaced():
    """ADR-001: a missing date stays missing; nothing is substituted."""
    assert looks_minted_now("2024-08-18T04:54:40.821Z", now=_NOW) is True
    # The provider's contract on rejection is an empty string, never `now()`.
    job = RawJob(source_job_id="1", title="t", apply_url="u")
    assert job.posted_at == ""
    assert job.date_confidence == "unknown"
