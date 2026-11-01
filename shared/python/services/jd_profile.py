"""The one JD dictionary, and the one component that builds it.

Before this module a job description was read twice, by two things that never
met: `fit_score.extract_requirements` (regex, at crawl, feeds filtering and the
match score) and `ResumeService.extract_jd` (LLM, at tailoring). They had
different fields, and neither could say whether a posting offers sponsorship.

Both now go through here:

* `read_fields` is called from `fit_score._extract_requirements_cached`, so
  ingest, the feed, the apply sweep and live scoring all carry the same answers
  under `requirements["jd"]`, and `required_degree_level` and
  `work_authorization_restricted` are derived from them rather than beside them.
* `attach_to_analysis` is called from `ResumeService.extract_jd`, the single
  entry for every tailoring flow, so `jd_analysis["jd_profile"]` is the same
  dictionary with the LLM's extras merged in.
* `build` is the public entry when you hold a JD and want the dictionary.

Everything here is pure: no network, no database, no LLM. An absent fact is
`not_stated`, never a guess. Silence must not cost a candidate anything, and a
wrong "no sponsorship" hides a real job from the people who most need it.

Do not import `fit_score` at module level: it imports this module.
"""
from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from services.jd_skill_vocabulary import JD_SKILL_VOCABULARY

# Bump when a reader changes what it returns. Stored blocks carry it as `v`.
JD_PROFILE_VERSION = "jd-1.0.0"

SPONSORSHIP_OFFERED = "offered"
SPONSORSHIP_NOT_OFFERED = "not_offered"
SPONSORSHIP_CONDITIONAL = "conditional"
NOT_STATED = "not_stated"

AUTH_CITIZEN = "us_citizen"
AUTH_CITIZEN_OR_PR = "us_citizen_or_permanent_resident"
AUTH_AUTHORIZED = "authorized_to_work"

DEGREE_NAMES = {0: NOT_STATED, 1: "high_school_or_associate", 2: "bachelor",
                3: "master", 4: "doctorate"}

_EVIDENCE_CHARS = 200
_LINE_SPLIT = re.compile(r"\n+")
# Never end a sentence on an abbreviation. Splitting "(e.g. H-1B) visa" orphaned
# "...or support future h-1b sponsorship" from its "we do not", and splitting
# "J.D. Power" left a bare "j.d." that read as a law degree.
_SENTENCE_SPLIT = re.compile(
    r"(?<!e\.g\.)(?<!i\.e\.)(?<!u\.s\.)(?<!etc\.)(?<!inc\.)(?<!vs\.)(?<!j\.d\.)(?<!m\.d\.)"
    r"(?<!ph\.d\.)(?<!b\.s\.)(?<!m\.s\.)(?<!b\.a\.)(?<!m\.a\.)(?<=[.;!?])\s+(?=[a-z(])|"
    r"\s*[•·]\s*")


