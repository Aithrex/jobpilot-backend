"""Canonical job inventory (ADR-003) - identity, parse-once, leases, retention.

These tests pin the five invariants that replace queue semantics, because
ADR-003 deliberately schedules ingestion on EventBridge with NO SQS: there is
no visibility timeout, no DLQ and no FIFO ordering, so at-most-one-worker,
ordered progress and at-least-once safety have to hold in MongoDB or nowhere.

Runs against the suite's in-memory mongomock DB (tests/_vg_support.py).
"""
from datetime import datetime, timedelta, timezone

import pytest

from services import job_inventory_service as inv

NOW = datetime(2026, 7, 21, 12, 0, 0, tzinfo=timezone.utc)

INVENTORY_COLLECTIONS = ("job_postings", "job_source_state",
                         "job_ingestion_runs", "job_match_snapshots")


def stored(dt):
    """BSON has no timezone: PyMongo (and mongomock) hand back NAIVE UTC
    datetimes unless the client is built tz_aware. The service writes aware
    datetimes, so every read-back comparison must drop the tzinfo."""
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


@pytest.fixture()
def db():
    d = inv._db()
    for c in INVENTORY_COLLECTIONS:
        d[c].delete_many({})
    return d


def _raw(**over):
    base = {
        "source": "workday",
        "source_job_id": "REQ-1001",
        "url": "https://acme.wd1.myworkdayjobs.com/en-US/careers/job/REQ-1001",
        "company": "Acme, Inc.",
        "title": "Senior Backend Engineer (Remote)",
        "location": ["Austin, TX, USA"],
        "description": "We need Python, MongoDB and AWS experience.",
        "employment_type": "Full time",
        "posted_at": NOW - timedelta(days=2),
        "cost_class": "free",
        "source_quality": "direct",
    }
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# 1. Idempotent upsert by (source, source_job_id), incl. duplicate delivery
# ---------------------------------------------------------------------------

def test_upsert_is_idempotent_on_duplicate_delivery(db):
    first = inv.upsert_posting(_raw(), now=NOW)
    assert first["created"] is True
    assert first["content_changed"] is True

    # Same payload delivered again (Lambda self-invocation is at-least-once).
    second = inv.upsert_posting(_raw(), now=NOW + timedelta(minutes=5))

    assert second["created"] is False
    assert second["job_id"] == first["job_id"], "job_id must be stable across deliveries"
    assert db.job_postings.count_documents({}) == 1, "duplicate delivery must not fork the doc"

    doc = db.job_postings.find_one({})
    assert doc["first_seen_at"] == stored(NOW), "first_seen_at is $setOnInsert and must not move"
    assert doc["last_seen_at"] == stored(NOW + timedelta(minutes=5))


def test_upsert_normalizes_identity_and_taxonomy(db):
    inv.upsert_posting(_raw(), now=NOW)
    doc = db.job_postings.find_one({})

    assert doc["source"] == "workday" and doc["source_job_id"] == "REQ-1001"
    assert doc["employer_id"] == "acme", "legal suffix dropped for a stable employer key"
    assert doc["title_normalized"] == "senior backend engineer"
    assert doc["seniority"] == "senior"
    assert doc["work_mode"] == "remote"
    assert doc["locations"][0]["key"] == "austin-tx-usa"
    assert doc["status"] == inv.STATUS_ACTIVE and doc["active"] is True
    assert doc["normalization_version"] == inv.NORMALIZATION_VERSION


def test_same_posting_from_two_sources_keeps_separate_identity_but_one_content_hash(db):
    a = inv.upsert_posting(_raw(source="workday"), now=NOW)
    b = inv.upsert_posting(_raw(source="greenhouse", source_job_id="gh-7"), now=NOW)

    assert a["job_id"] != b["job_id"], "identity is per (source, source_job_id)"
    assert a["content_hash"] == b["content_hash"], "identical JD body hashes identically"
    assert len(inv.find_by_content_hash(a["content_hash"])) == 2


