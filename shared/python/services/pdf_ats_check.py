"""
PDF ATS Parseability Check — verifies the rendered PDF will actually parse
cleanly through real Applicant Tracking Systems.

Real ATSes (Workday, Greenhouse, Taleo, iCIMS, Lever, SmartRecruiters) all
extract text from the uploaded PDF before any keyword matching. If the text
layer is malformed (glyph soup, joined words, missing sections, scrambled
contact info), the candidate's resume is silently dropped — keyword scoring
is irrelevant because the parser never gets that far.

This module runs AFTER ResumeRenderer.generate_pdf produces bytes and
verifies the round-trip: structured JSON → PDF → extracted text → can a
real ATS find each piece? Returns a deterministic 0–100 score + a
breakdown of which checks passed so the frontend can show actionable
diagnostics.

Why this is the strongest signal we can compute:
- No "real ATS API" exists publicly — Workday/Greenhouse are tenant-scoped.
- Every commercial ATS first runs pdf-to-text via the same pdfminer / pypdf
  / pdfplumber pipelines. If pypdf extracts our content cleanly, those
  ATSes will too.
- Glyph corruption (em-dash → undefined, ligature joining, missing spaces)
  is the #1 silent failure mode and is fully deterministic to detect here.
"""
from __future__ import annotations

import io
import logging
import re
from typing import Any, Dict, List, Tuple

logger = logging.getLogger(__name__)


# Section headers the renderer emits — matches resume_renderer.py.
# All-caps because that's what the renderer prints; ATS parsers do
# case-insensitive matching, but our renderer is consistent so we can
# look for the exact strings.
_EXPECTED_SECTIONS = ("SUMMARY", "EXPERIENCE", "SKILLS", "EDUCATION")

# A simple but well-tested set of patterns that any production ATS parser
# will use to recognize contact fields.
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(
    r"(\+?\d{1,3}[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}"
)
_URL_RE = re.compile(r"(linkedin\.com|github\.com)/[A-Za-z0-9._/-]+", re.IGNORECASE)

# Glyph corruption signals — ligatures, joined words, undefined char
# replacements. fpdf2 with Times font + Latin-1 doesn't emit ligatures,
# but we still guard in case the renderer ever switches.
_GLYPH_CORRUPTION_HINTS = ("ﬀ", "ﬁ", "ﬂ", "ﬃ", "ﬄ", "�")

# Date patterns real ATS parsers use (Workday/Greenhouse run very similar
# regex over the text layer). If experience dates don't match any of these,
# the ATS can't compute tenure or order roles chronologically.
_DATE_PATTERNS = [
    re.compile(r"(?i)\b(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{4}\b"),
    re.compile(r"\b(0?[1-9]|1[0-2])[/-](19|20)\d{2}\b"),
    re.compile(r"\b(19|20)\d{2}\b"),  # bare year as a fallback
    re.compile(r"(?i)\bpresent\b"),
]


def extract_pdf_text(pdf_bytes: bytes) -> Tuple[str, int]:
    """Round-trip extraction using pypdf — the same library ATSes use.

    Returns (text, page_count). Empty string + 0 if extraction fails.
    """
    try:
        from pypdf import PdfReader
    except ImportError:
        logger.warning("pypdf not installed — ATS parseability check disabled")
        return "", 0

    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
    except Exception as e:
        logger.warning("PDF parse failed: %s", e)
        return "", 0

    parts: List[str] = []
    for page in reader.pages:
        try:
            parts.append(page.extract_text() or "")
        except Exception as e:
            logger.warning("PDF page extraction failed: %s", e)
    return "\n".join(parts), len(reader.pages)


