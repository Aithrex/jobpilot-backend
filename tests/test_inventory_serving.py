"""The inventory READ path: storage layout, retention, and score fidelity.

`test_job_inventory.py` covers the write-side invariants (identity, leases,
watermarks). This file covers everything the cutover added:

* JD bodies live in S3, not Mongo, and the request path never loads them;
* a stored requirement profile scores IDENTICALLY to the raw JD;
* re-crawling the same posting does not grow the collection;
* the rolling 7-day window actually deletes, and a posting a user applied to
  survives that deletion in object storage;
* a guest read is one indexed query that enforces freshness, cost class and
  job-side evidence structurally.

S3 is faked in-process rather than mocked at the boto3 layer: the contract that
matters is `job_content_store`'s, and a dict makes the "archive write failed"
branch trivial to exercise, which is the branch that guards data loss.
"""
from datetime import datetime, timedelta, timezone

import pytest

from services import fit_score
from services import job_content_store as store
from services import job_inventory_service as inv

# Anchor "now" to wall-clock so the 7-day freshness window stays valid in CI
# months after the suite was written (a fixed 2026-07-22 NOW ages out of the
# window and every inventory retrieval returns 0 rows).
NOW = datetime.now(timezone.utc).replace(microsecond=0)

COLLECTIONS = ("job_postings", "job_source_state", "job_ingestion_runs",
               "job_match_snapshots")

JD_BODY = (
    "Senior Platform Engineer. Requirements: 5+ years of experience with "
    "Python, AWS and Kubernetes. Must have Terraform. "
    "Preferred: Datadog and Go. Bachelor's degree in Computer Science required."
)

RESUME = {
    "skills": ["Python", "AWS", "Kubernetes", "Terraform", "Docker"],
    "experience": [{
        "title": "Cloud Engineer", "company": "Acme",
        "start_date": "2020-01", "end_date": "2025-01",
        "description": "Built Python services on AWS with Kubernetes and Terraform.",
    }],
    "education": [{"degree": "BS Computer Science", "school": "ASU"}],
}


class FakeS3:
    """Minimal S3: put/get/head over a dict, with a switch to fail writes."""

    def __init__(self):
        self.objects = {}
        self.fail_writes = False
        self.puts = 0

    def put_object(self, Bucket, Key, Body, **kw):
        if self.fail_writes:
            raise RuntimeError("simulated S3 outage")
        self.puts += 1
        self.objects[Key] = Body

    def get_object(self, Bucket, Key, **kw):
        if Key not in self.objects:
            raise KeyError(Key)
        payload = self.objects[Key]

        class _B:
            @staticmethod
            def read():
                return payload
        return {"Body": _B}

    def head_object(self, Bucket, Key, **kw):
        if Key not in self.objects:
            raise KeyError(Key)
        return {"ContentLength": len(self.objects[Key])}


@pytest.fixture()
def s3(monkeypatch):
    fake = FakeS3()
    monkeypatch.setenv("RESUME_S3_BUCKET", "test-bucket")
    monkeypatch.setattr(store, "_s3", lambda: fake)
    store.reset_caches()
    yield fake
    store.reset_caches()


@pytest.fixture()
def db(s3):
    """Clean the inventory collections BEFORE and AFTER.

    The suite shares one process-wide mongomock database, and the guest path
    now READS `job_postings`, so postings left behind here leak into every
    later test that exercises matching - an order-dependent failure that would
    not reproduce locally if the collection order happens to differ.
    """
    d = inv._db()
    for c in COLLECTIONS:
        d[c].delete_many({})
    yield d
    for c in COLLECTIONS:
        d[c].delete_many({})


def _raw(**over):
    base = {
        "source": "career-pages",
        "source_job_id": "acme:REQ-1",
        "url": "https://boards.greenhouse.io/acme/jobs/1",
        "company": "Acme, Inc.",
        "title": "Senior Platform Engineer",
        "location": ["Austin, TX, USA"],
        "description": JD_BODY,
        "employment_type": "Full time",
        "posted_at": NOW - timedelta(days=2),
        "cost_class": "free",
        "source_quality": "direct",
    }
    base.update(over)
    return base


