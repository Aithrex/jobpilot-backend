# JobPilot — Mobile App Specification (iOS / Android)

> Hand this to a native contractor. It is meant to be complete enough that they
> never need to ask a question. It describes the **live production API** (the same
> backend the web app uses), the auth contract, every feature's request/response
> shapes, push + deep-link contracts, design tokens, screen states, the mobile
> approval flow, offline behaviour, an analytics taxonomy, and a phased plan.
>
> The backend is Flask on AWS Lambda (4 microservices behind one edge), MongoDB,
> and Claude-on-Bedrock. **You do not touch the backend** — you consume the JSON
> API below. Nothing here requires a new endpoint unless explicitly called out.

---

## 0. TL;DR for the contractor

- **Base URL (prod):** `https://aspirely.me/api` — all paths below are relative to this.
- **Auth:** Bearer JWT in the `Authorization` header. Get it from `POST /api/auth/login` (email) or the Google OAuth flow. **Store it in the Keychain/Keystore, not UserDefaults.**
- **The one rule that will bite you:** the API **never returns 401 for a business error**. A `401` means *the token is missing/expired* → log the user out. Auth *failures* (bad password, taken email, quota, forbidden) come back as **400 / 409 / 403 / 429** with a JSON body. Do not treat those as logout.
- **Long work is async:** POST returns `{ "job_id": "..." }` with HTTP `202`. Poll `GET /api/resume/job/{job_id}` every ~2s until `status` is `completed` or `failed`.
- **Feature flags:** some features are dark in prod until the operator flips them (see §14). Treat a `404` from a flag-gated route as "feature unavailable," **not** an error.

---

## 1. Product & users

**JobPilot** is a career operating system for job seekers, built first for **international students in the US on F-1 status** (OPT/pre-OPT, STEM/business) racing a hard visa deadline. It does four things: **Discover** jobs ranked by fit + realistic sponsorship odds, **Apply** fast (browser extension on desktop; on mobile, review + one-tap approve), **Learn** the 2–4 gaps that separate you from the median applicant, and **Signal** a public track record.

**Personas** (design for these):
- **Priya**, 24, MS CS, 4 months to her OPT deadline, 340 applications sent, exhausted, on a phone at 11pm. Primary device is a mid-range Android.
- **Daniel**, 31, MBA career switcher, non-technical, churns the moment something feels like homework.
- **Wei**, 27, PhD, distrustful of AI, reads the privacy policy, won't connect LinkedIn until he understands exactly what's posted.
- **Sam**, 22, bootcamp grad, older Android, poor connectivity.

**Emotional job:** *reduce panic.* Nothing bounces or celebrates loudly. The OPT clock is calm and precise, never alarm-red. Confidence, not gamification.

---

## 2. Architecture & environments

| | Value |
|---|---|
| Prod API base | `https://aspirely.me/api` |
| Local dev API | `http://<your-mac-ip>:5000/api` (run `python run_local.py` in `jobpilot-backend`) |
| Transport | HTTPS only, JSON in/out (`Content-Type: application/json`), except file uploads (multipart) |
| Content encoding | UTF-8 |
| Timezone | All timestamps are ISO-8601 UTC (`...Z`). Render in the user's local tz. |
| Pagination | Most list endpoints return the full (bounded) set; where a `limit` query param exists it's documented. |

The API surface is **227 routes** across `/api/auth`, `/api/jobs`, `/api/apply`, `/api/resume`, `/api/billing`, `/api/admin`, `/api/tech-chronicle`. §12 has the full inventory; you only need a subset for v1 (§15).

---

## 3. Authentication

### 3.1 The contract (memorize)
- Success payloads are **FLAT**: `POST /api/auth/login` → `{ "access_token": "...", "email": "...", "role": "...", "sector": "..." }`. `register` → `{ "access_token", "email" }`.
- **Auth failures are 400 / 409 / 403 / 429 — NEVER 401.** A `401` is reserved for "you called a protected route with a missing/invalid/expired token" → clear the token and route to sign-in.
- The token is a JWT whose identity is the user's **email**. Send it as `Authorization: Bearer <token>` on every authenticated call.
- Password reset uses the field name **`new_password`** (snake_case) on `POST /api/auth/reset-password`.
- There is currently **no refresh-token rotation**; the access token has a long TTL (24h). Re-authenticate on 401. (If you want silent refresh, coordinate a backend change — do not assume one exists.)

