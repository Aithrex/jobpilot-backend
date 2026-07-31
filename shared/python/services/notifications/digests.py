"""Digest payload builders (Compass §2.5) — PURE, no side effects, no sends.

    build_daily_digest(user_email)   -> payload   # morning: top matches
    build_evening_summary(user_email)-> payload   # evening: the day in review

Both return a channel-agnostic payload with pre-rendered per-channel blocks
(``email`` / ``sms`` / ``push`` / ``whatsapp``) so ``channels.py`` stays thin.
The scheduler calls a builder, then ``service.send(user_email, event, payload)``.

Source data is assembled from EXISTING services — nothing here scrapes or spends:
    * matches      — the latest COMPLETED ``daily_pipeline`` job cached in
                     ``resume_jobs`` (run_pipeline is paid/async and is NEVER
                     invoked inline here)
    * sponsorship  — ``sponsorship_service.bulk_signals`` (one Mongo query)
    * applications — ``tailoring_records`` (updated-today)
    * learning     — ``recommendations_service`` (resume-local, guarded)
    * OPT clock    — ``visa_timeline_service.compute_opt_clock`` on ``visa_profiles``

Every external read is lazy + soft-failing so a builder degrades to a smaller
payload rather than raising (mirrors ``recommendations_service._safe``).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Callable, Dict, List, Optional

from utils.db_connect import DBConnect

logger = logging.getLogger(__name__)

DAILY_DIGEST_EVENT = "daily_digest"
EVENING_SUMMARY_EVENT = "evening_summary"
TOP_N_MATCHES = 5


def _db():
    return DBConnect().get_db()


def _safe(fn: Callable, default: Any) -> Any:
    try:
        return fn()
    except Exception as e:
        logger.warning("[digest] source read failed: %s", e)
        return default


def _frontend_base() -> str:
    try:
        from utils.config import AppUrlsConfig
        return AppUrlsConfig.FRONTEND_URL
    except Exception:
        return "http://localhost:5173"


def _app_url(path: str) -> str:
    return f"{_frontend_base().rstrip('/')}/{path.lstrip('/')}"


def _user_tz_now(user_email: str) -> datetime:
    """'Now' in the user's configured timezone (for correct day boundaries)."""
    tz_name = "UTC"
    try:
        from services.notifications import service
        tz_name = (service.get_prefs(user_email).get("quiet_hours") or {}).get("tz") or "UTC"
    except Exception:
        pass
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(tz_name))
    except Exception:
        return datetime.now(timezone.utc)


# ===========================================================================
# Morning digest — top N matches with sponsorship badge + deep links
# ===========================================================================
def build_daily_digest(user_email: str, top_n: int = TOP_N_MATCHES) -> Dict[str, Any]:
    matches = _safe(lambda: _top_matches(user_email, top_n), [])
    matches = _attach_sponsorship(matches)
    hackathons = _safe(lambda: _new_hackathons(), [])

    count = len(matches)
    companies = ", ".join(m["company"] for m in matches if m.get("company"))[:120]
    cta_url = _app_url("/?tab=matches")  # FE route: confirm with frontend-engineer
    title = f"{count} fresh job match{'es' if count != 1 else ''} for you" if count else "Your daily job scan is ready"
    summary_line = (
        f"{count} new match{'es' if count != 1 else ''} today"
        + (f": {companies}" if companies else "")
    )
    if hackathons:
        hn = len(hackathons)
        summary_line += f" · {hn} new hackathon{'s' if hn != 1 else ''} this week"

    return {
        "event": DAILY_DIGEST_EVENT,
        "user_email": user_email,
        "title": title,
        "summary_line": summary_line,
        "cta_url": cta_url,
        "match_count": count,
        "matches": matches,
        "hackathon_count": len(hackathons),
        "hackathons": hackathons,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        # pre-rendered per-channel blocks
        "email": {
            "subject": title,
            "html": _daily_email_html(title, matches, cta_url, hackathons),
            "text": _daily_email_text(title, matches, cta_url, hackathons),
        },
        "sms": {"body": _clip(summary_line + f" — {cta_url}", 300)},
        "push": {"title": title, "body": summary_line, "url": cta_url},
        "whatsapp": {"template": "daily_digest", "language": "en_US",
                     "params": [str(count), companies or "new roles"]},
    }