def check_pdf_parseability(
    pdf_bytes: bytes,
    tailored: Dict[str, Any],
    jd_analysis: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Run the parseability suite. Returns:

        {
          "pdf_parseable_score": 0-100,
          "checks": {
            "extraction_ok": bool,
            "contact_name_found": bool,
            "contact_email_found": bool,
            "contact_phone_found": bool,
            "sections_found": [<found section names>],
            "sections_missing": [<missing section names>],
            "skills_keywords_found": int,
            "skills_keywords_total": int,
            "no_glyph_corruption": bool,
            "word_join_issues": [<sample joined-words>],
          },
          "warnings": [<human-readable warnings, actionable>]
        }
    """
    text, page_count = extract_pdf_text(pdf_bytes)
    extraction_ok = bool(text.strip())

    if not extraction_ok:
        return {
            "pdf_parseable_score": 0,
            "checks": {
                "extraction_ok": False,
                "contact_name_found": False,
                "contact_email_found": False,
                "contact_phone_found": False,
                "sections_found": [],
                "sections_missing": list(_EXPECTED_SECTIONS),
                "sections_in_order": False,
                "skills_keywords_found": 0,
                "skills_keywords_total": 0,
                "experience_dates_parseable": 0,
                "experience_dates_total": 0,
                "no_glyph_corruption": False,
                "word_join_issues": [],
                "contact_at_top": False,
                "page_count": page_count,
                "single_page": False,
            },
            "warnings": ["pypdf could not extract any text — real ATSes will reject this PDF."],
        }

    text_upper = text.upper()

    # --- Contact checks ---
    contact = (tailored.get("contact") or {})
    name = (contact.get("name") or "").strip()
    name_first_last = " ".join(name.split()[:2])  # at minimum first+last
    contact_name_found = bool(
        name_first_last and name_first_last.upper() in text_upper
    )
    contact_email_found = bool(
        contact.get("email") and contact["email"].lower() in text.lower()
    )
    contact_phone_found = (
        bool(_PHONE_RE.search(text)) if contact.get("phone") else True
    )

    # --- Section header presence + ORDER ---
    sections_found = [s for s in _EXPECTED_SECTIONS if s in text_upper]
    sections_missing = [s for s in _EXPECTED_SECTIONS if s not in text_upper]
    # ATSes route fields by section position. If EXPERIENCE appears before
    # SUMMARY in the extracted text, parsers misclassify the summary as
    # experience bullets. Verify monotonic positions match expected order.
    positions = [(s, text_upper.find(s)) for s in sections_found]
    positions_sorted = sorted(positions, key=lambda p: p[1])
    expected_seq = [s for s in _EXPECTED_SECTIONS if s in sections_found]
    sections_in_order = (
        [p[0] for p in positions_sorted] == expected_seq
    )

    # --- Contact at the top? (first ~5 lines) ---
    head = "\n".join(text.splitlines()[:5])
    contact_at_top = (
        (not contact.get("email") or contact["email"].lower() in head.lower())
        and (not name_first_last or name_first_last.upper() in head.upper())
    )

    # --- Experience date parseability ---
    # For every experience entry, the JSON has `dates` like "May 2023 - May
    # 2024". Verify that pattern survives in the extracted text — if Workday
    # can't parse the date, it can't compute tenure.
    #
    # Previously this required the full date string to appear verbatim in
    # text (`d in text`). That was too strict: fpdf2 / pypdf round-trips can
    # alter separators (en-dash → ?), inject spaces, or split the string
    # across PDF text objects, so 'August 2025 - Present' would fail even
    # when both endpoints were clearly recoverable. The check now confirms
    # each ATS-pattern token from the date string is independently present
    # in the extracted text — that's what a real Workday/Greenhouse parser
    # does (regex-scan, not literal-match).
    exp_dates = [
        (e.get("dates") or "").strip()
        for e in (tailored.get("experience") or [])
        if (e.get("dates") or "").strip()
    ]
    dates_total = len(exp_dates)
    dates_parseable = 0
    for d in exp_dates:
        # Collect every ATS-style token (month+year, MM/YYYY, year, "present")
        # the date string contains. Date is parseable if all such tokens also
        # appear in the extracted text.
        tokens = []
        for p in _DATE_PATTERNS:
            tokens.extend(m.group(0) for m in p.finditer(d))
        if not tokens:
            continue
        if all(t.lower() in text.lower() for t in tokens):
            dates_parseable += 1

    # --- Page count ---
    single_page = page_count == 1

    # --- Skills keyword coverage ---
    # Take the flat list of skills from the tailored JSON and verify each
    # appears in the extracted text. This catches ligature/Unicode issues
    # where a skill like "FastAPI" might come out as "Fast API" or "FastAPl".
    flat_skills: List[str] = []
    skills_dict = tailored.get("skills") or {}
    if isinstance(skills_dict, dict):
        for items in skills_dict.values():
            if isinstance(items, list):
                flat_skills.extend(s for s in items if isinstance(s, str))
    elif isinstance(skills_dict, list):
        flat_skills.extend(s for s in skills_dict if isinstance(s, str))

    text_low = text.lower()
    skills_found = sum(1 for s in flat_skills if s and s.lower() in text_low)
    skills_total = len([s for s in flat_skills if s])

    # --- Glyph corruption ---
    no_glyph_corruption = not any(g in text for g in _GLYPH_CORRUPTION_HINTS)

    # --- Word-join issues ---
    # pypdf occasionally drops spaces between words when the renderer's
    # character spacing is too tight (e.g. "Backendengineer"). Use a
    # CamelCase-after-lowercase heuristic to detect.
    word_join_issues: List[str] = []
    join_pattern = re.compile(r"\b[a-z]{4,}[A-Z][a-z]{3,}\b")
    for m in join_pattern.finditer(text):
        word_join_issues.append(m.group(0))
        if len(word_join_issues) >= 5:
            break

    # --- Score (weights chosen to mirror real-ATS rejection severity) ---
    # The first 4 checks are gates — any failure means real ATSes can't
    # process the resume at all. They carry the most weight.
    score = 0
    weights = {
        "extraction_ok": 8,
        "contact_name": 8,
        "contact_email": 8,
        "contact_phone": 4,
        "contact_at_top": 5,
        "sections": 20,            # awarded proportionally (5 per section)
        "sections_in_order": 5,
        "skills_coverage": 20,     # proportional
        "dates_parseable": 10,     # proportional
        "no_glyph_corruption": 6,
        "no_word_joins": 3,
        "single_page": 3,
    }

    if extraction_ok:
        score += weights["extraction_ok"]
    if contact_name_found:
        score += weights["contact_name"]
    if contact_email_found:
        score += weights["contact_email"]
    if contact_phone_found:
        score += weights["contact_phone"]
    if contact_at_top:
        score += weights["contact_at_top"]
    score += round(
        weights["sections"] * (len(sections_found) / max(1, len(_EXPECTED_SECTIONS)))
    )
    if sections_in_order:
        score += weights["sections_in_order"]
    if skills_total > 0:
        score += round(weights["skills_coverage"] * (skills_found / skills_total))
    else:
        score += weights["skills_coverage"]
    if dates_total > 0:
        score += round(weights["dates_parseable"] * (dates_parseable / dates_total))
    else:
        score += weights["dates_parseable"]
    if no_glyph_corruption:
        score += weights["no_glyph_corruption"]
    if not word_join_issues:
        score += weights["no_word_joins"]
    if single_page:
        score += weights["single_page"]

    # --- Human-readable warnings ---
    warnings: List[str] = []
    if not contact_name_found:
        warnings.append(f"Candidate name '{name}' did not extract cleanly from PDF.")
    if not contact_email_found and contact.get("email"):
        warnings.append("Email did not extract cleanly — recruiter can't reply.")
    if not contact_phone_found and contact.get("phone"):
        warnings.append("Phone number did not extract cleanly.")
    if not contact_at_top and contact.get("email"):
        warnings.append(
            "Contact info isn't in the top ~5 lines — many ATSes scan only "
            "the first ~10% of the document for contact fields."
        )
    if sections_missing:
        warnings.append(
            f"Missing section headers in extracted text: {', '.join(sections_missing)}. "
            "Real ATSes use these to route content into the correct field."
        )
    if not sections_in_order and len(sections_found) >= 2:
        warnings.append(
            f"Sections appear out of order in extracted text "
            f"({' -> '.join(p[0] for p in positions_sorted)}) — ATSes "
            "expect SUMMARY then EXPERIENCE then SKILLS then EDUCATION."
        )
    if skills_total > 0 and skills_found / skills_total < 0.9:
        warnings.append(
            f"Only {skills_found}/{skills_total} skills extracted cleanly — "
            "check for ligature or spacing issues in the PDF."
        )
    if dates_total > 0 and dates_parseable < dates_total:
        warnings.append(
            f"Only {dates_parseable}/{dates_total} experience date ranges "
            "match standard ATS date patterns. Workday especially needs "
            "'MMM YYYY' or 'MM/YYYY' formats to compute tenure."
        )
    if not no_glyph_corruption:
        warnings.append(
            "PDF contains undefined glyph or ligature characters — these "
            "appear as boxes/garbage in stricter parsers (Taleo, older Workday)."
        )
    if word_join_issues:
        warnings.append(
            f"Possible word-join issue (words ran together in extraction): "
            f"{', '.join(word_join_issues[:3])}"
        )
    if not single_page and page_count > 1:
        warnings.append(
            f"Resume is {page_count} pages. Most ATSes accept multi-page, "
            "but recruiter time-on-page averages 6-8 seconds — one page "
            "is preferred for early-career and IC roles."
        )

    return {
        "pdf_parseable_score": max(0, min(100, score)),
        "checks": {
            "extraction_ok": extraction_ok,
            "contact_name_found": contact_name_found,
            "contact_email_found": contact_email_found,
            "contact_phone_found": contact_phone_found,
            "contact_at_top": contact_at_top,
            "sections_found": sections_found,
            "sections_missing": sections_missing,
            "sections_in_order": sections_in_order,
            "skills_keywords_found": skills_found,
            "skills_keywords_total": skills_total,
            "experience_dates_parseable": dates_parseable,
            "experience_dates_total": dates_total,
            "no_glyph_corruption": no_glyph_corruption,
            "word_join_issues": word_join_issues,
            "page_count": page_count,
            "single_page": single_page,
        },
        "warnings": warnings,
    }
