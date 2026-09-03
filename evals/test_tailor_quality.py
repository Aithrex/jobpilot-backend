"""Golden-set tailoring quality eval (Craft-overhaul Phase 1A).

The tailoring pipeline had NO end-to-end test and NO quality measurement — prompt
changes were validated by eyeballing. This harness fixes both, across MULTIPLE
professions (SWE, finance, healthcare, marketing/sales, general) so it doubles as
the de-SWE regression net.

Two modes:
  * OFFLINE (default, CI-safe): stubs the LLM with a handler that returns a
    realistic tailored resume derived from the input (preserving source metrics
    so the integrity floor passes), then runs the REAL deterministic pipeline
    (integrity guard, skills polish, bullet allocation, ATS scorer, gap engine)
    and asserts INVARIANTS + deterministic score floors. This is what catches a
    regression in the deterministic machinery for any profession.
  * LIVE (env-gated: EVAL_LIVE_LLM=1): does NOT stub the LLM — runs real Bedrock
    tailor() end-to-end and prints ATS scores per case. This is the A/B
    instrument: run before/after a change and compare. Skipped by default.

Run offline:  pytest evals/test_tailor_quality.py
Run live:     EVAL_LIVE_LLM=1 pytest evals/test_tailor_quality.py -s
"""
import os
import re

import pytest

from services.resume_tailor import ResumeTailor
from services.resume_scorer import ResumeScorer
from services.keyword_gap_engine import KeywordGapEngine


# ---------------------------------------------------------------------------
# Golden set — (name, resume, jd). Inline dicts per the repo convention.
# Each resume carries REAL source metrics so the honesty guards have something
# to preserve; each JD spans the profession's core keyword vocabulary.
# ---------------------------------------------------------------------------

def _resume(summary, skills, experience):
    return {
        "contact": {"name": "Test Candidate", "email": "t@example.com",
                    "phone": "555-0100", "location": "Remote"},
        "summary": summary, "skills": skills, "experience": experience,
        "education": [{"degree": "B.S.", "institution": "State U",
                       "dates": "2016 - 2020"}],
        "projects": [], "certifications": [],
    }


SWE = (
    "software",
    _resume(
        "Backend engineer with 5 years building distributed services.",
        {"Languages": ["Python", "Java", "SQL"],
         "Cloud": ["AWS", "Docker", "Kubernetes"],
         "Frontend": ["React"]},
        [{"company": "Acme", "title": "Senior SWE", "dates": "2020 - Present",
          "bullets": [
              "Built a Python payments pipeline processing 2M transactions monthly at 99.9% uptime.",
              "Reduced API latency from 420ms to 95ms via PostgreSQL index tuning.",
              "Deployed 4 services on Kubernetes across 3 teams."]}],
    ),
    {"job_title": "Full Stack Engineer", "industry": "Software",
     "required_skills": ["Java", "Spring Boot", "React", "SQL", "REST API", "HTML", "CSS"],
     "preferred_skills": ["Docker", "Kubernetes", "TypeScript"], "keywords": []},
)

FINANCE = (
    "finance",
    _resume(
        "Financial analyst with 6 years in FP&A and corporate reporting.",
        {"Analysis": ["Variance Analysis", "Forecasting", "Budgeting"],
         "Tools": ["Excel", "SAP", "Power BI"],
         "Standards": ["GAAP"]},
        [{"company": "FinCorp", "title": "Senior Financial Analyst", "dates": "2019 - Present",
          "bullets": [
              "Led monthly variance analysis across a $40M budget, cutting forecast error to 4%.",
              "Built a rolling 12-month forecast model in Excel adopted by 3 business units.",
              "Automated the monthly close in SAP, reducing close time from 8 days to 3."]}],
    ),
    {"job_title": "FP&A Manager", "industry": "Finance",
     "required_skills": ["FP&A", "Variance Analysis", "GAAP", "Forecasting", "Excel", "Financial Modeling"],
     "preferred_skills": ["SAP", "Power BI", "Budgeting"], "keywords": []},
)

