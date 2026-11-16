"""The ATS score: one strict, explainable number for a resume against one job.

What it is for
--------------
After tailoring, the person sees ONE number: how well an applicant tracking
system plus a recruiter's first screen would rank this resume for this job.
It is computed here, in plain Python, from the structured resume, the analysed
job description and (when there is one) the rendered PDF's parse check. No
model call, so it costs nothing and gives the same answer every time.

Why it is built this way (research, 2026-11-16)
-----------------------------------------------
Real systems grade EVIDENCE against required qualifications, not keyword
counts: Workday HiredScore grades A-D on whether every required qualification
is met (B = all required, A = all required plus most preferred); Ashby counts
criteria met with citations; Oracle Recruiting averages skills, relevant
experience, domain and seniority; Textkernel's parser ties each skill to the
job it was used in and computes years from date ranges with overlaps merged.
Recruiters filter on skills (76%), education (60%), title (55%),
certifications (51%) and years (44%) (Jobscan recruiter survey, 2025). The
consumer tools (Jobscan, Resume Worded, Rezi) count keywords and are easy to
max out; this one is not.

So a skill earns full credit only when an experience bullet shows it with a
result, partial credit in a project or plain bullet, and little when it is
only in the skills list. Years, degree and parse failures CAP the score
instead of nudging it, the way knockouts work. 90+ needs nearly every
must-have proven in experience.

Components (100)
----------------
  must_have    30  required skills, by evidence tier
  nice_to_have 12  preferred skills (8) + job duties echoed in bullets (4)
  title        12  held job title in the same family and level
  years        10  months in relevant roles, overlaps merged, vs years asked
  education     8  required degree (6) + required certifications (2)
  impact       10  results in bullets, action verbs, length, no filler
  readable     10  contact details, dated roles, standard sections, PDF text
  first_look    8  what a recruiter sees in the first seconds

Deductions (at most -15): a job term repeated over and over, a sentence copied
from the posting, an overlong skills list, must-haves that are only listed.

The version string travels with every result so a stored score says which
rules produced it. Bump ATS_SCORE_VERSION when the rules change.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

ATS_SCORE_VERSION = "ats-1.0"

COMPONENT_MAX = {
    "must_have": 36,
    "nice_to_have": 12,
    "title": 10,
    "years": 10,
    "education": 8,
    "impact": 10,
    "readable": 8,
    "first_look": 6,
}
COMPONENT_LABELS = {
    "must_have": "Must-have skills",
    "nice_to_have": "Nice-to-have skills",
    "title": "Job title match",
    "years": "Years of experience",
    "education": "Education",
    "impact": "Results in your bullets",
    "readable": "Easy for an ATS to read",
    "first_look": "First glance",
}

# (min score, key, label). Plain words a first-time user understands.
BANDS: Tuple[Tuple[int, str, str], ...] = (
    (90, "excellent", "Excellent"),
    (80, "strong", "Strong"),
    (70, "good", "Good"),
    (55, "fair", "Fair"),
    (40, "weak", "Weak"),
    (0, "needs_work", "Needs work"),
)

# Evidence tiers for one job skill.
TIER_CREDIT = {"result": 1.0, "used": 0.75, "project": 0.75, "listed": 0.3, "missing": 0.0}
STALE_YEARS = 5
STALE_FACTOR = 0.7
# A bullet naming this many job skills is a list wearing a bullet's clothes,
# and so is one with a comma list of three or more of them.
DUMP_TERMS = 5
_COMMA_LIST = re.compile(r"(?:[\w+#./&-]+(?:\s[\w+#./&-]+)?\s*,\s*){2,}(?:(?:and|or|&)\s+)?[\w+#./&-]+(?:\s[\w+#./&-]+)?")
# A result in one bullet proves at most this many skills.
RESULT_SKILLS_PER_BULLET = 2

# A result: a percentage, money, a multiplier, a scale suffix, or a count of
# something. Version numbers ("Python 3.11", "Kubernetes 1.28", "GitLab 16")
# are stripped first (`_strip_versions`): they are not outcomes.
_METRIC = re.compile(
    r"\d+(?:\.\d+)?\s*%|\$\s?\d|\b\d+(?:\.\d+)?\s*[xX]\b|\b\d+(?:\.\d+)?\s*(?:k|m|b|K|M|B)\b|"
    r"\b(?!(?:19|20)\d{2}\b)\d{2,}(?:,\d{3})*\b|\b\d+\s*(?:ms|s|sec|seconds|minutes|hours|days|weeks|months|users|"
    r"customers|clients|patients|students|requests|transactions|services|endpoints|tests|engineers|people|"
    r"teams|countries|stores|sites|accounts|projects|reports|members)\b",
    re.IGNORECASE)
_VERSION = re.compile(
    r"\bv\d+(?:\.\d+)*\b|\b(?:version|ver\.?)\s*\d+(?:\.\d+)*|\b[A-Za-z][\w+#-]*\s+\d+\.\d+(?:\.\d+)*\b|"
    r"(?<=\s)[A-Z][A-Za-z+#]+\s\d{1,2}(?=[\s,.;)]|$)(?!\s+(?:users|customers|clients|patients|students|"
    r"engineers|people|teams|services|requests|stores|sites|countries|members|projects|accounts|%))")
_WEAK_OPENERS = re.compile(
    r"^\s*(?:responsible for|worked on|helped|assisted|duties included|tasked with|involved in|"
    r"participated in|in charge of)\b", re.IGNORECASE)
_WEAK_VERBS = {"worked", "helped", "assisted", "participated", "involved", "tasked", "responsible", "used"}
_FIRST_PERSON = re.compile(r"(?<![\w/])(?:I|me|my|mine|myself)(?![\w/])")
_IRREGULAR_VERBS = {
    "built", "led", "ran", "made", "wrote", "drove", "cut", "grew", "set", "took", "won", "taught",
    "sped", "spun", "rebuilt", "rewrote", "brought", "held", "kept", "found", "sold", "shrank",
    "own", "build", "lead", "design", "develop", "create", "ship", "drive", "manage", "run",
    "write", "deliver", "launch", "migrate", "automate", "architect", "scale", "mentor",
    "implement", "analyze", "analyse", "maintain", "improve", "reduce", "increase", "partner",
    "coordinate", "support", "operate", "deploy", "integrate", "optimize", "spearhead", "teach",
    "treat", "care", "audit", "prepare", "reconcile", "plan", "train", "serve",
}
_STOPWORDS = frozenset(
    "a an the and or of to in on for with by at from as is are be been was were this that these those "
    "our your their its it we you they will can may must should using use used across within into "
    "over under per via etc including such other all any each new work working ability strong "
    "experience years year team teams role job candidate skills knowledge understanding".split())
# Soft words a job ad uses that are not skills anyone can show.
_NOT_A_SKILL = frozenset(
    "fast-paced collaboration collaborative ownership communication teamwork team player self-starter "
    "detail-oriented passionate motivated driven innovative proactive leadership problem-solving "
    "problem solving initiative curiosity adaptability flexible dynamic".split())

# On a RESUME the degree field is about a degree, so short forms are safe.
_DEGREE_LEVELS: Tuple[Tuple[int, str, re.Pattern], ...] = (
    (4, "doctorate", re.compile(
        r"\bph\.?\s?d\b|\bdoctor(?:ate|al)?\b|\bd\.?phil\b|\bpharm\.?\s?d\b|\bm\.?d\.?\b(?=\s|$|,)|"
        r"\bj\.?d\.?\b(?=\s|$|,)|\bd\.?d\.?s\b|\bd\.?v\.?m\b|\bed\.?d\b|\bd\.?n\.?p\b|\bmbbs\b", re.I)),
    (3, "master's", re.compile(
        r"\bmaster|\bm\.?\s?s\.?c?\b|\bmba\b|\bm\.?\s?eng\b|\bm\.?\s?tech\b|\bm\.?\s?a\b|\bm\.?\s?c\.?a\b|"
        r"\bmsn\b|\bm\.?\s?com\b|\bmph\b|\bmpa\b|\bllm\b|\bmfa\b|\bm\.?\s?pharm\b|\bm\.?\s?arch\b|\bmca\b", re.I)),
    (2, "bachelor's", re.compile(
        r"\bbachelor|\bb\.?\s?s\.?c?\b|\bb\.?\s?a\b|\bb\.?\s?e\b|\bb\.?\s?eng\b|\bb\.?\s?tech\b|\bb\.?\s?c\.?a\b|"
        r"\bbsn\b|\bb\.?\s?com\b|\bbba\b|\bllb\b|\bb\.?\s?arch\b|\bbfa\b|\bb\.?\s?pharm\b|\bundergraduate\b", re.I)),
    (1, "associate's", re.compile(r"\bassociate|\ba\.?\s?a\.?s\b|\ba\.?\s?s\.?\b(?= in)", re.I)),
)
_NO_DEGREE = re.compile(r"\b(?:high school|secondary school|ged|diploma|certificate|bootcamp)\b", re.I)
# In a JOB POSTING short forms count only beside degree words: "MS Office",
# "MS/Azure" and "BA team" are not degrees.
_JD_DEGREE_CONTEXT = re.compile(
    r"\bdegree\b|\bdiploma\b|\bmajor\b|\brelated (?:field|discipline)\b|\bin (?:a )?(?:computer|engineering|"
    r"math|mathematics|science|information|business|statistics|data|electrical|mechanical|nursing|finance|"
    r"accounting|economics|physics|related|a related|technical|cs|cse|ece|ee|it|stem|is|mis)\b", re.I)
_ABBR_TAIL = r"\.?(?=\s*(?:in\b|or\b|and\b|degree|/|,|\(|with\b))"
_JD_DEGREE_LEVELS: Tuple[Tuple[int, str, re.Pattern, bool], ...] = (
    # (level, name, pattern, needs degree context)
    (4, "doctorate", re.compile(r"(?i:\bph\.?\s?d\b|\bdoctorate\b|\bdoctoral degree\b)"), False),
    (3, "master's", re.compile(r"(?i:\bmaster(?:'?s)?\b|\bmba\b)"), False),
    (3, "master's", re.compile(r"\b(?:MS|M\.S|MSc|M\.Sc|MEng|M\.Eng|MTech|M\.Tech)" + _ABBR_TAIL), True),
    (2, "bachelor's", re.compile(r"(?i:\bbachelor|\bundergraduate degree\b|\b(?:4|four)[- ]year degree\b)"), False),
    (2, "bachelor's", re.compile(r"\b(?:BS|B\.S|BSc|B\.Sc|BA|B\.A|BE|B\.E|BTech|B\.Tech)" + _ABBR_TAIL), True),
    (1, "associate's", re.compile(r"(?i:\bassociate'?s? degree\b)"), False),
)
_DEGREE_PREFERRED = re.compile(r"\b(?:prefer\w*|plus|nice to have|bonus|desired|ideally)\b", re.I)
_DEGREE_EQUIVALENT = re.compile(r"\bor (?:equivalent|comparable|related|relevant)\b[^.;\n]{0,40}"
                                r"\b(?:experience|work|training)\b|\bin lieu\b", re.I)
_CERT_TERM = re.compile(r"\bcertif\w*\b|\blicen[sc]\w*\b|\b(?:cka|ckad|cissp|cisa|cism|pmp|ccna|ccnp|oscp|rhce|"
                        r"security\+|comptia|cpa|cfa|cma|bls|acls|pals|cpr|ccrn|cna|lpn|series \d+)\b", re.I)
_YEARS_FIG = re.compile(r"(\d{1,2})\s*\+?\s*(?:(?:-|to)\s*\d{1,2}\s*\+?\s*)?(?:years?|yrs?)\b", re.I)
_YEARS_ABOUT_WORK = re.compile(
    r"\b(?:experience|exp\b|work(?:ing)?|industry|professional|relevant|hands-on|building|developing|"
    r"engineering|programming|managing|leading|practice|clinical|teaching)\b", re.I)
_YEARS_NOT_WORK = re.compile(r"\b(?:age|old|graduat\w*|licens\w*|within the (?:last|past)|warranty)\b", re.I)
_SENIORITY_RANK = {"intern": 0, "entry": 1, "junior": 1, "mid": 2, "senior": 3, "staff": 4,
                   "principal": 5, "lead": 4, "manager": 4, "director": 6, "executive": 7}
_INTERN_TITLE = re.compile(r"\b(?:intern|internship|co-?op|trainee|apprentice)\b", re.I)
_EVIDENCE_SECTIONS = re.compile(r"research|volunteer|teaching|clinical|leadership|service|fieldwork|practicum", re.I)
_VENDOR_PREFIX = re.compile(r"^(?:microsoft|google|amazon|apache|adobe|oracle|ibm|salesforce)\s+", re.I)
_CERT_TAIL = re.compile(r"\s+(?:certification|certificate|certified|license|licensure)$", re.I)

# Dates: one reader for every format resumes use.
_MONTHS = {m: i for i, ms in enumerate(
    (("jan",), ("feb",), ("mar",), ("apr",), ("may",), ("jun",), ("jul",), ("aug",), ("sep", "sept"),
     ("oct",), ("nov",), ("dec",)), start=1) for m in ms}
_SEASONS = {"spring": (3, 5), "summer": (6, 8), "fall": (9, 12), "autumn": (9, 12), "winter": (12, 2)}
_DATE_TOKEN = re.compile(
    r"(?P<mon>jan|feb|mar|apr|may|jun|jul|aug|sept?|oct|nov|dec)[a-z]*\.?,?\s*'?(?P<y1>(?:19|20)\d{2})|"
    r"(?P<season>spring|summer|fall|autumn|winter)\s*'?(?P<y4>(?:19|20)\d{2})|"
    r"\b(?P<mm>\d{1,2})\s*[/.-]\s*(?P<y2>(?:19|20)\d{2})\b|"
    r"\b(?P<y3>(?:19|20)\d{2})\s*[/.-]\s*(?P<mm2>\d{1,2})\b(?![\d/])|"
    r"\b(?P<y5>(?:19|20)\d{2})\b|"
    r"\b(?P<present>present|current|now|ongoing|today|till date|to date)\b",
    re.IGNORECASE)


# ── input: one shape, whatever arrived ────────────────────────────────────

def _s(value: Any) -> str:
    return value if isinstance(value, str) else ("" if value is None else str(value))


def _as_list(value: Any, split: str = r"[,;\n]") -> List[str]:
    """A list of non-empty strings from a list, a string or nothing."""
    if isinstance(value, str):
        return [p.strip() for p in re.split(split, value) if p.strip()]
    if isinstance(value, (list, tuple)):
        out: List[str] = []
        for v in value:
            if isinstance(v, str) and v.strip():
                out.append(v.strip())
            elif isinstance(v, (int, float)):
                out.append(str(v))
        return out
    return []


def _dicts(value: Any) -> List[Dict[str, Any]]:
    return [v for v in value if isinstance(v, dict)] if isinstance(value, (list, tuple)) else []


def _normalize_resume(resume: Any) -> Dict[str, Any]:
    r = resume if isinstance(resume, dict) else {}
    skills_raw = r.get("skills")
    skills: Dict[str, List[str]] = {}
    if isinstance(skills_raw, dict):
        for k, v in skills_raw.items():
            items = _as_list(v)
            if items:
                skills[_s(k)] = items
    else:
        items = _as_list(skills_raw)
        if items:
            skills["Skills"] = items

    def entry(e: Dict[str, Any]) -> Dict[str, Any]:
        out = {k: _s(e.get(k)) for k in ("title", "company", "dates", "type", "name", "tech", "degree",
                                          "institution", "coursework", "heading", "semantic_type", "body")}
        out["bullets"] = _as_list(e.get("bullets"), r"\n")
        out["entries"] = [entry(x) for x in _dicts(e.get("entries"))]
        return out

    return {
        "contact": r.get("contact") if isinstance(r.get("contact"), dict) else {},
        "summary": _s(r.get("summary")),
        "skills": skills,
        "experience": [entry(e) for e in _dicts(r.get("experience"))],
        "projects": [entry(e) for e in _dicts(r.get("projects"))],
        "education": [entry(e) for e in _dicts(r.get("education"))],
        "certifications": _as_list(r.get("certifications"), r"\n"),
        "additional_sections": [entry(e) for e in _dicts(r.get("additional_sections"))],
    }


def _normalize_jd(jd: Any) -> Dict[str, Any]:
    j = dict(jd) if isinstance(jd, dict) else {}
    for key in ("required_skills", "preferred_skills", "keywords"):
        j[key] = _as_list(j.get(key))
    for key in ("responsibilities", "qualifications"):
        j[key] = _as_list(j.get(key), r"\n|(?<=[.;])\s+")
    for key in ("job_title", "company", "experience_years"):
        j[key] = _s(j.get(key))
    return j


# ── small text helpers ────────────────────────────────────────────────────

def _words(text: str) -> List[str]:
    return re.findall(r"[a-z0-9+#.]+", _s(text).lower())


def _content_words(text: str) -> List[str]:
    out = []
    for w in _words(text):
        w = w.strip(".")
        if len(w) < 3 or w in _STOPWORDS or w.isdigit():
            continue
        # A crude stem so "deploying" and "deployed" meet.
        out.append(re.sub(r"(?:ing|ed|es|e|s)$", "", w) if len(w) > 5 else w)
    return out


def _bullets_of(entry: Dict[str, Any]) -> List[str]:
    return [b for b in (entry.get("bullets") or []) if b.strip()]


def _skills_list(resume: Dict[str, Any]) -> List[str]:
    return [i for items in (resume.get("skills") or {}).values() for i in items]


def _full_text(resume: Dict[str, Any]) -> str:
    """What the rendered resume says. A project's `tech` field is not printed
    on the PDF, so it is not read here either."""
    parts = [resume["summary"], ", ".join(_skills_list(resume))]
    for e in resume["experience"]:
        parts += [e["title"], e["company"]] + _bullets_of(e)
    for p in resume["projects"]:
        parts += [p["name"]] + _bullets_of(p)
    for e in resume["education"]:
        parts += [e["degree"], e["institution"], e["coursework"]]
    parts += resume["certifications"]
    return "\n".join(x for x in parts if x)


def _strip_versions(text: str) -> str:
    return _VERSION.sub(" ", text)


def _term_forms(term: str) -> List[str]:
    """"Microsoft Excel" is met by "Excel"; "BLS certification" by "BLS"."""
    forms = [term]
    for rx in (_CERT_TAIL, _VENDOR_PREFIX):
        cut = rx.sub("", term).strip()
        if cut and cut.lower() != term.lower() and len(cut) > 1:
            forms.append(cut)
    return forms


def _locate(text: str, term: str) -> Optional[int]:
    """Where in `text` the term (or one of its forms) appears, or None."""
    from services.requirement_matcher import match_term_in_text
    if not text:
        return None
    for form in _term_forms(term):
        hit = match_term_in_text(text, form)
        if hit.found:
            idx = text.lower().find((hit.matched_form or form).lower())
            return idx if idx >= 0 else 0
    # The same words in another form: "a rolling forecast" shows Forecasting.
    # Only for stems long enough to mean one thing.
    want = set(_content_words(term))
    if want and all(len(w) >= 5 for w in want):
        have = _content_words(text)
        if want <= set(have):
            first = min(have.index(w) for w in want)
            return text.lower().find(_words(text)[0]) if first == 0 else max(0, len(" ".join(have[:first])))
    return None


def _match(text: str, term: str) -> bool:
    return _locate(text, term) is not None


def _shingles(text: str, n: int) -> List[str]:
    words = _words(text)
    return [" ".join(words[i:i + n]) for i in range(max(0, len(words) - n + 1))]


def _echoes(text: str, duties: List[set]) -> bool:
    """A near paraphrase of one of the posting's duties that adds little of
    its own: it may show the skill, but it is not a result."""
    have = set(_content_words(text))
    for want in duties:
        if len(want) >= 4 and len(want & have) >= 0.8 * len(want) and len(have - want) <= 3:
            return True
    return False


def _is_copied(text: str, posting: set) -> bool:
    """Most of this line is the job posting's own words, in order."""
    grams = _shingles(text, 5)
    if len(grams) < 3 or not posting:
        return False
    return sum(1 for g in grams if g in posting) / len(grams) >= 0.5


