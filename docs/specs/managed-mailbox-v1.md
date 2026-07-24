# Managed Application Mailbox + Inbox — Backend Contract v1

Status: **DRAFT / contract-first** (no code until this is agreed). Owner: platform.
Supersedes the SES assumptions in the auto-apply mega-spec for the *inbound* path.

This is the authoritative FE<->BE contract for the "apply from a managed address"
feature (the Tsenta-style relay). It is a **new, separate feature** from the existing
`mail_ingress_service.py` ("forward your own Gmail to us"). See §9 for how the two
relate and what we reuse.

---

## 0. Decisions locked (do not re-litigate)

| # | Decision | Value | Rationale |
|---|----------|-------|-----------|
| D1 | Address format | `firstname.lastname@<APPLY_MAIL_DOMAIN>` | Human-readable, ATS-dedup-friendly, matches competitor. User-chosen over opaque. |
| D2 | Collision rule | `firstname.lastname`, then `firstname.lastname2`, `…3`, capped at N; final fallback `firstname.lastname.<4-char>` | Two identical names cannot share one address; suffix only on real collision. |
| D3 | Domain | **A new neutral registered domain** we control (NOT aithrex.tech / aspirely.me / jobpilot). Config: `APPLY_MAIL_DOMAIN`. | The address is shown to employers; it must read as the candidate's private email, not our brand. |
| D4 | Inbound transport | **Cloudflare Email Routing catch-all -> thin Email Worker -> HMAC-signed webhook** to the backend | Free, real-time, no per-user mailbox, no SES. |
| D5 | Outbound transport | Existing **ZeptoMail** (`email_service`) from a JobPilot-owned verified sender | Already wired; never spoof the recruiter. |
| D6 | Message storage | Full raw MIME quarantined in **S3**; queryable metadata in **Mongo**. Never base64/MIME in Mongo. | Inbox needs real bodies/attachments; privacy + cost. |
| D7 | Owner identity | Always resolved from JWT/server state; **never** from request body or a name parsed from a resume | IDOR + identity-spoof defense. |
| D8 | Guessable-address risk | Mitigated at the **processing** layer: inbound mail only drives a status/OTP transition when it correlates to a real application the user submitted, from the expected ATS sender, within the execution window. Unsolicited mail is stored in a low-trust bucket and never auto-transitions. | "Evidence, not proof" — a guessable local part cannot be weaponized. |

---

## 1. Address lifecycle

### 1.1 Provisioning
- One **stable primary** managed address per user, minted on first Auto-Apply onboarding.
- Local part = slug of the user's verified name: lowercase, ASCII-fold, non-alnum -> `.`,
  collapse repeats, strip leading/trailing `.`, max 40 chars. `Harshith Manne` -> `harshith.manne`.
- Collision handling per D2. The chosen local part is **immutable** for the life of the mailbox
  (ATS platforms dedup candidates by email — never silently change it under active applications).
- Optional **privacy alias**: user may instead mint an opaque `career.<6-char>@domain` if they do
  not want their name exposed. Same infrastructure; different local-part generator.

### 1.2 Controls (all owner-scoped, JWT-derived)
`copy`, `pause` (stop accepting/acting on mail), `rotate` (mint a new primary, tombstone the old —
old alias is retired, **never recycled to another user**), `retire`, forwarding on/off,
forwarding-destination re-verification. Changing the account email must **not** silently move mailbox
ownership.

### 1.3 Tombstones
Retired/rotated aliases live forever in `mail_aliases` with `state="retired"` so the local part can
never be reassigned. Uniqueness is enforced across live **and** tombstoned aliases.

---

## 2. Collections (Mongo `aspirely_db`)

All documents carry `user_email` (owner) and are queried owner-scoped. Timestamps are UTC datetimes.

### `managed_mailboxes` — one per user
```
{ user_email, mailbox_id, primary_alias_id, mode: "copilot"|"approval"|"autopilot",
  paused: bool, forwarding_enabled: bool, forward_to: <verified original email>,
  forward_to_verified: bool, created_at, updated_at }
```
Indexes: `uniq user_email`, `uniq mailbox_id`.

