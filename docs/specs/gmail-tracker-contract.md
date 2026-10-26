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
  counts the same validated review list shown by `GET /suggestions`, bounded
  at 100 visible items rather than all historical pending documents. The completed
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
  visible cap is applied after filtering, so invalid rows cannot hide older
  valid review items. Plain-text previews decode HTML entities without rendering
  HTML.
- `POST /suggestions/<id>/apply` accepts an optional JSON `record_id`. An
  ambiguous item requires it. A selection must be in the suggestion's candidate
  list and still belong to the JWT user. Invalid/unresolved selections return
  400; inaccessible suggestions or records return 404. The result remains
  `{suggestion}`. Empty-body requests remain valid for unambiguous suggestions.

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
