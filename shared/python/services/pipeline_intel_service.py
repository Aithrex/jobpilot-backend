"""Pipeline Intelligence — application funnel, ghost detection, cohort benchmarks.

Compass §6.8 (pipeline funnel) + §6.11.6 / §6.11.7 (ghost detection + anonymized
cohort benchmarks). Deterministic, honest math over the data we ALREADY store —
no parallel application store is created here.

Data model (discovered, reused — NOT invented)
-----------------------------------------------
The canonical application tracker is **`tailoring_records`** (same collection the
dashboard's `overview_service` and `recommendations_service` treat as "the
Applications board"). Each doc carries:

    { record_id, user_email, deleted, created_at, updated_at,
      jd_analysis: { company, job_title, ... },
      application: { status, applied_at, updated_at, interview_dates: [dt],
                     recruiter_email, recruiter_name, ... },
      ats_scores:  { overall: int, ... } | None }

`application.status` vocabulary (validated in resume.py::_APPLICATION_STATUSES):
    draft · interested · applied · interviewing · offer · rejected · withdrawn · ghosted

There is **no separate status-event / history collection** — status transitions
only `$set` the current status + `application.updated_at`. So the honest
"last-event" timestamp for a record is `application.updated_at` (falling back to
`applied_at` / the record's `updated_at` / the latest interview date). We do NOT
fabricate a per-stage timeline we don't have.

`saved_jobs` (the Daily-Pipeline "interested" list) is a SECONDARY store that is
bidirectionally synced to `tailoring_records` via `source_job_id`
(job_service.update_saved_job ↔ resume.py::update_application). To avoid
double-counting synced pairs, the funnel/cohort math is sourced from
`tailoring_records` only — the single source of truth for "did I actually apply".

Honesty rules enforced here
---------------------------
* Stages that are NOT tracked (employer "viewed", "online assessment") are
  OMITTED, never faked — surfaced under `untracked_stages` so the caller can be
  transparent about it.
* Conversion rates are `None` (not `0.0`) when the denominator is 0 — "no data"
  is not "0%".
* Cohort aggregates are anonymized, require a minimum peer count, and are
  differentially NOISED (Laplace) before release; another user's row is never
  exposed.

Only `draft_followup()` calls the LLM (Claude PRO via the provider facade); the
three read APIs are pure Mongo + arithmetic.
"""
from __future__ import annotations

import logging
import math
import random
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from utils.db_connect import DBConnect
from utils.security import InputSanitizer

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Tunables
# --------------------------------------------------------------------------- #
# Statuses that mean "submitted and still awaiting an employer response". These
# are the only records eligible to be flagged as ghosted.
_ACTIVE_STATUSES = ("applied", "interviewing")

# Forward-progress ordinal. draft/interested = 0 (not yet a real application).
_STAGE_RANK = {"applied": 1, "interviewing": 2, "offer": 3}

# No movement in > this many days on an ACTIVE application → likely ghosted.
_GHOST_DAYS = 21

# k-anonymity floor: never release a cohort aggregate computed from fewer peers.
_MIN_COHORT = 20

# Differential-privacy budget for the released cohort rate (Laplace mechanism).
# Sensitivity of a proportion to one record is 1/n; scale = sensitivity/epsilon.
_COHORT_EPSILON = 1.0

# A cohort peer must have at least this many applications to be a stable member
# (drops one-off accounts that would add noise, not signal).
_MIN_APPS_PER_PEER = 3

# Hard cap on the cross-user scan so a pathological collection size can't blow up
# a request. At current scale the whole collection is well under this; past it,
# switch to the nightly pre-aggregation described in the module's ship notes.
_COHORT_SCAN_CAP = 50000

# ATS threshold used by the (non-leaky) strongest-predictor probe.
# 75, not 80: scorer v2 is honest (deterministic rubric, real parseability)
# and lands ~10 points below the old coaxed scores; 75-80 is the target band.
_STRONG_ATS = 75
# Each arm of a predictor comparison needs this much support to be trustworthy.
_MIN_PREDICTOR_SUPPORT = 8


