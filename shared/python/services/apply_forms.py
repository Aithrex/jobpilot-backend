"""Normalized apply-form question schema — the ONE shape the auto-apply gather
phase speaks between the executor recon and the answer library / autonomy brain.

A raw recon question (see executor_adapter.ReconQuestion) carries an ATS-specific
field handle (``id`` — a DOM/native handle the executor fills back into) and a
human ``text`` label. Neither is a stable, cross-ATS key: the same "Are you
authorized to work in the US?" question has a different ``id`` on every posting.

This module maps a recon question onto a NORMALIZED question keyed by a stable
``field_id`` — the canonical seed-bank field id (``work_authorized_us``,
``desired_salary``, …) derived from the label via the answer library's own
deterministic classifier. That stable id is what `answer_library_service.resolve`
returns its resolutions under, and what the orchestrator looks answers up by — so
recon, resolution and answer-lookup all agree on ONE key and no question is ever
silently dropped (the bug this fixes: recon dicts keyed by ``id`` vs. resolutions
keyed by question text → zero overlap → false coverage 1.0).

Never fabricates: the ``is_legal_attestation`` flag is the OR of what recon
reported (executor errs toward TRUE) and what the classifier recognizes as a
legal category, so a legal attestation can never be down-graded to a guessable
screening field.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

try:  # TypedDict is stdlib on the Lambda runtime; degrade gracefully if not.
    from typing import TypedDict
except ImportError:  # pragma: no cover
    TypedDict = None  # type: ignore

from utils.security import InputSanitizer


if TypedDict is not None:  # pragma: no branch

    class NormalizedQuestion(TypedDict, total=False):
        field_id: str                 # stable seed-bank id (work_authorized_us, …)
        label: str                    # the human question label
        type: str                     # QTYPE_* (text/textarea/select/…)
        required: bool                # ATS blocks submit without it
        is_legal_attestation: bool    # work-auth / sponsorship / EEO / consent
        options: List[str]            # choices, when the field is a select/radio
        recon_id: str                 # original ATS handle (executor fill-back)


_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def _slug(text: str, fallback: str = "field") -> str:
    """A stable, filter-safe id for a question the classifier did not recognize
    (a genuinely novel screening field). Deterministic per label so a re-recon
    of the same form yields the same key."""
    s = _SLUG_STRIP.sub("_", (text or "").strip().lower()).strip("_")
    return s[:80] or fallback


def _same_control(existing: Dict[str, Any], recon_id: str, label: str) -> bool:
    """True when this recon question is the SAME form control we already kept.

    The DOM handle decides: two entries carrying the same ``recon_id`` are one
    input reported twice, so merging them loses nothing. Different handles are
    different inputs — even when they classify alike — and collapsing those is
    what silently dropped a required control (see `normalize_recon_questions`).
    When a handle is missing on either side we fall back to the label, which is
    the only signal left."""
    a = str(existing.get("recon_id") or "").strip()
    b = str(recon_id or "").strip()
    if a and b:
        return a == b
    return _slug(str(existing.get("label") or ""), "") == _slug(label or "", "")


def _disambiguate(field_id: str, label: str, taken: Dict[str, Any]) -> str:
    """A distinct, stable key for a DIFFERENT control that classifies onto an
    already-taken field id. Deterministic per label so a re-recon of the same
    form yields the same key (a remembered answer must keep matching)."""
    base = ("%s__%s" % (field_id, _slug(label)))[:120]
    candidate, n = base, 2
    while candidate in taken:
        candidate = ("%s_%d" % (base, n))[:120]
        n += 1
    return candidate


def normalize_recon_questions(
        recon_questions: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """Map raw recon questions onto the normalized ``field_id``-keyed shape.

    De-dupes by ``field_id`` (first-seen wins; ``required`` and
    ``is_legal_attestation`` are OR-merged across collisions, so the stricter
    signal always survives). Non-dict / empty entries are skipped. Import of the
    answer library is lazy to keep this module import-cheap for cold starts.
    """
    from services import answer_library_service as lib
    try:  # question bank is the authoritative catalog (polarity + sensitivity).
        from services import application_question_bank as qb
    except Exception:  # pragma: no cover - parallel build
        qb = None

    out: List[Dict[str, Any]] = []
    by_fid: Dict[str, Dict[str, Any]] = {}

    for rq in (recon_questions or []):
        if not isinstance(rq, dict):
            continue
        label = InputSanitizer.sanitize_string(
            str(rq.get("text") or rq.get("label") or ""), max_length=2000)
        recon_id = InputSanitizer.sanitize_string(
            str(rq.get("id") or rq.get("recon_id") or ""), max_length=200)

        opts = rq.get("options")
        opt_list = ([InputSanitizer.sanitize_string(str(o), max_length=200)
                     for o in opts[:50] if o not in (None, "")]
                    if isinstance(opts, list) else None)

        # 1) Bank normalizer (deterministic only here — no per-field LLM in the
        #    gather loop) gives the canonical field_id + polarity + sensitivity.
        field_id = ""
        category = None
        invert = False
        bank_gated = False
        if qb is not None and label:
            bank = qb.normalize_field(label, options=opt_list, allow_llm=False)
            if bank.get("field_id") and bank["field_id"] != "unknown":
                field_id = bank["field_id"]
                invert = bool(bank.get("invert"))
                bank_gated = bank.get("sensitivity") in (qb.SENS_PROTECTED, qb.SENS_LEGAL)

        # 2) Fall back to the seed-bank classifier, then a slug (never dropped).
        if not field_id:
            mapping = lib.classify(label) if label else None
            if mapping is not None:
                field_id, category = mapping[0], mapping[1]
            else:
                field_id = _slug(recon_id or label)

        if not field_id:
            continue

        required = bool(rq.get("required"))
        is_legal = bool(rq.get("is_legal_attestation")) or bank_gated or (
            category in lib.LEGAL_CATEGORIES)

        existing = by_fid.get(field_id)
        if existing is not None:
            if _same_control(existing, recon_id, label):
                # One input reported twice (same DOM handle) — merge, keeping
                # the stricter signal.
                existing["required"] = existing["required"] or required
                existing["is_legal_attestation"] = (
                    existing["is_legal_attestation"] or is_legal)
                continue
            # DIFFERENT controls that classify onto the same canonical id.
            #
            # Collapsing these DROPPED the second control outright: its recon_id
            # was discarded so nothing ever filled it, while its `required` was
            # OR-merged onto the survivor — so `_presubmit_gaps` saw a satisfied
            # field, reported ZERO gaps, and the executor clicked Submit on an
            # incomplete form. Observed in prod (EnergyHub, 2026-07-29): a
            # REQUIRED immigration attestation ("are you on a nonimmigrant visa
            # … will you require a written submission to your school …")
            # classified onto `school`, collided with the real School field and
            # vanished. Only the ATS's own server-side validation stopped it.
            #
            # Keeping both is safe now that Workday's recon no longer emits a
            # control's wrapper AND its inner input as two questions — that was
            # the one source of same-control duplicates with different handles.
            field_id = _disambiguate(field_id, label, by_fid)

        nq: Dict[str, Any] = {
            "field_id": field_id,
            "label": label,
            "type": InputSanitizer.sanitize_string(
                str(rq.get("type") or ""), max_length=40) or "text",
            "required": required,
            "is_legal_attestation": is_legal,
            "invert": invert,
            "recon_id": recon_id,
        }
        if opt_list is not None:
            nq["options"] = opt_list
        by_fid[field_id] = nq
        out.append(nq)

    return out
