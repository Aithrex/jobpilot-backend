"""Today Inbox — the unified feed of pending user decisions.

Powers the new Today dashboard hero ("N items need you today"). This is a
READ-ONLY aggregator: it stitches together, into ONE typed list, every distinct
"you need to decide something" surface the app already tracks in separate
collections, so the frontend can render the hero from a single call instead of
best-effort synthesizing it from four separate endpoints.

Sources aggregated (each guarded + error-isolated — a disabled feature, an
off flag, or a failing source contributes NOTHING and NEVER 500s the feed):

  1. "needs_input"      — `applications` ledger rows in `needs_input` status (or
                          autonomy `hold`): the one-tap review queue where an
                          auto-apply run stopped on a question only the user can
                          answer (application_ledger_service).
  2. "auto_submit"      — opt-in auto-submit approvals eligible to submit NOW
                          (auto_submit_service). Skipped cleanly when the master
                          AUTO_SUBMIT_ENABLED kill-switch is off.
  3. "inbox_suggestion" — pending Gmail status suggestions the user must confirm
                          or dismiss (gmail_service). Skipped when the Gmail flag
                          is off or the user hasn't connected an inbox.
  4. "did_you_apply"    — `tailoring_records` with
                          `application.batch_confirmation_pending`: the batch-tailor
                          "Did you apply?" prompt (resume_service writes the flag;
                          the Applications board clears it).

Every item is the SAME typed shape so the FE renders them uniformly:

    { id, kind, title, detail, deep_link, action_label, created_at }

Response: { count, items: [...] } — newest-first, capped. Nothing here calls an
LLM, spends quota, or mutates state; all queries are user_email-scoped (the
email comes from the verified JWT identity in the route). The per-source
collectors each swallow their own failures (log + yield []) so one broken
surface can never blank the whole inbox.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Total items returned across all sources — the hero is a glanceable digest, not
# a paginated list. Each source is also fetched with its own bound so no single
# noisy surface can crowd the others out before the final newest-first cut.
MAX_ITEMS = 50
_PER_SOURCE_LIMIT = 50

# In-app SPA paths (the FE resolves these relative to its router) — kept
# decoupled from any absolute-URL config so this shared service has no edge
# dependency. Auto-submit items prefer the concrete application page URL when
# the approval carries one.
#
# J (stale deep links): the migrated `/next` shell owns the applications surface
# (jobpilot-frontend App.tsx: `/app/apply` → index redirects to the
# `tracker` tab; Gmail-derived suggestions belong on the `inbox` tab). The legacy
# bare `/applications` no longer routes.
_LINK_APPLICATIONS = "/app/apply"
_LINK_APPLICATIONS_INBOX = "/app/apply/inbox"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _coerce_dt(value: Any) -> Optional[datetime]:
    """Normalize a datetime or ISO-8601 string to an aware UTC datetime for
    sorting. Returns None on anything unparseable (the item sorts oldest)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value:
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _iso(value: Any) -> Optional[str]:
    dt = _coerce_dt(value)
    return dt.isoformat() if dt else None


def _item(kind: str, ident: str, *, title: str, detail: str, deep_link: str,
          action_label: str, created_at: Any) -> Tuple[Dict[str, Any], datetime]:
    """Build one typed inbox item + its sort key (parsed created_at). `id` is
    namespaced by kind so ids are globally unique across the merged feed and the
    FE can key/dedupe on them safely."""
    dt = _coerce_dt(created_at)
    item = {
        "id": f"{kind}:{ident}",
        "kind": kind,
        "title": title,
        "detail": detail,
        "deep_link": deep_link,
        "action_label": action_label,
        "created_at": dt.isoformat() if dt else None,
    }
    # Oldest-possible sort key for items missing a timestamp so a good ts always
    # sorts ahead of a missing one under newest-first ordering.
    return item, (dt or datetime.min.replace(tzinfo=timezone.utc))


def _title(company: Any, role: Any, fallback: str) -> str:
    company = (str(company or "")).strip()
    role = (str(role or "")).strip()
    if company and role:
        return f"{company} — {role}"
    return company or role or fallback


# ---------------------------------------------------------------------------
# Source 1 — ledger rows awaiting one-tap input (needs_input status / hold)
# ---------------------------------------------------------------------------
def _collect_needs_input(user_email: str) -> List[Tuple[Dict[str, Any], datetime]]:
    """Applications ledger rows the user must answer. `needs_input` is the
    one-tap review status; autonomy `hold` rows also live there (a hold IS a
    one-tap review), so filtering on the `needs_input` status — served by the
    existing (user_email, status) index — captures both."""
    try:
        from services import application_ledger_service as als
        rows = als.list_applications(
            user_email, status=als.STATUS_NEEDS_INPUT, limit=_PER_SOURCE_LIMIT
        )
        out: List[Tuple[Dict[str, Any], datetime]] = []
        for r in rows:
            aid = r.get("application_id")
            if not aid:
                continue
            questions = r.get("needs_input") or []
            n = len(questions)
            first_q = ""
            if n and isinstance(questions[0], dict):
                first_q = (questions[0].get("question") or "").strip()
            if n > 1:
                detail = f"{n} questions need your answer to continue applying."
            elif first_q:
                detail = first_q
            else:
                detail = "This application needs your input to continue."
            out.append(_item(
                "needs_input", aid,
                title=_title(r.get("company"), r.get("title"), "Application needs input"),
                detail=detail,
                deep_link=_LINK_APPLICATIONS,
                action_label="Review & answer",
                created_at=r.get("created_at"),
            ))
        return out
    except Exception as e:  # pragma: no cover - defensive per-source isolation
        logger.warning("today_inbox: needs_input source failed for %s: %s", user_email, e)
        return []


