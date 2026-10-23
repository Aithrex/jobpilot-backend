"""Read what an UNCLASSIFIED employer's own website says it is.

Dry run by default; `--apply` writes `employers.site_self_description`
({kind, phrase, url, checked_at}), which `employer_business_model.
classify_employer` reads as the `site_self_description` basis (above the name
rule, below a registry category). Then run
`scripts/backfill_employer_business_model.py --apply` to restamp the models.

Why: 86% of employers (20,457 of 23,846 on 2026-10-23) have no business-model
evidence at all, and every small IT consultancy the 2026-10-22 link scan found
on the board was among them. Their own sites say it plainly - VBridgeIT "is a
leading Technical staffing and recruitment agency", INTELLYK "is one of the
fastest growing IT consulting and technology professional services firm".

Only SELF-descriptions count ("<name> is a ... staffing agency", "we are an IT
consulting firm") and a few unmistakable trade phrases (bench sales, H-1B
transfers, C2C). A product company that "partners with consulting firms" is
not one. Recognized firms (Insight Global, Deloitte, ...) are skipped - they
are shown by default whatever they are.

Network: the employer's own homepage and at most two about pages, one host at
a time, SSRF-checked, polite spacing. Run it ALONE (no other network pass).
Rollback: `--unset --apply` removes every `site_self_description`.

Usage:  PYTHONPATH=shared/python python3 scripts/classify_employer_sites.py
            [--limit 3000] [--workers 8] [--apply] [--unset]
"""
from __future__ import annotations

import argparse
import collections
import concurrent.futures as cf
import html
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "shared" / "python"))

_SELF = r"(?:\bwe\s+are|\bwe'?re|\bis|\bare)\s+(?:a|an|one\s+of\s+the)\s+(?:[\w&/,.'-]+\s+){0,7}?"
_STAFFING_CORE = (r"(?:staffing|recruit(?:ment|ing)|talent\s+acquisition|placement)"
                  r"(?:\s+[\w&/,-]+){0,4}?\s+(?:agency|firm|company|partner|provider|services|solutions)")
_CONSULT_CORE = (r"(?:it\s+consult\w*|technology\s+consult\w*|consulting|consultancy|it\s+services|"
                 r"staff\s+augmentation)(?:\s+[\w&/,-]+){0,4}?\s+"
                 r"(?:firm|company|provider|partner|organization|organisation)")
STAFFING_RE = re.compile(_SELF + _STAFFING_CORE, re.IGNORECASE)
CONSULT_RE = re.compile(_SELF + _CONSULT_CORE, re.IGNORECASE)
PROVIDER_RE = re.compile(r"\b(?:leading|premier|trusted)?\s*provider\s+of\s+(?:it|technology)\s+"
                         r"(?:staffing|consulting|services)\b", re.IGNORECASE)
TRADE_RE = re.compile(r"\b(bench\s+sales|h-?1b\s+transfers?|opt\s*/\s*cpt|cpt\s*/\s*opt|"
                      r"corp[- ]to[- ]corp|w2\s*/\s*c2c|c2c\s+(?:and|/)\s+w2|"
                      r"\d+\+?\s+dedicated developers|hire dedicated developers|"
                      r"it solutions and services[^.]{0,120}\bstaffing)\b", re.IGNORECASE)
#: Staffing offered as SERVICE LINES ("IT Staffing", "Engineering Staffing",
#: "Healthcare Staffing" in the menu - INTELLYK's site). Two distinct ones are a
#: staffing business; a product company does not sell staffing lines.
SERVICE_LINE_RE = re.compile(
    r"\b((?:it|engineering|healthcare|technical|technology|contract|permanent|direct[- ]hire|"
    r"light industrial|finance|accounting(?: and finance)?|professional|executive|light industrial) "
    r"staffing|staffing solutions?|staff augmentation|contract[- ]to[- ]hire)\b", re.IGNORECASE)
ABOUT_PATHS = ("/about", "/about-us", "/company")
UA = "Mozilla/5.0 (compatible; AspirelyEmployerCheck/1.0; +https://aspirely.me)"


def page_text(body: str) -> str:
    body = re.sub(r"<script.*?</script>|<style.*?</style>|<noscript.*?</noscript>", " ",
                  body, flags=re.S | re.I)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", body)))


