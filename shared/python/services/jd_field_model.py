"""Second opinion on the fields rules get wrong most: years and work mode.

The rules miss "Full-time onsite required" and "5+ Years of Commercial Banking",
and every new wording means a new regex. These two small models read the same
sentences a reader would and take the rule's answer as one input, so they keep
what the rules do well. Sponsorship is not here: on 138 "no sponsorship" postings
a model found nothing the rules had missed.

Trained by `scripts/train_jd_field_model.py` on the answer key plus postings where
the rules and an earlier model disagreed. Scoring is plain Python, makes no network
call, and returns the rule's answer if the weights cannot be read, so a bad file
degrades to the old behaviour instead of failing a crawl.
"""

from __future__ import annotations

import gzip
import json
import math
import os
import re
from collections import Counter
from typing import Any, Dict, List, Optional, Sequence, Tuple

from services import jd_skill_model

WEIGHTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "jd_field_model_weights.json.gz")

MODES = ("hybrid", "onsite", "remote", "unknown")
MAX_SNIPPETS = 40

_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
          "eight": 8, "nine": 9, "ten": 10, "twelve": 12, "fifteen": 15}
_NUMBER = r"(\d{1,2}|" + "|".join(_WORDS) + r")"
_YEARS = re.compile(
    r"(?:\(\s*\d{1,2}\s*\)\s*)?" + _NUMBER + r"(?:\s*\(\s*\d{1,2}\s*\))?\s*(\+|plus|or more)?\s*"
    r"(?:(?:-|to|through)\s*" + _NUMBER + r"\s*\+?\s*)?(?:\+\s*)?"
    r"(?:full[- ]time |relevant |professional |progressive |related |total |additional |plus )*"
    r"(?:years?|yrs?)\b", re.I)
_MONTHS = re.compile(_NUMBER + r"\s*\+?\s*months?\b", re.I)
_TOKEN = re.compile(r"[a-z0-9+#']+")
_LINE_CUES = ("experience", "preferred", "required", "minimum", "at least", "bachelor",
              "master", "phd", "degree", "nice to have", "plus", "ideally", "company",
              "founded", "over", "benefit", "tenure", "vest", "must")
_MODE_WORDS = re.compile(
    r"remote|hybrid|on-?\s?site|in[- ]office|in[- ]person|work(?:ing)? from home|wfh|telecommut|"
    r"office|days? (?:a|per) week|flexible work|virtual|distributed|commut|relocat|"
    r"field[- ]based|travel", re.I)
_NGRAM_WORD = re.compile(r"\b\w\w+\b")


def _sections(raw: Any) -> List[Tuple[str, str]]:
    """`(section, line)` for every line. A heading stays in: "5+ Years Experience"
    looks like one and is often the only place a posting states the number."""
    section = "intro"
    out: List[Tuple[str, str]] = []
    for line in jd_skill_model._lines(jd_skill_model.clean(raw)):
        section = jd_skill_model._section_of(line) or section
        out.append((section, line))
    return out


def _number(text: str) -> Optional[int]:
    text = text.lower()
    return _WORDS.get(text, int(text) if text.isdigit() else None)


