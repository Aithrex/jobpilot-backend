from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from services.gemini_client import GEMINI_FLASH, GEMINI_PRO, gemini_json
from utils.db_connect import DBConnect


def _db():
    return DBConnect().get_db()


def _tailoring_collection():
    return _db().tailoring_records


def _activity_collection():
    return _db().job_activity_log


def _network_collection():
    return _db().networking_contacts


def _offer_collection():
    return _db().offer_workspace


def _to_iso(value: Any) -> Optional[str]:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value if isinstance(value, str) else None


def _application_status(record: Dict[str, Any]) -> str:
    app = record.get("application") or {}
    return str(app.get("status") or "draft")


def _record_summary(record: Dict[str, Any]) -> Dict[str, Any]:
    jd = record.get("jd_analysis") or {}
    app = record.get("application") or {}
    return {
        "record_id": record.get("record_id"),
        "job_title": jd.get("job_title") or "Untitled role",
        "company": jd.get("company") or "Unknown company",
        "status": app.get("status") or "draft",
        "applied_at": _to_iso(app.get("applied_at")),
        "next_action_date": _to_iso(app.get("next_action_date")),
        "updated_at": _to_iso(app.get("updated_at") or record.get("updated_at") or record.get("created_at")),
    }


def _load_record(user_email: str, record_id: str) -> Optional[Dict[str, Any]]:
    return _tailoring_collection().find_one({"user_email": user_email, "record_id": record_id})