def _evidence(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()[:_EVIDENCE_CHARS]


def _sentences(blob: str) -> List[str]:
    out: List[str] = []
    for line in _LINE_SPLIT.split(blob or ""):
        out.extend(s.strip() for s in _SENTENCE_SPLIT.split(line) if s.strip())
    return out


# ---------------------------------------------------------------------------
# Sponsorship
# ---------------------------------------------------------------------------

# `[^\n.;!?]` keeps a match inside one clause, so a negation cannot reach into
# the next sentence. The patterns were written against real JD sentences.
_C = r"[^\n.;!?]"

# "Sponsor" in a sense that has nothing to do with immigration.
_SPONSOR_NOISE = re.compile(
    r"(?:executive|project|business|stakeholder|leadership|event|conference|"
    r"program|product|study|trial|clinical|plan|title|bank|bin|corporate|"
    r"hackathon|meetup)\s+sponsors?(?:hip)?(?![a-z])|"
    r"sponsors? (?:bank|of the|for the (?:event|project))|sponsored by|"
    r"sponsoring (?:organi[sz]ation|bank|agency|institution)")

_SPONSOR_CONDITIONAL = tuple(re.compile(p) for p in (
    rf"sponsor(?:ship)?{_C}{{0,90}}(?:exclusively|only|solely)\s+(?:be\s+)?"
    r"(?:for|to|on|open)(?![a-z])",
    rf"not all{_C}{{0,50}}sponsor|whether to sponsor",
    rf"sponsorship{_C}{{0,40}}(?:may|might|could)\s+(?:not\s+)?be\s+"
    r"(?:available|considered|offered|provided)",
    rf"(?:may|might)\s+not\s+(?:be\s+)?(?:eligible|qualify|able){_C}{{0,50}}sponsor",
    rf"(?:may|might|could)\s+(?:be able to\s+)?(?:offer|provide|consider|sponsor)"
    rf"{_C}{{0,40}}sponsor",
    r"case[- ]by[- ]case[^\n]{0,60}sponsor|sponsor[^\n]{0,60}case[- ]by[- ]case",
))

_SPONSOR_NEGATIVE = tuple(re.compile(p) for p in (
    # "sponsorship ... is not available", in either tense.
    r"sponsor(?:ship)?[^\n]{0,90}?(?<![a-z])(?:not|n't)\s+"
    r"(?:(?:currently|presently|yet|be)\s+)*"
    r"(?:available|offered|provided|possible|supported|considered|granted)",
    rf"(?:not|in)\s?eligible for{_C}{{0,50}}sponsor",
    rf"(?<![a-z])no(?:\s|-){_C}{{0,50}}sponsorship",
    rf"(?<![a-z])without{_C}{{0,70}}sponsor",
    rf"(?:must|do|does|will|should|can)\s*(?:not|n't){_C}{{0,50}}(?:require|need)"
    rf"{_C}{{0,70}}sponsor",
    r"(?<![a-z])(?:cannot|can ?not|can't|won't|will not|do not|don't|does not|"
    r"doesn't|did not|unable to|not able to|not willing to|unwilling to|"
    r"not in a position to|not currently|are not|is not|aren't|isn't)(?![a-z])"
    rf"{_C}{{0,90}}sponsor",
    rf"(?<![a-z])not require{_C}{{0,50}}sponsor",
    rf"prohibited from{_C}{{0,40}}sponsor",
    r"not\s+(?:a\s+)?visa[- ]sponsored",
    rf"(?:require|need)(?:s|ing)?{_C}{{0,50}}sponsor{_C}{{0,60}}"
    r"(?:not be considered|not eligible|ineligible|need not apply)",
    rf"sponsor(?:ship)?{_C}{{0,40}}(?<![a-z])unavailable",
))

_SPONSOR_POSITIVE = tuple(re.compile(p) for p in (
    rf"(?:visa|h-?1b|immigration|work authorization|employment|work)[- ]"
    rf"(?:[a-z]+ )?sponsorship{_C}{{0,30}}(?:available|offered|provided|supported)",
    rf"(?<![a-z])(?:we|company|employer|will|can|do|does|able to|happy to|"
    rf"willing to|open to)(?![a-z]){_C}{{0,30}}sponsor(?:s|ing)?(?![a-z])"
    rf"{_C}{{0,30}}(?:visa|h-?1b|work permit|work authorization|immigration|candidates)",
    rf"(?:offer|provide|support|include)(?:s|ing)?{_C}{{0,30}}"
    r"(?:visa|h-?1b|immigration)\s+sponsorship",
    rf"eligible for{_C}{{0,30}}(?:visa|immigration|employment){_C}{{0,20}}sponsorship",
    r"sponsorship\s+(?:is\s+)?available",
    r"(?<![a-z])will sponsor(?![a-z])",
))

_ABBREVIATION_DOTS = re.compile(r"(?<![a-z])(?:u\.s\.a?\.?|e\.g\.|i\.e\.|etc\.)")

# A positive reading must never come from a sentence that also negates. If it
# was not caught as negative, "not stated" is the safe answer.
_NEGATION = re.compile(r"(?<![a-z])(?:not|no|never|cannot|without|unable|unfortunately)(?![a-z])|n't")

# Forms printed into the body: "Visa Sponsorship? No". The answer usually sits
# on the next line, and an unanswered label ("Visa sponsorship?" followed by
# "Travel requirements:") must read as not stated.
_SPONSOR_FORM = re.compile(
    r"([^\n]{0,40}sponsor(?:ship)?[^\n]{0,30}[:?]+)[ \t]*\n?[ \t]*"
    r"(yes|no|none|n/a|not available|available)(?![a-z/])")
_FORM_ASKS_CANDIDATE = re.compile(
    r"(?<![a-z])(?:do you|will you|are you|would you|have you)(?![a-z])")


# Refusal phrased without the word "sponsor".
_VISA_SUPPORT_REFUSED = re.compile(
    r"(?:cannot|can't|unable to|not able to|will not|won't|do not|does not|not)(?![a-z])[^\n.;!?]{0,50}"
    r"(?:work authori[sz]ation|visa|immigration)\s+(?:assistance|support)")


def read_sponsorship(blob: str) -> Dict[str, str]:
    """`{"status", "evidence"}`. A negative statement outranks a positive one.

    "Must be authorized to work in the US" is deliberately NOT read as
    `not_offered`: it is said by employers who do sponsor, so it is reported
    by `read_authorization` instead.
    """
    if not blob:
        return {"status": NOT_STATED, "evidence": ""}
    if "sponsor" not in blob:
        for sentence in _sentences(blob):
            if _VISA_SUPPORT_REFUSED.search(sentence):
                return {"status": SPONSORSHIP_NOT_OFFERED, "evidence": _evidence(sentence)}
        return {"status": NOT_STATED, "evidence": ""}
    found: Dict[str, str] = {}

    def note(status: str, text: str) -> None:
        found.setdefault(status, _evidence(text))

    for m in _SPONSOR_FORM.finditer(blob):
        label = m.group(1)
        if _SPONSOR_NOISE.search(label) or _FORM_ASKS_CANDIDATE.search(label):
            continue
        answer = m.group(2)
        positive = answer in ("yes", "available")
        note(SPONSORSHIP_OFFERED if positive else SPONSORSHIP_NOT_OFFERED, m.group(0))

    for sentence in _sentences(blob):
        if "sponsor" not in sentence or _SPONSOR_NOISE.search(sentence):
            continue
        # The clause-bounded patterns stop at a full stop, so the dots inside
        # "u.s." and "etc." must not read as the end of a clause.
        text = _ABBREVIATION_DOTS.sub(lambda m: m.group(0).replace(".", ""), sentence)
        if any(rx.search(text) for rx in _SPONSOR_CONDITIONAL):
            note(SPONSORSHIP_CONDITIONAL, sentence)
        elif any(rx.search(text) for rx in _SPONSOR_NEGATIVE):
            note(SPONSORSHIP_NOT_OFFERED, sentence)
        elif not _NEGATION.search(text) \
                and any(rx.search(text) for rx in _SPONSOR_POSITIVE):
            note(SPONSORSHIP_OFFERED, sentence)

    for status in (SPONSORSHIP_NOT_OFFERED, SPONSORSHIP_CONDITIONAL, SPONSORSHIP_OFFERED):
        if status in found:
            return {"status": status, "evidence": found[status]}
    return {"status": NOT_STATED, "evidence": ""}


# ---------------------------------------------------------------------------
# Work authorization
# ---------------------------------------------------------------------------

_AUTH_CITIZEN_OR_PR = re.compile(
    r"(?:u\.?s\.?|united states)\s+citizen(?:ship)?(?:s)?\s*(?:,|or|/)\s*"
    r"(?:a\s+|an\s+)?(?:lawful\s+|legal\s+|u\.?s\.?\s+)?"
    r"(?:permanent resident|green[ -]card)|"
    r"green[ -]card holders?|permanent residents? only|"
    r"must be a (?:lawful )?permanent resident|"
    r"(?:must be|requires?|be) an?\s+u\.?s\.? person(?![a-z])")
_AUTH_CITIZEN = re.compile(
    r"must (?:be|hold) (?:a )?(?:u\.?s\.?|united states) citizen|"
    r"(?:u\.?s\.?|united states) citizenship(?: status)? (?:is |are )?required|"
    r"requires? (?:u\.?s\.?|united states) citizenship|"
    r"(?<![a-z])citizens? only(?![a-z])")
_AUTH_ANY = re.compile(
    r"(?:authori[sz]ed|eligible|entitled|legally able|permitted) to work|"
    r"(?:legal |legally )?right to work|work authori[sz]ation|work authori[sz]ed|"
    r"work lawfully|lawfully (?:authori[sz]ed|work)|valid work (?:permit|visa)|"
    r"eligibility to work")
_AUTH_WORK_REQUESTS = re.compile(r"authori[sz]ation requests?")
# Compliance boilerplate that names work authorization without requiring it.
_AUTH_BOILERPLATE = re.compile(
    r"e-?verify|i-9|employment eligibility verification|"
    r"non-?discrimination|equal (?:employment )?opportunit|poster|"
    r":\s*$|^\W*(?:work authori[sz]ation|right to work)\W*$")
# Citizenship named for OTHER roles, or as the group a rule does not apply to.
_AUTH_NOT_THIS_ROLE = re.compile(
    r"all positions with|certain (?:other )?(?:restricted )?positions|(?<![a-z])except(?![a-z])|"
    r"may be required|may require|(?<![a-z])if (?:required|applicable)(?![a-z])")


_AUTH_CLEARED_ROLES = re.compile(r"all positions with a (?:government |security )?clearance")


def read_authorization(blob: str, clearance_required: bool = False) -> Dict[str, str]:
    """`{"requirement", "evidence"}`, strictest statement first."""
    if not blob:
        return {"requirement": NOT_STATED, "evidence": ""}
    fallback = ""
    for sentence in _sentences(blob):
        # "Citizenship is required for all positions with a clearance" is boiler
        # plate on most postings and a rule on the ones that carry a clearance.
        if clearance_required and _AUTH_CLEARED_ROLES.search(sentence):
            return {"requirement": AUTH_CITIZEN, "evidence": _evidence(sentence)}
        if _AUTH_NOT_THIS_ROLE.search(sentence):
            continue
        if _AUTH_CITIZEN_OR_PR.search(sentence):
            return {"requirement": AUTH_CITIZEN_OR_PR, "evidence": _evidence(sentence)}
        if _AUTH_CITIZEN.search(sentence):
            return {"requirement": AUTH_CITIZEN, "evidence": _evidence(sentence)}
        if not fallback and _AUTH_ANY.search(sentence) \
                and not _AUTH_BOILERPLATE.search(sentence) \
                and not _AUTH_WORK_REQUESTS.search(sentence):
            fallback = sentence
    if fallback:
        return {"requirement": AUTH_AUTHORIZED, "evidence": _evidence(fallback)}
    return {"requirement": NOT_STATED, "evidence": ""}


# ---------------------------------------------------------------------------
# Education
# ---------------------------------------------------------------------------

# Two-letter degrees need a neighbour that makes them a degree. "ms" is also
# MS Teams, MS Office and Mississippi; "ba" is a business analyst; "md" and
# "ma" are states, and "jd" is how recruiters abbreviate "job description".
_ABBR_NEXT = (r"(?=\s*(?:/|,|\(|or(?![a-z])|and(?![a-z])|in(?![a-z])|with(?![a-z])|"
              r"degree|required|preferred))")
_DEGREE_READERS: Sequence[Tuple[int, re.Pattern]] = (
    (4, re.compile(
        r"(?<![a-z])ph\.?\s?d(?![a-z])|doctorate|doctoral degree|doctor of|"
        r"(?<![a-z.])m\.d\.|(?<![a-z.])j\.d\.(?!\s*(?:power|irving|edwards|vance|williams))|juris doctor|"
        r"(?<![a-z,] )(?<![a-z])(?:md|jd)\s+(?:degree|required)")),
    (3, re.compile(
        r"(?<![a-z-])master's(?![a-z])|(?<![a-z-])masters?\s+degree(?![a-z])|"
        r"(?<![a-z-])(?<!scrum )masters(?=\s+(?:in|of|or|level|program|preferred|required)(?![a-z])|"
        r"\s*[,/.;)]|\s*$)|"
        r"(?<![a-z-])master of (?:science|arts|business|engineering|education|public|fine|"
        r"laws|computer|technology|health|social|information)(?![a-z])|"
        r"(?:advanced|graduate|post-?graduate) degree|(?<![a-z])m\.?b\.?a(?![a-z])|(?<![a-z])m\.eng(?![a-z])|(?<![a-z])msc(?![a-z])|"
        r"(?<![a-z.])m\.s\.?(?![a-z])|(?<![a-z.])m\.a\.(?![a-z])(?!\s*certif)|"
        rf"(?<![a-z.])(?<!, )ms{_ABBR_NEXT}")),
    (2, re.compile(
        r"(?<![a-z])bachelor'?s?(?![a-z])|(?<![a-z])b\.?tech(?![a-z])|(?<![a-z])bsc(?![a-z])|"
        r"(?<![a-z])bsn(?![a-z])|(?<![a-z])b\.e\.(?![a-z])|undergraduate degree|"
        r"(?<![a-z])baccalaureate(?![a-z])|(?<![a-z])tertiary (?:qualification|degree|education)|"
        r"(?:four|4)[- ]year degree|(?:college|university) degree|"
        r"(?:a|relevant|related|technical|engineering|architectural|science|business|"
        r"accounting|nursing|finance|law)\s+degree(?![a-z])(?!\s+of(?![a-z]))|"
        # A bare "degree" only opening a line: anywhere else it may be the tail of
        # "master's degree in physics", which must not read as a bachelor's too.
        r"^\W*degree (?:in|qualified|or equivalent|required)(?![a-z])|"
        r"(?<![a-z.])b\.s\.?(?![a-z])|(?<![a-z.])b\.a\.(?![a-z])|"
        rf"(?<![a-z.])(?<!, )(?:bs|ba){_ABBR_NEXT}")),
    (1, re.compile(
        r"(?<![a-z])associate'?s?\s+(?:degree|of (?:arts|science|applied))(?![a-z])|"
        r"(?<![a-z])a\.a\.s\.?(?![a-z])|"
        r"(?<![a-z])diploma(?![a-z])|(?<![a-z])high school(?![a-z])|"
        r"(?<![a-z])ged(?![a-z])|"
        r"(?<![a-z])vocational (?:school|training|degree|diploma|certificate)(?![a-z])")),
)

# A degree named in passing. Tuition benefits are the common case.
_EDU_NOISE = re.compile(
    r"tuition|reimburs|scholarship|alumni|student loan|high school students|"
    r"continuing education|education assistance|education history|"
    r"(?:phd|doctoral|graduate) programs?|these include|working towards|"
    r"(?<![a-z])premium(?![a-z])|differential|years toward|"
    # Enrolment is not a held degree, and these lines waive one.
    r"(?<![a-z])pursuing(?![a-z])|currently enrolled|actively enrolled|must be enrolled|"
    # Required later or for someone else, not of this hire.
    r"(?:at|within) (?:\d+|one|two|three|four|five) years? of (?:employment|hire)|"
    r"(?<![a-z])if (?:in|candidate|you)(?![a-z])|"
    r"(?<![a-z])no (?:bachelor|college|university|degree)|"
    # A substitute path, not a lower minimum.
    r"will meet|substitut|"
    r"(?:bachelor|master)'?s? students|without a (?:college )?degree|"
    r"less than (?:a )?high school|"
    # The path offered INSTEAD of the degree, not a lower minimum.
    r"in lieu of|in place of|"
    r"we (?:also )?(?:offer|provide)")
# "degree, preferably in biology" prefers the FIELD, not the degree.
_FIELD_PREFERENCE = re.compile(r"prefer(?:ably|red)\s+(?:in|within|from)(?![a-z])[^.;,\n]*")
# Preference wording `fit_score._PREFERRED_CUE` does not carry.
_PARENTHETICAL = re.compile(r"\([^)]*\)")
_CLOSING_NAMES_OTHER = re.compile(r"experience|skills?|knowledge|certif|licen[sc]")
_EDU_PREFERRED_EXTRA = re.compile(r"(?<![a-z])(?:an asset|beneficial|considered a|valued)(?![a-z])")
# "High school diploma or equivalent" is a GED, not experience for a degree.
_EDU_EQUIVALENT = re.compile(
    r"equivalent[^.;\n]{0,30}(?:experience|combination)|"
    r"(?:experience|combination)[^.;\n]{0,30}equivalent|"
    r"or (?:related|relevant) experience")
_EDU_CLAUSE = re.compile(r"[,;]\s+|\s+but\s+|\s+while\s+|\s+and\s+(?=[a-z' ]{0,30}prefer)")
_EDU_FIELDS: Sequence[Tuple[str, re.Pattern]] = tuple(
    (name, re.compile(rf"(?<![a-z]){pattern}(?![a-z])")) for name, pattern in (
        ("computer science", r"computer science|cs"),
        ("computer engineering", r"computer engineering"),
        ("software engineering", r"software engineering"),
        ("electrical engineering", r"electrical(?: and electronics?)? engineering"),
        ("mechanical engineering", r"mechanical engineering"),
        ("engineering", r"engineering"),
        ("information systems", r"information (?:systems|technology)|mis"),
        ("data science", r"data science"),
        ("mathematics", r"math(?:ematics)?"),
        ("statistics", r"statistics"),
        ("physics", r"physics"),
        ("business", r"business(?: administration)?"),
        ("finance", r"finance"),
        ("accounting", r"accounting"),
        ("economics", r"economics"),
        ("nursing", r"nursing"),
    ))


def _degree_levels(text: str) -> List[int]:
    return [level for level, rx in _DEGREE_READERS if rx.search(text)]


def read_education(blob: str) -> Dict[str, Any]:
    """Minimum and preferred degree, read clause by clause.

    The minimum is the LOWEST level the posting accepts as a requirement:
    "BS with 5 years or MS with 3" asks for a bachelor's, and "Bachelor's
    required, Master's preferred" asks for a bachelor's. A degree named only as
    preferred is not a requirement at all.
    """
    from services import fit_score

    required: List[Tuple[int, str]] = []
    preferred: List[Tuple[int, str]] = []
    fields: List[str] = []
    saw_equivalent = False
    mode = "required"
    for line in _LINE_SPLIT.split(blob or ""):
        stripped = line.strip()
        if not stripped:
            continue
        if fit_score.is_section_heading(stripped):
            if "education" in stripped:
                mode = "required"
            elif fit_score._PREFERRED_HEAD.search(stripped):
                mode = "preferred"
            elif fit_score._REQUIRED_HEAD.search(stripped):
                mode = "required"
        for sentence in _sentences(stripped):
            if (_EDU_EQUIVALENT.search(sentence) or "in lieu of" in sentence) \
                    and (_degree_levels(sentence + ",") or "education" in sentence):
                saw_equivalent = True
            if not _degree_levels(sentence) or _EDU_NOISE.search(sentence):
                continue
            sentence = _FIELD_PREFERENCE.sub("", sentence)
            sentence_pref = bool(fit_score._PREFERRED_CUE.search(_PARENTHETICAL.sub("", sentence)))
            closing = _EDU_CLAUSE.split(sentence)[-1]
            if fit_score._PREFERRED_CUE.search(_PARENTHETICAL.sub("", closing)) \
                    and not _degree_levels(closing + ",") \
                    and not _CLOSING_NAMES_OTHER.search(closing):
                # "Bachelor's degree in accounting, business, or finance is
                # preferred": the closing cue covers the list it ends.
                preferred.extend((level, sentence) for level in _degree_levels(sentence + ","))
                continue
            sentence_req = bool(fit_score._REQUIRED_CUE.search(sentence))
            for name, rx in _EDU_FIELDS:
                if rx.search(sentence) and name not in fields:
                    fields.append(name)
            # Brackets hold their own commas: "(master's 6+, associate's 10+)".
            outer = _PARENTHETICAL.sub(" ", sentence)
            clauses = _EDU_CLAUSE.split(outer if _degree_levels(outer + ",") else sentence)
            cued = [c for c in clauses if fit_score._PREFERRED_CUE.search(_PARENTHETICAL.sub("", c))]
            cued_levels = {lvl for c in cued for lvl in _degree_levels(c + ",")}
            for clause in clauses:
                # A note in brackets qualifies the note: "degree (CPA highly
                # preferred)" still requires the degree, and "(or high school
                # diploma plus 6 years)" is a path around it.
                plain = _PARENTHETICAL.sub("", clause)
                # The split ate the comma that makes "bs," a degree.
                levels = _degree_levels(plain + ",") or (
                    _degree_levels(clause + ",") if "(or" not in clause else [])
                if not levels:
                    continue
                clause_pref = bool(fit_score._PREFERRED_CUE.search(plain)
                                   or _EDU_PREFERRED_EXTRA.search(plain))
                clause_req = bool(fit_score._REQUIRED_CUE.search(plain))
                if clause_pref or clause_req:
                    is_pref = clause_pref and not clause_req
                elif cued and len(clauses) > 1:
                    # "Bachelor's, Master's preferred": the cue belongs to its
                    # own clause, and this one stands on the section. Items of
                    # the same level are one list: "diploma, GED preferred".
                    is_pref = mode == "preferred" or bool(cued_levels & set(levels))
                else:
                    is_pref = sentence_pref and not sentence_req
                    if not sentence_pref and not sentence_req:
                        is_pref = mode == "preferred"
                bucket = preferred if is_pref else required
                bucket.extend((level, sentence) for level in levels)

    min_level, evidence = 0, ""
    if required:
        min_level, evidence = min(required, key=lambda item: item[0])
    preferred_level = max((level for level, _ in preferred), default=0)
    if preferred_level <= min_level:
        preferred_level = 0
    if not evidence and preferred:
        evidence = max(preferred, key=lambda item: item[0])[1]
    return {
        "min_level": min_level,
        "min_degree": DEGREE_NAMES[min_level],
        "preferred_level": preferred_level,
        "preferred_degree": DEGREE_NAMES[preferred_level],
        "fields": fields[:6],
        "equivalent_experience_accepted": saw_equivalent and bool(required or preferred),
        "evidence": _evidence(evidence),
    }


# ---------------------------------------------------------------------------
# Experience, clearance, certifications, employment type, alternatives
# ---------------------------------------------------------------------------

_YEARS_RANGE = re.compile(
    r"(?<![\d.$])(\d{1,2})\s*\+?\s*(?:-|to)\s*(\d{1,2})\s*\+?\s*(?:years?|yrs?)(?![a-z])")


_NUMBER_NAMES = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
                 "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
                 "eighteen": 18}
