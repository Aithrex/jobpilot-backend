# Handoff — unfinished relevance work (2026-09-16)

Context: a live user reported every job scoring 40–50 and being irrelevant.
Root causes were found and mostly fixed on branch
`h1b-sponsorship-ingest-and-gap-list`. **Sections A and B below were finished
2026-09-16** (measurement + acceptance). Section C (FE contract) and Section D
(deferred) remain.

Run everything with **`python3`**, never bare `python`. Gate:
`python3 scripts/verify.py --full` must be green before anything ships.
Never weaken a test or eval to get green.

---

## A. Taxonomy junk-vocabulary purge — DONE

**Shipped:** `shared/python/services/tech_taxonomy.py` (`_WEAK_TECH_TERMS` +
`discriminating_terms`, version `tech-tax-1.3.0`).

**Re-measurement (2026-09-16, inline bodies only):**
`docs/coverage/reports/2026-09-16-taxonomy-purge-measure.json`

- 600 stored core-no-family rows with inline body ≥200 chars
- Classify demotions out of core: **53**; stayed core-no-family: **404**
  (trade `53:404`)
- Clause-level weak-only ≥3 demotions: **91 / 91**
- Stayed rows mostly genuine by title: **385 vs 19** junk-looking
- Recall on 200 Analyst/Specialist/Engineer with real bodies + family:
  **1.000** (0 demoted)
- **Do NOT tighten** `_AMBIGUOUS_TITLE_RE` 2-hit clause

**eval_live_titles:** scope accuracy **0.979** (no regression). Miss list
saved at `docs/coverage/reports/2026-09-16-live-titles-misses.txt` (12 misses,
same titles as prior).

---

## B. Country normalization + feed location dimension — DONE

**Shipped:**
- `shared/python/services/location_country.py` — CDMX, Bangkok MSG HQ, German
  street addresses, `(M/F/D)`/`(F/H/X)` title markers, Remote-UK / India-Remote,
  Manila/Toulouse/Sydney CBD; never defaults to US; undeclared home no longer
  labels US rows as foreign
- `tech_feed_service.py` — `work_location` on `rank_score` (demote + badge +
  opt-in `hide_foreign_locations`), distinct from sponsorship
- `job_inventory_service.normalize_location` delegates to the resolver
- `scripts/backfill_posting_normalization.py` uses the resolver and selects
  rows with empty `country_code` even when already at `NORMALIZATION_VERSION`
  (dry-run: 271/500 country fills; live Mongo is read-only for agents — operator
  applies with `--apply`, and **without** `--with-bodies` so the version stamp
  is not frozen mid-upgrade)

**Acceptance:**
`docs/coverage/reports/2026-09-16-relevance-acceptance.md`

- Location: non-US primary in top-25 **9 → 0**; 9 before-top-25 rows sank
- Work-auth probe (`query=clearance`): match_score unchanged; rank drops
  (Anduril 0.50 → 0.16); reachability `unknown` → `unlikely` with clearance /
  no-sponsorship badges

**fit_score._score_location was NOT changed.**

---

## C. Frontend contract reconciliation — NOT DELIVERED

Frontend is fully committed (`feat/work-auth-signals`, 3 commits) and its CI
blocker is fixed. What is missing is the written contract: the exact field
names and types the UI consumes for work authorization, the clearance and
no-sponsorship badges, and the sponsorship signal, reconciled against what the
backend actually returns. A mismatch here is the classic cross-repo bug this
platform's CLAUDE.md warns about. Verify against
`shared/python/services/sponsorship_service.py` and `work_authorization.py`.

Note `data_vintage` is now the fiscal period string `"FY2026"`, NOT an ISO
timestamp (`data_imported_at` carries that). Three sponsorship outcomes must
render differently and must not be conflated: a real signal; an explicit
**census-absent** row meaning the employer provably filed nothing (458
employers / 12,753 active postings, incl. Northrop Grumman, SpaceX, Anduril);
and no row at all, which must render as nothing — not zero, not "does not
sponsor". `lookup_status(name)` reports which of the three a lookup got.

---

## D. Known-deferred, with the reasoning (do not "fix" blind)

1. **Band calibration.** Median score is ~37–48 per user while "good match"
   starts at **65**, so most things read as "weak match". Moving `BANDS` or
   `COMPONENT_MAX` to make the number look better is the same failure as
   weakening an eval. The honest levers are A and D.2. This is a product
   decision.
