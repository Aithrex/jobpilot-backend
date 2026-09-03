"""A guessed slug that answers is a candidate. It is never `validated`.

The fixtures below are the real collisions the current probe path admits.
Each one is a common English word or an initialism that two or more unrelated
employers use, so `ats_providers.util.slug_is_trustworthy` — a string-similarity
function — cannot separate them:

    Forward      Forward (healthcare) / Forward Networks / Forward Air
    Beyond       Beyond Inc / Beyond Meat / Beyond Finance
    Public       Public (investing) / Public Storage / Public Consulting Group
    CTS          CTS Corp / Cognizant Technology Solutions / CTS Cement
    Super        Super.com / Super Micro Computer / SuperAnnotate
    Archer       Archer Aviation / Archer Daniels Midland / ArcherDX
    CoStar       the Greenhouse board `costar` declares "Co-Star", an astrology
                 app; `names_match` normalises it identically to CoStar Group
    Alphabet     Alphabet Inc / ALPHABET, BMW's fleet-leasing subsidiary
    Capital One  capitalone.com / capital.com, a CFD trading site

The acceptance criterion is one line: **zero known wrong-owner fixtures reach
`validated`.**
"""
from __future__ import annotations

import pytest

from services import career_source_service as css
from services.career_source_service import evidence


def _sig(kind, *, confirmed=True, value=""):
    return evidence(kind, source="fixture", value=value, confirmed=confirmed)


# ---------------------------------------------------------------------------
# The wrong-owner fixtures
# ---------------------------------------------------------------------------
#: `(employer, vendor, tenant, note)` — every one reachable today by
#: `probe_software_company_boards.py` or `PortalDiscovery._slug_probe`.
WRONG_OWNER_FIXTURES = [
    ("Forward", "lever", "forward",
     "Forward Networks and Forward Air both answer to this slug"),
    ("Beyond", "greenhouse", "beyond",
     "Beyond Meat and Beyond Finance are different employers"),
    ("Public", "ashby", "public",
     "Public.com vs Public Storage vs Public Consulting Group"),
    ("CTS", "lever", "cts",
     "CTS Corp vs Cognizant Technology Solutions vs CTS Cement"),
    ("Super", "ashby", "super",
     "Super.com vs Super Micro vs SuperAnnotate"),
    ("Archer", "greenhouse", "archer",
     "Archer Aviation vs Archer Daniels Midland vs ArcherDX"),
    ("CoStar Group", "greenhouse", "costar",
     "the board declares 'Co-Star', an astrology app"),
    ("Alphabet", "workday", "alphabet",
     "BMW's ALPHABET fleet-leasing arm runs its own Workday"),
    ("Capital One", "greenhouse", "capital",
     "capital.com is a CFD trading site, not Capital One"),
]


class TestNoWrongOwnerReachesValidated:
    @pytest.mark.parametrize("employer, vendor, tenant, note", WRONG_OWNER_FIXTURES,
                             ids=[f[0] for f in WRONG_OWNER_FIXTURES])
    def test_a_responding_endpoint_with_a_plausible_slug_stays_candidate(
            self, employer, vendor, tenant, note):
        """Exactly what the probe path has today: the board answered, and the
        slug looks like the name. That is the whole of the current evidence."""
        row = css.build_source(
            employer_id="emp_test", ats_vendor=vendor, tenant=tenant,
            discovery_method=css.METHOD_SLUG_PROBE,
            ownership_evidence=[
                _sig(css.SIGNAL_ENDPOINT_RESPONDS, value="47 postings"),
                _sig(css.SIGNAL_SLUG_SIMILARITY, value="slug_is_trustworthy=True"),
            ],
            content_validation_evidence=[_sig("content_fetch", value="47 postings")],
            job_count_last=47,
        )
        assert row["state"] != css.STATE_VALIDATED, note
        assert row["state"] == css.STATE_CONTENT_VALIDATED
        assert row["ownership_verified"] is False
        assert row["failure_code"] == css.FAIL_OWNER_UNPROVEN

    @pytest.mark.parametrize("employer, vendor, tenant, note", WRONG_OWNER_FIXTURES,
                             ids=[f[0] for f in WRONG_OWNER_FIXTURES])
    def test_a_provider_naming_someone_else_is_decisive(
            self, employer, vendor, tenant, note):
        row = css.build_source(
            employer_id="emp_test", ats_vendor=vendor, tenant=tenant,
            ownership_evidence=[
                _sig(css.SIGNAL_PROVIDER_OWNER, confirmed=False,
                     value="Co-Star Astrology Society"),
                _sig(css.SIGNAL_SLUG_SIMILARITY),
            ],
            content_validation_evidence=[_sig("content_fetch")],
            job_count_last=12,
        )
        assert row["ownership_verified"] is False
        assert row["failure_code"] == css.FAIL_OWNER_MISMATCH
        assert row["state"] != css.STATE_VALIDATED


