# ADR-004 — Universal Employer Coverage

**Status:** Accepted (in build)
**Date:** 2026-08-17
**Supersedes:** nothing. **Extends:** ADR-003 (job inventory, shard/lease ingestion).

---

## 1. The problem, stated honestly

Today the crawlable universe of JobPilot is **three hand-maintained Python lists**:

| Registry | Rows | Maintained by |
|---|---:|---|
| `workday_tenant_catalog.WORKDAY_TENANT_CATALOG` | 1013 | `scripts/validate_workday_tenants.py` |
| `career_pages_catalog.CAREER_PAGES_CATALOG` | 1319 | `scripts/validate_career_tenants.py` |
| `career_pages_service.ENTERPRISE_CATALOG` | 11 | by hand |

That is ~2,340 employers. The US has roughly **11 million employer establishments** and about
**200k–400k** of them run a public, machine-readable ATS. So current coverage of the addressable
market is on the order of **1%**, and every additional employer costs a human a catalog row.

Three structural faults follow from "the catalog is a Python literal":

1. **Coverage is bounded by human typing.** There is no path from "a company exists" to
   "we crawl it" that does not involve someone editing a `.py` file.
2. **A new ATS vendor is a new bespoke fetcher.** `career_pages_service` has seven board
   fetchers and five enterprise fetchers, each with its own pagination, date handling and
   error posture. Vendor number thirteen costs the same as vendor number one.
3. **Discovery is a script, not a system.** `discover_career_pages.py` prints rows for a human
   to paste. Nothing runs continuously, so the catalog decays: employers migrate ATS and the
   row silently returns zero jobs.

The ask that triggered this ADR — "cover Oracle and iCIMS like we cover Workday, and go through
every company registered in every US state" — cannot be met by adding two more fetchers and a
bigger literal. It requires inverting the pipeline.

## 2. Decision

Build a **self-expanding employer graph**. The catalog stops being an input written by humans
and becomes an *output* produced and continuously re-validated by the system:

```
                 ┌──────────────────── sources of company identity ────────────────────┐
                 │  50 state business registries · SEC EDGAR · SAM.gov · DOL H-1B LCA  │
                 └──────────────────────────────┬──────────────────────────────────────┘
                                                │  company_registry_service
                                                ▼
                                     ┌────────────────────┐
                                     │     companies      │   name, state, domain,
                                     │     (MongoDB)      │   employment signal, priority
                                     └─────────┬──────────┘
                                               │  discovery_queue (priority-ordered)
                                               ▼
                                  ┌──────────────────────────┐
                                  │ career_portal_discovery  │  fetch site → follow
                                  │   ATS FINGERPRINTING     │  /careers → match vendor
                                  └─────────┬────────────────┘  regex → extract tenant
                                            │
                                            ▼
                                ┌──────────────────────────┐
                                │  company_ats_endpoints   │  vendor + tenant + site,
                                │       (MongoDB)          │  validated, last_ok_at
                                └─────────┬────────────────┘
                                          │  registry_members()  (ADR-003 shards)
                                          ▼
                                ┌──────────────────────────┐
                                │   ats_providers/*.py     │  ONE plugin per vendor
                                │   job_ingest_worker      │
                                └─────────┬────────────────┘
                                          ▼
                                    job_postings  (unchanged, ADR-003)
```

Everything downstream of `company_ats_endpoints` is ADR-003 and does not change. This ADR adds
what sits *above* it, and replaces twelve bespoke fetchers with one provider interface.

### 2.1 Why fingerprinting rather than per-vendor slug guessing

The instinct is to guess: for each company try `boards.greenhouse.io/{slug}`,
`jobs.lever.co/{slug}`, `{slug}.wd1.myworkdayjobs.com`, and so on. That is
**O(companies × vendors × host-variants)** requests — for Workday alone the host prefix ranges
`wd1`–`wd103`, so one company costs 103 requests to rule out one vendor. It is also unreliable:
slugs frequently do not match the company name (`Bread Financial` → tenant `alliancedata`).

