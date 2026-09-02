"""One canonical employer, one id, shared by every registry that names one.

The defect this fixes
---------------------
Five registries name employers and none of them agree on an identifier:

    companies                       82,815 rows, `company_id` = sha1(name|state)
    scripts/data/tech_companies.py     465 rows, keyed by display name
    career_pages_catalog               ...  rows, keyed by (ats, slug)
    workday_tenant_catalog / mnc_catalog    keyed by (vendor, tenant)
    company_ats_endpoints            2,592 rows, keyed by `member_key`
    job_postings                   405,133 rows, `employer_id` = SLUG OF THE NAME

`job_postings.employer_id` being a slug of whatever name the crawler saw is
the root cause of every identity bug in `docs/coverage/reports/tech-search-baseline-2026-09-01.md`:
one requisition published under two names is two employers, and a
state-registry shell that is name-adjacent to a large employer claims its
tenant and its entire posting run.

`employer_identity.resolve_employer_name` patched the *display* of this at
read time. It could not fix the *key*, because there was no key to fix. This
module supplies one.

What an `employer_id` is
------------------------
`emp_<12 hex>`, derived deterministically so the same employer computes the
same id on any machine, in any process, with no database round trip:

    verified domain known  ->  sha1("domain:" + registrable_domain)
    curated key known      ->  sha1("curated:" + key)
    otherwise              ->  sha1("name:" + name_key)

Domain-first is deliberate. A domain is the one employer attribute that is
globally unique, independently verifiable and stable across a rename; a name
is none of those things. An employer whose id is name-derived is explicitly
`identity_confidence < 0.6` and is a candidate for promotion the moment a
domain is verified for it — `rekey_to_domain` performs that promotion and
leaves the old id behind as an alias, so no stored reference dangles.

The merge rule, and why it is so restrictive
--------------------------------------------
**Never merge two employers on normalized name alone.** Measured on live
`aspirely_db`: `stripe` -> "STRIPES SOLUTIONS LLC", `notion` ->
"NOTIONSBYNANCY LLC", `ramp` -> "RAMPART RANGE MOTORCYCLE MANAGEMENT
COMMITTEE". Every one of those is a name match a human would reject on sight.

`merge_decision` therefore returns one of three answers, and only the first
writes anything:

    merge      identical verified domain, OR a curated alias/parent row, OR
               the same owner-verified (vendor, tenant)
    distinct   two different verified domains — a positive answer, not an
               absence of evidence
    quarantine everything else that *looks* like a match, written to
               `employer_merge_reviews` for a human

Operating status
----------------
`active | renamed | acquired | merged | closed | product_or_brand`. These are
not decorations: `merged`/`renamed` carry `parent_employer_id` and the feed
follows the pointer, so an acquisition does not silently delete an employer
from the denominator. `product_or_brand` is how Slack stops being counted as
an employer separate from Salesforce while its postings stay attributed.

Nothing in this module probes the network or writes to `companies`,
`company_ats_endpoints` or `job_postings`. It owns exactly one collection,
`employers`, plus the review queue — so it is additive, and the backfill that
populates it (`scripts/backfill_employer_registry.py`) is re-runnable and
reversible by dropping one collection.
"""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

REGISTRY_VERSION = "employer-registry-1.0.0"

EMPLOYERS = "employers"
MERGE_REVIEWS = "employer_merge_reviews"

# --------------------------------------------------------------------------
# Operating status
# --------------------------------------------------------------------------
STATUS_ACTIVE = "active"
STATUS_RENAMED = "renamed"
STATUS_ACQUIRED = "acquired"
STATUS_MERGED = "merged"
STATUS_CLOSED = "closed"
STATUS_PRODUCT_OR_BRAND = "product_or_brand"

OPERATING_STATUSES: Tuple[str, ...] = (
    STATUS_ACTIVE, STATUS_RENAMED, STATUS_ACQUIRED, STATUS_MERGED,
    STATUS_CLOSED, STATUS_PRODUCT_OR_BRAND,
)

#: Statuses whose postings should be attributed to `parent_employer_id`
#: instead of to the row itself. A closed employer keeps its own identity —
#: its history is real — it simply stops being crawled.
REDIRECTING_STATUSES: Tuple[str, ...] = (
    STATUS_RENAMED, STATUS_ACQUIRED, STATUS_MERGED, STATUS_PRODUCT_OR_BRAND,
)

# --------------------------------------------------------------------------
# Cohorts and categories
# --------------------------------------------------------------------------
#: WHO the employer is. Orthogonal to what they build.
COHORT_MNC = "mnc"
COHORT_PUBLIC_TECH = "public_tech"
COHORT_PRIVATE_GROWTH = "private_growth"
COHORT_NICHE_STARTUP = "niche_startup"
COHORT_GOVERNMENT = "government"
COHORT_NONPROFIT = "nonprofit"
COHORT_UNKNOWN = "unknown"

COHORTS: Tuple[str, ...] = (
    COHORT_MNC, COHORT_PUBLIC_TECH, COHORT_PRIVATE_GROWTH,
    COHORT_NICHE_STARTUP, COHORT_GOVERNMENT, COHORT_NONPROFIT, COHORT_UNKNOWN,
)

#: WHAT the employer builds. Every literal here is reported on by
#: `scripts/tech_employer_coverage.py`, so adding one adds a coverage row.
CATEGORIES: Tuple[str, ...] = (
    "ai_ml", "software_saas", "cloud_data", "security", "semiconductor",
    "robotics", "fintech", "gaming", "it_services", "devtools", "consumer",
    "healthtech", "hardware", "telecom", "aerospace_defense", "automotive",
    "ecommerce", "media", "energy", "research", "other",
)