class TestOwnershipRules:
    def test_a_verified_domain_link_is_sufficient_on_its_own(self):
        owned, code, why = css.adjudicate_ownership([
            _sig(css.SIGNAL_DOMAIN_LINK, value="stripe.com -> greenhouse/stripe")])
        assert owned and code == css.FAIL_NONE
        assert "verified employer domain" in why

    def test_a_redirect_chain_from_the_domain_is_sufficient(self):
        owned, _, _ = css.adjudicate_ownership([_sig(css.SIGNAL_DOMAIN_REDIRECT)])
        assert owned

    def test_two_independent_corroborating_signals_suffice(self):
        owned, _, why = css.adjudicate_ownership([
            _sig(css.SIGNAL_PROVIDER_OWNER, value="Stripe"),
            _sig(css.SIGNAL_BOARD_BACKLINK, value="https://stripe.com"),
        ])
        assert owned
        assert "two independent" in why

    def test_one_corroborating_signal_does_not(self):
        owned, code, why = css.adjudicate_ownership([_sig(css.SIGNAL_PROVIDER_OWNER)])
        assert not owned and code == css.FAIL_OWNER_UNPROVEN
        assert "needs two" in why

    def test_two_signals_of_the_SAME_class_are_one_signal(self):
        """`provider_owner_metadata` twice is the ATS agreeing with itself."""
        owned, _, _ = css.adjudicate_ownership([
            _sig(css.SIGNAL_PROVIDER_OWNER, value="a"),
            _sig(css.SIGNAL_PROVIDER_OWNER, value="b"),
        ])
        assert not owned

    def test_slug_similarity_is_recorded_and_never_counted(self):
        assert css.SIGNAL_SLUG_SIMILARITY in css.NON_PROBATIVE_SIGNALS
        assert css.SIGNAL_SLUG_SIMILARITY not in css.SUFFICIENT_SIGNALS
        assert css.SIGNAL_SLUG_SIMILARITY not in css.CORROBORATING_SIGNALS
        owned, code, why = css.adjudicate_ownership([
            _sig(css.SIGNAL_SLUG_SIMILARITY),
            _sig(css.SIGNAL_ENDPOINT_RESPONDS),
        ])
        assert not owned
        assert "non-probative" in why

    def test_no_evidence_at_all_is_reported_as_such(self):
        owned, code, why = css.adjudicate_ownership([])
        assert not owned and why == "no ownership evidence gathered"

    def test_an_unconfirmed_signal_does_not_count(self):
        owned, _, _ = css.adjudicate_ownership([
            _sig(css.SIGNAL_DOMAIN_LINK, confirmed=False)])
        assert not owned


