# Source and provider priority matrix

Prioritised by **expected core-tech posting yield**, not by vendor count.
Measured against live `aspirely_db`, 2026-09-01.

---

## 1. What each vendor actually returns today

"core %" is the share of that vendor's active postings that
`tech_taxonomy.classify` calls core-tech.

| vendor | boards | active postings | core-tech | jobs/board | **core %** |
|---|---:|---:|---:|---:|---:|
| workday | 545 | 148,754 | 9,791 | 273 | 6.6% |
| lever | 92 | 6,896 | 1,780 | 75 | **25.8%** |
| oracle_orc | 145 | 26,182 | 1,486 | 181 | 5.7% |
| greenhouse | 505 | 8,798 | 1,429 | 17 | **16.2%** |
| amazon | – | 2,902 | 523 | – | 18.0% |
| ashby | 197 | 2,040 | 409 | 10 | **20.0%** |
| icims | 191 | 15,260 | 408 | 80 | 2.7% |
| rippling | 31 | 925 | 208 | 30 | **22.5%** |
| smartrecruiters | 59 | 9,118 | 198 | 155 | 2.2% |
| recruitee | 17 | 426 | 130 | 25 | **30.5%** |
| workable | 39 | 874 | 118 | 22 | 13.5% |
| eightfold | 2 | 156 | 73 | 78 | **46.8%** |
| bamboohr | 77 | 574 | 52 | 7 | 9.1% |
| radancy | – | 255 | 50 | – | 19.6% |
| jazzhr | 31 | 1,145 | 47 | 37 | 4.1% |
| ukg | 58 | 1,257 | 42 | 22 | 3.3% |
| phenom | – | 304 | 38 | – | 12.5% |
| jobvite | 10 | 341 | 25 | 34 | 7.3% |
| taleo | – | 211 | 21 | – | 10.0% |
| cornerstone | 18 | 451 | 16 | 25 | 3.5% |
| neogov | 1 | 851 | 8 | 851 | 0.9% |
| paylocity | 92 | 575 | 6 | 6 | 1.0% |
| **total** | **2,110** | **228,295** | **16,858** | **108** | **7.4%** |

### What this says

**The vendor mix is optimised for volume, and the product needs density.**
Workday is 65% of the inventory at 6.6% core-tech. Greenhouse, Lever, Ashby
and Rippling are 3.5% of the inventory at 16–26% core-tech. For a
tech-employment product a Greenhouse board is worth roughly **3x** a Workday
tenant per posting ingested, and a Lever board roughly **4x**.

That inverts the priority in `TASK-company-discovery-throughput.md`, which
ranked by raw postings-per-board. Both rankings are right for their question;
this one is right for this product.

## 2. Priority: boards to find next

| rank | action | why | expected core-tech |
|---|---|---|---|
| 1 | **Greenhouse board discovery** | 505 boards found; thousands exist. 16.2% core, single API host, slug-probeable. | +2,000 boards x ~2.8 core/board ~= **+5,600** |
| 2 | **Lever board discovery** | 92 boards at 25.8% core and 75 jobs/board — the densest at scale. | +500 boards ~= **+9,700** |
| 3 | **Ashby board discovery** | 197 boards, 20% core, venture-backed base. | +400 boards ~= **+800** |
| 4 | **Re-arm the 597 `unresolved` tech employers** | Includes Microsoft, Apple, Salesforce, Atlassian, Dell, VMware, Shopify, Unity, Synopsys, SAP, SpaceX, Zoox, Aurora. | high, unquantified |
| 5 | **Re-fingerprint migrated portals** | `PortalMovedError` already records the destination (Rivian -> `careers.rivian.com`); nothing acts on it. | moderate |
| 6 | **Fix the 35 unresolved employer-name conflicts** | 12,230 postings currently unfindable by employer name. | recall, not new rows |

## 3. Unsupported providers, with the blocker

From `docs/coverage/ats-vendor-coverage.md` sec. 6, encoded in
`employer_tech_classifier.UNSUPPORTED_PROVIDERS` so an employer on one of
these reports `unsupported_provider` rather than "we failed".

| vendor | blocker | build? | expected core-tech |
|---|---|---|---|
| **USAJOBS** | needs a free API key. **No technical blocker.** | **yes — highest value/effort** | ~30k federal postings; federal IT/software is a large, low-competition segment |
| **SAP SuccessFactors** | white-labelled onto employer domains; no host to enumerate — reachable by fingerprinting | yes | large enterprise; moderate core % |
| **ADP Workforce Now** | boards behind `cid`-parameterised shells | yes | very large SMB base; low core % |
| **JazzHR** | already built (31 boards); needs more tenants | expand | 4.1% core — low priority for THIS product |
| **Dayforce / Ceridian** | Common Crawl index 504s on the domain | yes | mid-market; low core % |
| **Zoho Recruit** | 61 tenants found, provider not built | moderate | low core % |
| **Paycom** | no tenants surfaced in the index sample | moderate | SMB; low core % |
| **BreezyHR** | guessed slugs wrong; needs real tenants | moderate | SMB |
| **Teamtailor** | no tenants surfaced; mostly EU | low | out of the US-first default |
| **PeopleAdmin / Interfolio / PageUp** | per-institution hosts, higher-ed | low | higher-ed IT |

**Recommendation.** For a software/technical product, USAJOBS and
SuccessFactors are the only two unsupported providers worth building before
the Greenhouse/Lever/Ashby discovery work in sec. 2 — and both rank below it.
The SMB payroll vendors (Paycom, Dayforce, BreezyHR, Zoho, ADP) are correctly
prioritised for a general job board and are **the wrong next investment
here**: measured core-tech density for the payroll-bundled vendors already
built is 1.0–4.1%.

## 4. Constraints that do not move

* Per-vendor pacing stays as declared. iCIMS is `pacing_key="icims.com"`,
  `max_concurrency=1`, `min_interval=2.5s` because the wall counts per SOURCE
  IP across all tenants. Throughput is never bought by removing pacing.
* No CAPTCHA bypass, login bypass, proxy rotation, or fingerprint evasion.
* robots.txt, SSRF allowlists and source terms are respected.
* Direct employer and official ATS data are preferred over aggregators. The
  inventory is currently 100% `cost_class: free` and that is worth keeping.
* A verdict is only as wide as the instrument that produced it. Any "vendor is
  broken" claim needs `scripts/live_provider_check.py`, not a fixture.
