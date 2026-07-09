# Voice Graph — Ship Readiness (§6.12.9)

Status of the offline eval suite and the remaining human gates before
`VOICE_GRAPH_ENABLED` can be flipped in prod. **The flag flip is an operator
decision** — nothing in this document or in CI flips it, and nothing here may
be marked done on the operator's behalf.

## How to run

```
pip install -r shared/python/requirements.txt -r requirements-dev.txt
PYTHONPATH=shared/python pytest tests evals -q
```

CI: `.github/workflows/tests.yml` runs the same command on every push/PR with
**no AWS credentials and no secrets** — mongomock stands in for Mongo, a
FakeLLM stands in for Bedrock (patched at every import site), fpdf2/pypdf do
the ATS roundtrip locally. The legacy plain-python entrypoint still works:
`PYTHONPATH=shared/python python tests/test_voicegraph_cascade.py`.

> Deploy note: `deploy.yml`'s detect-changes filter treats
> `.github/workflows/**` as a shared-layer change, so merging the new
> workflow file triggers **one full 4-service redeploy**. Accepted.

## §6.12.9 eval matrix

| §6.12.9 eval | File | Status |
|---|---|---|
| Resume grounding / graph span fidelity — no node/edge without verified evidence; quotes must be exact substrings; hallucinated spans reject the whole extraction; offsets self-heal; unknown types/rels dropped | `evals/test_resume_grounding.py` | **automated-offline** |
| Entailment gate — non-entailed candidates dropped; B⊨A **and** A⊨B both required (added fact kills a "yes"); missing verdict = deny; judge failure discards all; zero survivors → verbatim passthrough; fabricated employer/volume blocked end-to-end; immutable fields copied through exactly | `evals/test_entailment.py` | **partially-covered + gap** — the gate *logic* and every fallback are pinned offline with canned verdicts; the *accuracy of the real PRO judge's verdicts* is runtime-only (cannot be measured without Bedrock). |
| Anti-pattern leak — unused clichés banned, used ones kept; Step 4 hard code filter removes every banned token (any casing, hyphenated) before the judge ever sees a candidate; word-boundary semantics | `evals/test_anti_pattern_leak.py` | **automated-offline** |
| voice_identity — `style.distance()` separation: held-out same-author text strictly closer than every cross-author text, all rotations, with a ≥1.15× margin; real `build_profile` pipeline preserves identity; <150-word profiles honestly refused | `evals/test_voice_identity.py` | **partially-covered + gap** — proven on a 3-author synthetic fixture corpus (worst measured margin 1.30×). Calibration on real user corpora within the same role family remains runtime/data work (partner: data-scientist). |
| ats_roundtrip — structured resume → fpdf2 PDF → pypdf extraction; name/email/employers/titles/skills/date tokens all findable; sections present and in order; no glyph corruption; score ≥ 90; checker fails loudly on garbage | `evals/test_ats_roundtrip.py` | **automated-offline** |
| Injection (§9.4) — writing samples wrapped as untrusted data, never in the `system` channel; an "obeyed" injection cannot mint ungrounded facts (span verification kills it); injected text can only enter as verbatim-quoted evidence; generation + judge keep the same trust boundary; scrub redacts third-party names, keeps tech terms; Collaborator nodes role-only | `evals/test_injection.py` | **partially-covered + gap** — the code-level contract (channel separation + span verification + scrub) is fully pinned; whether the *live model* resists a novel jailbreak is inherently runtime-only. The design assumes the model can be fooled and blocks the damage in code. |
| Evidence invariant + revocation cascade (§6.12.3/§6.12.8) — co-evidenced nodes survive revoke, sample-only rows die, resumes frozen not deleted, merge candidates purged | `tests/test_voicegraph_cascade.py` (pre-existing, now also CI-collected) | **automated-offline** |

## Known gap surfaced by the suite — FIXED

The suite surfaced that `ResumeParser()` was constructed with **no arguments**
(its `__init__` requires a `db` handle) at seven call sites, with the
`TypeError` swallowed by defensive fallbacks — so the parsed-resume Pass A
path, resume co-evidence in `resolution._resume_records`, the six-seconds
resume lookup, the outreach draft grounding, and the LinkedIn audit endpoint
either silently degraded or hard-failed.

**Status: fixed in-repo** — every site now constructs `ResumeParser(db)` with
a real database handle (`DBConnect().get_db()` or the module's `_db()`
helper); `grep -rn "ResumeParser()"` over `shared/` and `services/` returns
zero no-arg constructions. The evals still stub the parser at its import
boundary for determinism; that stubbing is now belt-and-braces, not a mask.

## Human gates before flipping `VOICE_GRAPH_ENABLED` in prod

These are HUMAN gates (see `MANUAL_STEPS.md`). None of them is done, and none
may be faked or auto-checked:

| Gate | What must happen | Status |
|---|---|---|
| **Recruiter-panel blind review** | The `recruiter-panel` blind human review: authored mode must beat assisted AND beat the original resume before go-live. This is the §6.12.9 ship gate proper. | **NOT DONE — human panel required. Do not flip without it.** |
| **SSM encryption key verify** | Confirm the resume Lambda resolves a Fernet key: `INTEGRATION_TOKEN_ENCRYPTION_KEY` is not in the SSM param map today, so it falls back to `GMAIL_TOKEN_ENCRYPTION_KEY` — verify that key is present (or add the integration key to `utils/ssm_config.py`). Sample save fails without it. | Pending operator verification |
| **Prod index migration** | Run `python scripts/migrate_indexes.py` against prod Mongo (writing_samples, consents, graph_nodes/edges/evidence, style_profiles, authored_resumes). Until then those queries COLLSCAN. | Pending one-time run |
| **Opus-tier upgrade decision** | Extraction/authored run on PRO (Sonnet 4.6), the top wired Bedrock tier. The addendum names `claude-opus-4-8`; using it requires enabling the model in Bedrock and adding a tier id (ADR-002). | Open decision — optional |

When the evals are green in CI **and** every gate above is satisfied, the
operator may set `VOICE_GRAPH_ENABLED=true` on the resume Lambda (env or
SSM). Default remains OFF in prod, ON in local dev.
