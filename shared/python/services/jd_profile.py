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


def read_sponsorship(blob: str) -> Dict[str, str]:
    """`{"status", "evidence"}`. A negative statement outranks a positive one.

    "Must be authorized to work in the US" is deliberately NOT read as
    `not_offered`: it is said by employers who do sponsor, so it is reported
    by `read_authorization` instead.
    """
    if not blob or "sponsor" not in blob:
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
    r"must be a (?:lawful )?permanent resident")
_AUTH_CITIZEN = re.compile(
    r"must (?:be|hold) (?:a )?(?:u\.?s\.?|united states) citizen|"
    r"(?:u\.?s\.?|united states) citizenship (?:is |are )?required|"
    r"requires? (?:u\.?s\.?|united states) citizenship|"
    r"(?<![a-z])citizens? only(?![a-z])")
_AUTH_ANY = re.compile(
    r"(?:authori[sz]ed|eligible|entitled|legally able|permitted) to work|"
    r"right to work|work authori[sz]ation|work authori[sz]ed|"
    r"valid work (?:permit|visa)")
# Compliance boilerplate that names work authorization without requiring it.
_AUTH_BOILERPLATE = re.compile(
    r"e-?verify|i-9|employment eligibility verification|"
    r"non-?discrimination|equal (?:employment )?opportunit")