def year_mentions(raw: Any) -> List[Tuple[float, List[str]]]:
    """Every number of years or months in a posting, with what surrounds it."""
    lines = _sections(raw)
    found: List[Tuple[float, List[str]]] = []
    position = 0
    for section, line in lines:
        lowered = line.lower()
        for m in _YEARS.finditer(line):
            value = _number(m.group(1))
            if value is None or value > 40:
                continue
            before = _TOKEN.findall(line[max(0, m.start() - 70):m.start()].lower())[-7:]
            after = _TOKEN.findall(line[m.end():m.end() + 90].lower())[:9]
            features = ["sec=" + section, "v=%d" % min(value, 15),
                        "range=%d" % bool(m.group(3)), "plus=%d" % bool(m.group(2))]
            features += ["b%d=%s" % (len(before) - i, w) for i, w in enumerate(before)][-3:]
            features += ["a%d=%s" % (i, w) for i, w in enumerate(after)][:4]
            features += ["bw=" + w for w in before] + ["aw=" + w for w in after]
            features.append("line_words=%d" % min(len(line.split()) // 4, 5))
            features += ["has=" + cue for cue in _LINE_CUES if cue in lowered]
            features.append("ord=%d" % min(position, 3))
            position += 1
            found.append((float(value), features))
    for section, line in lines:
        for m in _MONTHS.finditer(line):
            value = _number(m.group(1))
            if value is None or value >= 24:
                continue
            before = _TOKEN.findall(line[max(0, m.start() - 70):m.start()].lower())[-7:]
            after = _TOKEN.findall(line[m.end():m.end() + 90].lower())[:9]
            found.append((round(value / 12, 2),
                          ["months=1", "sec=" + section] + ["bw=" + w for w in before]
                          + ["aw=" + w for w in after]))
    return found


def year_features(mention: Tuple[float, Sequence[str]], rule: Any) -> Counter:
    same = rule is not None and float(rule) == mention[0]
    return Counter([*mention[1], "rule_same=%d" % same, "rule_none=%d" % (rule is None)])


def mode_text(raw: Any) -> str:
    """The stretches of a posting that could say where the work happens."""
    pieces: List[str] = []
    for _, line in _sections(raw):
        for m in _MODE_WORDS.finditer(line):
            pieces.append(line[max(0, m.start() - 110):m.end() + 110])
    return " || ".join(pieces[:MAX_SNIPPETS])


def mode_grams(text: str) -> Counter:
    words = _NGRAM_WORD.findall(text.lower())
    grams: List[str] = list(words)
    for size in (2, 3):
        grams.extend(" ".join(words[i:i + size]) for i in range(len(words) - size + 1))
    return Counter(grams)


_model: Optional[Dict[str, Any]] = None


def _load() -> Dict[str, Any]:
    global _model
    if _model is None:
        with gzip.open(WEIGHTS_FILE, "rt", encoding="utf-8") as handle:
            _model = json.load(handle)
    return _model


def score_years(raw: Any, rule: Any) -> List[Tuple[float, float]]:
    """`(value, probability)` for each mention."""
    model = _load()["years"]
    weights, bias = model["weights"], float(model["bias"])
    out: List[Tuple[float, float]] = []
    for mention in year_mentions(raw):
        total = bias + sum(weights.get(f, 0.0) * n
                           for f, n in year_features(mention, rule).items())
        out.append((mention[0], 1.0 / (1.0 + math.exp(-max(min(total, 30.0), -30.0)))))
    return out


def check_years(raw: Any, rule: Any) -> Any:
    """The minimum years this posting states. Only a confident reading replaces the
    rule's, and it never erases one: taking a number away broke more than it fixed."""
    try:
        threshold = float(_load()["years"]["threshold"])
        scored = score_years(raw, rule)
    except (OSError, ValueError, KeyError):
        return rule
    if not scored:
        return rule
    # The first of equals, which is the earliest mention.
    value, chance = max(scored, key=lambda item: item[1])
    if chance < threshold:
        return rule
    return int(value) if value == int(value) else value


def score_mode(raw: Any, rule: str) -> Dict[str, float]:
    """A raw score for each mode, highest wins."""
    model = _load()["mode"]
    vocabulary, idf = model["vocabulary"], model["idf"]
    row: Dict[int, float] = {}
    for gram, count in mode_grams(mode_text(raw)).items():
        at = vocabulary.get(gram)
        if at is not None:
            row[at] = (1.0 + math.log(count)) * float(idf[at])
    norm = math.sqrt(sum(v * v for v in row.values())) or 1.0
    scores: Dict[str, float] = {}
    for mode, weights, bias, rule_weights in zip(MODES, model["weights"], model["bias"],
                                                 model["rule_weights"]):
        total = float(bias) + sum(float(weights[i]) * v / norm for i, v in row.items())
        if rule in MODES:
            total += float(rule_weights[MODES.index(rule)]) * 3.0
        scores[mode] = total
    return scores


def check_work_mode(raw: Any, rule: str) -> str:
    """`remote`, `hybrid`, `onsite` or `unknown`. `rule` on any failure."""
    try:
        scores = score_mode(raw, rule)
    except (OSError, ValueError, KeyError, IndexError):
        return rule
    return max(MODES, key=lambda mode: scores[mode])
