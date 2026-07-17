"""Own-edit learning signals (Craft-overhaul Part B1).

The pipeline captures every manual edit a user makes to a tailored resume as a
``source:"edited"`` version (resume_versioning.append_version), but nothing has
ever consumed it. This service closes that loop: it reads a user's OWN edit
history, diffs each edited version against its parent to recover the phrasing the
user actually preferred, and renders a compact block the tailor prompt can use as
in-context grounding of that user's style.

FIREWALL — this is per-user only. Every read filters on the sanitized
``user_email`` of the current user; it never reads another user's records, and it
never moves any claim between users. It learns *phrasing preferences*, not facts:
the deterministic guards (integrity_guard, claim_guard) still gate the output, so
a user cannot launder a false claim in via an edit.

Mirrors ``profile_facts.enrichment_context`` in shape: return raw, char-capped
``"- "``-prefixed lines; the caller (resume_tailor) owns the header + untrusted
wrapping so framing stays centralized.
"""
from __future__ import annotations

import logging
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# How similar an edited bullet must be to a parent bullet to count as "the same
# bullet, reworded" (vs a brand-new bullet). Above this we treat it as an edit
# worth learning from; a near-identical match (>= _IDENTICAL) is not a real edit.
_REWORD_LOW = 0.55
_IDENTICAL = 0.97

# Caps so the block stays compact and cheap.
_MAX_RECORDS = 15
_MAX_EXAMPLES = 8


def _db():
    from utils.db_connect import DBConnect
    return DBConnect().get_db()


def _clean_email(user_email: str) -> Optional[str]:
    """Sanitize the JWT-supplied identity before it touches a Mongo filter."""
    from utils.security import InputSanitizer
    return InputSanitizer.sanitize_email(user_email or "")


def _norm(s: str) -> str:
    return " ".join((s or "").lower().split())


def _sim(a: str, b: str) -> float:
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ratio = SequenceMatcher(None, a, b).ratio()
    ta, tb = set(a.split()), set(b.split())
    jacc = len(ta & tb) / len(ta | tb) if (ta | tb) else 0.0
    return max(ratio, jacc)


def _all_bullets(resume: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    for section in ("experience", "projects"):
        for entry in resume.get(section, []) or []:
            for b in entry.get("bullets", []) or []:
                if isinstance(b, str) and b.strip():
                    out.append(b.strip())
    return out


def _match_reworded_pairs(
    parent_bullets: List[str], edited_bullets: List[str]
) -> List[Tuple[str, str]]:
    """Return (before, after) pairs where a parent bullet was reworded.

    Greedy best-match: for each edited bullet, find its closest parent bullet.
    A pair counts as a reword when similarity sits in the "same idea, different
    words" band (_REWORD_LOW .. _IDENTICAL). Exact/near-exact matches and
    entirely new bullets are ignored.
    """
    pairs: List[Tuple[str, str]] = []
    used_parent: set = set()
    for after in edited_bullets:
        best_i, best_s = -1, 0.0
        for i, before in enumerate(parent_bullets):
            if i in used_parent:
                continue
            s = _sim(before, after)
            if s > best_s:
                best_i, best_s = i, s
        if best_i >= 0 and _REWORD_LOW <= best_s < _IDENTICAL:
            used_parent.add(best_i)
            pairs.append((parent_bullets[best_i], after))
    return pairs


def _iter_edited_versions(record: Dict[str, Any]):
    """Yield (edited_version, parent_version) for each source:'edited' version."""
    from services.resume_versioning import ensure_versions, find_version

    versions, _ = ensure_versions(record)
    by_id = {v.get("version_id"): v for v in versions}
    for v in versions:
        if v.get("source") != "edited":
            continue
        parent = None
        pid = v.get("parent_version_id")
        if pid:
            parent = by_id.get(pid) or find_version(versions, pid)
        if parent:
            yield v, parent


def past_edits_context(user_email: str, max_chars: int = 1200) -> str:
    """Compact 'the user's own past edits' block for the tailor prompt.

    Returns raw ``"- before → after"`` lines plus any free-text feedback the
    user left, capped at ``max_chars``. Empty string when there's nothing to
    learn (new user, no edits, guest). NEVER raises — degrades to "".
    """
    email = _clean_email(user_email)
    if not email:
        return ""

    try:
        records = list(
            _db().tailoring_records.find(
                {"user_email": email, "deleted": {"$ne": True}},
                {"versions": 1, "user_email": 1, "deleted": 1},
            ).limit(_MAX_RECORDS)
        )
    except Exception as e:
        logger.warning("past_edits_context: read failed for %s: %s", email, e)
        return ""

    reword_lines: List[str] = []
    feedback_lines: List[str] = []
    seen_pairs: set = set()

    for record in records:
        # Defensive: never process a record that isn't this user's (belt-and-
        # suspenders on top of the query filter).
        if record.get("user_email") != email:
            continue
        for edited, parent in _iter_edited_versions(record):
            fb = (edited.get("user_feedback") or "").strip()
            if fb and fb not in feedback_lines:
                feedback_lines.append(fb)

            pairs = _match_reworded_pairs(
                _all_bullets(parent.get("tailored_resume") or {}),
                _all_bullets(edited.get("tailored_resume") or {}),
            )
            for before, after in pairs:
                key = (before, after)
                if key in seen_pairs:
                    continue
                seen_pairs.add(key)
                reword_lines.append(f"- rephrased: \"{before}\" → \"{after}\"")

    lines: List[str] = []
    if feedback_lines:
        lines.append("The user has previously asked for:")
        lines.extend(f"- {f}" for f in feedback_lines[:_MAX_EXAMPLES])
    if reword_lines:
        lines.append("The user has previously rephrased bullets like this "
                     "(match this style, do NOT copy the text verbatim):")
        lines.extend(reword_lines[:_MAX_EXAMPLES])

    if not lines:
        return ""

    # Char cap — keep whole lines.
    out: List[str] = []
    total = 0
    for line in lines:
        if total + len(line) + 1 > max_chars:
            break
        out.append(line)
        total += len(line) + 1
    return "\n".join(out)
