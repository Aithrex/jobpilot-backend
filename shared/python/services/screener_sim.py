"""Screener simulator: read the finished résumé the way an employer's screener does.

Employer AI (Workday HiredScore, Greenhouse Talent Matching, Ashby AI review)
checks the job's requirements one at a time and shows a recruiter the résumé
line that proves each one. This module asks the same question of OUR output,
using only what an ATS can see: the text extracted from the rendered PDF.

Two layers:

* **Deterministic** (always on unless ``SCREENER_SIM=off``): for each JD
  requirement, where it is visible on the page (in a bullet, only in Skills,
  or not at all), whether the SOURCE résumé proves it, and therefore whether a
  miss is fixable by writing or is a real gap. It also computes the countable
  signals of HackerRank's published intern rubric (interviewstreet/hiring-agent,
  MIT): project links, profile links, tutorial-style projects, AI work that is
  listed vs built vs evaluated, startup and open-source evidence. HackerRank's
  own README reports the same résumé scoring 74 to 90 across runs because their
  scorer is a single LLM call; counting these in code keeps them stable.
* **LLM read** (``SCREENER_SIM=full``): one temperature-0 call that sees ONLY
  the extracted PDF text — never the source résumé, fact store or writer notes,
  so it cannot reward evidence the employer cannot see. Every quote it returns
  is checked against the page; an unquotable "meets" becomes "unclear". The
  grade stays deterministic, because an LLM screener prefers text written by an
  LLM (Xu et al., AIES 2025) and ours would flatter our own writer.

``patch_visibility`` is the write side of the loop and it NEVER writes new
content: when the source résumé proves a requirement and the tailored résumé
no longer shows it, it restores the user's own source bullet, verbatim, into
the same job or project. A requirement the source does not prove is reported
as a gap (a question for the user) and is never written.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

VERSION = "screener-1.0"
LENS_NAME = "hackerrank-intern-rubric"
LENS_SOURCE = "https://github.com/interviewstreet/hiring-agent"

MAX_MUST = 8
MAX_NICE = 5
MAX_PATCHES = 4
_PDF_TEXT_LIMIT = 14000

# HackerRank's category caps (roles/software_engineering_intern/role.json).
LENS_CATEGORY_MAX = {
    "open_source": 35,
    "self_projects": 30,
    "production": 25,
    "technical_skills": 10,
    "ai_fluency": 10,
}

_AI_RE = re.compile(
    r"\b(llms?|large language models?|rag|retrieval[- ]augmented|embeddings?|vector (?:db|database|store|search)|"
    r"(?:ai|llm|multi-?agent) agents?|agentic|tool[- ]calling|function calling|fine[- ]tun\w*|"
    r"prompt engineering|prompt design|system prompts?|gpt-?\d\w*|chatgpt|openai|anthropic|"
    r"gemini|vertex ai|langchain|langgraph|llamaindex|hugging ?face|transformer models?|pytorch|tensorflow|"
    r"machine learning|deep learning|neural networks?|computer vision|nlp|natural language processing|"
    r"scikit-learn|sklearn|xgboost|faiss|pinecone|pgvector|chroma(?:db)?|weaviate|ollama)\b",
    re.I,
)
_EVAL_RE = re.compile(
    r"\b(eval\w*|accuracy|precision|recall|f1|bleu|rouge|benchmark\w*|test set|test questions?|"
    r"held[- ]out|ground truth|hallucinat\w*|guardrails?|a/b test\w*|latency|p\d\d|"
    r"\d+ of \d+|\d+(?:\.\d+)?\s?%)",
    re.I,
)
_TUTORIAL_RE = re.compile(
    r"\b(to-?do|todo list|calculator|weather app|note[- ]taking|notes app|recipe|"
    r"tic[- ]tac[- ]toe|portfolio website|personal website|crud app|blog app|"
    r"expense tracker|quiz app|hello world|sentiment analysis)\b",
    re.I,
)
_FOUNDER_RE = re.compile(r"\b(co-?founder|founder|founding (?:engineer|member|team))\b", re.I)
_EARLY_RE = re.compile(
    r"\b(first (?:\d+ )?(?:engineer|hire|employee)s?|early[- ]stage|employee #?\d{1,2}\b|seed[- ]stage|pre-seed)\b",
    re.I,
)
_OSS_RE = re.compile(
    r"\b(open[- ]source|pull requests?|merged (?:prs?|pull requests?)|maintainer|contributor to|"
    r"contributed to|google summer of code|gsoc|girl ?script summer of code|hacktoberfest)\b",
    re.I,
)
_LINK_RE = re.compile(r"(https?://|github\.com/|gitlab\.com/|\bwww\.|\.(?:dev|app|io|com|ai|me)/)", re.I)
_INJECTION_RE = re.compile(
    r"(ignore (?:all |any )?(?:previous|prior|above) instructions|disregard (?:the )?(?:previous|above)|"
    r"you are (?:an? )?(?:ai|language model|llm|chatgpt)|as an ai (?:screener|recruiter)|"
    r"(?:rank|rate|score|mark) (?:this|the) (?:candidate|applicant|resume)|"
    r"(?:hire|shortlist|select) this (?:candidate|applicant)|system prompt)",
    re.I,
)

ARCHETYPE_SIGNALS: Dict[str, List[str]] = {
    "founding_engineer": [
        "shipped something end to end, from idea to real users",
        "breadth across front end, back end and infrastructure",
        "ownership with little specification; speed of shipping",
        "real users or customers; startup, open-source or side-project exposure",
    ],
    "ai_engineer": [
        "built LLM features (RAG, agents, tool calls) rather than only listing them",
        "evaluation discipline: test sets, quality metrics, regressions",
        "cost, latency and quality trade-offs",
        "Python plus model APIs and vector stores, used in named projects or jobs",
    ],
    "backend": [
        "services and APIs in a named language, with data stores and queues",
        "scale stated: requests per second, data size, latency",
        "reliability: testing, on-call, incidents",
    ],
    "systems_staff": [
        "designed systems, not only implemented them; trade-offs named",
        "migrations, design reviews, cross-team scope, incident leadership",
        "level read from scope: own service, team, several teams, company",
    ],
    "new_grad_swe": [
        "degree, school and graduation month",
        "internships first, then substantial projects with links",
        "languages and fundamentals shown in projects, not only listed",
    ],
    "general": [
        "each stated requirement proven by a specific line of work",
        "recent, relevant experience near the top",
    ],
}


# ---------------------------------------------------------------------------
# Switches
# ---------------------------------------------------------------------------

def mode() -> str:
    """off | deterministic (default, no model call) | full (adds one LLM read)."""
    value = os.environ.get("SCREENER_SIM", "deterministic").strip().lower()
    return value if value in {"off", "deterministic", "full"} else "deterministic"


def patch_enabled() -> bool:
    return os.environ.get("SCREENER_VISIBILITY_PATCH", "true").strip().lower() in {
        "1", "true", "yes", "on",
    }


# ---------------------------------------------------------------------------
# Small text helpers
# ---------------------------------------------------------------------------

def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().lower()


def _nospace(text: str) -> str:
    return re.sub(r"\s+", "", str(text or "")).lower()


def _tokens(text: str) -> set:
    return {t for t in re.findall(r"[a-z0-9+#.]+", str(text or "").lower()) if len(t) > 2}


def _overlap(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / float(min(len(ta), len(tb)))


def _term_in(text: str, term: str) -> bool:
    from services.requirement_matcher import match_term_in_text
    return bool(text) and match_term_in_text(text, term).found


def _term_in_pdf(pdf_text: str, term: str) -> bool:
    """pypdf sometimes drops the space between words; accept a spaceless hit."""
    if _term_in(pdf_text, term):
        return True
    from services.requirement_matcher import accepted_aliases
    squashed = _nospace(pdf_text)
    for form in accepted_aliases(term):
        f = _nospace(form)
        if len(f) >= 5 and f in squashed:
            return True
    return False


def quote_on_page(quote: str, page_text: str) -> bool:
    q = _squash(quote).strip(" .\"'")
    if len(q) < 8:
        return False
    if q in _squash(page_text):
        return True
    return _nospace(q) in _nospace(page_text)


# ---------------------------------------------------------------------------
# What the job asks for
# ---------------------------------------------------------------------------

def criteria_from_jd(jd_analysis: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Must = the JD's required skills; nice = preferred. Logistics are dropped
    by the same sanitizer the rest of the system uses."""
    from services.requirement_matcher import scored_term_lists

    lists = scored_term_lists(jd_analysis or {})
    rows: List[Dict[str, Any]] = []
    for kind, terms, cap in (("must", lists["required"], MAX_MUST),
                             ("nice", lists["preferred"], MAX_NICE)):
        for term in terms[:cap]:
            if str(term or "").strip():
                rows.append({"id": f"C{len(rows) + 1}", "term": str(term).strip(), "kind": kind})
    return rows


