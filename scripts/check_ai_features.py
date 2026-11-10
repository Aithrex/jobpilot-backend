"""Run the real AI features once, end to end, against the configured Gemini.

`check_gemini.py` proves the credentials answer a two-line prompt. This proves
the features users pay for still work on the models those credentials reach:
the SAME service code the async jobs call, on a synthetic résumé and JD.

    python scripts/check_ai_features.py            # every step
    python scripts/check_ai_features.py --only tailor

Steps (tier in brackets):
    parse_text   résumé text  -> structured résumé       [FLASH]
    parse_pdf    résumé PDF   -> structured résumé       [FLASH, upload path]
    jd           job text     -> JD analysis             [PRO]   (LLM only: the
                 local-reader fallback the app uses is disabled here, so a
                 broken model cannot pass by falling back)
    tailor       résumé + JD  -> tailored résumé         [PRO]
    cover_letter tailored     -> cover letter            [PRO]
    chat         tool calling -> reply                   [FLASH]

SAFETY: the database is switched off for the whole run. `.env` names the
production cluster, so `DBConnect` is replaced with one that raises; a step
that tries to read or write Mongo FAILS here instead of touching prod. The
résumé is invented and every call masks contact details anyway.

Exit code 0 only when every selected step passed.
"""
import argparse
import base64
import io
import json
import os
import sys
import time
import traceback
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "shared" / "python"))
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

os.environ["LLM_PROVIDER"] = "gemini"


class _NoDB:
    """Stands in for the pymongo Database the parser is constructed with.
    Attribute access works (the constructor only stores a collection handle);
    any call on a collection raises, so parsing can never save anything."""

    def __getattr__(self, name):
        return self

    __getitem__ = __getattr__

    def __call__(self, *a, **k):
        raise RuntimeError("check_ai_features: the database is disabled for this run")


def _block_database():
    from utils import db_connect

    def _refuse(self, *a, **k):
        raise RuntimeError("check_ai_features: the database is disabled for this run")

    db_connect.DBConnect._connect = _refuse
    db_connect.DBConnect.get_db = _refuse
    db_connect.DBConnect.get_collection = _refuse


RESUME = """Priya Raman
priya.raman@example.com | (555) 010-2233 | linkedin.com/in/priya-raman-example | github.com/priya-example

SUMMARY
Backend engineer with 3 years building Python services and data pipelines on AWS.

EXPERIENCE
Software Engineer, Northwind Analytics — Austin, TX — Jun 2023 to Present
- Built a FastAPI service that serves 2M pricing requests a day with p99 under 120 ms.
- Moved nightly ETL from cron scripts to Airflow on AWS, cutting failed runs by 70%.
- Added PostgreSQL indexes and query rewrites that cut report generation from 9 to 2 minutes.

Software Engineering Intern, Contoso Health — Remote — May 2022 to Aug 2022
- Wrote Python scripts to validate 40k insurance claims a week against payer rules.
- Added pytest coverage to a Flask API, raising coverage from 35% to 80%.

EDUCATION
M.S. Computer Science, University of Texas at Dallas — May 2023
B.Tech Computer Science, Anna University — May 2021

SKILLS
Languages: Python, SQL, JavaScript
Frameworks: FastAPI, Flask, Airflow
Cloud & Data: AWS (S3, Lambda, ECS), PostgreSQL, Docker
"""

JD = """Backend Engineer II — Data Platform
Acme Freight, Chicago, IL (Hybrid)

We are looking for a Backend Engineer to build the services behind our shipment
tracking platform.

Responsibilities
- Design and build Python APIs that serve shipment events to customers.
- Own batch and streaming data pipelines (Airflow, Kafka).
- Tune PostgreSQL performance and data models.
- Work with product and data science on new tracking features.

Requirements
- 2+ years of professional backend experience in Python.
- Experience with REST APIs (FastAPI or Flask), PostgreSQL and AWS.
- Experience with Docker and CI/CD.

Nice to have
- Kafka, Kubernetes, Terraform.
- Logistics or supply-chain domain experience.
"""


def _pdf_base64(text: str) -> str:
    from fpdf import FPDF

    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=10)
    for line in text.splitlines():
        safe = line.replace("—", "-").encode("latin-1", "replace").decode("latin-1")
        pdf.multi_cell(0, 5, safe or " ", new_x="LMARGIN", new_y="NEXT")
    return base64.b64encode(bytes(pdf.output())).decode()


