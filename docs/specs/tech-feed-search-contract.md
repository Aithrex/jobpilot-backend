# `/api/jobs/tech-feed` — the unified search contract

Implemented by `shared/python/services/tech_feed_service.py`, routed in
`services/jobs/blueprints/jobs.py`. Mounted as a **subpath of the existing
`/api/jobs` edge route**, so no Terraform change is required (CLAUDE.md
invariant 6).

All three endpoints require `@jwt_required()`. None is entitlement-gated —
browsing the market is not a paid feature today, and making it one is a
product decision (ADR-005 sec. 3).

---

## `POST /api/jobs/tech-feed/search`

Returns **200 with the result inline**. It is a bounded indexed read, not a
crawl, so there is nothing to wait on. It returns **202 `{job_id}`** only when
the body sets `"async": true`, and that job is polled through the EXISTING
`GET /api/resume/job/<id>` contract — no new poll endpoint.

It returns **410** `{"error": "cursor_expired", "message": ...}` when a
cursor's session has expired or belongs to another user. **Never 401** — the
frontend force-logs-out on any 401, and an expired scroll position must not
sign a user out.

Rate limit: 120 requests / 5 minutes per IP. Paging and filter changes are
normal interaction, not abuse; the crawl endpoints stay at 6 / 5 min.

### Request

Every field is optional. Anything unrecognised is **dropped**, not passed
through: only keys written by `normalize_request` reach a Mongo filter.

```jsonc
{
  // intent
  "query": "backend engineer distributed systems",
  "titles": ["Backend Engineer", "Software Engineer"],
  "role_families": ["software_engineering", "cloud_platform"],
  "role_specialties": ["backend", "sre"],       // implies its family
  "tech_scope": ["core"],                        // default ["core"]

  // skills
  "skills_required": ["python", "kubernetes"],
  "skills_preferred": ["terraform"],

  // exclusions - applied FIRST, unconditionally, never readmitted
  "exclude_skills": ["php"],
  "exclude_title_terms": ["staff nurse"],
  "exclude_keywords": ["unpaid"],
  "exclude_companies": ["Some Staffing LLC"],

  // geography
  "countries": ["us"], "regions": ["TX"], "cities": ["Austin, TX"],
  "radius_km": 50,
  "work_modes": ["remote", "hybrid"],
  "remote_eligible_locations": ["US"],
  "timezone_overlap_hours": 4,

  // role shape
  "seniority": ["senior", "staff"],
  "min_years": 5, "max_years": 10,
  "employment_types": ["full-time"],

  // compensation
  "min_salary": 180000,
  "salary_currency": "USD",
  "pay_interval": "year",
  "include_unknown_salary": true,        // DEFAULT TRUE

  // employer
  "employer_scope": "any",               // "any" | "tech_only"
  "employer_kind": "direct_employer",    // any|direct_employer|staffing|marketplace
  "company_ids": ["nvidia", "stripe"],   // employer_id OR ATS tenant slug
  "company_categories": ["semiconductor"],

  // sector - an ORDERING preference, not a filter. Default "software"
  // (software engineering + data + AI/ML + cloud). Ignored when the request
  // names its own role_families / role_specialties.
  "sector": "software",                  // "software" | "any"

  // work authorization + clearance  (added 2026-09-15)
  "work_authorization_status": "",       // "" = use the user's saved profile.
                                         // us_citizen | us_permanent_resident |
                                         // needs_sponsorship_now |
                                         // needs_sponsorship_future |
                                         // non_us | unknown
  "hide_sponsorship_restricted": null,   // null = use the saved toggle. OPT-IN.
  "hide_clearance_required": null,       // null = use the saved toggle. OPT-IN.

  // eligibility (legacy)
  "sponsorship_preference": "any",
  "work_authorization": ["us_citizen"],  // INERT - see unapplied_filters
  "requires_clearance": false,

  // freshness + apply
  "posted_within_days": 30,              // default 30, max 180
  "include_undated": true,               // default true
  "apply_mode": "direct_apply",          // "any" | "direct_apply"

  // output
  "sort": "relevance",                   // relevance | newest | salary
  "page_size": 25,                       // default 25, CLAMPED to 100
  "cursor": null,                        // when set, every other filter is ignored
  "personalize": true,
  "resume_id": null,
  "tz_offset_minutes": -420,
  "async": false
}
```

