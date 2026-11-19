# Workday employers: other legitimate sources (measured 2026-11-17)

Since about 2026-11-14 Workday's job sites sit behind Cloudflare bot management.
From the Oracle box (150.136.238.50) most requests get `429 Security Check`
(`cf-mitigated: challenge`), even the first one after minutes of silence. Workday
supplied 103,578 of about 247,000 open postings, from 1,624 employers, and the
top 150 employers hold half of them (51,450).

Everything below is read-only. Requests were paced at least 1 s apart, obeyed
robots.txt and read public pages only.

## 1. Sources we already cover for the same employers

Only 13 Workday employers (1,534 postings, 1.5%) have a covered non-Workday
career source. Our existing sources barely overlap Workday.

## 2. DirectEmployers NLx (`<company>.jobs`)

For the top 150 employers, the probe tried up to five likely `.jobs` domains
each against `prod-search-api.jobsyn.org`, with no challenges. The company name
in the feed matched for 7 employers: Northrop Grumman, TD Bank, Sysco, BMO,
Sanofi, U.S. Bank and United Rentals. Together they hold 2,506 Workday postings
(about 5% of the top 150).

`mass.jobs` answered with a shared Massachusetts board listing many employers,
not Mass General Brigham's own. The name check rejected it.

**Not added.** None of the employers' own sites links to its `.jobs` domain.
`directemployers_nlx` requires that proof: the shared API host proves no
ownership. U.S. Bank is already covered through Phenom (`careers.usbank.com`).

## 3. Employers' own career sites (top 40)

The probe followed the careers link on each employer's own homepage. The
registry has a `canonical_domain` for only 3 of these 40 employers, so well-known
corporate domains were used, and only the homepage's own link counts.

| Outcome | Employers | Workday postings |
|---|---:|---:|
| Own site on Phenom, Eightfold or Radancy | 7 | about 4,100 |
| Own site with its own job pages (Cleveland Clinic, Labcorp, TD, Sanford) | 4 | about 2,000 |
| Careers page only links to Workday | 8 | about 4,000 |
| Careers link not found (script-rendered homepage) or no domain | 14 | about 7,200 |
| Homepage 403 (T-Mobile, Meijer, JD Sports); treated as walls | 3 | about 1,400 |

### Added in this change

| Employer | Site | Provider | Ownership chain | Live check |
|---|---|---|---|---|
| TJX Companies | jobs.tjx.com | Phenom | tjx.com links it | 9,777 listed; job page 5,457 chars |
| RTX | careers.rtx.com | Phenom | rtx.com links it | 4,540 listed (worldwide) |
| CVS Health | jobs.cvshealth.com | Phenom | cvshealth.com links it | 18,929 listed; job page 9,946 chars |
| DaVita | careers.davita.com | Phenom | davita.com links it | 3,259 listed |
| Thermo Fisher | jobs.thermofisher.com | Phenom | thermofisher.com links it | 3,237 listed; has test rows |
| Northrop Grumman | jobs.northropgrumman.com | Eightfold PCSX, domain `ngc.com` | northropgrumman.com/careers links it | 3,709 listed; detail 7,264 chars |

Listed totals are whole boards across all ages and countries. The crawl keeps
its usual age window and US filter.

**Not added:** Genuine Parts (jobs.genpt.com) runs Radancy TalentBrew, which
`radancy_talentbrew` reads only partially (15 rows per page). Northrop's
`ngc.eightfold.ai/careerhub` is the internal employee hub, not the public board.

### Second batch (2026-11-18)

The probe (`~/aithrex-probes/nov18/p5_sites.py`, paced 1.2 s, robots.txt
obeyed) tried the likely careers hosts of 20 more top-40 employers.

| Employer | Site | Provider | Ownership chain | Live check |
|---|---|---|---|---|
| Dollar Tree | careers.dollartree.com | Phenom | under dollartree.com | 18,537 listed; job page 9,015 chars |
| Trinity Health | jobs.trinity-health.org | Phenom | trinity-health.org/join-our-team links it | 6,675 listed; job page 9,277 chars (short `/job/<id>` path) |
| Abbott | www.jobs.abbott | Phenom | abbott.com and abbott.com/careers.html link it | 2,353 listed; job page 7,658 chars |

