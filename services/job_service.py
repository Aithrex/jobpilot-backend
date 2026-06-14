"""
Job Search Service - JSearch API integration, skill matching, resume parsing, AI analysis

Provides:
- Job search via JSearch (RapidAPI) with MongoDB caching
- Skill-based job matching and scoring
- Resume parsing via LLM
- Job analysis (summary, missing skills, cover letter)
- Saved jobs CRUD
- Saved job search filter preferences
- H1B sponsor detection
"""
import hashlib
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urlencode

import requests

from utils.config import _get_config_value
from utils.db_connect import DBConnect

logger = logging.getLogger(__name__)

# ---- Portfolio skills (same source as chat_service.PORTFOLIO_CONTEXT) ----
PORTFOLIO_SKILLS = [
    "Python", "Java", "Bash", "JavaScript", "SQL", "HTML", "CSS",
    "AWS", "EC2", "S3", "VPC", "ECS", "Lambda", "CloudWatch", "CloudTrail",
    "CodePipeline", "CloudFormation", "Terraform", "Docker", "Nginx", "Git",
    "GitHub", "Flask", "Postgres", "PostgreSQL", "Linux", "Unix",
    "React", "TypeScript", "CI/CD", "MongoDB", "REST", "API Gateway",
    "IAM", "RDS", "Fargate", "CodeBuild", "ECR", "ALB",
    "Kubernetes", "Jenkins", "Ansible", "Prometheus", "Grafana",
]

# ---- Known H1B sponsors (top tech + consulting + finance) ----
H1B_SPONSORS = {
    "google", "meta", "amazon", "apple", "microsoft", "netflix", "uber",
    "lyft", "airbnb", "stripe", "salesforce", "oracle", "ibm", "intel",
    "cisco", "vmware", "adobe", "nvidia", "qualcomm", "paypal",
    "twitter", "snap", "pinterest", "linkedin", "spotify", "shopify",
    "databricks", "snowflake", "palantir", "splunk", "datadog",
    "cloudflare", "twilio", "okta", "zscaler", "palo alto networks",
    "crowdstrike", "fortinet", "elastic", "mongodb", "confluent",
    "hashicorp", "github", "atlassian", "servicenow", "workday",
    "zoom", "docusign", "hubspot", "zendesk", "freshworks",
    "deloitte", "accenture", "pwc", "ey", "ernst & young", "kpmg",
    "mckinsey", "bain", "boston consulting", "bcg", "capgemini",
    "cognizant", "infosys", "tcs", "wipro", "hcl", "tech mahindra",
    "jpmorgan", "jp morgan", "goldman sachs", "morgan stanley",
    "bank of america", "citibank", "citi", "wells fargo", "barclays",
    "capital one", "american express", "visa", "mastercard",
    "blackrock", "citadel", "two sigma", "jane street", "de shaw",
    "bloomberg", "fidelity", "charles schwab", "robinhood",
    "walmart", "target", "costco", "home depot",
    "johnson & johnson", "pfizer", "abbvie", "merck", "bristol-myers",
    "lockheed martin", "raytheon", "boeing", "northrop grumman",
    "general electric", "ge", "siemens", "honeywell", "3m",
    "tesla", "rivian", "spacex", "relativity space",
    "epic systems", "cerner", "veeva", "medidata",
    "samsung", "sony", "lg", "toshiba", "hitachi",
    "sap", "dell", "hp", "hewlett packard", "lenovo",
    "red hat", "canonical", "suse",
    "figma", "notion", "vercel", "supabase", "retool",
    "anduril", "shield ai", "applied intuition",
    "openai", "anthropic", "cohere", "stability ai",
    "waymo", "cruise", "aurora", "nuro", "argo ai",
    "doordash", "instacart", "grubhub",
    "reddit", "discord", "roblox", "epic games", "unity",
    "plaid", "brex", "chime", "sofi", "affirm", "klarna",
    "nike", "tiktok", "bytedance", "lucid", "lucid motors",
    "thermo fisher", "thermofisher", "state farm", "zelle",
    "early warning services", "jobright",
}

# ---- New-grad keywords ----
NEW_GRAD_KEYWORDS = [
    "new grad", "new graduate", "entry level", "entry-level",
    "junior", "associate", "early career", "recent graduate",
    "0-2 years", "0-1 years", "graduate program",
]

CONTRACT_KEYWORDS = [
    "contract", "contractor", "contract-to-hire", "contract to hire",
    "c2c", "corp-to-corp", "w2 contract", "consultant",
]

TOP_COMPANY_CAREER_URLS = [
    "https://www.amazon.jobs",
    "https://careers.google.com",
    "https://www.metacareers.com",
    "https://www.apple.com/careers",
    "https://jobs.netflix.com",
    "https://careers.microsoft.com",
    "https://jobs.nike.com",
    "https://careers.walmart.com",
    "https://careers.tiktok.com",
    "https://jobs.lucidmotors.com",
    "https://www.qualcomm.com/company/careers",
    "https://jobs.thermofisher.com",
    "https://www.paypal.com/us/cshelp/article/how-do-i-apply-for-a-job-at-paypal-help528",
    "https://jobs.statefarm.com",
    "https://www.earlywarning.com/careers",
    "https://www.deloitte.com/us/en/careers.html",
    "https://www.tcs.com/careers",
    "https://www.infosys.com/careers",
]

COMPANY_CAREER_BATCH_SIZE = 3
COMPANY_CAREER_MAX_BATCHES = 4
COMPANY_CAREER_MAX_JOBS_PER_COMPANY = 2
MAX_RETURNED_JOBS = 80
JOB_DESCRIPTION_MAX_CHARS = 1600

# Max Apify actor runs per (source, query) batch. Keeps parallel fan-out bounded
# so we don't spawn hundreds of actor runs for a 6-query × 5-source batch.
APIFY_MAX_WORKERS = 12
# Max queries sent to each live Apify actor per batch. Sources with heavy
# per-run cost (Google, company) cap lower via _max_queries_for_source.
MAX_QUERIES_PER_APIFY_SOURCE = 3
# Max JSearch (RapidAPI) queries per batch — RapidAPI plans meter per call.
MAX_JSEARCH_QUERIES_PER_BATCH = 4
# Reuse identical Apify actor responses across users/filter post-processing.
# This prevents re-running paid/credit-consuming actors for repeated searches.
APIFY_ACTOR_CACHE_TTL_SECONDS = 3600
APIFY_ACTOR_CACHE_VERSION = "v6-jsearch-linkedin-workday"
# Bump this when the result schema/source behavior changes so old cached
# entries are bypassed.
JOBS_CACHE_VERSION = "v8-live-refresh"

# Tokens that should never end up in stored error strings or responses.
_SECRET_QUERY_PARAMS = ("token", "apify_token", "api_key", "X-RapidAPI-Key")
_SECRET_PATTERN = re.compile(
    r"(" + "|".join(_SECRET_QUERY_PARAMS) + r")=([^&\s]+)",
    re.IGNORECASE,
)


def _redact_secrets(text: str) -> str:
    """Strip token=… / api_key=… values from any string before logging/storing."""
    if not text:
        return text
    return _SECRET_PATTERN.sub(r"\1=***", str(text))


# Tokens commonly paired with resume-search queries that aren't useful as
# free-text keywords for JSearch / Google Jobs (they hurt relevance there).
_JSEARCH_STOPWORDS = {
    "h1b", "h-1b", "sponsor", "sponsorship", "visa",
    "new", "grad", "graduate", "entry", "level",
    "contract", "c2c", "corp-to-corp", "w2",
}


def _simplify_jsearch_query(query: str) -> str:
    """Drop filter-style tokens that confuse JSearch's keyword scoring."""
    parts = [p for p in re.split(r"\s+", query.lower()) if p]
    kept = [p for p in parts if p not in _JSEARCH_STOPWORDS]
    simplified = " ".join(kept).strip()
    return simplified or query.strip()


def _make_cache_key(params: dict) -> str:
    """Create a deterministic MD5 hash from query parameters."""
    normalized = json.dumps({**params, "_v": JOBS_CACHE_VERSION}, sort_keys=True)
    return hashlib.md5(normalized.encode()).hexdigest()


