"""Answer library — the self-growing Q&A store behind auto-apply form filling.

Auto-Apply Tranche 1 §3 support surface. Job application forms ask the same
~200 screening questions in a thousand phrasings ("Are you legally authorized to
work in the US?", "Do you have US work authorization?", "Will you now or in the
future require sponsorship?"). This service turns that chaos into three cheap,
deterministic operations:

  * resolve(user_email, questions[])  — for each question, find an answer from
    (1) a GLOBAL seed bank of canonical screening-question -> profile-field
    mappings, then (2) the user's own remembered answers, else mark it
    unresolved (escalate) or, for essays, flag it for grounded generation.
  * remember(user_email, question, answer) — persist a user's new answer keyed
    by the NORMALIZED question so it is never asked twice.

--------------------------------------------------------------------------------
Matching strategy — WHY normalized-key + token-overlap, NOT embeddings.
--------------------------------------------------------------------------------
We deliberately do NOT call an embedding model here. Reasons:
  * Screening questions are short, formulaic, and drawn from a small, stable
    universe — a normalized key plus a signal-keyword classifier plus a
    Jaccard token-overlap fallback against the seed bank matches them robustly.
  * An embedding call per question is a per-application Bedrock spend that would
    have to be entitlement-gated and adds latency to the gather phase — a bad
    trade for a lookup that a few hundred bytes of rules solve deterministically.
  * Determinism matters: the SAME question must always resolve to the SAME field
    (a legal attestation especially must never drift), and this mirrors the
    LLM-free posture of jd_match_scorer / keyword_gap_engine.
If a future need for true semantic matching appears, `_categorize` is the single
seam to swap; nothing else changes.

--------------------------------------------------------------------------------
NEVER-FABRICATE (IntegrityGuard, extended to form answers).
--------------------------------------------------------------------------------
  * LEGAL ATTESTATIONS (work authorization, sponsorship, visa status, EEO) are
    filled ONLY from the user's DECLARED onboarding / visa_profiles / preferences
    data. If the user has not declared it, the question is returned UNRESOLVED
    with kind "legal_attestation" so autonomy_service routes the application to
    "ask" — we never guess a legal answer.
  * ESSAYS ("why us", cover-letter, "tell us about a project") are NOT answered
    here. They are flagged `needs_generation` so the grounded, IntegrityGuard-
    checked answer generator writes them from real résumé facts.
  * Factual metrics we don't have (years-of-X we never measured) are left
    unresolved for a one-tap human answer — never computed and asserted.

--------------------------------------------------------------------------------
Storage.
--------------------------------------------------------------------------------
Canonical remembered answers live in a NEW lean collection
`application_answer_library`, keyed unique on (user_email, norm_key). This does
NOT duplicate the existing `application_answers` collection: that one is the
per-company history of GENERATED ESSAY VARIANTS (question + variants + company),
a different shape and purpose. `resolve()` still reuses `application_answers`
opportunistically to surface a prior essay draft for a "why us"-style question.
Nothing here calls an LLM; entitlement/quota gating happens in the route.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

from utils.db_connect import DBConnect
from utils.security import InputSanitizer

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------
CAT_WORK_AUTH = "work_authorization"
CAT_SPONSORSHIP = "sponsorship"
CAT_VISA_STATUS = "visa_status"
CAT_EEO = "eeo"
CAT_SALARY = "salary"
CAT_NOTICE = "notice_period"
CAT_RELOCATION = "relocation"
CAT_START_DATE = "start_date"
CAT_CONTACT = "contact"
CAT_LINKS = "links"
CAT_YEARS = "years_of_experience"
CAT_ESSAY = "essay"

# Legal/EEO attestations — NEVER auto-filled from anything but declared data,
# and NEVER generated. An unresolved one forces autonomy -> "ask".
LEGAL_CATEGORIES = frozenset({CAT_WORK_AUTH, CAT_SPONSORSHIP, CAT_VISA_STATUS, CAT_EEO})

# Kinds — the shape of the expected answer (drives the review UI + validation).
KIND_BOOLEAN = "boolean"
KIND_VALUE = "value"
KIND_YEARS = "years"
KIND_ESSAY = "essay"
KIND_EEO = "eeo"
KIND_LEGAL = "legal_attestation"   # unresolved legal → escalate to "ask"
KIND_NOVEL = "novel"               # unseen non-essay screening question

# Bound how many questions one resolve() call will process (a form recon should
# never hand us thousands; cap defends the per-user store scan + response size).
_MAX_QUESTIONS = 200

# Token-overlap acceptance threshold for the seed-bank fallback match.
_JACCARD_ACCEPT = 0.5
# Token-overlap acceptance for the per-user remembered-answer fuzzy match — a
# little stricter (a user's own answers must match closely to be reused).
_USER_STORE_JACCARD_ACCEPT = 0.6
# Cap the per-user fuzzy scan so a large history never turns into a huge read.
_USER_STORE_SCAN_LIMIT = 300

_STOPWORDS = frozenset({
    "a", "an", "the", "is", "are", "do", "does", "did", "you", "your", "yours",
    "will", "would", "can", "could", "have", "has", "had", "to", "of", "in",
    "on", "for", "and", "or", "with", "any", "please", "we", "us", "our", "this",
    "that", "be", "been", "if", "as", "at", "by", "it", "what", "which", "how",
    "when", "where", "who", "whom", "there", "their", "may", "must", "am", "i",
    "me", "my", "currently", "current", "now", "future", "ever", "yes", "no",
})


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------
# Field-id / marker noise that carries no meaning: "(required)", trailing "*",
# "question_12345", "q1", bracketed hints.
_MARKER_PATTERN = re.compile(
    r"\(required\)|\(optional\)|required|optional|\*", re.IGNORECASE
)
_ID_TOKEN = re.compile(r"^[a-z]?\d+[a-z0-9]*$")   # "12345", "q1", "field3a"


def _normalize(text: str) -> Tuple[str, FrozenSet[str]]:
    """Return (norm_key, content_tokens) for a raw question string.

    norm_key: lowercased, marker-stripped, punctuation-collapsed, single-spaced
    — the exact key we store/look up per user. content_tokens: norm_key minus
    stopwords and id-shaped tokens — the basis for token-overlap matching.
    """
    s = (text or "").lower().strip()
    s = _MARKER_PATTERN.sub(" ", s)
    s = re.sub(r"[\(\[\{].*?[\)\]\}]", " ", s)     # bracketed hints
    s = re.sub(r"[^a-z0-9+#/ ]+", " ", s)          # keep +,#,/ (c++, c#, and/or)
    s = re.sub(r"\s+", " ", s).strip()
    tokens = frozenset(
        t for t in s.split()
        if t and t not in _STOPWORDS and not _ID_TOKEN.match(t)
    )
    return s, tokens


def _jaccard(a: FrozenSet[str], b: FrozenSet[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if not inter:
        return 0.0
    return inter / len(a | b)


# ---------------------------------------------------------------------------
# GLOBAL seed bank — canonical screening questions -> (field, category, kind).
# ~180 phrasings. Two jobs: (1) document the canonical mapping rules, (2) provide
# the token-overlap fallback corpus. The primary classifier is signal-keyword
# based (_categorize) — robust to phrasings NOT literally in this list.
# Entries: (question_text, field, category, kind)
# ---------------------------------------------------------------------------
SEED_QUESTIONS: List[Tuple[str, str, str, str]] = [
    # --- WORK AUTHORIZATION (legal) ------------------------------------------
    ("Are you legally authorized to work in the United States?", "work_authorized_us", CAT_WORK_AUTH, KIND_BOOLEAN),
    ("Are you authorized to work in the US?", "work_authorized_us", CAT_WORK_AUTH, KIND_BOOLEAN),
    ("Do you have US work authorization?", "work_authorized_us", CAT_WORK_AUTH, KIND_BOOLEAN),
    ("Are you eligible to work in the United States?", "work_authorized_us", CAT_WORK_AUTH, KIND_BOOLEAN),
    ("Do you have the legal right to work in the country of this position?", "work_authorized_us", CAT_WORK_AUTH, KIND_BOOLEAN),
    ("Are you legally permitted to work in the US?", "work_authorized_us", CAT_WORK_AUTH, KIND_BOOLEAN),
    ("Can you provide proof of your eligibility to work in the United States?", "work_authorized_us", CAT_WORK_AUTH, KIND_BOOLEAN),
    ("Are you authorized to work lawfully in the United States for our company?", "work_authorized_us", CAT_WORK_AUTH, KIND_BOOLEAN),
    ("Do you currently have authorization to work in the US?", "work_authorized_us", CAT_WORK_AUTH, KIND_BOOLEAN),
    ("Are you legally authorized to work in the country where this job is located?", "work_authorized_us", CAT_WORK_AUTH, KIND_BOOLEAN),
    ("Will you be able to provide documentation of your right to work?", "work_authorized_us", CAT_WORK_AUTH, KIND_BOOLEAN),
    ("Are you authorized to work for any employer in the US?", "work_authorized_us", CAT_WORK_AUTH, KIND_BOOLEAN),

    # --- SPONSORSHIP (legal) -------------------------------------------------
    ("Will you now or in the future require visa sponsorship?", "requires_sponsorship", CAT_SPONSORSHIP, KIND_BOOLEAN),
    ("Do you require sponsorship for employment visa status?", "requires_sponsorship", CAT_SPONSORSHIP, KIND_BOOLEAN),
    ("Will you require sponsorship now or in the future?", "requires_sponsorship", CAT_SPONSORSHIP, KIND_BOOLEAN),
    ("Do you need visa sponsorship to work in the United States?", "requires_sponsorship", CAT_SPONSORSHIP, KIND_BOOLEAN),
    ("Would you require the company to sponsor a work visa?", "requires_sponsorship", CAT_SPONSORSHIP, KIND_BOOLEAN),
    ("Are you able to work without requiring sponsorship?", "requires_sponsorship", CAT_SPONSORSHIP, KIND_BOOLEAN),
    ("Do you now or will you in the future need immigration sponsorship?", "requires_sponsorship", CAT_SPONSORSHIP, KIND_BOOLEAN),
    ("Will you require H-1B or other work visa sponsorship?", "requires_sponsorship", CAT_SPONSORSHIP, KIND_BOOLEAN),
    ("Do you require sponsorship to continue working in the US?", "requires_sponsorship", CAT_SPONSORSHIP, KIND_BOOLEAN),
    ("Will you need visa sponsorship at any point during employment?", "requires_sponsorship", CAT_SPONSORSHIP, KIND_BOOLEAN),
    ("Are you currently on a visa that requires employer sponsorship?", "requires_sponsorship", CAT_SPONSORSHIP, KIND_BOOLEAN),
    ("Does your employment require us to file a visa petition on your behalf?", "requires_sponsorship", CAT_SPONSORSHIP, KIND_BOOLEAN),

    # --- VISA STATUS (legal) -------------------------------------------------
    ("What is your current visa status?", "visa_status", CAT_VISA_STATUS, KIND_VALUE),
    ("What is your current work authorization status?", "visa_status", CAT_VISA_STATUS, KIND_VALUE),
    ("What is your immigration status?", "visa_status", CAT_VISA_STATUS, KIND_VALUE),
    ("Which of the following describes your visa status?", "visa_status", CAT_VISA_STATUS, KIND_VALUE),
    ("What type of work authorization do you hold?", "visa_status", CAT_VISA_STATUS, KIND_VALUE),
    ("What is your citizenship or work authorization status?", "visa_status", CAT_VISA_STATUS, KIND_VALUE),

    # --- EEO (legal / voluntary self-ID) ------------------------------------
    ("What is your gender?", "eeo_gender", CAT_EEO, KIND_EEO),
    ("Please select your gender identity.", "eeo_gender", CAT_EEO, KIND_EEO),
    ("How do you describe your gender?", "eeo_gender", CAT_EEO, KIND_EEO),
    ("What is your race or ethnicity?", "eeo_race", CAT_EEO, KIND_EEO),
    ("Please identify your race/ethnicity.", "eeo_race", CAT_EEO, KIND_EEO),
    ("Are you Hispanic or Latino?", "eeo_race", CAT_EEO, KIND_EEO),
    ("What is your ethnic background?", "eeo_race", CAT_EEO, KIND_EEO),
    ("Are you a protected veteran?", "eeo_veteran", CAT_EEO, KIND_EEO),
    ("What is your veteran status?", "eeo_veteran", CAT_EEO, KIND_EEO),
    ("Have you served in the military?", "eeo_veteran", CAT_EEO, KIND_EEO),
    ("Do you have a disability?", "eeo_disability", CAT_EEO, KIND_EEO),
    ("What is your disability status?", "eeo_disability", CAT_EEO, KIND_EEO),
    ("Do you identify as a person with a disability?", "eeo_disability", CAT_EEO, KIND_EEO),

    # --- SALARY --------------------------------------------------------------
    ("What are your salary expectations?", "desired_salary", CAT_SALARY, KIND_VALUE),
    ("What is your desired salary?", "desired_salary", CAT_SALARY, KIND_VALUE),
    ("What is your expected compensation?", "desired_salary", CAT_SALARY, KIND_VALUE),
    ("What are your compensation requirements?", "desired_salary", CAT_SALARY, KIND_VALUE),
    ("What salary range are you looking for?", "desired_salary", CAT_SALARY, KIND_VALUE),
    ("What is your expected base salary?", "desired_salary", CAT_SALARY, KIND_VALUE),
    ("What are your pay expectations for this role?", "desired_salary", CAT_SALARY, KIND_VALUE),
    ("What is your target compensation?", "desired_salary", CAT_SALARY, KIND_VALUE),
    ("What is your current salary?", "desired_salary", CAT_SALARY, KIND_VALUE),
    ("What is your minimum acceptable salary?", "desired_salary", CAT_SALARY, KIND_VALUE),
    ("What are your total compensation expectations?", "desired_salary", CAT_SALARY, KIND_VALUE),
    ("What hourly rate are you seeking?", "desired_salary", CAT_SALARY, KIND_VALUE),
    ("What is your expected CTC?", "desired_salary", CAT_SALARY, KIND_VALUE),

    # --- NOTICE PERIOD -------------------------------------------------------
    ("What is your notice period?", "notice_period", CAT_NOTICE, KIND_VALUE),
    ("How much notice do you need to give your current employer?", "notice_period", CAT_NOTICE, KIND_VALUE),
    ("What is your current notice period with your employer?", "notice_period", CAT_NOTICE, KIND_VALUE),
    ("How long is your notice period?", "notice_period", CAT_NOTICE, KIND_VALUE),
    ("What notice period are you required to serve?", "notice_period", CAT_NOTICE, KIND_VALUE),

    # --- START DATE / AVAILABILITY -------------------------------------------
    ("When can you start?", "available_start_date", CAT_START_DATE, KIND_VALUE),
    ("What is your earliest available start date?", "available_start_date", CAT_START_DATE, KIND_VALUE),
    ("When are you available to begin work?", "available_start_date", CAT_START_DATE, KIND_VALUE),
    ("What is your availability to start?", "available_start_date", CAT_START_DATE, KIND_VALUE),
    ("How soon can you join?", "available_start_date", CAT_START_DATE, KIND_VALUE),
    ("What date can you commence employment?", "available_start_date", CAT_START_DATE, KIND_VALUE),
    ("When would you be able to start this position?", "available_start_date", CAT_START_DATE, KIND_VALUE),

    # --- RELOCATION ----------------------------------------------------------
    ("Are you willing to relocate?", "willing_to_relocate", CAT_RELOCATION, KIND_BOOLEAN),
    ("Are you open to relocation for this role?", "willing_to_relocate", CAT_RELOCATION, KIND_BOOLEAN),
    ("Would you be willing to relocate for this position?", "willing_to_relocate", CAT_RELOCATION, KIND_BOOLEAN),
    ("Are you able to relocate to the job location?", "willing_to_relocate", CAT_RELOCATION, KIND_BOOLEAN),
    ("Are you willing to move to another city for work?", "willing_to_relocate", CAT_RELOCATION, KIND_BOOLEAN),

    # --- CONTACT -------------------------------------------------------------
    ("What is your full name?", "full_name", CAT_CONTACT, KIND_VALUE),
    ("What is your first name?", "first_name", CAT_CONTACT, KIND_VALUE),
    ("What is your last name?", "last_name", CAT_CONTACT, KIND_VALUE),
    ("What is your legal name?", "full_name", CAT_CONTACT, KIND_VALUE),
    ("What is your email address?", "email", CAT_CONTACT, KIND_VALUE),
    ("What is your phone number?", "phone", CAT_CONTACT, KIND_VALUE),
    ("What is your mobile number?", "phone", CAT_CONTACT, KIND_VALUE),
    ("What is your current location?", "location", CAT_CONTACT, KIND_VALUE),
    ("Where are you located?", "location", CAT_CONTACT, KIND_VALUE),
    ("What city do you live in?", "location", CAT_CONTACT, KIND_VALUE),
    ("What is your current city and state?", "location", CAT_CONTACT, KIND_VALUE),
    ("What is your address?", "location", CAT_CONTACT, KIND_VALUE),

    # --- LINKS ---------------------------------------------------------------
    ("What is your LinkedIn profile URL?", "linkedin_url", CAT_LINKS, KIND_VALUE),
    ("Please provide your LinkedIn profile.", "linkedin_url", CAT_LINKS, KIND_VALUE),
    ("What is your GitHub profile URL?", "github_url", CAT_LINKS, KIND_VALUE),
    ("Please share a link to your GitHub.", "github_url", CAT_LINKS, KIND_VALUE),
    ("What is your portfolio website?", "portfolio_url", CAT_LINKS, KIND_VALUE),
    ("Do you have a personal website or portfolio?", "portfolio_url", CAT_LINKS, KIND_VALUE),
    ("Please provide a link to your website.", "portfolio_url", CAT_LINKS, KIND_VALUE),

    # --- YEARS OF EXPERIENCE (one-tap; never fabricated) ---------------------
    ("How many years of experience do you have?", "years_experience", CAT_YEARS, KIND_YEARS),
    ("How many years of professional experience do you have?", "years_experience", CAT_YEARS, KIND_YEARS),
    ("How many years of experience do you have with Python?", "years_experience", CAT_YEARS, KIND_YEARS),
    ("How many years of experience do you have in software engineering?", "years_experience", CAT_YEARS, KIND_YEARS),
    ("How many years have you worked with JavaScript?", "years_experience", CAT_YEARS, KIND_YEARS),
    ("How many years of experience do you have with React?", "years_experience", CAT_YEARS, KIND_YEARS),
    ("How many years of experience do you have with AWS?", "years_experience", CAT_YEARS, KIND_YEARS),
    ("How many years of management experience do you have?", "years_experience", CAT_YEARS, KIND_YEARS),
    ("How many years of experience do you have with SQL?", "years_experience", CAT_YEARS, KIND_YEARS),
    ("How many years of relevant experience do you have?", "years_experience", CAT_YEARS, KIND_YEARS),
    ("Years of experience with Java?", "years_experience", CAT_YEARS, KIND_YEARS),
    ("How many years of experience do you have leading teams?", "years_experience", CAT_YEARS, KIND_YEARS),

    # --- ESSAYS (grounded generation, never fabricated here) -----------------
    ("Why do you want to work here?", "essay", CAT_ESSAY, KIND_ESSAY),
    ("Why do you want to work for us?", "essay", CAT_ESSAY, KIND_ESSAY),
    ("Why are you interested in this role?", "essay", CAT_ESSAY, KIND_ESSAY),
    ("Why are you interested in this position?", "essay", CAT_ESSAY, KIND_ESSAY),
    ("Why do you want to join our company?", "essay", CAT_ESSAY, KIND_ESSAY),
    ("What interests you about this opportunity?", "essay", CAT_ESSAY, KIND_ESSAY),
    ("What excites you about this role?", "essay", CAT_ESSAY, KIND_ESSAY),
    ("Tell us about yourself.", "essay", CAT_ESSAY, KIND_ESSAY),
    ("Tell us about a project you are proud of.", "essay", CAT_ESSAY, KIND_ESSAY),
    ("Describe a challenging problem you solved.", "essay", CAT_ESSAY, KIND_ESSAY),
    ("Tell us about a project you are excited about.", "essay", CAT_ESSAY, KIND_ESSAY),
    ("What makes you a good fit for this position?", "essay", CAT_ESSAY, KIND_ESSAY),
    ("Why should we hire you?", "essay", CAT_ESSAY, KIND_ESSAY),
    ("Please provide a cover letter.", "essay", CAT_ESSAY, KIND_ESSAY),
    ("Describe your relevant experience for this role.", "essay", CAT_ESSAY, KIND_ESSAY),
    ("What are you looking for in your next role?", "essay", CAT_ESSAY, KIND_ESSAY),
    ("Tell us why you applied.", "essay", CAT_ESSAY, KIND_ESSAY),
    ("What do you know about our company?", "essay", CAT_ESSAY, KIND_ESSAY),
    ("Describe a time you overcame a technical challenge.", "essay", CAT_ESSAY, KIND_ESSAY),
    ("Is there anything else you would like us to know?", "essay", CAT_ESSAY, KIND_ESSAY),
]

# Precompute normalized keys + token sets for the seed corpus at import.
_SEED_INDEX: List[Tuple[str, FrozenSet[str], str, str, str]] = []
for _q, _field, _cat, _kind in SEED_QUESTIONS:
    _nk, _tok = _normalize(_q)
    _SEED_INDEX.append((_nk, _tok, _field, _cat, _kind))
# Exact-norm-key -> (field, cat, kind) fast path.
_SEED_EXACT: Dict[str, Tuple[str, str, str]] = {
    nk: (field, cat, kind) for nk, _t, field, cat, kind in _SEED_INDEX
}
# Reverse index: canonical field_id -> (category, kind). Lets resolve() and the
# form-normalizer recover a question's category/kind straight from a stable
# seed-bank field_id when the raw label alone did not classify (e.g. a recon
# that already tagged the field). First mapping for a field_id wins.
_FIELD_INDEX: Dict[str, Tuple[str, str]] = {}
for _nk, _t, _field, _cat, _kind in _SEED_INDEX:
    _FIELD_INDEX.setdefault(_field, (_cat, _kind))


# ---------------------------------------------------------------------------
# Signal-keyword classifier — robust to phrasings not literally in the seed.
# Ordered: legal categories FIRST (they must never be mis-routed), essays LAST
# so a legal/logistics signal wins over a loose essay match.
# ---------------------------------------------------------------------------
def _has(tokens: FrozenSet[str], *words: str) -> bool:
    return any(w in tokens for w in words)


def _categorize(norm: str, tokens: FrozenSet[str]) -> Optional[Tuple[str, str, str]]:
    """Return (field, category, kind) for a question, or None if unrecognized.

    Signal rules first (deterministic, phrasing-robust); seed token-overlap as a
    fallback. Legal categories are evaluated before everything else.
    """
    exact = _SEED_EXACT.get(norm)
    if exact:
        return exact

    # --- LEGAL: sponsorship (check before work-auth: "require sponsorship" is
    # the sharper signal and both may co-occur) ------------------------------
    if _has(tokens, "sponsorship", "sponsor") or (
        _has(tokens, "visa", "immigration", "h1b", "h-1b") and _has(tokens, "require", "need", "requires", "needs")
    ):
        return ("requires_sponsorship", CAT_SPONSORSHIP, KIND_BOOLEAN)

    # --- LEGAL: EEO ---------------------------------------------------------
    if _has(tokens, "gender", "sex"):
        return ("eeo_gender", CAT_EEO, KIND_EEO)
    if _has(tokens, "race", "ethnicity", "ethnic", "hispanic", "latino"):
        return ("eeo_race", CAT_EEO, KIND_EEO)
    if _has(tokens, "veteran", "military"):
        return ("eeo_veteran", CAT_EEO, KIND_EEO)
    if _has(tokens, "disability", "disabled"):
        return ("eeo_disability", CAT_EEO, KIND_EEO)

    # --- LEGAL: work authorization ------------------------------------------
    if _has(tokens, "authorized", "authorization", "eligible", "eligibility", "permitted", "lawfully") and _has(
        tokens, "work", "employment", "employer", "job", "position", "country"
    ):
        return ("work_authorized_us", CAT_WORK_AUTH, KIND_BOOLEAN)
    if "authorization" in tokens and _has(tokens, "work", "employment"):
        return ("work_authorized_us", CAT_WORK_AUTH, KIND_BOOLEAN)

    # --- LEGAL: visa / immigration status -----------------------------------
    if _has(tokens, "visa", "immigration", "citizenship") and _has(tokens, "status"):
        return ("visa_status", CAT_VISA_STATUS, KIND_VALUE)

    # --- Salary -------------------------------------------------------------
    if _has(tokens, "salary", "compensation", "pay", "ctc", "remuneration", "wage", "wages", "rate"):
        return ("desired_salary", CAT_SALARY, KIND_VALUE)

    # --- Notice period ------------------------------------------------------
    if "notice" in tokens:
        return ("notice_period", CAT_NOTICE, KIND_VALUE)

    # --- Relocation ---------------------------------------------------------
    if _has(tokens, "relocate", "relocation", "relocating"):
        return ("willing_to_relocate", CAT_RELOCATION, KIND_BOOLEAN)

    # --- Start date / availability ------------------------------------------
    if _has(tokens, "start", "begin", "commence", "join", "available", "availability", "soon"):
        return ("available_start_date", CAT_START_DATE, KIND_VALUE)

    # --- Years of experience ------------------------------------------------
    if "years" in tokens or ("year" in tokens and _has(tokens, "experience", "worked", "using")):
        return ("years_experience", CAT_YEARS, KIND_YEARS)

    # --- Links --------------------------------------------------------------
    if _has(tokens, "linkedin"):
        return ("linkedin_url", CAT_LINKS, KIND_VALUE)
    if _has(tokens, "github"):
        return ("github_url", CAT_LINKS, KIND_VALUE)
    if _has(tokens, "portfolio", "website", "url", "link"):
        return ("portfolio_url", CAT_LINKS, KIND_VALUE)

    # --- Contact ------------------------------------------------------------
    if _has(tokens, "email"):
        return ("email", CAT_CONTACT, KIND_VALUE)
    if _has(tokens, "phone", "mobile", "telephone"):
        return ("phone", CAT_CONTACT, KIND_VALUE)
    if _has(tokens, "address", "city", "location", "located", "reside", "zip", "postal"):
        return ("location", CAT_CONTACT, KIND_VALUE)
    if _has(tokens, "name"):
        if _has(tokens, "first", "given"):
            return ("first_name", CAT_CONTACT, KIND_VALUE)
        if _has(tokens, "last", "surname", "family"):
            return ("last_name", CAT_CONTACT, KIND_VALUE)
        return ("full_name", CAT_CONTACT, KIND_VALUE)

    # --- Essays (long-form; grounded generation) ----------------------------
    if _has(tokens, "why") and _has(tokens, "work", "join", "interested", "interest", "want", "apply", "applied", "company", "role", "position", "here"):
        return ("essay", CAT_ESSAY, KIND_ESSAY)
    if _has(tokens, "tell", "describe", "explain") or _has(tokens, "cover") and "letter" in tokens:
        return ("essay", CAT_ESSAY, KIND_ESSAY)
    if _has(tokens, "excites", "interests", "passionate", "motivates") and _has(tokens, "role", "position", "opportunity", "company", "about"):
        return ("essay", CAT_ESSAY, KIND_ESSAY)

    # --- Fallback: best token-overlap against the seed corpus ---------------
    best_score = 0.0
    best_map: Optional[Tuple[str, str, str]] = None
    for _nk, seed_tok, field, cat, kind in _SEED_INDEX:
        j = _jaccard(tokens, seed_tok)
        if j > best_score:
            best_score = j
            best_map = (field, cat, kind)
    if best_map is not None and best_score >= _JACCARD_ACCEPT:
        return best_map
    return None


def classify(text: str) -> Optional[Tuple[str, str, str]]:
    """PUBLIC: classify a raw question label into ``(field_id, category, kind)``,
    or ``None`` if unrecognized. The single seam the form-normalizer
    (`apply_forms`) uses to derive a stable ``field_id`` from a recon label —
    same deterministic classifier `resolve()` runs, so a normalized question's
    ``field_id`` and `resolve()`'s per-question field always agree."""
    norm, tokens = _normalize(text or "")
    if not norm:
        return None
    return _categorize(norm, tokens)


