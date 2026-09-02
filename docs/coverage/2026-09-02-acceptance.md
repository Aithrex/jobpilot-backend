# Acceptance criteria, and where the evidence is

Every row names the artefact that proves it. Where a criterion is only
partly met, it says so and says what is missing — a checklist that only
contains ticks is not a checklist.

Regenerate the live numbers with:

```bash
PYTHONPATH=shared/python python scripts/employer_coverage_report.py --markdown
```

---

## Identity and ownership

| criterion | evidence | status |
|---|---|---|
| Zero known wrong-owner fixtures reach `validated` | `tests/test_career_source_ownership.py::TestNoWrongOwnerReachesValidated` — 9 fixtures (Forward, Beyond, Public, CTS, Super, Archer, CoStar, Alphabet/BMW, Capital One), each asserted twice: once with a responding endpoint plus a plausible slug, once with a provider naming a different owner | **met** |
| Every validated endpoint has auditable ownership and content evidence | `career_sources.ownership_evidence[]` / `content_validation_evidence[]`; `career_source_service.adjudicate_ownership` is the only path to `validated`, and `build_source` DERIVES the state rather than accepting one | **met** |
| Never merge employers on normalized name alone | `employer_registry.merge_decision` returns `QUARANTINE` for a name-only match; `tests/test_employer_registry.py::TestMergeDecision` | **met** |
| Repair the documented identity conflicts | `employer_registry.IDENTITY_OVERRIDES` covers Stripe, Notion, Ramp, Capital One, Intuit, JPMorgan, Boeing, Roche, AT&T, Abbott, NVIDIA, plus Salesforce/Slack and Alphabet/BMW | **met** |
| Probing is dry-run by default | `backfill_employer_registry.py`, `verify_career_sources.py`, `rearm_uncovered_employers.py` all require `--apply`; `employer_coverage_report.py` cannot write at all | **met** |
| robots.txt, SSRF, size limits, redirects, pacing, timeouts preserved | `career_source_ownership.Fetcher` — SSRF on every hop INCLUDING each redirect, robots per origin, 1.5 MB cap, 12 s timeout, per-host pace. Two guards that had never fired were fixed (see below) | **met, and two were broken** |
| Every strategic Tier-A employer accounted for | `employer_coverage_report.tier_a_rollcall` builds the table from the REGISTRY list, so an employer missing from the database appears as `not_in_registry` rather than vanishing; `tests/test_employer_coverage_report.py::test_every_tier_a_employer_is_accounted_for` | **met** |
| Provider failures cannot silently remove employers from the denominator | `career_source_service.coverage_by_employer` returns a row for EVERY requested employer, including those with no source; `tests/test_career_source_ownership.py::test_a_provider_failure_cannot_remove_an_employer_from_the_denominator` and `tests/test_employer_coverage_report.py` | **met** |
| Coverage documents generated from code | `scripts/employer_coverage_report.py --markdown` → `docs/coverage/tech-employer-coverage.md`, stamped with the git revision and inventory watermark | **met** |
| **Validated coverage 43% → at least 70%** | see "The coverage number" below | **NOT met in this session** |

## Taxonomy and fit scoring

