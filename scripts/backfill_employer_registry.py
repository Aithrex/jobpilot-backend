#!/usr/bin/env python3
"""Give every employer we know about ONE id, from the five registries that disagree.

READ-ONLY by default. `--apply` writes `employers` and `career_sources`; it
never touches `companies`, `company_ats_endpoints` or `job_postings`, so the
crawler keeps running against exactly the rows it ran against before and the
whole migration is reversible by dropping two collections.

What it joins
-------------
1. `scripts/data/tech_companies.py`    curated seed, (name, domain, category)
2. `services/official_career_registry` official career URLs + cohorts
3. `workday_tenant_catalog` / `career_pages_catalog` / `mnc_catalog`
4. `companies`                          the EDGAR/state-registry universe
5. `company_ats_endpoints`              the boards we actually crawl
6. `job_postings`                       who we have actually ingested under

Order matters and is evidence-ranked, strongest first: a curated identity
override beats a domain-bearing seed row, which beats a catalog row, which
beats a `companies` row, which beats a name observed on a posting. A later,
weaker source can ADD an alias or a category to an employer; it can never
rename one or change its canonical domain (`employer_registry.upsert_employer`
enforces that with `identity_confidence`).

What it refuses to do
---------------------
Merge on a name. `stripe` -> "STRIPES SOLUTIONS LLC" is a live example of why.
Any two candidates that match on name alone are written to
`employer_merge_reviews` and left as two employers, which is the honest
outcome: two rows that might be one is a smaller error than one row that is
definitely two.

Career sources are seeded at the state the OFFLINE evidence supports, which
for most rows is `candidate`. The live ownership pass
(`scripts/verify_career_sources.py`) is what promotes them. Coverage
therefore DROPS when this lands and then climbs — that is the number becoming
true, not the crawl regressing.

Usage
-----
    PYTHONPATH=shared/python python scripts/backfill_employer_registry.py
    PYTHONPATH=shared/python python scripts/backfill_employer_registry.py --apply
    PYTHONPATH=shared/python python scripts/backfill_employer_registry.py \
        --apply --json out.json
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "shared" / "python"))
sys.path.insert(0, str(ROOT))


def _db():
    env_path = ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    from utils.db_connect import DBConnect
    return DBConnect().get_db()


from services import career_source_service as css        # noqa: E402
from services import employer_registry as er             # noqa: E402
from services import official_career_registry as ocr     # noqa: E402
from services.career_source_service import evidence      # noqa: E402


def _now() -> datetime:
    return datetime.now(timezone.utc)


_DOMAIN_BY_NAME: Optional[Dict[str, str]] = None


def domain_for_name(name: Any) -> str:
    """The curated domain for an employer name, from every curated source.

    Without this, an employer discovered from an ATS endpoint's
    `display_name` ("1Password") minted a NAME-derived id while the curated
    seed's row for the same employer ("1Password", `1password.com`) minted a
    DOMAIN-derived one — two employers, and the one the endpoint pointed at
    was the one with no domain. That is not a merge on a name: a curated
    registry naming both the employer and its domain is exactly the
    documentary evidence `merge_decision` accepts, and attaching it BEFORE
    the id is minted means the two rows are never created in the first place.
    """
    global _DOMAIN_BY_NAME
    if _DOMAIN_BY_NAME is None:
        out: Dict[str, str] = {}
        for row in ocr.OFFICIAL_CAREER_URLS:
            k = er.name_key(row["display_name"])
            if k:
                out.setdefault(k, row["domain"])
        try:
            from scripts.data.tech_companies import TECH_COMPANIES
        except Exception:
            sys.path.insert(0, str(ROOT / "scripts"))
            from data.tech_companies import TECH_COMPANIES  # type: ignore
        for disp, domain, _cat in TECH_COMPANIES:
            k = er.name_key(disp)
            if k:
                out.setdefault(k, domain)
        for row in er.IDENTITY_OVERRIDES:
            for alias in [row["display_name"], *(row.get("aliases") or ()),
                          *(row.get("legal_names") or ())]:
                k = er.name_key(alias)
                if k and row.get("domain"):
                    out.setdefault(k, row["domain"])
        # A name a curated override blacklists must never inherit a domain
        # from a lookalike.
        for bad in er.wrong_name_keys():
            out.pop(bad, None)
        _DOMAIN_BY_NAME = out
    return _DOMAIN_BY_NAME.get(er.name_key(name), "")


# ---------------------------------------------------------------------------
# Candidate collection, strongest evidence first
# ---------------------------------------------------------------------------
class Builder:
    """Accumulates employer candidates and adjudicates every collision."""

    def __init__(self) -> None:
        # employer_id -> record
        self.employers: Dict[str, Dict[str, Any]] = {}
        # name_key -> [employer_id] — the collision index, used only to DETECT
        # ambiguity, never to merge on.
        self.by_name: Dict[str, List[str]] = collections.defaultdict(list)
        self.refs: Dict[str, Dict[str, set]] = collections.defaultdict(
            lambda: collections.defaultdict(set))
        self.quarantined: List[Tuple[Dict[str, Any], Dict[str, Any], str]] = []
        self.stats: collections.Counter = collections.Counter()

    def usable(self, name: Any, domain: Any = None) -> bool:
        """Can this candidate be given an id at all?

        A "name" that normalises away entirely — "L.L.C.", "3", "- -" — is not
        an employer name, and minting `emp_<sha1 of "">` for every one of them
        would collapse hundreds of unrelated rows into a single junk employer.
        Live `company_ats_endpoints` contains several.
        """
        return bool(er.normalize_domain(domain) or er.name_key(name))

    def add(self, record: Dict[str, Any], *, refs: Sequence[Tuple[str, str]] = ()) -> str:
        eid = record["employer_id"]
        existing = self.employers.get(eid)
        if existing is None:
            # Same NAME, different id, and neither has a domain? That is the
            # Forward / Beyond / Public collision. Keep both, file a review.
            nk = record["name_key"]
            if nk and record["id_basis"] == "name":
                for other_id in self.by_name.get(nk, []):
                    other = self.employers[other_id]
                    verdict, why = er.merge_decision(record, other)
                    if verdict == er.QUARANTINE:
                        self.quarantined.append((record, other, why))
                        self.stats["quarantined"] += 1
            self.employers[eid] = record
            self.by_name[record["name_key"]].append(eid)
            self.stats["employers"] += 1
        else:
            self._absorb(existing, record)
            self.stats["merged_into_existing"] += 1
        for kind, value in refs:
            if value:
                self.refs[eid][kind].add(str(value))
        return eid

    @staticmethod
    def _absorb(target: Dict[str, Any], incoming: Dict[str, Any]) -> None:
        for field in ("legal_names", "aliases", "brands", "verified_domains",
                      "employer_categories", "cohorts", "countries"):
            have = {er.name_key(v): v for v in target.get(field) or []}
            for v in incoming.get(field) or []:
                have.setdefault(er.name_key(v), v)
            merged = [v for v in have.values() if v]
            if field == "cohorts":
                real = [c for c in merged if c != er.COHORT_UNKNOWN]
                merged = real or [er.COHORT_UNKNOWN]
            target[field] = merged[:40]
        target["evidence"] = (target.get("evidence") or []) + \
                             [e for e in (incoming.get("evidence") or [])][:8]
        target["evidence"] = target["evidence"][:40]
        if float(incoming.get("identity_confidence") or 0) > \
                float(target.get("identity_confidence") or 0):
            for field in ("display_name", "name_key", "id_basis",
                          "identity_confidence", "canonical_domain"):
                if incoming.get(field) is not None:
                    target[field] = incoming[field]

    # -- sources ------------------------------------------------------------
    def from_identity_overrides(self) -> None:
        """The documented conflicts. Highest authority in the file."""
        for row in er.IDENTITY_OVERRIDES:
            self.add(er.build_employer(
                display_name=row["display_name"],
                canonical_domain=row.get("domain"),
                legal_names=row.get("legal_names") or (),
                aliases=row.get("aliases") or (),
                brands=row.get("brands") or (),
                cohorts=row.get("cohorts") or (),
                employer_categories=row.get("categories") or (),
                evidence=[{"kind": "identity_override", "source": "employer_registry",
                           "value": row["display_name"]}],
            ))
        # A brand is not a separate employer. Point it at its parent so the
        # feed attributes Slack's postings to Salesforce without losing the
        # brand name on the card.
        for row in er.IDENTITY_OVERRIDES:
            parent_id = er.employer_id_for(domain=row.get("domain"),
                                           name=row["display_name"])
            for brand in row.get("brands") or ():
                bid = er.employer_id_for(name=brand)
                if bid in self.employers:
                    continue
                self.add(er.build_employer(
                    display_name=brand,
                    operating_status=er.STATUS_PRODUCT_OR_BRAND,
                    parent_employer_id=parent_id,
                    cohorts=row.get("cohorts") or (),
                    employer_categories=row.get("categories") or (),
                    evidence=[{"kind": "brand_of", "source": "identity_override",
                               "value": row["display_name"]}],
                ))
                self.stats["brands"] += 1

    def from_official_registry(self) -> None:
        for row in ocr.OFFICIAL_CAREER_URLS:
            self.add(er.build_employer(
                display_name=row["display_name"],
                canonical_domain=row["domain"],
                cohorts=row.get("cohorts") or (),
                employer_categories=row.get("categories") or (),
                evidence=[{"kind": "official_career_url", "source": "registry",
                           "value": row["career_landing_url"]}],
            ))
            self.stats["from_official_registry"] += 1

    def from_curated_seed(self) -> None:
        try:
            from scripts.data.tech_companies import TECH_COMPANIES
        except Exception:
            sys.path.insert(0, str(ROOT / "scripts"))
            from data.tech_companies import TECH_COMPANIES  # type: ignore
        for name, domain, category in TECH_COMPANIES:
            self.add(er.build_employer(
                display_name=name, canonical_domain=domain,
                employer_categories=[category],
                evidence=[{"kind": "curated_seed", "source": "tech_companies.py",
                           "value": category}],
            ))
            self.stats["from_curated_seed"] += 1

    def from_catalogs(self) -> None:
        """The three human-validated Python catalogs.

        These rows carry no domain, so they mint name-derived ids — and that
        is exactly right: a catalog row is evidence that an employer exists
        and runs this tenant, not evidence of which domain they own. The live
        verification pass promotes them with `rekey_to_domain`.
        """
        loaded: List[Tuple[str, str, str, str, str]] = []  # name, vendor, tenant, industry, registry
        try:
            from services.workday_tenant_catalog import WORKDAY_TENANT_CATALOG
            for r in WORKDAY_TENANT_CATALOG:
                loaded.append((r["display_name"], "workday", r.get("tenant") or "",
                               r.get("industry") or "", "workday_tenant_catalog"))
        except Exception as exc:
            print(f"  ! workday catalog unavailable: {exc}", file=sys.stderr)
        try:
            from services.career_pages_catalog import CAREER_PAGES_CATALOG
            for r in CAREER_PAGES_CATALOG:
                loaded.append((r["display_name"], r.get("ats") or "", r.get("slug") or "",
                               r.get("industry") or "", "career_pages_catalog"))
        except Exception as exc:
            print(f"  ! career catalog unavailable: {exc}", file=sys.stderr)
        try:
            from services.mnc_catalog import MNC_CATALOG
            for r in MNC_CATALOG:
                loaded.append((r["display_name"], r.get("ats") or "", r.get("slug") or "",
                               r.get("sector") or "", "mnc_catalog"))
        except Exception:
            pass

        for name, vendor, tenant, industry, registry in loaded:
            if not name:
                continue
            hit = ocr.lookup(name=name)
            eid = self.add(er.build_employer(
                display_name=name,
                canonical_domain=(hit or {}).get("domain") or domain_for_name(name),
                employer_categories=[industry] if industry else (),
                cohorts=(hit or {}).get("cohorts") or (),
                evidence=[{"kind": "catalog_row", "source": registry,
                           "value": f"{vendor}/{tenant}"}],
            ), refs=[("tenant", f"{vendor}:{tenant}"), ("catalog", registry)])
            self.stats["from_catalogs"] += 1
            _ = eid

    def from_companies(self, db, *, tech_only_ids: Optional[set] = None) -> None:
        """`companies` rows worth an employer id.

        Not all 82,815: the great majority are state-registry shells with no
        SIC, no domain and no postings, and minting an employer for each would
        bury every real one. The filter is "has evidence of being an employer
        we care about": in the tech denominator, or carrying a domain, or
        having an endpoint, or having postings.
        """
        q: Dict[str, Any] = {"$or": [
            {"signals.software": True},
            {"signals.tech_adjacent": True},
            {"domain": {"$nin": [None, ""]}},
            {"endpoints_found": {"$gt": 0}},
        ]}
        if tech_only_ids:
            q = {"$or": [q, {"company_id": {"$in": sorted(tech_only_ids)}}]}
        cur = db.companies.find(q, {
            "_id": 0, "company_id": 1, "name": 1, "domain": 1, "ticker": 1,
            "cik": 1, "sic": 1, "sic_description": 1, "state": 1, "tech": 1,
            "signals": 1})
        for row in cur:
            name = row.get("name") or ""
            if not self.usable(name, row.get("domain")):
                self.stats["company_name_unusable"] += 1
                continue
            cohorts: List[str] = []
            if row.get("cik") or row.get("ticker"):
                cohorts.append(er.COHORT_PUBLIC_TECH)
            cats = list((row.get("tech") or {}).get("tech_categories") or [])
            self.add(er.build_employer(
                display_name=name,
                canonical_domain=row.get("domain"),
                cohorts=cohorts,
                employer_categories=cats,
                countries=["US"] if row.get("state") else (),
                evidence=[{"kind": "company_registry", "source": "companies",
                           "value": row.get("sic_description") or row.get("company_id")}],
            ), refs=[("company", row.get("company_id"))])
            self.stats["from_companies"] += 1

    def from_endpoints(self, db) -> None:
        for row in db.company_ats_endpoints.find({}, {"_id": 0}):
            name = row.get("display_name") or ""
            vendor = row.get("vendor") or ""
            tenant = row.get("tenant") or ""
            if not self.usable(name):
                self.stats["endpoint_name_unusable"] += 1
                continue
            # A `display_name` that a curated override marks as a wrong owner
            # must not mint an employer at all — that is how "NUTMEG CONTAINER
            # CORPORATION" became JPMorgan's tenant owner in the first place.
            if er.name_key(name) in er.wrong_name_keys():
                self.stats["endpoint_name_blacklisted"] += 1
                continue
            hit = ocr.lookup(name=name)
            self.add(er.build_employer(
                display_name=name,
                canonical_domain=(hit or {}).get("domain") or domain_for_name(name),
                evidence=[{"kind": "ats_endpoint", "source": vendor,
                           "value": row.get("member_key") or tenant}],
            ), refs=[("tenant", f"{vendor}:{tenant}"),
                     ("member", row.get("member_key")),
                     ("company", row.get("company_id"))])
            self.stats["from_endpoints"] += 1

    def from_postings(self, db, *, limit: int = 20000) -> None:
        """Employers we have actually ingested under, resolved per tenant.

        `employer_identity.resolve_employer_name` already knows how to pick
        the right name for a tenant carrying several; reusing it here means
        the registry inherits that work rather than re-deciding it.
        """
        from services import employer_identity as ei
        rows = list(db.job_postings.aggregate([
            {"$match": {"status": "active"}},
            {"$group": {"_id": {"ats": "$ats", "tenant": "$tenant",
                                "employer_name": "$employer_name"},
                        "n": {"$sum": 1},
                        "legacy": {"$first": "$employer_id"}}},
            {"$sort": {"n": -1}},
            {"$limit": int(limit)},
        ], allowDiskUse=True))
        obs = [{"ats": r["_id"].get("ats"), "tenant": r["_id"].get("tenant"),
                "employer_name": r["_id"].get("employer_name")} for r in rows]
        rmap = ei.build_resolution_map(obs)
        for r in rows:
            key = r["_id"]
            vendor, tenant = key.get("ats") or "", key.get("tenant") or ""
            res = rmap.get((ei._slug_key(vendor), ei._slug_key(tenant))) or {}
            name = res.get("name") or key.get("employer_name") or ""
            if er.name_key(name) in er.wrong_name_keys():
                self.stats["posting_name_blacklisted"] += 1
                continue
            if not self.usable(name):
                self.stats["posting_name_unusable"] += 1
                continue
            if res.get("source") == "unresolved_conflict":
                self.stats["posting_tenant_unresolved"] += 1
            hit = ocr.lookup(name=name)
            self.add(er.build_employer(
                display_name=name,
                canonical_domain=(hit or {}).get("domain") or domain_for_name(name),
                aliases=[key.get("employer_name")] if key.get("employer_name") else (),
                evidence=[{"kind": "ingested_postings", "source": vendor,
                           "value": f"{r['n']} active on {vendor}/{tenant}"}],
            ), refs=[("tenant", f"{vendor}:{tenant}"),
                     ("legacy_employer_id", r.get("legacy"))])
            self.stats["from_postings"] += 1


# ---------------------------------------------------------------------------
# Career sources from what the endpoint rows can honestly support
# ---------------------------------------------------------------------------
def seed_career_sources(db, builder: Builder) -> List[Dict[str, Any]]:
    """One `career_sources` row per endpoint, at its EVIDENCED state.

    An endpoint whose only provenance is `slug-probe` gets
    `SIGNAL_SLUG_SIMILARITY` — recorded, and worth nothing. An endpoint that
    came from a curated catalog gets `SIGNAL_CURATED_ROW`, which is one class
    and still not enough on its own. Almost everything therefore lands as
    `candidate` or `content_validated`, and that is the correct starting
    point: the 43% figure was measuring "the endpoint answered", and this is
    measuring "we can prove whose it is".
    """
    # tenant key -> employer_id, from every reference the builder collected.
    tenant_index: Dict[str, str] = {}
    member_index: Dict[str, str] = {}
    for eid, kinds in builder.refs.items():
        for t in kinds.get("tenant", ()):
            tenant_index.setdefault(t, eid)
        for m in kinds.get("member", ()):
            member_index.setdefault(m, eid)

    catalog_tenants = set()
    for eid, kinds in builder.refs.items():
        if kinds.get("catalog"):
            catalog_tenants |= set(kinds.get("tenant", ()))

    out: List[Dict[str, Any]] = []
    for row in db.company_ats_endpoints.find({}, {"_id": 0}):
        vendor = row.get("vendor") or ""
        tenant = row.get("tenant") or ""
        member = row.get("member_key") or ""
        tkey = f"{vendor}:{tenant}"
        eid = member_index.get(member) or tenant_index.get(tkey)
        if not eid:
            continue
        sources = set(row.get("sources") or [])
        own_ev: List[Dict[str, Any]] = []
        method = css.METHOD_SLUG_PROBE
        if "catalog" in sources or tkey in catalog_tenants:
            own_ev.append(evidence(css.SIGNAL_CURATED_ROW, source="python_catalog",
                                   value=row.get("display_name") or tkey))
            method = css.METHOD_CURATED_CATALOG
        if "discovery" in sources:
            # The fingerprint path DID read the employer's own site — but the
            # domain confirmation it used (`_page_mentions`) was never
            # recorded, so we cannot replay it. Record the method and let the
            # live pass supply the evidence.
            method = css.METHOD_FINGERPRINT
        if "slug-probe" in sources:
            own_ev.append(evidence(css.SIGNAL_SLUG_SIMILARITY, source="slug-probe",
                                   value="historic slug probe; never counted"))
        if str(row.get("status")) in ("validated", "empty"):
            own_ev.append(evidence(css.SIGNAL_ENDPOINT_RESPONDS, source=vendor,
                                   value=f"{row.get('job_count_last') or 0} postings"))

        content_ev: List[Dict[str, Any]] = []
        job_count = int(row.get("job_count_last") or 0)
        if str(row.get("status")) == "validated" and job_count > 0:
            content_ev.append(evidence("content_fetch", source=vendor,
                                       value=f"{job_count} postings",
                                       at=row.get("validated_at")))
        elif str(row.get("status")) == "empty":
            content_ev.append(evidence("content_fetch", source=vendor,
                                       value="board answered with no postings"))

        fail = css.FAIL_NONE
        detail = str(row.get("last_check_detail") or "")[:400]
        st = str(row.get("status") or "")
        if st == "blocked":
            fail = css.FAIL_HTTP_403
        elif st == "dead":
            fail = css.FAIL_HTTP_404
        elif st == "empty":
            fail = css.FAIL_NO_POSTINGS

        out.append(css.build_source(
            employer_id=eid, ats_vendor=vendor, tenant=tenant,
            discovery_method=method,
            ownership_evidence=own_ev,
            content_validation_evidence=content_ev,
            member_key=member,
            job_count_last=job_count,
            failure_code=fail, failure_detail=detail,
            first_seen_at=row.get("created_at"),
        ))
    return out


# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="write `employers` and `career_sources` (default: report only)")
    ap.add_argument("--json", default=None, help="write the full summary here")
    ap.add_argument("--posting-limit", type=int, default=20000)
    ap.add_argument("--skip-postings", action="store_true")
    args = ap.parse_args()

    db = _db()
    print(f"mode: {'APPLY' if args.apply else 'dry-run'}")
    b = Builder()

    print("  identity overrides ...")
    b.from_identity_overrides()
    print("  official career registry ...")
    b.from_official_registry()
    print("  curated tech seed ...")
    b.from_curated_seed()
    print("  python catalogs ...")
    b.from_catalogs()
    print("  companies ...")
    b.from_companies(db)
    print("  ats endpoints ...")
    b.from_endpoints(db)
    if not args.skip_postings:
        print("  active postings ...")
        b.from_postings(db, limit=args.posting_limit)

    print(f"\nemployers built: {len(b.employers):,}")
    basis = collections.Counter(r["id_basis"] for r in b.employers.values())
    for k, v in basis.most_common():
        print(f"  id_basis {k:10s} {v:>7,}")
    print(f"  quarantined name collisions: {len(b.quarantined):,}")

    sources = seed_career_sources(db, b)
    by_state = collections.Counter(s["state"] for s in sources)
    print(f"\ncareer sources built: {len(sources):,}")
    for k, v in by_state.most_common():
        print(f"  {k:20s} {v:>7,}")
    covered = sum(by_state.get(s, 0) for s in css.COVERED_STATES)
    print(f"  owner-verified + content-validated: {covered:,} "
          f"({(covered / len(sources) * 100) if sources else 0:.1f}%)")
    print("  (this is the honest starting point; the live ownership pass "
          "promotes candidates)")

    summary = {
        "generated_at": _now().isoformat(),
        "registry_version": er.REGISTRY_VERSION,
        "career_source_version": css.CAREER_SOURCE_VERSION,
        "employers": len(b.employers),
        "employers_by_id_basis": dict(basis),
        "career_sources": len(sources),
        "career_sources_by_state": dict(by_state),
        "quarantined": len(b.quarantined),
        "stats": dict(b.stats),
    }

    if args.apply:
        print("\nwriting ...")
        er.ensure_indexes()
        css.ensure_indexes()
        def _with_refs():
            for eid, rec in b.employers.items():
                rec = dict(rec)
                rec["registry_refs"] = {k: sorted(v)[:200]
                                        for k, v in b.refs.get(eid, {}).items()}
                yield rec

        emp_stats = er.bulk_upsert_employers(_with_refs())
        print(f"    {emp_stats['seen']:,} employers "
              f"({emp_stats['written']:,} inserted or changed)")
        for left, right, why in b.quarantined:
            er.quarantine_merge(left, right, why)
        print(f"    {len(b.quarantined):,} merge reviews filed")
        src_stats = css.bulk_upsert_sources(sources)
        print(f"    {src_stats['seen']:,} career sources "
              f"({src_stats['written']:,} inserted or changed)")
        summary["written"] = {"employers": emp_stats, "career_sources": src_stats,
                              "merge_reviews": len(b.quarantined)}
    else:
        print("\ndry-run: nothing written (pass --apply)")

    if args.json:
        Path(args.json).write_text(json.dumps(summary, indent=2, default=str),
                                   encoding="utf-8")
        print(f"wrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
