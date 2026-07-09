# ADR-003: Microservices via bounded contexts + selective worker extraction

- Status: Accepted
- Date: 2026-07-09
- Deciders: Product owner, Staff Eng
- Related: ADR-001 (evolve JobPilot), master prompt §4

## Context

The product owner asked why the backend isn't split into more services ("create
one for learning, one for the human-generated features, and every other segregated
feature"). The current topology is 4 Lambdas (auth · jobs · resume · billing) that
are thin routers over a shared layer where each feature is its own bounded service
module.

## Decision

**Keep the service-oriented modular monolith. Make bounded contexts explicit in
code, and extract as separate deployables only the pieces with genuinely different
failure/scaling domains — the background/scheduled workers. Do NOT fragment into
one-Lambda-per-feature.**

Concretely:
- The code is **already segregated by domain** (per-feature service modules). The
  "more microservices" question is about *deployment topology*, not code structure.
- A new request Lambda (`/api/<prefix>`) needs an **external Terraform edge route**
  — and there are **no `.tf` files in this repo** (that infra lives elsewhere). So
  a new HTTP service 404s in prod until an operator adds the route. Hard human gate.
- The **async-job engine (self-invoke) lives only in the resume Lambda**. A new
  request Lambda must replicate it or call across Lambdas — the classic
  *distributed monolith* anti-pattern.
- For a **pre-launch product with no traffic**, per-feature Lambdas buy little
  (independent scale/failure isolation) at real cost (cold starts, IAM, deploy
  surface, edge routes).

Where separate deployables genuinely earn their keep here is the **workers** —
different failure/scaling domains, not on the request path: the notification-digest
scheduler, the mail-ingress inbound webhook, the nightly bandit update, the
sponsorship ingest, and the learning proof-of-work verification. Those are the
extraction candidates (master prompt §4 named `services/ingestion` + `services/notifications`).

## What we do

1. New domain-heavy features are built as **clean bounded-context packages** (e.g.
   `shared/python/services/learning/`, `voicegraph/`, `notifications/`), so a later
   extraction is a change of transport, not a rewrite.
2. We **do not** churn the existing 40 service modules into packages (that would
   touch hundreds of import sites on a deployed product for no user value).
3. We keep a **deploy-ready `services/learning/` Lambda skeleton** (mirrors the 4
   existing services) so the operator can extract the learning domain with one
   Terraform route + one `deploy.yml` filter entry — proving §4's "extract = change
   of transport."

## Extraction roadmap (each = a transport change, do when scale justifies)
1. **Workers first** — digest scheduler, mail-ingress inbound, nightly bandit,
   sponsorship ingest, learning verifier → EventBridge/queue-triggered Lambdas.
2. **learning / growth** — the learning loop + voice-graph/six-seconds/signal
   ("the human-generated features") → a `growth` Lambda under `/api/learning` or
   `/api/growth`.
3. **discover** — sponsorship + filters + matching → a `discover` Lambda.

## Consequences
- Best-practice for this stage; reversible; nothing inert ships.
- The bounded-context discipline (own tables, no cross-domain joins, thin
  blueprints) is the mechanical enforcement of the boundary — keep it.
- Full deployment-level microservices remain a documented, low-effort operator
  step, not a rewrite.