_MONTHS_REQUIRED = re.compile(
    r"(?<![\d.$])(\d{1,2}|" + "|".join(_NUMBER_NAMES) + r")(?:\s*\(\d{1,2}\))?\s*\+?\s*months?"
    r"(?![a-z])[^.\n]{0,60}(?:experience|required)")


def _months_required(blob: str) -> Optional[float]:
    """A requirement stated in months, in years: "six (6) months prior experience"."""
    from services import fit_score

    for sentence in _sentences(blob or ""):
        m = _MONTHS_REQUIRED.search(sentence)
        if m and not fit_score._PREFERRED_CUE.search(sentence):
            count = _NUMBER_NAMES.get(m.group(1)) or int(m.group(1))
            return round(count / 12, 2)
    return None


def read_experience(blob: str, min_years: Optional[int],
                    preferred_years: Optional[int] = None) -> Dict[str, Any]:
    """`min_years` is what the posting REQUIRES; `preferred_years` is set only when
    a preference is all it states. `fit_score` keeps scoring on either one."""
    if min_years is None:
        months = _months_required(blob)
        return {"min_years": months, "max_years": None,
                "preferred_years": preferred_years, "evidence": ""}
    max_years = None
    evidence = ""
    for sentence in _sentences(blob or ""):
        for m in _YEARS_RANGE.finditer(sentence):
            if int(m.group(1)) == min_years and int(m.group(2)) > min_years:
                max_years = int(m.group(2))
                evidence = sentence
                break
        if max_years is not None:
            break
    if not evidence:
        needle = re.compile(rf"(?<![\d.$]){min_years}\s*(?:\+|plus)?\s*(?:or more\s+)?(?:years?|yrs?)")
        evidence = next((s for s in _sentences(blob or "") if needle.search(s)), "")
    return {"min_years": min_years, "max_years": max_years, "preferred_years": None,
            "evidence": _evidence(evidence)}


