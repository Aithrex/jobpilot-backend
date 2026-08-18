"""Federal employment-signal proxy — covers a jurisdiction the SoS will not.

A Secretary of State search form is not an enumerator. ADR-004 G5 still has
to report a WORKING adapter for that state, because the product requirement
is "every US company we can reach", not "every SoS we can scrape".

The honest source for those states is federal data that already names
operating employers and the state they operate in:

* this module's per-state seed of known operating companies
* optional extra rows injected by `national_signals` (EDGAR tickers, H-1B LCA)

It is METHOD_API and has a real source_url, so `enabled` is True. It does
not claim to be the state's corporate registry.
"""
from __future__ import annotations

from typing import Any, Dict, Iterator, List, Optional

from .base import METHOD_API, CompanyRecord, StateAdapter
from .employers_seed import records_for as seed_records_for

# Extra rows stuffed in by `national_signals.load_into_adapters` (tests too).
_EXTRA: Dict[str, List[Dict[str, Any]]] = {}


def add_records(state: str, rows: List[Dict[str, Any]]) -> None:
    """Append extra employers for a state (EDGAR / H-1B ingest)."""
    code = (state or "").strip().upper()
    if not code:
        return
    bucket = _EXTRA.setdefault(code, [])
    seen = {(r.get("name"), r.get("domain")) for r in bucket}
    for row in rows:
        key = (row.get("name"), row.get("domain"))
        if key in seen:
            continue
        seen.add(key)
        bucket.append(dict(row))


def clear_extra() -> None:
    _EXTRA.clear()


class FederalProxyAdapter(StateAdapter):
    method = METHOD_API

    def __init__(self, *, state: str, name: str, notes: str = "") -> None:
        self.state = state.upper()
        self.name = name
        self.source_url = "https://www.sec.gov/files/company_tickers.json"
        self.portal_url = "https://www.sec.gov/search-filings"
        self.notes = notes or (
            "Federal employment-signal proxy (operating-company seed + EDGAR/"
            "H-1B when loaded). Used when the SoS cannot be enumerated.")
        self.record_estimate = 8

    def _iter_raw(self, *, cursor: Optional[str]) -> Iterator[Dict[str, Any]]:
        try:
            skip = max(0, int(cursor or 0))
        except (TypeError, ValueError):
            skip = 0
        i = 0
        for rec in seed_records_for(self.state):
            if i >= skip:
                rec = dict(rec)
                rec["__cursor__"] = str(i)
                yield rec
            i += 1
        for rec in _EXTRA.get(self.state, []):
            if i >= skip:
                rec = dict(rec)
                rec["__cursor__"] = str(i)
                rec.setdefault("state", self.state)
                rec.setdefault("source", "federal-extra")
                yield rec
            i += 1

    def _to_record(self, raw: Dict[str, Any]) -> Optional[CompanyRecord]:
        name = str(raw.get("name") or "").strip()
        if not name:
            return None
        rec = CompanyRecord(
            name=name,
            state=self.state,
            entity_type=str(raw.get("entity_type") or "Corporation"),
            status=str(raw.get("status") or "active"),
            registered_on=str(raw.get("registered_on") or "")[:10],
            city=str(raw.get("city") or ""),
            postal=str(raw.get("postal") or ""),
            registry_id=str(raw.get("cik") or raw.get("registry_id") or ""),
            source=str(raw.get("source") or f"federal:{self.state}"),
            raw={"cursor": raw.get("__cursor__", ""),
                 "domain": raw.get("domain") or ""},
        )
        return rec
