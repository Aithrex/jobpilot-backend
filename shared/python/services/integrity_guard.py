"""
Integrity Guard — deterministic post-tailor validation and correction.

Runs AFTER the LLM returns a tailored resume and AFTER schema validation.
Compares the tailored output against the original parsed resume to enforce:

  1. Immutable fields (contact, experience facts, education facts) are unchanged
  2. No hallucinated experience or project entries
  3. Project generation rules (max 1 if original had none)
  4. Experience count matches original

Pure Python, zero AI calls, stateless.
"""
import copy
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Immutable field definitions
# ---------------------------------------------------------------------------

IMMUTABLE_CONTACT_FIELDS = ("name", "email", "phone", "linkedin", "github", "location", "portfolio")
IMMUTABLE_EXPERIENCE_FIELDS = ("company", "title", "dates", "location")
IMMUTABLE_EDUCATION_FIELDS = ("degree", "institution", "dates", "location", "gpa", "coursework")


# ---------------------------------------------------------------------------
# Report dataclass
# ---------------------------------------------------------------------------

@dataclass
class IntegrityReport:
    """Captures everything detected and fixed by the integrity guard."""
    immutable_overwrites: List[str] = field(default_factory=list)
    hallucinated_experience: List[Dict] = field(default_factory=list)
    hallucinated_projects: List[Dict] = field(default_factory=list)
    missing_experience_reinjected: List[Dict] = field(default_factory=list)
    banned_phrase_substitutions: List[str] = field(default_factory=list)
    unicode_dash_substitutions: List[str] = field(default_factory=list)
    ai_tell_substitutions: List[str] = field(default_factory=list)
    terminal_period_fixes: List[str] = field(default_factory=list)
    fabricated_certs_stripped: List[str] = field(default_factory=list)
    nonskill_traits_stripped: List[str] = field(default_factory=list)
    experience_count_mismatch: bool = False
    project_cap_enforced: bool = False
    original_experience_count: int = 0
    tailored_experience_count: int = 0
    severity: str = "clean"  # "clean", "auto_fixed", "needs_retry"


# ---------------------------------------------------------------------------
# Matching helpers
# ---------------------------------------------------------------------------

def _exp_key(entry: Dict[str, Any]) -> Tuple[str, str]:
    """Build a matching key from an experience entry: (company, title) lowered."""
    return (
        entry.get("company", "").lower().strip(),
        entry.get("title", "").lower().strip(),
    )


def _edu_key(entry: Dict[str, Any]) -> Tuple[str, str]:
    """Build a matching key from an education entry: (institution, degree) lowered."""
    return (
        entry.get("institution", "").lower().strip(),
        entry.get("degree", "").lower().strip(),
    )


def _proj_key(entry: Dict[str, Any]) -> str:
    """Build a matching key from a project entry: name lowered."""
    return entry.get("name", "").lower().strip()


def _match_entries(
    original_list: List[Dict],
    tailored_list: List[Dict],
    key_fn,
) -> Tuple[List[Tuple[Dict, Dict]], List[Dict], List[Dict]]:
    """Match tailored entries to original entries by a key function.

    Returns:
        matched: list of (original_entry, tailored_entry) pairs
        unmatched_tailored: tailored entries with no original match (hallucinated)
        missing_original: original entries not found in tailored (dropped by AI)
    """
    # Build a map from key -> list of original entries (handles duplicates)
    orig_map: Dict[Any, List[Dict]] = {}
    for entry in original_list:
        k = key_fn(entry)
        orig_map.setdefault(k, []).append(entry)

    matched: List[Tuple[Dict, Dict]] = []
    unmatched_tailored: List[Dict] = []

    for t_entry in tailored_list:
        k = key_fn(t_entry)
        if k in orig_map and orig_map[k]:
            o_entry = orig_map[k].pop(0)
            matched.append((o_entry, t_entry))
        else:
            unmatched_tailored.append(t_entry)

    # Remaining original entries that were never matched
    missing_original = [e for entries in orig_map.values() for e in entries]
    return matched, unmatched_tailored, missing_original


# ---------------------------------------------------------------------------
# IntegrityGuard
# ---------------------------------------------------------------------------