def test_unknown_cost_class_fails_closed_to_paid(db):
    """ADR-004: guest exclusion must be structural. An unlabelled source must
    never default into the guest-eligible free class."""
    inv.upsert_posting(_raw(cost_class=None), now=NOW)
    assert db.job_postings.find_one({})["cost_class"] == inv.COST_CLASS_PAID


def test_missing_date_stays_unknown_and_is_excluded_from_fresh_reads(db):
    """ADR-001: an unknown posted date is never promoted to 'posted today'."""
    inv.upsert_posting(_raw(source_job_id="NO-DATE", posted_at=None), now=NOW)
    inv.upsert_posting(_raw(), now=NOW)

    doc = db.job_postings.find_one({"source_job_id": "NO-DATE"})
    assert doc["posted_at"] is None
    assert doc["posted_at_confidence"] == inv.CONFIDENCE_UNKNOWN

    fresh = inv.find_active_fresh(max_age_days=7, now=NOW)
    assert [d["source_job_id"] for d in fresh] == ["REQ-1001"]


def test_upsert_requires_identity(db):
    with pytest.raises(ValueError):
        inv.upsert_posting(_raw(source_job_id=""), now=NOW)


# ---------------------------------------------------------------------------
# 2. content_hash change detection -> a changed JD is re-parsed ONCE
# ---------------------------------------------------------------------------

def test_unchanged_jd_is_not_reparsed(db):
    first = inv.upsert_posting(_raw(), now=NOW)
    assert first["needs_parse"] is True

    inv.mark_requirements_parsed(
        "workday", "REQ-1001", first["content_hash"],
        {"required_skills": ["python", "mongodb"], "preferred_skills": ["aws"]},
        now=NOW,
    )

    again = inv.upsert_posting(_raw(), now=NOW + timedelta(hours=6))
    assert again["content_changed"] is False
    assert again["needs_parse"] is False, "an unchanged JD must not spend a second extraction"
    assert inv.needs_requirements_parse(db.job_postings.find_one({})) is False


def test_changed_jd_is_reparsed_exactly_once(db):
    first = inv.upsert_posting(_raw(), now=NOW)
    inv.mark_requirements_parsed("workday", "REQ-1001", first["content_hash"],
                                 {"required_skills": ["python"]}, now=NOW)

    changed = inv.upsert_posting(
        _raw(description="Now we need Go, Kubernetes and Terraform."),
        now=NOW + timedelta(days=1),
    )
    assert changed["content_hash"] != first["content_hash"]
    assert changed["content_changed"] is True
    assert changed["needs_parse"] is True

    inv.mark_requirements_parsed("workday", "REQ-1001", changed["content_hash"],
                                 {"required_skills": ["go", "kubernetes"]},
                                 now=NOW + timedelta(days=1))

    # Re-delivered after the re-parse: no third extraction.
    after = inv.upsert_posting(
        _raw(description="Now we need Go, Kubernetes and Terraform."),
        now=NOW + timedelta(days=2),
    )
    assert after["needs_parse"] is False
    assert db.job_postings.find_one({})["requirements"]["required_skills"] == [
        "go", "kubernetes"]


def test_cosmetic_whitespace_change_is_not_a_content_change(db):
    first = inv.upsert_posting(_raw(), now=NOW)
    same = inv.upsert_posting(
        _raw(description="We  need   Python,\n MongoDB and AWS experience.  "),
        now=NOW + timedelta(hours=1),
    )
    assert same["content_hash"] == first["content_hash"]
    assert same["content_changed"] is False


def test_stale_parse_cannot_overwrite_a_newer_jd(db):
    """The extraction carries the hash it parsed FROM, in the filter. A parse
    that lands after the JD moved on must be dropped, not stored."""
    first = inv.upsert_posting(_raw(), now=NOW)
    stale_hash = first["content_hash"]

    inv.upsert_posting(_raw(description="Completely rewritten requirements."),
                       now=NOW + timedelta(hours=1))

    ok = inv.mark_requirements_parsed("workday", "REQ-1001", stale_hash,
                                      {"required_skills": ["obsolete"]}, now=NOW)
    assert ok is False
    assert db.job_postings.find_one({})["requirements"] is None


