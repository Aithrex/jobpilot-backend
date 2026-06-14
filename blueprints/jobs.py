"""
Jobs blueprint — Job search dashboard endpoints.

Authentication is handled by the unified auth service (/api/auth/login).
All endpoints require a valid JWT whose identity is the user's email.
"""
import logging
import re

from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity

from utils.security import InputSanitizer, get_rate_limiter, get_client_ip

jobs_bp = Blueprint("jobs", __name__)
logger = logging.getLogger(__name__)

DEFAULT_JOB_FILTERS = {
    "query": "software engineer new grad entry level h1b sponsor",
    "location": "United States",
    "date_posted": "today",
    "remote_only": False,
    "employment_type": "",
    "h1b_only": False,
    "visa_or_contract": True,
    "experience_level": "entry",
    "source": "all",
    "include_company_careers": True,
    "use_resume_recommendations": True,
}


def _as_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).lower() == "true"


def _sanitize_job_filters(data):
    raw = data if isinstance(data, dict) else {}
    filters = {
        **DEFAULT_JOB_FILTERS,
        "query": InputSanitizer.sanitize_string(raw.get("query", DEFAULT_JOB_FILTERS["query"]), max_length=200),
        "location": InputSanitizer.sanitize_string(raw.get("location", DEFAULT_JOB_FILTERS["location"]), max_length=100),
        "remote_only": _as_bool(raw.get("remote_only"), DEFAULT_JOB_FILTERS["remote_only"]),
        "h1b_only": _as_bool(raw.get("h1b_only"), DEFAULT_JOB_FILTERS["h1b_only"]),
        "visa_or_contract": _as_bool(raw.get("visa_or_contract"), DEFAULT_JOB_FILTERS["visa_or_contract"]),
        "include_company_careers": _as_bool(raw.get("include_company_careers"), DEFAULT_JOB_FILTERS["include_company_careers"]),
        "use_resume_recommendations": _as_bool(raw.get("use_resume_recommendations"), DEFAULT_JOB_FILTERS["use_resume_recommendations"]),
    }
    date_posted = raw.get("date_posted", DEFAULT_JOB_FILTERS["date_posted"])
    filters["date_posted"] = date_posted if date_posted in ("all", "today", "3days", "week", "month") else "today"

    employment_type = raw.get("employment_type", DEFAULT_JOB_FILTERS["employment_type"])
    filters["employment_type"] = employment_type if employment_type in ("", "FULLTIME", "PARTTIME", "INTERN", "CONTRACTOR") else ""

    experience_level = raw.get("experience_level", DEFAULT_JOB_FILTERS["experience_level"])
    filters["experience_level"] = experience_level if experience_level in ("", "entry", "internship", "associate", "mid") else "entry"

    source = raw.get("source", DEFAULT_JOB_FILTERS["source"])
    filters["source"] = source if source in ("all", "linkedin", "workday", "indeed", "google", "company", "jobright", "jsearch", "workday_direct", "ats_direct") else "all"
    return filters


# ------------------------------------------------------------------
# GET /api/jobs/search
# ------------------------------------------------------------------

@jobs_bp.route("/search", methods=["GET"])
@jwt_required()
def search():
    client_ip = get_client_ip(request)
    limiter = get_rate_limiter()
    if limiter.is_rate_limited(f"jobs_search:{client_ip}", max_requests=30, window_seconds=60):
        return jsonify({"error": "Rate limit exceeded"}), 429

    query = InputSanitizer.sanitize_string(request.args.get("q", ""), max_length=200)
    if not query:
        return jsonify({"error": "Query parameter 'q' is required"}), 400

    page = request.args.get("page", "1")
    try:
        page = max(1, int(page))
    except ValueError:
        page = 1

    location = InputSanitizer.sanitize_string(request.args.get("location", ""), max_length=100)
    date_posted = request.args.get("date_posted", "today")
    if date_posted not in ("all", "today", "3days", "week", "month"):
        date_posted = "today"
    remote_only = request.args.get("remote", "").lower() == "true"
    employment_type = request.args.get("type", "")
    if employment_type and employment_type not in ("FULLTIME", "PARTTIME", "INTERN", "CONTRACTOR"):
        employment_type = ""
    h1b_only = request.args.get("h1b_only", "").lower() == "true"
    visa_or_contract = request.args.get("visa_or_contract", "").lower() == "true"
    experience_level = request.args.get("experience_level", "")
    if experience_level not in ("", "entry", "internship", "associate", "mid"):
        experience_level = ""
    source = request.args.get("source", "all")
    if source not in ("all", "linkedin", "workday", "indeed", "google", "company", "jobright", "jsearch"):
        source = "all"
    include_company_careers = request.args.get("include_company_careers", "true").lower() == "true"
    use_resume_recommendations = request.args.get("use_resume_recommendations", "true").lower() == "true"
    force_refresh = request.args.get("force_refresh", "false").lower() == "true"

    try:
        from services.resume_service import get_resume_service, ResumeService
        svc = get_resume_service()
        user_email = get_jwt_identity()
        payload = {
            "mode": "single",
            "query": query,
            "page": page,
            "location": location,
            "date_posted": date_posted,
            "remote_only": remote_only,
            "employment_type": employment_type,
            "h1b_only": h1b_only,
            "visa_or_contract": visa_or_contract,
            "experience_level": experience_level,
            "source": source,
            "include_company_careers": include_company_careers,
            "use_resume_recommendations": use_resume_recommendations,
            "force_refresh": force_refresh,
            "user_email": user_email,
        }
        job_id = svc.create_job("job_search", payload, user_email=user_email)
        ResumeService.invoke_async(job_id, "job_search", payload)
        return jsonify({"job_id": job_id}), 202
    except RuntimeError as e:
        if "not configured" in str(e).lower():
            return jsonify({"error": "Job search service not available"}), 503
        logger.error(f"Job search error: {e}")
        return jsonify({"error": "Search failed"}), 500
    except Exception as e:
        logger.error(f"Job search error: {e}")
        return jsonify({"error": "Search failed"}), 500


