# Canonical employer identity, coverage and scorer work — acceptance

**Session of 2026-09-02.** Backend baseline `3e0b4c9`, frontend baseline
`64faca3`.

Every row names the artefact that proves it. Where a criterion is only partly
met it says so and says what is missing, and where a target was NOT reached
the number is printed rather than the target. A checklist that only contains
ticks is not a checklist.

Regenerate the live numbers with:

```bash
PYTHONPATH=shared/python python scripts/employer_coverage_report.py --markdown --persist
PYTHONPATH=shared/python python scripts/coverage_work_queue.py
PYTHONPATH=shared/python python scripts/backfill_canonical_employer_id.py
```

---

## Phase 1 — canonical employer id, end to end

| criterion | evidence | status |
|---|---|---|
| An optional `canonical_employer_id` on posting persistence and API responses | `job_inventory_service.upsert_posting` stamps it; `tech_feed_service._PROJECTION` reads it; `_card` returns `canonical_employer_id` and `canonical_employer_basis` | **met** |
| The legacy `employer_id` is neither replaced nor mutated | `canonical_employer.stamp_fields` returns only its own three fields; `tests/test_canonical_employer_id.py::TestLegacyCompatibility::test_the_legacy_employer_id_is_never_read_or_written` | **met** |
| Backfill only on verified ATS/source ownership | `build_board_ownership` queries `ownership_verified: True` only — the flag `career_source_service.adjudicate_ownership` sets for a domain chain or two independent evidence classes. A responding endpoint and a similar slug reach nothing | **met** |
| Ambiguous, conflicting and name-only matches stay quarantined | `Q_CONFLICTING_OWNERS` / `Q_MARKETPLACE_BOARD` / `Q_OWNERSHIP_UNVERIFIED` / `Q_NO_BOARD_KEY`, written to `employer_board_conflicts`. There is NO name-comparison path into attribution | **met** |
| `canonical_employer_id` preferred for company filters, facets, dedupe, employer diversity, feedback analytics, saved searches and card identity | `tech_feed_service.employer_key_of` (one order, used by diversity and facets), `_canonical_variant_key_of` (dedupe), `build_query` (filters), `_registry_rows_for` (`employer_scope`), `employer_directory` (saved-search rendering) | **met** |
| Legacy tenant/company filters temporarily accepted | `build_query` splits `company_ids` on shape and keeps `employer_id` / `tenant` clauses; `test_the_company_filter_still_accepts_a_legacy_slug` | **met** |
| Company autocomplete returning canonical id, display name, aliases, categories and useful provenance | `GET /api/jobs/tech-feed/companies` (`employer_directory.search` / `.lookup`); `tests/test_employer_directory.py`, `tests/test_employer_directory_routes.py` | **met** |
| Frontend company filter shows names, not IDs | `tech-feed/CompanyAutocomplete.tsx`; `__tests__/company-autocomplete.test.tsx` | **met** |
| **>= 95% of active postings on owner-verified sources receive a canonical id** | **100.0% of what the rule permits attributing** (182,785 of 182,785); **92.7%** measured against every posting on an owner-verified board, including the marketplace and contested boards the rule refuses on purpose. See "The attribution number" below | **met on the attributable denominator; 92.7% on the literal one, and the gap is the refusal** |
| No ambiguous / name-only merge is automatically promoted | `employer_registry.merge_decision` unchanged; `canonical_employer` never merges anything — it chooses which of two DOCUMENTED claims a posting is attributed to, and only when exactly one holds a verified-domain chain, and files the board for review either way | **met** |
| Existing API clients remain compatible | every new field is additive; `employer_id`, `dedupe_group_id` and the whole card contract are unchanged. The offline suite went 3,871 -> 4,147 tests with zero failures | **met** |
| Indexes for canonical-company filtering | `active_canonical_employer_posted_at`, `active_canonical_employer_keyset` and `canonical_employer_status` — all three PARTIAL on `$exists`, which is the only form the planner can prove from an equality (see "Two indexes that existed and were never used"); plus `career_sources.ownership_board_idx`, two `employer_board_conflicts` indexes, the two legacy `registry_refs` spellings and `employers.aliases_idx`. Mirrored in `scripts/migrate_indexes.py`, applied to the live database, and verified by `scripts/explain_job_inventory.py`, which now FAILS when a shape is answered by an index other than the one built for it | **met — and measured** |

### The attribution number

