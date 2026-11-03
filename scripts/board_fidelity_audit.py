"""Read-only: does every user's For-you board list the jobs we crawl for them?

    docker exec -i <worker> python - < scripts/board_fidelity_audit.py
    AUDIT_EMAIL=a@b.c  one user (default: every user)
    AUDIT_DAYS=7       ground-truth window
    AUDIT_TZ=-300      viewer offset for "today" when the user never sent one
    AUDIT_JSON=/tmp/x.jsonl   one JSON line per user (optional)

For each user it replays both inventory tabs exactly as the board scan does,
then takes every ACTIVE posting of the window from EVERY crawled vendor that
matches the user's titles or the role families the board retrieves, keeps the
ones the default view should show (US, the user's levels, the user's employer
policy), and checks which reached the board - per vendor, with the reason for
each one that did not. A posting folded into another city's card or into a
duplicate group counts as listed.

Flags per user: NO_RESUME, NO_TITLES, NO_FAMILIES, THIN_TODAY, THIN_7D,
LOW_SCORES, LOW_COVERAGE, VENDOR_GAP, MISFILED, CAP_CUT, OFF_TARGET, SLOW.

READ-ONLY, enforced: every pymongo write method raises.
"""
from __future__ import annotations

import json
import os
import re
import statistics
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

# AUDIT_PYPATH replays a candidate build's code against live data (read-only),
# so a retrieval change is measured before it ships.
for candidate in reversed([os.environ.get("AUDIT_PYPATH") or "", "/app/shared/python",
                           os.path.join(os.getcwd(), "shared", "python")]):
    if candidate and os.path.isdir(candidate) and candidate not in sys.path:
        sys.path.insert(0, candidate)

import pymongo.collection as _pc  # noqa: E402

for _m in ("insert_one", "insert_many", "update_one", "update_many", "replace_one",
           "delete_one", "delete_many", "find_one_and_update", "find_one_and_replace",
           "find_one_and_delete", "bulk_write", "create_index", "create_indexes",
           "drop", "drop_index"):
    def _deny(self, *a, _m=_m, **k):
        raise RuntimeError(f"read-only audit: refused {_m} on {self.name}")
    setattr(_pc.Collection, _m, _deny)

from utils.db_connect import DBConnect  # noqa: E402
from services import (employer_policy as ep, experience_view, fit_score,  # noqa: E402
                      inventory_feed as feed, job_inventory_service as inv,
                      job_title_seed, resume_access, us_geo)
from services.experience_view import _VIEW_OF_BAND  # noqa: E402
from services.workday_jobs_service import _load_resume_profile  # noqa: E402

DAYS = int(os.environ.get("AUDIT_DAYS", "7"))
TZ = int(os.environ.get("AUDIT_TZ", "-300"))
ONLY = os.environ.get("AUDIT_EMAIL", "").strip().lower()
JSON_OUT = os.environ.get("AUDIT_JSON", "").strip()
db = DBConnect().get_db()
NOW = datetime.now(timezone.utc)
SENIORITY = re.compile(r"\b(sr|senior|jr|junior|lead|principal|staff|associate|entry|level|"
                       r"i{1,3}|iv|[1-4])\b\.?", re.I)
_HEAVY = {"description": 0, "description_html": 0, "raw": 0, "requirements": 0}


_GENERIC = {"engineer", "developer", "analyst", "manager", "scientist", "specialist",
            "consultant", "architect", "administrator", "intern", "associate", "lead"}


def phrase_of(t: str) -> str:
    """A title as the words a matching posting must contain, IN ORDER and
    adjacent, seniority words aside: "Senior Data Engineer II" -> "data engineer".
    Bag-of-words matching counted "On-Site Service Engineer - Data Center" as a
    Data Engineer role, which is not what anyone means by relevant."""
    t = SENIORITY.sub(" ", str(t or "").lower())
    words = re.findall(r"[a-z0-9+#]+", t)
    return " ".join(words)


def user_phrases(titles: List[str]) -> List[str]:
    out = []
    for t in titles:
        p = phrase_of(t)
        if p and (len(p.split()) > 1 or p not in _GENERIC) and p not in out:
            out.append(p)
    return out


def pct(a: int, b: int) -> str:
    return f"{(100 * a / b):.0f}%" if b else "n/a"


