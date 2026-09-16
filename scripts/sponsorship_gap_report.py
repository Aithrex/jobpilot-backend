"""The H-1B sponsor GAP LIST — demand we can prove, against inventory we hold.

READ-ONLY. It writes three files under ``docs/coverage/`` and touches no
collection.

``sponsorship_signals`` (written by ``scripts/ingest_sponsorship.py`` from the
USCIS H-1B Employer Data Hub) is the only demand signal on this platform that
comes from outside our own crawl: an employer that sponsored 3,000 H-1Bs last
fiscal year is hiring, whether or not we carry a single one of its postings.
This script joins that demand against the four things that decide whether we
can SHOW those jobs:

  ``employers``            — do we even know this employer exists?
  ``career_sources``       — do we know where its board is, and is that proven?
                             (only ``validated`` and ``empty`` count as
                             coverage — ADR-006)
  ``company_ats_endpoints``— do we have a fetchable endpoint for it?
  ``job_postings``         — are we actually carrying its jobs right now?

and assigns exactly one bucket per employer, most-covered first::

    covered                    >= 1 active posting today
    validated_but_zero_postings a proven board that currently returns nothing
    source_unvalidated         a board we found but have NOT proven is theirs
    no_career_source           an employers row with no board at all
    no_employer_row            we have never heard of this employer

Ranking is ``approvals x software_likelihood``, because the immediate product
goal is the SOFTWARE sector and raw approvals alone puts hospital systems,
universities and staffing agencies at the top. ``software_likelihood`` is
derived from the employer's OWN declared NAICS sector mix (approval-weighted)
plus name evidence, and every row carries the inputs so the number can be
argued with rather than trusted.

A domain is emitted ONLY when the ``employers`` registry already holds one for
the matched employer, with ``domain_source`` naming the evidence. Guessing
``<name>.com`` is exactly the failure mode ADR-006 exists to prevent, and the
agent that consumes this list is going to fetch whatever we print.

Usage::

    python scripts/sponsorship_gap_report.py                 # top 500, all formats
    python scripts/sponsorship_gap_report.py --top 2000
    python scripts/sponsorship_gap_report.py --min-approvals 25 --out docs/coverage
"""
import argparse
import csv
import json
import logging
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "shared" / "python"))
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

from services.sponsorship_service import normalize_employer  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("sponsorship_gap_report")

REPORT_VERSION = "sponsor-gap-1.0.0"

# Career-source states that COUNT as coverage (ADR-006 — see CLAUDE.md).
COVERING_STATES = ("validated", "empty")

BUCKET_COVERED = "covered"
# "Covered but under-read" is a THIRD answer, and it has to exist because the
# alternative is filing a fetch bug as a discovery gap. Eightfold declared
# page_size=20 against an API that returns 10 and ignores `num`, so every tenant
# stopped after page one: Microsoft read 29 postings instead of 1,178, Qualcomm
# 4 instead of 741, Lockheed 19 instead of 1,500+. Every one of those would have
# landed in a discovery queue that could not have fixed any of them.
BUCKET_UNDER_READ = "covered_under_read"
BUCKET_BOARD_HAS_JOBS = "board_publishes_jobs_we_hold_none"
BUCKET_VALIDATED_ZERO = "validated_but_zero_postings"
BUCKET_UNVALIDATED = "source_unvalidated"
BUCKET_NO_SOURCE = "no_career_source"
BUCKET_NO_EMPLOYER = "no_employer_row"
BUCKET_ORDER = [BUCKET_COVERED, BUCKET_UNDER_READ, BUCKET_BOARD_HAS_JOBS,
                BUCKET_VALIDATED_ZERO, BUCKET_UNVALIDATED,
                BUCKET_NO_SOURCE, BUCKET_NO_EMPLOYER]

#: We hold fewer than 1/2 of what the board last published, and the board
#: published at least this many — below it the ratio is noise (a board that
#: published 3 and we hold 1 is not a finding).
UNDER_READ_RATIO = 2.0
UNDER_READ_MIN_BOARD = 25

# ---------------------------------------------------------------------------
# Approvals are DEMAND. Postings we could show are SUPPLY, and for one cohort
# the two barely relate.
# ---------------------------------------------------------------------------
# Measured: LTIMindtree's board is reachable, owned and fully readable, and it
# publishes 54 jobs, NONE of them in the US — against 1,289 H-1B approvals. The
# staffing/IT-services model hires against client requisitions that never reach
# a careers site, so approvals do not convert into inventory we can carry. This
# cohort is 14,421 approvals, more than Apple, Microsoft and Google combined, so
# left unweighted it takes the whole head of a queue that is supposed to unlock
# postings. The discount is applied to the RANK only; `demand_score` keeps the
# unadjusted figure and every row states which basis was used.
CLIENT_SITE_STAFFING = frozenset({
    "infosys", "tata consultancy services", "cognizant", "wipro", "hcltech",
    "capgemini", "ltimindtree", "tech mahindra", "mphasis", "ust",
    "virtusa", "hexaware technologies", "coforge", "zensar technologies",
    "persistent systems", "birlasoft", "synechron", "genpact", "exlservice",
    "compunnel software", "infinite computer solutions", "htc global services",
    "innova solutions", "brillio", "atos syntel", "citiustech", "slk america",
    "v soft consulting", "intellectt", "tiger analytics", "erp analysts",
    "quadrant technologies", "randstad digital", "kforce", "insight",
    "populus", "cgi technologies and solutions", "ntt data", "l and t technology services",
    "globallogic", "nagarro", "iris software", "mastech digital", "collabera",
    "apex systems", "tek systems", "teksystems", "artech", "diverse lynx",
    "photon interactive", "sonata software", "happiest minds",
})
#: What we assume converts when we have no board reading at all for such a firm.
STAFFING_CONVERSION = 0.25
#: Floor, so a real employer never ranks at zero purely for a thin board day.
MIN_CONVERSION = 0.05


