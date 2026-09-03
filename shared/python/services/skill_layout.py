"""Skill headings: capture, classify, group, name.

Parse captures the user's labels. Classification is an internal index
(`kind` is never rendered). Headings are derived from the final term set
and only reused from the base when that layout is actually good.
"""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Literal, Optional, Tuple

from utils.keyword_normalizer import (
    normalize_single,
    taxonomy_group,
    is_programming_language,
)

logger = logging.getLogger(__name__)

Mode = Literal["PRESERVE", "REFINE", "DERIVE"]
Provenance = Literal["user", "inferred"]

CANNED_HEADINGS = frozenset({
    "Languages", "Frameworks & Libraries", "AI & ML", "Cloud & DevOps",
    "Databases", "Tools & Platforms", "Concepts & Methodologies",
    "Professional Skills",
})

ROLE_LABELS = frozenset({
    "backend", "frontend", "fullstack", "full-stack", "full stack",
    "back-end", "front-end", "full-stack development",
    "fullstack development", "full stack development",
    "backend development", "frontend development",
    "software engineering", "software development",
    "software engineer", "full-stack developer", "fullstack developer",
    "full stack developer", "backend engineer", "frontend engineer",
    "full stack engineer", "fullstack engineer", "full-stack engineer",
})

BANNED_HEADINGS = frozenset({
    "tool", "tools", "concept", "concepts", "other", "miscellaneous",
    "misc", "concepts & methodologies", "professional skills",
    "soft skills", "general", "technical stack", "tech stack",
    "ai & standards",
})

KIND_PRIORITY = (
    "language", "framework", "database", "messaging", "cloud", "infra",
    "observability", "testing", "ai_ml", "standard", "tool",
)

AFFINITY: Dict[frozenset, str] = {
    frozenset({"messaging", "database"}): "Distributed Systems & Data",
    frozenset({"testing", "observability"}): "Observability & Testing",
    frozenset({"infra", "cloud"}): "Cloud & Infrastructure",
    frozenset({"framework", "language"}): "Languages & Frameworks",
}

_GROUP_TO_KIND: Dict[str, Tuple[str, Optional[str]]] = {
    "programming_languages": ("language", None),
    "frontend": ("framework", "frontend"),
    "backend": ("framework", "backend"),
    "apis": ("framework", "api"),
    "mobile": ("framework", "mobile"),
    "databases": ("database", None),
    "cloud": ("cloud", None),
    "devops": ("infra", None),
    "ai_ml": ("ai_ml", None),
    "testing": ("testing", None),
    "methodologies": ("standard", None),
    "data_engineering": ("messaging", None),
    "security": ("tool", "security"),
    "cybersecurity": ("tool", "security"),
    "version_control": ("tool", None),
    "soft_skills": ("soft", None),
    "analytics": ("tool", None),
    "it_support": ("tool", None),
    "project_mgmt": ("standard", None),
    "finance": ("standard", None),
    "healthcare": ("standard", None),
    "marketing": ("standard", None),
    "sales": ("standard", None),
    "product": ("standard", None),
    "hr": ("standard", None),
    "consulting": ("standard", None),
    "construction": ("standard", None),
    "engineering_disciplines": ("standard", None),
}

