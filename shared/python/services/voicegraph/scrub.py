"""Third-party PII scrubbing (§6.12.3 rule 2 / privacy).

Collaborator nodes must store a *role*, never a name. A user writing "my manager
Sarah pushed back on the schema" yields Collaborator{label:"engineering manager"},
never "Sarah". Sarah did not consent to being in our database.

Interpretation note (recorded in ADR-002): we scrub third-party proper nouns out
of the STRUCTURED GRAPH (the queryable store) and out of any outward-facing
generated content. We do NOT rewrite the user's own encrypted writing sample —
that is their private asset, shown only back to them, and `raw` mode depends on
their literal words. So scrubbing happens at graph-insert time and at
content-generation time, not at sample storage time.
"""
import re

# Words that signal a ROLE (safe to keep as a Collaborator label).
_ROLE_HINTS = {
    "manager", "engineer", "developer", "lead", "director", "vp", "cto", "ceo",
    "cio", "founder", "cofounder", "co-founder", "designer", "recruiter",
    "professor", "advisor", "mentor", "teammate", "colleague", "collaborator",
    "scientist", "analyst", "architect", "intern", "student", "researcher",
    "head", "chief", "principal", "senior", "junior", "staff", "product",
    "engineering", "design", "data", "client", "customer", "stakeholder",
    "supervisor", "boss", "advisor", "ta", "professor", "dean", "peer",
}

_TOKEN = re.compile(r"[A-Za-z][A-Za-z.'-]*")


def _looks_like_person_name(label: str) -> bool:
    """Heuristic: 1-3 Capitalized alpha tokens, none of which is a role word,
    not an ALL-CAPS acronym. Deliberately conservative — only applied to
    Collaborator nodes, so a false positive just genericizes a collaborator."""
    if not label:
        return False
    tokens = _TOKEN.findall(label.strip())
    if not tokens or len(tokens) > 3:
        return False
    for t in tokens:
        low = t.lower().strip(".'-")
        if low in _ROLE_HINTS:
            return False
        if t.isupper() and len(t) > 1:      # acronym like "IBM", not a name
            return False
        if not (t[0].isupper() and t[1:].islower() if len(t) > 1 else t[0].isupper()):
            return False
    return True


def scrub_collaborator_label(label: str, role: str = "") -> str:
    """Return a role-only label for a Collaborator node; never a personal name."""
    role = (role or "").strip()
    if role and not _looks_like_person_name(role):
        return role
    if label and not _looks_like_person_name(label):
        return label.strip()
    return "collaborator"


def scrub_node(node: dict) -> dict:
    """Apply type-specific scrubbing before a node is persisted."""
    if node.get("type") == "Collaborator":
        node["label"] = scrub_collaborator_label(node.get("label", ""), node.get("role", ""))
    node.pop("role", None)  # role is folded into label; never stored separately
    return node
