# Handoff — unfinished relevance work (2026-09-16)

Context: a live user reported every job scoring 40–50 and being irrelevant.
Root causes were found and mostly fixed on branch
`h1b-sponsorship-ingest-and-gap-list`. Two workstreams were stopped
**mid-measurement** when the session ended. This file is what is left.

Run everything with **`python3`**, never bare `python` — several environments
here have no `python` on PATH and the failure is easy to misread as a pass.
Gate: `python3 scripts/verify.py --full` must be green before anything ships.
Never weaken a test or eval to get green.

---

## A. Taxonomy junk-vocabulary purge — STOPPED MID-MEASUREMENT

**Uncommitted:** `shared/python/services/tech_taxonomy.py` (+96 lines).

**The bug.** `tech_taxonomy` promotes a posting to `tech_scope: core` when
`len(tech_terms) >= 3`, counting loose imported O*NET/overlay vocabulary that
contains `linkedin`, `youtube`, `facebook`, `salesforce`, `chef`, `go`,
`magic`, `scheme`, `eiffel`, `vite`, `tempo`, `bedrock`, `enclave`. Measured:
**598 of 693** core-with-no-family rows in a 3,000-row live sample were
promoted by that one clause. "Growth Marketer" → core via
`linkedin, youtube, salesforce, sql`. "Tecnico Elettrico" → core via
`vite, tempo, agile`.

**Why it matters.** A posting with no role family has `role_alignment`
EXCLUDED from `_normalized_score`, so it competes on a ~55-point frame where
~25 points are near-automatic for an internship (intern job level ⇒ full
experience credit; degree met ⇒ full education credit). Sanofi "VMI Intern"
reached **80 = "strong match"** on a 4-skill requirement list. Not an
extraction defect — the bodies are real; the classification is wrong.

**Where it stopped.** The junk-token purge was implemented and being measured.
Its last recorded finding: *"The purge is a 113:1 trade, but it reveals the
named rows are mostly genuine."* Read that as: the purge helps, but the
remaining `fams=[]` rows are largely legitimate postings, so raising the
threshold may not be justified.

**Finish it by:**
1. Re-running the live measurement to confirm the 113:1 read and reporting how
   many of the 598 are demoted.
2. Taking a sample of `Analyst` / `Specialist` / `Engineer` postings **with
   real bodies** that are genuinely technical, and measuring how many the
   change demotes. This is the recall side and it is the reason the threshold
   was not touched.
3. Only tightening the `_AMBIGUOUS_TITLE_RE` + 2-hit clause if a measured gap
   remains AFTER the vocabulary purge. Bare nouns `manager` (119), `specialist`
   (65), `analyst` (62), `engineer` (52), `associate` (35), `lead` (30),
   `intern` (24), `director` (21) all match that regex and need only two hits.
4. Holding the error direction: a false `non_tech` removes a reachable job and
   is worse than admitting a marginal one.

**Acceptance:** `evals/eval_live_titles.py` scope accuracy must not regress
(0.979 baseline, and the miss list was byte-identical through the last change —
diff it). `evals/eval_tech_taxonomy.py` and
`evals/eval_fit_score_domains.py` stay green.

---

## B. Country normalization + feed location dimension — STOPPED MID-WORK

**Uncommitted:** `shared/python/services/location_country.py` (NEW),
`shared/python/services/tech_feed_service.py` (+107),
`evals/test_tech_feed_retrieval.py` (+19).

**The bug.** We cannot tell what country **71%** of postings are in. Measured
on live `job_postings`, active + `tech.tech_scope: core` (35,933 rows):

```
country_code == ""      25,562  (71.1%)
country_code missing       793  ( 2.2%)
no locations at all        765  ( 2.1%)
known: US 3,780 · IN 1,873 · CA 1,038 · GB 481 · IL 282 · SG 161 · MX 107 …
```

Only **3,780 are known-US**. The known distribution proves the rest are not
silently US — we simply never parsed them.

**The damage,** real top-of-feed results for a US-based candidate who needs
H-1B sponsorship:

```
91  Santander    "GERENCIA JR. DATA ANALYST ALM - RIESGOS"  [('CDMX', '')]
88  Michelin     "Digital Transformation - Internship"      [('Bangkok MSG Headquarters', '')]
93  Baker Hughes "Intern – Systems & Data Analysis (M/F/D)" [('DE-CELLE-BAKER-HUGHES-STRASSE 1', '')]
```

`fit_score._score_location` reads `unknown` and unknown is EXCLUDED rather than
penalised. **That rule is correct — do not change the scorer.** The data is
wrong.

**Finish it by:**
1. Completing `location_country.py`. The raw strings are parseable: `CDMX`,
   `Bangkok MSG Headquarters`, a German street address, and `(M/F/D)` /
   `(F/H/X)` title markers are all strong signals. Where a country genuinely
   cannot be resolved, leave it unknown — **never default to US**, which is the
   same error pointing the other way.
2. Backfilling. `scripts/backfill_posting_normalization.py` already exists.
   Read it first, and read the CLAUDE.md warning: it stamps
   `NORMALIZATION_VERSION` only when JD bodies were actually read
   (`--with-bodies`); stamping a half-upgraded row freezes it there.
3. Giving the feed a work-location dimension in `rank_score` — demote + badge +
   optional filter, the same shape as the work-authorization policy. Treat
   unknown-country conservatively; hard-filtering 71% of the corpus empties the
   feed. Keep it DISTINCT from sponsorship: "this employer will not sponsor"
   and "this job is in Bangkok" are different facts and must read differently.
4. **Never touch `match_score`.** Ordering/preference signals live in
   `rank_score` (ADR-002). There is exactly ONE match score.

**Acceptance (never delivered, still needed):** live before/after top-25 for
`cpulikan@asu.edu` with a location column, and the same for the
work-authorization demotion (employer, title, clearance flag, sponsorship
flag).

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
