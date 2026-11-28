"""Browser Radar, one posting from any job page (`radar-3`).

A signed-in person opens one job posting; the extension tells the server.
Any account may send, so these tests pin what keeps that honest
(docs/specs/browser-radar.md, section `radar-3`):

* identity is read from the URL on the server, never from the body;
* a known board is written through the batch's own guarded path;
* everything else is staged and never reaches `job_postings`;
* every refusal is 400/403/409/429, never 401, and the limits are atomic.

Everything runs on mongomock through the shared fixtures. No request leaves
the process: the code under test has no fetch to make.
"""
import json
import threading
from datetime import datetime, timedelta, timezone

import pytest

from _vg_support import current_db, load_service_app

from services import browser_radar_fleet as fleet
from services import browser_radar_posting as posting
from services import browser_radar_service as radar
from services import job_ingest_worker as w
from services import job_inventory_service as inv
from services.ats_providers import registry as ats

A = "ann@example.com"
B = "bob@example.com"
C = "cy@example.com"
NOW = datetime.now(timezone.utc).replace(microsecond=0)

COLLECTIONS = ("job_postings", "job_source_state", "job_ingestion_runs", "job_parse_queue",
               "posting_sightings", radar.BATCHES, radar.USAGE, radar.UNKNOWN_BOARDS,
               radar.LEASES, radar.COVERAGE, radar.FLEET, radar.STAGED, "users")

BODY_HTML = ("<p>Requirements: 3+ years of Python and AWS. Bachelor's degree in Computer "
             "Science. You will build backend services and APIs.</p>") * 20
UUID = "0f3c2a1b-1111-2222-3333-444455556666"


def _career(vendor, slug, name):
    """A member as the in-repo career catalog makes it."""
    return {"member_key": f"{vendor}-{slug}", "source": w.SOURCE_CAREER, "vendor": vendor,
            "ats": vendor, "slug": slug, "display_name": name, "industry": "tech"}


WORKDAY = [{"member_key": "acme", "source": w.SOURCE_WORKDAY, "vendor": "workday",
            "tenant": "acme", "wd_host": "wd5", "site": "External",
            "display_name": "Acme Corp", "industry": "tech"}]
CAREER = [
    _career("greenhouse", "acme", "Acme Corp"),
    _career("lever", "acme", "Acme Corp"),
    _career("ashby", "acme", "Acme Corp"),
    _career("workable", "acme", "Acme Corp"),
    _career("neogov", "lacity", "City of Los Angeles"),
    w._endpoint_member("smartrecruiters", tenant="Visa", display_name="Visa"),
    w._endpoint_member("jobvite", tenant="acme", display_name="Acme Corp"),
    w._endpoint_member("bamboohr", tenant="acme", host="acme.bamboohr.com",
                       display_name="Acme Corp"),
    w._endpoint_member("successfactors", tenant="jobs.supermicro.com",
                       host="jobs.supermicro.com", display_name="Supermicro"),
    w._endpoint_member("greenhouse", tenant="cognite", site="eu", display_name="Cognite"),
]
ORACLE = [w._endpoint_member("oracle_orc", tenant="eeho", site="CX_1",
                             host="eeho.fa.us2.oraclecloud.com", display_name="Oracle Co")]
ICIMS = [w._endpoint_member("icims", tenant="careers-acme", host="careers-acme.icims.com",
                            display_name="Acme Corp")]
REGISTRY = {w.SOURCE_WORKDAY: WORKDAY, w.SOURCE_CAREER: CAREER,
            w.SOURCE_ORACLE: ORACLE, w.SOURCE_ICIMS: ICIMS}

WD_URL = ("https://acme.wd5.myworkdayjobs.com/en-US/External/job/US-CA-Santa-Clara/"
          "Senior-Backend-Engineer_JR1001?source=LinkedIn&utm_campaign=x")
WD_APPLY = ("https://acme.wd5.myworkdayjobs.com/External/job/US-CA-Santa-Clara/"
            "Senior-Backend-Engineer_JR1001")
GH_URL = "https://job-boards.greenhouse.io/acme/jobs/8945798002?gh_src=abc123"
GH_APPLY = "https://job-boards.greenhouse.io/acme/jobs/8945798002"
SITE_URL = "https://careers.widgetco.com/openings/staff-engineer-42?utm_source=x#apply"


@pytest.fixture()
def db(monkeypatch):
    d = current_db()
    for c in COLLECTIONS:
        d[c].delete_many({})
    radar.reset_stats_cache()
    monkeypatch.setattr(w, "registry_members",
                        lambda source: list(REGISTRY.get(w.normalize_source(source), [])))
    for name in ("BROWSER_RADAR_INGEST", "BROWSER_RADAR_ENABLED", "BROWSER_RADAR_CONTRIBUTORS",
                 "BROWSER_RADAR_CONTRIBUTORS_SCOPE"):
        monkeypatch.delenv(name, raising=False)
    yield d
    for c in COLLECTIONS:
        d[c].delete_many({})
    radar.reset_stats_cache()


def _body(url=GH_URL, *, title="Senior Backend Engineer", description=BODY_HTML,
          date_posted=None, **extra):
    out = {"url": url, "title": title, "company": "Acme Corp",
           "location": "Santa Clara, CA", "description": description,
           "date_posted": (NOW - timedelta(days=1)).date().isoformat()
           if date_posted is None else date_posted,
           "employment_type": "FULL_TIME",
           "client": {"name": "applypilot", "version": "0.6.0"}}
    out.update(extra)
    return out


def _json_ld(**extra):
    node = {"@context": "https://schema.org", "@type": "JobPosting",
            "title": "Staff Engineer", "datePosted": "2026-11-20",
            "hiringOrganization": {"@type": "Organization", "name": "WidgetCo"},
            "description": "<p>Build widgets.</p>"}
    node.update(extra)
    return node


def _send(email=A, now=NOW, **kw):
    return posting.ingest_posting(email, _body(**kw), now=now)


def _look(url, email=A, now=NOW):
    return posting.lookup_posting(email, {"url": url}, now=now)


def _doc(source, sjid):
    return inv.get_posting(source, sjid)


def _aware(value):
    return value if value is None or value.tzinfo else value.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# The id, from the URL, per vendor
# ---------------------------------------------------------------------------

