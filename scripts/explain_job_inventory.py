"""Prove the job-inventory indexes with real explain("executionStats").

An index spec is a claim. This script is the evidence: it runs every critical
access path in `shared/python/services/job_inventory_service.py` against a LIVE
cluster and prints the winning stage, nReturned, totalKeysExamined,
totalDocsExamined and whether the plan is IXSCAN or COLLSCAN.

Safety posture (read this before running it against prod):
  * Nothing in `job_postings` / `job_source_state` / `job_ingestion_runs` is
    written, updated or deleted. Those are explained READ-ONLY, exactly as they
    stand.
  * An empty or missing collection yields an EOF plan that proves nothing, so
    the same queries are ALSO run against a clearly-named scratch namespace
    (`_scratch_explain_job_postings` / `_scratch_explain_job_source_state`)
    seeded with a small synthetic set. Every seeded _id is recorded and deleted
    in a `finally` block, so the script cleans up even on failure.
  * No collection is dropped and no existing document is touched. The scratch
    namespaces are left in place (empty) rather than dropped, per the operating
    rule; drop them by hand if you want them gone.

Usage:
    python scripts/explain_job_inventory.py              # explain live + scratch
    python scripts/explain_job_inventory.py --no-scratch # live collections only
    python scripts/explain_job_inventory.py --seed 2000  # bigger scratch corpus
"""
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Mirror local.py path wiring so `utils.*` / `services.*` resolve.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "shared" / "python"))
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

from pymongo import ASCENDING, DESCENDING  # noqa: E402
from pymongo.errors import PyMongoError  # noqa: E402
from utils.db_connect import DBConnect  # noqa: E402

SCRATCH_POSTINGS = "_scratch_explain_job_postings"
SCRATCH_STATE = "_scratch_explain_job_source_state"

NOW = datetime.now(timezone.utc)

ROLE_FAMILIES = ["backend", "frontend", "data", "ai_ml", "devops", "design", "swe", "other"]
LOCATION_KEYS = ["austin-tx-usa", "new-york-ny-usa", "london-uk", "remote-usa"]
SOURCES = ["workday", "greenhouse", "lever", "ashby"]

# The indexes under test - identical keys/options to the job_postings and
# job_source_state specs in scripts/migrate_indexes.py.
SCRATCH_POSTING_INDEXES = [
    ([("source", ASCENDING), ("source_job_id", ASCENDING)],
     {"unique": True, "name": "source_job_id_unique"}),
    ([("job_id", ASCENDING)], {"unique": True, "name": "job_id_unique"}),
    ([("status", ASCENDING), ("role_family", ASCENDING),
      ("cost_class", ASCENDING), ("posted_at", DESCENDING)],
     {"name": "active_role_cost_posted_at"}),
    ([("status", ASCENDING), ("cost_class", ASCENDING), ("posted_at", DESCENDING)],
     {"name": "active_cost_posted_at"}),
    ([("status", ASCENDING), ("locations.key", ASCENDING), ("posted_at", DESCENDING)],
     {"name": "active_location_posted_at"}),
    ([("status", ASCENDING), ("source", ASCENDING), ("last_verified_at", ASCENDING)],
     {"name": "status_source_last_verified"}),
    ([("content_hash", ASCENDING), ("normalization_version", ASCENDING)],
     {"name": "content_hash_norm_version"}),
    ([("purge_at", ASCENDING)],
     {"name": "purge_at_retention",
      "partialFilterExpression": {"purge_at": {"$exists": True}}}),
    ([("employer_id", ASCENDING), ("status", ASCENDING)], {"name": "employer_id_status"}),
    # The tech-feed paths. `tech.*` is multikey; the keyset index is what makes
    # paging PAST the ranked window an IXSCAN instead of a blocking sort, and
    # `locations.country_code` is what keeps the (previously broken) country
    # filter off a COLLSCAN.
    ([("status", ASCENDING), ("tech.tech_scope", ASCENDING),
      ("posted_at", DESCENDING)], {"name": "active_tech_scope_posted_at"}),
    ([("status", ASCENDING), ("tech.role_families", ASCENDING),
      ("posted_at", DESCENDING)], {"name": "active_tech_family_posted_at"}),
    ([("status", ASCENDING), ("tech.role_specialties", ASCENDING),
      ("posted_at", DESCENDING)], {"name": "active_tech_specialty_posted_at"}),
    ([("status", ASCENDING), ("posted_at", DESCENDING), ("job_id", ASCENDING)],
     {"name": "active_keyset_posted_job"}),
    ([("status", ASCENDING), ("locations.country_code", ASCENDING),
      ("posted_at", DESCENDING)], {"name": "active_country_posted_at"}),
    ([("status", ASCENDING), ("tech.tech_scope", ASCENDING),
      ("posted_at", DESCENDING), ("job_id", ASCENDING)],
     {"name": "active_tech_scope_keyset"}),
    ([("status", ASCENDING), ("tech.role_families", ASCENDING),
      ("posted_at", DESCENDING), ("job_id", ASCENDING)],
     {"name": "active_tech_family_keyset"}),
    ([("status", ASCENDING), ("tech.role_specialties", ASCENDING),
      ("posted_at", DESCENDING), ("job_id", ASCENDING)],
     {"name": "active_tech_specialty_keyset"}),
]

