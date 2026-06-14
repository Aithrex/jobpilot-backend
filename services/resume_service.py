"""
Resume Service — Top-level orchestrator for the resume processing pipeline.

This is the central hub that coordinates all resume-related operations.
It does NOT contain domain logic itself — instead, it delegates to
specialized sub-services for each concern:

  ┌────────────────────────────────────────────────────────────────────┐
  │                      ResumeService (orchestrator)                 │
  │                                                                   │
  │   Manages:                                                        │
  │     - Async job lifecycle (create → poll → complete/fail)         │
  │     - JD extraction (short, self-contained LLM call)               │
  │     - Lambda / background-thread dispatch                         │
  │                                                                   │
  │   Delegates to:                                                   │
  │     ├─ ResumeParser:      PDF extraction + structured parsing     │
  │     ├─ ResumeTailor:      resume ↔ JD alignment                   │
  │     ├─ ResumeScorer:      hybrid ATS scoring                      │
  │     ├─ ResumeRenderer:    PDF/DOCX generation                     │
  │     ├─ KeywordGapEngine:  missing keyword analysis                │
  │     ├─ ImpactEngine:      bullet quality assessment               │
  │     └─ ProjectGenerator:  grounded project generation             │
  └────────────────────────────────────────────────────────────────────┘

Async pattern:
  1. Client POSTs to /upload → creates a job (status: "processing"), returns job_id.
  2. Backend dispatches the job to a background thread (local) or Lambda (prod).
  3. Client polls GET /job/<id> until status is "completed" or "failed".
  4. On completion, the result dict is stored in MongoDB and returned.
"""

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from utils.db_connect import DBConnect

logger = logging.getLogger(__name__)


