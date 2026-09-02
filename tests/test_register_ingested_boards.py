"""Registering a board we already crawl is bookkeeping, not a claim of ownership.

1,437 boards carrying 124,226 active postings had no `career_sources` row at
all — Wells Fargo, Northrop Grumman, RTX, PwC, CVS Health, Amazon. We ingest
their jobs every crawl and the coverage registry did not know their boards
existed, which is why pass 2 (walk from the employer's domain TO a board) had
nothing to walk to.

What must NOT happen is the registration inventing ownership. These rows land
at `content_validated`; a second, independent evidence class still has to be
earned.
"""
import collections
import importlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "python"))
sys.path.insert(0, str(ROOT))

rib = importlib.import_module("scripts.register_ingested_boards")
from services import career_source_service as css   # noqa: E402


class TestVendorKey:
    def test_the_posting_and_registry_spellings_agree(self):
        """`oracle-orc` on a posting, `oracle_orc` in the registry. Comparing
        them raw reported 100 already-registered boards as missing."""
        assert rib.vendor_key("oracle-orc") == rib.vendor_key("oracle_orc")

    def test_blank_stays_blank(self):
        assert rib.vendor_key(None) == "" and rib.vendor_key("  ") == ""


class TestTheProviderNamingItsOwner:
    def test_a_board_whose_postings_all_name_one_employer(self):
        names = collections.Counter({"Northrop Grumman": 968})
        owner, share, total = rib.board_owner_signal(
            names, consistency=0.90, min_postings=3)
        assert owner == "Northrop Grumman" and share == 1.0 and total == 968

    def test_a_board_naming_many_employers_names_no_owner(self):
        """Jobgether's 6,533 postings name hundreds of companies. This is
        exactly the case the rule must refuse — it is the difference between
        a provider statement and a slug that looks similar."""
        names = collections.Counter({f"Company {i}": 20 for i in range(50)})
        owner, _, _ = rib.board_owner_signal(names, consistency=0.90, min_postings=3)
        assert owner is None

    def test_a_board_just_below_the_threshold_names_no_owner(self):
        names = collections.Counter({"Mostly Co": 88, "Other Co": 12})
        owner, share, _ = rib.board_owner_signal(
            names, consistency=0.90, min_postings=3)
        assert owner is None and 0.87 < share < 0.89

    def test_too_few_postings_proves_nothing(self):
        names = collections.Counter({"Tiny Co": 2})
        owner, _, total = rib.board_owner_signal(
            names, consistency=0.90, min_postings=3)
        assert owner is None and total == 2

    def test_a_blank_employer_name_is_not_an_owner(self):
        owner, _, _ = rib.board_owner_signal(
            collections.Counter({"": 100}), consistency=0.9, min_postings=3)
        assert owner is None


class TestOneSignalClassIsNeverEnough:
    def test_a_provider_named_board_lands_at_content_validated_not_validated(self):
        src = css.build_source(
            employer_id="emp_x", ats_vendor="workday", tenant="ngc",
            ownership_evidence=[css.evidence(
                css.SIGNAL_PROVIDER_OWNER, source="workday:postings",
                value="Northrop Grumman names 100% of 968 postings")],
            content_validation_evidence=[css.evidence(
                "content_fetch", source="job_inventory", value="968 postings")],
            job_count_last=968)
        assert src["state"] == css.STATE_CONTENT_VALIDATED
        assert src["ownership_verified"] is False

    def test_the_signal_is_the_ats_provider_class_not_a_heuristic(self):
        """A slug that looks similar is `heuristic` and never counts. The
        board's own postings naming the employer is the provider speaking."""
        assert css.SIGNAL_CLASSES[css.SIGNAL_PROVIDER_OWNER] == css.CLASS_ATS_PROVIDER


class TestARefreshNeverWritesAWeakerVerdict:
    """`upsert_source` puts `state` and `ownership_verified` in `_MUTABLE`, so
    an unguarded re-run overwrites them. This pass knows about two evidence
    classes; a live domain walk knows more, and clobbering its verdict would
    turn a re-run into a regression."""

    def test_validated_outranks_everything_this_pass_can_produce(self):
        assert rib.STATE_RANK[css.STATE_VALIDATED] > \
            rib.STATE_RANK[css.STATE_CONTENT_VALIDATED]
        assert rib.STATE_RANK[css.STATE_OWNERSHIP_VERIFIED] > \
            rib.STATE_RANK[css.STATE_CONTENT_VALIDATED]

    def test_candidate_is_worth_less_than_content_validated(self):
        assert rib.STATE_RANK[css.STATE_CANDIDATE] < \
            rib.STATE_RANK[css.STATE_CONTENT_VALIDATED]

    def test_a_dead_source_ranks_lowest_so_a_live_board_can_replace_it(self):
        assert rib.STATE_RANK[css.STATE_DEAD] == min(rib.STATE_RANK.values())

    def test_every_state_the_service_defines_has_a_rank(self):
        """A state with no rank would compare as 0 and let any refresh
        overwrite it."""
        assert set(css.STATES) <= set(rib.STATE_RANK)


class TestTwoIndependentClassesReachValidated:
    def test_provider_plus_an_agreeing_catalog_row_is_enough(self):
        src = css.build_source(
            employer_id="emp_x", ats_vendor="workday", tenant="ngc",
            ownership_evidence=[
                css.evidence(css.SIGNAL_PROVIDER_OWNER, source="workday:postings",
                             value="Northrop Grumman names 100% of 968 postings"),
                css.evidence(css.SIGNAL_CURATED_ROW, source="python_catalog",
                             value="Northrop Grumman at workday:ngc"),
            ],
            content_validation_evidence=[css.evidence(
                "content_fetch", source="job_inventory", value="968 postings")],
            job_count_last=968)
        assert src["ownership_verified"] is True
        assert src["state"] == css.STATE_VALIDATED

    def test_the_catalog_rows_name_exact_endpoints(self):
        rows = rib.curated_rows()
        assert len(rows) > 1000
        assert all(isinstance(k, tuple) and len(k) == 2 for k in rows)
        assert all(v and isinstance(v, str) for v in rows.values())
