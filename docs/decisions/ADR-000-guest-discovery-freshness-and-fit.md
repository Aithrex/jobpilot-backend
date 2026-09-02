# ADRs: Guest discovery, freshness, fit scoring, and identity

Date: 2026-07-21
Status: Accepted
Scope: jobpilot-backend + jobpilot-frontend

These ADRs were written after a root-cause audit of the guest path. Every
"Context" claim below carries a file:line reference to the code as it existed
before this change, so the reasoning can be re-checked later.

---

## ADR-001: Guest previews enforce a 7-day server-authoritative freshness gate

### Context

The only job-age constant in the pipeline was `WINDOW_DAYS = 30`
(`shared/python/services/workday_jobs_service.py:107`), imported verbatim by
`career_pages_service.py:34` and `mnc_jobs_service.py:53`.

`guest_service.py` applied no age filter at all. It inherited whatever the three
feeds returned and used recency only as a tiebreak. That is the direct cause of
the observed 26-day-old Workday result being presented to a guest as a match.

Three additional vectors inflated apparent freshness:

1. Greenhouse fell back to `updated_at` when `first_published` was absent
   (`career_pages_service.py:197`). A months-old requisition touched by a
   recruiter today reported `days_ago = 0`. Greenhouse is 686 of the 1309
   career-page companies, making this the single largest source of false
   freshness in the product.
2. Uber fell back to `updatedDate` (`career_pages_service.py:534`).
3. Eightfold fell back to `t_update` / `t_create` (`career_pages_service.py:405`).

Unknown dates were already handled correctly: `_days_ago` returns `None` and the
row is dropped (`workday_jobs_service.py:497-501`, `:1317-1319`). No code
substituted `now()` for an unknown date. The one place `now()` is used
(`workday_cxs_fetcher.py:355-371`) maps genuine relative signals ("today",
"yesterday") and returns `""` for anything unparseable, which is the safe branch.

### Decision

Guest previews enforce `GUEST_MAX_AGE_DAYS = 7` (env-overridable) applied in
`guest_service` **before scoring**, and **re-applied on every cache and snapshot
read** so a stored snapshot cannot serve a job that has since aged out.

Rules:

- Maximum age 7 calendar days.
- The date must be known and server-validated. Unknown-date jobs are excluded.
- Closed or inactive jobs are excluded.
- An unknown posted date is never replaced with the current timestamp.
- Results are never padded to 5. Fewer than 5 valid jobs is a success.
- Freshness is displayed on every guest card.

The `updated_at` / `updatedDate` / `t_update` fallbacks are removed. When a
source does not publish a real posting date, the date is UNKNOWN and the row is
dropped, rather than being silently promoted to "posted today".

### Consequences

Guest result counts will drop, in some cases substantially, because the previous
counts were partly manufactured by stale reqs presenting as fresh. This is the
intended correction: the product's central honesty claim is that these are real,
current openings. Showing three true matches beats showing five where two are a
month old.

`WINDOW_DAYS = 30` is retained for the authenticated feed surfaces, which are
browse-oriented rather than a freshness promise. The guest gate is strictly
tighter and independent.

---

## ADR-002: One shared, versioned, explainable fit score

### Context

`services/jobs/services/jd_match_scorer.py:115` computed:

```
denom = max(min_resume_skills, min(len(resume_skills), max_denom_skills))
raw   = (len(matched) / denom) * 100.0
```

with `min_resume_skills = 8`, `max_denom_skills = 14`. The numerator was built by
scanning the **resume's own skills** against the JD as a haystack
(`:106-113`). The job description was never parsed for what the employer
actually required.

Two consequences followed:

- The score measured "how much of this resume appears in this posting", not
  "how well does this candidate fit this job". A resume listing 8 skills and a
  posting mentioning 4 of them scored 50, whether the posting asked for 4
  things or 40.
- With the denominator clamped to at most 14, each matched skill was worth
  exactly 7.14 points, so the 0-100 scale had only 15 reachable values. Ties
  were pervasive, which in turn made the recency tiebreak (ADR-001) almost
  never fire.

