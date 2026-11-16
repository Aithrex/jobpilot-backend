# Gmail application tracker contract

Decision date: 2026-10-25.

The application tracker remains the source of the user's recorded applications.
Gmail reads job-related evidence and proposes or applies status changes to those
records. It does not send applications or create guessed applications from mail.
Career-source ingestion and historical application records are retained while the
frontend hides Career Portals, Auto Apply, Six Seconds, and Voice analysis.

## Why matching changed

The previous matcher used the first word of an employer name, substring matches,
and recruiter-domain matches. It chose the first tied record and discarded its
identity confidence before applying a high-confidence rejection. A correct
classification of an email could therefore reject the wrong role.

Matching now uses full normalized company names and role-title evidence from
the subject and body. Shared recruiter domains do not establish identity. An
automatic change requires a unique, strong identity match and a strong status
verdict. Company-only evidence stays in review. Tied roles require the user to
choose an application explicitly. Historical or conflicting outcomes and email
older than a manual status change remain review items. Mail older than an already
accepted email outcome is ignored.

The 2026-10-26 UTC verification (October 25 in Phoenix) also exposed unrelated company names in ATS social
footers and job recommendations. Employer identity now comes from the primary
application subject or explicit application content. An untracked employer in
that evidence does not fall back to a tracked footer brand. Company words inside
role titles are not subject employer evidence. Recommendation sections are
excluded from identity/classification; job-alert messages are ignored before
model work. Conditional phrases such as "if you're interested" cannot establish
an application receipt. Explicit application acknowledgements, interviews and
rejections remain valid, and same-employer duplicate roles still require review.

## Existing routes, additive fields

All routes remain below `/api/resume/gmail`; no new edge prefix is required.
The existing OAuth configuration/enablement switch, read-only scope, encrypted
token storage, JWT user scope and asynchronous sync remain in place.

- `POST /sync` returns HTTP 202 and `{ok, job_id, status}`. The existing
  `/api/resume/job/<id>` polling API returns the result. New result fields are
  `has_more: boolean` and `ambiguous_matches: number`, alongside
  `messages_scanned`, `auto_applied`, `suggested`, `ignored`, `records_with_mail`,
  `skipped_no_match`, and `skipped_already_in_status`. `auto_applied` counts
  **status updates**, not application submissions.
- `GET /status` adds `has_more` and `last_sync_summary`. `pending_suggestions`
  counts the complete validated pending review list, including later pages.
  The completed
  `last_synced_at` watermark advances only after all pages in a scan complete.
  Continuation tokens remain private to the backend.
- `GET /suggestions` preserves existing fields and adds `match_confidence`,
  `match_reason`, `candidate_records: [{record_id, company, title, applied_date?, created_at?}]`,
  `requires_record_selection`, and `email_received_at` (UTC ISO timestamp or
  null). `confidence` describes the status verdict; `match_confidence` describes
  application identity. Existing source/evidence message IDs remain available.
  Optional candidate dates distinguish repeated applications for the same role;
  `applied_date` is the stored civil day and `created_at` is an ISO timestamp.
  Existing pending suggestions are revalidated on read and before apply using
  retained subject/sender/snippet evidence: clear employer contradictions are
  pruned and recommendation-only items are excluded. Filtering does not write or
  delete stored documents; resolved history retains its original identity. The
  page cap is applied after filtering, so invalid rows cannot hide older
  valid review items. `GET /suggestions?offset=0` returns
  `{suggestions, total, has_more, offset}` with up to 100 rows per page and a
  stable creation-time/id order. `offset` must be a nonnegative integer.
  Plain-text previews decode HTML entities without rendering
  HTML.
- `POST /suggestions/<id>/apply` accepts an optional JSON `record_id`. An
  ambiguous item requires it. A selection must be in the suggestion's candidate
  list and still belong to the JWT user. Invalid/unresolved selections return
  400; inaccessible suggestions or records return 404. The result remains
  `{suggestion}`. Empty-body requests remain valid for unambiguous suggestions.

