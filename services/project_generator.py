"""
Project Generator — generates a single grounded project per invocation.

Called by ContentAugmenter when page fill is below threshold and project
count < 3, regardless of whether the original resume already has projects.
Output is validated against allowed tech from the resume before being injected.

Rules:
- Ground the project in the candidate's demonstrated skills and experience context
- Match the JD domain (use technologies that align with the JD AND exist in the resume)
- No fake company names, production deployment claims, or invented metrics
- dates field is always empty string
- tech field only contains technologies present in the resume skills or bullets
- bullets: 3 realistic, grounded descriptions — what was built and how
"""
import logging
import re
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Floor below which a generated project is rejected. Matches the bullet
# allocator's per-entry floor (services/bullet_allocator.py _FLOOR_PER_ENTRY):
# a project the allocator would keep needs at least this many good bullets.
# The generator over-generates up to `bullets_per_project` ranked bullets; the
# allocator downstream trims to what fits the page.
_MIN_PROJECT_BULLETS = 2


# Shared bullet-quality block injected into both single and batch prompts.
# Centralizes "what makes a good project bullet" so the two prompts can't drift.
# This is the single most important text in the generator — it's what turns a
# tag-cloud bullet ("Built FastAPI microservice ingesting Kafka events with
# Redis caching to detect anomalies") into a defensible-in-interview bullet
# ("Wrote a sliding-window outlier detector that flags transactions deviating
# beyond 3σ from a user's 30-day baseline").
_BULLET_QUALITY_BLOCK = (
    "BULLET QUALITY — THE REAL ENGINEER TEST (ATS-friendly + interview-defensible)\n"
    "Each bullet must do THREE things at the same time:\n"
    "  1. Read like something the candidate would say out loud in an interview.\n"
    "  2. Survive an ATS keyword scan by naming the right tech ONCE, in context.\n"
    "  3. Open with a strong action verb so a recruiter scanning the left edge\n"
    "     of the page sees impact in two seconds.\n\n"
    "STRUCTURE OF EVERY BULLET — follow this shape:\n"
    "  <Strong action verb> + <specific noun (the thing built)> + "
    "<concrete detail: a decision, trade-off, mechanism, or outcome> + "
    "<optional single metric>\n\n"
    "STRONG ACTION VERBS — open EVERY bullet with one of these. Vary them across\n"
    "the bullets; do not start three bullets with 'Built'.\n"
    "  Building: Architected, Engineered, Designed, Implemented, Built, Developed,\n"
    "            Wrote, Modeled, Shipped, Delivered.\n"
    "  Improving: Optimized, Reduced, Accelerated, Streamlined, Refactored,\n"
    "             Hardened, Tuned, Cut.\n"
    "  Deciding:  Chose, Selected, Replaced, Migrated, Consolidated, Standardized.\n"
    "  Owning:    Led, Drove, Established, Spearheaded, Owned, Automated,\n"
    "             Integrated, Indexed, Partitioned, Sharded.\n\n"
    "EACH BULLET MUST CARRY EXACTLY ONE OF THESE FOUR SIGNALS — pick the one that\n"
    "fits, do NOT try to cram all four into a single bullet:\n"
    "  (1) A decision + reason — 'Chose materialized views over an in-memory cache\n"
    "      because the aggregation window exceeded available memory.'\n"
    "  (2) A specific thing built or designed — 'Wrote a sliding-window outlier\n"
    "      detector flagging transactions deviating beyond 3σ from a 30-day user\n"
    "      baseline.'\n"
    "  (3) A trade-off accepted — 'Traded ~200ms of added latency for higher\n"
    "      accuracy by joining against historical baselines before scoring.'\n"
    "  (4) A concrete problem solved — 'Handled out-of-order events by buffering\n"
    "      30 seconds before scoring, eliminating false positives on late-arriving\n"
    "      transactions.'\n\n"
    "ATS-FRIENDLY TECH NAMING — read this carefully, it's the difference between\n"
    "passing and failing modern AI screeners:\n"
    "  • Each bullet names EXACTLY ONE technology from the JD-anchor list, spelled\n"
    "    the way the JD spells it. This is what ATS keyword scanners look for.\n"
    "  • Naming TWO OR MORE tech names in one bullet creates a 'keyword stuffing'\n"
    "    pattern modern AI screeners (and recruiters) recognize and downgrade.\n"
    "  • Across the bullets, cover DIFFERENT JD-anchor technologies — do not name\n"
    "    the same tech in every bullet. The 'tech:' field already lists the full\n"
    "    stack; bullets don't repeat it.\n"
    "  • The tech name must appear as the SUBJECT of a decision or action, NOT as\n"
    "    a tag at the end.\n"
    "    Good: 'Indexed flagged transactions in Elasticsearch for sub-second\n"
    "           forensic queries.'\n"
    "    Bad:  '...using Elasticsearch and Kibana for visualization.'\n\n"
    "HARD 'DO NOT DO' RULES — bullets violating these will be rejected:\n"
    "  • NO semicolons mid-bullet. A semicolon almost always means two unrelated\n"
    "    thoughts stuffed together for keyword density. Split or cut.\n"
    "  • NO tool-chain sentences. Patterns like 'Built X with Y and Z using A and\n"
    "    B' where every capitalized word is a tool name are banned. If a bullet\n"
    "    lists 3+ tool names, it has zero signal — rewrite.\n"
    "  • NO generic direct objects. 'Built service', 'Developed application',\n"
    "    'Implemented system', 'Created platform' — meaningless. The noun must\n"
    "    name what was SPECIFICALLY built: 'fraud rule engine', 'outlier\n"
    "    detector', 'back-pressure controller', 'idempotent job runner'.\n"
    "  • NO weak openers. Never 'Worked on', 'Helped', 'Was involved in',\n"
    "    'Participated in', 'Assisted with', 'Utilized', 'Leveraged'.\n"
    "  • NO filler adjectives. Drop 'robust', 'scalable', 'comprehensive',\n"
    "    'cutting-edge', 'innovative', 'seamless', 'state-of-the-art'.\n"
    "  • AT MOST ONE METRIC per bullet. Two metrics in one sentence is padding —\n"
    "    pick the one that matters.\n"
    "  • BULLET LENGTH: 100-150 characters. Long enough for one real thought,\n"
    "    short enough that every word earns its place.\n\n"
    "SELF-CHECK BEFORE EMITTING EACH BULLET — answer yes to all four:\n"
    "  1. Does it open with a strong action verb (from the list above)?\n"
    "  2. Does it name EXACTLY ONE JD-anchor technology — not zero, not two?\n"
    "  3. Does the action own a SPECIFIC noun an interviewer could probe with\n"
    "     'why did you do it that way?' — and could the candidate answer?\n"
    "  4. Could this exact bullet appear on five other candidates' resumes? If\n"
    "     yes, it's generic — rewrite with a specific detail.\n\n"
    "COMPARISON — this is exactly what to AVOID vs what to WRITE:\n"
    "  REJECTED (keyword stuffing, no decision, ATS recognizes the pattern):\n"
    "    'Built FastAPI microservice ingesting transaction events from Kafka\n"
    "    topics; implemented async processing with Redis caching to detect\n"
    "    spending anomalies within under one second latency.'\n"
    "    Why bad: 6 tools, semicolon splice, generic 'microservice' noun, no\n"
    "    decision an interviewer can probe.\n\n"
    "  ACCEPTED (one tech each, strong verbs, specific nouns, defensible):\n"
    "    'Wrote a sliding-window outlier detector in Python that flags\n"
    "     transactions deviating beyond 3σ from a user 30-day baseline.'\n"
    "    'Chose Kafka over a polling queue to keep ingestion lag under one\n"
    "     second even during evening traffic peaks.'\n"
    "    'Indexed flagged transactions in Elasticsearch so forensic queries on\n"
    "     false-positives return in under 200ms.'\n"
    "    Why good: each opens with a varied strong verb (Wrote / Chose / Indexed),\n"
    "    names exactly ONE JD-anchor tech (Python / Kafka / Elasticsearch), owns\n"
    "    a specific noun (outlier detector / polling queue / forensic queries),\n"
    "    and carries a decision or trade-off the candidate can defend.\n"
)


