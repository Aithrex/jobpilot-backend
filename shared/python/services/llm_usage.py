"""Per-feature ledger of LLM tokens and latency (zero AI, never raises).

Nothing recorded which feature spent what, so a cost question could only be
answered by re-running a feature by hand and reading `usage_metadata`. Every
Gemini call now reports here from `GeminiProvider._generate_one`:

* a log line, `llm_usage job=... caller=... model=... in=... out=...`;
* one `$inc` into `llm_usage_daily`, keyed by UTC day, job, caller and model.

`job` is the async job type (`tailor`, `cover_letter`, ...) when the call runs
inside `resume_service._process_job`, else `-`. `caller` is the first frame
outside the LLM plumbing, as `module.function`, so the ~40 AI call sites are
told apart without touching any of them.

The database write runs on one background thread with a bounded backlog, so a
slow or unreachable Atlas never delays an AI call; over the bound, rows are
dropped and counted in the log. Off under pytest unless `LLM_USAGE_LEDGER=on`,
and `LLM_USAGE_LEDGER=off` turns the write off anywhere (the log line stays).

Read it with `print_report(days)`; see `infra/compose/README.md`.
"""
from __future__ import annotations

import contextvars
import functools
import logging
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

COLLECTION = "llm_usage_daily"
RETENTION_DAYS = 400
MAX_BACKLOG = 500

_job: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("llm_job", default=None)

# Frames that are LLM plumbing rather than the feature asking for the call.
_PLUMBING = (
    "/llm_providers/", "/claude_client.py", "/llm_usage.py",
    "/llm_response_cache.py", "/contextlib.py", "/concurrent/",
)

_executor: Optional[ThreadPoolExecutor] = None
_executor_lock = threading.Lock()
_backlog = 0
_backlog_lock = threading.Lock()
_dropped = 0
_indexed = False


def tag_job(fn):
    """Decorator for `_process_job(job_id, job_type, payload)`: tags its calls."""
    @functools.wraps(fn)
    def wrapped(job_id, job_type, *args, **kwargs):
        token = _job.set(str(job_type or "-"))
        try:
            return fn(job_id, job_type, *args, **kwargs)
        finally:
            _job.reset(token)
    return wrapped


def current_job() -> str:
    return _job.get() or "-"


def current_caller() -> str:
    """`module.function` of the first frame outside the LLM plumbing."""
    frame = sys._getframe(1)
    while frame is not None:
        path = frame.f_code.co_filename.replace("\\", "/")
        if not any(p in path for p in _PLUMBING) and (
            "/services/" in path or "/blueprints/" in path or "/scripts/" in path
        ):
            module = os.path.splitext(os.path.basename(path))[0]
            return f"{module}.{frame.f_code.co_name}"
        frame = frame.f_back
    return "unknown"


def _ledger_enabled() -> bool:
    flag = os.environ.get("LLM_USAGE_LEDGER", "").strip().lower()
    if flag in {"off", "false", "0"}:
        return False
    if flag in {"on", "true", "1"}:
        return True
    from utils.db_connect import _under_test_runner
    return not _under_test_runner()


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def usage_counts(usage: Any) -> Dict[str, int]:
    """Token counts from a google-genai `usage_metadata` (None-safe)."""
    return {
        "prompt_tokens": _int(getattr(usage, "prompt_token_count", 0)),
        "output_tokens": _int(getattr(usage, "candidates_token_count", 0)),
        "thinking_tokens": _int(getattr(usage, "thoughts_token_count", 0)),
        "cached_tokens": _int(getattr(usage, "cached_content_token_count", 0)),
    }


def record(*, model: str, usage: Any = None, seconds: float = 0.0, ok: bool = True,
           cache_hit: bool = False) -> Optional[Dict[str, Any]]:
    """Log one call and queue its ledger row. Never raises."""
    try:
        counts = usage_counts(usage)
        row = {
            "day": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "job": current_job(),
            "caller": current_caller(),
            "model": model or "unknown",
            "ms": max(0, int(round(seconds * 1000))),
            "ok": bool(ok),
            "cache_hit": bool(cache_hit),
            **counts,
        }
        logger.info(
            "llm_usage job=%s caller=%s model=%s ok=%s cache_hit=%s in=%d out=%d "
            "think=%d cached=%d ms=%d",
            row["job"], row["caller"], row["model"], row["ok"], row["cache_hit"],
            counts["prompt_tokens"], counts["output_tokens"],
            counts["thinking_tokens"], counts["cached_tokens"], row["ms"],
        )
        if _ledger_enabled():
            _submit(row)
        return row
    except Exception as exc:  # accounting must never break an AI call
        logger.debug("llm_usage: record failed: %r", exc)
        return None


def _submit(row: Dict[str, Any]) -> None:
    global _executor, _backlog, _dropped
    with _backlog_lock:
        if _backlog >= MAX_BACKLOG:
            _dropped += 1
            if _dropped % 100 == 1:
                logger.warning("llm_usage: ledger backlog full, %d rows dropped", _dropped)
            return
        _backlog += 1
    with _executor_lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="llm-usage")
    _executor.submit(_write_safely, row)


def _write_safely(row: Dict[str, Any]) -> None:
    global _backlog
    try:
        write_row(_db(), row)
    except Exception as exc:
        logger.debug("llm_usage: ledger write failed: %r", exc)
    finally:
        with _backlog_lock:
            _backlog -= 1


