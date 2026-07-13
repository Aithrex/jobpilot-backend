"""Events (Pillar C) — hackathon/webinar aggregation + the legitimacy engine.

Two parts:
  1. Best-effort scraping of PUBLIC sources (Devpost today; the structure makes
     adding MLH/Unstop/company pages mechanical). Network-dependent, so every
     fetch is wrapped — a source that's down just contributes nothing.
  2. The LEGITIMACY engine — our differentiator. 70% deterministic heuristics
     (source authority, concreteness, prize/cost integrity, recency) + 30% a
     cheap FLASH classification, with a hard floor on red flags (pay-to-win,
     "guaranteed job", missing organizer/URL). Surfaced as a 0-100 score + a
     band so users can trust what they put on their resume.

Storage: events (dedup_key unique) + scrape_runs (provenance). Scraping +
scoring run inside the async-job substrate (job_type 'event_scrape').
"""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional

import requests

from utils.db_connect import DBConnect

logger = logging.getLogger(__name__)

HTTP_TIMEOUT = 15
UA = "JobPilot/1.0 (+https://jobpilot.app) events-aggregator"

TRUSTED_SOURCES = {"devpost", "mlh", "unstop", "hackerearth", "confstech"}
SUSPICIOUS_PHRASES = [
    "guaranteed job", "100% placement", "pay to win", "registration fee",
    "entry fee", "certificate fee", "pay ₹", "paid certificate", "earn money fast",
]


def _db():
    return DBConnect().get_db()


def _now():
    return datetime.now(timezone.utc)


def _iso(v):
    if not hasattr(v, "isoformat"):
        return v
    s = v.isoformat()
    return s if s.endswith("Z") or "+" in s else s + "Z"


def _norm_url(url: str) -> str:
    u = (url or "").strip().lower()
    u = re.sub(r"^https?://(www\.)?", "", u)
    return u.rstrip("/")


def dedup_key(ev: Dict) -> str:
    u = _norm_url(ev.get("url", ""))
    if u:
        return "u:" + hashlib.sha1(u.encode()).hexdigest()[:24]
    basis = f"{ev.get('title','')}|{ev.get('org','')}|{ev.get('start_date','')}".lower()
    return "t:" + hashlib.sha1(basis.encode()).hexdigest()[:24]


# ---------------------------------------------------------------------------
# Legitimacy engine
# ---------------------------------------------------------------------------
def _heuristic_score(ev: Dict):
    score = 50
    signals: Dict = {}
    red: List[str] = []

    src = (ev.get("source") or "").lower()
    if src in TRUSTED_SOURCES:
        score += 20
        signals["trusted_source"] = True

    if ev.get("url"):
        score += 8
    else:
        red.append("no_url")

    if ev.get("org"):
        score += 6
    else:
        red.append("no_organizer")

    desc = ev.get("description") or ""
    if len(desc) >= 120:
        score += 6

    if ev.get("start_date") or ev.get("end_date"):
        score += 6
    else:
        score -= 6
        signals["no_dates"] = True

    text = f"{ev.get('title','')} {desc}".lower()
    if any(p in text for p in SUSPICIOUS_PHRASES):
        red.append("suspicious_claims")
        score -= 25

    # Pay-to-win: a participation fee paired with a cash prize is a classic
    # low-signal pattern.
    if ev.get("entry_fee") and ev.get("prize_pool"):
        red.append("fee_with_prize")
        score -= 20

    return max(0, min(100, score)), signals, red


def _band_from_score(score: int) -> str:
    if score >= 80:
        return "verified"
    if score >= 60:
        return "credible"
    if score >= 40:
        return "unvetted"
    return "flagged"


def _llm_legitimacy(ev: Dict):
    from services.claude_client import claude_json, CLAUDE_FLASH
    prompt = (
        "Rate how LEGITIMATE and worthwhile this event looks for a serious job seeker to attend "
        "and put on a resume. Penalize spam, pay-to-win, vague organizers, and 'guaranteed job' claims.\n\n"
        f"Title: {ev.get('title')}\nOrganizer: {ev.get('org')}\nType: {ev.get('type')}\n"
        f"Source: {ev.get('source')}\nDates: {ev.get('start_date')} - {ev.get('end_date')}\n"
        f"Prize: {ev.get('prize_pool')}\nDescription: {(ev.get('description') or '')[:600]}\n\n"
        'Return strict JSON: {"score": 0-100, "rationale": "one short sentence"}'
    )
    res = claude_json(prompt, max_tokens=200, temperature=0.0, model=CLAUDE_FLASH,
                      schema={"score": int, "rationale": str})
    if not res:
        return None, None
    return int(res.get("score", 0)), res.get("rationale")


