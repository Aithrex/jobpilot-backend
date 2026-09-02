"""A posting gets a canonical employer only when the board's owner is proven.

Three claims, and each one is a separate failure if it breaks:

1. **Owner-verified means owner-verified.** A board with no accepted ownership
   evidence never yields a `canonical_employer_id`, however plausible its slug
   or however many jobs it returns.
2. **Ambiguity is quarantined, not guessed.** Two employers own-verifying one
   board attribute nothing unless exactly one of them holds a verified-domain
   chain — and even then the board still lands in the review queue, because
   attribution and identity are two different questions.
3. **Nothing that already worked stops working.** `employer_id` is untouched,
   and a legacy company filter still matches the postings it matched before.
"""
from __future__ import annotations

import pytest

from services import canonical_employer as ce
from services import career_source_service as css
from services.career_source_service import evidence


def _chain_ev():
    return evidence(css.SIGNAL_DOMAIN_LINK, source="https://acme.com/careers",
                    value="acme.com -> greenhouse:acme", confirmed=True)


def _corroborating_ev():
    return [evidence(css.SIGNAL_PROVIDER_OWNER, source="greenhouse:acme",
                     value="Acme", confirmed=True),
            evidence(css.SIGNAL_CURATED_ROW, source="python_catalog",
                     value="Acme", confirmed=True)]


def _source(db, *, employer_id, vendor, tenant, ownership_evidence):
    rec = css.build_source(employer_id=employer_id, ats_vendor=vendor,
                           tenant=tenant, ownership_evidence=ownership_evidence)
    db[css.CAREER_SOURCES].insert_one(dict(rec))
    return rec


def _employer(db, *, employer_id, name, status="active", parent=None):
    db["employers"].insert_one({
        "employer_id": employer_id, "display_name": name,
        "operating_status": status, "parent_employer_id": parent,
        "name_key": name.lower(), "aliases": [name],
    })


@pytest.fixture(autouse=True)
def _clear_cache():
    ce.reset_cache()
    yield
    ce.reset_cache()


# ---------------------------------------------------------------------------
# 1. Ownership, or nothing
# ---------------------------------------------------------------------------
class TestOnlyOwnerVerifiedBoardsAttribute:

    def test_a_board_with_two_corroborating_signals_attributes(self, mock_db):
        _employer(mock_db, employer_id="emp_aaaaaaaaaaaa", name="Acme")
        _source(mock_db, employer_id="emp_aaaaaaaaaaaa", vendor="greenhouse",
                tenant="acme", ownership_evidence=_corroborating_ev())
        own = ce.build_board_ownership(mock_db)
        d = own.resolve_board(ats="greenhouse", tenant="acme")
        assert d.employer_id == "emp_aaaaaaaaaaaa"
        assert d.basis == ce.BASIS_OWNER_VERIFIED_BOARD
        assert d.reason == ""

    def test_a_responding_endpoint_and_a_similar_slug_attribute_nothing(self, mock_db):
        """The exact evidence the old probe path called `validated`."""
        _employer(mock_db, employer_id="emp_bbbbbbbbbbbb", name="Forward")
        _source(mock_db, employer_id="emp_bbbbbbbbbbbb", vendor="lever",
                tenant="forward", ownership_evidence=[
                    evidence(css.SIGNAL_SLUG_SIMILARITY, source="util",
                             value="slug_is_trustworthy=True", confirmed=True),
                    evidence(css.SIGNAL_ENDPOINT_RESPONDS, source="lever",
                             value="12 postings", confirmed=True)])
        own = ce.build_board_ownership(mock_db)
        d = own.resolve_board(ats="lever", tenant="forward")
        assert not d.assigned
        assert d.reason == ce.Q_OWNERSHIP_UNVERIFIED

    def test_one_corroborating_class_is_not_two(self, mock_db):
        _employer(mock_db, employer_id="emp_cccccccccccc", name="Public")
        _source(mock_db, employer_id="emp_cccccccccccc", vendor="ashby",
                tenant="public", ownership_evidence=[
                    evidence(css.SIGNAL_CURATED_ROW, source="python_catalog",
                             value="Public", confirmed=True)])
        own = ce.build_board_ownership(mock_db)
        assert not own.resolve_board(ats="ashby", tenant="public").assigned

    def test_an_unknown_board_is_unverified_not_an_error(self, mock_db):
        own = ce.build_board_ownership(mock_db)
        d = own.resolve_board(ats="greenhouse", tenant="never-seen")
        assert not d.assigned and d.reason == ce.Q_OWNERSHIP_UNVERIFIED

    def test_a_posting_with_no_tenant_is_refused_by_its_own_reason(self, mock_db):
        own = ce.build_board_ownership(mock_db)
        d = own.resolve_board(ats="greenhouse", tenant="")
        assert d.reason == ce.Q_NO_BOARD_KEY

    def test_a_marketplace_board_never_attributes(self, mock_db):
        """Jobgether owns its Lever board and none of the jobs on it."""
        _employer(mock_db, employer_id="emp_dddddddddddd", name="Jobgether")
        _source(mock_db, employer_id="emp_dddddddddddd", vendor="lever",
                tenant="jobgether", ownership_evidence=_corroborating_ev())
        own = ce.build_board_ownership(mock_db)
        assert own.resolve_board(ats="lever", tenant="jobgether").assigned
        d = own.resolve_board(ats="lever", tenant="jobgether",
                              board_kind="marketplace")
        assert not d.assigned and d.reason == ce.Q_MARKETPLACE_BOARD


