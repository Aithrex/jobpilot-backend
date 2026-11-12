# Screener simulator (`screener-1.0`)

`shared/python/services/screener_sim.py` reads every tailored résumé the way an
employer's screener does, and attaches the result as the `screener` sidecar on
the tailor result (`resume_service._finalize_tailored_result`, both the legacy
and the evidence-led paths). It is advisory and fails open: a render, parse or
model failure never fails a tailor job.

## Why

Workday HiredScore (since the 5 Aug 2026 release), Greenhouse Talent Matching
and Ashby's AI-assisted review all check a job's requirements one at a time and
show a recruiter the résumé line that proves each one. HiredScore's basic
qualifications decide A/B versus C/D. Our old "ATS Score" counted the same JD
terms the writer had been told to add, so it could only go up. This module asks
the employer's question instead: for each requirement, can a reader of the PDF
text find a line that proves it?

## What it reports

| Field | Meaning |
|---|---|
| `criteria[]` | One row per JD requirement: `kind` (`must` = required skills, `nice` = preferred), `visibility` (`in_context` in a job/project line · `listed_only` in Skills/summary · `not_visible`), `evidence` in the SOURCE résumé (`proven` · `listed` · `confirmed` fact · `implied` · `gap`), the visible `span` and the `source_line` |
| `summary` | Must/nice counts and a deterministic `grade` shaped like HiredScore: all musts visible → A/B, one missing → C, more → D |
| `fixes[]` | Requirements the source proves but the page does not show. Fixable by presentation |
| `gaps[]` | Requirements the source does not prove. A question for the user, **never** written |
| `parse` | Render → pypdf extraction → name/email/phone/heading checks |
| `lens` | The countable parts of HackerRank's published intern rubric ([interviewstreet/hiring-agent](https://github.com/interviewstreet/hiring-agent), MIT): projects without links (3–5 points off each), tutorial-style projects, AI work listed vs built vs evaluated (1–3 / 4–7 / 8–10 of 10), LinkedIn/portfolio bonuses, founder / early-engineer / open-source evidence |
| `warnings[]` | Text addressed to an AI screener ("ignore previous instructions…"), requirements lost between the data and the PDF |
| `patched[]` | Lines the visibility patch restored (below) |
| `screener` | `SCREENER_SIM=full` only: the LLM read (level, six-second read, reason to interview, top concern, HackerRank rubric scores clamped to each category's cap) |

## The visibility patch (write side)

`patch_visibility` runs before verification. When the source résumé proves a
requirement in a job or project bullet and the tailored résumé no longer shows
it, it puts the **user's own source bullet, verbatim,** back into the **same**
job or project. It replaces the tailored rewrite of that bullet when one exists
(token overlap ≥ 0.5) so nothing is duplicated, and inserts it otherwise (first
for a must-have). At most four lines per run. It never adds a requirement the
source does not prove and never moves a line to another employer or project.
`SCREENER_VISIBILITY_PATCH=false` turns it off.

## The LLM read (`SCREENER_SIM=full`)

One FLASH call at temperature 0 that sees **only the extracted PDF text**: never
the source résumé, fact store, writer notes or earlier scores, because those
would let it reward evidence the employer cannot see. Each quote it returns is
checked against the page; a "meets" without a quote that is on the page becomes
"unclear". The grade stays deterministic. LLM screeners prefer text written by
an LLM (Xu et al., AIES 2025), so ours would flatter our own writer, and
HackerRank's README reports one résumé scoring 74–90 across runs of a single
LLM call. Cost: about 4–5k input tokens per tailor.

## Switches

| Env | Default | Effect |
|---|---|---|
| `SCREENER_SIM` | `deterministic` | `off` · `deterministic` (no model call) · `full` (adds the LLM read) |
| `SCREENER_VISIBILITY_PATCH` | `true` | restore proven source lines the tailored text lost |

## Known limits

- Project entries have no URL field (`schemas/resume_schemas.py` projects:
  `name, dates, bullets, tech`). Every project therefore counts as "without
  link" unless a URL appears in its text, and HackerRank's rubric deducts 3–5
  points for each. Adding `url` to parse → schema → PDF/DOCX → editor is the
  next change.
- Requirements come from the JD analysis's required/preferred skill lists, so
  non-skill requirements (years, degree, scope) are not checked here.
  `jd_profile` remains the only JD reader; the archetype is read from the JD
  analysis title, not from raw text.
- No frontend yet: the sidecar ships on the result and in the stored record.

Tests: `tests/test_screener_sim.py`.