# --------------------------------------------------------------------------- #
# Small shared helpers (mirror overview_service / recommendations_service)
# --------------------------------------------------------------------------- #
def _db():
    return DBConnect().get_db()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt):
    """Normalize a possibly-naive Mongo datetime to UTC-aware (or None)."""
    return dt if (dt is None or getattr(dt, "tzinfo", None)) else dt.replace(tzinfo=timezone.utc)


def _clean_email(user_email: str) -> Optional[str]:
    """Sanitize the JWT-supplied identity before it touches a Mongo filter."""
    return InputSanitizer.sanitize_email(user_email or "")


def _record_fields() -> Dict[str, int]:
    """Projection shared by every read path — pull only what the math needs."""
    return {
        "_id": 0,
        "record_id": 1,
        "user_email": 1,
        "created_at": 1,
        "updated_at": 1,
        "jd_analysis.company": 1,
        "jd_analysis.job_title": 1,
        "application.status": 1,
        "application.applied_at": 1,
        "application.updated_at": 1,
        "application.interview_dates": 1,
        # recruiter_email intentionally NOT projected — it is third-party PII the
        # cohort math never uses, and it must not enter the cross-user scan.
        "ats_scores.overall": 1,
    }


def _furthest_stage(app: Dict[str, Any]) -> int:
    """Furthest funnel stage a record reached (0..3), inferred from concrete
    evidence only — never optimistic.

    We have no per-transition history, so we reconstruct progress from the
    current status plus hard signals (a stamped `applied_at`, a non-empty
    `interview_dates`). Terminal-negative statuses (rejected/withdrawn/ghosted)
    imply the application WAS submitted — you cannot be rejected from a job you
    never applied to — and imply the interview stage only if an interview was
    actually recorded.
    """
    status = str((app or {}).get("status") or "").lower()
    if status == "offer":
        return 3
    if status == "interviewing":
        return 2
    if status == "applied":
        return 1

    has_interview = bool((app or {}).get("interview_dates"))
    has_applied = (app or {}).get("applied_at") is not None

    if status in ("rejected", "withdrawn", "ghosted"):
        return 2 if has_interview else 1

    # draft / interested / unknown — only credit stages we can prove.
    if has_interview:
        return 2
    if has_applied:
        return 1
    return 0


def _last_event(rec: Dict[str, Any]) -> Optional[datetime]:
    """Best available "last activity" timestamp for a record (UTC-aware).

    Preference: application.updated_at → latest interview date → applied_at →
    record.updated_at → created_at. This is the honest stand-in for a real
    status-event log, which the schema does not have.
    """
    app = rec.get("application") or {}
    candidates = [
        _aware(app.get("updated_at")),
        _aware(app.get("applied_at")),
        _aware(rec.get("updated_at")),
        _aware(rec.get("created_at")),
    ]
    for d in (app.get("interview_dates") or []):
        candidates.append(_aware(d))
    live = [c for c in candidates if c is not None]
    return max(live) if live else None


def _rate(numer: int, denom: int) -> Optional[float]:
    """Proportion rounded to 3dp, or None when there is no denominator (honest:
    'no data' is not '0%')."""
    if not denom:
        return None
    return round(numer / denom, 3)


def _company_role(rec: Dict[str, Any]) -> Dict[str, str]:
    jd = rec.get("jd_analysis") or {}
    return {
        "company": (jd.get("company") or "").strip() or "Unknown company",
        "role": (jd.get("job_title") or "").strip() or "Unknown role",
    }


def _load_records(email: str) -> List[Dict[str, Any]]:
    """All of a user's live application records (soft-deletes excluded)."""
    try:
        return list(_db().tailoring_records.find(
            {"user_email": email, "deleted": {"$ne": True}},
            _record_fields(),
        ))
    except Exception as e:
        logger.warning("pipeline_intel: _load_records failed for %s: %s", email, e)
        return []