def score_legitimacy(ev: Dict, use_llm: bool = True) -> Dict:
    heuristic, signals, red = _heuristic_score(ev)
    llm_score, rationale = (None, None)
    if use_llm:
        try:
            llm_score, rationale = _llm_legitimacy(ev)
        except Exception as e:
            logger.warning("legitimacy LLM failed: %s", e)
    final = round(0.7 * heuristic + 0.3 * llm_score) if isinstance(llm_score, int) else heuristic
    # Hard floor: any red flag caps the band at 'flagged'.
    band = "flagged" if red else _band_from_score(final)
    return {
        "legitimacy_score": final,
        "legitimacy_band": band,
        "vetting_signals": {
            "heuristic": heuristic,
            "llm": llm_score,
            "red_flags": red,
            "llm_rationale": rationale,
            **signals,
        },
    }


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------
_MONTHS = "Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec"


def _parse_devpost_range(raw: Optional[str]) -> tuple:
    """Best-effort parse of Devpost's human date range ("May 19 - Aug 17, 2026")
    into (start_iso, end_iso). Returns (None, None) if it can't be read."""
    try:
        raw = (raw or "").strip()
        if not raw:
            return (None, None)
        ym = re.search(r"\b(20\d{2})\b", raw)
        year = ym.group(1) if ym else None

        def _one(s: str, borrow_month: Optional[str] = None) -> Optional[str]:
            s = s.strip().rstrip(",").strip()
            # "Jul 09 - 10, 2026": the end ("10") has no month — borrow the start's.
            if borrow_month and not re.match(rf"^({_MONTHS})", s, re.I):
                s = f"{borrow_month} {s}"
            if not re.search(r"\b20\d{2}\b", s) and year:
                s = f"{s}, {year}"
            for fmt in ("%b %d, %Y", "%B %d, %Y"):
                try:
                    return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
                except ValueError:
                    continue
            return None

        parts = [p.strip() for p in raw.split(" - ")]
        if len(parts) == 2:
            m = re.match(rf"^({_MONTHS})", parts[0], re.I)
            start_month = m.group(1) if m else None
            return (_one(parts[0]), _one(parts[1], borrow_month=start_month))
        if len(parts) == 1:
            d = _one(parts[0])
            return (d, d)
    except Exception:
        pass
    return (None, None)


def scrape_devpost(limit: int = 40) -> List[Dict]:
    """Fetch open hackathons from Devpost's public listing API."""
    out: List[Dict] = []
    try:
        resp = requests.get(
            "https://devpost.com/api/hackathons",
            params={"order_by": "deadline", "status[]": "open"},
            headers={"User-Agent": UA, "Accept": "application/json"},
            timeout=HTTP_TIMEOUT,
        )
        if resp.status_code != 200:
            logger.warning("devpost fetch %s", resp.status_code)
            return out
        data = resp.json()
        for h in (data.get("hackathons") or [])[:limit]:
            themes = [t.get("name") for t in (h.get("themes") or []) if t.get("name")]
            prize = h.get("prize_amount")
            if isinstance(prize, str):
                prize = re.sub(r"<[^>]+>", "", prize).strip()
            _start_iso, _end_iso = _parse_devpost_range(h.get("submission_period_dates"))
            out.append({
                "source": "devpost",
                "external_id": str(h.get("id") or ""),
                "type": "hackathon",
                "title": h.get("title"),
                "org": h.get("organization_name"),
                "description": re.sub(r"<[^>]+>", " ", h.get("tagline") or "")[:1000],
                "url": h.get("url"),
                # Keep the human range for display; add parsed end + deadline.
                "start_date": (h.get("submission_period_dates") or ""),
                "end_date": _end_iso,
                "deadline": _end_iso,
                "tags": themes,
                "location": (h.get("displayed_location") or {}).get("location"),
                "prize_pool": prize,
                # NOTE: leave entry_fee unset — the legitimacy engine treats a
                # truthy entry_fee as a REAL fee (fee+prize = pay-to-win flag).
                "thumbnail": h.get("thumbnail_url"),
            })
    except Exception as e:
        logger.warning("devpost scrape error: %s", e)
    return out


# confs.tech open dataset topics (github tech-conferences/conference-data).
# A missing topic file just 404s and contributes nothing.
_CONFSTECH_TOPICS = [
    "general", "javascript", "python", "data", "devops",
    "security", "golang", "rust", "java", "ux", "ai",
]


def scrape_confstech(limit: int = 40) -> List[Dict]:
    """Fetch upcoming ONLINE tech events from the free confs.tech open dataset,
    surfaced as webinars/virtual events. In-person and past events are skipped.
    Free + keyless (raw GitHub JSON); a source that's down contributes nothing."""
    from concurrent.futures import ThreadPoolExecutor

    out: List[Dict] = []
    seen: set = set()
    today = _now().strftime("%Y-%m-%d")
    years = [str(_now().year), str(_now().year + 1)]
    base = "https://raw.githubusercontent.com/tech-conferences/conference-data/main/conferences"
    combos = [(y, t) for y in years for t in _CONFSTECH_TOPICS]

    def _fetch(yt):
        y, t = yt
        try:
            resp = requests.get(
                f"{base}/{y}/{t}.json",
                headers={"User-Agent": UA, "Accept": "application/json"},
                timeout=10,
            )
            return (t, resp.json() or []) if resp.status_code == 200 else (t, [])
        except Exception:
            return (t, [])

    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(_fetch, combos))
    except Exception as e:
        logger.warning("confstech scrape error: %s", e)
        return out

    for topic, items in results:
        for c in (items or []):
            if not c.get("online"):
                continue
            url = (c.get("url") or "").strip()
            start = (c.get("startDate") or "").strip()  # ISO — lexicographic compare is valid
            if not url or not c.get("name") or not start or start < today or url in seen:
                continue
            seen.add(url)
            # Organizer proxy from the event's own domain (rustconf.com …) —
            # confs.tech has no organizer field, and a blank org red-flags it.
            from urllib.parse import urlparse
            host = (urlparse(url).hostname or "").lower()
            org = host[4:] if host.startswith("www.") else host
            out.append({
                "source": "confstech",
                "external_id": url,
                "type": "webinar",
                "title": c.get("name"),
                "org": org or None,
                "description": f"Online tech event ({topic}).",
                "url": url,
                "start_date": start,
                "end_date": (c.get("endDate") or start),
                "tags": [topic],
                "location": "Online",
                "prize_pool": None,
                "thumbnail": None,
            })
    out.sort(key=lambda e: e.get("start_date") or "")
    return out[:limit]