class IntegrityGuard:
    """Post-tailor deterministic integrity enforcement.

    All methods are pure — no LLM calls, no side effects beyond the
    returned corrected dict and report.
    """

    def enforce(
        self,
        original: Dict[str, Any],
        tailored: Dict[str, Any],
        jd_analysis: Dict[str, Any] = None,
    ) -> Tuple[Dict[str, Any], IntegrityReport]:
        """Full integrity pipeline.

        Returns (corrected_tailored, report).
        The corrected dict is safe to render/score/return to the client.

        jd_analysis (optional) enables stripping ungrounded JD-verbatim skills
        (keyword-stuffing). When omitted, that step is skipped.
        """
        tailored = copy.deepcopy(tailored)
        report = IntegrityReport()

        report.original_experience_count = len(original.get("experience", []))
        report.tailored_experience_count = len(tailored.get("experience", []))

        logger.warning(
            "IntegrityGuard.enforce ENTRY: original_projects=%d %s, tailored_projects=%d %s",
            len(original.get("projects", [])),
            [p.get("name", "?") for p in original.get("projects", [])],
            len(tailored.get("projects", [])),
            [p.get("name", "?") for p in tailored.get("projects", [])],
        )

        # Step 1: Force immutable fields
        tailored, overwrites = self.force_immutable_fields(original, tailored)
        report.immutable_overwrites = overwrites

        # Step 2: Detect and remove hallucinated entries
        tailored, hall_exp, hall_proj, reinjected = self.detect_hallucinations(
            original, tailored
        )
        report.hallucinated_experience = hall_exp
        report.hallucinated_projects = hall_proj
        report.missing_experience_reinjected = reinjected

        # Step 3: Enforce project generation rules
        tailored, cap_enforced = self.enforce_project_rules(original, tailored)
        report.project_cap_enforced = cap_enforced

        # Step 4: Force certifications to mirror the ORIGINAL parsed resume.
        # The parser is the single source of truth for certifications (its
        # prompt forbids fabrication). The tailor must NEITHER drop nor
        # invent certs — preserve them verbatim from the source.
        original_certs = original.get("certifications") or []
        tailored["certifications"] = [
            c for c in original_certs if isinstance(c, str) and c.strip()
        ]

        # Step 4b: Strip fabricated certifications the LLM placed in the skills
        # section (the back door around the certifications field). This is the
        # ONLY fabrication we block deterministically — a cert is binary (held or
        # not). Defensibility of skills/tools is the model's job, not ours.
        tailored, stripped_certs = self.strip_fabricated_certs(original, tailored)
        report.fabricated_certs_stripped = stripped_certs

        # Step 4c: Strip JD soft-trait / working-style PROSE the model pasted
        # into the skills section ("ownership and follow-through", "comfort with
        # ambiguity"). This is a NARROW, profession-agnostic safety net for the
        # scorer fix: even though required-skill alignment now credits evidence
        # anywhere (removing the incentive to dump), the model still occasionally
        # mirrors the JD's culture-fit phrasing into skills. We strip ONLY
        # unambiguous multi-word trait phrases — never named technologies or
        # grounded competencies. Job-titles / bare-domains / vague catch-alls
        # stay the prompt's responsibility (too context-dependent to strip
        # safely; broad stripping caused net harm on real resumes before).
        tailored, stripped_traits = self.strip_nonskill_traits(tailored)
        report.nonskill_traits_stripped = stripped_traits

        # Step 5: Check experience count
        report.experience_count_mismatch = (
            len(tailored.get("experience", []))
            != len(original.get("experience", []))
        )

        # Step 6: Auto-substitute the safe banned phrases (buzzwords the model
        # keeps emitting). Anything outside the safe-substitution list stays
        # as the prompt's responsibility — no retry trigger for cosmetic
        # phrasing, since the cost (re-running PRO model) outweighs the gain.
        tailored, substitutions = self.fix_banned_phrases(tailored)
        report.banned_phrase_substitutions = substitutions

        # Step 7: Strip Unicode en-dash / em-dash globally. These are AI tells
        # outside of dates too — models reach for them in bullet text and
        # summaries because they look professional, but no human types them
        # on a resume.
        tailored, dash_subs = self.fix_unicode_dashes(tailored)
        report.unicode_dash_substitutions = dash_subs

        # Step 8: Strip fake-precision AI-tell symbols (K+, M+, %+, ~N, sub-N).
        # Legitimate technical shorthand (bare K/M, percent, ms) stays — only
        # the hedging decorations are stripped.
        tailored, ai_subs = self.fix_ai_tell_symbols(tailored)
        report.ai_tell_substitutions = ai_subs

        # Step 9: Enforce terminal periods on every bullet and summary
        # sentence. Belt-and-suspenders against models occasionally omitting
        # them; resume convention is to end each bullet with '.'.
        tailored, period_fixes = self.fix_terminal_periods(tailored)
        report.terminal_period_fixes = period_fixes

        # Determine severity
        report.severity = self._classify_severity(report)

        logger.warning(
            "IntegrityGuard.enforce EXIT: severity=%s, final_tailored_projects=%d %s, hallucinated_proj=%d",
            report.severity,
            len(tailored.get("projects", [])),
            [p.get("name", "?") for p in tailored.get("projects", [])],
            len(report.hallucinated_projects),
        )

        if report.severity != "clean":
            logger.warning(
                "IntegrityGuard report: severity=%s overwrites=%d "
                "hallucinated_exp=%d hallucinated_proj=%d reinjected=%d cap=%s "
                "banned_subs=%d dash_subs=%d ai_tell_subs=%d period_fixes=%d "
                "fab_certs=%d nonskill_traits=%d",
                report.severity,
                len(report.immutable_overwrites),
                len(report.hallucinated_experience),
                len(report.hallucinated_projects),
                len(report.missing_experience_reinjected),
                report.project_cap_enforced,
                len(report.banned_phrase_substitutions),
                len(report.unicode_dash_substitutions),
                len(report.ai_tell_substitutions),
                len(report.terminal_period_fixes),
                len(report.fabricated_certs_stripped),
                len(report.nonskill_traits_stripped),
            )

        return tailored, report

    # ------------------------------------------------------------------
    # 1. Immutable field enforcement
    # ------------------------------------------------------------------

    def force_immutable_fields(
        self,
        original: Dict[str, Any],
        tailored: Dict[str, Any],
    ) -> Tuple[Dict[str, Any], List[str]]:
        """Overwrite immutable fields in tailored with original values.

        Returns (tailored, list_of_overwritten_field_paths).
        """
        overwrites: List[str] = []

        # --- Contact ---
        orig_contact = original.get("contact", {})
        tail_contact = tailored.get("contact", {})
        for f in IMMUTABLE_CONTACT_FIELDS:
            orig_val = orig_contact.get(f, "")
            tail_val = tail_contact.get(f, "")
            if tail_val != orig_val:
                overwrites.append(f"contact.{f}")
                tail_contact[f] = orig_val
        tailored["contact"] = tail_contact

        # --- Experience (matched by company+title tuple) ---
        orig_exp = original.get("experience", [])
        tail_exp = tailored.get("experience", [])
        matched, _, _ = _match_entries(orig_exp, tail_exp, _exp_key)
        for o_entry, t_entry in matched:
            for f in IMMUTABLE_EXPERIENCE_FIELDS:
                orig_val = o_entry.get(f, "")
                tail_val = t_entry.get(f, "")
                if tail_val != orig_val:
                    overwrites.append(
                        f"experience[{o_entry.get('company', '')}].{f}"
                    )
                    t_entry[f] = orig_val

        # --- Education (matched by institution+degree tuple) ---
        orig_edu = original.get("education", [])
        tail_edu = tailored.get("education", [])
        matched_edu, unmatched_edu, missing_edu = _match_entries(
            orig_edu, tail_edu, _edu_key
        )
        for o_entry, t_entry in matched_edu:
            for f in IMMUTABLE_EDUCATION_FIELDS:
                orig_val = o_entry.get(f, "")
                tail_val = t_entry.get(f, "")
                if tail_val != orig_val:
                    overwrites.append(
                        f"education[{o_entry.get('institution', '')}].{f}"
                    )
                    t_entry[f] = orig_val

        # If AI dropped education entries, re-inject from original
        if missing_edu:
            for edu in missing_edu:
                tailored.setdefault("education", []).append(edu)

        # If AI invented education entries, remove them
        if unmatched_edu:
            valid_keys = {_edu_key(e) for e in orig_edu}
            tailored["education"] = [
                e for e in tailored.get("education", [])
                if _edu_key(e) in valid_keys
            ]

        # Clean degree fields: strip baked-in dates and duplicates
        for edu in tailored.get("education", []):
            degree = edu.get("degree", "")
            if degree and "|" in degree:
                segments = [s.strip() for s in degree.split("|")]
                cleaned = [
                    s for s in segments
                    if not re.match(
                        r'^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec|\d{4}|\d{1,2}/\d{4})',
                        s, re.IGNORECASE,
                    )
                ]
                # Deduplicate
                seen = set()
                unique = []
                for s in cleaned:
                    if s.lower() not in seen:
                        seen.add(s.lower())
                        unique.append(s)
                edu["degree"] = " | ".join(unique) if unique else degree

        return tailored, overwrites

    # ------------------------------------------------------------------
    # 2. Hallucination detection
    # ------------------------------------------------------------------

    def detect_hallucinations(
        self,
        original: Dict[str, Any],
        tailored: Dict[str, Any],
    ) -> Tuple[Dict[str, Any], List[Dict], List[Dict], List[Dict]]:
        """Detect and remove hallucinated experience and project entries.

        Returns (tailored, hallucinated_exp, hallucinated_proj, reinjected_exp).
        """
        # --- Experience ---
        orig_exp = original.get("experience", [])
        tail_exp = tailored.get("experience", [])

        matched, hallucinated_exp, missing_exp = _match_entries(
            orig_exp, tail_exp, _exp_key
        )

        # Rebuild experience list: matched entries (tailored version) + re-injected missing
        rebuilt_exp = [t_entry for _, t_entry in matched]
        for missing in missing_exp:
            rebuilt_exp.append(copy.deepcopy(missing))

        tailored["experience"] = rebuilt_exp

        # --- Projects: ALWAYS strip projects whose name doesn't match a name
        # in the ORIGINAL resume. Previously this was gated on
        # `if orig_projects:` — meaning candidates with zero parsed projects
        # got ZERO stripping, and Claude's invented projects (3 per call)
        # survived into ContentAugmenter, which then saw `len(projects)=3`
        # and skipped Phase 1 project generation entirely. The downstream
        # _overflow_trim removed 2 of the 3 invented projects to fit the
        # page, leaving the user with 1 random invented project and zero
        # backend-generated aligned ones.
        # ContentAugmenter adds its generated projects AFTER this guard
        # runs (guard runs inside ResumeTailor.tailor(), augmenter runs
        # after), so augmenter-generated projects are never stripped here.
        hallucinated_proj: List[Dict] = []
        orig_projects = original.get("projects", [])
        orig_proj_keys = {_proj_key(p) for p in orig_projects}  # may be empty set
        tail_projects = tailored.get("projects", [])
        valid_projects = []
        for p in tail_projects:
            if _proj_key(p) in orig_proj_keys:
                valid_projects.append(p)
            else:
                hallucinated_proj.append(p)
        tailored["projects"] = valid_projects

        return tailored, hallucinated_exp, hallucinated_proj, missing_exp

    # ------------------------------------------------------------------
    # 3. Project generation rules
    # ------------------------------------------------------------------

    def enforce_project_rules(
        self,
        original: Dict[str, Any],
        tailored: Dict[str, Any],
    ) -> Tuple[Dict[str, Any], bool]:
        """Enforce project generation limits.

        - If original has projects: re-inject any dropped originals.
        - Universal cap: max 3 total projects (original + generated).
          When trimming, originals are kept first, generated are trimmed.

        Returns (tailored, cap_enforced).
        """
        orig_projects = original.get("projects", [])
        tail_projects = tailored.get("projects", [])
        cap_enforced = False

        if orig_projects:
            # Re-inject any original projects that were dropped
            tail_proj_keys = {_proj_key(p) for p in tail_projects}
            for op in orig_projects:
                if _proj_key(op) not in tail_proj_keys:
                    tail_projects.append(copy.deepcopy(op))

        # Universal cap: max 3 total projects (original + generated)
        if len(tail_projects) > 3:
            if orig_projects:
                # Keep originals first, trim generated
                orig_keys = {_proj_key(p) for p in orig_projects}
                originals = [p for p in tail_projects if _proj_key(p) in orig_keys]
                generated = [p for p in tail_projects if _proj_key(p) not in orig_keys]
                tail_projects = originals + generated[:max(0, 3 - len(originals))]
            else:
                tail_projects = tail_projects[:3]
            cap_enforced = True

        tailored["projects"] = tail_projects
        return tailored, cap_enforced

    # ------------------------------------------------------------------
    # 4b. Anti-fabrication: strip certifications not held
    # ------------------------------------------------------------------

    def strip_fabricated_certs(
        self,
        original: Dict[str, Any],
        tailored: Dict[str, Any],
    ) -> Tuple[Dict[str, Any], List[str]]:
        """Drop certification claims from skills that aren't in the base resume.

        A cert is binary (held or not) — the one fabrication we block
        deterministically. The model placing a cert in the skills section is the
        back door around the certifications field; we close it here. Skill/tool
        defensibility (e.g. C# for a Python dev) is the model's judgment, not
        ours, so ordinary skills are left untouched.
        """
        from services.fabrication_filter import is_fabricated_certification

        skills = tailored.get("skills")
        if not isinstance(skills, dict):
            return tailored, []

        stripped: List[str] = []
        for category, items in list(skills.items()):
            if not isinstance(items, list):
                continue
            kept = []
            for s in items:
                if isinstance(s, str) and is_fabricated_certification(s, original):
                    stripped.append(s)
                else:
                    kept.append(s)
            skills[category] = kept
        tailored["skills"] = skills
        return tailored, stripped

    def strip_nonskill_traits(
        self,
        tailored: Dict[str, Any],
    ) -> Tuple[Dict[str, Any], List[str]]:
        """Drop JD soft-trait / working-style prose pasted into the skills section.

        Narrow safety net — removes ONLY unambiguous multi-word trait phrases
        ('ownership and follow-through', 'comfort with ambiguity', 'collaborative
        working style'). Named technologies, methods, and grounded competencies
        are never matched by is_nonskill_soft_trait, so they survive. Empty
        categories left behind are pruned.
        """
        from services.fabrication_filter import is_nonskill_soft_trait

        skills = tailored.get("skills")
        if not isinstance(skills, dict):
            return tailored, []

        stripped: List[str] = []
        for category, items in list(skills.items()):
            if not isinstance(items, list):
                continue
            kept = []
            for s in items:
                if isinstance(s, str) and is_nonskill_soft_trait(s):
                    stripped.append(s)
                else:
                    kept.append(s)
            if kept:
                skills[category] = kept
            else:
                # Don't leave an empty category behind.
                del skills[category]
        tailored["skills"] = skills
        return tailored, stripped

    # ------------------------------------------------------------------
    # 4. Banned-phrase auto-substitution
    # ------------------------------------------------------------------

    # Safe substitutions only — these rewrite filler without changing meaning.
    # Anything beyond this list is left for the prompt to handle (forcing a
    # PRO-model retry over a cosmetic phrase isn't worth the latency/cost).
    # Case-insensitive match, preserves the candidate's actual content.
    _SAFE_SUBSTITUTIONS = (
        # (regex, replacement)
        (re.compile(r'\bextensive\s+experience\b', re.IGNORECASE), 'experience'),
        (re.compile(r'\bproven\s+track\s+record\b', re.IGNORECASE), 'track record'),
        (re.compile(r'\bstrong\s+foundation\s+in\b', re.IGNORECASE), 'experience with'),
        (re.compile(r'\bstrong\s+background\s+in\b', re.IGNORECASE), 'experience with'),
        (re.compile(r'\bhands-on\s+expertise\b', re.IGNORECASE), 'hands-on experience'),
        (re.compile(r'\brobust\s+experience\b', re.IGNORECASE), 'experience'),
    )

    def fix_banned_phrases(
        self,
        tailored: Dict[str, Any],
    ) -> Tuple[Dict[str, Any], List[str]]:
        """Scan summary + all bullets for safe-substitution banned phrases.

        Mutates the tailored dict in place; returns it plus a list of human-
        readable substitution descriptions (for logging/reporting).
        Phrases outside the SAFE_SUBSTITUTIONS list are NOT rewritten here —
        the prompt's banned-language section is responsible for those.
        """
        substitutions: List[str] = []

        def _rewrite(text: str) -> str:
            if not isinstance(text, str) or not text:
                return text
            new = text
            for pattern, replacement in self._SAFE_SUBSTITUTIONS:
                if pattern.search(new):
                    before = new
                    new = pattern.sub(replacement, new)
                    if new != before:
                        substitutions.append(
                            f"'{pattern.pattern}' → '{replacement}'"
                        )
            return new

        # Summary
        if "summary" in tailored:
            tailored["summary"] = _rewrite(tailored["summary"])

        # Experience bullets
        for exp in tailored.get("experience", []) or []:
            bullets = exp.get("bullets") or []
            exp["bullets"] = [_rewrite(b) for b in bullets]

        # Project bullets
        for proj in tailored.get("projects", []) or []:
            bullets = proj.get("bullets") or []
            proj["bullets"] = [_rewrite(b) for b in bullets]

        return tailored, substitutions

    # ------------------------------------------------------------------
    # 5. Unicode-dash strip (en-dash, em-dash → ASCII hyphen)
    # ------------------------------------------------------------------

    # En-dash U+2013, em-dash U+2014. Real candidates type ASCII '-';
    # these Unicode variants are AI tells in bullet text and summaries.
    # Date separators get re-normalized in date_normalizer.py, but this
    # pass catches any that slip through anywhere in the resume.
    _DASH_CHARS = ("—", "–")  # em-dash, en-dash

    def fix_unicode_dashes(
        self,
        tailored: Dict[str, Any],
    ) -> Tuple[Dict[str, Any], List[str]]:
        """Replace en-dash / em-dash with ASCII hyphen-space in all candidate-
        facing text fields (summary, experience bullets, project bullets,
        experience dates, education dates).

        Strategy:
          - Spaced dash (` – ` / ` — `)        → ` - `
          - Bare dash inside a compound        → `-`
          - Collapse multi-space runs introduced by the substitution.
        """
        substitutions: List[str] = []

        def _strip(text: str) -> str:
            if not isinstance(text, str) or not text:
                return text
            new = text
            for dash in self._DASH_CHARS:
                if dash not in new:
                    continue
                before = new
                # Spaced dash → spaced ASCII hyphen (preserve breathing room)
                new = re.sub(rf"\s+{re.escape(dash)}\s+", " - ", new)
                # Any remaining bare dash → ASCII hyphen
                new = new.replace(dash, "-")
                # Collapse multiple spaces the substitution may have produced
                new = re.sub(r" {2,}", " ", new).strip()
                if new != before:
                    substitutions.append(
                        f"U+{ord(dash):04X} → '-'"
                    )
            return new

        if "summary" in tailored:
            tailored["summary"] = _strip(tailored["summary"])

        for exp in tailored.get("experience", []) or []:
            exp["bullets"] = [_strip(b) for b in (exp.get("bullets") or [])]
            if "dates" in exp:
                exp["dates"] = _strip(exp["dates"])

        for proj in tailored.get("projects", []) or []:
            proj["bullets"] = [_strip(b) for b in (proj.get("bullets") or [])]
            if "dates" in proj:
                proj["dates"] = _strip(proj["dates"])

        for edu in tailored.get("education", []) or []:
            if "dates" in edu:
                edu["dates"] = _strip(edu["dates"])

        return tailored, substitutions

    # ------------------------------------------------------------------
    # 6. AI-tell symbol strip (K+, M+, %+, ~N, sub-N)
    # ------------------------------------------------------------------

    # Each entry is (regex, replacement). Replacements use \1 / \2 group
    # references the same way `re.sub` expects. Order matters: percent
    # cases run before generic '+' cases to avoid double-substitution.
    _AI_TELL_SUBSTITUTIONS = (
        # Strip trailing '+' from K-suffixed counts: '120K+' → '120K'
        (re.compile(r"(\b\d+(?:\.\d+)?)K\+", re.IGNORECASE), r"\1K"),
        # Strip trailing '+' from M-suffixed counts: '2M+' → '2M'
        (re.compile(r"(\b\d+(?:\.\d+)?)M\+", re.IGNORECASE), r"\1M"),
        # Strip trailing '+' from percentages: '99.9%+' → '99.9%'
        (re.compile(r"(\b\d+(?:\.\d+)?%)\+"), r"\1"),
        # Drop the tilde approximation marker: '~5,000' → '5,000'
        (re.compile(r"~(?=\d)"), ""),
        # Rewrite 'sub-Nms' / 'sub-Ns' / 'sub-N second' → 'under Nunit'
        (re.compile(r"\bsub-(\d+)\s*(ms|s|sec|second|seconds)\b", re.IGNORECASE),
         r"under \1\2"),
        # Phrase form 'sub-second' → 'under one second'
        (re.compile(r"\bsub-second\b", re.IGNORECASE), "under one second"),
        # Phrase form 'sub-millisecond' → 'under one millisecond'
        (re.compile(r"\bsub-millisecond\b", re.IGNORECASE), "under one millisecond"),
    )

    def fix_ai_tell_symbols(
        self,
        tailored: Dict[str, Any],
    ) -> Tuple[Dict[str, Any], List[str]]:
        """Strip fake-precision AI-tell decorations.

        Legitimate technical shorthand stays: '120K msgs/sec', '85%', '100ms'.
        Only the hedging suffixes get stripped: 'K+', 'M+', '%+', '~', 'sub-N'.
        '5+ years' / '10+ years' style standalone '+' is left alone — that's
        genuine 'or more' semantics, not an AI tell.
        """
        substitutions: List[str] = []

        def _strip(text: str) -> str:
            if not isinstance(text, str) or not text:
                return text
            new = text
            for pattern, replacement in self._AI_TELL_SUBSTITUTIONS:
                if pattern.search(new):
                    before = new
                    new = pattern.sub(replacement, new)
                    if new != before:
                        substitutions.append(
                            f"'{pattern.pattern}' → '{replacement}'"
                        )
            return new

        if "summary" in tailored:
            tailored["summary"] = _strip(tailored["summary"])

        for exp in tailored.get("experience", []) or []:
            exp["bullets"] = [_strip(b) for b in (exp.get("bullets") or [])]

        for proj in tailored.get("projects", []) or []:
            proj["bullets"] = [_strip(b) for b in (proj.get("bullets") or [])]

        return tailored, substitutions

    # ------------------------------------------------------------------
    # 7. Terminal-period enforcement
    # ------------------------------------------------------------------

    # End-of-string chars that don't need a period appended (already
    # terminated). '.', '!', '?' are sentence terminators; closing quote
    # marks count too since the period precedes them in conventional
    # punctuation but a quoted bullet usually self-terminates.
    _TERMINAL_OK = (".", "!", "?", "\"", "'", ")", "]")

    # Punctuation we REPLACE with '.' (mid-thought connectors that look
    # broken when used as a bullet's terminal char).
    _TERMINAL_REPLACE = (";", ",", ":", "-", "—", "–")

    def fix_terminal_periods(
        self,
        tailored: Dict[str, Any],
    ) -> Tuple[Dict[str, Any], List[str]]:
        """Ensure every bullet and summary sentence ends with a period.

        Resume convention: each bullet is a complete thought ending in '.'.
        Tailor prompts request this but models occasionally forget; this
        deterministic pass guarantees it.
        """
        fixes: List[str] = []

        def _fix(text: str, label: str) -> str:
            if not isinstance(text, str):
                return text
            stripped = text.rstrip()
            if not stripped:
                return text
            last = stripped[-1]
            if last in self._TERMINAL_OK:
                return stripped
            if last in self._TERMINAL_REPLACE:
                fixed = stripped[:-1].rstrip() + "."
                fixes.append(f"{label}: '{last}' -> '.'")
                return fixed
            fixes.append(f"{label}: appended '.'")
            return stripped + "."

        if tailored.get("summary"):
            tailored["summary"] = _fix(tailored["summary"], "summary")

        for ei, exp in enumerate(tailored.get("experience", []) or []):
            bullets = exp.get("bullets") or []
            exp["bullets"] = [
                _fix(b, f"experience[{ei}].bullets[{bi}]")
                for bi, b in enumerate(bullets)
            ]

        for pi, proj in enumerate(tailored.get("projects", []) or []):
            bullets = proj.get("bullets") or []
            proj["bullets"] = [
                _fix(b, f"projects[{pi}].bullets[{bi}]")
                for bi, b in enumerate(bullets)
            ]

        return tailored, fixes

    # ------------------------------------------------------------------
    # Severity classification
    # ------------------------------------------------------------------

    @staticmethod
    def _classify_severity(report: IntegrityReport) -> str:
        """Classify the overall severity of violations found."""
        if report.hallucinated_experience:
            return "needs_retry"

        # Persistent count mismatch after re-injection means data was lost
        if report.experience_count_mismatch:
            return "needs_retry"

        has_any_fix = (
            report.immutable_overwrites
            or report.hallucinated_projects
            or report.missing_experience_reinjected
            or report.project_cap_enforced
            or report.banned_phrase_substitutions
            or report.unicode_dash_substitutions
            or report.ai_tell_substitutions
            or report.terminal_period_fixes
            or report.fabricated_certs_stripped
            or report.nonskill_traits_stripped
        )

        if has_any_fix:
            return "auto_fixed"

        return "clean"
