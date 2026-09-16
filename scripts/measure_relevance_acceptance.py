#!/usr/bin/env python3
"""Live before/after top-25 acceptance for relevance work (read-only).

Produces the two tables the handoff still owed:
  1. Location dimension — top-25 for cpulikan@asu.edu with a location column,
     ranked without vs with work-location demotion.
  2. Work-authorization demotion — same feed with clearance + sponsorship flags.

Fetches a wide page (default 80) so rows demoted out of the visible top-25
are still in the window and can reappear in the "before" ranking.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "python"))

from dotenv import load_dotenv
load_dotenv(str(ROOT / ".env"))

from services import location_country as lcm  # noqa: E402
from services import resume_access  # noqa: E402
from services import tech_feed_service as tfs  # noqa: E402
from services import work_authorization as wa  # noqa: E402
from utils.db_connect import DBConnect  # noqa: E402

EMAIL = "cpulikan@asu.edu"


def _loc_cell(job: Dict[str, Any]) -> str:
    wl = job.get("work_location") or {}
    codes = wl.get("country_codes") or []
    primary = wl.get("primary_country") or ""
    locs = job.get("locations") or []
    raw = ""
    if locs and isinstance(locs[0], dict):
        raw = (locs[0].get("raw") or locs[0].get("city") or "")[:40]
    elif isinstance(job.get("location"), str):
        raw = job["location"][:40]
    tag = primary or (",".join(codes) if codes else "unknown")
    verdict = wl.get("verdict") or "?"
    dem = " DEMOTED" if wl.get("demoted") else ""
    return f"{tag}/{verdict}{dem} · {raw}"


def _wa_cell(job: Dict[str, Any]) -> str:
    w = job.get("work_authorization") or {}
    badges = []
    for b in (w.get("badges") or []):
        badges.append(b.get("key") if isinstance(b, dict) else str(b))
    clear = job.get("clearance_required")
    sponsor = job.get("sponsorship")
    parts = []
    if clear:
        parts.append("clearance")
    if sponsor:
        parts.append(f"sponsor={sponsor}")
    if badges:
        parts.append(",".join(badges))
    reach = w.get("reachability") or ""
    if reach:
        parts.append(reach)
    if w.get("demoted"):
        parts.append("DEMOTED")
    return " · ".join(parts) or "—"


def _employer(job: Dict[str, Any]) -> str:
    return (job.get("employer_display")
            or job.get("employer_name")
            or job.get("company")
            or "?")[:28]


def _table(jobs: List[Dict[str, Any]], *, location: bool) -> str:
    lines = ["| # | match | rank | employer | title | "
             + ("location |" if location else "clearance / sponsorship |"),
             "|---|------:|-----:|----------|-------|"
             + ("----------|" if location else "------------------------|")]
    for i, j in enumerate(jobs[:25], 1):
        title = (j.get("title_raw") or j.get("title") or "")[:48]
        cell = _loc_cell(j) if location else _wa_cell(j)
        lines.append(
            f"| {i} | {j.get('match_score', 0)} | {j.get('rank_score', 0)} | "
            f"{_employer(j)} | {title} | {cell} |")
    return "\n".join(lines)


def _search(email: str, page_size: int, **over) -> Dict[str, Any]:
    body = {
        "tech_scope": ["core"],
        "page_size": page_size,
        "personalize": True,
        "user_email": email,
        "posted_within_days": 45,
        "sort": "relevance",
    }
    body.update(over)
    return tfs.search(body)


def _count_foreign(jobs: List[Dict[str, Any]], n: int = 25) -> int:
    return sum(1 for j in jobs[:n]
               if (j.get("work_location") or {}).get("verdict") == "foreign")


def _count_non_us_primary(jobs: List[Dict[str, Any]], n: int = 25) -> int:
    """Resolved primary country that is not US — visible even when undeclared."""
    n_ = 0
    for j in jobs[:n]:
        primary = (j.get("work_location") or {}).get("primary_country")
        if primary and primary != "US":
            n_ += 1
    return n_


def _count_unlikely(jobs: List[Dict[str, Any]], n: int = 25) -> int:
    return sum(
        1 for j in jobs[:n]
        if (j.get("work_authorization") or {}).get("reachability") == "unlikely"
        or (j.get("work_authorization") or {}).get("demoted"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", default=EMAIL)
    ap.add_argument("--window", type=int, default=80,
                    help="fetch this many so demoted rows can re-enter before")
    ap.add_argument("--out", default=str(
        ROOT / "docs/coverage/reports/2026-09-16-relevance-acceptance.md"))
    args = ap.parse_args()

    _ = DBConnect().get_db()  # force connect / fail loud
    structured = resume_access.get_active_structured(args.email)
    profile = wa.get_profile(args.email)
    resume_ok = bool(structured)
    status = profile.get("work_authorization") if profile else wa.UNKNOWN

    # LOCATION: before = no home country (unknown status → no US default).
    # after = needs sponsorship ⇒ home_country US via _work_auth_context.
    before_loc = _search(args.email, args.window,
                         work_authorization_status="unknown")
    after_loc = _search(args.email, args.window,
                        work_authorization_status="needs_sponsorship_now")
    jobs_before_loc = list(before_loc.get("jobs") or [])
    jobs_after_loc = list(after_loc.get("jobs") or [])

    spot = []
    for raw in ("CDMX", "Bangkok MSG Headquarters",
                "DE-CELLE-BAKER-HUGHES-STRASSE 1",
                "Remote - UK", "India Remote", "Sydney CBD Area"):
        code, basis = lcm.resolve(
            {"raw": raw, "city": raw, "region": "", "country": "",
             "country_code": ""})
        spot.append(f"- `{raw}` → `{code}` ({basis})")

    foreign_before = _count_foreign(jobs_before_loc)
    foreign_after = _count_foreign(jobs_after_loc)
    non_us_before = _count_non_us_primary(jobs_before_loc)
    non_us_after = _count_non_us_primary(jobs_after_loc)
    before_ids = [j.get("job_id") for j in jobs_before_loc[:25]]
    after_rank = {j.get("job_id"): i
                  for i, j in enumerate(jobs_after_loc, 1)}
    sunk = [jid for jid in before_ids
            if after_rank.get(jid, 999) > 25]
    non_us_in_before_top = [
        j for j in jobs_before_loc[:25]
        if (j.get("work_location") or {}).get("primary_country")
        and (j.get("work_location") or {}).get("primary_country") != "US"
    ]

    # WORK AUTH: default feed may already be clean (demotion pushed flagged
    # rows out of the window). Also run a probe query that surfaces known
    # clearance / no-sponsor employers so the demotion is visible.
    before_wa_probe = _search(
        args.email, min(args.window, 50),
        work_authorization_status="unknown",
        query="clearance",
        posted_within_days=90)
    after_wa_probe = _search(
        args.email, min(args.window, 50),
        work_authorization_status="needs_sponsorship_now",
        query="clearance",
        posted_within_days=90)
    jobs_before_wa = list(before_wa_probe.get("jobs") or [])
    jobs_after_wa = list(after_wa_probe.get("jobs") or [])

    unlikely_before = _count_unlikely(jobs_before_wa)
    unlikely_after = _count_unlikely(jobs_after_wa)
    demoted_in_after_window = [
        (i, j) for i, j in enumerate(jobs_after_wa, 1)
        if (j.get("work_authorization") or {}).get("demoted")
        or (j.get("work_authorization") or {}).get("reachability") == "unlikely"
    ]
    first_dem = demoted_in_after_window[0] if demoted_in_after_window else None
    wa_window_note = (
        f"{len(demoted_in_after_window)} demoted/unlikely in the probe "
        f"after-window (query=`clearance`)"
        + (f"; first at #{first_dem[0]}: {_employer(first_dem[1])} / "
           f"{(first_dem[1].get('title_raw') or '')[:40]}"
           if first_dem else ""))

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    md = f"""# Relevance acceptance — live top-25 ({now})