### `mail_aliases` — every address ever minted (live + tombstoned)
```
{ alias_id, mailbox_id, user_email, local_part, address, kind: "primary"|"privacy"|"reply_bound",
  state: "active"|"paused"|"retired", thread_id?（reply_bound only）, created_at, retired_at? }
```
Indexes: `uniq local_part` (case-folded, spans all states — the anti-recycle guard),
`uniq alias_id`, `by mailbox_id`, `by user_email`.

### `mail_threads` — a conversation, optionally bound to an application
```
{ thread_id, user_email, alias_id, application_id?, subject, participants: [addr…],
  message_count, unread_count, last_message_at, trust: "verified"|"unverified",
  status_suggestion?: {suggested, confidence, evidence_message_id}, created_at, updated_at }
```
Indexes: `uniq thread_id`, `by (user_email, last_message_at desc)`, `by (user_email, application_id)`.

### `mail_messages` — one inbound/outbound message (metadata only; body in S3)
```
{ message_id, provider_message_id, thread_id, user_email, alias_id,
  direction: "inbound"|"outbound",
  from_addr, from_name, to_addr, subject,
  received_at, spf: pass|fail|none, dkim: pass|fail|none, dmarc: pass|fail|none,
  raw_s3_key, text_s3_key?, safe_html_s3_key?,
  size_bytes, part_count, attachment_ids: [id…],
  read: bool, is_phishing_reported: bool,
  classification?: {status, confidence, evidence_sentence},  # reuses the closed schema
  created_at }
```
Indexes: `uniq (alias_id, provider_message_id)` (idempotency, per D8/spec),
`by thread_id`, `by (user_email, read)`.

### `mail_attachments` — one file (bytes in S3)
```
{ attachment_id, message_id, user_email, filename, declared_content_type,
  sniffed_content_type, size_bytes, sha256, s3_key, quarantined: bool,
  scan_verdict: "clean"|"suspicious"|"blocked", created_at }
```
Indexes: `uniq attachment_id`, `by message_id`.

> **No TTL on any of these** — the Inbox is durable. (Contrast the legacy `email_ingress`
> body-free rows, which keep their 30-day TTL.) Consent/audit rows are never TTL'd.

---

## 3. S3 layout (bucket `APPLY_MAIL_BUCKET`, SSE-KMS)

```
mail/raw/{user_email_hash}/{message_id}.eml            # immutable raw MIME (quarantine)
mail/text/{user_email_hash}/{message_id}.txt           # extracted plain text
mail/html/{user_email_hash}/{message_id}.sanitized.html# server-sanitized HTML (images stripped)
mail/att/{user_email_hash}/{attachment_id}/{filename}  # validated attachment bytes
```
All GET/PUT from workers/clients use **short-lived presigned URLs**; no bucket-wide grants.

---

## 4. Inbound contract (Cloudflare -> backend)

The two machine endpoints below ride the **already-routed `/api/resume/*` edge route** (like the
existing `/api/auth/ses-notifications` machine webhook rides `/api/auth`). **No new top-level prefix,
so no new external Terraform edge route is required.** They are JWT-exempt; auth is HMAC.

Two-phase protocol (dodges the API Gateway 10 MB body cap; raw MIME can be up to 25 MiB and goes
straight to S3, never through API Gateway):

### 4.1 Cloudflare Email Worker (thin; lives in `infra/cloudflare/email-worker/`)
For every inbound message on the catch-all:
1. Read `message.rawSize`; if > `MAIL_MAX_RAW_BYTES` (25 MiB) -> `message.setReject("552 message too large")`.
2. Compute `sig = HMAC-SHA256(MAIL_INBOUND_HMAC_SECRET, canonical)` where
   `canonical = envelopeTo\nenvelopeFrom\nmessageId\nrawSize` (hex).
3. **Phase A (slot):** `POST /api/resume/mail-inbound/cloudflare/slot` JSON
   `{envelope_to, envelope_from, message_id, raw_size}` + header `X-JP-Signature: sig`.
   - 404 -> unknown alias -> `message.setReject("550 5.1.1 no such user")`.
   - 200 -> `{upload_url, put_headers, commit_token}` (a **one-use, single-object, short-TTL presigned
     S3 PUT** for `mail/raw/{hash}/{message_id}.eml`).
