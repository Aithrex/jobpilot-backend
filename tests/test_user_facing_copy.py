"""User-facing backend copy carries no dash punctuation.

House rule shared with the frontend (`pnpm copy:check`): a person never reads
an em dash, an en dash, a spaced hyphen used as punctuation (" - "), a double
hyphen between words, or a leading "- " separator. Those read as machine
written. Hyphenated words ("full-time", "H-1B") are fine, as are the middot
"·" and "->".

This test parses every blueprint and service module with ``ast`` (no import,
no database) and checks the string constants a person reads:

  * values of dict entries whose key is a copy key (``message``, ``error``,
    ``detail``, ``hint``, ``title``, ``description``, ``reason``, ...), which
    covers ``jsonify({...})`` bodies and payloads built as dicts;
  * keyword arguments with those names (``message=``, ``detail=``, ...);
  * ``abort(..., description=...)``.

Machine-readable codes (``"error": "upgrade_required"``) never contain a dash,
so they pass untouched. Logging calls are not scanned: a log line is read by
an engineer, not a user.

Every blueprint and service module is scanned; ``OWNED_ELSEWHERE`` is the
escape for a file another track is still editing, and it is empty.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

SCAN_ROOTS = [
    ROOT / "services",
    ROOT / "shared" / "python" / "services",
]

# Nothing is exempt. The compliance track's files (auth, account, email
# templates, notifications, scheduled jobs, tech feed alerts) were scanned
# separately while both tracks were in flight and now pass here too.
OWNED_ELSEWHERE: tuple = ()

COPY_KEYS = {
    "message", "msg", "error", "error_message", "detail", "details", "hint", "title",
    "subtitle", "description", "reason", "label", "summary", "headline", "body",
    "subject", "cta", "cta_label", "tooltip", "placeholder", "warning", "note",
    "notice", "explanation", "suggestion", "user_message", "display_message",
    "help", "help_text", "empty_state", "status_text", "next_step", "action_label",
    "display", "question",
}

# Dashes as punctuation. A hyphen inside a word ("full-time") never matches.
VIOLATIONS = [
    ("em dash", re.compile("\u2014")),
    ("en dash", re.compile("\u2013")),
    ("spaced hyphen", re.compile(r"(?:^|(?<=\s))-(?=\s|$)")),
    ("double hyphen", re.compile(r"(?<=[\w\s])--+(?=[\w\s]|$)")),
    ("dash entity", re.compile(r"&(?:mdash|ndash|#8212|#8211|#x2014|#x2013);", re.IGNORECASE)),
    ("accented letter", re.compile("[\u00c0-\u00d6\u00d8-\u00f6\u00f8-\u024f]")),
]

PLACEHOLDER = "\u0001"


def _is_scanned(path: Path) -> bool:
    rel = path.relative_to(ROOT).as_posix()
    if "/tests/" in rel or rel.startswith("tests/"):
        return False
    return not any(rel.startswith(p) for p in OWNED_ELSEWHERE)


def _text_of(node: ast.AST) -> str | None:
    """The literal text of a str constant or f-string (substitutions as a placeholder)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = []
        for v in node.values:
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                parts.append(v.value)
            else:
                parts.append(PLACEHOLDER)
        return "".join(parts)
    return None


def _copy_values(node: ast.AST):
    """Yield string nodes that sit in a copy position under ``node``."""
    if isinstance(node, (ast.Constant, ast.JoinedStr)):
        yield node
    elif isinstance(node, ast.IfExp):
        yield from _copy_values(node.body)
        yield from _copy_values(node.orelse)
    elif isinstance(node, ast.BoolOp):
        for v in node.values:
            yield from _copy_values(v)
    elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        yield from _copy_values(node.left)
        yield from _copy_values(node.right)
    elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in {"format", "join"}:
        yield from _copy_values(node.func.value)


# Variables, attributes and function names that hold copy: ``label = ...``,
# ``UNSUPPORTED_MESSAGE = ...``, ``html_body = ...``, ``def _reason(...)``.
_COPY_NAME = re.compile(
    r"(?:^|_)(label|message|msg|title|subtitle|headline|text|summary|description|detail|"
    r"hint|reason|note|caption|body|html|subject|cta|tooltip|warning|blurb|salary)$",
    re.IGNORECASE,
)


