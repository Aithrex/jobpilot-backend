"""A listing teaser is not the job description.

Measured on the live inventory 2026-10-06: 62% of Oracle and 81% of iCIMS
engineering postings were "too thin to score" and capped at 40, because the
130-530 character blurb their listings print was stored as the description and
the real one was never fetched.
"""
from datetime import datetime, timezone

import pytest

from services import job_ingest_worker as w
from services import job_inventory_service as inv

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)
TEASER = "Join our team to build cloud services used by millions of people. " * 2
BODY = ("Requirements: 3+ years of Python, AWS and Kubernetes. Bachelor's degree. " * 40)

ORACLE = {"source": "career", "vendor": "oracle_orc", "tenant": "acme", "site": "CX"}
ICIMS = {"source": "career", "vendor": "icims", "tenant": "acme"}
PAYLOCITY = {"source": "career", "vendor": "paylocity", "tenant": "acme"}
APPLE = {"source": "career", "vendor": "custom_apple", "tenant": "apple"}
LEVER = {"source": "career", "ats": "lever", "slug": "acme"}


@pytest.fixture()
def db():
    d = inv._db()
    d.job_postings.delete_many({})
    return d


def _row(desc=TEASER, jid="R1"):
    return {"source_job_id": jid, "title": "Software Engineer", "description": desc,
            "_detail_ref": jid}


def _raw(jid, body, **extra):
    raw = {"source": "career", "source_job_id": jid, "title": "Software Engineer",
           "employer_name": "Acme", "employer_id": "acme", "url": f"https://x.test/{jid}",
           "description": body, "locations": ["Austin, TX"],
           "employment_type": "Full time", "posted_at": NOW,
           "cost_class": "free", "source_quality": "direct"}
    raw.update(extra)
    return raw


class TestATeaserStillNeedsItsDetail:
    @pytest.mark.parametrize("member", [ORACLE, ICIMS, PAYLOCITY, APPLE])
    def test_a_teaser_vendor_with_a_detail_route(self, member):
        assert len(TEASER) >= w.MIN_USABLE_DESCRIPTION_CHARS
        assert not w._listing_description_is_complete(_row(), member)
        assert w._needs_http_detail(_row(), member)

    def test_a_long_listing_body_is_a_body(self):
        assert w._listing_description_is_complete(_row(BODY), ORACLE)
        assert not w._needs_http_detail(_row(BODY), ORACLE)

    def test_a_body_in_listing_vendor_is_untouched(self):
        assert w._listing_description_is_complete(_row("Short but whole."), LEVER)
        assert not w._needs_http_detail(_row("Short but whole."), LEVER)


class TestAStoredTeaserIsFetchedAgain:
    def test_marked_teaser(self):
        assert w._owes_a_detail_fetch({"body_state": "teaser", "description_chars": 130},
                                      _row(), ORACLE)

    def test_stored_before_the_marker_is_judged_by_size(self):
        assert w._owes_a_detail_fetch({"description_chars": 130}, _row(), ORACLE)
        assert not w._owes_a_detail_fetch({"description_chars": 6000}, _row(), ORACLE)

    def test_a_fetched_body_is_never_asked_for_again(self):
        assert not w._owes_a_detail_fetch({"body_state": "full", "description_chars": 90},
                                          _row(), ORACLE)

    def test_it_gives_up_after_the_attempt_limit(self):
        stored = {"body_state": "teaser", "detail_attempts": w.DETAIL_MAX_ATTEMPTS}
        assert not w._owes_a_detail_fetch(stored, _row(), ORACLE)

    def test_no_route_means_nothing_is_owed(self):
        assert not w._owes_a_detail_fetch({"body_state": "teaser"}, _row(), LEVER)


