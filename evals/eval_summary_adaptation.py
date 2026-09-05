"""Controlled generation eval for JD-adaptive summaries.

Offline (default): asserts deterministic brief/role policy across the
multi-user matrix. Live (EVAL_LIVE_LLM=1): calls the real writer and
checks that summaries adapt across JDs without inventing identity.

Run offline:  pytest evals/eval_summary_adaptation.py
Run live:     EVAL_LIVE_LLM=1 pytest evals/eval_summary_adaptation.py -s
"""
import os

import pytest

from services.summary_tailor import build_summary_brief, write_summary
from tests.fixtures.summary_role_matrix import (
    BACKEND_ENGINEER,
    FINANCE_ANALYST,
    JDS,
    MARKETING_COORDINATOR,
    NURSE,
    gap_for,
    tailored_from,
)

LIVE = os.environ.get("EVAL_LIVE_LLM") == "1"


def _brief(profile, jd_key, required=None, matched=None):
    structured = profile["structured"]
    return build_summary_brief(
        tailored_from(structured),
        structured,
        JDS[jd_key],
        gap_for(required, matched),
    )


def test_matrix_role_labels_are_profile_specific():
    backend_sde = _brief(BACKEND_ENGINEER, "sde", ["Python"], ["AWS"])
    backend_fe = _brief(BACKEND_ENGINEER, "frontend", ["React"], [])
    marketing = _brief(MARKETING_COORDINATOR, "office_manager")
    finance = _brief(FINANCE_ANALYST, "staff_accountant", ["Excel"])
    nurse = _brief(
        NURSE,
        "office_manager",
    )
    nurse = build_summary_brief(
        tailored_from(NURSE["structured"]),
        NURSE["structured"],
        {"job_title": "Registered Nurse", "responsibilities": ["Provide bedside care"]},
        gap_for(),
    )

    assert backend_sde.role_family == "Software engineer"
    assert not backend_fe.role_family.lower().startswith("frontend")
    assert marketing.role_family != "Software engineer"
    assert finance.role_family != "Software engineer"
    assert nurse.role_family != "Software engineer"
    assert "fastapi" not in " ".join(finance.selected_terms).lower()
    assert "kubernetes" not in " ".join(marketing.selected_terms).lower()


def test_same_user_two_jds_change_selected_facts():
    reliability = _brief(BACKEND_ENGINEER, "same_title_reliability", ["Python", "AWS"])
    delivery = _brief(BACKEND_ENGINEER, "same_title_delivery", ["Python", "Django"])
    assert reliability.role_family == delivery.role_family
    assert (
        reliability.prioritized_capabilities != delivery.prioritized_capabilities
        or set(t.lower() for t in reliability.selected_terms)
        != set(t.lower() for t in delivery.selected_terms)
    )


@pytest.mark.skipif(not LIVE, reason="EVAL_LIVE_LLM=1 required for generation eval")
def test_live_summaries_adapt_without_identity_theft():
    structured = BACKEND_ENGINEER["structured"]
    tailored = tailored_from(structured)
    sde = write_summary(dict(tailored), structured, JDS["sde"], gap_for(["Python"], ["AWS"]))
    frontend = write_summary(
        dict(tailored), structured, JDS["frontend"], gap_for(["React"]),
    )
    print("SDE summary:", sde.get("summary"))
    print("Frontend summary:", frontend.get("summary"))
    print("SDE sidecar:", sde.get("summary_write"))
    print("Frontend sidecar:", frontend.get("summary_write"))

    if sde["summary_write"]["source"] == "generated":
        assert sde["summary"].lower().startswith("software engineer")
    if frontend["summary_write"]["source"] == "generated":
        assert not frontend["summary"].lower().startswith("frontend")
    assert "langchain" not in (sde.get("summary") or "").lower()
    assert "machine learning engineer" not in (sde.get("summary") or "").lower()
