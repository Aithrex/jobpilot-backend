"""Per-POSTING relationship, purpose and availability (contract §C2, §C3).

Pure functions over STORED fields. No network, no LLM, no database: a served
card is classified from what the posting document already carries.

* `relationship` - own_role | named_client | undisclosed_client |
  freelance_task | unknown. `own_role` is the default ONLY for an operating
  employer publishing on a board whose ownership is proven
  (`canonical_employer_id`). A consultancy or staffing firm's INTERNAL role
  (a recruiter, an account manager) is `own_role` too - and its employer
  kind stays `consultancy`/`staffing`: both facts are kept.
  Client placement needs EXPLICIT text ("our client is seeking", "end
  client", "C2C", "W2 through our vendor"). "W2 only" or contract wording
  alone is recorded as evidence and decides nothing: Mozilla posts contract
  roles and CloudIngest posts W2-only roles, and neither is a placement.
* `purpose` - requisition | talent_community | resume_collection | unknown.
* `availability` - verified_open | closed | unverifiable | unknown, with the
  method and the time of the verification it rests on. An undated posting is
  judged by `last_verified_at` / `last_seen_at`, NEVER by `posted_at` (most
  iCIMS, JazzHR and Rippling postings publish no date at all). A 403 / 429 /
  timeout / challenge is `unverifiable`, never `closed`: `verify_liveness`
  closes a posting with reason `unverified` when the crawl stops seeing its
  board, and that is reported as unverifiable, because a board that blocks us
  is a fact about us.

The posting BODY is offloaded to S3 at ingest, so serve time sees the title
and whatever `posting_signals` ingest stored from the full body
(`signals_from_text`, called in `job_inventory_service.normalize_posting`).
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

POSTING_PROFILE_VERSION = "pp-1.0.0"
SIGNALS_VERSION = "ps-1.0.0"

REL_OWN = "own_role"
REL_NAMED = "named_client"
REL_UNDISCLOSED = "undisclosed_client"
REL_TASK = "freelance_task"
REL_UNKNOWN = "unknown"

PURPOSE_REQUISITION = "requisition"
PURPOSE_COMMUNITY = "talent_community"
PURPOSE_RESUME = "resume_collection"
PURPOSE_UNKNOWN = "unknown"

AV_OPEN = "verified_open"
AV_CLOSED = "closed"
AV_UNVERIFIABLE = "unverifiable"
AV_UNKNOWN = "unknown"


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, "") or default))
    except (TypeError, ValueError):
        return default


#: ACCEPTABLE VERIFICATION AGE for strict serving and for submission.
#:
#: A posting counts as `verified_open` only when the crawl re-observed it on
#: its board (or a liveness probe answered) within this many hours. 48h is
#: deliberately inside `job_ingest_worker.VERIFY_STALE_HOURS` (72h, where the
#: backstop CLOSES an unseen posting) and well above the crawl cycle (~2h), so
#: a healthy board is always inside it and a board we have not been able to
#: read for two days is visibly not. Older than this is `unknown` with
#: `reason: verification_stale` - strict mode does not recommend it and the
#: submission gate does not submit to it.
ACCEPTABLE_VERIFICATION_AGE_HOURS = _env_int("EMPLOYER_POLICY_MAX_VERIFICATION_AGE_HOURS", 48)

_CLOSED_STATUSES = ("closed", "expired")
_BLOCKED_OUTCOMES = frozenset({"blocked", "rate_limited", "timeout", "challenge",
                               "unverifiable", "forbidden", "captcha", "error"})

# ---------------------------------------------------------------------------
# Text signals
# ---------------------------------------------------------------------------
_UNDISCLOSED_RE = re.compile(
    r"\b(our client is|our client,|on behalf of (?:our|a|an) client|"
    r"for (?:our|a|an) (?:(?:leading|major|top|large|global|well[- ]known|"
    r"prestigious|fortune \d+|reputed|renowned)\s+){0,3}client\b(?!s)|"
    r"end[- ]client|direct client|confidential client|implementation partner|"
    r"prime vendor|client is (?:seeking|looking|hiring))", re.IGNORECASE)
_NAMED_CLIENT_RE = re.compile(
    r"\b(?i:end[- ]client|client(?: name)?)\s*[:\-–]\s*([A-Z][\w&.,' -]{1,40})")
_C2C_RE = re.compile(r"\b(c2c|corp[- ]?to[- ]?corp|corp2corp)\b", re.IGNORECASE)
_W2_THROUGH_RE = re.compile(
    r"\bw-?2\s+(?:through|via|with)\s+(?:our|a|an|the)?\s*"
    r"(?:vendor|partner|implementation partner|prime|employer of record|agency)",
    re.IGNORECASE)
_W2_ONLY_RE = re.compile(r"\bw-?2\s*(?:only|position|role|candidates|employment)\b",
                         re.IGNORECASE)
_CONTRACT_RE = re.compile(r"\b(contract(?:or)?|1099|contract[- ]to[- ]hire|temp(?:orary)?)\b",
                          re.IGNORECASE)
_TASK_TITLE_RE = re.compile(
    r"\b(ai (?:trainer|tutor|rater|evaluator)|data annotat\w*|annotator|"
    r"rlhf|tasker|freelance|gig|per[- ]task|task[- ]based|"
    r"(?:evaluate|rate|review|rank)\w*\s+ai\b)", re.IGNORECASE)
_TASK_TEXT_RE = re.compile(
    r"\b((?:evaluate|rate|review|rank)\w*\s+(?:ai|model|llm|chatbot)\w*\s+"
    r"(?:responses|outputs|answers|generations)|paid per task|per[- ]task (?:pay|rate)|"
    r"ai trainer|annotation tasks|freelance (?:contract|opportunity|project)|"
    r"flexible tasks)\b", re.IGNORECASE)
_HOURLY_TITLE_RE = re.compile(r"(\$\s?\d+\s*(?:/|per)\s*(?:hr|hour))", re.IGNORECASE)
_RESUME_COLLECTION_RE = re.compile(
    r"\b(general application|general interest|open application|"
    r"spontaneous application|unsolicited application|resume drop|"
    r"submit your (?:resume|cv)|don'?t see (?:a|the) (?:role|position|job)|"
    r"not seeing (?:a|the) (?:role|position))", re.IGNORECASE)
_COMMUNITY_TITLE_RE = re.compile(
    r"\b(talent (?:community|pool|network|pipeline)|join our talent|"
    r"future (?:opportunit\w*|openings|consideration|roles)|"
    r"expression of interest|evergreen|pipeline (?:requisition|role|posting))",
    re.IGNORECASE)
_COMMUNITY_TEXT_RE = re.compile(
    r"\b(evergreen (?:posting|position|requisition|role|opening)|"
    r"this (?:is|posting is) (?:not )?(?:an? )?(?:talent (?:community|pool)|pipeline) "
    r"(?:posting|requisition|role)|not (?:an?|tied to an?) (?:active|current|specific) "
    r"(?:opening|vacancy|requisition)|for future (?:openings|opportunities|consideration))",
    re.IGNORECASE)
_INTERNAL_TITLE_RE = re.compile(
    r"\b(recruiter|recruiting coordinator|talent acquisition|sourcer|"
    r"account (?:manager|executive)|business development|sales|"
    r"human resources|hr (?:manager|generalist|business partner)|payroll|"
    r"accountant|finance|marketing|office manager|internal)\b", re.IGNORECASE)


def signals_from_text(title: Any, text: Any = "") -> List[str]:
    """Reason codes from a title + body. Stored at ingest as `posting_signals`.

    Codes, never prose: `undisclosed_client`, `named_client`, `c2c`,
    `w2_through_vendor`, `w2_only`, `contract_wording`, `task_work`,
    `talent_community`, `resume_collection`.
    """
    t = str(title or "")
    body = str(text or "")[:40000]
    out: List[str] = []

    def add(code: str) -> None:
        if code not in out:
            out.append(code)

    both = t + "\n" + body
    if _UNDISCLOSED_RE.search(both):
        add("undisclosed_client")
    m = _NAMED_CLIENT_RE.search(both)
    if m:
        name = m.group(1).strip().lower()
        if name.startswith(("confidential", "undisclosed", "n/a", "tbd")):
            add("undisclosed_client")
        else:
            add("named_client")
    if _C2C_RE.search(both):
        add("c2c")
    if _W2_THROUGH_RE.search(both):
        add("w2_through_vendor")
    if _W2_ONLY_RE.search(both):
        add("w2_only")
    if _CONTRACT_RE.search(both):
        add("contract_wording")
    if _TASK_TITLE_RE.search(t) or _TASK_TEXT_RE.search(body):
        add("task_work")
    if _RESUME_COLLECTION_RE.search(t):
        add("resume_collection")
    if _COMMUNITY_TITLE_RE.search(t) or _COMMUNITY_TEXT_RE.search(body):
        add("talent_community")
    return out


def _row_text(row: Dict[str, Any]) -> str:
    return str(row.get("description_text") or row.get("description") or
               row.get("job_description") or "")


def _row_title(row: Dict[str, Any]) -> str:
    return str(row.get("title_raw") or row.get("title") or row.get("job_title") or "")


def row_signals(row: Dict[str, Any]) -> List[str]:
    stored = [str(s) for s in (row.get("posting_signals") or []) if s]
    live = signals_from_text(_row_title(row), _row_text(row))
    return stored + [s for s in live if s not in stored]


# ---------------------------------------------------------------------------
# Relationship + purpose
# ---------------------------------------------------------------------------
def classify_posting(row: Dict[str, Any], employer_kind: str) -> Dict[str, Any]:
    """`posting_profile` for one row, given its employer's business-model kind."""
    title = _row_title(row)
    sig = row_signals(row)
    evidence: List[str] = []

    rel = REL_UNKNOWN
    if "task_work" in sig:
        rel = REL_TASK
        evidence.append("task_work_wording")
    elif employer_kind == "talent_marketplace" and _HOURLY_TITLE_RE.search(title) \
            and not _INTERNAL_TITLE_RE.search(title):
        rel = REL_TASK
        evidence.append("marketplace_hourly_contract")
    elif "named_client" in sig:
        rel = REL_NAMED
        evidence.append("client_named_in_posting")
    elif {"undisclosed_client", "c2c", "w2_through_vendor"} & set(sig):
        rel = REL_UNDISCLOSED
        evidence.extend(s for s in ("undisclosed_client", "c2c", "w2_through_vendor")
                        if s in sig)
    elif employer_kind == "operating" and row.get("canonical_employer_id"):
        rel = REL_OWN
        evidence.append("operating_employer_owned_board")
    elif employer_kind in ("staffing", "consultancy", "mixed") \
            and _INTERNAL_TITLE_RE.search(title):
        rel = REL_OWN
        evidence.append("internal_function_title")
    for s in ("w2_only", "contract_wording"):
        if s in sig:
            evidence.append(s + "_not_placement_evidence")

    if "resume_collection" in sig:
        purpose = PURPOSE_RESUME
        evidence.append("resume_collection_wording")
    elif "talent_community" in sig:
        purpose = PURPOSE_COMMUNITY
        evidence.append("talent_community_wording")
    elif title and (row.get("source_job_id") or row.get("job_id") or row.get("id")):
        purpose = PURPOSE_REQUISITION
    else:
        purpose = PURPOSE_UNKNOWN
    return {"relationship": rel, "purpose": purpose, "evidence": evidence[:8],
            "version": POSTING_PROFILE_VERSION}