# ---------------------------------------------------------------------------
# 2. The join keys that were dead
# ---------------------------------------------------------------------------
class TestKeyNormalization:

    def test_vendor_punctuation_does_not_break_the_join(self, mock_db):
        """`oracle-orc` on a posting, `oracle_orc` on a source. One board."""
        _employer(mock_db, employer_id="emp_eeeeeeeeeeee", name="Chubb")
        _source(mock_db, employer_id="emp_eeeeeeeeeeee", vendor="oracle_orc",
                tenant="eofe", ownership_evidence=_corroborating_ev())
        own = ce.build_board_ownership(mock_db)
        assert own.resolve_board(ats="oracle-orc", tenant="eofe").assigned

    def test_tenant_case_does_not_break_the_join(self, mock_db):
        _employer(mock_db, employer_id="emp_ffffffffffff", name="Avery Dennison")
        _source(mock_db, employer_id="emp_ffffffffffff", vendor="smartrecruiters",
                tenant="AveryDennison", ownership_evidence=_corroborating_ev())
        own = ce.build_board_ownership(mock_db)
        assert own.resolve_board(ats="smartrecruiters",
                                 tenant="averydennison").assigned


# ---------------------------------------------------------------------------
# 3. Parents, brands and genuine conflicts
# ---------------------------------------------------------------------------
class TestAmbiguityIsQuarantined:

    def test_a_brand_and_its_parent_are_one_employer(self, mock_db):
        """Slack and Salesforce on one board is not a conflict."""
        _employer(mock_db, employer_id="emp_111111111111", name="Salesforce")
        _employer(mock_db, employer_id="emp_222222222222", name="Slack",
                  status="product_or_brand", parent="emp_111111111111")
        _source(mock_db, employer_id="emp_111111111111", vendor="workday",
                tenant="salesforce", ownership_evidence=_corroborating_ev())
        _source(mock_db, employer_id="emp_222222222222", vendor="workday",
                tenant="salesforce", ownership_evidence=_corroborating_ev())
        own = ce.build_board_ownership(mock_db)
        d = own.resolve_board(ats="workday", tenant="salesforce")
        assert d.employer_id == "emp_111111111111"
        assert d.basis == ce.BASIS_OWNER_VERIFIED_BOARD
        assert own.conflicts == {}

    def test_two_unrelated_employers_on_one_board_attribute_nothing(self, mock_db):
        """Truist and the AFCO entities it acquired: both hold a chain."""
        _employer(mock_db, employer_id="emp_333333333333", name="AFCO Direct")
        _employer(mock_db, employer_id="emp_444444444444", name="AFCO Credit")
        for eid in ("emp_333333333333", "emp_444444444444"):
            _source(mock_db, employer_id=eid, vendor="workday", tenant="truist",
                    ownership_evidence=[_chain_ev()] + _corroborating_ev())
        own = ce.build_board_ownership(mock_db)
        d = own.resolve_board(ats="workday", tenant="truist")
        assert not d.assigned
        assert d.reason == ce.Q_CONFLICTING_OWNERS
        assert ("workday", "truist") in own.unresolved_conflicts

    def test_a_domain_chain_outranks_corroborating_signals(self, mock_db):
        """CITIGROUP INC (domain) beside Citi (name-derived, no chain)."""
        _employer(mock_db, employer_id="emp_555555555555", name="CITIGROUP INC")
        _employer(mock_db, employer_id="emp_666666666666", name="Citi")
        _source(mock_db, employer_id="emp_555555555555", vendor="workday",
                tenant="citi", ownership_evidence=[_chain_ev()])
        _source(mock_db, employer_id="emp_666666666666", vendor="workday",
                tenant="citi", ownership_evidence=_corroborating_ev())
        own = ce.build_board_ownership(mock_db)
        d = own.resolve_board(ats="workday", tenant="citi")
        assert d.employer_id == "emp_555555555555"
        assert d.basis == ce.BASIS_DOMAIN_CHAIN_PRECEDENCE

    def test_precedence_still_files_the_board_for_review(self, mock_db):
        """Attribution is settled; whether these are one employer is not."""
        _employer(mock_db, employer_id="emp_555555555555", name="CITIGROUP INC")
        _employer(mock_db, employer_id="emp_666666666666", name="Citi")
        _source(mock_db, employer_id="emp_555555555555", vendor="workday",
                tenant="citi", ownership_evidence=[_chain_ev()])
        _source(mock_db, employer_id="emp_666666666666", vendor="workday",
                tenant="citi", ownership_evidence=_corroborating_ev())
        own = ce.build_board_ownership(mock_db)
        key = ("workday", "citi")
        assert key in own.conflicts, "a settled board is still a contested board"
        assert key in own.precedence_boards
        assert key not in own.unresolved_conflicts

        ce.record_board_conflict(mock_db, key, own.conflicts[key], postings=1298,
                                 attributed_to=own.precedence_boards[key],
                                 resolved_by=ce.BASIS_DOMAIN_CHAIN_PRECEDENCE)
        rows = ce.open_board_conflicts(mock_db)
        assert len(rows) == 1
        assert rows[0]["attribution_blocked"] is False
        assert ce.open_board_conflicts(mock_db, blocked_only=True) == []

    def test_a_name_match_alone_never_merges_two_employers(self, mock_db):
        """There is no name-comparison path into attribution. At all."""
        _employer(mock_db, employer_id="emp_777777777777", name="Ramp")
        _employer(mock_db, employer_id="emp_888888888888", name="RAMP HOLDINGS INC")
        for eid in ("emp_777777777777", "emp_888888888888"):
            _source(mock_db, employer_id=eid, vendor="ashby", tenant="ramp",
                    ownership_evidence=_corroborating_ev())
        own = ce.build_board_ownership(mock_db)
        assert not own.resolve_board(ats="ashby", tenant="ramp").assigned


