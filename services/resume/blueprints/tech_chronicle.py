"""
Tech Chronicle blueprint — REAL tech news + honest career tips.

Feeds the landing/AuthGate "tech chronicle" and the dashboard "Market trends".

Integrity-first (like the rest of the product):
  - Tech news is fetched LIVE from Hacker News (Algolia front-page API): real
    titles, real working links, real points/comments — never fabricated. (This
    replaces a prior version that asked an LLM to *invent* "trending" headlines
    with made-up URLs and engagement counts — presenting fiction as real news.)
  - Career tips are AI-generated but EVERGREEN advice under a hard no-fabrication
    rule (no invented statistics, studies, or figures presented as fact).

Both are cached in MongoDB (tech_chronicle_cache) with a TTL; the feed
self-refreshes on access after expiry.

Endpoints:
  GET  /api/tech-chronicle            — merged feed, optional ?category filter (public)
  POST /api/tech-chronicle/regenerate — force refresh (ADMIN ONLY)
"""
import logging
import re
from datetime import datetime, timezone, timedelta
from urllib.parse import quote_plus

import requests
from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity

from utils.db_connect import DBConnect

logger = logging.getLogger(__name__)

tech_chronicle_bp = Blueprint("tech_chronicle", __name__)

_NEWS_TTL = timedelta(hours=2)     # real, cheap to refresh; front page moves
_CAREER_TTL = timedelta(hours=12)  # evergreen tips; refresh rarely

HN_API = "https://hn.algolia.com/api/v1/search"
HTTP_TIMEOUT = 10

# If Hacker News is unreachable we show NOTHING rather than fake news.
_FALLBACK_NEWS: list = []

_FALLBACK_CAREER = [
    {
        "id": "fallback-tip-quantify",
        "category": "tip",
        "headline": "Quantify your bullets with real numbers",
        "summary": "Recruiters skim fast, so concrete outcomes from your own work "
                   "('cut deploy time from 20 to 8 minutes') read stronger than vague "
                   "phrases. Use real figures — never invented ones.",
        "tags": ["Résumé", "Tips"],
        "timeAgo": "", "readTime": "2 min read",
    },
    {
        "id": "fallback-tip-tailor",
        "category": "tip",
        "headline": "Tailor each application to the role",
        "summary": "Mirror the job description's language where it honestly fits your "
                   "experience. A focused, relevant application beats a generic one — and "
                   "beats firing off dozens of untailored ones.",
        "tags": ["Job search"],
        "timeAgo": "", "readTime": "2 min read",
    },
]


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------
def _get_cache_collection():
    return DBConnect().get_db().tech_chronicle_cache


def _load_cached(kind: str):
    coll = _get_cache_collection()
    doc = coll.find_one({"kind": kind})
    if not doc:
        return None, None
    ttl = _NEWS_TTL if kind == "news" else _CAREER_TTL
    generated_at = doc.get("generated_at")
    if not generated_at:
        return None, None
    if generated_at.tzinfo is None:
        generated_at = generated_at.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) - generated_at > ttl:
        return None, generated_at  # expired
    return doc.get("items") or [], generated_at


def _save_cache(kind: str, items: list):
    _get_cache_collection().update_one(
        {"kind": kind},
        {"$set": {"kind": kind, "items": items, "generated_at": datetime.now(timezone.utc)}},
        upsert=True,
    )


# ---------------------------------------------------------------------------
# Real tech news — Hacker News front page
# ---------------------------------------------------------------------------
_LABEL = {"ai": "AI", "cloud": "Cloud", "devops": "DevOps", "security": "Security",
          "data": "Data", "web": "Web", "systems": "Systems"}
_CATEGORY_KEYWORDS = [
    ("ai", ("ai", " ml", "llm", "gpt", "claude", "gemini", "openai", "anthropic",
            "machine learning", "deep learning", "neural", "diffusion", "agent", "model")),
    ("cloud", ("aws", "azure", "gcp", "cloud", "serverless", "lambda", "s3")),
    ("devops", ("kubernetes", "k8s", "docker", "ci/cd", "devops", "terraform",
                "container", "deployment", "platform engineering")),
    ("security", ("security", "vulnerability", "cve", "exploit", "breach",
                  "ransomware", "zero-day", "encryption", "privacy")),
    ("data", ("database", "sql", "postgres", "mysql", "analytics", "etl",
              "warehouse", "kafka", "streaming", "duckdb")),
    ("web", ("react", "vue", "svelte", "angular", " css", "html", "browser",
             "javascript", "typescript", "frontend", "wasm", "next.js")),
]