_CLEARANCE_LEVELS: Sequence[Tuple[str, re.Pattern]] = (
    ("ts_sci", re.compile(r"ts/sci|ts\s*/\s*sci|top secret\s*/\s*sci|clearance (?:type|level):\s*ts|"
                          r"top[ -]secret[^.\n]{0,40}(?<![a-z])sci(?![a-z])")),
    ("top_secret", re.compile(r"top[ -]secret")),
    ("public_trust", re.compile(r"clearance (?:type|level):\s*public trust")),
    ("secret", re.compile(r"(?<![a-z])secret (?:u\.?s\.? )?(?:clearance|level|security|or higher)|"
                          r"clearance (?:type|level):\s*secret|"
                          r"(?<![a-z])(?:active|interim|dod) secret(?![a-z])")),
    ("public_trust", re.compile(r"public trust")),
    ("export_controlled", re.compile(r"\bitar\b|export[ -]control|(?<![a-z])u\.?s\.? persons?(?![a-z])")),
)


def clearance_level(blob: str, required: bool) -> str:
    if not required:
        return NOT_STATED
    for name, rx in _CLEARANCE_LEVELS:
        if rx.search(blob or ""):
            return name
    return "unspecified"


_CERTIFICATIONS: Sequence[Tuple[str, re.Pattern]] = tuple(
    (name, re.compile(rf"(?<![a-z]){pattern}(?![a-z])")) for name, pattern in (
        ("AWS Certification", r"aws certifi(?:ed|cation)"),
        ("Azure Certification", r"azure (?:\w+ )?certifi(?:ed|cation)"),
        ("Google Cloud Certification", r"(?:google cloud|gcp) (?:\w+ )?certifi(?:ed|cation)"),
        ("CKA", r"cka|certified kubernetes administrator"),
        ("CKAD", r"ckad"),
        ("CISSP", r"cissp"),
        ("CISM", r"cism"),
        ("CISA", r"cisa"),
        ("CEH", r"ceh"),
        ("CompTIA Security+", r"security\+"),
        ("CompTIA Network+", r"network\+"),
        ("CompTIA A+", r"comptia a\+"),
        ("CCNA", r"ccna"),
        ("CCNP", r"ccnp"),
        ("CASP+", r"casp\+?"),
        ("SSCP", r"sscp"),
        ("GCIH", r"gcih"),
        ("CompTIA Cloud+", r"cloud\+"),
        ("BLS", r"bls"),
        ("ACLS", r"acls"),
        ("PALS", r"pals"),
        ("CPR", r"cpr"),
        ("PMP", r"pmp"),
        ("CAPM", r"capm"),
        ("PRINCE2", r"prince2"),
        ("Certified ScrumMaster", r"certified scrum ?master|csm certification"),
        ("TOGAF", r"togaf"),
        ("CCSP", r"ccsp"),
        ("CySA+", r"cysa\+?"),
        ("GICSP", r"gicsp"),
        ("CHFI", r"chfi"),
        ("GCED", r"gced"),
        ("CompTIA Project+", r"project\+"),
        ("Databricks Certification", r"databricks certified|databricks certification"),
        ("Salesforce Certification", r"salesforce certified|salesforce certification"),
        ("Snowflake Certification", r"snowpro|snowflake certified|snowflake certification"),
        ("CPA", r"cpa"),
        ("CFP", r"cfp"),
        ("CMA", r"cma"),
        ("SPHR", r"sphr"),
        ("PHR", r"phr"),
        ("SHRM-CP", r"shrm-(?:cp|scp)"),
        ("CPIM", r"cpim"),
        ("CSCP", r"cscp"),
        ("CPSM", r"cpsm"),
        ("CQE", r"cqe"),
        ("CQA", r"cqa"),
        ("CCMA", r"ccma"),
        ("RMA", r"rma certification|registered medical assistant"),
        ("NRP", r"nrp"),
        ("CCRN", r"ccrn"),
        ("CNOR", r"cnor"),
        ("PTCB", r"ptcb"),
        ("CNA", r"certified nursing assistant|cna certification"),
        ("EPA 608", r"epa 608|epa section 608"),
        ("Six Sigma", r"six sigma"),
        ("ITIL", r"itil"),
        ("CFA", r"cfa"),
        ("PE License", r"professional engineer(?:ing)? licen[sc]e"),
    ))


