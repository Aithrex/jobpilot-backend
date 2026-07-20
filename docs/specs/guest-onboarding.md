# Guest Onboarding — Cross-Repo Spec (v2)

> **v2 supersedes v1 (2026-07-16).** Product decisions locked with owner 2026-07-19.
> v1 history: 5 lifetime tailors, 5-10 jobs, plaintext identity arrays, no retention enforcement,
> no download capability, tailor accepted arbitrary `jd_text|url`. All of that is replaced below.

## Product contract (locked)

- Guest gets: upload -> parsed resume -> **exactly 5** strong matches (direct employer sources)
  -> **1 lifetime tailored resume** with a **polished PDF/DOCX download** — all before signup,
  no account, no card.
- The single guest tailor is **per quota subject** (see Identity), not per browser/guest_id:
  re-uploading the same resume from the same device under a fresh `guest_id` must NOT reset the trial.
- Both download formats (PDF + DOCX) of the one tailored resume count as the **same single
  tailoring event** — downloads never consume quota.
- After the guest tailor is reserved, remaining matches stay **fully visible** (server returned them;
  we lock the capability, not the evidence). FE disables their tailor buttons with signup CTA.
- Free plan after signup: **5 tailors per UTC day** (`free_daily_tailors`). The guest tailor carries
  into day-1 usage on claim (leaves 4 that day). Resets normally next UTC day. Never double-counted.
- Multiple free accounts **confidently linked** to the same quota subject share one free daily pool
  (see Identity: account linkage). Premium behavior unchanged.
- Guest data retention: **default 30 days** for unclaimed sessions, admin-configurable
  (`retention_days`, 0 = keep forever), with a real cleanup job. Claimed data is never auto-deleted.

## Identity architecture (v2 — the core change)

Three separate concepts. Never conflate them.

1. **`guest_id`** — unguessable capability (uuid4, server-minted). Grants access to that session's
   resume, matches, tailored output, and download tokens. Generic 404 on mismatch. Unchanged from v1.
2. **`quota_subject_id`** — server-only pseudonymous uuid identifying an abuse/quota cluster.
   Used ONLY for quota counters, trial enforcement, match-cache reuse, and risk decisions.
   **Never returned to any client. Grants no artifact access.**
3. **Account identity** — JWT identity remains email (legacy secret pinned). Unchanged.

### Signals

Stored in `guest_sessions.identity` as **versioned keyed HMACs** (`"v1:" + hex(hmac_sha256(key, value))`)
using server secret `GUEST_SIGNAL_HMAC_KEY` (SSM `SSM_GUEST_SIGNAL_HMAC_KEY`; unset -> fail closed:
signals are not stored or matched, subjects stay standalone, warn once, feature keeps working).
No raw email/phone/resume-signature lookup fields may be added. v1 plaintext arrays
(`identity.emails/phones/resume_hashes`) are dropped by the migration.

| Signal | Field | Class |
|---|---|---|
| resume contact email (normalized) | `identity.email_hmacs[]` | **strong** |
| resume phone (digits-only) | `identity.phone_hmacs[]` | **strong** |
| canonical resume signature | `identity.resume_sig_hmacs[]` | **strong** |
| server-issued device token (sha256 stored) | `device_token_hash` | weak (device) |
| FE fingerprint (existing, low-entropy) | `fingerprint_hash` | weak (device) |
| normalized name | `identity.name_normalized` | weak (display/recognition only) |
| salted IP hash (existing) | `ip_hash` | velocity only, never linkage |

### Resolution rules (quota linkage only — NEVER artifact access)

- Merge into one quota subject requires **>=2 distinct signal types, at least one strong**
  (email/phone/resume-sig HMAC).
- `device_token` and `fingerprint` count as ONE combined "device" signal type (same evidence
  source; shared-device users must remain separate subjects).
- Name alone, fingerprint alone, device alone, IP alone, or name+device: **no merge** — ambiguous
  identities stay separate (false-positive merging is worse than a free retry).