# ---------------------------------------------------------------------------
# 4. The write path
# ---------------------------------------------------------------------------
class TestStampFields:

    def test_an_attributable_posting_gets_three_fields_and_no_unset(self, mock_db):
        _employer(mock_db, employer_id="emp_aaaaaaaaaaaa", name="Acme")
        _source(mock_db, employer_id="emp_aaaaaaaaaaaa", vendor="greenhouse",
                tenant="acme", ownership_evidence=_corroborating_ev())
        own = ce.build_board_ownership(mock_db)
        stamp, clear = ce.stamp_fields(
            {"ats": "greenhouse", "tenant": "acme", "board_kind": "employer"},
            ownership=own)
        assert stamp[ce.FIELD] == "emp_aaaaaaaaaaaa"
        assert stamp[ce.VERSION_FIELD] == ce.CANONICAL_EMPLOYER_VERSION
        assert clear == []

    def test_a_board_that_becomes_contested_CLEARS_a_stale_id(self, mock_db):
        """A wrong id is worse than none — so refusal must unset, not skip."""
        own = ce.build_board_ownership(mock_db)
        stamp, clear = ce.stamp_fields(
            {"ats": "workday", "tenant": "truist"}, ownership=own)
        assert stamp == {}
        assert set(clear) == {ce.FIELD, ce.BASIS_FIELD, ce.VERSION_FIELD}

    def test_an_unreachable_registry_writes_nothing_rather_than_failing(self):
        """Attribution is additive. It must never take a crawl down."""
        class _Boom:
            def __getitem__(self, name):
                raise RuntimeError("registry down")
        ce.reset_cache()
        assert ce.cached_board_ownership(_Boom()) is None
        assert ce.stamp_fields({"ats": "greenhouse", "tenant": "acme"},
                               db=_Boom()) == ({}, [])