## Low-cost evidence review (2026-10-26 UTC)

The remaining mailbox scan uses deterministic rules with **zero model calls by
default**. `GMAIL_MODEL_CALLS_PER_BATCH` optionally enables 0–3 FLASH attempts per
batch; invalid configuration becomes zero. There is no PRO fallback or model
retry. The full bounded batch is evaluated by rules; only optional model context
is capped at ten messages. Summary fields `model_calls` and `deferred` report
attempts and newly retained unknown-outcome review rows respectively.

When matched application mail has no reliable verdict (including an unavailable
model or exhausted optional budget), the evidence is persisted before advancing
the cursor. Such rows have `suggested_status: null` and
`requires_status_selection: true`. Applying one requires **both** an explicit
owned candidate `record_id` and a `status` from `applied`, `interviewing`, `offer`,
`rejected`, `ghosted`, or `withdrawn`. A known suggested status cannot be
overridden by that parameter. Resolution locks retain both selected application
and status across retries, preventing conflicting decisions from two tabs.

Complete role titles now preserve seniority and specialty: Software Engineer
does not establish Senior Software Engineer, Software Engineer II, or Software
Engineer, Workflow Systems. Known conflicting requisition IDs in stored job
URLs exclude candidates; an email requisition not confirmed in a record requires
review. Duplicate same-title applications still require a user choice unless
other identity evidence uniquely distinguishes them. Positive decisions such as
"decided to offer" no longer trigger the rejection rules' former optional
negation patterns.

Mail for an explicit untracked/conflicting role remains reviewable with
`requires_application_match: true`, `record_id: null`, and no candidate records.
It cannot update a different same-company application. Retained `email_roles`
and `email_requisition_ids` support read/apply revalidation; legacy truncated
role snippets only exclude candidates that contradict the known title prefix.
No guessed application is created, and resolved historical decisions remain
immutable. Unknown outcomes and unmatched roles may be reviewed directly in
Gmail using their retained message/thread identifiers.

Live batch follow-up also found role captures containing ATS sentence wrappers
and signatures. Only recognized prose is removed (`our`, `current`, `taking the
time to apply for`, `interview logistics for`, trailing `role`/`opening`, and
thank-you/signature text). Seniority, specialties, employment type and location
remain part of the title; requisition evidence is retained separately. Previously
unmatched rows can recover candidate choices from corrected retained role
evidence plus one evidenced employer. Recovery is read-only and always requires
explicit application selection, even with one candidate. It never applies a
status during listing, and successful manual resolution clears unmatched flags.

Unmatched rows show no current application status or match confidence. Their
company label comes from one subject-evidenced employer and an owned record's
readable name; uncertain employer labels are blank. Resolved history remains
unchanged. Narrow authentication-notice subject patterns (verification/security
codes, Google account-data-sharing notices, and exact identity-confirmation,
candidate-account verification and password-reset notices) are excluded before
matching and when reading/applying old pending rows. This is not a general
`verify` filter: interview availability and assessment-verification messages
remain eligible for review.
Unclear application portal updates, interview logistics and consent documents
remain reviewable.

## Scan behavior and limits

A first connection examines the previous 60 days in bounded batches of up to
50 messages. A fixed date window plus Gmail page token lets **Continue sync**
resume older pages without skipping them. New arrivals belong to the next scan.
Listing or message-fetch errors leave the completed watermark unchanged. A
per-account lease prevents concurrent scans from racing that cursor; its expiry
allows recovery after a terminated worker. Deterministic suggestion IDs make
replays idempotent. Rejected/dismissed review decisions do not reappear on replay.
Relinking clears the old mailbox cursor; workers fence their writes by the
original lease. Resolving a suggestion also claims it atomically before updating
an application, so competing role choices cannot update two applications. A
failed resolution can retry the same selected application.