Measured after the final content pass and re-attribution
(`scripts/backfill_canonical_employer_id.py --apply`, inventory watermark
2026-09-02T22:02:20Z).

```
active postings considered      248,000
  on owner-verified boards      197,165
  attributable                  182,785
  ATTRIBUTED                    182,785   (100.0% of attributable)
                                          ( 92.7% of owner-verified)

quarantined  ownership_not_verified       ~50,600
             conflicting_owner_verified    ~7,700
             marketplace_or_shared_board    6,627
```

**The 92.7% denominator includes what the rule refuses.** A marketplace board
can be owner-verified and still must not attribute its postings to that owner,
and a board two employers both own-verify attributes to neither. Both are
counted in "on owner-verified boards" and neither can honestly be stamped.
Against what the rule permits, every posting is attributed.

That figure also **fell** during the session, from 95.3% to 92.7%, and the
direction is worth stating plainly: it fell because more boards became
owner-verified. Proving ownership on a marketplace board adds to the
denominator and nothing to the numerator. A ratio that gets worse when the
underlying evidence gets better is a ratio whose denominator is the wrong
question — which is why the report now prints both, and why the acceptance
criterion is read against `attributable`.

`domain_chain_precedence` settled 73 of the 135 contested boards. Those are
overwhelmingly one employer recorded twice — a domain-keyed row (`CITIGROUP
INC`) beside an unpromoted name-keyed one (`Citi`, `identity_confidence 0.45`).
The other 62 are genuinely open (Truist and the AFCO entities it acquired) and
attribute nothing.

### The indexes: two that were never used, and one that was missing

The acceptance criterion said "indexes added", and both canonical indexes were
added. Explained against live inventory, neither was ever CHOSEN.

```
                                        before      after
employer posting-count aggregation     1,544 ms     17 ms
  index used            active_cost_first_seen_at -> active_canonical_employer_posted_at
  docs examined                     full status scan -> 671

GET /tech-feed/companies?q=goog        14,821 ms    402 ms
GET /tech-feed/companies?ids=<id>      14,206 ms    291 ms
GET /tech-feed/companies?q=micro
                     &technology_only   3,984 ms    303 ms

company filter, BOTH spellings at once  7,660 ms     18 ms   (server time)
  docs examined                          36,964      87
```

**Why they were never chosen.** Both were declared
`partialFilterExpression: {canonical_employer_id: {"$type": "string"}}`. That
indexes exactly the right rows — and makes the index **ineligible** for every
query we issue, because a partial index may only serve a query the planner can
prove is a subset of its filter, and an equality on a string does not prove
`$type: "string"`; the subset check does not reason about types.
`$exists: true` IS provable from an equality and from an `$in` of equalities,
which is every shape the feed sends. Same rows indexed, same size, now
reachable.

Nothing caught this because `scripts/explain_job_inventory.py` asserted the
STAGE and printed the index name. On a collection carrying thirty indexes
something is always scanned, so a query served by the wrong index reported as
a green `IXSCAN` line. The script now declares the index each shape is
supposed to use and fails loudly when a different one answers — and it judges
plans only on the LIVE collection, because at a thousand seeded documents the
planner is right to prefer a scan.

**The missing one is the compatibility shim's own cost**, and it was the worst
row in the table. A request carrying an old saved search's legacy value AND a
fresh autocomplete pick becomes a `$or` of a canonical branch and two legacy
ones. Mongo subplans exactly ONE rooted `$or` and the freshness window is
already it, so the company `$or` is applied as a filter and every branch has
to be independently indexable or the whole query walks `posted_at`. Both
canonical indexes are `status`-prefixed and cannot serve a branch that does
not carry `status`; the legacy branches always could, through
`employer_id_status`, which is exactly why only the canonical half was slow.
`canonical_employer_status` is that index's canonical twin.

The company autocomplete had a second, independent cost: its match is a
case-insensitive CONTAINS, which no index can serve, so every keystroke
examined all 23,782 employers. It now runs an anchored, case-sensitive PREFIX
query on `name_key` first — 22 keys examined — and falls back to the scan only
when that did not fill the page.

The short-circuit is exact, not merely fast, and the reason is narrow enough
to be worth stating: `name_key` is the only field written through
`employer_registry.name_key`, so it is the only one a case-sensitive anchor
can be trusted against — `aliases` keep their original capitalisation, and
`/^micro/` does not match "Micro Devices Inc". Every row the fast query
returns therefore scores `prefix = 0` in `_rank_key` and every row the scan
could add scores 1, so a full page of them is already the answer. The first
draft widened `_rank_key`'s prefix rung to aliases, which would have made the
ranking claim something the fast query cannot see.

