"""JD analyzer v2 — full-text requirement records, no logistics-as-keywords."""
from services.jd_analyzer import (
    enrich,
    extract_requirement_records,
    llm_window,
    strip_logistics_from_keywords,
)


def _long_jd(tail: str) -> str:
    pad = (
        "The unit collaborates across departments and values communication, "
        "documentation, and a calm presence in a fast-paced environment. "
    )
    body = pad * 200
    assert len(body) > 10000
    return body + "\n\n" + tail


def test_license_in_final_paragraph_of_12k_jd_is_captured():
    jd = _long_jd(
        "Must hold a current RN license and an unencumbered RN in this state."
    )
    assert len(jd) >= 12000
    records = extract_requirement_records(jd)
    kinds = {r["requirement_type"] for r in records}
    texts = " ".join(r["text"].lower() for r in records)
    assert "license" in kinds
    assert "registered nurse" in texts or "rn" in texts


def test_pe_and_cpa_in_tail_are_captured():
    pe = extract_requirement_records(
        _long_jd("Applicants must hold a PE license in the state of practice.")
    )
    assert any(r["requirement_type"] == "license" for r in pe)
    cpa = extract_requirement_records(
        _long_jd("CPA license required. Certified public accountant preferred.")
    )
    assert any("cpa" in r["text"].lower() for r in cpa)


def test_work_auth_never_becomes_a_keyword():
    analysis = {
        "required_skills": ["Python", "H-1B sponsorship", "US citizen"],
        "preferred_skills": ["visa sponsorship"],
        "keywords": ["remote", "Python"],
    }
    cleaned = strip_logistics_from_keywords(analysis)
    joined = " ".join(
        cleaned["required_skills"] + cleaned["preferred_skills"] + cleaned["keywords"]
    ).lower()
    assert "python" in joined
    assert "sponsor" not in joined
    assert "citizen" not in joined
    assert "remote" not in joined


def test_enrich_keeps_flat_lists_and_adds_requirements():
    jd = "Registered nurse license required. Epic experience preferred."
    out = enrich(
        {
            "required_skills": ["Epic"],
            "preferred_skills": [],
            "keywords": ["Epic"],
        },
        jd,
    )
    assert out["required_skills"] == ["Epic"]
    assert out["requirements"]
    assert any(r["requirement_type"] == "license" for r in out["requirements"])


def test_llm_window_includes_the_tail_of_a_long_jd():
    jd = _long_jd("UNIQUE_TAIL_LICENSE_MARKER PE license required.")
    window = llm_window(jd)
    assert "UNIQUE_TAIL_LICENSE_MARKER" in window
    assert len(window) < len(jd)