### Response (200)

```jsonc
{
  "search_id": "8f2c...e11",
  "jobs": [ /* see below */ ],
  "next_cursor": "eyJzIjoiOGYyYy4uLiIsIm8iOjI1fQ",
  "has_more": true,
  "total": 434,
  "facets": {
    "role_family":      [{"value": "software_engineering", "count": 196}, ...],
    "role_specialty":   [{"value": "general_software", "count": 173}, ...],
    "tech_scope":       [{"value": "core", "count": 434}],
    "seniority":        [{"value": "unknown", "count": 160}, ...],
    "work_mode":        [{"value": "unknown", "count": 386}, ...],
    "employer":         [{"value": "Northrop Grumman", "count": 20}, ...],
    "ats":              [...], "source": [...], "employment_type": [...],
    "salary_published": [{"value": "not published", "count": 353}, ...],
    "country":          [...], "employer_kind": [...]
  },
  "applied_filters": { /* the SERVER's normalized view - render THIS */ },
  "taxonomy_version": "tech-tax-1.0.0",
  "fit_score_version": "fit-v2.0",
  "feed_version": "tech-feed-1.0.0",
  "inventory_watermark": "2026-09-02T04:19:31.419Z",
  "coverage": {
    "as_of": "2026-09-02T04:21:38Z",
    "active_postings": 228146,
    "validated_endpoints": 2110,
    "known_employers": 82815
  },
  "source_health": {
    "sources": [
      {"source": "workday", "shards": 48,
       "last_success_at": "2026-09-02T04:17:04Z",
       "crawl_lag_minutes": 4, "stale": false, "retry_count": 0}
    ],
    "ingest_runner": {"last_heartbeat_at": "...", "minutes_ago": 1,
                      "healthy": true},
    "warnings": []
  },
  "warnings": [
    "More than 2000 postings match; showing the 2000 most recently posted."
  ],
  "broadening_suggestions": [
    {"label": "Include jobs without a published salary",
     "patch": {"include_unknown_salary": true}, "removed": 312},
    {"label": "Include adjacent technical roles (TPM, solutions engineering, IT, silicon design)",
     "patch": {"tech_scope": ["core", "adjacent"]}, "removed": 24}
  ],
  "diagnostics": {
    "retrieval_ms": 675, "candidates": 2000, "requirements_upfront": false,
    "filter_ms": 263, "after_filter": 454, "read_time_classified": 2000,
    "employer_resolve_ms": 47, "rank_ms": 4, "facet_ms": 2, "total_ms": 1873
  }
}
```

**Facets are computed over the FILTERED set, before pagination.** A facet
computed on one page is a lie about the result set; one computed before
filtering is a lie about what clicking it returns.

### A job card

