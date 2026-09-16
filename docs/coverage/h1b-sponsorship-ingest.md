# H-1B sponsorship signals — what we ingested, and what it does and does not say

`sponsorship_signals` held **0 documents** until 2026-09-15; the feature had
never been run. It now holds **43,053**, written by
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
| **employers after rollup** | **42,420** — 522 of them hold more than one entity |
| documents written | 43,053 (42,420 primary + 633 alias lookup rows) |
| employers with denials and NO approvals | 1,224 |

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
| `curated_alias` | 97 | an exact-name whitelist in [`scripts/data/sponsorship_rollups.py`](../../scripts/data/sponsorship_rollups.py), with every refusal recorded next to the merge |
| `tax_id_and_brand` | 425 | identical Tax-ID last-4 **and** first name token **and** petitioner state |
| `employer_registry` | 1 (Intuit + Credit Karma) | both names resolve to ONE `employers` row with `identity_confidence >= 0.80` |
| `single_entity` | 41,899 | not merged with anything |

**No merge happens on a name resemblance**, which is
`employer_registry.merge_decision`'s rule and it earns its keep here: the file
contains `AMAZON USA ENTERPRISE CORP D B A GRANITE AND MARBLE DESIGN IMPORT EXP`
(Delray Beach, FL — a stone importer), `META SOFT INC` (Roswell, GA),
`ORACLE ROBOTICS CORP` (Chicago), `KLA ELEMENTARY LLC` (a Miami school) and
`DELL PAINTING CORP` (North Arlington, NJ). 63 merges were explicitly refused
and **2,636 near-miss candidates** were left unmerged and written to
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
| 1 – 12 | `low` |
| 13 – 203 | `moderate` |
| 204 + | `strong` |

Denials temper without erasing: 210 approvals + 200 denials scores 56 and bands
`moderate` rather than `strong`. The **1,224 denial-only employers** band
`none` with score 0 and `guest_service._sponsorship_for_card` drops them — "we
have a row and it shows nothing" is an answer, and it is not the same as "we
have no data", which is still `None`.

Spot-checked through `sponsorship_service.bulk_signals` after the write: Amazon
13,069/strong · Google 3,183/strong · Apple 3,885/strong · Microsoft
3,688/strong · TCS 4,450/strong · Infosys 4,152 · Cognizant 3,530 · Deloitte
1,869 · EY 1,359 · PwC 830 · JPMorgan Chase 2,112 · Capital One 1,077 · Walmart
1,669 · NVIDIA 1,454 · Qualcomm 955 · LinkedIn 542 · Collins Aerospace
168/moderate · Siemens Energy 49/moderate (correctly NOT folded into Siemens) ·
Lucid Software 8/low (correctly NOT Lucid Motors) · HCL Global Systems
120/moderate (correctly NOT HCLTech) · an unknown company → `None`.

## 4. THE WINDOW: this is FY2026 only, and the service says five years

**The data covers one fiscal year. `sponsorship_service.DATA_WINDOW_YEARS` is
`5` and every string it renders says so.** With `sponsorship_signals`
populated, these live consumers start rendering that copy:
`GET /api/jobs/sponsorship` and `/sponsorship/bulk`, `job_service` search
results, `guest_service` match cards and the notification digests.

What the ingest does about it, inside what it owns:

* the fiscal years are **read from the file**, never assumed —
  `data_window.fiscal_years: [2026]`, `data_window.years: 1`,
  `data_window.label: "USCIS fiscal year 2026 (single fiscal year)"`;
* every document carries **`data_window.service_claim_mismatch: true`**, so the
  discrepancy is queryable rather than folklore;
* the run prints a loud warning on stderr, and a second one saying that
  `SOURCE_LABEL` still names the DOL dataset we do not have;
* the contract keys keep the names the service reads (`approvals_5y`,
  `denials_5y`) because renaming them would break the reader — the honest
  window lives beside them and is never inferred from the key.

What the ingest **cannot** do: `shared/python/services/sponsorship_service.py`
is owned elsewhere and was not modified. Three strings are currently
over-claiming for these rows:

| what renders | what it says | what is true |
|---|---|---|
| `_build_headline` | "Sponsored N people … in the last **5 years**" | one fiscal year (FY2026) |
| `_build_summary` / `DISCLAIMER` | "…from public **DOL and USCIS** disclosure data over the last 5 fiscal years" | USCIS only |
| `_build_summary` vintage | "data through `computed_at`" | `computed_at` is when the ingest RAN, not the data's period end |

**Required follow-up (owner: whoever owns `sponsorship_service`).** Make the
three presentation constants per-document, falling back to today's constants so
nothing else changes:

```python
# in _score_doc, replacing the module constants at the point of USE:
window = doc.get("data_window") or {}
years  = int(window.get("years") or DATA_WINDOW_YEARS)
label  = str(doc.get("source_label") or SOURCE_LABEL)
vintage = window.get("label") or _iso(doc.get("last_filed_at")) or _iso(doc.get("computed_at"))
# then pass years/label/vintage into _build_headline/_build_summary instead of
# the module-level DATA_WINDOW_YEARS / SOURCE_LABEL, and render
# "in the last {years} fiscal year(s)" / "Source: {label} ({vintage})".
```

Until that lands, the numbers are right and the window sentence is not. The
alternative — leaving the collection empty — keeps the sentence unrendered by
returning `None` everywhere, which is why this is a REQUIRED follow-up and not
a nice-to-have.

Adding a real 5-year window is otherwise just data: pass five Data Hub exports,
`--uscis FY2022.csv --uscis FY2023.csv …`. `data_window.years` becomes 5 and
`service_claim_mismatch` becomes `false` with no code change.

## 5. Indexes

Measured on the live collection (43,053 documents).

| query | call site | plan | examined | time |
|---|---|---|---|---|
| `{employer_norm: "amazon"}` | `sponsorship_service.get_signal` | `EXPRESS_IXSCAN` on `employer_norm_unique` | 1 key / 1 doc | 0 ms |
| `{employer_norm: {$in: [...203]}}` | `sponsorship_service.bulk_signals` | `IXSCAN` + `FETCH` | 7 keys / 3 docs | 0 ms |
| `{rollup.is_alias: false, approvals_5y: {$gte: 5}}` | `sponsorship_gap_report` (offline) | `COLLSCAN` | 43,053 docs | 69 ms |

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
43,053 documents to 25 ms / 7,226 (measured, then dropped). It saves 44 ms on a
script a human runs by hand, and costs 43,053 index entries plus a write on
every row of every ingest. Not worth it.

## 6. Re-running and rolling back

```bash
python scripts/ingest_sponsorship.py --uscis "Employer Information.xlsx"          # dry run (default)
python scripts/ingest_sponsorship.py --uscis "Employer Information.xlsx" --apply --prune
python scripts/sponsorship_gap_report.py --top 1000                               # read-only
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
