"""tailor_verifier — merge policy + fail-open behavior (fake judge, no LLM).

Trust model under test: deny-by-default (missing/unknown verdicts →
UNVERIFIED), deterministic claim-guard flags clamp verdicts downward, and
any verifier failure yields status="skipped" without raising.
"""
import pytest

from services.claim_guard import ClaimAudit
from services.tailor_verifier import enumerate_lines, verify


TAILORED = {
    "contact": {},
    "summary": "Backend engineer with Python and AWS experience. Ships reliable services.",
    "skills": {"Skills": ["Python"]},
    "experience": [{
        "title": "Engineer", "company": "Acme", "location": "",
        "dates": "March 2022 - Present", "type": "Full-time",
        "bullets": [
            "Built Python microservices on AWS Lambda",
            "Reduced API latency by tuning PostgreSQL queries",
        ],
    }],
    "education": [], "projects": [], "certifications": [],
}

FACT_STORE = {
    "facts": [
        {"fact_id": "res:exp:0", "type": "employment", "source": "resume",
         "display": "EMPLOYMENT: Engineer at Acme, March 2022 - Present"},
        {"fact_id": "res:exp:0:b0", "type": "bullet", "source": "resume",
         "display": 'ORIGINAL BULLET (Acme): "Built Python services on AWS"'},
    ],
    "by_id": {},
    "sources": {"resume": True, "linkedin": False, "github": False},
}


@pytest.fixture()
def fake_judge(monkeypatch):
    """Patch claude_json with a controllable verdict map."""
    state = {"responses": {}, "raise_error": False, "calls": 0}

    def _fake(prompt=None, **kw):
        state["calls"] += 1
        if state["raise_error"]:
            raise RuntimeError("bedrock down")
        return {"results": [
            {"id": addr, "verdict": verdict, "evidence": ev, "unsupported_claim": claim}
            for addr, (verdict, ev, claim) in state["responses"].items()
        ]}

    import services.claude_client as cc
    monkeypatch.setattr(cc, "claude_json", _fake)
    return state


def test_enumerate_lines_addresses():
    lines = dict(enumerate_lines(TAILORED))
    assert "summary.s0" in lines and "summary.s1" in lines
    assert lines["experience.0.bullets.0"] == "Built Python microservices on AWS Lambda"
    assert "experience.0.bullets.1" in lines


def test_supported_and_missing_ids(fake_judge):
    fake_judge["responses"] = {
        "experience.0.bullets.0": ("SUPPORTED", ["F2"], ""),
        # bullets.1 and both summary sentences deliberately missing
    }
    sidecar = verify(TAILORED, FACT_STORE)
    assert sidecar["status"] == "degraded"  # some lines unverified
    items = sidecar["items"]
    assert items["experience.0.bullets.0"]["verdict"] == "SUPPORTED"
    assert items["experience.0.bullets.0"]["evidence_fact_ids"] == ["F2"]
    assert items["experience.0.bullets.1"]["verdict"] == "UNVERIFIED"
    assert items["summary.s0"]["verdict"] == "UNVERIFIED"


def test_unknown_verdict_is_unverified(fake_judge):
    fake_judge["responses"] = {
        "experience.0.bullets.0": ("TOTALLY_FINE", [], ""),
    }
    sidecar = verify(TAILORED, FACT_STORE)
    assert sidecar["items"]["experience.0.bullets.0"]["verdict"] == "UNVERIFIED"


def test_claim_guard_flags_clamp_supported_to_partial(fake_judge):
    addr = "experience.0.bullets.1"
    fake_judge["responses"] = {
        "summary.s0": ("SUPPORTED", ["F1"], ""),
        "summary.s1": ("SUPPORTED", ["F1"], ""),
        "experience.0.bullets.0": ("SUPPORTED", ["F2"], ""),
        addr: ("SUPPORTED", ["F2"], ""),  # LLM says fine...
    }
    audit = ClaimAudit()
    audit.flag(addr, TAILORED["experience"][0]["bullets"][1],
               "ungrounded_metric", "45%")  # ...deterministic guard disagrees
    sidecar = verify(TAILORED, FACT_STORE, claim_audit=audit)
    item = sidecar["items"][addr]
    assert item["verdict"] == "PARTIAL"  # clamped down, never up
    assert item["flags"] == ["ungrounded_metric"]
    assert "45%" in item["unsupported_claim"]
    assert sidecar["status"] == "ok"
    assert sidecar["counts"]["partial"] == 1
    assert sidecar["counts"]["supported"] == 3


def test_verifier_failure_fails_open(fake_judge):
    fake_judge["raise_error"] = True
    audit = ClaimAudit()
    audit.flag("experience.0.bullets.0", "Built Python microservices on AWS Lambda",
               "ungrounded_metric", "5M")
    sidecar = verify(TAILORED, FACT_STORE, claim_audit=audit)
    assert sidecar["status"] == "skipped"
    assert "reason" in sidecar
    # All items UNVERIFIED, but deterministic flags still surface.
    assert all(i["verdict"] in ("UNVERIFIED", "PARTIAL")
               for i in sidecar["items"].values())
    assert sidecar["items"]["experience.0.bullets.0"]["flags"] == ["ungrounded_metric"]


def test_empty_fact_sheet_skips(fake_judge):
    sidecar = verify(TAILORED, {"facts": [], "by_id": {}, "sources": {}})
    assert sidecar["status"] == "skipped"
    assert fake_judge["calls"] == 0
    assert all(i["verdict"] == "UNVERIFIED" for i in sidecar["items"].values())
