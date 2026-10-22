"""Read-only, re-runnable per-user feed fidelity audit.

Replays what each real account is shown on the "For you" board and asks
whether those jobs are in the account's own occupation. A neighbouring script,
`match_accuracy_audit.py`, measures the tech-feed search. This one measures the
surface users actually open: the inventory-served Workday and career-pages
tabs that `CombinedJobsProvider` merges.

For every user with a parsed resume:

1. rebuild the request the frontend sends: `match_titles` (the title seed the
   board searches with) plus the user's own experience levels;
2. run the REAL `inventory_feed.build_feed` for both inventory tabs, merge,
   dedupe, apply the board default (own levels, unknown level kept, best match
   first) and keep the top N;
3. label every shown row's occupation with the production classifiers and
   judge it against the user's occupational DOMAIN;
4. count the in-domain postings we hold, independently of the feed's filters.

The live MNC tab is a network crawl and is not replayed.

The user's domain comes from `--labels` (a JSON file mapping an email to a list
of domain keys, kept out of git under `.agents/`) and falls back to the
resume's own taxonomy families. Domains are generic (`DOMAINS`): a fix is
judged by every account in a domain, never tuned for one.

READ-ONLY, enforced: every pymongo write method is replaced with one that
raises before anything is imported, so a write anywhere on the path fails the
run rather than touching production. Output goes to
`.agents/tasks/feed-fidelity/scorecard-<run>.{md,json}`.

Usage (from the repo root):
    python scripts/user_feed_fidelity_audit.py
    python scripts/user_feed_fidelity_audit.py --users bnb,san --top 50 --show 15
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "shared" / "python"))
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:  # pragma: no cover - dotenv is a dev/runtime dep
    pass

import pymongo.collection as _pc  # noqa: E402

_WRITE_METHODS = (
    "insert_one", "insert_many", "update_one", "update_many", "replace_one",
    "delete_one", "delete_many", "find_one_and_update", "find_one_and_replace",
    "find_one_and_delete", "bulk_write", "create_index", "create_indexes",
    "drop", "drop_index",
)


def _deny(name: str):
    def _raise(self, *args, **kwargs):
        raise RuntimeError(f"read-only audit: refused {name} on {self.name}")
    return _raise


for _method in _WRITE_METHODS:
    setattr(_pc.Collection, _method, _deny(_method))

from utils.db_connect import DBConnect  # noqa: E402

from services import experience_view  # noqa: E402
from services import fit_score  # noqa: E402
from services import inventory_feed  # noqa: E402
from services import job_title_seed  # noqa: E402
from services import resume_access  # noqa: E402
from services import tech_taxonomy as tx  # noqa: E402
from services.experience_view import _VIEW_OF_BAND  # noqa: E402

OUT_DIR = ROOT / ".agents" / "tasks" / "feed-fidelity"
#: G2's window: what a user checking the board daily must not miss.
RECALL_DAYS = 3
TABS = (inventory_feed.SOURCE_WORKDAY, inventory_feed.SOURCE_CAREER)

#: Occupational domains. A row is ON-domain when any of its readings lands in
#: the domain: a taxonomy family (`tech`), a legacy `role_family` (`legacy`),
#: or a title pattern for occupations neither vocabulary names (`title`).
#: It is OFF-domain when it carries a confident reading and none of them is
#: in the domain. Everything else is UNCLEAR and is reported, not judged.
DOMAINS: Dict[str, Dict[str, Any]] = {
    "software": {
        "tech": {"software_engineering", "cloud_platform", "quality",
                 "technical_leadership", "security"},
        "legacy": {"software_engineering", "devops", "qa", "security"},
    },
    "data_ml": {
        "tech": {"data", "ai_ml"},
        "legacy": {"data_engineering", "data_science", "data_analysis"},
    },
    "electrical_power": {
        "title": (r"\b(electrical|power systems?|power studies|power engineer|"
                  r"substation|protection (and|&) control|relay (engineer|technician|settings)|"
                  r"transmission (engineer|planning|line)|"
                  r"distribution (planning|engineer)|grid (engineer|modernization|integration)|"
                  r"interconnection|"
                  r"high voltage|switchgear|scada|energy systems?|"
                  r"utility (engineer|engineering|planner|designer)|"
                  r"electric(al)? design|ee engineer)\b"),
    },
    "hris": {
        "title": (r"\b(workday|hcm|hris|hr systems?|hr technology|people systems|"
                  r"hr information|human resources information|payroll systems?|"
                  r"successfactors|oracle hcm|ukg)\b"),
    },
    "embedded_hw": {
        "tech": {"semiconductor_hardware"},
        "legacy": {"hardware_engineering"},
    },
}


def _db():
    return DBConnect().get_db()


def _utc_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _mask(email: str) -> str:
    local = str(email or "").split("@", 1)[0]
    return (local[:3] + "***") if local else "***"


# ---------------------------------------------------------------------------
# The request the board sends
# ---------------------------------------------------------------------------

def board_titles(email: str) -> Dict[str, Any]:
    """`match_titles` exactly as `/jobs/title-seed` would compute them, minus
    the write `public_title_seed` makes to refresh its cache."""
    assets = resume_access.list_parsed_assets(email)
    view = job_title_seed.search_titles_view(
        email, assets, job_title_seed.load_title_seed(email))
    return {
        "resume_titles": view["resume_titles"],
        "synth_titles": view["synth_titles"],
        "match_titles": view["match_titles"],
        "resume_count": len(assets),
    }


def _band_of(row: Dict[str, Any]) -> str:
    return str(row.get("seniority") or row.get("seniority_band") or "unknown").lower()


def _in_levels(row: Dict[str, Any], levels: List[str]) -> bool:
    """`careerJobs.matchesSeniorityLevels`: unknown always passes."""
    if not levels:
        return True
    band = _band_of(row)
    if band in ("", "unknown"):
        return True
    return _VIEW_OF_BAND.get(band, "mid") in levels


def replay_board(email: str, titles: List[str], levels: List[str]) -> Dict[str, Any]:
    merged: Dict[str, Dict[str, Any]] = {}
    tab_counts: Dict[str, Any] = {}
    for tab in TABS:
        payload = {"titles": titles, "user_email": email, "us_only": True,
                   "tz_offset_minutes": 0}
        result = inventory_feed.build_feed(tab, payload)
        if result is None:
            tab_counts[tab] = "deferred_to_crawl"
            continue
        rows = result.get("jobs") or []
        tab_counts[tab] = len(rows)
        for row in rows:
            key = inventory_feed.dedupe_key_of(row)
            prev = merged.get(key)
            if prev is None or (row.get("match_score") or 0) > (prev.get("match_score") or 0):
                merged[key] = row
    shown = [r for r in merged.values() if _in_levels(r, levels)]
    shown.sort(key=lambda r: (
        4 if r.get("sponsorship_demoted") else 0,
        -(r.get("match_score") or 0),
        r.get("days_ago") if r.get("days_ago") is not None else 99))
    return {"rows": shown, "tab_counts": tab_counts, "merged": len(merged)}


# ---------------------------------------------------------------------------
# Judging a row against a domain
# ---------------------------------------------------------------------------

def readings(title: str) -> Dict[str, Any]:
    c = tx.classify(title)
    return {
        "scope": getattr(c, "tech_scope", None),
        "tech": list(getattr(c, "role_families", None) or []),
        "legacy": fit_score.classify_role(title),
        "non_tech": tx.non_tech_domain_of(title),
    }


def judge(title: str, domains: List[str]) -> str:
    r = readings(title)
    for key in domains:
        spec = DOMAINS.get(key) or {}
        rx = spec.get("title")
        if rx and re.search(rx, title, re.IGNORECASE):
            return "on"
        if set(r["tech"]) & set(spec.get("tech") or ()):
            return "on"
        if r["legacy"] and r["legacy"] in (spec.get("legacy") or ()):
            return "on"
    if r["tech"] or r["legacy"] or r["non_tech"] or r["scope"] == "non_tech":
        return "off"
    return "unclear"


def held_in_domain(domains: List[str]) -> int:
    """Active postings in the 30-day window (or undated) that a domain owns.
    Not geo-filtered: `us_geo` is a Python rule over free text, so this is an
    upper bound on what the US-only board could show."""
    since = datetime.now(timezone.utc) - timedelta(days=inventory_feed.WINDOW_DAYS)
    ors: List[Dict[str, Any]] = []
    for key in domains:
        spec = DOMAINS.get(key) or {}
        if spec.get("legacy"):
            ors.append({"role_family": {"$in": sorted(spec["legacy"])}})
        if spec.get("tech"):
            ors.append({"tech.role_families": {"$in": sorted(spec["tech"])}})
        if spec.get("title"):
            ors.append({"title_raw": {"$regex": spec["title"], "$options": "i"}})
    if not ors:
        return 0
    return _db().job_postings.count_documents({
        "status": "active",
        "$and": [{"$or": ors},
                 {"$or": [{"posted_at": {"$gte": since}}, {"posted_at": None}]}],
    })


def _domain_query(domains: List[str]) -> Optional[Dict[str, Any]]:
    ors: List[Dict[str, Any]] = []
    for key in domains:
        spec = DOMAINS.get(key) or {}
        if spec.get("legacy"):
            ors.append({"role_family": {"$in": sorted(spec["legacy"])}})
        if spec.get("tech"):
            ors.append({"tech.role_families": {"$in": sorted(spec["tech"])}})
        if spec.get("title"):
            ors.append({"title_raw": {"$regex": spec["title"], "$options": "i"}})
    return {"$or": ors} if ors else None


def missed_in_domain(domains: List[str], levels: List[str], board: Dict[str, Any],
                     match_titles: List[str], days: int,
                     cap: int = 5000, *, prefs: Optional[Dict[str, Any]] = None,
                     own_families: Sequence[str] = ()) -> Dict[str, Any]:
    """G2, per user: the in-domain postings they SHOULD be able to reach and
    are not shown.

    The population is what the board itself promises: active, US
    (`us_geo.keep_us_posting`, the board's own rule), at one of the user's
    levels (unknown always passes, as on the board), and new in the last
    `days` - posted, or first seen when undated. Every held row is judged
    with the same `judge` the top-N verdicts use, so "in domain" means one
    thing in both numbers.

    A row counts as shown when its job id OR its dedupe group is on the board:
    collapsing a duplicate is not missing it. A miss is attributed to the
    search when no match title occurs in its title (a vocabulary gap) and to
    serving otherwise (ranking window, scoring, eligibility). Bounded by `cap`
    and says so when it is hit.

    A miss the user's employer preference hides ON PURPOSE (a talent
    marketplace, a staffing agency) is not a miss: it is counted as
    `policy_hidden` and leaves the population. The first scorecard after the
    policy shipped charged Jobgether reposts as misses for every user.

    `own_recall` is the same measurement over postings whose role family is
    one of the candidate's own (`own_families`). The domain is broad on
    purpose - "software" holds aerospace Systems Engineers - so `recall` is a
    floor, and a vocabulary gap shows up as low `own_recall`, not low recall.
    """
    from services import us_geo

    base = _domain_query(domains)
    if base is None:
        return {"population": 0}
    since = datetime.now(timezone.utc) - timedelta(days=days)
    q = {"status": "active", "$and": [base, {"$or": [
        {"posted_at": {"$gte": since}},
        {"posted_at": None, "first_seen_at": {"$gte": since}}]}]}
    proj = {"_id": 0, "job_id": 1, "title_raw": 1, "employer_name": 1, "locations": 1,
            "seniority": 1, "dedupe_group_id": 1, "country_code": 1}
    shown_ids = {str(r.get("job_id") or "") for r in board["rows"]}
    shown_groups = {str(r.get("dedupe_group_id") or "") for r in board["rows"]} - {""}
    patterns = [re.compile(r"(?<![A-Za-z0-9_])" + re.escape(t) + r"(?![A-Za-z0-9_])",
                           re.IGNORECASE) for t in match_titles if t]
    own = set(own_families or ())
    judged: List[Dict[str, Any]] = []
    scanned = 0
    for doc in _db().job_postings.find(q, proj).limit(cap):
        scanned += 1
        if not us_geo.keep_us_posting(doc) or not _in_levels(doc, levels):
            continue
        title = str(doc.get("title_raw") or "")
        if judge(title, domains) != "on":
            continue
        is_shown = (str(doc.get("job_id") or "") in shown_ids
                    or str(doc.get("dedupe_group_id") or "") in shown_groups)
        judged.append({
            "job_id": str(doc.get("job_id") or ""), "title": title,
            "company": doc.get("employer_name"), "shown": is_shown,
            "own": fit_score.classify_role(title) in own,
            "why": (None if is_shown else "searched_not_served"
                    if any(p.search(title) for p in patterns) else "not_searched"),
        })
    hidden = _policy_hidden([j["job_id"] for j in judged if not j["shown"]], prefs)
    population = shown = own_pop = own_shown = 0
    reasons: Dict[str, int] = {}
    sample: List[Dict[str, Any]] = []
    for j in judged:
        if j["job_id"] in hidden:
            continue
        population += 1
        own_pop += j["own"]
        if j["shown"]:
            shown += 1
            own_shown += j["own"]
            continue
        reasons[j["why"]] = reasons.get(j["why"], 0) + 1
        # Own-family misses first: they are the ones a fix is owed for.
        if len(sample) < 12 and (j["own"] or not own):
            sample.append({"title": j["title"][:90], "company": j["company"],
                           "why": j["why"], "own": j["own"]})
    return {
        "days": days,
        "population": population,
        "shown": shown,
        "recall": round(shown / population, 3) if population else None,
        "own_population": own_pop,
        "own_recall": round(own_shown / own_pop, 3) if own_pop else None,
        "policy_hidden": len(hidden),
        "missed_by_reason": reasons,
        "missed_sample": sample,
        "truncated": scanned >= cap,
    }


def _policy_hidden(job_ids: List[str], prefs: Optional[Dict[str, Any]]) -> set:
    """The job ids among these that the employer preference hides - the same
    `employer_policy.apply` every served list runs, over the full documents."""
    from services import employer_policy

    if not job_ids:
        return set()
    out: set = set()
    for i in range(0, len(job_ids), 500):
        rows = list(_db().job_postings.find({"job_id": {"$in": job_ids[i:i + 500]}}))
        kept, _block = employer_policy.apply(rows, prefs)
        kept_ids = {str(r.get("job_id") or "") for r in kept}
        out |= {str(r.get("job_id") or "") for r in rows} - kept_ids
    return out


def auto_domains(email: str) -> List[str]:
    """Fallback when no label is given: the resume's own taxonomy families."""
    fams: set = set()
    for entry in fit_score.load_profiles_for_user(email) or []:
        prof = entry.get("profile") or {}
        fams |= set((prof.get("tech") or {}).get("role_families") or [])
        fams |= set(prof.get("families") or [])
    out = [k for k, spec in DOMAINS.items()
           if fams & (set(spec.get("tech") or ()) | set(spec.get("legacy") or ()))]
    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _user_prefs(email: str) -> Dict[str, Any]:
    from services import employer_policy

    return employer_policy.get_prefs(email)


