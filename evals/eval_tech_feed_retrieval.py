"""Retrieval eval for `/api/jobs/tech-feed/search`.

Referenced by `docs/coverage/employer-coverage-definition.md` and missing
until now, which meant the retrieval path had contract tests but no MEASURED
quality number — a filter could quietly stop matching a whole specialty and
nothing would say so.

What it measures, and why these seven dimensions
------------------------------------------------
Each is a distinct way for retrieval to be wrong, and each maps to a real
complaint:

    source            "I only ever see Workday jobs" — a provider whose rows
                      are systematically filtered out looks like a coverage
                      gap and is a retrieval bug.
    occupation family the 14-family tree. A family with poor recall is a
                      discipline that cannot use the product.
    specialty         the 102-specialty tree, where `tech-tax-1.1.0` added 20.
                      Family recall can be perfect while a specialty filter
                      returns nothing.
    employer cohort   MNC / public tech / private growth / niche startup.
                      Skew here is what makes a feed feel like a Workday
                      retail board.
    ambiguous title   "Member of Technical Staff", "Technology Analyst".
                      These must be RETRIEVABLE without being asserted — a
                      filter must not silently drop what it cannot classify.
    geography         a country/city filter must not drop remote roles that
                      are open to that geography.
    seniority         unknown seniority must never be excluded. 43.3% of the
                      live inventory states none.

Precision and recall are reported PER STRATUM, not only in aggregate: an
aggregate of 0.95 across seven dimensions can hide one that returns nothing.

Run:
    PYTHONPATH=shared/python python -m pytest evals/test_tech_feed_retrieval.py -q -s
"""
from __future__ import annotations

import collections
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT / "shared" / "python"), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)

