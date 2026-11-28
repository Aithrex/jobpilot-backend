# Browser Radar (`radar-1`, `radar-2`, `radar-3`)

**Status:** built 2026-11-19; writes are ON by default for contributor
accounts (the owner's call, 2026-11-19). Code:
`shared/python/services/browser_radar_service.py`, routes in
`services/jobs/blueprints/jobs.py`, tests `tests/test_browser_radar.py`. The
extension half lives in the ApplyPilot repo
(`.kiro/specs/r1-workday-radar/contract.md` is the same contract).

`radar-2` (built 2026-11-27) is the fleet half: boards are leased to one
browser at a time, and Admin gets a coverage number. It is additive. A
`radar-1` client keeps working unchanged. See [`radar-2`](#radar-2-the-fleet)
below. Code: `shared/python/services/browser_radar_fleet.py`, tests
`tests/test_browser_radar_fleet.py`.

`radar-3` (built 2026-11-27) is one posting from any job page: when a person
who opted in opens a single posting on any ATS, the extension records it. It
is additive too. See [`radar-3`](#radar-3-one-posting-from-any-job-page).
Code: `shared/python/services/browser_radar_posting.py`, tests
`tests/test_browser_radar_posting.py`. The same change made the Workday batch
match the crawl that production really runs
([Parity with the crawl](#parity-with-the-crawl)) and made a shared board
count as read only for its lease holder
([Uploads complete a lease](#uploads-complete-a-lease)).

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
  `locationsText`, `timeType`, and a detail's `jobPostingInfo.jobDescription`).
  The server maps them the way the production crawl does (`workday_row`
  mirrors `job_ingest_worker._list_workday`), then runs the crawl's own
  `_listing_row` and `_admit_by_age`. A browser sighting and a crawler
  sighting are the same row and the same stored document. See
  [Parity with the crawl](#parity-with-the-crawl).
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

## Parity with the crawl

Production lists Workday through the legacy path. `workday` is in
`_LEGACY_LISTING_VENDORS`, so `list_member` calls `_list_workday` and
`fetch_detail` takes its legacy branch. Until 2026-11-27 the browser path
mapped through `WorkdayProvider._to_raw`, and its test compared against
`_list_via_provider`. Production does not use that path for Workday, so the
"same row" claim was pinned against the wrong code.

Now the tests run the real crawl. `run_shard` runs with only the HTTP call
faked, and the stored document is compared with the one a browser batch
writes for the same raw posting
(`test_browser_document_equals_the_crawled_document`). The two may differ in
three fields, all about who wrote the row: `_id`, `origin` (browser only) and
`last_ingest_run_id` (crawl only). When no body arrived, the crawl also
records its failed fetch (`detail_misses`, `detail_next_at`,
`detail_revision`). A browser asked nobody, so it records no miss.

What changed in the browser path to match the crawl:

| Field | Before | Now, as the crawl does |
| --- | --- | --- |
| `detail_ref` | the full CXS URL | the bare `externalPath` (`job/...`) |
| relative dates | the provider's reader: no "just posted"; hours, weeks and minutes dated | `_approx_iso_from_relative`: "today" and "just" are now, "yesterday", N days, N months. Anything else has no date |
| a row with no readable date | written undated | skipped (ADR-001). It is counted under `rejected` |
| the clock for relative dates | the server clock at upload | the batch's own `observed_at` (see below) |
| the body text | `ats_providers.util.html_to_text` | `workday_cxs_fetcher._detail_html_to_text`, cut at 15,000 characters. So `content_hash`, `description_chars` and the requirements agree |
| `startDate` in a detail | replaced the listing date | not read. The crawl reads only `jobDescription` from a detail |
| `location` in a detail | filled an empty listing location | not read |

**`observed_at`.** "Posted Today" and "Posted 3 Days Ago" are counted back
from the batch's `observed_at`, because the extension can park a batch and
send it a day later. The bound: an `observed_at` in the future, or more than
7 days old, or one that does not parse, is ignored and the server clock is
used. No batch is refused for it.

**Dates in the future.** A date a client states is never believed when it
lands after the server's clock. It reads as unknown. With the bound above a
relative `postedOn` cannot land there, and `startDate` is no longer read at
all. The single posting route applies the same rule to `date_posted`.

Kept different on purpose, each pinned by a test:

- **Script text is dropped.** `<script>`, `<style>` and `<noscript>` blocks
  are removed before the crawl's converter runs. The crawl's converter would
  keep their text as prose. A real Workday body holds none, so a real body
  gives the same text either way. A hostile sender's does not get stored.
- **`myworkdaysite.com` boards keep the provider's link**
  (`https://wd1.myworkdaysite.com/recruiting/<tenant>/<site>/job/...`).
  `_list_workday` builds only `myworkdayjobs.com` addresses and cannot read
  such a board at all, so there is no crawled row to match.
- **Nothing is closed, and no run is recorded.** As before.

## Endpoints

### `GET /api/jobs/browser-radar/plan`

JWT. Read-only. `?limit=` boards (default 40, max 80).

| Field | Meaning |
| --- | --- |
| `enabled` | `BROWSER_RADAR_ENABLED`. When false, no tasks and no terms. |
| `ingest_mode` | `off`, `dry_run` or `on` (`BROWSER_RADAR_INGEST`; unset is `on`, an unknown value reads as `dry_run`). |
| `claims` | `radar-2`. True when this account can claim shared boards at `POST …/claims` (enabled, mode `on`, contributor). A new extension then asks there and skips the `stale` tasks below. |
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
counted as `rejected`. So is a posting whose `postedOn` gives no date the
crawl can read: the crawl skips such a row, and so does this.

`observed_at` (an ISO time, optional) is when the browser read the boards.
Relative dates are counted back from it when it is not in the future and not
more than 7 days old. See [Parity with the crawl](#parity-with-the-crawl).

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

`radar-2` adds two things to a batch, both optional. A board may carry an
empty `postings` list: the board was read and held nothing. And the batch may
carry a top-level `reads` list for boards that could not be read (see
[Uploads complete a lease](#uploads-complete-a-lease)). A batch with `reads`
may leave `boards` out. A batch with neither is still `400 invalid_batch`.

Response (contract section 2): `mode`, `boards.{accepted, unknown}`,
`postings.{accepted, rejected, created, updated, touched, reopened,
aged_out, held}`, and `would.{created, updated, touched}` (what the triage
decided; in `dry_run` only `would` moves).

### `GET /api/jobs/browser-radar/uploads/today`

JWT. Read-only. What the server received from this account in the current UTC
day, from `browser_radar_batches` (written batches only, never dry runs):
`available`, `day`, `batches`, `sent`, `created`, `updated`, `touched`,
`reopened`, `held`, `aged_out`, `rejected`, `boards`, `unknown_boards`,
`last_at`, `staged`, and `recorded` (created + updated + touched + reopened:
what reached the shared list). `available: false` means the count could not
be read; it is not zero. The extension's "Sent N jobs to Aspirely today" line
reads `recorded`. A single posting (`radar-3`) is one batch row of its own
(`kind: "posting"`), so it is counted here like a batch of one. `staged`
counts single postings the registry could not place. They are not in
`recorded`.

### `GET /api/admin/ops/browser-radar`

Admin. `?days=` 1 to 30 (default 7). The same totals per account, mode and UTC
day, newest first, plus the 25 most-seen unknown boards (reporter count only,
no emails) and the current `contributor_scope`, `ingest_mode` and `enabled`.
`radar-2` adds `coverage` (the stored coverage snapshot with its age) and
`fleet` (the brake and the leases as they are now). Both are described under
[`radar-2`](#radar-2-the-fleet). Neither holds an email or an account handle.
`radar-3` adds `staged_total`, `staged_corroborated` and `staged_by_host`
(the 10 hosts with the most staged postings, as `{host, postings}`). They are
counts only: no reporter, no URL. `null` means the count could not be read.
Each account row also carries `staged`.
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
| `browser_radar_board_leases` | `radar-2`. One row per registry board: last read, lease, backoff | kept, never deleted |
| `browser_radar_coverage_snapshots` | `radar-2`. One coverage measurement per run of `radar_coverage` | TTL 14 days (`expire_at`) |
| `browser_radar_fleet` | `radar-2`. The brake (`_id: "brake"`), hourly claim and challenge counters (`_id: "h\|YYYYMMDDHH"`), and a note when the registry shrank (`_id: "sync"`) | counters: TTL 3 days. The brake row is kept |
| `browser_radar_staged_postings` | `radar-3`. Single postings the registry cannot place: an unknown board, or a company's own page | TTL 30 days after the last sighting (`expire_at`) |

`browser_radar_usage` also holds the `radar-3` counters: lookups per hour
(`email\|pl\|YYYYMMDDHH`), single postings per hour (`email\|ph\|YYYYMMDDHH`)
and per day (`email\|pd\|YYYYMMDD`).

Every index is listed once in `browser_radar_service.INDEX_SPECS` and mirrored
in `scripts/migrate_indexes.py` (a test holds the two equal). The service
creates them on first use.

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

## `radar-2`: the fleet

### Why

Measured in production in late November 2026: 1,257 Workday tenants hold active
postings, and 1,244 of them had not been seen for over 12 hours. `radar-1`
gives every contributor the same largest stale boards, with no claim, so N
browsers read the same boards N times. `radar-2` shares the work out. Each
board is read by one browser inside a target interval, each account does a
bounded share, and Admin sees how much of the registry was read.

### The board ledger

`browser_radar_board_leases` holds one row per board in
`registry_members("workday")`. The `_id` is `domain|wd_host|tenant|site`,
lowercased. It is the same address a batch is matched on.

| Field | Meaning |
| --- | --- |
| `tenant`, `site`, `wd_host`, `domain`, `employer` | The board, as the plan names it. |
| `stats_tenant` | The tenant its postings are stored under. Coverage counts tenants by this. |
| `in_registry` | False once the board left the registry. Such a row is never handed out. |
| `last_read_at` | The newest termless read of the board by the account that held its lease. |
| `last_seen_unleased_at`, `last_unleased_postings` | The newest termless upload for the board from an account that did NOT hold its lease, and how many postings it carried. Not a read. Not coverage. |
| `last_read_by` | A salted one-way pseudonym of the account. Never the email. |
| `last_result` | `ok`, `empty`, `gone` or `robots`. |
| `last_postings` | How many postings that read carried. |
| `due_at` | When the board should be read again. The epoch for a board never read. |
| `lease` | `{id, by, until, at}` or null. `by` is the pseudonym. |
| `challenges` | Different accounts challenged on this board in the last 24 hours. |
| `parked_until`, `parked_reason` | Not handed out until then (`gone`, `robots` or `challenge`). |
| `active_postings`, `boost`, `rank` | From the crawler's per-tenant stats. See below. |

`sync_boards` builds the rows. It upserts a row for every registry board,
restores a row whose board came back (its history is kept), and marks a row
whose board left. It never deletes a row.

**A failed registry read changes nothing.** If the registry cannot be read or
is empty, the sync writes nothing. The database half of the registry answers
with an empty list when it cannot be reached, which looks like a registry
that lost those boards. So a sync that would retire more than 20 percent of
the live rows (and more than 5 rows) is not believed either. It writes
nothing but a note (`browser_radar_fleet`, `_id: "sync"`). It is believed
once the registry has said the same thing for 24 hours.

**Priority.** Each sync reads the crawler's stats per tenant (active postings
and the newest `last_seen_at`) and writes two numbers on the row:

- `boost`, from 1.0 to 4.0: `1 + 0.5 * log2(1 + active_postings / 20)`. A
  large board comes due sooner. The boost is bounded, so the smallest board is
  still due one target interval after its read.
- `rank`, the tie-break among boards equally due. Tenants the crawler has not
  seen for 12 hours go first, larger ones before smaller ones. Boards never
  read all sit at the epoch, so `rank` is their whole order.

### Claims

#### `POST /api/jobs/browser-radar/claims`

JWT. Optional JSON body `{"limit": n}` (default 6, at most 12; a larger number
is clamped). At most 30 calls an hour per account.

```json
{
  "plan_version": "radar-2",
  "tasks": [
    {"tenant": "acme", "wd_host": "wd5", "site": "External",
     "domain": "myworkdayjobs.com", "employer": "Acme Corp",
     "search_text": "", "max_pages": 1, "why": "shared",
     "lease_id": "0f3c...32 hex"}
  ],
  "lease_minutes": 60,
  "paused": false,
  "reason": ""
}
```

A board is handed out when it is in the registry, due (`due_at <= now`), not
leased (`lease` is null or `lease.until` has passed) and not parked. The
oldest `due_at` goes first, then the lower `rank`.

Each board is taken with ONE guarded `find_one_and_update`, the idiom of the
crawler's `acquire_lease`. The whole condition is in the filter of the write
that takes the board:

```python
db.browser_radar_board_leases.find_one_and_update(
    {"in_registry": True, "due_at": {"$lte": now},
     "$and": [{"$or": [{"lease": None}, {"lease.until": {"$lte": now}}]},
              {"$or": [{"parked_until": None}, {"parked_until": {"$lte": now}}]}]},
    {"$set": {"lease": {"id": token, "by": pseudonym, "until": now + 60 min, "at": now}},
     "$inc": {"leases_issued": 1}},
    sort=[("due_at", 1), ("rank", 1)])
```

MongoDB applies that to one document atomically, so two callers can never be
given the same board. The second caller's filter no longer matches the board
the first one took, and it gets the next one. No transaction and no reaper.
The index is `radar_board_due`: `(in_registry, due_at, rank, parked_until)`.

A lease lasts 60 minutes. Expiry is in the filter: a browser that went away
costs the board an hour, and the board keeps its old `due_at`, so it is the
next one handed out.

An account may claim at most `BROWSER_RADAR_CLAIMS_PER_DAY` boards (default
60) in a UTC day. The count is in `browser_radar_usage` (`_id =
email|c|YYYYMMDD`), taken with one atomic `$inc` and handed back when the
claim goes over or finds fewer boards than it reserved.

No tasks is an answer, not an error: HTTP 200 with a `reason`.

| `reason` | When |
| --- | --- |
| `disabled` | `BROWSER_RADAR_ENABLED=0` |
| `ingest_off` | `BROWSER_RADAR_INGEST=off` |
| `ingest_dry_run` | `BROWSER_RADAR_INGEST=dry_run`. A lease is completed by a written batch, and a dry run writes none. |
| `not_contributor` | the scope is `listed` and the account is not listed |
| `paused` | the fleet brake is on. `paused` is true. |
| `daily_limit` | the account used its claims for the day |
| `none_due` | no board is due and free |
| `unavailable` | the ledger could not be read |

Refusals: `400 invalid_request` (a body that is not a JSON object, or a
`limit` that is not a whole number) and `429 rate_limited`. Never 401.

#### `POST /api/jobs/browser-radar/claims/release`

JWT. Body `{"lease_ids": ["..."], "reason": "..."}`. One to 12 ids, each 32
lowercase hex characters. `reason` is one of `challenge`, `budget`,
`deferred`, `robots`, `gone`. At most 60 calls an hour per account.

Answer: `{"released": n, "reason": "...", "paused": false}`. A lease is freed
only when the caller holds it (the filter names both the id and the holder),
so one account cannot free another's. An id the caller does not hold is
skipped and not counted.

| `reason` | What it does besides freeing the lease |
| --- | --- |
| `budget`, `deferred` | Nothing. The board is still due and goes to the next caller. |
| `challenge` | Counted on the board and for the fleet. See the brake. |
| `robots` | `last_result: robots`, parked for 24 hours. |
| `gone` | `last_result: gone`, parked for 7 days. |

Refusals: `400 invalid_request` and `429 rate_limited`. Never 401.

### Uploads complete a lease

This runs after a batch is written, in mode `on` only.

- **A termless read is a read of the board, for its lease holder only.** A
  known board in the batch whose `search_text` is empty sets `last_read_at`,
  `last_read_by`, `last_result` (`ok`, or `empty` when it carried no
  postings) and `last_postings`, clears the lease, and sets
  `due_at = now + target / boost` (never under 3 hours). It does so only when
  the uploading account holds the board's lease: `lease.by` is that account's
  pseudonym, and `lease.until` is in the future or not more than 30 minutes
  past (`LEASE_GRACE_MINUTES`). The holder is named in the filter of the one
  write that completes the lease.
- **The same read from any other account is not a read.** Its postings are
  ingested exactly as before. The board row only gets `last_seen_unleased_at`
  and `last_unleased_postings`. `last_read_at`, `due_at` and the lease do not
  move, so the board stays due and `by_browser` does not count it. Before
  2026-11-27 any account could mark every board read without reading one.
- **The grace is for a late upload.** A browser reads the board inside its
  hour and the upload can arrive a little after. Past 30 minutes the read no
  longer counts. If another account claimed the board in between, the lease
  is theirs and the first account's upload does not count either.
- **`radar-1` clients.** Their plan `stale` tasks carry no lease. They keep
  working and their postings are still ingested, but their termless reads no
  longer count toward `by_browser` and no longer move `due_at`. A client
  that wants its reads to count asks `POST …/claims` first.
- **An empty read counts,** for the holder. A board with an empty `postings`
  list is accepted, touches nothing in `job_postings`, and is recorded as
  `empty`.
- **A title search is not a read.** A board read with a search term proves
  nothing about the board. It moves nothing in the ledger and leaves the
  lease alone.
- **`reads`.** A top-level list of at most 40
  `{tenant, site, wd_host, domain, result}` with `result` `gone` or `robots`.
  The board is recorded, freed and parked (`gone`: 7 days, `robots`: 24
  hours). It is taken only from the account that holds the board's lease:
  parking a board for a week is worth more to an abuser than marking one
  read. The response then carries `reads: {recorded, ignored}`. A bad entry,
  or more than 40, is `400 invalid_batch`.
- A repeated `batch_id` returns the first answer and records nothing again.

### Challenges and the brake

Workday walls an address, not an account.

- **One board.** A `challenge` release is recorded on the board with the
  account's pseudonym. When 3 or more DIFFERENT accounts were challenged on a
  board inside 24 hours, the board is parked for 24 hours. One account
  challenged many times is one account.
- **The fleet.** Each challenge is also counted in an hourly counter. When the
  number of different accounts challenged in the last hour reaches
  `BROWSER_RADAR_BRAKE_ACCOUNTS` (default 3), and at least 1 percent of the
  accounts that claimed in that hour once that is the larger number, every
  claim is paused for 24 hours. `POST …/claims` then answers `paused: true`
  with no tasks. The pause ends by itself.
- A challenge counts for the fleet only from an account that freed a lease
  with it, or that claimed boards in the last two hours. An account that
  never claimed cannot move the brake.
- The state is one small document in Mongo (`browser_radar_fleet`,
  `_id: "brake"`: `paused_until`, `tripped_at`, the counts), because the
  gateway runs several workers.

### The coverage snapshot

The scheduled job `radar_coverage` runs every 30 minutes on the worker
(`services/scheduler/runner.py`, `scheduled_jobs.dispatch_scheduled`), inline,
inside a 240 second budget. It reads Mongo only. Each run:

1. syncs the board ledger from the registry;
2. stores ONE document in `browser_radar_coverage_snapshots`.

It stores nothing when the registry read failed, was empty or shrank, or when
the crawler's stats could not be read. The last good snapshot then ages into
`stale`. It never writes to `coverage_snapshots`, whose readers expect the
employer coverage keys.

| Field | Meaning |
| --- | --- |
| `measured_at`, `window_hours` | When, and the window (24). |
| `tenants_total` | Tenants in the registry. |
| `tenants_refreshed` | Tenants refreshed by either reader below (the union). |
| `by_browser` | Tenants with ANY board whose `last_read_at` is inside the window. Only a lease holder's read sets `last_read_at`. |
| `by_crawler` | Tenants whose newest `last_seen_at` among open postings is inside the window. |
| `boards_total`, `boards_refreshed` | Boards, and boards a browser read inside the window. |
| `never_read` | Boards no browser has read. |
| `leased_now`, `parked` | Boards leased, and boards parked, at that moment. |
| `claims_24h`, `accounts_24h` | Boards claimed, and different accounts that claimed. |
| `registry` | `{members, catalog, endpoints, boards}`: the registry's own counts. |
| `target_hours` | The target interval in force. |

`by_crawler` reads `job_postings.last_seen_at`, which a browser upload also
moves. So a title search that touched one posting counts its tenant there. It
is "something saw an open posting of this tenant", not "the crawler read the
board". `by_browser` is the strict number.

`coverage_block()` serves the newest snapshot:
`{"available": true, "measured_at", "age_hours", "stale", "stale_after_hours": 2, ...the fields above}`.
`stale` is `age_hours > 2`. With no snapshot, or when the read fails, it is
`{"available": false, "detail": "..."}`. That means not known. It is never
zero. It is the `coverage` key of `GET /api/admin/ops/browser-radar`.

The same payload carries `fleet`:
`{"available": true, "paused", "paused_until", "leased_now", "parked", "parked_boards"}`.
`parked_boards` lists up to 25 parked boards (`tenant`, `site`, `wd_host`,
`domain`, `reason`, `parked_until`, `challenges`).

### Ops

| Variable | Default | Meaning |
| --- | --- | --- |
| `BROWSER_RADAR_CLAIMS_PER_DAY` | 60 | Boards one account may claim in a UTC day (0 to 1000). 0 stops claims. |
| `BROWSER_RADAR_TARGET_HOURS` | 24 | A board is due again this long after a read, divided by its boost. Clamped to 3 to 72. |
| `BROWSER_RADAR_BRAKE_ACCOUNTS` | 3 | Different accounts challenged inside an hour that pause all claims. |

The existing switches apply: `BROWSER_RADAR_ENABLED=0` stops claims and the
coverage job, and `BROWSER_RADAR_INGEST=off` or `dry_run` stops claims.

1. **Nothing to set.** The worker creates the rows on its first
   `radar_coverage` run. Until then `claims` answers `none_due`.
2. **Indexes.** The service creates them on first use, so there is nothing
   to run. `scripts/migrate_indexes.py` declares the same ones. That script
   writes to the database unless it is given `--dry-run`, and the owner runs it.
3. **Read the number.** `GET /api/admin/ops/browser-radar`, key `coverage`.
   Or `db.browser_radar_coverage_snapshots.find().sort({measured_at: -1}).limit(1)`.
   The job logs one line: `radar: coverage stored=... tenants=.../... boards=.../...`.
4. **Lift the brake by hand.**
   `db.browser_radar_fleet.deleteOne({_id: "brake"})`.
5. **Unpark a board.**
   `db.browser_radar_board_leases.updateOne({_id: "<key>"}, {$set: {parked_until: null}})`.
6. **Pseudonyms.** An account's handle uses `TELEMETRY_SALT` when it is set
   (the same handle as the tech-feed telemetry). When it is not, it is an
   HMAC keyed on the JWT secret, so every gateway worker computes the same
   one. Changing either makes held leases unreleasable until they lapse (60
   minutes) and restarts the "different accounts" counts.
7. **Dropping the three `radar-2` collections** removes the leases, the brake
   and the snapshots. The next `radar_coverage` run rebuilds the rows, with
   every board never read.
8. **Who really read a board.**
   `db.browser_radar_board_leases.find({last_seen_unleased_at: {$ne: null}}, {last_seen_unleased_at: 1, last_read_at: 1})`
   lists boards that got uploads from accounts holding no lease.

### What it does not do

- It does not close postings. A browser read is still a search, not a census.
- It reads page 1 of each board. Large boards whose newest postings sit past
  page 1 need page windows, which the extension cannot do yet.
- The crawler does not write to the ledger. Its reads appear only through
  `by_crawler`.

## `radar-3`: one posting from any job page

### Why

`radar-1` and `radar-2` read Workday boards. A person also opens single job
pages all day, on every ATS and on employers' own sites. When they opted in
(the extension's own switch), the extension tells Aspirely about the posting
they are looking at, so people with similar titles see it, unless Aspirely
already has it.

Any signed-in account may send. So every word from the client is untrusted,
and the rules are in the write path.

### The rules

- **Identity comes from the URL, resolved on the server.** The provider
  registry (`ats_providers.registry.match_url`) names the vendor. The vendor's
  `endpoint_from_match` names the board. The registry member
  (`registry_members`) names the employer. The posting's raw id is read from
  the URL by the vendor's own `posting_id_from_url`. The body cannot choose a
  tenant, an employer, an apply link or a `source_job_id`. Fields with those
  names in the body are never read.
- **The host decides the vendor.** A fingerprint is a search over the whole
  URL, so the URL's host must also be one of that vendor's own hosts
  (`portal_hosts`, `api_hosts`), or the own host of a registry member (a
  SuccessFactors career site on the employer's domain).
- **The link is rebuilt.** The stored apply link comes from the member and
  the id (`posting_links`). SuccessFactors has no builder: its link is the
  page URL as `https://<host><path>`, with the query and the fragment
  dropped, and only on the member's own host.
- **A known board is written through the batch's guarded path**
  (`_write_rows`: `_decide`, the guard fields, the `origin` label, the
  ledger). All the batch's guards apply.
- **Everything else is staged.** It never reaches `job_postings`.
- **The server makes no outbound request.** See
  [The open trust gap](#the-open-trust-gap).

### The id, per vendor

`posting_id_from_url(endpoint, url)` exists only where the id in the URL is
the id the crawl stores. Each reads the path of that vendor's own posting
page and refuses its listing page and its apply steps.

| Vendor | Posting page | Id | Stored link |
| --- | --- | --- | --- |
| `greenhouse` | `(job-)boards(.eu).greenhouse.io/<board>/jobs/<id>` | the number | `https://job-boards(.eu).greenhouse.io/<board>/jobs/<id>` |
| `lever` | `jobs(.eu).lever.co/<company>/<uuid>` | the uuid | the same, without a query |
| `ashby` | `jobs.ashbyhq.com/<org>/<uuid>` | the uuid | the same |
| `smartrecruiters` | `jobs.smartrecruiters.com/<Company>/<id>[-slug]` | the leading number | `https://jobs.smartrecruiters.com/<Company>/<id>` |
| `workable` | `apply.workable.com/<account>/j/<shortcode>` | the shortcode | `https://apply.workable.com/j/<shortcode>` (the widget's own link) |
| `neogov` | `www.governmentjobs.com/careers/<agency>/jobs/<id>[/slug]` | the number | `https://www.governmentjobs.com/careers/<agency>/jobs/<id>` |
| `jobvite` | `jobs.jobvite.com/<company>/job/<id>` | the id | the same |
| `bamboohr` | `<company>.bamboohr.com/careers/<id>` | the number | the same |
| `icims` | `<label>.icims.com/jobs/<id>/<slug>/job` | the number | the same, without a query (the slug is the page's own) |
| `oracle_orc` | `<pod>/hcmUI/CandidateExperience/<lang>/sites/<site>/job/<id>` | the id | the same with `en` |
| `successfactors` | `<host>/job/<slug>/<id>` | the number | the page URL without its query |
| `workday` | `{tenant}.wdN.myworkdayjobs.com/[locale/]{site}/job/...` and the `myworkdaysite.com` form | see below | `{apply base}/{site}/job/...`, locale dropped |

**Workday.** The id is the text after the LAST underscore of the last
`/job/` path segment (`.../Senior-Backend-Engineer_JR1001` gives `JR1001`).
It can differ from the id the crawl stores. The crawl keys a posting on the
listing's `bulletFields[0]`, which the URL does not carry, and a reposted
requisition's path can end `_JR1001-1`. So before a posting is created, the
server looks for one already stored on the same board under the same
canonical URL key (`canonical_url_key`, found through the indexed
`dedupe_group_id`). When there is one, the sighting is treated as that
posting: it is touched, or given its body. No twin is created. The same
check runs for every vendor.

**Not supported** (the URL does not carry an id that is provably the crawl's):
`recruitee` (the URL has a slug, the crawl stores a number), `taleo` (`job=`
may be the contest number, the crawl stores `jobId`), and every other
registered vendor (`adp`, `paycom`, `paycor`, `breezy`, `phenom`,
`eightfold`, `radancy`, `usajobs`, `amazon`, `avature`, `jibe`, own-site
providers and the rest). Also not read: a Greenhouse or Ashby job embedded in
a company page (`?gh_jid=`, `?ashby_jid=`: the URL names no board), the
Workable short link (`apply.workable.com/j/<shortcode>` names no account),
and the SuccessFactors portal form (`?career_job_req_id=`). Those pages are
company sites to this route.

### `POST /api/jobs/browser-radar/posting/lookup`

JWT. Body `{"url": "<the posting page URL, at most 2,000 characters>"}`, at
most 8 KB. Read-only apart from its hourly counter.

```json
{"supported": true, "vendor": "greenhouse", "known_board": true,
 "known": true, "job_id": "<32 hex or null>",
 "want": "nothing"}
```

| Field | Meaning |
| --- | --- |
| `supported` | The URL is a single posting on a vendor with an id rule. When false: `known` is false, `job_id` is null, `want` is `nothing`. |
| `vendor` | The provider that owns the host, or `""` when none does. It can be named while `supported` is false (a listing page, or a vendor with no id rule). |
| `known_board` | The board is a registry member. |
| `known` | A posting with that identity is in `job_postings`. |
| `job_id` | Its inventory id (`posting_job_id(source, source_job_id)`), when known. |
| `want` | `nothing`, `sighting` or `body`. |

`want`:

- `body`: we do not hold it, or hold only a teaser, or the board is not in
  the registry (the posting would be staged).
- `sighting`: we hold it with a full body and have not seen it for 12 hours,
  or it is closed for a reason a sighting can undo.
- `nothing`: seen in the last 12 hours with a full body. Also for an
  unsupported URL, and for a posting its own page closed (`detail-gone`,
  `link-dead`), which a sighting cannot reopen.

An account that may not send (ingest off, or not a contributor) is refused
with 409 or 403 below. It does not get `want: "nothing"`.

### `POST /api/jobs/browser-radar/posting`

JWT. Body at most 300 KB.

```json
{"url": "...", "title": "...", "company": "...", "location": "...",
 "description": "<plain text or HTML, at most 200,000 characters>",
 "date_posted": "<ISO date or empty>", "employment_type": "...",
 "json_ld": {"@type": "JobPosting"},
 "client": {"name": "applypilot", "version": "..."}}
```

`json_ld` is the page's schema.org `JobPosting` object. It is optional and
at most 100 KB. Any other key in the body is ignored.

Answer 200:

```json
{"result": "created", "job_id": "<32 hex or null>", "reason": ""}
```

| `result` | `reason` | When |
| --- | --- | --- |
| `created` | | Known board, posting not held. Written new. |
| `updated` | | Held with a teaser only, and a full body arrived. |
| `updated` | `reopened` | Held but closed, for a reason a live page can undo. |
| `touched` | | Held and open. Refreshed as still listed. |
| `staged` | `unknown_board` | A vendor's posting page for a board the registry lacks. |
| `staged` | `company_site` | No vendor rule for this page, and `json_ld` is a `JobPosting`. |
| `ignored` | `closed` | Held, and closed by its own page (`detail-gone`, `link-dead`). |
| `ignored` | `aged_out` | Not held and too old for the crawl's age rules. |
| `ignored` | `undated` | Not held, no usable `date_posted`, and this vendor's crawl skips undated rows. |
| `ignored` | `no_title` | Not held and no title. |
| `ignored` | `not_a_posting` | The vendor's own host, and not a posting page. |
| `ignored` | `unsupported` | No vendor rule and no `JobPosting` in `json_ld`. |
| `ignored` | `rejected` | The inventory refused the write. |
| `ignored` | `dry_run` | `BROWSER_RADAR_INGEST=dry_run`. See below. |

`job_id` is set for `created`, `updated` and `touched`, and for `ignored` /
`closed`. It is null otherwise.

**Dry run.** The route validates, resolves and triages, and writes nothing
to `job_postings` or to staging. The answer is `"result": "ignored",
"reason": "dry_run"` plus one more key, `would`, holding the `result` that
`on` would have given. The batch row is written with `mode: "dry_run"`, so
it is not counted as sent.

#### A known board, posting held

The sighting is rebuilt from what we STORED (`_row_from_stored`), not from
the body. So it carries our title, link, place, date and employment type.
Then `_decide` runs, as for a batch:

- held and open: `touch_seen`;
- held with a teaser, and at least 200 characters of text arrived: the body
  is added (`body_state: full`), and nothing else changes;
- a fetched body is never replaced;
- `detail-gone` and `link-dead` are never reopened;
- nothing is ever closed.

The age rules are not applied to a held posting. A person looking at an open
page is the evidence the crawl's 7 day listing window cannot give.

#### A known board, posting not held

It is written through `upsert_posting`, with `origin: "browser"`. The body
state is `full` when the description holds at least 200 characters of text
after flattening, else `teaser`. The description is flattened by the same
helper the Workday batch uses (`flatten_body`). The row is the shape that
vendor's crawl builds:

- Workday: `_listing_row` as `_list_workday` calls it (the bare path as
  `detail_ref`, the member's link).
- Greenhouse (US), Lever (US), Ashby, Workable, NeoGov: `_listing_row` as
  `_list_career` calls it. That crawl skips a row with no date, so this does
  too (`ignored` / `undated`).
- Every other supported vendor: a `RawJob` through `_raw_job_to_listing`,
  which keeps an undated row, as that crawl does.

Then `_admit_by_age` (7 days, 30 for early career), and requirements read
the way `_process` reads them (parse reuse, then `_persist_requirements`).
The employer is the registry member's. The body's `company` is not used.

Fields of a browser-created posting that cannot match a crawled one, and why:

| Field | Why it differs |
| --- | --- |
| `title_raw`, `locations`, `employment_type` | The page's wording, as the browser read it. The crawl reads the vendor's API fields (Greenhouse `location.name`, SmartRecruiters `city, region` plus the stated country, Workday `locationsText` and `timeType`). `employment_type` is taken only for vendors whose crawled row has one (Workday, Ashby, NeoGov, iCIMS, Oracle, BambooHR); for the others the crawl stores none and so does this. |
| description, and so `content_hash`, `description_chars`, `requirements`, `tech`, `seniority`, `work_mode` | The page's text, flattened here. The crawl flattens the vendor's API body with that vendor's converter. Lever's crawl joins four API fields. |
| `posted_at`, `raw_posted_text`, `posted_at_confidence` | `date_posted` from the page. For Workday the crawl has only relative text ("Posted Today"). A date in the future is dropped. |
| `compensation` | Never taken from a browser. The crawl reads pay fields from the API. |
| `canonical_url` | Rebuilt here. Greenhouse's crawl stores the board's `absolute_url`, which can be the employer's own domain (`stripe.com/jobs?gh_jid=`). Lever's stores `hostedUrl`. iCIMS and SuccessFactors keep the page's own slug. Where it differs, `dedupe_group_id` and `canonical_url_key` differ too. |
| `detail_ref` | Greenhouse (US), Lever, Ashby, NeoGov: none, as their crawl stores. Others: the provider's detail address. |
| `origin` | `browser`. A crawled posting has none. |
| `last_ingest_run_id`, miss bookkeeping | Crawl only. |

`source`, `source_job_id`, `job_id`, `employer_id`, `employer_name`, `ats`,
`tenant`, `industry`, `cost_class`, `source_quality`, `canonical_employer_id`
and the retention fields are the crawl's own. When the crawl next lists the
posting it writes the same `(source, source_job_id)` and its fields win.

#### An unknown board, or a company's own site

Never `job_postings`. The posting goes to `browser_radar_staged_postings`,
keyed by `sha256(normalised URL)[:32]`:

| Field | Meaning |
| --- | --- |
| `url`, `host` | The normalised URL (below) and its host. |
| `vendor`, `tenant`, `site`, `kind` | The server's own reading of the URL. `kind` is `unknown_board` or `company_site`. `vendor` is `""` for a company site. |
| `title`, `company`, `location` | As the browser said. Untrusted. |
| `excerpt` | The first 2,000 characters of the flattened description. |
| `content_hash` | `sha256` of the title and the flattened description, lowercased, whitespace collapsed. |
| `json_ld` | The page's object when its `@type` is `JobPosting`, else null. Keys Mongo would read as an operator or a path are dropped. |
| `first_seen_at`, `last_seen_at`, `sightings` | |
| `reporters` | Up to 50 account pseudonyms (`account_pseudonym`, the helper the fleet uses). Never an email. |
| `state` | `observed`, then `corroborated`. |
| `expire_at` | 30 days after the last sighting. |

A page with no vendor rule is staged only when `json_ld` is a `JobPosting`.
Without one the answer is `ignored` / `unsupported`.

**States.** A posting starts `observed`. A sighting from a DIFFERENT
pseudonym with the SAME `content_hash` moves it to `corroborated`
(`corroborated_at`). The same account again does not. Different content does
not, and it does not rewrite what the first sighting stored. Nothing reads
this collection into any feed in this change, and nothing promotes a row.

### The URL

Refused (`400 invalid_request`): not `http` or `https`, a user or password
in it, more than 2,000 characters, characters a URL cannot hold, a path that
walks upward, and a host that is not public. Not public means: an address
that is loopback, private, link-local or otherwise not global (IPv4 or
IPv6), `localhost`, a name with no dot, a numeric or `0x` last label, and
names under `.local`, `.internal`, `.intranet`, `.lan`, `.home`, `.corp`,
`.localhost`, `.test`, `.invalid`, `.example`, `.onion`. No DNS is asked.

Kept: scheme, host (lowercased), a port that is not the default, the path,
and only these query parameters when their value is a plain token: `gh_jid`,
`ashby_jid`, `jobid`, `job_id`, `job`, `jid`, `reqid`, `req_id`, `id`,
`career_job_req_id`, `company`, `jk`, `pid`. Everything else in the query is
dropped (tracking tags, session tokens, candidate ids), and so is the
fragment. Nothing but the contract's fields is stored. Cookies and other
page data are never read.

### Refusals and limits

Both routes, in this order, never 401:

| Status | `error` | When |
| --- | --- | --- |
| 409 | `ingest_off` | `BROWSER_RADAR_INGEST=off` or `BROWSER_RADAR_ENABLED=0` |
| 403 | `not_contributor` | the scope is `listed` and the account is not listed |
| 400 | `invalid_request` | shape, sizes, or the URL rules above |
| 429 | `rate_limited` | 120 lookups in the clock hour; 60 postings in the clock hour; 300 postings in the UTC day. `retry_after_seconds` and `Retry-After` say when |

Strings past their bound are cut, not refused: `title` 300, `company` 200,
`location` 300, `employment_type` 80, `date_posted` 40. A `description` over
200,000 characters, a `json_ld` over 100 KB and a body over 300 KB are
refused.

Each counter is one atomic `$inc` in `browser_radar_usage`, handed back when
it went over (the `_rate_limit` idiom). A refused request spends nothing.

### What is recorded

Every answered posting writes one row in `browser_radar_batches` with
`kind: "posting"`: the account, the mode, one board (`vendor`, `host`,
`tenant`, `site`), `sent: 1`, the same `response` counters a batch has (plus
`postings.staged`), and the same `written` ledger (posting id and what was
done). The page URL is not kept on it. So `uploads/today` and the admin
table count a single posting like a batch of one.

A refused request leaves no row. It is one log line
(`radar: posting refused code=... status=...`). An answered one is one log
line with the result, the vendor and whether the board was known. No URL and
no email are logged.

### The open trust gap

The server does not fetch the page. It cannot: the reason this feature
exists is that the server is walled where a browser is not, and a fetch of a
client's URL would be a new outbound path. So a body is the sender's word.
What bounds the damage today:

- the employer, the board, the id and the link are the server's, so a false
  posting still carries a real employer's link to that employer's real page;
- a fetched body is never replaced, and a held posting keeps its title,
  link, place and date;
- a posting its own page closed stays closed;
- an unknown board or a company page is never shown to anyone;
- every write is on a ledger row with the account, and a posting a browser
  created carries `origin: "browser"`, so one account's rows can be found
  and removed;
- 300 postings a day an account.

What is still open: on a known board, one account can create a posting with
invented words under a real employer, or give a teaser an invented body.
The staged state machine is the path to closing it. `observed` and
`corroborated` exist now. The next steps are a `verified` state (the crawl
later lists the posting, or a server fetch through the crawler's own
allowlisted path agrees), the same states for browser-created rows on known
boards, and promotion of staged rows only from `corroborated` plus a proven
board.

### Ops

No new environment variable. The existing switches apply:
`BROWSER_RADAR_ENABLED`, `BROWSER_RADAR_INGEST` (`off`, `dry_run`, `on`) and
`BROWSER_RADAR_CONTRIBUTORS_SCOPE`.

1. **Indexes.** The service creates them on first use. They are declared in
   `browser_radar_service.INDEX_SPECS` and in `scripts/migrate_indexes.py`:
   `ttl_expire_at`, `radar_staged_state` and `radar_staged_host` on
   `browser_radar_staged_postings`.
2. **See what arrived.**
   `db.browser_radar_batches.find({kind: "posting"}).sort({received_at: -1}).limit(5)`.
3. **See what is staged.**
   `db.browser_radar_staged_postings.find({state: "corroborated"}, {url: 1, title: 1, sightings: 1})`.
   Or the `staged_*` keys of `GET /api/admin/ops/browser-radar`.
4. **Remove one account's rows.** Its batch rows list the posting ids under
   `written`. Rows with `did: "created"` and `origin: "browser"` on the
   posting are the ones it made.
5. **Dropping `browser_radar_staged_postings`** removes the staged postings
   and nothing else.