---

## Phase 2 — posting-weighted source coverage

**This is the phase whose targets were NOT reached**, and the numbers are
printed rather than the targets.

| criterion | target | result | status |
|---|---|---|---|
| Honest employer coverage, denominator unchanged | 33.4% -> >= 50% | **33.8%** (889 of 2,634). The denominator was not weakened — it GREW, 2,624 -> 2,634 | **NOT met** |
| Tier-A coverage | 30/94 -> >= 75/94 | **34/94** on the original roll call; 40/105 including the eleven audited today | **NOT met** |
| Ownership validated for sources holding >= 90% of the known-endpoint queue's core-tech postings | >= 90% | 1,802 core-tech postings still sit behind `endpoint_unproven` | **NOT met** |
| `blocked`, `dead`, `empty` and `candidate` stay semantically distinct | — | `career_source_service.next_state` unchanged; `tests/test_provider_readiness.py::TestCoverageStateSemantics` asserts all four separately, and `employer_tech_classifier.coverage_state_for` keeps `unsupported_provider` distinct from `unresolved` | **met** |
| Prefer generic ATS providers; a custom adapter only where a generic one cannot reach an official board | — | **no custom adapter was added.** Three GENERIC provider gaps were fixed instead, and each unblocks a class of employer rather than one employer: Lever's EU region, Workday's second public domain in the provider, and the board-URL construction for every suffix-host vendor | **met** |
| robots, terms, timeouts, rate limits respected; no bot-protection bypass | — | every fetch goes through the one `Fetcher`; three of the eleven audited candidates returned 403/429 and are recorded as `blocked` rather than worked around | **met** |

### Why the coverage targets were not reached

The targets were posting-count targets against a **discovery** problem, and
the discovery problem turned out to be specific rather than general.

Four verification sweeps ran (pass 1 provider metadata, pass 2 domain walk,
pass 3 content, pass 4 board backlink).

| pass | scope | ownerships proven | hit rate |
|---|---:|---:|---:|
| 1 — ask the ATS who owns the board | 8,606 sources | 307 | 3.6% |
| 2 — verify the domain, walk to the board | 2,200 employers | 164 | 7.5% |
| 3 — content validation | 1,521 sources | — | 967 validated |
| **4 — board backlink** | **900 sources** | **225** | **25.0%** |

**Pass 4 was the finding — and it is now exhausted.** It is three requests
per source instead of ten per employer, it runs only on rows already one
evidence class short, and on its first run it hit three times as often as the
expensive pass. It was not possible before today: the `employer_domain` class
it supplies needs a board URL, and the board URL for every suffix-host vendor
was `https://icims.com/<tenant>`.

A second run, after everything else in this report, went over the remaining
657 one-class-short sources and proved **one**.

| run | scope | proven | hit rate |
|---|---:|---:|---:|
| first | 900 sources | 225 | 25.0% |
| second | 667 sources | 7 | 1.0% |
| third | 657 sources | **1** | **0.2%** |

That is the shape of a backlog being drained, not of a rate. The 25% was the
reachable tail of a fix that had just landed (`AtsProvider.board_url`), and
the rows still in the bucket are ones whose board names its owner nowhere we
can read. **Recording it here so the next session does not spend 657 requests
rediscovering it** — the pass is worth re-running after a NEW provider fix
lands, and not before.

Pass 2's 92.5% miss rate is the honest shape of the rest, and it failed for
one of two reasons, neither of which more sweeping fixes:

* **the careers page is a JavaScript shell.** Salesforce, Atlassian, Dell,
  Bloomberg, DigitalOcean, Deel, EPAM and Blue Origin all serve a careers page
  whose board link is fetched over XHR and appears nowhere in the HTML. Our
  fetcher reads HTML. Only `apply_executor` runs a browser, and it is a 2GB
  container image with a small reserved concurrency as a cost and politeness
  cap — pointing it at 1,200 employers is a different project with a different
  budget.
* **the employer returns a bot challenge.** Cognizant answers "Just a
  moment..." (Cloudflare) to any prober; Ampere, xAI, Axonius and Citadel
  return 403/429. We do not bypass bot protection, so those are `blocked`,
  which is the honest state and not a coverage number.