# ---------------------------------------------------------------------------
# Availability
# ---------------------------------------------------------------------------
def _dt(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value.strip():
        try:
            d = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    return None


def _iso(d: Optional[datetime]) -> Optional[str]:
    return d.isoformat() if d else None


def availability(row: Dict[str, Any], *, now: Optional[datetime] = None,
                 max_age_hours: Optional[int] = None) -> Dict[str, Any]:
    """`availability` block from stored timestamps. Never a network call."""
    now = now or datetime.now(timezone.utc)
    max_age = max_age_hours or ACCEPTABLE_VERIFICATION_AGE_HOURS
    status = str(row.get("status") or "").lower()
    last_verified = _dt(row.get("last_verified_at"))
    last_seen = _dt(row.get("last_seen_at"))
    probed = _dt(row.get("liveness_probed_at"))
    strikes = int(row.get("liveness_dead_strikes") or 0) if \
        str(row.get("liveness_dead_strikes") or "0").isdigit() else 0

    def out(state: str, method: str, at: Optional[datetime], reason: str) -> Dict[str, Any]:
        age = round((now - at).total_seconds() / 3600.0, 1) if at else None
        return {"state": state, "method": method, "verified_at": _iso(at),
                "age_hours": age, "reason": reason,
                "max_age_hours": max_age}

    if status in _CLOSED_STATUSES or (row.get("active") is False and status != "active"):
        reason = str(row.get("closed_reason") or "closed")
        if reason == "unverified":
            # The backstop closed it because the crawl stopped seeing it -
            # usually a blocked or failing board. Not proof of closure.
            return out(AV_UNVERIFIABLE, "none", last_verified, "unverified_by_crawl")
        method = "liveness_probe" if reason == "link-dead" else (
            "listing_seen" if reason in ("not-seen", "not_seen") else "none")
        return out(AV_CLOSED, method, _dt(row.get("closed_at")) or last_verified, reason)

    valid_through = _dt(row.get("valid_through") or row.get("posting_closes_at")
                        or row.get("job_offer_expiration_datetime_utc"))
    if valid_through is not None and valid_through <= now:
        return out(AV_CLOSED, "listing_seen", valid_through, "valid_through_passed")

    # The freshest POSITIVE observation. `posted_at` is never one of them.
    candidates = [(last_verified, "listing_seen"), (last_seen, "listing_seen")]
    if probed is not None and strikes == 0:
        candidates.append((probed, "liveness_probe"))
    candidates = [(d, m) for d, m in candidates if d is not None]
    best = max(candidates, key=lambda c: c[0]) if candidates else None

    outcome = str(row.get("liveness_outcome") or row.get("verification_state") or "").lower()
    if outcome in _BLOCKED_OUTCOMES:
        blocked_at = _dt(row.get("liveness_outcome_at")) or probed
        if best is None or (blocked_at and blocked_at >= best[0]):
            return out(AV_UNVERIFIABLE, "liveness_probe", best[0] if best else None,
                       "probe_" + outcome)
    if best is None:
        return out(AV_UNKNOWN, "none", None, "no_verification_recorded")
    at, method = best
    if (now - at).total_seconds() <= max_age * 3600:
        return out(AV_OPEN, method, at, "recently_seen")
    return out(AV_UNKNOWN, method, at, "verification_stale")
