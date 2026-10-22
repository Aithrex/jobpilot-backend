"""READ-ONLY, complete export of the job inventory for audit (CLAUDE-PROMPT I).

Every `job_postings` row selected by the filters is written EXACTLY ONCE, with
its company, title, source, source/original/canonical links, canonical
employer and requisition ids, business-model / relationship / availability
classification and evidence, timestamps, dedupe group, and an inclusion
decision with a reason. Nothing is sampled and there is no silent row cap.

Snapshot semantics
------------------
* `as_of` is fixed at start (or `--as-of`). Rows first seen AFTER it are not
  exported and are COUNTED (`arrived_after_as_of`), so the denominator is
  stable no matter how long the export runs. Rows updated after `as_of` are
  exported with their current state and counted (`updated_after_as_of`).
* Pages are read in `_id` order with a `_id > last` keyset, never skip/limit,
  so a concurrent insert or delete cannot shift a page and duplicate or drop
  a row.
* A page that fails (after one retry) stops the export and the summary says
  `partial: true` with the reason and the last `_id` reached. `--max-rows`
  exists for trial runs and also marks the output partial. A partial export is
  never presented as complete.

Decisions (one per row, in this order)
-------------------------------------
1. `excluded / closed:<status>`        - not active
2. `excluded / duplicate_of:<job_id>`  - same PROVEN identity as an earlier
   row (`posting_identity`: board+requisition, stored dedupe group, original
   apply URL, job id). Never company+title.
3. `excluded / policy:<reasons>`       - the strict direct-employer preset
   (`services.employer_policy`, when that module is present)
4. `review / unclassified` or `review / unknown_business_model` / `mixed`
5. `review / possible_duplicate_of:<job_id>` - same employer+title+location as
   an accepted row but no proven identity in common
6. `accepted`

Outputs under `--out` (CSV and JSON Lines, streamed - memory is bounded by the
identity index, O(distinct groups), not by the row payloads):
    all_rows, accepted_unique, exclusions, unknown_review  (.csv + .jsonl)
    summary.json, summary.csv

READ-ONLY, enforced twice: every pymongo write method is replaced with one
that raises before the database is opened, and the export only ever receives
a read-only view of the database (find / count only).

Usage (owner-run; reads the database named by the environment):
    python scripts/export_inventory_audit.py --out exports/inventory-2026-10-21
    python scripts/export_inventory_audit.py --out /tmp/x --status active --source workday
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "shared" / "python"))

EXPORT_VERSION = "inventory-export-1.0.0"
DEFAULT_PAGE_SIZE = 2000

_WRITE_METHODS = (
    "insert_one", "insert_many", "update_one", "update_many", "replace_one",
    "delete_one", "delete_many", "find_one_and_update", "find_one_and_replace",
    "find_one_and_delete", "bulk_write", "create_index", "create_indexes",
    "drop", "drop_index", "drop_indexes", "rename",
)

FIELDS = (
    "job_id", "doc_id", "source", "source_job_id", "status", "company", "title",
    "location", "source_url", "original_job_url", "canonical_url",
    "canonical_apply_url", "apply_url_key", "employer_id", "canonical_employer_id",
    "canonical_employer_basis", "ats", "tenant", "board_kind", "requisition_id",
    "requisition_key", "classification_status", "business_model_kind",
    "business_model_basis", "posting_relationship", "posting_purpose",
    "availability_state", "policy_reasons", "classification_evidence",
    "posted_at", "posted_at_confidence", "reposted_at", "first_seen_at",
    "last_seen_at", "last_verified_at", "valid_through", "closed_at",
    "updated_after_as_of", "dedupe_group", "dedupe_basis", "duplicate_of",
    "possible_duplicate_of", "decision", "reason",
)

_PROJECTION = {
    "job_id": 1, "source": 1, "source_job_id": 1, "status": 1, "employer_name": 1,
    "title_raw": 1, "locations": 1, "source_url": 1, "original_job_url": 1,
    "canonical_url": 1, "canonical_apply_url": 1, "apply_url_key": 1,
    "employer_id": 1, "canonical_employer_id": 1, "canonical_employer_basis": 1,
    "ats": 1, "tenant": 1, "board_kind": 1, "requisition_id": 1,
    "dedupe_group_id": 1, "posted_at": 1, "posted_at_confidence": 1,
    "reposted_at": 1, "first_seen_at": 1, "last_seen_at": 1,
    "last_verified_at": 1, "valid_through": 1, "closed_at": 1, "updated_at": 1,
    "employment_type": 1, "description_text": 1, "industry": 1, "tech": 1,
    "work_mode": 1, "seniority": 1, "cost_class": 1, "source_quality": 1,
}


# ---------------------------------------------------------------------------
# Read-only enforcement
# ---------------------------------------------------------------------------

def enforce_read_only() -> None:
    """Replace every pymongo write method with one that raises."""
    import pymongo.collection as _pc

    def _deny(name: str):
        def _raise(self, *args, **kwargs):
            raise RuntimeError(f"read-only export: refused {name} on {self.name}")
        return _raise

    for method in _WRITE_METHODS:
        if hasattr(_pc.Collection, method):
            setattr(_pc.Collection, method, _deny(method))


class _ReadOnlyCollection:
    """Exposes reads only; anything else raises AttributeError-free errors."""

    def __init__(self, coll: Any):
        self._c = coll
        self.name = getattr(coll, "name", "?")

    def find(self, *a, **k):
        return self._c.find(*a, **k)

    def find_one(self, *a, **k):
        return self._c.find_one(*a, **k)

    def count_documents(self, *a, **k):
        return self._c.count_documents(*a, **k)

    def __getattr__(self, name: str):
        raise RuntimeError(f"read-only export: refused {name} on {self.name}")


class ReadOnlyDB:
    def __init__(self, db: Any):
        self._db = db

    def __getitem__(self, name: str) -> _ReadOnlyCollection:
        return _ReadOnlyCollection(self._db[name])

    def __getattr__(self, name: str) -> _ReadOnlyCollection:
        if name.startswith("_"):
            raise AttributeError(name)
        return _ReadOnlyCollection(self._db[name])


# ---------------------------------------------------------------------------
# Classification (guarded: the policy module may not be present)
# ---------------------------------------------------------------------------

def _load_policy():
    try:
        from services import employer_policy  # type: ignore
        return employer_policy
    except Exception:
        return None


def classify(row: Dict[str, Any], policy: Any) -> Dict[str, Any]:
    out = {"classification_status": "unclassified", "business_model_kind": "unclassified",
           "business_model_basis": "", "posting_relationship": "unclassified",
           "posting_purpose": "unclassified", "availability_state": "unclassified",
           "policy_reasons": [], "classification_evidence": []}
    if policy is None:
        return out
    try:
        policy.annotate(row)
        model = row.get("employer_profile") or {}
        prof = row.get("posting_profile") or {}
        avail = row.get("availability") or {}
        reasons = list(policy.reasons_for(row, dict(policy.STRICT_EXCLUDE)))
    except Exception as exc:
        out["classification_evidence"] = [f"classification_error:{type(exc).__name__}"]
        return out
    out.update({
        "classification_status": "classified",
        "business_model_kind": str(model.get("kind") or "unknown"),
        "business_model_basis": str(model.get("basis") or ""),
        "posting_relationship": str(prof.get("relationship") or "unknown"),
        "posting_purpose": str(prof.get("purpose") or "unknown"),
        "availability_state": str(avail.get("state") or "unknown"),
        "policy_reasons": reasons,
        "classification_evidence": (list(model.get("evidence") or [])[:5]
                                    + list(prof.get("evidence") or [])[:5]),
    })
    return out


# ---------------------------------------------------------------------------
# Streaming writers
# ---------------------------------------------------------------------------

class _Sink:
    def __init__(self, out_dir: Path, name: str):
        self.csv_f = open(out_dir / f"{name}.csv", "w", newline="", encoding="utf-8")
        self.json_f = open(out_dir / f"{name}.jsonl", "w", encoding="utf-8")
        self.writer = csv.DictWriter(self.csv_f, fieldnames=list(FIELDS),
                                     extrasaction="ignore")
        self.writer.writeheader()
        self.count = 0

    def write(self, rec: Dict[str, Any]) -> None:
        flat = {k: (json.dumps(v, default=str) if isinstance(v, (list, dict)) else
                    ("" if v is None else v)) for k, v in rec.items()}
        self.writer.writerow(flat)
        self.json_f.write(json.dumps(rec, default=str, sort_keys=True) + "\n")
        self.count += 1

    def close(self) -> None:
        self.csv_f.close()
        self.json_f.close()


def _iso(value: Any) -> Optional[str]:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    return None if value in (None, "") else str(value)


def _as_utc(value: Any) -> Optional[datetime]:
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# The export
# ---------------------------------------------------------------------------

def _base_filter(as_of: datetime, status: Optional[str], source: Optional[str]) -> Dict[str, Any]:
    q: Dict[str, Any] = {"$or": [{"first_seen_at": {"$lte": as_of}},
                                 {"first_seen_at": None},
                                 {"first_seen_at": {"$exists": False}}]}
    if status:
        q["status"] = str(status)       # equality on a plain string: never an operator
    if source:
        q["source"] = str(source)
    return q


def _pages(coll: Any, query: Dict[str, Any], page_size: int,
           state: Dict[str, Any]) -> Iterator[List[Dict[str, Any]]]:
    last = None
    while True:
        q = dict(query)
        if last is not None:
            q["_id"] = {"$gt": last}
        page: Optional[List[Dict[str, Any]]] = None
        error: Optional[BaseException] = None
        for _attempt in range(2):
            try:
                page = list(coll.find(q, _PROJECTION).sort("_id", 1).limit(page_size))
                break
            except Exception as exc:     # retried once, then reported
                error = exc
        if page is None:
            state["partial"] = True
            state["partial_reasons"].append(
                f"page_failed_after_id:{last}:{type(error).__name__}")
            return
        if not page:
            return
        state["pages"] += 1
        last = page[-1]["_id"]
        state["last_id"] = str(last)
        yield page
        if len(page) < page_size:
            return


def run_export(db: Any, out_dir: Path, *, as_of: Optional[datetime] = None,
               status: Optional[str] = None, source: Optional[str] = None,
               page_size: int = DEFAULT_PAGE_SIZE, max_rows: Optional[int] = None,
               policy: Any = "auto") -> Dict[str, Any]:
    """Export the selected inventory. `db` is wrapped read-only. Returns the summary."""
    from services import posting_identity as pid

    rodb = db if isinstance(db, ReadOnlyDB) else ReadOnlyDB(db)
    coll = rodb["job_postings"]
    as_of = _as_utc(as_of) or datetime.now(timezone.utc)
    policy = _load_policy() if policy == "auto" else policy
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    query = _base_filter(as_of, status, source)
    state: Dict[str, Any] = {"partial": False, "partial_reasons": [], "pages": 0,
                             "last_id": None}
    sinks = {name: _Sink(out_dir, name) for name in
             ("all_rows", "accepted_unique", "exclusions", "unknown_review")}
    leader_of_key: Dict[str, Tuple[str, str]] = {}   # identity key -> (job_id, title)
    soft_first: Dict[str, str] = {}
    by_decision: Dict[str, int] = {}
    by_reason: Dict[str, int] = {}
    by_source: Dict[str, int] = {}
    by_kind: Dict[str, int] = {}
    newest_seen: Optional[datetime] = None
    oldest_seen: Optional[datetime] = None
    updated_after = 0
    seen_doc_ids: set = set()
    duplicates_in_stream = 0
    total = 0

    try:
        for page in _pages(coll, query, page_size, state):
            for doc in page:
                if max_rows is not None and total >= max_rows:
                    state["partial"] = True
                    state["partial_reasons"].append(f"max_rows_reached:{max_rows}")
                    raise StopIteration
                doc_id = str(doc.get("_id"))
                if doc_id in seen_doc_ids:       # cannot happen with _id keyset; proven
                    duplicates_in_stream += 1
                    continue
                seen_doc_ids.add(doc_id)
                total += 1
                rec = _record(doc, pid, policy, leader_of_key, soft_first, as_of)
                if rec["updated_after_as_of"]:
                    updated_after += 1
                seen = _as_utc(doc.get("last_seen_at"))
                if seen:
                    newest_seen = seen if newest_seen is None or seen > newest_seen else newest_seen
                    oldest_seen = seen if oldest_seen is None or seen < oldest_seen else oldest_seen
                by_decision[rec["decision"]] = by_decision.get(rec["decision"], 0) + 1
                reason_key = rec["reason"].split(":", 1)[0] if rec["reason"] else "accepted"
                by_reason[reason_key] = by_reason.get(reason_key, 0) + 1
                by_source[rec["source"]] = by_source.get(rec["source"], 0) + 1
                by_kind[rec["business_model_kind"]] = by_kind.get(rec["business_model_kind"], 0) + 1
                sinks["all_rows"].write(rec)
                target = {"accepted": "accepted_unique", "excluded": "exclusions",
                          "review": "unknown_review"}[rec["decision"]]
                sinks[target].write(rec)
    except StopIteration:
        pass
    finally:
        for sink in sinks.values():
            sink.close()

    try:
        arrived_after = coll.count_documents(
            {**{k: v for k, v in query.items() if k != "$or"},
             "first_seen_at": {"$gt": as_of}})
    except Exception as exc:
        arrived_after = None
        state["partial_reasons"].append(f"arrived_after_count_failed:{type(exc).__name__}")

    summary = {
        "export_version": EXPORT_VERSION,
        "identity_version": pid.IDENTITY_VERSION,
        "as_of": as_of.isoformat(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "filters": {"status": status, "source": source},
        "partial": bool(state["partial"]),
        "partial_reasons": state["partial_reasons"],
        "pages_read": state["pages"],
        "page_size": page_size,
        "last_id": state["last_id"],
        "classification_module": "services.employer_policy" if policy is not None
        else "absent (every row 'unclassified')",
        "denominators": {
            "input_rows_exported": total,
            "arrived_after_as_of_not_exported": arrived_after,
            "updated_after_as_of_exported": updated_after,
            "stream_duplicates_skipped": duplicates_in_stream,
        },
        "files": {name: sink.count for name, sink in sinks.items()},
        "by_decision": dict(sorted(by_decision.items())),
        "by_reason": dict(sorted(by_reason.items())),
        "by_source": dict(sorted(by_source.items())),
        "by_business_model": dict(sorted(by_kind.items())),
        "data_age": {
            "newest_last_seen_at": _iso(newest_seen),
            "oldest_last_seen_at": _iso(oldest_seen),
            "age_hours_of_newest": round((as_of - newest_seen).total_seconds() / 3600, 2)
            if newest_seen else None,
        },
        "every_row_once": sinks["all_rows"].count == total and (
            sinks["accepted_unique"].count + sinks["exclusions"].count
            + sinks["unknown_review"].count) == total,
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str),
                                          encoding="utf-8")
    with open(out_dir / "summary.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["metric", "value"])
        for key, value in _flatten(summary):
            w.writerow([key, value])
    return summary


def _flatten(obj: Any, prefix: str = "") -> Iterator[Tuple[str, Any]]:
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _flatten(v, f"{prefix}.{k}" if prefix else str(k))
    elif isinstance(obj, list):
        yield prefix, json.dumps(obj, default=str)
    else:
        yield prefix, obj


def _record(doc: Dict[str, Any], pid: Any, policy: Any,
            leader_of_key: Dict[str, Tuple[str, str]], soft_first: Dict[str, str],
            as_of: datetime) -> Dict[str, Any]:
    locs = doc.get("locations") or []
    location = (str(locs[0].get("raw") or "") if locs and isinstance(locs[0], dict) else "")
    row = {
        "job_id": str(doc.get("job_id") or ""), "company": str(doc.get("employer_name") or ""),
        "title": str(doc.get("title_raw") or ""), "location": location,
        "canonical_url": doc.get("canonical_url") or "",
        "canonical_apply_url": doc.get("canonical_apply_url") or "",
        "original_job_url": doc.get("original_job_url") or "",
        "apply_url_key": doc.get("apply_url_key") or "",
        "canonical_employer_id": doc.get("canonical_employer_id") or "",
        "ats": doc.get("ats") or "", "tenant": doc.get("tenant") or "",
        "source_job_id": doc.get("source_job_id") or "",
        "requisition_id": doc.get("requisition_id") or "",
        "dedupe_group_id": doc.get("dedupe_group_id") or "",
    }
    keys: List[str] = []
    req = pid.requisition_key(row)
    if req:
        keys.append("req|" + req)
    if row["dedupe_group_id"]:
        keys.append("grp|" + row["dedupe_group_id"])
    url_key = row["apply_url_key"] or pid.apply_url_key(pid.original_url_of(row))
    if url_key:
        keys.append("url|" + url_key)
    if row["job_id"]:
        keys.append("job|" + row["job_id"])

    duplicate_of, basis = "", ""
    for key in keys:
        hit = leader_of_key.get(key)
        if not hit or hit[0] == row["job_id"]:
            continue
        if key.startswith("url|") and not pid.titles_compatible(
                {"title": hit[1]}, {"title": row["title"]}):
            continue        # a URL fronting different roles is a portal, not identity
        duplicate_of, basis = hit[0], key.split("|", 1)[0]
        break
    group = keys[0] if keys else f"doc|{doc.get('_id')}"
    if duplicate_of:
        group = next(k for k in keys if k.split("|", 1)[0] == basis)
    for key in keys:
        leader_of_key.setdefault(key, (duplicate_of or row["job_id"], row["title"]))

    status = str(doc.get("status") or "")
    cls = classify(dict(doc, company=row["company"], title=row["title"],
                        location=location), policy)
    soft = pid.possible_duplicate_key(row)
    possible_of = ""
    if soft:
        first = soft_first.get(soft)
        if first is None:
            soft_first[soft] = row["job_id"]
        elif first != row["job_id"] and first != duplicate_of:
            possible_of = first

    if status and status != "active":
        decision, reason = "excluded", f"closed:{status}"
    elif duplicate_of:
        decision, reason = "excluded", f"duplicate_of:{duplicate_of}"
    elif cls["policy_reasons"]:
        decision, reason = "excluded", "policy:" + ",".join(cls["policy_reasons"])
    elif cls["classification_status"] != "classified":
        decision, reason = "review", "unclassified"
    elif cls["business_model_kind"] in ("unknown", "mixed"):
        decision, reason = "review", f"{cls['business_model_kind']}_business_model"
    elif possible_of:
        decision, reason = "review", f"possible_duplicate_of:{possible_of}"
    else:
        decision, reason = "accepted", ""

    updated = _as_utc(doc.get("updated_at"))
    return {
        "job_id": row["job_id"], "doc_id": str(doc.get("_id")),
        "source": str(doc.get("source") or ""), "source_job_id": row["source_job_id"],
        "status": status, "company": row["company"], "title": row["title"],
        "location": location, "source_url": doc.get("source_url") or "",
        "original_job_url": row["original_job_url"], "canonical_url": row["canonical_url"],
        "canonical_apply_url": row["canonical_apply_url"], "apply_url_key": url_key,
        "employer_id": doc.get("employer_id") or "",
        "canonical_employer_id": row["canonical_employer_id"],
        "canonical_employer_basis": doc.get("canonical_employer_basis") or "",
        "ats": row["ats"], "tenant": row["tenant"], "board_kind": doc.get("board_kind") or "",
        "requisition_id": pid.requisition_id_of(row), "requisition_key": req,
        **cls,
        "posted_at": _iso(doc.get("posted_at")),
        "posted_at_confidence": doc.get("posted_at_confidence") or "",
        "reposted_at": _iso(doc.get("reposted_at")),
        "first_seen_at": _iso(doc.get("first_seen_at")),
        "last_seen_at": _iso(doc.get("last_seen_at")),
        "last_verified_at": _iso(doc.get("last_verified_at")),
        "valid_through": _iso(doc.get("valid_through")),
        "closed_at": _iso(doc.get("closed_at")),
        "updated_after_as_of": bool(updated and updated > as_of),
        "dedupe_group": group, "dedupe_basis": basis or (group.split("|", 1)[0]),
        "duplicate_of": duplicate_of, "possible_duplicate_of": possible_of,
        "decision": decision, "reason": reason,
    }


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--out", required=True, help="output directory (created)")
    ap.add_argument("--as-of", help="ISO-8601 snapshot watermark (default: now)")
    ap.add_argument("--status", help="only this status (default: every status)")
    ap.add_argument("--source", help="only this source")
    ap.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE)
    ap.add_argument("--max-rows", type=int, default=None,
                    help="trial runs only; the output is then marked partial")
    args = ap.parse_args(argv)

    enforce_read_only()          # BEFORE the database is opened
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except ImportError:          # pragma: no cover
        pass
    from utils.db_connect import DBConnect

    as_of = datetime.fromisoformat(args.as_of) if args.as_of else None
    summary = run_export(DBConnect().get_db(), Path(args.out), as_of=as_of,
                         status=args.status, source=args.source,
                         page_size=max(1, args.page_size), max_rows=args.max_rows)
    print(json.dumps({k: summary[k] for k in ("as_of", "partial", "partial_reasons",
                                               "denominators", "by_decision", "files")},
                     indent=2, default=str))
    return 2 if summary["partial"] else 0


if __name__ == "__main__":
    sys.exit(main())
