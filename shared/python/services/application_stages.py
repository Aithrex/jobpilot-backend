"""When an application first reached each stage.

`application.status` says where an application is now. A funnel has to know
where it has BEEN: an application rejected after two interviews still reached
interview, and the status alone has forgotten that.

`application.stage_dates.<status>` holds the first time each status was set.
Writers add `first_reached(...)` to their update as `$min`, so the field is
set once by whichever writer gets there first, and an earlier fact (the date on
the employer's email) replaces a later one (the moment a sync happened to run).

Additive: nothing reads it for behaviour, and removing the field only makes the
tracker's analytics fall back to the current status.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

# `saved` and `draft` are not stages of an application; nothing was sent yet.
STAGES = ("applied", "interviewing", "offer", "rejected", "ghosted", "withdrawn")


def _naive_utc(value: Optional[datetime]) -> datetime:
    """Stored naive, in UTC, like every other date on a tailoring record."""
    if not isinstance(value, datetime):
        return datetime.now(timezone.utc).replace(tzinfo=None)
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def first_reached(status: Any, at: Optional[datetime] = None) -> Dict[str, datetime]:
    """The `$min` fragment for a move to `status`, or {} when it is not a stage."""
    if status not in STAGES:
        return {}
    return {f"application.stage_dates.{status}": _naive_utc(at)}


def serialize(application: Dict[str, Any]) -> None:
    """ISO-8601 with an explicit UTC offset, in place. Without the offset a
    browser reads the date as local time and the day can shift."""
    dates = application.get("stage_dates")
    if not isinstance(dates, dict):
        return
    out = {}
    for stage, value in dates.items():
        if isinstance(value, datetime):
            value = (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).isoformat()
        out[stage] = value
    application["stage_dates"] = out
