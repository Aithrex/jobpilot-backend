"""Skill implication map (Craft-overhaul — gap-engine refinement).

A JD-required skill should count as COVERED — not a gap — when the candidate
genuinely has a skill that *provably implies* it. Canonical case: shipping
production React necessarily means writing HTML (JSX compiles to it), CSS
(styling components), and DOM work (events/refs). Those aren't a separate skill
the candidate lacks; they're the substrate of the skill they already have.

Design rules (do not relax — over-promotion puts undefendable claims on a
resume):
  1. Edges are DIRECTED and NON-SYMMETRIC. Every SOLID edge below is a lie if
     reversed (Docker does NOT imply Kubernetes; JavaScript does NOT imply
     TypeScript). The forbidden-edge set guards the common reverse traps.
  2. Only the SOLID tier auto-covers. "Borderline" skills (defensible in an
     interview but not guaranteed) stay gaps — match_gap_reporter already tiers
     those as "defensible".
  3. Imply CONCEPTS, not named tools, where the tool varies (imply "CI/CD" and
     "unit testing", not "pytest" or "the specific runner").

The bar for a SOLID edge: you cannot produce the parent skill in production
without literally producing the child.
"""
from __future__ import annotations

from typing import Dict, List, Set

from utils.keyword_normalizer import normalize_single

