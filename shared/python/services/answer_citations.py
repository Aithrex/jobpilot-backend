"""Cited application answers: client-supplied facts in, `used_fact_ids` out.

The ApplyPilot extension never submits a drafted answer it cannot verify: every
number, employer, title, date, technology or credential in the draft must be
traceable to a FACT it cited (the extension's own integrity check, run on the
answer we return). For that, the model has to say which facts each answer used,
and the ids have to be the extension's ids.

So the extension may send the facts it built from the user's profile:

    facts: [{"id": "exp[0].title", "kind": "experience", "text": "SWE at Acme (2022-2024)"}, ...]

and every returned variant carries `used_fact_ids`. Before a variant leaves the
server it must pass `check_variant`:

  * every cited id is one the client sent (an invented id is a fabricated
    source, so the whole variant is dropped);
  * it cites at least one fact about the USER (the `jd.*` facts only name the
    company and role and can ground no claim about the candidate);
  * every number in the answer appears in the text of a fact it cited
    (the backend's half of the never-fabricate rule; the extension re-checks
    names, dates, technologies and credentials too).

A dropped variant is logged with its reason and never returned. This module is
pure: no database, no model call.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

#: Bounds on what a client may send. Generous for a real profile (a long résumé
#: is ~60 facts), small enough that a request stays cheap to validate.
MAX_FACTS = 150
MAX_FACT_TEXT = 500
MAX_FACT_KIND = 30

#: The extension's id grammar: `pf.summary`, `exp[0].highlights[2]`, `skill[7]`,
#: `persona.emphasis[1]`, `jd.company`. Anything else is refused.
FACT_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,30}(?:\[\d{1,3}\])?(?:\.[A-Za-z][A-Za-z0-9_]{0,30}(?:\[\d{1,3}\])?){0,3}$")

JOB_FACT_PREFIX = "jd."

# A numeric expression, with the currency / magnitude / multiplier / plus /
# percent forms answers actually use ("$5M", "150k", "10x", "5+", "40%",
# "1,000", "2.5"). Mirrors the extension's NUMERIC_RX closely enough that a
# number the server lets through is one the extension also accepts.
_NUMERIC_RE = re.compile(
    r"\$?\s?\b\d[\d,]*(?:\.\d+)?(?:\s?(?:k|mm|m|bn|b|x|million|billion|thousand|hundred)\b)?\+?%?",
    re.IGNORECASE,
)


class FactsError(ValueError):
    """The client's `facts` are malformed. The route answers 400 `invalid_facts`."""


def parse_facts(raw: Any, sanitize) -> Optional[List[Dict[str, str]]]:
    """Validate the request's `facts`. None when absent (the old request shape).

    `sanitize(text, max_length)` is `InputSanitizer.sanitize_string`, passed in
    so this module stays free of Flask/utility imports in tests.
    """
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise FactsError("facts must be a list")
    if len(raw) > MAX_FACTS:
        raise FactsError(f"at most {MAX_FACTS} facts")
    out: List[Dict[str, str]] = []
    seen = set()
    for item in raw:
        if not isinstance(item, dict):
            raise FactsError("each fact must be an object")
        fid = item.get("id")
        text = item.get("text")
        if not isinstance(fid, str) or not FACT_ID_RE.match(fid):
            raise FactsError("each fact needs a valid id")
        if not isinstance(text, str):
            raise FactsError("each fact needs text")
        if fid in seen:
            raise FactsError("fact ids must be unique")
        seen.add(fid)
        clean = sanitize(text, max_length=MAX_FACT_TEXT) or ""
        if not clean.strip():
            continue  # an empty fact grounds nothing; dropping it is harmless
        kind = item.get("kind")
        out.append({
            "id": fid,
            "kind": sanitize(kind, max_length=MAX_FACT_KIND) if isinstance(kind, str) else "",
            "text": clean.strip(),
        })
    if not any(not f["id"].startswith(JOB_FACT_PREFIX) for f in out):
        raise FactsError("facts must include at least one fact about the candidate")
    return out


def _numeric_base(raw: str) -> str:
    return re.sub(r"[$,\s]", "", raw.lower()).rstrip("+%").rstrip(".")


def numeric_bases(text: str) -> set:
    return {b for b in (_numeric_base(m) for m in _NUMERIC_RE.findall(text or "")) if b}


def check_variant(answer: str, used_ids: Iterable[Any],
                  facts_by_id: Dict[str, Dict[str, str]]) -> Tuple[bool, str, List[str]]:
    """`(ok, reason, cited_ids)` for one drafted variant.

    `cited_ids` is the de-duplicated, order-preserving list of string ids the
    model returned; `reason` names the first failed rule ('' when ok).
    """
    cited: List[str] = []
    for fid in used_ids or []:
        if isinstance(fid, str) and fid not in cited:
            cited.append(fid)
    unknown = [f for f in cited if f not in facts_by_id]
    if unknown:
        return False, "unknown_fact_id", cited
    if not any(not f.startswith(JOB_FACT_PREFIX) for f in cited):
        return False, "uncited", cited
    grounded = set()
    for fid in cited:
        grounded |= numeric_bases(facts_by_id[fid]["text"])
    for num in numeric_bases(answer):
        if num not in grounded:
            return False, "ungrounded_number", cited
    return True, "", cited


def facts_block(facts: List[Dict[str, str]], redact) -> str:
    """The facts as the model sees them: one `[id] text` line each.

    `redact` is `llm_safety.redact_pii_for_model`. The extension already strips
    the user's identifying values; redacting again here is defence in depth.
    """
    lines = []
    for f in facts:
        lines.append(f"[{f['id']}] {redact(f['text'])}")
    return "\n".join(lines)
