"""Production-readiness MongoDB index migration for JobPilot (`aspirely_db`).

The codebase defines almost no indexes in-repo, so virtually every recurring
filter/sort is a COLLSCAN — the #1 latent perf risk flagged by the audit. This
ONE idempotent script creates an index for every confirmed hot access pattern
(plus the new connect-OAuth / per-provider collections added this session, incl.
a TTL on the short-lived CSRF-state collection).

Each spec below was derived by READING the query that uses it; the call site is
cited in the comment next to the spec. We only index fields actually used in
find/sort, only set `unique=True` where the calling code guarantees uniqueness
(UUID keys or upsert-on-key), and only set a TTL where a doc carries a real
expiry timestamp it should be reaped at.

Safety / idempotency:
  * `create_index` is idempotent BY NAME — re-running with the same name + keys +
    options is a no-op. Every spec passes an explicit `name=` and `background=True`.
  * Each creation is wrapped in try/except and logged as created / exists / FAILED;
    one failure never aborts the rest.
  * `--dry-run` logs exactly what it WOULD ensure, touching nothing.
  * Some indexes are ALSO created opportunistically by app code at runtime
    (`scripts/migrate_users_v2.py`, `services/job_service.py:241-243`). We re-assert
    them here with matching keys/options so a fresh DB is fully covered up front;
    a matching re-assert is a no-op, a conflicting one is caught and logged.

Requires a LIVE MongoDB connection (singleton `utils.db_connect.DBConnect`).

Usage:
    python scripts/migrate_indexes.py            # apply (idempotent, safe to re-run)
    python scripts/migrate_indexes.py --dry-run  # preview only, no writes
"""
import sys
from collections import defaultdict
from pathlib import Path

# Mirror local.py path wiring so `utils.*` resolve from the shared layer.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "shared" / "python"))
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

from pymongo import ASCENDING, DESCENDING  # noqa: E402
from pymongo.errors import OperationFailure, PyMongoError  # noqa: E402
from utils.db_connect import DBConnect  # noqa: E402


