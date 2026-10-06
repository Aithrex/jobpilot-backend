"""Read-only, re-runnable per-user match-accuracy harness (FEAT-002).

Measures, for every user in `aspirely_db`, whether the tech feed shows them
EVERY posting that matches their query (no silent drops) and whether every
count the feed reports is a TRUE DB value (not a windowed estimate). It runs
the REAL `tech_feed_service.search()` — personalized to the user's experience
levels AND all-levels — and cross-checks the displayed numbers against an
independently-computed ground truth taken from the module's own filter builder
and stage functions.

READ-ONLY: this script never imports or calls any `set_/update/insert/save/
_save_session` path. The ONLY DB write on the whole audit path is the ephemeral
`tech_feed_searches` session document the real `search()` itself persists (TTL
`SESSION_TTL_SECONDS`, ~1h) — one per `search()` call, self-expiring. That is
documented in every scorecard header. No user / resume / posting / visa
document is ever mutated.

RE-RUNNABLE: each run writes a fresh timestamped pair of files under
`.agents/tasks/match-accuracy/scorecard-<run>.{md,json}`; nothing is
overwritten. `<run>` is a UTC `YYYYMMDDTHHMMSSZ` stamp.

Usage (always `PYTHONPATH=shared/python`):
    python scripts/match_accuracy_audit.py              # full live run
    python scripts/match_accuracy_audit.py --dry-facets # explain smoke only

The design is `.agents/tasks/match-accuracy/design.md` Parts C/D/E and B.6.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Mirror scripts/migrate_indexes.py path wiring so `services.*` / `utils.*`
# resolve from the shared Lambda layer, and load the repo-local .env.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "shared" / "python"))
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:  # pragma: no cover - dotenv is a dev/runtime dep
    pass

from utils.db_connect import DBConnect  # noqa: E402

from services import experience_view  # noqa: E402
from services import resume_access  # noqa: E402
from services import tech_feed_feedback  # noqa: E402
from services import tech_feed_service as tfs  # noqa: E402
from services import work_authorization as wa  # noqa: E402
from services import inventory_feed  # noqa: E402
from services.experience_view import _VIEW_OF_BAND  # noqa: E402

SCORECARD_DIR = ROOT / ".agents" / "tasks" / "match-accuracy"

#: The raw stored seniority bands we count against, each as its OWN uncapped
#: ground-truth `count_documents`. These are the raw `job_postings.seniority`
#: field values (what `build_query` filters on), NOT the folded board view.
RAW_SENIORITY_BANDS = ("intern", "entry", "mid", "senior", "staff",
                       "principal", "director", "executive", "unknown")

#: A sane ceiling on the full-pipeline survivor retrieval, so a user matching
#: the whole corpus does not make the recall check pretend to audit everything.
RECALL_CEILING = 250_000


# ---------------------------------------------------------------------------
# Small read-only helpers
# ---------------------------------------------------------------------------

def _utc_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _postings():
    """The job_postings collection via the module's own accessor (read-only)."""
    from services import job_inventory_service as inv
    return inv._postings()


def _facet_map(facets: Dict[str, Any], name: str) -> Dict[str, int]:
    return {str(e.get("value")): int(e.get("count") or 0)
            for e in (facets.get(name) or [])}


def _date_window_map(facets: Dict[str, Any]) -> Dict[int, int]:
    """`{days: count}` from the `date_window` facet list."""
    out: Dict[int, int] = {}
    for e in (facets.get("date_window") or []):
        try:
            out[int(e.get("days"))] = int(e.get("count") or 0)
        except (TypeError, ValueError):
            continue
    return out


