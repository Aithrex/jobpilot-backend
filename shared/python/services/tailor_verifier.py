"""
Tailor Verifier — LLM entailment pass over the final tailored resume.

Verifier ≠ generator: a separate FLASH call (temp 0) judges whether each
resume line's factual claims are SUPPORTED / PARTIAL / UNSUPPORTED by the
candidate's fact sheet (resume + confirmed enrichment). Output feeds the
human-in-the-loop review UI; it never edits the resume.

Trust model — the LLM never overrides the deterministic floor:
  - claim_guard flags CLAMP verdicts downward (a bullet the guard flagged
    can be at most PARTIAL, even if the verifier said SUPPORTED).
  - Verdicts only ever downgrade; nothing here strips content (that stays
    with IntegrityGuard/claim_guard, per the repo invariant: code guards
    floors, models advise).

Failure model — ADVISORY pass, fails OPEN: on timeout/exception the job
completes with `verification.status = "skipped"` and items UNVERIFIED.
(Deliberate contrast to voicegraph's authored.py judge, which gates
user-visible claims and therefore fails CLOSED.)
"""
from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from typing import Any, Dict, List, Tuple

from utils.llm_safety import guarded_system, wrap_untrusted

logger = logging.getLogger(__name__)

VERDICTS = ("SUPPORTED", "PARTIAL", "UNSUPPORTED", "UNVERIFIED")
_TIMEOUT_SECONDS = 25
_CHUNK_THRESHOLD = 60  # defensive; bullet caps keep real resumes ≤ ~40 lines

_VERIFY_SCHEMA = {
    "results": [{
        "id": str,
        "verdict": str,
        "evidence": [str],
        "unsupported_claim": str,
    }]
}

_VERIFIER_SYSTEM = guarded_system(
    "You are a strict resume fact-checker. You will receive a CANDIDATE FACT "
    "SHEET (numbered, verified facts about a real job candidate, drawn from "
    "their resume and their confirmed LinkedIn/GitHub data) and a list of "
    "RESUME LINES written by a resume-writing assistant. For each line, judge "
    "whether its factual claims are supported by the fact sheet.\n\n"
    "Verdicts — return exactly one per line:\n"
    "- SUPPORTED: every specific claim in the line (numbers, named "
    "tools/products, employers, outcomes, scope) is directly backed by one or "
    "more facts.\n"
    "- PARTIAL: the core activity is backed by the facts, but the line adds a "
    "plausible detail, tool, or generalization the facts do not explicitly "
    "state (a standard extension of work the candidate demonstrably did).\n"
    "- UNSUPPORTED: the line asserts something the facts do not back at all — "
    "a number with no source, a named product/technology the candidate never "
    "used, an outcome, scope, or seniority claim (e.g. leading a team, owning "
    "a budget) with no basis.\n\n"
    "Judging rules:\n"
    "1. Judge FACTS, not phrasing. Reworded, reordered, or merged facts are "
    "still SUPPORTED.\n"
    "2. Generic, role-standard activities with no specific claim (e.g. "
    "'collaborated with stakeholders', 'wrote clean, tested code') do not "
    "need an explicit fact — treat as SUPPORTED when the role itself is real.\n"
    "3. Numbers are strict: a percentage, dollar amount, count, latency, or "
    "duration is SUPPORTED only if that number (or an equivalent restatement) "
    "appears in the facts. An unstated number makes the line at best PARTIAL "
    "(if clearly an estimate of real work) and UNSUPPORTED if it manufactures "
    "an outcome.\n"
    "4. Named third-party products, certifications, employers, degrees, and "
    "dates are strict: unstated ones make the line UNSUPPORTED.\n"
    "5. evidence: list the fact numbers (e.g. \"F3\") that back the line — "
    "the strongest 1-3 only. Empty list if UNSUPPORTED.\n"
    "6. unsupported_claim: when the verdict is PARTIAL or UNSUPPORTED, quote "
    "the specific unsupported words from the line; otherwise return an empty "
    "string.\n"
    "7. Return a result for EVERY line id, in the same order they were given."
)


def enumerate_lines(tailored: Dict[str, Any]) -> List[Tuple[str, str]]:
    """(structural address, text) for every verifiable line.

    Addresses match claim_guard's: summary.s{k}, experience.{i}.bullets.{j},
    projects.{i}.bullets.{j} — the FE re-anchors by the text snapshot.
    """
    lines: List[Tuple[str, str]] = []
    summary = (tailored.get("summary") or "").strip()
    if summary:
        sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", summary) if s.strip()]
        for k, sentence in enumerate(sentences):
            lines.append((f"summary.s{k}", sentence))
    for i, exp in enumerate(tailored.get("experience") or []):
        for j, bullet in enumerate(exp.get("bullets") or []):
            if isinstance(bullet, str) and bullet.strip():
                lines.append((f"experience.{i}.bullets.{j}", bullet.strip()))
    for i, proj in enumerate(tailored.get("projects") or []):
        for j, bullet in enumerate(proj.get("bullets") or []):
            if isinstance(bullet, str) and bullet.strip():
                lines.append((f"projects.{i}.bullets.{j}", bullet.strip()))
    return lines


