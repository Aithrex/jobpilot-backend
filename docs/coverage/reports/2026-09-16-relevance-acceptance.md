# Relevance acceptance — live top-25 (2026-09-16 19:15 UTC)

Account: `cpulikan@asu.edu`
Parsed résumé via `resume_access.get_active_structured`: **yes**
Work-authorization profile status: `unknown`
Window: 80 scored rows; tables show the top 25 of each ranking.

> Live Mongo is read-only. **Before** = `work_authorization_status=unknown`
> (no home-country demotion, no sponsorship demotion). **After** =
> `needs_sponsorship_now` (home_country=US + sponsorship/clearance policy).

## Resolver spot-check (handoff failures)

- `CDMX` → `MX` (known_foreign_city)
- `Bangkok MSG Headquarters` → `TH` (known_foreign_city)
- `DE-CELLE-BAKER-HUGHES-STRASSE 1` → `DE` (foreign_address_marker)
- `Remote - UK` → `GB` (remote_country)
- `India Remote` → `IN` (remote_country)
- `Sydney CBD Area` → `AU` (known_foreign_city)

## A. Work-location dimension

Foreign verdict in top-25: **before 0 → after 0**
Non-US primary country in top-25: **before 9 → after 0**
Rows from the before-top-25 that sank below #25 after demotion: **9**
Non-US cards that were in the before top-25: **9**

### Before (no home-country demotion)