# SOLID tier — safe to auto-count as covered when the candidate has the parent.
# Keys/values are canonical forms (normalize_single is applied at lookup time so
# aliases on either side resolve).
SKILL_IMPLICATIONS_SOLID: Dict[str, List[str]] = {
    # --- Frontend ---
    "React": ["JavaScript", "HTML", "CSS", "DOM"],
    "Angular": ["TypeScript", "HTML", "CSS", "DOM"],
    "Vue": ["JavaScript", "HTML", "CSS", "DOM"],
    # --- Backend frameworks ---
    "Spring Boot": ["Java", "Spring"],
    "Django": ["Python", "SQL"],
    "FastAPI": ["Python"],
    "Express": ["Node.js", "JavaScript"],
    "Rails": ["Ruby", "SQL"],
    # --- ORMs / data ---
    "Hibernate": ["JPA", "SQL"],
    "PostgreSQL": ["SQL"],
    "MySQL": ["SQL"],
    "SQL Server": ["SQL"],
    "Oracle": ["SQL"],
    # --- Containers / orchestration (one-directional: K8s -> Docker, NEVER reverse) ---
    "Kubernetes": ["Docker", "YAML"],
    "Helm": ["Kubernetes", "YAML"],
    # --- Languages -> ecosystem ---
    "TypeScript": ["JavaScript"],
    # --- Cloud ---
    "EKS": ["Kubernetes", "AWS", "Docker", "YAML"],
    "ECS": ["AWS", "Docker"],
    "Lambda": ["AWS"],
    # --- Testing ---
    "TDD": ["Unit Testing"],
    # --- CI/CD ---
    "GitHub Actions": ["CI/CD", "YAML", "Git"],
    "GitLab CI": ["CI/CD", "YAML", "Git"],
    "CircleCI": ["CI/CD", "YAML"],
    "Jenkins": ["CI/CD"],  # deliberately NOT YAML — Jenkins is Groovy/UI-driven
    # --- Observability ---
    "Datadog": ["Monitoring"],
    "Grafana": ["Monitoring"],
    "Prometheus": ["Monitoring"],

    # --- Finance & Accounting (Phase 3B) ---
    # Doing FP&A in production necessarily involves variance analysis and
    # forecasting; a CPA has demonstrably worked in GAAP; the monthly close is
    # reconciliation-heavy by definition.
    "FP&A": ["Variance Analysis", "Forecasting", "Financial Analysis"],
    "CPA": ["GAAP", "Financial Reporting"],
    "Financial Modeling": ["Excel", "Forecasting"],
    "Month-End Close": ["Reconciliation", "Financial Reporting"],
    "Auditing": ["GAAP"],

    # --- Healthcare & Nursing (Phase 3B) ---
    # Critical-care/ICU nursing necessarily involves patient assessment and
    # telemetry; ACLS builds on BLS (you can't hold ACLS without the basics);
    # any bedside RN documents care and administers meds.
    "Critical Care": ["Patient Assessment", "Telemetry", "Patient Care"],
    "ACLS": ["BLS"],
    "Registered Nurse": ["Patient Assessment", "Medication Administration",
                         "Clinical Documentation"],
    "Epic": ["Electronic Health Records"],
    "Cerner": ["Electronic Health Records"],

    # --- Marketing (Phase 3B) ---
    # Running Google Ads IS paid search / SEM; CRO is defined by A/B testing;
    # SEM practitioners work in keyword/paid-search mechanics.
    "Google Ads": ["SEM", "PPC"],
    "Conversion Rate Optimization": ["A/B Testing"],
    "SEM": ["PPC"],
    "Marketing Automation": ["Email Marketing"],

    # --- Sales (Phase 3B) ---
    # Salesforce IS a CRM; pipeline management is inseparable from prospecting/
    # qualification; an AE by definition does account management + negotiation.
    "Salesforce": ["CRM"],
    "Pipeline Management": ["Prospecting", "Lead Qualification"],
    "Account Executive": ["Account Management", "Negotiation"],

    # --- Data analytics (domain expansion) ---
    # Building dashboards IS data visualization; DAX only exists inside Power BI;
    # any analyst querying data works in SQL.
    "Dashboarding": ["Data Visualization"],
    "DAX": ["Power BI"],
    "SQL Reporting": ["SQL"],
    "Cohort Analysis": ["Statistical Analysis"],

    # --- Cybersecurity (domain expansion) ---
    # SIEM tooling IS security monitoring; running a SOC involves incident
    # response; CISSP holders have demonstrable risk-management exposure.
    "SIEM Tools": ["Threat Detection", "Network Security"],
    "Security Operations Center": ["Incident Response", "Threat Detection"],
    "CISSP": ["Risk Assessment", "Security Frameworks"],
    "Penetration Testing": ["Vulnerability Management"],

    # --- IT / systems (domain expansion) ---
    # ITIL IS IT service management; managing endpoints/AD is system admin work.
    "ITIL": ["Help Desk Support"],
    "System Administration": ["Network Administration"],
    "Endpoint Management": ["System Administration"],

    # --- Product management (domain expansion) ---
    # A PRD is the artifact of requirements work; a roadmap implies strategy;
    # product analytics tools imply the analytics practice.
    "Product Requirements": ["User Stories"],
    "Product Roadmap": ["Product Strategy"],
    "Product Analytics": ["A/B Experimentation"],

    # --- Construction (domain expansion) ---
    # Primavera P6 IS project scheduling; estimating implies takeoff; Procore is
    # construction project management software.
    "Primavera P6": ["Project Scheduling"],
    "Cost Estimating": ["Project Scheduling"],
    "OSHA Compliance": ["Building Codes"],

    # --- Engineering disciplines (domain expansion) ---
    # FEA runs inside CAD/simulation tools; PLC work implies controls; a PE
    # license implies the FE/EIT prerequisite path.
    "Finite Element Analysis": ["Structural Analysis"],
    "Civil 3D": ["AutoCAD"],
    "PE License": ["Structural Analysis"],

    # --- Project/Program management (domain expansion) ---
    # Scrum mastery IS agile practice; a PMP implies risk + budget discipline.
    "Scrum": ["Agile"],
    "SAFe": ["Agile", "Scrum"],
    "PMP": ["Risk Management", "Budget Management"],

    # --- Consulting (domain expansion) ---
    # Management consulting implies market analysis + strategy; change management
    # is inseparable from stakeholder work.
    "Management Consulting": ["Market Analysis", "Business Strategy"],
    "Change Management": ["Stakeholder Management"],

    # --- HR (domain expansion) ---
    # Full-cycle talent acquisition IS sourcing.
    "Talent Acquisition": ["Sourcing"],
}

# HARD NEGATIVES — never infer these, even transitively. Guards the reverse
# traps and non-subset pairs. Stored as normalized (parent, child) lowercased.
_FORBIDDEN_RAW = [
    ("React", "TypeScript"), ("JavaScript", "TypeScript"),
    ("Docker", "Kubernetes"), ("Containers", "Kubernetes"),
    ("SQL", "PostgreSQL"), ("PostgreSQL", "MySQL"), ("MySQL", "PostgreSQL"),
    ("ECS", "Kubernetes"), ("ECS", "EKS"),
    ("AWS", "Azure"), ("AWS", "GCP"),
    ("Unit Testing", "TDD"),
    ("Grafana", "Prometheus"),
    ("Jenkins", "YAML"),
    ("React", "Redux"), ("Vue", "Vuex"),
    ("Spring Boot", "Hibernate"), ("JPA", "Hibernate"),
    ("Kafka", "RabbitMQ"),
    # --- Finance (reverse traps / non-subsets) ---
    ("GAAP", "IFRS"), ("IFRS", "GAAP"),        # different standards, not subsets
    ("GAAP", "CPA"),                            # knowing GAAP != being a CPA
    ("Excel", "Financial Modeling"),            # Excel doesn't imply modeling
    ("Variance Analysis", "FP&A"),              # reverse of a valid edge
    # --- Healthcare (reverse traps) ---
    ("BLS", "ACLS"),                            # BLS is the prerequisite, not the reverse
    ("Patient Assessment", "Registered Nurse"), # skill != the license
    ("Electronic Health Records", "Epic"),      # EHR != a specific vendor
    ("Epic", "Cerner"), ("Cerner", "Epic"),     # different systems
    # --- Marketing (reverse traps) ---
    ("A/B Testing", "Conversion Rate Optimization"),  # reverse of a valid edge
    ("PPC", "Google Ads"),                      # PPC != a specific platform
    ("SEM", "SEO"), ("SEO", "SEM"),             # organic vs paid, not subsets
    # --- Sales (reverse traps) ---
    ("CRM", "Salesforce"),                      # CRM != a specific vendor
    ("Account Management", "Account Executive"),
]