```jsonc
{
  "job_id": "5c1f...", "dedupe_group_id": "a419b5c1...",
  "employer_id": "nvidia", "employer_name": "NVIDIA",
  "employer_tech_classification": "software",
  "employer_tech_evidence": [{"kind": "sic", "source": "sec_edgar",
                              "value": "3674 Semiconductors & Related Devices"}],
  "employer_kind": "direct_employer",

  "title_raw": "Senior Software Engineer, Deep Learning (Req 12345)",
  "normalized_title": "Senior Software Engineer, Deep Learning",
  "tech_scope": "core",
  "role_family": "ai_ml",
  "role_families": ["ai_ml", "software_engineering"],
  "role_specialties": ["ml_engineering", "general_software"],
  "occupation_codes": ["15-2051.00", "15-1252.00"],
  "taxonomy_version": "tech-tax-1.0.0",
  "classification_confidence": 0.95,
  "classification_evidence": [
    {"kind": "title_phrase", "field": "title", "value": "software engineer"},
    {"kind": "skills", "field": "description", "value": "pytorch, cuda"}
  ],
  "skills_required": ["python", "pytorch"], "skills_preferred": ["cuda"],
  "seniority": "senior", "required_years_min": 5, "required_years_max": null,

  "work_mode": "hybrid",
  "locations": [{"raw": "Santa Clara, CA, USA", "city": "Santa Clara",
                 "region": "CA", "country": "us"}],
  "remote_eligibility": null,
  "employment_type": "full-time",

  "salary": {"min_annual": 184000, "max_annual": 287500, "currency": "USD",
             "interval": "year", "display": "$184,000 - $287,500",
             "confidence": "exact", "published": true, "plausible": true},

  // What the POSTING said. `null` means it did not say - never "no".
  "sponsorship": null,                 // "offered" | "not_offered" | null
  "clearance_required": null,          // true | null  (never false)

  // What that means FOR THIS READER (added 2026-09-15). Present on every card.
  "work_authorization": {
    "reachability": "unlikely",        // "likely" | "unlikely" | "unknown"
    "badges": [{"key": "no_sponsorship", "label": "No visa sponsorship"},
               {"key": "clearance_required",
                "label": "Security clearance required"}],
    "reasons": ["This posting states it does not offer visa sponsorship"],
    "demoted": true,                   // it was pushed down in rank_score
    "penalty": 0.25,                   // the multiplier applied to rank_score
    "evaluated_against": "needs_sponsorship_future"
  },

  // Employer-level H-1B disclosure intel. `null` is the NORMAL answer and
  // means "we hold no data", NEVER "this employer does not sponsor".
  "sponsorship_signal": null,          // or the sponsorship_service object

  "source": "workday", "ats": "workday", "source_requisition_id": "JR1998765",
  "canonical_url": "https://nvidia.wd5.myworkdayjobs.com/...",
  "apply_url": "https://nvidia.wd5.myworkdayjobs.com/...",
  "direct_apply": true,

  "posted_at": "2026-08-29T00:00:00Z", "posted_at_confidence": "relative",
  "days_ago": 4,
  "first_seen_at": "...", "first_seen_days_ago": 4,
  "last_seen_at": "...", "last_verified_at": "...", "valid_through": null,

  "match_score": 78, "fit_score_version": "fit-v2.0", "fit": { /* ... */ },
  "rank_score": 0.6412,
  "why_matched": ["Your skills: python, pytorch", "Direct employer posting",
                  "Specialty match: ml_engineering"],
  "hard_constraint_mismatches": ["Missing: cuda"],

  "source_variants": [{"job_id": "...", "source": "career-pages",
                       "ats": "lever", "location": "Austin, TX",
                       "url": "...", "days_ago": 6}],
  "location_variants": [{"location": "Austin, TX", "job_id": "...", "url": "..."}],
  "duplicate_count": 2
}
```

Unknown is ALWAYS absent or explicitly `null`/`"unknown"`, never a placeholder
value. `salary.published: false` with null bounds means the employer did not
disclose — it does not mean the job pays nothing.

`salary.plausible: false` means the employer published something we could not
read as pay. Measured on live inventory, 2.4% of priced postings claim over
$1M/year and every sampled one is a parse failure — a page fragment read as
"$5 - $630 an hour", a number split mid-digit into "$123,500 a month". Such a
row is treated as UNKNOWN for filtering and sorting (so it cannot win a salary
sort or pass a floor) while `display` still shows the employer's own text. The
root cause is in `salary_normalizer` on the ingest path; this is the read-side
guard.

### Zero results

`broadening_suggestions` is computed by the SERVER from `filtered_out`, so it
names the filters that actually cut rather than guessing, and each entry
carries the exact `patch` a client applies. Filters that were SET but produced
no drop count are included too — a specialty that matches nothing is applied in
Mongo, so those rows never reach the post-filter stage, and the filter most
likely responsible is precisely the one with no evidence in the counts.

### Sample requests

