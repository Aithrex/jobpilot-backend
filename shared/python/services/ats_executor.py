"""AtsExecutor — the first REAL ``ExecutorAdapter`` implementation.

It replaces ``StubExecutor`` for the auto-apply pipeline and delivers the piece
that is genuinely reachable without a browser fleet: **live form recon over the
ATS's public JSON API** (Greenhouse today). Everything that requires actually
driving a browser — ``submit``, ``create_account``, ``verify_state`` — is the
job of the future Playwright/headless fleet, which is NOT deployed yet, so those
methods return the contract's clean ``EXECUTOR_UNAVAILABLE`` shape and
``is_available()`` reports ``False`` (rows stay ``ready`` for the fleet to pick
up — exactly as the seam intends).

Why this split is correct (and honest):
  * Greenhouse exposes the full application question set at
    ``boards-api.greenhouse.io/v1/boards/{token}/jobs/{id}?questions=true`` with
    NO auth — so recon is real, cheap, and reliable. This lights up the entire
    "here's what this job will ask; answer the 2 we couldn't auto-fill" UX with
    zero browser and zero risk.
  * SUBMISSION cannot be done over the API by a third party: Greenhouse/Lever
    gate the POST behind the *employer's* API key, which we never have. The only
    route is driving the public candidate web form (CSRF tokens, JS, sometimes
    CAPTCHA) — a browser. So ``submit`` stays dark here until that fleet lands.
  * Lever/Ashby/Workday do not expose their custom question set over a public
    unauth API either → recon for them also needs the browser. Until then this
    executor returns ``EXECUTOR_UNAVAILABLE`` for those families, which the
    orchestrator handles as a clean no-op (row left queued, nothing fabricated).

Wiring: ``auto_apply_service.get_executor()`` returns this class when
``AUTO_APPLY_EXECUTOR=ats`` (else ``StubExecutor``). Recon does NOT gate on
``is_available()``, so enabling this executor lights up recon while submit stays
safely off.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

import requests

from services.ats_detector import detect_ats, GREENHOUSE
from services.executor_adapter import (
    ExecutorAdapter,
    EXECUTOR_UNAVAILABLE,
    QTYPE_TEXT,
    QTYPE_TEXTAREA,
    QTYPE_SELECT,
    QTYPE_CHECKBOX,
    QTYPE_FILE,
)

logger = logging.getLogger(__name__)

# Greenhouse public Job Board API base (no auth for GET).
_GH_API = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{job_id}?questions=true"
_HTTP_TIMEOUT = 10  # seconds — recon is a single small GET
_MAX_QUESTIONS = 120  # sanity cap on a pathological form
_USER_AGENT = "JobPilot-AutoApply/1.0 (+https://aspirely.me)"

# Greenhouse field type → the executor_adapter QTYPE_* vocabulary.
_GH_TYPE_MAP = {
    "input_text": QTYPE_TEXT,
    "textarea": QTYPE_TEXTAREA,
    "input_file": QTYPE_FILE,
    "multi_value_single_select": QTYPE_SELECT,
    "multi_value_multi_select": QTYPE_CHECKBOX,
    # input_hidden intentionally absent → hidden fields are dropped.
}

# Fields the EXECUTOR supplies at submit time (the tailored résumé / cover
# letter PDF), NOT the user. Surfacing them as questions would push an
# unanswerable file field into the one-tap queue and block every application.
_EXECUTOR_SUPPLIED_PREFIXES = ("resume", "cover_letter")

# Label patterns that mark a question as a LEGAL ATTESTATION. Per the seam
# contract we err toward TRUE: a legal attestation may ONLY be answered from the
# user's declared visa/onboarding data, never by the answer library or an LLM.
_LEGAL_PATTERNS = re.compile(
    r"""(
        sponsor(ship)? | \bvisa\b | work\s*authoriz | authoriz(ed|ation)\s+to\s+work |
        right\s+to\s+work | legally\s+(authorized|entitled) | require\s+sponsorship |
        \beeo\b | equal\s+employment | \brace\b | ethnicit | \bgender\b | veteran |
        disabilit | demographic | sexual\s+orientation |
        \b18\s+years | age\s+of\s+majority | over\s+18 |
        background\s+check | drug\s+(test|screen) | criminal | felony |
        \bconsent\b | \bgdpr\b | data\s+(privacy|protection|processing) | privacy\s+policy |
        i\s+(certify|agree|acknowledge|consent|confirm|attest) | terms\s+and\s+conditions |
        employment\s+agreement | non[-\s]?compete | restrictive\s+covenant |
        security\s+clearance
    )""",
    re.IGNORECASE | re.VERBOSE,
)


def _is_legal(label: str) -> bool:
    return bool(label) and bool(_LEGAL_PATTERNS.search(label))


#: Cap on choices carried per field (matches auto_apply_service._MAX_NEEDS_OPTIONS).
_MAX_OPTIONS = 40


def _option_labels(values: Any) -> List[str]:
    """Human labels from a Greenhouse ``values`` array, de-duped, order kept.

    Entries are ``{"label": "Yes", "value": 1}``; a bare string is tolerated so
    a shape change degrades to "fewer options", never to a crash."""
    out: List[str] = []
    seen = set()
    for v in (values or [])[:_MAX_OPTIONS] if isinstance(values, list) else []:
        if isinstance(v, dict):
            label = str(v.get("label") or v.get("value") or "").strip()
        else:
            label = str(v or "").strip()
        if label and label not in seen:
            seen.add(label)
            out.append(label[:200])
    return out


def _parse_question(q: Dict[str, Any], force_legal: bool = False) -> Optional[Dict[str, Any]]:
    """Map one Greenhouse question entry → a ReconQuestion dict, or None to drop
    it (hidden-only, executor-supplied, or malformed)."""
    if not isinstance(q, dict):
        return None
    fields = q.get("fields") or []
    non_hidden = [f for f in fields
                  if isinstance(f, dict) and f.get("type") != "input_hidden" and f.get("name")]
    if not non_hidden:
        return None

    # A question is executor-supplied if ALL of its fields are résumé/cover-letter
    # handles (the file OR its textarea fallback) — those we fill ourselves.
    def _supplied(name: str) -> bool:
        return any(name.startswith(p) for p in _EXECUTOR_SUPPLIED_PREFIXES)
    if all(_supplied(f["name"]) for f in non_hidden):
        return None

    # Primary field: prefer a real input over a file (we want the answerable
    # handle); the field name is the stable id the submit step will fill.
    primary = next((f for f in non_hidden if f.get("type") != "input_file"), non_hidden[0])
    fid = primary.get("name")
    if not fid:
        return None
    label = (q.get("label") or "").strip()
    qtype = _GH_TYPE_MAP.get(primary.get("type"), QTYPE_TEXT)
    parsed = {
        "id": fid,
        "text": label,
        "type": qtype,
        "required": bool(q.get("required")),
        "is_legal_attestation": bool(force_legal or _is_legal(label)),
    }
    # THE ENUMERATED-FIELD FIX: Greenhouse ships the accepted choices on the
    # field as ``values: [{label, value}, …]`` and we used to DROP them. Without
    # them the needs_input item reaches the FE as `value_type: select` with no
    # `options`, `needsInput.inferControl` renders a free-text box, the user
    # types prose, `application_question_bank.match_option` cannot map it to a
    # real choice, `fill_driver` returns `no_option_match` and the row re-parks
    # with the SAME question — a select question nobody (user or machine) can
    # answer. Carry the labels through; `apply_forms.normalize_recon_questions`
    # and `_needs_control_meta` already prefer the form's own options.
    opts = _option_labels(primary.get("values"))
    if opts:
        parsed["options"] = opts
    return parsed


def _parse_greenhouse(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Extract the ReconQuestion list from a Greenhouse job payload.

    Sources, all optional/defensive:
      * ``questions``            — the main application form.
      * ``compliance[].questions`` — EEOC / GDPR sections (always legal).
      * ``demographic_questions.questions`` — demographic survey (always legal).
      * ``location_questions``   — location confirmations (normal).
    """
    out: List[Dict[str, Any]] = []
    seen_ids = set()

    def _add(entries, force_legal=False):
        for q in entries or []:
            parsed = _parse_question(q, force_legal=force_legal)
            if parsed and parsed["id"] not in seen_ids:
                seen_ids.add(parsed["id"])
                out.append(parsed)
                if len(out) >= _MAX_QUESTIONS:
                    return

    _add(payload.get("questions"))
    for section in payload.get("compliance") or []:
        if isinstance(section, dict):
            _add(section.get("questions"), force_legal=True)
    dq = payload.get("demographic_questions")
    if isinstance(dq, dict):
        _add(dq.get("questions"), force_legal=True)
    _add(payload.get("location_questions"))
    return out[:_MAX_QUESTIONS]


