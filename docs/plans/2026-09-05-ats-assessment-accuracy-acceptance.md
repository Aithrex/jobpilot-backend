# ATS assessment accuracy — acceptance report

Status: **offline implementation milestone** (2026-09-05). Not a production go decision.  
`RESUME_ASSESSMENT_V2_ENABLED` remains default **false**. Tailoring prompts, generated resume content, live calibration, and human-review gates were not changed.

## What was asked

Correct assessment matching and reporting so the UI does not call keywords missing when they are on the tailored resume, and does not ask for evidence the document already shows. Do not inflate scores to match a vendor tool.

## What changed

- One matcher (`requirement_matcher.py`) for scorer, gap engine, requirement assessment, and gap correction.
- Required denominator = required terms only. Preferred, keywords, and parked domain context stay out.
- Phrase cores + slash OR: `REST API Development` ↔ REST endpoints; `OpenAPI/Swagger` is OR(OpenAPI, Swagger).
- Word-boundary distinctions: SQL ≠ NoSQL, Java ≠ JavaScript.
- Presence-only coverage: one mention equals many mentions.
- Source checked independently; generated text is never its own evidence; missing source stays unknown.
- Full-content hashes; PDF/DOCX dispatch; owned and detached drafts share `compute_assessment_stages`.
- V2 failures return partial/unavailable v2 — no hybrid fallback.
- Legacy numeric overall, if shown, is labeled **internal document coverage** with numerator/denominator. Vendor rings are not presented as measured.
- Match-gap UI: “Already shown” with expandable span; no attach form; optional experience add no longer inserts a skill.

## Reproduction

No owned failing pair was available. Representative fixture documented in [2026-09-05-ats-assessment-accuracy-diagnostic.md](2026-09-05-ats-assessment-accuracy-diagnostic.md). Screenshot numbers were **not** reproduced.

## Targeted tests (this machine)

```text
PYTHONPATH=shared/python python -m pytest \
  tests/test_assessment_accuracy.py \
  tests/test_resume_scorer_v2.py \
  tests/test_term_registry.py \
  tests/test_requirement_assessment.py \
  tests/test_assessment_workflow_consistency.py \
  tests/test_assessment_baseline_offline.py \
  tests/test_assessment_service.py \
  tests/test_assessment_adversarial.py \
  tests/test_assessment_caller_routing.py \
  tests/test_keyword_gap_stats.py \
  tests/test_assessment_contracts.py \
  tests/test_requirement_reducer.py \
  tests/test_artifact_assessment.py \
  tests/test_assessment_comparison.py \
  tests/test_assessment_stage12.py \
  tests/test_assessment_shadow_harness.py \
  tests/test_assessment_artifact_roundtrip.py \
  tests/test_evidence_tailoring.py \
  tests/test_skill_implications.py -q
```

**Result: 269 passed** (84 + 185). Frontend: `atsScores`, `matchGaps`, `resumeMutations` — **12 passed**.

Regression coverage includes REST vs REST-endpoint wording, OpenAPI/Swagger parse, Postgres aliases, SQL/NoSQL, Java/JavaScript, repetition, context-not-required, projects/certs, qualifier identity, source-absent-from-final, missing-source unknown, bullet-hash change, PDF/DOCX + reopen, and v2-no-hybrid-fallback.

## Not done (unchanged gates)

- Production flag enable / deploy
- Live ≥60-pair pilot and blinded human review
- Tailoring prompt or automatic resume rewrite
- Treating any vendor (including Jobalytics) as calibration truth