def read_authorization(blob: str) -> Dict[str, str]:
    """`{"requirement", "evidence"}`, strictest statement first."""
    if not blob:
        return {"requirement": NOT_STATED, "evidence": ""}
    fallback = ""
    for sentence in _sentences(blob):
        if _AUTH_CITIZEN_OR_PR.search(sentence):
            return {"requirement": AUTH_CITIZEN_OR_PR, "evidence": _evidence(sentence)}
        if _AUTH_CITIZEN.search(sentence):
            return {"requirement": AUTH_CITIZEN, "evidence": _evidence(sentence)}
        if not fallback and _AUTH_ANY.search(sentence) \
                and not _AUTH_BOILERPLATE.search(sentence):
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
        r"(?<![a-z])m\.d\.|(?<![a-z])j\.d\.(?!\s*(?:power|irving|edwards|vance|williams))|juris doctor|"
        r"(?<![a-z,] )(?<![a-z])(?:md|jd)\s+(?:degree|required)")),
    (3, re.compile(
        r"(?<![a-z-])(?<!scrum )master'?s(?![a-z])|(?<![a-z-])masters?\s+degree(?![a-z])|"
        r"(?<![a-z-])master of (?:science|arts|business|engineering|education|public|fine|"
        r"laws|computer|technology|health|social|information)(?![a-z])|"
        r"(?<![a-z])m\.?b\.?a(?![a-z])|(?<![a-z])m\.eng(?![a-z])|(?<![a-z])msc(?![a-z])|"
        r"(?<![a-z.])m\.s\.?(?![a-z])|(?<![a-z.])m\.a\.(?![a-z])(?!\s*certif)|"
        rf"(?<![a-z.])(?<!, )ms{_ABBR_NEXT}")),
    (2, re.compile(
        r"(?<![a-z])bachelor'?s?(?![a-z])|(?<![a-z])b\.?tech(?![a-z])|(?<![a-z])bsc(?![a-z])|"
        r"(?<![a-z])bsn(?![a-z])|(?<![a-z])b\.e\.(?![a-z])|undergraduate degree|"
        r"(?:four|4)[- ]year degree|(?:college|university) degree|"
        r"(?:a|relevant|related|technical|engineering)\s+degree(?![a-z])(?!\s+of(?![a-z]))|"
        r"(?<![a-z\d-])(?<!\d )degree (?:in|qualified)(?![a-z])|"
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
    r"less than (?:a )?high school|"
    # The path offered INSTEAD of the degree, not a lower minimum.
    r"in lieu of|in place of|"
    r"we (?:also )?(?:offer|provide)")
# "degree, preferably in biology" prefers the FIELD, not the degree.
_FIELD_PREFERENCE = re.compile(r"prefer(?:ably|red)\s+(?:in|within|from)(?![a-z])[^.;,\n]*")
# Preference wording `fit_score._PREFERRED_CUE` does not carry.
_EDU_PREFERRED_EXTRA = re.compile(r"(?<![a-z])(?:an asset|beneficial|considered a)(?![a-z])")
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
        if len(stripped) <= 90:
            if fit_score._PREFERRED_HEAD.search(stripped):
                mode = "preferred"
            elif fit_score._REQUIRED_HEAD.search(stripped):
                mode = "required"
        for sentence in _sentences(stripped):
            if _EDU_EQUIVALENT.search(sentence) or "in lieu of" in sentence:
                saw_equivalent = True
            if not _degree_levels(sentence) or _EDU_NOISE.search(sentence):
                continue
            sentence = _FIELD_PREFERENCE.sub("", sentence)
            sentence_pref = bool(fit_score._PREFERRED_CUE.search(sentence))
            sentence_req = bool(fit_score._REQUIRED_CUE.search(sentence))
            for name, rx in _EDU_FIELDS:
                if rx.search(sentence) and name not in fields:
                    fields.append(name)
            for clause in _EDU_CLAUSE.split(sentence):
                # The split ate the comma that makes "bs," a degree.
                levels = _degree_levels(clause + ",")
                if not levels:
                    continue
                clause_pref = bool(fit_score._PREFERRED_CUE.search(clause)
                                   or _EDU_PREFERRED_EXTRA.search(clause))
                clause_req = bool(fit_score._REQUIRED_CUE.search(clause))
                if clause_pref or clause_req:
                    is_pref = clause_pref and not clause_req
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


def read_experience(blob: str, min_years: Optional[int]) -> Dict[str, Any]:
    """`min_years` is `fit_score._required_years`; this adds the top of a range."""
    if min_years is None:
        return {"min_years": None, "max_years": None, "evidence": ""}
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
    return {"min_years": min_years, "max_years": max_years, "evidence": _evidence(evidence)}


_CLEARANCE_LEVELS: Sequence[Tuple[str, re.Pattern]] = (
    ("ts_sci", re.compile(r"ts/sci|ts\s*/\s*sci|top secret\s*/\s*sci")),
    ("top_secret", re.compile(r"top[ -]secret")),
    ("secret", re.compile(r"(?<![a-z])secret (?:clearance|level|security|or higher)|"
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
        ("Six Sigma", r"six sigma"),
        ("ITIL", r"itil"),
        ("CFA", r"cfa"),
        ("PE License", r"professional engineer(?:ing)? licen[sc]e"),
    ))


def read_certifications(blob: str) -> List[str]:
    return [name for name, rx in _CERTIFICATIONS if rx.search(blob or "")][:10]


_EMPLOYMENT_TYPES: Sequence[Tuple[str, re.Pattern]] = (
    ("internship", re.compile(r"(?<![a-z])(?:internship|co-op)(?![a-z])")),
    ("contract", re.compile(
        r"contract[- ]to[- ]hire|(?:contract|c2c|1099)\s+(?:role|position|job|opportunity|assignment)|"
        r"employment type:?\s*contract")),
    ("temporary", re.compile(r"(?<![a-z])temporary\s+(?:role|position|job|assignment)|"
                             r"(?<![a-z])seasonal\s+(?:role|position|job)")),
    ("part_time", re.compile(r"(?<![a-z])part[- ]time(?![a-z])")),
    ("full_time", re.compile(r"(?<![a-z])full[- ]time(?![a-z])")),
)


def read_employment_type(blob: str) -> str:
    """The earliest statement wins: a body saying "full-time" up top and "all
    part-time employees are eligible" under benefits is a full-time job."""
    best: Tuple[int, str] = (len(blob or "") + 1, NOT_STATED)
    for name, rx in _EMPLOYMENT_TYPES:
        m = rx.search(blob or "")
        if m and m.start() < best[0]:
            best = (m.start(), name)
    return best[1]


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

# People skills only. NOT `fit_score._GENERIC_SKILLS`, which means "does not
# discriminate between candidates" and so holds Excel, CRM and project
# management: those are skills a job asks for, just common ones.
_SOFT_SKILLS = frozenset({
    "communication", "teamwork", "collaboration", "interpersonal", "leadership",
    "problem solving", "critical thinking", "time management", "organization",
    "attention to detail", "multitasking", "adaptability", "work ethic",
    "self motivated", "public speaking", "presentation", "mentoring", "negotiation",
    "stakeholder management",
})


def read_fields(blob: str, *, min_years: Optional[int], clearance_required: bool,
                find_skills: Callable[[str], List[str]]) -> Dict[str, Any]:
    """The block stored at `requirements["jd"]`. `blob` is `fit_score._norm`ed."""
    return {
        "v": JD_PROFILE_VERSION,
        "sponsorship": read_sponsorship(blob),
        "authorization": read_authorization(blob),
        "education": read_education(blob),
        "experience": read_experience(blob, min_years),
        "clearance_level": clearance_level(blob, clearance_required),
        "certifications": read_certifications(blob),
        "employment_type": read_employment_type(blob),
        "skill_alternatives": read_skill_alternatives(blob, find_skills),
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
            "technical": [s for s in named if s not in _SOFT_SKILLS],
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