def _require(cond, what):
    if not cond:
        raise AssertionError(what)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="comma-separated steps to run")
    args = ap.parse_args()

    _block_database()

    from services.claude_client import CLAUDE_FLASH, CLAUDE_PRO, get_active_provider
    provider = get_active_provider()
    print(f"Backend: {provider.backend()}   FLASH={CLAUDE_FLASH}   PRO={CLAUDE_PRO}")

    state = {}

    def parse_text():
        from services.resume_parser import ResumeParser
        structured, _ = ResumeParser(db=_NoDB()).parse_to_structured(raw_text=RESUME)
        _require(len(structured.get("experience") or []) >= 2, "expected 2 experience entries")
        _require(structured.get("education"), "no education parsed")
        state["structured"] = structured
        return f"{len(structured['experience'])} roles, {len(structured['education'])} degrees"

    def parse_pdf():
        from services.resume_parser import ResumeParser
        structured, _ = ResumeParser(db=_NoDB()).parse_to_structured(
            file_base64=_pdf_base64(RESUME), mime_type="application/pdf",
        )
        _require(len(structured.get("experience") or []) >= 2, "expected 2 experience entries")
        state.setdefault("structured", structured)
        return f"{len(structured['experience'])} roles from the PDF"

    def jd():
        from services.resume_service import ResumeService
        from services.jd_profile import attach_to_analysis
        analysis = attach_to_analysis(ResumeService._analyze_jd_with_llm(JD), JD)
        _require(analysis.get("required_skills"), "no required skills extracted")
        state["jd"] = analysis
        return f"{analysis.get('job_title')!r}, {len(analysis['required_skills'])} required skills"

    def tailor():
        from services.resume_tailor import ResumeTailor
        structured = state.get("structured") or (parse_text() and state["structured"])
        analysis = state.get("jd") or (jd() and state["jd"])
        result = ResumeTailor().tailor(structured, analysis)
        _require(result.get("experience"), "tailored résumé has no experience")
        companies = {e.get("company") for e in result["experience"]}
        _require({"Northwind Analytics", "Contoso Health"} <= companies,
                 f"experience companies changed: {sorted(map(str, companies))}")
        _require(result.get("contact", {}).get("email") == "priya.raman@example.com",
                 "contact email was not restored after masking")
        state["tailored"] = result
        bullets = sum(len(e.get("bullets") or []) for e in result["experience"])
        return f"{len(result['experience'])} roles, {bullets} bullets, contact intact"

    def cover_letter():
        from services.claude_client import claude_json
        tailored = state.get("tailored") or (tailor() and state["tailored"])
        res = claude_json(
            prompt=("Write a 3-paragraph cover letter for this candidate and job. Use only "
                    "facts in the résumé.\nRESUME:\n" + json.dumps(tailored)[:6000]
                    + "\nJOB:\n" + JD),
            model=CLAUDE_PRO, max_tokens=2000,
            schema={"cover_letter": str},
            system='Return JSON: {"cover_letter": "..."}',
        )
        text = res.get("cover_letter", "")
        _require(len(text) > 300, f"cover letter too short ({len(text)} chars)")
        return f"{len(text)} chars"

    def chat():
        tools = [{
            "name": "count_saved_jobs",
            "description": "Return how many jobs the user has saved.",
            "parameters": {"type": "OBJECT", "properties": {}},
        }]
        calls = []

        def execute(name, args):
            calls.append(name)
            return {"saved_jobs": 7}, "jobs"

        reply, _log = provider.tool_call(
            system_text="You are a career assistant. Use tools when they answer the question.",
            user_message="How many jobs have I saved?",
            tools=tools, execute_tool=execute,
        )
        _require(calls == ["count_saved_jobs"] or calls[:1] == ["count_saved_jobs"],
                 f"tool not called (calls={calls})")
        _require("7" in reply, f"reply does not use the tool result: {reply[:200]!r}")
        return reply.strip().replace("\n", " ")[:80]

    steps = {"parse_text": parse_text, "parse_pdf": parse_pdf, "jd": jd,
             "tailor": tailor, "cover_letter": cover_letter, "chat": chat}
    chosen = [s.strip() for s in args.only.split(",")] if args.only else list(steps)
    unknown = [s for s in chosen if s not in steps]
    if unknown:
        print(f"unknown step(s): {unknown}; choose from {list(steps)}")
        return 2

    failed = 0
    for name in chosen:
        t0 = time.time()
        try:
            detail = steps[name]()
            print(f"✅ {name:<12} {time.time() - t0:5.1f}s  {detail}")
        except Exception as e:
            failed += 1
            print(f"❌ {name:<12} {time.time() - t0:5.1f}s  {type(e).__name__}: {str(e)[:300]}")
            if os.getenv("CHECK_AI_TRACEBACK"):
                traceback.print_exc()
    print(f"\n{len(chosen) - failed}/{len(chosen)} passed")
    if failed:
        print("Set CHECK_AI_TRACEBACK=1 for full tracebacks.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
