"""Says what each line of a job description is for. Read by `jd_profile` only.

Every line gets one role: `company`, `summary`, `duty`, `required`, `preferred`,
`benefit`, `legal`, `logistics`, `heading` or `other`. Tailoring needs this split and
no other reader makes it: responsibilities, qualifications and required-versus-preferred
used to exist only when an LLM was available.

The model never writes anything. It points at lines the posting already contains, so
it cannot invent a requirement.

Two stages, both in `jd_line_model_weights.json.gz`. A logistic regression reads a
line's own words; it learned from lines labelled by the headings above them, so 1.5M
postings became training data at no AI cost. Then small decision trees read that
evidence in context: the lines around it, the section, the position. They learned from
the blind line key. Trained by `scripts/train_jd_line_model.py`; scoring is plain
Python and makes no network call. Without the weights file the headings alone answer,
so a bad file degrades instead of failing a crawl.
"""

from __future__ import annotations

import gzip
import json
import math
import os
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from services import jd_skill_model

WEIGHTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "jd_line_model_weights.json.gz")
LINE_MODEL_VERSION = "lines-1.0.0"

#: What headings can teach. `other` is noise no heading announces, so only the blind
#: key teaches it, and only the second stage can answer it.
WORD_ROLES = ("company", "summary", "duty", "required", "preferred", "benefit", "legal",
              "logistics", "heading")
ROLES = WORD_ROLES + ("other",)

#: Past this it is a pasted handbook.
MAX_LINES = 400

# Headings that name no section: "Job Description" can introduce anything, so the
# lines under it are left to the model.
_NEUTRAL = re.compile(
    r"^(?:job description|description|job details|details|additional information|"
    r"additional job description|additional details|overview|job|position|"
    r"position details|position description|the job|role|general|other|misc.*|"
    r"information|company|employer|organization|important.*|note|notes|please note|"
    r"why .*|what's next|next steps|job posting)$")

# Order matters: "Preferred Qualifications" must reach `preferred` before `required`
# sees "qualifications", and "Travel Requirements" is logistics, not a requirement.
_HEADINGS: Sequence[Tuple[str, "re.Pattern[str]"]] = tuple(
    (role, re.compile(pattern)) for role, pattern in (
        ("legal", r"equal (?:employment )?opportunit|^eeo|\beeoc?\b|e-verify|accommodation|"
                  r"disclaimer|privacy|drug|non-?discrimination|americans with disabilities|"
                  r"fraud|know your rights|at-will|background (?:check|screen)|"
                  r"applicants with disabilities|\bada\b|ai usage policy|"
                  r"recruitment and hiring requirements|fmla|poster"),
        ("logistics", r"location|\bshift\b|schedule|\bhours\b|travel|department|time type|"
                      r"job (?:family|category|type|code|level|function|id|sub function)|"
                      r"position type|employment type|worker type|employee (?:type|status)|"
                      r"date posted|posting (?:date|end|statement|location)|requisition|"
                      r"remote type|work model|how to apply|deadline|closing date|division|"
                      r"\bgrade\b|management level|cost center|relocation|\baddress\b|"
                      r"country|work (?:city|state)|career area|line of service|specialism|"
                      r"flsa|^status$|^salaried$|^hourly$|apply now"),
        ("benefit", r"benefit|perks|compensation|salary|pay (?:range|type|details)|^pay$|"
                    r"what we (?:can )?offer|what's in it for you|what you(?:'ll| will) get|"
                    r"rewards|tuition|paid time off|insurance|401\(?k|retirement|"
                    r"employee assistance|vacation|parental leave|stock purchase|\bhsa\b"),
        ("preferred", r"preferred|prefer\b|nice[- ]to[- ]have|nice if|good[- ]to[- ]have|"
                      r"bonus|desired|\ba plus\b|optional skills|desirable|"
                      r"what sets you apart|stand out|extra credit|would be great|we'd love"),
        ("required", r"required|requirements?\b|qualifications?|must[- ]have|^minimum|"
                     r"what you(?:'ll| will)? (?:need|bring)|what you bring|"
                     r"what we(?:'re| are) looking for|who you are|about you|you have|^skills|"
                     r"skills\b|competenc|knowledge|education|experience|certifications?|"
                     r"licen[sc]e|must be able to|abilities"),
        ("duty", r"responsibilit|duties|accountabilit|"
                 r"what you(?:'ll| will) (?:do|be doing|work on)|"
                 r"essential (?:functions|job functions)|day in the life|day[- ]to[- ]day|"
                 r"in this role,? you will|your impact|the work you|key tasks|core functions|"
                 r"significant work activities|a caregiver who excels|principal|"
                 r"what you can expect"),
        ("summary", r"summary|position overview|job overview|role overview|"
                    r"about (?:the|this) (?:role|job|position|opportunity)|^the role$|"
                    r"^your role$|the opportunity|job purpose|position purpose|"
                    r"general function|purpose and scope|position highlights"),
        ("company", r"^about |who we are|company (?:overview|description|profile)|"
                    r"our (?:values|mission|purpose|story|commitment|culture|company)|"
                    r"^the team$|meet the team|working at|how we work|all about"),
    ))

