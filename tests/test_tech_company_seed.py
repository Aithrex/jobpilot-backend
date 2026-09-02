"""The curated seed is a probe list, and it has to be a CLEAN one.

A row here is a candidate to probe: it asserts only that the employer exists
and hires in tech. That makes a wrong domain cheap (a missed board) and a
duplicate expensive (a second discovery attempt against a board that already
has one, and a second employer row for one employer once the registry reads
this file).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from data.tech_companies import TECH_COMPANIES, as_records  # noqa: E402

from services import employer_registry as er  # noqa: E402


def _name_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def test_every_domain_appears_once():
    seen = {}
    dupes = []
    for name, domain, _cat in TECH_COMPANIES:
        d = domain.lower()
        if d in seen:
            dupes.append((d, seen[d], name))
        seen[d] = name
    assert not dupes, dupes


def test_every_name_appears_once():
    seen = {}
    dupes = []
    for name, _domain, _cat in TECH_COMPANIES:
        k = _name_key(name)
        if k in seen:
            dupes.append((name, seen[k]))
        seen[k] = name
    assert not dupes, dupes


def test_every_domain_is_usable_as_an_identity():
    """`employer_registry` keys on the registrable domain. A row whose domain
    does not survive normalisation silently falls back to a name-derived id,
    which is the weakest identity we have."""
    bad = [(n, d) for n, d, _ in TECH_COMPANIES if not er.normalize_domain(d)]
    assert not bad, bad


def test_no_row_carries_a_domain_a_curated_override_rejects():
    """`capital.com` is a CFD trading site and must never be seeded as
    Capital One's."""
    wrong = er.wrong_domains()
    bad = [(n, d) for n, d, _ in TECH_COMPANIES
           if wrong.get(er.normalize_domain(d))
           and er.name_key(wrong[er.normalize_domain(d)]) == er.name_key(n)]
    assert not bad, bad


def test_no_row_is_a_documented_wrong_owner():
    bad = [(n, d) for n, d, _ in TECH_COMPANIES
           if er.name_key(n) in er.wrong_name_keys()]
    assert not bad, bad


def test_every_category_maps_into_the_registry_vocabulary():
    """An unmapped category becomes `other`, which is legal but makes the row
    invisible to cohort reporting. The seed must not rely on that."""
    unmapped = sorted({c for _n, _d, c in TECH_COMPANIES
                       if er.canonical_category(c) == "other"})
    assert not unmapped, unmapped


@pytest.mark.parametrize("cohort", [
    "gaming", "semiconductor", "security", "ai_ml", "fintech", "robotics",
    "cloud_data", "it_services", "software_saas", "devtools", "healthtech",
    "consumer",
])
def test_every_reported_cohort_has_seed_rows(cohort):
    """A cohort the coverage report names must be probeable. `gaming` had
    ZERO rows before 2026-09-02, so it could not be probed, so it could not
    be crawled, so its coverage number could only ever have been zero."""
    have = [n for n, _d, c in TECH_COMPANIES
            if er.canonical_category(c) == cohort]
    assert len(have) >= 5, (cohort, len(have))


def test_the_audit_candidates_are_present():
    """Named in the coverage brief. Presence here is a commitment to PROBE
    them and report a state, not a claim that they are hiring."""
    want = ["DeepL", "Pydantic", "Turso", "Wasmer", "Deno", "Oven",
            "Oxide Computer Company", "Aikido Security", "Protect AI",
            "Lakera", "HiddenLayer", "Ampere Computing", "Celestial AI",
            "d-Matrix", "Enfabrica"]
    have = {_name_key(n) for n, _d, _c in TECH_COMPANIES}
    missing = [w for w in want if _name_key(w) not in have]
    assert not missing, missing


def test_as_records_marks_every_row_as_software():
    """`probe_software_company_boards.py --signal software` sweeps on this
    flag; a row without it is seeded and then never probed."""
    records = as_records()
    assert len(records) == len(TECH_COMPANIES)
    assert all(r["signals"]["software"] is True for r in records)
    assert all(r["domain"] and r["name"] for r in records)