class TestStateMachine:
    def test_ownership_plus_content_plus_jobs_is_the_only_route_to_validated(self):
        assert css.next_state(owned=True, content_ok=True, job_count=5) == css.STATE_VALIDATED

    def test_content_without_ownership_stops_at_content_validated(self):
        assert css.next_state(owned=False, content_ok=True, job_count=5) \
            == css.STATE_CONTENT_VALIDATED

    def test_ownership_without_content_stops_at_ownership_verified(self):
        assert css.next_state(owned=True, content_ok=None) == css.STATE_OWNERSHIP_VERIFIED

    def test_an_owner_verified_board_with_no_jobs_is_empty_not_a_gap(self):
        assert css.next_state(owned=True, content_ok=True, job_count=0) == css.STATE_EMPTY
        assert css.STATE_EMPTY in css.COVERED_STATES

    @pytest.mark.parametrize("code", [css.FAIL_HTTP_403, css.FAIL_HTTP_429,
                                      css.FAIL_CHALLENGE, css.FAIL_ROBOTS])
    def test_a_transient_blocker_is_blocked_not_dead(self, code):
        assert css.next_state(owned=True, content_ok=False, failure_code=code) \
            == css.STATE_BLOCKED

    def test_a_move_beats_every_other_verdict(self):
        assert css.next_state(owned=True, content_ok=True, job_count=99,
                              moved_to="https://new.example/board") == css.STATE_MOVED

    def test_ambiguity_goes_to_a_human_not_to_validated(self):
        assert css.next_state(owned=True, content_ok=True, job_count=99,
                              ambiguous=True) == css.STATE_MANUAL_REVIEW

    def test_only_validated_and_empty_count_as_covered(self):
        assert set(css.COVERED_STATES) == {css.STATE_VALIDATED, css.STATE_EMPTY}

    def test_a_moved_source_is_rechecked_soonest(self):
        """Rivian's iCIMS tenant was a 143-byte redirect that nothing
        re-fingerprinted. `moved` must come back tomorrow."""
        assert css._RECHECK[css.STATE_MOVED] <= 1


class TestBuildSource:
    def test_state_is_derived_not_dictated(self):
        row = css.build_source(employer_id="e", ats_vendor="greenhouse", tenant="acme",
                               ownership_evidence=[_sig(css.SIGNAL_SLUG_SIMILARITY)])
        assert row["state"] == css.STATE_CANDIDATE

    def test_the_full_evidence_schema_is_present(self):
        row = css.build_source(employer_id="e", ats_vendor="lever", tenant="acme")
        for field in ("employer_id", "verified_domain", "career_landing_url",
                      "official_jobs_url", "ats_vendor", "tenant",
                      "canonical_endpoint", "redirect_chain", "discovery_method",
                      "ownership_evidence", "content_validation_evidence",
                      "first_seen_at", "last_verified_at", "next_check_at",
                      "failure_code", "failure_detail"):
            assert field in row, field

    def test_source_id_survives_a_url_change(self):
        a = css.source_id_for(employer_id="e", ats_vendor="icims", tenant="rivian",
                              canonical_endpoint="https://old.example")
        b = css.source_id_for(employer_id="e", ats_vendor="icims", tenant="rivian",
                              canonical_endpoint="https://new.example")
        assert a == b

    def test_failure_vocabulary_is_closed(self):
        row = css.build_source(employer_id="e", ats_vendor="lever", tenant="a",
                               failure_code="something_i_made_up")
        assert row["failure_code"] in css.FAILURE_CODES


@pytest.fixture()
def sources_db(mock_db):
    mock_db[css.CAREER_SOURCES].delete_many({})
    return mock_db