def test_find_parsed_requirements_is_the_parse_once_cache(db):
    first = inv.upsert_posting(_raw(), now=NOW)
    assert inv.find_parsed_requirements(first["content_hash"]) is None

    inv.mark_requirements_parsed("workday", "REQ-1001", first["content_hash"],
                                 {"required_skills": ["python"]}, now=NOW)

    cached = inv.find_parsed_requirements(first["content_hash"])
    assert cached and cached["required_skills"] == ["python"]

    # A second source carrying the identical JD reuses the extraction.
    other = inv.upsert_posting(_raw(source="greenhouse", source_job_id="gh-9"), now=NOW)
    assert other["content_hash"] == first["content_hash"]
    assert inv.find_parsed_requirements(other["content_hash"])["required_skills"] == ["python"]


# ---------------------------------------------------------------------------
# 3. Lease acquire / expire / steal (bounded concurrency without SQS)
# ---------------------------------------------------------------------------

def test_lease_excludes_a_second_worker(db):
    assert inv.acquire_lease("workday", "shard-a", "worker-1", now=NOW) is not None
    assert inv.acquire_lease("workday", "shard-a", "worker-2", now=NOW) is None, (
        "a live lease is the only thing bounding concurrency; it must exclude"
    )


def test_lease_is_reentrant_for_the_same_owner(db):
    inv.acquire_lease("workday", "shard-a", "worker-1", ttl_seconds=600, now=NOW)
    again = inv.acquire_lease("workday", "shard-a", "worker-1", ttl_seconds=600,
                              now=NOW + timedelta(seconds=60))
    assert again is not None
    assert again["lease_expiry"] == stored(NOW + timedelta(seconds=660))


def test_expired_lease_is_stealable_without_a_reaper(db):
    inv.acquire_lease("workday", "shard-a", "worker-1", ttl_seconds=300, now=NOW)
    later = NOW + timedelta(seconds=301)

    stolen = inv.acquire_lease("workday", "shard-a", "worker-2", now=later)
    assert stolen is not None and stolen["lease_owner"] == "worker-2"

    # The crashed worker must no longer be able to renew or release.
    assert inv.renew_lease("workday", "shard-a", "worker-1", now=later) is False
    assert inv.release_lease("workday", "shard-a", "worker-1") is False
    assert db.job_source_state.find_one({})["lease_owner"] == "worker-2"


def test_release_frees_the_shard_for_the_next_worker(db):
    inv.acquire_lease("workday", "shard-a", "worker-1", now=NOW)
    assert inv.release_lease("workday", "shard-a", "worker-1") is True
    assert inv.acquire_lease("workday", "shard-a", "worker-2", now=NOW) is not None


def test_backoff_defers_the_shard_then_lets_it_run(db):
    inv.acquire_lease("workday", "shard-a", "worker-1", now=NOW)
    state = inv.record_failure("workday", "shard-a", "HTTP 503 from tenant",
                               owner="worker-1", now=NOW)
    assert state["retry_count"] == 1
    assert state["next_attempt_at"] == stored(NOW + timedelta(seconds=inv.BACKOFF_BASE_SECONDS))
    inv.release_lease("workday", "shard-a", "worker-1")

    assert inv.acquire_lease("workday", "shard-a", "worker-2", now=NOW) is None
    assert inv.due_shards("workday", now=NOW) == []

    after = NOW + timedelta(seconds=inv.BACKOFF_BASE_SECONDS + 1)
    assert inv.acquire_lease("workday", "shard-a", "worker-2", now=after) is not None

    inv.record_success("workday", "shard-a", owner="worker-2", now=after)
    reset = inv.get_source_state("workday", "shard-a")
    assert reset["retry_count"] == 0 and reset["next_attempt_at"] is None