def read_certifications(blob: str) -> List[str]:
    return [name for name, rx in _CERTIFICATIONS if rx.search(blob or "")][:12]


# Both readers below accept STATEMENTS about this role, never mentions. "Assist
# customers in person and on the phone" is not an on-site role, "hybrid cloud" is
# not a hybrid role, and "full-time employees are eligible for benefits" says
# nothing about this job. An unclear posting is `not_stated`, which costs a
# candidate nothing; a wrong reading filters them out.
_POLICY_PARAGRAPH = re.compile(
    r"if this (?:position|role|job) is|for (?:on-?site|hybrid|remote)(?: and \w+)? roles|"
    r"employees who are working|listed as|depending on|may be|interview|onboarding|"
    r"this notice|hired to work|transitioning|rollout|opportunity to come together")

_MODE_WORD = r"(remote|hybrid|on[- ]?site|in[- ]office|in[- ]person)"
_MODE_LABEL = (r"(?:work(?:place|ing)?(?: type| model| environment| arrangement| style| location| flexibility| mode)?|"
               r"location(?: type)?|workplace|remote status|job type|position type)")
_MODE_STATEMENTS: Sequence[re.Pattern] = tuple(re.compile(p) for p in (
    rf"^\W*{_MODE_LABEL}\s*[:\-]\s*(?:[a-z-]+,\s*){{0,3}}{_MODE_WORD}(?![a-z])",
    rf"^\W*{_MODE_WORD}\W*$",
    # A line opened by the mode: "Remote (Continental US)", "On-site - New York".
    rf"^\W*{_MODE_WORD}\s*[-–—(]",
    rf"(?:this|the) (?:is an?|role is|position is|job is|opportunity is|role will be|"
    rf"position will be)\s+(?:a\s+|an\s+)?(?:fully\s+|100%\s+)?{_MODE_WORD}(?![a-z])",
    rf"(?:fully|100%|entirely|completely)\s+{_MODE_WORD}(?![a-z])",
    rf"(?<![a-z-]){_MODE_WORD}\s+(?:work(?:ing)?\s+)?(?:role|position|opportunity|job|employee|"
    r"presence|schedule|requirement|arrangement)(?![a-z])",
    rf"(?:work|working|be|report|reporting|based|located)\s+(?:primarily\s+|fully\s+)?{_MODE_WORD}(?![a-z])",
))
# Fewer than five days a week somewhere is a hybrid schedule.
_PART_WEEK = (r"(?<![\d-])(?:[1-4]|one|two|three|four)(?:\s*\(\d\))?(?:\s*(?:-|to)\s*[1-4])?"
              r"(?:\s+or more)?\s*days? (?:a|per|each) week")
