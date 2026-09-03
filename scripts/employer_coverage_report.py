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


from services import canonical_employer as _ce           # noqa: E402
from services import career_source_service as css        # noqa: E402
from services import coverage_snapshot as _snap          # noqa: E402
from services import employer_registry as er             # noqa: E402
from services import employer_tech_classifier as etc     # noqa: E402
from services import official_career_registry as _ocr    # noqa: E402
from services import tech_taxonomy as tx                 # noqa: E402


def _attribution(db) -> Dict[str, Any]:
    """How much of the inventory carries a canonical employer, and why not."""
    try:
        return _ce.attribution_stats(db)
    except Exception as exc:      # pragma: no cover
        return {"error": type(exc).__name__}


def _provider_readiness() -> Dict[str, Any]:
    """Provider readiness DERIVED from the registered implementations.

    Reported here because "we cannot crawl this ATS" is a coverage claim, and
    it used to be made from a hand-written constant that listed two vendors we
    had already built. A number in this report has to come from the thing it
    describes.
    """
    try:
        from services.ats_providers import registry as ats_registry
        rows = ats_registry.readiness_report()
        by_state = collections.Counter(r["state"] for r in rows.values())
        return {"registered_providers": len(rows),
                "by_state": dict(by_state),
                "not_ready": {v: r["detail"] for v, r in sorted(rows.items())
                              if r["state"] != ats_registry.READY}}
    except Exception as exc:      # pragma: no cover
        return {"error": type(exc).__name__}


def _official_url_verification(db) -> Dict[str, Any]:
    """How many registry URLs have an ACTUAL verification event behind them.

    `verified_at` used to be `None` on every row because the script that was
    documented as writing it did not exist. This counts the events.
    """
    try:
        rows = _ocr.verified_rows(db=db)
        checked = [r for r in rows if r.get("check_outcome")]
        by_outcome = collections.Counter(str(r.get("check_outcome"))
                                         for r in checked)
        return {"registry_rows": len(rows),
                "rows_with_a_verification_event": len(checked),
                "rows_never_checked": len(rows) - len(checked),
                "by_outcome": dict(by_outcome)}
    except Exception as exc:      # pragma: no cover
        return {"error": type(exc).__name__}


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

    # Both spellings, for the same reason as `cohorts_for`: `tenant_keys` is
    # canonical and `tenant` is what rows written before the field names were
    # unified carry. Reading only the new one would silently drop the
    # posting-mix signal, which is the ONLY evidence that reaches a private
    # company - EDGAR knows public filers and nothing else - and the
    # denominator would quietly shrink to the employers we already crawl.
    tenant_to_employer: Dict[str, str] = {}
    for emp in db.employers.find({}, {"employer_id": 1, "registry_refs": 1}):
        refs = emp.get("registry_refs") or {}
        for t in list(refs.get("tenant_keys") or []) + list(refs.get("tenant") or []):
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


