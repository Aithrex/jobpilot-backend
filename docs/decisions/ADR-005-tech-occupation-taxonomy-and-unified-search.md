# ADR-005 — Tech occupation taxonomy and one unified search

**Status:** Accepted (in build, behind a feature flag)
**Date:** 2026-09-01
**Extends:** ADR-001 (date confidence), ADR-002 (one fit score), ADR-003 (job
inventory), ADR-004 (universal employer coverage).
**Supersedes:** nothing. The three live-crawl feed tabs keep working.

---

## 1. The problem

ADR-003 and ADR-004 built an inventory and a way to keep filling it. They did
not decide **what the product is a search engine FOR**, and the answer showed
up in the data: 227,914 active postings of which roughly 17,000 are software
or technical work. Measured 2026-09-01, the largest employers in the inventory
are CVS Health, TJX, Domino's, Dollar Tree and Safeway.

Three specific faults follow.

**1. There is no notion of a technical occupation.** `fit_score.ROLE_FAMILIES`
has 31 families covering nursing, retail and hospitality, because it scores
every seeker. It cannot answer "is this a software job?", and 20% of the
inventory (45,783 postings) sits in `other`, which it explicitly documents as
"never used to exclude a posting".

**2. Occupation and employer are conflated.** There is no way to ask for
"software roles at any employer" and no way to ask for "technology companies
only", so both of the classic failures are reachable: a Retail Store Manager
at Apple reads as tech, and a Software Engineer at a hospital does not.

**3. There are three search paths, and none of them is the inventory.**
`/jobs/workday/search`, `/jobs/career-pages/search` and `/jobs/mnc/search`
each fan out over a catalog inside the user's request. `inventory_feed`
already answers two of them from `job_postings`, but the request contract is
still the crawl contract: no cursor, no facets, no salary filter that reaches
the code that reads it, no way to name a specialty.

## 2. Decision

### 2.1 Two classifications, never one

```
tech_taxonomy.classify(title, description, department, skills)
      -> what KIND OF WORK is this?        (no employer input, by signature)

employer_tech_classifier.classify_employer(company, posting_mix, endpoints)
      -> is this employer a technology company?   (no job title input)
```

The search layer composes them. `employer_scope=any` (the default) means
core-tech occupations at ANY employer; `employer_scope=tech_only` narrows by
the second classifier. `classify()` takes no employer parameter and
`tests/test_tech_taxonomy.py::test_the_employer_is_not_an_input` asserts by
introspection that it never will — this is the invariant the whole product
scope rests on.

### 2.2 The taxonomy is versioned, multi-label, and evidence-bearing

`TAXONOMY_VERSION = "tech-tax-1.0.0"`, stamped on every classification and
stored on every posting. 14 families (7 core, 7 adjacent) and 78 specialties
covering everything the product scope names.

Multi-label because single-label loses recall exactly where it matters: a
"Senior ML Platform Engineer" is honestly `ai_ml/ai_platform` AND
`cloud_platform/platform_engineering`. Cross-family pairs are DECLARED
(`_CROSS_LISTED`) rather than derived, because there is no syntactic rule that
separates "a second true label" from "a misreading of the same words" — the
same containment that makes ML Platform dual-family makes "Design Systems
Engineer" read as Cloud & Platform if you let it.

Every non-unknown verdict carries `classification_evidence`: the literal
strings that produced it and which field they came from. A verdict with no
evidence is a bug.

### 2.3 Unknown is a first-class answer

ADR-001's rule for dates applies to classification. An ambiguous title
("Technology Analyst", "Member of Technical Staff", "Solutions Consultant",
"Associate Consultant") with no corroborating body evidence returns
`tech_scope: unknown` — never `core`. Unknown rows stay retrievable and
rankable; they are simply not counted as core-tech.

The same rule holds downstream: unknown salary does not fail a salary floor,
unknown seniority does not fail a seniority filter, an unknown posting date
does not hide a posting. Each is an explicit opt-out instead.

### 2.4 Deterministic, never per-posting LLM

No model runs in the search hot path. Classification happens once at ingest,
where the description is in hand, and is read from `tech.*`. Measured:
0.135 ms per posting on a title, 6.6 ms with a full body — the latter only at
ingest, which is network-bound anyway.

An optional bounded natural-language parser MAY translate free text into
structured constraints later, but the hard constraints stay deterministic and
are echoed back in `applied_filters` so the user sees what actually ran.

### 2.5 Occupation codes come from O*NET-SOC 2019

`SPECIALTY_OCCUPATION_CODES` maps each specialty to the public-domain federal
codes, which is what makes DOL/BLS signals (wages, projections, LCA
disclosure) joinable to a posting later. Only clean correspondences are
mapped; a specialty with no genuine SOC equivalent carries none rather than
the nearest plausible one. A full O*NET title/software-skills bulk import
would widen the alias table; it does not change the shape and is not a
prerequisite.

### 2.6 One search, on the inventory, with a session cursor

`POST /api/jobs/tech-feed/search`, a SUBPATH of the already-registered
`/api/jobs` edge route — so no new Terraform route is needed and it cannot
404 in prod while working locally (invariant 6 in `CLAUDE.md`).