- Resolution outcome carries quota + recognition only. It never blocks service by itself and never
  exposes one session's artifacts to another.
- `recognized_name` is returned **only** to a caller presenting a valid `guest_id` whose session has
  a parsed resume. Fingerprint/device-only recognition returns `{exists:false, recognized:true}`
  with **no name field**. (v1 returned `recognized_name:""` — v2 omits it entirely, including inside
  the `guest_parse` job result.)
- Account linkage ("confidently linked free accounts"): two accounts are linked **only** when each
  has claimed (authenticated + exact guest_id capability) guest sessions resolving to the same
  quota subject. Fingerprint/registration heuristics never link accounts.

### Server-issued device token

`POST /guest/onboard` response gains `device_token` (opaque random >=32 bytes urlsafe). FE persists
it (localStorage) and echoes it on session/matches/tailor calls. Server stores only `sha256(token)`.
It is an anti-abuse signal, not a capability. A transparent privacy notice on the FE explains this
first-party anti-abuse storage.

## Data model (Mongo `aspirely_db`)

### `guest_sessions` (v2 shape)
```
{
  guest_id, created_at, last_seen_at,
  quota_subject_id: uuid str,          // server-only
  fingerprint_hash, device_token_hash, ip_hash,
  identity: { name_normalized, email_hmacs:[], phone_hmacs:[], resume_sig_hmacs:[], sig_v: 1 },
  resume: { filename, structured, raw_text, uploaded_at },
  matched_jobs: { date, jobs:[{match_id, ...card}], computed_at, min_score },  // see match cache
  tailor_record_ids: [],
  merged_into, claimed_by, claimed_at,
}
```

### `guest_quota_subjects` (new)
```
{
  subject_id: uuid str,
  created_at, last_seen_at,
  tailors_used: int,                       // lifetime guest tailors (limit 1)
  reservation: { match_id, guest_id, job_id, record_id|null, status: reserved|completed|failed,
                 reserved_at } | null,     // the single atomic reservation slot
  matched_jobs: { date, jobs:[...], computed_at, min_score } | null,  // subject-level day cache
  linked_accounts: [email],                // set on claim only
  carryins: not stored here — see guest_claim_carryins
}
```
- **Atomic reservation:** `find_one_and_update({subject_id, $or:[{reservation:null},{reservation.status:"failed"}], tailors_used:{$lt:limit}}, {$set:{reservation:...}, $inc:{tailors_used:1}})`.
  Two concurrent tailor calls -> exactly one reservation. A retry with the SAME `match_id` while a
  reservation exists returns the existing `job_id` (`existing:true`) — idempotent; a different
  `match_id` after reservation -> 429 `quota_exhausted`. A `failed` tailor releases the slot
  (`$inc:-1`, status failed) so the guest is not cheated by an infra error.
- **Match cache moves to the subject** (with a session-level fallback when HMAC key is unset):
  same-day re-upload under a new guest_id serves the cached 5 matches and never re-spends the
  global matches budget.

### `guest_claim_carryins` (new)
`{ subject_id, date: "YYYY-MM-DD", email, created_at }` — unique index `(subject_id, date)`.
Claim seeds day-1 usage **once per subject per day** regardless of how many linked accounts claim.

### `platform_config` (v2 defaults)
`guest_tailor_limit: 1` (bounds 0-100) · `guest_jobs_max: 5` (1-50) · `guest_jobs_min_score: 40`
· `free_daily_tailors: 5` · `free_jobs_daily_cap: 10` · `retention_days: 30` (0-3650, 0 = forever)
· global breakers unchanged (`global_daily_guest_tailors/parses/matches`). All admin-editable at
runtime via `GET/PUT /api/admin/guest-config` (allowlist + bounds validation as v1).