def discover_users(db) -> Tuple[int, List[Dict[str, Any]]]:
    """`(users_total, [{email, active_resume_id, has_active_resume, structured}])`.

    Discovered at RUN TIME — never hardcoded. `has_active_resume` is the subset
    for which `resume_access.get_active_structured(email)` is non-None.
    """
    users_total = db.users.count_documents({})
    rows: List[Dict[str, Any]] = []
    for u in db.users.find({}, {"email": 1, "active_resume_id": 1}):
        email = str(u.get("email") or "").strip().lower()
        if not email:
            continue
        structured = resume_access.get_active_structured(email)
        rows.append({
            "email": email,
            "active_resume_id": u.get("active_resume_id"),
            "has_active_resume": structured is not None,
            "structured": structured,
        })
    rows.sort(key=lambda r: (not r["has_active_resume"], r["email"]))
    return users_total, rows


# ---------------------------------------------------------------------------
# Ground truth: uncapped true-DB counts over the SAME build_query filter
# ---------------------------------------------------------------------------

def true_counts(req: Dict[str, Any], now: datetime) -> Dict[str, Any]:
    """Independently-computed, UNCAPPED ground truth for one normalized request.

    - `total`              : count_documents(build_query(req)) with no COUNT_LIMIT.
    - `seniority[band]`    : count_documents(build_query(req) & {seniority: band})
                             for every raw band (the band's own uncapped truth;
                             the "unknown rides along" variant is naturally
                             counted because each band is its own `$eq`).
    - `date_window[days]`  : count_documents(_window_query(req, days=d, now)).
    """
    coll = _postings()
    query = tfs.build_query(req, now=now)
    total = coll.count_documents(query)

    seniority: Dict[str, int] = {}
    for band in RAW_SENIORITY_BANDS:
        seniority[band] = coll.count_documents({"$and": [query, {"seniority": band}]})

    date_window: Dict[int, int] = {}
    for days in tfs.FACET_DATE_WINDOWS:
        date_window[days] = coll.count_documents(
            tfs._window_query(req, days=days, now=now))

    return {"total": total, "seniority": seniority, "date_window": date_window}


# ---------------------------------------------------------------------------
# Full-pipeline SURVIVOR set — the recall ground truth (design C.3 step 4)
# ---------------------------------------------------------------------------

def survivor_set(req: Dict[str, Any], now: datetime, true_total: int
                 ) -> Tuple[set, bool, int]:
    """`(should_be_returnable, recall_bounded, recall_limit)`.

    Reproduces `_run_search`'s pipeline by calling the module's OWN stage
    functions (never re-implemented, never a filtered_out count subtraction),
    run unbounded up to a sane ceiling. Every call here is a pure read or an
    in-memory transform of already-read rows.
    """
    recall_limit = min(int(true_total), RECALL_CEILING)
    recall_bounded = int(true_total) > RECALL_CEILING

    wa_ctx = tfs._work_auth_context(req)
    suppressed = (tech_feed_feedback.suppressed_for(req["user_email"])
                  if req.get("user_email") else None)
    profile, skills, years = tfs._load_profile(req)

    docs, _diag = tfs.retrieve(req, limit=recall_limit, now=now)
    kept, _ = tfs.hard_filter(docs, req, suppressed=suppressed)
    tfs._resolve_employers(kept)
    kept, _ = tfs.filter_company_categories(kept, req)
    if req["employer_scope"] == tfs.EMPLOYER_SCOPE_TECH:
        kept, _ = tfs._apply_employer_scope(kept)
    kept = inventory_feed._dedupe(kept)
    kept = tfs._collapse_location_variants(kept)
    tfs.hydrate_requirements(kept)
    tfs.rank(kept, req, profile, skills, years, now=now, work_auth=wa_ctx)
    kept, _ = tfs.filter_work_authorization(kept, wa_ctx)
    kept, _ = tfs.filter_work_location(kept, wa_ctx)

    should = {str(r.get("job_id")) for r in kept if r.get("job_id")}
    return should, recall_bounded, recall_limit


