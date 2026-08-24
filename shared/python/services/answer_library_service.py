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

# ---------------------------------------------------------------------------
# Provenance vocabulary (the ``source`` on every resolution).
# ---------------------------------------------------------------------------
SOURCE_DECLARED = "declared"            # user_preferences / visa_profiles
SOURCE_PROFILE = "profile"              # résumé (resume_assets.structured)
SOURCE_USER_STORE = "user_store"        # a value the user answered once
SOURCE_RESUME_MATCH = "resume_match"    # résumé ESCALATION (spec §1) — Yes only
SOURCE_POLICY_DECLINE = "policy_decline"  # protected-EEO auto-decline (spec §0.3)
SOURCE_POLICY_SAFE = "policy_safe_default"  # consent-ack / marketing-opt-in default

# Sources that are NOT fabrications — every one is either the user's own stated
# data, a fact off their résumé, or an explicit "I decline to answer" (which
# asserts nothing). `auto_apply_service._DECLARED_SOURCES` must be a superset of
# this set or a legitimately-resolved answer is bounced back to needs_input.
DECLARED_SOURCES: FrozenSet[str] = frozenset({
    SOURCE_DECLARED, SOURCE_PROFILE, SOURCE_USER_STORE,
    SOURCE_RESUME_MATCH, SOURCE_POLICY_DECLINE,
})

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


# ---------------------------------------------------------------------------
# Address decomposition — `contact.location` is ONE free-text line ("Tempe, AZ
# 85281"), but ATS forms ask for city / state / postal_code / country as separate
# required fields. We decompose DETERMINISTICALLY and leave a component
# UNRESOLVED whenever the line cannot be parsed — never a guess (a bare
# "Remote" is not a city). A declared `user_preferences` value always wins over
# anything derived here (spec §2 order: prefs → visa → résumé).
# ---------------------------------------------------------------------------
_ZIP_RE = re.compile(r"\b(\d{5}(?:-\d{4})?)\b")

_US_STATE_ABBR: FrozenSet[str] = frozenset({
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI",
    "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN",
    "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH",
    "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA",
    "WV", "WI", "WY", "PR", "VI", "GU",
})
_US_STATE_NAMES: FrozenSet[str] = frozenset({
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "district of columbia", "florida", "georgia",
    "hawaii", "idaho", "illinois", "indiana", "iowa", "kansas", "kentucky",
    "louisiana", "maine", "maryland", "massachusetts", "michigan", "minnesota",
    "mississippi", "missouri", "montana", "nebraska", "nevada",
    "new hampshire", "new jersey", "new mexico", "new york", "north carolina",
    "north dakota", "ohio", "oklahoma", "oregon", "pennsylvania",
    "rhode island", "south carolina", "south dakota", "tennessee", "texas",
    "utah", "vermont", "virginia", "washington", "west virginia", "wisconsin",
    "wyoming", "puerto rico",
})
# Only the handful of country spellings that actually appear at the tail of a
# résumé location line. Anything else is left unresolved rather than guessed.
_COUNTRY_ALIASES: Dict[str, str] = {
    "usa": "United States", "u s a": "United States", "us": "United States",
    "u s": "United States", "united states": "United States",
    "united states of america": "United States", "america": "United States",
    "canada": "Canada", "mexico": "Mexico", "india": "India",
    "uk": "United Kingdom", "u k": "United Kingdom",
    "united kingdom": "United Kingdom", "england": "United Kingdom",
    "germany": "Germany", "france": "France", "spain": "Spain",
    "ireland": "Ireland", "netherlands": "Netherlands", "australia": "Australia",
    "singapore": "Singapore", "japan": "Japan", "china": "China",
    "brazil": "Brazil", "israel": "Israel", "poland": "Poland",
    "portugal": "Portugal", "switzerland": "Switzerland", "sweden": "Sweden",
}


def _country_of(part: str) -> Optional[str]:
    key = re.sub(r"[^a-z ]+", " ", (part or "").lower())
    key = re.sub(r"\s+", " ", key).strip()
    return _COUNTRY_ALIASES.get(key)


def _is_state(part: str) -> bool:
    p = (part or "").strip()
    if not p:
        return False
    if len(p) == 2 and p.isalpha() and p.upper() in _US_STATE_ABBR:
        return True
    return re.sub(r"\s+", " ", p.lower()) in _US_STATE_NAMES


def _decompose_location(location: Any) -> Dict[str, str]:
    """Best-effort split of a free-text location line into
    ``{city, state, postal_code, country}``. Emits ONLY the components it can
    actually read; an unparseable line yields ``{}`` (→ those fields stay
    unresolved and the user is asked, rather than being handed a guess)."""
    s = str(location or "").strip()[:500]     # bound the parse, whatever we got
    if not s:
        return {}
    out: Dict[str, str] = {}

    m = _ZIP_RE.search(s)
    if m:
        out["postal_code"] = m.group(1)
        s = (s[:m.start()] + " " + s[m.end():])

    parts = [p.strip(" ,\t") for p in s.split(",")]
    parts = [p for p in parts if p]

    had_country = False
    if parts:
        c = _country_of(parts[-1])
        if c:
            out["country"] = c
            had_country = True
            parts = parts[:-1]

    if len(parts) >= 2:
        if _is_state(parts[-1]):
            out["state"] = parts[-1]
            out["city"] = parts[-2]
        elif had_country:
            # "Bangalore, Karnataka, India" — a non-US region before a country.
            out["state"] = parts[-1]
            out["city"] = parts[-2]
        # else: "1216 E Vista Del Cerro Dr, Tempe" style — ambiguous, skip.
    elif len(parts) == 1:
        if _is_state(parts[0]):
            out["state"] = parts[0]
        elif had_country:
            out["city"] = parts[0]
        # A bare single token with no country ("Remote", "Austin") is NOT
        # assumed to be a city — leave unresolved.
    return out


# ---------------------------------------------------------------------------
# Résumé date / employment derivation (spec §2 step 3, rule 2: employer names,
# titles and dates come from the résumé — reformatted, never invented).
# ---------------------------------------------------------------------------
_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}
_PRESENT = frozenset({"present", "current", "now", "ongoing", "today", "date"})
_RANGE_SPLIT = re.compile(r"\s*(?:--|–|—|-|\bto\b|\bthrough\b|\buntil\b)\s*",
                          re.IGNORECASE)


