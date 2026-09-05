"""Whole-record, source-linked JD extraction with explicit completeness."""
import re
from schemas.resume_schemas import JD_ANALYSIS_SCHEMA, validate_and_coerce
from utils.llm_safety import guarded_system, wrap_untrusted, redact_pii_for_model


def _chunks(text: str, limit: int = 14000) -> list[str]:
    chunks, current = [], ""
    for paragraph in re.split(r"(?<=\n)", text):
        if len(current) + len(paragraph) > limit and current:
            chunks.append(current)
            current = ""
        while len(paragraph) > limit:
            cut = paragraph.rfind(" ", 0, limit)
            cut = cut if cut > 0 else limit
            chunks.append(paragraph[:cut])
            paragraph = paragraph[cut:]
        current += paragraph
    if current:
        chunks.append(current)
    return chunks


def extract(text: str) -> dict:
    from services.claude_client import claude_json, CLAUDE_PRO
    from utils.keyword_normalizer import normalize_keywords
    from services.term_registry import sanitize_jd_analysis
    schema = dict(JD_ANALYSIS_SCHEMA)
    schema["requirements"] = [{"text": str, "priority": str, "kind": str,
                               "alternatives": [str], "source_excerpt": str}]
    chunks = _chunks(text or "")
    merged = validate_and_coerce({}, JD_ANALYSIS_SCHEMA)
    records, successes, complete = [], 0, True
    for chunk in chunks[:4]:
        try:
            result = claude_json(
                prompt=wrap_untrusted("job_description", redact_pii_for_model(chunk)),
                model=CLAUDE_PRO, max_tokens=9000, schema=schema,
                system=guarded_system("""Extract this section of a job description. Do not invent missing
fields. Include requirements with text, exact source_excerpt, kind (skill,
responsibility, qualification, logistics), priority (required/preferred/unspecified),
and alternatives when the employer says A or B. Preserve alternative groups rather
than treating both as mandatory. Extract professional competencies for any occupation,
not just software technologies. Distinguish explicit requirements from inferred importance.
Return job_title, company, location, employment_type, required_skills, preferred_skills,
responsibilities, qualifications, experience_years, industry, keywords, and requirements."""))
        except Exception:
            complete = False
            continue
        successes += 1
        parsed = validate_and_coerce(result, JD_ANALYSIS_SCHEMA)
        for key, val in parsed.items():
            if isinstance(val, list):
                merged[key] = list(dict.fromkeys(merged.get(key, []) + val))
            elif val and not merged.get(key):
                merged[key] = val
        if not result.get("requirements"):
            complete = False
        for r in result.get("requirements") or []:
            if isinstance(r, dict) and r.get("source_excerpt") and r["source_excerpt"] in text:
                records.append(r)
            else:
                complete = False
    if not successes:
        raise ValueError("Job description analysis unavailable. Please try again.")
    for key in ("required_skills", "preferred_skills", "keywords"):
        merged[key] = normalize_keywords(merged.get(key) or [])
    merged = sanitize_jd_analysis(merged)
    merged["requirements"] = records
    # Retain the full source only within the existing JD payload, bounded by
    # the same four chunks; source offsets are relative to this exact text.
    merged["source_text"] = text[:56000]
    merged["analysis_completeness"] = "complete" if complete and successes == len(chunks) else "partial"
    return merged