def page_to_exhaustion(payload: Dict[str, Any], max_pages: int = 100_000
                       ) -> Tuple[set, Dict[str, Any], int]:
    """Run the REAL feed and follow `next_cursor` to the end.

    Returns `(returned_job_ids, first_response, pages_walked)`. The first
    response is kept so the caller reads its facets/total/warnings from the
    exact search the audit measured.
    """
    returned: set = set()
    first: Optional[Dict[str, Any]] = None
    pages = 0
    cursor: Optional[str] = None
    body = dict(payload)
    while True:
        if cursor is not None:
            body = dict(payload)
            body["cursor"] = cursor
        resp = tfs.search(body)
        pages += 1
        if first is None:
            first = resp
        for job in resp.get("jobs") or []:
            jid = str(job.get("job_id") or "")
            if jid:
                returned.add(jid)
        cursor = resp.get("next_cursor")
        if not cursor or not resp.get("has_more") or pages >= max_pages:
            break
    return returned, first or {}, pages


# ---------------------------------------------------------------------------
# Ranking relevance (reported, not gated)
# ---------------------------------------------------------------------------

def ranking_relevance(jobs: List[Dict[str, Any]], view_levels: List[str]
                      ) -> Dict[str, Any]:
    """Match-score distribution, rank monotonicity, in-view-level fraction."""
    scores = [int(j.get("match_score") or 0) for j in jobs]
    ranks = [float(j.get("rank_score") or 0) for j in jobs]
    monotonic = all(ranks[i] >= ranks[i + 1] for i in range(len(ranks) - 1))
    want = set(view_levels or [])
    in_view = 0
    for j in jobs:
        band = str(j.get("seniority") or "unknown")
        view = _VIEW_OF_BAND.get(band, band)
        if want and view in want:
            in_view += 1
    frac = round(in_view / len(jobs), 4) if jobs and want else None
    return {
        "top_page": len(jobs),
        "match_score_min": min(scores) if scores else None,
        "match_score_max": max(scores) if scores else None,
        "monotonic": monotonic,
        "in_view_level_fraction": frac,
    }


# ---------------------------------------------------------------------------
# Per-user audit of ONE run (one seniority configuration)
# ---------------------------------------------------------------------------