#: (page URL, vendor, raw id, the apply link the server stores). Each URL is
#: the shape that vendor's own provider module documents or mints.
POSTING_URLS = [
    (WD_URL, "workday", "JR1001", WD_APPLY),
    ("https://acme.wd5.myworkdayjobs.com/External/job/Austin/Data-Engineer_R-00123-1",
     "workday", "R-00123-1",
     "https://acme.wd5.myworkdayjobs.com/External/job/Austin/Data-Engineer_R-00123-1"),
    (GH_URL, "greenhouse", "8945798002", GH_APPLY),
    ("https://boards.greenhouse.io/acme/jobs/8945798002", "greenhouse", "8945798002", GH_APPLY),
    # The fixture board's own `absolute_url` shape (tests/fixtures/greenhouse_eu).
    ("https://job-boards.eu.greenhouse.io/cognite/jobs/4908731101", "greenhouse",
     "4908731101", "https://job-boards.eu.greenhouse.io/cognite/jobs/4908731101"),
    (f"https://jobs.lever.co/acme/{UUID}?lever-source=LinkedIn", "lever", UUID,
     f"https://jobs.lever.co/acme/{UUID}"),
    (f"https://jobs.ashbyhq.com/acme/{UUID}", "ashby", UUID,
     f"https://jobs.ashbyhq.com/acme/{UUID}"),
    ("https://jobs.smartrecruiters.com/Visa/744000158320111-senior-engineer",
     "smartrecruiters", "744000158320111",
     "https://jobs.smartrecruiters.com/Visa/744000158320111"),
    # The widget's own link for a job is the short form (provider test data).
    ("https://apply.workable.com/acme/j/AADE688F5F/", "workable", "AADE688F5F",
     "https://apply.workable.com/j/AADE688F5F"),
    ("https://www.governmentjobs.com/careers/lacity/jobs/4567890/rate-analyst",
     "neogov", "4567890", "https://www.governmentjobs.com/careers/lacity/jobs/4567890"),
    ("https://jobs.jobvite.com/acme/job/oAbCdfw1", "jobvite", "oAbCdfw1",
     "https://jobs.jobvite.com/acme/job/oAbCdfw1"),
    ("https://acme.bamboohr.com/careers/45", "bamboohr", "45",
     "https://acme.bamboohr.com/careers/45"),
    ("https://careers-acme.icims.com/jobs/1234/senior-engineer/job?mode=job&iis=LinkedIn"
     "&hashed=-123", "icims", "1234",
     "https://careers-acme.icims.com/jobs/1234/senior-engineer/job"),
    ("https://eeho.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/210012345",
     "oracle_orc", "210012345",
     "https://eeho.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/210012345"),
    # A board on the employer's own host, found by the registry member's host.
    # No builder: the page URL without its query.
    ("https://jobs.supermicro.com/job/San-Jose-Firmware-Engineer-CA/1234567/?feedId=1",
     "successfactors", "1234567",
     "https://jobs.supermicro.com/job/San-Jose-Firmware-Engineer-CA/1234567/"),
]

#: Pages on a vendor's own host that are NOT a single posting.
NOT_POSTINGS = [
    "https://acme.wd5.myworkdayjobs.com/en-US/External",                    # the search page
    "https://acme.wd5.myworkdayjobs.com/External/job/Austin/Engineer_JR1/apply",
    "https://acme.wd5.myworkdayjobs.com/External/job/Austin/Engineer",      # no requisition id
    "https://job-boards.greenhouse.io/acme",                                # the listing
    "https://job-boards.greenhouse.io/acme/jobs/abc",
    f"https://jobs.lever.co/acme/{UUID}/apply",                             # the form
    "https://jobs.lever.co/acme",
    f"https://jobs.ashbyhq.com/acme/{UUID}/application",
    "https://careers.smartrecruiters.com/Visa",
    "https://apply.workable.com/acme/",
    "https://apply.workable.com/acme/j/AADE688F5F/apply/",
    "https://www.governmentjobs.com/careers/lacity",
    "https://careers-acme.icims.com/jobs/search",
    "https://careers-acme.icims.com/jobs/1234/login",
    "https://eeho.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/requisitions",
    "https://eeho.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/2100/apply/x",
    "https://jobs.supermicro.com/search/",
]


class TestIdFromUrl:
    @pytest.mark.parametrize("url,vendor,raw_id,apply_url", POSTING_URLS)
    def test_the_id_and_the_link_come_from_the_url(self, db, url, vendor, raw_id, apply_url):
        got = posting.resolve(posting.clean_url(url))
        assert got["vendor"] == vendor
        assert got["member"] is not None, "the board is in the test registry"
        assert got["raw_id"] == raw_id
        assert got["apply_url"] == apply_url
        assert "utm" not in got["apply_url"] and "hashed" not in got["apply_url"]

    @pytest.mark.parametrize("url", NOT_POSTINGS)
    def test_a_listing_or_an_apply_step_is_not_a_posting(self, db, url):
        got = posting.resolve(posting.clean_url(url))
        assert got["raw_id"] == ""
        out = _look(url)
        assert out["supported"] is False and out["want"] == "nothing"
        assert out["known"] is False and out["job_id"] is None

    def test_the_method_is_the_providers_own(self, db):
        """Each supported vendor reads its own URL shape, on its own class."""
        from services.ats_providers.base import AtsProvider, Endpoint
        supported = {p.vendor for p in ats.all_providers(enabled_only=False)
                     if type(p).posting_id_from_url is not AtsProvider.posting_id_from_url}
        assert supported == {"workday", "greenhouse", "lever", "ashby", "smartrecruiters",
                             "workable", "neogov", "jobvite", "bamboohr", "icims",
                             "oracle_orc", "successfactors"}
        # Recruitee's URL carries a slug and Taleo's `job=` may be the contest
        # number; neither is provably the id the crawl stores.
        for vendor in ("recruitee", "taleo"):
            provider = ats.get(vendor)
            assert provider.posting_id_from_url(Endpoint(vendor=vendor, tenant="acme"),
                                                "https://acme.recruitee.com/o/engineer") == ""

    def test_the_id_is_the_one_the_crawl_lists(self, db, monkeypatch):
        """Greenhouse, Lever, Ashby: the id in the URL is the `id` the board
        API lists, so a browser posting and a crawled one share a
        `source_job_id` (and so a `job_id`)."""
        api = {
            "boards-api.greenhouse.io": {"jobs": [{
                "id": 8945798002, "title": "Senior Backend Engineer",
                "absolute_url": GH_APPLY,
                "first_published": (NOW - timedelta(days=1)).isoformat(),
                "location": {"name": "Santa Clara, CA"}}]},
            "api.lever.co": [{
                "id": UUID, "text": "Senior Backend Engineer",
                "hostedUrl": f"https://jobs.lever.co/acme/{UUID}",
                "createdAt": int((NOW - timedelta(days=1)).timestamp() * 1000),
                "categories": {"location": "Santa Clara, CA"},
                "descriptionPlain": "Build backend services."}],
            "api.ashbyhq.com": {"jobs": [{
                "id": UUID, "title": "Senior Backend Engineer",
                "jobUrl": f"https://jobs.ashbyhq.com/acme/{UUID}",
                "publishedAt": (NOW - timedelta(days=1)).isoformat(),
                "location": "Santa Clara, CA", "descriptionPlain": "Build backend services."}]},
        }

        def fake_http(source, url, policy, **_kw):
            return next(payload for host, payload in api.items() if host in url)

        monkeypatch.setattr(w, "http_get_json", fake_http)
        for vendor, page in (("greenhouse", GH_URL),
                             ("lever", f"https://jobs.lever.co/acme/{UUID}"),
                             ("ashby", f"https://jobs.ashbyhq.com/acme/{UUID}")):
            member = next(m for m in CAREER if m["member_key"] == f"{vendor}-acme")
            crawled = w.list_member(dict(member), cutoff=NOW - timedelta(days=7),
                                    policy=w.HostPolicy(min_interval=0, jitter=0),
                                    deadline=1e18, now=NOW)
            assert len(crawled) == 1
            out = _send(url=page)
            assert out["result"] == "created", vendor
            doc = _doc(w.SOURCE_CAREER, crawled[0]["source_job_id"])
            assert doc is not None, vendor
            assert doc["job_id"] == out["job_id"]
            assert doc["canonical_url"] == crawled[0]["url"]
            assert doc["employer_id"] == crawled[0]["employer_id"]


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------