# ------------------------------------------------------------------
# POST /api/jobs/batch-search
# ------------------------------------------------------------------

@jobs_bp.route("/batch-search", methods=["POST"])
@jwt_required()
def batch_search():
    client_ip = get_client_ip(request)
    limiter = get_rate_limiter()
    if limiter.is_rate_limited(f"jobs_batch:{client_ip}", max_requests=10, window_seconds=60):
        return jsonify({"error": "Rate limit exceeded"}), 429

    data = request.get_json(force=True) or {}
    queries = data.get("queries", [])
    if not isinstance(queries, list) or not queries:
        return jsonify({"error": "queries must be a non-empty list"}), 400
    if len(queries) > 8:
        return jsonify({"error": "Maximum 8 queries per batch"}), 400

    # Sanitize each query
    queries = [InputSanitizer.sanitize_string(q, max_length=200) for q in queries if isinstance(q, str)]
    queries = [q for q in queries if q]  # remove empties
    if not queries:
        return jsonify({"error": "No valid queries after sanitization"}), 400

    location = InputSanitizer.sanitize_string(data.get("location", ""), max_length=100)
    date_posted = data.get("date_posted", "today")
    if date_posted not in ("all", "today", "3days", "week", "month"):
        date_posted = "today"
    remote_only = str(data.get("remote", "")).lower() == "true"
    employment_type = data.get("type", "")
    if employment_type and employment_type not in ("FULLTIME", "PARTTIME", "INTERN", "CONTRACTOR"):
        employment_type = ""
    h1b_only = str(data.get("h1b_only", "")).lower() == "true"
    visa_or_contract = str(data.get("visa_or_contract", "")).lower() == "true"
    experience_level = data.get("experience_level", "")
    if experience_level not in ("", "entry", "internship", "associate", "mid"):
        experience_level = ""
    source = data.get("source", "all")
    if source not in ("all", "linkedin", "workday", "indeed", "google", "company", "jobright", "jsearch"):
        source = "all"
    include_company_careers = str(data.get("include_company_careers", True)).lower() == "true"
    use_resume_recommendations = str(data.get("use_resume_recommendations", True)).lower() == "true"
    force_refresh = str(data.get("force_refresh", False)).lower() == "true"

    try:
        from services.resume_service import get_resume_service, ResumeService
        svc = get_resume_service()
        user_email = get_jwt_identity()
        payload = {
            "mode": "batch",
            "queries": queries,
            "location": location,
            "date_posted": date_posted,
            "remote_only": remote_only,
            "employment_type": employment_type,
            "h1b_only": h1b_only,
            "visa_or_contract": visa_or_contract,
            "experience_level": experience_level,
            "source": source,
            "include_company_careers": include_company_careers,
            "use_resume_recommendations": use_resume_recommendations,
            "force_refresh": force_refresh,
            "user_email": user_email,
        }
        job_id = svc.create_job("job_search", payload, user_email=user_email)
        ResumeService.invoke_async(job_id, "job_search", payload)
        return jsonify({"job_id": job_id}), 202
    except RuntimeError as e:
        if "not configured" in str(e).lower():
            return jsonify({"error": "Job search service not available"}), 503
        logger.error(f"Batch search error: {e}")
        return jsonify({"error": "Batch search failed"}), 500
    except Exception as e:
        logger.error(f"Batch search error: {e}")
        return jsonify({"error": "Batch search failed"}), 500


# ------------------------------------------------------------------
# POST /api/jobs/pipeline   (daily Apify pipeline: LinkedIn + Workday)
# ------------------------------------------------------------------

def _split_lines(value, max_items: int, max_len: int = 200) -> list:
    """Accept a list[str] or a newline/comma separated string, return cleaned list."""
    if isinstance(value, list):
        items = [str(x) for x in value]
    elif isinstance(value, str):
        items = re.split(r"[\n,]", value)
    else:
        items = []
    cleaned = []
    for item in items:
        s = InputSanitizer.sanitize_string(item, max_length=max_len)
        if s:
            cleaned.append(s)
    return cleaned[:max_items]


