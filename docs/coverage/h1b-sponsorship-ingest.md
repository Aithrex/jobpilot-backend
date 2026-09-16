# H-1B sponsorship signals — what we ingested, and what it does and does not say

`sponsorship_signals` held **0 documents** until 2026-09-15; the feature had
never been run. It now holds **43,566**, written by
[`scripts/ingest_sponsorship.py`](../../scripts/ingest_sponsorship.py) from the
USCIS H-1B Employer Data Hub export `Employer Information.xlsx`
(sha256 `a2ce2edfa458…`, 49,495 rows, FY2026).

This is a **TRUST** feature. Everything below is about what we can and cannot
honestly say with it.

---

## 1. What was ingested

| | |
|---|---|
| source | USCIS H-1B Employer Data Hub, **fiscal year 2026 only** |
| rows read | 49,495 (49,486 used) |
| rows dropped | 7 with a blank petitioner name, 2 whose name normalizes to empty |
| approvals | 270,414 |
| denials | 8,226 |
| distinct legal entities | 42,968 (one per exact normalized petitioner name) |
| **employers after rollup** | **42,356** — 563 of them hold more than one entity |
| documents written | 43,566 (42,356 primary + 752 alias lookup rows + 458 census-absent) |
| employers with denials and NO approvals | 1,223 |
| employers we carry postings for that filed NOTHING | 458 (12,753 active postings) |

There is **no DOL LCA file**. `lca_filings_5y`, `median_wage`, `top_titles` and
`last_filed_at` are therefore **absent from every document** — not zero.
`sponsorship_service` defaults each of them on read (`0`, `None`, `[]`,
`None`), so a row scores on approvals alone, which is the true state of our
knowledge. `median_wage: 0` would have been a claim about wages.

## 2. Entity rollup — three bases, and a whitelist rather than a prefix

USCIS files per legal entity. Amazon appears as nine of them; Deloitte as
seven. A candidate asking "does Amazon sponsor?" means the employer.

| basis | groups | what it proves |
|---|---:|---|
| `curated_alias` | 142 | an exact-name whitelist in [`scripts/data/sponsorship_rollups.py`](../../scripts/data/sponsorship_rollups.py), with every refusal recorded next to the merge |
| `tax_id_and_brand` | 422 | identical Tax-ID last-4 **and** first name token **and** petitioner state |
| `employer_registry` | 1 (Intuit + Credit Karma) | both names resolve to ONE `employers` row with `identity_confidence >= 0.80` |
| `single_entity` | 41,794 | not merged with anything |

**No merge happens on a name resemblance**, which is
`employer_registry.merge_decision`'s rule and it earns its keep here: the file
contains `AMAZON USA ENTERPRISE CORP D B A GRANITE AND MARBLE DESIGN IMPORT EXP`
(Delray Beach, FL — a stone importer), `META SOFT INC` (Roswell, GA),
`ORACLE ROBOTICS CORP` (Chicago), `KLA ELEMENTARY LLC` (a Miami school),
`DELL PAINTING CORP` (North Arlington, NJ), `GSK SOLUTIONS INC` (an IT staffing
firm, not GlaxoSmithKline) and `CACIQUE FOODS` (a cheese manufacturer, not
CACI). 82 merges were explicitly refused — nine of them recorded permanently in
`REFUSED_LOOKALIKES` — and **2,639 near-miss candidates** were left unmerged and
written to
[`sponsorship-rollup-candidates.json`](sponsorship-rollup-candidates.json) for
review — the same shape as `employer_merge_reviews`.

The Tax-ID basis is worth its own note: USCIS publishes only the **last four
digits** of the EIN (9,879 distinct values across 44k employers), so that field
is never probative alone. With the first name token and the state alongside it,
what it actually finds is one entity spelled two ways —
`... D B A APTIV` vs `... DBA APTIV`, `RIVIAN AND VOLKSWAGEN GROUP TECHNOLOGIES`
vs `RIVIAN AND VW GROUP TECHNOLOGY` — which is exactly the case a name key
cannot merge and a human should not have to.

Two product rules decided the curated memberships, and both are recorded in the
table:

1. **One employer = one careers board.** LinkedIn is not Microsoft, Waymo is
   not Google, GitHub is not Microsoft, ByteDance is not TikTok. A candidate
   applies to a board.