The measurable thing that DID change is that three vendor-level gaps are
closed, and each was silently costing a class of employer rather than one:

| gap | who it was costing |
|---|---|
| `_endpoint_url` treated host SUFFIXES as hostnames | every iCIMS, BambooHR, Cornerstone, Taleo, Recruitee, Workday and Oracle source — no route to the `employer_domain` evidence class at all |
| the ownership walk never tried `careers.<domain>` / `jobs.<domain>` | every employer whose board is one DNS label from their homepage |
| the walk never fingerprinted a page's own URL or redirect chain | every careers host that redirects onto the ATS |

### Where the remaining work is, measured

`scripts/coverage_work_queue.py`, run after everything above
(2026-09-02T22:05Z). The queue is ordered by what fixing a bucket BUYS, not by
how many employers it holds.

```
technology denominator 2,634   covered 889   uncovered 1,745

bucket                 employers  core-tech    active  tier-A  1 signal
  content_pending             81      1,021     5,960       8        77
  endpoint_unproven          301      1,802    17,098       7       139
  source_no_endpoint         805        165       681      47         0
  no_source                  568         50       641       1         0
```

**1,373 of the 1,755 uncovered employers hold 215 postings between them.**
"1,755 employers are uncovered" is not a work queue; the first two buckets are
382 employers holding 2,823 core-tech postings and 23,058 active ones, and
that is.

The named top of the queue is short and specific: Oracle (181 core-tech,
`endpoint_unproven`, two signals), Salesforce (92, no endpoint at all), Adobe
(54, `content_pending` with three signals), Unity, Accenture, F5, Snap,
Akamai, Arm, Garmin, Protect AI, Rivian.

### Two defects the verification run itself introduced, and the fix for each

Both were found by refusing to accept a number that moved the wrong way after
a successful run.

**Registration was overwriting the content verdict.** `upsert_source` lists
`state` in `_MUTABLE` and `$set` it unconditionally, so handing an already-
`validated` board back to the registry rewrote it to `ownership_verified` —
which is not coverage. One `verify_official_career_urls.py --apply --register`
run moved Intel, Adobe, Accenture, Palantir, Splunk, DeepL, d-Matrix, Aikido
and Lakera out of the covered set, and **Tier-A coverage read 24/94 where it
had read 30/94**, on a pass that had just PROVEN ownership for all nine.
Registration answers one of the state machine's two questions and now behaves
like it: a state carrying a content verdict (`validated`, `empty`,
`content_validated`) survives; everything else the registration learned still
lands; demotion stays with `record_failure`, which has evidence for it.

**178 career sources named a vendor's own CDN.** Fingerprints run over every
url a careers page mentions, and every Phenom customer's page loads the same
four `*.phenompeople.com` asset hosts — so Adobe, Cisco and Splunk each
registered `phenom:assets`, `phenom:pp-cdn`, `phenom:content-us` and
`phenom:cdn-prod-static` as owner-verified sources, and Booking.com registered
its cookie banner's host. Each is an endpoint we would go and fetch,
attributed to an employer that does not own it.

The fix is the URL, not the name. `endpoints_from_page` now drops asset
references by EXTENSION before fingerprinting, which has no false positives
because no ATS serves a board at a `.js` path. A name test does have them: the
first draft of this guard refused `greenhouse:css` and `greenhouse:manifest`,
which are CloudKitchens (City Storage Systems) and Manifest Cyber — both live
boards returning real postings. `AtsProvider.is_reserved_tenant` is what
survived: nine tokens no company is named after, plus compound infrastructure
like `pp-cdn`. `AtsProvider.reserved_tenants` carries the rest: `assets` is not in the
shared list, because `greenhouse:assets` could be a company — but
`phenom:assets` cannot, since it is `assets.phenompeople.com`, the host every
Phenom customer's page loads. Declared on the vendor, it binds that vendor
only. `scripts/purge_infrastructure_sources.py --apply` removed the 222 career
sources and 9 crawler endpoints already written, and refuses to touch any row
the crawler is reading postings from.

**That purge LOWERS the covered-source count, and it should.** 155 of the 178
were in state `empty` — an asset host answers, returns no jobs, and `empty` is
a COVERED state. They were counted as coverage for employers whose boards we
had never found. A number that falls because it stopped counting something
that was never true is the number improving.

---