4. **PUT** `message.raw` bytes to `upload_url` with `put_headers` (direct to S3; the Worker holds **no**
   AWS creds — the presigned URL is the only capability, one object, one use).
5. **Phase B (commit):** `POST /api/resume/mail-inbound/cloudflare/commit` JSON
   `{commit_token, raw_sha256}` + `X-JP-Signature` over `commit_token\nraw_sha256`.
   - 2xx -> done. 5xx -> throw (Cloudflare retries). 4xx (already committed) -> treat as done.

The Worker does **no parsing** and holds **no secrets** beyond the HMAC key (a Cloudflare secret binding)
and the ephemeral presigned URL.

### 4.2 Backend endpoints (blueprint mounted in the `resume` service; JWT-exempt, HMAC-authed)
`POST /api/resume/mail-inbound/cloudflare/slot`
- Verify HMAC (constant-time) over the canonical string. Fail -> **403** (machine route; 403 does not
  trip the FE 401-logout rule).
- `resolve_alias(envelope_to)` (envelope recipient is authoritative, NOT MIME `To`). Unknown/retired ->
  **404** (never reveal whether another user owns an alias).
- Idempotency pre-check on `(alias_id, provider_message_id=message_id)`; already-seen -> return a
  `commit_token` flagged done so commit is a no-op.
- Mint a `commit_token` (opaque, short-TTL, bound to `{alias_id, message_id, raw_s3_key}`, stored in a
  TTL collection) + a one-use presigned PUT for the raw key. Return them.

`POST /api/resume/mail-inbound/cloudflare/commit`
- Verify HMAC; look up `commit_token` (reject if missing/expired/used). Mark used (atomic).
- Download raw from S3 -> enforce limits (§6) -> parse -> validate attachments (file-signature) ->
  sanitize HTML (strip remote images/scripts) -> upsert thread/message/attachments (idempotent on
  `(alias_id, provider_message_id)`) -> correlate to application (§8) -> classify (reuse
  `mail_ingress_service.classify`) as a **suggestion only** -> fire forwarding (Task P1-forward).
- Heavy scan/classify may be offloaded to the async-job mechanism; return `{ok, thread_id, message_id,
  deduplicated}` after durable storage.

> **No new edge route** (rides `/api/resume/{proxy+}`). The resume Lambda already has S3 + Bedrock
> access, which the pipeline needs.

---

## 5. Endpoints (JWT-required unless noted) — `/api/resume/mailbox/*`

Mounted under the already-routed `/api/resume` prefix to avoid a second new edge route for the
user-facing surface (only the machine `/api/mail-inbound` needs a new route).

| Method | Path | Body / Query | Returns |
|--------|------|--------------|---------|
| GET  | `/api/resume/mailbox` | — | `{provisioned, address, mode, paused, forwarding_enabled, forward_to, forward_to_verified, unread_count}` |
| POST | `/api/resume/mailbox/provision` | `{privacy_alias?: bool}` | mailbox object (idempotent) |
| POST | `/api/resume/mailbox/pause` | `{paused: bool}` | mailbox object |
| POST | `/api/resume/mailbox/rotate` | — | mailbox object (new address) |
| POST | `/api/resume/mailbox/forwarding` | `{enabled: bool, forward_to?: email}` | mailbox object (re-verify if changed) |
| GET  | `/api/resume/mailbox/threads` | `?trust=&application_id=&unread=` | `{threads: [ThreadSummary]}` |
| GET  | `/api/resume/mailbox/threads/<thread_id>` | — | `{thread, messages: [MessageView]}` |
| POST | `/api/resume/mailbox/threads/<thread_id>/read` | `{read: bool}` | `{ok}` |
| POST | `/api/resume/mailbox/threads/<thread_id>/attach` | `{application_id}` | `{ok}` |
| POST | `/api/resume/mailbox/threads/<thread_id>/detach` | — | `{ok}` |
| GET  | `/api/resume/mailbox/attachments/<attachment_id>` | — | `{download_url}` (presigned, short TTL) |
| POST | `/api/resume/mailbox/threads/<thread_id>/reply` | `{body}` | `{job_id}` (async send via ZeptoMail) |
| POST | `/api/resume/mailbox/threads/<thread_id>/report-phishing` | — | `{ok}` |