def conversion_factor(employer_norm, approvals, board_published):
    """How much of this employer's DEMAND could become inventory we can show.

    Evidence first: when our own crawl has read the employer's board, the number
    it published is the best available estimate of what we could carry, and no
    network request is needed to ask again. Only when we have never read a board
    does the cohort prior apply.
    """
    if board_published > 0 and approvals > 0:
        ratio = board_published / float(approvals)
        return round(max(MIN_CONVERSION, min(1.0, ratio)), 3), "board_published_last"
    if employer_norm in CLIENT_SITE_STAFFING:
        return STAFFING_CONVERSION, "client_site_staffing_cohort"
    return 1.0, "unadjusted"

# ---------------------------------------------------------------------------
# Software likelihood.
# ---------------------------------------------------------------------------
# USCIS prints the 2-digit NAICS SECTOR, not the 6-digit industry, so this is a
# coarse prior and is treated as one. It is the share of that sector's H-1B
# employment we would expect to be software/engineering work — 51 (Information)
# and 54 (Professional, Scientific & Technical Services) are where software
# publishers and IT-services firms file; 31-33 (Manufacturing) is where the
# semiconductor and hardware employers file, and semiconductor_hardware is a
# CORE family in tech_taxonomy, not an adjacent one.
NAICS_WEIGHT = {
    "51": 0.75,      # Information (software publishers, data processing)
    "54": 0.65,      # Professional, Scientific, and Technical Services
    "31-33": 0.45,   # Manufacturing — where Apple, NVIDIA, Cisco, Micron, Tesla
                     # and every fab file. semiconductor_hardware is a CORE
                     # family in tech_taxonomy, so this sector cannot be scored
                     # like a textile mill; the non-software name veto below is
                     # what separates the two.
    "52": 0.35,      # Finance and Insurance
    "55": 0.30,      # Management of Companies
    "56": 0.20,      # Administrative and Support Services
    "42": 0.15,      # Wholesale Trade
    "44-45": 0.15,   # Retail Trade
    "48-49": 0.12,   # Transportation and Warehousing
    "92": 0.10,      # Public Administration
    "22": 0.10,      # Utilities
    "71": 0.10,      # Arts, Entertainment, and Recreation
    "81": 0.08,      # Other Services
    "53": 0.06,      # Real Estate
    "21": 0.06,      # Mining, Quarrying, Oil and Gas
    "61": 0.05,      # Educational Services
    "62": 0.04,      # Health Care and Social Assistance
    "23": 0.03,      # Construction
    "11": 0.02,      # Agriculture
    "72": 0.02,      # Accommodation and Food Services
}
NAICS_WEIGHT_UNKNOWN = 0.25

# Name evidence. A token here is a claim the employer makes about itself in its
# own filed legal name, which is weaker than a NAICS code and stronger than
# nothing — so it can only RAISE a coarse sector prior to a floor, never invent
# a score on its own.
_STRONG_NAME = re.compile(
    r"\b(software|semiconductor|microelectronics|technolog(y|ies)|computing|"
    r"computer|cybersecurity|cyber|analytics|informatics|robotics|networks?|"
    r"digital|cloud|datacenter|data systems|artificial intelligence|ai labs?)\b")
_WEAK_NAME = re.compile(
    r"\b(systems?|solutions?|labs?|consulting|infotech|interactive|telecom|"
    r"tech|data|sciences?|engineering|automation|electronics?)\b")
# A declared qualifier that vetoes the technical reading, mirroring
# tech_taxonomy._DOMAIN_QUALIFIERS: an employer naming itself a hospital is a
# hospital even if it files under NAICS 54.
_NON_SOFTWARE_NAME = re.compile(
    r"\b(hospital|clinic|medical center|health system|healthcare system|"
    r"university|college|school district|academy|seminary|church|ministries|"
    r"restaurant|pizza|bakery|cafe|hotel|motel|resort|casino|"
    r"construction|roofing|plumbing|hvac|landscap\w*|paving|masonry|"
    r"farms?|dairy|orchard|poultry|trucking|towing|"
    r"nursing|dental|dentistry|pharmacy|veterinary|orthoped\w*|pediatric\w*|"
    r"cancer center|cancer institute|school of medicine|medical school|"
    r"home health|assisted living|rehabilitation|surgical center|urgent care|"
    r"behavioral health|physicians|"
    r"law offices?|attorneys?|realty|real estate|salon|spa|"
    r"county of|city of|board of education|public schools?)\b")


# Our own registry's categories, when it has an opinion. 20,654 of 23,791
# employers are categorised `other`, so this is evidence when present and
# silence otherwise — never a penalty (tech_taxonomy's rule: not knowing must
# not cost what being wrong costs).
CATEGORY_FLOOR = {
    "software_saas": 0.85, "it_services": 0.80, "cloud_data": 0.80,
    "devtools": 0.85, "ai_ml": 0.85, "security": 0.80, "gaming": 0.75,
    "robotics": 0.75, "semiconductor": 0.70, "hardware": 0.65,
    "fintech": 0.60, "ecommerce": 0.55, "telecom": 0.50, "media": 0.45,
    "automotive": 0.45, "aerospace_defense": 0.45, "healthtech": 0.45,
    "research": 0.40, "energy": 0.30, "consumer": 0.35,
}


