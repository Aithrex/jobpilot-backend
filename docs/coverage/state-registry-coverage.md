# US state business registries — what is actually harvestable

> **HISTORICAL, 2026-09-02.** Measured 2026-08-18 and unchanged since. State
> registries are an EMPLOYER-DISCOVERY source, not a job source, and nothing
> in the 2026-09-02 coverage work touched them — so the findings below still
> stand, but they have not been re-measured and the dates in them are the
> dates that matter.

**Systematically re-measured 2026-08-18.** Method and raw results in §2.

## 1. The short answer

The goal this was meant to serve is "cover every company in every state". After
measuring rather than assuming, the honest position is:

> **State business registries are a weak instrument for that goal, and they are
> nearly exhausted at 8 jurisdictions.** The stronger instrument is vendor-first
> tenant discovery, which reaches the same employers directly.

Both halves of that need justifying, so both are below.

## 2. How many states actually publish a registry — measured

Rather than checking states one at a time and stopping when it got tedious, the
Socrata **cross-domain catalog API** (`api.us.socrata.com/api/catalog/v1`) was
queried across 35 state and major-city open-data portals for datasets whose
titles match business-registry language (`business entities`, `corporations
registry`, `registered businesses`, `business master`, `business filing`,
`entity`).

Every state portal that publishes a corporate registry as open data:

| State | Domain | Dataset |
|---|---|---|
| NY | `data.ny.gov` | `n9v6-gdp6` Active Corporations (+ 5 related filing tables) |
| CO | `data.colorado.gov` | `4ykn-tg5h` Business Entities in Colorado |
| PA | `data.pa.gov` | `xvd7-5r2c` Registered Businesses in PA |
| OR | `data.oregon.gov` | `tckn-sxa6` Active Businesses — ALL |
| CT | `data.ct.gov` | `n7gp-d28j` Business Registry — Business Master |

**Five.** Those are exactly the five `state_registries/states.py` already
implements. The sweep found nothing new — which is the useful result: the
Socrata channel is *exhausted*, not under-exploited.

Three more jurisdictions are harvestable outside Socrata and are already
implemented: **FL** (Sunbiz fixed-width bulk), **ID** and **ND** (PCC bizfile
JSON). Total: **8 of 51**.

Also surfaced, and deliberately not used: city-level registries such as
`data.sfgov.org` *Registered Business Locations — San Francisco*. A city is not
a state, and mixing the two would make the "states with a working adapter"
metric mean nothing.

### What the other 43 do

Unchanged from ADR-004 §2, now confirmed by the negative result above:

* **search-only** — an HTML form that requires a name to search *for*. There is
  no query that enumerates the registry. This is the most common case and it is
  a genuine wall, not a missing feature.
* **paid** — sold commercially or by written request (Delaware, Texas SOSDirect).
* **bulk, but not at a stable published URL** — several states (TX Comptroller
  franchise-tax permit holders, OH SoS business filings, WA, MN, MI) do publish
  downloadable files, but behind pages that move. Four plausible URLs were
  probed on 2026-08-18 and all four 404'd, 403'd or refused the connection.
  These are *reachable with per-state research*, and that is the honest
  characterisation: not blocked, just not free.

## 3. Why this matters less than it appears

Two structural reasons, both of which apply regardless of how many states are
added:

**A registry lists legal entities, not employers.** The overwhelming majority
are single-member LLCs, holding companies, rental-property entities and
dissolved shells that will never post a job. Delaware alone has more registered
entities than residents by an order of magnitude. `companies` growing to
millions of rows is not coverage of anything a job seeker can apply to.

**A registry gives a NAME, not a DOMAIN.** The discovery pipeline works by
fetching a company's own site and fingerprinting the ATS link on its careers
page. Without a domain there is nothing to fetch. Resolving millions of company
names to domains is itself an unsolved, expensive problem, and doing it badly
produces confident nonsense.

So the path `state registry → company → domain → careers page → ATS endpoint →
jobs` has a hard break at the second arrow, for most of the rows, most of the
time.

## 4. What to do instead — and what was built

`shared/python/services/tenant_discovery.py` inverts the direction. Many ATS
vendors give every customer their own hostname, and those hostnames are public
record in Common Crawl's URL index and in Certificate Transparency. So instead
of

```
40M entity names  →  (no domain)  →  ✗
```

it runs

```
*.bamboohr.com in the public archives  →  96 real employer boards  →  validate  →  jobs
```

with **no company list required at all**. One live run against
`*.bamboohr.com` returned 96 employer tenants — `10web`, `24slg`,
`120water`, `2ndfamilyinc`, `321theagency` — small US firms of exactly the kind
that never appear on an aggregator and that no purchasable company list covers
well. See `docs/coverage/ats-vendor-coverage.md` §7.

That is the same target population the state-registry work was aiming at,
reached in two requests per vendor instead of forty million.

**The two are complementary, not competing.** Registries remain the only
unbiased source of company *identity* (they are not skewed toward firms we
already know), and they feed the priority queue via the federal employment
signals. They are simply not the fastest route to a crawlable job board, and
they should stop being treated as if they were.

## 5. Federal proxy — what the other 43 states actually get

Per ADR-004 §4.5, jurisdictions without a harvestable registry are covered by
`FederalProxyAdapter`, which enumerates *operating employers* via national
signals — DOL H-1B LCA filings, SEC EDGAR, SAM.gov — rather than pretending a
name-search form can enumerate a registry.

**This should be read carefully when interpreting goal G5.** `coverage_report.py`
reports "51 states with a working adapter", and that is true in the sense that
every jurisdiction has an adapter that returns rows. But 43 of those adapters
are the *same* federal proxy over the *same* national datasets, filtered by
state. G5 at 51/51 therefore means "no jurisdiction is unreachable", **not**
"51 independent state registries are being harvested". The distinction is worth
keeping visible, because the second reading would overstate coverage by a
factor of six.

## 6. Prioritised backlog, if state coverage is pursued further

Ordered by employers-per-unit-effort, not by state size:

1. **TX Comptroller active franchise-tax permit holders** — a permit means the
   entity is *operating and taxable*, which is a far better employer filter than
   "registered". Bulk CSV exists; the current URL needs locating.
2. **OH Secretary of State business filings** — free bulk download, historically
   a single ZIP.
3. **WA, MN, MI, TN, KY, WY** — each publishes bulk data; each needs its current
   URL and column map confirmed once.
4. **IRS Exempt Organizations Business Master File** — ~1.8M nonprofits
   nationally, with names and addresses. Hospitals, universities and school
   districts are heavy, continuous hirers and are under-represented in every
   commercial company list.
5. **SAM.gov entity extract** — federal contractors, and unusually it often
   carries a **URL**, which is the missing field everywhere else.

Note that (4) and (5) are national, not per-state, and both would deliver more
crawlable employers than the remaining 43 state registries combined.