Separately, two incompatible scorers operated on the same rows:
`workday_jobs_service._score_jobs:908-967` selected and ordered candidates, then
`guest_service.score_one:1552` overwrote the score. Selection and presentation
disagreed.

### Decision

A single versioned engine, shared by guest and authenticated paths, returns:
`score, band, confidence, score_version, component scores, evidence, strengths,
missing requirements, hard constraints, explanation`.

Displayed alignment components (100 total):

| Component                            | Points |
|--------------------------------------|--------|
| Role / title alignment               | 25     |
| Required skill coverage              | 30     |
| Relevant experience and seniority    | 20     |
| Preferred skills                     | 10     |
| Domain / industry alignment          | 5      |
| Location / work-mode compatibility   | 5      |
| Education / certification / license  | 5      |

Rules:

- **The required-skill denominator comes from the job description, not the
  resume.** This is the core correction.
- Required and preferred requirements are scored separately.
- Skill evidence inside work or project experience outweighs a bare
  skills-list mention.
- Experience is computed from dated evidence. Missing dates are not invented.
- Unknown information stays unknown rather than automatically failing.
- Explicit hard gates or score caps for mandatory licenses, clearance, work
  authorization, severe experience gaps, incompatible location, and unrelated
  roles.
- Scoring is deterministic and rule-based at request time. No Bedrock call in
  the matching request path. Expensive requirement extraction may run
  asynchronously once per changed JD, cached by content hash.
- Freshness, source quality, and employer diversity live in an internal
  `rank_score` and must never inflate the displayed candidate-job fit score.
- IntegrityGuard is preserved. No unsupported resume facts are ever inferred.

### Consequences

Scores change meaning, so `score_version` is mandatory on every stored and
served score. Snapshots carrying an older version are re-scored or invalidated
rather than compared against new ones.

---

## ADR-003: Canonical job inventory on EventBridge, NOT SQS

### Context

There is no canonical job inventory today. There are five mutually-unaware
per-query caches: `jobs_cache`, `workday_jobs_cache`, `career_pages_cache`,
`mnc_pages_cache`, `apify_actor_cache`. The same posting is stored once per
distinct query hash, with no stable identity and no cross-user reuse.

The cache key is derived from the search terms
(`workday_jobs_service._cache_key:1046-1055`), and those terms include up to two
resume-skill-derived entries. So the key is effectively resume-specific: two
engineers with identical titles but different top skills each pay a full scan.
The cache is too specific to amortize the fan-out and not specific enough to be
a per-user cache.

The fan-out itself: `WORKDAY_TENANT_CATALOG` holds 1013 tenants
(`workday_tenant_catalog.py:18`), multiplied by up to 6 terms as a full cross
product (`workday_jobs_service.py:1194`), for up to 6,078 HTTP tasks through 32
workers against a 110-second budget. Roughly 9% of tenants are reached before
timeout, and which 9% is nondeterministic. Adding career pages (1309 companies)
and MNCs (69) puts a single anonymous request at roughly 7,400 outbound requests
to about 1,100 third-party hosts.

The original brief specified EventBridge -> SQS listing/detail queues -> bounded
Lambda workers.

### Decision

Build the canonical inventory, but **schedule it on the existing EventBridge
pattern rather than introducing SQS.**

`infra/terraform/schedulers.tf:12-36` already defines nine EventBridge rules
targeting the resume Lambda with `{"scheduled_job": "<name>"}`. Source shards
become additional entries in that same `local.scheduled_jobs` map. Bounded
concurrency, leases, watermarks, and idempotent writes are implemented in
MongoDB (`job_source_state`) rather than in queue semantics.

Rationale: the explicit constraint is that infrastructure cost must not
increase. Additional EventBridge rules against an already-deployed Lambda are
effectively free. SQS would add queue, DLQ, and per-message costs plus new IAM
and Terraform surface for a workload that is scheduled and low-frequency, not
bursty and event-driven.

### Consequences

Accepted trade-offs versus the queued design:

