"""Bounded history reads with owner-scoped filters and signed keyset cursors."""
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone

from flask import current_app
from itsdangerous import BadSignature, URLSafeSerializer

from services.resume_versioning import serialize_record


PRESETS = ("week", "month", "highAts", "needsWork", "multiVersion", "offers", "interviewing", "pending")
STATUSES = ("saved", "draft", "applied", "interviewing", "offer", "rejected", "withdrawn", "ghosted")
SCORE = {"$cond": [{"$isNumber": "$ats_scores.overall"}, "$ats_scores.overall", -1]}
# Inclusion projection: new, potentially large fields never leak into a list.
PROJECTION = {key: 1 for key in (
    "record_id", "user_email", "created_at", "updated_at", "source", "source_job_id",
    "current_version_id", "base_resume_id", "base_resume_filename", "base_selection.filename",
    "jd_analysis.job_title", "jd_analysis.company", "jd_analysis.required_skills",
    "jd_analysis.location", "jd_analysis.seniority", "ats_scores.overall", "ats_scores.schema_version",
    "application.status", "application.next_action_date", "interview_prep.generated_at",
    "interview_prep.grounded_version_id", "versions.version_id", "versions.version_number",
    "versions.source", "versions.parent_version_id", "versions.content_hash", "versions.created_at",
    "versions.user_feedback", "versions.ats_scores.overall", "versions.ats_scores.schema_version",
    "versions.files.pdf.s3_key", "versions.files.pdf.filename", "versions.files.pdf.rendered_at",
    "versions.files.docx.s3_key", "versions.files.docx.filename", "versions.files.docx.rendered_at",
)}
PROJECTION.update({"_id": 0, "_history_score": SCORE})


def _date(value):
    parsed = datetime.strptime(value, "%Y-%m-%d")
    if parsed.strftime("%Y-%m-%d") != value:
        raise ValueError("Dates must use YYYY-MM-DD")
    return parsed


def _options(args):
    result = {key: (args.get(key) or "").strip() for key in
              ("q", "company", "role", "status", "date_from", "date_to", "ats_min", "preset")}
    if any(len(result[key]) > 200 for key in ("q", "company", "role")):
        raise ValueError("Search filters must be at most 200 characters")
    result["sort"] = args.get("sort", "newest")
    result["multi_version"] = args.get("multi_version", "false")
    if result["sort"] not in ("newest", "oldest", "ats"):
        raise ValueError("Invalid sort order")
    if result["status"] and result["status"] not in STATUSES:
        raise ValueError("Invalid application status")
    if result["preset"] and result["preset"] not in PRESETS:
        raise ValueError("Invalid history preset")
    if result["multi_version"] not in ("true", "false"):
        raise ValueError("Invalid multi-version filter")
    for key in ("date_from", "date_to"):
        if result[key]:
            _date(result[key])
    if result["date_from"] and result["date_to"] and result["date_from"] > result["date_to"]:
        raise ValueError("Start date must not be after end date")
    if result["ats_min"]:
        value = int(result["ats_min"])
        if not 0 <= value <= 100:
            raise ValueError("Minimum score must be between 0 and 100")
        result["ats_min"] = str(value)
    return result


def _preset(key, now):
    if key in ("week", "month"):
        return {"created_at": {"$gte": now - timedelta(days=7 if key == "week" else 30)}}
    if key == "highAts":
        return {"ats_scores.overall": {"$type": "number", "$gte": 80}}
    if key == "needsWork":
        return {"ats_scores.overall": {"$type": "number", "$lt": 60}}
    if key == "multiVersion":
        return {"versions.1": {"$exists": True}}
    if key in ("offers", "interviewing"):
        return {"application.status": "offer" if key == "offers" else key}
    if key == "pending":
        due = now + timedelta(days=3)
        # Older rows use date-only strings; newer rows may use BSON datetimes.
        return {"$or": [
            {"application.next_action_date": {"$type": "date", "$lte": due}},
            {"application.next_action_date": {"$type": "string", "$gte": "0001-01-01",
                                               "$lte": due.isoformat()}},
        ]}
    return {}


def _query(owner, options, now):
    clauses = [{"user_email": owner}]
    for key, fields in (("q", ("jd_analysis.job_title", "jd_analysis.company", "jd_analysis.required_skills")),
                        ("company", ("jd_analysis.company",)), ("role", ("jd_analysis.job_title",))):
        if options[key]:
            # User input is literal text, never a database expression or regex.
            clauses.append({"$or": [{field: {"$regex": re.escape(options[key]), "$options": "i"}}
                                     for field in fields]})
    if options["status"] == "draft":
        clauses.append({"application.status": {"$in": ["draft", None, ""]}})
    elif options["status"]:
        clauses.append({"application.status": options["status"]})
    dates = {}
    if options["date_from"]:
        dates["$gte"] = _date(options["date_from"])
    if options["date_to"]:
        dates["$lt"] = _date(options["date_to"]) + timedelta(days=1)
    if dates:
        clauses.append({"created_at": dates})
    if options["ats_min"]:
        clauses.append({"ats_scores.overall": {"$type": "number", "$gte": int(options["ats_min"])}})
    if options["multi_version"] == "true":
        clauses.append({"versions.1": {"$exists": True}})
    if options["preset"]:
        clauses.append(_preset(options["preset"], now))
    return {"$and": clauses}


