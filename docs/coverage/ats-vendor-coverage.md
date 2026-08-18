# ATS vendor coverage — what we crawl, what we cannot, and why

**Last verified against live endpoints: 2026-08-18.**
Regenerate the "works" column with `PYTHONPATH=shared/python python3 scripts/live_provider_check.py`.

This document exists because "we support vendor X" is a claim about code, and
the only claim worth making is about a live response. Every row below was
established by fetching the vendor today, not by reading its docs and not by
trusting a previous verdict. Two of those previous verdicts were wrong in
opposite directions, and both cost real coverage:

* iCIMS was recorded as **"Tier C, bot-walled, do not build"** because a probe
  built to find JSON APIs found no JSON. "Returns HTML" is a statement about a
  response, not about feasibility.
* Jobvite and BambooHR were recorded as **working** because their endpoints
  returned HTTP 200. Jobvite's `jobs.json` returns *HTML*; BambooHR's listing
  has no `datePosted` and no `locationLabel`, both of which the parser read.
  **A status code is not a payload.**

The general rule this yields, and the reason `scripts/live_provider_check.py`
exists at all: **a verdict is only as wide as the instrument that produced it.**
A fixture test proves a parser agrees with its author. Only a live fetch proves
a vendor agrees with us.

---

## 1. Vendors we crawl

19 registered providers. "Rows/board" is what one representative live board
returned during verification.

| Vendor | Transport | Date | Description | Salary | Notes |
|---|---|---|---|---|---|
| Greenhouse | JSON | exact | detail | sometimes | Single API host; slug-probeable. |
| Lever | JSON | exact | listing | sometimes | Single API host. |
| Ashby | JSON | exact | listing | sometimes | Single API host. |
| SmartRecruiters | JSON | exact | detail | sometimes | Single API host. |
| Workable | JSON | exact | detail | sometimes | Single API host. |
| Recruitee | JSON | exact | listing | — | Per-tenant subdomain. |
| NeoGov | JSON | exact | listing | often | US state/county/city government. |
| Workday | JSON | **relative** | detail | sometimes | `"Posted 2 Days Ago"`, so ADR-001 relative. Tenants shard `wd1`–`wd103`. |
| Oracle ORC | JSON | exact | detail | sometimes | True pagination + `TotalJobsCount`; newest-first. |
| iCIMS | **HTML** | detail-only | listing snippet | — | See §2.1. |
| Taleo | JSON | exact | — | — | See §2.2. |
| Jobvite | **HTML** | **none** | detail | — | See §2.3. |
| BambooHR | JSON | detail-only | detail | **yes** | See §2.4. |
| Phenom | JSON | exact | detail | sometimes | Per-employer host; not derivable. |
| Eightfold | JSON | exact | detail | sometimes | Per-employer host; rate-limits aggressively. |
| **UKG / UltiPro** | JSON (POST) | exact | listing | — | **New.** See §3. |
| **Paylocity** | HTML + embedded JSON | exact | detail | **yes** | **New.** See §3. |
| **Rippling** | JSON | detail-only | detail | **yes** | **New.** See §3. |
| **Cornerstone** | JSON (POST + JWT) | exact | **none** | — | **New.** See §3.1. |

### Why the last three matter disproportionately

Every vendor above the line serves either large enterprises (Workday, Oracle,
iCIMS, Phenom, Eightfold) or venture-backed technology companies (Greenhouse,
Lever, Ashby). Both populations are already heavily aggregated — a Greenhouse
posting at a funded startup reaches a dozen job boards within hours, and
hundreds of applicants with it.

The employers that are genuinely under-served are small and mid-sized firms.
They do not buy a standalone ATS; they get recruiting bundled with the payroll
system they already pay for. In the US that is overwhelmingly UKG, Paylocity,
Paycom, Paychex, ADP and (newly) Rippling. Those boards are poorly aggregated
*because* each is small and none is a name a scraper thinks to target.

That is the population where "few applicants, low visibility" is literally true,
so it is where added coverage converts most directly into a better outcome for a
job seeker.

---

## 2. Corrections to the record

### 2.1 iCIMS — HTML, and it works

Protocol: `GET https://{tenant}.icims.com/jobs/search?ss=1&in_iframe=1&searchRelation=keyword_all&pr={page}`.

Four things had to be right, and each was wrong:

