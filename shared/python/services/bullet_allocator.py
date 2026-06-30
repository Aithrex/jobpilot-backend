"""
Bullet allocator — fills the one-page-at-10pt budget with the most relevant
bullets instead of hard-coded per-slot counts.

Background
----------
The tailoring pipeline used to hard-code bullet counts by *position* (most-recent
role = 5 bullets, others = 3; 1 project = 5 bullets, 2-3 = 3 each). Those counts
were a layout hack to keep the resume on one page — they optimized for *filling*
the page, not for surfacing the most JD-relevant content. A thin-but-relevant
role couldn't borrow space from an over-full project, and hitting a fixed count
forced either padding (filler) or truncation (dropping a strong bullet).

This module replaces that with relevance-driven allocation:

  * The tailor model now emits MORE candidate bullets than will fit, ordered
    best-first (most JD-relevant / highest-impact first).
  * `allocate_bullets` greedily fills the page budget from the top of those
    ranked lists, re-measuring page height after each add via the renderer's
    existing `measure_only` pass — so "fits at 10pt" is *proven* per resume,
    never assumed.

Guardrails
----------
  * Floor of 2 bullets per EXPERIENCE role (when the model supplied at least
    that many) so a real job never looks empty. PROJECTS get NO reserved floor —
    work experience always outweighs projects, so project bullets earn page
    space only by competing in the greedy fill against (and after) experience.
    A project appears on the page only when its bullets win the space; a project
    that wins zero bullets is dropped entirely (no empty project header).
  * Recency bonus: ties break toward the most-recent role (experience arrives
    date-ordered, so index 0 = most recent).
  * Experience-over-projects: experience floors are locked first and every
    project bullet is a fill candidate, so project content can never displace an
    experience bullet — it only fills space experience didn't claim.

The default scorer is JD-aware: it ranks each bullet by how many distinct JD
terms it covers and whether it carries a quantified metric, then blends that
with the model's best-first order and the guardrail biases. It reuses the same
alias-aware matching the keyword gap analysis uses (utils.keyword_normalizer),
so "K8s" in a bullet matches "Kubernetes" in the JD. Scoring stays pluggable via
the `scorer` hook for callers that want a different policy.
"""
import logging
import re
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# A "candidate" is one bullet the model proposed, tagged with where it came from
# and its rank within that section's list.
#   section: "experience" | "project"
#   entry_idx: index of the role/project in its array (0 = most recent role)
#   rank: position within that entry's bullet list (0 = model's top pick)
Candidate = Dict[str, Any]

# Scorer signature: scorer(candidate, context) -> float. Higher = sooner.
# `context` carries precomputed JD term forms so the scorer stays O(1) per call.
Scorer = Callable[[Candidate, Dict[str, Any]], float]

# --- Guardrail constants -------------------------------------------------
_FLOOR_PER_ENTRY = 2          # min bullets kept per role / project (if supplied)
_RECENCY_BONUS = 0.25         # tie-break weight nudging the most-recent role up
_EXPERIENCE_PRIORITY = 1.0    # experience outranks projects when scores tie
# Hard ceilings matching what the model is asked to emit — defensive only.
_MAX_EXP_BULLETS = 6
_MAX_PROJ_BULLETS = 5

# --- Relevance scoring weights -------------------------------------------
# A bullet's score is dominated by JD coverage, with quality and model-rank as
# secondary signals. Required skills count double weight vs. general keywords.
_W_REQUIRED = 2.0     # per distinct JD *required_skill* the bullet covers
_W_KEYWORD = 1.0      # per distinct JD *keyword* the bullet covers
_W_METRIC = 0.75      # bullet contains a quantified metric (number/%/duration)
_W_MODEL_RANK = 0.4   # decay per position down the model's best-first list

# Detects a quantified claim: percentages, plain numbers, multipliers (2x),
# durations, or dollar amounts — the markers of an interview-defensible bullet.
_METRIC_RE = re.compile(
    r"(\d+(?:\.\d+)?\s*%|\$\s*\d|\b\d+\s*x\b|\b\d{2,}\b|\b\d+\s*"
    r"(?:ms|s|sec|min|hours?|days?|weeks?|months?|k|m|bn?)\b)",
    re.IGNORECASE,
)


