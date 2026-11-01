"""Finds the skills a job description asks for. Read by `jd_profile` only.

A word list cannot do this: the answer key's misses are 3,803 different terms and
57% of them appear once, and adding every term seen twice halves precision. So
this reads every short phrase in the posting and scores it on how it is written
and where it sits, which carries over to skills it has never seen.

Two stages, both in `jd_skill_model_weights.json.gz`: a logistic regression drops
the obvious non-skills, then boosted decision trees judge the rest. Trained by
`scripts/train_jd_skill_model.py`; scoring is plain Python, makes no network
call and needs no model server. Training imports `mentions` and `signals` from
here, so it cannot drift from what production reads.

The model reads the JD itself and never a resume, and it does not feed
`fit_score`: match scores still use `SKILL_LEXICON`.
"""

from __future__ import annotations

import gzip
import html
import json
import math
import os
import re
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

WEIGHTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "jd_skill_model_weights.json.gz")

MAX_WORDS = 4
MAX_SKILLS = 30
#: Long enough for any real posting. Past this it is a pasted handbook.
MAX_CHARS = 20_000

_TAG = re.compile(r"<[^>]{0,400}>")
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9+#./&'’-]*[A-Za-z0-9+#]|[A-Za-z0-9]")
_BULLET = re.compile(r"^\s*(?:[-*•·▪◦–—]|\d{1,2}[.)])\s+")
_FOLD = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"', "–": "-", "—": "-",
                       " ": " "})

_STOP = frozenset("""
a about above across after all also am an and any are as at be because been being
both but by can could did do does each either etc for from had has have having he
her here his how i if in including into is it its just may me more most must my
no nor not of on one only or other our out over own per please she should so some
such than that the their them then there these they this those through to too
under until up us very was we were well what when where which while who will
with within without would you your
""".split())

# A skill can be wrapped in these but is never one of them: "customer service
# skills", "experience in Python". A phrase may not start or end on one, so the
# candidate is the skill itself.
_WRAPPER = frozenset("""
ability abilities able advanced basic capabilities competency competencies demonstrated
desired excellent experience experienced expert expertise familiar familiarity
fluent good great hands-on knowledge knowledgeable preferred prior proficiency
proficient proven related relevant required skill skilled skills solid strong
strongly understanding various work working year years
""".split())

_SECTIONS: Sequence[Tuple[str, "re.Pattern[str]"]] = tuple(
    (name, re.compile(pattern, re.I)) for name, pattern in (
        ("legal", r"equal (?:employment )?opportunity|eeo\b|accommodation|disclaimer|"
                  r"privacy|applicant|e-verify|pay transparency"),
        ("benefits", r"benefits?\b|perks|compensation|what we offer|why (?:join|work)|"
                     r"salary|pay range"),
        ("preferred", r"preferred|nice to have|bonus|desired|plus\b|ideal"),
        ("required", r"requirements?|qualifications?|skills|must have|what you(?:'ll)? "
                     r"(?:need|bring)|experience|competenc|knowledge|about you|education|"
                     r"who you are|you have|technolog|tech stack"),
        ("duties", r"responsibilit|duties|what you(?:'ll)? do|role|the job|day to day|"
                   r"essential functions|you will"),
        ("about", r"about (?:us|the company|[a-z]+)|who we are|overview|our (?:company|"
                  r"mission|story)|company description"),
    ))

_INLINE_HEADING = re.compile(r"^\s*([A-Z][^:.,]{2,48}):\s+(\S.*)$")

_CUE_BEFORE = frozenset({"with", "in", "of", "using", "and", "or", "like", "as", "including"})


def clean(raw: Any) -> str:
    """Tags out and entities decoded. Case stays: it is half the evidence."""
    text = str(raw or "")
    if "&" in text:
        text = html.unescape(text)
    if "<" in text and ">" in text:
        text = _TAG.sub("\n", text)
    return text.translate(_FOLD)[:MAX_CHARS]


def _lines(text: str) -> List[str]:
    lines: List[str] = []
    for line in text.split("\n"):
        # Boards that flatten a posting leave it as one long line.
        lines.extend(p for p in re.split(r"(?<=[.;!?])\s+(?=[A-Z•])|\s+•\s+", line) if p.strip())
    return lines