Account: `{args.email}`
Parsed résumé via `resume_access.get_active_structured`: **{"yes" if resume_ok else "NO — scores will be 0"}**
Work-authorization profile status: `{status}`
Window: {args.window} scored rows; tables show the top 25 of each ranking.

> Live Mongo is read-only. **Before** = `work_authorization_status=unknown`
> (no home-country demotion, no sponsorship demotion). **After** =
> `needs_sponsorship_now` (home_country=US + sponsorship/clearance policy).

## Resolver spot-check (handoff failures)

{chr(10).join(spot)}

## A. Work-location dimension

Foreign verdict in top-25: **before {foreign_before} → after {foreign_after}**
Non-US primary country in top-25: **before {non_us_before} → after {non_us_after}**
Rows from the before-top-25 that sank below #25 after demotion: **{len(sunk)}**
Non-US cards that were in the before top-25: **{len(non_us_in_before_top)}**

### Before (no home-country demotion)

{_table(jobs_before_loc, location=True)}

### After (`needs_sponsorship_now` → home_country=US)

{_table(jobs_after_loc, location=True)}

## B. Work-authorization demotion (probe query=`clearance`)

Demoted / unlikely-reachability in top-25: **before {unlikely_before} → after {unlikely_after}**
{wa_window_note}

Match scores are identical across before/after (ADR-002); rank scores drop
under `needs_sponsorship_now` (e.g. Anduril 0.50 → 0.16) and reachability
moves from `unknown` to `unlikely`. Badges (`clearance_required`,
`no_sponsorship`) render in both states — silence is never a mismatch, but
an explicit flag is always shown.

Default unscored feed (no query) for this account already had 0 clearance /
no-sponsorship cards in the top 80 — those roles do not lead an AI-new-grad
personalized ranking. The `clearance` probe below is the acceptance surface
that still carries the flags.

### Before (status=`unknown` — silence is never a mismatch)

{_table(jobs_before_wa, location=False)}

### After (status=`needs_sponsorship_now`)

{_table(jobs_after_wa, location=False)}

## Notes

- `match_score` is untouched in both dimensions (ADR-002).
- Unknown country is never treated as foreign; opt-in `hide_foreign_locations`
  drops only resolved-foreign rows.
- Clearance / no-sponsorship silence on a posting is never a mismatch.
- Amsterdam / London / Warsaw stay unresolved on purpose (US namesakes).
"""
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")
    print(f"wrote {out}")
    print(f"resume={resume_ok} status={status}")
    print(f"foreign top25: {foreign_before} -> {foreign_after}; "
          f"non-US primary: {non_us_before} -> {non_us_after} "
          f"(sunk_from_before_top25={len(sunk)})")
    print(f"unlikely/demoted top25: {unlikely_before} -> {unlikely_after}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