2. **A corporate parent is not an employer.** Siemens Energy and Siemens
   Healthineers are separately listed companies and are excluded from Siemens;
   Tata Technologies and Tata Elxsi are excluded from TCS.

### Alias documents

A posting says "Amazon Web Services", "AWS", "TCS" or "Ernst & Young"; USCIS
says `AMAZON WEB SERVICES INC` and `ERNST AND YOUNG U S LLP`. Each employer
therefore also gets one **alias document** per alternative lookup key, carrying
the same numbers and **the parent's display name** — so the rendered sentence
is "Amazon had 13,069 approvals", never "AWS had 13,069 approvals". 633 of the
43,053 documents are aliases.

## 3. What a USCIS-only row bands as

With no DOL `last_filed_at`, `sponsorship_service._recency_multiplier` returns
**0.85** (the row is inside the window by construction, so it is not penalised
for a missing date), and the volume term is `log10(1+approvals) /
log10(2001)`. Measured against the live service:

| approvals (no denials) | band |
|---|---|
| 0 | `none` (score 0) |
| 1 – 7 | `low` |
| 8 – 65 | `moderate` |
| 66 + | `strong` |

**Those thresholds are per YEAR, and that is a fix.** `_VOLUME_SATURATION` was
calibrated against a five-fiscal-year window. Held fixed, a one-year file put
"strong" at **204 approvals in a single year** — the five-year equivalent of
~1,000 — so the band silently measured a different thing than it was calibrated
to mean, and an employer's band would move when we imported more history rather
than when their behaviour changed. Saturation is now
`_VOLUME_SATURATION_PER_YEAR (400) x window_years`: a five-year file scores
**exactly as before** (13 / 204, asserted in `tests/test_sponsorship_service.py`)
and a one-year file is judged at the same rate.

Denials temper without erasing: 70 approvals + 70 denials drops out of `strong`.
The **1,223 denial-only employers** band `none` with score 0 and
`guest_service._sponsorship_for_card` drops them — "we have a row and it shows
nothing" is an answer, and it is not the same as "we have no data".

Spot-checked through `sponsorship_service.bulk_signals` after the write (38
names, all correct): Amazon 13,069/strong · Google 3,183 · Apple 3,885 ·
Microsoft 3,688 · TCS 4,450 · Infosys 4,152 · Cognizant 3,530 · Deloitte 1,869 ·
EY 1,359 (**and "Ernst & Young" now resolves to it**) · PwC 830 · JPMorgan Chase
2,112 · Capital One 1,077 · Walmart 1,669 · NVIDIA 1,454 · Qualcomm 955 ·
LinkedIn 542 · Dell Technologies 425 · AMD 380 · **Fiserv 232** · U.S. Bank 372 ·
Barclays 572 · Johnson & Johnson 44/moderate · Collins Aerospace 168 · RTX
4/low · Booz Allen 5/low · Leidos 31/moderate · Siemens Energy 49 (correctly NOT
folded into Siemens) · Lucid Software 8/low (correctly NOT Lucid Motors) · HCL
Global Systems 120 (correctly NOT HCLTech) · **Northrop Grumman, SpaceX and
Anduril → `census_absent`** · an unknown company → `None` with
`lookup_status(...)["reason"] == "no_row"`.

> **RTX at 4, Booz Allen at 5, Leidos at 31, Northrop Grumman at 0.** Those four
> hold 1,260, 300+, 400+ and 833 active postings in our inventory. This is the
> user's complaint about the feed, quantified from the government's own census:
> the employers crowding the feed are the ones who sponsor nobody.

## 4. The window is FY2026, and the badge now says FY2026

The data covers ONE fiscal year of USCIS with no DOL component.
`sponsorship_service` used to describe every row with module constants —
`DATA_WINDOW_YEARS = 5`, a `SOURCE_LABEL` naming both datasets, and a
`data_vintage` that was the moment our ingest ran. Live consumers
(`GET /api/jobs/sponsorship`, `/sponsorship/bulk`, `job_service` search,
`guest_service` cards, the digests) rendered that copy verbatim, so the product
claimed five years of DOL+USCIS evidence while holding one year of USCIS.

**Both halves are now fixed, and the provenance comes from the row:**

| field | before | now |
|---|---|---|
| headline | "…in the last **5 years**" | "…in **FY2026**" |
| source | "DOL OFLC LCA … + USCIS …" | "USCIS H-1B Employer Data Hub" |
| disclaimer | "public **DOL and USCIS** … last 5 fiscal years" | built from the row's own source and period |
| `data_vintage` | `computed_at` (when we imported) | `"FY2026"` (what the data covers) |
| when we imported | — | `data_imported_at` |

