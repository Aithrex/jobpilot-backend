"""The business-model backfill: dry-run by default, idempotent, reversible,
joins industry codes by DOMAIN only, and never touches the owner override."""
import importlib.util
from pathlib import Path

import pytest

from _vg_support import current_db
from services import employer_business_model as ebm

_PATH = Path(__file__).resolve().parent.parent / "scripts" / "backfill_employer_business_model.py"


def _mod():
    spec = importlib.util.spec_from_file_location("bf_ebm", _PATH)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture()
def db():
    d = current_db()
    d.employers.delete_many({})
    d.companies.delete_many({})
    d.employers.insert_many([
        {"employer_id": "emp_cap", "display_name": "Capgemini", "canonical_domain": "capgemini.com"},
        {"employer_id": "emp_chip", "display_name": "Chipco", "employer_categories": ["semiconductor"]},
        {"employer_id": "emp_tmp", "display_name": "Tempworks", "canonical_domain": "tempworks.example"},
        {"employer_id": "emp_tmp2", "display_name": "Tempworks Bakery"},
        {"employer_id": "emp_own", "display_name": "Accenture", "canonical_domain": "accenture.com",
         "business_model_override": {"kind": "operating", "evidence": "owner", "set_by": "owner"}},
    ])
    d.companies.insert_one({"name": "TEMPWORKS INC", "domain": "tempworks.example", "naics": "561320"})
    # Same NAME, no domain: must NOT inherit the NAICS code.
    d.companies.insert_one({"name": "TEMPWORKS BAKERY", "naics": "561320"})
    yield d
    d.employers.delete_many({})
    d.companies.delete_many({})


def test_dry_run_writes_nothing(db):
    s = _mod().run(db)
    assert s["would_write"] == 5 and s["written"] == 0
    assert db.employers.count_documents({"business_model": {"$exists": True}}) == 0


def test_apply_is_idempotent_domain_joined_and_reversible(db):
    m = _mod()
    s = m.run(db, apply=True)
    assert s["written"] == 5
    by = {d["employer_id"]: d["business_model"] for d in db.employers.find()}
    assert by["emp_cap"]["kind"] == "consultancy" and by["emp_cap"]["basis"] == "research_register"
    assert by["emp_chip"]["kind"] == "operating"
    assert by["emp_tmp"]["kind"] == "staffing" and by["emp_tmp"]["basis"] == "naics"
    assert by["emp_tmp2"]["kind"] == "unknown"
    assert by["emp_own"]["basis"] == "owner_override"
    assert db.employers.find_one({"employer_id": "emp_own"})["business_model_override"]["kind"] == "operating"
    again = m.run(db, apply=True)
    assert again["written"] == 0 and again["unchanged"] == 5
    assert m.run(db, unstamp=True)["written"] == 0               # dry run
    undo = m.run(db, unstamp=True, apply=True)
    assert undo["written"] == 5
    assert db.employers.count_documents({"business_model": {"$exists": True}}) == 0
    assert ebm.EBM_VERSION == "ebm-1.0.0"
