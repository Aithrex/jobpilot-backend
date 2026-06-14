"""
Resume Parser — multi-modal LLM document extraction + structured parsing.

Architecture:
  ┌──────────────┐    ┌──────────────────────────────┐    ┌──────────┐
  │ PDF / DOCX   │───>│ LLM FLASH tier (multi-modal) │───>│ Validated│
  │ file (bytes) │    │  • Structured JSON parsing    │    │  JSON    │
  │              │    │  • Raw text transcription      │    │  + store │
  │              │    │  • Hyperlink URL extraction     │    │          │
  └──────────────┘    └──────────────────────────────┘    └──────────┘

Model routing:
  - FLASH tier (Claude Haiku on Bedrock today): fast, factual extraction —
    no hallucination, no rewriting.
  - Multi-modal mode: sends raw file bytes (PDF or DOCX) directly to the LLM
    for visual document understanding. Superior for complex layouts (columns,
    tables, sidebars) and eliminates the need for separate text extraction
    libraries (PyPDF2, python-docx).

Output conforms to PARSED_RESUME_SCHEMA so downstream consumers
(ResumeTailor, ResumeScorer, ProjectGenerator) receive consistent JSON.

Key design decisions:
  1. Strict extraction prompt — prevents the model from inventing or embellishing.
  2. No native schema= enforcement — PARSED_RESUME_SCHEMA uses _dict_of for skills
     (Record<string, string[]>) which the Gemini response_schema API could not
     handle. Instead, we rely on prompt instructions + post-call
     validate_and_coerce(); this also works cleanly for Claude/Bedrock tool use.
  3. Keyword normalization — standardizes skill names ("JS" → "JavaScript") for
     accurate downstream matching in scoring and gap analysis.
  4. Single-user storage — resumes are replaced (not versioned) in MongoDB.
"""

import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from schemas.resume_schemas import (
    PARSED_RESUME_SCHEMA,
    validate_and_coerce,
    flatten_skills,
    extract_job_titles,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Maximum characters of raw text sent to the LLM (prevents token overflow)
_MAX_TEXT_CHARS = 8000

# Minimum text length to consider a PDF extraction valid
_MIN_VALID_TEXT_LENGTH = 50

# Month abbreviation → number mapping for date parsing
_MONTH_MAP = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}

# Regex patterns compiled once at module level for performance
_YEAR_PATTERN = re.compile(r"(20\d{2}|19\d{2})")
_MONTH_PATTERN = re.compile(r"(?i)\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*")

# Words that indicate a date range extends to the current date
_PRESENT_KEYWORDS = frozenset({"present", "current", "now", "ongoing", "today"})

# Roman numeral suffixes (preserved uppercase)
_NAME_NUMERIC_SUFFIXES = frozenset({"ii", "iii", "iv", "v"})

# Textual suffixes (preserved with leading capital)
_NAME_TEXT_SUFFIXES = frozenset({"jr", "sr"})

# ---------------------------------------------------------------------------
# Extraction prompt template
# ---------------------------------------------------------------------------

_EXTRACTION_PROMPT = (
    "You are a strict resume parser optimized for downstream ATS keyword matching and "
    "recruiter screening. Your ONLY job is to extract information explicitly present in "
    "the resume. NEVER infer, synthesize, or invent anything.\n\n"
    "CRITICAL MANDATORY FIELDS — you MUST extract these if they exist anywhere in the resume:\n"
    "1. CONTACT: full name, email, phone, LinkedIn URL, GitHub URL, location (City, ST), portfolio website URL.\n"
    "   - The name is almost ALWAYS the very first line or the largest text at the top of the resume.\n"
    "   - Look for email patterns like user@domain.com\n"
    "   - Look for phone patterns like (123) 456-7890 or +1-234-567-8901\n"
    "   - For linkedin and github fields, ALWAYS prefer the actual hyperlink URL target\n"
    "     (e.g., https://linkedin.com/in/username) over display text.\n"
    "2. EDUCATION: institution/university name is NEVER optional.\n"
    "   - Also extract: degree, location, dates, GPA, and relevant coursework.\n\n"
    "STRICT EXTRACTION RULES:\n"
    "1. Extract ONLY information explicitly present in the resume. NEVER fabricate or invent.\n"
    "2. Summary: If no summary/objective section exists, return empty string. DO NOT write one.\n"
    "3. Location: If not stated for a job or education entry, use empty string. DO NOT guess.\n"
    "4. Employment type: If not explicitly stated (Full-time, Internship, Contract), use empty string.\n"
    "5. Job title: Copy the EXACT title as written. Do NOT expand abbreviations (keep 'Sr.',\n"
    "   'Jr.', 'Eng.' as written). Do NOT normalize 'Developer' to 'Engineer' or vice versa.\n"
    "   Preserve parentheticals (e.g., 'Cloud Engineer (Associate Data Scientist)').\n"
    "6. Dates: For a CURRENT role (no end date, or shows 'Current'/'Now'/'Ongoing'/'Today'/blank),\n"
    "   format the dates field as 'Month YYYY – Present'. The literal word 'Present' is required;\n"
    "   do NOT use synonyms. ATS scanners fingerprint this exact token.\n"
    "7. Bullet points: STRIP the leading bullet glyph (•, -, *, →, ▪, ■, □, ◦) and any whitespace\n"
    "   that follows it, then keep the rest of the text verbatim including punctuation.\n"
    "   The bullets array MUST NOT contain empty strings.\n"
    "8. Boilerplate: Do NOT include 'References available upon request', page numbers, or\n"
    "   header/footer artifacts in any field.\n"
    "9. If a section is not present in the resume, use an empty string or empty array.\n"
    "10. Every field must be a non-null string or array — never null.\n\n"
    "SKILLS — STRICT VOCABULARY RULES (ATS-critical):\n"
    "A. A 'skill' is a CONCRETE technology, tool, language, framework, library, platform,\n"
    "   database, cloud service, or methodology TOKEN. Examples of valid skills:\n"
    "   Python, Go, gRPC, FastAPI, Kubernetes, ECS, S3, Terraform, PostgreSQL, Kafka, Agile.\n"
    "B. DO NOT extract architectural CONCEPTS as skills. Examples to REJECT as skills:\n"
    "   'microservices', 'distributed systems', 'scalability', 'high availability',\n"
    "   'cloud-native', 'event-driven architecture', 'containerization'. These belong in\n"
    "   the summary or bullets, NOT in skills.\n"
    "C. DO NOT extract SOFT SKILLS as skills (leadership, communication, mentoring,\n"
    "   collaboration). These are not ATS keywords — leave them in summary/bullets.\n"
    "D. PRESERVE THE EXACT TOKEN AS WRITTEN. If the resume says 'AWS' use 'AWS' (not\n"
    "   'Amazon Web Services'). If the resume says 'Postgres' use 'Postgres' (not\n"
    "   'PostgreSQL'). If the resume says 'JS' use 'JS' (not 'JavaScript').\n"
    "E. Each skill must appear in EXACTLY ONE category — no cross-category duplicates.\n"
    "F. Extract skills from ALL sections (not just the Skills heading) — bullets and\n"
    "   summary often contain technologies. Example: 'Built microservices using Go and\n"
    "   gRPC on ECS' → extract 'Go', 'gRPC', 'ECS'. Do NOT extract 'microservices'.\n"
    "G. Categorize into these groups when applicable: Languages, Frameworks & Libraries,\n"
    "   Cloud & DevOps, Databases, Tools & Platforms, Methodologies. Add new groups\n"
    "   only if no existing group fits.\n\n"
    "CERTIFICATIONS — STRICT NO-FABRICATION RULE:\n"
    "- If the resume has a 'Certifications' / 'Certificates' / 'Licenses' section, OR if\n"
    "  certifications are explicitly named anywhere in the resume (e.g., a bullet says\n"
    "  'AWS Certified Solutions Architect – Associate (2023)'), extract every entry as a\n"
    "  string in the certifications array.\n"
    "- Preserve the EXACT name as written, including issuer/year if shown\n"
    "  (e.g., 'AWS Certified Solutions Architect – Associate (2023)').\n"
    "- If NO certifications are mentioned anywhere in the resume, return an EMPTY ARRAY [].\n"
    "- NEVER invent or infer certifications based on skills, education, or job titles.\n"
    "  ('AWS' as a skill is NOT evidence of any AWS certification.)\n\n"
    "Return a JSON object with EXACTLY this structure (ALL top-level keys are REQUIRED):\n"
    "{\n"
    '  "contact": {\n'
    '    "name": "Full Name from resume header",\n'
    '    "email": "email@example.com",\n'
    '    "phone": "(123) 456-7890",\n'
    '    "linkedin": "linkedin.com/in/profile",\n'
    '    "github": "github.com/username",\n'
    '    "location": "City, ST (e.g. Tempe, AZ) — leave empty if not in resume",\n'
    '    "portfolio": "personal website URL e.g. example.com — leave empty if not present"\n'
    "  },\n"
    '  "summary": "Professional summary if present, otherwise empty string",\n'
    '  "skills": {\n'
    '    "Category Name": ["Skill1", "Skill2"]\n'
    "  },\n"
    '  "experience": [\n'
    "    {\n"
    '      "title": "Job Title",\n'
    '      "company": "Company Name",\n'
    '      "location": "City, State",\n'
    '      "dates": "Start Date - End Date",\n'
    '      "type": "Full-time",\n'
    '      "bullets": ["Original bullet text..."]\n'
    "    }\n"
    "  ],\n"
    '  "education": [\n'
    "    {\n"
    '      "degree": "Degree Name",\n'
    '      "institution": "University Name (NEVER leave empty)",\n'
    '      "location": "City, State",\n'
    '      "dates": "Date range",\n'
    '      "gpa": "GPA if listed",\n'
    '      "coursework": "Relevant coursework if listed"\n'
    "    }\n"
    "  ],\n"
    '  "projects": [\n'
    "    {\n"
    '      "name": "Project Name",\n'
    '      "dates": "Date range",\n'
    '      "bullets": ["Description..."],\n'
    '      "tech": "Tech1, Tech2"\n'
    "    }\n"
    "  ],\n"
    '  "certifications": ["AWS Certified Solutions Architect – Associate (2023)", "..."],\n'
    '  "raw_text": "Full verbatim plain-text transcription of all visible text in the document, '
    'preserving line breaks. Include ALL text from every page/section.",\n'
    '  "extracted_urls": ["https://linkedin.com/in/...", "https://github.com/...", "...all '
    'hyperlinked URLs found in the document — actual href targets, not display text"]\n'
    "}\n\n"
)

