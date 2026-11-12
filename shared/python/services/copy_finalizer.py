"""Punctuation-only finalizer for AI-written text a person reads or sends.

Owner rule: no dash punctuation anywhere a person can read it. Em dashes and
their spaced-hyphen stand-ins read as machine-written text, and the product's
pitch is that the words are the candidate's own. Every generation path whose
output reaches a user (tailored bullets and summaries, cover letters,
application answers, outreach and referral messages, LinkedIn suggestions,
chat replies, screener notes, post drafts) ends here.

What it changes, and nothing else:
  * a dash between two clauses (em dash, spaced en dash, spaced hyphen, "--")
    becomes a comma, or a period when the next clause starts with its own
    subject ("Built X. It cut latency 40%"); a pair of them around an aside
    becomes a pair of commas; a trailing one is dropped;
  * a dash in a numeric or date range becomes " to " in prose
    ("2 to 4 years", "$120k to $150k", "2019 to 2021");
  * an en dash joining two words ("client-server") becomes a hyphen;
  * a leading dash used as a list marker becomes a bullet ("\u2022 ") in
    multi-line text, and is dropped from a single bullet.

What it never touches: words, numbers, names, URLs, e-mail addresses, code in
backticks, hyphenated words ("full-time", "H-1B"), negative numbers and minus
signs written without spaces. IntegrityGuard compares facts token by token, so
a finalizer that rewrote a word would be a fabrication path; tests in
``tests/test_copy_finalizer.py`` hold the word and number sequence fixed.

Resume ``dates`` fields are not prose and keep the resume convention of an
ASCII hyphen ("Jan 2020 - Present"); several parsers split on it
(``answer_library_service``, ``resume_access``). Use ``finalize_date_range``
for those.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Optional

# One line every generation prompt that writes user-facing prose includes.
NO_DASH_RULE = (
    "Do not use em dashes or en dashes. Join clauses with a comma or a period, "
    "and write ranges with \"to\" (\"2 to 4 years\")."
)

EM = "\u2014"
EN = "\u2013"
_FIGURE_DASHES = "\u2012\u2015\u2e3a\u2e3b"
_ANY_DASH_CHARS = EM + EN + _FIGURE_DASHES

_MONTHS = (
    "January|February|March|April|June|July|August|September|October|November|December|"
    "Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sept|Sep|Oct|Nov|Dec"
)
_MONTH = rf"(?:{_MONTHS})\.?"
_OPEN_END = r"(?:Present|Current|Now|Today|present|current|now|today)\b"

# Spans that are never edited.
_PROTECTED = re.compile(r"```[\s\S]*?```|`[^`\n]*`|https?://\S+|www\.\S+|[\w.+-]+@[\w-]+\.[\w.-]+")

# --- ranges ---------------------------------------------------------------
# En dash, "--", or a spaced hyphen between two numbers: always a range.
_NUM_RANGE = re.compile(
    r"(?P<l>\d[\d,.]*(?:\s?[kKmMbB](?![a-zA-Z]))?%?\+?)"
    r"(?:\s*[\u2013\u2012]\s*|\s*--\s*|\s+-\s+)"
    r"(?P<r>[$\u20ac\u00a3]?\d)"
)
# An UNSPACED em dash between two numbers is a range too ("2—4");
# a spaced one ("40% — 2 quarters early") is a clause break.
_NUM_RANGE_EM = re.compile(r"(?P<l>\d[\d,.]*(?:[kKmMbB](?![a-zA-Z]))?%?\+?)\u2014(?P<r>[$\u20ac\u00a3]?\d)")
# Month or year to month, year, or an open end: "Jan 2020 – Present".
_DATE_RANGE = re.compile(
    rf"(?P<l>\b(?:{_MONTH}\s+)?\d{{4}}|\b{_MONTH})"
    r"\s*(?:[\u2013\u2014\u2012]|--|(?<=\s)-(?=\s))\s*"
    rf"(?P<r>{_OPEN_END}|(?:{_MONTH})(?:\s+\d{{4}})?\b|\d{{4}}\b)"
)

# --- clause breaks --------------------------------------------------------
# Em dash (spaced or not), spaced en dash, "--" (spaced or between words),
# and a hyphen with whitespace on both sides.
_CLAUSE_DASH = re.compile(
    r"[ \t]*(?:\u2014|\u2015|\u2e3a|\u2e3b|(?<=\s)[\u2013\u2012](?=\s)|(?<=\w)--(?=\w)|(?<=\s)--(?=\s)|(?<=\s)-(?=\s))[ \t]*"
)
# Word-joining en dash: "client–server", "New York–based".
_JOIN_EN = re.compile(r"(?<=[A-Za-z])[\u2013\u2012](?=[A-Za-z])")

# A clause that starts with its own subject reads as a new sentence.
_SUBJECT_START = re.compile(
    r"(?:it|this|that|these|those|we|i|they|he|she|you|our|my|their|his|her)\b(?!['\u2019]s\b\s+(?:own))",
    re.IGNORECASE,
)

_GREETING = re.compile(r"(?:hi|hello|hey|dear|thanks|thank you|good (?:morning|afternoon|evening))\b[\w .']{0,30}$", re.IGNORECASE)

# Leading list marker on a line: "- item", "— item", "– item".
_LINE_MARKER = re.compile(r"^(?P<indent>[ \t]*)(?:[-\u2013\u2014\u2012]|--)[ \t]+(?=\S)", re.MULTILINE)


def _protect(text: str):
    """Swap protected spans for placeholders the rules cannot match."""
    saved = []

    def stash(m):
        saved.append(m.group(0))
        return f"\u0000{len(saved) - 1}\u0000"

    return _PROTECTED.sub(stash, text), saved


def _restore(text: str, saved) -> str:
    return re.sub("\u0000(\\d+)\u0000", lambda m: saved[int(m.group(1))], text)


def _ranges(text: str) -> str:
    text = _DATE_RANGE.sub(lambda m: f"{m.group('l')} to {m.group('r')}", text)
    text = _NUM_RANGE.sub(lambda m: f"{m.group('l')} to {m.group('r')}", text)
    text = _NUM_RANGE_EM.sub(lambda m: f"{m.group('l')} to {m.group('r')}", text)
    return text


def _sentence_end(text: str, start: int) -> int:
    m = re.compile(r"[.!?](?:\s|$)|\n").search(text, start)
    return m.start() if m else len(text)


def _clauses(text: str, *, label: bool = False) -> str:
    """Replace clause-break dashes, working left to right."""
    out = []
    pos = 0
    first = True
    pending_pair_end: Optional[int] = None
    for m in _CLAUSE_DASH.finditer(text):
        if m.start() < pos:
            continue
        left = text[pos:m.start()]
        right = text[m.end():]
        out.append(left)
        pos = m.end()
        before = "".join(out).rstrip()
        rest = right.lstrip()

        # Second dash of an aside: close it with a comma.
        if pending_pair_end is not None and m.start() <= pending_pair_end:
            pending_pair_end = None
            out[-1] = left.rstrip()
            out.append(", " if rest and rest[0] not in ",.;:!?)" else "")
            continue

        # Nothing before (a marker) or nothing after (a trailing dash): drop it.
        if not before or before.endswith("\n"):
            out[-1] = left.rstrip(" \t")
            continue
        if not rest or rest[0] in "\n":
            out[-1] = left.rstrip()
            continue
        # Already punctuated on either side: the dash adds nothing.
        if before[-1] in ",;:(" or rest[0] in ",.;:!?)":
            out[-1] = left.rstrip()
            out.append(" " if rest[0] not in ",.;:!?)" else "")
            continue

        out[-1] = left.rstrip()
        # A label ("Toolkit — a personal library") takes a colon.
        if label and first and len(before.split()) <= 4:
            out.append(": ")
        # Another clause dash in the same sentence: this opens an aside.
        elif _CLAUSE_DASH.search(text, m.end(), _sentence_end(text, m.end())):
            nxt = _CLAUSE_DASH.search(text, m.end(), _sentence_end(text, m.end()))
            pending_pair_end = nxt.start()
            out.append(", ")
        # A greeting ("Hi Priya — I saw ...") keeps a comma.
        elif _GREETING.match(re.split(r"[.!?\n]", before)[-1].strip()):
            out.append(", ")
        elif _SUBJECT_START.match(rest) and not before.endswith(("and", "or", "but")):
            out.append(". ")
            # Capitalise the first letter of the new sentence (the word itself
            # is unchanged, so facts and tokens survive).
            text = text[:pos] + right[: len(right) - len(rest)] + rest[0].upper() + rest[1:]
        else:
            out.append(", ")
        first = False
        # Swallow the whitespace after the dash; the separator above has its own.
        while pos < len(text) and text[pos] in " \t":
            pos += 1
    out.append(text[pos:])
    return "".join(out)


def _tidy(text: str) -> str:
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"[ \t]+([,.;:!?])", r"\1", text)
    text = re.sub(r",{2,}", ",", text)
    text = re.sub(r"\.,", ".", text)
    text = re.sub(r",\.", ".", text)
    return text


def finalize_text(text: Any, *, label: bool = False) -> Any:
    """Rewrite dash punctuation in one piece of prose. Non-strings pass through.

    ``label=True`` is for short headings and names ("Toolkit \u2014 a library"
    becomes "Toolkit: a library").
    """
    if not isinstance(text, str) or not text:
        return text
    if not any(c in text for c in _ANY_DASH_CHARS) and " -" not in text and "--" not in text and not text.lstrip().startswith("-"):
        return text
    body, saved = _protect(text)
    body = _LINE_MARKER.sub(lambda m: f"{m.group('indent')}\u2022 ", body) if "\n" in body else body
    body = _ranges(body)
    body = _JOIN_EN.sub("-", body)
    body = _clauses(body, label=label)
    # Any figure dash or stray en dash left over is a hyphen.
    body = re.sub(f"[{EN}{_FIGURE_DASHES}]", "-", body)
    body = _tidy(body)
    return _restore(body, saved)


def finalize_bullet(text: Any) -> Any:
    """A single bullet: drop a leading list marker, then finalize."""
    if not isinstance(text, str):
        return text
    stripped = re.sub(r"^[ \t]*(?:[-\u2013\u2014\u2012\u2022*]|--)[ \t]+", "", text)
    return finalize_text(stripped)


def finalize_date_range(text: Any) -> Any:
    """A resume ``dates`` field: dashes become the ASCII hyphen resumes use."""
    if not isinstance(text, str) or not text:
        return text
    new = re.sub(rf"\s+[{_ANY_DASH_CHARS}]\s+|\s+--\s+", " - ", text)
    new = re.sub(f"[{_ANY_DASH_CHARS}]", "-", new)
    return re.sub(r" {2,}", " ", new).strip()


# Keys whose values are identifiers or machine data, never prose.
_SKIP_KEYS = frozenset({
    "id", "_id", "key", "type", "kind", "status", "url", "href", "link", "email",
    "phone", "dates", "date", "start_date", "end_date", "code", "slug", "field_id",
    "job_id", "record_id", "application_id", "role", "source", "tags",
})


def finalize_fields(obj: Any, keys: Optional[Iterable[str]] = None, *, _key: str = "") -> Any:
    """Finalize prose strings inside a dict/list structure, in place where possible.

    ``keys`` limits which dict keys are prose; ``None`` means every string
    except identifier-like keys (``_SKIP_KEYS``) and ``dates``, which get the
    resume hyphen instead.
    """
    allowed = frozenset(keys) if keys is not None else None
    if isinstance(obj, str):
        if _key in ("dates",):
            return finalize_date_range(obj)
        if _key in _SKIP_KEYS:
            return obj
        if allowed is not None and _key not in allowed:
            return obj
        if _key in ("bullets", "bullet"):
            return finalize_bullet(obj)
        return finalize_text(obj, label=_key in ("name", "title", "label", "headline", "subject"))
    if isinstance(obj, list):
        for i, v in enumerate(obj):
            obj[i] = finalize_fields(v, keys, _key=_key)
        return obj
    if isinstance(obj, dict):
        for k, v in list(obj.items()):
            obj[k] = finalize_fields(v, keys, _key=str(k))
        return obj
    return obj


def has_dash_punctuation(text: Any) -> bool:
    """True when ``text`` still carries dash punctuation a person would read."""
    if not isinstance(text, str):
        return False
    body, _ = _protect(text)
    return bool(re.search(f"[{_ANY_DASH_CHARS}]|(?<=\\s)-(?=\\s)|(?<=[\\w\\s])--(?=[\\w\\s])", body))