SCRATCH_STATE_INDEXES = [
    ([("source", ASCENDING), ("shard", ASCENDING)],
     {"unique": True, "name": "source_shard_unique"}),
    ([("source", ASCENDING), ("next_attempt_at", ASCENDING)],
     {"name": "source_next_attempt_at"}),
    ([("lease_expiry", ASCENDING)], {"name": "lease_expiry_idx"}),
]


# ---------------------------------------------------------------------------
# Access paths under test. Each entry names the service function it mirrors.
# ---------------------------------------------------------------------------

def posting_paths(content_hash):
    cutoff = NOW - timedelta(days=7)
    return [
        # REQ-000044 is a workday doc in the scratch corpus (i % 4 == 0).
        ("upsert_posting / get_posting  (identity)",
         {"source": "workday", "source_job_id": "REQ-000044"}, None, 0),
        ("get_by_job_id / resolve_jd",
         {"job_id": "0" * 32}, None, 0),
        ("find_active_fresh(role_family, guest: cost_class=free)",
         {"status": "active", "role_family": "backend", "cost_class": "free",
          "posted_at": {"$gte": cutoff}}, [("posted_at", DESCENDING)], 60),
        ("find_active_fresh(role_family, authed: cost_class $in)",
         {"status": "active", "role_family": "backend",
          "cost_class": {"$in": ["free", "paid"]},
          "posted_at": {"$gte": cutoff}}, [("posted_at", DESCENDING)], 60),
        ("find_active_fresh(no role filter)",
         {"status": "active", "cost_class": "free", "posted_at": {"$gte": cutoff}},
         [("posted_at", DESCENDING)], 60),
        ("find_active_fresh(location_key)  [multikey]",
         {"status": "active", "locations.key": "austin-tx-usa",
          "posted_at": {"$gte": cutoff}}, [("posted_at", DESCENDING)], 60),
        ("find_stale_for_verification (maintenance)",
         {"status": "active", "source": "workday",
          "last_verified_at": {"$lt": NOW - timedelta(days=1)}},
         [("last_verified_at", ASCENDING)], 200),
        ("find_by_content_hash (parse-once)",
         {"content_hash": content_hash}, None, 25),
        ("find_parsed_requirements (parse-once, versioned)",
         {"content_hash": content_hash, "normalization_version": 1,
          "requirements.extractor_version": 1}, None, 1),
        ("purge_expired (retention)",
         {"purge_at": {"$lte": NOW}, "status": {"$in": ["closed", "expired"]}}, None, 0),
        ("close_unseen (closure sweep)",
         {"source": "workday", "status": "active",
          "last_seen_at": {"$lt": NOW - timedelta(days=1)}}, None, 5000),
        ("employer roll-up",
         {"employer_id": "employer-7", "status": "active"}, None, 0),

        # --- tech-feed retrieval, the finalized paths ----------------------
        ("tech-feed retrieve (default scope, ranked window)",
         {"status": "active", "tech.tech_scope": {"$in": ["core"]},
          "posted_at": {"$gte": cutoff}},
         [("posted_at", DESCENDING), ("job_id", ASCENDING)], 2000),
        ("tech-feed retrieve (family filter)  [multikey]",
         {"status": "active", "tech.role_families": {"$in": ["software_engineering"]},
          "posted_at": {"$gte": cutoff}},
         [("posted_at", DESCENDING), ("job_id", ASCENDING)], 2000),
        ("tech-feed retrieve (specialty filter)  [multikey]",
         {"status": "active", "tech.role_specialties": {"$in": ["backend"]},
          "posted_at": {"$gte": cutoff}},
         [("posted_at", DESCENDING), ("job_id", ASCENDING)], 2000),
        # THE path that removed the 2,000-result ceiling. A blocking sort here
        # would mean the keyset phase re-sorts the whole match set per page,
        # which is the opposite of what a keyset cursor is for.
        ("tech-feed keyset continuation (past the ranked window)",
         {"$and": [{"status": "active", "posted_at": {"$gte": cutoff}},
                   {"$and": [
                       {"posted_at": {"$lte": NOW - timedelta(days=3)}},
                       {"$or": [{"posted_at": {"$lt": NOW - timedelta(days=3)}},
                                {"posted_at": NOW - timedelta(days=3),
                                 "job_id": {"$gt": "0" * 32}}]}]}]},
         [("posted_at", DESCENDING), ("job_id", ASCENDING)], 250),
        ("tech-feed keyset continuation WITHOUT the range bound (regression)",
         {"$and": [{"status": "active", "posted_at": {"$gte": cutoff}},
                   {"$or": [{"posted_at": {"$lt": NOW - timedelta(days=3)}},
                            {"posted_at": NOW - timedelta(days=3),
                             "job_id": {"$gt": "0" * 32}}]}]},
         [("posted_at", DESCENDING), ("job_id", ASCENDING)], 250),
        ("tech-feed keyset continuation (undated tail)",
         {"$and": [{"status": "active"},
                   {"posted_at": None, "job_id": {"$gt": "0" * 32}}]},
         [("posted_at", DESCENDING), ("job_id", ASCENDING)], 250),
        ("tech-feed country filter (normalised ISO code)",
         {"status": "active", "locations.country_code": {"$in": ["US"]},
          "posted_at": {"$gte": cutoff}},
         [("posted_at", DESCENDING)], 2000),
        # The four clauses that were fixed after the live filter audit. Each
        # one was accepted, echoed and inert; each is now a real predicate, so
        # each needs a plan that a regression would show up in.
        ("tech-feed country filter (raw spelling fallback)",
         {"status": "active", "posted_at": {"$gte": cutoff},
          "$and": [{"$or": [{"locations.country_code": {"$in": ["IN"]}},
                            {"locations.country": {"$in": ["india", "India", "INDIA"]}}]}]},
         [("posted_at", DESCENDING)], 2000),
        ("tech-feed region filter",
         {"status": "active", "posted_at": {"$gte": cutoff},
          "$and": [{"locations.region": {"$in": ["TX", "Tx", "tx"]}}]},
         [("posted_at", DESCENDING)], 2000),
        ("tech-feed employment-type filter (normalised, unknown ORed in)",
         {"status": "active", "posted_at": {"$gte": cutoff},
          "$and": [{"$or": [{"employment_type_normalized": {"$in": ["contract"]}},
                            {"employment_type_normalized": {"$exists": False}}]}]},
         [("posted_at", DESCENDING)], 2000),
        ("tech-feed pay-interval filter (no compensation ORed in)",
         {"status": "active", "posted_at": {"$gte": cutoff},
          "$and": [{"$or": [{"compensation.period": "year"},
                            {"compensation": {"$exists": False}}]}]},
         [("posted_at", DESCENDING)], 2000),
        ("tech-feed page fetch by id (cursor page)",
         {"job_id": {"$in": ["0" * 32, "1" * 32, "2" * 32]}}, None, 25),
    ]


