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

  // eligibility
  "sponsorship_preference": "any",
  "work_authorization": ["us_citizen"],
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

  "sponsorship": null, "clearance_required": null,

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