def test_repeated_failures_back_off_exponentially_and_cap(db):
    inv.acquire_lease("workday", "shard-a", "w", now=NOW)
    delays = []
    for _ in range(4):
        s = inv.record_failure("workday", "shard-a", "boom", owner="w", now=NOW)
        delays.append(s["backoff_seconds"])
    assert delays == [60, 120, 240, 480]
    assert inv._backoff_seconds(99) == inv.BACKOFF_MAX_SECONDS


# ---------------------------------------------------------------------------
# 4. Watermark advance (ordering, with no FIFO queue to provide it)
# ---------------------------------------------------------------------------

def test_watermark_advances_and_never_rewinds(db):
    inv.acquire_lease("workday", "shard-a", "worker-1", now=NOW)

    first = inv.advance_watermark("workday", "shard-a", "worker-1",
                                  NOW - timedelta(hours=2), cursor="page-3",
                                  etag="etag-abc", now=NOW)
    assert first["watermark"] == stored(NOW - timedelta(hours=2))
    assert first["cursor"] == "page-3" and first["etag"] == "etag-abc"

    forward = inv.advance_watermark("workday", "shard-a", "worker-1",
                                    NOW - timedelta(hours=1), cursor="page-4", now=NOW)
    assert forward["watermark"] == stored(NOW - timedelta(hours=1))

    # A replayed / out-of-order update must be dropped, not applied.
    rewind = inv.advance_watermark("workday", "shard-a", "worker-1",
                                   NOW - timedelta(hours=5), cursor="page-1", now=NOW)
    assert rewind is None
    state = inv.get_source_state("workday", "shard-a")
    assert state["watermark"] == stored(NOW - timedelta(hours=1))
    assert state["cursor"] == "page-4", "a rejected advance must not write its cursor"


def test_watermark_requires_the_lease(db):
    inv.acquire_lease("workday", "shard-a", "worker-1", now=NOW)
    assert inv.advance_watermark("workday", "shard-a", "worker-2", NOW, now=NOW) is None
    assert inv.get_source_state("workday", "shard-a")["watermark"] is None


# ---------------------------------------------------------------------------
# 5. purge_at is NEVER set on an active posting
# ---------------------------------------------------------------------------

def test_active_posting_never_carries_purge_at(db):
    inv.upsert_posting(_raw(), now=NOW)
    inv.touch_seen("workday", ["REQ-1001"], now=NOW + timedelta(hours=1))
    inv.upsert_posting(_raw(description="edited"), now=NOW + timedelta(hours=2))

    assert db.job_postings.count_documents(
        {"status": inv.STATUS_ACTIVE, "purge_at": {"$exists": True}}) == 0


def test_set_purge_at_refuses_a_live_posting(db):
    inv.upsert_posting(_raw(), now=NOW)
    with pytest.raises(ValueError):
        inv.set_purge_at("workday", "REQ-1001", NOW + timedelta(days=1))
    assert "purge_at" not in db.job_postings.find_one({})


def test_close_sets_purge_at_and_is_idempotent(db):
    inv.upsert_posting(_raw(), now=NOW)
    assert inv.close_posting("workday", "REQ-1001", reason="not_seen", now=NOW) is True

    doc = db.job_postings.find_one({})
    assert doc["status"] == inv.STATUS_CLOSED and doc["active"] is False
    assert doc["purge_at"] == stored(NOW + timedelta(days=inv.RETENTION_DAYS_AFTER_CLOSE))

    later = NOW + timedelta(days=5)
    assert inv.close_posting("workday", "REQ-1001", now=later) is False, (
        "a repeated close must not push retention further out")
    assert db.job_postings.find_one({})["purge_at"] == stored(
        NOW + timedelta(days=inv.RETENTION_DAYS_AFTER_CLOSE))


def test_reopened_posting_loses_its_purge_at(db):
    inv.upsert_posting(_raw(), now=NOW)
    inv.close_posting("workday", "REQ-1001", now=NOW)

    back = inv.upsert_posting(_raw(), now=NOW + timedelta(days=1))
    assert back["reopened"] is True

    doc = db.job_postings.find_one({})
    assert doc["status"] == inv.STATUS_ACTIVE
    assert "purge_at" not in doc, "a live posting must never keep a retention stamp"


