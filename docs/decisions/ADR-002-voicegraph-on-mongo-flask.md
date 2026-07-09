# ADR-002: Voice Graph runs on MongoDB + Flask (not Postgres + FastAPI)

- Status: Accepted
- Date: 2026-07-09
- Deciders: Product owner, Staff Eng
- Related: ADR-001, Compass addendum §6.12

## Context

Compass addendum §6.12 ("Voice Graph and Authored Resume") specifies:

- Storage: **Postgres** — with an explicit "not Neo4j" rationale: traversal is
  2–3 hops over a few hundred nodes per user; recursive CTEs suffice; a graph DB
  "buys operational cost and nothing else."
- Runtime: the FastAPI/Postgres monorepo of the master prompt.

Per ADR-001 we are evolving JobPilot, which is Flask + MongoDB + Claude-on-Bedrock.
The product owner delegated the framework call: "if you feel fastapi makes more
sense change… it's your call."

## Decision

**Build the Voice Graph on the existing MongoDB + Flask stack.**

1. **Storage → MongoDB, not Postgres.** The addendum's *own* rationale for rejecting
   Neo4j applies verbatim to the Postgres-vs-Mongo choice here: a few hundred nodes
   per user, 2–3 hop bounded traversal, no unbounded path search. `$graphLookup`
   plus trivial app-level traversal covers every query in §6.12. Adding Postgres to
   a Mongo product would re-introduce exactly the "operational cost and nothing else"
   the addendum warns against. The relational DDL in §6.12.3/§6.12.4 is realized as
   Mongo collections with the identical fields and the identical **hard invariants**
   (evidence-span-required-at-insert enforced in the repository layer, not a prompt).

2. **Runtime → Flask, not FastAPI.** Switching frameworks contradicts ADR-001
   (evolve, not rebuild). The feature slots into the existing `resume` Lambda:
   routes under `/api/resume/voicegraph/*` (rides the existing edge route), long
   work via the existing async-job pattern, LLM via `services.llm_providers`.

3. **`vector(1024)` embeddings** in the schema become float arrays stored on the
   Mongo docs; similarity is computed in Python (cosine) over the small per-user
   candidate set for entity resolution. No pgvector, no ANN index needed at this
   scale (per-user, dozens-to-hundreds of nodes).

## Collections (Mongo realization of §6.12.3 / §6.12.4)

- `writing_samples` — `{_id, user_email, text_encrypted, word_count, source,
  consent_version, created_at}` (text Fernet-encrypted at rest, §9.2).
- `graph_nodes` — `{_id, user_email, type, label, canonical_label, embedding[],
  confidence, user_confirmed, first_seen_at}`.
- `graph_edges` — `{_id, user_email, src_id, dst_id, rel, confidence, user_confirmed}`.
- `graph_evidence` — `{_id, target_type, target_id, source, source_id, char_start,
  char_end}` (**required**; a node/edge with no evidence row is rejected at insert).
- `style_profiles` — `{_id, user_email, version, features, verb_inventory,
  anti_patterns[], sample_ids[], computed_at}`.

`user_email` (the JWT identity) scopes every document, consistent with the rest of
the backend. Compass §5 "Row Level Security" maps to disciplined `user_email`
filtering + `InputSanitizer` on every Mongo filter (no ORM/RLS in this stack).

## What does NOT change from the addendum

Every *product* and *integrity* rule is honored exactly:
- No detector-evasion objective anywhere (see `docs/product/authentic-not-undetectable.md`).
- Evidence-span-required-at-insert, enforced in code.
- Third-party proper-noun scrubbing before storage.
- Entailment gate at runtime + CI; anti-pattern post-filter; `raw` mode with zero
  model tokens; the revocation cascade; the provenance report; all §6.12.9 evals.

## Consequences

- Cross-domain Voice Graph services live in `shared/python/services/voicegraph/`
  (a shared-layer change redeploys all four Lambdas — acceptable, it is new code
  behind a default-off flag).
- No new external Terraform edge route required (mounts under `/api/resume`).
- If per-user graphs ever grow past what in-process cosine handles (they will not
  at this product's shape), revisit — the port boundary keeps that reversible.