# --------------------------------------------------------------------------- #
# Cohort keying — role family + seniority, derived from application titles so
# every user is keyed the SAME cheap way (one collection, no per-user resume
# parse → apples-to-apples cohorts and a bounded read).
# --------------------------------------------------------------------------- #
_ROLE_FAMILY_RULES = [
    ("data_ml", ("machine learning", "ml engineer", "ai engineer", "data scient",
                 "deep learning", "nlp", "computer vision", "mlops", "research scientist")),
    ("data_eng", ("data engineer", "analytics engineer", "etl", "data platform")),
    ("data_analyst", ("data analyst", "business analyst", "bi analyst", "analytics")),
    ("cloud_devops", ("devops", "site reliability", " sre", "platform engineer",
                      "infrastructure", "cloud engineer", "systems engineer")),
    ("security", ("security engineer", "appsec", "infosec", "cybersecurity", "security analyst")),
    ("mobile", ("ios engineer", "android engineer", "mobile engineer", "mobile developer")),
    ("frontend", ("frontend", "front end", "front-end", "ui engineer")),
    ("backend", ("backend", "back end", "back-end", "api engineer")),
    ("fullstack", ("full stack", "full-stack", "fullstack")),
    ("product", ("product manager", "product owner", "program manager", "technical program")),
    ("design", ("designer", "ux ", "ui/ux", "product design")),
    ("swe", ("software engineer", "software developer", "sde", "swe", "developer", "engineer")),
]


def _role_family(title: str) -> str:
    """Coarse role family from a job title. Order matters — specific families are
    matched before the generic `swe` catch-all."""
    t = f" {(title or '').lower().strip()} "
    for family, needles in _ROLE_FAMILY_RULES:
        if any(n in t for n in needles):
            return family
    return "other"


def _seniority(title: str) -> str:
    """Coarse seniority bucket from a job title (entry / mid / senior)."""
    t = f" {(title or '').lower()} "
    if any(k in t for k in (" senior ", " sr ", " staff ", " principal ", " lead ",
                            " manager ", " architect ", " ii ", " iii ", " head ")):
        return "senior"
    if any(k in t for k in (" intern", " internship", " junior ", " jr ", " entry ",
                            " new grad", " new-grad", " graduate ", " associate ", " trainee ",
                            " apprentice ", " i ")):
        return "entry"
    return "mid"


def _modal(values: List[str], default: str) -> str:
    counts: Dict[str, int] = {}
    for v in values:
        if v:
            counts[v] = counts.get(v, 0) + 1
    if not counts:
        return default
    return max(counts.items(), key=lambda kv: (kv[1], kv[0]))[0]


def _cohort_key_for(records: List[Dict[str, Any]]) -> Dict[str, str]:
    """A user's (role_family, seniority) cohort key = the modal family + modal
    seniority across the applications they actually submitted."""
    titles = [
        (r.get("jd_analysis") or {}).get("job_title") or ""
        for r in records
        if _furthest_stage(r.get("application") or {}) >= 1
    ]
    return {
        "role_family": _modal([_role_family(t) for t in titles], "other"),
        "seniority": _modal([_seniority(t) for t in titles], "mid"),
    }


