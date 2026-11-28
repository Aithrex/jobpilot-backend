# Browser Radar (`radar-1`)

**Status:** built 2026-11-19; writes are ON by default for contributor
accounts (the owner's call, 2026-11-19). Code:
`shared/python/services/browser_radar_service.py`, routes in
`services/jobs/blueprints/jobs.py`, tests `tests/test_browser_radar.py`. The
extension half lives in the ApplyPilot repo
(`.kiro/specs/r1-workday-radar/contract.md` is the same contract).

## Why

Since about 2026-11-14 Workday's job sites sit behind Cloudflare bot
management. The crawler's box (150.136.238.50) gets `429 Security Check`
(`cf-mitigated: challenge`) on most Workday requests, so Workday listings fell
from ~3M fetched a day to ~37k and ~13.8k open postings were closed as
`unverified` before the guards in #62 landed. A person's own browser still
reads the same public listings with plain `200`s
([measurements](../coverage/workday-alternatives-2026-11-17.md)).

ApplyPilot, the owner's Chrome extension, reads Workday boards for its owner's
own job search, one request at a time, at least 6 s apart, and stops at the
first challenge. This feature lets it (1) ask the server which boards are
worth reading and (2) send what it saw back, so the shared inventory, and
every user's board, gets those postings.

**The crawler does not change and does not depend on this.** It is one more
source. It covers a sliver of the outage (see "Size" below), not all of it.

## The rules

- **The server re-derives everything.** The browser sends Workday's raw CXS
  fields (`title`, `externalPath`, `bulletFields`, `postedOn`,
  `locationsText`, `timeType`, and a detail's `jobPostingInfo`). The server
  runs `WorkdayProvider._to_raw` and `fetch_detail` (through a stand-in
  context that answers with the browser's payload) and then the crawler's
  `_raw_job_to_listing`, `_admit_by_age`, `_adopt_detail_date`. A browser
  sighting and a crawler sighting are the same row (pinned by
  `test_browser_row_equals_the_crawler_row`).
- **Only known boards are written.** A board's `(domain, wd_host, tenant,
  site)` must match a member of `registry_members("workday")`. Anything else
  goes to `browser_radar_unknown_boards` (tenant, site, pod, sightings,
  who reported it) for discovery, and never to `job_postings`.
- **Nothing is closed.** A browser batch is a search, not a census. A posting
  it saw is refreshed (`touch_seen`) or written (`upsert_posting`); a closed
  posting it saw reopens, because a live listing is proof it is open.
- **The crawl's triage, exactly.** New → write. Held but closed → write
  (reopens). Held, open, listing fields prove the content unchanged, and the
  browser brings no better body → `touch_seen` (plus a stored re-read when
  only the reader is stale). A detail body over a stored teaser → write with
  `body_state: full`. Anything else → write (a teaser write never erases a
  stored body; `upsert_posting` guards that). Requirements are read the way
  the crawl reads them (parse reuse, then `_persist_requirements`).
- **A batch is untrusted (2026-11-27).** Every signed-in account may send, so
  the rules are in the write path, not in who sent it. A browser sighting:
  - never replaces a body we already fetched, and never changes a held
    posting's title, link or detail address (it may add a body to a teaser,
    or show the posting is still listed);
  - never reopens a posting closed as `detail-gone` or `link-dead` (its own
    page said so); it is counted as `held`;
  - is listed on its batch row (`written`: posting id and what was done), and
    a posting first written by a browser carries `origin: "browser"`, so one
    account's rows can be found and removed;
  - spends the daily posting limit atomically, and a refused batch spends
    nothing.
- **On by default.** `BROWSER_RADAR_INGEST` unset is `on`. Who may send is
  `BROWSER_RADAR_CONTRIBUTORS_SCOPE` (see Ops), not the mode. `dry_run` (set by
  env) validates, maps, triages and records a batch and says what `on` would
  do under `would`, writing nothing to `job_postings`; `off` refuses batches.
  A value that is none of the three reads as `dry_run`.

## Endpoints

### `GET /api/jobs/browser-radar/plan`

JWT. Read-only. `?limit=` boards (default 40, max 80).

| Field | Meaning |
| --- | --- |
| `enabled` | `BROWSER_RADAR_ENABLED`. When false, no tasks and no terms. |
| `ingest_mode` | `off`, `dry_run` or `on` (`BROWSER_RADAR_INGEST`; unset is `on`, an unknown value reads as `dry_run`). |
| `contributor` | Whether `POST …/listings` accepts this account. Every signed-in account by default; with `BROWSER_RADAR_CONTRIBUTORS_SCOPE=listed`, only `users.is_admin`, the admin set, or `BROWSER_RADAR_CONTRIBUTORS`. |
| `search_terms` | The user's board titles as phrases (`inventory_feed.title_phrases` over `board_requests[email].params.titles`), at most 5. |
| `limits` | 40 requests a run, 300 a day, 6,000 ms apart, 15 details a run. The extension clamps these to its own lower hard caps. |
| `tasks` | Ordered. `relevant`: a tenant on the user's own For-you Workday cards (`board_snapshots[email\|workday]`, most cards first), one task per search term (at most 2). `stale`, for contributors only: the crawler's newest sighting of the tenant's open postings is over 12 hours old, largest boards first, termless (`search_text: ""`, newest first). `both`: relevant and stale, listed first. |

The stale list comes from one grouped read of the open Workday inventory,
cached for 30 minutes per process.

### `POST /api/jobs/browser-radar/listings`

JWT. Body at most 2 MB; at most 40 boards, 100 postings a board, 1,000
postings, 30 details and 200,000 characters per `jobDescription`. Board
identity must pass the provider's own `tenant`/`site`/`wd_host` patterns.
Past any bound, or with a bad shape, the whole batch is `400 invalid_batch`.
A single posting of the wrong shape (or a path that is not `/job/…`) is
counted as `rejected`.

Refusals, in the order they are checked (never 401, which the frontend reads as
a dead session):

| Status | `error` | When |
| --- | --- | --- |
| 409 | `ingest_off` | `BROWSER_RADAR_INGEST=off` or `BROWSER_RADAR_ENABLED=0` |
| 403 | `not_contributor` | scope is `listed` and the account is not an admin and not listed |
| 400 | `invalid_batch` | shape or bounds |
| 429 | `rate_limited` | 30 batches in the clock hour, or 5,000 postings in the UTC day; `retry_after_seconds` and `Retry-After` say when |

A repeated `batch_id` from the same account returns the first answer and
writes nothing (checked before the rate limit, so a retry costs no quota).

Response (contract section 2): `mode`, `boards.{accepted, unknown}`,
`postings.{accepted, rejected, created, updated, touched, reopened,
aged_out, held}`, and `would.{created, updated, touched}` (what the triage
decided; in `dry_run` only `would` moves).

### `GET /api/jobs/browser-radar/uploads/today`

JWT. Read-only. What the server received from this account in the current UTC
day, from `browser_radar_batches` (written batches only, never dry runs):
`available`, `day`, `batches`, `sent`, `created`, `updated`, `touched`,
`reopened`, `held`, `aged_out`, `rejected`, `boards`, `unknown_boards`,
`last_at`, and `recorded` (created + updated + touched + reopened: what
reached the shared list). `available: false` means the count could not be
read; it is not zero. The extension's "Sent N jobs to Aspirely today" line
reads `recorded`.

### `GET /api/admin/ops/browser-radar`

Admin. `?days=` 1 to 30 (default 7). The same totals per account, mode and UTC
day, newest first, plus the 25 most-seen unknown boards (reporter count only,
no emails) and the current `contributor_scope`, `ingest_mode` and `enabled`.
A refused batch leaves no row; it is one log line
(`radar: batch refused code=... status=...`), and an accepted batch is one
log line with its counts.

## Collections

All additive; dropping them removes the feature's state and nothing else.

| Collection | Holds | Retention |
| --- | --- | --- |
| `browser_radar_batches` | `_id = email\|batch_id`: who, when, client, mode, boards read, and the response | TTL 30 days (`expire_at`) |
| `browser_radar_usage` | per-account hourly batch and daily posting counters | TTL, a day past the window |
| `browser_radar_unknown_boards` | boards the registry does not know, with sightings and reporters | kept, for discovery |

## Size

One browser, at the extension's caps, makes at most ~400 Workday requests a
day: about 8,000 listing rows and a few hundred bodies. Before 2026-11-14 the
crawler fetched ~3M Workday listing rows a day from 1,624 employers. So this
restores the owner's own employers and keeps the largest stale boards'
newest postings alive. It does not replace the crawl; Cloudflare Verified
Bots (Web Bot Auth, #64) and the employers' own sites
([coverage doc](../coverage/workday-alternatives-2026-11-17.md)) remain the
real fixes.

## Ops

1. **Writes are on once this deploys.** Nothing to set: with
   `BROWSER_RADAR_INGEST` unset, `GET /api/jobs/browser-radar/plan` reports
   `ingest_mode: "on"` and a contributor's batches are written.
2. **Watch the first batches.**
   `db.browser_radar_batches.find({}, {response: 1, received_at: 1}).sort({received_at: -1}).limit(5)`.
   `postings.created` and `postings.touched` should be non-zero;
   `boards.unknown` should be small (look at `browser_radar_unknown_boards`
   if it is not). To pause writes while you look, set
   `BROWSER_RADAR_INGEST=dry_run` in `/etc/jobpilot/stack.env` (the gateway
   reads it; the crawler does not) and restart the gateway container the way
   the runbook does (`infra/compose/README.md`).
3. **Who may send.** Every signed-in account, by default (owner's decision
   2026-11-26, `BROWSER_RADAR_CONTRIBUTORS_SCOPE=all`). Set the scope to
   `listed` to limit it to admins and `BROWSER_RADAR_CONTRIBUTORS=a@x.com,b@y.com`.
4. **Off switch.** `BROWSER_RADAR_INGEST=off` refuses every batch (409);
   `BROWSER_RADAR_ENABLED=0` also empties the plan.
5. **What it changes.** A browser write records no ingest run of its own
   (`job_ingestion_runs` is the crawl's); its provenance is the batch row.
   The retention watchdog's description health counts these postings like
   any other.