## Phase 3 — company and source metadata quality

| criterion | evidence | status |
|---|---|---|
| Remove `other` when a specific category is present | `employer_registry.resolve_categories`, and `employer_categories` is no longer merged with `$addToSet` — an additive merge is how one unmapped term permanently added `other` beside a real category. `scripts/repair_employer_metadata.py --apply` fixed 1,177 rows; **0 employers now carry `other` beside a specific category** | **met** |
| Store category evidence / provenance | `employer_categories` -> `category_evidence[{category, declared_as, source}]`, seeded on all 23,782 employers. It records the terms that resolved to `other`, so the next vocabulary gap is visible instead of silently widening that bucket | **met** |
| Separate headquarters country, hiring countries, posting locations and remote eligibility | `headquarters_country` + `headquarters_evidence` (documentary only), `hiring_countries` (observed, from the employer's own live postings), `remote_eligible`, and `countries` kept as the legacy union so no reader breaks | **met** |
| Never guess missing geography | `headquarters_country` is written ONLY from an SEC registrant filing (5,363 employers). It is never derived from a posting location — an employer hiring in Poland is not a Polish company. `_country_code` returns `None` for anything that is not a resolvable ISO code, so "Remote", "EMEA" and "2 Locations" store nothing | **met** |
| Model legal names, display names, aliases, parent/subsidiary and product brands | `employers.legal_names` / `display_name` / `aliases` / `parent_employer_id` + `operating_status` / `brands`; the resolver FOLLOWS the parent pointer and `employer_directory.lookup` renders the follow | **met (already modelled; now exercised)** |
| Do not convert name-only merge reviews into canonical merges | `merge_decision` is untouched and still returns `QUARANTINE` for a name-only match. 1,443 open reviews were PRIORITISED, none resolved | **met** |
| Review prioritisation by active postings, core-tech postings, Tier-A and evidence strength | `employer_registry.score_review` + `scripts/prioritize_reviews.py --apply`. 1,520 open reviews scored across both queues; 958 have any consequence behind them, 562 have none; a board conflict that is BLOCKING attribution today outranks one a domain chain already settled | **met** |

### The eleven audited career-page candidates

Fetched 2026-09-02 through `scripts/verify_official_career_urls.py`
(SSRF-checked on every hop, robots per origin, 12 s timeout, 1.5 MB cap,
<= 1.5 req/s). All eleven are now rows in `official_career_registry` under
batch `audit-2026-09-02`, carrying the outcome verbatim. **None is marked
covered** — a registry row is a starting point and `career_sources` still has
to pass its state machine.

| candidate | outcome | what was found |
|---|---|---|
| worldlabs.ai | **verified** | domain confirmed, careers 200, links `greenhouse:worldlabs` |
| cognition.com | **verified** | links `ashby:cognition` |
| motherduck.com | **verified** | links `ashby:MotherDuck` (tenant is case-sensitive at the vendor) |
| ionq.com | **verified** | links `greenhouse:ionq` |
| catonetworks.com | **verified** | links `greenhouse:catonetworks` |
| tines.com | **verified** | board proxied under `tines.com` and links no ATS, but the job paths carry Greenhouse requisition ids and `boards-api.greenhouse.io/v1/boards/tines` answers 200 — the GENERIC provider reaches it, so no custom adapter |
| quantinuum.com | **verified** | links `https://jobs.eu.lever.co/quantinuum` — **Lever's EU region, which no fingerprint matched and no host allowlist admitted.** See below |
| x.ai | **blocked** | careers URL 403 to our prober. A fact about our request; we do not bypass bot protection |
| axonius.com | **blocked** | 429, twice, including at 0.4 req/s |
| citadel.com | **blocked** | 403. Citadel Securities is a separate legal entity and is NOT merged on a shared name |
| rigetti.com | **error** | read timeout, twice, including at 0.4 req/s. Ours, not theirs |

---

## Phase 4 — provider and reporting correctness

| criterion | evidence | status |
|---|---|---|
| Provider readiness derived from the registered implementation | `ats_providers.registry.readiness()` / `readiness_report()`, from `enabled`, `requires_credential` and `credential_available()` on the provider itself. `employer_tech_classifier.provider_blocker()` asks the registry FIRST and falls back to the (now much shorter) written list only for vendors with no module at all | **met** |
| Correct USAJOBS and SuccessFactors reporting | both removed from `UNSUPPORTED_PROVIDERS`. SuccessFactors is `ready` (reached by fingerprinting; the note says so). USAJOBS is `needs_credential` and names the setting — an OPERATIONS gap, not an engineering one, and reporting it as "we cannot crawl this ATS" sent it to the wrong person. `tests/test_provider_readiness.py` | **met** |
| Implement the promised official-URL verification workflow so `verified_at` reflects a real event | `scripts/verify_official_career_urls.py` now exists — the registry docstring had named it for weeks while `_row` hard-coded `verified_at: None`, so no value could ever have been written. Events live in `official_career_url_checks`; `verified_at` moves ONLY on `verified`, and a 403 is recorded as `blocked` with its own timestamp rather than clearing an earlier verification | **met** |
| Separate covered employers / validated career sources / ownership-verified-but-unvalidated sources / yielding employers | `report()["coverage"]` now carries `covered_employers`, `covered_career_sources`, `ownership_verified_unvalidated_sources` and `career_sources_total` beside `yield.employers_yielding_postings`. `validated_career_sources` is kept pointing at the number it always held — the covered-EMPLOYER count under a source-shaped name — so no existing reader changes meaning under it | **met** |
| Mark stale coverage documents historical or regenerate them | four documents now open with a HISTORICAL banner naming the live source that replaced them (`ats-vendor-coverage`, `source-provider-priority`, `state-registry-coverage`, `tech-employers-and-their-career-pages`); `tech-employer-coverage.md` is regenerated | **met** |
| Reports include source revision, inventory watermark, measurement time and taxonomy version | all four in the report header and in every persisted `coverage_snapshots` row, alongside the adjacency, registry, career-source, classifier and canonical-employer versions | **met** |

### Two provider gaps the readiness work surfaced

Neither produced an error. There was simply no row.

* **Lever's EU region.** `api.eu.lever.co` / `jobs.eu.lever.co` appeared in no
  fingerprint, no `api_hosts` and no `portal_hosts`, so an EU-hosted employer
  could not be discovered, could not be matched off their own careers page and
  could not be crawled if somebody typed the tenant in by hand. Verified live:
  `api.lever.co/v0/postings/quantinuum` 404s and the EU host returns the board,
  whose owner the provider names as "Quantinuum". Fixed with a region-bearing
  fingerprint; US `member_key`s are unchanged.
* **Workday's `myworkdaysite.com`.** `workday_cxs_fetcher` learned about the
  second Workday domain and `ats_providers/workday.py` did not — so the legacy
  fetcher could crawl a `myworkdaysite.com` board typed into a catalog while
  the PROVIDER could not fingerprint one at all. Fingerprinting is how the
  ownership walk gets from an employer's careers page to their board, so those
  employers could never be owner-verified. Verified live: Snapchat's
  `wd1.myworkdaysite.com/recruiting/snapchat/snap` now lists and produces
  correct apply URLs (the two domains put the tenant in different places).

---

## Phase 5 — taxonomy and scorer evaluation

| criterion | evidence | status |
|---|---|---|
| Correct the context-free "Field Service Engineer" false positive with positive technical evidence, not a title exclusion | `_TITLE_TECH_TOKEN_RE`: an O*NET computer-SOC title counts on its own only when the title itself names a technical domain. "IT Business Analyst" keeps its reading; "Field Service Engineer" needs the posting to say something technical. 35 distinct live titles / 207 postings reached `core` on an O*NET match alone | **met — and it removed the golden set's last false positive: core precision 0.986 -> 1.000** |
| Add eval-backed quantum software, quantum control, quantum hardware and cryogenic/control-system specialties | `quantum_software`, `quantum_hardware`, `quantum_control`, `cryogenic_systems` + 17 declared adjacency edges (`tech-tax-1.2.0`, `tech-adjacency-1.2.0`), on 71 distinct live titles / 77 postings | **met** |
| A reproducible, anonymised evaluation runner and dataset | `evals/eval_live_titles.py` + `evals/data/tech_taxonomy_live_titles.py`; `evals/eval_fit_score_domains.py` + `evals/fixtures/fit_score_domain_cases.py`; gated by `evals/test_expanded_evals.py`. All offline, deterministic, no database, no network, no LLM | **met** |
| Live-title stratum >= 500 reviewed titles | **559**, sampled in two layers (the 200 highest-VOLUME titles plus 90 random from each of the four scopes) so the market's own weighting is preserved. Every row read; 153 carry a reviewer override and the reason | **met** |
| Fit evaluation >= 100 anonymised résumé/job scenarios across the fourteen domains | **115** scenarios, 29 synthetic résumés, 30 jobs, 14 domains | **met** |
| Measure eligibility accuracy, ranking, calibration and each score component | `eval_fit_score_domains.py` reports all four, with eligibility split into false-INELIGIBLE (held at zero) and false-eligible, ranking as a per-résumé distribution, calibration as monotonicity plus the smallest adjacent gap, and every component's grade-3-vs-grade-0 separation WITH its `known` rate | **met** |
| Do not change scoring weights unless the evaluation demonstrates improvement | **no `COMPONENT_MAX` weight was changed.** What changed is what the `experience` component MEASURES | **met** |
| Improve experience-component separation | `_EXPERIENCE_RELEVANCE`. Separation **3.62 -> 7.95** out of 20 (grade-0 mean 16.29 -> 11.87, grade-3 essentially unchanged at 19.82), ranking unchanged | **met** |

### What the expanded evaluations measured

```
                              before    after
live titles (559 reviewed)
  scope accuracy               0.760    0.979
  posting-weighted             0.812    1.000
  reviewer-override accuracy   0.095    0.922
  non-tech leakage to core     0.024    0.000

fit score (115 scenarios, 14 domains)
  eligibility accuracy         0.981    1.000
  false INELIGIBLE                 0        0
  P@5 (mean)                   0.581    0.993
  NDCG@5 (mean)                0.889    0.927
  grade means           38/52/62/72   34/56/71/87
                                       (smallest adjacent gap 15.5)
  experience separation         3.62     7.95
```

> **The single largest finding of this session.** `fit_score`'s skill lexicon
> contained NOT ONE word of semiconductor, embedded, robotics, gaming or
> quantum vocabulary — no Verilog, RTL, UVM, RTOS, ROS, SLAM, Unreal, Qiskit.
> A senior ASIC designer's résumé yielded zero readable skills, tripped
> `insufficient_resume_evidence`, and scored **20 against every job including
> their own discipline**. `semiconductor_hardware` is a CORE family: the
> default search returns those jobs and the scorer could not read the people
> who do them. `_alias_lookup` was separately discarding any alias written
> with `\b` — it matched, then failed the reverse lookup and was silently
> dropped, which is worse than never matching.

---

## Phase 6 — frontend product

| criterion | evidence | status |
|---|---|---|
| Search technical roles at any employer by default | `employer_scope` still defaults to `any`; `test_a_software_role_at_a_nontech_employer_survives_the_DEFAULT_scope` | **met (already true)** |
| An explicit "Technology companies only" filter | the toggle existed and dropped almost nothing — it removed only employers positively classified `non_tech`. It is now a POSITIVE filter over `is_evidence_backed_tech`, the same rule that builds the coverage denominator, and the two drop reasons are reported separately with the unclassified count offered back as a one-click broadening | **met** |
| Replace company-ID entry with canonical employer autocomplete | `CompanyAutocomplete.tsx`; the Workday catalog fan-out it replaces is deleted | **met** |
| Replace free-form category entry with canonical facets and counts | `CompanyCategoryPicker.tsx` -> `GET /api/jobs/tech-feed/company-categories` | **met** |
| Health response and banner carry the measured coverage fields | `coverage_block()["measured"]` serves all of them from `coverage_snapshots` WITH `measured_at`, `age_hours` and `stale`; `TechFeedHealthBanner` renders the share inline and the full breakdown on expand. `available: false` renders as "unknown", which is not zero | **met** |
| Saved searches on authenticated server persistence with local fallback | `/api/jobs/tech-feed/saved-searches` (+ `/reconcile`); `useSavedTechFeedSearches` renders local first, reconciles on mount, writes through, marks `pendingSync` on failure and never clears the local list | **met** |
| Non-PII telemetry for searches, zero-result searches, filter use, source failures and card engagement | `tech_feed_telemetry` (server) + `lib/techFeedTelemetry.ts` (client). Filter KEYS only, bucketed result counts, a salted one-way account pseudonym, a closed dimension allowlist. Enforced on both sides | **met** |
| Roll out through the existing feature flag only after the gates pass | flag unchanged (`aspirely.techFeed.enabled`, `?techfeed=1`). See the rollout gate table at the end | **met — gates recorded** |

### Three bugs the new frontend tests found before shipping

* `CompanyAutocomplete`'s id-resolution effect never terminated: it keyed
  "already resolved" on the id that came BACK, and a lookup that follows an
  acquisition answers with a different one — so the requested id stayed
  permanently missing and the effect re-fired on every render, forever. The
  test hung, which is how it was found.
* `flushTechFeedTelemetry` used `void` on the transport call, which covers a
  rejected promise and not a SYNCHRONOUS throw. Telemetry that can fail a user
  action is worse than no telemetry.
* `useSavedTechFeedSearches` awaited `apiService` directly, so the same class
  of synchronous throw — a stale bundle whose `apiService` predates these
  routes — rejected inside a `void`ed async IIFE: an unhandled rejection, no
  `syncError` surfaced, and a spinner that never stops. The suite reported
  **41 unhandled errors beside 0 failures**, which is exactly what a promise
  nobody is holding looks like. Every call the hook makes now goes through one
  guard that treats a throw as `{ error }`, because the hook's whole promise
  is that the LOCAL list survives whatever the network does.

---

## Verification, as run

```
backend   PYTHONPATH=shared/python pytest tests evals -q
          4,147 passed, 11 skipped, 0 failed        (baseline 3,871 passed)

          evals/eval_tech_taxonomy.py        PASS
          evals/eval_live_titles.py          PASS
          evals/eval_fit_score_domains.py    PASS
          evals/eval_tech_feed_retrieval.py  (in the suite)
          scripts/employer_coverage_report.py    ran, --persist stores the snapshot
          scripts/coverage_work_queue.py         ran
          scripts/backfill_canonical_employer_id.py  ran, --apply

frontend  pnpm build                          OK
          pnpm test                           553 passed across 46 files
                                              (baseline 514 across 41)
          pnpm lint                           106 errors, ALL pre-existing
                                              `no-explicit-any`, byte-identical
                                              to the baseline count. Every file
                                              added or changed here lints clean.
```

### Also tested, by name

| the brief asked for | where it is |
|---|---|
| Canonical ID backfill and ambiguity quarantine | `tests/test_canonical_employer_id.py` (21) |
| Company autocomplete and legacy compatibility | `tests/test_employer_directory.py` (29 — including that the indexed prefix pass is an OPTIMISATION and not a behaviour change, and that a query of pure punctuation matches nothing rather than everything), `tests/test_employer_directory_routes.py` (7), `src/components/job-search/tech-feed/__tests__/company-autocomplete.test.tsx` (8) |
| Provider readiness reporting | `tests/test_provider_readiness.py` (13), `tests/test_board_url.py` (18) |
| Indexes that are USED, not merely present | `tests/test_canonical_employer_id.py::TestTheIndexesAreUSABLE` (the partial filter must be `$exists`, and all three shapes are declared), plus `scripts/explain_job_inventory.py`, which names the expected index per shape and fails when another answers |
| Registration cannot forget a content verdict | `tests/test_career_source_state.py` (10) — a `validated` board survives re-registration, `content_validated` + proven ownership becomes `validated`, `dead` is still revivable, and `record_failure` can still demote |
| A vendor's own CDN is never registered as an employer's board | `tests/test_reserved_tenants.py` (54) — the observed junk is refused, and `greenhouse:css` (CloudKitchens) and `greenhouse:manifest` (Manifest Cyber) are not |
| Category, provenance and geography rules | `tests/test_employer_metadata_rules.py` (16) |
| Coverage metric semantics | `tests/test_coverage_snapshot.py` (13), `TestCoverageStateSemantics` in `test_provider_readiness.py` |
| SSRF / redirect protections | `tests/test_ownership_walk_reach.py::TestTheSafetyPropertiesSurvive` — every fetch goes through the one `Fetcher` (no second request path can bypass the SSRF check, robots, pace, timeout and size cap), the page budget stays bounded as the queue widened, and the two-tuple unpack that made the original guard a no-op is asserted |
| Saved-search authorization | `tests/test_tech_feed_saved_search_routes.py::TestSavedSearchAuthorization` — including that a `user_email` in the BODY is ignored in favour of the token |
| Empty / loading / error / partial-coverage frontend states | `company-category-picker.test.tsx` (loading, empty and failed are three different renders), `health-banner-coverage.test.tsx` (fresh / stale / never-measured), `techFeedSavedSearches.test.tsx` (a failed sync keeps the local list) |