def _laplace(scale: float) -> float:
    """Laplace(0, scale) sample via inverse-CDF (stdlib only)."""
    if scale <= 0:
        return 0.0
    u = random.random() - 0.5
    return -scale * math.copysign(1.0, u) * math.log(1.0 - 2.0 * abs(u))


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def funnel(user_email: str) -> Dict[str, Any]:
    """Application funnel + conversion rates for one user.

    Stages are CUMULATIVE ("reached at least"): a record at `offer` also counts
    toward `applied` and `interview`. Only tracked stages are returned; employer
    "viewed" and "online assessment" are omitted (not captured) and named under
    `untracked_stages` for honest UI copy.
    """
    email = _clean_email(user_email)
    if not email:
        return {"stages": [], "conversion": {}, "totals": {}, "untracked_stages": []}

    records = _load_records(email)
    applied = interview = offer = 0
    for r in records:
        stage = _furthest_stage(r.get("application") or {})
        if stage >= 1:
            applied += 1
        if stage >= 2:
            interview += 1
        if stage >= 3:
            offer += 1

    stages = [
        {"key": "applied", "label": "Applied", "count": applied},
        {"key": "interview", "label": "Interview", "count": interview},
        {"key": "offer", "label": "Offer", "count": offer},
    ]
    conversion = {
        "applied_to_interview": _rate(interview, applied),
        "interview_to_offer": _rate(offer, interview),
        "applied_to_offer": _rate(offer, applied),
    }
    return {
        "stages": stages,
        "conversion": conversion,
        "totals": {
            "tracked": len(records),
            "applied": applied,
            "interview": interview,
            "offer": offer,
        },
        # Named, not faked — no employer-view or online-assessment signal exists.
        "untracked_stages": ["viewed", "online_assessment"],
    }


def ghosts(user_email: str) -> List[Dict[str, Any]]:
    """Active applications with no status change in > _GHOST_DAYS days.

    "Active" = still awaiting an employer response (status applied/interviewing).
    Sorted most-stale first. Returns identifying context only for the caller's
    own records.
    """
    email = _clean_email(user_email)
    if not email:
        return []

    now = _now()
    cutoff = now - timedelta(days=_GHOST_DAYS)
    out: List[Dict[str, Any]] = []
    try:
        cursor = _db().tailoring_records.find(
            {
                "user_email": email,
                "application.status": {"$in": list(_ACTIVE_STATUSES)},
                "deleted": {"$ne": True},
            },
            _record_fields(),
        )
    except Exception as e:
        logger.warning("pipeline_intel: ghosts query failed for %s: %s", email, e)
        return []

    for r in cursor:
        last = _last_event(r)
        if last is None or last >= cutoff:
            continue
        days = (now - last).days
        cr = _company_role(r)
        out.append({
            "application_id": r.get("record_id"),
            "company": cr["company"],
            "role": cr["role"],
            "status": (r.get("application") or {}).get("status"),
            "days_since_last_event": days,
            "last_event_at": last.isoformat(),
        })

    out.sort(key=lambda g: g["days_since_last_event"], reverse=True)
    return out


def mark_ghosted(user_email: str) -> Dict[str, Any]:
    """Flip a user's stale-active applications to the `ghosted` status so they
    stop inflating the ACTIVE funnel — honest pipelines.

    Idempotent and race-safe: the update is guarded on the record still being in
    an active status, so a concurrent user edit is never clobbered. Marking a
    record `ghosted` does NOT remove it from the funnel's cumulative "applied"
    count (you DID apply) — it only removes it from the active/awaiting set.
    """
    email = _clean_email(user_email)
    if not email:
        return {"marked_count": 0, "ghosted": []}

    stale = ghosts(email)
    if not stale:
        return {"marked_count": 0, "ghosted": []}

    now = _now()
    # record_ids are our own UUIDs (not user input); safe in an $in operator.
    ids = [g["application_id"] for g in stale if g.get("application_id")]
    try:
        res = _db().tailoring_records.update_many(
            {
                "user_email": email,
                "record_id": {"$in": ids},
                "application.status": {"$in": list(_ACTIVE_STATUSES)},
            },
            {"$set": {
                "application.status": "ghosted",
                "application.updated_at": now,
                "application.ghosted_at": now,
                "updated_at": now,
            }},
        )
        marked = int(getattr(res, "modified_count", 0) or 0)
    except Exception as e:
        logger.warning("pipeline_intel: mark_ghosted failed for %s: %s", email, e)
        return {"marked_count": 0, "ghosted": []}

    logger.info("pipeline_intel: marked %d application(s) ghosted for %s", marked, email)
    return {"marked_count": marked, "ghosted": stale}


