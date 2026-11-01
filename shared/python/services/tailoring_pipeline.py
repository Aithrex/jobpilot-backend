"""Single opt-in composition path used by every ResumeTailor entry point.

Candidates are assembled from source fields plus scoped patches. Unknown checks
restore the corresponding source bullet; a model response never becomes evidence.
"""
from __future__ import annotations

import copy
import difflib
import json
import logging
import re
import time
from dataclasses import asdict

from services.tailoring_policy import VERSION, WRITING_RULES, content, content_hash, digest, policy_context
from services.tailoring_planner import build_plan, questions
from schemas.tailoring_contracts import CandidateFact
from utils.llm_safety import guarded_system, wrap_untrusted, redact_pii_for_model
from services.jd_profile import prompt_view

logger = logging.getLogger(__name__)


def is_result(resume: dict) -> bool:
    return (resume.get("tailoring_run") or {}).get("policy_version") == VERSION


def _call(prompt: str, schema: dict, *, system: str = WRITING_RULES) -> dict:
    from services.claude_client import claude_json, CLAUDE_PRO
    return claude_json(prompt=prompt, schema=schema, system=guarded_system(system),
                       model=CLAUDE_PRO, max_tokens=12000, temperature=0.2)


def _protected_quantities(text: str) -> set[str]:
    return {re.sub(r"[\s,]", "", x.lower()) for x in re.findall(
        r"(?:about\s+|at least\s+|more than\s+|under\s+|~)?\$?\d[\d,]*(?:\.\d+)?(?:\s*(?:%|ms|million|billion|seconds|minutes|hours|[KM]))?\+?",
        text, re.I)}


def _semantic_plan(plan) -> None:
    """Recognize transferable actions across occupations without new user input."""
    unresolved = [r for r in plan.requirements if r.state in {"related", "not_evidenced"}]
    facts = [f for f in plan.facts if f.kind in {"accomplishment", "credential"}]
    if not unresolved or not facts:
        return
    packet = {"requirements": [asdict(r) for r in unresolved], "facts": [asdict(f) for f in facts]}
    if len(json.dumps(packet)) > 45000:
        plan.completeness = "partial"
        return
    try:
        response = _call(wrap_untrusted("evidence", redact_pii_for_model(json.dumps(packet))),
            {"matches": [{"requirement_id": str, "fact_id": str, "state": str, "reason": str}]},
            system="""Compare requirements to actual candidate actions, across any profession.
For each match use one fact only. State demonstrated only when that action entails
the requirement; related for useful transferable experience that does not fully
meet it. Different words are welcome: reconciliation work can demonstrate accuracy
controls, facilitating stakeholder workshops can demonstrate requirements gathering.
Never infer a tool, setting, seniority, credential, scale or duty from a title.
Do not substitute related technologies or combine separate roles into one claim.
Give a concrete entailment reason. Omit unsupported matches.""")
        by_req = {r.id: r for r in unresolved}
        by_fact = {f.id: f for f in facts}
        for row in response.get("matches") or []:
            if not isinstance(row, dict):
                continue
            req = by_req.get(row.get("requirement_id"))
            fid, state = row.get("fact_id"), row.get("state")
            if req is None or fid not in by_fact or state not in {"demonstrated", "related"} or not row.get("reason"):
                continue
            if req.state != "demonstrated" or state == "demonstrated":
                if req.state != state:
                    req.fact_ids = []
                req.state, req.support_basis = state, "semantic" if state == "demonstrated" else "transferable"
                if fid not in req.fact_ids:
                    req.fact_ids.append(fid)
        weights = {fid: sum(3 if r.priority == "required" else 1 for r in plan.requirements if fid in r.fact_ids)
                   for fid in plan.selected}
        plan.selected.sort(key=lambda fid: -weights[fid])
    except Exception:
        plan.completeness = "partial"


