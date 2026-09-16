"""The authenticated feed tabs, served from the canonical inventory.

`workday_jobs_service.run_workday_jobs_search` and
`career_pages_service.run_career_pages_search` each answer one tab by crawling
their whole catalog live: 1013 Workday tenants crossed with up to 6 query terms,
and 1309 career-page companies, inside the user's request, against a 210s
budget. That is the same fan-out ADR-003 removed from the guest path.

This module answers those tabs from `job_postings` instead. It is deliberately
a SEPARATE module rather than an edit inside those two functions:

* the legacy path stays byte-for-byte intact as a fallback, so a cold shard or
  a bad deploy is one env var away from the old behaviour;
* the payload contract is asserted in one place instead of two;
* scoring moves onto the shared engine (below) without having to unpick the
  two legacy scorers first.

Scoring
-------
Rows are scored with `fit_score`, the ADR-002 engine that already serves the
guest path, using the requirement profile the crawler extracted. The legacy
`_score_jobs` in each service cannot be reused here: it reads `description`,
and inventory rows deliberately carry no JD text (bodies live in S3). Using
`fit_score` is also what ADR-002 asked for - "a single versioned engine, shared
by guest and authenticated paths" - so selection and display stop disagreeing.

The tab's own presentation helpers (`_attach_feasibility`, sponsorship,
window counts) are reused as-is, so what changes is where candidates come from
and how they are scored, not how they are rendered.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

SOURCE_WORKDAY = "workday"
SOURCE_CAREER = "career-pages"
# Oracle and iCIMS are ingested under their own source keys (ADR-004 shards)
# but they belong on the career-pages tab: they are "not Workday". Querying
# only `career-pages` would hide every ORC/iCIMS requisition from seekers.
CAREER_FEED_SOURCES = ("career-pages", "oracle", "icims")

# How many postings the ranking stage considers.
#
# Retrieval is `(posted_at DESC)`, so this is not a sample of the market - it
# is a TIME SLICE of it, and the width of that slice is whatever the crawler
# has ingested most recently. At 1,500 it was about ONE DAY of a thirty-day
# window: measured on one live account, 1,500 candidates covered 2 days and
# surfaced 72 Amazon requisitions out of 5,794 eligible postings. The user
# read the result as "Amazon shows while the search runs, then disappears",
# which is exactly what a slice that moves under you looks like.
#
# 4,000 covers ~7 days and 182 Amazon requisitions for the same account, at
# ~7.0s per source against ~5.2s (the jobs Lambda's budget is 60s and the
# three sources are fetched in parallel by the client). The cost is almost
# entirely `_score_rows` at ~2.2 ms/row - the `_in_family` filter that runs
# after it removes ~0%, so there is no cheaper ordering of the stages to
# find. Raising it further is a straight latency trade: 6,000 covers 7 days
# at ~10s, 10,000 covers 13 days at ~14.5s.
CANDIDATE_LIMIT = int(os.getenv("FEED_INVENTORY_CANDIDATES", "4000") or 4000)
# Below this the inventory is treated as not yet warm for the requested slice
# and the caller falls back to the live crawl. A thin tab is a worse answer
# than a slow one.
MIN_ROWS = int(os.getenv("FEED_INVENTORY_MIN_ROWS", "25") or 25)
# The authenticated tabs are browse surfaces, not the guest freshness promise
# (ADR-001 keeps the strict 7-day gate for guests only).
WINDOW_DAYS = int(os.getenv("FEED_INVENTORY_WINDOW_DAYS", "30") or 30)
GLOBAL_KEEP = 1500

#: Collapse (employer, title) across cities into one card. ON by default; the
#: flag exists so a feed regression can be reverted from the console rather
#: than by a deploy.
LOCATION_ROLLUP = (os.getenv("FEED_LOCATION_ROLLUP", "1") or "1").strip().lower() \
    not in ("0", "false", "no", "off")
#: How many other cities one card names before it just reports the count.
LOCATION_ROLLUP_CAP = int(os.getenv("FEED_LOCATION_ROLLUP_CAP", "5") or 5)


def enabled() -> bool:
    """`FEED_MATCH_MODE=scan` is the rollback to the legacy live crawl."""
    return (os.getenv("FEED_MATCH_MODE") or "inventory").strip().lower() != "scan"


# ---------------------------------------------------------------------------
# Inventory doc -> tab row
# ---------------------------------------------------------------------------

#: Display names for sources that predate the provider registry, or that are
#: not providers at all.
_ATS_LABEL_STATIC = {
    "greenhouse": "Greenhouse", "lever": "Lever", "ashby": "Ashby",
    "smartrecruiters": "SmartRecruiters", "workable": "Workable",
    "recruitee": "Recruitee", "workday": "Workday",
    "neogov": "GovernmentJobs",
    "amazon": "Amazon Jobs",
}


def _ats_key(vendor: str) -> str:
    """The form `job_postings.ats` is STORED in.

    `job_inventory_service._safe_key` keeps only `[a-z0-9-]`, so the vendor key
    `oracle_orc` lands in the document as `oracle-orc`. A label table keyed on
    the vendor form therefore missed every Oracle row and rendered the badge as
    the generic "Career Page".
    """
    import re
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-",
                                     str(vendor or "").lower())).strip("-")


def _ats_labels() -> Dict[str, str]:
    """`{stored ats key: display name}` for every vendor we can crawl.

    Derived from the provider registry rather than hand-listed, so a vendor
    added there is labelled here without a second edit — the same reason
    ADR-004 §4.1 derives the crawler's tables. A hand-written table is how
    Oracle ended up mislabelled and how UKG, Paylocity, Rippling, Cornerstone
    and JazzHR would have been.
    """
    labels = dict(_ATS_LABEL_STATIC)
    try:
        from services.ats_providers import registry as ats_registry
        for provider in ats_registry.all_providers():
            labels.setdefault(_ats_key(provider.vendor),
                              provider.label or provider.vendor)
            labels[_ats_key(provider.vendor)] = provider.label or provider.vendor
    except Exception:  # pragma: no cover - the feed must render regardless
        pass
    return labels


_ATS_LABEL = _ats_labels()


def _row_from_posting(doc: Dict[str, Any], *, tz_offset: int) -> Optional[Dict[str, Any]]:
    """One `job_postings` document in the shape the tab payload already uses.

    Field-for-field compatible with `career_pages_service._row` and the Workday
    tab's row builder, so the frontend renders it unchanged.
    """
    from services.workday_jobs_service import _days_ago

    # A posting whose VENDOR publishes no date is still a real opening.
    #
    # This used to `return None` for it, and combined with the retrieval gate
    # that meant 9,456 active postings - 7,725 of them iCIMS, including all 275
    # of PepsiCo's - were crawled, stored, and unreachable by any user. iCIMS
    # search results are HTML with the date only on the detail page, so this
    # was not a handful of odd rows, it was most of a source.
    #
    # ADR-001 says never CLAIM a date we do not have. It does not say hide the
    # job. So the row is kept, dated by `first_seen_at` - when the crawler
    # actually observed it live, a real lower bound rather than an assumption -
    # and labelled `date_confidence: "unknown"` with a posted_text that says so,
    # rather than being given a posting date it never published. `days_ago`
    # stays None, which every consumer already treats as "sorts last".
    posted_at = doc.get("posted_at")
    dated = isinstance(posted_at, datetime)
    if not dated:
        posted_at = doc.get("first_seen_at")
        if not isinstance(posted_at, datetime):
            return None
    aware = posted_at if posted_at.tzinfo else posted_at.replace(tzinfo=timezone.utc)
    iso = aware.isoformat()
    days = _days_ago(iso, tz_offset)
    if days is None:
        return None
    seen_days = days
    if not dated:
        days = None

    # `<vendor>-<tenant>-<site>:<requisition>` -> `(tenant, requisition)`.
    # Internal, stripped from the payload beside `_requirements`.
    req_key = ""
    tenant_raw = str(doc.get("tenant") or "").strip().lower()
    source_job_id = str(doc.get("source_job_id") or "")
    if tenant_raw and ":" in source_job_id:
        requisition = source_job_id.rsplit(":", 1)[-1].strip()
        if requisition:
            req_key = f"req|{tenant_raw}|{requisition}"

    locations = doc.get("locations") or []
    first = locations[0] if isinstance(locations, list) and locations else {}
    location = str(first.get("raw") or "") if isinstance(first, dict) else str(first)
    country = str(first.get("country") or "") if isinstance(first, dict) else ""
    work_mode = doc.get("work_mode") or ""
    ats = doc.get("ats") or ""
    url = str(doc.get("canonical_url") or "")
    tenant = doc.get("tenant") or ""

    from services.career_pages_catalog import is_marketplace_board

    row = {
        # The canonical inventory id, so a tab row can be tailored against and
        # pinned by the same reference the guest path and applications use.
        "job_id": doc.get("job_id"),
        "title": str(doc.get("title_raw") or "")[:200],
        "company": str(doc.get("employer_name") or ""),
        # The same value under the key `employer_display` reads, plus the
        # canonical id it resolves against. Both are needed for a card to be
        # shown under the EMPLOYER's name instead of the legal entity the
        # board printed — see `apply_display_names` below.
        "employer_name": str(doc.get("employer_name") or ""),
        "canonical_employer_id": str(doc.get("canonical_employer_id") or ""),
        "tenant": tenant,
        "_req_key": req_key,
        "ats": ats,
        "industry": doc.get("industry") or "other",
        "location": location or "Not specified",
        "country_hint": country.lower(),
        "country_code": "US" if country.lower() in ("us", "usa", "united states") else None,
        "date_posted": iso if dated else "",
        "days_ago": days,
        # What the row can honestly say about its age. A dated row gets the
        # usual "2 days ago"; an undated one says when we first SAW it, which
        # is a different and weaker claim, and is labelled as such.
        "posted_text": (_posted_text(days) if dated
                        else f"First seen {_posted_text(seen_days).lower()}"),
        "date_confidence": ("exact" if dated else "unknown"),
        "first_seen_days_ago": seen_days,
        "apply_link": url,
        "url": url,
        "description": "",          # body lives in S3; scoring uses requirements
        # Was hard-coded "" - nothing upstream stored a salary, so the card had
        # nothing to show and the filter had nothing to compare. `salary` is the
        # display string; `salary_min_annual` is what a numeric filter reads,
        # and it is None (not 0) when undisclosed, so "unknown" and "unpaid"
        # stay distinguishable.
        "salary": str((doc.get("compensation") or {}).get("display") or ""),
        "salary_min_annual": (doc.get("compensation") or {}).get("annual_min"),
        "salary_max_annual": (doc.get("compensation") or {}).get("annual_max"),
        "salary_currency": (doc.get("compensation") or {}).get("currency") or "",
        "salary_confidence": (doc.get("compensation") or {}).get("confidence") or "",
        "employment_type": doc.get("employment_type") or "",
        "is_remote": work_mode == "remote",
        "h1b_sponsor": False,
        "contract_friendly": False,
        "source": _ATS_LABEL.get(ats, "Workday" if doc.get("source") == SOURCE_WORKDAY else "Career Page"),
        "seniority": doc.get("seniority") or "",
        # Canonical identity, so the client never has to invent one from
        # display strings (which is what produced duplicate cards).
        "dedupe_group_id": doc.get("dedupe_group_id") or doc.get("job_id") or "",
        "variant_group_id": doc.get("variant_group_id") or "",
        "tech": doc.get("tech") or None,
        "_requirements": doc.get("requirements") or None,
    }
    if is_marketplace_board(row) or str(doc.get("board_kind") or "").lower() == "marketplace":
        row["board_kind"] = "marketplace"
    return row


def _posted_text(days: Optional[int]) -> str:
    if days is None:
        return ""
    if days <= 0:
        return "Today"
    if days == 1:
        return "Yesterday"
    return f"{days} days ago"


# ---------------------------------------------------------------------------
# Retrieval + scoring
# ---------------------------------------------------------------------------

def _role_families(titles: Iterable[str], skills: Iterable[str],
                   resume_families: Optional[Iterable[str]] = None) -> List[str]:
    """Families to retrieve for.

    Errs wide, exactly as the guest path does: retrieval that is too tight
    returns nothing and reads as an empty market, while the scorer is good at
    pushing a poor fit down. With no titles and no resume families at all we
    return [] and retrieve unfiltered, which is the correct reading of "show me
    everything".

    The RESUME's own families are unioned in because titles alone were far too
    tight. Measured on the live inventory: a "cloud engineer / devops engineer /
    site reliability engineer" search classifies to the single family `devops`,
    which is 565 of 53,730 active postings — 366 Workday candidates and 199
    career-page candidates, of which ~150 survived the geo filter. That is the
    whole reason the feed looked thin and mediocre. The same user's resume also
    classifies as `software_engineering`; unioning it takes retrieval to 2,528
    and 1,952 respectively, a ~7x wider pool for the scorer to rank. Widening
    RETRIEVAL cannot lower precision here — ordering is decided by `fit_score`,
    and an off-family posting scores below the ones already being shown.
    """
    from services import fit_score

    out: List[str] = []
    for value in list(titles or []):
        fam = fit_score.classify_role(str(value or ""))
        if fam and fam not in out:
            out.append(fam)
    # Only widen a filter that already exists. With no titles the caller wants
    # everything, and injecting the resume's families there would NARROW the
    # feed instead of widening it.
    if out:
        for fam in (resume_families or []):
            if fam and fam not in out:
                out.append(str(fam))
    return out


def _resume_families(profile: Optional[Dict[str, Any]]) -> Set[str]:
    """Union of role families across every resume in the scoring set."""
    families: Set[str] = set()
    for entry in (profile or {}).get("_profiles") or []:
        families |= ((entry.get("profile") or {}).get("families") or set())
    return families


def _build_profile(user_email: str, resume_skills: List[str],
                   resume_years: Optional[int]) -> Optional[Dict[str, Any]]:
    """The scorer's resume profile, or None when there is nothing to score on.

    Split out of `_score_rows` so RETRIEVAL can consult the resume's role
    families before the query runs (see `_role_families`) without loading and
    re-analysing the resume a second time.
    """
    from services import fit_score

    # EVERY parsed resume, not just users.active_resume_id. A user who keeps a
    # Backend and a Cloud resume was scored on whichever was pinned, so the
    # other file's skill coverage was invisible to the feed; `score_one_best`
    # takes the best verdict across the set. Profiles are memoized on the
    # resume-set fingerprint, so this costs one Mongo read per resume CHANGE,
    # not per request.
    profiles: List[Dict[str, Any]] = []
    if user_email:
        try:
            profiles = fit_score.load_profiles_for_user(user_email)
        except Exception as exc:
            logger.warning("inventory_feed: could not load resumes for scoring: %s",
                           type(exc).__name__)
            profiles = []

    if profiles:
        return {"_profiles": profiles}
    if resume_skills:
        return {"_profiles": [{
            "resume_id": "", "filename": "",
            "profile": fit_score._profile_from_skills(
                resume_skills, None, resume_years),
        }]}
    return None


def _score_rows(rows: List[Dict[str, Any]], user_email: str,
                resume_skills: List[str], resume_years: Optional[int],
                profile: Optional[Dict[str, Any]] = None) -> None:
    """Score in place with the shared engine.

    Writes `match_score` / `matched_skills` / `missing_skills` - the three
    fields the tab UI and `_rank` already consume - plus the versioned `fit`
    block the guest cards carry, so both surfaces explain a score the same way.

    `profile` may be passed by a caller that already built it (`build_feed`
    does, for retrieval); omitted, it is built here exactly as before.
    """
    from services import fit_score

    if profile is None:
        profile = _build_profile(user_email, resume_skills, resume_years)
    if profile is None:
        # No resume and no skills: there is nothing to score against, so leave
        # every row unscored rather than invent a number. `_rank` then falls
        # back to recency, which is the honest ordering.
        for row in rows:
            row["match_score"] = 0
            row["matched_skills"] = []
            row.setdefault("missing_skills", [])
        return

    profiles = profile.get("_profiles") or []
    for row in rows:
        verdict = fit_score.score_one_best(
            profiles, jd_text="", title=row.get("title") or "",
            location=row.get("location") or "",
            job={"requirements": row.get("_requirements")},
        )
        if verdict is None:
            row["match_score"] = 0
            row["matched_skills"] = []
            row.setdefault("missing_skills", [])
            continue
        if verdict.get("matched_resume"):
            row["matched_resume"] = verdict["matched_resume"]
        row["match_score"] = verdict["match_score"]
        row["matched_skills"] = verdict.get("matched_skills") or []
        row["missing_skills"] = (verdict.get("missing_top") or [])[:5]
        row["fit"] = {
            "score_version": verdict.get("score_version"),
            "band": verdict.get("band"),
            "confidence": verdict.get("confidence"),
            "components": verdict.get("component_scores") or {},
            "explanation": str(verdict.get("explanation") or "")[:400],
            "missing_requirements": (verdict.get("missing_requirements") or [])[:4],
            # fit-v2.1. Without these the card cannot tell a candidate WHY a
            # role is or is not reachable: `eligibility` is a different
            # question from `score` and no number expresses it, `evidence_
            # breadth` says how much of the frame had evidence, and
            # `role_alignment.detail` is written to be read aloud.
            "eligibility": verdict.get("eligibility"),
            "ineligible_reasons": (verdict.get("ineligible_reasons") or [])[:4],
            "evidence_breadth": verdict.get("evidence_breadth"),
            "role_alignment": verdict.get("role_alignment") or {},
            # The FULL constraint list, with `status` and `hard_requirement`
            # intact, because the two useful readings need different subsets:
            # a requirement to CHECK is `hard_requirement` + `status:
            # unknown`, an ineligibility reason is `status: missing`, and a
            # `met` one is worth showing as reassurance. Pre-filtering here
            # would decide that for every surface at once.
            "hard_constraints": [
                {"type": c.get("type"), "status": c.get("status"),
                 "hard_requirement": bool(c.get("hard_requirement")),
                 "detail": str(c.get("detail") or "")[:200]}
                for c in (verdict.get("hard_constraints") or [])
            ][:6],
        }


# Parenthetical / trailing fragments that are POSTING NOISE rather than part of
# the role. Only these are stripped for the dedupe key - see `_title_key`.
_TITLE_NOISE = re.compile(
    r"\b(remote|hybrid|on[- ]?site|onsite|work from home|wfh|full[- ]?time|part[- ]?time|"
    r"contract|temporary|temp|intern(ship)?|permanent|perm|"
    r"m/w/d|f/m/d|w/m/d|d/f/m|h/f|all genders|any gender)\b",
    re.IGNORECASE,
)
# "Req ID: 4471", "(Job ID 88213)", "#R-102934" - unique per requisition, so
# leaving them in guarantees two copies of one opening never collapse.
_TITLE_REQ_ID = re.compile(r"\b(job|req|requisition)?\s*id[:#\s]*[\w-]+\b", re.IGNORECASE)
_TITLE_TRAILING_CODE = re.compile(r"[#(\[]\s*[a-z]{0,3}[-_]?\d{3,}\s*[)\]]?", re.IGNORECASE)


def _title_key(title: Any) -> str:
    """Title reduced to the part that identifies the ROLE, for dedupe only.

    Deliberately NOT `job_inventory_service.normalize_title`. That one strips
    every parenthetical, which is correct for its job (grouping and retrieval)
    but destructive here: it maps "Software Engineer (Backend)" and "Software
    Engineer (Frontend)" both to "software engineer", so collapsing on it would
    silently HIDE a real second opening. Losing a job from the feed is a worse
    failure than showing one twice.

    So strip only what is provably noise - work mode, employment type, gender
    tags, and per-requisition ids - and keep every other qualifier, which means
    two genuinely different roles stay two rows.
    """
    t = str(title or "").lower()
    t = _TITLE_REQ_ID.sub(" ", t)
    t = _TITLE_TRAILING_CODE.sub(" ", t)
    t = _TITLE_NOISE.sub(" ", t)
    # Brackets that are now empty (or only punctuation) contributed nothing.
    t = re.sub(r"[(\[{]\s*[-,/|]*\s*[)\]}]", " ", t)
    t = re.sub(r"[^a-z0-9+#/ .-]+", " ", t)
    t = re.sub(r"[\s.\-/]+", " ", t)
    return t.strip()


def _collapse_locations(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """One card per (employer, title), with the other cities rolled up.

    `_dedupe` above keys on (employer, title, LOCATION), which is right for
    "the same opening listed twice" and leaves the other half of the problem
    untouched: an employer that opens one requisition PER STORE. Measured on
    the live inventory (2026-09-02) that is 33% of 229,965 active rows — CVS
    "Pharmacy Technician" is 1,148 separate, genuinely distinct requisitions,
    Dick's "Teammate - Sales" 602, Dollar Tree "Assistant Manager I" 592. None
    of them are data errors, and all of them read as spam in a feed.

    So they are collapsed for DISPLAY, not deleted: the best-scoring copy
    represents the group and carries `location_count` plus `other_locations`,
    which is the "Olathe, KS +1 more" affordance. The user can still reach
    every city, and one employer can no longer own the whole page.

    Deliberately runs AFTER `_score_rows`: the representative has to be the
    best-scoring copy, which is not knowable before scoring.
    """
    from services import job_inventory_service as inv

    groups: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    order: List[Tuple[str, str]] = []
    for row in rows:
        key = (inv.employer_id_of(row.get("company")), _title_key(row.get("title")))
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(row)

    out: List[Dict[str, Any]] = []
    for key in order:
        members = groups[key]
        if len(members) == 1:
            out.append(members[0])
            continue
        # Highest score wins; ties go to the freshest, so the age we print
        # stays the age of the copy we are showing.
        members.sort(key=lambda r: (
            -(r.get("match_score") or 0),
            r.get("days_ago") if r.get("days_ago") is not None else 99))
        lead = members[0]
        others: List[str] = []
        seen = {str(lead.get("location") or "").strip().lower()}
        for row in members[1:]:
            loc = str(row.get("location") or "").strip()
            if not loc or loc.lower() in seen:
                continue
            seen.add(loc.lower())
            others.append(loc)
        lead["location_count"] = len(seen)
        if others:
            lead["other_locations"] = others[:LOCATION_ROLLUP_CAP]
        out.append(lead)
    return out


def _dedupe(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Collapse the same opening, PRESERVING the provenance of what collapsed.

    Two changes from the previous client-only rule.

    **Identity comes from the inventory, not from display strings.** The old
    key was `(employer_id_of(company), title_key(title), location_key)`
    recomputed on the card. That is a guess made from three fields that were
    themselves normalized for display, and it merged rows it should not (two
    genuinely different reqs at one employer with the same shortened title)
    while missing rows it should have merged (the same requisition reached via
    two sources, whose URLs match but whose company strings do not).
    `dedupe_group_id` is computed ONCE at ingest from the strongest identity
    available - normalized requisition URL, then (employer, ATS, req id), then
    content hash - and every reader now agrees with it.

    **The losers are kept.** Dropping them destroyed information a seeker
    needs: which board this reached us through, and whether the role is open
    in more than one city. Each surviving card carries `source_variants`, so
    "also on Greenhouse" and "also in Austin, TX" are answerable.

    The freshest copy still wins the card, so the displayed age stays honest;
    a DIRECT employer source outranks a marketplace board at equal age,
    because that is the ordering rule the rest of the feed already uses.
    """
    best: Dict[str, Dict[str, Any]] = {}
    variants: Dict[str, List[Dict[str, Any]]] = {}
    order: List[str] = []

    def _variant_of(row: Dict[str, Any]) -> Dict[str, Any]:
        return {"job_id": row.get("job_id"), "source": row.get("source"),
                "ats": row.get("ats"), "location": row.get("location"),
                "url": row.get("url") or row.get("apply_link"),
                "days_ago": row.get("days_ago")}

    def _better(new: Dict[str, Any], prev: Dict[str, Any]) -> bool:
        new_direct = new.get("board_kind") != "marketplace"
        prev_direct = prev.get("board_kind") != "marketplace"
        if new_direct != prev_direct:
            return new_direct
        na, pa = new.get("days_ago"), prev.get("days_ago")
        if na is None:
            return False
        return pa is None or na < pa

    # Lazy, matching the rest of this module: the inventory service pulls in
    # pymongo and the feed is imported on paths that never touch Mongo.
    from services import job_inventory_service as inv

    def _key_of(row: Dict[str, Any], index: int) -> str:
        """Stored identity when we have it, the display-string rule otherwise.

        The fallback is not decoration. 227,914 active postings were written
        before `dedupe_group_id` existed and carry none until the backfill
        (docs/ops/tech-feed-rollout.md step 3) reaches them. Without this
        branch every one of those rows becomes its own group on the day this
        ships, and the feed shows every duplicate it currently collapses.
        """
        # SAME TENANT + SAME REQUISITION ID is the same job, whatever name
        # the board printed on it. JPMorgan's Oracle instance serves one
        # requisition (`210786394`) from three CX sites, and each copy is
        # stamped a different employer - "JPMorgan Chase", "NUTMEG CONTAINER
        # CORPORATION", "CHASE FAMILY LIMITED PARTNERSHIP NO. 5" - so all
        # three carry different `dedupe_group_id`s and nothing collapsed
        # them. Sorted by Best match they were the top THREE cards, one job
        # shown three times. Platform-wide this is 11,837 redundant copies
        # across 9,436 (tenant, requisition) groups, 4,216 of them wearing
        # more than one employer name.
        req = str(row.get("_req_key") or "")
        if req:
            return req
        stored = str(row.get("dedupe_group_id") or "")
        if stored:
            return stored
        location = str(row.get("location") or "").strip()
        return "|".join((
            "legacy",
            inv.employer_id_of(row.get("company")),
            _title_key(row.get("title")),
            inv.normalize_location(location)["key"] if location else "",
        ))

    for index, row in enumerate(rows):
        key = _key_of(row, index)
        prev = best.get(key)
        if prev is None:
            best[key] = row
            variants[key] = []
            order.append(key)
            continue
        if _better(row, prev):
            variants[key].append(_variant_of(prev))
            best[key] = row
        else:
            variants[key].append(_variant_of(row))

    out: List[Dict[str, Any]] = []
    for key in order:
        row = best[key]
        vs = variants.get(key) or []
        if vs:
            row["source_variants"] = vs[:8]
            row["duplicate_count"] = len(vs)
        out.append(row)
    return out


