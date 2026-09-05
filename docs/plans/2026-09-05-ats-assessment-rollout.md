# ATS assessment-v2 Package 7 — calibration and controlled rollout

Companion to [2026-09-05-ats-engine-master-implementation-plan.md](2026-09-05-ats-engine-master-implementation-plan.md).

## Rollout flag

| Setting | Default | Behavior |
|---|---|---|
| `RESUME_ASSESSMENT_V2_ENABLED` | `false` | Server-side. When off, legacy hybrid / evidence-v1 paths remain. When on, `/ats-scores` and owned assessment create/attach use assessment-v2 (`schema_version: 4`). |
| Job pin | `assessment_policy_version` on job payload | Disabling the flag stops **new** cohort assignment; in-flight jobs that already pinned keep `assessment-v2`. |

## Shadow / staging sequence

1. Frontend compatibility (null-safe overall, `AssessmentV2Panel`) — shipped with Packages 1 and 6.
2. Backend disabled by default — `RESUME_ASSESSMENT_V2_ENABLED=false`.
3. Staging: enable flag; run offline invariant suite (`pytest tests/test_assessment_*.py tests/test_requirement_*.py tests/test_artifact_*.py`).
4. Shadow: same inputs, no user-facing score swap; log stage status, version IDs, counts, latency — no resume PII / prompts.
5. Controlled cohort: enable for internal accounts first; preserve historical numeric `ats_scores` as historical.

## Rollback

- Set `RESUME_ASSESSMENT_V2_ENABLED=false`.
- Do **not** rewrite running jobs' pinned rubric.
- Do **not** silently fall back to simulated vendor scores for a failed v2 assessment — show incomplete/unavailable checks.
- Documents, ownership, and `resume_assessments` history remain.

## Offline invariant suite (Package 7 gate before live calibration)

```bash
python -m pytest \
  tests/test_assessment_contracts.py \
  tests/test_requirement_reducer.py \
  tests/test_assessment_baseline_offline.py \
  tests/test_assessment_service.py \
  tests/test_artifact_assessment.py \
  tests/test_requirement_assessment.py \
  tests/test_assessment_comparison.py \
  -q
```

Critical adversarial fixtures live in `tests/fixtures/assessment/cases.py`.

## Live calibration (not automated here)

Per master plan §11: ≥60 base/tailored pairs across professions, two blinded reviewers, separate calibration/holdout. Preregister subjective thresholds before examining holdout. Integrity gate: no accepted fabricated tool/ownership/metric claims or cross-employer provenance in the critical suite.

## Go / no-go checklist

- [ ] Offline invariants green
- [ ] Package 0 caller matrix covered for enabled cohort
- [ ] No public overall % on new results
- [ ] Save-version binds `assessment_id` (rejects forged v2 bodies without ID)
- [ ] Staging p95 / cost measured against §8 budgets
- [ ] Acceptance report filed before production cohort expansion