_MODE_REMOTE_EXTRA = re.compile(
    r"work(?:ing)? (?:from home|remotely)(?![a-z])|work[- ]from[- ]home (?:role|position|job)|"
    r"remote[- ](?:first|based|eligible|friendly)(?![a-z])")
_MODE_HYBRID_EXTRA = re.compile(
    r"hybrid (?:schedule|work|working|model|arrangement|environment)(?![a-z])|"
    rf"{_PART_WEEK}[^.\n]{{0,40}}(?:office|on-?site|in[- ]person|campus)|"
    rf"(?:office|on-?site|in[- ]person|campus|location)[^.\n]{{0,40}}{_PART_WEEK}")
# A LinkedIn tag is the recruiter picking this field in a form, so it outranks
# whatever the company's workplace policy paragraph says.
_MODE_TAG = re.compile(r"#li-(remote|hybrid|on-?site)(?![a-z])")
_TITLE_MODE = re.compile(r"(?:^|[-–—,(|/:]\s*)(remote|hybrid)(?:\s*$|\s*[-–—)|/:])")


def _mode_name(word: str) -> str:
    if word.startswith("remote"):
        return "remote"
    return "hybrid" if word.startswith("hybrid") else "onsite"


def read_work_mode(blob: str) -> str:
    """`remote`, `hybrid`, `onsite` or `unknown`: the earliest statement wins, so a
    posting's own header outranks the company policy printed under it."""
    if not blob:
        return "unknown"
    tag = _MODE_TAG.search(blob)
    if tag:
        return _mode_name(tag.group(1))
    first_line = blob.lstrip().split("\n", 1)[0]
    if len(first_line) <= 120:
        title = _TITLE_MODE.search(first_line)
        if title:
            return title.group(1)
    for sentence in _sentences(blob):
        if _POLICY_PARAGRAPH.search(sentence):
            continue
        if _MODE_HYBRID_EXTRA.search(sentence):
            return "hybrid"
        best: Optional[Tuple[int, str]] = None
        for rx in _MODE_STATEMENTS:
            m = rx.search(sentence)
            if m and (best is None or m.start() < best[0]):
                best = (m.start(), _mode_name(m.group(1)))
        m = _MODE_REMOTE_EXTRA.search(sentence)
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), "remote")
        if best:
            return best[1]
    return "unknown"


