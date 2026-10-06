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


def test_a_failed_attempt_is_recorded_so_it_cannot_jam_the_queue(monkeypatch):
    """Live 2026-10-06: an attempt that raised was logged and dropped, so the
    row stayed `running`, was re-claimed when its lease lapsed, and failed the
    same way - 502 rows, 50+ attempts each. Refill kept choosing those same
    companies, could not re-arm them, and the queue finished ONE company in 24
    hours while 69,785 were due a retry."""
    from services import career_portal_discovery as disc
    from services import company_registry_service as reg
    from services.ats_providers.base import ChallengeError

    recorded = {}
    monkeypatch.setattr(reg, "complete_discovery",
                        lambda cid, **kw: recorded.__setitem__(cid, kw))

    class _Engine:
        def __init__(self, *a, **k):
            pass

        def discover(self, name, **kwargs):
            if name == "walled":
                raise ChallengeError("discovery challenge (status=429)")
            if name == "broken":
                raise UnicodeError("label empty or too long")
            return disc.DiscoveryResult(name=name)

    monkeypatch.setattr(disc, "PortalDiscovery", _Engine)
    monkeypatch.setattr(disc, "_persist", lambda result: None)
    rows = [{"name": n, "company_id": n} for n in ("walled", "broken", "fine")]
    stats = disc.discover_batch(rows, _StubCtx(), persist=True, concurrency=1)

    assert set(recorded) == {"walled", "broken"}
    assert all(v["state"] == reg.DISCOVERY_DEFERRED for v in recorded.values())
    assert recorded["walled"]["detail"].startswith("challenge: ChallengeError")
    assert recorded["broken"]["detail"].startswith("error: UnicodeError")
    assert stats["errors"] == 2 and stats["blocked"] == 1


def test_a_run_of_challenges_stops_the_batch(monkeypatch):
    """One 429 is that site. Six in a row from different employers is the CDNs
    in front of them limiting OUR address; more requests only extend it."""
    from services import career_portal_discovery as disc
    from services import company_registry_service as reg
    from services.ats_providers.base import ChallengeError

    attempted = []
    monkeypatch.setattr(reg, "complete_discovery", lambda cid, **kw: None)
    monkeypatch.setattr(disc, "DISCOVERY_CHALLENGE_BREAKER", 3)

    class _Engine:
        def __init__(self, *a, **k):
            pass

        def discover(self, name, **kwargs):
            attempted.append(name)
            raise ChallengeError("discovery challenge (status=429)")

    monkeypatch.setattr(disc, "PortalDiscovery", _Engine)
    rows = [{"name": f"c{i}", "company_id": f"c{i}"} for i in range(10)]
    stats = disc.discover_batch(rows, _StubCtx(), persist=True, concurrency=1)

    assert attempted == ["c0", "c1", "c2"]
    assert stats["skipped_rate_limited"] == 7


def test_a_success_resets_the_challenge_streak(monkeypatch):
    from services import career_portal_discovery as disc
    from services import company_registry_service as reg
    from services.ats_providers.base import ChallengeError

    attempted = []
    monkeypatch.setattr(reg, "complete_discovery", lambda cid, **kw: None)
    monkeypatch.setattr(disc, "DISCOVERY_CHALLENGE_BREAKER", 2)
    monkeypatch.setattr(disc, "_persist", lambda result: None)

    class _Engine:
        def __init__(self, *a, **k):
            pass

        def discover(self, name, **kwargs):
            attempted.append(name)
            if name.startswith("w"):
                raise ChallengeError("discovery challenge (status=429)")
            return disc.DiscoveryResult(name=name)

    monkeypatch.setattr(disc, "PortalDiscovery", _Engine)
    rows = [{"name": n, "company_id": n} for n in ("w1", "ok1", "w2", "ok2", "w3", "ok3")]
    disc.discover_batch(rows, _StubCtx(), persist=True, concurrency=1)
    assert attempted == ["w1", "ok1", "w2", "ok2", "w3", "ok3"]