def _make_apify_cache_key(actor_id: str, payload: dict, max_items: int) -> str:
    """Create a stable cache key for one concrete Apify actor invocation."""
    normalized = json.dumps(
        {
            "_v": APIFY_ACTOR_CACHE_VERSION,
            "actor_id": actor_id,
            "payload": payload,
            "max_items": max_items,
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.md5(normalized.encode()).hexdigest()


class JobService:
    """Singleton service for job search, matching, analysis, and saved-jobs."""

    JSEARCH_BASE = "https://jsearch.p.rapidapi.com"
    APIFY_BASE = "https://api.apify.com/v2/acts"
    # Apify actors are optional and must be configured explicitly.
    # Pricing changes often; use only actor ids you have verified/approved.
    # Configure via APIFY_LINKEDIN_ACTOR / APIFY_WORKDAY_ACTOR /
    # APIFY_INDEED_ACTOR / APIFY_GOOGLE_ACTOR / APIFY_COMPANY_ACTOR /
    # APIFY_JOBRIGHT_ACTOR.
    APIFY_ACTORS = {
        "linkedin": "curious_coder/linkedin-jobs-scraper",
        "workday": "fantastic-jobs/workday-jobs-api",
        "indeed": "",
        "google": "",
        "company": "",
        "jobright": "",
    }
    CACHE_TTL_SECONDS = 6 * 3600  # 6 hours (Mongo TTL index — outer ceiling)

    # Logical TTL applied at read time, bucketed by date_posted. Fresh queries
    # ("today") expire after 30 minutes so newly-posted jobs surface quickly;
    # broad windows ("month") can stay cached longer because they don't churn.
    CACHE_TTL_BY_DATE = {
        "today": 30 * 60,
        "3days": 60 * 60,
        "week": 3 * 3600,
        "month": 6 * 3600,
        "all": 6 * 3600,
    }

    def __init__(self):
        self.db = DBConnect().get_db()
        self.jobs_cache = self.db.jobs_cache
        self.apify_actor_cache = self.db.apify_actor_cache
        self.user_resumes = self.db.user_resumes
        self.saved_jobs = self.db.saved_jobs
        self.job_filter_preferences = self.db.job_filter_preferences
        self.user_apify_keys = self.db.user_apify_keys
        self._ensure_indexes()

    def _ensure_indexes(self):
        try:
            self.jobs_cache.create_index("query_hash", unique=True)
            self.jobs_cache.create_index(
                "cached_at", expireAfterSeconds=self.CACHE_TTL_SECONDS
            )
            self.apify_actor_cache.create_index("cache_key", unique=True)
            self.apify_actor_cache.create_index(
                "cached_at",
                expireAfterSeconds=APIFY_ACTOR_CACHE_TTL_SECONDS,
                name="apify_actor_cache_cached_at_ttl",
            )
            self.saved_jobs.create_index("job_id", unique=True)
            self.job_filter_preferences.create_index("user_email", unique=True)
            self.user_apify_keys.create_index("user_email", unique=True)
        except Exception as e:
            logger.warning(f"Index creation warning: {e}")

    # ------------------------------------------------------------------
    # Per-user Apify key (BYO-key — falls back to the shared SSM/env key)
    # ------------------------------------------------------------------

    def get_user_apify_key(self, user_email: str) -> Optional[str]:
        """Return the raw stored key for use by the pipeline; never sent to UI."""
        if not user_email:
            return None
        try:
            doc = self.user_apify_keys.find_one({"user_email": user_email})
            if doc and isinstance(doc.get("apify_key"), str) and doc["apify_key"].strip():
                return doc["apify_key"].strip()
        except Exception as e:
            logger.warning(f"get_user_apify_key failed for {user_email}: {e}")
        return None

    def get_user_apify_key_status(self, user_email: str) -> Dict[str, Any]:
        """Public-safe view: has_key + masked tail + updated_at."""
        if not user_email:
            return {"has_key": False}
        try:
            doc = self.user_apify_keys.find_one({"user_email": user_email})
        except Exception as e:
            logger.warning(f"get_user_apify_key_status failed for {user_email}: {e}")
            return {"has_key": False}
        if not doc or not doc.get("apify_key"):
            return {"has_key": False}
        key = str(doc["apify_key"])
        masked = ("•" * 8 + key[-4:]) if len(key) >= 4 else "••••"
        updated = doc.get("updated_at")
        return {
            "has_key": True,
            "masked": masked,
            "updated_at": updated.isoformat() if hasattr(updated, "isoformat") else updated,
        }

    def set_user_apify_key(self, user_email: str, apify_key: str) -> Dict[str, Any]:
        if not user_email:
            return {"ok": False, "error": "Authentication required"}
        key = (apify_key or "").strip()
        if not key:
            return {"ok": False, "error": "Apify key cannot be empty"}
        if not re.match(r"^apify_api_[A-Za-z0-9]{20,}$", key):
            return {"ok": False, "error": "Invalid format. Expected apify_api_... (40+ chars)."}
        now = datetime.now(timezone.utc)
        try:
            self.user_apify_keys.update_one(
                {"user_email": user_email},
                {"$set": {
                    "user_email": user_email,
                    "apify_key": key,
                    "updated_at": now,
                }, "$setOnInsert": {"created_at": now}},
                upsert=True,
            )
        except Exception as e:
            logger.error(f"set_user_apify_key failed for {user_email}: {e}")
            return {"ok": False, "error": "Failed to save key"}
        return {"ok": True, **self.get_user_apify_key_status(user_email)}

    def delete_user_apify_key(self, user_email: str) -> Dict[str, Any]:
        if not user_email:
            return {"ok": False, "error": "Authentication required"}
        try:
            self.user_apify_keys.delete_one({"user_email": user_email})
        except Exception as e:
            logger.error(f"delete_user_apify_key failed for {user_email}: {e}")
            return {"ok": False, "error": "Failed to remove key"}
        return {"ok": True, "has_key": False}

    # ------------------------------------------------------------------
    # JSearch API
    # ------------------------------------------------------------------

    def search_jobs(
        self,
        query: str,
        page: int = 1,
        location: str = "",
        date_posted: str = "month",
        remote_only: bool = False,
        employment_type: str = "",
        h1b_only: bool = False,
        visa_or_contract: bool = False,
        experience_level: str = "",
        source: str = "all",
        include_company_careers: bool = True,
        use_resume_recommendations: bool = True,
        force_refresh: bool = False,
        user_email: str = "",
        partial_cb: Optional[Any] = None,
    ) -> Dict[str, Any]:
        # Cache key is built from FETCH-affecting params only (what changes
        # which jobs come back from each source). date_posted / h1b_only /
        # visa_or_contract / experience_level are POST-filters — applied to
        # the cached raw set so switching filters is instant.
        wide_params = {
            "query": query,
            "location": location,
            "remote_only": remote_only,
            "employment_type": employment_type,
            "source": source,
            "include_company_careers": include_company_careers,
            "use_resume_recommendations": use_resume_recommendations,
            "user_email": user_email if use_resume_recommendations else "",
        }
        cache_key = _make_cache_key(wide_params)

        result: Optional[Dict[str, Any]] = None
        if not force_refresh and self._cached_result_is_fresh(cache_key, date_posted):
            cached = self.jobs_cache.find_one({"query_hash": cache_key})
            if cached:
                cached.pop("_id", None)
                result = dict(cached["result"])
                result["cache_hits"] = max(1, result.get("cache_hits", 0) or 0)
                result["cache_bypassed"] = False

        if result is None:
            if not self._get_apify_token() and not self._get_jsearch_key():
                raise RuntimeError("APIFY_API_KEY/APIFY_TOKEN or JSEARCH_API_KEY is not configured")
            # Fetch the WIDEST sensible window from every source so the cached
            # set covers every filter combination the user might toggle.
            result = self._search_apify_jobs(
                queries=[query],
                location=location,
                date_posted="month",
                remote_only=remote_only,
                employment_type=employment_type,
                h1b_only=False,
                visa_or_contract=False,
                experience_level="",
                source=source,
                include_company_careers=include_company_careers,
                user_email=user_email if use_resume_recommendations else "",
                force_refresh=force_refresh,
                partial_cb=partial_cb,
            )
            result["cache_bypassed"] = bool(force_refresh)
            self._write_cache(cache_key, result)

        # Apply the user's requested filters in-memory on the cached raw set.
        result = self._apply_post_filters(
            result,
            date_posted=date_posted,
            h1b_only=h1b_only,
            visa_or_contract=visa_or_contract,
            experience_level=experience_level,
        )
        result["page"] = page
        result.setdefault("total_pages", 1)
        return result

    def _cached_result_is_fresh(self, cache_key: str, date_posted: str) -> bool:
        """Logical TTL check on top of Mongo's hard TTL index."""
        ttl = self.CACHE_TTL_BY_DATE.get(date_posted, self.CACHE_TTL_BY_DATE["week"])
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=ttl)
        try:
            doc = self.jobs_cache.find_one(
                {"query_hash": cache_key, "cached_at": {"$gte": cutoff}},
                {"_id": 1},
            )
            return doc is not None
        except Exception:
            return False

    @staticmethod
    def _get_jsearch_key() -> str:
        return _get_config_value('JSEARCH_API_KEY', '')

    def _search_jsearch_raw(
        self,
        query: str,
        location: str,
        date_posted: str,
        remote_only: bool,
        employment_type: str,
    ) -> List[Dict[str, Any]]:
        """Run one JSearch query and return normalized jobs (no scoring/caching).

        JSearch's relevance drops sharply on filter-style tokens (e.g. "h1b
        sponsor", "new grad"). We strip those before the request and apply
        the intent as post-filters in the main pipeline. We also widen
        date_posted from "today" → "3days" because JSearch's daily index
        lags ~1 day for many sources.
        """
        api_key = self._get_jsearch_key()
        if not api_key:
            logger.info("JSearch skipped: JSEARCH_API_KEY not configured")
            return []

        clean_query = _simplify_jsearch_query(query)
        effective_date = {
            "today": "3days",
        }.get(date_posted, date_posted if date_posted in ("3days", "week", "month", "all") else "week")

        headers = {
            "X-RapidAPI-Key": api_key,
            "X-RapidAPI-Host": "jsearch.p.rapidapi.com",
        }
        api_params = {
            "query": f"{clean_query} in {location}" if location else clean_query,
            "page": "1",
            "num_pages": "1",
            "country": "us",
            "language": "en",
            "date_posted": effective_date,
        }
        if remote_only:
            api_params["remote_jobs_only"] = "true"
            api_params["work_from_home"] = "true"
        if employment_type:
            api_params["employment_types"] = employment_type

        resp = requests.get(
            f"{self.JSEARCH_BASE}/search",
            headers=headers,
            params=api_params,
            timeout=20,
        )
        if not resp.ok:
            raise RuntimeError(f"JSearch returned {resp.status_code} {resp.reason}")
        data = resp.json()
        items = data.get("data") or []
        logger.info(
            "JSearch query=%r date=%s returned %d items",
            clean_query, effective_date, len(items),
        )
        return [self._normalize_job(j) for j in items]

    # ------------------------------------------------------------------
    # Batch Search (multiple queries in parallel)
    # ------------------------------------------------------------------

    def batch_search_jobs(
        self,
        queries: List[str],
        location: str = "",
        date_posted: str = "today",
        remote_only: bool = False,
        employment_type: str = "",
        h1b_only: bool = False,
        visa_or_contract: bool = False,
        experience_level: str = "",
        source: str = "all",
        include_company_careers: bool = True,
        use_resume_recommendations: bool = True,
        force_refresh: bool = False,
        user_email: str = "",
        partial_cb: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Run multiple search queries in parallel, deduplicate, and re-score.

        Same two-tier caching shape as search_jobs: the wide fetch is cached by
        the fetch-affecting params (queries, location, source, employment_type,
        remote_only, include_company_careers, user_email), and the user's
        date_posted / h1b_only / visa_or_contract / experience_level toggles
        apply as fast in-memory post-filters on the cached set.
        """
        wide_params = {
            "queries": queries,
            "location": location,
            "remote_only": remote_only,
            "employment_type": employment_type,
            "source": source,
            "include_company_careers": include_company_careers,
            "user_email": user_email if use_resume_recommendations else "",
        }
        cache_key = _make_cache_key(wide_params)
        result: Optional[Dict[str, Any]] = None
        if not force_refresh and self._cached_result_is_fresh(cache_key, date_posted):
            cached = self.jobs_cache.find_one({"query_hash": cache_key})
            if cached:
                cached.pop("_id", None)
                result = dict(cached["result"])
                result["cache_hits"] = result.get("queries_executed", len(queries))
                result["cache_bypassed"] = False

        if result is None:
            if not self._get_apify_token() and not self._get_jsearch_key():
                raise RuntimeError("APIFY_API_KEY/APIFY_TOKEN or JSEARCH_API_KEY is not configured")
            result = self._search_apify_jobs(
                queries=queries[:6],
                location=location,
                date_posted="month",
                remote_only=remote_only,
                employment_type=employment_type,
                h1b_only=False,
                visa_or_contract=False,
                experience_level="",
                source=source,
                include_company_careers=include_company_careers,
                user_email=user_email if use_resume_recommendations else "",
                force_refresh=force_refresh,
                partial_cb=partial_cb,
            )
            result["queries_executed"] = min(len(queries), 6)
            result["cache_hits"] = 0
            result["cache_bypassed"] = bool(force_refresh)
            self._write_cache(cache_key, result)

        return self._apply_post_filters(
            result,
            date_posted=date_posted,
            h1b_only=h1b_only,
            visa_or_contract=visa_or_contract,
            experience_level=experience_level,
        )

    def _apply_post_filters(
        self,
        result: Dict[str, Any],
        date_posted: str,
        h1b_only: bool,
        visa_or_contract: bool,
        experience_level: str,
    ) -> Dict[str, Any]:
        """Re-filter a cached wide result against the user's current filter set.

        Cheap (regex + dict access only) so toggling filters in the UI doesn't
        kick off a fresh actor run. Preserves the original 'jobs' field on the
        result so multiple filter calls against the same cache are idempotent.
        """
        out = dict(result)
        raw_jobs = out.get("_raw_jobs")
        if raw_jobs is None:
            raw_jobs = out.get("jobs") or []
            # Stash a copy so subsequent post-filter calls work from the wide
            # set rather than the most-recent filtered view.
            out["_raw_jobs"] = list(raw_jobs)
        filtered: List[Dict[str, Any]] = []
        rejected_reasons = dict(out.get("filtered_reasons") or {})
        for job in raw_jobs:
            reason = self._job_filter_rejection_reason(
                job=job,
                query_terms=[],
                location="",
                date_posted=date_posted,
                h1b_only=h1b_only,
                visa_or_contract=visa_or_contract,
                experience_level=experience_level,
            )
            if reason:
                rejected_reasons[reason] = rejected_reasons.get(reason, 0) + 1
                continue
            filtered.append(job)
        # Maintain the original match_score sort order from the wide cache.
        out["jobs"] = self._prepare_jobs_for_response(filtered)
        out["total"] = len(out["jobs"])
        out["filtered_reasons"] = rejected_reasons
        out["applied_filters"] = {
            "date_posted": date_posted,
            "h1b_only": h1b_only,
            "visa_or_contract": visa_or_contract,
            "experience_level": experience_level,
        }
        return out

    def _write_cache(self, cache_key: str, result: Dict[str, Any]) -> None:
        try:
            self.jobs_cache.update_one(
                {"query_hash": cache_key},
                {
                    "$set": {
                        "query_hash": cache_key,
                        "result": result,
                        "cached_at": datetime.now(timezone.utc),
                    }
                },
                upsert=True,
            )
        except Exception as e:
            logger.warning(f"Cache write failed: {e}")

    def _read_apify_actor_cache(
        self,
        actor_id: str,
        payload: Dict[str, Any],
        max_items: int,
    ) -> Optional[List[Dict[str, Any]]]:
        cache_key = _make_apify_cache_key(actor_id, payload, max_items)
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=APIFY_ACTOR_CACHE_TTL_SECONDS)
        try:
            cached = self.apify_actor_cache.find_one(
                {"cache_key": cache_key, "cached_at": {"$gte": cutoff}},
                {"_id": 0, "items": 1},
            )
            if cached and isinstance(cached.get("items"), list):
                logger.info(
                    "Apify actor cache hit for %s (%d items)",
                    actor_id,
                    len(cached["items"]),
                )
                return cached["items"]
        except Exception as e:
            logger.warning("Apify actor cache read failed for %s: %s", actor_id, e)
        return None

    def _write_apify_actor_cache(
        self,
        actor_id: str,
        payload: Dict[str, Any],
        max_items: int,
        items: List[Dict[str, Any]],
    ) -> None:
        cache_key = _make_apify_cache_key(actor_id, payload, max_items)
        try:
            self.apify_actor_cache.update_one(
                {"cache_key": cache_key},
                {
                    "$set": {
                        "cache_key": cache_key,
                        "actor_id": actor_id,
                        "items": items,
                        "cached_at": datetime.now(timezone.utc),
                    }
                },
                upsert=True,
            )
        except Exception as e:
            logger.warning("Apify actor cache write failed for %s: %s", actor_id, e)

    @staticmethod
    def _get_apify_token() -> str:
        return _get_config_value("APIFY_API_KEY", "") or _get_config_value("APIFY_TOKEN", "")

    def _run_apify_actor(
        self,
        actor_id: str,
        payload: Dict[str, Any],
        max_items: int = 50,
        timeout_seconds: int = 120,
        memory_mb: Optional[int] = None,
        bypass_cache: bool = False,
    ) -> List[Dict[str, Any]]:
        if not bypass_cache:
            cached = self._read_apify_actor_cache(actor_id, payload, max_items)
            if cached is not None:
                return cached

        token = self._get_apify_token()
        actor_path = quote(actor_id.replace("/", "~"), safe="")
        url = f"{self.APIFY_BASE}/{actor_path}/run-sync-get-dataset-items"
        if memory_mb is None:
            memory_mb = self._get_apify_memory_mb()
        params = {
            "token": token,
            "format": "json",
            "clean": "true",
            "timeout": str(timeout_seconds),
            "maxItems": str(max_items),
            "memory": str(memory_mb),
        }
        resp = requests.post(url, params=params, json=payload, timeout=timeout_seconds + 30)
        if not resp.ok:
            # Build an error that doesn't echo the token in the URL.
            raise RuntimeError(
                f"Apify actor {actor_id} returned {resp.status_code} {resp.reason}"
            )
        data = resp.json()
        if isinstance(data, dict) and data.get("error"):
            # Apify returns {"error": {...}} on timeouts/abort inside run-sync.
            err = data.get("error", {})
            msg = err.get("message") or err.get("type") or "actor run failed"
            raise RuntimeError(f"Apify actor {actor_id}: {msg}")
        items = data if isinstance(data, list) else []
        self._write_apify_actor_cache(actor_id, payload, max_items, items)
        return items

    @staticmethod
    def _get_apify_memory_mb() -> int:
        raw = _get_config_value("APIFY_ACTOR_MEMORY_MB", "1024")
        try:
            return max(512, min(int(raw), 8192))
        except (TypeError, ValueError):
            return 1024

    @staticmethod
    def _get_apify_memory_mb_for_source(source: str) -> int:
        if source in ("company", "workday"):
            raw = _get_config_value("APIFY_COMPANY_ACTOR_MEMORY_MB", "1024")
            try:
                return max(1024, min(int(raw), 8192))
            except (TypeError, ValueError):
                return 1024
        return JobService._get_apify_memory_mb()

    @staticmethod
    def _chunks(items: List[str], size: int) -> List[List[str]]:
        return [items[i:i + size] for i in range(0, len(items), size)]

    def _search_apify_jobs(
        self,
        queries: List[str],
        location: str,
        date_posted: str,
        remote_only: bool,
        employment_type: str,
        h1b_only: bool,
        visa_or_contract: bool,
        experience_level: str,
        source: str,
        include_company_careers: bool,
        user_email: str,
        force_refresh: bool = False,
        partial_cb: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Parallel multi-source job search.

        Runs JSearch (RapidAPI) and any configured Apify actors concurrently,
        then deduplicates, filters, and re-scores. Apify actor ids are
        configured-only so subscription/rental errors from Store defaults do
        not block normal job results.

        If ``partial_cb`` is provided, it is invoked with a snapshot of the
        partial result dict after each source completes, so the caller can
        stream progress to the client.
        """
        supported = {
            "all", "linkedin", "workday", "indeed", "google", "company",
            "jobright", "jsearch", "workday_direct", "ats_direct",
        }
        source = source if source in supported else "all"

        apify_token = self._get_apify_token()
        jsearch_key = self._get_jsearch_key()

        # Pick which sources to fan out to. JSearch is first so the UI can
        # render useful results while optional Apify actors are still running.
        # An Apify source only joins if its actor ID is explicitly configured.
        # workday_direct + ats_direct are FREE (no Apify, no RapidAPI key) and
        # always run when source == "all" — they fill the gap when the Apify
        # Workday actor's index doesn't include a tenant (e.g. Yahoo).
        selected: List[str] = []
        skipped_sources: Dict[str, str] = {}
        if source == "all":
            if jsearch_key:
                selected.append("jsearch")
            else:
                skipped_sources["jsearch"] = "JSEARCH_API_KEY not configured"
            # Free direct sources — always on for "all" searches.
            if include_company_careers:
                selected.append("workday_direct")
                selected.append("ats_direct")
            if apify_token:
                for src in ("linkedin", "indeed", "google", "jobright"):
                    if self._actor_id(src):
                        selected.append(src)
                if include_company_careers and self._actor_id("workday"):
                    selected.append("workday")
                elif include_company_careers:
                    skipped_sources["workday"] = "APIFY_WORKDAY_ACTOR not configured"
                if include_company_careers and self._actor_id("company"):
                    selected.append("company")
                elif include_company_careers:
                    skipped_sources["company"] = "APIFY_COMPANY_ACTOR not configured"
                else:
                    skipped_sources["workday"] = "Workday/company sites disabled"
                    skipped_sources["company"] = "Workday/company sites disabled"
            else:
                for src in ("linkedin", "workday", "company"):
                    skipped_sources[src] = "APIFY_API_KEY/APIFY_TOKEN not configured"
        elif source == "workday_direct":
            if include_company_careers:
                selected = ["workday_direct"]
            else:
                skipped_sources["workday_direct"] = "Workday/company sites disabled"
        elif source == "ats_direct":
            if include_company_careers:
                selected = ["ats_direct"]
            else:
                skipped_sources["ats_direct"] = "Workday/company sites disabled"
        elif source == "jsearch":
            if jsearch_key:
                selected = ["jsearch"]
            else:
                skipped_sources["jsearch"] = "JSEARCH_API_KEY not configured"
        else:
            # A specific Apify source was requested.
            if apify_token and self._actor_id(source):
                selected = [source]
            elif jsearch_key:
                # Keep the dashboard useful when a paid/rented Apify actor is
                # not configured. The source badge will show JSearch.
                selected = ["jsearch"]
                skipped_sources[source] = (
                    "APIFY_API_KEY/APIFY_TOKEN not configured"
                    if not apify_token
                    else f"APIFY_{source.upper()}_ACTOR not configured"
                )
            else:
                skipped_sources[source] = (
                    "APIFY_API_KEY/APIFY_TOKEN not configured"
                    if not apify_token
                    else f"APIFY_{source.upper()}_ACTOR not configured"
                )

        if not selected:
            return {
                "jobs": [], "total": 0, "page": 1, "total_pages": 1,
                "queries_executed": len(queries), "cache_hits": 0,
                "errors": ["Set JSEARCH_API_KEY or configure an APIFY_*_ACTOR source"],
                "sources": {},
                "selected_sources": [],
                "skipped_sources": skipped_sources,
            }

        all_jobs: List[Dict[str, Any]] = []
        fallback_jobs: List[Dict[str, Any]] = []
        errors: List[str] = []
        seen_ids: set = set()
        fallback_seen_ids: set = set()
        source_counts: Dict[str, int] = {}
        raw_source_counts: Dict[str, int] = {}
        fallback_source_counts: Dict[str, int] = {}
        filtered_reasons: Dict[str, int] = {}

        def _collect_apify(src: str, q: str, company_urls: Optional[List[str]] = None) -> List[Dict[str, Any]]:
            actor = self._actor_id(src)
            if not actor:
                raise RuntimeError(f"APIFY_{src.upper()}_ACTOR is not configured")
            payload = self._build_apify_input(
                source=src,
                query=q,
                location=location,
                date_posted=date_posted,
                remote_only=remote_only,
                employment_type=employment_type,
                experience_level=experience_level,
                company_urls=company_urls,
            )
            try:
                raw_items = self._run_apify_actor(
                    actor,
                    payload,
                    max_items=35 if src != "company" else max(10, len(company_urls or []) * COMPANY_CAREER_MAX_JOBS_PER_COMPANY),
                    timeout_seconds=180 if src != "company" else 120,
                    memory_mb=self._get_apify_memory_mb_for_source(src),
                    bypass_cache=force_refresh,
                )
            except Exception:
                if src != "company" or not company_urls or len(company_urls) == 1:
                    raise
                raw_items = []
                for url in company_urls:
                    single_payload = self._build_apify_input(
                        source=src,
                        query=q,
                        location=location,
                        date_posted=date_posted,
                        remote_only=remote_only,
                        employment_type=employment_type,
                        experience_level=experience_level,
                        company_urls=[url],
                    )
                    try:
                        raw_items.extend(
                            self._run_apify_actor(
                                actor,
                                single_payload,
                                max_items=COMPANY_CAREER_MAX_JOBS_PER_COMPANY,
                                timeout_seconds=60,
                                memory_mb=self._get_apify_memory_mb_for_source(src),
                                bypass_cache=force_refresh,
                            )
                        )
                    except Exception as e:
                        logger.warning(f"Company career scrape failed for {url}: {e}")
            return [self._normalize_apify_job(item, src) for item in raw_items]

        def _collect_jsearch(q: str) -> List[Dict[str, Any]]:
            """Run one JSearch query and return normalized jobs (no scoring yet)."""
            result = self._search_jsearch_raw(
                query=q,
                location=location,
                date_posted=date_posted,
                remote_only=remote_only,
                employment_type=employment_type,
            )
            return result

        def _collect_workday_direct(q: str) -> List[Dict[str, Any]]:
            """Hit each curated Workday tenant's public CXS endpoint in parallel."""
            from services.workday_cxs_fetcher import fetch_workday_direct
            return fetch_workday_direct(
                search_text=_simplify_jsearch_query(q),
                location=location,
            )

        def _collect_ats_direct(q: str) -> List[Dict[str, Any]]:
            """Public Greenhouse/Lever/Ashby board JSONs — free, no auth."""
            from services.ats_direct_fetcher import fetch_ats_direct
            return fetch_ats_direct(query=q)

        queries = [q for q in (queries or []) if q] or [""]

        tasks = []
        task_meta: Dict[Any, str] = {}
        with ThreadPoolExecutor(max_workers=APIFY_MAX_WORKERS) as pool:
            for src in selected:
                max_q = self._max_queries_for_source(src, total_queries=len(queries))
                src_queries = queries[:max_q] if src != "company" else [queries[0]]
                if src == "company":
                    url_batches = self._chunks(
                        TOP_COMPANY_CAREER_URLS,
                        COMPANY_CAREER_BATCH_SIZE,
                    )[:COMPANY_CAREER_MAX_BATCHES]
                    for urls in url_batches:
                        fut = pool.submit(_collect_apify, src, src_queries[0], urls)
                        tasks.append(fut)
                        task_meta[fut] = src
                    continue
                if src == "jsearch":
                    for q in src_queries:
                        fut = pool.submit(_collect_jsearch, q)
                        tasks.append(fut)
                        task_meta[fut] = src
                    continue
                if src == "workday_direct":
                    # Tenant fan-out is internal to the fetcher; submit one task
                    # per query, capped by _max_queries_for_source to keep noise
                    # down on big batch searches.
                    for q in src_queries:
                        fut = pool.submit(_collect_workday_direct, q)
                        tasks.append(fut)
                        task_meta[fut] = src
                    continue
                if src == "ats_direct":
                    # ATS boards don't take a query — one call returns the
                    # union of every configured company, then we keyword-filter
                    # in the fetcher. One submission is enough per batch.
                    fut = pool.submit(_collect_ats_direct, src_queries[0] if src_queries else "")
                    tasks.append(fut)
                    task_meta[fut] = src
                    continue
                for q in src_queries:
                    fut = pool.submit(_collect_apify, src, q)
                    tasks.append(fut)
                    task_meta[fut] = src

            pending_sources = {task_meta[t] for t in tasks}

            for future in as_completed(tasks):
                src = task_meta.get(future, "?")
                try:
                    jobs_from_future = future.result()
                    raw_source_counts[src] = raw_source_counts.get(src, 0) + len(jobs_from_future)
                    kept = 0
                    fallback_kept = 0
                    for job in jobs_from_future:
                        if not job.get("title") or not job.get("company"):
                            filtered_reasons["invalid"] = filtered_reasons.get("invalid", 0) + 1
                            continue
                        jid = job.get("job_id") or _make_cache_key(job)
                        if self._job_matches_filters(
                            job=job,
                            query_terms=queries,
                            location=location,
                            date_posted=date_posted,
                            h1b_only=h1b_only,
                            visa_or_contract=False,
                            experience_level="",
                        ) and jid not in fallback_seen_ids:
                            fallback_seen_ids.add(jid)
                            fallback_jobs.append(job)
                            fallback_kept += 1
                        rejection_reason = self._job_filter_rejection_reason(
                            job=job,
                            query_terms=queries,
                            location=location,
                            date_posted=date_posted,
                            h1b_only=h1b_only,
                            visa_or_contract=visa_or_contract,
                            experience_level=experience_level,
                        )
                        if rejection_reason:
                            filtered_reasons[rejection_reason] = filtered_reasons.get(rejection_reason, 0) + 1
                            continue
                        if jid in seen_ids:
                            filtered_reasons["duplicate"] = filtered_reasons.get("duplicate", 0) + 1
                            continue
                        seen_ids.add(jid)
                        all_jobs.append(job)
                        kept += 1
                    source_counts[src] = source_counts.get(src, 0) + kept
                    fallback_source_counts[src] = fallback_source_counts.get(src, 0) + fallback_kept
                    logger.info(
                        "Job source %s returned %d raw, kept %d strict / %d closest after filters",
                        src, len(jobs_from_future), kept, fallback_kept,
                    )
                except Exception as e:
                    raw_msg = str(e)
                    redacted = _redact_secrets(raw_msg)
                    # Classify into a short, user-friendly reason.
                    lower = raw_msg.lower()
                    if "timed out" in lower or "timeout" in lower:
                        reason = "timed out"
                    elif "403" in lower or "forbidden" in lower:
                        reason = "access denied (actor may require rental)"
                    elif "401" in lower or "unauthorized" in lower:
                        reason = "invalid credentials"
                    elif "429" in lower:
                        reason = "rate limited"
                    else:
                        reason = redacted
                    logger.warning("Job source %s failed: %s", src, redacted)
                    errors.append(f"{src}: {reason}")

                # Stream what we have so far to the client.
                if partial_cb is not None:
                    pending_sources.discard(src)
                    try:
                        partial_scored = self.match_jobs(
                            list(all_jobs), user_email=user_email,
                        )
                        partial_cb({
                            "jobs": self._prepare_jobs_for_response(partial_scored),
                            "total": len(partial_scored),
                            "page": 1,
                            "total_pages": 1,
                            "queries_executed": len(queries),
                            "cache_hits": 0,
                            "cache_bypassed": bool(force_refresh),
                            "errors": list(errors),
                            "sources": dict(source_counts),
                            "raw_sources": dict(raw_source_counts),
                            "filtered_reasons": dict(filtered_reasons),
                            "selected_sources": list(selected),
                            "skipped_sources": dict(skipped_sources),
                            "pending": sorted(pending_sources),
                            "streaming": True,
                        })
                    except Exception as stream_err:
                        logger.warning(
                            "Partial stream callback failed for %s: %s",
                            src, stream_err,
                        )

        matched = self.match_jobs(all_jobs, user_email=user_email)
        if not matched and fallback_jobs and not h1b_only:
            logger.info(
                "Strict job filters produced no rows; showing %d closest matches",
                len(fallback_jobs),
            )
            matched = self.match_jobs(fallback_jobs, user_email=user_email)
            source_counts = fallback_source_counts
        returned_jobs = self._prepare_jobs_for_response(matched)
        return {
            # UI-trimmed, source-balanced top-N for immediate display.
            "jobs": returned_jobs,
            # Full scored list — survives caching so _apply_post_filters can
            # work against every row that came back from every source, not
            # just the UI-trimmed top. Without this, post-filter would only
            # see the 80 highest-scored rows (typically dominated by ATS
            # entries with long descriptions) and never surface Workday
            # Direct rows whose CXS list view omits the JD text.
            "_raw_jobs": list(matched),
            "total": len(matched),
            "page": 1,
            "total_pages": 1,
            "queries_executed": len(queries),
            "cache_hits": 0,
            "errors": errors,
            "sources": source_counts,
            "raw_sources": raw_source_counts,
            "filtered_reasons": filtered_reasons,
            "selected_sources": selected,
            "skipped_sources": skipped_sources,
        }

    @staticmethod
    def _max_queries_for_source(src: str, total_queries: int) -> int:
        """How many queries each source is willing to absorb in one batch."""
        if src == "jsearch":
            return min(total_queries, MAX_JSEARCH_QUERIES_PER_BATCH)
        if src == "google":
            # parseforge/google-jobs-scraper is heavy — keep it small.
            return min(total_queries, 2)
        if src in ("company", "workday"):
            return 1  # company scraper is URL-driven, not query-driven
        if src == "workday_direct":
            # Free per-tenant fan-out internally; one query is enough per batch
            # because each tenant returns up to 20 rows for that searchText.
            return min(total_queries, 2)
        if src == "ats_direct":
            return 1  # board APIs don't take a query — one pass is enough
        return min(total_queries, MAX_QUERIES_PER_APIFY_SOURCE)

    def _actor_id(self, source: str) -> str:
        env_key = f"APIFY_{source.upper()}_ACTOR"
        configured = _get_config_value(env_key, "")
        return configured or self.APIFY_ACTORS[source]

    @staticmethod
    def _linkedin_search_url(
        query: str,
        location: str,
        date_posted: str,
        remote_only: bool,
        employment_type: str,
        experience_level: str,
    ) -> str:
        clean_query = _simplify_jsearch_query(query)
        params: Dict[str, str] = {
            "keywords": clean_query or query,
            "location": location or "United States",
        }
        days = JobService._date_posted_days(date_posted)
        if days:
            seconds = days * 24 * 60 * 60
            params["f_TPR"] = f"r{seconds}"
        if remote_only:
            params["f_WT"] = "2"
        linked_in_types = {
            "FULLTIME": "F",
            "PARTTIME": "P",
            "CONTRACTOR": "C",
            "INTERN": "I",
        }
        if employment_type in linked_in_types:
            params["f_JT"] = linked_in_types[employment_type]
        if experience_level in ("entry", "associate"):
            params["f_E"] = "2"
        elif experience_level == "internship":
            params["f_E"] = "1"
        return "https://www.linkedin.com/jobs/search/?" + urlencode(params)

    @staticmethod
    def _workday_location(location: str) -> str:
        normalized = (location or "").strip()
        if not normalized or normalized.lower() in {"us", "usa", "united states"}:
            return "United States"
        return normalized

    def _build_apify_input(
        self,
        source: str,
        query: str,
        location: str,
        date_posted: str,
        remote_only: bool,
        employment_type: str,
        experience_level: str,
        company_urls: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        days = self._date_posted_days(date_posted)
        location = location or "United States"
        query = query or "software engineer entry level"
        linkedin_type = {
            "FULLTIME": "full-time",
            "PARTTIME": "part-time",
            "INTERN": "internship",
            "CONTRACTOR": "contract",
        }.get(employment_type, "")
        indeed_type = {
            "FULLTIME": "fulltime",
            "PARTTIME": "parttime",
            "INTERN": "internship",
            "CONTRACTOR": "contract",
        }.get(employment_type, "")

        # Individual free-actor schemas vary wildly, so we send a permissive
        # payload containing the common field-name variants. Unknown fields
        # are ignored by each actor — this lets us swap actor IDs via env
        # var without retuning inputs.
        proxy_cfg = {"useApifyProxy": True, "apifyProxyCountry": "US"}
        days_str = str(days or 0)
        time_posted = (
            "past-24h" if days == 1
            else "past-week" if days and days <= 7
            else "past-month" if days else ""
        )

        if source == "linkedin":
            return {
                "urls": [
                    self._linkedin_search_url(
                        query=query,
                        location=location,
                        date_posted=date_posted,
                        remote_only=remote_only,
                        employment_type=employment_type,
                        experience_level=experience_level,
                    )
                ],
                "scrapeCompany": False,
                "count": 50,
                "splitByLocation": False,
            }
        if source == "workday":
            clean_query = _simplify_jsearch_query(query)
            workday_type = {
                "FULLTIME": "FULL_TIME",
                "PARTTIME": "PART_TIME",
                "INTERN": "INTERN",
                "CONTRACTOR": "CONTRACTOR",
            }.get(employment_type, "")
            # Cast the widest sensible net at the actor: skip aiExperienceLevelFilter
            # entirely so we don't lose "Software Engineer" postings that the
            # actor's AI labeller never tagged 0-2. Seniority is applied in
            # _entry_level_signal during re-rank, which preserves listings
            # without explicit seniority phrasing instead of dropping them.
            payload: Dict[str, Any] = {
                "limit": 200,
                "includeAi": True,
                "includeLinkedIn": False,
                "titleSearch": [clean_query or query],
                "locationSearch": [self._workday_location(location)],
                "descriptionType": "text",
                "removeAgency": True,
            }
            if workday_type:
                payload["aiEmploymentTypeFilter"] = [workday_type]
            if remote_only:
                payload["aiWorkArrangementFilter"] = ["Remote OK", "Remote Solely"]
            return payload
        if source == "indeed":
            return {
                "keyword": query, "keywords": query, "query": query,
                "position": query, "searchTerms": query,
                "country": "US", "countryCode": "us",
                "location": location,
                "maxItems": 50, "maxResults": 50, "limit": 50, "maxRows": 50,
                "datePosted": days_str,
                "fromDays": days_str,
                "remote": remote_only,
                "isRemote": remote_only,
                "sort": "date",
                "proxy": proxy_cfg,
                "proxyConfiguration": proxy_cfg,
            }
        if source == "google":
            google_date = {
                "today": "today", "3days": "3days",
                "week": "week", "month": "month",
            }.get(date_posted, "")
            return {
                "keyword": query, "keywords": query, "query": query,
                "searchTerms": query, "title": query,
                "location": location,
                "countryCode": "us", "country": "US", "languageCode": "en",
                "maxItems": 50, "maxResults": 50, "limit": 50,
                "includeDetails": True,
                "dateFilter": google_date,
                "datePosted": google_date,
                "fromDays": days_str,
                "proxy": proxy_cfg,
                "proxyConfiguration": proxy_cfg,
            }
        if source == "jobright":
            return {
                "query": query,
                "keywords": query,
                "location": location,
                "country": "United States",
                "countryCode": "us",
                "datePosted": date_posted,
                "fromDays": str(days or 0),
                "remote": remote_only,
                "employmentType": employment_type,
                "experienceLevel": experience_level,
                "maxItems": 35,
                "proxyConfiguration": {"useApifyProxy": True, "apifyProxyCountry": "US"},
            }
        return {
            "startUrls": [{"url": url} for url in (company_urls or TOP_COMPANY_CAREER_URLS[:COMPANY_CAREER_BATCH_SIZE])],
            "maxJobsPerCompany": COMPANY_CAREER_MAX_JOBS_PER_COMPANY,
            "includeClosed": False,
            "outputFormat": "jobs",
        }

    @staticmethod
    def _format_apify_location(raw: Dict[str, Any]) -> str:
        locations = raw.get("locations_derived")
        if isinstance(locations, list):
            formatted: List[str] = []
            for loc in locations[:3]:
                if isinstance(loc, dict):
                    parts = [loc.get("city"), loc.get("admin"), loc.get("country")]
                    text = ", ".join(str(p) for p in parts if p)
                else:
                    text = str(loc or "")
                if text and text not in formatted:
                    formatted.append(text)
            if formatted:
                return " | ".join(formatted)

        for key in ("locations_alt_raw", "locations_raw", "cities_derived", "regions_derived", "countries_derived"):
            values = raw.get(key)
            if isinstance(values, list) and values:
                formatted = []
                for value in values[:3]:
                    if isinstance(value, dict):
                        text = value.get("address") or value.get("name") or json.dumps(value, sort_keys=True)
                    else:
                        text = str(value or "")
                    if text and text not in formatted:
                        formatted.append(text)
                if formatted:
                    return " | ".join(formatted)
        return ""

    @staticmethod
    def _format_apify_salary(raw: Dict[str, Any]) -> str:
        salary_info = raw.get("salaryInfo")
        if isinstance(salary_info, list) and salary_info:
            return " - ".join(str(item) for item in salary_info if item)

        salary_raw = raw.get("salary_raw")
        if isinstance(salary_raw, dict):
            text = salary_raw.get("value") or salary_raw.get("text") or salary_raw.get("unitText")
            if text:
                return str(text)

        currency = raw.get("ai_salary_currency") or "$"
        unit = raw.get("ai_salary_unittext") or ""
        def _money(value: Any) -> Optional[float]:
            try:
                return float(str(value).replace(",", ""))
            except (TypeError, ValueError):
                return None

        min_salary = _money(raw.get("ai_salary_minvalue"))
        max_salary = _money(raw.get("ai_salary_maxvalue"))
        value = _money(raw.get("ai_salary_value"))
        if min_salary and max_salary:
            return f"{currency}{min_salary:,.0f} - {currency}{max_salary:,.0f} {unit}".strip()
        if value:
            return f"{currency}{value:,.0f} {unit}".strip()
        return ""

    def _normalize_apify_job(self, raw: Dict[str, Any], source: str) -> Dict[str, Any]:
        nested_job = raw.get("job") if isinstance(raw.get("job"), dict) else {}
        nested_company = raw.get("company") if isinstance(raw.get("company"), dict) else {}
        nested_apply = raw.get("apply") if isinstance(raw.get("apply"), dict) else {}
        nested_location = raw.get("location") if isinstance(raw.get("location"), dict) else {}
        nested_dates = nested_job.get("dates") if isinstance(nested_job.get("dates"), dict) else {}
        nested_branding = nested_company.get("branding") if isinstance(nested_company.get("branding"), dict) else {}

        company = (
            raw.get("company")
            if isinstance(raw.get("company"), str)
            else raw.get("companyName")
            or nested_company.get("name")
            or nested_company.get("displayName")
            or raw.get("organization")
            or "Unknown"
        )
        title = (
            raw.get("title")
            or raw.get("jobTitle")
            or raw.get("positionName")  # misceres/indeed-scraper
            or nested_job.get("title")
            or nested_job.get("displayTitle")
            or "Unknown"
        )
        description = (
            raw.get("description")
            or raw.get("description_text")
            or raw.get("description_html")
            or raw.get("descriptionText")
            or raw.get("jobDescription")
            or nested_job.get("descriptionText")
            or nested_job.get("jobDescription")
            or raw.get("ai_core_responsibilities")
            or raw.get("ai_requirements_summary")
            or raw.get("details")
            or ""
        )
        location = (
            raw.get("location")
            if isinstance(raw.get("location"), str)
            else raw.get("locationShort")
            or raw.get("jobLocation")
            or raw.get("formattedLocation")
            or nested_location.get("display")
            or nested_location.get("text")
            or ", ".join([p for p in [nested_location.get("city"), nested_location.get("state"), nested_location.get("country")] if p])
            or self._format_apify_location(raw)
            or "Not specified"
        )
        salary_obj = nested_job.get("salary") if isinstance(nested_job.get("salary"), dict) else {}
        salary_insights = raw.get("salaryInsights") if isinstance(raw.get("salaryInsights"), dict) else {}
        salary = (
            raw.get("salary")
            or raw.get("salaryText")
            or raw.get("salarySnippet")
            or salary_obj.get("text")
            or salary_insights.get("compensationRange")
            or salary_insights.get("text")
            or self._format_apify_salary(raw)
            or ""
        )
        employment_type = raw.get("employmentType") or raw.get("employment_type") or raw.get("ai_employment_type") or nested_job.get("jobType") or ""
        if isinstance(employment_type, list):
            employment_type = ", ".join(str(t) for t in employment_type)
        posted = (
            raw.get("postedDate")
            or raw.get("date_posted")
            or raw.get("datePosted")
            or raw.get("postedAt")
            or raw.get("publishedAt")
            or raw.get("posted_time")
            or nested_dates.get("datePublished")
            or ""
        )
        posted_text = raw.get("postedTimeAgo") or raw.get("postedText") or raw.get("postedTime") or nested_dates.get("postedText") or ""
        apply_links = raw.get("applyLinks") if isinstance(raw.get("applyLinks"), list) else []
        first_apply_link = apply_links[0] if apply_links else {}
        if isinstance(first_apply_link, dict):
            first_apply_link = first_apply_link.get("url") or first_apply_link.get("link") or ""
        apply_link = (
            raw.get("applyUrl")
            or raw.get("apply_link")
            or raw.get("jobUrl")
            or raw.get("externalApplyLink")  # misceres/indeed-scraper
            or raw.get("link")                # bebity/linkedin-jobs-scraper
            or raw.get("url")
            or first_apply_link
            or nested_apply.get("thirdPartyApplyUrl")
            or nested_apply.get("url")
            or ""
        )
        logo = (
            raw.get("companyLogo")
            or raw.get("organization_logo")
            or raw.get("logoUrl")             # bebity
            or raw.get("companyLogoUrl")      # misceres
            or raw.get("logo")
            or nested_branding.get("logoUrl")
            or ""
        )
        is_remote = bool(
            raw.get("isRemote")
            or raw.get("remote")
            or raw.get("remote_derived")
            or str(raw.get("location_type") or "").upper() == "TELECOMMUTE"
            or "remote" in str(raw.get("ai_work_arrangement") or "").lower()
            or nested_job.get("isRemote")
            or "remote" in str(location).lower()
        )
        contract = self._is_contract_friendly({"title": title, "description": description, "employment_type": employment_type})
        h1b = bool(raw.get("ai_visa_sponsorship")) or self._check_h1b(str(company), description)
        job_id = str(raw.get("jobId") or raw.get("job_id") or raw.get("id") or nested_job.get("id") or nested_job.get("jobKey") or "")
        if not job_id:
            job_id = hashlib.md5(f"{source}|{company}|{title}|{apply_link}".encode()).hexdigest()

        return {
            "job_id": f"{source}:{job_id}",
            "title": str(title).strip(),
            "company": str(company).strip(),
            "logo": logo,
            "location": str(location).strip(),
            "apply_link": apply_link,
            "description": str(description),
            "salary": str(salary),
            "employment_type": str(employment_type),
            "date_posted": str(posted),
            "posted_text": str(posted_text),
            "is_remote": is_remote,
            "h1b_sponsor": h1b,
            "contract_friendly": contract,
            "source": source.title() if source != "company" else "Company Careers",
        }

    @staticmethod
    def _prepare_jobs_for_response(jobs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Source-balanced top-N trim.

        Pure top-N by match_score has a failure mode: sources that return long
        JD text (Greenhouse/Lever/Ashby) get higher skill-overlap scores than
        sources that only return list-view rows (Workday CXS, where the
        description is empty). Result: the UI was filling with Anthropic /
        Stripe / Scale AI postings and pushing Yahoo / Mastercard / Adobe
        below the visible cut. We now reserve a per-source share of the top
        slots, then fill the remainder by global score. Within each source
        the order is still match_score-descending.
        """
        if not jobs:
            return []
        per_source_cap = max(8, MAX_RETURNED_JOBS // 6)  # ~13 per source for 80 total
        by_source: Dict[str, List[Dict[str, Any]]] = {}
        for job in jobs:
            by_source.setdefault(job.get("source", "?"), []).append(job)
        balanced: List[Dict[str, Any]] = []
        seen_ids = set()
        for src, src_jobs in by_source.items():
            for job in src_jobs[:per_source_cap]:
                jid = id(job)
                if jid in seen_ids:
                    continue
                seen_ids.add(jid)
                balanced.append(job)
        # Fill remainder with the highest-scoring jobs we haven't taken yet.
        for job in jobs:
            if len(balanced) >= MAX_RETURNED_JOBS:
                break
            if id(job) in seen_ids:
                continue
            seen_ids.add(id(job))
            balanced.append(job)
        balanced.sort(
            key=lambda j: j.get("match_score", 0),
            reverse=True,
        )
        prepared: List[Dict[str, Any]] = []
        for job in balanced[:MAX_RETURNED_JOBS]:
            item = dict(job)
            description = str(item.get("description") or "")
            if len(description) > JOB_DESCRIPTION_MAX_CHARS:
                item["description"] = description[:JOB_DESCRIPTION_MAX_CHARS].rstrip() + "..."
            missing = item.get("missing_skills")
            if isinstance(missing, list) and len(missing) > 30:
                item["missing_skills"] = missing[:30]
            matched = item.get("matched_skills")
            if isinstance(matched, list) and len(matched) > 20:
                item["matched_skills"] = matched[:20]
            prepared.append(item)
        return prepared

    def _prepare_result_for_response(self, result: Dict[str, Any]) -> Dict[str, Any]:
        prepared = dict(result)
        jobs = prepared.get("jobs")
        if isinstance(jobs, list):
            prepared["jobs"] = self._prepare_jobs_for_response(jobs)
            prepared["total"] = len(prepared["jobs"])
            prepared["total_pages"] = prepared.get("total_pages", 1)
        return prepared

    def _job_matches_filters(
        self,
        job: Dict[str, Any],
        query_terms: List[str],
        location: str,
        date_posted: str,
        h1b_only: bool,
        visa_or_contract: bool,
        experience_level: str,
    ) -> bool:
        return self._job_filter_rejection_reason(
            job=job,
            query_terms=query_terms,
            location=location,
            date_posted=date_posted,
            h1b_only=h1b_only,
            visa_or_contract=visa_or_contract,
            experience_level=experience_level,
        ) is None

    def _job_filter_rejection_reason(
        self,
        job: Dict[str, Any],
        query_terms: List[str],
        location: str,
        date_posted: str,
        h1b_only: bool,
        visa_or_contract: bool,
        experience_level: str,
    ) -> Optional[str]:
        if date_posted != "all" and not self._is_recent_job(job, date_posted):
            return "date"
        if h1b_only and not job.get("h1b_sponsor"):
            return "h1b"
        if visa_or_contract and not (job.get("h1b_sponsor") or job.get("contract_friendly")):
            return "visa_or_contract"
        # Soft-reject: only drop jobs with EXPLICIT senior signal. Postings with
        # no seniority marker pass through and are sorted lower by match_jobs.
        if experience_level in ("entry", "internship", "associate"):
            if self._entry_level_signal(job) == "senior":
                return "experience"
        if location and location.lower() not in {"united states", "usa", "us"}:
            loc = (job.get("location") or "").lower()
            if location.lower() not in loc and "remote" not in loc:
                return "location"
        if job.get("source") == "Company Careers" and query_terms:
            haystack = f"{job.get('title', '')} {job.get('description', '')}".lower()
            keywords = set()
            for query in query_terms:
                keywords.update(t for t in re.split(r"[^a-z0-9+#.]+", query.lower()) if len(t) > 2)
            ignored = {"entry", "level", "new", "grad", "h1b", "sponsor", "contract", "engineer"}
            keywords -= ignored
            if keywords and not any(k in haystack for k in keywords):
                return "query"
        return None

    @staticmethod
    def _date_posted_days(date_posted: str) -> Optional[int]:
        return {
            "today": 1,
            "3days": 3,
            "week": 7,
            "month": 30,
            "all": None,
        }.get(date_posted, 1)

    def _is_recent_job(self, job: Dict[str, Any], date_posted: str) -> bool:
        """True if the job's posted-date falls inside the requested window.

        Uses an hour-grain budget instead of a strict calendar-day cutoff so that
        a job posted 25h ago doesn't disappear when the user clicks "today" in
        their local timezone (the underlying actors return UTC timestamps). The
        grace windows are: today=36h, 3days=84h, week=192h, month=720h. Posts
        with no parseable date keep the previous fail-open behavior because most
        Workday/ATS feeds omit `postedAt` on roles updated less than 24h ago.
        """
        hours = self._date_posted_hours(date_posted)
        if hours is None:
            return True
        raw_date = str(job.get("date_posted") or "")
        posted_text = str(job.get("posted_text") or "").lower()
        if posted_text:
            if any(token in posted_text for token in ("hour", "minute", "just now", "today")):
                return True
            match = re.search(r"(\d+)\s+day", posted_text)
            if match:
                return int(match.group(1)) * 24 <= hours
            if "week" in posted_text:
                return hours >= 7 * 24
            if "month" in posted_text:
                return hours >= 30 * 24
        if not raw_date:
            return True
        candidates = [raw_date, raw_date[:10]]
        for candidate in candidates:
            try:
                normalized = candidate.replace("Z", "+00:00")
                dt = datetime.fromisoformat(normalized)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt >= datetime.now(timezone.utc) - timedelta(hours=hours)
            except Exception:
                continue
        return True

    @staticmethod
    def _date_posted_hours(date_posted: str) -> Optional[int]:
        """Window in hours used by the post-filter. Adds a 12h grace to each
        bucket so a job posted in a different timezone, or backdated when the
        actor first indexed it, doesn't get silently dropped at the boundary."""
        return {
            "today": 36,
            "3days": 84,
            "week": 192,
            "month": 30 * 24,
            "all": None,
        }.get(date_posted, 36)

    @staticmethod
    def _is_contract_friendly(job: Dict[str, Any]) -> bool:
        text = f"{job.get('title', '')} {job.get('description', '')} {job.get('employment_type', '')}".lower()
        return any(keyword in text for keyword in CONTRACT_KEYWORDS)

    @staticmethod
    def _entry_level_signal(job: Dict[str, Any]) -> str:
        """Three-state seniority read: 'entry' | 'unknown' | 'senior'.

        The old _is_entry_level required a NEW_GRAD_KEYWORDS match. That dropped
        the majority of real entry roles whose title is "Software Engineer" with
        nothing else qualifying. The new signal only rejects on EXPLICIT senior
        markers; everything else (most postings) lands in 'unknown' and is kept,
        but match_jobs() de-ranks it relative to confirmed 'entry' titles so
        explicit new-grad listings still bubble up first.
        """
        title = (job.get("title") or "").lower()
        desc = (job.get("description") or "").lower()
        emp = (job.get("employment_type") or "").lower()
        text = f"{title} {desc} {emp}"
        senior_markers = [
            "senior", "sr.", "sr ", "principal", "staff engineer", "staff swe",
            "lead engineer", "lead developer", "tech lead", "team lead",
            "engineering manager", "director", "head of", "vp ", "vice president",
            " architect ", "architect i", "architect ii",
        ]
        # Title is most reliable — description sometimes mentions "you'll mentor
        # senior engineers" on a junior role, which would false-positive.
        if any(marker in title for marker in (
            "senior", "sr.", "principal", "staff", "lead", "manager",
            "director", "head of", "vp ", "architect", "distinguished",
            "fellow ", " fellow", "chief ",
        )):
            return "senior"
        if any(marker in text for marker in senior_markers):
            return "senior"
        if any(kw in text for kw in NEW_GRAD_KEYWORDS):
            return "entry"
        # Common entry-level title patterns even when no new-grad phrase is present.
        if re.search(r"\b(swe|sde|software engineer|developer)\s*(i|1|i{1,3})\b", title):
            return "entry"
        return "unknown"

    @staticmethod
    def _is_entry_level(job: Dict[str, Any]) -> bool:
        """Legacy boolean kept for callers that don't need the 3-state result."""
        return JobService._entry_level_signal(job) == "entry"

    def _normalize_job(self, raw: dict) -> Dict[str, Any]:
        company = (raw.get("employer_name") or "Unknown").strip()
        normalized = {
            "job_id": raw.get("job_id", ""),
            "title": raw.get("job_title", "Unknown"),
            "company": company,
            "logo": raw.get("employer_logo", ""),
            "location": self._build_location(raw),
            "apply_link": raw.get("job_apply_link", ""),
            "description": raw.get("job_description", ""),
            "salary": self._build_salary(raw),
            "employment_type": raw.get("job_employment_type", ""),
            "date_posted": raw.get("job_posted_at_datetime_utc", ""),
            "is_remote": raw.get("job_is_remote", False),
            "h1b_sponsor": self._check_h1b(company, raw.get("job_description", "")),
            "source": "JSearch",
            "posted_text": raw.get("job_posted_at", ""),
        }
        normalized["contract_friendly"] = self._is_contract_friendly(normalized)
        return normalized

    @staticmethod
    def _build_location(raw: dict) -> str:
        city = raw.get("job_city", "")
        state = raw.get("job_state", "")
        country = raw.get("job_country", "")
        parts = [p for p in [city, state, country] if p]
        return ", ".join(parts) or "Not specified"

    @staticmethod
    def _build_salary(raw: dict) -> str:
        min_s = raw.get("job_min_salary")
        max_s = raw.get("job_max_salary")
        period = raw.get("job_salary_period", "")
        if min_s and max_s:
            return f"${min_s:,.0f} - ${max_s:,.0f} {period}".strip()
        if min_s:
            return f"${min_s:,.0f}+ {period}".strip()
        if max_s:
            return f"Up to ${max_s:,.0f} {period}".strip()
        return ""

    @staticmethod
    def _check_h1b(company: str, description: str) -> bool:
        company_lower = company.lower()
        for sponsor in H1B_SPONSORS:
            if sponsor in company_lower:
                return True
        desc_lower = description.lower()
        if "h1b" in desc_lower or "h-1b" in desc_lower or "visa sponsor" in desc_lower:
            return True
        return False

    # ------------------------------------------------------------------
    # Skill Matching
    # ------------------------------------------------------------------

    def match_jobs(self, jobs: List[Dict], user_email: str = "") -> List[Dict]:
        """Score and rank jobs against the user's resume profile.

        The score blends four signals (each 0..1, then weighted):
          - title_relevance:   does the job title fall in the candidate's intent family?
          - skill_overlap:     weighted resume-skill hits in the JD (core skills count more)
          - domain_relevance:  does the JD mention the candidate's domain at all?
          - experience_alignment: seniority bracket match

        On top of the blended base we still apply small bonuses (H1B, remote,
        new-grad keywords) so users with a hard preference still see them
        bubble up. The previous flat skill-overlap heuristic is preserved as
        a fallback when no resume / structured profile is available.
        """
        from services.resume_profiler import (
            domain_relevance,
            experience_alignment,
            feedback_signal_for_user,
            title_relevance,
            weighted_skill_overlap,
        )

        profile = self._get_resume_profile(user_email=user_email)

        # No resume yet → fall back to the original flat scorer (legacy path)
        # so first-time users still get useful results before they upload.
        if not profile:
            return self._legacy_match_jobs(jobs, user_email=user_email)

        weights = {
            "title": 35.0,
            "skill": 30.0,
            "domain": 20.0,
            "experience": 15.0,
        }

        for job in jobs:
            title = job.get("title", "") or ""
            desc = job.get("description", "") or ""
            text = f"{title} {desc}"

            title_score = title_relevance(profile, title)
            matched, raw_overlap, norm_overlap = weighted_skill_overlap(profile, text)
            domain_score = domain_relevance(profile, text)
            exp_score = experience_alignment(profile, text)

            base = (
                title_score * weights["title"]
                + norm_overlap * weights["skill"]
                + domain_score * weights["domain"]
                + exp_score * weights["experience"]
            )

            # Small qualitative bonuses (capped so they can't replace fit).
            bonus = 0
            if job.get("h1b_sponsor"):
                bonus += 8
            title_desc = text.lower()
            if any(kw in title_desc for kw in NEW_GRAD_KEYWORDS) and profile.get("experience_years", 0) <= 3:
                bonus += 5
            if job.get("is_remote"):
                bonus += 2

            score = int(min(100, max(0, round(base + bonus))))

            job["match_score"] = score
            job["matched_skills"] = sorted(set(matched))
            # Surface the signal breakdown so the UI can explain *why* a job
            # ranks where it does (and so we can tune weights from real data).
            job["match_breakdown"] = {
                "title_relevance": round(title_score, 2),
                "skill_overlap": round(norm_overlap, 3),
                "domain_relevance": round(domain_score, 2),
                "experience_alignment": round(exp_score, 2),
                "weighted_skill_score": raw_overlap,
                "primary_intent": profile.get("primary_intent"),
                "secondary_intent": profile.get("secondary_intent"),
            }
            # Keep `missing_skills` as the previous shape for the analyze flow:
            # everything in the candidate's weighted-skill list that the job
            # didn't mention. We report the highest-weight ones first.
            weighted = profile.get("weighted_skills") or {}
            missing = [s for s in weighted if s not in matched]
            missing.sort(key=lambda s: weighted.get(s, 0.0), reverse=True)
            job["missing_skills"] = missing[:30]

        jobs.sort(key=lambda j: j["match_score"], reverse=True)
        return jobs

    def _legacy_match_jobs(self, jobs: List[Dict], user_email: str = "") -> List[Dict]:
        """Pre-profile flat scorer — kept as a fallback for users without a resume."""
        resume_skills = self._get_resume_skills(user_email=user_email)
        all_skills = set(s.lower() for s in PORTFOLIO_SKILLS)
        all_skills.update(s.lower() for s in resume_skills)

        for job in jobs:
            desc_lower = (job.get("description", "") + " " + job.get("title", "")).lower()
            matched = []
            for skill in all_skills:
                pattern = r"\b" + re.escape(skill) + r"\b" if len(skill) > 2 else re.escape(skill)
                if re.search(pattern, desc_lower, re.IGNORECASE):
                    matched.append(skill)

            score = min(len(matched) * 10, 70)  # cap skill portion at 70
            if job.get("h1b_sponsor"):
                score += 15
            title_desc = (job.get("title", "") + " " + job.get("description", "")).lower()
            if any(kw in title_desc for kw in NEW_GRAD_KEYWORDS):
                score += 10
            if job.get("is_remote"):
                score += 5
            score = min(score, 100)

            job["match_score"] = score
            job["matched_skills"] = sorted(set(matched))
            job["missing_skills"] = sorted(all_skills - set(matched))

        jobs.sort(key=lambda j: j["match_score"], reverse=True)
        return jobs

    def _get_resume_skills(self, user_email: str = "") -> List[str]:
        """Get flat skills list from the stored resume for this user."""
        if not user_email:
            return []
        try:
            resume = self.user_resumes.find_one(
                {"user_email": user_email, "skills": {"$exists": True}},
                sort=[("parsed_at", -1)],
            )
            if resume:
                return resume.get("skills", [])
        except Exception as e:
            logger.warning(f"Failed to load resume skills for {user_email}: {e}")
        return []

    def _get_resume_profile(self, user_email: str = "") -> Optional[Dict[str, Any]]:
        """Build (or load) a resume_profiler profile for the user.

        Profiles are derived deterministically from the structured resume on
        every call — they're cheap (no LLM, ~milliseconds) so we don't bother
        caching across requests. We DO incorporate the user's recent activity
        as a feedback signal so clicks/applies bias future ranking.
        """
        if not user_email:
            return None
        try:
            resume = self.user_resumes.find_one(
                {"user_email": user_email, "structured": {"$exists": True}},
                sort=[("parsed_at", -1)],
            )
            if not resume:
                return None
            structured = resume.get("structured") or {}
            # Mirror experience_years from the flat field if structured lacks it.
            if "experience_years" not in structured and "experience_years" in resume:
                structured = dict(structured)
                structured["experience_years"] = resume.get("experience_years")
            from services.resume_profiler import build_profile, feedback_signal_for_user
            try:
                feedback = feedback_signal_for_user(self.db.job_activity_log, user_email)
            except Exception:
                feedback = None
            return build_profile(structured, feedback_signal=feedback)
        except Exception as e:
            logger.warning(f"Failed to build resume profile for {user_email}: {e}")
            return None

    def get_resume(self, user_email: str = "") -> Optional[Dict[str, Any]]:
        """Retrieve the latest stored resume for this user (used by analyze_job)."""
        if not user_email:
            return None
        resume = self.user_resumes.find_one(
            {"user_email": user_email, "skills": {"$exists": True}, "parsed_at": {"$exists": True}},
            sort=[("parsed_at", -1)],
        )
        if resume:
            resume.pop("_id", None)
        return resume

    # ------------------------------------------------------------------
    # AI Job Analysis
    # ------------------------------------------------------------------

    def analyze_job(self, job: Dict[str, Any], action: str, user_email: str = "") -> str:
        # Note: previously imported PORTFOLIO_CONTEXT from services.chat_service,
        # which doesn't exist in this Lambda bundle (cross-service import was
        # a long-standing bug — the call would ImportError at runtime). Dropping
        # the cross-service dep and using the local Claude provider.
        from services.llm_providers import get_provider

        resume = self.get_resume(user_email=user_email)
        resume_info = ""
        if resume:
            resume_info = (
                f"\nCandidate skills: {', '.join(resume.get('skills', []))}\n"
                f"Experience: {resume.get('experience_years', 'N/A')} years\n"
                f"Summary: {resume.get('summary', 'N/A')}\n"
            )

        job_info = (
            f"Job Title: {job.get('title', 'N/A')}\n"
            f"Company: {job.get('company', 'N/A')}\n"
            f"Location: {job.get('location', 'N/A')}\n"
            f"Description:\n{job.get('description', 'N/A')[:4000]}\n"
        )

        prompts = {
            "summarize": (
                f"Summarize this job posting concisely. Highlight key responsibilities, "
                f"requirements, and what makes it a good or poor fit for the candidate.\n\n"
                f"{job_info}\n{resume_info}"
            ),
            "missing_skills": (
                f"Analyze the gap between the candidate's skills and this job's requirements. "
                f"List missing skills, suggest learning resources, and rate the gap (Low/Medium/High).\n\n"
                f"{job_info}\n{resume_info}"
            ),
            "cover_letter": (
                f"Write a professional cover letter for this job tailored to the candidate. "
                f"Highlight relevant projects and experience. Keep it under 400 words.\n\n"
                f"{job_info}\n{resume_info}"
            ),
        }

        prompt = prompts.get(action)
        if not prompt:
            raise ValueError(f"Unknown action: {action}")

        return get_provider().text(
            prompt=prompt,
            temperature=0.7,
            max_tokens=2048,
        )

    # ------------------------------------------------------------------
    # Saved Jobs CRUD
    # ------------------------------------------------------------------

    def get_saved_jobs(self, user_email: str) -> List[Dict[str, Any]]:
        jobs = list(self.saved_jobs.find({"user_email": user_email}).sort("saved_at", -1))
        for j in jobs:
            j["_id"] = str(j["_id"])
        return jobs

    def save_job(self, job: Dict[str, Any], user_email: str) -> Dict[str, Any]:
        doc = {
            "job_id": job.get("job_id", ""),
            "user_email": user_email,
            "job_data": job,
            "status": "interested",
            "notes": "",
            "saved_at": datetime.now(timezone.utc),
        }
        self.saved_jobs.update_one(
            {"job_id": doc["job_id"], "user_email": user_email},
            {"$set": doc},
            upsert=True,
        )
        result = self.saved_jobs.find_one({"job_id": doc["job_id"], "user_email": user_email})
        result["_id"] = str(result["_id"])
        return result

    def update_saved_job(
        self, job_id: str, status: Optional[str] = None, notes: Optional[str] = None,
        user_email: str = ""
    ) -> Optional[Dict[str, Any]]:
        update = {}
        if status is not None:
            update["status"] = status
        if notes is not None:
            update["notes"] = notes
        if not update:
            return None
        query = {"job_id": job_id, "user_email": user_email}
        self.saved_jobs.update_one(query, {"$set": update})
        result = self.saved_jobs.find_one(query)
        if result:
            result["_id"] = str(result["_id"])

        # Bidirectional sync — when a Daily Pipeline saved_job changes status
        # AND the user has tailoring_records linked to it (source_job_id), keep
        # the most recent tailoring_record in lockstep so the Applications
        # kanban + Batch Tailor view both reflect the change. Daily Pipeline
        # row "I applied" → tailoring_record auto-applied, no separate click.
        # Best-effort; failure here doesn't block the saved_job update.
        if status is not None:
            try:
                from utils.db_connect import DBConnect
                from datetime import datetime as _dt, timezone as _tz
                _db = DBConnect().get_db()
                trec = _db.tailoring_records.find_one(
                    {"user_email": user_email, "source_job_id": job_id},
                    sort=[("created_at", -1)],
                    projection={"record_id": 1, "_id": 0},
                )
                if trec and trec.get("record_id"):
                    set_fields = {
                        "application.status": status,
                        "application.updated_at": _dt.now(_tz.utc),
                        # Clear the batch-confirmation prompt — the user has
                        # answered via the Daily Pipeline side.
                        "application.batch_confirmation_pending": False,
                        "updated_at": _dt.now(_tz.utc),
                    }
                    if status == "applied":
                        set_fields["application.applied_at"] = _dt.now(_tz.utc)
                    _db.tailoring_records.update_one(
                        {"record_id": trec["record_id"], "user_email": user_email},
                        {"$set": set_fields},
                    )
                    logger.info(
                        "saved_job→tailoring_record sync: %s → status=%s",
                        trec["record_id"], status,
                    )
            except Exception as e:
                logger.warning("saved_job→tailoring sync failed: %s", e)

        return result

    def delete_saved_job(self, job_id: str, user_email: str = "") -> bool:
        result = self.saved_jobs.delete_one({"job_id": job_id, "user_email": user_email})
        return result.deleted_count > 0

    # ------------------------------------------------------------------
    # Saved Search Filters
    # ------------------------------------------------------------------

    def get_saved_filters(self, user_email: str) -> Optional[Dict[str, Any]]:
        result = self.job_filter_preferences.find_one({"user_email": user_email})
        if not result:
            return None
        result["_id"] = str(result["_id"])
        if hasattr(result.get("updated_at"), "isoformat"):
            result["updated_at"] = result["updated_at"].isoformat()
        return result

    def save_filters(self, filters: Dict[str, Any], user_email: str) -> Dict[str, Any]:
        doc = {
            "user_email": user_email,
            "filters": filters,
            "updated_at": datetime.now(timezone.utc),
        }
        self.job_filter_preferences.update_one(
            {"user_email": user_email},
            {"$set": doc},
            upsert=True,
        )
        result = self.job_filter_preferences.find_one({"user_email": user_email})
        result["_id"] = str(result["_id"])
        if hasattr(result.get("updated_at"), "isoformat"):
            result["updated_at"] = result["updated_at"].isoformat()
        return result

    # ------------------------------------------------------------------
    # Daily-pipeline named presets — separate collection from the single
    # search-page filter prefs above. A preset bundles all the daily-pipeline
    # form state (titles, keywords, filter knobs) under a user-given name so
    # the candidate can keep e.g. "ML strict", "Cloud broad", "Internships"
    # ready to load with one click.
    # ------------------------------------------------------------------

    def list_pipeline_presets(self, user_email: str) -> List[Dict[str, Any]]:
        if not user_email:
            return []
        col = self.db.pipeline_filter_presets
        try:
            col.create_index([("user_email", 1), ("name", 1)], unique=True)
        except Exception:
            pass
        rows = list(col.find({"user_email": user_email}).sort("updated_at", -1))
        out: List[Dict[str, Any]] = []
        for row in rows:
            row["_id"] = str(row["_id"])
            if hasattr(row.get("updated_at"), "isoformat"):
                row["updated_at"] = row["updated_at"].isoformat()
            if hasattr(row.get("created_at"), "isoformat"):
                row["created_at"] = row["created_at"].isoformat()
            out.append(row)
        return out

    def save_pipeline_preset(
        self, user_email: str, name: str, filters: Dict[str, Any]
    ) -> Dict[str, Any]:
        if not user_email:
            return {"ok": False, "error": "Authentication required"}
        clean_name = (name or "").strip()
        if not clean_name or len(clean_name) > 60:
            return {"ok": False, "error": "Preset name must be 1-60 chars"}
        col = self.db.pipeline_filter_presets
        try:
            col.create_index([("user_email", 1), ("name", 1)], unique=True)
        except Exception:
            pass
        now = datetime.now(timezone.utc)
        col.update_one(
            {"user_email": user_email, "name": clean_name},
            {
                "$set": {
                    "user_email": user_email,
                    "name": clean_name,
                    "filters": filters or {},
                    "updated_at": now,
                },
                "$setOnInsert": {"created_at": now},
            },
            upsert=True,
        )
        return {"ok": True, "presets": self.list_pipeline_presets(user_email)}

    def delete_pipeline_preset(self, user_email: str, name: str) -> Dict[str, Any]:
        if not user_email or not name:
            return {"ok": False, "error": "Bad request"}
        col = self.db.pipeline_filter_presets
        col.delete_one({"user_email": user_email, "name": name})
        return {"ok": True, "presets": self.list_pipeline_presets(user_email)}


# Singleton
_job_service = None


def get_job_service() -> JobService:
    global _job_service
    if _job_service is None:
        _job_service = JobService()
    return _job_service