class TestPersistence:
    def _validated(self):
        return css.build_source(
            employer_id="emp_stripe", ats_vendor="greenhouse", tenant="stripe",
            verified_domain="stripe.com",
            ownership_evidence=[_sig(css.SIGNAL_DOMAIN_LINK)],
            content_validation_evidence=[_sig("content_fetch")],
            job_count_last=100)

    def test_round_trip(self, sources_db):
        sid = css.upsert_source(self._validated())
        assert css.get_source(sid)["state"] == css.STATE_VALIDATED

    def test_a_transient_failure_never_erases_the_ownership_proof(self, sources_db):
        sid = css.upsert_source(self._validated())
        css.record_failure(sid, failure_code=css.FAIL_HTTP_429, detail="rate limited")
        row = css.get_source(sid)
        assert row["state"] == css.STATE_BLOCKED
        assert row["ownership_verified"] is True
        assert row["consecutive_failures"] == 0
        assert any(e["kind"] == css.SIGNAL_DOMAIN_LINK for e in row["ownership_evidence"])

    def test_repeated_hard_failures_reach_dead_but_the_row_survives(self, sources_db):
        sid = css.upsert_source(self._validated())
        for _ in range(css.DEAD_AFTER_FAILURES):
            state = css.record_failure(sid, failure_code=css.FAIL_HTTP_5XX)
        assert state == css.STATE_DEAD
        assert css.get_source(sid) is not None

    def test_content_success_cannot_promote_an_unowned_source(self, sources_db):
        sid = css.upsert_source(css.build_source(
            employer_id="e", ats_vendor="lever", tenant="forward",
            ownership_evidence=[_sig(css.SIGNAL_SLUG_SIMILARITY)]))
        assert css.record_success(sid, job_count=80) == css.STATE_CONTENT_VALIDATED

    def test_a_move_is_recorded_with_its_destination(self, sources_db):
        sid = css.upsert_source(css.build_source(
            employer_id="e", ats_vendor="icims", tenant="us-careers-rivian",
            ownership_evidence=[_sig(css.SIGNAL_DOMAIN_LINK)]))
        css.record_move(sid, "https://careers.rivian.com/work/jobs/categories")
        row = css.get_source(sid)
        assert row["state"] == css.STATE_MOVED
        assert "careers.rivian.com" in row["redirect_chain"][0]

    def test_every_requested_employer_appears_in_the_coverage_map(self, sources_db):
        css.upsert_source(self._validated())
        got = css.coverage_by_employer(["emp_stripe", "emp_missing"])
        assert got["emp_stripe"]["covered"] is True
        assert got["emp_missing"]["covered"] is False
        assert got["emp_missing"]["state"] == css.STATE_CANDIDATE

    def test_a_provider_failure_cannot_remove_an_employer_from_the_denominator(
            self, sources_db):
        """Every state is accountable; none of them is absence."""
        sid = css.upsert_source(self._validated())
        for code in (css.FAIL_HTTP_403, css.FAIL_TIMEOUT, css.FAIL_HTTP_5XX):
            css.record_failure(sid, failure_code=code)
        got = css.coverage_by_employer(["emp_stripe"])
        assert got["emp_stripe"]["sources"] == 1
        assert got["emp_stripe"]["state"] in css.ACCOUNTABLE_STATES

    def test_counts_shape(self, sources_db):
        css.upsert_source(self._validated())
        got = css.counts()
        assert got["total"] == 1
        assert got["covered"] == 1
        assert got["by_state"][css.STATE_VALIDATED] == 1
        assert got["ownership_verified"] == 1


# ---------------------------------------------------------------------------
# End to end, with a fake network
# ---------------------------------------------------------------------------
class FakeFetcher:
    """A `career_source_ownership.Fetcher` with a scripted internet.

    Written as a class rather than a mock so the tests below read as
    "here is what the web said" — which is the only input the verifier has.
    """

    def __init__(self, pages):
        self.pages = pages
        self.requested = []

    def get(self, url):
        self.requested.append(url)
        for key, body in self.pages.items():
            if key in url:
                return {"status": 200, "text": body, "chain": [url],
                        "final_url": url, "error": ""}
        return {"status": 404, "text": "", "chain": [url],
                "final_url": url, "error": ""}


class _Ep:
    """Minimal `ats_providers.base.Endpoint` stand-in."""

    def __init__(self, vendor, tenant):
        self.vendor = vendor
        self.tenant = tenant
        self.display_name = ""
        self.member_key = f"{vendor}-{tenant}"


