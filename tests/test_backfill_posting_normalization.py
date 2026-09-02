"""The v4 backfill improves what it can prove and stamps only what it finished.

Two things here are load-bearing and easy to get wrong:

* the version stamp is what `job_ingest_worker._listing_implies_same_content`
  reads to decide a posting needs no re-fetch, so stamping a row this pass
  could only half-upgrade would freeze it in that state forever; and
* a stated seniority is the employer's own answer and must never be
  overwritten by an inference.
"""
import importlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

bf = importlib.import_module("scripts.backfill_posting_normalization")


def _doc(**over):
    d = {
        "title_raw": "Software Engineer",
        "seniority": "unknown",
        "locations": [{"raw": "Austin, TX, USA", "city": "Austin",
                       "region": "TX", "country": "USA", "key": "austin-tx-usa"}],
        "employment_type": "Full time",
        "requirements": {},
        "work_mode": "unknown",
    }
    d.update(over)
    return d


class TestSeniorityFromTheEmployersOwnStatedYears:
    def test_a_stated_year_count_becomes_a_band(self):
        sets, why = bf.plan(_doc(requirements={"required_years": 6}))
        assert sets["seniority"] == "senior"
        assert any("seniority" in w for w in why)

    def test_a_posting_that_states_nothing_stays_unknown(self):
        """`unknown` is the true answer, and retrieval ORs it in rather than
        excluding it — so inventing `mid` here would be a claim, not a
        default."""
        sets, _ = bf.plan(_doc())
        assert "seniority" not in sets

    def test_a_seniority_the_employer_stated_is_never_overwritten(self):
        sets, _ = bf.plan(_doc(seniority="entry", requirements={"required_years": 12}))
        assert "seniority" not in sets

    def test_years_alone_never_reach_executive(self):
        """"15+ years of experience" is evidence of a senior posting, not of a
        C-suite one, and reading it as executive puts ordinary senior roles
        three bands above every candidate who applies."""
        sets, _ = bf.plan(_doc(title_raw="Software Engineer",
                               requirements={"required_years": 20}))
        assert sets.get("seniority") in {"staff", None}


class TestCountryCodeAndEmploymentType:
    def test_a_country_spelling_gains_its_iso_code(self):
        sets, _ = bf.plan(_doc())
        assert sets["locations"][0]["country_code"] == "US"

    def test_an_unrecognised_country_gains_nothing_rather_than_a_guess(self):
        d = _doc(locations=[{"raw": "Somewhere", "city": "", "region": "",
                             "country": "Freedonia", "key": "somewhere"}])
        sets, _ = bf.plan(d)
        assert "locations" not in sets

    def test_the_raw_employment_type_is_never_rewritten(self):
        """`employment_type` is digested by `compute_content_hash`; renaming it
        would re-hash and re-parse the whole inventory for no change in
        meaning."""
        sets, _ = bf.plan(_doc())
        assert "employment_type" not in sets
        assert sets["employment_type_normalized"] == "full_time"


class TestWorkModeNeedsABody:
    def test_no_body_means_no_work_mode_claim(self):
        sets, _ = bf.plan(_doc(), body="")
        assert "work_mode" not in sets

    def test_a_body_that_states_remote_fills_the_gap(self):
        sets, _ = bf.plan(_doc(), body="This is a fully remote position.")
        assert sets["work_mode"] == "remote"

    def test_a_stated_work_mode_is_not_overwritten_by_the_body(self):
        sets, _ = bf.plan(_doc(work_mode="onsite"),
                          body="This is a fully remote position.")
        assert "work_mode" not in sets


class TestTheBodyReaderNeverBreaksTheBackfill:
    def test_bodies_off_reports_itself_as_not_live(self):
        read, live = bf._body_reader(False)
        assert live is False and read({"description_ref": "x"}) == ""

    def test_an_unavailable_store_degrades_instead_of_raising(self, monkeypatch):
        """A backfill that refuses to run because S3 is unreachable does less
        than one that runs the three field-derived improvements."""
        import types
        fake = types.SimpleNamespace(available=lambda: False)
        monkeypatch.setitem(sys.modules, "services.job_content_store", fake)
        read, live = bf._body_reader(True)
        assert live is False and read({"description_ref": "x"}) == ""


class TestBoardKind:
    def test_a_missing_board_kind_is_filled(self):
        sets, why = bf.plan(_doc())
        assert sets["board_kind"] == "employer"
        assert any("board_kind" in w for w in why)

    def test_a_marketplace_tenant_is_recorded_as_one(self):
        sets, _ = bf.plan(_doc(tenant="jobgether", employer_name="Jobgether"))
        assert sets["board_kind"] == "marketplace"

    def test_an_existing_value_is_not_overwritten(self):
        sets, _ = bf.plan(_doc(board_kind="marketplace"))
        assert "board_kind" not in sets