# ── dates ────────────────────────────────────────────────────────────────

def _token_dates(m: "re.Match", today: date) -> Tuple[Optional[date], Optional[date], bool]:
    """(first day, last day, is_present) for one date token."""
    if m.group("present"):
        return today, today, True
    if m.group("mon"):
        y, mo = int(m.group("y1")), _MONTHS[m.group("mon").lower()[:4] if m.group("mon").lower().startswith("sept") else m.group("mon").lower()[:3]]
        return date(y, mo, 1), date(y, mo, 28), False
    if m.group("season"):
        y = int(m.group("y4"))
        a, b = _SEASONS[m.group("season").lower()]
        return date(y, a, 1), date(y + (1 if b < a else 0), b, 28), False
    if m.group("mm"):
        mo = int(m.group("mm"))
        if 1 <= mo <= 12:
            y = int(m.group("y2"))
            return date(y, mo, 1), date(y, mo, 28), False
        return None, None, False
    if m.group("y3"):
        mo = int(m.group("mm2"))
        if 1 <= mo <= 12:
            y = int(m.group("y3"))
            return date(y, mo, 1), date(y, mo, 28), False
        return None, None, False
    y = int(m.group("y5"))
    return date(y, 1, 1), date(y, 12, 28), False


def role_span(dates: str, today: date) -> Tuple[Optional[date], Optional[date]]:
    """Start and end of a role from any common resume date format: "Oct 2023 -
    Present", "06/2022 - 08/2023", "2019-2021", "Summer 2024", "Mar 2015 to Feb
    2017". (None, None) when it names no date."""
    tokens = [_token_dates(m, today) for m in _DATE_TOKEN.finditer(_s(dates))]
    tokens = [t for t in tokens if t[0] is not None]
    dated = [t for t in tokens if not t[2]]
    if not dated:
        return None, None
    start = dated[0][0]
    if len(tokens) >= 2:
        end = tokens[1][1] if tokens[0] is dated[0] else tokens[-1][1]
    else:
        end = dated[0][1]
    end = min(end, today)
    if end < start:
        return None, None
    return start, end