Live verification on 2026-10-25 (America/Phoenix) found a per-user query-cost quota error during a
full-message read. The previous generic 403 message incorrectly suggested an
OAuth setup failure. Google's current [quota table](https://developers.google.com/workspace/gmail/api/reference/quota)
charges 60 units for a full-message read, against a default of 6,000 units per
minute per user for new projects. The former 250-message burst could consume
15,000 units before list requests. The actual project's quota value was not
present in the truncated provider log; no cloud quota or OAuth settings changed.

Reads are now spaced at least one second apart. Listing, full-message reads,
and all retries share a 180-second fetch budget, leaving the async worker time
for classification. Transient 403 rate limits, 429s, and transient 5xx responses
use up to seven attempts with exponential backoff and jitter. `Retry-After`
seconds or HTTP dates are respected; a delay beyond the remaining budget stops
the job rather than retrying early. Exhausted quota errors tell the user to try
again later while retaining the connected state and previous scan position.
Disabled API, missing permission, domain policy, and daily quota errors remain
distinct and do not trigger transient retries. Logs retain only status and a
known reason category, never raw provider bodies or mailbox message IDs.

Gmail remains unavailable when deployment credentials or `GMAIL_OAUTH_ENABLED`
do not permit it. This code does not change Google verification or deployment
configuration. The UI must present that state honestly.

## Tracker history paging

`GET /api/resume/tailoring-records?view=list&page_size=100&offset=0` returns
`{records, next_offset, total}`. `next_offset` is null at completion. `total` is
scoped to the current user. Page size is bounded at 100, with a stable descending
`created_at`/`record_id` order. The slim projection remains in Mongo, so resume
bodies and version arrays do not cross the wire. Unpaged consumers retain their
existing response. Clients must finish paging before calling a loaded subset a
total.

## Verification

Offline regressions cover same-employer roles, first-word collisions, role text
in the message body, generic recruiter domains, ambiguous selection, cross-user
access, replay, old/new conflicting outcomes, preservation of known application
dates, scan continuation, failed message fetches, quota backoff and deadlines,
preserved cursors after partial fetch failures, API validation, and slim-list
paging. All fixtures use the repository's mongomock guard. No live mailbox or
database is required.

## Sync runs, reconnect, activity and undo (2026-11-15)

Everything here is additive. Old clients keep working.

### One press, one run

`POST /api/resume/gmail/sync` starts a **run**. A run reads batch after batch
until the mailbox window is finished. The work is cut into job slices of about
90 seconds so a deploy never waits on it; each slice starts the next one. If a
slice cannot start the next (a deploy, a crash), the run is `paused` and the
scheduled sync (`gmail_sync`, every 30 minutes) continues it.

Response `202 {ok, job_id, run_id, status: "started"}`. If a run is already
active the answer is `202 {ok, run_id, status: "running", already_running: true}`
and no second run starts. Failures carry a `code`:

| status | code | meaning |
|---|---|---|
| 400 | `gmail_not_connected` | no connection |
| 409 | `gmail_reconnect_required` | Google no longer accepts the saved access |
| 429 | `rate_limited` | more than 6 starts in 5 minutes |
| 503 | `gmail_unavailable` | switched off or not configured |

Progress is read from **`GET /api/resume/gmail/status`**, not from the job.
`?light=1` skips the review count (`pending_suggestions: null`) for polling.

### Status

```
{
  configured, connected, gmail_address, linked_at, last_synced_at,
  pending_suggestions, has_more, last_sync_summary,
  state: "unavailable" | "not_connected" | "needs_reconnect" | "never_synced"
       | "syncing" | "paused" | "synced",
  needs_reconnect: bool,
  reconnect_reason: "expired_or_revoked" | "permission_missing" | null,
  tracked_applications: int,
  sync: null | {
    run_id, trigger: "manual" | "scheduled",
    state: "running" | "paused" | "completed" | "failed",
    started_at, updated_at, finished_at,
    batches, messages_scanned, auto_applied, suggested, deferred, ignored,
    outcome, error_code, error
  },
  auto_sync: {enabled: bool, interval_hours: int},
  access: {mode: "testing" | "production", expires_at: iso | null}
}
```

