"""Shared JSON parsing + repair helpers.

Both providers can return malformed JSON in edge cases (truncation, markdown
fences, trailing text). These helpers implement the same multi-strategy
cascade that the original `gemini_client.py` used.
"""
import json
import re
from typing import Optional


def clean_json_response(text: str) -> str:
    """Strip markdown fences and trailing garbage from an LLM response."""
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?\s*\n?", "", t)
        t = re.sub(r"\n?```\s*$", "", t)
    for end_char in ("}", "]"):
        idx = t.rfind(end_char)
        if idx != -1:
            t = t[: idx + 1]
            break
    return t.strip()


def repair_truncated_json(raw: str) -> str:
    """Close unclosed brackets/strings on truncated output."""
    if not raw or not raw.lstrip().startswith('{'):
        return ""

    text = raw.rstrip()
    in_string = False
    last_good = 0
    for i, ch in enumerate(text):
        if ch == '"' and (i == 0 or text[i - 1] != '\\'):
            in_string = not in_string
        if not in_string and ch in ',]}':
            last_good = i

    if in_string:
        text = text[:last_good + 1] if last_good > 0 else text

    opens = 0
    open_arrays = 0
    for ch in text:
        if ch == '{':
            opens += 1
        elif ch == '}':
            opens -= 1
        elif ch == '[':
            open_arrays += 1
        elif ch == ']':
            open_arrays -= 1

    text = text.rstrip().rstrip(',')
    text += ']' * max(0, open_arrays) + '}' * max(0, opens)
    return text


def try_parse_json(raw: str) -> Optional[dict]:
    """Multi-strategy JSON parse: direct → cleaned → regex-extracted → repaired."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    cleaned = clean_json_response(raw)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    json_match = re.search(r'\{[\s\S]*\}', raw)
    if json_match:
        try:
            return json.loads(json_match.group())
        except json.JSONDecodeError:
            pass

    repaired = repair_truncated_json(raw)
    if repaired:
        try:
            return json.loads(repaired)
        except json.JSONDecodeError:
            pass

    return None


def python_schema_to_json_schema(spec) -> dict:
    """Convert the project's custom Python schema DSL to standard JSON Schema.

    Mirrors `_to_openapi_schema` but emits JSON Schema (lowercase `"type"`)
    instead of OpenAPI (uppercase `"STRING"`). Used by Claude tool-use to
    enforce structured output.
    """
    if spec is str:
        return {"type": "string"}
    if spec is int:
        return {"type": "integer"}
    if spec is float:
        return {"type": "number"}
    if isinstance(spec, list) and len(spec) == 1:
        return {"type": "array", "items": python_schema_to_json_schema(spec[0])}
    if isinstance(spec, dict) and "_dict_of" in spec:
        return {
            "type": "object",
            "additionalProperties": python_schema_to_json_schema(spec["_dict_of"]),
        }
    if isinstance(spec, dict):
        props = {k: python_schema_to_json_schema(v) for k, v in spec.items()}
        return {"type": "object", "properties": props}
    return {"type": "string"}
