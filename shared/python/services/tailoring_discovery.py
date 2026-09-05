"""Optional candidate-authored additions, scoped to a frozen source and role."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from services.tailoring_policy import content_hash, digest


def confirm(db, user_email: str, record_id: str, entity: str, text: str) -> str:
    record = db.tailoring_records.find_one({"record_id": record_id, "user_email": user_email})
    if not record:
        raise LookupError("Record not found")
    source = record.get("tailoring_source_snapshot")
    if not source:
        raise ValueError("This resume needs a new tailoring run before adding experience here.")
    if not isinstance(entity, str) or not re.fullmatch(r"(?:experience|projects)\.\d+", entity):
        raise ValueError("Select an existing job or project.")
    section, index = entity.split(".")
    if int(index) >= len(source.get(section) or []):
        raise ValueError("Selected job or project no longer exists.")
    if not isinstance(text, str) or not 10 <= len(text.strip()) <= 1000:
        raise ValueError("Describe your actual work in 10 to 1000 characters.")
    text = text.strip()
    key = digest([entity, text])[:24]
    query = {"user_email": user_email, "source_hash": content_hash(source)}
    # One bounded document per source; update one key, never overwrite siblings.
    col = db["tailoring_evidence"]
    existing = col.find_one(query) or {}
    if key not in (existing.get("facts") or {}) and len(existing.get("facts") or {}) >= 20:
        raise ValueError("Add further experience to your base resume and tailor again.")
    col.update_one(query, {"$set": {f"facts.{key}": {"entity": entity, "text": text,
        "confirmed_at": datetime.now(timezone.utc)}, "updated_at": datetime.now(timezone.utc)}}, upsert=True)
    return key