Fingerprinting inverts it. Fetch the company's own site and its `/careers` page, then match every
outbound link and script src against a table of vendor URL regexes. The employer *tells us* which
ATS they use and what their tenant is, in the href of their own "View openings" button. That is
**O(companies)** — two requests, all vendors at once, and it discovers vendors we have not seen
because unknown-but-recurring hosts can be surfaced for review.

This is the single most important design choice in this ADR. §5.1 of
`docs/workday-tenant-expansion.md` reached the same conclusion for Workday specifically; this
generalises it.

## 3. Evidence — live probes, 2026-08-17

Written down because §"What it has already established" in `scripts/probe_new_ats.py` exists for
exactly this reason, and because **one of the recorded verdicts below was wrong** and cost real
coverage.

### 3.1 Oracle Recruiting Cloud (ORC) — FETCHABLE, and badly under-used today

```
GET https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions
    ?onlyData=true
    &expand=requisitionList.secondaryLocations,flexFieldsFacet.values
    &finder=findReqs;siteNumber={site},limit={n},sortBy=POSTING_DATES_DESC,offset={k}
```

| Host | Status | `TotalJobsCount` |
|---|---|---:|
| `jpmc.fa.oraclecloud.com` | 200 | **7,381** |
| `eeho.fa.us2.oraclecloud.com` (Oracle) | 200 | **2,106** |

Findings that change the design:

* `TotalJobsCount` is returned on every response, and `offset` pages cleanly. **Real pagination
  exists.** The current `_fetch_orc` sends `limit=25` with no `offset` and never pages, so it
  reads **25 of JPMorgan's 7,381 requisitions — 0.3%**.
* `PostedDate` is a true ISO date (`"2026-08-18"`), not relative text. Under ADR-001 this is
  `CONFIDENCE_EXACT`, strictly better than Workday's `"Posted 2 Days Ago"`.
* `sortBy=POSTING_DATES_DESC` makes it **newest-first**, so it satisfies the ADR-003 early-stop
  rule and can be a termless board crawl. Today it is misclassified as a term-driven
  "enterprise" fetcher and is therefore excluded from `_TERMLESS_ATS` — i.e. the background
  crawler never visits Oracle at all.
* Dead/parked pods answer 503/504 rather than 404, so endpoint validation must treat 5xx as
  "unknown, retry" and never as "delete this row".

### 3.2 iCIMS — FETCHABLE. The recorded "do not build" verdict was wrong.

`scripts/probe_new_ats.py` records: *"iCIMS (emory, rivian): every pattern returns HTML, no JSON,
no embedded job payload. INDEPENDENTLY CONFIRMS the doc's Tier C verdict. Do not build."*

The observation was accurate; the **conclusion did not follow**. "No JSON" is not "not
fetchable" — `lxml>=5.0.0` is already a runtime dependency. What the probe actually established
was only that iCIMS is not a JSON API. Re-probed:

* `GET https://careers-{slug}.icims.com/jobs/search?ss=1&in_iframe=1` → **200, ~26 KB of HTML.**
* That page is server-rendered and contains the search form, whose `action` carries a
  **session-bound `hashed` token**: `action=".../jobs/search?in_iframe=1&hashed=-435620505"`.
  Without it the page renders *"Sorry, no jobs were found that match your search criteria."* —
  which is precisely how a naive fetch concludes "empty board".
  → **iCIMS is a two-step fetch: bootstrap for `hashed` + cookies, then page results.**
  This is the same failure mode as the NeoGov discovery already recorded in that script: the
  endpoint was there, the first probe just did not speak its protocol.

* **`*.icims.com` is wildcard DNS.** Every candidate slug resolves, including nonsense ones.
  DNS resolution therefore proves nothing and must not be used to validate an iCIMS tenant;
  only a successful content fetch does.

* **Anti-bot behaviour is IP-scoped, not host-scoped.** Six concurrent requests across sixty
  *different* `careers-*.icims.com` hostnames caused **all sixty** to return
  `405` with an identical 2,115-byte body titled **"Human Verification"**. Serial requests
  spaced ~3 s returned 200 indefinitely. Once tripped, the block persists for a cooldown and
  affects every iCIMS host.

  → `HostPolicy` keys its spacing on **hostname**, so it would happily fire twenty different
  iCIMS tenants in parallel and trip the wall on all of them. **Pacing must be keyed on the
  vendor / registrable domain, not the hostname.** This is a genuine defect in the current
  crawler that this ADR fixes (§4.2).

