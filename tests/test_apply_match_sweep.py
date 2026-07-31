"""Offline tests for the new-posting -> auto-apply fan-out (spec §5).

Runs fully offline (mongomock via conftest/_vg_support; no Bedrock, no S3, no
network). `apply_match_sweep` is the join between the shared `job_postings`
inventory and per-user auto-apply that did not exist: `job_ingest_worker` wrote
postings and only bumped a counter, and `apply_scheduler` only ever looked at
jobs the user had already SAVED by hand.

What is pinned here, in priority order:

  * THE FIT GATE. 65 = `fit_score`'s "good match" band, and it is a JOB-MATCH
    threshold — completely separate from `autonomy_threshold`, which is
    form-fillability. A row below the bar is never enqueued.
  * NO POSTING WITHOUT EVIDENCE. `fit_score` caps a posting whose requirements
    cannot be read at 45 ("based on the job title alone"). The sweep drops
    those rather than scoring them, because an unattended application must
    never be placed on a posting we could not read. This test is what stops a
    future "just score the title" shortcut.
  * THE PER-DAY CAP. Before `auto_apply_daily` existed there was only a MONTHLY
    pool (premium_plus: unlimited), so 96 ticks/day x 50 matches/tick allowed
    ~4,800 unattended applications in a day. The cap is atomic, per-enqueue,
    and survives across ticks.
  * The watermark is a lossless, monotonic cursor.
  * Every existing safety rail still fires: master kill-switch, explicit
    autonomy mode, dedup gate + unique index, closed-posting skip.
  * The sweep NEVER submits — it only ever enqueues `auto_apply_gather`.
"""
from datetime import datetime, timedelta, timezone

import pytest

from _vg_support import current_db

from services import apply_match_sweep as sweep
from services import autonomy_service as autonomy
from services import entitlements_service as ent
from services import fit_score
from services import job_content_store as store
from services import job_inventory_service as inv

USER = "alice@aspirely.me"
NOW = datetime.now(timezone.utc)