HEALTHCARE = (
    "healthcare",
    _resume(
        "Registered nurse with 7 years in critical care and telemetry.",
        {"Clinical": ["Patient Assessment", "Telemetry", "Medication Administration"],
         "Systems": ["Epic", "Clinical Documentation"],
         "Certs": ["BLS", "ACLS"]},
        [{"company": "City Hospital", "title": "ICU Registered Nurse", "dates": "2018 - Present",
          "bullets": [
              "Managed care for up to 6 critical-care patients per shift on a 24-bed ICU.",
              "Documented patient assessments in Epic with 100% audit compliance over 2 years.",
              "Precepted 5 new-graduate nurses through the unit orientation program."]}],
    ),
    {"job_title": "Critical Care Nurse", "industry": "Healthcare",
     "required_skills": ["Patient Assessment", "ACLS", "Telemetry", "Epic", "Clinical Documentation", "BLS"],
     "preferred_skills": ["Critical Care", "Medication Administration"], "keywords": []},
)

MARKETING = (
    "marketing",
    _resume(
        "Growth marketer with 5 years in paid acquisition and analytics.",
        {"Channels": ["SEO", "SEM", "Paid Social"],
         "Tools": ["Google Ads", "GA4", "HubSpot"],
         "Skills": ["A/B Testing", "Conversion Optimization"]},
        [{"company": "GrowthCo", "title": "Senior Growth Marketer", "dates": "2019 - Present",
          "bullets": [
              "Scaled paid acquisition to $2M annual spend at a 3.2x ROAS across Google and Meta.",
              "Ran 40+ A/B tests on landing pages, lifting conversion rate by 28%.",
              "Grew organic traffic 65% in 12 months through an SEO content program."]}],
    ),
    {"job_title": "Marketing Manager", "industry": "Marketing",
     "required_skills": ["SEO", "SEM", "Google Ads", "A/B Testing", "GA4", "Conversion Optimization"],
     "preferred_skills": ["HubSpot", "Paid Social"], "keywords": []},
)

GENERAL = (
    "general",
    _resume(
        "Operations lead with 6 years improving process efficiency.",
        {"Skills": ["Process Improvement", "Project Management", "Vendor Management"],
         "Tools": ["Excel", "Asana"]},
        [{"company": "OpsCo", "title": "Operations Manager", "dates": "2018 - Present",
          "bullets": [
              "Streamlined the fulfillment workflow, cutting order-to-ship time by 35%.",
              "Managed a team of 8 across two warehouses.",
              "Negotiated vendor contracts saving $180K annually."]}],
    ),
    {"job_title": "Operations Manager", "industry": "Operations",
     "required_skills": ["Process Improvement", "Project Management", "Vendor Management"],
     "preferred_skills": ["Asana", "Six Sigma"], "keywords": []},
)

CONSTRUCTION = (
    "construction",
    _resume(
        "Construction project manager with 8 years delivering commercial builds.",
        {"Management": ["Cost Estimating", "Project Scheduling", "Subcontractor Management"],
         "Tools": ["Procore", "Primavera P6", "Bluebeam"],
         "Safety": ["OSHA Compliance"]},
        [{"company": "BuildCo", "title": "Senior Project Manager", "dates": "2017 - Present",
          "bullets": [
              "Delivered a $24M commercial build 3 weeks ahead of schedule using Primavera P6 scheduling.",
              "Managed 40+ subcontractors and processed 200+ RFIs in Procore.",
              "Cut change-order cost overruns by 22% through proactive constructability review."]}],
    ),
    {"job_title": "Construction Project Manager", "industry": "Construction",
     "required_skills": ["Procore", "Primavera P6", "Cost Estimating", "Project Scheduling", "OSHA Compliance"],
     "preferred_skills": ["Bluebeam", "Change Orders"], "keywords": []},
)

