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
