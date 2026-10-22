"""Provider readiness comes from the CODE, not from a list someone maintains.

`employer_tech_classifier.UNSUPPORTED_PROVIDERS` was the whole answer to "can
we crawl this ATS", and it went stale exactly as a hand-written list does: it
named `usajobs` and `successfactors` while `ats_providers/usajobs.py` and
`ats_providers/successfactors.py` were both registered, implemented and
crawling. Every employer on either vendor was reported as an ATS we cannot
reach.

A derived answer cannot go stale. That is the property under test.
"""
from __future__ import annotations

import pytest

from services import employer_tech_classifier as etc
from services.ats_providers import registry as ats_registry


class TestReadinessIsDerived:

    def test_every_registered_vendor_has_a_readiness_verdict(self):
        report = ats_registry.readiness_report()
        assert set(report) == set(ats_registry.vendors(enabled_only=False))
        for vendor, verdict in report.items():
            assert verdict["state"] in ats_registry.READINESS_STATES, vendor
            assert verdict["detail"], vendor

    def test_an_unregistered_vendor_is_not_implemented_not_an_error(self):
        verdict = ats_registry.readiness("a-vendor-that-does-not-exist")
        assert verdict["state"] == ats_registry.NOT_IMPLEMENTED
        assert verdict["crawlable"] is False

    def test_a_registered_implemented_vendor_is_ready(self):
        for vendor in ("greenhouse", "lever", "workday", "icims", "ashby"):
            assert ats_registry.readiness(vendor)["state"] == ats_registry.READY

    def test_the_two_vendors_the_stale_list_got_wrong(self):
        """The regression this whole mechanism exists for."""
        assert "usajobs" not in etc.UNSUPPORTED_PROVIDERS
        assert "successfactors" not in etc.UNSUPPORTED_PROVIDERS
        # SuccessFactors is implemented and reachable by fingerprinting.
        assert etc.provider_blocker("successfactors") is None
        # USAJOBS is implemented and needs an operator to set one key. That is
        # an OPERATIONS gap, and reporting it as "we cannot crawl this ATS"
        # sends it to the wrong person.
        blocker = etc.provider_blocker("usajobs")
        assert blocker is None or "USAJOBS_API_KEY" in blocker

    def test_a_vendor_with_no_provider_still_reports_its_recorded_reason(self):
        assert "61 tenants" in (etc.provider_blocker("zohorecruit") or "")

    def test_a_parked_vendor_reports_the_evidence_it_was_parked_on(self):
        # Dayforce was a written-down "not probed" until 2026-10-22; it is now
        # a parked provider carrying what was observed (a 403 bot wall).
        assert "403" in (etc.provider_blocker("dayforce") or "")

    def test_asking_about_readiness_does_not_PARK_a_vendor(self):
        """`_credentials` mutates `enabled` by design, so the crawl path can
        pick up a key delivered after warm-up. Asking "is this ready?" must
        not have that side effect — it did, and it dropped `usajobs` from
        `all_providers(enabled_only=True)` for every later caller in the
        process."""
        before = set(ats_registry.vendors(enabled_only=True))
        ats_registry.readiness_report()
        ats_registry.readiness_report()
        after = set(ats_registry.vendors(enabled_only=True))
        assert before == after
        assert "usajobs" in after


class TestReadinessSemanticsAreDistinct:
    """`blocked`, `dead`, `empty` and `candidate` must stay distinguishable —
    and so must the four reasons a vendor is not ready."""

    def test_needs_credential_is_not_the_same_as_not_implemented(self):
        assert ats_registry.NEEDS_CREDENTIAL != ats_registry.NOT_IMPLEMENTED
        states = {ats_registry.READY, ats_registry.NEEDS_CREDENTIAL,
                  ats_registry.PARKED, ats_registry.NOT_IMPLEMENTED}
        assert len(states) == 4

    def test_a_credential_gap_names_the_setting_to_change(self):
        verdict = ats_registry.readiness("usajobs")
        if verdict["state"] == ats_registry.NEEDS_CREDENTIAL:
            assert verdict["credential_setting"] == "USAJOBS_API_KEY"
        else:
            assert verdict["requires_credential"] is True


class TestCoverageStateSemantics:
    """The four employer coverage states the brief requires to stay distinct."""

    def test_an_empty_board_is_not_a_dead_one(self):
        state, _detail = etc.coverage_state_for(
            {}, [{"status": "empty", "vendor": "greenhouse"}])
        assert state == etc.COVERAGE_NO_PUBLIC_JOBS

    def test_a_blocked_board_is_not_a_dead_one(self):
        state, detail = etc.coverage_state_for(
            {}, [{"status": "blocked", "vendor": "icims",
                  "last_check_detail": "403 challenge"}])
        assert state == etc.COVERAGE_BLOCKED
        assert "403" in detail

    def test_a_dead_board_is_retrying_not_unsupported(self):
        state, _detail = etc.coverage_state_for(
            {}, [{"status": "dead", "vendor": "greenhouse"}])
        assert state == etc.COVERAGE_RETRYING

    def test_no_attempt_at_all_is_unresolved(self):
        state, _detail = etc.coverage_state_for({}, [])
        assert state == etc.COVERAGE_UNRESOLVED

    def test_an_unimplemented_vendor_is_unsupported_not_unresolved(self):
        state, detail = etc.coverage_state_for(
            {}, [{"status": "candidate", "vendor": "dayforce"}])
        assert state == etc.COVERAGE_UNSUPPORTED
        assert "dayforce" in detail