### Secrets (env/SSM, follow the `GUEST_IP_HASH_SALT` pattern; document in MANUAL_STEPS.md)
- `GUEST_SIGNAL_HMAC_KEY` (`SSM_GUEST_SIGNAL_HMAC_KEY`) — identity signal HMACs. Fail closed.
- `GUEST_DOWNLOAD_TOKEN_SECRET` (`SSM_GUEST_DOWNLOAD_TOKEN_SECRET`) — download capability tokens.
  Fail closed (503 capacity-style message on issue attempts, never 401).
- Never reuse `JWT_SECRET_KEY` for either. Never rotate `JWT_SECRET_KEY`.

### Indexes (`scripts/migrate_indexes.py`)
Keep: `guest_id` unique, `fingerprint_hash`, `merged_into`, `claimed_by`, sparse `resume_jobs.guest_id`,
sparse `tailoring_records.guest_id`. Add: `quota_subject_id`; `identity.email_hmacs`;
`identity.phone_hmacs`; `identity.resume_sig_hmacs`; `device_token_hash`;
`guest_quota_subjects.subject_id` unique; `guest_claim_carryins (subject_id, date)` unique;
partial `(claimed_by: null, last_seen_at)` for retention scans. Drop v1 plaintext identity indexes
(`identity.emails/phones/resume_hashes`, `identity.name_normalized`). **No Mongo TTL** — retention
is a job with claim-awareness, not a blind TTL.

### Migration/backfill
One-shot script: for each existing `guest_session` — mint `quota_subject_id` per existing
`merged_into` cluster (do NOT re-run linkage on plaintext data), HMAC the plaintext identity arrays
into the v2 fields, then `$unset` the plaintext arrays. Existing `tailors_used` sums map onto the
subject; sessions already over the new limit (used >= 1) simply have no remaining trial — counters
must never go negative and responses must render the exhausted state gracefully.

## Endpoints (all ride existing Terraform routes — NO new top-level `/api/<prefix>`)

Resume Lambda (`/api/resume/guest/*`, blueprint `services/resume/blueprints/guest.py`):

- `POST /guest/onboard` — multipart pdf/docx <=5MB + `fingerprint_hash`, optional `guest_id`,
  optional `device_token`. -> `202 {guest_id, job_id, device_token}`. IP-throttled 10/hr; global
  parse breaker; progressive challenge hook (below). Async parse (FLASH) unchanged.
  - Storage (v2.1): the raw file is uploaded to S3 under `guest/{guest_id}/` and the async job
    payload carries only `file_s3_key` (never multi-MB base64 in Mongo, and always under the 256KB
    async Lambda invoke limit). The key is tracked on the session (`resume_file_s3_keys`) so
    retention cleanup deletes the object with the expiring unclaimed session. `file_base64` in the
    payload survives ONLY as a bucketless local-dev fallback. Job finalization (`complete_job` /
    `fail_job`) `$unset`s the stored payload — nothing reads it after dispatch.
- `GET /guest/job/<job_id>?guest_id=` — unchanged capability-scoped polling, mirrors the authed
  job shape. `guest_parse` completed result: `{parsed_resume:{structured, filename},
  guest:{recognized, tailors_used, tailors_limit, recognized_name?}}` — `recognized_name` only via
  the owning guest_id rule above.
- `GET /guest/session?guest_id=&fingerprint_hash=&device_token=` —
  `{exists, recognized, tailors_used, tailors_limit, has_resume, matched_jobs_today, claimed,
  recognized_name?}`. Device/fingerprint-only hit: `{exists:false, recognized:true, claimed:false}`
  (no name).