def cohort_benchmark(user_email: str) -> Dict[str, Any]:
    """Anonymized, differentially-noised callback-rate benchmark vs. a peer
    cohort of the same role family + seniority.

    "Callback" = an application that reached the interview stage or beyond. The
    cohort must have >= `_MIN_COHORT` peers or `{insufficient: True}` is
    returned; the released cohort rate carries small Laplace noise and no
    individual peer is ever exposed. Truthful, not motivational.
    """
    email = _clean_email(user_email)
    if not email:
        return {"insufficient": True, "reason": "no_identity", "noised": True}

    my_records = _load_records(email)
    my_applied = sum(1 for r in my_records if _furthest_stage(r.get("application") or {}) >= 1)
    my_callbacks = sum(1 for r in my_records if _furthest_stage(r.get("application") or {}) >= 2)
    your_rate = _rate(my_callbacks, my_applied)
    key = _cohort_key_for(my_records)

    base = {
        "your_callback_rate": your_rate,
        "your_applications": my_applied,
        "role_family": key["role_family"],
        "seniority": key["seniority"],
        "noised": True,
    }

    # ---- bounded cross-user scan, grouped in Python by user_email ---------- #
    peers: Dict[str, Dict[str, Any]] = {}
    try:
        cursor = _db().tailoring_records.find(
            {"deleted": {"$ne": True}},
            _record_fields(),
        ).limit(_COHORT_SCAN_CAP)
        for r in cursor:
            owner = r.get("user_email")
            if not owner or owner == email:
                continue
            bucket = peers.setdefault(owner, {"records": []})
            bucket["records"].append(r)
    except Exception as e:
        logger.warning("pipeline_intel: cohort scan failed: %s", e)
        return {**base, "insufficient": True, "reason": "scan_failed", "sample_size": 0}

    cohort_apps: List[Dict[str, Any]] = []
    cohort_applied = cohort_callbacks = cohort_peers = 0
    for owner, bucket in peers.items():
        recs = bucket["records"]
        p_applied = sum(1 for r in recs if _furthest_stage(r.get("application") or {}) >= 1)
        if p_applied < _MIN_APPS_PER_PEER:
            continue
        if _cohort_key_for(recs) != key:
            continue
        p_callbacks = sum(1 for r in recs if _furthest_stage(r.get("application") or {}) >= 2)
        cohort_peers += 1
        cohort_applied += p_applied
        cohort_callbacks += p_callbacks
        cohort_apps.extend(r for r in recs if _furthest_stage(r.get("application") or {}) >= 1)

    if cohort_peers < _MIN_COHORT:
        return {
            **base,
            "insufficient": True,
            "reason": "cohort_too_small",
            "sample_size": cohort_peers,
            "min_required": _MIN_COHORT,
        }

    raw_rate = cohort_callbacks / cohort_applied if cohort_applied else 0.0
    # Laplace mechanism: sensitivity of a proportion to one record ≈ 1/n.
    scale = (1.0 / cohort_applied) / _COHORT_EPSILON if cohort_applied else 0.0
    noised_rate = min(1.0, max(0.0, raw_rate + _laplace(scale)))

    return {
        **base,
        "cohort_callback_rate": round(noised_rate, 3),
        "sample_size": cohort_peers,
        "cohort_applications": cohort_applied,
        "strongest_predictor": _strongest_predictor(cohort_apps),
    }


# --------------------------------------------------------------------------- #
# Strongest predictor — honest, non-leaky lift analysis over the cohort.
# --------------------------------------------------------------------------- #
def _feature_high_ats(rec: Dict[str, Any]) -> Optional[bool]:
    """Whether the tailored resume scored a strong ATS overall. Non-leaky: this
    is known at apply time, before any employer response. Returns None when no
    ATS score was recorded (that application abstains from this probe)."""
    overall = (rec.get("ats_scores") or {}).get("overall")
    if not isinstance(overall, (int, float)):
        return None
    return overall >= _STRONG_ATS


