"""Single source of truth for shared résumé-prompt rules (Craft-overhaul Part D).

An audit found ~19 collisions where different prompts (tailor, correction,
regenerate, the four content-augmenter LLM calls, project generator) stated the
SAME rule differently — or fought the deterministic guards outright (e.g. the
augmenter told the model to emit `~15%`/`1K+`, the exact tokens the tailor
banned, IntegrityGuard strips, and the scorer penalizes).

This module is the reconciliation kernel: each rule lives here ONCE and every
prompt interpolates it. Changing a rule means editing one constant, so prompts
can no longer drift apart. `tests/test_prompt_consistency.py` locks this in.

Authorities these blocks mirror (do not weaken):
  - Banned AI-tell symbols  ← IntegrityGuard._AI_TELL_SUBSTITUTIONS + fix_unicode_dashes
  - Banned phrases          ← resume_tailor.BANNED_PHRASES (imported, never re-listed)
  - Metric honesty          ← tailor HONEST QUANTIFICATION MENU + tailor_verifier + claim_guard
  - Terminal period / no `;` ← IntegrityGuard.fix_terminal_periods + project quality gate
  - Verb diversity          ← tailor VERB DIVERSITY
  - Bullet length           ← one band per section-type, stated here
  - Project cap             ← IntegrityGuard project cap (= 3)
"""
from __future__ import annotations

# Import (never re-list) the canonical banned-phrase tuple so a single edit in
# resume_tailor propagates everywhere.
from services.resume_tailor import BANNED_PHRASES

# ---------------------------------------------------------------------------
# Numeric quantities (single home for every "how long / how many" number)
# ---------------------------------------------------------------------------

# One bullet-length band per section-type. Previously four disagreeing windows
# (150-200 / 150-220 / 100-150 / <=250) existed across prompts.
EXPERIENCE_BULLET_MIN_CHARS = 120
EXPERIENCE_BULLET_MAX_CHARS = 200
PROJECT_BULLET_MIN_CHARS = 100
PROJECT_BULLET_MAX_CHARS = 150

# Project cap — mirrors IntegrityGuard's hard cap. Prompts must not ask for more.
MAX_PROJECTS = 3

# Verb-diversity cap — no single opening verb may start more than this many
# bullets across the whole résumé.
MAX_SAME_OPENING_VERB = 2

# JD keyword coverage band — deliberately NOT 100% (anti-stuffing).
KEYWORD_COVERAGE_LOW = 75
KEYWORD_COVERAGE_HIGH = 80

# Summary length — one band, matching the deferred summary writer
# (services.summary_tailor). Previously the augmenter alignment pass said
# "EXACTLY 3-4 sentences", fighting the tailor's "2-3 sentences / <=60 words".
SUMMARY_MIN_SENTENCES = 2
SUMMARY_MAX_SENTENCES = 3
SUMMARY_MAX_WORDS = 60
SUMMARY_RULE = (
    f"The summary is {SUMMARY_MIN_SENTENCES}-{SUMMARY_MAX_SENTENCES} clear, "
    f"confident sentences (max ~{SUMMARY_MAX_WORDS} words) that read as the "
    "candidate's own self-description."
)

# ---------------------------------------------------------------------------
# Rule text blocks (interpolate these into every prompt that touches bullets)
# ---------------------------------------------------------------------------

# Banned AI-tell numeric/dash symbols. Mirrors tailor HARD CONSTRAINT D.4 and the
# deterministic strips in IntegrityGuard. Any prompt that writes or rewrites
# bullets MUST include this so it cooperates with the guard instead of emitting
# tokens the guard later mangles.
BANNED_SYMBOLS_RULE = (
    "REALISTIC, HUMAN-WRITTEN NUMERIC FORMATTING — these symbols read AI-generated "
    "and must NOT appear:\n"
    "  - `K`/`M` shorthand is fine at scale ('120K msgs/sec', '5M users'); do NOT "
    "glue '+' on as hedging — write '120K msgs/sec' NOT '120K+ msgs/sec'. Reserve "
    "'+' for genuine 'or more' wording already in the source.\n"
    "  - NEVER use `~` (tilde). Write 'about 5,000' or '5,000', never '~5,000'.\n"
    "  - NEVER write `sub-N` / `sub-Nms` ('sub-100ms', 'sub-second'). Write "
    "'under 100ms' or '100ms'.\n"
    "  - NEVER coin hedging compounds ('near-zero', 'near-100%').\n"
    "  - NEVER use en-dash `–` or em-dash `—` anywhere — use ASCII hyphen `-`.\n"
    "  - Use commas in large bare numbers: '120,000' not '120000'."
)

