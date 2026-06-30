"""
Answer Generator — powers the Job Application Autofill Chrome extension.

Two responsibilities:

1. build_application_profile() — flatten the user's stored base resume +
   visa profile into a single "ApplicationProfile" JSON the extension seeds
   into local storage. This is what feeds standard-field autofill (name,
   email, phone, location, links, skills, certs, projects, work auth).

2. AnswerGenerator.generate() — write DISTINCTIVE answers to open-ended
   application questions ("Why did you apply?", "Tell us about a project
   you're excited about"). Answers are grounded in the user's REAL resume
   and projects and aligned to the job description on the page. The whole
   point is a 1-in-100 answer, not a template — see _DISTINCTIVENESS_BLOCK.

Grounding follows the same pattern as services/project_generator.py:
reuse flatten_skills / build_resume_text and the gemini_json LLM facade.
"""
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# The single most important text in this module. It's what turns a generic,
# could-be-anyone cover-letter answer ("I am passionate about your mission
# and excited by the opportunity to grow...") into something a recruiter
# reading their 80th application of the day actually stops on.
_DISTINCTIVENESS_BLOCK = (
    "WRITE A 1-IN-100 ANSWER — this is the whole job. A recruiter reads ~80 of\n"
    "these a day; 78 of them are interchangeable. Yours must be the one they\n"
    "remember. The rules below are how you get there.\n\n"
    "ANCHOR EVERY CLAIM TO SOMETHING REAL:\n"
    "  • Every sentence that makes a claim must point to a SPECIFIC project,\n"
    "    technology, decision, or outcome FROM THE CANDIDATE'S RESUME BELOW.\n"
    "  • Name the actual thing they built and the actual problem it solved.\n"
    "    'I built a sliding-window outlier detector that flags transactions\n"
    "    beyond 3σ of a user's baseline' beats 'I have experience with anomaly\n"
    "    detection' every time.\n"
    "  • NEVER invent facts, employers, metrics, or projects. If it isn't in\n"
    "    the resume, you cannot claim it. Distinctiveness comes from SPECIFICITY\n"
    "    about real work, not from embellishment.\n\n"
    "MIRROR THE JOB, DON'T FLATTER THE COMPANY:\n"
    "  • Connect the candidate's real work to THIS role's actual priorities\n"
    "    (from the job description). Show the overlap concretely.\n"
    "  • Do NOT praise the company's mission/values in the abstract. 'I admire\n"
    "    your commitment to innovation' is filler — cut it. Instead, name a\n"
    "    specific thing the role does and tie it to a specific thing they've done.\n\n"
    "OPEN WITH A CONCRETE HOOK:\n"
    "  • The first sentence must carry a real detail — a project, a decision, a\n"
    "    moment — not a throat-clear ('I am writing to express my interest...').\n\n"
    "BANNED — these phrases mark the other 78 applications. Never use them:\n"
    "  • 'I am passionate about', 'I am excited about the opportunity',\n"
    "    'I am a perfect fit', 'I thrive in', 'fast-paced environment',\n"
    "    'wear many hats', 'hit the ground running', 'team player',\n"
    "    'results-driven', 'detail-oriented', 'proven track record',\n"
    "    'leverage my skills', 'I believe my background', 'cutting-edge',\n"
    "    'I am confident that'.\n"
    "  • No empty superlatives ('amazing', 'incredible', 'world-class').\n"
    "  • No restating the question back ('You asked why I applied. I applied\n"
    "    because...').\n\n"
    "VOICE & SHAPE:\n"
    "  • First person, direct, warm but not gushing. Sound like a sharp engineer\n"
    "    talking, not a cover-letter template.\n"
    "  • Tight. Most application boxes want 80-160 words unless the question\n"
    "    implies more. Every sentence earns its place.\n"
    "  • End on a forward note tied to the role, not a generic 'I look forward\n"
    "    to hearing from you.'\n"
)


def _flatten_skill_groups(skills: Any) -> Dict[str, List[str]]:
    """Return skills as {category: [skill, ...]} regardless of stored shape.

    The parsed resume stores skills as a dict-of-lists, but older/odd records
    may hold a flat list. Normalize so the extension always gets groups.
    """
    if isinstance(skills, dict):
        return {
            str(cat): [str(s) for s in (vals or []) if str(s).strip()]
            for cat, vals in skills.items()
            if isinstance(vals, list)
        }
    if isinstance(skills, list):
        return {"General": [str(s) for s in skills if str(s).strip()]}
    return {}


