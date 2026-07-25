# Auto-Apply Fill Engine — Question Bank, Normalizer & Onboarding

Status: **spec / build-to**. The refined goal behind "when the user opts into auto-apply, WE open the
link and fill the whole form in a cloud headless browser, 100% correct."

Primary path = **cloud headless executor** (Playwright on GCP Cloud Run / AWS Lambda fallback) driving
the real ATS form (modes `approval`/`autopilot`). The **extension is the lower-priority fallback**
(Copilot / manual). Both share ONE answer brain defined here.

## The core problem
1. Every ATS asks the SAME question with different wording ("Are you legally authorized to work in the
   US?" == "Do you have US work authorization?" == "Eligible to work in the United States?").
2. Some are **inverse polarity**: "Will you require sponsorship?" (Yes = needs sponsorship) vs "Are you
   able to work without sponsorship?" (Yes = does NOT need). A naive map submits the WRONG answer.
3. Demographic/EEO/legal answers must **NEVER be fabricated or inferred** (IntegrityGuard / spec §6).
4. Select/radio options are worded differently per form ("Prefer not to answer" == "Decline to
   self-identify" == "I don't wish to answer").

So: a canonical **field catalog** + a **normalizer** (recon field text → canonical `field_id`, with
negation detection) + a **resolver** (field_id → the user's onboarding-declared value) + an **option
matcher** (canonical value → this form's actual option text). Unknown / required-missing /
sensitive-unconfirmed → `needs_input` (pause, ask the user) — never guess.

## Canonical field catalog (`application_question_bank.py`)
Each field: `{field_id, label, category, sensitivity, value_type, widget_hint, aliases[], negations[],
options[], default_policy}`.

- `category ∈ {identity, work_auth, eeo, logistics, screening, legal, free_text}`
- `sensitivity ∈ {safe, sensitive, protected, legal}`
- `default_policy ∈ {from_profile, required_onboarding, confirm_default, decline_default, never_default}`

### identity (safe, from_profile — resume/verified profile)
full_name, first_name, last_name, email, phone, address_line1/2, city, state, postal_code, country,
linkedin_url, portfolio_url, github_url, website_url.

### work_auth (required_onboarding — factual, NEVER guessed)
- `work_authorized_us` (Yes/No) — aliases: "authorized to work in the US", "legally authorized",
  "eligible to work in the United States", "have work authorization".
- `requires_sponsorship` (Yes/No) — aliases: "require sponsorship now or in the future", "need visa
  sponsorship", "require immigration sponsorship". **negations** (invert stored value): "able to work
  WITHOUT sponsorship", "do NOT require sponsorship", "without needing sponsorship".
- `visa_status` (Citizen/PermanentResident/F1-OPT/H1B/…), `work_auth_country`.

### eeo (protected, decline_default — voluntary; default is the form's DECLINE option; user may set a
real value at onboarding; NEVER inferred from name/photo/anything)
- `gender` — Male/Female/Non-binary/Decline. aliases: "gender", "sex", "gender identity".
- `race_ethnicity` — US EEO set: Hispanic or Latino / White / Black or African American / Asian /
  American Indian or Alaska Native / Native Hawaiian or Other Pacific Islander / Two or More Races /
  Decline. aliases: "race", "ethnicity", "race/ethnicity".
- `veteran_status` — Protected Veteran / Not a protected veteran / Decline. aliases: "veteran status",
  "protected veteran", "military service".
- `disability_status` — Yes / No / Decline (the CC-305 voluntary self-ID). aliases: "disability",
  "disability status", "do you have a disability".

### logistics (confirm_default — sensible default the user confirms at onboarding)
willing_to_relocate, remote_preference (Remote/Hybrid/Onsite/Flexible), desired_salary,
current_salary (decline_default — pay-history bans), notice_period, earliest_start_date,
currently_employed, willing_to_travel, how_did_you_hear (default "Company website"), referral_name.

### screening (from_profile / confirm_default)
years_experience (general + per-skill via a sub-map), highest_education, degree, field_of_study, school,
graduation_year, gpa, certifications, has_security_clearance (decline_default), has_drivers_license.

### legal (legal, never_default — REQUIRE an explicit user value each time or an onboarding-declared one;
NEVER auto-affirm)
over_18 (confirm), felony_conviction / criminal_history (**always needs_input unless user declared**;
respect ban-the-box), background_check_consent, drug_test_consent, non_compete,
can_provide_work_eligibility_proof.

### free_text (LLM-generated, grounded — never fabricates facts; IntegrityGuard)
cover_letter, why_company, why_you, additional_info.

## Normalizer (recon field → field_id)
1. Deterministic: lowercased alias/keyword + regex match against the catalog (fast, high-precision).
2. Negation pass: if the field text matches a `negations[]` pattern, set `invert=True` for Yes/No.
3. LLM fallback (FLASH, closed schema, untrusted-input-hardened): only for fields the deterministic
   layer can't map; returns a `field_id` from the CLOSED catalog or `unknown`. The model CLASSIFIES;
   it never supplies the answer value.
4. Confidence + evidence on every mapping; low-confidence → treat as unknown → `needs_input`.

## Resolver + option matcher
- Resolve `field_id` → the user's onboarding value from the versioned profile (the declared store the
  answer library reads). Apply `invert` for negated Yes/No.
- Option match: fuzzy-map the canonical value to THIS form's option list (synonyms for decline, yes/no
  variants, EEO wording). No confident option match on a required select → `needs_input`.
