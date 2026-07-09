# Compass spec → JobPilot reality: the gap map

> The Compass master prompt is the product north-star. JobPilot is the deployed
> implementation we are evolving (ADR-001). This maps every Compass surface to what
> already exists here, so we build the *gaps* and don't rebuild what ships.
>
> Legend: ✅ exists · 🟡 partial · 🔴 net-new. Service paths are in
> `jobpilot-backend/shared/python/services/` unless noted. Verify against current
> code before treating any single line as fact.

## Pillar 1 — Discover (jobs, matching, sponsorship)

| Compass §6.3 | JobPilot | State |
|---|---|---|
| ATS-first ingestion (Greenhouse/Lever/Ashby/Workday…) | `ats_direct_fetcher.py`, `workday_cxs_fetcher.py` | 🟡 fetchers exist; connector *fleet* + dedupe/simhash breadth is the gap |
| Hybrid retrieval + rerank + reason strings | `job_service`, `jd_match_scorer` (jobs svc), `match_gap_reporter` | 🟡 scoring + gap reasons exist; BM25+vector hybrid is keyword-leaning today |
| Sponsorship score from DOL LCA / USCIS | `visa_timeline_service`, sponsorship signals | 🟡 visa timeline exists; 5-yr LCA/USCIS score ingestion is the gap |
| Scam/ghost-job shield | `job_legitimacy_service` + `/api/jobs/trust-check` + JobTrustBadge | ✅ shipped |
| Filters (sponsors H-1B, E-Verify, CPT, clearance-exclude) | — | 🔴 net-new filter layer |

## Pillar 2 — Apply (extension + digest loop)

| Compass §6.4 | JobPilot | State |
|---|---|---|
| Chrome MV3 extension | `jobpilot-frontend/extension/` (Apply Assistant) | ✅ exists, wired to `apply.py` |
| Field fill + essay answers, grounded | `answer_generator.py` (jobs svc) | ✅ production-grade backend |
| 8-ATS adapters + Review Card + confidence/diff | extension | 🟡 needs adapter breadth + Review Card overlay |
| Fill/Review/One-Tap (no auto-submit) | design | ✅ human-approves is already the stance |
| Morning WhatsApp digest / notification fan-out | `daily_pipeline_service` | 🟡 digest logic exists; WhatsApp/SMS/push channel abstraction is the gap |

## Pillar 3 — Learn (differentiation → plan → proof)

| Compass §6.6 | JobPilot | State |
|---|---|---|
| Differentiation / gap analysis | `keyword_gap_engine`, `match_gap_reporter`, `learning_service` | 🟡 present; "scarce among this profile" selection rule to tighten |
| 7–14 day practical plan w/ acceptance criteria | `learning_service`, `project_generator` | ✅ largely present |
| GitHub proof verification | `github_profile_service`, github_connect | ✅ shipped (public API + connect) |

## Pillar 4 — Signal (LinkedIn posting + feedback)

| Compass §6.7 | JobPilot | State |
|---|---|---|
| Draft → approve → post (`w_member_social`) | `post_drafting_service`, linkedin_connect | ✅ shipped |
| Bandit over archetypes + cringe classifier | — | 🔴 net-new optimization loop |
| Post metrics (user-reported + extension-observed) | — | 🔴 net-new |

## Status detection & progress (§6.8)

| Compass | JobPilot | State |
|---|---|---|
| Mail ingress Track A (unique forward addr + webhook) | `gmail_service` (OAuth track) | 🟡 different approach (Gmail OAuth); Track A inbound-webhook is the gap |
| Status classifier (enum + confidence) | — | 🟡 build on existing LLM plumbing |
| Pipeline funnel + cohort benchmarks + ghost detection | daily pipeline / streak | 🟡 funnel partial; cohort + ghost are gaps |

## The "I'm adding these" set (§6.11)

| Feature | JobPilot | State |
|---|---|---|
| OPT Clock (first-class surface) | `visa_timeline_service` | 🟡 engine exists; calm dedicated surface is the gap |
| Recruiter's Six Seconds | — | 🔴 net-new |
| Referral Finder (from LinkedIn archive) | — | 🔴 net-new (needs archive importer §2.1) |
| Tailor-in-one-click | `resume_tailor`, `resume_versioning`, `resume_renderer` | ✅ present |
| Interview prep from JD | `interview_coach_service` (Pillar D) | ✅ present |
| Cohort benchmarks | — | 🔴 net-new |
| Ghost detection | — | 🔴 net-new |

## §6.12 Voice Graph / Authored Resume — 🔴 NET-NEW (current build)

The flagship net-new feature and the current focus. Depends on the resume parser
(spans) and the tailoring engine — both exist here (`resume_parser.py`,
`resume_tailor.py`). Built on Mongo + Flask per ADR-002. See the 10-step build order
in the addendum; `raw` mode ships before `authored`.

## Cross-cutting

| Compass | JobPilot | State |
|---|---|---|
| Never-fabricate / grounding firewall (§9.4) | `integrity_guard.py`, `fabrication_filter.py`, schema-validated LLM I/O | ✅ already the backbone |
| Entitlements / quota (§6.10) | `entitlements_service`, `usage_service` | ✅ present; Stripe deprioritized |
| Auth: Google OAuth + sessions | `oauth_service`, in-house JWT | ✅ present (email/password, not magic-link — divergence noted) |
| Consent ledger (§9.2) | `connect_oauth_states` pattern | 🟡 per-scope consent ledger to formalize (Voice Graph adds `voice_graph` scope) |
| Encryption at rest (per-user DEK) | Fernet via `*_TOKEN_ENCRYPTION_KEY` | 🟡 token encryption exists; extend to writing samples / resume blobs |

## Notable divergences from the Compass letter (intentional)

- **No passwords → JobPilot has password auth.** Not reworking auth now; magic-link
  is a later, isolated change.
- **Postgres/pgvector/FastAPI/Redis/Celery** → Mongo/Flask/Lambda self-invoke
  (ADR-001/002).
- **Stripe billing** is deprioritized (product is currently "not for money"); the
  entitlement *bones* exist and stay wired.
