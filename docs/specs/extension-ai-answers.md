# Extension AI answers: `POST /api/apply/answer` with citations

**Status:** added 2026-11-19 for the ApplyPilot extension's store release
("AI through Aspirely"). It answers the extension's backend requests BR-1 to
BR-4. Code: `services/jobs/blueprints/apply.py::generate_answer`,
`shared/python/services/answer_generator.py::AnswerGenerator._generate_cited`,
`shared/python/services/answer_citations.py`. Tests:
`tests/test_extension_ai_answers.py`.

## Why

The extension never fills a drafted answer it cannot verify. Every number,
employer, title, date, technology or credential in a draft must trace to a
fact the draft CITED; this is its local integrity check. Before this change,
`POST /api/apply/answer` returned `{answer, angle}` with no citations. So for a
store user with no OpenRouter key of their own, the extension had no AI path.

## Request

`POST /api/apply/answer`, JWT (`Authorization: Bearer`).

```json
{
  "question": "Why do you want to work at Acme?",
  "job_description": "…",
  "company": "Acme",
  "role": "Backend Engineer",
  "tone": "professional",
  "n_variants": 3,
  "facts": [
    {"id": "exp[0].title", "kind": "experience", "text": "Staff Engineer at Globex (2022 - Present)"},
    {"id": "exp[0].highlights[0]", "kind": "experience", "text": "Cut p99 checkout latency 40%."},
    {"id": "skill[0]", "kind": "skill", "text": "Python"},
    {"id": "jd.company", "kind": "job", "text": "Acme"}
  ]
}
```

Fields:
- `question` is required.
- `job_description`, `company`, `role` and `tone` are unchanged.
- `n_variants` is new and optional: an integer from 1 to 3, default 3.

`facts` is optional. Without it, the request is the old one, unchanged. With
it:
- **List size:** at most 150 facts.
- **`id`:** must use the extension's grammar, e.g. `pf.summary`,
  `exp[0].highlights[2]`, `edu[1].degree`, `proj[0].name`, `skill[7]`,
  `cert[0]`, `persona.emphasis[1]`, `jd.company`, `jd.role`. Ids must be
  unique.
- **`text`:** a string of at most 500 characters, sanitized server-side. An
  empty `text` is dropped.
- **Content:** at least one fact must be about the candidate (an id that does
  not start with `jd.`).
- **What the extension sends:** `buildFacts()` in
  `src/background/ai/grounded.ts`, which has already redacted the user's
  identifying values. The server redacts again for the model.

## Response 200

```json
{
  "variants": [
    {"answer": "…", "angle": "Specific project", "used_fact_ids": ["exp[0].title", "exp[0].highlights[0]"]}
  ],
  "usage": {"used": 3, "limit": 30},
  "grounding": "client_facts"
}
```

- `grounding` is `client_facts` when facts were sent and `resume` otherwise.
- Without facts, every variant carries `used_fact_ids: []`; nothing else
  changes.
- With facts, the model grounds ONLY in them. A parsed base résumé is not
  required.

A variant is dropped before it is returned when:
- it cites an id the client did not send (`unknown_fact_id`);
- it cites no fact about the candidate (`uncited`);
- it states a number that no fact it cited contains (`ungrounded_number`).
  Numbers count with `$`, `k`, `m`, `x`, `+` and `%`, plus years.

Drop reasons are logged as counts only, never the text. The extension still
runs its own fuller check (names, dates, technologies, credentials) on what it
receives.

## Errors (never 401 for a business failure)

| Status | `error` | When |
|---|---|---|
| 400 | `question is required` / `Invalid question` | as before |
| 400 | `invalid_facts` (+ `message`) | malformed `facts`; checked BEFORE any quota is spent |
| 400 | `no_resume` | old shape only: no facts and no parsed base résumé |
| 429 | `daily_quota_exceeded` (+ `usage`) | the `autoapply_answer` pool is empty; checked BEFORE the model call |
| 429 | rate / fill caps | as before (10 calls per 5 min per IP, plus per-user fill caps) |
| 502 | `no_grounded_answer` | facts sent, but every draft failed the checks (or the model failed). The extension routes the question to Needs-you. |
| 502 | `Failed to generate an answer…` | old shape, model failure |

Cost: one `autoapply_answer` unit per call, the same pool as before. Tailor
quota is never touched.

## Catalog ids shared with the extension (BR-1, BR-3, BR-4)

All three are accepted by `PUT /api/apply/onboarding/answers`. Their ids are
identical to the extension's.

| field_id | category | sensitivity | type | policy | aliases |
|---|---|---|---|---|---|
| `requires_sponsorship_future` | work_auth | legal | boolean | required_onboarding | the extension's set |
| `work_location` | screening | safe | text | from_profile | anchored only: `employer location`, `company location`, `location of (the) employer/job/position`, `work experience location` |
| `skills` | screening | safe | text | from_profile | anchored only: a label that is just `Skills` (optionally `technical/key/relevant skills`, `list your skills`) |

- `requires_sponsorship_future` sits AFTER `requires_sponsorship`. Every label
  that classified as `requires_sponsorship` before still does, so the server's
  classification is unchanged and auto-apply is unaffected.
- It is not in `READY_REQUIRED_FIELDS`, so no finished onboarding becomes
  unready.
- The extension's copies of `work_location` and `skills` carry no aliases.
  Here every field needs at least one (`test_catalog_is_comprehensive`), so
  each has anchored phrasings that can only mean that field. "Location",
  "Preferred work location" and "What skills would you like to develop?" never
  reach them (tested). The one server-side change is that a label that is
  exactly "Skills" now classifies as `skills` instead of `unknown`.
