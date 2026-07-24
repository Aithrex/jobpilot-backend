# Resume Platform v3 — Canonical Contracts (July 2026)

Source of truth for the v3 refactor: canonical resume schema, atomic quota ledger,
worker-persisted tailoring, guest claim state machine, matching supply fixes, and
storage hygiene. The configured MongoDB is FRESH AND EMPTY — implement the canonical
schema directly, no dual-read/migration code for old resume documents. Do NOT delete
any existing database or S3 bucket.

Platform invariants that MUST hold throughout (from CLAUDE.md):
- Auth failures 400/409/403/429, never 401 (401 = expired session only; guest routes
  never 401 except /guest/claim which is @jwt_required).
- Long AI work returns {job_id} and is polled at GET /api/resume/job/<id> (auth) or
  /api/resume/guest/job/<id> (guest).
- Bedrock only via services/llm_providers.get_provider / claude_client facade.
- Never fabricate resume facts (IntegrityGuard + fabrication_filter stay intact).
- Entitlements server-authoritative; no tailor double-count.
- No new top-level /api/<prefix> (everything rides existing proxy routes).
- No __init__.py in services/ or blueprints/ dirs (PEP 420 namespace merge).
- User-facing copy: no em/en dashes, no accented characters ("resume", not the
  accented form).

## 1. Canonical collections

### 1.1 `resume_assets` (replaces the dual-type `user_resumes` usage)
One document per uploaded file. Immutable `resume_id` (uuid4 hex).

```
{
  resume_id: str unique,
  owner_type: "user" | "guest",
  user_id: str | null,          # immutable users._id str, set for owner_type=user
  user_email: str | null,       # denormalized for queries; ownership key is user_id
  guest_id: str | null,         # for owner_type=guest
  quota_subject_id: str | null, # guests only; NEVER exposed to clients
  object_key: str | null,       # S3: resume-assets/{resume_id}/original
  filename: str,                # protected original filename (Mongo only, never in keys/logs)
  mime_type: str, size_bytes: int, content_hash: str (sha256 hex),
  parse_status: "pending" | "parsing" | "parsed" | "failed",
  parse_error: str | null,
  structured: {...} | null,     # PARSED_RESUME schema output
  raw_text: str | null,         # capped 60000 chars
  skills: [...], experience_years, job_titles, summary, education, certifications,
  claim_status: "n/a" | "unclaimed" | "claimed",   # guests
  created_at, parsed_at, purge_after: datetime | null  # guests: last_seen + retention
}
```

Indexes: `resume_id` unique; `(user_email, created_at desc)`; `(user_id)`;
`guest_id` sparse; `content_hash + user_email` (upload dedup); TTL on `purge_after`
(expireAfterSeconds=0, sparse via partialFilterExpression owner_type=guest).

### 1.2 `users.active_resume_id`
Single atomic pointer, updated only by:
- upload completion for the NEWLY uploaded asset **only if** the user had no active
  resume OR explicitly uploaded it as active (default true for uploads, atomic
  `$set {active_resume_id}` guarded by nothing racing: the worker sets it with
  `find_one_and_update({email, $or:[{active_resume_id: null},{active_resume_id: {"$exists": false}}]} ...)`
  plus an explicit route `PUT /api/resume/active {resume_id}` that validates ownership
  + parse_status=parsed then `$set`s unconditionally.
- Upload flow default: uploading a new resume sets it active on successful parse via
  `$set` compare-and-set against the job's resume_id (a stale/failed parse must NEVER
  overwrite a pointer the user changed meanwhile: set active only if
  `active_resume_candidate == resume_id` recorded at upload time, i.e. the route
  stamps `users.pending_active_resume_id = resume_id`; the worker promotes it with
  find_one_and_update({email, pending_active_resume_id: resume_id} ->
  {$set: {active_resume_id: resume_id}, $unset: {pending_active_resume_id}})).
- Deleting the active asset atomically re-points to the newest remaining parsed asset
  or unsets.

ALL matching and tailoring resolve structured content ONLY through
`resume_access.get_active_structured(user_email)` (new accessor in
`shared/python/services/resume_access.py`) which resolves users.active_resume_id ->
resume_assets. `resume_parser.get_structured_resume` becomes a thin delegate to it
(keeps 35+ call sites working). `save_parsed_resume` writes INTO the owning asset doc
(no delete_many-then-insert).

### 1.3 `async_jobs` = existing `resume_jobs`, slimmed (transport only)
- Payload NEVER contains file bytes/base64. Auth upload payload:
  `{resume_id, object_key, mime_type, content_hash}` (worker fetches from S3).
  In AWS Lambda env, S3 failure at upload = 503 `{"error":"storage_unavailable"}`;
  bytes-in-payload fallback allowed ONLY when not running on Lambda (local dev
  thread dispatch), key `file_bytes_b64_dev`.