#: `(job_key, title, description, company, cohort, source, ats, tenant,
#:   location, days_old, expected_families, expected_specialties)`
#:
#: Every row is a posting shape the live inventory actually contains. Company
#: names are real employers because the cohort strata are about employers; the
#: occupation classifier still receives no employer input.
CORPUS: Tuple[Dict[str, Any], ...] = (
    # ---- software engineering ---------------------------------------------
    dict(key="swe-backend", title="Senior Backend Engineer",
         desc="Python, Go, PostgreSQL, distributed services. 5+ years.",
         company="Stripe", cohort="private_growth", source="career-pages",
         ats="greenhouse", tenant="stripe", loc="San Francisco, CA, USA",
         days=1, families=["software_engineering"], specialties=["backend"]),
    dict(key="swe-frontend", title="Frontend Engineer",
         desc="React, TypeScript, Next.js and design systems.",
         company="Figma", cohort="private_growth", source="career-pages",
         ats="greenhouse", tenant="figma", loc="New York, NY, USA",
         days=2, families=["software_engineering"], specialties=["frontend"]),
    dict(key="swe-mobile", title="Staff iOS Engineer",
         desc="Swift, UIKit, SwiftUI at scale.", company="Snap",
         cohort="public_tech", source="career-pages", ats="lever",
         tenant="snap", loc="Los Angeles, CA, USA", days=3,
         families=["software_engineering"], specialties=["mobile"]),
    dict(key="swe-embedded", title="Embedded Software Engineer",
         desc="Embedded C, RTOS, device drivers and board bring-up.",
         company="Garmin", cohort="public_tech", source="workday",
         ats="workday", tenant="garmin", loc="Olathe, KS, USA", days=5,
         families=["software_engineering"], specialties=["embedded"]),
    dict(key="swe-mainframe", title="COBOL Developer",
         desc="Mainframe COBOL, JCL and CICS on z/OS for core banking.",
         company="JPMorgan Chase", cohort="mnc", source="oracle",
         ats="oracle-orc", tenant="jpmc", loc="Columbus, OH, USA", days=6,
         families=["software_engineering"], specialties=["enterprise_mainframe"]),
    dict(key="swe-quant", title="Low Latency C++ Engineer",
         desc="Ultra low latency trading systems, kernel bypass, market data.",
         company="Jane Street", cohort="private_growth", source="career-pages",
         ats="greenhouse", tenant="janestreet", loc="New York, NY, USA",
         days=4, families=["software_engineering"],
         specialties=["quantitative_low_latency"]),

    # ---- cloud / platform --------------------------------------------------
    dict(key="cloud-sre", title="Site Reliability Engineer",
         desc="Kubernetes, Terraform, on-call, SLOs and error budgets.",
         company="Cloudflare", cohort="public_tech", source="career-pages",
         ats="greenhouse", tenant="cloudflare", loc="Austin, TX, USA", days=2,
         families=["cloud_platform"], specialties=["sre"]),
    dict(key="cloud-hpc", title="HPC Engineer",
         desc="Slurm, MPI, CUDA and GPU cluster scheduling.",
         company="CoreWeave", cohort="private_growth", source="career-pages",
         ats="ashby", tenant="coreweave", loc="Livingston, NJ, USA", days=3,
         families=["cloud_platform"], specialties=["hpc"]),
    dict(key="cloud-storage", title="Distributed Storage Engineer",
         desc="Object storage, erasure coding and filesystem internals.",
         company="Pure Storage", cohort="public_tech", source="workday",
         ats="workday", tenant="purestorage", loc="Santa Clara, CA, USA",
         days=7, families=["cloud_platform"], specialties=["storage_engineering"]),
    dict(key="cloud-edge", title="Edge Computing Engineer",
         desc="CDN, anycast routing and edge runtimes.", company="Fastly",
         cohort="public_tech", source="career-pages", ats="lever",
         tenant="fastly", loc="Denver, CO, USA", days=8,
         families=["cloud_platform"], specialties=["edge_cdn"]),

    # ---- AI / ML -----------------------------------------------------------
    dict(key="ai-mle", title="Machine Learning Engineer",
         desc="PyTorch, feature pipelines, model serving at scale.",
         company="Databricks", cohort="private_growth", source="career-pages",
         ats="greenhouse", tenant="databricks", loc="Seattle, WA, USA",
         days=1, families=["ai_ml"], specialties=["ml_engineering"]),
    dict(key="ai-agents", title="AI Agent Engineer",
         desc="Agentic workflows, tool use, evaluation harnesses.",
         company="Sierra AI", cohort="niche_startup", source="career-pages",
         ats="ashby", tenant="sierra", loc="Remote, USA", days=2,
         families=["ai_ml"], specialties=["ai_agents"]),
    dict(key="ai-inference", title="Inference Performance Engineer",
         desc="CUDA kernels, quantization and throughput optimisation.",
         company="Groq", cohort="private_growth", source="career-pages",
         ats="greenhouse", tenant="groq", loc="Mountain View, CA, USA",
         days=4, families=["ai_ml"], specialties=["inference_performance"]),

    # ---- data --------------------------------------------------------------
    dict(key="data-de", title="Data Engineer",
         desc="Spark, Airflow, dbt and warehouse modelling.",
         company="Fivetran", cohort="private_growth", source="career-pages",
         ats="lever", tenant="fivetran", loc="Oakland, CA, USA", days=3,
         families=["data"], specialties=["data_engineering"]),
    dict(key="data-dbre", title="Database Reliability Engineer",
         desc="PostgreSQL internals, replication and failover.",
         company="MongoDB", cohort="public_tech", source="career-pages",
         ats="greenhouse", tenant="mongodb", loc="New York, NY, USA", days=9,
         families=["data"], specialties=["database_engineering"]),

    # ---- security ----------------------------------------------------------
    dict(key="sec-appsec", title="Application Security Engineer",
         desc="Threat modelling, SAST, secure code review.",
         company="Wiz", cohort="private_growth", source="career-pages",
         ats="greenhouse", tenant="wiz", loc="Remote, USA", days=2,
         families=["security"], specialties=["application_security"]),
    dict(key="sec-crypto", title="Applied Cryptography Engineer",
         desc="Key management, HSMs and post quantum migration.",
         company="1Password", cohort="private_growth", source="career-pages",
         ats="ashby", tenant="1password", loc="Toronto, ON, Canada", days=6,
         families=["security"], specialties=["cryptography"]),
    dict(key="sec-privacy", title="Privacy Engineer",
         desc="Data minimisation, GDPR tooling and consent infrastructure.",
         company="Apple", cohort="mnc", source="career-pages", ats="greenhouse",
         tenant="apple", loc="Cupertino, CA, USA", days=5,
         families=["security"], specialties=["privacy_engineering"]),

    # ---- semiconductor -----------------------------------------------------
    dict(key="semi-asic", title="ASIC Physical Design Engineer",
         desc="Place and route, timing closure, tapeout.", company="NVIDIA",
         cohort="mnc", source="workday", ats="workday", tenant="nvidia",
         loc="Santa Clara, CA, USA", days=2,
         families=["semiconductor_hardware"], specialties=["asic_soc_design"]),
    dict(key="semi-dv", title="Design Verification Engineer",
         desc="UVM testbenches, SystemVerilog assertions for an SoC.",
         company="Synopsys", cohort="public_tech", source="career-pages",
         ats="smartrecruiters", tenant="synopsys", loc="Austin, TX, USA",
         days=4, families=["semiconductor_hardware"], specialties=["verification"]),
    dict(key="semi-analog", title="Analog IC Design Engineer",
         desc="Bandgap references, PLLs and SerDes front ends.",
         company="Texas Instruments", cohort="mnc", source="workday",
         ats="workday", tenant="ti", loc="Dallas, TX, USA", days=10,
         families=["semiconductor_hardware"], specialties=["analog_mixed_signal"]),
    dict(key="semi-photonics", title="Silicon Photonics Engineer",
         desc="Photonic integrated circuits and optical packaging.",
         company="Ayar Labs", cohort="niche_startup", source="career-pages",
         ats="lever", tenant="ayarlabs", loc="Santa Clara, CA, USA", days=7,
         families=["semiconductor_hardware"], specialties=["photonics"]),
    dict(key="semi-yield", title="Yield Engineer",
         desc="Wafer defect analysis, SPC and process integration in the fab.",
         company="Intel", cohort="mnc", source="workday", ats="workday",
         tenant="intel", loc="Hillsboro, OR, USA", days=11,
         families=["semiconductor_hardware"], specialties=["process_yield"]),

    # ---- quality -----------------------------------------------------------
    dict(key="qa-sdet", title="SDET",
         desc="Test automation frameworks, Playwright and CI pipelines.",
         company="Atlassian", cohort="public_tech", source="career-pages",
         ats="greenhouse", tenant="atlassian", loc="Austin, TX, USA", days=5,
         families=["quality"], specialties=["sdet"]),
    dict(key="qa-build", title="Build and Release Engineer",
         desc="Bazel, artifact promotion and release trains.",
         company="Shopify", cohort="public_tech", source="career-pages",
         ats="greenhouse", tenant="shopify", loc="Remote, Canada", days=6,
         families=["quality"], specialties=["build_release"]),

    # ---- ambiguous titles: retrievable, never asserted ---------------------
    dict(key="amb-mts", title="Member of Technical Staff",
         desc="Build core platform services in Go and Rust.",
         company="OpenAI", cohort="private_growth", source="career-pages",
         ats="ashby", tenant="openai", loc="San Francisco, CA, USA", days=1,
         families=[], specialties=[], ambiguous=True),
    dict(key="amb-analyst", title="Technology Analyst",
         desc="Support enterprise applications and reporting.",
         company="Infosys", cohort="mnc", source="workday", ats="workday",
         tenant="infosys", loc="Richardson, TX, USA", days=3,
         families=[], specialties=[], ambiguous=True),

    # ---- seniority ---------------------------------------------------------
    dict(key="sen-intern", title="Software Engineer Intern",
         desc="Summer internship building internal tools in Python.",
         company="Etsy", cohort="public_tech", source="career-pages",
         ats="greenhouse", tenant="etsy", loc="Brooklyn, NY, USA", days=2,
         families=["software_engineering"], specialties=[], seniority="intern"),
    dict(key="sen-principal", title="Principal Software Engineer",
         desc="12+ years. Set technical direction across the platform org.",
         company="Dell Technologies", cohort="mnc", source="workday",
         ats="workday", tenant="dell", loc="Round Rock, TX, USA", days=4,
         families=["software_engineering"], specialties=[], seniority="principal"),
    dict(key="sen-unknown", title="Software Engineer",
         desc="Work across our services. Python and SQL.",
         company="Booking.com", cohort="mnc", source="career-pages",
         ats="smartrecruiters", tenant="booking", loc="Amsterdam, Netherlands",
         days=5, families=["software_engineering"], specialties=[],
         seniority="unknown"),

    # ---- geography ---------------------------------------------------------
    dict(key="geo-remote-us", title="Backend Engineer, Remote",
         desc="Fully remote within the United States. Python and Django.",
         company="Deel", cohort="private_growth", source="career-pages",
         ats="ashby", tenant="deel", loc="Remote, USA", days=3,
         families=["software_engineering"], specialties=["backend"],
         work_mode="remote"),
    dict(key="geo-india", title="Backend Engineer",
         desc="Java and Spring Boot for our payments platform.",
         company="Razorpay", cohort="private_growth", source="career-pages",
         ats="lever", tenant="razorpay", loc="Bengaluru, KA, India", days=4,
         families=["software_engineering"], specialties=["backend"]),
    dict(key="geo-germany", title="Site Reliability Engineer",
         desc="Kubernetes and observability for a European platform.",
         company="Zalando", cohort="public_tech", source="career-pages",
         ats="smartrecruiters", tenant="zalando", loc="Berlin, Germany",
         days=6, families=["cloud_platform"], specialties=["sre"]),

    # ---- negatives: must never be retrieved by a core-scope search ---------
    dict(key="neg-nurse", title="Registered Nurse, ICU",
         desc="Direct patient care and medication administration.",
         company="Cleveland Clinic", cohort="unknown", source="workday",
         ats="workday", tenant="ccf", loc="Cleveland, OH, USA", days=2,
         families=[], specialties=[], negative=True),
    dict(key="neg-store", title="Store Associate",
         desc="Stock shelves and serve customers on the sales floor.",
         company="Apple", cohort="mnc", source="career-pages", ats="greenhouse",
         tenant="apple", loc="Cupertino, CA, USA", days=1,
         families=[], specialties=[], negative=True),
    dict(key="neg-trainer", title="Portuguese Voice Actor - Freelance AI Trainer",
         desc="Record speech samples for a training corpus.",
         company="Scale AI", cohort="private_growth", source="career-pages",
         ats="greenhouse", tenant="scaleai", loc="Remote, USA", days=3,
         families=[], specialties=[], negative=True),
    dict(key="neg-process", title="Process Engineer",
         desc="Optimize the polymerization line and reduce scrap.",
         company="Dow", cohort="unknown", source="workday", ats="workday",
         tenant="dow", loc="Midland, MI, USA", days=4,
         families=[], specialties=[], negative=True),
)


