# ATS assessment-v2 acceptance report

Status: **implementation milestone** (2026-09-05, remaining-work pass). Not a production go decision.
Production flag **`RESUME_ASSESSMENT_V2_ENABLED` remains default `false`**.

Authoritative plan: [2026-09-05-ats-engine-master-implementation-plan.md](2026-09-05-ats-engine-master-implementation-plan.md).
Shadow harness output: [2026-09-05-ats-shadow-results.json](2026-09-05-ats-shadow-results.json).

## Evidence: offline suite (this machine)

```text
python -m pytest \
  tests/test_assessment_contracts.py \
  tests/test_requirement_reducer.py \
  tests/test_assessment_baseline_offline.py \
  tests/test_assessment_service.py \
  tests/test_artifact_assessment.py \
  tests/test_requirement_assessment.py \
  tests/test_assessment_comparison.py \
  tests/test_assessment_adversarial.py \
  tests/test_assessment_workflow_consistency.py \
  tests/test_assessment_stage12.py \
  tests/test_assessment_shadow_harness.py \
  tests/test_assessment_artifact_roundtrip.py \
  tests/test_assessment_caller_routing.py \
  tests/test_evidence_tailoring.py \
  -q
```

**Result (2026-09-05 remaining-work pass): 181 passed** on the suite above. Workflow proof included: tailor → edit → recheck → save → download → reopen.

Shadow offline harness:

```text
python -m evals.eval_assessment_shadow
```

**Result:** `n=4`, `ok=4`, `all_overall_null=true`, professions `{software, operations, sales, early_career}`, splits calibration=2 / holdout=2, **`go_nogo: no-go`** (honest blockers: no ≥60-pair live pilot, no blinded human reviewers, `EVAL_LIVE_LLM` off).

## Stages completed in this pass

### 1. Expression-driven planning and evaluation

| Behavior | Location / evidence |
|---|---|
| AND/OR expression selection in planner | `tailoring_planner._select_facts_for_expression`; `test_expression_driven_and_or_plan` |
| Ownership / negation / context / duration qualifiers | `requirement_assessment.observe_predicate` + adversarial suite |
| Date-interval union without double-counting overlap | `assessment_dates.merge_intervals` / `total_months`; `test_overlapping_intervals_not_double_counted` |
| Tenure alone ≠ tool-use duration | `tool_scoped_intervals`; `test_tenure_alone_does_not_credit_tool` |
| Duration with uncertain intervals | `meets_duration_months` / `duration_months_for_capability` |

### 2. Claude reader on canonical v2 path

| Behavior | Location / evidence |
|---|---|
| Provider-backed reader + deterministic merge | `assessment_reader.reader_pass_claude` / `reader_pass_v2` |
| Quote-backed findings must appear in document | quote validation in reader |
| Absence findings without fabricated quotes | `absence_finding` + reader merge; adversarial |
| Naturalness / retention / coverage-bloat helpers | retention + `detect_coverage_bloat` wired |
| Failure → partial/unavailable | `test_reader_claude_failure_is_partial`, empty-text unavailable |
| Stages use reader_pass_v2 | `assessment_service.run_assessment_stages` |

### 3. Caller routing

| Caller | Behavior |
|---|---|
| Guest tailor | `_finalize_tailored_result(..., guest_id=)` persists `ats_scores` / `assessment_id` on guest records when flag on |
| Batch `batch_tailor_item` | Same finalize path; version bind + assessment_id |
| Job-search `/tailor-resume` | Async `extract_and_tailor` when evidence **or** assessment-v2 enabled; policy pin via `adapt_ats_scores_job_payload` |
| Ownership / quotas / async | Unchanged guest reservation + ledger reserve on async path |
| Routing smoke | `test_assessment_caller_routing` |

Flag still default-off: overlay only runs when `assessment_service.enabled()`.

### 4. Real artifact validation