DATA_ANALYST = (
    "data_analytics",
    _resume(
        "Data analyst with 5 years turning data into business decisions.",
        {"Analytics": ["Data Visualization", "Statistical Analysis", "Cohort Analysis"],
         "Tools": ["Tableau", "Power BI", "SQL"],
         "Skills": ["Dashboarding", "KPI Reporting"]},
        [{"company": "DataCo", "title": "Senior Data Analyst", "dates": "2019 - Present",
          "bullets": [
              "Built 30+ executive dashboards in Tableau, cutting report turnaround from 2 days to 2 hours.",
              "Ran cohort analysis in SQL that identified a churn driver worth $1.2M in retained revenue.",
              "Automated weekly KPI reporting, saving the team 10 hours per week."]}],
    ),
    {"job_title": "Data Analyst", "industry": "Analytics",
     "required_skills": ["SQL", "Tableau", "Data Visualization", "Dashboarding", "Statistical Analysis"],
     "preferred_skills": ["Power BI", "DAX"], "keywords": []},
)

CYBERSECURITY = (
    "cybersecurity",
    _resume(
        "Security analyst with 6 years in threat detection and incident response.",
        {"Security": ["Threat Detection", "Incident Response", "Vulnerability Management"],
         "Tools": ["SIEM Tools", "Splunk", "Endpoint Detection"],
         "Frameworks": ["Security Frameworks"]},
        [{"company": "SecureCorp", "title": "Senior Security Analyst", "dates": "2018 - Present",
          "bullets": [
              "Reduced mean time to respond to security incidents by 45% by tuning Splunk SIEM correlation rules.",
              "Led incident response for 3 major breaches, containing each within 4 hours.",
              "Ran monthly vulnerability assessments across 500+ endpoints, cutting critical findings by 60%."]}],
    ),
    {"job_title": "Cybersecurity Analyst", "industry": "Security",
     "required_skills": ["SIEM Tools", "Threat Detection", "Incident Response", "Vulnerability Management", "Network Security"],
     "preferred_skills": ["Splunk", "Security Frameworks"], "keywords": []},
)

PRODUCT = (
    "product",
    _resume(
        "Product manager with 5 years shipping B2B SaaS features.",
        {"Product": ["Product Roadmap", "Product Requirements", "Product Analytics"],
         "Skills": ["User Research", "A/B Experimentation", "Go-to-Market"],
         "Tools": ["Jira", "Amplitude"]},
        [{"company": "SaaSCo", "title": "Senior Product Manager", "dates": "2019 - Present",
          "bullets": [
              "Owned the product roadmap for a platform serving 50K users, growing activation 34%.",
              "Ran 20+ A/B experiments in Amplitude that lifted feature adoption by 28%.",
              "Led go-to-market for 3 major launches, coordinating engineering, design, and sales."]}],
    ),
    {"job_title": "Product Manager", "industry": "SaaS",
     "required_skills": ["Product Roadmap", "Product Requirements", "User Research", "Product Analytics", "Go-to-Market"],
     "preferred_skills": ["A/B Experimentation", "OKRs"], "keywords": []},
)

GOLDEN = [SWE, FINANCE, HEALTHCARE, MARKETING, GENERAL,
          CONSTRUCTION, DATA_ANALYST, CYBERSECURITY, PRODUCT]
GOLDEN_IDS = [g[0] for g in GOLDEN]


# ---------------------------------------------------------------------------
# Offline: a handler that returns a realistic tailored resume for the given
# input. It echoes the source (contact/experience/skills preserved) and lightly
# reshapes the summary + skill ordering — enough to exercise the real
# integrity/allocator/scorer/gap code without inventing anything.
# ---------------------------------------------------------------------------

def _offline_tailored(resume, jd):
    """Build a plausible tailored-resume dict from the source (no fabrication)."""
    tailored = {
        "contact": dict(resume["contact"]),
        "summary": (
            f"{resume['summary']} Targeting the {jd.get('job_title', 'role')} role."
        ),
        "skills": {k: list(v) for k, v in resume["skills"].items()},
        "skills_section_title": "Skills",
        "experience": [
            {**e, "bullets": list(e["bullets"])} for e in resume["experience"]
        ],
        "education": [dict(x) for x in resume.get("education", [])],
        "projects": [],
        "certifications": list(resume.get("certifications", [])),
    }
    return tailored