def seed_corpus(now: Optional[datetime] = None) -> int:
    """Write `CORPUS` into whatever inventory `job_inventory_service` points at."""
    from services import fit_score
    from services import job_inventory_service as inv

    now = now or NOW
    for row in CORPUS:
        raw = {
            "source": row["source"], "source_job_id": row["key"],
            "url": f"https://example.test/{row['tenant']}/{row['key']}",
            "company": row["company"], "title": row["title"],
            "location": [row["loc"]], "description": row["desc"],
            "employment_type": "Full time", "cost_class": "free",
            "source_quality": "direct", "industry": "tech",
            "ats": row["ats"], "tenant": row["tenant"],
            "posted_at": now - timedelta(days=int(row["days"])),
        }
        if row.get("work_mode"):
            raw["work_mode"] = row["work_mode"]
        inv.upsert_posting(raw, now=now)
        doc = inv.get_posting(row["source"], row["key"])
        inv.mark_requirements_parsed(
            row["source"], row["key"], doc["content_hash"],
            fit_score.extract_requirements(row["desc"], row["title"]), now=now)
    return len(CORPUS)


def _keys(result: Dict[str, Any]) -> List[str]:
    """The corpus keys a result set contains, in order."""
    by_url = {f"https://example.test/{r['tenant']}/{r['key']}": r["key"]
              for r in CORPUS}
    out = []
    for job in result.get("jobs") or []:
        k = by_url.get(job.get("canonical_url"))
        if k:
            out.append(k)
    return out