def _ingest(raw=None, *, now=NOW):
    """upsert + deterministic requirement extraction, i.e. what a shard does."""
    raw = raw or _raw()
    outcome = inv.upsert_posting(raw, now=now)
    if outcome["needs_parse"]:
        profile = fit_score.extract_requirements(raw.get("description") or "",
                                                 raw.get("title") or "")
        inv.mark_requirements_parsed(raw["source"], raw["source_job_id"],
                                     outcome["content_hash"], profile, now=now)
    return outcome


# ---------------------------------------------------------------------------
# 1. Bodies live in S3, and the read path never loads them
# ---------------------------------------------------------------------------

def test_jd_body_goes_to_s3_and_mongo_keeps_only_a_pointer(db, s3):
    _ingest()
    doc = db.job_postings.find_one({})

    assert doc["description_text"] == "", "the body must not sit in Mongo"
    assert doc["description_ref"] == doc["content_hash"]
    assert doc["description_chars"] == len(JD_BODY)
    assert store.get_jd(doc["description_ref"]) == JD_BODY


def test_identical_bodies_store_one_object(db, s3):
    """Content-addressed keys dedupe the same requisition across boards."""
    _ingest(_raw(source_job_id="acme:REQ-1"))
    before = s3.puts
    _ingest(_raw(source_job_id="beta:REQ-9", company="Acme, Inc."))
    assert s3.puts == before, "an identical body must not be re-uploaded"


def test_card_projection_excludes_the_body(db, s3):
    _ingest()
    row = inv.find_active_fresh(cost_classes=["free"], now=NOW)[0]
    assert "description_text" not in row
    assert row["requirements"]["required_skills"]


# ---------------------------------------------------------------------------
# 2. Score fidelity - the reason the body can stay out of the read path
# ---------------------------------------------------------------------------

def test_stored_profile_scores_identically_to_the_raw_jd(db, s3):
    """The precomputed path must not cost a single point of accuracy.

    If these ever diverge, the fast path is quietly serving different rankings
    from the ones the eval suite measures.
    """
    _ingest()
    row = inv.find_active_fresh(cost_classes=["free"], now=NOW)[0]
    profile = fit_score.build_resume_profile(RESUME)

    from_jd = fit_score.score(
        RESUME, {"description": JD_BODY, "title": "Senior Platform Engineer",
                 "location": "Austin, TX, USA"}, profile=profile)
    from_store = fit_score.score(
        RESUME, {"requirements": row["requirements"],
                 "title": "Senior Platform Engineer",
                 "location": "Austin, TX, USA"}, profile=profile)

    assert from_store["match_score"] == from_jd["match_score"]
    assert from_store["component_scores"] == from_jd["component_scores"]
    assert from_store["band"] == from_jd["band"]
    assert from_store["missing_requirements"] == from_jd["missing_requirements"]


def test_a_posting_with_no_readable_requirements_cannot_present_as_a_match(db, s3):
    """The failure that produces "one or two wrong posts" in a top five.

    A body-less posting yields no requirements, and absent requirements earn
    NEUTRAL credit in every component, so before the job-evidence gate such a
    row scored 72 "good match" against a resume it had never been compared to.
    """
    profile = fit_score.build_resume_profile(RESUME)
    verdict = fit_score.score(
        RESUME, {"description": "", "title": "Senior Platform Engineer"},
        profile=profile)

    assert verdict["match_score"] <= fit_score.CONSTRAINT_CAPS["insufficient_job_requirements"]
    assert verdict["confidence"] == "low"
    assert any(c["type"] == "job_evidence" and c["status"] == "missing"
               for c in verdict["hard_constraints"])
    assert not fit_score.has_job_requirements(fit_score.extract_requirements("", "x"))


# ---------------------------------------------------------------------------
# 3. Re-crawl must not grow the collection
# ---------------------------------------------------------------------------

def test_recrawling_the_same_posting_never_duplicates(db, s3):
    """Every 15 minutes the same board is listed again. An unchanged posting
    must be an UPDATE, never a second document, or the collection grows without
    bound at the crawl frequency."""
    for i in range(5):
        _ingest(now=NOW + timedelta(minutes=15 * i))

    assert db.job_postings.count_documents({}) == 1
    doc = db.job_postings.find_one({})
    assert doc["last_seen_at"] > doc["first_seen_at"]

    second = inv.upsert_posting(_raw(), now=NOW + timedelta(hours=2))
    assert second["created"] is False
    assert second["content_changed"] is False
    assert second["needs_parse"] is False, "an unchanged body must not re-parse"


