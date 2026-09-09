# Content preservation (parse → tailor → select → export)

Date: 2026-09-08

## Problem

Tailored one-page PDFs could lose an entire nonempty source section
(reproduced by comparing a source résumé that still had Projects /
Infratrix with a tailored PDF that had no Projects heading). Saved
pipeline JSON for that pair was not available in-repo; the drop is
explained by selection policy plus empty-project deletion, not by the
renderer.

Root causes in `content_selection.py` (pre-change):

1. Experience was seeded with a 3/2 bullet floor; projects were seeded
   with pinned bullets only (usually none).
2. The last remaining experience bullet was protected; the last project
   bullet was not.
3. `apply_sel(drop_empty=True)` deleted project entries with zero bullets,
   which removed the Projects section.
4. IntegrityGuard re-injected dropped jobs at the end of the list,
   breaking employer grouping and chronology.
5. Unknown labeled sections (volunteer, research, publications, …) were
   stripped by schema coerce and never competed for space.

Parser loss, tailoring omission, selection removal, and export omission
are now distinguished via a content manifest (`parse` / `tailor` /
`select` / `export` / `user` stages).

## Policy

1. **Manifest.** Every section, entry, and supporting unit has a stable
   `content_id` derived from identity fields, not array position. The
   source snapshot is immutable. Omissions record a reason and remain
   recoverable.
2. **Section representation.** Page fitting may shorten bullets or pick
   representative entries. Deleting a nonempty source section requires an
   explicit user omission. Empty parsed sections may be omitted. Parse
   failures stay `unresolved`.
3. **No section favoritism.** Space is allocated from locks, JD
   relevance, unique evidence, and the actual document structure
   (student / credential / research-heavy signals). Not software-only
   rules.
4. **One page first.** Compact spacing, employer grouping, summary
   shortening, skill consolidation, then complementary selection.
   Overflow after bounded attempts is `one_page_not_possible` or
   `needs_shortening_choice`. Never silent two-page and never unreadably
   small type.
5. **One layout engine.** Measurement and PDF paint share `_render_pdf`.
   DOCX uses the same solver knobs.
6. **Failed preservation is not a successful export.** Download returns
   409 `preservation_failed` instead of a gutted file.

## Coverage

See `docs/coverage/resume-structure-coverage.md`.