_KIND_BY_TERM: Dict[str, Tuple[str, Optional[str]]] = {
    "kafka": ("messaging", None),
    "apache kafka": ("messaging", None),
    "rabbitmq": ("messaging", None),
    "redis": ("messaging", None),
    "celery": ("messaging", None),
    "pgvector": ("database", "vector-db"),
    "pinecone": ("database", "vector-db"),
    "weaviate": ("database", "vector-db"),
    "chromadb": ("database", "vector-db"),
    "opentelemetry": ("observability", "tracing"),
    "datadog": ("observability", None),
    "prometheus": ("observability", None),
    "grafana": ("observability", None),
    "elk stack": ("observability", None),
    "splunk": ("observability", None),
    "new relic": ("observability", None),
    "terraform": ("infra", "iac"),
    "helm": ("infra", "iac"),
    "ansible": ("infra", "iac"),
    "pulumi": ("infra", "iac"),
    "docker": ("infra", None),
    "kubernetes": ("infra", None),
    "pytest": ("testing", None),
    "junit": ("testing", None),
    "mockito": ("testing", None),
    "jest": ("testing", None),
    "cypress": ("testing", None),
    "selenium": ("testing", None),
    "spring data jpa": ("framework", "backend"),
    "spring data": ("framework", "backend"),
    "spring jpa": ("framework", "backend"),
    "github actions": ("infra", None),
    "rag": ("ai_ml", None),
    "rag pipelines": ("ai_ml", None),
    "vector embeddings": ("ai_ml", None),
    "embeddings": ("ai_ml", None),
    "llm api integration": ("ai_ml", None),
    "llm api": ("ai_ml", None),
    "prompt engineering": ("ai_ml", None),
    "langchain": ("ai_ml", None),
    "fhir": ("standard", "healthcare"),
    "fhir r4": ("standard", "healthcare"),
    "hipaa": ("standard", "healthcare"),
    "devops": ("infra", None),
    "microservices": ("standard", None),
    "async programming": ("standard", None),
    "query optimization": ("database", None),
    "monitoring": ("observability", None),
    "elk": ("observability", None),
}

_VERSION_RE = re.compile(
    r"\s+(?:v|se|ee)?\s*\d+(?:\.\d+)*\s*$",
    re.IGNORECASE,
)
_PAREN_RE = re.compile(r"\s*\(([^)]*)\)\s*$")


@dataclass(frozen=True)
class TermFacts:
    canonical: str
    kind: Optional[str]
    sub: Optional[str] = None
    display: str = ""


@dataclass
class SkillCategory:
    label: str
    items: List[str]
    provenance: Provenance = "user"


@dataclass
class LayoutScore:
    quality: float
    has_labels: bool
    balance: float
    agreement: float
    junk_ratio: float
    cardinality_ok: bool


@dataclass
class ComposeResult:
    skills: Dict[str, List[str]]
    mode: Mode
    score: LayoutScore
    unknown_queued: List[str] = field(default_factory=list)


# Composition hash → heading. Seeded so naming is stable without a model.
_NAME_CACHE: Dict[str, str] = {
    "language": "Languages",
    "framework": "Frameworks",
    "database": "Databases",
    "cloud": "Cloud",
    "infra": "Infrastructure",
    "observability": "Observability",
    "testing": "Testing",
    "messaging": "Messaging",
    "ai_ml": "AI & ML",
    "standard": "Standards",
    "cloud+infra": "Cloud & Infrastructure",
    "database+messaging": "Distributed Systems & Data",
    "observability+testing": "Observability & Testing",
    "framework+language": "Languages & Frameworks",
}

# Bare parent words that echo a heading. Dropped when a more specific
# sibling of the same kind is already on the resume (AWS + "Cloud").
GENERIC_PARENTS: Dict[str, str] = {
    "cloud": "cloud",
    "monitoring": "observability",
    "devops": "infra",
    "infrastructure": "infra",
    "observability": "observability",
    "ci/cd": "infra",
    "cicd": "infra",
}

ALWAYS_DROP = frozenset({
    "technical stack", "tech stack", "software", "tools", "platforms",
})

_DESCRIPTOR_TAIL = re.compile(
    r"\s+(pipelines?|integration|programming|optimization|"
    r"api(?:\s+integration)?)\s*$",
    re.IGNORECASE,
)
_SPEC_REV_RE = re.compile(r"\s+r\d+\s*$", re.IGNORECASE)


def is_role_label(term: str) -> bool:
    t = re.sub(r"\s+", " ", str(term or "").strip().lower())
    return t in ROLE_LABELS


def lookup_key(term: str) -> str:
    """Normalize a surface form for index lookup (Java 17 → java)."""
    raw = _PAREN_RE.sub("", str(term or "")).strip()
    raw = re.sub(
        r"\s+(programming|language|lang)\s*$", "", raw, flags=re.I,
    ).strip()
    raw = _VERSION_RE.sub("", raw).strip()
    raw = _SPEC_REV_RE.sub("", raw).strip()
    if not raw:
        return ""
    return (normalize_single(raw) or raw).lower()