def test_close_unseen_only_closes_stale_rows(db):
    inv.upsert_posting(_raw(source_job_id="OLD"), now=NOW - timedelta(days=3))
    inv.upsert_posting(_raw(source_job_id="SEEN"), now=NOW)

    closed = inv.close_unseen("workday", seen_before=NOW - timedelta(days=1), now=NOW)
    assert closed == 1
    assert db.job_postings.find_one({"source_job_id": "SEEN"})["status"] == inv.STATUS_ACTIVE
    assert db.job_postings.find_one({"source_job_id": "OLD"})["status"] == inv.STATUS_CLOSED


def test_purge_expired_only_deletes_closed_and_due(db):
    inv.upsert_posting(_raw(source_job_id="LIVE"), now=NOW)
    inv.upsert_posting(_raw(source_job_id="DEAD"), now=NOW)
    inv.close_posting("workday", "DEAD", now=NOW)

    assert inv.purge_expired(now=NOW + timedelta(days=1))["deleted"] == 0
    after = NOW + timedelta(days=inv.RETENTION_DAYS_AFTER_CLOSE + 1)
    assert inv.purge_expired(now=after)["deleted"] == 1
    assert [d["source_job_id"] for d in db.job_postings.find({})] == ["LIVE"]


# ---------------------------------------------------------------------------
# Retrieval + run bookkeeping + the snapshot bridge
# ---------------------------------------------------------------------------

def test_find_active_fresh_filters_age_status_and_cost_class(db):
    inv.upsert_posting(_raw(source_job_id="FRESH"), now=NOW)
    inv.upsert_posting(_raw(source_job_id="OLD",
                            posted_at=NOW - timedelta(days=26)), now=NOW)
    inv.upsert_posting(_raw(source_job_id="PAID", cost_class="paid"), now=NOW)
    inv.upsert_posting(_raw(source_job_id="CLOSED"), now=NOW)
    inv.close_posting("workday", "CLOSED", now=NOW)

    guest = inv.find_active_fresh(max_age_days=7, cost_classes=["free"], now=NOW)
    assert [d["source_job_id"] for d in guest] == ["FRESH"]

    authed = inv.find_active_fresh(max_age_days=30, now=NOW)
    assert sorted(d["source_job_id"] for d in authed) == ["FRESH", "OLD", "PAID"]

    assert [d["source_job_id"] for d in inv.find_active_fresh(
        role_family="software_engineering", cost_classes=["free"], now=NOW)] == ["FRESH"]
    assert inv.find_active_fresh(role_family="design", now=NOW) == []

    # A resume rarely maps to exactly one family, so retrieval accepts a set.
    assert [d["source_job_id"] for d in inv.find_active_fresh(
        role_families=["design", "software_engineering"],
        cost_classes=["free"], now=NOW)] == ["FRESH"]


def test_role_family_uses_the_same_taxonomy_the_scorer_uses():
    """Writer and reader MUST share one role vocabulary.

    The inventory stamps `role_family` at ingest; `guest_service` retrieves by
    the family `fit_score.classify_role` derives from the resume. If those two
    disagree the equality filter matches nothing and the product reports an
    empty job market with no error anywhere. It previously did disagree: the
    inventory used `pipeline_intel_service._role_family` (13 tech-only families,
    "swe") while the scorer used `fit_score.ROLE_FAMILIES` (31 families,
    "software_engineering"), so NO query could ever match and every
    non-technical posting was stamped "other".
    """
    from services import fit_score

    for title in ("Senior Backend Engineer", "Registered Nurse (ICU)",
                  "High School Math Teacher", "Staff Accountant",
                  "Journeyman Electrician", "Paralegal, Litigation"):
        assert inv.role_family_of(title) == fit_score.classify_role(
            inv.normalize_title(title)), title

    # Non-technical work must classify, not fall into the "other" bucket.
    assert inv.role_family_of("Registered Nurse (ICU)") == "nursing"
    assert inv.role_family_of("High School Math Teacher") == "teaching"
    # And an unrecognised title stays unclassified rather than being guessed
    # into an engineering family.
    assert inv.role_family_of("Grand Vizier of Vibes") == "other"