class ResumeService:
    """Central orchestrator for resume processing.

    Manages the async job lifecycle and delegates domain-specific work
    to specialized sub-services. Instantiated as a singleton via
    get_resume_service().
    """

    def __init__(self):
        """Initialize with all sub-services.

        Uses lazy imports to avoid circular dependencies between service modules.
        Each sub-service is responsible for one domain concern.
        """
        db = DBConnect().get_db()
        self.resume_jobs = db.resume_jobs

        # Domain sub-services (lazy-imported to avoid circular deps)
        from services.resume_parser import ResumeParser
        from services.resume_tailor import ResumeTailor
        from services.resume_scorer import ResumeScorer
        from services.resume_renderer import ResumeRenderer
        from services.keyword_gap_engine import KeywordGapEngine
        from services.impact_engine import ImpactEngine
        from services.project_generator import ProjectGenerator
        from services.content_augmenter import ContentAugmenter

        self.parser = ResumeParser(db)
        self.tailor = ResumeTailor()
        self.scorer = ResumeScorer()
        self.renderer = ResumeRenderer()
        self.keyword_gap = KeywordGapEngine()
        self.impact = ImpactEngine()
        self.project_generator = ProjectGenerator()
        self.augmenter = ContentAugmenter(self.renderer, self.project_generator)

    # ------------------------------------------------------------------
    # Job management (async pattern for Lambda)
    # ------------------------------------------------------------------

    def create_job(self, job_type: str, payload: dict, user_email: str = "") -> str:
        """Create a pending async job and return its unique ID.

        The job is stored in MongoDB with status='processing'.
        The caller should then dispatch it via invoke_async().

        Args:
            job_type: One of 'upload_parse', 'extract_jd', 'tailor', 'ats_scores'.
            payload: Job-type-specific data (e.g., raw_text, jd_analysis).
            user_email: Authenticated user's email to scope job polling.

        Returns:
            UUID string identifying the job.
        """
        job_id = str(uuid.uuid4())
        self.resume_jobs.insert_one(
            {
                "job_id": job_id,
                "job_type": job_type,
                "status": "processing",
                "payload": payload,
                "user_email": user_email,
                "result": None,
                "error": None,
                "created_at": datetime.now(timezone.utc),
                "completed_at": None,
            }
        )
        return job_id

    def get_job(self, job_id: str, user_email: str = "") -> Optional[Dict[str, Any]]:
        """Retrieve a job's current status and result for client polling.

        The payload field is stripped from the response to avoid sending
        large data (e.g., base64 PDFs) back to the client.

        Args:
            job_id: UUID of the job to check.
            user_email: Authenticated user's email to scope job polling.

        Returns:
            Job dict with status/result/error, or None if not found.
        """
        query: Dict[str, Any] = {"job_id": job_id}
        if user_email:
            query["user_email"] = user_email
        job = self.resume_jobs.find_one(query)
        if not job:
            return None
        job.pop("_id", None)
        job.pop("payload", None)  # Don't send payload back to client
        return job

    def complete_job(self, job_id: str, result: dict):
        """Mark a job as completed with its result dict."""
        self.resume_jobs.update_one(
            {"job_id": job_id},
            {
                "$set": {
                    "status": "completed",
                    "result": result,
                    "completed_at": datetime.now(timezone.utc),
                }
            },
        )

    def update_job_partial(self, job_id: str, partial_result: dict) -> None:
        """Write an in-progress partial result for streaming polls.

        Leaves status at "processing" so the client keeps polling, but
        refreshes `result` so each poll can render whatever has arrived
        (e.g. JSearch jobs before LinkedIn's scraper finishes).
        """
        try:
            self.resume_jobs.update_one(
                {"job_id": job_id, "status": {"$ne": "completed"}},
                {"$set": {"result": partial_result}},
            )
        except Exception as e:
            logger.warning(f"Partial update for job {job_id} failed: {e}")

    def fail_job(self, job_id: str, error: str, error_code: Optional[str] = None):
        """Mark a job as failed with an error message and optional machine-readable code."""
        update: Dict[str, Any] = {
            "status": "failed",
            "error": error,
            "completed_at": datetime.now(timezone.utc),
        }
        if error_code:
            update["error_code"] = error_code
        self.resume_jobs.update_one({"job_id": job_id}, {"$set": update})

    @staticmethod
    def invoke_async(job_id: str, job_type: str, payload: dict):
        """Dispatch a job for background processing.

        In production (AWS Lambda): uses boto3 to invoke the same Lambda
        function asynchronously via InvocationType='Event'.

        In local dev: spawns a daemon thread to process the job immediately.

        Args:
            job_id: UUID of the job to process.
            job_type: The pipeline step to execute.
            payload: Job-specific data.
        """
        func_name = os.environ.get("AWS_LAMBDA_FUNCTION_NAME")
        if not func_name:
            import threading

            t = threading.Thread(
                target=_process_job,
                args=(job_id, job_type, payload),
                daemon=True,
            )
            t.start()
            logger.info(f"Async job {job_id} ({job_type}) dispatched to background thread")
            return

        import boto3

        client = boto3.client("lambda", region_name=os.environ.get("AWS_REGION", "us-east-1"))
        client.invoke(
            FunctionName=func_name,
            InvocationType="Event",  # async — returns immediately
            Payload=json.dumps(
                {
                    "async_job": True,
                    "job_id": job_id,
                    "job_type": job_type,
                    "payload": payload,
                }
            ),
        )
        logger.info(f"Async job {job_id} ({job_type}) dispatched to Lambda")

    # ------------------------------------------------------------------
    # Resume retrieval (delegates to parser)
    # ------------------------------------------------------------------

    def get_base_resume(self, user_email: str = "") -> Optional[Dict[str, Any]]:
        """Retrieve the latest stored resume for the given user."""
        return self.parser.get_structured_resume(user_email=user_email)

    # ------------------------------------------------------------------
    # Step 1 — Extract JD fields (stays here — short, self-contained)
    # ------------------------------------------------------------------

    def extract_jd(self, jd_text: str) -> Dict[str, Any]:
        """Parse a job description into structured fields using the LLM (PRO tier).

        Extracts: job title, company, location, required/preferred skills,
        responsibilities, qualifications, experience requirements, industry,
        and ATS-critical keywords.

        All skill and keyword lists are normalized to canonical forms.

        Args:
            jd_text: Raw job description text (max 5000 chars sent to AI).

        Returns:
            Validated dict conforming to JD_ANALYSIS_SCHEMA.
        """
        from services.gemini_client import gemini_json, GEMINI_PRO
        from schemas.resume_schemas import JD_ANALYSIS_SCHEMA, validate_and_coerce

        prompt = (
            "You are an expert recruiter and ATS specialist.\n"
            "Extract structured information from the following job description.\n"
            "Return a JSON object with EXACTLY these keys:\n"
            '- "job_title": string (the specific role title)\n'
            '- "company": string (company name, or "Not specified")\n'
            '- "location": string (location or "Not specified")\n'
            '- "employment_type": string (Full-time/Part-time/Contract/Intern)\n'
            '- "required_skills": list of strings (must-have technical skills)\n'
            '- "preferred_skills": list of strings (nice-to-have skills)\n'
            '- "responsibilities": list of strings (key job duties, max 8)\n'
            '- "qualifications": list of strings (degree, years of experience, etc.)\n'
            '- "experience_years": string (e.g. "3-5 years" or "Entry level")\n'
            '- "industry": string (e.g. "Cloud Computing", "FinTech")\n'
            '- "keywords": list of strings (ATS-critical keywords from the JD)\n\n'
            f"=== JOB DESCRIPTION ===\n{jd_text[:5000]}"
        )

        result = gemini_json(prompt, max_tokens=12000, model=GEMINI_PRO, schema=JD_ANALYSIS_SCHEMA)
        validated = validate_and_coerce(result, JD_ANALYSIS_SCHEMA)

        # Normalize extracted keywords to canonical forms
        from utils.keyword_normalizer import normalize_keywords

        validated["required_skills"] = normalize_keywords(validated.get("required_skills", []))
        validated["preferred_skills"] = normalize_keywords(validated.get("preferred_skills", []))
        validated["keywords"] = normalize_keywords(validated.get("keywords", []))

        return validated