### 3.2 Email / password
```
POST /api/auth/register   { email, password, ... }      → { access_token, email }
POST /api/auth/login      { email, password }           → { access_token, email, role?, sector? }
POST /api/auth/validate-password { password }            → { valid, errors[] }   (client-side UX)
POST /api/auth/check-email { email }                     → { exists }
POST /api/auth/request-password-reset { email }          → 200 (always, no user enumeration)
POST /api/auth/reset-password { token, new_password }    → 200
GET  /api/auth/verify                                    → 200 if token valid  (use to validate a stored token on launch)
POST /api/auth/logout                                    → 200  (also just drop the token client-side)
```
Password rules: 8+ chars, upper + lower + digit + special. Show inline validation via `validate-password`.

### 3.3 Google OAuth — use PKCE for native
The web app uses a redirect flow. For native, use **ASWebAuthenticationSession (iOS) / Custom Tabs (Android) with PKCE**:
```
GET /api/auth/oauth/providers                 → { providers: ["google", ...] }
GET /api/auth/oauth/google/authorize          → { authorize_url }   (or 302)
```
Open `authorize_url` in the system web-auth session with a **native redirect URI** you register (e.g. `jobpilot://oauth/callback`). The callback delivers the app's JWT. **Coordinate the exact native redirect URI + PKCE support with the backend/`auth-engineer` before building** — the current callback is web-oriented (`GET,POST /api/auth/oauth/google/callback`); a native PKCE variant may need a small backend addition. This is the one auth item to confirm, not assume.

### 3.4 Session handling
- On launch: read token from Keychain → `GET /api/auth/verify`. 200 → proceed; 401 → sign-in.
- On any 401 mid-session: clear token, emit a `session-expired` event, route to sign-in (do **not** show a scary error — just "please sign in again").

---

## 4. API conventions

### 4.1 Async job pattern (used by all LLM work)
Long operations (résumé parse, tailor, ATS score, cover letter, learning plan, Six Seconds, authored résumé, referral draft, etc.) return immediately:
```
POST /api/resume/tailor  ...            → 202  { "job_id": "6f2c-..." }
GET  /api/resume/job/{job_id}           → 200  { "status": "processing" | "completed" | "failed",
                                                 "result": {...}?,  "error": "..."?, "error_code": "..."? }
```
Poll every ~2000ms, total timeout 120–180s depending on the op. Terminal states: `completed` (use `result`), `failed` (show `error`; if `error_code == "LLM_RETRIES_EXHAUSTED"` show a "try again" message). Some jobs write partial `result` while still `processing` — you may render partials.

### 4.2 Error envelope
Non-2xx responses carry a JSON body. Common shapes:
```
{ "error": "upgrade_required", "feature": "...", "usage": {...} }        // 403 — gating (show paywall)
{ "error": "daily_quota_exceeded", "message": "...", "usage": {...} }    // 429 — quota
{ "error": "consent_required", "message": "..." }                        // 403 — needs a consent grant
{ "error": "not_found" }                                                 // 404 — flag off / missing
{ "error": "<human message>" }                                          // 400 — validation
```
Always prefer `message` for display when present; fall back to a friendly generic.

### 4.3 Entitlements / gating (freemium)
Plans: **free / premium ($19) / premium_plus ($39)**. Metered + boolean gates are enforced server-side; you surface them:
- `403 { error: "upgrade_required" }` → show the upgrade sheet for `feature`.
- `429 { error: "daily_quota_exceeded" | quota }` → show "you've used all N today, resets at midnight UTC."
- `GET /api/billing/plan` → the user's plan, feature flags, limits, and live usage (drive lock badges + quota meters from this).

### 4.4 Rate limits
Some endpoints are IP/user rate-limited and return `429 { error: "Rate limit exceeded..." }`. Back off and retry with jitter.

---

## 5. Core feature contracts (v1 surface)

Request bodies are JSON; responses are `result` inside the job envelope for async endpoints, or direct JSON for sync ones.

