"""Auto-apply email templates (Tranche 1 §2) — PURE builders, no sends.

Three templates, each returning ``{"subject", "html", "text"}``:

    apply_approval_request(matches, approve_all_url=…, review_url=…, skip_url=…)
        Hourly: "N new matches — Approve all / Review / Skip". Every button is
        an approve-link URL minted by ``approval_token_service.make_link``.
    apply_needs_input(items)
        Oracle tap / novel screening question — one action button per item.
    apply_daily_summary(submitted, needs_you, review_url=…)
        EOD: what was applied + what still needs you — actionable buttons, not
        a passive report.

Design mirrors ``notifications/digests.py``: builders are pure (no Mongo, no
SES, no token minting — tokens are a WRITE, so the §5 scheduler mints links
first and passes URLs in), inline-styled HTML + a faithful text twin. Dispatch
reuses ``email_service.send_email`` via the ``send`` convenience below (which
inherits the suppression check + dev console fallback for free).

All user/job-derived text is HTML-escaped — job titles and screening questions
come from scraped postings, i.e. untrusted input inside our own email HTML.
"""
from __future__ import annotations

import html as _html
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

APP_NAME = "Aspirely"

# How long links stay clickable — must match approval_token_service.DEFAULT_TTL_SECONDS.
_LINK_LIFETIME_HUMAN = "3 days"

_FOOTER_HTML = (
    '<p style="color:#aaa;font-size:12px;margin-top:24px">Buttons in this email are '
    f'single-use and expire in {_LINK_LIFETIME_HUMAN}. You get these because auto-apply '
    'is on — change what you get or unsubscribe in Settings &rarr; Notifications.</p>'
)
_FOOTER_TEXT = (
    f"Buttons in this email are single-use and expire in {_LINK_LIFETIME_HUMAN}. "
    "Change preferences or unsubscribe: Settings -> Notifications."
)


def _esc(value: Any) -> str:
    return _html.escape(str(value or ""), quote=True)


def _wrap(title: str, body_html: str) -> str:
    """The shared outer frame (same look as digests.py / send_password_reset)."""
    return (
        '<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;'
        'max-width:560px;margin:auto">'
        f'<h2 style="margin:0 0 12px">{_esc(title)}</h2>'
        f'{body_html}'
        f'{_FOOTER_HTML}'
        '</div>'
    )


def _button(url: str, label: str, *, primary: bool = True) -> str:
    if primary:
        style = ("background:#4f46e5;color:#fff;padding:12px 20px;border-radius:8px;"
                 "text-decoration:none;display:inline-block;font-weight:600;margin:0 8px 8px 0")
    else:
        style = ("background:#eef0f6;color:#333;padding:12px 20px;border-radius:8px;"
                 "text-decoration:none;display:inline-block;font-weight:600;margin:0 8px 8px 0")
    return f'<a href="{_esc(url)}" style="{style}">{_esc(label)}</a>'


def _job_line_html(item: Dict[str, Any], extra_html: str = "") -> str:
    loc = f' <span style="color:#888">{_esc(item.get("location"))}</span>' if item.get("location") else ""
    return (
        '<li style="margin:0 0 10px;line-height:1.4">'
        f'<span style="font-weight:600">{_esc(item.get("title") or "Role")}</span>'
        f' &mdash; {_esc(item.get("company") or "")}{loc}{extra_html}</li>'
    )


def _job_line_text(item: Dict[str, Any]) -> str:
    loc = f" ({item.get('location')})" if item.get("location") else ""
    return f"- {item.get('title') or 'Role'} — {item.get('company') or ''}{loc}"


# ===========================================================================
# 1. apply_approval_request — hourly: "N new matches — Approve all / Review / Skip"
# ===========================================================================
def apply_approval_request(matches: List[Dict[str, Any]], *,
                           approve_all_url: str,
                           review_url: str,
                           skip_url: str) -> Dict[str, str]:
    """The hourly nudge for anything the §3 autonomy decision left at ask/hold.

    ``matches``: ``[{title, company, location?, approve_url?}]`` — pass a
    per-match ``approve_url`` (a scoped ``approve_one`` link) to render an
    inline Approve button on that row. The three footer buttons are scoped
    ``approve_all`` / ``review`` / ``skip`` links over this exact batch.
    """
    n = len(matches)
    subject = f"{n} new job match{'es' if n != 1 else ''} ready to apply — your call"

    rows = ""
    for m in matches:
        inline = ""
        if m.get("approve_url"):
            inline = (f' &middot; <a href="{_esc(m["approve_url"])}" '
                      'style="color:#4f46e5;font-weight:600;text-decoration:none">Approve this one</a>')
        rows += _job_line_html(m, inline)
    if not rows:
        rows = '<li style="color:#666">No new matches this hour.</li>'

    body = (
        '<p style="color:#444;line-height:1.5">These matches passed your filters but '
        'need your go-ahead before we apply. One tap and we take it from there.</p>'
        f'<ul style="padding-left:18px;margin:0 0 20px">{rows}</ul>'
        '<p style="margin:24px 0 0">'
        f'{_button(approve_all_url, f"Approve all {n}" if n > 1 else "Approve")}'
        f'{_button(review_url, "Review in app", primary=False)}'
        f'{_button(skip_url, "Skip these", primary=False)}'
        '</p>'
    )

    lines = [subject, "",
             "These matches need your go-ahead before we apply:", ""]
    for m in matches:
        lines.append(_job_line_text(m))
        if m.get("approve_url"):
            lines.append(f"  Approve this one: {m['approve_url']}")
    lines += ["",
              f"Approve all: {approve_all_url}",
              f"Review in app: {review_url}",
              f"Skip these: {skip_url}",
              "", _FOOTER_TEXT]

    return {"subject": subject,
            "html": _wrap(subject, body),
            "text": "\n".join(lines)}


