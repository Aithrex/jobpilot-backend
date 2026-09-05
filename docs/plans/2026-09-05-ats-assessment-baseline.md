# ATS assessment Package 0 — fixture inventory and offline baseline

Companion to [2026-09-05-ats-assessment-caller-matrix.md](2026-09-05-ats-assessment-caller-matrix.md).
Fixtures live in `tests/fixtures/assessment/cases.py`. No live Bedrock calls in Package 0.

## Fixture inventory

| ID | Adversarial claim | Target package | Offline stub |
|---|---|---|---|
| `sql_nosql_substring` | `SQL` must not match `NoSQL` after normalization that strips whitespace | 3 | resume + JD in `cases.py` |
| `and_or_python_java_sql` | `Python or Java, plus SQL` is `AND(OR(...), SQL)`, not three headlines | 1 (reducer) / 4 (producer) | JD stub |
| `wrong_employer_claim` | Same bullet text on the wrong employer fails source reconciliation | 4 | two-job resume |
| `keyword_repetition` | Repeating a term does not increase coverage | 4 | resume with repeated Python |
| `absence_without_fake_quote` | Absence findings must not fabricate resume quotes | 5 | nurse resume vs accountant JD |
| `qualitative_full_support` | Qualitative work can fully support appropriate requirements | 4 | nurse resume |
| `docx_table_order` | DOCX extraction must preserve document order | 3 | same resume; needs real DOCX bytes in Package 3 |

Additional cases to add when Package 3–5 land (listed in the master plan, not yet stubbed as files):

- Negated / uncertain production use
- Evaluated vs implemented vs owned
- Overlapping role date intervals for duration
- Base/tailored semantic equivalence (unchanged coverage)
- Longer keyword-heavy draft must not win on coverage counts alone
- User-confirmed addition changes source lineage, not “writing gain”
- Real PDF/DOCX round trips with glyph issues and dropped blocks

## Offline baseline shapes (2026-09-05)

### Legacy hybrid (`ResumeScorer.score` when not `is_result`)

Observed contract (validated against `ATS_SCORES_SCHEMA` / scorer output):

- Numeric `overall` and dimension scores (`keyword_match`, `keyword_frequency`, `skills_alignment`, …).
- Optional `pdf_parseable_*` when PDF bytes are provided.
- May include AI scanners / suggestions on the live path.
- Frontend `atsScores.ts` still documents **schema_version 2** rubric keys (`hard_skill_match`, `vendor_compat`). Current production scorer path does **not** reliably emit those v2 keys; stored historical records may differ.

Offline measurement note: construct a non-evidence resume and call `ResumeScorer.score(resume, jd, pdf_bytes=None)` with AI stubs disabled or expected to degrade — Package 0 does not require a live provider.

### Evidence-v1 assessment (`is_result` → `score_payload`)

From `tailoring_assessment.score_payload`:

```text
schema_version: 3
assessment_version: evidence-v1   # tailoring_policy.VERSION
overall: null
document_assessment: { version, content_hash, jd_hash, artifact, requirements, reader_review, ... }
suggestions / strengths / missing_keywords derived from reader + requirement states
```

Known defects relative to assessment-v2 (baseline, not fixed here):

| Defect | Evidence |
|---|---|
| Whitespace-stripped token match | `_normalized` removes all whitespace before containment |
| DOCX order | paragraphs joined, then all table cells |
| Quote-only reader | findings dropped unless `quote` appears in visible text |
| `ready` conflates stages | file + reader + no concern kinds |
| Flat requirements | `visible` boolean; planner single `state` |

### Call counts (offline, stubbed)

Package 0 does not run paid models. Expected offline shape when AI is stubbed/failed:

| Entry | Deterministic work | Model calls (live) |
|---|---|---|
| Legacy `score` without PDF | keyword/skill/section math | 0–1 suggestion/scanner (if enabled) |
| Legacy `score` with PDF | + `pdf_ats_check` | same |
| `assess(..., run_reader=False)` | export checks + requirement visibility | 0 |
| `assess(..., run_reader=True)` | + reader | 1 Claude PRO |
| `finalize` | 1 PDF render + `assess` | 0–1 |

Live p95 latency is deferred to Package 7.

Executable offline smoke: `tests/test_assessment_baseline_offline.py` asserts legacy numeric `overall` vs evidence-v1 `schema_version: 3` / `overall: null`, and that no current scorer path emits `schema_version: 4`.

## Discriminant reserved for Package 1+

| Field | Value |
|---|---|
| `schema_version` | `4` (unused by any current consumer as of this inventory) |
| `assessment_version` / rubric | `assessment-v2` |
| Public overall | absent / always `null` for new results |
| Rollout flag (Package 2) | `RESUME_ASSESSMENT_V2_ENABLED` |