def verify(
    tailored: Dict[str, Any],
    fact_store: Dict[str, Any],
    claim_audit: Any = None,
    timeout_seconds: int = _TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    """Run the entailment pass. Returns the verification sidecar dict.

    Never raises and never fails the tailor job: any error/timeout yields
    `status: "skipped"` with all items UNVERIFIED (advisory pass fails open).
    """
    from services.profile_facts import build_fact_sheet

    lines = enumerate_lines(tailored)
    if not lines:
        return _sidecar("skipped", {}, reason="no lines to verify")

    fact_sheet = build_fact_sheet(fact_store)
    if not fact_sheet.strip():
        return _sidecar(
            "skipped",
            {addr: _item(text, "UNVERIFIED") for addr, text in lines},
            reason="empty fact sheet",
        )

    # Sheet-ref → human-readable fact text, so the review UI can show WHAT
    # backs a bullet without holding the fact store (build_fact_sheet stamps
    # `sheet_ref` onto each included fact).
    ref_map = {
        f["sheet_ref"]: f.get("display", "")
        for f in (fact_store.get("facts") or [])
        if f.get("sheet_ref")
    }

    try:
        raw_results = _call_verifier_with_timeout(fact_sheet, lines, timeout_seconds)
        status = "ok"
    except FuturesTimeoutError:
        logger.warning("tailor_verifier: timed out after %ss — skipping", timeout_seconds)
        return _sidecar(
            "skipped",
            _merge(lines, {}, claim_audit, ref_map),
            reason=f"verifier timeout ({timeout_seconds}s)",
        )
    except Exception as e:
        logger.warning("tailor_verifier: failed (%s) — skipping", e)
        return _sidecar("skipped", _merge(lines, {}, claim_audit, ref_map), reason=str(e))

    by_id = {}
    for r in raw_results:
        if isinstance(r, dict) and r.get("id"):
            by_id[str(r["id"])] = r
    items = _merge(lines, by_id, claim_audit, ref_map)

    verified = sum(1 for it in items.values() if it["verdict"] != "UNVERIFIED")
    if verified < len(lines):
        status = "degraded"
    return _sidecar(status, items)


def _call_verifier_with_timeout(
    fact_sheet: str,
    lines: List[Tuple[str, str]],
    timeout_seconds: int,
) -> List[Dict[str, Any]]:
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        chunks = [lines] if len(lines) <= _CHUNK_THRESHOLD else [
            lines[i:i + _CHUNK_THRESHOLD] for i in range(0, len(lines), _CHUNK_THRESHOLD)
        ]
        results: List[Dict[str, Any]] = []
        for chunk in chunks:
            future = executor.submit(_call_verifier, fact_sheet, chunk)
            results.extend(future.result(timeout=timeout_seconds))
        return results
    finally:
        executor.shutdown(wait=False)


def _call_verifier(
    fact_sheet: str, lines: List[Tuple[str, str]]
) -> List[Dict[str, Any]]:
    from services.claude_client import claude_json, CLAUDE_FLASH

    numbered = "\n".join(f"{addr} | {text}" for addr, text in lines)
    prompt = (
        "=== CANDIDATE FACT SHEET ===\n"
        + wrap_untrusted("fact_sheet", fact_sheet)
        + "\n\n=== RESUME LINES TO VERIFY ===\n"
        + wrap_untrusted("resume_lines", numbered)
        + "\n\nReturn JSON: {\"results\": [{\"id\": \"experience.0.bullets.0\", "
        "\"verdict\": \"SUPPORTED\", \"evidence\": [\"F3\"], "
        "\"unsupported_claim\": \"\"}]}"
    )
    result = claude_json(
        prompt,
        max_tokens=6000,
        temperature=0.0,
        model=CLAUDE_FLASH,
        schema=_VERIFY_SCHEMA,
        system=_VERIFIER_SYSTEM,
    )
    return result.get("results") or []


def _item(text: str, verdict: str) -> Dict[str, Any]:
    return {
        "text": text,
        "verdict": verdict,
        "evidence_fact_ids": [],
        "evidence_display": [],
        "flags": [],
        "unsupported_claim": "",
    }


def _merge(
    lines: List[Tuple[str, str]],
    by_id: Dict[str, Dict[str, Any]],
    claim_audit: Any,
    ref_map: Dict[str, str] | None = None,
) -> Dict[str, Dict[str, Any]]:
    """Merge verifier verdicts with deterministic claim-guard flags.

    Deny-by-default: unknown verdicts / missing ids → UNVERIFIED. The
    deterministic guard is the floor: its flags clamp a verdict to at most
    PARTIAL and are always surfaced, whatever the LLM said.
    """
    audit_items = getattr(claim_audit, "items", None) or {}
    ref_map = ref_map or {}
    merged: Dict[str, Dict[str, Any]] = {}
    for addr, text in lines:
        raw = by_id.get(addr) or {}
        verdict = str(raw.get("verdict") or "").upper()
        if verdict not in VERDICTS:
            verdict = "UNVERIFIED"
        item = _item(text, verdict)
        item["evidence_fact_ids"] = [
            e for e in (raw.get("evidence") or []) if isinstance(e, str)
        ][:3]
        item["evidence_display"] = [
            ref_map[e] for e in item["evidence_fact_ids"] if e in ref_map
        ]
        item["unsupported_claim"] = str(raw.get("unsupported_claim") or "")

        flagged = audit_items.get(addr)
        if flagged:
            item["flags"] = list(flagged.get("flags") or [])
            if item["verdict"] == "SUPPORTED":
                item["verdict"] = "PARTIAL"
            if not item["unsupported_claim"] and flagged.get("unsupported_claims"):
                item["unsupported_claim"] = "; ".join(flagged["unsupported_claims"][:3])
        merged[addr] = item
    return merged


def _sidecar(
    status: str, items: Dict[str, Dict[str, Any]], reason: str = ""
) -> Dict[str, Any]:
    counts = {"supported": 0, "partial": 0, "unsupported": 0, "unverified": 0}
    for item in items.values():
        counts[item["verdict"].lower()] = counts.get(item["verdict"].lower(), 0) + 1
    sidecar = {
        "status": status,
        "model": "flash",
        "counts": counts,
        "items": items,
    }
    if reason:
        sidecar["reason"] = reason
    return sidecar