def test_seniority_uses_the_scorer_taxonomy():
    assert inv.seniority_of("Staff Backend Engineer") == "staff"
    # `intern` is its own band, no longer folded into `entry`. A candidate six
    # weeks from graduating wants new-grad roles, and a feed that answers
    # "entry" with unpaid summer positions is not answering the question.
    assert inv.seniority_of("Backend Engineer Intern") == "intern"
    assert inv.seniority_of("New Grad Backend Engineer") == "entry"
    # Resolved from the posting's own stated requirement, not the title.
    assert inv.seniority_of("Backend Engineer", required_years=1) == "entry"
    assert inv.seniority_of("Backend Engineer", required_years=8) == "staff"
    assert inv.seniority_of(
        "Backend Engineer",
        description="We are hiring recent graduates; 0-2 years of experience.",
    ) == "entry"
    # Was pinned to "mid". Changed deliberately: `fit_score.title_level` returns
    # None here, and storing that as `mid` asserted a level the posting never
    # stated, then let an equality filter hide every unmarked title from any
    # other band. See test_an_unmarked_title_is_unknown_not_mid below.
    assert inv.seniority_of("Backend Engineer") == inv.SENIORITY_UNKNOWN
    assert set(inv.SENIORITY_LEVELS) >= {"intern", "entry", "mid", "senior",
                                        "staff"}
    assert inv.SENIORITY_UNKNOWN not in inv.SENIORITY_LEVELS


def test_find_active_fresh_sorts_newest_first(db):
    for i in range(3):
        inv.upsert_posting(_raw(source_job_id=f"J{i}",
                                posted_at=NOW - timedelta(days=i)), now=NOW)
    got = [d["source_job_id"] for d in inv.find_active_fresh(now=NOW)]
    assert got == ["J0", "J1", "J2"]


def test_stale_verification_sweep_is_oldest_first(db):
    inv.upsert_posting(_raw(source_job_id="A"), now=NOW - timedelta(days=5))
    inv.upsert_posting(_raw(source_job_id="B"), now=NOW - timedelta(days=2))
    inv.upsert_posting(_raw(source_job_id="C"), now=NOW)

    stale = inv.find_stale_for_verification(
        "workday", older_than=NOW - timedelta(days=1))
    assert [d["source_job_id"] for d in stale] == ["A", "B"]


def test_run_counters_are_atomic_increments(db):
    run_id = inv.start_run("workday", "shard-a", trace_id="trace-1", now=NOW)
    inv.bump_run_counts(run_id, fetched=10, upserted=8)
    inv.bump_run_counts(run_id, fetched=5, created=2)
    inv.record_run_failure(run_id, "tenant timeout", item="REQ-9")

    doc = db.job_ingestion_runs.find_one({"run_id": run_id})
    assert doc["counts"]["fetched"] == 15
    assert doc["counts"]["upserted"] == 8 and doc["counts"]["created"] == 2
    assert doc["counts"]["errors"] == 1 and len(doc["failures"]) == 1
    assert doc["expire_at"] == stored(NOW + timedelta(days=inv.RUN_RETENTION_DAYS))

    finished = inv.finish_run(run_id, now=NOW + timedelta(seconds=42))
    assert finished["status"] == "succeeded" and finished["duration_ms"] == 42000


def test_run_failure_list_stays_bounded(db):
    run_id = inv.start_run("workday", "shard-a", now=NOW)
    for i in range(60):
        inv.record_run_failure(run_id, f"err {i}")
    doc = db.job_ingestion_runs.find_one({"run_id": run_id})
    assert len(doc["failures"]) == 50, "failure list must not grow unbounded"
    assert doc["counts"]["errors"] == 60


