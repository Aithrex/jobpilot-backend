"""Resume structure coverage: parser, schema, selection, renderer.

This is a living matrix, not a claim that every possible résumé is covered.
New inputs should be added here when they fail an invariant — not as
one-off special cases in selection.

Updated: 2026-09-08
"""

# Status: supported | generic | unresolved | gap

MATRIX = """
| Structure | Parser | Schema | Selection | PDF/DOCX | Preview | Notes |
|---|---|---|---|---|---|---|
| Students with no employment | supported | supported | supported | supported | supported | Projects/research get higher weight |
| Experienced candidates with many jobs | supported | supported | supported | supported | supported | Role headers kept; bullets compete globally |
| Multiple roles under one employer | supported | supported | supported | supported (grouped) | supported (grouped) | Consecutive same-employer; dates per role |
| Career change / mixed relevance | supported | supported | supported | supported | supported | JD tilts weights; does not delete sections |
| Project-heavy | supported | supported | supported | supported | supported | Representation required if source nonempty |
| Research-heavy | generic | additional_sections | supported | generic | generic | Not folded into projects |
| Healthcare licenses / certs | certs string + additional | additional licenses | supported | generic | generic | Flat cert names stay in certifications[] |
| Clinical rotations | additional | additional | supported | generic | generic | |
| Teaching | additional | additional | supported | generic | generic | |
| Publications / presentations | additional | additional | supported | generic | generic | Long-form CV: one-page is not forced silently |
| Academic CV (many pubs) | additional | additional | supported | generic | generic | Overflow → shortening/two-page choice |
| Trades / operations / sales / finance / admin | supported | experience+skills | supported | supported | supported | No software-only project generation |
| Freelance / consulting | experience | experience | supported | supported | supported | Distinct engagements stay distinct |
| Volunteer | additional | additional | supported | generic | generic | |
| Custom headings | additional custom | additional | supported | generic | generic | Original heading preserved |
| Paragraph entries (no bullets) | additional body | body field | supported | generic | generic | |
| Missing dates | supported (empty string) | supported | supported | supported | supported | |
| Long names / URLs | supported | supported | supported | wrap | wrap | |
| International / CJK text | parser+font | supported | n/a | CJK font path | preview fonts | Unrenderable glyphs fail loudly |
| Tables / columns / bad PDF extract | unresolved section | parse_status=unresolved | keep unresolved | generic raw_text | generic | Never masquerade as empty |
| User locks | pinned / locked_fact_ids | prefs | supported | n/a | n/a | Survive compression/reorder |
| Explicit omissions | omitted_sections / omitted_fact_ids | prefs | authorized drop | n/a | n/a | Reason recorded; source snapshot recoverable |
| Restored content | source snapshot | manifest | re-select | n/a | n/a | Omitted bullets remain in snapshot |
| Two-column original layout | unresolved if not linear | n/a | n/a | single column | single column | Layout warning; content kept if extracted |
| Interests / references | skipped | skipped | n/a | n/a | n/a | Boilerplate, not evidence |
"""