def archetype(jd_analysis: Dict[str, Any]) -> str:
    """Coarse role archetype from the JD analysis we already have (title and
    parked seniority context). It reads no raw JD text."""
    jd = jd_analysis or {}
    title = str(jd.get("job_title") or "").lower()
    ctx = jd.get("jd_context") or {}
    seniority = " ".join(str(s) for s in (ctx.get("seniority") or [])).lower()
    blob = f"{title} {seniority}"
    if re.search(r"\bfounding\b|\bfirst engineer\b|\bfounder\b", blob):
        return "founding_engineer"
    if re.search(r"\b(ai|ml|llm|genai|gen ai|machine learning|applied scientist)\b", title):
        return "ai_engineer"
    if re.search(r"\b(intern|internship|new grad|graduate|university|entry[- ]level|junior)\b", blob):
        return "new_grad_swe"
    if re.search(r"\b(staff|principal|distinguished|architect|distributed systems)\b", blob):
        return "systems_staff"
    if re.search(r"\b(backend|back[- ]end|platform|api)\b", title):
        return "backend"
    return "general"


# ---------------------------------------------------------------------------
# Where things are on a résumé
# ---------------------------------------------------------------------------

def _bullet_rows(resume: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for i, exp in enumerate((resume or {}).get("experience") or []):
        if not isinstance(exp, dict):
            continue
        label = ", ".join(str(x) for x in (exp.get("title"), exp.get("company")) if x)
        for j, b in enumerate(exp.get("bullets") or []):
            rows.append({"section": "experience", "index": i, "bullet": j, "label": label,
                         "dates": str(exp.get("dates") or ""), "text": str(b)})
    for i, proj in enumerate((resume or {}).get("projects") or []):
        if not isinstance(proj, dict):
            continue
        label = str(proj.get("name") or "")
        for j, b in enumerate(proj.get("bullets") or []):
            rows.append({"section": "projects", "index": i, "bullet": j, "label": label,
                         "dates": str(proj.get("dates") or ""), "text": str(b)})
        if proj.get("tech"):
            rows.append({"section": "projects", "index": i, "bullet": None, "label": label,
                         "dates": str(proj.get("dates") or ""), "text": str(proj.get("tech"))})
    return rows


def _listed_text(resume: Dict[str, Any]) -> str:
    from schemas.resume_schemas import flatten_skills
    parts = [str((resume or {}).get("summary") or "")]
    parts.extend(str(s) for s in (flatten_skills(resume or {}) or []))
    for edu in (resume or {}).get("education") or []:
        if isinstance(edu, dict):
            parts.append(" ".join(str(edu.get(k) or "") for k in ("degree", "coursework")))
    parts.extend(str(c) for c in ((resume or {}).get("certifications") or []))
    return "\n".join(p for p in parts if p)


def visibility(term: str, tailored: Dict[str, Any], pdf_text: Optional[str]) -> Dict[str, Any]:
    """in_context (a job/project line names it) > listed_only > not_visible."""
    hit = next((r for r in _bullet_rows(tailored) if _term_in(r["text"], term)), None)
    if hit:
        out = {"state": "in_context", "where": hit["label"], "dates": hit["dates"],
               "section": hit["section"], "span": hit["text"],
               "recency_rank": hit["index"] if hit["section"] == "experience" else None}
    elif _term_in(_listed_text(tailored), term):
        out = {"state": "listed_only", "where": "Skills / summary", "dates": "",
               "section": "skills", "span": "", "recency_rank": None}
    else:
        return {"state": "not_visible", "where": "", "dates": "", "section": "", "span": "",
                "recency_rank": None}
    if pdf_text is not None and pdf_text.strip() and not _term_in_pdf(pdf_text, term):
        out = {**out, "state": "not_visible", "lost_in_render": True}
    return out


def evidence(term: str, source: Dict[str, Any], extra_grounding: Sequence[str] = ()) -> Dict[str, Any]:
    """What the SOURCE résumé proves. proven > listed > confirmed > implied > gap."""
    hit = next((r for r in _bullet_rows(source) if r["bullet"] is not None and _term_in(r["text"], term)), None)
    if hit:
        return {"state": "proven", "line": hit}
    tech_hit = next((r for r in _bullet_rows(source) if _term_in(r["text"], term)), None)
    if tech_hit or _term_in(_listed_text(source), term):
        return {"state": "listed", "line": tech_hit}
    if any(_term_in(str(t), term) for t in extra_grounding or ()):
        return {"state": "confirmed", "line": None}
    try:
        from schemas.resume_schemas import flatten_skills
        from utils.skill_implications import is_implied
        owned = {str(s).lower() for s in (flatten_skills(source or {}) or [])}
        if owned and is_implied(term, owned):
            return {"state": "implied", "line": None}
    except Exception:
        pass
    return {"state": "gap", "line": None}


# ---------------------------------------------------------------------------
# The write side: restore the user's own proof, never invent
# ---------------------------------------------------------------------------

def _find_entry(tailored: Dict[str, Any], line: Dict[str, Any], source: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    src_list = (source or {}).get(line["section"]) or []
    if line["index"] >= len(src_list):
        return None
    src = src_list[line["index"]]
    for entry in (tailored or {}).get(line["section"]) or []:
        if not isinstance(entry, dict):
            continue
        if line["section"] == "experience":
            if (_squash(entry.get("company")) == _squash(src.get("company"))
                    and _squash(entry.get("title")) == _squash(src.get("title"))):
                return entry
        elif _squash(entry.get("name")) == _squash(src.get("name")):
            return entry
    return None


def patch_visibility(
    tailored: Dict[str, Any],
    source: Dict[str, Any],
    jd_analysis: Dict[str, Any],
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Restore source bullets that prove a requirement the tailored résumé
    stopped showing. Verbatim user text only, inside the same job/project.

    Returns (tailored, applied). Never touches a requirement the source does
    not prove, and never moves a line to a different employer or project.
    """
    if not isinstance(tailored, dict) or not isinstance(source, dict):
        return tailored, []
    applied: List[Dict[str, Any]] = []
    used_lines: set = set()
    for crit in criteria_from_jd(jd_analysis):
        if len(applied) >= MAX_PATCHES:
            break
        term = crit["term"]
        if visibility(term, tailored, None)["state"] == "in_context":
            continue
        ev = evidence(term, source)
        line = ev.get("line")
        if ev["state"] != "proven" or not line:
            continue
        key = (line["section"], line["index"], line["bullet"])
        if key in used_lines:
            continue
        entry = _find_entry(tailored, line, source)
        if entry is None:
            continue
        bullets = [str(b) for b in (entry.get("bullets") or [])]
        src_text = line["text"].strip()
        if any(_squash(b) == _squash(src_text) for b in bullets):
            continue
        derivative = max(range(len(bullets)), key=lambda k: _overlap(bullets[k], src_text), default=None)
        if derivative is not None and _overlap(bullets[derivative], src_text) >= 0.5:
            replaced = bullets[derivative]
            bullets[derivative] = src_text
            action = "replaced_rewrite"
        else:
            replaced = ""
            if crit["kind"] == "must":
                bullets.insert(0, src_text)
            else:
                bullets.append(src_text)
            action = "restored"
        entry["bullets"] = bullets
        used_lines.add(key)
        applied.append({
            "criterion": crit["id"], "term": term, "kind": crit["kind"],
            "entry": line["label"], "action": action, "text": src_text,
            "replaced": replaced,
        })
    if applied:
        try:
            from services.integrity_guard import IntegrityGuard
            guard = IntegrityGuard()
            tailored, _ = guard.fix_unicode_dashes(tailored)
            tailored, _ = guard.fix_terminal_periods(tailored)
        except Exception as exc:  # cosmetics only
            logger.debug("screener patch cosmetics skipped: %s", exc)
        block = tailored.get("screener") if isinstance(tailored.get("screener"), dict) else {}
        tailored["screener"] = {**block, "patched": applied}
        logger.info("screener_sim.patch restored=%d terms=%s", len(applied),
                    ",".join(a["term"] for a in applied))
    return tailored, applied


# ---------------------------------------------------------------------------
# The read side
# ---------------------------------------------------------------------------

def _render_text(tailored: Dict[str, Any], renderer) -> Tuple[Optional[str], int, str]:
    if renderer is None:
        return None, 0, "no_renderer"
    try:
        from services.pdf_ats_check import extract_pdf_text
        clean = {k: v for k, v in tailored.items() if k != "screener"}
        pdf_bytes, _resolution = renderer.generate_pdf(clean)
        if not pdf_bytes:
            return None, 0, "render_empty"
        text, pages = extract_pdf_text(pdf_bytes)
        return text, pages, "ok" if text.strip() else "no_text"
    except Exception as exc:
        logger.warning("screener_sim render/extract failed: %s", exc)
        return None, 0, "render_failed"


def _parse_checks(tailored: Dict[str, Any], pdf_text: str) -> List[Dict[str, Any]]:
    contact = (tailored or {}).get("contact") or {}
    checks = []
    for field, label in (("name", "Name"), ("email", "Email"), ("phone", "Phone")):
        value = str(contact.get(field) or "").strip()
        if not value:
            continue
        if field == "phone":
            digits = re.sub(r"\D", "", value)
            ok = len(digits) >= 7 and digits[-7:] in re.sub(r"\D", "", pdf_text)
        else:
            ok = _squash(value) in _squash(pdf_text) or _nospace(value) in _nospace(pdf_text)
        checks.append({"id": field, "label": f"{label} readable", "ok": bool(ok)})
    for key, heading in (("experience", "experience"), ("education", "education"), ("skills", "skills")):
        if (tailored or {}).get(key):
            checks.append({"id": f"heading_{key}", "label": f"'{heading.title()}' heading found",
                           "ok": heading in pdf_text.lower()})
    return checks


def _grade(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """HiredScore-shaped: basic (must) qualifications decide A/B vs C/D."""
    musts = [r for r in rows if r["kind"] == "must"]
    nices = [r for r in rows if r["kind"] == "nice"]
    shown = [r for r in musts if r["visibility"] in {"in_context", "listed_only"}]
    in_ctx = [r for r in musts if r["visibility"] == "in_context"]
    nice_shown = [r for r in nices if r["visibility"] in {"in_context", "listed_only"}]
    summary = {
        "must_total": len(musts), "must_visible": len(shown), "must_in_context": len(in_ctx),
        "nice_total": len(nices), "nice_visible": len(nice_shown),
    }
    if not musts:
        return {**summary, "grade": None, "grade_basis": "The job analysis has no required skills to check."}
    missing = len(musts) - len(shown)
    if missing == 0:
        strong = len(in_ctx) == len(musts) and (not nices or len(nice_shown) * 2 >= len(nices))
        grade = "A" if strong else "B"
        basis = ("Every must-have is shown in a job or project line"
                 + (" and most nice-to-haves are visible." if strong else ", but some are only listed or nice-to-haves are missing."))
    else:
        grade = "C" if missing == 1 else "D"
        basis = f"{missing} of {len(musts)} must-haves cannot be found on the page."
    return {**summary, "grade": grade, "grade_basis": basis}


def lens_signals(tailored: Dict[str, Any], pdf_text: Optional[str]) -> Dict[str, Any]:
    """The countable parts of HackerRank's intern rubric, computed in code."""
    resume = tailored or {}
    contact = resume.get("contact") or {}
    projects = [p for p in (resume.get("projects") or []) if isinstance(p, dict)]
    rows = _bullet_rows(resume)
    work_text = "\n".join(r["text"] for r in rows if r["section"] == "experience")
    work_text += "\n" + "\n".join(
        f"{e.get('title', '')} {e.get('company', '')}" for e in (resume.get("experience") or []) if isinstance(e, dict)
    )
    all_text = pdf_text if pdf_text else "\n".join(r["text"] for r in rows) + "\n" + _listed_text(resume)

    no_link = []
    tutorial = []
    for p in projects:
        blob = " ".join([str(p.get("name") or ""), str(p.get("tech") or ""), str(p.get("url") or "")]
                        + [str(b) for b in p.get("bullets") or []])
        if not _LINK_RE.search(blob):
            no_link.append(str(p.get("name") or "Untitled project"))
        if _TUTORIAL_RE.search(str(p.get("name") or "")):
            tutorial.append(str(p.get("name") or ""))

    ai_in_bullets = [r for r in rows if r["bullet"] is not None and _AI_RE.search(r["text"])]
    ai_evaluated = [r for r in ai_in_bullets if _EVAL_RE.search(r["text"])]
    if ai_evaluated:
        ai_band, ai_points = "built_and_evaluated", "8-10"
    elif ai_in_bullets:
        ai_band, ai_points = "built", "4-7"
    elif _AI_RE.search(_listed_text(resume)):
        ai_band, ai_points = "listed_only", "1-3"
    else:
        ai_band, ai_points = "none", "0"

    portfolio = str(contact.get("portfolio") or "").strip()
    portfolio_ok = bool(portfolio) and "github.com" not in portfolio.lower() and "linkedin.com" not in portfolio.lower()
    notes: List[str] = []
    if no_link:
        notes.append(
            f"{len(no_link)} project(s) have no link. HackerRank's rubric deducts 3-5 points per project "
            "without a GitHub link or live demo. Add the real repository or demo URL for each one."
        )
    if tutorial:
        notes.append("Tutorial-style project names score low in HackerRank's rubric (0-9 of 30): "
                     + ", ".join(tutorial) + ".")
    if ai_band == "listed_only":
        notes.append("AI tools appear only in Skills. HackerRank scores keyword-only AI at 1-3 of 10; "
                     "a project line that names the model, the data and how you checked the output scores 8-10.")
    elif ai_band == "built":
        notes.append("AI work is described but not measured. If you tested it (a test set, accuracy, latency), "
                     "saying so moves HackerRank's AI band from 4-7 to 8-10.")
    if not contact.get("linkedin"):
        notes.append("No LinkedIn link (+1 in HackerRank's rubric).")
    return {
        "projects_total": len(projects),
        "projects_without_link": no_link,
        "estimated_link_deduction": [3 * len(no_link), 5 * len(no_link)],
        "tutorial_projects": tutorial,
        "ai_fluency_band": ai_band,
        "ai_fluency_points": ai_points,
        "ai_evidence": [r["text"] for r in (ai_evaluated or ai_in_bullets)[:2]],
        "linkedin_bonus": 1 if contact.get("linkedin") else 0,
        "portfolio_bonus": 2 if portfolio_ok else 0,
        "github_profile": bool(contact.get("github")),
        "founder_evidence": bool(_FOUNDER_RE.search(work_text)),
        "early_engineer_evidence": bool(_EARLY_RE.search(work_text)),
        "open_source_evidence": bool(_OSS_RE.search(all_text or "")),
        "notes": notes,
    }


_SCREEN_SYSTEM = (
    "You screen job applications for one job. You see only the résumé text exactly as an "
    "applicant tracking system extracted it from the PDF. You know nothing else about this "
    "person. Unknown is unknown: never assume a skill, tool, scope or result that is not on "
    "the page. Every quote you return must be copied exactly from the résumé text."
)


def _screen_prompt(pdf_text: str, criteria: List[Dict[str, Any]], jd: Dict[str, Any], arch: str) -> str:
    from utils.llm_safety import wrap_untrusted
    crit_lines = "\n".join(f"  {c['id']} [{c['kind']}] {c['term']}" for c in criteria) or "  (none stated)"
    signals = "\n".join(f"  - {s}" for s in ARCHETYPE_SIGNALS.get(arch, ARCHETYPE_SIGNALS["general"]))
    return (
        f"JOB: {jd.get('job_title') or 'unknown title'} at {jd.get('company') or 'unknown company'}\n"
        f"ARCHETYPE: {arch}\n"
        "CRITERIA (\"must\" decides whether the applicant is shortlisted):\n" + crit_lines + "\n"
        "WHAT THIS HIRING MANAGER READS FOR:\n" + signals + "\n\n"
        "For each criterion return: id; verdict (meets | not_met | unclear); quote (the exact résumé "
        "words that prove it, or \"\"); where (job or project name); depth (mentioned | used | owned | led).\n"
        "Then: apparent_level (intern | new_grad | mid | senior | staff_plus); six_second_read (what the "
        "name line, the most recent role and its first bullet say together); reason_to_interview (one "
        "sentence that quotes the résumé); top_concern (one sentence).\n"
        "Also score HackerRank's public intern rubric from the page only, each with a short evidence quote: "
        "open_source 0-35 (contributions to OTHER people's projects; personal repos alone at most 10), "
        "self_projects 0-30 (complexity and real impact; tutorial apps low; projects without links lower), "
        "production 0-25 (jobs and internships), technical_skills 0-10, ai_fluency 0-10 (built and "
        "evaluated AI scores high; tools only listed scores 1-3). Score demonstrated evidence, not keywords.\n"
        "JSON only.\n\n"
        + wrap_untrusted("resume_text", pdf_text[:_PDF_TEXT_LIMIT])
    )


_SCREEN_SCHEMA = {
    "criteria": [{"id": str, "verdict": str, "quote": str, "where": str, "depth": str}],
    "apparent_level": str,
    "six_second_read": str,
    "reason_to_interview": str,
    "top_concern": str,
    "rubric": {
        "open_source": {"score": int, "evidence": str},
        "self_projects": {"score": int, "evidence": str},
        "production": {"score": int, "evidence": str},
        "technical_skills": {"score": int, "evidence": str},
        "ai_fluency": {"score": int, "evidence": str},
    },
}


def llm_screen(pdf_text: str, criteria: List[Dict[str, Any]], jd: Dict[str, Any], arch: str) -> Dict[str, Any]:
    """One temperature-0 read of the extracted text. Quotes are verified."""
    from services import claude_client
    from utils.llm_safety import guarded_system

    raw = claude_client.claude_json(
        prompt=_screen_prompt(pdf_text, criteria, jd, arch),
        max_tokens=3000,
        temperature=0.0,
        model=claude_client.CLAUDE_FLASH,
        schema=_SCREEN_SCHEMA,
        system=guarded_system(_SCREEN_SYSTEM),
    )
    raw = raw if isinstance(raw, dict) else {}
    known = {c["id"] for c in criteria}
    verdicts: Dict[str, Dict[str, Any]] = {}
    for item in raw.get("criteria") or []:
        if not isinstance(item, dict) or item.get("id") not in known:
            continue
        verdict = str(item.get("verdict") or "").strip().lower()
        verdict = verdict if verdict in {"meets", "not_met", "unclear"} else "unclear"
        quote = str(item.get("quote") or "").strip()
        quote_ok = bool(quote) and quote_on_page(quote, pdf_text)
        if verdict == "meets" and not quote_ok:
            verdict = "unclear"
        depth = str(item.get("depth") or "").strip().lower()
        verdicts[item["id"]] = {
            "verdict": verdict,
            "quote": quote if quote_ok else "",
            "quote_rejected": bool(quote) and not quote_ok,
            "where": str(item.get("where") or "")[:160],
            "depth": depth if depth in {"mentioned", "used", "owned", "led"} else "",
        }
    rubric: Dict[str, Any] = {}
    for key, cap in LENS_CATEGORY_MAX.items():
        cell = (raw.get("rubric") or {}).get(key) or {}
        try:
            score = int(cell.get("score") or 0)
        except (TypeError, ValueError):
            score = 0
        rubric[key] = {"score": max(0, min(cap, score)), "max": cap,
                       "evidence": str(cell.get("evidence") or "")[:300]}
    level = str(raw.get("apparent_level") or "").strip().lower()
    return {
        "criteria": verdicts,
        "apparent_level": level if level in {"intern", "new_grad", "mid", "senior", "staff_plus"} else "",
        "six_second_read": str(raw.get("six_second_read") or "")[:400],
        "reason_to_interview": str(raw.get("reason_to_interview") or "")[:400],
        "top_concern": str(raw.get("top_concern") or "")[:400],
        "rubric": rubric,
        "rubric_total": sum(c["score"] for c in rubric.values()),
        "rubric_max": sum(LENS_CATEGORY_MAX.values()),
    }


def screen(
    tailored: Dict[str, Any],
    jd_analysis: Dict[str, Any],
    *,
    source: Optional[Dict[str, Any]] = None,
    renderer=None,
    extra_grounding: Sequence[str] = (),
    use_llm: Optional[bool] = None,
) -> Dict[str, Any]:
    """Build the screener report for one tailored résumé. Read-only; fails open."""
    current_mode = mode()
    if current_mode == "off":
        return {"version": VERSION, "status": "skipped", "reason": "SCREENER_SIM=off"}
    use_llm = (current_mode == "full") if use_llm is None else use_llm
    jd = jd_analysis or {}
    arch = archetype(jd)
    criteria = criteria_from_jd(jd)

    pdf_text, pages, render_state = _render_text(tailored, renderer)
    parse = {"status": "unavailable", "pages": pages, "chars": 0, "checks": [], "render": render_state}
    if pdf_text is not None and pdf_text.strip():
        checks = _parse_checks(tailored, pdf_text)
        parse = {"status": "ok" if all(c["ok"] for c in checks) else "issues",
                 "pages": pages, "chars": len(pdf_text), "checks": checks, "render": render_state}

    rows: List[Dict[str, Any]] = []
    fixes: List[Dict[str, Any]] = []
    gaps: List[Dict[str, Any]] = []
    for crit in criteria:
        term = crit["term"]
        vis = visibility(term, tailored, pdf_text)
        ev = evidence(term, source, extra_grounding) if source else {"state": "unknown", "line": None}
        verdict = {"in_context": "meets", "listed_only": "weak"}.get(vis["state"], "not_met")
        row = {
            **crit,
            "visibility": vis["state"],
            "where": vis["where"],
            "dates": vis["dates"],
            "span": vis["span"],
            "recency_rank": vis.get("recency_rank"),
            "lost_in_render": bool(vis.get("lost_in_render")),
            "evidence": ev["state"],
            "source_line": (ev.get("line") or {}).get("text", ""),
            "verdict": verdict,
        }
        rows.append(row)
        if vis["state"] != "in_context":
            if ev["state"] == "proven":
                fixes.append({"criterion": crit["id"], "term": term, "kind": crit["kind"],
                              "action": "show_source_line", "entry": ev["line"]["label"],
                              "source_line": ev["line"]["text"]})
            elif ev["state"] in {"listed", "confirmed"} and vis["state"] == "not_visible":
                fixes.append({"criterion": crit["id"], "term": term, "kind": crit["kind"],
                              "action": "list_in_skills", "entry": "Skills", "source_line": ""})
            elif ev["state"] in {"gap", "implied"}:
                gaps.append({
                    "criterion": crit["id"], "term": term, "kind": crit["kind"],
                    "adjacent": ev["state"] == "implied",
                    "question": f"Have you used {term}? If yes, tell us in which job or project and we will show it.",
                })

    warnings: List[str] = []
    page_text = pdf_text if pdf_text else ""
    if page_text and _INJECTION_RE.search(page_text):
        warnings.append("The résumé contains text addressed to an AI screener. Employers treat this as "
                        "manipulation and drop the application; remove it.")
    if any(r["lost_in_render"] for r in rows):
        warnings.append("Some requirements are in the résumé data but missing from the PDF text an ATS reads.")

    report: Dict[str, Any] = {
        "version": VERSION,
        "status": "ok" if parse["status"] != "unavailable" else "partial",
        "mode": "full" if use_llm else "deterministic",
        "archetype": arch,
        "parse": parse,
        "criteria": rows,
        "summary": _grade(rows),
        "fixes": fixes,
        "gaps": gaps,
        "warnings": warnings,
        "lens": {"name": LENS_NAME, "source": LENS_SOURCE,
                 "signals": lens_signals(tailored, pdf_text)},
    }
    prior = tailored.get("screener") if isinstance(tailored.get("screener"), dict) else {}
    if prior.get("patched"):
        report["patched"] = prior["patched"]

    if use_llm and page_text.strip() and criteria:
        try:
            llm = llm_screen(page_text, criteria, jd, arch)
            for row in rows:
                row["screener"] = llm["criteria"].get(row["id"])
            report["screener"] = {k: v for k, v in llm.items() if k != "criteria"}
        except Exception as exc:  # advisory read; never break tailoring
            logger.warning("screener_sim LLM read failed (fails open): %s", exc)
            report["screener"] = {"status": "skipped", "reason": str(exc)[:200]}
    return report
