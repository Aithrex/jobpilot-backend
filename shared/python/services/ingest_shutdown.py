"""One process-wide "the crawler is stopping" signal.

A deploy restarts the ingest runner. Shards run for up to
`JOB_INGEST_SHARD_BUDGET` (300 s on the box, twice that on a deep listing) and
the runner drained for 150 s, inside systemd's 180 s stop timeout. So every
deploy SIGKILLed the shards still running, and the ones that outlived the
interpreter's shutdown failed with "cannot schedule new futures after
interpreter shutdown": 349 failed shard runs on 2026-11-01..02, each one
recorded as a failure and backed off.

`request()` sets every running shard's own stop event. A stopped shard leaves
the same way one that hits its time budget does - it writes what it read,
closes postings only for boards it read in full, and releases its lease - so
stopping early loses no inventory. Shards that start after `request()` return
at once.
"""
from __future__ import annotations

import threading

EVENT = threading.Event()
_ACTIVE: set = set()
_LOCK = threading.Lock()


def register(stop: threading.Event) -> None:
    with _LOCK:
        _ACTIVE.add(stop)
    if EVENT.is_set():
        stop.set()


def unregister(stop: threading.Event) -> None:
    with _LOCK:
        _ACTIVE.discard(stop)


def request() -> int:
    """Ask every running shard to wrap up now. Returns how many were told."""
    EVENT.set()
    with _LOCK:
        active = list(_ACTIVE)
    for stop in active:
        stop.set()
    return len(active)


def requested() -> bool:
    return EVENT.is_set()


def reset() -> None:
    """Tests only."""
    EVENT.clear()
    with _LOCK:
        _ACTIVE.clear()
