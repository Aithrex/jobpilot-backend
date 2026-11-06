# Public employer template fixtures

Captured read-only from the employer-linked public URLs below on 2026-11-05 UTC. These are excerpts used for deterministic offline tests; they are not production inventory or claims that an excerpt is the whole board.

- `kpmg_listing.html`: two original job cards and the original pagination control from https://www.kpmguscareers.com/job-search/ at 23:40:46.175809Z. The full page advertised 923 results; a synthetic labeled-counter regression preserves that shape separately.
- `kpmg_page2.html`: two original cards and pagination from https://www.kpmguscareers.com/job-search/?spage=2 at 23:47:26.267246Z. The literal public child-theme script at https://www.kpmguscareers.com/wp-content/themes/understrap-child-main/js/child-theme.min.js?ver=1788286300 (23:45:25.698960Z) maps `data-href` to the `spage` query parameter. The provider requires this mapping explicitly in endpoint configuration.
- `kpmg_detail.html`: original JSON-LD from https://www.kpmguscareers.com/jobdetail/?jobId=139724 at 23:42:58.609430Z. The source contains literal newlines in JSON string values and escaped HTML; the fixture retains them.
- `deel_flight.html`: four genuine job records from the public Next flight data on https://www.deel.com/careers/ at 22:57:08.696044Z. JSON was reserialized into the original flight-record shape for a small fixture. The original `jobsCount` of 251 remains, so the cropped fixture must be partial. All IDs, published detail/apply URLs, dates, locations and titles are copied from the source, including legitimate titles ending in “Benefits” and “Team”.
- `shopify_listing.html`: the 25 visible role links and two marketing links from https://www.shopify.com/careers at 22:55:32.199034Z. The latter both point to `/careers/extraordinary` and must not become jobs. Role links retain their `aria-labelledby` title structure.

Tests also mutate copies to simulate repeated IDs, shrinking counts, missing pages, and budget exhaustion. Those mutations are regression scenarios, not live observations. No JavaScript is executed, no applicant data is included, and no network is used by the tests.