def _top_matches(user_email: str, top_n: int) -> List[Dict[str, Any]]:
    """Read the latest COMPLETED daily_pipeline result from resume_jobs and take
    the strongest 'apply now' rows. Never re-runs the (paid) pipeline."""
    job = _db().resume_jobs.find_one(
        {"user_email": user_email, "job_type": "daily_pipeline", "status": "completed"},
        sort=[("completed_at", -1)],
    )
    result = (job or {}).get("result") or {}
    rows = result.get("apply_now") or []
    out: List[Dict[str, Any]] = []
    for r in rows[: max(0, top_n)]:
        out.append({
            "title": str(r.get("title") or "").strip(),
            "company": str(r.get("company") or "").strip(),
            "location": str(r.get("location") or "").strip(),
            "tier": r.get("tier"),
            "posted": r.get("posted"),
            "source": r.get("source"),
            "apply_url": str(r.get("url") or "").strip(),   # deep link to the posting
            "sponsorship": None,
        })
    return out


def _attach_sponsorship(matches: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not matches:
        return matches
    def _run():
        from services import sponsorship_service
        signals = sponsorship_service.bulk_signals([m.get("company") for m in matches])
        for m in matches:
            sig = signals.get(m.get("company") or "")
            if sig:
                m["sponsorship"] = {"band": sig.get("band"), "headline": sig.get("headline"),
                                    "score": sig.get("score")}
        return matches
    return _safe(_run, matches)


def _has_prize(h: Dict[str, Any]) -> bool:
    """True only for a real cash prize — suppresses Devpost's '$0'/'$' placeholders."""
    p = str(h.get("prize_pool") or "").strip()
    if not p:
        return False
    digits = "".join(ch for ch in p if ch.isdigit())
    return bool(digits) and int(digits) > 0


def _new_hackathons(days: int = 7, limit: int = 3) -> List[Dict[str, Any]]:
    """Vetted hackathons first added to the catalog in the last `days`, strongest
    first. Global (not per-user) — a light 'what's new' nudge in the digest."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    cur = (
        _db().events.find(
            {
                "type": "hackathon",
                "status": "active",
                "legitimacy_band": {"$in": ["verified", "credible"]},
                "first_seen": {"$gte": since},
            },
            {"_id": 0, "title": 1, "org": 1, "url": 1, "start_date": 1, "prize_pool": 1},
        )
        .sort("legitimacy_score", -1)
        .limit(max(0, limit))
    )
    out: List[Dict[str, Any]] = []
    for e in cur:
        out.append({
            "title": str(e.get("title") or "").strip(),
            "org": str(e.get("org") or "").strip(),
            "url": str(e.get("url") or "").strip(),
            "start_date": str(e.get("start_date") or "").strip(),
            "prize_pool": e.get("prize_pool"),
        })
    return out


def _daily_email_html(title: str, matches: List[Dict[str, Any]], cta_url: str,
                      hackathons: Optional[List[Dict[str, Any]]] = None) -> str:
    rows = ""
    for m in matches:
        badge = ""
        sp = m.get("sponsorship") or {}
        if sp.get("band"):
            badge = (f'<span style="font-size:12px;color:#4f46e5">· sponsorship: '
                     f'{sp.get("band")}</span>')
        link = m.get("apply_url") or cta_url
        rows += (
            '<li style="margin:0 0 10px;line-height:1.4">'
            f'<a href="{link}" style="color:#111;font-weight:600;text-decoration:none">'
            f'{m.get("title") or "Role"}</a> — {m.get("company") or ""} '
            f'<span style="color:#888">{m.get("location") or ""}</span> {badge}</li>'
        )
    if not rows:
        rows = '<li style="color:#666">No new matches today — your saved filters are still watching.</li>'

    hk_html = ""
    if hackathons:
        events_url = _app_url("/?tab=events")
        hk_items = ""
        for h in hackathons:
            prize = f' · {h.get("prize_pool")}' if _has_prize(h) else ""
            hlink = h.get("url") or events_url
            hk_items += (
                '<li style="margin:0 0 8px;line-height:1.4">'
                f'<a href="{hlink}" style="color:#111;font-weight:600;text-decoration:none">'
                f'{h.get("title") or "Hackathon"}</a> '
                f'<span style="color:#888">{h.get("org") or ""}{prize}</span></li>'
            )
        n = len(hackathons)
        hk_html = (
            f'<h3 style="margin:22px 0 8px">🏆 {n} new vetted hackathon{"s" if n != 1 else ""} this week</h3>'
            f'<ul style="padding-left:18px;margin:0 0 8px">{hk_items}</ul>'
            f'<p style="margin:0 0 20px"><a href="{events_url}" style="color:#4f46e5;'
            'font-weight:600;text-decoration:none">See all vetted events →</a></p>'
        )

    return (
        '<div style="font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;max-width:560px;margin:auto">'
        f'<h2 style="margin:0 0 12px">{title}</h2>'
        f'<ul style="padding-left:18px;margin:0 0 20px">{rows}</ul>'
        f'{hk_html}'
        f'<p><a href="{cta_url}" style="background:#4f46e5;color:#fff;padding:10px 18px;'
        'border-radius:8px;text-decoration:none;font-weight:600">Open your matches</a></p>'
        '<p style="color:#aaa;font-size:12px;margin-top:24px">You can change what you get or '
        'unsubscribe in Settings → Notifications.</p></div>'
    )


def _daily_email_text(title: str, matches: List[Dict[str, Any]], cta_url: str,
                      hackathons: Optional[List[Dict[str, Any]]] = None) -> str:
    lines = [title, ""]
    for m in matches:
        sp = (m.get("sponsorship") or {}).get("band")
        tail = f" [sponsorship: {sp}]" if sp else ""
        lines.append(f"- {m.get('title')} — {m.get('company')} ({m.get('location')}){tail}")
        if m.get("apply_url"):
            lines.append(f"  {m['apply_url']}")
    if not matches:
        lines.append("No new matches today — your saved filters are still watching.")
    if hackathons:
        n = len(hackathons)
        lines += ["", f"{n} new vetted hackathon{'s' if n != 1 else ''} this week:"]
        for h in hackathons:
            prize = f" — {h.get('prize_pool')}" if _has_prize(h) else ""
            lines.append(f"- {h.get('title')} ({h.get('org')}){prize}")
            if h.get("url"):
                lines.append(f"  {h['url']}")
    lines += ["", f"Open your matches: {cta_url}",
              "", "Change preferences or unsubscribe: Settings → Notifications."]
    return "\n".join(lines)


# ===========================================================================
# Evening summary — apps, status moves, tomorrow's task, OPT clock
# ===========================================================================
def build_evening_summary(user_email: str) -> Dict[str, Any]:
    activity = _safe(lambda: _today_application_activity(user_email), {"submitted": 0, "moved": []})
    learning = _safe(lambda: _tomorrow_learning_task(user_email), None)
    opt = _safe(lambda: _opt_clock(user_email), None)

    submitted = activity.get("submitted", 0)
    moved = activity.get("moved", [])
    cta_url = _app_url("/app/apply")  # J: canonical `/next` applications route

    bits = [f"{submitted} application{'s' if submitted != 1 else ''} submitted today"]
    if moved:
        bits.append(f"{len(moved)} status update{'s' if len(moved) != 1 else ''}")
    if learning:
        bits.append(f"tomorrow: {learning.get('title')}")
    if opt and opt.get("days_remaining") is not None:
        bits.append(f"OPT runway {opt['days_remaining']}d")
    summary_line = " · ".join(bits)
    title = "Your day on Aspirely"

    return {
        "event": EVENING_SUMMARY_EVENT,
        "user_email": user_email,
        "title": title,
        "summary_line": summary_line,
        "cta_url": cta_url,
        "applications_submitted": submitted,
        "status_moves": moved,
        "tomorrow_task": learning,
        "opt_clock": opt,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "email": {
            "subject": title,
            "html": _evening_email_html(title, submitted, moved, learning, opt, cta_url),
            "text": _evening_email_text(title, submitted, moved, learning, opt, cta_url),
        },
        "sms": {"body": _clip(summary_line + f" — {cta_url}", 300)},
        "push": {"title": title, "body": summary_line, "url": cta_url},
        "whatsapp": {"template": "evening_summary", "language": "en_US",
                     "params": [str(submitted), str(len(moved))]},
    }


def _today_application_activity(user_email: str) -> Dict[str, Any]:
    """Applications submitted today + status moves, from ``tailoring_records``.
    'Today' is the user's local day; comparison is naive-UTC-safe."""
    start_local = _user_tz_now(user_email).replace(hour=0, minute=0, second=0, microsecond=0)
    start_utc = start_local.astimezone(timezone.utc)
    submitted = 0
    moved: List[Dict[str, Any]] = []
    cur = _db().tailoring_records.find(
        {"user_email": user_email, "deleted": {"$ne": True}, "updated_at": {"$gte": start_utc}},
    ).limit(100)
    for r in cur:
        app = r.get("application") or {}
        status = (app.get("status") or "").lower()
        company = (r.get("jd_analysis") or {}).get("company") or "a role"
        if status == "applied":
            submitted += 1
        if status:
            moved.append({"company": company, "status": status})
    return {"submitted": submitted, "moved": moved[:10]}


def _tomorrow_learning_task(user_email: str) -> Optional[Dict[str, Any]]:
    """Next learning task. Prefer the recommendations feed (resume-local; guarded
    so this still works in Lambdas that don't carry it), else an active path."""
    def _from_recos():
        from services import recommendations_service  # resume-local
        for item in recommendations_service.get_recommendations(user_email, limit=6) or []:
            if item.get("type") == "learning":
                return {"title": item.get("title"), "cta": item.get("cta"),
                        "url": _app_url("/?tab=learning"), "source": "recommendations"}
        return None
    task = _safe(_from_recos, None)
    if task:
        return task

    def _from_paths():
        prog = {p["path_id"]: p for p in _db().learning_progress.find({"user_email": user_email})}
        for p in _db().learning_paths.find({"user_email": user_email, "status": "active"}).limit(5):
            done = len(set((prog.get(p["path_id"], {}) or {}).get("completed_step_ids", [])))
            total = len(p.get("modules", []))
            if total and done < total:
                return {"title": f"Continue “{p.get('title')}” ({done}/{total})",
                        "url": _app_url("/?tab=learning"), "source": "learning_paths"}
        return None
    return _safe(_from_paths, None)


def _opt_clock(user_email: str) -> Optional[Dict[str, Any]]:
    """Compact OPT clock from the visa profile (shared visa_timeline engine)."""
    from services.visa_timeline_service import normalize_profile, compute_opt_clock
    doc = _db().visa_profiles.find_one({"user_email": user_email}) or {}
    if not doc:
        return None
    clock = compute_opt_clock(normalize_profile(doc))
    if not clock.get("has_profile"):
        return None
    next_date = clock["key_dates"][0] if clock.get("key_dates") else None
    return {
        "days_remaining": clock.get("days_remaining"),
        "unemployment_days_limit": clock.get("unemployment_days_limit"),
        "status_band": clock.get("status_band"),
        "next_key_date": next_date,
    }


def _evening_email_html(title, submitted, moved, learning, opt, cta_url) -> str:
    parts = [f'<li>Applications submitted today: <b>{submitted}</b></li>']
    if moved:
        items = "".join(f"<li>{m.get('company')} → {m.get('status')}</li>" for m in moved)
        parts.append(f'<li>Status updates:<ul>{items}</ul></li>')
    if learning:
        parts.append(f'<li>Tomorrow: <b>{learning.get("title")}</b></li>')
    if opt and opt.get("days_remaining") is not None:
        nd = opt.get("next_key_date") or {}
        nd_txt = f" (next: {nd.get('label')} {nd.get('date')})" if nd else ""
        parts.append(f'<li>OPT unemployment runway: <b>{opt["days_remaining"]} days</b>{nd_txt}</li>')
    body = "".join(parts)
    return (
        '<div style="font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;max-width:560px;margin:auto">'
        f'<h2 style="margin:0 0 12px">{title}</h2>'
        f'<ul style="padding-left:18px;line-height:1.5">{body}</ul>'
        f'<p><a href="{cta_url}" style="background:#4f46e5;color:#fff;padding:10px 18px;'
        'border-radius:8px;text-decoration:none;font-weight:600">Review your applications</a></p>'
        '<p style="color:#aaa;font-size:12px;margin-top:24px">Manage these in Settings → Notifications.</p></div>'
    )


def _evening_email_text(title, submitted, moved, learning, opt, cta_url) -> str:
    lines = [title, "", f"Applications submitted today: {submitted}"]
    for m in moved:
        lines.append(f"- {m.get('company')} -> {m.get('status')}")
    if learning:
        lines.append(f"Tomorrow: {learning.get('title')}")
    if opt and opt.get("days_remaining") is not None:
        lines.append(f"OPT unemployment runway: {opt['days_remaining']} days")
    lines += ["", f"Review your applications: {cta_url}"]
    return "\n".join(lines)


def _clip(text: str, n: int) -> str:
    text = str(text or "")
    return text if len(text) <= n else text[: n - 1] + "…"
