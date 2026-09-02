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