class AtsExecutor(ExecutorAdapter):
    """Recon-real, submit-dark executor. See module docstring."""

    name = "ats-http"

    def __init__(self, session: Optional[requests.Session] = None):
        # Injectable session for tests; a module default otherwise.
        self._session = session or requests.Session()
        self._session.headers.setdefault("User-Agent", _USER_AGENT)

    # -- liveness ------------------------------------------------------------
    def is_available(self) -> bool:
        # The browser submit fleet is not deployed. Report unavailable so the
        # orchestrator never grabs a row into `submitting` — it stays `ready`.
        return False

    # -- recon (REAL for Greenhouse) ----------------------------------------
    def recon_form(self, apply_url: str,
                   ats_family: Optional[str]) -> Dict[str, Any]:
        family, ids = detect_ats(apply_url, ats_family)
        if family == GREENHOUSE and ids.get("board_token") and ids.get("job_id"):
            return self._greenhouse_recon(ids["board_token"], ids["job_id"])
        # Any other family (or a Greenhouse listing URL with no job id) needs the
        # browser recon path, which isn't deployed → clean unavailable. The
        # orchestrator leaves the row queued; nothing is fabricated.
        logger.info("ats_executor recon: unsupported for family=%s (url=%s) — unavailable",
                    family, apply_url)
        return {"questions": [], "needs_account": False, "status": EXECUTOR_UNAVAILABLE}

    def _greenhouse_recon(self, token: str, job_id: str) -> Dict[str, Any]:
        url = _GH_API.format(token=token, job_id=job_id)
        try:
            resp = self._session.get(url, timeout=_HTTP_TIMEOUT)
        except requests.RequestException as e:
            logger.warning("greenhouse recon GET failed (%s/%s): %s", token, job_id, e)
            return {"questions": [], "needs_account": False, "status": EXECUTOR_UNAVAILABLE}
        if resp.status_code != 200:
            logger.warning("greenhouse recon non-200 (%s/%s): %s", token, job_id, resp.status_code)
            return {"questions": [], "needs_account": False, "status": EXECUTOR_UNAVAILABLE}
        try:
            payload = resp.json()
        except ValueError as e:
            logger.warning("greenhouse recon bad JSON (%s/%s): %s", token, job_id, e)
            return {"questions": [], "needs_account": False, "status": EXECUTOR_UNAVAILABLE}

        questions = _parse_greenhouse(payload if isinstance(payload, dict) else {})
        # A form with zero surfaced questions is a legitimate result (résumé-only
        # apply) — NOT unavailable. Returning no `status` key = recon succeeded.
        return {"questions": questions, "needs_account": False}

    # -- submit / oracle hooks (DARK until the browser fleet lands) ----------
    def submit(self, application: Dict[str, Any], answers: Dict[str, Any],
               resume_pdf: Any) -> Dict[str, Any]:
        # Guarded upstream by is_available()==False; defensive clean shape here.
        return {"status": EXECUTOR_UNAVAILABLE, "confirmation": None,
                "screenshot_ref": None, "wall": None, "error": None}

    def create_account(self, user_email: str, ats_family: Optional[str],
                       tenant: Optional[str],
                       credentials: Dict[str, Any]) -> Dict[str, Any]:
        return {"status": EXECUTOR_UNAVAILABLE, "resolved": False,
                "wall": None, "error": None}

    def verify_state(self, application: Dict[str, Any],
                     freeze: Dict[str, Any]) -> Dict[str, Any]:
        return {"status": EXECUTOR_UNAVAILABLE, "resolved": False,
                "wall": None, "error": None}