class TestLookup:
    def test_an_unknown_posting_on_a_known_board_wants_the_body(self, db):
        assert _look(GH_URL) == {"supported": True, "vendor": "greenhouse",
                                 "known_board": True, "known": False, "job_id": None,
                                 "want": "body"}

    def test_a_board_the_registry_lacks_wants_the_body(self, db):
        out = _look("https://job-boards.greenhouse.io/strangers/jobs/5550001")
        assert out == {"supported": True, "vendor": "greenhouse", "known_board": False,
                       "known": False, "job_id": None, "want": "body"}

    def test_a_site_no_provider_matches_is_unsupported(self, db):
        assert _look(SITE_URL) == {"supported": False, "vendor": "", "known_board": False,
                                   "known": False, "job_id": None, "want": "nothing"}

    def test_a_fresh_posting_with_a_body_wants_nothing(self, db):
        sent = _send()
        out = _look(GH_URL, now=NOW + timedelta(hours=11))
        assert out["known"] is True and out["want"] == "nothing"
        assert out["job_id"] == sent["job_id"]
        assert out["job_id"] == inv.posting_job_id(w.SOURCE_CAREER, "greenhouse-acme:8945798002")
        assert len(out["job_id"]) == 32

    def test_a_stale_posting_with_a_body_wants_a_sighting(self, db):
        _send()
        out = _look(GH_URL, now=NOW + timedelta(hours=13))
        assert out["known"] is True and out["want"] == "sighting"

    def test_a_teaser_wants_the_body(self, db):
        assert _send(description="Short blurb.")["result"] == "created"
        assert _doc(w.SOURCE_CAREER, "greenhouse-acme:8945798002")["body_state"] == "teaser"
        out = _look(GH_URL)
        assert out["known"] is True and out["want"] == "body"

    def test_a_closed_posting_wants_a_sighting_unless_its_page_said_gone(self, db):
        _send()
        sjid = "greenhouse-acme:8945798002"
        assert inv.close_posting(w.SOURCE_CAREER, sjid, reason="unverified", now=NOW)
        assert _look(GH_URL)["want"] == "sighting"
        db.job_postings.update_one({"source_job_id": sjid}, {"$set": {"closed_reason": "detail-gone"}})
        out = _look(GH_URL)
        assert out["known"] is True and out["want"] == "nothing"

    def test_lookup_writes_nothing_but_its_counter(self, db):
        _look(GH_URL)
        assert db.job_postings.count_documents({}) == 0
        assert db[radar.STAGED].count_documents({}) == 0
        assert db[radar.BATCHES].count_documents({}) == 0
        usage = db[radar.USAGE].find_one({"_id": {"$regex": r"\|pl\|"}})
        assert usage["lookups"] == 1


# ---------------------------------------------------------------------------
# A known board
# ---------------------------------------------------------------------------