| criterion | evidence | status |
|---|---|---|
| `tech_taxonomy` is canonical for ingestion, résumé parsing, search and fit | `fit_score._score_role` scores on `tech_taxonomy.role_alignment`; `build_resume_profile` carries `tech` from `classify_resume`; `job_taxonomy` prefers the value stored at ingest | **met** |
| Remove or adapt the duplicate `ROLE_FAMILIES` | ADAPTED, not removed: it is the legacy bridge for `job_postings.role_family`, which three production indexes key on. `_legacy_role_tier` uses it only when the canonical tree is silent, and the reason is in the code | **met** |
| Persist `taxonomy_version`, `role_families[]`, `role_specialties[]` on jobs and résumé profiles | jobs: `job_postings.tech.*`, backfilled to `tech-tax-1.1.0` across 405,133 rows. résumés: `build_resume_profile()["tech"]`, surfaced as `resume_taxonomy` on the verdict | **met** |
| Five-rung role alignment | `ALIGNMENT_TIERS` = exact_specialty / adjacent_specialty / same_family / adjacent_family / unrelated | **met** |
| Versioned specialty-adjacency matrix | `ADJACENCY_VERSION = "tech-adjacency-1.1.0"`, 218 declared pairs, every one of 102 specialties has at least one neighbour. 1.1.0 added the 34 edges leadership and security are STAFFED FROM — without them a backend engineer was `unrelated` to "Staff Engineer" | **met** |
| Confident cross-domain veto; ineligible / not_scored; no second overall score | `fit_score.ELIGIBLE / INELIGIBLE / NOT_SCORED` + `_eligibility`. `eligibility` answers a different question from `score`; it is not a number and does not compete with one | **met** |
| Hard constraints separated from ranking | a hard requirement we cannot evaluate is `status: unknown` + `hard_requirement: True`, surfaced and NOT priced; only a KNOWN mismatch gates | **met** |
| Unknown never penalised (salary, sponsorship, seniority, work mode, employment type) | `_normalized_score` drops an unmeasurable component from numerator AND denominator; `tests/test_fit_score_v2.py::TestUnknownDoesNotAutoFail` | **met** |
| Import and version the official O*NET title and software-skill datasets, plus a tech overlay | `scripts/import_onet.py` → `services/onet_data.py` (`onet-30.0`, 1,236 titles / 1,449 skills, office products excluded) + `services/onet_overlay.py` (`onet-overlay-1.0.0`, 248 terms, 243 of them absent from O*NET) | **met** |
| Extend specialties from real unmatched-title evidence | 20 added: enterprise/mainframe, build/release, quant/low-latency, HPC, AI agents, AI tooling, AI security, edge/CDN, storage, virtualization, privacy, cryptography, analog/mixed-signal, RF, EDA, process/yield, product/test, packaging, photonics, data-centre operations | **met** |
| Semiconductor/hardware in the DEFAULT search | `F_SEMICONDUCTOR` moved into `CORE_FAMILIES`; `tests/test_tech_taxonomy.py::test_silicon_design_is_CORE_and_in_the_default_scope` | **met** |
| Do not bump `FIT_SCORE_VERSION` before the reparse/compat plan | `_COMPATIBLE_REQUIREMENT_VERSIONS = {"fit-v2.0", "fit-v2.1"}`: the extraction contract is unchanged, so 216,687 stored profiles stay valid through the deploy | **met** |
| The teacher-to-backend regression passes | `evals/test_fit_score_ranking.py::test_no_nontechnical_resume_surfaces_a_software_role` — for all five non-technical résumés EVERY `j_backend*` posting is `ineligible`, scores <= 34, and ranks below every eligible posting. P@5 0.978, NDCG@5 0.949 | **met** |
| Taxonomy eval uses real unmatched titles, per-family precision and recall | `evals/eval_tech_taxonomy.py` reports per-family AND per-stratum. Stratum **H** is 44 titles taken verbatim from the 26,990 distinct live titles `tech-tax-1.0.0` called `unknown`. Current: precision 1.000, recall 1.000, leakage 0.000, family accuracy 1.000, H 44/44 | **met** |

## Retrieval and data quality

| criterion | evidence | status |
|---|---|---|
| Remove the 2,000-result traversal ceiling | the cursor changes shape to a keyset past the ranked window; `evals/test_tech_feed_retrieval.py::TestTraversalCeiling` traverses 60 postings through a 20-row window with no repeats | **met** |
| Preserve stable ordering, cursor expiry, deduplication | index order is total on `(posted_at DESC, job_id ASC)`; a dead session still 410s; `_dedupe` + location-variant collapse still run on a keyset page | **met** |
| `evals/eval_tech_feed_retrieval.py` exists | added, plus `evals/test_tech_feed_retrieval.py` as the gate | **met** |
| Evaluate by source, family, specialty, cohort, ambiguous title, geography, seniority | all seven dimensions, 46 strata, recall 1.000 / precision 1.000 | **met** |
| Normalise work mode, employment type, salary, seniority, sponsorship, clearance without fabricating | `normalize_employment_type` (closed vocabulary, `foh`/`sm`/`career-service-exec` stay `unknown`), `normalize_work_mode` (body read only for EXPLICIT phrases), `normalize_country` (ISO, diacritics folded), `_sponsorship_of` / `_clearance_of` (True-or-None, never False); `tests/test_inventory_normalization.py` | **met** |
| Indexes and explain-plan tests for the finalized paths | `scripts/explain_job_inventory.py`; every tech-feed path IXSCAN with no blocking sort. The UNBOUNDED keyset query is kept in the harness as a 565x-slower regression marker | **met** |

## Frontend and rollout

