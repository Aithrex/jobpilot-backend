# JD Best-Base Resume Selection

> **For agentic workers:** execute this plan in both repos (backend contract first). Existing one-resume users must keep working without a client update.

**Goal:** When a user tailors a resume to a job description, automatically pick the best-fitting *parsed* base resume for that JD, tailor from it, and always tell the user which file was used — without breaking one-resume users or mutating the global active pointer.

**Architecture:** Per-job override, not a new global pointer. Ranking uses the existing deterministic `fit_score.score` (fit-v2.0) — no extra LLM call, no extra tailor quota. The worker resolves `resume_id` via owner-scoped `get_owned_asset` and returns a `base_selection` sidecar on the job result and `tailoring_records`.

**Tech Stack:** Flask + Mongo `resume_assets` / `tailoring_records`; React Tailor page + Batch Tailor + My Resumes + Tailored history.

## Global Constraints

- Auth failures stay 400/409/403/429 — never 401.
- Long LLM work stays async (`extract_and_tailor` / `tailor` / `batch_tailor_item`); ranking is CPU-only inside the existing job.
- Tailor quota is still one `reserve_tailor` unit per tailor. Ranking is free.
- Never mutate `users.active_resume_id` as a side effect of tailoring (races concurrent jobs; surprises job-match scores).
- Never fabricate resume facts (IntegrityGuard unchanged; we only *choose which existing asset* to feed it).
- No new `/api/<prefix>` — extend existing `/api/resume/*` (already routed).
- ASCII UI copy. Additive JSON only — old clients that omit `resume_id` still work, and gain auto-select server-side.
- Guest single-slot tailor is unchanged (one resume).

## Refined product spec

### User-visible behavior

| Resumes parsed | What happens |
|---|---|
| 0 | Existing error: re-upload. No quota wasted if we can fail at the route; worker still fails if parse raced. |
| 1 | Today's flow. Mode `single`. Show "Using {filename}". No ranking UI. |
| 2+ | Default: auto-pick the best fit for *this* JD, then tailor it. Mode `auto`. Picker defaults to "Best match for this job". User can pin a specific file instead (mode `pinned`). Opt-out: "Always use my active resume" (`selection_mode=active`). |

After any tailor (1 or many): a persistent banner names the file. Auto-picks also show the fit score and a one-line reason. History and batch cards show the same filename.

**Active resume** on My Resumes stays the pointer for job matching / Discover scores. Copy today says "Use for Tailoring" — that becomes "Set as active" plus a caption that Tailor picks per job.

### API contract (additive)

`POST /api/resume/tailor-with-jd`, `POST /api/resume/tailor`, and each `batch_tailor_item`:

```json
{
  "job_description": "...",
  "resume_id": "optional, owner-scoped parsed asset",
  "selection_mode": "optional, auto | active (default auto)"
}
```

Validation of `resume_id` happens **before** `reserve_tailor`:
- missing/unknown/other-user → 404 `{error: "resume_not_found"}`
- owned but not `parsed` → 409 `{error: "resume_not_parsed"}`
- invalid `selection_mode` → treat as `auto`

If `resume_id` is present it wins (pinned), regardless of `selection_mode`.

Job result adds:

```json
{
  "base_selection": {
    "resume_id": "...",
    "filename": "backend.pdf",
    "mode": "auto | pinned | active | single",
    "match_score": 82,
    "reason": "Best match for Senior Backend Engineer (82/100).",
    "candidates": [
      {"resume_id": "...", "filename": "backend.pdf", "match_score": 82, "is_selected": true, "is_active": false},
      {"resume_id": "...", "filename": "frontend.pdf", "match_score": 61, "is_selected": false, "is_active": true}
    ]
  }
}
```

`candidates` only when 2+ parsed assets were considered. `match_score` omitted if scoring failed open.

`tailoring_records` additive fields: `base_resume_id`, `base_selection` (same shape). `base_resume_filename` / `s3_key` stay populated.

Progress phase `selecting_base` (pct ~15) is written only when 2+ parsed assets exist. Older frontends ignore unknown phases.

### Selection algorithm

1. List owner-scoped assets with `parse_status=parsed` and `structured != null`. Skip pending/failed/empty.
2. 0 → fail like today.
3. 1 → that asset, mode `single`. No ranking.
4. Pinned `resume_id` → that asset, mode `pinned`.
5. `selection_mode=active` → `get_active_structured` path, mode `active`.
6. Else score each structured resume with `fit_score.score` against `{title: jd_analysis.job_title, description: jd_text, location: jd_analysis.location}`. Highest `score` wins.
7. Ties: currently-active asset, then newest `parsed_at` / `created_at`.
8. Scorer exception: fail open to the active (or only) parsed asset, mode `active`/`single`, log warning. Never fail the tailor because ranking hiccuped.
9. Then `ensure_structured_resume(user_email, resume_id=winner)` so a selected-but-stale parse can re-parse **that** asset, not the active one.

Cap public `candidates` at 8 (highest scores). Score all parsed assets (users have handfuls).

### Explicit non-goals

- Do not change Discover / inventory `match_score` (still the active resume).
- Do not auto-promote the winner to `active_resume_id`.
- Do not rank with a second LLM or a second scoring formula.
- Do not count ranking against tailor quota.
- Do not change guest tailor, regenerate (uses the already-chosen base), or cover-letter.

### Edge cases

1. Existing clients omit the new fields → server auto-selects when N≥2. One-resume users unchanged.
2. Concurrent tailor jobs: each payload carries its own `resume_id`; no active-pointer write → no race.
3. User deletes the selected asset mid-job → `ensure` returns none → existing fail message.
4. Two uploads of the same file (content-hash dup as two assets): equal scores; tie-break active then newest.
5. All scores 0: still pick (tie-break); do not fail.
6. Mix of parsed + pending: pending ignored.
7. Pinned id of another user: 404, no quota consume.
8. Batch: each JD independently auto-selects (a backend JD and a frontend JD can pick different files).
9. Daily Pipeline → Tailor handoff: same auto-select (no resume_id unless user pinned on the page).
10. Idempotent replay: selection already in the stored job result.
11. Filename missing: UI falls back to "Resume uploaded {date}" / "Untitled resume".
12. `selection_mode=active` with no active pointer: newest parsed (same fallback as `get_active_structured`).
13. Re-tailor from the result screen: user can pin a different file; costs another quota unit (honest).
14. IntegrityGuard still runs on the chosen asset only — we never merge facts across resumes.

## Files

**Backend**
- Create: `shared/python/services/base_resume_selector.py`
- Create: `tests/test_base_resume_selector.py`
- Modify: `shared/python/services/resume_access.py` (`list_parsed_assets`)
- Modify: `shared/python/services/resume_parser.py` (`ensure_structured_resume(resume_id=)`)
- Modify: `shared/python/services/resume_service.py` (resolve helper; three job types; persist)
- Modify: `services/resume/blueprints/resume.py` (optional args, pre-quota validate)

**Frontend**
- Create: `src/app-shell/pages/craft/tailor/BaseResumePicker.tsx`
- Create: `src/app-shell/pages/craft/tailor/BaseResumeUsedBanner.tsx`
- Create tests for picker + api payload
- Modify: types, `api.ts`, `TailorPage`, `TailorProgress`, `MyResumesPage`, `TailoredPage`, `BatchTailor`