### 5.1 Onboarding (the highest-leverage 90 seconds)
Exact sequence: **sign in → upload résumé → connect LinkedIn (skippable) → a few résumé-derived questions → done.**
```
POST /api/resume/upload            (multipart: file=<pdf/docx>)   → 202 {job_id}  → result.parsed_resume
GET  /api/resume/status                                          → { has_base, parsed?, years?, skills[], titles[] }
GET  /api/resume/connect/linkedin/authorize                     → { authorize_url }   (open in web-auth session)
GET  /api/resume/connect/status                                 → { github:{connected}, linkedin:{connected,name?} }
PUT  /api/auth/preferences  { onboarding_complete:true, target_role, target_roles[],
                              experience_level, needs_sponsorship, target_locations[],
                              remote_pref, comp_floor }          → updates the profile, flips `onboarded`
```
Pre-fill the questions from the parse (target role from the most recent title, level from years). The user **confirms/edits** rather than filling blanks. Keep it to ~4–6 fields.

### 5.2 Résumé
```
GET  /api/resume/status | /versions | /generated               // what the user has
POST /api/resume/extract-jd  { job_description }               → {job_id} → result.jd_analysis
POST /api/resume/tailor      { ... }                           → {job_id} → tailored résumé
POST /api/resume/ats-scores  { ... }                           → {job_id} → ATS breakdown
POST /api/resume/cover-letter{ ... }                           → {job_id}
POST /api/resume/download    { ... }  (pdf/docx)               → file
GET  /api/resume/usage/today                                    // daily tailor quota
```

### 5.3 Discover (jobs + sponsorship + visa filters)
```
GET  /api/jobs/search?query=&location=&visa_filters={...}       → { jobs: [Job], total }
POST /api/jobs/batch-search  { ..., visa_filters:{...} }        → {job_id} → job list
GET  /api/jobs/saved  /  POST /api/jobs/saved  /  DELETE /api/jobs/saved/{job_id}
POST /api/jobs/trust-check   { job }                            → scam-shield assessment
GET  /api/jobs/sponsorship?company=Acme                         → { company, sponsorship: SponsorshipSignal|null }
POST /api/jobs/sponsorship/bulk { companies:[...] }             → { signals: { name: Signal|null } }
```
Every `Job` in a search result already carries:
```
Job.sponsorship?  = { score:0..100, band:"none"|"low"|"moderate"|"strong",
                      headline:"Sponsored 63 people in similar roles in the last 3 years",
                      approvals_5y, lca_filings_5y, median_wage, data_vintage, source, disclaimer }
Job.visa_flags?   = { sponsors_h1b:{value:bool|null,evidence}, cap_exempt, accepts_cpt,
                      no_citizenship_requirement, clearance_required, e_verify }   // each {value,evidence}
```
Render the sponsorship badge from `band`+`headline` (show numbers + `disclaimer` on tap). `visa_flags.clearance_required.value===true` → a red "not for you" chip. `visa_filters` is a bool map of `{sponsors_h1b, cap_exempt, accepts_cpt, no_citizenship_requirement, exclude_clearance}`.

### 5.4 OPT Clock (handle with extreme care — never gamify)
```
GET /api/resume/opt-clock  → { has_profile, opt_ead_start, opt_ead_end, stem_eligible, stem_ead_end,
                               unemployment_days_used, unemployment_days_limit (90|150),
                               days_remaining, key_dates:[{label,date}],
                               status_band:"ok"|"watch"|"urgent", disclaimer }
GET/PUT /api/resume/visa/profile     GET /api/resume/visa/timeline
```
Always show the disclaimer ("not legal advice; confirm with your DSO"). `urgent` uses calm amber, never alarm-red. If `unemployment_days_used` is null, prompt the user for it (never invent it).

### 5.5 Recruiter's Six Seconds
```
POST /api/resume/six-seconds  → 202 {job_id} → result:
  { gaze:{ sections:[{key,label,weight:0..1,reason}], focal_points[], blind_spots[], pattern, note },
    impression:{ headline_read, level_read, standout, missing_or_unclear, six_second_verdict } }
  | { error:"no_resume" }
```
Render the gaze as a heatmap over the résumé sections (weight → opacity of one accent). Metered (`403 upgrade_required` past the free daily count).