_OPENS_A_SECTION = frozenset({"duty", "required", "preferred"})

_SECTION_WORDS = re.compile(
    r"^(?:(?:knowledge|skills?|abilities|qualifications?|education|experience|requirements?|"
    r"capabilities|competenc(?:y|ies)|certifications?|licen[sc]es?|licensure|training|duties|"
    r"responsibilities|benefits|compensation|and|or|&|and/or)[\s,]*)+:?$", re.I)

#: A line under a required heading that says this is still only a preference.
PREFERENCE = re.compile(r"\b(?:preferred|nice to have|a plus|bonus|desirable|ideally|"
                        r"would be great|preference)\b", re.I)

_WORD = re.compile(r"[a-z0-9+#.'/-]*[a-z0-9+#]|[$%]")
_DIGITS = re.compile(r"\d+")


def lines_of(raw: Any) -> List[str]:
    """The lines of a posting, cut the way every model here cuts them."""
    found = (line.strip() for line in jd_skill_model._lines(jd_skill_model.clean(raw)))
    return [line for line in found if line][:MAX_LINES]


def _heading_key(line: str) -> str:
    return re.sub(r"\s+", " ", line.lower()).strip(" -*•:\t")


def looks_like_heading(line: str) -> bool:
    """Short, and either ends in a colon or capitalises every word that matters."""
    text = line.strip()
    if not 2 < len(text) <= 70 or text[-1] in ".;!?,":
        return False
    # "Work Schedule: Monday-Friday" is one fact under a label. Reading it as a
    # heading turned the lines after it into logistics.
    if ":" in text[:-1]:
        return False
    # "Experience with Python, SQL, and Spark" is the best line in a posting, and
    # "Knowledge, Skills and Abilities" is a heading. Only the second is all section words.
    if "," in text and not _SECTION_WORDS.match(text):
        return False
    words = text.split()
    if len(words) > 9:
        return False
    if text.endswith(":"):
        return True
    alpha = [w for w in words if w[0].isalpha()]
    # "About Us" and "What You Will Do" are nothing but stop words, so when no other
    # word is left the stop words have to count.
    kept = [w for w in alpha if w.lower() not in jd_skill_model._STOP] or alpha
    return bool(kept) and all(w[0].isupper() for w in kept)


def heading_role(line: str) -> Optional[str]:
    """The section a heading opens, `neutral` if it names none, None if unknown."""
    key = _heading_key(line)
    if _NEUTRAL.match(key):
        return "neutral"
    for role, pattern in _HEADINGS:
        if pattern.search(key):
            return role
    return None


def sections(lines: Sequence[str]) -> List[Tuple[Optional[str], bool]]:
    """`(section, is_heading)` per line, from headings alone."""
    out: List[Tuple[Optional[str], bool]] = []
    section: Optional[str] = None
    for line in lines:
        is_heading = False
        here = section
        if looks_like_heading(line):
            role = heading_role(line)
            if role == "neutral":
                section, is_heading = None, True
            elif role:
                section, is_heading = role, True
            elif line.strip().endswith(":"):
                # An unknown labelled section: what follows is not the old section.
                section, is_heading = None, True
            here = section
        else:
            inline = jd_skill_model._INLINE_HEADING.match(line)
            if inline:
                role = heading_role(inline.group(1))
                if role and role != "neutral":
                    here = role
                    # "Qualifications: ..." opens a list. "Location: Austin" does not.
                    if role in _OPENS_A_SECTION:
                        section = role
        out.append((here, is_heading))
    return out


def silver_labels(lines: Sequence[str]) -> List[Optional[str]]:
    """Training labels from headings. None where no heading vouches for the line."""
    out: List[Optional[str]] = []
    for line, (section, is_heading) in zip(lines, sections(lines)):
        if is_heading:
            out.append("heading")
        elif section == "required" and PREFERENCE.search(line):
            out.append("preferred")
        else:
            out.append(section)
    return out