**Exact shapes (enumerated — both sides type to these verbatim; unlisted fields are a bug):**

`ThreadSummary` = `{thread_id, subject, participants:[addr], application_id?, message_count,
unread_count, last_message_at, snippet, last_from, has_attachments, trust, status_suggestion?}`
(`snippet`/`last_from`/`has_attachments` are denormalized onto the thread at ingest so the list needs
no per-thread body fetch).

`MessageView` = `{message_id, from, from_name, subject, received_at, spf, dkim, dmarc, trust, read,
text, safe_html, safe_html_url, attachments:[{id, filename, size, verdict}]}`. `text` = inline
sanitized plain text; `safe_html` = inline **sanitized** HTML (the FE's primary render path, in a
sandboxed iframe via `srcdoc`); `safe_html_url` = a short-TTL presigned GET of the same sanitized S3
object (large-body fallback). The **raw MIME is never exposed** in any field. Remote images are
neutralized server-side; the FE sandboxes again (defense in depth).

Error contract: business failures 400/403/404/409/429 — **never 401** (invariant #1). Long work
(reply send, full attachment scan) -> `{job_id}` polled via the existing async mechanism.

---

## 6. Attachment & MIME limits (fail-closed)

`MAIL_MAX_RAW_BYTES` (25 MiB), `MAIL_MAX_HEADERS` (200), `MAIL_MAX_PARTS` (50),
`MAIL_MAX_DEPTH` (10), `MAIL_MAX_DECODED_BYTES` (40 MiB), `MAIL_MAX_ATTACHMENTS` (20),
`MAIL_MAX_ATTACHMENT_BYTES` (15 MiB). Reject encrypted/nested archives and zip bombs (expansion
ratio cap). **Validate file signatures**, not extensions. Enforce SPF/DKIM/DMARC and any spam/virus
verdict; isolate suspicious attachments (`scan_verdict="suspicious"`, download blocked). Never execute
attachments, scripts, forms, or auto-follow links.

---

## 7. Config vars (via `utils/config`; prod via SSM)

| Var | Example | Notes |
|-----|---------|-------|
| `APPLY_MAIL_DOMAIN` | `pilotmail.co` | The neutral domain. Empty -> feature inert. |
| `APPLY_MAILBOX_ENABLED` | `false` | Master flag; OFF until domain + Cloudflare live + staging smoke passes. |
| `MAIL_INBOUND_HMAC_SECRET` | (SSM) | Shared with the Cloudflare Worker secret binding. |
| `APPLY_MAIL_BUCKET` | `jobpilot-mail-prod` | SSE-KMS bucket for raw/att. |
| `APPLY_MAIL_KMS_KEY_ID` | (SSM) | CMK for the bucket. |
| `MAIL_FORWARD_FROM` | `notifications@aspirely.me` | JobPilot-owned verified relay sender (never the recruiter). |

SSM mapping added to `ssm_config.py`; declared in `.env.example`, `config.py`, `lambda.tf`.

---

## 8. Application correlation (D8 — the guessable-address defense)

An inbound message is bound to an application (and may *suggest* a status change) **only** when:
- its `from` domain matches the ATS/employer domain recorded on an application the user actually
  submitted, **and** it arrived after that application's submission/execution began, **and**
- for OTP: the alias, ATS sender/domain policy, and an **active** execution all match; the OTP is
  exposed to the worker via a one-use short-lived capability, never the whole mailbox.

Otherwise the message lands in the **`unverified`** trust bucket: visible in the Inbox, but it can
**never** auto-transition application state, auto-surface an OTP, or auto-forward with full content.
Classification (LLM) may *propose*; deterministic evidence + confidence gate + code decides.

### 8.1 Worked acceptance scenario (the "Greenhouse rejection" case — must pass for any user)

1. `harshith@aithrex.tech` (registration email) enables Auto-Apply -> we provision
   `harshith.manne@pilotmail.co`; `forward_to=harshith@aithrex.tech` (verified).
2. Executor submits a Greenhouse role using `harshith.manne@pilotmail.co` as the applicant email;
   an `application` row is written with `ats="greenhouse"`, `employer_domain`, `submitted_at`.
3. Rejection email is sent by the employer/Greenhouse to `harshith.manne@pilotmail.co`.
4. Cloudflare catch-all -> Worker -> `POST /api/mail-inbound/cloudflare` (envelope-to authoritative).
5. Backend resolves the alias -> owner; quarantines raw to S3; stores thread+message.
   Correlation matches the submitted Greenhouse application (`trust="verified"`); classifier =
   `rejected` + grounded evidence + high confidence.
6. **Website:** application transitions to `rejected` in the tracker (canonical `applications`;
   interim: `tailoring_records.application.status`).
7. **Original inbox:** forwarding sends, from `MAIL_FORWARD_FROM` (JobPilot-owned, never spoofing the
   recruiter), a rejection notification + secure deep link to `/next/applications/<thread_id>` to
   `harshith@aithrex.tech`.

Both outcomes are evidence-driven: an `unverified` message that does not correlate to a submitted
application does neither (it only appears in the Inbox).

---

## 9. Relationship to existing `mail_ingress_service.py`

| | Legacy `mail_ingress` (Track A) | This spec (Managed Mailbox) |
|---|---|---|
| Address | opaque `u_<hex>@inbox.jobpilot.app` | `firstname.lastname@<neutral>` |
| User applies with | their OWN email; forwards recruiter mail to us | **this** managed address |
| Storage | body-free (30d TTL) | full threads/messages/attachments (durable) |
| Inbound | SES->SNS webhook (never wired) | Cloudflare Worker->HMAC webhook |

**Reused verbatim:** `classify()` (untrusted-input-hardened FLASH classifier + evidence grounding),
the closed `STATUS_ENUM`/`_STATUS_TO_TRACKER` maps, `resolve_user`-style alias resolution, the
idempotency-by-message-id pattern, `parse_raw_email` (extended for attachments). The legacy
"forward your Gmail" surface stays as an optional secondary signal; it is not deleted.

---

## 10. Test plan (TDD — write first)

Contract: settings/provision request+response shape; alias collision -> suffix; alias tombstone
anti-recycle; concurrent provision (one address). Inbound: HMAC accept/reject; forged MIME `To`
ignored in favor of envelope; shared/duplicate provider-message-id idempotency; zip-bomb/oversize/
part-count/attachment limits; file-signature vs extension mismatch; HTML/XSS sanitization + remote
images off. Correlation: unverified mail never auto-transitions; OTP requires active execution +
expected sender. IDOR: every mailbox/thread/message/attachment endpoint cross-user 404. Relay:
open-relay attempt blocked; reply only to the single pre-bound correspondent after all checks.

---

## 11. Rollout

Flag `APPLY_MAILBOX_ENABLED=false` until: neutral domain registered + on Cloudflare, catch-all +
Worker deployed, `MAIL_INBOUND_HMAC_SECRET` in SSM + Worker, S3 bucket + KMS provisioned, edge route
for `/api/mail-inbound/*` applied, and the staging round-trip test (send -> Worker -> webhook ->
Inbox -> forward) passes. Then internal -> canary -> beta.

## 12. Manual/external steps (cannot be done from this repo)
1. Register the neutral domain (user provides the name).
2. Add it to Cloudflare, set MX to Cloudflare Email Routing, create the **catch-all** rule -> Worker.
3. Deploy the Email Worker (`wrangler deploy`); set its `MAIL_INBOUND_HMAC_SECRET` + `MAIL_INBOUND_URL`
   secret/var bindings.
4. Create the SSE-KMS S3 bucket + CMK; grant the resume Lambda role `s3:PutObject`/`GetObject` on
   `mail/*` + presign rights; put `MAIL_INBOUND_HMAC_SECRET`, `APPLY_MAIL_KMS_KEY_ID` in SSM.
5. Verify `MAIL_FORWARD_FROM` sender/domain in ZeptoMail (SPF/DKIM).

> No new API-Gateway/edge route is required — the machine webhook rides `/api/resume/{proxy+}` and raw
> MIME goes directly to S3 via presigned PUT (never through API Gateway).