# ------------------------------------------------------------------
# Per-user Apify key (BYO) — GET status, PUT to set/update, DELETE to clear.
# ------------------------------------------------------------------

@jobs_bp.route("/apify-key", methods=["GET"])
@jwt_required()
def get_apify_key():
    user_email = get_jwt_identity()
    try:
        from services.job_service import get_job_service
        return jsonify(get_job_service().get_user_apify_key_status(user_email)), 200
    except Exception as e:
        logger.error(f"Get apify key error: {e}")
        return jsonify({"error": "Failed to load key"}), 500


@jobs_bp.route("/apify-key", methods=["PUT"])
@jwt_required()
def set_apify_key():
    client_ip = get_client_ip(request)
    limiter = get_rate_limiter()
    if limiter.is_rate_limited(f"jobs_apify_key:{client_ip}", max_requests=10, window_seconds=300):
        return jsonify({"error": "Rate limit exceeded"}), 429

    user_email = get_jwt_identity()
    data = request.get_json(silent=True) or {}
    raw = data.get("apify_key", "")
    if not isinstance(raw, str):
        return jsonify({"error": "apify_key must be a string"}), 400
    # Belt + braces: don't sanitize as 'string' here because the sanitizer
    # may strip safe chars; we validate format inside the service.
    try:
        from services.job_service import get_job_service
        result = get_job_service().set_user_apify_key(user_email, raw)
        if not result.get("ok"):
            return jsonify({"error": result.get("error", "Failed to save key")}), 400
        return jsonify(result), 200
    except Exception as e:
        logger.error(f"Save apify key error: {e}")
        return jsonify({"error": "Failed to save key"}), 500


@jobs_bp.route("/apify-key", methods=["DELETE"])
@jwt_required()
def delete_apify_key():
    user_email = get_jwt_identity()
    try:
        from services.job_service import get_job_service
        return jsonify(get_job_service().delete_user_apify_key(user_email)), 200
    except Exception as e:
        logger.error(f"Delete apify key error: {e}")
        return jsonify({"error": "Failed to remove key"}), 500


@jobs_bp.route("/fetch-jd", methods=["POST"])
@jwt_required()
def fetch_jd_route():
    """Best-effort fetch of a job description by URL.

    Body: { "url": "https://..." }
    Returns: { "jd_text": "...", "source_kind": "linkedin_guest|greenhouse_api|...|html_scrape", "ok": bool }

    Used by the Tailor flow when the pipeline scrape didn't include a
    description (LinkedIn/Workday/company pages) so the JD textarea opens
    pre-filled instead of empty. Rate-limited per IP because each call hits
    a third-party site.
    """
    client_ip = get_client_ip(request)
    limiter = get_rate_limiter()
    if limiter.is_rate_limited(f"jobs_fetch_jd:{client_ip}", max_requests=20, window_seconds=60):
        return jsonify({"ok": False, "error": "Rate limit exceeded"}), 429

    data = request.get_json(silent=True) or {}
    url = InputSanitizer.sanitize_string(str(data.get("url") or ""), max_length=600)
    if not url or not (url.startswith("http://") or url.startswith("https://")):
        return jsonify({"ok": False, "error": "A http(s) job URL is required"}), 400
    try:
        from services.jd_fetcher import fetch_jd
        text, kind = fetch_jd(url)
        if not text:
            return jsonify({
                "ok": False,
                "error": "Couldn't extract the JD from that URL. Paste the description manually.",
                "source_kind": kind,
            }), 200
        return jsonify({"ok": True, "jd_text": text, "source_kind": kind}), 200
    except Exception as e:
        logger.error(f"fetch-jd error: {e}")
        return jsonify({"ok": False, "error": "Failed to fetch JD"}), 500