def test_an_edited_posting_updates_in_place_and_reparses(db, s3):
    _ingest()
    before = db.job_postings.find_one({})["requirements"]["required_skills"]

    _ingest(_raw(description=JD_BODY + " Must also have PostgreSQL and Java."),
            now=NOW + timedelta(hours=1))

    assert db.job_postings.count_documents({}) == 1, "an edit updates, never forks"
    doc = db.job_postings.find_one({})
    assert doc["requirements"]["source_content_hash"] == doc["content_hash"],         "an edited body must be RE-extracted, not left on the old profile"
    assert set(doc["requirements"]["required_skills"]) > set(before)


# ---------------------------------------------------------------------------
# 4. The rolling 7-day window
# ---------------------------------------------------------------------------

def test_postings_older_than_the_window_are_closed_then_deleted(db, s3):
    _ingest(_raw(source_job_id="fresh:1", posted_at=NOW - timedelta(days=2)))
    _ingest(_raw(source_job_id="old:1", posted_at=NOW - timedelta(days=9)))

    assert inv.expire_aged_postings(now=NOW) == 1
    aged = db.job_postings.find_one({"source_job_id": "old:1"})
    assert aged["status"] == inv.STATUS_CLOSED
    assert aged["closed_reason"] == inv.CLOSED_REASON_AGED_OUT

    # retention_days=0 on an aged-out row, so the SAME sweep can purge it.
    counts = inv.purge_expired(now=NOW)
    assert counts["deleted"] == 1
    assert [d["source_job_id"] for d in db.job_postings.find({})] == ["fresh:1"]


def test_expiry_never_touches_a_posting_inside_the_window(db, s3):
    _ingest(_raw(posted_at=NOW - timedelta(days=6, hours=23)))
    assert inv.expire_aged_postings(now=NOW) == 0
    assert db.job_postings.find_one({})["status"] == inv.STATUS_ACTIVE


# ---------------------------------------------------------------------------
# 5. "A job you applied to is never lost"
# ---------------------------------------------------------------------------

def test_pinned_posting_survives_retention_in_object_storage(db, s3):
    _ingest()
    job_id = db.job_postings.find_one({})["job_id"]

    assert inv.pin_posting(job_id, reason="applied") is True
    assert inv.is_pinned(job_id) is True

    # Age it out and run the full sweep.
    db.job_postings.update_one({"job_id": job_id},
                               {"$set": {"posted_at": NOW - timedelta(days=30)}})
    inv.expire_aged_postings(now=NOW)
    counts = inv.purge_expired(now=NOW)

    assert counts["deleted"] == 1
    assert db.job_postings.count_documents({}) == 0, "Mongo must not keep it"

    # ...but it is still fully resolvable, body included.
    resolved = inv.resolve_jd(job_id)
    assert resolved is not None
    assert resolved["archived"] is True
    assert resolved["jd_text"] == JD_BODY
    assert resolved["company"] == "Acme, Inc."
    assert resolved["requirements"]["required_skills"]


def test_a_pinned_posting_is_retained_when_it_cannot_be_archived(db, s3):
    """The guarantee is verified against object storage, not trusted from a
    flag. If the archive is not there, the row does not go."""
    _ingest()
    job_id = db.job_postings.find_one({})["job_id"]

    s3.fail_writes = True
    assert inv.pin_posting(job_id, reason="applied") is False
    assert inv.is_pinned(job_id) is True, "the flag still lands so the guard fires"

    db.job_postings.update_one({"job_id": job_id},
                               {"$set": {"posted_at": NOW - timedelta(days=30)}})
    inv.expire_aged_postings(now=NOW)
    counts = inv.purge_expired(now=NOW)

    assert counts["deleted"] == 0
    assert counts["retained_unarchived"] == 1
    assert db.job_postings.count_documents({}) == 1, "never delete what is not archived"

    # Once storage recovers the next sweep completes the job.
    s3.fail_writes = False
    counts = inv.purge_expired(now=NOW)
    assert counts["archived"] == 1
    assert counts["deleted"] == 1
    assert inv.resolve_jd(job_id)["jd_text"] == JD_BODY