def field_category_kind(field_id: str) -> Optional[Tuple[str, str]]:
    """PUBLIC: recover ``(category, kind)`` for a known seed-bank ``field_id``,
    or ``None`` for an unmapped id. Lets a caller that already holds a stable
    field_id (e.g. from recon) know whether it is a legal attestation without
    re-parsing a label."""
    return _FIELD_INDEX.get(field_id)


# ---------------------------------------------------------------------------
# Canonical profile — the ONLY source for filled answers (never a model).
# ---------------------------------------------------------------------------
def _db():
    return DBConnect().get_db()


def _load_canonical(user_email: str) -> Dict[str, Any]:
    """Assemble the user's DECLARED canonical values from real sources:

      * user_resumes.structured.contact   — name/email/phone/location/links
      * user_preferences                  — salary/notice/relocation/start/years
      * visa_profiles (+ preferences)     — LEGAL attestations (declared only)

    Returns two maps: `values` (non-legal, safe to auto-fill) and `legal`
    (work-auth/sponsorship/visa/EEO — ONLY keys the user actually declared).
    Absent keys mean "undeclared" → the question stays unresolved.
    """
    db = _db()
    try:
        # Resume Platform v3: canonical read path (users.active_resume_id ->
        # resume_assets) — never a direct user_resumes scan.
        from services import resume_access
        resume_doc = resume_access.get_active_structured(user_email) or {}
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("answer_library: resume load failed for %s: %s", user_email, e)
        resume_doc = {}
    structured = resume_doc.get("structured") or {}
    contact = structured.get("contact") or {}

    try:
        prefs = db.user_preferences.find_one({"user_email": user_email}) or {}
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("answer_library: prefs load failed for %s: %s", user_email, e)
        prefs = {}

    full_name = (contact.get("name") or "").strip()
    first = full_name.split(" ", 1)[0] if full_name else ""
    last = full_name.split(" ", 1)[1] if (full_name and " " in full_name) else ""

    def _pref(*keys: str) -> Optional[Any]:
        for k in keys:
            v = prefs.get(k)
            if v not in (None, ""):
                return v
        return None

    _start = _pref("available_start_date", "start_date", "availability", "earliest_start_date")
    values: Dict[str, Any] = {
        "full_name": full_name or None,
        "first_name": first or None,
        "last_name": last or None,
        "email": (contact.get("email") or "").strip() or None,
        "phone": (contact.get("phone") or "").strip() or None,
        "location": (contact.get("location") or "").strip() or None,
        "linkedin_url": (contact.get("linkedin") or "").strip() or None,
        "github_url": (contact.get("github") or "").strip() or None,
        "portfolio_url": (contact.get("portfolio") or "").strip() or None,
        "desired_salary": _pref("desired_salary", "salary_expectation", "expected_salary", "target_salary"),
        "notice_period": _pref("notice_period", "notice"),
        "available_start_date": _start,
        # Bank uses ``earliest_start_date``; mirror the same declared value.
        "earliest_start_date": _start,
        "willing_to_relocate": _pref("willing_to_relocate", "open_to_relocation", "relocate"),
        "years_experience": _pref("years_experience", "total_experience_years", "years_of_experience"),
        # Additional bank (non-gated) logistics/screening fields — declared prefs
        # only; absent -> the question stays unresolved (never a guess).
        "how_did_you_hear": _pref("how_did_you_hear", "referral_source"),
        "referral_name": _pref("referral_name", "referred_by"),
        "remote_preference": _pref("remote_preference", "remote", "work_preference"),
        "current_salary": _pref("current_salary", "present_salary"),
        "currently_employed": _pref("currently_employed"),
        "willing_to_travel": _pref("willing_to_travel"),
        "has_drivers_license": _pref("has_drivers_license", "drivers_license"),
        "highest_education": _pref("highest_education", "education_level"),
        "degree": _pref("degree"),
        "field_of_study": _pref("field_of_study", "major"),
        "school": _pref("school", "university", "college"),
        "graduation_year": _pref("graduation_year", "grad_year"),
        "gpa": _pref("gpa"),
        "certifications": _pref("certifications"),
    }

    legal = _load_legal(prefs, user_email)
    # Best-effort source versions for the answer provenance (never load-bearing).
    values_version = (resume_doc.get("parsed_at") or resume_doc.get("resume_id")
                      or resume_doc.get("_id"))
    legal_version = prefs.get("updated_at") or prefs.get("_id") or "declared"
    return {"values": values, "legal": legal,
            "values_version": str(values_version) if values_version else None,
            "legal_version": str(legal_version) if legal_version else None}