@jobs_bp.route("/draft-outreach", methods=["POST"])
@jwt_required()
def draft_outreach_route():
    """Draft a short cold-outreach email for a specific job + company.

    Body: {
      "company": "Stripe",
      "title": "Backend Engineer",
      "location": "Seattle, WA",
      "jd_text": "...optional...",
      "tone": "warm" | "direct"   (optional, defaults to "warm")
    }
    Returns: { "subject": "...", "body": "..." }

    Synchronous (single LLM FLASH-tier call, ~2-3s). Rate-limited per IP at
    20/min so a user can run through their Tier 1 list without manual delay
    but a misuse can't burn the LLM quota. The user can edit either
    field before sending.
    """
    client_ip = get_client_ip(request)
    limiter = get_rate_limiter()
    if limiter.is_rate_limited(f"draft_outreach:{client_ip}", max_requests=20, window_seconds=60):
        return jsonify({"error": "Rate limit exceeded — try again in a minute"}), 429

    data = request.get_json(silent=True) or {}
    company = InputSanitizer.sanitize_string(str(data.get("company") or ""), max_length=200).strip()
    title = InputSanitizer.sanitize_string(str(data.get("title") or ""), max_length=200).strip()
    location = InputSanitizer.sanitize_string(str(data.get("location") or ""), max_length=200).strip()
    jd_text = InputSanitizer.sanitize_string(str(data.get("jd_text") or ""), max_length=15000).strip()
    tone = (str(data.get("tone") or "warm").lower())
    if tone not in ("warm", "direct"):
        tone = "warm"

    if not company or not title:
        return jsonify({"error": "company and title are required"}), 400

    user_email = get_jwt_identity()
    try:
        from services.resume_parser import ResumeParser
        from services.gemini_client import gemini_json, GEMINI_FLASH
        # Pull the candidate's parsed resume so the draft references real
        # bullets / projects instead of generic platitudes. If they haven't
        # uploaded a resume yet the endpoint still works — falls back to a
        # generic "I'm interested" template.
        parser = ResumeParser()
        resume = parser.ensure_structured_resume(user_email=user_email) if user_email else None
        structured = (resume or {}).get("structured") or {}
        contact = structured.get("contact") or {}
        candidate_name = (contact.get("name") or "").strip() or "Candidate"
        summary = (structured.get("summary") or "")[:600]
        # Pick top 2 most recent experience bullets to ground the message.
        bullets: list = []
        for exp in (structured.get("experience") or [])[:2]:
            for b in (exp.get("bullets") or [])[:2]:
                if b and str(b).strip():
                    bullets.append(str(b).strip())
        skills = []
        sk_raw = structured.get("skills") or {}
        if isinstance(sk_raw, dict):
            for cat in sk_raw.values():
                if isinstance(cat, list):
                    skills.extend(str(s) for s in cat[:5])
        skills = skills[:15]

        prompt = (
            "You are a senior career coach helping an international graduate "
            "student write a short, high-signal cold outreach email to a "
            "recruiter or hiring manager.\n\n"
            f"CANDIDATE: {candidate_name}\n"
            f"CANDIDATE SUMMARY: {summary or '(no summary)'}\n"
            f"RECENT WORK BULLETS:\n- " + "\n- ".join(bullets[:4] or ["(none provided)"]) + "\n"
            f"TOP SKILLS: {', '.join(skills) or '(none)'}\n\n"
            f"TARGET ROLE: {title} at {company}"
            + (f" ({location})" if location else "") + "\n"
            + (f"JOB DESCRIPTION EXCERPT (first 2000 chars):\n{jd_text[:2000]}\n\n" if jd_text else "\n")
            + "Rules:\n"
            f"  - Tone: {tone}. {'Conversational, warm, optimistic.' if tone == 'warm' else 'Direct, no fluff, professional.'}\n"
            "  - 3-4 sentences MAX in the body. Hiring managers skim — write to be skimmed.\n"
            "  - Sentence 1: One specific reason this role fits (reference a concrete JD requirement OR the team's mission).\n"
            "  - Sentence 2: ONE specific candidate accomplishment from the bullets above that backs it.\n"
            "  - Sentence 3: Ask for a 15-minute conversation.\n"
            "  - Optional sentence 4: Brief mention of attaching tailored resume.\n"
            "  - Do NOT use phrases like 'I am writing to express my interest' or 'I came across this position'.\n"
            "  - Do NOT mention visa / sponsorship — the candidate handles that signal separately.\n"
            "  - Subject line must be short (≤60 chars), specific, and not start with 'Re:' or 'Fwd:'.\n\n"
            'Return JSON: { "subject": "...", "body": "..." }. No markdown, no commentary.'
        )

        result = gemini_json(
            prompt=prompt,
            model=GEMINI_FLASH,
            temperature=0.5,
            schema={"subject": str, "body": str},
            max_tokens=800,
        )
        subject = str((result or {}).get("subject") or "").strip()
        body = str((result or {}).get("body") or "").strip()
        if not subject:
            subject = f"{candidate_name} — interest in {title} at {company}"
        if not body:
            return jsonify({"error": "Draft generation failed — retry"}), 500
        return jsonify({"subject": subject[:120], "body": body[:1800]}), 200
    except Exception as e:
        logger.error(f"draft-outreach error: {e}")
        return jsonify({"error": "Failed to draft outreach message"}), 500


@jobs_bp.route("/pipeline/suggest-filters", methods=["GET"])
@jwt_required()
def suggest_pipeline_filters():
    """Return resume-aware filter suggestions for the daily pipeline."""
    client_ip = get_client_ip(request)
    limiter = get_rate_limiter()
    if limiter.is_rate_limited(f"jobs_suggest:{client_ip}", max_requests=15, window_seconds=300):
        return jsonify({"error": "Rate limit exceeded"}), 429

    user_email = get_jwt_identity()
    try:
        from services.resume_service import get_resume_service
        from services.filter_suggester_service import suggest_filters

        svc = get_resume_service()
        resume = svc.parser.ensure_structured_resume(user_email=user_email)
        if not resume or not resume.get("structured"):
            return jsonify({"error": "No resume found. Upload a resume to enable smart filters."}), 404
        suggestions = suggest_filters(resume["structured"])
        return jsonify({"suggestions": suggestions}), 200
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        logger.error(f"Filter suggestion error: {e}")
        return jsonify({"error": "Failed to generate suggestions"}), 500