### 5.6 Voice Graph / Authored Résumé (flag-gated: `VOICE_GRAPH_ENABLED`)
```
GET  /api/resume/voice-graph/samples | POST .../samples { text, sample_id?, finalize? }
POST /api/resume/voice-graph/consent | DELETE .../consent
GET  /api/resume/voice-graph/graph      → { nodes[], edges[] }
GET  /api/resume/voice-graph/voice      → voice profile (or {insufficient})
POST /api/resume/voice-graph/resume/raw       → grounded, zero-model résumé + provenance
POST /api/resume/voice-graph/resume/authored  → 202 {job_id} → voice-matched résumé (metered)
GET  /api/resume/voice-graph/resume/{id}/provenance
```
Consent is off by default; a `404` means the feature is off in prod. The writing surface should support voice dictation.

### 5.7 Apply — the mobile approval loop (§10 has the full flow)
```
GET  /api/apply/profile | PUT /api/apply/profile     // the autofill profile
POST /api/apply/answer  { question, job }            // grounded free-text answers
GET  /api/apply/answers
```
On mobile the user **reviews + approves**; the desktop extension fills + the human clicks Submit. See §11.

### 5.8 Pipeline / Referrals / Notifications / Mail
```
GET  /api/resume/pipeline/funnel | /ghosts | POST /ghosts/mark | GET /cohort
GET  /api/resume/referrals?company= | POST /api/resume/referrals/draft (→job {message})   // Premium+
POST /api/resume/linkedin-archive (multipart zip) | GET /api/resume/linkedin-archive/diff
GET/PUT /api/resume/notifications/prefs | POST /api/resume/notifications/unsubscribe
POST /api/resume/mail-ingress/provision | GET /suggestions | POST /suggestions/{id}   // MAIL_INGRESS_ENABLED
```

### 5.9 Dashboard
There is no single "dashboard" endpoint — the web app composes it client-side from `opt-clock`, `career-readiness`, `recommendations`, `pipeline/funnel`, `overview`, `streak`, saved jobs, etc. On mobile, build a **relevance-ordered home** the same way: fetch these in parallel, rank widgets, and lead with **one recommended action** (the OPT clock if urgent, else the top sponsored match, else "finish your profile").

---

## 6. Push notifications (contract for you to implement)

The backend has a channel abstraction (`services/notifications`) that today sends **email** and has WhatsApp/SMS/web-push behind flags. **Native push (APNs/FCM) is your build.** Register the device token via `PUT /api/resume/notifications/prefs` inside `channels.push` (coordinate the exact field with the backend — the web-push shape exists; a native token field is a tiny addition). Honour these rules: opt-in, **max 2 proactive notifications/day**, quiet hours, an unsubscribe path in settings.

**Notification types** (event keys) and suggested payloads:
| Event key | Trigger | Payload (data) | Deep link |
|---|---|---|---|
| `daily_digest` | morning | `{ matches:[{company,title,match,sponsorship_band}], count }` | `jobpilot://discover` |
| `evening_summary` | evening | `{ applied, statuses_moved, tomorrow_task, opt_days_remaining }` | `jobpilot://home` |
| `match_alert` | new high-fit job | `{ job_id, company, title, match }` | `jobpilot://job/{job_id}` |
| `application_nudge` | ready-to-submit tray non-empty | `{ count }` | `jobpilot://apply/tray` |
| `visa_cliff` | OPT days low | `{ days_remaining }` | `jobpilot://opt-clock` |
| `interview_reminder` | interview upcoming | `{ company, at }` | `jobpilot://interviews` |

APNs: send `alert` + `sound` + a `data` dict with `type` = the event key + the fields above. FCM: `notification` + `data`. Tapping routes via the deep-link table (§7).

---

## 7. Deep links & universal links

**Custom scheme:** `jobpilot://`. **Universal/App Links:** `https://aspirely.me/app/...`.

| Destination | Deep link | Universal link |
|---|---|---|
| Home / dashboard | `jobpilot://home` | `https://aspirely.me/app` |
| Discover | `jobpilot://discover` | `https://aspirely.me/app?tab=jobs` |
| A job | `jobpilot://job/{job_id}` | `https://aspirely.me/app/job/{job_id}` |
| Ready-to-submit tray | `jobpilot://apply/tray` | `https://aspirely.me/app?tab=apply` |
| OPT clock | `jobpilot://opt-clock` | `https://aspirely.me/app?tab=dashboard` |
| Interviews | `jobpilot://interviews` | `https://aspirely.me/app?tab=interview` |
| Learning | `jobpilot://learn` | `https://aspirely.me/app?tab=learning` |
| OAuth callback | `jobpilot://oauth/callback` | — |

