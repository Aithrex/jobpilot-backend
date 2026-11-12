# Résumé format preferences (`services/resume_format.py`)

What the user chose about the look of their résumé, carried on the tailored
résumé as `tailoring_preferences` (the same object that holds wording locks).
No model call is involved anywhere, and **no preferences renders exactly what
rendered before** (`tests/test_resume_format.py::test_default_choices_render_exactly_as_before`).

| Key | Values (default first) | Effect |
|---|---|---|
| `page_length` | `one` · `two` | Content selection fits the chosen length; the page-fill step never pads a two-page résumé |
| `density` | `compact` · `comfortable` | Comfortable: 0.5in sides, 0.4in top/bottom, body never below 9.7pt; selection trims lines instead of the renderer shrinking type |
| `accent` | `black` · `navy` | Name, section headings and their rules in `#004F90` (PDF and DOCX); body text stays black |
| `section_order` | `auto` · `education_first` · `experience_first` | `auto` puts Education first for a current student or a graduate of the last 12 months with under 3 years of work |

## Where each is honoured

- **Tailor:** `POST /api/resume/tailor` and `/tailor-with-jd` accept
  `preferences`; `ResumeTailor.tailor(..., preferences=)` applies them before
  bullet allocation, and `regenerate` keeps the current résumé's choices.
- **Change after tailoring:** `POST /api/resume/format`
  `{tailored_resume, preferences, jd_analysis?}` → `{tailored_resume}`.
  Inline (a few renders, no model). Moving to two pages first restores the
  bullets selection dropped for space (`content_selection.omitted_bullets`,
  the user's own tailored lines); selection then re-fits. The screener's view
  is refreshed, keeping earlier evidence verdicts.
- **Render:** `ResumeRenderer.generate_pdf/docx` switch to a cached variant
  spec (`for_preferences`) and draw sections in `resume_format.section_sequence`.
- **Selection:** `content_selection._measure` measures at the renderer's floor
  tier (9pt, or 9.7pt comfortable).

## The one-page bug this fixed

Selection measured at 10pt with 3.10mm leading, tighter than any tier the
renderer draws (its floor is 9pt at 3.61mm). A dense four-role résumé
"selected to fit one page" measured 264.9mm against 265.7mm available, then
needed 289.3mm to draw at 9pt, so it rendered two pages while reporting
`fit_one`. Selection now measures at the floor tier, so a one-page choice is
one page.
