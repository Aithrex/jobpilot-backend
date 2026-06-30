"""
Visa Timeline Copilot — OPT / STEM / H-1B clock computations for F-1 students.

Frontend collects a small profile (current visa status + a few dates + STEM
eligibility flag). This service turns that into a calendar of cliffs:

  · OPT 90-day unemployment counter (drop-dead date)
  · STEM extension filing window (must file 90 days BEFORE OPT EAD ends)
  · STEM 24-month extension end
  · STEM unemployment limit (150 days cumulative on OPT + STEM-OPT)
  · H-1B lottery registration window (typically March 6-22 each fiscal year)
  · H-1B FY1 start (October 1)

Every cliff carries:
  - id (stable key)
  - label (short)
  - description (why it matters in plain English)
  - date (ISO date when the cliff fires, OR null when not applicable)
  - severity ("info" | "warning" | "critical") — drives UI color
  - days_remaining (computed at request time)

Pure-functional; no DB writes inside this module. All persistence lives in
the visa_profiles collection wired up by the resume.py blueprint.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Profile schema (Pythonic — the DB stores the same fields verbatim)
# ---------------------------------------------------------------------------

VALID_VISA_STATUS = {
    "F1",            # On F-1, not yet on OPT
    "F1-CPT",        # Currently using CPT
    "F1-OPT",        # Post-completion OPT (1 year)
    "F1-STEM-OPT",   # STEM extension (additional 24 months)
    "H1B",           # On H-1B
    "Other",
}


def _parse_iso(s: Optional[str]) -> Optional[date]:
    """Parse YYYY-MM-DD; return None for empty / malformed."""
    if not s or not isinstance(s, str):
        return None
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def normalize_profile(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Coerce a stored / submitted profile dict into the canonical shape.

    Unknown keys are dropped; unknown values for `visa_status` fall back to
    "Other". Dates are kept as ISO strings (YYYY-MM-DD) when valid.
    """
    raw = raw or {}
    status = str(raw.get("visa_status") or "Other")
    if status not in VALID_VISA_STATUS:
        status = "Other"

    def _norm_date(key: str) -> Optional[str]:
        v = raw.get(key)
        d = _parse_iso(v if isinstance(v, str) else None)
        return d.isoformat() if d else None

    return {
        "visa_status": status,
        "country_of_birth": (raw.get("country_of_birth") or "").strip()[:60] or None,
        "stem_degree": bool(raw.get("stem_degree", False)),
        # Optional date fields — all ISO YYYY-MM-DD, all nullable.
        "opt_ead_arrival_date": _norm_date("opt_ead_arrival_date"),
        "opt_start_date": _norm_date("opt_start_date"),
        "opt_end_date": _norm_date("opt_end_date"),
        "stem_extension_filed": bool(raw.get("stem_extension_filed", False)),
        "stem_extension_end_date": _norm_date("stem_extension_end_date"),
        "cpt_end_date": _norm_date("cpt_end_date"),
        "h1b_lottery_year": (
            int(raw["h1b_lottery_year"])
            if isinstance(raw.get("h1b_lottery_year"), (int, float))
            and 2024 <= int(raw["h1b_lottery_year"]) <= 2035
            else None
        ),
        "h1b_lottery_filed": bool(raw.get("h1b_lottery_filed", False)),
        "current_employer_e_verified": bool(raw.get("current_employer_e_verified", False)),
        # User-editable note (free text) — surfaces in the dashboard.
        "notes": (raw.get("notes") or "").strip()[:1000],
    }


# ---------------------------------------------------------------------------
# Timeline computation
# ---------------------------------------------------------------------------

@dataclass
class Milestone:
    id: str
    label: str
    description: str
    date: Optional[str]            # ISO YYYY-MM-DD or None
    days_remaining: Optional[int]  # negative if past
    severity: str                  # "info" | "warning" | "critical"


def _days_between(target: Optional[date], today: date) -> Optional[int]:
    if not target:
        return None
    return (target - today).days