def _categorize(title: str) -> str:
    t = f" {(title or '').lower()} "
    for cat, kws in _CATEGORY_KEYWORDS:
        if any(k in t for k in kws):
            return cat
    return "systems"


def _domain(url: str) -> str:
    m = re.search(r"https?://(?:www\.)?([^/]+)", url or "")
    return m.group(1) if m else "news.ycombinator.com"


def _time_ago(created_i) -> str:
    try:
        mins = int((datetime.now(timezone.utc).timestamp() - int(created_i)) / 60)
    except Exception:
        return ""
    if mins < 60:
        return f"{max(1, mins)}m ago"
    if mins < 1440:
        return f"{mins // 60}h ago"
    return f"{mins // 1440}d ago"


def _generate_tech_news() -> list:
    """Fetch REAL trending tech stories from Hacker News (front page). Real titles,
    real links, real points/comments — no fabrication."""
    try:
        resp = requests.get(
            HN_API, params={"tags": "front_page", "hitsPerPage": 20},
            headers={"User-Agent": "JobPilot/1.0 (+https://jobpilot.app)"},
            timeout=HTTP_TIMEOUT,
        )
        if resp.status_code != 200:
            logger.warning("HN fetch status %s", resp.status_code)
            return []
        hits = resp.json().get("hits") or []
    except Exception as e:
        logger.warning("HN fetch failed: %s", e)
        return []

    items = []
    for h in hits:
        title = (h.get("title") or "").strip()
        oid = str(h.get("objectID") or "")
        if not title or not oid:
            continue
        url = (h.get("url") or "").strip()
        hn_url = f"https://news.ycombinator.com/item?id={oid}"
        cat = _categorize(title)
        items.append({
            "id": f"hn-{oid}",
            "category": cat,
            "headline": title[:140],
            "summary": "",
            "source": _domain(url) if url else "news.ycombinator.com",
            "sourceUrl": url or hn_url,
            "sourceIsSearch": False,
            "tags": [_LABEL.get(cat, "Tech")],
            "upvotes": int(h.get("points") or 0),
            "comments": int(h.get("num_comments") or 0),
            "timeAgo": _time_ago(h.get("created_at_i")),
            "readTime": "",
        })
    logger.info("Fetched %d real tech stories from Hacker News", len(items))
    return items


# ---------------------------------------------------------------------------
# Career tips — AI-generated, EVERGREEN, no fabricated stats
# ---------------------------------------------------------------------------
def _generate_career_intel() -> list:
    from services.claude_client import claude_json, CLAUDE_FLASH

    prompt = """Generate a JSON array of 8 EVERGREEN, genuinely useful career tips for
tech job seekers (especially students and new grads). These are advice, not news.

Mix categories: "tip" (4), "trend" (2 durable skill trends), "stat" (2 widely-accepted insights).

CRITICAL honesty rules (this product is integrity-first):
- Do NOT invent specific statistics, percentages, salary figures, study citations, or
  company names and present them as fact. Keep advice qualitative unless it is genuine
  common knowledge. No fabricated precision like "callbacks rise 2.3x".

Each item: {
  id (lowercase-hyphenated slug), category ("tip"|"trend"|"stat"),
  headline (<=80 chars, punchy, no invented numbers),
  summary (2-3 sentence actionable, specific, not invented),
  tags (1-2 short strings), timeAgo (always ""), readTime ("X min read")
}
Return JSON: {"items": [...]}"""
    schema = {"items": [{
        "id": str, "category": str, "headline": str, "summary": str,
        "tags": [str], "timeAgo": str, "readTime": str,
    }]}
    try:
        result = claude_json(prompt=prompt, max_tokens=14000, temperature=0.6,
                             model=CLAUDE_FLASH, schema=schema)
        items = result.get("items") or []
        for it in items:
            it["timeAgo"] = ""  # evergreen — never fake a timestamp
        logger.info("Generated %d career tips via LLM", len(items))
        return items
    except Exception as e:
        logger.error("Career intel generation failed: %s", e)
        return []


