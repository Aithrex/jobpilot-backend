# Content selection policy (page fitting)

Date: 2026-09-05

## Problem reproduced

PDF/DOCX export could silently drop project bullets beyond a 2-bullet floor via
`ResumeRenderer._trim_to_fit` (and augmenter `_overflow_trim` strategy 2).
Preview kept the full list; download omitted later bullets — including a third
bullet that uniquely demonstrated a required JD capability (e.g. Kubernetes).

## Policy

1. **One revision.** `select_resume_content` runs in the tailor finalize path and
   writes the selected bullet set onto the stored resume. Preview, save,
   download, and reopen use that same revision identity.
2. **Renderer is faithful.** PDF/DOCX must not independently discard, truncate,
   or rewrite bullets. `_trim_to_fit` is a no-op. Overflow becomes two pages or
   an explicit shortening outcome.
3. **Selection criteria.** Prefer unique required-JD coverage, complementarity
   over near-duplicates, accomplishment specificity, and experience priority.
   Quantified metrics are only a soft tiebreaker — not an automatic advantage
   for every profession.
4. **Pins and omissions.** Locked/pinned bullets and user omission lists are
   honored. Source attribution / fact ids are preserved when present.
5. **Compression before drop.** Bounded filler removal may shorten wording
   while preserving factual scope. No invented metrics.
6. **Disclosure.** Omitted candidates are retained under
   `content_selection.omitted_bullets` with a concise
   “Content omitted to fit” disclosure. Assessment/Recheck remain read-only and
   do not run selection.
## Page length

**Prefer one page.** Selection packs against a one-page budget by default.
A second page is used only when:

- the user explicitly requests two pages, or
- after relevance-aware selection + compression, the kept content still cannot
  fit at readable font/margins without dropping valuable unique evidence.

Silent truncation to force one page is not allowed.

## Before / after (fixture)

| | Behavior |
| --- | --- |
| Before | `_trim_to_fit` pops project bullets while `len > 2` until height fits |
| After | Selection keeps uniquely covering 3rd bullet when it wins JD coverage; renderer paints all selected bullets; overflow → two pages / explicit outcome |

## Code

- `shared/python/services/content_selection.py`
- `bullet_allocator.allocate_bullets` delegates here
- `resume_service._finalize_tailored_result` applies selection before persist
- `resume_renderer` RENDER_VERSION `2026-09-05.2`