# ---------------------------------------------------------------------------
# Async job processor (called by Lambda async invocation or background thread)
# ---------------------------------------------------------------------------


def _attach_ats_keyword_audit(result: Dict[str, Any], jd_analysis: Dict[str, Any]) -> None:
    """Re-run the deterministic keyword gap engine on the FINAL tailored
    resume so the UI can warn about JD keywords that didn't make it in.

    The LLM occasionally paraphrases tokens (e.g. "Amazon Web Services" in
    place of the literal "AWS" the JD asked for); enterprise ATS screeners
    match exact strings. We attach the result as `ats_keyword_audit` so the
    tailor modal can show a "X keywords missed" badge — passive, not
    blocking. Failure here must not break the tailor pipeline.
    """
    try:
        from services.keyword_gap_engine import KeywordGapEngine
        audit = KeywordGapEngine().analyze(result, jd_analysis or {})
        result["ats_keyword_audit"] = {
            "matched": audit.get("matched_keywords", []),
            "missing": audit.get("missing_keywords", []),
            "partial": audit.get("partial_matches", []),
            "coverage_percentage": audit.get("coverage_percentage", 0),
            "required_missing": audit.get("required_missing", []),
            "required_coverage": audit.get("required_coverage", 0),
        }
        missed = result["ats_keyword_audit"]["required_missing"]
        if missed:
            logger.warning(
                "ATS audit: %d required JD keyword(s) NOT in tailored resume — %s",
                len(missed), missed[:8],
            )
    except Exception as e:
        logger.warning("ATS audit skipped: %s", e)


def _consume_projects_diagnostic(result: Dict[str, Any]) -> None:
    """Pop the augmenter's internal diagnostic, log a structured summary,
    and attach it under `result["_diagnostics"]["projects"]` so the job
    status response can surface why a Projects section ended up empty.

    The diagnostic dict is removed from the top-level tailored payload so
    the renderer never sees it.
    """
    diag = result.pop("_projects_diagnostic", None)
    if not diag:
        return
    final_count = diag.get("final_count", 0)
    stage = diag.get("stage_reached", "unknown")
    budget = diag.get("budget_shape")
    rejections = diag.get("rejections", []) or []
    logger.warning(
        "PROJECTS DIAGNOSTIC: stage=%s final_count=%d budget=%s "
        "generator_returned=%d rejections=%d defensive_stripped=%s deep_skip=%s",
        stage, final_count, budget,
        diag.get("generator_returned", 0),
        len(rejections),
        diag.get("defensive_stripped", []),
        diag.get("deep_project_skip", False),
    )
    if rejections:
        for r in rejections[:10]:
            logger.warning("  rejection: %s -> %s", r.get("name"), r.get("reason"))
    result.setdefault("_diagnostics", {})["projects"] = diag


