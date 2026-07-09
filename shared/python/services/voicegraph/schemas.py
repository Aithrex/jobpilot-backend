"""Schemas + taxonomy for graph extraction (§6.12.3).

Uses the project's Python-DSL schema (converted by python_schema_to_json_schema
and enforced via Claude tool-use). Enums are NOT expressible in the DSL, so node
`type` and edge `rel` are constrained in the prompt and re-validated in code
against the sets below (unknown types/rels are dropped, not trusted).

Every node and edge MUST carry a `quote` — the exact substring it was drawn from —
plus its char offsets. The quote is verified against the source text before the
row is persisted; a quote that is not a real substring means a hallucinated span,
which rejects the whole extraction (§6.12.3 rule / evals/graph_span_fidelity).
"""

NODE_TYPES = {
    "Self", "Organization", "Role", "Project", "Artifact", "Technology", "Skill",
    "Problem", "Decision", "Constraint", "Outcome", "Metric", "Domain",
    "TimePeriod", "Collaborator",
}

EDGE_RELS = {
    "WORKED_AT", "HELD_ROLE", "BUILT", "USED", "FACED", "CHOSE", "CONSTRAINED_BY",
    "PRODUCED", "MEASURED_BY", "IMPROVED", "OWNED", "MENTORED", "LEARNED_FROM",
    "SHIPPED_TO",
}

# Python-DSL schema for the graph_extract_v1 tool call.
GRAPH_EXTRACT_SCHEMA = {
    "nodes": [{
        "temp_id": str,        # local id the model assigns, referenced by edges
        "type": str,           # one of NODE_TYPES (validated in code)
        "label": str,          # short human label
        "role": str,           # Collaborator ONLY: role, never a personal name; else ""
        "quote": str,          # exact substring of the source the claim rests on
        "char_start": int,
        "char_end": int,
        "confidence": float,
    }],
    "edges": [{
        "src": str,            # temp_id of source node
        "dst": str,            # temp_id of destination node
        "rel": str,            # one of EDGE_RELS (validated in code)
        "quote": str,
        "char_start": int,
        "char_end": int,
        "confidence": float,
    }],
}