def _build_jd_context(jd: Dict[str, Any]) -> Dict[str, Any]:
    """Precompute alias-expanded JD term forms once, reused for every bullet.

    Returns {"required": [(canonical, [forms...]), ...], "keywords": [...]}.
    Uses utils.keyword_normalizer (same alias map the keyword gap engine uses)
    so a bullet's "K8s"/"CI/CD" matches the JD's "Kubernetes"/"CI/CD".
    """
    from utils.keyword_normalizer import get_all_forms, normalize_single

    def expand(terms: List[str]) -> List[Tuple[str, List[str]]]:
        out: List[Tuple[str, List[str]]] = []
        seen: Set[str] = set()
        for t in terms or []:
            canon = (normalize_single(t) or t).lower()
            if canon in seen:
                continue
            seen.add(canon)
            forms = [f.lower() for f in get_all_forms(t) if f]
            out.append((canon, forms or [canon]))
        return out

    return {
        "required": expand(jd.get("required_skills")),
        "keywords": expand(jd.get("keywords")),
    }


def _text_contains(text_lower: str, form: str) -> bool:
    """Word-boundary-aware match (mirrors KeywordGapEngine._text_contains)."""
    if not form:
        return False
    if len(form) <= 2:
        return form in text_lower
    return re.search(r"(?<![a-zA-Z])" + re.escape(form) + r"(?![a-zA-Z])",
                     text_lower) is not None


def _count_covered(text_lower: str, expanded: List[Tuple[str, List[str]]]) -> int:
    """Number of distinct JD terms whose any alias appears in the bullet."""
    n = 0
    for _canon, forms in expanded:
        if any(_text_contains(text_lower, f) for f in forms):
            n += 1
    return n


def _default_scorer(cand: Candidate, ctx: Dict[str, Any]) -> float:
    """JD-aware relevance score: coverage + quality + model rank + guardrails.

    Higher = allocated sooner. The dominant term is JD coverage (required skills
    weighted above general keywords); a quantified-metric bonus rewards the
    strongest bullets; the model's best-first rank breaks near-ties; experience
    and recency biases preserve the guardrail intent.
    """
    text_lower = cand["text"].lower()
    jd = ctx.get("jd_terms", {})

    covered_req = _count_covered(text_lower, jd.get("required", []))
    covered_kw = _count_covered(text_lower, jd.get("keywords", []))
    has_metric = bool(_METRIC_RE.search(cand["text"]))

    score = (
        _W_REQUIRED * covered_req
        + _W_KEYWORD * covered_kw
        + (_W_METRIC if has_metric else 0.0)
        - _W_MODEL_RANK * cand["rank"]   # model's order as a soft tiebreaker
    )
    if cand["section"] == "experience":
        score += _EXPERIENCE_PRIORITY
        if cand["entry_idx"] == 0:       # most-recent role
            score += _RECENCY_BONUS
    return score


def _collect_candidates(tailored: Dict[str, Any]) -> List[Candidate]:
    """Flatten every proposed bullet into a ranked candidate list."""
    out: List[Candidate] = []
    for ei, exp in enumerate(tailored.get("experience") or []):
        if not isinstance(exp, dict):
            continue
        bullets = [b for b in (exp.get("bullets") or []) if str(b).strip()]
        for rank, text in enumerate(bullets[:_MAX_EXP_BULLETS]):
            out.append({"section": "experience", "entry_idx": ei,
                        "rank": rank, "text": text})
    for pi, proj in enumerate(tailored.get("projects") or []):
        if not isinstance(proj, dict):
            continue
        bullets = [b for b in (proj.get("bullets") or []) if str(b).strip()]
        for rank, text in enumerate(bullets[:_MAX_PROJ_BULLETS]):
            out.append({"section": "project", "entry_idx": pi,
                        "rank": rank, "text": text})
    return out


def _apply_selection(
    tailored: Dict[str, Any],
    selected: Dict[Tuple[str, int], List[str]],
) -> Dict[str, Any]:
    """Write the chosen bullets back, preserving model order within each entry."""
    for ei, exp in enumerate(tailored.get("experience") or []):
        if isinstance(exp, dict):
            exp["bullets"] = selected.get(("experience", ei), [])
    for pi, proj in enumerate(tailored.get("projects") or []):
        if isinstance(proj, dict):
            proj["bullets"] = selected.get(("project", pi), [])
    return tailored


def _measure(renderer, tailored: Dict[str, Any]) -> float:
    """Measured content height (mm) at the 10pt base, minimum gaps.

    Mirrors the knobs `ResumeRenderer.generate_pdf` uses for its Pass-1
    measurement so the allocator's fit decision matches the real render.
    """
    _, height = renderer._render_pdf(
        tailored,
        section_gap=renderer._MIN_SECTION_GAP,
        entry_gap=renderer._MIN_ENTRY_GAP,
        post_header=renderer._MIN_POST_HEADER,
        header_gap=renderer._MIN_HEADER_GAP,
        skill_gap=renderer._MIN_SKILL_GAP,
        body_size=10.0, lh=3.10, lh_s=3.08,  # densest 10pt tier = max capacity
        measure_only=True,
    )
    return height