Retrieval order is fixed: hard eligibility and exclusions -> core tech scope
-> structured facets -> lexical -> `fit_score` -> freshness/source/preference.
Exclusions run FIRST and unconditionally; nothing downstream can readmit a row
an exclusion removed.

**Pagination is a recorded session, not a keyset.** One bounded retrieval per
SEARCH; the ordered `job_id` list, the facets and the inventory watermark go
into `tech_feed_searches` (TTL 1h) and the cursor is `{search_id, offset}`.
Pages after the first are a slice plus a `$in` fetch.

That is deliberate. Relevance ordering is computed in process from
`fit_score`, so there is no Mongo sort key to keyset on; and keysetting over
`posted_at` while the crawlers insert rows underneath produces exactly the
duplicates-and-omissions-across-pages failure the acceptance criteria forbid.
The cost is a bounded reachable depth (2,000 rows), which the response reports
as a warning rather than hiding.

### 2.7 One score, two numbers

`match_score` is `fit_score` and nothing else, via the same
`inventory_feed._score_rows` entry point the existing tabs use — not a
reimplementation. No parsed resume means `match_score: 0` and
`fit_score_version: null`, so the card renders "Not scored".

`rank_score` is a separate field carrying ordering preference (freshness,
direct-employer confidence, how many of the user's own filters a row
satisfies). It is presentation. No surface may display it as a match claim.

## 3. Rejected alternatives

**Extend `fit_score.ROLE_FAMILIES` instead of adding a taxonomy.** That
engine is single-label, is shared with every non-technical seeker, and has no
concept of scope, specialty, evidence or version. Adding 78 specialties to it
would change scoring for nurses.

**Vector / semantic retrieval.** Not built. `find_active_fresh` already
records the reason and it still holds: nothing in this repo benchmarks a
recall or latency win, and an unbenchmarked search index is a standing cost
against an unproven benefit. The lexical/faceted implementation IS the
baseline it would have to beat, on the golden query set, before it ships.

**An LLM classifier per posting.** ~228k postings re-crawled continuously. The
cost is real and the output is not reproducible, which makes a
user-visible classification unexplainable and a regression undetectable.

**Rewriting `role_family` in place.** Every existing index and read path uses
it. `tech.*` is stored ALONGSIDE, retrieval ORs the two during migration, and
the widening disappears on its own once the backfill completes.

**Entitlement-gating the feed.** Browsing the job market is not a paid feature
today. Making it one is a product decision, not an implementation detail.

## 4. Contracts this changes

| contract | before | after |
|---|---|---|
| `filter_suggester` anti-keywords | assigned to `custom_role_terms` (a POSITIVE field) | separate `exclude_title_terms`, applied as a hard drop |
| catalog company entries | `display_name` (Workday) vs `name` (career-pages, MNC) | **`display_name` canonical**, `name` kept as a legacy alias |
| `min_salary` | read by `inventory_feed`, never sent by any route | validated at the route, applied on all four feed paths, with `include_unknown_salary` |
| job identity | recomputed client-side from display strings, losers dropped | `dedupe_group_id` computed at ingest, losers kept as `source_variants` |

## 5. Acceptance

| target | measured |
|---|---|
| core-tech precision >= 95% | **100%** on the 112-row golden set; ~96–98% on a manual read of 45 random live core rows |
| core-tech recall >= 92% | **100%** on the golden set |
| non-tech leakage <= 2% | **0%** (0/28) |
| classifications carry version + evidence | asserted in `tests/test_tech_taxonomy.py` |
| no LLM per posting in search | structural — no model import on the path |
| no duplicates/omissions across pages | asserted in `tests/test_tech_feed_search.py::TestPagination` |

The golden set is stratified exactly as the product scope requires (clear
positives; non-tech at tech employers; tech at non-tech employers; ambiguous
titles; physical vs software/embedded; technical vs nontechnical
product/support/sales/consulting; AI/ML specialty edges) and every row states
why it exists.

**Honest caveat:** the golden set was authored alongside the rules, so it is a
regression gate, not an independent measurement. The independent check was a
manual read of random live classifications, which surfaced four real bugs the
golden set did not (bare "Design Engineer" reading as UX; seniority prefixes
changing the family; `<stack> Developer` titles unmatched; `specialist`
promoting a labelling role). A human-labelled sample drawn from production is
the next step and is not yet done.

## 6. Known gaps

* `tech.*` is not backfilled. Until it is, retrieval over-fetches ~4.4x and
  classification on the read path is title-only.
* `work_mode` is 96.1% unknown in the inventory. The remote filter is
  therefore weak, and the UI must say so rather than imply the market is
  onsite.
* `employment_type` is unnormalized (61+ raw values). It is a facet, not yet a
  reliable filter.
* `semiconductor_hardware` adjacent recall is low: most legacy
  `hardware_engineering` titles classify `unknown` rather than adjacent.
* 35 of 129 employer-name conflicts remain unadjudicable from the tenant
  alone and are flagged rather than resolved.
