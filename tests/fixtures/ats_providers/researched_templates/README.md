# Public fixture provenance

These are excerpts of public employer responses, captured with robots checks,
serialized requests and no browser session. HTML fixtures retain two job cards
and the first published paging link, except NRG, which retains its ten-row table
and published pagination metadata. Scripts, unrelated navigation and session
tokens were omitted. Fixture row counts are not board inventory counts.

| File | Public source | Captured at (UTC) |
| --- | --- | --- |
| `two_sigma.html` | https://careers.twosigma.com/careers/OpenRoles | 2026-11-05T23:21:08.992630Z |
| `ea.html` | https://jobs.ea.com/en_US/careers/SearchJobs | 2026-11-05T23:09:17.662104Z |
| `deloitte.html` | https://apply.deloitte.com/en_US/careers/SearchJobs?3_5_3=477%2C478%2C480&sort=relevancy | 2026-11-05T23:21:11.239184Z |
| `nrg_search.html` | https://careers.nrgenergy.com/search/?q=&sortColumn=referencedate&sortDirection=desc&startrow=0 | 2026-11-05T23:19:30.237245Z |
| `enphase_page.json` | https://enphase.com/api/v2/jobs | 2026-11-05T23:21:16.800426Z |

Enphase retains two original records and the original pager metadata (33 jobs,
seven per page). The tests explicitly change totals where a complete two-record
census is needed. Its endpoint was discovered through the employer's linked
`career-search-form.js`; no posting dates were supplied or invented.

Boundary tests mutate copies of these fixtures to model repeated pages, partial
responses, invalid paging links and a fourteen-page census. Those mutations are
synthetic tests and are not live employer measurements.
# Additional date evidence

`supermicro_undated.html` retains two real rows from
`https://jobs.supermicro.com/search/?q=&sortColumn=referencedate&sortDirection=desc&startrow=0`,
captured 2026-11-05T23:32:55.419881Z. The page supplies no posting dates;
the fixture retains that absence.


## Mott MacDonald

`mottmac_listing.json` retains two real records from the employer-published listing route (`https://www.mottmac.com/en-us/careers/explore-our-careers/listing/`), captured 2026-11-06. `mottmac_detail.html` retains the description section from the Civil Engineer 13569 posting. The served Jobs-MKG43EDE.js module publishes the offset, take, reset=false and UUID request-id contract. URL and exact UTC request evidence are recorded in `docs/coverage/research-evidence-2026-11.json`. No dates were present or invented.