def software_likelihood(name, naics_mix, categories=None):
    """0..1 prior that this employer's H-1B demand is SOFTWARE work.

    ``naics_mix`` is [{"code","approvals"}] — the employer's own sector
    declaration, weighted by the approvals filed under each. Returns
    (weight, evidence_dict) so the number is always auditable.
    """
    total = sum(max(0, int(m.get("approvals") or 0)) for m in (naics_mix or []))
    if total > 0:
        base = sum(NAICS_WEIGHT.get(str(m.get("code") or ""), NAICS_WEIGHT_UNKNOWN)
                   * max(0, int(m.get("approvals") or 0))
                   for m in naics_mix) / total
        basis = "naics_mix"
    else:
        base = NAICS_WEIGHT_UNKNOWN
        basis = "naics_unknown"
    low = str(name or "").lower()
    weight = base
    signals = []
    if _STRONG_NAME.search(low):
        weight = max(weight, 0.75)
        signals.append("name_strong")
    elif _WEAK_NAME.search(low):
        weight = max(weight, min(0.55, base + 0.15))
        signals.append("name_weak")
    floors = [CATEGORY_FLOOR[c] for c in (categories or []) if c in CATEGORY_FLOOR]
    if floors:
        weight = max(weight, max(floors))
        signals.append("employer_registry_category")
    if _NON_SOFTWARE_NAME.search(low):
        weight = min(weight, 0.08)
        signals.append("name_non_software_veto")
    weight = round(max(0.0, min(1.0, weight)), 3)
    return weight, {"naics_prior": round(base, 3), "basis": basis, "signals": signals,
                    "categories": sorted(c for c in (categories or []) if c != "other")}


# ---------------------------------------------------------------------------
# Loads.
# ---------------------------------------------------------------------------
def _db():
    from utils.db_connect import DBConnect
    return DBConnect().get_db()


def match_keys(value):
    """Every spelling of a name the two sides of this join might use.

    ``employer_registry.name_key`` turns "&" into a SPACE and
    ``sponsorship_service.normalize_employer`` turns it into " and ", so
    "Ernst & Young" is ``ernst young`` on one side and ``ernst and young`` on
    the other, and AT&T, Johnson & Johnson and Bain & Company all miss for the
    same reason. Indexing both spellings is the join; neither module is
    changed, because both are right about their own domain.
    """
    key = normalize_employer(value)
    if not key:
        return []
    out = [key]
    stripped = re.sub(r"\band\b", " ", key)
    stripped = re.sub(r"\s+", " ", stripped).strip()
    if stripped and stripped != key:
        out.append(stripped)
    return out


def build_name_index(rows):
    """(by_id, by_name, by_first_token) over ``employers`` rows.

    Separate from the DB read so the join can be tested without Mongo, and so
    there is exactly ONE definition of how a registry name is indexed.
    """
    by_id = {r["employer_id"]: r for r in rows if r.get("employer_id")}
    by_name = defaultdict(list)
    by_first_token = defaultdict(set)
    for row in rows:
        for raw in [row.get("name_key"), row.get("display_name")] + list(row.get("aliases") or []):
            for key in match_keys(raw):
                by_name[key].append(row)
                token = key.split(" ")[0]
                # >= 3 so acronym employers ("CGI", "SAP", "HCL") can be hinted
                # at; the hint still needs a full prefix relation on top.
                if len(token) >= 3:
                    by_first_token[token].add(key)
    return by_id, by_name, by_first_token


def load_employers(db):
    """normalized name -> employers row (best one), plus employer_id -> row."""
    t0 = time.time()
    rows = list(db.employers.find({}, {
        "_id": 0, "employer_id": 1, "name_key": 1, "aliases": 1, "display_name": 1,
        "canonical_domain": 1, "verified_domains": 1, "parent_employer_id": 1,
        "identity_confidence": 1, "operating_status": 1, "employer_categories": 1,
    }))
    by_id, by_name, by_first_token = build_name_index(rows)
    logger.info("employers: %d rows, %d distinct normalized names (%.1fs)",
                len(rows), len(by_name), time.time() - t0)
    return by_id, by_name, by_first_token


def load_alias_keys(db):
    """canonical employer_norm -> the other names its signal is filed under."""
    out = defaultdict(list)
    for row in db.sponsorship_signals.find(
            {"rollup.is_alias": True},
            {"_id": 0, "employer_norm": 1, "rollup.canonical_norm": 1}):
        canonical = (row.get("rollup") or {}).get("canonical_norm")
        if canonical:
            out[canonical].append(row["employer_norm"])
    return out


def load_career_sources(db):
    t0 = time.time()
    out = defaultdict(list)
    for row in db.career_sources.find({}, {
            "_id": 0, "employer_id": 1, "state": 1, "ats_vendor": 1, "tenant": 1,
            "member_key": 1, "ownership_verified": 1, "canonical_endpoint": 1,
            "job_count_last": 1}):
        if row.get("employer_id"):
            out[row["employer_id"]].append(row)
    logger.info("career_sources: %d employers with >=1 source (%.1fs)",
                len(out), time.time() - t0)
    return out


def load_ats_endpoints(db):
    t0 = time.time()
    out = {}
    for row in db.company_ats_endpoints.find({}, {"_id": 0, "member_key": 1,
                                                  "status": 1, "vendor": 1, "tenant": 1}):
        if row.get("member_key"):
            out[row["member_key"]] = row
    logger.info("company_ats_endpoints: %d member keys (%.1fs)", len(out), time.time() - t0)
    return out


