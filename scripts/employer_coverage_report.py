#!/usr/bin/env python3
"""The coverage number, computed from code, with the evidence attached.

READ-ONLY. Writes nothing to the database. `--markdown` regenerates
`docs/coverage/tech-employer-coverage.md` so the document and the code cannot
drift: the file says what this script measured, on a dated inventory, at a
named source revision.

What it will not do
-------------------
Claim coverage of "every technology company". There is no denominator that
could support that sentence. What there IS, is a measurable denominator -
employers we hold EVIDENCE for - and a numerator that only counts a career
source whose OWNERSHIP is proven. Both are reported with the date, the
inventory watermark and the git revision that produced them, so any number
here is falsifiable by re-running one command.

The two numbers that used to be conflated
-----------------------------------------
`company_ats_endpoints.status == "validated"` means "the endpoint answered".
`career_sources.state == "validated"` means "we can show whose board it is AND
it returned attributable postings". The first was reported as coverage and is
not coverage; this script reports both, side by side, so the gap between them
is visible rather than being the reason a number looked good.

Usage
-----
    PYTHONPATH=shared/python python scripts/employer_coverage_report.py
    PYTHONPATH=shared/python python scripts/employer_coverage_report.py \
        --json out.json --markdown
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "shared" / "python"))
sys.path.insert(0, str(ROOT))


def _db():
    env_path = ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    from utils.db_connect import DBConnect
    return DBConnect().get_db()


from services import career_source_service as css        # noqa: E402
from services import employer_registry as er             # noqa: E402
from services import employer_tech_classifier as etc     # noqa: E402
from services import tech_taxonomy as tx                 # noqa: E402


def _now() -> datetime:
    return datetime.now(timezone.utc)


def source_revision() -> str:
    """The git revision this measurement came from.

    Stored WITH the numbers. A coverage figure with no revision cannot be
    reproduced and cannot be attributed to a change, which is how a document
    ends up asserting a number nobody can get back to.
    """
    for repo in (ROOT,):
        try:
            out = subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "--short", "HEAD"],
                capture_output=True, text=True, timeout=10)
            if out.returncode == 0 and out.stdout.strip():
                dirty = subprocess.run(
                    ["git", "-C", str(repo), "status", "--porcelain"],
                    capture_output=True, text=True, timeout=15)
                suffix = "-dirty" if (dirty.stdout or "").strip() else ""
                return out.stdout.strip() + suffix
        except Exception:      # pragma: no cover - a missing git is not fatal
            pass
    return "unknown"


# ---------------------------------------------------------------------------
# The denominator: employers we hold evidence for
# ---------------------------------------------------------------------------
def posting_mix(db, *, days: int = 30) -> Dict[str, Dict[str, Any]]:
    """`{employer_id: {core, adjacent, non_tech, unknown, active}}`.

    Keyed on the CANONICAL employer id where the posting carries one, falling
    back to the tenant join. This is the only signal that reaches a private
    company: EDGAR knows public filers and nothing else.
    """
    cutoff = _now() - timedelta(days=days)
    rows = db.job_postings.aggregate([
        {"$match": {"status": "active",
                    "$or": [{"posted_at": {"$gte": cutoff}},
                            {"first_seen_at": {"$gte": cutoff}}]}},
        {"$group": {"_id": {"ats": "$ats", "tenant": "$tenant",
                            "scope": "$tech.tech_scope"},
                    "n": {"$sum": 1}}},
    ], allowDiskUse=True)
    by_tenant: Dict[str, Dict[str, int]] = collections.defaultdict(
        lambda: collections.defaultdict(int))
    for r in rows:
        key = f"{r['_id'].get('ats') or ''}:{r['_id'].get('tenant') or ''}"
        by_tenant[key][str(r["_id"].get("scope") or "unknown")] += int(r["n"])

    tenant_to_employer: Dict[str, str] = {}
    for emp in db.employers.find({}, {"employer_id": 1, "registry_refs.tenant_keys": 1}):
        for t in ((emp.get("registry_refs") or {}).get("tenant_keys") or []):
            tenant_to_employer.setdefault(str(t), emp["employer_id"])

    out: Dict[str, Dict[str, Any]] = collections.defaultdict(
        lambda: {"core": 0, "adjacent": 0, "non_tech": 0, "unknown": 0, "active": 0})
    for tenant_key, scopes in by_tenant.items():
        eid = tenant_to_employer.get(tenant_key)
        if not eid:
            continue
        bucket = out[eid]
        for scope, n in scopes.items():
            bucket[scope if scope in bucket else "unknown"] += n
            bucket["active"] += n
    return out


#: Evidence kinds that, on their own, put an employer in the TECHNOLOGY
#: denominator. Each is documentary; none is a keyword in a name.
TECH_EVIDENCE_KINDS = frozenset({
    "curated_seed", "official_career_url", "identity_override",
})


def is_evidence_backed_tech(emp: Dict[str, Any], mix: Dict[str, Any],
                            company: Optional[Dict[str, Any]] = None
                            ) -> Tuple[bool, str]:
    """`(in_denominator, why)` for one employer.

    Deliberately NOT a name heuristic: the live registry contains "NUROTOCO OF
    MASSACHUSETTS", "SLACK TIDE CRUISES LLC" and "NOTIONSBYNANCY LLC", and a
    rule that reads "Technologies" in a name promotes all three.

    The `companies` row is joined in because the strongest signal available -
    the SEC-assigned SIC code - lives there and nowhere else. Classifying
    without it discards a filing the registrant made about themselves and
    leaves only the posting mix, which cuts the denominator to the employers
    we already crawl. That is a denominator defined by our own coverage, which
    is precisely the circularity a coverage number must not have.
    """
    kinds = {str(e.get("kind")) for e in (emp.get("evidence") or [])
             if isinstance(e, dict)}
    hit = kinds & TECH_EVIDENCE_KINDS
    if hit:
        return True, f"curated: {sorted(hit)[0]}"

    company = company or {}
    tech_rows = int(mix.get("core", 0)) + int(mix.get("adjacent", 0))
    classified = etc.classify_employer(
        {"name": emp.get("display_name"),
         "domain": emp.get("canonical_domain") or company.get("domain"),
         "sic": company.get("sic"),
         "sic_description": company.get("sic_description"),
         "signals": company.get("signals") or {},
         "cik": company.get("cik"), "ticker": company.get("ticker")},
        posting_mix={"core": int(mix.get("core", 0)),
                     "adjacent": int(mix.get("adjacent", 0)),
                     "non_tech": int(mix.get("non_tech", 0)),
                     "unknown": int(mix.get("unknown", 0))} if mix else None)
    if classified.tech_classification in (etc.TECH_SOFTWARE, etc.TECH_ADJACENT):
        return True, f"classifier: {classified.evidence_source}"

    classified_rows = tech_rows + int(mix.get("non_tech", 0))
    if classified_rows >= etc.POSTING_MIX_MIN_ROWS:
        share = tech_rows / classified_rows
        if share >= etc.POSTING_MIX_ADJACENT:
            return True, (f"posting mix: {tech_rows} of {classified_rows} "
                          f"classified rows are technology ({share:.0%})")
    return False, "no technology evidence"


#: How an employer is placed in a cohort, in order. Every rule reads recorded
#: evidence; none reads a name. `unknown` is a real answer and is reported as
#: one rather than being spread across the others to make the table look full.
def cohorts_for(emp: Dict[str, Any], company: Optional[Dict[str, Any]],
                mix: Dict[str, Any]) -> List[str]:
    declared = [c for c in (emp.get("cohorts") or []) if c != er.COHORT_UNKNOWN]
    if declared:
        return declared
    company = company or {}
    is_filer = bool(company.get("cik") or company.get("ticker"))
    curated = any(str(e.get("kind")) in TECH_EVIDENCE_KINDS
                  for e in (emp.get("evidence") or []) if isinstance(e, dict))
    active = int((mix or {}).get("active", 0))
    # A row in `mnc_catalog` is a human-validated statement that this employer
    # is a multinational, which is documentary; inferring it from size or from
    # a name is not. A filer can be both.
    refs = emp.get("registry_refs") or {}
    # Read BOTH spellings. The canonical field is `catalog_keys`; rows written
    # before the field names were unified carry `catalog`, and a report that
    # silently reads only the new name would show a cohort emptying out during
    # the migration and look like a regression.
    catalogs = {str(c) for c in
                ((refs.get("catalog_keys") or []) + (refs.get("catalog") or []))}
    out: List[str] = []
    if "mnc_catalog" in catalogs:
        out.append(er.COHORT_MNC)
    if is_filer:
        # An SEC filer is a public company. Whether it is ALSO a multinational
        # is a separate claim, made only by the catalog rule above.
        out.append(er.COHORT_PUBLIC_TECH)
    if out:
        return out
    if curated:
        # Curated, not a filer: privately held. The split is on hiring
        # VOLUME, which is observed, rather than on funding, which is not.
        return [er.COHORT_PRIVATE_GROWTH if active >= 50
                else er.COHORT_NICHE_STARTUP]
    return [er.COHORT_UNKNOWN]


def strategic_tier_a() -> List[Tuple[str, str]]:
    """`(display_name, domain)` for the employers this work is accountable for.

    The acceptance criterion is that every one is accounted for as validated,
    empty, blocked or manual review — none silently disappears. The list is
    the official career-URL registry, which is exactly the set someone wrote
    down and asked for.
    """
    from services import official_career_registry as ocr
    return [(r["display_name"], r["domain"]) for r in ocr.OFFICIAL_CAREER_URLS]


# ---------------------------------------------------------------------------
def build(db, *, mix_days: int) -> Dict[str, Any]:
    mix = posting_mix(db, days=mix_days)
    employers = list(db.employers.find({}, {
        "_id": 0, "employer_id": 1, "display_name": 1, "canonical_domain": 1,
        "cohorts": 1, "employer_categories": 1, "countries": 1,
        "operating_status": 1, "id_basis": 1, "identity_confidence": 1,
        "evidence": 1, "registry_refs": 1}))

    # Join the `companies` rows the registry references, in ONE query.
    company_ids = sorted({cid for e in employers
                          for cid in ((e.get("registry_refs") or {}).get("company_ids") or [])})
    companies: Dict[str, Dict[str, Any]] = {}
    for chunk in range(0, len(company_ids), 5000):
        for c in db.companies.find(
                {"company_id": {"$in": company_ids[chunk:chunk + 5000]}},
                {"_id": 0, "company_id": 1, "sic": 1, "sic_description": 1,
                 "signals": 1, "cik": 1, "ticker": 1, "domain": 1, "state": 1}):
            companies[c["company_id"]] = c

    denominator: List[Dict[str, Any]] = []
    for emp in employers:
        if emp.get("operating_status") in er.REDIRECTING_STATUSES:
            continue      # attributed to the parent, not counted twice
        emp_mix = mix.get(emp["employer_id"], {})
        company = next((companies[c] for c in
                        ((emp.get("registry_refs") or {}).get("company_ids") or [])
                        if c in companies), None)
        ok, why = is_evidence_backed_tech(emp, emp_mix, company)
        if not ok:
            continue
        emp["_why_tech"] = why
        emp["_mix"] = emp_mix
        emp["_company"] = company
        emp["cohorts"] = cohorts_for(emp, company, emp_mix)
        if not emp.get("countries") and (company or {}).get("state"):
            emp["countries"] = ["US"]
        denominator.append(emp)

    coverage = css.coverage_by_employer([e["employer_id"] for e in denominator])
    for emp in denominator:
        emp["_coverage"] = coverage.get(emp["employer_id"], {})

    return {"employers": denominator, "mix": mix, "registry_total": len(employers)}


def report(db, built: Dict[str, Any], *, mix_days: int) -> Dict[str, Any]:
    rows = built["employers"]
    total = len(rows)

    by_state = collections.Counter(
        (r["_coverage"].get("state") or css.STATE_CANDIDATE) for r in rows)
    covered = sum(by_state.get(s, 0) for s in css.COVERED_STATES)

    def _facet(field: str) -> Dict[str, Dict[str, int]]:
        out: Dict[str, Dict[str, int]] = collections.defaultdict(
            lambda: {"employers": 0, "covered": 0, "core_postings": 0})
        for r in rows:
            values = r.get(field) or ["unknown"]
            for v in values:
                bucket = out[str(v)]
                bucket["employers"] += 1
                if (r["_coverage"].get("state") in css.COVERED_STATES):
                    bucket["covered"] += 1
                bucket["core_postings"] += int((r.get("_mix") or {}).get("core", 0))
        for bucket in out.values():
            bucket["covered_pct"] = round(
                bucket["covered"] / bucket["employers"], 4) if bucket["employers"] else 0.0
        return dict(out)

    # Provider view, straight off the career sources.
    by_provider: Dict[str, Dict[str, int]] = collections.defaultdict(
        lambda: {"sources": 0, "validated": 0, "ownership_verified": 0})
    for src in db.career_sources.find({}, {"ats_vendor": 1, "state": 1,
                                           "ownership_verified": 1}):
        b = by_provider[str(src.get("ats_vendor") or "unknown")]
        b["sources"] += 1
        if src.get("state") in css.COVERED_STATES:
            b["validated"] += 1
        if src.get("ownership_verified"):
            b["ownership_verified"] += 1

    core_postings = sum(int((r.get("_mix") or {}).get("core", 0)) for r in rows)
    active_postings = sum(int((r.get("_mix") or {}).get("active", 0)) for r in rows)
    yielding = sum(1 for r in rows if int((r.get("_mix") or {}).get("active", 0)) > 0)
    core_yielding = sum(1 for r in rows if int((r.get("_mix") or {}).get("core", 0)) > 0)

    watermark = None
    try:
        wm = list(db.job_postings.aggregate([
            {"$match": {"status": "active"}},
            {"$group": {"_id": None, "m": {"$max": "$last_seen_at"}}}]))
        watermark = wm[0]["m"] if wm else None
    except Exception:      # pragma: no cover
        pass

    from services import tech_feed_metrics as tfm
    try:
        health = tfm.ingestion_metrics()
    except Exception as exc:      # pragma: no cover
        health = {"error": type(exc).__name__}

    endpoint_states = collections.Counter(
        str(e.get("status") or "?")
        for e in db.company_ats_endpoints.find({}, {"status": 1}))

    return {
        "generated_at": _now().isoformat(),
        "source_revision": source_revision(),
        "inventory_watermark": watermark.isoformat() if watermark else None,
        "mix_window_days": mix_days,
        "versions": {
            "taxonomy": tx.TAXONOMY_VERSION,
            "adjacency": tx.ADJACENCY_VERSION,
            "employer_registry": er.REGISTRY_VERSION,
            "career_source": css.CAREER_SOURCE_VERSION,
            "employer_classifier": etc.CLASSIFIER_VERSION,
        },
        "registry": {
            "employers_total": built["registry_total"],
            "evidence_backed_technology_employers": total,
            "open_merge_reviews": db.employer_merge_reviews.count_documents(
                {"resolved": False}),
        },
        "coverage": {
            "validated_career_sources": covered,
            "validated_pct": round(covered / total, 4) if total else 0.0,
            "by_state": dict(by_state),
            "without_validated_coverage": total - covered,
        },
        "yield": {
            "employers_yielding_postings": yielding,
            "employers_yielding_core_tech": core_yielding,
            "active_postings": active_postings,
            "core_tech_postings": core_postings,
            "core_share": round(core_postings / active_postings, 4)
            if active_postings else 0.0,
        },
        "by_cohort": _facet("cohorts"),
        "by_category": _facet("employer_categories"),
        "by_country": _facet("countries"),
        "by_provider": dict(by_provider),
        # The number that used to be reported AS coverage, kept beside the one
        # that is, so the difference is visible.
        "endpoint_status_for_comparison": dict(endpoint_states),
        "crawl_health": health,
        "tier_a": tier_a_rollcall(db),
    }


def tier_a_rollcall(db) -> Dict[str, Any]:
    """Every strategic employer, and the state it is actually in.

    "Accounted for" means it appears here with a state. An employer missing
    from this table is the failure the acceptance criterion names, so the
    table is built from the REGISTRY list rather than from whatever the
    database happens to contain.
    """
    from services import employer_registry as _er
    rows: List[Dict[str, Any]] = []
    for name, domain in strategic_tier_a():
        eid = _er.employer_id_for(domain=domain, name=name)
        emp = db.employers.find_one({"employer_id": eid},
                                    {"_id": 0, "employer_id": 1, "display_name": 1})
        cov = css.coverage_by_employer([eid]).get(eid, {}) if emp else {}
        rows.append({
            "display_name": name, "domain": domain, "employer_id": eid,
            "in_registry": bool(emp),
            "state": cov.get("state") if emp else "not_in_registry",
            "sources": cov.get("sources", 0) if emp else 0,
            "detail": cov.get("detail", "") if emp else
            "no employer row - the backfill did not mint one",
        })
    by_state = collections.Counter(r["state"] for r in rows)
    return {"total": len(rows), "by_state": dict(by_state), "rows": rows}


def render_markdown(summary: Dict[str, Any]) -> str:
    out: List[str] = []
    a = out.append
    a("# Technology employer coverage")
    a("")
    a("**GENERATED FILE — do not edit by hand.**")
    a("Regenerate with:")
    a("")
    a("```bash")
    a("PYTHONPATH=shared/python python scripts/employer_coverage_report.py --markdown")
    a("```")
    a("")
    a(f"- measured at `{summary['generated_at']}`")
    a(f"- source revision `{summary['source_revision']}`")
    a(f"- inventory watermark `{summary['inventory_watermark']}`")
    a(f"- posting-mix window: {summary['mix_window_days']} days")
    versions = ", ".join(f"`{k}={v}`" for k, v in summary["versions"].items())
    a(f"- versions: {versions}")
    a("")
    a("## What is and is not claimed")
    a("")
    a("This is not a claim to cover every technology company; no denominator")
    a("could support that sentence. It is a measured denominator — employers")
    a("we hold documentary evidence for — and a numerator that counts a career")
    a("source only when its OWNERSHIP is proven and its content validated.")
    a("")
    reg, cov, yld = summary["registry"], summary["coverage"], summary["yield"]
    a("| | value |")
    a("|---|---:|")
    a(f"| employers in the registry | {reg['employers_total']:,} |")
    a(f"| **evidence-backed technology employers** | "
      f"**{reg['evidence_backed_technology_employers']:,}** |")
    a(f"| **validated official career sources** | "
      f"**{cov['validated_career_sources']:,}** |")
    a(f"| **validated coverage** | **{cov['validated_pct']:.1%}** |")
    a(f"| employers without validated coverage | "
      f"{cov['without_validated_coverage']:,} |")
    a(f"| employers yielding postings | {yld['employers_yielding_postings']:,} |")
    a(f"| employers yielding core-tech postings | "
      f"{yld['employers_yielding_core_tech']:,} |")
    a(f"| active postings from them | {yld['active_postings']:,} |")
    a(f"| core-tech postings | {yld['core_tech_postings']:,} "
      f"({yld['core_share']:.1%}) |")
    a(f"| open employer merge reviews | {reg['open_merge_reviews']:,} |")
    a("")
    a("## Every employer is in exactly one state")
    a("")
    a("No employer silently disappears: an employer with no career source at")
    a("all is `candidate`, and a provider failure moves a source to `blocked`,")
    a("`moved` or `dead` — never out of the denominator.")
    a("")
    a("| state | employers |")
    a("|---|---:|")
    for state, n in sorted(cov["by_state"].items(), key=lambda kv: -kv[1]):
        a(f"| `{state}` | {n:,} |")
    a("")
    for title, key in (("Coverage by cohort", "by_cohort"),
                       ("Coverage by category", "by_category"),
                       ("Coverage by country", "by_country")):
        a(f"## {title}")
        a("")
        a("| | employers | validated | % | core-tech postings |")
        a("|---|---:|---:|---:|---:|")
        for name, m in sorted(summary[key].items(),
                              key=lambda kv: -kv[1]["employers"]):
            a(f"| `{name}` | {m['employers']:,} | {m['covered']:,} | "
              f"{m['covered_pct']:.1%} | {m['core_postings']:,} |")
        a("")
    a("## Coverage by provider")
    a("")
    a("| provider | career sources | ownership verified | validated |")
    a("|---|---:|---:|---:|")
    for vendor, m in sorted(summary["by_provider"].items(),
                            key=lambda kv: -kv[1]["sources"]):
        a(f"| `{vendor}` | {m['sources']:,} | {m['ownership_verified']:,} | "
          f"{m['validated']:,} |")
    a("")
    a("## The number this replaces")
    a("")
    a("`company_ats_endpoints.status` records whether an endpoint ANSWERED.")
    a("That was previously reported as coverage. It is not coverage — it")
    a("cannot distinguish a company's own board from a stranger's board with")
    a("a similar slug. Both are shown so the gap is visible.")
    a("")
    a("| endpoint status | count |")
    a("|---|---:|")
    for state, n in sorted(summary["endpoint_status_for_comparison"].items(),
                           key=lambda kv: -kv[1]):
        a(f"| `{state}` | {n:,} |")
    a("")
    a("## Strategic Tier-A roll call")
    a("")
    a("Every employer in the official career-URL registry, and the state it is")
    a("in. An employer missing from this table is the failure the acceptance")
    a("criterion names, so the table is built from the REGISTRY rather than")
    a("from whatever the database happens to contain.")
    a("")
    tier = summary.get("tier_a") or {}
    a("| state | employers |")
    a("|---|---:|")
    for state, n in sorted((tier.get("by_state") or {}).items(),
                           key=lambda kv: -kv[1]):
        a(f"| `{state}` | {n:,} |")
    a("")
    a("| employer | domain | state | sources | detail |")
    a("|---|---|---|---:|---|")
    for r in tier.get("rows") or []:
        a(f"| {r['display_name']} | `{r['domain']}` | `{r['state']}` | "
          f"{r['sources']} | {str(r['detail'])[:90]} |")
    a("")
    a("## Crawl health")
    a("")
    health = summary.get("crawl_health") or {}
    if health.get("error"):
        a(f"unavailable: `{health['error']}`")
    else:
        a("| source | shards | crawl lag (min) | stale | shards in backoff |")
        a("|---|---:|---:|---|---:|")
        for src in health.get("sources") or []:
            a(f"| `{src.get('source')}` | {src.get('shards', 0)} | "
              f"{src.get('crawl_lag_minutes')} | {src.get('stale')} | "
              f"{src.get('shards_in_backoff', 0)} |")
        a("")
        a(f"- ingest runner heartbeat age: "
          f"{health.get('ingest_runner_heartbeat_age_minutes')} min "
          f"(healthy: {health.get('ingest_runner_healthy')})")
        for alarm in health.get("alarms") or []:
            a(f"- ALARM: {alarm}")
    a("")
    return "\n".join(out) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default=None)
    ap.add_argument("--markdown", action="store_true",
                    help="regenerate docs/coverage/tech-employer-coverage.md")
    ap.add_argument("--mix-days", type=int, default=30)
    args = ap.parse_args()

    db = _db()
    built = build(db, mix_days=args.mix_days)
    summary = report(db, built, mix_days=args.mix_days)

    reg, cov, yld = summary["registry"], summary["coverage"], summary["yield"]
    print(f"generated_at        {summary['generated_at']}")
    print(f"source_revision     {summary['source_revision']}")
    print(f"inventory_watermark {summary['inventory_watermark']}")
    print(f"employers total     {reg['employers_total']:,}")
    print(f"tech denominator    {reg['evidence_backed_technology_employers']:,}")
    print(f"validated sources   {cov['validated_career_sources']:,} "
          f"({cov['validated_pct']:.1%})")
    print(f"uncovered           {cov['without_validated_coverage']:,}")
    print(f"yielding            {yld['employers_yielding_postings']:,} "
          f"({yld['employers_yielding_core_tech']:,} core-tech)")
    print(f"core-tech postings  {yld['core_tech_postings']:,} of "
          f"{yld['active_postings']:,} ({yld['core_share']:.1%})")
    print("\nby state:")
    for state, n in sorted(cov["by_state"].items(), key=lambda kv: -kv[1]):
        print(f"  {state:22s} {n:>7,}")
    print("\nby cohort:")
    for name, m in sorted(summary["by_cohort"].items(),
                          key=lambda kv: -kv[1]["employers"]):
        print(f"  {name:22s} {m['employers']:>7,} employers  "
              f"{m['covered']:>6,} validated ({m['covered_pct']:.1%})")

    if args.json:
        Path(args.json).write_text(json.dumps(summary, indent=2, default=str),
                                   encoding="utf-8")
        print(f"\nwrote {args.json}")
    if args.markdown:
        out = ROOT / "docs" / "coverage" / "tech-employer-coverage.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(render_markdown(summary), encoding="utf-8")
        print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