def _process_job(job_id: str, job_type: str, payload: dict):
    """Execute the appropriate pipeline step and store the result in MongoDB."""
    svc = get_resume_service()
    try:
        if job_type == "gmail_sync":
            # Gmail sync can scan dozens of messages and run an LLM classifier
            # per match — easily blows past API Gateway's 29s. Done async so the
            # HTTP route can return 202 immediately and the UI polls /job/<id>.
            from services.gmail_service import sync_user
            user_email = payload["user_email"]
            summary = sync_user(user_email)
            svc.complete_job(job_id, summary)
            return

        if job_type == "extract_jd":
            result = svc.extract_jd(payload["job_description"])
            svc.complete_job(job_id, {"jd_analysis": result})

        elif job_type == "upload_parse":
            user_email = payload.get("user_email", "")
            content_hash = payload.get("content_hash", "")
            # file_base64 + mime_type for multi-modal LLM input (new path)
            # Falls back to pdf_base64 for backward compatibility
            file_base64 = payload.get("file_base64") or payload.get("pdf_base64")
            mime_type = payload.get("mime_type", "application/pdf")
            input_raw_text = payload.get("raw_text", "")

            # parse_to_structured validates internally via validate_and_coerce.
            # If the LLM returns fundamentally broken output, attempt one repair.
            try:
                validated, raw_text = svc.parser.parse_to_structured(
                    input_raw_text, file_base64, mime_type
                )
            except (ValueError, KeyError) as ve:
                logger.warning("Upload parse validation failed, attempting repair: %s", ve)
                # Try to get raw LLM output for repair (re-parse without validation)
                validated = _repair_extraction(input_raw_text, {}, str(ve))
                raw_text = input_raw_text
                if validated is None:
                    svc.fail_job(job_id, f"Resume parsing failed validation: {ve}")
                    return

            # Validate that the LLM extracted meaningful content
            if len(raw_text.strip()) < 50:
                svc.fail_job(
                    job_id,
                    "Could not extract meaningful text from the document. "
                    "The file may be a scanned image or empty.",
                )
                return

            # Store in DB
            doc = svc.parser.save_parsed_resume(
                validated, raw_text, user_email=user_email, content_hash=content_hash
            )
            svc.complete_job(job_id, {"parsed_resume": doc})

        elif job_type == "tailor":
            user_email = payload.get("user_email", "")
            resume = svc.parser.ensure_structured_resume(user_email=user_email)
            if not resume:
                svc.fail_job(
                    job_id,
                    "No parsed resume found. Please re-upload your resume on the My Resumes tab and try again.",
                )
                return

            # ensure_structured_resume guarantees resume has a populated
            # `structured` field (re-parsing from base S3 file if needed).
            structured = resume["structured"]
            raw_text = resume.get("raw_text", "")
            if raw_text:
                from services.resume_parser import ResumeParser

                # Backfill individual empty contact fields (phone, name, etc.)
                contact = structured.get("contact", {})
                contact_fields = ("name", "email", "phone", "linkedin", "github")
                missing_contact = [f for f in contact_fields if not contact.get(f, "").strip()]
                if missing_contact:
                    structured = ResumeParser._backfill_contact(structured, raw_text)
                    logger.info("Tailor: backfilled missing contact fields: %s", missing_contact)

                # Backfill empty education institution names
                edu_missing = any(
                    not edu.get("institution", "").strip()
                    for edu in structured.get("education", [])
                )
                if edu_missing:
                    structured = ResumeParser._backfill_education(structured, raw_text)
                    logger.info("Tailor: backfilled missing education institutions")

            result = svc.tailor.tailor(structured, payload["jd_analysis"])

            # Normalize contact whitespace + proper-case the name
            # (safety net for resumes parsed before name-normalization was added).
            result_contact = result.get("contact", {})
            if result_contact.get("name"):
                from services.resume_parser import ResumeParser as _RP
                result_contact["name"] = _RP._normalize_name(
                    " ".join(result_contact["name"].split())
                )
            if result_contact.get("phone"):
                result_contact["phone"] = " ".join(result_contact["phone"].split())

            # Content augmentation: page-fill, impact injection, ATS hardening
            result = svc.augmenter.augment(result, structured, payload["jd_analysis"])
            _consume_projects_diagnostic(result)

            # Date normalization: consistent "Month YYYY – Present" format
            from services.date_normalizer import normalize_dates
            from services.title_normalizer import normalize_titles

            result = normalize_dates(result)
            result = normalize_titles(result)

            _attach_ats_keyword_audit(result, payload["jd_analysis"])
            svc.complete_job(job_id, {"tailored_resume": result})

        elif job_type == "regenerate":
            user_email = payload.get("user_email", "")
            resume = svc.parser.ensure_structured_resume(user_email=user_email)
            if not resume:
                svc.fail_job(
                    job_id, "No parsed resume found. Please re-upload your resume and try again."
                )
                return

            structured = resume.get("structured")
            if not structured:
                svc.fail_job(job_id, "No structured resume found. Please re-upload.")
                return

            result = svc.tailor.regenerate(
                structured,
                payload["tailored_resume"],
                payload["jd_analysis"],
                payload["user_feedback"],
            )

            # Content augmentation: refill the page after user-feedback regeneration
            # (same pipeline as initial tailor — bullet expansion, impact injection,
            # project generation, ATS hardening). Without this, feedback that shrinks
            # content leaves empty space on the rendered page.
            result = svc.augmenter.augment(result, structured, payload["jd_analysis"])
            _consume_projects_diagnostic(result)

            # Date + title normalization
            from services.date_normalizer import normalize_dates
            from services.title_normalizer import normalize_titles

            result = normalize_dates(result)
            result = normalize_titles(result)

            _attach_ats_keyword_audit(result, payload["jd_analysis"])
            svc.complete_job(job_id, {"tailored_resume": result})

        elif job_type == "cover_letter":
            import json as _json
            from services.gemini_client import gemini_json, GEMINI_PRO

            tailored = payload["tailored_resume"]
            jd = payload["jd_analysis"]

            resume_summary = tailored.get("summary", "")
            contact = tailored.get("contact", {})
            name = contact.get("name", "Applicant")
            skills = []
            for cat_skills in tailored.get("skills", {}).values():
                skills.extend(cat_skills if isinstance(cat_skills, list) else [])
            top_skills = ", ".join(skills[:10])

            experience_summary = ""
            for exp in tailored.get("experience", [])[:2]:
                title = exp.get("title", "")
                company = exp.get("company", "")
                bullets = exp.get("bullets", [])[:2]
                if title and company:
                    experience_summary += f"- {title} at {company}: {'; '.join(bullets)}\n"

            prompt = (
                "You are a professional cover letter writer. Generate a compelling, personalized "
                "cover letter based on the candidate's resume and the job description.\n\n"
                f"CANDIDATE: {name}\n"
                f"TARGET ROLE: {jd.get('job_title', 'the position')}"
                f"{' at ' + jd.get('company') if jd.get('company') and jd.get('company') != 'Not specified' else ''}\n"
                f"KEY SKILLS: {top_skills}\n"
                f"SUMMARY: {resume_summary}\n"
                f"RECENT EXPERIENCE:\n{experience_summary}\n"
                f"JD REQUIREMENTS: {', '.join(jd.get('required_skills', []))}\n"
                f"JD RESPONSIBILITIES: {'; '.join(jd.get('responsibilities', [])[:5])}\n\n"
                "RULES:\n"
                "1. Write 3-4 paragraphs (250-400 words total).\n"
                "2. Opening: Express genuine interest in the specific role and company.\n"
                "3. Body: Connect 2-3 specific experiences/skills to JD requirements. Use concrete examples.\n"
                "4. Closing: Reiterate enthusiasm and readiness. Include a call to action.\n"
                "5. Tone: Professional but personable. No buzzwords or cliches.\n"
                "6. Do NOT fabricate experiences or skills not in the resume.\n"
                "7. Do NOT use phrases like 'I am writing to apply', 'I believe I am a strong candidate', "
                "'I am excited to bring my skills'. Be direct and specific.\n\n"
                'Return JSON: {"cover_letter": "The full cover letter text with paragraph breaks as \\n\\n"}'
            )

            result = gemini_json(prompt, max_tokens=12000, temperature=0.5, model=GEMINI_PRO)
            cover_text = result.get("cover_letter", "")

            svc.complete_job(job_id, {"cover_letter": cover_text})

        elif job_type == "extract_and_tailor":
            # Combined single-tailor flow: extract JD analysis, then tailor +
            # project generation in PARALLEL (project gen reads the original
            # resume only — independent of tailored output), then augmenter
            # (which naturally skips Phase 1 because projects are pre-injected
            # to the cap), then normalization.
            #
            # Saves ~22s vs separate /extract-jd + /tailor jobs (no inter-job
            # HTTP round-trip + poll lag) and ~10s vs sequential project gen.
            # No accuracy change — same LLM calls in different orchestration.
            from concurrent.futures import (
                ThreadPoolExecutor,
                TimeoutError as FuturesTimeoutError,
            )

            user_email = payload.get("user_email", "")
            jd_text = payload.get("job_description", "")
            if not jd_text:
                svc.fail_job(job_id, "job_description is required")
                return

            # Step 1: Extract JD analysis (sequential — required input for tailor)
            jd_analysis = svc.extract_jd(jd_text)

            # Step 2: Load the user's structured resume (re-parsing from S3 if needed)
            resume = svc.parser.ensure_structured_resume(user_email=user_email)
            if not resume or not resume.get("structured"):
                svc.fail_job(
                    job_id,
                    "No parsed resume found. Please re-upload your resume on the "
                    "My Resumes tab and try again.",
                )
                return

            structured = resume["structured"]
            raw_text = resume.get("raw_text", "")
            if raw_text:
                from services.resume_parser import ResumeParser

                contact = structured.get("contact", {})
                contact_fields = ("name", "email", "phone", "linkedin", "github")
                missing_contact = [
                    f for f in contact_fields if not contact.get(f, "").strip()
                ]
                if missing_contact:
                    structured = ResumeParser._backfill_contact(structured, raw_text)

                edu_missing = any(
                    not edu.get("institution", "").strip()
                    for edu in structured.get("education", [])
                )
                if edu_missing:
                    structured = ResumeParser._backfill_education(structured, raw_text)

            # Step 3: PARALLEL — tailor + (optional) project generation.
            # Project generation only runs if the original resume has fewer
            # than the cap of projects (the augmenter will need them anyway).
            # Reading `original` only — never depends on tailor's output.
            from services.content_augmenter import _MAX_PROJECTS as _PROJECT_CAP

            existing_project_count = len(structured.get("projects", []))
            projects_needed = max(0, _PROJECT_CAP - existing_project_count)

            executor = ThreadPoolExecutor(max_workers=2)
            try:
                future_tailor = executor.submit(
                    svc.tailor.tailor, structured, jd_analysis
                )
                future_projects = None
                if projects_needed > 0:
                    future_projects = executor.submit(
                        svc.project_generator.generate_batch,
                        projects_needed,
                        structured,
                        jd_analysis,
                        structured.get("projects", []),
                    )

                # Wait for tailor (the longer call). Fail loudly if tailor errors.
                try:
                    result = future_tailor.result(timeout=120)
                except Exception as e:
                    logger.error("extract_and_tailor: tailor failed: %s", e)
                    svc.fail_job(job_id, f"Tailoring failed: {e}")
                    return

                # Best-effort: collect parallel project gen. If it errored or
                # timed out, we let the augmenter fall back to sequential
                # project generation later. Either way, the user still gets
                # a resume — only difference is wall time.
                parallel_projects = []
                if future_projects is not None:
                    try:
                        parallel_projects = future_projects.result(timeout=60)
                    except FuturesTimeoutError:
                        logger.warning(
                            "extract_and_tailor: parallel project gen timed out — "
                            "augmenter will retry sequentially"
                        )
                        parallel_projects = []
                    except Exception as e:
                        logger.warning(
                            "extract_and_tailor: parallel project gen failed (%s) — "
                            "augmenter will retry sequentially", e
                        )
                        parallel_projects = []
            finally:
                # Don't wait for orphan threads (same pattern as ATS hardening fix).
                executor.shutdown(wait=False)

            # Step 4: Inject parallel-generated projects into tailored result.
            # Augmenter's Phase 1 will skip if we've reached the cap.
            if parallel_projects:
                tailored_projects = list(result.get("projects") or [])
                for proj in parallel_projects:
                    if len(tailored_projects) >= _PROJECT_CAP:
                        break
                    if not svc.augmenter._is_duplicate_project(proj, tailored_projects):
                        tailored_projects.append(proj)
                result["projects"] = tailored_projects
                logger.info(
                    "extract_and_tailor: pre-injected %d parallel projects (final count %d)",
                    len(parallel_projects),
                    len(tailored_projects),
                )

            # Step 5: Same post-tailor pipeline as the legacy `tailor` job.
            result_contact = result.get("contact", {})
            if result_contact.get("name"):
                from services.resume_parser import ResumeParser as _RP
                result_contact["name"] = _RP._normalize_name(
                    " ".join(result_contact["name"].split())
                )
            if result_contact.get("phone"):
                result_contact["phone"] = " ".join(result_contact["phone"].split())

            result = svc.augmenter.augment(result, structured, jd_analysis)
            _consume_projects_diagnostic(result)

            from services.date_normalizer import normalize_dates
            from services.title_normalizer import normalize_titles

            result = normalize_dates(result)
            result = normalize_titles(result)

            _attach_ats_keyword_audit(result, jd_analysis)
            svc.complete_job(
                job_id,
                {
                    "tailored_resume": result,
                    "jd_analysis": jd_analysis,
                },
            )

        elif job_type == "batch_tailor_item":
            # Combined: extract JD → tailor resume (single job for batch efficiency)
            user_email = payload.get("user_email", "")
            jd_text = payload.get("jd_text", "")
            source_job_id = (payload.get("source_job_id") or "").strip() or None

            # Step 1: Extract JD
            jd_analysis = svc.extract_jd(jd_text)

            # Step 2: Get structured resume
            resume = svc.parser.ensure_structured_resume(user_email=user_email)
            if not resume or not resume.get("structured"):
                svc.fail_job(
                    job_id, "No parsed resume found. Please re-upload your resume and try again."
                )
                return
            structured = resume["structured"]

            # Step 3: Tailor
            result = svc.tailor.tailor(structured, jd_analysis)

            # Step 4: Augment
            result = svc.augmenter.augment(result, structured, jd_analysis)
            _consume_projects_diagnostic(result)

            # Step 5: Date + title normalization
            from services.date_normalizer import normalize_dates
            from services.title_normalizer import normalize_titles

            result = normalize_dates(result)
            result = normalize_titles(result)

            _attach_ats_keyword_audit(result, jd_analysis)

            # ── Wire the batch result into the same lifecycle as a single-row
            # tailor — create a tailoring_record (so it surfaces in My Resumes /
            # Tailored / Applications), record a streak increment, and mark the
            # source saved_job as applied so the Applications kanban moves it
            # off "Interested". Previously the batch flow only stored the result
            # in the jobs table → invisible to Applications/Streak.
            record_id_for_response = None
            try:
                import uuid as _uuid
                from services.resume_versioning import build_initial_version
                from utils.db_connect import DBConnect
                _db = DBConnect().get_db()
                rid = str(_uuid.uuid4())
                initial = build_initial_version(result, source="initial", ats_scores=None)
                # Batch tailoring no longer auto-marks as "applied" — that was
                # too presumptuous. The user might tailor 10 jobs in one batch
                # but only actually submit 3. Status starts at "interested"
                # with a confirmation_pending flag so the Applications board
                # can surface an "Did you apply?" prompt for each one.
                record_doc = {
                    "record_id": rid,
                    "user_email": user_email,
                    "base_resume_filename": resume.get("filename") or "",
                    "base_resume_s3_key": resume.get("s3_key") or "",
                    "jd_text": (jd_text or "")[:15000],
                    "jd_analysis": jd_analysis,
                    "tailored_resume": result,
                    "versions": [initial],
                    "current_version_id": initial["version_id"],
                    "ats_scores": None,
                    "created_at": datetime.now(timezone.utc),
                    "updated_at": datetime.now(timezone.utc),
                    "source": "batch_tailor",
                    "source_job_id": source_job_id,
                    "application": {
                        "status": "interested",
                        "applied_at": None,
                        "updated_at": datetime.now(timezone.utc),
                        # Drives the "Did you apply? [I applied / Not yet /
                        # Dismiss]" prompt on the Applications board. Cleared
                        # by any of the three button clicks.
                        "batch_confirmation_pending": True,
                    },
                }
                _db.tailoring_records.insert_one(record_doc)
                record_id_for_response = rid
                logger.info(
                    "batch_tailor_item: saved tailoring_record %s for %s (source_job_id=%s) — pending user confirmation",
                    rid, user_email, source_job_id,
                )
            except Exception as e:
                logger.warning("batch_tailor_item: failed to save tailoring_record: %s", e)

            # Streak does NOT increment here — only when the user explicitly
            # confirms "I applied" via the board prompt. Tailoring alone
            # isn't an application.

            # Source saved_job stays at "interested". Flipping to "applied"
            # only happens when the user confirms via the Applications board.

            svc.complete_job(
                job_id,
                {
                    "tailored_resume": result,
                    "jd_analysis": jd_analysis,
                    "record_id": record_id_for_response,
                    "source_job_id": source_job_id,
                },
            )

        elif job_type == "ats_scores":
            # Render the PDF too so the scorer can run the parseability
            # check — extracts text via pypdf and verifies real-ATS-style
            # parsing (contact, sections, glyph integrity).
            try:
                pdf_bytes = svc.renderer.generate_pdf(payload["tailored_resume"])
            except Exception as e:
                logger.warning("ATS scoring: PDF render failed, parseability skipped: %s", e)
                pdf_bytes = None
            result = svc.scorer.score(
                payload["tailored_resume"],
                payload["jd_analysis"],
                pdf_bytes=pdf_bytes,
            )
            svc.complete_job(job_id, {"ats_scores": result})

        elif job_type == "job_search":
            from services.job_service import get_job_service

            job_svc = get_job_service()
            mode = payload.get("mode", "batch")
            common = {
                "location": payload.get("location", ""),
                "date_posted": payload.get("date_posted", "today"),
                "remote_only": bool(payload.get("remote_only", False)),
                "employment_type": payload.get("employment_type", ""),
                "h1b_only": bool(payload.get("h1b_only", False)),
                "visa_or_contract": bool(payload.get("visa_or_contract", False)),
                "experience_level": payload.get("experience_level", ""),
                "source": payload.get("source", "all"),
                "include_company_careers": bool(payload.get("include_company_careers", True)),
                "use_resume_recommendations": bool(payload.get("use_resume_recommendations", True)),
                "force_refresh": bool(payload.get("force_refresh", False)),
                "user_email": payload.get("user_email", ""),
            }

            def _stream_partial(partial: dict) -> None:
                # Preserve page context from the request when streaming.
                partial = dict(partial)
                if mode == "single":
                    partial["page"] = int(payload.get("page", 1) or 1)
                svc.update_job_partial(job_id, partial)

            if mode == "single":
                result = job_svc.search_jobs(
                    query=payload.get("query", ""),
                    page=int(payload.get("page", 1) or 1),
                    partial_cb=_stream_partial,
                    **common,
                )
            else:
                result = job_svc.batch_search_jobs(
                    queries=payload.get("queries", []),
                    partial_cb=_stream_partial,
                    **common,
                )
            svc.complete_job(job_id, result)

        elif job_type == "daily_pipeline":
            # Idempotency guard — Lambda async invocations are at-least-once,
            # so AWS may retry the same job after a transient failure or long
            # run. Re-running would duplicate paid Apify scrapes for the same
            # logical pipeline. Bail out if this job already completed.
            existing = svc.get_job(job_id, user_email=payload.get("user_email", ""))
            if existing and existing.get("status") == "completed":
                logger.info(f"Skipping daily_pipeline job {job_id}: already completed")
                return

            from services.daily_pipeline_service import run_pipeline
            from services.job_service import get_job_service

            user_email = payload.get("user_email", "")
            user_key = get_job_service().get_user_apify_key(user_email) if user_email else None

            result = run_pipeline(
                linkedin_keywords=payload.get("linkedin_keywords"),
                workday_titles=payload.get("workday_titles"),
                past_days=int(payload.get("past_days", 1) or 1),
                custom_role_terms=payload.get("custom_role_terms"),
                linkedin_count=int(payload.get("linkedin_count", 80) or 80),
                workday_limit=int(payload.get("workday_limit", 200) or 200),
                include_indeed=bool(payload.get("include_indeed", False)),
                apify_token=user_key,
                user_email=user_email,
                location=str(payload.get("location") or "United States"),
                experience_level=str(payload.get("experience_level") or "entry"),
                employment_type=str(payload.get("employment_type") or "FULLTIME"),
                work_arrangement=str(payload.get("work_arrangement") or "any"),
                domain_strict=bool(payload.get("domain_strict", False)),
                h1b_only=bool(payload.get("h1b_only", False)),
                exclude_no_sponsorship=bool(payload.get("exclude_no_sponsorship", False)),
                hide_companies=payload.get("hide_companies") or [],
                hide_title_patterns=payload.get("hide_title_patterns") or [],
                max_per_company=int(payload.get("max_per_company", 4) or 4),
            )
            # Surface which token tier was used so the UI can display it.
            result["used_user_apify_key"] = bool(user_key)
            svc.complete_job(job_id, result)

        else:
            svc.fail_job(job_id, f"Unknown job type: {job_type}")

        logger.info(f"Job {job_id} ({job_type}) completed successfully")

    except Exception as e:
        from services.gemini_client import LLMRetriesExhaustedError
        logger.error(f"Job {job_id} ({job_type}) failed: {e}")
        code = getattr(e, "code", None) if isinstance(e, LLMRetriesExhaustedError) else None
        svc.fail_job(job_id, str(e), error_code=code)