STATE_PATHS = [
    ("acquire_lease / advance_watermark (shard identity)",
     {"source": "workday", "shard": "shard-004"}, None, 0),
    ("due_shards(source)",
     {"source": "workday",
      "$and": [
          {"$or": [{"lease_owner": None}, {"lease_expiry": None},
                   {"lease_expiry": {"$lte": NOW}}]},
          {"$or": [{"next_attempt_at": None}, {"next_attempt_at": {"$lte": NOW}}]},
      ]}, None, 50),
    ("expired-lease sweep (cross-source)",
     {"lease_expiry": {"$lte": NOW}}, None, 50),
]


# ---------------------------------------------------------------------------
# Explain plumbing
# ---------------------------------------------------------------------------

def _winning_stages(stage, out=None):
    out = out if out is not None else []
    if not isinstance(stage, dict):
        return out
    name = stage.get("stage")
    if name:
        out.append(name)
    for child_key in ("inputStage", "innerStage", "outerStage"):
        if stage.get(child_key):
            _winning_stages(stage[child_key], out)
    for child in stage.get("inputStages", []) or []:
        _winning_stages(child, out)
    return out


def explain_one(coll, label, query, sort, limit):
    try:
        cursor = coll.find(query)
        if sort:
            cursor = cursor.sort(sort)
        if limit:
            cursor = cursor.limit(limit)
        plan = cursor.explain()
    except PyMongoError as e:
        print(f"  {label:<52} EXPLAIN FAILED: {e}")
        return

    ex = plan.get("executionStats", {}) or {}
    winning = (plan.get("queryPlanner", {}) or {}).get("winningPlan", {}) or {}
    # Mongo 7 nests the executed plan under queryPlan.
    stages = _winning_stages(winning.get("queryPlan") or winning)
    scan = "IXSCAN" if "IXSCAN" in stages else ("COLLSCAN" if "COLLSCAN" in stages
                                                else stages[-1] if stages else "?")
    index_name = None
    node = winning.get("queryPlan") or winning
    while isinstance(node, dict):
        if node.get("stage") == "IXSCAN":
            index_name = node.get("indexName")
            break
        node = node.get("inputStage")

    blocking = " +SORT(blocking)" if "SORT" in stages else ""
    print(f"  {label:<52} {scan:<9} idx={str(index_name):<30} "
          f"nReturned={ex.get('nReturned')} keys={ex.get('totalKeysExamined')} "
          f"docs={ex.get('totalDocsExamined')} ms={ex.get('executionTimeMillis')}"
          f"{blocking}")
    print(f"      stages: {' <- '.join(stages) or 'n/a'}")


