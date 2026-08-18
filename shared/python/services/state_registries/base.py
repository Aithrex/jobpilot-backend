"""State business-registry adapter contract.

What this layer is for
----------------------
Every US state runs a Secretary-of-State registry of incorporated entities. That
is the closest thing that exists to a complete list of American employers, and it
is the only input to this pipeline that is not already biased toward companies we
happen to know about. ADR-004 G4 is sourced entirely from here.

What this layer is NOT for
--------------------------
It is not a source of *employers*. A state registry is a list of legal entities,
and the overwhelming majority of them — single-member LLCs, holding companies,
dissolved shells, rental-property entities, one filing and no activity since —
will never post a job. Delaware alone has more registered entities than it has
residents by an order of magnitude.

So adapters deliberately do NO scoring and NO filtering beyond entity status.
They are dumb, resumable pipes. Deciding which of 40M entities is worth a
discovery fetch is `company_registry_service.score_priority`'s job, informed by
the national employment signals (EDGAR, SAM.gov, H-1B). Mixing the two would
bury the ranking logic in 51 different adapters.

Why access methods differ so much
---------------------------------
There is no federal standard, so each state chose independently:

    socrata / ckan  open-data portal with a real query API   — best case
    bulk            a CSV or ZIP at a stable URL             — good, big
    api             a bespoke JSON endpoint                  — fine, rare
    search          an interactive HTML form only            — NOT harvestable
    paid            sold commercially, or written request    — not harvestable
    none            nothing published                        — not harvestable

`search` is the most common and it is a genuine wall, not a missing feature: the
form requires a name to search FOR, so there is no query that enumerates the
registry. Those states are recorded honestly as unavailable rather than papered
over with a scraper that would return nothing, and the companies in them get
reached through the national signals instead. ADR-004 G5 counts jurisdictions
with a WORKING adapter, so an unharvestable state must never report as covered.

Resumability
------------
These datasets run to millions of rows and the fetch will be interrupted. Every
adapter therefore takes a `cursor` and returns rows in a stable order, so a run
resumes where the last one stopped instead of restarting.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional

logger = logging.getLogger(__name__)

# --- access methods ---------------------------------------------------------
METHOD_SOCRATA = "socrata"
METHOD_CKAN = "ckan"
METHOD_BULK = "bulk"
METHOD_API = "api"
METHOD_SEARCH = "search"      # interactive form only — cannot enumerate
METHOD_PAID = "paid"          # commercial licence required
METHOD_NONE = "none"          # nothing published

#: Methods that can actually enumerate a registry. `enabled` is derived from
#: this rather than set by hand so a state cannot claim coverage it lacks.
HARVESTABLE = frozenset({METHOD_SOCRATA, METHOD_CKAN, METHOD_BULK, METHOD_API})


# ---------------------------------------------------------------------------
# Record
# ---------------------------------------------------------------------------

@dataclass
class CompanyRecord:
    """One registered entity, normalised across 51 different schemas."""
    name: str
    state: str
    entity_type: str = ""
    status: str = ""
    registered_on: str = ""      # ISO date when the state publishes one
    city: str = ""
    postal: str = ""
    registry_id: str = ""        # the state's own filing number
    source: str = ""             # adapter identity, for provenance
    raw: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "state": self.state,
                "entity_type": self.entity_type, "status": self.status,
                "registered_on": self.registered_on, "city": self.city,
                "postal": self.postal, "registry_id": self.registry_id,
                "source": self.source}

    def as_upsert(self) -> Dict[str, Any]:
        """Shape `company_registry_service.upsert_company` actually reads.

        The dataclass field is `registered_on` (what a state publishes). The
        registry collection field is `registered_at`. Passing `__dict__`
        silently drops the date; this is the one mapping that must not.
        """
        out: Dict[str, Any] = {
            "name": self.name, "state": self.state,
            "entity_type": self.entity_type, "status": self.status,
            "registered_at": (self.registered_on or "")[:10],
            "source": self.source,
        }
        if self.city or self.postal:
            out["address"] = {"city": self.city, "postal": self.postal,
                              "state": self.state}
        if self.registry_id:
            out["source_url"] = self.source
        domain = (self.raw or {}).get("domain")
        if domain:
            out["domain"] = str(domain).strip().lower()
            out["signals"] = {"known_domain": True}
        extra_signals = (self.raw or {}).get("signals")
        if isinstance(extra_signals, dict) and extra_signals:
            merged = dict(out.get("signals") or {})
            merged.update(extra_signals)
            out["signals"] = merged
        return out


#: Entity names that are structurally incapable of being an employer we can find
#: a careers page for. Filtering these in the adapter (rather than downstream) is
#: worth the exception to "adapters do not filter": they are pure noise, they are
#: a large fraction of some states' rows, and carrying them costs a Mongo write
#: plus a discovery slot each.
_JUNK_NAME_RE = re.compile(
    r"^(?:"
    r"\d+\s+[\w\s]+(?:LLC|L\.L\.C\.)$"      # "123 Main Street LLC" — property
    r"|(?:the\s+)?estate\s+of\b"
    r"|.{0,3}$"                              # too short to identify anything
    r")", re.IGNORECASE)

#: Words that mean "this entity is not operating", matched ANYWHERE in the
#: status rather than as a prefix. Colorado's live distribution is why: it ships
#: "Voluntarily Dissolved", "Administratively Dissolved" and "Judicially
#: Dissolved" as three distinct values, and a prefix test catches the first two
#: while silently passing the third. Matching the operative word catches all
#: three plus whatever adverb the 51st state invents.
#:
#: This is load-bearing, not tidying: of Colorado's 3,095,631 registered
#: entities only 1,036,219 are in good standing, so roughly two thirds of the
#: rows are defunct. Carrying them would triple the registry and spend the
#: discovery budget on companies that no longer exist.
_DEAD_STATUS_RE = re.compile(
    r"\b(?:"
    r"inactive|dissolv\w*|revoked|forfeit\w*|terminat\w*|withdrawn|expired"
    r"|cancell?ed|merged|suspended|delinquent|surrendered|closed|defunct"
    r"|void|abandoned|struck|rejected|denied"
    r")\b", re.IGNORECASE)


def is_probably_operating(status: str) -> bool:
    """True when a status does not positively indicate a defunct entity.

    Unknown or blank statuses return True on purpose. Many states publish no
    status at all, and dropping every row from those states would silently lose
    whole jurisdictions — a false positive costs one wasted discovery probe, a
    false negative loses an employer permanently.
    """
    s = (status or "").strip()
    if not s:
        return True
    return not _DEAD_STATUS_RE.search(s)


def is_junk_name(name: str) -> bool:
    return bool(_JUNK_NAME_RE.match((name or "").strip()))


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class StateAdapter:
    """Base class. Subclasses implement `_iter_raw`."""

    #: 2-letter postal code.
    state: str = ""
    name: str = ""
    method: str = METHOD_NONE
    source_url: str = ""
    portal_url: str = ""
    #: Rough published size, for planning only.
    record_estimate: int = 0
    notes: str = ""

    @property
    def enabled(self) -> bool:
        return self.method in HARVESTABLE and bool(self.source_url)

    def fetch(self, *, limit: int = 0,
              cursor: Optional[str] = None) -> Iterator[CompanyRecord]:
        """Yield records, newest-stable-order first, honouring `limit`.

        Filtering of defunct and junk rows happens here, once, so every adapter
        inherits it rather than reimplementing it 51 times.
        """
        if not self.enabled:
            raise RuntimeError(
                f"{self.state}: method '{self.method}' cannot enumerate a registry")
        emitted = 0
        for raw in self._iter_raw(cursor=cursor):
            rec = self._to_record(raw)
            if rec is None or not rec.name:
                continue
            if is_junk_name(rec.name) or not is_probably_operating(rec.status):
                continue
            yield rec
            emitted += 1
            if limit and emitted >= limit:
                return

    # -- subclass surface ----------------------------------------------------

    def _iter_raw(self, *, cursor: Optional[str]) -> Iterator[Dict[str, Any]]:
        raise NotImplementedError

    def _to_record(self, raw: Dict[str, Any]) -> Optional[CompanyRecord]:
        raise NotImplementedError

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{type(self).__name__} {self.state} {self.method}>"


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_ADAPTERS: Dict[str, StateAdapter] = {}


def register(adapter: StateAdapter) -> StateAdapter:
    code = (adapter.state or "").strip().upper()
    if not code:
        raise ValueError("adapter has no state code")
    if code in _ADAPTERS:
        raise ValueError(f"duplicate adapter for {code}")
    _ADAPTERS[code] = adapter
    return adapter


def get(state: str) -> Optional[StateAdapter]:
    return _ADAPTERS.get((state or "").strip().upper())


def all_adapters() -> List[StateAdapter]:
    return [_ADAPTERS[k] for k in sorted(_ADAPTERS)]


def enabled_adapters() -> List[StateAdapter]:
    return [a for a in all_adapters() if a.enabled]


def coverage() -> Dict[str, Any]:
    """Summary for `coverage_report.py` G5."""
    by_method: Dict[str, int] = {}
    for a in all_adapters():
        by_method[a.method] = by_method.get(a.method, 0) + 1
    enabled = enabled_adapters()
    return {"total": len(_ADAPTERS),
            "enabled": len(enabled),
            "enabled_states": [a.state for a in enabled],
            "estimated_records": sum(a.record_estimate for a in enabled),
            "by_method": by_method}