def _months(start: date, end: date) -> int:
    return (end.year - start.year) * 12 + (end.month - start.month) + 1


def _merged_months(spans: List[Tuple[date, date]]) -> int:
    spans = sorted(spans)
    total, cur = 0, None
    for a, b in spans:
        if cur and a <= cur[1]:
            cur = (cur[0], max(cur[1], b))
        else:
            if cur:
                total += _months(*cur)
            cur = (a, b)
    return total + (_months(*cur) if cur else 0)


# ── the job side ──────────────────────────────────────────────────────────

def _job_terms(jd: Dict[str, Any]) -> Dict[str, Any]:
    """Must-have and nice-to-have skills. When the analysis names no must-haves
    the keywords stand in, but only the ones the shared skill dictionary knows:
    "fast-paced", "collaboration" and the company's own name are not skills."""
    from services.requirement_matcher import scored_term_lists
    try:
        lists = scored_term_lists(jd)
    except Exception:  # pragma: no cover - a malformed analysis scores as empty
        lists = {"required": [], "preferred": [], "keywords": []}
    company = _s(jd.get("company")).strip().lower()

    def keep(term: str) -> bool:
        low = _s(term).strip().lower()
        return bool(re.search(r"[a-z]", low)) and low not in _NOT_A_SKILL and low != company

    required = [t for t in lists.get("required") or [] if keep(t)]
    preferred = [t for t in lists.get("preferred") or [] if keep(t)]
    thin = False
    if not required:
        thin = True
        try:
            from services.fit_score import resume_item_skills
            required = [t for t in lists.get("keywords") or [] if keep(t) and resume_item_skills([t])][:12]
        except Exception:  # pragma: no cover
            required = []
    return {"required": required, "preferred": preferred, "thin": thin}


