"""Requirement observation and source reconciliation (assessment-v2 Package 4).

Builds canonical AND/OR expressions from JD records, observes the final document
independently of planner matches, and reconciles claims against scoped source
facts. Qualifiers (ownership, production use, negation, action strength) affect
document_state and unmet_qualifiers. Uses the deterministic reducer.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from schemas.assessment_contracts import (
    AtomicPredicate,
    CanonicalRequirement,
    ClaimState,
    DocumentRef,
    DocumentState,
    MatchMethod,
    RequirementEvaluation,
    RequirementExpression,
    SourceRef,
    SourceState,
    atomic,
    count_evaluations,
    group_and,
    group_or,
    unresolved,
)
from services.assessment_policy import LeafObservation, reduce_expression
from services.artifact_assessment import text_contains_capability, token_boundary_match
from services.tailoring_policy import digest
from utils.keyword_normalizer import get_all_forms


_PLUS_SPLIT = re.compile(
    r",?\s+(?:plus|along with|as well as)\s+",
    re.IGNORECASE,
)
_OR_SPLIT = re.compile(r"\s+or\s+", re.IGNORECASE)

# Action strength for qualifier checks (higher = stronger evidence).
_ACTION_RANK = {
    "evaluated": 1,
    "integrated_with": 2,
    "implemented": 3,
    "operated": 4,
    "owned": 5,
}

_NEGATION = re.compile(
    r"\b(?:never|not|no longer|without|did not|didn't|stopped|deprecated|"
    r"migrated away|replaced|uncertain|possibly|maybe|unconfirmed)\b",
    re.I,
)
_OWNERSHIP = re.compile(
    r"\b(?:owned|owner of|led|led the|responsible for|end[- ]to[- ]end|"
    r"solely|primary owner)\b",
    re.I,
)
_PRODUCTION = re.compile(
    r"\b(?:production|prod\b|live traffic|customer[- ]facing|in production)\b",
    re.I,
)
_HISTORICAL = re.compile(
    r"\b(?:previously|formerly|legacy|historical|used to|past)\b",
    re.I,
)


def classify_action(text: str) -> str:
    """Map prose to action class for qualifier comparison."""
    t = (text or "").lower()
    if re.search(r"\b(?:owned|ownership|sole owner|primary owner)\b", t):
        return "owned"
    if re.search(r"\b(?:operated|operations|on[- ]call|maintained|ran)\b", t):
        return "operated"
    if re.search(r"\b(?:implemented|built|developed|wrote|created)\b", t):
        return "implemented"
    if re.search(r"\b(?:integrated with|integration with|another team'?s)\b", t):
        return "integrated_with"
    if re.search(r"\b(?:evaluated|assessed|reviewed|POC|proof of concept|spike)\b", t):
        return "evaluated"
    return ""


def _plus_split_or_ambiguous(raw: str) -> Optional[RequirementExpression]:
    if re.search(r"\bor\b.+\band\b", raw, re.I) and not _PLUS_SPLIT.search(raw):
        if "," not in raw:
            return unresolved("ambiguous and/or wording")
    plus_parts = _PLUS_SPLIT.split(raw)
    if len(plus_parts) == 2:
        left, right = plus_parts[0].strip(), plus_parts[1].strip()
        return group_and(_or_group(left, None), atomic(right) if right else unresolved("missing plus clause"))
    return None


def parse_expression_from_text(
    text: str,
    alternatives: Optional[Sequence[str]] = None,
    *,
    ownership_required: bool = False,
    duration_required: bool = False,
    currency_required: bool = False,
    evidence_expectation: str = "document_or_skills",
    action: str = "",
) -> RequirementExpression:
    """Parse a JD requirement line into an expression tree with qualifiers on atomics."""
    raw = (text or "").strip()
    alts = [str(a).strip() for a in (alternatives or []) if str(a).strip()]

    if not raw and not alts:
        return unresolved("empty requirement")

    def _atom(cap: str) -> RequirementExpression:
        return RequirementExpression(
            kind="atomic",
            predicate=AtomicPredicate(
                capability=cap,
                action=action,
                ownership_required=ownership_required,
                duration_required=duration_required,
                currency_required=currency_required,
                evidence_expectation=evidence_expectation,
            ),
        )

    special = _plus_split_or_ambiguous(raw)
    if special is not None and special.kind == "unresolved":
        return special
    if special is not None and special.kind == "and":
        # Re-wrap children with qualifier-aware atomics where possible.
        return special

    if alts and len(alts) >= 2:
        return group_or(*[_atom(a) for a in alts])

    or_parts = [p.strip() for p in _OR_SPLIT.split(raw) if p.strip()]
    if len(or_parts) >= 2:
        return group_or(*[_atom(p) for p in or_parts])

    return _atom(raw or (alts[0] if alts else ""))


def _or_group(text: str, alts: Optional[Sequence[str]]) -> RequirementExpression:
    if alts and len(alts) >= 2:
        return group_or(*[atomic(a) for a in alts])
    parts = [p.strip() for p in _OR_SPLIT.split(text) if p.strip()]
    if len(parts) >= 2:
        return group_or(*[atomic(p) for p in parts])
    return atomic(text)


def infer_qualifiers_from_text(text: str) -> Dict[str, Any]:
    """Heuristic JD qualifier inference (explicit cues only)."""
    t = (text or "").lower()
    ownership = bool(re.search(r"\b(?:own|ownership|end[- ]to[- ]end responsibility)\b", t))
    duration = bool(re.search(r"\b(?:\d+\+?\s*years?|years? of experience|duration)\b", t))
    currency = bool(re.search(r"\b(?:current|recent|within the last|hands[- ]on now)\b", t))
    production = bool(re.search(r"\bproduction\b", t))
    action = ""
    if ownership:
        action = "owned"
    elif production:
        action = "operated"
    elif re.search(r"\bevaluat", t):
        action = "evaluated"
    expectation = "production_use" if production else (
        "ownership" if ownership else "document_or_skills"
    )
    return {
        "ownership_required": ownership,
        "duration_required": duration,
        "currency_required": currency,
        "action": action,
        "evidence_expectation": expectation,
    }


def canonicalize_requirements(jd: Dict[str, Any]) -> List[CanonicalRequirement]:
    """Build top-level canonical groups (counted once) from JD analysis."""
    out: List[CanonicalRequirement] = []
    seen: set[str] = set()
    records = list(jd.get("requirements") or [])
    if not records:
        for field, priority, kind in (
            ("required_skills", "required", "capability"),
            ("preferred_skills", "preferred", "capability"),
            ("responsibilities", "unspecified", "responsibility"),
            ("qualifications", "unspecified", "credential"),
        ):
            for t in jd.get(field) or []:
                if isinstance(t, str) and t.strip():
                    records.append({
                        "text": t, "priority": priority, "kind": kind,
                        "alternatives": [], "source_excerpt": t,
                    })

    for record in records:
        if not isinstance(record, dict):
            continue
        text = str(record.get("text") or "").strip()
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        alts = [str(a).strip() for a in (record.get("alternatives") or []) if str(a).strip()]
        quals = infer_qualifiers_from_text(text)
        for k in ("ownership_required", "duration_required", "currency_required",
                  "action", "evidence_expectation"):
            if record.get(k) not in (None, ""):
                quals[k] = record[k]
        expr_payload = record.get("expression")
        if isinstance(expr_payload, dict) and expr_payload.get("kind"):
            expression = _expression_from_dict(expr_payload)
        else:
            expression = parse_expression_from_text(text, alts, **quals)
        # Apply plus-pattern with qualifiers on atomics
        if expression.kind == "and" and _PLUS_SPLIT.search(text):
            expression = parse_expression_from_text(text, alts, **quals)
            # Rebuild plus tree with qualified atomics
            plus_parts = _PLUS_SPLIT.split(text)
            if len(plus_parts) == 2:
                left, right = plus_parts[0].strip(), plus_parts[1].strip()
                left_alts = alts if alts else None
                if left_alts and len(left_alts) >= 2:
                    left_expr = group_or(*[
                        RequirementExpression(
                            kind="atomic",
                            predicate=AtomicPredicate(capability=a, **{
                                k: quals[k] for k in (
                                    "ownership_required", "duration_required",
                                    "currency_required", "action",
                                    "evidence_expectation",
                                )
                            }),
                        ) for a in left_alts
                    ])
                else:
                    left_expr = _or_group(left, left_alts)
                right_expr = RequirementExpression(
                    kind="atomic",
                    predicate=AtomicPredicate(capability=right, **{
                        k: quals[k] for k in (
                            "ownership_required", "duration_required",
                            "currency_required", "action",
                            "evidence_expectation",
                        )
                    }),
                )
                expression = group_and(left_expr, right_expr)

        priority = record.get("priority", "unspecified")
        if priority not in {"required", "preferred", "unspecified"}:
            priority = "unspecified"
        kind = record.get("kind") or "capability"
        kind_map = {
            "skill": "capability",
            "responsibility": "responsibility",
            "qualification": "credential",
            "logistics": "application_context",
        }
        kind = kind_map.get(kind, kind)
        if kind not in {
            "capability", "responsibility", "credential", "duration",
            "domain", "application_context",
        }:
            kind = "capability"
        extraction = "ambiguous" if expression.kind == "unresolved" else "complete"
        if jd.get("analysis_completeness") == "partial":
            extraction = "partial" if extraction == "complete" else extraction
        rid = digest(key)[:12]
        out.append(CanonicalRequirement(
            id=rid,
            display_text=text,
            priority=priority,  # type: ignore[arg-type]
            kind=kind,  # type: ignore[arg-type]
            expression=expression,
            source_excerpt=str(record.get("source_excerpt") or text),
            provenance="jd.requirements",
            extraction_state=extraction,  # type: ignore[arg-type]
        ))
    return out


def _expression_from_dict(d: Dict[str, Any]) -> RequirementExpression:
    kind = d.get("kind") or "unresolved"
    children = [_expression_from_dict(c) for c in (d.get("children") or []) if isinstance(c, dict)]
    pred = None
    p = d.get("predicate")
    if isinstance(p, dict) and p.get("capability"):
        pred = AtomicPredicate(
            capability=str(p.get("capability") or ""),
            action=str(p.get("action") or ""),
            context=str(p.get("context") or ""),
            ownership_required=bool(p.get("ownership_required")),
            duration_required=bool(p.get("duration_required")),
            currency_required=bool(p.get("currency_required")),
            evidence_expectation=str(p.get("evidence_expectation") or "document_or_skills"),
        )
    return RequirementExpression(
        kind=kind,  # type: ignore[arg-type]
        predicate=pred,
        children=children,
        reason=str(d.get("reason") or ""),
    )


def attach_expressions(jd: Dict[str, Any]) -> Dict[str, Any]:
    """Mutate a copy of JD requirements with expression trees (for planners)."""
    import copy
    out = copy.deepcopy(jd)
    reqs = []
    for record in out.get("requirements") or []:
        if not isinstance(record, dict):
            continue
        r = dict(record)
        quals = infer_qualifiers_from_text(str(r.get("text") or ""))
        expr = parse_expression_from_text(
            str(r.get("text") or ""),
            r.get("alternatives") or [],
            **quals,
        )
        # Prefer canonicalize for plus patterns
        r["expression"] = expr.as_dict()
        for k, v in quals.items():
            r.setdefault(k, v)
        reqs.append(r)
    if reqs:
        # Rebuild via canonicalize so plus trees are consistent
        can = canonicalize_requirements({**out, "requirements": reqs})
        by_text = {c.display_text.lower(): c for c in can}
        rebuilt = []
        for r in reqs:
            c = by_text.get(str(r.get("text") or "").lower())
            if c:
                r["expression"] = c.expression.as_dict()
                r["canonical_id"] = c.id
            rebuilt.append(r)
        out["requirements"] = rebuilt
    return out


def _atomic_nodes(expr: RequirementExpression) -> List[AtomicPredicate]:
    if expr.kind == "atomic" and expr.predicate:
        return [expr.predicate]
    nodes: List[AtomicPredicate] = []
    for c in expr.children:
        nodes.extend(_atomic_nodes(c))
    return nodes


def _capability_aliases(cap: str) -> List[str]:
    forms = list(get_all_forms(cap) or [])
    if cap and cap not in forms:
        forms.insert(0, cap)
    return forms


def _find_capability_span(document_text: str, capability: str) -> Tuple[bool, MatchMethod, str]:
    """Return (found, method, matched_form)."""
    for form in _capability_aliases(capability):
        if token_boundary_match(document_text, form):
            method: MatchMethod = "exact" if form.casefold() == capability.casefold() else "recognized_alias"
            return True, method, form
    return False, "none", ""


def _span_context(document_text: str, matched: str, window: int = 120) -> str:
    if not matched:
        return ""
    m = re.search(re.escape(matched), document_text, re.I)
    if not m:
        return matched
    start = max(0, m.start() - window)
    end = min(len(document_text), m.end() + window)
    return document_text[start:end]


def observe_predicate(
    document_text: str,
    pred: AtomicPredicate,
) -> Tuple[DocumentState, List[str], MatchMethod, str]:
    """Observe one atomic predicate with qualifier checks.

    Returns (document_state, unmet_qualifiers, match_method, span_excerpt).
    """
    found, method, form = _find_capability_span(document_text, pred.capability)
    if not found:
        return "not_demonstrated", [], "none", ""

    ctx = _span_context(document_text, form)
    unmet: List[str] = []

    # Negation / uncertain near the match cannot satisfy production_use or ownership.
    if _NEGATION.search(ctx):
        if pred.evidence_expectation in {"production_use", "ownership"} or pred.ownership_required:
            unmet.append("negated_or_uncertain")
            return "not_demonstrated", unmet, method, ctx
        # Soft negation with no strong expectation → partial at best
        unmet.append("negated_or_uncertain")
        return "partially_supported", unmet, method, ctx

    if pred.ownership_required or pred.evidence_expectation == "ownership":
        if not _OWNERSHIP.search(ctx):
            unmet.append("ownership")
    if pred.evidence_expectation == "production_use":
        if not _PRODUCTION.search(ctx):
            unmet.append("production_use")
    if pred.action:
        observed = classify_action(ctx)
        need = _ACTION_RANK.get(pred.action, 0)
        have = _ACTION_RANK.get(observed, 0)
        if need and have < need:
            unmet.append(f"action:{pred.action}")
    if pred.currency_required and _HISTORICAL.search(ctx) and not re.search(
        r"\b(?:currently|present|now)\b", ctx, re.I,
    ):
        unmet.append("currency")
    if pred.duration_required:
        # Prefer explicit tool-scoped duration; tenure alone is uncertain and does not satisfy.
        try:
            from services.assessment_dates import (
                meets_duration_months, tool_scoped_intervals, _span_to_months,
            )
            # Local span in context
            local = re.search(r"\b(\d+\+?\s*(?:years?|months?))\b", ctx, re.I)
            if local and _span_to_months(local.group(1)) > 0:
                pass  # explicit local duration near match — ok
            else:
                unmet.append("duration")
        except Exception:
            if not re.search(r"\b\d+\+?\s*(?:years?|months?)\b", ctx, re.I):
                unmet.append("duration")
    if pred.evidence_expectation == "listed_skill_ok":
        # Mention is enough.
        return "satisfied", unmet, method, ctx

    if unmet:
        # Genuine limited positive evidence → partial, not contradiction.
        return "partially_supported", unmet, method, ctx
    return "satisfied", unmet, method, ctx


def duration_months_for_capability(
    resume: dict | None,
    capability: str,
    required_months: float,
) -> tuple[bool, float, bool]:
    """Evaluate duration using merged tool-scoped intervals (no tenure double-count)."""
    from services.assessment_dates import meets_duration_months, tool_scoped_intervals
    roles = list((resume or {}).get("experience") or [])
    intervals = tool_scoped_intervals(roles=roles, capability=capability)
    return meets_duration_months(intervals, required_months, allow_uncertain=False)


def observe_document(
    document_text: str,
    requirements: Sequence[CanonicalRequirement],
) -> Dict[str, LeafObservation]:
    """Observe capabilities in the final file independently of planner state."""
    obs: Dict[str, LeafObservation] = {}
    for req in requirements:
        if req.expression.kind == "unresolved":
            continue
        for pred in _atomic_nodes(req.expression):
            state, unmet, _method, _span = observe_predicate(document_text, pred)
            key = pred.capability.strip().lower()
            if not key:
                continue
            # Keep strongest observation if duplicate keys appear.
            prev = obs.get(key)
            rank = {"contradicted": 0, "not_demonstrated": 1, "unknown": 2,
                    "partially_supported": 3, "satisfied": 4}
            if prev is None or rank.get(state, 0) >= rank.get(prev.document_state, 0):
                leaf = LeafObservation(capability=pred.capability, document_state=state)
                obs[key] = leaf
                obs[pred.capability] = leaf
            # Stash unmet on a side channel via capability|meta keys for evaluate
            obs[f"__unmet__{key}"] = LeafObservation(
                capability="|".join(unmet), document_state=state,
            )
    return obs


def reconcile_source(
    *,
    document_text: str,
    document_refs: Sequence[Tuple[str, str]],
    source_facts: Sequence[Dict[str, Any]],
    claim_text: str,
    expected_entity: str = "",
    document_entity: str = "",
) -> Tuple[SourceState, ClaimState, List[SourceRef], MatchMethod]:
    """Reconcile a document claim with scoped source facts.

    Same bullet text on the wrong employer → contradicted.
    document_entity: entity inferred from the tailored resume placement.
    """
    claim = (claim_text or "").strip()
    entity = expected_entity or document_entity
    if not claim:
        return "not_evidenced", "unverified", [], "none"

    matches = []
    for fact in source_facts:
        text = str(fact.get("text") or "").strip()
        if not text:
            continue
        if text.casefold() == claim.casefold() or claim.casefold() in text.casefold():
            matches.append(fact)
        elif any(token_boundary_match(text, f) for f in _capability_aliases(claim)):
            matches.append(fact)

    if not matches:
        if text_contains_capability(document_text, claim):
            return "not_evidenced", "unverified", [], "none"
        return "not_evidenced", "unverified", [], "none"

    if entity:
        entity_ok = [m for m in matches if str(m.get("entity") or "") == entity]
        wrong = [m for m in matches if str(m.get("entity") or "") != entity]
        if not entity_ok and wrong:
            refs = [SourceRef(
                fact_id=str(m.get("id") or ""),
                entity=str(m.get("entity") or ""),
                text=str(m.get("text") or "")[:200],
            ) for m in wrong]
            return "contradicted", "contradicted", refs, "exact"
        if entity_ok:
            matches = entity_ok

    best = matches[0]
    refs = [SourceRef(
        fact_id=str(best.get("id") or ""),
        entity=str(best.get("entity") or ""),
        text=str(best.get("text") or "")[:200],
    )]
    kind = str(best.get("kind") or "accomplishment")
    if kind == "skill":
        return "listed_only", "supported_explicit", refs, "exact"
    return "supported", "supported_explicit", refs, "exact"


def _document_entity_for_span(resume: Optional[Dict[str, Any]], span: str) -> str:
    """Infer which experience/project entity a span appears under in the resume."""
    if not resume or not span:
        return ""
    for section in ("experience", "projects"):
        for i, row in enumerate(resume.get(section) or []):
            blobs = [str(row.get("title") or ""), str(row.get("company") or ""),
                     str(row.get("name") or "")]
            blobs.extend(str(b) for b in (row.get("bullets") or []))
            blob = "\n".join(blobs)
            if token_boundary_match(blob, span) or span.casefold() in blob.casefold():
                return f"{section}.{i}"
    return ""


def evaluate_requirements(
    *,
    document_text: str,
    jd: Dict[str, Any],
    source_facts: Optional[Sequence[Dict[str, Any]]] = None,
    resume: Optional[Dict[str, Any]] = None,
    source_resume: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Full Package 4 evaluation: expressions → observations → reducer → counts."""
    requirements = canonicalize_requirements(jd)
    # Deduplicate acronym+expansion as one group: if two reqs share same core, keep one.
    requirements = _dedupe_acronym_groups(requirements)

    facts = list(source_facts or [])
    fact_source = source_resume or resume
    if not facts and fact_source:
        from services.tailoring_planner import source_facts as _sf
        facts = [
            (f.__dict__ if hasattr(f, "__dict__") else dict(f))
            for f in _sf(fact_source)
        ]

    # Build observations using qualifier-aware predicate observer, then reduce.
    leaf_obs: Dict[str, LeafObservation] = {}
    pred_meta: Dict[str, Dict[str, Any]] = {}  # capability.lower → meta

    for req in requirements:
        if req.expression.kind == "unresolved":
            continue
        for pred in _atomic_nodes(req.expression):
            state, unmet, method, span = observe_predicate(document_text, pred)
            key = pred.capability.strip().lower()
            leaf_obs[key] = LeafObservation(capability=pred.capability, document_state=state)
            leaf_obs[pred.capability] = leaf_obs[key]
            pred_meta[key] = {
                "unmet": unmet, "method": method, "span": span, "pred": pred,
            }

    evaluations: List[RequirementEvaluation] = []
    for req in requirements:
        if req.expression.kind == "unresolved":
            evaluations.append(RequirementEvaluation(
                requirement_id=req.id,
                document_state="unknown",
                source_state="unknown",
                claim_state="unverified",
                match_method="none",
                reason=req.expression.reason or "ambiguous requirement wording",
            ))
            continue

        doc_state = reduce_expression(req.expression, leaf_obs)
        source_state: SourceState = "unknown"
        claim_state: ClaimState = "unverified"
        match_method: MatchMethod = "none"
        source_refs: List[SourceRef] = []
        doc_refs: List[DocumentRef] = []
        unmet: List[str] = []

        if doc_state == "not_demonstrated":
            source_state = "not_evidenced"
        elif doc_state in {"satisfied", "partially_supported"}:
            any_supported = False
            any_listed = False
            any_contradicted = False
            for pred in _atomic_nodes(req.expression):
                key = pred.capability.strip().lower()
                meta = pred_meta.get(key) or {}
                for u in meta.get("unmet") or []:
                    if u not in unmet:
                        unmet.append(u)
                span = meta.get("span") or pred.capability
                if meta.get("span"):
                    doc_refs.append(DocumentRef(
                        span=str(meta["span"])[:240],
                        section="document",
                        entity=_document_entity_for_span(resume, pred.capability),
                    ))
                if not text_contains_capability(document_text, pred.capability) and not meta.get("span"):
                    continue
                entity = _document_entity_for_span(resume, pred.capability)
                # For wrong-employer: if document places claim under entity A but
                # only entity B has the source fact with same text, contradict.
                ss, cs, refs, mm = reconcile_source(
                    document_text=document_text,
                    document_refs=[],
                    source_facts=facts,
                    claim_text=span if len(str(span)) > len(pred.capability) else pred.capability,
                    document_entity=entity,
                    expected_entity=entity,
                )
                # Also try matching accomplishment bullets that contain the capability
                if ss == "not_evidenced" and entity:
                    for fact in facts:
                        if str(fact.get("entity") or "") != entity:
                            continue
                        if token_boundary_match(str(fact.get("text") or ""), pred.capability):
                            ss, cs, refs, mm = "supported", "supported_explicit", [
                                SourceRef(
                                    fact_id=str(fact.get("id") or ""),
                                    entity=entity,
                                    text=str(fact.get("text") or "")[:200],
                                )
                            ], meta.get("method") or "exact"
                            break
                # Wrong employer: capability text exists only under a different entity in source
                if ss in {"supported", "listed_only"} and entity:
                    pass
                elif ss == "not_evidenced":
                    # Check if an identical bullet exists under another employer
                    for fact in facts:
                        ft = str(fact.get("text") or "")
                        if token_boundary_match(ft, pred.capability) or (
                            span and span.casefold() in ft.casefold()
                        ):
                            if entity and str(fact.get("entity") or "") != entity:
                                # Document claims at entity but source support is elsewhere
                                # only contradict when document attributes to a specific entity
                                if entity:
                                    ss, cs = "contradicted", "contradicted"
                                    refs = [SourceRef(
                                        fact_id=str(fact.get("id") or ""),
                                        entity=str(fact.get("entity") or ""),
                                        text=ft[:200],
                                    )]
                                    break
                source_refs.extend(refs)
                if mm != "none":
                    match_method = mm  # type: ignore[assignment]
                if ss == "contradicted":
                    any_contradicted = True
                    claim_state = "contradicted"
                elif ss == "supported":
                    any_supported = True
                    claim_state = cs
                elif ss == "listed_only":
                    any_listed = True
                    claim_state = cs
            if any_contradicted:
                source_state = "contradicted"
            elif any_supported:
                source_state = "supported"
            elif any_listed:
                source_state = "listed_only"
            else:
                source_state = "not_evidenced"
                claim_state = "unverified"
            # Skills-only cannot satisfy ownership/production expectations.
            if source_state == "listed_only" and any(
                p.ownership_required or p.evidence_expectation in {"ownership", "production_use"}
                for p in _atomic_nodes(req.expression)
            ):
                if "ownership" not in unmet and "production_use" not in unmet:
                    unmet.append("work_context")
                if doc_state == "satisfied":
                    doc_state = "partially_supported"

        evaluations.append(RequirementEvaluation(
            requirement_id=req.id,
            document_state=doc_state,
            source_state=source_state,
            claim_state=claim_state,
            match_method=match_method,
            document_refs=doc_refs,
            source_refs=source_refs,
            unmet_qualifiers=unmet,
        ))

    counts = count_evaluations(requirements, evaluations)
    return {
        "requirements": [r.as_dict() for r in requirements],
        "evaluations": [e.as_dict() for e in evaluations],
        "counts": counts.as_dict(),
    }


def _dedupe_acronym_groups(
    requirements: List[CanonicalRequirement],
) -> List[CanonicalRequirement]:
    """A term plus its acronym expansion does not double-count.

    If one requirement is 'Amazon Web Services' and another is 'AWS', keep one.
    """
    ACRONYM_PAIRS = [
        ("amazon web services", "aws"),
        ("kubernetes", "k8s"),
        ("continuous integration", "ci"),
        ("continuous delivery", "cd"),
        ("machine learning", "ml"),
        ("natural language processing", "nlp"),
        ("application programming interface", "api"),
    ]
    texts = {r.display_text.strip().lower(): r for r in requirements}
    drop: set[str] = set()
    for long, short in ACRONYM_PAIRS:
        if long in texts and short in texts:
            # Keep the longer display form; drop short duplicate.
            drop.add(texts[short].id)
    return [r for r in requirements if r.id not in drop]


def coverage_counts_equal(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    """True when required display buckets match (semantic equivalence helper)."""
    return (a.get("counts") or {}).get("required") == (b.get("counts") or {}).get("required")