# ---------------------------------------------------------------------------
# Index specs: (collection, keys, options)
#   options always carries `name`; `background` is injected below.
#   The trailing comment cites the query that justifies the index.
# ---------------------------------------------------------------------------
INDEX_SPECS = [
    # === users (identity = email; OAuth subject lookups) ===================
    # oauth_service.upsert_user: users.find_one({"email": email}); usage_service,
    # overview_service, tech_chronicle all key on email. (also in migrate_users_v2)
    ("users", [("email", ASCENDING)], {"unique": True, "name": "email_unique"}),
    # oauth_service.upsert_user:329-338 find_one({f"oauth_identities.{p}.subject"}).
    # Sparse: only users with that provider linked carry the field.
    ("users", [("oauth_identities.google.subject", ASCENDING)],
     {"sparse": True, "name": "oauth_google_subject"}),
    ("users", [("oauth_identities.github.subject", ASCENDING)],
     {"sparse": True, "name": "oauth_github_subject"}),
    ("users", [("oauth_identities.linkedin.subject", ASCENDING)],
     {"sparse": True, "name": "oauth_linkedin_subject"}),
    ("users", [("oauth_identities.microsoft.subject", ASCENDING)],
     {"sparse": True, "name": "oauth_microsoft_subject"}),
    ("users", [("oauth_identities.apple.subject", ASCENDING)],
     {"sparse": True, "name": "oauth_apple_subject"}),

    # === resume_assets (Resume Platform v3 §1.1 — canonical resume store) ==
    # One doc per uploaded file; resume_id is the immutable uuid4-hex key
    # (resume_access.get_asset / save_parsed_fields filter {resume_id}).
    ("resume_assets", [("resume_id", ASCENDING)],
     {"unique": True, "name": "resume_id_unique"}),
    # resume_access.get_active_structured fallback + /versions listing:
    # find({user_email, ...}) sort created_at/parsed_at desc.
    ("resume_assets", [("user_email", ASCENDING), ("created_at", DESCENDING)],
     {"name": "user_email_created_at"}),
    # Account erasure walks the {user_id}-owned assets.
    ("resume_assets", [("user_id", ASCENDING)],
     {"sparse": True, "name": "user_id_sparse"}),
    # Guest-owned assets (attach_parsed_resume / retention).
    ("resume_assets", [("guest_id", ASCENDING)],
     {"sparse": True, "name": "guest_id_sparse"}),
    # Upload dedup: resume.py upload route find({user_email, content_hash,
    # parse_status:'parsed'}).
    ("resume_assets", [("content_hash", ASCENDING), ("user_email", ASCENDING)],
     {"name": "content_hash_user_email"}),
    # Guest retention TTL: only guest-owned assets carry a real purge_after;
    # partialFilterExpression keeps user assets exempt (spec 1.1).
    ("resume_assets", [("purge_after", ASCENDING)],
     {"expireAfterSeconds": 0, "name": "ttl_purge_after",
      "partialFilterExpression": {"owner_type": "guest"}}),

    # === user_resumes (LEGACY reader seam) ================================
    # v3 moved canonical storage to resume_assets; a handful of Phase B
    # matching readers (daily_pipeline_service, jd_match_scorer, job_service)
    # still query {user_email, structured:exists} sort parsed_at — keep this
    # index until those readers move to resume_access.
    ("user_resumes", [("user_email", ASCENDING), ("parsed_at", DESCENDING)],
     {"name": "user_email_parsed_at"}),

    # === resume_jobs (async-job polling backbone) =========================
    # resume_service: insert/find/update_one({job_id}); get_job adds user_email scope.
    # job_id is a uuid4 → unique is safe and protects polling correctness.
    ("resume_jobs", [("job_id", ASCENDING)], {"unique": True, "name": "job_id_unique"}),
    ("resume_jobs", [("user_email", ASCENDING)], {"name": "user_email_idx"}),
    # v3 §1.3: settled jobs (complete_job/fail_job) stamp expire_at = now+7d;
    # TTL reaps them. In-flight jobs carry no expire_at and are untouched.
    ("resume_jobs", [("expire_at", ASCENDING)],
     {"expireAfterSeconds": 0, "name": "ttl_expire_at"}),
    # v3 §1.3: optional caller-supplied dedup key (create_job) — unique sparse
    # so client retries can't double-dispatch.
    ("resume_jobs", [("idempotency_key", ASCENDING)],
     {"unique": True, "sparse": True, "name": "idempotency_key_unique"}),

    # === usage counters (atomic $inc upserts) =============================
    # usage_service:65 tailor_usage.find_one({email, date}); :90 $inc upsert on it.
    ("tailor_usage", [("email", ASCENDING), ("date", ASCENDING)],
     {"unique": True, "name": "email_date_unique"}),
    # === quota ledger (Resume Platform v3 §1.6 — atomic tailor quota) ======
    # quota_counters needs NO extra index: its key is the implicit unique _id
    # ("tailor:{pool_key}:{date}") that reserve's find_one_and_update targets.
    # quota_ledger: entry_id is the settle key (complete_entry/refund_entry
    # find_one_and_update {entry_id, state}); uuid4 → unique.
    ("quota_ledger", [("entry_id", ASCENDING)],
     {"unique": True, "name": "entry_id_unique"}),
    # Caller-supplied dedup key (reserve_tailor idempotency) — unique sparse
    # so a client retry can't double-reserve.
    ("quota_ledger", [("idempotency_key", ASCENDING)],
     {"unique": True, "sparse": True, "name": "idempotency_key_unique"}),
    # Per-actor daily reads: entries_count_for_users filters
    # {user_email $in, date, state $in}.
    ("quota_ledger", [("user_email", ASCENDING), ("date", ASCENDING)],
     {"name": "user_email_date"}),
    # feature_usage real doc shape is {user_email, date, feature} — confirmed at
    # entitlements_service.py:241 (find_one) and :273 ($inc upsert). Matches the
    # existing migrate_users_v2 index name so this is a clean no-op re-assert.
    ("feature_usage", [("user_email", ASCENDING), ("date", ASCENDING), ("feature", ASCENDING)],
     {"unique": True, "name": "user_date_feature_unique"}),

    # === saved_jobs =======================================================
    # job_service:241 already creates saved_jobs.job_id unique (dedup). The HOT
    # READ path is {user_email}: overview_service:57/98, recommendations:63/128,
    # beta_features:232, daily_pipeline:146 — none indexed. Add it.
    ("saved_jobs", [("user_email", ASCENDING)], {"name": "user_email_idx"}),
    # Re-assert the dedup index with PyMongo's default name so it matches the
    # runtime-created one (job_service.create_index("job_id", unique=True)).
    ("saved_jobs", [("job_id", ASCENDING)], {"unique": True, "name": "job_id_1"}),

    # === tailoring_records (application history) ===========================
    # record_id is a uuid4, queried as {record_id}/{record_id,user_email}
    # (resume.py:1611, admin.py:292, job_service:2100); list path is
    # {user_email} sort updated_at/created_at desc (overview:37, admin:210/308).
    ("tailoring_records", [("record_id", ASCENDING)], {"unique": True, "name": "record_id_unique"}),
    ("tailoring_records", [("user_email", ASCENDING), ("updated_at", DESCENDING)],
     {"name": "user_email_updated_at"}),

    # === learning ==========================================================
    # learning_service.count_active_paths:164 / overview:63,72 find({user_email,status}).
    ("learning_paths", [("user_email", ASCENDING), ("status", ASCENDING)],
     {"name": "user_email_status"}),
    # learning_service.get_path:186 find_one({user_email, path_id}).
    ("learning_paths", [("user_email", ASCENDING), ("path_id", ASCENDING)],
     {"name": "user_email_path_id"}),
    # learning_service:139 update_one({user_email, path_id}, upsert) — one doc per
    # (user, path) → unique is safe and dedups progress rows.
    ("learning_progress", [("user_email", ASCENDING), ("path_id", ASCENDING)],
     {"unique": True, "name": "user_email_path_id_unique"}),

    # === interview_attempts ===============================================
    # interview_coach_service:87/137/188 find({user_email}) sort answered_at desc.
    ("interview_attempts", [("user_email", ASCENDING), ("answered_at", DESCENDING)],
     {"name": "user_email_answered_at"}),

    # === events + per-user relations ======================================
    # event_scraper:227 update_one({dedup_key}, upsert) — dedup key is unique.
    ("events", [("dedup_key", ASCENDING)], {"unique": True, "name": "dedup_key_unique"}),
    # event_scraper.get_event:291 / set_relation guard:308 find_one({event_id}).
    ("events", [("event_id", ASCENDING)], {"name": "event_id_idx"}),
    # event_scraper.list_events:273-278 find({status, legitimacy_band}) sort
    # legitimacy_score desc; recommendations:153 same shape. equality->equality->sort.
    ("events", [("status", ASCENDING), ("legitimacy_band", ASCENDING),
                ("legitimacy_score", DESCENDING)],
     {"name": "status_band_score"}),
    # event_scraper:225 sets expires_at (~120d out). TTL reaps stale events.
    ("events", [("expires_at", ASCENDING)], {"expireAfterSeconds": 0, "name": "ttl_expires"}),
    # event_scraper.set_relation:310 update_one({user_email, event_id}, upsert);
    # get_relations:280 find({user_email}); :295 find_one({user_email, event_id}).
    ("user_events", [("user_email", ASCENDING), ("event_id", ASCENDING)],
     {"unique": True, "name": "user_email_event_id_unique"}),

    # === apply (extension) =================================================
    # apply.py:92/128 application_profiles find_one/update_one({user_email}) — one per user.
    ("application_profiles", [("user_email", ASCENDING)],
     {"unique": True, "name": "user_email_unique"}),
    # apply.py:200 insert {user_email,...}; :231-243 find({user_email}[+company]) sort created_at desc.
    ("application_answers", [("user_email", ASCENDING), ("created_at", DESCENDING)],
     {"name": "user_email_created_at"}),

    # === opt-in auto-submit (guardrailed apply) ============================
    # auto_submit_service: settings upsert/find {user_email} — one doc per user.
    ("auto_submit_settings", [("user_email", ASCENDING)],
     {"unique": True, "name": "user_email_unique"}),
    # approval_id is a uuid4, queried on transitions; unique protects single-use.
    ("auto_submit_approvals", [("approval_id", ASCENDING)],
     {"unique": True, "name": "approval_id_unique"}),
    # get_active_approval / cancel / mark_submitted / list_pending_eligible all
    # filter {user_email, application_id, status} (equality->equality->equality).
    ("auto_submit_approvals",
     [("user_email", ASCENDING), ("application_id", ASCENDING), ("status", ASCENDING)],
     {"name": "user_app_status"}),
    # Partial-unique DEDUP guard: at most ONE live (approved/pending) approval per
    # (user, application) — the real backstop for idempotent re-approve under
    # concurrent Lambdas (the service also check-then-inserts). Partial filter so
    # terminal records (submitted/cancelled/expired) don't collide.
    ("auto_submit_approvals",
     [("user_email", ASCENDING), ("application_id", ASCENDING)],
     {"unique": True, "name": "active_approval_unique",
      "partialFilterExpression": {"status": {"$in": ["approved", "pending"]}}}),
    # Audit log: per-user history sorted by time; plus per-application scan.
    ("auto_submit_audit", [("user_email", ASCENDING), ("at", DESCENDING)],
     {"name": "user_email_at"}),
    ("auto_submit_audit", [("application_id", ASCENDING), ("at", DESCENDING)],
     {"name": "application_id_at"}),

    # === auto-apply Tranche 1 ==============================================
    # applications ledger (Tranche 1 §1)
    ("applications", [("user_email", ASCENDING), ("job_identity", ASCENDING)], {"unique": True, "name": "user_email_job_identity_unique"}),
    ("applications", [("user_email", ASCENDING), ("status", ASCENDING)], {"name": "user_email_status"}),
    ("applications", [("posting_closes_at", ASCENDING)], {"sparse": True, "name": "posting_closes_at_sparse"}),
    ("applications", [("application_id", ASCENDING)], {"unique": True, "name": "application_id_unique"}),
    # approval tokens (Tranche 1 §2)
    ("approval_tokens", [("jti", ASCENDING)], {"unique": True, "name": "jti_unique"}),
    ("approval_tokens", [("exp", ASCENDING)], {"expireAfterSeconds": 0, "name": "ttl_exp"}),
    # ATS credential vault (Tranche 1 §4)
    ("ats_credentials", [("user_email", ASCENDING), ("ats_family", ASCENDING), ("tenant", ASCENDING)], {"unique": True, "name": "user_family_tenant_unique"}),
    ("ats_credential_audit", [("user_email", ASCENDING), ("at", DESCENDING)], {"name": "user_email_at"}),

    # === auto-apply Tranche 2/3 ============================================
    # cross-user trend signal
    ("skill_market_stats", [("role", ASCENDING), ("window", ASCENDING)], {"name": "role_window"}),
    ("skill_market_stats", [("role", ASCENDING), ("skill", ASCENDING), ("window", ASCENDING)], {"unique": True, "name": "role_skill_window_unique"}),
    # auto-apply engine
    ("auto_apply_answers", [("user_email", ASCENDING), ("application_id", ASCENDING)], {"unique": True, "name": "user_application_unique"}),
    ("auto_apply_freezes", [("user_email", ASCENDING), ("application_id", ASCENDING)], {"unique": True, "name": "user_application_unique"}),
    ("auto_apply_freezes", [("status", ASCENDING), ("next_check_at", ASCENDING)], {"name": "status_next_check"}),
    # self-growing answer library
    ("application_answer_library", [("user_email", ASCENDING), ("norm_key", ASCENDING)], {"unique": True, "name": "user_email_norm_key_unique"}),

    # === feedback ==========================================================
    # feedback.py:81 my_feedback find({email}) sort created_at desc (user field is `email`).
    ("feedback", [("email", ASCENDING), ("created_at", DESCENDING)],
     {"name": "email_created_at"}),
    # admin.py:229 find(query incl. status) sort created_at desc.
    ("feedback", [("status", ASCENDING), ("created_at", DESCENDING)],
     {"name": "status_created_at"}),

    # === gmail_connections ================================================
    # gmail_service:259 update_one({user_email}, upsert); :267 find_one({user_email}).
    ("gmail_connections", [("user_email", ASCENDING)],
     {"unique": True, "name": "user_email_unique"}),

    # === password reset / login state (time-bounded) ======================
    # auth.py:412 find_one({token_hash}); :377 insert with expires_at.
    # (also asserted in migrate_users_v2 — re-assert with matching names.)
    ("password_reset_tokens", [("token_hash", ASCENDING)],
     {"unique": True, "name": "token_hash_unique"}),
    ("password_reset_tokens", [("expires_at", ASCENDING)],
     {"expireAfterSeconds": 0, "name": "ttl_expires"}),
    # oauth_service:202 find_one_and_delete({state}); state docs carry expires_at.
    ("oauth_login_states", [("state", ASCENDING)], {"unique": True, "name": "state_unique"}),
    ("oauth_login_states", [("expires_at", ASCENDING)],
     {"expireAfterSeconds": 0, "name": "ttl_expires"}),

    # === tech_chronicle_cache =============================================
    # tech_chronicle.py:78 find_one({kind}); :93 update_one({kind}, upsert). Freshness
    # is enforced in-app via generated_at, so NO TTL here — just unique on kind.
    ("tech_chronicle_cache", [("kind", ASCENDING)], {"unique": True, "name": "kind_unique"}),

    # === NEW THIS SESSION: connect-OAuth + per-provider snapshots ==========
    # github/linkedin connect:116 insert {state, provider, expires_at};
    # :137 find_one_and_delete({state, provider}). TTL reaps un-consumed states.
    ("connect_oauth_states", [("state", ASCENDING), ("provider", ASCENDING)],
     {"name": "state_provider"}),
    ("connect_oauth_states", [("expires_at", ASCENDING)],
     {"expireAfterSeconds": 0, "name": "ttl_expires"}),
    # *_connect_service.handle_callback:197 update_one({user_email}, upsert);
    # get_status:212 find_one({user_email}) — one connection per user.
    ("github_connections", [("user_email", ASCENDING)],
     {"unique": True, "name": "user_email_unique"}),
    ("linkedin_connections", [("user_email", ASCENDING)],
     {"unique": True, "name": "user_email_unique"}),
    # github_profile_service:166 update_one({user_email}, upsert); :175/187 find_one({user_email}).
    ("github_profiles", [("user_email", ASCENDING)],
     {"unique": True, "name": "user_email_unique"}),

    # === Voice Graph (addendum §6.12) =====================================
    # writing_samples: samples.list find({user_email}) sort updated_at desc;
    # get/save/delete filter {sample_id, user_email}. sample_id is a uuid4.
    ("writing_samples", [("user_email", ASCENDING), ("updated_at", DESCENDING)],
     {"name": "user_email_updated_at"}),
    ("writing_samples", [("sample_id", ASCENDING)], {"unique": True, "name": "sample_id_unique"}),
    # consents: per-scope ledger, upsert/find {user_email, scope} — one row per pair.
    ("consents", [("user_email", ASCENDING), ("scope", ASCENDING)],
     {"unique": True, "name": "user_email_scope_unique"}),
    # graph_nodes / graph_edges / graph_evidence (populated in VG Step 2) — index
    # now so per-user traversal never COLLSCANs.
    ("graph_nodes", [("user_email", ASCENDING)], {"name": "user_email_idx"}),
    ("graph_nodes", [("node_id", ASCENDING)], {"unique": True, "name": "node_id_unique"}),
    ("graph_edges", [("user_email", ASCENDING)], {"name": "user_email_idx"}),
    ("graph_edges", [("edge_id", ASCENDING)], {"unique": True, "name": "edge_id_unique"}),
    ("graph_evidence", [("user_email", ASCENDING)], {"name": "user_email_idx"}),
    ("graph_evidence", [("target_type", ASCENDING), ("target_id", ASCENDING)],
     {"name": "target_idx"}),
    # style_profiles: one active profile per user (versioned in-doc).
    ("style_profiles", [("user_email", ASCENDING)], {"unique": True, "name": "user_email_unique"}),
    # authored_resumes: raw/authored resume versions with provenance.
    ("authored_resumes", [("user_email", ASCENDING), ("created_at", DESCENDING)],
     {"name": "user_email_created_at"}),
    ("authored_resumes", [("resume_id", ASCENDING)], {"unique": True, "name": "resume_id_unique"}),
    # merge_candidates: entity-resolution suggestions awaiting user confirmation.
    ("merge_candidates", [("user_email", ASCENDING), ("status", ASCENDING)],
     {"name": "user_email_status"}),
    ("merge_candidates", [("candidate_id", ASCENDING)], {"unique": True, "name": "candidate_id_unique"}),

    # === Sponsorship intel (§6.3) — upsert/lookup key is the normalized employer.
    ("sponsorship_signals", [("employer_norm", ASCENDING)],
     {"unique": True, "name": "employer_norm_unique"}),

    # === LinkedIn archive importer (§2.1) — one archive doc per user.
    ("linkedin_archives", [("user_email", ASCENDING)],
     {"unique": True, "name": "user_email_unique"}),

    # === Enrichment confirmations — one decisions doc per user.
    # profile_facts.record_decisions: update_one({user_email}, upsert);
    # _confirmed_enrichment_facts / list_candidate_facts: find_one({user_email}).
    ("enrichment_confirmations", [("user_email", ASCENDING)],
     {"unique": True, "name": "user_email_unique"}),

    # === Pipeline intel (§6.8) — ghosts() filters {user_email, application.status}.
    ("tailoring_records", [("user_email", ASCENDING), ("application.status", ASCENDING)],
     {"name": "user_email_app_status"}),

    # === Signal loop (§6.7) — bandit arms + post performance metrics.
    ("bandit_arms", [("arm_id", ASCENDING)], {"unique": True, "name": "arm_id_unique"}),
    ("post_metrics", [("post_id", ASCENDING), ("recorded_at", DESCENDING)],
     {"name": "post_id_recorded_at"}),

    # === Notifications (§2.5) — prefs, per-day cap log (unique is REQUIRED for the
    # atomic proactive-cap reserve), and the channel consent ledger.
    ("notification_prefs", [("user_email", ASCENDING)],
     {"unique": True, "name": "user_email_unique"}),
    ("notification_log", [("user_email", ASCENDING), ("day", ASCENDING)],
     {"unique": True, "name": "user_email_day_unique"}),
    ("notification_consents", [("user_email", ASCENDING), ("scope", ASCENDING)],
     {"unique": True, "name": "user_email_scope_unique"}),

    # === Mail-ingress Track A (§2.4) — unique addresses, dedup + 30d TTL purge.
    ("mail_ingress_addresses", [("user_email", ASCENDING)], {"unique": True, "name": "user_email_unique"}),
    ("mail_ingress_addresses", [("ingress_local", ASCENDING)], {"unique": True, "name": "ingress_local_unique"}),
    ("email_ingress", [("message_id", ASCENDING)], {"unique": True, "name": "message_id_unique"}),
    ("email_ingress", [("user_email", ASCENDING)], {"name": "by_user"}),
    ("email_ingress", [("purge_after", ASCENDING)], {"expireAfterSeconds": 0, "name": "ttl_purge_after"}),
    ("mail_ingress_suggestions", [("suggestion_id", ASCENDING)], {"unique": True, "name": "suggestion_id_unique"}),
    ("mail_ingress_suggestions", [("user_email", ASCENDING)], {"name": "by_user"}),

    # === Admin cockpit + Stripe billing bones (§6.9/§6.10).
    # stripe_events unique is a CORRECTNESS requirement (webhook idempotency).
    ("stripe_events", [("event_id", ASCENDING)], {"unique": True, "name": "event_id_unique"}),
    ("entitlement_overrides", [("user_email", ASCENDING)], {"unique": True, "name": "user_email_unique"}),
    ("audit_log", [("at", DESCENDING)], {"name": "at_desc"}),
    ("audit_log", [("target", ASCENDING), ("at", DESCENDING)], {"name": "target_at"}),
    ("eval_ratings", [("kind", ASCENDING), ("sample_id", ASCENDING)], {"name": "kind_sample"}),

    # === Workday Jobs / Career Pages live-search caches ====================
    # Both services ALSO create these lazily at runtime (workday_jobs_service
    # ._get_cache_collection / career_pages_service._get_cache_collection) —
    # re-asserted here with MATCHING names/options so a fresh DB is covered
    # up front and the runtime call is a no-op.
    # workday_jobs_service._read_cache/_write_cache: find_one/update_one on
    # {cache_key} (upsert key → unique). Runtime create_index("cache_key",
    # unique=True) yields PyMongo's default name "cache_key_1" — match it.
    ("workday_jobs_cache", [("cache_key", ASCENDING)],
     {"unique": True, "name": "cache_key_1"}),
    # TTL reaper on cached_at (CACHE_TTL_SECONDS = 6h); freshness horizon is
    # enforced in-app at 3h via the $gte cutoff in _read_cache.
    ("workday_jobs_cache", [("cached_at", ASCENDING)],
     {"expireAfterSeconds": 6 * 3600, "name": "workday_jobs_cache_ttl"}),
    # career_pages_service._read_cache: find_one({cache_key}); upsert on it.
    # Runtime creates it unnamed → default "cache_key_1"; not unique in the
    # legacy service, kept identical here.
    ("career_pages_cache", [("cache_key", ASCENDING)], {"name": "cache_key_1"}),
    ("career_pages_cache", [("cached_at", ASCENDING)],
     {"expireAfterSeconds": 6 * 3600, "name": "career_pages_cache_ttl"}),
    # mnc_jobs_service._get_cache_collection creates these at runtime with the
    # same names; re-asserted here for fresh DBs (6h TTL / 3h in-app freshness).
    ("mnc_pages_cache", [("cache_key", ASCENDING)], {"name": "cache_key_1"}),
    ("mnc_pages_cache", [("cached_at", ASCENDING)],
     {"expireAfterSeconds": 6 * 3600, "name": "mnc_pages_cache_ttl"}),

    # === Learning §6.6 — cached differentiation analysis (reload without re-spend).
    ("differentiation_analyses", [("user_email", ASCENDING)],
     {"unique": True, "name": "user_email_unique"}),
    # scheduled_posts: the auto-post 30-min hold; the scheduler scans due ones.
    ("scheduled_posts", [("status", ASCENDING), ("scheduled_for", ASCENDING)],
     {"name": "status_scheduled_for"}),
    ("scheduled_posts", [("user_email", ASCENDING)], {"name": "by_user"}),

    # === Guest Onboarding v2 (docs/specs/guest-onboarding.md) ===============
    # guest_sessions: guest_id is the uuid4 capability token — every route
    # filters {guest_id}. Unique protects capability-token integrity.
    ("guest_sessions", [("guest_id", ASCENDING)], {"unique": True, "name": "guest_id_unique"}),
    # v2 identity-resolution lookups (guest_identity_service._match_counts):
    # exact-match / $in on the versioned keyed-HMAC signal fields. The v1
    # plaintext indexes (identity.emails/phones/resume_hashes,
    # identity.name_normalized) are DROPPED below (DROP_INDEXES).
    ("guest_sessions", [("identity.email_hmacs", ASCENDING)], {"name": "identity_email_hmacs"}),
    ("guest_sessions", [("identity.phone_hmacs", ASCENDING)], {"name": "identity_phone_hmacs"}),
    ("guest_sessions", [("identity.resume_sig_hmacs", ASCENDING)],
     {"name": "identity_resume_sig_hmacs"}),
    ("guest_sessions", [("fingerprint_hash", ASCENDING)],
     {"sparse": True, "name": "fingerprint_hash_sparse"}),
    # Server-issued device token (sha256 stored) — session recognition +
    # identity "device" signal lookup.
    ("guest_sessions", [("device_token_hash", ASCENDING)],
     {"sparse": True, "name": "device_token_hash_sparse"}),
    # Quota-subject membership (unify_subjects re-points by subject id).
    ("guest_sessions", [("quota_subject_id", ASCENDING)], {"name": "quota_subject_id_idx"}),
    # cluster membership scan (guest_identity_service.cluster_ids) filters
    # {merged_into: canonical_id}; sparse — only merged members carry it.
    ("guest_sessions", [("merged_into", ASCENDING)],
     {"sparse": True, "name": "merged_into_sparse"}),
    # claim lookups / admin "who claimed what" (guest_service.claim_session).
    ("guest_sessions", [("claimed_by", ASCENDING)],
     {"sparse": True, "name": "claimed_by_sparse"}),
    # Retention sweep: {claimed_by: null, last_seen_at: {$lt: cutoff}}. A
    # partial index can't express equality-to-null (unsupported in
    # partialFilterExpression), so this is a plain compound index serving the
    # same claim-aware scan. NO Mongo TTL — retention is a claim-aware job.
    ("guest_sessions", [("claimed_by", ASCENDING), ("last_seen_at", ASCENDING)],
     {"name": "retention_scan"}),
    # guest_quota_subjects: the server-only quota/abuse identity. subject_id
    # is the atomic-reservation key (find_one_and_update) — unique required.
    ("guest_quota_subjects", [("subject_id", ASCENDING)],
     {"unique": True, "name": "subject_id_unique"}),
    # usage_service._linked_pool_emails: find({linked_accounts: email}).
    ("guest_quota_subjects", [("linked_accounts", ASCENDING)],
     {"name": "linked_accounts_idx"}),
    # guest_claim_carryins: once-per-(subject, UTC day) day-1 usage seed
    # marker — unique is a CORRECTNESS requirement (claim idempotency).
    ("guest_claim_carryins", [("subject_id", ASCENDING), ("date", ASCENDING)],
     {"unique": True, "name": "subject_date_unique"}),
    # Guest job polling: get_guest_job filters {job_id, guest_id} — job_id is
    # already unique above; add guest_id so the ownership check stays indexed.
    ("resume_jobs", [("guest_id", ASCENDING)], {"sparse": True, "name": "guest_id_sparse"}),
    # Guest tailoring records: claim_session.update_many filters
    # {guest_id: {$in: cluster}, user_email: None}; download mint filters
    # {record_id, guest_id, user_email: None}.
    ("tailoring_records", [("guest_id", ASCENDING)],
     {"sparse": True, "name": "guest_id_sparse"}),
    # platform_config is keyed on _id (default index) — nothing extra needed;
    # guest_global_counters + guest_challenge_velocity ($inc counters) ride _id.

    # === job_match_snapshots (Resume Platform v3 §1.4) ======================
    # Guest matches live HERE (moved out of guest_sessions/guest_quota_subjects
    # embedded matched_jobs — those embedded paths are gone; the fresh DB
    # carries no legacy index for them). snapshot_id is the session's ref key.
    ("job_match_snapshots", [("snapshot_id", ASCENDING)],
     {"unique": True, "name": "snapshot_id_unique"}),
    # One snapshot per (quota_subject_id, date): guest_service._store_matches
    # upserts on this pair; _cache_doc_for_guest reads it.
    ("job_match_snapshots", [("quota_subject_id", ASCENDING), ("date", ASCENDING)],
     {"unique": True, "name": "subject_date_unique"}),
    # Retention sweep / claim purge-extension / erasure all filter {guest_id}.
    ("job_match_snapshots", [("guest_id", ASCENDING)], {"name": "guest_id_idx"}),
    # Unclaimed snapshots carry purge_after = now + 7d; claim sets it null
    # (TTL ignores non-date values, so claimed snapshots are retained).
    ("job_match_snapshots", [("purge_after", ASCENDING)],
     {"expireAfterSeconds": 0, "name": "ttl_purge_after"}),
]

