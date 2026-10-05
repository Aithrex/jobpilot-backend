# Tailoring history pages

`GET /api/resume/tailoring-history` returns one authenticated user's history
page. Deploy the resume backend before the frontend that calls this endpoint.
It uses the existing `/api/resume/{proxy+}` gateway route.

- `page_size`: 1–100, default 50. Responses also stop near 2 MiB.
- `cursor`: opaque continuation from `next_cursor`; omit for a fresh search.
  A cursor is signed and bound to the user, filters and sort order.
- `q`: literal, case-insensitive substring search across role, company and
  required skills. `company` and `role` narrow their respective fields.
  Each text filter is limited to 200 characters.
- `status`, `date_from`, `date_to` (inclusive UTC dates, `YYYY-MM-DD`),
  `ats_min` (0–100), and `multi_version=true` are combined with AND.
- `sort`: `newest` (default), `oldest`, or `ats`. Date and record ID break ties;
  unscored records follow numeric scores when sorting by ATS.
- `preset`: `week`, `month`, `highAts`, `needsWork`, `multiVersion`, `offers`,
  `interviewing`, or `pending`. Relative time filters keep the first page's
  reference time across cursor requests.

The response includes `records`, filtered `total`, and `next_cursor` (null at
the end). First pages also include `summary.total` and `summary.presets` for
the user's entire history, independently of active filters. These are database
counts, not counts derived from the currently loaded page. Documents and full
assessment reports stay in the existing authenticated record-detail endpoint.

The browser fetches one page initially and another only on **Load more**. Search
changes replace the loaded results. The workspace overview separately requests
only five recent records and uses the server's total. Other legacy consumers
retain their existing list API.

The `user_email_history_created_record` compound index in
`scripts/migrate_indexes.py` supports the owner/date/record-ID ordering. Ensure
that index in the target database when rolling out this change. Text substring
search and score sorts still inspect matching user metadata; counts are not
cached. Queries have a ten-second database execution limit.

Keyset pagination avoids offset drift from new records inserted before a page.
It is not a transaction snapshot: records edited or deleted while browsing can
change the total and ordering. Refresh starts a new traversal.

Regression coverage: `tests/test_tailoring_history_pages.py` and the frontend's
`tailored-history.test.tsx`. Run both repositories' full verification gates
before publishing; the normal CI deployment gates still apply.