def _load_legal(prefs: Dict[str, Any], user_email: str) -> Dict[str, Any]:
    """Declared legal attestations only. Priority: an explicit preferences
    declaration, then a derivation from the normalized visa profile. Anything
    not declared is simply ABSENT (→ the question escalates to "ask").
    """
    legal: Dict[str, Any] = {}

    # Explicit, user-declared preferences win (most authoritative).
    for pref_key, field in (
        ("work_authorized_us", "work_authorized_us"),
        ("requires_sponsorship", "requires_sponsorship"),
        ("visa_status", "visa_status"),
    ):
        v = prefs.get(pref_key)
        if v not in (None, ""):
            legal[field] = v
    eeo = prefs.get("eeo")
    if isinstance(eeo, dict):
        for k in ("eeo_gender", "eeo_race", "eeo_veteran", "eeo_disability"):
            short = k[len("eeo_"):]
            v = eeo.get(k) if eeo.get(k) not in (None, "") else eeo.get(short)
            if v not in (None, ""):
                legal[k] = v

    # Full-catalog gated fields (question_bank): every protected (EEO) / legal
    # (work-auth + attestation) field the user DECLARED, read from a flat pref,
    # or a nested prefs["eeo"] / prefs["legal"] / prefs["work_auth"] map. A
    # declared "decline" IS an explicit value and is loaded here so the resolver's
    # sensitivity gate can honor it. Anything undeclared stays ABSENT (→ ask).
    try:
        from services import application_question_bank as qb
        gated = qb.gated_field_ids()
    except Exception as e:  # pragma: no cover - parallel build / import issue
        logger.warning("answer_library: question_bank unavailable: %s", e)
        gated = frozenset()
    _nested = [prefs.get(n) for n in ("eeo", "legal", "work_auth", "attestations")]
    for fid in gated:
        if legal.get(fid) not in (None, ""):
            continue
        v = prefs.get(fid)
        if v in (None, ""):
            for d in _nested:
                if isinstance(d, dict) and d.get(fid) not in (None, ""):
                    v = d.get(fid)
                    break
        if v not in (None, ""):
            legal[fid] = v

    # Derive from the declared visa profile for anything still undeclared.
    try:
        db = _db()
        visa_raw = db.visa_profiles.find_one({"user_email": user_email})
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("answer_library: visa load failed for %s: %s", user_email, e)
        visa_raw = None
    if visa_raw:
        try:
            from services.visa_timeline_service import normalize_profile
            visa = normalize_profile(visa_raw)
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("answer_library: visa normalize failed: %s", e)
            visa = None
        if visa:
            derived = _derive_legal_from_visa(visa)
            for k, v in derived.items():
                legal.setdefault(k, v)
    return legal


