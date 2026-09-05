"""Internal contracts; persisted dictionaries are JSON serializable."""
from dataclasses import dataclass, field, asdict


@dataclass
class CandidateFact:
    id: str
    address: str
    entity: str
    text: str
    kind: str = "accomplishment"


@dataclass
class Requirement:
    id: str
    text: str
    priority: str
    kind: str
    alternatives: list[str] = field(default_factory=list)
    source_excerpt: str = ""
    source_start: int | None = None
    state: str = "not_evidenced"
    support_basis: str = ""
    fact_ids: list[str] = field(default_factory=list)


@dataclass
class TailoringPlan:
    source_hash: str
    jd_hash: str
    facts: list[CandidateFact]
    requirements: list[Requirement]
    selected: list[str]
    completeness: str = "structured_only"

    def as_dict(self):
        return asdict(self)