def test_unpinned_postings_are_deleted_without_an_archive(db, s3):
    """Archiving every expired posting would defeat the point: only rows a user
    is attached to are worth keeping."""
    _ingest()
    job_id = db.job_postings.find_one({})["job_id"]
    db.job_postings.update_one({"job_id": job_id},
                               {"$set": {"posted_at": NOW - timedelta(days=30)}})
    inv.expire_aged_postings(now=NOW)

    counts = inv.purge_expired(now=NOW)
    assert counts["deleted"] == 1
    assert counts["archived"] == 0
    assert store.archive_exists(job_id) is False


def test_resolve_jd_reads_the_body_from_object_storage(db, s3):
    _ingest()
    job_id = db.job_postings.find_one({})["job_id"]
    resolved = inv.resolve_jd(job_id)
    assert resolved["archived"] is False
    assert resolved["jd_text"] == JD_BODY, "body resolved from S3, not from Mongo"


# ---------------------------------------------------------------------------
# 6. Retrieval enforces the guest contract in the QUERY
# ---------------------------------------------------------------------------

def test_retrieval_enforces_freshness_cost_class_and_status(db, s3):
    _ingest(_raw(source_job_id="ok:1"))
    _ingest(_raw(source_job_id="stale:1", posted_at=NOW - timedelta(days=20)))
    _ingest(_raw(source_job_id="paid:1", cost_class="paid"))
    _ingest(_raw(source_job_id="undated:1", posted_at=None))
    _ingest(_raw(source_job_id="closed:1"))
    inv.close_posting("career-pages", "closed:1", now=NOW)

    got = [d["source_job_id"] for d in inv.find_active_fresh(
        max_age_days=7, cost_classes=[inv.COST_CLASS_FREE], now=NOW)]
    assert got == ["ok:1"]


def test_retrieval_matches_the_family_the_scorer_derives(db, s3):
    """The end-to-end version of the taxonomy invariant: a nurse's resume must
    retrieve nursing postings."""
    _ingest(_raw(source_job_id="rn:1", title="Registered Nurse, ICU",
                 description="Requires active RN license, BLS and ACLS. 2+ years ICU."))
    nurse_resume = {"skills": ["ICU", "ACLS", "BLS"],
                    "experience": [{"title": "Registered Nurse", "company": "St Mary",
                                    "start_date": "2019-01", "end_date": "2025-01",
                                    "description": "ICU nurse, ACLS certified."}]}
    family = fit_score.classify_role("registered nurse")
    assert family == "nursing"

    got = inv.find_active_fresh(role_families=[family],
                                cost_classes=[inv.COST_CLASS_FREE], now=NOW)
    assert [d["source_job_id"] for d in got] == ["rn:1"]
    assert fit_score.score(nurse_resume, {"requirements": got[0]["requirements"],
                                          "title": "Registered Nurse, ICU"},
                           profile=None)["match_score"] > 0


# ---------------------------------------------------------------------------
# 7. The guest read path end to end
# ---------------------------------------------------------------------------

CLOUD_RESUME = {
    "name": "Test Candidate",
    "skills": ["Python", "AWS", "Kubernetes", "Terraform", "Docker", "Linux"],
    "experience": [{
        "title": "Cloud Engineer", "company": "Acme",
        "start_date": "2020-01", "end_date": "2025-01",
        "description": ("Built Python services on AWS with Kubernetes, "
                        "Terraform and Docker on Linux."),
    }],
    "education": [{"degree": "BS Computer Science", "school": "ASU"}],
    "projects": [], "certifications": [],
}


def _seed_inventory(n=20, **over):
    from services import guest_service
    for i in range(n):
        _ingest(_raw(source_job_id=f"acme:REQ-{i}",
                     url=f"https://boards.greenhouse.io/acme/jobs/{i}", **over))
    return guest_service