#: The seed file, the SIC classifier and the Workday catalog each invented
#: their own category vocabulary. This is the single translation into
#: `CATEGORIES`; anything unmapped becomes "other" rather than being dropped,
#: so a coverage report never loses an employer to a vocabulary mismatch.
_CATEGORY_ALIASES: Dict[str, str] = {
    "ai-ml": "ai_ml", "ai/ml": "ai_ml", "ai": "ai_ml", "ml": "ai_ml",
    "saas": "software_saas", "software": "software_saas",
    "big-tech": "software_saas", "big_tech": "software_saas",
    "enterprise-software": "software_saas",
    "cloud-infra": "cloud_data", "cloud_infrastructure": "cloud_data",
    "cloud": "cloud_data", "data": "cloud_data", "data_services": "cloud_data",
    "storage": "cloud_data", "computing": "cloud_data",
    "database": "cloud_data", "analytics": "cloud_data",
    "security": "security", "cybersecurity": "security", "privacy": "security",
    "semiconductor": "semiconductor", "semiconductor_equipment": "semiconductor",
    "semis": "semiconductor", "eda": "semiconductor", "chips": "semiconductor",
    "robotics-av": "robotics", "robotics_av": "robotics", "av": "robotics",
    "autonomous": "robotics", "drones": "robotics",
    "fintech": "fintech", "payments": "fintech", "banking": "fintech",
    "insurtech": "fintech",
    "gaming": "gaming", "games": "gaming", "game": "gaming",
    "it-services": "it_services", "it_services": "it_services",
    "consulting": "it_services", "systems_integration": "it_services",
    "business_services": "it_services", "outsourcing": "it_services",
    "devtools": "devtools", "developer-tools": "devtools",
    "dev-tools": "devtools", "observability": "devtools",
    "consumer": "consumer", "social": "consumer", "marketplace": "consumer",
    "healthtech": "healthtech", "medtech": "healthtech",
    "biotech_tools": "healthtech", "health": "healthtech",
    "hardware": "hardware", "instruments": "hardware",
    "electronics": "hardware", "networking": "hardware",
    "telecom": "telecom", "telecommunications": "telecom",
    "aerospace": "aerospace_defense", "defense": "aerospace_defense",
    "defense_tech": "aerospace_defense", "space": "aerospace_defense",
    "automotive": "automotive", "mobility": "automotive", "ev": "automotive",
    "ecommerce": "ecommerce", "e-commerce": "ecommerce", "retail": "ecommerce",
    "media": "media", "adtech": "media", "streaming": "media",
    "energy": "energy", "climate": "energy", "cleantech": "energy",
    "research": "research",
}


def canonical_category(value: Any) -> str:
    """Any registry's category string -> one of `CATEGORIES` ("other" if new)."""
    key = re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")
    if not key:
        return "other"
    if key in CATEGORIES:
        return key
    return _CATEGORY_ALIASES.get(key, _CATEGORY_ALIASES.get(key.replace("_", "-"), "other"))


# --------------------------------------------------------------------------
# Identity keys
# --------------------------------------------------------------------------
_LEGAL_SUFFIX_RE = re.compile(
    r"\b(inc|incorporated|corp|corporation|co|company|llc|l\.l\.c|ltd|limited|"
    r"plc|lp|llp|lllp|pllc|holdings?|group|the|sa|s\.a|nv|n\.v|ag|se|gmbh|"
    r"pte|pty|ab|as|oy|bv|b\.v|kk|srl|spa|kft|doo)\b", re.IGNORECASE)

#: Suffixes that are a registry artefact, not part of the registrable domain.
#: `co.uk`, `com.au` and friends have two labels below the registrable name.
_MULTI_LABEL_TLDS = frozenset({
    "co.uk", "org.uk", "ac.uk", "gov.uk", "co.jp", "or.jp", "ne.jp",
    "com.au", "net.au", "org.au", "co.nz", "com.br", "com.cn", "com.sg",
    "co.in", "co.il", "co.za", "com.mx", "com.tr", "co.kr", "com.hk",
})


def name_key(value: Any) -> str:
    """Comparable identity key: lowercase, legal suffixes stripped, squeezed.

    Deliberately identical in behaviour to `employer_identity.name_key` so the
    two modules agree on what "the same name" means. This key is used for
    *lookup* and for *quarantine*; on its own it never authorises a merge.
    """
    s = re.sub(r"[^a-z0-9]+", " ", str(value or "").lower())
    s = _LEGAL_SUFFIX_RE.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


def normalize_domain(value: Any) -> str:
    """URL or hostname -> registrable domain, lowercase, no `www.`.

    Returns "" for anything that is not a plausible public hostname, which the
    caller must treat as "no domain evidence" — never as a match key.
    """
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    raw = re.sub(r"^[a-z][a-z0-9+.-]*://", "", raw)
    raw = raw.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    raw = raw.split("@")[-1]
    raw = raw.split(":", 1)[0].strip(".")
    if not raw or " " in raw or "." not in raw:
        return ""
    if not re.fullmatch(r"[a-z0-9.-]+", raw):
        return ""
    labels = raw.split(".")
    if len(labels) < 2:
        return ""
    # An IP literal is never an employer identity. `192.168.1.1` would
    # otherwise reduce to the registrable-looking `1.1` and become a join key
    # that several unrelated employers could share.
    if all(re.fullmatch(r"\d{1,3}", part) for part in labels):
        return ""
    if not re.fullmatch(r"[a-z]{2,}", labels[-1]):
        return ""
    tail2 = ".".join(labels[-2:])
    keep = 3 if (tail2 in _MULTI_LABEL_TLDS and len(labels) >= 3) else 2
    return ".".join(labels[-keep:])


def _sha(prefix: str, value: str) -> str:
    return "emp_" + hashlib.sha1(f"{prefix}:{value}".encode("utf-8")).hexdigest()[:12]