class TestKnownBoard:
    def test_a_new_posting_is_written_in_the_crawls_shape(self, db):
        out = _send()
        assert out == {"result": "created", "reason": "",
                       "job_id": inv.posting_job_id(w.SOURCE_CAREER,
                                                    "greenhouse-acme:8945798002")}
        doc = _doc(w.SOURCE_CAREER, "greenhouse-acme:8945798002")
        assert doc["origin"] == "browser" and doc["status"] == "active"
        assert doc["body_state"] == "full" and int(doc["description_chars"]) > 500
        assert doc["canonical_url"] == GH_APPLY
        assert doc["title_raw"] == "Senior Backend Engineer"
        assert (doc["ats"], doc["tenant"]) == ("greenhouse", "acme")
        assert doc["cost_class"] == inv.COST_CLASS_FREE
        # Requirements are read the way the crawl reads them.
        assert (doc.get("requirements") or {}).get("extractor_version") == inv.EXTRACTOR_VERSION
        # Greenhouse's crawled row carries no employment type, so neither does this.
        assert doc["employment_type"] == ""

    def test_the_employer_is_the_registrys_not_the_bodys(self, db):
        """A body claiming another company, tenant, link or id changes nothing."""
        out = _send(company="Evil Corp", employer_name="Evil Corp", employer_id="evil-corp",
                    tenant="evil", source_job_id="greenhouse-evil:1", apply_url="https://evil.test/x",
                    canonical_url="https://evil.test/x", source="workday", ats="lever",
                    canonical_employer_id="emp_000000000000", job_id="f" * 32)
        assert out["result"] == "created"
        assert db.job_postings.count_documents({}) == 1
        doc = db.job_postings.find_one({})
        assert doc["source_job_id"] == "greenhouse-acme:8945798002"
        assert doc["source"] == w.SOURCE_CAREER
        assert (doc["employer_name"], doc["employer_id"]) == ("Acme Corp", "acme-corp")
        assert doc["canonical_url"] == GH_APPLY and doc["job_id"] != "f" * 32
        assert "evil" not in json.dumps(doc, default=str).lower()

    def test_a_held_posting_is_touched(self, db):
        _send()
        first = _doc(w.SOURCE_CAREER, "greenhouse-acme:8945798002")
        later = NOW + timedelta(hours=14)
        out = _send(now=later, description="")            # a bare sighting
        assert out == {"result": "touched", "job_id": first["job_id"], "reason": ""}
        after = _doc(w.SOURCE_CAREER, "greenhouse-acme:8945798002")
        assert _aware(after["last_seen_at"]) == later
        assert after["content_hash"] == first["content_hash"]

    def test_a_teaser_gets_its_body(self, db):
        _send(description="Short blurb.")
        first = _doc(w.SOURCE_CAREER, "greenhouse-acme:8945798002")
        out = _send(email=B, now=NOW + timedelta(hours=1))
        assert out["result"] == "updated" and out["job_id"] == first["job_id"]
        after = _doc(w.SOURCE_CAREER, "greenhouse-acme:8945798002")
        assert after["body_state"] == "full" and int(after["description_chars"]) > 500

    def test_a_fetched_body_is_never_replaced(self, db):
        _send()
        first = _doc(w.SOURCE_CAREER, "greenhouse-acme:8945798002")
        out = _send(email=B, now=NOW + timedelta(hours=1),
                    title="Senior Backend Engineer (pay a fee to apply)", location="Remote",
                    description="<p>Message recruiter on a chat app and pay the fee.</p>" * 40)
        assert out["result"] == "touched"
        after = _doc(w.SOURCE_CAREER, "greenhouse-acme:8945798002")
        for field in ("content_hash", "title_raw", "locations", "canonical_url",
                      "description_chars", "posted_at"):
            assert after[field] == first[field], field

    def test_a_held_teaser_keeps_its_title_link_place_and_date(self, db):
        _send(description="Short blurb.")
        first = _doc(w.SOURCE_CAREER, "greenhouse-acme:8945798002")
        out = _send(email=B, now=NOW + timedelta(hours=1), title="Something Else Entirely",
                    location="Lagos", date_posted=NOW.date().isoformat(),
                    employment_type="CONTRACTOR")
        assert out["result"] == "updated"
        after = _doc(w.SOURCE_CAREER, "greenhouse-acme:8945798002")
        assert after["body_state"] == "full"
        for field in ("title_raw", "canonical_url", "locations", "posted_at",
                      "employment_type", "detail_ref"):
            assert after.get(field) == first.get(field), field

    @pytest.mark.parametrize("reason", ["detail-gone", "link-dead"])
    def test_a_posting_its_own_page_closed_stays_closed(self, db, reason):
        _send()
        sjid = "greenhouse-acme:8945798002"
        assert inv.close_posting(w.SOURCE_CAREER, sjid, reason=reason, now=NOW)
        out = _send(email=B, now=NOW + timedelta(hours=1))
        assert (out["result"], out["reason"]) == ("ignored", "closed")
        assert _doc(w.SOURCE_CAREER, sjid)["status"] != "active"

    def test_a_posting_closed_for_another_reason_reopens(self, db):
        _send()
        sjid = "greenhouse-acme:8945798002"
        assert inv.close_posting(w.SOURCE_CAREER, sjid, reason="unverified", now=NOW)
        out = _send(email=B, now=NOW + timedelta(hours=1), description="")
        assert (out["result"], out["reason"]) == ("updated", "reopened")
        assert _doc(w.SOURCE_CAREER, sjid)["status"] == "active"

    def test_nothing_is_ever_closed(self, db):
        _send()
        _send(url="https://job-boards.greenhouse.io/acme/jobs/8945798003", title="Data Engineer")
        assert {d["status"] for d in db.job_postings.find({})} == {"active"}

    def test_the_age_rules_are_the_crawls(self, db):
        old = (NOW - timedelta(days=20)).date().isoformat()
        out = _send(date_posted=old)
        assert (out["result"], out["reason"], out["job_id"]) == ("ignored", "aged_out", None)
        # An early-career title is kept for 30 days, as in the crawl.
        out = _send(url="https://job-boards.greenhouse.io/acme/jobs/8945798009",
                    title="Software Engineering Intern", date_posted=old)
        assert out["result"] == "created"
        assert db.job_postings.count_documents({}) == 1

    @pytest.mark.parametrize("stated", ["", "soon", "2099-01-01"])
    def test_a_board_whose_crawl_skips_undated_rows_gets_none_from_a_browser(self, db, stated):
        """Greenhouse's crawl skips a job with no `first_published`. A date in
        the future is not believed, so it is no date."""
        out = _send(date_posted=stated)
        assert (out["result"], out["reason"]) == ("ignored", "undated")
        assert db.job_postings.count_documents({}) == 0

    def test_a_provider_listed_board_keeps_an_undated_row_as_its_crawl_does(self, db):
        out = _send(url="https://acme.bamboohr.com/careers/45", date_posted="")
        assert out["result"] == "created"
        doc = _doc(w.SOURCE_CAREER, "bamboohr-acme:45")
        assert doc["posted_at"] is None
        assert doc["posted_at_confidence"] == inv.CONFIDENCE_UNKNOWN
        assert doc["canonical_url"] == "https://acme.bamboohr.com/careers/45"
        assert doc["detail_ref"] == "https://acme.bamboohr.com/careers/45/detail"
        assert doc["employment_type"] == inv._safe_key("FULL_TIME", 40) != ""

    def test_a_posting_without_a_title_is_not_written(self, db):
        out = _send(title="   ")
        assert (out["result"], out["reason"]) == ("ignored", "no_title")

    def test_script_text_in_a_description_is_not_stored(self, db):
        _send(description=BODY_HTML + "<script>steal(document.cookie)</script>")
        doc = _doc(w.SOURCE_CAREER, "greenhouse-acme:8945798002")
        assert "steal" not in inv.stored_description(w.SOURCE_CAREER, doc)

    def test_the_batch_row_lists_what_it_wrote(self, db):
        _send()
        row = db[radar.BATCHES].find_one({"kind": "posting"})
        assert row["user_email"] == A and row["mode"] == "on" and row["sent"] == 1
        assert row["written"] == [{"id": "greenhouse-acme:8945798002", "did": "created"}]
        assert row["boards"][0]["vendor"] == "greenhouse" and row["boards"][0]["tenant"] == "acme"
        assert row["response"]["postings"]["created"] == 1
        assert "url" not in row and "description" not in json.dumps(row, default=str)
        assert _aware(row["expire_at"]) == NOW + timedelta(days=30)


