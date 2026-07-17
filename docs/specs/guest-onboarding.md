# Guest Onboarding — Cross-Repo Spec (v1)

Product decisions (locked with owner 2026-07-16):
- Guests get **upload → parsed resume → top matched jobs (5–10, high-score only) → 5 LIFETIME free tailors**, all **before signup**.
- **Composite identity, SOFT enforcement**: normalized name + resume contact email(s) + phone(s) + resume content hash + device fingerprint. **2+ matching signals = same person** (quota carries over, "welcome back" recognition). Never hard-block on a single signal (namesakes are safe). Name alone is a signal, never the key.
- After signup/login, free plan = **5 tailors/day** + capped daily job matches; guest usage carries into day-1 quota. All numbers **admin-editable at runtime** (no deploy).
- Guest data retained **until claimed (no auto-purge)** — but implement `retention_days` knob (0 = keep forever, DEFAULT 0) so policy can change from admin.
- Trust bar: show ONLY high-confidence matches (score threshold, admin knob) — fewer, accurate results beat volume.

## Feature flag
`GUEST_ONBOARDING_ENABLED` — same resolution pattern as `GMAIL_OAUTH_ENABLED` (unset → ON in dev, OFF in prod). Plus a runtime admin `enabled` knob (both must be on).

## Data model (Mongo, `portfolio_db`)

### `guest_sessions`
```
{
  guest_id: uuid4 str (capability token, unguessable),
  created_at, last_seen_at,
  fingerprint_hash: str|null,          // from FE, sanitized, <=128 chars
  ip_hash: str|null,                   // sha256(ip + GUEST_IP_HASH_SALT), abuse correlation, never raw IP;
                                       // NULL when the salt secret is unset (fail closed — never a reversible hash)
  identity: {
    name_normalized: str,              // lowercase, collapsed whitespace, unicode-folded
    emails: [str],                     // extracted from resume contact
    phones: [str],                     // digits-only normalized
    resume_hashes: [str],              // sha256 of canonicalized resume text
  },
  resume: { filename, structured: {...parsed}, uploaded_at },
  matched_jobs: { date: "YYYY-MM-DD", jobs: [...], computed_at },   // per-day cache
  tailors_used: int,                   // effective count = max across merged identity cluster
  tailor_record_ids: [str],            // guest tailor outputs (stored in tailoring_records with user_email=null, guest_id set)
  merged_into: guest_id|null,          // identity-cluster pointer (soft merge)
  claimed_by: email|null, claimed_at,
}
```
Indexes (add to `scripts/migrate_indexes.py`): `guest_id` unique; `identity.name_normalized`; `identity.emails`; `identity.phones`; `identity.resume_hashes`; `fingerprint_hash`; `claimed_by`.