### 3.3 Taleo — partial, deprioritised

`{zone}.taleo.net` mostly no longer resolves (chevron, csx, flowserve, parsons, oracle: NXDOMAIN)
— Oracle has migrated customers to ORC. `tetratech.taleo.net` resolves and returns **405 on
`GET /careersection/rest/jobboard/searchjobs`**, i.e. the endpoint exists and requires POST.

Taleo is therefore a real but *shrinking* surface. It is implemented behind the same provider
interface, but it is explicitly not on the critical path: ORC is where Oracle's volume went.

## 4. Architecture

### 4.1 `ats_providers/` — one plugin per vendor

A provider is a class, not a function, because a fetcher needs more than "give me jobs": it needs
to declare how it may be crawled, how its tenants are recognised, and how its failures read.

```python
class AtsProvider:
    vendor: str                    # "oracle_orc" | "icims" | "greenhouse" | ...
    label: str                     # UI badge text
    pacing_key: str                # THROTTLE SCOPE — see §4.2
    policy: CrawlPolicy            # concurrency, interval, backoff, challenge detection
    termless: bool                 # can it be paged newest-first with no search term?
    date_confidence: str           # "exact" | "relative" | "unknown"  (ADR-001)
    api_hosts / portal_hosts       # SSRF allowlist contributions
    fingerprints: tuple[Fingerprint, ...]   # regexes that recognise this vendor + capture tenant

    def list_jobs(self, endpoint, ctx) -> Iterator[RawJob]
    def fetch_detail(self, job, ctx) -> RawJob | None
    def validate(self, endpoint, ctx) -> ValidationResult
```

Registering a provider automatically contributes its fingerprints to discovery, its hosts to the
SSRF allowlist, and its pacing key to the crawler. Under the current design those are three
separate hand-edited tables (`FETCHERS`, `_HOST_ALLOW`, `_TERMLESS_ATS`) and forgetting any one of
them ships a source that silently returns nothing — a failure the existing code comments already
call out. **One registration, no separate tables to forget.**

### 4.2 Pacing keyed on vendor, not host

`HostPolicy` gains a `key_of(url)` indirection. Default stays the hostname, preserving today's
behaviour for Greenhouse/Lever/etc. A provider may override `pacing_key` so that every
`careers-*.icims.com` shares one token bucket. Providers also declare:

* `max_concurrency` — iCIMS is **1**; Greenhouse stays at the shard default.
* `min_interval` — iCIMS ~2.5 s; board APIs keep 0.15 s.
* `is_challenge(response)` — recognises the "Human Verification" body so a soft-block is
  recorded as a *retryable vendor-wide* condition (durable backoff via
  `job_source_state.next_attempt_at`) rather than being mistaken for a permanent 4xx and
  silently dropping the employer.

Note the current `http_get_json` treats any non-429 4xx as permanent. iCIMS's block is a
**405**, so without `is_challenge` every throttled tenant would be discarded as dead.

### 4.3 HTML providers are first class

`http_get_json` gets a sibling `http_get_text` sharing the same guard/pacing/deadline logic, and
providers may parse with `lxml`. Not every ATS ships JSON and that must stop being a reason to
skip an employer.

### 4.4 Company registry in MongoDB, not in Python

New collections (indexes in `scripts/migrate_indexes.py`):

* **`companies`** — `{company_id, name, name_normalized, state, jurisdictions[], domain,
  signals{h1b, sam, edgar, employees}, priority, discovery_status, last_discovered_at}`
* **`company_ats_endpoints`** — `{company_id, vendor, tenant, site, host, canonical_url,
  status: candidate|validated|dead, job_count_last, validated_at, consecutive_failures}`
* **`discovery_queue`** — priority-ordered work items with the ADR-003 lease pattern.

The existing Python catalogs are **seeded in as validated endpoints** and keep working unchanged;
`registry_members()` reads the union of catalog + Mongo. Nothing regresses if Mongo is empty.

