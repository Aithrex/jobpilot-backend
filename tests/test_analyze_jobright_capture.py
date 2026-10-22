"""The offline capture analysis counts only what the captured fields support."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _mod():
    spec = importlib.util.spec_from_file_location(
        "analyze_jobright_capture", ROOT / "scripts" / "analyze_jobright_capture.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _card(i, company, title, url, *, age="3 hours ago", attrs=("Full-time", "Mid Level"),
          triage="Unclassified; verify employer and role", location="Austin, TX"):
    return {"job_id": f"jr{i}", "company": company, "title": title, "location": location,
            "card_attributes": list(attrs), "original_job_url": url,
            "original_link_status": "observed_on_jobright_detail", "displayed_age": age,
            "preference_triage": triage, "fraud_verdict": "not_determined",
            "official_requisition_verified": False}


def test_analysis_counts_with_denominators(tmp_path):
    portal = "https://www1.jobdiva.com/portal/?a=tok&jr_id={}#/jobs/{}"
    jobs = [
        _card(1, "OpenAI", "SWE", "https://jobs.ashbyhq.com/openai/e6d5ca02-f30b-4ac5-a69d-"
              "c947efb430f9/application?jr_id=a"),
        _card(2, "OpenAI", "SWE", "https://jobs.ashbyhq.com/openai/e6d5ca02-f30b-4ac5-a69d-"
              "c947efb430f9/application?jr_id=b", age="Reposted 2 hours ago"),
        _card(3, "Scalence L.L.C.", "SRE II", portal.format("x", "111")),
        _card(4, "Scalence L.L.C.", "Data Engineer", portal.format("y", "222")),
        _card(5, "Capgemini", "Java Developer", "https://careers.capgemini.com/job/x/1/",
              triage="Consultancy exclusion candidate"),
        _card(6, "Mercor", "MacOS Developer", "https://www.ziprecruiter.com/kn/abc123def456",
              attrs=("Contract", "Mid Level")),
        _card(7, "Acme", "Engineer", "https://www.linkedin.com/jobs/view/4481153600",
              attrs=("Entry Level",)),
    ]
    (tmp_path / "jobright-postings-reviewed.json").write_text(json.dumps({
        "captured_on": "2026-10-21", "timezone": "America/Phoenix", "scope": "test",
        "filters": ["Entry Level (+1)", "0-2 Years"], "jobs": jobs}))
    m = _mod()
    a = m.analyze(tmp_path)
    assert a["cards"] == 7
    assert [g["company"] for g in a["proven_duplicate_groups"]] == ["OpenAI"]
    assert a["generic_portal_groups"][0]["distinct_fragment_job_ids"] == 2
    assert next(f for f in a["service_firms"] if f["firm"] == "Capgemini")["cards"] == 1
    assert [t["company"] for t in a["task_contracts"]] == ["Mercor"]
    assert a["reposted_label_cards"] == 1
    assert sum(a["original_link_on_aggregator"].values()) == 2
    assert a["no_entry_level_label"] == 6
    md = m.render(a)
    assert "No fraud is established" in md and "1/7" in md
    assert "linkedin" not in a          # optional input absent -> section skipped
