# ADR-001: Evolve JobPilot; do not greenfield "Compass"

- Status: Accepted
- Date: 2026-07-09
- Deciders: Product owner (harshith@aithrex.tech), Staff Eng
- Supersedes: none

## Context

A "Compass — Career OS for International Students" master build prompt was handed
in as a greenfield spec with a `DECISION LOCKED` stack: Next.js 15 · FastAPI ·
Postgres 16 + pgvector · Redis · Celery · Anthropic direct API · pnpm/uv/Turborepo
monorepo.

The environment (`D:\Aithrex`) already contains **JobPilot (Aithrex)** — the *same
product thesis* (career OS for international students), ~80% built and **deployed**
to AWS via CI/CD, on a *different* stack:

| | Compass spec | JobPilot (deployed) |
|---|---|---|
| Frontend | Next.js 15 | Vite 5 + React 18 SPA |
| Backend | FastAPI async | Flask, 4-Lambda microservice split |
| DB | Postgres + pgvector | MongoDB (`portfolio_db`), raw PyMongo |
| LLM | Anthropic direct | Claude on AWS Bedrock (`bedrock-runtime`) |
| Repo | one monorepo | two git repos (`Aithrex/jobpilot-{frontend,backend}`) |

Most of Compass already exists in JobPilot under different names (see
`docs/compass/GAP-MAP.md`): resume parse/tailor/score, sponsorship/visa signals,
ATS fetchers, a shipped Chrome MV3 extension, learning engine, GitHub proof,
LinkedIn `w_member_social` posting, an entitlements/quota layer, and — critically —
the exact never-fabricate guardrails Compass §9.4 demands (`integrity_guard.py`,
`fabrication_filter.py`).

## Decision

**Adopt the Compass document as the product north-star (spec), and deliver it by
evolving the deployed JobPilot stack — not by rebuilding greenfield.**

The Compass "DECISION LOCKED" *tech stack* is treated as **spec intent, not infra
mandate**, because it was authored without knowledge that a deployed product exists.
Where Compass names an infra choice that only makes sense in the greenfield world
(FastAPI, Postgres, Turborepo, Celery, Redis-as-queue), we map it to the JobPilot
equivalent that already runs in production. Where Compass names a *product* behavior
(never-fabricate, evidence spine, human-approves-before-submit, OPT clock,
authored-not-undetectable), we build it faithfully.

The master prompt's own operating rule §0.2 governs exactly this unanticipated fork:
"pick the option that is (a) reversible, (b) cheaper to operate, (c) safer for user
data." Rebuilding a deployed product is the irreversible, expensive option; evolving
it is the reversible, cheap one. The product owner confirmed this choice explicitly.

## Consequences

- **Kept:** Flask 4-Lambda split, MongoDB, Claude-on-Bedrock provider abstraction,
  the shared Lambda layer + PEP-420 namespace-package convention, the existing
  extension, the AWS CI/CD pipeline, all hard invariants in the two `CLAUDE.md`s.
- New features mount under **existing** `/api/<prefix>` routes where possible (e.g.
  `/api/resume/...`) to avoid needing new external Terraform edge routes.
- Long LLM work uses the existing **async-job** pattern (`resume_service._process_job`),
  never inline.
- All new work ships **feature-flagged, default-off in prod**, so committing to
  `main` (which auto-deploys) stays safe.
- Individual Compass infra deviations are recorded in their own ADRs (e.g. ADR-002
  for the Voice Graph storage/runtime choice).
- The "impossible as specified" constraints in Compass §2 (LinkedIn read scope,
  auto-submit, Gmail restricted scope, WhatsApp templates, "every US company") are
  inherited unchanged — JobPilot already lives within most of them.

## Reversibility

High. Nothing here forecloses a later migration to the Compass stack; it defers it.
The evidence-spine and provider abstractions keep business logic portable if a
Postgres/FastAPI move is ever justified.