def explain_collection(db, name, paths, header):
    print(f"\n== {header} ({db.name}.{name}, {db[name].estimated_document_count()} docs) ==")
    idx = sorted(db[name].index_information().keys())
    print(f"   indexes present: {', '.join(idx) if idx else 'NONE'}")
    for label, query, sort, limit in paths:
        explain_one(db[name], label, query, sort, limit)


# ---------------------------------------------------------------------------
# Scratch corpus (seeded, then deleted by _id in a finally block)
# ---------------------------------------------------------------------------

def seed_scratch(db, n):
    postings, states = [], []
    content_hash = "c" * 64
    for i in range(n):
        # Spread ages across ~12 days so the 7-day freshness window keeps a real
        # majority, and vary the moduli so role/cost/age do not correlate (a
        # corpus where the filters collapse to zero rows proves nothing).
        posted = NOW - timedelta(days=i % 12, hours=i % 23)
        closed = (i % 17 == 0)
        # `country_code` is the field the country filter reads. Vary it so the
        # explain is against a corpus where the filter selects rather than
        # matching everything - a selective-looking plan over a corpus with one
        # value proves nothing.
        country = ("US", "US", "IN", "DE", "CA")[i % 5]
        locs = [{"raw": "Austin, TX, USA", "country_code": country,
                 "key": LOCATION_KEYS[i % len(LOCATION_KEYS)]}]
        if i % 3 == 0:  # genuinely multikey: some postings list several offices
            locs.append({"raw": "Remote, USA", "country_code": "US",
                         "key": LOCATION_KEYS[(i + 1) % len(LOCATION_KEYS)]})
        doc = {
            "_id": f"scratch-{i}",
            "job_id": "0" * 32 if i == 0 else uuid.uuid4().hex,
            "source": SOURCES[i % len(SOURCES)],
            "source_job_id": f"REQ-{i:06d}",
            "employer_id": f"employer-{i % 250}",
            "employer_name": f"Employer {i % 250}",
            "title_raw": "Senior Backend Engineer",
            "title_normalized": "senior backend engineer",
            "role_family": ROLE_FAMILIES[i % len(ROLE_FAMILIES)],
            # The tech-feed reads `tech.*`, not the legacy `role_family`.
            "tech": {
                "tech_scope": ("core", "core", "adjacent", "non_tech",
                               "unknown")[i % 5],
                "role_families": [("software_engineering", "cloud_platform",
                                   "data", "ai_ml", "security")[i % 5]],
                "role_specialties": [("backend", "sre", "data_engineering",
                                      "ml_engineering", "secops")[i % 5]],
                "taxonomy_version": "tech-tax-1.1.0",
            },
            "seniority": ("senior", "mid", "entry")[i % 3],
            "locations": locs,
            "work_mode": ("remote", "hybrid", "onsite")[i % 3],
            "employment_type": "full-time",
            "description_text": "Scratch explain corpus. " * 20,
            "posted_at": posted,
            "posted_at_confidence": "exact",
            "first_seen_at": posted,
            "last_seen_at": NOW - timedelta(hours=i % 60),
            "last_verified_at": NOW - timedelta(hours=i % 60),
            "status": "closed" if closed else "active",
            "active": not closed,
            "cost_class": "paid" if i % 7 == 0 else "free",
            "source_quality": "direct",
            "content_hash": content_hash if i % 100 == 0 else uuid.uuid4().hex,
            "normalization_version": 1,
            "scoring_version": 1,
            "requirements": ({"extractor_version": 1,
                              "source_content_hash": content_hash,
                              "required_skills": ["python"]} if i % 100 == 0 else None),
        }
        if closed:
            doc["purge_at"] = NOW + timedelta(days=30 - (i % 40))
        postings.append(doc)

    for i in range(64):
        states.append({
            "_id": f"scratch-state-{i}",
            "source": SOURCES[i % len(SOURCES)],
            "shard": f"shard-{i:03d}",
            "watermark": NOW - timedelta(hours=i),
            "cursor": f"page-{i}",
            "lease_owner": None if i % 3 == 0 else f"worker-{i}",
            # Live leases, expired leases (crashed worker) and free shards.
            "lease_expiry": (None if i % 3 == 0
                             else (NOW - timedelta(minutes=5) if i % 3 == 1
                                   else NOW + timedelta(minutes=15))),
            "retry_count": i % 4,
            # A mix of "due now" (None / past) and "still backing off" (future).
            "next_attempt_at": (None if i % 3 == 0
                                else (NOW - timedelta(minutes=i) if i % 3 == 1
                                      else NOW + timedelta(minutes=i))),
        })

    db[SCRATCH_POSTINGS].insert_many(postings)
    db[SCRATCH_STATE].insert_many(states)
    for keys, opts in SCRATCH_POSTING_INDEXES:
        db[SCRATCH_POSTINGS].create_index(keys, **opts)
    for keys, opts in SCRATCH_STATE_INDEXES:
        db[SCRATCH_STATE].create_index(keys, **opts)
    return ([d["_id"] for d in postings], [d["_id"] for d in states], content_hash)