def required_years(jd: Dict[str, Any]) -> Optional[float]:
    """Years of EXPERIENCE the posting asks for, or None.

    The analysis's own field leads. Otherwise only lines that talk about work
    count ("must be 18 years of age" and "graduated within 2 years" do not);
    alternatives joined by "or" ask for the smaller figure, and across lines
    the largest stands ("5+ years overall, 2+ with AWS" asks for 5)."""
    stated = [int(m.group(1)) for m in _YEARS_FIG.finditer(_s(jd.get("experience_years")))]
    bare = re.fullmatch(r"\s*(\d{1,2})\s*\+?\s*", _s(jd.get("experience_years")))
    if not stated and bare:
        stated = [int(bare.group(1))]
    stated = [n for n in stated if 0 < n <= 25]
    if stated:
        return float(min(stated))
    found: List[int] = []
    for line in jd.get("qualifications") or []:
        if not line or _DEGREE_PREFERRED.search(line) or _YEARS_NOT_WORK.search(line):
            continue
        if not _YEARS_ABOUT_WORK.search(line):
            continue
        figs = [int(m.group(1)) for m in _YEARS_FIG.finditer(line) if 0 < int(m.group(1)) <= 25]
        if figs:
            found.append(min(figs) if re.search(r"\bor\b", line, re.I) else max(figs))
    return float(max(found)) if found else None