The web app already reads a `?tab=<key>` param; mirror those tab keys (`dashboard, jobs, tailor, apply, interview, learning, pipeline, six-seconds, notifications, voicegraph, network, billing, profile`). Host an `apple-app-site-association` + `assetlinks.json` at `aspirely.me` (coordinate with devops).

---

## 8. Design tokens (platform-agnostic)

Colors are HSL triplets `H S% L%` (the source of truth is the web app). Convert to your platform's color type; hex approximations are given for convenience. **Support light + dark; dark is designed, not inverted.**

```json
{
  "radius": "0.7rem (≈11pt)",
  "fonts": { "display": "Plus Jakarta Sans (600/700/800)", "body": "Inter (400–800)" },
  "easing": { "standard": "cubic-bezier(0.16,1,0.3,1)", "durations_ms": { "state": 200, "surface": 350 } },
  "light": {
    "background": "220 33% 99%  (#fbfcfe)", "foreground": "222 32% 12%  (#151b28)",
    "card": "0 0% 100% (#ffffff)", "primary": "238 70% 57% (#4a54df)", "primary_foreground": "#ffffff",
    "muted": "220 20% 96% (#f1f3f7)", "muted_foreground": "220 10% 44% (#666f7d)",
    "border": "220 16% 90% (#dfe3ea)", "ring": "238 70% 57%",
    "success": "158 64% 38% (#249d76)", "warning": "32 95% 44% (#db7a06)", "destructive": "0 72% 51% (#dd3b3b)",
    "brand_from": "243 75% 59% (#5b52e6)", "brand_to": "199 89% 48% (#0fa5e8)"
  },
  "dark": {
    "background": "222 24% 8%  (#0f131b)", "foreground": "220 14% 96%  (#f2f3f6)",
    "card": "222 22% 11% (#161b24)", "primary": "235 84% 72% (#8b95f4)", "primary_foreground": "#0e1729",
    "muted": "222 16% 16% (#222831)", "muted_foreground": "220 10% 62% (#9199a6)",
    "border": "222 14% 20% (#2b313b)", "ring": "235 84% 72%",
    "success": "158 58% 52% (#41c397)", "warning": "32 92% 56% (#f0973a)", "destructive": "0 72% 57% (#e15252)",
    "brand_from": "243 84% 72% (#8f8bf3)", "brand_to": "199 90% 60% (#3ec1f5)"
  }
}
```
Rules: restrained neutral base + the indigo primary as the single accent; semantic colors (success/warning/destructive) are never decorative. Motion respects the OS "reduce motion" setting (disable entrance/settle animations; the product must be equally good without them).

---

## 9. Screens & required states

Every screen must design **all** of: loading (skeleton that matches the final layout — no spinners, no layout shift), empty, partial, error, offline, permission-denied, rate-limited, quota-exceeded. Minimum screen set for v1:

1. **Auth** — sign-in, register, forgot-password, Google button.
2. **Onboarding** — 3 steps (résumé upload w/ live parse, LinkedIn connect [skippable], the few questions). Resumable.
3. **Home** — relevance-ordered widgets + one hero recommended action; OPT clock prominent, calmer-when-urgent.
4. **Discover** — search + visa filters, job cards with sponsorship badge + visa chips, saved list.
5. **Job detail** — full JD, match reasons, sponsorship numbers, "save"/"apply"/"find a referral".
6. **Apply / Ready-to-submit tray** — the approval loop (§11).
7. **OPT clock** — calm, precise; key dates; "talk to your DSO."
8. **Six Seconds** — gaze heatmap + impression.
9. **Pipeline** — funnel, ghosts, cohort.
10. **Interview prep** — prep pack from the JD.
11. **Learning** — today's task + plan.
12. **Voice Graph** (if enabled) — writing surface, graph reveal, voice card.
13. **Notifications settings** — channels + per-event toggles + quiet hours.
14. **Account / billing** — plan, usage, upgrade.

Mobile target: fully usable at **360px width** on a mid-range Android; touch targets ≥44pt; WCAG AA contrast; screen-reader labels on the core flows.

