"""Learning Hub — Verification Service (pluggable validator layer).

Today every challenge is verified by `NoOpValidator` (auto-pass): the user
marks a day complete from the UI. The whole point of this module is that a
FUTURE automated verification pipeline plugs in WITHOUT touching the Submission,
Challenge, or Progress services:

    from services.sprint_verification import register_validator, Validator, VerificationResult

    class DockerBuildsValidator(Validator):
        method = "docker"
        def validate(self, ctx):
            ok = _docker_build(ctx.evidence["repo_url"])
            return VerificationResult("docker", "passed" if ok else "failed", {...}, "docker_builds")

    register_validator("containerize", DockerBuildsValidator())

Future validators (all GitHub-OAuth based, never credentials): repo exists,
commit detected, required files/structure, build succeeds, tests pass, endpoints
respond, docker image builds, terraform validates, CI succeeds. Each keys off a
day's `challenge_type` in the registry.

The `VerificationResult` shape is persisted verbatim into
`sprint_submissions.verification`, so the data model is already future-proof:
`method` + `status` + `evidence` (repo_url, commit_sha, run_id, logs...).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class VerificationContext:
    user_email: str
    path_key: str
    day_key: str
    challenge_type: str
    submission_note: str = ""
    evidence: Dict = field(default_factory=dict)   # future: repo_url, commit_sha, run_id...


@dataclass
class VerificationResult:
    method: str            # "manual" | "repo_exists" | "commit" | "build" | "tests" | "docker" | "terraform" | "ci" ...
    status: str            # "passed" | "failed" | "pending" | "skipped"
    evidence: Dict         # empty today; future proof/logs references
    validator: str         # registry-visible validator name
    checked_at: datetime = field(default_factory=_now)

    def to_doc(self) -> Dict:
        return {
            "method": self.method,
            "status": self.status,
            "evidence": self.evidence or {},
            "validator": self.validator,
            "checked_at": self.checked_at,
        }

    @property
    def passed(self) -> bool:
        return self.status == "passed"


class Validator(ABC):
    method: str = "manual"

    @abstractmethod
    def validate(self, ctx: VerificationContext) -> VerificationResult:
        ...


class NoOpValidator(Validator):
    """Default validator — trusts the UI 'mark complete' action (auto-pass)."""
    method = "manual"

    def validate(self, ctx: VerificationContext) -> VerificationResult:
        return VerificationResult(
            method="manual",
            status="passed",
            evidence={},
            validator="noop",
        )


# Registry keyed by challenge_type, plus a swappable fallback. Generated tickets
# don't carry a stable challenge_type (it falls back to the day key), so the
# DEFAULT is what actually governs most days — see set_default_validator.
_NOOP = NoOpValidator()
VALIDATOR_REGISTRY: Dict[str, Validator] = {}
_DEFAULT: Validator = _NOOP


def register_validator(challenge_type: str, validator: Validator) -> None:
    VALIDATOR_REGISTRY[challenge_type] = validator


def set_default_validator(validator: Validator) -> None:
    """Swap the fallback used when a challenge_type has no explicit validator."""
    global _DEFAULT
    _DEFAULT = validator


def get_validator(challenge_type: str) -> Validator:
    return VALIDATOR_REGISTRY.get(challenge_type, _DEFAULT)


def verify(ctx: VerificationContext) -> VerificationResult:
    """Run the validator registered for this challenge type."""
    return get_validator(ctx.challenge_type).validate(ctx)
