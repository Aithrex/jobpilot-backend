# The JD dictionary (`jd-1.0.0`)

`shared/python/services/jd_profile.py` is the one place a job description is
read. Filtering and tailoring both go through it, so a posting cannot read one
way in the feed and another way in tailoring.

| Path | Entry point | Where the result lives |
|---|---|---|
| Ingest, feed, match score, apply sweep | `fit_score.extract_requirements` calls `jd_profile.read_fields` | `job_postings.requirements.jd` |
| Every tailoring flow | `ResumeService.extract_jd` calls `jd_profile.attach_to_analysis` | `jd_analysis.jd_profile` |
| Anything holding a JD | `jd_profile.build(jd_text, title, analysis=None)` | Return value |

All readers are pure: no network, database or model. They cost about 19 ms per
JD including the existing skill extraction. An absent fact is `not_stated`, never
a guess, and a model is never needed to produce the dictionary.

## The dictionary

| Section | Key | Values |
|---|---|---|
| `role` | `title`, `company`, `location` | Text; company and location need the LLM analysis |
| | `family`, `level`, `work_mode`, `industries` | From `fit_score` |
| | `employment_type` | `full_time`, `part_time`, `contract`, `temporary`, `internship`, `not_stated` |
| `experience` | `min_years`, `max_years` | Number or null |
| | `evidence` | Source sentence |
| `education` | `min_level`, `preferred_level` | 0 not stated, 1 high school or associate, 2 bachelor, 3 master, 4 doctorate |
| | `min_degree`, `preferred_degree` | The same levels by name |
| | `fields` | For example `computer science` |
| | `equivalent_experience_accepted` | True or false |
| | `evidence` | Source sentence |
| `skills` | `required`, `preferred` | Canonical names; the LLM's are added when present |
| | `technical`, `soft` | The same skills split by kind |
| | `alternatives` | Groups the posting accepts instead of each other |
| | `certifications` | For example `CISSP` |
| `work_eligibility` | `sponsorship.status` | `offered`, `not_offered`, `conditional`, `not_stated` |
| | `sponsorship.evidence` | Source sentence |
| | `authorization.requirement` | `us_citizen`, `us_citizen_or_permanent_resident`, `authorized_to_work`, `not_stated` |
| | `authorization.evidence` | Source sentence |
| | `restricted` | The flag the feed and scorer read |
| | `clearance_required`, `clearance_level` | `ts_sci`, `top_secret`, `secret`, `public_trust`, `export_controlled`, `unspecified`, `not_stated` |
| | `licenses` | For example RN or CPA |
| `compensation` | | `salary_normalizer` output or null |
| `content` | `responsibilities`, `qualifications`, `keywords` | LLM only; empty without it |
| `provenance` | `deterministic`, `llm`, `skills_source`, `text_length` | How this reading was made |

The stored block drops the evidence quotes except sponsorship, because it sits on
every posting and the cluster is small. `build` re-reads them from the body.

## Rules that are easy to break

- **"Must be authorized to work in the US" is not `not_offered`.** Employers who
  sponsor say it too. It is `authorization.requirement = authorized_to_work`.
- **A negative statement outranks a positive one**, and a sentence containing a
  negation can never produce `offered`.
- **`conditional` is not an offer.** The feed shows no badge for it.
- **An unanswered form label is `not_stated`.** "Visa sponsorship?" followed by
  another field says nothing.
- **`min_level` is the lowest level accepted as a requirement.** "Bachelor's
  required, Master's preferred" is 2, and "Master's preferred" alone is 0.
- **`skills.alternatives` is reported, not applied.** `fit_score` still counts
  each alternative as required.

The résumé side keeps `fit_score._DEGREE_LEVELS`: on an education line a bare
"MS" really is a degree. A whole posting is full of "MS Teams", "Bethesda, MD"
and "scrum master", which is why JDs use the stricter readers here.

## Accuracy

`evals/data/jd_answer_key.jsonl` holds 300 real JDs, none from the set the readers
were written against. They were labelled blind, from the JD text alone, and half
are held out as `test`. Half of each split is random; the rest were picked for
eligibility wording. `evals/eval_jd_answer_key.py` scores it and refuses to print
`test` errors, and `evals/test_jd_answer_key.py` is the CI gate.

**The labels are AI-drafted until a person reviews them** (`review` on each row).
`evals/data/jd_answer_key_review.csv` lists the rows worth checking.

Held-out `test` split, 150 JDs, compared with the code before this module:

| Field | Before | Now |
|---|---|---|
| Stated sponsorship refusals caught | 18 of 28 | 28 of 28 |
| `sponsorship` status | not measured | 98.7% |
| `education.min_level` | 8% | 84% |
| `experience.min_years` | 78% | 85% |
| `authorization` | not measured | 93.3% |
| `clearance_required` | not measured | 96.7% |
| `work_mode` | not measured | 73% |
| `employment_type` | not measured | 77% |
| Skills | not measured | 48% precision, 20% recall |

Skills are the weak field: the lexicon cannot name most of what a JD lists, and
two labellers overlapped only 56% on skills, so the label is noisy too. Non-English
JDs are not read.

## Changing a reader

Bump `JD_PROFILE_VERSION`. Bump `job_inventory_service.EXTRACTOR_VERSION` too when
stored postings should be re-read; the crawl then re-parses them with no model
calls. Add the real sentence to `tests/test_jd_profile.py` first.