#: THE definition now lives in `employer_tech_classifier`, because the feed's
#: `employer_scope=tech_only` filter has to give the same answer this report's
#: denominator gives. A coverage number over a set the product cannot
#: reproduce is a number about nothing. These two names are kept as aliases so
#: every existing reference and test keeps resolving.
TECH_EVIDENCE_KINDS = etc.TECH_EVIDENCE_KINDS
is_evidence_backed_tech = etc.is_evidence_backed_tech


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
        "headquarters_country": 1, "hiring_countries": 1,
        "operating_status": 1, "id_basis": 1, "identity_confidence": 1,
        "evidence": 1, "registry_refs": 1}))

    # Join the `companies` rows the registry references, in ONE query.
    def _company_refs(row: Dict[str, Any]) -> List[str]:
        refs = row.get("registry_refs") or {}
        return [str(c) for c in
                (list(refs.get("company_ids") or []) + list(refs.get("company") or []))]

    company_ids = sorted({cid for e in employers for cid in _company_refs(e)})
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
        company = next((companies[c] for c in _company_refs(emp)
                        if c in companies), None)
        ok, why = is_evidence_backed_tech(emp, emp_mix, company)
        if not ok:
            continue
        emp["_why_tech"] = why
        emp["_mix"] = emp_mix
        emp["_company"] = company
        emp["cohorts"] = cohorts_for(emp, company, emp_mix)
        # Headquarters is documentary. It is NOT hiring availability. A US
        # incorporation state is not evidence the employer hires in the US.
        if not emp.get("headquarters_country") and (company or {}).get("country"):
            emp["headquarters_country"] = company.get("country")
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

    def _values(row: Dict[str, Any], field: str) -> List[Any]:
        raw = row.get(field)
        if raw in (None, "", []):
            return ["unknown"]
        if isinstance(raw, list):
            return raw or ["unknown"]
        return [raw]

    def _facet(field: str) -> Dict[str, Dict[str, int]]:
        out: Dict[str, Dict[str, int]] = collections.defaultdict(
            lambda: {"employers": 0, "covered": 0, "core_postings": 0})
        for r in rows:
            values = _values(r, field)
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

    # ---------------------------------------------------------------------
    # SOURCE-level counts. A different UNIT from the employer counts above,
    # and conflating the two is how `validated_career_sources` came to hold a
    # count of EMPLOYERS. One employer can hold several sources (a Workday
    # board and a Greenhouse board), and one source can be owner-verified
    # without being validated (proven ownership, content not yet confirmed) —
    # so these three numbers are all different and all needed:
    #
    #   covered_career_sources                  sources in a COVERED state
    #   ownership_verified_unvalidated_sources  ownership proven, coverage not
    #   career_sources_total                    everything, including candidates
    #
    # The third one exists so the first two can be read as shares of something
    # rather than as bare counts.
    source_states = collections.Counter()
    covered_sources = ov_unvalidated = sources_total = 0
    for src in db.career_sources.find({}, {"state": 1, "ownership_verified": 1}):
        sources_total += 1
        state = str(src.get("state") or css.STATE_CANDIDATE)
        source_states[state] += 1
        if state in css.COVERED_STATES:
            covered_sources += 1
        elif src.get("ownership_verified"):
            ov_unvalidated += 1

    candidates = total - covered

    return {
        "generated_at": _now().isoformat(),
        "measured_at": _now(),
        "source_revision": source_revision(),
        "inventory_watermark": watermark.isoformat() if watermark else None,
        "mix_window_days": mix_days,
        "versions": {
            "taxonomy": tx.TAXONOMY_VERSION,
            "adjacency": tx.ADJACENCY_VERSION,
            "employer_registry": er.REGISTRY_VERSION,
            "career_source": css.CAREER_SOURCE_VERSION,
            "employer_classifier": etc.CLASSIFIER_VERSION,
            "canonical_employer": _ce.CANONICAL_EMPLOYER_VERSION,
            "official_career_registry": _ocr.REGISTRY_VERSION,
        },
        "registry": {
            "employers_total": built["registry_total"],
            "evidence_backed_technology_employers": total,
            "open_merge_reviews": db.employer_merge_reviews.count_documents(
                {"resolved": False}),
            "open_board_conflicts": db[_ce.BOARD_CONFLICTS].count_documents(
                {"resolved": False}) if _ce.BOARD_CONFLICTS in
            db.list_collection_names() else 0,
        },
        "coverage": {
            # --- EMPLOYERS (the accountability number) --------------------
            "covered_employers": covered,
            "employer_coverage_pct": round(covered / total, 4) if total else 0.0,
            "candidate_employers": candidates,
            "by_state": dict(by_state),
            # --- SOURCES (a different unit) --------------------------------
            "covered_career_sources": covered_sources,
            "ownership_verified_unvalidated_sources": ov_unvalidated,
            "career_sources_total": sources_total,
            "sources_by_state": dict(source_states),
            # --- compatibility --------------------------------------------
            # `validated_career_sources` has ALWAYS held the covered-EMPLOYER
            # count under a source-shaped name. Kept, pointing at the same
            # number it always did, so no existing reader changes meaning
            # under it; `covered_employers` is the name to read.
            "validated_career_sources": covered,
            "validated_pct": round(covered / total, 4) if total else 0.0,
            "without_validated_coverage": candidates,
        },
        "yield": {
            "employers_yielding_postings": yielding,
            "employers_yielding_core_tech": core_yielding,
            "active_postings": active_postings,
            "core_tech_postings": core_postings,
            "core_share": round(core_postings / active_postings, 4)
            if active_postings else 0.0,
        },
        "attribution": _attribution(db),
        "provider_readiness": _provider_readiness(),
        "official_url_verification": _official_url_verification(db),
        "by_cohort": _facet("cohorts"),
        "by_category": _facet("employer_categories"),
        "by_headquarters_country": _facet("headquarters_country"),
        "by_hiring_country": _facet("hiring_countries"),
        # Legacy field, labelled as such. Do not treat it as hiring geography.
        "by_country_legacy": _facet("countries"),
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
    for entry in _ocr.OFFICIAL_CAREER_URLS:
        name, domain = entry["display_name"], entry["domain"]
        eid = _er.employer_id_for(domain=domain, name=name)
        emp = db.employers.find_one({"employer_id": eid},
                                    {"_id": 0, "employer_id": 1, "display_name": 1})
        cov = css.coverage_by_employer([eid]).get(eid, {}) if emp else {}
        rows.append({
            "display_name": name, "domain": domain, "employer_id": eid,
            "batch": str(entry.get("batch") or _ocr.BATCH_BASELINE),
            "in_registry": bool(emp),
            "state": cov.get("state") if emp else "not_in_registry",
            "sources": cov.get("sources", 0) if emp else 0,
            "detail": cov.get("detail", "") if emp else
            "no employer row - the backfill did not mint one",
        })
    by_state = collections.Counter(r["state"] for r in rows)

    # The target was set against the 94 employers in the registry ON THE DAY,
    # so "75 of 94" has to stay checkable against THOSE 94. Rows added later
    # are reported separately rather than being folded into a denominator that
    # quietly grew — growing the denominator is the cheapest way to make a
    # coverage ratio look worse, and shrinking it the cheapest way to make one
    # look better. Neither is measurement.
    def _bucket(batch: str) -> Dict[str, Any]:
        subset = [r for r in rows if r["batch"] == batch]
        states = collections.Counter(r["state"] for r in subset)
        covered = sum(states.get(st, 0) for st in css.COVERED_STATES)
        return {"total": len(subset), "covered": covered,
                "pct": round(covered / len(subset), 4) if subset else 0.0,
                "by_state": dict(states)}

    batches = sorted({r["batch"] for r in rows})
    return {"total": len(rows), "by_state": dict(by_state), "rows": rows,
            "covered": sum(by_state.get(st, 0) for st in css.COVERED_STATES),
            "by_batch": {b: _bucket(b) for b in batches},
            "accountable": _bucket(_ocr.BATCH_BASELINE)}


def snapshot_payload(summary: Dict[str, Any]) -> Dict[str, Any]:
    """The subset every product surface reads, flattened.

    Flat on purpose: the health banner, the coverage endpoint and this report
    must be reading the SAME field, and a nested shape invites each of them to
    reach into a different part of it and drift.
    """
    cov, reg, yld = summary["coverage"], summary["registry"], summary["yield"]
    tier = summary.get("tier_a") or {}
    tier_states = tier.get("by_state") or {}
    tier_covered = sum(tier_states.get(s, 0) for s in css.COVERED_STATES)
    att = summary.get("attribution") or {}
    return {
        "measured_at": summary.get("measured_at") or _now(),
        "source_revision": summary.get("source_revision"),
        "inventory_watermark": summary.get("inventory_watermark"),
        "mix_window_days": summary.get("mix_window_days"),
        "versions": summary.get("versions") or {},
        # --- employers -------------------------------------------------
        "technology_employer_denominator":
            reg["evidence_backed_technology_employers"],
        "covered_technology_employers": cov["covered_employers"],
        "employer_coverage_pct": cov["employer_coverage_pct"],
        "candidate_technology_employers": cov["candidate_employers"],
        "registry_employers_total": reg["employers_total"],
        # --- sources ---------------------------------------------------
        "covered_career_sources": cov["covered_career_sources"],
        "ownership_verified_unvalidated_sources":
            cov["ownership_verified_unvalidated_sources"],
        "career_sources_total": cov["career_sources_total"],
        # --- yield -----------------------------------------------------
        "employers_yielding_postings": yld["employers_yielding_postings"],
        "employers_yielding_core_tech": yld["employers_yielding_core_tech"],
        "active_postings": yld["active_postings"],
        "core_tech_postings": yld["core_tech_postings"],
        # --- strategic -------------------------------------------------
        "tier_a": {"total": tier.get("total", 0), "covered": tier_covered,
                   "by_state": dict(tier_states),
                   "by_batch": tier.get("by_batch") or {},
                   "accountable": tier.get("accountable") or {}},
        # --- breakdowns ------------------------------------------------
        "by_cohort": summary.get("by_cohort") or {},
        "by_category": summary.get("by_category") or {},
        "by_headquarters_country": summary.get("by_headquarters_country") or {},
        "by_hiring_country": summary.get("by_hiring_country") or {},
        # --- attribution ------------------------------------------------
        "canonical_attribution": {
            "attributed_postings": att.get("attributed_postings", 0),
            "owner_verified_postings": att.get("owner_verified_postings", 0),
            "owner_verified_attribution_pct":
                att.get("owner_verified_attribution_pct", 0.0),
            "contested_boards_unresolved":
                att.get("contested_boards_unresolved", 0),
        },
        "open_merge_reviews": reg.get("open_merge_reviews", 0),
        "open_board_conflicts": reg.get("open_board_conflicts", 0),
    }


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
    a(f"| **covered technology employers** | "
      f"**{cov['covered_employers']:,}** |")
    a(f"| **employer coverage** | **{cov['employer_coverage_pct']:.1%}** |")
    a(f"| candidate technology employers (no covered source) | "
      f"{cov['candidate_employers']:,} |")
    a(f"| covered career SOURCES | {cov['covered_career_sources']:,} |")
    a(f"| ownership-verified but unvalidated SOURCES | "
      f"{cov['ownership_verified_unvalidated_sources']:,} |")
    a(f"| career sources total | {cov['career_sources_total']:,} |")
    a(f"| employers yielding postings | {yld['employers_yielding_postings']:,} |")
    a(f"| employers yielding core-tech postings | "
      f"{yld['employers_yielding_core_tech']:,} |")
    a(f"| active postings from them | {yld['active_postings']:,} |")
    a(f"| core-tech postings | {yld['core_tech_postings']:,} "
      f"({yld['core_share']:.1%}) |")
    a(f"| open employer merge reviews | {reg['open_merge_reviews']:,} |")
    a(f"| open employer board conflicts | {reg.get('open_board_conflicts', 0):,} |")
    a("")
    a("### Four counts, four units")
    a("")
    a("These are deliberately separated because they were previously one")
    a("number under a name that fitted none of them.")
    a("")
    a("- **covered technology employers** — employers, the accountability")
    a("  number. One employer, however many boards.")
    a("- **covered career sources** — boards. An employer running a Workday")
    a("  board and a Greenhouse board is one employer and two sources.")
    a("- **ownership-verified but unvalidated sources** — boards whose OWNER")
    a("  is proven and whose CONTENT is not yet confirmed. Work in progress,")
    a("  not failure, and not coverage.")
    a("- **employers yielding postings** — employers we actually have live")
    a("  postings from. Never equal to covered: a covered board can be")
    a("  genuinely empty, and an uncovered board can still be crawled.")
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
                       ("Coverage by headquarters country", "by_headquarters_country"),
                       ("Coverage by hiring country", "by_hiring_country")):
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
    acc = tier.get("accountable") or {}
    a(f"**The accountable set is the {acc.get('total', 0)} employers the target "
      f"was set against**, and it is reported separately from anything added")
    a("since. A denominator that grows is not a measurement.")
    a("")
    a("| batch | employers | covered | % |")
    a("|---|---:|---:|---:|")
    for batch, m in sorted((tier.get("by_batch") or {}).items()):
        a(f"| `{batch}` | {m['total']:,} | {m['covered']:,} | {m['pct']:.1%} |")
    a("")
    a("| state | employers |")
    a("|---|---:|")
    for state, n in sorted((tier.get("by_state") or {}).items(),
                           key=lambda kv: -kv[1]):
        a(f"| `{state}` | {n:,} |")
    a("")
    a("| employer | domain | batch | state | sources | detail |")
    a("|---|---|---|---|---:|---|")
    for r in tier.get("rows") or []:
        a(f"| {r['display_name']} | `{r['domain']}` | `{r.get('batch', '')}` | "
          f"`{r['state']}` | {r['sources']} | {str(r['detail'])[:90]} |")
    a("")
    a("## Canonical employer attribution")
    a("")
    a("A posting carries `canonical_employer_id` only when the board it came")
    a("from has owner-verified evidence naming exactly one employer. Refusing")
    a("is a correct answer, so the share is reported against the postings on")
    a("owner-verified boards, not against the whole inventory.")
    a("")
    att = summary.get("attribution") or {}
    if att.get("error"):
        a(f"unavailable: `{att['error']}`")
    else:
        a("| | value |")
        a("|---|---:|")
        a(f"| active postings considered | {att.get('postings_considered', 0):,} |")
        a(f"| on owner-verified boards | {att.get('owner_verified_postings', 0):,} |")
        a(f"| **carrying a canonical employer** | "
          f"**{att.get('attributed_postings', 0):,}** |")
        a(f"| share of owner-verified postings | "
          f"{float(att.get('owner_verified_attribution_pct') or 0):.1%} |")
        a(f"| attributing boards | {att.get('attributing_boards', 0):,} |")
        a(f"| contested boards | {att.get('contested_boards', 0):,} "
          f"({att.get('contested_boards_unresolved', 0):,} still blocking) |")
        a("")
        a("| quarantine reason | postings |")
        a("|---|---:|")
        for reason, n in sorted((att.get("quarantined_by_reason") or {}).items(),
                                key=lambda kv: -kv[1]):
            a(f"| `{reason}` | {n:,} |")
    a("")
    a("## Provider readiness")
    a("")
    a("Derived from the REGISTERED provider implementations, not from a")
    a("hand-maintained list. The previous list named `usajobs` and")
    a("`successfactors` as unsupported while both were implemented and")
    a("crawling, so every employer on either vendor was reported as an ATS we")
    a("cannot reach.")
    a("")
    pr = summary.get("provider_readiness") or {}
    if pr.get("error"):
        a(f"unavailable: `{pr['error']}`")
    else:
        a(f"- registered providers: **{pr.get('registered_providers', 0)}**")
        for state, n in sorted((pr.get("by_state") or {}).items(),
                               key=lambda kv: -kv[1]):
            a(f"- `{state}`: {n}")
        if pr.get("not_ready"):
            a("")
            a("| vendor | why |")
            a("|---|---|")
            for vendor, why in sorted(pr["not_ready"].items()):
                a(f"| `{vendor}` | {why} |")
    a("")
    a("## Official career-URL verification")
    a("")
    a("`verified_at` now records an actual fetch. Before")
    a("`scripts/verify_official_career_urls.py` existed, `_row` hard-coded it")
    a("to `None` and nothing could ever have written another value.")
    a("")
    ouv = summary.get("official_url_verification") or {}
    if ouv.get("error"):
        a(f"unavailable: `{ouv['error']}`")
    else:
        a(f"- registry rows: {ouv.get('registry_rows', 0)}")
        a(f"- **with a verification event: {ouv.get('rows_with_a_verification_event', 0)}**")
        a(f"- never checked: {ouv.get('rows_never_checked', 0)}")
        for outcome, n in sorted((ouv.get("by_outcome") or {}).items(),
                                 key=lambda kv: -kv[1]):
            a(f"  - `{outcome}`: {n}")
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
    ap.add_argument("--persist", action="store_true",
                    help="store the measurement in `coverage_snapshots` so the "
                         "product reads the SAME numbers this report prints")
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
    print(f"covered employers   {cov['covered_employers']:,} "
          f"({cov['employer_coverage_pct']:.1%})")
    print(f"uncovered           {cov['candidate_employers']:,}")
    print(f"covered SOURCES     {cov['covered_career_sources']:,} of "
          f"{cov['career_sources_total']:,}")
    print(f"owner-verified but  {cov['ownership_verified_unvalidated_sources']:,} "
          f"(unvalidated sources)")
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
    if args.persist:
        _snap.ensure_indexes(db=db)
        stored = _snap.save(snapshot_payload(summary), db=db)
        if stored:
            _snap.prune(db=db)
            print(f"\npersisted coverage snapshot measured_at={stored}")
        else:
            print("\nsnapshot NOT persisted - the measurement was incomplete")
    if args.markdown:
        out = ROOT / "docs" / "coverage" / "tech-employer-coverage.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(render_markdown(summary), encoding="utf-8")
        print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
