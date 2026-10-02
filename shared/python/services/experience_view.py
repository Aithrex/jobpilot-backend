"""The experience levels a candidate's job board opens on.

The "For you" board used to open on every level. Every current user is
early-career (0-4 years, measured 2026-10-02), and the first screen a
zero-year user saw held "Web Application Architect 3", "Software Engineer III"
and two executive postings. This is the default the board opens on instead;
"All levels" is one tap away and the board remembers it.

Two inputs, both the candidate's own:

* `user_preferences.experience_level` - what they told us at onboarding
  ("Junior", "Mid-level", ...). Stated, so it is never overridden: a
  three-year candidate whose resume title says "Senior" but who told us "Mid"
  sees mid.
* the active resume's `experience_years`.

When the two point at different bands, or the years sit on a band's edge,
the view is BOTH bands - a candidate between entry and mid sees entry and
mid, not an arbitrary one of them.

Levels are in the board's VIEW vocabulary: intern, entry, mid, senior, staff,
where `staff` ("Staff+") covers staff, principal, director and executive. It
was one "Senior+" view until 2026-10-03, so a four-year candidate on
Mid + Senior was shown principal and director roles (30% of one feed).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

VIEWS = ("intern", "entry", "mid", "senior", "staff")
_VIEW_OF_BAND = {"intern": "intern", "entry": "entry", "mid": "mid",
                 "senior": "senior", "staff": "staff", "principal": "staff",
                 "director": "staff", "executive": "staff"}
# Years at the TOP of a band also reach the next one up: two years is the
# last entry year and a credible mid applicant; four is the last mid year.
# Same ladder as `fit_score._YEARS_TO_LEVEL`, so the board and the score agree.
_EDGE_YEARS = {2: "mid", 4: "senior", 7: "staff"}


def resolve(preference: Any, years: Any) -> Dict[str, Any]:
    """`{levels, source, years, preference}` from the two raw inputs."""
    from services import fit_score

    views: set = set()
    pref_band = fit_score.normalize_level(preference) if preference else None
    if pref_band in _VIEW_OF_BAND:
        views.add(_VIEW_OF_BAND[pref_band])
        if pref_band == "intern":
            # "Intern / New grad": a new graduate is the entry band too.
            views.add("entry")
    else:
        pref_band = None

    try:
        n: Optional[int] = int(years) if years is not None else None
    except (TypeError, ValueError):
        n = None
    if n is not None and n < 0:
        n = None
    if n is not None:
        band = fit_score._level_from_years(n)
        resume_view = _VIEW_OF_BAND.get(band or "", None)
        # A stated level is the candidate's own answer; the resume adds a
        # band BESIDE it, never one far away ("Junior" with nine years stays
        # junior - they told us what they are looking for).
        if resume_view and (not pref_band or _beside(resume_view, views)):
            views.add(resume_view)
        if n in _EDGE_YEARS and (not pref_band or resume_view in views):
            views.add(_EDGE_YEARS[n])
        if n == 0 and not pref_band:
            views.add("intern")

    source = ("preference+resume" if pref_band and n is not None
              else "preference" if pref_band
              else "resume" if n is not None else "none")
    return {
        "levels": [v for v in VIEWS if v in views],
        "source": source,
        "years": n,
        "preference": str(preference) if pref_band else None,
    }


def _beside(view: str, views: set) -> bool:
    """`view` is one of `views` or one rung away from one of them."""
    i = VIEWS.index(view)
    return any(abs(i - VIEWS.index(v)) <= 1 for v in views)


def for_user(user_email: str) -> Dict[str, Any]:
    """`resolve` for a stored user. Never raises: a failure is "all levels"."""
    try:
        from utils.db_connect import DBConnect

        db = DBConnect().get_db()
        email = str(user_email or "").strip().lower()
        prefs = db.user_preferences.find_one({"user_email": email},
                                             {"experience_level": 1, "seniority": 1}) or {}
        preference = prefs.get("experience_level") or prefs.get("seniority")
        years = None
        user = db.users.find_one({"email": email}, {"active_resume_id": 1}) or {}
        rid = user.get("active_resume_id")
        if rid:
            asset = db.resume_assets.find_one(
                {"resume_id": rid, "parse_status": "parsed"}, {"experience_years": 1}) or {}
            years = asset.get("experience_years")
        return resolve(preference, years)
    except Exception as exc:  # pragma: no cover - never break the board
        logger.warning("experience_view: falling back to all levels (%s)", type(exc).__name__)
        return resolve(None, None)


def view_levels(levels: List[str]) -> List[str]:
    """Clamp an arbitrary list to the view vocabulary, ladder-ordered."""
    wanted = {str(l).strip().lower() for l in (levels or [])}
    return [v for v in VIEWS if v in wanted]
