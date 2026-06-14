"""
Thin facade over `datadog_lambda.metric` that is safe to import everywhere.

If the Datadog Lambda layer isn't attached (local dev, tests, or
enable_datadog=false in Terraform), every helper here becomes a no-op so
business code can call `dd_metric(...)` and `dd_span(...)` unconditionally.

Usage:
    from utils.datadog_metrics import dd_metric, dd_span

    dd_metric("portfolio.resume.download", 1, tags=["variant:tailored"])

    with dd_span("ai.gemini.generate", tags={"model": "gemini-2.5-flash"}):
        response = gemini_client.generate(prompt)

Metric naming: `portfolio.<domain>.<action>` — keep cardinality low.
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Iterator, Mapping, Sequence

_log = logging.getLogger(__name__)

# Resolve once at import. The Lambda Extension sets DD_LAMBDA_HANDLER, so its
# presence is a reliable signal that we're running with Datadog attached.
_DD_ATTACHED = bool(os.getenv("DD_LAMBDA_HANDLER")) or bool(os.getenv("DD_API_KEY")) or bool(os.getenv("DD_API_KEY_SECRET_ARN"))

try:
    if _DD_ATTACHED:
        from datadog_lambda.metric import lambda_metric as _lambda_metric  # type: ignore
        from ddtrace import tracer as _tracer  # type: ignore
        _AVAILABLE = True
    else:
        _AVAILABLE = False
except ImportError:
    _AVAILABLE = False


def dd_metric(
    name: str,
    value: float = 1,
    tags: Sequence[str] | None = None,
) -> None:
    """Submit a custom Datadog metric. No-op when Datadog isn't attached."""
    if not _AVAILABLE:
        return
    try:
        _lambda_metric(metric_name=name, value=value, tags=list(tags or []))
    except Exception as exc:  # pragma: no cover — never let metrics break business code
        _log.debug("dd_metric failed for %s: %s", name, exc)


@contextmanager
def dd_span(
    operation: str,
    tags: Mapping[str, str | int | float] | None = None,
    resource: str | None = None,
) -> Iterator[object]:
    """
    Open a manual APM span around a block of code. Use for sub-second work
    that ddtrace's auto-instrumentation doesn't cover (e.g. LLM calls, custom
    parsing pipelines).

    No-op fall-through when Datadog isn't attached, so the `with` body still
    runs normally.
    """
    if not _AVAILABLE:
        yield None
        return
    try:
        with _tracer.trace(operation) as span:
            if resource:
                span.resource = resource
            for k, v in (tags or {}).items():
                span.set_tag(k, v)
            yield span
    except Exception:  # pragma: no cover
        yield None


__all__ = ["dd_metric", "dd_span"]