def features(line: str) -> List[str]:
    """Words, word pairs, the opening words (the strongest cue) and a little shape."""
    words = _WORD.findall(_DIGITS.sub("0", line.lower()))
    out = list(words)
    out.extend(a + "_" + b for a, b in zip(words, words[1:]))
    for size in (1, 2, 3):
        if len(words) >= size:
            out.append("S%d=%s" % (size, "_".join(words[:size])))
    count = len(words)
    out.append("L=%d" % sum(count > step for step in (2, 5, 12, 30)))
    text = line.rstrip()
    if text.endswith(":"):
        out.append("COLON")
    if text.endswith("."):
        out.append("DOT")
    if text[:1].isupper():
        out.append("CAP")
    if "$" in text:
        out.append("MONEY")
    return out


_model: Optional[Dict[str, Any]] = None


def _load() -> Dict[str, Any]:
    global _model
    if _model is None:
        with gzip.open(WEIGHTS_FILE, "rt", encoding="utf-8") as handle:
            _model = json.load(handle)
    return _model


def word_scores(model: Dict[str, Any], line: str) -> List[float]:
    """Each `WORD_ROLES` role's log probability from the line's own words."""
    found = features(line)
    weights = model["weights"]
    size = len(WORD_ROLES)
    totals = [0.0] * size
    for feature in found:
        row = weights.get(feature)
        if row:
            for i in range(size):
                totals[i] += row[i]
    scale = 1.0 / math.sqrt(max(1, len(found)))
    bias = model["bias"]
    return [-math.log1p(math.exp(-max(min(bias[i] + totals[i] * scale, 30.0), -30.0)))
            for i in range(size)]


_SECTION_CODE = {None: 0.0, **{role: float(i + 1) for i, role in enumerate(WORD_ROLES)}}
_NOTHING = [0.0] * len(WORD_ROLES)


def context_rows(lines: Sequence[str], scores: Sequence[Sequence[float]]) -> List[List[float]]:
    """What the second stage sees for each line: the evidence around it and where it sits."""
    found = sections(lines)
    count = len(lines)
    bullets = [bool(jd_skill_model._BULLET.match(line)) for line in lines]
    rows: List[List[float]] = []
    since = 0
    for i, line in enumerate(lines):
        section, is_heading = found[i]
        since = 0 if is_heading else since + 1
        words = line.split()
        text = line.rstrip()
        before = scores[i - 1] if i else _NOTHING
        after = scores[i + 1] if i + 1 < count else _NOTHING
        far_before = scores[i - 2] if i > 1 else _NOTHING
        far_after = scores[i + 2] if i + 2 < count else _NOTHING
        rows.append([
            *scores[i], *before, *after,
            *((a + b) / 2 for a, b in zip(far_before, far_after)),
            _SECTION_CODE[section], float(is_heading), i / max(1, count), (count - i) / 50.0,
            min(count, 120) / 120.0, float(len(words)), float(bullets[i]),
            float(text.endswith(".")), float(text.endswith(":")), float(line[:1].isupper()),
            float(":" in line), float(line.isupper()), float(bool(PREFERENCE.search(line))),
            float(min(since, 30)), float(i > 0 and found[i - 1][1]),
            float(i + 1 < count and found[i + 1][1]), float(i > 0 and bullets[i - 1]),
            float(i + 1 < count and bullets[i + 1]),
        ])
    return rows


def _tree_value(tree: Sequence[Sequence[float]], row: Sequence[float]) -> float:
    """One decision tree. Each node is `[feature, threshold, left, right, value]`."""
    node = tree[0]
    while node[0] >= 0:
        node = tree[int(node[2] if row[int(node[0])] <= node[1] else node[3])]
    return node[4]


def _from_headings(lines: Sequence[str]) -> List[Dict[str, Any]]:
    """Without weights the headings answer, and an unvouched line says so."""
    return [{"text": line, "role": role, "confidence": 0.0}
            for line, role in zip(lines, silver_labels(lines))]


def read_lines(raw: Any) -> List[Dict[str, Any]]:
    """`{"text", "role", "confidence"}` for every line of a posting, in order.

    `role` is one of `ROLES`, or None when there is no model and no heading.
    """
    lines = lines_of(raw)
    if not lines:
        return []
    try:
        model = _load()
    except (OSError, ValueError, KeyError, EOFError):
        return _from_headings(lines)
    rows = context_rows(lines, [word_scores(model, line) for line in lines])
    baseline, trees = model["baseline"], model["trees"]
    out: List[Dict[str, Any]] = []
    for line, row in zip(lines, rows):
        logits = [baseline[k] + sum(_tree_value(tree, row) for tree in trees[k])
                  for k in range(len(ROLES))]
        top = max(logits)
        shares = [math.exp(value - top) for value in logits]
        best = logits.index(top)
        out.append({"text": line, "role": ROLES[best],
                    "confidence": round(shares[best] / sum(shares), 3)})
    return out
