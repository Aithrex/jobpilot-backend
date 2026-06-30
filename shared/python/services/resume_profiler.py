"""
Resume Profiler — derives a candidate's career intent + weighted skill profile.

Used by:
  - filter_suggester_service (to bias dynamic filter generation)
  - job_service.match_jobs (to score jobs by domain relevance, not just skill overlap)
  - daily_pipeline_service._score (to weight title-family bonuses by detected intent)

Design goals:
  * Deterministic, no LLM call — fast and free, runs every job match.
  * One place owns the intent taxonomy + skill->intent map. Adding a new
    intent or skill is a one-spot edit.
  * Hybrid profiles (e.g. ML + DevOps) emit a primary AND secondary intent;
    secondary still influences scoring but at a discount.
  * Feedback signal (clicks/applies/skips on a job's role family) can shift
    the intent ranking for a user without changing the resume.

The profile is a plain dict — easy to cache in Mongo or a request context.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Intent taxonomy
# ---------------------------------------------------------------------------
# Each intent has:
#   core_skills:    canonical lowercase tokens that DEFINE the domain (weight 3.0)
#   support_skills: tokens commonly seen in the domain but not defining (weight 1.5)
#   title_terms:    regex fragments for matching job titles in JD/experience
#   target_titles:  default titles the filter suggester should always include
#   keyword_phrases: LinkedIn-style search phrases biased to this intent
INTENTS: Dict[str, Dict[str, Any]] = {
    "ai_ml": {
        "label": "AI / ML Engineer",
        "core_skills": {
            "machine learning", "deep learning", "neural network", "neural networks",
            "pytorch", "tensorflow", "keras", "scikit-learn", "sklearn", "huggingface",
            "hugging face", "transformers", "llm", "llms", "nlp", "natural language processing",
            "computer vision", "cv", "rag", "langchain", "llamaindex", "vector database",
            "embeddings", "fine-tuning", "fine tuning", "model training", "mlops",
            "diffusion", "generative ai", "genai", "prompt engineering", "agentic",
            "reinforcement learning",
        },
        "support_skills": {
            "python", "pandas", "numpy", "jupyter", "matplotlib", "spacy", "opencv",
            "onnx", "tensorrt", "weights & biases", "wandb", "mlflow", "kubeflow",
            "ray", "dask", "ml engineer", "data scientist",
        },
        "title_terms": [
            r"\b(machine learning|ml|ai|applied ai|nlp|computer vision)\s+(engineer|scientist|researcher)\b",
            r"\b(ml engineer|ai engineer|mlops engineer|llm engineer|gen[- ]?ai engineer)\b",
            r"\b(research scientist|applied scientist|deep learning)\b",
            r"\b(ai/ml|ai\\ml|ml/ai)\b",
        ],
        "target_titles": [
            "Machine Learning Engineer", "ML Engineer", "AI Engineer",
            "Applied AI Engineer", "Applied Scientist", "Research Engineer",
            "NLP Engineer", "Computer Vision Engineer", "Deep Learning Engineer",
            "MLOps Engineer", "LLM Engineer", "GenAI Engineer",
            "Associate Machine Learning Engineer", "Junior ML Engineer",
            "Machine Learning Engineer I", "AI Engineer I",
        ],
        "keyword_phrases": [
            "Machine Learning Engineer", "AI Engineer PyTorch",
            "ML Engineer new grad", "NLP Engineer LLM",
            "Applied AI Engineer", "MLOps Engineer",
        ],
    },
    "data_science": {
        "label": "Data Scientist",
        "core_skills": {
            "data science", "statistics", "regression", "hypothesis testing",
            "a/b testing", "experimentation", "causal inference", "bayesian",
            "time series", "forecasting", "r", "stata",
        },
        "support_skills": {
            "python", "pandas", "numpy", "jupyter", "sql", "tableau", "looker",
            "powerbi", "scikit-learn", "matplotlib", "seaborn",
        },
        "title_terms": [
            r"\b(data scientist|analytics scientist|product analyst|research analyst)\b",
            r"\b(quant(itative)? (analyst|researcher))\b",
        ],
        "target_titles": [
            "Data Scientist", "Associate Data Scientist", "Junior Data Scientist",
            "Data Scientist I", "Product Data Scientist", "Decision Scientist",
            "Quantitative Analyst", "Research Analyst",
        ],
        "keyword_phrases": [
            "Data Scientist", "Junior Data Scientist", "Decision Scientist",
            "Data Scientist new grad", "Product Analyst",
        ],
    },
    "data_eng": {
        "label": "Data Engineer",
        "core_skills": {
            "spark", "pyspark", "hadoop", "airflow", "dbt", "snowflake", "redshift",
            "bigquery", "kafka", "kinesis", "etl", "elt", "data warehouse",
            "data lake", "data pipeline", "delta lake", "databricks", "fivetran",
            "data engineer", "stream processing", "flink",
        },
        "support_skills": {
            "python", "scala", "sql", "postgres", "postgresql", "mongodb", "aws",
            "gcp", "azure", "s3", "glue", "emr", "athena",
        },
        "title_terms": [
            r"\b(data engineer|analytics engineer|big data engineer|etl developer)\b",
            r"\b(data platform engineer)\b",
        ],
        "target_titles": [
            "Data Engineer", "Associate Data Engineer", "Junior Data Engineer",
            "Data Engineer I", "Analytics Engineer", "Data Platform Engineer",
            "Big Data Engineer", "ETL Developer",
        ],
        "keyword_phrases": [
            "Data Engineer", "Analytics Engineer", "Data Platform Engineer",
            "Data Engineer new grad", "Big Data Engineer Spark",
        ],
    },
    "cloud_devops": {
        "label": "Cloud / DevOps Engineer",
        "core_skills": {
            "aws", "gcp", "azure", "kubernetes", "k8s", "terraform", "ansible",
            "jenkins", "ci/cd", "github actions", "gitlab ci", "docker",
            "helm", "argocd", "prometheus", "grafana", "site reliability",
            "sre", "devops", "cloudformation", "pulumi", "istio", "service mesh",
        },
        "support_skills": {
            "linux", "bash", "shell", "python", "go", "nginx", "load balancer",
            "ec2", "s3", "vpc", "iam", "rds", "lambda", "ecs", "fargate", "eks",
            "cloudwatch", "datadog", "splunk", "pagerduty",
        },
        "title_terms": [
            r"\b(cloud|devops|sre|site reliability|platform|infrastructure)\s+(engineer|developer)\b",
            r"\b(kubernetes|terraform|aws|gcp|azure)\s+engineer\b",
        ],
        "target_titles": [
            "Cloud Engineer", "DevOps Engineer", "Site Reliability Engineer",
            "SRE", "Platform Engineer", "Infrastructure Engineer",
            "Cloud Engineer I", "Associate DevOps Engineer", "Junior DevOps Engineer",
            "Kubernetes Engineer", "AWS Cloud Engineer",
        ],
        "keyword_phrases": [
            "Cloud Engineer DevOps", "DevOps Engineer Kubernetes",
            "Site Reliability Engineer", "Platform Engineer",
            "Cloud Engineer new grad", "AWS Engineer",
        ],
    },
    "backend": {
        "label": "Backend Engineer",
        "core_skills": {
            "rest api", "rest", "graphql", "grpc", "microservices", "distributed systems",
            "spring boot", "spring", "django", "flask", "fastapi", "express",
            "node.js", "nodejs", "go", "golang", "rust", "ruby on rails",
            "postgres", "postgresql", "mysql", "mongodb", "redis", "elasticsearch",
        },
        "support_skills": {
            "python", "java", "kotlin", "typescript", "javascript", "sql",
            "kafka", "rabbitmq", "docker", "aws", "system design",
        },
        "title_terms": [
            r"\b(backend|back-?end|api|server-side|distributed systems)\s+(engineer|developer)\b",
            r"\b(software engineer)\b.*\b(backend|api|server)\b",
        ],
        "target_titles": [
            "Backend Engineer", "Backend Software Engineer", "Backend Developer",
            "API Engineer", "Server-Side Engineer", "Software Engineer Backend",
            "Software Engineer I", "Associate Software Engineer", "Junior Software Engineer",
            "New Grad Software Engineer", "Entry Level Software Engineer",
        ],
        "keyword_phrases": [
            "Backend Engineer Python", "Backend Engineer Java",
            "Software Engineer Backend new grad", "API Engineer",
        ],
    },
    "frontend": {
        "label": "Frontend Engineer",
        "core_skills": {
            "react", "vue", "angular", "next.js", "nextjs", "svelte", "remix",
            "tailwind", "css", "scss", "sass", "styled components", "redux",
            "vite", "webpack", "html5", "css3", "framer motion",
        },
        "support_skills": {
            "javascript", "typescript", "html", "node.js", "graphql",
            "rest", "jest", "cypress", "playwright", "storybook", "figma",
        },
        "title_terms": [
            r"\b(front[- ]?end|frontend|ui|web)\s+(engineer|developer)\b",
            r"\b(react|vue|angular)\s+(engineer|developer)\b",
        ],
        "target_titles": [
            "Frontend Engineer", "Frontend Software Engineer", "Frontend Developer",
            "UI Engineer", "Web Engineer", "React Engineer", "JavaScript Engineer",
            "Frontend Engineer I", "Associate Frontend Engineer", "Junior Frontend Engineer",
        ],
        "keyword_phrases": [
            "Frontend Engineer React", "UI Engineer TypeScript",
            "Frontend Engineer new grad", "Web Engineer",
        ],
    },
    "fullstack": {
        "label": "Full-Stack Engineer",
        "core_skills": {
            "full stack", "fullstack", "full-stack", "mern", "mean", "next.js",
            "nextjs", "react", "node.js", "django", "flask", "ruby on rails",
        },
        "support_skills": {
            "javascript", "typescript", "python", "rest", "graphql", "postgres",
            "mongodb", "html", "css", "tailwind", "aws",
        },
        "title_terms": [
            r"\b(full[- ]?stack|fullstack)\s+(engineer|developer)\b",
        ],
        "target_titles": [
            "Full Stack Engineer", "Full-Stack Engineer", "Full Stack Developer",
            "Full Stack Software Engineer", "Full-Stack Engineer I",
            "Associate Full Stack Engineer", "Junior Full Stack Engineer",
        ],
        "keyword_phrases": [
            "Full Stack Engineer", "Full Stack Developer React Node",
            "Full Stack Engineer new grad",
        ],
    },
    "security": {
        "label": "Security Engineer",
        "core_skills": {
            "security", "infosec", "penetration testing", "pentest", "owasp",
            "soc", "siem", "splunk", "iam", "zero trust", "threat modeling",
            "vulnerability management", "cryptography", "burp suite",
            "incident response", "security engineer", "appsec", "secops",
        },
        "support_skills": {
            "linux", "python", "bash", "aws", "kubernetes", "networking",
            "tcp/ip", "kali", "wireshark",
        },
        "title_terms": [
            r"\b(security|appsec|secops|cyber)\s+(engineer|analyst)\b",
            r"\b(penetration tester|red team|blue team)\b",
        ],
        "target_titles": [
            "Security Engineer", "Application Security Engineer", "AppSec Engineer",
            "Cloud Security Engineer", "Security Analyst", "SOC Analyst",
            "Junior Security Engineer", "Associate Security Engineer",
        ],
        "keyword_phrases": [
            "Security Engineer", "Application Security Engineer",
            "Cloud Security Engineer", "Security Engineer new grad",
        ],
    },
    "mobile": {
        "label": "Mobile Engineer",
        "core_skills": {
            "ios", "android", "swift", "objective-c", "kotlin", "react native",
            "flutter", "swiftui", "jetpack compose", "xamarin", "mobile",
        },
        "support_skills": {
            "java", "javascript", "typescript", "rest", "graphql", "firebase",
        },
        "title_terms": [
            r"\b(ios|android|mobile)\s+(engineer|developer)\b",
            r"\b(react native|flutter)\s+(engineer|developer)\b",
        ],
        "target_titles": [
            "iOS Engineer", "Android Engineer", "Mobile Engineer",
            "React Native Engineer", "Flutter Engineer",
            "Mobile Software Engineer", "Junior Mobile Engineer",
        ],
        "keyword_phrases": [
            "iOS Engineer Swift", "Android Engineer Kotlin",
            "Mobile Engineer new grad", "React Native Engineer",
        ],
    },
}


# Generic SWE baseline — only emitted when the candidate has no clear specialty
# OR when they're early-career (so they can still get the SWE safety net).
GENERIC_SWE_TITLES = [
    "Software Engineer", "Software Developer",
    "Software Engineer I", "Associate Software Engineer", "Junior Software Engineer",
    "New Grad Software Engineer", "Entry Level Software Engineer",
    "Graduate Software Engineer", "Early Career Software Engineer",
]


# ---------------------------------------------------------------------------
# Profile derivation
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9+#./\- ]*")


def _flatten_skills(structured: Dict[str, Any]) -> List[str]:
    raw = structured.get("skills") or {}
    out: List[str] = []
    if isinstance(raw, dict):
        for v in raw.values():
            if isinstance(v, list):
                out.extend(str(s) for s in v if s)
    elif isinstance(raw, list):
        out.extend(str(s) for s in raw if s)
    return [s.strip().lower() for s in out if s and s.strip()]


def _experience_text(structured: Dict[str, Any]) -> str:
    parts: List[str] = []
    for exp in (structured.get("experience") or [])[:8]:
        parts.append(str(exp.get("title") or ""))
        parts.append(str(exp.get("company") or ""))
        for b in (exp.get("bullets") or [])[:6]:
            parts.append(str(b))
    return " ".join(parts).lower()


def _projects_text(structured: Dict[str, Any]) -> str:
    parts: List[str] = []
    for p in (structured.get("projects") or [])[:8]:
        parts.append(str(p.get("name") or ""))
        parts.append(str(p.get("tech") or ""))
        for b in (p.get("bullets") or [])[:4]:
            parts.append(str(b))
    return " ".join(parts).lower()


def _summary_text(structured: Dict[str, Any]) -> str:
    return str(structured.get("summary") or "").lower()


def _experience_titles(structured: Dict[str, Any]) -> List[str]:
    return [
        str(e.get("title") or "").lower()
        for e in (structured.get("experience") or [])
        if e.get("title")
    ]


def _skill_hits(skill_set: Iterable[str], skills_lower: Iterable[str], haystack: str) -> int:
    """Count how many skills from `skill_set` appear in resume (skills list OR free text)."""
    skills_set = set(skills_lower)
    hits = 0
    for s in skill_set:
        if s in skills_set:
            hits += 1
            continue
        # multi-word skill: look for it as a phrase in the haystack
        if " " in s and s in haystack:
            hits += 1
    return hits


def _intent_scores(structured: Dict[str, Any]) -> Dict[str, float]:
    skills_flat = _flatten_skills(structured)
    summary = _summary_text(structured)
    exp_text = _experience_text(structured)
    proj_text = _projects_text(structured)
    titles = _experience_titles(structured)
    free_text = " ".join([summary, exp_text, proj_text])

    # An explicit "full stack" / "back-end" / "front-end" mention in the resume's
    # summary or recent titles is the single highest-signal classifier — much
    # more reliable than skill inventory, which is contaminated by the universal
    # "I list AWS + Docker on every resume" effect.
    summary_full_stack = bool(re.search(r"\bfull[- ]?stack\b", summary + " " + exp_text[:1000]))

    scores: Dict[str, float] = {}
    title_hits_map: Dict[str, float] = {}
    for intent_key, spec in INTENTS.items():
        core_hits = _skill_hits(spec["core_skills"], skills_flat, free_text)
        support_hits = _skill_hits(spec["support_skills"], skills_flat, free_text)

        title_hits: float = 0.0
        for pattern in spec["title_terms"]:
            rx = re.compile(pattern, re.IGNORECASE)
            for t in titles:
                if rx.search(t):
                    title_hits += 1
            if rx.search(summary):
                title_hits += 0.5
        title_hits_map[intent_key] = title_hits

        # Project tech mentions (e.g., "PyTorch model training") count strongly
        # for ML profiles — proj_text is the high-signal place for current focus.
        project_mentions = sum(1 for s in spec["core_skills"] if " " not in s and s in proj_text)
        project_mentions += sum(1 for s in spec["core_skills"] if " " in s and s in proj_text)

        # Weighting: core skills dominate, titles confirm, projects boost recent focus.
        score = (core_hits * 3.0) + (support_hits * 1.0) + (title_hits * 4.0) + (project_mentions * 1.5)
        scores[intent_key] = score

    # ------------------------------------------------------------------
    # Title-anchor penalty — fixes the most common misclassification:
    # a full-stack / backend engineer's resume lists AWS, Docker, Kubernetes,
    # CI/CD as table-stakes tooling, which used to dominate the cloud_devops
    # score even though zero of their job titles say "Cloud" / "DevOps" / "SRE".
    # If an intent's title-pattern never matched the candidate's actual work
    # history OR summary, halve its score. Titles are ground truth — skills
    # are aspirational. This single change correctly down-ranks "Cloud
    # Engineer" suggestions for an engineer whose resume says "Software Engineer"
    # at every step.
    # ------------------------------------------------------------------
    for intent_key in list(scores.keys()):
        if title_hits_map.get(intent_key, 0.0) <= 0:
            scores[intent_key] = scores[intent_key] * 0.5

    # Explicit "full stack" in summary/titles bumps the fullstack intent so
    # candidates who self-identify as full-stack don't get classified as
    # backend-only or frontend-only based on a slight skill imbalance.
    if summary_full_stack and "fullstack" in scores:
        scores["fullstack"] = scores["fullstack"] + 8.0

    return scores


def _build_weighted_skills(primary: str, secondary: Optional[str], skills_flat: List[str]) -> Dict[str, float]:
    """
    Assign each resume skill a weight 0.5..3.0 based on how core it is to the
    candidate's primary (and secondary) intent.

    A core skill of the primary domain → 3.0 (e.g., PyTorch for an ML candidate)
    A support skill of the primary domain → 1.5 (e.g., Pandas for an ML candidate)
    A core skill of the secondary domain → 1.5 (e.g., AWS for an ML+Cloud hybrid)
    A core skill of any other domain → 0.5 (cross-domain noise — e.g., Flutter on an ML resume)
    Otherwise → 1.0 (neutral)
    """
    primary_spec = INTENTS.get(primary, {})
    secondary_spec = INTENTS.get(secondary or "", {})

    primary_core = primary_spec.get("core_skills", set())
    primary_support = primary_spec.get("support_skills", set())
    secondary_core = secondary_spec.get("core_skills", set())

    # Build a set of "any other intent's core" to flag noise.
    other_core: set = set()
    for k, spec in INTENTS.items():
        if k in (primary, secondary):
            continue
        other_core.update(spec.get("core_skills", set()))

    weights: Dict[str, float] = {}
    for s in skills_flat:
        if s in primary_core:
            weights[s] = 3.0
        elif s in secondary_core:
            weights[s] = 1.5
        elif s in primary_support:
            weights[s] = 1.5
        elif s in other_core:
            weights[s] = 0.5
        else:
            weights[s] = 1.0
    return weights


def _experience_years(structured: Dict[str, Any]) -> int:
    """Mirror the lightweight estimator from resume_parser without the import cycle."""
    raw = structured.get("experience_years")
    try:
        if isinstance(raw, (int, float)) and raw >= 0:
            return int(raw)
    except (TypeError, ValueError):
        pass
    # Conservative fallback: number of distinct experience entries, capped.
    entries = structured.get("experience") or []
    return min(len(entries), 8)


def build_profile(
    structured: Dict[str, Any],
    feedback_signal: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    """
    Build a ResumeProfile from a structured resume.

    feedback_signal: optional dict { intent_key: bonus } from prior user actions
        (clicks/applies on jobs of that role family). Bumps intent ranking
        without modifying the resume itself.
    """
    if not isinstance(structured, dict):
        structured = {}

    raw_scores = _intent_scores(structured)
    if feedback_signal:
        for k, bump in feedback_signal.items():
            if k in raw_scores:
                raw_scores[k] = raw_scores[k] + float(bump)

    ordered = sorted(raw_scores.items(), key=lambda kv: kv[1], reverse=True)
    primary, primary_score = ordered[0] if ordered else ("backend", 0.0)
    # Hybrid detection: secondary only kicks in if it scores above floor AND
    # within 70% of primary — otherwise we treat it as a single-focus profile
    # to avoid recommending two unrelated domains.
    #
    # Complementarity guard: a backend/fullstack candidate listing AWS+Docker
    # used to surface cloud_devops as "secondary" — bleeding cloud-eng roles
    # into the daily pipeline. We now require the secondary to be in a curated
    # "complementary" set per primary, so the suggestions stay coherent.
    _COMPLEMENTARY = {
        "backend":      {"fullstack", "data_eng", "backend"},
        "fullstack":    {"backend", "frontend", "fullstack"},
        "frontend":     {"fullstack", "mobile", "frontend"},
        "ai_ml":        {"data_science", "data_eng", "ai_ml"},
        "data_science": {"ai_ml", "data_eng", "data_science"},
        "data_eng":     {"backend", "ai_ml", "data_eng"},
        "cloud_devops": {"backend", "security", "cloud_devops"},
        "security":     {"cloud_devops", "backend", "security"},
        "mobile":       {"frontend", "fullstack", "mobile"},
    }
    secondary: Optional[str] = None
    if len(ordered) >= 2:
        allowed = _COMPLEMENTARY.get(primary, set(INTENTS.keys()))
        for sec_key, sec_score in ordered[1:]:
            if sec_key == primary:
                continue
            if sec_key not in allowed:
                # Skip non-complementary intents (e.g. cloud_devops for a
                # backend primary) even if their skill score looks high.
                continue
            if sec_score >= 4.0 and sec_score >= 0.55 * max(primary_score, 1.0):
                secondary = sec_key
            break

    # If everything scores near-zero we fall back to a generalist SWE/backend
    # profile rather than picking the alphabetically-first intent.
    is_generalist = primary_score < 5.0
    if is_generalist:
        primary = "backend"
        secondary = None

    skills_flat = _flatten_skills(structured)
    weighted = _build_weighted_skills(primary, secondary, skills_flat)
    years = _experience_years(structured)

    primary_spec = INTENTS[primary]
    target_titles = list(primary_spec["target_titles"])
    keyword_phrases = list(primary_spec["keyword_phrases"])
    domain_keywords = sorted(primary_spec["core_skills"])[:25]

    if secondary:
        sec_spec = INTENTS[secondary]
        # Splice in a few secondary titles so a hybrid candidate (e.g. ML+Cloud)
        # still gets relevant ML+Cloud roles. Cap to avoid diluting the primary.
        for t in sec_spec["target_titles"][:6]:
            if t not in target_titles:
                target_titles.append(t)
        for p in sec_spec["keyword_phrases"][:3]:
            if p not in keyword_phrases:
                keyword_phrases.append(p)

    # Early-career or generalist profiles get the SWE safety net so we don't
    # narrow them to a single specialty before they have one.
    if is_generalist or years <= 2:
        for t in GENERIC_SWE_TITLES:
            if t not in target_titles:
                target_titles.append(t)

    confidence = 0.0
    if primary_score > 0:
        # 12 is roughly "strong" — multiple core skills + a confirming title.
        confidence = max(0.0, min(1.0, primary_score / 12.0))

    return {
        "primary_intent": primary,
        "primary_label": INTENTS[primary]["label"],
        "secondary_intent": secondary,
        "secondary_label": INTENTS[secondary]["label"] if secondary else None,
        "is_generalist": is_generalist,
        "intent_confidence": round(confidence, 2),
        "intent_scores": {k: round(v, 2) for k, v in raw_scores.items()},
        "weighted_skills": weighted,
        "skills_flat": skills_flat,
        "target_titles": target_titles[:60],
        "keyword_phrases": keyword_phrases[:8],
        "domain_keywords": domain_keywords,
        "experience_years": years,
        # Pre-compiled title-match regex bundle (caller reuses)
        "title_patterns": [
            re.compile(p, re.IGNORECASE) for p in primary_spec["title_terms"]
        ] + ([re.compile(p, re.IGNORECASE) for p in INTENTS[secondary]["title_terms"]]
             if secondary else []),
    }


# ---------------------------------------------------------------------------
# Job-side helpers (used by match_jobs and the daily pipeline scorer)
# ---------------------------------------------------------------------------

def title_relevance(profile: Dict[str, Any], job_title: str) -> float:
    """0.0..1.0 — how well the job title aligns with the candidate's intent.

    1.0 = exact primary-intent title match
    0.6 = secondary-intent title match
    0.3 = generic SWE title (fallback safety net)
    0.0 = unrelated
    """
    if not job_title:
        return 0.0
    title_lower = job_title.lower()
    primary = profile.get("primary_intent")
    secondary = profile.get("secondary_intent")
    if primary and any(rx.search(title_lower) for rx in (
        re.compile(p, re.IGNORECASE) for p in INTENTS[primary]["title_terms"]
    )):
        return 1.0
    if secondary and any(rx.search(title_lower) for rx in (
        re.compile(p, re.IGNORECASE) for p in INTENTS[secondary]["title_terms"]
    )):
        return 0.6
    if re.search(r"\b(software|swe|developer|engineer)\b", title_lower):
        return 0.3
    return 0.0


def weighted_skill_overlap(
    profile: Dict[str, Any],
    job_text: str,
) -> Tuple[List[str], float, float]:
    """
    Match the candidate's weighted skills against a job's title+description.

    Returns (matched_skills, weighted_score, max_possible_score).
    `weighted_score / max_possible_score` is the normalized overlap in [0, 1].
    """
    if not job_text:
        return [], 0.0, 1.0
    haystack = job_text.lower()
    weights: Dict[str, float] = profile.get("weighted_skills") or {}
    if not weights:
        return [], 0.0, 1.0

    matched: List[str] = []
    score = 0.0
    for skill, w in weights.items():
        if not skill:
            continue
        if len(skill) <= 2:
            pattern = re.escape(skill)
        else:
            pattern = r"\b" + re.escape(skill) + r"\b"
        if re.search(pattern, haystack, re.IGNORECASE):
            matched.append(skill)
            score += w

    # max-possible is the sum of all weights — but bias the denominator toward
    # the high-weight (primary-domain) skills so a job that hits 5 cloud skills
    # but zero ML skills can't outrank a job that hits 3 ML skills for an ML
    # candidate. We do this by squaring weights in the denominator.
    max_score = sum(w * w for w in weights.values()) or 1.0
    realized = sum((weights[s] * weights[s]) for s in matched if s in weights) or 0.0
    norm_score = realized / max_score
    return matched, round(score, 2), round(norm_score, 4)


def domain_relevance(profile: Dict[str, Any], job_text: str) -> float:
    """0..1 — does the job description actually mention the candidate's domain?

    Looks for primary-intent core-skill mentions in the description. A job
    with title "Software Engineer" and zero ML keywords scores low for an ML
    candidate even if it incidentally mentions "Python".
    """
    if not job_text:
        return 0.0
    haystack = job_text.lower()
    primary = profile.get("primary_intent")
    if not primary:
        return 0.5
    spec = INTENTS.get(primary, {})
    core = spec.get("core_skills") or set()
    if not core:
        return 0.5
    hits = 0
    for s in core:
        if " " in s:
            if s in haystack:
                hits += 1
        else:
            if re.search(r"\b" + re.escape(s) + r"\b", haystack):
                hits += 1
    # 4+ core mentions = saturated.
    return min(1.0, hits / 4.0)


def experience_alignment(profile: Dict[str, Any], job_text: str) -> float:
    """0..1 — does the job's seniority bracket match the candidate's years?

    Heuristic: parse "X+ years" / "X-Y years" / "senior"/"junior" markers from
    the job. Penalise mismatches by half rather than zero (titles lie often).
    """
    years = int(profile.get("experience_years") or 0)
    if not job_text:
        return 0.6
    text = job_text.lower()

    # Required years bracket
    required: Optional[int] = None
    m = re.search(r"(\d{1,2})\+?\s*(?:-|to|–)\s*(\d{1,2})\s*years", text)
    if m:
        required = int(m.group(1))
    else:
        m = re.search(r"(\d{1,2})\+\s*years", text)
        if m:
            required = int(m.group(1))

    is_senior_marker = bool(re.search(
        r"\b(senior|sr\.?|principal|staff|lead|manager|director|head of|architect)\b",
        text,
    ))
    is_entry_marker = bool(re.search(
        r"\b(intern|new grad|entry level|entry-level|junior|jr\.?|associate|early career|graduate)\b",
        text,
    ))

    if is_entry_marker and years <= 3:
        return 1.0
    if is_senior_marker and years < 5:
        return 0.3  # mismatch — candidate too junior
    if required is not None:
        if years >= required:
            return 1.0
        gap = required - years
        if gap <= 1:
            return 0.85
        if gap <= 3:
            return 0.6
        return 0.3
    # No explicit signal — neutral.
    return 0.7


def feedback_signal_for_user(
    activity_log_collection: Any,
    user_email: str,
    lookback_events: int = 200,
) -> Dict[str, float]:
    """
    Read the user's recent click/apply/skip actions and emit a per-intent bump.

    Each event must carry metadata like {"role_family": "ai_ml"} OR a job title
    we can re-classify against the intent taxonomy. Applies/clicks add positive
    bumps; explicit skips subtract. The scale is small (max ~3 points) so the
    base resume signal still dominates.
    """
    bumps: Dict[str, float] = {k: 0.0 for k in INTENTS}
    if not user_email or activity_log_collection is None:
        return bumps
    try:
        cursor = activity_log_collection.find(
            {"user_email": user_email,
             "type": {"$in": ["job_click", "job_applied", "job_saved", "job_skipped", "job_dismissed"]}}
        ).sort("timestamp", -1).limit(lookback_events)
    except Exception as e:  # pragma: no cover — best-effort signal only
        logger.warning(f"feedback_signal_for_user: failed to read activity log: {e}")
        return bumps

    weights = {
        "job_applied": 0.6,
        "job_saved": 0.3,
        "job_click": 0.15,
        "job_skipped": -0.25,
        "job_dismissed": -0.25,
    }

    for event in cursor:
        meta = event.get("metadata") or {}
        family = meta.get("role_family")
        if not family:
            # Try to classify from the title in metadata.
            title = (meta.get("job_title") or "").lower()
            if not title:
                continue
            for k, spec in INTENTS.items():
                if any(re.search(p, title, re.IGNORECASE) for p in spec["title_terms"]):
                    family = k
                    break
        if family in bumps:
            bumps[family] += weights.get(event.get("type") or "", 0.0)

    # Clip extreme bumps so a single noisy spree can't lock the user into a
    # mismatched intent forever.
    return {k: max(-3.0, min(3.0, round(v, 2))) for k, v in bumps.items()}