### 4.5 Prioritisation, because 30M entities cannot be probed

State registries return every LLC ever filed, the overwhelming majority of which are single-member
holding companies with no employees and no careers page. Probing them all is neither possible nor
useful. Companies are therefore scored by **employment signal** before entering the queue:

| Signal | Source | Meaning |
|---|---|---|
| H-1B LCA filings | DOL disclosure data (already ingested for `sponsorship_signals`) | provably hires, and sponsors |
| Federal registration | SAM.gov entity API | real operating business |
| Public filer | SEC EDGAR `company_tickers.json` + submissions | large employer |
| Registry recency/status | state registry | active, not dissolved |

Unscored tail entities are not discarded — they drain through the queue at low priority
indefinitely. The system is designed to run forever, not to finish.

## 5. Goals and the loop

Coverage is a **measured number with a target**, not a claim. `scripts/coverage_report.py` emits
these against live Mongo, and the loop below runs until each is met.

| # | Goal | Metric | Target |
|---|---|---|---|
| G1 | Oracle ORC fully crawled | ORC reqs in `job_postings` / Σ `TotalJobsCount` | ≥ 95% |
| G2 | iCIMS live | validated iCIMS endpoints; postings ingested | ≥ 500 endpoints, > 0 postings, 0 challenge-trips per run |
| G3 | Vendor breadth | providers registered and validated in prod | ≥ 12 |
| G4 | Company universe | rows in `companies` | ≥ 2,000,000 |
| G5 | State breadth | states with ≥ 1 working registry adapter | 50 + DC |
| G6 | Discovery yield | validated endpoints / companies probed | ≥ 8% |
| G7 | Endpoint health | validated endpoints returning ≥ 1 posting in 7d | ≥ 90% |
| G8 | No regression | existing catalog postings/day vs baseline | ≥ 100% |

**The loop** (each iteration is a closed feedback cycle, not a checklist):

```
  measure ── coverage_report.py ──▶ pick the lowest goal vs target
     ▲                                          │
     │                                          ▼
  verify ◀── ingest_smoke.py + pytest ◀── fix the binding constraint
```

Exit criterion: all eight targets met **on live data**, verified by the report — not by a
subagent's summary and not by a passing unit test alone, per
`.claude/skills/verification-before-completion`.

## 6. Consequences

**Accepted costs**

* iCIMS is slow by construction (1 connection, ~2.5 s spacing). ~1,400 tenants/hour/IP. It is
  sharded across ticks and will lag Greenhouse; that is the price of an HTML, bot-walled vendor
  and it is still infinitely better than zero.
* State registry adapters are heterogeneous and will break when states redesign their portals.
  Each is small, independently disableable, and reports its own health.
* `companies` at millions of rows is the largest collection in `aspirely_db`. It is write-light
  and read by `company_id` / priority index only.

**Explicitly rejected**

* **Headless browsers for job discovery.** `jobpilot-backend/CLAUDE.md` makes Playwright an
  apply-only invariant; zip Lambdas cannot carry Chromium. iCIMS is beaten with a correct
  two-step HTTP protocol instead, which is cheaper and faster anyway.
* **Paid aggregators to close the gap.** Re-enabling Apify would inflate the number without
  building the asset, and re-introduces the guest cost gate.
* **Scraping registries that forbid it.** Adapters use published bulk files, Socrata/SODA APIs
  and documented endpoints. States that gate data behind paid subscriptions or CAPTCHA
  (Delaware, Texas SOSDirect) are covered via *proxy* sources — e.g. Texas Comptroller active
  franchise-tax permit holders — and the adapter records which route it used.
* **`robots.txt` disregard.** Discovery honours it. Employers publish careers pages to be found.

## 7. Correction to the record

`scripts/probe_new_ats.py` currently instructs future readers that iCIMS is Tier C, bot-walled,
"Do not build". §3.2 supersedes that. The script's own docstring is updated in this change so the
next reader inherits the corrected finding rather than re-deriving it — the same courtesy the
NeoGov note extended.