import re

# Qualifier noise JD writers append that the alias normalizer doesn't strip:
# version pins ("Java 8+", "React 18"), parenthetical qualifiers
# ("JavaScript (Vanilla)"), and generic-skill suffixes ("DOM Manipulation",
# "Client-side Debugging"). Stripping these lets implication lookups see the
# real skill token. Applied ONLY for implication matching, never mutating the
# displayed keyword.
_QUALIFIER_SUFFIXES = (
    " manipulation", " development", " debugging", " programming",
    " fundamentals", " basics", " concepts",
)


def _strip_qualifiers(s: str) -> str:
    s = re.sub(r"\(.*?\)", "", s)          # drop "(vanilla)" etc.
    s = re.sub(r"\s*\d+(\.\d+)?\+?\s*$", "", s)  # drop trailing "8+", "18", "3.11"
    s = s.strip()
    for suf in _QUALIFIER_SUFFIXES:
        if s.endswith(suf):
            s = s[: -len(suf)].strip()
            break
    # "vanilla javascript" / "javascript vanilla" -> "javascript"
    s = s.replace("vanilla", "").strip()
    return re.sub(r"\s{2,}", " ", s)


def _canon(skill: str) -> str:
    raw = (skill or "").strip()
    # Try the alias normalizer first; if it leaves the token unchanged, retry on
    # a qualifier-stripped form so "Java 8+" / "DOM Manipulation" resolve.
    norm = normalize_single(raw)
    if norm == raw:
        stripped = _strip_qualifiers(raw.lower())
        if stripped and stripped != raw.lower():
            norm = normalize_single(stripped)
    return norm.strip().lower()


# Precompute a normalized adjacency map: parent_canon -> {child_canon, ...}.
_IMPLIES: Dict[str, Set[str]] = {}
for _parent, _children in SKILL_IMPLICATIONS_SOLID.items():
    _pc = _canon(_parent)
    _IMPLIES.setdefault(_pc, set()).update(_canon(c) for c in _children)

_FORBIDDEN: Set = {(_canon(p), _canon(c)) for p, c in _FORBIDDEN_RAW}

# Bounded transitive closure (e.g. EKS -> Kubernetes -> Docker/YAML). Depth is
# capped and every hop is re-checked against the forbidden set, so transitivity
# can never manufacture a forbidden edge.
_MAX_DEPTH = 2


def _closure(parent_canon: str) -> Set[str]:
    seen: Set[str] = set()
    frontier = {parent_canon}
    for _ in range(_MAX_DEPTH):
        nxt: Set[str] = set()
        for node in frontier:
            for child in _IMPLIES.get(node, set()):
                if (parent_canon, child) in _FORBIDDEN:
                    continue
                if child not in seen and child != parent_canon:
                    seen.add(child)
                    nxt.add(child)
        frontier = nxt
        if not frontier:
            break
    return seen


def implied_skills(owned_skill: str) -> Set[str]:
    """Canonical-lowercase skills that `owned_skill` provably implies (SOLID)."""
    return _closure(_canon(owned_skill))


def is_implied(target: str, owned_skills) -> bool:
    """True if any owned skill provably implies `target` (and it's not forbidden).

    `owned_skills` is an iterable of skill strings the candidate genuinely has.
    """
    tgt = _canon(target)
    if not tgt:
        return False
    for owned in owned_skills:
        oc = _canon(owned)
        if not oc or oc == tgt:
            continue
        if (oc, tgt) in _FORBIDDEN:
            continue
        if tgt in _closure(oc):
            return True
    return False