def _classify_key(key: str, display: str) -> TermFacts:
    if key in _KIND_BY_TERM:
        kind, sub = _KIND_BY_TERM[key]
        return TermFacts(key, kind, sub, display)
    if is_programming_language(key) or is_programming_language(display):
        return TermFacts(key, "language", None, display)
    group = taxonomy_group(key) or taxonomy_group(display)
    if group == "data_engineering" and key in _KIND_BY_TERM:
        kind, sub = _KIND_BY_TERM[key]
        return TermFacts(key, kind, sub, display)
    if group:
        kind, sub = _GROUP_TO_KIND.get(group, (None, None))
        if group == "data_engineering" and kind == "messaging":
            # Most data-eng terms are pipelines, not brokers.
            if key not in {"kafka", "apache kafka", "rabbitmq", "redis", "celery"}:
                kind, sub = "database", None
        return TermFacts(key, kind, sub, display)
    return TermFacts(key, None, None, display)


def _lookup_candidates(key: str) -> List[str]:
    """Full key, then drop trailing descriptors / last tokens (RAG pipelines → RAG)."""
    out: List[str] = []
    seen = set()

    def add(item: str) -> None:
        item = (item or "").strip()
        if item and item not in seen:
            seen.add(item)
            out.append(item)

    add(key)
    stripped = key
    for _ in range(4):
        m = _DESCRIPTOR_TAIL.search(stripped)
        if not m:
            break
        stripped = stripped[:m.start()].strip()
        add(stripped)
    parts = key.split()
    for i in range(len(parts) - 1, 0, -1):
        add(" ".join(parts[:i]))
    return out


def lookup_term(term: str) -> TermFacts:
    display = str(term or "").strip()
    if is_role_label(display):
        return TermFacts("", "soft", None, display)
    raw = _PAREN_RE.sub("", display).strip().lower()
    key = lookup_key(display)
    candidates = [c for c in (raw, key) if c]
    if key:
        candidates.extend(_lookup_candidates(key))
    seen = set()
    for cand in candidates:
        if cand in seen:
            continue
        seen.add(cand)
        facts = _classify_key(cand, display)
        if facts.kind:
            return facts
    return TermFacts(key or raw, None, None, display)


def index_terms(terms: Iterable[str]) -> Dict[str, TermFacts]:
    out: Dict[str, TermFacts] = {}
    for term in terms:
        facts = lookup_term(term)
        if facts.canonical:
            out.setdefault(facts.canonical, facts)
        elif str(term).strip():
            out.setdefault(str(term).strip().lower(), facts)
    return out


def layout_from_skills(
    skills: Any,
    *,
    provenance: Optional[Provenance] = None,
    stored: Optional[List[Any]] = None,
) -> List[SkillCategory]:
    if stored:
        rows: List[SkillCategory] = []
        for row in stored:
            if not isinstance(row, dict):
                continue
            label = str(row.get("label") or "").strip()
            items = [
                str(s).strip() for s in (row.get("items") or [])
                if str(s).strip() and not is_role_label(s)
            ]
            if not label or not items:
                continue
            prov = row.get("provenance")
            if prov not in ("user", "inferred"):
                prov = "user"
            rows.append(SkillCategory(label, items, prov))
        if rows:
            return rows
    if isinstance(skills, list):
        items = [
            str(s).strip() for s in skills
            if str(s).strip() and not is_role_label(s)
        ]
        return [SkillCategory("Skills", items, "inferred")] if items else []
    if not isinstance(skills, dict):
        return []
    labels = [str(k).strip() for k, v in skills.items() if v]
    inferred = (
        bool(labels)
        and all(lab in CANNED_HEADINGS for lab in labels)
        and provenance != "user"
    )
    prov: Provenance = provenance or ("inferred" if inferred else "user")
    rows = []
    for cat, items in skills.items():
        if not isinstance(items, list):
            continue
        kept = [
            str(s).strip() for s in items
            if isinstance(s, str) and s.strip() and not is_role_label(s)
        ]
        if kept:
            rows.append(SkillCategory(str(cat), kept, prov))
    return rows