# Maps the visa_timeline_service visa_status values to plain-English work
# authorization answers an application form actually asks for. These are
# STARTING POINTS — the extension stores them locally and the user edits
# anything that doesn't match their situation.
_WORK_AUTH_BY_STATUS = {
    "F-1 OPT": {
        "authorized_us": True,
        "requires_sponsorship_now": False,
        "requires_sponsorship_future": True,
        "summary": "Authorized to work in the U.S. on F-1 OPT; will require "
                   "visa sponsorship (H-1B) in the future.",
    },
    "F-1 STEM OPT": {
        "authorized_us": True,
        "requires_sponsorship_now": False,
        "requires_sponsorship_future": True,
        "summary": "Authorized to work in the U.S. on F-1 STEM OPT; will "
                   "require visa sponsorship (H-1B) in the future.",
    },
    "F-1 CPT": {
        "authorized_us": True,
        "requires_sponsorship_now": False,
        "requires_sponsorship_future": True,
        "summary": "Authorized to work in the U.S. on F-1 CPT; will require "
                   "visa sponsorship in the future.",
    },
    "H-1B": {
        "authorized_us": True,
        "requires_sponsorship_now": True,
        "requires_sponsorship_future": True,
        "summary": "Authorized to work in the U.S. on H-1B; requires transfer "
                   "of visa sponsorship.",
    },
    "Green Card": {
        "authorized_us": True,
        "requires_sponsorship_now": False,
        "requires_sponsorship_future": False,
        "summary": "U.S. Permanent Resident; no visa sponsorship required.",
    },
    "U.S. Citizen": {
        "authorized_us": True,
        "requires_sponsorship_now": False,
        "requires_sponsorship_future": False,
        "summary": "U.S. Citizen; no visa sponsorship required.",
    },
}