def _search(**over) -> Dict[str, Any]:
    from services import tech_feed_service as tfs
    body = {"tech_scope": ["core", "adjacent"], "page_size": 100,
            "personalize": False, "posted_within_days": 90}
    body.update(over)
    return tfs.search(body)


# ---------------------------------------------------------------------------
# Strata
# ---------------------------------------------------------------------------
def _classifiable(row: Dict[str, Any]) -> bool:
    """Rows a DEFAULT-scope search (`core` + `adjacent`) can return.

    Ambiguous and negative rows are excluded from the source / cohort /
    geography strata on purpose: those searches do not widen the scope, so an
    ambiguous title is correctly absent from them. Ambiguity has its own
    stratum, which widens the scope and asserts the row IS retrievable there.
    """
    return not row.get("negative") and not row.get("ambiguous")


def _expected_for_source(src: str) -> List[str]:
    return [r["key"] for r in CORPUS if r["source"] == src and _classifiable(r)]


def _expected_for_family(fam: str) -> List[str]:
    return [r["key"] for r in CORPUS if fam in (r.get("families") or ())]


def _expected_for_specialty(spec: str) -> List[str]:
    return [r["key"] for r in CORPUS if spec in (r.get("specialties") or ())]


def _expected_for_cohort(cohort: str) -> List[str]:
    return [r["key"] for r in CORPUS
            if r["cohort"] == cohort and _classifiable(r)]