def load_posting_counts(db):
    """Active postings by canonical employer id, by (ats, tenant) BOARD, and by name.

    THE PRECEDENCE IS THE POINT, and it is the feed's own
    (`tech_feed_service.employer_key_of`): canonical id, then the board, then a
    name. `employer_name` still carries whatever the board printed — a Boeing
    posting carries `employer_name: "M.J. WILLIAMS COMPANY"`, the same slug bug
    that gives JPMorgan three names — so counting on names reads a
    well-covered employer as zero and, worse, can credit one employer with
    another's postings. The name path survives only as the LAST resort, for
    sponsors whose boards we have never proven and which therefore carry no
    canonical id at all.
    """
    t0 = time.time()
    core = {"$sum": {"$cond": [{"$eq": ["$tech.tech_scope", "core"]}, 1, 0]}}
    by_canonical = {}
    for row in db.job_postings.aggregate([
        {"$match": {"status": "active", "canonical_employer_id": {"$type": "string"}}},
        {"$group": {"_id": "$canonical_employer_id", "active": {"$sum": 1}, "core": core}},
    ], allowDiskUse=True):
        by_canonical[row["_id"]] = {"active": row["active"], "core": row["core"]}
    by_board = {}
    for row in db.job_postings.aggregate([
        {"$match": {"status": "active"}},
        {"$group": {"_id": {"ats": "$ats", "tenant": "$tenant"},
                    "active": {"$sum": 1}, "core": core}},
    ], allowDiskUse=True):
        ats = str((row["_id"] or {}).get("ats") or "").strip().lower()
        tenant = str((row["_id"] or {}).get("tenant") or "").strip().lower()
        if ats and tenant:
            by_board[(ats, tenant)] = {"active": row["active"], "core": row["core"]}
    by_name = defaultdict(lambda: {"active": 0, "core": 0})
    for row in db.job_postings.aggregate([
        {"$match": {"status": "active"}},
        {"$group": {"_id": "$employer_name", "active": {"$sum": 1}, "core": core}},
    ], allowDiskUse=True):
        key = normalize_employer(row["_id"])
        if not key:
            continue
        by_name[key]["active"] += row["active"]
        by_name[key]["core"] += row["core"]
    logger.info("job_postings: %d canonical employers, %d boards, %d names (%.1fs)",
                len(by_canonical), len(by_board), len(by_name), time.time() - t0)
    return by_canonical, by_board, dict(by_name)


def load_census_absent(db):
    """The INVERSE finding: employers we carry that provably filed nothing.

    They are not gaps — we already have their postings — but they are the other
    half of the same question, and the one the user's complaint is about: a feed
    dominated by employers who sponsor nobody.
    """
    rows = list(db.sponsorship_signals.find(
        {"census_absent": True},
        {"_id": 0, "employer_norm": 1, "employer_display": 1, "census": 1}))
    rows.sort(key=lambda r: -int((r.get("census") or {}).get(
        "inventory_active_postings") or 0))
    return rows


def load_sponsors(db, min_approvals):
    t0 = time.time()
    rows = list(db.sponsorship_signals.find(
        {"rollup.is_alias": False, "approvals_5y": {"$gte": max(0, min_approvals)}},
        {"_id": 0, "employer_norm": 1, "employer_display": 1, "approvals_5y": 1,
         "denials_5y": 1, "naics": 1, "location": 1, "rollup.basis": 1,
         "rollup.entity_count": 1, "rollup.entities.employer_norm": 1,
         "data_window": 1}))
    logger.info("sponsorship_signals: %d employers with >=%d approvals (%.1fs)",
                len(rows), min_approvals, time.time() - t0)
    return rows


# ---------------------------------------------------------------------------
# Join.
# ---------------------------------------------------------------------------
def _follow(row, by_id, depth=0):
    parent = row.get("parent_employer_id")
    if parent and parent in by_id and depth < 5:
        return _follow(by_id[parent], by_id, depth + 1)
    return row


def sponsor_names(sponsor, alias_keys):
    """The EXACT normalized names this sponsor is filed under.

    Used for the posting join, where the other side is also
    ``normalize_employer`` output, so no spelling variants belong here — a
    variant could collide with a different employer's postings.
    """
    keys = [sponsor["employer_norm"]]
    keys += alias_keys.get(sponsor["employer_norm"], [])
    keys += [e.get("employer_norm") for e in
             (sponsor.get("rollup", {}).get("entities") or [])]
    out = []
    for key in keys:
        if key and key not in out:
            out.append(key)
    return out


def sponsor_keys(sponsor, alias_keys):
    """``sponsor_names`` plus the "&"-spelling variants, for the REGISTRY join."""
    out = []
    for key in sponsor_names(sponsor, alias_keys):
        for variant in match_keys(key):
            if variant not in out:
                out.append(variant)
    return out


def match_employer(sponsor, by_id, by_name, alias_keys):
    """Best ``employers`` row for a sponsor, or None. Never invents one.

    Tries the employer key, then its curated aliases, then every legal entity
    that rolled into it — each in both "&" spellings. When several registry
    rows answer to one name the strongest identity wins and ``ambiguous``
    records that a human may need to look.
    """
    primary = sponsor["employer_norm"]
    for key in sponsor_keys(sponsor, alias_keys):
        rows = by_name.get(key)
        if not rows:
            continue
        best = max(rows, key=lambda r: (float(r.get("identity_confidence") or 0),
                                        str(r.get("employer_id") or "")))
        return {
            "row": _follow(best, by_id),
            "matched_on": key,
            "matched_via": "employer_key" if key in match_keys(primary) else "alias_or_entity",
            "ambiguous": len({r.get("employer_id") for r in rows}) > 1,
        }
    return None


def registry_hint(sponsor, by_name, by_first_token, alias_keys):
    """An UNVERIFIED suggestion for a sponsor with no registry match.

    "CGI TECHNOLOGIES AND SOLUTIONS INC" and the registry's "CGI INC" are
    probably the same company, and a prefix relation is exactly the evidence
    ADR-006 says may not merge anything. So it is emitted as a hint for the
    human/agent working the row, is never used for bucketing, and says so in
    its own field name.
    """
    for key in sponsor_keys(sponsor, alias_keys):
        token = key.split(" ")[0]
        if len(token) < 3:
            continue
        for candidate in by_first_token.get(token, ()):
            if candidate == key:
                continue
            if candidate.startswith(key + " ") or key.startswith(candidate + " "):
                rows = by_name.get(candidate) or []
                if len({r.get("employer_id") for r in rows}) == 1:
                    return {"name": candidate, "employer_id": rows[0].get("employer_id"),
                            "basis": "name_prefix_only_UNVERIFIED"}
    return None


