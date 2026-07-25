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
        ("myInformationPage", "my_information"),
        ("myExperiencePage", "my_experience"),
        ("questionnairePage", "application_questions"),
        ("voluntaryDisclosuresPage", "voluntary_disclosures"),
        ("selfIdentificationPage", "self_identify"),
        ("reviewPage", "review"),
    )

    # Automation ids that mark the candidate account / sign-in gate.
    _ACCOUNT_AUTOMATION_IDS = (
        "signInContent", "signInFormo", "createAccountLink", "signInLink",
        "createAccountSubmitButton", "signInSubmitButton", "backToSignInLink",
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
        return True

    def recon(self) -> List[Dict[str, Any]]:
        qs = super().recon()
        # Cache the wizard position alongside the normalized fields so the executor
        # knows whether to advance (Next) or submit (Review).
        self._current_step = self.current_step()
        self._has_next_step = self.has_next_step()
        return qs

    # =====================================================================
    # Field extraction keyed by data-automation-id (theme-independent)
    # =====================================================================
    def _extract_raw_fields(self) -> List[Dict[str, Any]]:
        raw: List[Dict[str, Any]] = []
        seen = set()
        self._widget_by_handle: Dict[str, str] = {}
        candidates: List[Tuple[Any, str]] = []
        for el in self._qsa("input[data-automation-id]"):
            candidates.append((el, "input"))
        for el in self._qsa("textarea[data-automation-id]"):
            candidates.append((el, "textarea"))
        for el in self._qsa("select[data-automation-id]"):
            candidates.append((el, "select"))
        # Workday's custom button-style dropdowns / multiselects.
        for el in self._qsa("button[data-automation-id][aria-haspopup=listbox]"):
            candidates.append((el, "dropdown"))

        for el, kind in candidates:
            handle = (self._attr(el, "data-automation-id") or "").strip()
            if not handle or handle in seen:
                continue
            if any(handle.startswith(p) for p in _EXECUTOR_SUPPLIED):
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
            else:  # Workday custom dropdown button
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
        """Resolve a field by its ``data-automation-id`` (the deterministic key)."""
        if not handle:
            return None
        for el in self._qsa("[data-automation-id]"):
            if self._attr(el, "data-automation-id") == handle:
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
        el = self._find_element(handle)
        if el is None:
            return {"field_id": key, "handle": handle, "found": False,
                    "status": "needs_input", "value": None,
                    "reason": "no data-automation-id match on current step"}
        widget = getattr(self, "_widget_by_handle", {}).get(handle, "text")
        if widget in ("text", "textarea", "date", "checkbox", "radio"):
            try:
                el.fill(value)
            except Exception as e:  # pragma: no cover - defensive
                logger.info("workday fill set failed %s: %s", field_id, e)
        # Custom dropdown/select widgets: the live Playwright driver actuates them
        # via this same automation id; self._filled is the authoritative record.
        self._filled[key] = value
        return {"field_id": key, "handle": handle, "found": True,
                "status": "filled", "widget": widget, "value": value}

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
        if self._account_gate_present():
            return {"reason": TAKEOVER_ACCOUNT_REQUIRED,
                    "evidence": self._account_evidence()}
        novel = self._novel_legal_attestation()
        if novel:
            return {"reason": TAKEOVER_NOVEL_LEGAL, "evidence": novel}
        return None

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
        pa["never_auto_create_account"] = (reason == TAKEOVER_ACCOUNT_REQUIRED)
        out["pending_action"] = pa
        return out

    # =====================================================================
    # Confirmation (Workday marks it with a confirmation automation id)
    # =====================================================================
    def detect_confirmation(self) -> Dict[str, Any]:
        evidence: List[Dict[str, Any]] = []
        for el in self._qsa("[data-automation-id*=confirmation]"):
            if self._visible(el):
                t = self._text(el)
                evidence.append({"automation_id": self._attr(el, "data-automation-id"),
                                 "text": t[:200]})
                break
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
