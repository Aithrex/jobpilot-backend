"""Assessment-v2 contracts (schema_version 4).

Canonical request/result types for resume file assessment. No public overall
score. Independent document / source / claim states. Requirement expressions
support atomic predicates, AND, OR, and unresolved ambiguity.

Authoritative product rules:
``docs/plans/2026-09-05-ats-engine-master-implementation-plan.md``.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Literal, Optional, Sequence

SCHEMA_VERSION = 4
ASSESSMENT_VERSION = "assessment-v2"

DocumentState = Literal[
    "satisfied",
    "partially_supported",
    "not_demonstrated",
    "contradicted",
    "unknown",
]
SourceState = Literal[
    "supported",
    "listed_only",
    "related",
    "not_evidenced",
    "contradicted",
    "unknown",
]
ClaimState = Literal[
    "supported_explicit",
    "supported_semantic",
    "unverified",
    "contradicted",
]
MatchMethod = Literal["exact", "recognized_alias", "semantic", "none"]
RequirementPriority = Literal["required", "preferred", "unspecified"]
RequirementKind = Literal[
    "capability",
    "responsibility",
    "credential",
    "duration",
    "domain",
    "application_context",
]
ExpressionKind = Literal["atomic", "and", "or", "unresolved"]
FindingKind = Literal[
    "wording",
    "absence",
    "relationship",
    "visual",
    "strength",
    "clarity",
    "integrity",
]
Severity = Literal["critical", "material", "advisory", "informational"]
ProcessingStatus = Literal[
    "pending", "running", "complete", "partial", "failed", "superseded",
]
ArtifactStatus = Literal["passed", "issues", "failed", "unchecked"]
SourceCheckStatus = Literal["complete", "partial", "unresolved"]
ReaderStatus = Literal["completed", "unavailable", "partial"]
ComparisonStatus = Literal["comparable", "not_comparable", "unavailable"]
DisplayBucket = Literal[
    "supported_and_satisfied",
    "partial_or_related",
    "not_demonstrated",
    "conflict",
    "unknown",
]


@dataclass
class AssessmentSnapshot:
    """Server-resolved identity for an assessment request."""

    owner_scope: str
    record_id: str
    version_id: str
    format: str  # pdf | docx
    source_snapshot_id: str = ""
    source_hash: str = ""
    confirmed_evidence_hash: str = ""
    jd_hash: str = ""
    requirement_version_hash: str = ""
    document_content_hash: str = ""
    preferences_hash: str = ""
    policy_version: str = ASSESSMENT_VERSION
    previous_assessment_id: str = ""
    idempotency_key: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class AtomicPredicate:
    """Capability/tool observation with optional qualifiers."""

    capability: str
    action: str = ""
    context: str = ""
    ownership_required: bool = False
    duration_required: bool = False
    currency_required: bool = False
    evidence_expectation: str = "document_or_skills"
    # listed_skill_ok | production_use | ownership | duration | credential

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class RequirementExpression:
    """Tree node: atomic | and | or | unresolved."""

    kind: ExpressionKind
    predicate: Optional[AtomicPredicate] = None
    children: List["RequirementExpression"] = field(default_factory=list)
    reason: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "predicate": self.predicate.as_dict() if self.predicate else None,
            "children": [c.as_dict() for c in self.children],
            "reason": self.reason,
        }


@dataclass
class CanonicalRequirement:
    """Top-level requirement group counted once in the UI."""

    id: str
    display_text: str
    priority: RequirementPriority
    kind: RequirementKind
    expression: RequirementExpression
    source_excerpt: str = ""
    provenance: str = ""
    extraction_state: Literal["complete", "partial", "ambiguous"] = "complete"

    def as_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "display_text": self.display_text,
            "priority": self.priority,
            "kind": self.kind,
            "expression": self.expression.as_dict(),
            "source_excerpt": self.source_excerpt,
            "provenance": self.provenance,
            "extraction_state": self.extraction_state,
        }


@dataclass
class DocumentRef:
    span: str
    section: str = ""
    entity: str = ""
    offset_start: Optional[int] = None
    offset_end: Optional[int] = None

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class SourceRef:
    fact_id: str
    entity: str = ""
    text: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class RequirementEvaluation:
    requirement_id: str
    document_state: DocumentState
    source_state: SourceState
    claim_state: ClaimState
    match_method: MatchMethod
    document_refs: List[DocumentRef] = field(default_factory=list)
    source_refs: List[SourceRef] = field(default_factory=list)
    unmet_qualifiers: List[str] = field(default_factory=list)
    reason: str = ""
    dependencies: Dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "requirement_id": self.requirement_id,
            "document_state": self.document_state,
            "source_state": self.source_state,
            "claim_state": self.claim_state,
            "match_method": self.match_method,
            "document_refs": [r.as_dict() for r in self.document_refs],
            "source_refs": [r.as_dict() for r in self.source_refs],
            "unmet_qualifiers": list(self.unmet_qualifiers),
            "reason": self.reason,
            "dependencies": dict(self.dependencies),
        }

    def display_bucket(self) -> DisplayBucket:
        return display_bucket(self.document_state, self.source_state, self.claim_state)


@dataclass
class FindingReference:
    """Typed reference — absence findings must not invent resume quotes."""

    kind: Literal["resume_span", "jd_requirement", "search_scope", "entity_pair", "page_region"]
    value: str
    secondary: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class AssessmentFinding:
    kind: FindingKind
    severity: Severity
    explanation: str
    requirement_id: str = ""
    references: List[FindingReference] = field(default_factory=list)
    completion_state: Literal["complete", "partial", "unavailable"] = "complete"
    suggested_action: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "severity": self.severity,
            "explanation": self.explanation,
            "requirement_id": self.requirement_id,
            "references": [r.as_dict() for r in self.references],
            "completion_state": self.completion_state,
            "suggested_action": self.suggested_action,
        }


@dataclass
class StageStatus:
    processing: ProcessingStatus = "pending"
    artifact: ArtifactStatus = "unchecked"
    source_check: SourceCheckStatus = "unresolved"
    reader: ReaderStatus = "unavailable"
    comparison: ComparisonStatus = "unavailable"

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def user_status_label(self) -> str:
        if self.artifact == "failed" or self.processing == "failed":
            return "File needs attention"
        if self.processing in {"partial", "running", "pending"}:
            return "Review incomplete"
        if self.processing == "complete":
            return "Review complete"
        return "Review incomplete"


@dataclass
class RequirementCounts:
    """Mutually exclusive display buckets, broken out by priority."""

    required: Dict[DisplayBucket, int] = field(default_factory=dict)
    preferred: Dict[DisplayBucket, int] = field(default_factory=dict)
    unspecified: Dict[DisplayBucket, int] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "required": dict(self.required),
            "preferred": dict(self.preferred),
            "unspecified": dict(self.unspecified),
        }


@dataclass
class AssessmentResult:
    """schema_version 4 response. No invented numeric overall."""

    assessment_id: str
    schema_version: int = SCHEMA_VERSION
    assessment_version: str = ASSESSMENT_VERSION
    snapshot: Optional[AssessmentSnapshot] = None
    stages: StageStatus = field(default_factory=StageStatus)
    evaluations: List[RequirementEvaluation] = field(default_factory=list)
    counts: RequirementCounts = field(default_factory=RequirementCounts)
    findings: List[AssessmentFinding] = field(default_factory=list)
    top_actions: List[str] = field(default_factory=list)
    comparison: Optional[Dict[str, Any]] = None
    application_context: Dict[str, Any] = field(default_factory=dict)
    overall: None = None  # always null for new results

    def as_dict(self) -> Dict[str, Any]:
        return {
            "assessment_id": self.assessment_id,
            "schema_version": self.schema_version,
            "assessment_version": self.assessment_version,
            "snapshot": self.snapshot.as_dict() if self.snapshot else None,
            "stages": self.stages.as_dict(),
            "evaluations": [e.as_dict() for e in self.evaluations],
            "counts": self.counts.as_dict(),
            "findings": [f.as_dict() for f in self.findings],
            "top_actions": list(self.top_actions),
            "comparison": self.comparison,
            "application_context": dict(self.application_context),
            "overall": None,
            "document_assessment": None,  # v3 sidecar not used for v4
        }


def display_bucket(
    document_state: DocumentState,
    source_state: SourceState,
    claim_state: ClaimState,
) -> DisplayBucket:
    """Derive mutually exclusive UI bucket from independent fields.

    Full credit requires document satisfied + source supported + claim not
    unverified/contradicted. Conflicts win. Unknowns stay unknown.
    """
    if (
        document_state == "contradicted"
        or source_state == "contradicted"
        or claim_state == "contradicted"
    ):
        return "conflict"
    if document_state == "unknown" or source_state == "unknown":
        return "unknown"
    if (
        document_state == "satisfied"
        and source_state == "supported"
        and claim_state in {"supported_explicit", "supported_semantic"}
    ):
        return "supported_and_satisfied"
    if document_state == "not_demonstrated" and source_state == "not_evidenced":
        return "not_demonstrated"
    if document_state == "not_demonstrated" and source_state not in {
        "listed_only", "related", "supported",
    }:
        return "not_demonstrated"
    if document_state in {"partially_supported", "satisfied"} or source_state in {
        "listed_only", "related", "supported",
    }:
        return "partial_or_related"
    if document_state == "not_demonstrated":
        return "not_demonstrated"
    return "unknown"


def count_evaluations(
    requirements: Sequence[CanonicalRequirement],
    evaluations: Sequence[RequirementEvaluation],
) -> RequirementCounts:
    """Count each top-level requirement group once into priority buckets."""
    by_id = {e.requirement_id: e for e in evaluations}
    empty = {
        "supported_and_satisfied": 0,
        "partial_or_related": 0,
        "not_demonstrated": 0,
        "conflict": 0,
        "unknown": 0,
    }
    counts = RequirementCounts(
        required=dict(empty), preferred=dict(empty), unspecified=dict(empty),
    )
    for req in requirements:
        ev = by_id.get(req.id)
        bucket: DisplayBucket = ev.display_bucket() if ev else "unknown"
        target = getattr(counts, req.priority if req.priority in {
            "required", "preferred", "unspecified",
        } else "unspecified")
        target[bucket] = target.get(bucket, 0) + 1
    return counts


def validate_assessment_result(payload: Dict[str, Any]) -> List[str]:
    """Lightweight structural validation. Returns error strings."""
    errors: List[str] = []
    if payload.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION}")
    if payload.get("assessment_version") != ASSESSMENT_VERSION:
        errors.append(f"assessment_version must be {ASSESSMENT_VERSION}")
    if "overall" in payload and payload.get("overall") is not None:
        errors.append("overall must be null for assessment-v2 results")
    if not payload.get("assessment_id"):
        errors.append("assessment_id required")
    stages = payload.get("stages") or {}
    for key in ("processing", "artifact", "source_check", "reader", "comparison"):
        if key not in stages:
            errors.append(f"stages.{key} required")
    return errors


def atomic(capability: str, **kwargs: Any) -> RequirementExpression:
    return RequirementExpression(
        kind="atomic", predicate=AtomicPredicate(capability=capability, **kwargs),
    )


def group_and(*children: RequirementExpression) -> RequirementExpression:
    return RequirementExpression(kind="and", children=list(children))


def group_or(*children: RequirementExpression) -> RequirementExpression:
    return RequirementExpression(kind="or", children=list(children))


def unresolved(reason: str) -> RequirementExpression:
    return RequirementExpression(kind="unresolved", reason=reason)