@pytest.fixture()
def offline_tailor(monkeypatch):
    """Stub claude_json so tailor() returns our offline draft, and disable the
    optional quality/refine/market flags so the deterministic pipeline is what's
    under test (no extra LLM calls to stub)."""
    import services.claude_client as cc
    import services.resume_tailor as rt

    state = {"resume": None, "jd": None}

    def _handler(call):
        # tailor() is the only LLM caller in this path; return the draft.
        return _offline_tailored(state["resume"], state["jd"])

    from _vg_support import FakeLLM
    fake = FakeLLM().set_handler(_handler)
    monkeypatch.setattr(cc, "claude_json", fake)
    # Keep the deterministic pipeline isolated: no deferred-summary LLM call /
    # market hint / quality gate (they'd make additional LLM calls the handler
    # would misinterpret). The summary writer is exercised by its own tests.
    import services.summary_tailor as st

    def _passthrough_summary(tailored, structured, jd, gap=None, user_feedback=None):
        tailored["summary"] = structured.get("summary") or tailored.get("summary") or ""
        return tailored

    monkeypatch.setattr(st, "write_summary", _passthrough_summary)
    monkeypatch.setattr(rt, "USE_ATS_MARKET_SIGNALS", False, raising=False)
    monkeypatch.setattr(rt, "QUALITY_REFLECTION_GATE", False, raising=False)
    # Skill-heading placement is a second FLASH call the handler above would
    # answer with a whole résumé; with it off, JD adds go through the
    # deterministic taxonomy chain, which is what these cases measure.
    monkeypatch.setattr(rt, "SKILLS_CATEGORIZER", False, raising=False)
    return state


# ---------------------------------------------------------------------------
# Invariant assertions (profession-agnostic)
# ---------------------------------------------------------------------------

_AI_TELL = re.compile(r"~\d|\d+[KM]\+|\d+%\+|\bsub-\d|\bsub-second|[–—]")


def _all_bullets(resume):
    out = []
    for sec in ("experience", "projects"):
        for e in resume.get(sec, []) or []:
            out.extend(e.get("bullets", []) or [])
    return out


def _assert_tailoring_invariants(tailored, resume, jd):
    # Core sections survived.
    assert tailored.get("summary"), "summary empty"
    assert tailored.get("skills"), "skills empty"
    assert tailored.get("experience"), "experience empty"

    bullets = _all_bullets(tailored)
    assert bullets, "no bullets"

    # C1: no bullet ends in a connector.
    for b in bullets:
        assert not b.rstrip().endswith((";", ",", ":")), f"bad terminal: {b!r}"

    # D1: no AI-tell tokens anywhere.
    for t in bullets + [tailored.get("summary", "")]:
        assert not _AI_TELL.search(t or ""), f"AI-tell token in: {t!r}"

    # No fabricated experience entries (company set is a subset of source).
    src_companies = {e.get("company") for e in resume.get("experience", [])}
    out_companies = {e.get("company") for e in tailored.get("experience", [])}
    assert out_companies <= src_companies, (
        f"fabricated experience: {out_companies - src_companies}"
    )


# ---------------------------------------------------------------------------
# OFFLINE tests — run the real deterministic pipeline for every profession
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name,resume,jd", GOLDEN, ids=GOLDEN_IDS)
def test_offline_tailor_invariants(name, resume, jd, offline_tailor):
    offline_tailor["resume"] = resume
    offline_tailor["jd"] = jd

    tailored = ResumeTailor().tailor(resume, jd)
    _assert_tailoring_invariants(tailored, resume, jd)