| criterion | evidence | status |
|---|---|---|
| Contracts stay under `/api/jobs/tech-feed` | `POST` / `DELETE` / `GET /api/jobs/tech-feed/feedback` — no new top-level prefix, so no Terraform edge route is needed | **met** |
| save/unsave, apply confirmation, tailoring, tracker, auto-apply on unified cards | wired through the existing `useJobSearch` handlers via one adapter; `tech-feed-card-flows.test.tsx` | **met** |
| Feedback contract with the six reasons | `not_interested`, `irrelevant_role`, `wrong_company`, `duplicate`, `stale_job`, `incorrect_metadata` | **met** |
| Cross-device suppression replaces browser-only | server write-through + reconcile on mount; localStorage demoted to an optimistic cache | **met** |
| Occupation filters separate from employer-category filters | occupation is the primary rail (`roleFamilies`/`roleSpecialties`/`techScope`); employer scope, kind, ids and categories are a separate advanced group | **met (already true)** |
| Source freshness, direct-apply status, concise "why matched" | `verifiedLabel()`, a two-state direct-apply chip, and `role_alignment.detail` rendered verbatim | **met** |
| Rollback and staged rollout | flag unchanged; `?techfeed=1` / `?techfeed=0` writes it, so a staged rollout is a link | **met** |

## Bugs found while building, each fixed with a test

| bug | measured impact |
|---|---|
| `career_portal_discovery._worker_guard` did `if not url_is_safe(url)` on a function returning `(ok, reason)`. A non-empty tuple is always truthy. | the scheduled discovery worker's SSRF guard had **never blocked anything** |
| `probe_software_company_boards.py` passed no `guard=` at all | no SSRF check on a script that fetches employer-guessed hosts |
| `{"locations.country": {"$in": ["usa"]}}` against a stored `"USA"` | the country filter matched **0 of 217,019** located postings, on every country |
| `employment_type` filtered on the provider's raw spelling | `full_time` missed "fulltime", "regular-full-time", "ft-hourly-w-bn", "cdi" |
| requirements hydrated on one profile loader, scored by another | when they disagreed, every row scored with NO requirements and the page was pinned at 40 |
| `$addToSet` with `$slice` | rejected per-op in an unordered bulk write; **16,339 of 24,175** employers silently dropped |
| `_ONET_TITLES` computed before `_norm` was defined, into a bare `except` | a vocabulary that looked sourced and did nothing (0 of 1,236 titles usable) |
| `registry_refs.company` written, `registry_refs.company_ids` read | `resolve_employer(company_id=...)` matched nothing on 23,782 employers |
| `retrieve(limit=CANDIDATE_LIMIT)` bound at import | the constant could not be changed at runtime |
| keyset `$or` with no range bound | 71,333 keys examined for 250 rows, **13.6 s**; 250 keys and 24 ms with the bound |
| `RAMP HOLDINGS INC` in Ramp's own `wrong_names` | `name_key` strips `holdings`/`inc`, so **Ramp blacklisted Ramp** |
| `build_query` ended with `q["$and"] = _and`, an assignment, while `countries` and `employment_types` appended to `q.setdefault("$and", [])` | both clauses were built, put on the query and **deleted one line later**. `countries: ["india"]` returned the same 17,683 postings as no filter at all |
| `query` — the search box — was read only as a fallback when `titles` was also set | with no title list, the normal case, typing "kubernetes" **narrowed nothing** |
| `min_years` compared against `tech.required_years_max` | populated only when a JD spells out a range; set on **0 of 224,389** live postings, so the filter could never fire |
| `role_alignment` set `cross_domain` on ANY two unrelated technology families, and `_eligibility` turns that into a hard INELIGIBLE | **522 of 2,500** core-tech postings were removed from one live account's feed — 317 staff/architect roles, 161 security roles. None was a change of occupational domain |
| `technical_leadership` had no adjacency edge to the IC specialties it is staffed from | a backend engineer read as `unrelated` (0.08) to "Staff Engineer" — the most common promotion in the industry |
| `board_kind` was declared in the catalogs, read by two surfaces, and written by no ingest path | absent on all 225,095 active postings, so `apply_mode: direct_apply` matched **every posting in the inventory**, marketplace reposts included |
| `_diversify_employers`' first key was the posting's `employer_id` slug | JPMorgan's Oracle instance stamps three legal-entity names on one tenant, so the page cap saw three companies and **nine of the first twenty-five cards** were one employer |
| `resolve_employer` queried `registry_refs.tenant_keys`; the backfill wrote `registry_refs.tenant` | 4,156 employers carry the written spelling, **0** carry the read one — the strongest join key available was dead |
| `coverage_block` reported `companies.estimated_document_count()` as `known_employers` | the banner read "across N employers" where N was **every company ever harvested**, ~80,000 |
| `normalize_posting` never ran again after ingest | 3 v4 normalisations existed in code and on **no row**; 10,721 postings stated "minimum N years" and still read `seniority: unknown`, invisible to anyone filtering by level |

## The coverage number

