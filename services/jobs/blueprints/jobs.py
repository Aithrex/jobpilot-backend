"""
Jobs blueprint — Job search dashboard endpoints.

Authentication is handled by the unified auth service (/api/auth/login).
All endpoints require a valid JWT whose identity is the user's email.
"""
import logging
import re

from flask import Blueprint, Response, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity

from utils.security import (
    InputSanitizer,
    check_rate_limit,
    get_client_ip,
    get_rate_limiter,
)

jobs_bp = Blueprint("jobs", __name__)
logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# H-1B sponsorship intel (§6.3) — honest, sourced, never a prediction
# ------------------------------------------------------------------
@jobs_bp.route("/sponsorship", methods=["GET"])
@jwt_required()
def sponsorship():
    company = InputSanitizer.sanitize_string(request.args.get("company", ""), max_length=200)
    if not company:
        return jsonify({"error": "Query parameter 'company' is required"}), 400
    from services.sponsorship_service import get_signal
    return jsonify({"company": company, "sponsorship": get_signal(company)}), 200


@jobs_bp.route("/sponsorship/bulk", methods=["POST"])
@jwt_required()
def sponsorship_bulk():
    data = request.get_json(silent=True) or {}
    raw = data.get("companies")
    if not isinstance(raw, list):
        return jsonify({"error": "Body must be {\"companies\": [\"...\"]}"}), 400
    companies = [InputSanitizer.sanitize_string(str(c), max_length=200) for c in raw[:200] if c]
    from services.sponsorship_service import bulk_signals
    return jsonify({"signals": bulk_signals(companies)}), 200


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