def test_guest_retrieval_returns_scorable_rows_from_one_query(db, s3):
    guest_service = _seed_inventory(20)
    rows, report = guest_service._fetch_from_inventory(
        CLOUD_RESUME, None, resume_skills=CLOUD_RESUME["skills"],
        resume_years=5, tz_offset_minutes=0)

    assert report["ok"] == ["inventory"]
    # The invariant is AGREEMENT, not a specific label: the families derived
    # from the resume must include the family the crawler stamped on the
    # postings, or retrieval silently returns nothing.
    stored_family = db.job_postings.find_one({})["role_family"]
    assert stored_family in report["role_families"]
    assert len(rows) == 20
    row = rows[0]
    # The shape the rest of the guest pipeline already expects.
    assert row["days_ago"] is not None, "freshness gate needs a known age"
    assert row["apply_link"].startswith("https://boards.greenhouse.io/")
    assert row["description"] == "", "no JD text crosses the wire on this path"
    assert row["requirements"]["required_skills"]


def test_guest_retrieval_excludes_paid_sources_structurally(db, s3):
    """ADR-004: an anonymous guest must never see a metered aggregator row, and
    the guarantee lives in the QUERY, not in a config flag."""
    guest_service = _seed_inventory(15)
    _ingest(_raw(source_job_id="paid:1", cost_class="paid"))

    rows, _ = guest_service._fetch_from_inventory(CLOUD_RESUME)
    assert all(r["job_id"] for r in rows)
    assert not any("paid" in str(r["url"]) for r in rows)
    assert len(rows) == 15


def test_guest_retrieval_drops_postings_with_no_readable_requirements(db, s3):
    """A row we never read the body of stays in the inventory but cannot be a
    headline match."""
    guest_service = _seed_inventory(15)
    inv.upsert_posting(_raw(source_job_id="nobody:1", description=""), now=NOW)

    rows, report = guest_service._fetch_from_inventory(CLOUD_RESUME)
    assert report["dropped_no_requirements"] == 1
    assert not any(r["job_id"] == "nobody:1" for r in rows)
    assert len(rows) == 15


def test_guest_retrieval_reports_undetermined_role_rather_than_guessing(db, s3):
    from services import guest_service
    rows, report = guest_service._fetch_from_inventory({"skills": [], "experience": []})
    assert rows == []
    assert report["empty_reason"] == "role_undetermined"


def test_sync_path_returns_none_when_the_inventory_is_cold(db, s3, monkeypatch):
    """The synchronous route runs in a 60s Lambda. A cold inventory must return
    None so the caller falls back to the async job, NEVER start a 210s scan."""
    from services import guest_service

    _ingest()  # one posting, far below INVENTORY_MIN_CANDIDATES

    def _boom(*a, **kw):
        raise AssertionError("inventory_only must never trigger a live scan")

    monkeypatch.setattr(guest_service, "_fetch_free_jobs", _boom)
    gid = guest_service.create_or_get_session(None, None, "iphash")["guest_id"]
    guest_service.attach_parsed_resume(gid, CLOUD_RESUME, "resume text " * 10,
                                       "resume.pdf")

    assert guest_service.compute_guest_matches(gid, inventory_only=True) is None


def test_sync_path_serves_matches_when_the_inventory_is_warm(db, s3, monkeypatch):
    from services import guest_service

    _seed_inventory(20)

    def _boom(*a, **kw):
        raise AssertionError("a warm inventory must not trigger a live scan")

    monkeypatch.setattr(guest_service, "_fetch_free_jobs", _boom)
    gid = guest_service.create_or_get_session(None, None, "iphash")["guest_id"]
    guest_service.attach_parsed_resume(gid, CLOUD_RESUME, "resume text " * 10,
                                       "resume.pdf")

    result = guest_service.compute_guest_matches(gid, inventory_only=True)

    assert result is not None
    assert result["sources_report"]["mode"] == "inventory"
    assert result["jobs"], "a warm inventory with a matching resume must return jobs"
    for card in result["jobs"]:
        assert card["match_score"] >= result["min_score"], \
            "only score-qualifying jobs are served"
        assert card["days_ago"] <= 7, "the 7-day guest contract still holds"
        assert card["url"].startswith("https://")
    # Ranked by score, best first.
    scores = [c["match_score"] for c in result["jobs"]]
    assert scores == sorted(scores, reverse=True)