def employer_id_for(
    *,
    domain: Any = None,
    curated_key: Any = None,
    name: Any = None,
) -> str:
    """The canonical id, strongest available evidence first.

    Raises `ValueError` when nothing identifying was supplied — an employer
    with no domain, no curated key and no name is not an employer, and
    minting an id for it would create a row nothing can ever join to.
    """
    dom = normalize_domain(domain)
    if dom:
        return _sha("domain", dom)
    ck = re.sub(r"[^a-z0-9]+", "-", str(curated_key or "").strip().lower()).strip("-")
    if ck:
        return _sha("curated", ck)
    nk = name_key(name)
    if nk:
        return _sha("name", nk)
    raise ValueError("employer_id_for: no domain, curated_key or name supplied")


def id_basis(employer: Dict[str, Any]) -> str:
    """`domain` | `curated` | `name` — what the row's id was derived from."""
    return str((employer or {}).get("id_basis") or "name")


#: How much to trust that this row is one real employer. Consumed by the
#: coverage report (a name-basis row is never counted as evidence-backed) and
#: by `merge_decision`.
CONFIDENCE_DOMAIN = 0.95
CONFIDENCE_CURATED = 0.80
CONFIDENCE_NAME = 0.45


def _confidence_for_basis(basis: str) -> float:
    return {"domain": CONFIDENCE_DOMAIN,
            "curated": CONFIDENCE_CURATED}.get(basis, CONFIDENCE_NAME)


# --------------------------------------------------------------------------
# Curated identity repairs
# --------------------------------------------------------------------------
#: The identity conflicts named in `docs/coverage/reports/tech-search-baseline-2026-09-01.md`, each
#: with the domain that settles it. A row here asserts ONE thing: this display
#: name, in a technology-employer context, means the entity at this domain.
#: It never asserts which ATS they use — that stays owner-verified.
#:
#: `wrong_names` are the entities that actually claimed the identity in the
#: live registry. Listing them is what lets the backfill *refuse* a merge it
#: would otherwise make on name similarity alone, which is the whole point.
IDENTITY_OVERRIDES: Tuple[Dict[str, Any], ...] = (
    {"display_name": "Stripe", "domain": "stripe.com",
     "cohorts": [COHORT_PRIVATE_GROWTH], "categories": ["fintech"],
     "wrong_names": ["STRIPES SOLUTIONS LLC", "Stripe Solutions",
                     "STRIPE-A-ZONE INC"]},
    {"display_name": "Notion Labs", "domain": "notion.so",
     "aliases": ["Notion"], "cohorts": [COHORT_PRIVATE_GROWTH],
     "categories": ["software_saas"],
     "wrong_names": ["NOTIONSBYNANCY LLC", "NOTIONS MARKETING CORPORATION"]},
    {"display_name": "Ramp", "domain": "ramp.com",
     "cohorts": [COHORT_PRIVATE_GROWTH], "categories": ["fintech"],
     # "RAMP HOLDINGS INC" is deliberately ABSENT: `name_key` strips
     # `holdings` and `inc`, so it normalises to `ramp` - the same key as the
     # employer itself - and listing it would make Ramp blacklist Ramp.
     # `_no_override_blacklists_itself` in tests/test_employer_registry.py
     # guards the whole table against that shape.
     "wrong_names": ["RAMPART RANGE MOTORCYCLE MANAGEMENT COMMITTEE"]},
    {"display_name": "Capital One", "domain": "capitalone.com",
     "legal_names": ["Capital One Financial Corporation"],
     "cohorts": [COHORT_MNC, COHORT_PUBLIC_TECH], "categories": ["fintech"],
     # `capital.com` is a CFD trading site with no relationship to Capital One.
     "wrong_domains": ["capital.com"],
     "wrong_names": ["CAPITAL ONE REALTY", "Capital.com"]},
    {"display_name": "Intuit", "domain": "intuit.com",
     "cohorts": [COHORT_PUBLIC_TECH], "categories": ["fintech", "software_saas"],
     "brands": ["Mailchimp", "Credit Karma", "TurboTax", "QuickBooks"],
     "wrong_names": ["INTUITIVE MACHINES", "Intuitive Surgical",
                     "INTUIT CONSULTING LLC"]},
    {"display_name": "JPMorgan Chase", "domain": "jpmorganchase.com",
     "legal_names": ["JPMORGAN CHASE & CO"],
     "aliases": ["JPMorgan", "Chase", "J.P. Morgan", "JPMC"],
     "cohorts": [COHORT_MNC], "categories": ["fintech"],
     "wrong_names": ["NUTMEG CONTAINER CORPORATION",
                     "CHASE FAMILY LIMITED PARTNERSHIP NO. 5",
                     "CHASE FAMILY LIMITED PARTNERSHIP NO. 3"]},
    {"display_name": "Boeing", "domain": "boeing.com",
     "legal_names": ["THE BOEING COMPANY", "BOEING CO"],
     "cohorts": [COHORT_MNC], "categories": ["aerospace_defense"],
     "wrong_names": ["M.J. WILLIAMS COMPANY"]},
    {"display_name": "Roche", "domain": "roche.com",
     "legal_names": ["F. Hoffmann-La Roche AG"],
     "brands": ["Genentech"],
     "cohorts": [COHORT_MNC], "categories": ["healthtech"],
     "wrong_names": ["GENE F. LANG & CO.", "COBAS SERVICES LLC"]},
    {"display_name": "AT&T", "domain": "att.com",
     "aliases": ["ATT", "AT&T Inc."],
     "cohorts": [COHORT_MNC], "categories": ["telecom"],
     "wrong_names": ["AIO GROUND BREAKING LLC", "AIO WIRELESS"]},
    {"display_name": "Abbott", "domain": "abbott.com",
     "legal_names": ["Abbott Laboratories"],
     "cohorts": [COHORT_MNC], "categories": ["healthtech"],
     "wrong_names": ["ABBOTT SOLUTIONS LLC", "Abbott BBQ", "Abbott's AI",
                     "ABBOTTS FROZEN CUSTARD"]},
    {"display_name": "NVIDIA", "domain": "nvidia.com",
     "legal_names": ["NVIDIA CORP", "NVIDIA Corporation"],
     "cohorts": [COHORT_PUBLIC_TECH, COHORT_MNC],
     "categories": ["semiconductor", "ai_ml"],
     "wrong_names": []},
    # Salesforce owns Slack; Slack is a brand, not a separate employer, and
    # `workday/salesforce` legitimately publishes under both names.
    {"display_name": "Salesforce", "domain": "salesforce.com",
     "legal_names": ["Salesforce, Inc.", "salesforce.com, inc."],
     "brands": ["Slack", "Tableau", "MuleSoft", "Heroku"],
     "cohorts": [COHORT_MNC, COHORT_PUBLIC_TECH],
     "categories": ["software_saas"], "wrong_names": []},
    {"display_name": "Alphabet", "domain": "abc.xyz",
     "brands": ["Google", "Waymo", "DeepMind", "Verily", "Wing", "X"],
     "cohorts": [COHORT_MNC, COHORT_PUBLIC_TECH],
     "categories": ["software_saas", "ai_ml"],
     # BMW's `ALPHABET` is a fleet-leasing subsidiary. Same word, different
     # employer, and it runs its own boards.
     "wrong_names": ["Alphabet Fleet Management", "Alphabet International GmbH",
                     "ALPHABET (BMW GROUP)"]},
)