def allocate_bullets(
    tailored: Dict[str, Any],
    renderer,
    *,
    jd_analysis: Optional[Dict[str, Any]] = None,
    scorer: Optional[Scorer] = None,
) -> Dict[str, Any]:
    """Select the page-fitting subset of model-proposed bullets by relevance.

    Greedy fill: seed every entry with its floor (top-ranked bullets), then add
    the next-highest-scored remaining candidate, re-measuring after each add,
    stopping before the first overflow. Deterministic and order-stable.

    The 10pt densest tier (lh 3.10) is used as the capacity yardstick so we
    never select a set that would force the renderer below 10pt. The renderer's
    own two-pass logic then relaxes line-height to spread the final layout.
    """
    jd = jd_analysis or {}
    score_fn = scorer or _default_scorer
    # Precompute alias-expanded JD terms once; the scorer reads them per call.
    ctx = {"jd_terms": _build_jd_context(jd)}

    def score(c: Candidate) -> float:
        return score_fn(c, ctx)

    candidates = _collect_candidates(tailored)
    if not candidates:
        return tailored

    avail = renderer._AVAIL_H

    # Group candidates by entry. Within an entry, order by relevance SCORE (not
    # the raw model rank) so the floor keeps each entry's most JD-relevant
    # bullets — the model's order survives only as a tiebreaker inside the score.
    by_entry: Dict[Tuple[str, int], List[Candidate]] = {}
    for c in candidates:
        by_entry.setdefault((c["section"], c["entry_idx"]), []).append(c)
    for lst in by_entry.values():
        lst.sort(key=score, reverse=True)

    # --- Seed: floor bullets for EXPERIENCE ONLY (cannot be dropped) ---
    # Projects get no reserved floor — their bullets all go into `remaining` and
    # compete in the greedy fill, so work experience always wins page space first.
    selected: Dict[Tuple[str, int], List[str]] = {}
    remaining: List[Candidate] = []
    for key, lst in by_entry.items():
        section, _idx = key
        if section == "experience":
            floor = lst[:_FLOOR_PER_ENTRY]
            selected[key] = [c["text"] for c in floor]
            remaining.extend(lst[_FLOOR_PER_ENTRY:])
        else:
            selected[key] = []           # no project floor
            remaining.extend(lst)        # every project bullet competes

    # If even the experience floors overflow, drop the lowest-scored experience
    # floor bullets until it fits. Never go below 1 bullet per role.
    _apply_selection(tailored, selected)
    if _measure(renderer, tailored) > avail:
        droppable = sorted(
            (c for key in selected for c in by_entry[key]
             if c["text"] in selected[key]),
            key=score,  # lowest-scored floor bullet dropped first
        )
        for c in droppable:
            key = (c["section"], c["entry_idx"])
            if len(selected[key]) <= 1:
                continue  # keep at least 1 per role
            selected[key].remove(c["text"])
            _apply_selection(tailored, selected)
            if _measure(renderer, tailored) <= avail:
                break

    # --- Greedy fill: add remaining candidates by score until next add overflows ---
    # `remaining` holds extra experience bullets AND all project bullets. The
    # scorer's +_EXPERIENCE_PRIORITY keeps experience ahead of equally-relevant
    # project bullets, so projects fill only the space experience leaves behind.
    remaining.sort(key=score, reverse=True)
    for c in remaining:
        key = (c["section"], c["entry_idx"])
        # Re-insert keeping the entry's relevance order (by_entry is score-sorted).
        trial = selected[key] + [c["text"]]
        ordered = [b["text"] for b in by_entry[key] if b["text"] in trial]
        prev = selected[key]
        selected[key] = ordered
        _apply_selection(tailored, selected)
        if _measure(renderer, tailored) > avail:
            selected[key] = prev  # revert this add
            _apply_selection(tailored, selected)
            # Keep trying lower-scored candidates — a shorter bullet elsewhere
            # may still fit even after a long one didn't.
            continue

    # --- Drop projects that won zero bullets (no empty project headers) ---
    projects = tailored.get("projects") or []
    if projects:
        kept_projects = [
            p for pi, p in enumerate(projects)
            if isinstance(p, dict) and selected.get(("project", pi))
        ]
        if len(kept_projects) != len(projects):
            logger.info(
                "Bullet allocation: dropped %d project(s) that won no bullets",
                len(projects) - len(kept_projects),
            )
            tailored["projects"] = kept_projects

    counts = {k: len(v) for k, v in selected.items()}
    logger.info("Bullet allocation: %s (avail %.1fmm)", counts, avail)
    return tailored