# Slim variant — used when raw text + hyperlinks have already been extracted
# locally (pypdf for PDFs, python-docx for DOCX). Drops the `raw_text` and
# `extracted_urls` fields, which used to dominate output token cost.
# Saves ~3-4k output tokens per parse, cutting wall time roughly proportionally.
_EXTRACTION_PROMPT_SLIM = (
    "You are a strict resume parser optimized for ATS keyword matching and recruiter "
    "screening. Your ONLY job is to extract information explicitly present in the resume. "
    "NEVER infer, synthesize, or invent anything.\n\n"
    "CRITICAL MANDATORY FIELDS — you MUST extract these if they exist anywhere in the resume:\n"
    "1. CONTACT: full name, email, phone, LinkedIn URL, GitHub URL, location (City, ST), portfolio website URL.\n"
    "   - The name is almost ALWAYS the very first line or the largest text at the top of the resume.\n"
    "   - For linkedin and github, prefer the actual hyperlink URL target over display text.\n"
    "2. EDUCATION: institution/university name is NEVER optional. Also extract degree, location, "
    "dates, GPA, and relevant coursework.\n\n"
    "STRICT EXTRACTION RULES:\n"
    "1. Extract ONLY information explicitly present in the resume. NEVER fabricate or invent.\n"
    "2. Summary: If no summary/objective section exists, return empty string. DO NOT write one.\n"
    "3. Location/Employment type: If not stated, use empty string. DO NOT guess.\n"
    "4. Job title: Copy the EXACT title as written. Do NOT expand abbreviations (keep 'Sr.',\n"
    "   'Jr.', 'Eng.'). Do NOT normalize 'Developer' to 'Engineer' or vice versa. Preserve\n"
    "   parentheticals.\n"
    "5. Dates: For a CURRENT role (no end date, or shows 'Current'/'Now'/'Ongoing'/'Today'/blank),\n"
    "   format dates as 'Month YYYY – Present'. The literal word 'Present' is required;\n"
    "   ATS scanners fingerprint this exact token.\n"
    "6. Bullets: STRIP leading bullet glyphs (•, -, *, →, ▪, ■, □, ◦) plus following whitespace,\n"
    "   then keep the rest verbatim. No empty strings in the bullets array.\n"
    "7. Boilerplate: Do NOT include 'References available upon request', page numbers, or\n"
    "   header/footer artifacts in any field.\n"
    "8. If a section is absent, use an empty string or empty array. Never null.\n\n"
    "SKILLS — STRICT VOCABULARY RULES (ATS-critical):\n"
    "A. A 'skill' is a CONCRETE technology, tool, language, framework, library, platform,\n"
    "   database, cloud service, or methodology TOKEN (e.g., Python, Kubernetes, Terraform,\n"
    "   PostgreSQL, Kafka, Agile).\n"
    "B. REJECT architectural CONCEPTS as skills: 'microservices', 'distributed systems',\n"
    "   'scalability', 'high availability', 'cloud-native', 'containerization'. These belong\n"
    "   in summary/bullets, NOT in skills.\n"
    "C. REJECT SOFT SKILLS as skills (leadership, communication, mentoring). Leave them in\n"
    "   summary/bullets.\n"
    "D. PRESERVE the exact token as written ('AWS' not 'Amazon Web Services'; 'Postgres'\n"
    "   not 'PostgreSQL'; 'JS' not 'JavaScript'). The post-processing normalizer canonicalizes.\n"
    "E. Each skill must appear in EXACTLY ONE category — no cross-category duplicates.\n"
    "F. Extract skills from ALL sections (bullets and summary often contain technologies).\n"
    "G. Categorize into: Languages, Frameworks & Libraries, Cloud & DevOps, Databases,\n"
    "   Tools & Platforms, Methodologies. Add new groups only if no existing group fits.\n\n"
    "CERTIFICATIONS — STRICT NO-FABRICATION RULE:\n"
    "- Extract every entry from a 'Certifications' / 'Certificates' / 'Licenses' section,\n"
    "  OR named explicitly anywhere in the resume (e.g., a bullet 'Earned AWS Certified\n"
    "  Solutions Architect – Associate (2023)').\n"
    "- Preserve the EXACT name as written, including issuer/year if shown.\n"
    "- If NO certifications are mentioned anywhere, return an EMPTY ARRAY [].\n"
    "- NEVER invent or infer certifications from skills, education, or job titles.\n\n"
    "Return a JSON object with EXACTLY this structure (ALL top-level keys REQUIRED):\n"
    "{\n"
    '  "contact": {"name":"","email":"","phone":"","linkedin":"","github":"","location":"","portfolio":""},\n'
    '  "summary": "",\n'
    '  "skills": {"Category Name": ["Skill1", "Skill2"]},\n'
    '  "experience": [{"title":"","company":"","location":"","dates":"","type":"","bullets":[]}],\n'
    '  "education": [{"degree":"","institution":"","location":"","dates":"","gpa":"","coursework":""}],\n'
    '  "projects": [{"name":"","dates":"","bullets":[],"tech":""}],\n'
    '  "certifications": []\n'
    "}\n\n"
    "Do NOT include a raw_text field. Do NOT include an extracted_urls field. Return ONLY the "
    "structured JSON above.\n\n"
)