# Lists the UI renders line by line: ``warnings.append("...")``.
_COPY_LIST = re.compile(
    r"(?:^|_)(reasons|warnings|nudges|suggestions|tips|insights|highlights|messages|notes)$",
    re.IGNORECASE,
)


def _target_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def violations_in_source(source: str, filename: str = "<string>"):
    """[(line, kind, text)] for every copy string with dash punctuation."""
    tree = ast.parse(source, filename=filename)
    found = []
    seen = set()

    def check(value_node):
        for s in _copy_values(value_node):
            text = _text_of(s)
            if not text or text.startswith(("http://", "https://", "/")) or id(s) in seen:
                continue
            seen.add(id(s))
            for kind, rx in VIOLATIONS:
                if rx.search(text):
                    found.append((s.lineno, kind, text.replace(PLACEHOLDER, "{...}")))
                    break

    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if isinstance(k, ast.Constant) and isinstance(k.value, str) and k.value in COPY_KEYS:
                    check(v)
        elif isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            owner = func.value.id if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) else ""
            if owner in {"logger", "log", "logging", "LOG", "_log", "_logger"} or name in {"print"}:
                continue
            for kw in node.keywords:
                if kw.arg in COPY_KEYS:
                    check(kw.value)
            if name == "abort" and len(node.args) > 1:
                check(node.args[1])
            # reasons.append("..."), warnings.append("..."): lists the UI renders.
            if (name in {"append", "insert", "extend"} and isinstance(func, ast.Attribute)
                    and _COPY_LIST.search(_target_name(func.value))):
                for arg in node.args:
                    check(arg)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if node.value is not None and any(_COPY_NAME.search(_target_name(t)) for t in targets):
                check(node.value)
        elif isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call) and node.exc.args:
            # A raised message is what a blueprint hands back as ``error``.
            check(node.exc.args[0])
        elif isinstance(node, ast.FunctionDef) and _COPY_NAME.search(node.name):
            for r in ast.walk(node):
                if isinstance(r, ast.Return) and r.value is not None:
                    check(r.value)
    return found


def _scan_tree():
    results = []
    for root in SCAN_ROOTS:
        for path in sorted(root.rglob("*.py")):
            if not _is_scanned(path):
                continue
            src = path.read_text(encoding="utf-8")
            for line, kind, text in violations_in_source(src, str(path)):
                results.append(f"{path.relative_to(ROOT).as_posix()}:{line}  {text[:90]!r}  [{kind}]")
    return results


# --------------------------------------------------------------------------
# The rules themselves.
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "source,expected",
    [
        ('jsonify({"error": "Upload failed \u2014 try again"})', ["em dash"]),
        ('jsonify({"message": "Saved - nice"})', ["spaced hyphen"]),
        ('return {"detail": f"{a} - {b}"}', ["spaced hyphen"]),
        ('return {"hint": "2\u20134 weeks"}', ["en dash"]),
        ('abort(400, description="Bad -- input")', ["double hyphen"]),
        ('make(message="One - two")', ["spaced hyphen"]),
        ('return {"error": "upgrade_required", "message": "Upgrade to continue."}', []),
        ('return {"message": "Full-time, H-1B, one-click \u00b7 2-4 years"}', []),
        ('logger.warning("x \u2014 y", message="a - b")', []),
        ('x = {"id": "a - b", "error": None}', []),
        ('label = f"Most recent role \u2014 {who}"', ["em dash"]),
        ('raise ValueError("Could not connect \u2014 try again")', ["em dash"]),
        ('def _reason(x):\n    return "Using it - only one"', ["spaced hyphen"]),
        ('html_body = "<p>Hello &mdash; there</p>"', ["dash entity"]),
        ('return {"message": "Upload a r\u00e9sum\u00e9"}', ["accented letter"]),
        ('prompt = "Rewrite \u2014 keep facts"', []),
        ('reasons.append("Talent-pool language \u2014 may not be open")', ["em dash"]),
    ],
)
def test_rules(source, expected):
    assert [kind for _, kind, _ in violations_in_source(source)] == expected


def test_backend_user_facing_strings_have_no_dash_punctuation():
    found = _scan_tree()
    assert not found, "Rewrite these by hand (period, comma, colon, 'to', or the middot):\n" + "\n".join(found)