class TestTheStoredBodySurvivesATeaser:
    def test_state_and_attempts_are_recorded(self, db):
        inv.upsert_posting(_raw("T1", TEASER, body_state="teaser", detail_attempted=True), now=NOW)
        inv.upsert_posting(_raw("T1", TEASER, body_state="teaser", detail_attempted=True), now=NOW)
        doc = db.job_postings.find_one({"source_job_id": "T1"})
        assert doc["body_state"] == "teaser" and doc["detail_attempts"] == 2
        stored = inv.get_postings_map("career", ["T1"])["T1"]
        assert stored["body_state"] == "teaser" and stored["detail_attempts"] == 2

    def test_a_deferred_teaser_is_not_an_attempt(self, db):
        inv.upsert_posting(_raw("T2", TEASER, body_state="teaser"), now=NOW)
        assert "detail_attempts" not in db.job_postings.find_one({"source_job_id": "T2"})

    def test_the_detail_replaces_the_teaser(self, db):
        inv.upsert_posting(_raw("T3", TEASER, body_state="teaser"), now=NOW)
        out = inv.upsert_posting(_raw("T3", BODY, body_state="full"), now=NOW)
        doc = db.job_postings.find_one({"source_job_id": "T3"})
        assert out["content_changed"] and out["needs_parse"]
        assert doc["body_state"] == "full" and doc["description_chars"] > 1200

    def test_a_teaser_never_replaces_a_fetched_body(self, db):
        first = inv.upsert_posting(_raw("T4", BODY, body_state="full"), now=NOW)
        out = inv.upsert_posting(_raw("T4", TEASER, body_state="teaser"), now=NOW)
        doc = db.job_postings.find_one({"source_job_id": "T4"})
        assert out.get("kept_stored_body") and not out["content_changed"]
        assert doc["content_hash"] == first["content_hash"]
        assert doc["body_state"] == "full" and doc["description_chars"] > 1200

    def test_nor_a_longer_body_stored_before_the_marker(self, db):
        first = inv.upsert_posting(_raw("T5", BODY), now=NOW)
        out = inv.upsert_posting(_raw("T5", TEASER, body_state="teaser"), now=NOW)
        assert out.get("kept_stored_body")
        assert db.job_postings.find_one({"source_job_id": "T5"})["content_hash"] == first["content_hash"]


class TestAConfigBoardCanDeclareADetail:
    """Apple's search API prints a pitch; the qualifications are on the posting."""

    def _provider(self):
        from services.ats_providers.config_boards import ConfigBoardProvider
        from services.official_career_registry import CUSTOM_BOARD_ADAPTERS
        row = next(r for r in CUSTOM_BOARD_ADAPTERS if r["key"] == "apple")
        return ConfigBoardProvider(row)

    def test_apple_declares_one(self):
        provider = self._provider()
        assert provider.has_detail

    def test_a_row_without_one_is_unchanged(self):
        from services.ats_providers.config_boards import ConfigBoardProvider
        provider = ConfigBoardProvider({"key": "x", "vendor": "custom_x"})
        assert not provider.has_detail

    def test_the_body_keeps_its_section_headings(self):
        from services.ats_providers.base import RawJob

        provider = self._provider()
        seen = []

        class Ctx:
            def get_json(self, url, **kwargs):
                seen.append(url)
                return 200, {"res": {
                    "jobSummary": "Imagine what you could do here.",
                    "description": "<p>You will build compilers.</p>",
                    "minimumQualifications": "BS in Computer Science\n3+ years of C++",
                    "preferredQualifications": "Experience with LLVM"}}

        job = RawJob(source_job_id="200313970", title="Compiler Engineer",
                     apply_url="https://jobs.apple.com/en-us/details/200313970")
        out = provider.fetch_detail(provider.default_endpoint(), job, Ctx())
        assert seen == ["https://jobs.apple.com/api/v1/jobDetails/200313970?locale=en-us"]
        assert "Minimum Qualifications\nBS in Computer Science" in out.description
        assert "Preferred Qualifications\nExperience with LLVM" in out.description
        assert "build compilers" in out.description and "<p>" not in out.description

    def test_a_failed_request_keeps_the_listing(self):
        from services.ats_providers.base import RawJob

        class Ctx:
            def get_json(self, url, **kwargs):
                return 404, None

        provider = self._provider()
        job = RawJob(source_job_id="1", title="T", apply_url="https://jobs.apple.com/x")
        assert provider.fetch_detail(provider.default_endpoint(), job, Ctx()) is None