SOURCES = {"devpost": scrape_devpost, "confstech": scrape_confstech}


# ---------------------------------------------------------------------------
# Upsert + job entry
# ---------------------------------------------------------------------------
def _upsert_event(ev: Dict, use_llm: bool = True) -> str:
    import uuid
    key = dedup_key(ev)
    legit = score_legitimacy(ev, use_llm=use_llm)
    doc = {
        **ev,
        **legit,
        "dedup_key": key,
        "status": "active",
        "scraped_at": _now(),
        "expires_at": _now() + timedelta(days=120),
    }
    _db().events.update_one(
        {"dedup_key": key},
        {"$set": doc, "$setOnInsert": {"event_id": str(uuid.uuid4()), "first_seen": _now()}},
        upsert=True,
    )
    return key


def run_scrape(payload: Dict) -> Dict:
    """Job entry (job_type='event_scrape'): pull sources, score, upsert."""
    sources = payload.get("sources") or list(SOURCES.keys())
    limit = int(payload.get("limit") or 40)
    use_llm = bool(payload.get("use_llm", True))
    total = 0
    by_source: Dict[str, int] = {}
    for s in sources:
        fn = SOURCES.get(s)
        if not fn:
            continue
        events = fn(limit)
        for ev in events:
            if ev.get("title"):
                _upsert_event(ev, use_llm=use_llm)
                total += 1
        by_source[s] = len(events)
    _db().scrape_runs.insert_one({
        "ran_at": _now(), "sources": sources, "by_source": by_source, "total": total,
    })
    return {"upserted": total, "by_source": by_source}


# ---------------------------------------------------------------------------
# Reads + relations
# ---------------------------------------------------------------------------
def _serialize(ev: Dict) -> Dict:
    ev = dict(ev)
    ev.pop("_id", None)
    for k in ("scraped_at", "first_seen", "expires_at"):
        ev.pop(k, None)
    return ev


# Bands a tier is allowed to see fully. Flagged is always hidden; unvetted is
# collapsed for free (returned with a flag the UI uses to collapse).
def list_events(user_email: str, type_filter: Optional[str] = None,
                include_unvetted: bool = True, limit: int = 60) -> List[Dict]:
    q: Dict = {"status": "active", "legitimacy_band": {"$ne": "flagged"}}
    if type_filter:
        q["type"] = type_filter
    if not include_unvetted:
        q["legitimacy_band"] = {"$in": ["verified", "credible"]}
    events = list(_db().events.find(q).sort("legitimacy_score", -1).limit(limit))
    # annotate with the user's relation
    rels = {r["event_id"]: r for r in _db().user_events.find({"user_email": user_email})}
    out = []
    for e in events:
        s = _serialize(e)
        rel = rels.get(e.get("event_id"))
        s["relation"] = rel.get("relation") if rel else None
        out.append(s)
    return out


def get_event(user_email: str, event_id: str) -> Optional[Dict]:
    e = _db().events.find_one({"event_id": event_id})
    if not e:
        return None
    s = _serialize(e)
    rel = _db().user_events.find_one({"user_email": user_email, "event_id": event_id})
    s["relation"] = rel.get("relation") if rel else None
    s["outcome"] = rel.get("outcome") if rel else None
    return s


VALID_RELATIONS = {"saved", "registered", "attended", "won", "dismissed"}


def set_relation(user_email: str, event_id: str, relation: str,
                 outcome: str = "", notes: str = "") -> Dict:
    if relation not in VALID_RELATIONS:
        raise ValueError("invalid relation")
    if not _db().events.find_one({"event_id": event_id}, {"_id": 1}):
        raise ValueError("event not found")
    _db().user_events.update_one(
        {"user_email": user_email, "event_id": event_id},
        {"$set": {"relation": relation, "outcome": outcome[:500], "notes": notes[:1000], "updated_at": _now()},
         "$setOnInsert": {"user_email": user_email, "event_id": event_id, "created_at": _now()}},
        upsert=True,
    )
    return {"event_id": event_id, "relation": relation}
