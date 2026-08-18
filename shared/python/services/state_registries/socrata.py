"""Socrata (and CKAN) open-data adapters.

Most states that publish their corporate registry at all publish it on Socrata,
so one parameterised adapter covers many jurisdictions and each state becomes a
table entry rather than a module. The per-state variation is entirely in two
places — the dataset id and the column names — so those are the only things a
state has to supply.

Paging
------
Socrata pages with `$limit`/`$offset` and requires an explicit `$order` for
stable paging: without it the server may return rows in a different order per
request, which silently duplicates some rows and drops others across pages. We
always order by `:id`, the internal row identifier, which every dataset has and
which no state can rename.

Deep offsets degrade badly on large datasets, so the cursor is the offset and
runs are expected to be resumed rather than run to completion in one pass.

App tokens
----------
Anonymous requests are throttled per IP and shared across everyone on that IP;
a free app token raises the limit substantially. `SOCRATA_APP_TOKEN` is read
from the environment when present and simply omitted when not, so this works
unconfigured and works better configured.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, Iterator, Optional
from urllib.parse import urlencode

from .base import METHOD_SOCRATA, CompanyRecord, StateAdapter

logger = logging.getLogger(__name__)

PAGE_SIZE = 1000
_TIMEOUT = 45
_MAX_RETRIES = 3


def _session():
    import requests
    s = requests.Session()
    s.headers.update({
        "Accept": "application/json",
        "User-Agent": "JobPilot/1.0 (+employer-coverage; contact via site)",
    })
    token = (os.getenv("SOCRATA_APP_TOKEN") or "").strip()
    if token:
        s.headers["X-App-Token"] = token
    return s


class SocrataAdapter(StateAdapter):
    """Reads a Socrata dataset and maps its columns onto `CompanyRecord`."""

    method = METHOD_SOCRATA

    def __init__(self, *, state: str, name: str, url: str,
                 fields: Dict[str, str], portal_url: str = "",
                 record_estimate: int = 0, where: str = "",
                 notes: str = "") -> None:
        self.state = state.upper()
        self.name = name
        self.source_url = url
        self.portal_url = portal_url
        self.record_estimate = record_estimate
        self.fields = fields
        #: Optional SoQL filter, e.g. restricting to active entities server-side.
        #: Cheaper than filtering client-side over millions of rows.
        self.where = where
        self.notes = notes

    # -- fetching ------------------------------------------------------------

    def _iter_raw(self, *, cursor: Optional[str]) -> Iterator[Dict[str, Any]]:
        session = _session()
        try:
            offset = max(0, int(cursor or 0))
        except (TypeError, ValueError):
            offset = 0

        while True:
            params = {"$limit": PAGE_SIZE, "$offset": offset, "$order": ":id"}
            if self.where:
                params["$where"] = self.where
            url = f"{self.source_url}?{urlencode(params)}"
            rows = self._get(session, url)
            if not rows:
                return
            for row in rows:
                # The offset is carried on each row so an interrupted run can be
                # resumed from the last row actually persisted, not the last page
                # requested — those differ by up to PAGE_SIZE rows.
                row["__cursor__"] = str(offset)
                yield row
            if len(rows) < PAGE_SIZE:
                return
            offset += len(rows)

    def _get(self, session, url: str):
        for attempt in range(_MAX_RETRIES):
            try:
                resp = session.get(url, timeout=_TIMEOUT)
            except Exception as exc:
                logger.warning("%s: %s: %s", self.state, type(exc).__name__, exc)
                time.sleep(2 * (attempt + 1))
                continue
            if resp.status_code == 429:
                # Throttled. Anonymous quota is shared per IP, so back off hard.
                wait = 10 * (attempt + 1)
                logger.warning("%s: throttled, sleeping %ds", self.state, wait)
                time.sleep(wait)
                continue
            if resp.status_code != 200:
                logger.warning("%s: HTTP %s from %s", self.state,
                               resp.status_code, url[:100])
                return []
            try:
                payload = resp.json()
            except ValueError:
                logger.warning("%s: non-JSON response", self.state)
                return []
            return payload if isinstance(payload, list) else []
        return []

    # -- mapping -------------------------------------------------------------

    def _pick(self, raw: Dict[str, Any], key: str) -> str:
        col = self.fields.get(key)
        if not col:
            return ""
        value = raw.get(col)
        if value is None:
            return ""
        return str(value).strip()

    def _to_record(self, raw: Dict[str, Any]) -> Optional[CompanyRecord]:
        name = self._pick(raw, "name")
        if not name:
            return None
        registered = self._pick(raw, "registered")
        if registered and "T" in registered:
            registered = registered.split("T", 1)[0]
        return CompanyRecord(
            name=name,
            state=self.state,
            entity_type=self._pick(raw, "type"),
            status=self._pick(raw, "status"),
            registered_on=registered,
            city=self._pick(raw, "city"),
            postal=self._pick(raw, "postal"),
            registry_id=self._pick(raw, "registry_id"),
            source=f"socrata:{self.state}",
            raw={"cursor": raw.get("__cursor__", "")},
        )

    def count(self) -> int:
        """Server-side row count. Used to size a run before starting it."""
        session = _session()
        params = {"$select": "count(*) as n"}
        if self.where:
            params["$where"] = self.where
        rows = self._get(session, f"{self.source_url}?{urlencode(params)}")
        if not rows:
            return 0
        try:
            return int(str(rows[0].get("n") or rows[0].get("count") or 0))
        except (ValueError, TypeError):
            return 0