2. **`_normalized_score` evidence-breadth amplifier.** It is `earned/possible`,
   so excluding a component re-weights the survivors upward: a posting
   measurable on 3 components competes on a 55-point frame, a fully-measured
   one on 100. After the fixes this is a tail problem (a few dozen rows), not a
   distribution-wide tax. Recommended: handle in `rank_score` — the feed
   already has `evidence_breadth` and `confidence` on every verdict — and/or
   improve job-side requirement extraction. Do NOT price unknown; 2.1 removed
   that deliberately for measured reasons.
3. **robots.txt, two bugs, neither fixed.** `career_portal_discovery.RobotsCache`
   uses stdlib `urllib.robotparser`, which is first-match not longest-match and
   mangles `Disallow: /?` into `Disallow: /`. It fails CLOSED — it refuses
   boards employers explicitly allow (Netflix and Qualcomm publish
   `Allow: /api/pcsx`). And it has exactly ONE caller, in discovery: **nothing
   in the ingest path reads robots.txt at all.** Needs its own review.
4. **Apple attribution.** Apple postings will land with `employer_id: "apple"`
   and NO `canonical_employer_id` until `career_sources` carries an
   owner-verified row for `custom_apple:apple`, so Apple reads as 0 in any
   canonical-id count — the exact artifact that hid Boeing's 402 postings.
   Dry run first:
   `PYTHONPATH=shared/python python3 scripts/verify_career_sources.py --mode employers --employer-id emp_d471a0526f61`
   Add `--apply --register` only after reading the proposed evidence.
5. **Apple retail volume.** ~60% of Apple's US board is Retail (Specialist,
   Genius, Business Expert). `tech_taxonomy` should class them `non_tech` and
   keep them out of the default feed. **Verify it does** — ~2,700 retail rows
   leaking into the tech feed is the visible regression of that change.
6. **Bad live data, read-only findings.** `career_sources` holds a `walmart`
   row in state `content_validated` with an EMPTY `canonical_endpoint`. And
   `palantir` is `validated` and live but the crawl carries 0 postings from it
   — a sweep gap, not a source gap.
7. **JPMorgan display names.** `CHASE FAMILY LIMITED PARTNERSHIP NO. 5`,
   `NUTMEG CONTAINER CORPORATION`, `M.J. WILLIAMS COMPANY` (2,360 postings) all
   sit on `oracle-orc:jpmc` under one `canonical_employer_id`. A card that
   DISPLAYS those names looks up sponsorship by them and gets nothing. Fix on
   the display side — prefer the canonical employer name — not with an alias
   table. Never rewrite `employer_id`; three production indexes key on it.
8. **`CLAUDE.md` still documents bare `python`** on 4 lines (11, 12, 582, 583).
   `docs/qa/verification.md` is already corrected. Fix CLAUDE.md to match.
9. **Sponsor coverage gap list** — `docs/coverage/h1b-sponsor-gaps.{json,csv}`
   ranks 6,610 uncovered sponsors by `approvals × software_likelihood ×
   conversion`. Top five are Apple, Google, Meta, Tesla, IBM. Google
   (robots.txt disallows paged results) and Meta (ToS prohibits automated
   collection) were deliberately NOT crawled — that is a human/legal decision,
   not an oversight. IBM, Goldman, Deloitte, Walmart, KPMG, ByteDance need a
   rendered pass. Do not bypass bot walls or robots.txt.

---

## Ground rules that are load-bearing here

- **ONE match score** (ADR-002). Never a second formula, never post-process
  `match_score`. Preference/ordering goes in `rank_score`.
- **No parsed résumé ⇒ NO score.** `match_score: 0` with no `fit`/
  `score_version` marker renders "Not scored", never a number.
- **Unknown is excluded, not priced.** Not knowing must never cost what being
  wrong costs.
- **Silence is never a mismatch** for clearance or sponsorship.
- **Employer identity:** count on `canonical_employer_id` → `(ats, tenant)` →
  name, in that precedence, never summed. Counting on `employer_name` reports
  Boeing as 0 when it holds 402 active postings.
- **One network pass at a time.** Running three concurrently once turned 830
  sources `blocked` and cut validated coverage by a third.
- The crawler **is already running** (EC2 systemd unit, all four sources, every
  ~15 min). EventBridge rules are disabled because the unit replaced them, and
  `ingest_runner_heartbeat` is stale from the pre-cutover runner. Neither means
  dormant.