#: `{name_key: employer_id}` for names that must NEVER resolve to the employer
#: they resemble. Built from every `wrong_names` entry above.
_WRONG_NAME_KEYS: Optional[Dict[str, str]] = None


def wrong_name_keys() -> Dict[str, str]:
    """`{name_key: the display name it must not be confused with}`.

    A `wrong_names` entry that normalises to the employer's OWN key is
    dropped rather than honoured: "RAMP HOLDINGS INC" reduces to `ramp` once
    `holdings` and `inc` are stripped, and honouring it would make Ramp
    unresolvable as itself. Silently skipping is right here — the entry is a
    no-op, not a contradiction the caller can act on — and a test asserts the
    table contains none.
    """
    global _WRONG_NAME_KEYS
    if _WRONG_NAME_KEYS is None:
        out: Dict[str, str] = {}
        for row in IDENTITY_OVERRIDES:
            own = {name_key(x) for x in
                   [row["display_name"], *(row.get("aliases") or ()),
                    *(row.get("legal_names") or ()), *(row.get("brands") or ())]}
            for bad in row.get("wrong_names") or ():
                k = name_key(bad)
                if k and k not in own:
                    out[k] = str(row["display_name"])
        _WRONG_NAME_KEYS = out
    return _WRONG_NAME_KEYS


def wrong_domains() -> Dict[str, str]:
    """`{domain: the display name it must not be attached to}`."""
    out: Dict[str, str] = {}
    for row in IDENTITY_OVERRIDES:
        for bad in row.get("wrong_domains") or ():
            d = normalize_domain(bad)
            if d:
                out[d] = str(row["display_name"])
    return out


def override_for_name(value: Any) -> Optional[Dict[str, Any]]:
    """The curated identity row whose display name / alias matches `value`."""
    k = name_key(value)
    if not k:
        return None
    for row in IDENTITY_OVERRIDES:
        candidates = [row["display_name"], *(row.get("aliases") or ()),
                      *(row.get("legal_names") or ())]
        if any(name_key(c) == k for c in candidates):
            return row
    return None


# --------------------------------------------------------------------------
# The record
# --------------------------------------------------------------------------
def _now() -> datetime:
    return datetime.now(timezone.utc)


#: How long a verified identity stays trusted before it must be re-checked.
#: Long, because an employer's domain rarely changes — but not infinite,
#: because acquisitions and shutdowns are exactly what this field exists to
#: catch, and "we last looked in 2026" must be visible in the report.
VERIFY_INTERVAL_DAYS = 90


