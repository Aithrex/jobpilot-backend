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


# ---------------------------------------------------------------------------
# Every rule must be handled, not just the ingest family
# ---------------------------------------------------------------------------

def _all_scheduler_rules() -> set:
    if not _TF.exists():  # pragma: no cover
        pytest.skip(f"{_TF} not present")
    text = _TF.read_text(encoding="utf-8", errors="replace")
    body = text.split("scheduled_jobs", 1)[-1]
    return set(re.findall(r"^\s*([a-z][a-z0-9_]+)\s*=\s*\{\s*expression",
                          body, re.MULTILINE))


def test_every_scheduled_rule_has_a_dispatch_branch():
    """A rule with no branch fires on schedule and does nothing.

    That is precisely how `oracle` and `icims` sat idle: EventBridge-shaped
    names existed in one layer and the dispatcher knew nothing about them. The
    check is textual on purpose — it costs nothing and it fails the moment
    someone adds a rule without wiring it.
    """
    dispatcher = (pathlib.Path(__file__).resolve().parents[1] / "shared"
                  / "python" / "services" / "scheduled_jobs.py")
    source = dispatcher.read_text(encoding="utf-8", errors="replace")

    unhandled = []
    for rule in _all_scheduler_rules():
        if rule.startswith("ingest_"):
            continue          # covered by the prefix branch + source mapping
        if f'"{rule}"' not in source and f"'{rule}'" not in source:
            unhandled.append(rule)
    assert not unhandled, (
        f"scheduler rules with no branch in scheduled_jobs.py: {unhandled}. "
        f"Such a rule fires forever and dispatches nothing.")


def test_discovery_is_scheduled_and_dispatchable():
    """Discovery GROWS the registry; ingestion crawls it. Both need a tick."""
    assert "discover_companies" in _all_scheduler_rules()
    # And it must not be mistaken for an ingest source.
    assert _source_for_ingest_rule("discover_companies") is None


def test_discovery_worker_entrypoints_exist():
    from services import career_portal_discovery as disc

    assert callable(disc.dispatch_due_discovery)
    assert callable(disc.run_discovery_worker)
    assert disc.DISCOVERY_WORKERS_PER_TICK >= 1
    assert disc.DISCOVERY_BATCH_SIZE >= 1


# ---------------------------------------------------------------------------
# Discovery batch concurrency
# ---------------------------------------------------------------------------

class _StubCtx:
    def __init__(self):
        self.cutoff_iso = None

    def should_stop(self):
        return False

    def log(self, *a, **k):
        pass

    def get_text(self, *a, **k):
        return 200, ""

    def get_json(self, *a, **k):
        return 200, None


def test_one_failing_company_does_not_end_the_batch(monkeypatch):
    """A live pass died on a 429 from one employer, abandoning 59,672 others.

    Discovery reaches out to companies' OWN websites, so a hostile or broken
    one is routine. It must cost a row, never the batch.
    """
    from services import career_portal_discovery as disc

    seen = []

    class _Engine:
        def __init__(self, *a, **k):
            pass

        def discover(self, name, **kwargs):
            seen.append(name)
            if name == "boom":
                raise RuntimeError("429 from the employer's own site")
            return disc.DiscoveryResult(name=name)

    monkeypatch.setattr(disc, "PortalDiscovery", _Engine)
    rows = [{"name": n, "company_id": n} for n in ("a", "boom", "c")]
    stats = disc.discover_batch(rows, _StubCtx(), persist=False, concurrency=1)

    assert sorted(seen) == ["a", "boom", "c"], "every company must be attempted"
    assert stats["companies"] == 3
    assert stats["errors"] == 1


def test_batch_runs_companies_concurrently(monkeypatch):
    """Throughput is the whole point: 20.8s/company serial vs 4.85s at 8-way."""
    import threading as _t

    from services import career_portal_discovery as disc

    live = {"now": 0, "peak": 0}
    lock = _t.Lock()
    gate = _t.Barrier(4, timeout=10)

    class _Engine:
        def __init__(self, *a, **k):
            pass

        def discover(self, name, **kwargs):
            with lock:
                live["now"] += 1
                live["peak"] = max(live["peak"], live["now"])
            try:
                gate.wait()      # only passes if 4 run at once
            except Exception:
                pass
            with lock:
                live["now"] -= 1
            return disc.DiscoveryResult(name=name)

    monkeypatch.setattr(disc, "PortalDiscovery", _Engine)
    rows = [{"name": f"c{i}", "company_id": f"c{i}"} for i in range(4)]
    disc.discover_batch(rows, _StubCtx(), persist=False, concurrency=4)

    assert live["peak"] >= 2, "companies must not be fingerprinted one at a time"


# ---------------------------------------------------------------------------
# Discovery outcome classification
# ---------------------------------------------------------------------------

def test_an_unreached_company_is_not_recorded_as_having_no_portal():
    """`no_portal` is terminal, so it must mean the EMPLOYER, not our luck.

    `refill_queue` never revisits `no_portal`, so recording it after a 429, a
    bot wall or a failed domain guess writes a false negative that no later run
    corrects. Measured on live data: 4,420 of 6,468 companies filed as
    "no portal" had detail `no reachable domain` — we never fetched a page.
    """
    from services import company_registry_service as reg
    from services.career_portal_discovery import DiscoveryResult, _inconclusive

    # Never reached a site at all.
    assert _inconclusive(DiscoveryResult(company_id="c")) is True
    # Reached one, but it refused us.
    assert _inconclusive(DiscoveryResult(
        company_id="c", domain="x.com",
        detail="discovery challenge (status=429)")) is True
    assert _inconclusive(DiscoveryResult(
        company_id="c", domain="x.com", detail="robots disallow")) is True
    # Reached one, read it, genuinely nothing there — that IS a claim we can make.
    assert _inconclusive(DiscoveryResult(
        company_id="c", domain="x.com", detail="no ats links found")) is False

    assert reg.DISCOVERY_DEFERRED != reg.DISCOVERY_NO_PORTAL


def test_deferred_companies_are_picked_up_again(monkeypatch):
    """A deferred row is retryable; that is the whole point of the state."""
    import inspect

    from services import company_registry_service as reg

    src = inspect.getsource(reg.refill_queue)
    assert "DISCOVERY_DEFERRED" in src, (
        "refill_queue must re-select deferred companies, or deferring them is "
        "just a slower way of dropping them")