class TestVerifyEmployerSourceEndToEnd:
    def _employer(self, name="Databricks", domain="databricks.com"):
        from services import employer_registry as er
        return er.build_employer(display_name=name, canonical_domain=domain)

    def test_the_employers_own_site_linking_to_the_board_is_enough(self):
        from services import career_source_ownership as cso

        fetcher = FakeFetcher({
            "databricks.com/careers": (
                '<html><body>Databricks careers. '
                '<a href="https://boards.greenhouse.io/databricks">Open roles</a>'
                "</body></html>"),
            "databricks.com": "<html><body>Databricks</body></html>",
        })
        records = cso.verify_employer_source(
            employer=self._employer(), fetcher=fetcher, ctx=None)
        assert records
        owned = [r for r in records if r["ownership_verified"]]
        assert owned, [r["ownership_reason"] for r in records]
        assert owned[0]["ats_vendor"] == "greenhouse"
        assert owned[0]["tenant"] == "databricks"
        assert owned[0]["discovery_method"] in (
            css.METHOD_OFFICIAL_LINK, css.METHOD_OFFICIAL_REDIRECT)

    def test_a_site_that_does_not_name_the_employer_proves_nothing(self):
        """Domain confirmation is what makes "their site links here" a claim
        about THEM. Without it the walk starts from a page we cannot attribute.

        The body below names the employer ONLY inside an href. An attribute is
        not a statement about whose page this is, and reading markup as text is
        the same confusion this module exists to prevent, one level up."""
        from services import career_source_ownership as cso

        fetcher = FakeFetcher({
            "databricks.com": (
                '<html><body>Domain for sale. '
                '<a href="https://boards.greenhouse.io/databricks">jobs</a>'
                "</body></html>"),
        })
        records = cso.verify_employer_source(
            employer=self._employer(), fetcher=fetcher, ctx=None)
        assert not any(r["ownership_verified"] for r in records)

    def test_an_employer_with_no_endpoint_still_gets_a_row(self):
        """An employer with no row is an employer that silently left the
        denominator."""
        from services import career_source_ownership as cso

        fetcher = FakeFetcher({"nowhere.example": "<html>Nowhere Inc</html>"})
        records = cso.verify_employer_source(
            employer=self._employer("Nowhere Inc", "nowhere.example"),
            fetcher=fetcher, ctx=None)
        assert len(records) == 1
        assert records[0]["state"] == css.STATE_CANDIDATE
        assert records[0]["failure_code"] == css.FAIL_OWNER_UNPROVEN
        assert records[0]["failure_detail"]

    def test_a_responding_board_alone_never_reaches_validated(self):
        """The whole point, end to end: the endpoint is real and answering,
        the slug looks right, and the employer's site says nothing."""
        from services import career_source_ownership as cso

        fetcher = FakeFetcher({})       # nothing resolves
        records = cso.verify_employer_source(
            employer=self._employer("Forward", "goforward.com"),
            fetcher=fetcher, ctx=None,
            candidate_endpoints=[_Ep("lever", "forward")])
        assert records
        for r in records:
            assert r["state"] != css.STATE_VALIDATED
            assert not r["ownership_verified"]
        # And the heuristic IS recorded, so a report can say we declined it.
        kinds = {e["kind"] for r in records for e in r["ownership_evidence"]}
        assert css.SIGNAL_SLUG_SIMILARITY in kinds

    def test_the_name_must_appear_in_VISIBLE_text(self):
        from services import career_source_ownership as cso

        in_markup = ('<html><body>Domain for sale.'
                     '<a href="https://boards.greenhouse.io/databricks">x</a>'
                     "</body></html>")
        in_text = "<html><body>Databricks careers</body></html>"
        assert cso.page_confirms_employer(in_markup, "Databricks")[0] is False
        assert cso.page_confirms_employer(in_text, "Databricks")[0] is True

    def test_a_title_or_site_name_counts_as_visible(self):
        from services import career_source_ownership as cso

        page = ('<html><head><title>Databricks</title>'
                '<meta property="og:site_name" content="Databricks"></head>'
                "<body><div></div></body></html>")
        assert cso.page_confirms_employer(page, "Databricks")[0] is True

    def test_script_contents_are_not_visible_text(self):
        from services import career_source_ownership as cso

        page = ('<html><body><script>var vendor="Databricks";</script>'
                "Nothing here.</body></html>")
        assert cso.page_confirms_employer(page, "Databricks")[0] is False

    def test_short_brand_names_like_f5_confirm_from_visible_text(self):
        from services import career_source_ownership as cso

        page = "<html><head><title>Careers at F5</title></head>" \
               "<body>Careers at F5</body></html>"
        assert cso._name_tokens("F5") == ["f5"]
        assert cso.page_confirms_employer(page, "F5")[0] is True

    def test_verify_domain_accepts_careers_url_when_homepage_is_walled(self):
        """Oracle-shaped: bare homepage 403s; careers subdomain names them."""
        from services import career_source_ownership as cso

        class WallFetcher:
            def get(self, url, **kw):
                if "careers.oracle.com" in url or "/careers" in url:
                    return {"status": 200, "error": "", "text":
                            "<html><title>Oracle Careers</title>"
                            "<body>Oracle</body></html>",
                            "final_url": url, "chain": [url],
                            "network_urls": []}
                return {"status": 403, "error": "", "text": "",
                        "final_url": url, "chain": [url], "network_urls": []}

        ok, final, ev = cso.verify_domain(
            WallFetcher(), display_name="Oracle", domain="oracle.com",
            official_url="https://careers.oracle.com/jobs/")
        assert ok is True
        assert "careers.oracle.com" in final

    def test_verify_domain_accepts_403_body_that_still_names_the_employer(self):
        from services import career_source_ownership as cso

        class SoftWall:
            def get(self, url, **kw):
                return {"status": 403, "error": "",
                        "text": "<html><body>Oracle</body></html>",
                        "final_url": url, "chain": [url], "network_urls": []}

        ok, final, ev = cso.verify_domain(
            SoftWall(), display_name="Oracle", domain="oracle.com")
        assert ok is True

    def test_collapse_keeps_owned_endpoint_over_empty_twin(self):
        from services import career_source_ownership as cso
        from services import career_source_service as css

        owned = css.build_source(
            employer_id="emp_x", ats_vendor="oracle_orc", tenant="eeho",
            canonical_endpoint="https://eeho.fa.us2.oraclecloud.com/hcmUI/...",
            member_key="oracle_orc-eeho-cx_1",
            ownership_evidence=[css.evidence(css.SIGNAL_DOMAIN_LINK, confirmed=True)],
        )
        empty = css.build_source(
            employer_id="emp_x", ats_vendor="oracle_orc", tenant="eeho",
            member_key="oracle_orc-eeho",
            ownership_evidence=[],
        )
        assert owned["source_id"] == empty["source_id"]
        out = cso._collapse_by_source_id([empty, owned])
        assert len(out) == 1
        assert out[0]["ownership_verified"] is True
        assert out[0]["canonical_endpoint"]

    def test_a_domain_the_board_declares_is_a_candidate_not_a_conclusion(self):
        """`candidate_domains_from_board` is how an employer that exists only
        as an ATS display name gets a domain at all. It is then verified the
        same way as any other."""
        from services import career_source_ownership as cso

        fetcher = FakeFetcher({
            "jobs.lever.co/acme": (
                '<html><a href="https://acme.example/about">Acme Corp</a></html>'),
            "acme.example": "<html><body>Acme Corp</body></html>",
        })
        got = cso.candidate_domains_from_board(fetcher, _Ep("lever", "acme"))
        assert "acme.example" in got

    def test_board_infrastructure_is_never_offered_as_the_employers_domain(self):
        from services import career_source_ownership as cso

        fetcher = FakeFetcher({
            "jobs.lever.co/acme": (
                '<html><a href="https://www.linkedin.com/company/acme">li</a>'
                '<a href="https://cdn.jsdelivr.net/x.js">cdn</a>'
                '<a href="https://jobs.lever.co/acme/1">job</a></html>'),
        })
        assert cso.candidate_domains_from_board(fetcher, _Ep("lever", "acme")) == []