def build_employer(
    *,
    display_name: Any,
    canonical_domain: Any = None,
    curated_key: Any = None,
    legal_names: Sequence[Any] = (),
    aliases: Sequence[Any] = (),
    brands: Sequence[Any] = (),
    verified_domains: Sequence[Any] = (),
    parent_employer_id: Any = None,
    operating_status: str = STATUS_ACTIVE,
    employer_categories: Sequence[Any] = (),
    cohorts: Sequence[Any] = (),
    countries: Sequence[Any] = (),
    evidence: Sequence[Dict[str, Any]] = (),
    first_seen_at: Optional[datetime] = None,
) -> Dict[str, Any]:
    """A complete `employers` document. Pure — no DB, no clock beyond `now`.

    `canonical_domain` is only accepted when it survives `normalize_domain`
    AND is not in `wrong_domains()`; a rejected domain becomes evidence of the
    rejection rather than being silently dropped, so the report can say why an
    employer stayed on a name-derived id.
    """
    name = str(display_name or "").strip()
    if not name:
        raise ValueError("build_employer: display_name is required")

    ev: List[Dict[str, Any]] = [dict(e) for e in (evidence or ()) if isinstance(e, dict)]

    dom = normalize_domain(canonical_domain)
    bad_domains = wrong_domains()
    if dom and bad_domains.get(dom) and name_key(bad_domains[dom]) == name_key(name):
        ev.append({"kind": "domain_rejected", "source": "identity_override",
                   "value": f"{dom} is not {name}'s domain"})
        dom = ""

    status = str(operating_status or STATUS_ACTIVE)
    if status not in OPERATING_STATUSES:
        status = STATUS_ACTIVE

    if dom:
        basis = "domain"
    elif curated_key:
        basis = "curated"
    else:
        basis = "name"
    eid = employer_id_for(domain=dom, curated_key=curated_key, name=name)

    def _clean(seq: Sequence[Any], limit: int = 40) -> List[str]:
        out, seen = [], set()
        for v in seq or ():
            s = str(v or "").strip()
            k = name_key(s)
            if not s or not k or k in seen:
                continue
            seen.add(k)
            out.append(s)
            if len(out) >= limit:
                break
        return out

    verified = []
    seen_d = set()
    for d in [dom, *(verified_domains or ())]:
        nd = normalize_domain(d)
        if nd and nd not in seen_d and not (
                bad_domains.get(nd) and name_key(bad_domains[nd]) == name_key(name)):
            seen_d.add(nd)
            verified.append(nd)

    cats: List[str] = []
    for c in employer_categories or ():
        cc = canonical_category(c)
        if cc not in cats:
            cats.append(cc)

    coh: List[str] = []
    for c in cohorts or ():
        s = str(c or "").strip().lower()
        if s in COHORTS and s not in coh:
            coh.append(s)
    if not coh:
        coh = [COHORT_UNKNOWN]

    now = _now()
    return {
        "employer_id": eid,
        "id_basis": basis,
        "identity_confidence": _confidence_for_basis(basis),
        "display_name": name,
        "name_key": name_key(name),
        "legal_names": _clean(legal_names),
        "aliases": _clean(aliases),
        "brands": _clean(brands),
        "parent_employer_id": str(parent_employer_id or "") or None,
        "operating_status": status,
        "canonical_domain": dom or None,
        "verified_domains": verified,
        "employer_categories": cats or ["other"],
        "cohorts": coh,
        "countries": _clean(countries, limit=30),
        "evidence": ev[:40],
        "first_seen_at": first_seen_at or now,
        "last_verified_at": now if (dom or curated_key) else None,
        "next_verification_at": now + timedelta(days=VERIFY_INTERVAL_DAYS),
        "registry_version": REGISTRY_VERSION,
        "updated_at": now,
    }


# --------------------------------------------------------------------------
# Merge adjudication
# --------------------------------------------------------------------------
MERGE = "merge"
DISTINCT = "distinct"
QUARANTINE = "quarantine"


def merge_decision(
    left: Dict[str, Any],
    right: Dict[str, Any],
    *,
    shared_owner_verified_tenant: bool = False,
) -> Tuple[str, str]:
    """`(MERGE | DISTINCT | QUARANTINE, reason)` for two candidate employers.

    The asymmetry is intentional. Evidence that two employers are the SAME has
    to be documentary (a shared verified domain, a curated alias, an
    owner-verified tenant). Evidence that they are DIFFERENT only has to be
    two different verified domains. Everything in between — including an exact
    name match — is a question for a human, because on this dataset an exact
    name match is wrong often enough to have broken five well-known employers.
    """
    ln, rn = str(left.get("display_name") or ""), str(right.get("display_name") or "")
    lk, rk = name_key(ln), name_key(rn)

    # A curated override that names one side and blacklists the other is the
    # single strongest signal available, and it always says "distinct".
    bad = wrong_name_keys()
    if bad.get(lk) and name_key(bad[lk]) == rk:
        return DISTINCT, f"curated: {ln!r} is explicitly not {rn!r}"
    if bad.get(rk) and name_key(bad[rk]) == lk:
        return DISTINCT, f"curated: {rn!r} is explicitly not {ln!r}"

    ld = set(left.get("verified_domains") or ())
    rd = set(right.get("verified_domains") or ())
    shared = ld & rd
    if shared:
        return MERGE, f"shared verified domain {sorted(shared)[0]}"
    if ld and rd:
        return DISTINCT, (f"different verified domains "
                          f"{sorted(ld)[0]} vs {sorted(rd)[0]}")

    ov = override_for_name(ln)
    if ov:
        family = {name_key(x) for x in
                  [ov["display_name"], *(ov.get("aliases") or ()),
                   *(ov.get("legal_names") or ()), *(ov.get("brands") or ())]}
        if rk in family:
            return MERGE, f"curated alias of {ov['display_name']}"

    if shared_owner_verified_tenant:
        return MERGE, "same owner-verified ATS tenant"

    if lk and lk == rk:
        # The trap. "Forward" the healthcare startup and "Forward" the
        # logistics firm both normalise to `forward`.
        return QUARANTINE, f"name-only match on {lk!r}; no domain evidence"

    return DISTINCT, "no shared identity evidence"


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------
def _db():
    from utils.db_connect import DBConnect
    return DBConnect().get_db()


def _col(name: str):
    return _db()[name]


def ensure_indexes() -> List[str]:
    """Create the registry's indexes. Idempotent; safe to run on every deploy."""
    created: List[str] = []
    try:
        emp = _col(EMPLOYERS)
        created.append(emp.create_index("employer_id", unique=True,
                                        name="employer_id_unique"))
        created.append(emp.create_index("canonical_domain",
                                        name="canonical_domain_idx", sparse=True))
        created.append(emp.create_index("verified_domains",
                                        name="verified_domains_idx"))
        created.append(emp.create_index("name_key", name="name_key_idx"))
        created.append(emp.create_index([("operating_status", 1), ("cohorts", 1)],
                                        name="status_cohort_idx"))
        created.append(emp.create_index([("employer_categories", 1),
                                         ("identity_confidence", -1)],
                                        name="category_confidence_idx"))
        created.append(emp.create_index("next_verification_at",
                                        name="next_verification_idx"))
        created.append(emp.create_index("parent_employer_id",
                                        name="parent_idx", sparse=True))
        created.append(emp.create_index("registry_refs.company_ids",
                                        name="ref_company_idx"))
        created.append(emp.create_index("registry_refs.tenant_keys",
                                        name="ref_tenant_idx"))
        rev = _col(MERGE_REVIEWS)
        created.append(rev.create_index("review_key", unique=True,
                                        name="review_key_unique"))
        created.append(rev.create_index([("resolved", 1), ("created_at", -1)],
                                        name="open_reviews_idx"))
    except Exception as exc:  # pragma: no cover - index creation must not break boot
        logger.warning("employer_registry.ensure_indexes: %s", exc)
    return [c for c in created if c]