def _load_migrate_indexes():
    """Load `scripts/migrate_indexes.py` by FILE PATH.

    `scripts/` is not a package and is not guaranteed to be on `sys.path`:
    importing it as `scripts.migrate_indexes` resolves when pytest is run from
    the repo root and raises ModuleNotFoundError in CI. Resolving relative to
    this file makes the test independent of the invocation.
    """
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / "scripts" / "migrate_indexes.py"
    assert path.is_file(), f"migrate_indexes.py not found at {path}"
    spec = importlib.util.spec_from_file_location("_migrate_indexes_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

SWE_JD = (
    "Senior Backend Engineer\n\n"
    "Requirements:\n"
    "- 5+ years of software engineering experience\n"
    "- Strong Python and SQL\n"
    "- Experience with AWS and Docker\n"
    "- Familiarity with Kubernetes\n\n"
    "Preferred qualifications:\n- Kafka\n- Terraform\n"
)

NURSE_JD = (
    "Registered Nurse, ICU. Requirements: a current RN license, BLS "
    "certification, patient care, medication administration and wound care "
    "experience, documented in the Epic EHR."
)

RESUME = {
    "contact": {"name": "Jordan Alvarez", "email": USER, "location": "Austin, TX"},
    "summary": "Backend engineer.",
    "skills": {"languages": ["Python", "SQL"], "cloud": ["AWS", "Docker"]},
    "experience": [{
        "title": "Backend Engineer", "company": "Acme",
        "dates": "Jan 2019 - Jan 2025",
        "bullets": ["Built Python services on AWS with Docker and PostgreSQL."],
    }],
    "education": [{"degree": "BS Computer Science", "institution": "UT"}],
}

COLLECTIONS = ("job_postings", "job_source_state", "job_ingestion_runs",
               "applications", "auto_submit_audit", "auto_submit_settings",
               "resume_assets", "users", "feature_usage", "quota_counters",
               "quota_ledger", "entitlement_overrides", "notification_prefs",
               "notification_log")


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------
class _Enqueued(list):
    """Records every async job the sweep dispatches, so a test can assert that
    NOTHING but `auto_apply_gather` is ever enqueued (the sweep must not be able
    to reach a submit path)."""

    def types(self):
        return [t for t, _ in self]


@pytest.fixture()
def sw(monkeypatch):
    """Clean collections, the real unique index, an opted-in user with a résumé,
    and a stubbed async dispatcher."""
    monkeypatch.delenv("RESUME_S3_BUCKET", raising=False)
    store.reset_caches()
    d = current_db()
    for c in COLLECTIONS:
        d[c].delete_many({})

    # Mirror scripts/migrate_indexes.py: the (user_email, job_identity) unique
    # index is the dedup backstop create_application relies on.
    d.applications.create_index([("user_email", 1), ("job_identity", 1)],
                                unique=True, name="user_email_job_identity_unique")

    d.users.insert_one({"email": USER, "plan": "free", "active_resume_id": "asset-a"})
    d.resume_assets.insert_one({
        "resume_id": "asset-a", "owner_type": "user", "user_email": USER,
        "structured": RESUME, "parse_status": "parsed",
        "parsed_at": "2026-01-01T00:00:00+00:00",
    })
    d.auto_submit_settings.insert_one({
        "user_email": USER, "auto_submit_enabled": True,
        "mode": autonomy.MODE_AGGRESSIVE,
    })

    # Master kill-switch ON for the duration of the test.
    from services import auto_submit_service as ass
    monkeypatch.setattr(ass, "auto_submit_enabled", lambda: True)

    # Capture async dispatch instead of self-invoking a Lambda.
    enq = _Enqueued()

    class _Svc:
        def create_job(self, job_type, payload, user_email=""):
            return f"job-{len(enq)}"

    import services.resume_service as rs
    monkeypatch.setattr(rs, "get_resume_service", lambda: _Svc())
    monkeypatch.setattr(rs.ResumeService, "invoke_async",
                        staticmethod(lambda job_id, job_type, payload: enq.append(
                            (job_type, payload))))

    # Notifications are exercised separately; keep them inert by default.
    from services.notifications import service as notify
    monkeypatch.setattr(notify, "send",
                        lambda email, event, payload=None: {"ok": False,
                                                            "status": "blocked"})
    d._enqueued = enq
    yield d
    for c in COLLECTIONS:
        d[c].delete_many({})


def _seed_posting(*, sjid, title="Senior Backend Engineer", description=SWE_JD,
                  company="Acme Corp", first_seen=None, parse=False,
                  ats="greenhouse", source="career-pages"):
    """One inventory posting. `first_seen` drives `first_seen_at` ($setOnInsert),
    which is the cursor the sweep reads."""
    when = first_seen or NOW
    inv.upsert_posting({
        "source": source, "source_job_id": sjid,
        "url": f"https://boards.greenhouse.io/acme/jobs/{sjid}",
        "company": company, "title": title,
        "location": ["Austin, TX, USA"], "description": description,
        "employment_type": "Full time", "cost_class": "free",
        "source_quality": "direct", "industry": "tech",
        "ats": ats, "tenant": "acme",
        "posted_at": when - timedelta(days=1),
    }, now=when)
    if parse:
        doc = inv.get_posting(source, sjid)
        inv.mark_requirements_parsed(
            source, sjid, doc["content_hash"],
            fit_score.extract_requirements(description, title), now=when)
    return inv.get_posting(source, sjid)


def _sjids(docs):
    """Candidate docs carry the canonical `job_id`, not the source's own id
    (the projection is deliberately minimal), so map back for readability."""
    out = []
    for d in docs:
        row = current_db().job_postings.find_one(
            {"job_id": d["job_id"]}, {"_id": 0, "source_job_id": 1})
        out.append((row or {}).get("source_job_id"))
    return out


def _rewind_watermark(db, hours=1, user=USER):
    db.auto_submit_settings.update_one(
        {"user_email": user},
        {"$set": {sweep.WATERMARK_FIELD: NOW - timedelta(hours=hours)}})


def _seed_second_user(db, email):
    """A SECOND opted-in user with the same résumé — the shared-inventory case.
    `job_postings` is one corpus, so both users match the same posting and derive
    the same `normalized_job_identity`."""
    db.users.insert_one({"email": email, "plan": "free",
                         "active_resume_id": f"asset-{email}"})
    db.resume_assets.insert_one({
        "resume_id": f"asset-{email}", "owner_type": "user", "user_email": email,
        "structured": RESUME, "parse_status": "parsed",
        "parsed_at": "2026-01-01T00:00:00+00:00",
    })
    db.auto_submit_settings.insert_one({
        "user_email": email, "auto_submit_enabled": True,
        "mode": autonomy.MODE_AGGRESSIVE,
    })


def _daily_count(db, email):
    """The user's OWN charged daily-cap counter (per-user counter _id)."""
    from services import quota_ledger_service as ql
    doc = db.quota_counters.find_one({"_id": ql.counter_id_for(
        email, ent._period_key(sweep.DAILY_CAP_FEATURE), sweep.DAILY_CAP_FEATURE)})
    return int((doc or {}).get("count", 0))


# ---------------------------------------------------------------------------
# The premise: without readable requirements NOTHING can reach the 65 bar.
# ---------------------------------------------------------------------------
class TestScoringPremise:
    def test_a_body_less_posting_can_never_reach_the_match_threshold(self):
        """The reason `_hydrate` fetches JD bodies at all.

        `fit_score` caps a posting with no readable requirements at 45
        ("insufficient_job_requirements"), which is BELOW the 65 "good match"
        bar. So a sweep that scored inventory rows as-is — inventory carries no
        JD text, bodies live in S3 — would match exactly zero jobs forever, and
        look like a working feature.
        """
        profile = fit_score.build_resume_profile(RESUME)
        blind = fit_score.score_one(jd_text="", title="Senior Backend Engineer",
                                    profile=profile, job={"requirements": None})
        assert blind["match_score"] <= 45
        assert blind["match_score"] < autonomy.DEFAULT_MATCH_THRESHOLD

        reqs = fit_score.extract_requirements(SWE_JD, "Senior Backend Engineer")
        seeing = fit_score.score_one(jd_text="", title="Senior Backend Engineer",
                                     profile=profile, job={"requirements": reqs})
        assert seeing["match_score"] >= autonomy.DEFAULT_MATCH_THRESHOLD


# ---------------------------------------------------------------------------
# Watermark — the resumable cursor
# ---------------------------------------------------------------------------
class TestWatermark:
    def test_cold_start_looks_back_a_bounded_window_not_the_epoch(self, sw):
        wm = sweep.get_watermark(USER)
        expected = datetime.now(timezone.utc) - timedelta(
            hours=sweep.COLD_START_LOOKBACK_HOURS)
        assert abs((wm - expected).total_seconds()) < 120

    def test_only_postings_first_seen_after_the_watermark_are_candidates(self, sw):
        _seed_posting(sjid="old", first_seen=NOW - timedelta(hours=6))
        _seed_posting(sjid="new", first_seen=NOW - timedelta(minutes=5))
        since = NOW - timedelta(hours=1)
        assert _sjids(sweep._new_postings([], since)) == ["new"]

    def test_candidates_come_back_oldest_first(self, sw):
        _seed_posting(sjid="b", first_seen=NOW - timedelta(minutes=10))
        _seed_posting(sjid="a", first_seen=NOW - timedelta(minutes=30))
        since = NOW - timedelta(hours=1)
        assert _sjids(sweep._new_postings([], since)) == ["a", "b"], \
            "ascending first_seen_at is what makes the cursor lossless"

    def test_watermark_never_moves_backward(self, sw):
        ahead = NOW
        assert sweep.advance_watermark(USER, ahead) is True
        assert sweep.advance_watermark(USER, NOW - timedelta(hours=3)) is False
        stored = sw.auto_submit_settings.find_one({"user_email": USER})
        got = stored[sweep.WATERMARK_FIELD]
        got = got if got.tzinfo else got.replace(tzinfo=timezone.utc)
        # Mongo stores millisecond precision, so compare with that tolerance.
        assert abs((got - ahead).total_seconds()) < 0.001

    def test_watermark_never_creates_a_settings_doc(self, sw):
        assert sweep.advance_watermark("nobody@example.com", NOW) is False
        assert sw.auto_submit_settings.count_documents(
            {"user_email": "nobody@example.com"}) == 0

    def test_a_sweep_advances_the_cursor_past_what_it_examined(self, sw):
        _seed_posting(sjid="a", first_seen=NOW - timedelta(minutes=20))
        _rewind_watermark(sw)
        sweep.run_user_sweep(USER)
        after = sweep.get_watermark(USER)
        assert after >= NOW - timedelta(minutes=21)

    def test_a_window_of_unreadable_postings_still_advances_the_cursor(self, sw):
        """THE LIVELOCK. The cursor used to be derived from surviving rows only,
        so a window in which EVERY candidate was dropped for no readable evidence
        left it parked: the same candidates were re-read (and their bodies
        re-fetched — up to HYDRATE_LIMIT S3 GETs) every 15 minutes forever,
        postings behind the stuck window were never reached, and auto-apply
        stopped for that user with no error surfaced anywhere.
        """
        for i in range(3):
            _seed_posting(sjid=f"blank-{i}", description="", company=f"Acme {i}",
                          first_seen=NOW - timedelta(minutes=30 - i))
        _rewind_watermark(sw, hours=2)

        first = sweep.run_user_sweep(USER)
        assert (first["candidates"], first["no_evidence"], first["scored"]) == (3, 3, 0)

        # The cursor moved past the whole unreadable window...
        assert sweep.get_watermark(USER) >= NOW - timedelta(minutes=28, seconds=1)
        # ...so the next tick does not re-read (and re-fetch) the same rows.
        assert sweep.run_user_sweep(USER)["candidates"] == 0

    def test_the_cursor_stops_short_of_what_the_io_budget_skipped(self, sw):
        """The other half of the same invariant: advancing over EXAMINED rows must
        not advance over rows `_hydrate` never looked at, or the lossless-cursor
        guarantee is gone and postings are silently dropped."""
        _seed_posting(sjid="a", description="", first_seen=NOW - timedelta(minutes=30))
        _seed_posting(sjid="b", company="Acme B",
                      first_seen=NOW - timedelta(minutes=10))
        docs = sweep._new_postings([], NOW - timedelta(hours=2))
        assert len(docs) == 2

        _rows, _ctx, dropped, examined_max = sweep._hydrate(docs, limit=1)

        assert dropped == 1
        # Exactly the first doc's marker — the budget stopped us before the second.
        assert examined_max == sweep._as_aware(docs[0]["first_seen_at"])
        assert examined_max < sweep._as_aware(docs[1]["first_seen_at"])


# ---------------------------------------------------------------------------
# The fit gate
# ---------------------------------------------------------------------------
class TestFitGate:
    def test_a_good_match_is_enqueued_end_to_end(self, sw):
        _seed_posting(sjid="swe-1", first_seen=NOW - timedelta(minutes=5))
        _rewind_watermark(sw)

        out = sweep.run_user_sweep(USER)

        assert out["enqueued"] == 1, out
        row = sw.applications.find_one({"user_email": USER})
        assert row is not None
        assert row["status"] == "queued"
        assert row["company"] == "Acme Corp"
        assert row["source"] == sweep.LEDGER_SOURCE
        assert row["posting_job_id"], "the inventory row must be pinned"
        assert sw._enqueued.types() == ["auto_apply_gather"]

    def test_a_poor_match_is_never_enqueued(self, sw):
        _seed_posting(sjid="rn-1", title="Registered Nurse",
                      description=NURSE_JD, company="Mercy Hospital",
                      first_seen=NOW - timedelta(minutes=5))
        _rewind_watermark(sw)

        out = sweep.run_user_sweep(USER)

        assert out["enqueued"] == 0
        assert sw.applications.count_documents({}) == 0
        assert sw._enqueued.types() == []

    def test_the_gate_reads_the_per_user_match_threshold(self, sw):
        _seed_posting(sjid="swe-1", first_seen=NOW - timedelta(minutes=5))
        _rewind_watermark(sw)
        # A 99 threshold is above what this pairing scores (80) -> no enqueue.
        autonomy.set_match_threshold(USER, 99)

        out = sweep.run_user_sweep(USER)

        assert out["threshold"] == 99
        assert out["enqueued"] == 0

    def test_match_threshold_is_not_autonomy_threshold(self, sw):
        """The two thresholds must never be read from the same field.

        `autonomy_threshold` is 0.0-1.0 form-fillability; `match_threshold` is
        0-100 job match. Conflating them breaks `decide()` — a 0.75 read as a
        match score would apply to literally everything.
        """
        autonomy.set_autonomy_threshold(USER, 0.80)
        autonomy.set_match_threshold(USER, 70)
        assert autonomy.get_autonomy_threshold(USER) == 0.80
        assert autonomy.get_match_threshold(USER) == 70
        doc = sw.auto_submit_settings.find_one({"user_email": USER})
        assert doc["autonomy_threshold"] == 0.80
        assert doc["match_threshold"] == 70

    def test_a_posting_with_no_readable_requirements_is_dropped_not_scored(self, sw):
        """The single most important safety property in this module.

        A posting with no readable body scores 45 (title-only cap). It must be
        DROPPED, not scored — otherwise a future threshold change below 45 would
        silently start auto-applying to postings nobody ever read.
        """
        _seed_posting(sjid="blank", description="", first_seen=NOW - timedelta(minutes=5))
        _rewind_watermark(sw)

        out = sweep.run_user_sweep(USER)

        assert out["candidates"] == 1
        assert out["scored"] == 0
        assert out["no_evidence"] == 1
        assert out["enqueued"] == 0

    def test_persisted_requirements_are_reused_without_an_s3_fetch(self, sw, monkeypatch):
        _seed_posting(sjid="swe-1", first_seen=NOW - timedelta(minutes=5), parse=True)
        _rewind_watermark(sw)
        calls = []
        monkeypatch.setattr(store, "get_jd",
                            lambda h: calls.append(h) or "should not be needed")

        out = sweep.run_user_sweep(USER)

        assert out["enqueued"] == 1
        assert calls == [], "a parsed posting must not re-fetch its body"

    def test_body_is_hydrated_from_the_content_store_when_offloaded(self, sw, monkeypatch):
        doc = _seed_posting(sjid="swe-1", first_seen=NOW - timedelta(minutes=5))
        # Simulate the production shape: body offloaded to S3, pointer only.
        sw.job_postings.update_one(
            {"source_job_id": "swe-1"},
            {"$set": {"description_text": "", "description_ref": "s3://jd/x"}})
        monkeypatch.setattr(store, "get_jd", lambda h: SWE_JD)
        _rewind_watermark(sw)

        out = sweep.run_user_sweep(USER)

        assert out["enqueued"] == 1, "the S3-offloaded body must be readable"
        assert doc["content_hash"]


# ---------------------------------------------------------------------------
# The per-day cap (spec §5 hard requirement)
# ---------------------------------------------------------------------------
class TestDailyCap:
    def test_feature_is_registered_as_a_daily_period(self):
        assert ent.FEATURE_PERIODS[sweep.DAILY_CAP_FEATURE] == ent.PERIOD_DAY

    def test_the_quota_ledger_idempotency_index_is_not_a_global_namespace(self):
        """The index has to agree with the lookup filter. A GLOBAL unique on
        `idempotency_key` alone makes the key namespace cross-tenant AND
        perpetual: it is what turned one user's reservation into every user's, and
        without a TTL the keys outlive the periods they belong to."""
        # Load by PATH, not by module name. `scripts/` is not a package and is
        # not on sys.path under every pytest invocation — importing it by name
        # passes locally (repo root happens to be on the path) and raises
        # ModuleNotFoundError in CI.
        mig = _load_migrate_indexes()

        specs = [(keys, opts) for coll, keys, opts in mig.INDEX_SPECS
                 if coll == "quota_ledger"]
        idem = [(keys, opts) for keys, opts in specs
                if any(k == "idempotency_key" for k, _ in keys)]
        assert len(idem) == 1
        keys, opts = idem[0]
        assert [k for k, _ in keys] == ["user_email", "feature", "date",
                                        "idempotency_key"]
        assert opts["unique"] is True
        # A compound SPARSE index would still collide on the entries that carry
        # no key at all (they all carry user_email) — it must be partial.
        assert opts["partialFilterExpression"] == {
            "idempotency_key": {"$exists": True}}
        assert opts.get("sparse") is None
        # The superseded global unique must be dropped, or a second user's insert
        # still collides with the first user's key.
        assert ("quota_ledger", "idempotency_key_unique") in \
            mig.POST_CREATE_DROP_INDEXES
        # And the entries expire with (well after) their own quota period.
        ttl = [opts for keys, opts in specs if "expireAfterSeconds" in opts]
        assert len(ttl) == 1 and ttl[0]["expireAfterSeconds"] >= 31 * 24 * 3600

    def test_every_plan_including_unlimited_has_a_finite_daily_cap(self):
        """premium_plus is unlimited on the MONTHLY auto_submit pool. If it were
        also unlimited per day, a 15-minute unattended sweep would be unbounded
        — the ~4,800/day failure mode."""
        for plan in (ent.FREE, ent.PREMIUM, ent.PREMIUM_PLUS):
            limit = ent.PLAN_LIMITS[plan][sweep.DAILY_CAP_FEATURE]
            assert limit is not None, f"{plan} must not be unlimited per day"
            assert limit == ent.DEFAULT_DAILY_AUTO_APPLY_CAP == 20

    def test_the_cap_stops_a_single_tick(self, sw):
        sw.entitlement_overrides.insert_one(
            {"user_email": USER, "overrides": {"limits": {sweep.DAILY_CAP_FEATURE: 1}}})
        for i in range(3):
            _seed_posting(sjid=f"swe-{i}", company=f"Acme {i}",
                          first_seen=NOW - timedelta(minutes=5 + i))
        _rewind_watermark(sw)

        out = sweep.run_user_sweep(USER)

        assert out["enqueued"] == 1
        assert out["daily_capped"] == 1
        assert sw.applications.count_documents({}) == 1

    def test_the_cap_survives_across_ticks(self, sw):
        sw.entitlement_overrides.insert_one(
            {"user_email": USER, "overrides": {"limits": {sweep.DAILY_CAP_FEATURE: 1}}})
        _seed_posting(sjid="swe-a", company="Acme A", first_seen=NOW - timedelta(minutes=9))
        _rewind_watermark(sw)
        first = sweep.run_user_sweep(USER)
        assert first["enqueued"] == 1

        _seed_posting(sjid="swe-b", company="Acme B", first_seen=NOW - timedelta(minutes=1))
        second = sweep.run_user_sweep(USER)

        assert second["enqueued"] == 0
        assert second["daily_capped"] == 1
        assert sw.applications.count_documents({}) == 1

    def test_a_capped_tick_does_not_burn_the_monthly_pool(self, sw):
        """The daily throttle and the monthly `auto_submit` pool are separate
        meters — the sweep must not double-count against `auto_submit`."""
        sw.entitlement_overrides.insert_one(
            {"user_email": USER, "overrides": {"limits": {sweep.DAILY_CAP_FEATURE: 1}}})
        _seed_posting(sjid="swe-a", first_seen=NOW - timedelta(minutes=5))
        _rewind_watermark(sw)
        sweep.run_user_sweep(USER)
        assert ent.get_usage(USER, "auto_submit")["used"] == 0

    def test_an_exhausted_monthly_pool_stops_the_user_before_any_work(self, sw):
        """Queueing rows that could never submit wastes the daily cap and fills
        the user's board with dead applications."""
        ent.increment_usage(USER, "auto_submit",
                            ent.PLAN_LIMITS[ent.FREE]["auto_submit"])
        _seed_posting(sjid="swe-1", first_seen=NOW - timedelta(minutes=5))
        _rewind_watermark(sw)

        assert sweep.run_user_sweep(USER) == {"skipped": "monthly_pool_exhausted"}
        assert sw.applications.count_documents({}) == 0

    def test_an_untouched_monthly_pool_fails_open(self, sw, monkeypatch):
        """The pre-check reads `feature_usage`, but with AUTO_APPLY_RESERVE on
        the real counter lives in `quota_counters`. A pre-check that guesses
        wrong must not silently disable the whole sweep — the authoritative
        denial happens later, at approve."""
        monkeypatch.setattr(ent, "check_quota",
                            lambda email, feature, n=1: (False, {"used": 0, "limit": 25}))
        _seed_posting(sjid="swe-1", first_seen=NOW - timedelta(minutes=5))
        _rewind_watermark(sw)

        assert sweep.run_user_sweep(USER)["enqueued"] == 1

    def test_the_daily_cap_key_carries_the_owner(self, sw, monkeypatch):
        """`normalized_job_identity` is computed from the SHARED `job_postings`
        row, so it is identical for every user who matches that posting. The
        reservation key must therefore carry the user_email — reserving under a
        bare posting hash puts the per-user daily cap in a cross-tenant key
        namespace (the quota ledger scopes its lookups too; this is the caller
        half of the same fix, asserted where the bug was)."""
        from services import quota_ledger_service as ql
        seen = []
        real = ql.reserve

        def _spy(email, feature, n=1, idempotency_key=None):
            seen.append(idempotency_key)
            return real(email, feature, n=n, idempotency_key=idempotency_key)

        monkeypatch.setattr(ql, "reserve", _spy)
        _seed_posting(sjid="swe-1", first_seen=NOW - timedelta(minutes=5))
        _rewind_watermark(sw)

        assert sweep.run_user_sweep(USER)["enqueued"] == 1

        assert len(seen) == 1
        assert USER in seen[0], seen[0]
        assert seen[0].startswith(sweep.DAILY_CAP_FEATURE + ":")

    def test_two_users_matching_one_posting_each_charge_their_own_slot(self, sw):
        """The cap is PER USER, and `job_postings` is SHARED inventory.

        `normalized_job_identity` is derived from (company, title, jd) — the same
        posting yields the SAME hash for every user. When the reservation's
        idempotency key was that hash alone, the second user's reserve matched the
        FIRST user's ledger entry, returned `reserved: True`, and left the second
        user's counter at 0: the 20/day cap silently stopped binding for everyone
        but whoever swept first. Both counters must move.
        """
        other = "bob@aspirely.me"
        _seed_second_user(sw, other)
        _seed_posting(sjid="swe-shared", first_seen=NOW - timedelta(minutes=5))
        _rewind_watermark(sw)
        _rewind_watermark(sw, hours=1, user=other)

        assert sweep.run_user_sweep(USER)["enqueued"] == 1
        assert sweep.run_user_sweep(other)["enqueued"] == 1

        # One ledger row and one CHARGED daily slot per user — never shared.
        assert _daily_count(sw, USER) == 1
        assert _daily_count(sw, other) == 1
        assert sw.applications.count_documents({"user_email": USER}) == 1
        assert sw.applications.count_documents({"user_email": other}) == 1
        assert sw.quota_ledger.count_documents(
            {"feature": sweep.DAILY_CAP_FEATURE, "user_email": other}) == 1

    def test_the_cap_still_binds_for_the_second_user_of_a_shared_posting(self, sw):
        """The consequence of the cross-tenant key, stated as a cap: with a cap of
        1 already spent, the second user must be DENIED — not handed the first
        user's reservation."""
        other = "bob@aspirely.me"
        _seed_second_user(sw, other)
        for u in (USER, other):
            sw.entitlement_overrides.insert_one(
                {"user_email": u,
                 "overrides": {"limits": {sweep.DAILY_CAP_FEATURE: 1}}})
        _seed_posting(sjid="swe-1", company="Acme One",
                      first_seen=NOW - timedelta(minutes=9))
        _seed_posting(sjid="swe-2", company="Acme Two",
                      first_seen=NOW - timedelta(minutes=8))
        _rewind_watermark(sw)
        _rewind_watermark(sw, hours=1, user=other)

        assert sweep.run_user_sweep(USER)["daily_capped"] == 1      # 1 of 2 enqueued
        second = sweep.run_user_sweep(other)

        assert second["enqueued"] == 1
        assert second["daily_capped"] == 1
        assert _daily_count(sw, other) == 1

    def test_a_redelivered_tick_never_refunds_the_live_reservation(self, sw,
                                                                   monkeypatch):
        """Same user, overlapping/redelivered ticks on ONE posting.

        Tick B's reserve is an idempotent REPLAY (same key), so it charged
        nothing. Its `create_application` then loses to the unique index
        (`skipped_duplicate`) — and the old refund handed back tick A's slot, so a
        REAL queued application existed with nothing charged for it. A replayed
        reservation must never be refunded.
        """
        _seed_posting(sjid="swe-1", first_seen=NOW - timedelta(minutes=5))
        _rewind_watermark(sw)
        assert sweep.run_user_sweep(USER)["enqueued"] == 1
        assert _daily_count(sw, USER) == 1
        entry_ids = [e["entry_id"] for e in sw.quota_ledger.find(
            {"feature": sweep.DAILY_CAP_FEATURE, "user_email": USER})]
        assert len(entry_ids) == 1

        # Simulate the concurrent tick: the dedup gate ran BEFORE tick A inserted,
        # so it sees nothing and lets the enqueue through to the unique index.
        from services import application_ledger_service as als
        monkeypatch.setattr(als, "dedup_gate", lambda email, identity: (False, None))
        _rewind_watermark(sw)
        out = sweep.run_user_sweep(USER)

        assert out["duplicates"] == 1
        assert out["enqueued"] == 0
        # The real application from tick A is still there and still CHARGED.
        assert sw.applications.count_documents({"user_email": USER}) == 1
        assert _daily_count(sw, USER) == 1
        assert sw.quota_ledger.find_one(
            {"entry_id": entry_ids[0]})["state"] == "reserved"

    def test_a_duplicate_never_consumes_a_daily_slot(self, sw):
        _seed_posting(sjid="swe-a", first_seen=NOW - timedelta(minutes=9))
        _rewind_watermark(sw)
        sweep.run_user_sweep(USER)
        used_after_first = sw.quota_counters.find_one(
            {"_id": {"$regex": sweep.DAILY_CAP_FEATURE}})

        # Same posting, cursor rewound: the dedup gate must short-circuit BEFORE
        # the reservation, or a re-sweep would silently drain the day's budget.
        _rewind_watermark(sw)
        out = sweep.run_user_sweep(USER)

        assert out["duplicates"] == 1
        assert out["enqueued"] == 0
        after = sw.quota_counters.find_one({"_id": {"$regex": sweep.DAILY_CAP_FEATURE}})
        assert after["count"] == used_after_first["count"] == 1


# ---------------------------------------------------------------------------
# Safety rails that already existed and must keep firing
# ---------------------------------------------------------------------------
class TestSafetyRails:
    def test_master_kill_switch_stops_everything(self, sw, monkeypatch):
        from services import auto_submit_service as ass
        monkeypatch.setattr(ass, "auto_submit_enabled", lambda: False)
        _seed_posting(sjid="swe-1", first_seen=NOW - timedelta(minutes=5))
        _rewind_watermark(sw)

        assert sweep.run_match_sweep() == {"skipped": "master_off"}
        assert sw.applications.count_documents({}) == 0

    def test_an_unset_autonomy_mode_refuses_to_act(self, sw):
        sw.auto_submit_settings.update_one({"user_email": USER},
                                           {"$unset": {"mode": ""}})
        _seed_posting(sjid="swe-1", first_seen=NOW - timedelta(minutes=5))
        _rewind_watermark(sw)

        assert sweep.run_user_sweep(USER) == {"skipped": "mode_unset"}
        assert sw.applications.count_documents({}) == 0

    def test_copilot_mode_is_not_swept(self, sw):
        """copilot = prepare-only, the user applies by hand. An unattended
        fan-out is not what that user asked for."""
        sw.auto_submit_settings.update_one(
            {"user_email": USER}, {"$set": {"mode": autonomy.MODE_COPILOT}})
        _seed_posting(sjid="swe-1", first_seen=NOW - timedelta(minutes=5))
        _rewind_watermark(sw)

        assert sweep.run_user_sweep(USER)["skipped"] == "mode_copilot"

    def test_no_resume_means_no_matching(self, sw):
        sw.resume_assets.delete_many({})
        sw.users.update_one({"email": USER}, {"$unset": {"active_resume_id": ""}})
        _seed_posting(sjid="swe-1", first_seen=NOW - timedelta(minutes=5))
        _rewind_watermark(sw)

        assert sweep.run_user_sweep(USER) == {"skipped": "no_resume"}

    def test_a_closed_posting_is_skipped(self, sw):
        _seed_posting(sjid="swe-1", first_seen=NOW - timedelta(minutes=5))
        _rewind_watermark(sw)
        candidates = sweep._new_postings([], NOW - timedelta(hours=1))
        assert len(candidates) == 1
        # Close it AFTER retrieval — the race the pre-write re-check exists for.
        sw.job_postings.update_one({"source_job_id": "swe-1"},
                                   {"$set": {"status": inv.STATUS_CLOSED}})
        assert sweep._still_open(candidates[0]["job_id"]) is False

    def test_a_closed_posting_never_reaches_the_ledger(self, sw):
        _seed_posting(sjid="swe-1", first_seen=NOW - timedelta(minutes=5))
        sw.job_postings.update_one({"source_job_id": "swe-1"},
                                   {"$set": {"status": inv.STATUS_CLOSED}})
        _rewind_watermark(sw)

        out = sweep.run_user_sweep(USER)

        assert out["candidates"] == 0
        assert sw.applications.count_documents({}) == 0

    def test_paid_cost_class_inventory_is_never_auto_applied_to(self, sw):
        """ADR-004: metered aggregator rows are neither free to read nor
        reliably applicable. They must not enter the unattended path."""
        _seed_posting(sjid="swe-1", first_seen=NOW - timedelta(minutes=5))
        sw.job_postings.update_one({"source_job_id": "swe-1"},
                                   {"$set": {"cost_class": inv.COST_CLASS_PAID}})
        _rewind_watermark(sw)

        assert sweep.run_user_sweep(USER)["candidates"] == 0

    def test_the_sweep_only_ever_enqueues_gather_never_a_submit(self, sw):
        for i in range(3):
            _seed_posting(sjid=f"swe-{i}", company=f"Acme {i}",
                          first_seen=NOW - timedelta(minutes=5 + i))
        _rewind_watermark(sw)

        sweep.run_user_sweep(USER)

        assert set(sw._enqueued.types()) == {"auto_apply_gather"}
        assert all(r["status"] == "queued" for r in sw.applications.find({}))

    def test_a_tick_is_bounded_even_with_a_flood_of_matches(self, sw):
        for i in range(30):
            _seed_posting(sjid=f"swe-{i}", company=f"Acme {i}",
                          first_seen=NOW - timedelta(minutes=5))
        _rewind_watermark(sw)

        out = sweep.run_user_sweep(USER)

        # Exactly the per-tick cap: 30 matches are available and the daily cap
        # (20) is not the binding constraint here, so the tick cap must be.
        assert out["enqueued"] == sweep.MAX_ENQUEUE_PER_TICK_AGGRESSIVE
        assert sw.applications.count_documents({}) == sweep.MAX_ENQUEUE_PER_TICK_AGGRESSIVE

    def test_one_users_failure_does_not_abort_the_sweep(self, sw, monkeypatch):
        sw.auto_submit_settings.insert_one(
            {"user_email": "broken@example.com", "auto_submit_enabled": True,
             "mode": autonomy.MODE_APPROVAL})
        _seed_posting(sjid="swe-1", first_seen=NOW - timedelta(minutes=5))
        _rewind_watermark(sw)

        real = sweep.run_user_sweep

        def flaky(email):
            if email == "broken@example.com":
                raise RuntimeError("boom")
            return real(email)

        monkeypatch.setattr(sweep, "run_user_sweep", flaky)
        totals = sweep.run_match_sweep()

        assert totals["users"] == 2
        assert totals["swept"] == 1
        assert totals["enqueued"] == 1


# ---------------------------------------------------------------------------
# Notification — match_alert finally gets a sender
# ---------------------------------------------------------------------------
class TestMatchAlert:
    def test_enqueued_matches_fire_one_match_alert(self, sw, monkeypatch):
        sent = []
        from services.notifications import service as notify
        monkeypatch.setattr(notify, "send",
                            lambda email, event, payload=None: sent.append(
                                (email, event, payload)) or {"ok": True})
        for i in range(2):
            _seed_posting(sjid=f"swe-{i}", company=f"Acme {i}",
                          first_seen=NOW - timedelta(minutes=5 + i))
        _rewind_watermark(sw)

        out = sweep.run_user_sweep(USER)

        assert out["enqueued"] == 2
        assert len(sent) == 1, "one alert per tick, not one per application"
        email, event, payload = sent[0]
        assert (email, event) == (USER, "match_alert")
        assert "Acme" in payload["text"]

    def test_no_matches_means_no_alert(self, sw, monkeypatch):
        sent = []
        from services.notifications import service as notify
        monkeypatch.setattr(notify, "send",
                            lambda email, event, payload=None: sent.append(1) or {"ok": True})
        _seed_posting(sjid="rn-1", title="Registered Nurse", description=NURSE_JD,
                      first_seen=NOW - timedelta(minutes=5))
        _rewind_watermark(sw)

        sweep.run_user_sweep(USER)

        assert sent == []


# ---------------------------------------------------------------------------
# Scheduler wiring — a new rule key needs BOTH a branch and a .tf entry
# ---------------------------------------------------------------------------
class TestSchedulerWiring:
    def test_dispatch_scheduled_routes_the_new_rule(self, sw):
        from services import scheduled_jobs
        result = scheduled_jobs.dispatch_scheduled("apply_match_sweep")
        assert result.get("job") == "apply_match_sweep"
        assert result.get("enqueued")
        assert sw._enqueued.types() == ["apply_match_sweep"]

    def test_terraform_declares_the_rule(self):
        """`ingest_*` has a prefix branch, so a new shard needs no Terraform.
        This rule does NOT — without the .tf entry it simply never fires."""
        from pathlib import Path
        tf = (Path(__file__).resolve().parent.parent
              / "infra" / "terraform" / "schedulers.tf").read_text(encoding="utf-8")
        assert "apply_match_sweep" in tf
        assert "cron(10/15 * * * ? *)" in tf, "must be offset from ingest (0/15, 7/15)"

    def test_resume_service_has_the_job_branch(self):
        from pathlib import Path
        src = (Path(__file__).resolve().parent.parent / "shared" / "python"
               / "services" / "resume_service.py").read_text(encoding="utf-8")
        assert 'elif job_type == "apply_match_sweep":' in src


# ---------------------------------------------------------------------------
# Autonomy threshold reachability (the 0.90 / Workday defect)
# ---------------------------------------------------------------------------
class TestAutonomyThresholdReachability:
    def test_every_implemented_ats_can_actually_reach_the_default(self):
        """0.90 was UNREACHABLE for Workday (max 0.85), so a Workday row could
        never auto-submit no matter how good the match. This test is the guard
        that stops that defect returning via an ATS_EASE edit."""
        for vendor in autonomy.IMPLEMENTED_ATS:
            ceiling = autonomy.max_achievable_score(vendor)
            assert ceiling >= autonomy.DEFAULT_AUTONOMY_THRESHOLD, (
                f"{vendor} tops out at {ceiling:.3f}, below the "
                f"{autonomy.DEFAULT_AUTONOMY_THRESHOLD} default threshold")

    def test_workday_ceiling_is_exactly_the_documented_number(self):
        assert autonomy.max_achievable_score("workday") == pytest.approx(0.85)

    def test_the_old_default_is_recorded_as_unreachable_for_workday(self):
        assert autonomy.threshold_reachable("workday", 0.90) is False
        assert autonomy.threshold_reachable("greenhouse", 0.90) is True

    def test_evaluate_reports_unreachability_instead_of_clamping(self, sw):
        autonomy.set_autonomy_threshold(USER, 0.90)
        out = autonomy.evaluate(USER, {"ats_family": "workday"},
                                answer_coverage=1.0, has_novel_free_text=False)
        assert out["threshold"] == 0.90, "the user's explicit setting is never overridden"
        assert out["threshold_reachable"] is False
        assert out["autonomy"] != autonomy.AUTO


class TestMatchThresholdPersistence:
    def test_default_is_the_good_match_band(self, sw):
        assert autonomy.DEFAULT_MATCH_THRESHOLD == 65
        assert dict(fit_score.BANDS)[65] == "good match"
        assert autonomy.get_match_threshold("nobody@example.com") == 65

    def test_roundtrip_and_clamp(self, sw):
        assert autonomy.set_match_threshold(USER, 70) == 70
        assert autonomy.get_match_threshold(USER) == 70
        assert autonomy.set_match_threshold(USER, 400) == 100
        assert autonomy.set_match_threshold(USER, -5) == 0

    def test_a_bad_value_raises_valueerror_for_a_400_never_a_401(self, sw):
        with pytest.raises(ValueError):
            autonomy.set_match_threshold(USER, "not-a-number")

    def test_a_corrupt_stored_value_falls_back_to_the_default(self, sw):
        sw.auto_submit_settings.update_one(
            {"user_email": USER}, {"$set": {"match_threshold": "banana"}})
        assert autonomy.get_match_threshold(USER) == autonomy.DEFAULT_MATCH_THRESHOLD