def _summarize_experience_bullets(original_resume: Dict[str, Any], max_chars: int = 1800) -> str:
    """Build a compact 'avoid duplicating these' block for project prompts.

    Lists each experience entry's bullets so the generator can see what
    territory the resume already covers. Returns an empty string when the
    candidate has no experience — in that case there's nothing to avoid.
    """
    experience = original_resume.get("experience") or []
    if not experience:
        return ""

    lines = []
    for exp in experience:
        company = (exp.get("company") or "").strip() or "Experience"
        title = (exp.get("title") or "").strip()
        header = f"  • {company} — {title}" if title else f"  • {company}"
        lines.append(header)
        for b in (exp.get("bullets") or []):
            text = str(b).strip()
            if text:
                lines.append(f"    - {text}")

    joined = "\n".join(lines)
    if len(joined) > max_chars:
        joined = joined[:max_chars].rsplit("\n", 1)[0] + "\n    [...]"
    return joined


# Weak bullet openers that signal "I wasn't really driving this work." Anything
# starting with one of these phrases (case-insensitive, optional leading bullet
# glyph stripped) gets dropped by the post-check.
_WEAK_BULLET_OPENERS = (
    "worked on",
    "helped",
    "was involved in",
    "was involved with",
    "participated in",
    "assisted with",
    "assisted in",
    "utilized",
    "leveraged",
    "was responsible for",
    "responsible for",
    "tasked with",
    "supported the",
    "contributed to",
)

# Lowercase tech-stack tokens used to count "named technologies" inside a
# single bullet. Kept conservative so we don't false-flag English words —
# only well-known tools/languages/frameworks appear here.
_BULLET_TECH_TOKENS = frozenset({
    # Languages
    "python", "java", "javascript", "typescript", "go", "golang", "rust",
    "ruby", "php", "scala", "kotlin", "swift", "csharp",
    # Web / API frameworks
    "fastapi", "flask", "django", "spring", "springboot", "express",
    "react", "angular", "vue", "next", "nextjs", "node", "nodejs",
    "rails", "laravel", "dotnet", "aspnet", "blazor", "graphql", "grpc",
    # Data / messaging
    "postgres", "postgresql", "mysql", "mariadb", "oracle", "sqlite",
    "mongo", "mongodb", "redis", "memcached", "cassandra", "dynamodb",
    "kafka", "rabbitmq", "rabbit", "sqs", "sns", "kinesis", "pubsub",
    "elasticsearch", "opensearch", "solr",
    # Observability
    "prometheus", "grafana", "datadog", "splunk", "elk", "logstash",
    "kibana", "fluentd", "loki", "tempo", "jaeger", "otel", "opentelemetry",
    # Cloud / infra
    "aws", "gcp", "azure", "docker", "kubernetes", "k8s",
    "terraform", "ansible", "helm", "jenkins", "argocd", "ecs", "eks",
    "ec2", "s3", "lambda", "rds", "cloudwatch", "cloudfront",
})

# Pre-compiled patterns used by the bullet-quality post-check.
_SEMICOLON_PATTERN = re.compile(r";")
_BULLET_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9.+#]+")