def build_rows(sponsors, by_id, by_name, by_first_token, alias_keys,
               sources_by_employer, ats_by_member,
               postings_by_canonical, postings_by_board=None, postings_by_name=None):
    rows = []
    for sponsor in sponsors:
        norm = sponsor["employer_norm"]
        approvals = int(sponsor.get("approvals_5y") or 0)
        naics = sponsor.get("naics") or {}
        match = match_employer(sponsor, by_id, by_name, alias_keys)
        employer = match["row"] if match else None
        employer_id = employer.get("employer_id") if employer else ""
        weight, evidence = software_likelihood(
            sponsor.get("employer_display") or norm, naics.get("mix"),
            (employer or {}).get("employer_categories"))
        hint = None if employer else registry_hint(sponsor, by_name, by_first_token, alias_keys)

        srcs = sources_by_employer.get(employer_id, []) if employer_id else []
        states = sorted({str(s.get("state") or "") for s in srcs})
        covering = [s for s in srcs if s.get("state") in COVERING_STATES]
        endpoint_known = any(
            s.get("member_key") in ats_by_member or s.get("canonical_endpoint")
            for s in srcs)

        postings_by_board = postings_by_board or {}
        postings_by_name = postings_by_name or {}

        # PRECEDENCE, not a maximum: canonical id, then the board, then a name.
        # `employer_name` carries whatever the board printed (a Boeing posting
        # says "M.J. WILLIAMS COMPANY"), so a name can both miss an employer we
        # cover and credit us with someone else's postings. Each level is taken
        # ONLY when the one above it has nothing.
        boards = sorted({(str(src.get("ats_vendor") or "").strip().lower(),
                          str(src.get("tenant") or "").strip().lower())
                         for src in srcs
                         if src.get("ats_vendor") and src.get("tenant")})
        board_counts = {"active": 0, "core": 0}
        for board in boards:
            hit = postings_by_board.get(board)
            if hit:
                board_counts["active"] += hit["active"]
                board_counts["core"] += hit["core"]
        name_counts = {"active": 0, "core": 0}
        # Deduped, and the "&" variants are included: "ELI LILLY AND COMPANY"
        # files as `eli lilly and` while its postings say "Eli Lilly".
        for key in sponsor_keys(sponsor, alias_keys):
            hit = postings_by_name.get(key)
            if hit:
                name_counts["active"] += hit["active"]
                name_counts["core"] += hit["core"]

        canonical_counts = postings_by_canonical.get(employer_id) if employer_id else None
        if canonical_counts:
            active, core, count_basis = (canonical_counts["active"],
                                         canonical_counts["core"],
                                         "canonical_employer_id")
        elif board_counts["active"]:
            active, core, count_basis = (board_counts["active"], board_counts["core"],
                                         "ats_tenant")
        elif name_counts["active"]:
            active, core, count_basis = (name_counts["active"], name_counts["core"],
                                         "employer_name")
        else:
            active, core, count_basis = 0, 0, "none"

        # What the BOARD itself last reported, from our own crawl — no network.
        # An employer whose proven board published 1,500 jobs while we hold 19
        # is not a discovery gap, it is an under-read (the Eightfold pagination
        # bug did exactly this to Microsoft, Qualcomm, Lockheed and Netflix).
        board_published = 0
        for src in srcs:
            board_published = max(board_published, int(src.get("job_count_last") or 0))
            hit = ats_by_member.get(src.get("member_key"))
            if hit:
                board_published = max(board_published, int(hit.get("job_count_last") or 0))

        if (active > 0 and board_published >= UNDER_READ_MIN_BOARD
                and board_published >= active * UNDER_READ_RATIO):
            bucket = BUCKET_UNDER_READ
        elif active > 0:
            bucket = BUCKET_COVERED
        elif covering and board_published > 0:
            bucket = BUCKET_BOARD_HAS_JOBS
        elif covering:
            bucket = BUCKET_VALIDATED_ZERO
        elif srcs:
            bucket = BUCKET_UNVALIDATED
        elif employer:
            bucket = BUCKET_NO_SOURCE
        else:
            bucket = BUCKET_NO_EMPLOYER

        conversion, conversion_basis = conversion_factor(norm, approvals, board_published)

        domain = ""
        domain_source = "none"
        if employer:
            verified = [d for d in (employer.get("verified_domains") or []) if d]
            if verified:
                domain, domain_source = verified[0], "employer_registry_verified"
            elif employer.get("canonical_domain"):
                domain, domain_source = employer["canonical_domain"], "employer_registry_unverified"

        rows.append({
            "rank": 0,
            "employer_norm": norm,
            "employer_display": sponsor.get("employer_display") or norm,
            "approvals": approvals,
            "denials": int(sponsor.get("denials_5y") or 0),
            "software_likelihood": weight,
            "demand_score": round(approvals * weight, 1),
            "conversion": conversion,
            "conversion_basis": conversion_basis,
            "priority_score": round(approvals * weight * conversion, 1),
            "bucket": bucket,
            "naics_code": naics.get("primary") or "",
            "naics_label": naics.get("label") or "",
            # The PRIMARY code alone is not a sufficient signal and the file
            # proves it: Amazon's most-approved sector is "44-45 Retail Trade"
            # with "51 Information" second. The weight above is computed from
            # this MIX, so the mix is emitted beside the code rather than left
            # in the database where nobody reading the CSV would see it.
            "naics_mix": "|".join(f"{m.get('code')}:{m.get('approvals')}"
                                  for m in (naics.get("mix") or [])[:5]),
            "city": (sponsor.get("location") or {}).get("city") or "",
            "state": (sponsor.get("location") or {}).get("state") or "",
            "employer_id": employer_id,
            "employer_match": (match or {}).get("matched_via", "none"),
            "employer_match_ambiguous": bool((match or {}).get("ambiguous")),
            "employer_operating_status": (employer or {}).get("operating_status") or "",
            "registry_hint_unverified": (hint or {}).get("name", ""),
            "registry_hint_employer_id": (hint or {}).get("employer_id", ""),
            "domain": domain,
            "domain_source": domain_source,
            "career_source_count": len(srcs),
            "career_source_states": states,
            "career_source_covering": len(covering),
            "ats_endpoint_known": bool(endpoint_known),
            "ats_boards": sorted({f"{s.get('ats_vendor')}:{s.get('tenant')}"
                                  for s in srcs if s.get("ats_vendor")})[:6],
            "active_postings": active,
            "core_tech_postings": core,
            "posting_count_basis": count_basis,
            "postings_by_canonical_id": (canonical_counts or {}).get("active", 0),
            "postings_by_board": board_counts["active"],
            "postings_by_name": name_counts["active"],
            "board_published_last": board_published,
            "rollup_basis": (sponsor.get("rollup") or {}).get("basis", ""),
            "rollup_entity_count": (sponsor.get("rollup") or {}).get("entity_count", 1),
            "software_evidence": evidence,
            "gap_reason": GAP_REASON[bucket],
        })
    return rows