def audit_user(email: str, domains: List[str], top: int, show: int) -> Dict[str, Any]:
    seed = board_titles(email)
    view = experience_view.for_user(email)
    levels = list(view.get("levels") or [])
    profiles = fit_score.load_profiles_for_user(email) or []
    resume_families = sorted({f for e in profiles
                              for f in ((e.get("profile") or {}).get("families") or set())})
    resume_tech = sorted({f for e in profiles
                          for f in (((e.get("profile") or {}).get("tech") or {})
                                    .get("role_families") or [])})
    board = replay_board(email, seed["match_titles"], levels)
    rows = board["rows"][:top]
    verdicts = [judge(str(r.get("title") or ""), domains) for r in rows]

    def _share(n: int, verdict: str) -> Optional[float]:
        head = verdicts[:n]
        return round(head.count(verdict) / len(head), 3) if head else None

    return {
        "user": _mask(email),
        "domains": domains,
        "levels": levels,
        "resume_families": resume_families,
        "resume_tech_families": resume_tech,
        "match_titles": seed["match_titles"],
        "resume_titles": seed["resume_titles"],
        "tab_counts": board["tab_counts"],
        "shown_total": len(board["rows"]),
        "on_top25": _share(25, "on"),
        "off_top25": _share(25, "off"),
        "on_top100": _share(100, "on"),
        "off_top10": verdicts[:10].count("off"),
        "held_in_domain": held_in_domain(domains),
        "missed": missed_in_domain(domains, levels, board, seed["match_titles"],
                                   days=RECALL_DAYS, prefs=_user_prefs(email),
                                   own_families=resume_families),
        "sample": [{"title": r.get("title"), "company": r.get("company"),
                    "score": r.get("match_score"), "verdict": v}
                   for r, v in list(zip(rows, verdicts))[:show]],
    }