class TestWorkday:
    def test_a_new_workday_posting(self, db):
        out = _send(url=WD_URL)
        assert out["result"] == "created"
        doc = _doc("workday", "acme:JR1001")
        assert doc["canonical_url"] == WD_APPLY           # locale and query dropped
        assert doc["detail_ref"] == "job/US-CA-Santa-Clara/Senior-Backend-Engineer_JR1001"
        assert doc["employer_name"] == "Acme Corp" and doc["origin"] == "browser"
        assert doc["employment_type"] == inv._safe_key("FULL_TIME", 40) != ""

    def test_the_row_matches_a_radar_batch_row_for_the_same_posting(self, db):
        """Identity, employer, link and detail address are the ones a batch
        (and so the crawl) writes."""
        _send(url=WD_URL)
        single = _doc("workday", "acme:JR1001")
        db.job_postings.delete_many({})
        db.posting_sightings.delete_many({})
        listed = {"title": "Senior Backend Engineer",
                  "externalPath": "/job/US-CA-Santa-Clara/Senior-Backend-Engineer_JR1001",
                  "bulletFields": ["JR1001"], "postedOn": "Posted Yesterday",
                  "locationsText": "Santa Clara, CA", "timeType": "FULL_TIME"}
        radar.ingest_batch(A, {"batch_id": "batch-parity-01", "boards": [{
            "tenant": "acme", "wd_host": "wd5", "site": "External", "search_text": "",
            "postings": [listed],
            "details": [{"externalPath": listed["externalPath"],
                         "jobPostingInfo": {"jobDescription": BODY_HTML}}]}]}, now=NOW)
        batch = _doc("workday", "acme:JR1001")
        for field in ("job_id", "source_job_id", "canonical_url", "detail_ref", "employer_id",
                      "employer_name", "tenant", "ats", "title_raw", "locations",
                      "employment_type", "content_hash", "dedupe_group_id", "body_state"):
            assert single.get(field) == batch.get(field), field

    def test_a_posting_stored_under_another_id_is_not_twinned(self, db):
        """The crawl keyed this requisition on `bulletFields[0]` = REQ-77,
        and its URL ends `_JR1001`. Same page, so same posting."""
        listed = {"title": "Senior Backend Engineer",
                  "externalPath": "/job/US-CA-Santa-Clara/Senior-Backend-Engineer_JR1001",
                  "bulletFields": ["REQ-77"], "postedOn": "Posted Today",
                  "locationsText": "Santa Clara, CA", "timeType": "Full time"}
        radar.ingest_batch(B, {"batch_id": "batch-twin-0001", "boards": [{
            "tenant": "acme", "wd_host": "wd5", "site": "External", "search_text": "",
            "postings": [listed], "details": []}]}, now=NOW)
        held = _doc("workday", "acme:REQ-77")
        assert held["body_state"] == "teaser"

        look = _look(WD_URL)
        assert look["known"] is True and look["job_id"] == held["job_id"]
        assert look["want"] == "body"

        out = _send(url=WD_URL, now=NOW + timedelta(hours=1))
        assert out["result"] == "updated" and out["job_id"] == held["job_id"]
        assert db.job_postings.count_documents({}) == 1
        assert _doc("workday", "acme:JR1001") is None
        after = _doc("workday", "acme:REQ-77")
        assert after["body_state"] == "full" and after["title_raw"] == held["title_raw"]

        out = _send(url=WD_URL, now=NOW + timedelta(hours=2), description="")
        assert out["result"] == "touched"
        assert db.job_postings.count_documents({}) == 1

    def test_an_undated_workday_posting_is_not_created(self, db):
        out = _send(url=WD_URL, date_posted="")
        assert (out["result"], out["reason"]) == ("ignored", "undated")

    def test_an_unknown_workday_board_is_staged(self, db):
        out = _send(url="https://nobody.wd1.myworkdayjobs.com/Careers/job/Austin/Engineer_R1")
        assert (out["result"], out["reason"]) == ("staged", "unknown_board")
        assert db.job_postings.count_documents({}) == 0


# ---------------------------------------------------------------------------
# Staging
# ---------------------------------------------------------------------------

class TestStaging:
    UNKNOWN = "https://job-boards.greenhouse.io/strangers/jobs/5550001?gh_src=q"

    def test_an_unknown_board_is_staged_never_written(self, db):
        out = _send(url=self.UNKNOWN, company="Strangers Inc")
        assert out == {"result": "staged", "job_id": None, "reason": "unknown_board"}
        assert db.job_postings.count_documents({}) == 0
        row = db[radar.STAGED].find_one({})
        assert row["url"] == "https://job-boards.greenhouse.io/strangers/jobs/5550001"
        assert (row["host"], row["vendor"]) == ("job-boards.greenhouse.io", "greenhouse")
        assert (row["tenant"], row["kind"]) == ("strangers", "unknown_board")
        assert (row["title"], row["company"]) == ("Senior Backend Engineer", "Strangers Inc")
        assert row["location"] == "Santa Clara, CA"
        assert row["excerpt"].startswith("Requirements: 3+ years") and len(row["excerpt"]) <= 2000
        assert len(row["content_hash"]) == 64 and row["json_ld"] is None
        assert (row["state"], row["sightings"]) == ("observed", 1)
        assert row["reporters"] == [fleet.account_pseudonym(A)]
        assert _aware(row["first_seen_at"]) == NOW == _aware(row["last_seen_at"])
        assert _aware(row["expire_at"]) == NOW + timedelta(days=30)
        assert A not in json.dumps(row, default=str)

    def test_a_company_site_with_a_job_posting_is_staged(self, db):
        out = _send(url=SITE_URL, title="Staff Engineer", company="WidgetCo",
                    json_ld=_json_ld(**{"$where": "1", "a.b": 2, "nested": {"$gt": 1, "ok": "x"}}))
        assert (out["result"], out["reason"]) == ("staged", "company_site")
        assert db.job_postings.count_documents({}) == 0
        row = db[radar.STAGED].find_one({})
        # Tracking parameters and the fragment are gone.
        assert row["url"] == "https://careers.widgetco.com/openings/staff-engineer-42"
        assert (row["host"], row["vendor"], row["kind"]) == (
            "careers.widgetco.com", "", "company_site")
        assert row["json_ld"]["@type"] == "JobPosting"
        assert row["json_ld"]["hiringOrganization"]["name"] == "WidgetCo"
        # Keys Mongo would read as an operator or a path are dropped.
        assert "$where" not in row["json_ld"] and "a.b" not in row["json_ld"]
        assert row["json_ld"]["nested"] == {"ok": "x"}

    @pytest.mark.parametrize("json_ld", [None, {"@type": "Organization", "name": "WidgetCo"},
                                         {"title": "no type"}])
    def test_a_company_site_without_a_job_posting_is_ignored(self, db, json_ld):
        extra = {} if json_ld is None else {"json_ld": json_ld}
        out = _send(url=SITE_URL, **extra)
        assert out == {"result": "ignored", "job_id": None, "reason": "unsupported"}
        assert db[radar.STAGED].count_documents({}) == 0
        assert db.job_postings.count_documents({}) == 0

    def test_a_vendor_page_that_is_not_a_posting_is_ignored(self, db):
        out = _send(url="https://job-boards.greenhouse.io/acme", json_ld=_json_ld())
        assert (out["result"], out["reason"]) == ("ignored", "not_a_posting")
        assert db[radar.STAGED].count_documents({}) == 0

    def test_a_vendor_address_inside_another_sites_url_is_not_that_vendor(self, db):
        """A fingerprint searches the whole URL. The HOST decides."""
        url = f"https://careers.widgetco.com/r/https://jobs.lever.co/acme/{UUID}"
        got = posting.resolve(posting.clean_url(url))
        assert got["vendor"] == "" and got["member"] is None and got["raw_id"] == ""
        assert _send(url=url)["result"] == "ignored"
        assert db.job_postings.count_documents({}) == 0

    def test_a_second_account_with_the_same_content_corroborates(self, db):
        _send(url=self.UNKNOWN)
        _send(url=self.UNKNOWN, now=NOW + timedelta(hours=1))        # the same account again
        row = db[radar.STAGED].find_one({})
        assert (row["state"], row["sightings"]) == ("observed", 2)
        later = NOW + timedelta(days=2)
        out = _send(url=self.UNKNOWN + "&utm_source=y", email=B, now=later)
        assert out["result"] == "staged"
        assert db[radar.STAGED].count_documents({}) == 1
        row = db[radar.STAGED].find_one({})
        assert (row["state"], row["sightings"]) == ("corroborated", 3)
        assert _aware(row["corroborated_at"]) == later
        assert sorted(row["reporters"]) == sorted(
            [fleet.account_pseudonym(A), fleet.account_pseudonym(B)])
        # The TTL runs from the LAST sighting.
        assert _aware(row["expire_at"]) == later + timedelta(days=30)
        assert _aware(row["first_seen_at"]) == NOW
        # Still nothing in the inventory: nothing promotes a staged row yet.
        assert db.job_postings.count_documents({}) == 0

    def test_a_second_account_with_other_content_does_not(self, db):
        _send(url=self.UNKNOWN)
        _send(url=self.UNKNOWN, email=B, description="<p>Pay a fee to apply.</p>" * 30)
        row = db[radar.STAGED].find_one({})
        assert (row["state"], row["sightings"]) == ("observed", 2)
        assert len(row["reporters"]) == 2
        # The first sighting's words are kept; a later one cannot rewrite them.
        assert row["excerpt"].startswith("Requirements: 3+ years")

    def test_the_reporter_set_is_bounded(self, db, monkeypatch):
        monkeypatch.setattr(posting, "STAGED_MAX_REPORTERS", 2)
        for email in (A, B, C):
            _send(url=self.UNKNOWN, email=email)
        row = db[radar.STAGED].find_one({})
        assert len(row["reporters"]) == 2 and row["sightings"] == 3

    def test_no_feed_reads_the_staged_collection(self):
        """Nothing outside the Radar modules names it."""
        from pathlib import Path
        root = Path(__file__).resolve().parents[1]
        users = set()
        for base in ("shared/python", "services"):
            for path in (root / base).rglob("*.py"):
                text = path.read_text(encoding="utf-8", errors="ignore")
                if "browser_radar_staged_postings" in text or "radar.STAGED" in text:
                    users.add(path.name)
        assert users == {"browser_radar_service.py", "browser_radar_posting.py"}