def _derive_work_authorization(visa_profile: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Map a normalized visa profile to application work-auth fields."""
    if not visa_profile:
        return {}
    status = visa_profile.get("visa_status") or "Other"
    mapped = _WORK_AUTH_BY_STATUS.get(status)
    out: Dict[str, Any] = {
        "visa_status": status,
        "country_of_birth": visa_profile.get("country_of_birth"),
    }
    if mapped:
        out.update(mapped)
    return out


def build_application_profile(
    base_resume_doc: Optional[Dict[str, Any]],
    visa_profile: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Flatten the stored base resume (+ visa profile) into an ApplicationProfile.

    `base_resume_doc` is the document returned by ResumeService.get_base_resume
    — the structured resume lives under its "structured" key. Returns a JSON-safe
    dict the extension seeds into chrome.storage.local. Everything here is a
    STARTING POINT the user can edit locally; nothing is authoritative.
    """
    structured = {}
    if isinstance(base_resume_doc, dict):
        structured = base_resume_doc.get("structured") or {}
    contact = structured.get("contact") or {}

    skills_groups = _flatten_skill_groups(structured.get("skills"))
    skills_flat: List[str] = []
    for vals in skills_groups.values():
        skills_flat.extend(vals)

    projects = []
    for proj in structured.get("projects") or []:
        if not isinstance(proj, dict):
            continue
        projects.append({
            "name": proj.get("name", ""),
            "tech": proj.get("tech", ""),
            "highlights": [str(b) for b in (proj.get("bullets") or []) if str(b).strip()],
        })

    experience = []
    for exp in structured.get("experience") or []:
        if not isinstance(exp, dict):
            continue
        experience.append({
            "title": exp.get("title", ""),
            "company": exp.get("company", ""),
            "location": exp.get("location", ""),
            "dates": exp.get("dates", ""),
            "highlights": [str(b) for b in (exp.get("bullets") or []) if str(b).strip()],
        })

    education = []
    for edu in structured.get("education") or []:
        if not isinstance(edu, dict):
            continue
        education.append({
            "degree": edu.get("degree", ""),
            "institution": edu.get("institution", ""),
            "location": edu.get("location", ""),
            "dates": edu.get("dates", ""),
            "gpa": edu.get("gpa", ""),
        })

    return {
        "personal": {
            "name": contact.get("name", ""),
            "email": contact.get("email", ""),
            "phone": contact.get("phone", ""),
            "location": contact.get("location", ""),
        },
        "links": {
            "linkedin": contact.get("linkedin", ""),
            "github": contact.get("github", ""),
            "portfolio": contact.get("portfolio", ""),
        },
        "summary": structured.get("summary", ""),
        "skills": {
            "groups": skills_groups,
            "flat": skills_flat,
        },
        "certifications": [
            str(c) for c in (structured.get("certifications") or []) if str(c).strip()
        ],
        "projects": projects,
        "experience": experience,
        "education": education,
        "work_authorization": _derive_work_authorization(visa_profile),
        "has_resume": bool(structured),
    }


# JSON schema (gemini_json/_dict_of style) for the multi-variant answer output.
_ANSWER_SCHEMA = {
    "variants": [{
        "answer": str,
        "angle": str,
    }]
}


class AnswerGenerator:
    """Generates distinctive, resume-grounded answers to application questions."""

    def generate(
        self,
        question: str,
        base_resume_doc: Optional[Dict[str, Any]],
        job_description: str = "",
        company: str = "",
        role: str = "",
        tone: str = "professional",
        n_variants: int = 3,
    ) -> List[Dict[str, str]]:
        """Return up to n_variants answers, each a {answer, angle} dict.

        Grounds the answer in the candidate's structured resume (under the
        "structured" key of base_resume_doc) and the job context. Returns an
        empty list on failure so the caller can surface a clean error.
        """
        from services.gemini_client import gemini_json, GEMINI_PRO
        from schemas.resume_schemas import flatten_skills, build_resume_text

        structured = {}
        if isinstance(base_resume_doc, dict):
            structured = base_resume_doc.get("structured") or {}
        if not structured:
            logger.warning("AnswerGenerator: no structured resume for grounding")
            return []

        n_variants = max(1, min(int(n_variants or 3), 3))

        resume_text = build_resume_text(structured)
        all_skills = flatten_skills(structured)
        skills_text = ", ".join(all_skills[:40]) if all_skills else ""

        # Compact project list — the richest grounding for "tell us about a
        # project" style questions.
        project_lines = []
        for proj in (structured.get("projects") or [])[:6]:
            if not isinstance(proj, dict):
                continue
            name = (proj.get("name") or "").strip()
            tech = (proj.get("tech") or "").strip()
            bullets = " ".join(str(b).strip() for b in (proj.get("bullets") or [])[:3])
            if name:
                project_lines.append(f"  • {name} ({tech}): {bullets}")
        projects_block = "\n".join(project_lines)

        job_context_parts = []
        if role:
            job_context_parts.append(f"Role: {role}")
        if company:
            job_context_parts.append(f"Company: {company}")
        if job_description:
            job_context_parts.append(f"Job description:\n{job_description[:3000]}")
        job_context = "\n".join(job_context_parts) or "(no job description provided)"

        prompt = (
            "You are helping a strong candidate answer ONE open-ended question on a\n"
            "job application form. Write the answer in the CANDIDATE'S OWN VOICE — it\n"
            "is submitted as their words, so it must be grounded entirely in the real\n"
            "resume facts below.\n\n"
            f"=== THE QUESTION BEING ASKED ===\n{question.strip()}\n\n"
            f"=== THE JOB ===\n{job_context}\n\n"
            f"=== CANDIDATE SKILLS ===\n{skills_text}\n\n"
            + (f"=== CANDIDATE PROJECTS ===\n{projects_block}\n\n" if projects_block else "")
            + f"=== CANDIDATE RESUME ===\n{resume_text[:3500]}\n\n"
            + _DISTINCTIVENESS_BLOCK + "\n"
            f"Tone: {tone}.\n\n"
            f"Produce {n_variants} DISTINCT answer variants. Each must take a different\n"
            "ANGLE on the same question — e.g. one leads with a specific project, one\n"
            "leads with a technical decision/trade-off, one leads with how the role's\n"
            "priorities line up with concrete past work. The variants must NOT be light\n"
            "rewrites of each other; a reader should see three genuinely different ways\n"
            "in. Each grounded in the real resume above.\n\n"
            "Return a JSON object with EXACTLY this structure:\n"
            "{\n"
            '  "variants": [\n'
            '    {"answer": "the full answer, ready to paste", "angle": "3-5 word label for this angle"}\n'
            "  ]\n"
            "}\n"
        )

        try:
            result = gemini_json(
                prompt,
                max_tokens=2500,
                temperature=0.7,  # higher than the deterministic generators — we WANT variety
                model=GEMINI_PRO,
                schema=_ANSWER_SCHEMA,
            )
        except Exception as e:
            logger.warning("AnswerGenerator: LLM call failed: %s", e)
            return []

        if not isinstance(result, dict):
            logger.warning("AnswerGenerator: non-dict result: %s", type(result))
            return []

        variants = []
        for v in result.get("variants", []) or []:
            if not isinstance(v, dict):
                continue
            answer = str(v.get("answer", "")).strip()
            if not answer:
                continue
            variants.append({
                "answer": answer,
                "angle": str(v.get("angle", "")).strip() or "Answer",
            })
            if len(variants) >= n_variants:
                break

        logger.info("AnswerGenerator: produced %d/%d variants", len(variants), n_variants)
        return variants