def layout_from_resume(structured: Dict[str, Any]) -> List[SkillCategory]:
    return layout_from_skills(
        (structured or {}).get("skills"),
        stored=(structured or {}).get("skills_layout"),
    )


def skills_dict(layout: List[SkillCategory]) -> Dict[str, List[str]]:
    return {row.label: list(row.items) for row in layout if row.items}


def score_layout(layout: List[SkillCategory]) -> LayoutScore:
    if not layout:
        return LayoutScore(0.0, False, 0.0, 0.0, 1.0, False)
    n_cats = len(layout)
    sizes = [len(row.items) for row in layout]
    total = sum(sizes) or 1
    largest = max(sizes) / total
    singles = sum(1 for s in sizes if s == 1)
    balance = 1.0
    if largest > 0.40:
        balance -= min(0.4, (largest - 0.40) * 2)
    if singles:
        balance -= min(0.3, 0.1 * singles)
    balance = max(0.0, balance)

    agree_hits = 0
    agree_n = 0
    junk = 0
    for row in layout:
        kinds = [lookup_term(s).kind for s in row.items]
        known = [k for k in kinds if k and k != "soft"]
        agree_n += len(row.items)
        if not known:
            junk += len(row.items)
            continue
        from collections import Counter
        dom, count = Counter(known).most_common(1)[0]
        if count / len(known) < 0.5:
            junk += len(row.items)
        agree_hits += sum(1 for k in kinds if k == dom)
    agreement = (agree_hits / agree_n) if agree_n else 0.0
    junk_ratio = junk / total
    cardinality_ok = 4 <= n_cats <= 8
    has_labels = any(row.provenance == "user" for row in layout)
    quality = (
        0.30 * balance
        + 0.35 * agreement
        + 0.20 * (1.0 - junk_ratio)
        + 0.15 * (1.0 if cardinality_ok else 0.4)
    )
    if n_cats < 4 or total < 8:
        quality = min(quality, 0.65)
    return LayoutScore(
        round(quality, 3), has_labels, round(balance, 3),
        round(agreement, 3), round(junk_ratio, 3), cardinality_ok,
    )


def resolve_mode(layout: List[SkillCategory], score: Optional[LayoutScore] = None) -> Mode:
    score = score or score_layout(layout)
    if not layout:
        return "DERIVE"
    if all(row.provenance == "inferred" for row in layout):
        return "DERIVE"
    if score.quality >= 0.75:
        return "PRESERVE"
    if score.quality >= 0.40:
        return "REFINE"
    return "DERIVE"


def composition_hash(items: List[str]) -> str:
    kinds: Dict[str, int] = {}
    subs: Dict[str, int] = {}
    for term in items:
        facts = lookup_term(term)
        if facts.kind:
            kinds[facts.kind] = kinds.get(facts.kind, 0) + 1
        if facts.sub:
            subs[facts.sub] = subs.get(facts.sub, 0) + 1
    blob = (
        ",".join(f"{k}:{kinds[k]}" for k in sorted(kinds))
        + "|"
        + ",".join(f"{s}:{subs[s]}" for s in sorted(subs))
    )
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:12]


def cached_name(items: List[str]) -> Optional[str]:
    kinds = sorted({
        k for k in (lookup_term(t).kind for t in items) if k and k != "soft"
    })
    if not kinds:
        return None
    key = "+".join(kinds)
    if key in _NAME_CACHE:
        return _NAME_CACHE[key]
    if len(kinds) == 1 and kinds[0] in _NAME_CACHE:
        return _NAME_CACHE[kinds[0]]
    return None


def remember_name(items: List[str], heading: str) -> None:
    kinds = sorted({
        k for k in (lookup_term(t).kind for t in items) if k and k != "soft"
    })
    if kinds:
        _NAME_CACHE["+".join(kinds)] = heading