- Add `idempotency_key` (optional, unique sparse index) — tailor dispatch supplies
  one so client retries don't double-dispatch.
- TTL: new field `expire_at` set by complete_job/fail_job = now + 7 days; TTL index
  expireAfterSeconds=0. (Guest retention still sweeps guest jobs earlier.)
- Keep `update_job_progress(job_id, phase, pct)`; guest_parse, guest_matches, tailor,
  extract_and_tailor MUST emit phases (see 3.4).

### 1.4 `job_match_snapshots` (guest matches move OUT of guest_sessions/subjects)
```
{
  snapshot_id: str unique, owner_type: "guest",
  guest_id, quota_subject_id, resume_id,
  date: "YYYY-MM-DD" (UTC), computed_at,
  jobs: [card...],              # <=5, card shape unchanged (title, company, location,
                                # source, posted, apply_link, url, logo_url, match_score,
                                # matched_skills, gaps, why, sponsorship, match_id)
  jd_sources: {match_id: {title, company, jd_text<=15000, url}},  # server-only
  sources_report: {ok: [..], failed: [..], timed_out: [..], considered: int},
  min_score: int, purge_after: datetime   # unclaimed: now + 7d; claim extends
}
```
Indexes: snapshot_id unique; (quota_subject_id, date) unique; guest_id; TTL purge_after.
`guest_sessions.matched_jobs` and `guest_quota_subjects.matched_jobs` are REMOVED;
`find_cached_match(match_id)` resolves via snapshots. Guest tailor still accepts ONLY
{guest_id, match_id}.

### 1.5 `tailoring_records` + embedded bounded versions (deliberate deviation)
Versions stay EMBEDDED in tailoring_records (not a separate collection): versions are
immutable append-only entries, content-hash idempotent (existing resume_versioning.py),
and now BOUNDED: max 5 versions per record (structural cap `refinements_per_record`,
free and premium alike for now). The 6th refinement attempt returns 409
`{"error":"version_limit_reached", limit: 5}`. Rationale: bounded embedding cannot hit
the 16MB doc cap, avoids a cross-collection consistency problem, and keeps every
existing endpoint shape. Document this deviation in the final report.

Worker persistence: the `tailor` and `extract_and_tailor` workers now PERSIST the
canonical record themselves at finalize time (like batch/guest already do) and include
`record_id` in the job result. `POST /save-record` remains for ats_scores attach +
legacy clients but becomes idempotent-by-record_id (upsert keyed on record_id; it must
NOT create a duplicate when the worker already saved).

### 1.6 `quota_ledger` (atomic authenticated tailor quota, 5/UTC day)
Two-part design:
- Counter doc (atomic authority): collection `quota_counters`,
  `_id = "tailor:{pool_key}:{YYYY-MM-DD}"` where pool_key = the linked free pool key
  (existing `_linked_pool_emails` semantics: subject-linked free accounts share; else
  the email). Fields `{count, limit_snapshot, updated_at}`.
  RESERVE = `find_one_and_update({_id, count: {"$lt": limit}}, {"$inc": {count: 1}})`;
  if doc missing, upsert with count=1 guarded by a unique _id insert; no
  check-then-increment anywhere.
- Ledger entries: collection `quota_ledger`
  `{entry_id, idempotency_key unique sparse, user_email, feature: "tailor", date,
    state: "reserved"|"completed"|"refunded", job_id, record_id?, reserved_at,
    settled_at}`.
  complete_job(tailor*) -> state completed. fail_job on TERMINAL INTERNAL failure ->
  state refunded + `$inc count: -1` (guarded by state transition reserved->refunded so
  refunds are idempotent and never double-decrement; count never below 0 via
  `count: {"$gt": 0}` filter). A user-caused failure (invalid input rejected pre-LLM)
  is refunded too — only a SUCCESSFUL tailoring consumes.
- Routes: tailor/tailor-with-jd/regenerate-as-new-session reserve BEFORE dispatch and
  pass entry_id in the job payload. 429 shape unchanged
  (`daily_quota_exceeded` + usage block). Regenerate WITHIN a record's bounded
  versions does NOT consume quota (refinement policy 1.5) — only new sessions do.
- `usage_service.check_and_increment` is retired for tailor; keep the module as the
  reader (`get_usage_today` reads quota_counters) so QuotaMeter endpoints keep working.
  `feature_usage` continues to NOT meter tailor (no double count).
- `POST /batch-tailor` now enforces `require_feature("batch_tailor")` (403
  upgrade_required on free) AND reserves n slots atomically (all-or-nothing).
