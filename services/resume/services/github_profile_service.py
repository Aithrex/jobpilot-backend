"""GitHub proof-of-skill — recognize what students actually BUILD.

Uses GitHub's PUBLIC REST API: a username is enough (no OAuth, no config), and
it's often already on the résumé as contact.github. We analyze public repos to
surface real, verifiable proof of skill — languages, shipped projects, stars,
recent activity — and a 0-100 proof_score that feeds the Career Readiness Score.

This is REAL data, not claims: squarely on-brand for an integrity-first product.
Snapshots are cached (github_profiles) so we only call GitHub on explicit
link/refresh — unauthenticated GitHub is 60 req/hr per IP, so set a GITHUB_TOKEN
later (Bearer) to raise it to 5000/hr if volume grows.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from typing import Dict, List, Optional

import requests

from utils.db_connect import DBConnect

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
HTTP_TIMEOUT = 12
UA = "JobPilot/1.0 (+https://jobpilot.app) profile-analyzer"
# GitHub login rules: 1-39 chars, alphanumeric or single hyphens, no edge hyphen.
USERNAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")


def _db():
    return DBConnect().get_db()


def _now():
    return datetime.now(timezone.utc)


def extract_username(raw: str) -> Optional[str]:
    """Accept a username or a github.com URL; return a valid login or None.

    Validating to GitHub's login charset also closes SSRF: the host is hardcoded
    to api.github.com and the only interpolated value is a charset-checked login.
    """
    s = (raw or "").strip()
    if not s:
        return None
    m = re.search(r"github\.com/([^/?#]+)", s, re.IGNORECASE)
    if m:
        s = m.group(1)
    s = s.lstrip("@").strip("/")
    return s if USERNAME_RE.match(s) else None


def _headers() -> Dict[str, str]:
    h = {"User-Agent": UA, "Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _get(url: str, params: Optional[Dict] = None):
    return requests.get(url, headers=_headers(), params=params, timeout=HTTP_TIMEOUT)


def _months_since(iso: str) -> Optional[float]:
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        return (_now() - dt).days / 30.0
    except Exception:
        return None


def analyze(raw_username: str) -> Dict:
    """Fetch + analyze a public GitHub profile. Raises ValueError on bad input /
    not found, RuntimeError on rate-limit or upstream failure."""
    login = extract_username(raw_username)
    if not login:
        raise ValueError("Enter a valid GitHub username or profile URL")

    pr = _get(f"{GITHUB_API}/users/{login}")
    if pr.status_code == 404:
        raise ValueError(f"GitHub user '{login}' not found")
    if pr.status_code in (403, 429):
        raise RuntimeError("GitHub rate limit reached — try again shortly")
    if pr.status_code != 200:
        raise RuntimeError("Couldn't reach GitHub")
    prof = pr.json()

    rr = _get(f"{GITHUB_API}/users/{login}/repos",
              params={"per_page": 100, "sort": "pushed", "type": "owner"})
    repos = rr.json() if rr.status_code == 200 else []
    if not isinstance(repos, list):
        repos = []

    originals = [r for r in repos if isinstance(r, dict) and not r.get("fork")]
    total_stars = sum(int(r.get("stargazers_count") or 0) for r in originals)
    lang_counts: Dict[str, int] = {}
    for r in originals:
        lang = r.get("language")
        if lang:
            lang_counts[lang] = lang_counts.get(lang, 0) + 1
    top_languages = [k for k, _ in sorted(lang_counts.items(), key=lambda kv: -kv[1])[:6]]

    push_months = [m for m in (_months_since(r.get("pushed_at")) for r in originals if r.get("pushed_at")) if m is not None]
    recent_months = min(push_months) if push_months else None
    active_recently = recent_months is not None and recent_months <= 3
    starred_3plus = sum(1 for r in originals if int(r.get("stargazers_count") or 0) >= 5)

    top_repos = [{
        "name": r.get("name"),
        "stars": int(r.get("stargazers_count") or 0),
        "language": r.get("language"),
        "description": (r.get("description") or "")[:160],
        "url": r.get("html_url"),
        "topics": (r.get("topics") or [])[:5],
    } for r in sorted(originals, key=lambda r: int(r.get("stargazers_count") or 0), reverse=True)[:5]]

    # proof score — reward real, shipped, recent public work.
    score = 0.0
    score += min(len(originals), 10) * 5      # up to 50 — building & shipping
    score += min(total_stars, 50) * 0.6       # up to 30 — social proof
    score += min(len(top_languages), 5) * 2   # up to 10 — breadth
    score += 10 if active_recently else 0      # 10 — currently active
    proof_score = max(0, min(100, int(round(score))))

    reasons: List[str] = []
    if originals:
        reasons.append(f"{len(originals)} public project(s) shipped")
    if top_languages:
        reasons.append("Top: " + ", ".join(top_languages[:3]))
    if total_stars:
        reasons.append(f"{total_stars} star(s) earned")
    if starred_3plus:
        reasons.append(f"{starred_3plus} project(s) with 5+ stars")
    if active_recently:
        reasons.append("Active in the last 3 months")
    elif recent_months is not None:
        reasons.append(f"Last active ~{int(recent_months)} month(s) ago")

    return {
        "username": login,
        "name": prof.get("name"),
        "avatar_url": prof.get("avatar_url"),
        "html_url": prof.get("html_url"),
        "bio": (prof.get("bio") or "")[:200],
        "public_repos": int(prof.get("public_repos") or 0),
        "followers": int(prof.get("followers") or 0),
        "original_repos": len(originals),
        "total_stars": total_stars,
        "top_languages": top_languages,
        "top_repos": top_repos,
        "active_recently": active_recently,
        "last_active_months": round(recent_months, 1) if recent_months is not None else None,
        "proof_score": proof_score,
        "reasons": reasons[:6],
    }


def link_and_store(user_email: str, raw_username: str) -> Dict:
    snap = analyze(raw_username)
    _db().github_profiles.update_one(
        {"user_email": user_email},
        {"$set": {**snap, "user_email": user_email, "fetched_at": _now()}},
        upsert=True,
    )
    return snap


def get_stored(user_email: str) -> Optional[Dict]:
    doc = _db().github_profiles.find_one({"user_email": user_email})
    if not doc:
        return None
    doc.pop("_id", None)
    fa = doc.get("fetched_at")
    if hasattr(fa, "isoformat"):
        doc["fetched_at"] = fa.isoformat()
    return doc


def proof_score_for(user_email: str) -> Optional[int]:
    """Cheap accessor for the readiness service. None = not linked."""
    doc = _db().github_profiles.find_one({"user_email": user_email}, {"proof_score": 1})
    return int(doc.get("proof_score") or 0) if doc else None