def audit_run(payload: Dict[str, Any], now: datetime, view_levels: List[str]
              ) -> Dict[str, Any]:
    """Measure displayed-vs-true for ONE search configuration."""
    # The REAL feed, paged to exhaustion.
    returned, first, pages = page_to_exhaustion(payload)
    req = first.get("applied_filters") or tfs.normalize_request(payload)
    facets = first.get("facets") or {}
    diagnostics = first.get("diagnostics") or {}

    displayed_total = int(first.get("total") or 0)
    total_is_exact = bool(diagnostics.get("total_is_exact"))

    displayed_sen = _facet_map(facets, "seniority")
    displayed_win = _date_window_map(facets)
    exactness = facets.get("_exactness") or {}
    date_window_is_exact = bool(facets.get("date_window_is_exact"))

    # Independent ground truth (uncapped) over the SAME normalized request.
    truth = true_counts(req, now)
    true_total = int(truth["total"])

    # Recall: full-pipeline survivor set vs what paging actually surfaced.
    should, recall_bounded, recall_limit = survivor_set(req, now, true_total)
    silent_drops = should - returned
    extra_returned = returned - should

    # Cap-aware total check (design Finding 1).
    total_capped = not total_is_exact
    if total_is_exact:
        total_diff = displayed_total - true_total
    else:
        total_diff = displayed_total - min(true_total, tfs.COUNT_LIMIT)

    # Per-facet diffs, each against its OWN uncapped ground truth.
    sen_diffs = {band: displayed_sen.get(band, 0) - truth["seniority"].get(band, 0)
                 for band in RAW_SENIORITY_BANDS}
    win_diffs = {days: displayed_win.get(days, 0) - truth["date_window"].get(days, 0)
                 for days in tfs.FACET_DATE_WINDOWS}

    sen_exact = bool((exactness.get("seniority") or {}).get("exact"))

    # The below-cap corollary: sum(level buckets) == total ONLY when exact.
    sum_buckets = sum(displayed_sen.values())
    sum_equals_total = (sum_buckets == displayed_total) if total_is_exact else None

    ranking = ranking_relevance(first.get("jobs") or [], view_levels)

    max_abs_level_diff = max((abs(v) for v in sen_diffs.values()), default=0)
    max_abs_window_diff = max((abs(v) for v in win_diffs.values()), default=0)

    return {
        "normalized_request": req,
        "pages_walked": pages,
        "displayed": {
            "total": displayed_total,
            "total_is_exact": total_is_exact,
            "total_capped": total_capped,
            "seniority": displayed_sen,
            "date_window": {str(k): v for k, v in displayed_win.items()},
            "exactness": {k: bool((exactness.get(k) or {}).get("exact"))
                          for k in sorted(exactness)},
            "date_window_is_exact": date_window_is_exact,
        },
        "true": {
            "total": true_total,
            "seniority": truth["seniority"],
            "date_window": {str(k): v for k, v in truth["date_window"].items()},
        },
        "diffs": {
            "total": total_diff,
            "seniority": sen_diffs,
            "date_window": {str(k): v for k, v in win_diffs.items()},
            "max_abs_level_diff": max_abs_level_diff,
            "max_abs_window_diff": max_abs_window_diff,
        },
        "recall": {
            "true_total_build_query": true_total,
            "should_be_returnable": len(should),
            "returned": len(returned),
            "silent_drops": len(silent_drops),
            "silent_drop_ids": sorted(silent_drops)[:50],
            "extra_returned": len(extra_returned),
            "extra_returned_ids": sorted(extra_returned)[:50],
            "recall_bounded": recall_bounded,
            "recall_limit": recall_limit,
            "explained_drops": diagnostics.get("filtered_out") or {},
            "pass": len(silent_drops) == 0 and len(extra_returned) == 0,
        },
        "checks": {
            "seniority_exact": sen_exact,
            "date_window_is_exact": date_window_is_exact,
            "sum_buckets": sum_buckets,
            "sum_equals_total": sum_equals_total,
            "level_counts_exact": max_abs_level_diff == 0 and sen_exact,
            "window_counts_exact": max_abs_window_diff == 0 and date_window_is_exact,
        },
        "ranking": ranking,
        "warnings": first.get("warnings") or [],
    }


def audit_user(user: Dict[str, Any], now: datetime) -> Dict[str, Any]:
    """Run both the personalized and the all-levels configurations for one user."""
    email = user["email"]
    view = experience_view.for_user(email)
    levels = list(view.get("levels") or [])
    profile = wa.get_profile(email)

    base = {"user_email": email, "personalize": True}

    # (i) personalized — seniority = the user's experience_view levels mapped
    #     into the request vocab (the view vocab is a subset of the request
    #     seniority vocab, so a direct passthrough is the mapping).
    personalized_payload = dict(base)
    if levels:
        personalized_payload["seniority"] = levels
    personalized_payload["page_size"] = tfs.MAX_PAGE_SIZE

    # (ii) all-levels — no seniority filter at all.
    all_levels_payload = dict(base)
    all_levels_payload["page_size"] = tfs.MAX_PAGE_SIZE

    personalized = audit_run(personalized_payload, now, levels)
    all_levels = audit_run(all_levels_payload, now, levels)

    return {
        "email": email,
        "has_active_resume": user["has_active_resume"],
        "experience": {
            "levels": levels,
            "years": view.get("years"),
            "source": view.get("source"),
            "preference": view.get("preference"),
        },
        "work_auth": {
            "status": profile.get("work_authorization"),
            "source": profile.get("source"),
        },
        "personalized": personalized,
        "all_levels": all_levels,
    }


# ---------------------------------------------------------------------------
# Aggregate + scorecard emission
# ---------------------------------------------------------------------------