Each listing's `reqId` is the Workday requisition number (Dollar Tree
`R-015906`, Abbott `31166532`, Trinity `00098446`), and the Workday ids end in
the same number, so the copies merge. Abbott's Workday ids are
`workday-abbott-abbottcareers:<req>`; the key uses the stored tenant and the
last segment, so this still merges (pinned in the tests).

**Not added:**

| Employer | Why |
|---|---|
| Johnson & Johnson, Verizon, Wells Fargo | careers site answers 403: a wall |
| Citi (jobs.citi.com), Sysco (careers.sysco.com) | Radancy TalentBrew, read only partially |
| Cleveland Clinic, TD, Labcorp, Sanford | Symphony Talent job pages that link to Workday: a new reader would be needed |
| Nordstrom, Mass General Brigham, Fresenius, Meijer | careers page links straight to Workday |
| Fifth Third, Tapestry, JLL | nothing readable found, or the host did not answer |

Dollar Tree (18,537) and CVS (18,929) list more than the 60 pages x 200 rows
the Phenom reader reads, in relevance order, so some fresh rows can be missed.
The listing is then marked truncated, so nothing is closed for it.

## How duplicates stay one card

These sites serve the same requisitions as the employer's Workday board.

- **Tenant.** Each seed's `tenant` is the employer's Workday tenant: `tjx`,
  `globalhr`, `cvshealth`, `davita`, `thermofisher`, `ngc`.
- **Posting id.** `requisition_ids` makes the posting id the Workday
  requisition number: Phenom `reqId` and Eightfold `atsJobId`.
- **Result.** The feed's `(tenant, requisition)` key (`inventory_feed._dedupe`)
  merges the two copies into one card and keeps the other in `source_variants`.

Existing Phenom and Eightfold boards keep their old ids.

## Full descriptions

The Phenom listing carries only `descriptionTeaser`, a few hundred characters,
so every Phenom posting used to score as having no readable requirements.
`PhenomProvider.has_detail` is now true: the job page
(`/<region>/<lang>/job/<jobId>/<slug>`) embeds a schema.org JobPosting with the
full body. This also fills descriptions for the Phenom boards already crawled
(U.S. Bank, United, Lilly, Merck, Lowe's and others).

## Still open

- **The 14 unmeasured employers** (J&J, Wells Fargo, Verizon, Citi, Nordstrom,
  Dollar Tree, ...). Find their branded careers hosts through their own pages.
- **The 4 own-site employers.** Check each site's job-page format.
- **Registry gap.** `employers.canonical_domain` is missing for 37 of the top 40
  Workday employers. Discovery and ownership proof need it.
- **NLx ownership.** The 7 `.jobs` employers need proof (a link from the
  employer's site or two independent signals) before `BOARDS` can list them.

## 4. Browser Radar (2026-11-19)

A fourth source, for Workday itself: the owner's own browser reads Workday
boards at a person's pace through the ApplyPilot extension and sends what it
saw to `POST /api/jobs/browser-radar/listings`. The server maps the raw CXS
fields with the crawler's own `WorkdayProvider` and writes them through
`upsert_posting` / `touch_seen`, so the rows are the crawl's rows. It never
closes a posting, and a posting the browser saw stops counting as "not seen".

It covers a sliver, not the outage: one browser makes at most ~400 Workday
requests a day (about 8,000 listing rows), against the ~3M fetched a day
before 2026-11-14. Its plan sends the browser to the user's own For-you
Workday employers first, then (for contributors) the largest boards the
crawler has not reached for 12 hours. Contract, switches and ops:
[`docs/specs/browser-radar.md`](../specs/browser-radar.md). Writes are on by
default for contributor accounts (`BROWSER_RADAR_INGEST=dry_run` or `off` to
stop them).