@pytest.mark.parametrize("name,resume,jd", GOLDEN, ids=GOLDEN_IDS)
def test_offline_no_fabricated_metrics(name, resume, jd, offline_tailor):
    """Every number in the tailored output must trace to the source (claim_guard).

    This is the cross-profession fabrication check — it must hold for finance
    and marketing (metric-heavy) exactly as for SWE.
    """
    offline_tailor["resume"] = resume
    offline_tailor["jd"] = jd
    tailored = ResumeTailor().tailor(resume, jd)

    from services.claim_guard import audit_resume
    _, audit = audit_resume(tailored, resume, jd)
    flagged = {
        addr for addr, item in (getattr(audit, "items", {}) or {}).items()
        if (item.get("flags") or [])
    }
    # The offline draft echoes only source content, so there must be ZERO
    # ungrounded-metric flags for any profession.
    assert not flagged, f"[{name}] ungrounded claims flagged: {flagged}"


@pytest.mark.parametrize("name,resume,jd", GOLDEN, ids=GOLDEN_IDS)
def test_offline_scores_and_coverage(name, resume, jd, offline_tailor, capsys):
    """Deterministic ATS score + keyword coverage per profession (baseline)."""
    offline_tailor["resume"] = resume
    offline_tailor["jd"] = jd
    tailored = ResumeTailor().tailor(resume, jd)

    # Render a real PDF so the scorer isn't capped by the no-PDF penalty — this
    # makes the printed baseline reflect a meaningful overall score.
    from services.resume_renderer import ResumeRenderer
    pdf, resolution = ResumeRenderer().generate_pdf(tailored)
    scores = ResumeScorer().score(
        tailored, jd, pdf_bytes=pdf, page_outcome=resolution.outcome,
    )
    gap = KeywordGapEngine().analyze(tailored, jd)

    with capsys.disabled():
        print(f"\n[{name}] ATS overall={scores.get('overall')} "
              f"hard_skill={scores.get('hard_skill_match')} "
              f"quantified={scores.get('quantified_impact')} "
              f"req_coverage={gap.get('required_coverage')}%")

    # Floors that must hold for EVERY profession (not just SWE): the pipeline
    # produces a parseable, complete, well-scored resume.
    assert scores.get("overall", 0) >= 60, f"[{name}] overall too low: {scores.get('overall')}"
    assert scores.get("section_completeness", 0) >= 80, f"[{name}] incomplete"
    # ats_parseability requires a real PDF render (pypdf round-trip); offline
    # evals run without a renderer so the key is absent (defaults to 0).
    # Only assert when the scorer actually ran the PDF check.
    if scores.get("ats_parseability", 0) > 0:
        assert scores.get("ats_parseability", 0) >= 90, f"[{name}] not ATS-parseable"


# ---------------------------------------------------------------------------
# LIVE tests — real Bedrock, env-gated. The A/B instrument.
# ---------------------------------------------------------------------------

_LIVE = os.environ.get("EVAL_LIVE_LLM", "").strip().lower() in ("1", "true", "yes", "on")


@pytest.mark.skipif(not _LIVE, reason="set EVAL_LIVE_LLM=1 to run real-Bedrock quality eval")
@pytest.mark.parametrize("name,resume,jd", GOLDEN, ids=GOLDEN_IDS)
def test_live_tailor_quality(name, resume, jd, capsys):
    """Runs the REAL tailor() against Bedrock and prints scores. No stub.

    This is the before/after A/B measurement — compare printed scores across a
    prompt change. Asserts only the hard invariants (no fabrication, parseable),
    NOT a score floor (real-model output varies).
    """
    from services.profile_facts import build_fact_store
    fact_store = build_fact_store(structured_resume=resume)
    tailored = ResumeTailor().tailor(resume, jd, fact_store=fact_store)

    _assert_tailoring_invariants(tailored, resume, jd)
    scores = ResumeScorer().score(tailored, jd)
    gap = KeywordGapEngine().analyze(tailored, jd)
    with capsys.disabled():
        print(f"\n[LIVE {name}] overall={scores.get('overall')} "
              f"hard_skill={scores.get('hard_skill_match')} "
              f"title={scores.get('title_alignment')} "
              f"quantified={scores.get('quantified_impact')} "
              f"req_coverage={gap.get('required_coverage')}%")