@jobs_bp.route("/pipeline", methods=["POST"])
@jwt_required()
def daily_pipeline():
    """Run the daily Apify pipeline: parallel LinkedIn + Workday scrape, score, tier.

    Body (all optional - sensible defaults match job_pipeline.py):
        {
          "linkedin_keywords": ["Cloud Engineer DevOps", ...],
          "workday_titles":    ["Cloud Engineer", "Backend Engineer", ...],
          "past_days":         1,
          "custom_role_terms": ["security", "data engineer"],
          "linkedin_count":    80,
          "workday_limit":     200
        }
    Returns: {"job_id": "..."}  - poll /api/resume/job/<id> for the result.
    """
    client_ip = get_client_ip(request)
    limiter = get_rate_limiter()
    if limiter.is_rate_limited(f"jobs_pipeline:{client_ip}", max_requests=6, window_seconds=300):
        return jsonify({"error": "Rate limit exceeded. Pipeline runs are limited to 6 / 5 min."}), 429

    data = request.get_json(silent=True) or {}
    try:
        past_days = int(data.get("past_days") if data.get("past_days") is not None else 1)
    except (TypeError, ValueError):
        past_days = 1
    # 0 = "Today only" (cutoff = today). 1-30 days otherwise.
    past_days = max(0, min(past_days, 30))

    linkedin_keywords = _split_lines(data.get("linkedin_keywords"), max_items=10, max_len=200)
    workday_titles = _split_lines(data.get("workday_titles"), max_items=200, max_len=120)
    custom_role_terms = _split_lines(data.get("custom_role_terms"), max_items=20, max_len=80)

    try:
        linkedin_count = max(10, min(int(data.get("linkedin_count") or 80), 250))
    except (TypeError, ValueError):
        linkedin_count = 80
    try:
        workday_limit = max(20, min(int(data.get("workday_limit") or 300), 800))
    except (TypeError, ValueError):
        workday_limit = 300

    user_email = get_jwt_identity()
    include_indeed = bool(data.get("include_indeed", False))

    # New filter knobs — ALL optional. When the request omits a key, the
    # pipeline reproduces the original hardcoded behavior (entry-level,
    # full-time, US-only, any work arrangement). When the user explicitly
    # passes "any" / "" the corresponding actor filter is dropped entirely
    # so the actor returns the full result set.
    location = InputSanitizer.sanitize_string(str(data.get("location") or ""), max_length=80) or "United States"
    raw_exp = (str(data.get("experience_level")).lower() if "experience_level" in data else "entry")
    if raw_exp not in ("any", "internship", "entry", "associate", "mid", "senior"):
        raw_exp = "entry"
    raw_emp = (str(data.get("employment_type")).upper() if "employment_type" in data else "FULLTIME")
    if raw_emp not in ("ANY", "FULLTIME", "PARTTIME", "INTERN", "CONTRACTOR"):
        raw_emp = "FULLTIME"
    raw_arr = (str(data.get("work_arrangement")).lower() if "work_arrangement" in data else "any")
    if raw_arr not in ("any", "remote", "hybrid", "onsite"):
        raw_arr = "any"
    domain_strict = bool(data.get("domain_strict", False))
    # F-1 / H-1B opt-ins — both default OFF so non-visa users keep current flow.
    h1b_only = bool(data.get("h1b_only", False))
    exclude_no_sponsorship = bool(data.get("exclude_no_sponsorship", False))
    raw_hide = data.get("hide_companies") or []
    hide_companies: list = []
    if isinstance(raw_hide, list):
        hide_companies = [str(h).strip() for h in raw_hide if str(h or "").strip()][:50]
    raw_hide_titles = data.get("hide_title_patterns") or []
    hide_title_patterns: list = []
    if isinstance(raw_hide_titles, list):
        hide_title_patterns = [str(p).strip() for p in raw_hide_titles if str(p or "").strip()][:30]
    try:
        max_per_company = int(data.get("max_per_company", 4))
    except (TypeError, ValueError):
        max_per_company = 4
    max_per_company = max(1, min(max_per_company, 20))

    # Per-source filter overrides used to be a knob here (LinkedIn / Workday /
    # Indeed / ATS each with its own enabled/past_days/experience_level). They
    # were removed because they encoded filters BEFORE the fetch, which is the
    # exact pattern that was hiding Yahoo's Workday postings from us. Today
    # every source fetches the widest sensible window and the user's filters
    # apply once on the merged result inside _filter_and_score.

    payload = {
        "linkedin_keywords": linkedin_keywords or None,
        "workday_titles": workday_titles or None,
        "past_days": past_days,
        "custom_role_terms": custom_role_terms or None,
        "linkedin_count": linkedin_count,
        "workday_limit": workday_limit,
        "include_indeed": include_indeed,
        "user_email": user_email,
        "location": location,
        "experience_level": raw_exp,
        "employment_type": raw_emp,
        "work_arrangement": raw_arr,
        "domain_strict": domain_strict,
        "h1b_only": h1b_only,
        "exclude_no_sponsorship": exclude_no_sponsorship,
        "hide_companies": hide_companies,
        "hide_title_patterns": hide_title_patterns,
        "max_per_company": max_per_company,
    }

    try:
        from services.resume_service import get_resume_service, ResumeService
        svc = get_resume_service()
        job_id = svc.create_job("daily_pipeline", payload, user_email=user_email)
        ResumeService.invoke_async(job_id, "daily_pipeline", payload)
        return jsonify({"job_id": job_id}), 202
    except Exception as e:
        logger.error(f"Daily pipeline submit error: {e}")
        return jsonify({"error": "Failed to start pipeline"}), 500