# ---------------------------------------------------------------------------
# Refusals, bounds and limits
# ---------------------------------------------------------------------------

BAD_URLS = [
    None, "", 7, ["https://a.com"], "ftp://jobs.lever.co/acme/x", "javascript:alert(1)",
    "//jobs.lever.co/acme", "https://", "https://user:pw@jobs.lever.co/acme/x",
    "https://user@jobs.lever.co/acme/x", "http://localhost/job/1", "http://localhost:8080/x",
    "http://127.0.0.1/job/1", "http://10.1.2.3/job", "http://192.168.1.5/job",
    "http://169.254.169.254/latest/meta-data", "http://[::1]/job", "http://[fd00::1]/job",
    "http://0x7f.0.0.1/job", "http://2130706433/job", "https://intranet/jobs/1",
    "https://jobs.corp.internal/1", "https://printer.local/job", "https://a b.com/job",
    "https://jobs.lever.co/acme/../admin", "https://jobs.lever.co/" + "a" * 2000,
]


class TestRefusals:
    @pytest.mark.parametrize("url", BAD_URLS)
    def test_bad_urls_are_refused_by_both(self, db, url):
        for call in (lambda: posting.lookup_posting(A, {"url": url}),
                     lambda: posting.ingest_posting(A, _body(url=url))):
            with pytest.raises(radar.RadarError) as e:
                call()
            assert (e.value.status, e.value.code) == (400, "invalid_request")
        assert db.job_postings.count_documents({}) == 0
        assert db[radar.USAGE].count_documents({}) == 0       # a refusal spends nothing

    @pytest.mark.parametrize("mutate", [
        lambda b: b.update(title=["x"]),
        lambda b: b.update(company={"$ne": ""}),
        lambda b: b.update(location=5),
        lambda b: b.update(description={"$gt": ""}),
        lambda b: b.update(description="x" * 200_001),
        lambda b: b.update(json_ld=["JobPosting"]),
        lambda b: b.update(json_ld="JobPosting"),
        lambda b: b.update(json_ld=_json_ld(description="x" * 103_000)),
        lambda b: b.update(date_posted=20261120),
    ])
    def test_bad_shapes_and_sizes(self, db, mutate):
        body = _body()
        mutate(body)
        with pytest.raises(radar.RadarError) as e:
            posting.ingest_posting(A, body)
        assert (e.value.status, e.value.code) == (400, "invalid_request")
        assert db.job_postings.count_documents({}) == 0

    @pytest.mark.parametrize("body", [None, [], "x", 3])
    def test_a_body_that_is_not_an_object(self, db, body):
        for call in (posting.lookup_posting, posting.ingest_posting):
            with pytest.raises(radar.RadarError) as e:
                call(A, body)
            assert (e.value.status, e.value.code) == (400, "invalid_request")

    def test_long_strings_are_cut_not_refused(self, db):
        out = _send(title="T" * 5000, location="L" * 5000)
        assert out["result"] == "created"
        doc = db.job_postings.find_one({})
        assert len(doc["title_raw"]) <= 300

    def test_the_gates_come_in_order(self, db, monkeypatch):
        """off, then contributor, then shape, then the limit."""
        monkeypatch.setenv("BROWSER_RADAR_CONTRIBUTORS_SCOPE", "listed")
        monkeypatch.setattr(posting, "POSTINGS_PER_HOUR", 0)
        monkeypatch.setattr(posting, "LOOKUPS_PER_HOUR", 0)
        bad = {"url": "ftp://x"}
        for call in (posting.lookup_posting, posting.ingest_posting):
            monkeypatch.setenv("BROWSER_RADAR_INGEST", "off")
            with pytest.raises(radar.RadarError) as e:
                call(A, bad)
            assert (e.value.status, e.value.code) == (409, "ingest_off")
            monkeypatch.delenv("BROWSER_RADAR_INGEST")
            monkeypatch.setenv("BROWSER_RADAR_ENABLED", "0")
            with pytest.raises(radar.RadarError) as e:
                call(A, bad)
            assert (e.value.status, e.value.code) == (409, "ingest_off")
            monkeypatch.delenv("BROWSER_RADAR_ENABLED")
            with pytest.raises(radar.RadarError) as e:
                call(A, bad)
            assert (e.value.status, e.value.code) == (403, "not_contributor")
            monkeypatch.setenv("BROWSER_RADAR_CONTRIBUTORS", A)
            with pytest.raises(radar.RadarError) as e:
                call(A, bad)
            assert (e.value.status, e.value.code) == (400, "invalid_request")
            with pytest.raises(radar.RadarError) as e:
                call(A, _body())
            assert (e.value.status, e.value.code) == (429, "rate_limited")
            assert 0 < e.value.retry_after <= 3600
            monkeypatch.delenv("BROWSER_RADAR_CONTRIBUTORS")
        assert db.job_postings.count_documents({}) == 0

    def test_dry_run_says_what_it_would_do_and_writes_nothing(self, db, monkeypatch):
        monkeypatch.setenv("BROWSER_RADAR_INGEST", "dry_run")
        out = _send()
        assert out == {"result": "ignored", "reason": "dry_run", "job_id": inv.posting_job_id(
            w.SOURCE_CAREER, "greenhouse-acme:8945798002"), "would": "created"}
        out = _send(url=TestStaging.UNKNOWN)
        assert out == {"result": "ignored", "reason": "dry_run", "job_id": None,
                       "would": "staged"}
        out = _send(url=SITE_URL)
        assert (out["result"], out["reason"], out["would"]) == ("ignored", "dry_run", "ignored")
        assert db.job_postings.count_documents({}) == 0
        assert db[radar.STAGED].count_documents({}) == 0
        assert {r["mode"] for r in db[radar.BATCHES].find({})} == {"dry_run"}
        assert radar.uploads_today(A)["batches"] == 0

    def test_dry_run_over_a_held_posting_changes_nothing(self, db, monkeypatch):
        _send(description="Short blurb.")
        first = _doc(w.SOURCE_CAREER, "greenhouse-acme:8945798002")
        monkeypatch.setenv("BROWSER_RADAR_INGEST", "dry_run")
        out = _send(now=NOW + timedelta(hours=5))
        assert (out["result"], out["would"], out["job_id"]) == ("ignored", "updated",
                                                                first["job_id"])
        assert _doc(w.SOURCE_CAREER, "greenhouse-acme:8945798002") == first