def audit_user(email: str) -> Dict[str, Any]:
    t_user = time.time()
    out: Dict[str, Any] = {"email": email, "flags": []}
    flags: List[str] = out["flags"]
    assets = resume_access.list_parsed_assets(email)
    out["resumes"] = len(assets)
    skills, years = _load_resume_profile(email)
    out["skills"], out["years"] = len(skills), years
    if not assets:
        flags.append("NO_RESUME")
    seed = job_title_seed.search_titles_view(email, assets, job_title_seed.load_title_seed(email))
    req = db.board_requests.find_one({"_id": email})
    if req:
        params = dict(req.get("params") or {})
        out["request"] = "board_requests"
    else:
        params = {"titles": seed.get("match_titles") or [], "location": "", "remote_only": False,
                  "us_only": True, "tz_offset_minutes": TZ}
        out["request"] = "title_seed"
    titles = [str(t) for t in params.get("titles") or [] if str(t).strip()]
    out["titles"] = titles
    if not titles:
        flags.append("NO_TITLES")
    levels = list((experience_view.for_user(email) or {}).get("levels") or [])
    out["levels"] = levels
    profile = feed._build_profile(email, skills, years)
    res_fams = sorted(feed._resume_families(profile))
    families = list(feed._role_families(titles, skills, res_fams) or [])
    out["resume_families"], out["families"] = res_fams, families
    if not families:
        flags.append("NO_FAMILIES")
    prefs = ep.get_prefs(email)
    tz = int(params.get("tz_offset_minutes") or TZ)

    # ---- the board, replayed
    board: Dict[str, Dict[str, Any]] = {}
    board_keys, out["tabs"] = set(), {}
    for tab in (feed.SOURCE_WORKDAY, feed.SOURCE_CAREER):
        t0 = time.time()
        res = feed.build_feed(tab, dict(params, user_email=email))
        dt = round(time.time() - t0, 1)
        if res is None:
            out["tabs"][tab] = {"deferred_to_crawl": True, "seconds": dt}
            continue
        rows = res.get("jobs") or []
        diag = res.get("diagnostics") or {}
        out["tabs"][tab] = {"cards": len(rows), "candidates": diag.get("candidates"),
                            "title_slice": diag.get("title_slice"), "seconds": dt}
        if dt > 45:
            flags.append(f"SLOW({tab} {dt}s)")
        for r in rows:
            board[str(r.get("job_id"))] = r
            board_keys.add(feed.dedupe_key_of(r))
    groups = {(inv.employer_id_of(r.get("company")), feed._title_key(r.get("title"))) for r in board.values()}

    def in_levels(row: Dict[str, Any]) -> bool:
        band = str(row.get("seniority") or "unknown").lower()
        return not levels or band in ("", "unknown") or _VIEW_OF_BAND.get(band, "mid") in levels

    def age(r):
        a = r.get("days_ago")
        return r.get("first_seen_days_ago") if a is None else a

    shown = [r for r in board.values() if in_levels(r) and r.get("sponsorship") != "refused"]
    win = {name: [r for r in shown if age(r) is None or age(r) <= hi]
           for name, hi in (("today", 0), ("3d", 3), ("7d", 7), ("30d", 30))}
    win["today"] = [r for r in shown if age(r) in (0, None)]
    s7 = [r.get("match_score") or 0 for r in win["7d"]]
    out["shown"] = {k: len(v) for k, v in win.items()}
    out["scores_7d"] = {"median": statistics.median(s7) if s7 else None,
                        ">=82": sum(x >= 82 for x in s7), ">=70": sum(x >= 70 for x in s7),
                        "unscored": sum(x == 0 for x in s7)}
    out["vendors_on_board"] = dict(Counter(r.get("ats") or r.get("source") for r in shown).most_common(12))
    if out["shown"]["today"] < 20:
        flags.append("THIN_TODAY")
    if out["shown"]["7d"] < 100:
        flags.append("THIN_7D")
    if assets and out["scores_7d"][">=70"] < 20:
        flags.append("LOW_SCORES")
    top = sorted(win["7d"], key=lambda r: -(r.get("match_score") or 0))[:50]
    fam_set = set(families)
    off = [r for r in top if fam_set and (fit_score.classify_role(r.get("title") or "") or "")
           not in fam_set | {""}]
    out["off_target_top50"] = len(off)
    out["off_target_samples"] = [f"{r.get('title')} | {r.get('company')}" for r in off[:4]]
    if len(off) >= 10:
        flags.append("OFF_TARGET")

    # ---- ground truth: every crawled vendor, the window
    phrases = user_phrases(titles)
    since = NOW - timedelta(days=DAYS)
    gap = r"(?:[^a-z0-9+#]+(?:" + SENIORITY.pattern.replace(r"\b", "") + r")?)*[^a-z0-9+#]+"
    title_rx = (r"(?<![a-z0-9])(?:" + "|".join(gap.join(re.escape(w) for w in p.split())
                                               for p in phrases) + r")(?![a-z0-9])") if phrases else ""
    match_or: List[Dict[str, Any]] = (
        [{"title_raw": {"$regex": title_rx, "$options": "i"}}] if title_rx else [])
    if fam_set:
        match_or.append({"role_family": {"$in": sorted(fam_set)}})
    if not match_or:
        out["seconds"] = round(time.time() - t_user, 1)
        return out
    t_q = time.time()
    docs = list(db.job_postings.find(
        {"status": "active",
         "$and": [{"$or": [{"posted_at": {"$gte": since}},
                           {"posted_at": None, "first_seen_at": {"$gte": since}}]},
                  {"$or": match_or}]}, _HEAVY))
    out["query_seconds"] = round(time.time() - t_q, 1)
    phrase_res = [re.compile(rf"(?<![a-z0-9]){re.escape(p)}(?![a-z0-9])") for p in phrases]

    def title_hit(d) -> bool:
        norm = phrase_of(d.get("title_raw") or "")
        return any(rx.search(norm) for rx in phrase_res)

    allowed = {id(d) for d in ep.apply(docs, prefs)[0]}
    gone = Counter()                       # not something the default view shows
    elig = Counter()                       # by vendor: (title-matched) eligible
    covered = Counter()
    elig_fam = covered_fam = 0             # family-only matches (broad pool)
    today_elig = today_cov = 0
    miss: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    rendered = []
    for d in docs:
        row = feed._row_from_posting(d, tz_offset=tz)
        if row is None:
            gone["unrenderable"] += 1
            continue
        rendered.append((d, row))
    try:  # the board shows "JPMorgan Chase", not the legal entity on the requisition
        from services import employer_display
        employer_display.apply_display_names([r for _, r in rendered])
    except Exception:
        pass
    for d, row in rendered:
        if params.get("us_only", True) and not us_geo.keep_us_row(row):
            gone["not_us"] += 1
            continue
        if id(d) not in allowed:
            gone["employer_policy"] += 1
            continue
        if not in_levels(row):
            gone["other_levels"] += 1
            continue
        hit = title_hit(d)
        listed = (str(d.get("job_id")) in board or feed.dedupe_key_of(row) in board_keys
                  or (inv.employer_id_of(row.get("company")), feed._title_key(row.get("title"))) in groups)
        vendor = d.get("ats") or d.get("source") or "?"
        if hit:
            elig[vendor] += 1
            covered[vendor] += listed
            if age(row) == 0:
                today_elig += 1
                today_cov += listed
        else:
            elig_fam += 1
            covered_fam += listed
        if listed or not hit:
            continue
        live = fit_score.classify_role(d.get("title_raw") or "")
        if fam_set and d.get("role_family") not in fam_set:
            why = f"filed_as:{d.get('role_family')}"
        elif feed._contradicts_its_family(d):
            why = "non_tech_by_taxonomy"
        elif fam_set and live is not None and live not in fam_set:
            why = f"title_reads:{live}"
        else:
            why = "cut(time_slice_or_cap)"
        miss[why].append(d)
    te, tc = sum(elig.values()), sum(covered.values())
    out["truth"] = {"docs": len(docs), "excluded": dict(gone), "title_eligible": te, "title_listed": tc,
                    "title_coverage": pct(tc, te), "family_only_eligible": elig_fam,
                    "family_only_listed": covered_fam, "family_only_coverage": pct(covered_fam, elig_fam),
                    "today_eligible": today_elig, "today_listed": today_cov,
                    "today_coverage": pct(today_cov, today_elig)}
    out["by_vendor"] = {v: f"{covered[v]}/{n} {pct(covered[v], n)}" for v, n in elig.most_common()}
    out["missing"] = {k: len(v) for k, v in sorted(miss.items(), key=lambda kv: -len(kv[1]))}
    out["missing_samples"] = {k: [f"{d.get('title_raw')} | {d.get('employer_name')} | {d.get('ats')}"
                                  for d in v[:5]] for k, v in list(miss.items())}
    if te >= 20 and tc / te < 0.8:
        flags.append("LOW_COVERAGE")
    gaps = [v for v, n in elig.items() if n >= 30 and covered[v] / n < 0.6]
    if gaps:
        flags.append("VENDOR_GAP(" + ",".join(gaps) + ")")
    misfiled = sum(len(v) for k, v in miss.items() if k.startswith("filed_as:"))
    if misfiled >= 20:
        flags.append(f"MISFILED({misfiled})")
    cut = len(miss.get("cut(time_slice_or_cap)", []))
    if cut >= 50:
        flags.append(f"CAP_CUT({cut})")
    out["seconds"] = round(time.time() - t_user, 1)
    return out