# ------------------------------------------------------------------
# POST /api/jobs/analyze
# ------------------------------------------------------------------

@jobs_bp.route("/analyze", methods=["POST"])
@jwt_required()
def analyze():
    client_ip = get_client_ip(request)
    limiter = get_rate_limiter()
    if limiter.is_rate_limited(f"jobs_analyze:{client_ip}", max_requests=10, window_seconds=60):
        return jsonify({"error": "Rate limit exceeded"}), 429

    data = request.get_json(force=True) or {}
    job = data.get("job")
    action = data.get("action", "")
    if not job or not isinstance(job, dict):
        return jsonify({"error": "Job data is required"}), 400
    if action not in ("summarize", "missing_skills", "cover_letter"):
        return jsonify({"error": "Invalid action"}), 400

    try:
        from services.job_service import get_job_service
        user_email = get_jwt_identity()
        result = get_job_service().analyze_job(job, action, user_email=user_email)
        return jsonify({"result": result, "action": action}), 200
    except Exception as e:
        logger.error(f"Job analysis error: {e}")
        return jsonify({"error": "Analysis failed"}), 500


# ------------------------------------------------------------------
# POST /api/jobs/tailor-resume
# ------------------------------------------------------------------

@jobs_bp.route("/tailor-resume", methods=["POST"])
@jwt_required()
def tailor_resume():
    client_ip = get_client_ip(request)
    limiter = get_rate_limiter()
    if limiter.is_rate_limited(f"jobs_tailor:{client_ip}", max_requests=10, window_seconds=60):
        return jsonify({"error": "Rate limit exceeded"}), 429

    data = request.get_json(force=True) or {}
    job_description = data.get("job_description", "")
    if not job_description or not isinstance(job_description, str):
        return jsonify({"error": "job_description is required"}), 400

    job_description = InputSanitizer.sanitize_string(job_description, max_length=6000)
    if not job_description:
        return jsonify({"error": "Invalid job description"}), 400

    user_email = get_jwt_identity()

    try:
        from services.resume_service import get_resume_service
        svc = get_resume_service()

        # Extract JD → get structured resume → tailor
        jd_analysis = svc.extract_jd(job_description)
        # ensure_structured_resume re-parses from the base S3 file if the user
        # only has a legacy base upload with no structured doc yet.
        resume = svc.parser.ensure_structured_resume(user_email=user_email)
        if not resume:
            return jsonify({"error": "No resume uploaded"}), 404

        structured = resume.get("structured")
        if not structured:
            raw_text = resume.get("raw_text", "")
            if not raw_text:
                return jsonify({"error": "Resume data unavailable. Please re-upload."}), 404
            structured, _ = svc.parser.parse_to_structured(raw_text)

        tailored = svc.tailor.tailor(structured, jd_analysis)

        # Map to the simpler format expected by the job search UI
        from schemas.resume_schemas import flatten_skills
        result = {
            "summary": tailored.get("summary", ""),
            "skills": flatten_skills(tailored),
            "experience_bullets": [
                b for exp in tailored.get("experience", [])
                for b in exp.get("bullets", [])
            ],
            "keywords_to_add": jd_analysis.get("keywords", []),
        }
        return jsonify({"tailored": result}), 200
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        logger.error(f"Resume tailoring error: {e}")
        return jsonify({"error": "Failed to tailor resume"}), 500


# ------------------------------------------------------------------
# POST /api/jobs/resume  (multipart PDF upload)
# GET  /api/jobs/resume
# ------------------------------------------------------------------