def _section_of(line: str) -> Optional[str]:
    """The section a heading opens, or None. It has to LOOK like a heading: a
    short bullet saying "Experience with Python" is the best line in the posting."""
    text = line.strip()
    words = text.split()
    if not 0 < len(words) <= 7 or _BULLET.match(line) or "," in text or text[-1] in ".;!?":
        return None
    if ":" in text[:-1]:  # "Qualifications: Python and SQL" is content under a label
        return None
    if not text.endswith(":"):
        if len(words) > 5:
            return None
        capitals = sum(w[0].isupper() for w in words if w.lower() not in _STOP)
        if capitals < max(1, len([w for w in words if w.lower() not in _STOP])):
            return None
    for name, pattern in _SECTIONS:
        if pattern.search(text):
            return name
    return None


def _shape(words: Sequence[str]) -> str:
    joined = "".join(words)
    if re.search(r"[+#./&]", joined):
        return "symbol"
    if re.search(r"\d", joined):
        return "digit"
    if all(w.isupper() and len(w) > 1 for w in words):
        return "caps"
    if any(w[1:] != w[1:].lower() for w in words):
        return "mixed"
    if all(w[0].isupper() for w in words):
        return "title"
    if words[0][0].isupper():
        return "first"
    return "lower"


def _bucket(n: int, steps: Sequence[int]) -> int:
    return sum(n >= s for s in steps)


def mentions(raw: Any, known: Any = ()) -> Iterator[Tuple[str, List[str]]]:
    """Every candidate phrase in a posting as `(phrase, features)`, one per mention."""
    text = clean(raw)
    known = frozenset(known)
    rows: List[Tuple[str, bool, bool, List[Tuple[int, int, str]], str]] = []
    counts: Dict[str, int] = {}
    section = "intro"
    for line in _lines(text):
        heading = _section_of(line)
        if heading:
            section = heading
            continue
        inline = _INLINE_HEADING.match(line)
        if inline:
            named = _section_of(inline.group(1) + ":")
            if named:
                section, line = named, inline.group(2)
        tokens = [(m.start(), m.end(), m.group(0)) for m in _TOKEN.finditer(line)]
        if not tokens:
            continue
        rows.append((section, bool(_BULLET.match(line)), len(tokens) <= 14, tokens, line))
        lowered = [t[2].lower() for t in tokens]
        for i in range(len(lowered)):
            for j in range(i + 1, min(i + MAX_WORDS, len(lowered)) + 1):
                phrase = " ".join(lowered[i:j])
                counts[phrase] = counts.get(phrase, 0) + 1

    for section, bullet, short, tokens, line in rows:
        lowered = [t[2].lower() for t in tokens]
        for i in range(len(tokens)):
            if lowered[i] in _STOP or lowered[i] in _WRAPPER:
                continue
            for j in range(i + 1, min(i + MAX_WORDS, len(tokens)) + 1):
                end = lowered[j - 1]
                if end in _STOP or end in _WRAPPER or end.isdigit():
                    continue
                inner = line[tokens[i][1]:tokens[j - 1][0]]
                if j - i > 1 and re.search(r"[,;:()|]", inner):
                    break
                if "and" in lowered[i:j] or "or" in lowered[i:j]:
                    break
                originals = [t[2] for t in tokens[i:j]]
                phrase = " ".join(lowered[i:j])
                before = lowered[i - 1] if i else "^"
                after = lowered[j] if j < len(tokens) else "$"
                gap_before = line[tokens[i - 1][1]:tokens[i][0]] if i else ""
                gap_after = line[tokens[j - 1][1]:tokens[j][0]] if j < len(tokens) else ""
                listed = "," in gap_before or "," in gap_after or "/" in gap_after
                shape = _shape(originals)
                size = j - i
                features = [
                    "p=" + phrase, "first=" + lowered[i], "last=" + end,
                    "suffix=" + end[-3:], "n=%d" % size, "shape=" + shape,
                    "sec=" + section, "sec_shape=%s|%s" % (section, shape),
                    "sec_n=%s|%d" % (section, size),
                    "before=" + before, "after=" + after,
                    "before2=%s %s" % (lowered[i - 2] if i > 1 else "^", before),
                    "tf=%d" % _bucket(counts.get(phrase, 1), (2, 3, 6)),
                ]
                features.extend("w=" + w for w in lowered[i:j])
                if phrase in known:
                    features.append("known")
                if bullet:
                    features.append("bullet")
                if listed:
                    features.extend(("listed", "listed_shape=" + shape))
                if short:
                    features.append("short_line")
                if i == 0:
                    features.extend(("starts_line", "starts_line_shape=" + shape))
                if before in _CUE_BEFORE:
                    features.append("cue_before")
                yield phrase, features


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