def test_a_name_too_long_for_dns_yields_no_invalid_guess():
    from services.career_portal_discovery import candidate_domains
    name = ("ZION EVANGELICAL LUTHERAN CHURCH OF THE UNALTERED AUGSBURG "
            "CONFESSION OF SOUTHINGTON, HARTFORD AV.")
    for host in candidate_domains(name):
        assert all(0 < len(label) <= 63 for label in host.split(".")), host
        host.encode("idna")     # raised UnicodeError before


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


@pytest.fixture
def _reg_db():
    from services import company_registry_service as reg
    d = reg._db()
    for c in (reg.COMPANIES, reg.QUEUE):
        d[c].delete_many({})
    return d


def _company(reg, cid, *, name, priority, status, last=None):
    doc = {"company_id": cid, "name": name, "priority": priority,
           "discovery_status": status}
    if last is not None:
        doc["last_discovered_at"] = last
    reg._db()[reg.COMPANIES].insert_one(doc)


def test_refill_makes_deferred_companies_claimable_again(_reg_db):
    """A deferred row is retryable; that is the whole point of the state.

    This used to be an `inspect.getsource(...)` grep for the string
    "DISCOVERY_DEFERRED", which the stalled implementation passed happily
    while producing no claimable work at all. Assert the OUTCOME instead: after
    a refill, can a worker actually claim the company?

    Live state that motivated this (aspirely_db, 2026-08-27): 7,380 queue rows,
    of which 4,437 deferred / 2,059 no_portal / 872 done / 12 running and
    **zero pending**. `refill_queue` reported 7,368 rows refilled; 16 workers
    on a 15-minute tick claimed nothing, every tick, for eight days.
    """
    from datetime import timedelta

    from services import company_registry_service as reg

    now = reg._now()
    stale = now - timedelta(days=reg.DISCOVERY_RETRY_AFTER_DAYS + 1)
    _company(reg, "c-def", name="Deferred Co", priority=30,
             status=reg.DISCOVERY_DEFERRED, last=stale)
    # Its queue row is already there, in the terminal state a previous run left.
    reg._db()[reg.QUEUE].insert_one({
        "company_id": "c-def", "state": reg.DISCOVERY_DEFERRED, "priority": 30,
        "attempts": 1, "created_at": stale, "lease_owner": None,
        "lease_expiry": None})

    assert reg.claim_discovery_batch("w0", size=5) == [], "precondition"

    made_claimable = reg.refill_queue(limit=100)
    assert made_claimable == 1, "refill must COUNT work it created, not rows it read"

    claimed = reg.claim_discovery_batch("w1", size=5)
    assert [c["company_id"] for c in claimed] == ["c-def"]


def test_refill_does_not_rearm_a_settled_company(_reg_db):
    """`done` and `no_portal` are conclusions, not cooldowns."""
    from services import company_registry_service as reg

    for cid, state in (("c-done", reg.DISCOVERY_DONE),
                       ("c-none", reg.DISCOVERY_NO_PORTAL)):
        _company(reg, cid, name=cid, priority=30, status=state,
                 last=reg._now())
        reg._db()[reg.QUEUE].insert_one({
            "company_id": cid, "state": state, "priority": 30, "attempts": 1,
            "created_at": reg._now(), "lease_owner": None,
            "lease_expiry": None})

    assert reg.refill_queue(limit=100) == 0
    assert reg.claim_discovery_batch("w1", size=5) == []


def test_refill_does_not_starve_the_low_priority_tail(_reg_db):
    """The selection window must MOVE between ticks.

    `sort("priority", -1)` with a hard limit picks the same rows forever, so a
    tail below the cut is never served rather than served late. Live proof:
    4,436 companies at priority 30 against a 1,600-row refill budget, with
    74,477 more at priority 1 that the query could not physically reach.
    """
    from datetime import timedelta

    from services import company_registry_service as reg

    now = reg._now()
    # Same priority; one was probed recently, one has never been probed.
    _company(reg, "c-probed", name="Probed", priority=30,
             status=reg.DISCOVERY_DEFERRED, last=now - timedelta(days=30))
    _company(reg, "c-fresh", name="Never probed", priority=30,
             status=reg.DISCOVERY_PENDING)

    picked = [d["company_id"] for d in reg._db()[reg.COMPANIES].find(
        {}, {"company_id": 1}).sort([("priority", -1),
                                     ("last_discovered_at", 1)])]
    assert picked[0] == "c-fresh", (
        "a company we have never probed must outrank one we already tried")