@jobs_bp.route("/resume", methods=["POST"])
@jwt_required()
def upload_resume():
    client_ip = get_client_ip(request)
    limiter = get_rate_limiter()
    if limiter.is_rate_limited(f"jobs_resume:{client_ip}", max_requests=5, window_seconds=3600):
        return jsonify({"error": "Rate limit exceeded. Max 5 uploads per hour."}), 429

    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    file = request.files["file"]
    allowed_extensions = (".pdf", ".docx")
    if not file.filename or not any(file.filename.lower().endswith(ext) for ext in allowed_extensions):
        return jsonify({"error": "Only PDF and DOCX files are accepted"}), 400

    # 5 MB limit
    file_bytes = file.read()
    if len(file_bytes) > 5 * 1024 * 1024:
        return jsonify({"error": "File too large (max 5 MB)"}), 400

    user_email = get_jwt_identity()

    try:
        from services.resume_service import get_resume_service
        svc = get_resume_service()
        parsed = svc.parser.upload_and_parse_file(file_bytes, file.filename, user_email=user_email)

        # Return flat format matching the job search UI's ParsedResume type
        return jsonify({
            "resume": {
                "skills": parsed.get("skills", []),
                "experience_years": parsed.get("experience_years", 0),
                "education": parsed.get("education", []),
                "certifications": parsed.get("certifications", []),
                "job_titles": parsed.get("job_titles", []),
                "summary": parsed.get("summary", ""),
            }
        }), 200
    except ValueError as e:
        logger.error(f"PDF parsing error: {e}")
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error(f"Resume parsing error: {e}")
        return jsonify({"error": "Failed to parse resume"}), 500


@jobs_bp.route("/resume", methods=["GET"])
@jwt_required()
def get_resume():
    user_email = get_jwt_identity()
    try:
        from services.resume_service import get_resume_service
        resume = get_resume_service().get_base_resume(user_email=user_email)
        if not resume:
            return jsonify({"error": "No resume uploaded yet"}), 404
        if hasattr(resume.get('parsed_at'), 'isoformat'):
            resume['parsed_at'] = resume['parsed_at'].isoformat()
        return jsonify({"resume": resume}), 200
    except Exception as e:
        logger.error(f"Get resume error: {e}")
        return jsonify({"error": "Failed to retrieve resume"}), 500


# ------------------------------------------------------------------
# Saved Job Search Filters
# ------------------------------------------------------------------

@jobs_bp.route("/filters", methods=["GET"])
@jwt_required()
def get_filters():
    user_email = get_jwt_identity()
    try:
        from services.job_service import get_job_service
        saved = get_job_service().get_saved_filters(user_email=user_email)
        return jsonify({"filters": saved.get("filters") if saved else None}), 200
    except Exception as e:
        logger.error(f"Get saved job filters error: {e}")
        return jsonify({"error": "Failed to load filters"}), 500


@jobs_bp.route("/filters", methods=["PUT"])
@jwt_required()
def save_filters():
    user_email = get_jwt_identity()
    data = request.get_json(force=True) or {}
    filters = _sanitize_job_filters(data.get("filters", data))
    try:
        from services.job_service import get_job_service
        saved = get_job_service().save_filters(filters, user_email=user_email)
        return jsonify({"filters": saved["filters"], "updated_at": saved.get("updated_at")}), 200
    except Exception as e:
        logger.error(f"Save job filters error: {e}")
        return jsonify({"error": "Failed to save filters"}), 500


# ------------------------------------------------------------------
# Daily-pipeline named presets — list / save / delete
# ------------------------------------------------------------------

@jobs_bp.route("/pipeline/presets", methods=["GET"])
@jwt_required()
def list_pipeline_presets_route():
    user_email = get_jwt_identity()
    try:
        from services.job_service import get_job_service
        presets = get_job_service().list_pipeline_presets(user_email)
        return jsonify({"presets": presets}), 200
    except Exception as e:
        logger.error(f"List pipeline presets error: {e}")
        return jsonify({"error": "Failed to load presets"}), 500


@jobs_bp.route("/pipeline/presets", methods=["POST"])
@jwt_required()
def save_pipeline_preset_route():
    user_email = get_jwt_identity()
    data = request.get_json(force=True) or {}
    name = InputSanitizer.sanitize_string(str(data.get("name") or ""), max_length=60)
    raw_filters = data.get("filters") if isinstance(data.get("filters"), dict) else {}
    # Whitelist only fields we actually consume on the pipeline side so a
    # malicious or malformed payload can't smuggle other state through.
    allowed = {
        "linkedin_keywords", "workday_titles", "custom_role_terms",
        "past_days", "linkedin_count", "workday_limit", "include_indeed",
        "location", "experience_level", "employment_type", "work_arrangement",
        "domain_strict",
    }
    filters = {k: v for k, v in raw_filters.items() if k in allowed}
    try:
        from services.job_service import get_job_service
        out = get_job_service().save_pipeline_preset(user_email, name, filters)
        if not out.get("ok"):
            return jsonify({"error": out.get("error", "Failed")}), 400
        return jsonify(out), 200
    except Exception as e:
        logger.error(f"Save pipeline preset error: {e}")
        return jsonify({"error": "Failed to save preset"}), 500


