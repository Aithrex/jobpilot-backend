"""No dash punctuation or accented letters in the compliance track's copy.

`tests/test_user_facing_copy.py` skips these files (its ``OWNED_ELSEWHERE``)
and leaves them to their owner. This check is STRICTER than that one: every
string literal in these modules is scanned, not only strings in known copy
positions, because most of their output is email HTML assembled from plain
variables (``rows = ...``) that a position-based scan cannot see.

Skipped: docstrings and arguments to logging calls (read by engineers).
Offline and import-free: the files are parsed with ``ast``.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

OWNED = (
    "services/auth/blueprints/auth.py",
    "services/auth/services/oauth_service.py",
    "shared/python/services/account_service.py",
    "shared/python/services/email_service.py",
    "shared/python/services/apply_email_templates.py",
    "shared/python/services/notifications/service.py",
    "shared/python/services/notifications/channels.py",
    "shared/python/services/notifications/digests.py",
    "shared/python/services/scheduled_jobs.py",
    "shared/python/services/tech_feed_alerts.py",
    "shared/python/services/privacy_service.py",
)

BAD = re.compile(
    "\u2014|\u2013"                                   # em / en dash
    "|&(?:mdash|ndash|rarr|#8212|#8211);"             # the same as HTML entities
    "|\u2192"                                         # arrow used as a separator
    "|(?:^|(?<=\\s))-(?=\\s)"                         # spaced hyphen
    "|(?<=[\\w\\s])--(?=[\\w\\s])"                    # double hyphen between words
    "|[\u00c0-\u00d6\u00d8-\u00f6\u00f8-\u024f]"      # accented letters (resume, not the accented form)
)

_LOG_OWNERS = {"logger", "logging", "log", "_log", "_logger"}


def _violations(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    skip = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) \
                and body and isinstance(body[0], ast.Expr) \
                and isinstance(body[0].value, ast.Constant):
            skip.add(id(body[0].value))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and isinstance(node.func.value, ast.Name) and node.func.value.id in _LOG_OWNERS:
            skip.update(id(n) for n in ast.walk(node))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip:
            if BAD.search(node.value):
                yield node.lineno, node.value[:90]


@pytest.mark.parametrize("rel", OWNED)
def test_owned_copy_has_no_dash_punctuation(rel):
    found = list(_violations(ROOT / rel))
    assert not found, "\n".join(f"{rel}:{line} {text!r}" for line, text in found)


@pytest.mark.parametrize("text,bad", [
    ("Upload failed \u2014 try again", True),
    ("2\u20134 weeks", True),
    ("Saved - nice", True),
    ("Bad -- input", True),
    ("Open &mdash; now", True),
    ("Settings \u2192 Notifications", True),
    ("r\u00e9sum\u00e9", True),
    ("Full-time, H-1B, one-click \u00b7 sign-in", False),
    ("font-family:-apple-system", False),
])
def test_rule_self_check(text, bad):
    assert bool(BAD.search(text)) is bad