def _parse_date_token(tok: str) -> Optional[str]:
    """Normalize one side of a résumé date range to ``MM/YYYY`` (or ``YYYY``).
    Returns None when there is no readable year — we never invent precision."""
    t = (tok or "").strip()
    if not t:
        return None
    year_m = re.search(r"\b(19\d{2}|20\d{2})\b", t)
    if not year_m:
        return None
    year = year_m.group(1)
    month: Optional[int] = None
    name_m = re.search(r"\b([A-Za-z]{3,9})\b", t)
    if name_m:
        month = _MONTHS.get(name_m.group(1).lower())
    if month is None:
        num_m = re.search(r"\b(0?[1-9]|1[0-2])\s*[/\-]\s*(?:19|20)\d{2}\b", t)
        if num_m:
            month = int(num_m.group(1))
    if month is None:
        num_m = re.search(r"\b(?:19|20)\d{2}\s*[/\-]\s*(0?[1-9]|1[0-2])\b", t)
        if num_m:
            month = int(num_m.group(1))
    return f"{month:02d}/{year}" if month else year


def _split_date_range(dates: Any) -> Tuple[Optional[str], Optional[str], Optional[bool]]:
    """``("Jan 2023 - Present")`` -> ``("01/2023", None, True)``.

    Returns ``(start, end, currently_here)``. ``currently_here`` is True only
    when the résumé literally says Present/Current, False only when a real end
    date is present, and None when the range is unreadable."""
    s = str(dates or "").strip()
    if not s:
        return (None, None, None)
    parts = [p for p in _RANGE_SPLIT.split(s) if p and p.strip()]
    if not parts:
        return (None, None, None)
    start = _parse_date_token(parts[0])
    if len(parts) == 1:
        tail = parts[0].strip().lower()
        return (start, None, True if tail in _PRESENT else None)
    tail = parts[-1].strip().lower().strip(".")
    if tail in _PRESENT:
        return (start, None, True)
    end = _parse_date_token(parts[-1])
    return (start, end, False if end else None)


# Highest-education ranking, mapped onto the catalog's option list. Only an
# explicit keyword hit counts; an unrecognised degree line yields nothing.
_EDU_RANK: List[Tuple[str, int, str]] = [
    (r"\b(ph\.?d|doctor(ate|al)?|d\.?phil|\bmd\b|\bjd\b|ed\.?d)\b", 5, "Doctorate"),
    (r"\b(m\.?s\.?c?|m\.?eng|m\.?b\.?a|master'?s?|m\.?tech|m\.?a\b|mca)\b", 4, "Master's"),
    (r"\b(b\.?s\.?c?|b\.?eng|b\.?b\.?a|bachelor'?s?|b\.?tech|b\.?a\b|bca)\b", 3, "Bachelor's"),
    (r"\b(associate'?s?|a\.?a\.?s?\b|diploma)\b", 2, "Associate"),
    (r"\b(high school|secondary school|ged|hsc|12th)\b", 1, "High School"),
]


def _education_level(text: str) -> Tuple[int, Optional[str]]:
    t = (text or "").lower()
    for pattern, rank, label in _EDU_RANK:
        if re.search(pattern, t):
            return (rank, label)
    return (0, None)


def _reconcile_name_parts(values: Dict[str, Any], prefs: Dict[str, Any]) -> None:
    """Keep full_name / first_name / last_name consistent after the declared
    sweep. A user who DECLARED only ``full_name`` must not keep résumé-derived
    first/last parts from a different spelling of their name; a user who declared
    only the parts gets a composed full name."""
    full = str(values.get("full_name") or "").strip()
    if full and prefs.get("full_name") not in (None, "") \
            and prefs.get("first_name") in (None, "") \
            and prefs.get("last_name") in (None, ""):
        values["first_name"] = full.split(" ", 1)[0] or None
        values["last_name"] = full.split(" ", 1)[1] if " " in full else None
        return
    first = str(values.get("first_name") or "").strip()
    last = str(values.get("last_name") or "").strip()
    if not full and first and last:
        values["full_name"] = f"{first} {last}"


def _fill_from_resume(values: Dict[str, Any], structured: Dict[str, Any],
                      contact: Dict[str, Any]) -> None:
    """Layer résumé-derived facts UNDER whatever is already in ``values`` (i.e.
    only fill keys the declared prefs left empty) — spec §2 puts
    ``user_preferences`` ahead of ``resume_assets.structured``."""
    def _put(fid: str, val: Any) -> None:
        if val in (None, "") or values.get(fid) not in (None, ""):
            return
        values[fid] = val

    # --- contact ---------------------------------------------------------
    _put("website_url", (contact.get("website") or "").strip()
         if isinstance(contact.get("website"), str) else None)
    _put("address_line1", (contact.get("address") or "").strip()
         if isinstance(contact.get("address"), str) else None)
    for fid, val in _decompose_location(contact.get("location")).items():
        _put(fid, val)

    # --- most-recent experience -----------------------------------------
    experience = [e for e in (structured.get("experience") or [])
                  if isinstance(e, dict)]
    if experience:
        exp = experience[0]
        _put("current_company", str(exp.get("company") or "").strip() or None)
        _put("current_title", str(exp.get("title") or "").strip() or None)
        bullets = [str(b).strip() for b in (exp.get("bullets") or []) if str(b).strip()]
        if bullets:
            _put("role_description", " ".join(bullets)[:2000])
        start, end, current = _split_date_range(exp.get("dates"))
        _put("work_start_date", start)
        _put("work_end_date", end)
        if current is not None:
            _put("currently_work_here", current)

    # --- education -------------------------------------------------------
    education = [e for e in (structured.get("education") or []) if isinstance(e, dict)]
    if education:
        edu = education[0]
        degree = str(edu.get("degree") or "").strip()
        _put("degree", degree or None)
        _put("school", str(edu.get("institution") or "").strip() or None)
        _put("gpa", str(edu.get("gpa") or "").strip() or None)
        grad = _parse_date_token(str(edu.get("dates") or "").split("-")[-1])
        if grad:
            _put("graduation_year", grad.split("/")[-1])
        # "Master of Science in Computer Science" -> "Computer Science" (the LAST
        # "in"/"of" clause wins, so the degree name itself is not mistaken for
        # the major).
        for sep in (r"\bin\s+", r"\bof\s+"):
            m = list(re.finditer(sep + r"([A-Za-z][A-Za-z&,\-\s]{2,60})$", degree))
            if m:
                major = m[-1].group(1).strip()
                if _education_level(major)[0] == 0:   # not just "…of Science"
                    _put("field_of_study", major)
                break
        best_rank, best_label = 0, None
        for e in education:
            rank, label = _education_level(
                f"{e.get('degree') or ''} {e.get('institution') or ''}")
            if rank > best_rank:
                best_rank, best_label = rank, label
        _put("highest_education", best_label)


# ---------------------------------------------------------------------------
# Company fuzzy matching — the ONLY résumé-driven escalation (spec §1).
# ---------------------------------------------------------------------------
_CO_NOISE = re.compile(
    r"\b(inc|llc|l\s?l\s?c|ltd|limited|corp|corporation|co|company|plc|gmbh|"
    r"sa|nv|ag|llp|lp|pvt|private|group|holdings|international|the|and)\b")


