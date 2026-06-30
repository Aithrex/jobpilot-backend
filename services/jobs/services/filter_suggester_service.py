"""
Resume-aware filter suggester for the daily Apify pipeline.

Given a user's structured base resume, returns LinkedIn keyword phrases,
Workday job titles, custom role terms, and a recommended past-days window
that match the candidate's profile.

Intent-aware behavior:
  - We first run a deterministic resume profiler (resume_profiler.py) to
    detect the candidate's primary career intent (e.g. ai_ml, cloud_devops,
    backend) and a secondary intent for hybrid profiles (e.g. ML + Cloud).
  - The intent's curated title list seeds the suggestion — so an ML-focused
    candidate gets ML titles first, not generic SWE.
  - The generic SWE safety net is only added for early-career or generalist
    profiles, NOT for senior specialists where it would dilute results.
  - The LLM (FLASH tier) then refines the seed and adds tail variants. The deterministic
    seed is authoritative — anything the LLM drops is restored.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

from services.gemini_client import GEMINI_FLASH, GEMINI_PRO, gemini_json
from services.resume_profiler import GENERIC_SWE_TITLES, INTENTS, build_profile

logger = logging.getLogger(__name__)

_SUGGESTION_SCHEMA = {
    "headline": str,
    "rationale": str,
    "linkedin_keyword_sets": [str],
    "workday_titles": [str],
    "custom_role_terms": [str],
    "past_days": int,
    "preset_tags": [str],
}

# Synthesizer schema — what we ask the LLM (PRO tier) to return when generating
# personalized titles from the resume content itself.
_SYNTHESIZER_SCHEMA = {
    "detected_role": str,        # short label, e.g. "Full-Stack Backend Engineer (mid)"
    "rationale": str,            # 1-2 sentences explaining WHY based on resume evidence
    "seniority": str,            # "junior" | "mid" | "senior" — used to filter title variants
    "workday_titles": [str],     # 20-40 titles SPECIFIC to this candidate
    "linkedin_phrases": [str],   # 5-8 search phrases SPECIFIC to this candidate
    "anti_keywords": [str],      # 3-8 tokens to EXCLUDE (off-domain noise)
}


def _resume_summary(structured: Dict[str, Any]) -> Dict[str, Any]:
    """Slim the resume down to just what the model needs to refine filters."""
    skills = structured.get("skills") or {}
    flat_skills: List[str] = []
    if isinstance(skills, dict):
        for v in skills.values():
            if isinstance(v, list):
                flat_skills.extend(v)
    elif isinstance(skills, list):
        flat_skills = [str(s) for s in skills]

    experiences = []
    for exp in (structured.get("experience") or [])[:6]:
        experiences.append({
            "title": exp.get("title", ""),
            "company": exp.get("company", ""),
            "bullets": (exp.get("bullets") or [])[:3],
        })

    projects = []
    for p in (structured.get("projects") or [])[:5]:
        projects.append({
            "name": p.get("name", ""),
            "tech": p.get("tech", ""),
        })

    education = []
    for ed in (structured.get("education") or [])[:3]:
        education.append({
            "degree": ed.get("degree", ""),
            "institution": ed.get("institution", ""),
        })

    return {
        "summary": (structured.get("summary") or "")[:600],
        "skills": flat_skills[:40],
        "experience": experiences,
        "projects": projects,
        "education": education,
    }


def _preset_tag_for_intent(intent_key: str) -> str:
    """Map our internal intent keys to the short UI preset tags."""
    return {
        "ai_ml": "ai-ml",
        "data_science": "data",
        "data_eng": "data",
        "cloud_devops": "cloud-devops",
        "backend": "backend",
        "frontend": "frontend",
        "fullstack": "fullstack",
        "security": "security",
        "mobile": "mobile",
    }.get(intent_key, "backend")


def _synthesize_titles_from_resume(
    structured_resume: Dict[str, Any],
    profile: Dict[str, Any],
) -> Dict[str, Any]:
    """Generate personalized titles / phrases directly from the resume.

    This is the architectural fix for "every user gets the same cloud titles."
    Previously the suggester pulled from a STATIC per-intent template
    (INTENTS[primary]["target_titles"]) and forced the LLM to keep them. Now
    we ask the LLM (PRO tier) to read the actual resume content (titles held,
    projects, skills) and emit titles that match THIS candidate — not a
    cohort. Intent classification is included only as a hint to ground the
    model when the resume itself is ambiguous.

    Returns {} on LLM failure so the caller can fall back to the legacy
    static-seed path. Never raises.
    """
    primary = profile.get("primary_intent") or "backend"
    secondary = profile.get("secondary_intent")
    primary_label = INTENTS[primary]["label"]
    secondary_label = INTENTS[secondary]["label"] if secondary else None
    years = int(profile.get("experience_years") or 0)
    seniority_hint = "junior" if years <= 2 else ("senior" if years >= 7 else "mid")

    slim = _resume_summary(structured_resume)
    secondary_clause = f" + {secondary_label}" if secondary_label else ""
    prompt = (
        "You are a senior tech recruiter. Your job is to generate Workday "
        "job-title search terms and LinkedIn keyword phrases PERSONALIZED to a "
        "specific candidate's resume. The output drives an automated daily "
        "job-pipeline scrape that must surface BOTH the candidate's primary "
        "specialty AND realistic adjacent specializations — too narrow = "
        "candidate misses opportunities; too broad = noise. Aim for the right "
        "balance, leaning slightly broad on a hybrid resume.\n\n"
        f"INTENT HINT (NOT authoritative): {primary_label}{secondary_clause}, "
        f"~{seniority_hint} (≈{years}y experience).\n\n"
        f"CANDIDATE RESUME (JSON, slimmed):\n{slim}\n\n"
        "MANDATORY COVERAGE RULES — non-negotiable. Every example below is "
        "ILLUSTRATIVE across multiple role families to show the *shape* of "
        "good output — IGNORE any example whose stack doesn't match this "
        "candidate's resume. Use the candidate's actual tech stack words.\n\n"
        "  1. PRIMARY domain — generate AT LEAST 10 titles for the candidate's "
        "strongest signal. Include level variants AND stack-specific variants. "
        "Shape examples across different domains:\n"
        "       Frontend candidate → 'Frontend Engineer React', 'UI Engineer "
        "TypeScript', 'Web Engineer Next.js'\n"
        "       Backend candidate  → 'Backend Engineer Python', 'API Engineer "
        "Go', 'Server Engineer Java'\n"
        "       Cloud candidate    → 'Cloud Engineer AWS', 'DevOps Engineer "
        "Kubernetes', 'Platform Engineer Terraform'\n"
        "       Data candidate     → 'Data Engineer Spark', 'Analytics "
        "Engineer dbt', 'Data Platform Engineer'\n"
        "       ML/AI candidate    → 'ML Engineer PyTorch', 'Applied "
        "Scientist NLP', 'GenAI Engineer LLM'\n"
        "       Mobile candidate   → 'iOS Engineer Swift', 'Android Engineer "
        "Kotlin', 'Mobile Engineer React Native'\n"
        "       Security candidate → 'Security Engineer AppSec', 'DevSecOps "
        "Engineer', 'Cloud Security Engineer'\n"
        "    Pick the row that matches THIS candidate; the others are noise.\n"
        "  2. SECONDARY domain — if the resume shows clear evidence for a "
        "secondary role family (here: " + (secondary_label or "any cross-trained area") + "), "
        "generate AT LEAST 6 titles for it too. Examples of valid hybrid pairs:\n"
        "       Full-Stack       → frontend + backend titles\n"
        "       Cloud + Backend  → cloud/devops + backend titles\n"
        "       ML + Data Eng    → ML + data engineer titles\n"
        "       Backend + Data   → backend + data engineer titles\n"
        "    Do NOT force a secondary that isn't supported by the resume — "
        "a pure-frontend resume should NOT get backend titles forced in.\n"
        "  3. AI / ML adjacency — ONLY if the resume mentions LLMs, foundation "
        "models, RAG, vector DBs, agentic AI, or fine-tuning, include 3-5 AI "
        "infra titles (e.g. AI Platform Engineer, ML Infrastructure Engineer, "
        "GenAI Platform Engineer, MLOps Engineer). Otherwise omit.\n"
        "  4. Seniority variants — for the detected seniority, include 2-3 "
        'prefix variants: "Junior/Associate/New Grad/Graduate" for junior; '
        '"" (no prefix) and "II"/"Mid-Level" for mid; "Senior"/"Staff"/"Lead" '
        "for senior. If the candidate is a current MS / new grad, also include "
        "new-grad variants regardless of past experience years.\n"
        "  5. Stack-specific phrasing — bake in the candidate's actual stack "
        "tokens (look at their skills + experience bullets + project tech). "
        "If they show 'Postgres + Node + GraphQL', generate 'Backend Engineer "
        "Node', 'GraphQL Engineer', 'API Engineer Postgres'. If they show "
        "'React + TypeScript + Tailwind', generate 'Frontend Engineer React', "
        "'UI Engineer TypeScript' — NOT generic 'Software Engineer'.\n"
        "  6. Each title must be anchored to a tech engineering domain word "
        '(Engineer / Developer / Scientist / SRE / Analyst / Architect) — '
        'never just "Junior" or "Associate" alone.\n'
        "  7. ANTI-keywords: list 4-10 tokens that should NEVER appear in this "
        "candidate's feed. Examples vary by candidate:\n"
        "       For a pure frontend dev → 'Backend', 'Data Scientist', 'SRE'\n"
        "       For a backend dev       → 'Frontend', 'UI Designer', 'Data Analyst'\n"
        "       For a data scientist    → 'Frontend', 'Mobile', 'DevOps'\n"
        "       Universal noise         → 'Sales Engineer', 'Hardware Engineer', "
        "'Solutions Architect (Pre-Sales)', 'Manager', 'Director'\n\n"
        "OUTPUT QUOTAS — enforce strictly:\n"
        "  · workday_titles: MIN 22, MAX 45. Cover primary + secondary (if "
        "any) + AI adjacent (only if signal exists) + seniority variants.\n"
        "  · linkedin_phrases: MIN 7, MAX 10. Each 3-6 words. MUST mirror the "
        "domain coverage from workday_titles — every domain represented in "
        "workday_titles should have ≥1 LinkedIn phrase. Mix specific-stack "
        "phrases with broader fallback phrases.\n"
        "  · anti_keywords: MIN 4, MAX 10.\n\n"
        "Return JSON:\n"
        '  - "detected_role": one-line role summary using resume evidence. '
        'Shape examples (pick the matching one for this candidate, write your '
        'own using their actual stack):\n'
        '       "Frontend Engineer specializing in React + TypeScript, mid-level"\n'
        '       "Backend Engineer with Java + Spring Boot + Postgres, junior"\n'
        '       "Cloud / DevOps Engineer with AWS + Terraform + K8s, mid-level"\n'
        '       "ML Engineer focused on PyTorch + NLP, senior"\n'
        '  - "rationale": 1-2 sentences citing 2-3 specific resume signals.\n'
        '  - "seniority": "junior" | "mid" | "senior".\n'
        '  - "workday_titles": list (length 22-45).\n'
        '  - "linkedin_phrases": list (length 7-10).\n'
        '  - "anti_keywords": list (length 4-10).\n'
        "Return ONLY the JSON object, no commentary."
    )
    # Two-tier synth: Pro first (best quality), Flash on Pro failure or thin
    # output. Either result counts as success — the upstream backfill safety
    # net fills any gap from intent seeds. Falling back to empty {} only when
    # BOTH calls error or return zero titles (very rare).
    def _try_model(model_name: str, label: str) -> Dict[str, Any]:
        try:
            out = gemini_json(
                prompt=prompt,
                model=model_name,
                temperature=0.35,
                schema=_SYNTHESIZER_SCHEMA,
                max_tokens=2000,
            )
            if not isinstance(out, dict):
                logger.warning("synthesize/%s: non-dict output for %s", label, primary_label)
                return {}
            titles_local = [str(t).strip() for t in (out.get("workday_titles") or []) if t]
            phrases_local = [str(p).strip() for p in (out.get("linkedin_phrases") or []) if p]
            anti_local = [str(a).strip() for a in (out.get("anti_keywords") or []) if a]
            logger.info(
                "synthesize/%s: %s -> %d titles, %d phrases, %d anti for %s",
                label, model_name, len(titles_local), len(phrases_local), len(anti_local),
                primary_label,
            )
            if len(titles_local) < 3:
                # Threshold lowered from 5 → 3 — even thin output is more
                # personalized than the static seed list. The upstream
                # backfill helper will top it up from intent seeds.
                logger.warning(
                    "synthesize/%s: too few titles (%d), retrying / falling through",
                    label, len(titles_local),
                )
                return {}
            return {
                "detected_role": str(out.get("detected_role") or "")[:200].strip(),
                "rationale": str(out.get("rationale") or "")[:400].strip(),
                "seniority": str(out.get("seniority") or seniority_hint).strip(),
                "workday_titles": titles_local[:60],
                "linkedin_phrases": phrases_local[:8],
                "anti_keywords": anti_local[:10],
            }
        except Exception as e:
            logger.warning("synthesize/%s (%s) failed: %s", label, model_name, e)
            return {}

    # Try Pro first.
    result = _try_model(GEMINI_PRO, "pro")
    if result:
        return result
    # Pro failed or returned thin — retry with Flash. Flash is faster and has
    # different rate-limit pool, so this catches Pro's quota / timeout issues.
    logger.info("synthesize: Pro returned empty; retrying with Flash for %s", primary_label)
    result = _try_model(GEMINI_FLASH, "flash")
    if result:
        return result
    logger.warning("synthesize: BOTH Pro and Flash failed for %s; caller will use seed fallback", primary_label)
    return {}


def _resume_signature(structured_resume: Dict[str, Any]) -> str:
    """Stable short fingerprint of the resume content for logging.

    Used so we can confirm in production logs that different resumes
    actually produce different outputs (no stale-cache surprises).
    """
    import hashlib
    parts = [
        str((structured_resume.get("contact") or {}).get("name") or ""),
        str(structured_resume.get("summary") or "")[:200],
    ]
    for exp in (structured_resume.get("experience") or [])[:3]:
        parts.append(str(exp.get("title") or ""))
        parts.append(str(exp.get("company") or ""))
    blob = "|".join(parts).lower().strip()
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def suggest_filters(structured_resume: Dict[str, Any]) -> Dict[str, Any]:
    """Return a JSON suggestion bundle the UI can prefill onto the pipeline form.

    Architecture:
      1. Profile classifier runs first — gives us an intent HINT and the
         seniority estimate.
      2. The LLM (PRO tier) generates titles personalized to THIS resume (not a
         per-intent template). This is the authoritative output.
      3. If the LLM fails or returns too few titles, we fall back to the legacy
         static-seed path so the user is never left empty-handed.
      4. Anti-keywords from the synthesizer get exposed back to the UI so the
         pipeline scoring can use them to suppress off-domain noise.
    """
    profile = build_profile(structured_resume)
    primary = profile["primary_intent"]
    secondary = profile.get("secondary_intent")
    is_generalist = profile.get("is_generalist", False)
    confidence = profile.get("intent_confidence", 0.0)
    sig = _resume_signature(structured_resume)
    logger.info(
        "suggest_filters: resume_sig=%s primary=%s secondary=%s years=%s",
        sig, primary, secondary, profile.get("experience_years"),
    )

    primary_label = INTENTS[primary]["label"]
    secondary_label = INTENTS[secondary]["label"] if secondary else None

    # ── Resume-driven synthesis (the real fix) ────────────────────────
    synth = _synthesize_titles_from_resume(structured_resume, profile)
    used_synth = bool(synth)

    if used_synth:
        workday_titles = list(synth["workday_titles"])
        linkedin_phrases = list(synth["linkedin_phrases"])
        custom_role_terms = list(synth.get("anti_keywords") or [])
        detected_role = synth.get("detected_role") or primary_label
        rationale = synth.get("rationale") or ""
        headline = detected_role

        # Breadth safety net: even when synth runs, the LLM (PRO tier) sometimes
        # returns a narrow primary-only list (e.g. only Cloud Engineer
        # variants for a cloud+backend+AI candidate). Backfill from the
        # primary+secondary intent seeds so the feed has enough surface area
        # for adjacent specializations. Synth titles stay first (better
        # targeting); seeds fill any gap to reach a minimum of 20.
        MIN_TITLES = 20
        MIN_PHRASES = 5
        if len(workday_titles) < MIN_TITLES:
            seed_titles = list(profile.get("target_titles") or [])
            if secondary:
                seed_titles += list(INTENTS[secondary].get("target_titles") or [])[:6]
            workday_titles = _merge_preserving_order(workday_titles, seed_titles)
            logger.info(
                "synth: workday_titles too thin (%d), backfilled with seeds -> %d",
                len(synth["workday_titles"]), len(workday_titles),
            )
        if len(linkedin_phrases) < MIN_PHRASES:
            seed_phrases = list(profile.get("keyword_phrases") or [])
            linkedin_phrases = _merge_preserving_order(linkedin_phrases, seed_phrases)
    else:
        # Fallback path — keep the legacy static-seed behaviour so nobody is
        # left without filters when the LLM is rate-limited.
        logger.warning(
            "suggest_filters: synth failed for resume_sig=%s, falling back to static seeds",
            sig,
        )
        workday_titles = list(profile["target_titles"])
        linkedin_phrases = list(profile["keyword_phrases"])
        custom_role_terms = []
        detected_role = primary_label
        # Accurate rationale: the resume parse worked (profile got a confident
        # primary intent), but the LLM (PRO tier) AND Flash both errored or timed out.
        # The user should retry — this is usually transient (rate limit or
        # cold start) rather than a resume issue.
        rationale = (
            f"Classified resume as {primary_label}. AI title-synthesizer hit a "
            "transient error (rate limit / timeout) and fell back to a curated "
            "default set for this role. Click ↻ Re-suggest in 10-20 seconds — "
            "it usually succeeds on retry."
        )
        headline = primary_label

    # Custom role terms (adjacent skills) — use top skills from resume.
    # This stays grounded in the candidate's actual stack.
    if not custom_role_terms:
        skills = structured_resume.get("skills") or {}
        flat_skills: List[str] = []
        if isinstance(skills, dict):
            for v in skills.values():
                if isinstance(v, list):
                    flat_skills.extend(str(s) for s in v)
        elif isinstance(skills, list):
            flat_skills = [str(s) for s in skills]
        custom_role_terms = [s.strip() for s in flat_skills if s and s.strip()][:8]

    # Generic SWE safety net only for early-career / generalist profiles AND
    # only when the synthesizer didn't already cover SWE titles.
    add_generalist = is_generalist or int(profile.get("experience_years") or 0) <= 2
    if add_generalist:
        workday_titles = _merge_preserving_order(workday_titles, GENERIC_SWE_TITLES)

    workday_titles = workday_titles[:60]
    linkedin_phrases = linkedin_phrases[:8]

    # past_days default — fast loop for active applicants
    past_days = 1

    # preset_tags — UI badge to confirm classification
    preset_tags: List[str] = []
    primary_tag = _preset_tag_for_intent(primary)
    if primary_tag not in preset_tags:
        preset_tags.append(primary_tag)
    if secondary:
        sec_tag = _preset_tag_for_intent(secondary)
        if sec_tag not in preset_tags:
            preset_tags.append(sec_tag)
    preset_tags = preset_tags[:5]

    coerced = {
        "headline": str(headline)[:120],
        "rationale": str(rationale)[:400],
        "linkedin_keyword_sets": linkedin_phrases,
        "workday_titles": workday_titles,
        "custom_role_terms": custom_role_terms[:12],
        "past_days": past_days,
        "preset_tags": preset_tags,
    }

    logger.info(
        "suggest_filters: resume_sig=%s used_synth=%s -> %d titles, %d phrases (first: '%s')",
        sig, used_synth, len(workday_titles), len(linkedin_phrases),
        workday_titles[0] if workday_titles else "(none)",
    )

    # Echo the resolved profile so the UI / client can show "we detected: X" and
    # so downstream callers (run_pipeline) can avoid re-deriving it.
    coerced["intent"] = {
        "primary": primary,
        "primary_label": primary_label,
        "secondary": secondary,
        "secondary_label": secondary_label,
        "confidence": confidence,
        "is_generalist": is_generalist,
    }
    # Also surface the synthesizer's debug info so the UI can show a "Based
    # on your resume: …" banner and the user can sanity-check.
    coerced["synth_meta"] = {
        "used_synth": used_synth,
        "resume_sig": sig,
    }

    # ------------------------------------------------------------------
    # Per-domain "groups" breakdown — lets the Smart Filters UI render
    # the resume's broader coverage (primary intent + secondary intent +
    # all adjacent intents that scored above a soft floor) and let the
    # user "+ Add to pipeline" each group selectively. The pipeline
    # workday_titles list contains primary+secondary; groups also exposes
    # adjacent specialties (e.g. an ML candidate's "Data Science" tail).
    # ------------------------------------------------------------------
    intent_scores = profile.get("intent_scores") or {}
    # Lowered floor 0.15 → 0.08 (and base 3.0 → 1.5) so more adjacent intent
    # groups surface in the Smart Filters tab. Users wanted broader coverage
    # while still filtering out near-zero signals.
    soft_floor = max(1.5, profile.get("intent_scores", {}).get(primary, 0.0) * 0.08)
    groups = []
    for intent_key, spec in INTENTS.items():
        score = float(intent_scores.get(intent_key, 0.0))
        if intent_key == primary:
            kind = "primary"
        elif intent_key == secondary:
            kind = "secondary"
        elif score >= soft_floor:
            kind = "adjacent"
        else:
            continue
        groups.append({
            "intent": intent_key,
            "label": spec["label"],
            "kind": kind,
            "score": round(score, 2),
            "tag": _preset_tag_for_intent(intent_key),
            "workday_titles": list(spec["target_titles"]),
            "linkedin_phrases": list(spec["keyword_phrases"]),
            "custom_role_terms": sorted(spec["core_skills"])[:8],
        })
    # Order: primary, secondary, then adjacent by score desc.
    kind_rank = {"primary": 0, "secondary": 1, "adjacent": 2}
    groups.sort(key=lambda g: (kind_rank.get(g["kind"], 3), -g["score"]))
    coerced["groups"] = groups

    return coerced


def _merge_preserving_order(first: List[str], second: List[str]) -> List[str]:
    """Concatenate two string lists, dedup case-insensitively, keep first's order."""
    seen: set = set()
    out: List[str] = []
    for item in list(first) + list(second):
        s = (item or "").strip()
        if not s:
            continue
        key = s.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


def _coerce(raw: Any) -> Dict[str, Any]:
    """Defensive coercion: clamp counts, dedupe, and bound past_days."""
    if not isinstance(raw, dict):
        raw = {}

    def _str_list(key: str, max_items: int) -> List[str]:
        items = raw.get(key) or []
        if not isinstance(items, list):
            return []
        seen: set = set()
        out: List[str] = []
        for item in items:
            s = str(item).strip()
            if not s or s.lower() in seen:
                continue
            seen.add(s.lower())
            out.append(s)
            if len(out) >= max_items:
                break
        return out

    try:
        past_days = int(raw.get("past_days") or 1)
    except (TypeError, ValueError):
        past_days = 1
    past_days = max(1, min(past_days, 14))

    return {
        "headline": str(raw.get("headline") or "Resume-derived filters")[:120],
        "rationale": str(raw.get("rationale") or "")[:400],
        "linkedin_keyword_sets": _str_list("linkedin_keyword_sets", 8),
        "workday_titles": _str_list("workday_titles", 60),
        "custom_role_terms": _str_list("custom_role_terms", 12),
        "past_days": past_days,
        "preset_tags": _str_list("preset_tags", 5),
    }