# Visa statuses (visa_timeline_service canonical values) that PERMIT employment.
_WORK_PERMITTED_STATUSES = frozenset({"F1-CPT", "F1-OPT", "F1-STEM-OPT", "H1B"})
# Statuses that imply the user will require sponsorship now or in the future.
_SPONSORSHIP_STATUSES = frozenset({"F1", "F1-CPT", "F1-OPT", "F1-STEM-OPT", "H1B"})


def _derive_legal_from_visa(visa: Dict[str, Any]) -> Dict[str, Any]:
    """Map a normalized visa profile to declared legal attestations — grounded
    strictly in the user's stated status. Only asserts what the status makes
    unambiguous; unknown/"Other" yields nothing (→ escalate)."""
    out: Dict[str, Any] = {}
    status = visa.get("visa_status") or "Other"
    if status in _WORK_PERMITTED_STATUSES:
        out["work_authorized_us"] = True
    if status in _SPONSORSHIP_STATUSES:
        out["requires_sponsorship"] = True
    if status and status != "Other":
        out["visa_status"] = status
    return out


# ---------------------------------------------------------------------------
# Per-user remembered answers — the self-growing store.
# ---------------------------------------------------------------------------
def _library():
    return _db().application_answer_library


def _lookup_user_store(user_email: str, norm: str,
                       tokens: FrozenSet[str]) -> Optional[Dict[str, Any]]:
    """Return a remembered answer for this question: exact norm_key first (index-
    served), then a bounded fuzzy token-overlap scan of the user's own entries."""
    try:
        rec = _library().find_one(
            {"user_email": user_email, "norm_key": norm}, {"_id": 0}
        )
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("answer_library: user-store exact lookup failed: %s", e)
        return None
    if rec and rec.get("answer") not in (None, ""):
        return rec

    if not tokens:
        return None
    try:
        cursor = _library().find(
            {"user_email": user_email},
            {"_id": 0, "norm_key": 1, "tokens": 1, "answer": 1, "field": 1, "category": 1},
        ).limit(_USER_STORE_SCAN_LIMIT)
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("answer_library: user-store scan failed: %s", e)
        return None
    best_score = 0.0
    best: Optional[Dict[str, Any]] = None
    for doc in cursor:
        if doc.get("answer") in (None, ""):
            continue
        j = _jaccard(tokens, frozenset(doc.get("tokens") or []))
        if j > best_score:
            best_score = j
            best = doc
    if best is not None and best_score >= _USER_STORE_JACCARD_ACCEPT:
        return best
    return None