def required_degree(jd: Dict[str, Any]) -> Tuple[Optional[int], Optional[str], bool]:
    """(level, name, or_equivalent) of the degree the posting REQUIRES."""
    lines = list(jd.get("qualifications") or []) + list(jd.get("required_skills") or [])
    best: Optional[Tuple[int, str]] = None
    equivalent = False
    for line in lines:
        if not line or _DEGREE_PREFERRED.search(line):
            continue
        context = bool(_JD_DEGREE_CONTEXT.search(line))
        levels = [(lvl, name) for lvl, name, rx, needs in _JD_DEGREE_LEVELS
                  if rx.search(line) and (context or not needs)]
        if not levels:
            continue
        low = min(levels)  # "BS or MS", "PhD or MS with 3 years": the lower one
        if best is None or low[0] < best[0]:
            best = low
        if _DEGREE_EQUIVALENT.search(line):
            equivalent = True
    return (best[0], best[1], equivalent) if best else (None, None, False)


def _degree_level(resume: Dict[str, Any]) -> Optional[int]:
    """Highest degree level on the resume; 0 for none; None when there is a
    degree we cannot place (then nothing is capped on a guess)."""
    entries = resume["education"]
    if not entries:
        return 0
    level, unknown = 0, False
    for edu in entries:
        text = edu["degree"] or edu["institution"]
        placed = next((lvl for lvl, _, rx in _DEGREE_LEVELS if rx.search(text)), None)
        if placed is not None:
            level = max(level, placed)
        elif text and not _NO_DEGREE.search(text):
            unknown = True
    return None if (unknown and level == 0) else level


def _article(name: str) -> str:
    return ("an " if name[:1].lower() in "aeiou" else "a ") + name


# ── titles and levels ─────────────────────────────────────────────────────

def _classify(title: str):
    from services import tech_taxonomy
    try:
        return tech_taxonomy.classify(title)
    except Exception:  # pragma: no cover
        return None


def _title_fit(job_title: str, held_title: str) -> float:
    """0..1: how close a held title is to the job's, by occupation then words."""
    if not job_title or not held_title:
        return 0.0
    from services import tech_taxonomy
    job, held = _classify(job_title), _classify(held_title)
    if job is not None and held is not None and job.role_families and held.role_families:
        aligned = tech_taxonomy.role_alignment(
            job_families=job.role_families, job_specialties=job.role_specialties,
            resume_families=held.role_families, resume_specialties=held.role_specialties)
        weight = float(aligned.get("weight") or 0.0)
        if aligned.get("tier") != "unknown":
            # Two tech titles the matrix does not connect are still both tech.
            return max(weight, 0.3 if job.tech_scope == held.tech_scope == "core" else weight)
    a, b = set(_content_words(job_title)), set(_content_words(held_title))
    if not a or not b:
        return 0.4
    return min(1.0, 0.25 + len(a & b) / len(a))


def _title_fit_with_context(job_title: str, role: Dict[str, Any], summary: str) -> float:
    """Title fit, lifted when the job's own distinctive words describe this
    role's work or the summary ("6 years in FP&A" for an FP&A Manager)."""
    fit = _title_fit(job_title, role["title"])
    if fit >= 0.6 or not job_title:
        return fit
    generic = {"manager", "senior", "lead", "junior", "associate", "specialist", "engineer", "analyst",
               "director", "head", "staff", "principal", "intern", "coordinator", "officer"}
    distinct = {w for w in _content_words(job_title) if w not in generic} | {
        w for w in re.findall(r"[a-z]+&[a-z]+", job_title.lower())}
    if not distinct:
        return fit
    context = " ".join([summary] + _bullets_of(role)).lower()
    words = set(_content_words(context)) | set(re.findall(r"[a-z]+&[a-z]+", context))
    return max(fit, 0.6) if distinct & words else fit


def _seniority(title: str) -> Optional[int]:
    if _INTERN_TITLE.search(title or ""):
        return 0
    c = _classify(title)
    level = getattr(c, "seniority", None) if c is not None else None
    return _SENIORITY_RANK.get(level) if level and level != "unknown" else None


def _newest_first(roles: List[Dict[str, Any]], today: date) -> List[Dict[str, Any]]:
    spans = [role_span(r["dates"], today) for r in roles]
    if roles and all(s[1] for s in spans):
        order = sorted(range(len(roles)), key=lambda i: (spans[i][1], spans[i][0]), reverse=True)
        return [roles[i] for i in order]
    return roles


# ── evidence for each skill ──────────────────────────────────────────────

def _evidence(resume: Dict[str, Any], terms: List[str], posting: set, today: date,
              duties: Optional[List[set]] = None) -> Dict[str, Any]:
    """For every job skill, the strongest place the resume shows it, and the
    lines that cannot count (copied from the posting, or a list of skills
    written as a bullet)."""
    best: Dict[str, Dict[str, Any]] = {}
    copied: List[str] = []
    dumps: List[str] = []
    echoes: List[str] = []

    def offer(term: str, tier: str, where: str, stale: bool = False) -> None:
        cur = best.get(term)
        credit = TIER_CREDIT[tier] * (STALE_FACTOR if stale else 1.0)
        if cur is None or credit > cur["credit"]:
            best[term] = {"tier": tier, "where": where, "stale": stale, "credit": credit}

    units: List[Tuple[str, str, str, bool]] = []  # (text, kind, where, stale)
    for e in resume["experience"]:
        _, end = role_span(e["dates"], today)
        stale = bool(end and (today - end).days > STALE_YEARS * 365)
        units += [(b, "job", e["company"] or e["title"], stale) for b in _bullets_of(e)]
    for p in resume["projects"]:
        units += [(b, "project", p["name"], False) for b in _bullets_of(p)]
    for sec in resume["additional_sections"]:
        if _EVIDENCE_SECTIONS.search(sec["semantic_type"] + " " + sec["heading"]):
            for e in sec["entries"]:
                units += [(b, "project", sec["heading"], False) for b in _bullets_of(e)]

    for text, kind, where, stale in units:
        if _is_copied(text, posting):
            copied.append(text)
            continue
        hits = sorted(((pos, t) for t in terms if (pos := _locate(text, t)) is not None))
        if not hits:
            continue
        other = len(_content_words(text)) - len(hits)
        listed_run = any(sum(1 for t in terms if _match(run.group(0), t)) >= 3
                         for run in _COMMA_LIST.finditer(text))
        if len(hits) >= DUMP_TERMS or listed_run or (len(hits) >= 3 and other < 4):
            dumps.append(text)
            for _, t in hits:
                offer(t, "listed", "a list of skills in a bullet")
            continue
        echo = bool(duties) and _echoes(text, duties)
        if echo:
            echoes.append(text)
        result = bool(_METRIC.search(_strip_versions(text))) and not echo
        for rank, (_, t) in enumerate(hits):
            if kind == "job":
                tier = "result" if result and rank < RESULT_SKILLS_PER_BULLET else "used"
            else:
                tier = "project"
            offer(t, tier, where, stale)

    # A required certification or licence is shown by listing it.
    certs_blob = "\n".join(resume["certifications"])
    for t in terms:
        if _CERT_TERM.search(t) and _match(certs_blob, t):
            offer(t, "result", "certifications")

    listed_blob = "\n".join([", ".join(_skills_list(resume)), resume["summary"]]
                            + resume["certifications"] + [e["coursework"] for e in resume["education"]]
                            + [p["tech"] for p in resume["projects"]])
    for t in terms:
        if t not in best and _match(listed_blob, t):
            offer(t, "listed", "skills")
    for t in terms:
        best.setdefault(t, {"tier": "missing", "where": "", "stale": False, "credit": 0.0})
    return {"best": best, "copied": copied, "dumps": dumps, "echoes": echoes}