def read_text(text: str) -> Optional[Dict[str, str]]:
    """{kind, phrase} from one page's text, or None."""
    for kind, rx in (("staffing", STAFFING_RE), ("consultancy", CONSULT_RE),
                     ("consultancy", PROVIDER_RE), ("consultancy", TRADE_RE)):
        m = rx.search(text)
        if m:
            return {"kind": kind, "phrase": m.group(0).strip()[:200]}
    lines = {m.group(1).lower() for m in SERVICE_LINE_RE.finditer(text)}
    if len(lines) >= 2:
        return {"kind": "staffing", "phrase": "service lines: " + ", ".join(sorted(lines))[:180]}
    return None


def check_domain(domain: str, timeout: float) -> Dict[str, Any]:
    import requests
    from services import jd_fetcher

    for path in ("/", *ABOUT_PATHS):
        url = f"https://{domain}{path}"
        ok, _why = jd_fetcher.url_is_safe(url)
        if not ok:
            return {"status": "unsafe"}
        try:
            r = requests.get(url, headers={"User-Agent": UA}, timeout=timeout,
                             allow_redirects=True)
        except Exception as exc:  # noqa: BLE001
            if path == "/":
                return {"status": f"error:{type(exc).__name__}"}
            continue
        finally:
            time.sleep(1.0)
        if r.status_code != 200 or "html" not in r.headers.get("content-type", ""):
            continue
        hit = read_text(page_text(r.text[:800_000]))
        if hit:
            return {"status": "classified", **hit, "url": r.url}
    return {"status": "no_self_description"}


def candidates(db, limit: int) -> List[Dict[str, Any]]:
    from services import employer_business_model as ebm

    active = collections.Counter()
    for row in db.job_postings.aggregate([
            {"$match": {"status": "active", "canonical_employer_id": {"$exists": True}}},
            {"$group": {"_id": "$canonical_employer_id", "n": {"$sum": 1}}}]):
        active[row["_id"]] = row["n"]
    out = []
    for emp in db.employers.find(
            {"employer_id": {"$in": list(active)}, "canonical_domain": {"$nin": [None, ""]},
             "site_self_description": {"$exists": False}},
            {"_id": 0, "employer_id": 1, "display_name": 1, "canonical_domain": 1,
             "business_model": 1, "employer_categories": 1}):
        kind = (emp.get("business_model") or {}).get("kind") or "unknown"
        if kind != "unknown" or ebm.is_recognized(emp.get("display_name")):
            continue
        out.append({**emp, "active": active[emp["employer_id"]]})
    out.sort(key=lambda e: -e["active"])
    return out[:limit]


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--unset", action="store_true")
    ap.add_argument("--limit", type=int, default=3000)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--timeout", type=float, default=12)
    args = ap.parse_args(argv)
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except ImportError:  # pragma: no cover
        pass
    from utils.db_connect import DBConnect
    db = DBConnect().get_db()

    if args.unset:
        n = db.employers.count_documents({"site_self_description": {"$exists": True}})
        print(f"{'APPLY' if args.apply else 'dry-run'} unset: {n} employers")
        if args.apply:
            db.employers.update_many({"site_self_description": {"$exists": True}},
                                     {"$unset": {"site_self_description": ""}})
        return 0

    todo = candidates(db, args.limit)
    print(f"{len(todo)} unclassified employers with active postings and a domain", flush=True)
    stats = collections.Counter()
    found: List[Dict[str, Any]] = []
    with cf.ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futs = {pool.submit(check_domain, e["canonical_domain"], args.timeout): e for e in todo}
        for i, f in enumerate(cf.as_completed(futs), 1):
            emp = futs[f]
            res = f.result()
            stats[res["status"] if res["status"] != "classified" else res["kind"]] += 1
            if res["status"] == "classified":
                found.append({**emp, **res})
                print(f"  {res['kind']:11} {emp['active']:4} active  {emp['display_name'][:36]:36} "
                      f"| {res['phrase'][:90]}", flush=True)
                if args.apply:
                    db.employers.update_one(
                        {"employer_id": emp["employer_id"]},
                        {"$set": {"site_self_description": {
                            "kind": res["kind"], "phrase": res["phrase"], "url": res["url"],
                            "checked_at": datetime.now(timezone.utc)}}})
            if i % 100 == 0:
                print(f"  ... {i}/{len(todo)} {dict(stats)}", flush=True)
    print(json.dumps({"mode": "APPLY" if args.apply else "dry-run", "checked": len(todo),
                      "results": dict(stats),
                      "active_postings_reclassified": sum(e["active"] for e in found)}, indent=2))
    if not args.apply:
        print("DRY RUN - nothing written. Re-run with --apply, then "
              "scripts/backfill_employer_business_model.py --apply.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