# Indexes DROPPED by this migration (Guest Onboarding v2: plaintext identity
# lookup fields are removed — no raw email/phone/resume-signature index may
# remain). Dropping a non-existent index is logged and skipped.
DROP_INDEXES = [
    ("guest_sessions", "identity_name_normalized"),
    ("guest_sessions", "identity_emails"),
    ("guest_sessions", "identity_phones"),
    ("guest_sessions", "identity_resume_hashes"),
]


def _fmt(keys, opts):
    key_str = ", ".join(f"{k}:{'desc' if d == DESCENDING else 'asc'}" for k, d in keys)
    flags = []
    if opts.get("unique"):
        flags.append("unique")
    if opts.get("sparse"):
        flags.append("sparse")
    if "expireAfterSeconds" in opts:
        flags.append(f"TTL={opts['expireAfterSeconds']}s")
    suffix = f" [{', '.join(flags)}]" if flags else ""
    return f"{{{key_str}}}{suffix}"


def run_drops(db, dry_run: bool):
    """Drop superseded indexes (v1 → v2 migrations). Missing indexes skip."""
    for coll, name in DROP_INDEXES:
        label = f"{coll}.{name}"
        if dry_run:
            print(f"  [would-drop]   {label}")
            continue
        try:
            if name in db[coll].index_information():
                db[coll].drop_index(name)
                print(f"  [dropped]  {label}")
            else:
                print(f"  [absent]   {label}")
        except (OperationFailure, PyMongoError) as e:
            print(f"  [DROP-FAILED] {label}: {e}")


