# H-1B Sponsor Board Discovery — 2026-09-15/16

**Status:** two changes shipped on this branch · the rest is a probe record
**Scope:** the largest US H-1B sponsors that hold **zero** live postings, biggest
sponsor first, software sector only.
**Ranked against:** [`docs/coverage/h1b-sponsor-gaps.csv`](../coverage/h1b-sponsor-gaps.csv)
(`sponsorship-ingest-2.0.0`, USCIS fiscal year 2026).

> **Every approval figure here is a ROLLED-UP employer total for ONE fiscal
> year**, read from `sponsorship_signals.approvals_5y` (the field name is
> historical; `data_window` on the same document says
> `fiscal_years: [2026], years: 1`). Rolled-up means all of an employer's
> filing entities: Amazon is **13,069 across 9 entities**, not the 9,337 that
> `AMAZON COM SERVICES LLC` files alone. Single-entity numbers understate every
> employer that files under more than one name, which is most large ones —
> Apple 3,885/2, EY 1,359/4, Goldman Sachs 1,050/4, Deloitte 1,869/8.

> Every number below was measured live from this environment on 2026-09-15
> and 2026-09-16.
> Nothing here is an estimate, and where a board could not be read the reason
> is recorded as a property of **us** or of **them**, never as "empty".

---

## 0. The finding that reframed the brief

**Four of the eight "nearly absent" employers were not thin. They were read
once.**

`EightfoldProvider.policy.page_size` said **20**. The live API returns
**10** rows a page and ignores `num` (`num=20` and `num=50` both come back with
ten; `start` is a row offset and pages cleanly — start 0/10/20/30 share no ids).
`list_jobs` stops on `len(positions) < size`, so `10 < 20` was true on page ONE
for every Eightfold employer we have, on every crawl, forever.

| Employer | stored active | published US | after the fix |
| --- | ---: | ---: | ---: |
| Microsoft | 29 | 1,178 | **1,178** |
| Qualcomm | 4 | 741 | **741** |
| Netflix | 10 | 204 | **204** |
| Lockheed Martin | 19 | ~2,195 | **1,500**, then 2,195 (see below) |

Lockheed stopped at exactly 1,500 on the first attempt — `150 × 10`, a
truncation wearing a round number — so `max_pages` is 260 rather than 150.

**This is a one-constant fix worth ~+3,600 postings** and it covers Microsoft
(3,688 FY2026 approvals, 1 entity) and Qualcomm (955, 4 entities) without
discovering anything. Lockheed Martin has **no** `sponsorship_signals` row at
all — a cleared-work employer, not an H-1B one — so it stays out of
`h1b_sponsor_data*.py`, where absent means unknown rather than zero.

### And a measurement artifact worth knowing about

The brief listed **Boeing** and **ServiceNow** as absent. They are not:

```
canonical_employer_id emp_4246e586719d  Boeing      383 active  (workday:boeing)
canonical_employer_id emp_39f76ae8b4c0  ServiceNow  103 active  (smartrecruiters:servicenow)
canonical_employer_id emp_f79fd2cc4167  GDIT        290 active  (workday:gdit)
```

They read as zero when counted on `employer_name`, because that field still
carries the legacy slug's display name — a Boeing posting says
`employer_name: "M.J. WILLIAMS COMPANY"` while `canonical_employer_id` is
correct. **Count employers on `canonical_employer_id`.** `employer_name` is not
an employer.

---

## 1. Apple — new source, validated, shipped