def _norm_company(name: Any) -> str:
    s = re.sub(r"[^a-z0-9 ]+", " ", str(name or "").lower())
    s = _CO_NOISE.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


def _company_matches(a: Any, b: Any) -> bool:
    """Conservative fuzzy equality between two employer names. Exact normalized
    equality, or whole-word containment where the shorter side is substantial
    (>= 4 chars) — so "Acme" matches "Acme Corporation" but never "AC"."""
    na, nb = _norm_company(a), _norm_company(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    short, long = (na, nb) if len(na) <= len(nb) else (nb, na)
    if len(short) < 4:
        return False
    return re.search(r"(?:^| )" + re.escape(short) + r"(?:$| )", long) is not None


# Fields whose "Yes" the résumé is allowed to ESCALATE to, and the employer the
# field is about. ``None`` means "the company being applied to".
_ESCALATABLE_EMPLOYER: Dict[str, Optional[Tuple[str, ...]]] = {
    "previous_employee": None,
    "ey_employee": ("Ernst & Young", "Ernst and Young", "EY"),
}


def _resume_company_escalations(structured: Dict[str, Any],
                                company: Optional[str]) -> Dict[str, Any]:
    """Spec §1: the résumé may ONLY escalate a worked-here question to "Yes".

    If ``resume_assets.structured.experience[].company`` fuzzy-matches the target
    employer, we answer Yes and flag it for review. The résumé is a curated
    document, not an employment record — so it is NEVER used to assert "No"; the
    user's declared value is the only source for a No. Returns ``{field_id: True}``
    (never False, never any other value)."""
    resume_companies = [e.get("company") for e in (structured.get("experience") or [])
                        if isinstance(e, dict) and e.get("company")]
    if not resume_companies:
        return {}
    out: Dict[str, Any] = {}
    for fid, employers in _ESCALATABLE_EMPLOYER.items():
        targets = list(employers) if employers else ([company] if company else [])
        for target in targets:
            if not target:
                continue
            if any(_company_matches(target, rc) for rc in resume_companies):
                out[fid] = True
                break
    return out


def _sweep_declared_prefs(values: Dict[str, Any], prefs: Dict[str, Any],
                          qb: Any, gated: FrozenSet[str]) -> set:
    """The generic pass that fixes the live bug: onboarding writes EVERY answer
    as a flat ``user_preferences[field_id]`` key
    (`apply_onboarding_service.save_answers`) and, before this, only ~9 of them
    were ever read back — so the resolver kept asking for address_line1 / city /
    state / postal_code / country / phone_device_type / current_title /
    current_company / work_start_date / role_description even though
    ``READY_REQUIRED_FIELDS`` had forced the user to declare them.

    Every non-gated, non-free-text catalog field now gets a path here. Gated
    (protected-EEO / legal) fields are deliberately EXCLUDED — they are loaded by
    `_load_legal`, which enforces the never-fabricate rule.

    Returns the set of field_ids that came from a DECLARED pref (so the
    resolution can carry ``source: "declared"`` rather than the generic
    ``"profile"`` — a value the user stated is not the same provenance as one
    read off their résumé)."""
    declared: set = set()
    if qb is None:
        return declared
    for f in qb.FIELDS:
        fid = f["field_id"]
        if fid in gated or f["category"] == qb.CAT_FREE_TEXT:
            continue
        v = prefs.get(fid)
        if v in (None, ""):
            values.setdefault(fid, None)
            continue
        values[fid] = _coerce_declared(v, f, qb)
        declared.add(fid)
    return declared


def _coerce_declared(value: Any, field: Dict[str, Any], qb: Any) -> Any:
    """Normalize a DECLARED value to the shape the field expects.

    Live data mixes types for the same concept — ``previous_employee='No'``
    (string) next to ``ey_employee=False`` (bool). A Yes/No SELECT must carry the
    exact option string or the executor's option match is doing extra work; a
    BOOLEAN field must carry a real bool so `_apply_invert` can flip polarity.
    Anything not clearly coercible is passed through untouched (never guessed)."""
    vt = field.get("value_type")
    if vt == qb.VT_BOOLEAN:
        b = _to_bool(value)
        return value if b is None else b
    if vt == qb.VT_SELECT:
        opts = field.get("options") or []
        if not opts or value in opts:
            return value
        # A Yes/No select declared as a bool / 0 / 1 / "no" -> the exact option.
        if {"Yes", "No"} <= set(opts):
            b = _to_bool(value)
            if b is not None:
                return "Yes" if b else "No"
    return value


# The 48 contiguous states + DC. Alaska and Hawaii are deliberately ABSENT:
# "do you reside in the 48 contiguous US" is False for them, and a US territory
# is not a state at all.
_CONTIGUOUS_US = frozenset("""
AL AZ AR CA CO CT DE DC FL GA ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE
NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY
""".split())

_US_COUNTRY_NAMES = frozenset({
    "us", "usa", "u.s.", "u.s.a.", "united states", "united states of america",
})


def _derive_ai_agent_disclosure(values: Dict[str, Any]) -> None:
    """`ai_agent_disclosure` — always "Yes", because it is always true here.

    Employers have begun asking outright whether an AI agent is submitting
    (Attentive, 2026-08-24: "If you are an AI Agent applying on behalf of a
    candidate, please select Yes"). Auto-apply IS an AI agent applying on behalf
    of a candidate, so Yes is simply the fact.

    This is the one gated-adjacent question the platform can answer WITHOUT a user
    declaration, because it is about the platform rather than the user — and the
    alternative ("Not Applicable") would be a false statement to an employer about
    the submission itself. Owner policy, 2026-08-24: always disclose truthfully.

    NOT overridden if already present, so an explicit user preference still wins.
    """
    if values.get("ai_agent_disclosure") in (None, ""):
        values["ai_agent_disclosure"] = "Yes"


def _derive_signature_date(values: Dict[str, Any]) -> None:
    """`date_signed` — the date the form is being signed, i.e. TODAY.

    Derived at resolve time and never stored: a signature date read back from
    user_preferences would be a false statement about when the form was signed.
    Asserts nothing about the user, which is why it is safe to fill on a CC-305
    Self Identify page where every other field is a protected attestation.

    Live gap: Workday's `selfIdentifiedDisabilityData--dateSignedOn` is REQUIRED
    and there was no catalog field to answer it, so the page reported
    `dateSectionMonth-input: required/invalid` and the run could not complete
    Self Identify on a tenant that enforces it.

    MM/DD/YYYY — the format `WorkdayAdapter._fill_month_year` parses.
    """
    if values.get("date_signed") in (None, ""):
        from datetime import datetime, timezone
        values["date_signed"] = datetime.now(timezone.utc).strftime("%m/%d/%Y")


def _derive_location_facts(values: Dict[str, Any]) -> None:
    """Fill location fields that are pure RESTATEMENTS of declared data.

    Only two, and both are facts the user already gave us:

      * ``current_location``       — "Tempe, AZ, United States"
      * ``resides_contiguous_us``  — country is unambiguously the US AND the
        declared state is one of the 48 (+DC)

    Deliberately NOT derived: "do you live in <city/county>?" and "is a commute
    to <city> workable?". Both are knockout screening questions, and the honest
    answer to a commute question depends on relocation intent — which
    ``willing_to_relocate`` does NOT settle for a specific city. Those stay
    unresolved and are asked. Mutates ``values`` in place; never overwrites a
    value already present."""
    city = str(values.get("city") or "").strip()
    state = str(values.get("state") or "").strip()
    country = str(values.get("country") or "").strip()

    if not values.get("current_location"):
        parts = [p for p in (city, state, country) if p]
        if parts:
            values["current_location"] = ", ".join(parts)

    if values.get("resides_contiguous_us") is None:
        if country and country.strip().lower() in _US_COUNTRY_NAMES:
            st = state.upper().replace(".", "").strip()
            if st in _CONTIGUOUS_US:
                values["resides_contiguous_us"] = True
            elif st in ("AK", "HI"):
                values["resides_contiguous_us"] = False
            # unknown/unparseable state -> leave undeclared, so the user is asked
        elif country:
            # A declared NON-US country answers this unambiguously.
            values["resides_contiguous_us"] = False


_LINKEDIN_RE = re.compile(
    r"^\s*(?:https?://)?(?:[a-z]{2,3}\.)?(?:www\.)?linkedin\.com/(.+?)/*\s*$", re.I)


def _canonical_linkedin(url: Any) -> Any:
    """Canonicalize a LinkedIn profile URL to ``https://www.linkedin.com/<path>``.

    Workday validates this field STRICTLY and rejects anything without the
    scheme + ``www.`` host, with "Invalid LinkedIn URL" — even though the URL
    resolves fine in a browser.

    Live evidence (Expedia, 2026-08-23): the declared value was
    ``https://linkedin.com/in/<slug>`` (no ``www.``). The driver placed it
    correctly and Workday refused the page with:
        "Error - If you would like us to view your LinkedIn profile, provide
         your profile URL (i.e. https://www.linkedin.com/in/YOURNAME)."

    SPELLING normalization only — the host and path are preserved, so this can
    only ever point at the SAME profile. A non-LinkedIn or unparseable value is
    returned untouched rather than coerced."""
    if not isinstance(url, str) or not url.strip():
        return url
    m = _LINKEDIN_RE.match(url)
    if not m:
        return url
    return "https://www.linkedin.com/" + m.group(1)


def _application_email(user_email: str, contact: Dict[str, Any]) -> Optional[str]:
    """The address to put on an APPLICATION FORM.

    The managed alias when this deployment can actually receive mail at it,
    otherwise the résumé's contact address.

    WHY THE ALIAS. The whole point of the managed mailbox is that employer mail
    lands somewhere WE can act on: verification links, OTPs and confirmations
    drive the freeze/thaw loop automatically. Resolving this from
    ``contact.email`` put the user's PERSONAL address on every application, so
    an employer's verification mail went to an inbox the platform cannot read
    and the loop could never close without the user.

    Live evidence (ClickHouse, 2026-08-23): the submitted form carried the
    personal Gmail, Greenhouse accepted the application and immediately asked
    for email confirmation, and that OTP landed in the user's own inbox — the
    one place the automation cannot see. The row parked on `oracle_verify`.

    The user does NOT lose sight of anything: the alias forwards to their
    registered address by default (`managed_mailbox_service.provision`), and the
    in-app inbox keeps every message.

    Returns None when there is no usable alias, and the CALLER then leaves the
    already-resolved value alone. Returning the résumé address here instead
    would overwrite a DECLARED ``user_preferences.email`` and invert the spec §2
    precedence (declared > résumé) — which is exactly what
    ``test_declared_pref_beats_resume`` caught."""
    del contact  # resolution order is the caller's; this only offers the alias
    try:
        from services import managed_mailbox_service as mailbox
        if not mailbox.can_receive_mail():
            return None
        return (mailbox.apply_alias(user_email) or "").strip() or None
    except Exception as e:  # pragma: no cover - never block resolution on this
        logger.warning("answer_library: managed alias lookup failed (%s) — "
                       "keeping the already-resolved address", type(e).__name__)
        return None


def _load_canonical(user_email: str,
                    company: Optional[str] = None) -> Dict[str, Any]:
    """Assemble the user's DECLARED canonical values from real sources, in the
    order the repair spec §2 fixes:

      1. ``user_preferences[field_id]`` — the flat declared keys onboarding writes
      2. ``visa_profiles``              — derived work-auth (`_derive_legal_from_visa`)
      3. ``resume_assets.structured``   — contact, experience[], education[],
                                          address decomposition
      4. (in `resolve`) ``application_answer_library`` — remembered answers
      5. (in `resolve`) protected-EEO ``decline_default`` → auto-decline
      6. otherwise → unresolved → ask

    ``company`` (optional) is the employer being applied to; it enables the
    résumé "have you worked here before" ESCALATION (spec §1 — Yes only).

    Returns `values` (non-gated, safe to auto-fill), `legal` (protected-EEO /
    work-auth — ONLY keys the user actually declared) and `escalations`.
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

    try:
        from services import application_question_bank as qb
        gated = qb.gated_field_ids()
    except Exception as e:  # pragma: no cover - parallel build / import issue
        logger.warning("answer_library: question_bank unavailable: %s", e)
        qb, gated = None, frozenset()

    # (1) DECLARED prefs win over everything derived — the generic sweep that
    # makes every catalog field resolvable, not just the ~9 contact fields.
    values_declared = _sweep_declared_prefs(values, prefs, qb, gated)
    _reconcile_name_parts(values, prefs)
    # (3) Résumé-derived facts fill only what the declared prefs left empty.
    _fill_from_resume(values, structured, contact)
    # (3b) Restate declared location as the two facts employers ask for.
    _derive_location_facts(values)
    _derive_signature_date(values)
    _derive_ai_agent_disclosure(values)
    # (3c) The address that goes ON THE FORM is the MANAGED alias — applied
    # AFTER the declared-prefs sweep, which would otherwise reinstate the
    # personal address and send every employer's verification mail to an inbox
    # the platform cannot read. See _application_email.
    _app_email = _application_email(user_email, contact)
    if _app_email:
        values["email"] = _app_email
    # (3d) LinkedIn URLs must carry scheme + www or Workday rejects the page.
    if values.get("linkedin_url"):
        values["linkedin_url"] = _canonical_linkedin(values["linkedin_url"])

    legal = _load_legal(prefs, user_email)
    escalations = _resume_company_escalations(structured, company)
    # Best-effort source versions for the answer provenance (never load-bearing).
    values_version = (resume_doc.get("parsed_at") or resume_doc.get("resume_id")
                      or resume_doc.get("_id"))
    legal_version = prefs.get("updated_at") or prefs.get("_id") or "declared"
    return {"values": values, "legal": legal, "escalations": escalations,
            "values_declared": values_declared,
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
                       tokens: FrozenSet[str],
                       field_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Return a remembered answer for this question.

    Order: the stable catalog ``field_id`` (the strongest key — the SAME question
    asked in different words on a different ATS still hits), then the exact
    ``norm_key``, then a bounded fuzzy token-overlap scan of the user's own
    entries. Suggested index: ``(user_email, field_id)`` and the existing unique
    ``(user_email, norm_key)``."""
    if field_id and isinstance(field_id, str):
        try:
            rec = _library().find_one(
                {"user_email": user_email, "field_id": field_id}, {"_id": 0},
                sort=[("updated_at", -1)],
            )
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("answer_library: user-store field lookup failed: %s", e)
            rec = None
        if rec and rec.get("answer") not in (None, ""):
            return rec

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
            "has_novel_excluding_generation": False,
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


def _policy_decline_value(qb: Any, field_id: str, bank_meta: Dict[str, Any],
                          form_options: Optional[List[str]]) -> Optional[Any]:
    """Spec §0 rule 3: the ONE auto-default allowed without a declared value.

    Returns the exact option string to submit for a PROTECTED EEO field whose
    catalog ``default_policy`` is ``decline_default``, or None. "Prefer not to
    answer" asserts nothing about the candidate, so selecting it fabricates
    nothing — and it is what a privacy-respecting default should be anyway.

    HARD SCOPE — this NEVER applies to work authorization, sponsorship,
    citizenship, export-control person status, age, criminal history, background/
    drug-test consent or any other legal attestation: those carry
    ``required_onboarding`` / ``never_default`` / ``confirm_default`` policies and
    a non-EEO category, and still require a DECLARED value.

    ``form_options`` is THIS form's live option text (from recon); we option-match
    the canonical decline against it so we submit the form's own wording. If the
    form offers no decline option at all we return None → the field stays
    unresolved and the user is asked."""
    if qb is None or not isinstance(bank_meta, dict):
        return None
    if bank_meta.get("category") != qb.CAT_EEO:
        return None
    if bank_meta.get("sensitivity") != qb.SENS_PROTECTED:
        return None
    if bank_meta.get("default_policy") != qb.POLICY_DECLINE_DEFAULT:
        return None
    options = list(form_options or bank_meta.get("options") or [])
    match = qb.match_option(field_id, qb.DECLINE, options)
    return match["option"] if match else None


def _policy_safe_default(qb: Any, field_id: str, bank_meta: Dict[str, Any],
                        form_options: Optional[List[str]]) -> Any:
    """The value for a SAFE field whose catalog policy answers it without a
    declared value, or None.

    Two cases, both of which ASSERT NOTHING FALSE about the candidate:

      * ``accept_default``  — acknowledging a privacy notice, or certifying that
        answers already given are true. The certification IS true: every answer
        we submit is either declared by the user or read off their résumé.
      * ``decline_default`` on a SAFE, non-EEO field — a marketing/SMS opt-in.
        Declining asserts nothing and is the privacy-preserving default.

    HARD SCOPE — mirrors ``_policy_decline_value``. This NEVER applies to work
    authorization, sponsorship, citizenship, export-control person status, age,
    criminal history or any other legal attestation: those are GATED
    (``SENS_PROTECTED``/``SENS_LEGAL``) and never reach this path, and they are
    re-checked here anyway. This must never become a blanket default map —
    see AUTOAPPLY_REPAIR_SPEC.md §0.4.
    """
    if qb is None or not isinstance(bank_meta, dict):
        return None
    if bank_meta.get("sensitivity") != qb.SENS_SAFE:
        return None
    if bank_meta.get("category") in (qb.CAT_WORK_AUTH, qb.CAT_EEO):
        return None
    policy = bank_meta.get("default_policy")
    if policy == qb.POLICY_ACCEPT_DEFAULT:
        want = True
    elif policy == qb.POLICY_DECLINE_DEFAULT:
        want = False
    else:
        return None
    if bank_meta.get("value_type") == qb.VT_BOOLEAN and not (form_options or []):
        return want
    wanted = ("yes", "y", "i agree", "agree", "i acknowledge", "acknowledge",
              "i accept", "accept", "true") if want else (
              "no", "n", "false", "decline", "opt out")
    for o in list(form_options or bank_meta.get("options") or []):
        if isinstance(o, str) and o.strip().lower() in wanted:
            return o
    return want


def _escalated_yes(bank_meta: Dict[str, Any], qb: Any) -> Any:
    """The "Yes" value shaped for this field — a real bool for a BOOLEAN field,
    the form's exact "Yes" option string for a SELECT."""
    meta = bank_meta or {}
    if qb is not None and meta.get("value_type") == qb.VT_BOOLEAN:
        return True
    for o in (meta.get("options") or []):
        if isinstance(o, str) and o.strip().lower() in ("yes", "y"):
            return o
    return True


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


def resolve(user_email: str, questions: List[Any], *,
            company: Optional[str] = None) -> Dict[str, Any]:
    """Resolve a batch of form questions against the seed mappings + the user's
    canonical profile + their remembered answers.

    ``company`` (keyword-only, optional) is the employer being applied to. Pass
    it from the gather path (``application['company']``) to enable the spec §1
    résumé escalation: if the user's résumé shows they worked at this company,
    "Have you previously been employed here?" answers **Yes** and is flagged for
    review. Omitting it is always safe — the escalation simply does not fire.

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
            "has_novel_free_text": bool,          # incl. the generatable essays
            "has_novel_excluding_generation": bool,  # the residue after generation
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
        return _resolve_normalized(user_email, questions, company=company)
    return _resolve_legacy(user_email, questions, company=company)


# Control shapes that are genuinely free prose. A question whose shape is
# anything else — or unknown — is NOT eligible for generation.
_PROSE_VALUE_TYPES = frozenset({"essay", "textarea", "longtext", "long_text",
                                "multiline", "paragraph"})
# Length above which an unlabelled control is treated as prose anyway: ATS
# recon often reports a bare `text` type for a long question, and a 60+
# character question that also has no options is not a dropdown.
_PROSE_MIN_LABEL = 60


# Canonical value types that are a SHORT SCALAR — a URL, a date, a phone
# number. None of them can be a truthful answer to a paragraph question.
_SCALAR_VALUE_TYPES = frozenset({"url", "date", "phone", "email", "number",
                                 "money", "years"})

# The legacy `_categorize` path reports a CATEGORY rather than a value type.
# These are its short-scalar categories — the ones whose stored value is a
# link, a date, a phone number or an amount.
_LEGACY_SCALAR_CATEGORIES = frozenset({
    CAT_LINKS, CAT_START_DATE, CAT_CONTACT, CAT_SALARY, CAT_YEARS, CAT_NOTICE,
})


def _scalar_answer_to_prose_question(item: Dict[str, Any],
                                     value_type: Any) -> bool:
    """Would this fill put a short scalar into a paragraph box?

    A live failure: the classifier matched the word "portfolio" inside

        "Our team values written communication above all. Please share a link
         to something you wrote and say why you chose it."

    and resolved it to `portfolio_url`, so the application would have
    submitted the bare string "manneharshithsiddardha.com" as the answer to a
    120-character essay. Loose token matching cannot tell a mention from a
    request. When the shape of the question and the shape of the answer
    disagree this badly, the honest move is to generate a real answer (or ask)
    rather than submit a URL into a paragraph.
    """
    return bool(_is_open_prose(item)
                and str(value_type or "").lower() in _SCALAR_VALUE_TYPES)


def _value_fits_options(field_id: str, value: Any, options: Any) -> bool:
    """Is `value` one of the options THIS form will actually accept?

    Only consulted when recon captured the form's real option list. A second
    live failure: "Which shift are you available for?" matched the token
    "available" and resolved to `available_start_date`, so a dropdown of
    Day/Night would have been answered "2026-07-06". That is the exact loop
    `auto_apply_service._needs_control_meta` documents — the fill driver
    reports `no_option_match`, the row re-parks, AND the bad value is banked
    in the answer library and poisons every future application. Failing the
    match here keeps it out of the answer entirely.
    """
    if not options or not isinstance(options, list):
        return True          # no option list captured: nothing to check against
    if value in (None, ""):
        return False
    try:
        from services import application_question_bank as qb
        return qb.match_option(field_id, value, options) is not None
    except Exception:        # pragma: no cover - never block on a helper fault
        return True


def _is_open_prose(item: Dict[str, Any]) -> bool:
    """Is this unrecognised question a free-prose box we may generate into?

    Deliberately conservative: an enumerated control must never reach the
    generator, so the presence of ANY options list disqualifies the question
    outright, and an unknown shape only qualifies on a long question with no
    options at all.
    """
    if item.get("options"):
        return False
    vt = item.get("value_type") or ""
    if vt in _PROSE_VALUE_TYPES:
        return True
    if vt:
        # A declared shape that is not prose (select, radio, date, number...).
        return False
    return len(str(item.get("label") or "")) >= _PROSE_MIN_LABEL


def _resolve_normalized(user_email: str,
                        questions: List[Dict[str, Any]], *,
                        company: Optional[str] = None) -> Dict[str, Any]:
    """The canonical field-id-keyed resolution (see `resolve`)."""
    resolved: Dict[str, Dict[str, Any]] = {}
    unresolved: List[str] = []
    needs_generation: List[Dict[str, Any]] = []
    has_novel = False
    has_unconfirmed_legal = False

    # De-duped, sanitized view of the normalized questions. Each entry is a dict
    # (see the keys set below) — the tuple form grew past readability once the
    # form's live ``options`` and the raw label had to travel with it.
    prepared: List[Dict[str, Any]] = []
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
        # THIS form's live option strings (when recon captured them) — used to
        # pick the form's own wording for the protected-EEO auto-decline.
        raw_opts = q.get("options")
        options = ([InputSanitizer.sanitize_string(str(o), max_length=200)
                    for o in raw_opts[:50] if o not in (None, "")]
                   if isinstance(raw_opts, list) else None)

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
        prepared.append({"field_id": field_id, "label": label, "norm": norm,
                         "tokens": tokens, "required": required,
                         "flagged_legal": flagged_legal, "invert": invert,
                         "mapping": mapping, "options": options,
                         # The form's own control shape. Carried so an
                         # UNRECOGNISED question can be routed to grounded
                         # generation only when it is genuinely a prose box —
                         # see the open-ended branch below.
                         "value_type": InputSanitizer.sanitize_string(
                             str(q.get("value_type") or q.get("type") or ""),
                             max_length=40).lower()})

    if not prepared:
        return _empty_resolution()

    canonical = _load_canonical(user_email, company=company)
    values, legal = canonical["values"], canonical["legal"]
    escalations = canonical.get("escalations") or {}
    values_declared = canonical.get("values_declared") or set()
    values_version = canonical.get("values_version")
    legal_version = canonical.get("legal_version")
    try:
        from services import application_question_bank as qb
    except Exception as e:  # pragma: no cover - parallel build / import issue
        logger.warning("answer_library: question_bank unavailable: %s", e)
        qb = None

    required_total = 0
    required_resolved = 0

    for item in prepared:
        field_id = item["field_id"]
        norm, tokens = item["norm"], item["tokens"]
        required, flagged_legal = item["required"], item["flagged_legal"]
        invert, mapping = item["invert"], item["mapping"]
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
            # Deliberately does NOT set `has_novel`: the returned
            # `has_novel_free_text` already ORs in `needs_generation`, and keeping
            # the two sources apart is what lets the gather path recompute the
            # flag after generation instead of staying penalised for questions it
            # just answered. See `has_novel_excluding_generation` below.
            if bank_meta["category"] == qb.CAT_FREE_TEXT:
                needs_generation.append({"field_id": field_id,
                                         "question": item["label"] or norm,
                                         "label": norm,
                                         "category": CAT_ESSAY, "kind": KIND_ESSAY})
                continue

            if required:
                required_total += 1

            if gated:
                # DECLARED data only. A declared "decline" IS explicit → allowed.
                val = legal.get(field_id)
                src, sv = SOURCE_DECLARED, legal_version
                if val in (None, ""):
                    # The user answered this exact attestation once before (via
                    # the needs-input queue) — their own stated value, not a
                    # guess. Same rule as the non-gated path.
                    stored = _lookup_user_store(user_email, norm, tokens, field_id)
                    if stored is not None:
                        val = stored.get("answer")
                        src = SOURCE_USER_STORE
                        sv = str(stored.get("updated_at") or "") or None
                if val not in (None, ""):
                    out_val = _apply_invert(val, vtype, invert)
                    resolved[field_id] = _resolution(
                        field_id, norm, out_val, src, b_category, b_kind,
                        sensitivity, review_required=True, source_version=sv)
                    if required:
                        required_resolved += 1
                    continue
                # Spec §0 rule 3 / §2 step 5 — the ONE auto-default allowed with
                # no declared value: a PROTECTED EEO field whose catalog policy is
                # decline_default gets the form's own "prefer not to answer"
                # option. Declining ASSERTS NOTHING, so it fabricates nothing —
                # and it stops every undeclared EEO field from setting
                # has_unconfirmed_legal_attestation, which hard-blocks the whole
                # application in autonomy_service.decide().
                auto = _policy_decline_value(qb, field_id, bank_meta,
                                             item.get("options"))
                if auto is not None:
                    resolved[field_id] = _resolution(
                        field_id, norm, auto, SOURCE_POLICY_DECLINE, b_category,
                        b_kind, sensitivity, review_required=True,
                        confidence=0.9, source_version="policy")
                    if required:
                        required_resolved += 1
                else:
                    unresolved.append(field_id)
                    has_unconfirmed_legal = True
                continue

            # Non-gated: canonical profile, then the per-user remembered store.
            # A value the user DECLARED at onboarding carries the stronger
            # ``declared`` provenance; ``profile`` means "read off the résumé".
            val = values.get(field_id)
            if field_id in values_declared:
                src, sv = SOURCE_DECLARED, legal_version
            else:
                src, sv = SOURCE_PROFILE, values_version
            if val in (None, ""):
                stored = _lookup_user_store(user_email, norm, tokens, field_id)
                if stored is not None:
                    val = stored.get("answer")
                    src, sv = SOURCE_USER_STORE, str(stored.get("updated_at") or "") or None
            # Spec §1 — the résumé may ONLY escalate a worked-here question to
            # "Yes"; it may NEVER assert "No" beyond what the user declared. So a
            # truthy declared value stands, and a falsy/absent one is overridden
            # ONLY upward, always flagged for review.
            escalated = False
            if escalations.get(field_id) is True and _to_bool(val) is not True:
                val = _escalated_yes(bank_meta, qb)
                src, sv, escalated = SOURCE_RESUME_MATCH, None, True
            # Shape guards — a resolved value must fit the QUESTION, not just
            # the field name the classifier matched. Both of these were live
            # wrong-answer paths; see the helpers for the exact forms.
            if val not in (None, "") and _scalar_answer_to_prose_question(item, vtype):
                needs_generation.append({"field_id": field_id,
                                         "question": item["label"] or norm,
                                         "label": norm,
                                         "category": CAT_ESSAY, "kind": KIND_ESSAY,
                                         "open_ended": True})
                if required:
                    required_total -= 1
                continue
            if val not in (None, "") and not _value_fits_options(
                    field_id, _apply_invert(val, vtype, invert), item.get("options")):
                unresolved.append(field_id)
                has_novel = True
                continue

            if val not in (None, ""):
                out_val = _apply_invert(val, vtype, invert)
                review = (sensitivity != qb.SENS_SAFE) or escalated or (
                    src == SOURCE_USER_STORE)
                hit = _resolution(
                    field_id, norm, out_val, src, b_category, b_kind, sensitivity,
                    review_required=review, source_version=sv,
                    confidence=0.8 if escalated else 1.0)
                if escalated:
                    hit["escalated_by_resume"] = True
                resolved[field_id] = hit
                if required:
                    required_resolved += 1
            else:
                # A SAFE field the catalog can answer on policy alone (consent
                # acknowledgement -> accept, marketing opt-in -> decline). Both
                # assert nothing false, so neither needs to interrupt the user.
                # Measured at 13.6% of unclassifiable questions — see
                # PHASE1-question-catalog-gaps.md.
                safe = _policy_safe_default(qb, field_id, bank_meta,
                                            item.get("options"))
                if safe is not None:
                    resolved[field_id] = _resolution(
                        field_id, norm, safe, SOURCE_POLICY_SAFE, b_category,
                        b_kind, sensitivity, review_required=False,
                        confidence=0.9, source_version="policy")
                    if required:
                        required_resolved += 1
                    continue
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
            # Unrecognized, non-legal. If the form is asking for PROSE, this is
            # answerable from the resume even though no alias matched, so route
            # it to grounded generation instead of stranding it.
            #
            # This was the dead end behind the live one-tap queue: an employer
            # asks "Describe a system you designed end to end" or "What
            # interests you about our infrastructure team", no alias matches,
            # and the row parks forever with the user told to write it. The
            # generator is résumé-grounded and marks every answer
            # `review_required`, so nothing is submitted unseen.
            #
            # STRICTLY prose only. A select/radio/checkbox/date/number needs an
            # exact option the form will accept; a generated string there fails
            # `no_option_match`, re-parks the row AND banks a poisoned answer
            # for every future application (the loop `_needs_control_meta`
            # documents). Absent metadata is treated as NOT prose.
            if (_is_open_prose(item)
                    and not (qb is not None and qb.is_gated(field_id))):
                needs_generation.append({"field_id": field_id,
                                         "question": item["label"] or norm,
                                         "label": norm,
                                         "category": CAT_ESSAY,
                                         "kind": KIND_ESSAY,
                                         # Tells the generator this id is not in
                                         # its closed catalog but is safe to
                                         # answer. Nothing else may set it.
                                         "open_ended": True})
                continue
            if required:
                required_total += 1
            unresolved.append(field_id)
            has_novel = True
            continue

        is_legal = flagged_legal or (category in LEGAL_CATEGORIES)

        # Essays: grounded generation, never counted in coverage, never here.
        # `has_novel` intentionally untouched — see the CAT_FREE_TEXT branch.
        if kind == KIND_ESSAY and not is_legal:
            needs_generation.append({"field_id": field_id,
                                     "question": item["label"] or norm,
                                     "label": norm,
                                     "category": category, "kind": KIND_ESSAY})
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
        # Same two shape guards as the bank path above. This is the branch that
        # actually produced both live wrong answers, because `_categorize` is
        # the loosest classifier in the chain: it matched "portfolio" inside a
        # 120-character essay prompt and "available" inside "Which shift are
        # you available for?".
        if val not in (None, "") and _scalar_answer_to_prose_question(
                item, "url" if category in _LEGACY_SCALAR_CATEGORIES else ""):
            needs_generation.append({"field_id": field_id,
                                     "question": item["label"] or norm,
                                     "label": norm,
                                     "category": CAT_ESSAY, "kind": KIND_ESSAY,
                                     "open_ended": True})
            if required:
                required_total -= 1
            continue
        if val not in (None, "") and not _value_fits_options(
                field_id, val, item.get("options")):
            unresolved.append(field_id)
            has_novel = True
            continue
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
        # The SAME flag with the generatable essays excluded. `has_novel_free_text`
        # is the pre-generation view (unchanged for every existing caller); this is
        # the residue that grounded generation can never clear — an unrecognised
        # field, or a known field with no declared value. The gather path ORs this
        # with the essays generation could NOT answer, so answering "Why this
        # company?" for the user actually improves the autonomy score instead of
        # leaving it penalised for a question that is no longer open.
        "has_novel_excluding_generation": bool(has_novel),
        "has_unconfirmed_legal_attestation": has_unconfirmed_legal,
    }


def _resolve_legacy(user_email: str, questions: List[str], *,
                    company: Optional[str] = None) -> Dict[str, Any]:
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

    canonical = _load_canonical(user_email, company=company)
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
def remember(user_email: str, question: str = "", answer: Any = None, *,
             field_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Store the user's own answer to a question so it is NEVER asked again.

    ── CONTRACT for `POST /api/apply/needs-input/answer` (repair spec §3) ───────
        remember(user_email: str,
                 question: str = "",          # the label shown to the user
                 answer: Any = None,          # str | bool | int | float
                 *, field_id: Optional[str] = None) -> Optional[dict]

    Either ``question`` OR ``field_id`` is enough:
      * ``field_id`` — a canonical `application_question_bank` id
        (``notice_period``, ``previous_employee``, …). The catalog label is used
        as the question text when none is given, and the stored row is keyed by
        the field_id, so the SAME question asked in different words on a
        different ATS resolves straight from this store next time
        (``source: "user_store"``).
      * ``question`` — a raw novel label, for a question with no catalog id.

    Returns the stored view ``{question, norm_key, field_id, answer, field,
    category, kind}`` or **None** when the input is unusable / deliberately not
    banked. Never raises — a store failure degrades to None (the caller keeps the
    answer in ``auto_apply_answers``; only the never-ask-again memo is lost).

    Persistence: upsert on ``(user_email, norm_key)`` in
    ``application_answer_library``. Suggested indexes: unique
    ``(user_email, norm_key)`` and ``(user_email, field_id)``.

    NOT banked (returns None): essays / free-text (they are per-JD generated —
    see `application_answers` and `answer_generator.generate_free_text_answers`),
    empty answers, and unknown ``field_id`` values that are not in the catalog.

    NOTE for the endpoint: for a CATALOG field prefer calling
    `apply_onboarding_service.declare_fields` **as well** — that writes the flat
    ``user_preferences[field_id]`` key, which upgrades the answer's provenance
    from ``user_store`` to ``declared`` and makes it visible in onboarding.
    `remember_many(..., declare=True)` does both in one call.
    """
    if not user_email:
        return None

    # A catalog field_id is the strongest key we can store under. Validate it
    # against the CLOSED catalog before it goes anywhere near a Mongo filter.
    bank_meta = None
    fid = InputSanitizer.sanitize_string(str(field_id or ""), max_length=200)
    if fid:
        try:
            from services import application_question_bank as qb
            bank_meta = qb.get_field(fid)
        except Exception as e:  # pragma: no cover - parallel build
            logger.warning("answer_library: question_bank unavailable: %s", e)
            bank_meta = None
        if bank_meta is None:
            fid = ""

    q = InputSanitizer.sanitize_string(question or "", max_length=2000)
    if not q and bank_meta is not None:
        q = InputSanitizer.sanitize_string(str(bank_meta.get("label") or fid),
                                           max_length=2000)
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

    if bank_meta is not None:
        category, kind = _bank_category_kind(fid, bank_meta)
        field = fid
        # `qb` is bound whenever bank_meta is not None (see the lookup above).
        is_essay = (bank_meta.get("value_type") == qb.VT_ESSAY
                    or bank_meta.get("category") == qb.CAT_FREE_TEXT)
    else:
        mapping = _categorize(norm, tokens)
        field = mapping[0] if mapping else None
        category = mapping[1] if mapping else None
        kind = mapping[2] if mapping else KIND_NOVEL
        is_essay = (kind == KIND_ESSAY)

    # Do not bank essays here (they belong to the per-company generation history).
    if is_essay:
        return None

    now = datetime.now(timezone.utc)
    doc = {
        "user_email": user_email,
        "norm_key": norm,
        "question": q,
        "answer": stored_answer,
        "tokens": sorted(tokens),
        "field": field,
        "field_id": fid or None,
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
    return {"question": q, "norm_key": norm, "field_id": fid or None,
            "answer": stored_answer, "field": field, "category": category,
            "kind": kind}


def remember_many(user_email: str, answers: Dict[str, Any], *,
                  labels: Optional[Dict[str, str]] = None,
                  declare: bool = True) -> Dict[str, Any]:
    """Bulk `remember()` — the ONE call `POST /api/apply/needs-input/answer`
    should make after merging the user's answers into ``auto_apply_answers``.

    ── CONTRACT ────────────────────────────────────────────────────────────────
        remember_many(user_email: str,
                      answers: Dict[str, Any],        # {field_id_or_label: value}
                      *,
                      labels: Optional[Dict[str, str]] = None,
                      declare: bool = True) -> Dict[str, Any]

        returns {"remembered": [field_id_or_key, ...],
                 "declared":   [catalog_field_id, ...],
                 "skipped":    {key: reason, ...}}

    ``answers`` is keyed by catalog ``field_id`` where one exists (the
    ``remaining[].field_id`` the needs-input queue handed the FE), else by the
    raw question label. ``labels`` optionally supplies the human question text
    per key (used when the key is a field_id and you want the form's own
    wording banked).

    ``declare=True`` (default) ALSO writes every recognised catalog field to the
    flat ``user_preferences[field_id]`` declared store via
    `apply_onboarding_service.declare_fields(strict=False)`, so the answer
    resolves as ``source: "declared"`` from then on and shows up in the
    onboarding profile. Values that fail the catalog's type/option validation are
    reported in ``skipped`` and are still remembered in the answer library.

    Never raises. Total function — a bad key is reported, not thrown."""
    out: Dict[str, Any] = {"remembered": [], "declared": [], "skipped": {}}
    if not user_email or not isinstance(answers, dict):
        return out
    labels = labels if isinstance(labels, dict) else {}

    try:
        from services import application_question_bank as qb
    except Exception as e:  # pragma: no cover - parallel build
        logger.warning("answer_library: question_bank unavailable: %s", e)
        qb = None

    catalog_answers: Dict[str, Any] = {}
    for raw_key, value in list(answers.items())[:_MAX_QUESTIONS]:
        key = InputSanitizer.sanitize_string(str(raw_key or ""), max_length=2000)
        if not key:
            out["skipped"][str(raw_key)[:80]] = "empty_key"
            continue
        is_catalog = qb is not None and qb.get_field(key) is not None
        stored = remember(
            user_email,
            labels.get(raw_key) or ("" if is_catalog else key),
            value,
            field_id=key if is_catalog else None,
        )
        if stored is None:
            out["skipped"][key] = "not_banked"
        else:
            out["remembered"].append(key)
        if is_catalog:
            catalog_answers[key] = value

    if declare and catalog_answers:
        try:
            from services import apply_onboarding_service as onboarding
            res = onboarding.declare_fields(user_email, catalog_answers,
                                            strict=False)
            out["declared"] = res.get("declared") or []
            for fid, reason in (res.get("rejected") or {}).items():
                out["skipped"].setdefault(fid, reason)
        except Exception as e:  # pragma: no cover - defensive
            # The library memo already landed; losing the declared write only
            # costs provenance, never correctness.
            logger.warning("answer_library: declare_fields failed for %s: %s",
                           user_email, e)
    return out