def skill_evidence(resume: Dict[str, Any], term: str, today: date) -> Dict[str, Any]:
    """The strongest place one skill shows (kept for callers and tests)."""
    return _evidence(_normalize_resume(resume), [term], set(), today)["best"][term]


# ── the parts of the score ───────────────────────────────────────────────

def band_of(score: int) -> Tuple[str, str]:
    for floor, key, label in BANDS:
        if score >= floor:
            return key, label
    return BANDS[-1][1], BANDS[-1][2]


def _relevant_months(roles: List[Dict[str, Any]], job_title: str, today: date) -> Tuple[float, float]:
    """(relevant months, all months), overlaps merged. Internships and roles in
    another discipline count half."""
    full, every = [], []
    for e in roles:
        start, end = role_span(e["dates"], today)
        if not start:
            continue
        every.append((start, end))
        intern = bool(_INTERN_TITLE.search(e["title"]) or e["type"].lower() in ("internship", "intern"))
        if not intern and (not job_title or _title_fit(job_title, e["title"]) >= 0.3):
            full.append((start, end))
    months_full = _merged_months(full)
    months_all = _merged_months(every)
    return months_full + 0.5 * max(0, months_all - months_full), float(months_all)


def _impact(resume: Dict[str, Any], roles: List[Dict[str, Any]]) -> Tuple[float, Dict[str, Any]]:
    bullets: List[Tuple[str, float]] = []
    for i, e in enumerate(roles):
        bullets += [(b, 2.0 if i == 0 else 1.0) for b in _bullets_of(e)]
    # Project work counts too: a student's best evidence often lives there.
    bullets += [(b, 1.0) for p in resume["projects"] for b in _bullets_of(p)]
    if not bullets:
        return 0.0, {"bullets": 0}
    total = sum(w for _, w in bullets)

    def share(pred) -> float:
        return sum(w for b, w in bullets if pred(b)) / total

    def strong_verb(b: str) -> bool:
        first = (_words(b) or [""])[0]
        return first not in _WEAK_VERBS and (first.endswith("ed") or first in _IRREGULAR_VERBS)

    with_result = share(lambda b: bool(_METRIC.search(_strip_versions(b))))
    verb = share(strong_verb)
    sized = share(lambda b: 8 <= len(b.split()) <= 35)
    filler = share(lambda b: bool(_WEAK_OPENERS.search(b) or _FIRST_PERSON.search(b)))
    points = (4.0 * min(1.0, with_result / 0.5) + 2.0 * min(1.0, verb / 0.8)
              + 2.0 * min(1.0, sized / 0.85) + 2.0 * max(0.0, 1.0 - filler / 0.25))
    return points, {"bullets": len(bullets), "with_result": round(with_result, 2),
                    "action_verbs": round(verb, 2), "well_sized": round(sized, 2),
                    "filler": round(filler, 2)}


def _readable(resume: Dict[str, Any], pdf_check: Optional[Dict[str, Any]],
              render_error: Optional[str], today: date) -> Tuple[float, List[str], Optional[Tuple[int, str]]]:
    """Points (of 8), problems a person can fix, and a cap for a fatal one."""
    problems: List[str] = []
    contact = resume["contact"]
    points = 0.0
    for key, label in (("name", "your name"), ("email", "an email address"), ("phone", "a phone number")):
        if _s(contact.get(key)).strip():
            points += 0.8
        else:
            problems.append(f"Add {label} at the top.")
    roles = resume["experience"]
    if roles:
        complete = sum(1 for r in roles if r["title"].strip() and r["company"].strip()
                       and role_span(r["dates"], today)[0])
        points += 2.4 * complete / len(roles)
        if complete < len(roles):
            problems.append("Give every job a title, a company and its dates.")
    elif resume["projects"]:
        points += 1.2
    sections = [bool(roles or resume["projects"]), bool(resume["education"]), bool(_skills_list(resume))]
    points += 1.6 * sum(sections) / 3
    if not sections[2]:
        problems.append("Add a skills section.")
    pdf_score = None
    if pdf_check and isinstance(pdf_check.get("pdf_parseable_score"), (int, float)):
        pdf_score = float(pdf_check["pdf_parseable_score"])
        points += 1.6 * max(0.0, min(100.0, pdf_score)) / 100
    else:
        points += 1.6  # No PDF to check (the original upload): judged on structure alone.
    cap = None
    if render_error:
        cap = (50, "The PDF could not be made, so an ATS could not read it.")
    elif not _s(contact.get("email")).strip() and not _s(contact.get("phone")).strip():
        cap = (50, "There is no way to contact you on the resume.")
    elif not roles and not resume["projects"]:
        cap = (50, "There is no work history or project to read.")
    elif roles and not any(role_span(r["dates"], today)[0] for r in roles):
        cap = (60, "None of your jobs has dates, so years of experience cannot be counted.")
    elif pdf_score is not None and pdf_score < 80:
        cap = (int(max(40, pdf_score)), "Parts of the PDF text could not be read back.")
    return points, problems, cap


def _unscored(reason: str) -> Dict[str, Any]:
    return {"version": ATS_SCORE_VERSION, "score": None, "band": "unavailable", "label": "Not scored",
            "reason": reason, "components": [], "deductions": [], "caps": [], "held_back_by": None,
            "fixes": [], "skills": {"proven": [], "in_projects": [], "listed_only": [], "missing": [],
                                    "nice_to_have_missing": []}}