# Extendable registry of non-leaky, apply-time feature probes. Leaky signals
# (e.g. recruiter contact, which is usually captured AFTER a callback) are
# deliberately excluded so this never claims false causation.
_PREDICTOR_PROBES = [
    ("high_ats", "a strong ATS score (>= %d)" % _STRONG_ATS, _feature_high_ats),
]


def _strongest_predictor(cohort_apps: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The feature with the largest positive callback lift across the cohort's
    applications, or None when no probe has enough support to be trustworthy.

    Honest by construction: only apply-time (non-leaky) features are probed, each
    arm needs `_MIN_PREDICTOR_SUPPORT` samples, and a non-positive lift returns
    nothing rather than a motivational fiction.
    """
    best: Optional[Dict[str, Any]] = None
    for key, label, probe in _PREDICTOR_PROBES:
        with_n = with_cb = without_n = without_cb = 0
        for rec in cohort_apps:
            flag = probe(rec)
            if flag is None:
                continue
            got_callback = _furthest_stage(rec.get("application") or {}) >= 2
            if flag:
                with_n += 1
                with_cb += 1 if got_callback else 0
            else:
                without_n += 1
                without_cb += 1 if got_callback else 0

        if with_n < _MIN_PREDICTOR_SUPPORT or without_n < _MIN_PREDICTOR_SUPPORT:
            continue
        lift = (with_cb / with_n) - (without_cb / without_n)
        if lift <= 0:
            continue
        if best is None or lift > best["lift"]:
            best = {"feature": key, "label": label, "lift": round(lift, 3)}
    return best


# --------------------------------------------------------------------------- #
# Optional LLM helper — a short, non-desperate follow-up email (Claude PRO).
# Kept separate so the three read APIs stay pure/deterministic. The caller is
# responsible for entitlement-gating + async-dispatching this (it is the only
# LLM spend in this module).
# --------------------------------------------------------------------------- #
_FOLLOWUP_SYSTEM = (
    "You are a candidate writing a brief, professional follow-up email about a job "
    "application. Rules: 6 sentences max; warm and confident, never desperate or "
    "apologetic; reference the specific role and company; restate one concrete, "
    "genuine strength; end with a low-pressure question about timeline or next "
    "steps. Do NOT invent facts, dates, interviews, or referrals that are not "
    "given. Output ONLY the email body as plain text — no subject line, no "
    "placeholders like [Name], no markdown."
)


def draft_followup(application: Dict[str, Any]) -> str:
    """Draft a short, grounded follow-up email for one stale application.

    `application` is a record-shaped dict (company/role from `jd_analysis`, plus
    optional `days_since_last_event`). Returns the email body as plain text, or
    "" on failure. This is the ONLY function here that spends LLM tokens — gate +
    async-dispatch it at the call site.
    """
    cr = _company_role(application) if "record_id" in application else {
        "company": (application.get("company") or "").strip() or "the company",
        "role": (application.get("role") or "").strip() or "the role",
    }
    days = application.get("days_since_last_event")
    since = f"It has been about {days} days since I applied. " if isinstance(days, int) else ""

    prompt = (
        f"Role: {cr['role']}\n"
        f"Company: {cr['company']}\n"
        f"{since}"
        "Write the follow-up email body now."
    )
    try:
        from services.claude_client import claude_json, CLAUDE_PRO
        result = claude_json(
            prompt=prompt,
            system=_FOLLOWUP_SYSTEM,
            model=CLAUDE_PRO,
            max_tokens=600,
            temperature=0.5,
            schema={"email": str},
        )
        return (result or {}).get("email", "").strip()
    except Exception as e:
        logger.warning("pipeline_intel: draft_followup failed: %s", e)
        return ""