_EMPLOYMENT_NOISE = re.compile(
    r"benefit|eligib|employees|colleagues|hires|students|basis|working model|"
    r"(?:all|our|for) (?:full|part)[- ]time|contract of employment|not a contract")
# "This full-time position is eligible for benefits" still says what the job is.
_EMPLOYMENT_THIS_ROLE = re.compile(r"this\s+(full|part)\s*-?\s*time\s+(?:position|role|job)")
_SEASONAL_ROLE = re.compile(
    r"(?<![a-z])(seasonal)\s+(?:[a-z]+\s+)?(?:associate|colleague|employee|worker|team member|"
    r"position|role|job|help|hire)")
_EMPLOYMENT_VALUE = (r"(full\s*-?\s*time|part\s*-?\s*time|contract(?:or)?|temporary|temp|seasonal|"
                     r"intern(?:ship)?|co-op|per diem|prn|regular)")
_EMPLOYMENT_NAMES = {"full": "full_time", "part": "part_time", "cont": "contract",
                     "temp": "temporary", "seas": "temporary", "inte": "internship",
                     "co-o": "internship", "per ": "part_time", "prn": "part_time",
                     "regu": "full_time"}
_EMPLOYMENT_STATEMENTS: Sequence[re.Pattern] = tuple(re.compile(p) for p in (
    rf"(?:employment|job|position|work|worker|contract|role|schedule)(?: type| status)?\s*[:\-]\s*"
    rf"(?:[a-z]+,\s*)?(?:regular\s+)?{_EMPLOYMENT_VALUE}(?![a-z])",
    rf"^\W*{_EMPLOYMENT_VALUE}(?![a-z])",
    rf"(?:this is an?|this (?:role|position|job) is(?: an?)?|is an?)\s+"
    rf"{_EMPLOYMENT_VALUE}(?![a-z])",
    rf"(?<![a-z-]){_EMPLOYMENT_VALUE}[ ,]+(?:[a-z]+ )?(?:role|position|job|opportunity|"
    rf"assignment|employment|schedule)(?![a-z])",
    r"1099|independent (contract)or",
    r"(contract)[- ]to[- ]hire|(temp)orary contract|\((contract)or\)|(seasonal) [a-z]+ associate",
    r"(intern(?:ship)?)(?![a-z])\s*(?:program|position|opportunity|role)",
))
_EMPLOYMENT_HOURS = re.compile(r"(\d{2})\s*\+?\s*(?:hours?|hrs?)\s*(?:per|a|/)\s*(?:week|wk)")


def _employment_name(word: str) -> str:
    word = word.replace(" ", "")
    return _EMPLOYMENT_NAMES.get("prn" if word.startswith("prn") else word[:4], NOT_STATED)


def read_employment_type(blob: str) -> str:
    """The earliest statement wins. Benefits wording never counts: it describes
    who gets a plan, not this job. Hours settle a posting that states only those."""
    sentences = _sentences(blob or "")
    for sentence in sentences:
        this_role = _EMPLOYMENT_THIS_ROLE.search(sentence)
        if this_role:
            return _employment_name(this_role.group(1))
        seasonal = _SEASONAL_ROLE.search(sentence)
        if seasonal:
            return "temporary"
        if _EMPLOYMENT_NOISE.search(sentence):
            continue
        best: Optional[Tuple[int, str]] = None
        for rx in _EMPLOYMENT_STATEMENTS:
            m = rx.search(sentence)
            if m and (best is None or m.start() < best[0]):
                word = next((g for g in m.groups() if g), "contract")
                best = (m.start(), _employment_name(word))
        if best and best[1] != NOT_STATED:
            return best[1]
    for sentence in sentences:
        hours = _EMPLOYMENT_HOURS.search(sentence)
        if hours and not _EMPLOYMENT_NOISE.search(sentence):
            return "full_time" if int(hours.group(1)) >= 35 else "part_time"
    return NOT_STATED


_ALTERNATIVE_JOIN = re.compile(r"\s+(?:and/)?or\s+")
_MAX_ITEM_WORDS = 3


def read_skill_alternatives(blob: str, find_skills: Callable[[str], List[str]]) -> List[List[str]]:
    """Skills the posting accepts instead of each other: "AWS, GCP or Azure".

    Only a tight list counts: every item must be a short phrase naming a hard
    skill, so "Excel or recruiting experience" and "ci/cd" make no group.
    Reported beside the skill lists, not applied to them: `fit_score` still
    counts each one as required until it is taught to read these.
    """
    from services import fit_score

    groups: List[List[str]] = []
    for sentence in _sentences(blob or ""):
        for m in _ALTERNATIVE_JOIN.finditer(sentence):
            right = re.split(r"[,;:.()]", sentence[m.end():], maxsplit=1)[0]
            items = [" ".join(right.split()[:_MAX_ITEM_WORDS])]
            pieces = re.split(r",\s*", sentence[:m.start()])
            for position, piece in enumerate(reversed(pieces)):
                words = piece.split()
                if position and len(words) > _MAX_ITEM_WORDS:
                    break
                candidate = " ".join(words[-_MAX_ITEM_WORDS:])
                if not find_skills(candidate):
                    break
                items.insert(0, candidate)
            named = [(find_skills(item) or [""])[-1 if i < len(items) - 1 else 0]
                     for i, item in enumerate(items)]
            named = list(dict.fromkeys(n for n in named if n))
            if 2 <= len(named) <= 5 and len(fit_score._hard_skills(named)) == len(named) \
                    and named not in groups:
                groups.append(named)
    return groups[:8]


# ---------------------------------------------------------------------------
# The dictionary
# ---------------------------------------------------------------------------

# Not technical. NOT `fit_score._GENERIC_SKILLS`, which means "does not
# discriminate between candidates" and so holds Excel, CRM and project
# management: those are skills a job asks for, just common ones. The last group is
# measured: lexicon terms the labellers agreed with under 30% of the time, usually
# because the word describes the job ("recruiting" in an equal-opportunity notice,
# "documentation" as a duty) rather than a skill it asks for.
_SOFT_SKILLS = frozenset({
    "communication", "teamwork", "collaboration", "interpersonal", "leadership",
    "problem solving", "critical thinking", "time management", "organization",
    "attention to detail", "multitasking", "adaptability", "work ethic",
    "self motivated", "public speaking", "presentation", "mentoring", "negotiation",
    "stakeholder management", "bilingual",
    "compliance", "recruiting", "documentation", "reporting", "monitoring", "onboarding",
    "process improvement", "patient care", "quality control", "accessibility",
    "safety compliance", "requirements gathering", "auditing", "roadmapping",
    "hipaa compliance",
})