def _dominant_kind(items: List[str]) -> Optional[str]:
    from collections import Counter
    kinds = [lookup_term(t).kind for t in items]
    known = [k for k in kinds if k and k != "soft"]
    if not known:
        return None
    return Counter(known).most_common(1)[0][0]


def _affinity_label(kinds: Iterable[Optional[str]]) -> Optional[str]:
    known = {k for k in kinds if k}
    if len(known) < 2:
        return None
    for pair, label in AFFINITY.items():
        if pair <= known:
            return label
    return None


def _seed_groups(terms: List[str]) -> Dict[str, List[str]]:
    groups: Dict[str, List[str]] = {}
    unknown: List[str] = []
    for term in terms:
        facts = lookup_term(term)
        if facts.kind is None:
            unknown.append(term)
            continue
        if facts.kind == "soft":
            continue
        groups.setdefault(facts.kind, []).append(term)
    if unknown:
        groups.setdefault("_unknown", unknown)
    return groups


def _merge_small(groups: Dict[str, List[str]]) -> Dict[str, List[str]]:
    """Fold tiny groups only into a real affinity partner — never a neighbor dump."""
    moved = True
    while moved:
        moved = False
        small = [k for k, v in groups.items() if k != "_unknown" and 0 < len(v) <= 2]
        for key in small:
            kinds_here = {lookup_term(t).kind for t in groups[key]}
            best = None
            for other, items in groups.items():
                if other in (key, "_unknown") or not items:
                    continue
                other_kind = _dominant_kind(items)
                if _affinity_label(kinds_here | {other_kind}):
                    best = other
                    break
            if best:
                groups[best].extend(groups.pop(key))
                moved = True
                break
    return groups


def _merge_overflow(groups: Dict[str, List[str]]) -> Dict[str, List[str]]:
    """When we still have more than 8 headings, merge the smallest affinity pair."""
    while True:
        labeled = [k for k, v in groups.items() if v and k != "_unknown"]
        if len(labeled) <= 8:
            return groups
        best = None
        best_size = 10**9
        for i, a in enumerate(labeled):
            for b in labeled[i + 1:]:
                kinds = {_dominant_kind(groups[a]), _dominant_kind(groups[b])}
                if not _affinity_label(kinds):
                    continue
                size = len(groups[a]) + len(groups[b])
                if size < best_size:
                    best = (a, b)
                    best_size = size
        if not best:
            return groups
        a, b = best
        groups[a].extend(groups.pop(b))
    return groups


def _split_large(groups: Dict[str, List[str]]) -> Dict[str, List[str]]:
    extra: Dict[str, List[str]] = {}
    for key, items in list(groups.items()):
        if key == "_unknown" or len(items) <= 10:
            continue
        by_sub: Dict[str, List[str]] = {}
        rest: List[str] = []
        for term in items:
            sub = lookup_term(term).sub
            if sub:
                by_sub.setdefault(sub, []).append(term)
            else:
                rest.append(term)
        viable = [(s, ts) for s, ts in by_sub.items() if len(ts) >= 3]
        if not viable:
            continue
        sub, taken = max(viable, key=lambda kv: len(kv[1]))
        leftover = [t for t in items if t not in taken]
        if len(leftover) >= 3 and len(taken) >= 3:
            extra[f"{key}:{sub}"] = taken
            groups[key] = leftover
    groups.update(extra)
    return groups


def _drop_generic_parents(terms: List[str]) -> List[str]:
    kinds_present = set()
    for t in terms:
        if is_generic_parent(t):
            continue
        kind = lookup_term(t).kind
        if kind and kind != "soft":
            kinds_present.add(kind)
    kept: List[str] = []
    for t in terms:
        key = lookup_key(t) or t.strip().lower()
        if key in ALWAYS_DROP:
            continue
        parent_kind = GENERIC_PARENTS.get(key)
        if parent_kind and parent_kind in kinds_present:
            continue
        kept.append(t)
    return kept


def is_generic_parent(term: str) -> bool:
    key = lookup_key(term) or str(term or "").strip().lower()
    return key in GENERIC_PARENTS or key in ALWAYS_DROP