def _run_passes(run: Dict[str, Any]) -> bool:
    r = run["recall"]
    c = run["checks"]
    ok = r["pass"] and c["level_counts_exact"] and c["window_counts_exact"]
    # Below-cap corollary only when total is exact.
    if run["displayed"]["total_is_exact"]:
        ok = ok and run["diffs"]["total"] == 0 and c["sum_equals_total"]
    else:
        ok = ok and run["diffs"]["total"] == 0  # cap-aware diff already 0 on pass
    return bool(ok)


def aggregate(users_total: int, users_with_resume: int,
              audited: List[Dict[str, Any]]) -> Dict[str, Any]:
    recall_pass = recall_fail = 0
    level_exact_users = window_exact_users = total_capped_users = 0
    max_level = max_window = silent_total = 0

    for u in audited:
        for key in ("personalized", "all_levels"):
            run = u[key]
            r, d, c = run["recall"], run["diffs"], run["checks"]
            if r["pass"]:
                recall_pass += 1
            else:
                recall_fail += 1
            if c["level_counts_exact"]:
                level_exact_users += 1
            if c["window_counts_exact"]:
                window_exact_users += 1
            if run["displayed"]["total_capped"]:
                total_capped_users += 1
            max_level = max(max_level, d["max_abs_level_diff"])
            max_window = max(max_window, d["max_abs_window_diff"])
            silent_total += r["silent_drops"]

    all_pass = all(_run_passes(u["personalized"]) and _run_passes(u["all_levels"])
                   for u in audited)
    return {
        "users_total": users_total,
        "users_with_active_resume": users_with_resume,
        "runs_audited": len(audited) * 2,
        "recall_pass": recall_pass,
        "recall_fail": recall_fail,
        "level_count_exact_runs": level_exact_users,
        "date_window_exact_runs": window_exact_users,
        "total_capped_runs": total_capped_users,
        "max_abs_level_diff": max_level,
        "max_abs_window_diff": max_window,
        "silent_drops_total": silent_total,
        "acceptance_bar_pass": bool(all_pass and recall_fail == 0
                                    and silent_total == 0
                                    and max_level == 0 and max_window == 0),
    }


SESSION_NOTE = (
    "Read-only harness. The ONLY DB write on the audit path is the ephemeral "
    "`tech_feed_searches` session document the real `search()` persists — one "
    "per search() call, self-expiring via the session TTL (~1h). No user / "
    "resume / posting / visa document is ever mutated.")

CHIP_FOOTNOTE = (
    "Seniority facet counts the stored `job_postings.seniority` field — i.e. "
    "exactly what clicking that filter returns. The per-card seniority chip "
    "prefers a read-time re-classification on rows lacking a backfilled `tech` "
    "subdoc, so chip-sum on a page may differ from the facet; that is a "
    "display detail, not a counting bug.")


def write_scorecards(run_id: str, payload: Dict[str, Any]) -> Tuple[Path, Path]:
    SCORECARD_DIR.mkdir(parents=True, exist_ok=True)
    json_path = SCORECARD_DIR / f"scorecard-{run_id}.json"
    md_path = SCORECARD_DIR / f"scorecard-{run_id}.md"
    if json_path.exists() or md_path.exists():  # never overwrite
        raise SystemExit(f"scorecard for run {run_id} already exists; refusing to overwrite")
    json_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    md_path.write_text(render_markdown(payload), encoding="utf-8")
    return md_path, json_path


def _fmt_counts(d: Dict[str, Any]) -> str:
    return ", ".join(f"{k}={v}" for k, v in d.items() if v) or "—"


