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
- `GET /status` adds `has_more` and `last_sync_summary`. The completed
  `last_synced_at` watermark advances only after all pages in a scan complete.
  Continuation tokens remain private to the backend.
- `GET /suggestions` preserves existing fields and adds `match_confidence`,
  `match_reason`, `candidate_records: [{record_id, company, title}]`,
  `requires_record_selection`, and `email_received_at` (UTC ISO timestamp or
  null). `confidence` describes the status verdict; `match_confidence` describes
  application identity. Existing source/evidence message IDs remain available.
- `POST /suggestions/<id>/apply` accepts an optional JSON `record_id`. An
  ambiguous item requires it. A selection must be in the suggestion's candidate
  list and still belong to the JWT user. Invalid/unresolved selections return
  400; inaccessible suggestions or records return 404. The result remains
  `{suggestion}`. Empty-body requests remain valid for unambiguous suggestions.

## Scan behavior and limits

A first connection examines the previous 60 days in bounded batches of up to
250 messages. A fixed date window plus Gmail page token lets **Continue sync**
resume older pages without skipping them. New arrivals belong to the next scan.
Listing or message-fetch errors leave the completed watermark unchanged. A
per-account lease prevents concurrent scans from racing that cursor; its expiry
allows recovery after a terminated worker. Deterministic suggestion IDs make
replays idempotent. Rejected/dismissed review decisions do not reappear on replay.
Relinking clears the old mailbox cursor; workers fence their writes by the
original lease. Resolving a suggestion also claims it atomically before updating
an application, so competing role choices cannot update two applications. A
failed resolution can retry the same selected application.

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
dates, scan continuation, failed message fetches, API validation, and slim-list
paging. All fixtures use the repository's mongomock guard. No live mailbox or
database is required.