`state` is the one field a client needs to choose a screen:

- `unavailable`: not configured (credentials, the encryption key, or the
  `GMAIL_OAUTH_ENABLED` switch).
- `needs_reconnect`: the saved access was rejected. Only a new consent fixes it.
- `never_synced`: connected, no finished run.
- `syncing`: a run is reading mail now. `sync` carries the running totals.
- `paused`: mail remains and no slice is running. It continues by itself.
- `synced`: the last run finished. `last_sync_summary` is that run's totals.

`sync.outcome` and `last_sync_summary.outcome` say why a finished run ended the
way it did:

| outcome | meaning |
|---|---|
| `no_applications` | the tracker is empty, so no mail was read |
| `no_mail` | no new mail in the window |
| `no_matches` | mail was read, none matched a tracked application |
| `up_to_date` | matching mail agreed with the tracker already |
| `updated` | at least one status moved or one item needs review |

`last_sync_summary` also carries `tracked_applications` and, for a first scan,
`window_days` (60).

`access.mode` comes from `GOOGLE_OAUTH_CONSENT_MODE` (`testing` unless set to
`production`). In testing mode Google ends every connection 7 days after
consent, so `access.expires_at` is `linked_at + 7 days`. It is null in
production mode.

### Error codes on a failed run

`sync.error_code` (and the job's `error_code`): `gmail_reconnect_required`,
`gmail_rate_limited`, `gmail_unavailable`, `gmail_policy_blocked`,
`gmail_time_limit`, `gmail_error`. `sync.error` is always a sentence written
for the user. Provider bodies and exception text never reach a response.

### Callback

`POST /api/resume/gmail/callback` still needs the signed-in user: the state is
bound to the account that asked for it, and that check is what stops a consent
link sent to someone else from attaching their mailbox to the sender's account.
Failures are `400` with a `code` and a sentence, never a raw exception and
never 401 for a Google problem:

| code | meaning |
|---|---|
| `missing_params` | no code or state |
| `state_invalid` | expired, used, or issued to another account |
| `code_rejected` | Google refused the code (expired or reused) |
| `permission_missing` | the Gmail read box was not ticked on Google's screen |
| `no_refresh_token` | Google returned no long-lived access |
| `gmail_unavailable` (503) | switched off or not configured |

### What a sync changed, and undo

`GET /api/resume/gmail/activity?limit=20` returns the automatic moves of the
last 30 days, newest first:

```
{changes: [{
  suggestion_id, record_id, company, title,
  from_status, to_status, subject, from_address, email_received_at,
  applied_at, sync_run_id, undoable: bool, undone: bool
}]}
```

`POST /api/resume/gmail/suggestions/<id>/undo` puts the application back to
`from_status`. It only works for an automatic move, and only while the
application still holds the status Gmail gave it:

- `200 {suggestion, restored_status}`
- `404` unknown suggestion
- `409 {code: "not_undoable"}` not an automatic move, or already undone
- `409 {code: "status_changed"}` the status was changed again afterwards

An undone email is never applied automatically again; the undo counts as the
user's own status change, which later evidence must be newer than.

### Scheduled sync

The worker's `gmail_sync` job (every 30 minutes, inline, one user at a time)
syncs connections that are due: an unfinished run, or a last finished scan
older than `GMAIL_AUTO_SYNC_HOURS` (default 6). It skips connections that need
a reconnect, users with no tracked applications, and users who have not signed
in for `GMAIL_AUTO_SYNC_ACTIVE_DAYS` (default 30). `GMAIL_AUTO_SYNC_ENABLED=false`
turns it off without touching manual sync.
