# Browser Radar (`radar-1`, `radar-2`)

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
`last_at`, and `recorded` (created + updated + touched + reopened: what
reached the shared list). `available: false` means the count could not be
read; it is not zero. The extension's "Sent N jobs to Aspirely today" line
reads `recorded`.

### `GET /api/admin/ops/browser-radar`

Admin. `?days=` 1 to 30 (default 7). The same totals per account, mode and UTC
day, newest first, plus the 25 most-seen unknown boards (reporter count only,
no emails) and the current `contributor_scope`, `ingest_mode` and `enabled`.
`radar-2` adds `coverage` (the stored coverage snapshot with its age) and
`fleet` (the brake and the leases as they are now). Both are described under
[`radar-2`](#radar-2-the-fleet). Neither holds an email or an account handle.
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
| `last_read_at` | The newest termless read of the board by a browser. |
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

- **A termless read is a read of the board.** Every known board in the batch
  whose `search_text` is empty sets `last_read_at`, `last_read_by`,
  `last_result` (`ok`, or `empty` when it carried no postings) and
  `last_postings`, clears the lease, and sets `due_at = now + target / boost`
  (never under 3 hours). It counts whoever held the lease. A read is a read.
- **An empty read counts.** A board with an empty `postings` list is
  accepted, touches nothing in `job_postings`, and is recorded as `empty`.
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
| `by_browser` | Tenants with ANY board whose `last_read_at` is inside the window. |
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

### What it does not do

- It does not close postings. A browser read is still a search, not a census.
- It reads page 1 of each board. Large boards whose newest postings sit past
  page 1 need page windows, which the extension cannot do yet.
- The crawler does not write to the ledger. Its reads appear only through
  `by_crawler`.
