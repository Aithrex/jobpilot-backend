"""Auto-apply observability — structured, PII-FREE milestone metrics (spec §11).

A single, dependency-free emitter used across the execution plane (AWS side +
the Cloud Run worker) to log ONE structured line per lifecycle milestone with
stable correlation ids. CloudWatch metric filters / GCP log-based metrics parse
these lines into the alarms listed in ``docs/ops/auto-apply-observability.md``.

HARD RULE (control C6): a metric line NEVER carries answers, résumé bytes/keys,
mail, credentials, cookies, or any PII. This module enforces that structurally:
only a fixed allow-list of scalar fields is ever serialized, and every value is
coerced to a bounded scalar (str/int/float/bool) — an arbitrary object is dropped,
not stringified. Callers therefore cannot accidentally leak a PII payload through
``**extra``.

Emitted shape (one compact JSON object behind a stable ``apply_metric`` prefix so
a metric filter can anchor on it)::

    apply_metric {"metric":"apply.milestone","phase":"submit","outcome":"submitted",
                  "execution_id":"...","application_id":"...","adapter":"greenhouse",
                  "duration_ms":812,"ts":"2026-07-24T12:00:00.000Z"}

Nothing here is request-scoped, does any I/O, or imports a heavy dependency — it
is safe to call from a hot path and from the worker image.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger("apply.metrics")

# Stable line prefix a CloudWatch metric filter / GCP log metric anchors on.
_PREFIX = "apply_metric"

# The ONLY milestone phases we emit — a bounded vocabulary keeps the metric
# cardinality (and the alarm set) closed. Anything else is coerced to "unknown".
PHASES = (
    "queue_ack",           # Cloud Task accepted / worker received the task
    "task_start",          # worker began the run (post-claim)
    "fill_start",          # adapter began filling the form
    "submit",              # the Submit click boundary
    "confirmation",        # a confirmed submission was observed
    "submission_unknown",  # clicked, outcome ambiguous — HELD, never retried
    "waiting_human",       # captcha / verify wall — handed back to the human
    "pause",               # execution paused (kill switch / user pause / revocation)
    "selector_drift",      # an adapter selector missed (drift / circuit-breaker signal)
)

# Correlation + measurement fields allowed on a line. NO free-text, NO payloads.
_ALLOWED_FIELDS = (
    "execution_id", "application_id", "adapter", "phase", "outcome",
    "duration_ms", "reason", "ats", "attempt", "count",
)

# ``reason``/``outcome`` are short bounded enums (never user text) — cap defensively.
_MAX_SCALAR_LEN = 64


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _scalar(value: Any) -> Optional[Any]:
    """Coerce to a bounded, PII-safe scalar or drop it. bool before int (bool is
    a subclass of int). Non-scalars (dict/list/objects) are DROPPED, never
    stringified — that is the structural guarantee against leaking a payload."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value
    if isinstance(value, str):
        return value[:_MAX_SCALAR_LEN]
    return None


def emit(phase: str, *, execution_id: str = "", application_id: str = "",
         adapter: str = "", outcome: Optional[str] = None,
         duration_ms: Optional[int] = None, **extra: Any) -> None:
    """Emit ONE structured, PII-free milestone metric line. Best-effort: a
    logging fault never propagates to the caller (metrics must not break a run)."""
    try:
        ph = phase if phase in PHASES else "unknown"
        line: dict = {"metric": "apply.milestone", "phase": ph}
        # Correlation ids + core measurements (all bounded scalars).
        candidates = {
            "execution_id": execution_id,
            "application_id": application_id,
            "adapter": adapter,
            "outcome": outcome,
            "duration_ms": duration_ms,
        }
        for k, v in extra.items():
            if k in _ALLOWED_FIELDS:
                candidates[k] = v
        for k in _ALLOWED_FIELDS:
            if k in ("phase",):
                continue
            if k in candidates:
                s = _scalar(candidates[k])
                if s is not None and s != "":
                    line[k] = s
        line["ts"] = _now_iso()
        logger.info("%s %s", _PREFIX, json.dumps(line, separators=(",", ":"),
                                                 sort_keys=True))
    except Exception:  # pragma: no cover - metrics are never load-bearing
        pass


class Timer:
    """Tiny monotonic stopwatch → ``duration_ms`` for a milestone. Usage::

        with apply_metrics.Timer() as t:
            ...work...
        apply_metrics.emit("submit", ..., duration_ms=t.ms)
    """

    def __init__(self) -> None:
        self._start = time.monotonic()
        self.ms: int = 0

    def __enter__(self) -> "Timer":
        self._start = time.monotonic()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.ms = int((time.monotonic() - self._start) * 1000)

    def lap(self) -> int:
        """Milliseconds elapsed since construction (without closing the timer)."""
        return int((time.monotonic() - self._start) * 1000)