- Guest quota: existing subject reservation slot stays; align refund semantics
  (already correct).

### 1.7 `guest_sessions` slim + claim state machine
Remove embedded `resume` and `matched_jobs`; keep capability/identity/refs:
`{guest_id, quota_subject_id, identity{...hmacs}, resume_id (ref), snapshot_id (ref),
 tailor_record_ids, resume_file_s3_keys (legacy field kept for retention), device/fp
 hashes, merged_into, claim: {state: "unclaimed"|"claim_pending"|"transferring"|
 "completed", email, started_at, completed_at, steps: {records: bool, resume: bool,
 subject_link: bool, carryin: bool}}, created_at, last_seen_at}`.
Claim flow (POST /guest/claim, @jwt_required):
1. CAS unclaimed->claim_pending (find_one_and_update guarded on state in
   [unclaimed] OR (claim_pending/transferring AND claim.email == caller)) — a second
   account gets 403 already_claimed; the SAME account resumes an interrupted claim.
2. transferring: run steps idempotently, tick claim.steps flags as each completes:
   records (tailoring_records guest->email), resume (resume_assets claim_status ->
   claimed + owner attach user_id/email, extend purge_after -> null), subject link,
   day-1 carryin (existing double guard).
3. completed: set claimed_by/claimed_at (legacy fields) + claim.state=completed.
4. On completion, guest download minting for the claimed records STOPS (mint requires
   an unclaimed record); token REDEMPTION also re-checks the record is still guest-
   owned (user_email null) so outstanding pre-claim tokens die at claim.
Retention sweep + account erasure both understand the new shape (see 4).

## 2. S3 rules

- New object keys: base uploads `resume-assets/{resume_id}/original`; generated files
  `tailoring/{record_id}/{version_id}/{render_version}.{fmt}`. No names, emails,
  original filenames, job titles, or second-resolution timestamps in NEW keys. Old
  objects under old key schemes remain readable via stored object_key refs (never
  parse keys for metadata; fresh Mongo means no old refs anyway).
- Guest uploads use the same `resume-assets/{resume_id}/original` scheme
  (resume_id exists pre-parse; the guest session stores the ref).
- Orphan rule: S3 write succeeds then Mongo insert fails => compensating
  delete_object in the same request path (best effort) + the write order is
  Mongo-insert-first-with-parse_status=pending, S3 second, then $set object_key;
  a failed S3 write marks the asset failed. Assets stuck in pending with no
  object_key >24h are swept by the retention job (both Mongo doc and any object).
- Deletion:
  - tailoring record delete removes every versions[].files[fmt].s3_key from S3
    (per-object result inspected; Mongo delete proceeds only after S3 errors are
    collected; failures leave the record with `deletion_pending: true` for the sweep).
  - resume asset delete: remove S3 object + doc; if it was active, atomically
    re-point (1.2).
  - account erasure adds: guest_sessions claimed by the email (and their
    resume_file_s3_keys + resume_assets guest docs + snapshots), quota_ledger,
    quota_counters entries for the pool where sole member, guest_claim_carryins,
    guest_quota_subjects linked_accounts $pull (subject doc itself is retained as
    abuse memory WITHOUT the email). S3 deletes during erasure use
    list_object_versions + delete all versions (bucket is versioned) for the
    {user_id}/ prefix AND resume-assets/{resume_id}/ prefixes owned by the user.
    Identity (users doc) deleted last, only after storage cleanup verified.
- Trust copy rule: guest retention is 30 days for unclaimed guest data (Mongo TTL +
  retention job + S3 delete); because bucket versioning keeps noncurrent versions 90
  days (lifecycle), copy must say "deleted from our systems within 30 days; residual
  encrypted backups expire within a further 90 days" — never claim immediate physical
  erasure.

## 3. Matching supply + latency (guest 5 matches)

Root causes found: Workday CXS rows have empty descriptions so the >=40 score filter
kills them (title-only scoring); supply collapses to ~18 hardcoded ATS boards; silent
{jobs: []} success; cold-start 15s Workday budget; no progress markers.

Fixes (guest path, all free/deterministic — no paid providers, no Bedrock):
3.1 Description enrichment: after initial rank, fetch CXS job DETAIL for the top ~15
    description-less Workday candidates (parallel, per-request timeout 6s, overall
    budget 12s, best-effort) and re-score with real descriptions.
3.2 Title-relevance prefilter replaces the tech-only `_ATS_TECH_TITLE` drop: filter
    sources by overlap with the guest's resume-derived role terms so non-tech resumes
    are not zeroed out.