def test_snapshot_bridge_is_additive_and_path_safe(db):
    up = inv.upsert_posting(_raw(), now=NOW)
    db.job_match_snapshots.insert_one({
        "snapshot_id": "a" * 32,
        "jobs": [{"match_id": "b" * 32}],
        "jd_sources": {"b" * 32: {"jd_text": "legacy per-guest copy"}},
    })

    match_id = "b" * 32
    assert inv.link_snapshot_jobs("a" * 32, {match_id: up["job_id"]}) == 1

    snap = db.job_match_snapshots.find_one({"snapshot_id": "a" * 32})
    assert snap["job_refs"][match_id] == up["job_id"]
    assert snap["job_ids"] == [up["job_id"]]
    assert snap["jd_sources"][match_id]["jd_text"] == "legacy per-guest copy", (
        "the existing guest jd_sources path must keep working")

    assert inv.snapshot_job_ref("a" * 32, match_id) == up["job_id"]
    assert inv.resolve_jd(up["job_id"])["jd_text"].startswith("We need Python")

    # A match_id carrying a dotted/operator path must never become a field path.
    assert inv.link_snapshot_jobs("a" * 32, {"$set.evil": up["job_id"]}) == 0
    assert "evil" not in db.job_match_snapshots.find_one({"snapshot_id": "a" * 32})


def test_filters_reject_operator_injection(db):
    """CLAUDE.md invariant 4: untrusted values never reach an operator
    position. A dict in an identity field must not become a Mongo operator."""
    inv.upsert_posting(_raw(), now=NOW)
    assert inv.get_posting({"$ne": None}, {"$ne": None}) is None
    assert inv.get_by_job_id({"$ne": None}) is None
    assert inv.find_by_content_hash({"$ne": None}) == []
    assert inv.find_active_fresh(role_family={"$ne": None}, now=NOW) != []


# ---------------------------------------------------------------------------
# Seniority. `fit_score.title_level` returns None for a title with no level
# marker, and most real titles have none - so what we store for "unknown"
# decides whether the seniority filter narrows the market or hides it.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("title", [
    "Software Engineer", "Data Analyst", "Registered Nurse", "Accountant",
])
def test_an_unmarked_title_is_unknown_not_mid(title):
    """Defaulting to `mid` asserted a level the posting never stated, and
    because retrieval filters on the stored value, it also meant `mid` silently
    absorbed everything unclassified."""
    assert inv.seniority_of(title) == inv.SENIORITY_UNKNOWN


@pytest.mark.parametrize("title,level", [
    ("Senior Software Engineer", "senior"),
    ("Staff Engineer", "staff"),
    ("Engineering Director", "director"),
])
def test_a_marked_title_still_reads_its_level(title, level):
    assert inv.seniority_of(title) == level


def test_a_seniority_filter_does_not_hide_unmarked_postings(db):
    """The failure this guards. A candidate filtering for `entry` used to lose
    every posting whose title carried no marker - which is most of them."""
    NOW_ = NOW
    for i, title in enumerate(["Junior Developer", "Software Engineer"]):
        inv.upsert_posting({
            "source": "career-pages", "source_job_id": f"sen-{i}",
            "url": f"https://boards.greenhouse.io/x/jobs/{i}",
            "company": "Acme", "title": title, "location": ["Austin, TX, USA"],
            "description": "x" * 300, "cost_class": "free",
            "posted_at": NOW_ - timedelta(days=1),
        }, now=NOW_)

    rows = inv.find_active_fresh(source="career-pages", seniority="entry",
                                 max_age_days=30, now=NOW_)
    titles = {r["title_raw"] for r in rows}

    assert "Junior Developer" in titles
    assert "Software Engineer" in titles, \
        "an unmarked title must not be excluded by a seniority filter"


def test_a_seniority_filter_still_excludes_a_different_stated_level(db):
    """Riding unknown along must not turn the filter into a no-op."""
    for i, title in enumerate(["Junior Developer", "Engineering Director"]):
        inv.upsert_posting({
            "source": "career-pages", "source_job_id": f"lvl-{i}",
            "url": f"https://boards.greenhouse.io/x/jobs/{i}",
            "company": "Acme", "title": title, "location": ["Austin, TX, USA"],
            "description": "x" * 300, "cost_class": "free",
            "posted_at": NOW - timedelta(days=1),
        }, now=NOW)

    titles = {r["title_raw"] for r in inv.find_active_fresh(
        source="career-pages", seniority="entry", max_age_days=30, now=NOW)}
    assert "Engineering Director" not in titles