# ===========================================================================
# 2. apply_needs_input — Oracle tap / novel screening question, one action per item
# ===========================================================================
def apply_needs_input(items: List[Dict[str, Any]]) -> Dict[str, str]:
    """Applications blocked on something only the user can resolve.

    ``items``: ``[{title, company, question?, reason?, action_url, action_label?}]``
    — ``question`` is the novel screening question (if that's the blocker),
    ``reason`` a short human blocker description otherwise (e.g. "verification
    code required"). ``action_url`` is a scoped ``review`` (or ``skip``) link;
    exactly one button per item, per the spec.
    """
    n = len(items)
    subject = (f"{n} application{'s' if n != 1 else ''} "
               f"need{'s' if n == 1 else ''} a quick answer from you")

    blocks = ""
    for it in items:
        detail = ""
        if it.get("question"):
            detail = (
                '<p style="margin:6px 0 10px;padding:10px 12px;background:#f6f7fb;'
                'border-radius:8px;color:#444;line-height:1.5">'
                f'&ldquo;{_esc(it["question"])}&rdquo;</p>'
            )
        elif it.get("reason"):
            detail = f'<p style="margin:6px 0 10px;color:#444;line-height:1.5">{_esc(it["reason"])}</p>'
        blocks += (
            '<div style="margin:0 0 20px">'
            f'<p style="margin:0 0 4px;font-weight:600">{_esc(it.get("title") or "Role")}'
            f' &mdash; {_esc(it.get("company") or "")}</p>'
            f'{detail}'
            f'{_button(it.get("action_url") or "", it.get("action_label") or "Answer & continue")}'
            '</div>'
        )
    if not blocks:
        blocks = '<p style="color:#666">Nothing needs you right now.</p>'

    body = (
        '<p style="color:#444;line-height:1.5">These applications are ready except for '
        'one thing only you can answer. Each takes about a tap.</p>'
        f'{blocks}'
    )

    lines = [subject, ""]
    for it in items:
        lines.append(_job_line_text(it))
        if it.get("question"):
            lines.append(f"  Q: {it['question']}")
        elif it.get("reason"):
            lines.append(f"  {it['reason']}")
        lines.append(f"  {it.get('action_label') or 'Answer & continue'}: {it.get('action_url') or ''}")
        lines.append("")
    lines += [_FOOTER_TEXT]

    return {"subject": subject,
            "html": _wrap(subject, body),
            "text": "\n".join(lines)}


# ===========================================================================
# 3. apply_daily_summary — EOD: applied + still-needs-you, with live buttons
# ===========================================================================
def apply_daily_summary(submitted: List[Dict[str, Any]],
                        needs_you: List[Dict[str, Any]], *,
                        review_url: str) -> Dict[str, str]:
    """The actionable end-of-day report (sent from run_evening_summaries, §5).

    ``submitted``: ``[{title, company, location?}]`` — applied today.
    ``needs_you``: ``[{title, company, reason?, action_url?}]`` — still blocked;
    rows with an ``action_url`` (a scoped link) render their own button.
    ``review_url`` is a scoped ``review`` link into the app.
    """
    ns, nn = len(submitted), len(needs_you)
    subject = (f"Auto-apply today: {ns} submitted"
               + (f" · {nn} need{'s' if nn == 1 else ''} you" if nn else ""))

    sub_rows = "".join(_job_line_html(m) for m in submitted) or (
        '<li style="color:#666">No applications submitted today.</li>')
    body = (
        f'<h3 style="margin:0 0 8px">Applied for you ({ns})</h3>'
        f'<ul style="padding-left:18px;margin:0 0 20px">{sub_rows}</ul>'
    )

    if needs_you:
        ny_rows = ""
        for it in needs_you:
            extra = ""
            if it.get("reason"):
                extra = f' <span style="color:#b45309">&mdash; {_esc(it["reason"])}</span>'
            if it.get("action_url"):
                extra += (f' &middot; <a href="{_esc(it["action_url"])}" '
                          'style="color:#4f46e5;font-weight:600;text-decoration:none">Resolve</a>')
            ny_rows += _job_line_html(it, extra)
        body += (
            f'<h3 style="margin:0 0 8px">Still needs you ({nn})</h3>'
            f'<ul style="padding-left:18px;margin:0 0 20px">{ny_rows}</ul>'
        )

    body += f'<p style="margin:24px 0 0">{_button(review_url, "Open your applications")}</p>'

    lines = [subject, "", f"Applied for you ({ns}):"]
    lines += [_job_line_text(m) for m in submitted] or ["- none today"]
    if needs_you:
        lines += ["", f"Still needs you ({nn}):"]
        for it in needs_you:
            row = _job_line_text(it)
            if it.get("reason"):
                row += f" — {it['reason']}"
            lines.append(row)
            if it.get("action_url"):
                lines.append(f"  Resolve: {it['action_url']}")
    lines += ["", f"Open your applications: {review_url}", "", _FOOTER_TEXT]

    return {"subject": subject,
            "html": _wrap(subject, body),
            "text": "\n".join(lines)}


# ===========================================================================
# Dispatch — reuse email_service's send path (suppression check + dev fallback)
# ===========================================================================
def send(to_email: str, template: Dict[str, str]) -> bool:
    """Send a built template through ``email_service.send_email``. Best-effort,
    never raises — mirrors the send contract callers already rely on."""
    try:
        from services.email_service import send_email
        return send_email(to_email, template["subject"], template["html"], template["text"])
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("apply_email_templates.send failed for %s: %s", to_email, e)
        return False