def _repair_extraction(raw_text: str, invalid_output: dict, error_msg: str):
    """Attempt one bounded repair of invalid extraction output using Preview model.

    Returns the repaired and validated dict, or None if repair fails.
    """
    import json
    from services.gemini_client import gemini_json, GEMINI_PREVIEW
    from schemas.resume_schemas import PARSED_RESUME_SCHEMA, validate_and_coerce

    repair_prompt = (
        "You are a JSON repair assistant. The following resume extraction output "
        "failed schema validation.\n\n"
        f"VALIDATION ERROR:\n{error_msg}\n\n"
        f"INVALID OUTPUT:\n{json.dumps(invalid_output, indent=2)[:6000]}\n\n"
        f"ORIGINAL RESUME TEXT:\n{raw_text[:4000]}\n\n"
        "Fix ONLY the validation errors. Do not change any factual content. "
        "Do not invent or infer any information. Return the corrected JSON object "
        "with the same schema as the original output."
    )

    try:
        result = gemini_json(repair_prompt, max_tokens=24000, temperature=0.2, model=GEMINI_PREVIEW)
        validated = validate_and_coerce(result, PARSED_RESUME_SCHEMA)
        logger.info("Repair extraction succeeded via Preview model")
        return validated
    except Exception as e:
        logger.error("Repair extraction also failed: %s", e)
        return None


def process_async_job(event: dict):
    """Entry point called by lambda_handler for async job events."""
    job_id = event["job_id"]
    job_type = event["job_type"]
    payload = event["payload"]
    logger.info(f"Processing async job {job_id} ({job_type})")
    _process_job(job_id, job_type, payload)


# Singleton
_resume_service: Optional[ResumeService] = None


def get_resume_service() -> ResumeService:
    global _resume_service
    if _resume_service is None:
        _resume_service = ResumeService()
    return _resume_service
