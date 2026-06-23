"""Job-posting legitimacy / scam-shield engine — JobPilot's trust differentiator.

No mainstream job board flags ghost or scam postings, yet employment scams are
the #2 BBB risk for ages 18-34 ($501M FTC losses in 2024) and >40% of scams
targeting international students are fake job offers. This engine screens every
job we surface so students can tell a real opening from a trap.

Two tiers, mirroring the event-legitimacy engine:
  1. assess_job(job)  — DETERMINISTIC heuristics. Fast, free, no LLM. Runs inline
     on every search/pipeline result. Conservative by design: we only raise a
     flag on specific scam/ghost patterns so the signal stays trustworthy (a
     false "high risk" on a real Workday post would erode the whole feature).
  2. deep_scan(job)   — heuristics + a cheap FLASH classification, for an
     on-demand "deep scan this posting" action. Never gated behind a paywall —
     safety is free and on-brand.

Output (merged onto the job dict):
  trust_score  : 0-100  (100 = clearly legit, 0 = clearly scam/ghost)
  trust_band   : "trusted" | "caution" | "high_risk"
  trust_flags  : machine codes  e.g. ["asks_for_payment", "offplatform_contact"]
  trust_reasons: short human strings for the badge tooltip
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Tuple

logger = logging.getLogger(__name__)

# --- strong scam signals (any one caps the band at high_risk) ---------------
# Upfront money is the #1 tell — no legitimate employer charges you to work.
PAYMENT_PHRASES = (
    "registration fee", "training fee", "processing fee", "application fee",
    "certificate fee", "starter kit", "pay for equipment", "equipment fee",
    "security deposit", "onboarding fee", "pay ₹", "pay a fee", "buy your own",
    "purchase the software", "activation fee", "refundable deposit",
)
# Real hiring almost never moves to Telegram/WhatsApp before an offer.
MESSAGING_PHRASES = (
    "telegram", "whatsapp", "signal app", "text me at", "contact me on whatsapp",
    "via telegram", "message me on telegram", "hangouts",
)
# Financial-data harvesting before any offer.
FINANCIAL_HARVEST = (
    "bank account number", "routing number", "social security number",
    "credit card number", "ssn upfront", "direct deposit details before",
    "send your bank", "share your bank",
)
# Money-mule / reshipping schemes dressed as remote roles.
MULE_PHRASES = (
    "reshipping", "package forwarding", "payment processing agent",
    "money transfer agent", "receive and forward payments", "wire the funds",
    "process payments through your", "funds transfer",
)
# "Too good to be true" hiring claims.
GUARANTEE_PHRASES = (
    "guaranteed job", "100% placement", "no interview required", "hire on the spot",
    "guaranteed income", "get paid daily to start", "earn money fast",
    "be your own boss", "unlimited earning potential", "no experience no problem high pay",
)
# Ghost / evergreen posting language (soft — caution, never high_risk alone).
GHOST_PHRASES = (
    "always hiring", "we're always looking", "evergreen", "talent pool",
    "future openings", "building a pipeline", "for future consideration",
    "ongoing recruitment", "no specific opening",
)

PERSONAL_EMAIL = re.compile(
    r"[a-z0-9._%+-]+@(?:gmail|yahoo|hotmail|outlook|aol|protonmail|icloud|mail)\.com",
    re.IGNORECASE,
)
WEEKLY_PAY = re.compile(r"\$\s?([0-9]{3,5})\s?(?:/|per\s|a\s)?\s?(?:week|wk|weekly)", re.IGNORECASE)
HOURLY_PAY = re.compile(r"\$\s?([0-9]{2,4})\s?(?:/|per\s|an\s)?\s?(?:hour|hr|hourly)", re.IGNORECASE)

# Sources we fetch from are reputable boards/ATS: scams are rare there and
# their list-view rows are often legitimately terse, so we exempt them from the
# thin-description / undisclosed-company *soft* penalties (but NOT from hard
# scam-phrase detection).
TRUSTED_SOURCES = {
    "linkedin", "workday", "workday direct", "workday_direct", "ats direct",
    "ats_direct", "company careers", "greenhouse", "lever", "ashby", "google",
    "jobright", "jsearch",
}
TRUSTED_ATS_DOMAINS = (
    "greenhouse.io", "lever.co", "myworkdayjobs.com", "ashbyhq.com", "icims.com",
    "smartrecruiters.com", "jobvite.com", "workable.com", "bamboohr.com",
    "successfactors.com", "taleo.net", "oraclecloud.com/hcm",
)
STAFFING_MARKERS = ("staffing", "recruiting", "recruitment", "talent solutions", "consultancy services")


def _norm(s: Any) -> str:
    return str(s or "").strip()


def _has_any(blob: str, phrases) -> bool:
    return any(p in blob for p in phrases)


def assess_job(job: Dict[str, Any]) -> Dict[str, Any]:
    """Deterministic scam/ghost assessment for a single job dict. No LLM."""
    title = _norm(job.get("title"))
    company = _norm(job.get("company"))
    desc = _norm(job.get("description"))
    salary = _norm(job.get("salary"))
    source = _norm(job.get("source")).lower()
    apply_link = _norm(job.get("apply_link")).lower()
    blob = f"{title}\n{company}\n{desc}".lower()

    score = 70
    flags: List[str] = []
    reasons: List[str] = []
    scam = False
    trusted_src = source in TRUSTED_SOURCES or any(d in apply_link for d in TRUSTED_ATS_DOMAINS)

    # ---- hard scam signals (apply to everyone) ----
    if _has_any(blob, PAYMENT_PHRASES):
        scam = True; flags.append("asks_for_payment")
        reasons.append("Asks you to pay a fee or buy equipment — real employers never do")
    if _has_any(blob, MESSAGING_PHRASES):
        scam = True; flags.append("offplatform_contact")
        reasons.append("Pushes you to Telegram/WhatsApp — a classic recruiter-scam tell")
    if _has_any(blob, FINANCIAL_HARVEST):
        scam = True; flags.append("financial_harvest")
        reasons.append("Requests bank / SSN details before any offer")
    if _has_any(blob, MULE_PHRASES):
        scam = True; flags.append("money_mule")
        reasons.append("'Process/forward payments' work — a money-mule pattern")
    if _has_any(blob, GUARANTEE_PHRASES):
        scam = True; flags.append("too_good_to_be_true")
        reasons.append("'Guaranteed job' / 'no interview' / 'earn fast' claims")

    # Unrealistic pay for a no-experience remote role.
    no_exp = ("no experience" in blob or "no degree" in blob or "entry level" in blob)
    wk = WEEKLY_PAY.search(salary + " " + desc)
    big_weekly = bool(wk and int(wk.group(1)) >= 1500)
    if no_exp and big_weekly and ("remote" in blob or "work from home" in blob):
        scam = True; flags.append("unrealistic_pay")
        reasons.append("Unusually high pay for a no-experience work-from-home role")

    # ---- soft / ghost signals (caution only, never high_risk alone) ----
    soft = 0
    if _has_any(blob, GHOST_PHRASES):
        soft += 1; flags.append("evergreen_posting")
        reasons.append("'Always hiring' / talent-pool language — may be a ghost post")
    if not trusted_src:
        if company.lower() in ("", "unknown", "confidential", "n/a", "private"):
            soft += 1; flags.append("undisclosed_company")
            reasons.append("Employer name not disclosed")
        if len(desc) < 120:
            soft += 1; flags.append("thin_description")
            reasons.append("Very little detail about the actual role")
        if PERSONAL_EMAIL.search(desc):
            soft += 1; flags.append("personal_email")
            reasons.append("Contact is a personal email, not a company domain")
    if any(m in company.lower() for m in STAFFING_MARKERS):
        soft += 1; flags.append("staffing_agency")
        reasons.append("Posted by a staffing/recruiting agency (verify the end client)")

    # ---- positive signals ----
    if trusted_src:
        score += 12
    if len(desc) >= 400:
        score += 6
    if re.search(r"\$\s?[0-9]{2,3}[,k]", salary, re.IGNORECASE) or "-" in salary and "$" in salary:
        score += 4  # a concrete salary range reads as a real, compliant posting
    if company and company.lower() not in ("unknown", ""):
        score += 4

    score -= soft * 12
    if scam:
        score = min(score, 20)
    score = max(0, min(100, score))

    if scam or score < 35:
        band = "high_risk"
    elif score < 60 or soft >= 1:
        band = "caution"
    else:
        band = "trusted"

    return {
        "trust_score": score,
        "trust_band": band,
        "trust_flags": flags[:8],
        "trust_reasons": reasons[:5],
    }


def assess_batch(items: List[Dict[str, Any]]) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Any]]:
    """Score a batch of jobs by id. Mirrors jd_match_scorer.score_batch so the FE
    can overlay trust on already-rendered pipeline rows."""
    out: Dict[str, Dict[str, Any]] = {}
    flagged = 0
    for it in items or []:
        jid = str(it.get("id") or it.get("job_id") or it.get("url") or "")
        if not jid:
            continue
        res = assess_job(it)
        out[jid] = res
        if res["trust_band"] != "trusted":
            flagged += 1
    return out, {"scored": len(out), "flagged": flagged}


# ---------------------------------------------------------------------------
# Deep scan (on-demand): heuristics + a cheap FLASH second opinion.
# ---------------------------------------------------------------------------
def _llm_scan(job: Dict[str, Any]):
    from services.gemini_client import gemini_json, GEMINI_FLASH
    prompt = (
        "You are a job-scam analyst protecting a student / international job seeker — "
        "a prime target for employment fraud. Assess how SAFE and REAL this posting is. "
        "Penalize: upfront fees, pay/equipment purchases, off-platform contact "
        "(Telegram/WhatsApp), requests for bank/SSN details, money-forwarding 'roles', "
        "guaranteed-job claims, vague employers, and pay that's too high for the work. "
        "Do NOT penalize a terse description from a real company.\n\n"
        f"Title: {job.get('title')}\nCompany: {job.get('company')}\n"
        f"Location: {job.get('location')}\nSalary: {job.get('salary')}\n"
        f"Source: {job.get('source')}\nApply link: {job.get('apply_link')}\n"
        f"Description: {(_norm(job.get('description')))[:1500]}\n\n"
        'Return strict JSON: {"verdict": "trusted|caution|high_risk", '
        '"confidence": 0-100, "rationale": "one short sentence", '
        '"advice": "one specific, actionable safety tip for the applicant"}'
    )
    res = gemini_json(
        prompt, max_tokens=300, temperature=0.0, model=GEMINI_FLASH,
        schema={"verdict": str, "confidence": int, "rationale": str, "advice": str},
    )
    return res or None


_BAND_RANK = {"trusted": 0, "caution": 1, "high_risk": 2}


def deep_scan(job: Dict[str, Any], use_llm: bool = True) -> Dict[str, Any]:
    """On-demand deep scan: deterministic assessment + FLASH second opinion.

    The two are combined by taking the MORE CAUTIOUS band (safety-first), and a
    hard scam flag from the heuristics always wins."""
    base = assess_job(job)
    out = dict(base)
    if not use_llm:
        return out
    try:
        llm = _llm_scan(job)
    except Exception as e:
        logger.warning("job deep-scan LLM failed: %s", e)
        llm = None
    if llm:
        llm_band = str(llm.get("verdict") or "caution")
        if llm_band not in _BAND_RANK:
            llm_band = "caution"
        # Hard heuristic scam flag is sticky; otherwise take the more cautious band.
        if "asks_for_payment" in base["trust_flags"] or "money_mule" in base["trust_flags"] \
                or "offplatform_contact" in base["trust_flags"] or "financial_harvest" in base["trust_flags"]:
            final_band = "high_risk"
        else:
            final_band = base["trust_band"] if _BAND_RANK[base["trust_band"]] >= _BAND_RANK[llm_band] else llm_band
        out["trust_band"] = final_band
        out["llm_rationale"] = _norm(llm.get("rationale"))
        out["advice"] = _norm(llm.get("advice"))
        out["llm_confidence"] = int(llm.get("confidence") or 0)
    return out