# ---------------------------------------------------------------------------
# Source 2 — opt-in auto-submit approvals eligible to submit now
# ---------------------------------------------------------------------------
def _collect_auto_submit(user_email: str) -> List[Tuple[Dict[str, Any], datetime]]:
    """Eligible (past-undo, unexpired) auto-submit approvals awaiting the final
    tap. Cleanly contributes nothing when the master kill-switch is off."""
    try:
        from services import auto_submit_service as ass
        if not ass.auto_submit_enabled():
            return []
        pending = (ass.list_pending_eligible(user_email) or {}).get("pending") or []
        out: List[Tuple[Dict[str, Any], datetime]] = []
        for a in pending[:_PER_SOURCE_LIMIT]:
            approval_id = a.get("approval_id")
            if not approval_id:
                continue
            out.append(_item(
                "auto_submit", approval_id,
                title="Application ready to submit",
                detail="Reviewed and approved — one tap to submit it for you.",
                deep_link=a.get("url") or a.get("application_url") or _LINK_APPLICATIONS,
                action_label="Submit now",
                created_at=a.get("approved_at") or a.get("created_at"),
            ))
        return out
    except Exception as e:  # pragma: no cover - defensive per-source isolation
        logger.warning("today_inbox: auto_submit source failed for %s: %s", user_email, e)
        return []


# ---------------------------------------------------------------------------
# Source 3 — pending Gmail status suggestions to confirm/dismiss
# ---------------------------------------------------------------------------
def _collect_inbox_suggestions(user_email: str) -> List[Tuple[Dict[str, Any], datetime]]:
    """Unresolved Gmail-derived status suggestions. Skipped when the Gmail flag
    is off or the user has no connected inbox."""
    try:
        from services import gmail_service as gs
        if not gs.gmail_oauth_enabled():
            return []
        if not gs.get_connection(user_email):
            return []
        suggestions = gs.list_suggestions(user_email, include_resolved=False) or []
        out: List[Tuple[Dict[str, Any], datetime]] = []
        for s in suggestions[:_PER_SOURCE_LIMIT]:
            sid = s.get("suggestion_id")
            if not sid:
                continue
            suggested = (s.get("suggested_status") or "").strip()
            reason = (s.get("reason") or "").strip()
            detail = f'Looks like "{suggested}" from a recent email.' if suggested \
                else "A recent email may update this application's status."
            if reason:
                detail = f"{detail} {reason}"
            out.append(_item(
                "inbox_suggestion", sid,
                title=_title(s.get("company"), s.get("title"), "Inbox status update"),
                detail=detail[:400],
                # J: Gmail-derived suggestions land on the applications INBOX tab.
                deep_link=_LINK_APPLICATIONS_INBOX,
                action_label="Confirm status",
                created_at=s.get("created_at"),
            ))
        return out
    except Exception as e:  # pragma: no cover - defensive per-source isolation
        logger.warning("today_inbox: inbox_suggestion source failed for %s: %s", user_email, e)
        return []


# ---------------------------------------------------------------------------
# Source 4 — batch-tailor "Did you apply?" confirmations
# ---------------------------------------------------------------------------
def _collect_did_you_apply(user_email: str) -> List[Tuple[Dict[str, Any], datetime]]:
    """tailoring_records flagged `application.batch_confirmation_pending` — the
    batch-tailor prompt asking whether the user actually applied. user_email is
    the verified JWT identity (never client input) so it is safe in the filter;
    every other value is a literal we control."""
    try:
        from utils.db_connect import DBConnect
        db = DBConnect().get_db()
        cursor = (
            db.tailoring_records.find(
                {"user_email": user_email,
                 "application.batch_confirmation_pending": True},
                {"_id": 0, "record_id": 1, "created_at": 1,
                 "jd_analysis.company": 1, "jd_analysis.job_title": 1},
            )
            .sort("created_at", -1)
            .limit(_PER_SOURCE_LIMIT)
        )
        out: List[Tuple[Dict[str, Any], datetime]] = []
        for r in cursor:
            rid = r.get("record_id")
            if not rid:
                continue
            jd = r.get("jd_analysis") or {}
            out.append(_item(
                "did_you_apply", rid,
                title=_title(jd.get("company"), jd.get("job_title"), "Did you apply?"),
                detail="Did you apply to this tailored role? Confirm to track it "
                       "and keep your streak.",
                deep_link=_LINK_APPLICATIONS,
                action_label="I applied / Not yet",
                created_at=r.get("created_at"),
            ))
        return out
    except Exception as e:  # pragma: no cover - defensive per-source isolation
        logger.warning("today_inbox: did_you_apply source failed for %s: %s", user_email, e)
        return []


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------
def get_today_inbox(user_email: str) -> Dict[str, Any]:
    """Aggregate every pending-decision surface into one newest-first, capped
    feed. Read-only, no entitlement spend. Never raises for a per-source failure
    (each collector isolates its own errors); returns
    ``{ "count": int, "items": [ {id, kind, title, detail, deep_link,
    action_label, created_at}, ... ] }``."""
    if not user_email or not isinstance(user_email, str):
        # The route always passes the JWT identity; this is a pure safety net.
        return {"count": 0, "items": []}

    collected: List[Tuple[Dict[str, Any], datetime]] = []
    collected.extend(_collect_needs_input(user_email))
    collected.extend(_collect_auto_submit(user_email))
    collected.extend(_collect_inbox_suggestions(user_email))
    collected.extend(_collect_did_you_apply(user_email))

    # Newest-first across all sources, then the global cap.
    collected.sort(key=lambda pair: pair[1], reverse=True)
    items = [pair[0] for pair in collected[:MAX_ITEMS]]
    return {"count": len(items), "items": items}