def _after(cursor, sort):
    direction = "$gt" if sort == "oldest" else "$lt"
    created = datetime.fromisoformat(cursor["created"]) if cursor["created"] else None
    parts = [{"created_at": created, "record_id": {direction: cursor["id"]}}]
    if created is not None:
        parts.append({"created_at": {direction: created}})
        if direction == "$lt":
            parts.append({"created_at": None})
    elif direction == "$gt":
        parts.append({"created_at": {"$ne": None}})
    date_tail = {"$or": parts}
    if sort == "ats":
        return {"$or": [
            {"$expr": {"$lt": [SCORE, cursor["score"]]}},
            {"$and": [{"$expr": {"$eq": [SCORE, cursor["score"]]}}, date_tail]},
        ]}
    return date_tail


def _summary(collection, owner, now):
    facets = {key: [{"$match": _preset(key, now)}, {"$count": "count"}] for key in PRESETS}
    facets["total"] = [{"$count": "count"}]
    rows = list(collection.aggregate([
        {"$match": {"user_email": owner}},
        {"$project": {"created_at": 1, "ats_scores.overall": 1, "application.status": 1,
                      "application.next_action_date": 1, "versions.version_id": 1}},
        {"$facet": facets},
    ], maxTimeMS=10000))
    values = rows[0] if rows else {}
    counts = {key: (values.get(key) or [{}])[0].get("count", 0) for key in (*PRESETS, "total")}
    return {"total": counts.pop("total"), "presets": counts}


def list_history(collection, owner, args):
    options = _options(args)
    size = int(args.get("page_size", 50))
    if not 1 <= size <= 100:
        raise ValueError("Page size must be between 1 and 100")
    signer = URLSafeSerializer(current_app.config["JWT_SECRET_KEY"], salt="tailoring-history-v1")
    fingerprint = hashlib.sha256(json.dumps([owner, options], sort_keys=True).encode()).hexdigest()
    cursor = None
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    if args.get("cursor"):
        try:
            if len(args["cursor"]) > 2048:
                raise ValueError()
            cursor = signer.loads(args["cursor"])
            if cursor["filter"] != fingerprint:
                raise ValueError()
            now = datetime.fromisoformat(cursor["as_of"])
        except (BadSignature, ValueError, KeyError, TypeError):
            raise ValueError("Invalid history cursor; refresh the list") from None
    query = _query(owner, options, now)
    total = collection.count_documents(query, maxTimeMS=10000)
    page_query = {"$and": [query, _after(cursor, options["sort"])]} if cursor else query
    direction = 1 if options["sort"] == "oldest" else -1
    order = {"_history_score": -1} if options["sort"] == "ats" else {}
    order.update({"created_at": direction, "record_id": direction})
    # Date pages can use the owner/date/id index. Score sorting first projects
    # only metadata so large reports never enter the sort working set.
    stages = ([{"$project": PROJECTION}, {"$sort": order}, {"$limit": size + 1}]
              if options["sort"] == "ats" else
              [{"$sort": order}, {"$limit": size + 1}, {"$project": PROJECTION}])
    rows = collection.aggregate([{"$match": page_query}, *stages], allowDiskUse=True, maxTimeMS=10000)
    records = []
    last = None
    has_more = False
    byte_count = 1024
    for row in rows:
        if len(records) == size:
            has_more = True
            break
        position = {"created": row["created_at"].isoformat() if row.get("created_at") else None,
                    "score": row.pop("_history_score"), "id": row["record_id"]}
        serialized = serialize_record(row)
        row_bytes = len(current_app.json.dumps(serialized).encode("utf-8")) + 1
        if byte_count + row_bytes > 2 * 1024 * 1024:
            if not records:
                raise ValueError("A history summary is too large to load")
            has_more = True
            break
        records.append(serialized)
        byte_count += row_bytes
        last = position
    next_cursor = signer.dumps({**last, "filter": fingerprint, "as_of": now.isoformat()}) if has_more else None
    result = {"records": records, "total": total, "next_cursor": next_cursor}
    if not cursor:
        result["summary"] = _summary(collection, owner, now)
    return result