class TestMigrationDetection:
    def test_a_cross_host_redirect_is_a_move(self):
        """Rivian's iCIMS tenant answered with a 143-byte redirect and nothing
        re-fingerprinted it."""
        from services import career_source_ownership as cso

        class _F:
            def get(self, url):
                return {"status": 200, "text": "<html>ok</html>",
                        "chain": [url, "https://careers.rivian.com/work/jobs"],
                        "final_url": "https://careers.rivian.com/work/jobs",
                        "error": ""}

        moved, dest, chain = cso.detect_migration(
            _F(), "https://us-careers-rivian.icims.com/jobs")
        assert moved and "careers.rivian.com" in dest and len(chain) == 2

    def test_a_tiny_redirect_shim_is_a_move(self):
        from services import career_source_ownership as cso

        class _F:
            def get(self, url):
                return {"status": 200,
                        "text": '<meta http-equiv="refresh" '
                                'content="0;url=https://new.example/jobs">',
                        "chain": [url], "final_url": url, "error": ""}

        moved, dest, _chain = cso.detect_migration(_F(), "https://old.example/jobs")
        assert moved and dest == "https://new.example/jobs"

    def test_a_normal_page_is_not_a_move(self):
        from services import career_source_ownership as cso

        class _F:
            def get(self, url):
                return {"status": 200, "text": "<html>" + "x" * 5000 + "</html>",
                        "chain": [url], "final_url": url, "error": ""}

        assert cso.detect_migration(_F(), "https://old.example/jobs")[0] is False


