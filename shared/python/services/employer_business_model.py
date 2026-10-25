"""Employer BUSINESS MODEL classification (`ebm-1.0.0`, contract §C1).

The question this answers
-------------------------
"Is the employer that publishes this posting a product/operating company, a
staffing agency, a consultancy/outsourcer, a talent marketplace, a mixed
business, or do we not know?" It is a separate axis from
`employer_tech_classifier` ("is this a technology company?") and from the
posting's own relationship (`posting_profile`): a consultancy can hire for
itself, and both facts are kept.

The record (`employers.business_model`)::

    {"kind": "operating|staffing|consultancy|talent_marketplace|mixed|unknown",
     "confidence": 0.0-1.0,
     "basis": "owner_override|research_register|registry_category|naics|site_self_description|name_rule|none",
     "evidence": [{"kind": "url|naics|category|rule|register", "value": ..., "source": ...}],
     "version": "ebm-1.0.0", "classified_at": ISO-8601}

Bases, strongest first, and what each may conclude
---------------------------------------------------
1. `owner_override` - `employers.business_model_override`, written by a human.
2. `research_register` - a dated, evidence-backed row in
   `employer_business_model_seed`, matched by REGISTRABLE DOMAIN only (the
   employer's canonical/verified domains, the deterministic
   `employer_id_for(domain=...)` of a canonical posting, or the posting's own
   URL host). Never a bare name.
3. `registry_category` - the employer's declared `employer_categories`.
   `it_services` -> consultancy (0.6 when a catalogue DECLARED it, 0.4 when it
   was derived from an SEC SIC code, which product companies also file);
   any other specific category -> operating (0.6).
4. `naics` - an industry code on a stored source. NAICS 5613 (Employment
   Services) and SIC 7361/7363 are staffing evidence; NAICS 5415 and SIC
   7371/7373 are consultancy-LIKELY at 0.35 only; SIC 8742 consultancy 0.5.
   `sponsorship_signals.naics` carries only the USCIS two-digit SECTOR (e.g.
   "54"), which cannot separate a consultancy from a software publisher, so a
   two-digit code is never evidence here.
5. `name_rule` - explicit words ("staffing", "recruitment", "consulting") or
   an exact well-known brand key. Low confidence, and it can ONLY ever yield
   staffing / consultancy / talent_marketplace. It never yields operating.
6. `none` -> `unknown`. Unknown is a real answer and is never rendered as
   direct.

A weaker basis that CONTRADICTS a `registry_category` operating verdict turns
the answer into `unknown` with both pieces of evidence: a catalogue category
and a staffing NAICS code disagreeing is a question for a human, not a coin
toss.

Nothing here touches the network. `resolve_rows` makes at most ONE `$in`
query on `employers` per call.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

EBM_VERSION = "ebm-1.1.0"

KIND_OPERATING = "operating"
KIND_STAFFING = "staffing"
KIND_CONSULTANCY = "consultancy"
KIND_MARKETPLACE = "talent_marketplace"
KIND_MIXED = "mixed"
KIND_UNKNOWN = "unknown"
KINDS = (KIND_OPERATING, KIND_STAFFING, KIND_CONSULTANCY, KIND_MARKETPLACE,
         KIND_MIXED, KIND_UNKNOWN)

BASIS_OVERRIDE = "owner_override"
BASIS_REGISTER = "research_register"
BASIS_CATEGORY = "registry_category"
BASIS_NAICS = "naics"
BASIS_NAME = "name_rule"
BASIS_SITE = "site_self_description"
BASIS_NONE = "none"
BASES = (BASIS_OVERRIDE, BASIS_REGISTER, BASIS_CATEGORY, BASIS_NAICS,
         BASIS_NAME, BASIS_NONE)

#: Name rule ceiling. Below every other basis by construction.
NAME_RULE_CONFIDENCE = 0.35

#: Hosts that are platforms, never an employer's identity. A posting whose URL
#: is on one of these says nothing about who the employer is.
PLATFORM_DOMAINS = frozenset({
    "linkedin.com", "indeed.com", "glassdoor.com", "ziprecruiter.com",
    "greenhouse.io", "lever.co", "ashbyhq.com", "myworkdayjobs.com",
    "myworkdaysite.com", "icims.com", "workable.com", "smartrecruiters.com",
    "breezy.hr", "applytojob.com", "jazzhr.com", "ultipro.com", "ukg.com",
    "paycomonline.net", "adp.com", "oraclecloud.com", "taleo.net",
    "jobvite.com", "dayforcehcm.com", "recruitingbypaycor.com",
    "newtonsoftware.com", "appone.com", "paychex.com", "ripplehire.com",
    "teamtailor.com", "rippling.com", "bamboohr.com", "recruitee.com",
    "successfactors.com", "successfactors.eu", "jobdiva.com", "eightfold.ai",
    "phenompeople.com", "avature.net", "ycombinator.com", "jobright.ai",
    "google.com", "simplify.jobs", "builtin.com", "wellfound.com",
})


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ev(kind: str, value: Any, source: str) -> Dict[str, str]:
    return {"kind": kind, "value": str(value or "")[:300], "source": str(source or "")[:120]}


def _model(kind: str, confidence: float, basis: str, evidence: List[Dict[str, Any]],
           *, now: Optional[datetime] = None, subtype: Optional[str] = None
           ) -> Dict[str, Any]:
    out = {
        "kind": kind if kind in KINDS else KIND_UNKNOWN,
        "confidence": round(max(0.0, min(1.0, float(confidence or 0.0))), 2),
        "basis": basis if basis in BASES else BASIS_NONE,
        "evidence": list(evidence or [])[:8],
        "version": EBM_VERSION,
        "classified_at": (now or _now()).isoformat(),
    }
    if subtype:
        out["subtype"] = str(subtype)[:60]
    return out


def unknown_model(*, now: Optional[datetime] = None,
                  evidence: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    return _model(KIND_UNKNOWN, 0.0, BASIS_NONE, evidence or [], now=now)


# ---------------------------------------------------------------------------
# Domains
# ---------------------------------------------------------------------------
def registrable_domain(value: Any) -> str:
    from services.employer_registry import normalize_domain
    return normalize_domain(value)


def employer_domain_of_url(url: Any) -> str:
    """The registrable domain of a posting URL, or "" when it is a platform.

    `careers.capgemini.com/job/...` -> `capgemini.com`. A Greenhouse, Workday
    or LinkedIn host is the VENDOR'S domain and is refused, so this can never
    attribute every customer of a vendor to one employer.
    """
    dom = registrable_domain(url)
    if not dom or dom in PLATFORM_DOMAINS:
        return ""
    return dom


# ---------------------------------------------------------------------------
# research_register (the seed), by domain only
# ---------------------------------------------------------------------------
_SEED_INDEX: Optional[Dict[str, Dict[str, Any]]] = None


def build_seed_index(exclude_names: Iterable[str] = (),
                     rows: Optional[Sequence[Dict[str, Any]]] = None
                     ) -> Dict[str, Dict[str, Any]]:
    """`{registrable domain: seed row}`. `exclude_names` is the eval's holdout.

    A row with no domain is skipped: it documents research we could not pin
    to an identity, and applying it by name is exactly what is forbidden.
    """
    from services import employer_business_model_seed as seed
    skip = {str(n) for n in exclude_names or ()}
    out: Dict[str, Dict[str, Any]] = {}
    for row in (rows if rows is not None else seed.SEED_ROWS):
        if row.get("name") in skip:
            continue
        for d in [row.get("domain"), *(row.get("alt_domains") or ())]:
            dom = registrable_domain(d)
            if dom and dom not in PLATFORM_DOMAINS and dom not in out:
                out[dom] = row
    return out


def seed_index() -> Dict[str, Dict[str, Any]]:
    global _SEED_INDEX
    if _SEED_INDEX is None:
        _SEED_INDEX = build_seed_index()
    return _SEED_INDEX


def _seed_by_employer_id(index: Dict[str, Dict[str, Any]]) -> Dict[str, Tuple[str, Dict[str, Any]]]:
    """`{employer_id_for(domain=d): (d, row)}` - the canonical id a domain-keyed
    employer carries, computed without a database."""
    from services.employer_registry import employer_id_for
    return {employer_id_for(domain=d): (d, row) for d, row in index.items()}


_SEED_EID: Optional[Dict[str, Tuple[str, Dict[str, Any]]]] = None


def _seed_eid_default() -> Dict[str, Tuple[str, Dict[str, Any]]]:
    global _SEED_EID
    if _SEED_EID is None:
        _SEED_EID = _seed_by_employer_id(seed_index())
    return _SEED_EID


def _from_seed(domain: str, row: Dict[str, Any], *, via: str,
               now: Optional[datetime] = None) -> Dict[str, Any]:
    ev = [_ev("register", f"{row.get('name')} ({domain}) -> {row.get('kind')}",
              str(row.get("source") or "")),
          _ev("url", row.get("evidence_url") or "", f"reviewed {row.get('reviewed_on')}")]
    if row.get("evidence"):
        ev.append(_ev("register", row["evidence"], "evidence"))
    ev.append(_ev("rule", f"matched by {via}", "resolution"))
    return _model(str(row.get("kind") or KIND_UNKNOWN),
                  float(row.get("confidence") or 0.0), BASIS_REGISTER, ev,
                  now=now, subtype=row.get("subtype"))


# ---------------------------------------------------------------------------
# registry_category
# ---------------------------------------------------------------------------
def _category_rule(employer: Dict[str, Any]) -> Optional[Tuple[str, float, List[Dict[str, Any]]]]:
    cats = [str(c).lower() for c in (employer.get("employer_categories") or []) if c]
    specific = [c for c in cats if c not in ("other", "")]
    if not specific:
        return None
    cat_ev = [e for e in (employer.get("category_evidence") or []) if isinstance(e, dict)]
    if "it_services" in specific:
        srcs = [str(e.get("source") or "") for e in cat_ev if e.get("category") == "it_services"]
        declared = [s for s in srcs if not s.startswith("sic:") and "sic" not in s]
        conf = 0.6 if (declared or not srcs) else 0.4
        others = [c for c in specific if c != "it_services"]
        ev = [_ev("category", "it_services", ",".join(srcs[:3]) or "employer_categories")]
        if others:
            ev.append(_ev("category", ",".join(others[:3]), "employer_categories"))
            return KIND_MIXED, 0.45, ev
        return KIND_CONSULTANCY, conf, ev
    return KIND_OPERATING, 0.6, [_ev("category", ",".join(specific[:3]),
                                     "employer_categories")]


# ---------------------------------------------------------------------------
# naics / sic
# ---------------------------------------------------------------------------
def _code_rule(codes: Iterable[Dict[str, Any]]) -> Optional[Tuple[str, float, List[Dict[str, Any]]]]:
    best: Optional[Tuple[str, float, List[Dict[str, Any]]]] = None
    for c in codes or ():
        if not isinstance(c, dict):
            continue
        system = str(c.get("system") or "naics").lower()
        code = re.sub(r"\D", "", str(c.get("code") or ""))
        src = str(c.get("source") or system)
        hit: Optional[Tuple[str, float]] = None
        if system == "naics" and len(code) >= 4:
            if code.startswith("5613"):
                hit = (KIND_STAFFING, 0.7)
            elif code.startswith("5415"):
                hit = (KIND_CONSULTANCY, 0.35)
        elif system == "sic" and len(code) == 4:
            if code in ("7361", "7363"):
                hit = (KIND_STAFFING, 0.7)
            elif code == "8742":
                hit = (KIND_CONSULTANCY, 0.5)
            elif code in ("7371", "7373"):
                hit = (KIND_CONSULTANCY, 0.35)
        if hit and (best is None or hit[1] > best[1]):
            best = (hit[0], hit[1], [_ev("naics", f"{system}:{code}", src)])
    return best


# ---------------------------------------------------------------------------
# name_rule - the weakest basis; never operating
# ---------------------------------------------------------------------------
_NAME_STAFFING_RE = re.compile(
    r"\b(staffing|recruit(?:ing|ment|ers)|executive search|search partners|"
    r"talent solutions|workforce solutions|employment (?:agency|services)|"
    r"manpower|personnel services|staff augmentation)\b", re.IGNORECASE)
_NAME_CONSULTANCY_RE = re.compile(
    r"\b(consulting|consultancy|consultants|it services|outsourcing|"
    r"systems integrators?)\b", re.IGNORECASE)
#: IT-services naming. Owner decision 2026-10-23: small IT consultancies are
#: hidden by default. Every one the 2026-10-22 link scan found on the board
#: was classified UNKNOWN - AGAP Technologies, ANJI Technologies, SVASTHA
#: Infotech, REVEL IT, PIT Solutions, GANTEC Publishing Solutions, AKZAC
#: Global, ACMESOFT Global - so no preference could hide them. This is the
#: weakest rule there is and is applied ONLY when nothing better is known: a
#: product company with a registry category is never reached here, and a
#: recognized firm (`is_recognized`) is never hidden for it.
_NAME_IT_SERVICES_RE = re.compile(
    r"\b((?:info ?tech\w*|infotek|infosoft|info ?solutions|it ?(?:solutions|services|consulting)|"
    r"soft ?tech\w*|software (?:solutions|services))(?:,?\s+(?:inc|llc|corp|ltd)\.?)?|"
    r"it,?\s+(?:inc|llc|corp)\.?|"
    r"tech(?:nology)? solutions|solutions,?\s+(?:inc|llc|corp|corporation)\.?|"
    r"global,?\s+(?:inc|llc)\.?|consulting group)$", re.IGNORECASE)
#: NOT "Technologies, Inc." / "Systems, Inc.": Cisco Systems, Bentley Systems,
#: Unity Technologies and many startups are named that way.
_NAME_MARKETPLACE_RE = re.compile(
    r"\b(jobgether|ziprecruiter|talent\.com|adzuna|jooble|whatjobs|lensa|"
    r"snagajob|dice|mercor|toptal|upwork|fiverr|remotehunter|torentify|wiraa|"
    r"talentally)\b", re.IGNORECASE)

#: Exact `name_key` matches only - "apex systems", never "apex".
_STAFFING_BRANDS = frozenset({
    "hays", "robert half", "randstad", "adecco", "manpowergroup", "kforce",
    "teksystems", "insight global", "apex systems", "collabera", "cybercoders",
    "beacon hill", "beacon hill staffing", "jobot", "aerotek", "allegis",
    "kelly services", "spherion", "modis", "akkodis", "experis", "judge",
    "vaco", "motion recruitment", "harnham", "optomi", "brooksource",
})
_CONSULTANCY_BRANDS = frozenset({
    "accenture", "accenture federal services", "deloitte", "cognizant",
    "infosys", "wipro", "tata consultancy services", "tcs", "capgemini",
    "hcltech", "hcl technologies", "tech mahindra", "ltimindtree", "mphasis",
    "ust", "ust global", "epam", "epam systems", "genpact", "ntt data", "cgi",
    "kpmg", "pwc", "ey", "ernst young",
})


def name_rule(name: Any) -> Optional[Tuple[str, float, List[Dict[str, Any]]]]:
    """(kind, confidence, evidence) from the NAME alone, or None.

    Can never return operating: no name proves a product company, and a
    missing staffing word is not evidence of anything.
    """
    from services.employer_registry import name_key
    raw = str(name or "").strip()
    if not raw:
        return None
    key = name_key(raw)
    if _NAME_MARKETPLACE_RE.search(raw):
        return KIND_MARKETPLACE, NAME_RULE_CONFIDENCE, [_ev("rule", "marketplace_name", raw)]
    if key in _STAFFING_BRANDS:
        return KIND_STAFFING, NAME_RULE_CONFIDENCE, [_ev("rule", "staffing_brand", raw)]
    if key in _CONSULTANCY_BRANDS:
        return KIND_CONSULTANCY, NAME_RULE_CONFIDENCE, [_ev("rule", "consultancy_brand", raw)]
    if _NAME_STAFFING_RE.search(raw):
        return KIND_STAFFING, 0.3, [_ev("rule", "staffing_word", raw)]
    if _NAME_CONSULTANCY_RE.search(raw):
        return KIND_CONSULTANCY, 0.3, [_ev("rule", "consultancy_word", raw)]
    if key in _VERIFIED_IT_SERVICES:
        return KIND_CONSULTANCY, NAME_RULE_CONFIDENCE, [
            {"kind": "rule", "value": "verified_by_link_scan_2026_10_22", "source": raw[:120]}]
    if _NAME_IT_SERVICES_RE.search(raw.strip(" ,.")):
        return KIND_CONSULTANCY, 0.25, [{"kind": "rule", "value": "it_services_name",
                                         "source": raw[:120]}]
    return None


#: Small IT consultancies the 2026-10-22 link scan of every user's board
#: proved, page by page: recruitment notices posted months or years earlier
#: (CURATE PARTNERS 38 stale of 72 cards, AGAP, GANTEC, TECHTIERA, SELECT
#: MINDS, INTELLYK), pages with no way to apply (TEC FRAMES, TKZUS, ACMESOFT),
#: closed roles (AKZAC), or a site that sells staffing (VBridgeIT, ALFVO).
_VERIFIED_IT_SERVICES = frozenset({
    "curate partners", "tec frames", "agap technologies", "techtiera", "select minds",
    "tkzus", "akzac global", "gantec publishing solutions", "intellyk", "vbridgeit",
    "alfvo", "anji technologies", "svastha infotech", "revel it", "acmesoft global",
    "transcend street solutions", "wintrio", "pit solutions",
})


#: Large, well-known staffing and consulting names. NOT a visibility rule any
#: more - since 2026-10-24 the default hides every staffing agency and
#: consultancy, these included. `scripts/classify_employer_sites.py` uses it
#: to skip firms whose kind is already known. `name_key` exact matches only.
RECOGNIZED_FIRMS = frozenset(_STAFFING_BRANDS | _CONSULTANCY_BRANDS | {
    "ibm", "booz allen hamilton", "booz allen", "leidos", "saic", "caci",
    "guidehouse", "slalom", "thoughtworks", "globant", "globallogic", "luxoft",
    "endava", "grid dynamics", "perficient", "mckinsey", "mckinsey company",
    "boston consulting group", "bcg", "bain", "bain company", "publicis sapient",
    "kyndryl", "dxc technology", "virtusa", "hexaware", "hexaware technologies",
    "persistent systems", "zs associates", "protiviti", "huron", "huron consulting group",
    "ernst young", "pricewaterhousecoopers", "deloitte consulting", "accenture federal",
    "ltimindtree", "larsen toubro infotech", "tata consultancy", "infosys limited",
    "cognizant technology solutions", "wipro limited", "hcl america", "capgemini america",
    "ntt data americas", "cgi technologies and solutions", "kforce", "robert half international",
    "randstad usa", "randstad digital", "adecco usa", "manpower", "experis",
    "insight global", "teksystems", "aerotek", "apex systems", "kelly services",
    "akkodis", "modis", "collabera", "cybercoders", "vaco", "motion recruitment",
    "beacon hill", "jobot", "brooksource", "optomi", "harnham", "hays", "allegis group",
    "mantech", "peraton", "general dynamics information technology", "gdit",
})


def is_recognized(name: Any) -> bool:
    """One of the large, well-known staffing or consulting names."""
    from services.employer_registry import name_key
    key = name_key(str(name or ""))
    return bool(key) and key in RECOGNIZED_FIRMS


# ---------------------------------------------------------------------------
# The classifier
# ---------------------------------------------------------------------------
def _override(employer: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    o = employer.get("business_model_override")
    if not isinstance(o, dict) or o.get("kind") not in KINDS:
        return None
    return o


def classify_employer(employer: Optional[Dict[str, Any]], *,
                      domains: Iterable[Any] = (),
                      industry_codes: Iterable[Dict[str, Any]] = (),
                      name: Any = None,
                      seed: Optional[Dict[str, Dict[str, Any]]] = None,
                      now: Optional[datetime] = None) -> Dict[str, Any]:
    """One employer -> its `business_model`. Pure: no DB, no network.

    `employer` is an `employers` document (or None for a posting with no
    canonical employer). `domains` are extra REGISTRABLE domains proven for
    this posting (its own URL host); `industry_codes` are
    `{"system": "naics"|"sic", "code", "source"}` rows a caller joined BY
    DOMAIN, never by name.
    """
    emp = employer or {}
    now = now or _now()
    seed = seed if seed is not None else seed_index()

    o = _override(emp)
    if o:
        return _model(o["kind"], float(o.get("confidence") or 0.95), BASIS_OVERRIDE,
                      [_ev("rule", o.get("evidence") or "owner override",
                           str(o.get("set_by") or "owner"))], now=now,
                      subtype=o.get("subtype"))

    cand_domains: List[str] = []
    for d in [emp.get("canonical_domain"), *(emp.get("verified_domains") or ()),
              *(domains or ())]:
        dom = registrable_domain(d)
        if dom and dom not in cand_domains:
            cand_domains.append(dom)
    for dom in cand_domains:
        row = seed.get(dom)
        if row is not None:
            return _from_seed(dom, row, via="domain", now=now)

    display = name if name is not None else emp.get("display_name")
    cat = _category_rule(emp) if emp else None
    code = _code_rule(industry_codes)
    named = name_rule(display)

    if cat is not None:
        kind, conf, ev = cat
        # The IT-services naming rule is too weak to dispute a category.
        weaker = [w for w in (code, named) if w is not None and w[0] != KIND_OPERATING
                  and not any(e.get("value") == "it_services_name" or
                              (e.get("kind") == "rule" and e.get("value") == "it_services_name")
                              for e in w[2])]
        if kind == KIND_OPERATING and weaker:
            evidence = list(ev)
            for w in weaker:
                evidence.extend(w[2])
            evidence.append(_ev("rule", "conflict: category says operating, weaker basis disagrees",
                                "classifier"))
            return _model(KIND_UNKNOWN, 0.0, BASIS_CATEGORY, evidence, now=now)
        return _model(kind, conf, BASIS_CATEGORY, ev, now=now)
    if code is not None:
        return _model(code[0], code[1], BASIS_NAICS, code[2], now=now)
    site = emp.get("site_self_description") if emp else None
    if isinstance(site, dict) and site.get("kind") in (KIND_STAFFING, KIND_CONSULTANCY):
        # What the employer's OWN website says it is ("a leading technical
        # staffing and recruitment agency", "an IT consulting firm"), read by
        # scripts/classify_employer_sites.py. Outranks the name.
        return _model(site["kind"], 0.6, BASIS_SITE,
                      [_ev("site", str(site.get("phrase") or "")[:160],
                           str(site.get("url") or ""))], now=now)
    if named is not None:
        return _model(named[0], min(named[1], NAME_RULE_CONFIDENCE), BASIS_NAME,
                      named[2], now=now)
    return unknown_model(now=now)


# ---------------------------------------------------------------------------
# Serve-time resolution for a batch of postings
# ---------------------------------------------------------------------------
_EMPLOYER_FIELDS = {"_id": 0, "employer_id": 1, "display_name": 1,
                    "canonical_domain": 1, "verified_domains": 1,
                    "employer_categories": 1, "category_evidence": 1,
                    "business_model": 1, "business_model_override": 1,
                    "site_self_description": 1}


def _row_name(row: Dict[str, Any]) -> str:
    return str(row.get("employer_name") or row.get("company") or "")


def _row_url_domains(row: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    for key in ("canonical_url", "apply_link", "url", "apply_url", "job_apply_link"):
        dom = employer_domain_of_url(row.get(key))
        if dom and dom not in out:
            out.append(dom)
    return out


def load_employers(ids: Iterable[str]) -> Dict[str, Dict[str, Any]]:
    wanted = sorted({str(i) for i in ids if i})
    if not wanted:
        return {}
    try:
        from utils.db_connect import DBConnect
        cur = DBConnect().get_db()["employers"].find(
            {"employer_id": {"$in": wanted}}, _EMPLOYER_FIELDS)
        return {str(d.get("employer_id")): d for d in cur}
    except Exception as exc:     # a registry outage must not 500 a feed
        logger.warning("employer_business_model: registry read failed (%s)",
                       type(exc).__name__)
        return {}


def profile_for_row(row: Dict[str, Any], *,
                    employers: Optional[Dict[str, Dict[str, Any]]] = None,
                    seed: Optional[Dict[str, Dict[str, Any]]] = None,
                    now: Optional[datetime] = None) -> Dict[str, Any]:
    """The employer business model for ONE posting row.

    Order: the canonical employer's STORED model (current version) -> the
    canonical employer classified now -> a seed row whose domain hashes to
    the canonical id -> the posting's own URL host -> the name rule ->
    unknown. `employers` is the batch `load_employers` result; None means
    "no database" (pure / offline callers).
    """
    seed = seed if seed is not None else seed_index()
    cid = str(row.get("canonical_employer_id") or "").strip()
    url_domains = _row_url_domains(row)
    emp = (employers or {}).get(cid) if cid else None
    if emp is not None:
        stored = emp.get("business_model")
        if isinstance(stored, dict) and stored.get("version") == EBM_VERSION \
                and stored.get("kind") in KINDS and not _override(emp):
            return dict(stored)
        return classify_employer(emp, domains=url_domains, seed=seed, now=now,
                                 name=emp.get("display_name") or _row_name(row))
    if cid:
        eid_map = (_seed_eid_default() if seed is seed_index()
                   else _seed_by_employer_id(seed))
        hit = eid_map.get(cid)
        if hit:
            return _from_seed(hit[0], hit[1], via="canonical_employer_id", now=now)
    return classify_employer(None, domains=url_domains, seed=seed,
                             name=_row_name(row), now=now)


def resolve_rows(rows: Sequence[Dict[str, Any]], *, attach: bool = True,
                 seed: Optional[Dict[str, Dict[str, Any]]] = None,
                 offline: bool = False) -> List[Dict[str, Any]]:
    """Business model per row, ONE registry query for the whole batch.

    Attaches `_employer_profile` to each row when `attach`. Rows that already
    carry a current `_employer_profile` are not re-resolved, so the stages of
    one request agree with each other.
    """
    now = _now()
    need = [r for r in rows if not _has_profile(r)]
    employers = ({} if offline else
                 load_employers(r.get("canonical_employer_id") for r in need))
    out: List[Dict[str, Any]] = []
    for row in rows:
        prof = row.get("_employer_profile") if _has_profile(row) else \
            profile_for_row(row, employers=employers, seed=seed, now=now)
        if attach:
            row["_employer_profile"] = prof
        out.append(prof)
    return out


def _has_profile(row: Dict[str, Any]) -> bool:
    p = row.get("_employer_profile")
    return isinstance(p, dict) and p.get("version") == EBM_VERSION


def public_profile(model: Dict[str, Any]) -> Dict[str, Any]:
    """The card's `employer_profile` block: the model, evidence trimmed."""
    m = dict(model or unknown_model())
    m["evidence"] = list(m.get("evidence") or [])[:4]
    return m