# Metric honesty — grounded-or-none. Replaces the augmenter's old "invent a
# realistic metric for every bullet" instruction, which manufactured exactly the
# ungrounded numbers tailor_verifier and claim_guard exist to catch.
METRIC_HONESTY_RULE = (
    "METRIC HONESTY — never invent numbers. Preserve every number already in the "
    "source. When a bullet has real impact but no source number, quantify a "
    "dimension that IS grounded (scope: users/team/data volume; cadence: "
    "'weekly close', 'daily deploys'; frequency of the work) ONLY if stated or "
    "clearly implied in the source. If nothing is grounded, write a concrete "
    "unnumbered outcome ('eliminated the manual reconciliation step') — a specific "
    "outcome beats a fabricated metric every time."
)

# Terminal punctuation + no semicolon splices. Mirrors fix_terminal_periods and
# the project quality gate. Stated so prompts stop emitting the very endings the
# guard has to clean up.
TERMINAL_PUNCTUATION_RULE = (
    "Every bullet and every summary sentence MUST end with a period ('.'). Do NOT "
    "end a bullet with a semicolon, comma, colon, or no punctuation, and do NOT "
    "use a semicolon to splice two clauses into one bullet — write two bullets or "
    "one clean sentence."
)

# Verb diversity + quality. Mirrors tailor VERB DIVERSITY. Note: excludes the
# 'robotic' verbs the tailor prompt flags (Spearheaded/Architected/Engineered/
# Orchestrated) from the recommended list.
VERB_DIVERSITY_RULE = (
    "Every bullet starts with a strong, specific action verb (Built, Designed, "
    "Reduced, Automated, Migrated, Shipped, Optimized, Integrated, Led, "
    f"Streamlined, Refactored). No single verb may open more than "
    f"{MAX_SAME_OPENING_VERB} bullets across the whole résumé. Avoid overusing "
    "'Spearheaded', 'Architected', 'Engineered', or 'Orchestrated' — they read robotic."
)

# Voice — first-person-implied, never third-person 'candidate'. Mirrors tailor
# HARD CONSTRAINT E. Belongs on any prompt that rewrites summary OR bullets.
VOICE_RULE = (
    "Write in first-person-implied voice (no 'I' pronoun, describing the "
    "candidate's own work). NEVER write in the third person about 'the candidate' "
    "/ 'this candidate', and NEVER open with the JD's job title as the subject."
)


def experience_bullet_length_rule() -> str:
    return (
        f"Each experience bullet is {EXPERIENCE_BULLET_MIN_CHARS}-"
        f"{EXPERIENCE_BULLET_MAX_CHARS} characters (about two full lines rendered)."
    )


def project_bullet_length_rule() -> str:
    return (
        f"Each project bullet is {PROJECT_BULLET_MIN_CHARS}-"
        f"{PROJECT_BULLET_MAX_CHARS} characters, names exactly one technology, "
        "and ends with a period."
    )


def banned_phrases_rule() -> str:
    """Render the banned-phrase list from the canonical tuple (never hand-copy)."""
    joined = ", ".join(BANNED_PHRASES)
    return (
        "BANNED LANGUAGE — do not use any of these buzzwords/filler (they read "
        f"AI-generated): {joined}."
    )


# Convenience bundle: the full floor every bullet-writing/rewriting prompt should
# carry so it cooperates with the guards. Secondary prompts (correction,
# regenerate, augmenter phases, refiners) interpolate this.
def bullet_floor_rules() -> str:
    return "\n".join(
        (
            METRIC_HONESTY_RULE,
            BANNED_SYMBOLS_RULE,
            TERMINAL_PUNCTUATION_RULE,
            VERB_DIVERSITY_RULE,
            banned_phrases_rule(),
        )
    )
