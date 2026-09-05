# ATS assessment accuracy: per-requirement diagnostic

Date: 2026-09-05. **The screenshot’s exact saved pair was not available.** This uses a representative fixture (`SCREENSHOT_LIKE_*` in `tests/fixtures/assessment/cases.py`) that reproduces the verified matching defects.

Scored text (final structured resume, all visible sections):

```
Alex Rivera
Backend engineer who developed REST endpoints and documented them with OpenAPI.
Languages: Python, Java
Data: PostgreSQL
APIs: OpenAPI
Software Engineer / DataCo
Developed REST endpoints for billing and account services.
Published OpenAPI specifications for those services.
Tuned PostgreSQL queries for the reporting warehouse.
Payments sandbox
Certified AWS practitioner coursework applied to a sandbox API.
BS Computer Science / State University
AWS Certified Cloud Practitioner
```

JD required: `REST API Development`, `OpenAPI/Swagger`, `PostgreSQL`, `Java`. Preferred: `Kafka`. Keywords: `microservices`. Context (not required): `fintech`, `distributed systems`.

## Before (legacy `get_all_forms` + domain promotion)

Reproduced on this machine against the fixture, without changing any user record.

| JD text | Old forms searched | Found by old scorer | In required denominator |
| --- | --- | --- | --- |
| REST API Development | `rest api development` only | **No** | Yes (and would also add parked domain terms) |
| OpenAPI/Swagger | `openapi/swagger` only | **No** | Yes |
| PostgreSQL | postgres / postgresql / psql / … | Yes | Yes |
| Java | `java` | Yes | Yes |
| fintech, distributed systems | n/a | n/a | **Yes — promoted from `jd_context.domain`** |

Keyword frequency would have scored higher if `REST` or `PostgreSQL` were repeated. Gap reporting would have forced the two false misses into defensible/learnable/stretch.

## After (canonical matcher)

| JD text | Class | Canonical id | Aliases (abbrev.) | Matched span / section | Source | Method | Unmet | In required denominator |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| REST API Development | required | rest api development | REST API, rest, restful, rest apis, … | “developed REST endpoints…” / summary | supported | recognized_alias | — | yes |
| OpenAPI/Swagger | required | openapi/swagger | OpenAPI, openapi spec, swagger | “…documented them with OpenAPI.” / summary | supported | recognized_alias | — | yes |
| PostgreSQL | required | postgresql | postgres, psql, pg | Skills: PostgreSQL | supported | exact | — | yes |
| Java | required | java | Java | Skills: Java | supported | exact | — | yes |
| Apache Kafka | preferred | apache kafka | kafka, … | — | not_evidenced | none | — | **no** |
| Microservices | keyword | microservices | microservice, … | — | not_evidenced | none | — | **no** |
| fintech | context | fintech | fintech | — | not_evidenced | none | — | **no** |
| distributed systems | context | distributed systems | … | — | not_evidenced | none | — | **no** |

Required coverage on this fixture: **4 / 4**. Preferred Kafka remains a genuine optional gap.

## Reproduced defects vs suspected causes

**Reproduced on the fixture**

- Long JD phrases (`REST API Development`) did not expand to the REST alias family, so the resume’s “developed REST endpoints” was missed.
- Slash compounds (`OpenAPI/Swagger`) were searched as one literal token.
- Domain context could be promoted into the required denominator (code path in `_deterministic_scores`).
- Frequency counted repeats as extra credit (code path in `_keyword_frequency_score`).
- Scorer used `get_all_forms`; gap engine used `match_forms` — same JD term could disagree.

**Suspected for the screenshot, not replayed**

- Match-gap LLM forced every “missing” item into defensible/learnable/stretch even when its rationale admitted explicit experience.
- UI asked for evidence attachment on those cards.
- Detached hash covered only the summary (bullet-only edits could look unchanged).
- Source check used the generated resume when no independent source was supplied.

Not claimed: Jobalytics or any vendor score is ground truth. These rows are document coverage, not employer pass probability.
