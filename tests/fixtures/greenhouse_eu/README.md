Trimmed captures of Cognite's public EU Greenhouse board, fetched 2026-10-21
with the crawler's User-Agent at <= 1 request/second:

- `cognite_board_page1.html` <- https://job-boards.eu.greenhouse.io/cognite
- `cognite_board_page2.html` <- ...?page=2
- `cognite_board_page3_empty.html` <- ...?page=3 (past the end: 200, `data: []`)
- `cognite_job_4969944101.html` <- .../cognite/jobs/4969944101

Only `window.__remixContext` is kept. Real field names and values; the job
lists were cut to 3 + 1 rows and `count/total/total_pages` edited to agree
(4 rows over 2 pages). The live board held 51 jobs over 2 pages that day.
`boards-api.eu.greenhouse.io` did not resolve and `api.eu.greenhouse.io/v1/...`
answered 302 to the recruiter sign-in, so the board page IS the public EU API.