# ---------------------------------------------------------------------------
# resolve() — the main entry: classify + fill + escalate for a batch of Qs.
# ---------------------------------------------------------------------------
def _empty_resolution() -> Dict[str, Any]:
    """The no-op return: coverage 1.0 (nothing to fill), no gaps. Carries BOTH
    the field-id-keyed ``resolved`` and the legacy text-keyed ``answers`` so a
    caller of either contract reads a coherent, empty shape."""
    return {"resolved": {}, "answers": {}, "unresolved": [], "coverage": 1.0,
            "needs_generation": [], "has_novel_free_text": False,
            "has_unconfirmed_legal_attestation": False}


def _to_bool(val: Any) -> Optional[bool]:
    """Coerce a declared value to a bool for polarity inversion, or None if it is
    not clearly boolean (then invert is a no-op — never guess)."""
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return bool(val)
    if isinstance(val, str):
        v = val.strip().lower()
        if v in ("yes", "y", "true", "t", "1", "i am", "i do", "authorized"):
            return True
        if v in ("no", "n", "false", "f", "0", "i am not", "i do not"):
            return False
    return None


def _apply_invert(val: Any, value_type: str, invert: bool) -> Any:
    """Apply detected NEGATION polarity: for a Yes/No field asked in inverse
    wording ("able to work WITHOUT sponsorship"), return the flipped boolean so
    the answer submitted matches the user's actual declared stance. Non-boolean
    values (and unparseable ones) pass through unchanged."""
    if not invert:
        return val
    try:
        from services import application_question_bank as qb
        if value_type != qb.VT_BOOLEAN:
            return val
    except Exception:  # pragma: no cover - defensive
        return val
    b = _to_bool(val)
    if b is None:
        return val
    return (not b)


