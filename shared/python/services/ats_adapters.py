"""Deterministic ATS adapter framework (auto-apply defect P4).

The auto-apply executor (``browser_executor`` / the P5 apply-executor worker) needs
a SMALL, honest, per-ATS contract that behaves the same way every time — no LLM in
the fill/submit path, evidence-based support labels, and a hard human-takeover
boundary. This module is that contract.

It is ADDITIVE and sits alongside the existing executor seam
(``executor_adapter.ExecutorAdapter`` + ``ats_executor`` HTTP recon +
``browser_executor`` Playwright driver), which keep working unchanged. Where those
prove the end-to-end plumbing, THIS layer is the forward, deterministic,
per-family adapter surface a worker drives one page at a time.

WHAT AN ADAPTER IS
------------------
An adapter is bound to a ``page`` (a Playwright page, or the offline
``tests/ats_dom.FakePage`` — anything exposing ``query_selector`` /
``query_selector_all`` / ``url`` / ``inner_text('body')`` and, per element,
``get_attribute`` / ``inner_text`` / ``is_visible`` / ``fill`` / ``click``) plus
its apply ``url``. Every method returns a STRUCTURED dict carrying its evidence.

THE CONTRACT (``AtsAdapter``)
    detect()                 -> {ats, confidence, evidence}   selector-based
    canonical_job_identity() -> str                           stable per posting
    recon()                  -> [NormalizedQuestion]          apply_forms field_id shape
    required_account()       -> bool                          account/login gate present
    upload_file(field, path) -> {...}                         deterministic file handle
    fill(field_id, value)    -> {...}                         deterministic, no LLM
    capture_validation_errors() -> [ {field, message, evidence} ]
    review_snapshot()        -> {fields, values, hash}        stable hash
    submit()                 -> submitted | submission_unknown | waiting_human
                                | validation_failed  (NEVER auto-retries ambiguity)
    detect_confirmation()    -> {confirmed, evidence}
    receipt()                -> structured receipt
    form_fingerprint()       -> str                           selector/structure hash
    health()                 -> {status, circuit_open, ...}
    supported_reason()       -> {supported, reason, evidence, recommended_action}

Plus a per-adapter, per-family CIRCUIT BREAKER that opens on repeated
selector/fingerprint DRIFT (a silent form template change) and flips ``health``
+ ``supported_reason`` so the worker routes to a human instead of blindly filling
a form it no longer understands.

HUMAN TAKEOVER (never bypass anti-bot):
CAPTCHA, MFA/OTP, an unsupported account-creation gate, a novel unconfirmed legal
attestation, or an unexpected form change all resolve to a ``waiting_human``
outcome that carries a review snapshot of the exact pending action. This module
NEVER integrates CAPTCHA-solving or anti-bot bypass, and LinkedIn is explicitly
never automated.

SUPPORT STATUS (evidence-based — we do NOT claim "works on any website"):
    greenhouse / lever / ashby   supported (deterministic selectors verified)
    workday                      supported WITH a caveat — a deterministic,
                                 data-automation-id-keyed multi-step wizard adapter
                                 that is candidate-ACCOUNT gated, so it FREQUENTLY
                                 routes to a human (account creation, EEO/self-
                                 identify steps). NOT full autonomy — evidence-based.
    generic                      NOT supported — may prepare/fill, defaults to human review
    linkedin                     never automated (policy)
"""
from __future__ import annotations

