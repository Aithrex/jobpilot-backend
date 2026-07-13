"""claim_guard — deterministic numeric/entity grounding audit tests.

Pure functions: no LLM, no Mongo. The guard runs on FINAL tailored text and
flags (never silently strips) ungrounded metrics; strict mode additionally
downgrades safely-removable trailing metric clauses.
"""
from services.claim_guard import (
    audit_resume,
    extract_numbers,
    safe_downgrade,
)


def _source(bullets, dates="June 2019 - Present"):
    return {
        "contact": {}, "summary": "",
        "skills": {"Skills": ["Python", "Airflow"]},
        "experience": [{
            "title": "Engineer", "company": "Acme", "location": "",
            "dates": dates, "type": "Full-time", "bullets": list(bullets),
        }],
        "education": [], "projects": [], "certifications": [],
    }


def _tailored(bullets, summary=""):
    return {
        "contact": {}, "summary": summary,
        "skills": {"Skills": ["Python", "Airflow"]},
        "experience": [{
            "title": "Engineer", "company": "Acme", "location": "",
            "dates": "June 2019 - Present", "type": "Full-time",
            "bullets": list(bullets),
        }],
        "education": [], "projects": [], "certifications": [],
    }


# ---------------------------------------------------------------------------
# extract_numbers
# ---------------------------------------------------------------------------

def test_extract_number_classes():
    claims = extract_numbers(
        "Cut costs by 38% and $1.2M, 3x faster, from 420ms to 95ms, 120K users, 10+"
    )
    by_class = {(c.value_norm, c.unit_class) for c in claims}
    assert ("38", "percent") in by_class
    assert ("1.2", "currency") in by_class
    assert ("3", "multiplier") in by_class
    assert ("420", "duration") in by_class and ("95", "duration") in by_class
    assert ("120", "scaled") in by_class
    assert ("10", "plus") in by_class


def test_extract_numbers_empty():
    assert extract_numbers("") == []
    assert extract_numbers("No digits here at all.") == []


# ---------------------------------------------------------------------------
# Grounding audit (flag mode — default)
# ---------------------------------------------------------------------------

def test_grounded_metric_not_flagged():
    src = _source(["Cut infrastructure costs by 38% through autoscaling"])
    out = _tailored(["Reduced cloud infrastructure spend by 38% via autoscaling policies"])
    _, audit = audit_resume(out, src, mode="flag")
    assert audit.items == {}


def test_invented_metric_flagged_not_stripped():
    src = _source(["Maintained internal reporting scripts for the sales team"])
    bullet = "Automated reporting, improving efficiency by 45% across the org"
    out = _tailored([bullet])
    result, audit = audit_resume(out, src, mode="flag")
    addr = "experience.0.bullets.0"
    assert addr in audit.items
    assert "ungrounded_metric" in audit.items[addr]["flags"]
    assert "45%" in audit.items[addr]["unsupported_claims"]
    # Flag mode never modifies text
    assert result["experience"][0]["bullets"][0] == bullet


def test_latency_pair_grounds_both_endpoints():
    src = _source(["Reduced response time from 420ms to 95ms by tuning queries"])
    out = _tailored(["Cut p95 latency from 420ms to 95ms through query optimization"])
    _, audit = audit_resume(out, src, mode="flag")
    flagged_claims = [
        c for item in audit.items.values() for c in item["unsupported_claims"]
    ]
    assert "420ms" not in flagged_claims and "95ms" not in flagged_claims


def test_version_numbers_not_flagged():
    src = _source(["Built services in Python for data processing"])
    out = _tailored(["Built data services in Python 3 with type-checked interfaces"])
    _, audit = audit_resume(out, src, mode="flag")
    assert audit.items == {}


def test_date_years_and_tenure_grounded():
    src = _source(
        ["Shipped features for the platform team"],
        dates="June 2019 - Present",
    )
    out = _tailored(["Shipped platform features continuously since 2019"])
    _, audit = audit_resume(out, src, mode="flag")
    assert audit.items == {}


def test_unsupported_jd_product_flagged():
    src = _source(["Built Python data pipelines with Airflow"])
    out = _tailored(["Built Python data pipelines with Airflow and ServiceNow workflows"])
    jd = {"required_skills": ["ServiceNow", "Python"], "keywords": []}
    _, audit = audit_resume(out, src, jd_analysis=jd, mode="flag")
    addr = "experience.0.bullets.0"
    assert addr in audit.items
    assert "unsupported_product" in audit.items[addr]["flags"]
    assert "ServiceNow" in audit.items[addr]["unsupported_claims"]
    # Python IS in the source — must not be flagged
    assert "Python" not in audit.items[addr]["unsupported_claims"]


def test_summary_sentences_audited_read_only():
    src = _source(["Did work"])
    out = _tailored(["Did work"], summary="Engineer who cut costs by 60%. Builds pipelines.")
    result, audit = audit_resume(out, src, mode="strict")
    assert "summary.s0" in audit.items
    # Strict mode still never rewrites summary prose
    assert "60%" in result["summary"]


# ---------------------------------------------------------------------------
# safe_downgrade + strict mode
# ---------------------------------------------------------------------------

def test_safe_downgrade_trailing_clause():
    bullet = "Rebuilt the ETL pipeline in Airflow with idempotent tasks, reducing processing time by 40%"
    claim = next(c for c in extract_numbers(bullet) if c.unit_class == "percent")
    downgraded = safe_downgrade(bullet, claim)
    assert downgraded == "Rebuilt the ETL pipeline in Airflow with idempotent tasks."


def test_safe_downgrade_returns_none_when_unsafe():
    bullet = "Served 45% of tenant traffic through the new gateway layer daily"
    claim = next(c for c in extract_numbers(bullet) if c.unit_class == "percent")
    assert safe_downgrade(bullet, claim) is None


def test_strict_mode_downgrades_where_safe():
    src = _source(["Rebuilt the ETL pipeline in Airflow with idempotent tasks"])
    out = _tailored([
        "Rebuilt the ETL pipeline in Airflow with idempotent tasks, reducing processing time by 40%",
    ])
    result, audit = audit_resume(out, src, mode="strict")
    assert audit.downgraded == 1
    final = result["experience"][0]["bullets"][0]
    assert "40%" not in final
    assert final.endswith("idempotent tasks.")