def _bank_category_kind(field_id: str, bank_meta: Dict[str, Any]) -> Tuple[str, str]:
    """Map a bank field onto the legacy (category, kind) constants the autonomy
    consumers understand. Prefers the seed-bank ``_FIELD_INDEX`` when the id is
    shared, else derives from the bank category + value_type."""
    ck = _FIELD_INDEX.get(field_id)
    if ck is not None:
        return ck
    try:
        from services import application_question_bank as qb
    except Exception:  # pragma: no cover - defensive
        return (bank_meta.get("category"), KIND_VALUE)
    cat = bank_meta.get("category")
    vt = bank_meta.get("value_type")
    if cat == qb.CAT_EEO:
        return (CAT_EEO, KIND_EEO)
    if cat == qb.CAT_WORK_AUTH:
        legacy = CAT_SPONSORSHIP if field_id == "requires_sponsorship" else (
            CAT_VISA_STATUS if field_id in ("visa_status", "work_auth_country")
            else CAT_WORK_AUTH)
        return (legacy, KIND_BOOLEAN if vt == qb.VT_BOOLEAN else KIND_VALUE)
    if vt == qb.VT_ESSAY:
        return (CAT_ESSAY, KIND_ESSAY)
    if vt == qb.VT_YEARS:
        return (CAT_YEARS, KIND_YEARS)
    if vt == qb.VT_BOOLEAN:
        return (cat, KIND_BOOLEAN)
    return (cat, KIND_VALUE)


def _resolution(field_id: str, question_text: str, value: Any, source: str,
                category: str, kind: str, sensitivity: Optional[str],
                review_required: bool, *, confidence: float = 1.0,
                source_version: Optional[str] = None,
                field: Optional[str] = None) -> Dict[str, Any]:
    """The canonical resolved-answer shape. Every filled answer carries its
    provenance so the review UI / executor can audit it before submit."""
    return {
        "field_id": field_id,
        "question_text": question_text,
        "value": value,
        "field": field or field_id,
        "category": category,
        "kind": kind,
        "sensitivity": sensitivity,
        "source": source,
        "source_version": source_version,
        "confidence": round(float(confidence), 4),
        "review_required": bool(review_required),
    }


