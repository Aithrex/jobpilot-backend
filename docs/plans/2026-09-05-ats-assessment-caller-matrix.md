# ATS assessment caller matrix

Status: Package 0 inventory for [2026-09-05-ats-engine-master-implementation-plan.md](2026-09-05-ats-engine-master-implementation-plan.md). Captured 2026-09-05. Not an implementation of assessment-v2.

Two live scoring paths:

1. **Legacy hybrid** — `ResumeScorer.score` when the resume is not an evidence-led result. Numeric `overall`, optional scanners / parseability.
2. **Document assessment (evidence-v1)** — when `is_result(tailored)` is true. `tailoring_assessment.score_payload` returns `schema_version: 3`, `assessment_version: evidence-v1`, `overall: null`, plus `document_assessment`.

There is a `resume_assessments` collection and opt-in `RESUME_ASSESSMENT_V2_ENABLED` flag (Package 2+). Default off.

## Backend callers

| Caller | Path | R/W | Path today | Ownership | Package 6 migration |
|---|---|---|---|---|---|
| `POST /api/resume/ats-scores` | `services/resume/blueprints/resume.py` ~466 | write job | both (branch in scorer) | JWT user | Adapter → canonical assessment request; keep route |
| Job poll `GET .../job/<id>` | same ~2151 | read | both | JWT / guest | Result shape includes assessment-v2 discriminant |
| `ats_scores` worker | `shared/python/services/resume_service.py` ~2131 | write result | both | job owner | Call `assessment_service`; pin rubric on job |
| `ResumeScorer.score` | `shared/python/services/resume_scorer.py` ~79 | compute | assessment if `is_result` else legacy | n/a | Compatibility facade only |
| `tailoring_assessment.score_payload` / `assess` | `shared/python/services/tailoring_assessment.py` | compute | assessment | n/a | Delegate to canonical service; drop duplicate rules |
| `_finalize_tailored_result` | `resume_service.py` ~619 | write into resume | assessment (`finalize`) | tailor job user | Attach assessment ID after stages complete |
| Evidence tailor / regenerate / guest / extract_and_tailor | via `_finalize_tailored_result` | write | assessment | user / guest session | Same finalize path |
| `batch_tailor_item` | `resume_service.py` batch branch | write | partial (may skip shared finalize pieces) | user | Must use same measurement policy |
| `prepare_tailored_artifact` | `resume_service.py` | write resume | often no assessment | user | Explicit pending/unchecked if not assessed |
| Job-search tailor shortcut | `services/jobs/blueprints/jobs.py` | write simplified | neither/legacy | JWT | Inventory in Package 6; do not invent scores |
| `tailoring_pipeline` quality loop | `tailoring_pipeline.py` ~316 | write into resume | assessment | tailor job | Re-run affected stages only |
| `save-record` | `resume.py` ~2031 | write Mongo | client `ats_scores` body | JWT | Bind server assessment ID; reject forged scores |
| `save_version` / `append_version` | `resume.py` ~1020, `resume_versioning.py` | write Mongo | both; clears stale assessment | JWT | Same; hash mismatch → drop sidecar |
| List / get records | `LIST_VIEW_PROJECTION` in `resume_versioning.py` | read | legacy `overall` only | JWT / guest | Project status counts, not fake overall |
| Download / `_render_document` | `resume.py` ~2468 | export check | assessment `check_export` | JWT / guest | Format-specific artifact assessment; never reuse PDF as DOCX |
| `verify_resume` | `resume.py` ~501, job ~1725 | write verification | neither | JWT | Keep separate from assessment |
| `ats_signal_service` | `ats_signal_service.py` | read Mongo | legacy overall | analytics | Must not treat assessment-v2 as job match |
| `pipeline_intel_service` | funnel reads overall | read | legacy overall | analytics | Same |
| Admin activity | `services/auth/blueprints/admin.py` ~581 | read | legacy overall | admin | Label historical; blank for null overall |
| Entitlements / rate limit | `entitlements_service.py`, ats-scores route | quota | n/a | user | Preserve quotas on new request API |
| `pdf_ats_check` | via scorer / `check_export` | compute | both (differently) | n/a | Fold into `artifact_assessment` |
| `ats_vendor_checks.build_vendor_compat` | unused in prod scorer | — | orphan | n/a | Do not revive as simulated vendor scores |

## Frontend callers

| Caller | Path | R/W | Path today | Package 6 migration |
|---|---|---|---|---|
| `fetchATSScores` | `src/lib/api.ts` ~2061 | read (poll) | both | Accept schema_version 4; no invented overall |
| `saveTailoringRecord` / save version | `api.ts` ~2127+ | write | client scores | Send assessment ID when present |
| `AtsScorePanel` | `craft/tailor/AtsScorePanel.tsx` | read UI | prefers `document_assessment` | Requirement counts + file status + top actions |
| `DocumentAssessmentPanel` | `craft/tailor/DocumentAssessmentPanel.tsx` | read UI | assessment | Evolve to v2 fields; absence findings |
| `TailorPage` badge / rescore | `craft/TailorPage.tsx` | read / trigger | both | Stop assuming numeric overall |
| `ResumePreviewFrame` strength meter | `craft/tailor/ResumePreviewFrame.tsx` | read | numeric overall only | Hide meter when overall null |
| `NudgeBar` | `craft/tailor/NudgeBar.tsx` | read | numeric overall / skills_alignment | Top actions from assessment |
| `TailoredPage` sort / highAts | `craft/TailoredPage.tsx` | read | uses `atsOverall()` (Package 1) | Sort unknown last; never coerce null→0 |
| `ResumeOverviewPage` | via `atsOverall()` | read | safe undefined | Keep using helper |
| Tracker ApplicationRow / Detail | components | read | legacy overall | Blank if null |
| `BatchTailor` | `components/resume/BatchTailor.tsx` | read | null-safe via `atsOverall()` (Package 1) | Same null-safe display |
| AdminPortal | `pages/AdminPortal.tsx` | read | legacy overall | Historical label |
| `resumeMutations` | `src/lib/resumeMutations.ts` | clear sidecar | strips assessment on edit | Invalidate correct dependencies |
| `atsScores.ts` | `src/lib/atsScores.ts` | helpers | `isAssessmentV2` / schema 4 helpers (Package 1) | Keep discriminant helpers |

## Gaps vs assessment-v2

| Gap | Today |
|---|---|
| Server-authoritative assessment store | Scores on `tailoring_records.ats_scores` / `versions[].ats_scores` only |
| Client-trusted scores | save-record / save-version accept client body |
| AND/OR requirement expressions | Flat `alternatives` OR only |
| Independent document/source/claim states | Single planner `state` + boolean `visible` |
| Ordered artifact structure | DOCX concatenates paragraphs then tables; `_normalized` strips whitespace |
| Absence findings | Reader requires a resume quote |
| Rollout flag | Plan-only `RESUME_ASSESSMENT_V2_ENABLED` |
| No public overall at launch | Legacy path still emits overall; UI still sorts/nudges on it |

## Related non-callers (do not confuse)

- `fit_score.py` — candidate-to-job match only; out of scope for resume assessment.
- Claim verification (`verify_resume`, `claim_guard`) — advisory fact sidecar; evidence-led finalize currently skips `_attach_verification`.