class ResumeParser:
    """Handles PDF/DOCX → structured JSON parsing and MongoDB storage.

    Uses the LLM (FLASH tier) multi-modal to process documents directly — file bytes
    (PDF or DOCX) are sent to the LLM for visual document understanding,
    structured extraction, and raw text transcription in a single call.

    Usage:
        parser = ResumeParser(db)
        doc = parser.upload_and_parse(pdf_bytes)  # full sync pipeline
        # -- or --
        structured, raw_text = parser.parse_to_structured("", file_b64, mime_type)
        doc = parser.save_parsed_resume(structured, raw_text)  # async path
    """

    def __init__(self, db):
        """Initialize with a MongoDB database instance.

        Args:
            db: pymongo Database object. The parser uses the 'user_resumes' collection.
        """
        self.user_resumes = db.user_resumes

    # ======================================================================
    # 1. FILE VALIDATION
    # ======================================================================

    # MIME types for supported file formats
    MIME_TYPES = {
        "pdf": "application/pdf",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }

    @staticmethod
    def validate_file(file_bytes: bytes, filename: str) -> None:
        """Validate that a file is a supported, non-corrupted PDF or DOCX.

        Performs lightweight structural checks without extracting text content.
        Text extraction is handled by the multi-modal LLM in the async job.

        Args:
            file_bytes: Raw file content as bytes.
            filename: Original filename (used to determine format).

        Raises:
            ValueError: If the file is empty, corrupted, or unsupported.
        """
        if not file_bytes or len(file_bytes) < 10:
            raise ValueError("File is empty or too small to be a valid document.")

        if filename.lower().endswith(".pdf"):
            if not file_bytes[:5].startswith(b"%PDF"):
                raise ValueError("File is not a valid PDF (missing PDF header).")
        elif filename.lower().endswith(".docx"):
            # DOCX files are ZIP archives — check for ZIP magic bytes
            if not file_bytes[:4].startswith(b"PK\x03\x04"):
                raise ValueError("File is not a valid DOCX (missing ZIP header).")
        else:
            raise ValueError("Unsupported file format. Only PDF and DOCX are accepted.")

    @staticmethod
    def get_mime_type(filename: str) -> str:
        """Return the MIME type for a supported file based on its extension."""
        ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
        return ResumeParser.MIME_TYPES.get(ext, ResumeParser.MIME_TYPES["pdf"])

    # ======================================================================
    # 2. GEMINI STRUCTURED PARSING
    # ======================================================================

    def parse_to_structured(
        self,
        raw_text: str = "",
        file_base64: str = None,
        mime_type: str = "application/pdf",
    ) -> tuple:
        """Parse resume content into structured JSON using the LLM (FLASH tier).

        Supports two input modes:
          - Multi-modal (preferred): Sends raw file bytes (PDF or DOCX) directly
            to the LLM for visual document understanding. It also extracts
            raw text and hyperlink URLs in the same call.
          - Text-only (fallback): Uses pre-extracted raw text when file bytes
            aren't available (e.g., legacy uploads).

        Args:
            raw_text: Optional pre-extracted text (used for text-only fallback).
            file_base64: Optional base64-encoded file bytes for multi-modal parsing.
            mime_type: MIME type of the file (PDF or DOCX).

        Returns:
            Tuple of (structured_dict, raw_text_str):
              - structured_dict: Resume data conforming to PARSED_RESUME_SCHEMA.
              - raw_text_str: Full plain-text transcription from the LLM (or input raw_text).
        """
        from services.gemini_client import gemini_json, GEMINI_FLASH, get_active_provider
        import base64
        import time

        provider = get_active_provider()
        t0 = time.perf_counter()

        # ---- Local pre-extraction (text layer + PDF /Annots / DOCX rels) ----
        # Doing this locally cuts the dominant cost (the LLM transcribing the
        # whole document into raw_text) and gives byte-perfect hyperlink targets.
        # If the PDF is image-based or extraction fails, local_text is empty
        # and we transparently fall back to the legacy FULL prompt path.
        local_text = ""
        local_urls: list = []
        local_extract_status = "skipped"  # one of: skipped | ok | unhealthy | error | unavailable
        if file_base64:
            try:
                from services.text_extractor import (
                    extract_text,
                    is_text_healthy,
                    clean_extracted_text,
                )

                file_bytes = base64.b64decode(file_base64)
                local_text_raw, local_urls = extract_text(file_bytes, mime_type)
                local_text_clean = clean_extracted_text(local_text_raw)
                if is_text_healthy(local_text_clean):
                    local_text = local_text_clean
                    local_extract_status = "ok"
                    logger.info(
                        "Resume parse: local extraction OK (%d chars, %d links) — "
                        "using slim LLM prompt",
                        len(local_text),
                        len(local_urls),
                    )
                else:
                    local_extract_status = "unhealthy"
                    logger.warning(
                        "Resume parse: local extraction yielded %d chars (unhealthy) — "
                        "falling back to FULL LLM transcription. "
                        "If this happens often, check pypdf install and PDF text layer.",
                        len(local_text_clean) if local_text_clean else 0,
                    )
            except Exception as e:
                local_extract_status = "error"
                logger.warning("Local pre-extraction failed, falling back to LLM: %s", e)

        t_local = time.perf_counter()
        use_slim = bool(local_text)

        # Build provider-native parts (PDF + prompt for Claude/Gemini, or
        # text-only fallback). Provider knows its own multimodal payload shape.
        parts = self._build_extraction_parts(
            raw_text, file_base64, mime_type, provider, base64, slim=use_slim
        )

        # Call the LLM (FLASH tier). Slim path needs far fewer output tokens because
        # we no longer ask for raw_text or extracted_urls.
        result = gemini_json(
            prompt=None,  # Prompt is embedded in the parts
            parts=parts,
            max_tokens=6000 if use_slim else 24000,
            temperature=0.2,  # Low temp for factual extraction
            model=GEMINI_FLASH,
        )

        t_gemini = time.perf_counter()

        # In slim mode the model is instructed not to emit these, but be
        # defensive: pop them either way so they don't leak into the schema.
        gemini_raw_text = result.pop("raw_text", "") or ""
        gemini_urls = result.pop("extracted_urls", []) or []

        # Decide on the authoritative raw_text + URL set:
        #   - Slim path: local extraction is canonical (byte-perfect text +
        #     /Annots URI targets are more reliable than LLM scraping).
        #   - Full path: the LLM transcribed the document; use that.
        if use_slim:
            effective_raw_text = local_text
            extracted_urls = local_urls
        else:
            effective_raw_text = gemini_raw_text if gemini_raw_text.strip() else raw_text
            extracted_urls = gemini_urls

        # Build synthetic [Extracted Link: ...] markers — preserves the
        # contract for _backfill_contact which greps for these tokens to
        # source authoritative hyperlink hrefs.
        if extracted_urls:
            link_markers = "\n".join(f"[Extracted Link: {url}]" for url in extracted_urls if url)
            effective_raw_text = effective_raw_text + "\n" + link_markers

        # Normalize skill keywords to canonical forms
        # ("JS" → "JavaScript", "k8s" → "Kubernetes", etc.)
        result = self._normalize_skills(result)

        # Backfill contact info from raw text if the LLM failed to extract it
        result = self._backfill_contact(result, effective_raw_text)

        # Backfill education institution from raw text if missing
        result = self._backfill_education(result, effective_raw_text)

        # Clean education fields: strip dates baked into degree, deduplicate segments
        result = self._clean_education_fields(result)

        # Log warnings for any remaining missing critical fields
        self._validate_critical_fields(result)

        # Schema validation: ensure all required keys exist with correct types
        from schemas.resume_schemas import PARSED_RESUME_SCHEMA, validate_and_coerce

        result = validate_and_coerce(result, PARSED_RESUME_SCHEMA)

        # Normalize experience titles and project names to Title Case
        from services.title_normalizer import normalize_titles

        result = normalize_titles(result)

        # ---- Single-line timing summary ----
        # Look for this in production logs to diagnose slow parses. If
        # `path=full` here, the slim optimization was bypassed (most likely
        # cause: pypdf not installed in the runtime, or the PDF has no text
        # layer / encrypted / etc.). If `path=slim` but total > ~30s, the
        # cost is in LLM-side latency, not local extraction.
        t_end = time.perf_counter()
        # NOTE: emitted at WARNING (not INFO) because AWS Lambda's preconfigured
        # root logger swallows INFO-level records from user code — verified by
        # `aws logs filter-log-events --filter-pattern '"[INFO]"'` returning
        # zero hits over 7 days. WARNING is reliably surfaced. Telemetry only,
        # no actual warning.
        logger.warning(
            "RESUME_PARSE_TIMING path=%s local_extract=%s local_ms=%d "
            "gemini_ms=%d post_ms=%d total_ms=%d local_chars=%d local_urls=%d",
            "slim" if use_slim else "full",
            local_extract_status,
            int((t_local - t0) * 1000),
            int((t_gemini - t_local) * 1000),
            int((t_end - t_gemini) * 1000),
            int((t_end - t0) * 1000),
            len(local_text),
            len(local_urls),
        )

        return result, effective_raw_text

    @staticmethod
    def _build_extraction_parts(
        raw_text, file_base64, mime_type, provider, base64, slim: bool = False
    ) -> list:
        """Build provider-native parts for the resume extraction call.

        Multi-modal path (preferred): delegates to `provider.build_pdf_parts`
        which returns either Gemini `types.Part` objects or Claude content
        blocks depending on the active provider.

        Text-only fallback: returns a single text part wrapping the prompt
        with embedded raw text. The provider's `json()` accepts this shape
        for both Gemini and Claude (both treat single text part as the
        equivalent of a plain `prompt=` argument).

        Args:
            raw_text: Plain text from the resume (used for text-only fallback).
            file_base64: Optional base64-encoded file for multi-modal input.
            mime_type: MIME type of the file (e.g., application/pdf).
            provider: The active LLMProvider instance (Gemini or Claude).
            base64: base64 module (passed to avoid re-import).
            slim: If True, use the slim prompt (no raw_text / extracted_urls fields).
                Caller is responsible for sourcing those locally.

        Returns:
            Provider-native parts list — opaque to the caller, passed back to
            `gemini_json(parts=...)` as-is.
        """
        prompt_template = _EXTRACTION_PROMPT_SLIM if slim else _EXTRACTION_PROMPT

        if file_base64:
            file_bytes = base64.b64decode(file_base64)
            tail = (
                "Extract from the attached document."
                if slim
                else (
                    "Extract from the attached document. For any hyperlinked text, "
                    "extract the actual URL target (not the display text) and include "
                    "it in the extracted_urls array."
                )
            )
            return provider.build_pdf_parts(
                prompt_text=prompt_template + tail,
                pdf_bytes=file_bytes,
                mime_type=mime_type,
            )

        # Text-only fallback: provider.json accepts None for `parts` so we
        # could pass `prompt=` instead, but the caller path here always uses
        # parts. Build a single text part using the provider's PDF helper
        # with empty bytes — providers degrade to text-only when no PDF.
        prompt = prompt_template + f"=== RESUME TEXT ===\n{raw_text[:_MAX_TEXT_CHARS]}"
        return provider.build_pdf_parts(
            prompt_text=prompt,
            pdf_bytes=b"",
            mime_type="text/plain",  # signals "no PDF, just text"
        )

    @staticmethod
    def _normalize_skills(result: dict) -> dict:
        """Normalize extracted skill names to canonical forms.

        Applies the project's keyword normalizer to standardize abbreviations
        and aliases (e.g., "JS" → "JavaScript", "k8s" → "Kubernetes").
        This ensures accurate downstream matching in scoring and gap analysis.

        Args:
            result: Parsed resume dict with a 'skills' key.

        Returns:
            The same dict with normalized skill values.
        """
        from utils.keyword_normalizer import normalize_keywords

        skills = result.get("skills", {})
        if isinstance(skills, dict):
            for category in list(skills.keys()):
                if isinstance(skills[category], list):
                    skills[category] = normalize_keywords(skills[category])
            result["skills"] = skills
        return result

    @staticmethod
    def _backfill_contact(result: dict, raw_text: str) -> dict:
        """Backfill empty contact fields from raw text using regex patterns.

        When the LLM omits or partially extracts contact fields, this method
        fills in the gaps using regex patterns for email, phone, LinkedIn,
        GitHub, and a name heuristic (first non-URL/email line).
        """
        contact = result.get("contact", {})
        if not isinstance(contact, dict):
            contact = {}

        # Count how many fields need backfill
        fields_to_fill = [
            f
            for f in ("name", "email", "phone", "linkedin", "github")
            if not contact.get(f, "").strip()
        ]
        if not fields_to_fill:
            # Even if all fields are populated, still run href override + fabrication check
            pass
        else:
            logger.info("Contact backfill: filling missing fields %s from raw text", fields_to_fill)

        # --- Extract [Extracted Link: ...] markers from PDF annotations ---
        # These are actual href URLs from the PDF, more reliable than the LLM's extraction.
        extracted_links = re.findall(r"\[Extracted Link:\s*([^\]]+)\]", raw_text)
        linkedin_href = ""
        github_href = ""
        for link in extracted_links:
            link = link.strip()
            if "linkedin.com" in link.lower() and not linkedin_href:
                linkedin_href = link
                logger.info("Contact backfill: found LinkedIn PDF href: %s", link)
            elif "github.com" in link.lower() and not github_href:
                github_href = link
                logger.info("Contact backfill: found GitHub PDF href: %s", link)

        # Override GitHub immediately from href (authoritative)
        if github_href:
            contact["github"] = github_href

        # For LinkedIn: if the LLM returned a non-URL value (e.g. display text "LinkedIn"),
        # clear it now so regex backfill can try, but the href will override at the end.
        linkedin_val = contact.get("linkedin", "").strip()
        if (
            linkedin_val
            and "linkedin.com" not in linkedin_val.lower()
            and "github.com" not in linkedin_val.lower()
        ):
            contact["linkedin"] = ""
            logger.warning("Contact: cleared non-URL linkedin value '%s'", linkedin_val)

        # --- Email ---
        if not contact.get("email", "").strip():
            email_match = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", raw_text)
            if email_match:
                contact["email"] = email_match.group(0)

        # --- Phone (multiple patterns for international formats) ---
        if not contact.get("phone", "").strip():
            phone_patterns = [
                # US format: (123) 456-7890 or 123-456-7890 or +1 234 567 8901
                r"(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}",
                # International format: +91 12345 67890 or +44 7911 123456
                r"\+\d{1,3}[-.\s]?\d{4,5}[-.\s]?\d{4,6}",
                # General: any sequence of 10+ digits with separators
                r"(?:\+?\d{1,3}[-.\s]?)?\d[\d\s.-]{8,}\d",
            ]
            for pattern in phone_patterns:
                phone_match = re.search(pattern, raw_text)
                if phone_match:
                    phone = phone_match.group(0).strip()
                    # Validate: must have at least 10 digits
                    digits = re.sub(r"\D", "", phone)
                    if len(digits) >= 10:
                        contact["phone"] = phone
                        break

        # --- LinkedIn ---
        if not contact.get("linkedin", "").strip():
            linkedin_match = re.search(
                r"(?:https?://)?(?:www\.)?linkedin\.com/(?:in/)?[\w-]+/?", raw_text, re.IGNORECASE
            )
            if linkedin_match:
                contact["linkedin"] = linkedin_match.group(0)

        # --- GitHub ---
        if not contact.get("github", "").strip():
            github_match = re.search(
                r"(?:https?://)?(?:www\.)?github\.com/[\w-]+/?", raw_text, re.IGNORECASE
            )
            if github_match:
                contact["github"] = github_match.group(0)

        # --- Name (heuristic: first non-empty line that isn't a URL/email/phone) ---
        if not contact.get("name", "").strip():
            for line in raw_text.split("\n"):
                line = line.strip()
                if (
                    line
                    and len(line) < 60
                    and "@" not in line
                    and "http" not in line.lower()
                    and not line.startswith("[")
                    and not re.match(r"^[\d(+]", line)
                    and "linkedin" not in line.lower()
                    and "github" not in line.lower()
                ):
                    contact["name"] = line
                    break

        # Normalize whitespace in name and phone (collapse multiple spaces, strip edges)
        if contact.get("name"):
            contact["name"] = " ".join(contact["name"].split())
        if contact.get("phone"):
            contact["phone"] = " ".join(contact["phone"].split())

        # --- Cross-contamination guard: swap if fields contain the wrong domain ---
        linkedin_val = contact.get("linkedin", "")
        github_val = contact.get("github", "")
        if linkedin_val and "github.com" in linkedin_val.lower():
            if not github_val:
                contact["github"] = linkedin_val
            contact["linkedin"] = ""
            logger.warning(
                "Contact cross-contamination: moved github.com URL from linkedin to github field"
            )
        if github_val and "linkedin.com" in github_val.lower():
            if not linkedin_val:
                contact["linkedin"] = github_val
            contact["github"] = ""
            logger.warning(
                "Contact cross-contamination: moved linkedin.com URL from github to linkedin field"
            )

        # --- FINAL: LinkedIn href is authoritative — override everything ---
        # This runs AFTER all other processing (fabrication, cross-contamination, regex).
        # The PDF annotation URL is the single source of truth for LinkedIn.
        if linkedin_href:
            contact["linkedin"] = linkedin_href
            logger.info(
                "Contact backfill: LinkedIn FINAL override from PDF href: %s", linkedin_href
            )

        # Normalize LinkedIn and GitHub URLs to canonical forms
        if contact.get("linkedin"):
            contact["linkedin"] = ResumeParser._normalize_linkedin(contact["linkedin"])
        if contact.get("github"):
            contact["github"] = ResumeParser._normalize_github(contact["github"])

        # Proper-case the name regardless of source (LLM extract / regex fallback / display text)
        # so "HARSHITH SIDDARDHA MANNE" → "Harshith Siddardha Manne" everywhere.
        if contact.get("name"):
            original_name = contact["name"]
            normalized_name = ResumeParser._normalize_name(original_name)
            if original_name != normalized_name:
                logger.info("Contact: normalized name '%s' → '%s'", original_name, normalized_name)
            contact["name"] = normalized_name

        result["contact"] = contact
        return result

    @staticmethod
    def _normalize_name(name: str) -> str:
        """Convert a person's name to proper Title Case while preserving common
        capitalization patterns (Mc/Mac, O', hyphens, initials, suffixes).

        Examples:
          "HARSHITH SIDDARDHA MANNE" → "Harshith Siddardha Manne"
          "harshith siddardha manne" → "Harshith Siddardha Manne"
          "Harshith Siddardha Manne" → "Harshith Siddardha Manne" (unchanged)
          "MCDONALD"                  → "McDonald"
          "O'CONNOR"                  → "O'Connor"
          "MARY-JANE WATSON"          → "Mary-Jane Watson"
          "JOHN F. KENNEDY JR"        → "John F. Kennedy Jr"
          "JANE SMITH II"             → "Jane Smith II"

        A word that already contains internal mixed-case (e.g., "McDonald",
        "DeShawn") is left untouched so user-curated capitalization survives.
        """
        if not name or not name.strip():
            return name

        cleaned = " ".join(name.split())

        def _cap_simple(w: str) -> str:
            return (w[:1].upper() + w[1:].lower()) if w else w

        def _cap_compound(w: str) -> str:
            # Hyphen-separated: "MARY-JANE" → "Mary-Jane"
            if "-" in w:
                return "-".join(_cap_compound(p) for p in w.split("-"))
            # Apostrophe-separated: "O'CONNOR" → "O'Connor"
            if "'" in w:
                segs = w.split("'")
                return "'".join(
                    _cap_simple(s) if i == 0 else _cap_simple(s) for i, s in enumerate(segs)
                )
            lower = w.lower()
            # Mc-prefix: "MCDONALD" → "McDonald"
            if lower.startswith("mc") and len(lower) > 2 and lower[2:].isalpha():
                return "Mc" + _cap_simple(lower[2:])
            # Mac-prefix: "MACARTHUR" → "MacArthur" (skip short words like "Mack", "Mach")
            if lower.startswith("mac") and len(lower) > 4 and lower[3:].isalpha():
                return "Mac" + _cap_simple(lower[3:])
            return _cap_simple(w)

        def _fix(word: str) -> str:
            if not word:
                return word
            # Strip trailing punctuation for analysis, re-attach later
            trailing = ""
            core = word
            while core and core[-1] in ".,;":
                trailing = core[-1] + trailing
                core = core[:-1]
            if not core:
                return word

            lower_core = core.lower()

            # Initials like "F" or "F." — single alpha char
            if len(core) == 1 and core.isalpha():
                return core.upper() + trailing

            # Roman-numeral suffixes ("II", "III", ...) — uppercase
            if lower_core in _NAME_NUMERIC_SUFFIXES:
                return lower_core.upper() + trailing

            # Text suffixes ("Jr", "Sr") — capitalized
            if lower_core in _NAME_TEXT_SUFFIXES:
                return lower_core.capitalize() + trailing

            # Preserve already-mixed case (McDonald, DeShawn, iPhone-like)
            has_lower = any(c.islower() for c in core)
            has_upper = any(c.isupper() for c in core)
            if has_lower and has_upper:
                return core + trailing

            # All-upper or all-lower → apply compound title-casing
            return _cap_compound(core) + trailing

        return " ".join(_fix(w) for w in cleaned.split(" "))

    @staticmethod
    def _normalize_linkedin(url: str) -> str:
        """Normalize a LinkedIn URL to https://linkedin.com/in/username format.

        Handles common malformations:
          - Missing protocol: linkedin.com/in/user → https://linkedin.com/in/user
          - Missing /in/: linkedin.com/user → https://linkedin.com/in/user
          - www prefix: www.linkedin.com/in/user → https://linkedin.com/in/user
          - Trailing slashes, query params, fragments
        """
        url = url.strip()
        if not url:
            return ""
        # Match canonical /in/username form
        match = re.search(r"linkedin\.com/in/([\w-]+)", url, re.IGNORECASE)
        if match:
            return f"https://linkedin.com/in/{match.group(1)}"
        # Match linkedin.com/username (missing /in/) — insert /in/
        match = re.search(r"linkedin\.com/([\w-]+)", url, re.IGNORECASE)
        if match:
            username = match.group(1)
            # Exclude known LinkedIn subpaths that aren't usernames
            if username.lower() not in (
                "pub",
                "company",
                "school",
                "jobs",
                "feed",
                "messaging",
                "in",
                "notifications",
                "mynetwork",
            ):
                return f"https://linkedin.com/in/{username}"
        # Fallback: if it contains linkedin.com, just normalize protocol
        if "linkedin.com" in url.lower():
            url = re.sub(r"^(https?://)?(www\.)?", "https://", url)
            return url.rstrip("/")
        return url

    @staticmethod
    def _normalize_github(url: str) -> str:
        """Normalize a GitHub URL to https://github.com/username format.

        Handles missing protocol, www prefix, trailing slashes.
        """
        url = url.strip()
        if not url:
            return ""
        match = re.search(r"github\.com/([\w-]+)", url, re.IGNORECASE)
        if match:
            username = match.group(1)
            return f"https://github.com/{username}"
        if "github.com" in url.lower():
            url = re.sub(r"^(https?://)?(www\.)?", "https://", url)
            return url.rstrip("/")
        return url

    @staticmethod
    def _backfill_education(result: dict, raw_text: str) -> dict:
        """Backfill empty education institution names from raw text.

        The LLM sometimes extracts the degree and dates but omits the
        institution name. This method searches the raw text for common
        university name patterns near the degree text.

        Args:
            result: Parsed resume dict with possibly empty education institutions.
            raw_text: Original raw text extracted from the PDF.

        Returns:
            The same dict with education institutions backfilled where empty.
        """
        education = result.get("education", [])
        if not education:
            return result

        # Common university name patterns
        uni_pattern = re.compile(
            r"(?:University|Institute|College|School|Academy)" r"(?:\s+of\s+[\w\s]+)?",
            re.IGNORECASE,
        )

        # Strip trailing date/year fragments glued onto the institution line by
        # cross-column-aware extractors (e.g. "Arizona State University, Tempe, AZMay 2026").
        date_strip_re = re.compile(
            r"\s*(?:"
            r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s*\d{4}"
            r"|(?:Spring|Summer|Fall|Winter)\s*\d{4}"
            r"|\b(?:19|20)\d{2}\s*[-–—]\s*(?:Present|Current|(?:19|20)\d{2})"
            r"|\b(?:19|20)\d{2}\b"
            r")\s*$",
            re.IGNORECASE,
        )

        def _trim_institution(line: str) -> str:
            prev = None
            cur = line.strip()
            # Run repeatedly so e.g. "...AZMay 2026" → "...AZ" → "...AZ"
            while prev != cur:
                prev = cur
                cur = date_strip_re.sub("", cur).strip().rstrip(",;|").strip()
            return cur

        for edu_entry in education:
            if not isinstance(edu_entry, dict):
                continue
            if edu_entry.get("institution", "").strip():
                continue  # Already has an institution

            # Strategy 1: Search near the degree text in raw_text
            degree = edu_entry.get("degree", "")
            if degree:
                # Look for university names within ~200 chars of the degree text
                degree_idx = raw_text.lower().find(degree.lower()[:30])
                if degree_idx >= 0:
                    search_window = raw_text[max(0, degree_idx - 200) : degree_idx + 200]
                    # Look for lines that look like university names
                    for line in search_window.split("\n"):
                        line = line.strip()
                        if uni_pattern.search(line) and len(line) < 100:
                            edu_entry["institution"] = _trim_institution(line)
                            logger.info(
                                "Education backfill: found institution '%s' near degree '%s'",
                                edu_entry["institution"],
                                degree,
                            )
                            break

            # Strategy 2: If still empty, search for university name patterns globally
            if not edu_entry.get("institution", "").strip():
                for line in raw_text.split("\n"):
                    line = line.strip()
                    if uni_pattern.search(line) and len(line) < 100 and not line.startswith("["):
                        edu_entry["institution"] = _trim_institution(line)
                        logger.info(
                            "Education backfill: found institution '%s' via global search",
                            edu_entry["institution"],
                        )
                        break

        return result

    @staticmethod
    def _clean_education_fields(result: dict) -> dict:
        """Strip date patterns and duplicate segments from education degree fields.

        The LLM sometimes returns degree fields with dates baked in, e.g.:
          degree: "Master of Science, Information Technology | Dec 2025"
        or even:
          degree: "Master of Science, IT | Dec 2025 | Master of Science, IT"

        This method:
          1. Detects and removes date patterns from degree fields
          2. Removes pipe-separated duplicate segments within degree
          3. Extracts dates into the dates field if dates was empty
        """
        _DATE_RE = re.compile(
            r"\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
            r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|"
            r"Dec(?:ember)?)\s+\d{4}\b"
            r"|\b(?:Spring|Fall|Summer|Winter)\s+\d{4}\b",
            re.IGNORECASE,
        )

        education = result.get("education", [])
        for edu in education:
            if not isinstance(edu, dict):
                continue

            degree = edu.get("degree", "").strip()
            if not degree:
                continue

            # Step 1: If degree contains pipe characters, split and deduplicate
            if "|" in degree:
                segments = [s.strip() for s in degree.split("|") if s.strip()]
                date_segments = []
                non_date_segments = []
                seen = set()
                for seg in segments:
                    seg_stripped = _DATE_RE.sub("", seg).strip()
                    if not seg_stripped:
                        date_segments.append(seg)
                    else:
                        key = seg_stripped.lower()
                        if key not in seen:
                            seen.add(key)
                            non_date_segments.append(seg_stripped)

                if non_date_segments:
                    # Join all non-date segments with comma so multi-part degrees
                    # like "Master of Science | Information Technology" keep
                    # both halves instead of dropping the specialization.
                    degree = ", ".join(non_date_segments)
                if not edu.get("dates", "").strip() and date_segments:
                    edu["dates"] = " - ".join(date_segments)

            # Step 2: Even without pipes, strip inline date patterns from degree
            cleaned = _DATE_RE.sub("", degree).strip()
            cleaned = re.sub(r"^[\s|,\-]+|[\s|,\-]+$", "", cleaned)
            if cleaned:
                edu["degree"] = cleaned

        return result

    @staticmethod
    def _validate_critical_fields(result: dict):
        """Log warnings for any missing critical fields after all backfills.

        This is a diagnostic method — it doesn't modify the result, just
        logs clear warnings so that issues are visible in the server logs
        and can be debugged easily.

        Critical fields checked:
          - contact: name, email
          - education: institution (for each entry)
          - experience: company, title (for each entry)
        """
        # Check contact
        contact = result.get("contact", {})
        missing_contact = []
        if not contact.get("name", "").strip():
            missing_contact.append("name")
        if not contact.get("email", "").strip():
            missing_contact.append("email")
        if not contact.get("phone", "").strip():
            logger.info(
                "PARSE VALIDATION: Phone number missing — raw text may not contain one, "
                "or regex backfill did not match."
            )
        if missing_contact:
            logger.warning(
                "PARSE VALIDATION: Missing critical contact fields: %s", ", ".join(missing_contact)
            )

        # Check education
        for i, edu in enumerate(result.get("education", [])):
            if not edu.get("institution", "").strip():
                logger.warning(
                    "PARSE VALIDATION: Education[%d] has empty institution "
                    "(degree: '%s', location: '%s')",
                    i,
                    edu.get("degree", "?"),
                    edu.get("location", "?"),
                )

        # Check experience
        for i, exp in enumerate(result.get("experience", [])):
            if not exp.get("company", "").strip():
                logger.warning("PARSE VALIDATION: Experience[%d] has empty company", i)
            if not exp.get("title", "").strip():
                logger.warning("PARSE VALIDATION: Experience[%d] has empty title", i)

    # ======================================================================
    # 3. FULL UPLOAD PIPELINE
    # ======================================================================

    def upload_and_parse(self, file_bytes: bytes, user_email: str = "") -> dict:
        """Full synchronous pipeline: parse → validate → store.

        This is the legacy synchronous path. The async path (used by the
        /upload endpoint) calls parse_to_structured() and save_parsed_resume()
        separately via the background job worker.

        Args:
            file_bytes: Raw PDF file content as bytes.
            user_email: Email of the authenticated user (for scoping storage).

        Returns:
            The stored document dict (includes structured JSON, raw_text,
            flat skill list, experience_years, and job_titles).
        """
        import base64

        file_b64 = base64.b64encode(file_bytes).decode("utf-8")
        structured, raw_text = self.parse_to_structured("", file_b64, self.MIME_TYPES["pdf"])
        return self.save_parsed_resume(structured, raw_text, user_email=user_email)

    def upload_and_parse_file(self, file_bytes: bytes, filename: str, user_email: str = "") -> dict:
        """Upload and parse a PDF or DOCX resume file via multi-modal LLM."""
        import base64

        file_b64 = base64.b64encode(file_bytes).decode("utf-8")
        mime_type = self.get_mime_type(filename)
        structured, raw_text = self.parse_to_structured("", file_b64, mime_type)
        return self.save_parsed_resume(structured, raw_text, user_email=user_email)

    def save_parsed_resume(
        self,
        structured: dict,
        raw_text: str,
        user_email: str = "",
        content_hash: str = "",
    ) -> dict:
        """Build flat status fields, persist to MongoDB, and return the document.

        This method is separated from upload_and_parse so the async job worker
        can call it independently after LLM parsing completes.

        Flat fields (skills, experience_years, job_titles, summary) are stored
        alongside the full structured JSON for backward compatibility with
        the GET /api/resume/status endpoint.

        Storage model: per-user — the previous resume for this user is replaced.

        Args:
            structured: Validated structured resume JSON.
            raw_text: Original extracted text (stored for fallback and repair).
            user_email: Email of the authenticated user (for scoping storage).

        Returns:
            The stored document dict with _id removed.
        """
        # Derive flat status fields from the structured data
        flat_skills = flatten_skills(structured)
        job_titles = extract_job_titles(structured)
        experience_years = self._estimate_experience_years(structured)
        summary = structured.get("summary", "")

        doc = {
            "user_email": user_email,
            "structured": structured,
            "content_hash": content_hash,
            "raw_text": raw_text[:_MAX_TEXT_CHARS],
            "raw_text_length": len(raw_text),
            "skills": flat_skills,
            "experience_years": experience_years,
            "job_titles": job_titles,
            "summary": summary,
            "education": [
                {
                    "degree": edu.get("degree", ""),
                    "institution": edu.get("institution", ""),
                    "year": (
                        edu.get("dates", "").split("–")[-1].strip() if edu.get("dates") else ""
                    ),
                }
                for edu in structured.get("education", [])
            ],
            # Mirror structured.certifications at the top level for backwards
            # compat with status/list endpoints. Empty list when the resume
            # had none — the parser prompt forbids fabrication.
            "certifications": [
                c for c in (structured.get("certifications") or []) if isinstance(c, str) and c.strip()
            ],
            "parsed_at": datetime.now(timezone.utc),
        }

        # Per-user storage (parsed/structured resume only):
        # Don't delete "base" documents (type='base') which store the uploaded file metadata.
        self.user_resumes.delete_many({"user_email": user_email, "structured": {"$exists": True}})
        self.user_resumes.insert_one(doc)

        doc.pop("_id", None)
        return doc

    # ======================================================================
    # 4. RETRIEVAL
    # ======================================================================

    def get_structured_resume(self, user_email: str = "") -> Optional[Dict[str, Any]]:
        """Retrieve the latest stored structured resume from MongoDB.

        Args:
            user_email: Email of the authenticated user (for scoping retrieval).

        Returns:
            The resume document dict with _id removed, or None if no resume
            has been uploaded yet.
        """
        # `user_resumes` contains both:
        # - base resume docs: { type: 'base', s3_key, filename, ... } (no `structured`)
        # - parsed/structured resume docs: { structured, raw_text, parsed_at, ... }
        # Only return parsed/structured docs.
        query = {"user_email": user_email, "structured": {"$exists": True}} if user_email else {}
        resume = self.user_resumes.find_one(query, sort=[("parsed_at", -1)])
        if resume:
            resume.pop("_id", None)
            # Backwards-compat: resumes parsed before name normalization may
            # still hold ALL-CAPS names. Normalize on read so display is correct.
            structured = resume.get("structured")
            if isinstance(structured, dict):
                contact = structured.get("contact")
                if isinstance(contact, dict) and contact.get("name"):
                    contact["name"] = ResumeParser._normalize_name(contact["name"])
        return resume

    def ensure_structured_resume(self, user_email: str = "") -> Optional[Dict[str, Any]]:
        """Return a structured resume, re-parsing from the base S3 file if needed.

        Users who uploaded before the async parse flow (or whose parse job
        failed) only have a `type='base'` doc with an S3 key — no `structured`
        field. Without this fallback the tailor endpoint would reject them
        even though their resume is sitting in S3.
        """
        resume = self.get_structured_resume(user_email=user_email)
        if resume and resume.get("structured"):
            return resume

        if not user_email:
            logger.warning(
                "ensure_structured_resume: no user_email provided, cannot look up resume"
            )
            return None

        logger.info(
            "ensure_structured_resume: no parsed resume for %s, searching for base file to re-parse",
            user_email,
        )

        base = self.user_resumes.find_one(
            {"user_email": user_email, "type": "base", "is_active": True}
        ) or self.user_resumes.find_one(
            {"user_email": user_email, "type": "base"},
            sort=[("uploaded_at", -1)],
        )
        if not base or not base.get("s3_key"):
            logger.warning(
                "ensure_structured_resume: no base file doc with s3_key for %s (base doc exists: %s)",
                user_email,
                bool(base),
            )
            return None

        logger.info(
            "ensure_structured_resume: found base file for %s — s3_key=%s, filename=%s — attempting re-parse from S3",
            user_email,
            base.get("s3_key"),
            base.get("filename", "?"),
        )

        try:
            from services.s3_service import get_storage_service
            import base64 as _b64
            import hashlib as _hashlib

            file_bytes = get_storage_service().get_resume(base["s3_key"])
            file_b64 = _b64.b64encode(file_bytes).decode("utf-8")
            mime_type = self.get_mime_type(base.get("filename", "resume.pdf"))
            content_hash = base.get("content_hash") or _hashlib.sha256(file_bytes).hexdigest()
            structured, raw_text = self.parse_to_structured("", file_b64, mime_type)
            result = self.save_parsed_resume(
                structured, raw_text, user_email=user_email, content_hash=content_hash
            )
            logger.info(
                "ensure_structured_resume: successfully re-parsed resume for %s", user_email
            )
            return result
        except Exception as e:
            logger.error(
                "ensure_structured_resume: S3 reparse failed for %s (s3_key=%s): %s",
                user_email,
                base.get("s3_key"),
                e,
                exc_info=True,
            )
            return None

    # ======================================================================
    # 5. HELPERS
    # ======================================================================

    @staticmethod
    def _estimate_experience_years(structured: dict) -> int:
        """Estimate total years of professional experience from date ranges.

        Parses experience entry dates (e.g., "May 2021 – Present") to calculate
        month-level duration. Handles various formats:
          - "Jan 2020 – Dec 2022"  → 36 months → 3 years
          - "May 2021 – Present"   → months until now
          - "2019 – 2021"          → 24 months → 2 years
          - "Aug 2025 - Current"   → months until now

        Falls back to year-only calculation if month abbreviations aren't found.

        Args:
            structured: Parsed resume dict with 'experience' entries.

        Returns:
            Estimated total years of experience (minimum 1 if any work found,
            0 if no parseable dates exist).
        """
        total_months = 0
        now = datetime.now()
        current_year = now.year
        current_month = now.month

        for exp in structured.get("experience", []):
            dates = exp.get("dates", "")
            if not dates:
                continue

            # Extract 4-digit years (e.g., 2020, 2023)
            years = _YEAR_PATTERN.findall(dates)

            # Extract month abbreviations (e.g., "Jan", "May", "December")
            months = [m.group(0).lower()[:3] for m in _MONTH_PATTERN.finditer(dates)]

            if len(years) >= 2:
                # Date range with start and end years
                try:
                    start_year = int(years[0])
                    end_year = int(years[-1])
                    start_m = _MONTH_MAP.get(months[0], 1) if months else 1
                    end_m = (
                        _MONTH_MAP.get(months[-1] if len(months) >= 2 else "", 12) if months else 12
                    )
                    total_months += (end_year - start_year) * 12 + (end_m - start_m)
                except (ValueError, IndexError):
                    # Fallback: year-only calculation
                    total_months += (int(years[-1]) - int(years[0])) * 12

            elif len(years) == 1:
                # Single year — check if it extends to "Present"
                dates_lower = dates.lower()
                if any(kw in dates_lower for kw in _PRESENT_KEYWORDS):
                    try:
                        start_year = int(years[0])
                        start_m = _MONTH_MAP.get(months[0], 1) if months else 1
                        total_months += (current_year - start_year) * 12 + (current_month - start_m)
                    except (ValueError, IndexError):
                        total_months += (current_year - int(years[0])) * 12

        return max(1, total_months // 12) if total_months > 0 else 0
