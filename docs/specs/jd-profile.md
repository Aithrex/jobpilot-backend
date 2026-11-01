# The JD dictionary (`jd-1.2.0`)

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
| `experience` | `min_years`, `max_years` | What the posting requires, or null; months read as fractions of a year |
| | `preferred_years` | Set only when a preference is all it states |
| | `evidence` | Source sentence |
| `education` | `min_level`, `preferred_level` | 0 not stated, 1 high school or associate, 2 bachelor, 3 master, 4 doctorate |
| | `min_degree`, `preferred_degree` | The same levels by name |
| | `fields` | For example `computer science` |
| | `equivalent_experience_accepted` | True or false |
| | `evidence` | Source sentence |
| `skills` | `required`, `preferred` | Canonical names; the LLM's are added when present |
| | `technical`, `soft` | The same skills split by kind; `soft` also holds terms measured as noise, such as "recruiting" |
| | `additional` | Skills from `jd_skill_vocabulary.py`, which the match score does not read |
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

Prompts never see the dictionary directly. `jd_profile.prompt_view` replaces it
with `job_facts` (years, degrees, fields, skill alternatives, certifications), so
eligibility wording and raw JD quotes stay out of anything that writes a résumé.
Every prompt that dumps a JD analysis must go through it.

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

`evals/data/jd_answer_key.jsonl` holds 1,050 real JDs labelled blind from the JD
text. The `dev` splits were used to write rules. `holdout` is the newest 150
random JDs, and `evals/eval_jd_answer_key.py` refuses to print its errors.
`evals/test_jd_answer_key.py` is the CI gate.

**The labels are AI-drafted until a person reviews them** (`review` on each row).
`evals/data/jd_answer_key_review.csv` lists the rows worth checking.

Scores on JDs no rule had seen when they were scored, 150 random JDs each round:

| Field | `jd-1.1.0`, round 1 / 2 | `jd-1.2.0`, round 1 / 2 |
|---|---|---|
| Sponsorship status | 98.7% / 100% | 98.7% / 100% |
| Work authorization | 99.3% / 96.7% | 99.3% / 96.7% |
| Clearance required | 99.3% / 98% | 99.3% / 98% |
| Minimum degree | 91.3% / 87.3% | 92% / 86.7% |
| Minimum years | 79.3% / 89.3% | 79.3% / 90% |
| Work mode | 87.3% / 88.7% | 86.7% / 90% |
| Employment type | 86% / 84% | 90.7% / 88.7% |
| Skills recall | 31.4% / 29.3% | 33.5% / 32% |
| Every field right on one JD | 52% / 50.7% | 52.7% / 52.7% |

Each round's score can move about six points by chance, which is why round 1's
years and round 2's differ so much. The loop stopped when degree, years and work
mode stayed flat for two rounds. Rules written after reading more JDs raise the
tuning score and leave unseen JDs where they are.

What remains is mostly not a bug. Labellers split 27 to 16 on whether a
preferred-only figure is `min_years`, and `min_years` stays required-only because
that agrees with more labels (85.9% to 84.4%) while `preferred_years` still
carries the figure. Non-English JDs are not read. Skills need a model.

## Changing a reader

Bump `JD_PROFILE_VERSION`. Bump `job_inventory_service.EXTRACTOR_VERSION` too when
stored postings should be re-read; the crawl then re-parses them with no model
calls. Add the real sentence to `tests/test_jd_profile.py` first.