def _markdown(run: str, results: List[Dict[str, Any]]) -> str:
    lines = [f"# Feed fidelity scorecard {run}", "",
             "Read-only replay of the inventory-served For-you tabs. "
             "on/off = share of shown rows in / out of the user's domain.", "",
             "| user | domains | resume fams | shown | on@25 | off@25 | off@10 | on@100 | held in-domain | recall (new, own levels) | own-family recall | policy hidden | missed: not searched / not served |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in results:
        if r.get("error"):
            lines.append(f"| {r['user']} | error: {r['error']} |||||||")
            continue
        lines.append(
            f"| {r['user']} | {','.join(r['domains']) or '-'} | "
            f"{','.join(r['resume_families']) or '-'} | {r['shown_total']} | "
            f"{r['on_top25']} | {r['off_top25']} | {r['off_top10']} | "
            f"{r['on_top100']} | {r['held_in_domain']} | "
            f"{(r.get('missed') or {}).get('recall')} of {(r.get('missed') or {}).get('population')} | "
            f"{(r.get('missed') or {}).get('own_recall')} of {(r.get('missed') or {}).get('own_population')} | "
            f"{(r.get('missed') or {}).get('policy_hidden')} | "
            f"{((r.get('missed') or {}).get('missed_by_reason') or {}).get('not_searched', 0)} / "
            f"{((r.get('missed') or {}).get('missed_by_reason') or {}).get('searched_not_served', 0)} |")
    for r in results:
        if r.get("error"):
            continue
        lines += ["", f"## {r['user']} ({', '.join(r['domains']) or 'no domain'})",
                  f"- match_titles: {r['match_titles']}",
                  f"- levels: {r['levels'] or 'all'} · tabs: {r['tab_counts']}"]
        for s in r["sample"]:
            lines.append(f"  - [{s['verdict']}] {s['score']} · {s['title']} · {s['company']}")
        missed = r.get("missed") or {}
        if missed.get("missed_sample"):
            lines.append(f"- missed (new in {missed.get('days')}d, own levels, US):")
            for m in missed["missed_sample"]:
                own_tag = " (own family)" if m.get("own") else ""
                lines.append(f"  - [{m['why']}{own_tag}] {m['title']} · {m['company']}")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--users", default="", help="comma-separated email prefixes")
    ap.add_argument("--labels", default=str(OUT_DIR / "user_domains.json"))
    ap.add_argument("--top", type=int, default=100)
    ap.add_argument("--show", type=int, default=10)
    args = ap.parse_args()

    labels: Dict[str, List[str]] = {}
    if Path(args.labels).exists():
        labels = json.loads(Path(args.labels).read_text())
    prefixes = [p.strip().lower() for p in args.users.split(",") if p.strip()]

    results: List[Dict[str, Any]] = []
    for user in _db().users.find({}, {"email": 1}).sort("created_at", 1):
        email = str(user.get("email") or "").lower()
        if prefixes and not any(email.startswith(p) for p in prefixes):
            continue
        if not resume_access.list_parsed_assets(email):
            continue
        domains = labels.get(email) or auto_domains(email)
        try:
            results.append(audit_user(email, domains, args.top, args.show))
        except Exception as exc:
            results.append({"user": _mask(email), "error": f"{type(exc).__name__}: {exc}"})
        r = results[-1]
        print(f"{r['user']:10} {','.join(r.get('domains') or []):24} "
              f"on@25={r.get('on_top25')} off@10={r.get('off_top10')} "
              f"held={r.get('held_in_domain')} "
              f"recall={(r.get('missed') or {}).get('recall')}/"
              f"{(r.get('missed') or {}).get('population')} "
              f"own={(r.get('missed') or {}).get('own_recall')}/"
              f"{(r.get('missed') or {}).get('own_population')} "
              f"hidden={(r.get('missed') or {}).get('policy_hidden')} {r.get('error') or ''}", flush=True)

    run = _utc_run_id()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / f"scorecard-{run}.json").write_text(json.dumps(results, indent=1, default=str))
    (OUT_DIR / f"scorecard-{run}.md").write_text(_markdown(run, results))
    print(f"\nwrote {OUT_DIR / f'scorecard-{run}.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