1. **Parse the cards, not the anchors.** Rows come as
   `<li class="iCIMS_JobCardItem">` containing a `<h3>` title, a
   `<div class="description">` snippet, and a `<dl>` of labelled fields
   (job type, department, category, location, remote classification). Walking
   bare `<a href*="/jobs/">` and reading the nearest ancestor's flattened text
   produced titles prefixed `"Job Title "` — iCIMS puts a
   `<span class="sr-only field-label">Job Title </span>` inside the anchor for
   screen readers — and found a location on roughly one row in six. Reading the
   structure gives **100% location coverage** on the same board.
2. **Rewrite the coded location.** iCIMS publishes `US-GA-Atlanta`. No resume
   says that. It is now normalised to `Atlanta, GA, US` before it reaches
   `normalize_posting`.
3. **Distrust some `datePosted` values.** See §4.
4. **Recognise a migrated portal.** See §5.

**Pacing is vendor-wide, not per-host.** `*.icims.com` is wildcard DNS, so every
candidate slug resolves and DNS proves nothing; and the anti-bot wall counts per
*source IP* across all tenants — six concurrent requests over sixty distinct
hostnames returned `405 Human Verification` on all sixty. Hence
`pacing_key="icims.com"`, `max_concurrency=1`, `min_interval=2.5s`. A 405 is a
challenge, not a permanent 4xx; treating it as permanent would retire live
employers on the first burst.

**Not every `*.icims.com` host is a board.** `careers-emory` is a landing page
that iframes the real one; Emory's jobs are on `staff-emory`. Portals matching
`internal-|employee-|redeploy-` are employee-only: they serve well-formed rows
that an outsider cannot apply to, which is worse than omitting the employer.

### 2.2 Taleo — the portal id is published

ADR-004 §3.3 declined to build Taleo because "every zone needs an opaque portal
id that is not derivable and that we do not have." The id is not derivable, but
it is not hidden:

```
GET https://{zone}.taleo.net/careersection/jobsearch.ftl
  → 302 /careersection/{n}/jobsearch.ftl
  → body contains  portal=101430233
```

One extra GET per zone. Then:

```
POST /careersection/rest/jobboard/searchjobs?lang=en&portal={id}
```

**The `tz` header is required.** Without it this endpoint answers **500**, not
400 — so a malformed request is indistinguishable from a broken zone, which is
how Taleo stayed "unbuildable". Isolated on `aarcorp`: `tz` alone → 200 with 25
rows; `Referer` alone → 500; cookies irrelevant. With it, AAR Corp returns
**200 requisitions** with titles, locations and real posted dates.

Column semantics are per-tenant and read from the response (`linkedColumn`,
`locationsColumns`); a tenant configured to show only a title column genuinely
has no location or date to give, and gets none rather than an invented one.

Taleo is still shrinking — Oracle has migrated customers to ORC, and five of six
zones probed previously were NXDOMAIN — but "shrinking" is not "zero".

### 2.3 Jobvite — HTML, no dates at all

`jobs.jobvite.com/{slug}/jobs.json` returns `Content-Type: text/html`.
`/api/v1/company/{slug}/jobs` 200-redirects **every** slug to Jobvite's
marketing site at `/support/job-seeker-support/?invalid=1`. The old provider
therefore returned zero rows for every tenant, forever, while its test passed.

The real board is `GET /{slug}/search`, complete on one page, as a table of
`td.jv-job-list-name` / `td.jv-job-list-location`.

**Jobvite publishes no posting date anywhere** — not on the listing, not on the
detail page, and there is no `JobPosting` JSON-LD. The provider declares
`date_confidence = unknown` and ships empty dates rather than inventing them.

Multi-site reqs render `<div class="jv-meta">2 Locations</div>`. That is a
count, not a place; storing it would make the req match nowhere and appear to
hire in a town called "2 Locations".

### 2.4 BambooHR — the listing is an index, not a record

`GET /{tenant}.bamboohr.com/careers/list` returns
`{meta:{totalCount}, result:[…]}` — with **no** `datePosted` and **no**
`locationLabel`. The old provider read both. `location` is a *dict*
(`{"city": "Wattana", "state": "Bangkok"}`), so `str()` of it would have indexed
`{'city': 'Wattana', 'state': 'Bangkok'}` as a place.

The date, the full JD and the pay band are on
`GET /careers/{id}/detail` → `result.jobOpening`. That matters more here than
elsewhere: BambooHR's customers are overwhelmingly small employers, and a row
with no description and no salary is not much use to a seeker even once indexed.

---

## 3. New vendors added