def render_markdown(payload: Dict[str, Any]) -> str:
    agg = payload["aggregate"]
    lines: List[str] = []
    has_bar = "acceptance_bar_pass" in agg
    verdict = ("PASS" if agg.get("acceptance_bar_pass") else "FAIL") if has_bar \
        else "N/A (dry-facets smoke only)"
    lines.append(f"# Match-accuracy scorecard — {payload['run_id']}")
    lines.append("")
    lines.append(f"**Aggregate: {verdict}**")
    lines.append("")
    lines.append(f"- db: `{payload['db']}` · feed_version: `{payload['feed_version']}`")
    lines.append(f"- generated_at: {payload['generated_at']}")
    lines.append(f"- {SESSION_NOTE}")
    if payload.get("dry_facets"):
        lines.append("")
        lines.append("## --dry-facets explain smoke")
        df = payload["dry_facets"]
        lines.append(f"- heavy user: `{df.get('user')}` · entry match set: {df.get('entry_match_set')}")
        lines.append(f"- winning plan: `{df.get('winning_plan')}`")
        lines.append(f"- wall time: {df.get('wall_ms')} ms")
        lines.append(f"- index decision: {df.get('index_decision')}")
    lines.append("")
    lines.append("## Aggregate")
    for k, v in agg.items():
        lines.append(f"- {k}: {v}")
    lines.append("")
    lines.append("## Params")
    for k, v in payload["params"].items():
        if k == "normalized_request":
            continue
        lines.append(f"- {k}: {v}")
    lines.append("")
    for u in payload.get("users", []):
        lines.append(f"### {u['email']}")
        exp = u["experience"]
        lines.append(f"- experience: levels={exp['levels']} years={exp['years']} "
                     f"source={exp['source']}")
        lines.append(f"- work_auth: {u['work_auth']['status']} "
                     f"(source={u['work_auth']['source']})")
        for key, label in (("personalized", "Personalized (your levels)"),
                           ("all_levels", "All levels")):
            run = u[key]
            d, r, c = run["diffs"], run["recall"], run["checks"]
            disp = run["displayed"]
            run_verdict = "PASS" if _run_passes(run) else "FAIL"
            lines.append("")
            lines.append(f"#### {label} — {run_verdict}")
            lines.append(f"- displayed.total={disp['total']} "
                         f"(exact={disp['total_is_exact']}, capped={disp['total_capped']}) "
                         f"· true.total={run['true']['total']} · diff={d['total']}")
            lines.append(f"- seniority displayed: {_fmt_counts(disp['seniority'])}")
            lines.append(f"- seniority true:      {_fmt_counts(run['true']['seniority'])}")
            lines.append(f"- max_abs_level_diff={d['max_abs_level_diff']} "
                         f"max_abs_window_diff={d['max_abs_window_diff']}")
            lines.append(f"- date_window displayed: {_fmt_counts(disp['date_window'])}")
            lines.append(f"- date_window true:      {_fmt_counts(run['true']['date_window'])}")
            lines.append(f"- recall: should_be_returnable={r['should_be_returnable']} "
                         f"returned={r['returned']} silent_drops={r['silent_drops']} "
                         f"extra_returned={r['extra_returned']} "
                         f"(bounded={r['recall_bounded']}, limit={r['recall_limit']})")
            if r["silent_drop_ids"]:
                lines.append(f"  - silent_drop_ids (first 50): {r['silent_drop_ids']}")
            lines.append(f"- explained_drops: {_fmt_counts(r['explained_drops'])}")
            rk = run["ranking"]
            lines.append(f"- ranking: top_page={rk['top_page']} "
                         f"match_score[{rk['match_score_min']}..{rk['match_score_max']}] "
                         f"monotonic={rk['monotonic']} "
                         f"in_view_level_fraction={rk['in_view_level_fraction']}")
            if run["warnings"]:
                lines.append(f"- warnings: {run['warnings']}")
        lines.append("")
        lines.append(f"> {CHIP_FOOTNOTE}")
        lines.append("")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# --dry-facets: aggregate(explain=True) smoke on one heavy user
# ---------------------------------------------------------------------------