@jobs_bp.route("/pipeline/presets/<path:name>", methods=["DELETE"])
@jwt_required()
def delete_pipeline_preset_route(name: str):
    user_email = get_jwt_identity()
    name = InputSanitizer.sanitize_string(name or "", max_length=60)
    if not name:
        return jsonify({"error": "Preset name is required"}), 400
    try:
        from services.job_service import get_job_service
        out = get_job_service().delete_pipeline_preset(user_email, name)
        return jsonify(out), 200
    except Exception as e:
        logger.error(f"Delete pipeline preset error: {e}")
        return jsonify({"error": "Failed to delete preset"}), 500


# ------------------------------------------------------------------
# JD ↔ Resume match scoring (deterministic, reuses scraped JDs)
# ------------------------------------------------------------------

@jobs_bp.route("/pipeline/match-scores", methods=["POST"])
@jwt_required()
def pipeline_match_scores():
    """Score a batch of pipeline rows against the caller's parsed resume.

    Body: { "items": [{ "id": "...", "title": "...", "description": "..." }, ...] }
    Returns: { "scores": { id: { match_score, matched_skills, missing_top, explain } },
               "meta":   { resume_skill_count, has_resume } }

    Stateless on the backend — the frontend fires this after a pipeline run
    lands and overlays the scores on the already-rendered tier groups. No
    LLM call, no caching needed: the comparison is regex-against-skills,
    fast enough to score 100 rows under ~50ms.
    """
    client_ip = get_client_ip(request)
    limiter = get_rate_limiter()
    if limiter.is_rate_limited(f"jobs_match_scores:{client_ip}", max_requests=30, window_seconds=300):
        return jsonify({"error": "Rate limit exceeded"}), 429

    user_email = get_jwt_identity()
    data = request.get_json(silent=True) or {}
    items_raw = data.get("items") or []
    if not isinstance(items_raw, list):
        return jsonify({"error": "items must be a list"}), 400
    # Cap input size — 200 is double the typical pipeline result, plenty of
    # headroom but bounded so a misbehaving client can't ask us to score 10k.
    items = items_raw[:200]

    try:
        from services.jd_match_scorer import score_batch
        scores, meta = score_batch(user_email, items)
        return jsonify({"scores": scores, "meta": meta}), 200
    except Exception as e:
        logger.error(f"Pipeline match-scores error: {e}")
        return jsonify({"error": "Failed to compute match scores"}), 500


# ------------------------------------------------------------------
# Saved Jobs CRUD
# ------------------------------------------------------------------

@jobs_bp.route("/saved", methods=["GET"])
@jwt_required()
def list_saved():
    user_email = get_jwt_identity()
    try:
        from services.job_service import get_job_service
        jobs = get_job_service().get_saved_jobs(user_email=user_email)
        return jsonify({"jobs": jobs}), 200
    except Exception as e:
        logger.error(f"List saved jobs error: {e}")
        return jsonify({"error": "Failed to load saved jobs"}), 500


@jobs_bp.route("/saved", methods=["POST"])
@jwt_required()
def save_job():
    user_email = get_jwt_identity()
    data = request.get_json(force=True) or {}
    job = data.get("job")
    if not job or not isinstance(job, dict):
        return jsonify({"error": "Job data is required"}), 400
    try:
        from services.job_service import get_job_service
        saved = get_job_service().save_job(job, user_email=user_email)
        return jsonify({"job": saved}), 201
    except Exception as e:
        logger.error(f"Save job error: {e}")
        return jsonify({"error": "Failed to save job"}), 500


@jobs_bp.route("/saved/<job_id>", methods=["PUT"])
@jwt_required()
def update_saved(job_id):
    user_email = get_jwt_identity()
    data = request.get_json(force=True) or {}
    status = data.get("status")
    notes = data.get("notes")
    if status and status not in ("interested", "applied", "interview", "offer", "rejected", "withdrawn"):
        return jsonify({"error": "Invalid status"}), 400
    try:
        from services.job_service import get_job_service
        updated = get_job_service().update_saved_job(job_id, status=status, notes=notes, user_email=user_email)
        if not updated:
            return jsonify({"error": "Job not found"}), 404
        return jsonify({"job": updated}), 200
    except Exception as e:
        logger.error(f"Update saved job error: {e}")
        return jsonify({"error": "Failed to update job"}), 500


@jobs_bp.route("/saved/<job_id>", methods=["DELETE"])
@jwt_required()
def delete_saved(job_id):
    user_email = get_jwt_identity()
    try:
        from services.job_service import get_job_service
        deleted = get_job_service().delete_saved_job(job_id, user_email=user_email)
        if not deleted:
            return jsonify({"error": "Job not found"}), 404
        return jsonify({"message": "Job removed"}), 200
    except Exception as e:
        logger.error(f"Delete saved job error: {e}")
        return jsonify({"error": "Failed to delete job"}), 500