import abc
import hashlib
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from services.ats_detector import (
    detect_ats, GREENHOUSE, LEVER, ASHBY, WORKDAY,
)
from services.ats_executor import _is_legal
from services.executor_adapter import (
    QTYPE_TEXT, QTYPE_TEXTAREA, QTYPE_SELECT, QTYPE_CHECKBOX, QTYPE_RADIO,
    QTYPE_FILE, QTYPE_DATE,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Outcome / health / takeover vocabulary
# ---------------------------------------------------------------------------
ADAPTER_SUBMITTED = "submitted"
ADAPTER_SUBMISSION_UNKNOWN = "submission_unknown"   # clicked, no confirm/no error
ADAPTER_VALIDATION_FAILED = "validation_failed"     # form blocked pre/post submit
ADAPTER_WAITING_HUMAN = "waiting_human"             # safe pause for a person

HEALTH_HEALTHY = "healthy"
HEALTH_DEGRADED = "degraded"
HEALTH_OPEN = "open"

# Human-takeover reasons (why we paused instead of acting).
TAKEOVER_CAPTCHA = "captcha"
TAKEOVER_MFA = "mfa"
TAKEOVER_ACCOUNT_REQUIRED = "account_required"
TAKEOVER_NOVEL_LEGAL = "novel_legal_attestation"
TAKEOVER_FORM_CHANGED = "form_change"
TAKEOVER_UNSUPPORTED = "unsupported"
TAKEOVER_ADAPTER_DISABLED = "adapter_disabled"     # operator kill-switch (P1-KILL)

_MAX_QUESTIONS = 120


# ---------------------------------------------------------------------------
# Per-adapter operational kill switch (audit P1-KILL). An operator can dark a
# single ATS family WITHOUT a deploy by listing it (csv) in the config value
# ``AUTO_APPLY_ADAPTERS_DISABLED`` (e.g. "greenhouse,workday"). A disabled family
# reports unsupported with a ``circuit_open`` reason and its ``submit`` routes to
# a human — the in-process CircuitBreaker stays as the AUTOMATIC (drift-triggered)
# layer; this is the MANUAL one. Read defensively so a config failure can only
# leave the adapter ENABLED (never accidentally dark the whole fleet).
# ---------------------------------------------------------------------------
def adapter_disabled(family: Optional[str]) -> bool:
    fam = (family or "").strip().lower()
    if not fam:
        return False
    try:
        from utils.config import _get_config_value
        raw = _get_config_value("AUTO_APPLY_ADAPTERS_DISABLED", "") or ""
    except Exception:  # pragma: no cover - defensive
        return False
    disabled = {s.strip().lower() for s in raw.split(",") if s.strip()}
    return fam in disabled

# HTML input ``type`` -> the executor QTYPE_* vocabulary.
_INPUT_TYPE_MAP = {
    "text": QTYPE_TEXT, "email": QTYPE_TEXT, "tel": QTYPE_TEXT, "url": QTYPE_TEXT,
    "search": QTYPE_TEXT, "number": QTYPE_TEXT, "password": QTYPE_TEXT,
    "checkbox": QTYPE_CHECKBOX, "radio": QTYPE_RADIO, "file": QTYPE_FILE,
    "date": QTYPE_DATE,
}

# Fields the EXECUTOR supplies (résumé / cover letter), never surfaced as
# answerable questions and never a blocker for the applicant.
_EXECUTOR_SUPPLIED = ("resume", "cover_letter", "cover-letter", "_systemfield_resume")

# Confirmation surface (post-submit success).
_CONFIRM_SELECTORS = (
    "#application-confirmation", "#application_confirmation",
    "[class*=confirmation]", "[data-testid*=confirmation]", "[role=status]",
)
_CONFIRM_RE = re.compile(
    r"(thank you for applying|application (has been |was )?(submitted|received|sent)|"
    r"we[’'`]?ve received your application|successfully submitted)", re.IGNORECASE)

# CAPTCHA / MFA walls. We DETECT to pause — we never solve or bypass.
_CAPTCHA_SELECTORS = (
    "iframe[src*=bframe]", "iframe[src*=hcaptcha]", "iframe[src*=recaptcha]",
    ".g-recaptcha", "[data-sitekey]",
)
_MFA_RE = re.compile(
    r"(verify your email|confirm your email|check your (inbox|email)|"
    r"one[-\s]?time (code|password)|\botp\b|enter the (code|verification code)|"
    r"two[-\s]?factor|multi[-\s]?factor|authenticator app)", re.IGNORECASE)

# Account/login gate signals (text, in addition to a password field).
_ACCOUNT_RE = re.compile(
    r"(create an account|sign in to apply|log ?in to apply|register to apply|"
    r"create your account|already have an account)", re.IGNORECASE)

# The "with email" control on a federated auth panel. Must NOT match the
# identity-provider buttons that sit next to it ("Sign in with Apple/Google/
# LinkedIn") — clicking one of those would hand the employer an OAuth flow we
# have no account for and cannot complete.
_EMAIL_AUTH_RE = re.compile(
    r"((?:sign|log)\s*(?:in|up)|continue|register)\s+with\s+(?:an?\s+)?email"
    r"|use\s+(?:my\s+|an?\s+)?email"
    r"|email\s+and\s+password", re.IGNORECASE)


# ===========================================================================
# Per-family circuit breaker (opens on repeated selector/fingerprint drift)
# ===========================================================================
class CircuitBreaker:
    """Trips OPEN after ``threshold`` drift events for one ATS family; a clean
    recon slowly heals the failure count but never force-closes an open breaker
    (an operator/redeploy resets it). Shared across adapter instances of the same
    family so drift observed across a fleet accumulates."""

    def __init__(self, family: str, threshold: int = 3):
        self.family = family
        self.threshold = threshold
        self.failures = 0
        self._open = False
        self._evidence: List[Dict[str, Any]] = []

    def record_drift(self, evidence: Optional[Dict[str, Any]] = None) -> None:
        self.failures += 1
        if evidence:
            self._evidence.append(evidence)
        if self.failures >= self.threshold:
            self._open = True

    def record_ok(self) -> None:
        if not self._open and self.failures > 0:
            self.failures -= 1

    def is_open(self) -> bool:
        return self._open

    def evidence(self) -> List[Dict[str, Any]]:
        return list(self._evidence)

    def reset(self) -> None:
        self.failures = 0
        self._open = False
        self._evidence = []


_BREAKERS: Dict[str, CircuitBreaker] = {}


def get_circuit_breaker(family: str) -> CircuitBreaker:
    b = _BREAKERS.get(family)
    if b is None:
        b = CircuitBreaker(family)
        _BREAKERS[family] = b
    return b


def reset_circuit_breakers() -> None:
    """Test/operational helper — clear every family's breaker."""
    for b in _BREAKERS.values():
        b.reset()


# ===========================================================================
# The adapter base class
# ===========================================================================
class AtsAdapter(abc.ABC):
    """Deterministic per-ATS adapter. See module docstring for the full contract.

    Subclasses declare detection signatures (``_HOST_NEEDLES`` / ``_DOM_SIGNALS``)
    and a capability claim (``supported``); everything else — recon, fill,
    snapshot, submit boundary, confirmation, receipt, fingerprint, health,
    takeover — is shared and page-abstraction based.
    """

    family: str = "abstract"
    supported: bool = False
    MIN_CONFIDENCE: float = 0.75
    NEVER_AUTOMATE: bool = False
    STUB_REASON: Optional[str] = None

    _HOST_NEEDLES: Tuple[str, ...] = ()
    #: (css_selector, evidence_label) DOM signatures that raise confidence.
    _DOM_SIGNALS: Tuple[Tuple[str, str], ...] = ()

    def __init__(self, page: Any = None, url: str = "", *,
                 baseline_fingerprint: Optional[str] = None):
        self._page = page
        self._url = url or (getattr(page, "url", "") or "")
        self._filled: Dict[str, Any] = {}
        self._uploads: Dict[str, str] = {}
        self._questions: List[Dict[str, Any]] = []
        self._fingerprint: Optional[str] = None
        self._baseline_fp = baseline_fingerprint
        self._submit_clicks = 0
        self._breaker = get_circuit_breaker(self.family)

    # -- page access helpers (defensive; work for Playwright AND the harness) --
    def _qs(self, css: str):
        try:
            return self._page.query_selector(css) if self._page is not None else None
        except Exception:
            return None

    def _qsa(self, css: str) -> List[Any]:
        try:
            return (self._page.query_selector_all(css) or []) if self._page is not None else []
        except Exception:
            return []

    @staticmethod
    def _attr(el, name: str) -> Optional[str]:
        try:
            return el.get_attribute(name)
        except Exception:
            return None

    @staticmethod
    def _text(el) -> str:
        try:
            return (el.inner_text() or "").strip()
        except Exception:
            return ""

    @staticmethod
    def _visible(el) -> bool:
        try:
            return bool(el.is_visible())
        except Exception:
            return True

    def _body_text(self) -> str:
        if self._page is None:
            return ""
        for arg in (("body",), ()):
            try:
                return self._page.inner_text(*arg) or ""
            except Exception:
                continue
        return ""

    # =====================================================================
    # Detection
    # =====================================================================
    @classmethod
    def sniff(cls, page: Any, url: str) -> Dict[str, Any]:
        """Selector + host based detection with a confidence score and the list
        of evidence signals that produced it. Host match = 0.6; each DOM
        signature = +0.2 (cap 3). Pure read, never raises."""
        evidence: List[str] = []
        conf = 0.0
        host = _host(url)
        if host and any(n in host for n in cls._HOST_NEEDLES):
            conf += 0.6
            evidence.append("host:%s" % host)
        hits = 0
        for sel, label in cls._DOM_SIGNALS:
            if _page_has(page, sel):
                hits += 1
                evidence.append("dom:%s" % label)
        conf += min(hits, 3) * 0.2
        return {"ats": cls.family, "confidence": round(min(conf, 0.99), 3),
                "evidence": evidence}

    def detect(self) -> Dict[str, Any]:
        return self.sniff(self._page, self._url)

    def canonical_job_identity(self, url: Optional[str] = None) -> str:
        """A stable per-posting identity: ``greenhouse:{token}:{job_id}`` /
        ``lever:{site}:{posting}`` / ``ashby:{org}:{posting}``, else
        ``{family}:{sha1(url)[:12]}``. Deterministic."""
        u = url or self._url or ""
        declared = None if self.family in ("generic", "abstract") else self.family
        fam, ids = detect_ats(u, declared)
        fam = fam or self.family or "unknown"
        if fam == GREENHOUSE and ids.get("board_token") and ids.get("job_id"):
            return "greenhouse:%s:%s" % (ids["board_token"], ids["job_id"])
        if fam == LEVER and ids.get("site") and ids.get("posting_id"):
            return "lever:%s:%s" % (ids["site"], ids["posting_id"])
        if fam == ASHBY and ids.get("org") and ids.get("posting_id"):
            return "ashby:%s:%s" % (ids["org"], ids["posting_id"])
        return "%s:%s" % (fam, hashlib.sha1(u.encode("utf-8")).hexdigest()[:12])

    # =====================================================================
    # Recon (-> normalized field_id questions) + fingerprint / drift
    # =====================================================================
    def recon(self) -> List[Dict[str, Any]]:
        """Walk the form deterministically and return the normalized
        ``field_id``-keyed questions (apply_forms shape). Also (re)computes the
        form fingerprint and records drift against a pinned baseline."""
        from services.apply_forms import normalize_recon_questions

        raw = self._extract_raw_fields()
        self._fingerprint = self._compute_fingerprint(raw)
        self._note_fingerprint(self._fingerprint)
        self._questions = normalize_recon_questions(raw)[:_MAX_QUESTIONS]
        return self._questions

    def _extract_raw_fields(self) -> List[Dict[str, Any]]:
        raw: List[Dict[str, Any]] = []
        seen = set()
        typed: List[Tuple[Any, str]] = []
        for el in self._qsa("select"):
            typed.append((el, "select"))
        for el in self._qsa("textarea"):
            typed.append((el, "textarea"))
        for el in self._qsa("input"):
            typed.append((el, "input"))
        for el, tag in typed:
            handle = (self._attr(el, "id") or self._attr(el, "name") or "").strip()
            if not handle or handle in seen:
                continue
            itype = (self._attr(el, "type") or "text").lower()
            if tag == "input" and itype in ("hidden", "submit", "button", "reset", "image"):
                continue
            # Executor-supplied résumé / cover-letter file handles → drop.
            if itype == "file" or handle.startswith(_EXECUTOR_SUPPLIED):
                continue
            seen.add(handle)
            qtype = (QTYPE_SELECT if tag == "select"
                     else QTYPE_TEXTAREA if tag == "textarea"
                     else _INPUT_TYPE_MAP.get(itype, QTYPE_TEXT))
            label = self._label_for(el, handle)
            required = (self._attr(el, "required") is not None
                        or self._attr(el, "aria-required") == "true")
            raw.append({
                "id": handle, "text": label, "type": qtype,
                "required": bool(required), "is_legal_attestation": _is_legal(label),
            })
            if len(raw) >= _MAX_QUESTIONS:
                break
        return raw

    def _label_for(self, el, handle: str) -> str:
        """Deterministic label resolution: <label for=id> → aria-label →
        placeholder → the field handle."""
        lab = self._qs('label[for="%s"]' % handle)
        if lab is not None:
            t = self._text(lab)
            if t:
                return t
        for a in ("aria-label", "placeholder", "title"):
            v = self._attr(el, a)
            if v and v.strip():
                return v.strip()
        return handle

    # -- fingerprint / drift -------------------------------------------------
    def form_fingerprint(self) -> str:
        if self._fingerprint is None:
            self._fingerprint = self._compute_fingerprint(self._extract_raw_fields())
        return self._fingerprint

    def _compute_fingerprint(self, raw: List[Dict[str, Any]]) -> str:
        sig = sorted((r["id"], r["type"], bool(r["required"])) for r in raw)
        submit = bool(self._qs("button[type=submit]") or self._qs("input[type=submit]")
                      or self._qs("button"))
        payload = json.dumps({"family": self.family, "fields": sig, "submit": submit},
                             sort_keys=True, separators=(",", ":"))
        return "fp_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]

    def _note_fingerprint(self, fp: str) -> None:
        """Compare to the pinned baseline (if any). A mismatch is a drift event
        (the form changed under us); repeated drift opens the circuit."""
        if not self._baseline_fp:
            return
        if fp == self._baseline_fp:
            self._breaker.record_ok()
        else:
            self._breaker.record_drift({"expected": self._baseline_fp, "got": fp,
                                        "url": self._url})
            logger.warning("ats_adapter drift family=%s expected=%s got=%s",
                           self.family, self._baseline_fp, fp)

    # =====================================================================
    # Deterministic fill / upload (NO LLM — a model may propose upstream)
    # =====================================================================
    def _canonical_key(self, key: str) -> str:
        """Normalize an incoming fill/upload key to the canonical ``field_id`` so a
        caller may address a field by EITHER its normalized field_id or its raw
        recon handle and the snapshot / legal-attestation checks stay consistent."""
        for q in self._questions:
            if q.get("field_id") == key:
                return key
            if q.get("recon_id") == key:
                return q.get("field_id") or key
        return key

    def _handle_for(self, field_id: str) -> str:
        for q in self._questions:
            if q.get("field_id") == field_id:
                return q.get("recon_id") or field_id
            if q.get("recon_id") == field_id:
                return field_id
        return self._canonical_fallback_handle(field_id)

    def _canonical_fallback_handle(self, field_id: str) -> str:
        """Resolve a CANONICAL field_id onto a recon handle when recon never
        classified the field.

        A Workday control discovered mid-wizard often has no visible label, so
        recon keeps the raw automation-id as its ``field_id``
        (``socialNetworkAccounts--linkedInAccount``). The answer map is keyed
        canonically (``linkedin_url``), the two never intersect, and a fully
        answered application parks asking for something already declared.

        ``normalize_field`` maps that handle correctly on its own, so use it as
        a last resort. Returns ``field_id`` unchanged when nothing matches —
        never guesses a control."""
        try:
            from services import application_question_bank as qb
        except Exception:  # pragma: no cover - defensive
            return field_id
        for q in self._questions:
            for raw in (q.get("recon_id"), q.get("field_id"), q.get("label")):
                if not raw:
                    continue
                try:
                    if qb.normalize_field(str(raw), allow_llm=False
                                          ).get("field_id") == field_id:
                        return str(q.get("recon_id") or raw)
                except Exception:
                    continue
        return field_id

    def _find_element(self, handle: str):
        if not handle:
            return None
        for el in self._qsa("input") + self._qsa("select") + self._qsa("textarea"):
            if (self._attr(el, "id") == handle) or (self._attr(el, "name") == handle):
                return el
        return None

    def fill(self, field_id: str, value: Any) -> Dict[str, Any]:
        """Deterministically record ``value`` for ``field_id`` and (best-effort)
        set it on the live element. ``self._filled`` is the source of truth for the
        review snapshot — honest about what WE put in, independent of the DOM."""
        key = self._canonical_key(field_id)
        handle = self._handle_for(key)
        el = self._find_element(handle)
        if el is not None:
            try:
                el.fill(value)
            except Exception as e:  # pragma: no cover - defensive
                logger.info("fill set failed %s: %s", field_id, e)
        self._filled[key] = value
        return {"field_id": key, "handle": handle,
                "found": el is not None, "value": value}

    def upload_file(self, field: str, path: str) -> Dict[str, Any]:
        """Record a résumé/attachment handle deterministically (basename only —
        no file bytes travel through the snapshot)."""
        name = os.path.basename(str(path or "")) or "attachment"
        key = self._canonical_key(field)
        self._uploads[key] = name
        el = self._find_element(self._handle_for(key))
        return {"field": key, "filename": name, "found": el is not None}

    # -- live option enumeration (for the fill driver's option matcher) ------
    def options_for(self, field_id: str) -> List[str]:
        """Enumerate a select/radio field's CURRENT option texts, read LIVE from
        the page (Playwright ElementHandle or the offline harness both expose
        ``query_selector_all`` on an element). Returns [] for a text field, a
        custom (non-``<select>``) widget, or an unresolved handle — the driver
        then fills the raw value rather than option-matching. Deterministic, no
        LLM, never invents an option."""
        handle = self._handle_for(self._canonical_key(field_id))
        el = self._find_element(handle)
        if el is None:
            return []
        opts: List[str] = []
        try:
            for opt in (el.query_selector_all("option") or []):
                t = self._text(opt)
                if t and t not in opts:
                    opts.append(t)
        except Exception:
            return []
        return opts

    # -- multi-step wizard defaults (single-step families are terminal) ------
    def current_step(self) -> str:
        """Wizard step name. Single-step families (Greenhouse/Lever/Ashby) have
        exactly one step → ``"single"``. Overridden by the Workday adapter."""
        return "single"

    def has_next_step(self) -> bool:
        """Whether more wizard steps follow (drives the driver's advance loop).
        Single-step families never advance. Overridden by Workday."""
        return False

    def advance_step(self) -> bool:
        """Advance to the next wizard step, returning whether an advance control
        was actuated. Single-step families never advance. Overridden by Workday."""
        return False

    # =====================================================================
    # Validation capture / review snapshot
    # =====================================================================
    # An accessible uploader announces completion through `role=alert` — the same
    # channel validation errors use. Treating that as an error made a SUCCESSFUL
    # résumé upload abort the submit (see _is_success_notice's callers).
    _SUCCESS_NOTICE_RE = re.compile(
        r"(successfully|success\b|uploaded|attached|saved|complete[d]?\b|"
        r"received|thank you)", re.IGNORECASE)
    _ERROR_SIGNAL_RE = re.compile(
        r"(error|invalid|required|must |cannot|can't|failed|"
        r"missing|not valid|please (enter|select|provide|check|correct))",
        re.IGNORECASE)

    @classmethod
    def _is_success_notice(cls, text: str) -> bool:
        """True when a captured message reads as SUCCESS and carries no error
        signal. Conservative on purpose: any error wording anywhere in the text
        keeps the message. Refusing to submit on a false error is recoverable;
        submitting past a real one is not."""
        t = (text or "").strip()
        if not t:
            return False
        if cls._ERROR_SIGNAL_RE.search(t):
            return False
        return bool(cls._SUCCESS_NOTICE_RE.search(t))

    def capture_validation_errors(self) -> List[Dict[str, Any]]:
        """Collect VISIBLE field-level validation errors (aria-invalid controls +
        shown error messages). Never invents an error."""
        out: List[Dict[str, Any]] = []
        seen = set()
        for sel in ("input[aria-invalid=true]", "select[aria-invalid=true]",
                    "textarea[aria-invalid=true]"):
            for el in self._qsa(sel):
                if not self._visible(el):
                    continue
                handle = self._attr(el, "id") or self._attr(el, "name") or "field"
                key = ("aria", handle)
                if key in seen:
                    continue
                seen.add(key)
                out.append({"field": handle, "message": "required/invalid",
                            "evidence": "aria-invalid"})
        for sel in (".field-error", "[class*=error-message]", "[role=alert]"):
            for el in self._qsa(sel):
                if not self._visible(el):
                    continue
                txt = self._text(el)
                if not txt or ("msg", txt) in seen:
                    continue
                seen.add(("msg", txt))
                if self._is_success_notice(txt):
                    # e.g. "resume.pdf successfully uploaded" on [role=alert].
                    continue
                out.append({"field": None, "message": txt[:200], "evidence": sel})
        return out

    def review_snapshot(self) -> Dict[str, Any]:
        """A stable, hashable record of exactly what the adapter would submit:
        the sorted field ids, their values (uploads shown as ``<file:name>``), and
        a sha256 over the canonical JSON. Same inputs → same hash."""
        values: Dict[str, str] = {k: ("" if v is None else str(v))
                                  for k, v in self._filled.items()}
        for k, name in self._uploads.items():
            values[k] = "<file:%s>" % name
        fields = sorted(values.keys())
        canonical = json.dumps({"fields": fields,
                                "values": {k: values[k] for k in fields}},
                               sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return {"fields": fields, "values": {k: values[k] for k in fields},
                "hash": digest}

    # =====================================================================
    # Human-takeover detection (never bypass anti-bot)
    # =====================================================================
    def required_account(self) -> bool:
        """True when an account/login gate stands in front of the form (a password
        field, or explicit create-account/sign-in-to-apply copy)."""
        if self._qs("input[type=password]") is not None:
            return True
        return bool(_ACCOUNT_RE.search(self._body_text()))

    def _captcha_present(self) -> bool:
        """A VISIBLE challenge surface. Uses a size hint when available (a real
        image/checkbox challenge is sizable), else falls back to visibility. We
        DETECT to pause — never solve."""
        for sel in _CAPTCHA_SELECTORS:
            for el in self._qsa(sel):
                if not self._visible(el):
                    continue
                try:
                    box = el.bounding_box()
                except Exception:
                    box = None
                if box is None or (box.get("width", 0) >= 40 and box.get("height", 0) >= 40):
                    return True
        return False

    def _check_takeover(self) -> Optional[Dict[str, Any]]:
        """Return a takeover descriptor (reason + evidence) when the page must be
        handed to a human, else None. Order: form-drift → captcha → mfa →
        account gate → novel unconfirmed legal attestation."""
        if self._breaker.is_open():
            return {"reason": TAKEOVER_FORM_CHANGED,
                    "evidence": self._breaker.evidence() or ["circuit_open"]}
        if self._captcha_present():
            return {"reason": TAKEOVER_CAPTCHA, "evidence": ["captcha_challenge_visible"]}
        body = self._body_text()
        if _MFA_RE.search(body):
            return {"reason": TAKEOVER_MFA, "evidence": ["mfa_or_otp_prompt"]}
        if self.required_account():
            return {"reason": TAKEOVER_ACCOUNT_REQUIRED,
                    "evidence": ["account_or_login_gate"]}
        novel = self._novel_legal_attestation()
        if novel:
            return {"reason": TAKEOVER_NOVEL_LEGAL, "evidence": novel}
        return None

    def _novel_legal_attestation(self) -> List[str]:
        """Required legal attestations from recon that WE have not been given a
        vetted answer for. A legal attestation may only be answered from the
        user's declared data upstream — never guessed here — so an unfilled,
        required one must pause for a person."""
        out = []
        for q in self._questions:
            if (q.get("is_legal_attestation") and q.get("required")
                    and q.get("field_id") not in self._filled):
                out.append(q.get("field_id"))
        return out

    # =====================================================================
    # Submit boundary + confirmation + receipt
    # =====================================================================
    def detect_confirmation(self) -> Dict[str, Any]:
        """Look for a VISIBLE, non-empty confirmation node and/or confirmation
        body text. Returns {confirmed, evidence}. An empty/hidden confirmation
        placeholder does NOT count."""
        evidence: List[Dict[str, Any]] = []
        for sel in _CONFIRM_SELECTORS:
            for el in self._qsa(sel):
                if self._visible(el):
                    t = self._text(el)
                    if t:
                        evidence.append({"selector": sel, "text": t[:200]})
                        break
        m = _CONFIRM_RE.search(self._body_text())
        if m:
            evidence.append({"text_match": m.group(0)[:120]})
        return {"confirmed": bool(evidence), "evidence": evidence}

    def _click_submit(self) -> bool:
        """Click a single submit control exactly ONCE. Returns whether one was
        found. Never loops / never retries."""
        for el in self._qsa("button[type=submit]") + self._qsa("input[type=submit]"):
            self._submit_clicks += 1
            try:
                el.click()
            except Exception:
                pass
            return True
        # Fall back to a plain button whose label reads submit/apply.
        for el in self._qsa("button"):
            if any(w in self._text(el).lower() for w in ("submit", "apply", "send")):
                self._submit_clicks += 1
                try:
                    el.click()
                except Exception:
                    pass
                return True
        return False

    def submit(self) -> Dict[str, Any]:
        """The final boundary. Pre-flight takeover checks → click ONCE → classify.
        Returns exactly one of: waiting_human, validation_failed, submitted, or
        submission_unknown. NEVER auto-retries an ambiguous (unknown) result — the
        caller re-verifies out of band."""
        # Operator kill-switch (P1-KILL): a darked family never autonomously
        # submits — it routes to a human, exactly like an open circuit.
        if adapter_disabled(self.family):
            return self._human(TAKEOVER_ADAPTER_DISABLED,
                               ["operator kill-switch: %s disabled" % self.family])
        if not self.supported:
            # Generic / stub / never-automate families never autonomously submit.
            reason = (TAKEOVER_UNSUPPORTED if not self.NEVER_AUTOMATE
                      else TAKEOVER_UNSUPPORTED)
            return self._human(reason,
                               [self.STUB_REASON or "%s: human review/takeover" % self.family])

        pre = self._check_takeover()
        if pre is not None:
            return self._human(pre["reason"], pre["evidence"])

        # A visible, blocking validation error already on the form → do not click.
        pre_errs = self.capture_validation_errors()
        if pre_errs:
            return self._validation_failed(pre_errs)

        self._click_submit()

        post = self._check_takeover()
        if post is not None:
            return self._human(post["reason"], post["evidence"])

        conf = self.detect_confirmation()
        if conf["confirmed"]:
            return {"status": ADAPTER_SUBMITTED, "takeover": None,
                    "confirmation": conf, "review_snapshot": self.review_snapshot(),
                    "receipt": self.receipt(confirmed=True, confirmation=conf)}

        errs = self.capture_validation_errors()
        if errs:
            return self._validation_failed(errs)

        # Clicked, but neither confirmed nor errored → genuinely unknown. Report
        # it and STOP; the orchestrator re-verifies rather than blindly re-submits.
        return {"status": ADAPTER_SUBMISSION_UNKNOWN, "takeover": None,
                "confirmation": None, "review_snapshot": self.review_snapshot(),
                "receipt": self.receipt(confirmed=False)}

    def receipt(self, confirmed: bool = False,
                confirmation: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """A structured, deterministic receipt of the submission attempt (no wall
        clock — the caller stamps time so the hash stays stable in tests)."""
        snap = self.review_snapshot()
        return {
            "adapter": self.family,
            "job_identity": self.canonical_job_identity(),
            "url": self._url,
            "form_fingerprint": self.form_fingerprint(),
            "confirmed": bool(confirmed),
            "confirmation_evidence": (confirmation or {}).get("evidence", []) if confirmed else [],
            "fields_submitted": snap["fields"],
            "uploads": sorted(self._uploads.keys()),
            "snapshot_hash": snap["hash"],
        }

    # -- outcome builders ----------------------------------------------------
    def _human(self, reason: str, evidence: Any) -> Dict[str, Any]:
        return {
            "status": ADAPTER_WAITING_HUMAN,
            "takeover": {"reason": reason,
                         "evidence": evidence if isinstance(evidence, list) else [evidence]},
            "confirmation": None,
            "review_snapshot": self.review_snapshot(),
            "pending_action": {"action": "submit", "url": self._url,
                               "job_identity": self.canonical_job_identity(),
                               "reason": reason},
            "receipt": None,
        }

    def _validation_failed(self, errors: List[Dict[str, Any]]) -> Dict[str, Any]:
        return {"status": ADAPTER_VALIDATION_FAILED, "takeover": None,
                "confirmation": None, "errors": errors,
                "review_snapshot": self.review_snapshot(), "receipt": None}

    # =====================================================================
    # Health + evidence-based support label
    # =====================================================================
    def health(self) -> Dict[str, Any]:
        disabled = adapter_disabled(self.family)
        if disabled or self._breaker.is_open():
            status = HEALTH_OPEN
        elif self._breaker.failures > 0:
            status = HEALTH_DEGRADED
        else:
            status = HEALTH_HEALTHY
        return {"family": self.family, "status": status,
                "circuit_open": self._breaker.is_open() or disabled,
                "disabled": disabled,
                "drift_count": self._breaker.failures}

    def supported_reason(self) -> Dict[str, Any]:
        """Evidence-based support verdict. We do NOT market "works on any site":
        an unsupported family, a policy block, an open circuit, or low detection
        confidence all return supported=False with a concrete reason."""
        if self.NEVER_AUTOMATE:
            return {"supported": False,
                    "reason": "policy: %s is never automated" % self.family,
                    "evidence": [], "recommended_action": "human_takeover"}
        if adapter_disabled(self.family):
            # Operator kill-switch (P1-KILL) — reported as a circuit_open so the
            # existing "route to human" plumbing (worker + presubmit) honors it.
            return {"supported": False,
                    "reason": "circuit_open: %s operationally disabled "
                              "(operator kill-switch)" % self.family,
                    "evidence": ["adapter_disabled"],
                    "recommended_action": "human_takeover"}
        if not self.supported:
            return {"supported": False,
                    "reason": self.STUB_REASON or
                    ("%s: no deterministic adapter — human review/takeover recommended"
                     % self.family),
                    "evidence": [], "recommended_action": "human_review"}
        if self._breaker.is_open():
            return {"supported": False,
                    "reason": "circuit_open: repeated selector/form-fingerprint drift",
                    "evidence": self._breaker.evidence(),
                    "recommended_action": "human_takeover"}
        det = self.detect()
        if det["confidence"] < self.MIN_CONFIDENCE:
            return {"supported": False,
                    "reason": "low detection confidence %.2f" % det["confidence"],
                    "evidence": det["evidence"], "recommended_action": "human_review"}
        return {"supported": True,
                "reason": "%s deterministic adapter (confidence %.2f)"
                % (self.family, det["confidence"]),
                "evidence": det["evidence"], "recommended_action": "autonomous_fill"}


# ===========================================================================
# Concrete adapters
# ===========================================================================
class GreenhouseAdapter(AtsAdapter):
    family = GREENHOUSE
    supported = True
    _HOST_NEEDLES = ("greenhouse.io", "grnh.se")
    _DOM_SIGNALS = (
        ("#application-confirmation", "confirmation-node"),
        ("#application_confirmation", "confirmation-node"),
        ("form[action*=greenhouse]", "form-action"),
        ("#s3_upload_for_resume", "s3-resume-upload"),
        (".select__control", "react-select"),
        ("#first_name", "first-name-id"),
    )


class LeverAdapter(AtsAdapter):
    family = LEVER
    supported = True
    _HOST_NEEDLES = ("lever.co",)
    _DOM_SIGNALS = (
        (".application-question", "application-question"),
        ("form[action*=lever]", "form-action"),
        (".application-form", "application-form"),
        (".postings-btn", "postings-btn"),
    )


class AshbyAdapter(AtsAdapter):
    family = ASHBY
    supported = True
    _HOST_NEEDLES = ("ashbyhq.com",)
    _DOM_SIGNALS = (
        ("[id^=_systemfield_]", "systemfield-id"),
        ("[class*=ashby]", "ashby-class"),
        ("form[action*=ashby]", "form-action"),
    )


class WorkdayAdapter(AtsAdapter):
    """Deterministic Workday adapter — supported WITH an honest caveat.

    Workday is a per-tenant, THEMED, MULTI-STEP wizard (My Information → My
    Experience → Application Questions → Voluntary Disclosures (EEO) → Self-Identify
    (disability CC-305) → Review) sitting behind a candidate-ACCOUNT gate. Two facts
    shape this adapter:

      1. THEMING VARIES PER TENANT, so we key EVERYTHING off ``data-automation-id``
         (Workday's stable, theme-independent automation hook) — never CSS classes.
      2. It is almost always ACCOUNT-GATED. ``required_account()`` is therefore a
         hard TRUE (capability fact), and whenever the login / create-account gate
         is actually on screen we PAUSE ``waiting_human`` with a clear pending
         action. We NEVER auto-create an account here (that is the ats_credential /
         ats_accounts path, out of scope) and never bypass anti-bot.

    So it is ``supported = True`` (deterministic fill/submit, no LLM) but its
    ``supported_reason`` carries the caveat that the account gate + EEO/self-identify
    steps FREQUENTLY route to a human — we do NOT claim full autonomy.
    """
    family = WORKDAY
    supported = True
    MIN_CONFIDENCE = 0.6
    _HOST_NEEDLES = ("myworkdayjobs.com", "myworkdaysite.com", "myworkday.com")
    #: Detection leans on the stable ``data-automation-id`` hook (theme-independent),
    #: NOT tenant CSS classes. Multiple automation-id signals let a Workday page be
    #: recognized by DOM signature alone (host-less), across any tenant theme.
    _DOM_SIGNALS = (
        ("[data-automation-id]", "workday-automation-id"),
        ("[data-automation-id=pageFooterNextButton]", "workday-wizard-next"),
        ("[data-automation-id=pageFooterSubmitButton]", "workday-wizard-submit"),
        ("[data-automation-id*=formField]", "workday-form-field"),
    )

    _CAVEAT = ("multi-step wizard + candidate-account gate — frequently routes to "
               "human takeover (account creation, EEO / self-identify steps); "
               "NOT full autonomy")

    # Wizard step markers (data-automation-id of the active step panel) → step name.
    _STEP_MARKERS = (
        ("applyFlowMyInfoPage", "my_information"),
        ("myInformationPage", "my_information"),
        ("applyFlowMyExperiencePage", "my_experience"),
        ("myExperiencePage", "my_experience"),
        ("applyFlowQuestionnairePage", "application_questions"),
        ("questionnairePage", "application_questions"),
        ("applyFlowVoluntaryDisclosuresPage", "voluntary_disclosures"),
        ("voluntaryDisclosuresPage", "voluntary_disclosures"),
        ("applyFlowSelfIdentificationPage", "self_identify"),
        ("selfIdentificationPage", "self_identify"),
        ("applyFlowReviewPage", "review"),
        ("reviewPage", "review"),
    )

    # Automation ids that mark the candidate account / sign-in gate.
    _ACCOUNT_AUTOMATION_IDS = (
        "signInContent", "signInFormo", "createAccountLink", "signInLink",
        "createAccountSubmitButton", "signInSubmitButton", "backToSignInLink",
        "createAccountHeader", "authView", "loginPanel",
    )

    # The "…or sign in with email" escape hatch on a FEDERATED auth panel.
    # Newer Workday tenants (NXP, State Street, Midland States Bank — all
    # observed live on 2026-07-28) render ONLY social buttons ("Sign in with
    # Apple / Google / LinkedIn / email") on the Create Account/Sign In step:
    # there is NO email input and NO password input in the DOM until the email
    # option is chosen. `fill_account_credentials` therefore found nothing to
    # fill and returned False, `_account_gate_present` stayed True, and the row
    # parked on `account_required` forever. These reveal the credential form.
    _EMAIL_AUTH_AUTOMATION_IDS = (
        "signInWithEmail", "signUpWithEmail", "emailSignInLink",
        "useEmailLink", "emailAuthLink",
    )

    # OTP / email-verify controls (theme-independent automation hooks).
    _OTP_AUTOMATION_IDS = (
        "otp", "otpCode", "otpInput", "verificationCode", "verifyCode",
        "securityCode", "emailVerificationCode", "oneTimePassword",
        "passcode", "mfaCode", "authCode",
    )

    # Workday job path: /job/[location/]{title_or_slug}  → req id trails after '_'.
    _WD_JOB_RE = re.compile(r"/job/(?:[^/]+/)*([^/?#]+)", re.IGNORECASE)

    # =====================================================================
    # Detection / identity
    # =====================================================================
    def canonical_job_identity(self, url: Optional[str] = None) -> str:
        """``workday:{tenant}:{job_req_id}`` parsed from the Workday job path — the
        tenant is the leading host label, the req id trails the last ``/job/``
        segment (after a ``_`` when the segment is ``Title_REQID``). Falls back to
        the base ``{family}:{sha1}`` when either cannot be parsed. Deterministic."""
        u = url or self._url or ""
        host = _host(u)
        tenant = ""
        if host:
            first = host.split(".")[0]
            if first not in ("www", "careers", "wd1", "wd2", "wd3", "wd5"):
                tenant = first
        reqid = self._workday_req_id(u)
        if tenant and reqid:
            return "workday:%s:%s" % (tenant, reqid)
        return super().canonical_job_identity(u)

    @classmethod
    def _workday_req_id(cls, url: str) -> str:
        m = cls._WD_JOB_RE.search(url or "")
        if not m:
            return ""
        seg = m.group(1).strip()
        if "_" in seg:
            seg = seg.rsplit("_", 1)[-1]
        return seg

    # =====================================================================
    # Multi-step wizard position
    # =====================================================================
    def current_step(self) -> str:
        """The active wizard step, by its ``data-automation-id`` panel marker
        (``my_information`` … ``review``), or ``unknown``."""
        for aid, name in self._STEP_MARKERS:
            if self._qs("[data-automation-id=%s]" % aid) is not None:
                return name
        return "unknown"

    def has_next_step(self) -> bool:
        """True when a Next/Continue control is present (more wizard steps follow).
        The Review step has only a Submit control → False (this is the terminal
        step the executor calls ``submit()`` on)."""
        return self._qs("[data-automation-id=pageFooterNextButton]") is not None

    def advance_step(self) -> bool:
        """Click the wizard's Next/Continue control to advance to the following
        step (the driver re-recons after). Returns whether the control was found.
        Never a submit — the terminal Review step has no Next, so this is only
        ever called on a non-terminal step."""
        el = self._qs("[data-automation-id=pageFooterNextButton]")
        if el is None:
            return False
        try:
            el.click()
        except Exception:  # pragma: no cover - defensive
            pass
        # Wait for Workday to transition to the next step.
        if self._page is not None:
            try:
                self._page.wait_for_timeout(2_000)
                self._page.wait_for_load_state("networkidle", timeout=10_000)
            except Exception:
                try:
                    self._page.wait_for_timeout(3_000)
                except Exception:
                    pass
        self._current_step = self.current_step()
        return True

    def upload_file(self, field: str, path: str) -> Dict[str, Any]:
        """Attach a résumé/PDF into Workday's hidden file-upload input.

        Workday uses ``data-automation-id=file-upload-input-ref`` (often hidden);
        Playwright ``set_input_files`` works on hidden inputs. Falls back to the
        Select files → file-chooser path.
        """
        name = os.path.basename(str(path or "")) or "resume.pdf"
        key = self._canonical_key(field)
        self._uploads[key] = name
        self._resume_path = path  # keep for mid-wizard re-attach
        if not path or self._page is None:
            return {"field": key, "filename": name, "found": False}
        page = self._page
        # Direct set_input_files on the Workday hook (preferred).
        for sel in (
            "[data-automation-id='file-upload-input-ref']",
            "input[type=file][data-automation-id*=file]",
            "input[type=file]",
        ):
            try:
                loc = page.locator(sel).first
                if loc.count():
                    loc.set_input_files(path, timeout=10_000)
                    page.wait_for_timeout(1_000)
                    logger.info("workday resume uploaded via %s", sel)
                    return {"field": key, "filename": name, "found": True}
            except Exception as e:
                logger.info("workday upload via %s failed: %s", sel, e)
        # Fallback: click Select files → native chooser.
        try:
            btn = page.locator("[data-automation-id='select-files']").first
            if btn.count():
                with page.expect_file_chooser(timeout=8_000) as fc:
                    btn.click(timeout=5_000)
                fc.value.set_files(path)
                page.wait_for_timeout(1_000)
                return {"field": key, "filename": name, "found": True}
        except Exception as e:
            logger.info("workday upload file-chooser failed: %s", e)
        return {"field": key, "filename": name, "found": False}

    def _ensure_resume_uploaded(self) -> None:
        """Re-attach résumé if a Workday file input is on the current step."""
        path = getattr(self, "_resume_path", None)
        if not path or self._page is None:
            return
        try:
            if self._page.query_selector("[data-automation-id='file-upload-input-ref']") \
                    or self._page.query_selector("input[type=file]"):
                # Skip if a file name already appears in the attachments panel.
                body = self._body_text() or ""
                if ".pdf" in body.lower() and "upload a file" in body.lower():
                    # Still try — Workday may show the label without a file.
                    pass
                self.upload_file("resume", path)
        except Exception as e:
            logger.info("workday mid-wizard resume skip: %s", e)

    def recon(self) -> List[Dict[str, Any]]:
        # Resume upload lives on My Experience — attach whenever the hook is present.
        self._ensure_resume_uploaded()
        qs = super().recon()
        self._current_step = self.current_step()
        self._has_next_step = self.has_next_step()
        return qs

    # =====================================================================
    # Field extraction keyed by data-automation-id (theme-independent)
    # =====================================================================
    def _extract_raw_fields(self) -> List[Dict[str, Any]]:
        raw: List[Dict[str, Any]] = []
        seen: set = set()
        self._widget_by_handle: Dict[str, str] = {}
        # Map formField handle → child input element for fill().
        self._formfield_child: Dict[str, Any] = {}
        candidates: List[Tuple[Any, str]] = []

        # Pass 1: direct data-automation-id on input/textarea/select/button.
        for el in self._qsa("input[data-automation-id]"):
            candidates.append((el, "input"))
        for el in self._qsa("textarea[data-automation-id]"):
            candidates.append((el, "textarea"))
        for el in self._qsa("select[data-automation-id]"):
            candidates.append((el, "select"))
        for el in self._qsa("button[data-automation-id][aria-haspopup=listbox]"):
            candidates.append((el, "dropdown"))

        # Pass 2: Workday puts data-automation-id on WRAPPER DIVs
        # (e.g. formField-legalName--firstName) with plain inputs inside.
        for wrapper in self._qsa("div[data-automation-id^='formField-']"):
            wrapper_aid = (self._attr(wrapper, "data-automation-id") or "").strip()
            if not wrapper_aid or wrapper_aid in seen:
                continue
            if not self._visible(wrapper):
                continue
            # CC-305: formField-disabilityForm wraps the whole section — still
            # extract the required disability radio/checkbox group inside it.
            if wrapper_aid == "formField-disabilityForm":
                radios = self._qsa_within(wrapper, "input[type=radio]")
                checks = self._qsa_within(wrapper, "input[type=checkbox]")
                if not radios and not checks:
                    continue
                child = (radios or checks)[0]
                child_kind = "radio" if radios else "checkbox"
                seen.add(wrapper_aid)
                label = self._label_for_wrapper(wrapper, child, wrapper_aid)
                required = True  # CC-305 "check one of the boxes" is required
                raw.append({
                    "label": label or "disability status",
                    "required": required,
                    "type": QTYPE_SELECT,
                    "options": [],
                    "handle": wrapper_aid,
                    "name": wrapper_aid,
                })
                self._widget_by_handle[wrapper_aid] = child_kind
                self._formfield_child[wrapper_aid] = wrapper
                continue
            # Skip Workday multiselect containers (pre-filled dropdowns
            # like Country/Region phone code).
            if self._qsa_within(wrapper, "[data-automation-id='multiSelectContainer']"):
                continue
            # Look for an input/textarea/select child inside the wrapper.
            child = None
            child_kind = "text"
            # Check radio buttons first.
            radios = self._qsa_within(wrapper, "input[type=radio]")
            if radios:
                child = radios[0]
                child_kind = "radio"
            # Check Workday custom dropdown BEFORE standard inputs
            # (dropdowns also contain hidden search inputs inside).
            if child is None:
                btns = self._qsa_within(wrapper, "button[aria-haspopup=listbox]")
                if btns:
                    child = btns[0]
                    child_kind = "dropdown"
            # Then standard input/textarea/select (skip if dropdown found).
            if child is None:
                for tag in ("input", "textarea", "select"):
                    children = self._qsa_within(wrapper, tag)
                    for ch in children:
                        if tag == "input":
                            itype = (self._attr(ch, "type") or "text").lower()
                            if itype in ("hidden", "submit", "button", "reset",
                                         "image", "file", "password", "radio"):
                                continue
                        child = ch
                        child_kind = tag
                        break
                    if child:
                        break
            if child is None:
                continue
            # Dedupe: skip if the child was already captured by pass 1.
            child_aid = self._attr(child, "data-automation-id") or ""
            if child_aid and child_aid in seen:
                continue
            seen.add(wrapper_aid)
            # …and claim the CHILD too, so pass 1 does not re-emit the same
            # control under its inner handle. Pass 2 runs FIRST and only marked
            # the wrapper, so `formField-yearsExperience` and `yearsExperience`
            # both reached the normalizer as separate questions — one physical
            # input reported twice, with different labels (a <label> plus an
            # aria-label). The field_id collapse downstream then had to hide it,
            # and that collapse is what silently DROPPED a genuinely distinct
            # required control on Greenhouse. Fix the double-emit at the source
            # so the normalizer never has to guess which duplicates are real.
            if child_aid:
                seen.add(child_aid)
            if child_kind == "radio":
                qtype, widget = QTYPE_SELECT, "radio"
            elif child_kind == "textarea":
                qtype, widget = QTYPE_TEXTAREA, "textarea"
            elif child_kind == "select":
                qtype, widget = QTYPE_SELECT, "select"
            elif child_kind == "dropdown":
                qtype, widget = QTYPE_SELECT, "dropdown"
            else:
                itype = (self._attr(child, "type") or "text").lower()
                qtype = _INPUT_TYPE_MAP.get(itype, QTYPE_TEXT)
                widget = ("checkbox" if itype == "checkbox"
                          else "date" if itype == "date" else "text")
            label = self._label_for_wrapper(wrapper, child, wrapper_aid)
            required = (self._attr(child, "required") is not None
                        or self._attr(child, "aria-required") == "true"
                        or self._attr(wrapper, "aria-required") == "true")
            # Also detect Workday's asterisk pattern in wrapper text.
            if not required:
                wtext = self._text(wrapper) or ""
                if "*" in wtext[:80]:
                    required = True
            self._widget_by_handle[wrapper_aid] = widget
            self._formfield_child[wrapper_aid] = child
            raw.append({
                "id": wrapper_aid, "text": label, "type": qtype,
                "required": bool(required),
                "is_legal_attestation": _is_legal(label),
            })
            if len(raw) >= _MAX_QUESTIONS:
                return raw

        # Process pass 1 candidates.
        for el, kind in candidates:
            handle = (self._attr(el, "data-automation-id") or "").strip()
            if not handle or handle in seen:
                continue
            if any(handle.startswith(p) for p in _EXECUTOR_SUPPLIED):
                continue
            if not self._visible(el):
                continue
            if kind == "input":
                itype = (self._attr(el, "type") or "text").lower()
                if itype in ("hidden", "submit", "button", "reset", "image",
                             "file", "password"):
                    continue
                qtype = _INPUT_TYPE_MAP.get(itype, QTYPE_TEXT)
                widget = ("checkbox" if itype == "checkbox"
                          else "radio" if itype == "radio"
                          else "date" if itype == "date" else "text")
            elif kind == "textarea":
                qtype, widget = QTYPE_TEXTAREA, "textarea"
            elif kind == "select":
                qtype, widget = QTYPE_SELECT, "select"
            else:
                qtype, widget = QTYPE_SELECT, "dropdown"

            seen.add(handle)
            label = self._label_for(el, handle)
            required = (self._attr(el, "required") is not None
                        or self._attr(el, "aria-required") == "true")
            self._widget_by_handle[handle] = widget
            raw.append({
                "id": handle, "text": label, "type": qtype,
                "required": bool(required), "is_legal_attestation": _is_legal(label),
            })
            if len(raw) >= _MAX_QUESTIONS:
                break
        return raw

    def _qsa_within(self, parent, selector: str) -> list:
        """Query all matching children inside a parent element."""
        try:
            return parent.query_selector_all(selector) or []
        except Exception:
            return []

    def _label_for_wrapper(self, wrapper, child, handle: str) -> str:
        """Extract label from a Workday formField wrapper div. Prefer the
        wrapper's question text over generic child aria-labels (Month/Year/
        Select One/Search) that Workday puts on inner controls."""
        _GENERIC = {
            "month", "year", "day", "search", "select one", "select one required",
            "required", "select", "type to add",
        }
        # Prefer wrapper text first (contains the real question).
        wtext = (self._text(wrapper) or "").strip()
        if wtext:
            for noise in ("Select One", "Required", "use right and left arrows",
                          "current value is", "Drop files here", "orSelect files",
                          "Type to Add Skills", "0 items selected"):
                idx = wtext.find(noise)
                if idx > 10:
                    wtext = wtext[:idx]
            wtext = re.sub(r"\*+\s*$", "", wtext).strip()
            wtext = re.sub(r"^\d+\)\s*\*?\s*", "", wtext).strip()
            wtext = re.sub(r"\*+$", "", wtext).strip()
            if wtext and wtext.lower() not in _GENERIC:
                return wtext[:160]
        for a in ("aria-label", "title", "placeholder"):
            v = self._attr(child, a)
            if v and v.strip() and v.strip().lower() not in _GENERIC:
                return v.strip()
        return self._humanize(handle.replace("formField-", ""))

    def _label_for(self, el, handle: str) -> str:
        """Workday labels: the ``aria-label`` on the control is the stable,
        theme-independent source; fall back to title/placeholder, then a humanized
        automation id. (No ``label[for]`` — Workday inputs are automation-id keyed,
        not id keyed.)"""
        for a in ("aria-label", "title", "placeholder"):
            v = self._attr(el, a)
            if v and v.strip():
                return v.strip()
        return self._humanize(handle)

    @staticmethod
    def _humanize(handle: str) -> str:
        s = re.sub(r"[_\-]+", " ", handle or "")
        s = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", s)
        return s.strip().lower() or (handle or "field")

    def _find_element(self, handle: str):
        """Resolve a field by its ``data-automation-id`` (the deterministic key).
        For ``formField-*`` handles, returns the child input inside the wrapper."""
        if not handle:
            return None
        # Check cached formField child map first.
        child = getattr(self, "_formfield_child", {}).get(handle)
        if child is not None:
            return child
        # Direct automation-id match.
        for el in self._qsa("[data-automation-id]"):
            if self._attr(el, "data-automation-id") == handle:
                # If it's a wrapper div, find the input inside.
                tag = (getattr(el, "tag_name", None) or
                       self._attr(el, "tagName") or "").lower()
                if tag in ("div", "span", "section"):
                    for itag in ("input", "textarea", "select"):
                        children = self._qsa_within(el, itag)
                        for ch in children:
                            itype = (self._attr(ch, "type") or "text").lower()
                            if itype not in ("hidden", "submit", "button",
                                             "reset", "image", "file"):
                                return ch
                return el
        return None

    # =====================================================================
    # Deterministic fill — keyed by data-automation-id, NO LLM
    # =====================================================================
    def fill(self, field_id: str, value: Any) -> Dict[str, Any]:
        """Deterministically fill by ``data-automation-id``. A field whose
        automation id is NOT on the current step is NOT silently recorded — it
        returns ``found=False`` / ``status=needs_input`` so a renamed/missing hook
        surfaces instead of writing a wrong-but-plausible value into the snapshot."""
        key = self._canonical_key(field_id)
        handle = self._handle_for(key)
        el = self._find_visible_element(handle)
        if el is None:
            return {"field_id": key, "handle": handle, "found": False,
                    "status": "needs_input", "value": None,
                    "reason": "no data-automation-id match on current step"}
        widget = getattr(self, "_widget_by_handle", {}).get(handle, "text")
        # Workday date widgets: separate Month + Year inputs inside the wrapper.
        if handle in ("formField-startDate", "formField-endDate") \
                or "Date" in (handle or "") \
                or widget == "date":
            if self._fill_month_year(handle, str(value)):
                self._filled[key] = value
                return {"field_id": key, "handle": handle, "found": True,
                        "status": "filled", "widget": "date", "value": value}
            return {"field_id": key, "handle": handle, "found": True,
                    "status": "needs_input", "value": None,
                    "reason": "date_fill_failed"}
        if widget == "radio":
            # For radio groups inside formField wrappers, click the matching
            # label/radio by value text (e.g. "Yes"/"No").
            filled = self._fill_radio(handle, str(value))
            if not filled:
                return {"field_id": key, "handle": handle, "found": True,
                        "status": "needs_input", "value": None,
                        "reason": "radio option '%s' not found" % value}
        elif widget in ("text", "textarea", "date", "checkbox"):
            if widget == "checkbox":
                # Multi-option checkbox groups: click the option by label text.
                val_s = str(value).strip()
                if val_s.lower() not in ("1", "true", "yes", "y", "on", "checked",
                                          "0", "false", "no", "n", "off", ""):
                    if self._fill_radio(handle, val_s):  # same label-click path
                        self._filled[key] = value
                        return {"field_id": key, "handle": handle, "found": True,
                                "status": "filled", "widget": "checkbox", "value": value}
                want = val_s.lower() in ("1", "true", "yes", "y", "on", "checked")
                try:
                    checked = False
                    try:
                        checked = bool(el.is_checked())
                    except Exception:
                        pass
                    if want != checked:
                        try:
                            el.click(timeout=5_000)
                        except TypeError:
                            el.click()
                except Exception as e:
                    logger.info("workday checkbox fill failed %s: %s", field_id, e)
                    return {"field_id": key, "handle": handle, "found": True,
                            "status": "needs_input", "value": None,
                            "reason": "checkbox_failed:%s" % e}
            else:
                try:
                    try:
                        el.scroll_into_view_if_needed(timeout=3_000)
                    except Exception:
                        pass
                    try:
                        el.fill(str(value), timeout=8_000)
                    except TypeError:
                        el.fill(str(value))
                except Exception as e:  # pragma: no cover - defensive
                    logger.info("workday fill set failed %s: %s", field_id, e)
                    try:
                        if self._page is not None and hasattr(self._page, "locator"):
                            loc = self._page.locator(
                                "[data-automation-id=%s]" % handle).first
                            if loc.count() and loc.is_visible():
                                loc.fill(str(value), timeout=8_000, force=True)
                            else:
                                return {"field_id": key, "handle": handle,
                                        "found": False, "status": "needs_input",
                                        "value": None, "reason": "not_visible:%s" % e}
                        else:
                            return {"field_id": key, "handle": handle,
                                    "found": False, "status": "needs_input",
                                    "value": None, "reason": str(e)[:120]}
                    except Exception as e2:
                        logger.info("workday fill retry failed %s: %s", field_id, e2)
                        return {"field_id": key, "handle": handle, "found": False,
                                "status": "needs_input", "value": None,
                                "reason": str(e2)[:120]}
        elif widget == "dropdown":
            # Workday custom dropdown: click button → wait for listbox → click option.
            # Guard: never try to pick a phone number as a dropdown option
            # (misclassified Phone Device Type → phone). Coerce to Mobile.
            val_s = str(value).strip()
            if re.fullmatch(r"[\d\s\-\+\(\)\.]{7,}", val_s) and "phone" in (handle or "").lower():
                value = "Mobile"
                val_s = "Mobile"
            filled_dd = self._fill_dropdown(handle, el, str(value))
            if not filled_dd:
                return {"field_id": key, "handle": handle, "found": True,
                        "status": "needs_input", "value": None,
                        "reason": "dropdown option '%s' not found" % value}
        # Custom dropdown/select widgets: the live Playwright driver actuates them
        # via this same automation id; self._filled is the authoritative record.
        self._filled[key] = value
        return {"field_id": key, "handle": handle, "found": True,
                "status": "filled", "widget": widget, "value": value}

    def _fill_month_year(self, handle: str, value: str) -> bool:
        """Fill Workday's split Month[/Day]/Year date widget (MM/YYYY or MM/DD/YYYY).

        Handles both ``formField-*`` wrappers and bare ``dateSectionMonth-input``
        automation ids (CC-305 Self Identify uses the latter as the recon handle).

        Workday spinbuttons often ignore fill()/JS value setters and leave a
        broken value like ``07//``. The reliable path is: focus each segment,
        clear, type digits, Tab to the next, then verify year is present.
        """
        if self._page is None or not hasattr(self._page, "locator"):
            return True
        v = (value or "").strip()
        month = day = year = None
        m = re.match(r"^(\d{1,2})[/\-](\d{1,2})[/\-](\d{4})$", v)
        if m:
            month, day, year = (
                str(int(m.group(1))).zfill(2),
                str(int(m.group(2))).zfill(2),
                m.group(3),
            )
        else:
            m = re.match(r"^(\d{1,2})[/\-](\d{4})$", v)
            if m:
                month, year = str(int(m.group(1))).zfill(2), m.group(2)
            else:
                m = re.match(r"^(\d{4})[/\-](\d{1,2})$", v)
                if m:
                    year, month = m.group(1), str(int(m.group(2))).zfill(2)
        if not month or not year:
            return False
        # CC-305 Self Identify requires day — default to today if only MM/YYYY.
        if not day:
            from datetime import datetime, timezone
            day = datetime.now(timezone.utc).strftime("%d")
        try:
            page = self._page
            # Prefer the signed-on date widget when present (Self Identify).
            signed = page.locator(
                "[data-automation-id*='dateSignedOn'] "
                "[data-automation-id='dateSectionMonth-input'], "
                "[data-automation-id='dateSignedOn'] "
                "[data-automation-id='dateSectionMonth-input']"
            )
            if "dateSignedOn" in (handle or "") or (
                    signed.count() and "dateSection" in (handle or "")):
                month_inp = page.locator(
                    "[data-automation-id*='dateSignedOn'] "
                    "[data-automation-id='dateSectionMonth-input']"
                ).first
                if not month_inp.count():
                    month_inp = page.locator(
                        "[data-automation-id='dateSectionMonth-input']"
                    ).last
                day_inp = page.locator(
                    "[data-automation-id*='dateSignedOn'] "
                    "[data-automation-id='dateSectionDay-input']"
                ).first
                if not day_inp.count():
                    day_inp = page.locator(
                        "[data-automation-id='dateSectionDay-input']"
                    ).last
                year_inp = page.locator(
                    "[data-automation-id*='dateSignedOn'] "
                    "[data-automation-id='dateSectionYear-input']"
                ).first
                if not year_inp.count():
                    year_inp = page.locator(
                        "[data-automation-id='dateSectionYear-input']"
                    ).last
            else:
                wrapper = page.locator("[data-automation-id=%s]" % handle)
                if not wrapper.count() and "dateSectionMonth" in handle:
                    month_inp = page.locator(
                        "[data-automation-id=%s]" % handle).first
                    day_inp = month_inp.locator(
                        "xpath=ancestor::*[contains(@data-automation-id,'date') "
                        "or contains(@class,'date')][1]"
                        "//*[@data-automation-id='dateSectionDay-input']"
                    ).first
                    year_inp = month_inp.locator(
                        "xpath=ancestor::*[contains(@data-automation-id,'date') "
                        "or contains(@class,'date')][1]"
                        "//*[@data-automation-id='dateSectionYear-input']"
                    ).first
                else:
                    month_inp = wrapper.locator(
                        "[data-automation-id='dateSectionMonth-input']").first
                    day_inp = wrapper.locator(
                        "[data-automation-id='dateSectionDay-input']").first
                    year_inp = wrapper.locator(
                        "[data-automation-id='dateSectionYear-input']").first

            if not month_inp.count() or not year_inp.count():
                logger.info("workday date missing segments handle=%s m=%s y=%s",
                            handle, month_inp.count(), year_inp.count())
                return False

            # Already correct? Treat as success (avoids flaky viewport clicks).
            try:
                mv0 = (month_inp.input_value() or "").strip()
                yv0 = (year_inp.input_value() or "").strip()
                if (mv0.lstrip("0") == month.lstrip("0") or mv0 == month) and (
                        yv0 == year or yv0.endswith(year[-2:])):
                    return True
            except Exception:
                pass

            # Experience dates are MM/YYYY — only Self Identify / full dates need day.
            # Prefer visible day segment; hidden day inputs must not be typed into.
            day_visible = False
            try:
                day_visible = bool(day_inp.count() and day_inp.is_visible())
            except Exception:
                day_visible = bool(day_inp.count())
            need_day = day_visible and (
                bool(re.search(r"[/\-]\d{1,2}[/\-]\d{4}", v))
                or "dateSignedOn" in (handle or "")
                or "date_signed" in (handle or "").lower()
            )

            def _scroll(loc) -> None:
                try:
                    handle_el = loc.element_handle(timeout=2_000)
                    if handle_el is not None:
                        page.evaluate(
                            "(el) => el.scrollIntoView({block:'center', inline:'nearest'})",
                            handle_el,
                        )
                        page.wait_for_timeout(100)
                except Exception:
                    pass

            def _display_ok() -> bool:
                """Match the visible date string Workday renders (more reliable
                than spinbutton input_value, which often returns a partial)."""
                try:
                    root = page.locator("[data-automation-id=%s]" % handle)
                    if not root.count():
                        root = month_inp.locator(
                            "xpath=ancestor::*[contains(@data-automation-id,'date') "
                            "or contains(@data-automation-id,'Date')][1]")
                    text = (root.inner_text(timeout=2_000) or "").replace(" ", "")
                except Exception:
                    text = ""
                if need_day:
                    targets = (
                        f"{month}/{day}/{year}",
                        f"{int(month)}/{int(day)}/{year}",
                        f"{month}{day}{year}",
                    )
                else:
                    targets = (
                        f"{month}/{year}",
                        f"{int(month)}/{year}",
                        f"{month}{year}",
                    )
                return any(t in text for t in targets)

            if _display_ok():
                return True

            # Workday date spinbuttons auto-advance after 2 digits. Type the
            # whole date as one continuous digit stream into the month input —
            # do NOT Tab between segments (that is what produces values like
            # month='2' day='27' year='2026').
            stream = f"{month}{day}{year}" if need_day else f"{month}{year}"
            _scroll(month_inp)
            typed = False
            try:
                try:
                    month_inp.click(force=True, timeout=5_000)
                except Exception:
                    try:
                        month_inp.evaluate("el => el.focus()")
                    except Exception:
                        pass
                page.keyboard.press("Control+a")
                page.keyboard.press("Backspace")
                page.keyboard.type(stream, delay=70)
                typed = True
            except Exception as e:
                logger.info("workday date keyboard stream failed %s: %s", handle, e)

            page.wait_for_timeout(300)
            if _display_ok():
                return True

            # Per-segment JS setter fallback (no clicks).
            segs = [(month_inp, month), (year_inp, year)]
            if need_day:
                segs.insert(1, (day_inp, day))
            for loc, digits in segs:
                if not loc.count():
                    continue
                try:
                    loc.evaluate(
                        """(el, v) => {
                          const setter = Object.getOwnPropertyDescriptor(
                            window.HTMLInputElement.prototype, 'value').set;
                          el.focus();
                          setter.call(el, v);
                          el.dispatchEvent(new Event('input', {bubbles:true}));
                          el.dispatchEvent(new Event('change', {bubbles:true}));
                          el.dispatchEvent(new KeyboardEvent('keyup', {bubbles:true}));
                        }""", digits)
                except Exception:
                    pass
            page.wait_for_timeout(200)

            try:
                mv = (month_inp.input_value() or "").strip()
                yv = (year_inp.input_value() or "").strip()
                dv = (day_inp.input_value() or "").strip() if need_day else ""
            except Exception:
                mv = yv = dv = ""
            ok_m = mv.lstrip("0") == month.lstrip("0") or mv == month
            ok_y = yv == year or yv.endswith(year[-2:])
            ok_d = (not need_day) or dv.lstrip("0") == day.lstrip("0") or dv == day
            if _display_ok() or (ok_m and ok_y and ok_d):
                return True
            logger.info(
                "workday date verify failed handle=%s got m=%r d=%r y=%r want %s/%s/%s typed=%s",
                handle, mv, dv, yv, month, day if need_day else "--", year, typed,
            )
            return False
        except Exception as e:
            logger.info("workday fill_month_year failed %s: %s", handle, e)
            return False


    def _listbox_for(self, btn):
        """The popup THIS button owns, never merely 'a listbox on the page'.

        Workday renders the popup in a portal outside the field wrapper, so it
        cannot be found by scoping to the wrapper — but a page-wide
        ``[role=listbox]`` matches any OTHER open listbox just as happily, which
        is how `state` came to read the phone-country-code options. Prefer the
        element the button explicitly owns; fall back to the single VISIBLE
        listbox; refuse (and say so) when the page is ambiguous."""
        page = self._page
        if page is None or not hasattr(page, "locator"):
            return None
        for attr in ("aria-controls", "aria-owns"):
            try:
                ref = (btn.get_attribute(attr) or "").strip()
            except Exception:
                ref = ""
            if not ref:
                continue
            try:
                loc = page.locator('[id="%s"]' % ref.replace('"', ""))
                if loc.count():
                    return loc
            except Exception:
                continue
        try:
            vis = page.locator("[role=listbox]:visible")
            n = vis.count()
        except Exception:
            return None
        if n == 1:
            return vis
        if n > 1:
            # Several popups open at once: we cannot tell which is ours, and
            # guessing is how a value lands in the wrong control.
            logger.warning("workday: %d listboxes visible at once — refusing to "
                           "guess which belongs to this control", n)
        return None

    # A Workday prompt list is VIRTUALIZED: only the rendered window exists in
    # the DOM, so reading it once yields a PREFIX of the real option set. Live:
    # country enumerated 199 of ~250 entries and "United States of America" —
    # alphabetically near the end — was never rendered, so a declared, valid
    # country produced `no_option_match` and blocked the application.
    _OPTION_SCROLL_PASSES = 40      # bounded: ~250-entry country list settles well inside this
    _OPTION_HARD_CAP = 600          # never collect more than this from one popup

    @classmethod
    def _options_within(cls, listbox, page=None):
        """Every option in one popup, scrolling a virtualized list to the end.

        Stops as soon as a pass adds nothing new, so a short (non-virtualized)
        list costs exactly one pass and behaves as before."""
        texts: List[str] = []
        sel = None
        for candidate in ("[role=option]", "[data-automation-id=promptOption]", "li"):
            try:
                if listbox.locator(candidate).count():
                    sel = candidate
                    break
            except Exception:
                continue
        if sel is None:
            return texts

        for _ in range(cls._OPTION_SCROLL_PASSES):
            grew = False
            try:
                loc = listbox.locator(sel)
                n = loc.count()
            except Exception:
                break
            for i in range(n):
                try:
                    t = (loc.nth(i).text_content() or "").strip()
                except Exception:
                    continue
                # "Select One" is a placeholder, never a real answer.
                if t and t.lower() != "select one" and t not in texts:
                    texts.append(t)
                    grew = True
                    if len(texts) >= cls._OPTION_HARD_CAP:
                        return texts
            if not grew:
                break
            # Pull the next window into the DOM by scrolling the LAST rendered
            # option into view; that is what the virtualizer listens to.
            try:
                listbox.locator(sel).last.scroll_into_view_if_needed(timeout=2_000)
                if page is not None:
                    page.wait_for_timeout(150)
            except Exception:
                break
        return texts

    def options_for(self, field_id: str) -> List[str]:
        """Enumerate a Workday dropdown's LIVE option texts.

        The base implementation only reads native ``<option>`` elements and
        returns [] for a custom widget, which makes ``fill_driver`` skip
        ``match_option`` and fill the RAW canonical value. Workday renders its
        selects as a button + ``[role=listbox]``, so every one of them took that
        path.

        Live consequence (Expedia, 2026-08-23): the user's declared
        ``how_did_you_hear`` is "Company website"; the live control offers
        "Company Career Site / Current Contingent Worker / Former Employee /
        I am an employee / I know someone in the company". No option contains
        "Company website", ``_fill_dropdown`` found nothing to click, and the row
        parked on a `fill_failed` wall telling the user to go do it by hand —
        for a field we had a perfectly good answer for.

        Reading the options here lets the driver's existing
        ``qb.match_option(field_id, value, options)`` map the canonical value
        onto the form's own wording BEFORE ``_fill_dropdown`` runs.

        Opens the popup, reads it, and closes it again. Returns [] on any
        failure so the previous raw-fill behaviour still applies."""
        native = super().options_for(field_id)
        if native:
            return native
        page = self._page
        if page is None or not hasattr(page, "locator"):
            return []
        handle = self._handle_for(self._canonical_key(field_id))
        if not handle:
            return []
        opened = False
        try:
            wrapper = page.locator("[data-automation-id=%s]" % handle)
            if not wrapper.count():
                return []
            btn = wrapper.locator("button[aria-haspopup=listbox]").first
            if not btn.count():
                btn = wrapper.locator("button").first
            if not btn.count():
                return []
            btn.scroll_into_view_if_needed(timeout=3_000)
            btn.click(timeout=5_000)
            opened = True
            page.wait_for_timeout(400)
            listbox = self._listbox_for(btn)
            if listbox is None:
                logger.info("workday options_for(%s): could not identify the "
                            "popup this control owns — returning no options "
                            "rather than another field's", field_id)
                return []
            texts: List[str] = self._options_within(listbox, page)
            if texts:
                logger.info("workday options_for(%s): %d live option(s)",
                            field_id, len(texts))
            return texts
        except Exception as e:
            logger.info("workday options_for(%s) failed: %s", field_id, e)
            return []
        finally:
            if opened:
                # Leave the widget as we found it; _fill_dropdown reopens it.
                try:
                    page.keyboard.press("Escape")
                    page.wait_for_timeout(150)
                except Exception:
                    pass

    def capture_validation_errors(self) -> List[Dict[str, Any]]:
        """Workday's own error surface, plus the base collector.

        The base implementation reads `.field-error` / `[class*=error-message]` /
        `[role=alert]` and keys controls by id/name. Workday uses neither: it
        renders an "Errors Found" panel behind `data-automation-id` hooks and
        identifies every control by `data-automation-id`. The result was
        `field=None reason=validation_failed` with no message — three runs stopped
        on REVIEW and every diagnosis needed an audit screenshot.

        Returns the base errors PLUS Workday's, and LOGS them, so this class of
        failure is self-diagnosing (same rationale as logging the live options on
        `no_option_match`). Error text is employer form copy, not user data."""
        out = super().capture_validation_errors()
        seen = {(e.get("field"), e.get("message")) for e in out}
        page = self._page
        if page is None or not hasattr(page, "locator"):
            return out

        # 1. Workday's error messages.
        for sel in ("[data-automation-id=errorMessage]",
                    "[data-automation-id=validationErrorLabel]",
                    "[data-automation-id=errorBanner]",
                    "[data-automation-id*=rrorMessage]"):
            try:
                loc = page.locator(sel)
                for i in range(min(loc.count(), 12)):
                    node = loc.nth(i)
                    if not node.is_visible():
                        continue
                    txt = (node.text_content() or "").strip()
                    if not txt or (None, txt[:200]) in seen:
                        continue
                    seen.add((None, txt[:200]))
                    if self._is_success_notice(txt):
                        continue
                    out.append({"field": None, "message": txt[:200],
                                "evidence": sel})
            except Exception:
                continue

        # 2. Invalid controls keyed the way Workday actually keys them.
        try:
            loc = page.locator("[aria-invalid=true]")
            for i in range(min(loc.count(), 12)):
                node = loc.nth(i)
                if not node.is_visible():
                    continue
                handle = (node.get_attribute("data-automation-id")
                          or node.get_attribute("id")
                          or node.get_attribute("name"))
                if not handle or (handle, "required/invalid") in seen:
                    continue
                seen.add((handle, "required/invalid"))
                out.append({"field": handle, "message": "required/invalid",
                            "evidence": "aria-invalid[data-automation-id]"})
        except Exception:
            pass

        if out:
            logger.warning(
                "workday validation errors (%d): %s", len(out),
                " | ".join("%s: %s" % (e.get("field") or "-",
                                       str(e.get("message"))[:200])
                           for e in out[:6]))
        return out

    def _fill_dropdown(self, handle: str, el, value: str) -> bool:
        """Actuate a Workday custom dropdown by clicking the button, waiting for
        the listbox popup, and clicking the option that matches ``value``."""
        if self._page is None or not hasattr(self._page, "locator"):
            return True  # FakePage — just record the value
        try:
            # Find and click the dropdown button inside the wrapper.
            wrapper_loc = self._page.locator(
                "[data-automation-id=%s]" % handle)
            btn = wrapper_loc.locator("button[aria-haspopup=listbox]").first
            if not btn.count():
                btn = wrapper_loc.locator("button").first
            if not btn.count():
                return False
            # If already shows desired value, skip.
            current = (btn.text_content() or "").strip()
            if current.lower() == value.lower():
                return True
            btn.scroll_into_view_if_needed(timeout=3_000)
            btn.click(timeout=5_000)
            self._page.wait_for_timeout(500)
            # Look for the listbox popup and click the matching option.
            # Scoped to the popup this button owns: a page-wide match can click
            # an option belonging to a DIFFERENT control.
            listbox = self._listbox_for(btn)
            if listbox is None:
                logger.warning("workday fill_dropdown(%s): could not identify the "
                               "popup this control owns — refusing to click", handle)
                try:
                    self._page.keyboard.press("Escape")
                except Exception:
                    pass
                return False
            candidates = [value]
            # Decline / prefer-not synonyms for EEO fields.
            vl = value.lower()
            if any(tok in vl for tok in ("decline", "do not wish", "prefer not", "don't wish",
                                          "do not want", "don't want")):
                candidates.extend([
                    "Decline to self-identify", "I do not wish to answer",
                    "I don't wish to answer", "Prefer not to say",
                    "I do not want to answer", "I Don't Wish To Answer",
                    "Decline To Self Identify", "Choose not to disclose",
                    "Decline to State (United States of America)",
                    "Decline",
                    "I don't wish to answer",
                    "No, I do not have a disability",
                    "No, I Don't Have A Disability, Or A History/Record Of Having A Disability",
                ])
            if listbox.count():
                for cand in candidates:
                    option = listbox.get_by_text(cand, exact=False).first
                    if option.count():
                        option.click(timeout=5_000)
                        self._page.wait_for_timeout(300)
                        return True
            # Workday sometimes uses [data-automation-id=promptOption] — still
            # INSIDE our own popup, never page-wide.
            options = listbox.locator("[data-automation-id=promptOption]")
            for i in range(min(options.count(), 80)):
                opt = options.nth(i)
                text = (opt.text_content() or "").strip()
                tl = text.lower()
                for cand in candidates:
                    if cand.lower() in tl or tl in cand.lower():
                        opt.click(timeout=5_000)
                        self._page.wait_for_timeout(300)
                        return True
            # Close dropdown if no match found.
            self._page.keyboard.press("Escape")
            return False
        except Exception as e:
            logger.info("workday fill_dropdown failed %s: %s", handle, e)
            try:
                self._page.keyboard.press("Escape")
            except Exception:
                pass
            return False

    def _fill_radio(self, handle: str, value: str) -> bool:
        """Click the radio option whose label matches ``value`` inside a
        formField wrapper or automation-id group. Returns True on success."""
        val_lower = value.strip().lower()
        # Find the wrapper div.
        wrapper = None
        for el in self._qsa("[data-automation-id]"):
            if self._attr(el, "data-automation-id") == handle:
                wrapper = el
                break
        if wrapper is None:
            return False
        # Find all radio inputs and their labels inside.
        radios = self._qsa_within(wrapper, "input[type=radio]")
        for radio in radios:
            # Check associated label: Workday uses <label for=id>.
            rid = self._attr(radio, "id") or ""
            label_el = self._qs('label[for="%s"]' % rid) if rid else None
            label_text = (self._text(label_el) or "").strip().lower() if label_el else ""
            if label_text == val_lower:
                try:
                    radio.scroll_into_view_if_needed(timeout=3_000)
                except Exception:
                    pass
                try:
                    radio.click(timeout=5_000)
                except TypeError:
                    radio.click()
                return True
            # Also try clicking the label itself.
        # Fallback: use page locator to click by text (exact, then contains).
        if self._page is not None and hasattr(self._page, "locator"):
            try:
                wrapper_loc = self._page.locator(
                    "[data-automation-id=%s]" % handle)
                label_loc = wrapper_loc.get_by_text(value, exact=True)
                if label_loc.count():
                    label_loc.first.click(timeout=5_000)
                    return True
                # Soft match for long CC-305 options.
                for cand in (
                    value,
                    "I Don't Wish To Answer",
                    "I do not want to answer",
                    "Don't wish to answer",
                    "Prefer not",
                ):
                    soft = wrapper_loc.get_by_text(cand, exact=False)
                    if soft.count():
                        soft.first.click(timeout=5_000)
                        return True
                # Page-wide last resort for Self Identify radios.
                soft = self._page.get_by_text(value, exact=False)
                if soft.count():
                    soft.first.click(timeout=5_000)
                    return True
            except Exception:
                pass
        return False

    def _find_visible_element(self, handle: str):
        """Prefer a visible automation-id match (hidden prior-step clones exist).

        For ``formField-*`` handles, returns the child input inside the wrapper.
        Falls back to the first match so the FakePage fixture harness (and
        partially hydrated pages) still resolve handles.
        """
        if not handle:
            return None
        # Check cached formField child map first.
        child = getattr(self, "_formfield_child", {}).get(handle)
        if child is not None:
            return child
        matches = []
        for el in self._qsa("[data-automation-id]"):
            if self._attr(el, "data-automation-id") == handle:
                matches.append(el)
        for el in matches:
            if self._visible(el):
                # If wrapper div, dig into child input.
                tag = (getattr(el, "tag_name", None) or
                       self._attr(el, "tagName") or "").lower()
                if tag in ("div", "span", "section"):
                    # For dropdowns/radios, return the wrapper itself so
                    # _fill_dropdown / _fill_radio can operate on it.
                    widget = getattr(self, "_widget_by_handle", {}).get(
                        handle, "text")
                    if widget in ("dropdown", "radio"):
                        return el
                    for itag in ("input", "textarea", "select"):
                        children = self._qsa_within(el, itag)
                        for ch in children:
                            itype = (self._attr(ch, "type") or "text").lower()
                            if itype not in ("hidden", "submit", "button",
                                             "reset", "image", "file"):
                                return ch
                return el
        if matches:
            return self._find_element(handle) or matches[0]
        return None

    # =====================================================================
    # Human-takeover: account gate is a HARD boundary here
    # =====================================================================
    def required_account(self) -> bool:
        """Workday almost always requires a per-tenant candidate account — this is a
        capability FACT, hard TRUE, independent of whether the gate is on screen
        right now (that is ``_account_gate_present``)."""
        return True

    def _account_gate_present(self) -> bool:
        """Is the login / create-account gate actually on screen? A password field
        or a sign-in/create-account automation id (theme-independent), or explicit
        account copy. Filling a form STEP must not be mistaken for the gate, so this
        is DISTINCT from the always-true ``required_account`` capability."""
        if self._qs("input[type=password]") is not None:
            return True
        for aid in self._ACCOUNT_AUTOMATION_IDS:
            if self._qs("[data-automation-id=%s]" % aid) is not None:
                return True
        return bool(_ACCOUNT_RE.search(self._body_text()))

    def _account_evidence(self) -> List[str]:
        ev: List[str] = []
        if self._qs("input[type=password]") is not None:
            ev.append("password_field")
        for aid in self._ACCOUNT_AUTOMATION_IDS:
            if self._qs("[data-automation-id=%s]" % aid) is not None:
                ev.append("automation_id:%s" % aid)
        return ev or ["account_or_login_gate"]

    def _check_takeover(self) -> Optional[Dict[str, Any]]:
        """Same order as the base, but the account boundary uses the ON-SCREEN gate
        (``_account_gate_present``) rather than the always-true capability, so form
        steps are fillable while the actual login/create-account wall pauses."""
        if self._breaker.is_open():
            return {"reason": TAKEOVER_FORM_CHANGED,
                    "evidence": self._breaker.evidence() or ["circuit_open"]}
        if self._captcha_present():
            return {"reason": TAKEOVER_CAPTCHA, "evidence": ["captcha_challenge_visible"]}
        if _MFA_RE.search(self._body_text()):
            return {"reason": TAKEOVER_MFA, "evidence": ["mfa_or_otp_prompt"]}
        if self._otp_gate_present():
            return {"reason": TAKEOVER_MFA, "evidence": ["otp_automation_id"]}
        if self._account_gate_present():
            return {"reason": TAKEOVER_ACCOUNT_REQUIRED,
                    "evidence": self._account_evidence()}
        novel = self._novel_legal_attestation()
        if novel:
            return {"reason": TAKEOVER_NOVEL_LEGAL, "evidence": novel}
        return None

    def _credential_inputs_present(self) -> bool:
        """An email AND a password control are in the DOM right now.

        Distinct from ``_account_gate_present`` (which also fires on account
        COPY and on a federated panel with no inputs at all): this is the
        precondition ``fill_account_credentials`` actually needs."""
        email = (self._qs("[data-automation-id=email]")
                 or self._qs("input[type=email]")
                 or self._qs("input[autocomplete=username]")
                 or self._qs("input[name=email]"))
        pwd = (self._qs("[data-automation-id=password]")
               or self._qs("input[type=password]")
               or self._qs("input[name=password]"))
        return email is not None and pwd is not None

    def _reveal_email_auth_form(self) -> bool:
        """Click the "…with email" option on a FEDERATED auth panel so the
        email/password controls render. Returns True only when they now exist.

        No-op (False) when the inputs are already present or the panel offers no
        email route. Deliberately CSS + visible-text only — no ``locator``
        pseudo-selectors — so it is exercisable by the offline DOM harness and
        cannot be defeated by a theme rename.
        """
        if self._page is None or self._credential_inputs_present():
            return False
        clicked = False
        for aid in self._EMAIL_AUTH_AUTOMATION_IDS:
            el = self._qs("[data-automation-id=%s]" % aid)
            if el is None:
                continue
            try:
                el.click()
                clicked = True
                break
            except Exception:
                continue
        if not clicked:
            for el in self._qsa("button, a, [role=button]")[:80]:
                try:
                    text = (el.inner_text() or "").strip()
                except Exception:
                    continue
                if not text or not _EMAIL_AUTH_RE.search(text):
                    continue
                try:
                    if hasattr(el, "is_visible") and not el.is_visible():
                        continue
                    el.click()
                    clicked = True
                    break
                except Exception:
                    continue
        if not clicked:
            return False
        try:
            self._page.wait_for_timeout(1_500)
        except Exception:
            pass
        revealed = self._credential_inputs_present()
        logger.info("workday federated auth panel: email form %s",
                    "revealed" if revealed else "still hidden")
        return revealed

    def _otp_gate_present(self) -> bool:
        """True when a Workday/Okta OTP input is on screen."""
        for aid in self._OTP_AUTOMATION_IDS:
            if self._qs("[data-automation-id=%s]" % aid) is not None:
                return True
            # Case-sensitive contains — automation ids are camelCase.
            if self._qs("[data-automation-id*=%s]" % aid) is not None:
                return True
        try:
            if self._qs("input[autocomplete=one-time-code]") is not None:
                return True
        except Exception:
            pass
        return False

    def dismiss_overlays(self) -> None:
        """Cookie / OneTrust / GDPR banners that intercept Apply clicks."""
        if self._page is None:
            return
        for sel in (
            "button:has-text('Accept Cookies')",
            "button:has-text('Accept All')",
            "button:has-text('Accept all')",
            "button:has-text('I Accept')",
            "button:has-text('Agree')",
            "button:has-text('Decline')",  # Intel: still clears overlay
            "[data-automation-id=legalNoticeAcceptButton]",
            "[data-automation-id=legalNoticeDeclineButton]",
            "#onetrust-accept-btn-handler",
            "button#onetrust-accept-btn-handler",
            "[id*=onetrust] button:has-text('Accept')",
            "button[aria-label*='Accept']",
        ):
            try:
                loc = self._page.locator(sel).first
                if loc.count() and loc.is_visible():
                    loc.click(timeout=2_500, force=True)
                    self._page.wait_for_timeout(400)
                    return
            except Exception:
                continue

    def recover_error_page(self) -> bool:
        """Intel/Workday sometimes lands on 'Something went wrong — refresh'.

        Returns True if an error shell was detected and a recovery action ran
        (reload or refresh control). Callers should re-select the adapter after.
        """
        if self._page is None:
            return False
        page = self._page
        try:
            body = (page.inner_text("body") or "")[:4000]
        except Exception:
            body = ""
        if not re.search(r"something went wrong|please refresh the page", body, re.I):
            return False
        logger.info("workday error shell detected — recovering")
        # Prefer an in-page Refresh control if present.
        for sel in (
            "button:has-text('Refresh')",
            "a:has-text('Refresh')",
            "button:has-text('Try again')",
            "button:has-text('Retry')",
        ):
            try:
                loc = page.locator(sel).first
                if loc.count() and loc.is_visible():
                    loc.click(timeout=3_000, force=True)
                    page.wait_for_timeout(2_500)
                    try:
                        page.wait_for_load_state("networkidle", timeout=12_000)
                    except Exception:
                        pass
                    return True
            except Exception:
                continue
        try:
            page.reload(wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(2_000)
            try:
                page.wait_for_load_state("networkidle", timeout=12_000)
            except Exception:
                pass
            return True
        except Exception as e:
            logger.info("workday error-shell reload failed: %s", e)
            return False

    def click_apply_cta(self) -> bool:
        """Open the application wizard from a job-details / Apply landing page.

        Workday tenants are SPAs: wait for hydration, clear cookie banners, then
        click Apply (role/text + automation-id). Uses Playwright locators so
        ``:has-text`` / role queries work reliably.
        """
        if self._page is None:
            return False
        page = self._page
        self.dismiss_overlays()
        # Wait for SPA Apply control (Intel / classic WD posting shells).
        for wait_sel in (
            "[data-automation-id=adventureButton]",
            "[data-automation-id=jobPostingApplyButton]",
            "[data-automation-id=applyButton]",
            "button:has-text('Apply')",
            "a:has-text('Apply')",
        ):
            try:
                page.wait_for_selector(wait_sel, state="visible", timeout=8_000)
                break
            except Exception:
                continue
        self.dismiss_overlays()

        selectors = (
            "[data-automation-id=adventureButton]",
            "[data-automation-id=jobPostingApplyButton]",
            "[data-automation-id=applyButton]",
            "a[data-automation-id*=Apply]",
            "button[data-automation-id*=Apply]",
            "button:has-text('Apply Manually')",
            "a:has-text('Apply Manually')",
            "button:has-text('Apply')",
            "a:has-text('Apply')",
        )
        before = page.url
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if not loc.count():
                    continue
                if not loc.is_visible():
                    continue
                loc.scroll_into_view_if_needed(timeout=3_000)
                try:
                    loc.click(timeout=5_000)
                except Exception:
                    loc.click(timeout=5_000, force=True)
                page.wait_for_timeout(1_200)
                # Intel / WD: "Start Your Application" chooser modal.
                self._click_application_path()
                # Success: URL change, account gate, or form fields appeared.
                if page.url != before:
                    return True
                if self._account_gate_present() or self._otp_gate_present():
                    return True
                if (self._qs("[data-automation-id=email]")
                        or self._qs("input[type=email]")
                        or self._qs("[data-automation-id=file-upload-input-ref]")
                        or self._qs("input[data-automation-id*=formField]")):
                    return True
                # Still on JD but click registered — treat as ok so caller waits.
                return True
            except Exception:
                continue
        # get_by_role last resort (exact name Apply).
        try:
            btn = page.get_by_role("button", name="Apply", exact=True)
            if btn.count():
                btn.first.click(timeout=5_000, force=True)
                page.wait_for_timeout(1_200)
                self._click_application_path()
                return True
        except Exception:
            pass
        return False

    def _click_application_path(self) -> bool:
        """Chooser modal after Apply: Prefer Apply Manually (account/wizard)."""
        if self._page is None:
            return False
        page = self._page
        for sel in (
            "[data-automation-id=applyManually]",
            "button:has-text('Apply Manually')",
            "a:has-text('Apply Manually')",
            "[data-automation-id=autofillWithResume]",
            "button:has-text('Autofill with Resume')",
            "button:has-text('Apply with Resume')",
        ):
            try:
                loc = page.locator(sel).first
                if not loc.count() or not loc.is_visible():
                    continue
                loc.click(timeout=5_000)
                page.wait_for_timeout(1_500)
                return True
            except Exception:
                continue
        return False

    # Consent checkboxes on a Create Account panel that may be ticked without
    # asserting anything about the candidate. ALLOW-LIST, deliberately: the same
    # panel can carry checkboxes that ARE claims about the person ("I am a
    # protected veteran", "I am a US citizen"), and blanket-ticking those is the
    # exact fabrication class AUTOAPPLY_REPAIR_SPEC.md §0 forbids. Acknowledging
    # a privacy policy asserts nothing false — same rule as POLICY_ACCEPT_DEFAULT
    # in application_question_bank.
    _ACCOUNT_CONSENT_RE = re.compile(
        r"(privacy\s+(policy|notice|statement)|terms\s+(of\s+use|and\s+conditions|of\s+service)"
        r"|i\s+have\s+read|i\s+acknowledge|i\s+agree|candidate\s+privacy"
        r"|data\s+(protection|processing)\s+notice)", re.I)

    # A claim ABOUT the person — never auto-ticked, even if it also matches the
    # allow-list above. Checked first and wins.
    _ACCOUNT_CONSENT_NEVER_RE = re.compile(
        r"(veteran|disab|citizen|sponsor|work\s+authoriz|felony|convict|clearance"
        r"|gender|race|ethnic|hispanic|over\s+18|age\s+of\s+18|export\s+control)", re.I)

    def _tick_account_consent(self) -> int:
        """Tick REQUIRED consent checkboxes on the Create Account panel.

        Workday refuses account creation with "Error: Please check the box to
        continue" until the privacy-policy box is ticked. Nothing ticked it, so
        every create-account attempt was rejected AFTER email + password +
        verify-password had been filled correctly — which is indistinguishable
        from a credential failure in the logs, and is why the Expedia row parked
        on `account_required` on three consecutive runs.

        Returns the number of boxes ticked. Never raises."""
        page = self._page
        if page is None:
            return 0
        ticked = 0
        try:
            boxes = page.locator("input[type=checkbox]")
            count = min(boxes.count(), 12)
        except Exception:
            return 0
        for i in range(count):
            try:
                box = boxes.nth(i)
                if not box.is_visible() or box.is_checked():
                    continue
                label = ""
                box_id = box.get_attribute("id")
                for getter in (
                    lambda: box.get_attribute("aria-label") or "",
                    lambda: (page.locator(f"label[for='{box_id}']").first.inner_text()
                             if box_id else ""),
                    lambda: box.locator(
                        "xpath=ancestor::*[self::label or self::div][1]").first.inner_text(),
                ):
                    try:
                        label = (getter() or "").strip()
                        if label:
                            break
                    except Exception:
                        continue
                if not label:
                    continue
                if self._ACCOUNT_CONSENT_NEVER_RE.search(label):
                    logger.info("account consent: REFUSING to auto-tick a claim "
                                "checkbox (%s)", label[:80])
                    continue
                if not self._ACCOUNT_CONSENT_RE.search(label):
                    continue
                box.check(timeout=3_000)
                ticked += 1
                logger.info("account consent: ticked %r", label[:80])
            except Exception:
                continue
        return ticked

    def fill_account_credentials(self, email: str, password: str, *,
                                 create: bool = False) -> bool:
        """Fill sign-in or create-account form. Returns True if submit clicked."""
        if self._page is None:
            return False
        page = self._page

        # A federated panel ("Sign in with Apple / Google / LinkedIn / email")
        # has NO email or password input until the email route is chosen. Reveal
        # it BEFORE anything below looks for those controls — otherwise every
        # `_find` returns None and this method returns False without ever having
        # touched the form (the live failure on NXP / State Street / Midland).
        self._reveal_email_auth_form()

        if create:
            # Never click header "Sign In" — it opens a modal over Create Account.
            link = self._qs("[data-automation-id=createAccountLink]")
            if link is not None:
                try:
                    link.click()
                    page.wait_for_timeout(600)
                except Exception:
                    pass
            # Close a stray Sign In dialog if it covers the create form.
            self._dismiss_signin_overlay()
        else:
            self.dismiss_overlays()
            # Already on a Sign In form → do not click header "Sign In" (resets).
            already = (
                self._qs("[data-automation-id=email]") is not None
                or self._qs("input[type=email]") is not None
                or self._qs("input[type=password]") is not None
            )
            if not already:
                for sel in (
                    "[data-automation-id=signInLink]",
                    "button:has-text('Sign In')",
                    "a:has-text('Sign In')",
                ):
                    try:
                        loc = page.locator(sel).first
                        if loc.count() and loc.is_visible():
                            loc.click(timeout=2_500)
                            page.wait_for_timeout(600)
                            break
                    except Exception:
                        continue
            # If Sign In form says create instead, follow Create Account when asked.
            try:
                body_l = ((page.inner_text("body") or "")[:800]).lower()
                if "don't have an account" in body_l or "create account" in body_l:
                    # Prefer create when vault has never succeeded for this tenant
                    # and Sign In would just bounce — caller decides via create flag.
                    pass
            except Exception:
                pass

        # Prefer fields inside the active dialog / create panel.
        # Do NOT scope to a bare ``form`` — Intel login embeds a honeypot form
        # ("Enter website") that steals the first email/password match.
        root = page
        for scope in (
            "[data-automation-id=createAccountSection]",
            "[data-automation-id=signInContent]",
            "[data-automation-id=authView]",
            "[role=dialog]",
            "section:has(h1:has-text('Sign In'))",
            "section:has(h2:has-text('Sign In'))",
            "div:has(> h1:has-text('Sign In'))",
            "div:has(> h2:has-text('Create Account'))",
        ):
            try:
                loc = page.locator(scope).first
                if loc.count() and loc.is_visible():
                    root = loc
                    break
            except Exception:
                continue

        def _find(sel: str):
            try:
                loc = root.locator(sel).first
                if loc.count():
                    return loc
            except Exception:
                pass
            try:
                loc = page.locator(sel).first
                if loc.count():
                    return loc
            except Exception:
                pass
            return None

        email_el = (_find("[data-automation-id=email]")
                    or _find("input[type=email]")
                    or _find("input[autocomplete=username]")
                    or _find("input[name=email]")
                    or _find("input[id*=email i]"))
        if email_el is None:
            try:
                email_el = page.get_by_label(re.compile(r"email\s*address", re.I)).first
                if not email_el.count():
                    email_el = None
            except Exception:
                email_el = None
        pass_el = (_find("[data-automation-id=password]")
                   or _find("input[type=password]")
                   or _find("input[name=password]")
                   or _find("input[autocomplete=current-password]")
                   or _find("input[autocomplete=new-password]"))
        if pass_el is None:
            try:
                pass_el = page.get_by_label(re.compile(r"^password$", re.I)).first
                if not pass_el.count():
                    pass_el = None
            except Exception:
                pass_el = None
        if email_el is None or pass_el is None:
            return False
        try:
            email_el.fill(email, timeout=5_000)
            pass_el.fill(password, timeout=5_000)
        except Exception:
            try:
                email_el.click()
                email_el.fill("")
                email_el.type(email, delay=20)
                pass_el.click()
                pass_el.fill("")
                pass_el.type(password, delay=20)
            except Exception:
                return False

        if create:
            confirm = (_find("[data-automation-id=confirmPassword]")
                       or _find("[data-automation-id=verifyPassword]")
                       or _find("input[name*=confirm][type=password]")
                       or _find("input[aria-label*='Verify'][type=password]")
                       or _find("input[aria-label*='Confirm'][type=password]"))
            if confirm is None:
                try:
                    pwds = page.locator("input[type=password]")
                    if pwds.count() >= 2:
                        confirm = pwds.nth(1)
                except Exception:
                    confirm = None
            if confirm is not None:
                try:
                    confirm.fill(password, timeout=5_000)
                except Exception:
                    pass

        if create:
            # Workday blocks Create Account with "Please check the box to
            # continue" until the privacy-policy consent box is ticked. Must run
            # AFTER the password fields are filled and BEFORE the submit click.
            self._tick_account_consent()
            btn_sels = (
                "[data-automation-id=createAccountSubmitButton]",
                "button:has-text('Create Account')",
                "button:has-text('Create account')",
                "button[type=submit]",
            )
        else:
            btn_sels = (
                "[data-automation-id=signInSubmitButton]",
                "button[data-automation-id=signInSubmitButton]",
                "button[type=submit]",
            )
        clicked = False
        for sel in btn_sels:
            try:
                btn = root.locator(sel).first if hasattr(root, "locator") else None
                if btn is None or not btn.count():
                    btn = page.locator(sel).first
                if not btn.count() or not btn.is_visible():
                    continue
                if create and "sign" in sel.lower():
                    continue
                try:
                    btn.scroll_into_view_if_needed(timeout=3_000)
                except Exception:
                    pass
                btn.click(timeout=5_000)
                page.wait_for_timeout(1_200)
                clicked = True
                break
            except Exception:
                continue
        if not clicked and create:
            try:
                btn = page.get_by_role("button", name=re.compile(r"create\s*account", re.I))
                if btn.count():
                    btn.first.scroll_into_view_if_needed(timeout=3_000)
                    btn.first.click(timeout=5_000)
                    page.wait_for_timeout(1_200)
                    clicked = True
            except Exception:
                pass
        if not clicked and not create:
            # Prefer the Sign In control nearest the password field — never the
            # header/nav "Sign In" which resets the form.
            try:
                # Stable Workday automation id first.
                btn = page.locator("[data-automation-id=signInSubmitButton]").first
                if btn.count() and btn.is_visible():
                    btn.scroll_into_view_if_needed(timeout=3_000)
                    btn.click(timeout=5_000, force=True)
                    page.wait_for_timeout(1_200)
                    clicked = True
            except Exception:
                pass
            if not clicked:
                try:
                    candidates = page.locator(
                        "button:has-text('Sign In'), button:has-text('Sign in')")
                    n = candidates.count()
                    chosen = None
                    for i in range(n):
                        c = candidates.nth(i)
                        if not c.is_visible():
                            continue
                        box = c.bounding_box() or {}
                        # Header is typically y < 80; form submit is mid-page.
                        if (box.get("y") or 0) < 100:
                            continue
                        chosen = c
                    if chosen is None and n:
                        chosen = candidates.last if n > 1 else candidates.first
                    if chosen is not None:
                        chosen.scroll_into_view_if_needed(timeout=3_000)
                        chosen.click(timeout=5_000, force=True)
                        page.wait_for_timeout(1_200)
                        clicked = True
                except Exception:
                    pass
        # Verify credentials stuck in the email control (catch header-reset races).
        try:
            if email_el is not None:
                got = (email_el.input_value() or "").strip()
                if got and got.lower() != (email or "").strip().lower():
                    logger.info("workday auth email mismatch after fill")
        except Exception:
            pass
        return clicked

    def _dismiss_signin_overlay(self) -> None:
        """Close a Sign In dialog that overlays the Create Account panel."""
        if self._page is None:
            return
        page = self._page
        for sel in (
            "[role=dialog] button[aria-label=Close]",
            "[role=dialog] button[aria-label=close]",
            "[role=dialog] button:has-text('×')",
            "[data-automation-id=click_close]",
            "[data-automation-id=closeButton]",
            "div[role=dialog] button >> nth=0",
        ):
            try:
                # Only dismiss if dialog title looks like Sign In and create
                # fields remain behind it.
                dlg = page.locator("[role=dialog]").first
                if not dlg.count() or not dlg.is_visible():
                    return
                text = (dlg.inner_text() or "")[:200].lower()
                if "sign in" not in text and "sign-in" not in text:
                    return
                btn = page.locator(sel).first
                if btn.count() and btn.is_visible():
                    btn.click(timeout=2_000, force=True)
                    page.wait_for_timeout(400)
                    return
            except Exception:
                continue
        # Escape key as last resort.
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(300)
        except Exception:
            pass

    def _human(self, reason: str, evidence: Any) -> Dict[str, Any]:
        """Enrich the pending action with a concrete Workday next step + the wizard
        position so a person knows EXACTLY what to do on takeover."""
        out = super()._human(reason, evidence)
        action_map = {
            TAKEOVER_ACCOUNT_REQUIRED: "create_or_sign_in_candidate_account",
            TAKEOVER_FORM_CHANGED: "human_review_form_change",
            TAKEOVER_CAPTCHA: "solve_challenge_manually",
            TAKEOVER_MFA: "complete_mfa_manually",
            TAKEOVER_NOVEL_LEGAL: "confirm_legal_answer",
        }
        pa = out.get("pending_action") or {}
        pa["action"] = action_map.get(reason, pa.get("action", "submit"))
        pa["ats"] = self.family
        pa["current_step"] = self.current_step()
        # True = we will NOT invent an account without the vault path. The
        # browser executor may still create/sign-in via ats_credential_service.
        pa["never_auto_create_account"] = (reason == TAKEOVER_ACCOUNT_REQUIRED)
        out["pending_action"] = pa
        return out

    # =====================================================================
    # Submit + Confirmation
    # =====================================================================
    _WD_CONFIRM_AIDS = (
        "confirmationMessage", "confirmationMessagePanel",
        "appliedSuccessfully", "applicationSubmitted",
        "jobApplicationConfirmation", "submittedSuccessfully",
    )
    _WD_CONFIRM_RE = re.compile(
        r"(thank you for (?:your )?appl(?:y|ication)|"
        r"application (?:has been |was )?(?:successfully )?(?:submitted|received|sent)|"
        r"we(?:['’`]ve| have) received your application|"
        r"successfully submitted|"
        r"your application (?:is|has been) (?:complete|submitted)|"
        r"application complete|"
        r"you(?:['’`]ve| have) already applied|"
        r"already applied for this job)",
        re.IGNORECASE,
    )

    def _click_submit(self) -> bool:
        """Click Workday's Review-step Submit (pageFooterSubmitButton) once."""
        el = self._qs("[data-automation-id=pageFooterSubmitButton]")
        if el is None:
            # Fall back to any button labelled Submit Application / Submit.
            for btn in self._qsa("button"):
                t = (self._text(btn) or "").strip().lower()
                if t in ("submit", "submit application", "submit my application"):
                    el = btn
                    break
        if el is None:
            return super()._click_submit()
        self._submit_clicks += 1
        try:
            try:
                el.scroll_into_view_if_needed(timeout=3_000)
            except Exception:
                pass
            try:
                el.click(timeout=8_000)
            except TypeError:
                el.click()
        except Exception as e:  # pragma: no cover - defensive
            logger.info("workday submit click failed: %s", e)
            return False
        # SPA confirmation paints after the POST settles.
        if self._page is not None:
            try:
                self._page.wait_for_timeout(2_500)
                self._page.wait_for_load_state("networkidle", timeout=15_000)
            except Exception:
                try:
                    self._page.wait_for_timeout(3_000)
                except Exception:
                    pass
        return True

    def detect_confirmation(self) -> Dict[str, Any]:
        evidence: List[Dict[str, Any]] = []
        for aid in self._WD_CONFIRM_AIDS:
            el = self._qs("[data-automation-id=%s]" % aid)
            if el is not None and self._visible(el):
                t = self._text(el) or aid
                evidence.append({"automation_id": aid, "text": t[:200]})
                break
        if not evidence:
            for el in self._qsa("[data-automation-id*=confirmation]"):
                if self._visible(el):
                    t = self._text(el)
                    if t:
                        evidence.append({
                            "automation_id": self._attr(el, "data-automation-id"),
                            "text": t[:200],
                        })
                        break
        # URL heuristics — Workday often redirects to /confirmation or Candidate Home.
        url = (self._url or "")
        if self._page is not None:
            try:
                url = self._page.url or url
            except Exception:
                pass
        url_l = url.lower()
        if any(tok in url_l for tok in (
                "/confirmation", "applied=true", "applicationsuccess",
                "candidatehome", "/myapplications")):
            evidence.append({"url": url[:200]})
        body = self._body_text()
        m = self._WD_CONFIRM_RE.search(body or "")
        if m:
            evidence.append({"text_match": m.group(0)[:120]})
        base = super().detect_confirmation()
        evidence.extend(base.get("evidence", []))
        return {"confirmed": bool(evidence), "evidence": evidence}

    # =====================================================================
    # Evidence-based support verdict (WITH the honest caveat)
    # =====================================================================
    def supported_reason(self) -> Dict[str, Any]:
        base = super().supported_reason()
        if not base.get("supported"):
            return base  # circuit_open / low-confidence already honest
        base["caveat"] = self._CAVEAT
        base["reason"] = base["reason"] + " [caveat: %s]" % self._CAVEAT
        # If the account gate is actually on screen, be explicit: this page needs a
        # human even though the family is supported.
        if self._account_gate_present():
            base["recommended_action"] = "human_takeover"
        return base


class LinkedInAdapter(AtsAdapter):
    """LinkedIn is explicitly NEVER automated (policy). Detected only to refuse."""
    family = "linkedin"
    supported = False
    NEVER_AUTOMATE = True
    MIN_CONFIDENCE = 0.5
    _HOST_NEEDLES = ("linkedin.com",)
    _DOM_SIGNALS = (
        ("[data-tracking-control-name]", "linkedin-tracking"),
    )


class GenericAdapter(AtsAdapter):
    """Conservative fallback for an UNKNOWN host. It may recon/prepare/fill common
    fields, but it does NOT claim autonomous support and its submit defaults to a
    human review/takeover — it must never pretend it drove an unknown form."""
    family = "generic"
    supported = False
    MIN_CONFIDENCE = 0.0

    @classmethod
    def sniff(cls, page: Any, url: str) -> Dict[str, Any]:
        has_form = _page_has(page, "form") or _page_has(page, "input")
        return {"ats": cls.family, "confidence": 0.2 if has_form else 0.0,
                "evidence": ["generic-form" if has_form else "no-form"]}

    def supported_reason(self) -> Dict[str, Any]:
        return {"supported": False,
                "reason": "generic fallback — unrecognized ATS; human review/takeover "
                          "recommended (adapter may prepare answers but will not submit)",
                "evidence": ["no_known_ats_host"],
                "recommended_action": "human_review"}


# Family adapters tried (in order) by the registry. Generic is the fallback.
_FAMILY_ADAPTERS: Tuple[type, ...] = (
    GreenhouseAdapter, LeverAdapter, AshbyAdapter, WorkdayAdapter, LinkedInAdapter,
)


def detect_adapter(page: Any, url: str) -> Dict[str, Any]:
    """Run every registered family's ``sniff`` and return the best
    ``{ats, confidence, evidence, adapter_cls}`` (Generic when nothing clears its
    threshold). Pure detection — instantiates nothing."""
    best: Optional[Tuple[float, type, Dict[str, Any]]] = None
    for cls in _FAMILY_ADAPTERS:
        s = cls.sniff(page, url)
        if s["confidence"] >= cls.MIN_CONFIDENCE:
            if best is None or s["confidence"] > best[0]:
                best = (s["confidence"], cls, s)
    if best is None:
        s = GenericAdapter.sniff(page, url)
        return {**s, "adapter_cls": GenericAdapter}
    return {**best[2], "adapter_cls": best[1]}


def select_adapter(page: Any, url: str = "", *,
                   baseline_fingerprint: Optional[str] = None) -> AtsAdapter:
    """Instantiate the best adapter for ``(page, url)`` — a supported family
    adapter, a registered unsupported stub (Workday), a policy refusal (LinkedIn),
    or the conservative Generic fallback."""
    det = detect_adapter(page, url)
    cls = det["adapter_cls"]
    return cls(page, url or (getattr(page, "url", "") or ""),
               baseline_fingerprint=baseline_fingerprint)


# ---------------------------------------------------------------------------
# small pure helpers
# ---------------------------------------------------------------------------
def _host(url: Optional[str]) -> str:
    if not url or not isinstance(url, str):
        return ""
    try:
        return (urlparse(url.strip()).hostname or "").lower()
    except Exception:
        return ""


def _page_has(page: Any, css: str) -> bool:
    if page is None:
        return False
    try:
        return page.query_selector(css) is not None
    except Exception:
        return False