def show(r: Dict[str, Any]) -> None:
    print(f"\n=== {r['email']}  [{r.get('seconds')}s]  flags: {' '.join(r['flags']) or 'none'}")
    print(f"  resumes={r['resumes']} skills={r['skills']} years={r['years']} levels={r['levels']} "
          f"request={r['request']} titles={len(r['titles'])}: {r['titles'][:8]}")
    print(f"  families: board={r['families']} resume={r['resume_families']}")
    print(f"  tabs: {r.get('tabs')}")
    print(f"  shown by default: {r.get('shown')}  scores(7d): {r.get('scores_7d')}")
    print(f"  vendors on board: {r.get('vendors_on_board')}")
    if r.get("off_target_top50"):
        print(f"  off-target in top 50: {r['off_target_top50']}  e.g. {r['off_target_samples']}")
    if "truth" in r:
        print(f"  truth: {r['truth']}")
        print(f"  title coverage by vendor: {r['by_vendor']}")
        print(f"  missing (title-matched, eligible): {r['missing']}")
        for k, v in r["missing_samples"].items():
            print(f"    {k}: {v[:3]}")


def main() -> None:
    q = {"email": {"$regex": f"^{re.escape(ONLY)}$", "$options": "i"}} if ONLY else {}
    emails = [u["email"] for u in db.users.find(q, {"email": 1}).sort("last_login", -1) if u.get("email")]
    print(f"auditing {len(emails)} users, window {DAYS}d, at {NOW.isoformat()}")
    results, fh = [], (open(JSON_OUT, "a") if JSON_OUT else None)
    for e in emails:
        try:
            r = audit_user(e)
        except Exception as exc:  # one bad account must not end the audit
            r = {"email": e, "flags": [f"AUDIT_ERROR:{type(exc).__name__}:{str(exc)[:120]}"]}
            print(f"\n=== {e}  ERROR {r['flags'][0]}")
        else:
            show(r)
        results.append(r)
        if fh:
            fh.write(json.dumps(r, default=str) + "\n")
            fh.flush()
        sys.stdout.flush()
    print("\n=== SUMMARY")
    flag_counts = Counter(re.sub(r"\(.*", "", f) for r in results for f in r["flags"])
    print("flags across users:", dict(flag_counts.most_common()))
    tot_e = sum((r.get("truth") or {}).get("title_eligible", 0) for r in results)
    tot_c = sum((r.get("truth") or {}).get("title_listed", 0) for r in results)
    print(f"title-matched eligible postings listed, all users: {tot_c}/{tot_e} {pct(tot_c, tot_e)}")
    vend = defaultdict(lambda: [0, 0])
    for r in results:
        for v, s in (r.get("by_vendor") or {}).items():
            a, b = s.split()[0].split("/")
            vend[v][0] += int(a)
            vend[v][1] += int(b)
    print("by vendor, all users:", {v: f"{a}/{b} {pct(a, b)}" for v, (a, b) in
                                    sorted(vend.items(), key=lambda kv: -kv[1][1])})
    reasons = Counter()
    for r in results:
        for k, n in (r.get("missing") or {}).items():
            reasons[re.sub(r":.*", "", k) if not k.startswith("filed_as") else k] += n
    print("why title-matched eligible postings were missing, all users:", dict(reasons.most_common(15)))


if __name__ == "__main__" or True:
    main()