def score(resume: Any, jd: Any, *, pdf_check: Optional[Dict[str, Any]] = None,
          render_error: Optional[str] = None, today: Optional[date] = None) -> Dict[str, Any]:
    """Score one resume against one analysed job description."""
    resume = _normalize_resume(resume)
    jd = _normalize_jd(jd)
    today = today or date.today()
    terms = _job_terms(jd)
    job_title = jd["job_title"]
    duties = jd["responsibilities"]
    if not terms["required"] and not terms["preferred"] and not duties:
        # Nothing the job asks for could be read: say so, never invent a number.
        return _unscored("We could not read what this job asks for.")

    posting = set(_shingles(" ".join(duties + jd["qualifications"]), 5))
    required_terms = terms["required"] or terms["preferred"]
    preferred_terms = terms["preferred"] if terms["required"] else []
    duty_words = [set(_content_words(d)) for d in duties]
    ev = _evidence(resume, required_terms + [t for t in preferred_terms if t not in required_terms], posting, today,
                   duties=duty_words)
    best = ev["best"]
    roles = _newest_first(resume["experience"], today)
    components: Dict[str, float] = {}
    notes: Dict[str, str] = {}
    caps: List[Dict[str, Any]] = []
    deductions: List[Dict[str, Any]] = []

    # Must-have skills.
    required = [(t, best[t]) for t in required_terms]
    mean = sum(e["credit"] for _, e in required) / len(required) if required else 0.0
    components["must_have"] = COMPONENT_MAX["must_have"] * mean
    proven = [t for t, e in required if e["tier"] in ("result", "used", "project")]
    missing = [t for t, e in required if e["tier"] == "missing"]
    listed_only = [t for t, e in required if e["tier"] == "listed"]
    with_result = [t for t, e in required if e["tier"] == "result"]
    notes["must_have"] = f"{len(proven)} of {len(required)} shown in your work"
    if terms["thin"]:
        caps.append({"key": "thin_posting", "limit": 75,
                     "reason": "This posting names few must-have skills, so the score is a rough guide."})

    # Nice-to-have skills and duties (copied lines never count).
    preferred = [(t, best[t]) for t in preferred_terms]
    pref_points = (8.0 * sum(e["credit"] for _, e in preferred) / len(preferred)) if preferred else 8.0 * mean
    copied_set = set(ev["copied"]) | set(ev["echoes"])
    bullet_words = [set(_content_words(b)) for e in roles + resume["projects"]
                    for b in _bullets_of(e) if b not in copied_set]
    covered = 0
    for duty in duties:
        want = set(_content_words(duty))
        if len(want) >= 2 and any(len(want & got) >= max(2, round(0.4 * len(want))) for got in bullet_words):
            covered += 1
    duty_share = covered / len(duties) if duties else mean
    components["nice_to_have"] = pref_points + 4.0 * min(1.0, duty_share / 0.6)
    notes["nice_to_have"] = (f"{sum(1 for _, e in preferred if e['tier'] != 'missing')} of {len(preferred)} "
                             f"nice-to-haves, {covered} of {len(duties)} duties")

    # Title and level.
    recent = roles[:2]
    fits = [_title_fit_with_context(job_title, r, resume["summary"]) for r in recent] if job_title else []
    title_fit = max(fits) if fits else (0.5 if roles else 0.3)
    job_level = _seniority(job_title)
    held_level = _seniority(roles[0]["title"]) if roles else None
    level_factor = 1.0
    if job_level is not None and held_level is not None:
        gap = abs(job_level - held_level)
        if held_level == 0 and job_level <= 1:
            gap = 0  # an intern applying to an entry role is exactly the profile
        level_factor = 1.0 if gap == 0 else 0.85 if gap == 1 else 0.65
    components["title"] = COMPONENT_MAX["title"] * title_fit * level_factor
    notes["title"] = (f"Closest title: {recent[fits.index(max(fits))]['title']}" if fits
                      else "No job title to compare")

    # Years.
    need = required_years(jd)
    relevant_months, all_months = _relevant_months(roles, job_title, today)
    have = relevant_months / 12.0
    if need:
        ratio = have / need
        components["years"] = COMPONENT_MAX["years"] * min(1.0, ratio)
        notes["years"] = f"About {have:.1f} relevant years, the job asks for {need:g}+"
        if ratio < 0.75:
            caps.append({"key": "years", "limit": 55 if ratio < 0.5 else 70,
                         "reason": f"The job asks for {need:g}+ years of experience and your resume shows about {have:.1f}."})
    else:
        components["years"] = float(COMPONENT_MAX["years"])
        notes["years"] = "The posting asks for no set number of years"

    # Education and certifications.
    degree_need, degree_name, equivalent = required_degree(jd)
    degree_have = _degree_level(resume)
    degree_met = degree_need is None or (degree_have is not None and degree_have >= degree_need)
    if degree_need is None or (degree_have is not None and degree_have >= degree_need):
        edu_points = 6.0
    elif degree_have is None:
        edu_points = 4.0  # a degree we cannot place: no penalty on a guess
    elif equivalent and have >= max(need or 0.0, 2.0):
        edu_points = 4.0
    else:
        edu_points = 0.0
        caps.append({"key": "degree", "limit": 60,
                     "reason": f"The job asks for {_article(degree_name)} degree, and your resume does not show one."})
    cert_terms = [t for t in required_terms if _CERT_TERM.search(t)]
    certs_have = [t for t in cert_terms if best[t]["tier"] != "missing"]
    cert_points = 2.0 * (len(certs_have) / len(cert_terms)) if cert_terms else 2.0
    components["education"] = edu_points + cert_points
    notes["education"] = (f"Asks for {_article(degree_name)} degree" if degree_name else "No degree named") + (
        f", {len(certs_have)} of {len(cert_terms)} certifications" if cert_terms else "")

    # Impact.
    components["impact"], impact_detail = _impact(resume, roles)
    notes["impact"] = (f"{round(100 * impact_detail.get('with_result', 0))}% of bullets show a result"
                       if impact_detail.get("bullets") else "No bullets to read")

    # Readable by an ATS.
    components["readable"], read_problems, read_cap = _readable(resume, pdf_check, render_error, today)
    if read_cap:
        caps.append({"key": "readable", "limit": read_cap[0], "reason": read_cap[1]})
    notes["readable"] = read_problems[0] if read_problems else "Clean structure"

    # First glance.
    summary = resume["summary"]
    glance = 0.0
    core_title = " ".join(_content_words(job_title)[-2:]) if job_title else ""
    if job_title and (_match(summary, job_title) or (core_title and core_title in " ".join(_content_words(summary)))):
        glance += 1.5
    glance += min(2.0, 1.0 * len([t for t in proven if _match(summary, t)]))
    if fits and fits[0] >= 0.5:
        glance += 1.5
    if 20 <= len(summary.split()) <= 80:
        glance += 1.0
    components["first_look"] = min(float(COMPONENT_MAX["first_look"]), glance)
    notes["first_look"] = ("Your summary names the job and proven skills" if glance >= 4.5
                           else "Your summary could name the job and your proven must-haves")

    # Deductions: rewards for gaming are taken back.
    repeated = []
    units = [summary] + [b for e in roles + resume["projects"] for b in _bullets_of(e)]
    for term in required_terms + preferred_terms:
        if len(term) < 2 or term in repeated:
            continue
        rx = re.compile(r"(?<![a-z0-9])" + re.escape(term.lower()) + r"(?![a-z0-9])")
        if any(len(rx.findall(u.lower())) >= 3 for u in units):
            repeated.append(term)
    listed_lower = [i.strip().lower() for i in _skills_list(resume)]
    repeated += [i for i in {x for x in listed_lower if listed_lower.count(x) > 1} if i not in repeated][:2]
    if repeated:
        deductions.append({"key": "repeated", "points": min(6, 2 * len(repeated)),
                           "reason": f"Said over and over: {', '.join(repeated[:3])}"})
    copied_lines = list(ev["copied"]) + [x for x in re.split(r"(?<=[.!?])\s+", summary) if _is_copied(x, posting)]
    if copied_lines:
        deductions.append({"key": "copied", "points": min(8, 4 * len(copied_lines)),
                           "reason": "Copied from the job posting: " + (
                               "1 line" if len(copied_lines) == 1 else f"{len(copied_lines)} lines")})
        if len(copied_lines) >= 2:
            caps.append({"key": "copied", "limit": 65,
                         "reason": "Several lines are copied word for word from the job posting."})
    if ev["dumps"]:
        deductions.append({"key": "skill_dump", "points": 3,
                           "reason": "A bullet lists skills instead of saying what you did"})
    if len(_skills_list(resume)) > 40:
        deductions.append({"key": "long_skills", "points": 2, "reason": "The skills list is over 40 items"})
    present = [t for t, e in required if e["tier"] != "missing"]
    if len(listed_only) >= 4 and len(listed_only) > 0.5 * max(1, len(present)):
        deductions.append({"key": "listed_only", "points": 3,
                           "reason": "Most must-haves are only listed, not shown in your work"})
    deducted = min(15, sum(d["points"] for d in deductions))

    raw = sum(components.values()) - deducted
    if len(required) >= 3 and len(proven) < 0.5 * len(required):
        caps.append({"key": "must_have", "limit": 65,
                     "reason": "Fewer than half of the must-have skills are shown in your work."})
    # 90 and up only when nearly everything is proven.
    years_met = not need or have >= need
    elite = bool(required) and len(proven) >= 0.9 * len(required) and len(with_result) >= 0.6 * len(required)
    if not (elite and years_met and degree_met and not deductions and not terms["thin"]):
        caps.append({"key": "top_band", "limit": 89, "reason": "90 and up needs every must-have shown with a result."})
    limit = min([c["limit"] for c in caps] or [100])
    final = int(round(max(0.0, min(float(limit), raw))))
    band_key, band_label = band_of(final)

    out_components = [{"key": k, "label": COMPONENT_LABELS[k], "points": round(components[k], 1),
                       "max": COMPONENT_MAX[k], "note": notes.get(k, "")} for k in COMPONENT_MAX]
    binding = [c for c in caps if c["limit"] == limit and limit < raw and c["key"] != "top_band"]
    has_jobs = bool(roles)
    return {
        "version": ATS_SCORE_VERSION,
        "score": final,
        "band": band_key,
        "label": band_label,
        "components": out_components,
        "deductions": deductions,
        "caps": [c for c in caps if c["key"] != "top_band" or (limit == 89 and raw > 89)],
        "held_back_by": binding[0]["reason"] if binding else None,
        "skills": {
            "proven": [t for t, e in required if e["tier"] in ("result", "used")],
            "in_projects": [t for t, e in required if e["tier"] == "project"],
            "listed_only": listed_only,
            "missing": missing,
            "nice_to_have_missing": [t for t, e in preferred if e["tier"] == "missing"],
        },
        "years": {"required": need, "relevant": round(have, 1), "total": round(all_months / 12.0, 1)},
        "degree": {"required": degree_name, "or_equivalent": equivalent, "met": degree_met},
        "fixes": _fixes(missing, listed_only, impact_detail, read_problems, caps, job_title,
                        components, deductions, has_jobs),
    }


