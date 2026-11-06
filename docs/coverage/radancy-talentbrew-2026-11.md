# TalentBrew public-page evidence, 2026-11-05

Five known employers satisfy the `>=5 employers OR >=300 accessible US jobs`
value threshold. The reachable full US inventory was **not** measured; the
decision uses the five-employer threshold. Custom employer domains provide no
common vendor hostname suffix for a meaningful archive tenant count.

| Employer | Public entry | Advertised total | Advertised US | Rows read | Detail chars |
|---|---|---:|---:|---:|---:|
| Synopsys | https://careers.synopsys.com/search-jobs | 676 | 175 | 15 | 7,017 |
| Charter/Spectrum | https://jobs.spectrum.com/search-jobs | 1,458 | 1,458 | 15 | 4,731 |
| Optum | https://careers.unitedhealthgroup.com/optum-jobs | 5,933 | 5,575 | 15 | 5,263 |
| PG&E | https://jobs.pge.com/search-jobs | 149 | 149 | 15 | 7,993 |
| SolutionHealth | https://careers.solutionhealth.org/search-jobs | 510 | 510 | 15 | 2,228 |

All five registered-provider reads validated and were explicitly truncated.
Each first detail had a public `JobPosting` schema with a real `datePosted`.
Unknown listing dates and omitted employment types remain unknown. Underlying
schema employer labels are preserved as evidence; they do not grant ownership.
Catalog deployment starts crawling; owner-approved canonical source registration
is still separate and was not performed.

The official Optum page links `/optum-jobs`, which redirects to a search with
`custom_fields.UHGAJDType=Optum`; the returned checkbox is checked. The provider
requires that exact checked facet on every listing. It refuses an unscoped UHG
endpoint or a response where the scope disappeared.

The observed vendor script, `https://tbcdn.talentbrew.com/js/client/search.js`,
uses `/search-jobs/results` and `/search-jobs/resultspost`. Robots prohibit the
`/search-jobs/` prefix on Synopsys, Spectrum and UHG. Those attempted requests
were refused locally without issuing HTTP. Spectrum's literal public Next link,
`/search-jobs&p=2`, returned 302 to the homepage. The reader performs no Ajax or
invented pagination request. It marks partial coverage before yielding its
first row, preserving the crawler's stale-closure protection.

`live_provider_check.check("radancy_talentbrew")` returned `OK`, five jobs,
sample “Lead Cybersecurity Automation Engineer”. The onboarding validator
returned `budget_limited` for each board, accurately reflecting the incomplete
census. Both used same-turn public responses through the shared sequential,
robots-aware, no-retry probe transport. No database was opened or modified.

Fixtures contain three real cards per employer and public detail schemas for
three tenants. Provider, fingerprint, refusal, robots, missing schema, exact
entity scope, early truncation and detail tests are in
`tests/test_radancy_talentbrew_provider.py`.

Verification: 122 focused provider, registry, readiness and onboarding tests
passed on the reviewed implementation. The required Linux Python 3.12
`scripts/verify.py --full` was invoked on that source. Its last readable progress
was 52%; the WSL service then stopped answering new requests. No completed full
result is claimed. The draft PR must pass the repository's Linux full CI before
merge. A rebase onto current main is followed by the focused checks and the
new detail-revisit tests from main.

The Optum scope check requires exactly the Optum facet value and every card's
published Optum brand marker. Details retain each complete location, including
its country. The combined PR includes the Phase 2 worker mapping that preserves distinct
inventory locations. Review and verify the combined change in PR #40.