# ---------------------------------------------------------------------------
# 5. Backward compatibility
# ---------------------------------------------------------------------------
class TestLegacyCompatibility:

    def test_the_legacy_employer_id_is_never_read_or_written(self, mock_db):
        """`employer_id` is a slug three production indexes key on."""
        _employer(mock_db, employer_id="emp_aaaaaaaaaaaa", name="Acme")
        _source(mock_db, employer_id="emp_aaaaaaaaaaaa", vendor="greenhouse",
                tenant="acme", ownership_evidence=_corroborating_ev())
        own = ce.build_board_ownership(mock_db)
        posting = {"ats": "greenhouse", "tenant": "acme",
                   "employer_id": "acme-inc", "board_kind": "employer"}
        stamp, _clear = ce.stamp_fields(posting, ownership=own)
        assert "employer_id" not in stamp
        assert posting["employer_id"] == "acme-inc"

    def test_the_company_filter_still_accepts_a_legacy_slug(self):
        from services import tech_feed_service as tfs
        q = tfs.build_query(tfs.normalize_request({"company_ids": ["acme-inc"]}))
        clauses = [c for c in q.get("$and", []) if "$or" in c or "employer_id" in c]
        flat = str(clauses)
        assert "acme-inc" in flat
        assert "canonical_employer_id" not in flat

    def test_a_canonical_id_filters_on_the_canonical_field(self):
        from services import tech_feed_service as tfs
        q = tfs.build_query(tfs.normalize_request(
            {"company_ids": ["emp_fd96667721fe"]}))
        flat = str(q)
        assert '"canonical_employer_id"' in flat.replace("'", '"')

    def test_a_mixed_filter_matches_either_spelling(self):
        from services import tech_feed_service as tfs
        q = tfs.build_query(tfs.normalize_request(
            {"company_ids": ["emp_fd96667721fe", "acme-inc"]}))
        flat = str(q).replace("'", '"')
        assert '"canonical_employer_id"' in flat
        assert "acme-inc" in flat

    def test_employer_key_prefers_canonical_then_board_then_name(self):
        from services.tech_feed_service import employer_key_of
        assert employer_key_of({"canonical_employer_id": "emp_1", "ats": "workday",
                                "tenant": "jpmc"}) == "emp_1"
        assert employer_key_of({"ats": "workday", "tenant": "jpmc"}) == "workday|jpmc"
        assert employer_key_of({"employer_name": "Nutmeg"}) == "nutmeg"


class TestTheIndexesAreUSABLE:
    """An index that exists and is never chosen is not an index.

    Both canonical indexes were declared `partialFilterExpression:
    {canonical_employer_id: {"$type": "string"}}`. They indexed exactly the
    right rows — and the planner never used either one, because a partial
    index is eligible only for a query it can prove is a SUBSET of the filter,
    and `{canonical_employer_id: "emp_..."}` does not prove `$type: "string"`;
    the subset check does not reason about types. Measured on live inventory
    the employer posting-count aggregation ran 1,544 ms on an unrelated
    fallback plan and 17 ms once the same index was declared with `$exists`.

    `$exists: true` IS provable from an equality (and from an `$in` of
    equalities), which is every shape we issue.
    """

    def _specs(self, monkeypatch):
        from services import canonical_employer as ce
        captured = []

        class _Coll:
            def __init__(self, name):
                self.name = name

            def create_index(self, keys, **kw):
                captured.append((self.name, keys, kw))
                return kw.get("name") or "idx"

        class _DB:
            def __getitem__(self, name):
                return _Coll(name)

        monkeypatch.setattr(ce, "_db", lambda: _DB())
        ce.ensure_indexes()
        return captured

    def test_the_posting_indexes_are_partial_on_EXISTENCE_not_on_type(self, monkeypatch):
        specs = self._specs(monkeypatch)
        posting = [kw for (coll, _keys, kw) in specs if coll == "job_postings"]
        assert len(posting) == 3, ("the ranked window, the keyset "
                                   "continuation, and the branch index a "
                                   "mixed `$or` needs")
        for kw in posting:
            pfe = kw.get("partialFilterExpression")
            assert pfe == {"canonical_employer_id": {"$exists": True}}, (
                "a $type filter makes the index ineligible for the equality "
                "queries that are the only reason it exists")

    def test_every_posting_shape_is_covered(self, monkeypatch):
        specs = self._specs(monkeypatch)
        keys = [tuple(k for k, _ in ks) for (coll, ks, _kw) in specs
                if coll == "job_postings"]
        assert ("status", "canonical_employer_id", "posted_at") in keys
        assert ("status", "canonical_employer_id", "posted_at", "job_id") in keys
        # Prefixed on the FIELD, not on `status`. A company filter carrying
        # both spellings is a `$or`; Mongo subplans only one rooted `$or` and
        # the freshness window is already it, so a branch that does not carry
        # `status` cannot use either index above. 8,033 ms vs 467 ms live.
        assert ("canonical_employer_id", "status") in keys