The generalisable lesson, which is why it is recorded here rather than only in a commit message:
**"returns HTML" and "returns no JSON" are statements about a response, not about feasibility.**
The probe was built to find JSON APIs, so it could only ever conclude that something was or was
not a JSON API. A verdict is only as wide as the instrument that produced it.

---

## 8. Addendum — 2026-08-18: measured against live endpoints

This ADR was written from probes and then implemented; the implementation was
never run against production. `scripts/live_provider_check.py` was built to ask
the only question that decides whether a seeker sees a posting — *does our code
still speak this vendor's protocol today?* — and it changed several conclusions
above. Full detail in `docs/coverage/ats-vendor-coverage.md`.

**Where the pipeline actually stood.** `companies`, `company_ats_endpoints` and
`discovery_queue` were all empty; `job_source_state` held shards only for
`workday` and `career-pages`, so the `oracle` and `icims` sources this ADR
defines had never been dispatched. Of 15 registered providers, **7 produced
postings in production**: Oracle ORC, iCIMS, SmartRecruiters, Taleo, Jobvite,
BambooHR and Eightfold were all at zero.

**§3.2 (iCIMS) was right that it is fetchable and understated the work.** The
protocol was correct; the parsing was not. Titles carried a screen-reader label
prefix, locations were found on ~1 row in 6, and no dates were produced despite
`date_confidence = DATE_EXACT`. Reading the `iCIMS_JobCardItem` structure gives
100% location coverage on the same board.

**§3.3 (Taleo) was wrong.** "Every zone needs an opaque portal id that is not
derivable and that we do not have" — the id is printed in the career-section page
the zone's own search URL redirects to. The REST endpoint additionally requires a
`tz` header, without which it answers **500**, so a malformed request was
indistinguishable from a dead zone. AAR Corp returns 200 requisitions.

**The seed in §4 could never have worked.** `us-careers-rivian` — the only iCIMS
endpoint seeded — has migrated off iCIMS and answers 200 with a JS redirect stub.

**Two providers were confidently wrong in the other direction.** Jobvite and
BambooHR were recorded as working because their endpoints returned HTTP 200.
Jobvite's `jobs.json` returns HTML; BambooHR's listing has neither `datePosted`
nor `locationLabel`, both of which its parser read, and its `location` is a dict
whose `str()` would have been indexed as a place. Their unit tests passed
throughout, because the fixtures invented the same fields.

The generalisation §7 draws for iCIMS holds in both directions: **"returns HTML"
is not "not fetchable", and "returns 200" is not "returns jobs."**

**§4.5 needs one correction.** The prioritisation argument is sound, but the
premise that state registries are the route to the long tail does not survive
measurement. A systematic sweep of 35 state open-data portals found corporate
registries in exactly five — the five already implemented. More importantly a
registry yields a NAME, and fingerprinting needs a DOMAIN, so the chain breaks at
the second step for most rows. `services/tenant_discovery.py` reaches the same
employers vendor-first, from Common Crawl and Certificate Transparency, with no
company list at all: one run over `*.bamboohr.com` returned 96 live employer
boards. See `docs/coverage/state-registry-coverage.md`.

**G5 should be read carefully.** "51 jurisdictions with a working adapter" is
true, but 43 of those adapters are the same `FederalProxyAdapter` over the same
national datasets. It means "no jurisdiction is unreachable", not "51 registries
are being harvested".

**Vendor count.** G3 is met and exceeded: 19 providers registered, all 19
verified returning live postings. Added since this ADR: UKG, Paylocity, Rippling
and Cornerstone — payroll-attached and mid-market ATS platforms, chosen because
their customers are the small employers whose postings no aggregator carries.

**One infrastructure defect worth recording.** `make_fetchers` used the bare
`requests` module, so every request was cookieless. Cornerstone publishes a
bearer token in its anonymous shell page but rejects a search that presents the
token without the session cookie issued alongside it — a correct token still got
a 401. Fetchers now share a per-thread `requests.Session` for the life of one
member's crawl. This is the same failure class as iCIMS's two-step `hashed`
token and Taleo's `tz` header: **a handshake protocol looks like a broken
endpoint when only the last step is sent.**