`DATA_WINDOW_YEARS` / `SOURCE_LABEL` survive only as the fallback for a legacy
row carrying neither, which `tests/test_sponsorship_service.py` pins. The
measured `data_window` is also passed through to `fit_score`, whose
`_employer_sponsorship` states the measured window beside any advertised one —
it could not do that before, because the signal never carried the field.

Adding a real five-year window is now just data: pass five Data Hub exports,
`--uscis FY2022.csv --uscis FY2023.csv …`. `data_window.years` becomes 5, the
copy says "FY2022-FY2026", and the band thresholds return to 13 / 204 on their
own.

## 4b. "They filed nothing" is an answer; "we never looked" is not

`get_signal` returned `None` for both, and a user cannot tell those apart. The
USCIS Data Hub is a **census** — it lists every employer that filed at least one
petition — so absence from it is a measurement.

`--census-absent` writes an explicit row for every employer **we carry active
postings for** that provably filed nothing: 458 employers, 12,753 active
postings, 2,079 of them core-tech. The proof standard is the same one the rollup
uses in the other direction: *every* distinctive token of the employer's name
must be absent from *every* petitioner name in the file.

* **Northrop Grumman** — 833 active postings, 408 core-tech, no petitioner name
  in 42,968 contains "northrop" or "grumman". Row written, band `none`,
  headline "Filed no H-1B petitions in FY2026". Same for SpaceX (140/57) and
  Anduril (285/192).
* **Fiserv** — 201 postings, and "fiserv" DOES appear in the census (`FISERV
  SOLUTIONS LLC`, 232 approvals). No zero is written; it was a curation task and
  is now a curated alias.
* **TD Bank, JLL, PACS** — every token is too common for absence to prove
  anything. No row, no claim.
* **Jobgether (4,478 postings), `uscareers-oslrs`** — an aggregator and an
  iCIMS tenant slug. Not employers, so no employer-level claim is made about
  them (`rollups.is_not_an_employer`).

The 1,416 we can resolve neither way are written to
[`sponsorship-unmatched-employers.json`](sponsorship-unmatched-employers.json)
as a curation queue, with the nine refusals a human already made
(`REFUSED_LOOKALIKES`: GSK Solutions is not GlaxoSmithKline, ASM America is not
ASM Global, Latham Inc makes swimming pools). `lookup_status(name)` reports
which of the three answers a lookup got.

## 5. Indexes

Measured on the live collection (43,566 documents).

| query | call site | plan | examined | time |
|---|---|---|---|---|
| `{employer_norm: "amazon"}` | `sponsorship_service.get_signal` | `EXPRESS_IXSCAN` on `employer_norm_unique` | 1 key / 1 doc | 0 ms |
| `{employer_norm: {$in: [...203]}}` | `sponsorship_service.bulk_signals` | `IXSCAN` + `FETCH` | 7 keys / 3 docs | 0 ms |
| `{rollup.is_alias: false, approvals_5y: {$gte: 5}}` | `sponsorship_gap_report` (offline) | `COLLSCAN` | 43,566 docs | 69 ms |

**The only index this collection needs already exists** and is already declared
in `scripts/migrate_indexes.py`; `ingest_sponsorship.ensure_index()` re-asserts
it idempotently on every `--apply`:

```js
db.sponsorship_signals.createIndex({ employer_norm: 1 },
                                   { unique: true, name: "employer_norm_unique", background: true })
```

`unique` is load-bearing twice over: it is what makes the ingest's keyed upsert
idempotent, and it is what guarantees one employer holds exactly one row.

**Recommended AGAINST:** an index for the gap report's own scan. A
`{rollup.is_alias: 1, approvals_5y: -1}` index takes that query from 69 ms /
43,561 documents to 25 ms / 7,226 (measured, then dropped). It saves 44 ms on a
script a human runs by hand, and costs 43,566 index entries plus a write on
every row of every ingest. Not worth it.

## 6. Re-running and rolling back