def _validate_patches(patches: list[dict], facts: dict) -> dict[str, dict]:
    """One isolated evidence packet per proposed change; no resume-wide grounding."""
    changed = []
    verdicts = {}
    for patch in patches:
        fid, text = patch["id"], patch["text"]
        source = facts[fid].text
        if text == source:
            verdicts[fid] = {"state": "supported_explicit", "reason": "source_text"}
            continue
        # Qualifiers/units must be retained. Semantic normalization is left to
        # the writer; an unknown numeric conversion safely retains the source.
        if _protected_quantities(text) != _protected_quantities(source):
            verdicts[fid] = {"state": "unsupported", "reason": "metric_semantics_changed"}
            continue
        changed.append({"id": fid, "source": source, "proposed": text})
    if changed:
        try:
            result = _call(wrap_untrusted("changes", redact_pii_for_model(json.dumps(changed))),
                           {"results": [{"id": str, "state": str, "reason": str}]},
                           system="""Validate each proposed resume change using ONLY its own source.
Return supported_explicit for faithful wording, supported_semantic for an accurate
capability description implied by the action, ambiguous_new_detail for a plausible
new claim, or unsupported for a contradiction. Retries demonstrate failure recovery;
collaboration does not demonstrate leadership. Do not require identical words.
Preserve who did what, tools, scope, setting, causality, numbers and outcomes.
A typical activity for a job title is not evidence. Never use one packet's source
to support another packet. Provide a short reason; assess facts, not polish.""")
            for row in result.get("results") or []:
                if isinstance(row, dict) and row.get("id") in {p["id"] for p in changed}:
                    state = row.get("state")
                    verdicts[row["id"]] = {"state": state if state in {
                        "supported_explicit", "supported_semantic", "ambiguous_new_detail", "unsupported"
                    } else "unverified", "reason": str(row.get("reason") or "")[:200]}
        except Exception:
            logger.warning("Scoped tailoring validation unavailable; retaining source changes")
    return verdicts


def rewrite_bullet(bullet: str, job_title: str = "", keywords: list | None = None) -> str:
    fact = CandidateFact("bullet", "bullet", "bullet", bullet)
    try:
        generated = _call(wrap_untrusted("rewrite", redact_pii_for_model(json.dumps({
            "source": bullet, "target_role": job_title, "job_terms": keywords or [],
        }))), {"rewritten": str})
        text = generated.get("rewritten", "")
        if not isinstance(text, str) or not 10 <= len(text) <= 650:
            return bullet
        verdict = _validate_patches([{"id": "bullet", "text": text}], {"bullet": fact})
        return text if verdict.get("bullet", {}).get("state") in {"supported_explicit", "supported_semantic"} else bullet
    except Exception:
        return bullet