def _db():
    from utils.db_connect import DBConnect
    return DBConnect().get_db()


def _row_key(row: Dict[str, Any]) -> str:
    return "|".join([row["day"], row["job"], row["caller"], row["model"]])


def write_row(db, row: Dict[str, Any]) -> None:
    """One `$inc` per call; the document count stays days x features x models."""
    global _indexed
    coll = db[COLLECTION]
    if not _indexed:
        try:
            coll.create_index("expire_at", expireAfterSeconds=0, name="ttl_expire_at")
            coll.create_index("day", name="day")
        except Exception as exc:  # an index is an optimisation
            logger.debug("llm_usage: index ensure failed: %r", exc)
        _indexed = True
    day_start = datetime.strptime(row["day"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    coll.update_one(
        {"_id": _row_key(row)},
        {
            "$setOnInsert": {
                "day": row["day"], "job": row["job"], "caller": row["caller"],
                "model": row["model"],
                "expire_at": day_start + timedelta(days=RETENTION_DAYS),
            },
            "$inc": {
                "calls": 1,
                "failed": 0 if row["ok"] else 1,
                "cache_hits": 1 if row["cache_hit"] else 0,
                "prompt_tokens": row["prompt_tokens"],
                "output_tokens": row["output_tokens"],
                "thinking_tokens": row["thinking_tokens"],
                "cached_tokens": row["cached_tokens"],
                "ms_total": row["ms"],
            },
            "$max": {"ms_max": row["ms"]},
        },
        upsert=True,
    )


def flush(timeout: float = 5.0) -> None:
    """Wait for queued ledger writes (for scripts that exit right after)."""
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        with _backlog_lock:
            if _backlog <= 0:
                return
        time.sleep(0.05)


# ---------------------------------------------------------------------------
# Reading it back
# ---------------------------------------------------------------------------

# USD per 1M tokens: gemini-3.8-flash standard text on the `us` ("Regional")
# endpoint, from the Cloud Billing catalogue on 2026-11-10 (`global` is
# 1.50 / 7.50). Override with LLM_PRICE_IN_PER_M / LLM_PRICE_OUT_PER_M.
# Thinking tokens bill as output.
def _price(env: str, default: float) -> float:
    try:
        return float(os.environ.get(env, "") or default)
    except ValueError:
        return default


def usage_report(db, days: int = 7, by: str = "caller") -> List[Dict[str, Any]]:
    """Totals per `job` or `caller` over the last `days` UTC days, costliest first."""
    since = (datetime.now(timezone.utc) - timedelta(days=max(1, days) - 1)).strftime("%Y-%m-%d")
    price_in = _price("LLM_PRICE_IN_PER_M", 1.65)
    price_out = _price("LLM_PRICE_OUT_PER_M", 8.25)
    totals: Dict[str, Dict[str, Any]] = {}
    for doc in db[COLLECTION].find({"day": {"$gte": since}}):
        key = doc.get("job", "-") if by == "job" else f"{doc.get('job', '-')} / {doc.get('caller', '?')}"
        agg = totals.setdefault(key, {
            "key": key, "calls": 0, "failed": 0, "cache_hits": 0, "prompt_tokens": 0,
            "output_tokens": 0, "thinking_tokens": 0, "cached_tokens": 0,
            "ms_total": 0, "ms_max": 0,
        })
        for field in ("calls", "failed", "cache_hits", "prompt_tokens", "output_tokens",
                      "thinking_tokens", "cached_tokens", "ms_total"):
            agg[field] += _int(doc.get(field))
        agg["ms_max"] = max(agg["ms_max"], _int(doc.get("ms_max")))
    rows = list(totals.values())
    for r in rows:
        r["usd_estimate"] = round(
            r["prompt_tokens"] / 1e6 * price_in
            + (r["output_tokens"] + r["thinking_tokens"]) / 1e6 * price_out, 4)
        r["ms_avg"] = int(r["ms_total"] / r["calls"]) if r["calls"] else 0
    rows.sort(key=lambda r: r["usd_estimate"], reverse=True)
    return rows


def print_report(days: int = 7, by: str = "caller", db=None) -> None:
    rows = usage_report(db if db is not None else _db(), days=days, by=by)
    print(f"LLM usage, last {days} day(s) UTC, by {by} (USD is an estimate; "
          f"LLM_PRICE_IN_PER_M / LLM_PRICE_OUT_PER_M set the rates)")
    header = f"{'feature':60} {'calls':>6} {'fail':>5} {'hits':>5} {'in':>10} {'out+think':>10} {'avg s':>6} {'max s':>6} {'USD':>8}"
    print(header)
    print("-" * len(header))
    total = 0.0
    for r in rows:
        total += r["usd_estimate"]
        print(f"{r['key'][:60]:60} {r['calls']:>6} {r['failed']:>5} {r['cache_hits']:>5} "
              f"{r['prompt_tokens']:>10} {r['output_tokens'] + r['thinking_tokens']:>10} "
              f"{r['ms_avg'] / 1000:>6.1f} {r['ms_max'] / 1000:>6.1f} {r['usd_estimate']:>8.4f}")
    print(f"{'total':60} {'':>6} {'':>5} {'':>5} {'':>10} {'':>10} {'':>6} {'':>6} {total:>8.4f}")
