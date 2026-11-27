# Candidate brief

Status: built 2026-11-26. Service `shared/python/services/candidate_brief.py`,
routes in `services/jobs/blueprints/apply.py`.

## What it is

One markdown profile per user, built from what Aspirely holds that a model may
see, so every AI answer starts from the same, complete picture of the person.

Sources:

- the active parsed resume (`resume_access.get_active_structured`): summary,
  experience and bullets, education, projects, skills, certifications;
- declared application answers in `user_preferences`, an allow-list only:
  work authorization, sponsorship now and future, visa status, relocation,
  remote preference, notice period, start date, travel, current location,
  desired salary (only when set), profile links.

Never included: EEO and demographics (gender, race, Hispanic or Latino,
veteran, disability, pronouns), legal attestations, criminal history, contact
details (email, phone, street address), current salary, passwords, tokens,
anything from Gmail. A defence-in-depth deny-list backs the allow-list.

Every bullet is a fact with a stable id under `srv.` (for example
`srv.exp[0].title`, `srv.exp[0].highlights[2]`, `srv.auth.sponsorship_now`),
valid under the citation id grammar in `answer_citations.FACT_ID_RE`.

## Storage

`candidate_briefs` `{_id: email, md, facts, hash, version, sources_hash,
built_at}`. The read path hashes the sources and rebuilds only when that hash
changes; otherwise it is one Mongo read.

## Contract

`GET /api/apply/brief` (JWT) returns `{md, facts, hash, built_at}`; 404
`no_profile` when the user has neither a parsed resume nor declared answers.

`POST /api/apply/answer` with client `facts`:

- the brief's facts are merged after the client's (dedupe by id and text);
- the prompt puts the fixed instructions, rules and facts FIRST and the job
  and question LAST, so Vertex AI's implicit context cache serves the shared
  prefix (`cached_tokens` in the `llm_usage` log line shows hits);
- answers are cached in `answer_cache` (30-day TTL on `created_at`) keyed by
  sha256 of email, brief hash, normalized question, company, role and the
  exact facts. A hit returns at once with `cached: true`, spends no
  `autoapply_answer` quota, no fill cap and no model call;
- the response adds `cached`, `brief_hash`, and `cited_facts` (`{id: text}`
  for every `srv.*` fact a returned variant cites) so the extension can verify
  a citation to a fact it never sent.

Requests without `facts` keep the old resume-grounded contract.