| | |
| --- | --- |
| FY2026 approvals | **3,885** (gap-list rank #2) |
| ATS | first-party (`custom_apple`, config-board adapter) |
| Endpoint | `POST https://jobs.apple.com/api/v1/search` |
| Handshake | `GET https://jobs.apple.com/api/v1/CSRFToken` on the same session |
| Live count | **6,141 rows / 4,905 distinct requisitions; 4,503 rows carry a US location** (≈3,600 distinct US), 85 s walk |
| New fetcher | no new `_fetch_*` — a `preflight` key on the existing config-board provider |

The 2026-09-02 record had this right about the symptom and one step short of
the cause. `/api/v1/search` really does answer `200` with
`{"res":{"searchResults":[],"totalRecords":0}}`. The missing half is a
**cookie**, not a credential: `jobs.apple.com/static/jobsite.main.*.js` builds
every call from `{csrf:{token:{url:"/api/v1/CSRFToken"}}}`, and one anonymous
GET there is the whole thing.

Tested three ways so the row asks for the cheapest honest thing:

| session cookie | `X-Apple-CSRF-Token` | result |
| --- | --- | --- |
| yes | yes | 6,141 |
| yes | **absent** | 6,141 |
| no | stale token | 6,141 |
| no | empty string | **0** |

So the header is not required once the session is held, and the adapter sends
no invented header. `/api/role/search` is a 404 — renamed, not gated.

**No working country filter exists.** `filters.locations` is ignored for every
value tried (`postLocation-USA`, `united-states-USA`, `USA`) and an unknown
filter key falls through to an *unfiltered* result rather than erroring — so a
location filter in the row would be a silent lie. The whole board is walked and
the US filter stays where it already is, on the row.

Ownership evidence — **counted**:

* **`employer_domain`, link/redirect chain (4a, sufficient alone).**
  `apple.com` is `employers.emp_d471a0526f61`'s verified canonical domain;
  `jobs.apple.com` is a subdomain of it, and the endpoint, the apply URL
  (`jobs.apple.com/en-us/details/{id}`) and the landing page are all on that
  host. There is no third party between the domain and the endpoint.

Recorded and **not** counted: the endpoint responded (`SIGNAL_ENDPOINT_RESPONDS`),
and the slug `apple` matching the name (`SIGNAL_SLUG_SIMILARITY`).

---

## 2. SuccessFactors RMK — the provider works, and had never been run

`ats_providers/successfactors.py` opens with *"NOTHING here was verified against
the live vendor from this environment — there is no outbound network in it."*
It is verified now.

`careers.ey.com` returns **300 rows on the first try** through the shipped
provider, against a published total of **8,297** (`Results 1 to 25 of 8297`).
The `/search/?q=&sortColumn=referencedate&sortDirection=desc&startrow=N`
transcription is correct, `startrow` really is a row offset stepping by 25, and
`_parse_rows` reads the stock template.

**EY is NOT pasted into a catalog**, deliberately, for three reasons:

1. **Ownership is one class short.** `careers.ey.com` is on EY's registrable
   domain, which is the `employer_domain` signal — but `www.ey.com` is a JS
   shell whose raw HTML contains no `careers.ey.com` reference, so there is no
   link chain, and a curated catalog row would be our own data corroborating
   our own data. ADR-006 needs a second class from a *different* class.
2. **EY has no `employers` row at all** (gap list bucket `no_employer_row`),
   so there is nothing to adjudicate ownership against yet.
3. The board publishes **no date column** — every row is `DATE_UNKNOWN` — and
   the sampled rows are overwhelmingly non-US (London, Noida, ...). The US
   share is unmeasured; `?locationsearch=` and `?location=` are both ignored.

What it unlocks is bigger than EY: SuccessFactors RMK is a **live, working
vendor** for us now, and it is the answer for a chunk of the Indian IT-services
cohort. `careers.wipro.com` is RMK-shaped but serves its board at a different
path (`/careers-home/jobs`, zero `data-row` markers at `/search/`), so it needs
a second template in `_parse_rows`, not a new provider.

`_fetch_successfactors` is registered in `ENTERPRISE_FETCHERS` so a future row
works on both the interactive tab and the crawl from one paste.

---

## 3. Probe record — what could not be reached, and why

The distinction is the whole point: a refusal, a wall and an empty board are
three different claims, and only one of them is ours to make.

| Employer | FY2026 | What answered | Verdict |
| --- | ---: | --- | --- |
| **Google** | 3,183 | `www.google.com/robots.txt` **Disallows** every paged `/about/careers/applications/jobs/results` URL (`?page=`, `/?page=`, `?*&page=`) in the `*` group | **robots refusal.** Page 1 is server-rendered HTML with 20 job links and is allowed; paging is not. We do not page it. Not addable without written permission. |
| **Meta** | 2,563 | `metacareers.com/robots.txt` publishes `Sitemap: /jobsearch/sitemap.xml` and Disallows only `/ajax/`, `/*.php`, `/login/`, … — but the file opens with *"Collection of data on Facebook through automated means is prohibited unless you have express written permission"* | **ToS flag, human decision.** The machine-readable directives allow it; the human-readable notice forbids it. Not registered. |
| **Tesla** | 1,574 | `www.tesla.com/cua-api/apps/careers/state` → Akamai `403 Access Denied` | **blocked** (unchanged from 2026-09-02). Not empty. |
| **Bloomberg** | 587 | `careers.bloomberg.com` → `403` + PerimeterX captcha payload | **blocked.** Bypassing a captcha is out of scope. |
| **SAIC** | — | `www.saic.com/careers` → `403` Cloudflare *"Just a moment…"* | **blocked.** |
| **Cognizant** | 3,530 | `careers.cognizant.com` → `403` Cloudflare interstitial. A `taleo:cognizant` candidate row exists | **blocked** at the marketing site; Taleo portal id not derivable. |
| **General Dynamics** | — | `www.gd.com/careers` → `403` | **blocked.** Note GD**IT** is already covered (`workday:gdit`, 290 active). |
| **Intuit** | 640 | `jobs.intuit.com/search-jobs/results` (Radancy shape — we have the provider) → **robots.txt Disallow**; `intuit.com` returned `429` and cooled the vendor for 900 s | **robots refusal on the board path.** Worth a re-read of the robots file at a later date; do not retry inside a cooldown. |
| **Goldman Sachs** | 1,050 | `higher.gs.com/roles` → 200, Next.js shell; `…/services/analytics/roles` → 404 | **endpoint not found.** Needs a rendered pass. |
| **ByteDance / TikTok** | 435 / 474 | `jobs.bytedance.com/api/v1/search/job/posts` → `405`; on a second pass a `GET` returns the **CN headhunter portal** shell (`字节跳动猎头平台`), not the global board | **wrong endpoint**, not blocked. The global board is elsewhere. |
| **MITRE** | — | `mitre.wd5.myworkdayjobs.com/wday/cxs/mitre/MITRE_Jobs/jobs` → `404 not found: Job_Posting_Site_ID=MITRE_Jobs`; `mitre.org/careers/join-us` → `403` | **wrong Workday site path**, tenant is right — the 404 names the tenant back to us. The careers page that would settle it is behind a wall. |
| **Infosys, TCS, HCLTech, Capgemini, LTIMindtree, Tech Mahindra, Mphasis** | 4,152 / 4,450 / 1,525 / 1,612 / 1,289 / 636 / 657 | see §3.1 — each was fingerprinted individually on a second pass | **mostly reachable, and mostly not worth a row.** §3.1. |

### 3.1 The Indian IT-services cohort, individually

Together these seven hold **14,421** FY2026 approvals — more than Apple,
Microsoft and Google combined — so it is worth being precise about why almost
none of it is addressable as US inventory.

| Employer | FY2026 | Vendor, from its own careers page | Outcome |
| --- | ---: | --- | --- |
| TCS | 4,450 | `tcs.com/careers` → `403`; `ibegin.tcs.com` does not resolve from here | **unreachable** |
| Infosys | 4,152 | `career.infosys.com` — Angular shell, no vendor fingerprint, no XHR path in the served bundles | **no endpoint found** |
| HCLTech | 1,525 | `hcltech.com/careers` read-timeouts twice | **unreachable** |
| Capgemini | 1,612 | **custom API host found**: `cg-jobstream-api.azurewebsites.net/api`, referenced from their job-search page. Six path guesses all `404` | **host known, path unknown.** Not guessed into data — see the `icims.com/<tenant>` lesson. |
| **LTIMindtree** | 1,289 | **SuccessFactors RMK at `careers.ltm.com`**, linked from `ltimindtree.com/careers`. Reads cleanly through the shipped provider: 54 rows, real dates, real apply URLs | **reachable and genuinely thin** — see below |
| Tech Mahindra | 636 | `careers.techmahindra.com` — RMK-shaped shell, 0 rows at `/search/` | **JS-rendered board** |
| Mphasis | 657 | RippleHire (`mphasis.ripplehire.com`), a vendor we have no provider for | **new vendor needed** |
| Wipro | 1,192 | **SuccessFactors confirmed** — `rmkcdn.successfactors.com` assets plus `hcm55.sapsf.eu` / `career55.sapsf.eu` on `careers.wipro.com`. But `/search/` renders **0 rows**: this is the Career-Site-Builder variant of RMK, which fills the table over XHR | **JS-rendered board.** Same provider, second template. |

> **LTIMindtree is the measurement that reframes this whole cohort.** Its
> board is reachable, correct and complete — and it is **54 postings**,
> essentially all Chennai / Singapore / São Paulo. `?locationsearch=United+States`
> returns nothing. An employer with 1,289 US H-1B approvals publishes a
> two-digit public board, because staffing-model hiring happens against client
> requisitions that never reach a careers site.
>
> So `approvals × software_likelihood` **over-ranks this cohort for board
> discovery**. The gap list is right that they are our biggest uncovered
> sponsors and wrong that a board would fix it. Anything here should be ranked
> on *published board size*, which is one cheap request per employer, before
> any provider work is funded. Two of the seven need a new vendor (RippleHire)
> and two need a rendered pass — that is real engineering spent on employers
> whose public US board may be a few dozen rows.

### 3.2 Others fingerprinted on the second pass

| Employer | FY2026 | Found | Outcome |
| --- | ---: | --- | --- |
| Deloitte | 1,869 | `apply.deloitte.com` + `applyglobal.deloitte.com`, no vendor fingerprint; RMK template returns 0 | no endpoint found |
| Walmart | 1,669 | `careers.walmart.com` + `cms.careers.walmart.com`, first-party, no vendor fingerprint | no endpoint found |
| IBM | 1,230 | `careers.ibm.com` referenced twice from `ibm.com/careers/search`; `/api/jobs` is a `404` HTML page | no endpoint found |
| Goldman Sachs | 1,050 | `higher.gs.com` Next.js shell, 25 URLs, nothing ATS-shaped | needs a rendered pass |
| ByteDance / TikTok | 435 / 474 | `jobs.bytedance.com` — `/api/v1/search/job/posts` serves the CN headhunter portal shell, not the global board | wrong endpoint |
| KPMG US | 295 | `www.kpmguscareers.com` with `jobadx` tracking pixels (Radancy-adjacent). `/search-jobs`, `/jobs`, `/search-jobs/results` all `404` | path unknown |

**RippleHire** (`*.ripplehire.com`) is the recurring unmatched host of this
pass — LTIMindtree and Mphasis both run it. That is exactly the signal
`career_portal_discovery.unknown_hosts` exists to produce, and it is the next
vendor to build **if** §3.1's board-size test says those boards are worth it.

### Two employers whose *domain* the discovery pass could not confirm

`PortalDiscovery._resolve_domain` returned `no reachable domain` for Google,
ServiceNow, Bloomberg, Infosys, HCLTech and Collins Aerospace — their
homepages refuse a non-browser UA, which `_page_mentions` then reads as "this
is not their site". That is a **different** failure from "no careers page" and
it currently looks identical in the results.

---

## 4. A bug this pass found in our own politeness layer

`career_portal_discovery.RobotsCache` uses `urllib.robotparser`, which is
**first-match**, not longest-match, and normalises `Disallow: /?` into
`Disallow: /` (`urlunparse` drops an empty query). Both are wrong, and both
fail CLOSED — we refuse boards the employer explicitly allows.

Measured on two files:

* `explore.jobs.netflix.net` and `careers.qualcomm.com` publish
  `Disallow: /` followed by `Allow: /api/apply` and `Allow: /api/pcsx`.
  Under the real (longest-match) rule those APIs are **allowed**; stdlib says
  no to both. These are boards we are already crawling.
* `www.google.com` publishes `Disallow: /?` (query-only root). Stdlib turns
  that into a blanket `Disallow: /`, so *every* Google URL reads as refused —
  including the careers paths Google took the trouble to enumerate separately.

Fixing this is a small, self-contained change to `RobotsCache` (parse groups,
apply longest-match with `*`/`$`), and it is a **correctness** fix in both
directions. It is not on this branch: it changes what the crawler is permitted
to fetch, which deserves its own review.

### And the larger half of the same finding

**`RobotsCache` has exactly one caller: `PortalDiscovery._get_html`.** Grep it.
Nothing in the ingest path — `job_ingest_worker`, `_list_via_provider`,
`ats_providers.http.make_fetchers`, any `provider.list_jobs` — reads
robots.txt at all. So the posture today is inverted:

* **discovery**, which reads a handful of public marketing pages per employer,
  is gated by a parser that over-refuses;
* **the crawl**, which fetches an employer's board on a schedule forever, is
  gated by nothing.

That is very likely fine in substance — every one of these endpoints is a
public board API the employer publishes for exactly this purpose, and several
(Netflix, Qualcomm) `Allow:` the API path explicitly. But it is fine by
accident rather than by check, and "be a well-behaved crawler" is a claim we
currently cannot evidence for the path that does almost all of the fetching.

The two fixes belong together and in that order: make the parser correct, then
put it in front of the listing fetchers, where a correct parser will allow the
boards we already read and refuse the ones (Google's paged results, Intuit's
`/search-jobs/`) that we should not be taking anyway.

---

## 5. What was pasted, and what it is worth

| Employer | FY2026 | ATS + tenant | Endpoint | Live postings | Ownership evidence | New fetcher? |
| --- | ---: | --- | --- | ---: | --- | --- |
| **Apple** | 3,885 | `custom_apple` / `apple` | `POST jobs.apple.com/api/v1/search` (+ `GET /api/v1/CSRFToken`) | 6,141 rows / 4,905 distinct; **4,503 US-located rows** (≈3,600 distinct US) | 4a domain chain (`apple.com` → `jobs.apple.com`), **counted**; endpoint-responds + slug-similarity **recorded only** | No — a `preflight` key on `ConfigBoardProvider` |
| **Microsoft** | 3,688 | `eightfold` / `microsoft` (already registered) | `apply.careers.microsoft.com/api/pcsx/search` | **1,178** (was 29) | already `validated` | No — `page_size` |
| **Qualcomm** | 955 | `eightfold` / `qualcomm` (already registered) | `careers.qualcomm.com/api/pcsx/search` | **741** (was 4) | already `validated` | No — `page_size` |
| **Lockheed Martin** | — | `eightfold` / `lockheedmartin` (already registered) | `lockheedmartin.eightfold.ai/api/pcsx/search` | **1,500+** (was 19) | already `validated` | No — `page_size` + `max_pages` |
| **Netflix** | 258 | `eightfold` / `netflix` (already registered) | `explore.jobs.netflix.net/api/apply/v2/jobs` | **204** (was 10) | already `validated` | No — `page_size` |
| EY (Ernst & Young) | 1,359 | `successfactors` / `careers.ey.com` | `GET careers.ey.com/ey/search/?startrow=N` | 300 read, **8,297 published** | `employer_domain` only — **one class short**, see §2 | No — provider existed, unverified |
| LTIMindtree | 1,289 | `successfactors` / `careers.ltm.com` | `GET careers.ltm.com/search/?startrow=N` | **54 total, ~0 US** | 4a link chain (`ltimindtree.com/careers` → `careers.ltm.com`) | No — same provider |

> The last two rows are **not pasted**. EY is one ownership class short (§2);
> LTIMindtree is proven, owned and fully readable, and publishes 54 jobs, none
> of them US (§3.1). Both are recorded here so the next pass starts from a
> measurement instead of repeating the probe.

**Expected inventory gain: ~+7,200 active US postings**, ~3,600 of them Apple's
and ~3,600 from four employers we already had and were reading one page of.

None of this is live. Every source and every EventBridge rule on the crawler is
still disabled — see [`docs/ops/h1b-sponsor-source-rollout.md`](../ops/h1b-sponsor-source-rollout.md).

---

## 6. Things found in passing, not fixed here

* **Palantir** (`lever:palantir`, `validated`, catalog row present) holds
  **0** postings while the live board returns a 6 MB payload. Lever holds 4,755
  active rows in total and 4,157 of them are one marketplace board
  (`jobgether`), so this is the crawl not having swept the source — an
  operations state, not a catalog gap.
* **`career_pages_catalog.py` carries the Palantir row twice** (lines 14 and
  432, identical). Harmless — `_load_career_members` keys on `(ats, slug)` —
  but it makes the catalog count wrong.
* **Netflix's Eightfold rows carry no date.** 204 rows, `dated=0`, while the
  pcsx tenants are 100% dated. The `classic` variant's `postedTs`/`creationTs`
  are absent on that tenant; worth one look before the classic path is trusted
  for freshness.
* **`career_sources` has a `bamboohr:walmart` row in `content_validated`.**
  Walmart is not on BambooHR. Five postings. Worth an ownership re-adjudication.