- `POST /guest/tailor` — **`{guest_id, match_id, device_token?}` ONLY.** The v1
  `job:{title,company,jd_text|url}` shape is removed; requests carrying it get 400 with a
  self-explanatory message (FE ships in the same window). `match_id` must belong to the subject's
  CURRENT cached 5 matches. JD is resolved server-side from the stored match (stored description,
  else `jd_fetcher` on the stored URL — SSRF guards intact; never a client-supplied URL).
  Responses: `202 {job_id, tailors_used, tailors_limit}` (atomic reservation) ·
  `200 {job_id, existing:true, ...}` (idempotent retry, same match) ·
  `429 {quota_exhausted:true, tailors_used, tailors_limit}` ·
  `429 {error:"capacity", global:true}` · `400/404` bad/unknown ids. Never 401.
- `POST /guest/download` — `{guest_id, record_id, format: "pdf"|"docx"}` ->
  `200 {token, expires_at, formats:["pdf","docx"]}`. Only for a `tailoring_records` doc owned by
  this exact guest_id (unclaimed) — 404 otherwise. Unlimited re-issue; never consumes quota.
- `GET /guest/download/<token>` — streams the rendered file via the existing hardened
  `resume_renderer` (same output as authed downloads). Token = versioned HMAC over
  `record_id|guest_id|format|exp`, TTL 15 min. `410` expired · `404` invalid/forged (generic).
  No JWT, never 401.
- `POST /guest/claim` — **JWT required** (the only legitimate 401 surface). `{guest_id}` ->
  atomic + idempotent + **exact-capability-only**: transfers ONLY artifacts of the presented
  guest_id's session chain that the capability proves (its own records; never "everything in the
  soft cluster" beyond sessions already merged server-side by strong-signal resolution).
  Marks claimed, copies resume if user has none, links account email on the subject, and seeds
  day-1 free usage via `usage_service` guarded by BOTH the claimed flag and the
  `guest_claim_carryins (subject_id, date)` unique marker. Free plan only; premium untouched.
  -> `200 {claimed:true, already_claimed:bool, resume_imported:bool, records_transferred:int,
  tailors_carried:int, daily_tailors_used:int, daily_tailors_limit:int}`.
  Repeat by same account: `already_claimed:true`, unchanged counts. Another account: 403
  `already_claimed` (v1 behavior kept). Claim stays NOT flag-gated.
- `guest_retention_cleanup` — new `_process_job` branch: deletes guest_sessions with
  `claimed_by:null` and `last_seen_at` older than `retention_days` (skip when 0), plus their
  unclaimed `tailoring_records` and stale guest `resume_jobs`; subjects with no remaining sessions
  keep quota counters until their own staleness horizon (abuse memory). Triggered by EventBridge
  schedule (infra) and admin-invokable. Deletes ONLY expired unclaimed data.

Jobs Lambda (`/api/jobs/guest/*`):