def upsert_employer(record: Dict[str, Any]) -> str:
    """Insert or merge one employer document. Returns the `employer_id`.

    Merging is additive on the list fields: a second source that knows one
    more alias adds it, and no source can delete another's evidence. Scalars
    are only overwritten when the incoming row is at least as well-founded
    (`identity_confidence`), so a name-derived discovery row can never
    downgrade a domain-verified one.
    """
    eid = record.get("employer_id")
    if not eid:
        raise ValueError("upsert_employer: record has no employer_id")
    col = _col(EMPLOYERS)
    existing = col.find_one({"employer_id": eid})
    now = _now()

    if not existing:
        doc = dict(record)
        doc.setdefault("registry_refs", {})
        doc["updated_at"] = now
        col.update_one({"employer_id": eid}, {"$setOnInsert": doc}, upsert=True)
        return eid

    add_to_set: Dict[str, Any] = {}
    for field in ("legal_names", "aliases", "brands", "verified_domains",
                  "employer_categories", "cohorts", "countries"):
        vals = [v for v in (record.get(field) or []) if v]
        if vals:
            add_to_set[field] = {"$each": vals}
    ev = [e for e in (record.get("evidence") or []) if isinstance(e, dict)]
    if ev:
        # `$each` ONLY. `$slice` is a `$push` modifier and MongoDB rejects the
        # whole update with "Found unexpected fields after $each in $addToSet"
        # when it appears here - which failed silently in a bulk write and
        # dropped 16,339 of 24,175 employers on the first run. `$addToSet` is
        # the right operator regardless: an evidence entry is a fact about
        # WHERE this employer was seen, so re-running the backfill must not
        # append the same fact twice. Growth is bounded by the number of
        # distinct sources, not by the number of runs.
        add_to_set["evidence"] = {"$each": ev}

    set_fields: Dict[str, Any] = {"updated_at": now,
                                  "registry_version": REGISTRY_VERSION}
    inc_conf = float(record.get("identity_confidence") or 0)
    cur_conf = float(existing.get("identity_confidence") or 0)
    if inc_conf >= cur_conf:
        for field in ("display_name", "name_key", "id_basis",
                      "identity_confidence", "canonical_domain",
                      "operating_status", "parent_employer_id"):
            if record.get(field) is not None:
                set_fields[field] = record[field]
    if record.get("last_verified_at"):
        set_fields["last_verified_at"] = record["last_verified_at"]
        set_fields["next_verification_at"] = (
            record.get("next_verification_at")
            or now + timedelta(days=VERIFY_INTERVAL_DAYS))
    # `cohorts` defaults to ["unknown"]; once anything real is known, drop it.
    update: Dict[str, Any] = {"$set": set_fields}
    if add_to_set:
        update["$addToSet"] = add_to_set
    col.update_one({"employer_id": eid}, update)
    if add_to_set.get("cohorts"):
        real = [c for c in (record.get("cohorts") or []) if c != COHORT_UNKNOWN]
        if real:
            col.update_one({"employer_id": eid},
                           {"$pull": {"cohorts": COHORT_UNKNOWN}})
    return eid


def bulk_upsert_employers(records: Iterable[Dict[str, Any]], *,
                          batch_size: int = 500) -> Dict[str, int]:
    """`upsert_employer` for a whole registry, in batches. `{seen, written}`.

    One `update_one` per employer against a hosted cluster is a round trip per
    row: the 24,005-employer backfill took over 40 minutes and spent
    essentially all of it waiting. `bulk_write` sends a batch per round trip
    and turns that into under a minute. The per-row SEMANTICS are identical -
    the same `$set`/`$addToSet`/`$setOnInsert` shape, built by the same
    helper - because a bulk path that diverges from the single path is a
    second, untested implementation of the merge rule.

    `ordered=False` so one malformed row cannot abandon the rest of its batch.
    """
    from pymongo import UpdateOne
    from pymongo.errors import BulkWriteError

    col = _col(EMPLOYERS)
    seen = written = 0
    ops: List[Any] = []

    def _flush() -> int:
        if not ops:
            return 0
        try:
            res = col.bulk_write(ops, ordered=False)
        except BulkWriteError as exc:
            errs = exc.details.get("writeErrors") or []
            # LOUD, and with the server's own message. The first version
            # logged only a count, so "16,339 employers silently dropped"
            # looked identical to "everything fine" in the run output.
            logger.error("bulk_upsert_employers: %d write error(s); first: %s",
                         len(errs), (errs[0].get("errmsg") if errs else "")[:300])
            return int((exc.details.get("nUpserted") or 0)
                       + (exc.details.get("nModified") or 0))
        except (TypeError, NotImplementedError) as exc:
            # mongomock (and older drivers) do not implement every kwarg the
            # installed pymongo puts on an `UpdateOne`. Degrade to the
            # sequential path rather than failing the whole backfill: it is
            # the same update document either way, just slower.
            logger.debug("bulk_upsert_employers: bulk unavailable (%s); "
                         "falling back to sequential", type(exc).__name__)
            n = 0
            for op in ops:
                try:
                    col.update_one(op._filter, op._doc, upsert=True)
                    n += 1
                except Exception as inner:  # pragma: no cover
                    logger.warning("bulk_upsert_employers: %s", inner)
            return n
        return int((res.upserted_count or 0) + (res.modified_count or 0))

    for record in records:
        eid = record.get("employer_id")
        if not eid:
            continue
        seen += 1
        ops.append(UpdateOne({"employer_id": eid}, _merge_update(record), upsert=True))
        if len(ops) >= batch_size:
            written += _flush()
            ops = []
    written += _flush()
    return {"seen": seen, "written": written}


