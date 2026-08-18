"""Florida Sunbiz — published corporate data files.

    GET https://sftp.floridados.gov/Public/doc/cor/{YYYYMMDD}c.txt
        Authorization: Basic Public:PubAccess1845!

Credentials are published by the Florida Department of State on
dos.fl.gov/sunbiz/other-services/data-downloads. This adapter fetches a
documented data file by exact path; it does not crawl the portal.
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Dict, Iterator, List, Optional, Tuple

from .base import (METHOD_BULK, CompanyRecord, StateAdapter, is_junk_name,
                   is_probably_operating)

SUNBIZ_HOST = "sftp.floridados.gov"
SUNBIZ_DAILY_DIR = "/Public/doc/cor"
SUNBIZ_PUBLIC_AUTH = ("Public", "PubAccess1845!")
MAX_LOOKBACK_DAYS = 45

_LAYOUT: Dict[str, Tuple[int, int]] = {
    "number": (1, 12),
    "name": (13, 192),
    "status": (205, 1),
    "filing_type": (206, 15),
    "princ_addr1": (221, 42),
    "princ_city": (305, 28),
    "princ_state": (333, 2),
    "princ_zip": (335, 10),
    "file_date": (473, 8),
    "agent_name": (545, 42),
}


class SunbizDailyAdapter(StateAdapter):
    method = METHOD_BULK
    state = "FL"
    name = "Florida"

    def __init__(self, *, notes: str = "", session: Any = None,
                 today: Optional[dt.date] = None) -> None:
        self.notes = notes or (
            "Florida Sunbiz daily corporate files. Quarterly ZIP at "
            "/Public/doc/quarterly/cor/cordata.zip for a cold backfill.")
        self.source_url = f"https://{SUNBIZ_HOST}{SUNBIZ_DAILY_DIR}/"
        self.portal_url = "https://dos.fl.gov/sunbiz/other-services/data-downloads/"
        self.record_estimate = 2_000_000
        self._session = session
        self._today = today

    def file_url(self, day: dt.date) -> str:
        return (f"https://{SUNBIZ_HOST}{SUNBIZ_DAILY_DIR}/"
                f"{day.strftime('%Y%m%d')}c.txt")

    def fetch(self, *, limit: int = 0,
              cursor: Optional[str] = None) -> Iterator[CompanyRecord]:
        if not self.enabled:
            raise RuntimeError(
                f"{self.state}: method '{self.method}' cannot enumerate a registry")
        remaining = int(limit) if limit else 10 ** 9
        for day in self._days(cursor=cursor):
            body = self._get_file(day)
            if body is None:
                continue
            for line in body:
                rec = self._line_to_record(line, day)
                if rec is None or not rec.name:
                    continue
                if is_junk_name(rec.name) or not is_probably_operating(rec.status):
                    continue
                yield rec
                remaining -= 1
                if remaining <= 0:
                    return

    def _days(self, *, cursor: Optional[str]) -> List[dt.date]:
        today = self._today or dt.date.today()
        start = today
        if cursor:
            kind, _, value = cursor.partition(":")
            if kind != "d" or len(value) != 8 or not value.isdigit():
                raise ValueError(f"FL: unusable cursor {cursor!r}")
            start = (dt.datetime.strptime(value, "%Y%m%d").date()
                     - dt.timedelta(days=1))
        floor = start - dt.timedelta(days=MAX_LOOKBACK_DAYS)
        days: List[dt.date] = []
        day = start
        while day >= floor:
            if day.weekday() < 5:
                days.append(day)
            day -= dt.timedelta(days=1)
        return days

    def _get_file(self, day: dt.date) -> Optional[List[str]]:
        session = self._session
        if session is None:
            import requests
            session = requests.Session()
            self._session = session
        url = self.file_url(day)
        response = session.get(url, auth=SUNBIZ_PUBLIC_AUTH,
                               headers={"User-Agent": "JobPilot/1.0"},
                               timeout=60)
        status = int(getattr(response, "status_code", 0) or 0)
        if status == 404:
            return None
        if status != 200:
            return None
        text = getattr(response, "text", "") or ""
        return [line.replace("\x00", "").rstrip("\r")
                for line in text.split("\n") if line.strip("\x00\r ")]

    def _line_to_record(self, line: str, day: dt.date) -> Optional[CompanyRecord]:
        if not line or len(line) < _LAYOUT["status"][0]:
            return None
        name = _field(line, "name")
        if not name:
            return None
        code = _field(line, "status")
        status = {"A": "active", "I": "inactive"}.get(code.strip().upper(), "")
        return CompanyRecord(
            name=name,
            state="FL",
            entity_type=_field(line, "filing_type"),
            status=status,
            registered_on=_iso_date(_field(line, "file_date")),
            city=_field(line, "princ_city"),
            postal=_field(line, "princ_zip"),
            registry_id=_field(line, "number"),
            source=f"sunbiz:{SUNBIZ_HOST}",
            raw={"file_day": day.strftime("%Y%m%d")},
        )

    def _iter_raw(self, *, cursor: Optional[str]) -> Iterator[Dict[str, Any]]:
        return iter(())

    def _to_record(self, raw: Dict[str, Any]) -> Optional[CompanyRecord]:
        return None


def _field(line: str, key: str) -> str:
    start, length = _LAYOUT[key]
    return line[start - 1:start - 1 + length].strip()


def _iso_date(value: str) -> str:
    text = value.strip()
    if len(text) != 8 or not text.isdigit():
        return ""
    month, day, year = text[:2], text[2:4], text[4:]
    try:
        return dt.date(int(year), int(month), int(day)).isoformat()
    except ValueError:
        return ""