class TestLimits:
    def _usage(self, db, kind):
        doc = db[radar.USAGE].find_one({"_id": {"$regex": rf"\|{kind}\|"}}) or {}
        return doc.get("lookups", doc.get("postings", 0))

    def test_lookups_per_hour(self, db, monkeypatch):
        monkeypatch.setattr(posting, "LOOKUPS_PER_HOUR", 3)
        for _ in range(3):
            _look(GH_URL)
        for _ in range(4):
            with pytest.raises(radar.RadarError) as e:
                _look(GH_URL)
            assert (e.value.status, e.value.code) == (429, "rate_limited")
            assert 0 < e.value.retry_after <= 3600
        assert self._usage(db, "pl") == 3                 # refusals hand their unit back
        assert _look(GH_URL, email=B)["supported"] is True     # per account
        nxt = (NOW + timedelta(hours=1)).replace(minute=1)
        assert _look(GH_URL, now=nxt)["supported"] is True     # a new clock hour

    def test_postings_per_hour(self, db, monkeypatch):
        monkeypatch.setattr(posting, "POSTINGS_PER_HOUR", 2)
        _send()
        _send()
        for _ in range(3):
            with pytest.raises(radar.RadarError) as e:
                _send()
            assert e.value.status == 429 and 0 < e.value.retry_after <= 3600
        assert self._usage(db, "ph") == 2
        assert self._usage(db, "pd") == 2                 # the day's unit went back too
        assert _send(email=B)["result"] == "touched"

    def test_postings_per_day(self, db, monkeypatch):
        monkeypatch.setattr(posting, "POSTINGS_PER_DAY", 2)
        at = NOW.replace(hour=10, minute=0)
        _send(now=at)
        _send(now=at + timedelta(hours=1))
        with pytest.raises(radar.RadarError) as e:
            _send(now=at + timedelta(hours=2))
        assert e.value.status == 429 and 3600 < e.value.retry_after <= 86400
        assert self._usage(db, "pd") == 2
        assert db[radar.USAGE].count_documents({"_id": {"$regex": r"\|ph\|"}}) == 2
        assert _send(now=at + timedelta(days=1))["result"] == "touched"

    def test_the_defaults_are_the_contracts(self):
        assert (posting.LOOKUPS_PER_HOUR, posting.POSTINGS_PER_HOUR,
                posting.POSTINGS_PER_DAY) == (120, 60, 300)
        assert (posting.MAX_BODY_BYTES, posting.MAX_URL_CHARS, posting.MAX_DESCRIPTION_CHARS,
                posting.MAX_JSON_LD_BYTES) == (300 * 1024, 2000, 200_000, 100 * 1024)
        assert (posting.FRESH_HOURS, posting.STAGED_KEEP_DAYS, posting.FULL_BODY_CHARS,
                posting.STAGED_EXCERPT_CHARS) == (12.0, 30, 200, 2000)

    @pytest.mark.parametrize("which", ["lookup", "posting"])
    def test_concurrent_requests_cannot_pass_the_limit_together(self, db, monkeypatch, which):
        """Each counter is one atomic `$inc`, handed back when it went over,
        so callers racing each other cannot all read "under the limit"."""
        cap, callers = 5, 16
        monkeypatch.setattr(posting, "LOOKUPS_PER_HOUR", cap)
        monkeypatch.setattr(posting, "POSTINGS_PER_HOUR", cap)
        monkeypatch.setattr(posting, "POSTINGS_PER_DAY", cap)
        results, gate = [], threading.Barrier(callers)

        def one():
            gate.wait()
            try:
                if which == "lookup":
                    _look(SITE_URL)
                else:
                    _send(url=SITE_URL)
                results.append("ok")
            except radar.RadarError as e:
                results.append(e.status)

        threads = [threading.Thread(target=one) for _ in range(callers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert results.count("ok") == cap and results.count(429) == callers - cap
        counters = list(db[radar.USAGE].find({}))
        assert counters and all((c.get("lookups", c.get("postings"))) == cap for c in counters)


# ---------------------------------------------------------------------------
# What was received
# ---------------------------------------------------------------------------

class TestVisibility:
    def test_single_postings_count_in_uploads_today(self, db):
        _send()                                                        # created
        _send(now=NOW + timedelta(minutes=1), description="")          # touched
        _send(url=TestStaging.UNKNOWN)                                 # staged
        _send(url=SITE_URL)                                            # ignored
        radar.ingest_batch(A, {"batch_id": "batch-vis-00001", "boards": [{
            "tenant": "acme", "wd_host": "wd5", "site": "External", "search_text": "sre",
            "postings": [{"title": "SRE", "externalPath": "/job/Austin/SRE_JR9",
                          "bulletFields": ["JR9"], "postedOn": "Posted Today",
                          "locationsText": "Austin, TX", "timeType": "Full time"}]}]}, now=NOW)
        out = radar.uploads_today(A, now=NOW + timedelta(minutes=5))
        assert out["available"] is True
        assert (out["batches"], out["sent"]) == (5, 5)
        assert (out["created"], out["touched"], out["staged"]) == (2, 1, 1)
        assert out["recorded"] == 3                 # "Sent 3 jobs to Aspirely today"
        assert radar.uploads_today(B)["recorded"] == 0

    def test_the_admin_payload_counts_staged_rows_and_names_no_one(self, db):
        _send(url=TestStaging.UNKNOWN)
        _send(url=TestStaging.UNKNOWN, email=B)
        _send(url="https://job-boards.greenhouse.io/strangers/jobs/5550002", email=B)
        _send(url=SITE_URL, json_ld=_json_ld(), email=C)
        out = radar.uploads_summary(7, now=NOW + timedelta(minutes=5))
        assert out["available"] is True
        assert (out["staged_total"], out["staged_corroborated"]) == (3, 1)
        assert out["staged_by_host"] == [
            {"host": "job-boards.greenhouse.io", "postings": 2},
            {"host": "careers.widgetco.com", "postings": 1}]
        staged = json.dumps({k: out[k] for k in ("staged_total", "staged_corroborated",
                                                 "staged_by_host", "coverage", "fleet")})
        for email in (A, B, C):
            assert email not in staged and fleet.account_pseudonym(email) not in staged
        assert "@" not in staged and "reporters" not in staged and "strangers" not in staged
        row = next(r for r in out["rows"] if r["email"] == B)
        assert (row["batches"], row["staged"], row["recorded"]) == (2, 2, 0)
        # The staged collection itself holds pseudonyms, never an email.
        for doc in db[radar.STAGED].find({}):
            assert "example.com" not in json.dumps(doc, default=str)
            assert all("@" not in who for who in doc["reporters"])

    def test_only_the_ten_largest_hosts_are_listed(self, db):
        for i in range(12):
            _send(url=f"https://careers.site{i:02d}.com/jobs/{i}", json_ld=_json_ld(),
                  email=f"u{i}@example.com")
        out = radar.uploads_summary(7)
        assert out["staged_total"] == 12 and len(out["staged_by_host"]) == 10

    def test_unreadable_staged_counts_are_unknown_not_zero(self, db, monkeypatch):
        def boom():
            raise RuntimeError("down")
        monkeypatch.setattr(radar, "_db", boom)
        assert posting.staged_counts() == {"staged_total": None, "staged_corroborated": None,
                                           "staged_by_host": None}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

LOOKUP = "/api/jobs/browser-radar/posting/lookup"
POSTING = "/api/jobs/browser-radar/posting"


@pytest.fixture()
def app():
    return load_service_app("jobs")


def _call(app, email, path, json=None, data=None):
    from flask_jwt_extended import create_access_token
    with app.app_context():
        tok = create_access_token(identity=email)
    kwargs = {"headers": {"Authorization": f"Bearer {tok}"}}
    if json is not None:
        kwargs["json"] = json
    if data is not None:
        kwargs["data"] = data
        kwargs["content_type"] = "application/json"
    return app.test_client().post(path, **kwargs)


class TestRoutes:
    def test_the_lookup_route_shape(self, db, app):
        r = _call(app, A, LOOKUP, json={"url": GH_URL})
        assert r.status_code == 200
        assert r.get_json() == {"supported": True, "vendor": "greenhouse", "known_board": True,
                                "known": False, "job_id": None, "want": "body"}

    def test_the_posting_route_shape(self, db, app):
        r = _call(app, A, POSTING, json=_body())
        assert r.status_code == 200
        body = r.get_json()
        assert set(body) == {"result", "job_id", "reason"}
        assert body["result"] == "created" and len(body["job_id"]) == 32 and body["reason"] == ""
        r = _call(app, A, LOOKUP, json={"url": GH_URL})
        assert r.get_json()["known"] is True and r.get_json()["job_id"] == body["job_id"]

    def test_both_routes_need_a_token(self, db, app):
        for path in (LOOKUP, POSTING):
            assert app.test_client().post(path, json={"url": GH_URL}).status_code == 401

    def test_the_routes_are_in_the_blueprint_the_gateway_registers(self):
        from pathlib import Path
        root = Path(__file__).resolve().parents[1]
        gateway = (root / "services" / "gateway" / "wsgi.py").read_text(encoding="utf-8")
        assert "app.register_blueprint(jobs_bp, url_prefix='/api/jobs')" in gateway
        routes = (root / "services" / "jobs" / "blueprints" / "jobs.py").read_text(encoding="utf-8")
        assert '@jobs_bp.route("/browser-radar/posting/lookup", methods=["POST"])' in routes
        assert '@jobs_bp.route("/browser-radar/posting", methods=["POST"])' in routes

    def test_refusals_are_never_401(self, db, app, monkeypatch):
        seen = []

        def both(expect, code, **kw):
            for path, body in ((LOOKUP, kw.get("lookup", {"url": GH_URL})),
                               (POSTING, kw.get("posting", _body()))):
                r = _call(app, A, path, json=body) if "data" not in kw else \
                    _call(app, A, path, data=kw["data"])
                assert r.status_code == expect, (path, r.status_code, r.get_json())
                assert r.get_json()["error"] == code
                seen.append(r.status_code)
                yield r

        list(both(400, "invalid_request", lookup={"url": "http://127.0.0.1/x"},
                  posting=_body(url="http://127.0.0.1/x")))
        list(both(400, "invalid_request", lookup=["nope"], posting=["nope"]))
        list(both(400, "invalid_request", data="not json"))
        monkeypatch.setenv("BROWSER_RADAR_CONTRIBUTORS_SCOPE", "listed")
        list(both(403, "not_contributor"))
        monkeypatch.delenv("BROWSER_RADAR_CONTRIBUTORS_SCOPE")
        monkeypatch.setattr(posting, "LOOKUPS_PER_HOUR", 0)
        monkeypatch.setattr(posting, "POSTINGS_PER_HOUR", 0)
        for r in both(429, "rate_limited"):
            assert r.get_json()["retry_after_seconds"] > 0 and r.headers["Retry-After"]
        monkeypatch.setenv("BROWSER_RADAR_INGEST", "off")
        list(both(409, "ingest_off"))
        assert 401 not in seen and len(seen) == 12
        assert db.job_postings.count_documents({}) == 0

    def test_oversized_bodies_are_400(self, db, app):
        r = _call(app, A, POSTING, data="x" * (posting.MAX_BODY_BYTES + 1))
        assert r.status_code == 400 and r.get_json()["error"] == "invalid_request"
        r = _call(app, A, LOOKUP, data="x" * (posting.MAX_LOOKUP_BYTES + 1))
        assert r.status_code == 400 and r.get_json()["error"] == "invalid_request"
        # A posting just under the bound, with the largest description, is taken.
        r = _call(app, A, POSTING, json=_body(description="word " * 40_000))
        assert r.status_code == 200 and r.get_json()["result"] == "created"

    def test_dry_run_over_the_route(self, db, app, monkeypatch):
        monkeypatch.setenv("BROWSER_RADAR_INGEST", "dry_run")
        r = _call(app, A, POSTING, json=_body())
        assert r.status_code == 200
        assert (r.get_json()["result"], r.get_json()["reason"]) == ("ignored", "dry_run")
        assert db.job_postings.count_documents({}) == 0