```bash
python3 scripts/ingest_sponsorship.py --uscis "Employer Information.xlsx"         # dry run (default)
python3 scripts/ingest_sponsorship.py --uscis "Employer Information.xlsx" \
    --apply --prune --census-absent \
    --candidates docs/coverage/sponsorship-rollup-candidates.json \
    --unmatched  docs/coverage/sponsorship-unmatched-employers.json
python3 scripts/sponsorship_gap_report.py --top 1000                              # read-only
```

`--apply` is required to write; `--dry-run` is the default. Writes are keyed
`ReplaceOne(upsert=True)` on `employer_norm`, so a re-run is idempotent and an
interrupted run is resumed by running it again — there is no partial state to
clean up. `--prune` deletes documents left by an EARLIER run of this script
(matched on `ingest_run_id`) and refuses to run alongside `--limit`, which
would delete employers a partial pass never looked at.

Rollback is `db.sponsorship_signals.deleteMany({})` — nothing else reads or
writes the collection, and every consumer already treats an empty collection as
"no data" and returns `None`.

## 7. Counting coverage: canonical id, then board, then (last) a name

`scripts/sponsorship_gap_report.py` counts an employer's inventory in the
feed's own precedence — `canonical_employer_id`, then `(ats, tenant)`, then a
normalized `employer_name` — and records which one answered in
`posting_count_basis`.

This is a correctness requirement, not a preference. `employer_name` carries
whatever the board printed: a **Boeing** posting says
`employer_name: "M.J. WILLIAMS COMPANY"`, and JPMorgan's Oracle instance
(`oracle-orc:jpmc`) stamps its postings `CHASE FAMILY LIMITED PARTNERSHIP NO. 5`
and `NUTMEG CONTAINER CORPORATION` — all three carry
`canonical_employer_id: emp_31ec06b38f21` and are counted as JPMorgan. Measured
on the name path alone, Boeing read **0** postings and is actually **401 active
/ 199 core-tech**; ServiceNow read 100 and GDIT 335. Those three were false
"gaps". IBM, Apple, Google and Meta stay at zero on every path, so those are
real.

### Three buckets exist because a fetch bug is not a discovery gap

Eightfold's provider declared `page_size=20` against an API that returns 10 rows
and ignores `num`, so every tenant stopped after page one — Microsoft read 29
postings instead of 1,178, Qualcomm 4 instead of 741, Lockheed 19 instead of
1,500+. A gap list that cannot express "we have this board and read part of it"
files all of those as discovery work that could never have fixed them. So the
report compares what we hold against what the board itself last published
(`career_sources.job_count_last` / `company_ats_endpoints.job_count_last` — our
own crawl's reading, no new request):

| bucket | meaning | what fixes it |
|---|---|---|
| `covered` | we hold what the board publishes | nothing |
| `covered_under_read` | board published >= 2x what we hold (and >= 25) | re-read the endpoint |
| `board_publishes_jobs_we_hold_none` | proven board, it published jobs, we hold none | re-read the endpoint |
| `validated_but_zero_postings` | proven board, it published nothing either | believe it, or check the endpoint address |
| `source_unvalidated` / `no_career_source` / `no_employer_row` | actual discovery work | `verify_career_sources.py`, then discovery |

Splitting them moved `validated_but_zero_postings` from 49 employers to **13**:
45 were under-read and 32 had a board publishing jobs we hold none of.

### Approvals are demand; `conversion` is how much of it we could show

The client-site staffing cohort — Infosys, TCS, Cognizant, Wipro, HCLTech,
Capgemini, LTIMindtree, Tech Mahindra, Mphasis — holds **14,421 approvals**,
more than Apple, Microsoft and Google combined, and it is the largest block in
the file. It does not convert: those firms hire against client requisitions that
never reach a careers site. **LTIMindtree is the disproof** — its board is
reachable, owned and fully readable, and it publishes 54 jobs, none in the US,
against 1,289 approvals.

So the rank is `approvals x software_likelihood x conversion`, where
`conversion` is:

* `board_published_last / approvals` when our crawl has actually read the
  employer's board (evidence beats any prior, and needs no new request);
* `0.25` for the curated `CLIENT_SITE_STAFFING` cohort when we have never read
  one;
* `1.0` otherwise.

`demand_score` keeps the unadjusted `approvals x software_likelihood` on every
row, so the discount is visible and reversible rather than baked into the only
number. The effect on the queue: Apple, Google, Meta, Tesla and IBM take the top
five, and Infosys/TCS/Cognizant move to 8/10/11 — still high, because 4,000
approvals is still 4,000 approvals.