class ProjectGenerator:
    """Generates a single grounded project entry per invocation."""

    @staticmethod
    def _bullet_passes_quality_check(bullet: str) -> "tuple[bool, str]":
        """Deterministic gate behind the BULLET QUALITY prompt block.

        Drops bullets that obviously violate the hard rules. Used as a
        safety net — the prompt asks the model to follow these rules,
        but when it slips, we catch the worst offenders here so they
        don't reach the rendered resume.

        Returns (ok, reason). reason is the failure label when ok=False
        and empty when ok=True.
        """
        if not bullet or not bullet.strip():
            return False, "empty"

        # Strip a leading bullet glyph if present so opener checks work
        # whether the model included one or not.
        stripped = re.sub(r"^[-•*\s]+", "", bullet).strip()

        # Hard rule: no semicolons mid-bullet — almost always a sign of
        # two thoughts stitched together for keyword density.
        if _SEMICOLON_PATTERN.search(stripped):
            return False, "contains_semicolon"

        # Hard rule: no weak openers.
        lower = stripped.lower()
        for weak in _WEAK_BULLET_OPENERS:
            if lower.startswith(weak):
                return False, f"weak_opener:{weak}"

        # Hard rule: too many named technologies in one bullet (>= 3) is
        # keyword stuffing. The 'tech:' field already carries the stack.
        tokens_lower = [t.lower() for t in _BULLET_TOKEN_PATTERN.findall(stripped)]
        tech_hits = [t for t in tokens_lower if t in _BULLET_TECH_TOKENS]
        # Use distinct tech tokens — repeating one tech name doesn't count
        # against the bullet (e.g. "indexed in Elasticsearch and queried
        # Elasticsearch" is still one tech).
        if len(set(tech_hits)) >= 3:
            return False, f"tech_stuffing:{sorted(set(tech_hits))[:5]}"

        # Soft rule (warn-only, not a reject): bullet length. We surface
        # outliers via log but don't drop them — short bullets sometimes
        # carry the strongest signal ("Chose materialized views over an
        # in-memory cache for the 30-day window.").
        # No-op here on purpose.

        return True, ""

    def generate(
        self,
        original_resume: Dict[str, Any],
        jd_analysis: Dict[str, Any],
        bullets_per_project: int = 5,
    ) -> Optional[Dict[str, Any]]:
        """Generate one project grounded in the candidate's skills and the JD domain.

        Args:
            bullets_per_project: how many ranked bullets the generated project
                should emit (best-first). Default 5 over-generates so the
                downstream bullet_allocator can keep the strongest that fit the
                page (it never goes below 2 per project). Callers may pass a
                smaller target.

        Returns a project dict {name, dates, bullets, tech} or None if generation
        fails validation or an error occurs.
        """
        from services.gemini_client import gemini_json
        from schemas.resume_schemas import flatten_skills, build_resume_text

        # Build context strings
        all_skills = flatten_skills(original_resume)
        resume_text = build_resume_text(original_resume)
        skills_text = ", ".join(all_skills) if all_skills else "general software development"

        jd_title = jd_analysis.get("job_title", "software engineering")
        jd_required = jd_analysis.get("required_skills", [])
        jd_keywords = jd_analysis.get("keywords", [])
        jd_industry = jd_analysis.get("industry", "")

        # Identify JD-relevant skills that the candidate actually has
        resume_text_lower = resume_text.lower()
        all_skills_lower = {s.lower() for s in all_skills}
        relevant_tech = [
            s for s in (jd_required + jd_keywords)
            if s.lower() in all_skills_lower
            or re.search(r'\b' + re.escape(s.lower()) + r'\b', resume_text_lower)
        ]
        relevant_tech_str = ", ".join(relevant_tech[:10]) if relevant_tech else skills_text[:200]
        experience_block = _summarize_experience_bullets(original_resume)

        # JD-required skills the candidate plausibly has — the project's
        # primary anchor (project should showcase these, not random tech).
        jd_required_in_resume = [
            s for s in jd_required
            if s.lower() in all_skills_lower
            or re.search(r'\b' + re.escape(s.lower()) + r'\b', resume_text_lower)
        ]
        jd_anchor = ", ".join(jd_required_in_resume[:8]) if jd_required_in_resume else ""

        prompt = (
            "You are generating a single portfolio project entry for a resume.\n"
            "Frame it as a PERSONAL / SIDE / OPEN-SOURCE project the candidate built on their\n"
            "own time. The project should REINFORCE the candidate's experience — same domain,\n"
            "same tech, same problem area as their day job is EXPECTED and GOOD. This is what\n"
            "tells a recruiter the candidate knows the area well enough to explore it outside\n"
            "of work, and lets them speak to it fluently in an interview.\n\n"
            "PROJECT MUST BE ALL THREE: defensible, JD-aligned, reinforcing-not-copying.\n\n"
            "  (A) DEFENSIBLE — the candidate could actually build this with the tech in their\n"
            "      profile. No fake stacks, no domains they have no exposure to, no fabricated\n"
            "      metrics. Drawing the project from the candidate's day-job tech and domain is\n"
            "      encouraged — that is precisely what makes it defensible. A technical\n"
            "      interviewer asking 'tell me more' should get a coherent answer rooted in\n"
            "      work the candidate already does.\n\n"
            "  (B) JD-ALIGNED — the project must SHOWCASE the JD's required skills, not the\n"
            "      candidate's miscellaneous stack. Anchor the project's purpose to a problem\n"
            "      that NATURALLY USES the JD-required skills listed below. If the JD wants\n"
            "      C#/ASP.NET/Angular/Oracle and the candidate plausibly knows them, the\n"
            "      project should be a C#/ASP.NET/Angular/Oracle project — not a Python/ELK\n"
            "      project just because the candidate also knows Python and ELK.\n\n"
            "  (C) REINFORCE, DON'T COPY — the project SHOULD overlap with experience in domain,\n"
            "      tech, and problem area. A recruiter expects to see 'they worked on X at\n"
            "      $company AND built a similar X on their own time' — that's a strength, not\n"
            "      a weakness. The ONLY thing forbidden is COPY-PASTING an experience bullet.\n"
            "      Do not reuse the exact wording, structure, or metric of an experience bullet.\n"
            "      Write the project as if explaining a separate piece of code that lives in\n"
            "      the candidate's own GitHub — different scope, different consumer, different\n"
            "      framing of the same area is exactly right.\n"
            "      ALLOWED:\n"
            "        • Experience: 'Maintained Kafka telemetry ingestion at GE HealthCare\n"
            "          serving clinical devices across 12 sites.'\n"
            "          Project: 'Kafka Device Telemetry Toolkit — a personal library wrapping\n"
            "          common patterns for ingesting device telemetry over Kafka, with examples\n"
            "          showing producer back-pressure and consumer fan-out.' Same area, fresh\n"
            "          framing as a library. ALLOWED.\n"
            "        • Experience: 'Built ELK-based log alerting at HealthPlix.'\n"
            "          Project: 'Log Anomaly Detection Dashboard — Elasticsearch + Kibana\n"
            "          sandbox that experiments with anomaly rules on synthetic log data.'\n"
            "          Same ELK family, candidate's own playground. ALLOWED.\n"
            "      FORBIDDEN (only this one thing):\n"
            "        • Experience: 'Built Kafka anomaly-detection pipeline for clinical devices\n"
            "          at GE, reducing false alerts by 40%.'\n"
            "          Project bullet: 'Built Kafka anomaly-detection pipeline for clinical\n"
            "          devices, reducing false alerts by 40%.' ← Identical text. REJECTED.\n"
            "      Bottom line: write a NEW bullet in the candidate's voice, even if it covers\n"
            "      the same territory. Do not paste the experience bullet.\n\n"
            "WORKFLOW — follow IN ORDER (do not start with the name):\n"
            "  STEP 1: List the JD-required skills the candidate plausibly knows (provided below).\n"
            "          These are your project's tech anchors.\n"
            "  STEP 2: Read the EXPERIENCE BULLETS block. The project SHOULD live in the same\n"
            "          territory — same domain, same tech. Use it as inspiration, not as text\n"
            "          to copy verbatim.\n"
            "  STEP 3: Pick a personal/side project PURPOSE that uses the JD anchors (Step 1)\n"
            "          and reinforces the candidate's experience area (Step 2). Write the\n"
            "          one-sentence purpose into 'purpose'.\n"
            "  STEP 4: Write the bullets in the candidate's own voice — each must reference at\n"
            "          least one JD-anchor skill naturally. Do NOT copy experience bullets'\n"
            "          wording or metrics.\n"
            "  STEP 5: ONLY NOW derive the NAME from the purpose. Name = WHAT it does, never\n"
            "          WHO built it.\n\n"
            + _BULLET_QUALITY_BLOCK + "\n"
            "STRICT RULES — violations will be rejected:\n"
            "1. Use ONLY technologies that appear in the candidate's skills or experience below.\n"
            "2. Do NOT invent fake company names, production systems, or client work.\n"
            "3. Do NOT invent metrics, percentages, or numbers.\n"
            "4. The project must be a realistic personal or open-source project that a developer "
            "would actually build and put on GitHub — not a generic label or placeholder.\n"
            "5. dates: return empty string ''.\n"
            "6. tech: comma-separated list of ONLY technologies present in the candidate's profile.\n"
            f"7. bullets: up to {bullets_per_project} bullets, ordered best-first (strongest, most "
            f"JD-relevant first). Each ~100-150 chars, ending with a period.\n"
            "   Bullet quality is governed by the BULLET QUALITY block above — follow it exactly.\n"
            "   Each bullet must pass the four-question self-check before being emitted.\n"
            "8. PROJECT NAME RULES — the name describes WHAT the project does:\n"
            "   GOOD NAMES: 'Payment Fraud Detection API', 'Real-Time Log Aggregator', "
            "'E-Commerce Search Engine', 'Cloud Cost Optimizer', 'Distributed Task Queue', "
            "'Sentiment Analysis Pipeline', 'API Rate Limiter Service'.\n"
            "   BAD NAMES (auto-rejected — name MUST NOT contain any of these words/patterns):\n"
            "     - The JD's job title (e.g. 'Backend Engineer Project', 'Full Stack Developer ...')\n"
            "     - The candidate's role title\n"
            "     - 'Portfolio', 'Project', 'Demo', 'Application', 'Platform' as filler\n"
            "     - Lists of techs in the name (e.g. 'Python & AWS Integration Platform')\n"
            "     - Anything describing WHO built it or WHAT ROLE it's for\n\n"
            f"=== JD CONTEXT ===\n"
            f"JD role: {jd_title}" + (f" in {jd_industry}" if jd_industry else "") + "\n"
            f"JD-REQUIRED SKILLS THE CANDIDATE PLAUSIBLY KNOWS (PRIMARY ANCHORS — use these for the project): "
            f"{jd_anchor or '(none — fall back to general candidate skills, but only those most JD-adjacent)'}\n"
            f"JD-relevant tech (broader, secondary): {relevant_tech_str}\n\n"
            f"=== CANDIDATE'S OWN SKILLS (for plausibility checks; tech must come from this list) ===\n"
            f"{skills_text[:400]}\n\n"
            + (
                "=== EXPERIENCE BULLETS (DO NOT DUPLICATE these problems/solutions in your project) ===\n"
                f"{experience_block}\n\n"
                if experience_block else ""
            )
            +
            "Return a JSON object with EXACTLY this structure:\n"
            "{\n"
            '  "purpose": "One sentence: what real problem does this project solve, using the JD-required skills, in a domain NOT covered by experience above?",\n'
            '  "name": "Project Name (derived from purpose — NOT starting from the name)",\n'
            '  "dates": "",\n'
            '  "bullets": [\n'
            '    "Bullet 1 (~100-150 chars, action verb + technology + outcome)",\n'
            '    "Bullet 2",\n'
            '    "Bullet 3"\n'
            "  ],\n"
            '  "tech": "Tech1, Tech2, Tech3"\n'
            "}\n\n"
            f"=== CANDIDATE RESUME CONTEXT ===\n{resume_text[:3000]}"
        )

        try:
            from services.gemini_client import GEMINI_FLASH
            PROJECT_SCHEMA = {
                "purpose": str,
                "name": str,
                "dates": str,
                "bullets": [str],
                "tech": str
            }
            # Use FLASH for speed — project quality is governed by the prompt,
            # and FLASH is ~2-3x faster for structured JSON output.
            result = None
            try:
                result = gemini_json(
                    prompt,
                    max_tokens=12000,
                    temperature=0.3,
                    model=GEMINI_FLASH,
                    schema=PROJECT_SCHEMA
                )
            except Exception as e:
                logger.warning("ProjectGenerator: FLASH call failed: %s", e)

            if not isinstance(result, dict):
                logger.warning("ProjectGenerator: all LLM models failed — result type: %s", type(result))
                return None

            logger.info("ProjectGenerator: LLM returned project name='%s', tech='%s'",
                        result.get('name', '?'), result.get('tech', '?'))

        except Exception as e:
            logger.warning("ProjectGenerator: LLM call failed: %s", e)
            return None

        project = self._validate_and_clean(result, original_resume, jd_analysis, bullets_per_project)
        if project is None:
            logger.warning("ProjectGenerator: AI project failed validation (name='%s')",
                           result.get('name', '?') if isinstance(result, dict) else '?')
            return None

        # Reject if it restates an existing experience bullet.
        from services.content_augmenter import ContentAugmenter
        original_experience = original_resume.get("experience") or []
        overlaps, reason = ContentAugmenter._overlaps_experience(project, original_experience)
        if overlaps:
            logger.warning(
                "ProjectGenerator: project '%s' rejected — %s",
                project.get("name"), reason,
            )
            return None

        return project

    def generate_batch(
        self,
        count: int,
        original_resume: Dict[str, Any],
        jd_analysis: Dict[str, Any],
        existing_projects: list = None,
        bullets_per_project: int = 5,
        rejections_sink: list = None,
    ) -> list:
        """Generate multiple projects in a single LLM call for speed.

        Args:
            bullets_per_project: how many ranked bullets each generated project
                should emit (best-first). Default 5 over-generates so the
                downstream bullet_allocator keeps the strongest that fit the
                page (never below 2 per project).
            rejections_sink: optional list that receives one dict per rejected
                project for diagnostic surfacing upstream. Caller pre-allocates
                the list; this method appends. Each dict has keys
                {"name", "reason"}.

        Returns a list of validated project dicts (may be fewer than `count`
        if some fail validation or are duplicates).
        """
        if rejections_sink is None:
            rejections_sink = []
        from services.gemini_client import gemini_json, GEMINI_FLASH
        from schemas.resume_schemas import flatten_skills, build_resume_text

        if count <= 0:
            return []

        all_skills = flatten_skills(original_resume)
        resume_text = build_resume_text(original_resume)
        skills_text = ", ".join(all_skills) if all_skills else "general software development"

        jd_title = jd_analysis.get("job_title", "software engineering")
        jd_required = jd_analysis.get("required_skills", [])
        jd_keywords = jd_analysis.get("keywords", [])
        jd_industry = jd_analysis.get("industry", "")

        resume_text_lower = resume_text.lower()
        all_skills_lower = {s.lower() for s in all_skills}
        relevant_tech = [
            s for s in (jd_required + jd_keywords)
            if s.lower() in all_skills_lower
            or re.search(r'\b' + re.escape(s.lower()) + r'\b', resume_text_lower)
        ]
        relevant_tech_str = ", ".join(relevant_tech[:10]) if relevant_tech else skills_text[:200]

        # Build list of existing project names to avoid duplicates
        existing_names = []
        if existing_projects:
            existing_names = [p.get("name", "") for p in existing_projects if p.get("name")]

        avoid_clause = ""
        if existing_names:
            avoid_clause = (
                f"\nAVOID DUPLICATES: The resume already has these projects: "
                f"{', '.join(existing_names)}. Generate DIFFERENT projects.\n"
            )

        experience_block = _summarize_experience_bullets(original_resume)

        # JD-required skills the candidate plausibly has (intersection of
        # JD.required_skills with candidate.skills/text). This is the
        # PRIMARY anchor for the project's purpose — the project should
        # showcase these skills, not the candidate's overall stack.
        jd_required_in_resume = [
            s for s in jd_required
            if s.lower() in all_skills_lower
            or re.search(r'\b' + re.escape(s.lower()) + r'\b', resume_text_lower)
        ]
        jd_anchor = ", ".join(jd_required_in_resume[:8]) if jd_required_in_resume else ""

        prompt = (
            f"You are generating {count} portfolio project entries for a resume.\n"
            "Frame each as a PERSONAL / SIDE / OPEN-SOURCE project the candidate built on their\n"
            "own time. The projects should REINFORCE the candidate's experience — same domain,\n"
            "same tech, same problem area as their day job is EXPECTED and GOOD. This is what\n"
            "tells a recruiter the candidate knows the area well enough to explore it outside\n"
            "of work, and lets them speak to it fluently in an interview.\n\n"
            "EACH PROJECT MUST BE ALL THREE: defensible, JD-aligned, reinforcing-not-copying.\n\n"
            "  (A) DEFENSIBLE — the candidate could actually build this with the tech in their\n"
            "      profile. No fake stacks, no domains they have no exposure to, no fabricated\n"
            "      metrics. Drawing projects from the candidate's day-job tech and domain is\n"
            "      encouraged — that is precisely what makes them defensible. A technical\n"
            "      interviewer asking 'tell me more' should get a coherent answer rooted in\n"
            "      work the candidate already does.\n\n"
            "  (B) JD-ALIGNED — the project must SHOWCASE the JD's required skills, not the\n"
            "      candidate's miscellaneous stack. Anchor the project's purpose to a problem\n"
            "      that NATURALLY USES the JD-required skills listed below. If the JD wants\n"
            "      C#/ASP.NET/Angular/Oracle and the candidate plausibly knows them, the\n"
            "      project should be a C#/ASP.NET/Angular/Oracle project — not a Python/ELK\n"
            "      project just because the candidate also knows Python and ELK.\n\n"
            "  (C) REINFORCE, DON'T COPY — the project SHOULD overlap with experience in\n"
            "      domain, tech, and problem area. A recruiter expects to see 'they worked on\n"
            "      X at $company AND built a similar X on their own time' — that's a strength,\n"
            "      not a weakness. The ONLY thing forbidden is COPY-PASTING an experience\n"
            "      bullet. Do not reuse the exact wording, structure, or metric of an\n"
            "      experience bullet. Write the project as if explaining a separate piece of\n"
            "      code that lives in the candidate's own GitHub — different scope, different\n"
            "      consumer, or different framing of the same area is exactly right.\n"
            "      ALLOWED:\n"
            "        • Experience: 'Maintained Kafka telemetry ingestion at GE HealthCare\n"
            "          serving clinical devices across 12 sites.'\n"
            "          Project: 'Kafka Device Telemetry Toolkit — a personal library wrapping\n"
            "          common patterns for ingesting device telemetry over Kafka, with examples\n"
            "          showing producer back-pressure and consumer fan-out.' Same area, fresh\n"
            "          framing as a library. ALLOWED.\n"
            "        • Experience: 'Built ELK-based log alerting at HealthPlix.'\n"
            "          Project: 'Log Anomaly Detection Dashboard — Elasticsearch + Kibana\n"
            "          sandbox that experiments with anomaly rules on synthetic log data.'\n"
            "          Same ELK family, candidate's own playground. ALLOWED.\n"
            "      FORBIDDEN (only this one thing):\n"
            "        • Experience: 'Built Kafka anomaly-detection pipeline for clinical devices\n"
            "          at GE, reducing false alerts by 40%.'\n"
            "          Project bullet: 'Built Kafka anomaly-detection pipeline for clinical\n"
            "          devices, reducing false alerts by 40%.' ← Identical text. REJECTED.\n"
            "      Bottom line: write NEW bullets in the candidate's voice, even if they cover\n"
            "      the same territory. Do not paste experience bullets.\n\n"
            "WORKFLOW — follow IN ORDER (do not start with the name):\n"
            "  STEP 1: List the JD-required skills the candidate plausibly knows (provided\n"
            "          below). These are your project's tech anchors.\n"
            "  STEP 2: Read the EXPERIENCE BULLETS block. The projects SHOULD live in the same\n"
            "          territory — same domain, same tech. Use it as inspiration, not as text\n"
            "          to copy verbatim.\n"
            "  STEP 3: Pick a personal/side project PURPOSE that uses the JD anchors (Step 1)\n"
            "          and reinforces the candidate's experience area (Step 2). Write the\n"
            "          one-sentence purpose into 'purpose'.\n"
            "  STEP 4: Write the bullets in the candidate's own voice — each must reference at\n"
            "          least one JD-anchor skill naturally. Do NOT copy experience bullets'\n"
            "          wording or metrics. The reader should think 'this candidate built X\n"
            "          with the exact tech this role uses'.\n"
            "  STEP 5: ONLY NOW derive the NAME from the purpose. Name = WHAT it does.\n\n"
            + _BULLET_QUALITY_BLOCK + "\n"
            "STRICT RULES — violations will be rejected:\n"
            "1. Use ONLY technologies that appear in the candidate's skills or experience below.\n"
            "2. Do NOT invent fake company names, production systems, or client work.\n"
            "3. Do NOT invent metrics, percentages, or numbers.\n"
            "4. Each project must be a realistic personal or open-source project that a developer "
            "would actually build and put on GitHub — not a generic label or placeholder.\n"
            "5. dates: return empty string '' for each project.\n"
            "6. tech: comma-separated list of ONLY technologies present in the candidate's profile.\n"
            f"7. bullets: up to {bullets_per_project} bullets per project, ordered best-first (strongest, "
            "most JD-relevant first). Each ~100-150 chars, ending with a period.\n"
            "   Bullet quality is governed by the BULLET QUALITY block above — follow it exactly.\n"
            "   Each bullet must pass the four-question self-check before being emitted.\n"
            "8. PROJECT NAME RULES — the name describes WHAT the project does:\n"
            "   GOOD NAMES: 'Payment Fraud Detection API', 'Real-Time Log Aggregator', "
            "'E-Commerce Search Engine', 'Cloud Cost Optimizer', 'Distributed Task Queue'.\n"
            "   BAD NAMES (auto-rejected — name MUST NOT contain any of these words/patterns):\n"
            "     - The JD's job title or the candidate's role title\n"
            "     - 'Portfolio', 'Project', 'Demo', 'Application', 'Platform' as filler\n"
            "     - Lists of techs in the name ('Python & AWS Integration Platform')\n"
            "     - Anything describing WHO built it or WHAT ROLE it's for\n"
            f"9. Each project must be DISTINCT — different domains, different tech stacks.\n"
            f"{avoid_clause}\n"
            f"=== JD CONTEXT ===\n"
            f"JD role: {jd_title}" + (f" in {jd_industry}" if jd_industry else "") + "\n"
            f"JD-REQUIRED SKILLS THE CANDIDATE PLAUSIBLY KNOWS (PRIMARY ANCHORS — use these for the project): "
            f"{jd_anchor or '(none — fall back to general candidate skills, but only those most JD-adjacent)'}\n"
            f"JD-relevant tech (broader, secondary): {relevant_tech_str}\n\n"
            f"=== CANDIDATE'S OWN SKILLS (for plausibility checks; tech must come from this list) ===\n"
            f"{skills_text[:400]}\n\n"
            + (
                "=== EXPERIENCE BULLETS (DO NOT DUPLICATE these problems/solutions in any project) ===\n"
                f"{experience_block}\n\n"
                if experience_block else ""
            )
            +
            "Return a JSON object with EXACTLY this structure:\n"
            '{"projects": [\n'
            '  {"purpose": "One sentence: what problem this solves, using the JD-required skills, '
            'in a domain NOT covered by experience above", '
            '"name": "Project Name (derived from purpose)", '
            '"dates": "", "bullets": ["Bullet 1", "Bullet 2", "Bullet 3"], "tech": "Tech1, Tech2"}\n'
            "]}\n\n"
            f"=== CANDIDATE RESUME CONTEXT ===\n{resume_text[:3000]}"
        )

        BATCH_SCHEMA = {
            "projects": [{
                "purpose": str,
                "name": str,
                "dates": str,
                "bullets": [str],
                "tech": str,
            }]
        }

        try:
            result = gemini_json(
                prompt, max_tokens=12000, temperature=0.3,
                model=GEMINI_FLASH, schema=BATCH_SCHEMA,
            )
        except Exception as e:
            logger.warning("ProjectGenerator: batch generation failed: %s", e)
            rejections_sink.append({"name": "<llm-call>", "reason": f"llm_exception: {e}"})
            return []

        raw_projects = result.get("projects", [])
        if not isinstance(raw_projects, list):
            logger.warning(
                "ProjectGenerator: batch returned non-list 'projects' field "
                "(type=%s) — treating as empty",
                type(raw_projects).__name__,
            )
            rejections_sink.append({"name": "<llm-output>", "reason": "non_list_projects_field"})
            return []

        validated = []
        original_experience = original_resume.get("experience") or []
        for raw in raw_projects:
            if not isinstance(raw, dict):
                continue
            raw_name = (raw.get("name") if isinstance(raw, dict) else None) or "?"
            project = self._validate_and_clean(raw, original_resume, jd_analysis, bullets_per_project)
            if project is None:
                logger.warning(
                    "ProjectGenerator batch: project '%s' rejected — failed validation",
                    raw_name,
                )
                rejections_sink.append({"name": raw_name, "reason": "validation"})
                continue
            from services.content_augmenter import ContentAugmenter
            # Check for duplicates against existing + already-validated projects
            all_existing = (existing_projects or []) + validated
            if ContentAugmenter._is_duplicate_project(project, all_existing):
                logger.warning(
                    "ProjectGenerator batch: project '%s' rejected — duplicate",
                    project.get("name"),
                )
                rejections_sink.append({"name": project.get("name", "?"), "reason": "duplicate"})
                continue
            # Check for overlap with the candidate's experience bullets — projects
            # that restate experience add no signal and waste resume real estate.
            overlaps, reason = ContentAugmenter._overlaps_experience(project, original_experience)
            if overlaps:
                logger.warning(
                    "ProjectGenerator batch: project '%s' rejected — %s",
                    project.get("name"), reason,
                )
                rejections_sink.append({
                    "name": project.get("name", "?"),
                    "reason": f"overlaps_experience: {reason}",
                })
                continue
            validated.append(project)
            if len(validated) >= count:
                break

        logger.info("ProjectGenerator batch: generated %d/%d valid projects", len(validated), count)
        return validated

    def _validate_and_clean(
        self,
        project: Dict[str, Any],
        original_resume: Dict[str, Any],
        jd_analysis: Dict[str, Any] = None,
        bullets_per_project: int = 5,
    ) -> Optional[Dict[str, Any]]:
        """Validate generated project against allowed tech and schema.

        Returns cleaned project or None if it fails safety checks.
        """
        from schemas.resume_schemas import flatten_skills, build_resume_text

        name = str(project.get("name", "")).strip()
        bullets = project.get("bullets", [])
        tech_str = str(project.get("tech", "")).strip()

        if not name:
            logger.warning("ProjectGenerator: generated project has no name")
            return None

        # Fix slug-style names: "payment-fraud-detection-api" → "Payment Fraud Detection API"
        if "-" in name and name == name.lower():
            name = name.replace("-", " ").title()
            project["name"] = name
            logger.info("ProjectGenerator: converted slug name to title case: '%s'", name)

        # If name contains banned filler words or the JD/role title, ask LLM for a better name.
        # Expanded rejection list — matches the BAD NAMES rule in the prompt.
        BANNED_NAME_TOKENS = ("portfolio", "demo", "project", "application", "platform")
        needs_rename = False
        jd_title = ""
        name_lower = name.lower()
        if jd_analysis:
            jd_title = jd_analysis.get("job_title", "").strip()
            if jd_title and jd_title.lower() in name_lower:
                logger.warning(
                    "ProjectGenerator: name '%s' contains job title '%s', requesting new name from LLM",
                    name, jd_title,
                )
                needs_rename = True
        for token in BANNED_NAME_TOKENS:
            # match as a whole word so e.g. 'Application' as a noun is rejected
            # but 'Web Application Firewall' (rare) would still bounce — that's OK.
            if re.search(r'\b' + token + r'\b', name_lower):
                logger.warning(
                    "ProjectGenerator: name '%s' contains banned token '%s', requesting new name",
                    name, token,
                )
                needs_rename = True
                break

        if needs_rename:
            new_name = self._generate_project_name(tech_str, jd_title)
            if new_name:
                project["name"] = new_name
                name = new_name
                logger.info("ProjectGenerator: LLM generated new name: '%s'", new_name)
            else:
                logger.warning("ProjectGenerator: LLM name retry failed, rejecting project")
                return None

        if not isinstance(bullets, list) or len(bullets) == 0:
            logger.warning("ProjectGenerator: generated project has no bullets")
            return None

        # Keep up to `bullets_per_project` good bullets, ranked best-first (the
        # downstream bullet_allocator trims to what fits the page). We slice a
        # wide initial pool so the quality filter can drop weak bullets and still
        # leave a full ranked set.
        candidate_bullets = [str(b).strip() for b in bullets if str(b).strip()][:bullets_per_project * 2]
        if not candidate_bullets:
            return None

        # Bullet-quality filter — enforce the hard rules from the BULLET QUALITY
        # block deterministically. The prompt asks the model to follow them; this
        # post-check is the safety net for when it forgets. We DROP individual
        # weak bullets rather than rejecting the whole project; the project is
        # rejected only if fewer than the allocator floor (2) survive.
        clean_bullets = []
        for b in candidate_bullets:
            ok, reason = self._bullet_passes_quality_check(b)
            if not ok:
                logger.warning(
                    "ProjectGenerator: dropped bullet (%s): %r",
                    reason, b[:120],
                )
                continue
            clean_bullets.append(b)
            if len(clean_bullets) >= bullets_per_project:
                break

        if len(clean_bullets) < _MIN_PROJECT_BULLETS:
            logger.warning(
                "ProjectGenerator: project '%s' rejected — only %d/%d bullets passed "
                "quality check (need at least %d)",
                name, len(clean_bullets), len(candidate_bullets), _MIN_PROJECT_BULLETS,
            )
            return None

        # Validate tech: only keep items that appear in the resume text or skills
        resume_text_lower = build_resume_text(original_resume).lower()
        all_skills_lower = {s.lower() for s in flatten_skills(original_resume)}

        allowed_tech = []
        for tech_item in tech_str.split(","):
            t = tech_item.strip()
            if not t:
                continue
            t_lower = t.lower()
            if t_lower in all_skills_lower:
                allowed_tech.append(t)
            elif re.search(r'\b' + re.escape(t_lower) + r'\b', resume_text_lower):
                allowed_tech.append(t)
            else:
                logger.debug("ProjectGenerator: dropped unsupported tech '%s'", t)

        if not allowed_tech:
            # Keep up to 3 skills from resume as fallback
            allowed_tech = flatten_skills(original_resume)[:3]

        return {
            "name": name,
            "dates": "",
            "bullets": clean_bullets,
            "tech": ", ".join(allowed_tech),
        }

    @staticmethod
    def _generate_project_name(tech_str: str, jd_title: str) -> Optional[str]:
        """Ask LLM for just a project name when the initial name was bad.

        Uses FLASH for speed/cost — this is a lightweight one-shot call.
        Returns the name string or None if it fails.
        """
        try:
            from services.gemini_client import gemini_json, GEMINI_FLASH

            prompt = (
                "Generate a single project name for a developer's portfolio project.\n\n"
                "The project uses these technologies: " + tech_str + "\n"
                + (f"The target job domain is: {jd_title}\n" if jd_title else "")
                + "\nRULES:\n"
                "- The name must describe WHAT the project does (e.g. 'Payment Fraud Detection API', "
                "'Real-Time Log Aggregator', 'E-Commerce Search Engine').\n"
                "- Do NOT include any job title, role name, or the word 'Portfolio'.\n"
                "- The name should be 2-5 words, sound like a real GitHub project.\n"
                "- Think about what a developer would actually build with these technologies.\n\n"
                'Return a JSON object: {"name": "Your Project Name"}\n'
            )

            result = gemini_json(
                prompt, max_tokens=300, temperature=0.7, model=GEMINI_FLASH,
                schema={"name": str}
            )
            if isinstance(result, dict) and result.get("name", "").strip():
                name = result["name"].strip()
                # Final safety check: ensure the name doesn't still contain the job title
                if jd_title and jd_title.lower() in name.lower():
                    logger.warning("ProjectGenerator: LLM name retry still contains job title: '%s'", name)
                    return None
                if "portfolio" in name.lower():
                    logger.warning("ProjectGenerator: LLM name retry still contains 'portfolio': '%s'", name)
                    return None
                return name
        except Exception as e:
            logger.warning("ProjectGenerator: name generation failed: %s", e)
        return None