def resolve(user_email: str, questions: List[Any]) -> Dict[str, Any]:
    """Resolve a batch of form questions against the seed mappings + the user's
    canonical profile + their remembered answers.

    Two input shapes, one classifier:

      * NORMALIZED (auto-apply, the canonical path): a list of normalized
        question dicts ``{field_id, label, type, required, is_legal_attestation,
        options?}`` (see `apply_forms`). Returns a **field_id-keyed** map:
          {
            "resolved": { field_id: {value, field, category, kind, source,
                                     confidence} },
            "unresolved": [ field_id, ... ],
            "coverage": float,                  # REQUIRED fields only
            "needs_generation": [ {field_id, label, category, kind} ],
            "has_novel_free_text": bool,
            "has_unconfirmed_legal_attestation": bool,
          }

      * LEGACY (back-compat: the answer-library's own unit tests): a list of
        raw question STRINGS. Returns the historical text-keyed ``answers`` map
        (see `_resolve_legacy`). No production caller uses this path — the
        auto-apply orchestrator is the only production consumer and passes
        normalized dicts — but it is preserved so the library stays independently
        testable by label.

    Per question, in order: classify (`_categorize`); LEGAL → fill from DECLARED
    data only, else unresolved (never guessed); ESSAY → flag `needs_generation`
    (never fabricated here); other → canonical profile then the per-user store,
    else unresolved; unrecognized → novel. `coverage`,
    `has_novel_free_text` and `has_unconfirmed_legal_attestation` are exactly
    what autonomy_service.score()/decide() consume (see module doc).
    """
    if not user_email or not isinstance(questions, list):
        return _empty_resolution()
    # Normalized dicts vs. legacy strings. An all-dict (incl. empty) list takes
    # the canonical field-id path; anything else is the legacy string path.
    if all(isinstance(q, dict) for q in questions):
        return _resolve_normalized(user_email, questions)
    return _resolve_legacy(user_email, questions)


def _resolve_normalized(user_email: str,
                        questions: List[Dict[str, Any]]) -> Dict[str, Any]:
    """The canonical field-id-keyed resolution (see `resolve`)."""
    resolved: Dict[str, Dict[str, Any]] = {}
    unresolved: List[str] = []
    needs_generation: List[Dict[str, Any]] = []
    has_novel = False
    has_unconfirmed_legal = False

    # De-duped, sanitized view of the normalized questions.
    prepared: List[Tuple[str, str, FrozenSet[str], bool, bool, bool, Optional[Tuple[str, str, str]]]] = []
    seen_fids: set = set()
    for q in questions[:_MAX_QUESTIONS]:
        label = InputSanitizer.sanitize_string(
            str(q.get("label") or q.get("text") or ""), max_length=2000)
        raw_fid = q.get("field_id") or q.get("id")
        field_id = InputSanitizer.sanitize_string(str(raw_fid or ""), max_length=200)
        required = bool(q.get("required"))
        flagged_legal = bool(q.get("is_legal_attestation"))
        # Polarity from the normalizer's negation pass (e.g. "able to work WITHOUT
        # sponsorship" -> invert the stored Yes/No). Never inferred here.
        invert = bool(q.get("invert"))

        norm, tokens = _normalize(label)
        mapping = _categorize(norm, tokens)
        # Recover (field, cat, kind) from a stable seed-bank field_id when the
        # bare label did not classify (a recon may hand us a recognized id).
        if mapping is None and field_id:
            ck = _FIELD_INDEX.get(field_id)
            if ck is not None:
                mapping = (field_id, ck[0], ck[1])
        # Choose the output key: the caller's stable field_id wins; else the
        # classified field; else the normalized label (never silently dropped).
        if not field_id:
            field_id = (mapping[0] if mapping else "") or norm or label
        if not field_id or field_id in seen_fids:
            continue
        seen_fids.add(field_id)
        prepared.append((field_id, norm, tokens, required, flagged_legal, invert, mapping))

    if not prepared:
        return _empty_resolution()

    canonical = _load_canonical(user_email)
    values, legal = canonical["values"], canonical["legal"]
    values_version = canonical.get("values_version")
    legal_version = canonical.get("legal_version")
    try:
        from services import application_question_bank as qb
    except Exception as e:  # pragma: no cover - parallel build / import issue
        logger.warning("answer_library: question_bank unavailable: %s", e)
        qb = None

    required_total = 0
    required_resolved = 0

    for field_id, norm, tokens, required, flagged_legal, invert, mapping in prepared:
        # ---------------------------------------------------------------
        # BANK-FIRST resolution: when the field_id is a canonical catalog
        # field, the question bank is authoritative for sensitivity + polarity
        # and the sensitivity gate is enforced here (protected/legal with no
        # DECLARED value -> needs_input, never a guess).
        # ---------------------------------------------------------------
        bank_meta = qb.get_field(field_id) if qb is not None else None
        if bank_meta is not None:
            b_category, b_kind = _bank_category_kind(field_id, bank_meta)
            sensitivity = bank_meta["sensitivity"]
            vtype = bank_meta["value_type"]
            gated = sensitivity in (qb.SENS_PROTECTED, qb.SENS_LEGAL)

            # Free-text / essay: grounded generation elsewhere, never here.
            if bank_meta["category"] == qb.CAT_FREE_TEXT:
                needs_generation.append({"field_id": field_id, "label": norm,
                                         "category": CAT_ESSAY, "kind": KIND_ESSAY})
                has_novel = True
                continue

            if required:
                required_total += 1

            if gated:
                # DECLARED data only. A declared "decline" IS explicit → allowed.
                val = legal.get(field_id)
                if val not in (None, ""):
                    out_val = _apply_invert(val, vtype, invert)
                    resolved[field_id] = _resolution(
                        field_id, norm, out_val, "declared", b_category, b_kind,
                        sensitivity, review_required=True,
                        source_version=legal_version)
                    if required:
                        required_resolved += 1
                else:
                    unresolved.append(field_id)
                    has_unconfirmed_legal = True
                continue

            # Non-gated: canonical profile, then the per-user remembered store.
            val = values.get(field_id)
            src, sv = "profile", values_version
            if val in (None, ""):
                stored = _lookup_user_store(user_email, norm, tokens)
                if stored is not None:
                    val = stored.get("answer")
                    src, sv = "user_store", str(stored.get("updated_at") or "") or None
            if val not in (None, ""):
                out_val = _apply_invert(val, vtype, invert)
                review = (sensitivity != qb.SENS_SAFE) or (src == "user_store")
                resolved[field_id] = _resolution(
                    field_id, norm, out_val, src, b_category, b_kind, sensitivity,
                    review_required=review, source_version=sv)
                if required:
                    required_resolved += 1
            else:
                unresolved.append(field_id)
                if vtype in (qb.VT_BOOLEAN, qb.VT_SELECT, qb.VT_YEARS,
                             qb.VT_MONEY, qb.VT_NUMBER, qb.VT_DATE):
                    has_novel = True
            continue

        # ---------------------------------------------------------------
        # LEGACY (non-bank) resolution — unchanged behavior.
        # ---------------------------------------------------------------
        if mapping is not None:
            field, category, kind = mapping
        elif flagged_legal:
            # Flagged legal by recon but unclassifiable — treat as an unconfirmed
            # legal attestation (err toward the never-fabricate guard).
            field, category, kind = field_id, None, KIND_LEGAL
        else:
            # Unrecognized, non-legal → novel free-text risk.
            if required:
                required_total += 1
            unresolved.append(field_id)
            has_novel = True
            continue

        is_legal = flagged_legal or (category in LEGAL_CATEGORIES)

        # Essays: grounded generation, never counted in coverage, never here.
        if kind == KIND_ESSAY and not is_legal:
            needs_generation.append({"field_id": field_id, "label": norm,
                                     "category": category, "kind": KIND_ESSAY})
            has_novel = True
            continue

        if required:
            required_total += 1

        # Legal attestations — DECLARED data only, never guessed.
        if is_legal:
            val = legal.get(field) if field else None
            if val not in (None, ""):
                resolved[field_id] = {"value": val, "field": field,
                                      "category": category, "kind": kind,
                                      "source": "declared", "confidence": 1.0}
                if required:
                    required_resolved += 1
            else:
                unresolved.append(field_id)
                has_unconfirmed_legal = True
            continue

        # Global mapping — fill from the canonical profile.
        val = values.get(field) if field else None
        if val not in (None, ""):
            resolved[field_id] = {"value": val, "field": field, "category": category,
                                  "kind": kind, "source": "profile", "confidence": 1.0}
            if required:
                required_resolved += 1
            continue

        # Per-user remembered answer.
        stored = _lookup_user_store(user_email, norm, tokens)
        if stored is not None:
            resolved[field_id] = {"value": stored.get("answer"), "field": field,
                                  "category": category, "kind": kind,
                                  "source": "user_store", "confidence": 1.0}
            if required:
                required_resolved += 1
            continue

        # Unresolved — a known field we simply have no declared value for.
        unresolved.append(field_id)
        if kind in (KIND_YEARS, KIND_VALUE, KIND_BOOLEAN):
            has_novel = True

    coverage = (required_resolved / required_total) if required_total else 1.0
    return {
        "resolved": resolved,
        "unresolved": unresolved,
        "coverage": round(coverage, 4),
        "needs_generation": needs_generation,
        "has_novel_free_text": bool(has_novel or needs_generation),
        "has_unconfirmed_legal_attestation": has_unconfirmed_legal,
    }


