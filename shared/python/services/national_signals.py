"""National employment-signal ingest — EDGAR tickers + optional H-1B LCA.

State registries are the breadth play; this is the *priority* play. An H-1B
LCA or an SEC filer is documentary proof the entity hires, which is what
`company_registry_service.score_priority` ranks on.

EDGAR `company_tickers.json` is a single public file (CIK + ticker + title).
It has no state; we still upsert the company (state="") so discovery can
resolve its domain, and we attach the `edgar` signal.

H-1B LCA files are too large to fetch inside a Lambda. When a path is passed
(`H1B_LCA_PATH` or `--lca`), rows are streamed and the employer+state pair
is upserted with `signals.h1b=True`, then stuffed into the federal proxy
adapter for that state so a subsequent `ingest-state` sees them.

Network is optional. Offline tests inject rows.
"""
from __future__ import annotations

import csv
import logging
import os
from typing import Any, Dict, Iterable, Iterator, List, Optional

logger = logging.getLogger(__name__)

EDGAR_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
EDGAR_UA = "JobPilot/1.0 (employer-coverage; contact via site)"

_LCA_NAME_COLS = ("EMPLOYER_NAME", "LCA_CASE_EMPLOYER_NAME",
                  "EMPLOYER_BUSINESS_DBA")
_LCA_STATE_COLS = ("EMPLOYER_STATE", "LCA_CASE_EMPLOYER_STATE",
                   "WORKSITE_STATE", "WORKSITE_STATE_1")
_LCA_CITY_COLS = ("EMPLOYER_CITY", "LCA_CASE_EMPLOYER_CITY",
                  "WORKSITE_CITY")


def _session():
    import requests
    s = requests.Session()
    s.headers.update({"User-Agent": EDGAR_UA, "Accept": "application/json"})
    return s


def fetch_edgar_tickers(*, session=None, url: str = EDGAR_TICKERS_URL
                        ) -> List[Dict[str, Any]]:
    """[{name, ticker, cik}] from the SEC company_tickers file."""
    sess = session or _session()
    resp = sess.get(url, timeout=60)
    resp.raise_for_status()
    payload = resp.json()
    if not isinstance(payload, dict):
        return []
    out: List[Dict[str, Any]] = []
    for row in payload.values():
        if not isinstance(row, dict):
            continue
        name = str(row.get("title") or "").strip()
        ticker = str(row.get("ticker") or "").strip()
        cik = str(row.get("cik_str") or row.get("cik") or "").strip()
        if not name:
            continue
        out.append({"name": name, "ticker": ticker, "cik": cik.zfill(10),
                    "source": "edgar", "signals": {"edgar": True}})
    return out


def iter_lca_rows(path: str, *, limit: int = 0) -> Iterator[Dict[str, Any]]:
    """Stream employer+state from a DOL LCA CSV/TSV (not XLSX)."""
    emitted = 0
    with open(path, "r", encoding="utf-8-sig", errors="replace", newline="") as fh:
        sample = fh.read(4096)
        fh.seek(0)
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t|")
        reader = csv.DictReader(fh, dialect=dialect)
        for raw in reader:
            rec = _lca_record(raw)
            if rec is None:
                continue
            yield rec
            emitted += 1
            if limit and emitted >= limit:
                return


def _lca_record(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    name = _first(raw, _LCA_NAME_COLS)
    state = _first(raw, _LCA_STATE_COLS).upper()[:2]
    if not name or len(state) != 2 or not state.isalpha():
        return None
    return {
        "name": name,
        "state": state,
        "city": _first(raw, _LCA_CITY_COLS),
        "source": "h1b-lca",
        "signals": {"h1b": True},
    }


def _first(raw: Dict[str, Any], keys: Iterable[str]) -> str:
    lower = {str(k).strip().lower(): v for k, v in raw.items()}
    for key in keys:
        val = raw.get(key)
        if val in (None, ""):
            val = lower.get(key.lower())
        if val not in (None, ""):
            return str(val).strip()
    return ""


def ingest_edgar(*, session=None, persist: bool = True) -> Dict[str, int]:
    from services import company_registry_service as reg

    rows = fetch_edgar_tickers(session=session)
    stats = {"seen": len(rows), "written": 0, "skipped": 0}
    if persist and rows:
        written = reg.bulk_upsert_companies(rows)
        stats["written"] = written.get("written", 0)
        stats["skipped"] = written.get("skipped", 0)
    return stats


def ingest_lca(path: str, *, limit: int = 0,
               persist: bool = True) -> Dict[str, int]:
    from services import company_registry_service as reg
    from services.state_registries import federal as fed

    stats = {"seen": 0, "written": 0, "skipped": 0}
    by_state: Dict[str, List[Dict[str, Any]]] = {}
    batch: List[Dict[str, Any]] = []
    for rec in iter_lca_rows(path, limit=limit):
        stats["seen"] += 1
        batch.append(rec)
        by_state.setdefault(rec["state"], []).append(rec)
        if len(batch) >= 1000:
            if persist:
                w = reg.bulk_upsert_companies(batch)
                stats["written"] += w.get("written", 0)
                stats["skipped"] += w.get("skipped", 0)
            batch.clear()
    if persist and batch:
        w = reg.bulk_upsert_companies(batch)
        stats["written"] += w.get("written", 0)
        stats["skipped"] += w.get("skipped", 0)
    for state, rows in by_state.items():
        fed.add_records(state, rows)
    return stats


def ingest_from_env(*, persist: bool = True) -> Dict[str, Any]:
    """Operator helper: pull whatever local files / network are configured."""
    out: Dict[str, Any] = {}
    lca = (os.getenv("H1B_LCA_PATH") or "").strip()
    if lca:
        try:
            out["lca"] = ingest_lca(lca, persist=persist)
        except Exception as exc:
            logger.warning("national_signals: LCA ingest failed: %s", exc)
            out["lca"] = {"error": type(exc).__name__}
    try:
        out["edgar"] = ingest_edgar(persist=persist)
    except Exception as exc:
        logger.warning("national_signals: EDGAR ingest failed: %s", exc)
        out["edgar"] = {"error": type(exc).__name__}
    return out