GAP_REASON = {
    BUCKET_COVERED: "already carrying active postings",
    BUCKET_UNDER_READ: "we hold postings but the board last published at least "
                       "twice as many — a fetch/pagination problem, not a "
                       "discovery one",
    BUCKET_BOARD_HAS_JOBS: "the board is proven ours and last published jobs, "
                           "and we hold none of them — re-read the endpoint "
                           "before discovering anything",
    BUCKET_VALIDATED_ZERO: "board proven ours and returns nothing right now — "
                           "re-check the endpoint before discovering a new one",
    BUCKET_UNVALIDATED: "a board is registered but not proven to be theirs "
                        "(career_sources state is not validated/empty)",
    BUCKET_NO_SOURCE: "we know the employer, we do not know where its board is",
    BUCKET_NO_EMPLOYER: "no employers row at all — this employer is invisible to us",
}


def posting_model(rows):
    """Median active postings per KNOWN sponsor, by approvals band.

    This is the only defensible way to answer "how much inventory would this
    bucket unlock": we cannot know the size of a board we have never fetched,
    so we use what comparable sponsors we already know about actually hold.

    The reference population is every sponsor with an ``employers`` row —
    INCLUDING the ones holding zero postings. Using only covered sponsors
    would condition on success: those are the employers we deliberately went
    and sourced, so their median answers "how big is a board we chose to
    integrate", not "how big is the next one". It is an estimate either way and
    is labelled one everywhere it appears.
    """
    bands = [(0, 10), (10, 50), (50, 200), (200, 1000), (1000, 10 ** 9)]

    def band_of(n):
        for lo, hi in bands:
            if lo <= n < hi:
                return f"{lo}-{hi if hi < 10 ** 9 else '+'}"
        return "0-10"

    known = defaultdict(list)
    for row in rows:
        if row["employer_id"]:
            known[band_of(row["approvals"])].append(row["active_postings"])
    medians = {}
    for band, values in known.items():
        values.sort()
        medians[band] = values[len(values) // 2]
    return band_of, medians


def summarize(rows):
    band_of, medians = posting_model(rows)
    buckets = {}
    for bucket in BUCKET_ORDER:
        members = [r for r in rows if r["bucket"] == bucket]
        estimate = 0
        if bucket != BUCKET_COVERED:
            for row in members:
                estimate += medians.get(band_of(row["approvals"]), 0)
        buckets[bucket] = {
            "employers": len(members),
            "approvals": sum(r["approvals"] for r in members),
            "software_weighted_approvals": round(sum(r["demand_score"] for r in members), 1),
            "ranked_score": round(sum(r["priority_score"] for r in members), 1),
            "active_postings_today": sum(r["active_postings"] for r in members),
            "core_tech_postings_today": sum(r["core_tech_postings"] for r in members),
            "estimated_postings_if_covered": estimate,
            "estimate_method": ("n/a — already covered" if bucket == BUCKET_COVERED else
                                "median active postings held by sponsors we already have "
                                "an employers row for, in the same approvals band, zeros "
                                "included; an estimate, not a measurement"),
            "gap_reason": GAP_REASON[bucket],
        }
    return {"buckets": buckets, "median_postings_per_covered_sponsor_by_band": medians}


CSV_FIELDS = ["rank", "employer_display", "employer_norm", "approvals", "denials",
              "software_likelihood", "demand_score", "conversion",
              "conversion_basis", "priority_score", "bucket", "gap_reason",
              "naics_code", "naics_mix", "naics_label", "city", "state",
              "domain", "domain_source",
              "employer_id", "employer_match", "career_source_count",
              "career_source_states", "ats_endpoint_known", "ats_boards",
              "active_postings", "board_published_last", "registry_hint_unverified",
              "core_tech_postings", "posting_count_basis", "rollup_basis",
              "rollup_entity_count"]


def write_outputs(out_dir, rows, gaps, summary, meta, top):
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "h1b-sponsor-gaps.json"
    csv_path = out_dir / "h1b-sponsor-gaps.csv"
    md_path = out_dir / "h1b-sponsor-coverage.md"

    json_path.write_text(json.dumps({
        "generated_at": meta["generated_at"],
        "report_version": REPORT_VERSION,
        "data_window": meta["data_window"],
        "source": meta["source"],
        "totals": meta["totals"],
        "census_absent": meta["census_absent"],
        "summary": summary,
        "ranking": "approvals x software_likelihood x conversion, restricted to "
                   "employers we do not already cover. `demand_score` is the "
                   "unadjusted approvals x software_likelihood; `conversion` is "
                   "how much of that demand could become postings we can show, "
                   "from the employer's own last-published board size where we "
                   "have read it.",
        "count": len(gaps),
        "gaps": gaps[:top],
    }, indent=2, default=str), encoding="utf-8")

    with open(csv_path, "w", encoding="utf-8", newline="") as fh:
        # LF, not the csv module's default CRLF: this repo pins line endings so
        # real diffs are not drowned in CRLF churn.
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS, extrasaction="ignore",
                                lineterminator="\n")
        writer.writeheader()
        for row in gaps[:top]:
            flat = dict(row)
            flat["career_source_states"] = "|".join(row["career_source_states"])
            flat["ats_boards"] = "|".join(row["ats_boards"])
            writer.writerow(flat)

    md_path.write_text(render_markdown(rows, gaps, summary, meta, top), encoding="utf-8")
    return json_path, csv_path, md_path


