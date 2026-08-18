"""Every declared ingest source must actually be dispatched.

ADR-004 gave Oracle ORC and iCIMS their own sources and their own shard counts,
and `job_ingest_worker.SOURCES` has listed all four ever since. EventBridge only
ever fired two. So the providers worked, the tests passed, the code deployed —
and `job_source_state` never grew an `oracle` or `icims` row, because nothing
asked it to.

That failure is invisible to every other test in this suite, because each layer
is individually correct. It is only visible by comparing the layers, which is
what these tests do:

    SOURCES  <->  the EventBridge rules in infra/terraform/schedulers.tf
    SOURCES  <->  the dispatch branch in scheduled_jobs.py

Both directions matter. A source with no rule is never crawled; a rule with no
source fires forever and dispatches nothing.
"""
import pathlib
import re

import pytest

from services.job_ingest_worker import (SHARD_COUNTS, SOURCE_CAREER,
                                        SOURCE_WORKDAY, SOURCES)
from services.scheduled_jobs import _source_for_ingest_rule

_TF = (pathlib.Path(__file__).resolve().parents[1]
       / "infra" / "terraform" / "schedulers.tf")

#: Rules that are ingest-related but are not a per-source shard tick.
_NON_SOURCE_RULES = {"ingest_maintenance"}


def _terraform_ingest_rules() -> set:
    """Rule keys declared in `local.scheduled_jobs` that start with `ingest_`."""
    if not _TF.exists():  # pragma: no cover - repo layout guard
        pytest.skip(f"{_TF} not present")
    text = _TF.read_text(encoding="utf-8", errors="replace")
    return set(re.findall(r"^\s*(ingest_[a-z0-9_]+)\s*=\s*\{", text,
                          re.MULTILINE))


def _rule_for(source: str) -> str:
    """EventBridge rule names use `_` where a source key uses `-`."""
    return "ingest_" + source.replace("-", "_")


def test_every_source_has_an_eventbridge_rule():
    rules = _terraform_ingest_rules()
    missing = [s for s in SOURCES if _rule_for(s) not in rules]
    assert not missing, (
        f"sources declared in job_ingest_worker.SOURCES with no schedule in "
        f"{_TF.name}: {missing}. Such a source is never crawled, however "
        f"correct its provider is.")


def test_every_ingest_rule_maps_back_to_a_source():
    for rule in _terraform_ingest_rules() - _NON_SOURCE_RULES:
        assert _source_for_ingest_rule(rule) is not None, (
            f"{rule} fires on a schedule but scheduled_jobs.py cannot map it "
            f"to a source, so the tick dispatches nothing.")


def test_dispatch_mapping_is_derived_not_hand_listed():
    """A new source must be reachable without editing the dispatcher."""
    for source in SOURCES:
        assert _source_for_ingest_rule(_rule_for(source)) == source


def test_unknown_rules_are_rejected_rather_than_guessed():
    assert _source_for_ingest_rule("ingest_nonsense") is None
    assert _source_for_ingest_rule("ingest_") is None


def test_every_source_has_a_shard_count():
    missing = [s for s in SOURCES if not SHARD_COUNTS.get(s)]
    assert not missing, f"sources with no shard count: {missing}"


# ---------------------------------------------------------------------------
# Seeds
# ---------------------------------------------------------------------------

def test_seeds_for_a_catalog_backed_source_are_still_loaded():
    """Taleo rides on `career-pages`, which loads a catalog and stopped there.

    `_SEED_ENDPOINTS` is the only bootstrap a vendor with no Python catalog
    gets, so a seed that is defined and never loaded ships a vendor that can
    only ever return zero.
    """
    import services.job_ingest_worker as worker

    catalog = worker._load_career_members()
    seeds = worker._load_seed_members(SOURCE_CAREER)
    assert seeds, "no career-sourced seeds defined"

    catalog_vendors = {worker._vendor_of(m) for m in catalog}
    seed_vendors = {worker._vendor_of(m) for m in seeds}
    # The seeds exist precisely because these vendors are NOT in the catalog.
    assert seed_vendors - catalog_vendors, (
        "career seeds duplicate the catalog; they are meant to cover vendors "
        "the catalog does not")

    merged = {worker._vendor_of(m) for m in catalog + seeds}
    assert seed_vendors <= merged


@pytest.mark.parametrize("source", [SOURCE_WORKDAY, SOURCE_CAREER])
def test_seed_members_are_well_formed(source):
    import services.job_ingest_worker as worker

    for member in worker._load_seed_members(source):
        assert member["member_key"], member
        assert member["tenant"], member
        assert worker.source_of_vendor(member["vendor"]) == source
        # A seed whose vendor is not termless would never be swept.
        assert member["vendor"] in worker._TERMLESS_ATS, member