```bash
# core-tech, anywhere, relevance
curl -sX POST "$API/api/jobs/tech-feed/search" -H "Authorization: Bearer $T" \
  -H 'content-type: application/json' \
  -d '{"tech_scope":["core"],"page_size":25}'

# SRE + backend, remote, $180k floor, published pay only, newest first
curl -sX POST "$API/api/jobs/tech-feed/search" -H "Authorization: Bearer $T" \
  -H 'content-type: application/json' -d '{
    "role_specialties":["sre","backend"],
    "work_modes":["remote"],
    "min_salary":180000,"include_unknown_salary":false,
    "sort":"newest","page_size":25}'

# AI/ML at technology companies only, excluding staffing
curl -sX POST "$API/api/jobs/tech-feed/search" -H "Authorization: Bearer $T" \
  -H 'content-type: application/json' -d '{
    "role_families":["ai_ml"],"employer_scope":"tech_only",
    "employer_kind":"direct_employer"}'

# page 2
curl -sX POST "$API/api/jobs/tech-feed/search" -H "Authorization: Bearer $T" \
  -H 'content-type: application/json' \
  -d '{"cursor":"eyJzIjoiOGYyYy4uLiIsIm8iOjI1fQ","page_size":25}'
```

---

## `GET /api/jobs/tech-feed/taxonomy`

`Cache-Control: private, max-age=3600`. Static for a given
`taxonomy_version`; revalidate when a search response reports a new one.

```jsonc
{
  "taxonomy_version": "tech-tax-1.0.0",
  "scopes": ["core", "adjacent", "non_tech", "unknown"],
  "families": [
    {"key": "software_engineering", "label": "Software Engineering",
     "scope": "core",
     "specialties": [
       {"key": "backend", "label": "Backend",
        "occupation_codes": ["15-1252.00"]},
       ...
     ]},
    ...
  ]
}
```

14 families (7 core, 7 adjacent), 78 specialties.

---

## Work authorization, clearance and aggregators (2026-09-15)

### The defect

Measured on live `job_postings`, active + `tech.tech_scope == "core"`
(31,220 rows): `requirements.clearance_required` is true on **3,004** and
`requirements.work_authorization_restricted` on **2,076**. Both are written at
ingest, and **nothing in retrieval or ranking read either of them**. RTX,
Northrop Grumman, Booz Allen, Leidos and GDIT held ~2,300 core postings between
them, most flagged, and they sat at the top of the feed for an F-1 student who
is categorically ineligible for them. `Jobgether` — a marketplace aggregator,
not an employer — was the single largest source of active core postings
(4,157) and led the unfiltered feed.

Measured for one live account before this change, top-25 of the default feed:
**four rows** stated a clearance requirement or "no sponsorship", **including
the first one**. After: **zero**.

### Filter or demote? DEMOTE, with an opt-in filter

The default is a hard demotion plus a visible badge. The hide-filters exist and
are **never on by default**.

* A default-on filter would silently delete reachable jobs. The flag is an
  extraction from free text, an employer's statement is often about a different
  requisition family, and defence primes do staff non-cleared programmes. The
  codebase rule is that a false-ineligible — removing a job the candidate could
  have taken — is the worst error available (`fit_score` holds its
  false-INELIGIBLE rate at zero for the same reason).
* Arithmetic alone is not a demotion. The marketplace penalty that has been in
  `rank` all along is `-0.6` on a component weighted `0.15`, i.e. 0.09 of
  `rank_score` — which is why Jobgether still led the page. So a DECLARED
  sponsorship need relegates an explicitly-restricted posting by a **sort
  tier**, below every reachable row. It is still in the result set, still
  counted in `total`, still reachable by paging.
* `unknown` is conservative, not punitive: **no** relegation, a mild
  multiplicative penalty (0.85 / 0.92), and the badge either way — "no
  sponsorship" is a fact about the posting regardless of who reads it.

### Where it lives

**`rank_score` and the new `work_authorization` block. `match_score` is
untouched and there is no second match formula** (ADR-002, CLAUDE.md).

### Sort keys, in order of authority