# ---------------------------------------------------------------------------
# A listing-only write never erases a stored description
# ---------------------------------------------------------------------------

class TestAListingNeverErasesTheBody:
    """Measured 2026-10-02: 59,454 active postings had their description
    erased. A detail fetch over the per-shard cap is DEFERRED and the row is
    upserted from the listing alone - which, for Workday and Greenhouse,
    carries no body - and `upsert_posting` $set the empty body over the
    stored one, along with everything read from it (level, work mode, the
    hash the requirements are guarded by). Every version bump sent the whole
    inventory through that path at once."""

    BODY = ("Qualifications: 6+ years of professional software engineering "
            "experience. This is a fully remote role. Python and AWS.")

    def test_the_stored_body_and_what_was_read_from_it_survive(self, db):
        first = inv.upsert_posting(_raw(description=self.BODY), now=NOW)
        before = db.job_postings.find_one({"source_job_id": "REQ-1001"})
        assert before["description_text"] or before["description_ref"]

        out = inv.upsert_posting(_raw(description=""), now=NOW + timedelta(hours=1))
        after = db.job_postings.find_one({"source_job_id": "REQ-1001"})
        for field in ("description_text", "description_ref", "description_chars",
                      "content_hash", "seniority", "work_mode"):
            assert after.get(field) == before.get(field), field
        assert after["last_seen_at"] == stored(NOW + timedelta(hours=1))
        assert out["content_changed"] is False
        assert out["content_hash"] == first["content_hash"]
        assert out["job_id"] == first["job_id"]

    def test_a_closed_posting_listed_again_reopens_with_its_body(self, db):
        inv.upsert_posting(_raw(description=self.BODY), now=NOW)
        db.job_postings.update_one({"source_job_id": "REQ-1001"},
                                   {"$set": {"status": "closed", "active": False,
                                             "purge_at": NOW}})
        out = inv.upsert_posting(_raw(description=""), now=NOW + timedelta(hours=1))
        after = db.job_postings.find_one({"source_job_id": "REQ-1001"})
        assert after["status"] == "active" and after["active"] is True
        assert "purge_at" not in after
        assert out["reopened"] is True
        assert after["description_text"] or after["description_ref"]

    def test_a_new_listing_only_posting_is_still_stored(self, db):
        out = inv.upsert_posting(_raw(source_job_id="REQ-2002", description=""), now=NOW)
        assert out["created"] is True
        assert db.job_postings.find_one({"source_job_id": "REQ-2002"}) is not None

    def test_a_new_body_still_replaces_the_old_one(self, db):
        inv.upsert_posting(_raw(description=self.BODY), now=NOW)
        out = inv.upsert_posting(_raw(description="A different body: Go and Kafka."),
                                 now=NOW + timedelta(hours=1))
        assert out["content_changed"] is True


def test_fetch_once_reads_employment_type_the_way_it_was_stored(db):
    """`FULL_TIME` was stored as `full-time` (`_safe_key`) but compared as
    `full_time` (`_slug`), so the posting never counted as unchanged and spent
    a detail fetch from the per-shard budget on every run."""
    from services import job_ingest_worker as w
    body = "Qualifications: 2+ years of experience in Python."
    inv.upsert_posting(_raw(employment_type="FULL_TIME", description=body), now=NOW)
    existing = db.job_postings.find_one({"source_job_id": "REQ-1001"})
    existing["requirements"] = {"extractor_version": inv.EXTRACTOR_VERSION}
    existing["normalization_version"] = inv.NORMALIZATION_VERSION
    row = {"title": "Senior Backend Engineer (Remote)", "employment_type": "FULL_TIME",
           "locations": ["Austin, TX, USA"]}
    assert w._listing_implies_same_content(existing, row) is True