def _merge_update(record: Dict[str, Any]) -> Dict[str, Any]:
    """The additive merge, as an update document.

    Shared by `upsert_employer` and `bulk_upsert_employers`. Note what is
    NOT here: the confidence comparison. A bulk write cannot read the existing
    row, so scalar fields go in `$setOnInsert` and the strongest source wins
    by ORDER OF ARRIVAL instead - which is why the backfill feeds sources
    strongest-first. `upsert_employer` keeps the read-compare path for callers
    that arrive out of order.
    """
    now = _now()
    add_to_set: Dict[str, Any] = {}
    for field in ("legal_names", "aliases", "brands", "verified_domains",
                  "employer_categories", "cohorts", "countries"):
        vals = [v for v in (record.get(field) or []) if v]
        if vals:
            add_to_set[field] = {"$each": vals}
    ev = [e for e in (record.get("evidence") or []) if isinstance(e, dict)]
    if ev:
        add_to_set["evidence"] = {"$each": ev}   # see `upsert_employer`: no $slice
    for kind, values in (record.get("registry_refs") or {}).items():
        field = REF_FIELDS.get(str(kind))
        vals = [v for v in (values or []) if v]
        if field and vals:
            add_to_set[f"registry_refs.{field}"] = {"$each": vals[:200]}

    on_insert = {
        "employer_id": record["employer_id"],
        "first_seen_at": record.get("first_seen_at") or now,
        "display_name": record.get("display_name"),
        "name_key": record.get("name_key"),
        "id_basis": record.get("id_basis"),
        "identity_confidence": record.get("identity_confidence"),
        "canonical_domain": record.get("canonical_domain"),
        "operating_status": record.get("operating_status") or STATUS_ACTIVE,
        "parent_employer_id": record.get("parent_employer_id"),
        "last_verified_at": record.get("last_verified_at"),
        "next_verification_at": record.get("next_verification_at"),
    }
    update: Dict[str, Any] = {
        "$set": {"updated_at": now, "registry_version": REGISTRY_VERSION},
        "$setOnInsert": on_insert,
    }
    if add_to_set:
        update["$addToSet"] = add_to_set
    return update


#: `{kind: registry_refs field}`. ONE map, used by both the single-row
#: `link_reference` and the bulk `_merge_update`.
#:
#: They disagreed: `link_reference` and `resolve_employer` wrote and read
#: `registry_refs.company_ids`, while the bulk path wrote whatever kind string
#: the caller passed (`registry_refs.company`). The backfill uses the bulk
#: path, so every join key landed in a field the resolver never looked at and
#: `resolve_employer(company_id=...)` matched nothing on 23,782 employers.
#: Two spellings of a join key is not a join.
REF_FIELDS: Dict[str, str] = {
    "company": "company_ids",
    "company_ids": "company_ids",
    "tenant": "tenant_keys",
    "tenant_keys": "tenant_keys",
    "member": "member_keys",
    "member_keys": "member_keys",
    "catalog": "catalog_keys",
    "catalog_keys": "catalog_keys",
    "legacy_employer_id": "legacy_employer_ids",
    "legacy_employer_ids": "legacy_employer_ids",
}


def link_reference(employer_id: str, *, kind: str, value: Any) -> None:
    """Record that `value` (a company_id, tenant key, member_key, catalog key)
    names this employer. These are the join keys the rest of the platform
    already has, so nothing downstream needs to be rewritten at once."""
    v = str(value or "").strip()
    field = REF_FIELDS.get(kind)
    if not field:
        raise ValueError(f"link_reference: unknown kind {kind!r}")
    if not employer_id or not v:
        return
    _col(EMPLOYERS).update_one({"employer_id": employer_id},
                               {"$addToSet": {f"registry_refs.{field}": v}})


def quarantine_merge(
    left: Dict[str, Any],
    right: Dict[str, Any],
    reason: str,
) -> str:
    """File an ambiguous identity match for review instead of guessing.

    Returns the review key. Idempotent — re-running the backfill re-files the
    same pair without duplicating it.
    """
    a, b = sorted([str(left.get("employer_id") or ""),
                   str(right.get("employer_id") or "")])
    key = f"{a}|{b}"
    _col(MERGE_REVIEWS).update_one(
        {"review_key": key},
        {"$setOnInsert": {
            "review_key": key, "created_at": _now(), "resolved": False,
            "left": {k: left.get(k) for k in
                     ("employer_id", "display_name", "canonical_domain",
                      "id_basis", "registry_refs")},
            "right": {k: right.get(k) for k in
                      ("employer_id", "display_name", "canonical_domain",
                       "id_basis", "registry_refs")},
        },
         "$set": {"reason": reason, "last_seen_at": _now(),
                  "registry_version": REGISTRY_VERSION}},
        upsert=True)
    return key


def get_employer(employer_id: str) -> Optional[Dict[str, Any]]:
    return _col(EMPLOYERS).find_one({"employer_id": str(employer_id or "")})