The 43.0% baseline measured `company_ats_endpoints.status == "validated"`,
which means the endpoint ANSWERED. It cannot distinguish a company's own board
from a stranger's with a similar slug, so it is not a coverage number and the
70% target cannot be read against it.

Rebuilt on the honest definition, coverage started at **0%** — nothing in the
database recorded who owned anything.

### Where it got to, and how

| stage | validated coverage of the technology denominator |
|---|---:|
| honest definition, before any verification | 0% |
| after pass 1 (provider owner metadata) | 8.9% |
| after pass 2 (verify the domain, walk to the board) | 13.2% |
| after registering the boards we were already crawling | 22.8% |
| after the curated catalogs corroborated the provider | 33.1% (868 of 2,624) |
| after a guarded content pass re-validated them | **33.1%** (869 of 2,625) |

By cohort at 33.1%: `niche_startup` 60.3% · `private_growth` 67.7% ·
`mnc` 55.2% · `public_tech` 20.3%.

**This criterion is NOT met.** 33.1%, not 70%. Regenerate the current figure
before quoting it — the passes are idempotent and re-runnable, so the number
moves:

```bash
PYTHONPATH=shared/python python scripts/employer_coverage_report.py --markdown
cat docs/coverage/tech-employer-coverage.md
```

### What actually stood in the way

Three of the four blockers turned out to be bookkeeping, not discovery, and
each one was worth more than a faster crawler:

1. **1,437 boards carrying 124,226 active postings had no `career_sources`
   row at all** — Wells Fargo, Northrop Grumman, RTX, PwC, CVS Health, Amazon.
   We ingested their jobs on every crawl and the coverage registry did not know
   their boards existed, so pass 2 — which walks from an employer's verified
   domain TO a board — had nothing to walk to. `register_ingested_boards.py`.
2. **`resolve_employer` read `registry_refs.tenant_keys`; the backfill wrote
   `registry_refs.tenant`.** 4,156 employers carry the written spelling and 0
   carry the read one, so the strongest join key available for a Workday or
   Oracle board was dead. Unresolved employers went 373 → 4.
3. **A second evidence class was already in the repo and unread.** The three
   Python catalogs name 2,370 exact `(vendor, tenant)` endpoints and are
   human-maintained — which is what `CLASS_CURATED` is for. Paired with the
   provider's own statement they satisfy rule 4b, and only when the two AGREE.
4. **Our own concurrency was erasing the number.** One content pass run beside
   two others turned 830 sources `blocked` in a single sweep — boards the
   crawler had read that morning — and dropped validated from 2,040 to 1,311.
   A 403 to our prober is a fact about our request; the pass now checks it
   against the live inventory and records a note instead of a demotion. Re-run
   with the guard and half the concurrency: 38 blocked, not 830, and 81
   verdicts declined because the inventory contradicted them.

### What is left, measured

Of the uncovered technology employers:

| bucket | employers | core-tech postings they hold |
|---|---:|---:|
| endpoint known, ownership unproven | 578 | **8,538** |
| a source row but no endpoint | 822 | 165 |
| no source row at all | 543 | 54 |

Almost the entire posting-weighted gap is one bucket, and 187 of those already
carry two independent signals and are waiting only on content validation. The
long tail — 1,365 employers holding 219 postings between them — is real
discovery work with very little yield behind it.

### Two things that were checked and found NOT to be problems

Recorded because a negative result is only useful if it is written down, and
both of these looked like large misses before they were measured.

**The `adjacent` scope is not hiding software roles.** 2,700 active postings
are `adjacent` and therefore outside the default `core` search. They are IT
support (893), solutions engineering (867), technical product (510), UX and
design systems (323), technical writing (85) and developer relations (24).
Sampled titles are "Service Desk Analyst", "Senior Sales Engineer", "Technical
Support Engineer". That is what adjacent means; nothing in it belongs in the
default scope.

**The `unknown` scope is not hiding them either**, which was the bigger worry:
132,543 active postings (57.4%) carry `tech.tech_scope: unknown`, and the
default query cannot reach them. Of a 6,000-row sample, 118 titles matched a
deliberately obvious technical-title test, and re-running the CURRENT
classifier over those 118 moved **3** to `core` — 0.05% of the sample, roughly
66 postings across the whole bucket. The bucket is genuinely non-technical, not
misclassified.

### What is emphatically NOT in place

A way to reach 70% by relaxing the definition. The old 43% was 43% of "the
endpoint answered". Every employer counted here has either a documented chain
from their own verified domain to the exact endpoint, or two independent
parties agreeing about who owns it — and where those two disagree, the
conflict is reported and no ownership is claimed.