1. `fit_score`'s positive `ineligible` verdict (unchanged)
2. **work-auth tier** — the user declared a need and the posting states the
   opposite. Applied under `relevance`, `newest` and `salary`.
3. **marketplace tier** — an aggregator repost. `relevance` **only**: under an
   explicit date or salary sort the user asked for a date or a number, and
   pinning 4,157 rows behind everything would make that ordering meaningless.
4. `rank_score`, then `match_score`, then `job_id`

Marketplace rows also get a tighter employer-diversity cap
(`TECH_FEED_MARKETPLACE_PAGE_CAP`, default **1** per page versus 3 for a real
employer): three Jobgether cards is not three employers.

### The envelope block

Every page — first, session and keyset — carries:

```jsonc
"work_authorization": {
  "status": "needs_sponsorship_future",
  "source": "declared",          // declared | visa_profile | user_preferences |
                                 // request | none
  "declared": true,              // false => ASK. Render the question below.
  "needs_sponsorship": true,
  "clearance_eligible": false,   // true | false | null(unknown is a third answer)
  "hide_sponsorship_restricted": false,
  "hide_clearance_required": false,
  "question": "What is your work authorization in the United States?",
  "options": [{"value": "us_citizen", "label": "U.S. citizen",
               "help": "No sponsorship needed. Eligible for roles that require a security clearance."},
              /* ...six options, `unknown` last as "Prefer not to say" */],
  "version": "work-auth-1.0.0"
}
```

`declared: false` is the signal to ask, **once, in context**. The live account
this work started from had no `visa_profiles` row at all because onboarding
never asked. The options are served by the backend so a client cannot invent a
seventh answer the ordering does not model.

### Reading and writing the preference

| route | body / response |
|---|---|
| `GET /api/resume/visa/work-authorization` | the block above, flat |
| `PUT /api/resume/visa/work-authorization` | `{work_authorization?, hide_sponsorship_restricted?, hide_clearance_required?}` → `{ok: true, ...the block}` |
| `GET /api/resume/visa/profile` | now also returns `work_authorization` |
| `PUT /api/resume/visa/profile` | accepts the same three keys on the same body |

Stored on **`visa_profiles`**, additively, alongside the Visa Timeline
Copilot's own fields — not in a third home. A user who already told us their
F-1 status (`visa_status` ∈ `F1 / F1-CPT / F1-OPT / F1-STEM-OPT`) is derived as
`needs_sponsorship_future` and **is not asked again**; `H1B` derives
`needs_sponsorship_now`; the legacy `user_preferences.requires_sponsorship`
boolean is honoured last. `inventory_feed.requires_sponsorship` now delegates
here, so the tech feed, the legacy tabs and the auto-apply answer bank read one
fact.

An unrecognised value normalizes to `unknown` rather than 400: the safe wrong
answer is "we do not know", never a status the user did not pick.

### Employer display names

Three JPMorgan/Boeing legal-entity shells held **681** active core postings and
were rendering as the employer name: `NUTMEG CONTAINER CORPORATION`,
`CHASE FAMILY LIMITED PARTNERSHIP NO. 5`, `M.J. WILLIAMS COMPANY`.
`employer_display` now applies, in order: the curated
`employer_registry.IDENTITY_OVERRIDES` `wrong_names` correction (an in-process
dict, zero queries, and it has named all three all along); then a confident
`employer_identity` resolution; then `canonical_employer_id ->
employers.display_name`, but **only when the name we hold is a registry
shell** — measured unguarded it made 77 substitutions on a live page and
several were worse (`Johnson & Johnson` → `JNJ CORPORATION`), because the
registry inherited its name from the same `companies` scrape.

`employer_id` is **never** rewritten; three production indexes key on it. The
previous name is preserved on `employer_name_original` and
`employer_name_variants`, and `employer_name_source` says which rung answered
(`identity_override`, `canonical_registry`, `curated_catalog`,
`tenant_slug_affinity`, `sole_candidate`).

The canonical-name lookup is cached for the process
(`EMPLOYER_DISPLAY_CACHE_TTL`, default 900 s). Measured on a 2,000-row live
candidate set: **447 ms cold, 36–48 ms warm** — 3 ms of it server-side, the
rest round-trip.

