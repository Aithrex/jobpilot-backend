#!/usr/bin/env python3
"""onboard_researched_companies.py — one outcome per researched company.

DRY-RUN BY DEFAULT. No database is touched unless `--apply` is passed, and
`--apply` is for the OWNER to run (the local `.env` is production).

What it does
------------
Joins three inputs and prints exactly one outcome per company:

  * `scripts/data/direct_employer_leads.py` — the 174 LinkedIn-capture leads as
    researched on 2026-10-21 (research facts, generated);
  * `scripts/data/direct_employer_plan.py` — what each eligible lead becomes,
    with the EXACT endpoint identity, plus the owner's top-employer list;
  * the in-repo catalogs and seeds the Oracle crawler reads — so "existing"
    and "added" are checked against the crawl's own member tables, not
    asserted.

Outcomes: existing_covered | existing_unhealthy_or_moved | new_validated |
excluded | review | blocked | unsupported | pending.

How a row reaches the crawler (A5)
----------------------------------
`services/ingest_runner` (systemd on the Oracle A1 box) dispatches shards via
`job_ingest_worker.dispatch_due_shards` -> `run_shard(source, shard)`. A
shard's members are `job_ingest_worker.registry_members(source)`, the union of:

  * `workday` source      <- `workday_tenant_catalog.WORKDAY_TENANT_CATALOG`
                             (+ MNC Workday rows, + seeds);
  * `career-pages` source <- `career_pages_catalog.CAREER_PAGES_CATALOG`,
                             `career_pages_service.ENTERPRISE_CATALOG`,
                             MNC non-Workday rows, and `_SEED_ENDPOINTS` for
                             every termless vendor that rides this source
                             (taleo, ukg, jobvite, jazzhr, radancy, phenom,
                             successfactors, adp_wfn, greenhouse-EU, ...);
  * `oracle` / `icims`    <- `_SEED_ENDPOINTS` only;
  * every source          <- `company_ats_endpoints` rows with status
                             validated|empty (Mongo), deduped by identity.

Each member hashes to a shard (`blake2b(member_key) % SHARD_COUNTS[source]`);
`list_member` lists it through the registered provider
(`ats_providers.registry.get(vendor).list_jobs`, or the legacy board path for
US Greenhouse/Lever/Ashby/Workable/Workday), and each row is written by
`job_inventory_service.upsert_posting`, which stamps `canonical_employer_id`
only when `career_sources` holds owner-verified evidence for the board.
Hence: the CATALOG/SEED row makes the board crawled after deploy; `--apply`
here writes the OWNERSHIP half (`career_sources`) so its postings are
attributed and counted as coverage.

`--apply` deliberately does NOT write `company_ats_endpoints`: a Mongo
endpoint is read by whatever code is on the box, and a region- or host-
dependent row (EU Greenhouse, a newly allowlisted Jibe host) read by the old
code would be fetched from the wrong place. The code-side row ships with the
deploy; run `--apply` after it is live.

Usage
-----
    PYTHONPATH=shared/python python3 scripts/onboard_researched_companies.py
    ... --validate [--only "Cognite" --only "Swift"] [--cohort leads|top|all]
    ... --write-docs                     # docs/audit/direct-employer-onboarding.{json,md}
    ... --apply                          # OWNER: intake + employers + career_sources

`--validate` makes bounded public requests through the SAME provider code the
crawler runs, at <= 1 request/second overall, with the crawler's honest
User-Agent. A 403/429/503, a challenge, a timeout, an empty SPA shell or a
static "No jobs found" template is `blocked`/unverified — never zero.

Running it regularly (the "next batch" loop)
--------------------------------------------
1. Pick the next rows from the doc's `next_batch` queue; research the official
   career page and its ATS chain; add a `TOP_EMPLOYER_PLAN` entry with the
   exact endpoint and `plan="candidate"`.
2. `--validate --only <name>`; if postings come back, add the catalog/seed
   row named by `where`, flip `plan` to "added", and re-run `--write-docs`.
3. Merge + deploy; then the owner runs `--apply` and
   `verify_career_sources.py --mode content --apply`.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
for _p in (ROOT / "shared" / "python", ROOT / "scripts" / "data", ROOT / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import direct_employer_leads as leads_mod        # noqa: E402
import direct_employer_plan as plan_mod          # noqa: E402

DOC_JSON = ROOT / "docs" / "audit" / "direct-employer-onboarding.json"
DOC_MD = ROOT / "docs" / "audit" / "direct-employer-onboarding.md"
ONBOARDING_VERSION = "direct-employer-onboarding-1.0.0"

EXISTING_COVERED = "existing_covered"
EXISTING_UNHEALTHY = "existing_unhealthy_or_moved"
NEW_VALIDATED = "new_validated"
EXCLUDED = "excluded"
REVIEW = "review"
BLOCKED = "blocked"
UNSUPPORTED = "unsupported"
PENDING = "pending"
OUTCOMES: Tuple[str, ...] = (EXISTING_COVERED, EXISTING_UNHEALTHY, NEW_VALIDATED,
                             EXCLUDED, REVIEW, BLOCKED, UNSUPPORTED, PENDING)

#: Validation run statuses (rule 13): never call a partial crawl complete.
V_COMPLETE = "complete"
V_BUDGET = "budget_limited"
V_BLOCKED = "blocked"
V_FAILED = "failed"
V_EMPTY = "empty_verified"
V_NOT_RUN = "not_run"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Requisition vs. non-requisition (counting only)
# ---------------------------------------------------------------------------
#: A posting that collects applicants without being an opening. Counted
#: separately so a board of "General Application" rows never validates as a
#: hiring source. Serve-time purpose (`posting_profile.purpose`) is owned by
#: the direct-employer policy work; this is the onboarding COUNT only.
_NON_REQUISITION_RE = re.compile(
    r"\b(general application|general interest|open application|"
    r"talent (?:community|network|pool|pipeline)|join our talent|"
    r"expression of interest|future (?:opportunities|openings|consideration)|"
    r"don'?t see (?:a|the|your) (?:role|job|position)|resume drop|"
    r"submit your (?:resume|cv)|insert job you excel at|spontaneous application)\b",
    re.IGNORECASE)


def is_non_requisition(title: Any) -> bool:
    return bool(_NON_REQUISITION_RE.search(str(title or "")))


# ---------------------------------------------------------------------------
# Static "No jobs found" templates are not zero
# ---------------------------------------------------------------------------
_EMPTY_TEMPLATE_RE = re.compile(
    r"no (?:jobs|openings|positions|results) (?:found|available|at this time)|"
    r"there are (?:currently )?no (?:open )?(?:jobs|positions|openings)",
    re.IGNORECASE)


def classify_zero(*, parsed: int, source_reported: Optional[int],
                  source_kind: str, statuses: Sequence[Any],
                  body_text: str = "") -> str:
    """What a run that produced NO rows means.

    Only a vendor JSON API that answered 200 and itself reported a total of 0
    is an empty board. Everything else — an HTML page saying "No jobs found"
    (Hover and Quantcast render that template next to real jobs), an SPA
    shell, a 403/429/503, a timeout — is unverified, never zero.
    """
    if parsed > 0:
        return V_COMPLETE
    codes = [s for s in statuses if s is not None]
    if any(c in (403, 429, 503) for c in codes):
        return V_BLOCKED
    if any(c not in (200,) for c in codes) or not codes:
        return V_FAILED
    if source_kind == "json" and source_reported == 0:
        return V_EMPTY
    # 200 everywhere and nothing parsed: a template or shell. Unverified.
    _ = _EMPTY_TEMPLATE_RE.search(body_text or "")
    return V_BLOCKED


# ---------------------------------------------------------------------------
# Shared boards
# ---------------------------------------------------------------------------
def shared_board_refusal(entry: Dict[str, Any]) -> Optional[str]:
    """A reason to refuse, or None. A shared parent board (Roper's UKG board,
    Koch's Avature site) may only be onboarded when the provider filters to
    the hiring entity — otherwise every parent job would be attributed to
    the subsidiary."""
    sb = entry.get("shared_board")
    if not sb:
        return None
    if sb.get("provider_entity_filter"):
        return None
    return (f"shared {sb.get('parent', 'parent')} board; provider cannot filter "
            f"to {sb.get('entity', 'the hiring entity')}")


# ---------------------------------------------------------------------------
# What the crawler actually reads (no Mongo)
# ---------------------------------------------------------------------------
def crawl_members() -> Dict[str, List[Dict[str, Any]]]:
    """Catalog + seed members per source, WITHOUT the Mongo half.

    `registry_members` would also read `company_ats_endpoints`; this script
    never connects to a database outside `--apply`, so it calls the three
    code-side loaders directly. That is exactly what ships with a deploy.
    """
    from services import job_ingest_worker as jw
    out: Dict[str, List[Dict[str, Any]]] = {}
    out[jw.SOURCE_WORKDAY] = jw._load_workday_members() + jw._load_seed_members(
        jw.SOURCE_WORKDAY)
    out[jw.SOURCE_CAREER] = jw._load_career_members() + jw._load_seed_members(
        jw.SOURCE_CAREER)
    for src in (jw.SOURCE_ORACLE, jw.SOURCE_ICIMS):
        out[src] = jw._load_seed_members(src)
    return out


def member_for(endpoint: Dict[str, Any],
               members: Dict[str, List[Dict[str, Any]]]) -> Optional[Dict[str, Any]]:
    """The crawl member serving this exact endpoint identity, or None.

    Identity = (vendor, tenant, site) case-insensitively, plus host where the
    vendor addresses a board by host. Workday additionally must match the pod
    (`wd_host`) - the same tenant on the wrong pod is a different URL.
    """
    from services import job_ingest_worker as jw
    vendor = str(endpoint.get("vendor") or "").lower()
    tenant = str(endpoint.get("tenant") or "").lower()
    site = str(endpoint.get("site") or "").lower()
    host = str(endpoint.get("host") or "").lower()
    pod = str((endpoint.get("extra") or {}).get("wd_host") or "").lower()
    for m in members.get(jw.source_of_vendor(vendor), []):
        mv, mt, ms = jw._identity_of(m)
        if mv != vendor or mt != tenant:
            continue
        if vendor == "workday":
            if ms != site or str(m.get("wd_host") or "").lower() != (pod or "wd1"):
                continue
        elif vendor in ("greenhouse", "lever", "oracle_orc", "ukg", "adp_wfn",
                        "cornerstone"):
            if ms != site:
                continue
        if host and vendor in ("oracle_orc", "radancy", "phenom", "successfactors",
                               "icims", "ukg") and \
                str(m.get("host") or "").lower() not in ("", host):
            continue
        return m
    return None


# ---------------------------------------------------------------------------
# Live validation through the crawler's own providers
# ---------------------------------------------------------------------------
_TOTAL_KEYS = ("total", "totalFound", "totalCount", "TotalJobsCount", "totalHits")


def _sniff_total(payload: Any) -> Optional[int]:
    if not isinstance(payload, dict):
        return None
    for k in _TOTAL_KEYS:
        v = payload.get(k)
        if isinstance(v, int) and not isinstance(v, bool):
            return v
    meta = payload.get("meta")
    if isinstance(meta, dict) and isinstance(meta.get("total"), int):
        return meta["total"]
    items = payload.get("items")
    if isinstance(items, list) and items and isinstance(items[0], dict):
        v = items[0].get("TotalJobsCount")
        if isinstance(v, int):
            return v
    return None


def validate_endpoint(endpoint: Dict[str, Any], *, cap: int = 400,
                      budget: float = 90.0, entity_filter: Optional[str] = None,
                      ctx: Any = None) -> Dict[str, Any]:
    """List one board through its provider and report what came back.

    `ctx` is injectable so tests drive this with fixtures; by default it is
    `simple_context(global_rate=1.0)` — one request per second across all
    hosts, the crawler's User-Agent, no auth.
    """
    from services.ats_providers import registry
    from services.ats_providers.base import ChallengeError, Endpoint

    vendor = str(endpoint.get("vendor") or "")
    provider = registry.get(vendor)
    out: Dict[str, Any] = {"at": _now_iso(), "vendor": vendor,
                           "status": V_FAILED, "source_reported": None,
                           "parsed": 0, "unique_ids": 0, "duplicate_rows": 0,
                           "requisitions": 0, "filtered_non_requisition": 0,
                           "filtered_other_entity": 0, "http_statuses": [],
                           "sample_titles": [], "detail": ""}
    if provider is None:
        out["detail"] = f"no provider registered for {vendor!r}"
        return out
    ep = Endpoint(vendor=vendor, tenant=str(endpoint.get("tenant") or ""),
                  site=str(endpoint.get("site") or ""),
                  host=str(endpoint.get("host") or ""),
                  extra=dict(endpoint.get("extra") or {}))
    out["member_key"] = ep.member_key
    if ctx is None:
        # The crawler's OWN outbound guard (host allowlist + SSRF), so a pass
        # here proves the Oracle runner may fetch these hosts too - a board
        # that answers but sits off the allowlist would be crawled as nothing.
        from services import job_ingest_worker as jw
        from services.ats_providers.http import simple_context
        source = jw.source_of_vendor(vendor)
        ctx = simple_context(
            budget_seconds=budget, global_rate=1.0,
            guard=lambda _v, url: jw.assert_outbound_url(source, url))
    statuses: List[Any] = []
    first_total: List[Optional[int]] = []
    kinds: List[str] = []

    gj, gt, pj = ctx.get_json, ctx.get_text, getattr(ctx, "post_json", None)

    def _wj(url, params=None, timeout=None, vendor="", **kw):
        s, p = gj(url, params=params, timeout=timeout, vendor=vendor, **kw)
        statuses.append(s)
        kinds.append("json")
        if not first_total:
            first_total.append(_sniff_total(p))
        return s, p

    def _wt(url, params=None, timeout=None, vendor="", **kw):
        s, b = gt(url, params=params, timeout=timeout, vendor=vendor, **kw)
        statuses.append(s)
        kinds.append("html")
        return s, b

    def _wp(url, params=None, json_body=None, timeout=None, vendor="", **kw):
        s, p = pj(url, params=params, json_body=json_body, timeout=timeout,
                  vendor=vendor, **kw)
        statuses.append(s)
        kinds.append("json")
        if not first_total:
            first_total.append(_sniff_total(p))
        return s, p

    ctx.get_json, ctx.get_text = _wj, _wt
    if pj is not None:
        ctx.post_json = _wp

    rows: List[Any] = []
    seen: set = set()
    status = V_COMPLETE
    try:
        for job in provider.list_jobs(ep, ctx):
            rows.append(job)
            if len(rows) >= cap:
                status = V_BUDGET
                break
    except ChallengeError as exc:
        status, out["detail"] = V_BLOCKED, str(exc)[:200]
    except Exception as exc:  # a provider bug is a FAILED run, never a crash
        status, out["detail"] = V_FAILED, f"{type(exc).__name__}: {exc}"[:200]
    if status == V_COMPLETE and getattr(ctx, "listing_truncated", False):
        status = V_BUDGET

    for job in rows:
        rid = str(getattr(job, "source_job_id", "") or "")
        if rid in seen:
            out["duplicate_rows"] += 1
            continue
        seen.add(rid)
        title = str(getattr(job, "title", "") or "")
        if is_non_requisition(title):
            out["filtered_non_requisition"] += 1
            continue
        if entity_filter:
            blob = " ".join(str(getattr(job, k, "") or "") for k in
                            ("department", "description", "title"))
            if entity_filter.lower() not in blob.lower():
                out["filtered_other_entity"] += 1
                continue
        out["requisitions"] += 1
        if len(out["sample_titles"]) < 3:
            out["sample_titles"].append(title[:80])
    out["parsed"] = len(rows)
    out["unique_ids"] = len(seen)
    out["source_reported"] = first_total[0] if first_total else None
    out["http_statuses"] = sorted({str(s) for s in statuses})[:8]
    if status == V_COMPLETE and not rows:
        status = classify_zero(parsed=0, source_reported=out["source_reported"],
                               source_kind=(kinds[0] if kinds else "json"),
                               statuses=statuses)
    out["status"] = status
    return out


def validation_ok(v: Optional[Dict[str, Any]]) -> bool:
    return bool(v) and v.get("status") in (V_COMPLETE, V_BUDGET) \
        and int(v.get("requisitions") or 0) > 0


# ---------------------------------------------------------------------------
# Outcomes
# ---------------------------------------------------------------------------
def outcome_for(entry: Dict[str, Any], *, disposition: str,
                validation: Optional[Dict[str, Any]],
                members: Dict[str, List[Dict[str, Any]]]) -> Dict[str, Any]:
    """The single outcome for one company. Pure apart from `members`."""
    if disposition.startswith("exclude_"):
        return {"outcome": EXCLUDED, "why": disposition.replace("exclude_", "")}
    if disposition in ("mixed_review", "identity_unresolved"):
        return {"outcome": REVIEW, "why": disposition}
    if not entry:
        return {"outcome": PENDING, "why": "no onboarding plan recorded"}

    plan = entry.get("plan")
    refusal = shared_board_refusal(entry)
    if refusal and plan in ("added", "candidate", "existing"):
        return {"outcome": UNSUPPORTED, "why": refusal, "provider_needed": "entity_filter"}
    if plan == "pending":
        return {"outcome": PENDING, "why": entry.get("reason") or "evidence missing"}
    if plan == "unsupported":
        need = entry.get("provider_needed")
        if need:
            return {"outcome": UNSUPPORTED,
                    "why": f"unsupported_pending_provider:{need}",
                    "provider_needed": need}
        return {"outcome": UNSUPPORTED, "why": entry.get("reason") or refusal or ""}

    endpoint = entry.get("endpoint")
    if plan == "existing":
        if endpoint is None:
            return {"outcome": EXISTING_UNHEALTHY,
                    "why": entry.get("note") or "no crawlable row"}
        member = member_for(endpoint, members)
        if member is None:
            return {"outcome": EXISTING_UNHEALTHY,
                    "why": "plan says existing but no crawl member serves it"}
        if validation and not validation_ok(validation):
            return {"outcome": EXISTING_UNHEALTHY,
                    "why": f"validation {validation.get('status')}",
                    "member_key": member["member_key"]}
        return {"outcome": EXISTING_COVERED, "member_key": member["member_key"],
                "why": "row already in the crawl tables"}
    if plan == "added":
        member = member_for(endpoint or {}, members)
        if member is None:
            return {"outcome": BLOCKED,
                    "why": "plan says added but no crawl member serves it"}
        if not validation_ok(validation):
            return {"outcome": BLOCKED,
                    "why": "added row has no successful recorded validation",
                    "member_key": member["member_key"]}
        return {"outcome": NEW_VALIDATED, "member_key": member["member_key"],
                "why": "row added; live validation returned requisitions"}
    # candidate: exact endpoint known, not added
    if validation and validation.get("status") == V_EMPTY:
        return {"outcome": BLOCKED, "why": "board answered with zero postings"}
    v_status = (validation or {}).get("status") or V_NOT_RUN
    return {"outcome": BLOCKED, "why": f"not added; validation {v_status}"}


def _lead_rows() -> List[Dict[str, Any]]:
    return [dict(r) for r in leads_mod.LEADS]


def _top_rows() -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for domain, entries in getattr(plan_mod, "TOP_EMPLOYERS", {}).items():
        for name in entries:
            rows.append({"company": name, "domain_group": domain,
                         "disposition": "eligible_direct"})
    return rows


def _entry(name: str, cohort: str) -> Dict[str, Any]:
    if cohort == "leads":
        return dict(plan_mod.LEAD_PLAN.get(name) or {})
    return dict(getattr(plan_mod, "TOP_EMPLOYER_PLAN", {}).get(name) or {})


def build_report(*, validations: Dict[str, Dict[str, Any]],
                 cohorts: Sequence[str] = ("leads", "top", "health")
                 ) -> Dict[str, Any]:
    members = crawl_members()
    report: Dict[str, Any] = {"version": ONBOARDING_VERSION,
                              "generated_at": _now_iso(),
                              "research_reviewed_on": leads_mod.REGISTER_REVIEWED_ON,
                              "coverage_basis": (
                                  "in-repo catalogs and seed tables the crawler "
                                  "loads (no production inventory was read)"),
                              "cohorts": {}}
    if "leads" in cohorts:
        rows = []
        for lead in _lead_rows():
            name = lead["company"]
            entry = _entry(name, "leads")
            res = outcome_for(entry, disposition=lead["disposition"],
                              validation=validations.get(name), members=members)
            rows.append(_row(name, lead, entry, res, validations.get(name)))
        report["cohorts"]["leads"] = _with_counts(rows)
    if "top" in cohorts:
        rows = []
        for top in _top_rows():
            name = top["company"]
            entry = _entry(name, "top")
            res = outcome_for(entry, disposition="eligible_direct",
                              validation=validations.get(name), members=members)
            row = _row(name, top, entry, res, validations.get(name))
            row["domain_group"] = top["domain_group"]
            rows.append(row)
        report["cohorts"]["top"] = _with_counts(rows)
        report["next_batch"] = [r["company"] for r in rows
                                if r["outcome"] in (PENDING, BLOCKED)]
    if "health" in cohorts:
        rows = []
        for name, entry in plan_mod.HEALTH_CHECKS.items():
            res = outcome_for(entry, disposition="eligible_direct",
                              validation=validations.get(name), members=members)
            rows.append(_row(name, {}, entry, res, validations.get(name)))
        report["cohorts"]["health"] = _with_counts(rows)
    return report


def _row(name: str, lead: Dict[str, Any], entry: Dict[str, Any],
         res: Dict[str, Any], validation: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    ep = entry.get("endpoint") or None
    return {
        "company": name,
        "disposition": lead.get("disposition", ""),
        "evidence_state": entry.get("state") or lead.get("source_status", ""),
        "domain": entry.get("domain", ""),
        "provider": (ep or {}).get("vendor") or entry.get("provider_needed")
        or lead.get("provider", ""),
        "endpoint": ep,
        "where": entry.get("where", ""),
        "plan": entry.get("plan", ""),
        "outcome": res["outcome"],
        "why": res.get("why", ""),
        "member_key": res.get("member_key", ""),
        "provider_needed": res.get("provider_needed", ""),
        "note": entry.get("note", ""),
        "validation": validation or {"status": V_NOT_RUN},
    }


def _with_counts(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    counts = {o: 0 for o in OUTCOMES}
    for r in rows:
        counts[r["outcome"]] = counts.get(r["outcome"], 0) + 1
    return {"denominator": len(rows), "counts": counts, "rows": rows}


# ---------------------------------------------------------------------------
# Docs
# ---------------------------------------------------------------------------
def render_markdown(report: Dict[str, Any]) -> str:
    out: List[str] = []
    out.append("# Direct-employer onboarding — outcomes per company")
    out.append("")
    out.append(f"Generated {report['generated_at']} by "
               "`scripts/onboard_researched_companies.py --write-docs` "
               f"({report['version']}). Research reviewed "
               f"{report['research_reviewed_on']}.")
    out.append("")
    out.append("**Coverage basis:** " + report["coverage_basis"] + ". "
               "`existing_covered` means the crawl tables already carry the exact "
               "endpoint; it is not a claim about live inventory counts.")
    out.append("")
    out.append("Validation counts: `source` = total the vendor reported (blank when "
               "the API reports none), `parsed` = rows our provider produced, "
               "`req` = distinct requisitions after removing General Application/"
               "talent-community rows (`non-req`). Status is complete | "
               "budget_limited | blocked | failed | not_run. A blocked/failed run "
               "is unverified, never zero.")
    out.append("")
    titles = {"leads": "174 researched LinkedIn leads",
              "top": "Owner priority: top employers per domain",
              "health": "Existing rows health-checked (no duplicates added)"}
    for key in ("leads", "top", "health"):
        cohort = report["cohorts"].get(key)
        if not cohort:
            continue
        out.append(f"## {titles[key]}")
        out.append("")
        out.append(f"Denominator: **{cohort['denominator']}**. " + ", ".join(
            f"{k}: {v}" for k, v in cohort["counts"].items() if v))
        out.append("")
        out.append("| company | " + ("domain | " if key == "top" else "") +
                   "evidence | provider | endpoint | outcome | why | validated at |"
                   " status | source | parsed | req | non-req |")
        out.append("|---" * (13 if key == "top" else 12) + "|")
        for r in cohort["rows"]:
            v = r["validation"] or {}
            ep = r.get("endpoint") or {}
            ident = ""
            if ep:
                ident = "/".join(x for x in (ep.get("vendor"), ep.get("host"),
                                             ep.get("tenant"), ep.get("site"),
                                             (ep.get("extra") or {}).get("wd_host"))
                                 if x)
            cells = [r["company"]]
            if key == "top":
                cells.append(r.get("domain_group", ""))
            cells += [r["evidence_state"], r["provider"] or "", f"`{ident}`" if ident else "",
                      r["outcome"], (r["why"] or "")[:120],
                      v.get("at", "") if v.get("status") != V_NOT_RUN else "",
                      v.get("status", ""), "" if v.get("source_reported") is None
                      else str(v.get("source_reported")),
                      str(v.get("parsed", "")) if "parsed" in v else "",
                      str(v.get("requisitions", "")) if "requisitions" in v else "",
                      str(v.get("filtered_non_requisition", ""))
                      if "filtered_non_requisition" in v else ""]
            out.append("| " + " | ".join(c.replace("|", "/") for c in cells) + " |")
        out.append("")
    if report.get("next_batch"):
        out.append("## Next batch (work these regularly)")
        out.append("")
        out.append("Run `--validate --only <name>` after recording the official "
                   "career page and the exact ATS endpoint in "
                   "`scripts/data/direct_employer_plan.py`; add the row only when "
                   "the run returns requisitions. See the script docstring.")
        out.append("")
        for name in report["next_batch"]:
            out.append(f"- {name}")
        out.append("")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# --apply (OWNER ONLY): ownership half through the existing services
# ---------------------------------------------------------------------------
def _db():
    env = ROOT / ".env"
    if env.exists():
        import os
        for line in env.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    from utils.db_connect import DBConnect
    return DBConnect().get_db()


def registration_records(row: Dict[str, Any], entry: Dict[str, Any]) -> Dict[str, Any]:
    """Pure: the three documents `--apply` writes for one new_validated row.

    Ownership evidence is the official chain the research observed
    (`SIGNAL_DOMAIN_LINK`, sufficient on its own under ADR-006 rule 4a), dated
    on the research day; content evidence is this run's validation.
    """
    from services import career_source_service as css
    from services import employer_registry as er
    from services.ats_providers import registry
    from services.ats_providers.base import Endpoint

    ep = row["endpoint"]
    v = row["validation"]
    domain = entry["domain"]
    eid = er.employer_id_for(domain=domain, name=row["company"])
    endpoint = Endpoint(vendor=ep["vendor"], tenant=ep["tenant"],
                        site=ep.get("site") or "", host=ep.get("host") or "",
                        extra=dict(ep.get("extra") or {}))
    board = ""
    prov = registry.get(ep["vendor"])
    if prov is not None:
        try:
            board = prov.board_url(endpoint)
        except Exception:
            board = ""
    research_at = datetime.strptime(plan_mod.RESEARCH_DATE, "%Y-%m-%d").replace(
        tzinfo=timezone.utc)
    lead = next((l for l in leads_mod.LEADS if l["company"] == row["company"]), {})
    career_url = entry.get("career_url") or lead.get("official_career_url") or ""
    evidence_url = entry.get("evidence_url") or lead.get("evidence_url") or career_url
    own_ev = [css.evidence(
        css.SIGNAL_DOMAIN_LINK, source=evidence_url or domain,
        value=f"{domain} -> {endpoint.member_key} ({entry.get('state', '')})",
        confirmed=True, at=research_at)]
    con_ev = [css.evidence(
        "content_fetch", source="onboard_researched_companies",
        value=(f"{v.get('requisitions', 0)} requisitions "
               f"({v.get('status')}) at {v.get('at')}"))]
    source = css.build_source(
        employer_id=eid, ats_vendor=ep["vendor"], tenant=ep["tenant"],
        verified_domain=domain, career_landing_url=career_url,
        canonical_endpoint=board, discovery_method=css.METHOD_OFFICIAL_LINK,
        ownership_evidence=own_ev, content_validation_evidence=con_ev,
        member_key=endpoint.member_key,
        job_count_last=int(v.get("requisitions") or 0))
    employer = er.build_employer(
        display_name=row["company"], canonical_domain=domain,
        aliases=(row["company"],), verified_domains=(domain,),
        evidence=[{"kind": "official_career_url",
                   "source": "direct_employer_onboarding",
                   "value": career_url[:200]}])
    candidate = {
        "company_name": row["company"], "owner_domain": domain,
        "official_company_url": lead.get("official_url") or f"https://{domain}/",
        "official_career_url": career_url, "candidate_ats": ep["vendor"],
        "candidate_board_token": ep["tenant"], "candidate_board_url": board,
        "discovery_method": "operator_review", "evidence_url": evidence_url,
        "evidence_at": research_at, "verification_state": "verified",
        "verification_reason": (f"{entry.get('state', '')} chain on "
                                f"{plan_mod.RESEARCH_DATE}; validated "
                                f"{v.get('at')}: {v.get('requisitions')} requisitions"),
        "canonical_employer_id": eid,
    }
    return {"employer": employer, "career_source": source, "candidate": candidate}


def apply_rows(report: Dict[str, Any]) -> List[str]:
    from services import career_source_service as css
    from services import company_discovery_intake as intake
    from services import employer_registry as er
    db = _db()
    written: List[str] = []
    for key in ("leads", "top"):
        for row in (report["cohorts"].get(key) or {}).get("rows", []):
            if row["outcome"] != NEW_VALIDATED:
                continue
            entry = _entry(row["company"], key)
            recs = registration_records(row, entry)
            intake.upsert_candidate(recs["candidate"], db=db)
            er.upsert_employer(recs["employer"])
            sid = css.upsert_source(recs["career_source"])
            written.append(f"{row['company']}: {sid} "
                           f"{recs['career_source']['state']}")
    return written


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _validation_targets(cohort: str, only: Sequence[str]) -> List[Tuple[str, Dict[str, Any]]]:
    out: List[Tuple[str, Dict[str, Any]]] = []
    tables: List[Dict[str, Dict[str, Any]]] = []
    if cohort in ("leads", "all"):
        tables.append(plan_mod.LEAD_PLAN)
        tables.append(plan_mod.HEALTH_CHECKS)
    if cohort in ("top", "all"):
        tables.append(getattr(plan_mod, "TOP_EMPLOYER_PLAN", {}))
    for table in tables:
        for name, entry in table.items():
            if only and name not in only:
                continue
            if entry.get("endpoint") and entry.get("plan") in (
                    "existing", "added", "candidate"):
                if shared_board_refusal(entry):
                    continue
                out.append((name, entry))
    return out


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--validate", action="store_true",
                    help="live, bounded validation through the providers")
    ap.add_argument("--only", action="append", default=[])
    ap.add_argument("--cohort", choices=("leads", "top", "all"), default="all")
    ap.add_argument("--cap", type=int, default=400)
    ap.add_argument("--budget", type=float, default=90.0)
    ap.add_argument("--results", default=str(DOC_JSON),
                    help="validation results to read/merge (the doc JSON)")
    ap.add_argument("--write-docs", action="store_true")
    ap.add_argument("--apply", action="store_true",
                    help="OWNER ONLY: write intake/employers/career_sources")
    args = ap.parse_args(argv)

    validations: Dict[str, Dict[str, Any]] = {}
    prior = Path(args.results)
    if prior.exists():
        try:
            doc = json.loads(prior.read_text(encoding="utf-8"))
            for cohort in (doc.get("cohorts") or {}).values():
                for r in cohort.get("rows", []):
                    v = r.get("validation") or {}
                    if v.get("status") and v.get("status") != V_NOT_RUN:
                        validations[r["company"]] = v
        except (OSError, ValueError) as exc:
            print(f"could not read {prior}: {exc}", file=sys.stderr)

    if args.validate:
        for name, entry in _validation_targets(args.cohort, args.only):
            sb = entry.get("shared_board") or {}
            res = validate_endpoint(entry["endpoint"], cap=args.cap,
                                    budget=args.budget,
                                    entity_filter=sb.get("entity")
                                    if sb.get("provider_entity_filter") else None)
            validations[name] = res
            print(json.dumps({name: {k: res[k] for k in (
                "status", "source_reported", "parsed", "requisitions",
                "filtered_non_requisition", "http_statuses", "detail")}}))
            sys.stdout.flush()

    report = build_report(validations=validations)
    for key, cohort in report["cohorts"].items():
        print(f"{key}: denominator={cohort['denominator']} " + json.dumps(
            {k: v for k, v in cohort["counts"].items() if v}))

    if args.write_docs:
        DOC_JSON.parent.mkdir(parents=True, exist_ok=True)
        DOC_JSON.write_text(json.dumps(report, indent=1, default=str) + "\n",
                            encoding="utf-8")
        DOC_MD.write_text(render_markdown(report), encoding="utf-8")
        print(f"wrote {DOC_JSON.relative_to(ROOT)} and {DOC_MD.relative_to(ROOT)}")

    if args.apply:
        for line in apply_rows(report):
            print("applied:", line)
    else:
        n = sum(1 for c in report["cohorts"].values() for r in c["rows"]
                if r["outcome"] == NEW_VALIDATED)
        print(f"dry-run: {n} new_validated rows would be registered "
              "(intake + employers + career_sources); pass --apply (owner)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