def run(db, dry_run: bool):
    created = defaultdict(int)
    existed = defaultdict(int)
    failed = defaultdict(int)

    for coll, keys, opts in INDEX_SPECS:
        opts = {**opts, "background": True}
        name = opts["name"]
        label = f"{coll}.{name} -> {_fmt(keys, opts)}"

        if dry_run:
            print(f"  [would-ensure] {label}")
            created[coll] += 0  # register collection in summary
            continue

        try:
            existing = set(db[coll].index_information().keys())
            db[coll].create_index(keys, **opts)
            if name in existing:
                existed[coll] += 1
                print(f"  [exists]   {label}")
            else:
                created[coll] += 1
                print(f"  [created]  {label}")
        except (OperationFailure, PyMongoError) as e:
            failed[coll] += 1
            print(f"  [FAILED]   {label}: {e}")

    return created, existed, failed


def main():
    dry_run = "--dry-run" in sys.argv
    db = DBConnect().get_db()
    mode = "DRY RUN" if dry_run else "APPLY"
    print(f"== migrate_indexes ({mode}) on db '{db.name}' (LIVE Mongo connection) ==")
    print(f"   {len(INDEX_SPECS)} index spec(s) across "
          f"{len({c for c, _, _ in INDEX_SPECS})} collection(s)\n")

    run_drops(db, dry_run)
    created, existed, failed = run(db, dry_run)

    print("\n== Summary by collection ==")
    all_colls = sorted({c for c, _, _ in INDEX_SPECS})
    total_specs = total_created = total_existed = total_failed = 0
    for coll in all_colls:
        n_specs = sum(1 for c, _, _ in INDEX_SPECS if c == coll)
        c, e, f = created[coll], existed[coll], failed[coll]
        total_specs += n_specs
        total_created += c
        total_existed += e
        total_failed += f
        if dry_run:
            print(f"  {coll:<24} would ensure {n_specs}")
        else:
            print(f"  {coll:<24} specs={n_specs} created={c} exists={e} failed={f}")

    print(f"\n  TOTAL specs={total_specs}", end="")
    if not dry_run:
        print(f" created={total_created} exists={total_existed} failed={total_failed}")
    else:
        print(" (dry run — nothing written)")

    print("\nDone." + ("  (dry run — no indexes created)" if dry_run else ""))
    if not dry_run and total_failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