- Sensitivity gate (hard): a `protected`/`legal` field with no explicit user-declared value →
  `needs_input`, never a guess. A user-declared "decline" IS an explicit value and may be used.

## Onboarding profile (ask once, smart defaults)
Backend: `GET /api/apply/onboarding/questions` → the catalog grouped by category with the user's current
value, `required`, `sensitivity`, `default`, and `missing` flags. `PUT /api/apply/onboarding/answers`
persists to the versioned profile (reuse `application_profiles`/the declared store). Required set
(contact + work_auth + sponsorship) blocks auto-apply until filled; EEO/legal default to decline but are
editable; logistics/screening pre-filled from resume + sensible defaults for one-tap confirm.
FE: an onboarding flow that collects these (grouped, progressive), plus the mode picker (Copilot/
Approval/Autopilot). "Most-predicted default" = the safe/common value (decline for protected/legal,
resume-derived for identity/screening, common choice for logistics) — the user confirms/edits.

## Executor integration (P4 adapters + P5 worker)
recon() → normalize each field → resolve → option-match → deterministic fill → validation capture →
review snapshot → submit (per mode) → confirmation → receipt. Any `needs_input` (unknown / required
missing / sensitive unconfirmed / no option match / CAPTCHA / MFA / account required / novel legal) →
pause to `needs_input`/`waiting_human`, surface the exact field to the user, never fabricate.

## Definite tasks
- **QT1** Question bank + normalizer + option matcher + negation handling + tests (data-scientist).
- **QT2** Onboarding endpoints + versioned profile + FE onboarding flow (backend + frontend).
- **QT3** Wire the executor/adapters (P4/P5) to normalize→resolve→fill via QT1; needs_input on any gap.
- **QT4** Rigorous fixture E2E: every ATS × every question category, polarity flips, option-mismatch,
  multi-page, validation errors, selector drift, CAPTCHA/MFA takeover, post-click timeout; loop as
  tester/developer/end-user (qa). Owned fixtures only — never real employers.

## Non-negotiables
Never infer gender/race/veteran/disability/salary-history/criminal-history/clearance/legal attestations.
Sensitive/legal unconfirmed → needs_input. Model classifies/drafts; CODE fills/submits. Every filled
answer carries {field_id, question_text, value, source, source_version, confidence, review_required}.