3.3 Deadlines: compute_guest_matches gets an overall wall-clock budget (75s); on
    expiry it persists whatever it has (partial genuine results). Per-source budgets
    stay (Workday 15s -> raise to 25s for guests; ATS fan-out gets an overall 20s cap).
3.4 Progress: emit update_job_progress phases — reading_resume(10),
    searching_sources(30, then 30..70 as providers complete), ranking(80),
    finalizing(95). guest_parse emits uploading->reading_experience->extracting_skills
    equivalents (parsing(20), extracting_skills(60), finalizing(90)).
3.5 Structured outcomes: job result gains `sources_report` (1.4) and `empty_reason`:
    "no_openings_matched" | "sources_unavailable" | "low_signal_resume". Completed-
    empty is distinguishable from provider failure; FE renders honest states.
3.6 Result floor honesty: never pad; if 1-4 clear the bar, return them with
    `total_considered`; copy on FE says "We found N strong matches".
3.7 Idempotent retry: POST /guest/matches when a snapshot exists for today returns it
    (existing); when the last job failed, a retry re-dispatches WITHOUT consuming the
    global counter twice (counter reserve happens only once per (subject, date) —
    guard with a `matches_reserved:{date}` flag on the subject).
3.8 Instrumentation: per-phase duration fields on the snapshot
    (`timings: {workday_ms, ats_ms, enrich_ms, score_ms, total_ms}`) + a WARNING log
    line with counts; no PII.

## 4. Upload security

- Global Flask MAX_CONTENT_LENGTH = 8MB on all four app factories (413 handler returns
  JSON `{"error":"file_too_large"}`).
- Auth upload aligned to the guest bounded-read pattern: content_length pre-check,
  read(cap+1), extension + magic-byte validation (existing validate_file), sha256.
- DOCX zip guard (new `shared/python/utils/zip_guard.py`): entry count <= 2048,
  total uncompressed <= 60MB, per-entry compression ratio cap 120x, reject nested
  containers; run BEFORE python-docx opens the file (both upload paths + parser).
- Parse state gains `scan_status: "unscanned"` recorded on the asset (explicit
  quarantine seam; no scanner integration yet — documented limitation).
- Logging hygiene: remove/redact the resume_parser.py lines that log candidate name,
  LinkedIn/GitHub hrefs, institutions (`:635,:638,:653,:759,:969,:982` region) and
  stop logging original filenames + full S3 keys (log resume_id instead). Guest paths
  already clean; keep them clean. Delete the committed app.log and gitignore it.

## 5. API contract deltas (FE-visible)

- GET /api/resume/guest/job/<id>: result may include `progress {phase, pct}` (already
  transported), and for guest_matches: `sources_report`, `empty_reason`,
  `total_considered`. For guest_parse: result gains `resume_id` and a `profile`
  block derived from structured: `{first_name?, headline_title?, experience_level?,
  years?, skills: [<=10], location?, work_authorization?, summary_short?}` — ONLY
  fields genuinely extracted; absent otherwise. (FE renders the profile reveal from
  this; extracted name is presentation-only, never identity.)
- POST /api/resume/upload (auth): unchanged shape (202 {job_id}), but response adds
  `resume_id`. GET /api/resume/versions now lists resume_assets
  ({resume_id, filename, uploaded_at, size_bytes, parse_status, is_active}).
  PUT /api/resume/active accepts {resume_id} (s3_key form retired — fresh DB, FE
  updated in lockstep).
- Tailor job results include `record_id` (worker-persisted).
- 409 `{"error":"version_limit_reached"}` on the 6th refinement of one record.
- Auth quota: GET /api/resume/usage (existing reader) unchanged shape, now backed by
  quota_counters. The SIXTH new session returns the existing 429 daily_quota_exceeded
  shape.
- Combined feed / careers job rows: backend guarantees `title` is always present on
  every job card (normalize any source-side job_title/role variants server-side in
  the fetchers); FE additionally defends in toJob.

## 6. Index / bootstrap

`scripts/migrate_indexes.py` gains all new indexes (1.1-1.6) including the TTLs and
partialFilterExpressions; stays idempotent; runbook: `python scripts/migrate_indexes.py`
against the fresh cluster. Obsolete index specs for removed embedded paths
(guest_sessions.matched_jobs etc.) are deleted from the spec list (fresh DB carries
no legacy indexes; do not write drop-code for other environments).

## 7. Explicit non-goals (documented deviations)

- tailoring_record_versions as a separate collection: deviated, see 1.5.
- Malware scanning engine: seam only (scan_status), no scanner wired.
- Paid job providers for guests: not added (cost restriction honored).
- Old-database migration/dual-read: none (fresh DB per decision).
