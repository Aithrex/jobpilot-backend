#!/usr/bin/env python3
"""READ-ONLY feed legitimacy verification (Aithrex docs/prompts/feed-legitimacy-verification.md).

For every user with a parsed resume: replay the two inventory tabs exactly as
production serves them, take the top 25 cards, and judge each card on
G1 posted date, G2 skills, G3 experience, G4 company, G5 link, G6 field.
The DB is read-only (the audit module makes every pymongo write raise); the
only network traffic is one polite GET per job page.

Usage (from jobpilot-backend):  python3 scripts/feed_legitimacy_check.py [--users a,b] [--top 25]
Writes .agents/tasks/feed-fidelity/legitimacy_<run>.json and prints a per-user table.
First run 2026-10-25: 643 of 675 top-25 cards clean across 27 accounts.
"""
import argparse
import collections
import concurrent.futures as cf
import html
import importlib.util
import json
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path.cwd()
HERE = ROOT / ".agents" / "tasks" / "feed-fidelity"
spec = importlib.util.spec_from_file_location("audit", ROOT / "scripts" / "user_feed_fidelity_audit.py")
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)

import requests  # noqa: E402
from services import employer_business_model as ebm  # noqa: E402
from services import experience_view, fit_score, resume_access  # noqa: E402

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
CLOSED_RX = re.compile(
    r"(this (?:position|job|role|posting|requisition) (?:is|has been) (?:closed|filled|no longer "
    r"(?:available|open|active|accepting))|no longer (?:accepting|taking) applications|job (?:is )?no "
    r"longer available|(?:job|posting|position) (?:has )?expired|position is no longer available|"
    r"the job you(?:'| a)re (?:looking for|trying to apply for) (?:is no longer|has been|may have)|"
    r"job you are looking for may have closed|\"isClosed\"\s*:\s*true)", re.I)
UNTIL_RX = re.compile(r"until (?:the )?(?:position|role|job) (?:is|has been) (?:filled|closed)|until filled", re.I)
JSONLD_DATE = re.compile(r"\"datePosted\"\s*:\s*\"([^\"]{8,30})\"")
VISIBLE_DATE = re.compile(
    r"(?:date posted|posted on|posting date)\s*[:\-]?\s*((?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)"
    r"[a-z]*\.?\s+\d{1,2},?\s+\d{4}|\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{4})", re.I)
SENIOR = re.compile(r"\b(senior|sr\.?|staff|principal|lead|director|head of|vp|manager)\b", re.I)
INTERN = re.compile(r"\b(intern|internship|co-?op)\b", re.I)