### Sponsorship intel on the card

`sponsorship_signal` carries `sponsorship_service`'s scored object verbatim —
its `headline`, `summary`, `source`, `data_vintage`, `band` and its mandated
`disclaimer`. It is a **trust feature and never a prediction**: it reports what
an employer has done in public DOL/USCIS disclosure data and says nothing about
an individual's odds. Batched — **one** `$in` on `sponsorship_signals` for the
~20 employers on a page, never one lookup per row — and computed only for the
page being serialized, not for the 2,000-row candidate set.

**`null` means "we hold no data", never "this employer does not sponsor."** A
client must not render an absence as a negative.

#### The provenance guard

`sponsorship_signals` holds 43,561 rows from the USCIS H-1B Employer Data Hub
covering **one fiscal year (FY2026)**. `sponsorship-ingest-2.0.0` made
`sponsorship_service` read each row's own `data_window` / `source_label`, so a
card correctly reads:

```
headline   "Sponsored 13,069 people in similar roles in FY2026"
source     "USCIS H-1B Employer Data Hub"
disclaimer "... covering FY2026. It is NOT a prediction of your individual
            outcome, is not legal or immigration advice, ..."
```

That framing is passed through **verbatim** — this feed reproduces none of the
copy, so there is exactly one place it can be wrong.

A row written **before** provenance existed has no `data_window`, and
`window_of` then falls back to `DATA_WINDOW_YEARS = 5` and a `SOURCE_LABEL`
naming DOL. Measured on such a row the object reads *"Sponsored 10 people in
similar roles in the last 5 fiscal years"* from *"DOL OFLC LCA Disclosure Data +
USCIS H-1B Employer Data Hub"* — five years of two datasets we do not have. The
feed **withholds** those and reports it as
`diagnostics.sponsorship_suppressed_window`.

The check is on `signal.data_window` alone and costs **no extra query**. It
cannot key on `data_vintage` or `source`: both are populated from the fallback
constants and are precisely the claim being refused. It is self-healing — a
re-ingest that stamps provenance lights the row up by itself, with nothing to
flip.

---

## `GET /api/jobs/tech-feed/coverage`

`{coverage, source_health}` with the same shapes as in the search envelope.
Separate so the UI can render an honest "what you are not seeing" banner
without paying for it on every search.

---

## Invariants a client may rely on

1. **Exclusions can only remove.** A term in `exclude_*` can never cause a job
   to appear, regardless of what else is set.
2. **Unknown never silently excludes.** Unknown salary, unknown posting date,
   unknown seniority and unknown work mode all pass every filter unless the
   caller opts out explicitly.
3. **`match_score` is `fit_score`.** With no parsed resume it is `0` and
   `fit_score_version` is `null`. `rank_score` is a different number and is
   not a match claim.
4. **Pages are stable.** For one `search_id`, no job appears on two pages and
   none is skipped, under the `inventory_watermark` the first page recorded.
5. **Paging costs nothing.** A cursor request performs no retrieval, no
   ranking, no classification and no crawl (`diagnostics.served_from:
   "session"`).
6. **`applied_filters` is authoritative.** Render it, not the request body:
   invalid values were dropped and a specialty may have implied a family.
7. **A demotion never removes a posting.** The work-auth and marketplace tiers
   reorder; they do not filter. `total` is unchanged, the row is still on a
   later page, and only the explicit `hide_*` toggles remove anything.
8. **A `null` is never a "no".** `sponsorship: null` means the posting did not
   say. `clearance_required` is `true` or `null` and never `false`.
   `sponsorship_signal: null` means we hold no disclosure data for that
   employer. Rendering any of these as a negative is a contract violation.
9. **The employer name may change between a card and the posting's
   `employer_id`.** `employer_id` is a legacy slug of whatever name the board
   printed and is never rewritten; the displayed `employer_name` is resolved,
   and `employer_name_original` / `employer_name_source` say so. Never display
   a raw id.