| Check | Evidence |
|---|---|
| Renderer-generated PDF round-trip | `test_pdf_roundtrip_preserves_employer_and_bullets` |
| DOCX ordered extract + format-specific cache keys | `test_docx_roundtrip_ordered_and_hashes` |
| Employer / title / date / bullet association targets | `assess_artifact` `checks` keys `experience.0.*` |
| Representative visual cohort (smoke) | `test_visual_regression_cohort_smoke` — no `\ufffd`; **not** pixel/human review |

### 5. Release evaluation harness

| Item | Result |
|---|---|
| Shadow measurements | Builtin multi-profession cohort; JSON report written |
| Calibration vs holdout splits | Present in builtin samples |
| Go/no-go | **`no-go`** always until live/human blockers clear |
| Fabricated live quality | **Not done** (and must not be equated with offline mocks) |

## Exit criteria vs evidence

### Passed (code + offline tests)

| Area | Evidence |
|---|---|
| Contracts / reducer / null overall | contracts + reducer tests |
| Flag default off | `test_flag_still_default_off`, `.env.example` |
| Ownership / idempotency / stale / cross-user | `test_assessment_service` |
| Ordered DOCX + SQL≠NoSQL | artifact + adversarial |
| Qualifiers + expression planning + date unions | adversarial + stage12 |
| Absence / bloat / retention | comparison + reader |
| `score_payload_v2` real stages | service + workflow |
| Guest/batch/job-search routing (code paths) | resume_service + jobs + caller routing tests |
| Workflow consistency | `test_workflow_tailor_edit_recheck_save_download_reopen` |
| Renderer PDF/DOCX round-trips | artifact_roundtrip |
| Shadow harness honesty | shadow_harness + `go_nogo: no-go` |

### Partial / genuine blockers

| Gap | Status |
|---|---|
| Deep semantic entailment for every capability | Alias/exact + limited semantic; not full NLI |
| Live Bedrock reader quality under load | Offline returns deterministic/partial; needs `EVAL_LIVE_LLM=1` + credentials |
| Pixel-level PDF visual regression + human layout review | Smoke cohort only |
| Tracker / Admin / auto-apply label audit for every surface | Null-safe overall in primary UI; not exhaustive admin audit |
| Concurrent multi-writer stress harness | Hash guards covered; no stress suite |
| **Live ≥60-pair multi-profession pilot + two blinded human reviewers** | **BLOCKER — cannot complete with available access** |
| **Consented multi-resume corpus for shadow vs production traffic** | **BLOCKER** |
| Production flag enable / deploy | **Out of scope; flag stays false** |

## Integrity gate (offline critical suite)

- NoSQL does not satisfy SQL
- Negated production use does not satisfy production_use
- Listed skill mention does not satisfy ownership
- Wrong-employer source scope → contradicted
- Absence findings never use fabricated `resume_span`
- Keyword repetition / bloat does not increase coverage counts
- Recheck does not rewrite resume prose
- Overlapping role intervals are not double-counted for duration
- Tenure alone does not prove tool-use duration
- Reader failure surfaces partial/unavailable

**Not claimed:** statistical FP/FN rates, employer ATS acceptance, or live multi-profession preference studies.

## Go / no-go (Package 7)

| Decision | **no-go** for production enablement |
|---|---|
| Why | Live calibration, holdout human review, and consented corpus are unavailable in this session; offline suite success is necessary but not sufficient |
| Staging | Offline cohort OK for continued staging experiments with flag still default-off |
| Next unblockers | Run `EVAL_LIVE_LLM=1` shadow with budget; recruit ≥60 pairs across professions; two blinded reviewers; then revisit go/no-go |

## Rollout stance

1. Keep **`RESUME_ASSESSMENT_V2_ENABLED=false`** in production.
2. Staging may enable for internal accounts after re-running the offline suite above.
3. Production cohort requires clearing Package 7 live/human blockers — **this report is not that decision**.

## Operator notes

- Indexes: `scripts/migrate_indexes.py` includes `resume_assessments`.
- Pin: in-flight jobs with `assessment_policy_version` keep assessment-v2 even if the flag flips off mid-flight.
- Rollback: disable flag; do not display legacy simulated scanners as a fallback for a failed v2 assessment.
- Shadow report path: `docs/plans/2026-09-05-ats-shadow-results.json`.