- `POST /jobs/guest/matches` — `{guest_id, device_token?}`. Subject-level same-day cache hit ->
  sync `200 {jobs, date, computed_at, min_score, cached:true}` (never spends budget); miss ->
  breaker reservation -> async `202 {job_id}`. Free direct sources only (unchanged).
  **Server returns at most `guest_jobs_max` (5) jobs — no extra locked rows for CSS to hide.**
  Each card: `{match_id, title, company, location, apply_link, url, source, posted, tier,
  logo_url, match_score, matched_skills:[], gaps:[], why: str, sponsorship: {signal, note}|null}`.
  - `match_id` — opaque server-minted id stored in the cache; the only tailorable reference.
  - `gaps` — additive v2.1 field (absent on older cached cards): up to 5 of the guest's own
    resume skills the posting does NOT mention (the scorer's `missing_top`). Resume-relative
    score context, NOT JD requirements the guest lacks — the FE must label it accordingly
    (e.g. "from your resume, not mentioned in this posting").
  - `logo_url` — server-generated relative path `/api/jobs/logo/<sha1(company)>` serving the
    company's cached real favicon (public, no auth); when it 404s the FE renders a monogram.
  - `why` — short deterministic explanation from the scorer (matched skills / role-term overlap).
    Scores are presented as estimates, never hiring probabilities.
  - `sponsorship` — only when the employer matches public-filing data (`h1b_sponsor_data`):
    precise language, "signals from public filings", never "verified sponsor". Null otherwise.

Auth Lambda: `GET/PUT /api/admin/guest-config` unchanged surface, v2 defaults/bounds.
Register/login unchanged (flat payloads; failures 400/409/403/429 never 401).

## Progressive challenge (CAPTCHA) hook

`guest_challenge_service.maybe_challenge(kind, ip_hash, subject_id)` — provider-agnostic seam.
Default provider `none` (no-op). When a provider is configured AND velocity is suspicious
(per-IP-hash counters over threshold), guest write endpoints return
`429 {error:"challenge_required", provider}` BEFORE spending budget. Never triggered on a normal
first attempt. No provider is shipped in v2 — the seam + tests only.

## Security invariants (unchanged from v1 + v2 additions)

1. InputSanitizer before every Mongo filter; guest_id/match_id/record_id format-validated first.
2. Capability tokens -> generic 404; no existence oracles (download 404 for invalid AND
   other-guest records; 410 only for a well-formed expired token).
3. No PII in logs (names/emails/phones/resume content/raw tokens).
4. Guest failures are 400/403/404/409/410/429/503 — **never 401** (FE force-logs-out on 401).
5. IntegrityGuard, fabrication filter, schema validation, Bedrock-only routing untouched.
6. SSRF: JD fetch only via `jd_fetcher` from server-stored match URLs.
7. Async `{job_id}` pattern for all long work; reservation happens BEFORE dispatch; failed
   dispatch rolls back reservation and breaker spend.

## Frontend (jobpilot-frontend) consumption

- Reusable guest primitives extracted from `Start.tsx`: upload dropzone + flow state hooks shared
  by the landing hero and `/start`.
- Exactly 5 match cards; any one can be tailored; after the single success every other tailor
  button locks (disabled + signup CTA) — data stays fully visible, no blur, no hidden rows.
- Polished PDF (and DOCX) download via the token endpoints replaces the client-side `.txt` export.
- `device_token` persisted alongside `guest_id`; echoed on session/matches/tailor.
- Recognition UI renders a name-less "Welcome back" unless the session (guest_id-proven) provides
  `recognized_name`.
- 429 quota/capacity/challenge responses render conversion CTAs — never error toasts, never logout.
- Claim flow unchanged in shape (AuthContext calls claim post-auth) but consumes the v2 response
  (`tailors_carried`, `daily_tailors_used/limit`) to show "1 of 5 used today".
- User-facing copy: sentence case, no em/en-dashes, no accented characters ("resume", not the
  accented spelling), match scores framed as estimates.

## QA / acceptance (tests must prove)

Backend (`tests/test_guest_onboarding.py`, mongomock + fake provider, offline):
name-only / fingerprint-only / name+device never merge subjects · fingerprint-only lookup returns
no name anywhere (session + parse result) · shared-device users cannot see each other's artifacts ·
resume+device re-upload under fresh guest_id does not reset the trial · second linked free account
shares the daily pool (carry-in once) · ambiguous identities stay separate · server never returns
more than 5 guest jobs · tailor accepts only a served match_id (raw url/jd_text payloads -> 400) ·
two concurrent tailors -> exactly one reservation · same-match retry idempotent (existing:true) ·
forged/expired download tokens -> 404/410 generic · claim exact/atomic/idempotent, usage carried
once · retention deletes only expired unclaimed · SSRF + prompt-injection posture intact · global
breakers still enforce · no guest failure path emits 401.

Frontend: hero upload keyboard-accessible · loading/empty/failure/capacity/exhausted states
designed · exactly 5 matches render · one tailor then all others lock · one polished download ·
auth preserves claim · 429 never force-logs-out · reduced-motion honored · no hidden-data-as-
access-control.

## Out of scope v2
Shipped CAPTCHA provider integration · guest email verification · WhatsApp notifications ·
paid job sources for guests.