def resolve_employer(
    *,
    domain: Any = None,
    name: Any = None,
    company_id: Any = None,
    tenant_key: Any = None,
) -> Optional[Dict[str, Any]]:
    """Find the canonical employer from whatever the caller happens to hold.

    Order is evidence strength, not convenience: a verified domain beats an
    existing join reference, which beats a name. A name-only lookup that hits
    a `wrong_names` entry returns **None** rather than the employer it
    resembles — refusing to answer is the correct answer there.
    """
    col = _col(EMPLOYERS)
    dom = normalize_domain(domain)
    if dom:
        hit = col.find_one({"verified_domains": dom})
        if hit:
            return _follow(hit)
    # BOTH spellings of every join key. The backfill wrote `registry_refs.
    # tenant` (4,156 employers carry it) while the resolver asked only for
    # `registry_refs.tenant_keys` (0 carry it), so the tenant lookup - the
    # STRONGEST key available for a Workday or Oracle board, and the one the
    # feed's own identity resolution adjudicates on - matched nothing at all.
    # This is the same reader/writer split that was already fixed once for
    # `company` / `company_ids`; two spellings of a join key is not a join.
    for fields, val in (
            (("registry_refs.company_ids", "registry_refs.company"), company_id),
            (("registry_refs.tenant_keys", "registry_refs.tenant"), tenant_key)):
        v = str(val or "").strip()
        if not v:
            continue
        hit = col.find_one({"$or": [{f: v} for f in fields]})
        if hit:
            return _follow(hit)
    nk = name_key(name)
    if nk:
        if nk in wrong_name_keys():
            return None
        hits = list(col.find({"$or": [{"name_key": nk},
                                      {"aliases": {"$in": [str(name)]}}]}).limit(3))
        if len(hits) == 1:
            return _follow(hits[0])
        # Two employers share the name and neither supplied a domain. This is
        # exactly the `Forward` / `Beyond` / `Public` collision; answering
        # would be a coin flip.
        if len(hits) > 1:
            logger.info("employer_registry.resolve_employer: ambiguous name %r "
                        "(%d matches); refusing", name, len(hits))
    return None


def _follow(row: Dict[str, Any], _depth: int = 0) -> Dict[str, Any]:
    """Follow `parent_employer_id` for renamed/acquired/merged/brand rows.

    Depth-capped: a cycle in the parent graph is a data error, and looping
    forever on a read path would take the feed down with it.
    """
    if _depth >= 4:
        return row
    if row.get("operating_status") in REDIRECTING_STATUSES and row.get("parent_employer_id"):
        parent = _col(EMPLOYERS).find_one({"employer_id": row["parent_employer_id"]})
        if parent and parent.get("employer_id") != row.get("employer_id"):
            return _follow(parent, _depth + 1)
    return row


def rekey_to_domain(employer_id: str, domain: Any, *, evidence: Dict[str, Any]) -> str:
    """Promote a name-derived employer to a domain-derived id, losslessly.

    The old row is kept with `operating_status: merged` and
    `parent_employer_id` pointing at the new one, so every stored reference to
    the old id still resolves through `_follow`. Nothing is deleted, which is
    what makes this reversible.
    """
    dom = normalize_domain(domain)
    if not dom:
        raise ValueError("rekey_to_domain: unusable domain")
    old = get_employer(employer_id)
    if not old:
        raise ValueError(f"rekey_to_domain: unknown employer {employer_id!r}")
    new_id = employer_id_for(domain=dom)
    if new_id == employer_id:
        return employer_id

    merged = build_employer(
        display_name=old.get("display_name"),
        canonical_domain=dom,
        legal_names=old.get("legal_names") or (),
        aliases=old.get("aliases") or (),
        brands=old.get("brands") or (),
        verified_domains=old.get("verified_domains") or (),
        parent_employer_id=old.get("parent_employer_id"),
        operating_status=old.get("operating_status") or STATUS_ACTIVE,
        employer_categories=old.get("employer_categories") or (),
        cohorts=old.get("cohorts") or (),
        countries=old.get("countries") or (),
        evidence=[*(old.get("evidence") or []), evidence],
        first_seen_at=old.get("first_seen_at"),
    )
    merged["registry_refs"] = old.get("registry_refs") or {}
    merged.setdefault("registry_refs", {}).setdefault("legacy_employer_ids", [])
    if employer_id not in merged["registry_refs"]["legacy_employer_ids"]:
        merged["registry_refs"]["legacy_employer_ids"].append(employer_id)
    upsert_employer(merged)
    _col(EMPLOYERS).update_one(
        {"employer_id": employer_id},
        {"$set": {"operating_status": STATUS_MERGED,
                  "parent_employer_id": new_id,
                  "updated_at": _now()}})
    return new_id


def counts() -> Dict[str, Any]:
    """Registry shape, for the coverage report and the metrics snapshot."""
    col = _col(EMPLOYERS)

    def _group(field: str) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for row in col.aggregate([{"$unwind": f"${field}"},
                                  {"$group": {"_id": f"${field}",
                                              "n": {"$sum": 1}}}]):
            out[str(row["_id"])] = int(row["n"])
        return out

    def _plain(field: str) -> Dict[str, int]:
        return {str(r["_id"]): int(r["n"]) for r in
                col.aggregate([{"$group": {"_id": f"${field}", "n": {"$sum": 1}}}])}

    return {
        "total": col.estimated_document_count(),
        "by_id_basis": _plain("id_basis"),
        "by_operating_status": _plain("operating_status"),
        "by_cohort": _group("cohorts"),
        "by_category": _group("employer_categories"),
        "domain_verified": col.count_documents({"canonical_domain": {"$ne": None}}),
        "open_merge_reviews": _col(MERGE_REVIEWS).count_documents({"resolved": False}),
        "registry_version": REGISTRY_VERSION,
    }
