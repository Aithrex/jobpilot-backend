"""US state business-registry adapters (ADR-004 G4/G5).

Importing this package registers every jurisdiction, so callers can go straight
to `get("NY")` or `enabled_adapters()` without knowing which module a state
lives in.
"""
from __future__ import annotations

from .base import (HARVESTABLE, METHOD_API, METHOD_BULK, METHOD_CKAN,
                   METHOD_NONE, METHOD_PAID, METHOD_SEARCH, METHOD_SOCRATA,
                   CompanyRecord, StateAdapter, all_adapters, coverage,
                   enabled_adapters, get, is_junk_name, is_probably_operating,
                   register)
from .bulk import BulkFileAdapter
from .socrata import SocrataAdapter
from .federal import FederalProxyAdapter
from . import states as _states  # noqa: F401  (import registers the table)

__all__ = [
    "CompanyRecord", "StateAdapter", "SocrataAdapter", "BulkFileAdapter",
    "FederalProxyAdapter",
    "register", "get", "all_adapters", "enabled_adapters", "coverage",
    "is_junk_name", "is_probably_operating", "HARVESTABLE",
    "METHOD_SOCRATA", "METHOD_CKAN", "METHOD_BULK", "METHOD_API",
    "METHOD_SEARCH", "METHOD_PAID", "METHOD_NONE",
]
