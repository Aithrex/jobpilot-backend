"""The measured coverage numbers, stored once and read everywhere.

Why a snapshot rather than a live query
---------------------------------------
`scripts/employer_coverage_report.py` reads five collections, classifies
23,782 employers and takes ~2 minutes. That is the right cost for an
accountability artefact and the wrong cost for a banner on a search page — so
the banner used to show a DIFFERENT, cheaper set of numbers, and the two
disagreed. One of them said 43%.

So the expensive measurement runs on a schedule, writes ONE document here, and
every surface reads that document. There is exactly one set of coverage
numbers in the system, and every consumer can see how old it is.

Freshness is part of the payload, not an assumption
----------------------------------------------------
Every snapshot carries `measured_at`, `inventory_watermark`, `source_revision`
and the taxonomy/registry versions it was computed under. `age_hours` and
`stale` are derived on read. A client that renders a coverage figure without
rendering its age is making a claim the data does not support — the numbers
move on a crawl cadence, and a figure measured before a verification sweep is
history, not status.

`STALE_AFTER_HOURS` is deliberately generous: coverage moves when somebody
runs a verification pass, which is a human-scheduled event, not a continuous
one. A snapshot six hours old is current; one six days old is a document about
last week, and saying so is the whole point.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

SNAPSHOT_VERSION = "coverage-snapshot-1.0.0"
COVERAGE_SNAPSHOTS = "coverage_snapshots"

#: Older than this and a reader must present the number as historical.
STALE_AFTER_HOURS = float(24 * 3)

#: The keys a snapshot must carry to be usable. A partial snapshot is refused
#: on write rather than being served with holes in it — a banner that renders
#: `covered_technology_employers` as 0 because a field was missing is worse
#: than a banner that says the measurement is unavailable.
REQUIRED_KEYS = (
    "technology_employer_denominator", "covered_technology_employers",
    "employer_coverage_pct", "covered_career_sources",
    "ownership_verified_unvalidated_sources", "candidate_technology_employers",
    "measured_at",
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _db():
    from utils.db_connect import DBConnect
    return DBConnect().get_db()


def ensure_indexes(*, db=None) -> List[str]:
    db = db if db is not None else _db()
    try:
        return [db[COVERAGE_SNAPSHOTS].create_index(
            [("measured_at", -1)], name="coverage_snapshot_recent")]
    except Exception:      # pragma: no cover
        return []


def save(payload: Dict[str, Any], *, db=None) -> Optional[str]:
    """Store one measurement. Returns its id, or None when it is incomplete."""
    missing = [k for k in REQUIRED_KEYS if payload.get(k) is None]
    if missing:
        logger.warning("coverage_snapshot: refusing an incomplete snapshot "
                       "(missing %s)", ", ".join(missing))
        return None
    db = db if db is not None else _db()
    doc = dict(payload)
    doc["snapshot_version"] = SNAPSHOT_VERSION
    doc.setdefault("stored_at", _now())
    db[COVERAGE_SNAPSHOTS].insert_one(doc)
    return str(doc.get("measured_at"))


def latest(*, db=None) -> Optional[Dict[str, Any]]:
    """The newest snapshot, with its age and staleness derived. `None` if none."""
    db = db if db is not None else _db()
    try:
        doc = db[COVERAGE_SNAPSHOTS].find_one({}, {"_id": 0},
                                              sort=[("measured_at", -1)])
    except Exception as exc:      # pragma: no cover
        logger.warning("coverage_snapshot: unavailable (%s)", type(exc).__name__)
        return None
    if not doc:
        return None
    return annotate(doc)


def annotate(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Add `age_hours` and `stale`. Pure, so a caller can annotate any snapshot."""
    out = dict(doc)
    measured = out.get("measured_at")
    if isinstance(measured, str):
        try:
            measured = datetime.fromisoformat(measured.replace("Z", "+00:00"))
        except ValueError:
            measured = None
    if isinstance(measured, datetime):
        if measured.tzinfo is None:
            measured = measured.replace(tzinfo=timezone.utc)
        age = (_now() - measured).total_seconds() / 3600.0
        out["age_hours"] = round(age, 2)
        out["stale"] = age > STALE_AFTER_HOURS
    else:
        out["age_hours"] = None
        # No timestamp is not "fresh". A measurement that cannot say when it
        # was taken has to be presented as historical.
        out["stale"] = True
    return out


def prune(*, keep: int = 60, db=None) -> int:
    """Keep the most recent `keep` snapshots; the rest are history on disk.

    Retained rather than overwritten so a coverage number can be attributed to
    a date and a revision — "it was 33.4% at 3e0b4c9" is a claim somebody must
    be able to check.
    """
    db = db if db is not None else _db()
    ids = [d["_id"] for d in db[COVERAGE_SNAPSHOTS]
           .find({}, {"_id": 1}).sort("measured_at", -1).skip(int(keep))]
    if not ids:
        return 0
    return int(db[COVERAGE_SNAPSHOTS].delete_many({"_id": {"$in": ids}}).deleted_count)