def _resolve_legacy(user_email: str, questions: List[str]) -> Dict[str, Any]:
    """Back-compat text-keyed resolution (list of raw question strings). Returns
    ``answers`` keyed by question TEXT. Preserved for the library's own unit
    tests; no production caller uses it (auto-apply passes normalized dicts)."""
    answers: Dict[str, Dict[str, Any]] = {}
    unresolved: List[Dict[str, Any]] = []
    needs_generation: List[Dict[str, Any]] = []
    has_novel = False
    has_unconfirmed_legal = False

    # Clean + dedupe questions (bounded). Preserve first-seen order.
    cleaned: List[Tuple[str, str, FrozenSet[str]]] = []
    seen: set = set()
    for raw in questions[:_MAX_QUESTIONS]:
        if not isinstance(raw, str):
            continue
        q = InputSanitizer.sanitize_string(raw, max_length=2000)
        if not q:
            continue
        norm, tokens = _normalize(q)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        cleaned.append((q, norm, tokens))

    if not cleaned:
        return _empty_resolution()

    canonical = _load_canonical(user_email)
    values, legal = canonical["values"], canonical["legal"]

    total = len(cleaned)
    resolved = 0

    for q, norm, tokens in cleaned:
        mapping = _categorize(norm, tokens)
        if mapping is None:
            unresolved.append({"question": q, "field": None,
                               "category": None, "kind": KIND_NOVEL})
            has_novel = True
            continue

        field, category, kind = mapping

        if category in LEGAL_CATEGORIES:
            val = legal.get(field)
            if val not in (None, ""):
                answers[q] = {"value": val, "field": field, "category": category,
                              "kind": kind, "source": "declared"}
                resolved += 1
            else:
                unresolved.append({"question": q, "field": field,
                                   "category": category, "kind": KIND_LEGAL})
                has_unconfirmed_legal = True
            continue

        if kind == KIND_ESSAY:
            needs_generation.append({"question": q, "field": field,
                                     "category": category, "kind": KIND_ESSAY})
            has_novel = True
            continue

        val = values.get(field)
        if val not in (None, ""):
            answers[q] = {"value": val, "field": field, "category": category,
                          "kind": kind, "source": "profile"}
            resolved += 1
            continue

        stored = _lookup_user_store(user_email, norm, tokens)
        if stored is not None:
            answers[q] = {"value": stored.get("answer"), "field": field,
                          "category": category, "kind": kind, "source": "user_store"}
            resolved += 1
            continue

        unresolved.append({"question": q, "field": field,
                           "category": category, "kind": kind})
        if kind in (KIND_YEARS, KIND_VALUE, KIND_BOOLEAN):
            has_novel = True

    denom = total - len(needs_generation)
    coverage = (resolved / denom) if denom else 1.0
    return {
        "answers": answers,
        "unresolved": unresolved,
        "coverage": round(coverage, 4),
        "needs_generation": needs_generation,
        "has_novel_free_text": bool(has_novel or needs_generation),
        "has_unconfirmed_legal_attestation": has_unconfirmed_legal,
    }


# ---------------------------------------------------------------------------
# remember() — persist a user's answer so it is never asked twice.
# ---------------------------------------------------------------------------
def remember(user_email: str, question: str, answer: Any) -> Optional[Dict[str, Any]]:
    """Store the user's own answer to a question, keyed by the normalized
    question (upsert on (user_email, norm_key)). Returns the stored view, or None
    when the input is unusable.

    The answer is the USER's declared value — this is how a legal attestation
    the user confirmed once becomes reusable (source "user_store") without ever
    being guessed. Essays are NOT remembered here (they are per-JD generated,
    tracked in application_answers), so an essay-classified question is skipped.
    """
    if not user_email:
        return None
    q = InputSanitizer.sanitize_string(question or "", max_length=2000)
    if not q:
        return None
    norm, tokens = _normalize(q)
    if not norm:
        return None

    # Sanitize the answer: strings get cleaned/capped; bool/number pass through.
    if isinstance(answer, str):
        stored_answer: Any = InputSanitizer.sanitize_string(answer, max_length=4000)
    elif isinstance(answer, (bool, int, float)):
        stored_answer = answer
    elif answer is None:
        return None
    else:
        # Anything structured is coerced to a bounded string — never store raw,
        # potentially operator-shaped, objects.
        stored_answer = InputSanitizer.sanitize_string(str(answer), max_length=4000)
    if stored_answer in (None, ""):
        return None

    mapping = _categorize(norm, tokens)
    field = mapping[0] if mapping else None
    category = mapping[1] if mapping else None
    kind = mapping[2] if mapping else KIND_NOVEL

    # Do not bank essays here (they belong to the per-company generation history).
    if kind == KIND_ESSAY:
        return None

    now = datetime.now(timezone.utc)
    doc = {
        "user_email": user_email,
        "norm_key": norm,
        "question": q,
        "answer": stored_answer,
        "tokens": sorted(tokens),
        "field": field,
        "category": category,
        "kind": kind,
        "updated_at": now,
    }
    try:
        _library().update_one(
            {"user_email": user_email, "norm_key": norm},
            {"$set": doc, "$setOnInsert": {"created_at": now}},
            upsert=True,
        )
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("answer_library: remember upsert failed: %s", e)
        return None
    return {"question": q, "norm_key": norm, "answer": stored_answer,
            "field": field, "category": category, "kind": kind}
