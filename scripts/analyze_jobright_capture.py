"""Offline analysis of the 2026-10-21 Jobright / LinkedIn capture.

Reads ONLY the captured JSON files (no network, no database) and writes a
markdown report that quantifies what the captured labels and links support -
nothing more. The capture is ONE user's filtered recommendation feed on one
day plus one LinkedIn query; it is competitor evidence for evaluating
JobPilot, never JobPilot inventory, and it establishes no fraud, no hiring
intent and nothing about anyone's revenue.

Usage:
    python scripts/analyze_jobright_capture.py \\
        --capture-dir ../docs/audit/jobright-2026-10-21 \\
        --out docs/audit/jobright-capture-analysis.md
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "shared" / "python"))

from services import posting_identity as pid  # noqa: E402  (pure, no DB)

ANALYSIS_VERSION = "capture-analysis-1.0.0"

#: The service-firm cards the brief names, matched on the DISPLAYED company
#: (case-insensitive prefix). Expected counts from the brief are printed beside
#: the measured ones so a drift in the capture file is visible.
NAMED_SERVICE_FIRMS: Tuple[Tuple[str, str, Optional[int]], ...] = (
    ("Capgemini", "capgemini", 9),
    ("Tata Consultancy Services", "tata consultancy", 1),
    ("UST", "ust", 2),
    ("GBIT (Global Bridge InfoTech)", "gbit", 1),
    ("Blueera Technologies", "blueera", None),
    ("West Monroe", "west monroe", 1),
    ("Mphasis", "mphasis", 1),
    ("Vidoori", "vidoori", None),
    ("Lorven Technologies", "lorven", 1),
    ("Wise Equation Solutions", "wise equation", 1),
)
TASK_PLATFORMS = (("Mercor", "mercor"),)
AGGREGATOR_HOSTS = ("linkedin.com", "ziprecruiter.com", "indeed.com", "glassdoor.com",
                    "dice.com", "jobright.ai")
_YEARS_RE = re.compile(r"(\d+)\s*(?:[–-]\s*\d+|\+)?\s*years?\s+exp", re.IGNORECASE)


def _load(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _company_match(company: str, needle: str) -> bool:
    c = company.strip().lower()
    return c == needle or c.startswith(needle + " ") or c.startswith(needle + ",") \
        or c.startswith(needle + "(") or (len(needle) > 4 and c.startswith(needle))


def _host(url: str) -> str:
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def _pct(n: int, d: int) -> str:
    return f"{n}/{d} ({(100.0 * n / d):.1f}%)" if d else f"{n}/0"


def analyze(capture_dir: Path) -> Dict[str, Any]:
    reviewed_path = capture_dir / "jobright-postings-reviewed.json"
    raw_path = capture_dir / "jobright-postings-raw.json"
    src = _load(reviewed_path if reviewed_path.exists() else raw_path)
    jobs: List[Dict[str, Any]] = src.get("jobs") or []
    n = len(jobs)
    out: Dict[str, Any] = {
        "version": ANALYSIS_VERSION, "captured_on": src.get("captured_on"),
        "timezone": src.get("timezone"), "scope": src.get("scope"),
        "filters": src.get("filters") or [], "cards": n,
        "companies": len({j.get("company") for j in jobs}),
        "reviewed_file": reviewed_path.exists(),
    }

    # --- reviewer labels (only present in the reviewed file) ---------------
    out["triage"] = Counter(str(j.get("preference_triage") or "not labelled") for j in jobs)
    out["fraud_verdicts"] = Counter(str(j.get("fraud_verdict") or "absent") for j in jobs)
    out["requisition_verified"] = sum(1 for j in jobs if j.get("official_requisition_verified"))

    # --- named service firms and task platforms ----------------------------
    firms = []
    for label, needle, expected in NAMED_SERVICE_FIRMS:
        rows = [j for j in jobs if _company_match(str(j.get("company") or ""), needle)]
        firms.append({"firm": label, "cards": len(rows), "expected": expected,
                      "triage": Counter(str(j.get("preference_triage") or "") for j in rows)})
    out["service_firms"] = firms
    out["service_firm_cards"] = sum(f["cards"] for f in firms)
    tasks = []
    for label, needle in TASK_PLATFORMS:
        for j in jobs:
            if _company_match(str(j.get("company") or ""), needle):
                tasks.append({"company": label, "title": j.get("title"),
                              "attributes": j.get("card_attributes") or [],
                              "original_host": _host(str(j.get("original_job_url") or ""))})
    out["task_contracts"] = tasks
    out["contract_attribute_cards"] = sum(
        1 for j in jobs if "Contract" in (j.get("card_attributes") or []))

    # --- duplicates / reposts ----------------------------------------------
    rows = [{"job_id": j.get("job_id"), "company": j.get("company"), "title": j.get("title"),
             "location": j.get("location"), "original_job_url": j.get("original_job_url")}
            for j in jobs]
    grouping = pid.group_rows(rows)
    groups: Dict[str, List[int]] = defaultdict(list)
    for i, g in enumerate(grouping["group_of"]):
        groups[g].append(i)
    proven = [[jobs[i] for i in idx] for g, idx in groups.items() if len(idx) > 1]
    out["proven_duplicate_groups"] = [
        {"company": m[0].get("company"), "cards": len(m),
         "titles": sorted({str(x.get("title")) for x in m}),
         "key": next(g for g, idx in groups.items() if jobs[idx[0]] is m[0])}
        for m in proven]
    # Same URL once the fragment and the click id are removed, different roles:
    # a portal whose only job identifier (if any) lives after `#`.
    by_base: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for j in jobs:
        u = str(j.get("original_job_url") or "").split("#", 1)[0]
        by_base[re.sub(r"[?&]jr_id=[^&#]*", "", u)].append(j)
    portal = []
    for u, members in by_base.items():
        if len(members) > 1 and len({m.get("title") for m in members}) > 1:
            frags = {pid.fragment_job_id(m.get("original_job_url")) for m in members}
            portal.append({"company": members[0].get("company"), "cards": len(members),
                           "host": _host(u), "titles": sorted({m.get("title") for m in members}),
                           "distinct_fragment_job_ids": len(frags - {""})})
    out["generic_portal_groups"] = portal
    frag = defaultdict(list)
    for j in jobs:
        u = str(j.get("original_job_url") or "")
        fid = pid.fragment_job_id(u)
        if fid:
            frag[(_host(u), fid)].append(j)
    out["fragment_id_groups"] = [
        {"company": v[0].get("company"), "host": k[0], "fragment_job_id": k[1],
         "cards": len(v), "titles": sorted({x.get("title") for x in v}),
         "distinct_urls": len({x.get("original_job_url") for x in v})}
        for k, v in frag.items() if len(v) > 1]
    possible = [i for i, p in enumerate(grouping["possible_duplicate_of"]) if p is not None]
    out["possible_duplicates"] = [
        {"company": jobs[i].get("company"), "title": jobs[i].get("title"),
         "location": jobs[i].get("location"),
         "other_title": jobs[grouping["possible_duplicate_of"][i]].get("title")}
        for i in possible]
    out["reposted_label_cards"] = sum(
        1 for j in jobs if str(j.get("displayed_age") or "").lower().startswith("reposted"))
    same_ct = Counter((str(j.get("company")).lower(), str(j.get("title")).lower()) for j in jobs)
    out["same_company_title_pairs"] = [
        {"company": c, "title": t, "cards": k} for (c, t), k in same_ct.items() if k > 1]

    # --- original source / recency -----------------------------------------
    out["missing_original_link"] = sum(1 for j in jobs if not j.get("original_job_url"))
    out["original_link_on_aggregator"] = Counter(
        h for h in (_host(str(j.get("original_job_url") or "")) for j in jobs)
        if any(h == a or h.endswith("." + a) for a in AGGREGATOR_HOSTS))
    out["original_link_generic"] = sum(
        1 for j in jobs if j.get("original_job_url") and not pid.apply_url_key(
            j.get("original_job_url")))
    out["relative_age_only"] = sum(
        1 for j in jobs if re.search(r"ago$", str(j.get("displayed_age") or "")))
    out["missing_age"] = sum(1 for j in jobs if not j.get("displayed_age"))

    # --- seniority labels vs the captured filter ---------------------------
    level_cards = Counter()
    years_over_2 = 0
    for j in jobs:
        attrs = [str(a) for a in (j.get("card_attributes") or [])]
        levels = [a for a in attrs if "level" in a.lower() or "new grad" in a.lower()]
        label = levels[0] if levels else "no level label"
        level_cards[label] += 1
        for a in attrs:
            m = _YEARS_RE.search(a)
            if m and int(m.group(1)) > 2:
                years_over_2 += 1
                break
    out["level_labels"] = level_cards
    out["no_entry_level_label"] = sum(
        k for lbl, k in level_cards.items()
        if "entry" not in lbl.lower() and "new grad" not in lbl.lower())
    out["years_min_over_filter"] = years_over_2

    # --- LinkedIn ----------------------------------------------------------
    li_path = capture_dir / "linkedin-postings.json"
    cmp_path = capture_dir / "linkedin-catalog-comparison.json"
    if li_path.exists():
        li = _load(li_path)
        lj = li.get("jobs") or []
        out["linkedin"] = {
            "rows": len(lj), "companies": len({j.get("company") for j in lj}),
            "surfaces": Counter(j.get("source_surface") for j in lj),
            "verified_label": sum(1 for j in lj if j.get("verified_label")),
            "promoted": sum(1 for j in lj if "Promoted" in (j.get("displayed_labels") or [])),
            "easy_apply": sum(1 for j in lj if "Easy Apply" in (j.get("displayed_labels") or [])),
            "scope": li.get("scope"),
        }
        if cmp_path.exists():
            cj = _load(cmp_path).get("jobs") or []
            miss = [j for j in cj if not j.get("static_catalog_name_matches")]
            out["linkedin"]["catalog_miss_rows"] = len(miss)
            out["linkedin"]["catalog_miss_companies"] = len(
                {str(j.get("company")).lower() for j in miss})
            out["linkedin"]["live_inventory_membership"] = Counter(
                j.get("live_inventory_membership") for j in cj)
    return out


def render(a: Dict[str, Any]) -> str:
    n = a["cards"]
    L: List[str] = []
    w = L.append
    w("# Jobright / LinkedIn capture analysis (2026-10-21)")
    w("")
    w(f"Generated by `scripts/analyze_jobright_capture.py` ({a['version']}) from the captured "
      "JSON files only - no network, no database. Regenerate rather than edit.")
    w("")
    w("## Scope and limits - read first")
    w("")
    w(f"- **What this is:** {n} Jobright recommendation cards across {a['companies']} displayed "
      f"company names, captured {a['captured_on']} ({a['timezone']}) by scrolling ONE user's "
      "currently filtered recommendations top to bottom. Filters in effect: "
      + ", ".join(f"`{f}`" for f in a["filters"]) + ".")
    w("- **What it is not:** all of Jobright, a random sample, or JobPilot inventory. Nothing "
      "here is a JobPilot measurement, and no JobPilot metric may be computed from it.")
    w(f"- **No fraud is established.** Reviewer `fraud_verdict` values: "
      + ", ".join(f"`{k}` x{v}" for k, v in a["fraud_verdicts"].items())
      + f". Official requisitions verified: {a['requisition_verified']}/{n}. Legitimate "
      "service firms are not scams; a consultancy label is a PREFERENCE category.")
    w("- **No revenue, hiring-intent or ranking-quality claims** about Jobright. Ghost hiring "
      "intent is unobservable from a card; an original link is an observed outbound link, not "
      "proof of ownership or that the role is open.")
    w("- Counts below are only what the card fields, reviewer labels and links support. "
      "Every figure carries its denominator.")
    w("")
    w("## Reviewer triage labels")
    w("")
    w("| label | cards |")
    w("|---|---|")
    for k, v in a["triage"].most_common():
        w(f"| {k} | {_pct(v, n)} |")
    w("")
    w("These are the reviewer's preference-triage CANDIDATES, not verified business-model "
      "classifications.")
    w("")
    w("## Named service firms (consultancy / staffing candidates)")
    w("")
    w("| displayed company | cards | brief expected | reviewer triage on those cards |")
    w("|---|---|---|---|")
    for f in a["service_firms"]:
        tri = "; ".join(f"{k} x{v}" for k, v in f["triage"].items()) or "-"
        exp = "-" if f["expected"] is None else str(f["expected"])
        w(f"| {f['firm']} | {f['cards']} | {exp} | {tri} |")
    w("")
    w(f"Total: **{_pct(a['service_firm_cards'], n)}** of the captured cards belong to the ten "
      "named firms. Whether each card is the firm's own internal role or a client placement "
      "is NOT determinable from the card; the strict preset treats them per "
      "`employer-policy-1.0.0` after exact employer resolution.")
    w("")
    w("## Task / gig contracts")
    w("")
    for t in a["task_contracts"]:
        w(f"- **{t['company']}** - {t['title']} - attributes: {', '.join(t['attributes'])}; "
          f"original link host `{t['original_host']}` (an aggregator, not the platform's own "
          "posting).")
    w(f"- Cards carrying a `Contract` attribute: {_pct(a['contract_attribute_cards'], n)}. "
      "Contract employment alone is not agency placement or task work.")
    w("")
    w("## Duplicates and reposts")
    w("")
    w("Grouped with JobPilot's own rule (`posting_identity`, identity-1.0.0): merge only on "
      "the same normalized original apply URL (tracking such as `jr_id` removed, functional "
      "ids kept); a URL that names no specific job never merges.")
    w("")
    w(f"- **Proven duplicate groups** (same original requisition URL): "
      f"{len(a['proven_duplicate_groups'])} group(s), "
      f"{sum(g['cards'] for g in a['proven_duplicate_groups'])} cards.")
    for g in a["proven_duplicate_groups"]:
        w(f"  - {g['company']}: {g['cards']} cards, title(s) {g['titles']}")
    w(f"- **One portal base URL behind different roles** (NOT duplicates): "
      f"{len(a['generic_portal_groups'])} group(s).")
    for g in a["generic_portal_groups"]:
        w(f"  - {g['company']}: {g['cards']} cards on `{g['host']}` with {len(g['titles'])} "
          f"different titles {g['titles']}; the job id exists only in the URL fragment "
          f"({g['distinct_fragment_job_ids']} distinct ids). A key that drops the fragment - "
          "as the frontend's `applyUrlKey` does - sees ONE URL for these different jobs.")
    w(f"- **Same job id in the URL fragment, different share tokens** (possible duplicates, "
      f"unproven): {len(a['fragment_id_groups'])} group(s).")
    for g in a["fragment_id_groups"]:
        w(f"  - {g['company']}: {g['cards']} cards, `{g['host']}` fragment job "
          f"{g['fragment_job_id']}, {g['distinct_urls']} distinct URLs, titles {g['titles']}")
    w(f"- **Same company + title + location, different identity** (review hints only): "
      f"{len(a['possible_duplicates'])} card(s).")
    for p in a["possible_duplicates"]:
        w(f"  - {p['company']} - {p['title']} ({p['location']})")
    w(f"- Same company + title pairs (any location): {len(a['same_company_title_pairs'])}; "
      "these are exactly the cases title-based dedupe would have merged wrongly or rightly "
      "without evidence.")
    w(f"- Cards labelled `Reposted ...`: {_pct(a['reposted_label_cards'], n)}. A repost label "
      "is the card's claim; the original publication date is not shown on any card.")
    w("")
    w("## Original source and recency")
    w("")
    w(f"- Cards with no original job link: {_pct(a['missing_original_link'], n)}.")
    agg = sum(a["original_link_on_aggregator"].values())
    w(f"- Original link points at ANOTHER aggregator rather than an employer/ATS page: "
      f"{_pct(agg, n)} (" + ", ".join(f"{k} x{v}" for k, v in
                                      a["original_link_on_aggregator"].most_common()) + ").")
    w(f"- Original link names no specific job (generic portal): "
      f"{_pct(a['original_link_generic'], n)}.")
    w(f"- Recency shown only as a relative age (\"N hours ago\"), never an absolute posting "
      f"date: {_pct(a['relative_age_only'], n)}; no age at all: {_pct(a['missing_age'], n)}.")
    w("")
    w("## Seniority labels vs the captured filter")
    w("")
    w("The level filter reads `Entry Level (+1)`: one further level was selected and is not "
      "visible in the capture, so a non-entry label is NOT proof of a filter violation.")
    w("")
    w("| card level label | cards |")
    w("|---|---|")
    for k, v in a["level_labels"].most_common():
        w(f"| {k} | {_pct(v, n)} |")
    w("")
    w(f"- Cards with no Entry / New Grad label: {_pct(a['no_entry_level_label'], n)}.")
    w(f"- Cards stating a minimum of more than 2 years against the `0-2 Years` filter: "
      f"{_pct(a['years_min_over_filter'], n)}.")
    w("")
    if a.get("linkedin"):
        li = a["linkedin"]
        w("## LinkedIn capture (discovery leads, not a scraping dependency)")
        w("")
        w(f"- {li['rows']} distinct job ids across {li['companies']} displayed companies "
          f"(" + ", ".join(f"{k}: {v}" for k, v in li["surfaces"].items()) + ").")
        w(f"- `verified_label`: {_pct(li['verified_label'], li['rows'])}; Promoted: "
          f"{_pct(li['promoted'], li['rows'])}; Easy Apply: {_pct(li['easy_apply'], li['rows'])}.")
        if "catalog_miss_rows" in li:
            w(f"- No exact-name match in the five static catalogs: "
              f"{_pct(li['catalog_miss_rows'], li['rows'])} rows, "
              f"{li['catalog_miss_companies']} companies. Live inventory membership was "
              f"never queried ({dict(li['live_inventory_membership'])}), so this is a "
              "discovery-lead count, NOT 'missing employers'.")
        w("")
    w("## What JobPilot takes from this (regressions, not metrics)")
    w("")
    w("- Two OpenAI cards share Ashby requisition `e6d5ca02-...`: JobPilot groups them "
      "(`tests/test_posting_identity.py`).")
    w("- Three Scalence titles share one JobDiva portal base URL, the job id only in the "
      "fragment: JobPilot keeps the fragment id in the key, and a fragment-less portal URL "
      "is never a key, so they are never merged.")
    w("- Different Microsoft `Software Engineer` cards (different requisitions, cities): never "
      "merged, never flagged.")
    w("- Whether JobPilot is better is measured on JobPilot's own labelled sample and outcome "
      "metrics (CLAUDE-PROMPT J), never against this snapshot.")
    w("")
    return "\n".join(L) + "\n"


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--capture-dir", required=True, type=Path)
    ap.add_argument("--out", type=Path, default=ROOT / "docs" / "audit"
                    / "jobright-capture-analysis.md")
    ap.add_argument("--json", type=Path, default=None, help="also write the raw numbers")
    args = ap.parse_args(argv)
    analysis = analyze(args.capture_dir)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render(analysis), encoding="utf-8")
    if args.json:
        args.json.write_text(json.dumps(analysis, indent=2, default=str), encoding="utf-8")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