| # | match | rank | employer | title | location |
|---|------:|-----:|----------|-------|----------|
| 1 | 89 | 0.8505 | Guidewire | AI Engineer- Applied AI | US/unknown · United States - Remote |
| 2 | 88 | 0.8425 | Cisco | Data Engineer | unknown/unknown · 2 Locations |
| 3 | 81 | 0.8145 | TD Bank | Data Scientist II | CA/unknown · Markham, Ontario |
| 4 | 81 | 0.8145 | Waymo | 2027 Summer Intern, PhD, Machine Learning, Compu | US/unknown · Mountain View, California, United States |
| 5 | 77 | 0.7965 | eBay | Data Scientist | IN/unknown · Bengaluru, India |
| 6 | 77 | 0.7965 | Mastercard | Data Scientist II | CA/unknown · Vancouver, Canada |
| 7 | 79 | 0.796 | VusionGroup | Engineering Intern | US/unknown · Dallas, TX |
| 8 | 76 | 0.792 | Commonwealth Bank of Austral | Chapter Lead - AI - Home Buying (Lead Data Scien | AU/unknown · Sydney CBD Area |
| 9 | 75 | 0.784 | Gartner | Associate Data Scientist | unknown/unknown · 4 Locations |
| 10 | 74 | 0.783 | Blue Yonder | Data Science Consultant II (Predictive Modeling, | IN/unknown · Bangalore |
| 11 | 74 | 0.783 | The Mutual Group | Data Science Intern - Summer 2027 | unknown/unknown · 2 Locations |
| 12 | 74 | 0.783 | Adobe | 2027 University Graduate - People Data Scientist | unknown/unknown · 2 Locations |
| 13 | 74 | 0.783 | EQT Corp | Data Engineering Intern | US/unknown · Canonsburg, PA |
| 14 | 74 | 0.7795 | Genpact | Lead Data Scientist 4D | IN/unknown · 1401-G-India: Floor 1 to 12, Hafeezpet P |
| 15 | 73 | 0.7785 | AUTO-OWNERS LIFE INSURANCE C | Business Intelligence Developer | US/unknown · Lansing, MI |
| 16 | 73 | 0.7785 | HP | AI /ML Engineer | ES/unknown · Sant Cugat del Valles, Barcelona, Spain |
| 17 | 72 | 0.774 | Gordon Food Service | Data Science Engineer Internship | US/unknown · Atlanta, Georgia |
| 18 | 72 | 0.774 | RELX | Applied AI Data Scientist | US/unknown · New York, NY |
| 19 | 72 | 0.774 | RELX | Applied AI Data Scientist | US/unknown · New York, NY |
| 20 | 72 | 0.7705 | Kyndryl | Early Career Consult Program - Associate AI Engi | unknown/unknown · USDALFRI - Dallas Frisco AI HUB (FRI) |
| 21 | 71 | 0.766 | Target | Data Analyst | IN/unknown · Bangalore,India |
| 22 | 70 | 0.765 | Adobe | Machine Learning Engineer | unknown/unknown · San Jose |
| 23 | 70 | 0.765 | Kyndryl | Early Career Consult Program - Associate AI Engi | unknown/unknown · USDALFRI - Dallas Frisco AI HUB (FRI) |
| 24 | 72 | 0.7645 | HedgeServ | Engineer II | PH/unknown · Manila |
| 25 | 72 | 0.7625 | Intel | Operations Research, Engineering Analytics Gradu | US/unknown · US, Arizona, Phoenix |

### After (`needs_sponsorship_now` → home_country=US)

| # | match | rank | employer | title | location |
|---|------:|-----:|----------|-------|----------|
| 1 | 89 | 0.8505 | Guidewire | AI Engineer- Applied AI | US/home · United States - Remote |
| 2 | 88 | 0.8425 | Cisco | Data Engineer | unknown/unknown · 2 Locations |
| 3 | 81 | 0.8145 | Waymo | 2027 Summer Intern, PhD, Machine Learning, Compu | US/home · Mountain View, California, United States |
| 4 | 79 | 0.796 | VusionGroup | Engineering Intern | US/home · Dallas, TX |
| 5 | 75 | 0.784 | Gartner | Associate Data Scientist | unknown/unknown · 4 Locations |
| 6 | 74 | 0.783 | The Mutual Group | Data Science Intern - Summer 2027 | unknown/unknown · 2 Locations |
| 7 | 74 | 0.783 | Adobe | 2027 University Graduate - People Data Scientist | unknown/unknown · 2 Locations |
| 8 | 74 | 0.783 | EQT Corp | Data Engineering Intern | US/home · Canonsburg, PA |
| 9 | 73 | 0.7785 | AUTO-OWNERS LIFE INSURANCE C | Business Intelligence Developer | US/home · Lansing, MI |
| 10 | 72 | 0.774 | Gordon Food Service | Data Science Engineer Internship | US/home · Atlanta, Georgia |
| 11 | 72 | 0.774 | RELX | Applied AI Data Scientist | US/home · New York, NY |
| 12 | 72 | 0.774 | RELX | Applied AI Data Scientist | US/home · New York, NY |
| 13 | 72 | 0.7705 | Kyndryl | Early Career Consult Program - Associate AI Engi | unknown/unknown · USDALFRI - Dallas Frisco AI HUB (FRI) |
| 14 | 70 | 0.765 | Adobe | Machine Learning Engineer | unknown/unknown · San Jose |
| 15 | 70 | 0.765 | Kyndryl | Early Career Consult Program - Associate AI Engi | unknown/unknown · USDALFRI - Dallas Frisco AI HUB (FRI) |
| 16 | 72 | 0.7625 | Intel | Operations Research, Engineering Analytics Gradu | US/home · US, Arizona, Phoenix |
| 17 | 70 | 0.7615 | General Motors | Data Engineer - Data Software Engineering and Cl | unknown/unknown · 2 Locations |
| 18 | 69 | 0.7605 | Sedgwick | Applied & Agentic AI Engineer | unknown/unknown · 30 Locations |
| 19 | 71 | 0.756 | eBay | Applied Researcher, Generative AI | unknown/unknown · Amsterdam |
| 20 | 68 | 0.756 | RELX | Machine Learning Engineer Lead | US/home · Raleigh, NC |
| 21 | 68 | 0.7525 | Huntington National Bank | Lead Data Scientist - Machine Learning Focus | US/home · Columbus, OH |
| 22 | 68 | 0.7525 | Roche | Data Scientist | unknown/unknown · Warsaw |
| 23 | 66 | 0.747 | Huron | Data Platform Engineer (Associate) | unknown/unknown · Chicago - 550 Van Buren |
| 24 | 66 | 0.7435 | Barclays | 2027 Quantitative Analytics Analyst Graduate Pro | US/home · New York, 745 7th Avenue |
| 25 | 66 | 0.7435 | Cleveland Clinic | EBI Database Developer II | unknown/unknown · Remote Location |

## B. Work-authorization demotion (probe query=`clearance`)

Demoted / unlikely-reachability in top-25: **before 3 → after 3**
3 demoted/unlikely in the probe after-window (query=`clearance`); first at #1: Anduril Industries / Quality Specialist- Clearance

Match scores are identical across before/after (ADR-002); rank scores drop
under `needs_sponsorship_now` (e.g. Anduril 0.50 → 0.16) and reachability
moves from `unknown` to `unlikely`. Badges (`clearance_required`,
`no_sponsorship`) render in both states — silence is never a mismatch, but
an explicit flag is always shown.

Default unscored feed (no query) for this account already had 0 clearance /
no-sponsorship cards in the top 80 — those roles do not lead an AI-new-grad
personalized ranking. The `clearance` probe below is the acceptance surface
that still carries the flags.

### Before (status=`unknown` — silence is never a mismatch)

| # | match | rank | employer | title | clearance / sponsorship |
|---|------:|-----:|----------|-------|------------------------|
| 1 | 40 | 0.5291 | GDIT | JMSC-P JLVC Database Technician – SECRET Clearan | clearance · sponsor=not_offered · clearance_required,no_sponsorship · unknown · DEMOTED |
| 2 | 35 | 0.5 | Anduril Industries | Quality Specialist- Clearance | clearance · clearance_required · unknown · DEMOTED |
| 3 | 35 | 0.4577 | ICF International, Inc. | Project Analyst - SETA Level 2, - Security Clear | clearance · sponsor=not_offered · clearance_required,no_sponsorship · unknown · DEMOTED |

### After (status=`needs_sponsorship_now`)

| # | match | rank | employer | title | clearance / sponsorship |
|---|------:|-----:|----------|-------|------------------------|
| 1 | 35 | 0.1631 | Anduril Industries | Quality Specialist- Clearance | clearance · clearance_required · unlikely · DEMOTED |
| 2 | 40 | 0.1556 | GDIT | JMSC-P JLVC Database Technician – SECRET Clearan | clearance · sponsor=not_offered · clearance_required,no_sponsorship · unlikely · DEMOTED |
| 3 | 35 | 0.1346 | ICF International, Inc. | Project Analyst - SETA Level 2, - Security Clear | clearance · sponsor=not_offered · clearance_required,no_sponsorship · unlikely · DEMOTED |

## Notes

- `match_score` is untouched in both dimensions (ADR-002).
- Unknown country is never treated as foreign; opt-in `hide_foreign_locations`
  drops only resolved-foreign rows.
- Clearance / no-sponsorship silence on a posting is never a mismatch.
- Amsterdam / London / Warsaw stay unresolved on purpose (US namesakes).
