# What fit-v2.1 did to the score distribution

Measured 2026-09-02 against live `aspirely_db`, for
`harshith.manne.jobs@gmail.com` — the account the brief names, whose complaint
was that only five or six postings ever scored above 80.

Sample: the 2,500 most recently posted ACTIVE core-tech postings that carry a
parsed `requirements` profile. `fit-v2.0` is reconstructed from the same
verdicts by paying each unmeasurable component the neutral fraction 2.0 gave
it (role 0.60, required skills 0.60, experience 0.65, preferred 0.60, domain
0.60, location 0.60, education 0.80) and applying the same caps — so the two
columns differ only by the change under test.

Reproduce: `scripts/employer_coverage_report.py` does not do this; the script
is in the session log and the numbers below are the output.

| band | fit-v2.0 | fit-v2.1 |
|---|---:|---:|
| >= 90 | 0 (0.0%) | **2 (0.1%)** |
| >= 85 | 3 (0.1%) | **12 (0.5%)** |
| >= 80 | 30 (1.2%) | **57 (2.3%)** |
| >= 75 | 95 (3.8%) | **172 (6.9%)** |
| >= 70 | 316 (12.6%) | 353 (14.1%) |
| >= 65 | 456 (18.2%) | 468 (18.7%) |
| >= 50 | 1,561 (62.4%) | 1,470 (58.8%) |
| mean | 50.1 | 49.3 |
| max | 88 | **95** |
| distinct values | 72 | **82** |

## The part that matters

**The mean did not move.** 50.1 to 49.3. This is not a scoring engine that got
more generous; it is one that started discriminating. The top band roughly
doubled while the `>= 50` band shrank, because the same change that lifted
genuinely-matching roles pushed unrelated ones down.

Under 2.0 this candidate's ceiling was 88 and NOTHING could reach 90. That was
not a fact about the job market. It was arithmetic: every component paid a
"neutral" fraction when it had no evidence, and a neutral fraction is a
deduction. On live data 96.1% of postings state no work mode, 43.3% state no
seniority and 81.5% state no salary, so essentially every posting lost the same
~12 points for gaps in OUR parsing. A candidate cannot out-run that by being a
better match.

2.1 averages only the components that had evidence on both sides
(`_normalized_score`), so an unmeasurable component is excluded from the
numerator AND the denominator rather than priced.

## Where the discrimination came from

Alignment tiers over 4,000 core-tech postings for the same account:

| tier | postings |
|---|---:|
| `exact_specialty` | 1,486 |
| `adjacent_specialty` | 815 |
| `adjacent_family` | 540 |
| `same_family` | 363 |
| `unrelated` | 796 |

Under 2.0 this 25-point component read one of two values for almost this whole
set, because its finest software distinction was the single family
`software_engineering`. A component that is nearly constant contributes
nothing to an ordering, so ranking collapsed onto skill overlap alone. The
five-rung ladder over 14 families / 102 specialties is what separates them.

796 postings came back `ineligible` — a confident cross-domain or hard-
constraint finding, not a low score — and they are ranked below every posting
the candidate can actually take.

## The résumé side

The account has six parsed résumés. The canonical classification for the set:

```
role_families      ['software_engineering', 'cloud_platform']
role_specialties   ['general_software', 'devops', 'backend']
tech_scope         core
confidence         0.93
```

Before 2.1 the résumé side of the comparison was the 32-key legacy list
(`{'devops', 'software_engineering'}`), so a backend résumé and an ASIC
verification résumé scored identically against a backend posting. Both sides
now speak the same 14/102 vocabulary, which is what makes the ladder
meaningful.

---

# Addendum, 2026-09-02: the veto was eating a fifth of the feed

Re-measured after the v4 normalisation backfill, same account, same 2,500-row
sample. The backfill itself changed almost nothing (>= 75 moved 172 -> 174),
which is the right answer: recovering 11,653 seniority bands gave the
`level_gap` component evidence it did not have, and `_normalized_score` was
already excluding it rather than pricing it.

What the re-measurement DID surface is a defect, and a costly one.

## 522 of 2,500 core-tech postings were `ineligible`

`ineligible` is not a low score. It removes the row from the ranked feed
entirely. Broken down by what the veto rejected:

| rejected family | postings | examples |
|---|---:|---|
| `technical_leadership` | 317 | Senior Software Engineer - AI Products (SVP) @ Citi · Staff Engineer @ Stryker · Associate Tech Lead @ Trane |
| `security` | 161 | Information Security Engineer II @ First Citizens · Security Architect · Sr Security Engineer @ Wells Fargo |
| `semiconductor_hardware` | 44 | CPU Verification Engineer @ Intel · Sr Principal Analog Design @ NXP |

The résumé is software engineering and cloud platform. A software engineer
being told they may not apply for "Staff Engineer" is not a screening result;
it is a bug with a confident voice.

## Two causes, both now fixed

**1. `cross_domain` meant the wrong thing.** `role_alignment` set
`cross_domain: bool(rf_all and jf_all)` on its `unrelated` return — i.e. any
two technology families the adjacency matrix does not connect. `fit_score.
_eligibility` turns that flag into INELIGIBLE. But the flag is documented, in
the same function, as the teacher-versus-backend case: a change of
occupational DOMAIN. Two technology families are still two technology
families, and `unrelated` already carries a 0.08 weight — a severe penalty on
its own. The flag is now set only by the two scope-level branches, and the
mirror branch (a technical résumé against a non-tech posting) was added, since
that case previously fell through to `unknown` (0.60, neutral).

**2. The adjacency matrix had no edge into leadership or security.** The
`technical_leadership` family connected only to itself and to
`distributed_systems`, so the specialty bridge could not reach it from
`backend`, `full_stack`, `platform_engineering` or `cloud_engineering` — the
specialties leadership roles are *staffed from*. Security had the same hole.
`tech-adjacency-1.1.0` declares 33 further pairs: IC specialties to
`principal_staff_ic` and `architecture`, software specialties to
`application_security` / `product_security`, and cloud specialties to
`iam_pam` / `cloud_security` / `secops`. `engineering_management` is
deliberately left with no direct IC pair — it is reachable at the
adjacent-family rung (0.46) through the bridge, which is the honest distance.

## After

| band | before the fix | after |
|---|---:|---:|
| ineligible | 522 (20.9%) | **0** |
| >= 90 | 2 | 2 |
| >= 85 | 12 | 12 |
| >= 80 | 57 | 59 |
| >= 75 | 174 | 178 |
| >= 70 | 354 | 357 |
| >= 65 | 470 | 480 |
| >= 50 | 1,470 (58.8%) | **1,696 (67.8%)** |
| mean | 49.4 | 52.9 |
| max | 95 | 95 |

**The top bands did not move.** 57 -> 59 at >= 80. Nothing was inflated to
make the feed look better: 522 postings stopped being hard-excluded and now
compete on their merits, and they land in the middle of the distribution
because that is where they belong. The `>= 50` band absorbed almost all of
them.

Reproduce: the sampling script is in the session log; the taxonomy assertions
are pinned in `tests/test_cross_domain_veto_scope.py` and the per-stratum
precision/recall in `evals/eval_tech_taxonomy.py` is unchanged by the edit.
