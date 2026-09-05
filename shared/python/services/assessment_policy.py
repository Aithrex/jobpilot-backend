"""Assessment-v2 policy: rubric pin, severity, AND/OR requirement reducer.

Deterministic only — no LLM calls. Used by Package 4+ evaluators; Package 1
locks the truth table so later stages cannot silently change counting rules.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Literal, Optional, Sequence

from schemas.assessment_contracts import (
    ASSESSMENT_VERSION,
    DocumentState,
    RequirementExpression,
)

# Rubric / policy pin (bump when severity or reducer semantics change).
POLICY_VERSION = ASSESSMENT_VERSION
RUBRIC_VERSION = "assessment-v2"

Severity = Literal["critical", "material", "advisory", "informational"]

# Evidence expectation policy: which document surfaces may satisfy a predicate.
EVIDENCE_EXPECTATIONS = frozenset({
    "document_or_skills",
    "listed_skill_ok",
    "production_use",
    "ownership",
    "duration",
    "credential",
})

# Severity defaults by finding kind (Package 5 may refine per case).
DEFAULT_SEVERITY: Dict[str, Severity] = {
    "absence": "material",
    "wording": "advisory",
    "relationship": "critical",
    "visual": "material",
    "strength": "informational",
    "clarity": "advisory",
    "integrity": "critical",
}


@dataclass(frozen=True)
class LeafObservation:
    """Observed document state for one atomic capability key."""

    capability: str
    document_state: DocumentState
    # Optional: whether this leaf could still flip if more evidence arrives.
    unresolved: bool = False


def severity_for(kind: str, override: Optional[Severity] = None) -> Severity:
    if override:
        return override
    return DEFAULT_SEVERITY.get(kind, "advisory")


def evidence_expectation_ok(expectation: str) -> bool:
    return expectation in EVIDENCE_EXPECTATIONS


def reduce_expression(
    expression: RequirementExpression,
    observations: Dict[str, LeafObservation],
) -> DocumentState:
    """Reduce an AND/OR/atomic/unresolved expression to a document_state.

    Rules (Package 1 exit criteria):
    - atomic: look up observation by capability; missing → unknown
    - AND: all children must be satisfied for satisfied; any contradicted →
      contradicted; any not_demonstrated (with no better path) → not_demonstrated;
      mix of partial/satisfied → partially_supported; unresolved child that
      could change the outcome → unknown
    - OR: one complete (satisfied) alternative is enough; if any alternative is
      partial and none satisfied → partially_supported; all not_demonstrated →
      not_demonstrated; unresolved that could change outcome → unknown
    - unresolved node: always unknown
    """
    return _reduce(expression, observations)


def _reduce(
    expr: RequirementExpression,
    obs: Dict[str, LeafObservation],
) -> DocumentState:
    kind = expr.kind
    if kind == "unresolved":
        return "unknown"
    if kind == "atomic":
        return _reduce_atomic(expr, obs)
    if kind == "and":
        return _reduce_and(expr.children, obs)
    if kind == "or":
        return _reduce_or(expr.children, obs)
    return "unknown"


def _reduce_atomic(
    expr: RequirementExpression,
    obs: Dict[str, LeafObservation],
) -> DocumentState:
    if not expr.predicate:
        return "unknown"
    key = (expr.predicate.capability or "").strip().lower()
    if not key:
        return "unknown"
    leaf = obs.get(key)
    if leaf is None:
        # Also try exact capability string for callers that don't normalize.
        leaf = obs.get(expr.predicate.capability.strip())
    if leaf is None:
        return "unknown"
    if leaf.unresolved:
        return "unknown"
    return leaf.document_state


def _child_states(
    children: Sequence[RequirementExpression],
    obs: Dict[str, LeafObservation],
) -> List[DocumentState]:
    return [_reduce(c, obs) for c in children]


def _could_change_and(states: Sequence[DocumentState]) -> bool:
    """True if an unknown child could still flip the AND outcome."""
    if "unknown" not in states:
        return False
    known = [s for s in states if s != "unknown"]
    # Already contradicted or already all-not — unknown cannot save AND.
    if any(s == "contradicted" for s in known):
        return False
    if known and all(s == "not_demonstrated" for s in known) and "unknown" in states:
        # Unknown might still become satisfied → outcome could become partial
        # or better; treat as could-change → unknown.
        return True
    # If remaining known are satisfied/partial, unknown could block full credit.
    return True


def _could_change_or(states: Sequence[DocumentState]) -> bool:
    """True if an unknown child could still flip the OR outcome."""
    if "unknown" not in states:
        return False
    if any(s == "satisfied" for s in states):
        # Already have a complete alternative; unknown cannot worsen OR to
        # not_demonstrated under our rules (we keep best).
        return False
    # Unknown might become satisfied → could change partial/not → satisfied.
    return True


def _reduce_and(
    children: Sequence[RequirementExpression],
    obs: Dict[str, LeafObservation],
) -> DocumentState:
    if not children:
        return "unknown"
    states = _child_states(children, obs)
    if any(s == "contradicted" for s in states):
        return "contradicted"
    if _could_change_and(states):
        return "unknown"
    if all(s == "satisfied" for s in states):
        return "satisfied"
    if any(s == "not_demonstrated" for s in states):
        # Some children missing, none unknown (handled above), none contradicted.
        if any(s in {"satisfied", "partially_supported"} for s in states):
            return "partially_supported"
        return "not_demonstrated"
    if any(s == "partially_supported" for s in states):
        return "partially_supported"
    return "unknown"


def _reduce_or(
    children: Sequence[RequirementExpression],
    obs: Dict[str, LeafObservation],
) -> DocumentState:
    if not children:
        return "unknown"
    states = _child_states(children, obs)
    if any(s == "satisfied" for s in states):
        return "satisfied"
    if _could_change_or(states):
        return "unknown"
    if any(s == "partially_supported" for s in states):
        return "partially_supported"
    if any(s == "contradicted" for s in states) and all(
        s in {"contradicted", "not_demonstrated"} for s in states
    ):
        # Prefer not_demonstrated over contradicted for OR unless all contradicted.
        if all(s == "contradicted" for s in states):
            return "contradicted"
        return "not_demonstrated"
    if all(s == "not_demonstrated" for s in states):
        return "not_demonstrated"
    if all(s == "contradicted" for s in states):
        return "contradicted"
    return "unknown"


def observations_from_pairs(
    pairs: Iterable[tuple],
) -> Dict[str, LeafObservation]:
    """Build observation map from (capability, document_state[, unresolved])."""
    out: Dict[str, LeafObservation] = {}
    for item in pairs:
        cap = str(item[0]).strip()
        state: DocumentState = item[1]  # type: ignore[assignment]
        unresolved = bool(item[2]) if len(item) > 2 else False
        leaf = LeafObservation(
            capability=cap, document_state=state, unresolved=unresolved,
        )
        out[cap.lower()] = leaf
        out[cap] = leaf
    return out