def group_terms(terms: List[str]) -> List[List[str]]:
    """Deterministic groups for a term set. Same terms → same groups."""
    unique: List[str] = []
    seen = set()
    for t in terms:
        if is_role_label(t) or is_generic_parent(t) and (
            lookup_key(t) or t.strip().lower()
        ) in ALWAYS_DROP:
            continue
        key = lookup_key(t) or t.lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(t)
    unique = _drop_generic_parents(unique)
    groups = _seed_groups(unique)
    for _ in range(6):
        before = {k: tuple(v) for k, v in groups.items()}
        n = len([k for k, v in groups.items() if v and k != "_unknown"])
        if n > 7:
            groups = _merge_small(groups)
        elif n < 5:
            groups = _split_large(groups)
            groups = _merge_small(groups)
        else:
            groups = _split_large(groups)
            groups = _merge_small(groups)
        after = {k: tuple(v) for k, v in groups.items()}
        if after == before:
            break
    n = len([k for k, v in groups.items() if v and k != "_unknown"])
    if n > 7:
        groups = _merge_small(groups)
    groups = _merge_overflow(groups)
    return [list(v) for k, v in groups.items() if v and k != "_unknown"]


def _overlap(a: List[str], b: List[str]) -> float:
    ka = {lookup_key(t) or t.lower() for t in a}
    kb = {lookup_key(t) or t.lower() for t in b}
    if not ka or not kb:
        return 0.0
    return len(ka & kb) / len(ka)


def name_group(
    items: List[str],
    base: List[SkillCategory],
    siblings: List[str],
    namer: Optional[Callable[[List[str], List[str]], str]] = None,
) -> str:
    banned = {s.lower() for s in siblings} | BANNED_HEADINGS
    for row in base:
        if row.provenance == "inferred" and row.label in CANNED_HEADINGS:
            continue
        if _overlap(items, row.items) >= 0.60:
            if row.label.lower() not in banned:
                return row.label
    cached = cached_name(items)
    if cached and cached.lower() not in banned:
        return cached
    affinity = _affinity_label(lookup_term(t).kind for t in items)
    if affinity and affinity.lower() not in banned:
        return affinity
    if namer:
        try:
            proposed = (namer(items, siblings) or "").strip()
            if (
                proposed
                and proposed.lower() not in banned
                and not proposed.lower().endswith(" skills")
                and not is_role_label(proposed)
            ):
                remember_name(items, proposed)
                return proposed
        except Exception:
            logger.warning("skill_layout namer failed", exc_info=True)
    dom = _dominant_kind(items)
    if dom and _NAME_CACHE.get(dom) and _NAME_CACHE[dom].lower() not in banned:
        return _NAME_CACHE[dom]
    return _NAME_CACHE.get(dom or "", "Skills")


def order_groups(
    labeled: List[SkillCategory],
    mode: Mode,
    jd_required: List[str],
    base: List[SkillCategory],
) -> List[SkillCategory]:
    req = {lookup_key(t) for t in jd_required if lookup_key(t)}

    def req_hits(row: SkillCategory) -> int:
        return sum(1 for t in row.items if (lookup_key(t) or "") in req)

    if mode in ("PRESERVE", "REFINE") and base:
        base_pos = {row.label: i for i, row in enumerate(base)}
        indexed = list(enumerate(labeled))
        indexed.sort(key=lambda iv: base_pos.get(iv[1].label, 100 + iv[0]))
        # Bounded promotion: ≥2 required terms may move up at most 2 slots.
        items = [row for _, row in indexed]
        for i, row in enumerate(list(items)):
            if req_hits(row) < 2:
                continue
            dest = max(0, i - 2)
            if dest < i:
                items.insert(dest, items.pop(i))
        # Never alphabetical: if we somehow produced sorted labels and base
        # order differs, restore base order.
        labels = [r.label for r in items]
        if labels == sorted(labels) and [r.label for r in base] != labels:
            by_label = {r.label: r for r in items}
            restored = [by_label[r.label] for r in base if r.label in by_label]
            restored.extend(r for r in items if r.label not in {x.label for x in restored})
            items = restored
        return items

    def derive_key(row: SkillCategory) -> Tuple[int, int]:
        hits = req_hits(row)
        dom = _dominant_kind(row.items) or "tool"
        pri = KIND_PRIORITY.index(dom) if dom in KIND_PRIORITY else 99
        return (-hits, pri)

    return sorted(labeled, key=derive_key)


