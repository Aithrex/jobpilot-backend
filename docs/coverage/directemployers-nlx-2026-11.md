# DirectEmployers NLX public-board evidence

The Stantec official careers chain links `stantec.jobs`. Its served Nuxt
modules publish `https://prod-search-api.jobsyn.org/api/v1/solr/search`, the
`X-Origin: stantec.jobs` header, and the US listing's `location=usa` query.
No credential, browser execution, database or guessed tenant was used.

At 2026-11-05 23:52:57 UTC, 13 permitted pages returned 650 distinct location
GUID rows containing 300 distinct employer requisition IDs. Every row printed
Stantec and United States. This met the requested 300 accessible US jobs gate.
An earlier exploratory request included `offset=0`, which the browser's
`filterFalsy` helper omits; it repeated page one and was stopped. Those repeated
rows are excluded from all counts.

The final registered-provider run at 2026-11-06 00:17:57 UTC read 56 pages:

| Measurement | Result |
|---|---:|
| Advertised US location rows | 2,760 |
| Distinct location GUIDs read | 2,760 |
| Distinct requisitions after location merging | 995 |
| Requisitions with explicit US country | 995 |
| Listing truncated | false |

The exact request is GET with `location=usa`, `page=N`, `num_items=50` and
`X-Origin: stantec.jobs`. The generic provider stores this proven employer and
country scope in a data table. It rejects mixed or missing company/country
markers, follows robots for both public hosts, and stops on challenge statuses
or bodies. The shared API receives one request at a time at least 1.05 seconds
apart. No detail request is needed because the listing includes the body.

Public detail URLs use the location, title slug and GUID exactly as the served
frontend does. `date_added` is the frontend's published JobPosting `datePosted`
field; missing dates remain unknown. Locations are merged by `reqid`, with each
complete place retained in `extra.locations`. Inventory's separate-place
normalization depends on the Phase 2 worker mapping change; merge that PR first.

Completion uses distinct GUIDs against the highest observed location total.
Missing rows, malformed IDs, overlap, a repeated page, page cap, deadline or
unverified response marks the listing partial before any job is yielded.
Only an explicit complete zero response is empty.

`live_provider_check.check("directemployers_nlx")` returned `OK` with five jobs,
sample “Project Manager, PD&E”. The actual onboarding validator returned
`budget_limited` with 400 requisitions because its probe has a separate cap.
Both checks replayed the same-turn guarded HTTP responses. Generated onboarding
docs preserve that cap; they do not substitute the full-reader count.

Three board samples were considered. Stantec is added. Burns & McDonnell's
literal NLX API request returned 504, leaving its inventory unknown. Southern
Company's official board returned an Incapsula challenge and was not retried.
Neither is seeded. Common Crawl queries were robots-refused; the bounded CT
service reached 429 and was stopped. Archive tenant totals remain unknown.

One initial registered Stantec attempt met a transient 504 on API robots and
sent no job request. A bounded subsequent attempt returned robots 404 and
completed the permitted census above. A robots failure is unknown, never zero.

Verification includes 28 provider and onboarding regressions, real trimmed
payload fixtures, and 162 focused tests across provider, registry, readiness,
onboarding and owed-detail paths. The exact `scripts/verify.py --full` command
was invoked with native Python 3.12; final results are recorded in the parent
delivery report and Linux CI remains required before merge.