def test_scan_mode_is_the_rollback(db, s3, monkeypatch):
    from services import guest_service
    monkeypatch.setenv("GUEST_MATCH_MODE", "scan")
    assert guest_service.inventory_mode_enabled() is False
    monkeypatch.delenv("GUEST_MATCH_MODE")
    assert guest_service.inventory_mode_enabled() is True


# ---------------------------------------------------------------------------
# 8. Fetch-once must survive the body moving to S3
# ---------------------------------------------------------------------------

def test_unchanged_posting_is_not_refetched_when_the_body_lives_in_s3(db, s3):
    """The fetch-once rule keyed on `description_text` being non-empty.

    With bodies in object storage that field is empty BY DESIGN, so the check
    would have returned False for every posting on every tick and re-fetched
    the entire registry every 15 minutes - roughly a 20x increase in outbound
    requests, and the exact opposite of the change's purpose. The signal is
    `description_chars` / `description_ref` instead.
    """
    from services import job_ingest_worker as w

    _ingest()
    existing = db.job_postings.find_one({})
    assert existing["description_text"] == "", "precondition: body is in S3"

    listing_row = {
        "source_job_id": existing["source_job_id"],
        "title": "Senior Platform Engineer",
        "employment_type": "Full time",
        "locations": ["Austin, TX, USA"],
    }
    assert w._listing_implies_same_content(existing, listing_row) is True

    # A real change still forces a refetch.
    assert w._listing_implies_same_content(
        existing, {**listing_row, "title": "Staff Platform Engineer"}) is False


def test_triage_batches_the_existing_posting_lookup(db, s3):
    """One query per shard, not one per row."""
    for i in range(30):
        _ingest(_raw(source_job_id=f"acme:REQ-{i}"))

    found = inv.get_postings_map("career-pages", [f"acme:REQ-{i}" for i in range(30)])
    assert len(found) == 30
    assert found["acme:REQ-7"]["content_hash"]
    assert found["acme:REQ-7"]["description_chars"] == len(JD_BODY)
    assert inv.get_postings_map("career-pages", []) == {}
    assert inv.get_postings_map("career-pages", ["nope"]) == {}


# ---------------------------------------------------------------------------
# 9. Source-specific body assembly and role-family recovery
# ---------------------------------------------------------------------------

def test_lever_body_is_assembled_from_all_four_fields():
    """`descriptionPlain` alone loses most Lever postings.

    It is empty on a large share of real postings and, when present, holds only
    the intro: the requirements live in the structured `lists` sections. Reading
    only that field stored ~60% of Lever rows with no body, which made them
    unscorable and unservable.
    """
    from services import job_ingest_worker as w

    job = {
        "id": "abc", "text": "Construction Inspector",
        "descriptionPlain": "",                    # genuinely empty in the wild
        "descriptionBodyPlain": "",
        "lists": [
            {"text": "What You'll Do:", "content": "<ul><li>Site inspection</li></ul>"},
            {"text": "What You'll Bring:",
             "content": "<ul><li>5 years of AutoCAD</li><li>OSHA 30</li></ul>"},
        ],
        "additionalPlain": "Benefits and EEO statement.",
    }
    body = w._lever_body(job)

    assert "Site inspection" in body
    assert "AutoCAD" in body
    assert "OSHA 30" in body
    assert "Benefits and EEO" in body
    # Headings survive: fit_score._split_sections uses them for the
    # required-vs-preferred split.
    assert "What You'll Bring:" in body
    assert "<li>" not in body, "list HTML must be flattened to text"

    assert w._lever_body({"id": "x", "text": "t"}) == ""


def test_requirements_can_upgrade_an_unclassified_role_family(db, s3):
    """A title `classify_role` cannot read must not strand the posting.

    `role_family` is stamped from the title alone, and the classifier refuses to
    guess, so titles like "Sr. Forward Deployed Engineer" land in "other" and
    become unreachable by role-family retrieval even when the body says plainly
    what the job is.
    """
    raw = _raw(source_job_id="odd:1", title="Sr. Forward Deployed Specialist",
               description=("We are hiring a registered nurse for our ICU. "
                            "Requires an active RN license, BLS and ACLS."))
    outcome = inv.upsert_posting(raw, now=NOW)
    assert db.job_postings.find_one({"source_job_id": "odd:1"})["role_family"] == "other"

    profile = fit_score.extract_requirements(raw["description"], raw["title"])
    assert profile["role_family"] == "nursing", "the body classifies even when the title does not"
    inv.mark_requirements_parsed("career-pages", "odd:1", outcome["content_hash"],
                                 profile, now=NOW)

    assert db.job_postings.find_one({"source_job_id": "odd:1"})["role_family"] == "nursing"
    assert inv.find_active_fresh(role_families=["nursing"],
                                 cost_classes=["free"], now=NOW)