@policy_context
def run(source: dict, jd: dict, *, current: dict | None = None,
        feedback: str = "", fact_store: dict | None = None, past_edits: str = "") -> dict:
    started = time.monotonic()
    source, jd = copy.deepcopy(content(source)), copy.deepcopy(jd)
    additions = []
    evidence_source = copy.deepcopy(source)
    for f in (fact_store or {}).get("facts") or []:
        if f.get("source") != "user" or f.get("source_hash") != content_hash(source):
            continue
        entity, text = f.get("entity", ""), f.get("text", "")
        if not re.fullmatch(r"(?:experience|projects)\.\d+", entity) or not isinstance(text, str) or not text.strip():
            continue
        section, index = entity.split(".")
        if int(index) >= len(source.get(section) or []):
            continue
        fid = "confirmed:" + digest([entity, text])[:16]
        additions.append(CandidateFact(fid, entity + ".confirmed", entity, text))
        evidence_source[section][int(index)].setdefault("bullets", []).append(text)
        if len(additions) >= 20:
            break
    plan = build_plan(source, jd, additions)
    _semantic_plan(plan)
    facts = {f.id: f for f in plan.facts if f.kind == "accomplishment"}
    # A prior result carries its original snapshot only inside the server-owned
    # version; callers still supply source explicitly. No current prose is a fact.
    result = content(current if current is not None else source)
    if current is None and isinstance(result.get("skills"), dict):
        from services.tailoring_planner import contains
        def relevance(skill):
            return -sum(3 if r.priority == "required" else 1 for r in plan.requirements
                        if any(contains(a, str(skill)) for a in r.alternatives))
        result["skills"] = {category: sorted(values, key=relevance)
                            for category, values in result["skills"].items()}
    result.setdefault("skills_section_title", "Skills")
    for field in ("contact", "education", "additional_sections", "certifications"):
        if field in source:
            result[field] = copy.deepcopy(source[field])
    feedback = str(feedback or "")
    remove_summary = bool(re.search(r"(?:remove|omit|exclude|no)\s+(?:the\s+)?summary", feedback, re.I))
    preferences = copy.deepcopy((current or {}).get("tailoring_preferences") or {})
    if remove_summary:
        preferences["omit_summary"] = True
    if re.search(r"(?:add|include|restore)\s+(?:the\s+)?summary", feedback, re.I):
        preferences["omit_summary"] = False
        preferences["summary_locked"] = False
    locked = set(preferences.get("locked_fact_ids") or [])
    omitted = set(preferences.get("omitted_fact_ids") or [])
    user_omitted = set(omitted)
    source_rows = [asdict(facts[fid]) for fid in plan.selected if fid in facts]
    # Select whole evidence objects, never slice JSON mid-object.
    packed, used = [], 0
    for row in source_rows:
        size = len(json.dumps(row))
        if used + size > 45000:
            continue
        packed.append(row)
        used += size
    allowed = {r["id"] for r in packed} - locked - omitted
    prompt = "\n".join([
        "Choose the strongest relevant accomplishments. Return patches in preferred order within each role.",
        "Every patch id is an existing fact id. Each text is backed by that fact; do not merge unrelated events.",
        "omitted_ids may contain low-value bullets to omit, but preserve at least one bullet per existing role.",
        "Never omit an entire nonempty source section (projects, research, volunteer, credentials, custom) unless the user omitted it.",
        "For regeneration keep unrelated existing edits and exclusions. Return only requested changes.",
        wrap_untrusted("job", redact_pii_for_model(json.dumps(prompt_view(jd)))),
        wrap_untrusted("plan", json.dumps([asdict(r) for r in plan.requirements])),
        wrap_untrusted("facts", redact_pii_for_model(json.dumps(packed))),
        wrap_untrusted("current_document", redact_pii_for_model(json.dumps(result))),
        wrap_untrusted("feedback", redact_pii_for_model(feedback)),
        wrap_untrusted("style_preferences", redact_pii_for_model(past_edits or "")),
        "Return JSON with patches [{id,text}] and omitted_ids [id]. No identity fields or new entries.",
    ])
    reasons, patches = [], []
    try:
        generated = _call(prompt, {"patches": [{"id": str, "text": str}], "omitted_ids": [str]})
        seen = set()
        for row in generated.get("patches") or []:
            fid = row.get("id") if isinstance(row, dict) else None
            text = row.get("text") if isinstance(row, dict) else None
            if fid in allowed and fid not in seen and isinstance(text, str) and 10 <= len(text) <= 650:
                patches.append({"id": fid, "text": text.strip()})
                seen.add(fid)
        proposed_omissions = set(generated.get("omitted_ids") or []) & allowed
        omitted |= proposed_omissions
    except Exception:
        reasons.append("generation_unavailable")
    verdicts = _validate_patches(patches, facts)
    accepted = {p["id"]: p["text"] for p in patches if
                verdicts.get(p["id"], {}).get("state") in {"supported_explicit", "supported_semantic"}}
    for p in patches:
        if p["id"] not in accepted:
            reasons.append(verdicts.get(p["id"], {}).get("reason") or "unverified_patch")
    # Current text is retained by address only when its source provenance is
    # available and the text still matches the prior verified hash.
    prior = (current or {}).get("tailoring_run") or {}
    prior_text = {x["fact_id"]: x["text"] for x in prior.get("provenance") or []
                  if isinstance(x, dict) and "fact_id" in x and "text" in x}
    prior_trusted = prior.get("source_hash") == plan.source_hash
    # Explicit edit locks survive unrelated changes. They are preserved as user
    # text, never promoted into evidence by virtue of being in a prior draft.
    locked_text = preferences.get("locked_text") or {}
    manual, manual_extra = {}, {}
    if prior_trusted and current:
        for entity in {p.get("entity") for p in prior.get("provenance") or []}:
            if not entity or not re.fullmatch(r"(?:experience|projects)\.\d+", entity):
                continue
            section, idx = entity.split(".")
            entries = current.get(section) or []
            if int(idx) >= len(entries):
                continue
            old = [p for p in prior.get("provenance") or [] if p.get("entity") == entity]
            now = entries[int(idx)].get("bullets") or []
            # The editor sends ids for removals. Equal-length legacy edits can
            # also be recovered safely by address without shifting later bullets.
            matcher = difflib.SequenceMatcher(a=[p.get("text") for p in old], b=now, autojunk=False)
            for tag, a0, a1, b0, b1 in matcher.get_opcodes():
                if tag == "equal":
                    manual_extra.setdefault(entity, []).extend(
                        p["text"] for p in old[a0:a1] if p["fact_id"] not in facts)
                    continue
                previous, updated = old[a0:a1], now[b0:b1]
                for p, text in zip(previous, updated):
                    if p["fact_id"] in facts:
                        manual[p["fact_id"]] = text
                    else:
                        manual_extra.setdefault(entity, []).append(text)
                for p in previous[len(updated):]:
                    omitted.add(p["fact_id"])
                    user_omitted.add(p["fact_id"])
                manual_extra.setdefault(entity, []).extend(updated[len(previous):])
        manual.update({fid: text for fid, text in locked_text.items()
                       if fid in facts and isinstance(text, str)})
    locked |= set(manual)
    omitted = (omitted - locked) | user_omitted
    preferences["locked_fact_ids"] = sorted(locked)
    preferences["locked_text"] = manual
    provenance = []
    patch_order = [p["id"] for p in patches if p["id"] in accepted]
    for section in ("experience", "projects"):
        rows = []
        for i, entry in enumerate(source.get(section) or []):
            out = copy.deepcopy(entry)
            group = [f for f in plan.facts if f.entity == f"{section}.{i}" and f.kind == "accomplishment"]
            group.sort(key=lambda f: (0, patch_order.index(f.id)) if f.id in patch_order else (1, plan.selected.index(f.id)))
            kept = [f for f in group if f.id not in omitted]
            if not kept and any(f.id not in user_omitted for f in group):
                kept = [f for f in group if f.id not in user_omitted][:1]
                omitted.discard(kept[0].id)
            out["bullets"] = []
            for f in kept:
                text = manual.get(f.id, accepted.get(f.id, prior_text.get(f.id, f.text) if prior_trusted else f.text))
                out["bullets"].append(text)
                provenance.append({"fact_id": f.id, "entity": f.entity, "text": text,
                                   "source_text": f.text, "state": "user_edit_unverified" if f.id in manual
                                   else verdicts.get(f.id, {}).get("state", "supported_explicit")
                                   if f.id in accepted else "source_or_prior"})
            for text in manual_extra.get(f"{section}.{i}", []):
                out["bullets"].append(text)
                provenance.append({"fact_id": "manual:" + digest(text)[:16], "entity": f"{section}.{i}",
                                   "text": text, "source_text": "", "state": "user_edit_unverified"})
            rows.append(out)
        result[section] = rows
    preferences["omitted_fact_ids"] = sorted(omitted)
    result["tailoring_preferences"] = preferences
    if preferences.get("omit_summary"):
        result["summary"] = ""
    elif not preferences.get("summary_locked"):
        from services.summary_tailor import write_summary
        result = write_summary(result, evidence_source, jd, user_feedback=feedback or None)
    result["target_role"] = jd.get("job_title", "")
    result["tailoring_run"] = {
        "policy_version": VERSION, "source_hash": plan.source_hash, "jd_hash": plan.jd_hash,
        "evidence_hash": digest([asdict(f) for f in plan.facts]),
        "input_hash": digest([plan.source_hash, plan.jd_hash, [asdict(f) for f in plan.facts], preferences, feedback]),
        "content_hash": content_hash(result), "requirements": [asdict(r) for r in plan.requirements],
        "provenance": provenance, "questions": questions(plan),
        "analysis_completeness": plan.completeness, "source_completeness": "complete" if len(packed) == len(source_rows) else "partial",
        "reasons": list(dict.fromkeys(reasons)), "elapsed_ms": round((time.monotonic() - started) * 1000),
    }
    from services.tailoring_assessment import finalize
    from services.resume_renderer import ResumeRenderer
    renderer = ResumeRenderer()
    reviewed = finalize(result, jd, renderer)
    # One bounded repair of specifically quoted over-tailored prose. Reviewed
    # text is never rewritten by an unvalidated global polish or expansion pass.
    concerns = [f for f in ((reviewed.get("document_assessment") or {}).get("reader_review") or {}).get("findings", [])
                if f.get("kind") == "over_tailoring"]
    targets = [p for p in provenance if p["fact_id"] in accepted and p["fact_id"] not in locked
               and any(c.get("quote") and c["quote"] in p["text"] for c in concerns)]
    if targets:
        original_reviewed = copy.deepcopy(reviewed)
        try:
            response = _call(wrap_untrusted("repair", redact_pii_for_model(json.dumps({
                "changes": targets[:6], "reader_feedback": concerns,
                "instruction": "Simplify only these bullets; preserve the concrete accomplishment and relevant supported capability.",
            }))), {"patches": [{"id": str, "text": str}]})
            ids = {p["fact_id"] for p in targets[:6]}
            repairs = [p for p in response.get("patches") or [] if isinstance(p, dict) and p.get("id") in ids
                       and isinstance(p.get("text"), str) and 10 <= len(p["text"]) <= 650]
            checks = _validate_patches(repairs, facts)
            replacements = {p["id"]: p["text"] for p in repairs if checks.get(p["id"], {}).get("state")
                            in {"supported_explicit", "supported_semantic"}}
            changed = False
            for section in ("experience", "projects"):
                for i, row in enumerate(reviewed.get(section) or []):
                    refs = [p for p in provenance if p["entity"] == f"{section}.{i}"]
                    for j, ref in enumerate(refs):
                        new = replacements.get(ref["fact_id"])
                        if new and new != row["bullets"][j]:
                            row["bullets"][j] = new
                            ref.update(text=new, state=checks[ref["fact_id"]]["state"])
                            changed = True
            if changed:
                if not preferences.get("omit_summary") and not preferences.get("summary_locked"):
                    from services.summary_tailor import write_summary
                    reviewed = write_summary(reviewed, evidence_source, jd, user_feedback=feedback or None)
                reviewed["tailoring_run"]["provenance"] = provenance
                reviewed["tailoring_run"]["content_hash"] = content_hash(reviewed)
                reviewed["tailoring_run"]["reasons"].append("reader_repair_rechecked")
                reviewed.pop("document_assessment", None)
                reviewed = finalize(reviewed, jd, renderer)
        except Exception:
            reviewed = original_reviewed
            logger.warning("Reader repair unavailable; preserving the reviewed draft and its findings")
    return reviewed