def validate_layout(
    layout: List[SkillCategory],
    base: List[SkillCategory],
    *,
    added: Optional[List[str]] = None,
    evidenced: Optional[set] = None,
) -> List[str]:
    reasons: List[str] = []
    if not (4 <= len(layout) <= 8) and base and len(base) >= 4:
        if not (4 <= len(layout) <= 8):
            reasons.append(f"shape:{len(layout)}_categories")
    for row in layout:
        if len(row.items) > 12:
            reasons.append(f"shape:{row.label}_overfull")
        if is_role_label(row.label) or row.label.lower() in BANNED_HEADINGS:
            reasons.append(f"naming:{row.label}")
        if any(is_role_label(t) for t in row.items):
            reasons.append(f"role_label:{row.label}")
        if any(lookup_term(t).kind == "soft" for t in row.items):
            reasons.append(f"soft:{row.label}")
    labels = [r.label.lower() for r in layout]
    if len(labels) != len(set(labels)):
        reasons.append("naming:duplicate")
    lang_home = None
    for row in layout:
        if _dominant_kind(row.items) == "language":
            lang_home = row.label
            break
    if lang_home:
        for row in layout:
            if row.label == lang_home:
                continue
            for term in row.items:
                if lookup_term(term).kind == "language":
                    reasons.append(f"language_integrity:{term}")
    base_keys = {lookup_key(t) or t.lower() for r in base for t in r.items}
    out_keys = {lookup_key(t) or t.lower() for r in layout for t in r.items}
    missing = base_keys - out_keys
    if missing:
        reasons.append(f"retention:{len(missing)}")
    if added and evidenced is not None:
        for term in added:
            key = lookup_key(term) or term.lower()
            if key not in evidenced:
                reasons.append(f"evidence:{term}")
    again = group_terms([t for r in layout for t in r.items])
    # Determinism of grouping — same bag, same partition sizes.
    if sorted(len(g) for g in again) != sorted(len(r.items) for r in layout):
        # Only enforce when we derived; preserve/refine keep user partitions.
        pass
    return reasons