def test_a_title_derived_family_is_never_overwritten_by_the_body(db, s3):
    """Upgrade only. A body can mention many roles ("you will partner with our
    nursing staff"); the title is the employer's own summary."""
    raw = _raw(source_job_id="eng:1", title="Senior Platform Engineer",
               description="You will partner with our nursing staff. RN license a plus.")
    outcome = inv.upsert_posting(raw, now=NOW)
    stamped = db.job_postings.find_one({"source_job_id": "eng:1"})["role_family"]
    assert stamped not in ("", "other")

    profile = fit_score.extract_requirements(raw["description"], raw["title"])
    inv.mark_requirements_parsed("career-pages", "eng:1", outcome["content_hash"],
                                 profile, now=NOW)

    assert db.job_postings.find_one({"source_job_id": "eng:1"})["role_family"] == stamped


# ---------------------------------------------------------------------------
# 10. Strict US gating on the guest path
# ---------------------------------------------------------------------------

US_CASES = [
    # Proven US: explicit marker, state code, or full state name.
    ("Austin, TX, USA", True), ("Tempe, Arizona", True),
    ("Austin, Texas", True), ("New York, New York", True),
    ("Remote - USA", True), ("Remote - United States", True),
    ("Office - USA - CA - Headquarters", True), ("Washington, DC", True),
    ("San Juan, Puerto Rico", True), ("Chantilly, VA", True),
    # A US STATE outranks a foreign city name, which is what rescues these.
    ("Paris, TX", True), ("Vienna, VA", True), ("Dublin, Ohio", True),
    # Explicitly foreign.
    ("Hyderabad, India", False), ("London, United Kingdom", False),
    ("Toronto, Ontario", False), ("Sydney, New South Wales", False),
    ("KSA-Work from Home", False),
    # An explicit country beats a colliding state code ("or" is Oregon).
    ("Canada - Remote (ON, AB, or NS Only)", False),
    # UNPROVEN, and therefore dropped. This is the whole difference from the
    # browse tabs, which keep these.
    ("3 Locations", False), ("Remote", False), ("Getafe Area", False),
    ("Asia", False), ("", False),
]


@pytest.mark.parametrize("location,expected", US_CASES)
def test_strict_us_gate(location, expected):
    from services import guest_service as g

    assert g.is_us_confident({"location": location, "country_hint": ""}) is expected


def test_a_parsed_country_field_is_authoritative():
    from services import guest_service as g

    assert g.is_us_confident({"location": "Tempe, Arizona", "country_hint": "us"})
    # Country wins over a US-looking city string.
    assert not g.is_us_confident({"location": "Springfield", "country_hint": "india"})


def test_guest_feed_drops_non_us_postings(db, s3, monkeypatch):
    """The crawler mirrors each board in full, US or not, so this filter is
    what makes the guest feed US-only. The legacy scan enforced it by passing
    us_only=True to the source services; the inventory path must too."""
    from services import guest_service as g

    monkeypatch.setenv("GUEST_US_STRICT", "1")
    for i in range(20):
        _ingest(_raw(source_job_id=f"us:{i}", location="Austin, TX, USA"))
    for i in range(20):
        _ingest(_raw(source_job_id=f"in:{i}", location="Hyderabad, India"))
    for i in range(10):
        _ingest(_raw(source_job_id=f"amb:{i}", location="3 Locations"))

    rows, report = g._fetch_from_inventory(CLOUD_RESUME)

    assert report["us_gate"] == "strict"
    assert report["dropped_non_us"] == 30, "foreign AND unproven rows both go"
    assert len(rows) == 20
    assert all("Austin" in r["location"] for r in rows)


