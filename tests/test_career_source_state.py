"""Registration must not overwrite what the content pass established.

The nine-state machine asks two independent questions — who owns this board,
and does it serve jobs — and keeps the answers apart on purpose. This file
covers the one place they were being collapsed.
"""
from __future__ import annotations

import pytest

from services import career_source_service as css
from services.career_source_service import evidence

class TestRegistrationNeverForgetsAContentVerdict:
    """`upsert_source` is the REGISTRATION path, and registration answers only
    half of the state machine's question.

    Re-registering a board that was already `validated` rewrote its state to
    `ownership_verified`, because `state` is in `_MUTABLE` and was `$set`
    unconditionally. On live data one `verify_official_career_urls.py --apply
    --register` run moved Intel, Adobe, Accenture, Palantir, Splunk, DeepL,
    d-Matrix, Aikido and Lakera out of the covered set — Tier-A coverage read
    24/94 instead of 30/94, and the write that caused it had proved something
    GOOD about every one of them.
    """

    def _register(self, employer_id="emp_000000000001", state=None, **extra):
        rec = css.build_source(
            employer_id=employer_id, ats_vendor="workday", tenant="acme",
            verified_domain="acme.example",
            canonical_endpoint="https://acme.wd1.myworkdayjobs.com/External",
            ownership_evidence=[
                evidence(css.SIGNAL_DOMAIN_LINK, source="acme.example",
                         confirmed=True)],
            **extra)
        if state is not None:
            rec["state"] = state
        return css.upsert_source(rec)

    @pytest.mark.parametrize("stored", [css.STATE_VALIDATED, css.STATE_EMPTY])
    def test_a_content_state_survives_a_re_registration(self, mock_db, stored):
        sid = self._register()
        mock_db[css.CAREER_SOURCES].update_one(
            {"source_id": sid}, {"$set": {"state": stored, "job_count_last": 7}})
        self._register()
        row = mock_db[css.CAREER_SOURCES].find_one({"source_id": sid})
        assert row["state"] == stored

    def test_content_validated_PLUS_proven_ownership_is_validated(self, mock_db):
        """The guard facing the other way. `content_validated` means the board
        was read and held postings; the only thing missing was ownership, and
        this registration is exactly that. Leaving it at `content_validated`
        would keep an employer out of coverage for a reason that no longer
        holds."""
        sid = self._register()
        mock_db[css.CAREER_SOURCES].update_one(
            {"source_id": sid}, {"$set": {"state": css.STATE_CONTENT_VALIDATED,
                                          "job_count_last": 12}})
        self._register()
        row = mock_db[css.CAREER_SOURCES].find_one({"source_id": sid})
        assert row["state"] == css.STATE_VALIDATED

    def test_content_validated_WITHOUT_ownership_is_left_alone(self, mock_db):
        sid = self._register()
        mock_db[css.CAREER_SOURCES].update_one(
            {"source_id": sid}, {"$set": {"state": css.STATE_CONTENT_VALIDATED}})
        rec = css.build_source(
            employer_id="emp_000000000001", ats_vendor="workday",
            tenant="acme",
            canonical_endpoint="https://acme.wd1.myworkdayjobs.com/External")
        assert not rec.get("ownership_verified")
        css.upsert_source(rec)
        row = mock_db[css.CAREER_SOURCES].find_one({"source_id": sid})
        assert row["state"] == css.STATE_CONTENT_VALIDATED

    def test_everything_ELSE_the_registration_learned_still_lands(self, mock_db):
        sid = self._register()
        mock_db[css.CAREER_SOURCES].update_one(
            {"source_id": sid}, {"$set": {"state": css.STATE_VALIDATED}})
        self._register(career_landing_url="https://jobs.acme.example/search",
                       redirect_chain=["https://acme.example/careers",
                                       "https://acme.wd1.myworkdayjobs.com/External"])
        row = mock_db[css.CAREER_SOURCES].find_one({"source_id": sid})
        assert row["state"] == css.STATE_VALIDATED
        assert row["career_landing_url"] == "https://jobs.acme.example/search"
        assert len(row["redirect_chain"]) == 2
        assert row["ownership_verified"] is True

    def test_a_DEAD_source_is_still_revivable(self, mock_db):
        """Re-registration fetched the endpoint and followed its redirects, so
        it has fresh evidence the board exists. `dead` carries no content
        verdict to forget."""
        sid = self._register()
        mock_db[css.CAREER_SOURCES].update_one(
            {"source_id": sid}, {"$set": {"state": css.STATE_DEAD}})
        self._register()
        row = mock_db[css.CAREER_SOURCES].find_one({"source_id": sid})
        assert row["state"] == css.STATE_OWNERSHIP_VERIFIED

    def test_the_content_pass_can_still_demote(self, mock_db):
        """The guard is on REGISTRATION, not on the state machine. A source
        that stops serving must still be able to fall."""
        sid = self._register()
        mock_db[css.CAREER_SOURCES].update_one(
            {"source_id": sid}, {"$set": {"state": css.STATE_VALIDATED}})
        css.record_failure(sid, failure_code=css.FAIL_HTTP_403)
        row = mock_db[css.CAREER_SOURCES].find_one({"source_id": sid})
        assert row["state"] == css.STATE_BLOCKED

    def test_the_bulk_path_applies_the_SAME_guard(self, mock_db):
        sid = self._register()
        mock_db[css.CAREER_SOURCES].update_one(
            {"source_id": sid}, {"$set": {"state": css.STATE_VALIDATED}})
        rec = css.build_source(
            employer_id="emp_000000000001", ats_vendor="workday", tenant="acme",
            verified_domain="acme.example",
            canonical_endpoint="https://acme.wd1.myworkdayjobs.com/External",
            ownership_evidence=[
                evidence(css.SIGNAL_DOMAIN_LINK, source="acme.example",
                         confirmed=True)])
        css.bulk_upsert_sources([rec])
        row = mock_db[css.CAREER_SOURCES].find_one({"source_id": sid})
        assert row["state"] == css.STATE_VALIDATED

    def test_the_bulk_path_still_INSERTS_a_new_source(self, mock_db):
        rec = css.build_source(
            employer_id="emp_00000000000f", ats_vendor="lever", tenant="newco",
            canonical_endpoint="https://jobs.lever.co/newco")
        out = css.bulk_upsert_sources([rec])
        assert out["seen"] == 1
        assert mock_db[css.CAREER_SOURCES].find_one(
            {"source_id": rec["source_id"]}) is not None

    def test_the_bulk_path_chunks_its_lookup(self, mock_db, monkeypatch):
        """A seeding run passes every source in the registry. One `$in` of
        eleven thousand ids is a query document nobody wants to explain."""
        seen_sizes = []
        col = mock_db[css.CAREER_SOURCES]
        real_find = col.find

        def _spy(query=None, *a, **kw):
            ids = ((query or {}).get("source_id") or {}).get("$in")
            if ids is not None:
                seen_sizes.append(len(ids))
            return real_find(query, *a, **kw)

        monkeypatch.setattr(col, "find", _spy)
        records = [css.build_source(employer_id=f"emp_{i:012x}",
                                    ats_vendor="lever", tenant=f"co{i}",
                                    canonical_endpoint=f"https://jobs.lever.co/co{i}")
                   for i in range(25)]
        css.bulk_upsert_sources(records, batch_size=10)
        assert seen_sizes, "the guard must read the stored states"
        assert max(seen_sizes) <= 10
