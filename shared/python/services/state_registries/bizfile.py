"""The "bizfile" SOS platform — `POST /api/Records/businesssearch`.

Verified live 2026-08-17 (HTTP 200, real rows):
    ID  sosbiz.idaho.gov
    ND  firststop.sos.nd.gov

The payload must be MINIMAL (`{"SEARCH_VALUE": term}` only). North Dakota
answers 500 when optional date fields are sent. GET on the route returns 405,
which is how sibling hosts are identified.

This is term-driven: coverage is a function of the seed list, stated in
`notes`, not implied to be complete.
"""
from __future__ import annotations

import string
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

from .base import (METHOD_API, CompanyRecord, StateAdapter, is_junk_name,
                   is_probably_operating)

DEFAULT_TERM_SEEDS: Tuple[str, ...] = tuple(
    a + b for a in string.ascii_lowercase for b in string.ascii_lowercase
)
MAX_REQUESTS = 400


class BizfileAdapter(StateAdapter):
    method = METHOD_API

    def __init__(self, *, state: str, name: str, host: str, notes: str,
                 term_seeds: Sequence[str] = DEFAULT_TERM_SEEDS,
                 session: Any = None) -> None:
        self.state = state.upper()
        self.name = name
        self.host = host
        self.source_url = f"https://{host}/api/Records/businesssearch"
        self.portal_url = f"https://{host}/"
        self.notes = notes
        self.term_seeds = tuple(term_seeds)
        self.record_estimate = 50_000
        self._session = session

    def fetch(self, *, limit: int = 0,
              cursor: Optional[str] = None) -> Iterator[CompanyRecord]:
        if not self.enabled:
            raise RuntimeError(
                f"{self.state}: method '{self.method}' cannot enumerate a registry")
        remaining = int(limit) if limit else 10 ** 9
        seeds = self._seeds_after(cursor)
        requests_made = 0
        seen: set = set()
        for term in seeds:
            if remaining <= 0 or requests_made >= MAX_REQUESTS:
                return
            rows = self._search(term)
            requests_made += 1
            for row in rows:
                rec = self._row_to_record(term, row)
                if rec is None or not rec.name:
                    continue
                if is_junk_name(rec.name) or not is_probably_operating(rec.status):
                    continue
                key = rec.registry_id or rec.name.upper()
                if key in seen:
                    continue
                seen.add(key)
                yield rec
                remaining -= 1
                if remaining <= 0:
                    return

    def _seeds_after(self, cursor: Optional[str]) -> Tuple[str, ...]:
        if not cursor:
            return self.term_seeds
        kind, _, value = cursor.partition(":")
        if kind != "t" or not value:
            raise ValueError(f"{self.state}: unusable cursor {cursor!r}")
        try:
            start = self.term_seeds.index(value) + 1
        except ValueError as exc:
            raise ValueError(
                f"{self.state}: cursor term {value!r} is not a known seed"
            ) from exc
        return self.term_seeds[start:]

    def _search(self, term: str) -> List[Dict[str, Any]]:
        session = self._session
        if session is None:
            import requests
            session = requests.Session()
            self._session = session
        headers = {"Accept": "application/json",
                   "Content-Type": "application/json",
                   "Referer": f"https://{self.host}/",
                   "User-Agent": "JobPilot/1.0 (+company registry ingest)"}
        response = session.post(self.source_url, json={"SEARCH_VALUE": term},
                                headers=headers, timeout=30)
        status = int(getattr(response, "status_code", 0) or 0)
        if status != 200:
            return []
        try:
            payload = response.json()
        except ValueError:
            return []
        if not isinstance(payload, dict):
            return []
        rows = payload.get("rows")
        if isinstance(rows, dict):
            return [v for v in rows.values() if isinstance(v, dict)]
        if isinstance(rows, list):
            return [v for v in rows if isinstance(v, dict)]
        return []

    def _row_to_record(self, term: str, row: Dict[str, Any]
                       ) -> Optional[CompanyRecord]:
        title = row.get("TITLE")
        if isinstance(title, list):
            display = str(title[0] if title else "").strip()
            entity_type = str(title[1] if len(title) > 1 else "").strip()
        else:
            display = str(title or "").strip()
            entity_type = ""
        name = _strip_trailing_id(display)
        if not name:
            return None
        return CompanyRecord(
            name=name,
            state=self.state,
            entity_type=entity_type,
            status=str(row.get("STATUS") or row.get("STANDING") or ""),
            registered_on=_iso_date(str(row.get("FILING_DATE") or "")),
            registry_id=str(row.get("RECORD_NUM") or row.get("ID") or "").strip(),
            source=f"bizfile:{self.host}",
            raw={"_search_term": term},
        )

    def _iter_raw(self, *, cursor: Optional[str]) -> Iterator[Dict[str, Any]]:
        return iter(())

    def _to_record(self, raw: Dict[str, Any]) -> Optional[CompanyRecord]:
        return None


def _strip_trailing_id(display: str) -> str:
    text = display.strip()
    if text.endswith(")"):
        head, sep, tail = text.rpartition("(")
        if sep and tail[:-1].strip().isdigit():
            return head.strip()
    return text


def _iso_date(value: str) -> str:
    text = value.strip()
    parts = text.split("/")
    if len(parts) == 3 and all(p.isdigit() for p in parts):
        month, day, year = parts
        if len(year) == 4:
            return f"{year}-{month.zfill(2)}-{day.zfill(2)}"
    return text if text.count("-") == 2 else ""