def main():
    no_scratch = "--no-scratch" in sys.argv
    seed_n = 1000
    if "--seed" in sys.argv:
        seed_n = int(sys.argv[sys.argv.index("--seed") + 1])

    db = DBConnect().get_db()
    print(f"== explain_job_inventory on '{db.name}' (LIVE, read-only on real "
          f"collections) ==")

    live_hash = "c" * 64
    sample = db.job_postings.find_one({}, {"content_hash": 1})
    if sample and sample.get("content_hash"):
        live_hash = sample["content_hash"]

    explain_collection(db, "job_postings", posting_paths(live_hash),
                       "LIVE job_postings (no writes)")
    explain_collection(db, "job_source_state", STATE_PATHS,
                       "LIVE job_source_state (no writes)")

    if no_scratch:
        print("\n(--no-scratch: skipped seeded verification)")
        return

    posting_ids = state_ids = []
    try:
        print(f"\n-- seeding {seed_n} scratch postings + 64 scratch shard states --")
        posting_ids, state_ids, content_hash = seed_scratch(db, seed_n)
        explain_collection(db, SCRATCH_POSTINGS, posting_paths(content_hash),
                           "SCRATCH job_postings (seeded)")
        explain_collection(db, SCRATCH_STATE, STATE_PATHS,
                           "SCRATCH job_source_state (seeded)")
    finally:
        # Delete EXACTLY what was inserted, by _id. Collections are not dropped.
        d1 = db[SCRATCH_POSTINGS].delete_many({"_id": {"$in": list(posting_ids)}})
        d2 = db[SCRATCH_STATE].delete_many({"_id": {"$in": list(state_ids)}})
        print(f"\n-- cleanup: deleted {d1.deleted_count} scratch postings, "
              f"{d2.deleted_count} scratch states "
              f"(remaining: {db[SCRATCH_POSTINGS].estimated_document_count()}, "
              f"{db[SCRATCH_STATE].estimated_document_count()}) --")
        print("   scratch collections left in place (empty) - not dropped by design.")


if __name__ == "__main__":
    main()