def _window_counts(jobs: List[Dict[str, Any]]) -> Dict[str, int]:
    counts = {"today": 0, "d1": 0, "d3": 0, "d7": 0, "d30": 0}
    for job in jobs:
        days = job.get("days_ago")
        if days is None:
            continue
        if days == 0:
            counts["today"] += 1
        for key, limit in (("d1", 1), ("d3", 3), ("d7", 7), ("d30", 30)):
            if days <= limit:
                counts[key] += 1
    return counts


def build_feed(source: str, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Serve one feed tab from inventory, or None when it cannot.

    Returns None (rather than a thin payload) when the requested slice has too
    few postings, so the caller falls back to the live crawl. That is the only
    honest behaviour while shards are still warming: an empty Workday tab would
    read as "there are no jobs", which is false.
    """
    from services import job_inventory_service as inv
    from services.workday_jobs_service import (
        _attach_feasibility, _attach_sponsorship, _clamp_tz_offset,
        _load_resume_profile,
    )

    started = datetime.now(timezone.utc)
    titles = [str(t).strip() for t in (payload.get("titles") or []) if str(t or "").strip()]
    industries = [str(i).strip() for i in (payload.get("industries") or []) if str(i or "").strip()]
    companies = [str(c).strip() for c in (payload.get("companies") or []) if str(c or "").strip()]
    us_only = bool(payload.get("us_only", True))
    remote_only = bool(payload.get("remote_only", False))
    try:
        min_salary = float(payload.get("min_salary") or 0) or 0.0
    except (TypeError, ValueError):
        min_salary = 0.0
    # Unknown remains unknown, and the caller decides what to do with it.
    # Default True: most postings publish no band, and treating "unpriced" as
    # "fails the filter" hides the market instead of narrowing it. Only an
    # explicit False drops them, and the payload echoes the choice back so the
    # surface can say which it did.
    include_unknown_salary = payload.get("include_unknown_salary")
    include_unknown_salary = (True if include_unknown_salary is None
                              else bool(include_unknown_salary))
    user_email = str(payload.get("user_email") or "")
    tz_offset = _clamp_tz_offset(payload.get("tz_offset_minutes"))

    skills, resume_years = _load_resume_profile(user_email)
    if not user_email:
        payload_skills = [str(s).strip() for s in (payload.get("resume_skills") or [])
                          if str(s or "").strip()]
        if payload_skills:
            skills = payload_skills[:60]
        try:
            if payload.get("resume_years") is not None:
                resume_years = max(0, int(payload["resume_years"]))
        except (TypeError, ValueError):
            pass

    # Built BEFORE retrieval: its role families widen the query (the pool was
    # the feed's binding constraint, not the ranking), and reusing the same
    # object for scoring means the resume is loaded and analysed once.
    profile = _build_profile(user_email, skills, resume_years)
    # UNION of every resume's families, not the active resume's. Same reasoning
    # as the docstring above — retrieval that is too tight reads as an empty
    # market — except the pool was additionally narrowed to whichever file
    # happened to be pinned. A user with a Backend and a Cloud resume retrieves
    # for both families now, and `fit_score` still decides the ordering.
    families = _role_families(titles, skills,
                              sorted(_resume_families(profile)))
    try:
        docs = inv.find_active_fresh(
            source=None if source == SOURCE_CAREER else source,
            sources=CAREER_FEED_SOURCES if source == SOURCE_CAREER else None,
            role_families=families or None,
            industries=industries or None,
            tenants=companies or None,
            max_age_days=WINDOW_DAYS,
            work_mode="remote" if remote_only else None,
            limit=CANDIDATE_LIMIT,
            # The authenticated tabs are a browse surface, not the guest
            # freshness promise, so a posting whose vendor publishes no date
            # belongs here - ranked below every dated row, never hidden.
            include_undated=True,
        )
    except Exception as exc:
        logger.warning("inventory_feed: retrieval failed for %s: %s",
                       source, type(exc).__name__)
        return None

    # Geo is decided by the ONE shared rule (`us_geo`), not a country-code
    # check: most inventory rows carry no parsed country (boards ship a
    # free-text location), so a country-code test passes almost everything and
    # leaks obvious non-US rows - "IN_Senior Associate_DevOps_GCC" and
    # Commonwealth Bank both showed up in a US-only slice. This module used to
    # borrow `career_pages_service._keep_us` for BOTH source tabs, which was
    # the weaker of the two implementations then in the tree.
    from services import us_geo

    rows: List[Dict[str, Any]] = []
    for doc in docs:
        row = _row_from_posting(doc, tz_offset=tz_offset)
        if row is None:
            continue
        if us_only and not us_geo.keep_us_row(row):
            continue
        # Salary floor. Drops rows whose band is KNOWN and below the floor, and
        # keeps rows with no stated band. That asymmetry is deliberate: most
        # postings do not publish a salary, so treating unknown as "fails the
        # filter" would empty the feed and, worse, would silently hide the
        # market rather than narrow it. The row carries `salary_confidence` so
        # the frontend can label what it is showing.
        if min_salary:
            top = row.get("salary_max_annual") or row.get("salary_min_annual")
            if top is None:
                if not include_unknown_salary:
                    continue
            elif top < min_salary:
                continue
        elif not include_unknown_salary:
            # "Only show jobs with published pay" is a filter in its own right,
            # independent of any floor.
            if (row.get("salary_max_annual") or row.get("salary_min_annual")) is None:
                continue
        rows.append(row)
    rows = _dedupe(rows)
    # Show the EMPLOYER, not the legal entity the board stamped on the
    # requisition. `_dedupe` already collapses JPMorgan's three CX copies of
    # one requisition into a single card (`_req_key`), but which of the three
    # SURVIVES is whichever arrived first — measured across all six input
    # orders, the kept name was "JPMorgan Chase", "CHASE FAMILY LIMITED
    # PARTNERSHIP NO. 5" and "NUTMEG CONTAINER CORPORATION" in two orders
    # each. So the duplicate was already gone and the name was still a coin
    # flip, which is worse than it sounds: a card showing "NUTMEG CONTAINER
    # CORPORATION" also looks its sponsorship up under that name and finds
    # nothing.
    #
    # `employer_display` is the existing, curated answer and was simply never
    # wired into this path. Its first rung is a name-level correction that
    # needs no registry read (NUTMEG / CHASE FAMILY -> JPMorgan Chase,
    # M.J. WILLIAMS COMPANY -> Boeing); the canonical-registry rung is
    # available now that the id is projected. The original is preserved on
    # `employer_name_original` so a card can still say "also listed as".
    try:
        from services import employer_display
        employer_display.apply_display_names(rows)
    except Exception as exc:  # pragma: no cover - display polish is best effort
        logger.warning("inventory_feed: display names skipped: %s", type(exc).__name__)

    if len(rows) < MIN_ROWS:
        logger.info("inventory_feed: %s slice too thin (%d < %d) - deferring to crawl",
                    source, len(rows), MIN_ROWS)
        return None

    _score_rows(rows, user_email, skills, resume_years, profile=profile)
    # Live re-classify: stale inventory may still tag Systems Engineer as
    # software_engineering. Keep rows whose live title family is in the
    # retrieval set (resume titles + resume profile families) - and rows the
    # classifier cannot place at all.
    #
    # That second clause is the fix for a silent drop. `classify_role` returns
    # None rather than guessing, and the old expression turned that None into
    # `""`, which is in no `allowed` set, so every title the taxonomy has no
    # rule for was deleted here AFTER retrieval had deliberately kept it -
    # `job_inventory_service.role_family_of` documents in as many words that
    # "other" means unclassified and "is never used to exclude a posting from
    # retrieval". Two layers, opposite rules, and the stricter one ran last.
    #
    # It lands hardest on exactly the postings an early-career seeker needs:
    # "Technology Analyst", "Associate Consultant", "Rotational Program
    # Associate" and most graduate-programme titles name no discipline, so the
    # classifier declines - correctly - and the feed then threw them away.
    # An off-family row that survives here is not a precision problem: ordering
    # is decided by `fit_score`, which scores it below the rows already shown.
    if families:
        from services import fit_score
        allowed = set(families)
        def _in_family(row: Dict[str, Any]) -> bool:
            fam = fit_score.classify_role(row.get("title") or "")
            return fam is None or fam in allowed

        rows = [r for r in rows if _in_family(r)]
    if LOCATION_ROLLUP:
        rows = _collapse_locations(rows)
    from services.career_pages_catalog import marketplace_sort_key
    rows.sort(key=lambda j: (
        marketplace_sort_key(j),
        -(j.get("match_score") or 0),
        j.get("days_ago") if j.get("days_ago") is not None else 99))
    rows = rows[:GLOBAL_KEEP]

    _attach_feasibility(rows, skills, resume_years)
    rows.sort(key=lambda j: (
        marketplace_sort_key(j),
        -(j.get("match_score") or 0),
        j.get("days_ago") if j.get("days_ago") is not None else 99))
    try:
        _attach_sponsorship(rows)
        _sponsorship_from_stored_requirements(rows)
        _demote_unreachable_sponsors(rows, user_email)
    except Exception as exc:  # pragma: no cover - enrichment is best effort
        logger.warning("inventory_feed: sponsorship skipped: %s", type(exc).__name__)

    tenants_with_results = len({r.get("tenant") for r in rows if r.get("tenant")})
    for row in rows:
        row.pop("_requirements", None)
        row.pop("_req_key", None)

    result: Dict[str, Any] = {
        "ok": True,
        "generated_at": started.isoformat(),
        "jobs": rows,
        "total": len(rows),
        "window_counts": _window_counts(rows),
        "query_terms": titles,
        "tenants_total": _registry_size(source),
        # The crawl already covered the whole registry; there is no per-request
        # partial progress to report, and claiming otherwise would be a lie the
        # tab's progress UI would render.
        "tenants_done": _registry_size(source),
        "tenants_with_results": tenants_with_results,
        "industries_available": _industries_for(source),
        "cache_hit": True,
        "us_only": us_only,
        "min_salary": min_salary or None,
        "include_unknown_salary": include_unknown_salary,
        "resume_skill_count": len(skills),
        "diagnostics": {"task_errors": 0, "served_from": "inventory",
                        "candidates": len(docs)},
        "errors": [],
        "served_from": "inventory",
    }
    if source == SOURCE_CAREER:
        result["ats_available"] = _ats_available()
    return result


def _ats_available() -> List[str]:
    """ATS kinds the career tab may be filtered by.

    This was `career_pages_catalog.ATS_KINDS`, i.e. the seven vendors that
    happen to have Python catalog rows. Everything ADR-004 added — Oracle,
    iCIMS, Taleo, Jobvite, BambooHR, UKG, Paylocity, Rippling, Cornerstone,
    JazzHR — was therefore absent from the filter a seeker is offered, even
    while its postings were in the results. Advertise what is actually
    crawlable.
    """
    kinds = set()
    try:
        from services.career_pages_catalog import ATS_KINDS
        kinds.update(ATS_KINDS)
    except Exception:  # pragma: no cover
        pass
    try:
        from services.ats_providers import registry as ats_registry
        kinds.update(_ats_key(v) for v in ats_registry.vendors())
    except Exception:  # pragma: no cover
        pass
    kinds.discard("workday")   # its own tab
    return sorted(k for k in kinds if k)


def _registry_size(source: str) -> int:
    try:
        from services import job_ingest_worker as w

        return len(w.registry_members(source))
    except Exception:  # pragma: no cover
        return 0


def _industries_for(source: str) -> List[str]:
    try:
        if source == SOURCE_CAREER:
            from services.career_pages_catalog import INDUSTRIES

            return list(INDUSTRIES)
        from services.workday_jobs_service import INDUSTRIES as WD_INDUSTRIES

        return list(WD_INDUSTRIES)
    except Exception:  # pragma: no cover
        return []


#: Sponsorship verdicts a candidate who NEEDS sponsorship cannot realistically
#: reach. `unknown` is deliberately absent - absence of evidence is not
#: evidence, and most employers publish nothing.
_UNREACHABLE_SPONSORSHIP = frozenset({"refused", "rare"})


def requires_sponsorship(user_email: str) -> bool:
    """Whether this user has declared they need visa sponsorship.

    Delegates to `work_authorization.get_profile`, which is now THE reader for
    this fact: `visa_profiles.work_authorization` first, the visa-timeline
    status second (an F-1 on OPT has effectively answered this question), and
    the legacy `user_preferences.requires_sponsorship` boolean this function
    used to read on its own, third.

    That legacy read is exactly why this delegates rather than duplicates. The
    boolean is set by an onboarding question most accounts never saw — the live
    account this work started from had `user_preferences` holding only an
    experience level, a remote preference and six target roles — so reading it
    alone answered `False` for a student on OPT and the whole demotion below
    never fired.

    Any failure still reads as False: a feed that silently reorders itself
    because a lookup broke is worse than one that does not reorder at all.
    """
    if not user_email:
        return False
    try:
        from services import work_authorization as wa
        return bool(wa.get_profile(user_email).get("needs_sponsorship"))
    except Exception:  # pragma: no cover - never fail a search on a preference
        logger.warning("inventory_feed: sponsorship preference unavailable")
        return False


def _sponsorship_from_stored_requirements(rows: List[Dict[str, Any]]) -> None:
    """Re-derive `sponsorship` from the STORED requirements on each posting.

    `_attach_sponsorship` is shared with the live-crawl path and reads its
    highest-fidelity signal out of `title + description`. On THIS path the
    description is deliberately empty — the body lives in S3 and scoring runs
    off `requirements` (see the row builder) — so the JD regex matches
    nothing, every row falls through to the tenant tier table, and a tenant
    that table does not name resolves `unknown`.

    That is not a cosmetic gap. `_demote_unreachable_sponsors` only fires on
    `refused` / `rare`, so on the inventory path — the one that actually
    serves the feed since the ADR-003 read cutover — it fired on nothing, for
    every user, since the cutover. Measured live on an F1-OPT account:
    `requires_sponsorship` returns True, RTX's postings store
    `clearance_required: true` AND `work_authorization_restricted: true`, and
    RTX still held six of the first twenty-four cards with an H-1B chip on
    them. The verdict existed at ingest, was stored correctly, and was then
    read from the one field the row does not carry.

    The stored flags ARE the JD verdict — `fit_score.extract_requirements`
    produced them from the real body when the posting was ingested — so they
    outrank the tenant-tier estimate and are applied after it.

    A clearance requirement is treated as unreachable for a sponsorship-
    needing candidate for the same reason the JD text is: US-cleared work is
    US-person work. This is still a DEMOTION, never a filter — silence stays
    `unknown`, and nothing is removed.
    """
    for row in rows:
        reqs = row.get("_requirements") or {}
        if not isinstance(reqs, dict):
            continue
        if reqs.get("work_authorization_restricted") or reqs.get("clearance_required"):
            row["sponsorship"] = "refused"
            row["h1b_sponsor"] = False
            row["sponsor_source"] = "jd_requirements"
            row["sponsor_note"] = (
                "This posting states a US work-authorization or security-clearance "
                "requirement.")


def _demote_unreachable_sponsors(rows: List[Dict[str, Any]],
                                 user_email: str) -> None:
    """Flag rows an international candidate almost certainly cannot convert.

    `sponsorship` already carries the verdict; nothing read it for ORDER. For
    one live F-1 OPT account, 171 of 1,006 rows (17%) were defence and
    clearance employers, 108 of them already marked `rare`, and two of them
    sat in his top twenty - roles he cannot take, above roles he can.

    This DEMOTES, it does not hide. The platform rule is that a filter never
    removes a job on a signal this soft, and `rare` means "files
    occasionally", not "never": the row stays reachable, it just stops
    outranking employers that do sponsor. `unknown` is untouched, because most
    postings say nothing and treating silence as refusal would hide the
    market. `match_score` is not modified - this is `rank_score` territory.
    """
    if not rows or not requires_sponsorship(user_email):
        return
    for row in rows:
        if str(row.get("sponsorship") or "") in _UNREACHABLE_SPONSORSHIP:
            row["sponsorship_demoted"] = True