# ---------------------------------------------------------------------------
# Source-link safety (career items have no link)
# ---------------------------------------------------------------------------
_URL_RE = re.compile(r"^https?://[\w.-]+\.\w+(/[^\s]*)?$", re.IGNORECASE)


def _normalize_source_link(item: dict) -> dict:
    if item.get("category") in {"trend", "tip", "stat"}:
        return item
    source_url = (item.get("sourceUrl") or "").strip()
    if source_url and not _URL_RE.match(source_url):
        source_url = ""
    if not source_url and item.get("headline"):
        query = quote_plus(f"{item.get('headline')} {item.get('source') or ''}".strip())
        item["sourceUrl"] = f"https://www.google.com/search?q={query}"
        item["sourceIsSearch"] = True
    else:
        item["sourceUrl"] = source_url
        item["sourceIsSearch"] = False
    return item


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------
def _ensure_fresh(kind: str) -> list:
    cached, _ = _load_cached(kind)
    if cached:
        return cached
    if kind == "news":
        items = _generate_tech_news()
        if not items:
            return _FALLBACK_NEWS
        items = [_normalize_source_link(it) for it in items]
    else:
        items = _generate_career_intel()
        if not items:
            return _FALLBACK_CAREER
    _save_cache(kind, items)
    return items


def _parse_time_ago(s: str) -> int:
    if not s:
        return 999
    m = re.match(r"(\d+)\s*([mhd])", s.lower())
    if not m:
        return 999
    n, unit = int(m.group(1)), m.group(2)
    return n if unit == "m" else (n * 60 if unit == "h" else n * 1440)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@tech_chronicle_bp.route("", methods=["GET"])
@tech_chronicle_bp.route("/", methods=["GET"])
def get_feed():
    """Merged feed. ?category = all|ai|cloud|devops|security|data|web|systems|career."""
    category = (request.args.get("category") or "all").lower().strip()
    try:
        news = _ensure_fresh("news")
        career = _ensure_fresh("career")
    except Exception as e:
        logger.error("tech-chronicle fetch failed: %s", e)
        news, career = _FALLBACK_NEWS, _FALLBACK_CAREER

    items = list(news) + list(career)
    if category and category != "all":
        if category == "career":
            items = [i for i in items if i.get("category") in {"trend", "tip", "stat"}]
        else:
            items = [i for i in items if i.get("category") == category]
    items.sort(key=lambda i: _parse_time_ago(i.get("timeAgo", "")))

    all_items = list(news) + list(career)
    tag_counts: dict = {}
    for it in all_items:
        for t in (it.get("tags") or []):
            if t:
                tag_counts[t] = tag_counts.get(t, 0) + 1
    trending_tags = sorted(tag_counts.keys(), key=lambda t: -tag_counts[t])[:6]

    _, news_ts = _load_cached("news")
    _, career_ts = _load_cached("career")
    timestamps = [t for t in (news_ts, career_ts) if t]
    generated_at = min(timestamps).isoformat() if timestamps else None

    return jsonify({"items": items, "trendingTags": trending_tags, "generatedAt": generated_at})


@tech_chronicle_bp.route("/regenerate", methods=["POST"])
@jwt_required()
def regenerate():
    """Force-refresh both caches. ADMIN ONLY — this was previously unauthenticated,
    a security/cost hole. The public feed self-refreshes on TTL expiry regardless."""
    email = get_jwt_identity()
    user = DBConnect().get_db().users.find_one({"email": email}, {"is_admin": 1}) or {}
    if not user.get("is_admin"):
        return jsonify({"error": "Admin only"}), 403

    _, news_ts = _load_cached("news")
    if news_ts and (datetime.now(timezone.utc) - news_ts) < timedelta(minutes=10):
        return jsonify({"error": "Cache is less than 10 minutes old"}), 429

    try:
        news = _generate_tech_news()
        news = [_normalize_source_link(it) for it in news] if news else _FALLBACK_NEWS
        _save_cache("news", news)
        career = _generate_career_intel() or _FALLBACK_CAREER
        _save_cache("career", career)
        return jsonify({"success": True, "news_count": len(news), "career_count": len(career),
                        "regenerated_at": datetime.now(timezone.utc).isoformat()})
    except Exception as e:
        logger.error("Regenerate failed: %s", e)
        return jsonify({"error": "Regeneration failed"}), 500