def record_activity(user_email: str, activity_type: str, metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if not activity_type:
        return {"ok": False, "error": "activity_type is required"}
    metadata = dict(metadata or {})
    # Enrich with role_family at write-time so the resume_profiler feedback
    # aggregator can attribute clicks/applies/skips to a domain without
    # re-classifying every event on read. Only adds when we can confidently
    # match the title to one of the known intent regexes.
    if activity_type in ("job_click", "job_applied", "job_saved", "job_skipped", "job_dismissed"):
        if "role_family" not in metadata:
            family = _classify_role_family(metadata.get("job_title") or "")
            if family:
                metadata["role_family"] = family
    doc = {
        "event_id": uuid.uuid4().hex[:12],
        "user_email": user_email,
        "type": activity_type,
        "metadata": metadata,
        "timestamp": datetime.now(timezone.utc),
    }
    _activity_collection().insert_one(doc)
    doc.pop("_id", None)
    doc["timestamp"] = _to_iso(doc["timestamp"])
    return {"ok": True, "event": doc}


def _classify_role_family(title: str) -> Optional[str]:
    """Classify a job title into one of the intent taxonomy keys, or None."""
    if not title:
        return None
    try:
        import re as _re
        from services.resume_profiler import INTENTS
    except Exception:
        return None
    title_lower = title.lower()
    for key, spec in INTENTS.items():
        for pattern in spec.get("title_terms", []):
            try:
                if _re.search(pattern, title_lower, _re.IGNORECASE):
                    return key
            except _re.error:
                continue
    return None


def get_stale_applications(user_email: str, stale_days: int = 5) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    stale_cutoff = now - timedelta(days=max(1, stale_days))
    rows: List[Dict[str, Any]] = []
    cursor = _tailoring_collection().find({"user_email": user_email})
    for record in cursor:
        if _application_status(record) != "applied":
            continue
        app = record.get("application") or {}
        anchor = app.get("updated_at") or app.get("applied_at") or record.get("updated_at") or record.get("created_at")
        if not isinstance(anchor, datetime):
            continue
        # Mongo timestamps are stored naive (datetime.utcnow); align with the tz-aware "now".
        if anchor.tzinfo is None:
            anchor = anchor.replace(tzinfo=timezone.utc)
        if anchor > stale_cutoff:
            continue

        # If the user dismissed the follow-up nudge after the last status touch,
        # don't keep nagging them. A subsequent status update (anchor moves
        # forward) re-surfaces the record, since dismissed_at stays put.
        dismissed_at = app.get("followup_dismissed_at")
        if isinstance(dismissed_at, datetime):
            if dismissed_at.tzinfo is None:
                dismissed_at = dismissed_at.replace(tzinfo=timezone.utc)
            if dismissed_at >= anchor:
                continue

        days_stale = max(1, (now - anchor).days)
        row = _record_summary(record)
        row["days_stale"] = days_stale
        rows.append(row)
    rows.sort(key=lambda x: x.get("days_stale", 0), reverse=True)
    return {"ok": True, "count": len(rows), "stale_applications": rows}


def dismiss_followup(user_email: str, record_id: str) -> Dict[str, Any]:
    """Mark this application's follow-up nudge as dismissed.

    The dismissal is anchored to "now" and the get_stale_applications query
    skips records where dismissed_at >= updated_at, so any subsequent status
    change naturally re-arms the nudge.
    """
    now = datetime.now(timezone.utc)
    res = _tailoring_collection().update_one(
        {"user_email": user_email, "record_id": record_id},
        {"$set": {"application.followup_dismissed_at": now}},
    )
    if not res.matched_count:
        return {"ok": False, "error": "Record not found"}
    return {"ok": True, "dismissed_at": now.isoformat()}


def generate_followup_message(user_email: str, record_id: str, channel: str = "email") -> Dict[str, Any]:
    record = _load_record(user_email, record_id)
    if not record:
        return {"ok": False, "error": "Application record not found"}
    summary = _record_summary(record)
    channel = channel if channel in ("email", "linkedin") else "email"
    prompt = (
        "You are a job search assistant. Draft a concise, high-response follow-up message.\n"
        f"Channel: {channel}\n"
        f"Role: {summary['job_title']}\n"
        f"Company: {summary['company']}\n"
        f"Status: {summary['status']}\n"
        f"Applied At: {summary['applied_at']}\n\n"
        "Output JSON with keys: subject, message, tone, next_step.\n"
        "Keep message under 130 words, specific and polite, avoid desperation."
    )
    schema = {"subject": str, "message": str, "tone": str, "next_step": str}
    result = gemini_json(prompt=prompt, model=GEMINI_FLASH, temperature=0.35, schema=schema)
    record_activity(
        user_email,
        "followup_generated",
        {"record_id": record_id, "channel": channel, "company": summary["company"]},
    )
    return {"ok": True, "record": summary, "channel": channel, "followup": result}


def get_funnel_analytics(user_email: str) -> Dict[str, Any]:
    counts: Dict[str, int] = {
        "draft": 0,
        "applied": 0,
        "interviewing": 0,
        "offer": 0,
        "rejected": 0,
        "ghosted": 0,
        "withdrawn": 0,
    }
    rows = list(_tailoring_collection().find({"user_email": user_email}))
    for row in rows:
        status = _application_status(row)
        if status not in counts:
            counts["draft"] += 1
        else:
            counts[status] += 1

    applied_base = max(1, counts["applied"] + counts["interviewing"] + counts["offer"] + counts["rejected"] + counts["ghosted"])
    interview_base = max(1, counts["interviewing"] + counts["offer"] + counts["rejected"])
    conversions = {
        "applied_to_interview_pct": round(((counts["interviewing"] + counts["offer"]) / applied_base) * 100, 1),
        "interview_to_offer_pct": round((counts["offer"] / interview_base) * 100, 1),
        "rejection_rate_pct": round(((counts["rejected"] + counts["ghosted"]) / applied_base) * 100, 1),
    }

    insight_prompt = (
        "Given this job application funnel data, produce a short actionable insight for a job seeker.\n"
        f"Counts: {counts}\nConversions: {conversions}\n"
        "Return JSON with fields: headline, insight, action_items (array of 3 short bullets)."
    )
    schema = {"headline": str, "insight": str, "action_items": [str]}
    insight = gemini_json(prompt=insight_prompt, model=GEMINI_FLASH, temperature=0.3, schema=schema)
    return {"ok": True, "counts": counts, "conversions": conversions, "insight": insight}


def analyze_rejection_patterns(user_email: str) -> Dict[str, Any]:
    rejected = list(
        _tailoring_collection().find(
            {"user_email": user_email, "application.status": {"$in": ["rejected", "ghosted"]}},
            {"jd_analysis": 1, "application": 1, "record_id": 1, "created_at": 1},
        )
    )
    if not rejected:
        return {"ok": True, "total": 0, "patterns": [], "suggestions": []}

    slim = []
    for row in rejected[:80]:
        jd = row.get("jd_analysis") or {}
        app = row.get("application") or {}
        slim.append(
            {
                "record_id": row.get("record_id"),
                "job_title": jd.get("job_title"),
                "company": jd.get("company"),
                "industry": jd.get("industry"),
                "experience_years": jd.get("experience_years"),
                "status": app.get("status"),
                "updated_at": _to_iso(app.get("updated_at") or row.get("created_at")),
            }
        )

    prompt = (
        "Analyze these rejected/ghosted applications and identify practical patterns.\n"
        f"Data: {slim}\n"
        "Return JSON with keys: patterns (array of {theme, evidence}), suggestions (array of 4 specific actions)."
    )
    schema = {"patterns": [{"theme": str, "evidence": str}], "suggestions": [str]}
    result = gemini_json(prompt=prompt, model=GEMINI_PRO, temperature=0.35, schema=schema)
    return {"ok": True, "total": len(rejected), **result}


def reframe_rejection(user_email: str, record_id: str) -> Dict[str, Any]:
    record = _load_record(user_email, record_id)
    if not record:
        return {"ok": False, "error": "Application record not found"}
    summary = _record_summary(record)
    prompt = (
        "You are an empathetic but practical career coach.\n"
        f"Application context: {summary}\n"
        "Return JSON with keys: acknowledgement, what_went_right, next_actions (3 bullets), silver_lining."
    )
    schema = {
        "acknowledgement": str,
        "what_went_right": str,
        "next_actions": [str],
        "silver_lining": str,
    }
    result = gemini_json(prompt=prompt, model=GEMINI_PRO, temperature=0.5, schema=schema)
    record_activity(user_email, "rejection_reframed", {"record_id": record_id, "company": summary["company"]})
    return {"ok": True, "record": summary, "reframe": result}


def get_weekly_digest(user_email: str) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    week_start = now - timedelta(days=7)
    previous_start = now - timedelta(days=14)
    previous_end = week_start
    col = _activity_collection()
    this_week = list(col.find({"user_email": user_email, "timestamp": {"$gte": week_start, "$lte": now}}))
    last_week = list(col.find({"user_email": user_email, "timestamp": {"$gte": previous_start, "$lt": previous_end}}))
    stats = {
        "events_this_week": len(this_week),
        "events_last_week": len(last_week),
    }
    prompt = (
        "Create a motivating weekly digest for a job seeker based on activity data.\n"
        f"Stats: {stats}\n"
        "Return JSON with: headline, summary, wins (array), focus_for_next_week (array of 3)."
    )
    schema = {"headline": str, "summary": str, "wins": [str], "focus_for_next_week": [str]}
    digest = gemini_json(prompt=prompt, model=GEMINI_FLASH, temperature=0.45, schema=schema)
    return {"ok": True, "stats": stats, "digest": digest}


def get_momentum_data(user_email: str) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    events = list(_activity_collection().find({"user_email": user_email}).sort("timestamp", 1))
    unique_days = sorted({e["timestamp"].date().isoformat() for e in events if isinstance(e.get("timestamp"), datetime)})

    streak = 0
    day_ptr = now.date()
    day_set = set(unique_days)
    while day_ptr.isoformat() in day_set:
        streak += 1
        day_ptr = day_ptr - timedelta(days=1)

    this_week = 0
    last_week = 0
    for e in events:
        ts = e.get("timestamp")
        if not isinstance(ts, datetime):
            continue
        days_ago = (now.date() - ts.date()).days
        if 0 <= days_ago <= 6:
            this_week += 1
        elif 7 <= days_ago <= 13:
            last_week += 1

    records = list(_tailoring_collection().find({"user_email": user_email}))
    milestones: List[Dict[str, Any]] = []
    app_count = len(records)
    interview_count = sum(1 for r in records if _application_status(r) == "interviewing")
    offer_count = sum(1 for r in records if _application_status(r) == "offer")
    if app_count >= 10:
        milestones.append({"id": "apps10", "label": "10 applications sent", "earned": True})
    if interview_count >= 1:
        milestones.append({"id": "first_interview", "label": "First interview unlocked", "earned": True})
    if offer_count >= 1:
        milestones.append({"id": "first_offer", "label": "First offer unlocked", "earned": True})
    milestones.append({"id": "streak7", "label": "7-day activity streak", "earned": streak >= 7})

    momentum_score = min(100, max(0, (streak * 7) + min(40, this_week * 3) + min(30, interview_count * 10) + min(25, offer_count * 25)))
    trend = "up" if this_week >= last_week else "down"

    heatmap = []
    for i in range(29, -1, -1):
        d = now.date() - timedelta(days=i)
        key = d.isoformat()
        count = sum(1 for e in events if isinstance(e.get("timestamp"), datetime) and e["timestamp"].date().isoformat() == key)
        heatmap.append({"date": key, "count": count})

    return {
        "ok": True,
        "streak": streak,
        "momentum_score": momentum_score,
        "trend": trend,
        "this_week": this_week,
        "last_week": last_week,
        "milestones": milestones,
        "heatmap": heatmap,
    }


def add_contact(user_email: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    doc = {
        "contact_id": uuid.uuid4().hex[:12],
        "user_email": user_email,
        "name": payload.get("name") or "",
        "company": payload.get("company") or "",
        "role": payload.get("role") or "",
        "platform": payload.get("platform") or "linkedin",
        "relationship_strength": payload.get("relationship_strength") or "cold",
        "notes": payload.get("notes") or "",
        "last_contacted": payload.get("last_contacted") or None,
        "referral_status": payload.get("referral_status") or "none",
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }
    _network_collection().insert_one(doc)
    doc.pop("_id", None)
    doc["created_at"] = _to_iso(doc["created_at"])
    doc["updated_at"] = _to_iso(doc["updated_at"])
    return {"ok": True, "contact": doc}


def list_contacts(user_email: str) -> Dict[str, Any]:
    rows = list(_network_collection().find({"user_email": user_email}).sort("updated_at", -1))
    contacts = []
    for row in rows:
        row.pop("_id", None)
        row["created_at"] = _to_iso(row.get("created_at"))
        row["updated_at"] = _to_iso(row.get("updated_at"))
        contacts.append(row)
    return {"ok": True, "contacts": contacts}


def update_contact(user_email: str, contact_id: str, patch: Dict[str, Any]) -> Dict[str, Any]:
    allowed = {
        "name",
        "company",
        "role",
        "platform",
        "relationship_strength",
        "notes",
        "last_contacted",
        "referral_status",
    }
    update = {k: v for k, v in patch.items() if k in allowed}
    if not update:
        return {"ok": False, "error": "No valid fields to update"}
    update["updated_at"] = datetime.now(timezone.utc)
    _network_collection().update_one(
        {"user_email": user_email, "contact_id": contact_id},
        {"$set": update},
    )
    result = _network_collection().find_one({"user_email": user_email, "contact_id": contact_id})
    if not result:
        return {"ok": False, "error": "Contact not found"}
    result.pop("_id", None)
    result["created_at"] = _to_iso(result.get("created_at"))
    result["updated_at"] = _to_iso(result.get("updated_at"))
    return {"ok": True, "contact": result}


def delete_contact(user_email: str, contact_id: str) -> Dict[str, Any]:
    out = _network_collection().delete_one({"user_email": user_email, "contact_id": contact_id})
    return {"ok": out.deleted_count > 0}


def generate_intro_message(user_email: str, contact_id: str) -> Dict[str, Any]:
    contact = _network_collection().find_one({"user_email": user_email, "contact_id": contact_id})
    if not contact:
        return {"ok": False, "error": "Contact not found"}
    prompt = (
        "Write a concise networking intro message to request career advice/referral.\n"
        f"Contact: {contact.get('name')} ({contact.get('role')} at {contact.get('company')})\n"
        f"Platform: {contact.get('platform')}\n"
        f"Relationship strength: {contact.get('relationship_strength')}\n"
        f"Notes: {contact.get('notes')}\n"
        "Return JSON with: message, subject, tone."
    )
    schema = {"message": str, "subject": str, "tone": str}
    out = gemini_json(prompt=prompt, model=GEMINI_PRO, temperature=0.45, schema=schema)
    return {"ok": True, "contact_id": contact_id, "intro": out}


def get_networking_insights(user_email: str) -> Dict[str, Any]:
    rows = list(_network_collection().find({"user_email": user_email}, {"name": 1, "company": 1, "role": 1, "relationship_strength": 1, "referral_status": 1}))
    if not rows:
        return {"ok": True, "insights": {"headline": "No network data yet", "analysis": "Add contacts to unlock insights.", "actions": []}}
    prompt = (
        "Analyze this networking contact list and provide practical job search advice.\n"
        f"Contacts: {rows}\n"
        "Return JSON with: headline, analysis, actions (array of 3 specific actions)."
    )
    schema = {"headline": str, "analysis": str, "actions": [str]}
    insights = gemini_json(prompt=prompt, model=GEMINI_FLASH, temperature=0.35, schema=schema)
    return {"ok": True, "insights": insights}


def add_offer(user_email: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    doc = {
        "offer_id": uuid.uuid4().hex[:12],
        "user_email": user_email,
        "company": payload.get("company") or "",
        "role": payload.get("role") or "",
        "base": float(payload.get("base") or 0),
        "equity": float(payload.get("equity") or 0),
        "bonus": float(payload.get("bonus") or 0),
        "benefits": payload.get("benefits") or "",
        "location": payload.get("location") or "",
        "remote": bool(payload.get("remote")),
        "start_date": payload.get("start_date") or None,
        "deadline": payload.get("deadline") or None,
        "notes": payload.get("notes") or "",
        "status": payload.get("status") or "active",
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }
    _offer_collection().insert_one(doc)
    doc.pop("_id", None)
    doc["created_at"] = _to_iso(doc["created_at"])
    doc["updated_at"] = _to_iso(doc["updated_at"])
    return {"ok": True, "offer": doc}


def list_offers(user_email: str) -> Dict[str, Any]:
    rows = list(_offer_collection().find({"user_email": user_email}).sort("updated_at", -1))
    offers = []
    for row in rows:
        row.pop("_id", None)
        row["created_at"] = _to_iso(row.get("created_at"))
        row["updated_at"] = _to_iso(row.get("updated_at"))
        offers.append(row)
    return {"ok": True, "offers": offers}


def update_offer(user_email: str, offer_id: str, patch: Dict[str, Any]) -> Dict[str, Any]:
    allowed = {
        "company",
        "role",
        "base",
        "equity",
        "bonus",
        "benefits",
        "location",
        "remote",
        "start_date",
        "deadline",
        "notes",
        "status",
    }
    update = {k: v for k, v in patch.items() if k in allowed}
    if "base" in update:
        update["base"] = float(update["base"] or 0)
    if "equity" in update:
        update["equity"] = float(update["equity"] or 0)
    if "bonus" in update:
        update["bonus"] = float(update["bonus"] or 0)
    update["updated_at"] = datetime.now(timezone.utc)
    _offer_collection().update_one(
        {"user_email": user_email, "offer_id": offer_id},
        {"$set": update},
    )
    result = _offer_collection().find_one({"user_email": user_email, "offer_id": offer_id})
    if not result:
        return {"ok": False, "error": "Offer not found"}
    result.pop("_id", None)
    result["created_at"] = _to_iso(result.get("created_at"))
    result["updated_at"] = _to_iso(result.get("updated_at"))
    return {"ok": True, "offer": result}


def delete_offer(user_email: str, offer_id: str) -> Dict[str, Any]:
    out = _offer_collection().delete_one({"user_email": user_email, "offer_id": offer_id})
    return {"ok": out.deleted_count > 0}


def compare_offers(user_email: str) -> Dict[str, Any]:
    offers = list(_offer_collection().find({"user_email": user_email}))
    if not offers:
        return {"ok": True, "comparison": {"headline": "No offers yet", "recommendation": "Add offers to compare.", "pros_cons": []}}
    stripped = []
    for o in offers:
        stripped.append(
            {
                "offer_id": o.get("offer_id"),
                "company": o.get("company"),
                "role": o.get("role"),
                "base": o.get("base", 0),
                "bonus": o.get("bonus", 0),
                "equity": o.get("equity", 0),
                "location": o.get("location"),
                "remote": o.get("remote"),
                "status": o.get("status"),
            }
        )
    prompt = (
        "Compare these job offers for a candidate and provide an objective recommendation.\n"
        f"Offers: {stripped}\n"
        "Return JSON with: headline, recommendation, pros_cons (array of {company, pros, cons}), total_comp_table (array of {company, total_comp})."
    )
    schema = {
        "headline": str,
        "recommendation": str,
        "pros_cons": [{"company": str, "pros": [str], "cons": [str]}],
        "total_comp_table": [{"company": str, "total_comp": float}],
    }
    comp = gemini_json(prompt=prompt, model=GEMINI_PRO, temperature=0.35, schema=schema)
    return {"ok": True, "offers": stripped, "comparison": comp}


def generate_negotiation_script(user_email: str, offer_id: str, ask: str) -> Dict[str, Any]:
    offer = _offer_collection().find_one({"user_email": user_email, "offer_id": offer_id})
    if not offer:
        return {"ok": False, "error": "Offer not found"}
    prompt = (
        "Generate a respectful but confident negotiation script.\n"
        f"Offer: company={offer.get('company')}, role={offer.get('role')}, base={offer.get('base')}, bonus={offer.get('bonus')}, equity={offer.get('equity')}\n"
        f"Candidate ask: {ask}\n"
        "Return JSON with: email_subject, email_body, talking_points (array), fallback_if_denied."
    )
    schema = {
        "email_subject": str,
        "email_body": str,
        "talking_points": [str],
        "fallback_if_denied": str,
    }
    script = gemini_json(prompt=prompt, model=GEMINI_PRO, temperature=0.4, schema=schema)
    return {"ok": True, "offer_id": offer_id, "script": script}