# --- APIFY DISABLED (temporary): the BYO per-user Apify key endpoints are
# turned off (GET/PUT/DELETE /api/jobs/apify-key). Uncomment this whole block
# to re-enable. ---
# @jobs_bp.route("/apify-key", methods=["GET"])
# @jwt_required()
# def get_apify_key():
#     user_email = get_jwt_identity()
#     try:
#         from services.job_service import get_job_service
#         return jsonify(get_job_service().get_user_apify_key_status(user_email)), 200
#     except Exception as e:
#         logger.error(f"Get apify key error: {e}")
#         return jsonify({"error": "Failed to load key"}), 500
#
#
# @jobs_bp.route("/apify-key", methods=["PUT"])
# @jwt_required()
# def set_apify_key():
#     client_ip = get_client_ip(request)
#     limiter = get_rate_limiter()
#     if limiter.is_rate_limited(f"jobs_apify_key:{client_ip}", max_requests=10, window_seconds=300):
#         return jsonify({"error": "Rate limit exceeded"}), 429
#
#     user_email = get_jwt_identity()
#     data = request.get_json(silent=True) or {}
#     raw = data.get("apify_key", "")
#     if not isinstance(raw, str):
#         return jsonify({"error": "apify_key must be a string"}), 400
#     # Belt + braces: don't sanitize as 'string' here because the sanitizer
#     # may strip safe chars; we validate format inside the service.
#     try:
#         from services.job_service import get_job_service
#         result = get_job_service().set_user_apify_key(user_email, raw)
#         if not result.get("ok"):
#             return jsonify({"error": result.get("error", "Failed to save key")}), 400
#         return jsonify(result), 200
#     except Exception as e:
#         logger.error(f"Save apify key error: {e}")
#         return jsonify({"error": "Failed to save key"}), 500
#
#
# @jobs_bp.route("/apify-key", methods=["DELETE"])
# @jwt_required()
# def delete_apify_key():
#     user_email = get_jwt_identity()
#     try:
#         from services.job_service import get_job_service
#         return jsonify(get_job_service().delete_user_apify_key(user_email)), 200
#     except Exception as e:
#         logger.error(f"Delete apify key error: {e}")
#         return jsonify({"error": "Failed to remove key"}), 500


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
        from services.claude_client import claude_json, CLAUDE_FLASH
        from utils.db_connect import DBConnect
        # Pull the candidate's parsed resume so the draft references real
        # bullets / projects instead of generic platitudes. If they haven't
        # uploaded a resume yet the endpoint still works — falls back to a
        # generic "I'm interested" template.
        parser = ResumeParser(DBConnect().get_db())
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

        result = claude_json(
            prompt=prompt,
            model=CLAUDE_FLASH,
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


@jobs_bp.route("/title-seed", methods=["GET"])
@jwt_required()
def get_title_seed():
    """Union of job titles across ALL parsed resumes. Never calls the LLM.

    Cached by resume-set fingerprint (upload / delete / reparse). Job-search
    chips hydrate from this so a 3-resume user searches every title, not
    only the active file.
    """
    user_email = get_jwt_identity()
    try:
        from services.job_title_seed import public_title_seed
        from services import experience_view

        payload = public_title_seed(user_email)
        # The levels the board OPENS on (`{levels, source, years, preference}`,
        # `levels: []` = all). Here because this is the call the board already
        # makes on load; `for_user` never raises.
        payload["experience_view"] = experience_view.for_user(user_email)
        return jsonify(payload), 200
    except Exception as e:
        logger.error(f"title-seed error: {e}")
        return jsonify({"error": "Failed to load job titles"}), 500


@jobs_bp.route("/pipeline/suggest-filters", methods=["GET"])
@jwt_required()
def suggest_pipeline_filters():
    """Return resume-aware filter suggestions for the daily pipeline.

    LLM runs only when the parsed-resume set fingerprint has changed (or
    there is no stored synth). Repeat visits return the cached bundle.
    """
    client_ip = get_client_ip(request)
    limiter = get_rate_limiter()
    if limiter.is_rate_limited(f"jobs_suggest:{client_ip}", max_requests=15, window_seconds=300):
        return jsonify({"error": "Rate limit exceeded"}), 429

    user_email = get_jwt_identity()
    try:
        from services.filter_suggester_service import suggest_filters
        from services.job_title_seed import suggest_filters_cached

        suggestions, cached = suggest_filters_cached(
            user_email, synthesizer=suggest_filters,
        )
        return jsonify({"suggestions": suggestions, "cached": cached}), 200
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        logger.error(f"Filter suggestion error: {e}")
        return jsonify({"error": "Failed to generate suggestions"}), 500


def _salary_filter(data) -> dict:
    """`{min_salary, include_unknown_salary}` from a search body.

    `inventory_feed.build_feed` has read `payload["min_salary"]` since it was
    written, but no route ever put it there - the three search routes build
    their payload from an explicit whitelist, so the field was silently
    dropped and the filter has never once run in production.

    Unknown stays unknown. Most postings publish no band, so a floor that
    excluded them would hide the market rather than narrow it. The default is
    therefore to KEEP unpriced rows, and `include_unknown_salary: false` is
    the explicit opt-out for a user who only wants published pay.
    """
    try:
        raw = data.get("min_salary")
        min_salary = float(raw) if raw not in (None, "", False) else 0.0
    except (TypeError, ValueError):
        min_salary = 0.0
    # A negative or absurd floor is a client bug, not a filter.
    if min_salary < 0 or min_salary > 10_000_000:
        min_salary = 0.0
    inc = data.get("include_unknown_salary")
    return {
        "min_salary": min_salary or None,
        "include_unknown_salary": True if inc is None else bool(inc),
    }


@jobs_bp.route("/pipeline", methods=["POST"])
@jwt_required()
def daily_pipeline():
    """Run the daily Apify pipeline: parallel LinkedIn + Workday scrape, score, tier.

    Body (all optional - sensible defaults match job_pipeline.py):
        {
          "linkedin_keywords": ["Cloud Engineer DevOps", ...],
          "workday_titles":    ["Cloud Engineer", "Backend Engineer", ...],
          "past_days":         1,
          "custom_role_terms":   ["security", "data engineer"],   # POSITIVE
          "exclude_title_terms": ["nurse", "sales"],               # NEGATIVE
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
    # Negative list. Kept structurally separate from `custom_role_terms` all
    # the way down: the pipeline drops a matching title before any positive
    # rule can readmit it. Accepts the legacy `anti_keywords` spelling because
    # the suggester's own LLM schema uses that word.
    exclude_title_terms = _split_lines(
        data.get("exclude_title_terms") if data.get("exclude_title_terms") is not None
        else data.get("anti_keywords"),
        max_items=20, max_len=80)

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
        "exclude_title_terms": exclude_title_terms or None,
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
# Workday Jobs tab — direct CXS fan-out across the validated tenant catalog.
# Free (no Apify credits), async submit → poll /api/resume/job/<id>.
# ------------------------------------------------------------------

@jobs_bp.route("/workday/catalog", methods=["GET"])
@jwt_required()
def workday_catalog():
    """Catalog metadata for the Workday Jobs tab's industry/company filters."""
    try:
        from services.workday_jobs_service import catalog_summary
        return jsonify(catalog_summary()), 200
    except Exception as e:
        logger.error(f"Workday catalog error: {e}")
        return jsonify({"error": "Failed to load Workday catalog"}), 500


@jobs_bp.route("/workday/search", methods=["POST"])
@jwt_required()
def workday_search():
    """Run a Workday Jobs search across the curated tenant catalog.

    Body (all optional):
        {
          "titles":       ["Backend Engineer", ...],   # prefilled from resume
          "industries":   ["fintech", "banking"],      # [] = all industries
          "companies":    ["cvshealth", ...],          # [] = all tenants
          "location":     "Texas",   # optional narrowing WITHIN the US
          "remote_only":  false,
          "us_only":      true,      # server-side US country facet (default)
          "force_refresh": false
        }
    Returns: {"job_id": "..."} (202) — poll /api/resume/job/<id>. The result
    carries the full 30-day window; recency filtering happens client-side.
    """
    client_ip = get_client_ip(request)
    limiter = get_rate_limiter()
    if limiter.is_rate_limited(f"jobs_workday:{client_ip}", max_requests=6, window_seconds=300):
        return jsonify({"error": "Rate limit exceeded. Workday searches are limited to 6 / 5 min."}), 429

    data = request.get_json(silent=True) or {}
    titles = _split_lines(data.get("titles"), max_items=40, max_len=120)
    industries = _split_lines(data.get("industries"), max_items=15, max_len=40)
    companies = _split_lines(data.get("companies"), max_items=100, max_len=60)
    location = InputSanitizer.sanitize_string(str(data.get("location") or ""), max_length=80)
    remote_only = bool(data.get("remote_only", False))
    us_only = bool(data.get("us_only", True))
    force_refresh = bool(data.get("force_refresh", False))

    user_email = get_jwt_identity()
    payload = {
        "titles": titles,
        "industries": industries,
        "companies": companies,
        "location": location,
        "remote_only": remote_only,
        "us_only": us_only,
        "force_refresh": force_refresh,
        **_salary_filter(data),
        "user_email": user_email,
        # Viewer timezone: "posted today" is computed in the USER's calendar
        # day (service clamps to the real-world offset range).
        "tz_offset_minutes": data.get("tz_offset_minutes"),
    }
    try:
        from services.resume_service import get_resume_service, ResumeService
        svc = get_resume_service()
        job_id = svc.create_job("workday_jobs", payload, user_email=user_email)
        ResumeService.invoke_async(job_id, "workday_jobs", payload)
        return jsonify({"job_id": job_id}), 202
    except Exception as e:
        logger.error(f"Workday search submit error: {e}")
        return jsonify({"error": "Failed to start Workday search"}), 500


# ------------------------------------------------------------------
# Career Pages tab — direct fan-out across companies' OWN career sites
# (Greenhouse/Lever/Ashby/SmartRecruiters/Workable/Recruitee public JSON
# APIs — the non-Workday universe). Same async submit/poll pattern.
# ------------------------------------------------------------------

@jobs_bp.route("/career-pages/catalog", methods=["GET"])
@jwt_required()
def career_pages_catalog():
    """Catalog metadata for the Career Pages tab's industry/company filters."""
    try:
        from services.career_pages_service import catalog_summary
        return jsonify(catalog_summary()), 200
    except Exception as e:
        logger.error(f"Career pages catalog error: {e}")
        return jsonify({"error": "Failed to load Career Pages catalog"}), 500


@jobs_bp.route("/career-pages/search", methods=["POST"])
@jwt_required()
def career_pages_search():
    """Run a Career Pages search across the validated ATS catalog.

    Body mirrors /workday/search: titles, industries, companies (slugs),
    location, remote_only, us_only, force_refresh.
    Returns: {"job_id": "..."} (202) — poll /api/resume/job/<id>.
    """
    client_ip = get_client_ip(request)
    limiter = get_rate_limiter()
    if limiter.is_rate_limited(f"jobs_career_pages:{client_ip}", max_requests=6, window_seconds=300):
        return jsonify({"error": "Rate limit exceeded. Career Pages searches are limited to 6 / 5 min."}), 429

    data = request.get_json(silent=True) or {}
    payload = {
        "titles": _split_lines(data.get("titles"), max_items=40, max_len=120),
        "industries": _split_lines(data.get("industries"), max_items=15, max_len=40),
        "companies": _split_lines(data.get("companies"), max_items=100, max_len=60),
        "location": InputSanitizer.sanitize_string(str(data.get("location") or ""), max_length=80),
        "remote_only": bool(data.get("remote_only", False)),
        "us_only": bool(data.get("us_only", True)),
        "force_refresh": bool(data.get("force_refresh", False)),
        **_salary_filter(data),
        "user_email": get_jwt_identity(),
        "tz_offset_minutes": data.get("tz_offset_minutes"),
    }
    try:
        from services.resume_service import get_resume_service, ResumeService
        svc = get_resume_service()
        job_id = svc.create_job("career_pages", payload, user_email=payload["user_email"])
        ResumeService.invoke_async(job_id, "career_pages", payload)
        return jsonify({"job_id": job_id}), 202
    except Exception as e:
        logger.error(f"Career pages search submit error: {e}")
        return jsonify({"error": "Failed to start Career Pages search"}), 500


# ------------------------------------------------------------------
# MNCs / Big Tech tab — third career-pages variant over the curated
# MNC_CATALOG (board ATS + Workday CXS + enterprise platforms, with a
# JSearch aggregator fallback for bot-walled companies). Same async
# submit/poll pattern.
# ------------------------------------------------------------------

@jobs_bp.route("/mnc/catalog", methods=["GET"])
@jwt_required()
def mnc_catalog():
    """Catalog metadata for the MNCs tab's sector/company filters."""
    try:
        from services.mnc_jobs_service import catalog_summary
        return jsonify(catalog_summary()), 200
    except Exception as e:
        logger.error(f"MNC catalog error: {e}")
        return jsonify({"error": "Failed to load MNC catalog"}), 500


@jobs_bp.route("/mnc/search", methods=["POST"])
@jwt_required()
def mnc_search():
    """Run an MNCs search across the verified MNC catalog.

    Body mirrors /career-pages/search: titles, industries (sector keys),
    companies (slugs), location, remote_only, us_only, force_refresh.
    Returns: {"job_id": "..."} (202) — poll /api/resume/job/<id>.
    """
    client_ip = get_client_ip(request)
    limiter = get_rate_limiter()
    if limiter.is_rate_limited(f"jobs_mnc:{client_ip}", max_requests=6, window_seconds=300):
        return jsonify({"error": "Rate limit exceeded. MNC searches are limited to 6 / 5 min."}), 429

    data = request.get_json(silent=True) or {}
    payload = {
        "titles": _split_lines(data.get("titles"), max_items=40, max_len=120),
        "industries": _split_lines(data.get("industries"), max_items=15, max_len=40),
        "companies": _split_lines(data.get("companies"), max_items=100, max_len=60),
        "location": InputSanitizer.sanitize_string(str(data.get("location") or ""), max_length=80),
        "remote_only": bool(data.get("remote_only", False)),
        "us_only": bool(data.get("us_only", True)),
        "force_refresh": bool(data.get("force_refresh", False)),
        **_salary_filter(data),
        "user_email": get_jwt_identity(),
        "tz_offset_minutes": data.get("tz_offset_minutes"),
    }
    try:
        from services.resume_service import get_resume_service, ResumeService
        svc = get_resume_service()
        job_id = svc.create_job("mnc_jobs", payload, user_email=payload["user_email"])
        ResumeService.invoke_async(job_id, "mnc_jobs", payload)
        return jsonify({"job_id": job_id}), 202
    except Exception as e:
        logger.error(f"MNC search submit error: {e}")
        return jsonify({"error": "Failed to start MNC search"}), 500


# ------------------------------------------------------------------
# Tech feed (ADR-005) - ONE inventory-first search for software and
# technical employment, replacing the three live-crawl fan-outs above.
#
# Mounted as a SUBPATH of the existing /api/jobs prefix on purpose: a new
# top-level /api/<prefix> needs a matching external Terraform route (there is
# no .tf in this repo) or it 404s in prod while working locally. /api/jobs is
# already registered, so /api/jobs/tech-feed/* needs no infrastructure change.
# ------------------------------------------------------------------

@jobs_bp.route("/tech-feed/taxonomy", methods=["GET"])
@jwt_required()
def tech_feed_taxonomy():
    """The role-family / specialty tree the filter UI renders.

    Static for a given `taxonomy_version`, so the client may cache it hard and
    revalidate only when the version in a search response changes.
    """
    try:
        from services import tech_feed_service
        payload = tech_feed_service.taxonomy_catalog()
        resp = jsonify(payload)
        resp.headers["Cache-Control"] = "private, max-age=3600"
        return resp, 200
    except Exception as e:
        logger.error(f"Tech feed taxonomy error: {e}")
        return jsonify({"error": "Failed to load taxonomy"}), 500


@jobs_bp.route("/tech-feed/search", methods=["POST"])
@jwt_required()
def tech_feed_search():
    """Search the canonical inventory for software / technical roles.

    Body: see `tech_feed_service.normalize_request` - every field is optional
    and anything unrecognised is dropped rather than passed through.

    Responses:
      200 {search_id, jobs, next_cursor, has_more, facets, applied_filters,
           taxonomy_version, fit_score_version, inventory_watermark,
           coverage, source_health, warnings}
      202 {job_id}  - only when the caller asked for async, or when the first
           page needs a resume profile that is not warm. Poll the EXISTING
           /api/resume/job/<id> contract; no new poll endpoint.

    Deliberately NOT entitlement-gated. Browsing the job market is not a paid
    feature today and making it one is a product decision, not an
    implementation detail (see CLAUDE.md / ADR-005 sec. 9).
    """
    client_ip = get_client_ip(request)
    limiter = get_rate_limiter()
    # Generous relative to the crawl endpoints (6/5min) because this is a
    # bounded indexed read, not a fan-out: paging and filter changes are
    # normal interaction, not abuse.
    if limiter.is_rate_limited(f"jobs_tech_feed:{client_ip}",
                               max_requests=120, window_seconds=300):
        return jsonify({"error": "Rate limit exceeded. "
                                 "Tech feed searches are limited to 120 / 5 min."}), 429

    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({"error": "Body must be a JSON object"}), 400

    user_email = get_jwt_identity()
    payload = dict(data)
    payload["user_email"] = user_email
    payload.setdefault("tz_offset_minutes", data.get("tz_offset_minutes"))

    want_async = bool(data.get("async"))
    try:
        from services import tech_feed_service
        if want_async:
            from services.resume_service import get_resume_service, ResumeService
            svc = get_resume_service()
            job_id = svc.create_job("tech_feed_search", payload,
                                    user_email=user_email)
            ResumeService.invoke_async(job_id, "tech_feed_search", payload)
            return jsonify({"job_id": job_id}), 202
        result = tech_feed_service.search(payload)
        if result.get("error") == "cursor_expired":
            # 410 and not 401: the FE force-logs-out on ANY 401, and an
            # expired scroll position must never sign a user out.
            return jsonify(result), 410
        return jsonify(result), 200
    except Exception as e:
        logger.error(f"Tech feed search error: {e}", exc_info=True)
        return jsonify({"error": "Failed to run tech feed search"}), 500


@jobs_bp.route("/tech-feed/coverage", methods=["GET"])
@jwt_required()
def tech_feed_coverage():
    """Inventory coverage + per-source crawl health.

    Its own endpoint so the UI can render an honest "what you are NOT seeing"
    banner without paying for it on every search.
    """
    try:
        from services import tech_feed_service
        return jsonify({
            "coverage": tech_feed_service.coverage_block(),
            "source_health": tech_feed_service.source_health_block(),
        }), 200
    except Exception as e:
        logger.error(f"Tech feed coverage error: {e}")
        return jsonify({"error": "Failed to load coverage"}), 500


# --- Tech feed telemetry -------------------------------------------------
# Non-PII by construction: the account is a salted one-way pseudonym, the
# dimensions are a closed allowlist of KEYS (never filter values), and result
# counts are bucketed. See `services/tech_feed_telemetry.py`.

@jobs_bp.route("/tech-feed/telemetry", methods=["POST"])
@jwt_required()
def tech_feed_telemetry():
    """Batch of `{event, count?, dimensions?}`. Returns `{accepted, rejected}`.

    Deliberately fire-and-forget from the client's point of view: telemetry
    must never be able to fail a user action, so a malformed batch is counted
    and dropped rather than 400ed.
    """
    client_ip = get_client_ip(request)
    limiter = get_rate_limiter()
    if limiter.is_rate_limited(f"jobs_tech_feed_telemetry:{client_ip}",
                               max_requests=120, window_seconds=300):
        # 204, not 429: a rate-limited telemetry batch is dropped silently by
        # design. Telling the client to retry would make the busiest sessions
        # send the most traffic.
        return ("", 204)
    data = request.get_json(silent=True) or {}
    try:
        from services import tech_feed_telemetry as tft
        tft.ensure_indexes()
        return jsonify(tft.record((data or {}).get("events") or [],
                                  user_email=get_jwt_identity())), 200
    except Exception as e:
        logger.warning(f"Tech feed telemetry error: {e}")
        return ("", 204)


# --- Tech feed saved searches -------------------------------------------
# Server-owned, with the client keeping a local mirror. Same subpath as the
# rest of the tech feed, so no new edge route is needed.

@jobs_bp.route("/tech-feed/employer-preferences", methods=["GET"])
@jwt_required()
def tech_feed_employer_preferences_get():
    """The user's direct-employer policy (`employer-policy-1.0.0`).

    `{preset, exclude{...}, set_by: "user"|"default", version}`. A user with
    no stored choice gets the documented default (`strict_direct`, unknown
    shown as unverified) with `set_by: "default"` - nothing is written.
    """
    try:
        from services import employer_policy
        prefs = employer_policy.get_prefs(get_jwt_identity())
        prefs.pop("updated_at", None)
        return jsonify(prefs), 200
    except Exception as e:
        logger.error(f"Employer preferences read error: {type(e).__name__}")
        return jsonify({"error": "Failed to load employer preferences"}), 500


@jobs_bp.route("/tech-feed/employer-preferences", methods=["PUT"])
@jwt_required()
def tech_feed_employer_preferences_put():
    """Store the user's choice. Body: `{preset?, exclude?{bool...}}`.

    400 on an unusable body (never 401 - that is reserved for a bad token).
    The preset is derived from the toggles, so `strict_direct` with a toggle
    changed is stored as `custom`.
    """
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Body must be a JSON object"}), 400
    # Whitelist + InputSanitizer: only these keys survive, strings bounded.
    body = {}
    if "preset" in data:
        body["preset"] = InputSanitizer.sanitize_string(str(data.get("preset") or ""),
                                                        max_length=20)
    if isinstance(data.get("exclude"), dict):
        body["exclude"] = {str(k)[:40]: bool(v) for k, v in list(data["exclude"].items())[:20]
                           if not isinstance(v, (dict, list))}
    try:
        from services import employer_policy
        stored = employer_policy.put_prefs(get_jwt_identity(), body)
        if stored is None:
            return jsonify({"error": "invalid_preferences",
                            "message": "Send a preset (strict_direct, custom, off) "
                                       "and/or an exclude object of booleans."}), 400
        stored.pop("updated_at", None)
        return jsonify(stored), 200
    except Exception as e:
        logger.error(f"Employer preferences write error: {type(e).__name__}")
        return jsonify({"error": "Failed to save employer preferences"}), 500


@jobs_bp.route("/tech-feed/saved-searches", methods=["GET"])
@jwt_required()
def tech_feed_saved_searches_list():
    """`{searches: [{id, name, filters, createdAt, updatedAt, fingerprint}]}`."""
    try:
        from services import tech_feed_saved_searches as tfss
        return jsonify(tfss.list_for(get_jwt_identity())), 200
    except Exception as e:
        logger.error(f"Tech feed saved-search list error: {e}")
        return jsonify({"error": "Failed to load saved searches"}), 500


@jobs_bp.route("/tech-feed/saved-searches", methods=["POST"])
@jwt_required()
def tech_feed_saved_searches_save():
    """Upsert one saved search, keyed on the CLIENT's own id.

    Body: `{id, name, filters}`. `id` is the client's opaque key — sending the
    same one twice updates one row rather than creating two, which is what
    makes an offline queue safe to drain.
    """
    client_ip = get_client_ip(request)
    limiter = get_rate_limiter()
    if limiter.is_rate_limited(f"jobs_tech_feed_saved:{client_ip}",
                               max_requests=60, window_seconds=300):
        return jsonify({"error": "Rate limit exceeded."}), 429
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({"error": "Body must be a JSON object"}), 400
    try:
        from services import tech_feed_saved_searches as tfss
        tfss.ensure_indexes()
        return jsonify(tfss.save(
            get_jwt_identity(),
            client_id=data.get("id"),
            name=InputSanitizer.sanitize_string(data.get("name", ""), max_length=80),
            filters=data.get("filters"),
            alerts=data.get("alerts"))), 200
    except ValueError as e:
        # The per-account cap. A 409 rather than a 400: the request is
        # well-formed and the account is full, and the client should say so
        # rather than reporting the user's input as malformed.
        return jsonify({"error": str(e)}), 409
    except Exception as e:
        logger.error(f"Tech feed saved-search save error: {e}", exc_info=True)
        return jsonify({"error": "Failed to save the search"}), 500


@jobs_bp.route("/tech-feed/saved-searches/<search_id>", methods=["DELETE"])
@jwt_required()
def tech_feed_saved_searches_delete(search_id):
    try:
        from services import tech_feed_saved_searches as tfss
        return jsonify(tfss.delete(get_jwt_identity(), search_id)), 200
    except Exception as e:
        logger.error(f"Tech feed saved-search delete error: {e}")
        return jsonify({"error": "Failed to delete the search"}), 500


@jobs_bp.route("/tech-feed/saved-searches/reconcile", methods=["POST"])
@jwt_required()
def tech_feed_saved_searches_reconcile():
    """Push the client's local list up, then return the server's.

    Called once on mount. Never deletes: a search the client does not have is
    one saved on another device, not one the user removed.
    """
    data = request.get_json(silent=True) or {}
    try:
        from services import tech_feed_saved_searches as tfss
        tfss.ensure_indexes()
        return jsonify(tfss.reconcile(
            get_jwt_identity(), (data or {}).get("searches"))), 200
    except Exception as e:
        logger.error(f"Tech feed saved-search reconcile error: {e}", exc_info=True)
        return jsonify({"error": "Failed to reconcile saved searches"}), 500


@jobs_bp.route("/tech-feed/company-requests", methods=["POST"])
@jwt_required()
def tech_feed_company_request():
    """Queue a company for later verification. Does not fetch the URL."""
    client_ip = get_client_ip(request)
    limiter = get_rate_limiter()
    email = get_jwt_identity()
    if limiter.is_rate_limited(f"jobs_tech_feed_creq:{email or client_ip}",
                               max_requests=8, window_seconds=3600):
        return jsonify({"error": "Rate limit exceeded."}), 429
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({"error": "Body must be a JSON object"}), 400
    try:
        from services import tech_feed_company_requests as reqs
        body = reqs.submit(
            email,
            company_name=data.get("company_name"),
            career_url=data.get("career_url"))
        return jsonify(body), 200
    except OverflowError as e:
        return jsonify({"error": str(e)}), 429
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error("Tech feed company-request error: %s", e, exc_info=True)
        return jsonify({"error": "Failed to submit the company request"}), 500


@jobs_bp.route("/tech-feed/company-requests/<request_id>", methods=["GET"])
@jwt_required()
def tech_feed_company_request_get(request_id):
    try:
        from services import tech_feed_company_requests as reqs
        row = reqs.get_for_user(get_jwt_identity(), request_id)
        if not row:
            return jsonify({"error": "Not found"}), 404
        return jsonify(row), 200
    except Exception as e:
        logger.error("Tech feed company-request get error: %s", e)
        return jsonify({"error": "Failed to load the request"}), 500


# --- Tech feed employer directory ---------------------------------------
# Same /api/jobs/tech-feed subpath as everything else here, for the same
# infrastructure reason: a new top-level /api/<prefix> needs a matching entry
# in var.service_config and 404s in prod until it exists.

@jobs_bp.route("/tech-feed/companies", methods=["GET"])
@jwt_required()
def tech_feed_companies():
    """Company autocomplete over the canonical employer registry.

    `GET /api/jobs/tech-feed/companies?q=stri&limit=10&technology_only=1`
    -> `{query, companies: [{employer_id, display_name, aliases, categories,
        cohorts, domain, identity: {basis, confidence, operating_status,
        redirects_to}, career_source_verified, active_postings,
        core_tech_postings}], truncated}`

    `?ids=emp_a,emp_b` resolves ids back to names instead, so a saved or
    bookmarked filter renders as company names rather than as hex.

    The payload is deliberately narrow: nothing from `career_sources` appears
    beyond the `career_source_verified` boolean. Ownership evidence names the
    endpoints we fetch and the domains we probe, and that is operational
    detail about our crawl, not information about the employer.
    """
    client_ip = get_client_ip(request)
    limiter = get_rate_limiter()
    # Keystroke-driven, so the ceiling is per-minute rather than per-5-minutes,
    # and it is the reason the handler does at most two bounded queries.
    if limiter.is_rate_limited(f"jobs_tech_feed_companies:{client_ip}",
                               max_requests=60, window_seconds=60):
        return jsonify({"error": "Rate limit exceeded. "
                                 "Company lookups are limited to 60 / min."}), 429
    try:
        from services import employer_directory
        raw_ids = InputSanitizer.sanitize_string(
            request.args.get("ids", ""), max_length=1000)
        if raw_ids:
            ids = [i.strip() for i in raw_ids.split(",") if i.strip()][:25]
            return jsonify(employer_directory.lookup(ids)), 200

        query = InputSanitizer.sanitize_string(
            request.args.get("q", ""), max_length=120)
        try:
            limit = int(request.args.get("limit", 10))
        except (TypeError, ValueError):
            limit = 10
        cats = [InputSanitizer.sanitize_string(c, max_length=40)
                for c in (request.args.get("categories", "").split(","))
                if c.strip()][:10]
        cohorts = [InputSanitizer.sanitize_string(c, max_length=40)
                   for c in (request.args.get("cohorts", "").split(","))
                   if c.strip()][:8]
        tech_only = str(request.args.get("technology_only", "")).strip().lower() \
            in ("1", "true", "yes")
        return jsonify(employer_directory.search(
            query, limit=limit, categories=cats, cohorts=cohorts,
            technology_only=tech_only)), 200
    except Exception as e:
        logger.error(f"Tech feed company lookup error: {e}", exc_info=True)
        return jsonify({"error": "Failed to look up companies"}), 500


@jobs_bp.route("/tech-feed/company-categories", methods=["GET"])
@jwt_required()
def tech_feed_company_categories():
    """The canonical employer-category facet, with counts.

    Replaces free-form category entry, which accepted any string and silently
    matched nothing when the string was not one of ours.
    """
    try:
        from services import employer_directory
        return jsonify(employer_directory.categories()), 200
    except Exception as e:
        logger.error(f"Tech feed company categories error: {e}")
        return jsonify({"error": "Failed to load company categories"}), 500


# --- Tech feed feedback -------------------------------------------------
# Mounted under the SAME /api/jobs/tech-feed subpath as the routes above, for
# the same infrastructure reason: a new top-level /api/<prefix> would need a
# matching entry in var.service_config and would 404 in prod until it existed.

@jobs_bp.route("/tech-feed/feedback", methods=["POST"])
@jwt_required()
def tech_feed_feedback_record():
    """Record one piece of feedback on one posting.

    Body: {job_id (required), reason (required, one of
           tech_feed_feedback.REASONS), dedupe_group_id?, employer_id?,
           search_id?, note?}

    Responses:
      204  recorded (no body). Idempotent per (user, job_id): re-posting
           updates the single existing row rather than adding another.
      400  {"error": ...} - missing job_id, or a reason outside the closed set.
      429  {"error": ...} - rate limited.

    Only three of the six reasons hide anything (`not_interested`,
    `irrelevant_role`, `duplicate`). `wrong_company`, `stale_job` and
    `incorrect_metadata` are data-quality reports and are recorded but never
    used to suppress a posting - see `tech_feed_feedback`'s module docstring.
    """
    client_ip = get_client_ip(request)
    limiter = get_rate_limiter()
    # Higher than the search limit: dismissing cards is a per-row gesture, so a
    # user scrolling a feed legitimately produces many more of these than
    # searches. Still bounded, because this is an authenticated write.
    if limiter.is_rate_limited(f"jobs_tech_feed_feedback:{client_ip}",
                               max_requests=240, window_seconds=300):
        return jsonify({"error": "Rate limit exceeded. Tech feed feedback is "
                                 "limited to 240 / 5 min."}), 429

    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({"error": "Body must be a JSON object"}), 400

    from services import tech_feed_feedback

    def _opt(key, limit=128):
        """Sanitized scalar or "". A container is rejected outright rather than
        stringified, so nothing user-supplied can look like a Mongo operator."""
        value = data.get(key)
        if isinstance(value, (dict, list)):
            return ""
        return InputSanitizer.sanitize_string(str(value or ""), max_length=limit)

    job_id = _opt("job_id")
    if not job_id:
        return jsonify({"error": "job_id is required"}), 400

    raw_reason = data.get("reason")
    reason = raw_reason.strip().lower() if isinstance(raw_reason, str) else ""
    if reason not in tech_feed_feedback.REASONS:
        return jsonify({"error": "reason must be one of: "
                                 + ", ".join(tech_feed_feedback.REASONS)}), 400

    try:
        tech_feed_feedback.record(
            get_jwt_identity(),
            job_id=job_id,
            reason=reason,
            dedupe_group_id=_opt("dedupe_group_id"),
            employer_id=_opt("employer_id"),
            search_id=_opt("search_id", 64),
            note=_opt("note", tech_feed_feedback.MAX_NOTE_CHARS),
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error(f"Tech feed feedback record error: {e}")
        return jsonify({"error": "Failed to record feedback"}), 500
    return "", 204


@jobs_bp.route("/tech-feed/feedback/<job_id>", methods=["DELETE"])
@jwt_required()
def tech_feed_feedback_undo(job_id):
    """Undo feedback on one posting. Always 204 - deleting nothing is not an
    error, so a double-tapped undo and a retried request behave identically."""
    try:
        from services import tech_feed_feedback
        tech_feed_feedback.undo(get_jwt_identity(), job_id)
    except Exception as e:
        logger.error(f"Tech feed feedback undo error: {e}")
        return jsonify({"error": "Failed to remove feedback"}), 500
    return "", 204


@jobs_bp.route("/tech-feed/feedback", methods=["GET"])
@jwt_required()
def tech_feed_feedback_list():
    """The caller's current suppression set.

    Exists so a second device converges on the same feed: suppression is
    applied server-side during search, and this endpoint lets a client hide the
    same cards optimistically without waiting for a round trip.

    `employer_ids` is always empty today and the key is present only for shape
    stability - no reason suppresses at the employer level.
    """
    try:
        from services import tech_feed_feedback
        payload = tech_feed_feedback.suppressed_for(get_jwt_identity())
        resp = jsonify(payload)
        # Short and PRIVATE: this is per-user state that changes on every
        # dismissal, so it must never land in a shared cache.
        resp.headers["Cache-Control"] = "private, max-age=30"
        return resp, 200
    except Exception as e:
        logger.error(f"Tech feed feedback list error: {e}")
        return jsonify({"error": "Failed to load feedback"}), 500


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
# POST /api/jobs/trust-check   (scam-shield deep scan, on-demand)
# ------------------------------------------------------------------

@jobs_bp.route("/trust-check", methods=["POST"])
@jwt_required()
def trust_check():
    """Deep scam / ghost-job scan of a single posting (heuristics + FLASH).

    Body: { "job": {title, company, description, salary, source, apply_link, location} }
    Returns: { trust_score, trust_band, trust_flags, trust_reasons, llm_rationale?, advice? }

    Safety is FREE: rate-limited but never entitlement-gated. Protecting a student
    from an employment scam must not sit behind a paywall — it's the whole point.
    """
    client_ip = get_client_ip(request)
    limiter = get_rate_limiter()
    if limiter.is_rate_limited(f"jobs_trust:{client_ip}", max_requests=20, window_seconds=60):
        return jsonify({"error": "Rate limit exceeded — try again in a minute"}), 429

    data = request.get_json(silent=True) or {}
    job = data.get("job")
    if not job or not isinstance(job, dict):
        return jsonify({"error": "Job data is required"}), 400

    safe = {
        "title": InputSanitizer.sanitize_string(str(job.get("title") or ""), max_length=200),
        "company": InputSanitizer.sanitize_string(str(job.get("company") or ""), max_length=200),
        "location": InputSanitizer.sanitize_string(str(job.get("location") or ""), max_length=200),
        "salary": InputSanitizer.sanitize_string(str(job.get("salary") or ""), max_length=120),
        "source": InputSanitizer.sanitize_string(str(job.get("source") or ""), max_length=60),
        "apply_link": str(job.get("apply_link") or "")[:600],
        "description": InputSanitizer.sanitize_string(str(job.get("description") or ""), max_length=8000),
    }
    try:
        from services.job_legitimacy_service import deep_scan
        return jsonify(deep_scan(safe, use_llm=True)), 200
    except Exception as e:
        logger.error(f"Job trust-check error: {e}")
        return jsonify({"error": "Scan failed"}), 500


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

    from services.tailoring_policy import enabled as evidence_enabled
    from services import assessment_service as _assess
    use_async_pipeline = evidence_enabled() or _assess.enabled()
    if use_async_pipeline and len(job_description) > 56000:
        return jsonify({"error": "Job description is too long (max 56000 characters)."}), 400
    job_description = InputSanitizer.sanitize_string(
        job_description, max_length=56000 if use_async_pipeline else 6000,
    )
    if not job_description:
        return jsonify({"error": "Invalid job description"}), 400

    user_email = get_jwt_identity()

    try:
        from services.resume_service import get_resume_service
        svc = get_resume_service()
        if use_async_pipeline:
            from services import quota_ledger_service as ledger
            from services.resume_service import ResumeService
            allowed, entries, usage = ledger.reserve_tailor(user_email, idempotency_key=data.get("idempotency_key"))
            if not allowed:
                return jsonify({"error": "daily_quota_exceeded", "usage": usage}), 429
            payload = {"job_description": job_description, "user_email": user_email,
                       "quota_entry_id": entries[0], "selection_mode": "auto"}
            if _assess.enabled():
                payload = _assess.adapt_ats_scores_job_payload(payload)
            try:
                job_id = svc.create_job("extract_and_tailor", payload, user_email=user_email)
                ledger.attach_job(entries[0], job_id)
                ResumeService.invoke_async(job_id, "extract_and_tailor", payload)
            except Exception:
                ledger.refund_entry(entries[0])
                raise
            return jsonify({"job_id": job_id, "usage": usage}), 202

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
        # Additive: union across every parsed resume so older clients that
        # still read GET /jobs/resume can seed search from all files.
        try:
            from services.job_title_seed import union_titles_from_assets
            from services import resume_access
            resume["job_titles_all"] = union_titles_from_assets(
                resume_access.list_parsed_assets(user_email),
            )
        except Exception:
            logger.exception("job_titles_all union failed")
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
        "exclude_title_terms",
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
    Returns: { "scores": { id: { score, band, confidence, score_version,
                                 components, explanation, missing_requirements,
                                 hard_constraints, ...legacy match_score/
                                 matched_skills/missing_top } },
               "meta":   { score_version, resume_skill_count, has_resume,
                           resume_years, resume_role_families } }

    ADR-002: this is the SAME shared fit engine the guest path and the feed
    pipelines use, so an authenticated user and a guest looking at the same
    posting see the same number. It replaced jd_match_scorer, whose
    denominator came from the resume's own skill list.

    Stateless on the backend — the frontend fires this after a pipeline run
    lands and overlays the scores on the already-rendered tier groups. No
    LLM call in this path (deterministic rules only), fast enough to score
    100 rows well inside the request budget.
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
        from services.fit_score import score_batch_for_user
        scores, meta = score_batch_for_user(user_email, items)
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


@jobs_bp.route("/tracker", methods=["POST"])
@jwt_required()
def touch_pipeline():
    """Create/update Saved + Tracker for listing Save / open / apply / tailor."""
    user_email = get_jwt_identity()
    data = request.get_json(force=True) or {}
    job = data.get("job")
    intent = (data.get("intent") or "").strip().lower()
    if not job or not isinstance(job, dict):
        return jsonify({"error": "Job data is required"}), 400
    try:
        from services.job_service import get_job_service
        saved = get_job_service().touch_pipeline(job, user_email, intent)
        return jsonify({"job": saved}), 200
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error(f"Pipeline touch error: {e}")
        return jsonify({"error": "Failed to update tracker"}), 500


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


# ------------------------------------------------------------------
# GET /api/jobs/logo/<key> — public company-logo asset (cached favicon)
# ------------------------------------------------------------------
# Rides the existing ANY /api/jobs/{proxy+} edge route — NO new Terraform
# prefix. PUBLIC (an <img> tag can't send Authorization) and never 401.
# The key is format-validated (exactly 40 lowercase hex chars) BEFORE any
# Mongo filter. The service NEVER fetches for a key without an existing
# company_logos doc — docs are created only by the stamp/warm paths, which
# derive keys from real scraped companies (no SSRF-by-hash oracle).
_LOGO_KEY_RE = re.compile(r"^[0-9a-f]{40}$")


@jobs_bp.route("/logo/<key>", methods=["GET"])
def company_logo(key):
    if not isinstance(key, str) or not _LOGO_KEY_RE.match(key):
        return jsonify({"error": "Not found"}), 404

    client_ip = get_client_ip(request)
    limiter = get_rate_limiter()
    if limiter.is_rate_limited(f"jobs_logo:{client_ip}", max_requests=300, window_seconds=300):
        return jsonify({"error": "Rate limit exceeded"}), 429

    try:
        from services.company_logo_service import get_logo_bytes
        got = get_logo_bytes(key)
    except Exception as e:
        logger.error(f"Logo serve error: {type(e).__name__}")
        got = None
    if not got:
        # Unknown key, negative-cached miss, or fetch failure — all the same
        # generic 404 (the FE falls back to a monogram).
        return jsonify({"error": "Not found"}), 404

    data, content_type = got
    resp = Response(data, mimetype=content_type)
    # Immutable: the key is a content-addressed sha1 of the company name and
    # the underlying icon changes rarely; a week of edge/browser caching is
    # safe. The app-level no-store hook carves this path out.
    resp.headers["Cache-Control"] = "public, max-age=604800, immutable"
    return resp


# ------------------------------------------------------------------
# POST /api/jobs/guest/matches — Guest Onboarding job matching (NO JWT)
# ------------------------------------------------------------------
# Free direct sources ONLY (Workday CXS + Greenhouse/Lever/Ashby — the same
# skip_apify=True path as the daily pipeline; no Apify, no paid actors),
# scored with jd_match_scorer against the guest's parsed resume, filtered to
# score >= guest_jobs_min_score, HARD-capped at guest_jobs_max (5) — the
# server never returns more rows (no hidden locked rows for CSS to hide).
# Each card carries a server-minted match_id (the only tailorable reference),
# a deterministic `why`, and a public-filings `sponsorship` signal (or null).
#
# Cached per QUOTA SUBJECT per day (session-level fallback): a same-day
# repeat call — even from a fresh guest_id of the same subject — returns the
# cache synchronously (200, {cached:true}) and never re-spends the global
# matches budget. A cache miss dispatches the scrape+score as an ASYNC job
# (invariant #2 — the free-source fan-out blows past API Gateway's 29s),
# returning 202 {job_id}; the FE polls GET /api/resume/guest/job/<id>?guest_id=.
@jobs_bp.route("/guest/matches", methods=["POST"])
def guest_matches():
    from services import guest_challenge_service
    from services import guest_service

    if not guest_service.guest_onboarding_enabled():
        return (
            jsonify({
                "error": "guest_onboarding_disabled",
                "message": "Guest onboarding is currently unavailable. Please sign up to continue.",
            }),
            503,
        )

    client_ip = get_client_ip(request)
    # Distributed (cross-Lambda-container) limit: a per-container dict cannot
    # enforce "10 guest match runs per hour per client" when N containers each
    # keep their own. Fails CLOSED - a match run fans out paid-adjacent scrapes
    # and LLM scoring, so a Mongo blip must not open it up.
    if check_rate_limit(
        f"guest:matches:{client_ip}", max_requests=10, window_seconds=3600
    ):
        return jsonify({"error": "Rate limit exceeded"}), 429

    data = request.get_json(force=True, silent=True) or {}
    guest_id = guest_service.validate_guest_id(data.get("guest_id") or "")
    if not guest_id:
        return jsonify({"error": "A valid guest_id is required"}), 400
    # Optional echoed device token — validated anti-abuse signal, never trusted.
    guest_service.validate_device_token(data.get("device_token") or "")
    # Optional seniority view over the day's scanned pool (instant re-rank,
    # never a re-scan) + viewer timezone for local-day recency.
    seniority = data.get("seniority")
    requested_seniority = (
        guest_service.normalize_seniority(seniority) if seniority is not None else None
    )
    tz_offset_minutes = data.get("tz_offset_minutes")

    session = guest_service.get_session(guest_id)
    if not session:
        return jsonify({"error": "Session not found"}), 404
    if not ((session.get("resume") or {}).get("structured")):
        return jsonify({"error": "Upload your resume first"}), 400

    # Progressive challenge hook — BEFORE any budget spend (default: no-op).
    ch = guest_challenge_service.maybe_challenge(
        "matches", guest_service.hash_ip(client_ip), session.get("quota_subject_id")
    )
    if ch:
        return (
            jsonify({
                "error": "challenge_required",
                "provider": ch.get("provider"),
                # Solve the widget, POST {challenge_id, solution} to
                # /api/resume/guest/challenge/verify, then retry.
                "challenge_id": ch.get("challenge_id"),
                "message": "Please complete a quick verification to continue.",
            }),
            429,
        )

    try:
        cached = guest_service.get_cached_matches(guest_id, requested_seniority)
        if cached is not None:
            # Same-day subject-level cache hit — must NOT consume the global
            # daily scrape budget (re-uploads never re-spend the breaker).
            # A seniority change lands here too: it re-slices the stored
            # pool instantly — no re-scan, no quota.
            return jsonify(cached), 200

        # Cache MISS. The global counter is spent ONCE per (subject, date)
        # (spec 3.7): the atomic matches_reserved guard on the quota subject
        # decides whether this call is today's FIRST run (spend the breaker)
        # or a retry after a failed run (re-dispatch WITHOUT re-spending).
        run = guest_service.reserve_matches_run(guest_id)
        if run.get("first"):
            if not guest_service.try_reserve_global_matches():
                guest_service.release_matches_run(guest_id)
                return (
                    jsonify({
                        "error": "capacity",
                        "global": True,
                        "message": (
                            "We've hit today's platform-wide limit for free guest "
                            "job matching. Sign up (free) to continue — or try "
                            "again tomorrow."
                        ),
                    }),
                    429,
                )
        else:
            prior_job_id = run.get("job_id")
            if prior_job_id:
                prior = guest_service.get_guest_job(prior_job_id, guest_id)
                if prior and prior.get("status") == "processing":
                    # Idempotent retry while today's run is still in flight.
                    return jsonify({"job_id": prior_job_id, "existing": True}), 202
                # Failed (or evaporated) run: re-dispatch, no second spend.

        from services.resume_service import ResumeService

        payload = {"guest_id": guest_id}
        if requested_seniority:
            payload["seniority"] = requested_seniority
        try:
            payload["tz_offset_minutes"] = int(tz_offset_minutes)
        except (TypeError, ValueError):
            pass

        # --- fast path: serve synchronously from the canonical inventory ----
        # The legacy path returns 202 + a job_id because matching used to crawl
        # ~1,100 third-party hosts and take up to 210 seconds. Reading the
        # inventory is one indexed query plus deterministic scoring, so there is
        # nothing to wait for: the guest gets results in the response instead of
        # a spinner and 2-second polling.
        #
        # `inventory_only=True` guarantees this cannot start a fan-out inside
        # the 60s jobs Lambda: if the crawler has not warmed this role family it
        # returns None and we fall through to the async job below, unchanged.
        if guest_service.inventory_mode_enabled():
            try:
                result = guest_service.compute_guest_matches(
                    guest_id,
                    None,
                    requested_seniority,
                    payload.get("tz_offset_minutes", 0),
                    inventory_only=True,
                )
            except Exception as sync_err:
                logger.warning("Guest matches inventory fast path failed: %s", sync_err)
                result = None
            if result is not None:
                # The run is genuinely spent: compute_guest_matches persisted
                # today's snapshot, so a repeat call hits get_cached_matches.
                return jsonify(result), 200

        try:
            job_id = guest_service.create_guest_job("guest_matches", payload, guest_id)
            ResumeService.invoke_async(job_id, "guest_matches", payload)
        except Exception:
            if run.get("first"):
                # The job never dispatched — free the reserved global slot
                # and today's subject reservation (pre-dispatch rollback).
                guest_service.release_global_matches()
                guest_service.release_matches_run(guest_id)
            raise
        # Stamp AFTER a successful dispatch so a dispatch failure never pins
        # a dead job onto today's reservation.
        guest_service.attach_matches_job(guest_id, job_id)
        return jsonify({"job_id": job_id}), 202
    except Exception as e:
        logger.error(f"Guest matches error: {e}")
        return jsonify({"error": "Failed to compute job matches"}), 500