def build_cases() -> List[Dict[str, Any]]:
    """Every stratum, as `(dimension, label, request, expected_keys)`."""
    cases: List[Dict[str, Any]] = []

    for src in sorted({r["source"] for r in CORPUS}):
        cases.append(dict(dimension="source", label=src,
                          request={}, expect=_expected_for_source(src),
                          restrict_to_source=src))

    for fam in sorted({f for r in CORPUS for f in (r.get("families") or ())}):
        cases.append(dict(dimension="family", label=fam,
                          request={"role_families": [fam]},
                          expect=_expected_for_family(fam)))

    for spec in sorted({s for r in CORPUS for s in (r.get("specialties") or ())}):
        cases.append(dict(dimension="specialty", label=spec,
                          request={"role_specialties": [spec]},
                          expect=_expected_for_specialty(spec)))

    for cohort in ("mnc", "public_tech", "private_growth", "niche_startup"):
        cases.append(dict(dimension="cohort", label=cohort,
                          request={}, expect=_expected_for_cohort(cohort),
                          restrict_to_cohort=cohort))

    cases.append(dict(dimension="ambiguous", label="retrievable_when_unknown",
                      request={"tech_scope": ["core", "adjacent", "unknown"]},
                      expect=[r["key"] for r in CORPUS if r.get("ambiguous")]))

    cases.append(dict(dimension="geography", label="united_states",
                      request={"countries": ["usa"]},
                      expect=[r["key"] for r in CORPUS
                              if "USA" in r["loc"] and _classifiable(r)]))
    cases.append(dict(dimension="geography", label="india",
                      request={"countries": ["india"]},
                      expect=[r["key"] for r in CORPUS if "India" in r["loc"]]))
    cases.append(dict(dimension="geography", label="germany",
                      request={"countries": ["germany"]},
                      expect=[r["key"] for r in CORPUS if "Germany" in r["loc"]]))

    cases.append(dict(dimension="seniority", label="intern",
                      request={"seniority": ["intern"]},
                      expect=["sen-intern"]))
    cases.append(dict(dimension="seniority", label="principal",
                      request={"seniority": ["principal"]},
                      expect=["sen-principal"]))
    cases.append(dict(dimension="seniority", label="unknown_is_included",
                      request={"seniority": ["mid", "senior", "unknown"]},
                      expect=["sen-unknown"], subset_ok=True))
    return cases