def dry_facets(db) -> Dict[str, Any]:
    """Run `aggregate(pipeline, explain=True)` on one heavy user and record the
    winning plan + wall time, then DECIDE the seniority index."""
    import time

    now = tfs._now()
    users_total, users = discover_users(db)

    # Pick the user with the largest entry-level match set (the "60k+ entry"
    # heavy case the design calls for), falling back to the first user.
    best_user = None
    best_entry = -1
    coll = _postings()
    for u in users:
        payload = {"user_email": u["email"], "personalize": True}
        req = tfs.normalize_request(payload)
        q = tfs.build_query(req, now=now)
        entry = coll.count_documents({"$and": [q, {"seniority": "entry"}]})
        if entry > best_entry:
            best_entry = entry
            best_user = u
            best_req = req
            best_q = q

    if best_user is None:
        return {"user": None, "entry_match_set": 0,
                "winning_plan": "no users", "wall_ms": 0,
                "index_decision": "undecided — no users to probe"}

    pipeline = [
        {"$match": best_q},
        {"$facet": {
            "seniority": [{"$group": {
                "_id": {"$ifNull": ["$seniority", "unknown"]},
                "count": {"$sum": 1}}}],
            "work_mode": [{"$group": {
                "_id": {"$ifNull": ["$work_mode", "unknown"]},
                "count": {"$sum": 1}}}],
            "country": [{"$group": {
                "_id": {"$ifNull": [
                    {"$arrayElemAt": ["$locations.country", 0]}, "unknown"]},
                "count": {"$sum": 1}}}],
        }},
    ]

    t0 = time.perf_counter()
    try:
        explain = db.command("aggregate", "job_postings", pipeline=pipeline,
                             explain=True)
    except Exception as exc:
        return {"user": best_user["email"], "entry_match_set": best_entry,
                "winning_plan": f"explain failed: {type(exc).__name__}: {exc}",
                "wall_ms": round((time.perf_counter() - t0) * 1000, 1),
                "index_decision": "undecided — explain failed"}
    wall_ms = round((time.perf_counter() - t0) * 1000, 1)

    winning = _winning_plan_summary(explain)
    served_by_index = _match_served_by_index(explain)
    decision = (
        "KEEP existing index (no new index): the $match range for the facet "
        f"aggregation is already served by an index scan ({winning}); the "
        "per-facet $group is a blocking in-memory group in all cases and no "
        "seniority-leading index can serve it. Adding "
        "active_seniority_posted_at would only tax the hot ingest write path."
        if served_by_index else
        "ADD active_seniority_posted_at: the $match range is NOT served by an "
        f"index scan ({winning}); a (status, seniority, posted_at) index would "
        "turn the facet scan into a range scan. Added to migrate_indexes.py; "
        "apply on the ingest/DB box (do not --apply here).")

    return {
        "user": best_user["email"],
        "entry_match_set": best_entry,
        "winning_plan": winning,
        "match_served_by_index": served_by_index,
        "wall_ms": wall_ms,
        "index_decision": decision,
        "explain_raw_keys": sorted(explain.keys()),
    }


def _winning_plan_summary(explain: Dict[str, Any]) -> str:
    """A short human description of the winning stage of a $match explain."""
    stages = explain.get("stages")
    cursor_plan = None
    if isinstance(stages, list) and stages:
        first = stages[0]
        cursor_plan = (((first.get("$cursor") or {}).get("queryPlanner") or {})
                       .get("winningPlan"))
    if cursor_plan is None:
        cursor_plan = (explain.get("queryPlanner") or {}).get("winningPlan")
    if not isinstance(cursor_plan, dict):
        return "unknown (explain shape unrecognized)"
    return _describe_plan(cursor_plan)


