"""ATS provider plugins.

One module per vendor. Each declares how it is recognised, how it may be
crawled, and how its postings map onto `RawJob`; `registry.py` derives the
crawler's host allowlists, termless set and pacing scopes from those
declarations so a vendor is added in exactly one place.

See docs/decisions/ADR-004-universal-employer-coverage.md.
"""
from services.ats_providers.base import (  # noqa: F401
    DATE_EXACT, DATE_RELATIVE, DATE_UNKNOWN,
    STATUS_BLOCKED, STATUS_CANDIDATE, STATUS_DEAD, STATUS_EMPTY,
    STATUS_VALIDATED,
    AtsProvider, ChallengeError, CrawlPolicy, Endpoint, FetchContext,
    Fingerprint, RawJob, ValidationResult, fp,
)

__all__ = [
    "DATE_EXACT", "DATE_RELATIVE", "DATE_UNKNOWN",
    "STATUS_BLOCKED", "STATUS_CANDIDATE", "STATUS_DEAD", "STATUS_EMPTY",
    "STATUS_VALIDATED",
    "AtsProvider", "ChallengeError", "CrawlPolicy", "Endpoint", "FetchContext",
    "Fingerprint", "RawJob", "ValidationResult", "fp",
]