def run(verbose: bool = True) -> Dict[str, Any]:
    """Seed, run every stratum, report precision/recall per dimension."""
    seed_corpus()

    negatives = {r["key"] for r in CORPUS if r.get("negative")}
    rows: List[Dict[str, Any]] = []

    for case in build_cases():
        result = _search(**case["request"])
        got = _keys(result)

        if case.get("restrict_to_source"):
            src = case["restrict_to_source"]
            keys_of_src = {r["key"] for r in CORPUS if r["source"] == src}
            got = [k for k in got if k in keys_of_src]
        if case.get("restrict_to_cohort"):
            coh = case["restrict_to_cohort"]
            keys_of_coh = {r["key"] for r in CORPUS if r["cohort"] == coh}
            got = [k for k in got if k in keys_of_coh]

        expect = set(case["expect"])
        got_set = set(got)
        tp = len(expect & got_set)
        fn = len(expect - got_set)
        # A "false positive" here is a NEGATIVE row that a core-scope search
        # returned. Extra positives from other strata are not errors — a
        # family filter legitimately returns every row in that family.
        fp = len(got_set & negatives)
        rows.append({
            "dimension": case["dimension"], "label": case["label"],
            "tp": tp, "fp": fp, "fn": fn,
            "recall": (tp / (tp + fn)) if (tp + fn) else 1.0,
            "precision": (tp / (tp + fp)) if (tp + fp) else 1.0,
            "missing": sorted(expect - got_set),
            "leaked": sorted(got_set & negatives),
        })

    by_dim: Dict[str, Dict[str, int]] = collections.defaultdict(
        lambda: {"tp": 0, "fp": 0, "fn": 0, "cases": 0})
    for r in rows:
        d = by_dim[r["dimension"]]
        d["tp"] += r["tp"]
        d["fp"] += r["fp"]
        d["fn"] += r["fn"]
        d["cases"] += 1
    summary = {}
    for dim, d in by_dim.items():
        summary[dim] = {
            "cases": d["cases"], "tp": d["tp"], "fp": d["fp"], "fn": d["fn"],
            "recall": (d["tp"] / (d["tp"] + d["fn"])) if (d["tp"] + d["fn"]) else 1.0,
            "precision": (d["tp"] / (d["tp"] + d["fp"])) if (d["tp"] + d["fp"]) else 1.0,
        }

    total_tp = sum(d["tp"] for d in by_dim.values())
    total_fn = sum(d["fn"] for d in by_dim.values())
    total_fp = sum(d["fp"] for d in by_dim.values())
    overall = {
        "recall": (total_tp / (total_tp + total_fn)) if (total_tp + total_fn) else 1.0,
        "precision": (total_tp / (total_tp + total_fp)) if (total_tp + total_fp) else 1.0,
    }

    if verbose:
        from services import tech_feed_service as tfs
        from services import tech_taxonomy as tx
        print("")
        print("=" * 78)
        print(f"tech-feed retrieval eval   feed={tfs.FEED_VERSION}  "
              f"taxonomy={tx.TAXONOMY_VERSION}")
        print(f"{len(CORPUS)} postings, {len(rows)} strata")
        print("=" * 78)
        print(f"{'dimension':<14}{'cases':>7}{'recall':>10}{'precision':>12}"
              f"{'tp':>6}{'fn':>6}{'fp':>6}")
        for dim in sorted(summary):
            m = summary[dim]
            print(f"{dim:<14}{m['cases']:>7}{m['recall']:>10.3f}"
                  f"{m['precision']:>12.3f}{m['tp']:>6}{m['fn']:>6}{m['fp']:>6}")
        print("-" * 78)
        print(f"{'OVERALL':<14}{len(rows):>7}{overall['recall']:>10.3f}"
              f"{overall['precision']:>12.3f}")
        misses = [r for r in rows if r["missing"] or r["leaked"]]
        if misses:
            print(f"\n--- {len(misses)} stratum/strata with an issue ---")
            for r in misses:
                print(f"  {r['dimension']}/{r['label']}: "
                      f"missing={r['missing']} leaked={r['leaked']}")
        print("=" * 78)

    return {"rows": rows, "by_dimension": summary, "overall": overall,
            "corpus": len(CORPUS)}