def _describe_plan(plan: Dict[str, Any]) -> str:
    """Collect stage names + any index name from a winningPlan tree."""
    names: List[str] = []
    index_name = None
    node: Optional[Dict[str, Any]] = plan
    # Walk the typical single-child chain (queryPlan / inputStage).
    seen = 0
    stack = [plan]
    while stack and seen < 50:
        node = stack.pop()
        seen += 1
        if not isinstance(node, dict):
            continue
        stage = node.get("stage") or node.get("stageType")
        if stage:
            names.append(str(stage))
        if node.get("indexName"):
            index_name = node["indexName"]
        for key in ("inputStage", "queryPlan"):
            child = node.get(key)
            if isinstance(child, dict):
                stack.append(child)
        for child in (node.get("inputStages") or []):
            if isinstance(child, dict):
                stack.append(child)
    summary = " -> ".join(names) if names else "unknown"
    if index_name:
        summary += f" (index: {index_name})"
    return summary


def _match_served_by_index(explain: Dict[str, Any]) -> bool:
    """True when the $match scan is an IXSCAN (not a COLLSCAN)."""
    summary = _winning_plan_summary(explain)
    return "IXSCAN" in summary and "COLLSCAN" not in summary


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def build_params() -> Dict[str, Any]:
    probe = tfs.normalize_request({"personalize": True, "page_size": tfs.MAX_PAGE_SIZE})
    return {
        "posted_within_days": probe["posted_within_days"],
        "include_undated": probe["include_undated"],
        "candidate_limit": tfs.CANDIDATE_LIMIT,
        "count_limit": tfs.COUNT_LIMIT,
        "facet_count_max_ms": tfs.FACET_COUNT_MAX_MS,
        "facet_date_windows": list(tfs.FACET_DATE_WINDOWS),
        "max_page_size": tfs.MAX_PAGE_SIZE,
        "normalized_request_defaults": {k: probe[k] for k in sorted(probe)},
    }


def run_full(db, dry: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    now = tfs._now()
    users_total, users = discover_users(db)
    audited = [audit_user(u, now) for u in users]
    agg = aggregate(users_total, sum(1 for u in users if u["has_active_resume"]),
                    audited)
    payload = {
        "run_id": _utc_run_id(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "db": db.name,
        "feed_version": getattr(tfs, "FEED_VERSION", "unknown"),
        "params": build_params(),
        "aggregate": agg,
        "users": audited,
    }
    if dry:
        payload["dry_facets"] = dry
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-facets", action="store_true",
                        help="Run the aggregate(explain=True) smoke on one heavy "
                             "user and decide the seniority index, then exit.")
    args = parser.parse_args()

    db = DBConnect().get_db()
    print(f"== match_accuracy_audit on db '{db.name}' (LIVE Mongo) ==")

    if args.dry_facets:
        dry = dry_facets(db)
        run_id = _utc_run_id()
        payload = {
            "run_id": run_id,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "db": db.name,
            "feed_version": getattr(tfs, "FEED_VERSION", "unknown"),
            "params": build_params(),
            "aggregate": {"note": "dry-facets smoke only — no per-user audit"},
            "dry_facets": dry,
            "users": [],
        }
        md_path, json_path = write_scorecards(run_id, payload)
        print(json.dumps(dry, indent=2, default=str))
        print(f"\nscorecard: {md_path}")
        return

    payload = run_full(db)
    md_path, json_path = write_scorecards(payload["run_id"], payload)
    agg = payload["aggregate"]
    print(f"\nusers_total={agg['users_total']} "
          f"with_active_resume={agg['users_with_active_resume']} "
          f"runs={agg['runs_audited']}")
    print(f"recall_fail={agg['recall_fail']} silent_drops_total={agg['silent_drops_total']} "
          f"max_abs_level_diff={agg['max_abs_level_diff']} "
          f"max_abs_window_diff={agg['max_abs_window_diff']}")
    print(f"ACCEPTANCE BAR: {'PASS' if agg['acceptance_bar_pass'] else 'FAIL'}")
    print(f"scorecard: {md_path}")
    if not agg["acceptance_bar_pass"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