| Vendor | Identity | Enumerable? |
|---|---|---|
| UKG / UltiPro | `{customer code}/JobBoard/{board GUID}` | No — **both** ids opaque. Fingerprint only. |
| Paylocity | `{board GUID}` | No — GUID. Fingerprint only. |
| Rippling | readable slug on one shared host | Yes — a slug guess is one definitive call. |

### 3.1 Cornerstone — the 401 was not a wall

`/services/x/career-site/v1/search` answers **401** anonymously, which reads as
"login required" and is where an earlier look stopped. It is not. The anonymous
shell page inlines `csod.context`, which carries a 2 KB JWT that the page's own
bundle sends as `Authorization: Bearer`.

That makes Cornerstone the **third** vendor here with the same shape — iCIMS
hands out a `hashed` token, Taleo prints its `portal` id, Cornerstone inlines a
JWT. The pattern is worth naming: **the page gives you the credential; the job
is to read it, not to guess around it.**

Two further details, each of which independently returns zero:

* **The token alone is not enough.** The search also requires the
  `ASP.NET_SessionId` cookie issued with the shell page. The fetchers used the
  bare `requests` module and were therefore cookieless, so a correct token still
  got a 401. They now share a per-thread `requests.Session` for the life of one
  member's crawl. This is the same failure class as iCIMS's two-step token and
  Taleo's `tz` header: **a handshake protocol looks like a broken endpoint when
  only the last step is sent.**
* **Site ids are not derivable and are not always 1.** University of Arizona
  serves 324 requisitions on site **4** and zero on site 1; Alamo Colleges
  serves 212 on site 1. A provider assuming 1 reports Arizona as not hiring. The
  provider probes a small range and caches the id that answered.

**Cornerstone publishes no job description on any public route.** Every
documented detail path 404s and the posting page carries no JSON-LD, so rows are
title/location/date only. That is a genuine match-quality limit — a resume can
only be scored against a title and a location for these — and it is recorded
here rather than hidden.

Two field traps, both caught by the live check rather than by tests:

* **UKG** publishes `Address.State` and `Address.Country` as objects
  (`{"Code": "SC", "Name": "South Carolina"}`), not strings — the same class of
  bug as BambooHR's `location`. Codes are preferred (`SC`), because that is what
  postings *and* resumes use.
* **Paylocity's** embedded listing `Description` is a fixed **110-character
  teaser** on every row, not the job description. Indexing it would leave every
  posting with a truncated first sentence and nothing to match a resume
  against. The real JD and a structured `baseSalary` are in the detail page's
  schema.org block.

---

## 4. Fabricated posting dates

`staff-emory.icims.com` publishes, on every requisition:

```
datePosted   2024-08-18T04:54:40.821Z     ← render time minus 2 years
validThrough 2027-08-18T04:54:40.821Z     ← render time plus 1 year
```

The sub-second component moves with each request: these are minted from the
server's clock at render time. Meanwhile `careers-fremontbank.icims.com`
publishes `2026-08-14T04:00:00.000Z`, a genuine date. **This is a per-portal
misconfiguration, not a vendor-wide one**, so it cannot be handled by
disbelieving iCIMS as a whole.

Ingesting the synthetic value is not cosmetic. A date two years old is dropped
outright by the freshness cutoff, so the employer silently contributes zero
postings — and a shift the other way would re-date stale requisitions as fresh,
the exact harm ADR-001 exists to prevent.

`ats_providers.util.looks_minted_now()` rejects a timestamp only when **both**
hold:

* it has a **non-zero sub-second component** — real feeds publish whole seconds,
  usually midnight or a whole hour, because a posting date is a date, not an
  instant; and
* its **time of day** is within five minutes of ours, comparing time-of-day only
  so the vendor's year offset is irrelevant.

Both conditions are required because a false positive discards a real date. A
`.000Z` stamp is never rejected however close to now it falls.

---

## 5. Migrated portals

`us-careers-rivian.icims.com` — the *only* iCIMS seed ADR-004 shipped — answers
**HTTP 200** with a 143-byte body whose entire content is

```html
<script>window.top.location.href = 'https://careers.rivian.com/jobs';</script>
```

Rivian has moved off iCIMS. Read as an ordinary empty board, that tenant is
retained and re-crawled indefinitely and the employer is lost — even though
their jobs moved to a vendor we do support.

`PortalMovedError` now carries the destination, and `validate()` reports
`dead — migrated to https://careers.rivian.com/jobs`, so discovery can
re-fingerprint the new host instead. Detection requires the body to carry **no
job link at all** (a real board always links its requisitions) *and* to be under
2 KB; live pages assign `location.href` in their own scripts, and reading one of
those as a migration would retire a working board.