---

## 10 & 11. The mobile approval flow (Apply)

**We never auto-submit.** On desktop, the browser extension fills the form and the human clicks Submit. On mobile, the user's job is **review + approve**, which queues a job into a **Ready-to-Submit tray** cleared later at a laptop (or in a mobile web view for mobile-friendly ATS). Flow:

1. Morning `daily_digest` push → user taps → **Discover / today's matches**.
2. User taps a job → **job detail** → **Approve** (this pre-generates the essay answers via `POST /api/apply/answer`, grounded only in the user's own history — never fabricated).
3. Approve adds the job to the **Ready-to-Submit tray** (client-side list synced to the profile/answers store). The user can read the generated answers on the train.
4. At a laptop, the extension shows the tray; each item: open → auto-filled → **Review Card** (every field, per-field confidence, amber for guessed) → the human clicks Submit on the employer's page.

The mobile app **surfaces + edits** answers; it must clearly label AI-drafted content and never present a fabricated achievement. There is a hard cap (40 fills/hr, 150/day) enforced by the extension — the mobile app just shouldn't spam approvals.

---

## 12. Full API inventory (authoritative, 227 routes)

Grouped; the mobile app needs a subset (§15). Regenerate any time from `jobpilot-backend` with the route-dump used to produce this list. Path params: `<x>` is a segment, `<path:x>` may contain slashes.

**auth** — `/api/auth/{register, login, logout, verify, check-email, check-user, validate-password, request-password-reset, reset-password, preferences(PUT), profile(GET/PUT), oauth/<provider>/authorize, oauth/<provider>/callback, oauth/providers}`

**billing** — `/api/billing/{plan, catalog, checkout, portal, upgrade, webhook}`

**jobs** — `/api/jobs/{search, batch-search, saved(GET/POST), saved/<id>(PUT/DELETE), filters(GET/PUT), analyze, fetch-jd, tailor-resume, trust-check, draft-outreach, resume(GET/POST), sponsorship, sponsorship/bulk, pipeline, pipeline/match-scores, pipeline/presets(GET/POST), pipeline/presets/<name>(DELETE), pipeline/suggest-filters, apify-key(GET/PUT/DELETE)}`

**apply** — `/api/apply/{profile(GET/PUT), answer, answers}`

**resume (core)** — `/api/resume/{upload, status, versions, generated, active(PUT), download, download-file/<s3_key>, file/<s3_key>(DELETE), extract-jd, tailor, tailor-with-jd, regenerate, batch-tailor, ats-scores, cover-letter, cover-letter/download, rewrite-bullet, save-record, salary, streak, overview, recommendations, career-readiness, usage/today, job/<job_id>}`

**resume (records / applications)** — `/api/resume/tailoring-records{, /<id>, /<id>/application(PATCH), /<id>/versions, /<id>/current-version(PUT), /<id>/chat(GET/POST), /<id>/interview-prep, /<id>/mock-evaluate, /<id>/practice-question}`

**resume (visa / OPT)** — `/api/resume/{opt-clock, visa/profile(GET/PUT), visa/timeline}`

**resume (new Compass epics)** — `/api/resume/{six-seconds, pipeline/funnel, pipeline/ghosts, pipeline/ghosts/mark, pipeline/cohort, referrals, referrals/draft, linkedin-archive(POST), linkedin-archive/diff, notifications/prefs(GET/PUT), notifications/unsubscribe, mail-ingress/provision, mail-ingress/suggestions, mail-ingress/suggestions/<id>, posts, posts/<id>/metrics(GET/POST)}`

**resume (voice graph)** — `/api/resume/voice-graph/{consent(POST/DELETE), samples(GET/POST), samples/<id>(GET/DELETE), graph, voice, prompts, resume(GET), resume/<id>, resume/<id>/provenance, resume/raw, resume/authored, merge-candidates, merge-candidates/<id>}`

**resume (connect / gmail / github / linkedin)** — `/api/resume/{connect/<p>/authorize, connect/<p>/callback, connect/<p>/disconnect, connect/status, connect/github/publish, connect/linkedin/post, github(GET), github/link, linkedin/audit, gmail/{auth-url, callback, status, sync, disconnect, suggestions, suggestions/<id>/apply, suggestions/<id>/dismiss}}`

**resume (learning / events / interview / career-copilot)** — `/api/resume/{learning/paths(GET), learning/paths/<id>(GET/DELETE), learning/paths/<id>/module/<week>/complete, learning/paths/<id>/quiz, learning/plan, events, events/<id>, events/<id>/relation, events/draft-post, events/refresh, interview/attempts, interview/coach, interview/fit, career-copilot/...}` (career-copilot is a large sub-tree — deprioritize for v1).

**admin** — `/api/admin/...` (operator console; **not** part of the consumer app).

**tech-chronicle** — `/api/tech-chronicle/...` (a news surface; optional for v1).

---

## 13. Offline behaviour & sync

- **Read-through cache** the last successful response for Home, Discover, saved jobs, OPT clock, and the résumé — show cached data instantly with a "last updated" line, then refresh.
- **Queue writes** made offline (save/unsave job, approve-to-tray, notification prefs) and replay on reconnect; these endpoints are idempotent enough to retry (`saved` upserts; `preferences` is a full `$set`).
- **Never queue LLM jobs offline** — they're async server-side; require connectivity, show a clear "needs internet" state.
- Detect connectivity and surface an unobtrusive offline banner; the OPT clock and saved data must remain viewable offline (they're the anxiety-critical bits).

---

## 14. Feature flags (what's live vs dark)

| Feature | Prod state today | Note |
|---|---|---|
| Auth, résumé, Discover, tailor, ATS, cover letter, saved jobs, interview prep, learning, career-readiness | **Live** | build against these first |
| Six Seconds, sponsorship badges, visa filters, pipeline, referrals, LinkedIn archive, notification prefs, onboarding, generated home | **Live-capable** | fully built; safe to consume |
| **Voice Graph** (`VOICE_GRAPH_ENABLED`) | **Dark** until operator flip | routes return `404` when off → hide the feature |
| **Mail-ingress** (`MAIL_INGRESS_ENABLED`) | **Dark** | `404` when off → hide |
| **Billing/Stripe** (`BILLING_ENFORCEMENT`) | **Dark** | `/checkout` returns `503 billing_not_configured` → show "coming soon" |
| WhatsApp / SMS / native-push channels | **Dark** | email works; native push is your build |

**Rule:** a `404` from a flag-gated route = "feature unavailable," hide it gracefully; a `503 billing_not_configured` = "coming soon."

---

## 15. Analytics event taxonomy

Emit these (name → key props). Keep names stable.
```
auth_signed_in {method}          onboarding_step_completed {step}    onboarding_completed {}
resume_uploaded {}               resume_parsed {ok}                  job_searched {query,filters}
job_saved {job_id}               job_viewed {job_id}                 apply_approved {job_id}
tray_submitted {job_id}          tailor_started/completed {job_id}   ats_scored {overall}
six_seconds_run {}               opt_clock_viewed {days_remaining,band}
referral_requested {company}     pipeline_viewed {}                  push_opened {type}
paywall_shown {feature}          upgrade_started {plan}              voice_sample_saved {}
```
Send with a stable anonymous device id + the authenticated user email (hashed) once known.

---

## 16. Phased delivery plan

- **Phase 1 (MVP, ~live endpoints only):** Auth (email + Google/PKCE), onboarding, Home (relevance widgets + hero), Discover + job detail + save, OPT clock, résumé view + tailor, push scaffolding. Ship this first.
- **Phase 2:** Apply approval loop + Ready-to-Submit tray, Pipeline, Six Seconds, Interview prep, notification settings, offline cache.
- **Phase 3:** Referrals + LinkedIn archive, Learning, Voice Graph (behind its flag), billing/upgrade (when `BILLING_ENFORCEMENT` flips), deep-link + universal-link polish, a11y + performance pass (LCP-equivalent, INP-equivalent, 360px Android).

**Performance budget (mobile):** first meaningful paint < 2.0s on 4G, interaction latency < 200ms, no dropped frames on the OPT clock / Discover scroll on a mid-range Android.

---

### The one thing to confirm before you start
The **native OAuth (PKCE) redirect** and the **native push token field** are the only two items where the current web-oriented backend may need a small addition. Confirm both with the backend owner in your kickoff; everything else in this document is live and stable.