- No SQS DLQ. Failure isolation relies on `job_ingestion_runs` records plus
  per-shard retry/backoff state in `job_source_state`.
- No partial-batch response semantics. A shard is the retry unit, so shards
  must stay small enough that re-running one is cheap.
- No queue-level backpressure. Concurrency is bounded by shard size and
  schedule spacing instead.
- Lambda self-invocation remains the async dispatch mechanism, so at-least-once
  delivery must still be assumed and every write must stay idempotent.

If ingestion volume later becomes bursty or per-shard retries prove too coarse,
revisit SQS. This decision is a cost trade, not an architectural preference.

---

## ADR-004: Paid sources carry cost_class and are excluded from guest previews

### Context

No `cost_class` concept existed anywhere in the repository. A repo-wide grep
returned zero hits.

`mnc_jobs_service._fetch_aggregator:241-257` calls JSearch on RapidAPI, which is
metered. It is reachable on the anonymous guest path via
`guest_service.py:1142-1147`. The only mechanism that removed aggregator rows
was a missing API key (`mnc_jobs_service.py:344-350`).

`JSEARCH_API_KEY` is present in the deployed configuration. So the guest path's
paid-API safety was "we happen to have not configured the key", and the key was
in fact configured. Every anonymous guest match run was firing metered RapidAPI
calls, with no per-guest attribution and no cost breaker other than a
platform-wide daily run counter.

### Decision

Sources are tagged `cost_class` of `free` (direct ATS) or `paid`
(aggregator / metered). Guest discovery excludes `cost_class=paid` sources
**before any fetch**, structurally rather than by configuration accident.

### Consequences

Guest coverage narrows to direct sources. This is aligned with the product claim
of direct-source openings, and it removes an unbounded, unattributed billing
path from an unauthenticated endpoint.

---

## ADR-005: Guest identity is abuse-resistant, not provable

### Context

Quota reservation is genuinely atomic. `try_reserve_tailor:564-633` is a single
`find_one_and_update` carrying both the emptiness condition and the limit check
in the filter, incrementing in the same operation, with status-guarded
compensating decrements. This is correct and is retained unchanged.

The weakness is not atomicity, it is identity. The atomic slot hangs on
`quota_subject_id`, and a fresh subject is minted per session
(`create_or_get_session:301`). Subjects merge only via
`guest_identity_service.resolve:272-303`, which requires at least two distinct
signal types including one strong type (email / phone / resume-signature HMAC).
Those HMACs require `GUEST_SIGNAL_HMAC_KEY`; with it unset the code fails closed
and `resolve` always returns `None`. `GUEST_IP_HASH_SALT` fails closed the same
way, disabling IP correlation.

Layered on top: the rate limiter is an in-process dict
(`shared/python/utils/security.py:229-239`), so on four independently-scaled
Lambdas the effective limit is containers x limit. And
`guest_challenge_service` is a no-op by default with **no verification function
at all** - nothing anywhere accepts or validates a solved challenge token.

### Decision

- Keep the atomic reservation exactly as it is.
- Identity resolution uses privacy-preserving HMACs of strong normalized signals
  (resume email / phone when present) plus server-issued session and device
  signals. Never merge on name alone. Never merge on a weak fingerprint alone.
  At least two signal types including one strong signal are required.
- Rate and quota buckets move to distributed MongoDB atomic operations. The
  in-process limiter is not a security control and must not be relied on.
- A challenge is only meaningful if a response is verified. Either implement
  real server-side verification with replay protection, expiry, and one-time
  semantics, or do not present a challenge.
- The required keys (`GUEST_SIGNAL_HMAC_KEY`, `GUEST_IP_HASH_SALT`) are
  deployment prerequisites, not optional hardening. Their absence silently
  disables identity resolution.

### Documented limitation

Anonymous identity can be made abuse-resistant but cannot be proven without
authentication. A sufficiently motivated actor who clears storage, rotates IPs,
and varies resume signals can obtain additional trials. The goal is to make this
cost more than the trial is worth, not to claim it is impossible. Any statement
that guest trials are strictly one-per-person would be false.