def _severity_for(days: Optional[int], *, critical_at: int = 14, warning_at: int = 60) -> str:
    if days is None:
        return "info"
    if days < 0:
        return "info"  # past events demoted
    if days <= critical_at:
        return "critical"
    if days <= warning_at:
        return "warning"
    return "info"


def _h1b_lottery_dates(fy_year: int) -> Dict[str, date]:
    """Hardcoded USCIS H-1B FY cap-season anchors.

    These have been stable across multiple FYs:
      - Registration window: March 6 → March 22
      - Selection notifications: by March 31
      - Filing window opens: April 1
      - FY start (employment): October 1 of the same fiscal year
    """
    return {
        "registration_open": date(fy_year - 1, 3, 6),
        "registration_close": date(fy_year - 1, 3, 22),
        "selection_complete": date(fy_year - 1, 3, 31),
        "filing_open": date(fy_year - 1, 4, 1),
        "fy_start": date(fy_year - 1, 10, 1),
    }


def compute_timeline(profile: Dict[str, Any], today: Optional[date] = None) -> List[Milestone]:
    """Return an ordered list of upcoming/past visa milestones for the profile.

    Order: future events ascending by date first, then past events newest-first.
    """
    today = today or date.today()
    out: List[Milestone] = []

    status = profile.get("visa_status") or "Other"
    opt_ead = _parse_iso(profile.get("opt_ead_arrival_date"))
    opt_start = _parse_iso(profile.get("opt_start_date"))
    opt_end = _parse_iso(profile.get("opt_end_date"))
    stem_filed = bool(profile.get("stem_extension_filed"))
    stem_end = _parse_iso(profile.get("stem_extension_end_date"))
    cpt_end = _parse_iso(profile.get("cpt_end_date"))
    is_stem = bool(profile.get("stem_degree"))
    h1b_year = profile.get("h1b_lottery_year")
    h1b_filed = bool(profile.get("h1b_lottery_filed"))

    # ── CPT (only if currently on CPT) ──────────────────────────────────
    if status == "F1-CPT" and cpt_end:
        days = _days_between(cpt_end, today)
        out.append(Milestone(
            id="cpt_end",
            label="CPT ends",
            description="Last day of current CPT authorization. Plan a CPT renewal, start OPT, or end employment by this date.",
            date=cpt_end.isoformat(),
            days_remaining=days,
            severity=_severity_for(days, critical_at=21, warning_at=60),
        ))

    # ── OPT EAD arrival ─────────────────────────────────────────────────
    if status in ("F1", "F1-OPT") and opt_ead and opt_ead >= today:
        days = _days_between(opt_ead, today)
        out.append(Milestone(
            id="opt_ead_arrival",
            label="OPT EAD card expected",
            description="USCIS typically mails the EAD ~7-10 days after approval. You cannot work until the card arrives.",
            date=opt_ead.isoformat(),
            days_remaining=days,
            severity=_severity_for(days, critical_at=7, warning_at=21),
        ))

    # ── OPT 90-day unemployment clock ───────────────────────────────────
    # The clock starts the day OPT BEGINS (opt_start_date), not when the
    # card arrives. 90 days cumulative unemployed → must depart the US.
    if status in ("F1-OPT", "F1-STEM-OPT") and opt_start:
        unemployment_deadline = opt_start + timedelta(days=90)
        days = _days_between(unemployment_deadline, today)
        out.append(Milestone(
            id="opt_unemployment_deadline",
            label="OPT 90-day unemployment cliff",
            description="If you're unemployed past this date you lose F-1 status. STEM extension extends this to 150 cumulative days.",
            date=unemployment_deadline.isoformat(),
            days_remaining=days,
            severity=_severity_for(days, critical_at=14, warning_at=45),
        ))

    # ── OPT end ─────────────────────────────────────────────────────────
    if opt_end:
        days = _days_between(opt_end, today)
        out.append(Milestone(
            id="opt_end",
            label="OPT EAD expires",
            description="Last day of work authorization on standard OPT (1 year). File STEM extension before this if eligible.",
            date=opt_end.isoformat(),
            days_remaining=days,
            severity=_severity_for(days, critical_at=30, warning_at=120),
        ))

        # STEM filing window: USCIS recommends filing ≥90 days before OPT
        # ends, and the application MUST be received before OPT EAD expires.
        if is_stem and not stem_filed and status in ("F1-OPT",):
            file_by = opt_end - timedelta(days=1)  # latest acceptable
            recommended_by = opt_end - timedelta(days=90)
            days = _days_between(recommended_by, today)
            out.append(Milestone(
                id="stem_filing_window",
                label="STEM extension filing window",
                description="File the STEM 24-month extension ≥90 days before OPT expires. USCIS must RECEIVE the application before OPT ends or you lose work authorization.",
                date=recommended_by.isoformat(),
                days_remaining=days,
                severity=_severity_for(days, critical_at=30, warning_at=90),
            ))
            # Hard cliff
            days_hard = _days_between(file_by, today)
            out.append(Milestone(
                id="stem_filing_hard_cliff",
                label="STEM extension — last day to file",
                description="USCIS MUST receive your I-765 STEM application before this date or you lose F-1 work authorization.",
                date=file_by.isoformat(),
                days_remaining=days_hard,
                severity="critical",
            ))

    # ── STEM extension end ──────────────────────────────────────────────
    if status == "F1-STEM-OPT" and stem_end:
        days = _days_between(stem_end, today)
        out.append(Milestone(
            id="stem_extension_end",
            label="STEM extension expires",
            description="Last day of STEM-OPT (24 months on top of standard OPT). After this you need H-1B or another visa class.",
            date=stem_end.isoformat(),
            days_remaining=days,
            severity=_severity_for(days, critical_at=30, warning_at=180),
        ))

    # ── H-1B lottery (always show next FY's window) ─────────────────────
    if status in ("F1", "F1-OPT", "F1-STEM-OPT", "F1-CPT"):
        # Default to next FY if user hasn't set one
        if not h1b_year:
            # If we're already past March of current year, target next FY
            h1b_year = today.year + 1 if today.month >= 4 else today.year
        dates_ = _h1b_lottery_dates(int(h1b_year))
        reg_close = dates_["registration_close"]
        days = _days_between(reg_close, today)
        if days is not None and days >= -7:  # show through close + 1 week grace
            out.append(Milestone(
                id="h1b_registration_close",
                label=f"H-1B FY{h1b_year} lottery registration closes",
                description="Final day to register an employer-sponsored H-1B cap entry on USCIS. If you have multiple offers, each employer can register you.",
                date=reg_close.isoformat(),
                days_remaining=days,
                severity=_severity_for(days, critical_at=7, warning_at=30),
            ))
        if h1b_filed:
            sel = dates_["selection_complete"]
            sel_days = _days_between(sel, today)
            out.append(Milestone(
                id="h1b_selection",
                label=f"H-1B FY{h1b_year} selection results",
                description="USCIS notifies selected registrants by this date. If selected, filing window opens April 1.",
                date=sel.isoformat(),
                days_remaining=sel_days,
                severity="info" if sel_days is None or sel_days < 0 else _severity_for(sel_days),
            ))
            fy = dates_["fy_start"]
            fy_days = _days_between(fy, today)
            out.append(Milestone(
                id="h1b_fy_start",
                label=f"H-1B FY{h1b_year} employment starts",
                description="Earliest possible H-1B employment start date once approved.",
                date=fy.isoformat(),
                days_remaining=fy_days,
                severity="info",
            ))

    # Sort: future events ascending, past events descending after.
    def _sort_key(m: Milestone):
        if m.days_remaining is None:
            return (3, 0)
        if m.days_remaining >= 0:
            return (0, m.days_remaining)
        return (1, -m.days_remaining)
    out.sort(key=_sort_key)
    return out


def serialize_timeline(milestones: List[Milestone]) -> List[Dict[str, Any]]:
    return [asdict(m) for m in milestones]