_VOCABULARY = re.compile(
    r"(?<![a-z0-9])(?:" + "|".join(
        re.escape(v) for v in sorted(JD_SKILL_VOCABULARY, key=len, reverse=True))
    + r")(?![a-z0-9])")


def read_additional_skills(blob: str, known: Sequence[str]) -> List[str]:
    """Skills the JD names that the scoring lexicon cannot, in order of appearance."""
    seen = set(known)
    out: List[str] = []
    for m in _VOCABULARY.finditer(blob or ""):
        name = m.group(0)
        if name not in seen:
            seen.add(name)
            out.append(name)
    return out[:20]


def read_fields(blob: str, *, min_years: Optional[int], clearance_required: bool,
                find_skills: Callable[[str], List[str]],
                preferred_years: Optional[int] = None) -> Dict[str, Any]:
    """The block stored at `requirements["jd"]`. `blob` is `fit_score._norm`ed."""
    return {
        "v": JD_PROFILE_VERSION,
        "sponsorship": read_sponsorship(blob),
        "authorization": read_authorization(blob, clearance_required),
        "education": read_education(blob),
        "experience": read_experience(blob, min_years, preferred_years),
        "clearance_level": clearance_level(blob, clearance_required),
        "certifications": read_certifications(blob),
        "employment_type": read_employment_type(blob),
        "skill_alternatives": read_skill_alternatives(blob, find_skills),
        "additional_skills": read_additional_skills(blob, find_skills(blob)),
    }


def build(jd_text: Any, title: Any = "", *,
          analysis: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """THE JD dictionary. `analysis` is an LLM `JD_ANALYSIS`, when there is one.

    Eligibility facts always come from the deterministic readers, so the same
    posting cannot read one way in the feed and another in tailoring. The LLM
    adds what regex cannot produce: skills beyond the lexicon, responsibilities
    and keywords.
    """
    from services import fit_score, salary_normalizer

    llm = analysis if isinstance(analysis, dict) else {}
    role_title = str(title or llm.get("job_title") or "")
    reqs = fit_score.extract_requirements(jd_text, role_title)
    jd = reqs["jd"]

    required = list(reqs.get("required_skills") or [])
    preferred = list(reqs.get("preferred_skills") or [])
    for target, key in ((required, "required_skills"), (preferred, "preferred_skills")):
        for skill in llm.get(key) or []:
            name = str(skill).strip().lower()
            if name and name not in required and name not in preferred:
                target.append(name)
    named = required + preferred

    try:
        compensation = salary_normalizer.normalize(text=str(jd_text or ""))
    except Exception:  # a salary we cannot read must not cost the dictionary
        compensation = None

    return {
        "version": JD_PROFILE_VERSION,
        "content_hash": reqs.get("content_hash"),
        "role": {
            "title": role_title,
            "company": str(llm.get("company") or ""),
            "location": str(llm.get("location") or ""),
            "family": reqs.get("role_family"),
            "level": reqs.get("job_level"),
            "work_mode": reqs.get("work_mode"),
            "employment_type": jd["employment_type"],
            "industries": list(reqs.get("industries") or []),
        },
        "experience": dict(jd["experience"]),
        "education": dict(jd["education"]),
        "skills": {
            "required": required,
            "preferred": preferred,
            "technical": [s for s in named if s not in _SOFT_SKILLS]
            + [s for s in jd["additional_skills"] if s not in named],
            "additional": list(jd["additional_skills"]),
            "soft": [s for s in named if s in _SOFT_SKILLS],
            "alternatives": [list(g) for g in jd["skill_alternatives"]],
            "certifications": list(jd["certifications"]),
        },
        "work_eligibility": {
            "sponsorship": dict(jd["sponsorship"]),
            "authorization": dict(jd["authorization"]),
            "restricted": bool(reqs.get("work_authorization_restricted")),
            "clearance_required": bool(reqs.get("clearance_required")),
            "clearance_level": jd["clearance_level"],
            "licenses": [dict(l) for l in reqs.get("licenses") or []],
        },
        "compensation": compensation,
        "content": {
            "responsibilities": list(llm.get("responsibilities") or []),
            "qualifications": list(llm.get("qualifications") or []),
            "keywords": list(llm.get("keywords") or []),
        },
        "provenance": {
            "deterministic": True,
            "llm": bool(llm),
            "skills_source": reqs.get("requirements_source"),
            "text_length": reqs.get("text_length"),
        },
    }


def prompt_view(analysis: Any) -> Any:
    """`analysis` as a prompt may see it: `jd_profile` is replaced by `job_facts`.

    Prompts dump the analysis whole, so without this they would carry raw JD
    quotes and sponsorship wording, which are no use to a résumé writer and are
    the one thing a résumé must never echo. Eligibility stays out on purpose.
    """
    if not isinstance(analysis, dict) or "jd_profile" not in analysis:
        return analysis
    view = {k: v for k, v in analysis.items() if k != "jd_profile"}
    profile = analysis["jd_profile"]
    if isinstance(profile, dict):
        experience = profile.get("experience") or {}
        education = profile.get("education") or {}
        skills = profile.get("skills") or {}
        view["job_facts"] = {
            "min_years": experience.get("min_years"),
            "max_years": experience.get("max_years"),
            "min_degree": education.get("min_degree"),
            "preferred_degree": education.get("preferred_degree"),
            "degree_fields": education.get("fields") or [],
            "skill_alternatives": skills.get("alternatives") or [],
            "certifications": skills.get("certifications") or [],
        }
    return view


def attach_to_analysis(analysis: Any, jd_text: Any) -> Any:
    """Add `jd_profile` to an LLM analysis without touching what it returned.

    Never raises: tailoring must not fail because of an optional dictionary.
    """
    if not isinstance(analysis, dict):
        return analysis
    try:
        analysis["jd_profile"] = build(jd_text, analysis.get("job_title") or "",
                                       analysis=analysis)
    except Exception:  # pragma: no cover - defensive, see docstring
        pass
    return analysis
