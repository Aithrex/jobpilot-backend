"""Employer-level technology classification, kept SEPARATE from occupation.

Why this is its own module
--------------------------
`tech_taxonomy` answers "is this JOB technical?" and takes no employer input.
This module answers "is this EMPLOYER a technology company?" and takes no job
title. The search layer composes them, and the product is defined by the fact
that they compose rather than collapse:

  * default scope   - core-tech OCCUPATIONS at ANY employer
  * `tech_only`     - the same, narrowed to employers this module calls
                      software or tech_adjacent

Collapsing them produces both classic failures: a Store Manager at Apple
surfacing as tech, and a Software Engineer at a hospital being excluded.

Evidence ladder, strongest first
--------------------------------
1. **SEC SIC code** (`companies.sic`). A filed classification from the issuer
   itself. Authoritative, and already in the registry for the 7,987 EDGAR-
   sourced rows.
2. **Posting mix.** What fraction of the employer's own active postings does
   `tech_taxonomy` call core-tech? An employer whose board is 70% software
   engineering is a software employer whatever its SIC says, and this is the
   only signal that reaches the private companies EDGAR cannot see.
3. **Curated seed** (`scripts/data/tech_companies.py`), which carries a
   category per row.
4. **ATS vendor.** Greenhouse/Lever/Ashby customers skew venture-backed tech.
   Real, weak, never decisive.
5. **Company-name keywords.** Explicitly a DISCOVERY signal only, capped at
   low confidence and never sufficient on its own - "Technologies" in a name
   is how "NUROTOCO OF MASSACHUSETTS" and "SLACK TIDE CRUISES LLC" would get
   classified, and both are in the live registry.

Nothing here is inferred from a company's size, stage or funding unless an
authorized source supplied it; those fields stay absent rather than guessed.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

CLASSIFIER_VERSION = "employer-tech-1.0.0"

TECH_SOFTWARE = "software"
TECH_ADJACENT = "tech_adjacent"
TECH_NON = "non_tech"
TECH_UNKNOWN = "unknown"
TECH_CLASSIFICATIONS = (TECH_SOFTWARE, TECH_ADJACENT, TECH_NON, TECH_UNKNOWN)

STATUS_PUBLIC = "public"
STATUS_PRIVATE = "private"
STATUS_GOVERNMENT = "government"
STATUS_NONPROFIT = "nonprofit"
STATUS_UNKNOWN = "unknown"

# ---------------------------------------------------------------------------
# SIC -> (classification, categories).  SEC's own list; only unambiguous rows.
# ---------------------------------------------------------------------------

_SIC_SOFTWARE: Dict[str, Tuple[str, ...]] = {
    "7370": ("software", "it_services"),          # Computer programming, data processing
    "7371": ("it_services",),                     # Computer programming services
    "7372": ("software",),                        # Prepackaged software
    "7373": ("it_services", "systems_integration"),
    "7374": ("cloud_infrastructure", "data_services"),
    "7375": ("internet_platform",),               # Information retrieval services
    "7377": ("it_services",),
    "7379": ("it_services",),
    "3571": ("hardware", "computing"),            # Electronic computers
    "3572": ("hardware", "storage"),
    "3575": ("hardware",),
    "3576": ("networking", "hardware"),           # Computer communications equipment
    "3577": ("hardware",),
    "3578": ("hardware",),
    "3661": ("telecom", "hardware"),
    "3663": ("telecom", "hardware"),
    "3669": ("hardware",),
    "3672": ("semiconductor", "hardware"),        # Printed circuit boards
    "3674": ("semiconductor",),                   # Semiconductors & related devices
    "3675": ("semiconductor",), "3676": ("semiconductor",),
    "3677": ("semiconductor",), "3678": ("semiconductor",),
    "3679": ("semiconductor", "hardware"),
    "3559": ("semiconductor_equipment",),         # Special industry machinery (fab tools)
    "3827": ("hardware", "instruments"),
    "3812": ("defense_tech", "hardware"),         # Search, detection, navigation
}

_SIC_ADJACENT: Dict[str, Tuple[str, ...]] = {
    "4813": ("telecom",), "4812": ("telecom",), "4899": ("telecom",),
    "3826": ("instruments",), "3829": ("instruments",),
    "3841": ("medtech",), "3845": ("medtech",),   # Electromedical apparatus
    "8731": ("research", "biotech_tools"),
    "6199": ("fintech",), "6141": ("fintech",),
    "7389": ("business_services",),
    "8742": ("consulting",),
    "3728": ("aerospace",), "3721": ("aerospace",), "3761": ("aerospace",),
    "3711": ("automotive",), "3714": ("automotive",),
}

# ---------------------------------------------------------------------------
# Name keywords - DISCOVERY ONLY.  Capped hard; see the module docstring.
# ---------------------------------------------------------------------------

_NAME_TECH_RE = re.compile(
    r"(?<![a-z])("
    r"software|technolog(?:y|ies)|semiconductor|microelectronics|"
    r"cyber|cybersecurity|infosec|datacenter|data center|cloud|saas|"
    r"analytics|informatics|robotics|artificial intelligence|"
    r"computing|computer|digital labs?|devops|networks?|systems inc"
    r")(?![a-z])", re.IGNORECASE)

#: Names that CONTAIN a tech keyword and are demonstrably not tech employers.
#: All three are live rows in `companies` today. They are why a name keyword
#: can never exceed `unknown` on its own.
_NAME_FALSE_FRIEND_RE = re.compile(
    r"(?<![a-z])("
    r"cruises?|realty|real estate|holdings? llc|family (?:limited )?partnership|"
    r"church|ministries|salon|landscap\w*|plumbing|roofing|"
    r"restaurant|catering|trucking|hauling|towing|motorcycle|"
    r"irrigation|container|redistribution"
    r")(?![a-z])", re.IGNORECASE)

#: ATS vendors whose customer base skews venture-backed technology. A weak
#: prior, never decisive. Enterprise vendors (Workday, Oracle, iCIMS, Taleo,
#: SuccessFactors) carry NO signal - their customers are every large employer
#: in every industry, which is exactly why the inventory skews retail/health.
_TECH_LEANING_ATS = frozenset({"greenhouse", "lever", "ashby", "rippling"})

#: Fraction of an employer's active postings that must be core-tech before
#: the posting mix alone calls them a software company. 0.45 is set from the
#: live distribution: platform-first employers (Stripe, Databricks, Datadog)
#: sit well above it, while banks and retailers with real engineering orgs
#: (JPMorgan ~0.15, Target ~0.03) sit well below and land `tech_adjacent`.
POSTING_MIX_SOFTWARE = 0.45
POSTING_MIX_ADJACENT = 0.12
#: Below this many classified postings the mix is noise, not evidence.
POSTING_MIX_MIN_ROWS = 12


@dataclass
class EmployerClassification:
    employer_id: str = ""
    employer_name: str = ""
    tech_classification: str = TECH_UNKNOWN
    tech_categories: List[str] = field(default_factory=list)
    confidence: float = 0.0
    evidence: List[Dict[str, str]] = field(default_factory=list)
    evidence_source: str = ""
    evidence_updated_at: Optional[datetime] = None
    public_private_status: str = STATUS_UNKNOWN
    company_size: Optional[str] = None
    company_stage: Optional[str] = None
    validated_career_endpoint_count: int = 0
    last_endpoint_validation_at: Optional[datetime] = None
    classifier_version: str = CLASSIFIER_VERSION

    def to_doc(self) -> Dict[str, Any]:
        return {
            "tech_classification": self.tech_classification,
            "tech_categories": list(self.tech_categories),
            "confidence": round(float(self.confidence), 3),
            "evidence": list(self.evidence),
            "evidence_source": self.evidence_source,
            "evidence_updated_at": self.evidence_updated_at,
            "public_private_status": self.public_private_status,
            # Absent unless an authorized source supplied them. A None here is
            # "we were not told", which is a different claim from "small".
            "company_size": self.company_size,
            "company_stage": self.company_stage,
            "validated_career_endpoint_count": int(self.validated_career_endpoint_count),
            "last_endpoint_validation_at": self.last_endpoint_validation_at,
            "classifier_version": self.classifier_version,
        }


def _ev(kind: str, source: str, value: Any) -> Dict[str, str]:
    return {"kind": kind, "source": source, "value": str(value)[:160]}


def _sic4(value: Any) -> str:
    s = re.sub(r"\D", "", str(value or ""))
    return s[:4].rjust(4, "0") if s else ""


def classify_employer(
    company: Dict[str, Any],
    *,
    posting_mix: Optional[Dict[str, int]] = None,
    endpoints: Optional[Sequence[Dict[str, Any]]] = None,
    curated_category: Optional[str] = None,
) -> EmployerClassification:
    """Classify one employer.

    `company`      - a `companies` document (or anything with the same keys).
    `posting_mix`  - `{"core": n, "adjacent": n, "non_tech": n, "unknown": n}`
                     from `tech_taxonomy` over this employer's ACTIVE postings.
    `endpoints`    - that employer's `company_ats_endpoints` rows.
    `curated_category` - a category from the curated tech seed, if the row
                     came from there.
    """
    company = company if isinstance(company, dict) else {}
    out = EmployerClassification(
        employer_id=str(company.get("company_id") or ""),
        employer_name=str(company.get("name") or company.get("display_name") or ""),
    )

    endpoints = list(endpoints or [])
    validated = [e for e in endpoints if str(e.get("status") or "") == "validated"]
    out.validated_career_endpoint_count = len(validated)
    stamps = [e.get("validated_at") or e.get("last_checked_at") for e in endpoints]
    stamps = [s for s in stamps if isinstance(s, datetime)]
    out.last_endpoint_validation_at = max(stamps) if stamps else None

    # public/private is a FACT we hold, not an inference: an EDGAR CIK means
    # the entity files with the SEC.
    if company.get("cik") or (company.get("signals") or {}).get("edgar"):
        out.public_private_status = STATUS_PUBLIC
    elif str(company.get("sector") or "").lower() == "government" or \
            "government" in str(company.get("industry") or "").lower():
        out.public_private_status = STATUS_GOVERNMENT

    candidates: List[Tuple[str, float, Tuple[str, ...], str, Dict[str, str]]] = []

    # ---- 1. SIC ------------------------------------------------------------
    sic = _sic4(company.get("sic"))
    if sic in _SIC_SOFTWARE:
        candidates.append((TECH_SOFTWARE, 0.92, _SIC_SOFTWARE[sic], "sec_edgar_sic",
                           _ev("sic", "sec_edgar",
                               f"{sic} {company.get('sic_description') or ''}".strip())))
    elif sic in _SIC_ADJACENT:
        candidates.append((TECH_ADJACENT, 0.8, _SIC_ADJACENT[sic], "sec_edgar_sic",
                           _ev("sic", "sec_edgar",
                               f"{sic} {company.get('sic_description') or ''}".strip())))
    elif sic:
        # A filed SIC that is not on either list is real evidence of NOT being
        # a technology company - it is the issuer's own classification.
        candidates.append((TECH_NON, 0.72, (), "sec_edgar_sic",
                           _ev("sic", "sec_edgar",
                               f"{sic} {company.get('sic_description') or ''}".strip())))

    # ---- 2. posting mix ----------------------------------------------------
    mix = posting_mix or {}
    total = sum(int(mix.get(k) or 0) for k in ("core", "adjacent", "non_tech", "unknown"))
    if total >= POSTING_MIX_MIN_ROWS:
        core = int(mix.get("core") or 0)
        adj = int(mix.get("adjacent") or 0)
        # Denominator excludes `unknown`: an employer whose titles we cannot
        # read is not thereby less technical, and including them would make
        # every large employer look non-tech simply because their board is
        # long. If everything is unknown the ratio is undefined and skipped.
        known = core + adj + int(mix.get("non_tech") or 0)
        if known >= POSTING_MIX_MIN_ROWS:
            ratio = (core + 0.5 * adj) / known
            ev = _ev("posting_mix", "own_ats_board",
                     f"{core} core + {adj} adjacent of {known} classified "
                     f"({ratio:.0%})")
            if ratio >= POSTING_MIX_SOFTWARE:
                candidates.append((TECH_SOFTWARE, 0.86, ("software",),
                                   "posting_mix", ev))
            elif ratio >= POSTING_MIX_ADJACENT:
                candidates.append((TECH_ADJACENT, 0.7, (), "posting_mix", ev))
            else:
                candidates.append((TECH_NON, 0.6, (), "posting_mix", ev))

    # ---- 3. curated seed ---------------------------------------------------
    if curated_category:
        candidates.append((TECH_SOFTWARE, 0.8, (str(curated_category),),
                           "curated_seed",
                           _ev("curated", "jobpilot_tech_seed", curated_category)))
    elif (company.get("signals") or {}).get("software") is True:
        candidates.append((TECH_SOFTWARE, 0.66, (), "registry_signal",
                           _ev("signal", "companies.signals.software", "true")))

    # ---- 4. ATS vendor (weak) ---------------------------------------------
    vendors = {str(e.get("vendor") or "").lower() for e in validated}
    leaning = vendors & _TECH_LEANING_ATS
    if leaning:
        candidates.append((TECH_SOFTWARE, 0.35, (), "ats_vendor",
                           _ev("ats_vendor", "company_ats_endpoints",
                               ", ".join(sorted(leaning)))))

    # ---- 5. name keywords (discovery only, capped) ------------------------
    name = out.employer_name
    if name and _NAME_TECH_RE.search(name) and not _NAME_FALSE_FRIEND_RE.search(name):
        candidates.append((TECH_UNKNOWN, 0.2, (), "name_keyword",
                           _ev("name_keyword", "company_name",
                               _NAME_TECH_RE.search(name).group(0))))

    if not candidates:
        return out

    # Resolve: highest confidence wins the verdict; every candidate that
    # AGREES with it contributes its categories and its evidence, and the
    # agreement itself raises confidence (two independent sources saying the
    # same thing is stronger than either alone).
    candidates.sort(key=lambda c: c[1], reverse=True)
    verdict, conf, cats, source, ev = candidates[0]
    if verdict == TECH_UNKNOWN:
        # Only a name keyword fired. That is a lead, not a classification.
        out.tech_classification = TECH_UNKNOWN
        out.confidence = conf
        out.evidence = [ev]
        out.evidence_source = source
        out.evidence_updated_at = datetime.now(timezone.utc)
        return out

    categories: List[str] = list(cats)
    evidence: List[Dict[str, str]] = [ev]
    agree = 0
    for v, c, cs, sc, e in candidates[1:]:
        if v == verdict:
            agree += 1
            evidence.append(e)
            for cat in cs:
                if cat not in categories:
                    categories.append(cat)
        elif c >= 0.6:
            # A high-confidence DISAGREEMENT is worth recording and worth a
            # confidence penalty. This is the SIC-says-bank /
            # board-says-software case, which is real and common (Capital One,
            # Goldman) and is exactly what `tech_adjacent` is for.
            evidence.append({**e, "kind": e["kind"] + "_dissent"})
            if {verdict, v} == {TECH_SOFTWARE, TECH_NON}:
                verdict = TECH_ADJACENT
                conf = min(conf, 0.7)
    conf = min(0.98, conf + 0.04 * agree)

    out.tech_classification = verdict
    out.tech_categories = categories[:6]
    out.confidence = conf
    out.evidence = evidence[:6]
    out.evidence_source = source
    out.evidence_updated_at = datetime.now(timezone.utc)
    return out


# ---------------------------------------------------------------------------
# The technology-employer denominator — ONE definition, two callers
# ---------------------------------------------------------------------------
#: Evidence kinds on an `employers` row that, on their own, make an employer a
#: technology employer. Each is documentary; none is a keyword in a name.
TECH_EVIDENCE_KINDS = frozenset({
    "curated_seed", "official_career_url", "identity_override",
})


def is_evidence_backed_tech(
    employer: Dict[str, Any],
    mix: Optional[Dict[str, Any]] = None,
    company: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, str]:
    """`(in_denominator, why)` for one employer row from the registry.

    THE definition of "technology employer", and it lives here rather than in
    `scripts/employer_coverage_report.py` because two callers need the same
    answer and a second copy is a second answer:

      * the coverage report's DENOMINATOR — the employers we hold ourselves
        accountable for covering;
      * the feed's `employer_scope=tech_only` FILTER — the employers a user
        asking for "technology companies only" expects to see.

    Those two being one rule is the point. A coverage number computed over a
    set the product cannot reproduce is a number about nothing, and a filter
    whose membership no report measures is a filter nobody can audit. They
    were two implementations, and the filter's was a page-local posting mix
    that on a live 2,000-row candidate set cleared its minimum for 4 of 191
    employers — a filter that rendered, echoed and dropped nothing.

    Deliberately NOT a name heuristic: the live registry contains "NUROTOCO OF
    MASSACHUSETTS", "SLACK TIDE CRUISES LLC" and "NOTIONSBYNANCY LLC", and a
    rule that reads "Technologies" in a name promotes all three.

    The `companies` row is joined in because the strongest signal available —
    the SEC-assigned SIC code — lives there and nowhere else. Classifying
    without it discards a filing the registrant made about themselves and
    leaves only the posting mix, which cuts the denominator to the employers
    we already crawl. That is a denominator defined by our own coverage, which
    is precisely the circularity a coverage number must not have.
    """
    mix = mix or {}
    kinds = {str(e.get("kind")) for e in (employer.get("evidence") or [])
             if isinstance(e, dict)}
    hit = kinds & TECH_EVIDENCE_KINDS
    if hit:
        return True, f"curated: {sorted(hit)[0]}"

    company = company or {}
    tech_rows = int(mix.get("core", 0)) + int(mix.get("adjacent", 0))
    classified = classify_employer(
        {"name": employer.get("display_name"),
         "domain": employer.get("canonical_domain") or company.get("domain"),
         "sic": company.get("sic"),
         "sic_description": company.get("sic_description"),
         "signals": company.get("signals") or {},
         "cik": company.get("cik"), "ticker": company.get("ticker")},
        posting_mix={"core": int(mix.get("core", 0)),
                     "adjacent": int(mix.get("adjacent", 0)),
                     "non_tech": int(mix.get("non_tech", 0)),
                     "unknown": int(mix.get("unknown", 0))} if mix else None)
    if classified.tech_classification in (TECH_SOFTWARE, TECH_ADJACENT):
        return True, f"classifier: {classified.evidence_source}"

    classified_rows = tech_rows + int(mix.get("non_tech", 0))
    if classified_rows >= POSTING_MIX_MIN_ROWS:
        share = tech_rows / classified_rows
        if share >= POSTING_MIX_ADJACENT:
            return True, (f"posting mix: {tech_rows} of {classified_rows} "
                          f"classified rows are technology ({share:.0%})")
    return False, "no technology evidence"


# ---------------------------------------------------------------------------
# Coverage accounting
# ---------------------------------------------------------------------------

COVERAGE_VALIDATED = "validated"
COVERAGE_RETRYING = "retrying"
COVERAGE_BLOCKED = "blocked"
COVERAGE_UNSUPPORTED = "unsupported_provider"
COVERAGE_NO_PUBLIC_JOBS = "verified_no_public_jobs"
COVERAGE_UNRESOLVED = "unresolved"

COVERAGE_STATES = (COVERAGE_VALIDATED, COVERAGE_RETRYING, COVERAGE_BLOCKED,
                   COVERAGE_UNSUPPORTED, COVERAGE_NO_PUBLIC_JOBS,
                   COVERAGE_UNRESOLVED)

#: Vendors with NO provider implementation, and why each was left. This list
#: is now only about vendors we have never built, and `provider_blocker`
#: below consults `ats_providers.registry` FIRST — so a vendor that ships a
#: provider stops being reported as unsupported the moment the module lands,
#: with no second list to remember.
#:
#: It used to be the whole answer, and it went stale exactly as you would
#: expect: it listed `usajobs` and `successfactors` while
#: `ats_providers/usajobs.py` and `ats_providers/successfactors.py` were both
#: registered and crawling. Every employer on either vendor was reported as
#: "we cannot crawl this ATS". Neither is in it any more, and neither can be
#: re-added by accident, because the registry is asked first.
UNSUPPORTED_PROVIDERS: Dict[str, str] = {
    "dayforce": "not probed to completion; Common Crawl index 504s on the domain",
    "ceridian": "see dayforce",
    "paycom": "no tenants surfaced in the index sample taken",
    "breezyhr": "guessed slugs wrong; needs real tenants first",
    "teamtailor": "no tenants surfaced; mostly EU",
    "zohorecruit": "61 tenants found, provider not built",
    "adp": "boards behind cid-parameterised shells",
    "peopleadmin": "per-institution hosts, higher-ed",
    "interfolio": "per-institution hosts, higher-ed",
    "pageup": "per-institution hosts, higher-ed",
}


def provider_blocker(vendor: Any) -> Optional[str]:
    """Why we cannot crawl this vendor, or `None` when we can.

    THE answer, derived from the registered provider implementation. A vendor
    is unsupported when no provider is registered for it or when a registered
    one is parked; a vendor missing a credential is reported as such, because
    "set one config value" and "nobody has built this" are different problems
    with different owners and only one of them is an engineering task.
    """
    v = str(vendor or "").strip().lower()
    if not v:
        return None
    try:
        from services.ats_providers import registry as ats_registry
        verdict = ats_registry.readiness(v)
    except Exception:      # pragma: no cover - fall back to the written list
        return UNSUPPORTED_PROVIDERS.get(v)
    if verdict["state"] == ats_registry.READY:
        return None
    if verdict["state"] == ats_registry.NEEDS_CREDENTIAL:
        return verdict["detail"]
    if verdict["state"] == ats_registry.PARKED:
        return verdict["detail"]
    return UNSUPPORTED_PROVIDERS.get(v) or verdict["detail"]


def coverage_state_for(
    company: Dict[str, Any],
    endpoints: Sequence[Dict[str, Any]],
    *,
    now: Optional[datetime] = None,
) -> Tuple[str, str]:
    """`(state, detail)` for one employer. Never "covered because a domain resolved".

    ADR-004 sec. 3.2 is explicit that `*.icims.com` is wildcard DNS, so DNS
    resolution proves nothing. An employer counts as covered only when a
    provider actually fetched content from their board.
    """
    now = now or datetime.now(timezone.utc)
    endpoints = list(endpoints or [])

    validated = [e for e in endpoints if str(e.get("status") or "") == "validated"]
    if validated:
        rows = sum(int(e.get("job_count_last") or 0) for e in validated)
        return COVERAGE_VALIDATED, (
            f"{len(validated)} validated endpoint(s), {rows} rows on last check")

    empty = [e for e in endpoints if str(e.get("status") or "") == "empty"]
    if empty and not any(str(e.get("status") or "") in ("dead", "blocked")
                         for e in endpoints):
        return COVERAGE_NO_PUBLIC_JOBS, (
            f"{len(empty)} endpoint(s) fetched successfully with zero postings")

    blocked = [e for e in endpoints if str(e.get("status") or "") == "blocked"]
    if blocked:
        detail = str(blocked[0].get("last_check_detail") or "vendor challenge")
        return COVERAGE_BLOCKED, detail[:200]

    vendors = {str(e.get("vendor") or "").lower() for e in endpoints}
    blocked = {v: provider_blocker(v) for v in vendors}
    blocked = {v: why for v, why in blocked.items() if why}
    if blocked:
        v = sorted(blocked)[0]
        return COVERAGE_UNSUPPORTED, f"{v}: {blocked[v]}"

    dead = [e for e in endpoints if str(e.get("status") or "") == "dead"]
    if dead:
        detail = str(dead[0].get("last_check_detail") or "endpoint dead")
        return COVERAGE_RETRYING, f"dead, awaiting re-fingerprint: {detail}"[:200]

    status = str(company.get("discovery_status") or "")
    if status in ("pending", "deferred", "running"):
        return COVERAGE_RETRYING, f"discovery_status={status}"
    if status == "no_portal":
        # This is the state that must NOT be read as "no jobs". `no_portal`
        # means our fingerprinter read some pages and matched nothing - which
        # for NVIDIA, Microsoft, Apple and Salesforce (all `no_portal` on live
        # data while all four run a public ATS) is a discovery failure, not an
        # employer fact.
        return COVERAGE_UNRESOLVED, (
            "discovery read the site and matched no ATS fingerprint - "
            f"{company.get('detail') or 'no detail recorded'}")[:200]
    return COVERAGE_UNRESOLVED, "no discovery attempt recorded"