_SECTION_CODES = ("required", "preferred", "duties", "about", "benefits", "legal", "intro")
_SHAPE_CODES = ("lower", "first", "title", "caps", "mixed", "digit", "symbol")


def signals(phrase: str, features: Sequence[str], stats: Dict[str, Sequence[int]],
            first_pass: float, in_lexicon: bool) -> List[float]:
    """What the second stage sees. `stats` is how a phrase behaves across postings."""
    row = stats.get(phrase)
    seen = int(row[0]) if row else 0
    base = max(1, seen)
    share = [int(v) / base for v in row[1:]] if row else [0.0] * 6
    words = phrase.split()
    cohesion = 1.0
    if len(words) > 1:
        parts = [int(stats[w][0]) for w in words if w in stats]
        cohesion = seen / max(1, min(parts or [base]))
    section, shape, frequency = "intro", "lower", 0
    flags = {"bullet": 0.0, "listed": 0.0, "short_line": 0.0, "starts_line": 0.0,
             "cue_before": 0.0, "known": 0.0}
    for feature in features:
        if feature.startswith("sec="):
            section = feature[4:]
        elif feature.startswith("shape="):
            shape = feature[6:]
        elif feature.startswith("tf="):
            frequency = int(feature[3:])
        elif feature in flags:
            flags[feature] = 1.0
    return [first_pass, math.log1p(seen), *share, cohesion, float(len(words)),
            float(frequency), float(_SECTION_CODES.index(section)),
            float(_SHAPE_CODES.index(shape)), flags["bullet"], flags["listed"],
            flags["short_line"], flags["starts_line"], flags["cue_before"], flags["known"],
            1.0 if in_lexicon else 0.0]


def _tree_value(tree: Sequence[Sequence[float]], row: Sequence[float]) -> float:
    """One decision tree. Each node is `[feature, threshold, left, right, value]`."""
    node = tree[0]
    while node[0] >= 0:
        node = tree[int(node[2] if row[int(node[0])] <= node[1] else node[3])]
    return node[4]


_model: Optional[Dict[str, Any]] = None


def _load() -> Dict[str, Any]:
    global _model
    if _model is None:
        with gzip.open(WEIGHTS_FILE, "rt", encoding="utf-8") as handle:
            _model = json.load(handle)
    return _model


def _lexicon_hit(phrase: str) -> bool:
    from services import fit_score

    return bool(fit_score.find_skills(phrase))


def score(raw: Any, known: Any = ()) -> Dict[str, float]:
    """Each candidate phrase's probability of being a skill, its best mention's."""
    model = _load()
    weights = model["weights"]
    bias = float(model["bias"])
    found = [(p, f, bias + sum(weights.get(x, 0.0) for x in f)) for p, f in mentions(raw, known)]
    if not found:
        return {}
    # The first stage is cheap and the trees are not: only the likeliest go on.
    found.sort(key=lambda item: -item[2])
    found = found[:max(1, int(len(found) * float(model["keep_share"])))]
    stats, trees = model["stats"], model["trees"]
    base = float(model["baseline"])
    rate = float(model["negative_rate"])
    best: Dict[str, float] = {}
    for phrase, features, first_pass in found:
        row = signals(phrase, features, stats, first_pass, _lexicon_hit(phrase))
        logit = base + sum(_tree_value(tree, row) for tree in trees)
        if logit > best.get(phrase, -math.inf):
            best[phrase] = logit
    out: Dict[str, float] = {}
    for phrase, logit in best.items():
        chance = 1.0 / (1.0 + math.exp(-max(min(logit, 30.0), -30.0)))
        # Negatives were sampled in training, which inflates every probability.
        out[phrase] = chance / (chance + (1.0 - chance) / rate)
    return out


def pick(scores: Dict[str, float], threshold: float, limit: int = MAX_SKILLS) -> List[str]:
    """Phrases over the threshold, best first, skipping one a better phrase contains."""
    chosen: List[str] = []
    taken: List[frozenset] = []
    for phrase in sorted(scores, key=lambda p: (-scores[p], p)):
        if scores[phrase] < threshold or len(chosen) >= limit:
            break
        words = frozenset(phrase.split())
        if any(words <= other or other <= words for other in taken):
            continue
        chosen.append(phrase)
        taken.append(words)
    return chosen


def read_skills(raw: Any, known: Any = ()) -> List[str]:
    """The skills this posting asks for, best first. `[]` if there is no model."""
    try:
        return pick(score(raw, known), float(_load()["threshold"]))
    except (OSError, ValueError, KeyError):
        return []