def _fixes(missing: List[str], listed_only: List[str], impact: Dict[str, Any], read_problems: List[str],
           caps: List[Dict[str, Any]], job_title: str, components: Dict[str, float],
           deductions: List[Dict[str, Any]], has_jobs: bool) -> List[str]:
    """At most three next steps, biggest first. None of them asks anyone to
    claim something they have not done."""
    fixes: List[Tuple[float, str]] = []
    where = "at work or in a project" if has_jobs else "in a project or class"
    if listed_only:
        names = ", ".join(listed_only[:3])
        fixes.append((len(listed_only) * 2.0,
                      f"If you used {names} {where}, say where and what came of it."))
    if missing:
        names = ", ".join(missing[:3])
        fixes.append((len(missing) * 2.5, f"Not on your resume: {names}. Add only what you have really used."))
    if impact.get("bullets") and impact.get("with_result", 1) < 0.5:
        fixes.append((COMPONENT_MAX["impact"] - components.get("impact", 0) + 1,
                      "Add a number where you know it: people served, time saved, or how big the work was."))
    if read_problems:
        fixes.append((3.0, read_problems[0]))
    if job_title and components.get("first_look", 0) < 4:
        fixes.append((COMPONENT_MAX["first_look"] - components.get("first_look", 0),
                      f"Say in your summary that you are aiming for {job_title} roles, with your strongest skills."))
    for d in deductions:
        if d["key"] == "copied":
            fixes.append((d["points"] + 1, "Rewrite lines copied from the posting in your own words."))
        elif d["key"] == "repeated":
            fixes.append((d["points"], "Name each skill where you used it; once per line is enough."))
        elif d["key"] == "skill_dump":
            fixes.append((d["points"], "Turn the bullet that lists skills into what you built with them."))
    for c in caps:
        if c["key"] in ("years", "degree"):
            fixes.append((1.0, c["reason"] + " Lead with your closest experience."))
    fixes.sort(key=lambda f: -f[0])
    out: List[str] = []
    for _, text in fixes:
        if text not in out:
            out.append(text)
    return out[:3]


def compact(result: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The few fields a list row or a 'before' comparison needs."""
    if not isinstance(result, dict):
        return None
    return {k: result.get(k) for k in ("version", "score", "band", "label")}