def compose(
    base: List[SkillCategory],
    terms: List[str],
    *,
    jd_required: Optional[List[str]] = None,
    namer: Optional[Callable[[List[str], List[str]], str]] = None,
    place_unknown: Optional[Callable[[str, List[SkillCategory]], Optional[str]]] = None,
) -> ComposeResult:
    score = score_layout(base)
    mode = resolve_mode(base, score)
    jd_required = jd_required or []
    clean = [t for t in terms if str(t).strip() and not is_role_label(t)]
    unknown_queued: List[str] = []

    if mode == "PRESERVE" and base:
        labeled = [SkillCategory(r.label, [], r.provenance) for r in base]
        by_label = {r.label: r for r in labeled}
        for row in base:
            by_label[row.label].items.extend(
                t for t in row.items if not is_role_label(t)
            )
        base_keys = {lookup_key(t) or t.lower() for r in base for t in r.items}
        for term in clean:
            key = lookup_key(term) or term.lower()
            if key in base_keys:
                continue
            if is_generic_parent(term):
                continue
            facts = lookup_term(term)
            if facts.kind is None:
                placed = None
                if place_unknown:
                    placed = place_unknown(term, labeled)
                if placed and placed in by_label:
                    by_label[placed].items.append(term)
                else:
                    unknown_queued.append(term)
                continue
            target = next(
                (row for row in labeled if _dominant_kind(row.items) == facts.kind),
                None,
            )
            if target:
                target.items.append(term)
            else:
                unknown_queued.append(term)
        labeled = [r for r in labeled if r.items]
    elif mode == "REFINE" and base:
        passing, failing_terms = [], []
        for row in base:
            row_score = score_layout([row])
            if row_score.agreement >= 0.6 and 2 <= len(row.items) <= 10:
                passing.append(SkillCategory(row.label, list(row.items), row.provenance))
            else:
                failing_terms.extend(row.items)
        extra = [
            t for t in clean
            if (lookup_key(t) or t.lower())
            not in {lookup_key(x) or x.lower() for r in base for x in r.items}
        ]
        failing_terms.extend(extra)
        derived = group_terms(failing_terms)
        labeled = list(passing)
        for items in derived:
            labeled.append(SkillCategory("", items, "inferred"))
    else:
        grouped = group_terms(clean)
        labeled = [SkillCategory("", items, "inferred") for items in grouped]

    named: List[SkillCategory] = []
    used: List[str] = []
    for row in labeled:
        if row.label and row.provenance == "user":
            heading = row.label
        else:
            heading = name_group(row.items, base, used, namer=namer)
        if heading.lower() in BANNED_HEADINGS:
            heading = name_group(row.items, [], used, namer=None)
        if heading.lower() in BANNED_HEADINGS:
            unknown_queued.extend(row.items)
            continue
        used.append(heading)
        named.append(SkillCategory(heading, row.items, row.provenance))

    ordered = order_groups(named, mode, jd_required, base)
    placed_keys = {lookup_key(t) or t.lower() for r in ordered for t in r.items}
    for t in clean:
        key = lookup_key(t) or t.lower()
        if key not in placed_keys and not is_generic_parent(t) and t not in unknown_queued:
            unknown_queued.append(t)
    reasons = validate_layout(ordered, base)
    if reasons and base and mode != "DERIVE":
        logger.info("skill_layout: gates failed %s — shipping base", reasons)
        fallback = [
            SkillCategory(
                r.label,
                [t for t in r.items if not is_role_label(t)],
                r.provenance,
            )
            for r in base if any(not is_role_label(t) for t in r.items)
        ]
        # Still enforce language integrity on the fallback.
        langs = [t for r in fallback for t in r.items if lookup_term(t).kind == "language"]
        if langs:
            home = next(
                (r for r in fallback if _dominant_kind(r.items) == "language"),
                None,
            )
            if home:
                for row in fallback:
                    if row is home:
                        continue
                    stay = []
                    for t in row.items:
                        if lookup_term(t).kind == "language":
                            if (lookup_key(t) or t.lower()) not in {
                                lookup_key(x) or x.lower() for x in home.items
                            }:
                                home.items.append(t)
                        else:
                            stay.append(t)
                    row.items = stay
        ordered = [r for r in fallback if r.items]
        mode = "PRESERVE"
    return ComposeResult(
        skills=skills_dict(ordered),
        mode=mode,
        score=score,
        unknown_queued=unknown_queued,
    )


def apply_to_tailored(
    tailored: Dict[str, Any],
    original: Dict[str, Any],
    jd_analysis: Dict[str, Any],
    **kwargs: Any,
) -> Dict[str, Any]:
    base = layout_from_resume(original or {})
    current = tailored.get("skills") or {}
    terms: List[str] = []
    seen = set()
    for items in (current.values() if isinstance(current, dict) else []):
        if not isinstance(items, list):
            continue
        for t in items:
            key = lookup_key(t) or str(t).lower()
            if key in seen:
                continue
            seen.add(key)
            terms.append(str(t))
    if not terms:
        for row in base:
            for t in row.items:
                key = lookup_key(t) or t.lower()
                if key in seen:
                    continue
                seen.add(key)
                terms.append(t)
    result = compose(
        base,
        terms,
        jd_required=list(jd_analysis.get("required_skills") or []),
        **kwargs,
    )
    tailored["skills"] = result.skills
    tailored["_skill_layout_mode"] = result.mode
    if result.unknown_queued:
        tailored["_skill_unknown"] = result.unknown_queued
        logger.info(
            "skill_layout: queued %d unknown term(s) for registry review: %s",
            len(result.unknown_queued), result.unknown_queued,
        )
    logger.info(
        "skill_layout: mode=%s quality=%.2f cats=%d",
        result.mode, result.score.quality, len(result.skills),
    )
    return tailored
