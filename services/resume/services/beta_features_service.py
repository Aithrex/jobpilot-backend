"""
Beta features — small, focused utility services for the Beta Lab tab.

These are intentionally lightweight (single LLM call or pure-Python
analytics) so they can ship as "Beta — feedback wanted" without committing
to a heavy infrastructure stack. Each function is callable on its own and
returns a JSON-serializable dict.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# #8 Morning Agent — daily "what to do today" briefing
# ---------------------------------------------------------------------------

def build_morning_brief(user_email: str) -> Dict[str, Any]:
    """Synthesize a single-screen agenda from existing app state.

    Inputs (all already on the user's account):
      · stale applications (>5d silent) → "follow up X"
      · applied-today counter from tailoring records
      · saved-but-not-applied count
      · funnel snapshot (counts per stage)

    Output:
      { ok: bool, briefing: { headline, lines[], counts{}, generated_at } }

    Headline is an LLM-generated 1-sentence call-to-action. Lines are a
    deterministic punch list (no LLM risk) so the agenda is always
    actionable even if the LLM is rate-limited.
    """
    from services.job_intelligence_service import (
        get_stale_applications,
        get_funnel_analytics,
    )
    from utils.db_connect import DBConnect

    today = datetime.now(timezone.utc).date()
    db = DBConnect().get_db()

    stale = (get_stale_applications(user_email, stale_days=5) or {}).get(
        "stale_applications", []
    )
    funnel = (get_funnel_analytics(user_email) or {}).get("counts", {}) or {}
    counts: Dict[str, int] = {
        "stale": len(stale),
        "saved_not_applied": int(funnel.get("interested", 0)),
        "applied_total": int(funnel.get("applied", 0))
            + int(funnel.get("interview", 0))
            + int(funnel.get("offer", 0)),
        "interview_pipeline": int(funnel.get("interview", 0)),
        "offers": int(funnel.get("offer", 0)),
    }

    # Today's applied count — query tailoring records with applied_at today.
    try:
        start_of_day = datetime(today.year, today.month, today.day, tzinfo=timezone.utc)
        applied_today = db.tailoring_records.count_documents({
            "user_email": user_email,
            "application.status": {"$in": ["applied", "interview", "offer"]},
            "application.applied_at": {"$gte": start_of_day},
        })
    except Exception:
        applied_today = 0
    counts["applied_today"] = applied_today

    # Deterministic lines — always actionable, never empty.
    lines: List[str] = []
    if counts["interview_pipeline"] > 0:
        lines.append(
            f"🎯 You have {counts['interview_pipeline']} interview"
            f"{'s' if counts['interview_pipeline'] != 1 else ''} in flight — "
            "prep behavioral + technical for each."
        )
    if counts["stale"] > 0:
        oldest_n = min(3, counts["stale"])
        sample = ", ".join(
            f"{s.get('company') or '?'}"
            for s in stale[:oldest_n] if s.get("company")
        )
        lines.append(
            f"✉️ Follow up on {counts['stale']} silent application"
            f"{'s' if counts['stale'] != 1 else ''}"
            + (f" ({sample})" if sample else "")
            + " — use the Needs Follow-Up panel."
        )
    if counts["saved_not_applied"] > 0:
        lines.append(
            f"📥 {counts['saved_not_applied']} saved role"
            f"{'s' if counts['saved_not_applied'] != 1 else ''} waiting for a tailored submission."
        )
    if counts["applied_today"] >= 5:
        lines.append(
            f"✅ You've applied to {counts['applied_today']} today — strong cadence. "
            "Pace yourself; quality matters past 5/day."
        )
    elif counts["applied_today"] > 0:
        lines.append(
            f"💪 {counts['applied_today']} app{'s' if counts['applied_today'] != 1 else ''} "
            "in already today — keep pushing toward your daily goal."
        )
    else:
        lines.append(
            "🌅 No applications submitted yet today. Pick the top Tier 1 from "
            "the daily pipeline and ship one before noon."
        )
    if counts["offers"] > 0:
        lines.append(
            f"🎉 {counts['offers']} active offer"
            f"{'s' if counts['offers'] != 1 else ''} on your board — "
            "compare carefully and review the Visa Timeline tab for start-date implications."
        )

    # LLM-generated headline (Flash, single field) with deterministic fallback
    # so the briefing is always populated even when LLM is rate-limited.
    headline = ""
    try:
        from services.gemini_client import gemini_json, GEMINI_FLASH
        prompt = (
            "You are a calm, sharp career coach speaking to an international "
            "graduate student grinding through a daily job search. Write ONE "
            "short, specific call-to-action (≤22 words, no emojis, no markdown) "
            "for the day given this dashboard:\n\n"
            f"  · applications today: {counts['applied_today']}\n"
            f"  · stale follow-ups owed: {counts['stale']}\n"
            f"  · interviews in flight: {counts['interview_pipeline']}\n"
            f"  · saved-not-applied: {counts['saved_not_applied']}\n"
            f"  · offers: {counts['offers']}\n\n"
            'Return JSON: { "headline": "..." }. Pick the single thread that '
            "should dominate today and make it concrete (mention numbers)."
        )
        out = gemini_json(
            prompt=prompt, model=GEMINI_FLASH, temperature=0.5,
            schema={"headline": str}, max_tokens=120,
        )
        if isinstance(out, dict) and isinstance(out.get("headline"), str):
            headline = out["headline"].strip().split("\n")[0][:200]
    except Exception as e:
        logger.info("morning-brief headline LLM soft-fail: %s", e)

    if not headline:
        # Deterministic fallback — picks the most urgent thread.
        if counts["interview_pipeline"] > 0:
            headline = "Interviews are the priority — prep for the ones in flight."
        elif counts["stale"] > 0:
            headline = f"You owe {counts['stale']} follow-up{'s' if counts['stale'] != 1 else ''}. Send them first."
        elif counts["applied_today"] == 0:
            headline = "Start the day by shipping one Tier 1 application before noon."
        elif counts["saved_not_applied"] > 0:
            headline = f"Drain the {counts['saved_not_applied']} saved roles waiting on a tailored submission."
        else:
            headline = "Strong cadence — keep going, you're building momentum."

    return {
        "ok": True,
        "briefing": {
            "headline": headline,
            "lines": lines,
            "counts": counts,
            "generated_at": datetime.utcnow().isoformat() + "Z",
        },
    }


# ---------------------------------------------------------------------------
# #12 JD reverse-search — find more roles like the one I'm interested in
# ---------------------------------------------------------------------------

def find_similar_roles(
    user_email: str,
    seed_jd_text: str,
    limit: int = 10,
) -> Dict[str, Any]:
    """Given a seed JD, return saved/pipeline rows with similar skill signatures.

    Lightweight keyword-vector match (no embeddings dependency):
      1. Extract top skill tokens from the seed JD via the LLM (FLASH tier).
      2. Pull the user's saved_jobs and most-recent pipeline_results from DB.
      3. Score each candidate by token overlap with the seed signature.
      4. Return top-N sorted by overlap desc.
    """
    from utils.db_connect import DBConnect
    db = DBConnect().get_db()

    seed_jd_text = (seed_jd_text or "").strip()
    if not seed_jd_text or len(seed_jd_text) < 80:
        return {"ok": False, "error": "Seed JD text is too short — paste a real posting."}

    # Step 1: extract skill signature
    seed_tokens: List[str] = []
    try:
        from services.gemini_client import gemini_json, GEMINI_FLASH
        prompt = (
            "Extract the top 15 technical skill / tool / concept tokens from this "
            "job description, focused on what a candidate would put on a resume. "
            "Return ONLY a JSON object: { \"tokens\": [str, str, ...] }. Lowercase, "
            "1-2 words each, no soft skills, no company names, no locations.\n\n"
            f"JD:\n{seed_jd_text[:6000]}"
        )
        result = gemini_json(
            prompt=prompt,
            model=GEMINI_FLASH,
            temperature=0.2,
            schema={"tokens": [str]},
            max_tokens=400,
        )
        seed_tokens = [
            str(t).strip().lower() for t in (result or {}).get("tokens", []) if t
        ][:15]
    except Exception as e:
        logger.warning("similar-roles LLM soft-fail: %s", e)
    if not seed_tokens:
        # Fallback: regex tokens from JD itself.
        import re
        seed_tokens = list(set(re.findall(r"[a-zA-Z][a-zA-Z+#./-]{2,}", seed_jd_text.lower())))[:15]

    seed_set = set(seed_tokens)

    # Step 2 + 3: pull candidates and score
    def _score(text: str) -> int:
        t = (text or "").lower()
        return sum(1 for tok in seed_set if tok and tok in t)

    candidates: List[Dict[str, Any]] = []
    try:
        for sj in db.saved_jobs.find({"user_email": user_email}).limit(200):
            blob = " ".join([
                str(sj.get("title", "")),
                str(sj.get("description", "")),
                str(sj.get("company", "")),
            ])
            sc = _score(blob)
            if sc <= 1:
                continue
            candidates.append({
                "source": "saved",
                "title": sj.get("title"),
                "company": sj.get("company"),
                "location": sj.get("location"),
                "url": sj.get("apply_link") or sj.get("url"),
                "score_overlap": sc,
                "status": sj.get("status"),
            })
    except Exception as e:
        logger.info("similar-roles saved_jobs read failed: %s", e)

    candidates.sort(key=lambda r: -int(r.get("score_overlap") or 0))
    return {
        "ok": True,
        "seed_tokens": seed_tokens,
        "results": candidates[:limit],
        "total_scanned": len(candidates),
    }


# ---------------------------------------------------------------------------
# #6 A/B Telemetry stats — current variant split + response rates
# ---------------------------------------------------------------------------

def ab_telemetry(user_email: str) -> Dict[str, Any]:
    """Aggregate response rates by variant tag across the user's tailoring history.

    Schema expectation: tailoring_records.application has `variant` (str or None).
    If no records have a variant set, returns a "framework ready" empty state so
    the UI can prompt the user to start tagging.
    """
    from utils.db_connect import DBConnect
    db = DBConnect().get_db()

    pipeline = [
        {"$match": {"user_email": user_email}},
        {"$group": {
            "_id": {"$ifNull": ["$application.variant", "untagged"]},
            "total": {"$sum": 1},
            "interviewed": {
                "$sum": {
                    "$cond": [
                        {"$in": ["$application.status", ["interview", "offer"]]},
                        1,
                        0,
                    ]
                }
            },
            "offers": {
                "$sum": {
                    "$cond": [{"$eq": ["$application.status", "offer"]}, 1, 0]
                }
            },
        }},
        {"$sort": {"total": -1}},
    ]
    try:
        rows = list(db.tailoring_records.aggregate(pipeline))
    except Exception as e:
        logger.warning("ab_telemetry aggregate failed: %s", e)
        rows = []

    variants: List[Dict[str, Any]] = []
    total_apps = 0
    has_tagged = False
    for r in rows:
        tag = r.get("_id") or "untagged"
        if tag != "untagged":
            has_tagged = True
        total = int(r.get("total") or 0)
        interviewed = int(r.get("interviewed") or 0)
        offers = int(r.get("offers") or 0)
        variants.append({
            "variant": tag,
            "total": total,
            "interviewed": interviewed,
            "offers": offers,
            "callback_rate": round((interviewed / total * 100) if total else 0.0, 1),
        })
        total_apps += total

    # LLM insight — only worth spending a call when there are ≥2 tagged
    # variants with enough sample size to compare. Soft-fail on rate-limit.
    insight = ""
    if has_tagged:
        tagged = [v for v in variants if v["variant"] != "untagged" and v["total"] >= 3]
        if len(tagged) >= 2:
            try:
                from services.gemini_client import gemini_json, GEMINI_FLASH
                prompt = (
                    "You are a senior career coach analyzing this student's "
                    "resume A/B test results. In 2-3 short sentences, give a "
                    "specific, data-grounded recommendation. No fluff. Mention "
                    "the winning variant by name and the actual callback rate.\n\n"
                    f"Variants: {tagged}\n\n"
                    'Return JSON: { "insight": "..." }.'
                )
                out = gemini_json(
                    prompt=prompt, model=GEMINI_FLASH, temperature=0.4,
                    schema={"insight": str}, max_tokens=200,
                )
                if isinstance(out, dict) and isinstance(out.get("insight"), str):
                    insight = out["insight"].strip()[:500]
            except Exception as e:
                logger.info("ab_telemetry insight soft-fail: %s", e)

    return {
        "ok": True,
        "has_tagged_data": has_tagged,
        "total_apps": total_apps,
        "variants": variants,
        "insight": insight,
    }