class TestAProbeFailureDoesNotEraseALiveBoard:
    """A 403 or a bot challenge is a fact about OUR request, not about the
    board. When the live inventory holds fresh postings from the same
    `(vendor, tenant)`, the endpoint is demonstrably serving jobs.

    Measured: one content pass running beside two others turned 830 sources
    `blocked` in a single sweep — boards the crawler had read that morning.
    Acting on that verdict erased a third of the coverage the same session had
    just earned.
    """

    def test_record_note_leaves_the_state_alone(self, sources_db):
        src = css.build_source(
            employer_id="emp_live", ats_vendor="workday", tenant="ngc",
            ownership_evidence=[
                css.evidence(css.SIGNAL_PROVIDER_OWNER, value="Northrop Grumman"),
                css.evidence(css.SIGNAL_CURATED_ROW, value="Northrop Grumman"),
            ],
            content_validation_evidence=[css.evidence("content_fetch", value="968")],
            job_count_last=968)
        css.upsert_source(src)
        assert css.record_note(src["source_id"], "probe 403; 968 fresh postings") \
            == css.STATE_VALIDATED
        row = sources_db.career_sources.find_one({"source_id": src["source_id"]})
        assert row["state"] == css.STATE_VALIDATED
        assert "968 fresh postings" in row["last_note"]
        assert row["last_checked_at"] is not None, "the check still happened"

    def test_record_failure_would_have_demoted_it(self, sources_db):
        """The contrast that makes the guard necessary."""
        src = css.build_source(
            employer_id="emp_live2", ats_vendor="workday", tenant="wf",
            ownership_evidence=[
                css.evidence(css.SIGNAL_PROVIDER_OWNER, value="Wells Fargo"),
                css.evidence(css.SIGNAL_CURATED_ROW, value="Wells Fargo"),
            ],
            content_validation_evidence=[css.evidence("content_fetch", value="1443")],
            job_count_last=1443)
        css.upsert_source(src)
        assert css.record_failure(src["source_id"],
                                  failure_code=css.FAIL_HTTP_403,
                                  detail="bot wall") != css.STATE_VALIDATED

    def test_a_note_on_a_missing_source_is_not_an_error(self, sources_db):
        assert css.record_note("cs_does_not_exist", "anything") == ""