def test_a_deferred_company_waits_out_its_cooldown(_reg_db):
    """Retrying is right; retrying every fifteen minutes is not."""
    from services import company_registry_service as reg

    _company(reg, "c-hot", name="Just deferred", priority=30,
             status=reg.DISCOVERY_DEFERRED, last=reg._now())
    reg._db()[reg.QUEUE].insert_one({
        "company_id": "c-hot", "state": reg.DISCOVERY_DEFERRED, "priority": 30,
        "attempts": 1, "created_at": reg._now(), "lease_owner": None,
        "lease_expiry": None})

    assert reg.refill_queue(limit=100) == 0
    assert reg.claim_discovery_batch("w1", size=5) == []


# ---------------------------------------------------------------------------
# Tech classification -> discovery order
# ---------------------------------------------------------------------------

class TestTechClassification:
    """Which employers get probed first.

    `refill_queue` selects by `priority DESC`, so this classification IS the
    discovery order. Live shape of the registry it has to sort (2026-08-27):
    82,492 companies, of which 74,477 sat at priority 1 and are almost entirely
    single-member state-registry shells - a 4,000-row sample found 0.5% whose
    NAME even contains a technology word. The 7,987 SEC filers are where the
    software employers actually are.
    """

    def test_sic_decides_before_the_name(self):
        from services import company_registry_service as reg

        # The registrant's own filed classification.
        assert reg.classify_tech("NVIDIA CORP", "3674") == "software"
        assert reg.classify_tech("Alphabet Inc.", "7370") == "software"
        assert reg.classify_tech("Apple Inc.", "3571") == "software"
        assert reg.classify_tech("COGNEX CORP", "3823") == "tech_adjacent"
        assert reg.classify_tech("Tesla, Inc.", "3711") is None

    def test_an_unfiled_sic_falls_back_to_the_published_description(self):
        from services import company_registry_service as reg

        assert reg.classify_tech(
            "Some Co", "7389",
            "Services-Computer Programming, Data Processing") == "software"

    def test_name_evidence_is_priced_below_a_filed_signal(self):
        """A state-registry shell called "... Technologies LLC" must not
        outrank a real SEC filer. It should sort ahead of its own unscored
        neighbours and nothing more."""
        from services import company_registry_service as reg

        shell = reg.signals_for_company({"name": "SUMMIT TECHNOLOGIES LLC"})
        assert shell == {"software_name": True}
        filer = reg.signals_for_company(
            {"name": "NVIDIA CORP", "sic": "3674", "edgar": True})
        assert filer == {"software": True}

        assert reg.score_priority({"software_name": True}) \
            < reg.score_priority({"edgar": True}) \
            < reg.score_priority({"edgar": True, "software": True})
        # And a proven H-1B sponsor still beats a plausible software employer.
        assert reg.score_priority({"h1b": True}) > reg.score_priority({"software": True})

    def test_a_company_with_a_sic_never_gets_the_name_signal(self):
        """Belt and braces: once we know the filed classification, the name
        heuristic must not add a second, weaker vote for the same fact."""
        from services import company_registry_service as reg

        out = reg.signals_for_company(
            {"name": "ACME TECHNOLOGIES INC", "sic": "3711"})
        assert out == {}


# ---------------------------------------------------------------------------
# Domain confirmation
# ---------------------------------------------------------------------------