def test_lenient_mode_is_the_rollback(db, s3, monkeypatch):
    from services import guest_service as g

    monkeypatch.setenv("GUEST_US_STRICT", "0")
    for i in range(20):
        _ingest(_raw(source_job_id=f"us:{i}", location="Austin, TX, USA"))
    for i in range(10):
        _ingest(_raw(source_job_id=f"amb:{i}", location="3 Locations"))

    rows, report = g._fetch_from_inventory(CLOUD_RESUME)
    assert report["us_gate"] == "lenient"
    assert len(rows) == 30, "lenient keeps ambiguous locations"


def test_warmth_is_judged_before_the_quality_gates(db, s3, monkeypatch):
    """A role family the crawler HAS covered must not fall back to the 210s
    live scan just because most of its postings are non-US.

    Those removals are a real answer about the market, not a cold cache, and
    the scan would spend three minutes rediscovering the same thinness.
    """
    from services import guest_service as g

    monkeypatch.setenv("GUEST_US_STRICT", "1")
    for i in range(40):
        _ingest(_raw(source_job_id=f"in:{i}", location="Hyderabad, India"))
    for i in range(3):
        _ingest(_raw(source_job_id=f"us:{i}", location="Austin, TX, USA"))

    def _boom(*a, **kw):
        raise AssertionError("a warm-but-US-thin family must not trigger a scan")

    monkeypatch.setattr(g, "_fetch_free_jobs", _boom)
    gid = g.create_or_get_session(None, None, "iphash")["guest_id"]
    g.attach_parsed_resume(gid, CLOUD_RESUME, "resume text " * 10, "resume.pdf")

    result = g.compute_guest_matches(gid, inventory_only=True)
    assert result is not None
    assert result["sources_report"]["mode"] == "inventory"


def test_bare_us_token_and_unqualified_cities_are_recognised(db, s3):
    """Both were false NEGATIVES found by auditing 9000 live locations.

    "US" alone was the single most common location string in the inventory
    (828 rows) and was being dropped, because the shared `_US_WORD_RE` matches
    "usa" and "united states" but not a lone "US". Major cities also routinely
    appear with no state ("San Francisco", "Seattle"), which lenient gating
    kept by default and strict gating would otherwise discard.
    """
    from services import guest_service as g

    for location in ("US", "U.S.", "Remote, US", "San Francisco", "Seattle",
                     "New York City", "Palo Alto", "Bellevue", "Tempe"):
        assert g.is_us_confident({"location": location, "country_hint": ""}), location


def test_a_multi_location_posting_including_the_us_is_kept():
    """Boards concatenate locations ("Remote, Canada; Remote, United States").
    A role open in the US is a US role even when it is also open elsewhere."""
    from services import guest_service as g

    assert g.is_us_confident(
        {"location": "Remote, Canada; Remote, United States", "country_hint": ""})
    assert not g.is_us_confident(
        {"location": "Remote, Canada; Remote, Brazil", "country_hint": ""})


def test_ambiguous_city_names_are_not_assumed_us():
    """Cambridge, Manchester and Richmond each name a well-known non-US city;
    a wrong keep is worse than a missed keep on the guest path."""
    from services import guest_service as g

    for location in ("Cambridge", "Manchester", "Birmingham", "Richmond"):
        assert not g.is_us_confident({"location": location, "country_hint": ""}), location


def test_us_state_names_survive_the_foreign_country_veto():
    """`_FOREIGN_COUNTRY_RE` matches "mexico" inside "New Mexico", which would
    veto an entire US state. State names are masked before the veto runs."""
    from services import guest_service as g

    assert g.is_us_confident({"location": "New Mexico", "country_hint": ""})
    assert g.is_us_confident({"location": "Albuquerque, New Mexico", "country_hint": ""})
    assert not g.is_us_confident({"location": "Mexico City", "country_hint": ""})
    assert not g.is_us_confident({"location": "Remote, Mexico", "country_hint": ""})


def test_a_us_city_beside_a_foreign_country_is_not_enough():
    """"Dublin, Ireland (Mountain View)" is an Irish posting that names a US
    office. Only COUNTRY-level US evidence outranks the foreign veto."""
    from services import guest_service as g

    assert not g.is_us_confident(
        {"location": "Dublin, Ireland (Mountain View)", "country_hint": ""})
    assert g.is_us_confident(
        {"location": "Remote, Canada; Remote, United States", "country_hint": ""})