### `platform_config` (new, generic runtime config)
One doc `{_id:"guest_onboarding", enabled:true, guest_tailor_limit:5, guest_jobs_min_score:40, guest_jobs_max:8, free_daily_tailors:5, free_jobs_daily_cap:10, global_daily_guest_tailors:200, global_daily_guest_parses:500, global_daily_guest_matches:1000, retention_days:0, updated_at, updated_by}`.
> `guest_jobs_min_score` default is **40** (originally spec'd 70): `jd_match_scorer.score_one` = matched/max(8, total_skills)×100, which lands ~20–55 on real resumes — 70 would blank the funnel. Admin bounds stay 0–100.
Read through a small `shared/python/services/platform_config_service.py` with in-process TTL cache (60s) — Lambda-warm-container friendly.
`global_daily_guest_tailors` = platform-wide circuit breaker on anonymous LLM spend (atomic `$inc` on a per-day counter doc; refuse guest tailors past it with a friendly "sign up to continue" message — signup bypasses it).
`global_daily_guest_parses` / `global_daily_guest_matches` = the same per-day atomic-reservation breaker for anonymous resume parses (`POST /guest/onboard`) and match scrapes (`POST /jobs/guest/matches`, cache **misses** only — a same-day cache hit never spends the budget). Exhaustion → `429 {error:"capacity", global:true, message}` (never 401); a failed job dispatch rolls the reservation back.

### Config / env (secrets + knobs outside `platform_config`)
- `GUEST_IP_HASH_SALT` — **required secret** (SSM in prod via `SSM_GUEST_IP_HASH_SALT`, `.env` locally): server-side salt for `guest_sessions.ip_hash`. Unset → `hash_ip` fails **closed**: `ip_hash` is stored as `null` (feature keeps working, no IP-abuse correlation) and a no-PII warning is logged once. Never ship a hardcoded fallback salt — a constant salt makes the hash reversible by brute-forcing the IPv4 space. See MANUAL_STEPS.md for the SSM wiring.
- `TRUSTED_PROXY_HOPS` (default `1`) — used by `utils/security.get_client_ip`: number of trusted forwarders in front of API Gateway that append the true peer to `X-Forwarded-For` (CloudFront = 1). Set `0` if API Gateway is ever exposed without CloudFront so only the connection source IP is trusted.

## Identity resolution (`shared/python/services/guest_identity_service.py`)
- `extract_signals(parsed_resume, fingerprint_hash)` → {name_normalized, emails, phones, resume_hash, fingerprint}.
- `resolve(signals)` → finds guest_sessions where **≥2 distinct signal types** match → returns canonical (oldest) session. 0–1 matches → new/standalone.
- On match: merge (set `merged_into` on the newer, union identity arrays onto canonical, `tailors_used = max/sum` — use SUM of cluster to prevent quota splitting).
- SOFT: resolution only carries quota + recognition; it never refuses service by itself.
- All lookups via sanitized exact-match values (InputSanitizer first — hard invariant #4, NoSQL injection).

## Backend endpoints (no new top-level /api prefix — everything rides existing Terraform routes)

> **Build note:** `jd_match_scorer.py` and `jd_fetcher.py` moved from `services/jobs/services/` to `shared/python/services/` — guest tailor (JD-by-URL) and guest matches (scoring) execute inside the resume Lambda's async worker, which can't import jobs-local modules in prod. All existing `from services.jd_*` imports resolve unchanged via the PEP 420 namespace merge.

Resume Lambda (`/api/resume/guest/*`, blueprint `services/resume/blueprints/guest.py`, registered in resume `app.py`):
- `POST /api/resume/guest/onboard` — multipart file (pdf/docx, ≤5MB) + `fingerprint_hash`, optional `guest_id`. IP-throttled (reuse `_ip_throttled` pattern, e.g. 10/hr). Parses via existing parser (FLASH) using the **async job pattern** (invariant #2): returns `{guest_id, job_id}`.
- `GET  /api/resume/guest/job/<job_id>?guest_id=` — guest-scoped job polling (verify the job belongs to this guest_id; NO JWT). Mirrors `/api/resume/job/<id>` response shape so FE `pollJob` can be reused with a flag. Completed `guest_parse` result nesting (as built): `{parsed_resume: {structured, filename}, guest: {recognized, recognized_name, tailors_used, tailors_limit}}`.
- `GET  /api/resume/guest/session?guest_id=&fingerprint_hash=` — `{exists, recognized_name, tailors_used, tailors_limit, has_resume, matched_jobs_today, claimed}`. If guest_id unknown but fingerprint matches an identity cluster → `{recognized: true, recognized_name}` (drives "Welcome back, sign up/login").
- `POST /api/resume/guest/tailor` — `{guest_id, job: {title, company, jd_text | url}}`. Checks: flag+admin enabled → guest quota (cluster-wide sum < guest_tailor_limit) → global daily circuit breaker → async tailor job (PRO tier, IntegrityGuard fully active). Creates a `tailoring_records` doc with `user_email: null, guest_id: <id>` so claim is a simple ownership flip. 429 + `{quota_exhausted: true}` when out (FE shows signup CTA — NOT 401, auth error contract).
- `POST /api/resume/guest/claim` — **JWT required.** `{guest_id}` → atomically: set `user_email` on guest tailoring_records, copy resume into `user_resumes` (if user has none), mark session claimed_by, seed today's tailor usage with cluster `tailors_used` (no double-count with `usage_service` — carry-in happens ONCE, guarded by claimed flag). Called by FE right after login/register.
  > `/guest/claim` is intentionally NOT flag-gated (unlike every other guest route): claiming spends no LLM and must survive a flag-off so users can always pull their guest work into their account.

Jobs Lambda (`/api/jobs/guest/*`, blueprint or routes in `services/jobs/blueprints/jobs.py`):
- `POST /api/jobs/guest/matches` — `{guest_id}`. Requires parsed resume on the session. Runs the FREE direct sources only (Workday CXS + Greenhouse/Lever/Ashby career portals — the same `skip_apify=True` path as the daily pipeline; NO Apify, NO paid actors), scores with `jd_match_scorer` vs parsed resume, returns only `score >= guest_jobs_min_score`, capped at `guest_jobs_max`, sorted desc. **Cache per guest per day** in `matched_jobs`. IP-throttled.
  > **Async on cache miss** (invariant #2 — the free-source fan-out blows past API Gateway's 29s): cache miss → `202 {job_id}`, FE polls `GET /api/resume/guest/job/<id>?guest_id=` (completed result: `{jobs, date, min_score, total_considered, cached:false}`). Same-day repeat → synchronous `200 {jobs, date, computed_at, min_score, cached:true}`. Each job card: `{title, company, location, apply_link, source, posted, tier, match_score, matched_skills}`.

Auth Lambda (admin, `services/auth/blueprints/admin.py`):
- `GET/PUT /api/admin/guest-config` — `@admin_required`. PUT validates types/ranges, writes `platform_config`, returns effective config. This is the admin tailoring-control surface.

Register/login unchanged (flat payloads, failures 400/409/403/429 never 401). Register already accepts `fingerprint_hash` — FE now always sends it.

## Security requirements (build to these; security agent verifies)
1. Every guest input through `InputSanitizer` before any Mongo filter; guest_id validated as UUID format before use.
2. Rate limits: per-IP on onboard/matches/tailor; global daily tailor circuit breaker; upload size/type enforced before read.
3. `guest_id` is a capability token — 404/generic on mismatch, no existence oracle beyond the session endpoint's designed recognition response.
4. No PII in logs (name/email/phone from resumes never logged).
5. JD text from guest goes through the same prompt-injection posture as logged-in tailor (schema-validated LLM output, IntegrityGuard, fabrication filter untouched).
6. SSRF: guest tailor accepts `url` only through the existing `jd_fetcher` (it has the SSRF guards); never a raw fetch.
7. CORS/security headers: same app-factory hooks as other blueprints.

## Frontend (jobpilot-frontend)

Flow (signed-out visitors):
1. `/start` (new route, also becomes the logged-out landing redirect): hero + resume dropzone (pdf/docx). Generates/persists `guest_id` + fingerprint hash in localStorage (`guest_id`, reuse existing fingerprint util used by register if present; else small hash of UA+canvas-free stable fields — no external lib).
2. Upload → `POST /guest/onboard` → poll `GET /guest/job/<id>?guest_id=` → parsing states (skeleton, progress copy).
3. Results screen: "Top matches for you today" — 5–10 job cards with match score, company, source badge; honest empty state if fewer clear the score bar ("We only show strong matches — check back tomorrow" + broaden CTA after signup).
4. Each card: "Tailor my resume — free (X of 5 left)". Tailor → poll → tailored preview + download. Quota meter always visible.
5. Quota exhausted / "more features" touchpoints → signup/login modal (existing auth). After auth: call `POST /guest/claim` with guest_id, then route into the normal app; their resume + tailors are already in their account.
6. Return visit: on `/start` load, call `GET /guest/session` → if recognized: "Welcome back, {name} — sign in to pick up where you left off" (soft: they can also continue as guest if quota remains).
7. All guest API calls go through `lib/api.ts` additions; 429 quota responses render the upgrade/signup CTA (never treated as errors); NO 401 handling changes.

UX bar: reuse shadcn components, existing LockBadge/QuotaMeter patterns, framer-motion for step transitions, mobile-first, dark mode, honest loading/empty/error states. This flow is the first impression — accuracy and calm simplicity over density.

## QA / verification
- BE: pytest for identity resolution (2-signal matrix, namesake non-merge, quota cluster sum), quota enforcement, claim idempotency (double-claim doesn't double-count), config validation. Mock Mongo (mongomock) + fake LLM provider.
- FE: `pnpm build` (vite only, NO tsc gate) + `pnpm lint` clean.
- E2E happy path via local.py where feasible.

## Out of scope v1
Email verification for guests, CAPTCHA (add hook point only), auto-purge cron (knob exists, job later), WhatsApp-style notifications.
