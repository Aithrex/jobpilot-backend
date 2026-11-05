"""What KIND of employer a board card is from (`company-type-1.0.0`).

The For-you board's company filter groups employers the way a job seeker
thinks about them: "Big Tech & MNCs", "large enterprises", "startups",
"staffing firms". Nothing in our data states company size, so every answer
here is read off evidence we already hold, and each card carries the reason
(`company_type.basis`) so the interface can say WHY a company sits in a group.

Evidence, strongest first:

1. **The curated Big Tech / MNC catalog** (`mnc_catalog.MNC_CATALOG`, matched
   by board tenant or by normalised employer name). A human put them there.
2. **The employer's business model** (`employer_profile.kind`, from
   `employer_business_model`): staffing, consultancy and talent marketplaces
   are a different relationship with the candidate, whatever their size.
3. **How many roles the employer has open on our boards right now**, from the
   grouped inventory read the Big Tech crawl already caches
   (`mnc_jobs_service.inventory_groups`). 1,000+ open roles is a
   multinational's hiring volume; 150+ is an enterprise's.
4. **The hiring system the board runs on.** Workday, Oracle, SuccessFactors,
   Taleo, iCIMS, Eightfold and Phenom are enterprise HR suites; Ashby, Lever,
   Greenhouse, Workable and their peers are what startups and growing
   companies use. This is the weakest rung and the basis says so.

Anything none of these reaches is `unknown` ("Not yet categorised") - shown,
never hidden, so the group counts always add up to the board.

Startups and mid-size companies are ONE group on purpose: nothing we hold can
tell a 40-person startup from a 900-person scale-up that both hire through
Greenhouse, and a split we cannot evidence would be a guess dressed as a fact.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

COMPANY_TYPE_VERSION = "company-type-1.0.0"

BIG_TECH_MNC = "big_tech_mnc"
ENTERPRISE = "enterprise"
STARTUP_MIDSIZE = "startup_midsize"
STAFFING = "staffing"
UNKNOWN = "unknown"

#: Display order and labels. The frontend owns its own copy of the labels;
#: these exist for scripts and tests.
TYPES: Tuple[str, ...] = (BIG_TECH_MNC, ENTERPRISE, STARTUP_MIDSIZE, STAFFING, UNKNOWN)
LABELS: Dict[str, str] = {
    BIG_TECH_MNC: "Big Tech & MNCs",
    ENTERPRISE: "Large enterprises",
    STARTUP_MIDSIZE: "Startups & mid-size",
    STAFFING: "Staffing & consulting",
    UNKNOWN: "Not yet categorised",
}

#: Open roles on our boards at which an employer is hiring at multinational
#: volume, and at enterprise volume.
MNC_OPEN_ROLES = 1000
ENTERPRISE_OPEN_ROLES = 150

#: Business-model kinds that are not a direct employer of the candidate.
_STAFFING_KINDS = frozenset({"staffing", "consultancy", "talent_marketplace"})

#: Enterprise HR suites (lower-case `ats` values as postings store them).
_ENTERPRISE_ATS = frozenset({
    "workday", "oracle", "orc", "successfactors", "taleo", "icims", "eightfold",
    "phenom", "radancy", "avature", "brassring", "cornerstone", "ukg", "adp",
})
#: Hiring tools built for startups and growing companies.
_STARTUP_ATS = frozenset({
    "ashby", "lever", "greenhouse", "workable", "recruitee", "breezy", "jazzhr",
    "rippling", "gem", "pinpoint", "teamtailor", "homerun", "bamboohr",
    "personio", "polymer", "wellfound", "dover", "trakstar",
})


def _norm(name: Any) -> str:
    from services.mnc_jobs_service import _normalize_employer
    return _normalize_employer(str(name or ""))


def _catalog_keys() -> Tuple[set, set]:
    """(tenants, normalised names) of every curated catalog employer."""
    from services.mnc_catalog import MNC_CATALOG
    from services.mnc_jobs_service import _inventory_keys
    tenants: set = set()
    names: set = set()
    for row in MNC_CATALOG:
        _source, t, n = _inventory_keys(row)
        tenants |= t
        names |= n
    return tenants, names


def _open_roles() -> Tuple[Dict[Tuple[str, str], int], Dict[str, int]]:
    """Open roles per (ats-tenant) board and per normalised employer name."""
    from services.mnc_jobs_service import inventory_groups
    by_tenant: Dict[Tuple[str, str], int] = {}
    by_name: Dict[str, int] = {}
    for g in inventory_groups() or []:
        key, c = g.get("_id") or {}, int(g.get("c") or 0)
        ten = str(key.get("t") or "").lower()
        if ten:
            k = (str(key.get("s") or ""), ten)
            by_tenant[k] = by_tenant.get(k, 0) + c
        name = _norm(key.get("n"))
        if name:
            by_name[name] = by_name.get(name, 0) + c
    return by_tenant, by_name


class Classifier:
    """One board's worth of classification. Build once per board build: the
    catalog keys and the role counts are read once, not per card."""

    def __init__(self, *, catalog: Optional[Tuple[set, set]] = None,
                 open_roles: Optional[Tuple[Dict[Tuple[str, str], int], Dict[str, int]]] = None):
        self.catalog_tenants, self.catalog_names = catalog if catalog is not None else _catalog_keys()
        self.by_tenant, self.by_name = open_roles if open_roles is not None else _open_roles()

    def open_roles_of(self, row: Dict[str, Any]) -> int:
        """Open roles under this employer's NAME. Not by tenant: a tenant is
        unique only within its vendor ("cisco" on a career-pages board was a
        fan page, not Cisco - `test_board_one_scorer`), and the card does not
        carry the posting's `source` to disambiguate."""
        name = _norm(row.get("employer_name") or row.get("company"))
        return self.by_name.get(name, 0) if name else 0

    def classify(self, row: Dict[str, Any]) -> Dict[str, Any]:
        name = _norm(row.get("employer_name") or row.get("company"))
        tenant = str(row.get("tenant") or "").lower()
        ats = str(row.get("ats") or "").lower()

        if name and name in self.catalog_names:
            return {"type": BIG_TECH_MNC, "basis": "On our curated Big Tech & MNC list"}
        if tenant and len(tenant) >= 4 and tenant in self.catalog_tenants \
                and ats in ("workday", "oracle", "orc", "eightfold", "phenom", ""):
            return {"type": BIG_TECH_MNC, "basis": "On our curated Big Tech & MNC list"}

        profile = row.get("employer_profile")
        kind = str((profile or {}).get("kind") or "") if isinstance(profile, dict) else ""
        if kind in _STAFFING_KINDS:
            word = {"staffing": "a staffing firm", "consultancy": "a consultancy",
                    "talent_marketplace": "a talent marketplace"}[kind]
            return {"type": STAFFING, "basis": f"Hires for clients as {word}"}

        roles = self.open_roles_of(row)
        if roles >= MNC_OPEN_ROLES:
            return {"type": BIG_TECH_MNC, "basis": f"{roles:,} open roles on our boards"}
        if roles >= ENTERPRISE_OPEN_ROLES:
            return {"type": ENTERPRISE, "basis": f"{roles:,} open roles on our boards"}
        if ats in _ENTERPRISE_ATS:
            return {"type": ENTERPRISE, "basis": f"Hires through an enterprise HR system ({ats.title()})"}
        if ats in _STARTUP_ATS:
            return {"type": STARTUP_MIDSIZE,
                    "basis": f"Hires through {ats.title()}, used mostly by startups and growing companies"}
        return {"type": UNKNOWN, "basis": "Not enough information to group this company yet"}


def annotate(rows: Iterable[Dict[str, Any]], *, classifier: Optional[Classifier] = None) -> None:
    """Stamp `company_type` ({type, basis, version}) on every card. Best
    effort: a failure leaves the cards untouched and the board still builds."""
    rows = list(rows)
    if not rows:
        return
    try:
        clf = classifier or Classifier()
    except Exception as exc:  # pragma: no cover - grouping is polish, never fatal
        logger.warning("company_type: classifier unavailable (%s)", type(exc).__name__)
        return
    for row in rows:
        try:
            row["company_type"] = dict(clf.classify(row), version=COMPANY_TYPE_VERSION)
        except Exception:  # pragma: no cover - one odd row never stops the rest
            row["company_type"] = {"type": UNKNOWN, "basis": "", "version": COMPANY_TYPE_VERSION}


def group_counts(rows: List[Dict[str, Any]]) -> Dict[str, int]:
    """Cards per type, every type present (zeros included)."""
    out = {t: 0 for t in TYPES}
    for row in rows:
        t = ((row.get("company_type") or {}).get("type")) or UNKNOWN
        out[t if t in out else UNKNOWN] += 1
    return out