def render_markdown(rows, gaps, summary, meta, top):
    win = meta["data_window"]
    lines = [
        "# H-1B sponsor coverage gap list",
        "",
        f"_Generated {meta['generated_at']} · `{REPORT_VERSION}` · read-only_",
        "",
        "## What this measures",
        "",
        "Every employer in `sponsorship_signals` (USCIS H-1B Employer Data Hub) "
        "joined against `employers`, `career_sources`, `company_ats_endpoints` "
        "and live `job_postings`. The demand signal is the employer's OWN "
        "petition count; the coverage signal is ADR-006's — only `validated` "
        "and `empty` career sources count, and a posting count is the number we "
        "actually hold today.",
        "",
        f"**Data vintage: {win.get('label', 'unknown')}.** "
        f"Source: {meta['source']}.",
    ]
    if win.get("service_claim_mismatch"):
        lines += [
            "",
            f"> **The window is {win.get('years')} fiscal year(s), not "
            f"{win.get('service_claim_years')}.** `sponsorship_service."
            "DATA_WINDOW_YEARS` is 5 and every headline it renders says \"in the "
            "last 5 years\". Until that is fixed the product over-states the "
            "window for these rows; every document carries "
            "`data_window.service_claim_mismatch: true` so the mismatch is "
            "queryable, not folklore.",
        ]
    lines += [
        "",
        "## Totals",
        "",
        f"- sponsors measured: **{meta['totals']['sponsors']:,}** "
        f"(of {meta['totals']['sponsors_all']:,} in the collection; "
        f"`--min-approvals {meta['totals']['min_approvals']}`)",
        f"- their FY approvals: **{meta['totals']['approvals']:,}**",
        f"- active postings we hold for them today: "
        f"**{meta['totals']['active_postings']:,}** "
        f"({meta['totals']['core_tech_postings']:,} core-tech)",
        "",
        "## Buckets",
        "",
        "| bucket | employers | approvals | software-weighted | postings today | "
        "est. postings if covered |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for bucket in BUCKET_ORDER:
        b = summary["buckets"][bucket]
        est = "—" if bucket == BUCKET_COVERED else f"~{b['estimated_postings_if_covered']:,}"
        lines.append(
            f"| `{bucket}` | {b['employers']:,} | {b['approvals']:,} | "
            f"{b['software_weighted_approvals']:,.0f} | "
            f"{b['active_postings_today']:,} | {est} |")
    lines += [
        "",
        "`est. postings if covered` is a MODEL, not a measurement: the median "
        "active postings held by sponsors we already have an `employers` row "
        "for, in the same approvals band, zeros included. It answers \"is this "
        "bucket worth a week?\" and nothing finer.",
        "",
        f"## The ranked gap list — {len(gaps):,} uncovered sponsors by "
        "`approvals x software_likelihood x conversion`",
        "",
        f"The first {min(100, len(gaps))} are below. "
        f"`h1b-sponsor-gaps.csv` and `.json` carry the top "
        f"{min(top, len(gaps)):,}.",
        "",
        "| # | employer | approvals | sw | conv | score | bucket | NAICS | where | postings |",
        "|---:|---|---:|---:|---:|---:|---|---|---|---:|",
    ]
    for row in gaps[:min(top, 100)]:
        where = ", ".join(x for x in [row["city"].title(), row["state"]] if x)
        lines.append(
            f"| {row['rank']} | {row['employer_display'][:42]} | {row['approvals']:,} | "
            f"{row['software_likelihood']:.2f} | {row['conversion']:.2f} | "
            f"{row['priority_score']:,.0f} | "
            f"`{row['bucket']}` | {row['naics_code']} | {where} | "
            f"{row['active_postings']} |")
    if len(gaps) > 100:
        lines.append(f"| … | _{min(top, len(gaps)) - 100:,} more rows in "
                     "`h1b-sponsor-gaps.csv` / `.json`_ | | | | | | | | |")
    lines += [
        "",
        "## The inverse: employers we carry who sponsor nobody",
        "",
        f"{meta['census_absent']['employers']:,} employers we already hold "
        f"**{meta['census_absent']['active_postings']:,} active postings** for "
        f"({meta['census_absent']['core_tech_postings']:,} core-tech) filed **no "
        f"H-1B petition at all** in this window. The USCIS Data Hub lists every "
        f"employer that filed, so this is a measurement, not an absence of data — "
        f"`sponsorship_signals` carries an explicit `census_absent` row for each so "
        f"the badge can say it.",
        "",
        "| employer | active postings | core-tech |",
        "|---|---:|---:|",
    ] + [
        f"| {c['employer_display'][:48]} | {c['active_postings']:,} | "
        f"{c['core_tech_postings']:,} |"
        for c in meta["census_absent"]["top"][:15]
    ] + [
        "",
        "## How to use this",
        "",
        "`h1b-sponsor-gaps.csv` is the work queue, already ordered. For each "
        "row: `employer_norm` is the `sponsorship_signals` key, `domain` is "
        "populated ONLY when the `employers` registry already holds one "
        "(`domain_source` says which kind of evidence) — an empty `domain` "
        "means we do not know it, never that it does not exist. Do not derive "
        "one from the name; that is the mistake ADR-006 exists to stop.",
        "",
        "Work the buckets in this order, which is cheapest-proof-first. The "
        "first three are NOT discovery work and must not be sent to a discovery "
        "pass:",
        "",
        f"1. `{BUCKET_UNDER_READ}` — we have the board and read part of it. "
        "Eightfold's `page_size=20` against a 10-row API cost Microsoft 1,149 "
        "postings, Qualcomm 737 and Lockheed ~1,480; no amount of discovery "
        "would have found them.",
        f"2. `{BUCKET_BOARD_HAS_JOBS}` — the board is proven ours and last "
        "published jobs we hold none of. Re-read the endpoint.",
        f"3. `{BUCKET_VALIDATED_ZERO}` — proven board, and it published nothing "
        "either. Believe it, or check the endpoint address (CLAUDE.md: asking "
        "the wrong URL reads as a broken board).",
        f"4. `{BUCKET_UNVALIDATED}` — the board is known and unproven. This is "
        "`verify_career_sources.py --mode backlink`, not a new network sweep.",
        f"5. `{BUCKET_NO_SOURCE}` — the employer is known, the board is not. "
        "Discovery, one employer at a time.",
        f"6. `{BUCKET_NO_EMPLOYER}` — we have never heard of them. Cheapest to "
        "add a registry row, longest path to a posting.",
        "",
        "**Read `conversion` before you spend a day on a row.** Approvals are "
        "demand; `conversion` is how much of it could become inventory. The "
        "client-site staffing cohort (Infosys, TCS, Cognizant, Wipro, HCLTech, "
        "Capgemini, LTIMindtree, Tech Mahindra, Mphasis — 14,421 approvals "
        "between them, more than Apple, Microsoft and Google combined) hires "
        "against client requisitions that never reach a careers site. "
        "LTIMindtree is the proof: its board is reachable, owned and fully "
        "readable, and it publishes 54 jobs, none of them in the US.",
        "",
        "Run ONE network pass at a time (CLAUDE.md: a content pass beside two "
        "others turned 830 sources `blocked` in one sweep).",
        "",
    ]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="H-1B sponsor coverage gap list (read-only)")
    parser.add_argument("--out", default=str(ROOT / "docs" / "coverage"),
                        help="Output directory (default docs/coverage)")
    parser.add_argument("--top", type=int, default=500,
                        help="Rows to emit in the artifacts (default 500)")
    parser.add_argument("--min-approvals", type=int, default=5,
                        help="Ignore sponsors below this many approvals (default 5)")
    parser.add_argument("--include-covered", action="store_true",
                        help="Keep already-covered employers in the ranked list")
    args = parser.parse_args()

    db = _db()
    sponsors = load_sponsors(db, args.min_approvals)
    if not sponsors:
        logger.error("sponsorship_signals is empty — run scripts/ingest_sponsorship.py first")
        return 1
    sponsors_all = db.sponsorship_signals.count_documents({"rollup.is_alias": False})
    census_absent = load_census_absent(db)
    window = (sponsors[0].get("data_window") or {})

    by_id, by_name, by_first_token = load_employers(db)
    alias_keys = load_alias_keys(db)
    sources_by_employer = load_career_sources(db)
    ats_by_member = load_ats_endpoints(db)
    postings_by_canonical, postings_by_board, postings_by_name = load_posting_counts(db)

    t0 = time.time()
    rows = build_rows(sponsors, by_id, by_name, by_first_token, alias_keys,
                      sources_by_employer, ats_by_member,
                      postings_by_canonical, postings_by_board, postings_by_name)
    logger.info("joined %d sponsors in %.1fs", len(rows), time.time() - t0)

    summary = summarize(rows)
    gaps = [r for r in rows if args.include_covered or r["bucket"] != BUCKET_COVERED]
    gaps.sort(key=lambda r: (-r["priority_score"], -r["approvals"], r["employer_norm"]))
    for i, row in enumerate(gaps, 1):
        row["rank"] = i

    meta = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "data_window": window,
        "source": " + ".join(s.get("label", "") for s in
                             ((sponsors[0].get("provenance") or {}).get("sources") or
                              [{"label": "USCIS H-1B Employer Data Hub"}])),
        "census_absent": {
            "employers": len(census_absent),
            "active_postings": sum(int((c.get("census") or {}).get(
                "inventory_active_postings") or 0) for c in census_absent),
            "core_tech_postings": sum(int((c.get("census") or {}).get(
                "inventory_core_tech_postings") or 0) for c in census_absent),
            "note": "employers we ALREADY carry postings for that filed no H-1B "
                    "petition in the window — the inverse of this list",
            "top": [{"employer_display": c.get("employer_display"),
                     "employer_norm": c.get("employer_norm"),
                     "active_postings": (c.get("census") or {}).get(
                         "inventory_active_postings"),
                     "core_tech_postings": (c.get("census") or {}).get(
                         "inventory_core_tech_postings")}
                    for c in census_absent[:50]],
        },
        "totals": {
            "sponsors": len(rows),
            "sponsors_all": sponsors_all,
            "min_approvals": args.min_approvals,
            "approvals": sum(r["approvals"] for r in rows),
            "active_postings": sum(r["active_postings"] for r in rows),
            "core_tech_postings": sum(r["core_tech_postings"] for r in rows),
        },
    }
    paths = write_outputs(Path(args.out), rows, gaps, summary, meta, args.top)
    print(f"\n  inverse finding: {meta['census_absent']['employers']:,} employers we "
          f"already carry {meta['census_absent']['active_postings']:,} active postings "
          f"for filed NOTHING in the window")

    print("\n== Sponsor coverage ==")
    for bucket in BUCKET_ORDER:
        b = summary["buckets"][bucket]
        print(f"  {bucket:28s} employers={b['employers']:6,d}  approvals={b['approvals']:8,d}  "
              f"postings_today={b['active_postings_today']:7,d}  "
              f"est_if_covered={b['estimated_postings_if_covered']:7,d}")
    print(f"\n  ranked uncovered rows: {len(gaps):,} (emitting top {min(args.top, len(gaps)):,})")
    print("\n== Top 25 ==")
    for row in gaps[:25]:
        print(f"  {row['rank']:4d} {row['priority_score']:9,.0f}  {row['approvals']:6,d} appr  "
              f"sw={row['software_likelihood']:.2f}  {row['employer_display'][:40]:40s} "
              f"{row['bucket']}")
    for path in paths:
        print(f"\nwrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