def parse_date(s):
    s = s.strip().replace("Sept", "Sep")
    for fmt in ("%B %d, %Y", "%B %d %Y", "%b %d, %Y", "%b %d %Y", "%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    try:
        d = datetime.fromisoformat(s[:19].replace("Z", ""))
        return d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


_locks = collections.defaultdict(threading.Lock)
_last = collections.defaultdict(float)


def check_link(url):
    host = urlsplit(url).hostname or ""
    with _locks[host]:
        wait = _last[host] + 1.5 - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        try:
            r = requests.get(url, headers={"User-Agent": UA}, timeout=15, allow_redirects=True)
        except Exception as exc:  # noqa: BLE001
            return {"verdict": "unknown", "why": type(exc).__name__}
        finally:
            _last[host] = time.monotonic()
    body = r.text[:600_000]
    text = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", body)))
    out = {"status": r.status_code, "final": r.url}
    if r.status_code in (404, 410):
        return {**out, "verdict": "dead"}
    if r.status_code in (401, 403, 429, 503):
        return {**out, "verdict": "unknown", "why": f"blocked_{r.status_code}"}
    m = CLOSED_RX.search(UNTIL_RX.sub(" ", text))
    if m:
        return {**out, "verdict": "closed", "why": m.group(0)[:60]}
    d = JSONLD_DATE.search(body) or VISIBLE_DATE.search(text)
    if d:
        pd = parse_date(d.group(1))
        if pd:
            out["page_posted"] = pd.date().isoformat()
    fin = urlsplit(r.url).path.rstrip("/")
    if fin in ("", "/careers", "/jobs", "/search", "/careers/search") and urlsplit(url).path.rstrip("/") != fin:
        return {**out, "verdict": "redirected_away"}
    return {**out, "verdict": "ok"}


def resume_skills(email):
    out = set()
    for e in fit_score.load_profiles_for_user(email) or []:
        p = e.get("profile") or {}
        out |= {str(s).lower() for s in (p.get("listed_skills") or set())}
        out |= {str(s).lower() for s in (p.get("experience_skills") or set())}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--users", default="")
    ap.add_argument("--top", type=int, default=25)
    args = ap.parse_args()
    prefixes = [p.strip().lower() for p in args.users.split(",") if p.strip()]
    labels = {}
    lp = audit.OUT_DIR / "user_domains.json"
    if lp.exists():
        labels = json.loads(lp.read_text())
    db = audit._db()
    now = datetime.now(timezone.utc)
    report = []
    for u in db.users.find({}, {"email": 1}).sort("created_at", 1):
        email = str(u.get("email") or "").lower()
        if prefixes and not any(email.startswith(p) for p in prefixes):
            continue
        if not resume_access.list_parsed_assets(email):
            continue
        domains = labels.get(email) or audit.auto_domains(email)
        seed = audit.board_titles(email)
        levels = list((experience_view.for_user(email) or {}).get("levels") or [])
        board = audit.replay_board(email, seed["match_titles"], levels)
        rows = board["rows"][: args.top]
        docs = {d["job_id"]: d for d in db.job_postings.find(
            {"job_id": {"$in": [r.get("job_id") for r in rows]}},
            {"_id": 0, "job_id": 1, "requirements": 1, "seniority": 1, "posted_at": 1,
             "first_seen_at": 1, "employer_name": 1, "source": 1, "ats": 1})}
        skills = resume_skills(email)
        links = {}
        urls = list({r.get("apply_link") or r.get("url") for r in rows if r.get("apply_link") or r.get("url")})
        with cf.ThreadPoolExecutor(8) as pool:
            for url, res in zip(urls, pool.map(check_link, urls)):
                links[url] = res
        cards = []
        for r in rows:
            d = docs.get(r.get("job_id")) or {}
            req = d.get("requirements") or {}
            need = {str(s).lower() for s in (req.get("required_skills") or []) + (req.get("preferred_skills") or [])}
            prof = r.get("employer_profile") or {}
            kind = prof.get("kind") or "unknown"
            title = str(r.get("title") or "")
            lk = links.get(r.get("apply_link") or r.get("url")) or {}
            flags = []
            # G1 date
            page = lk.get("page_posted")
            if page and r.get("days_ago") is not None:
                real = (now.date() - datetime.fromisoformat(page).date()).days
                if abs(real - r["days_ago"]) > 2:
                    flags.append(f"G1 date: card {r['days_ago']}d, page {real}d")
                if real > 30:
                    flags.append("G1 older than 30 days")
            # G2 skills
            overlap = len(need & skills)
            if need and overlap == 0:
                flags.append("G2 no skill overlap")
            # G3 experience
            sen = str(d.get("seniority") or r.get("seniority") or "")
            if set(levels) <= {"intern", "entry"} and (sen in ("senior", "staff", "principal", "director")
                                                      or SENIOR.search(title)):
                flags.append(f"G3 too senior ({sen or 'title'})")
            if levels and not set(levels) & {"intern", "entry"} and INTERN.search(title):
                flags.append("G3 internship for experienced user")
            # G4 company
            name = r.get("company") or d.get("employer_name") or ""
            if kind in ("staffing", "consultancy") and not ebm.is_recognized(name) \
                    and prof.get("subtype") != "engineering_design_services":
                flags.append(f"G4 {kind}")
            if kind == "talent_marketplace":
                flags.append("G4 marketplace")
            # G5 link
            if lk.get("verdict") in ("dead", "closed", "redirected_away"):
                flags.append(f"G5 link {lk.get('verdict')}")
            # G6 field
            verdict = audit.judge(title, domains)
            if verdict == "off":
                flags.append("G6 off-field")
            cards.append({"job_id": r.get("job_id"), "title": title, "company": name,
                          "score": r.get("match_score"),
                          "days_ago": r.get("days_ago"), "seniority": sen, "kind": kind,
                          "skills_overlap": overlap, "skills_needed": len(need),
                          "link": lk.get("verdict"), "page_posted": page,
                          "url": r.get("apply_link") or r.get("url"), "flags": flags})
        g = collections.Counter(f.split(" ")[0] for c in cards for f in c["flags"])
        rec = {"user": audit._mask(email), "domains": domains, "levels": levels,
               "cards": len(cards), "clean": sum(1 for c in cards if not c["flags"]),
               "flags": dict(g), "resume_skills": len(skills), "detail": cards}
        report.append(rec)
        print(f"{rec['user']:10} {','.join(domains)[:18]:18} {','.join(levels)[:14]:14} "
              f"clean {rec['clean']:2}/{rec['cards']:2}  {dict(g)}", flush=True)
    run = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = HERE / f"legitimacy_{run}.json"
    out.write_text(json.dumps(report, indent=1, default=str))
    tot = collections.Counter()
    for rec in report:
        tot.update(rec["flags"])
    print("\nALL USERS", dict(tot), "| cards", sum(r["cards"] for r in report),
          "| clean", sum(r["clean"] for r in report))
    print("wrote", out)


if __name__ == "__main__":
    main()