---

## 6. Investigated and NOT built — with the blocker

Listed so the next reader inherits the finding instead of re-deriving it. None
of these is "impossible"; each is a specific, named obstacle.

| Vendor | What happens | Blocker | Worth building? |
|---|---|---|---|
| **JazzHR** (`{t}.applytojob.com`) | `/apply/` → 200, 211 KB HTML; `jobs.json` → 404 | No JSON; needs an HTML parser like iCIMS/Jobvite. 17 tenants found. | **Yes.** Pure-SMB base. |
| **Dayforce / Ceridian** (`{t}.dayforcehcm.com`) | not probed to completion | Common Crawl index 504'd on this domain during enumeration. | Yes — mid-market. |
| **Paycom** (`{t}.paycomonline.net`) | not probed to completion | No tenants surfaced in the index sample taken. | Yes — SMB. |
| **BreezyHR** (`{t}.breezy.hr`) | `/json` → marketing HTML for guessed slugs | Guessed slugs were wrong; needs real tenants first. Index reports 2 pages of data. | Yes — SMB. |
| **Teamtailor** (`{t}.teamtailor.com`) | `jobs.json` → 404 | Same; no tenants surfaced in the sample. | Moderate — mostly EU. |
| **Zoho Recruit** (`{t}.zohorecruit.com`) | not probed | 61 tenants found and waiting. | Moderate. |
| **ADP Workforce Now** | not probed | Boards are behind `cid`-parameterised shells. | Yes — very large SMB base. |
| **SAP SuccessFactors** | not probed | Sites are heavily white-labelled onto employer domains, so there is no single host to enumerate; reachable by fingerprinting instead. | Yes — large enterprise. |
| **USAJOBS** | — | Public REST API requires a free API key. **No technical blocker; it needs a credential.** | **Yes — highest value/effort.** ~30k federal postings. |
| **PeopleAdmin / Interfolio / PageUp** | not probed | Higher-education ATS; per-institution hosts. | Moderate. |

### Two vendors that are genuinely hard

* **Anything requiring a headless browser.** `jobpilot-backend/CLAUDE.md` makes
  Playwright apply-only, and zip Lambdas cannot carry Chromium. This is a
  deliberate constraint, not an oversight: iCIMS was beaten with a correct
  two-step HTTP protocol instead, which is cheaper and faster. A vendor that
  *truly* cannot be read without executing JavaScript is out of scope until that
  constraint changes.
* **Boards behind a login or a CAPTCHA.** Not attempted, and should not be.

---

## 7. How tenants are discovered

Fingerprinting (`career_portal_discovery`) is **company-first**: it can only
reach an employer we already know exists and whose domain we already have. That
is the binding constraint on the long tail — state business registries publish
*names*, not domains, and the smallest employers are missing from every company
list one can buy.

`services/tenant_discovery.py` inverts it. Many vendors give every customer
their own hostname, and those hostnames are public record in two archives that
exist for other reasons:

* **Common Crawl's URL index** — hostnames that really serve pages.
* **Certificate Transparency** (Cert Spotter, crt.sh) — hostnames that exist
  even if no crawler reached them.

Measured index sizes (`showNumPages`, 2026-08-18): `breezy.hr` 2 pages;
`applytojob.com`, `csod.com`, `dayforcehcm.com`, `zohorecruit.com`,
`teamtailor.com`, `paycomonline.net`, `taleo.net` 1 page each — i.e. **fully
enumerable**. `icims.com` is large enough that the index times out on a
whole-domain query, so iCIMS enumeration is currently **partial**; that is the
one known gap in this channel.

Two limits worth recording:

* **Certificate Transparency is useless where the vendor uses a wildcard
  certificate.** For `icims.com` it returned only iCIMS's own infrastructure
  (`api-us1`, `billing`, `academy`), because `*.icims.com` covers every tenant
  with one cert. It works where certs are per-tenant.
* **The Wayback CDX API is impractical here.** Results are ordered by SURT key,
  so tens of thousands of `www.icims.com` captures precede the first tenant
  subdomain; four pages of 8,000 rows surfaced five hosts.

Nothing in this module *decides* a tenant is real. A hostname in an archive is a
candidate: `*.icims.com` is wildcard DNS, Common Crawl records URLs that have
since 404'd, and a certificate outlives the customer that needed it. Only
`provider.validate()` promotes a candidate — "only a successful content fetch
does". And nothing is synthesised from a company name: every hostname returned
was observed by a third party.