class TestDomainConfirmation:
    """A stored domain is a previous GUESS, not ground truth.

    `_resolve_domain` used to return a caller-supplied domain unverified
    ("caller-supplied: trust it, do not re-confirm"), which promoted one
    unconfirmed guess into permanent fact. Live examples from the registry:
    `AMAZON COM INC` was stored as `amazon-com.com` - a parked host answering
    202 with an empty page - and `Alphabet Inc.` as `alphabet.com`, which is
    BMW's fleet-leasing business.
    """

    def test_a_supplied_domain_is_tried_first_but_not_exclusively(self):
        """Returning ONLY the stored value meant a bad one suppressed every
        derivable guess as well as skipping confirmation - two failures
        compounding."""
        from services.career_portal_discovery import candidate_domains

        got = candidate_domains("Acme Widgets Inc", known="stored.example")
        assert got[0] == "stored.example", "the hint still goes first"
        assert any("acmewidgets" in c for c in got[1:]), \
            "a bad stored domain must not hide the derivable guesses"

    def test_every_name_token_must_appear_not_just_the_longest(self):
        from services.career_portal_discovery import _page_mentions

        page = "we build technology for everyone"
        # "technology" is the longest token of "MICRON TECHNOLOGY INC", and the
        # old rule confirmed this page on that alone.
        assert _page_mentions(page, "MICRON TECHNOLOGY INC") is False
        assert _page_mentions("micron technology memory",
                              "MICRON TECHNOLOGY INC") is True

    def test_the_ticker_confirms_on_its_own(self):
        """A site printing the registrant's ticker is that registrant's site."""
        from services.career_portal_discovery import _page_mentions

        assert _page_mentions("investor relations - NASDAQ: CGNX", "Some Holding Co",
                              ticker="CGNX") is True

    def test_a_single_token_name_is_confirmed_by_its_own_domain(self):
        """The ordinary "acme.com belongs to Acme Widgets" case must still
        pass, or narrowing the rule costs real employers."""
        from services.career_portal_discovery import _page_mentions

        assert _page_mentions("welcome to cognex", "COGNEX CORP",
                              host="cognex.com") is True


class TestNoPortalRequiresLookingAtTheCareersPage:
    """`no_portal` is terminal, so it must be a finding, not a shrug.

    Live discovery run, 2026-08-27, after the queue was unstalled: Salesforce,
    Apple, Microsoft, Broadcom, Shopify, Atlassian, Dell and Synopsys were each
    filed `no_portal` with the detail "read 4-6 page(s) on <domain>, no ATS
    link found". Every one of them has a careers page; the crawl had read the
    marketing site and never reached it, because those careers pages are React
    shells that fingerprinting cannot see into.

    Reading six pages of a marketing site is not evidence about an employer's
    hiring. It is inconclusive, and `deferred` is the state for that.
    """

    def _result(self, **over):
        from services.career_portal_discovery import DiscoveryResult
        res = DiscoveryResult(company_id="c1", name="Acme")
        res.domain = "acme.com"
        for k, v in over.items():
            setattr(res, k, v)
        return res

    def test_marketing_pages_only_is_not_a_no_portal_verdict(self, monkeypatch):
        from services import career_portal_discovery as disc
        from services import company_registry_service as reg

        seen = {}
        monkeypatch.setattr(reg, "complete_discovery",
                            lambda cid, **kw: seen.update(kw))
        monkeypatch.setattr(reg, "upsert_company", lambda *a, **kw: "c1")
        disc._persist(self._result(pages_fetched=6, career_pages_read=0))
        assert seen["state"] == reg.DISCOVERY_DEFERRED
        assert "careers page" in seen["detail"]

    def test_reading_the_careers_page_and_finding_nothing_IS_a_verdict(self, monkeypatch):
        from services import career_portal_discovery as disc
        from services import company_registry_service as reg

        seen = {}
        monkeypatch.setattr(reg, "complete_discovery",
                            lambda cid, **kw: seen.update(kw))
        monkeypatch.setattr(reg, "upsert_company", lambda *a, **kw: "c1")
        disc._persist(self._result(pages_fetched=3, career_pages_read=2))
        assert seen["state"] == reg.DISCOVERY_NO_PORTAL
        assert seen["detail"], "a terminal state must carry its evidence"

    def test_the_single_word_stem_form_is_always_offered(self):
        """`palantir.com` was generated LAST and truncated off the end, so
        discovery settled on `palantir-technologies.com`, found nothing, and
        wrote a terminal `no_portal`. Palantir's board is on Lever."""
        from services.career_portal_discovery import candidate_domains

        for name, wanted in [("PALANTIR TECHNOLOGIES INC", "palantir.com"),
                             ("SAP SE", "sap.com")]:
            got = candidate_domains(name)
            assert wanted in got, f"{name}: {got}"
            assert got.index(wanted) <= 2, f"{name} must try {wanted} early: {got}"
